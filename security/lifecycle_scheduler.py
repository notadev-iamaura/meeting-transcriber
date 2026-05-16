"""라이프사이클 자동 실행 스케줄러.

삭제/압축 작업은 ``security.lifecycle.LifecycleManager`` 에 위임하고, 이 모듈은
서버 생명주기에 맞춘 주기 실행과 설정 변경 반영만 담당한다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from config import AppConfig
from security.lifecycle import LifecycleManager, LifecycleResult

logger = logging.getLogger(__name__)


class LifecycleScheduler:
    """데이터 라이프사이클 관리를 주기적으로 실행한다.

    ``LifecycleConfig.enabled`` 가 false 이면 태스크를 만들지 않는다. 설정 화면에서
    값을 바꾸면 ``update_config`` 로 현재 태스크를 재시작해 새 주기를 즉시 반영한다.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._task: asyncio.Task[None] | None = None
        self._run_lock = asyncio.Lock()
        self.last_started_at: datetime | None = None
        self.last_completed_at: datetime | None = None
        self.last_result: LifecycleResult | None = None
        self.last_error: str | None = None
        self._run_on_startup_for_current_task = False

    @property
    def is_running(self) -> bool:
        """스케줄러 태스크가 실행 중인지 반환한다."""
        return self._task is not None and not self._task.done()

    @property
    def interval_seconds(self) -> int:
        """현재 설정 기준 점검 주기를 초 단위로 반환한다."""
        return self._config.lifecycle.interval_hours * 3600

    async def start(self, *, run_on_startup: bool | None = None) -> None:
        """설정이 활성화되어 있으면 백그라운드 태스크를 시작한다."""
        if not self._config.lifecycle.enabled:
            logger.info("LifecycleScheduler 비활성화 (lifecycle.enabled=false)")
            return
        if self.is_running:
            return
        self._run_on_startup_for_current_task = (
            self._config.lifecycle.run_on_startup if run_on_startup is None else run_on_startup
        )
        self._task = asyncio.create_task(self._run_loop(), name="lifecycle-scheduler")
        logger.info(
            f"LifecycleScheduler 시작: interval={self._config.lifecycle.interval_hours}h, "
            f"run_on_startup={self._run_on_startup_for_current_task}"
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
        logger.info("LifecycleScheduler 정지 완료")

    async def update_config(self, config: AppConfig) -> None:
        """새 설정을 반영하고 필요하면 태스크를 재시작한다."""
        was_running = self.is_running
        self._config = config
        if was_running:
            await self.stop()
        # 설정 저장 직후에는 데이터 삭제가 즉시 발생하지 않게 한다. run_on_startup은
        # 실제 서버 startup 경로에서만 적용된다.
        await self.start(run_on_startup=False)

    async def run_once(self) -> LifecycleResult:
        """라이프사이클 관리를 1회 실행한다."""
        async with self._run_lock:
            self.last_started_at = datetime.now()
            self.last_error = None
            try:
                manager = LifecycleManager(self._config)
                result = await manager.run_async()
                self.last_result = result
                self.last_completed_at = datetime.now()
                return result
            except Exception as exc:
                self.last_error = str(exc)
                self.last_completed_at = datetime.now()
                logger.exception(f"라이프사이클 자동 실행 실패: {exc}")
                raise

    def get_status(self) -> dict[str, Any]:
        """상태 조회용 직렬화 가능한 딕셔너리를 반환한다."""
        result = self.last_result
        return {
            "enabled": self._config.lifecycle.enabled,
            "running": self.is_running,
            "interval_hours": self._config.lifecycle.interval_hours,
            "run_on_startup": self._config.lifecycle.run_on_startup,
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
                "total_scanned": result.total_scanned,
                "compressed": result.compressed,
                "deleted": result.deleted,
                "skipped": result.skipped,
                "errors": result.errors,
                "bytes_saved": result.bytes_saved,
            },
        }

    async def _run_loop(self) -> None:
        """설정된 주기로 라이프사이클 관리를 반복 실행한다."""
        try:
            if self._run_on_startup_for_current_task:
                await self._run_once_safely()

            while True:
                await asyncio.sleep(self.interval_seconds)
                await self._run_once_safely()
        except asyncio.CancelledError:
            raise

    async def _run_once_safely(self) -> None:
        """스케줄러 루프용 1회 실행 래퍼. 실패해도 다음 주기를 유지한다."""
        try:
            await self.run_once()
        except Exception:
            logger.warning("라이프사이클 실행 실패 후 다음 주기를 계속 대기합니다.")
