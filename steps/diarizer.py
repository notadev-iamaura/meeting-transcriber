"""
화자분리기 모듈 (Speaker Diarization Module)

목적: pyannote-audio 3.1을 사용하여 오디오에서 화자별 발화 구간을 추출한다.
주요 기능:
    - pyannote/speaker-diarization-3.1 파이프라인 기반 화자분리
    - ModelLoadManager를 통한 모델 라이프사이클 관리 (뮤텍스)
    - 반드시 device='cpu' 강제 (MPS 버그 방지)
    - 화자별 시간 구간(speaker, start, end) 반환
    - HuggingFace 토큰 검증
    - 비동기(async) 인터페이스 지원
    - JSON 체크포인트 저장/복원 지원
의존성: pyannote-audio, torch, config 모듈, core/model_manager 모듈
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from config import AppConfig, get_config
from core.model_manager import ModelLoadManager, get_model_manager

logger = logging.getLogger(__name__)


@dataclass
class DiarizationSegment:
    """화자분리 결과의 단일 세그먼트를 나타내는 데이터 클래스.

    Attributes:
        speaker: 화자 라벨 (예: "SPEAKER_00", "SPEAKER_01")
        start: 발화 시작 시간 (초)
        end: 발화 종료 시간 (초)
    """

    speaker: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        """발화 구간의 길이 (초)
        
        Returns:
            발화 길이 (초)
        """
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화용).

        Returns:
            세그먼트 데이터 딕셔너리
        """
        return asdict(self)  # type: ignore[return-value]


@dataclass
class DiarizationResult:
    """전체 화자분리 결과를 담는 데이터 클래스.

    Attributes:
        segments: 화자분리된 세그먼트 목록
        num_speakers: 감지된 화자 수
        audio_path: 원본 오디오 파일 경로 문자열
    """

    segments: list[DiarizationSegment]
    num_speakers: int
    audio_path: str

    @property
    def total_duration(self) -> float:
        """전체 오디오 길이 추정치 (마지막 세그먼트 종료 시간).
        
        Returns:
            오디오 전체 길이 (초)
        """
        if not self.segments:
            return 0.0
        return max(seg.end for seg in self.segments)

    @property
    def speakers(self) -> list[str]:
        """감지된 화자 라벨 목록 (중복 제거, 정렬).
        
        Returns:
            화자 라벨 목록
        """
        return sorted(set(seg.speaker for seg in self.segments))

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화/체크포인트 저장용).

        Returns:
            전체 화자분리 결과 딕셔너리
        """
        return {
            "segments": [seg.to_dict() for seg in self.segments],
            "num_speakers": self.num_speakers,
            "audio_path": self.audio_path,
        }

    def save_checkpoint(self, output_path: Path) -> None:
        """화자분리 결과를 JSON 파일로 저장한다 (체크포인트).

        Args:
            output_path: 저장할 JSON 파일 경로
            
        Raises:
            IOError: 파일 쓰기 실패 시
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info(f"화자분리 체크포인트 저장: {output_path}")

    @classmethod
    def from_checkpoint(cls, checkpoint_path: Path) -> "DiarizationResult":
        """체크포인트 JSON 파일에서 화자분리 결과를 복원한다.

        Args:
            checkpoint_path: 체크포인트 JSON 파일 경로

        Returns:
            복원된 DiarizationResult 인스턴스

        Raises:
            FileNotFoundError: 체크포인트 파일이 없을 때
            json.JSONDecodeError: JSON 파싱 실패 시
        """
        with open(checkpoint_path, encoding="utf-8") as f:
            data = json.load(f)

        segments = [
            DiarizationSegment(**seg) for seg in data.get("segments", [])
        ]

        return cls(
            segments=segments,
            num_speakers=data.get("num_speakers", 0),
            audio_path=data.get("audio_path", ""),
        )


class DiarizationError(Exception):
    """화자분리 처리 중 발생하는 에러의 기본 클래스."""


class ModelNotAvailableError(DiarizationError):
    """pyannote 모델을 로드할 수 없을 때 발생한다."""


class EmptyAudioError(DiarizationError):
    """오디오 파일이 비어있거나 화자분리 결과가 없을 때 발생한다."""


class TokenNotConfiguredError(DiarizationError):
    """HuggingFace 토큰이 설정되지 않았을 때 발생한다."""


