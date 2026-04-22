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
import contextlib
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

# Aggregate Device 포함 출력 (본인 마이크 + BlackHole 통합 시나리오)
_FFMPEG_AGGREGATE_OUTPUT = """\
[AVFoundation indev @ 0x7f8] AVFoundation video devices:
[AVFoundation indev @ 0x7f8] [0] FaceTime HD Camera
[AVFoundation indev @ 0x7f8] AVFoundation audio devices:
[AVFoundation indev @ 0x7f8] [0] MacBook Air Microphone
[AVFoundation indev @ 0x7f8] [1] BlackHole 2ch
[AVFoundation indev @ 0x7f8] [2] Meeting Transcriber Aggregate
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

    def test_get_status_is_system_audio_blackhole(self, tmp_path: Path) -> None:
        """BlackHole 사용 중에는 is_system_audio=True."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)
        recorder._current_device = AudioDevice(index=1, name="BlackHole 2ch", is_blackhole=True)
        assert recorder.get_status()["is_system_audio"] is True

    def test_get_status_is_system_audio_aggregate(self, tmp_path: Path) -> None:
        """Aggregate 사용 중에도 is_system_audio=True (BlackHole 경로 포함)."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)
        recorder._current_device = AudioDevice(
            index=2, name="Meeting Transcriber Aggregate", is_aggregate=True
        )
        assert recorder.get_status()["is_system_audio"] is True

    def test_get_status_is_system_audio_mic_only(self, tmp_path: Path) -> None:
        """물리 마이크만 사용 중에는 is_system_audio=False."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)
        recorder._current_device = AudioDevice(index=0, name="MacBook Air Microphone")
        assert recorder.get_status()["is_system_audio"] is False


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

        with (
            patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError),
            pytest.raises(AudioDeviceError, match="ffmpeg"),
        ):
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

        with (
            patch.object(recorder, "detect_audio_devices", return_value=[]),
            pytest.raises(AudioDeviceError, match="오디오 입력 장치"),
        ):
            await recorder._select_audio_device()

    # === Aggregate Device 지원 테스트 ===

    def test_aggregate_감지_파싱(self, tmp_path: Path) -> None:
        """Aggregate 이름이 포함되면 is_aggregate=True, is_virtual=False 로 태깅된다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)
        devices = recorder._parse_device_list(_FFMPEG_AGGREGATE_OUTPUT)

        assert len(devices) == 3
        agg = next(d for d in devices if d.is_aggregate)
        assert agg.name == "Meeting Transcriber Aggregate"
        assert agg.is_aggregate is True
        # Aggregate 는 실제 마이크 입력을 포함하는 합성 장치이므로 virtual 이 아니다
        assert agg.is_virtual is False
        assert agg.is_blackhole is False

    @pytest.mark.asyncio
    async def test_aggregate_blackhole_보다_우선(self, tmp_path: Path) -> None:
        """Aggregate 와 BlackHole 이 모두 있을 때 Aggregate 가 우선 선택된다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        devices = [
            AudioDevice(index=0, name="MacBook Air Microphone"),
            AudioDevice(index=1, name="BlackHole 2ch", is_blackhole=True),
            AudioDevice(index=2, name="Meeting Transcriber Aggregate", is_aggregate=True),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=devices):
            selected = await recorder._select_audio_device()

        assert selected.is_aggregate is True
        assert selected.name == "Meeting Transcriber Aggregate"

    @pytest.mark.asyncio
    async def test_aggregate_없으면_blackhole_폴백(self, tmp_path: Path) -> None:
        """Aggregate 가 없으면 기존처럼 BlackHole 이 선택된다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        devices = [
            AudioDevice(index=0, name="MacBook Air Microphone"),
            AudioDevice(index=1, name="BlackHole 2ch", is_blackhole=True),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=devices):
            selected = await recorder._select_audio_device()

        assert selected.is_blackhole is True

    @pytest.mark.asyncio
    async def test_preferred_device_name_정확_매칭(self, tmp_path: Path) -> None:
        """preferred_device_name 정확 매칭이 BlackHole/Aggregate 우선순위를 덮어쓴다."""
        config = AppConfig(
            paths=PathsConfig(base_dir=str(tmp_path)),
            recording=RecordingConfig(
                enabled=True,
                prefer_system_audio=True,
                preferred_device_name="My Custom Device",
            ),
        )
        recorder = AudioRecorder(config=config)

        devices = [
            AudioDevice(index=0, name="BlackHole 2ch", is_blackhole=True),
            AudioDevice(index=1, name="My Custom Device"),
            AudioDevice(index=2, name="Meeting Transcriber Aggregate", is_aggregate=True),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=devices):
            selected = await recorder._select_audio_device()

        assert selected.name == "My Custom Device"

    @pytest.mark.asyncio
    async def test_preferred_device_name_부분_매칭(self, tmp_path: Path) -> None:
        """preferred_device_name 정확 매칭 실패 시 부분 매칭으로 선택한다."""
        config = AppConfig(
            paths=PathsConfig(base_dir=str(tmp_path)),
            recording=RecordingConfig(
                enabled=True,
                prefer_system_audio=True,
                preferred_device_name="Aggregate",
            ),
        )
        recorder = AudioRecorder(config=config)

        devices = [
            AudioDevice(index=0, name="BlackHole 2ch", is_blackhole=True),
            AudioDevice(index=1, name="Meeting Transcriber Aggregate", is_aggregate=True),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=devices):
            selected = await recorder._select_audio_device()

        assert selected.name == "Meeting Transcriber Aggregate"

    @pytest.mark.asyncio
    async def test_preferred_device_name_미발견_폴백(self, tmp_path: Path) -> None:
        """preferred_device_name 장치 미발견 시 자동 선택(Aggregate>BlackHole) 으로 폴백한다."""
        config = AppConfig(
            paths=PathsConfig(base_dir=str(tmp_path)),
            recording=RecordingConfig(
                enabled=True,
                prefer_system_audio=True,
                preferred_device_name="Does Not Exist",
            ),
        )
        recorder = AudioRecorder(config=config)

        devices = [
            AudioDevice(index=0, name="MacBook Air Microphone"),
            AudioDevice(index=1, name="BlackHole 2ch", is_blackhole=True),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=devices):
            selected = await recorder._select_audio_device()

        # 미발견 → 자동 선택 경로 → prefer_system_audio=True 이므로 BlackHole
        assert selected.is_blackhole is True

    @pytest.mark.asyncio
    async def test_prefer_system_audio_false_aggregate_건너뜀(self, tmp_path: Path) -> None:
        """prefer_system_audio=False 일 때 Aggregate 장치가 있어도 건너뛰고 실제 마이크를 선택한다.

        이는 사용자가 시스템 오디오 캡처를 원하지 않을 때 Aggregate 장치가
        자동 선택되지 않아야 함을 보장하는 회귀 방지 테스트이다.
        """
        config = AppConfig(
            paths=PathsConfig(base_dir=str(tmp_path)),
            recording=RecordingConfig(
                enabled=True,
                prefer_system_audio=False,  # 시스템 오디오 비선호
            ),
        )
        recorder = AudioRecorder(config=config)

        devices = [
            AudioDevice(index=0, name="MacBook Air Microphone"),
            AudioDevice(index=1, name="BlackHole 2ch", is_blackhole=True),
            AudioDevice(index=2, name="Meeting Transcriber Aggregate", is_aggregate=True),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=devices):
            selected = await recorder._select_audio_device()

        # prefer_system_audio=False → Aggregate·BlackHole 모두 3단계 real_devices 필터에서 제외
        # → 마이크 키워드 "macbook" 에 매칭되는 MacBook Air Microphone 선택
        assert selected.name == "MacBook Air Microphone"
        assert selected.is_aggregate is False
        assert selected.is_blackhole is False

    @pytest.mark.asyncio
    async def test_preferred_device_name_prefer_system_false_조합(self, tmp_path: Path) -> None:
        """preferred_device_name 은 prefer_system_audio=False 여도 최우선 선택된다."""
        config = AppConfig(
            paths=PathsConfig(base_dir=str(tmp_path)),
            recording=RecordingConfig(
                enabled=True,
                prefer_system_audio=False,
                preferred_device_name="Meeting Transcriber Aggregate",
            ),
        )
        recorder = AudioRecorder(config=config)

        devices = [
            AudioDevice(index=0, name="MacBook Air Microphone"),
            AudioDevice(index=1, name="Meeting Transcriber Aggregate", is_aggregate=True),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=devices):
            selected = await recorder._select_audio_device()

        # 0단계: preferred_device_name 정확 매칭 → prefer_system_audio 관계없이 선택
        assert selected.name == "Meeting Transcriber Aggregate"
        assert selected.is_aggregate is True


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

        with (
            patch.object(recorder, "_select_audio_device", return_value=mock_device),
            patch("asyncio.create_subprocess_exec", return_value=mock_process),
        ):
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

        with (
            patch.object(recorder, "_select_audio_device", return_value=mock_device),
            patch("asyncio.create_subprocess_exec", return_value=mock_process),
        ):
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
        with contextlib.suppress(asyncio.CancelledError):
            await task

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
        assert d == {
            "index": 0,
            "name": "Mic",
            "is_blackhole": False,
            "is_virtual": False,
            "is_aggregate": False,
        }


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


# === TestMultiTrackRecording ===


def _make_multitrack_config(tmp_path: Path) -> AppConfig:
    """멀티트랙 테스트용 AppConfig를 생성한다."""
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
            multi_track=True,
        ),
    )


class TestMultiTrackRecording:
    """멀티트랙 녹음 테스트."""

    @pytest.mark.asyncio
    async def test_select_devices_멀티트랙_BlackHole_AND_마이크(self, tmp_path: Path) -> None:
        """BlackHole + mic가 동시 반환된다."""
        config = _make_multitrack_config(tmp_path)
        recorder = AudioRecorder(config=config)

        devices = [
            AudioDevice(index=0, name="MacBook Air Microphone", is_blackhole=False),
            AudioDevice(index=1, name="BlackHole 2ch", is_blackhole=True),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=devices):
            selected = await recorder._select_devices_multitrack()

        assert "system" in selected
        assert "mic" in selected
        assert selected["system"].is_blackhole is True
        assert selected["mic"].is_blackhole is False

    @pytest.mark.asyncio
    async def test_select_devices_BlackHole_없으면_마이크만(self, tmp_path: Path) -> None:
        """BlackHole이 없으면 마이크만 반환된다."""
        config = _make_multitrack_config(tmp_path)
        recorder = AudioRecorder(config=config)

        devices = [
            AudioDevice(index=0, name="MacBook Air Microphone", is_blackhole=False),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=devices):
            selected = await recorder._select_devices_multitrack()

        assert "mic" in selected
        assert "system" not in selected

    @pytest.mark.asyncio
    async def test_select_devices_멀티트랙에서_Aggregate_제외(self, tmp_path: Path) -> None:
        """multi_track=True 경로에서 Aggregate 는 mic 후보에서 제외된다.

        Aggregate 는 본인 마이크 + BlackHole 합성 장치라 멀티트랙에 끌려가면
        system 채널과 중복 녹음이 된다. 물리 마이크가 별도로 있으면 그것을
        mic 로 선택해야 한다.
        """
        config = _make_multitrack_config(tmp_path)
        recorder = AudioRecorder(config=config)

        devices = [
            AudioDevice(
                index=0,
                name="Meeting Transcriber Aggregate",
                is_aggregate=True,
            ),
            AudioDevice(index=1, name="BlackHole 2ch", is_blackhole=True),
            AudioDevice(index=2, name="MacBook Air Microphone"),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=devices):
            selected = await recorder._select_devices_multitrack()

        assert selected["system"].is_blackhole is True
        assert selected["mic"].name == "MacBook Air Microphone"
        # Aggregate 는 어느 쪽에도 들어가지 않아야 한다
        assert not selected["mic"].is_aggregate
        assert not selected["system"].is_aggregate

    @pytest.mark.asyncio
    async def test_start_recording_멀티트랙_두_프로세스(self, tmp_path: Path) -> None:
        """멀티트랙 모드에서 2개 ffmpeg 프로세스가 시작된다."""
        config = _make_multitrack_config(tmp_path)
        recorder = AudioRecorder(config=config)

        devices = {
            "system": AudioDevice(index=1, name="BlackHole 2ch", is_blackhole=True),
            "mic": AudioDevice(index=0, name="MacBook Air Microphone", is_blackhole=False),
        }

        mock_proc1 = AsyncMock()
        mock_proc1.pid = 1001
        mock_proc1.stdin = MagicMock()
        mock_proc1.wait = AsyncMock()

        mock_proc2 = AsyncMock()
        mock_proc2.pid = 1002
        mock_proc2.stdin = MagicMock()
        mock_proc2.wait = AsyncMock()

        call_count = 0

        async def mock_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_proc1 if call_count == 1 else mock_proc2

        with (
            patch.object(recorder, "_select_devices_multitrack", return_value=devices),
            patch("asyncio.create_subprocess_exec", side_effect=mock_exec),
        ):
            await recorder.start_recording(meeting_id="mt_test")

        assert recorder.state == RecordingState.RECORDING
        assert len(recorder._processes) == 2
        assert "system" in recorder._processes
        assert "mic" in recorder._processes

        # 정리
        recorder._state = RecordingState.IDLE
        if recorder._max_duration_task:
            recorder._max_duration_task.cancel()
        if recorder._duration_broadcast_task:
            recorder._duration_broadcast_task.cancel()

    @pytest.mark.asyncio
    async def test_파일명_규칙(self, tmp_path: Path) -> None:
        """멀티트랙 파일명이 {meeting_id}_system.wav, {meeting_id}_mic.wav 형식이다."""
        config = _make_multitrack_config(tmp_path)
        recorder = AudioRecorder(config=config)

        devices = {
            "system": AudioDevice(index=1, name="BlackHole 2ch", is_blackhole=True),
            "mic": AudioDevice(index=0, name="Mic", is_blackhole=False),
        }

        mock_proc = AsyncMock()
        mock_proc.pid = 999
        mock_proc.stdin = MagicMock()
        mock_proc.wait = AsyncMock()

        with (
            patch.object(recorder, "_select_devices_multitrack", return_value=devices),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            await recorder.start_recording(meeting_id="test123")

        assert "system" in recorder._current_files
        assert "mic" in recorder._current_files
        assert recorder._current_files["system"].name == "test123_system.wav"
        assert recorder._current_files["mic"].name == "test123_mic.wav"

        # 정리
        recorder._state = RecordingState.IDLE
        if recorder._max_duration_task:
            recorder._max_duration_task.cancel()
        if recorder._duration_broadcast_task:
            recorder._duration_broadcast_task.cancel()

    @pytest.mark.asyncio
    async def test_stop_멀티트랙_파일_이동(self, tmp_path: Path) -> None:
        """멀티트랙 정지 시 두 파일이 audio_input으로 이동된다."""
        import time as time_mod

        config = _make_multitrack_config(tmp_path)
        recorder = AudioRecorder(config=config)

        # 상태 수동 설정
        recorder._state = RecordingState.RECORDING
        recorder._start_time = time_mod.time() - 10  # 10초 전

        # 임시 파일 생성
        temp_dir = tmp_path / "recordings_temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        sys_file = temp_dir / "mt_system.wav"
        mic_file = temp_dir / "mt_mic.wav"
        sys_file.write_bytes(b"sys" * 100)
        mic_file.write_bytes(b"mic" * 100)

        recorder._current_files = {"system": sys_file, "mic": mic_file}
        recorder._current_devices = {
            "system": AudioDevice(index=1, name="BlackHole", is_blackhole=True),
            "mic": AudioDevice(index=0, name="Mic", is_blackhole=False),
        }
        recorder._meeting_id = "mt"

        # ffmpeg 프로세스 모킹
        mock_proc = AsyncMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.wait = AsyncMock()
        recorder._processes = {"system": mock_proc, "mic": mock_proc}

        result = await recorder.stop_recording()

        assert result is not None
        assert result.is_multitrack is True
        assert result.file_paths is not None
        assert "system" in result.file_paths
        assert "mic" in result.file_paths
        assert (tmp_path / "audio_input" / "mt_system.wav").exists()
        assert (tmp_path / "audio_input" / "mt_mic.wav").exists()
        assert recorder.state == RecordingState.IDLE

    @pytest.mark.asyncio
    async def test_싱글트랙_기존_동작_유지(self, tmp_path: Path) -> None:
        """multi_track=False일 때 기존 싱글트랙 동작이 유지된다."""
        config = _make_test_config(tmp_path)  # multi_track=False (기본값)
        recorder = AudioRecorder(config=config)

        assert recorder._multi_track is False

        mock_device = AudioDevice(index=0, name="Test Mic", is_blackhole=False)
        mock_process = AsyncMock()
        mock_process.pid = 12345
        mock_process.stdin = MagicMock()
        mock_process.wait = AsyncMock()

        with (
            patch.object(recorder, "_select_audio_device", return_value=mock_device),
            patch("asyncio.create_subprocess_exec", return_value=mock_process),
        ):
            await recorder.start_recording(meeting_id="single_test")

        assert recorder.state == RecordingState.RECORDING
        assert recorder._process is not None
        assert len(recorder._processes) == 0  # 멀티트랙 아님

        # 정리
        recorder._state = RecordingState.IDLE
        if recorder._max_duration_task:
            recorder._max_duration_task.cancel()
        if recorder._duration_broadcast_task:
            recorder._duration_broadcast_task.cancel()

    def test_RecordingResult_멀티트랙_필드(self) -> None:
        """RecordingResult에 멀티트랙 필드가 있다."""
        result = RecordingResult(
            file_path=Path("/tmp/test.wav"),
            duration_seconds=30.0,
            audio_device="BlackHole, Mic",
            started_at="2026-03-09T10:00:00",
            ended_at="2026-03-09T10:00:30",
            file_size_bytes=960000,
            file_paths={
                "system": Path("/tmp/test_system.wav"),
                "mic": Path("/tmp/test_mic.wav"),
            },
            is_multitrack=True,
        )
        assert result.is_multitrack is True
        assert result.file_paths is not None
        assert len(result.file_paths) == 2

    def test_RecordingResult_싱글트랙_기본값(self) -> None:
        """RecordingResult 싱글트랙에서 멀티트랙 필드 기본값이 올바르다."""
        result = RecordingResult(
            file_path=Path("/tmp/test.wav"),
            duration_seconds=30.0,
            audio_device="Mic",
            started_at="2026-03-09T10:00:00",
            ended_at="2026-03-09T10:00:30",
            file_size_bytes=480000,
        )
        assert result.is_multitrack is False
        assert result.file_paths is None


# === 가상 장치 필터링 테스트 ===


class TestVirtualDeviceFiltering:
    """가상 장치 필터링 및 장치 선택 우선순위를 테스트한다."""

    def test_parse_device_가상장치_감지(self, tmp_path: Path) -> None:
        """ZoomAudioDevice가 is_virtual=True로 파싱되는지 확인한다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        stderr_output = (
            "[AVFoundation indev @ 0x7f8] AVFoundation audio devices:\n"
            "[AVFoundation indev @ 0x7f8] [0] ZoomAudioDevice\n"
            "[AVFoundation indev @ 0x7f8] [1] MacBook Air Microphone\n"
        )
        devices = recorder._parse_device_list(stderr_output)

        assert len(devices) == 2
        # ZoomAudioDevice는 is_virtual=True
        assert devices[0].name == "ZoomAudioDevice"
        assert devices[0].is_virtual is True
        assert devices[0].is_blackhole is False
        # MacBook Air Microphone은 is_virtual=False
        assert devices[1].name == "MacBook Air Microphone"
        assert devices[1].is_virtual is False

    def test_parse_device_blackhole_is_not_virtual(self, tmp_path: Path) -> None:
        """BlackHole은 is_blackhole=True이고 is_virtual=False이다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        stderr_output = (
            "[AVFoundation indev @ 0x7f8] AVFoundation audio devices:\n"
            "[AVFoundation indev @ 0x7f8] [0] BlackHole 2ch\n"
        )
        devices = recorder._parse_device_list(stderr_output)

        assert len(devices) == 1
        assert devices[0].is_blackhole is True
        assert devices[0].is_virtual is False

    def test_parse_device_여러_가상장치_키워드(self, tmp_path: Path) -> None:
        """가상 장치 키워드(virtual, soundflower, loopback)가 감지된다.

        Aggregate Device 는 본인 마이크 + BlackHole 통합의 합성 장치로 녹음에
        활용 가능하므로 is_aggregate=True 로 분리되어 virtual 에서 제외된다.
        """
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        stderr_output = (
            "[AVFoundation indev @ 0x7f8] AVFoundation audio devices:\n"
            "[AVFoundation indev @ 0x7f8] [0] Virtual Audio Cable\n"
            "[AVFoundation indev @ 0x7f8] [1] Soundflower (2ch)\n"
            "[AVFoundation indev @ 0x7f8] [2] Aggregate Device\n"
            "[AVFoundation indev @ 0x7f8] [3] Loopback Audio\n"
        )
        devices = recorder._parse_device_list(stderr_output)

        assert len(devices) == 4
        by_name = {d.name: d for d in devices}

        # virtual (사용 불가) — 3건
        assert by_name["Virtual Audio Cable"].is_virtual is True
        assert by_name["Soundflower (2ch)"].is_virtual is True
        assert by_name["Loopback Audio"].is_virtual is True

        # aggregate — virtual 에서 제외되고 is_aggregate=True 로 태깅
        agg = by_name["Aggregate Device"]
        assert agg.is_virtual is False
        assert agg.is_aggregate is True
        assert agg.is_blackhole is False

    @pytest.mark.asyncio
    async def test_select_device_가상장치_필터링(self, tmp_path: Path) -> None:
        """ZoomAudioDevice + 마이크가 있으면 마이크가 선택된다."""
        config = _make_test_config(tmp_path)
        config.recording.prefer_system_audio = False
        recorder = AudioRecorder(config=config)

        mock_devices = [
            AudioDevice(index=0, name="ZoomAudioDevice", is_virtual=True),
            AudioDevice(index=1, name="MacBook Air Microphone"),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=mock_devices):
            selected = await recorder._select_audio_device()

        assert selected.index == 1
        assert selected.name == "MacBook Air Microphone"

    @pytest.mark.asyncio
    async def test_select_device_blackhole_우선(self, tmp_path: Path) -> None:
        """prefer_system_audio=True일 때 BlackHole + 마이크 + ZoomAudioDevice → BlackHole 선택."""
        config = _make_test_config(tmp_path)
        config.recording.prefer_system_audio = True
        recorder = AudioRecorder(config=config)

        mock_devices = [
            AudioDevice(index=0, name="BlackHole 2ch", is_blackhole=True),
            AudioDevice(index=1, name="MacBook Air Microphone"),
            AudioDevice(index=2, name="ZoomAudioDevice", is_virtual=True),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=mock_devices):
            selected = await recorder._select_audio_device()

        assert selected.index == 0
        assert selected.is_blackhole is True

    @pytest.mark.asyncio
    async def test_select_device_마이크_키워드(self, tmp_path: Path) -> None:
        """여러 실제 장치 중 'Microphone' 포함 장치가 우선 선택된다."""
        config = _make_test_config(tmp_path)
        config.recording.prefer_system_audio = False
        recorder = AudioRecorder(config=config)

        mock_devices = [
            AudioDevice(index=0, name="External USB Audio"),
            AudioDevice(index=1, name="Built-in Microphone"),
            AudioDevice(index=2, name="Line In"),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=mock_devices):
            selected = await recorder._select_audio_device()

        assert selected.index == 1
        assert "Microphone" in selected.name

    @pytest.mark.asyncio
    async def test_select_device_가상장치만(self, tmp_path: Path) -> None:
        """가상 장치만 있으면 경고 후 첫 번째 장치를 폴백 사용한다."""
        config = _make_test_config(tmp_path)
        config.recording.prefer_system_audio = False
        recorder = AudioRecorder(config=config)

        mock_devices = [
            AudioDevice(index=0, name="ZoomAudioDevice", is_virtual=True),
            AudioDevice(index=1, name="Virtual Audio Cable", is_virtual=True),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=mock_devices):
            selected = await recorder._select_audio_device()

        assert selected.index == 0
        assert selected.name == "ZoomAudioDevice"

    @pytest.mark.asyncio
    async def test_select_device_키워드없는_실제장치_첫번째(self, tmp_path: Path) -> None:
        """마이크 키워드가 없는 실제 장치들 중 첫 번째가 선택된다."""
        config = _make_test_config(tmp_path)
        config.recording.prefer_system_audio = False
        recorder = AudioRecorder(config=config)

        mock_devices = [
            AudioDevice(index=0, name="ZoomAudioDevice", is_virtual=True),
            AudioDevice(index=1, name="USB Audio Interface"),
            AudioDevice(index=2, name="External Sound Card"),
        ]
        with patch.object(recorder, "detect_audio_devices", return_value=mock_devices):
            selected = await recorder._select_audio_device()

        assert selected.index == 1
        assert selected.name == "USB Audio Interface"

    def test_AudioDevice_is_virtual_기본값(self) -> None:
        """AudioDevice의 is_virtual 기본값은 False이다."""
        device = AudioDevice(index=0, name="Test")
        assert device.is_virtual is False

    def test_to_dict_includes_is_virtual(self) -> None:
        """to_dict()에 is_virtual 필드가 포함된다."""
        dev = AudioDevice(index=0, name="ZoomAudioDevice", is_virtual=True)
        d = dev.to_dict()
        assert d["is_virtual"] is True
        assert d["is_blackhole"] is False


class TestSilenceDetection:
    """무음 감지 로직(_check_audio_energy)을 테스트한다."""

    def _make_wav(self, path: Path, samples: list[float], sr: int = 16000) -> None:
        """테스트용 WAV 파일을 생성한다."""
        import numpy as np
        import soundfile as sf

        data = np.array(samples, dtype=np.float32)
        sf.write(str(path), data, sr)

    def test_무음_파일_감지(self, tmp_path: Path) -> None:
        """RMS가 임계값 미만인 무음 파일은 False를 반환한다."""
        config = _make_test_config(tmp_path)
        config.recording.silence_threshold_rms = 0.001
        recorder = AudioRecorder(config=config)

        wav_path = tmp_path / "silent.wav"
        # 1초 분량의 완전 무음 (모두 0.0)
        self._make_wav(wav_path, [0.0] * 16000)

        assert recorder._check_audio_energy(wav_path) is False

    def test_정상_오디오_통과(self, tmp_path: Path) -> None:
        """RMS가 임계값 이상인 정상 오디오는 True를 반환한다."""
        import numpy as np

        config = _make_test_config(tmp_path)
        config.recording.silence_threshold_rms = 0.001
        recorder = AudioRecorder(config=config)

        wav_path = tmp_path / "normal.wav"
        # 1초 분량의 정상 오디오 (사인파)
        t = np.linspace(0, 1, 16000, dtype=np.float32)
        samples = (0.5 * np.sin(2 * np.pi * 440 * t)).tolist()
        self._make_wav(wav_path, samples)

        assert recorder._check_audio_energy(wav_path) is True

    def test_매우_작은_소리_감지(self, tmp_path: Path) -> None:
        """극소량의 노이즈(RMS < 임계값)도 무음으로 판정한다."""
        import numpy as np

        config = _make_test_config(tmp_path)
        config.recording.silence_threshold_rms = 0.001
        recorder = AudioRecorder(config=config)

        wav_path = tmp_path / "almost_silent.wav"
        # RMS ≈ 0.0001 수준의 극소 노이즈
        samples = (np.ones(16000, dtype=np.float32) * 0.0001).tolist()
        self._make_wav(wav_path, samples)

        assert recorder._check_audio_energy(wav_path) is False

    def test_soundfile_import_실패시_통과(self, tmp_path: Path) -> None:
        """soundfile을 import할 수 없으면 True를 반환한다 (graceful fallback)."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        wav_path = tmp_path / "test.wav"
        wav_path.touch()

        with patch.dict("sys.modules", {"soundfile": None}):
            assert recorder._check_audio_energy(wav_path) is True

    def test_파일_읽기_실패시_통과(self, tmp_path: Path) -> None:
        """파일을 읽을 수 없으면 True를 반환한다 (graceful fallback)."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        # 유효하지 않은 WAV 파일
        bad_wav = tmp_path / "bad.wav"
        bad_wav.write_text("not a wav file")

        assert recorder._check_audio_energy(bad_wav) is True

    def test_빈_데이터_파일_감지(self, tmp_path: Path) -> None:
        """WAV 헤더만 있고 오디오 데이터가 없으면 False를 반환한다."""
        config = _make_test_config(tmp_path)
        recorder = AudioRecorder(config=config)

        wav_path = tmp_path / "empty_data.wav"
        self._make_wav(wav_path, [])

        assert recorder._check_audio_energy(wav_path) is False
