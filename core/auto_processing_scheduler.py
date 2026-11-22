"""자동 전사/요약 스케줄러."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timedelta
from typing import Any

from config import AppConfig
from core.auto_processing import AutoProcessingResult, AutoProcessingRunner
from core.io_utils import atomic_write_json_pinned, read_text_no_follow
from core.nightly_window import NightlyWindowClosed, current_window, run_during_window

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
            return await self._execute_run_once(self._config)

    async def reserve_run_once(self) -> asyncio.Task[AutoProcessingResult] | None:
        """이 호출 소유의 자동 처리 실행을 non-blocking 방식으로 예약한다.

        이미 다른 수동/예약 실행이 lock을 소유하면 ``None``을 반환한다. lock이
        비어 있으면 현재 이벤트 루프 turn에서 즉시 선점한 뒤, 설정 스냅샷을
        사용하는 task에 lock 해제 책임을 넘긴다.
        """
        if self._run_lock.locked():
            return None
        await self._run_lock.acquire()
        config_snapshot = self._config.model_copy(
            update={"auto_processing": self._config.auto_processing.model_copy()}
        )
        started = False

        async def _reserved_wrapper() -> AutoProcessingResult:
            nonlocal started
            started = True
            return await self._run_reserved_once(config_snapshot)

        def _release_if_never_started(_task: asyncio.Task[AutoProcessingResult]) -> None:
            # create_task 직후 요청이 취소되면 coroutine 본문과 finally가 한 번도
            # 실행되지 않는다. 그 경우에만 예약자가 아직 가진 lock을 회수한다.
            if not started and self._run_lock.locked():
                self._run_lock.release()

        try:
            task = asyncio.create_task(
                _reserved_wrapper(),
                name="auto-processing-reserved-run",
            )
            task.add_done_callback(_release_if_never_started)
            return task
        except Exception:
            self._run_lock.release()
            raise

    async def _run_reserved_once(self, config_snapshot: AppConfig) -> AutoProcessingResult:
        """예약된 실행을 완료하고 어떤 종료 경로에서도 run lock을 해제한다."""
        try:
            return await self._execute_run_once(config_snapshot)
        finally:
            self._run_lock.release()

    async def _execute_run_once(
        self, config_snapshot: AppConfig, *, scheduled: bool = False
    ) -> AutoProcessingResult:
        """이미 선점된 run lock 아래에서 설정 스냅샷으로 배치를 실행한다."""
        self.last_started_at = datetime.now()
        self.last_error = None
        try:
            runner = AutoProcessingRunner(
                config=config_snapshot,
                job_queue=self._job_queue,
                pipeline=self._pipeline,
            )

            async def run() -> AutoProcessingResult:
                """수동 실행과 예약 실행을 같은 분류기로 처리한다."""
                return await runner.run(
                    action=config_snapshot.auto_processing.action,
                    recent_hours=config_snapshot.auto_processing.recent_hours,
                    scheduled=scheduled,
                )

            result = await run_during_window(run, self._seconds_left) if scheduled else await run()
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
            "stop_at": self._config.auto_processing.stop_at,
            "recent_hours": self._config.auto_processing.recent_hours,
            "action": self._config.auto_processing.action,
            "max_items_per_run": self._config.auto_processing.max_items_per_run,
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
                "skipped_by_limit": result.skipped_by_limit,
                "skipped_reasons": result.skipped_reasons,
                "failed": result.failed,
                "meeting_ids": result.meeting_ids,
                "errors": result.errors,
            },
        }

    async def _run_loop(self) -> None:
        """설정된 매일 시각까지 대기 후 자동 처리를 반복한다."""
        auto = self._config.auto_processing
        window = current_window(auto, datetime.now())
        last_window = window[0] if window and not auto.run_on_startup_if_missed else None
        while True:
            now = datetime.now()
            window = current_window(auto, now)
            if window and last_window != window[0]:
                try:
                    if await self._run_scheduled_window(window[0]):
                        last_window = window[0]
                except NightlyWindowClosed:
                    logger.info("야간 처리 시간이 끝나 예약 대상 확인을 중단했습니다.")
                except Exception:
                    logger.exception("야간 처리 예약 확인 실패")
            # 시스템 절전 동안 asyncio의 단조 시간이 정지하는 Mac에서도
            # 깨어난 다음 벽시계를 기준으로 현재 실행 창을 다시 확인한다.
            await asyncio.sleep(min(60.0, self.seconds_until_next_run()))

    def _seconds_left(self) -> float:
        """현재 예약 창에 남은 실제 시각을 계산한다."""
        now = datetime.now()
        window = current_window(self._config.auto_processing, now)
        return (window[1] - now).total_seconds() if window else 0.0

    async def _run_scheduled_window(self, start: datetime) -> bool:
        """완료한 창은 재시작 후에도 중복 예약하지 않는다. 중단되면 큐 상태에서 재개한다."""
        marker = self._config.paths.resolved_base_dir / "nightly_processing.json"
        key = start.isoformat()
        async with self._run_lock:
            try:
                saved = json.loads(await asyncio.to_thread(read_text_no_follow, marker))
            except FileNotFoundError:
                saved = {}
            if saved.get("start") == key:
                return True
            window = current_window(self._config.auto_processing, datetime.now())
            if not window or window[0] != start:
                return False
            result = await self._execute_run_once(self._config, scheduled=True)
            if result.errors:
                return False
            await asyncio.to_thread(atomic_write_json_pinned, marker, {"start": key}, backup=False)
            return True

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
