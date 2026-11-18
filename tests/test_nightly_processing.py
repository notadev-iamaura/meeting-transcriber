"""야간 예약의 로컬시각·실제 DB·작업 중단 경계를 확인한다."""

from __future__ import annotations

import asyncio
import wave
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from config import AppConfig, AutoProcessingConfig, PathsConfig
from core.auto_processing import AutoProcessingRunner
from core.auto_processing_scheduler import AutoProcessingScheduler
from core.job_queue import AsyncJobQueue, JobQueue, JobStatus, parse_cancellation_claim
from core.nightly_window import current_window
from core.orchestrator import JobProcessor


class Clock(datetime):
    """절전·날짜 변경을 실제로 기다리지 않고 Mac 로컬 시계를 이동시킨다."""

    instant = datetime(2026, 4, 20, 2)

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        """테스트가 지정한 현재 시각을 사용한다."""
        return cls.instant


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """실제 큐, 독립 데이터 폴더 및 제어 가능한 파이프라인을 생성한다."""
    for module in ("auto_processing", "auto_processing_scheduler", "orchestrator"):
        monkeypatch.setattr(f"core.{module}.datetime", Clock)
    monkeypatch.setattr(Clock, "instant", datetime(2026, 4, 20, 2))
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    monkeypatch.setattr(JobQueue, "_now_iso", staticmethod(lambda: Clock.now().isoformat()))
    db = JobQueue(tmp_path / "jobs.db")
    db.initialize()
    queue = AsyncJobQueue(db)
    pipeline = Mock()
    pipeline._config = config
    pipeline.run = AsyncMock(return_value=SimpleNamespace(status="completed"))
    pipeline.run_llm_steps = AsyncMock()
    pipeline.get_status.return_value = None
    scheduler = AutoProcessingScheduler(config=config, job_queue=queue, pipeline=pipeline)
    processor = JobProcessor(queue, pipeline, AsyncMock())
    processor._perf_stats = None

    def add(name: str, **kwargs: str) -> int:
        """녹음을 실제 파일과 DB에 기록한다."""
        path = tmp_path / f"{name}.wav"
        path.write_bytes(b"test recording")
        return db.add_job(
            name, str(path), initial_status=kwargs.pop("initial_status", "recorded"), **kwargs
        )

    yield SimpleNamespace(
        config=config,
        db=db,
        queue=queue,
        pipeline=pipeline,
        scheduler=scheduler,
        processor=processor,
        add=add,
    )
    db.close()


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(0, 59, False), (1, 0, True), (5, 59, True), (6, 0, False), (12, 0, False)],
)
def test_window_time_boundaries(hour: int, minute: int, expected: bool) -> None:
    """첫 시작과 마감 경계를 포함 여부까지 검증한다."""
    assert (
        bool(current_window(AutoProcessingConfig(), datetime(2026, 4, 20, hour, minute)))
        == expected
    )


def test_overnight_config_and_legacy_start_at_six() -> None:
    """자정 넘김, 종료를 저장하지 않은 과거 설정, 명시된 같은 시각을 구분한다."""
    config = AutoProcessingConfig(run_at="22:00", stop_at="07:00")
    before_midnight = current_window(config, datetime(2026, 4, 20, 22))
    assert before_midnight == current_window(config, datetime(2026, 4, 21, 6, 59))
    assert before_midnight == (datetime(2026, 4, 20, 22), datetime(2026, 4, 21, 7))
    assert current_window(config, datetime(2026, 4, 21, 7)) is None
    assert AutoProcessingConfig(run_at="06:00").stop_at == "11:00"
    with pytest.raises(ValidationError):
        AutoProcessingConfig(run_at="06:00", stop_at="06:00")


