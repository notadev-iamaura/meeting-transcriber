"""자동 전사/요약 스케줄러 테스트."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers.auto_processing import router as auto_processing_router
from config import AppConfig, PathsConfig
from core.auto_processing import AutoProcessingRunner, classify_meeting
from core.auto_processing_scheduler import AutoProcessingScheduler


@dataclass
class _Job:
    meeting_id: str
    audio_path: str
    created_at: str
    status: str = "completed"


class _Queue:
    def __init__(self, jobs: list[_Job]) -> None:
        self._jobs = jobs

    async def get_all_jobs(self) -> list[_Job]:
        return self._jobs


def _make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))


def test_classify_meeting_전사_요약_완료를_구분한다(tmp_path: Path) -> None:
    checkpoints = tmp_path / "checkpoints"
    outputs = tmp_path / "outputs"

    assert classify_meeting(checkpoints, outputs, "needs_transcribe") == "transcribe"

    (checkpoints / "needs_summary").mkdir(parents=True)
    (checkpoints / "needs_summary" / "merge.json").write_text("{}", encoding="utf-8")
    assert classify_meeting(checkpoints, outputs, "needs_summary") == "summarize"

    (checkpoints / "done").mkdir(parents=True)
    (checkpoints / "done" / "merge.json").write_text("{}", encoding="utf-8")
    (outputs / "done").mkdir(parents=True)
    (outputs / "done" / "summary.md").write_text("# done", encoding="utf-8")
    assert classify_meeting(checkpoints, outputs, "done") == "done"


@pytest.mark.asyncio
async def test_auto_processing_runner_full은_최근_누락분을_순차_처리한다(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    audio = config.paths.resolved_audio_input_dir / "m1.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")

    checkpoints = config.paths.resolved_checkpoints_dir
    outputs = config.paths.resolved_outputs_dir
    (checkpoints / "m2").mkdir(parents=True)
    (checkpoints / "m2" / "merge.json").write_text("{}", encoding="utf-8")
    (checkpoints / "m3").mkdir(parents=True)
    (checkpoints / "m3" / "merge.json").write_text("{}", encoding="utf-8")
    (outputs / "m3").mkdir(parents=True)
    (outputs / "m3" / "summary.md").write_text("# done", encoding="utf-8")

    now = datetime.now().isoformat()
    old = datetime(2020, 1, 1, 0, 0).isoformat()
    queue = _Queue(
        [
            _Job("m1", str(audio), now),
            _Job("m2", str(audio), now),
            _Job("m3", str(audio), now),
            _Job("old", str(audio), old),
        ]
    )
    queue._jobs[0].status = "recorded"
    pipeline = AsyncMock()

    runner = AutoProcessingRunner(config=config, job_queue=queue, pipeline=pipeline)
    result = await runner.run(action="full", recent_hours=48)

    assert result.queued == 2
    assert result.transcribed == 1
    assert result.summarized == 2
    assert result.failed == 0
    pipeline.run.assert_awaited_once()
    assert pipeline.run.await_args.kwargs["skip_llm_steps"] is False
    pipeline.run_llm_steps.assert_awaited_once_with("m2")


@pytest.mark.asyncio
async def test_auto_processing_runner는_진행중인_작업을_건너뛴다(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    audio = config.paths.resolved_audio_input_dir / "m1.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")

    now = datetime.now().isoformat()
    queue = _Queue(
        [
            _Job("m1", str(audio), now, status="queued"),
            _Job("m2", str(audio), now),
        ]
    )
    pipeline = AsyncMock()

    runner = AutoProcessingRunner(config=config, job_queue=queue, pipeline=pipeline)
    result = await runner.run(action="transcribe", recent_hours=48)

    assert result.queued == 0
    assert result.transcribed == 0
    assert result.failed == 0
    pipeline.run.assert_not_awaited()


def test_scheduler_다음_실행_시각을_계산한다(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config = config.model_copy(
        update={"auto_processing": config.auto_processing.model_copy(update={"run_at": "02:00"})}
    )
    scheduler = AutoProcessingScheduler(
        config=config,
        job_queue=_Queue([]),
        pipeline=AsyncMock(),
    )

    before = datetime(2026, 5, 20, 1, 30)
    after = datetime(2026, 5, 20, 2, 30)

    assert scheduler.seconds_until_next_run(before) == 30 * 60
    assert scheduler.seconds_until_next_run(after) == 23.5 * 60 * 60


def test_auto_processing_status_api는_스케줄러_상태를_반환한다(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(auto_processing_router, prefix="/api")
    config = _make_config(tmp_path)
    scheduler = AutoProcessingScheduler(
        config=config,
        job_queue=_Queue([]),
        pipeline=AsyncMock(),
    )
    app.state.config = config
    app.state.auto_processing_scheduler = scheduler

    with TestClient(app) as client:
        resp = client.get("/api/auto-processing/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["run_at"] == "02:00"
    assert data["recent_hours"] == 48
    assert data["processing"] is False


def test_auto_processing_run_now_api는_중복_실행을_거부한다(tmp_path: Path) -> None:
    class _Scheduler:
        is_processing = True

        async def run_once(self) -> None:
            raise AssertionError("호출되면 안 됨")

    app = FastAPI()
    app.include_router(auto_processing_router, prefix="/api")
    app.state.config = _make_config(tmp_path)
    app.state.auto_processing_scheduler = _Scheduler()

    with TestClient(app) as client:
        resp = client.post("/api/auto-processing/run-now")

    assert resp.status_code == 409
