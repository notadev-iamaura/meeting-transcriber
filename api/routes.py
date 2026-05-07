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
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.dependencies import (
    get_job_queue as _get_job_queue,
)
from api.dependencies import (
    get_pipeline_manager as _get_pipeline_manager,
)
from api.dependencies import (
    get_recorder as _get_recorder,
)

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


# 회의 상세 API 는 api.routers.meeting_detail 로 분리한다.
# 아래 re-export 는 기존 `api.routes.MeetingItem`/`TranscriptResponse` 같은 접근을 보존한다.
from api.routers import meeting_detail as _meeting_detail_router  # noqa: E402

MeetingItem = _meeting_detail_router.MeetingItem
TranscriptReplaceRequest = _meeting_detail_router.TranscriptReplaceRequest
TranscriptReplaceResponse = _meeting_detail_router.TranscriptReplaceResponse
TranscriptResponse = _meeting_detail_router.TranscriptResponse
TranscriptUpdateRequest = _meeting_detail_router.TranscriptUpdateRequest
TranscriptUtteranceItem = _meeting_detail_router.TranscriptUtteranceItem
TranscriptUtterancePatch = _meeting_detail_router.TranscriptUtterancePatch
SummaryResponse = _meeting_detail_router.SummaryResponse
SummaryUpdateRequest = _meeting_detail_router.SummaryUpdateRequest
MeetingPatchRequest = _meeting_detail_router.MeetingPatchRequest
_AUDIO_MIME_BY_EXT = _meeting_detail_router._AUDIO_MIME_BY_EXT
_PLAYABLE_AUDIO_EXTS = _meeting_detail_router._PLAYABLE_AUDIO_EXTS
_atomic_write_json = _meeting_detail_router._atomic_write_json
_atomic_write_text = _meeting_detail_router._atomic_write_text
_find_meeting_audio_path = _meeting_detail_router._find_meeting_audio_path
_find_transcript_file = _meeting_detail_router._find_transcript_file
_parse_range_header = _meeting_detail_router._parse_range_header
cancel_meeting = _meeting_detail_router.cancel_meeting
delete_meeting = _meeting_detail_router.delete_meeting
get_meeting = _meeting_detail_router.get_meeting
get_meeting_audio = _meeting_detail_router.get_meeting_audio
get_pipeline_state = _meeting_detail_router.get_pipeline_state
get_summary = _meeting_detail_router.get_summary
get_transcript = _meeting_detail_router.get_transcript
patch_meeting = _meeting_detail_router.patch_meeting
re_transcribe_meeting = _meeting_detail_router.re_transcribe_meeting
replace_transcript_pattern = _meeting_detail_router.replace_transcript_pattern
retry_meeting = _meeting_detail_router.retry_meeting
summarize_meeting = _meeting_detail_router.summarize_meeting
transcribe_meeting = _meeting_detail_router.transcribe_meeting
update_summary = _meeting_detail_router.update_summary
update_transcript = _meeting_detail_router.update_transcript

router.include_router(_meeting_detail_router.router)


class MeetingsResponse(BaseModel):
    """회의 목록 응답 스키마.

    Attributes:
        meetings: 회의 목록
        total: 전체 회의 수
    """

    meetings: list[MeetingItem] = Field(default_factory=list)
    total: int = 0


class DashboardStatsResponse(BaseModel):
    """홈 화면 대시보드 통계 응답 스키마.

    Attributes:
        total_meetings: 전체 회의 수 (queue 의 모든 작업)
        this_week_meetings: 최근 7 일 내 등록된 회의 수
        queue_pending: 전사 처리 대기열 (queued) 합계 — 워커가 자동으로 처리할 항목
        untranscribed_recordings: 미전사 녹음 (recorded) 합계 — 사용자가 수동으로
            "전사 시작" 을 눌러야 진행되는 항목. 자동 처리되지 않는다.
        active_processing: 현재 진행 중 (recording, transcribing, diarizing,
            merging, embedding) 합계
        completed: 완료 상태 작업 수
        failed: 실패 상태 작업 수
        audio_input_dir: 오디오 입력 폴더 절대 경로 (UI 가 폴더 위치 안내에 사용)
    """

    total_meetings: int = 0
    this_week_meetings: int = 0
    queue_pending: int = 0
    untranscribed_recordings: int = 0
    active_processing: int = 0
    completed: int = 0
    failed: int = 0
    audio_input_dir: str = ""


