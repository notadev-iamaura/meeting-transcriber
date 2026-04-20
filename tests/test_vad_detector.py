"""
VAD 음성 구간 감지기 모듈 테스트 (VAD Detector Module Tests)

목적: steps/vad_detector.py의 VoiceActivityDetector 클래스를 단위 테스트한다.
테스트 범위:
    - VAD 비활성화 시 None 반환
    - 파일 미존재 시 FileNotFoundError
    - clip_timestamps 형식 변환
    - 마지막 타임스탬프 무한루프 방지 조정
    - 빈 음성구간 시 None 반환
    - CPU 강제 실행 확인
    - ModelLoadManager 미사용 확인
    - VADResult 데이터 정확성
    - config 기본값 사용
    - silero_vad 미설치 시 VADError
    - 다중 음성구간 clip_timestamps 변환
    - 단일 음성구간 처리
의존성: pytest, pytest-asyncio, unittest.mock
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from steps.vad_detector import (
    VADError,
    VADResult,
    VoiceActivityDetector,
)

# === 픽스처 ===


@pytest.fixture
def mock_config_with_vad() -> MagicMock:
    """VAD 활성화된 테스트용 설정 객체를 생성한다."""
    config = MagicMock()
    config.vad.enabled = True
    config.vad.threshold = 0.5
    config.vad.min_speech_duration_ms = 250
    config.vad.min_silence_duration_ms = 100
    config.vad.speech_pad_ms = 30
    return config


@pytest.fixture
def mock_config_vad_disabled() -> MagicMock:
    """VAD 비활성화된 테스트용 설정 객체를 생성한다."""
    config = MagicMock()
    config.vad.enabled = False
    config.vad.threshold = 0.5
    config.vad.min_speech_duration_ms = 250
    config.vad.min_silence_duration_ms = 100
    config.vad.speech_pad_ms = 30
    return config


@pytest.fixture
def mock_config_no_vad() -> MagicMock:
    """VAD 설정이 없는 config 객체를 생성한다 (getattr 시 None 반환)."""
    config = MagicMock(spec=[])  # spec=[]로 모든 속성 접근 차단
    return config


@pytest.fixture
def detector(mock_config_with_vad: MagicMock) -> VoiceActivityDetector:
    """VAD 활성화된 VoiceActivityDetector 인스턴스."""
    return VoiceActivityDetector(config=mock_config_with_vad)


@pytest.fixture
def detector_disabled(mock_config_vad_disabled: MagicMock) -> VoiceActivityDetector:
    """VAD 비활성화된 VoiceActivityDetector 인스턴스."""
    return VoiceActivityDetector(config=mock_config_vad_disabled)


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    """테스트용 더미 오디오 파일 생성."""
    audio = tmp_path / "test_audio.wav"
    audio.write_bytes(b"\x00" * 1024)
    return audio


# === 1. VAD 비활성화 시 None 반환 테스트 ===


class TestVAD비활성화:
    """VAD가 비활성화되었을 때의 동작 테스트."""

    @pytest.mark.asyncio
    async def test_VAD_비활성화시_None_반환(
        self, detector_disabled: VoiceActivityDetector, audio_file: Path
    ) -> None:
        """enabled=False일 때 detect()가 None을 반환한다."""
        result = await detector_disabled.detect(audio_file)
        assert result is None


# === 2. 파일 미존재 시 FileNotFoundError 테스트 ===


class TestFileValidation:
    """오디오 파일 유효성 검증 테스트."""

    @pytest.mark.asyncio
    async def test_파일_미존재시_FileNotFoundError(self, detector: VoiceActivityDetector) -> None:
        """존재하지 않는 파일 경로에 대해 FileNotFoundError를 발생시킨다."""
        with pytest.raises(FileNotFoundError, match="찾을 수 없습니다"):
            await detector.detect(Path("/nonexistent/audio.wav"))


# === 3. clip_timestamps 형식 변환 테스트 ===


class TestClipTimestamps변환:
    """clip_timestamps 형식 변환 테스트."""

    def test_clip_timestamps_형식_변환(self) -> None:
        """음성 구간 리스트를 [s1, e1, s2, e2, ...] 형식으로 변환한다."""
        segments = [
            {"start": 1.0, "end": 3.0},
            {"start": 5.0, "end": 8.0},
        ]
        # duration을 충분히 크게 설정하여 조정이 발생하지 않도록 함
        result = VoiceActivityDetector._to_clip_timestamps(segments, duration=20.0)
        assert result == [1.0, 3.0, 5.0, 8.0]


# === 4. 마지막 타임스탬프 무한루프 방지 테스트 ===


class TestTimestampAdjustment:
    """마지막 타임스탬프 조정 테스트 (mlx-whisper 무한루프 방지)."""

    def test_마지막_타임스탬프_무한루프_방지(self) -> None:
        """마지막 end가 duration과 같으면 -0.1초 조정한다."""
        segments = [
            {"start": 1.0, "end": 3.0},
            {"start": 5.0, "end": 10.0},  # duration=10.0과 동일
        ]
        result = VoiceActivityDetector._to_clip_timestamps(segments, duration=10.0)
        # 마지막 end가 10.0 → 9.9로 조정되어야 함
        assert result == [1.0, 3.0, 5.0, 9.9]

    def test_마지막_타임스탬프_근접시_조정(self) -> None:
        """마지막 end가 duration과 0.15초 이내면 조정한다."""
        segments = [
            {"start": 0.0, "end": 9.95},  # duration=10.0과 0.05 차이
        ]
        result = VoiceActivityDetector._to_clip_timestamps(segments, duration=10.0)
        assert abs(result[-1] - 9.85) < 0.001

    def test_마지막_타임스탬프_차이_큰_경우_미조정(self) -> None:
        """마지막 end가 duration과 0.15초 이상 차이나면 조정하지 않는다."""
        segments = [
            {"start": 1.0, "end": 8.0},  # duration=10.0과 2.0 차이
        ]
        result = VoiceActivityDetector._to_clip_timestamps(segments, duration=10.0)
        assert result == [1.0, 8.0]  # 조정 없음


# === 5. 빈 음성구간 시 None 반환 테스트 ===


class TestEmptySpeech:
    """음성 구간이 없을 때의 동작 테스트."""

    @pytest.mark.asyncio
    async def test_빈_음성구간시_None_반환(
        self,
        detector: VoiceActivityDetector,
        audio_file: Path,
    ) -> None:
        """음성 구간이 없으면 detect()가 None을 반환한다."""
        # _run_vad가 빈 결과를 반환하도록 모킹
        with patch.object(detector, "_run_vad", return_value=([], 10.0)):
            result = await detector.detect(audio_file)
            assert result is None


# === 6. CPU 강제 실행 테스트 ===


class TestCPU강제실행:
    """모델이 CPU에서만 실행되는지 확인하는 테스트."""

    def test_CPU_강제_실행(self, detector: VoiceActivityDetector) -> None:
        """모델 로드 시 CPU 디바이스를 강제 사용하는지 확인한다."""
        mock_torch = MagicMock()
        mock_model = MagicMock()
        mock_get_timestamps = MagicMock()

        mock_load_vad = MagicMock(return_value=mock_model)

        with (
            patch.dict("sys.modules", {"torch": mock_torch}),
            patch(
                "steps.vad_detector.load_silero_vad",
                mock_load_vad,
                create=True,
            ),
            patch(
                "steps.vad_detector.get_speech_timestamps",
                mock_get_timestamps,
                create=True,
            ),
        ):
            # silero_vad 모듈 모킹
            mock_silero_module = MagicMock()
            mock_silero_module.load_silero_vad = mock_load_vad
            mock_silero_module.get_speech_timestamps = mock_get_timestamps

            with patch.dict(
                "sys.modules",
                {
                    "silero_vad": mock_silero_module,
                    "torch": mock_torch,
                },
            ):
                detector._model = None
                detector._load_model()

                # torch.device("cpu")가 호출되었는지 확인
                mock_torch.device.assert_called_with("cpu")
                # model.to()가 CPU 디바이스로 호출되었는지 확인
                mock_model.to.assert_called_once()


# === 7. ModelLoadManager 미사용 테스트 ===


class TestModelLoadManager미사용:
    """ModelLoadManager를 사용하지 않는지 확인하는 테스트."""

    @pytest.mark.asyncio
    async def test_ModelLoadManager_미사용(
        self,
        detector: VoiceActivityDetector,
        audio_file: Path,
    ) -> None:
        """VoiceActivityDetector가 ModelLoadManager.acquire()를 호출하지 않는다."""
        # _run_vad 모킹
        speech_segments = [{"start": 1.0, "end": 3.0}]
        with (
            patch.object(detector, "_run_vad", return_value=(speech_segments, 10.0)),
            patch("steps.vad_detector.logger"),
        ):
            result = await detector.detect(audio_file)
            assert result is not None

            # ModelLoadManager 관련 모듈이 임포트되지 않았는지 확인
            # VoiceActivityDetector 클래스에 _manager 속성이 없어야 함
            assert not hasattr(detector, "_manager")


# === 8. VADResult 데이터 정확성 테스트 ===


class TestVADResult데이터:
    """VADResult 데이터의 정확성 테스트."""

    @pytest.mark.asyncio
    async def test_VADResult_데이터_정확성(
        self,
        detector: VoiceActivityDetector,
        audio_file: Path,
    ) -> None:
        """total_speech + total_silence가 전체 duration과 근사하다."""
        speech_segments = [
            {"start": 1.0, "end": 3.0},  # 2초
            {"start": 5.0, "end": 8.0},  # 3초
        ]
        duration = 10.0

        with patch.object(detector, "_run_vad", return_value=(speech_segments, duration)):
            result = await detector.detect(audio_file)

            assert result is not None
            # 음성 2초 + 3초 = 5초
            assert abs(result.total_speech_seconds - 5.0) < 0.01
            # 무음 10초 - 5초 = 5초
            assert abs(result.total_silence_seconds - 5.0) < 0.01
            # 합산이 duration과 근사
            total = result.total_speech_seconds + result.total_silence_seconds
            assert abs(total - duration) < 0.01


# === 9. config 기본값 사용 테스트 ===


class TestConfig기본값:
    """config에 VAD 설정이 없을 때 기본값 사용 테스트."""

    def test_config_기본값_사용(self, mock_config_no_vad: MagicMock) -> None:
        """config에 vad 속성이 없으면 기본값으로 초기화된다 (비활성)."""
        detector = VoiceActivityDetector(config=mock_config_no_vad)
        assert detector._enabled is False
        assert detector._threshold == 0.5
        assert detector._min_speech_duration_ms == 250
        assert detector._min_silence_duration_ms == 100
        assert detector._speech_pad_ms == 30

    @pytest.mark.asyncio
    async def test_config_기본값_비활성_detect_None(
        self, mock_config_no_vad: MagicMock, audio_file: Path
    ) -> None:
        """config에 vad 없으면 detect()가 None을 반환한다."""
        detector = VoiceActivityDetector(config=mock_config_no_vad)
        result = await detector.detect(audio_file)
        assert result is None


# === 10. silero_vad 미설치 시 VADError 테스트 ===


class TestSileroMissing:
    """silero-vad 패키지 미설치 시 에러 처리 테스트."""

    def test_silero_미설치시_VADError(self, detector: VoiceActivityDetector) -> None:
        """silero_vad import 실패 시 VADError를 발생시킨다."""
        detector._model = None  # 모델 초기화

        mock_torch = MagicMock()

        with (
            patch.dict(
                "sys.modules",
                {
                    "torch": mock_torch,
                    "silero_vad": None,  # import 실패 시뮬레이션
                },
            ),
            pytest.raises(VADError, match="silero-vad가 설치되어 있지 않습니다"),
        ):
            detector._load_model()

    def test_torch_미설치시_VADError(self, detector: VoiceActivityDetector) -> None:
        """PyTorch import 실패 시 VADError를 발생시킨다."""
        detector._model = None

        with (
            patch.dict("sys.modules", {"torch": None}),
            pytest.raises(VADError, match="PyTorch가 설치되어 있지 않습니다"),
        ):
            detector._load_model()


# === 11. 다중 음성구간 clip_timestamps 변환 테스트 ===


class TestMultipleSegments:
    """3개 이상 음성 구간의 clip_timestamps 변환 테스트."""

    def test_다중_음성구간_clip_timestamps(self) -> None:
        """3개 음성 구간이 올바르게 변환된다."""
        segments = [
            {"start": 0.5, "end": 2.0},
            {"start": 3.5, "end": 5.0},
            {"start": 7.0, "end": 9.0},
        ]
        result = VoiceActivityDetector._to_clip_timestamps(segments, duration=20.0)
        assert result == [0.5, 2.0, 3.5, 5.0, 7.0, 9.0]
        assert len(result) == 6  # 3구간 x 2 (start, end)

    def test_빈_구간_리스트_clip_timestamps(self) -> None:
        """빈 구간 리스트는 빈 리스트를 반환한다."""
        result = VoiceActivityDetector._to_clip_timestamps([], duration=10.0)
        assert result == []

    @pytest.mark.asyncio
    async def test_다중_구간_detect_결과(
        self,
        detector: VoiceActivityDetector,
        audio_file: Path,
    ) -> None:
        """다중 음성 구간에 대한 detect() 전체 흐름 검증."""
        speech_segments = [
            {"start": 0.5, "end": 2.0},
            {"start": 3.5, "end": 5.0},
            {"start": 7.0, "end": 9.0},
        ]

        with patch.object(detector, "_run_vad", return_value=(speech_segments, 20.0)):
            result = await detector.detect(audio_file)

            assert result is not None
            assert result.num_segments == 3
            assert len(result.clip_timestamps) == 6


# === 12. 단일 음성구간 테스트 ===


class TestSingleSegment:
    """1개 음성 구간만 있을 때의 처리 테스트."""

    def test_단일_음성구간(self) -> None:
        """1개 음성 구간이 올바르게 변환된다."""
        segments = [{"start": 2.0, "end": 7.0}]
        result = VoiceActivityDetector._to_clip_timestamps(segments, duration=20.0)
        assert result == [2.0, 7.0]
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_단일_구간_detect_결과(
        self,
        detector: VoiceActivityDetector,
        audio_file: Path,
    ) -> None:
        """1개 음성 구간에 대한 detect() 전체 흐름 검증."""
        speech_segments = [{"start": 2.0, "end": 7.0}]

        with patch.object(detector, "_run_vad", return_value=(speech_segments, 10.0)):
            result = await detector.detect(audio_file)

            assert result is not None
            assert result.num_segments == 1
            assert result.total_speech_seconds == 5.0
            assert result.total_silence_seconds == 5.0
            assert result.clip_timestamps == [2.0, 7.0]
            assert str(audio_file) in result.audio_path


# === VADResult 데이터 클래스 테스트 ===


class TestVADResultDataclass:
    """VADResult 데이터 클래스 속성 테스트."""

    def test_num_segments_속성(self) -> None:
        """num_segments가 speech_segments 개수를 반환한다."""
        result = VADResult(
            speech_segments=[
                {"start": 0.0, "end": 1.0},
                {"start": 2.0, "end": 3.0},
            ],
            clip_timestamps=[0.0, 1.0, 2.0, 3.0],
            audio_path="/test/audio.wav",
            total_speech_seconds=2.0,
            total_silence_seconds=3.0,
        )
        assert result.num_segments == 2

    def test_빈_결과_num_segments(self) -> None:
        """빈 결과의 num_segments는 0이다."""
        result = VADResult(
            speech_segments=[],
            clip_timestamps=[],
            audio_path="/test/audio.wav",
            total_speech_seconds=0.0,
            total_silence_seconds=10.0,
        )
        assert result.num_segments == 0


# === VADError 에러 계층 테스트 ===


class TestVADErrorHierarchy:
    """VADError 에러 클래스 계층 테스트."""

    def test_VADError는_Exception_상속(self) -> None:
        """VADError는 Exception을 상속한다."""
        assert issubclass(VADError, Exception)

    def test_VADError로_에러_포착(self) -> None:
        """VADError로 에러를 포착할 수 있다."""
        with pytest.raises(VADError):
            raise VADError("테스트 에러")