@pytest.mark.parametrize(
    ("initial_hour", "wake_hour", "catchup", "days"),
    [(0, 4, True, [20, 21]), (0, 8, True, [21]), (2, 4, False, [21])],
)
async def test_scheduler_wall_clock_after_sleep(
    env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    initial_hour: int,
    wake_hour: int,
    catchup: bool,
    days: list[int],
) -> None:
    """앱과 시계를 재현해 창 안 복귀·창 밖 복귀·당일 보충 해제를 구별한다."""
    env.config.auto_processing.run_on_startup_if_missed = catchup
    Clock.instant = datetime(2026, 4, 20, initial_hour)
    ticks = iter(
        [
            datetime(2026, 4, 20, wake_hour),
            datetime(2026, 4, 20, wake_hour, 1),
            datetime(2026, 4, 20, 12),
            datetime(2026, 4, 21, 1),
        ]
    )

    async def clock_tick(seconds: float) -> None:
        """절전 동안 경과한 벽시각으로 건너뛴다."""
        assert 0 < seconds <= 60
        try:
            Clock.instant = next(ticks)
        except StopIteration:
            raise asyncio.CancelledError from None

    scan = AsyncMock(return_value=True)
    monkeypatch.setattr(env.scheduler, "_run_scheduled_window", scan)
    monkeypatch.setattr("core.auto_processing_scheduler.asyncio.sleep", clock_tick)
    with pytest.raises(asyncio.CancelledError):
        await env.scheduler._run_loop()
    assert [call.args[0] for call in scan.await_args_list] == [
        datetime(2026, 4, day, 1) for day in days
    ]


async def test_catch_up_range_actual_db_and_restart(env: SimpleNamespace) -> None:
    """절전 중 놓친 시각을 보충하고 48시간·편집·취소·클라우드 경계를 보존한다."""
    target = Clock.now()
    Clock.instant = target - timedelta(hours=48, seconds=1)
    old = env.add("too-old")
    Clock.instant = target - timedelta(hours=48)
    eligible = env.add("exact-boundary")
    Clock.instant = target
    more = [env.add(f"recent-{i}") for i in range(4)]
    edited = env.add("finished", initial_status="completed")
    rejected = env.add("cancelled")
    env.db.force_set_status(rejected, JobStatus.RECORDED, "사용자가 취소함")
    external = env.add("openai", stt_provider="openai", stt_model="gpt-4o-transcribe-diarize")
    env.config.stt.provider = "openai"
    start = target.replace(hour=1)
    assert await env.scheduler._run_scheduled_window(start)
    assert env.scheduler.last_result.queued == 5
    for job_id in [eligible, *more]:
        job = env.db.get_job(job_id)
        assert (job.status, job.requested_action, job.stt_provider, job.stt_model) == (
            "queued",
            "nightly_full",
            "local",
            env.config.stt.model_name,
        )
    assert [env.db.get_job(i).status for i in (old, edited, rejected, external)] == [
        "recorded",
        "completed",
        "recorded",
        "recorded",
    ]
    env.pipeline.run_llm_steps.assert_not_awaited()
    # 같은 창에 앱을 재실행해도 새 스캔이 발생하지 않는다.
    restarted = AutoProcessingScheduler(
        config=env.config, job_queue=env.queue, pipeline=env.pipeline
    )
    assert await restarted._run_scheduled_window(start)
    assert restarted.last_started_at is None
    Clock.instant = target.replace(hour=7)
    assert not await restarted._run_scheduled_window(start + timedelta(days=1))


