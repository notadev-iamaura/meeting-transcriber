"""자동 전사/요약 스케줄러."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Any

from config import AppConfig
from core.auto_processing import AutoProcessingResult, AutoProcessingRunner

logger = logging.getLogger(__name__)


class AutoProcessingScheduler:
    """설정된 매일 시각에 자동 전사/요약을 실행한다."""

    def __init__(
        self,
        *,
        config: AppConfig,
        job_queue: Any,
        pipeline: Any,
    ) -> None:
        self._config = config
        self._job_queue = job_queue
        self._pipeline = pipeline
        self._task: asyncio.Task[None] | None = None
        self._run_lock = asyncio.Lock()
        self.last_started_at: datetime | None = None
        self.last_completed_at: datetime | None = None
        self.last_result: AutoProcessingResult | None = None
        self.last_error: str | None = None

    @property
    def is_running(self) -> bool:
        """스케줄러 태스크가 실행 중인지 반환한다."""
        return self._task is not None and not self._task.done()

    @property
    def is_processing(self) -> bool:
        """자동 처리 실행이 진행 중인지 반환한다."""
        return self._run_lock.locked()

    async def start(self) -> None:
        """자동 처리가 활성화되어 있으면 백그라운드 태스크를 시작한다."""
        if not self._config.auto_processing.enabled:
            logger.info("AutoProcessingScheduler 비활성화 (auto_processing.enabled=false)")
            return
        if self.is_running:
            return
        self._task = asyncio.create_task(self._run_loop(), name="auto-processing-scheduler")
        logger.info(
            "AutoProcessingScheduler 시작: run_at=%s, recent_hours=%sh, action=%s",
            self._config.auto_processing.run_at,
            self._config.auto_processing.recent_hours,
            self._config.auto_processing.action,
        )

    async def stop(self) -> None:
        """백그라운드 태스크를 중지한다."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        logger.info("AutoProcessingScheduler 정지 완료")

    async def update_config(self, config: AppConfig) -> None:
        """새 설정을 반영하고 필요하면 스케줄러를 재시작한다."""
        was_running = self.is_running
        self._config = config
        if was_running:
            await self.stop()
        await self.start()

    async def run_once(self) -> AutoProcessingResult:
        """자동 처리를 즉시 1회 실행한다."""
        async with self._run_lock:
            self.last_started_at = datetime.now()
            self.last_error = None
            try:
                runner = AutoProcessingRunner(
                    config=self._config,
                    job_queue=self._job_queue,
                    pipeline=self._pipeline,
                )
                result = await runner.run(
                    action=self._config.auto_processing.action,
                    recent_hours=self._config.auto_processing.recent_hours,
                )
                self.last_result = result
                self.last_completed_at = datetime.now()
                return result
            except Exception as exc:
                self.last_error = str(exc)
                self.last_completed_at = datetime.now()
                logger.exception("자동 전사/요약 실행 실패: %s", exc)
                raise

    def get_status(self) -> dict[str, Any]:
        """상태 조회용 직렬화 가능한 딕셔너리를 반환한다."""
        result = self.last_result
        return {
            "enabled": self._config.auto_processing.enabled,
            "running": self.is_running,
            "processing": self.is_processing,
            "run_at": self._config.auto_processing.run_at,
            "recent_hours": self._config.auto_processing.recent_hours,
            "action": self._config.auto_processing.action,
            "run_on_startup_if_missed": self._config.auto_processing.run_on_startup_if_missed,
            "next_run_at": self._next_run_at().isoformat(),
            "last_started_at": self.last_started_at.isoformat()
            if self.last_started_at is not None
            else None,
            "last_completed_at": self.last_completed_at.isoformat()
            if self.last_completed_at is not None
            else None,
            "last_error": self.last_error,
            "last_result": None
            if result is None
            else {
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

    async def _run_loop(self) -> None:
        """설정된 매일 시각까지 대기 후 자동 처리를 반복한다."""
        try:
            if self._config.auto_processing.run_on_startup_if_missed:
                now = datetime.now()
                if now.time() >= self._parse_run_at():
                    await self._run_once_safely()

            while True:
                await asyncio.sleep(self.seconds_until_next_run())
                await self._run_once_safely()
        except asyncio.CancelledError:
            raise

    async def _run_once_safely(self) -> None:
        """스케줄러 루프용 1회 실행 래퍼. 실패해도 다음 일정을 유지한다."""
        try:
            await self.run_once()
        except Exception:
            logger.warning("자동 전사/요약 실패 후 다음 일정을 계속 대기합니다.")

    def seconds_until_next_run(self, now: datetime | None = None) -> float:
        """다음 실행 시각까지 남은 초를 반환한다."""
        now = now or datetime.now()
        next_run = self._next_run_at(now)
        return max(0.0, (next_run - now).total_seconds())

    def _next_run_at(self, now: datetime | None = None) -> datetime:
        """다음 실행 시각을 계산한다."""
        now = now or datetime.now()
        run_time = self._parse_run_at()
        next_run = datetime.combine(now.date(), run_time)
        if next_run <= now:
            next_run += timedelta(days=1)
        return next_run

    def _parse_run_at(self) -> time:
        """HH:MM 설정값을 time 객체로 변환한다."""
        hour_s, minute_s = self._config.auto_processing.run_at.split(":", 1)
        return time(hour=int(hour_s), minute=int(minute_s))
