"""
STT 전사기 모듈 테스트 (Transcriber Module Tests)

목적: steps/transcriber.py의 Transcriber 클래스를 단위 테스트한다.
테스트 범위:
    - 초기화 및 설정 로드
    - 오디오 파일 유효성 검증
    - mlx_whisper 모듈 로드 및 에러 처리
    - 전사 결과 파싱 및 한국어 NFC 정규화
    - ModelLoadManager 연동 (모킹)
    - 체크포인트 저장/복원
    - 에러 계층 구조
의존성: pytest, pytest-asyncio, unittest.mock
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig, reset_config
from steps.transcriber import (
    EmptyAudioError,
    ModelNotAvailableError,
    Transcriber,
    TranscriptionError,
    TranscriptResult,
    TranscriptSegment,
)



# === 픽스처 ===


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    """각 테스트 전 싱글턴 인스턴스 초기화."""
    reset_config()
    from core.model_manager import reset_model_manager

    reset_model_manager()


@pytest.fixture
def config() -> AppConfig:
    """테스트용 AppConfig 인스턴스."""
    return AppConfig()


@pytest.fixture
def mock_manager() -> MagicMock:
    """모킹된 ModelLoadManager 인스턴스.

    acquire() 컨텍스트 매니저에서 mock_whisper_module을 반환한다.
    """
    manager = MagicMock()

    # mock whisper 모듈 생성
    mock_whisper = MagicMock()
    mock_whisper.transcribe.return_value = _make_raw_result()

    # acquire() 컨텍스트 매니저 설정
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_whisper)
    ctx.__aexit__ = AsyncMock(return_value=False)
    manager.acquire.return_value = ctx

    return manager


@pytest.fixture
def transcriber(config: AppConfig, mock_manager: MagicMock) -> Transcriber:
    """모킹된 매니저를 사용하는 Transcriber 인스턴스."""
    return Transcriber(config=config, model_manager=mock_manager)


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    """테스트용 더미 오디오 파일 생성."""
    audio = tmp_path / "test_audio.wav"
    # 더미 WAV 데이터 (실제 오디오가 아니지만 파일 존재 확인용)
    audio.write_bytes(b"\x00" * 1024)
    return audio


def _make_raw_result(
    segments: list[dict[str, Any]] | None = None,
    text: str = "안녕하세요 회의를 시작하겠습니다",
    language: str = "ko",
) -> dict[str, Any]:
    """mlx_whisper.transcribe() 반환값을 생성하는 헬퍼."""
    if segments is None:
        segments = [
            {
                "id": 0,
                "text": " 안녕하세요",
                "start": 0.0,
                "end": 2.5,
                "avg_logprob": -0.3,
                "no_speech_prob": 0.01,
                "tokens": [50364, 2345],
                "temperature": 0.0,
                "compression_ratio": 1.2,
            },
            {
                "id": 1,
                "text": " 회의를 시작하겠습니다",
                "start": 2.5,
                "end": 5.0,
                "avg_logprob": -0.25,
                "no_speech_prob": 0.02,
                "tokens": [50564, 3456],
                "temperature": 0.0,
                "compression_ratio": 1.1,
            },
        ]

    return {
        "text": text,
        "language": language,
        "segments": segments,
    }


# === TranscriptSegment 테스트 ===


class TestTranscriptSegment:
    """TranscriptSegment 데이터 클래스 테스트."""

    def test_생성_기본값(self) -> None:
        """기본값으로 세그먼트를 생성한다."""
        seg = TranscriptSegment(text="안녕하세요", start=0.0, end=2.5)
        assert seg.text == "안녕하세요"
        assert seg.start == 0.0
        assert seg.end == 2.5
        assert seg.avg_logprob == 0.0
        assert seg.no_speech_prob == 0.0

    def test_생성_전체값(self) -> None:
        """모든 필드를 지정하여 세그먼트를 생성한다."""
        seg = TranscriptSegment(
            text="테스트",
            start=1.0,
            end=3.0,
            avg_logprob=-0.5,
            no_speech_prob=0.1,
        )
        assert seg.avg_logprob == -0.5
        assert seg.no_speech_prob == 0.1

    def test_딕셔너리_변환(self) -> None:
        """to_dict()가 올바른 딕셔너리를 반환한다."""
        seg = TranscriptSegment(
            text="테스트",
            start=1.0,
            end=3.0,
            avg_logprob=-0.3,
            no_speech_prob=0.05,
        )
        d = seg.to_dict()
        assert d["text"] == "테스트"
        assert d["start"] == 1.0
        assert d["end"] == 3.0
        assert d["avg_logprob"] == -0.3


# === TranscriptResult 테스트 ===


class TestTranscriptResult:
    """TranscriptResult 데이터 클래스 테스트."""

    def test_생성(self) -> None:
        """TranscriptResult를 올바르게 생성한다."""
        segments = [
            TranscriptSegment(text="안녕", start=0.0, end=1.0),
            TranscriptSegment(text="반갑습니다", start=1.0, end=2.5),
        ]
        result = TranscriptResult(
            segments=segments,
            full_text="안녕 반갑습니다",
            language="ko",
            audio_path="/tmp/audio.wav",
        )
        assert len(result.segments) == 2
        assert result.full_text == "안녕 반갑습니다"
        assert result.language == "ko"

    def test_딕셔너리_변환(self) -> None:
        """to_dict()가 중첩 구조를 올바르게 변환한다."""
        segments = [
            TranscriptSegment(text="테스트", start=0.0, end=1.0),
        ]
        result = TranscriptResult(
            segments=segments,
            full_text="테스트",
            language="ko",
            audio_path="/tmp/audio.wav",
        )
        d = result.to_dict()
        assert len(d["segments"]) == 1
        assert d["segments"][0]["text"] == "테스트"
        assert d["full_text"] == "테스트"
        assert d["language"] == "ko"
        assert d["audio_path"] == "/tmp/audio.wav"

    def test_체크포인트_저장_및_복원(self, tmp_path: Path) -> None:
        """체크포인트 저장 후 복원하면 동일한 데이터가 복원된다."""
        segments = [
            TranscriptSegment(
                text="안녕하세요",
                start=0.0,
                end=2.5,
                avg_logprob=-0.3,
                no_speech_prob=0.01,
            ),
            TranscriptSegment(
                text="회의를 시작합니다",
                start=2.5,
                end=5.0,
                avg_logprob=-0.25,
                no_speech_prob=0.02,
            ),
        ]
        original = TranscriptResult(
            segments=segments,
            full_text="안녕하세요 회의를 시작합니다",
            language="ko",
            audio_path="/tmp/audio.wav",
        )

        checkpoint_path = tmp_path / "sub" / "transcript.json"
        original.save_checkpoint(checkpoint_path)

        # 파일이 생성되었는지 확인
        assert checkpoint_path.exists()

        # JSON 내용 확인
        with open(checkpoint_path, encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["segments"]) == 2
        assert data["full_text"] == "안녕하세요 회의를 시작합니다"

        # 복원
        restored = TranscriptResult.from_checkpoint(checkpoint_path)
        assert len(restored.segments) == 2
        assert restored.segments[0].text == "안녕하세요"
        assert restored.segments[1].start == 2.5
        assert restored.full_text == "안녕하세요 회의를 시작합니다"
        assert restored.language == "ko"

    def test_체크포인트_한국어_인코딩(self, tmp_path: Path) -> None:
        """체크포인트 파일이 한국어를 올바르게 저장한다 (ensure_ascii=False)."""
        result = TranscriptResult(
            segments=[
                TranscriptSegment(text="한글 테스트", start=0.0, end=1.0),
            ],
            full_text="한글 테스트",
            language="ko",
            audio_path="/tmp/audio.wav",
        )
        checkpoint_path = tmp_path / "transcript.json"
        result.save_checkpoint(checkpoint_path)

        # 파일 내용에 한국어가 그대로 있는지 확인 (이스케이프 안 됨)
        raw_content = checkpoint_path.read_text(encoding="utf-8")
        assert "한글 테스트" in raw_content
        assert "\\u" not in raw_content  # 유니코드 이스케이프 없음


# === Transcriber 초기화 테스트 ===


class TestTranscriberInit:
    """Transcriber 초기화 테스트."""

    def test_기본_초기화(self, config: AppConfig, mock_manager: MagicMock) -> None:
        """config와 manager로 초기화된다."""
        t = Transcriber(config=config, model_manager=mock_manager)
        assert t._model_name == "whisper-medium-ko-zeroth"
        assert t._language == "ko"
        assert t._beam_size == 5

    def test_커스텀_설정(self, mock_manager: MagicMock) -> None:
        """커스텀 STT 설정이 반영된다."""
        custom_config = AppConfig(
            stt={"model_name": "custom-model", "language": "en", "beam_size": 3}
        )
        t = Transcriber(config=custom_config, model_manager=mock_manager)
        assert t._model_name == "custom-model"
        assert t._language == "en"
        assert t._beam_size == 3

    def test_condition_on_previous_text_기본값(
        self, config: AppConfig, mock_manager: MagicMock
    ) -> None:
        """condition_on_previous_text 기본값은 True이다."""
        t = Transcriber(config=config, model_manager=mock_manager)
        assert t._condition_on_previous_text is True

    def test_condition_on_previous_text_False(
        self, mock_manager: MagicMock
    ) -> None:
        """condition_on_previous_text=False가 올바르게 전파된다."""
        custom_config = AppConfig(
            stt={"condition_on_previous_text": False}
        )
        t = Transcriber(config=custom_config, model_manager=mock_manager)
        assert t._condition_on_previous_text is False


# === 오디오 파일 유효성 검증 테스트 ===


class TestValidateAudio:
    """오디오 파일 유효성 검증 테스트."""

    def test_파일_없음(self, transcriber: Transcriber) -> None:
        """존재하지 않는 파일에 대해 FileNotFoundError를 발생시킨다."""
        with pytest.raises(FileNotFoundError, match="찾을 수 없습니다"):
            transcriber._validate_audio(Path("/nonexistent/audio.wav"))

    def test_디렉토리_지정(self, transcriber: Transcriber, tmp_path: Path) -> None:
        """디렉토리를 지정하면 FileNotFoundError를 발생시킨다."""
        with pytest.raises(FileNotFoundError, match="파일이 아닙니다"):
            transcriber._validate_audio(tmp_path)

    def test_빈_파일(self, transcriber: Transcriber, tmp_path: Path) -> None:
        """빈 파일에 대해 EmptyAudioError를 발생시킨다."""
        empty_file = tmp_path / "empty.wav"
        empty_file.write_bytes(b"")
        with pytest.raises(EmptyAudioError, match="비어있습니다"):
            transcriber._validate_audio(empty_file)

    def test_유효한_파일(self, transcriber: Transcriber, audio_file: Path) -> None:
        """유효한 파일은 예외 없이 통과한다."""
        transcriber._validate_audio(audio_file)  # 예외 없음


# === 한국어 NFC 정규화 테스트 ===


class TestNormalizeKoreanText:
    """한국어 텍스트 유니코드 NFC 정규화 테스트."""

    def test_NFC_정규화(self) -> None:
        """NFD 형태의 한글이 NFC로 정규화된다."""
        # NFD: ㅎ+ㅏ+ㄴ+ㄱ+ㅜ+ㄱ (분리형)
        nfd_text = "\u1112\u1161\u11ab\u1100\u116e\u11a8"
        result = Transcriber._normalize_korean_text(nfd_text)
        assert result == "한국"

    def test_공백_제거(self) -> None:
        """앞뒤 공백이 제거된다."""
        result = Transcriber._normalize_korean_text("  안녕하세요  ")
        assert result == "안녕하세요"

    def test_이미_NFC(self) -> None:
        """이미 NFC인 텍스트는 변경되지 않는다."""
        text = "안녕하세요 회의를 시작합니다"
        result = Transcriber._normalize_korean_text(text)
        assert result == text


# === 세그먼트 파싱 테스트 ===


class TestParseSegments:
    """mlx_whisper 원시 결과 파싱 테스트."""

    def test_정상_파싱(self, transcriber: Transcriber) -> None:
        """정상적인 결과를 올바르게 파싱한다."""
        raw = _make_raw_result()
        segments = transcriber._parse_segments(raw)

        assert len(segments) == 2
        assert segments[0].text == "안녕하세요"
        assert segments[0].start == 0.0
        assert segments[0].end == 2.5
        assert segments[0].avg_logprob == -0.3
        assert segments[1].text == "회의를 시작하겠습니다"
        assert segments[1].start == 2.5
        assert segments[1].end == 5.0

    def test_빈_세그먼트_필터링(self, transcriber: Transcriber) -> None:
        """빈 텍스트 세그먼트는 필터링된다."""
        raw = _make_raw_result(
            segments=[
                {"text": "유효", "start": 0.0, "end": 1.0},
                {"text": "", "start": 1.0, "end": 2.0},
                {"text": "   ", "start": 2.0, "end": 3.0},
                {"text": "텍스트", "start": 3.0, "end": 4.0},
            ]
        )
        segments = transcriber._parse_segments(raw)
        assert len(segments) == 2
        assert segments[0].text == "유효"
        assert segments[1].text == "텍스트"

    def test_빈_결과(self, transcriber: Transcriber) -> None:
        """세그먼트가 없는 결과는 빈 리스트를 반환한다."""
        raw = _make_raw_result(segments=[])
        segments = transcriber._parse_segments(raw)
        assert len(segments) == 0

    def test_segments_키_없음(self, transcriber: Transcriber) -> None:
        """segments 키가 없어도 안전하게 빈 리스트를 반환한다."""
        raw: dict[str, Any] = {"text": "test", "language": "ko"}
        segments = transcriber._parse_segments(raw)
        assert len(segments) == 0

    def test_누락_필드_기본값(self, transcriber: Transcriber) -> None:
        """세그먼트에서 누락된 필드는 기본값(0.0)을 사용한다."""
        raw = _make_raw_result(segments=[{"text": "테스트", "start": 1.0}])
        segments = transcriber._parse_segments(raw)
        assert len(segments) == 1
        assert segments[0].end == 0.0
        assert segments[0].avg_logprob == 0.0

    def test_NFC_정규화_적용(self, transcriber: Transcriber) -> None:
        """파싱 시 한국어 텍스트가 NFC 정규화된다."""
        # NFD 형태로 입력
        nfd = "\u1112\u1161\u11ab\u1100\u116e\u11a8"
        raw = _make_raw_result(segments=[{"text": nfd, "start": 0.0, "end": 1.0}])
        segments = transcriber._parse_segments(raw)
        assert segments[0].text == "한국"


# === Whisper 모듈 로드 테스트 ===


class TestLoadWhisperModule:
    """mlx_whisper 모듈 로드 테스트."""

    def test_임포트_실패(self, transcriber: Transcriber) -> None:
        """mlx_whisper가 없으면 ModelNotAvailableError를 발생시킨다."""
        with (
            patch.dict("sys.modules", {"mlx_whisper": None}),
            pytest.raises(ModelNotAvailableError, match="mlx-whisper가 설치되어"),
        ):
            transcriber._load_whisper_module()

    def test_임포트_성공(self, transcriber: Transcriber) -> None:
        """mlx_whisper 모듈이 있으면 모듈 객체를 반환한다."""
        mock_module = MagicMock()
        with patch.dict("sys.modules", {"mlx_whisper": mock_module}):
            result = transcriber._load_whisper_module()
            assert result is mock_module


# === 전사 (transcribe) 테스트 ===


class TestTranscribe:
    """Transcriber.transcribe() 비동기 전사 테스트."""

    @pytest.mark.asyncio
    async def test_정상_전사(
        self,
        transcriber: Transcriber,
        mock_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """정상적인 전사 흐름을 검증한다."""
        result = await transcriber.transcribe(audio_file)

        # ModelLoadManager.acquire가 "whisper"로 호출됨
        mock_manager.acquire.assert_called_once()
        call_args = mock_manager.acquire.call_args
        assert call_args[0][0] == "whisper"

        # 결과 검증
        assert isinstance(result, TranscriptResult)
        assert len(result.segments) == 2
        assert result.segments[0].text == "안녕하세요"
        assert result.segments[1].text == "회의를 시작하겠습니다"
        assert result.full_text == "안녕하세요 회의를 시작하겠습니다"
        assert result.language == "ko"
        assert str(audio_file) in result.audio_path

    @pytest.mark.asyncio
    async def test_whisper_transcribe_호출_파라미터(
        self,
        transcriber: Transcriber,
        mock_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """whisper.transcribe()에 올바른 파라미터가 전달된다."""
        # acquire 컨텍스트에서 반환되는 mock whisper 모듈 가져오기
        ctx = mock_manager.acquire.return_value
        mock_whisper = ctx.__aenter__.return_value

        await transcriber.transcribe(audio_file)

        # transcribe 호출 확인
        mock_whisper.transcribe.assert_called_once()
        call_kwargs = mock_whisper.transcribe.call_args
        assert call_kwargs[0][0] == str(audio_file)
        assert call_kwargs[1]["path_or_hf_repo"] == "whisper-medium-ko-zeroth"
        assert call_kwargs[1]["language"] == "ko"
        # beam_size는 decode_options → DecodingOptions로 전달됨
        assert call_kwargs[1]["beam_size"] == 5
        assert call_kwargs[1]["word_timestamps"] is True

    @pytest.mark.asyncio
    async def test_beam_size_decode_options_전달(
        self,
        transcriber: Transcriber,
        mock_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """beam_size가 transcribe() 호출의 kwargs에 포함되어 전달된다."""
        ctx = mock_manager.acquire.return_value
        mock_whisper = ctx.__aenter__.return_value

        await transcriber.transcribe(audio_file)

        call_kwargs = mock_whisper.transcribe.call_args[1]
        # beam_size가 kwargs에 존재해야 함
        assert "beam_size" in call_kwargs
        assert call_kwargs["beam_size"] == 5  # STTConfig 기본값

    @pytest.mark.asyncio
    async def test_커스텀_beam_size_전달(
        self,
        config: AppConfig,
        mock_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """beam_size=10 설정 시 10이 transcribe()에 전달된다."""
        custom_config = AppConfig(stt={"beam_size": 10})
        t = Transcriber(config=custom_config, model_manager=mock_manager)

        ctx = mock_manager.acquire.return_value
        mock_whisper = ctx.__aenter__.return_value

        await t.transcribe(audio_file)

        call_kwargs = mock_whisper.transcribe.call_args[1]
        assert call_kwargs["beam_size"] == 10

    def test_batch_size_캐싱(
        self,
        config: AppConfig,
        mock_manager: MagicMock,
    ) -> None:
        """Transcriber 초기화 시 _batch_size가 STTConfig 기본값(16)으로 캐싱된다."""
        t = Transcriber(config=config, model_manager=mock_manager)
        assert t._batch_size == 16

    def test_커스텀_batch_size_캐싱(
        self,
        mock_manager: MagicMock,
    ) -> None:
        """batch_size=8 설정 시 _batch_size가 8로 캐싱된다."""
        custom_config = AppConfig(stt={"batch_size": 8})
        t = Transcriber(config=custom_config, model_manager=mock_manager)
        assert t._batch_size == 8

    @pytest.mark.asyncio
    async def test_batch_size_미전달_확인(
        self,
        transcriber: Transcriber,
        mock_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """batch_size는 mlx-whisper API에 전달되지 않는다 (미지원 파라미터)."""
        ctx = mock_manager.acquire.return_value
        mock_whisper = ctx.__aenter__.return_value

        await transcriber.transcribe(audio_file)

        call_kwargs = mock_whisper.transcribe.call_args[1]
        assert "batch_size" not in call_kwargs

    @pytest.mark.asyncio
    async def test_파일_없음_에러(self, transcriber: Transcriber) -> None:
        """존재하지 않는 파일에 대해 FileNotFoundError를 발생시킨다."""
        with pytest.raises(FileNotFoundError):
            await transcriber.transcribe(Path("/nonexistent/audio.wav"))

    @pytest.mark.asyncio
    async def test_빈_파일_에러(self, transcriber: Transcriber, tmp_path: Path) -> None:
        """빈 파일에 대해 EmptyAudioError를 발생시킨다."""
        empty = tmp_path / "empty.wav"
        empty.write_bytes(b"")
        with pytest.raises(EmptyAudioError, match="비어있습니다"):
            await transcriber.transcribe(empty)

    @pytest.mark.asyncio
    async def test_빈_전사결과_에러(
        self,
        config: AppConfig,
        audio_file: Path,
    ) -> None:
        """전사 결과가 비어있으면 EmptyAudioError를 발생시킨다."""
        # 빈 세그먼트 반환하도록 설정
        manager = MagicMock()
        mock_whisper = MagicMock()
        mock_whisper.transcribe.return_value = _make_raw_result(segments=[], text="")
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_whisper)
        ctx.__aexit__ = AsyncMock(return_value=False)
        manager.acquire.return_value = ctx

        transcriber = Transcriber(config=config, model_manager=manager)

        with pytest.raises(EmptyAudioError, match="전사 결과가 비어있습니다"):
            await transcriber.transcribe(audio_file)

    @pytest.mark.asyncio
    async def test_모델_로드_실패(
        self,
        config: AppConfig,
        audio_file: Path,
    ) -> None:
        """모델 로드 실패 시 ModelNotAvailableError가 전파된다."""
        manager = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(side_effect=ModelNotAvailableError("mlx-whisper 미설치"))
        ctx.__aexit__ = AsyncMock(return_value=False)
        manager.acquire.return_value = ctx

        transcriber = Transcriber(config=config, model_manager=manager)

        with pytest.raises(ModelNotAvailableError, match="mlx-whisper"):
            await transcriber.transcribe(audio_file)

    @pytest.mark.asyncio
    async def test_전사_중_예외_래핑(
        self,
        config: AppConfig,
        audio_file: Path,
    ) -> None:
        """전사 중 발생한 일반 예외는 TranscriptionError로 래핑된다."""
        manager = MagicMock()
        mock_whisper = MagicMock()
        mock_whisper.transcribe.side_effect = RuntimeError("GPU 메모리 부족")
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_whisper)
        ctx.__aexit__ = AsyncMock(return_value=False)
        manager.acquire.return_value = ctx

        transcriber = Transcriber(config=config, model_manager=manager)

        with pytest.raises(TranscriptionError, match="전사 처리 중 오류"):
            await transcriber.transcribe(audio_file)

    @pytest.mark.asyncio
    async def test_한국어_NFC_정규화_전체텍스트(
        self,
        config: AppConfig,
        audio_file: Path,
    ) -> None:
        """전체 텍스트(full_text)도 NFC 정규화가 적용된다."""
        # NFD 형태 텍스트 반환
        nfd = "\u1112\u1161\u11ab\u1100\u116e\u11a8"

        manager = MagicMock()
        mock_whisper = MagicMock()
        mock_whisper.transcribe.return_value = _make_raw_result(
            segments=[
                {"text": nfd, "start": 0.0, "end": 1.0},
            ],
            text=nfd,
        )
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_whisper)
        ctx.__aexit__ = AsyncMock(return_value=False)
        manager.acquire.return_value = ctx

        transcriber = Transcriber(config=config, model_manager=manager)
        result = await transcriber.transcribe(audio_file)

        assert result.full_text == "한국"
        assert result.segments[0].text == "한국"


# === initial_prompt / vad_clip_timestamps 전달 테스트 ===


def _make_fallback_mock_whisper(
    raw_result: dict[str, Any] | None = None,
) -> MagicMock:
    """beam search에서 NotImplementedError를 발생시켜 폴백 경로를 타도록 설정된 mock whisper.

    첫 번째 호출: NotImplementedError (beam search 미지원 시뮬레이션)
    두 번째 호출: 정상 결과 반환 (greedy decoding 폴백)

    Args:
        raw_result: 폴백 시 반환할 결과 딕셔너리 (None이면 기본값 사용)

    Returns:
        mock whisper 모듈
    """
    if raw_result is None:
        raw_result = _make_raw_result()
    mock_whisper = MagicMock()
    mock_whisper.transcribe.side_effect = [NotImplementedError("beam 미지원"), raw_result]
    return mock_whisper


class TestInitialPromptAndVadClipTimestamps:
    """initial_prompt 및 vad_clip_timestamps 전달 테스트."""

    @pytest.mark.asyncio
    async def test_initial_prompt_전달(
        self,
        audio_file: Path,
    ) -> None:
        """initial_prompt가 설정되면 폴백 경로에서 whisper.transcribe에 전달된다."""
        # initial_prompt가 설정된 config 생성
        config = AppConfig()
        # getattr 폴백을 위해 직접 속성 설정
        config.stt.initial_prompt = "회의 전사 테스트 프롬프트"  # type: ignore[attr-defined]

        # 폴백 경로를 타는 mock whisper 설정
        mock_whisper = _make_fallback_mock_whisper()

        manager = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_whisper)
        ctx.__aexit__ = AsyncMock(return_value=False)
        manager.acquire.return_value = ctx

        transcriber = Transcriber(config=config, model_manager=manager)
        await transcriber.transcribe(audio_file)

        # 두 번째 호출(폴백)의 kwargs 확인
        assert mock_whisper.transcribe.call_count == 2
        fallback_call_kwargs = mock_whisper.transcribe.call_args_list[1][1]
        assert "initial_prompt" in fallback_call_kwargs
        assert fallback_call_kwargs["initial_prompt"] == "회의 전사 테스트 프롬프트"

    @pytest.mark.asyncio
    async def test_vad_clip_timestamps_전달(
        self,
        config: AppConfig,
        audio_file: Path,
    ) -> None:
        """vad_clip_timestamps가 전달되면 폴백 경로에서 clip_timestamps로 전달된다."""
        mock_whisper = _make_fallback_mock_whisper()

        manager = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_whisper)
        ctx.__aexit__ = AsyncMock(return_value=False)
        manager.acquire.return_value = ctx

        transcriber = Transcriber(config=config, model_manager=manager)
        timestamps = [1.0, 5.0, 8.0, 12.0]
        await transcriber.transcribe(audio_file, vad_clip_timestamps=timestamps)

        # 두 번째 호출(폴백)의 kwargs 확인
        assert mock_whisper.transcribe.call_count == 2
        fallback_call_kwargs = mock_whisper.transcribe.call_args_list[1][1]
        assert "clip_timestamps" in fallback_call_kwargs
        assert fallback_call_kwargs["clip_timestamps"] == [1.0, 5.0, 8.0, 12.0]

    @pytest.mark.asyncio
    async def test_initial_prompt_None이면_kwargs에_미포함(
        self,
        config: AppConfig,
        audio_file: Path,
    ) -> None:
        """initial_prompt가 None이면 폴백 경로의 kwargs에 포함되지 않는다."""
        # 기본 config에는 initial_prompt가 없으므로 getattr 결과 None
        mock_whisper = _make_fallback_mock_whisper()

        manager = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_whisper)
        ctx.__aexit__ = AsyncMock(return_value=False)
        manager.acquire.return_value = ctx

        transcriber = Transcriber(config=config, model_manager=manager)
        assert transcriber._initial_prompt is None  # None 확인

        await transcriber.transcribe(audio_file)

        # 두 번째 호출(폴백)의 kwargs에서 initial_prompt 없음 확인
        assert mock_whisper.transcribe.call_count == 2
        fallback_call_kwargs = mock_whisper.transcribe.call_args_list[1][1]
        assert "initial_prompt" not in fallback_call_kwargs

    @pytest.mark.asyncio
    async def test_vad_clip_timestamps_None이면_kwargs에_미포함(
        self,
        config: AppConfig,
        audio_file: Path,
    ) -> None:
        """vad_clip_timestamps가 None이면 폴백 경로의 kwargs에 clip_timestamps가 없다."""
        mock_whisper = _make_fallback_mock_whisper()

        manager = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_whisper)
        ctx.__aexit__ = AsyncMock(return_value=False)
        manager.acquire.return_value = ctx

        transcriber = Transcriber(config=config, model_manager=manager)
        # vad_clip_timestamps=None (기본값)
        await transcriber.transcribe(audio_file)

        # 두 번째 호출(폴백)의 kwargs에서 clip_timestamps 없음 확인
        assert mock_whisper.transcribe.call_count == 2
        fallback_call_kwargs = mock_whisper.transcribe.call_args_list[1][1]
        assert "clip_timestamps" not in fallback_call_kwargs


# === 에러 계층 구조 테스트 ===


class TestErrorHierarchy:
    """에러 클래스 계층 구조 테스트."""

    def test_TranscriptionError는_Exception_상속(self) -> None:
        """TranscriptionError는 Exception을 상속한다."""
        assert issubclass(TranscriptionError, Exception)

    def test_ModelNotAvailableError는_TranscriptionError_상속(self) -> None:
        """ModelNotAvailableError는 TranscriptionError를 상속한다."""
        assert issubclass(ModelNotAvailableError, TranscriptionError)

    def test_EmptyAudioError는_TranscriptionError_상속(self) -> None:
        """EmptyAudioError는 TranscriptionError를 상속한다."""
        assert issubclass(EmptyAudioError, TranscriptionError)

    def test_TranscriptionError_catch로_하위_에러_포착(self) -> None:
        """TranscriptionError로 하위 에러를 모두 포착할 수 있다."""
        with pytest.raises(TranscriptionError):
            raise ModelNotAvailableError("테스트")

        with pytest.raises(TranscriptionError):
            raise EmptyAudioError("테스트")
