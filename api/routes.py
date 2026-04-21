"""
API 라우터 모듈 (API Router Module)

목적: FastAPI 라우터로 REST API 엔드포인트를 정의한다.
주요 기능:
    - /api/status: 시스템 상태 및 작업 큐 현황 조회
    - /api/meetings: 전체 회의 목록 조회
    - /api/meetings/{meeting_id}: 특정 회의 상세 조회
    - /api/search: 하이브리드 검색 (벡터 + FTS5)
    - /api/chat: RAG 기반 AI Chat
    - /api/settings: 시스템 설정 조회/수정 (GET/PUT)
    - pydantic 요청/응답 스키마 정의
의존성: fastapi, pydantic, pyyaml, search/hybrid_search, search/chat, core/job_queue
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """백그라운드 태스크의 미처리 예외를 로깅한다.

    asyncio.Task.add_done_callback()에 등록하여 사용한다.
    태스크가 예외로 종료된 경우 logger.error로 기록하고,
    CancelledError는 정상 취소이므로 무시한다.

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


# === PERF: JSON 파일 캐시 (mtime 기반 무효화) ===


class _JsonFileCache:
    """JSON 파일을 mtime 기반으로 캐싱하는 스레드 안전 캐시.

    파일이 변경(mtime 갱신)되면 자동으로 다시 파싱한다.
    동일 파일의 반복 요청에서 JSON 파싱 오버헤드를 제거한다.

    Args:
        max_size: 최대 캐시 항목 수 (기본값: 64)
    """

    def __init__(self, max_size: int = 64) -> None:
        self._cache: dict[str, tuple[float, Any]] = {}
        self._max_size = max_size
        self._lock = threading.Lock()

    def get(self, file_path: Path) -> Any:
        """캐시된 JSON 데이터를 반환한다. 변경 시 자동 갱신.

        Args:
            file_path: JSON 파일 경로

        Returns:
            파싱된 JSON 데이터

        Raises:
            FileNotFoundError: 파일이 없을 때
            json.JSONDecodeError: JSON 파싱 실패 시
        """
        key = str(file_path)
        current_mtime = file_path.stat().st_mtime

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                cached_mtime, cached_data = cached
                if cached_mtime == current_mtime:
                    return cached_data

        # 캐시 미스 또는 mtime 변경 → 다시 파싱
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)

        with self._lock:
            # LRU 간이 구현: 최대 크기 초과 시 가장 오래된 항목 제거
            if len(self._cache) >= self._max_size and key not in self._cache:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
            self._cache[key] = (current_mtime, data)

        return data

    def invalidate(self, file_path: Path) -> None:
        """특정 파일의 캐시를 무효화한다.

        Args:
            file_path: 무효화할 JSON 파일 경로
        """
        with self._lock:
            self._cache.pop(str(file_path), None)


# 모듈 수준 JSON 파일 캐시 싱글턴
_json_cache = _JsonFileCache()

# === API 라우터 ===

router = APIRouter(prefix="/api", tags=["api"])


# === 요청/응답 Pydantic 스키마 ===


class SystemResourcesResponse(BaseModel):
    """시스템 리소스 상태 응답 스키마.

    Attributes:
        ram_used_gb: 사용 중인 RAM (GB)
        ram_total_gb: 전체 RAM (GB)
        ram_percent: RAM 사용률 (%)
        cpu_percent: CPU 사용률 (%)
        loaded_model: 현재 로드된 모델명 (없으면 None)
    """

    ram_used_gb: float
    ram_total_gb: float
    ram_percent: float
    cpu_percent: float
    loaded_model: str | None = None


class StatusResponse(BaseModel):
    """시스템 상태 응답 스키마.

    Attributes:
        status: 서버 동작 상태 ("ok")
        queue_summary: 상태별 작업 수 집계
        active_jobs: 현재 진행 중인 작업 수
        total_jobs: 전체 작업 수
    """

    status: str = "ok"
    queue_summary: dict[str, int] = Field(default_factory=dict)
    active_jobs: int = 0
    total_jobs: int = 0
    is_recording: bool = False
    recording_duration: float = 0.0


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


class MeetingsResponse(BaseModel):
    """회의 목록 응답 스키마.

    Attributes:
        meetings: 회의 목록
        total: 전체 회의 수
    """

    meetings: list[MeetingItem] = Field(default_factory=list)
    total: int = 0


class SearchRequest(BaseModel):
    """검색 요청 스키마.

    Attributes:
        query: 검색 쿼리 문자열
        date_filter: 날짜 필터 (선택, 예: "2026-03-04")
        speaker_filter: 화자 필터 (선택, 예: "SPEAKER_00")
        meeting_id_filter: 회의 ID 필터 (선택)
        top_k: 반환할 최대 결과 수 (선택)
    """

    query: str = Field(..., min_length=1, description="검색 쿼리")
    date_filter: str | None = None
    speaker_filter: str | None = None
    meeting_id_filter: str | None = None
    top_k: int | None = Field(None, ge=1, le=20)


class SearchResultItem(BaseModel):
    """검색 결과 아이템 스키마.

    Attributes:
        chunk_id: 청크 고유 식별자
        text: 청크 텍스트
        score: RRF 결합 점수
        meeting_id: 회의 식별자
        date: 회의 날짜
        speakers: 화자 목록
        start_time: 시작 시간 (초)
        end_time: 종료 시간 (초)
        chunk_index: 청크 순서 인덱스
        source: 검색 소스 ("vector", "fts", "both")
    """

    chunk_id: str
    text: str
    score: float
    meeting_id: str
    date: str
    speakers: list[str] = Field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    chunk_index: int = 0
    source: str = "both"


class SearchResponse(BaseModel):
    """검색 응답 스키마.

    Attributes:
        results: 검색 결과 목록
        query: 원본 검색 쿼리
        total_found: 검색된 결과 수
        vector_count: 벡터 검색 결과 수
        fts_count: FTS 검색 결과 수
        filters_applied: 적용된 필터 정보
    """

    results: list[SearchResultItem] = Field(default_factory=list)
    query: str
    total_found: int = 0
    vector_count: int = 0
    fts_count: int = 0
    filters_applied: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    """Chat 요청 스키마.

    Attributes:
        query: 사용자 질문
        session_id: 대화 세션 ID (선택)
        meeting_id_filter: 특정 회의로 검색 범위 제한 (선택)
        date_filter: 특정 날짜로 검색 범위 제한 (선택)
        speaker_filter: 특정 화자로 검색 범위 제한 (선택)
    """

    query: str = Field(..., min_length=1, description="사용자 질문")
    session_id: str | None = None
    meeting_id_filter: str | None = None
    date_filter: str | None = None
    speaker_filter: str | None = None


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


class ChatReferenceItem(BaseModel):
    """Chat 참조 출처 스키마.

    Attributes:
        chunk_id: 청크 고유 식별자
        meeting_id: 회의 식별자
        date: 회의 날짜
        speakers: 화자 목록
        start_time: 시작 시간 (초)
        end_time: 종료 시간 (초)
        text_preview: 청크 텍스트 미리보기
        score: 검색 관련도 점수
    """

    chunk_id: str
    meeting_id: str
    date: str
    speakers: list[str] = Field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    text_preview: str = ""
    score: float = 0.0


class ChatResponse(BaseModel):
    """Chat 응답 스키마.

    Attributes:
        answer: LLM이 생성한 답변
        references: 참조 출처 목록
        query: 원본 질문
        has_context: 검색 컨텍스트 존재 여부
        llm_used: LLM 응답 성공 여부
        error_message: 에러 메시지 (선택)
    """

    answer: str
    references: list[ChatReferenceItem] = Field(default_factory=list)
    query: str
    has_context: bool = True
    llm_used: bool = True
    error_message: str | None = None


# === 헬퍼 함수 ===


def _get_job_queue(request: Request) -> Any:
    """app.state에서 AsyncJobQueue를 가져온다.

    Args:
        request: FastAPI Request 객체

    Returns:
        AsyncJobQueue 인스턴스

    Raises:
        HTTPException: job_queue가 초기화되지 않았을 때 (503)
    """
    queue = getattr(request.app.state, "job_queue", None)
    if queue is None:
        raise HTTPException(
            status_code=503,
            detail="작업 큐가 초기화되지 않았습니다.",
        )
    return queue


def _get_search_engine(request: Request) -> Any:
    """app.state에서 HybridSearchEngine을 가져온다.

    Args:
        request: FastAPI Request 객체

    Returns:
        HybridSearchEngine 인스턴스

    Raises:
        HTTPException: search_engine이 초기화되지 않았을 때 (503)
    """
    engine = getattr(request.app.state, "search_engine", None)
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail="검색 엔진이 초기화되지 않았습니다.",
        )
    return engine


# meeting_id 유효성 검증 정규식 (path traversal 방지)
_MEETING_ID_PATTERN = re.compile(r"^[\w\-\.]+$")


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


