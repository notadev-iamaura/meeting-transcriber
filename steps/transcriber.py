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
from typing import Any

from config import AppConfig, get_config
from core.model_manager import ModelLoadManager, get_model_manager
from core.preflight import run_preflight

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
        return asdict(self)  # type: ignore[return-value]


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

        Raises:
            IOError: 파일 쓰기 실패 시
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

        segments = [TranscriptSegment(**seg) for seg in data.get("segments", [])]

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
        config: AppConfig | None = None,
        model_manager: ModelLoadManager | None = None,
    ) -> None:
        """Transcriber를 초기화한다.

        Args:
            config: 애플리케이션 설정 (None이면 get_config() 사용)
            model_manager: 모델 매니저 (None이면 get_model_manager() 사용)
        """
        self._config = config or get_config()
        self._manager = model_manager or get_model_manager()

        # STT 설정 캐시
        self._model_name = self._config.stt.resolve_model_path()
        self._language = self._config.stt.language
        self._beam_size = self._config.stt.beam_size
        self._batch_size = self._config.stt.batch_size  # 향후 mlx-whisper batch 지원 대비 캐싱
        # 컨텍스트 바이어싱용 initial_prompt (None이면 미적용)
        self._initial_prompt: str | None = getattr(self._config.stt, "initial_prompt", None)
        # 이전 윈도우 텍스트 전파 제어 (False: 각 윈도우 독립 전사)
        self._condition_on_previous_text: bool = getattr(
            self._config.stt, "condition_on_previous_text", True
        )

        logger.info(
            f"Transcriber 초기화: model={self._model_name}, "
            f"language={self._language}, beam_size={self._beam_size}, "
            f"batch_size={self._batch_size}"
        )

    def _load_whisper_module(self) -> Any:
        """mlx_whisper 모듈을 임포트하여 반환한다.

        ModelLoadManager의 loader 함수로 사용된다.
        모듈 임포트 시점에 MLX 런타임이 초기화된다.

        사전 검증(preflight)에서 Metal 불가로 판정된 경우
        import를 시도하지 않아 SIGABRT를 방지한다.

        Returns:
            mlx_whisper 모듈 객체

        Raises:
            ModelNotAvailableError: mlx-whisper가 설치되지 않았거나
                Metal GPU를 사용할 수 없을 때
            ImportError: 모듈 임포트 실패 시
        """
        # SIGABRT 방지: Metal 가용성 사전 검증
        preflight = run_preflight()
        if not preflight.can_use_mlx:
            reasons = "; ".join(preflight.warnings) if preflight.warnings else "알 수 없는 원인"
            raise ModelNotAvailableError(
                f"MLX를 사용할 수 없습니다: {reasons}. "
                "Apple Silicon Mac + Python 3.11/3.12 환경이 필요합니다."
            )

        try:
            import mlx_whisper  # type: ignore[import-untyped]

            logger.info("mlx_whisper 모듈 로드 완료")
            return mlx_whisper
        except ImportError as e:
            raise ModelNotAvailableError(
                "mlx-whisper가 설치되어 있지 않습니다. 'pip install mlx-whisper'로 설치하세요."
            ) from e

    def _validate_audio(self, audio_path: Path) -> None:
        """오디오 파일의 유효성을 검증한다.

        Args:
            audio_path: 검증할 오디오 파일 경로

        Raises:
            FileNotFoundError: 파일이 존재하지 않거나 파일이 아닐 때
            EmptyAudioError: 파일 크기가 0일 때
        """
        if not audio_path.exists():
            raise FileNotFoundError(f"오디오 파일을 찾을 수 없습니다: {audio_path}")

        if not audio_path.is_file():
            raise FileNotFoundError(f"오디오 경로가 파일이 아닙니다: {audio_path}")

        if audio_path.stat().st_size == 0:
            raise EmptyAudioError(f"오디오 파일이 비어있습니다: {audio_path}")

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

        Raises:
            TypeError: 텍스트가 문자열이 아닐 때
        """
        return unicodedata.normalize("NFC", text.strip())

    def _parse_segments(self, raw_result: dict[str, Any]) -> list[TranscriptSegment]:
        """mlx_whisper의 원시 결과를 TranscriptSegment 리스트로 변환한다.

        word_timestamps=True일 때 세그먼트 내 words 배열에서
        세밀한 시작/종료 시간을 추출하여 30초 고정 윈도우 문제를 해결한다.
        빈 텍스트 세그먼트는 필터링하고, 한국어 텍스트는 NFC 정규화한다.

        Args:
            raw_result: mlx_whisper.transcribe()의 반환 딕셔너리

        Returns:
            파싱된 TranscriptSegment 리스트 (빈 세그먼트 제외)

        Raises:
            TypeError: raw_result 형식이 잘못되었을 때
        """
        segments: list[TranscriptSegment] = []

        for seg in raw_result.get("segments", []):
            text = seg.get("text", "").strip()
            if not text:
                continue

            # 한국어 텍스트 NFC 정규화
            text = self._normalize_korean_text(text)

            # word_timestamps가 있으면 세밀한 타임스탬프 사용
            words = seg.get("words")
            if words and len(words) > 0:
                start = float(words[0].get("start", seg.get("start", 0.0)))
                end = float(words[-1].get("end", seg.get("end", 0.0)))
            else:
                start = float(seg.get("start", 0.0))
                end = float(seg.get("end", 0.0))

            segments.append(
                TranscriptSegment(
                    text=text,
                    start=start,
                    end=end,
                    avg_logprob=float(seg.get("avg_logprob", 0.0)),
                    no_speech_prob=float(seg.get("no_speech_prob", 0.0)),
                )
            )

        return segments

    def _build_transcribe_kwargs(
        self,
        vad_clip_timestamps: list[float] | None = None,
    ) -> dict[str, Any]:
        """Whisper 전사 파라미터를 공통 딕셔너리로 구성한다.

        beam search와 greedy decoding 양쪽에서 동일한 파라미터를 사용하여
        경로 간 불일치를 방지한다.

        Args:
            vad_clip_timestamps: VAD가 감지한 음성 구간 경계 타임스탬프 리스트.
                [start1, end1, start2, end2, ...] 형식. None이면 전체 오디오 처리.

        Returns:
            mlx_whisper.transcribe()에 전달할 키워드 인자 딕셔너리
        """
        kwargs: dict[str, Any] = {
            "path_or_hf_repo": self._model_name,
            "language": self._language,
            "word_timestamps": True,
            "condition_on_previous_text": self._condition_on_previous_text,
        }

        # 컨텍스트 바이어싱: initial_prompt 전달 (None이면 생략)
        if self._initial_prompt is not None:
            kwargs["initial_prompt"] = self._initial_prompt
            logger.debug(f"initial_prompt 적용 (길이: {len(self._initial_prompt)}자)")

        # VAD clip_timestamps 전달 (None이면 전체 오디오 처리)
        if vad_clip_timestamps is not None:
            kwargs["clip_timestamps"] = vad_clip_timestamps
            logger.debug(f"VAD clip_timestamps 적용: {len(vad_clip_timestamps) // 2}개 구간")

        return kwargs

    async def _transcribe_with_fallback(
        self,
        whisper_module: Any,
        audio_path: Path,
        timeout: float,
        vad_clip_timestamps: list[float] | None = None,
    ) -> dict[str, Any]:
        """beam search로 전사를 시도하고, 미지원 시 greedy decoding으로 폴백한다.

        Args:
            whisper_module: mlx_whisper 모듈
            audio_path: 전사할 오디오 파일 경로
            timeout: 전사 타임아웃 (초)
            vad_clip_timestamps: VAD가 감지한 음성 구간 경계 타임스탬프 리스트.
                [start1, end1, start2, end2, ...] 형식. None이면 전체 오디오 처리.

        Returns:
            mlx_whisper.transcribe() 결과 딕셔너리
        """
        # 공통 파라미터 구성 (beam search / greedy 양쪽 동일)
        kwargs = self._build_transcribe_kwargs(vad_clip_timestamps)

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    whisper_module.transcribe,
                    str(audio_path),
                    beam_size=self._beam_size,
                    **kwargs,
                ),
                timeout=timeout,
            )
        except NotImplementedError:
            logger.warning(
                f"beam search(beam_size={self._beam_size}) 미지원 → greedy decoding으로 폴백"
            )
            return await asyncio.wait_for(
                asyncio.to_thread(
                    whisper_module.transcribe,
                    str(audio_path),
                    **kwargs,
                ),
                timeout=timeout,
            )

    async def transcribe(
        self,
        audio_path: Path,
        vad_clip_timestamps: list[float] | None = None,
    ) -> TranscriptResult:
        """오디오 파일을 한국어로 전사한다.

        ModelLoadManager를 통해 whisper 모델을 로드하고,
        전사 완료 후 모델을 언로드한다. 전사 작업은 별도 스레드에서
        실행하여 이벤트 루프를 블로킹하지 않는다.

        Args:
            audio_path: 전사할 오디오 파일 경로 (16kHz mono WAV 권장)
            vad_clip_timestamps: VAD가 감지한 음성 구간 경계 타임스탬프 리스트.
                [start1, end1, start2, end2, ...] 형식. None이면 전체 오디오 처리.

        Returns:
            전사 결과 (TranscriptResult)

        Raises:
            FileNotFoundError: 오디오 파일이 없을 때
            EmptyAudioError: 오디오가 비어있거나 전사 결과가 없을 때
            ModelNotAvailableError: mlx-whisper를 사용할 수 없을 때
            TranscriptionError: 전사 처리 중 오류 발생 시
        """
        self._validate_audio(audio_path)

        logger.info(f"전사 시작: {audio_path.name} (beam_size={self._beam_size})")

        # 전사 타임아웃: 오디오 길이에 비례하여 설정
        # 기본 30분 타임아웃 (매우 긴 오디오 고려)
        # (STAB: 전사 작업 무한 대기 방지)
        transcribe_timeout = self._config.stt.transcribe_timeout_seconds

        try:
            async with self._manager.acquire(
                "whisper", self._load_whisper_module
            ) as whisper_module:
                # 전사를 별도 스레드에서 실행 (CPU/GPU 집약 작업)
                raw_result = await self._transcribe_with_fallback(
                    whisper_module,
                    audio_path,
                    transcribe_timeout,
                    vad_clip_timestamps,
                )
        except TimeoutError as e:
            raise TranscriptionError(
                f"전사 타임아웃 ({transcribe_timeout}초 초과): {audio_path}"
            ) from e
        except ModelNotAvailableError:
            raise
        except Exception as e:
            raise TranscriptionError(f"전사 처리 중 오류 발생: {audio_path} — {e}") from e

        # 결과 파싱
        segments = self._parse_segments(raw_result)

        if not segments:
            raise EmptyAudioError(
                f"전사 결과가 비어있습니다. 오디오에 음성이 없거나 인식 불가: {audio_path}"
            )

        # 전체 텍스트 NFC 정규화
        full_text = self._normalize_korean_text(raw_result.get("text", ""))

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
