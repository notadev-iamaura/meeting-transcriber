"""모델 실행 없이 실제 SQLite로 벌크 단계 순서와 복구 경계를 확인한다."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from core.job_queue import AsyncJobQueue, Job, JobQueue, JobStatus
from core.meeting_mutation import MeetingMutationCoordinator
from core.orchestrator import JobProcessor
from core.pipeline import PIPELINE_STEPS, PipelineState, PipelineStep
from core.stage_work_queue import StageWorkStatus


class _ResidencyManager:
    """락을 보유하지 않는 모델 유지 scope 테스트 대역."""

    def __init__(self) -> None:
        """진입과 이탈 기록을 준비한다."""
        self.events: list[tuple[str, str]] = []
        self.native_cleanup_pending = False

    def get_status(self) -> dict[str, bool]:
        """native 종료 대기 상태를 모델 없이 제공한다."""
        return {"native_cleanup_pending": self.native_cleanup_pending}

    @asynccontextmanager
    async def residency_scope(self, name: str, *, reuse_key: str) -> AsyncIterator[None]:
        """회의별 실행과 별도로 scope 경계만 기록한다."""
        assert reuse_key
        self.events.append(("enter", name))
        try:
            yield
        finally:
            self.events.append(("exit", name))


class _Pipeline:
    """중간 pause와 checkpoint prefix를 모사하는 모델 없는 파이프라인."""

    def __init__(self) -> None:
        """opt-in 설정과 회의별 상태를 만든다."""
        self._config = SimpleNamespace(
            pipeline=SimpleNamespace(
                bulk_stage_batching=True, bulk_max_items=2, checkpoint_enabled=True
            ),
            llm=SimpleNamespace(backend="mlx", mlx_model_name="gemma-test", model_name="unused"),
            thermal=SimpleNamespace(batch_size=2),
        )
        self._model_manager = _ResidencyManager()
        self.meeting_mutation_coordinator = MeetingMutationCoordinator()
        self.states: dict[str, PipelineState] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_at: tuple[str, str] | None = None
        self.stop_at: tuple[str, str] | None = None
        self.after_step: Any = None

    def get_status(self, meeting_id: str) -> PipelineState | None:
        """이벤트 기록에 현재 prefix를 제공한다."""
        return self.states.get(meeting_id)

    async def run(
        self,
        audio_path: Path,
        *,
        meeting_id: str,
        on_step_start: Any,
        stop_after: PipelineStep | None = None,
        **_options: Any,
    ) -> PipelineState:
        """실제 step callback과 같은 순서로 진행하고 지정 경계에서 멈춘다."""
        state = self.states.setdefault(meeting_id, PipelineState(meeting_id, str(audio_path)))
        if len(state.completed_steps) == len(PIPELINE_STEPS):
            state.status = "completed"
            return state
        last = len(PIPELINE_STEPS) - 1 if stop_after is None else PIPELINE_STEPS.index(stop_after)
        async with self.meeting_mutation_coordinator.lease(meeting_id):
            for step in PIPELINE_STEPS[: last + 1]:
                if step.value in state.completed_steps:
                    continue
                await on_step_start(step.value)
                self.calls.append((meeting_id, step.value))
                if self.fail_at == (meeting_id, step.value):
                    state.status = "failed"
                    raise RuntimeError("synthetic step failure")
                if self.stop_at == (meeting_id, step.value):
                    raise asyncio.CancelledError("synthetic app shutdown")
                state.completed_steps.append(step.value)
                if self.after_step is not None:
                    await self.after_step(meeting_id, step.value)
            state.status = "paused" if stop_after is not None else "completed"
            return state


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[tuple[JobProcessor, JobQueue, _Pipeline, AsyncMock]]:
    """기존 DB와 pipeline 대역을 연결하고 임시 DB 연결을 정리한다."""
    raw_queue = JobQueue(tmp_path / "jobs.db")
    raw_queue.initialize()
    pipeline = _Pipeline()
    thermal = AsyncMock()
    thermal.batch_count = 0
    with patch("core.orchestrator.PerfStats.load", return_value=None):
        processor = JobProcessor(AsyncJobQueue(raw_queue), pipeline, thermal)
    processor._broadcast_event = AsyncMock()
    try:
        yield processor, raw_queue, pipeline, thermal
    finally:
        raw_queue.close()


def _add(
    queue: JobQueue, tmp_path: Path, mid: str, *, model: str = "whisper-test", action: str = "full"
) -> Job:
    """원본 경로를 가진 recorded 요청을 기존 API대로 queued로 만든다."""
    path = tmp_path / f"{mid}.wav"
    path.write_bytes(b"synthetic audio placeholder")
    job_id = queue.add_job(mid, str(path), initial_status="recorded")
    queue.queue_job(job_id, requested_action=action, stt_provider="local", stt_model=model)
    return queue.get_job(job_id)


@pytest.mark.asyncio
async def test_bulk_order_has_one_completion_per_meeting(setup: Any, tmp_path: Path) -> None:
    """중간 phase는 completed를 보내지 않고 전사→병합→요약→embed 순으로 묶인다."""
    processor, queue, pipeline, thermal = setup
    jobs = [_add(queue, tmp_path, mid) for mid in ("one", "two")]
    completion_counts = []

    async def observe(mid: str, step: str) -> None:
        """embed 시작 이전에는 완료 알림이 없음을 확인한다."""
        if step == "summarize":
            completion_counts.append(
                sum(
                    call.args[0] == "job_completed"
                    for call in processor._broadcast_event.await_args_list
                )
            )

    pipeline.after_step = observe
    await processor._process_bulk_cohort(await processor._select_bulk_cohort(jobs[0]))
    assert pipeline.calls == [
        (mid, step)
        for steps in (
            ("convert", "transcribe"),
            ("diarize", "merge"),
            ("correct", "summarize"),
            ("chunk", "embed"),
        )
        for mid in ("one", "two")
        for step in steps
    ]
    assert completion_counts == [0, 0]
    assert [queue.get_job(job.id).status for job in jobs] == ["completed", "completed"]
    assert thermal.notify_job_started.await_count == thermal.notify_job_completed.await_count == 2
    assert (
        sum(call.args[0] == "job_completed" for call in processor._broadcast_event.await_args_list)
        == 2
    )
    assert pipeline._model_manager.events == [
        ("enter", "whisper"),
        ("exit", "whisper"),
        ("enter", "exaone"),
        ("exit", "exaone"),
    ]
    with processor._stage_work_queue._connection() as conn:
        stages = conn.execute("SELECT status, attempt_count FROM stage_work_items").fetchall()
    assert len(stages) == 8
    assert {(row["status"], row["attempt_count"]) for row in stages} == {("completed", 1)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "disabled",
        "no-checkpoint",
        "transcribe-only",
        "different-model",
        "one-slot",
        "thermal-one",
        "missing-source",
    ],
)
async def test_bulk_selection_falls_back_to_existing_single_path(
    setup: Any, tmp_path: Path, reason: str
) -> None:
    """지원하지 않는 설정과 원본 없는 재개는 기존 단건 경로로 보낸다."""
    processor, queue, pipeline, thermal = setup
    first = _add(
        queue, tmp_path, "one", action="transcribe" if reason == "transcribe-only" else "full"
    )
    _add(
        queue,
        tmp_path,
        "two",
        model="different" if reason == "different-model" else "whisper-test",
    )
    if reason == "disabled":
        pipeline._config.pipeline.bulk_stage_batching = False
    elif reason == "no-checkpoint":
        pipeline._config.pipeline.checkpoint_enabled = False
    elif reason == "one-slot":
        thermal.batch_count = 1
    elif reason == "thermal-one":
        pipeline._config.thermal.batch_size = 1
    elif reason == "missing-source":
        Path(first.audio_path).unlink()
    assert [job.id for job in await processor._select_bulk_cohort(first)] == [first.id]


@pytest.mark.asyncio
async def test_new_pending_work_is_not_added_to_fixed_cohort(setup: Any, tmp_path: Path) -> None:
    """접수 뒤 추가된 작업은 현재 묶음에 끼워 넣지 않는다."""
    processor, queue, pipeline, _thermal = setup
    first = _add(queue, tmp_path, "one")
    _add(queue, tmp_path, "two")
    cohort = await processor._select_bulk_cohort(first)
    later = _add(queue, tmp_path, "later")
    await processor._process_bulk_cohort(cohort)
    assert queue.get_job(later.id).status == "queued"
    assert not any(mid == "later" for mid, _step in pipeline.calls)


@pytest.mark.asyncio
async def test_failed_meeting_does_not_block_rest_of_cohort(setup: Any, tmp_path: Path) -> None:
    """화자분리 한 건 실패 뒤 다른 회의는 요약과 검색까지 진행한다."""
    processor, queue, pipeline, thermal = setup
    jobs = [_add(queue, tmp_path, mid) for mid in ("one", "two")]
    pipeline.fail_at = ("one", "diarize")
    await processor._process_bulk_cohort(jobs)
    assert [queue.get_job(job.id).status for job in jobs] == ["failed", "completed"]
    assert ("one", "correct") not in pipeline.calls
    assert ("two", "embed") in pipeline.calls
    assert thermal.notify_job_completed.await_count == 2


@pytest.mark.asyncio
async def test_cancellation_between_phases_preserves_durable_intent(
    setup: Any, tmp_path: Path
) -> None:
    """이미 전사한 첫 회의의 취소는 다음 phase에서 확정하고 나머지는 계속한다."""
    processor, queue, pipeline, _thermal = setup
    jobs = [_add(queue, tmp_path, mid) for mid in ("one", "two")]

    async def cancel_first(mid: str, step: str) -> None:
        """두 번째 전사가 끝날 때 첫 번째 회의를 사용자 취소한다."""
        if (mid, step) == ("two", "transcribe"):
            queue.claim_active_job_for_cancellation(jobs[0].id, "user-cancel")
            processor.request_cancellation("one")

    pipeline.after_step = cancel_first
    await processor._process_bulk_cohort(jobs)
    assert queue.get_job(jobs[0].id).status == "recorded"
    assert queue.get_job(jobs[0].id).error_message.startswith("사용자가 취소함")
    assert queue.get_job(jobs[1].id).status == "completed"
    assert ("one", "diarize") not in pipeline.calls
    with processor._stage_work_queue._connection() as conn:
        rows = conn.execute(
            "SELECT status, owner_session_id FROM stage_work_items WHERE meeting_id='one' AND stage!='transcribe'"
        ).fetchall()
    assert {row["status"] for row in rows} == {StageWorkStatus.CANCEL_REQUESTED.value}
    assert {row["owner_session_id"] for row in rows} == {""}


@pytest.mark.asyncio
async def test_shutdown_requeues_every_started_partial_meeting(setup: Any, tmp_path: Path) -> None:
    """앱 종료 시 실행 중 한 건과 앞서 paused인 회의 모두 queued로 남긴다."""
    processor, queue, pipeline, thermal = setup
    jobs = [_add(queue, tmp_path, mid) for mid in ("one", "two")]
    pipeline.stop_at = ("two", "transcribe")
    with pytest.raises(asyncio.CancelledError):
        await processor._process_bulk_cohort(jobs)
    assert [queue.get_job(job.id).status for job in jobs] == ["queued", "queued"]
    assert pipeline.states["one"].completed_steps == ["convert", "transcribe"]
    assert thermal.notify_job_completed.await_count == 2
    assert pipeline._model_manager.events == [("enter", "whisper"), ("exit", "whisper")]
    assert not any(
        call.args[0] == "job_completed" for call in processor._broadcast_event.await_args_list
    )


@pytest.mark.asyncio
async def test_original_replacement_between_phases_blocks_only_that_meeting(
    setup: Any, tmp_path: Path
) -> None:
    """원본 변경 뒤 이전 전사 checkpoint와 새 입력을 섞지 않는다."""
    processor, queue, pipeline, _thermal = setup
    jobs = [_add(queue, tmp_path, mid) for mid in ("one", "two")]

    async def replace_source(mid: str, step: str) -> None:
        """다음 phase 시작 전 첫 회의 입력 identity를 바꾼다."""
        if (mid, step) == ("two", "transcribe"):
            Path(jobs[0].audio_path).write_bytes(b"changed-source-data")

    pipeline.after_step = replace_source
    await processor._process_bulk_cohort(jobs)
    assert [queue.get_job(job.id).status for job in jobs] == ["failed", "completed"]
    assert ("one", "diarize") not in pipeline.calls


@pytest.mark.asyncio
async def test_resume_past_first_boundary_does_not_recompute_prefix(
    setup: Any, tmp_path: Path
) -> None:
    """이미 요약된 작업도 phase 경계를 no-op으로 지나 남은 검색만 처리한다."""
    processor, queue, pipeline, _thermal = setup
    jobs = [_add(queue, tmp_path, mid) for mid in ("one", "two")]
    pipeline.states["one"] = PipelineState(
        "one", jobs[0].audio_path, completed_steps=[step.value for step in PIPELINE_STEPS[:6]]
    )
    await processor._process_bulk_cohort(jobs)
    assert [step for mid, step in pipeline.calls if mid == "one"] == ["chunk", "embed"]
    assert queue.get_job(jobs[0].id).status == JobStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_already_completed_pipeline_is_noop_until_final_phase(
    setup: Any, tmp_path: Path
) -> None:
    """전체 checkpoint 완료의 멱등 응답도 중간 phase에서 실패로 바꾸지 않는다."""
    processor, queue, pipeline, _thermal = setup
    jobs = [_add(queue, tmp_path, mid) for mid in ("one", "two")]
    pipeline.states["one"] = PipelineState(
        "one", jobs[0].audio_path, completed_steps=[step.value for step in PIPELINE_STEPS]
    )
    await processor._process_bulk_cohort(jobs)
    assert not any(mid == "one" for mid, _step in pipeline.calls)
    assert [queue.get_job(job.id).status for job in jobs] == ["completed", "completed"]
    assert (
        sum(call.args[0] == "job_completed" for call in processor._broadcast_event.await_args_list)
        == 2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setting,value",
    [
        ("mlx_model_name", "changed-model"),
        ("backend", "ollama"),
        ("temperature", 0.9),
        ("mlx_max_tokens", 2400),
    ],
)
async def test_llm_option_change_stops_cohort_and_requeues_partial_work(
    setup: Any,
    tmp_path: Path,
    setting: str,
    value: Any,
) -> None:
    """모델·backend·sampling 옵션 변경 시 기존 세션에서 다른 설정을 실행하지 않는다."""
    processor, queue, pipeline, thermal = setup
    jobs = [_add(queue, tmp_path, mid) for mid in ("one", "two")]

    async def change_setting(mid: str, step: str) -> None:
        """첫 전사 뒤 LLM 설정을 바꾼다."""
        if (mid, step) == ("one", "transcribe"):
            setattr(pipeline._config.llm, setting, value)

    pipeline.after_step = change_setting
    await processor._process_bulk_cohort(jobs)
    assert pipeline.calls == [("one", "convert"), ("one", "transcribe")]
    assert [queue.get_job(job.id).status for job in jobs] == ["queued", "queued"]
    assert thermal.notify_job_started.await_count == thermal.notify_job_completed.await_count == 1
    assert pipeline._model_manager.events == [("enter", "whisper"), ("exit", "whisper")]


@pytest.mark.asyncio
async def test_changed_database_stt_snapshot_is_requeued_before_inference(
    setup: Any, tmp_path: Path
) -> None:
    """cohort 선택 뒤 달라진 DB 전사 모델을 기존 session 안에서 실행하지 않는다."""
    processor, queue, pipeline, _thermal = setup
    jobs = [_add(queue, tmp_path, mid) for mid in ("one", "two")]
    queue.cancel_queued_job(jobs[0].id, "settings updated")
    queue.queue_job(
        jobs[0].id, requested_action="full", stt_provider="local", stt_model="changed-model"
    )
    await processor._process_bulk_cohort(jobs)
    assert queue.get_job(jobs[0].id).status == "queued"
    assert queue.get_job(jobs[0].id).stt_model == "changed-model"
    assert not any(mid == "one" for mid, _step in pipeline.calls)
    assert queue.get_job(jobs[1].id).status == "completed"


@pytest.mark.asyncio
async def test_native_pending_preserves_cancel_owner_and_untouched_jobs(
    setup: Any, tmp_path: Path
) -> None:
    """취소 응답 후 native가 남아 있으면 audit owner와 나머지 queued 작업을 보존한다."""
    processor, queue, pipeline, _thermal = setup
    jobs = [_add(queue, tmp_path, mid) for mid in ("one", "two")]

    async def pending_cancel(mid: str, step: str) -> None:
        """모델 native 종료를 기다리는 사용자 취소 상태를 모사한다."""
        if (mid, step) == ("one", "convert"):
            queue.claim_active_job_for_cancellation(jobs[0].id, "native-cancel")
            processor.request_cancellation("one")
            pipeline._model_manager.native_cleanup_pending = True

    pipeline.after_step = pending_cancel
    await processor._process_bulk_cohort(jobs)
    assert [queue.get_job(job.id).status for job in jobs] == ["recorded", "queued"]
    assert not any(mid == "two" for mid, _step in pipeline.calls)
    with processor._stage_work_queue._connection() as conn:
        claim = conn.execute(
            "SELECT status, owner_session_id, claim_token FROM stage_work_items WHERE meeting_id='one' AND stage='transcribe'"
        ).fetchone()
    assert claim["status"] == "cancel_requested"
    assert claim["owner_session_id"] == processor._bulk_session_id
    assert claim["claim_token"]


@pytest.mark.asyncio
async def test_run_loop_waits_for_native_cleanup_before_claiming_new_jobs(
    setup: Any, tmp_path: Path
) -> None:
    """이전 native 정리가 끝나기 전에 대기 작업을 선점해 실패시키지 않는다."""
    processor, queue, pipeline, _thermal = setup
    job = _add(queue, tmp_path, "one")
    pipeline._model_manager.native_cleanup_pending = True
    processor._poll_interval = 0.001
    processor._running = True
    task = asyncio.create_task(processor._run_loop())
    await asyncio.sleep(0.01)
    processor._running = False
    await task
    assert queue.get_job(job.id).status == "queued"
    assert pipeline.calls == []


@pytest.mark.asyncio
async def test_cancel_claim_racing_with_shutdown_cleanup_does_not_strand_other_meeting(
    setup: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """중단 정리 CAS 직전 취소가 들어와도 취소 확정과 다른 회의 재대기를 모두 마친다."""
    processor, queue, pipeline, thermal = setup
    jobs = [_add(queue, tmp_path, mid) for mid in ("one", "two")]
    original_force = queue.force_set_status
    cancelled_id: int | None = None

    async def change_config(mid: str, step: str) -> None:
        """두 회의 전사 뒤 설정 변경으로 cohort 정리를 시작한다."""
        if (mid, step) == ("two", "transcribe"):
            pipeline._config.llm.mlx_model_name = "changed-model"

    def race_force(job_id: int, status: JobStatus, error_message: str = "") -> None:
        """정리의 첫 force 직전에 durable 취소 claim을 만든다."""
        nonlocal cancelled_id
        if cancelled_id is None:
            cancelled_id = job_id
            queue.claim_active_job_for_cancellation(job_id, "cleanup-cancel-race")
        original_force(job_id, status, error_message)

    pipeline.after_step = change_config
    monkeypatch.setattr(queue, "force_set_status", race_force)
    await processor._process_bulk_cohort(jobs)
    assert cancelled_id is not None
    cancelled = queue.get_job(cancelled_id)
    other = queue.get_job(next(job.id for job in jobs if job.id != cancelled_id))
    assert cancelled.status == "recorded"
    assert cancelled.error_message.startswith("사용자가 취소함")
    assert other.status == "queued"
    assert thermal.notify_job_completed.await_count == 2
    assert (
        sum(call.args[0] == "job_cancelled" for call in processor._broadcast_event.await_args_list)
        == 1
    )
    assert (
        sum(
            call.args[0] == "job_interrupted"
            for call in processor._broadcast_event.await_args_list
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "storage"])
async def test_cleanup_storage_failure_is_isolated_per_meeting(
    setup: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """한 회의의 누락·저장 실패가 나머지 회의의 queued 복구를 중단하지 않는다."""
    from core.job_queue import JobNotFoundError, JobQueueError

    processor, queue, pipeline, _thermal = setup
    jobs = [_add(queue, tmp_path, mid) for mid in ("one", "two")]
    original_force = queue.force_set_status

    async def change_config(mid: str, step: str) -> None:
        """두 회의가 paused 상태가 된 뒤 정리를 유발한다."""
        if (mid, step) == ("two", "transcribe"):
            pipeline._config.llm.mlx_model_name = "changed-model"

    def fail_first(job_id: int, status: JobStatus, error_message: str = "") -> None:
        """첫 회의의 DB 복구 한 건만 실패시킨다."""
        if job_id == jobs[0].id:
            if failure == "missing":
                raise JobNotFoundError(job_id)
            raise JobQueueError("synthetic storage failure")
        original_force(job_id, status, error_message)

    pipeline.after_step = change_config
    monkeypatch.setattr(queue, "force_set_status", fail_first)
    await processor._process_bulk_cohort(jobs)
    assert queue.get_job(jobs[1].id).status == "queued"
    assert pipeline.states["two"].completed_steps == ["convert", "transcribe"]


@pytest.mark.asyncio
async def test_cancel_claim_racing_with_cleanup_status_read_is_finalized(
    setup: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """최초 취소 검사 이후 상태 조회에서 나타난 recording claim을 놓치지 않는다."""
    processor, queue, _pipeline, thermal = setup
    job = _add(queue, tmp_path, "one")
    queue.claim_queued_job_for_processing(job.id)
    original_get = queue.get_job
    get_count = 0

    def race_get(job_id: int) -> Job:
        """정리의 두 번째 상태 읽기 직전에 durable 취소를 접수한다."""
        nonlocal get_count
        get_count += 1
        if get_count == 2:
            queue.claim_active_job_for_cancellation(job_id, "cleanup-read-race")
        return original_get(job_id)

    monkeypatch.setattr(queue, "get_job", race_get)
    await processor._restore_interrupted_bulk_job(job)
    current = original_get(job.id)
    assert current.status == "recorded"
    assert current.error_message.startswith("사용자가 취소함")
    assert thermal.notify_job_completed.await_count == 1
    assert processor._broadcast_event.await_args.args[0] == "job_cancelled"
