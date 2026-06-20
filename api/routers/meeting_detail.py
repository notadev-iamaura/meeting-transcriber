"""단일 회의 상세 API 라우터.

목록/일괄 작업은 ``api.routes`` 와 전용 batch router 에 남기고, 단일 회의 상세 조회,
상태 전이, 재전사, 오디오 스트리밍, 전사/요약 조회 및 편집, 단건 요약 실행을 담당한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.dependencies import get_job_queue as _get_job_queue
from api.dependencies import get_outputs_dir as _get_outputs_dir
from api.dependencies import get_pipeline_manager as _get_pipeline_manager
from core.io_utils import atomic_write_json as _atomic_write_json
from core.io_utils import atomic_write_text as _atomic_write_text
from steps.embedder import IndexPurgeError, purge_meeting_index

logger = logging.getLogger(__name__)

router = APIRouter()


class _JsonFileCache:
    """JSON 파일을 mtime 기반으로 캐싱하는 스레드 안전 캐시."""

    def __init__(self, max_size: int = 64) -> None:
        self._cache: dict[str, tuple[float, Any]] = {}
        self._max_size = max_size
        self._lock = threading.Lock()

    def get(self, file_path: Path) -> Any:
        """캐시된 JSON 데이터를 반환한다. 변경 시 자동 갱신한다."""
        key = str(file_path)
        current_mtime = file_path.stat().st_mtime

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                cached_mtime, cached_data = cached
                if cached_mtime == current_mtime:
                    return cached_data

        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)

        with self._lock:
            if len(self._cache) >= self._max_size and key not in self._cache:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
            self._cache[key] = (current_mtime, data)

        return data

    def invalidate(self, file_path: Path) -> None:
        """특정 파일의 캐시를 무효화한다."""
        with self._lock:
            self._cache.pop(str(file_path), None)


_json_cache = _JsonFileCache()

_MEETING_ID_PATTERN = re.compile(r"^[\w\-\.]+$")


def _validate_meeting_id(meeting_id: str) -> None:
    """meeting_id 형식을 검증한다 (path traversal 방지)."""
    if not _MEETING_ID_PATTERN.match(meeting_id):
        raise HTTPException(
            status_code=400,
            detail=f"유효하지 않은 회의 ID 형식입니다: {meeting_id}",
        )


def _get_config(request: Request) -> Any:
    """app.state 에서 AppConfig 를 가져온다."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="서버 설정이 초기화되지 않았습니다.",
        )
    return config


def _read_pipeline_state_for_response(config: Any, meeting_id: str) -> dict[str, Any] | None:
    """응답 보정용 pipeline_state.json 을 읽는다."""
    state_path = config.paths.resolved_checkpoints_dir / meeting_id / "pipeline_state.json"
    if not state_path.is_file():
        return None

    try:
        data = _json_cache.get(state_path)
    except Exception as exc:
        logger.warning(f"pipeline_state.json 응답 보정 읽기 실패: {meeting_id}, error={exc}")
        return None

    return data if isinstance(data, dict) else None


def _has_transcript_artifact(config: Any, meeting_id: str) -> bool:
    """회의 전사 탭을 구성할 수 있는 산출물이 있는지 확인한다."""
    outputs_dir = config.paths.resolved_outputs_dir
    checkpoints_dir = config.paths.resolved_checkpoints_dir
    candidates = (
        outputs_dir / meeting_id / "corrected.json",
        checkpoints_dir / meeting_id / "correct.json",
        checkpoints_dir / meeting_id / "merge.json",
    )
    return any(path.is_file() for path in candidates)


async def _purge_meeting_search_index(config: Any, meeting_id: str, operation: str) -> None:
    """회의 삭제/재전사 전 검색 인덱스를 정리하고 실패 시 HTTP 500으로 중단한다."""
    try:
        result = await asyncio.to_thread(purge_meeting_index, config, meeting_id)
    except IndexPurgeError as exc:
        logger.error(
            "%s 전 검색 인덱스 정리 실패: meeting_id=%s, error=%s",
            operation,
            meeting_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail=f"{operation} 전 검색 인덱스 정리에 실패했습니다: {exc}",
        ) from exc

    logger.info(
        "%s 전 검색 인덱스 정리 완료: meeting_id=%s, chroma_deleted=%s, fts_deleted=%s",
        operation,
        meeting_id,
        result.chroma_deleted,
        result.fts_deleted,
    )


def _build_meeting_item(
    job: Any,
    *,
    pipeline_state: dict[str, Any] | None = None,
    status_detail: str = "",
) -> MeetingItem:
    """Job 과 pipeline_state 를 API 응답 스키마로 변환한다."""
    skipped_steps = []
    degraded = False
    if pipeline_state is not None:
        raw_skipped = pipeline_state.get("skipped_steps", [])
        if isinstance(raw_skipped, list):
            skipped_steps = [str(step) for step in raw_skipped]
        degraded = bool(pipeline_state.get("degraded", False))

    return MeetingItem(
        id=job.id,
        meeting_id=job.meeting_id,
        audio_path=job.audio_path,
        status=job.status,
        retry_count=job.retry_count,
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
        title=getattr(job, "title", "") or "",
        degraded=degraded,
        skipped_steps=skipped_steps,
        status_detail=status_detail,
    )


