"""
STT 전사기 모듈 (Speech-to-Text Transcriber Module)

목적: mlx-whisper를 사용하여 한국어 오디오를 텍스트 세그먼트로 전사한다.
주요 기능:
    - whisper-medium-ko-zeroth 모델 기반 한국어 전사
    - ModelLoadManager를 통한 모델 라이프사이클 관리 (뮤텍스)
    - 세그먼트별 타임스탬프 포함 결과 반환
    - 유니코드 NFC 정규화로 한국어 텍스트 일관성 보장
    - 비동기(async) 인터페이스 지원
    - JSON 체크포인트 저장 지원
의존성: mlx-whisper, config 모듈, core/model_manager 모듈
"""

from __future__ import annotations

import asyncio
import json
import logging
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from config import AppConfig, get_config
from core.model_manager import ModelLoadManager, get_model_manager

logger = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    """전사 결과의 단일 세그먼트를 나타내는 데이터 클래스.

    Attributes:
        text: 전사된 텍스트
        start: 세그먼트 시작 시간 (초)
        end: 세그먼트 종료 시간 (초)
        avg_logprob: 평균 로그 확률 (전사 신뢰도 지표)
        no_speech_prob: 무음 확률
    """

    text: str
    start: float
    end: float
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화용).

        Returns:
            세그먼트 데이터 딕셔너리
        """
        return asdict(self)


@dataclass
class TranscriptResult:
    """전체 전사 결과를 담는 데이터 클래스.

    Attributes:
        segments: 전사된 세그먼트 목록
        full_text: 전체 전사 텍스트
        language: 감지/지정된 언어 코드
        audio_path: 원본 오디오 파일 경로 문자열
    """

    segments: list[TranscriptSegment]
    full_text: str
    language: str
    audio_path: str

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화/체크포인트 저장용).

        Returns:
            전체 전사 결과 딕셔너리
        """
        return {
            "segments": [seg.to_dict() for seg in self.segments],
            "full_text": self.full_text,
            "language": self.language,
            "audio_path": self.audio_path,
        }

    def save_checkpoint(self, output_path: Path) -> None:
        """전사 결과를 JSON 파일로 저장한다 (체크포인트).

        Args:
            output_path: 저장할 JSON 파일 경로
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info(f"전사 체크포인트 저장: {output_path}")

    @classmethod
    def from_checkpoint(cls, checkpoint_path: Path) -> TranscriptResult:
        """체크포인트 JSON 파일에서 전사 결과를 복원한다.

        Args:
            checkpoint_path: 체크포인트 JSON 파일 경로

        Returns:
            복원된 TranscriptResult 인스턴스

        Raises:
            FileNotFoundError: 체크포인트 파일이 없을 때
            json.JSONDecodeError: JSON 파싱 실패 시
        """
        with open(checkpoint_path, encoding="utf-8") as f:
            data = json.load(f)

        segments = [
            TranscriptSegment(**seg) for seg in data.get("segments", [])
        ]

        return cls(
            segments=segments,
            full_text=data.get("full_text", ""),
            language=data.get("language", "ko"),
            audio_path=data.get("audio_path", ""),
        )


class TranscriptionError(Exception):
    """전사 처리 중 발생하는 에러의 기본 클래스."""


class ModelNotAvailableError(TranscriptionError):
    """mlx-whisper 모델을 로드할 수 없을 때 발생한다."""


class EmptyAudioError(TranscriptionError):
    """오디오 파일이 비어있거나 전사 결과가 없을 때 발생한다."""


class Transcriber:
    """mlx-whisper 기반 한국어 STT 전사기.

    ModelLoadManager를 통해 whisper 모델의 메모리 라이프사이클을 관리하고,
    config.yaml의 STT 설정에 따라 전사를 수행한다.

    Args:
        config: 애플리케이션 설정 인스턴스 (None이면 싱글턴 사용)
        model_manager: 모델 로드 매니저 (None이면 싱글턴 사용)

    사용 예시:
        transcriber = Transcriber(config, model_manager)
        result = await transcriber.transcribe(Path("audio.wav"))
        print(result.full_text)
    """

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        model_manager: Optional[ModelLoadManager] = None,
    ) -> None:
        """Transcriber를 초기화한다.

        Args:
            config: 애플리케이션 설정 (None이면 get_config() 사용)
            model_manager: 모델 매니저 (None이면 get_model_manager() 사용)
        """
        self._config = config or get_config()
        self._manager = model_manager or get_model_manager()

        # STT 설정 캐시
        self._model_name = self._config.stt.model_name
        self._language = self._config.stt.language
        self._beam_size = self._config.stt.beam_size

        logger.info(
            f"Transcriber 초기화: model={self._model_name}, "
            f"language={self._language}, beam_size={self._beam_size}"
        )

    def _load_whisper_module(self) -> Any:
        """mlx_whisper 모듈을 임포트하여 반환한다.

        ModelLoadManager의 loader 함수로 사용된다.
        모듈 임포트 시점에 MLX 런타임이 초기화된다.

        Returns:
            mlx_whisper 모듈 객체

        Raises:
            ModelNotAvailableError: mlx-whisper가 설치되지 않았을 때
        """
        try:
            import mlx_whisper  # type: ignore[import-untyped]

            logger.info("mlx_whisper 모듈 로드 완료")
            return mlx_whisper
        except ImportError as e:
            raise ModelNotAvailableError(
                "mlx-whisper가 설치되어 있지 않습니다. "
                "'pip install mlx-whisper'로 설치하세요."
            ) from e

    def _validate_audio(self, audio_path: Path) -> None:
        """오디오 파일의 유효성을 검증한다.

        Args:
            audio_path: 검증할 오디오 파일 경로

        Raises:
            FileNotFoundError: 파일이 존재하지 않을 때
            EmptyAudioError: 파일 크기가 0일 때
        """
        if not audio_path.exists():
            raise FileNotFoundError(
                f"오디오 파일을 찾을 수 없습니다: {audio_path}"
            )

        if not audio_path.is_file():
            raise FileNotFoundError(
                f"오디오 경로가 파일이 아닙니다: {audio_path}"
            )

        if audio_path.stat().st_size == 0:
            raise EmptyAudioError(
                f"오디오 파일이 비어있습니다: {audio_path}"
            )

    @staticmethod
    def _normalize_korean_text(text: str) -> str:
        """한국어 텍스트를 유니코드 NFC로 정규화한다.

        NFC 정규화는 한글 자모를 조합형으로 통일하여
        동일한 글자가 다른 바이트로 표현되는 문제를 방지한다.
        예: ㅎ+ㅏ+ㄴ → 한 (NFD → NFC)

        Args:
            text: 정규화할 텍스트

        Returns:
            NFC 정규화된 텍스트 (앞뒤 공백 제거)
        """
        return unicodedata.normalize("NFC", text.strip())

    def _parse_segments(
        self, raw_result: dict[str, Any]
    ) -> list[TranscriptSegment]:
        """mlx_whisper의 원시 결과를 TranscriptSegment 리스트로 변환한다.

        빈 텍스트 세그먼트는 필터링하고, 한국어 텍스트는 NFC 정규화한다.

        Args:
            raw_result: mlx_whisper.transcribe()의 반환 딕셔너리

        Returns:
            파싱된 TranscriptSegment 리스트 (빈 세그먼트 제외)
        """
        segments: list[TranscriptSegment] = []

        for seg in raw_result.get("segments", []):
            text = seg.get("text", "").strip()
            if not text:
                continue

            # 한국어 텍스트 NFC 정규화
            text = self._normalize_korean_text(text)

            segments.append(
                TranscriptSegment(
                    text=text,
                    start=float(seg.get("start", 0.0)),
                    end=float(seg.get("end", 0.0)),
                    avg_logprob=float(seg.get("avg_logprob", 0.0)),
                    no_speech_prob=float(seg.get("no_speech_prob", 0.0)),
                )
            )

        return segments

    async def transcribe(self, audio_path: Path) -> TranscriptResult:
        """오디오 파일을 한국어로 전사한다.

        ModelLoadManager를 통해 whisper 모델을 로드하고,
        전사 완료 후 모델을 언로드한다. 전사 작업은 별도 스레드에서
        실행하여 이벤트 루프를 블로킹하지 않는다.

        Args:
            audio_path: 전사할 오디오 파일 경로 (16kHz mono WAV 권장)

        Returns:
            전사 결과 (TranscriptResult)

        Raises:
            FileNotFoundError: 오디오 파일이 없을 때
            EmptyAudioError: 오디오가 비어있거나 전사 결과가 없을 때
            ModelNotAvailableError: mlx-whisper를 사용할 수 없을 때
            TranscriptionError: 전사 처리 중 오류 발생 시
        """
        self._validate_audio(audio_path)

        logger.info(f"전사 시작: {audio_path.name}")

        try:
            async with self._manager.acquire(
                "whisper", self._load_whisper_module
            ) as whisper_module:
                # 전사를 별도 스레드에서 실행 (CPU/GPU 집약 작업)
                # mlx-whisper 0.4.x에서 beam_size는 직접 파라미터가 아닌
                # decode_options로 전달해야 한다 (미구현 시 무시됨)
                raw_result = await asyncio.to_thread(
                    whisper_module.transcribe,
                    str(audio_path),
                    path_or_hf_repo=self._model_name,
                    language=self._language,
                    word_timestamps=False,
                )
        except ModelNotAvailableError:
            raise
        except Exception as e:
            raise TranscriptionError(
                f"전사 처리 중 오류 발생: {audio_path} — {e}"
            ) from e

        # 결과 파싱
        segments = self._parse_segments(raw_result)

        if not segments:
            raise EmptyAudioError(
                f"전사 결과가 비어있습니다. "
                f"오디오에 음성이 없거나 인식 불가: {audio_path}"
            )

        # 전체 텍스트 NFC 정규화
        full_text = self._normalize_korean_text(
            raw_result.get("text", "")
        )

        transcript = TranscriptResult(
            segments=segments,
            full_text=full_text,
            language=raw_result.get("language", self._language),
            audio_path=str(audio_path),
        )

        logger.info(
            f"전사 완료: {audio_path.name} | "
            f"세그먼트 수: {len(segments)} | "
            f"전체 길이: {segments[-1].end:.1f}초"
        )

        return transcript
