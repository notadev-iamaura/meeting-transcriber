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
    async def test_opt_in_expires_audio_and_keeps_searchable_meeting(self, tmp_path: Path) -> None:
        """실제 설정 전환과 만료 작업 후에도 전사·요약 HTTP 조회와 FTS 검색이 된다."""
        import json

        import httpx
        from fastapi import FastAPI

        from api.routes import router
        from core.job_queue import AsyncJobQueue, JobQueue
        from core.meeting_mutation import MeetingMutationCoordinator
        from search.hybrid_search import _search_fts
        from steps.embedder import EmbeddedChunk, _ensure_fts_table, _store_chunks_fts

        config = _make_config(tmp_path, LifecycleConfig())
        queue = AsyncJobQueue(JobQueue(config.paths.resolved_pipeline_db))
        await queue.initialize()
        lock = MeetingMutationCoordinator()
        scheduler = LifecycleScheduler(config, queue=queue, coordinator=lock)
        meeting = "meeting_20200101_090000"
        source = config.paths.resolved_audio_input_dir / f"{meeting}.wav"
        source.parent.mkdir()
        source.write_bytes(b"local retention fixture")
        await queue.add_job(meeting, str(source), initial_status="completed")
        checkpoint = config.paths.resolved_checkpoints_dir / meeting
        checkpoint.mkdir(parents=True)
        phrase = "예산안을 검토했습니다."
        (checkpoint / "correct.json").write_text(
            json.dumps(
                {"utterances": [{"text": phrase, "start": 0, "end": 2, "speaker": "나"}]},
                ensure_ascii=False,
            )
        )
        output = config.paths.resolved_outputs_dir / meeting
        output.mkdir()
        (output / "meeting_minutes.md").write_text("예산안 검토 완료")
        database = config.paths.resolved_meetings_db
        _ensure_fts_table(database)
        chunk = EmbeddedChunk("retention-fixture", phrase, [], meeting, "2020-01-01", ["나"], 0, 2)
        _store_chunks_fts([chunk], database, meeting)

        app = FastAPI()
        app.include_router(router)
        app.state.job_queue = queue
        app.state.meeting_mutation_coordinator = lock
        app.state.config = config
        url = f"/api/meetings/{meeting}"
        try:
            await scheduler.start()
            assert source.exists()
            enabled = config.model_copy(
                update={"lifecycle": LifecycleConfig(audio_retention_enabled=True)}
            )
            await scheduler.update_config(enabled)
            async with asyncio.timeout(10):
                while source.exists():
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                assert (await client.get(url)).json()["audio_expired_at"]
                assert (await client.get(f"{url}/audio")).status_code == 410
                assert (await client.get(f"{url}/transcript")).json()["utterances"][0][
                    "text"
                ] == phrase
                assert (await client.get(f"{url}/summary")).json()[
                    "markdown"
                ] == "예산안 검토 완료"
            assert _search_fts("예산안을", database, 1, strict=True)[0]["meeting_id"] == meeting
        finally:
            await scheduler.stop()
            await queue.close()

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
