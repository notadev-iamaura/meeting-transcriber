"""시스템 상태, 리소스, 대시보드 API 라우터."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.dependencies import get_job_queue as _get_job_queue

logger = logging.getLogger(__name__)

router = APIRouter()


class SystemResourcesResponse(BaseModel):
    """시스템 리소스 상태 응답 스키마.

    Attributes:
        ram_used_gb: 사용 중인 RAM (GB)
        ram_total_gb: 전체 RAM (GB)
        ram_percent: RAM 사용률 (%)
        cpu_percent: CPU 사용률 (%)
        loaded_model: 현재 로드된 모델명 (없으면 None)
        native_cleanup_pending: timeout/cancel된 native worker 정리 대기 여부
        native_cleanup_model_name: 정리 대기 중인 모델명
        native_cleanup_pending_workers: 아직 종료되지 않은 native worker 수
        native_cleanup_started_at: deferred cleanup 시작 Unix 시각
    """

    ram_used_gb: float
    ram_total_gb: float
    ram_percent: float
    cpu_percent: float
    loaded_model: str | None = None
    native_cleanup_pending: bool = False
    native_cleanup_model_name: str | None = None
    native_cleanup_pending_workers: int = 0
    native_cleanup_started_at: float | None = None


class AudioInputScanStatusResponse(BaseModel):
    """경로·stderr를 제외한 오디오 입력 감사 집계."""

    phase: str = "unavailable"
    mode: str = ""
    candidate_count: int = 0
    registered_count: int = 0
    already_registered_count: int = 0
    deferred_count: int = 0
    preserved_count: int = 0
    failed_count: int = 0
    conflict_count: int = 0
    registered_meeting_ids: list[str] = Field(default_factory=list)
    recent_registered_meeting_ids: list[str] = Field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""


class StatusResponse(BaseModel):
    """시스템 상태와 작업 큐·입력 감사 현황 응답 스키마."""

    status: str = "ok"
    queue_summary: dict[str, int] = Field(default_factory=dict)
    active_jobs: int = 0
    total_jobs: int = 0
    is_recording: bool = False
    recording_duration: float = 0.0
    startup_scan_status: str = "unknown"
    background_runtime_ready: bool = False
    audio_input_scan: AudioInputScanStatusResponse = Field(
        default_factory=AudioInputScanStatusResponse
    )


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


def _get_config(request: Request) -> Any:
    """app.state 에서 AppConfig 를 가져온다."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="서버 설정이 초기화되지 않았습니다.",
        )
    return config


def _get_routes_compat_attr(name: str, fallback: Any) -> Any:
    """기존 api.routes monkeypatch 경로가 있으면 우선 사용한다."""
    routes_module = sys.modules.get("api.routes")
    if routes_module is None:
        return fallback
    return getattr(routes_module, name, fallback)


def _audio_scan_status(request: Request) -> AudioInputScanStatusResponse:
    """FolderWatcher의 공개 가능한 감사 스냅샷만 API 모델로 복사한다."""
    watcher = getattr(request.app.state, "folder_watcher", None)
    report = getattr(watcher, "scan_report", None) if watcher is not None else None
    if report is None:
        return AudioInputScanStatusResponse()
    return AudioInputScanStatusResponse(
        phase=str(getattr(report, "phase", "unavailable")),
        mode=str(getattr(report, "mode", "")),
        candidate_count=int(getattr(report, "candidate_count", 0)),
        registered_count=int(getattr(report, "registered_count", 0)),
        already_registered_count=int(getattr(report, "already_registered_count", 0)),
        deferred_count=int(getattr(report, "deferred_count", 0)),
        preserved_count=int(getattr(report, "preserved_count", 0)),
        failed_count=int(getattr(report, "failed_count", 0)),
        conflict_count=int(getattr(report, "conflict_count", 0)),
        registered_meeting_ids=list(getattr(report, "registered_meeting_ids", ())),
        recent_registered_meeting_ids=list(getattr(report, "recent_registered_meeting_ids", ())),
        started_at=str(getattr(report, "started_at", "")),
        finished_at=str(getattr(report, "finished_at", "")),
    )


async def _get_reconciled_jobs(queue: Any, config: Any) -> list[Any]:
    """시스템 집계 전에 회의 상태 불일치를 복구한 Job 목록을 반환한다."""
    from api.routers.meeting_detail import reconcile_job_state_for_response

    raw_queue = getattr(queue, "queue", queue)
    all_jobs = await queue.get_all_jobs()
    reconciled: list[Any] = []
    for job in all_jobs:
        job, _pipeline_state, _status_detail = await reconcile_job_state_for_response(
            raw_queue,
            config,
            job,
            include_pipeline_state=False,
        )
        reconciled.append(job)
    return reconciled


