"""
음성 구간 감지기 모듈 (Voice Activity Detection Module)

목적: Silero VAD를 사용하여 오디오 파일에서 음성 구간의 타임스탬프를 추출한다.
     무음 구간은 Whisper 처리에서 제외하여 환각(hallucination) 현상을 억제한다.

주요 기능:
    - Silero VAD v5 기반 음성 구간 감지
    - 반드시 CPU 모드 실행 (MPS 버그 방지 정책 준수)
    - ModelLoadManager 비사용 (1.8MB 소형 모델)
    - clip_timestamps 형식으로 변환하여 Transcriber에 전달

의존성: silero-vad, torch (이미 pyannote 의존성으로 설치됨)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class VADResult:
    """VAD 음성 구간 감지 결과를 담는 데이터 클래스.

    Attributes:
        speech_segments: 음성 구간 목록 [{'start': 초, 'end': 초}, ...]
        clip_timestamps: mlx-whisper clip_timestamps 형식 [s1, e1, s2, e2, ...]
        audio_path: 분석한 오디오 파일 경로 문자열
        total_speech_seconds: 총 음성 구간 길이 (초)
        total_silence_seconds: 총 무음 구간 길이 (초)
    """

    speech_segments: list[dict[str, float]]
    clip_timestamps: list[float]
    audio_path: str
    total_speech_seconds: float
    total_silence_seconds: float

    @property
    def num_segments(self) -> int:
        """감지된 음성 구간 수.

        Returns:
            음성 구간 개수
        """
        return len(self.speech_segments)


class VADError(Exception):
    """VAD 처리 중 발생하는 에러의 기본 클래스."""


class VoiceActivityDetector:
    """Silero VAD 기반 음성 구간 감지기.

    Silero VAD v5 모델을 사용하여 오디오에서 음성 구간만 감지한다.
    감지된 구간은 mlx-whisper의 clip_timestamps 파라미터로 전달하여
    무음 구간에서의 환각(hallucination)을 방지한다.

    ModelLoadManager를 사용하지 않는다 (모델 크기 1.8MB로 소형).
    반드시 CPU 모드에서만 실행한다 (MPS 버그 방지 정책 준수).

    Args:
        config: 애플리케이션 설정 인스턴스

    사용 예시:
        detector = VoiceActivityDetector(config)
        result = await detector.detect(Path("audio.wav"))
        if result is not None:
            # clip_timestamps를 Transcriber에 전달
            transcriber.transcribe(audio_path, clip_timestamps=result.clip_timestamps)
    """

    def __init__(self, config: Any) -> None:
        """VoiceActivityDetector를 초기화한다.

        config에서 VAD 설정을 읽는다. VADConfig가 아직 config에 없을 경우
        기본값으로 동작한다 (비활성 상태).

        Args:
            config: 애플리케이션 설정 인스턴스
        """
        # config에서 VAD 설정 읽기 (VADConfig가 없으면 기본 인스턴스 사용)
        vad_config = getattr(config, "vad", None)
        _has_vad_in_config = vad_config is not None

        if vad_config is None:
            from config import VADConfig
            vad_config = VADConfig()
            logger.info("VoiceActivityDetector 초기화: VAD 설정 없음, VADConfig 기본값 사용 (비활성)")

        self._enabled = vad_config.enabled
        self._threshold = vad_config.threshold
        self._min_speech_duration_ms = vad_config.min_speech_duration_ms
        self._min_silence_duration_ms = vad_config.min_silence_duration_ms
        self._speech_pad_ms = vad_config.speech_pad_ms

        if _has_vad_in_config:
            logger.info(
                f"VoiceActivityDetector 초기화: enabled={self._enabled}, "
                f"threshold={self._threshold}, "
                f"min_speech_ms={self._min_speech_duration_ms}, "
                f"min_silence_ms={self._min_silence_duration_ms}, "
                f"speech_pad_ms={self._speech_pad_ms}"
            )

        # 모델은 지연 로드 (첫 detect() 호출 시)
        self._model: Any = None
        self._utils: Any = None

    def _load_model(self) -> None:
        """Silero VAD 모델을 CPU 모드로 로드한다.

        silero_vad 패키지에서 모델과 유틸리티 함수를 로드한다.
        반드시 CPU에서만 실행하며, MPS는 사용하지 않는다.

        Raises:
            VADError: silero-vad가 설치되지 않았을 때
            VADError: torch가 설치되지 않았을 때
            VADError: 모델 로드 실패 시
        """
        # torch 임포트 확인
        try:
            import torch  # type: ignore[import-untyped]
        except ImportError as e:
            raise VADError(
                "PyTorch가 설치되어 있지 않습니다. 'pip install torch'로 설치하세요."
            ) from e

        # silero-vad 임포트 확인
        try:
            from silero_vad import get_speech_timestamps, load_silero_vad  # type: ignore[import-untyped]
        except ImportError as e:
            raise VADError(
                "silero-vad가 설치되어 있지 않습니다. "
                "'pip install silero-vad'로 설치하세요."
            ) from e

        try:
            # CPU 모드 강제 — MPS 버그 방지 정책 준수
            model = load_silero_vad()
            model.to(torch.device("cpu"))

            self._model = model
            self._utils = get_speech_timestamps

            logger.info("Silero VAD 모델 로드 완료 (CPU 모드)")
        except Exception as e:
            raise VADError(f"Silero VAD 모델 로드 실패: {e}") from e

    def _get_audio_duration(self, audio_path: Path) -> float:
        """오디오 파일의 전체 길이를 초 단위로 반환한다.

        torch.hub 유틸리티를 사용하여 오디오를 로드하고 길이를 계산한다.

        Args:
            audio_path: 오디오 파일 경로

        Returns:
            오디오 전체 길이 (초)

        Raises:
            VADError: 오디오 로드 실패 시
        """
        try:
            import torch  # type: ignore[import-untyped]
            import torchaudio  # type: ignore[import-untyped]

            waveform, sample_rate = torchaudio.load(str(audio_path))
            duration = waveform.shape[1] / sample_rate
            return float(duration)
        except Exception as e:
            raise VADError(f"오디오 길이 측정 실패: {audio_path} — {e}") from e

    def _run_vad(self, audio_path: Path) -> tuple[list[dict[str, float]], float]:
        """Silero VAD를 실행하여 음성 구간을 감지한다 (동기, to_thread에서 호출).

        get_speech_timestamps(return_seconds=True)를 사용하여
        음성 구간의 시작/종료 시간을 초 단위로 반환받는다.

        Args:
            audio_path: 분석할 오디오 파일 경로

        Returns:
            (음성 구간 목록, 오디오 전체 길이) 튜플
            음성 구간: [{'start': 초, 'end': 초}, ...]

        Raises:
            FileNotFoundError: 오디오 파일이 존재하지 않을 때
            VADError: VAD 처리 중 오류 발생 시
        """
        if not audio_path.exists():
            raise FileNotFoundError(f"오디오 파일을 찾을 수 없습니다: {audio_path}")

        if not audio_path.is_file():
            raise FileNotFoundError(f"오디오 경로가 파일이 아닙니다: {audio_path}")

        # 모델 지연 로드
        if self._model is None:
            self._load_model()

        try:
            import torch  # type: ignore[import-untyped]
            import torchaudio  # type: ignore[import-untyped]

            # 오디오 로드 (16kHz mono로 변환)
            waveform, sample_rate = torchaudio.load(str(audio_path))

            # 모노로 변환 (스테레오인 경우)
            if waveform.shape[0] > 1:
                waveform = waveform.mean(dim=0, keepdim=True)

            # 16kHz로 리샘플링 (Silero VAD 요구사항)
            if sample_rate != 16000:
                resampler = torchaudio.transforms.Resample(
                    orig_freq=sample_rate, new_freq=16000
                )
                waveform = resampler(waveform)
                sample_rate = 16000

            duration = float(waveform.shape[1] / sample_rate)

            logger.info(
                f"VAD 분석 시작: {audio_path.name} | "
                f"길이: {duration:.1f}초"
            )

            # Silero VAD 실행 — return_seconds=True로 초 단위 타임스탬프 반환
            speech_timestamps = self._utils(
                waveform.squeeze(),
                self._model,
                threshold=self._threshold,
                min_speech_duration_ms=self._min_speech_duration_ms,
                min_silence_duration_ms=self._min_silence_duration_ms,
                speech_pad_ms=self._speech_pad_ms,
                return_seconds=True,
            )

            logger.info(
                f"VAD 분석 완료: {audio_path.name} | "
                f"음성 구간 수: {len(speech_timestamps)}"
            )

            return speech_timestamps, duration

        except (FileNotFoundError, VADError):
            raise
        except Exception as e:
            raise VADError(f"VAD 처리 중 오류 발생: {audio_path} — {e}") from e

    @staticmethod
    def _to_clip_timestamps(
        segments: list[dict[str, float]],
        duration: float,
    ) -> list[float]:
        """음성 구간 목록을 mlx-whisper clip_timestamps 형식으로 변환한다.

        입력: [{'start': 1.0, 'end': 3.0}, {'start': 5.0, 'end': 8.0}]
        출력: [1.0, 3.0, 5.0, 8.0]

        **중요**: 마지막 end가 오디오 전체 길이(duration)와 거의 같으면
        -0.1초 조정한다. mlx-whisper가 마지막 타임스탬프에서 무한루프에
        빠지는 버그를 방지하기 위한 처리이다.

        Args:
            segments: 음성 구간 목록 [{'start': 초, 'end': 초}, ...]
            duration: 오디오 전체 길이 (초)

        Returns:
            clip_timestamps 형식 리스트 [s1, e1, s2, e2, ...]
        """
        if not segments:
            return []

        clip_timestamps: list[float] = []

        for seg in segments:
            start = float(seg["start"])
            end = float(seg["end"])
            # start >= end인 역전/영길이 세그먼트는 제외 (잘못된 VAD 출력 방어)
            if start >= end:
                logger.warning(
                    f"역전된 세그먼트 무시: start={start:.3f} >= end={end:.3f}"
                )
                continue
            clip_timestamps.append(start)
            clip_timestamps.append(end)

        # 마지막 end가 duration과 거의 같으면(0.15초 이내) -0.1초 조정
        # mlx-whisper 무한루프 버그 방지
        if clip_timestamps and abs(clip_timestamps[-1] - duration) < 0.15:
            adjusted = clip_timestamps[-1] - 0.1
            # 조정된 값이 마지막 start보다 작아지지 않도록 보호
            if len(clip_timestamps) >= 2 and adjusted > clip_timestamps[-2]:
                logger.debug(
                    f"clip_timestamps 마지막 end 조정: "
                    f"{clip_timestamps[-1]:.3f} → {adjusted:.3f} "
                    f"(duration={duration:.3f}, mlx-whisper 무한루프 방지)"
                )
                clip_timestamps[-1] = adjusted

        return clip_timestamps

    async def detect(self, audio_path: Path) -> VADResult | None:
        """오디오 파일에서 음성 구간을 감지한다 (비동기 진입점).

        VAD가 비활성화되어 있으면 None을 반환하여 전체 오디오를 처리하도록 한다.
        음성 구간이 0개이면 None을 반환하여 전체 오디오로 폴백한다.

        Args:
            audio_path: 분석할 오디오 파일 경로

        Returns:
            VADResult 또는 None (비활성/음성 없음 시)

        Raises:
            FileNotFoundError: 오디오 파일이 존재하지 않을 때
            VADError: VAD 처리 중 오류 발생 시
        """
        # VAD 비활성 시 None 반환 (전체 오디오 폴백)
        if not self._enabled:
            logger.debug("VAD 비활성 상태, 전체 오디오 처리로 폴백")
            return None

        # 파일 존재 확인
        if not audio_path.exists():
            raise FileNotFoundError(f"오디오 파일을 찾을 수 없습니다: {audio_path}")

        logger.info(f"VAD 감지 시작: {audio_path.name}")

        # VAD를 별도 스레드에서 실행 (CPU 집약 작업)
        try:
            speech_segments, duration = await asyncio.to_thread(
                self._run_vad, audio_path
            )
        except (FileNotFoundError, VADError):
            raise
        except Exception as e:
            raise VADError(f"VAD 감지 중 오류 발생: {audio_path} — {e}") from e

        # 음성 구간이 없으면 None 반환 (전체 오디오 폴백)
        if not speech_segments:
            logger.warning(
                f"VAD 결과 음성 구간 없음: {audio_path.name} | "
                "전체 오디오 처리로 폴백"
            )
            return None

        # clip_timestamps 변환
        clip_timestamps = self._to_clip_timestamps(speech_segments, duration)

        # 음성/무음 시간 계산
        total_speech = sum(
            float(seg["end"]) - float(seg["start"]) for seg in speech_segments
        )
        total_silence = max(0.0, duration - total_speech)

        result = VADResult(
            speech_segments=speech_segments,
            clip_timestamps=clip_timestamps,
            audio_path=str(audio_path),
            total_speech_seconds=round(total_speech, 3),
            total_silence_seconds=round(total_silence, 3),
        )

        logger.info(
            f"VAD 감지 완료: {audio_path.name} | "
            f"음성 구간: {result.num_segments}개 | "
            f"음성: {result.total_speech_seconds:.1f}초 | "
            f"무음: {result.total_silence_seconds:.1f}초"
        )

        return result