class OpenFolderResponse(BaseModel):
    """폴더 열기 결과 응답 스키마.

    Attributes:
        opened: 성공 여부 (Finder 등 외부 프로그램 호출 성공 시 True)
        path: 실제로 열린 폴더 절대 경로
    """

    opened: bool = False
    path: str = ""


class UploadResponse(BaseModel):
    """오디오 업로드 결과 응답 스키마.

    Attributes:
        filename: 저장된 파일명 (충돌 방지로 변경된 경우 변경된 이름)
        path: 저장 후 절대 경로 (audio_input_dir 하위)
        size: 저장된 파일 크기 (바이트)
    """

    filename: str
    path: str
    size: int


# === 헬퍼 함수 ===


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


# 검색/채팅 API 는 api.routers.search_chat 으로 분리한다.
# 아래 re-export 는 기존 `api.routes.SearchResponse`/`ChatResponse` 같은 접근을 보존한다.
from api.routers import search_chat as _search_chat_router  # noqa: E402

ChatReferenceItem = _search_chat_router.ChatReferenceItem
ChatRequest = _search_chat_router.ChatRequest
ChatResponse = _search_chat_router.ChatResponse
SearchRequest = _search_chat_router.SearchRequest
SearchResponse = _search_chat_router.SearchResponse
SearchResultItem = _search_chat_router.SearchResultItem
_ChatEngineAdapter = _search_chat_router._ChatEngineAdapter
_build_chat_references = _search_chat_router._build_chat_references
_build_hybrid_chat_service = _search_chat_router._build_hybrid_chat_service
_serialize_router_verdict = _search_chat_router._serialize_router_verdict
_serialize_wiki_sources = _search_chat_router._serialize_wiki_sources
chat = _search_chat_router.chat
search = _search_chat_router.search

router.include_router(_search_chat_router.router)


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


# === 홈 화면 대시보드 / 시스템 액션 / 업로드 엔드포인트 ===


# 활성 (진행 중) 작업 상태 집합 — DashboardStats 와 status 엔드포인트가 공유.
_ACTIVE_JOB_STATUSES: frozenset[str] = frozenset(
    {"recording", "transcribing", "diarizing", "merging", "embedding"}
)
# 처리 대기 상태 — 워커가 자동으로 잡아갈 항목.
# recorded 는 사용자가 수동으로 전사를 시작해야 하므로 분리해서 집계한다
# (홈 카드에서 "처리 대기" vs "미전사 녹음" 으로 구분 표시).
_PENDING_JOB_STATUSES: frozenset[str] = frozenset({"queued"})
_UNTRANSCRIBED_JOB_STATUSES: frozenset[str] = frozenset({"recorded"})