async def test_expired_job_cannot_block_manual_and_resumes_after_restart(
    env: SimpleNamespace,
) -> None:
    """중간 산출물 보존, 새 모델 진입 차단, 수동 작업, 다음 날 재개를 실제 큐로 확인한다."""
    jid = env.add("night")
    mid = env.add("manual")
    env.db.queue_job(
        jid,
        "nightly_full",
        stt_provider="local",
        stt_model="test",
        nightly_window=Clock.now().isoformat(),
    )
    env.db.queue_job(mid, "full", stt_provider="local", stt_model="test")
    checkpoint = env.config.paths.resolved_base_dir / "preserved.json"
    cleaned = asyncio.Event()

    async def interrupted(path: Path, **kwargs: Any) -> None:
        """모델 실행 도중 마감된 경우를 재현한다."""
        assert kwargs["skip_llm_steps"] is False and kwargs["stt_provider"] == "local"
        await kwargs["on_step_start"]("transcribe")
        checkpoint.write_text("completed conversion")
        Clock.instant = Clock.now().replace(hour=6)
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    env.pipeline.run.side_effect = interrupted
    assert not await asyncio.wait_for(env.processor._process_job(env.db.get_job(jid)), timeout=4)
    assert cleaned.is_set() and checkpoint.read_text() == "completed conversion"
    paused = env.db.get_job(jid)
    assert paused.status == "queued" and paused.requested_action == "nightly_full"
    restarted = JobProcessor(env.queue, env.pipeline, AsyncMock())
    assert (await restarted._get_next_job()).id == mid
    assert not await restarted._process_job(paused)

    async def finish(path: Path, **kwargs: Any) -> SimpleNamespace:
        """재개 또는 수동 실행의 단계 이벤트를 기록한다."""
        for name in ("transcribe", "diarize", "merge", "correct", "summarize", "chunk", "embed"):
            await kwargs["on_step_start"](name)
        return SimpleNamespace(status="completed")

    env.pipeline.run.side_effect = finish
    assert await restarted._process_job(env.db.get_job(mid))
    assert await restarted._get_next_job() is None
    Clock.instant += timedelta(hours=67)
    assert (await restarted._get_next_job()).id == jid
    assert await restarted._process_job(paused)
    assert (
        env.db.get_job(jid).status == "completed"
        and checkpoint.read_text() == "completed conversion"
    )


async def test_manual_run_now_not_bound_to_night_and_user_cancel_preserved(
    env: SimpleNamespace,
) -> None:
    """수동 즉시 실행과 야간 선점 뒤 사용자의 취소를 구분한다."""
    jid = env.add("manual-run-now")
    Clock.instant = Clock.now().replace(hour=12)
    runner = AutoProcessingRunner(config=env.config, job_queue=env.queue, pipeline=env.pipeline)
    assert (await runner.run(action="full", recent_hours=48)).queued == 1
    assert env.db.get_job(jid).requested_action == "full"
    jid = env.add("night-cancel")
    env.db.queue_job(
        jid,
        "nightly_full",
        stt_provider="local",
        stt_model="test",
        nightly_window=Clock.now().isoformat(),
    )
    env.db.claim_queued_job_for_processing(jid)
    claim = env.db.claim_active_job_for_cancellation(jid, "test-nightly-cancel")
    assert (
        parse_cancellation_claim(claim.requested_action).original_requested_action
        == "nightly_full"
    )