async def reconcile_job_state_for_response(
    raw_queue: Any,
    config: Any,
    job: Any,
    *,
    include_pipeline_state: bool = True,
) -> tuple[Any, dict[str, Any] | None, str]:
    """완료 체크포인트와 실패 job.status 가 충돌하면 DB 상태를 복구한다."""
    from core.job_queue import JobStatus

    if job.status != JobStatus.FAILED.value:
        pipeline_state = (
            _read_pipeline_state_for_response(config, job.meeting_id)
            if include_pipeline_state
            else None
        )
        return job, pipeline_state, ""

    pipeline_state = _read_pipeline_state_for_response(config, job.meeting_id)
    if pipeline_state is None or pipeline_state.get("status") != "completed":
        return job, pipeline_state, ""

    if not _has_transcript_artifact(config, job.meeting_id):
        return job, pipeline_state, ""

    reason = (
        "pipeline_state.status=completed 와 전사 산출물이 확인되어 "
        "failed job.status 를 completed 로 복구함"
    )
    try:
        updated_job = await asyncio.to_thread(
            raw_queue.force_set_status,
            job.id,
            JobStatus.COMPLETED,
            "",
        )
        logger.warning(
            "회의 상태 불일치 자동 복구: meeting_id=%s, job_id=%s, failed → completed",
            job.meeting_id,
            job.id,
        )
        return updated_job, pipeline_state, reason
    except Exception as exc:
        logger.error(
            "회의 상태 불일치 자동 복구 실패: meeting_id=%s, job_id=%s, error=%s",
            job.meeting_id,
            job.id,
            exc,
        )
        return job, pipeline_state, "상태 불일치 감지됨: DB 복구 실패"


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """백그라운드 태스크의 미처리 예외를 로깅한다."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            f"백그라운드 태스크 실패: {task.get_name()}: {exc}",
            exc_info=exc,
        )


class MeetingItem(BaseModel):
    """회의 목록 아이템 스키마.

    Attributes:
        id: 작업 ID
        meeting_id: 회의 고유 식별자
        audio_path: 오디오 파일 경로
        status: 현재 상태
        retry_count: 재시도 횟수
        error_message: 에러 메시지
        created_at: 생성 시각
        updated_at: 수정 시각
        title: 사용자 정의 제목 (빈 문자열이면 프론트가 타임스탬프 폴백)
    """

    id: int
    meeting_id: str
    audio_path: str
    status: str
    retry_count: int = 0
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""
    title: str = ""
    degraded: bool = False
    skipped_steps: list[str] = Field(default_factory=list)
    status_detail: str = ""


class TranscriptUtteranceItem(BaseModel):
    """전사문 개별 발화 스키마.

    Attributes:
        text: 보정된 발화 텍스트
        original_text: 원본 STT 텍스트
        speaker: 화자 라벨 (예: "SPEAKER_00")
        start: 발화 시작 시간 (초)
        end: 발화 종료 시간 (초)
        was_corrected: LLM 보정 적용 여부
    """

    text: str
    original_text: str = ""
    speaker: str = "UNKNOWN"
    start: float = 0.0
    end: float = 0.0
    was_corrected: bool = False


class TranscriptResponse(BaseModel):
    """전사문 응답 스키마.

    Attributes:
        utterances: 보정된 발화 목록
        meeting_id: 회의 고유 식별자
        num_speakers: 감지된 화자 수
        speakers: 화자 라벨 목록
        total_utterances: 전체 발화 수
    """

    utterances: list[TranscriptUtteranceItem] = Field(default_factory=list)
    meeting_id: str
    num_speakers: int = 0
    speakers: list[str] = Field(default_factory=list)
    total_utterances: int = 0


class SummaryResponse(BaseModel):
    """회의록 요약 응답 스키마.

    Attributes:
        markdown: 마크다운 형식의 회의록
        meeting_id: 회의 고유 식별자
        num_speakers: 화자 수
        speakers: 화자 라벨 목록
        num_utterances: 발화 수
        created_at: 회의록 생성 시각
    """

    markdown: str
    meeting_id: str
    num_speakers: int = 0
    speakers: list[str] = Field(default_factory=list)
    num_utterances: int = 0
    created_at: str = ""


@router.get("/meetings/{meeting_id}", response_model=MeetingItem)
async def get_meeting(request: Request, meeting_id: str) -> MeetingItem:
    """특정 회의의 상세 정보를 반환한다.

    meeting_id로 작업을 조회하여 상세 정보를 반환한다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 회의 고유 식별자

    Returns:
        MeetingItem: 회의 상세 정보

    Raises:
        HTTPException: 회의를 찾을 수 없을 때 (404)
    """
    queue = _get_job_queue(request)

    try:
        # meeting_id로 작업 조회 (동기 함수를 비동기로 래핑)
        import asyncio

        job = await asyncio.to_thread(
            queue.queue.get_job_by_meeting_id,
            meeting_id,
        )

        if job is None:
            raise HTTPException(
                status_code=404,
                detail=f"회의를 찾을 수 없습니다: {meeting_id}",
            )

        config = _get_config(request)
        raw_queue = getattr(queue, "queue", queue)
        job, pipeline_state, status_detail = await reconcile_job_state_for_response(
            raw_queue,
            config,
            job,
        )
        return _build_meeting_item(
            job,
            pipeline_state=pipeline_state,
            status_detail=status_detail,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"회의 상세 조회 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"회의 상세 조회 중 오류가 발생했습니다: {e}",
        ) from e


class MeetingPatchRequest(BaseModel):
    """PATCH /api/meetings/{meeting_id} 요청 본문 (부분 업데이트)."""

    title: str | None = Field(
        default=None,
        max_length=200,
        description="사용자 정의 제목 (빈 문자열이면 자동 타임스탬프 복귀)",
    )


@router.patch("/meetings/{meeting_id}", response_model=MeetingItem)
async def patch_meeting(
    request: Request,
    meeting_id: str,
    body: MeetingPatchRequest,
) -> MeetingItem:
    """회의 메타데이터를 부분 업데이트한다. 현재는 title 만 지원.

    빈 문자열을 보내면 title 이 초기화되어 프론트엔드가 자동 타임스탬프 제목으로
    돌아간다. 다른 필드(status, audio_path 등)는 이 엔드포인트로 수정할 수 없다.

    Raises:
        HTTPException 400: 유효하지 않은 meeting_id 또는 title 길이 초과
        HTTPException 404: 회의 없음
        HTTPException 503: JobQueue 미초기화
    """
    _validate_meeting_id(meeting_id)
    queue = _get_job_queue(request)

    try:
        # 기존 라우트들과 동일 패턴: queue.queue 로 raw JobQueue 접근
        raw_queue = getattr(queue, "queue", queue)
        job = await asyncio.to_thread(raw_queue.get_job_by_meeting_id, meeting_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"회의를 찾을 수 없습니다: {meeting_id}")

        if body.title is not None:
            try:
                job = await asyncio.to_thread(raw_queue.update_title, meeting_id, body.title)
            except Exception as exc:  # JobQueueError 또는 기타 검증 오류
                from core.job_queue import JobQueueError as _JQErr

                if isinstance(exc, _JQErr):
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                raise

        return MeetingItem(
            id=job.id,
            meeting_id=job.meeting_id,
            audio_path=job.audio_path,
            status=job.status,
            retry_count=job.retry_count,
            error_message=job.error_message,
            created_at=job.created_at,
            updated_at=job.updated_at,
            title=getattr(job, "title", "") or "",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"회의 메타데이터 업데이트 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"회의 메타데이터 업데이트 중 오류가 발생했습니다: {e}",
        ) from e


@router.post("/meetings/{meeting_id}/retry")
async def retry_meeting(request: Request, meeting_id: str) -> MeetingItem:
    """실패한 회의를 재시도한다.

    meeting_id로 작업을 찾아 상태를 queued로 되돌리고 파이프라인을 재실행한다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 재시도할 회의 고유 식별자

    Returns:
        MeetingItem: 업데이트된 회의 정보

    Raises:
        HTTPException: 회의를 찾을 수 없을 때 (404), 재시도 불가 시 (409)
    """
    from core.job_queue import InvalidTransitionError, JobNotFoundError, MaxRetriesExceededError

    queue = _get_job_queue(request)

    try:
        import asyncio

        # meeting_id로 작업 조회
        job = await asyncio.to_thread(
            queue.queue.get_job_by_meeting_id,
            meeting_id,
        )
        if job is None:
            raise HTTPException(
                status_code=404,
                detail=f"회의를 찾을 수 없습니다: {meeting_id}",
            )

        config = _get_config(request)
        raw_queue = getattr(queue, "queue", queue)
        job, pipeline_state, status_detail = await reconcile_job_state_for_response(
            raw_queue,
            config,
            job,
        )
        if status_detail:
            if job.status == "completed":
                return _build_meeting_item(
                    job,
                    pipeline_state=pipeline_state,
                    status_detail=status_detail,
                )
            raise HTTPException(
                status_code=409,
                detail=(
                    "회의 산출물은 완료 상태로 보이지만 작업 큐 상태 복구에 실패했습니다. "
                    f"{status_detail}"
                ),
            )

        # 재시도 실행 (job_id 기반)
        updated_job = await asyncio.to_thread(queue.queue.retry_job, job.id)

        # 이전 취소 요청이 set 에 남아있을 수 있으니 정리 (stale 방어)
        job_processor = getattr(request.app.state, "job_processor", None)
        if job_processor is not None:
            job_processor._cancellation_requests.discard(meeting_id)

        logger.info(f"회의 재시도 요청: {meeting_id} (job_id={job.id})")

        return _build_meeting_item(updated_job, pipeline_state=pipeline_state)
    except HTTPException:
        raise
    except (InvalidTransitionError, MaxRetriesExceededError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"회의 재시도 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"회의 재시도 중 오류가 발생했습니다: {e}",
        ) from e


@router.post("/meetings/{meeting_id}/transcribe")
async def transcribe_meeting(
    request: Request,
    meeting_id: str,
    force: bool = False,
) -> MeetingItem:
    """녹음 완료된 회의의 전사를 시작한다.

    recorded 상태의 작업을 queued로 전환하여 전사 파이프라인을 트리거한다.
    이슈 J 대응: ``force=true`` 를 전달하면 ``failed`` 상태에서도 재시도를 시작한다.
    이때 기존 에러 메시지는 지우고 retry_count 는 그대로 유지한다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 전사할 회의 고유 식별자
        force: True이면 failed 상태도 강제로 재시도한다 (쿼리파라미터)

    Returns:
        MeetingItem: 업데이트된 회의 정보

    Raises:
        HTTPException: 회의를 찾을 수 없을 때 (404), 상태 전이 불가 시 (409)
    """
    from core.job_queue import InvalidTransitionError, JobNotFoundError, JobStatus

    queue = _get_job_queue(request)

    try:
        import asyncio

        job = await asyncio.to_thread(
            queue.queue.get_job_by_meeting_id,
            meeting_id,
        )
        if job is None:
            raise HTTPException(
                status_code=404,
                detail=f"회의를 찾을 수 없습니다: {meeting_id}",
            )

        # 이슈 J: failed 상태에서도 force=true 이면 재시도 허용
        if job.status == JobStatus.FAILED.value and force:
            logger.info(
                f"failed 상태 강제 재시도: {meeting_id} (job_id={job.id}, "
                f"retry_count={job.retry_count})"
            )
            # failed → recorded 로 되돌린 뒤 아래 공통 경로에서 queued 로 전이
            job = await asyncio.to_thread(
                queue.queue.force_set_status,
                job.id,
                JobStatus.RECORDED,
                "",
            )

        if job.status != JobStatus.RECORDED.value:
            detail = f"전사를 시작할 수 없는 상태입니다: {job.status} (recorded 상태만 가능)"
            if job.status == JobStatus.FAILED.value:
                # 힌트: force=true 로 재시도 가능
                detail += ". 실패한 회의를 재시도하려면 ?force=true 를 붙여 요청하세요."
            raise HTTPException(status_code=409, detail=detail)

        updated_job = await asyncio.to_thread(
            queue.queue.update_status,
            job.id,
            JobStatus.QUEUED,
        )

        # 이전 취소 요청이 set 에 남아있을 수 있으니 정리 (stale 방어)
        job_processor = getattr(request.app.state, "job_processor", None)
        if job_processor is not None:
            job_processor._cancellation_requests.discard(meeting_id)

        logger.info(f"전사 시작 요청: {meeting_id} (job_id={job.id})")

        return MeetingItem(
            id=updated_job.id,
            meeting_id=updated_job.meeting_id,
            audio_path=updated_job.audio_path,
            status=updated_job.status,
            retry_count=updated_job.retry_count,
            error_message=updated_job.error_message,
            created_at=updated_job.created_at,
            updated_at=updated_job.updated_at,
            title=getattr(updated_job, "title", "") or "",
        )
    except HTTPException:
        raise
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"전사 시작 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"전사 시작 중 오류가 발생했습니다: {e}",
        ) from e


@router.post("/meetings/{meeting_id}/cancel")
async def cancel_meeting(request: Request, meeting_id: str) -> MeetingItem:
    """진행 중(또는 대기 중)인 회의 전사를 취소하고 recorded 로 되돌린다.

    동작:
        - status == queued: 아직 워커가 잡지 않았으므로 즉시 force_set_status 로 recorded.
        - status in (transcribing, diarizing, merging, embedding):
          JobProcessor.request_cancellation() 으로 취소 요청 등록.
          현재 실행 중인 단계가 끝난 뒤 다음 단계 경계에서 CancelledError 가 발생하여
          orchestrator 가 status 를 recorded 로 되돌리고 brodcast.
        - 그 외 상태: 409 (취소 대상 아님)

    Args:
        request: FastAPI Request
        meeting_id: 취소할 회의 ID

    Returns:
        업데이트된 MeetingItem (queued 였다면 즉시 recorded, 진행 중이었다면
        아직 recorded 가 아닐 수 있음 — 프론트가 폴링/브로드캐스트로 갱신)

    Raises:
        HTTPException: 회의 없음(404), 취소 대상 상태 아님(409)
    """
    from core.job_queue import JobNotFoundError, JobStatus

    queue = _get_job_queue(request)

    in_progress_states = {
        JobStatus.QUEUED.value,
        JobStatus.TRANSCRIBING.value,
        JobStatus.DIARIZING.value,
        JobStatus.MERGING.value,
        JobStatus.EMBEDDING.value,
    }

    try:
        job = await asyncio.to_thread(queue.queue.get_job_by_meeting_id, meeting_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail=f"회의를 찾을 수 없습니다: {meeting_id}",
            )

        if job.status not in in_progress_states:
            raise HTTPException(
                status_code=409,
                detail=f"취소할 수 있는 상태가 아닙니다: {job.status}",
            )

        # queued: 즉시 recorded 로 강제 전환 (아직 워커가 잡지 않음)
        if job.status == JobStatus.QUEUED.value:
            updated_job = await asyncio.to_thread(
                queue.queue.force_set_status,
                job.id,
                JobStatus.RECORDED,
                "사용자가 취소함 (대기 중)",
            )
            # 혹시 이전에 in-progress 취소 요청이 등록되어 있을 수 있으니 정리
            job_processor = getattr(request.app.state, "job_processor", None)
            if job_processor is not None:
                job_processor._cancellation_requests.discard(meeting_id)
        else:
            # 실행 중: JobProcessor 에 취소 요청 등록.
            # 단계 경계에서 orchestrator 가 잡고 recorded 로 되돌린다.
            job_processor = getattr(request.app.state, "job_processor", None)
            if job_processor is None:
                raise HTTPException(
                    status_code=503,
                    detail="JobProcessor 가 초기화되지 않아 취소할 수 없습니다.",
                )
            job_processor.request_cancellation(meeting_id)
            # 현재 시점의 job 그대로 반환 — 프론트는 폴링/WebSocket 으로 갱신
            updated_job = job

        logger.info(f"취소 요청 처리: {meeting_id} (이전 status={job.status})")

        return MeetingItem(
            id=updated_job.id,
            meeting_id=updated_job.meeting_id,
            audio_path=updated_job.audio_path,
            status=updated_job.status,
            retry_count=updated_job.retry_count,
            error_message=updated_job.error_message,
            created_at=updated_job.created_at,
            updated_at=updated_job.updated_at,
            title=getattr(updated_job, "title", "") or "",
        )
    except HTTPException:
        raise
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"취소 처리 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"취소 처리 중 오류가 발생했습니다: {e}",
        ) from e


@router.post("/meetings/{meeting_id}/re-transcribe")
async def re_transcribe_meeting(request: Request, meeting_id: str) -> MeetingItem:
    """기존 전사 결과를 폐기하고 처음부터 다시 전사한다.

    completed/failed 상태의 작업을 대상으로:
        1. ChromaDB/FTS5 의 stale 청크 삭제
        2. 체크포인트 디렉토리 전체 삭제 (pipeline_state.json 포함)
        3. 출력 디렉토리의 corrected.json/summary.md 삭제 (오디오는 보존)
        4. job 상태를 queued 로 강제 전환 (retry_count 0 으로 리셋)

    Args:
        request: FastAPI Request 객체
        meeting_id: 재전사할 회의 고유 식별자

    Returns:
        MeetingItem: 업데이트된 회의 정보 (status=queued)

    Raises:
        HTTPException: 회의를 찾을 수 없을 때 (404), 재전사 불가 상태 (409)
    """
    import shutil

    from core.job_queue import InvalidTransitionError, JobNotFoundError

    queue = _get_job_queue(request)
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="설정이 초기화되지 않았습니다.")

    try:
        job = await asyncio.to_thread(queue.queue.get_job_by_meeting_id, meeting_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail=f"회의를 찾을 수 없습니다: {meeting_id}",
            )

        # 1) 검색 인덱스 삭제. 실패하면 산출물/DB 상태를 건드리지 않는다.
        await _purge_meeting_search_index(config, meeting_id, "재전사")

        # 2) 체크포인트 디렉토리 삭제
        checkpoints_dir = config.paths.resolved_checkpoints_dir / meeting_id
        if checkpoints_dir.exists():
            await asyncio.to_thread(shutil.rmtree, checkpoints_dir)
            logger.info(f"재전사: 체크포인트 삭제 — {checkpoints_dir}")

        # 3) 출력 파일 삭제 (오디오/녹음본은 보존)
        outputs_meeting_dir = config.paths.resolved_outputs_dir / meeting_id
        if outputs_meeting_dir.exists():
            for fname in ("corrected.json", "summary.md"):
                fpath = outputs_meeting_dir / fname
                if fpath.exists():
                    try:
                        await asyncio.to_thread(fpath.unlink)
                    except OSError as exc:
                        logger.warning(f"재전사: {fname} 삭제 실패: {exc}")

        # 4) job 상태 강제 리셋
        updated_job = await asyncio.to_thread(queue.queue.reset_for_retranscribe, job.id)

        # 이전 취소 요청이 set 에 남아있을 수 있으니 정리 (stale 방어)
        job_processor = getattr(request.app.state, "job_processor", None)
        if job_processor is not None:
            job_processor._cancellation_requests.discard(meeting_id)

        logger.info(f"재전사 요청: {meeting_id} (job_id={job.id})")

        return MeetingItem(
            id=updated_job.id,
            meeting_id=updated_job.meeting_id,
            audio_path=updated_job.audio_path,
            status=updated_job.status,
            retry_count=updated_job.retry_count,
            error_message=updated_job.error_message,
            created_at=updated_job.created_at,
            updated_at=updated_job.updated_at,
            title=getattr(updated_job, "title", "") or "",
        )
    except HTTPException:
        raise
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"재전사 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"재전사 중 오류가 발생했습니다: {e}",
        ) from e


@router.get("/meetings/{meeting_id}/pipeline-state")
async def get_pipeline_state(request: Request, meeting_id: str) -> dict[str, Any]:
    """파이프라인 실행 상태 (단계별 소요시간 포함) 를 반환한다.

    `~/.meeting-transcriber/checkpoints/{meeting_id}/pipeline_state.json` 을 그대로 반환한다.
    프론트엔드 로그 탭에서 단계별 elapsed_seconds 와 총 소요시간을 표시한다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 회의 고유 식별자

    Returns:
        PipelineState 직렬화 dict + total_elapsed_seconds (편의 필드)

    Notes:
        오래된 회의나 수동 시드 데이터에는 pipeline_state.json 이 없을 수 있다.
        이 경우 프론트엔드 콘솔에 404 노이즈를 남기지 않도록 빈 상태를 반환한다.
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="설정이 초기화되지 않았습니다.")

    state_path = config.paths.resolved_checkpoints_dir / meeting_id / "pipeline_state.json"
    if not state_path.exists():
        return {
            "status": "missing",
            "step_results": [],
            "skipped_steps": [],
            "warnings": [],
            "total_elapsed_seconds": 0.0,
        }

    try:
        data = cast(
            dict[str, Any],
            await asyncio.to_thread(lambda: json.loads(state_path.read_text(encoding="utf-8"))),
        )
    except (OSError, json.JSONDecodeError) as e:
        logger.exception(f"pipeline_state.json 읽기 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"파이프라인 상태를 읽을 수 없습니다: {e}",
        ) from e

    # 편의: 총 소요시간 계산 (step_results 의 elapsed_seconds 합산)
    step_results = data.get("step_results", []) or []
    total_elapsed = sum(float(step.get("elapsed_seconds") or 0.0) for step in step_results)
    data["total_elapsed_seconds"] = round(total_elapsed, 2)
    return data