def _get_outputs_dir(request: Request) -> Path:
    """app.state.config에서 outputs 디렉토리 경로를 반환한다.

    Args:
        request: FastAPI Request 객체

    Returns:
        outputs 디렉토리 절대 경로
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="서버 설정이 초기화되지 않았습니다.",
        )
    return config.paths.resolved_outputs_dir


def _get_chat_engine(request: Request) -> Any:
    """app.state에서 ChatEngine을 가져온다.

    Args:
        request: FastAPI Request 객체

    Returns:
        ChatEngine 인스턴스

    Raises:
        HTTPException: chat_engine이 초기화되지 않았을 때 (503)
    """
    engine = getattr(request.app.state, "chat_engine", None)
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail="Chat 엔진이 초기화되지 않았습니다.",
        )
    return engine


# === 엔드포인트 ===


@router.get("/status", response_model=StatusResponse)
async def get_status(request: Request) -> StatusResponse:
    """시스템 상태를 반환한다.

    작업 큐의 상태별 집계와 활성 작업 수를 포함한다.

    Args:
        request: FastAPI Request 객체

    Returns:
        StatusResponse: 시스템 상태 정보
    """
    queue = _get_job_queue(request)

    try:
        summary = await queue.count_by_status()
        all_jobs = await queue.get_all_jobs()

        # 진행 중인 상태 목록 (queued, completed, failed 제외)
        active_statuses = {
            "recording",
            "transcribing",
            "diarizing",
            "merging",
            "embedding",
        }
        active_count = sum(count for status, count in summary.items() if status in active_statuses)

        # 녹음 상태 확인
        recorder = getattr(request.app.state, "recorder", None)
        is_recording = False
        recording_duration = 0.0
        if recorder is not None:
            is_recording = recorder.is_recording
            recording_duration = round(recorder.current_duration, 1)

        return StatusResponse(
            status="ok",
            queue_summary=summary,
            active_jobs=active_count,
            total_jobs=len(all_jobs),
            is_recording=is_recording,
            recording_duration=recording_duration,
        )
    except Exception as e:
        logger.exception(f"상태 조회 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"상태 조회 중 오류가 발생했습니다: {e}",
        ) from e


@router.get("/meetings", response_model=MeetingsResponse)
async def get_meetings(
    request: Request,
    offset: int = 0,
    limit: int = 50,
) -> MeetingsResponse:
    """회의 목록을 반환한다.

    PERF: 페이지네이션을 지원하여 대량 데이터 시 응답 속도를 개선한다.
    최신순으로 정렬된 회의(작업) 목록을 offset/limit으로 페이징한다.

    Args:
        request: FastAPI Request 객체
        offset: 건너뛸 항목 수 (기본 0)
        limit: 반환할 최대 항목 수 (기본 50, 최대 200)

    Returns:
        MeetingsResponse: 회의 목록 (페이징 적용)
    """
    queue = _get_job_queue(request)

    # limit 상한 제한
    limit = min(limit, 200)

    try:
        all_jobs = await queue.get_all_jobs()
        total = len(all_jobs)

        # PERF: 메모리에서 슬라이싱으로 페이지네이션 적용
        # (SQLite 쿼리에 LIMIT/OFFSET 추가 시 JobQueue 인터페이스 변경 필요)
        paged_jobs = all_jobs[offset : offset + limit]

        meetings = [
            MeetingItem(
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
            for job in paged_jobs
        ]

        return MeetingsResponse(
            meetings=meetings,
            total=total,
        )
    except Exception as e:
        logger.exception(f"회의 목록 조회 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"회의 목록 조회 중 오류가 발생했습니다: {e}",
        ) from e


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

        # 재시도 실행 (job_id 기반)
        updated_job = await asyncio.to_thread(queue.queue.retry_job, job.id)

        # 이전 취소 요청이 set 에 남아있을 수 있으니 정리 (stale 방어)
        job_processor = getattr(request.app.state, "job_processor", None)
        if job_processor is not None:
            job_processor._cancellation_requests.discard(meeting_id)

        logger.info(f"회의 재시도 요청: {meeting_id} (job_id={job.id})")

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
async def transcribe_meeting(request: Request, meeting_id: str) -> MeetingItem:
    """녹음 완료된 회의의 전사를 시작한다.

    recorded 상태의 작업을 queued로 전환하여 전사 파이프라인을 트리거한다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 전사할 회의 고유 식별자

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

        if job.status != JobStatus.RECORDED.value:
            raise HTTPException(
                status_code=409,
                detail=f"전사를 시작할 수 없는 상태입니다: {job.status} (recorded 상태만 가능)",
            )

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
        1. 체크포인트 디렉토리 전체 삭제 (pipeline_state.json 포함)
        2. 출력 디렉토리의 corrected.json/summary.md 삭제 (오디오는 보존)
        3. job 상태를 queued 로 강제 전환 (retry_count 0 으로 리셋)
        4. ChromaDB/FTS5 의 stale 청크는 embedder 단계에서 멱등 삭제

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

        # 1) 체크포인트 디렉토리 삭제
        checkpoints_dir = config.paths.resolved_checkpoints_dir / meeting_id
        if checkpoints_dir.exists():
            await asyncio.to_thread(shutil.rmtree, checkpoints_dir)
            logger.info(f"재전사: 체크포인트 삭제 — {checkpoints_dir}")

        # 2) 출력 파일 삭제 (오디오/녹음본은 보존)
        outputs_meeting_dir = config.paths.resolved_outputs_dir / meeting_id
        if outputs_meeting_dir.exists():
            for fname in ("corrected.json", "summary.md"):
                fpath = outputs_meeting_dir / fname
                if fpath.exists():
                    try:
                        await asyncio.to_thread(fpath.unlink)
                    except OSError as exc:
                        logger.warning(f"재전사: {fname} 삭제 실패: {exc}")

        # 3) job 상태 강제 리셋
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

    Raises:
        HTTPException: pipeline_state.json 이 없을 때 (404)
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="설정이 초기화되지 않았습니다.")

    state_path = config.paths.resolved_checkpoints_dir / meeting_id / "pipeline_state.json"
    if not state_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"파이프라인 상태 파일이 없습니다: {meeting_id}",
        )

    try:
        data = await asyncio.to_thread(lambda: json.loads(state_path.read_text(encoding="utf-8")))
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


