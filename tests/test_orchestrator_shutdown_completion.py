"""완료 후 쿨다운 취소가 durable 완료를 재대기로 되돌리지 않는지 검증한다."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from core.job_queue import AsyncJobQueue, JobQueue, JobStatus
from core.orchestrator import JobProcessor


async def _setup(
    tmp_path: Path,
) -> tuple[JobProcessor, AsyncJobQueue, Any, SimpleNamespace, SimpleNamespace, AsyncMock]:
    """실제 임시 SQLite 큐와 모델 없는 파이프라인·서멀 대역을 만든다."""
    queue = AsyncJobQueue(JobQueue(tmp_path / "pipeline.db"))
    await queue.initialize()
    job_id = await queue.add_job(
        "sample",
        str(tmp_path / "audio.wav"),
        stt_provider="local",
        stt_model="mlx-community/whisper-large-v3-turbo",
    )
    pipeline = SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(status="completed")),
        get_status=lambda _meeting_id: None,
        _config=SimpleNamespace(),
    )
    thermal = SimpleNamespace(
        wait_if_needed=AsyncMock(),
        notify_job_started=AsyncMock(),
        notify_job_completed=AsyncMock(),
    )
    ws = AsyncMock()
    with patch("core.orchestrator.PerfStats.load", return_value=None):
        processor = JobProcessor(queue, pipeline, thermal, ws)
    return processor, queue, await queue.get_job(job_id), pipeline, thermal, ws


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "commit_confirmed,user_cancel,expected_status",
    [
        (True, False, JobStatus.COMPLETED.value),
        (False, False, JobStatus.QUEUED.value),
        (True, True, JobStatus.RECORDED.value),
    ],
)
async def test_쿨다운중_종료취소는_확정완료만_보존하고_서멀통지는_한번만_한다(
    tmp_path: Path,
    commit_confirmed: bool,
    user_cancel: bool,
    expected_status: str,
) -> None:
    """실제 DB 완료 확인 뒤 Event 대기에서 취소해 종료·사용자 취소 경계를 검증한다."""
    processor, queue, job, pipeline, thermal, ws = await _setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def cooldown() -> None:
        """완료 카운트 증가 뒤 첫 쿨다운만 실제 비동기 대기시킨다."""
        entered.set()
        if thermal.notify_job_completed.await_count == 1:
            await release.wait()

    thermal.notify_job_completed.side_effect = cooldown
    task: asyncio.Task[bool] | None = None
    try:
        with patch.object(
            processor,
            "_mark_job_completed_after_pipeline",
            wraps=processor._mark_job_completed_after_pipeline if commit_confirmed else None,
            **({} if commit_confirmed else {"return_value": False}),
        ):
            task = asyncio.create_task(processor._process_job(job))
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert (
                (await queue.get_job(job.id)).status == JobStatus.COMPLETED.value
            ) is commit_confirmed
            if user_cancel:
                processor.request_cancellation(job.meeting_id)
            task.cancel()
            if user_cancel:
                assert await asyncio.wait_for(task, timeout=5) is False
            else:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
        assert (await queue.get_job(job.id)).status == expected_status
        thermal.notify_job_completed.assert_awaited_once()
        pipeline.run.assert_awaited_once()
        events = [call.args[0].event_type for call in ws.broadcast_event.call_args_list]
        assert ("job_interrupted" in events) is (not commit_confirmed and not user_cancel)
        assert ("job_cancelled" in events) is user_cancel
        assert ("job_completed" in events) is (commit_confirmed and not user_cancel)
        assert not processor.is_cancellation_requested(job.meeting_id)
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await queue.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("websocket_mode", ["missing", "failing"])
async def test_확정완료후_종료취소도_접수기록의_마지막완료를_보존한다(
    tmp_path: Path, websocket_mode: str
) -> None:
    """실제 SQLite 접수 기록은 WebSocket 없이도 completed로 끝나고 재개 후 유지된다."""
    processor, queue, job, _pipeline, thermal, ws = await _setup(tmp_path)
    queue.queue.save_batch_receipt(
        dict(request_id="batch", action="full", meeting_ids=[job.meeting_id], queued=1)
    )
    queue.queue.record_batch_event(job.meeting_id, dict(status="queued"))
    if websocket_mode == "missing":
        processor._ws_manager = None
    else:
        ws.broadcast_event.side_effect = OSError("테스트 전송 실패")
    entered, release = asyncio.Event(), asyncio.Event()

    async def cooldown() -> None:
        """완료 이벤트 전 쿨다운에서 종료 취소를 받는다."""
        entered.set()
        await release.wait()

    thermal.notify_job_completed.side_effect = cooldown
    task = asyncio.create_task(processor._process_job(job))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert (await queue.get_job(job.id)).status == JobStatus.COMPLETED.value
        assert queue.queue.get_batch_receipts("batch")[0]["events"][-1]["status"] == "queued"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        events = queue.queue.get_batch_receipts("batch")[0]["events"]
        assert [event["status"] for event in events] == ["queued", "completed"]
        assert events[-1]["status_label"] == "완료"
        thermal.notify_job_completed.assert_awaited_once()
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await queue.close()
    reopened = JobQueue(tmp_path / "pipeline.db")
    try:
        reopened.initialize()
        assert reopened.get_job(job.id).status == JobStatus.COMPLETED.value
        assert reopened.get_batch_receipts("batch")[0]["events"][-1]["status"] == "completed"
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("user_cancel", [False, True])
async def test_진짜_미완료_파이프라인의_종료와_사용자취소_계약은_유지한다(
    tmp_path: Path, user_cancel: bool
) -> None:
    """본체 대기 중 취소는 기존대로 queued 또는 recorded로 돌아간다."""
    processor, queue, job, pipeline, thermal, ws = await _setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def run(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        """모델 없이 아직 완료되지 않은 본체를 대기시킨다."""
        entered.set()
        await release.wait()
        return SimpleNamespace(status="completed")

    pipeline.run.side_effect = run
    task = asyncio.create_task(processor._process_job(job))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if user_cancel:
            processor.request_cancellation(job.meeting_id)
        task.cancel()
        if user_cancel:
            assert await asyncio.wait_for(task, timeout=5) is False
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        expected = JobStatus.RECORDED.value if user_cancel else JobStatus.QUEUED.value
        assert (await queue.get_job(job.id)).status == expected
        thermal.notify_job_completed.assert_awaited_once()
        events = [call.args[0].event_type for call in ws.broadcast_event.call_args_list]
        assert "job_completed" not in events
        assert ("job_cancelled" in events) is user_cancel
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await queue.close()