# === 회의 음성 재생 ===


# 재생 가능한 오디오 확장자 (HTML <audio> 호환)
_PLAYABLE_AUDIO_EXTS: tuple[str, ...] = (".wav", ".mp3", ".m4a", ".flac", ".ogg")

# 확장자 → MIME 매핑 (표준 우선)
_AUDIO_MIME_BY_EXT: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
}


def _find_meeting_audio_path(config: Any, meeting_id: str) -> Path | None:
    """회의의 재생 가능한 오디오 파일을 찾는다.

    탐색 우선순위:
        1. checkpoints/{id}/pipeline_state.json 의 ``wav_path`` (16kHz 변환본 — 회의록 화자분리·STT 의 정답 시간축과 동일)
        2. checkpoints/{id}/pipeline_state.json 의 ``audio_path`` (원본)
        3. outputs/{id}/ 디렉토리 내 ``*_16k.wav`` 또는 임의 ``*.wav`` (폴백)

    Args:
        config: AppConfig
        meeting_id: 회의 고유 식별자 (이미 검증된 값)

    Returns:
        실제 존재하는 오디오 파일 Path, 못 찾으면 None.
    """
    state_path = config.paths.resolved_checkpoints_dir / meeting_id / "pipeline_state.json"
    if state_path.is_file():
        try:
            with open(state_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}

        # wav_path 가 회의록 시간축과 일치하므로 우선 사용
        for key in ("wav_path", "audio_path"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, str) and value:
                candidate = Path(value)
                if candidate.is_file() and candidate.suffix.lower() in _PLAYABLE_AUDIO_EXTS:
                    return candidate

    # 폴백: outputs/{id}/ 디렉토리 글롭
    outputs_root = config.paths.resolved_outputs_dir / meeting_id
    if outputs_root.is_dir():
        # 16kHz 변환본을 우선, 없으면 임의 wav
        for pattern in ("*_16k.wav", "*.wav"):
            matches = sorted(outputs_root.glob(pattern))
            if matches:
                return cast(Path, matches[0])

    return None


