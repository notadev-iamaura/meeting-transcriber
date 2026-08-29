"""회의별 mutation coordinator와 PipelineManager 연동 테스트."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.meeting_mutation import MeetingMutationCoordinator


@pytest.fixture
def mutation_pipeline_config(tmp_path: Path) -> MagicMock:
    """PipelineManager 생성에 필요한 최소 설정을 만든다."""
    config = MagicMock()
    config.pipeline.checkpoint_enabled = True
    config.pipeline.checkpoint_json_indent = 2
    config.pipeline.retry_max_count = 2
    config.pipeline.llm_lock_acquire_timeout_seconds = 5
    config.paths.resolved_outputs_dir = tmp_path / "outputs"
    config.paths.resolved_checkpoints_dir = tmp_path / "checkpoints"
    return config


@pytest.mark.asyncio
async def test_같은_task의_lease_재진입은_deadlock_없이_유지된다() -> None:
    """API wrapper 안의 PipelineManager가 동일 lease를 다시 얻을 수 있다."""
    coordinator = MeetingMutationCoordinator()

    async with coordinator.lease("meeting_reentrant"):
        assert coordinator.locked("meeting_reentrant") is True
        async with coordinator.lease("meeting_reentrant"):
            assert coordinator.locked("meeting_reentrant") is True
        assert coordinator.locked("meeting_reentrant") is True

    assert coordinator.locked("meeting_reentrant") is False


@pytest.mark.asyncio
async def test_다른_task의_동일_회의_mutation은_lease_해제까지_대기한다() -> None:
    """서로 다른 task의 동일 회의 mutation이 겹치지 않는다."""
    coordinator = MeetingMutationCoordinator()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def _first() -> None:
        async with coordinator.lease("meeting_serial"):
            first_entered.set()
            await release_first.wait()

    async def _second() -> None:
        async with coordinator.lease("meeting_serial"):
            second_entered.set()

    first_task = asyncio.create_task(_first())
    await first_entered.wait()
    second_task = asyncio.create_task(_second())
    await asyncio.sleep(0)
    assert second_entered.is_set() is False

    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set() is True


@pytest.mark.asyncio
async def test_run_llm_steps_직접_호출도_회의_mutation_lease를_보유(
    mutation_pipeline_config: MagicMock,
) -> None:
    """자동·일괄 요약처럼 API wrapper 밖 호출도 재전사와 직렬화된다."""
    from core.pipeline import PipelineManager, PipelineState

    coordinator = MeetingMutationCoordinator()
    pipeline = PipelineManager(
        mutation_pipeline_config,
        MagicMock(),
        meeting_mutation_coordinator=coordinator,
    )
    inner_entered = asyncio.Event()
    release_inner = asyncio.Event()
    competing_entered = asyncio.Event()
    expected = PipelineState(meeting_id="meeting_direct_llm", audio_path="/tmp/test.wav")

    async def _run_inner(*_args: object, **_kwargs: object) -> PipelineState:
        inner_entered.set()
        await release_inner.wait()
        return expected

    pipeline._run_llm_steps_inner = AsyncMock(side_effect=_run_inner)
    pipeline._unload_llm_model_if_current = AsyncMock()

    async def _competing_mutation() -> None:
        async with coordinator.lease("meeting_direct_llm"):
            competing_entered.set()

    llm_task = asyncio.create_task(pipeline.run_llm_steps("meeting_direct_llm"))
    await inner_entered.wait()
    competing_task = asyncio.create_task(_competing_mutation())
    await asyncio.sleep(0)
    assert competing_entered.is_set() is False

    release_inner.set()
    result, _ = await asyncio.gather(llm_task, competing_task)
    assert result is expected
    assert competing_entered.is_set() is True


@pytest.mark.asyncio
async def test_run_전체도_회의_mutation_lease를_보유(
    mutation_pipeline_config: MagicMock,
) -> None:
    """JobProcessor의 전체 파이프라인도 수동 mutation과 겹치지 않는다."""
    from core.pipeline import PipelineManager, PipelineState

    coordinator = MeetingMutationCoordinator()
    pipeline = PipelineManager(
        mutation_pipeline_config,
        MagicMock(),
        meeting_mutation_coordinator=coordinator,
    )
    inner_entered = asyncio.Event()
    release_inner = asyncio.Event()
    competing_entered = asyncio.Event()
    meeting_id = "meeting_full_pipeline"
    audio_path = Path("/tmp/meeting-full-pipeline.wav")
    expected = PipelineState(meeting_id=meeting_id, audio_path=str(audio_path))

    async def _run_inner(*_args: object, **_kwargs: object) -> PipelineState:
        inner_entered.set()
        await release_inner.wait()
        return expected

    pipeline._run_with_meeting_lease_held = AsyncMock(side_effect=_run_inner)

    async def _competing_mutation() -> None:
        async with coordinator.lease(meeting_id):
            competing_entered.set()

    pipeline_task = asyncio.create_task(
        pipeline.run(audio_path, meeting_id=meeting_id),
    )
    await inner_entered.wait()
    competing_task = asyncio.create_task(_competing_mutation())
    await asyncio.sleep(0)
    assert competing_entered.is_set() is False

    release_inner.set()
    result, _ = await asyncio.gather(pipeline_task, competing_task)
    assert result is expected
    assert competing_entered.is_set() is True
