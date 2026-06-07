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
    scheduler = getattr(request.app.state, "auto_processing_scheduler", None)
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
    result = await scheduler.run_once()
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
            "failed": result.failed,
            "meeting_ids": result.meeting_ids,
            "errors": result.errors,
        },
    }