def _parse_range_header(range_header: str, file_size: int) -> tuple[int, int] | None:
    """HTTP Range 헤더를 파싱한다 (단일 range 만 지원).

    지원 형식:
        - ``bytes=START-END`` — 명시적 범위
        - ``bytes=START-`` — START 부터 끝까지
        - ``bytes=-N`` — 마지막 N 바이트 (suffix range)

    multipart range (``bytes=0-100,200-300``) 는 복잡도 대비 활용도가 낮아 미지원.

    Args:
        range_header: Range 헤더 원본 문자열
        file_size: 대상 파일 크기 (바이트)

    Returns:
        (start, end) 튜플 — 둘 다 inclusive. 형식 불량·범위 초과 시 None.
    """
    if not range_header.lower().startswith("bytes="):
        return None

    spec = range_header[len("bytes=") :].strip()
    if "," in spec:
        # multipart range 미지원
        return None

    parts = spec.split("-", 1)
    if len(parts) != 2:
        return None

    start_s, end_s = parts[0].strip(), parts[1].strip()
    try:
        if start_s == "":
            # suffix range: 마지막 N 바이트
            if end_s == "":
                return None
            n = int(end_s)
            if n <= 0:
                return None
            start = max(0, file_size - n)
            end = file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s != "" else file_size - 1
    except ValueError:
        return None

    if start < 0 or start >= file_size or end < start:
        return None

    end = min(end, file_size - 1)
    return (start, end)


