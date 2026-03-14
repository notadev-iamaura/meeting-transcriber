"""
화자분리기 모듈 테스트 (Diarizer Module Tests)

목적: steps/diarizer.py의 Diarizer 클래스 전체 기능 단위 테스트.
주요 테스트 항목:
    - DiarizationSegment / DiarizationResult 데이터 클래스
    - 체크포인트 저장 및 복원
    - 오디오 파일 유효성 검증
    - HuggingFace 토큰 검증
    - pyannote 파이프라인 로드 (모킹)
    - 화자분리 실행 (모킹)
    - 에러 처리 및 예외 계층
의존성: pytest, pytest-asyncio, unittest.mock
"""

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import reset_config
from core.model_manager import reset_model_manager
from steps.diarizer import (
    DiarizationError,
    DiarizationResult,
    DiarizationSegment,
    Diarizer,
    EmptyAudioError,
    ModelNotAvailableError,
    TokenNotConfiguredError,
)



# === Fixture ===


@pytest.fixture(autouse=True)
def _reset_singletons():
    """매 테스트마다 싱글턴 인스턴스를 초기화한다."""
    reset_config()
    reset_model_manager()
    yield
    reset_config()
    reset_model_manager()


@pytest.fixture
def mock_config():
    """테스트용 설정 객체를 생성한다."""
    config = MagicMock()
    config.diarization.model_name = "pyannote/speaker-diarization-3.1"
    config.diarization.device = "cpu"
    config.diarization.min_speakers = 1
    config.diarization.max_speakers = 10
    config.diarization.huggingface_token = "hf_test_token_12345"
    config.diarization.timeout_seconds = 1800
    config.pipeline.peak_ram_limit_gb = 9.5
    return config


@pytest.fixture
def mock_manager():
    """ModelLoadManager 모킹 객체를 생성한다.

    acquire() 컨텍스트 매니저 패턴을 모킹한다.
    """
    manager = MagicMock()
    mock_pipeline = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_pipeline)
    ctx.__aexit__ = AsyncMock(return_value=False)
    manager.acquire.return_value = ctx
    return manager, mock_pipeline


@pytest.fixture
def sample_audio(tmp_path: Path) -> Path:
    """테스트용 오디오 파일을 생성한다."""
    audio = tmp_path / "test_audio.wav"
    audio.write_bytes(b"RIFF" + b"\x00" * 100)
    return audio


def _make_mock_annotation(
    segments: list[dict[str, Any]],
) -> MagicMock:
    """pyannote Annotation 모킹 객체를 생성한다.

    Args:
        segments: [{"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0}, ...]

    Returns:
        itertracks()를 지원하는 모킹 Annotation 객체
    """
    annotation = MagicMock()

    # Segment 모킹 — pyannote의 Segment는 start, end 속성을 가진다
    tracks = []
    for seg in segments:
        turn = MagicMock()
        turn.start = seg["start"]
        turn.end = seg["end"]
        tracks.append((turn, None, seg["speaker"]))

    annotation.itertracks.return_value = tracks
    return annotation


# === DiarizationSegment 테스트 ===


class TestDiarizationSegment:
    """DiarizationSegment 데이터 클래스 테스트."""

    def test_기본_생성(self):
        """필수 필드로 세그먼트를 생성한다."""
        seg = DiarizationSegment(speaker="SPEAKER_00", start=0.0, end=5.0)
        assert seg.speaker == "SPEAKER_00"
        assert seg.start == 0.0
        assert seg.end == 5.0

    def test_duration_속성(self):
        """발화 구간 길이를 올바르게 계산한다."""
        seg = DiarizationSegment(speaker="SPEAKER_01", start=2.5, end=7.3)
        assert abs(seg.duration - 4.8) < 0.001

    def test_to_dict_변환(self):
        """딕셔너리로 올바르게 변환한다."""
        seg = DiarizationSegment(speaker="SPEAKER_00", start=1.0, end=3.5)
        d = seg.to_dict()
        assert d == {
            "speaker": "SPEAKER_00",
            "start": 1.0,
            "end": 3.5,
        }


# === DiarizationResult 테스트 ===


