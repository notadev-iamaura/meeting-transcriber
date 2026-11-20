"""기기 선택·권한 실패와 출력 재연결 중 Zoom 녹음 세션의 수명을 검증한다."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from config import AppConfig, PathsConfig, RecordingConfig
from security.setup_readiness import check_audio_devices
from steps.recorder import AudioDeviceError, AudioRecorder
from steps.zoom_activity import ZoomActivityResult, ZoomAudioActivityChecker, use_zoom_tap
from steps.zoom_detector import ZoomDetector


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    """OS와 데이터 경로를 고정하고 실제 권한 창은 열지 않는다."""
    monkeypatch.setattr("steps.zoom_activity.supports_zoom_tap", lambda: True)
    return AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))


@pytest.mark.parametrize("auto", [True, False])
async def test_zoom_uses_tap_without_blackhole(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch, auto: bool
) -> None:
    """Zoom 자동 시작과 직접 시작이 동일한 분리 녹음기를 연다."""
    recorder = AudioRecorder(config)
    selected = AsyncMock(side_effect=AssertionError("가상 장치 탐색"))
    monkeypatch.setattr(recorder, "_select_audio_device", selected)
    monkeypatch.setattr(ZoomAudioActivityChecker, "is_active", AsyncMock(return_value=True))
    script = (
        "import sys\n"
        "print('frame:1\\nlavfi.astats.1.RMS_level=-20\\nlavfi.astats.2.RMS_level=-12',flush=True)\n"
        "sys.stdin.buffer.read(1)\n"
    )
    command = AsyncMock(return_value=[sys.executable, "-c", script])
    monkeypatch.setattr(ZoomAudioActivityChecker, "tap_command", command)
    await recorder.start_recording("test_zoom", zoom_meeting=auto)
    try:
        status = recorder.get_status()
        assert status["is_recording"] and status["is_system_audio"]
        assert status["audio_levels"] == {"mic_dbfs": -20, "system_dbfs": -12}
    finally:
        await recorder.stop_recording()
    selected.assert_not_awaited()
    command.assert_awaited_once()


async def test_manual_recording_with_idle_zoom_uses_normal_input(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zoom을 백그라운드에 열어 둔 채 다른 회의를 수동 녹음할 수 있다."""
    recorder = AudioRecorder(config)
    monkeypatch.setattr(ZoomAudioActivityChecker, "is_active", AsyncMock(return_value=False))
    selected = AsyncMock(side_effect=AudioDeviceError("기존 입력 조회"))
    monkeypatch.setattr(recorder, "_select_audio_device", selected)
    with pytest.raises(AudioDeviceError, match="기존 입력 조회"):
        await recorder.start_recording("manual")
    selected.assert_awaited_once()


async def test_denial_preserves_error_and_releases_recorder(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """권한 거부를 UI에 남기고 마이크만 녹음 완료로 오인하지 않는다."""
    recorder = AudioRecorder(config)
    cmd = [sys.executable, "-c", "print('capture-error=permission'); raise SystemExit(2)"]
    monkeypatch.setattr(ZoomAudioActivityChecker, "tap_command", AsyncMock(return_value=cmd))
    with pytest.raises(AudioDeviceError, match="마이크 권한"):
        await recorder.start_recording("test_zoom", zoom_meeting=True)
    assert recorder.get_status()["state"] == "idle"
    assert "마이크 권한" in recorder.get_status()["last_error"]["error"]
    assert recorder._process is None


def test_capture_selection_and_setup(config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """기본 준비 화면과 명시적 기존 입력 설정의 우선순위를 확인한다."""
    monkeypatch.setattr("security.setup_readiness.sys.platform", "darwin")
    monkeypatch.setattr("security.setup_readiness.shutil.which", lambda _: "/mock/ffmpeg")
    monkeypatch.setattr(
        "security.setup_readiness._load_avfoundation_devices", lambda _: ("MacBook Air mic", "")
    )
    monkeypatch.setattr("security.setup_readiness.get_aggregate_device_names", lambda **_: set())
    readiness = check_audio_devices(config)
    assert readiness.ready and readiness.details["selected_mode"] == "zoom_process_tap"
    assert not readiness.details["has_blackhole"] and not readiness.details["permissions_verified"]
    assert use_zoom_tap(config.recording)
    for override in (
        {"multi_track": True},
        {"preferred_device_name": "USB Input"},
        {"prefer_system_audio": False},
        {"zoom_process_tap": False},
    ):
        assert not use_zoom_tap(RecordingConfig.model_validate(override))


async def test_brief_zoom_io_restart_keeps_session(config: AppConfig) -> None:
    """한 번의 I/O 공백은 복구되고 연속 비활성 표본에서만 회의를 종료한다."""
    detector = ZoomDetector(config)
    await detector._handle_state_change(True)
    detector._activity_checker.check = AsyncMock(
        side_effect=[
            ZoomActivityResult(value, "coreaudio") for value in [False, True, False, False]
        ]
    )
    assert [await detector._check_zoom_process() for _ in range(4)] == [True, True, True, False]
