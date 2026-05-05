"""
회의 일괄 처리 API 라우터.

목적: `POST /api/meetings/batch` 엔드포인트와 해당 엔드포인트 전용
스키마·헬퍼를 api.routes 모놀리스에서 분리한다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.dependencies import (
    get_job_queue as _get_job_queue,
)
from api.dependencies import (
    get_pipeline_manager as _get_pipeline_manager,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# meeting_id 유효성 검증 정규식 (path traversal 방지)
_MEETING_ID_PATTERN = re.compile(r"^[\w\-\.]+$")


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """백그라운드 태스크의 미처리 예외를 로깅한다.

    Args:
        task: 완료된 asyncio.Task 객체
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            f"백그라운드 태스크 실패: {task.get_name()}: {exc}",
            exc_info=exc,
        )


def _validate_meeting_id(meeting_id: str) -> None:
    """meeting_id 형식을 검증한다 (path traversal 방지).

    Args:
        meeting_id: 검증할 회의 ID

    Raises:
        HTTPException: 유효하지 않은 형식일 때 (400)
    """
    if not _MEETING_ID_PATTERN.match(meeting_id):
        raise HTTPException(
            status_code=400,
            detail=f"유효하지 않은 회의 ID 형식입니다: {meeting_id}",
        )


class BatchActionRequest(BaseModel):
    """일괄 처리 요청 스키마.

    Attributes:
        action: 수행할 작업 종류 — "transcribe" | "summarize" | "full"
        scope: 대상 회의 수집 정책 — "all" | "recent" | "selected"
        hours: scope="recent" 일 때의 시간 윈도우 (1~720)
        meeting_ids: scope="selected" 일 때의 명시 회의 ID 목록.
            최대 500 개로 제한 (Phase 6 보안 감사 Medium-01: DoS 차단).
    """

    action: Literal["transcribe", "summarize", "full"]
    scope: Literal["all", "recent", "selected"]
    hours: int = Field(default=24, ge=1, le=720)
    # 보안 Medium-01 (Phase 6): 비정상적으로 큰 배열로 fs I/O / 정규식 매칭이
    # 폭주하는 것을 차단한다. 500 은 운영 환경의 단일 일괄 처리 상한선.
    meeting_ids: list[str] = Field(default_factory=list, max_length=500)


class BatchActionResponse(BaseModel):
    """일괄 처리 응답 스키마.

    Attributes:
        status: "ok" 또는 "no_targets"
        message: 사람이 읽을 수 있는 결과 메시지
        action: 요청한 action 값 (echo)
        scope: 요청한 scope 값 (echo)
        matched: 후보로 식별된 회의 수 (필터 적용 전)
        queued: 실제 백그라운드 큐에 들어간 회의 수
        skipped: matched - queued (분류 불일치, audio 부재, 권한 등)
        meeting_ids: 큐잉된 회의 ID 목록
    """

    status: Literal["ok", "no_targets"]
    message: str
    action: str
    scope: str
    matched: int
    queued: int
    skipped: int
    meeting_ids: list[str]


def _has_merge_checkpoint(checkpoints_dir: Path, meeting_id: str) -> bool:
    """merge.json 체크포인트 존재 여부를 반환한다.

    Args:
        checkpoints_dir: 체크포인트 루트 디렉토리
        meeting_id: 회의 ID

    Returns:
        merge.json 이 있으면 True
    """
    return (checkpoints_dir / meeting_id / "merge.json").is_file()


def _has_summary_output(outputs_dir: Path, meeting_id: str) -> bool:
    """요약 결과물(summary.md 또는 meeting_minutes.md) 존재 여부를 반환한다.

    레거시 회의는 meeting_minutes.md, 신규 회의는 summary.md 를 사용한다.
    둘 중 하나라도 있으면 요약 완료로 간주한다.

    Args:
        outputs_dir: 출력 루트 디렉토리
        meeting_id: 회의 ID

    Returns:
        둘 중 하나라도 있으면 True
    """
    out_dir = outputs_dir / meeting_id
    return (out_dir / "summary.md").is_file() or (out_dir / "meeting_minutes.md").is_file()


def _classify_meeting_for_batch(
    checkpoints_dir: Path,
    outputs_dir: Path,
    meeting_id: str,
) -> Literal["transcribe", "summarize", "done"]:
    """회의의 현재 진행 단계를 분류한다.

    분류 규칙:
        - merge 체크포인트 없음 → "transcribe" (전사부터 필요)
        - merge 있음 + summary 없음 → "summarize" (LLM 단계만 필요)
        - merge + summary 모두 있음 → "done" (처리 불필요)

    Args:
        checkpoints_dir: 체크포인트 루트
        outputs_dir: 출력 루트
        meeting_id: 회의 ID

    Returns:
        분류 결과 문자열
    """
    if not _has_merge_checkpoint(checkpoints_dir, meeting_id):
        return "transcribe"
    if _has_summary_output(outputs_dir, meeting_id):
        return "done"
    return "summarize"


