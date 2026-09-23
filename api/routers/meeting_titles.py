"""단건·일괄 AI 제목을 접수하고 앱 소유 작업으로 생성·적용한다."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import deque
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.dependencies import get_config, get_job_queue, get_meeting_mutation_coordinator
from api.routers.meeting_detail import (
    _validate_meeting_id,
    get_transcript,
    suggest_meeting_title,
)
from api.routers.transcription_models import require_loopback_server
from core.job_queue import Job, JobQueue, JobQueueError
from core.model_manager import get_model_manager

logger = logging.getLogger(__name__)
router = APIRouter()


class TitleJobsRequest(BaseModel):
    """선택한 회의를 중복 접수 방지 키와 함께 전달한다."""

    meeting_ids: list[str] = Field(min_length=1, max_length=500)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def _queue(request: Request) -> JobQueue:
    """비동기 래퍼에서 실제 SQLite 큐를 가져온다."""
    queue = get_job_queue(request)
    return getattr(queue, "queue", queue)  # type: ignore[no-any-return]


async def _source_revision(request: Request, meeting_id: str) -> str:
    """읽기 가능한 전사문 내용을 비교용 해시로 고정한다."""
    transcript = await get_transcript(request, meeting_id)
    if not any(item.text.strip() for item in transcript.utterances):
        raise HTTPException(status_code=409, detail="전사문이 없어 제목을 만들 수 없습니다.")
    return hashlib.sha256(transcript.model_dump_json().encode()).hexdigest()


class TitleWorker:
    """한 앱에서 제목 생성을 순차 실행하고 결과를 접수 이력에 보존한다."""

    def __init__(self, request: Request) -> None:
        self.request = request
        self.queue = _queue(request)
        self.lock = asyncio.Lock()
        self.pending: set[str] = set()
        self.items: deque[tuple[str, Job, str]] = deque()
        self.task: asyncio.Task[None] | None = None

    async def submit(self, body: TitleJobsRequest) -> dict[str, Any]:
        """검증·기록 후 생성 완료를 기다리지 않고 접수 결과를 반환한다."""
        mids = list(dict.fromkeys(body.meeting_ids))
        for mid in mids:
            _validate_meeting_id(mid)
        async with self.lock:
            saved = await asyncio.to_thread(self.queue.get_batch_receipts, body.request_id)
            if saved:
                receipt = saved[0]
                if (
                    receipt.get("action") != "title"
                    or receipt.get("requested_meeting_ids") != mids
                ):
                    raise HTTPException(
                        status_code=409, detail="다른 요청에 사용된 접수 ID입니다."
                    )
                return {k: v for k, v in receipt.items() if k not in {"events", "created_at"}}
            candidates = []
            accepted = []
            for mid in mids:
                job = await asyncio.to_thread(self.queue.get_job_by_meeting_id, mid)
                reason = ""
                revision = ""
                if mid in self.pending:
                    reason = "이미 AI 제목을 생성 중입니다."
                elif job is None:
                    reason = "회의를 찾을 수 없습니다."
                elif job.status not in {"completed", "failed", "recorded"}:
                    reason = "전사 작업 완료 후 사용할 수 있습니다."
                elif get_meeting_mutation_coordinator(self.request).locked(mid):
                    reason = "현재 이 녹취를 처리 중입니다."
                else:
                    try:
                        revision = await _source_revision(self.request, mid)
                    except HTTPException as exc:
                        reason = str(exc.detail)
                if not reason and job is not None:
                    accepted.append((body.request_id, job, revision))
                candidates.append(
                    dict(
                        meeting_id=mid,
                        title=job.title if job else "",
                        admission="skipped" if reason else "queued",
                        reason=reason,
                    )
                )
            state = self.request.app.state
            if not getattr(state, "batch_session", None):
                state.batch_session = str(uuid4())
            queued_ids = [item[1].meeting_id for item in accepted]
            receipt = dict(
                request_id=body.request_id,
                action="title",
                scope="selected",
                status="ok" if accepted else "no_targets",
                message=f"AI 제목 {len(accepted)}건을 백그라운드에서 생성합니다.",
                matched=len(mids),
                queued=len(accepted),
                skipped=len(mids) - len(accepted),
                meeting_ids=queued_ids,
                requested_meeting_ids=mids,
                candidates=candidates,
                server_session=state.batch_session,
                background_ids=queued_ids,
            )
            await asyncio.to_thread(self.queue.save_batch_receipt, receipt)
            self.pending.update(queued_ids)
            self.items.extend(accepted)
            if accepted and (self.task is None or self.task.done()):
                self.task = asyncio.create_task(self.run(), name="meeting-title-worker")
                tasks = getattr(state, "running_tasks", None)
                if tasks is None:
                    tasks = state.running_tasks = set()
                tasks.add(self.task)
                self.task.add_done_callback(tasks.discard)
            return receipt

    async def event(self, rid: str, mid: str, status: str, label: str) -> None:
        """요청을 명시하여 다른 작업의 접수 기록과 섞이지 않도록 한다."""
        await asyncio.to_thread(
            self.queue.record_batch_event,
            mid,
            dict(status=status, status_label=label),
            rid,
        )

    async def run(self) -> None:
        """개별 실패는 분리하고 종료 취소 시 남은 작업도 중단으로 기록한다."""
        current: tuple[str, Job, str] | None = None
        try:
            while self.items:
                current = self.items.popleft()
                rid, job, revision = current
                try:
                    await self.process(rid, job, revision)
                except HTTPException as exc:
                    await self.event(rid, job.meeting_id, "failed", str(exc.detail))
                except Exception:
                    logger.exception(f"AI 제목 작업 실패: meeting_id={job.meeting_id}")
                    await self.event(
                        rid, job.meeting_id, "failed", "AI 제목 생성 실패 · 다시 시도"
                    )
                self.pending.discard(job.meeting_id)
                current = None
        except asyncio.CancelledError:
            remaining = ([current] if current else []) + list(self.items)
            self.items.clear()
            for rid, job, _ in remaining:
                # 종료 시 실제 native 수명은 ModelLoadManager가 계속 소유한다.
                await self.event(rid, job.meeting_id, "interrupted", "앱 종료로 제목 생성 중단")
                self.pending.discard(job.meeting_id)
            raise

    async def process(self, rid: str, snapshot: Job, revision: str) -> None:
        """전사문과 작업 세대를 확인하고 수동 제목 변경을 존중하여 적용한다."""
        mid = snapshot.meeting_id
        receipts = await asyncio.to_thread(self.queue.get_batch_receipts, rid)
        events = [event for event in receipts[0]["events"] if event["meeting_id"] == mid]
        if events and events[-1].get("status") == "cancelled":
            return
        coordinator = get_meeting_mutation_coordinator(self.request)
        async with coordinator.lease(mid):
            current = await asyncio.to_thread(self.queue.get_job_by_meeting_id, mid)
            if current != snapshot or await _source_revision(self.request, mid) != revision:
                await self.event(rid, mid, "skipped", "녹취 또는 제목이 변경되어 건너뜀")
                return
            await self.event(rid, mid, "running", "AI 제목 생성 중")
            manager = getattr(self.request.app.state, "model_manager", None) or get_model_manager()
            async with asyncio.timeout(get_config(self.request).llm.request_timeout_seconds):
                while manager.get_status().get("native_cleanup_pending"):
                    await asyncio.sleep(0.1)
            suggestion = await suggest_meeting_title(self.request, mid)
            if await _source_revision(self.request, mid) != revision:
                await self.event(rid, mid, "skipped", "전사문이 변경되어 건너뜀")
                return
            applied = await asyncio.to_thread(
                self.queue.apply_generated_title,
                snapshot,
                suggestion.title,
                rid,
                date_source=getattr(suggestion, "date_source", ""),
                sampled=getattr(suggestion, "sampled", False),
            )
            if not applied:
                await self.event(rid, mid, "skipped", "수동 변경 또는 작업 상태 변경으로 건너뜀")


@router.post("/meetings/titles")
async def create_title_jobs(request: Request, body: TitleJobsRequest) -> dict[str, Any]:
    """AI 제목을 자동 적용할 백그라운드 작업으로 접수한다."""
    require_loopback_server(get_config(request), request, feature_label="AI 제목 만들기")
    worker = getattr(request.app.state, "title_worker", None)
    if worker is None:
        worker = request.app.state.title_worker = TitleWorker(request)
    # 응답 연결이 끊겨도 durable 접수 후 worker 등록까지 마친다.
    task = asyncio.create_task(worker.submit(body), name="meeting-title-admission")
    tasks = getattr(request.app.state, "running_tasks", None)
    if tasks is None:
        tasks = request.app.state.running_tasks = set()
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return await asyncio.shield(task)


@router.post("/title-jobs/{request_id}/undo")
async def undo_title_jobs(request: Request, request_id: str) -> dict[str, int]:
    """AI 적용 뒤 바뀌지 않은 제목만 이전 값으로 되돌린다."""
    require_loopback_server(get_config(request), request, feature_label="AI 제목 되돌리기")
    queue = _queue(request)
    receipts = await asyncio.to_thread(queue.get_batch_receipts, request_id)
    if not receipts or receipts[0].get("action") != "title":
        raise HTTPException(status_code=404, detail="AI 제목 접수를 찾을 수 없습니다.")
    latest = {event["meeting_id"]: event for event in receipts[0]["events"]}
    restored = 0
    skipped = 0
    for mid in receipts[0]["meeting_ids"]:
        event = latest.get(mid, {})
        if event.get("status") != "completed":
            skipped += 1
            continue
        async with get_meeting_mutation_coordinator(request).lease(mid):
            job = await asyncio.to_thread(queue.get_job_by_meeting_id, mid)
            if (
                job is None
                or job.id != event["job_id"]
                or job.title != event["applied_title"]
                or job.updated_at != event["write_revision"]
                or job.audio_path != event["audio_path"]
            ):
                skipped += 1
                continue
            changed = await asyncio.to_thread(
                queue.apply_generated_title,
                job,
                event["previous_title"],
                request_id,
                restore=True,
            )
            restored += int(changed)
            skipped += int(not changed)
    return dict(restored=restored, skipped=skipped)


@router.post("/title-jobs/{request_id}/cancel")
async def cancel_title_jobs(request: Request, request_id: str) -> dict[str, int]:
    """모델 수명을 강제로 끊지 않고 제목 적용과 남은 항목을 취소한다."""
    require_loopback_server(get_config(request), request, feature_label="AI 제목 취소")
    try:
        cancelled = await asyncio.to_thread(_queue(request).cancel_title_request, request_id)
    except JobQueueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return dict(cancelled=cancelled)