@router.get("/dashboard/stats", response_model=DashboardStatsResponse)
async def get_dashboard_stats(request: Request) -> DashboardStatsResponse:
    """홈 화면 상단 대시보드용 통계를 반환한다.

    회의 큐를 한 번 조회한 뒤 메모리에서 상태별 집계와 이번 주 카운트를
    동시에 계산한다. 외부 I/O(메타 파일 등) 는 사용하지 않으므로 응답이
    빠르고 대시보드 폴링에 안전하다.

    Args:
        request: FastAPI Request

    Returns:
        DashboardStatsResponse: 대시보드 통계
    """
    from datetime import datetime, timedelta

    queue = _get_job_queue(request)
    config = _get_config(request)

    try:
        all_jobs = await queue.get_all_jobs()
    except Exception as e:
        logger.exception(f"대시보드 통계 조회 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"대시보드 통계 조회 중 오류가 발생했습니다: {e}",
        ) from e

    week_ago = datetime.now() - timedelta(days=7)

    total = len(all_jobs)
    this_week = 0
    pending = 0
    untranscribed = 0
    active = 0
    completed = 0
    failed = 0

    for job in all_jobs:
        status = getattr(job, "status", "")
        if status in _ACTIVE_JOB_STATUSES:
            active += 1
        elif status in _PENDING_JOB_STATUSES:
            pending += 1
        elif status in _UNTRANSCRIBED_JOB_STATUSES:
            untranscribed += 1
        elif status == "completed":
            completed += 1
        elif status == "failed":
            failed += 1

        # this_week 계산 — created_at 파싱 실패 시 무시 (안전한 기본값)
        created_at = getattr(job, "created_at", "")
        if created_at:
            try:
                created_dt = datetime.fromisoformat(created_at)
                if created_dt >= week_ago:
                    this_week += 1
            except (ValueError, TypeError):
                pass

    return DashboardStatsResponse(
        total_meetings=total,
        this_week_meetings=this_week,
        queue_pending=pending,
        untranscribed_recordings=untranscribed,
        active_processing=active,
        completed=completed,
        failed=failed,
        audio_input_dir=str(config.paths.resolved_audio_input_dir),
    )


@router.post("/system/open-audio-folder", response_model=OpenFolderResponse)
async def open_audio_folder(request: Request) -> OpenFolderResponse:
    """오디오 입력 폴더를 macOS Finder 로 연다.

    `~/.meeting-transcriber/audio_input` 폴더(설정에 따라 다를 수 있음)를
    Finder 의 `open` 명령으로 띄운다. 폴더가 없으면 자동 생성한다.
    macOS 가 아닌 환경에서는 500 을 반환한다 (이 앱 자체가 macOS 전용).

    Returns:
        OpenFolderResponse: 성공 여부와 실제 폴더 경로

    Raises:
        HTTPException 500: 폴더 생성 실패 또는 외부 명령 실행 실패
    """
    config = _get_config(request)
    folder = config.paths.resolved_audio_input_dir

    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"오디오 입력 폴더 생성 실패: {folder} — {e}")
        raise HTTPException(
            status_code=500,
            detail=f"폴더 생성 실패: {e}",
        ) from e

    if sys.platform != "darwin":
        # macOS 전용 앱이지만 비-macOS 에서 실행되어도 경로는 반환해 UI 가
        # "수동으로 이 경로를 열어 주세요" 안내를 표시할 수 있게 한다.
        return OpenFolderResponse(opened=False, path=str(folder))

    open_cmd = shutil.which("open")
    if not open_cmd:
        logger.error("`open` 명령을 찾을 수 없습니다 (PATH 설정을 확인하세요)")
        raise HTTPException(
            status_code=500,
            detail="`open` 명령을 찾을 수 없습니다.",
        )

    try:
        # asyncio.to_thread 로 블로킹 호출을 이벤트 루프에서 분리.
        # check=True 로 실패 시 CalledProcessError 가 raise.
        await asyncio.to_thread(
            subprocess.run,
            [open_cmd, str(folder)],
            check=True,
            capture_output=True,
            timeout=5.0,
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"폴더 열기 실패: returncode={e.returncode}, stderr={e.stderr!r}")
        raise HTTPException(
            status_code=500,
            detail=f"폴더 열기 실패: {e}",
        ) from e
    except subprocess.TimeoutExpired as e:
        logger.error(f"폴더 열기 타임아웃: {folder}")
        raise HTTPException(status_code=500, detail="폴더 열기가 응답하지 않습니다.") from e

    logger.info(f"오디오 입력 폴더 열기 성공: {folder}")
    return OpenFolderResponse(opened=True, path=str(folder))