def _is_meeting_eligible(
    action: str,
    classification: str,
) -> bool:
    """주어진 action 에 대해 분류 결과가 적합한지 판단한다.

    매핑:
        - action="transcribe" → classification == "transcribe" 만 허용
        - action="summarize"  → classification == "summarize" 만 허용
        - action="full"       → classification ∈ {"transcribe", "summarize"}

    Args:
        action: 요청 action
        classification: _classify_meeting_for_batch 결과

    Returns:
        적합하면 True
    """
    if action == "transcribe":
        return classification == "transcribe"
    if action == "summarize":
        return classification == "summarize"
    if action == "full":
        return classification in ("transcribe", "summarize")
    return False


async def _resolve_audio_path(
    queue: Any,
    meeting_id: str,
    base_dir_resolved: Path,
) -> Path | None:
    """JobQueue 에서 audio_path 를 조회하고 base_dir 내부인지 검증한다.

    보안 (Phase 6 Medium-02): SQLite 직접 편집이나 심링크 공격으로 base_dir
    외부 경로가 들어와도 파이프라인에 도달하지 못하도록 strict resolve 후
    base_dir 하위 여부를 검증한다.

    성능 (Phase 6 perf M-1): base_dir 의 resolve() 결과는 호출자가 사전 1회
    계산해 전달한다. 회의별로 반복 resolve() 호출하지 않는다.

    Args:
        queue: AsyncJobQueue 인스턴스
        meeting_id: 회의 ID
        base_dir_resolved: 사전 resolve 된 base_dir 절대 경로

    Returns:
        검증을 통과한 audio_path 절대 경로. 부재·미존재·외부 경로면 None.
    """
    # AsyncJobQueue 의 내부 동기 핸들 (테스트 mock 호환)
    sync_queue = getattr(queue, "_queue", None) or getattr(queue, "queue", None)
    if sync_queue is None:
        logger.warning(f"일괄 처리: JobQueue 핸들을 얻을 수 없음 ({meeting_id})")
        return None

    job = await asyncio.to_thread(sync_queue.get_job_by_meeting_id, meeting_id)
    if job is None or not getattr(job, "audio_path", None):
        return None

    try:
        # strict=True 로 실재하지 않는 경로는 즉시 차단
        candidate = Path(job.audio_path).resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        logger.warning(f"일괄 처리: audio_path resolve 실패 ({meeting_id}): {exc}")
        return None

    # 보안 Medium-02: base_dir 외부 경로 차단
    try:
        if not candidate.is_relative_to(base_dir_resolved):
            logger.warning(
                f"일괄 처리: audio_path 가 base_dir 외부를 가리킴 — 차단 "
                f"({meeting_id}: {candidate})"
            )
            return None
    except ValueError:
        # is_relative_to 가 OS/플랫폼에 따라 ValueError 를 낼 수 있음
        return None

    return candidate


def _collect_candidate_ids_sync(
    scope: str,
    meeting_ids: list[str],
    all_jobs: list[Any],
    hours: int,
    checkpoints_dir: Path,
) -> list[str]:
    """scope 정책에 따라 후보 회의 ID 목록을 수집한다 (동기 함수).

    asyncio.to_thread 로 호출되어 이벤트 루프 블로킹을 방지한다 (Phase 6 perf C-1).

    수집 정책:
        - selected: 입력 meeting_ids 를 dedupe 만 적용해 그대로 사용
        - recent:   all_jobs 의 created_at 을 파싱하여 cutoff 기준 윈도우 필터
        - all:      checkpoints_dir.iterdir() 로 디스크 스캔

    중복 제거 (Phase 3 Major #1): 같은 회의가 두 번 처리되어 LLM 토큰을 낭비하거나
    summary.md 가 덮어써지는 사고를 방지하기 위해 list(dict.fromkeys(...)) 로
    순서를 보존한 채 중복을 제거한다.

    Args:
        scope: "all" | "recent" | "selected"
        meeting_ids: scope="selected" 일 때 사용할 ID 목록
        all_jobs: scope="recent" 일 때 사용할 Job 목록 (created_at 보유)
        hours: scope="recent" 의 시간 윈도우
        checkpoints_dir: scope="all" 일 때 스캔할 디렉토리

    Returns:
        중복 제거된 회의 ID 목록 (순서 보존)
    """
    from datetime import datetime, timedelta

    candidate_ids: list[str] = []

    if scope == "selected":
        candidate_ids = list(meeting_ids)
    elif scope == "recent":
        cutoff = datetime.now() - timedelta(hours=hours)
        for job in all_jobs:
            mid = getattr(job, "meeting_id", None)
            created_at = getattr(job, "created_at", None)
            if not mid or not created_at:
                continue
            try:
                created_dt = datetime.fromisoformat(str(created_at))
            except (ValueError, TypeError):
                # 파싱 실패는 명시적으로 로깅하고 건너뛴다
                logger.warning(f"일괄 처리: created_at 파싱 실패 — 건너뜀 ({mid}: {created_at!r})")
                continue
            if created_dt >= cutoff:
                candidate_ids.append(mid)
    elif scope == "all":
        if checkpoints_dir.is_dir():
            for cp_dir in sorted(checkpoints_dir.iterdir()):
                if cp_dir.is_dir():
                    candidate_ids.append(cp_dir.name)

    # Phase 3 Major #1: 순서 보존 dedupe
    return list(dict.fromkeys(candidate_ids))


