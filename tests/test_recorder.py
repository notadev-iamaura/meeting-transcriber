"""
오디오 녹음 모듈 테스트 (Audio Recorder Module Tests)

목적: steps/recorder.py의 AudioRecorder 클래스를 검증한다.
주요 테스트:
    - 초기화 및 상태 기본값
    - 오디오 장치 감지 (ffmpeg 출력 파싱)
    - BlackHole 장치 감지
    - 녹음 시작 (ffmpeg 프로세스 시작)
    - 이중 녹음 시작 방지
    - 녹음 정지 (graceful 종료, 타임아웃 처리)
    - 파일 이동 (recordings_temp → audio_input)
    - 최소 시간 미달 파기
    - 콜백 호출 (동기/비동기)
    - 최대 시간 가드
    - 에러 계층 구조
의존성: pytest, asyncio, unittest.mock, config 모듈
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig, PathsConfig, RecordingConfig
from steps.recorder import (
    AlreadyRecordingError,
    AudioDevice,
    AudioDeviceError,
    AudioRecorder,
    FFmpegRecordError,
    RecorderError,
    RecordingResult,
    RecordingState,
)


# === 테스트 헬퍼 ===


def _make_test_config(tmp_path: Path) -> AppConfig:
    """테스트용 AppConfig를 생성한다."""
    return AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        recording=RecordingConfig(
            enabled=True,
            auto_record_on_zoom=True,
            prefer_system_audio=True,
            sample_rate=16000,
            channels=1,
            max_duration_seconds=14400,
            min_duration_seconds=5,
            ffmpeg_graceful_timeout_seconds=10,
        ),
    )


# ffmpeg -list_devices 출력 예시
_FFMPEG_DEVICE_OUTPUT = """\
[AVFoundation indev @ 0x7f8] AVFoundation video devices:
[AVFoundation indev @ 0x7f8] [0] FaceTime HD Camera
[AVFoundation indev @ 0x7f8] [1] Capture screen 0
[AVFoundation indev @ 0x7f8] AVFoundation audio devices:
[AVFoundation indev @ 0x7f8] [0] MacBook Air Microphone
[AVFoundation indev @ 0x7f8] [1] BlackHole 2ch
[AVFoundation indev @ 0x7f8] [2] External Microphone
"""

_FFMPEG_NO_BLACKHOLE_OUTPUT = """\
[AVFoundation indev @ 0x7f8] AVFoundation video devices:
[AVFoundation indev @ 0x7f8] [0] FaceTime HD Camera
[AVFoundation indev @ 0x7f8] AVFoundation audio devices:
[AVFoundation indev @ 0x7f8] [0] MacBook Air Microphone
"""


# === TestAudioRecorderInit ===


class TestAudioRecorderInit:
    """AudioRecorder 초기화 테스트."""

    def test_기본_초기화(self, tmp_path: Path) -> None:
        """기본 설정으로 초기화되고 상태가 IDLE이다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        assert recorder.state == RecordingState.IDLE
        assert recorder.is_recording is False
        assert recorder.current_duration == 0.0
        assert recorder.current_device_name == ""

    def test_비활성화_설정(self, tmp_path: Path) -> None:
        """recording.enabled=False일 때도 초기화 가능하다."""
        config = AppConfig(
            paths=PathsConfig(base_dir=str(tmp_path)),
            recording=RecordingConfig(enabled=False),
        )
        recorder = AudioRecorder(config=config)
        assert recorder.state == RecordingState.IDLE

    def test_ws_manager_없이_초기화(self, tmp_path: Path) -> None:
        """ws_manager 없이도 정상 초기화된다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config, ws_manager=None)
        assert recorder._ws_manager is None

    def test_get_status_idle(self, tmp_path: Path) -> None:
        """IDLE 상태에서 get_status()가 올바른 딕셔너리를 반환한다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)
        status = recorder.get_status()

        assert status["state"] == "idle"
        assert status["is_recording"] is False
        assert status["duration_seconds"] == 0.0
        assert status["meeting_id"] is None
        assert status["device"] is None


# === TestAudioDeviceDetection ===


