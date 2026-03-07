"""
API 라우터 모듈 (API Router Module)

목적: FastAPI 라우터로 REST API 엔드포인트를 정의한다.
주요 기능:
    - /api/status: 시스템 상태 및 작업 큐 현황 조회
    - /api/meetings: 전체 회의 목록 조회
    - /api/meetings/{meeting_id}: 특정 회의 상세 조회
    - /api/search: 하이브리드 검색 (벡터 + FTS5)
    - /api/chat: RAG 기반 AI Chat
    - pydantic 요청/응답 스키마 정의
의존성: fastapi, pydantic, search/hybrid_search, search/chat, core/job_queue
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


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
    """

    id: int
    meeting_id: str
    audio_path: str
    status: str
    retry_count: int = 0
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""


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
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"회의 상세 조회 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"회의 상세 조회 중 오류가 발생했습니다: {e}",
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


@router.delete("/meetings/{meeting_id}")
async def delete_meeting(request: Request, meeting_id: str) -> dict[str, str]:
    """회의를 삭제한다.

    meeting_id로 작업을 찾아 DB에서 삭제한다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 삭제할 회의 고유 식별자

    Returns:
        삭제 완료 메시지

    Raises:
        HTTPException: 회의를 찾을 수 없을 때 (404)
    """
    from core.job_queue import JobNotFoundError

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

        # 삭제 실행 (job_id 기반)
        await asyncio.to_thread(queue.queue.delete_job, job.id)

        logger.info(f"회의 삭제: {meeting_id} (job_id={job.id})")

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

    outputs/{meeting_id}/corrected.json 파일에서 발화 데이터를 읽어 반환한다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 회의 고유 식별자

    Returns:
        TranscriptResponse: 전사문 데이터

    Raises:
        HTTPException: 유효하지 않은 ID(400), 파일 미존재(404), 서버 에러(500)
    """
    _validate_meeting_id(meeting_id)
    outputs_dir = _get_outputs_dir(request)
    corrected_path = outputs_dir / meeting_id / "corrected.json"

    if not corrected_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"전사문을 찾을 수 없습니다: {meeting_id}",
        )

    try:
        import asyncio

        # PERF: mtime 기반 JSON 캐시 사용 (매 요청마다 파싱하지 않음)
        data = await asyncio.to_thread(_json_cache.get, corrected_path)

        utterances = [
            TranscriptUtteranceItem(
                text=u.get("text", ""),
                original_text=u.get("original_text", ""),
                speaker=u.get("speaker", "UNKNOWN"),
                start=u.get("start", 0.0),
                end=u.get("end", 0.0),
                was_corrected=u.get("was_corrected", False),
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

    summary_md_path = meeting_dir / "summary.md"
    summary_json_path = meeting_dir / "summary.json"

    if not summary_md_path.is_file() and not summary_json_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"회의록을 찾을 수 없습니다: {meeting_id}",
        )

    try:
        import asyncio

        markdown = ""
        meta: dict = {}

        # 마크다운 파일 읽기
        if summary_md_path.is_file():

            def _read_md() -> str:
                return summary_md_path.read_text(encoding="utf-8")

            markdown = await asyncio.to_thread(_read_md)

        # PERF: mtime 기반 JSON 캐시 사용 (매 요청마다 파싱하지 않음)
        if summary_json_path.is_file():
            meta = await asyncio.to_thread(_json_cache.get, summary_json_path)

            # JSON에 마크다운이 포함되어 있고 파일이 없는 경우 대체
            if not markdown and meta.get("markdown"):
                markdown = meta["markdown"]

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