def _classify_eligibility_sync(
    candidate_ids: list[str],
    action: str,
    scope: str,
    checkpoints_dir: Path,
    outputs_dir: Path,
) -> list[tuple[str, str]]:
    """후보 ID 목록을 분류하고 적합한 회의만 (id, classification) 페어로 반환한다.

    동기 함수로 asyncio.to_thread 를 통해 호출되어 이벤트 루프를 막지 않는다
    (Phase 6 perf C-1).

    scope != "selected" 인 경우, 디스크에서 가져온 ID 도 path traversal 방어를
    위해 _validate_meeting_id 로 재검증한다. 검증 실패 ID 는 silently skip
    (HTTPException 던지지 않음 — 디스크 자료는 사용자 입력이 아니므로).

    Args:
        candidate_ids: 사전 수집된 회의 ID 목록
        action: 요청 action
        scope: 요청 scope (selected 인지 검사용)
        checkpoints_dir: 체크포인트 루트
        outputs_dir: 출력 루트

    Returns:
        (meeting_id, classification) 페어 목록 — eligibility 통과한 회의만
    """
    pairs: list[tuple[str, str]] = []
    for mid in candidate_ids:
        # selected 는 엔드포인트에서 미리 _validate_meeting_id 로 검증됨.
        # selected 가 아닌 경우 (recent / all) 는 디스크 자료라 재검증 후 skip.
        if scope != "selected":
            if not _MEETING_ID_PATTERN.match(mid):
                logger.warning(f"일괄 처리: 디스크에서 발견된 비정상 meeting_id 건너뜀: {mid!r}")
                continue

        classification = _classify_meeting_for_batch(checkpoints_dir, outputs_dir, mid)
        if _is_meeting_eligible(action, classification):
            pairs.append((mid, classification))

    return pairs


