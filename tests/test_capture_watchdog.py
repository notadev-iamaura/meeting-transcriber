"""실제 합성 캡처 프로세스로 오류 알림·부분 파일·정지 경쟁을 검증한다."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from config import AppConfig, PathsConfig, RecordingConfig
from steps.recorder import AudioRecorder, FFmpegRecordError, RecordingState

_CAPTURE = """
import pathlib, select, sys, time, wave
target, gate, mode = sys.argv[1:]
if mode == 'finalize':
    output = wave.open(target, 'wb')
    output.setparams((2, 2, 16000, 0, 'NONE', 'not compressed'))
    output.writeframesraw(b'\\x00\\x01\\x00\\x02' * 1600)
    output.writeframesraw(b'\\x00\\x03\\x00\\x04' * 1600)
else:
    output = wave.open(target, 'wb')
    output.setparams((2, 2, 16000, 0, 'NONE', 'not compressed'))
    output.writeframes(b'\\x00\\x01\\x00\\x02' * 1600)
    output.close()
print('ready', flush=True)
def meter():
    print('frame:1\\nlavfi.astats.1.RMS_level=-inf\\nlavfi.astats.2.RMS_level=-inf', flush=True)
meter()
def native_failure():
    print('raw stderr: /private/synthetic-path authorization=synthetic-token', file=sys.stderr)
    print('x' * 70000, file=sys.stderr)
    print('capture-failure={"stage":"/private/synthetic-path","error_domain":"RecapCapture",'
          '"error_code":4}', file=sys.stderr)
    print('capture-failure={"stage":"input_layout","error_domain":"RecapCapture",'
          '"error_code":3,"last_input_frame_ms":1735689600123}', file=sys.stderr)
    print('capture-failure={"stage":"wav_finalize","error_domain":"NSCocoaErrorDomain",'
          '"error_code":512}', file=sys.stderr)
    print('capture-error=layout', flush=True)
    sys.exit(2)
while True:
    if mode == 'native-failure' and pathlib.Path(gate).exists():
        native_failure()
    if mode.startswith('exit') and pathlib.Path(gate).exists():
        sys.exit(int(mode[4:]))
    if select.select([sys.stdin], [], [], 0.02)[0]:
        if sys.stdin.buffer.read(1) in (b'q', b''):
            if mode == 'late-native-failure':
                native_failure()
            if mode == 'finalize':
                output.close()
            sys.exit(0)
    if mode == 'silent':
        meter()