@router.get("/meetings/{meeting_id}/audio")
async def get_meeting_audio(request: Request, meeting_id: str) -> Any:
    """회의의 원본 음성을 재생용으로 스트리밍한다 (HTTP Range 지원).

    프론트엔드 ViewerView 에서 utterance 별 ▶ 버튼이 클릭되면
    ``<audio>`` 요소가 ``currentTime = u.start`` 으로 seek 한 뒤 play 한다.
    Range 헤더 (``Accept-Ranges: bytes``) 를 응답하므로 브라우저가 임의 시점으로
    바로 점프할 수 있다.

    Args:
        request: FastAPI Request
        meeting_id: 회의 고유 식별자

    Returns:
        FileResponse (전체 파일, 200) 또는 StreamingResponse (Range, 206)

    Raises:
        HTTPException: 잘못된 ID 형식 (400), 음성 파일 없음 (404), 설정 미초기화 (503)
    """
    from fastapi.responses import FileResponse, Response, StreamingResponse

    _validate_meeting_id(meeting_id)
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")

    audio_path = await asyncio.to_thread(_find_meeting_audio_path, config, meeting_id)
    if audio_path is None:
        raise HTTPException(
            status_code=404,
            detail=f"재생 가능한 음성 파일이 없습니다: {meeting_id} "
            "(라이프사이클 정책에 따라 30~90일 후 삭제될 수 있습니다)",
        )

    file_size = audio_path.stat().st_size
    media_type = _AUDIO_MIME_BY_EXT.get(audio_path.suffix.lower(), "application/octet-stream")

    # Range 요청 처리
    range_header = request.headers.get("range") or request.headers.get("Range")
    if range_header:
        parsed = _parse_range_header(range_header, file_size)
        if parsed is None:
            # 416 Range Not Satisfiable — 클라이언트가 잘못된 범위를 요청
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        start, end = parsed
        length = end - start + 1

        def _iter_range():
            """파일을 64KB 청크로 부분 스트리밍한다."""
            with open(audio_path, "rb") as f:
                f.seek(start)
                remaining = length
                chunk_size = 64 * 1024
                while remaining > 0:
                    chunk = f.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            _iter_range(),
            status_code=206,
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                # 같은 파일에 대한 반복 seek 시 브라우저 캐시 활용
                "Cache-Control": "private, max-age=3600",
            },
        )

    # 전체 파일 응답 (Range 헤더 없는 첫 요청 또는 단순 다운로드)
    return FileResponse(
        path=audio_path,
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.delete("/meetings/{meeting_id}")
async def delete_meeting(request: Request, meeting_id: str) -> dict[str, str]:
    """회의를 삭제한다 (검색 인덱스 + DB 레코드 + 오디오 파일 → quarantine).

    Phase 1-7: 오디오 파일이 watcher에 의해 재감지되는 문제를 차단하기 위해
    검색 인덱스/DB 삭제와 함께 원본 오디오 파일을 quarantine 디렉토리로 이동한다.
    파일 이동 실패는 best-effort(경고 로그만) 처리하여 DB 삭제 자체는
    항상 성공시킨다. 파일이 이미 없는 경우(사용자가 직접 삭제했거나,
    예전에 격리되었거나)도 마찬가지로 DB 삭제는 성공 처리한다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 삭제할 회의 고유 식별자

    Returns:
        삭제 완료 메시지

    Raises:
        HTTPException: 회의를 찾을 수 없을 때 (404) 또는 DB 삭제 실패 시 (500)
    """
    import asyncio

    from core.job_queue import JobNotFoundError
    from core.quarantine import QuarantineError, move_to_quarantine

    queue = _get_job_queue(request)
    config = _get_config(request)

    try:
        # meeting_id로 작업 조회
        job = await asyncio.to_thread(
            queue.queue.get_job_by_meeting_id,
            meeting_id,
        )
        if job is None:
            raise HTTPException(
                status_code=404,
                detail=f"회의를 찾을 수 없습니다: {meeting_id}",
            )

        # 삭제 전 audio_path 확보 (DB 삭제 이후에도 파일을 찾을 수 있도록 먼저 스냅샷)
        audio_path_str = getattr(job, "audio_path", None)

        # 검색 인덱스 삭제. 실패하면 DB 레코드와 오디오 파일을 보존한다.
        await _purge_meeting_search_index(config, meeting_id, "삭제")

        # DB 삭제 (인덱스 정리 후 — 파일 이동 실패해도 DB는 정리)
        await asyncio.to_thread(queue.queue.delete_job, job.id)
        logger.info(f"회의 DB 삭제: {meeting_id} (job_id={job.id})")

        # 오디오 파일 quarantine 이동 (best-effort)
        # watcher 재감지 루프를 끊기 위해 DB 삭제 직후에 수행한다.
        if audio_path_str:
            audio_path = Path(audio_path_str)
            if audio_path.exists():
                try:
                    quarantine_dir = config.paths.resolved_audio_quarantine_dir
                    new_path = await asyncio.to_thread(
                        move_to_quarantine,
                        audio_path,
                        quarantine_dir,
                        reason=f"사용자 삭제: meeting_id={meeting_id}",
                    )
                    logger.info(f"오디오 파일 격리 완료: {audio_path} → {new_path}")
                except QuarantineError as e:
                    # 파일 이동 실패해도 DB 삭제는 이미 성공 — 경고만 남기고 진행
                    logger.warning(f"오디오 파일 격리 실패 (DB 삭제는 완료): {e}")
            else:
                logger.debug(f"오디오 파일이 이미 존재하지 않음: {audio_path}")

        return {"message": f"회의가 삭제되었습니다: {meeting_id}"}
    except HTTPException:
        raise
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"회의 삭제 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"회의 삭제 중 오류가 발생했습니다: {e}",
        ) from e