@router.post("/meetings/batch", response_model=BatchActionResponse)
async def batch_action(
    request: Request,
    body: BatchActionRequest,
) -> BatchActionResponse:
    """전사·요약·full 통합 일괄 처리 엔드포인트.

    동작 흐름:
        1. config / pipeline / queue 로딩 (없으면 503)
        2. base_dir 절대 경로를 1회 resolve (Phase 6 perf M-1)
        3. scope=selected 면 _validate_meeting_id 로 사전 검증
           scope=recent 면 queue.get_all_jobs() 로 Job 목록 미리 조회
        4. 후보 ID 수집 — asyncio.to_thread (Phase 6 perf C-1)
        5. matched = len(candidate_ids)
        6. 분류·eligibility 검사 — asyncio.to_thread
        7. transcribe 분류 항목은 audio_path 사전 검증 (Phase 3 Major #2)
        8. queued == 0 이면 status="no_targets" 응답
        9. 백그라운드 task 로 회의별 순차 실행 (한 건 실패해도 다음 진행)

    Args:
        request: FastAPI Request 객체 (app.state 접근용)
        body: 일괄 처리 요청 스키마 (Pydantic 검증 통과)

    Returns:
        BatchActionResponse — matched / queued / skipped 카운트와 ID 목록

    Raises:
        HTTPException: 파이프라인/설정/큐 미초기화(503), meeting_ids 형식 오류(400)
    """
    # === 1. 의존성 로딩 ===
    pipeline = _get_pipeline_manager(request)
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="서버 설정이 초기화되지 않았습니다.",
        )
    queue = _get_job_queue(request)

    checkpoints_dir = config.paths.resolved_checkpoints_dir
    outputs_dir = config.paths.resolved_outputs_dir
    # Phase 6 perf M-1: base_dir resolve 를 1회만 수행하여 _resolve_audio_path
    # 호출 시마다 반복 계산하지 않는다.
    base_dir_resolved = config.paths.resolved_base_dir

    # === 2. selected 사전 검증 / recent Job 사전 조회 ===
    if body.scope == "selected":
        for mid in body.meeting_ids:
            _validate_meeting_id(mid)
        all_jobs: list[Any] = []
    elif body.scope == "recent":
        # Phase 3 Major: AsyncJobQueue.get_all_jobs() 는 비동기 (내부적으로
        # to_thread). asyncio.to_thread 로 다시 감싸지 않는다.
        all_jobs = await queue.get_all_jobs()
    else:  # "all"
        all_jobs = []

    # === 3. 후보 ID 수집 (Phase 6 perf C-1: to_thread) ===
    candidate_ids = await asyncio.to_thread(
        _collect_candidate_ids_sync,
        body.scope,
        body.meeting_ids,
        all_jobs,
        body.hours,
        checkpoints_dir,
    )
    matched = len(candidate_ids)

    # === 4. 분류·eligibility (Phase 6 perf C-1: to_thread) ===
    eligible_pairs = await asyncio.to_thread(
        _classify_eligibility_sync,
        candidate_ids,
        body.action,
        body.scope,
        checkpoints_dir,
        outputs_dir,
    )

    # === 5. transcribe 분류는 audio_path 사전 검증 (Phase 3 Major #2) ===
    # audio 파일이 없거나 base_dir 외부면 백그라운드 큐에 넣지 않고 skipped 로
    # 카운트한다. 이렇게 해야 응답의 queued 가 실제 실행 가능 수와 일치한다.
    final_items: list[tuple[str, str, Path | None]] = []
    for mid, classification in eligible_pairs:
        if classification == "transcribe":
            audio_path = await _resolve_audio_path(queue, mid, base_dir_resolved)
            if audio_path is None:
                # 사전 제외 — skipped 로 카운트
                continue
            final_items.append((mid, classification, audio_path))
        else:
            # summarize 분류는 audio_path 불필요
            final_items.append((mid, classification, None))

    queued = len(final_items)
    skipped = matched - queued
    queued_ids = [mid for (mid, _cls, _ap) in final_items]

    # === 6. 후보 0 건이면 즉시 종료 ===
    if queued == 0:
        return BatchActionResponse(
            status="no_targets",
            message="일괄 처리 대상 회의가 없습니다.",
            action=body.action,
            scope=body.scope,
            matched=matched,
            queued=0,
            skipped=skipped,
            meeting_ids=[],
        )

    # === 7. 백그라운드 task ===
    async def _run_batch(
        items: list[tuple[str, str, Path | None]],
        action: str,
    ) -> None:
        """회의별로 분류에 맞는 파이프라인 메서드를 순차 호출한다.

        한 회의 실패는 logger.exception 으로 기록 후 다음 회의 진행.

        Args:
            items: (meeting_id, classification, audio_path) 튜플 목록
            action: 요청 action (로그용)
        """
        for mid, classification, audio_path in items:
            try:
                if classification == "transcribe":
                    if audio_path is None:
                        # 사전 검증을 통과했으므로 이 경로는 정상적으로 도달
                        # 불가능. 안전망으로 logger.warning 후 건너뜀.
                        logger.warning(
                            f"일괄 처리: transcribe 단계인데 audio_path 가 None — 건너뜀 ({mid})"
                        )
                        continue
                    logger.info(f"일괄 처리[{action}] 전사 시작: {mid} ({audio_path})")
                    await pipeline.run(
                        audio_path,
                        meeting_id=mid,
                        skip_llm_steps=True,
                    )
                    logger.info(f"일괄 처리[{action}] 전사 완료: {mid}")
                elif classification == "summarize":
                    logger.info(f"일괄 처리[{action}] 요약 시작: {mid}")
                    await pipeline.run_llm_steps(mid)
                    logger.info(f"일괄 처리[{action}] 요약 완료: {mid}")
                else:
                    logger.warning(f"일괄 처리: 알 수 없는 분류 '{classification}' 건너뜀 ({mid})")
            except Exception:
                # 한 건 실패가 나머지 회의를 막지 않는다
                logger.exception(f"일괄 처리[{action}] 회의 실패: {mid}")

    task = asyncio.create_task(
        _run_batch(final_items, body.action),
        name=f"batch-action-{body.action}",
    )
    running_tasks = getattr(request.app.state, "running_tasks", None)
    if running_tasks is not None:
        running_tasks.add(task)
        task.add_done_callback(_log_task_exception)
        task.add_done_callback(running_tasks.discard)
    else:
        task.add_done_callback(_log_task_exception)

    logger.info(
        f"일괄 처리 시작: action={body.action}, scope={body.scope}, "
        f"matched={matched}, queued={queued}, skipped={skipped}"
    )

    return BatchActionResponse(
        status="ok",
        message=f"일괄 처리를 시작합니다 ({queued}건).",
        action=body.action,
        scope=body.scope,
        matched=matched,
        queued=queued,
        skipped=skipped,
        meeting_ids=queued_ids,
    )