class TestAudioDeviceDetection:
    """오디오 장치 감지 테스트."""

    def test_장치_파싱_blackhole_포함(self, tmp_path: Path) -> None:
        """BlackHole이 포함된 ffmpeg 출력을 올바르게 파싱한다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)
        devices = recorder._parse_device_list(_FFMPEG_DEVICE_OUTPUT)

        assert len(devices) == 3
        assert devices[0].name == "MacBook Air Microphone"
        assert devices[0].index == 0
        assert devices[0].is_blackhole is False

        assert devices[1].name == "BlackHole 2ch"
        assert devices[1].index == 1
        assert devices[1].is_blackhole is True

        assert devices[2].name == "External Microphone"
        assert devices[2].index == 2
        assert devices[2].is_blackhole is False

    def test_장치_파싱_blackhole_없음(self, tmp_path: Path) -> None:
        """BlackHole이 없는 출력에서 마이크만 감지한다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)
        devices = recorder._parse_device_list(_FFMPEG_NO_BLACKHOLE_OUTPUT)

        assert len(devices) == 1
        assert devices[0].name == "MacBook Air Microphone"
        assert devices[0].is_blackhole is False

    def test_빈_출력_파싱(self, tmp_path: Path) -> None:
        """빈 출력에서 장치 목록이 비어있다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)
        devices = recorder._parse_device_list("")

        assert devices == []

    @pytest.mark.asyncio
    async def test_ffmpeg_미설치_에러(self, tmp_path: Path) -> None:
        """ffmpeg가 설치되지 않으면 AudioDeviceError가 발생한다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            with pytest.raises(AudioDeviceError, match="ffmpeg"):
                await recorder.detect_audio_devices()

    @pytest.mark.asyncio
    async def test_blackhole_우선_선택(self, tmp_path: Path) -> None:
        """prefer_system_audio=True일 때 BlackHole이 우선 선택된다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        # detect_audio_devices를 모킹
        devices = [
            AudioDevice(index=0, name="MacBook Air Microphone", is_blackhole=False),
            AudioDevice(index=1, name="BlackHole 2ch", is_blackhole=True),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=devices):
            selected = await recorder._select_audio_device()

        assert selected.name == "BlackHole 2ch"
        assert selected.is_blackhole is True

    @pytest.mark.asyncio
    async def test_blackhole_없으면_기본_마이크(self, tmp_path: Path) -> None:
        """BlackHole이 없으면 첫 번째 장치(기본 마이크)가 선택된다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        devices = [
            AudioDevice(index=0, name="MacBook Air Microphone", is_blackhole=False),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=devices):
            selected = await recorder._select_audio_device()

        assert selected.name == "MacBook Air Microphone"

    @pytest.mark.asyncio
    async def test_장치_없으면_에러(self, tmp_path: Path) -> None:
        """사용 가능한 장치가 없으면 AudioDeviceError가 발생한다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        with patch.object(recorder, "detect_audio_devices", return_value=[]):
            with pytest.raises(AudioDeviceError, match="오디오 입력 장치"):
                await recorder._select_audio_device()


# === TestStartRecording ===


class TestStartRecording:
    """녹음 시작 테스트."""

    @pytest.mark.asyncio
    async def test_이중_시작_방지(self, tmp_path: Path) -> None:
        """이미 녹음 중이면 AlreadyRecordingError가 발생한다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)
        recorder._state = RecordingState.RECORDING

        with pytest.raises(AlreadyRecordingError, match="이미 녹음"):
            await recorder.start_recording()

    @pytest.mark.asyncio
    async def test_비활성화시_무시(self, tmp_path: Path) -> None:
        """recording.enabled=False이면 녹음 시작이 무시된다."""
        config = AppConfig(
            paths=PathsConfig(base_dir=str(tmp_path)),
            recording=RecordingConfig(enabled=False),
        )
        recorder = AudioRecorder(config=config)
        await recorder.start_recording()

        assert recorder.state == RecordingState.IDLE

    @pytest.mark.asyncio
    async def test_녹음_시작_성공(self, tmp_path: Path) -> None:
        """정상적으로 녹음이 시작되면 상태가 RECORDING으로 변경된다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        # 장치 선택 모킹
        mock_device = AudioDevice(index=0, name="Test Mic", is_blackhole=False)
        mock_process = AsyncMock()
        mock_process.pid = 12345
        mock_process.stdin = MagicMock()
        mock_process.wait = AsyncMock()

        with patch.object(recorder, "_select_audio_device", return_value=mock_device):
            with patch("asyncio.create_subprocess_exec", return_value=mock_process):
                await recorder.start_recording(meeting_id="test_meeting")

        assert recorder.state == RecordingState.RECORDING
        assert recorder.is_recording is True
        assert recorder._meeting_id == "test_meeting"
        assert recorder.current_device_name == "Test Mic"

        # 정리
        recorder._state = RecordingState.IDLE
        if recorder._max_duration_task:
            recorder._max_duration_task.cancel()
        if recorder._duration_broadcast_task:
            recorder._duration_broadcast_task.cancel()

    @pytest.mark.asyncio
    async def test_meeting_id_자동생성(self, tmp_path: Path) -> None:
        """meeting_id를 지정하지 않으면 타임스탬프로 자동 생성된다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        mock_device = AudioDevice(index=0, name="Test Mic", is_blackhole=False)
        mock_process = AsyncMock()
        mock_process.pid = 12345
        mock_process.stdin = MagicMock()
        mock_process.wait = AsyncMock()

        with patch.object(recorder, "_select_audio_device", return_value=mock_device):
            with patch("asyncio.create_subprocess_exec", return_value=mock_process):
                await recorder.start_recording()

        assert recorder._meeting_id is not None
        assert recorder._meeting_id.startswith("meeting_")

        # 정리
        recorder._state = RecordingState.IDLE
        if recorder._max_duration_task:
            recorder._max_duration_task.cancel()
        if recorder._duration_broadcast_task:
            recorder._duration_broadcast_task.cancel()