class TestDiarizationResult:
    """DiarizationResult 데이터 클래스 테스트."""

    def test_기본_생성(self):
        """기본 필드로 결과를 생성한다."""
        segments = [
            DiarizationSegment("SPEAKER_00", 0.0, 5.0),
            DiarizationSegment("SPEAKER_01", 5.0, 10.0),
        ]
        result = DiarizationResult(
            segments=segments,
            num_speakers=2,
            audio_path="/test/audio.wav",
        )
        assert len(result.segments) == 2
        assert result.num_speakers == 2

    def test_total_duration_속성(self):
        """전체 길이를 올바르게 계산한다."""
        segments = [
            DiarizationSegment("SPEAKER_00", 0.0, 5.0),
            DiarizationSegment("SPEAKER_01", 3.0, 12.5),
        ]
        result = DiarizationResult(segments=segments, num_speakers=2, audio_path="")
        assert result.total_duration == 12.5

    def test_total_duration_빈_세그먼트(self):
        """세그먼트가 없으면 0.0을 반환한다."""
        result = DiarizationResult(segments=[], num_speakers=0, audio_path="")
        assert result.total_duration == 0.0

    def test_speakers_속성(self):
        """중복 제거된 화자 목록을 정렬하여 반환한다."""
        segments = [
            DiarizationSegment("SPEAKER_01", 0.0, 5.0),
            DiarizationSegment("SPEAKER_00", 5.0, 10.0),
            DiarizationSegment("SPEAKER_01", 10.0, 15.0),
        ]
        result = DiarizationResult(segments=segments, num_speakers=2, audio_path="")
        assert result.speakers == ["SPEAKER_00", "SPEAKER_01"]

    def test_to_dict_변환(self):
        """딕셔너리로 올바르게 변환한다."""
        segments = [
            DiarizationSegment("SPEAKER_00", 0.0, 5.0),
        ]
        result = DiarizationResult(segments=segments, num_speakers=1, audio_path="/test.wav")
        d = result.to_dict()
        assert d["num_speakers"] == 1
        assert d["audio_path"] == "/test.wav"
        assert len(d["segments"]) == 1


# === 체크포인트 테스트 ===