async def test_failed_local_jobs_are_retried_once_per_window(env: SimpleNamespace) -> None:
    """실제 WAV와 손상 파일을 판정하고 취소·재시도 한도·편집·격리를 존중한다."""
    root = env.config.paths.resolved_base_dir
    valid = env.add("failed-valid", initial_status="failed")
    audio = Path(env.db.get_job(valid).audio_path)
    with wave.open(str(audio), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", ""))
        stream.writeframes(b"\x00\x40\x00\xc0" * 8000 * 32)
    corrupt = env.add("failed-corrupt", initial_status="failed")
    edited = env.add("failed-edited", initial_status="failed")
    corrected = env.config.paths.resolved_outputs_dir / "failed-edited" / "corrected.json"
    corrected.parent.mkdir(parents=True)
    corrected.write_text("사용자 편집", encoding="utf-8")
    exhausted = env.add("failed-exhausted", initial_status="failed")
    for _ in range(env.db.get_job(exhausted).max_retries):
        env.db.retry_job(exhausted)
        env.db.force_set_status(exhausted, JobStatus.FAILED)
    quarantine = env.config.paths.resolved_audio_quarantine_dir
    quarantine.mkdir(parents=True)
    moved = quarantine / "quarantined.wav"
    moved.write_bytes(audio.read_bytes())
    quarantined = env.db.add_job("quarantined", str(moved), initial_status="failed")
    cancelled = env.add("failed-cancelled", initial_status="failed")
    env.db.force_set_status(cancelled, JobStatus.FAILED, "사용자가 취소함")
    locked = env.add("failed-locked", initial_status="failed")
    env.db.queue_mic_retry(locked, stt_model="local-mic")
    env.db.force_set_status(locked, JobStatus.FAILED)
    runner = AutoProcessingRunner(config=env.config, job_queue=env.queue, pipeline=env.pipeline)
    result = await runner.run(action="full", recent_hours=48, scheduled=True)
    assert result.meeting_ids == ["failed-valid"] and result.queued == 1
    assert result.skipped_reasons == {
        "오디오 검증 보류 (media_invalid)": 1,
        "기존 전사 또는 회의록 보존": 1,
        "재시도 한도 도달": 1,
        "격리된 오디오": 1,
        "사용자 취소": 1,
        "다른 작업 진행 또는 복구 필요": 1,
    }
    job = env.db.get_job(valid)
    assert (
        job.retry_count == 1 and job.last_nightly_window == Clock.now().replace(hour=1).isoformat()
    )
    for job_id in (corrupt, edited, exhausted, quarantined, cancelled, locked):
        assert env.db.get_job(job_id).status == "failed"
    assert corrected.read_text(encoding="utf-8") == "사용자 편집" and moved.is_file()
    env.db.force_set_status(valid, JobStatus.FAILED, "일시적인 모델 오류")
    # 앱이 daily marker를 쓰기 전에 종료된 시나리오도 같은 밤 중복 차감하지 않는다.
    assert not (root / "nightly_processing.json").exists()
    repeated = await runner.run(action="full", recent_hours=48, scheduled=True)
    assert repeated.queued == 0 and repeated.skipped_reasons["이번 예약에서 이미 등록"] == 1
    merged = env.config.paths.resolved_checkpoints_dir / "failed-valid" / "merge.json"
    merged.parent.mkdir(parents=True)
    merged.write_text("{}")
    saved_summary = env.config.paths.resolved_outputs_dir / "failed-valid" / "summary.md"
    saved_summary.parent.mkdir(parents=True)
    saved_summary.write_text("이미 만든 요약")
    env.pipeline.get_status.return_value = SimpleNamespace(
        status="failed",
        completed_steps=[
            "convert",
            "transcribe",
            "diarize",
            "merge",
            "correct",
            "summarize",
            "chunk",
        ],
        skipped_steps=[],
    )
    Clock.instant += timedelta(days=1)
    assert (await runner.run(action="full", recent_hours=48, scheduled=True)).queued == 1
    assert env.db.get_job(valid).retry_count == 2
    assert saved_summary.read_text() == "이미 만든 요약"
    env.db.force_set_status(valid, JobStatus.FAILED)
    Clock.instant = Clock.now().replace(hour=12)
    assert env.db.retry_job(valid).requested_action == "full"
    assert (await env.processor._get_next_job()).id == valid


async def test_memory_pressure_never_marks_full_complete(env: SimpleNamespace) -> None:
    """메모리가 부족한 밤에는 완료로 표시하지 않고 다른 작업을 정상 처리한다."""
    jid = env.add("memory")
    await env.scheduler._run_scheduled_window(Clock.now().replace(hour=1))
    env.pipeline.run.return_value = SimpleNamespace(status="paused")
    assert not await env.processor._process_job(env.db.get_job(jid))
    assert env.pipeline.run.await_args.kwargs["require_full_processing"] is True
    assert await env.processor._get_next_job() is None
    assert env.db.get_job(jid).status == "queued" and env.db.get_job(jid).retry_count == 0
    manual = env.add("manual-after-memory", initial_status="queued")
    assert (await env.processor._get_next_job()).id == manual
    Clock.instant += timedelta(days=1)
    assert (await env.processor._get_next_job()).id == jid