@router.delete("/meetings/{meeting_id}")
async def delete_meeting(request: Request, meeting_id: str) -> dict[str, str]:
    """회의를 삭제한다 (DB 레코드 + 오디오 파일 → quarantine).

    Phase 1-7: 오디오 파일이 watcher에 의해 재감지되는 문제를 차단하기 위해
    DB 삭제와 함께 원본 오디오 파일을 quarantine 디렉토리로 이동한다.
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

        # DB 삭제 (반드시 먼저 — 파일 이동 실패해도 DB는 정리)
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
                    logger.info(
                        f"오디오 파일 격리 완료: {audio_path} → {new_path}"
                    )
                except QuarantineError as e:
                    # 파일 이동 실패해도 DB 삭제는 이미 성공 — 경고만 남기고 진행
                    logger.warning(
                        f"오디오 파일 격리 실패 (DB 삭제는 완료): {e}"
                    )
            else:
                logger.debug(
                    f"오디오 파일이 이미 존재하지 않음: {audio_path}"
                )

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


# 원자적 파일 쓰기 헬퍼 — core/io_utils.py 의 공용 구현을 사용한다.
# 기존 두 곳에 분산되어 있던 패턴을 통합하기 위해 thin alias 로 유지.
from core.io_utils import atomic_write_json as _atomic_write_json  # noqa: E402
from core.io_utils import atomic_write_text as _atomic_write_text  # noqa: E402

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
                return json.load(f)

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
                return json.load(f)

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


@router.post("/search", response_model=SearchResponse)
async def search(request: Request, body: SearchRequest) -> SearchResponse:
    """하이브리드 검색을 수행한다.

    벡터 검색(ChromaDB)과 키워드 검색(FTS5)을 RRF로 결합하여
    관련 회의 내용을 검색한다.

    Args:
        request: FastAPI Request 객체
        body: SearchRequest 검색 요청

    Returns:
        SearchResponse: 검색 결과

    Raises:
        HTTPException: 빈 쿼리(400), 엔진 미초기화(503), 서버 에러(500)
    """
    search_engine = _get_search_engine(request)

    try:
        from search.hybrid_search import EmptyQueryError, ModelLoadError

        result = await search_engine.search(
            query=body.query,
            date_filter=body.date_filter,
            speaker_filter=body.speaker_filter,
            meeting_id_filter=body.meeting_id_filter,
            top_k=body.top_k,
        )

        # SearchResult → SearchResultItem 변환
        items = [
            SearchResultItem(
                chunk_id=r.chunk_id,
                text=r.text,
                score=r.score,
                meeting_id=r.meeting_id,
                date=r.date,
                speakers=r.speakers,
                start_time=r.start_time,
                end_time=r.end_time,
                chunk_index=r.chunk_index,
                source=r.source,
            )
            for r in result.results
        ]

        return SearchResponse(
            results=items,
            query=result.query,
            total_found=result.total_found,
            vector_count=result.vector_count,
            fts_count=result.fts_count,
            filters_applied=result.filters_applied,
        )

    except EmptyQueryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ModelLoadError as e:
        logger.error(f"검색 모델 로드 실패: {e}")
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"검색 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"검색 중 오류가 발생했습니다: {e}",
        ) from e


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    """RAG 기반 AI Chat을 수행한다.

    하이브리드 검색으로 관련 회의 내용을 찾은 후,
    EXAONE LLM으로 답변을 생성한다.

    Args:
        request: FastAPI Request 객체
        body: ChatRequest 채팅 요청

    Returns:
        ChatResponse: AI 답변 + 참조 출처

    Raises:
        HTTPException: 빈 질문(400), 엔진 미초기화(503), 서버 에러(500)
    """
    chat_engine = _get_chat_engine(request)

    try:
        from search.chat import EmptyQueryError as ChatEmptyQueryError

        result = await chat_engine.chat(
            query=body.query,
            session_id=body.session_id,
            meeting_id_filter=body.meeting_id_filter,
            date_filter=body.date_filter,
            speaker_filter=body.speaker_filter,
        )

        # ChatReference → ChatReferenceItem 변환
        refs = [
            ChatReferenceItem(
                chunk_id=r.chunk_id,
                meeting_id=r.meeting_id,
                date=r.date,
                speakers=r.speakers,
                start_time=r.start_time,
                end_time=r.end_time,
                text_preview=r.text_preview,
                score=r.score,
            )
            for r in result.references
        ]

        return ChatResponse(
            answer=result.answer,
            references=refs,
            query=result.query,
            has_context=result.has_context,
            llm_used=result.llm_used,
            error_message=result.error_message,
        )

    except ChatEmptyQueryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"Chat 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Chat 중 오류가 발생했습니다: {e}",
        ) from e


# === 시스템 리소스 엔드포인트 ===


@router.get("/system/resources", response_model=SystemResourcesResponse)
async def get_system_resources(request: Request) -> SystemResourcesResponse:
    """시스템 리소스 사용량을 반환한다.

    psutil로 RAM/CPU 사용량을 측정하고,
    ModelLoadManager에서 현재 로드된 모델명을 조회한다.

    Args:
        request: FastAPI Request 객체

    Returns:
        SystemResourcesResponse: 시스템 리소스 정보
    """
    import psutil

    mem = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=None)

    # model_manager에서 현재 로드된 모델명 조회
    model_manager = getattr(request.app.state, "model_manager", None)
    loaded_model = None
    if model_manager is not None:
        loaded_model = getattr(model_manager, "current_model_name", None)

    return SystemResourcesResponse(
        ram_used_gb=round(mem.used / (1024**3), 2),
        ram_total_gb=round(mem.total / (1024**3), 2),
        ram_percent=round(mem.percent, 1),
        cpu_percent=round(cpu, 1),
        loaded_model=loaded_model,
    )


# === 온디맨드 요약 엔드포인트 ===


def _get_pipeline_manager(request: Request) -> Any:
    """app.state에서 PipelineManager를 가져온다.

    Args:
        request: FastAPI Request 객체

    Returns:
        PipelineManager 인스턴스

    Raises:
        HTTPException: pipeline_manager가 초기화되지 않았을 때 (503)
    """
    pipeline = getattr(request.app.state, "pipeline_manager", None)
    if pipeline is None:
        raise HTTPException(
            status_code=503,
            detail="파이프라인이 초기화되지 않았습니다.",
        )
    return pipeline


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
        state_path = pipeline._get_state_path(meeting_id)
        if not state_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"회의를 찾을 수 없습니다: {meeting_id}",
            )

        merge_cp = pipeline._get_checkpoint_path(meeting_id, PipelineStep.MERGE)
        if not merge_cp.exists():
            raise HTTPException(
                status_code=400,
                detail=f"merge 체크포인트가 없습니다. 파이프라인을 먼저 실행하세요: {meeting_id}",
            )

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


class SummarizeBatchRequest(BaseModel):
    """일괄 요약 요청 모델."""

    meeting_ids: list[str] = Field(
        default_factory=list,
        description="요약할 회의 ID 목록. 빈 리스트이면 요약이 없는 전체 회의 대상.",
    )


@router.post("/meetings/summarize-batch")
async def summarize_batch(
    request: Request,
    body: SummarizeBatchRequest | None = None,
) -> dict[str, Any]:
    """일괄 요약 생성: 여러 회의의 LLM 후처리를 순차 실행한다.

    meeting_ids를 지정하면 해당 회의만, 빈 리스트이면
    merge 체크포인트가 있고 summary가 없는 모든 회의를 대상으로 한다.
    메모리 부족 방지를 위해 백그라운드에서 순차(하나씩) 실행된다.

    Args:
        request: FastAPI Request 객체
        body: 요약할 회의 ID 목록 (선택)

    Returns:
        요약 시작 확인 메시지 및 대상 회의 목록
    """

    pipeline = _get_pipeline_manager(request)
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")

    checkpoints_dir = config.paths.resolved_checkpoints_dir
    outputs_dir = config.paths.resolved_outputs_dir

    meeting_ids = body.meeting_ids if body and body.meeting_ids else []

    if not meeting_ids:
        # merge 체크포인트가 있고 summary가 없는 회의 자동 탐색
        for cp_dir in sorted(checkpoints_dir.iterdir()):
            if not cp_dir.is_dir():
                continue
            mid = cp_dir.name
            merge_cp = cp_dir / "merge.json"
            summary_md = outputs_dir / mid / "summary.md"
            if merge_cp.is_file() and not summary_md.is_file():
                meeting_ids.append(mid)

    if not meeting_ids:
        return {
            "status": "ok",
            "message": "요약 대상 회의가 없습니다.",
            "meeting_ids": [],
            "total": 0,
        }

    # 유효성 검증: merge 체크포인트 존재 여부
    valid_ids: list[str] = []
    for mid in meeting_ids:
        _validate_meeting_id(mid)
        merge_cp = checkpoints_dir / mid / "merge.json"
        if merge_cp.is_file():
            valid_ids.append(mid)
        else:
            logger.warning(f"일괄 요약 건너뜀: merge 체크포인트 없음 ({mid})")

    if not valid_ids:
        return {
            "status": "ok",
            "message": "유효한 요약 대상이 없습니다.",
            "meeting_ids": [],
            "total": 0,
        }

    async def _run_batch(ids: list[str]) -> None:
        """백그라운드에서 순차적으로 LLM 단계를 실행한다."""
        for mid in ids:
            try:
                logger.info(f"일괄 요약 실행: {mid}")
                await pipeline.run_llm_steps(mid)
                logger.info(f"일괄 요약 완료: {mid}")
            except Exception:
                logger.exception(f"일괄 요약 실패: {mid}")

    task = asyncio.create_task(
        _run_batch(valid_ids),
        name="summarize-batch",
    )
    task.add_done_callback(_log_task_exception)
    running_tasks = getattr(request.app.state, "running_tasks", None)
    if running_tasks is not None:
        running_tasks.add(task)
        task.add_done_callback(running_tasks.discard)

    logger.info(f"일괄 요약 시작: {len(valid_ids)}건")

    return {
        "status": "ok",
        "message": f"일괄 요약 생성을 시작합니다 ({len(valid_ids)}건).",
        "meeting_ids": valid_ids,
        "total": len(valid_ids),
    }


# === 녹음 관련 헬퍼 ===


def _get_recorder(request: Request) -> Any:
    """app.state에서 AudioRecorder를 가져온다.

    Args:
        request: FastAPI Request 객체

    Returns:
        AudioRecorder 인스턴스

    Raises:
        HTTPException: recorder가 초기화되지 않았을 때 (503)
    """
    recorder = getattr(request.app.state, "recorder", None)
    if recorder is None:
        raise HTTPException(
            status_code=503,
            detail="녹음 기능이 초기화되지 않았습니다.",
        )
    return recorder


# === 녹음 엔드포인트 ===


class RecordingStatusResponse(BaseModel):
    """녹음 상태 응답 스키마.

    Attributes:
        state: 녹음 상태 ("idle", "recording", "stopping")
        is_recording: 녹음 중 여부
        duration_seconds: 현재 녹음 경과 시간 (초)
        meeting_id: 현재 녹음 중인 회의 ID
        device: 사용 중인 오디오 장치명
        is_system_audio: 시스템 오디오 캡처 여부
    """

    state: str
    is_recording: bool = False
    duration_seconds: float = 0.0
    meeting_id: str | None = None
    device: str | None = None
    is_system_audio: bool = False


class AudioDeviceItem(BaseModel):
    """오디오 장치 응답 스키마.

    Attributes:
        index: ffmpeg 장치 인덱스
        name: 장치 이름
        is_blackhole: BlackHole 가상 장치 여부
    """

    index: int
    name: str
    is_blackhole: bool = False


class RecordingStartRequest(BaseModel):
    """녹음 시작 요청 스키마.

    Attributes:
        meeting_id: 회의 식별자 (선택, 없으면 자동 생성)
    """

    meeting_id: str | None = None


@router.get("/recording/status", response_model=RecordingStatusResponse)
async def get_recording_status(
    request: Request,
) -> RecordingStatusResponse:
    """녹음 상태를 조회한다.

    Args:
        request: FastAPI Request 객체

    Returns:
        RecordingStatusResponse: 현재 녹음 상태
    """
    recorder = _get_recorder(request)
    status = recorder.get_status()
    return RecordingStatusResponse(**status)


@router.post("/recording/start")
async def start_recording(
    request: Request,
    body: RecordingStartRequest | None = None,
) -> dict[str, Any]:
    """수동 녹음을 시작한다.

    Args:
        request: FastAPI Request 객체
        body: 녹음 시작 요청 (선택)

    Returns:
        녹음 시작 결과

    Raises:
        HTTPException: 이미 녹음 중(409), 장치 에러(500), 서버 에러(500)
    """
    recorder = _get_recorder(request)
    meeting_id = body.meeting_id if body else None

    try:
        from steps.recorder import AlreadyRecordingError, AudioDeviceError

        await recorder.start_recording(meeting_id=meeting_id)
        return {
            "status": "ok",
            "message": "녹음을 시작했습니다.",
            "meeting_id": recorder._meeting_id,
            "device": recorder.current_device_name,
        }
    except AlreadyRecordingError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except AudioDeviceError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"녹음 시작 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"녹음 시작 중 오류가 발생했습니다: {e}",
        ) from e


@router.post("/recording/stop")
async def stop_recording(request: Request) -> dict[str, Any]:
    """녹음을 정지한다.

    Args:
        request: FastAPI Request 객체

    Returns:
        녹음 정지 결과

    Raises:
        HTTPException: 서버 에러(500)
    """
    recorder = _get_recorder(request)

    try:
        result = await recorder.stop_recording()
        if result is None:
            return {
                "status": "ok",
                "message": "녹음이 정지되었습니다. (최소 시간 미달로 파일 파기)",
                "discarded": True,
            }

        return {
            "status": "ok",
            "message": "녹음이 정지되었습니다.",
            "file_path": str(result.file_path),
            "duration_seconds": result.duration_seconds,
            "audio_device": result.audio_device,
        }
    except Exception as e:
        logger.exception(f"녹음 정지 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"녹음 정지 중 오류가 발생했습니다: {e}",
        ) from e


@router.get("/recording/devices", response_model=list[AudioDeviceItem])
async def get_recording_devices(
    request: Request,
) -> list[AudioDeviceItem]:
    """사용 가능한 오디오 장치 목록을 반환한다.

    Args:
        request: FastAPI Request 객체

    Returns:
        오디오 장치 목록

    Raises:
        HTTPException: 장치 검색 실패(500)
    """
    recorder = _get_recorder(request)

    try:
        devices = await recorder.detect_audio_devices()
        return [
            AudioDeviceItem(
                index=dev.index,
                name=dev.name,
                is_blackhole=dev.is_blackhole,
            )
            for dev in devices
        ]
    except Exception as e:
        logger.exception(f"오디오 장치 조회 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"오디오 장치 조회 중 오류가 발생했습니다: {e}",
        ) from e


# === 설정 관리 API ===

# 허용된 MLX 모델 목록 (보안: 화이트리스트 방식)
_ALLOWED_MLX_MODELS = {
    "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit",
    "mlx-community/gemma-4-e4b-it-4bit",
    "mlx-community/gemma-4-e2b-it-4bit",
}

# BCP-47 언어 코드 화이트리스트 패턴 (보안: YAML 인젝션 차단)
# 예: "ko", "en", "en-US", "zh-Hant", "ja-JP-x-keb"
_STT_LANGUAGE_PATTERN = re.compile(r"^[a-zA-Z]{2,8}(-[a-zA-Z0-9]{2,8})*$")

# 프론트엔드 드롭다운용 모델 프리셋 (읽기 전용)
_AVAILABLE_MODELS = [
    {
        "id": "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit",
        "label": "EXAONE 3.5 7.8B (한국어 특화)",
        "size": "~5GB",
        "description": "LG AI Research가 한국어에 특화시킨 모델로, 회의록 보정·요약에 검증된 안정적인 선택입니다.",
    },
    {
        "id": "mlx-community/gemma-4-e4b-it-4bit",
        "label": "Gemma 4 E4B (다국어, 빠름)",
        "size": "~5.3GB",
        "description": "Google이 만든 최신 경량 모델로, EXAONE보다 약 50% 빠르며 다국어를 골고루 잘 처리합니다.",
    },
    {
        "id": "mlx-community/gemma-4-e2b-it-4bit",
        "label": "Gemma 4 E2B (경량)",
        "size": "~3GB",
        "description": "Gemma 4 의 가벼운 버전으로, 8GB RAM 환경에서도 안정적으로 동작합니다.",
    },
]


class SettingsResponse(BaseModel):
    """설정 응답 스키마.

    현재 시스템 설정값을 프론트엔드에 전달한다.

    Attributes:
        llm_backend: LLM 백엔드 ("mlx" 또는 "ollama")
        llm_mlx_model_name: MLX 모델명
        llm_temperature: 생성 온도 (0.0~2.0)
        llm_mlx_max_tokens: MLX 최대 생성 토큰
        llm_skip_steps: LLM 단계 스킵 여부 (pipeline.skip_llm_steps)
        stt_language: STT 언어 코드
        available_models: 선택 가능한 모델 프리셋 목록 (읽기 전용)
    """

    llm_backend: str = "mlx"
    llm_mlx_model_name: str = "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit"
    llm_temperature: float = 0.3
    llm_mlx_max_tokens: int = 2000
    llm_skip_steps: bool = True
    stt_language: str = "ko"
    # 환각 필터 (hallucination_filter)
    hf_enabled: bool = True
    hf_no_speech_threshold: float = 0.9
    hf_compression_ratio_threshold: float = 2.4
    hf_repetition_threshold: int = 3
    available_models: list[dict] = Field(default_factory=lambda: _AVAILABLE_MODELS)


class SettingsUpdateRequest(BaseModel):
    """설정 업데이트 요청 스키마.

    변경하려는 필드만 전송하면 된다 (부분 업데이트).

    Attributes:
        llm_backend: LLM 백엔드 ("mlx" 또는 "ollama")
        llm_mlx_model_name: MLX 모델명 (허용 목록 내)
        llm_temperature: 생성 온도 (0.0~2.0)
        llm_mlx_max_tokens: MLX 최대 생성 토큰 (100 이상)
        llm_skip_steps: LLM 단계 스킵 여부
        stt_language: STT 언어 코드
    """

    llm_backend: str | None = None
    llm_mlx_model_name: str | None = None
    llm_temperature: float | None = None
    llm_mlx_max_tokens: int | None = None
    llm_skip_steps: bool | None = None
    stt_language: str | None = None
    # 환각 필터
    hf_enabled: bool | None = None
    hf_no_speech_threshold: float | None = None
    hf_compression_ratio_threshold: float | None = None
    hf_repetition_threshold: int | None = None


class SettingsUpdateResponse(BaseModel):
    """설정 업데이트 응답 스키마.

    Attributes:
        settings: 업데이트된 설정값
        message: 결과 메시지
        changed_fields: 변경된 필드 목록
    """

    settings: SettingsResponse
    message: str = "설정이 저장되었습니다."
    changed_fields: list[str] = Field(default_factory=list)


def _get_config_path() -> Path:
    """config.yaml 파일 경로를 반환한다.

    Returns:
        프로젝트 루트의 config.yaml 절대 경로
    """
    return Path(__file__).parent.parent / "config.yaml"


def _replace_yaml_value(text: str, section: str, key: str, new_val: str) -> str:
    """YAML 텍스트에서 특정 섹션의 키 값을 교체한다 (주석 보존).

    정규식 기반으로 섹션을 찾고 해당 섹션 내에서 키의 값 부분만 교체한다.
    라인 끝의 주석(`# ...`)은 그대로 유지된다.

    Args:
        text: 원본 YAML 텍스트
        section: 최상위 섹션명 (예: "stt", "llm")
        key: 교체할 키 (예: "model_name")
        new_val: 새 값 (문자열일 경우 호출자가 직접 따옴표를 포함시켜 전달)

    Returns:
        값이 교체된 YAML 텍스트. 섹션/키를 찾지 못하면 원본 반환.
    """
    section_pattern = re.compile(rf"^{re.escape(section)}:", re.MULTILINE)
    section_match = section_pattern.search(text)
    if not section_match:
        return text

    start = section_match.end()
    next_section = re.search(r"^\S", text[start:], re.MULTILINE)
    end = start + next_section.start() if next_section else len(text)

    section_text = text[start:end]
    key_pattern = re.compile(
        rf"^(  {re.escape(key)}:)\s*[^\n#]*(#[^\n]*)?$",
        re.MULTILINE,
    )
    key_match = key_pattern.search(section_text)
    if not key_match:
        return text

    comment = key_match.group(2) or ""
    if comment:
        comment = "  " + comment.strip()
    replacement = f"{key_match.group(1)} {new_val}{comment}"
    new_section = section_text[: key_match.start()] + replacement + section_text[key_match.end() :]
    return text[:start] + new_section + text[end:]


@router.get("/settings", response_model=SettingsResponse)
async def get_settings(request: Request) -> SettingsResponse:
    """현재 시스템 설정을 반환한다.

    app.state.config에서 설정값을 읽어 SettingsResponse로 매핑한다.

    Args:
        request: FastAPI Request 객체

    Returns:
        현재 설정값

    Raises:
        HTTPException: 설정이 초기화되지 않았을 때 (503)
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="서버 설정이 초기화되지 않았습니다.",
        )

    return SettingsResponse(
        llm_backend=config.llm.backend,
        llm_mlx_model_name=config.llm.mlx_model_name,
        llm_temperature=config.llm.temperature,
        llm_mlx_max_tokens=config.llm.mlx_max_tokens,
        llm_skip_steps=config.pipeline.skip_llm_steps,
        stt_language=config.stt.language,
        hf_enabled=config.hallucination_filter.enabled,
        hf_no_speech_threshold=config.hallucination_filter.no_speech_threshold,
        hf_compression_ratio_threshold=config.hallucination_filter.compression_ratio_threshold,
        hf_repetition_threshold=config.hallucination_filter.repetition_threshold,
        available_models=_AVAILABLE_MODELS,
    )