# 업로드 제한 — 사용자가 한 회의를 통째로 업로드하는 시나리오를 고려해 2 GB.
# audio_input 폴더 자체가 회의 전용이라 더 큰 파일은 watcher 가 거부할 가능성이 높다.
_UPLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# 파일명에서 안전한 문자만 허용 (path traversal · 제어문자 차단).
# 한글/공백/대시/언더스코어/괄호/점은 허용하되 슬래시·백슬래시·NUL 은 거부.
_FILENAME_FORBIDDEN_PATTERN = re.compile(r"[\x00-\x1f/\\]")


def _sanitize_upload_filename(raw: str, supported_exts: set[str]) -> str:
    """업로드 파일명을 정제·검증한다.

    Args:
        raw: X-Filename 헤더로 전달된 원본 파일명 (URL 디코딩 이후).
        supported_exts: 허용 확장자 집합 (점 제외, 소문자, 예: {"wav", "mp3"}).

    Returns:
        정제된 파일명 (앞뒤 공백·점 제거).

    Raises:
        HTTPException 400: 빈 문자열, 금지 문자, 미지원 확장자.
    """
    cleaned = (raw or "").strip().strip(".")
    if not cleaned:
        raise HTTPException(status_code=400, detail="파일명이 비어 있습니다.")
    if _FILENAME_FORBIDDEN_PATTERN.search(cleaned):
        raise HTTPException(
            status_code=400,
            detail="파일명에 사용할 수 없는 문자가 포함되어 있습니다.",
        )
    # path traversal 추가 방어 — basename 만 사용
    basename = Path(cleaned).name
    if basename != cleaned:
        raise HTTPException(
            status_code=400,
            detail="파일명에 경로 구분자가 포함되어 있습니다.",
        )

    suffix = Path(basename).suffix.lower().lstrip(".")
    if suffix not in supported_exts:
        raise HTTPException(
            status_code=400,
            detail=(
                f"지원하지 않는 확장자입니다: .{suffix or '(없음)'} "
                f"(지원 형식: {sorted(supported_exts)})"
            ),
        )
    return basename


def _resolve_unique_upload_path(target_dir: Path, filename: str) -> Path:
    """동일한 파일명이 이미 존재하면 `name (1).ext`, `name (2).ext` 식으로 중복 회피.

    Args:
        target_dir: 저장 대상 디렉토리.
        filename: 정제 완료된 파일명.

    Returns:
        실제로 저장될 절대 경로 (중복 회피 적용 후).
    """
    candidate = target_dir / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    for i in range(1, 1000):
        alt = target_dir / f"{stem} ({i}){suffix}"
        if not alt.exists():
            return alt
    # 비현실적 시나리오 — 1000 개 같은 이름이 쌓여 있을 때만 도달
    raise HTTPException(status_code=409, detail="동일한 이름의 파일이 너무 많습니다.")