@router.get(
    "/meetings/{meeting_id}/transcript",
    response_model=TranscriptResponse,
)
async def get_transcript(
    request: Request,
    meeting_id: str,
) -> TranscriptResponse:
    """특정 회의의 전사문(보정된 발화 목록)을 반환한다.

    다음 순서로 폴백하여 데이터를 찾는다:
      1. outputs/{meeting_id}/corrected.json (LLM 보정 완료)
      2. checkpoints/{meeting_id}/correct.json (보정 체크포인트)
      3. checkpoints/{meeting_id}/merge.json (병합 결과, 미보정)

    Args:
        request: FastAPI Request 객체
        meeting_id: 회의 고유 식별자

    Returns:
        TranscriptResponse: 전사문 데이터

    Raises:
        HTTPException: 유효하지 않은 ID(400), 파일 미존재(404), 서버 에러(500)
    """
    _validate_meeting_id(meeting_id)
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")

    outputs_dir = config.paths.resolved_outputs_dir
    checkpoints_dir = config.paths.resolved_checkpoints_dir

    # 폴백 순서: corrected.json → correct.json → merge.json
    candidates = [
        outputs_dir / meeting_id / "corrected.json",
        checkpoints_dir / meeting_id / "correct.json",
        checkpoints_dir / meeting_id / "merge.json",
    ]

    transcript_path: Path | None = None
    for candidate in candidates:
        if candidate.is_file():
            transcript_path = candidate
            break

    if transcript_path is None:
        raise HTTPException(
            status_code=404,
            detail=f"전사문을 찾을 수 없습니다: {meeting_id}",
        )

    try:
        import asyncio

        # PERF: mtime 기반 JSON 캐시 사용 (매 요청마다 파싱하지 않음)
        data = await asyncio.to_thread(_json_cache.get, transcript_path)

        # merge.json은 original_text/was_corrected 필드가 없으므로 폴백 처리
        is_merge_fallback = "merge" in transcript_path.name

        utterances = [
            TranscriptUtteranceItem(
                text=u.get("text", ""),
                original_text=u.get("original_text", u.get("text", "")),
                speaker=u.get("speaker", "UNKNOWN"),
                start=u.get("start", 0.0),
                end=u.get("end", 0.0),
                was_corrected=u.get("was_corrected", False) if not is_merge_fallback else False,
            )
            for u in data.get("utterances", [])
        ]

        # 화자 목록 추출 (UNKNOWN 제외, 순서 보존)
        seen: set[str] = set()
        speakers: list[str] = []
        for u in utterances:
            if u.speaker != "UNKNOWN" and u.speaker not in seen:
                seen.add(u.speaker)
                speakers.append(u.speaker)

        return TranscriptResponse(
            utterances=utterances,
            meeting_id=meeting_id,
            num_speakers=data.get("num_speakers", len(speakers)),
            speakers=speakers,
            total_utterances=len(utterances),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"전사문 조회 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"전사문 조회 중 오류가 발생했습니다: {e}",
        ) from e


@router.get(
    "/meetings/{meeting_id}/summary",
    response_model=SummaryResponse,
)
async def get_summary(
    request: Request,
    meeting_id: str,
) -> SummaryResponse:
    """특정 회의의 AI 요약(회의록)을 반환한다.

    outputs/{meeting_id}/summary.json 메타데이터와
    summary.md 마크다운 파일에서 회의록을 읽어 반환한다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 회의 고유 식별자

    Returns:
        SummaryResponse: 회의록 데이터

    Raises:
        HTTPException: 유효하지 않은 ID(400), 파일 미존재(404), 서버 에러(500)
    """
    _validate_meeting_id(meeting_id)
    outputs_dir = _get_outputs_dir(request)
    meeting_dir = outputs_dir / meeting_id

    # 폴백 순서: summary.md → meeting_minutes.md → summary.json → checkpoints/summarize.json
    summary_md_path = meeting_dir / "summary.md"
    minutes_md_path = meeting_dir / "meeting_minutes.md"
    summary_json_path = meeting_dir / "summary.json"
    # 체크포인트 폴백
    config = getattr(request.app.state, "config", None)
    checkpoints_dir = (
        config.paths.resolved_checkpoints_dir
        if config
        else meeting_dir.parent.parent / "checkpoints"
    )
    checkpoint_path = checkpoints_dir / meeting_id / "summarize.json"

    if (
        not summary_md_path.is_file()
        and not minutes_md_path.is_file()
        and not summary_json_path.is_file()
        and not checkpoint_path.is_file()
    ):
        raise HTTPException(
            status_code=404,
            detail=f"회의록을 찾을 수 없습니다: {meeting_id}",
        )

    try:
        import asyncio

        markdown = ""
        meta: dict = {}

        # 마크다운 파일 읽기 (폴백 순서: summary.md → meeting_minutes.md)
        md_file = None
        if summary_md_path.is_file():
            md_file = summary_md_path
        elif minutes_md_path.is_file():
            md_file = minutes_md_path

        if md_file:

            def _read_md() -> str:
                return md_file.read_text(encoding="utf-8")

            markdown = await asyncio.to_thread(_read_md)

        # PERF: mtime 기반 JSON 캐시 사용
        if summary_json_path.is_file():
            meta = await asyncio.to_thread(_json_cache.get, summary_json_path)
            if not markdown and meta.get("markdown"):
                markdown = meta["markdown"]

        # 체크포인트 폴백 (outputs에 없을 때)
        if not markdown and checkpoint_path.is_file():
            cp_data = await asyncio.to_thread(_json_cache.get, checkpoint_path)
            if cp_data.get("markdown"):
                markdown = cp_data["markdown"]
                meta = cp_data

        return SummaryResponse(
            markdown=markdown,
            meeting_id=meeting_id,
            num_speakers=meta.get("num_speakers", 0),
            speakers=meta.get("speakers", []),
            num_utterances=meta.get("num_utterances", 0),
            created_at=meta.get("created_at", ""),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"회의록 조회 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"회의록 조회 중 오류가 발생했습니다: {e}",
        ) from e


# ===========================================================================
# 회의록 / 전사문 편집 엔드포인트
# ===========================================================================
# 사용자가 AI 생성 결과물을 수동으로 수정하거나, 자주 틀리는 전사 패턴을
# 한 번에 치환하면서 용어집에도 자동 등록할 수 있도록 지원한다.
#
# 저장 원칙:
#   - 기존 파일(meeting_minutes.md, correct.json)을 직접 덮어쓴다.
#   - 원자적 쓰기: {파일}.tmp 에 쓰고 os.replace 로 교체
#   - 직전 버전은 {파일}.bak 으로 백업 (복구용)
#   - force 재생성 시에도 .bak 로 보존되어 수동 편집을 복구할 수 있다.
# ===========================================================================


# === 요약 편집 ===


class SummaryUpdateRequest(BaseModel):
    """PUT /api/meetings/{meeting_id}/summary 요청."""

    markdown: str = Field(
        ...,
        min_length=1,
        max_length=200000,
        description="수정된 회의록 마크다운 본문",
    )