# === TestStopRecording ===


class TestStopRecording:
    """녹음 정지 테스트."""

    @pytest.mark.asyncio
    async def test_녹음중_아닐때_정지(self, tmp_path: Path) -> None:
        """녹음 중이 아닐 때 정지하면 None을 반환한다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)
        result = await recorder.stop_recording()

        assert result is None

    @pytest.mark.asyncio
    async def test_최소시간_미달_파기(self, tmp_path: Path) -> None:
        """녹음 시간이 최소 시간보다 짧으면 파일을 파기한다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        # 상태 설정 (1초만 녹음)
        import time
        recorder._state = RecordingState.RECORDING
        recorder._start_time = time.time() - 1  # 1초 전
        temp_file = tmp_path / "recordings_temp" / "test.wav"
        temp_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file.write_bytes(b"test audio data")
        recorder._current_file = temp_file
        recorder._current_device = AudioDevice(index=0, name="Mic", is_blackhole=False)
        recorder._meeting_id = "test"

        # ffmpeg 프로세스 모킹
        mock_process = AsyncMock()
        mock_process.stdin = MagicMock()
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.drain = AsyncMock()
        mock_process.wait = AsyncMock()
        recorder._process = mock_process

        result = await recorder.stop_recording()

        assert result is None
        assert not temp_file.exists()  # 파일이 삭제됨
        assert recorder.state == RecordingState.IDLE

    @pytest.mark.asyncio
    async def test_정상_정지_파일이동(self, tmp_path: Path) -> None:
        """정상 정지 시 파일이 audio_input으로 이동된다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        # 상태 설정 (10초 녹음)
        import time
        recorder._state = RecordingState.RECORDING
        recorder._start_time = time.time() - 10  # 10초 전
        temp_file = tmp_path / "recordings_temp" / "test.wav"
        temp_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file.write_bytes(b"x" * 1000)  # 비어있지 않은 파일
        recorder._current_file = temp_file
        recorder._current_device = AudioDevice(index=0, name="Mic", is_blackhole=False)
        recorder._meeting_id = "test"

        # ffmpeg 프로세스 모킹
        mock_process = AsyncMock()
        mock_process.stdin = MagicMock()
        mock_process.stdin.write = MagicMock()
        mock_process.stdin.drain = AsyncMock()
        mock_process.wait = AsyncMock()
        recorder._process = mock_process

        result = await recorder.stop_recording()

        assert result is not None
        assert result.file_path == tmp_path / "audio_input" / "test.wav"
        assert result.duration_seconds >= 9.0
        assert result.audio_device == "Mic"
        assert result.file_size_bytes == 1000
        assert recorder.state == RecordingState.IDLE


# === TestRecordingCallbacks ===


class TestRecordingCallbacks:
    """녹음 콜백 테스트."""

    def test_동기_콜백_등록(self, tmp_path: Path) -> None:
        """동기 콜백이 등록된다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        def callback(result: RecordingResult) -> None:
            pass

        recorder.on_recording_complete(callback)
        assert len(recorder._sync_callbacks) == 1

    def test_비동기_콜백_등록(self, tmp_path: Path) -> None:
        """비동기 콜백이 등록된다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        async def callback(result: RecordingResult) -> None:
            pass

        recorder.on_recording_complete(callback)
        assert len(recorder._async_callbacks) == 1

    @pytest.mark.asyncio
    async def test_콜백_호출(self, tmp_path: Path) -> None:
        """녹음 완료 시 등록된 콜백이 호출된다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        callback_called = False
        callback_result = None

        async def on_complete(result: RecordingResult) -> None:
            nonlocal callback_called, callback_result
            callback_called = True
            callback_result = result

        recorder.on_recording_complete(on_complete)

        # 녹음 결과 모킹
        result = RecordingResult(
            file_path=Path("/tmp/test.wav"),
            duration_seconds=30.0,
            audio_device="Test Mic",
            started_at="2026-03-05T10:00:00",
            ended_at="2026-03-05T10:00:30",
            file_size_bytes=480000,
        )

        await recorder._fire_callbacks(result)

        assert callback_called is True
        assert callback_result is not None
        assert callback_result.duration_seconds == 30.0