@router.post("/uploads", response_model=UploadResponse, status_code=201)
async def upload_audio(request: Request) -> UploadResponse:
    """프론트가 fetch 로 전송한 단일 오디오 파일을 audio_input 폴더에 저장한다.

    multipart/form-data 대신 Content-Type=application/octet-stream + X-Filename
    헤더를 사용한다. python-multipart 같은 추가 의존성을 피하면서, 프론트의
    File 객체를 그대로 fetch body 로 전달할 수 있어 단순하다.

    저장된 파일은 `core.watcher.FolderWatcher` 가 자동으로 감지하여 큐에
    `recorded` 상태로 등록한다. 즉 이 엔드포인트는 "큐 진입" 직접 책임을
    지지 않는다 (단일 책임).

    Headers:
        X-Filename: URL 인코딩된 원본 파일명. 예: "회의록 2026-04-29.m4a"
        Content-Length: 본문 크기 (선택, 사전 검증용).

    Returns:
        UploadResponse: 저장된 파일 정보.

    Raises:
        HTTPException 400: 헤더 누락, 잘못된 파일명, 미지원 확장자, 빈 본문.
        HTTPException 413: 본문이 _UPLOAD_MAX_BYTES 초과.
        HTTPException 500: 디스크 쓰기 실패.
    """
    from urllib.parse import unquote

    config = _get_config(request)
    audio_input_dir = config.paths.resolved_audio_input_dir
    supported_exts = {fmt.lower().lstrip(".") for fmt in config.audio.supported_input_formats}

    raw_filename = request.headers.get("x-filename")
    if not raw_filename:
        raise HTTPException(status_code=400, detail="X-Filename 헤더가 필요합니다.")

    try:
        decoded = unquote(raw_filename)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"X-Filename 헤더 디코딩 실패: {e}",
        ) from e

    filename = _sanitize_upload_filename(decoded, supported_exts)

    # 본문 크기 사전 검증 — Content-Length 가 있을 때만 (정확하지 않을 수 있음).
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            cl = int(content_length)
            if cl > _UPLOAD_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"파일이 너무 큽니다 (최대 {_UPLOAD_MAX_BYTES // (1024**3)} GB)",
                )
        except ValueError:
            # Content-Length 가 잘못된 경우는 본문 읽으며 실측에 의존
            pass

    # 디렉토리 보장
    try:
        await asyncio.to_thread(audio_input_dir.mkdir, parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"입력 폴더 생성 실패: {e}",
        ) from e

    target_path = _resolve_unique_upload_path(audio_input_dir, filename)

    # 본문을 스트리밍으로 받아 디스크에 직접 쓴다 — 대용량 파일 메모리 폭주 방지.
    written = 0
    tmp_path = target_path.with_suffix(target_path.suffix + ".part")
    try:
        # 동기 파일 I/O 를 to_thread 로 위임하지 않고 그대로 사용하는 이유:
        # FastAPI 의 request.stream() 은 비동기 제너레이터이므로 같은 코루틴에서
        # 청크별로 받아야 한다. write 는 OS 캐시로 빠르게 끝나며,
        # 청크 크기는 starlette 기본(64KB)이라 이벤트 루프 블로킹이 미미하다.
        with open(tmp_path, "wb") as fp:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > _UPLOAD_MAX_BYTES:
                    fp.close()
                    tmp_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"파일이 너무 큽니다 (최대 {_UPLOAD_MAX_BYTES // (1024**3)} GB)",
                    )
                fp.write(chunk)

        if written == 0:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="요청 본문이 비어 있습니다.")

        # 원자적 rename — watcher 가 .part 파일은 무시하고, 최종 이름으로 등장
        # 하는 순간을 새 파일 생성 이벤트로 감지한다.
        tmp_path.rename(target_path)
    except HTTPException:
        # tmp_path 정리는 이미 위에서 처리됨
        raise
    except OSError as e:
        # 미들 단계에서 깨진 .part 정리 (best-effort)
        tmp_path.unlink(missing_ok=True)
        logger.error(f"업로드 저장 실패: {target_path} — {e}")
        raise HTTPException(status_code=500, detail=f"파일 저장 실패: {e}") from e

    logger.info(
        f"오디오 업로드 완료: filename={target_path.name}, size={written}, path={target_path}"
    )
    return UploadResponse(
        filename=target_path.name,
        path=str(target_path),
        size=written,
    )


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


# === 통합 일괄 처리 (Bulk Actions) ===
#
# POST /api/meetings/batch 는 api.routers.meetings_batch 로 분리한다.
# 아래 import 는 기존 `api.routes.BatchActionResponse` 같은 접근을 보존한다.

from api.routers import meetings_batch as _meetings_batch  # noqa: E402

BatchActionRequest = _meetings_batch.BatchActionRequest
BatchActionResponse = _meetings_batch.BatchActionResponse
_classify_eligibility_sync = _meetings_batch._classify_eligibility_sync
_classify_meeting_for_batch = _meetings_batch._classify_meeting_for_batch
_collect_candidate_ids_sync = _meetings_batch._collect_candidate_ids_sync
_has_merge_checkpoint = _meetings_batch._has_merge_checkpoint
_has_summary_output = _meetings_batch._has_summary_output
_is_meeting_eligible = _meetings_batch._is_meeting_eligible
_resolve_audio_path = _meetings_batch._resolve_audio_path
batch_action = _meetings_batch.batch_action