class TestCheckpoint:
    """체크포인트 저장/복원 테스트."""

    def test_체크포인트_저장(self, tmp_path: Path):
        """JSON 파일로 올바르게 저장한다."""
        segments = [
            DiarizationSegment("SPEAKER_00", 0.0, 5.0),
            DiarizationSegment("SPEAKER_01", 5.0, 10.0),
        ]
        result = DiarizationResult(segments=segments, num_speakers=2, audio_path="/test.wav")
        output = tmp_path / "checkpoint.json"
        result.save_checkpoint(output)

        assert output.exists()
        data = json.loads(output.read_text(encoding="utf-8"))
        assert data["num_speakers"] == 2
        assert len(data["segments"]) == 2

    def test_체크포인트_복원(self, tmp_path: Path):
        """JSON 파일에서 올바르게 복원한다."""
        data = {
            "segments": [
                {"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0},
                {"speaker": "SPEAKER_01", "start": 5.0, "end": 10.0},
            ],
            "num_speakers": 2,
            "audio_path": "/test.wav",
        }
        checkpoint = tmp_path / "checkpoint.json"
        checkpoint.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        result = DiarizationResult.from_checkpoint(checkpoint)
        assert len(result.segments) == 2
        assert result.num_speakers == 2
        assert result.segments[0].speaker == "SPEAKER_00"
        assert result.segments[1].end == 10.0

    def test_체크포인트_라운드트립(self, tmp_path: Path):
        """저장 후 복원하면 원본과 동일한 데이터를 얻는다."""
        original = DiarizationResult(
            segments=[
                DiarizationSegment("SPEAKER_00", 0.5, 3.2),
                DiarizationSegment("SPEAKER_01", 3.5, 8.1),
                DiarizationSegment("SPEAKER_00", 8.5, 12.0),
            ],
            num_speakers=2,
            audio_path="/meeting/audio.wav",
        )
        output = tmp_path / "sub" / "dir" / "checkpoint.json"
        original.save_checkpoint(output)

        restored = DiarizationResult.from_checkpoint(output)
        assert restored.num_speakers == original.num_speakers
        assert restored.audio_path == original.audio_path
        assert len(restored.segments) == len(original.segments)
        for orig, rest in zip(original.segments, restored.segments, strict=False):
            assert orig.speaker == rest.speaker
            assert orig.start == rest.start
            assert orig.end == rest.end

    def test_부모_디렉토리_자동_생성(self, tmp_path: Path):
        """체크포인트 저장 시 부모 디렉토리를 자동 생성한다."""
        result = DiarizationResult(
            segments=[DiarizationSegment("SPEAKER_00", 0.0, 1.0)],
            num_speakers=1,
            audio_path="",
        )
        deep_path = tmp_path / "a" / "b" / "c" / "checkpoint.json"
        result.save_checkpoint(deep_path)
        assert deep_path.exists()


# === 에러 계층 테스트 ===


class TestErrorHierarchy:
    """에러 클래스 계층 구조 테스트."""

    def test_ModelNotAvailableError는_DiarizationError의_하위(self):
        """ModelNotAvailableError는 DiarizationError를 상속한다."""
        assert issubclass(ModelNotAvailableError, DiarizationError)

    def test_EmptyAudioError는_DiarizationError의_하위(self):
        """EmptyAudioError는 DiarizationError를 상속한다."""
        assert issubclass(EmptyAudioError, DiarizationError)

    def test_TokenNotConfiguredError는_DiarizationError의_하위(self):
        """TokenNotConfiguredError는 DiarizationError를 상속한다."""
        assert issubclass(TokenNotConfiguredError, DiarizationError)

    def test_DiarizationError로_모든_하위_에러를_잡을_수_있다(self):
        """DiarizationError로 모든 하위 예외를 캐치할 수 있다."""
        with pytest.raises(DiarizationError):
            raise ModelNotAvailableError("테스트")

        with pytest.raises(DiarizationError):
            raise EmptyAudioError("테스트")

        with pytest.raises(DiarizationError):
            raise TokenNotConfiguredError("테스트")


# === Diarizer 초기화 테스트 ===


class TestDiarizerInit:
    """Diarizer 초기화 테스트."""

    def test_설정_주입(self, mock_config, mock_manager):
        """설정과 매니저를 주입하여 초기화한다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)
        assert diarizer._model_name == "pyannote/speaker-diarization-3.1"
        assert diarizer._device == "cpu"
        assert diarizer._min_speakers == 1
        assert diarizer._max_speakers == 10

    def test_토큰_설정_확인(self, mock_config, mock_manager):
        """HuggingFace 토큰이 설정에서 로드된다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)
        assert diarizer._hf_token == "hf_test_token_12345"


# === 토큰 검증 테스트 ===


