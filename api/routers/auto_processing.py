"""자동 전사/요약 스케줄러 API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/auto-processing/status")
async def get_auto_processing_status(request: Request) -> dict[str, Any]:
    """자동 전사/요약 스케줄러 상태를 반환한다."""
    scheduler = getattr(request.app.state, "auto_processing_scheduler", None)
    config = getattr(request.app.state, "config", None)
    if scheduler is not None:
        return scheduler.get_status()  # type: ignore[no-any-return]  # 동적 scheduler(Any) 반환
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")
    return {
        "enabled": config.auto_processing.enabled,
        "running": False,
        "processing": False,
        "run_at": config.auto_processing.run_at,
        "recent_hours": config.auto_processing.recent_hours,
        "action": config.auto_processing.action,
        "max_items_per_run": config.auto_processing.max_items_per_run,
        "run_on_startup_if_missed": config.auto_processing.run_on_startup_if_missed,
        "next_run_at": None,
        "last_started_at": None,
        "last_completed_at": None,
        "last_error": None,
        "last_result": None,
    }


@router.post("/auto-processing/run-now")
async def run_auto_processing_now(request: Request) -> dict[str, Any]:
    """자동 전사/요약을 즉시 1회 실행한다."""
    from api.openai_settings_guard import get_openai_settings_mutation_lock

    watcher = getattr(request.app.state, "folder_watcher", None)
    if bool(getattr(watcher, "is_scan_running", False)):
        raise HTTPException(
            status_code=409,
            detail="오디오 입력 스캔 중에는 자동 처리를 시작할 수 없습니다.",
        )

    async with get_openai_settings_mutation_lock(request):
        # precheck와 lock 획득 사이에 복구가 시작되는 경합도 다시 확인한다.
        watcher = getattr(request.app.state, "folder_watcher", None)
        if bool(getattr(watcher, "is_scan_running", False)):
            raise HTTPException(
                status_code=409,
                detail="오디오 입력 스캔 중에는 자동 처리를 시작할 수 없습니다.",
            )

        scheduler = getattr(request.app.state, "auto_processing_scheduler", None)
        config = getattr(request.app.state, "config", None)
        if scheduler is None:
            raise HTTPException(
                status_code=503,
                detail="자동 전사/요약 스케줄러가 초기화되지 않았습니다.",
            )
        if getattr(scheduler, "is_processing", False):
            raise HTTPException(
                status_code=409,
                detail="자동 전사/요약이 이미 실행 중입니다.",
            )
        if config is not None and getattr(config.auto_processing, "action", "full") in {
            "transcribe",
            "full",
        }:
            # recorded 작업은 취소 전 pipeline_state에 고정된 OpenAI provider를 재개할
            # 수 있다. 현재 기본 provider만 검사하면 local 전환으로 guard가 우회된다.
            from api.routers.transcription_models import require_loopback_server

            require_loopback_server(config, request)
        # 이 요청 소유의 scheduler run lock만 선점하고 실제 배치는 공용 lock
        # 밖에서 기다린다. background scheduler와의 경합을 is_processing만으로
        # 추측하면 다른 실행을 내 것으로 오인할 수 있으므로 예약 API를 강제한다.
        reserve_run_once = getattr(scheduler, "reserve_run_once", None)
        if not callable(reserve_run_once):
            raise HTTPException(
                status_code=503,
                detail="자동 처리 예약 기능이 초기화되지 않았습니다.",
            )
        run_task = await reserve_run_once()
        if run_task is None:
            raise HTTPException(
                status_code=409,
                detail="자동 전사/요약이 이미 실행 중입니다.",
            )

    result = await run_task
    return {
        "status": "ok",
        "result": {
            "action": result.action,
            "recent_hours": result.recent_hours,
            "matched": result.matched,
            "queued": result.queued,
            "transcribed": result.transcribed,
            "summarized": result.summarized,
            "skipped": result.skipped,
            "skipped_by_limit": result.skipped_by_limit,
            "failed": result.failed,
            "meeting_ids": result.meeting_ids,
            "errors": result.errors,
        },
    }