@router.put(
    "/meetings/{meeting_id}/summary",
    response_model=SummaryResponse,
)
async def update_summary(
    request: Request,
    meeting_id: str,
    body: SummaryUpdateRequest,
) -> SummaryResponse:
    """사용자가 편집한 회의록(마크다운) 본문을 저장한다.

    기존 `meeting_minutes.md` (없으면 `summary.md`) 파일을 덮어쓰고,
    직전 버전을 `.bak` 로 백업한다. 이후 `GET /summary` 는 수정본을 반환한다.

    주의: `POST /summarize?force=true` 로 AI 재생성 시 현재 수정본은 .bak 로만
    남고 다시 AI 출력으로 대체된다. 프론트엔드에서 재생성 전 경고를 표시하세요.

    Raises:
        HTTPException 400: 유효하지 않은 meeting_id
        HTTPException 404: 회의 디렉토리 없음
        HTTPException 500: 파일 쓰기 실패
    """
    _validate_meeting_id(meeting_id)
    outputs_dir = _get_outputs_dir(request)
    meeting_dir = outputs_dir / meeting_id

    if not meeting_dir.exists():
        raise HTTPException(
            status_code=404,
            detail=f"회의 출력 폴더를 찾을 수 없습니다: {meeting_id}",
        )

    # 기존 파일 결정: meeting_minutes.md 우선, 없으면 summary.md
    minutes_md = meeting_dir / "meeting_minutes.md"
    summary_md = meeting_dir / "summary.md"
    if minutes_md.exists():
        target = minutes_md
    elif summary_md.exists():
        target = summary_md
    else:
        # 둘 다 없으면 meeting_minutes.md 로 새로 생성
        target = minutes_md

    try:
        await asyncio.to_thread(_atomic_write_text, target, body.markdown)
        # JSON 캐시 무효화 (다음 GET 에서 수정본 반영되도록)
        _json_cache.invalidate(target)
    except OSError as exc:
        logger.exception(f"회의록 저장 실패: {exc}")
        raise HTTPException(
            status_code=500,
            detail=f"회의록 저장 중 오류가 발생했습니다: {exc}",
        ) from exc

    logger.info(
        "회의록 수동 편집 저장: meeting_id=%s, path=%s, length=%d",
        meeting_id,
        target.name,
        len(body.markdown),
    )
    return SummaryResponse(
        markdown=body.markdown,
        meeting_id=meeting_id,
        num_speakers=0,
        speakers=[],
        num_utterances=0,
        created_at="",
    )


# === 전사문 편집 ===


class TranscriptUtterancePatch(BaseModel):
    """전사문 수정 시 단일 발화 스키마.

    기존 구조와 호환: speaker, start, end, text 등 필수 필드.
    """

    text: str = Field(..., max_length=10000)
    original_text: str = ""
    speaker: str = "UNKNOWN"
    start: float = 0.0
    end: float = 0.0
    was_corrected: bool = False


class TranscriptUpdateRequest(BaseModel):
    """PUT /api/meetings/{meeting_id}/transcript 요청."""

    utterances: list[TranscriptUtterancePatch] = Field(..., min_length=1)


class TranscriptReplaceRequest(BaseModel):
    """POST /api/meetings/{meeting_id}/transcript/replace 요청."""

    find: str = Field(..., min_length=1, max_length=500, description="치환 대상 패턴 (정확 매칭)")
    replace: str = Field(..., min_length=1, max_length=500, description="치환 후 문자열")
    add_to_vocabulary: bool = Field(
        default=False,
        description="True면 자동으로 용어집에 등록 (replace=term, find=alias)",
    )


class TranscriptReplaceResponse(BaseModel):
    """POST /api/meetings/{meeting_id}/transcript/replace 응답."""

    changes: int = 0
    updated_utterances: int = 0
    vocabulary_action: str | None = None
    vocabulary_term_id: str | None = None


def _find_transcript_file(config: Any, meeting_id: str) -> tuple[Path | None, str]:
    """전사 편집 대상 파일을 찾는다.

    편집 시에는 readonly 폴백(merge.json)을 사용하지 않고,
    correct.json(우선) 또는 corrected.json 만 대상으로 한다.

    Returns:
        (파일 경로, 'output'|'checkpoint') 튜플, 없으면 (None, "")
    """
    outputs_dir = config.paths.resolved_outputs_dir
    checkpoints_dir = config.paths.resolved_checkpoints_dir

    # 1순위: outputs/{id}/corrected.json
    corrected = outputs_dir / meeting_id / "corrected.json"
    if corrected.is_file():
        return corrected, "output"

    # 2순위: checkpoints/{id}/correct.json
    checkpoint = checkpoints_dir / meeting_id / "correct.json"
    if checkpoint.is_file():
        return checkpoint, "checkpoint"

    return None, ""


@router.put(
    "/meetings/{meeting_id}/transcript",
    response_model=TranscriptResponse,
)
async def update_transcript(
    request: Request,
    meeting_id: str,
    body: TranscriptUpdateRequest,
) -> TranscriptResponse:
    """사용자가 편집한 전사문 전체(발화 목록)를 저장한다.

    Raises:
        HTTPException 400: 유효하지 않은 meeting_id
        HTTPException 404: 편집 가능한 전사 파일 없음
        HTTPException 500: 파일 쓰기 실패
    """
    _validate_meeting_id(meeting_id)
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")

    target, _ = _find_transcript_file(config, meeting_id)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"편집 가능한 전사 파일이 없습니다: {meeting_id} (먼저 파이프라인을 실행하세요)",
        )

    try:
        # 기존 데이터 로드 (num_speakers 등 메타 필드 보존)
        def _load() -> dict[str, Any]:
            with open(target, encoding="utf-8") as f:
                return cast(dict[str, Any], json.load(f))

        existing = await asyncio.to_thread(_load)

        # 발화 목록 교체
        new_utterances = [u.model_dump() for u in body.utterances]
        existing["utterances"] = new_utterances

        # 화자 수 재계산
        speakers = sorted({u["speaker"] for u in new_utterances if u["speaker"] != "UNKNOWN"})
        existing["num_speakers"] = len(speakers)

        await asyncio.to_thread(_atomic_write_json, target, existing)
        _json_cache.invalidate(target)
    except OSError as exc:
        logger.exception(f"전사문 저장 실패: {exc}")
        raise HTTPException(
            status_code=500,
            detail=f"전사문 저장 중 오류가 발생했습니다: {exc}",
        ) from exc

    logger.info(
        "전사문 수동 편집 저장: meeting_id=%s, utterances=%d",
        meeting_id,
        len(new_utterances),
    )

    return TranscriptResponse(
        utterances=[
            TranscriptUtteranceItem(
                text=u["text"],
                original_text=u.get("original_text", u["text"]),
                speaker=u["speaker"],
                start=u["start"],
                end=u["end"],
                was_corrected=u.get("was_corrected", False),
            )
            for u in new_utterances
        ],
        meeting_id=meeting_id,
        num_speakers=existing.get("num_speakers", 0),
        speakers=speakers,
        total_utterances=len(new_utterances),
    )