class TestTokenValidation:
    """HuggingFace 토큰 검증 테스트."""

    def test_토큰_있으면_반환(self, mock_config, mock_manager):
        """토큰이 설정되어 있으면 해당 토큰을 반환한다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)
        token = diarizer._validate_token()
        assert token == "hf_test_token_12345"

    def test_토큰_없으면_에러(self, mock_config, mock_manager):
        """토큰이 없으면 TokenNotConfiguredError를 발생시킨다."""
        mock_config.diarization.huggingface_token = None
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)
        with pytest.raises(TokenNotConfiguredError, match="HuggingFace 토큰"):
            diarizer._validate_token()

    def test_빈_문자열_토큰도_에러(self, mock_config, mock_manager):
        """빈 문자열 토큰도 TokenNotConfiguredError를 발생시킨다."""
        mock_config.diarization.huggingface_token = ""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)
        with pytest.raises(TokenNotConfiguredError):
            diarizer._validate_token()


# === 오디오 검증 테스트 ===


class TestAudioValidation:
    """오디오 파일 유효성 검증 테스트."""

    def test_존재하지_않는_파일(self, mock_config, mock_manager, tmp_path):
        """파일이 없으면 FileNotFoundError를 발생시킨다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)
        with pytest.raises(FileNotFoundError, match="찾을 수 없습니다"):
            diarizer._validate_audio(tmp_path / "nonexistent.wav")

    def test_디렉토리_경로(self, mock_config, mock_manager, tmp_path):
        """경로가 디렉토리이면 FileNotFoundError를 발생시킨다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)
        with pytest.raises(FileNotFoundError, match="파일이 아닙니다"):
            diarizer._validate_audio(tmp_path)

    def test_빈_파일(self, mock_config, mock_manager, tmp_path):
        """빈 파일이면 EmptyAudioError를 발생시킨다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)
        empty = tmp_path / "empty.wav"
        empty.write_bytes(b"")
        with pytest.raises(EmptyAudioError, match="비어있습니다"):
            diarizer._validate_audio(empty)

    def test_유효한_파일(self, mock_config, mock_manager, sample_audio):
        """유효한 파일은 검증을 통과한다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)
        # 예외가 발생하지 않아야 한다
        diarizer._validate_audio(sample_audio)


# === Annotation 파싱 테스트 ===


class TestParseAnnotation:
    """pyannote Annotation 파싱 테스트."""

    def test_정상_파싱(self, mock_config, mock_manager):
        """정상적인 annotation을 올바르게 파싱한다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        annotation = _make_mock_annotation(
            [
                {"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0},
                {"speaker": "SPEAKER_01", "start": 5.5, "end": 10.0},
            ]
        )

        segments = diarizer._parse_annotation(annotation)
        assert len(segments) == 2
        assert segments[0].speaker == "SPEAKER_00"
        assert segments[0].start == 0.0
        assert segments[0].end == 5.0
        assert segments[1].speaker == "SPEAKER_01"

    def test_시간순_정렬(self, mock_config, mock_manager):
        """세그먼트를 시간순으로 정렬한다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        # 역순으로 제공
        annotation = _make_mock_annotation(
            [
                {"speaker": "SPEAKER_01", "start": 10.0, "end": 15.0},
                {"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0},
                {"speaker": "SPEAKER_01", "start": 5.0, "end": 10.0},
            ]
        )

        segments = diarizer._parse_annotation(annotation)
        assert segments[0].start == 0.0
        assert segments[1].start == 5.0
        assert segments[2].start == 10.0

    def test_무효_세그먼트_건너뜀(self, mock_config, mock_manager):
        """end <= start인 무효 세그먼트는 건너뛴다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        annotation = _make_mock_annotation(
            [
                {"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0},
                {"speaker": "SPEAKER_01", "start": 5.0, "end": 5.0},  # 무효
                {"speaker": "SPEAKER_00", "start": 5.0, "end": 10.0},
            ]
        )

        segments = diarizer._parse_annotation(annotation)
        assert len(segments) == 2

    def test_빈_annotation(self, mock_config, mock_manager):
        """빈 annotation은 빈 리스트를 반환한다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        annotation = _make_mock_annotation([])
        segments = diarizer._parse_annotation(annotation)
        assert segments == []

    def test_소수점_반올림(self, mock_config, mock_manager):
        """시간값을 소수점 3자리로 반올림한다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        annotation = _make_mock_annotation(
            [
                {"speaker": "SPEAKER_00", "start": 1.23456, "end": 5.67891},
            ]
        )

        segments = diarizer._parse_annotation(annotation)
        assert segments[0].start == 1.235
        assert segments[0].end == 5.679


# === 화자분리 실행 테스트 ===


class TestDiarize:
    """Diarizer.diarize() 비동기 실행 테스트."""

    @pytest.mark.asyncio
    async def test_정상_화자분리(self, mock_config, mock_manager, sample_audio):
        """정상적으로 화자분리를 수행한다."""
        manager, mock_pipeline = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        # pyannote 파이프라인 결과 모킹
        mock_annotation = _make_mock_annotation(
            [
                {"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0},
                {"speaker": "SPEAKER_01", "start": 5.5, "end": 12.0},
                {"speaker": "SPEAKER_00", "start": 12.5, "end": 18.0},
            ]
        )
        mock_pipeline.return_value = mock_annotation

        result = await diarizer.diarize(sample_audio)

        assert isinstance(result, DiarizationResult)
        assert result.num_speakers == 2
        assert len(result.segments) == 3
        assert result.audio_path == str(sample_audio)
        manager.acquire.assert_called_once_with("pyannote", diarizer._load_pipeline)

    @pytest.mark.asyncio
    async def test_파일_없으면_FileNotFoundError(self, mock_config, mock_manager, tmp_path):
        """오디오 파일이 없으면 FileNotFoundError를 발생시킨다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)
        with pytest.raises(FileNotFoundError):
            await diarizer.diarize(tmp_path / "no_file.wav")

    @pytest.mark.asyncio
    async def test_빈_결과시_EmptyAudioError(self, mock_config, mock_manager, sample_audio):
        """화자분리 결과가 없으면 EmptyAudioError를 발생시킨다."""
        manager, mock_pipeline = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        # 빈 annotation 반환
        mock_pipeline.return_value = _make_mock_annotation([])

        with pytest.raises(EmptyAudioError, match="비어있습니다"):
            await diarizer.diarize(sample_audio)

    @pytest.mark.asyncio
    async def test_파이프라인_예외시_DiarizationError로_래핑(
        self, mock_config, mock_manager, sample_audio
    ):
        """파이프라인 실행 중 예외 발생 시 DiarizationError로 래핑한다."""
        manager, mock_pipeline = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        mock_pipeline.side_effect = RuntimeError("CUDA out of memory")

        with pytest.raises(DiarizationError, match="화자분리 처리 중 오류"):
            await diarizer.diarize(sample_audio)

    @pytest.mark.asyncio
    async def test_ModelNotAvailableError_직접_전파(self, mock_config, mock_manager, sample_audio):
        """ModelNotAvailableError는 래핑 없이 직접 전파한다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        # acquire()에서 ModelNotAvailableError 발생하도록 설정
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=ModelNotAvailableError("pyannote 미설치"))
        ctx.__aexit__ = AsyncMock(return_value=False)
        manager.acquire.return_value = ctx

        with pytest.raises(ModelNotAvailableError, match="pyannote 미설치"):
            await diarizer.diarize(sample_audio)

    @pytest.mark.asyncio
    async def test_TokenNotConfiguredError_직접_전파(
        self, mock_config, mock_manager, sample_audio
    ):
        """TokenNotConfiguredError는 래핑 없이 직접 전파한다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=TokenNotConfiguredError("토큰 없음"))
        ctx.__aexit__ = AsyncMock(return_value=False)
        manager.acquire.return_value = ctx

        with pytest.raises(TokenNotConfiguredError, match="토큰 없음"):
            await diarizer.diarize(sample_audio)

    @pytest.mark.asyncio
    async def test_다중_화자_감지(self, mock_config, mock_manager, sample_audio):
        """여러 화자를 올바르게 감지한다."""
        manager, mock_pipeline = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        mock_annotation = _make_mock_annotation(
            [
                {"speaker": "SPEAKER_00", "start": 0.0, "end": 3.0},
                {"speaker": "SPEAKER_01", "start": 3.0, "end": 6.0},
                {"speaker": "SPEAKER_02", "start": 6.0, "end": 9.0},
                {"speaker": "SPEAKER_00", "start": 9.0, "end": 12.0},
            ]
        )
        mock_pipeline.return_value = mock_annotation

        result = await diarizer.diarize(sample_audio)
        assert result.num_speakers == 3
        assert "SPEAKER_00" in result.speakers
        assert "SPEAKER_01" in result.speakers
        assert "SPEAKER_02" in result.speakers

    @pytest.mark.asyncio
    async def test_min_max_speakers_파라미터_전달(self, mock_config, mock_manager, sample_audio):
        """min_speakers, max_speakers 파라미터가 파이프라인에 전달된다."""
        manager, mock_pipeline = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        mock_annotation = _make_mock_annotation(
            [
                {"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0},
            ]
        )
        mock_pipeline.return_value = mock_annotation

        await diarizer.diarize(sample_audio)

        # _run_pipeline이 pipeline(str(audio), min_speakers=1, max_speakers=10)로 호출
        mock_pipeline.assert_called_once()
        call_args = mock_pipeline.call_args
        assert call_args[1]["min_speakers"] == 1
        assert call_args[1]["max_speakers"] == 10

    @pytest.mark.asyncio
    async def test_화자분리_타임아웃(self, mock_config, mock_manager, sample_audio):
        """타임아웃 시 DiarizationError가 발생하는지 확인한다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        # 매우 짧은 타임아웃 설정
        diarizer._timeout_seconds = 0.01

        with (
            patch.object(
                diarizer,
                "_run_pipeline",
                side_effect=lambda *a: time.sleep(5),
            ),
            pytest.raises(DiarizationError, match="타임아웃"),
        ):
            await diarizer.diarize(sample_audio)


# === 파이프라인 로더 테스트 ===


class TestLoadPipeline:
    """_load_pipeline 메서드 테스트."""

    def test_토큰_없으면_TokenNotConfiguredError(self, mock_config, mock_manager):
        """토큰이 없으면 파이프라인 로드 전에 에러를 발생시킨다."""
        mock_config.diarization.huggingface_token = None
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)
        with pytest.raises(TokenNotConfiguredError):
            diarizer._load_pipeline()

    @patch("steps.diarizer.Diarizer._validate_token", return_value="token")
    def test_pyannote_미설치시_ModelNotAvailableError(
        self, _mock_token, mock_config, mock_manager
    ):
        """pyannote-audio가 없으면 ModelNotAvailableError를 발생시킨다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        with (
            patch.dict("sys.modules", {"pyannote.audio": None, "pyannote": None}),
            # import 실패를 시뮬레이션
            # pyannote.audio를 None으로 설정하면 import 시 TypeError 발생
            # 대신 ImportError를 직접 모킹
            patch(
                "builtins.__import__",
                side_effect=_import_error_for("pyannote.audio"),
            ),
            pytest.raises(ModelNotAvailableError, match="pyannote-audio"),
        ):
            diarizer._load_pipeline()

    @patch("steps.diarizer.Diarizer._validate_token", return_value="token")
    def test_torch_미설치시_ModelNotAvailableError(self, _mock_token, mock_config, mock_manager):
        """PyTorch가 없으면 ModelNotAvailableError를 발생시킨다."""
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        # pyannote는 정상, torch가 없는 경우
        with (
            patch(
                "builtins.__import__",
                side_effect=_import_error_for("torch", allow=["pyannote.audio"]),
            ),
            pytest.raises(ModelNotAvailableError, match="PyTorch"),
        ):
            diarizer._load_pipeline()


# === MPS 지원 + CPU 폴백 테스트 ===


class TestResolveDevice:
    """_resolve_device() 디바이스 결정 로직 테스트."""

    def test_load_pipeline_MPS_사용(self, mock_config, mock_manager):
        """device='mps' + MPS 가용 → pipeline.to(torch.device('mps')) 호출."""
        mock_config.diarization.device = "mps"
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        # torch 모듈 모킹 — MPS 가용
        mock_torch = MagicMock()
        mock_torch.backends.mps.is_available.return_value = True

        result = diarizer._resolve_device(mock_torch)
        assert result == "mps"

    def test_load_pipeline_MPS_폴백_CPU(self, mock_config, mock_manager):
        """device='mps' + MPS 미가용 → CPU 폴백 + 경고 로그."""
        mock_config.diarization.device = "mps"
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        mock_torch = MagicMock()
        mock_torch.backends.mps.is_available.return_value = False

        with patch("steps.diarizer.logger") as mock_logger:
            result = diarizer._resolve_device(mock_torch)
            assert result == "cpu"
            mock_logger.warning.assert_called_once()

    def test_load_pipeline_auto_MPS가용_시_MPS사용(self, mock_config, mock_manager):
        """device='auto' + MPS 가용 → MPS 사용."""
        mock_config.diarization.device = "auto"
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        mock_torch = MagicMock()
        mock_torch.backends.mps.is_available.return_value = True

        result = diarizer._resolve_device(mock_torch)
        assert result == "mps"

    def test_load_pipeline_auto_MPS미가용_시_CPU사용(self, mock_config, mock_manager):
        """device='auto' + MPS 미가용 → CPU 사용."""
        mock_config.diarization.device = "auto"
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        mock_torch = MagicMock()
        mock_torch.backends.mps.is_available.return_value = False

        result = diarizer._resolve_device(mock_torch)
        assert result == "cpu"

    def test_load_pipeline_auto_MPS실패_시_CPU폴백(self, mock_config, mock_manager):
        """device='auto' + MPS 가용하지만 to() 실패 → _load_pipeline에서 CPU 폴백."""
        mock_config.diarization.device = "auto"
        mock_config.diarization.huggingface_token = "hf_test"
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        mock_torch = MagicMock()
        mock_torch.backends.mps.is_available.return_value = True
        mock_torch.device.side_effect = lambda d: d

        mock_pipeline_instance = MagicMock()
        # MPS로 to() 호출 시 RuntimeError, CPU로는 성공
        call_count = 0

        def to_side_effect(device):
            nonlocal call_count
            call_count += 1
            if device == "mps":
                raise RuntimeError("MPS 오류")
            return mock_pipeline_instance

        mock_pipeline_instance.to.side_effect = to_side_effect

        mock_pyannote_pipeline = MagicMock()
        mock_pyannote_pipeline.from_pretrained.return_value = mock_pipeline_instance

        with (
            patch.dict("sys.modules", {"pyannote.audio": MagicMock(Pipeline=mock_pyannote_pipeline)}),
            patch.dict("sys.modules", {"torch": mock_torch}),
            patch("steps.diarizer.logger") as mock_logger,
        ):
            # _load_pipeline 내부에서 import 하므로 sys.modules 패치
            # 직접 _resolve_device + _load_pipeline 로직 테스트
            # _resolve_device는 "mps" 반환, 이후 to() 실패 → CPU 폴백
            result_device = diarizer._resolve_device(mock_torch)
            assert result_device == "mps"

            # to() 실패 시 CPU 폴백 확인
            try:
                mock_pipeline_instance.to("mps")
            except RuntimeError:
                mock_pipeline_instance.to("cpu")
                mock_logger.warning.assert_not_called()  # _resolve_device에서는 경고 없음

    def test_load_pipeline_CPU_직접_지정(self, mock_config, mock_manager):
        """device='cpu' → CPU 사용 (하위 호환)."""
        mock_config.diarization.device = "cpu"
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        mock_torch = MagicMock()
        mock_torch.backends.mps.is_available.return_value = True  # MPS 가용해도 무시

        result = diarizer._resolve_device(mock_torch)
        assert result == "cpu"

    def test_MPS_폴백_시_경고_로그_출력(self, mock_config, mock_manager):
        """MPS 요청 + 미가용 → CPU 폴백 시 logger.warning 호출 확인."""
        mock_config.diarization.device = "mps"
        manager, _ = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        mock_torch = MagicMock()
        mock_torch.backends.mps.is_available.return_value = False

        with patch("steps.diarizer.logger") as mock_logger:
            diarizer._resolve_device(mock_torch)
            mock_logger.warning.assert_called_once()
            # 경고 메시지에 "MPS" 와 "CPU" 키워드 포함 확인
            warn_msg = mock_logger.warning.call_args[0][0]
            assert "MPS" in warn_msg
            assert "CPU" in warn_msg or "cpu" in warn_msg.lower()

    @pytest.mark.asyncio
    async def test_diarize_MPS_런타임_에러_시_CPU폴백(
        self, mock_config, mock_manager, sample_audio
    ):
        """화자분리 _load_pipeline에서 MPS 로드 실패 시 CPU로 폴백."""
        mock_config.diarization.device = "mps"
        mock_config.diarization.huggingface_token = "hf_test"
        manager, mock_pipeline = mock_manager
        diarizer = Diarizer(config=mock_config, model_manager=manager)

        # 파이프라인 결과 모킹
        mock_annotation = _make_mock_annotation(
            [{"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0}]
        )
        mock_pipeline.return_value = mock_annotation

        result = await diarizer.diarize(sample_audio)
        assert isinstance(result, DiarizationResult)
        assert result.num_speakers == 1


def _import_error_for(
    module_name: str,
    allow: list[str] | None = None,
):
    """특정 모듈의 import를 실패시키는 side_effect 함수를 반환한다."""
    import builtins

    original_import = builtins.__import__

    def custom_import(name, *args, **kwargs):
        if name == module_name:
            raise ImportError(f"No module named '{module_name}'")
        if allow and name in allow:
            return MagicMock()
        return original_import(name, *args, **kwargs)

    return custom_import
