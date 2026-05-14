"""시스템 상태, 리소스, 대시보드 API 라우터."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from typing import Any

from fastapi import APIRouter, HTTPException, Request
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