router.include_router(_meetings_batch.router)


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
        is_aggregate: macOS Aggregate Device 여부 (본인 마이크 + 시스템 오디오 통합)
    """

    index: int
    name: str
    is_blackhole: bool = False
    is_aggregate: bool = False


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
                is_aggregate=dev.is_aggregate,
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

# 시스템 설정 API 는 api.routers.settings 로 분리한다.
# 아래 re-export 는 기존 `api.routes.SettingsResponse` 같은 접근을 보존한다.
from api.routers import settings as _settings_router  # noqa: E402

SettingsResponse = _settings_router.SettingsResponse
SettingsUpdateRequest = _settings_router.SettingsUpdateRequest
SettingsUpdateResponse = _settings_router.SettingsUpdateResponse
_ALLOWED_MLX_MODELS = _settings_router._ALLOWED_MLX_MODELS
_STT_LANGUAGE_PATTERN = _settings_router._STT_LANGUAGE_PATTERN
_AVAILABLE_MODELS = _settings_router._AVAILABLE_MODELS
_get_config_path = _settings_router._get_config_path
_replace_yaml_value = _settings_router._replace_yaml_value
get_settings = _settings_router.get_settings
update_settings = _settings_router.update_settings

router.include_router(_settings_router.router)


# 사용자 편집 가능 프롬프트/용어집 API 는 api.routers.user_settings 로 분리한다.
# 아래 re-export 는 기존 `api.routes.PromptsResponse` 같은 접근을 보존한다.
from api.routers import user_settings as _user_settings_router  # noqa: E402

PromptEntryPayload = _user_settings_router.PromptEntryPayload
PromptsPayload = _user_settings_router.PromptsPayload
PromptsResponse = _user_settings_router.PromptsResponse
PromptsUpdateRequest = _user_settings_router.PromptsUpdateRequest
VocabularyAddRequest = _user_settings_router.VocabularyAddRequest
VocabularyResponse = _user_settings_router.VocabularyResponse
VocabularyTermPayload = _user_settings_router.VocabularyTermPayload
VocabularyUpdateRequest = _user_settings_router.VocabularyUpdateRequest
_map_user_settings_error = _user_settings_router._map_user_settings_error
_prompts_to_payload = _user_settings_router._prompts_to_payload
_term_to_payload = _user_settings_router._term_to_payload
_user_settings = _user_settings_router._user_settings
add_vocabulary_term_endpoint = _user_settings_router.add_vocabulary_term_endpoint
delete_vocabulary_term_endpoint = _user_settings_router.delete_vocabulary_term_endpoint
get_prompts = _user_settings_router.get_prompts
get_vocabulary = _user_settings_router.get_vocabulary
reset_prompts = _user_settings_router.reset_prompts
reset_vocabulary_endpoint = _user_settings_router.reset_vocabulary_endpoint
update_prompts = _user_settings_router.update_prompts
update_vocabulary_term_endpoint = _user_settings_router.update_vocabulary_term_endpoint

router.include_router(_user_settings_router.router)


# ============================================================
# STT 모델 선택기 API (Phase 4)는 api.routers.stt_models 로 분리한다.
# 아래 re-export 는 기존 `api.routes.STTModelInfo` 같은 테스트/외부 접근을 보존한다.
from api.routers import stt_models as _stt_models  # noqa: E402

STTImportRequest = _stt_models.STTImportRequest
STTImportResponse = _stt_models.STTImportResponse
STTManualDownloadFile = _stt_models.STTManualDownloadFile
STTManualDownloadInfo = _stt_models.STTManualDownloadInfo
STTModelInfo = _stt_models.STTModelInfo
STTModelsResponse = _stt_models.STTModelsResponse
_is_active_stt_model = _stt_models._is_active_stt_model
activate_stt_model = _stt_models.activate_stt_model
download_stt_model = _stt_models.download_stt_model
download_stt_model_direct = _stt_models.download_stt_model_direct
get_stt_download_status = _stt_models.get_stt_download_status
get_stt_manual_download_info = _stt_models.get_stt_manual_download_info
import_stt_manual = _stt_models.import_stt_manual
list_stt_models = _stt_models.list_stt_models

router.include_router(_stt_models.router)


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


# LLM Wiki API 는 api.routers.wiki 로 분리한다.
# 아래 re-export 는 기존 `api.routes.WikiPagesResponse` 같은 접근을 보존한다.
from api.routers import wiki as _wiki_router  # noqa: E402

WikiBackfillErrorItem = _wiki_router.WikiBackfillErrorItem
WikiBackfillRequest = _wiki_router.WikiBackfillRequest
WikiBackfillStartedResponse = _wiki_router.WikiBackfillStartedResponse
WikiBackfillStatusResponse = _wiki_router.WikiBackfillStatusResponse
WikiCitationItem = _wiki_router.WikiCitationItem
WikiHealthResponse = _wiki_router.WikiHealthResponse
WikiPageDetail = _wiki_router.WikiPageDetail
WikiPageItem = _wiki_router.WikiPageItem
WikiPagesResponse = _wiki_router.WikiPagesResponse
WikiSearchResponse = _wiki_router.WikiSearchResponse
WikiSearchResult = _wiki_router.WikiSearchResult
_WIKI_PAGE_TYPE_TO_DIRNAME = _wiki_router._WIKI_PAGE_TYPE_TO_DIRNAME
_WIKI_SEARCH_DEFAULT_LIMIT = _wiki_router._WIKI_SEARCH_DEFAULT_LIMIT
_WIKI_SEARCH_MAX_LIMIT = _wiki_router._WIKI_SEARCH_MAX_LIMIT
_WIKI_SEARCH_SNIPPET_AFTER = _wiki_router._WIKI_SEARCH_SNIPPET_AFTER
_WIKI_SEARCH_SNIPPET_BEFORE = _wiki_router._WIKI_SEARCH_SNIPPET_BEFORE
_extract_title_from_markdown = _wiki_router._extract_title_from_markdown
_get_raw_job_queue = _wiki_router._get_raw_job_queue
_make_search_snippet = _wiki_router._make_search_snippet
_resolve_wiki_root = _wiki_router._resolve_wiki_root
_wiki_backfill_jobs = _wiki_router._wiki_backfill_jobs
_wiki_backfill_lock = _wiki_router._wiki_backfill_lock
cancel_wiki_backfill = _wiki_router.cancel_wiki_backfill
get_wiki_backfill_status = _wiki_router.get_wiki_backfill_status
get_wiki_health = _wiki_router.get_wiki_health
get_wiki_page_detail = _wiki_router.get_wiki_page_detail
list_wiki_pages = _wiki_router.list_wiki_pages
search_wiki = _wiki_router.search_wiki
start_wiki_backfill = _wiki_router.start_wiki_backfill

router.include_router(_wiki_router.router)


# RAG 검색 인덱스 백필 API 는 api.routers.reindex 로 분리한다.
# 아래 re-export 는 기존 `api.routes.ReindexResponse` 같은 접근을 보존한다.
from api.routers import reindex as _reindex_router  # noqa: E402

ReindexAllResponse = _reindex_router.ReindexAllResponse
ReindexResponse = _reindex_router.ReindexResponse
ReindexStatusResponse = _reindex_router.ReindexStatusResponse
_count_chunks_for_meeting = _reindex_router._count_chunks_for_meeting
_get_chroma_collection_for_status = _reindex_router._get_chroma_collection_for_status
_reindex_meeting = _reindex_router._reindex_meeting
_start_reindex_all = _reindex_router._start_reindex_all
get_index_status = _reindex_router.get_index_status
reindex_all = _reindex_router.reindex_all
reindex_meeting = _reindex_router.reindex_meeting

router.include_router(_reindex_router.router)