"""


async def _start_capture(recorder: AudioRecorder, name: str, mode: str) -> None:
    """오디오 장치 대신 임시 WAV와 정상 또는 고장난 프레임 스트림을 만든다."""
    target = recorder._temp_dir / f"{name}.wav"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _CAPTURE,
        str(target),
        str(recorder._temp_dir / "fail-now"),
        mode,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert await asyncio.wait_for(proc.stdout.readline(), timeout=5) == b"ready\n"
    recorder._process = proc
    recorder._current_file = target
    recorder._split_level_meter = True
    recorder._start_pipe_drain(proc, split_meter=True)


def _recorder(tmp_path: Path) -> AudioRecorder:
    """짧은 프레임 장애 감지 예산을 가진 격리 녹음기를 만든다."""
    return AudioRecorder(
        config=AppConfig(
            paths=PathsConfig(base_dir=str(tmp_path)),
            recording=RecordingConfig(capture_stall_timeout_seconds=1),
        )
    )


async def _wait_idle(recorder: AudioRecorder) -> None:
    """합성 캡처가 고아 프로세스 없이 종료됐는지 제한 시간 안에 확인한다."""
    async with asyncio.timeout(5):
        while recorder.state != RecordingState.IDLE:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exit0", "exit1", "exit2", "stall"])
@pytest.mark.parametrize("duration", [1, 31])
async def test_failure_without_stop_preserves_audio_and_reports_once(
    tmp_path: Path, mode: str, duration: int
) -> None:
    """사용자 정지 없이도 프로세스 종료·입력 단절을 실패로 표시하고 원본을 보존한다."""
    recorder = _recorder(tmp_path)
    event = AsyncMock()
    complete = AsyncMock()
    notification = Mock()
    recorder.on_recording_complete(complete)
    recorder.on_capture_failure(notification)

    async def start(name: str) -> None:
        await _start_capture(recorder, name, mode)

    with (
        patch.object(recorder, "_start_singletrack_recording", side_effect=start),
        patch.object(recorder, "_broadcast_event", event),
        patch.object(recorder, "_check_audio_energy") as energy,
    ):
        await recorder.start_recording("capture")
        recorder._start_time = time.time() - duration
        partial = recorder._current_file
        proc = recorder._process
        assert partial is not None and proc is not None
        original = partial.read_bytes()
        try:
            (recorder._temp_dir / "fail-now").touch()
            await _wait_idle(recorder)
            with pytest.raises(FFmpegRecordError):
                await recorder.stop_recording()
            status = recorder.get_status()
            failures = [
                call.args[1] for call in event.await_args_list if call.args[0] == "recording_error"
            ]
            assert len(failures) == 1
            assert status["last_error"] == failures[0]
            assert status["last_error"]["partial_files_preserved"] is True
            assert status["last_error"]["partial_file_names"] == ["capture.wav"]
            assert str(tmp_path) not in str(status["last_error"])
            assert status["meeting_id"] is None
            assert status["audio_levels"] is None
            assert status["duration_seconds"] == 0
            assert recorder._max_duration_task is None
            assert recorder._duration_broadcast_task is None
            assert recorder._capture_watchdog_task is None
            assert recorder._pipe_drain_tasks == []
            assert proc.returncode is not None
            assert partial.read_bytes() == original
            assert not list(recorder._audio_input_dir.glob("*.wav"))
            assert not any(call.args[0] == "recording_stopped" for call in event.await_args_list)
            count = event.await_count
            await asyncio.sleep(0.55)
            assert event.await_count == count
            complete.assert_not_awaited()
            energy.assert_not_called()
            notification.assert_called_once_with()
        finally:
            await recorder.cleanup()


@pytest.mark.parametrize("stop_path", ["watchdog", "duration-limit", "late-diagnostic"])
def test_native_exit_two_under_uvloop_logs_first_failure_and_preserves_wav(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stop_path: str
) -> None:
    """실제 uvloop 자식이 종료해도 최초 진단과 부분 WAV가 남고 알림은 한 번만 간다."""
    uvloop = pytest.importorskip("uvloop")

    async def exercise() -> None:
        recorder = _recorder(tmp_path)
        if stop_path == "duration-limit":
            recorder._max_duration = 1
        event, complete, notification = AsyncMock(), AsyncMock(), Mock()
        recorder.on_recording_complete(complete)
        recorder.on_capture_failure(notification)
        gate = recorder._temp_dir / "fail-now"
        command = AsyncMock(
            side_effect=lambda output, _: [
                sys.executable,
                "-c",
                _CAPTURE,
                str(output),
                str(gate),
                "late-native-failure" if stop_path == "late-diagnostic" else "native-failure",
            ]
        )
        unhandled: list[dict[str, object]] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _, context: unhandled.append(context)
        )
        with (
            patch("steps.recorder.use_zoom_tap", return_value=True),
            patch("steps.recorder.ZoomAudioActivityChecker.tap_command", command),
            patch.object(recorder, "_broadcast_event", event),
        ):
            await recorder.start_recording("native-failure", zoom_meeting=True)
            proc, partial = recorder._process, recorder._current_file
            watcher = recorder._capture_watchdog_task
            assert proc is not None and proc.stdin is not None and partial is not None
            assert watcher is not None
            original = partial.read_bytes()
            try:
                stopper = watcher
                if stop_path == "duration-limit":
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
                    stopper = recorder._max_duration_task
                    assert stopper is not None
                with patch.object(proc.stdin, "write", wraps=proc.stdin.write) as write:
                    gate.touch()
                    await asyncio.wait_for(stopper, timeout=5)
                    if stop_path == "late-diagnostic":
                        write.assert_called_once_with(b"q")
                    else:
                        write.assert_not_called()
                assert proc.returncode == 2 and recorder.state == RecordingState.IDLE
                assert partial.read_bytes() == original
                assert not list(recorder._audio_input_dir.glob("*.wav"))
                assert (
                    len([c for c in event.await_args_list if c.args[0] == "recording_error"]) == 1
                )
                assert recorder.get_status()["last_error"]["partial_files_preserved"] is True
                notification.assert_called_once_with()
                complete.assert_not_awaited()
            finally:
                await recorder.cleanup()
        assert not unhandled

    uvloop.run(exercise())
    diagnostics = [
        r.getMessage() for r in caplog.records if "CoreAudio 녹음 실패 진단" in r.message
    ]
    assert len(diagnostics) == 1
    assert "stage=input_layout" in diagnostics[0]
    assert "error_domain=RecapCapture" in diagnostics[0]
    assert "error_code=3" in diagnostics[0]
    assert "exit_code=2" in diagnostics[0]
    assert "last_input_frame_at=2025-01-01T00:00:00.123+00:00" in diagnostics[0]
    assert "wav_finalize" not in caplog.text
    assert "synthetic-path" not in caplog.text and "synthetic-token" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("closed_at", ["before_write", "write", "drain"])
async def test_stop_handles_stdin_transport_closing_during_q(
    tmp_path: Path, closed_at: str
) -> None:
    """자식 종료 통지가 늦게 도착해도 닫힌 입력을 일반 녹음 실패로 처리한다."""
    recorder = _recorder(tmp_path)
    stdin = Mock(spec=asyncio.StreamWriter)
    stdin.is_closing.return_value = closed_at == "before_write"
    stdin.drain = AsyncMock()
    if closed_at == "write":
        stdin.write.side_effect = RuntimeError("unable to perform operation on a closed handler")
    if closed_at == "drain":
        stdin.drain.side_effect = RuntimeError("unable to perform operation on a closed handler")
    proc = Mock(
        spec=asyncio.subprocess.Process,
        returncode=None,
        stdin=stdin,
        wait=AsyncMock(return_value=2),
    )

    with pytest.raises(FFmpegRecordError, match="정지 요청 전 종료 코드 2"):
        await recorder._terminate_one_ffmpeg(proc, "recording")
    if closed_at == "before_write":
        stdin.write.assert_not_called()
    proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_silence_frames_stay_recording_and_successful_restart_clears_error(
    tmp_path: Path,
) -> None:
    """무음은 프레임 단절이 아니며 성공한 새 녹음에서만 이전 오류를 지운다."""
    recorder = _recorder(tmp_path)
    recorder._last_recording_error = {"error": "이전 녹음 실패"}

    async def start(name: str) -> None:
        await _start_capture(recorder, name, "silent")

    with patch.object(recorder, "_start_singletrack_recording", side_effect=start):
        await recorder.start_recording("silence")
        try:
            await asyncio.sleep(1.3)
            assert recorder.is_recording
            assert recorder.get_status()["last_error"] is None
            assert recorder.get_status()["audio_levels"] == {
                "mic_dbfs": -90.0,
                "system_dbfs": -90.0,
            }
        finally:
            await recorder.cleanup()


@pytest.mark.asyncio
async def test_stop_race_cannot_publish_already_exited_zero_capture(tmp_path: Path) -> None:
    """감시 루프보다 정지가 먼저 와도 예상치 않은 코드 0 종료를 성공으로 게시하지 않는다."""
    recorder = _recorder(tmp_path)
    complete = AsyncMock()
    recorder.on_recording_complete(complete)

    async def start(name: str) -> None:
        await _start_capture(recorder, name, "exit0")

    with patch.object(recorder, "_start_singletrack_recording", side_effect=start):
        await recorder.start_recording("race")
        proc = recorder._process
        partial = recorder._current_file
        assert proc is not None and partial is not None
        original = partial.read_bytes()
        (recorder._temp_dir / "fail-now").touch()
        await asyncio.wait_for(proc.wait(), timeout=5)
        with pytest.raises(FFmpegRecordError):
            await recorder.stop_recording()
        await _wait_idle(recorder)
        assert partial.read_bytes() == original
        complete.assert_not_awaited()
        assert recorder.get_status()["last_error"]["code"] == "capture_failed"


@pytest.mark.asyncio
async def test_exit_between_stop_transition_and_q_is_not_published(tmp_path: Path) -> None:
    """stop이 시작된 뒤 가드 정리 await 중 캡처가 끝나도 q 직전 재검사로 게시를 막는다."""
    recorder = _recorder(tmp_path)
    complete = AsyncMock()
    recorder.on_recording_complete(complete)

    async def start(name: str) -> None:
        await _start_capture(recorder, name, "exit0")

    with patch.object(recorder, "_start_singletrack_recording", side_effect=start):
        await recorder.start_recording("late-exit")
        proc = recorder._process
        partial = recorder._current_file
        assert proc is not None and partial is not None
        original = partial.read_bytes()
        cancel_background = recorder._cancel_background_tasks

        async def exit_while_cancelling(*, _from_guard: bool) -> None:
            (recorder._temp_dir / "fail-now").touch()
            await asyncio.wait_for(proc.wait(), timeout=5)
            await cancel_background(_from_guard=_from_guard)

        with patch.object(recorder, "_cancel_background_tasks", side_effect=exit_while_cancelling):
            with pytest.raises(FFmpegRecordError, match="정지 요청 전 종료 코드 0"):
                await recorder.stop_recording()
        assert recorder.state == RecordingState.IDLE
        assert partial.read_bytes() == original
        complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_multitrack_failure_stops_survivor_and_preserves_both_tracks(tmp_path: Path) -> None:
    """한 트랙이 실패하면 나머지는 정상 종료하되 두 파일 모두 자동 게시하지 않는다."""
    recorder = _recorder(tmp_path)
    recorder._multi_track = True
    complete = AsyncMock()
    recorder.on_recording_complete(complete)
    processes = []

    async def start(_: str) -> None:
        for label, mode in (("mic", "exit2"), ("system", "silent")):
            await _start_capture(recorder, label, mode)
            assert recorder._process is not None and recorder._current_file is not None
            recorder._processes[label] = recorder._process
            recorder._current_files[label] = recorder._current_file
            processes.append(recorder._process)
        recorder._process = None
        recorder._current_file = None
        recorder._split_level_meter = False

    with patch.object(recorder, "_start_multitrack_recording", side_effect=start):
        await recorder.start_recording("multi")
        originals = {path: path.read_bytes() for path in recorder._current_files.values()}
        try:
            (recorder._temp_dir / "fail-now").touch()
            await _wait_idle(recorder)
            assert processes[0].returncode == 2
            assert processes[1].returncode == 0
            assert all(path.read_bytes() == content for path, content in originals.items())
            assert recorder.get_status()["last_error"]["partial_file_names"] == [
                "mic.wav",
                "system.wav",
            ]
            complete.assert_not_awaited()
        finally:
            await recorder.cleanup()


@pytest.mark.asyncio
async def test_failure_is_reported_before_slow_cleanup_and_cancellation_cannot_publish(
    tmp_path: Path,
) -> None:
    """종료 대기 전 오류를 알리고, 감시 태스크 취소·동시 stop에도 보존 정리를 끝낸다."""
    recorder = _recorder(tmp_path)
    reported = asyncio.Event()
    terminating = asyncio.Event()
    release = asyncio.Event()
    complete = AsyncMock()
    recorder.on_recording_complete(complete)
    notification = Mock(side_effect=RuntimeError("알림 실패"))
    recorder.on_capture_failure(notification)
    events: list[str] = []
    original_terminate = recorder._terminate_one_ffmpeg

    async def start(name: str) -> None:
        await _start_capture(recorder, name, "stall")

    async def broadcast(event: str, _: object) -> None:
        events.append(event)
        if event == "recording_error":
            reported.set()

    async def terminate(proc: asyncio.subprocess.Process, label: str) -> None:
        terminating.set()
        await release.wait()
        await original_terminate(proc, label)

    with (
        patch.object(recorder, "_start_singletrack_recording", side_effect=start),
        patch.object(recorder, "_broadcast_event", side_effect=broadcast),
        patch.object(recorder, "_terminate_one_ffmpeg", side_effect=terminate),
    ):
        await recorder.start_recording("cancelled")
        watchdog = recorder._capture_watchdog_task
        partial = recorder._current_file
        proc = recorder._process
        assert watchdog is not None and partial is not None and proc is not None
        original = partial.read_bytes()
        try:
            await asyncio.wait_for(reported.wait(), timeout=3)
            await asyncio.wait_for(terminating.wait(), timeout=3)
            assert recorder.state == RecordingState.STOPPING
            assert recorder._duration_broadcast_task is None
            assert recorder.get_status()["last_error"]["code"] == "capture_failed"
            assert proc.returncode is None
            stop = asyncio.create_task(recorder.stop_recording())
            watchdog.cancel()
            await asyncio.sleep(0)
            assert not stop.done()
            assert not watchdog.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await watchdog
            with pytest.raises(FFmpegRecordError):
                await stop
            assert recorder.state == RecordingState.IDLE
            assert partial.read_bytes() == original
            assert events.count("recording_error") == 1
            assert "recording_stopped" not in events
            notification.assert_called_once_with()
            complete.assert_not_awaited()
        finally:
            release.set()
            await recorder.cleanup()


@pytest.mark.asyncio
async def test_manual_stop_wins_failure_race_and_still_finalizes_surviving_track(
    tmp_path: Path,
) -> None:
    """한쪽 오류 뒤 stop이 먼저 lock을 얻어도 살아 있는 WAV의 헤더는 q로 마감한다."""
    import wave

    recorder = _recorder(tmp_path)
    recorder._multi_track = True
    completed = AsyncMock()
    event = AsyncMock()
    notification = Mock()
    recorder.on_recording_complete(completed)
    recorder.on_capture_failure(notification)

    async def start(_: str) -> None:
        for label, mode in (("mic", "exit2"), ("system", "finalize")):
            await _start_capture(recorder, label, mode)
            assert recorder._process is not None and recorder._current_file is not None
            recorder._processes[label] = recorder._process
            recorder._current_files[label] = recorder._current_file
        recorder._process = None
        recorder._current_file = None
        recorder._split_level_meter = False

    with (
        patch.object(recorder, "_start_multitrack_recording", side_effect=start),
        patch.object(recorder, "_broadcast_event", event),
    ):
        await recorder.start_recording("manual-race")
        mic = recorder._processes["mic"]
        survivor = recorder._processes["system"]
        partial = recorder._current_files["system"]
        try:
            async with recorder._state_lock:
                # FIFO lock 대기열에 수동 stop을 먼저 등록한 뒤 캡처를 실패시킨다.
                stop = asyncio.create_task(recorder.stop_recording())
                await asyncio.sleep(0)
                (recorder._temp_dir / "fail-now").touch()
                await asyncio.wait_for(mic.wait(), timeout=5)
                with wave.open(str(partial)) as audio:
                    assert audio.getnframes() == 1600
            with pytest.raises(FFmpegRecordError):
                await stop
            assert survivor.returncode == 0
            with wave.open(str(partial)) as audio:
                assert audio.getnframes() == 3200
                assert audio.readframes(3200) == (
                    b"\x00\x01\x00\x02" * 1600 + b"\x00\x03\x00\x04" * 1600
                )
            assert recorder.state == RecordingState.IDLE
            assert [call.args[0] for call in event.await_args_list].count("recording_error") == 1
            notification.assert_called_once_with()
            completed.assert_not_awaited()
            assert not list(recorder._audio_input_dir.glob("*.wav"))
        finally:
            await recorder.cleanup()
