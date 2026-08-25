"""Zoom 보호용 화자분리 process guard 테스트."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from steps.diarization_process_guard import WorkerSupervisionTimeout, ZoomPauseGuard


class FakeProcess:
    """ZoomPauseGuard 테스트용 fake process."""

    def __init__(self, poll_results: list[int | None]) -> None:
        self.pid = 12345
        self._poll_results = poll_results
        self.killed = False

    def poll(self) -> int | None:
        if self._poll_results:
            return self._poll_results.pop(0)
        return None

    def kill(self) -> None:
        self.killed = True


class RecordingGuard(ZoomPauseGuard):
    """Zoom active 상태와 pause/resume 호출을 기록하는 guard."""

    def __init__(self, active_results: list[bool]) -> None:
        super().__init__(process_name="CptHost", poll_interval_seconds=0.5)
        self._active_results = active_results
        self.paused: list[int] = []
        self.resumed: list[int] = []

    async def is_zoom_active(self) -> bool:
        if self._active_results:
            return self._active_results.pop(0)
        return False

    def pause(self, pid: int) -> None:
        self.paused.append(pid)

    def resume(self, pid: int) -> None:
        self.resumed.append(pid)


@pytest.mark.asyncio
async def test_supervise_pauses_and_resumes_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zoom active 동안 worker를 멈추고 종료 전 재개한다."""
    guard = RecordingGuard(active_results=[True, False])
    process = FakeProcess(poll_results=[None, None, 0])
    sleep = AsyncMock()
    monkeypatch.setattr("steps.diarization_process_guard.asyncio.sleep", sleep)

    returncode = await guard.supervise(process, timeout_seconds=60)

    assert returncode == 0
    assert guard.paused == [process.pid]
    assert guard.resumed == [process.pid]
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_wait_until_idle_defers_while_zoom_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zoom active 상태면 worker 시작 전 대기한다."""
    guard = RecordingGuard(active_results=[True, True, False])
    sleep = AsyncMock()
    monkeypatch.setattr("steps.diarization_process_guard.asyncio.sleep", sleep)

    await guard.wait_until_idle()

    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_timeout은_실행_Zoom_pause_전체경과를_구분해_보존한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker timeout 진단은 Zoom 일시정지를 실행 예산에서 제외해 기록한다."""
    guard = RecordingGuard(active_results=[True, False, False])
    process = FakeProcess(poll_results=[None, None, None, None])
    sleep = AsyncMock()
    monotonic_values = iter([0.0, 0.0, 0.5, 1.5, 2.5, 3.0, 3.0])
    monkeypatch.setattr("steps.diarization_process_guard.asyncio.sleep", sleep)
    guard._clock = lambda: next(monotonic_values)

    with pytest.raises(WorkerSupervisionTimeout) as exc_info:
        await guard.supervise(process, timeout_seconds=1)

    diagnostic = exc_info.value
    assert process.killed is True
    assert diagnostic.timeout_seconds == 1
    assert diagnostic.active_elapsed_seconds == pytest.approx(1.0)
    assert diagnostic.paused_elapsed_seconds == pytest.approx(2.0)
    assert diagnostic.wall_elapsed_seconds == pytest.approx(3.0)
    assert "실제 실행=1.0초" in str(diagnostic)
    assert "Zoom 일시정지=2.0초" in str(diagnostic)
    assert sleep.await_count == 2