# === TestMaxDurationGuard ===


class TestMaxDurationGuard:
    """최대 녹음 시간 가드 테스트."""

    @pytest.mark.asyncio
    async def test_가드_태스크_취소(self, tmp_path: Path) -> None:
        """녹음 정지 시 가드 태스크가 취소된다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        task = asyncio.create_task(recorder._max_duration_guard())
        await asyncio.sleep(0.01)

        assert not task.done()

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert task.done()


# === TestErrorHierarchy ===


class TestErrorHierarchy:
    """에러 상속 구조 테스트."""

    def test_AlreadyRecordingError_상속(self) -> None:
        """AlreadyRecordingError는 RecorderError의 하위 클래스이다."""
        assert issubclass(AlreadyRecordingError, RecorderError)

    def test_FFmpegRecordError_상속(self) -> None:
        """FFmpegRecordError는 RecorderError의 하위 클래스이다."""
        assert issubclass(FFmpegRecordError, RecorderError)

    def test_AudioDeviceError_상속(self) -> None:
        """AudioDeviceError는 RecorderError의 하위 클래스이다."""
        assert issubclass(AudioDeviceError, RecorderError)

    def test_RecorderError는_Exception_상속(self) -> None:
        """RecorderError는 Exception의 하위 클래스이다."""
        assert issubclass(RecorderError, Exception)

    def test_에러_메시지(self) -> None:
        """에러 인스턴스에서 메시지가 올바르게 전달된다."""
        error = AlreadyRecordingError("이미 녹음 중")
        assert str(error) == "이미 녹음 중"


# === TestAudioDevice ===


class TestAudioDevice:
    """AudioDevice 데이터 클래스 테스트."""

    def test_기본_생성(self) -> None:
        """기본값으로 생성된다."""
        device = AudioDevice(index=0, name="Test Mic")
        assert device.index == 0
        assert device.name == "Test Mic"
        assert device.is_blackhole is False

    def test_blackhole_장치(self) -> None:
        """BlackHole 장치가 올바르게 생성된다."""
        device = AudioDevice(index=1, name="BlackHole 2ch", is_blackhole=True)
        assert device.is_blackhole is True

    def test_to_dict(self) -> None:
        """딕셔너리 변환이 올바르다."""
        device = AudioDevice(index=0, name="Mic", is_blackhole=False)
        d = device.to_dict()
        assert d == {"index": 0, "name": "Mic", "is_blackhole": False}


# === TestRecordingState ===


class TestRecordingState:
    """RecordingState 열거형 테스트."""

    def test_상태값(self) -> None:
        """상태 값이 올바르다."""
        assert RecordingState.IDLE.value == "idle"
        assert RecordingState.RECORDING.value == "recording"
        assert RecordingState.STOPPING.value == "stopping"

    def test_문자열_비교(self) -> None:
        """문자열로 비교 가능하다."""
        assert RecordingState.IDLE == "idle"
        assert RecordingState.RECORDING == "recording"
