"""LifecycleScheduler 테스트."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from config import AppConfig, LifecycleConfig, PathsConfig
from security.lifecycle import LifecycleResult
from security.lifecycle_scheduler import LifecycleScheduler


def _make_config(tmp_path: Path, lifecycle: LifecycleConfig) -> AppConfig:
    """테스트용 AppConfig를 생성한다."""
    paths = PathsConfig(base_dir=str(tmp_path))
    paths.resolved_outputs_dir.mkdir(parents=True, exist_ok=True)
    return AppConfig(paths=paths, lifecycle=lifecycle)


class TestLifecycleScheduler:
    """라이프사이클 스케줄러 동작을 검증한다."""

    @pytest.mark.asyncio
    async def test_disabled_start_does_not_create_task(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, LifecycleConfig(enabled=False))
        scheduler = LifecycleScheduler(config)

        await scheduler.start()

        assert scheduler.is_running is False

    @pytest.mark.asyncio
    async def test_enabled_start_creates_sleeping_task(self, tmp_path: Path) -> None:
        config = _make_config(
            tmp_path,
            LifecycleConfig(enabled=True, interval_hours=24, run_on_startup=False),
        )
        scheduler = LifecycleScheduler(config)

        await scheduler.start()
        try:
            assert scheduler.is_running is True
        finally:
            await scheduler.stop()

        assert scheduler.is_running is False

    @pytest.mark.asyncio
    async def test_update_config_restarts_with_new_interval(self, tmp_path: Path) -> None:
        disabled = _make_config(tmp_path, LifecycleConfig(enabled=False))
        enabled = _make_config(tmp_path, LifecycleConfig(enabled=True, interval_hours=12))
        scheduler = LifecycleScheduler(disabled)

        await scheduler.update_config(enabled)
        try:
            assert scheduler.is_running is True
            assert scheduler.interval_seconds == 12 * 3600
        finally:
            await scheduler.stop()

    @pytest.mark.asyncio
    async def test_run_once_records_result(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, LifecycleConfig(enabled=False))
        scheduler = LifecycleScheduler(config)

        result = await scheduler.run_once()

        assert result.total_scanned == 0
        assert scheduler.last_result is result
        assert scheduler.last_completed_at is not None
        assert scheduler.last_error is None

    @pytest.mark.asyncio
    async def test_update_config_does_not_run_startup_cleanup_immediately(
        self,
        tmp_path: Path,
    ) -> None:
        class _CountingScheduler(LifecycleScheduler):
            def __init__(self, config: AppConfig) -> None:
                super().__init__(config)
                self.run_count = 0

            async def run_once(self) -> LifecycleResult:
                self.run_count += 1
                return LifecycleResult()

        disabled = _make_config(tmp_path, LifecycleConfig(enabled=False))
        enabled = _make_config(
            tmp_path,
            LifecycleConfig(enabled=True, interval_hours=24, run_on_startup=True),
        )
        scheduler = _CountingScheduler(disabled)

        await scheduler.update_config(enabled)
        try:
            await asyncio.sleep(0)
            assert scheduler.run_count == 0
        finally:
            await scheduler.stop()

    @pytest.mark.asyncio
    async def test_startup_flag_runs_once_on_start(self, tmp_path: Path) -> None:
        class _CountingScheduler(LifecycleScheduler):
            def __init__(self, config: AppConfig) -> None:
                super().__init__(config)
                self.run_count = 0

            async def run_once(self) -> LifecycleResult:
                self.run_count += 1
                return LifecycleResult()

        config = _make_config(
            tmp_path,
            LifecycleConfig(enabled=True, interval_hours=24, run_on_startup=True),
        )
        scheduler = _CountingScheduler(config)

        await scheduler.start()
        try:
            await asyncio.sleep(0)
            assert scheduler.run_count == 1
        finally:
            await scheduler.stop()
