"""자동 전사/요약 스케줄러 테스트."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers.auto_processing import router as auto_processing_router
from api.routers.system import router as system_router
from config import AppConfig, PathsConfig
from core.auto_processing import AutoProcessingResult, AutoProcessingRunner, classify_meeting
from core.auto_processing_scheduler import AutoProcessingScheduler
from core.watcher import AudioInputScanReport


@dataclass
class _Job:
    meeting_id: str
    audio_path: str
    created_at: str
    status: str = "completed"
    id: int = 1
    error_message: str = ""
    stt_provider: str = ""
    stt_model: str = ""


class _Queue:
    def __init__(self, jobs: list[_Job]) -> None:
        self._jobs = jobs

    async def get_all_jobs(self) -> list[_Job]:
        return self._jobs

    async def queue_job(
        self,
        job_id: int,
        requested_action: str = "",
        *,
        stt_provider: str = "",
        stt_model: str = "",
    ) -> _Job:
        """테스트 큐에서도 recorded→queued 예약만 수행한다."""
        job = next(job for job in self._jobs if job.id == job_id)
        if job.status != "recorded":
            raise RuntimeError("recorded 작업만 큐잉할 수 있습니다.")
        job.status = "queued"
        job.stt_provider = stt_provider
        job.stt_model = stt_model
        return job


def _make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))


def _with_auto_processing_overrides(config: AppConfig, **updates: object) -> AppConfig:
    """테스트용 auto_processing 설정 override를 적용한다."""
    return config.model_copy(
        update={"auto_processing": config.auto_processing.model_copy(update=updates)}
    )


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
            _Job("m1", str(audio), now, id=1),
            _Job("m2", str(audio), now, id=2),
            _Job("m3", str(audio), now, id=3),
            _Job("old", str(audio), old, id=4),
        ]
    )
    queue._jobs[0].status = "recorded"
    pipeline = AsyncMock()

    runner = AutoProcessingRunner(config=config, job_queue=queue, pipeline=pipeline)
    result = await runner.run(action="full", recent_hours=48)

    assert result.queued == 1
    assert result.transcribed == 0
    assert result.summarized == 1
    assert result.failed == 0
    pipeline.run.assert_not_awaited()
    assert queue._jobs[0].status == "queued"
    pipeline.run_llm_steps.assert_awaited_once_with("m2")


@pytest.mark.asyncio
async def test_auto_processing_runner는_기본적으로_누락분_전체를_큐에_등록한다(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    audio = config.paths.resolved_audio_input_dir / "m1.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")

    now = datetime.now().isoformat()
    queue = _Queue(
        [
            _Job("m1", str(audio), now, status="recorded", id=1),
            _Job("m2", str(audio), now, status="recorded", id=2),
        ]
    )
    pipeline = AsyncMock()

    runner = AutoProcessingRunner(config=config, job_queue=queue, pipeline=pipeline)
    result = await runner.run(action="transcribe", recent_hours=48)

    assert result.matched == 2
    assert result.queued == 2
    assert result.skipped == 0
    assert result.skipped_by_limit == 0
    assert result.meeting_ids == ["m1", "m2"]
    pipeline.run.assert_not_awaited()
    assert queue._jobs[0].status == "queued"
    assert queue._jobs[1].status == "queued"


@pytest.mark.asyncio
async def test_auto_processing_runner는_명시한_1회_상한을_적용한다(
    tmp_path: Path,
) -> None:
    """운영자가 설정한 양수 상한은 보존하되 기본값은 아니다."""
    config = _with_auto_processing_overrides(_make_config(tmp_path), max_items_per_run=1)
    audio = config.paths.resolved_audio_input_dir / "m1.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")
    now = datetime.now().isoformat()
    queue = _Queue(
        [
            _Job("m1", str(audio), now, status="recorded", id=1),
            _Job("m2", str(audio), now, status="recorded", id=2),
        ]
    )

    result = await AutoProcessingRunner(
        config=config,
        job_queue=queue,
        pipeline=AsyncMock(),
    ).run(action="transcribe", recent_hours=48)

    assert result.queued == 1
    assert result.skipped_by_limit == 1
    assert result.meeting_ids == ["m1"]
    assert queue._jobs[1].status == "recorded"


@pytest.mark.asyncio
async def test_auto_processing_runner는_큐_등록_성공건만_queued로_반환한다(
    tmp_path: Path,
) -> None:
    """대기열 등록 실패를 전사 완료나 큐 등록 성공으로 표시하지 않는다."""
    config = _make_config(tmp_path)
    audio = config.paths.resolved_audio_input_dir / "m1.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")
    queue = _Queue([_Job("m1", str(audio), datetime.now().isoformat(), status="recorded")])
    queue.queue_job = AsyncMock(side_effect=RuntimeError("queue unavailable"))  # type: ignore[method-assign]

    result = await AutoProcessingRunner(
        config=config,
        job_queue=queue,
        pipeline=AsyncMock(),
    ).run(action="transcribe", recent_hours=48)

    assert result.matched == 1
    assert result.queued == 0
    assert result.transcribed == 0
    assert result.failed == 1
    assert result.meeting_ids == []


@pytest.mark.asyncio
async def test_auto_processing_runner는_HF_offline_pyannote_cache_누락시_보류한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config(tmp_path)
    config = _with_auto_processing_overrides(config, max_items_per_run=0)
    audio = config.paths.resolved_audio_input_dir / "m1.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(
        "core.runtime_safety.missing_pyannote_offline_cache_files",
        lambda _model_name: ["pyannote/speaker-diarization-community-1:config.yaml"],
    )

    now = datetime.now().isoformat()
    queue = _Queue([_Job("m1", str(audio), now, status="recorded")])
    pipeline = AsyncMock()

    runner = AutoProcessingRunner(config=config, job_queue=queue, pipeline=pipeline)
    result = await runner.run(action="full", recent_hours=48)

    assert result.queued == 0
    assert result.skipped == 1
    assert result.errors[0]["code"] == "hf_offline_pyannote_cache_incomplete"
    pipeline.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_processing_runner는_공격적_thermal_설정이면_전사를_보류한다(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    config = config.model_copy(
        update={
            "thermal": config.thermal.model_copy(update={"batch_size": 3, "cooldown_seconds": 60})
        }
    )
    audio = config.paths.resolved_audio_input_dir / "m1.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")

    now = datetime.now().isoformat()
    queue = _Queue([_Job("m1", str(audio), now, status="recorded")])
    pipeline = AsyncMock()

    runner = AutoProcessingRunner(config=config, job_queue=queue, pipeline=pipeline)
    result = await runner.run(action="transcribe", recent_hours=48)

    assert result.queued == 0
    assert result.skipped == 1
    assert result.errors[0]["code"] == "auto_processing_aggressive_thermal"
    pipeline.run.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_사용자가_취소한_recorded_회의는_다음_자동실행에서_재큐잉하지_않는다(
    tmp_path: Path,
) -> None:
    """명시적 취소 marker를 자동 OpenAI 업로드가 무동의로 재개하지 않는다."""
    config = _make_config(tmp_path)
    audio = config.paths.resolved_audio_input_dir / "cancelled.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio")
    job = _Job(
        "cancelled",
        str(audio),
        datetime.now().isoformat(),
        status="recorded",
        id=9,
        error_message="사용자가 취소함",
    )
    queue = _Queue([job])
    pipeline = AsyncMock()

    result = await AutoProcessingRunner(
        config=config,
        job_queue=queue,
        pipeline=pipeline,
    ).run(action="transcribe", recent_hours=48)

    assert result.matched == 0
    assert job.status == "recorded"
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
    assert data["max_items_per_run"] == 0
    assert data["run_on_startup_if_missed"] is True
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


def test_auto_processing_run_now_api는_큐등록과_상한보류를_분리해_반환한다(
    tmp_path: Path,
) -> None:
    """즉시 실행 응답은 전사 완료 대신 큐 등록 및 상한 보류를 명시한다."""

    class _Scheduler:
        is_processing = False

        async def reserve_run_once(self) -> asyncio.Task[AutoProcessingResult]:
            async def _result() -> AutoProcessingResult:
                return AutoProcessingResult(
                    action="transcribe",
                    recent_hours=48,
                    matched=3,
                    queued=1,
                    skipped=2,
                    skipped_by_limit=2,
                )

            return asyncio.create_task(_result())

    app = FastAPI()
    app.include_router(auto_processing_router, prefix="/api")
    config = _make_config(tmp_path)
    config.auto_processing.action = "summarize"
    app.state.config = config
    app.state.auto_processing_scheduler = _Scheduler()

    with TestClient(app) as client:
        response = client.post("/api/auto-processing/run-now")

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["queued"] == 1
    assert result["transcribed"] == 0
    assert result["skipped"] == 2
    assert result["skipped_by_limit"] == 2


def test_auto_processing_run_now_transcribe는_악성_Host에서_실행하지_않는다(
    tmp_path: Path,
) -> None:
    """pinned OpenAI 작업 가능성이 있는 transcribe/full 즉시 실행은 loopback 전용이다."""

    class _Scheduler:
        is_processing = False
        run_once = AsyncMock()

    app = FastAPI()
    app.include_router(auto_processing_router, prefix="/api")
    config = _make_config(tmp_path)
    config.auto_processing.action = "full"
    app.state.config = config
    scheduler = _Scheduler()
    app.state.auto_processing_scheduler = scheduler

    with TestClient(app) as client:
        resp = client.post(
            "/api/auto-processing/run-now",
            headers={"host": "attacker.example:8765"},
        )

    assert resp.status_code == 403
    scheduler.run_once.assert_not_called()


@pytest.mark.asyncio
async def test_복구스캔과_run_now는_동시에_실행되지_않는다(tmp_path: Path) -> None:
    """복구 중 생성된 recorded row를 자동 처리 요청이 선점하지 못한다."""

    class _Watcher:
        def __init__(self) -> None:
            self.running = False
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.scan_report = AudioInputScanReport()

        @property
        def is_scan_running(self) -> bool:
            return self.running

        async def recover_unregistered_files(self, *, recent_days: int) -> None:
            assert recent_days == 7
            self.running = True
            self.entered.set()
            try:
                await self.release.wait()
                self.scan_report = AudioInputScanReport(
                    phase="completed",
                    mode="recovery",
                )
            finally:
                self.running = False

    class _Scheduler:
        is_processing = False
        run_once = AsyncMock()

    app = FastAPI()
    app.include_router(auto_processing_router, prefix="/api")
    app.include_router(system_router, prefix="/api")
    config = _make_config(tmp_path)
    config.auto_processing.enabled = False
    watcher = _Watcher()
    scheduler = _Scheduler()
    app.state.config = config
    app.state.folder_watcher = watcher
    app.state.auto_processing_scheduler = scheduler

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8765",
    ) as client:
        recovery_task = asyncio.create_task(
            client.post("/api/system/audio-input/recover?recent_days=7")
        )
        await asyncio.wait_for(watcher.entered.wait(), timeout=1.0)
        assert app.state.openai_settings_mutation_lock.locked()

        hostile_recovery = await asyncio.wait_for(
            client.post(
                "/api/system/audio-input/recover?recent_days=7",
                headers={"host": "attacker.example:8765"},
            ),
            timeout=0.5,
        )
        assert hostile_recovery.status_code == 403

        run_now = await client.post("/api/auto-processing/run-now")
        assert run_now.status_code == 409
        scheduler.run_once.assert_not_awaited()

        watcher.release.set()
        recovery = await asyncio.wait_for(recovery_task, timeout=1.0)

    assert recovery.status_code == 200
    scheduler.run_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_now_배치중에는_설정lock을_놓고_복구를_거부한다(tmp_path: Path) -> None:
    """자동 처리 선점 뒤 긴 배치는 공용 lock을 독점하지 않는다."""

    class _Watcher:
        is_scan_running = False
        recover_unregistered_files = AsyncMock()

    class _Scheduler:
        def __init__(self) -> None:
            self.processing = False
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        @property
        def is_processing(self) -> bool:
            return self.processing

        async def reserve_run_once(self) -> asyncio.Task[AutoProcessingResult] | None:
            if self.processing:
                return None
            self.processing = True

            async def _run() -> AutoProcessingResult:
                self.entered.set()
                try:
                    await self.release.wait()
                finally:
                    self.processing = False
                return AutoProcessingResult(action="full", recent_hours=48)

            return asyncio.create_task(_run())

    app = FastAPI()
    app.include_router(auto_processing_router, prefix="/api")
    app.include_router(system_router, prefix="/api")
    config = _make_config(tmp_path)
    config.auto_processing.enabled = False
    watcher = _Watcher()
    scheduler = _Scheduler()
    app.state.config = config
    app.state.folder_watcher = watcher
    app.state.auto_processing_scheduler = scheduler

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8765",
    ) as client:
        run_task = asyncio.create_task(client.post("/api/auto-processing/run-now"))
        await asyncio.wait_for(scheduler.entered.wait(), timeout=1.0)

        assert not app.state.openai_settings_mutation_lock.locked()
        recovery = await asyncio.wait_for(
            client.post("/api/system/audio-input/recover?recent_days=7"),
            timeout=0.5,
        )
        assert recovery.status_code == 409
        watcher.recover_unregistered_files.assert_not_awaited()

        scheduler.release.set()
        run_response = await asyncio.wait_for(run_task, timeout=1.0)

    assert run_response.status_code == 200


@pytest.mark.asyncio
async def test_scheduler_reservation은_호출소유권을_즉시_선점한다(tmp_path: Path) -> None:
    """예약 task만 run lock을 소유하고 중복 예약은 대기 없이 거부한다."""
    config = _make_config(tmp_path)
    scheduler = AutoProcessingScheduler(
        config=config,
        job_queue=_Queue([]),
        pipeline=AsyncMock(),
    )

    reserved = await scheduler.reserve_run_once()

    assert reserved is not None
    assert scheduler.is_processing is True
    assert await scheduler.reserve_run_once() is None
    result = await reserved
    assert result.action == config.auto_processing.action
    assert scheduler.is_processing is False


@pytest.mark.asyncio
async def test_scheduler_reservation은_background_owner를_가로채지_않는다(
    tmp_path: Path,
) -> None:
    """기존 background run이 소유한 lock을 수동 예약이 오인하지 않는다."""

    class _BlockingQueue(_Queue):
        def __init__(self) -> None:
            super().__init__([])
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def get_all_jobs(self) -> list[_Job]:
            self.entered.set()
            await self.release.wait()
            return []

    config = _make_config(tmp_path)
    queue = _BlockingQueue()
    scheduler = AutoProcessingScheduler(
        config=config,
        job_queue=queue,
        pipeline=AsyncMock(),
    )
    background = asyncio.create_task(scheduler.run_once())
    await asyncio.wait_for(queue.entered.wait(), timeout=1.0)

    assert scheduler.is_processing is True
    assert await scheduler.reserve_run_once() is None

    queue.release.set()
    await asyncio.wait_for(background, timeout=1.0)
    assert scheduler.is_processing is False


@pytest.mark.asyncio
async def test_scheduler_reserved_task_취소도_lock을_해제한다(tmp_path: Path) -> None:
    """HTTP 요청 취소가 예약 task와 run lock을 고아로 남기지 않는다."""

    class _BlockingQueue(_Queue):
        def __init__(self) -> None:
            super().__init__([])
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def get_all_jobs(self) -> list[_Job]:
            self.entered.set()
            await self.release.wait()
            return []

    config = _make_config(tmp_path)
    queue = _BlockingQueue()
    scheduler = AutoProcessingScheduler(
        config=config,
        job_queue=queue,
        pipeline=AsyncMock(),
    )
    reserved = await scheduler.reserve_run_once()
    assert reserved is not None
    await asyncio.wait_for(queue.entered.wait(), timeout=1.0)

    reserved.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reserved

    assert scheduler.is_processing is False


@pytest.mark.asyncio
async def test_scheduler_reserved_task_시작전취소도_lock을_해제한다(tmp_path: Path) -> None:
    """예약 coroutine 첫 turn 전 취소도 done callback으로 lock을 회수한다."""
    scheduler = AutoProcessingScheduler(
        config=_make_config(tmp_path),
        job_queue=_Queue([]),
        pipeline=AsyncMock(),
    )
    reserved = await scheduler.reserve_run_once()
    assert reserved is not None
    assert scheduler.is_processing is True

    reserved.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reserved
    await asyncio.sleep(0)

    assert scheduler.is_processing is False