def _count_jobs_by_status(jobs: list[Any]) -> dict[str, int]:
    """Job 목록에서 상태별 개수를 집계한다."""
    summary: dict[str, int] = {}
    for job in jobs:
        status = str(getattr(job, "status", ""))
        if not status:
            continue
        summary[status] = summary.get(status, 0) + 1
    return summary


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
    config = _get_config(request)

    try:
        all_jobs = await _get_reconciled_jobs(queue, config)
        summary = _count_jobs_by_status(all_jobs)

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
            startup_scan_status=str(getattr(request.app.state, "startup_scan_status", "unknown")),
            background_runtime_ready=bool(
                getattr(request.app.state, "background_runtime_ready", False)
            ),
            audio_input_scan=_audio_scan_status(request),
        )
    except Exception as e:
        logger.exception(f"상태 조회 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"상태 조회 중 오류가 발생했습니다: {e}",
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
    native_cleanup_pending = False
    native_cleanup_model_name = None
    native_cleanup_pending_workers = 0
    native_cleanup_started_at = None
    if model_manager is not None:
        get_status = getattr(model_manager, "get_status", None)
        model_status = get_status() if callable(get_status) else None
        if isinstance(model_status, dict):
            loaded_model = model_status.get("current_model_name")
            native_cleanup_pending = bool(model_status.get("native_cleanup_pending", False))
            native_cleanup_model_name = model_status.get("native_cleanup_model_name")
            native_cleanup_pending_workers = int(
                model_status.get("native_cleanup_pending_workers", 0)
            )
            native_cleanup_started_at = model_status.get("native_cleanup_started_at")
        else:
            loaded_model = getattr(model_manager, "current_model_name", None)

    return SystemResourcesResponse(
        ram_used_gb=round(mem.used / (1024**3), 2),
        ram_total_gb=round(mem.total / (1024**3), 2),
        ram_percent=round(mem.percent, 1),
        cpu_percent=round(cpu, 1),
        loaded_model=loaded_model,
        native_cleanup_pending=native_cleanup_pending,
        native_cleanup_model_name=native_cleanup_model_name,
        native_cleanup_pending_workers=native_cleanup_pending_workers,
        native_cleanup_started_at=native_cleanup_started_at,
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
        all_jobs = await _get_reconciled_jobs(queue, config)
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


@router.post(
    "/system/audio-input/recover",
    response_model=AudioInputScanStatusResponse,
)
async def recover_unregistered_audio(
    request: Request,
    recent_days: Annotated[int, Query(ge=1, le=365)] = 7,
) -> AudioInputScanStatusResponse:
    """audio_input의 DB 미등록 파일만 비파괴 방식으로 복구한다.

    원본 이동·삭제·quarantine과 기존 row 변경은 하지 않는다. 응답의
    ``recent_registered_meeting_ids``는 source mtime 기준이므로, 운영자가
    해당 ID에만 명시적 로컬 전사 요청을 보낼 수 있다.
    """
    from api.openai_settings_guard import get_openai_settings_mutation_lock
    from api.routers.transcription_models import require_loopback_server

    # 장시간 복구 lock을 기다리기 전에 명백한 비-loopback 요청을 즉시 거부한다.
    # lock 획득 뒤 최신 runtime config로 다시 검사해 설정 경합도 닫는다.
    require_loopback_server(_get_config(request), request, feature_label="오디오 복구")

    # 설정에서 auto-processing을 켜거나 수동 run-now를 호출하는 요청과
    # 복구 전체를 직렬화한다. 그렇지 않으면 복구 도중 생긴 recorded row가
    # 사용자가 선택한 recent/local 범위를 우회해 전역 provider로 큐잉될 수 있다.
    async with get_openai_settings_mutation_lock(request):
        config = _get_config(request)
        require_loopback_server(config, request, feature_label="오디오 복구")

        auto_processing = getattr(config, "auto_processing", None)
        scheduler = getattr(request.app.state, "auto_processing_scheduler", None)
        if getattr(auto_processing, "enabled", False) is True or bool(
            getattr(scheduler, "is_processing", False)
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "비파괴 복구 중 자동 전사 경합을 막기 위해 "
                    "auto_processing을 비활성화한 뒤 다시 시도하세요."
                ),
            )

        watcher = getattr(request.app.state, "folder_watcher", None)
        if watcher is None:
            raise HTTPException(
                status_code=503,
                detail="FolderWatcher가 초기화되지 않아 오디오 복구를 실행할 수 없습니다.",
            )
        if bool(getattr(watcher, "is_scan_running", False)):
            raise HTTPException(
                status_code=409,
                detail=(
                    "오디오 입력 스캔이 이미 실행 중입니다. /api/status에서 완료를 확인하세요."
                ),
            )

        try:
            await watcher.recover_unregistered_files(recent_days=recent_days)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"오디오 입력 복구 실패: {e}")
            raise HTTPException(
                status_code=500,
                detail="오디오 입력 복구 중 오류가 발생했습니다. 원본 파일은 보존되었습니다.",
            ) from e

    return _audio_scan_status(request)


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

    compat_sys = _get_routes_compat_attr("sys", sys)
    if compat_sys.platform != "darwin":
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