@router.post(
    "/meetings/{meeting_id}/transcript/replace",
    response_model=TranscriptReplaceResponse,
)
async def replace_transcript_pattern(
    request: Request,
    meeting_id: str,
    body: TranscriptReplaceRequest,
) -> TranscriptReplaceResponse:
    """전사문에서 특정 패턴을 모두 찾아 치환한다.

    자주 틀리는 오인식(예: '파이선' → 'FastAPI')을 한 번에 수정하고,
    옵션으로 용어집에 자동 등록하여 앞으로의 보정에 반영되게 한다.

    동작:
        1. 편집 대상 전사 파일(correct.json 또는 corrected.json) 로드
        2. 각 발화의 text 에서 `find` 를 `replace` 로 문자열 치환 (대소문자 구분)
        3. 변경된 발화의 `was_corrected=True` 로 마크
        4. `add_to_vocabulary=True` 면 `core.user_settings.add_vocabulary_term` 또는
           기존 동일 term 의 aliases 에 find 추가
        5. 원자적 파일 저장 + 결과 요약 반환

    Raises:
        HTTPException 400: 유효하지 않은 meeting_id 또는 빈 find/replace
        HTTPException 404: 편집 가능한 전사 파일 없음
        HTTPException 500: 파일 쓰기 실패
    """
    _validate_meeting_id(meeting_id)
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")

    if body.find == body.replace:
        raise HTTPException(
            status_code=400,
            detail="find와 replace가 같습니다. 다른 값을 입력해 주세요.",
        )

    target, _ = _find_transcript_file(config, meeting_id)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"편집 가능한 전사 파일이 없습니다: {meeting_id}",
        )

    try:

        def _load() -> dict[str, Any]:
            with open(target, encoding="utf-8") as f:
                return cast(dict[str, Any], json.load(f))

        existing = await asyncio.to_thread(_load)
        utterances = existing.get("utterances", [])

        total_changes = 0
        updated_count = 0
        for u in utterances:
            text = u.get("text", "")
            if body.find in text:
                new_text = text.replace(body.find, body.replace)
                change_count = text.count(body.find)
                total_changes += change_count
                updated_count += 1
                u["text"] = new_text
                u["was_corrected"] = True

        if total_changes == 0:
            return TranscriptReplaceResponse(
                changes=0,
                updated_utterances=0,
                vocabulary_action=None,
                vocabulary_term_id=None,
            )

        existing["utterances"] = utterances
        await asyncio.to_thread(_atomic_write_json, target, existing)
        _json_cache.invalidate(target)

        # 용어집 자동 등록
        vocab_action: str | None = None
        vocab_term_id: str | None = None
        if body.add_to_vocabulary:
            try:
                from core import user_settings as _us

                vocab = _us.load_vocabulary(force_reload=True)
                # 기존에 같은 term 이 있으면 alias 에 find 추가
                existing_term = None
                for t in vocab.terms:
                    if t.term.strip().lower() == body.replace.strip().lower():
                        existing_term = t
                        break

                if existing_term is not None:
                    if body.find not in existing_term.aliases:
                        new_aliases = list(existing_term.aliases) + [body.find]
                        _us.update_vocabulary_term(term_id=existing_term.id, aliases=new_aliases)
                        vocab_action = "alias_added"
                    else:
                        vocab_action = "alias_already_exists"
                    vocab_term_id = existing_term.id
                else:
                    new_term = _us.add_vocabulary_term(
                        term=body.replace,
                        aliases=[body.find],
                        note=f"'{meeting_id}' 전사 편집에서 자동 등록",
                    )
                    vocab_action = "term_created"
                    vocab_term_id = new_term.id
                logger.info(
                    "용어집 자동 등록: action=%s, term=%s, alias=%s",
                    vocab_action,
                    body.replace,
                    body.find,
                )
            except Exception as exc:
                # 용어집 등록 실패는 전사 수정 자체를 실패시키지 않는다
                logger.warning(f"용어집 자동 등록 실패 (전사 수정은 유지): {exc}")
                vocab_action = "failed"

    except OSError as exc:
        logger.exception(f"전사문 치환 실패: {exc}")
        raise HTTPException(
            status_code=500,
            detail=f"전사문 치환 중 오류가 발생했습니다: {exc}",
        ) from exc

    logger.info(
        "전사문 패턴 치환: meeting_id=%s, find=%r, replace=%r, changes=%d",
        meeting_id,
        body.find,
        body.replace,
        total_changes,
    )

    return TranscriptReplaceResponse(
        changes=total_changes,
        updated_utterances=updated_count,
        vocabulary_action=vocab_action,
        vocabulary_term_id=vocab_term_id,
    )


@router.post("/meetings/{meeting_id}/summarize")
async def summarize_meeting(
    request: Request,
    meeting_id: str,
    force: bool = False,
) -> dict[str, str]:
    """온디맨드로 회의 요약(LLM 후처리)을 실행한다.

    skip_llm_steps=True로 파이프라인을 실행한 뒤,
    나중에 LLM 단계(correct + summarize)만 별도 실행할 때 사용한다.
    백그라운드 태스크로 비동기 실행된다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 회의 고유 식별자
        force: True이면 기존 요약 체크포인트를 삭제하고 재생성

    Returns:
        요약 시작 확인 메시지

    Raises:
        HTTPException: 유효하지 않은 ID(400), 상태 파일 미존재(404),
                       체크포인트 미존재(400), 파이프라인 미초기화(503)
    """
    import asyncio

    from core.pipeline import PipelineStep

    _validate_meeting_id(meeting_id)
    pipeline = _get_pipeline_manager(request)

    # 상태 파일 / 체크포인트 존재 여부를 사전 검증
    try:
        merge_cp = pipeline._get_checkpoint_path(meeting_id, PipelineStep.MERGE)
        if not merge_cp.exists():
            # 이슈 I: merge 체크포인트가 없다면 state 파일 유무와 상관없이 404
            state_path = pipeline._get_state_path(meeting_id)
            if not state_path.exists():
                raise HTTPException(
                    status_code=404,
                    detail=f"회의를 찾을 수 없습니다: {meeting_id}",
                )
            raise HTTPException(
                status_code=400,
                detail=f"merge 체크포인트가 없습니다. 파이프라인을 먼저 실행하세요: {meeting_id}",
            )

        # 이슈 I: merge 체크포인트는 있는데 state 파일만 유실된 경우 자동 재구성.
        # 404 로 차단하지 않고 체크포인트 기반으로 state 를 복원하여 summarize 진행.
        state_path = pipeline._get_state_path(meeting_id)
        if not state_path.exists():
            logger.warning(f"state 파일 유실, merge 체크포인트 기반 재구성: {meeting_id}")
            pipeline._rebuild_state_from_checkpoints(meeting_id)

        # force=True: 기존 요약 체크포인트/출력 삭제 (재생성)
        if force:
            outputs_dir = _get_outputs_dir(request)
            # 체크포인트 삭제
            for cp_name in ("correct.json", "summarize.json"):
                cp_path = pipeline._get_checkpoint_path(
                    meeting_id,
                    PipelineStep.CORRECT if "correct" in cp_name else PipelineStep.SUMMARIZE,
                )
                if cp_path.exists():
                    cp_path.unlink()
                    logger.info(f"기존 체크포인트 삭제: {cp_path}")
            # 출력 파일 삭제
            meeting_out = outputs_dir / meeting_id
            for fname in ("summary.md", "meeting_minutes.md", "summary.json", "corrected.json"):
                fpath = meeting_out / fname
                if fpath.exists():
                    fpath.unlink()
                    logger.info(f"기존 출력 파일 삭제: {fpath}")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"요약 사전 검증 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"요약 사전 검증 중 오류가 발생했습니다: {e}",
        ) from e

    # 백그라운드 태스크로 LLM 단계 실행
    task = asyncio.create_task(
        pipeline.run_llm_steps(meeting_id),
        name=f"llm-steps-{meeting_id}",
    )
    task.add_done_callback(_log_task_exception)
    running_tasks = getattr(request.app.state, "running_tasks", None)
    if running_tasks is not None:
        running_tasks.add(task)
        task.add_done_callback(running_tasks.discard)

    logger.info(f"온디맨드 요약 시작: {meeting_id} (force={force})")

    return {
        "status": "ok",
        "message": "요약 생성을 시작합니다.",
        "meeting_id": meeting_id,
    }