class Diarizer:
    """pyannote-audio 기반 화자분리기.

    ModelLoadManager를 통해 pyannote 파이프라인의 메모리 라이프사이클을 관리하고,
    config.yaml의 diarization 설정에 따라 화자분리를 수행한다.
    반드시 device='cpu'로 실행한다 (MPS 버그 방지).

    Args:
        config: 애플리케이션 설정 인스턴스 (None이면 싱글턴 사용)
        model_manager: 모델 로드 매니저 (None이면 싱글턴 사용)

    사용 예시:
        diarizer = Diarizer(config, model_manager)
        result = await diarizer.diarize(Path("audio.wav"))
        for seg in result.segments:
            print(f"{seg.speaker}: {seg.start:.1f}s ~ {seg.end:.1f}s")
    """

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        model_manager: Optional[ModelLoadManager] = None,
    ) -> None:
        """Diarizer를 초기화한다.

        Args:
            config: 애플리케이션 설정 (None이면 get_config() 사용)
            model_manager: 모델 매니저 (None이면 get_model_manager() 사용)
        """
        self._config = config or get_config()
        self._manager = model_manager or get_model_manager()

        # 화자분리 설정 캐시
        self._model_name = self._config.diarization.model_name
        self._device = self._config.diarization.device
        self._min_speakers = self._config.diarization.min_speakers
        self._max_speakers = self._config.diarization.max_speakers
        self._hf_token = self._config.diarization.huggingface_token
        self._timeout_seconds = self._config.diarization.timeout_seconds

        logger.info(
            f"Diarizer 초기화: model={self._model_name}, "
            f"device={self._device}, "
            f"speakers={self._min_speakers}~{self._max_speakers}, "
            f"timeout={self._timeout_seconds}초"
        )

    def _validate_token(self) -> str:
        """HuggingFace 토큰이 설정되어 있는지 검증한다.

        Returns:
            검증된 HuggingFace 토큰 문자열

        Raises:
            TokenNotConfiguredError: 토큰이 없을 때
        """
        if not self._hf_token:
            raise TokenNotConfiguredError(
                "HuggingFace 토큰이 설정되지 않았습니다. "
                "환경변수 HUGGINGFACE_TOKEN을 설정하거나 "
                "config.yaml의 diarization.huggingface_token을 설정하세요. "
                "pyannote 모델 접근에 HuggingFace 토큰이 필요합니다."
            )
        return self._hf_token

    def _load_pipeline(self) -> Any:
        """pyannote 화자분리 파이프라인을 로드한다.

        ModelLoadManager의 loader 함수로 사용된다.
        반드시 device='cpu'로 로드한다 (MPS 버그 방지).

        Returns:
            pyannote.audio Pipeline 인스턴스

        Raises:
            ModelNotAvailableError: pyannote-audio가 설치되지 않았을 때
            TokenNotConfiguredError: HuggingFace 토큰이 없을 때
        """
        # 토큰 검증
        token = self._validate_token()

        try:
            from pyannote.audio import Pipeline  # type: ignore[import-untyped]
        except ImportError as e:
            raise ModelNotAvailableError(
                "pyannote-audio가 설치되어 있지 않습니다. "
                "'pip install pyannote-audio'로 설치하세요."
            ) from e

        try:
            import torch  # type: ignore[import-untyped]
        except ImportError as e:
            raise ModelNotAvailableError(
                "PyTorch가 설치되어 있지 않습니다. "
                "'pip install torch'로 설치하세요."
            ) from e

        logger.info(
            f"pyannote 파이프라인 로드 시작: {self._model_name} "
            f"(device={self._device})"
        )

        try:
            # pyannote 4.x: use_auth_token → token으로 변경됨
            pipeline = Pipeline.from_pretrained(
                self._model_name,
                token=token,
            )
        except Exception as e:
            raise ModelNotAvailableError(
                f"pyannote 파이프라인 로드 실패: {self._model_name} — {e}"
            ) from e

        # CPU 강제 (MPS 버그 방지)
        pipeline.to(torch.device("cpu"))

        logger.info(
            f"pyannote 파이프라인 로드 완료: {self._model_name} "
            f"(device=cpu)"
        )
        return pipeline

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

    def _run_pipeline(self, pipeline: Any, audio_path: Path) -> Any:
        """pyannote 파이프라인을 실행한다 (동기, 스레드에서 호출).

        PERF: 화자분리 진행 상태를 중간 로깅하여 장시간 작업의 진행 상황을 파악한다.

        Args:
            pipeline: pyannote Pipeline 인스턴스
            audio_path: 오디오 파일 경로

        Returns:
            pyannote Annotation 객체
        """
        import time as _time

        params: dict[str, Any] = {}
        if self._min_speakers is not None:
            params["min_speakers"] = self._min_speakers
        if self._max_speakers is not None:
            params["max_speakers"] = self._max_speakers

        # 파일 크기로 예상 소요 시간 안내
        file_size_mb = audio_path.stat().st_size / (1024 * 1024)
        logger.info(
            f"화자분리 실행: {audio_path.name} | "
            f"파일 크기: {file_size_mb:.1f}MB | "
            f"speakers={self._min_speakers}~{self._max_speakers} | "
            f"device={self._device}"
        )

        start_time = _time.monotonic()
        result = pipeline(str(audio_path), **params)
        elapsed = _time.monotonic() - start_time

        logger.info(
            f"화자분리 파이프라인 실행 완료: {elapsed:.1f}초 소요"
        )

        return result

    def _parse_annotation(
        self, annotation: Any
    ) -> list[DiarizationSegment]:
        """pyannote Annotation 객체를 DiarizationSegment 리스트로 변환한다.

        Args:
            annotation: pyannote Annotation 객체

        Returns:
            파싱된 DiarizationSegment 리스트 (시간순 정렬)
        """
        segments: list[DiarizationSegment] = []

        # pyannote 4.x: DiarizeOutput 객체에서 Annotation 추출
        # (DiarizeOutput은 itertracks 메서드가 없으므로 타입으로 구분)
        if not callable(getattr(annotation, "itertracks", None)):
            annotation = annotation.speaker_diarization

        for turn, _, speaker in annotation.itertracks(yield_label=True):
            # 유효한 구간만 포함 (duration > 0)
            if turn.end <= turn.start:
                logger.debug(
                    f"무효 세그먼트 건너뜀: speaker={speaker}, "
                    f"start={turn.start}, end={turn.end}"
                )
                continue

            segments.append(
                DiarizationSegment(
                    speaker=str(speaker),
                    start=round(turn.start, 3),
                    end=round(turn.end, 3),
                )
            )

        # 시간순 정렬
        segments.sort(key=lambda s: s.start)

        return segments

    async def diarize(self, audio_path: Path) -> DiarizationResult:
        """오디오 파일에서 화자분리를 수행한다.

        ModelLoadManager를 통해 pyannote 파이프라인을 로드하고,
        화자분리 완료 후 모델을 언로드한다. 화자분리 작업은 별도 스레드에서
        실행하여 이벤트 루프를 블로킹하지 않는다.

        Args:
            audio_path: 화자분리할 오디오 파일 경로 (16kHz mono WAV 권장)

        Returns:
            화자분리 결과 (DiarizationResult)

        Raises:
            FileNotFoundError: 오디오 파일이 없을 때
            EmptyAudioError: 오디오가 비어있거나 화자분리 결과가 없을 때
            ModelNotAvailableError: pyannote-audio를 사용할 수 없을 때
            TokenNotConfiguredError: HuggingFace 토큰이 없을 때
            DiarizationError: 화자분리 처리 중 오류 또는 타임아웃 발생 시
        """
        self._validate_audio(audio_path)

        logger.info(f"화자분리 시작: {audio_path.name}")

        try:
            async with self._manager.acquire(
                "pyannote", self._load_pipeline
            ) as pipeline:
                # 화자분리를 별도 스레드에서 실행 (CPU 집약 작업)
                # 타임아웃으로 무한 대기 방지 (STAB-029)
                try:
                    annotation = await asyncio.wait_for(
                        asyncio.to_thread(
                            self._run_pipeline, pipeline, audio_path
                        ),
                        timeout=self._timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    raise DiarizationError(
                        f"화자분리 타임아웃: {self._timeout_seconds}초 초과. "
                        f"오디오 파일이 너무 크거나 모델이 응답하지 않습니다."
                    )
        except (ModelNotAvailableError, TokenNotConfiguredError):
            raise
        except DiarizationError:
            raise
        except Exception as e:
            raise DiarizationError(
                f"화자분리 처리 중 오류 발생: {audio_path} — {e}"
            ) from e

        # 결과 파싱
        segments = self._parse_annotation(annotation)

        if not segments:
            raise EmptyAudioError(
                f"화자분리 결과가 비어있습니다. "
                f"오디오에 음성이 없거나 인식 불가: {audio_path}"
            )

        # 화자 수 계산
        unique_speakers = set(seg.speaker for seg in segments)
        num_speakers = len(unique_speakers)

        result = DiarizationResult(
            segments=segments,
            num_speakers=num_speakers,
            audio_path=str(audio_path),
        )

        logger.info(
            f"화자분리 완료: {audio_path.name} | "
            f"화자 수: {num_speakers} | "
            f"세그먼트 수: {len(segments)} | "
            f"전체 길이: {result.total_duration:.1f}초"
        )

        return result