@router.put("/settings", response_model=SettingsUpdateResponse)
async def update_settings(
    request: Request,
    body: SettingsUpdateRequest,
) -> SettingsUpdateResponse:
    """시스템 설정을 업데이트한다.

    전달된 필드만 config.yaml에 반영하고 런타임 config도 갱신한다.
    모델이 변경된 경우 안내 메시지를 포함한다.

    Args:
        request: FastAPI Request 객체
        body: 변경할 설정 필드 (Optional — 전달된 것만 반영)

    Returns:
        업데이트 결과 (변경된 설정, 메시지, 변경 필드 목록)

    Raises:
        HTTPException: 검증 실패(400), 설정 미초기화(503), 파일 저장 실패(500)
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="서버 설정이 초기화되지 않았습니다.",
        )

    # 변경할 필드만 추출 (None이 아닌 값)
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return SettingsUpdateResponse(
            settings=SettingsResponse(
                llm_backend=config.llm.backend,
                llm_mlx_model_name=config.llm.mlx_model_name,
                llm_temperature=config.llm.temperature,
                llm_mlx_max_tokens=config.llm.mlx_max_tokens,
                llm_skip_steps=config.pipeline.skip_llm_steps,
                stt_language=config.stt.language,
                hf_enabled=config.hallucination_filter.enabled,
                hf_no_speech_threshold=config.hallucination_filter.no_speech_threshold,
                hf_compression_ratio_threshold=config.hallucination_filter.compression_ratio_threshold,
                hf_repetition_threshold=config.hallucination_filter.repetition_threshold,
                available_models=_AVAILABLE_MODELS,
            ),
            message="변경할 설정이 없습니다.",
            changed_fields=[],
        )

    # === 입력 검증 ===
    if "llm_backend" in updates and updates["llm_backend"] not in ("mlx", "ollama"):
        raise HTTPException(
            status_code=400,
            detail="llm_backend는 'mlx' 또는 'ollama'만 허용됩니다.",
        )

    if "llm_mlx_model_name" in updates:
        if updates["llm_mlx_model_name"] not in _ALLOWED_MLX_MODELS:
            raise HTTPException(
                status_code=400,
                detail=f"허용되지 않은 모델입니다. 허용 목록: {sorted(_ALLOWED_MLX_MODELS)}",
            )

    if "llm_temperature" in updates:
        temp = updates["llm_temperature"]
        if not (0.0 <= temp <= 2.0):
            raise HTTPException(
                status_code=400,
                detail="llm_temperature는 0.0~2.0 범위여야 합니다.",
            )

    if "llm_mlx_max_tokens" in updates and updates["llm_mlx_max_tokens"] < 100:
        raise HTTPException(
            status_code=400,
            detail="llm_mlx_max_tokens는 100 이상이어야 합니다.",
        )

    if "stt_language" in updates:
        lang = updates["stt_language"]
        # 보안: BCP-47 형식만 허용. 따옴표·개행·#·콜론 등이 들어가면
        # _replace_yaml_value 가 그대로 config.yaml 에 삽입해 YAML 파일이
        # 손상될 수 있음 (예: "en\": y\n#" 같은 입력으로 부팅 불가).
        # 따라서 알파벳 + 선택적 BCP-47 서브태그(`-`로 분리)만 허용한다.
        if not lang or not _STT_LANGUAGE_PATTERN.match(lang):
            raise HTTPException(
                status_code=400,
                detail=(
                    "stt_language 는 BCP-47 언어 코드 형식만 허용됩니다 "
                    "(예: ko, en, en-US, zh-Hant)."
                ),
            )

    # 환각 필터 파라미터 검증 (Pydantic Field 의 ge/le 와 동일 범위)
    if "hf_no_speech_threshold" in updates:
        v = updates["hf_no_speech_threshold"]
        if not (0.0 <= v <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="hf_no_speech_threshold 는 0.0~1.0 범위여야 합니다.",
            )
    if "hf_compression_ratio_threshold" in updates:
        v = updates["hf_compression_ratio_threshold"]
        if not (1.0 <= v <= 10.0):
            raise HTTPException(
                status_code=400,
                detail="hf_compression_ratio_threshold 는 1.0~10.0 범위여야 합니다.",
            )
    if "hf_repetition_threshold" in updates:
        v = updates["hf_repetition_threshold"]
        if not (isinstance(v, int) and 2 <= v <= 10):
            raise HTTPException(
                status_code=400,
                detail="hf_repetition_threshold 는 2~10 범위의 정수여야 합니다.",
            )

    # === config.yaml 파일 업데이트 ===
    config_path = _get_config_path()
    try:
        with open(config_path, encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning(f"config.yaml 미발견: {config_path}. 새로 생성합니다.")
        yaml_data = {}

    # YAML 필드 매핑 (API 필드명 → YAML 경로)
    changed_fields: list[str] = []
    model_changed = False

    if "llm_backend" in updates:
        yaml_data.setdefault("llm", {})["backend"] = updates["llm_backend"]
        changed_fields.append("llm_backend")

    if "llm_mlx_model_name" in updates:
        yaml_data.setdefault("llm", {})["mlx_model_name"] = updates["llm_mlx_model_name"]
        changed_fields.append("llm_mlx_model_name")
        model_changed = True

    if "llm_temperature" in updates:
        yaml_data.setdefault("llm", {})["temperature"] = updates["llm_temperature"]
        changed_fields.append("llm_temperature")

    if "llm_mlx_max_tokens" in updates:
        yaml_data.setdefault("llm", {})["mlx_max_tokens"] = updates["llm_mlx_max_tokens"]
        changed_fields.append("llm_mlx_max_tokens")

    if "llm_skip_steps" in updates:
        yaml_data.setdefault("pipeline", {})["skip_llm_steps"] = updates["llm_skip_steps"]
        changed_fields.append("llm_skip_steps")

    if "stt_language" in updates:
        yaml_data.setdefault("stt", {})["language"] = updates["stt_language"]
        changed_fields.append("stt_language")

    if "hf_enabled" in updates:
        yaml_data.setdefault("hallucination_filter", {})["enabled"] = updates["hf_enabled"]
        changed_fields.append("hf_enabled")
    if "hf_no_speech_threshold" in updates:
        yaml_data.setdefault("hallucination_filter", {})["no_speech_threshold"] = updates[
            "hf_no_speech_threshold"
        ]
        changed_fields.append("hf_no_speech_threshold")
    if "hf_compression_ratio_threshold" in updates:
        yaml_data.setdefault("hallucination_filter", {})["compression_ratio_threshold"] = updates[
            "hf_compression_ratio_threshold"
        ]
        changed_fields.append("hf_compression_ratio_threshold")
    if "hf_repetition_threshold" in updates:
        yaml_data.setdefault("hallucination_filter", {})["repetition_threshold"] = updates[
            "hf_repetition_threshold"
        ]
        changed_fields.append("hf_repetition_threshold")

    # YAML 파일 저장 (주석 보존: 정규식으로 해당 키의 값만 교체)
    try:
        with open(config_path, encoding="utf-8") as f:
            content = f.read()

        # 정규식 기반 값 교체 (모듈 레벨 _replace_yaml_value 사용 — 주석 보존)
        if "llm_backend" in updates:
            content = _replace_yaml_value(content, "llm", "backend", f'"{updates["llm_backend"]}"')
        if "llm_mlx_model_name" in updates:
            content = _replace_yaml_value(
                content, "llm", "mlx_model_name", f'"{updates["llm_mlx_model_name"]}"'
            )
        if "llm_temperature" in updates:
            content = _replace_yaml_value(
                content, "llm", "temperature", str(updates["llm_temperature"])
            )
        if "llm_mlx_max_tokens" in updates:
            content = _replace_yaml_value(
                content, "llm", "mlx_max_tokens", str(updates["llm_mlx_max_tokens"])
            )
        if "llm_skip_steps" in updates:
            val = "true" if updates["llm_skip_steps"] else "false"
            content = _replace_yaml_value(content, "pipeline", "skip_llm_steps", val)
        if "stt_language" in updates:
            content = _replace_yaml_value(
                content, "stt", "language", f'"{updates["stt_language"]}"'
            )
        if "hf_enabled" in updates:
            val = "true" if updates["hf_enabled"] else "false"
            content = _replace_yaml_value(content, "hallucination_filter", "enabled", val)
        if "hf_no_speech_threshold" in updates:
            content = _replace_yaml_value(
                content,
                "hallucination_filter",
                "no_speech_threshold",
                str(updates["hf_no_speech_threshold"]),
            )
        if "hf_compression_ratio_threshold" in updates:
            content = _replace_yaml_value(
                content,
                "hallucination_filter",
                "compression_ratio_threshold",
                str(updates["hf_compression_ratio_threshold"]),
            )
        if "hf_repetition_threshold" in updates:
            content = _replace_yaml_value(
                content,
                "hallucination_filter",
                "repetition_threshold",
                str(updates["hf_repetition_threshold"]),
            )

        # 원자적 쓰기 + .bak 백업 (도중 죽어도 config.yaml 손상 방지)
        await asyncio.to_thread(_atomic_write_text, config_path, content)
        logger.info(f"config.yaml 저장 완료 (원자적, 주석 보존). 변경 필드: {changed_fields}")
    except OSError as e:
        logger.exception(f"config.yaml 저장 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"설정 파일 저장에 실패했습니다: {e}",
        ) from e

    # === 런타임 config 갱신 ===
    if "llm_backend" in updates:
        new_llm = config.llm.model_copy(update={"backend": updates["llm_backend"]})
        config = config.model_copy(update={"llm": new_llm})

    if "llm_mlx_model_name" in updates:
        new_llm = config.llm.model_copy(update={"mlx_model_name": updates["llm_mlx_model_name"]})
        config = config.model_copy(update={"llm": new_llm})

    if "llm_temperature" in updates:
        new_llm = config.llm.model_copy(update={"temperature": updates["llm_temperature"]})
        config = config.model_copy(update={"llm": new_llm})

    if "llm_mlx_max_tokens" in updates:
        new_llm = config.llm.model_copy(update={"mlx_max_tokens": updates["llm_mlx_max_tokens"]})
        config = config.model_copy(update={"llm": new_llm})

    if "llm_skip_steps" in updates:
        new_pipeline = config.pipeline.model_copy(
            update={"skip_llm_steps": updates["llm_skip_steps"]}
        )
        config = config.model_copy(update={"pipeline": new_pipeline})

    if "stt_language" in updates:
        new_stt = config.stt.model_copy(update={"language": updates["stt_language"]})
        config = config.model_copy(update={"stt": new_stt})

    # 환각 필터 런타임 갱신
    hf_updates: dict[str, Any] = {}
    if "hf_enabled" in updates:
        hf_updates["enabled"] = updates["hf_enabled"]
    if "hf_no_speech_threshold" in updates:
        hf_updates["no_speech_threshold"] = updates["hf_no_speech_threshold"]
    if "hf_compression_ratio_threshold" in updates:
        hf_updates["compression_ratio_threshold"] = updates["hf_compression_ratio_threshold"]
    if "hf_repetition_threshold" in updates:
        hf_updates["repetition_threshold"] = updates["hf_repetition_threshold"]
    if hf_updates:
        new_hf = config.hallucination_filter.model_copy(update=hf_updates)
        config = config.model_copy(update={"hallucination_filter": new_hf})

    # app.state.config 갱신
    request.app.state.config = config

    # 응답 메시지 구성
    message = "설정이 저장되었습니다."
    if model_changed:
        message += " 모델 변경은 다음 LLM 호출 시 적용됩니다."

    return SettingsUpdateResponse(
        settings=SettingsResponse(
            llm_backend=config.llm.backend,
            llm_mlx_model_name=config.llm.mlx_model_name,
            llm_temperature=config.llm.temperature,
            llm_mlx_max_tokens=config.llm.mlx_max_tokens,
            llm_skip_steps=config.pipeline.skip_llm_steps,
            stt_language=config.stt.language,
            hf_enabled=config.hallucination_filter.enabled,
            hf_no_speech_threshold=config.hallucination_filter.no_speech_threshold,
            hf_compression_ratio_threshold=config.hallucination_filter.compression_ratio_threshold,
            hf_repetition_threshold=config.hallucination_filter.repetition_threshold,
            available_models=_AVAILABLE_MODELS,
        ),
        message=message,
        changed_fields=changed_fields,
    )


# =========================================================================
# 사용자 편집 가능 프롬프트 & 용어집 엔드포인트
# =========================================================================
# core/user_settings.py를 통해 프롬프트(보정/요약/채팅)와 고유명사 용어집을
# 동적으로 관리한다. 기존 /api/settings와 달리 config.yaml을 수정하지 않고
# ~/.meeting-transcriber/user_data/ 아래 JSON 파일로 영속화한다.
# =========================================================================

from core import user_settings as _user_settings  # noqa: E402
from core.user_settings import (  # noqa: E402
    PromptEntry,
    PromptsData,
    UserSettingsError,
    UserSettingsIOError,
    UserSettingsLockError,
    UserSettingsValidationError,
    VocabularyTerm,
)

# --- 요청/응답 스키마 ---


class PromptEntryPayload(BaseModel):
    """프롬프트 항목 요청/응답 페이로드."""

    system_prompt: str = Field(..., min_length=20, max_length=8000)
    updated_at: str | None = None


class PromptsPayload(BaseModel):
    """프롬프트 전체 응답 페이로드."""

    schema_version: int = 1
    corrector: PromptEntryPayload
    summarizer: PromptEntryPayload
    chat: PromptEntryPayload
    updated_at: str | None = None


class PromptsResponse(BaseModel):
    """GET /api/prompts 응답."""

    prompts: PromptsPayload


class PromptsUpdateRequest(BaseModel):
    """PUT /api/prompts 요청 (부분 업데이트 지원)."""

    corrector: PromptEntryPayload | None = None
    summarizer: PromptEntryPayload | None = None
    chat: PromptEntryPayload | None = None


class VocabularyTermPayload(BaseModel):
    """용어 항목 응답 페이로드."""

    id: str
    term: str
    aliases: list[str] = Field(default_factory=list)
    category: str | None = None
    note: str | None = None
    enabled: bool = True
    created_at: str | None = None


class VocabularyResponse(BaseModel):
    """GET /api/vocabulary 응답."""

    terms: list[VocabularyTermPayload]
    total: int
    schema_version: int = 1


class VocabularyAddRequest(BaseModel):
    """POST /api/vocabulary/terms 요청."""

    term: str = Field(..., min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    category: str | None = Field(default=None, max_length=50)
    note: str | None = Field(default=None, max_length=500)
    enabled: bool = True


class VocabularyUpdateRequest(BaseModel):
    """PUT /api/vocabulary/terms/{id} 요청 (부분 업데이트)."""

    term: str | None = Field(default=None, min_length=1, max_length=100)
    aliases: list[str] | None = Field(default=None, max_length=20)
    category: str | None = Field(default=None, max_length=50)
    note: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None


# --- 변환 헬퍼 ---


def _prompts_to_payload(data: PromptsData) -> PromptsPayload:
    """PromptsData → API 응답 페이로드로 변환한다."""
    raw = data.model_dump(mode="json")
    return PromptsPayload(
        schema_version=raw["schema_version"],
        corrector=PromptEntryPayload(**raw["corrector"]),
        summarizer=PromptEntryPayload(**raw["summarizer"]),
        chat=PromptEntryPayload(**raw["chat"]),
        updated_at=raw.get("updated_at"),
    )


def _term_to_payload(term: VocabularyTerm) -> VocabularyTermPayload:
    """VocabularyTerm → API 응답 페이로드로 변환한다."""
    return VocabularyTermPayload(**term.model_dump(mode="json"))


def _map_user_settings_error(exc: UserSettingsError) -> HTTPException:
    """저장소 예외를 HTTPException으로 매핑한다.

    Args:
        exc: UserSettingsError 인스턴스

    Returns:
        적절한 상태 코드와 한국어 메시지가 담긴 HTTPException
    """
    if isinstance(exc, UserSettingsValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, UserSettingsLockError):
        return HTTPException(status_code=503, detail=f"{exc}. 잠시 후 다시 시도해 주세요.")
    if isinstance(exc, UserSettingsIOError):
        return HTTPException(status_code=500, detail=str(exc))
    return HTTPException(status_code=500, detail=f"내부 저장소 오류: {exc}")


# --- 프롬프트 엔드포인트 ---


@router.get("/prompts", response_model=PromptsResponse)
async def get_prompts() -> PromptsResponse:
    """현재 저장된 프롬프트 3종(보정/요약/채팅)을 조회한다.

    Returns:
        PromptsResponse

    Raises:
        HTTPException: I/O 실패(500)
    """
    try:
        data = _user_settings.load_prompts()
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e
    return PromptsResponse(prompts=_prompts_to_payload(data))


@router.put("/prompts", response_model=PromptsResponse)
async def update_prompts(body: PromptsUpdateRequest) -> PromptsResponse:
    """프롬프트를 부분 업데이트한다 (전달된 필드만 반영).

    Args:
        body: 변경할 프롬프트 (선택적 필드)

    Returns:
        업데이트된 PromptsResponse

    Raises:
        HTTPException: 검증 실패(400), 락 타임아웃(503), I/O 실패(500)
    """
    try:
        current = _user_settings.load_prompts()
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e

    updates: dict[str, Any] = {}
    if body.corrector is not None:
        updates["corrector"] = PromptEntry(system_prompt=body.corrector.system_prompt)
    if body.summarizer is not None:
        updates["summarizer"] = PromptEntry(system_prompt=body.summarizer.system_prompt)
    if body.chat is not None:
        updates["chat"] = PromptEntry(system_prompt=body.chat.system_prompt)

    if not updates:
        return PromptsResponse(prompts=_prompts_to_payload(current))

    try:
        merged = current.model_copy(update=updates)
        saved = _user_settings.save_prompts(merged)
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"프롬프트 검증 실패: {e}") from e

    logger.info("프롬프트 업데이트: %s", ", ".join(sorted(updates.keys())))
    return PromptsResponse(prompts=_prompts_to_payload(saved))


@router.post("/prompts/reset", response_model=PromptsResponse)
async def reset_prompts() -> PromptsResponse:
    """프롬프트를 공장 기본값으로 복원한다.

    Returns:
        복원된 PromptsResponse

    Raises:
        HTTPException: I/O 실패(500)
    """
    try:
        data = _user_settings.reset_prompts_to_default()
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e
    return PromptsResponse(prompts=_prompts_to_payload(data))


# --- 용어집 엔드포인트 ---


@router.get("/vocabulary", response_model=VocabularyResponse)
async def get_vocabulary() -> VocabularyResponse:
    """전체 용어집을 조회한다.

    Returns:
        VocabularyResponse
    """
    try:
        data = _user_settings.load_vocabulary()
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e
    return VocabularyResponse(
        terms=[_term_to_payload(t) for t in data.terms],
        total=len(data.terms),
        schema_version=data.schema_version,
    )


@router.post(
    "/vocabulary/terms",
    response_model=VocabularyTermPayload,
    status_code=201,
)
async def add_vocabulary_term_endpoint(
    body: VocabularyAddRequest,
) -> VocabularyTermPayload:
    """용어를 추가한다 (ULID는 서버가 생성).

    Args:
        body: 추가할 용어 정보

    Returns:
        생성된 VocabularyTermPayload

    Raises:
        HTTPException: 중복·최대 개수 초과·검증 실패(400), 저장 실패(500)
    """
    try:
        new_term = _user_settings.add_vocabulary_term(
            term=body.term,
            aliases=body.aliases,
            category=body.category,
            note=body.note,
            enabled=body.enabled,
        )
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e
    return _term_to_payload(new_term)


@router.put("/vocabulary/terms/{term_id}", response_model=VocabularyTermPayload)
async def update_vocabulary_term_endpoint(
    term_id: str,
    body: VocabularyUpdateRequest,
) -> VocabularyTermPayload:
    """용어를 부분 업데이트한다.

    Args:
        term_id: 대상 용어의 ULID
        body: 변경할 필드 (선택적)

    Returns:
        업데이트된 VocabularyTermPayload

    Raises:
        HTTPException: 대상 없음/중복/검증 실패(400), 저장 실패(500)
    """
    try:
        updated = _user_settings.update_vocabulary_term(
            term_id=term_id,
            term=body.term,
            aliases=body.aliases,
            category=body.category,
            note=body.note,
            enabled=body.enabled,
        )
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e
    return _term_to_payload(updated)


@router.delete("/vocabulary/terms/{term_id}", status_code=204)
async def delete_vocabulary_term_endpoint(term_id: str) -> None:
    """용어를 삭제한다.

    Args:
        term_id: 삭제할 용어의 ULID

    Raises:
        HTTPException: 대상 없음(400), 저장 실패(500)
    """
    try:
        _user_settings.delete_vocabulary_term(term_id)
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e


@router.post("/vocabulary/reset", response_model=VocabularyResponse)
async def reset_vocabulary_endpoint() -> VocabularyResponse:
    """용어집을 공장 기본값(빈 목록)으로 복원한다.

    Returns:
        복원된 VocabularyResponse
    """
    try:
        data = _user_settings.reset_vocabulary_to_default()
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e
    return VocabularyResponse(
        terms=[_term_to_payload(t) for t in data.terms],
        total=len(data.terms),
        schema_version=data.schema_version,
    )


# ============================================================
# STT 모델 선택기 API (Phase 4)
# ============================================================

# 모듈 레벨 임포트 — 테스트에서 monkeypatch 하기 쉽도록 이름을 고정한다.
from core.stt_model_downloader import DownloadConflictError  # noqa: E402
from core.stt_model_registry import STT_MODELS, STTModelSpec  # noqa: E402
from core.stt_model_registry import get_by_id as _stt_get_by_id  # noqa: E402
from core.stt_model_status import (  # noqa: E402
    ModelStatus,
    get_actual_size_mb,
    get_model_status,
)


class STTModelInfo(BaseModel):
    """STT 모델 한 건의 정적 메타데이터 + 런타임 상태."""

    id: str
    label: str
    description: str
    base_model: str
    expected_size_mb: int
    actual_size_mb: float | None = None
    cer_percent: float
    wer_percent: float
    memory_gb: float
    rtf: float
    license: str
    is_default: bool
    is_recommended: bool
    status: str
    is_active: bool
    download_progress: int | None = None
    error_message: str | None = None


class STTModelsResponse(BaseModel):
    """GET /api/stt-models 응답 스키마."""

    models: list[STTModelInfo]
    active_model_id: str
    active_model_path: str


def _is_active_stt_model(spec: STTModelSpec, active_path: str) -> bool:
    """spec 이 현재 활성 STT 모델인지 판정한다.

    다음 세 경로 중 하나라도 `active_path` (config.stt.model_name) 와 일치하면
    활성으로 본다:

    1. `spec.model_path` (HF repo ID 또는 로컬 양자화 경로, tilde 가능)
    2. `spec.model_path` 의 tilde 확장본
    3. `get_effective_model_path(spec)` — 수동 임포트가 있으면 그 로컬 경로

    수동 임포트 대응이 핵심 이유:
        - 자동 다운로드 실패 → 사용자가 브라우저로 받아 import-manual
        - 활성화 시 config.stt.model_name 에는 수동 임포트 로컬 경로가 저장됨
        - 그러나 spec.model_path 는 여전히 HF repo ID 이므로 단순 비교하면
          `is_active=False` 가 되어 UI 에 "활성화" 버튼이 계속 표시되는 버그
          → 효과적 경로까지 함께 비교해 해결
    """
    from core.stt_model_status import get_effective_model_path

    candidates: list[str] = [spec.model_path]
    try:
        candidates.append(str(Path(spec.model_path).expanduser()))
    except Exception:  # noqa: BLE001
        pass
    try:
        effective = get_effective_model_path(spec)
        if effective not in candidates:
            candidates.append(effective)
        try:
            expanded = str(Path(effective).expanduser())
            if expanded not in candidates:
                candidates.append(expanded)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass

    return active_path in candidates


@router.get("/stt-models", response_model=STTModelsResponse)
async def list_stt_models(request: Request) -> STTModelsResponse:
    """STT 모델 레지스트리의 3개 모델과 동적 상태를 반환한다.

    각 모델에 대해 다운로드 여부(READY/NOT_DOWNLOADED)를 확인하고,
    현재 진행 중인 다운로드가 있으면 진행률을 오버레이한다.
    config.stt.model_name 과 일치하는 모델에 is_active=True 플래그를 설정한다.
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")

    downloader = getattr(request.app.state, "stt_downloader", None)
    active_path = config.stt.model_name

    models: list[STTModelInfo] = []
    active_id: str | None = None

    for spec in STT_MODELS:
        # 1차: 디스크/HF 캐시 기반 상태
        disk_status = get_model_status(spec)
        # 2차: 진행 중 작업이 있으면 그 상태로 오버라이드
        job = downloader.get_progress(spec.id) if downloader is not None else None
        runtime_status = job.status if job is not None else disk_status

        # 방어 로직: 디스크가 READY 이고 runtime 이 ERROR 이면 stale 한 에러 job.
        # 사용자가 수동 다운로드·가져오기로 파일을 배치했으나 이전 자동 다운로드의
        # 에러 job 이 in-memory 에 남아있는 경우 (앱 재시작 없이도 복구되도록).
        if (
            disk_status == ModelStatus.READY
            and runtime_status == ModelStatus.ERROR
            and downloader is not None
        ):
            logger.info("stale ERROR job 제거 (디스크는 READY): %s", spec.id)
            downloader.clear_job(spec.id)
            job = None
            runtime_status = disk_status

        is_active = _is_active_stt_model(spec, active_path)
        if is_active:
            active_id = spec.id

        actual_size: float | None = None
        if disk_status == ModelStatus.READY:
            try:
                actual_size = get_actual_size_mb(spec.model_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("실제 모델 크기 계산 실패 (%s): %s", spec.id, exc)

        models.append(
            STTModelInfo(
                id=spec.id,
                label=spec.label,
                description=spec.description,
                base_model=spec.base_model,
                expected_size_mb=spec.expected_size_mb,
                actual_size_mb=actual_size,
                cer_percent=spec.cer_percent,
                wer_percent=spec.wer_percent,
                memory_gb=spec.memory_gb,
                rtf=spec.rtf,
                license=spec.license,
                is_default=spec.is_default,
                is_recommended=spec.is_recommended,
                status=runtime_status.value,
                is_active=is_active,
                download_progress=job.progress_percent if job is not None else None,
                error_message=job.error_message if job is not None else None,
            )
        )

    return STTModelsResponse(
        models=models,
        active_model_id=active_id or "",
        active_model_path=active_path,
    )


@router.post("/stt-models/{model_id}/download", status_code=202)
async def download_stt_model(request: Request, model_id: str) -> dict[str, Any]:
    """지정한 STT 모델의 다운로드를 백그라운드에서 시작한다.

    Raises:
        HTTPException 404: 알 수 없는 model_id
        HTTPException 409: 이미 다른 모델 다운로드가 진행 중
        HTTPException 503: 다운로더 미초기화
    """
    # 보안: model_id 화이트리스트 검증 (레지스트리에 등록된 ID만 허용)
    spec = _stt_get_by_id(model_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 STT 모델: {model_id}")

    downloader = getattr(request.app.state, "stt_downloader", None)
    if downloader is None:
        raise HTTPException(status_code=503, detail="STT 다운로더가 초기화되지 않았습니다.")

    try:
        job_id = await downloader.start_download(model_id)
    except DownloadConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        # 다운로더 내부의 레지스트리 재검증에서 실패 (이론상 도달 불가)
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    logger.info("STT 모델 다운로드 요청 수락: %s (%s)", model_id, job_id)
    return {
        "job_id": job_id,
        "model_id": model_id,
        "status": "downloading",
        "message": "다운로드를 시작합니다.",
    }


@router.post("/stt-models/{model_id}/download-direct", status_code=202)
async def download_stt_model_direct(request: Request, model_id: str) -> dict[str, Any]:
    """HF 직접 URL 로 STT 모델을 다운로드한다 (huggingface_hub 건너뜀).

    기업 프록시·MITM SSL 검사·ISP 필터링 등으로 `huggingface_hub` 가 실패하는
    환경에서 사용자가 명시적으로 선택하는 대체 경로. `urllib.request` 스트리밍
    다운로드로 파일을 `{id}-manual/` 디렉토리에 저장한다.

    일반 `/download` 엔드포인트도 실패 시 자동으로 direct URL 폴백을 시도하지만,
    사용자가 "URL로 직접 받기" 버튼으로 이 경로를 직접 호출할 수도 있다.

    Raises:
        HTTPException 404: 알 수 없는 model_id
        HTTPException 409: 이미 다른 모델 다운로드가 진행 중
        HTTPException 503: 다운로더 미초기화
    """
    spec = _stt_get_by_id(model_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 STT 모델: {model_id}")

    downloader = getattr(request.app.state, "stt_downloader", None)
    if downloader is None:
        raise HTTPException(status_code=503, detail="STT 다운로더가 초기화되지 않았습니다.")

    try:
        job_id = await downloader.start_download(model_id, prefer_direct=True)
    except DownloadConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    logger.info("STT 모델 직접 URL 다운로드 요청 수락: %s (%s)", model_id, job_id)
    return {
        "job_id": job_id,
        "model_id": model_id,
        "status": "downloading",
        "message": "직접 URL로 다운로드를 시작합니다.",
        "method": "direct_url",
    }


@router.get("/stt-models/{model_id}/download-status")
async def get_stt_download_status(request: Request, model_id: str) -> dict[str, Any]:
    """STT 모델 다운로드 작업의 진행 상태를 반환한다.

    Raises:
        HTTPException 404: 해당 model_id 의 작업이 없음
        HTTPException 503: 다운로더 미초기화
    """
    # 화이트리스트 검증 (알 수 없는 ID로 downloader 내부 상태를 노출하지 않음)
    if _stt_get_by_id(model_id) is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 STT 모델: {model_id}")

    downloader = getattr(request.app.state, "stt_downloader", None)
    if downloader is None:
        raise HTTPException(status_code=503, detail="STT 다운로더가 초기화되지 않았습니다.")

    job = downloader.get_progress(model_id)
    if job is None:
        raise HTTPException(status_code=404, detail="다운로드 작업을 찾을 수 없습니다.")

    return {
        "model_id": model_id,
        "job_id": job.job_id,
        "status": job.status.value,
        "progress_percent": job.progress_percent,
        "current_step": job.current_step,
        "started_at": job.started_at.isoformat(),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error_message": job.error_message,
    }


@router.post("/stt-models/{model_id}/activate")
async def activate_stt_model(request: Request, model_id: str) -> dict[str, Any]:
    """활성 STT 모델을 변경하고 config.yaml 을 업데이트한다.

    모델은 반드시 READY 상태여야 하며, config.yaml 의 stt.model_name 필드를
    주석을 보존하며 교체한 뒤 런타임 config 도 갱신한다.

    Raises:
        HTTPException 404: 알 수 없는 model_id
        HTTPException 400: 모델이 READY 상태가 아님
        HTTPException 500: config.yaml 저장 실패
        HTTPException 503: 서버 설정 미초기화
    """
    spec = _stt_get_by_id(model_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 STT 모델: {model_id}")

    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")

    # 다운로드 완료 상태 검증
    if get_model_status(spec) != ModelStatus.READY:
        raise HTTPException(
            status_code=400,
            detail="모델이 다운로드되지 않았습니다. 먼저 다운로드하세요.",
        )

    previous_model = config.stt.model_name
    # get_effective_model_path: 수동 임포트가 있으면 그 로컬 경로 우선,
    # 없으면 spec.model_path (HF repo ID 또는 로컬 양자화 경로) 사용
    from core.stt_model_status import get_effective_model_path

    spec_path = get_effective_model_path(spec)
    if spec_path.startswith(("~", "/", "./", "../")):
        new_path = str(Path(spec_path).expanduser())
    else:
        new_path = spec_path

    # config.yaml 업데이트 (주석 보존, 원자적 쓰기 + .bak 백업)
    config_path = _get_config_path()
    try:
        with open(config_path, encoding="utf-8") as f:
            content = f.read()
        content = _replace_yaml_value(content, "stt", "model_name", f'"{new_path}"')
        # 도중 죽어도 config.yaml 손상 방지 — _atomic_write_text 가 .bak 자동 생성
        await asyncio.to_thread(_atomic_write_text, config_path, content)
        logger.info(
            "활성 STT 모델 변경: %s → %s (config.yaml 원자적 저장)",
            previous_model,
            new_path,
        )
    except OSError as exc:
        logger.exception("config.yaml 저장 실패: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"설정 파일 저장에 실패했습니다: {exc}",
        ) from exc

    # 런타임 config 갱신
    new_stt = config.stt.model_copy(update={"model_name": new_path})
    request.app.state.config = config.model_copy(update={"stt": new_stt})

    return {
        "model_id": model_id,
        "previous_model_path": previous_model,
        "model_path": new_path,
        "message": "활성 모델이 변경되었습니다. 다음 전사부터 적용됩니다.",
    }


# ---------------------------------------------------------------------------
# 수동 다운로드 / 가져오기 엔드포인트
# ---------------------------------------------------------------------------
# 네트워크·방화벽·프록시 이슈로 huggingface_hub 자동 다운로드가 실패하는
# 사용자를 위해, HF 직접 URL을 노출하고 로컬에서 받은 파일을 app이 인식할 수
# 있는 위치로 가져오는 경로를 제공한다. 모든 사전 양자화된 STT 모델이 대상이다.
# ---------------------------------------------------------------------------


class STTManualDownloadFile(BaseModel):
    """수동 다운로드 파일 하나의 URL 정보."""

    name: str
    url: str
    size_bytes: int | None = None


class STTManualDownloadInfo(BaseModel):
    """GET /api/stt-models/{id}/manual-download-info 응답."""

    model_id: str
    label: str
    supported: bool
    files: list[STTManualDownloadFile] = Field(default_factory=list)
    target_directory: str = ""
    instructions: str = ""


class STTImportRequest(BaseModel):
    """POST /api/stt-models/{id}/import-manual 요청 본문."""

    source_dir: str = Field(
        ...,
        description=(
            "사용자가 다운로드한 파일들이 있는 로컬 디렉토리 절대 경로. "
            "해당 디렉토리 안에 config.json 과 weights.safetensors 파일이 있어야 한다."
        ),
    )


class STTImportResponse(BaseModel):
    """POST /api/stt-models/{id}/import-manual 응답."""

    model_id: str
    imported_dir: str
    files_copied: list[str]
    message: str


@router.get(
    "/stt-models/{model_id}/manual-download-info",
    response_model=STTManualDownloadInfo,
)
async def get_stt_manual_download_info(model_id: str) -> STTManualDownloadInfo:
    """수동 다운로드용 HF 직접 URL 목록과 타겟 폴더 경로를 반환한다.

    사용자는 응답에 포함된 `files[*].url` 을 브라우저로 직접 열어
    각 파일을 받은 뒤, `target_directory` 에 저장하면 된다.
    이후 `POST /api/stt-models/{id}/import-manual` 로 가져오기를 수행한다.

    Raises:
        HTTPException 404: 알 수 없는 model_id
    """
    from core.stt_model_registry import get_hf_download_urls, get_manual_import_dir

    spec = _stt_get_by_id(model_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 STT 모델: {model_id}")

    urls = get_hf_download_urls(spec)

    target_dir = get_manual_import_dir(spec)
    files = [STTManualDownloadFile(name=u["name"], url=u["url"]) for u in urls]

    return STTManualDownloadInfo(
        model_id=model_id,
        label=spec.label,
        supported=True,
        files=files,
        target_directory=target_dir,
        instructions=(
            "1) 아래 파일들을 브라우저로 각각 다운로드하세요.\n"
            f"2) 다운로드한 파일 2개를 한 폴더에 모으세요 (예: ~/Downloads/{spec.id}/).\n"
            "3) '가져오기' 버튼을 누르고 해당 폴더 경로를 입력하면 앱이 자동으로 "
            f"{target_dir} 로 복사합니다.\n"
            "4) 이후 '활성화' 버튼으로 이 모델을 사용할 수 있어요."
        ),
    )


@router.post(
    "/stt-models/{model_id}/import-manual",
    response_model=STTImportResponse,
)
async def import_stt_manual(
    request: Request, model_id: str, body: STTImportRequest
) -> STTImportResponse:
    """사용자가 브라우저로 받은 모델 파일을 앱 내부 경로로 복사한다.

    body.source_dir 안에 있는 config.json, weights.safetensors 를
    `~/.meeting-transcriber/stt_models/{id}-manual/` 로 복사한다.
    복사 완료 후 해당 모델은 READY 상태가 되며 활성화 가능하다.

    Raises:
        HTTPException 404: 알 수 없는 model_id
        HTTPException 400: source_dir 없음·필수 파일 누락·수동 가져오기 미지원
        HTTPException 500: 파일 복사 실패
    """
    import shutil

    from core.stt_model_registry import get_manual_import_dir

    spec = _stt_get_by_id(model_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 STT 모델: {model_id}")

    # source_dir 검증
    source = Path(body.source_dir).expanduser().resolve()
    if not source.exists() or not source.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"폴더를 찾을 수 없어요: {body.source_dir}",
        )

    required = ["config.json", "weights.safetensors"]
    missing = [name for name in required if not (source / name).is_file()]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"다음 파일이 폴더에 없어요: {', '.join(missing)}. "
                "HuggingFace에서 받은 두 파일을 모두 같은 폴더에 넣어 주세요."
            ),
        )

    # 타겟 디렉토리 생성 후 원자적 복사 (임시 경로 → rename)
    target_dir = Path(get_manual_import_dir(spec))
    target_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    try:
        for name in required:
            src_file = source / name
            dst_file = target_dir / name
            tmp_file = target_dir / (name + ".tmp")
            shutil.copy2(str(src_file), str(tmp_file))
            tmp_file.replace(dst_file)
            copied.append(name)
        logger.info("STT 모델 수동 가져오기 완료: %s ← %s", target_dir, source)
    except OSError as exc:
        logger.exception("STT 모델 수동 가져오기 실패: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"파일 복사에 실패했어요: {exc}",
        ) from exc

    # stale 한 downloader job 상태 초기화.
    # 이전 자동 다운로드가 SSL/네트워크 오류로 실패해 ERROR 상태로 남아 있었다면,
    # 수동 가져오기가 성공한 지금 그 에러 상태를 제거해야 /api/stt-models 응답이
    # 디스크 기준(READY) 으로 정상 표시된다. 앱 재시작 없이도 복구되도록 한다.
    downloader = getattr(request.app.state, "stt_downloader", None)
    if downloader is not None:
        try:
            downloader.clear_job(model_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("수동 가져오기 후 stale job 정리 실패 (무시): %s", exc)

    return STTImportResponse(
        model_id=model_id,
        imported_dir=str(target_dir),
        files_copied=copied,
        message=(
            f"모델 파일 {len(copied)}개를 가져왔어요. "
            "이제 '활성화' 버튼으로 이 모델을 사용할 수 있어요."
        ),
    )


# ============================================================
# A/B 테스트 엔드포인트 (Phase 2)
# ============================================================


# A/B 테스트 관련 심볼 — 파일 상단이 아닌 이 위치에 두는 이유는 테스트에서 monkeypatch 로
# 이름을 교체할 수 있게 하기 위함. 파일 최상단으로 옮기면 순환 import / 모듈 초기화 순서
# 문제가 발생한다. 각 import 에 E402 noqa 를 명시적으로 달아 의도를 표시한다.
from typing import Literal  # noqa: E402

from core import ab_test_store  # noqa: E402
from core.ab_test_runner import (  # noqa: E402
    LlmScope,
    ModelSpec,
)
from core.ab_test_runner import (  # noqa: E402
    cancel_test as _runner_cancel_test,
)
from core.ab_test_runner import (  # noqa: E402
    delete_test as _runner_delete_test,
)
from core.ab_test_runner import (  # noqa: E402
    get_test_result as _runner_get_test_result,
)
from core.ab_test_runner import (  # noqa: E402
    list_tests as _runner_list_tests,
)
from core.ab_test_runner import (  # noqa: E402
    new_test_id as _runner_new_test_id,
)
from core.ab_test_runner import (  # noqa: E402
    run_llm_ab_test as _runner_run_llm_ab_test,
)
from core.ab_test_runner import (  # noqa: E402
    run_stt_ab_test as _runner_run_stt_ab_test,
)

# --- Pydantic 모델 ---


class ModelSpecPayload(BaseModel):
    """A/B 비교 대상 모델 스펙.

    Attributes:
        label: 사용자에게 표시할 라벨
        model_id: HF repo ID 또는 레지스트리 ID
        backend: LLM 백엔드 ("mlx" | "ollama")
    """

    label: str
    model_id: str
    backend: Literal["mlx", "ollama"] = "mlx"


class LlmScopePayload(BaseModel):
    """LLM A/B 테스트 실행 범위.

    Attributes:
        correct: 교정 수행 여부
        summarize: 요약 수행 여부
    """

    correct: bool = True
    summarize: bool = True


class ABTestLLMRequest(BaseModel):
    """LLM A/B 테스트 요청 바디.

    Attributes:
        source_meeting_id: 원본 회의 ID
        variant_a: A 모델 스펙
        variant_b: B 모델 스펙
        scope: 실행 범위
    """

    source_meeting_id: str
    variant_a: ModelSpecPayload
    variant_b: ModelSpecPayload
    scope: LlmScopePayload = LlmScopePayload()


class ABTestSTTRequest(BaseModel):
    """STT A/B 테스트 요청 바디.

    Attributes:
        source_meeting_id: 원본 회의 ID
        variant_a: A 모델 스펙
        variant_b: B 모델 스펙
        allow_diarize_rerun: 화자분리 체크포인트가 없을 때 재실행 허용
    """

    source_meeting_id: str
    variant_a: ModelSpecPayload
    variant_b: ModelSpecPayload
    allow_diarize_rerun: bool = False


class ABTestStartedResponse(BaseModel):
    """A/B 테스트 시작 응답.

    Attributes:
        test_id: 생성된 테스트 ID
        status: 초기 상태
    """

    test_id: str
    status: str = "running"


# --- 유효성 검증 헬퍼 ---


def _validate_test_id(test_id: str) -> None:
    """test_id 를 화이트리스트로 검증한다 (path traversal 방지).

    Args:
        test_id: 검증 대상

    Raises:
        HTTPException: 유효하지 않은 형식 (400)
    """
    if not ab_test_store.is_valid_test_id(test_id):
        raise HTTPException(
            status_code=400,
            detail=f"유효하지 않은 A/B 테스트 ID 형식입니다: {test_id}",
        )


def _validate_variant(variant: str) -> None:
    """variant 경로 파라미터가 "a" 또는 "b" 인지 검증한다.

    Args:
        variant: 검증 대상

    Raises:
        HTTPException: 허용되지 않는 값 (400)
    """
    if variant not in ("a", "b"):
        raise HTTPException(
            status_code=400,
            detail=f"variant 는 'a' 또는 'b' 만 허용됩니다: {variant}",
        )


def _get_config(request: Request) -> Any:
    """app.state 에서 AppConfig 를 가져온다.

    Args:
        request: FastAPI Request 객체

    Returns:
        AppConfig 인스턴스

    Raises:
        HTTPException: config 가 초기화되지 않았을 때 (503)
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="서버 설정이 초기화되지 않았습니다.",
        )
    return config


def _get_ws_manager(request: Request) -> Any | None:
    """app.state 에서 WebSocket ConnectionManager 를 가져온다 (없으면 None).

    Args:
        request: FastAPI Request 객체

    Returns:
        ConnectionManager 또는 None
    """
    return getattr(request.app.state, "ws_manager", None)


async def _make_ab_broadcaster(request: Request):
    """A/B 테스트 러너에 주입할 ws_broadcaster 콜러블을 생성한다.

    러너가 보내는 payload dict 를 WebSocketEvent 로 변환하여 브로드캐스트한다.
    ws_manager 가 없으면 None 을 반환하여 러너가 no-op 으로 동작하게 한다.

    Args:
        request: FastAPI Request 객체

    Returns:
        async callable 또는 None
    """
    ws_manager = _get_ws_manager(request)
    if ws_manager is None:
        return None

    async def _broadcast(payload: dict[str, Any]) -> None:
        """러너 payload 를 WebSocket 이벤트로 브로드캐스트한다.

        Args:
            payload: 러너가 생성한 step_progress 딕셔너리
        """
        try:
            from api.websocket import EventType, WebSocketEvent

            event = WebSocketEvent(
                event_type=EventType.STEP_PROGRESS.value,
                data=payload,
            )
            await ws_manager.broadcast_event(event)
        except Exception as exc:  # noqa: BLE001 — 브로드캐스트 실패는 비치명적
            logger.warning(f"A/B 테스트 WS 브로드캐스트 실패(무시): {exc}")

    return _broadcast


def _validate_meeting_exists(config: Any, meeting_id: str, test_type: str = "llm") -> None:
    """원본 회의가 존재하는지 검증한다.

    test_type 에 따라 검증 기준이 다르다:
    - "stt": 오디오 파일(audio_input/{id}.wav) 만 있으면 됨 (미전사 회의 가능)
    - "llm": outputs/{id}/ 또는 checkpoints/{id}/ 가 있어야 함 (전사 완료 필요)

    Args:
        config: AppConfig
        meeting_id: 회의 ID
        test_type: "stt" | "llm"

    Raises:
        HTTPException: 필요한 파일/디렉터리가 없을 때 (404)
    """
    _validate_meeting_id(meeting_id)

    if test_type == "stt":
        wav = config.paths.resolved_audio_input_dir / f"{meeting_id}.wav"
        if not wav.exists():
            raise HTTPException(
                status_code=404,
                detail=f"오디오 파일을 찾을 수 없습니다: {meeting_id}",
            )
        return

    # LLM: checkpoints 또는 outputs 에 데이터가 있어야 함
    ckpt_dir = config.paths.resolved_checkpoints_dir / meeting_id
    out_dir = config.paths.resolved_outputs_dir / meeting_id
    if not ckpt_dir.exists() and not out_dir.exists():
        raise HTTPException(
            status_code=404,
            detail=f"원본 회의를 찾을 수 없습니다: {meeting_id}",
        )


# --- LLM 모델 로컬 보유 목록 엔드포인트 ---


# A/B 테스트에서 사용할 수 있는 LLM 프리셋 목록 (로컬 보유 여부 포함)
_LLM_PRESETS = [
    {"label": "EXAONE 3.5 7.8B 4bit", "id": "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit"},
    {"label": "Gemma 4 E4B 4bit", "id": "mlx-community/gemma-4-e4b-it-4bit"},
    {"label": "Gemma 4 E2B 4bit", "id": "mlx-community/gemma-4-e2b-it-4bit"},
    {"label": "Gemma 4 E4B UD 4bit (Unsloth)", "id": "unsloth/gemma-4-E4B-it-UD-MLX-4bit"},
    {"label": "Gemma 4 E2B UD 4bit (Unsloth)", "id": "unsloth/gemma-4-E2B-it-UD-MLX-4bit"},
]


def _check_hf_cache_exists(repo_id: str) -> bool:
    """HF 캐시에 모델이 존재하는지 확인한다.

    ~/.cache/huggingface/hub/models--{owner}--{name}/ 디렉토리의 snapshots/ 에
    파일이 존재하면 True.
    """
    from pathlib import Path

    cache_dir = (
        Path.home()
        / ".cache"
        / "huggingface"
        / "hub"
        / f"models--{repo_id.replace('/', '--')}"
        / "snapshots"
    )
    if not cache_dir.exists():
        return False
    # snapshots 아래에 실제 파일이 있는 디렉터리가 하나라도 있으면 캐시됨
    return any(snap.is_dir() and any(snap.iterdir()) for snap in cache_dir.iterdir())


@router.get(
    "/llm-models/available",
    summary="A/B 테스트용 LLM 모델 목록",
    description="로컬 HF 캐시 보유 여부를 포함한 LLM 프리셋 목록을 반환한다.",
)
async def list_available_llm_models() -> list[dict]:
    """프리셋 LLM 모델 목록 + 로컬 보유 여부를 반환한다."""
    result = []
    for preset in _LLM_PRESETS:
        available = _check_hf_cache_exists(preset["id"])
        result.append(
            {
                "label": preset["label"],
                "model_id": preset["id"],
                "available": available,
            }
        )
    return result


# --- A/B 테스트 엔드포인트 ---


@router.post(
    "/ab-tests/llm",
    status_code=202,
    response_model=ABTestStartedResponse,
    summary="LLM A/B 테스트 시작",
    description="동일 회의에 대해 LLM 모델 2종의 교정/요약을 순차 비교 실행한다.",
)
async def start_llm_ab_test(
    body: ABTestLLMRequest,
    request: Request,
) -> ABTestStartedResponse:
    """LLM A/B 테스트를 백그라운드로 시작한다.

    Args:
        body: LLM A/B 테스트 요청 바디
        request: FastAPI Request 객체

    Returns:
        202 + test_id
    """
    config = _get_config(request)

    # 사전 검증: 동일 모델 거부
    if (
        body.variant_a.model_id == body.variant_b.model_id
        and body.variant_a.backend == body.variant_b.backend
    ):
        raise HTTPException(
            status_code=400,
            detail="variant_a 와 variant_b 가 동일합니다.",
        )

    # 사전 검증: 원본 회의 존재
    _validate_meeting_exists(config, body.source_meeting_id)

    # test_id 선점
    selected_id = _runner_new_test_id()

    # ws_broadcaster 생성
    broadcaster = await _make_ab_broadcaster(request)

    # ModelLoadManager 주입
    model_manager = getattr(request.app.state, "model_manager", None)

    # 백그라운드 태스크 발사
    task = asyncio.create_task(
        _runner_run_llm_ab_test(
            config=config,
            source_meeting_id=body.source_meeting_id,
            variant_a=ModelSpec(
                label=body.variant_a.label,
                model_id=body.variant_a.model_id,
                backend=body.variant_a.backend,
            ),
            variant_b=ModelSpec(
                label=body.variant_b.label,
                model_id=body.variant_b.model_id,
                backend=body.variant_b.backend,
            ),
            scope=LlmScope(
                correct=body.scope.correct,
                summarize=body.scope.summarize,
            ),
            ws_broadcaster=broadcaster,
            model_manager=model_manager,
            test_id=selected_id,
        ),
        name=f"ab-test-llm-{selected_id}",
    )
    task.add_done_callback(_log_task_exception)

    return ABTestStartedResponse(test_id=selected_id)


@router.post(
    "/ab-tests/stt",
    status_code=202,
    response_model=ABTestStartedResponse,
    summary="STT A/B 테스트 시작",
    description="동일 회의에 대해 STT 모델 2종의 전사 결과를 순차 비교 실행한다.",
)
async def start_stt_ab_test(
    body: ABTestSTTRequest,
    request: Request,
) -> ABTestStartedResponse:
    """STT A/B 테스트를 백그라운드로 시작한다.

    Args:
        body: STT A/B 테스트 요청 바디
        request: FastAPI Request 객체

    Returns:
        202 + test_id
    """
    config = _get_config(request)

    # 사전 검증: 동일 모델 거부
    if body.variant_a.model_id == body.variant_b.model_id:
        raise HTTPException(
            status_code=400,
            detail="variant_a 와 variant_b 가 동일합니다.",
        )

    # 사전 검증: 원본 회의 존재 (STT 는 오디오만 있으면 됨)
    _validate_meeting_exists(config, body.source_meeting_id, test_type="stt")

    # test_id 선점
    selected_id = _runner_new_test_id()

    # ws_broadcaster 생성
    broadcaster = await _make_ab_broadcaster(request)

    # ModelLoadManager 주입
    model_manager = getattr(request.app.state, "model_manager", None)

    # 백그라운드 태스크 발사
    task = asyncio.create_task(
        _runner_run_stt_ab_test(
            config=config,
            source_meeting_id=body.source_meeting_id,
            variant_a=ModelSpec(
                label=body.variant_a.label,
                model_id=body.variant_a.model_id,
                backend=body.variant_a.backend,
            ),
            variant_b=ModelSpec(
                label=body.variant_b.label,
                model_id=body.variant_b.model_id,
                backend=body.variant_b.backend,
            ),
            allow_diarize_rerun=body.allow_diarize_rerun,
            ws_broadcaster=broadcaster,
            model_manager=model_manager,
            test_id=selected_id,
        ),
        name=f"ab-test-stt-{selected_id}",
    )
    task.add_done_callback(_log_task_exception)

    return ABTestStartedResponse(test_id=selected_id)


@router.get(
    "/ab-tests",
    summary="A/B 테스트 목록 조회",
    description="저장된 A/B 테스트 목록을 최신순으로 반환한다. source_meeting_id 쿼리 파라미터로 필터 가능.",
)
async def list_ab_tests(
    request: Request,
    source_meeting_id: str | None = None,
) -> dict[str, Any]:
    """A/B 테스트 목록을 조회한다.

    Args:
        request: FastAPI Request 객체
        source_meeting_id: (쿼리) 특정 원본 회의에 속한 테스트만 필터

    Returns:
        {"tests": [...]}
    """
    config = _get_config(request)
    tests = _runner_list_tests(config, source_meeting_id)
    return {"tests": tests}


@router.get(
    "/ab-tests/{test_id}",
    summary="A/B 테스트 상세 조회",
    description="metadata + variant_a/variant_b 산출물을 포함한 테스트 상세를 반환한다.",
)
async def get_ab_test(
    test_id: str,
    request: Request,
) -> dict[str, Any]:
    """특정 A/B 테스트의 상세 결과를 조회한다.

    Args:
        test_id: 테스트 ID (path param)
        request: FastAPI Request 객체

    Returns:
        {metadata, variant_a, variant_b}
    """
    _validate_test_id(test_id)
    config = _get_config(request)
    try:
        return _runner_get_test_result(config, test_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"A/B 테스트를 찾을 수 없습니다: {test_id}",
        ) from None


@router.get(
    "/ab-tests/{test_id}/variant/{variant}/summary",
    summary="A/B 테스트 variant 요약 마크다운 조회",
    description="variant_a 또는 variant_b 의 summary.md 를 text/markdown 으로 반환한다.",
)
async def get_ab_test_summary(
    test_id: str,
    variant: str,
    request: Request,
):
    """A/B 테스트 variant 의 요약 마크다운을 반환한다.

    Args:
        test_id: 테스트 ID (path param)
        variant: "a" 또는 "b" (path param)
        request: FastAPI Request 객체

    Returns:
        text/markdown Response
    """
    from fastapi.responses import Response

    _validate_test_id(test_id)
    _validate_variant(variant)
    config = _get_config(request)

    test_dir = ab_test_store.resolve_test_dir(config, test_id)
    summary_path = test_dir / f"variant_{variant}" / "summary.md"

    if not summary_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"요약 파일이 없습니다: variant_{variant}/summary.md",
        )

    content = summary_path.read_text(encoding="utf-8")
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
    )


@router.delete(
    "/ab-tests/{test_id}",
    status_code=204,
    summary="A/B 테스트 삭제",
    description="테스트 디렉터리를 통째로 삭제한다.",
)
async def delete_ab_test(
    test_id: str,
    request: Request,
):
    """A/B 테스트를 삭제한다.

    Args:
        test_id: 테스트 ID (path param)
        request: FastAPI Request 객체

    Returns:
        204 No Content
    """
    _validate_test_id(test_id)
    config = _get_config(request)
    try:
        _runner_delete_test(config, test_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"A/B 테스트를 찾을 수 없습니다: {test_id}",
        ) from None
    return None


@router.post(
    "/ab-tests/{test_id}/cancel",
    status_code=202,
    response_model=ABTestStartedResponse,
    summary="A/B 테스트 취소",
    description="진행 중인 A/B 테스트의 취소를 요청한다 (variant 경계에서 중단).",
)
async def cancel_ab_test(
    test_id: str,
    request: Request,
) -> ABTestStartedResponse:
    """A/B 테스트 취소를 요청한다.

    Args:
        test_id: 테스트 ID (path param)
        request: FastAPI Request 객체

    Returns:
        202 + test_id
    """
    _validate_test_id(test_id)
    config = _get_config(request)
    try:
        await _runner_cancel_test(config, test_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ABTestStartedResponse(test_id=test_id, status="cancelling")
