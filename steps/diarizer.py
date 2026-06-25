"""
화자분리기 모듈 (Speaker Diarization Module)

목적: pyannote-audio 3.1을 사용하여 오디오에서 화자별 발화 구간을 추출한다.
주요 기능:
    - pyannote/speaker-diarization-3.1 파이프라인 기반 화자분리
    - ModelLoadManager를 통한 모델 라이프사이클 관리 (뮤텍스)
    - pyannote MPS 버그 회피를 위해 런타임 CPU 강제
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
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import AppConfig, get_config
from core.model_manager import ModelLoadManager, get_model_manager
from core.runtime_safety import pyannote_offline_cache_issue
from steps.diarization_process_guard import ZoomPauseGuard, terminate_process

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
        return {
            "speaker": self.speaker,
            "start": self.start,
            "end": self.end,
        }


@dataclass
class DiarizationResult:
    """전체 화자분리 결과를 담는 데이터 클래스.

    Attributes:
        segments: 화자분리된 세그먼트 목록
        num_speakers: 감지된 화자 수
        audio_path: 원본 오디오 파일 경로 문자열
        model_name: 사용한 pyannote 모델명
        output_mode: 파싱한 pyannote 출력 모드
    """

    segments: list[DiarizationSegment]
    num_speakers: int
    audio_path: str
    model_name: str = ""
    output_mode: str = "regular"

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
            "model_name": self.model_name,
            "output_mode": self.output_mode,
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
            json.dump(self.to_dict(), f, ensure_ascii=False)
        logger.info(f"화자분리 체크포인트 저장: {output_path}")

    @classmethod
    def from_checkpoint(cls, checkpoint_path: Path) -> DiarizationResult:
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

        segments = [DiarizationSegment(**seg) for seg in data.get("segments", [])]

        return cls(
            segments=segments,
            num_speakers=data.get("num_speakers", 0),
            audio_path=data.get("audio_path", ""),
            model_name=data.get("model_name", ""),
            output_mode=data.get("output_mode", "regular"),
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
    pyannote MPS 버그 회피 정책에 따라 설정값과 관계없이 CPU로 실행한다.

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
        config: AppConfig | None = None,
        model_manager: ModelLoadManager | None = None,
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
        self._output_mode = getattr(self._config.diarization, "output_mode", "regular")
        self._min_speakers = self._config.diarization.min_speakers
        self._max_speakers = self._config.diarization.max_speakers
        self._hf_token = self._config.diarization.huggingface_token
        self._timeout_seconds = self._config.diarization.timeout_seconds
        self._selected_output_mode = str(self._output_mode)
        self._protect_zoom_meetings = bool(
            getattr(self._config.diarization, "protect_zoom_meetings", False)
        )
        self._zoom_protection_mode = getattr(
            self._config.diarization,
            "zoom_protection_mode",
            "off",
        )
        self._zoom_protection_poll_seconds = float(
            getattr(self._config.diarization, "zoom_protection_poll_seconds", 1.0)
        )

        logger.info(
            f"Diarizer 초기화: model={self._model_name}, "
            f"device={self._device}, "
            f"output_mode={self._output_mode}, "
            f"speakers={self._min_speakers}~{self._max_speakers}, "
            f"timeout={self._timeout_seconds}초, "
            f"zoom_protection={self._zoom_protection_mode if self._protect_zoom_meetings else 'off'}"
        )

    def _reserve_external_worker_slot(self) -> object:
        """worker 프로세스가 pyannote를 로드하는 동안 모델 슬롯을 예약한다.

        실제 pyannote 모델은 child process 안에서 로드되지만, 부모 프로세스의
        ModelLoadManager 컨텍스트를 잡아 다른 대형 모델이 동시에 로드되지 않게 한다.
        """
        return object()

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

    def _validate_offline_cache(self) -> None:
        """HF 오프라인 모드에서 pyannote 캐시가 완전한지 사전 검증한다."""
        issue = pyannote_offline_cache_issue(self._model_name)
        if issue is not None:
            raise ModelNotAvailableError(issue.message)

    def _resolve_device(self, torch_module: Any) -> str:
        """pyannote 실행 디바이스를 CPU로 강제한다.

        Args:
            torch_module: 하위 호환 시그니처 유지를 위한 torch 모듈. 사용하지 않는다.

        Returns:
            사용할 디바이스 문자열 ("cpu")
        """
        if self._device != "cpu":
            logger.warning(
                "pyannote diarization.device=%s 요청은 MPS 버그 회피 정책에 따라 CPU로 강제합니다.",
                self._device,
            )
        return "cpu"

    def _load_pipeline(self) -> Any:
        """pyannote 화자분리 파이프라인을 로드한다.

        ModelLoadManager의 loader 함수로 사용된다.
        _resolve_device()로 결정된 디바이스를 사용하며, 실패 시 CPU로 폴백한다.

        Returns:
            pyannote.audio Pipeline 인스턴스

        Raises:
            ModelNotAvailableError: pyannote-audio가 설치되지 않았을 때
            TokenNotConfiguredError: HuggingFace 토큰이 없을 때
        """
        # 토큰 검증
        token = self._validate_token()
        self._validate_offline_cache()

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
                "PyTorch가 설치되어 있지 않습니다. 'pip install torch'로 설치하세요."
            ) from e

        logger.info(f"pyannote 파이프라인 로드 시작: {self._model_name} (device={self._device})")

        try:
            # pyannote 4.x: use_auth_token → token으로 변경됨
            pipeline = Pipeline.from_pretrained(
                self._model_name,
                token=token,
            )
            if pipeline is None:
                raise ModelNotAvailableError(f"pyannote 파이프라인 로드 실패: {self._model_name}")
        except Exception as e:
            raise ModelNotAvailableError(
                f"pyannote 파이프라인 로드 실패: {self._model_name} — {e}"
            ) from e

        # 디바이스 결정 및 적용 (MPS 실패 시 CPU 폴백)
        target_device = self._resolve_device(torch)
        try:
            pipeline.to(torch.device(target_device))
            logger.info(
                f"pyannote 파이프라인 로드 완료: {self._model_name} (device={target_device})"
            )
        except (RuntimeError, ValueError) as e:
            if target_device != "cpu":
                logger.warning(f"pyannote {target_device} 로드 실패, CPU 폴백: {e}")
                pipeline.to(torch.device("cpu"))
                logger.info(f"pyannote 파이프라인 CPU 폴백 완료: {self._model_name}")
            else:
                raise
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
            raise FileNotFoundError(f"오디오 파일을 찾을 수 없습니다: {audio_path}")

        if not audio_path.is_file():
            raise FileNotFoundError(f"오디오 경로가 파일이 아닙니다: {audio_path}")

        if audio_path.stat().st_size == 0:
            raise EmptyAudioError(f"오디오 파일이 비어있습니다: {audio_path}")

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

        logger.info(f"화자분리 파이프라인 실행 완료: {elapsed:.1f}초 소요")

        return result

    def _should_use_zoom_protected_worker(self) -> bool:
        """Zoom 보호용 worker 프로세스를 사용할지 반환한다."""
        return (
            self._protect_zoom_meetings
            and self._zoom_protection_mode == "pause"
            and hasattr(signal, "SIGSTOP")
            and hasattr(signal, "SIGCONT")
        )

    def _build_worker_payload(self, audio_path: Path, output_path: Path) -> dict[str, Any]:
        """worker 프로세스에 전달할 JSON payload 를 생성한다."""
        token = self._validate_token()
        self._validate_offline_cache()
        return {
            "model_name": self._model_name,
            "audio_path": str(audio_path),
            "output_path": str(output_path),
            "min_speakers": self._min_speakers,
            "max_speakers": self._max_speakers,
            "huggingface_token": token,
            "output_mode": self._output_mode,
        }

    async def _run_zoom_protected_worker(self, audio_path: Path) -> DiarizationResult:
        """별도 worker 프로세스에서 화자분리를 실행하고 Zoom 중에는 일시정지한다."""
        self._validate_token()
        process_name = getattr(getattr(self._config, "zoom", None), "process_name", "CptHost")
        detection_backend = getattr(
            getattr(self._config, "zoom", None),
            "detection_backend",
            "coreaudio",
        )
        guard = ZoomPauseGuard(
            process_name=process_name,
            poll_interval_seconds=self._zoom_protection_poll_seconds,
            prefer_coreaudio=detection_backend == "coreaudio",
        )

        try:
            await asyncio.wait_for(guard.wait_until_idle(), timeout=self._timeout_seconds)
        except TimeoutError as e:
            raise DiarizationError(
                "Zoom 회의가 지속되어 화자분리 worker 시작 대기가 시간 초과되었습니다."
            ) from e

        async with self._manager.acquire("pyannote", self._reserve_external_worker_slot):
            return await self._run_zoom_protected_worker_with_guard(audio_path, guard)

    async def _run_zoom_protected_worker_with_guard(
        self,
        audio_path: Path,
        guard: ZoomPauseGuard,
    ) -> DiarizationResult:
        """이미 ModelLoadManager 슬롯을 확보한 상태에서 worker를 실행한다."""

        with tempfile.TemporaryDirectory(prefix="meeting-transcriber-diarize-") as tmpdir:
            output_path = Path(tmpdir) / "diarization.json"
            stderr_path = Path(tmpdir) / "worker.stderr.log"
            payload = self._build_worker_payload(audio_path, output_path)
            env = os.environ.copy()
            root = str(Path(__file__).resolve().parents[1])
            env["PYTHONPATH"] = (
                root if not env.get("PYTHONPATH") else f"{root}{os.pathsep}{env['PYTHONPATH']}"
            )

            process: subprocess.Popen[str] | None = None
            with stderr_path.open("w+", encoding="utf-8") as stderr_file:
                try:
                    process = subprocess.Popen(
                        [sys.executable, "-m", "steps.diarization_worker"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=stderr_file,
                        text=True,
                        env=env,
                    )
                    if process.stdin is None:
                        raise DiarizationError("화자분리 worker stdin 초기화 실패")
                    process.stdin.write(json.dumps(payload, ensure_ascii=False))
                    process.stdin.close()

                    logger.info(
                        f"Zoom 보호 화자분리 worker 시작: pid={process.pid}, "
                        f"audio={audio_path.name}"
                    )
                    returncode = await guard.supervise(process, self._timeout_seconds)
                    stderr_file.seek(0)
                    stderr = stderr_file.read()
                except asyncio.CancelledError:
                    if process is not None:
                        terminate_process(process)
                    raise
                except TimeoutError as e:
                    if process is not None:
                        terminate_process(process)
                    raise DiarizationError(str(e)) from e
                except DiarizationError:
                    if process is not None:
                        terminate_process(process)
                    raise
                except Exception as e:
                    if process is not None:
                        terminate_process(process)
                    raise DiarizationError("화자분리 worker 실행 중 오류가 발생했습니다.") from e

            if returncode != 0:
                detail = stderr.strip() or f"exit={returncode}"
                raise DiarizationError(f"화자분리 worker 실패: {detail}")
            if not output_path.exists():
                raise DiarizationError("화자분리 worker 결과 파일이 생성되지 않았습니다.")

            result = DiarizationResult.from_checkpoint(output_path)
            logger.info(f"Zoom 보호 화자분리 worker 완료: pid={process.pid}")
            return result

    def _select_annotation_output(self, annotation: Any) -> tuple[Any, str]:
        """설정에 따라 pyannote DiarizeOutput에서 사용할 Annotation을 선택한다."""
        explicit_output_attrs = getattr(annotation, "__dict__", {})
        looks_like_diarize_output = any(
            key in explicit_output_attrs
            for key in ("speaker_diarization", "exclusive_speaker_diarization")
        )
        if callable(getattr(annotation, "itertracks", None)) and not looks_like_diarize_output:
            return annotation, "regular"

        mode = str(self._output_mode).lower()
        if mode in {"exclusive", "auto"}:
            exclusive = getattr(annotation, "exclusive_speaker_diarization", None)
            if callable(getattr(exclusive, "itertracks", None)):
                return exclusive, "exclusive"
            if mode == "exclusive":
                logger.warning(
                    "exclusive_speaker_diarization 출력이 없어 speaker_diarization으로 폴백합니다."
                )

        regular = getattr(annotation, "speaker_diarization", None)
        return regular, "regular"

    def _parse_annotation(self, annotation: Any) -> list[DiarizationSegment]:
        """pyannote Annotation 객체를 DiarizationSegment 리스트로 변환한다.

        Args:
            annotation: pyannote Annotation 객체

        Returns:
            파싱된 DiarizationSegment 리스트 (시간순 정렬)
        """
        segments: list[DiarizationSegment] = []

        # pyannote 4.x: DiarizeOutput 객체에서 Annotation 추출
        # community-1 등은 exclusive_speaker_diarization도 제공할 수 있다.
        annotation, selected_mode = self._select_annotation_output(annotation)
        self._selected_output_mode = selected_mode
        if not callable(getattr(annotation, "itertracks", None)):
            raise DiarizationError(
                "pyannote 결과에서 itertracks 가능한 Annotation을 찾을 수 없습니다."
            )
        if selected_mode != "regular":
            logger.info("pyannote 출력 모드 적용: %s", selected_mode)

        for turn, _, speaker in annotation.itertracks(yield_label=True):
            # 유효한 구간만 포함 (duration > 0)
            if turn.end <= turn.start:
                logger.debug(
                    f"무효 세그먼트 건너뜀: speaker={speaker}, start={turn.start}, end={turn.end}"
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

        Zoom 보호가 활성화된 macOS 환경에서는 별도 worker 프로세스를 사용해
        Zoom 회의 중 worker만 일시정지한다. 보호가 꺼져 있거나 플랫폼 신호가
        없으면 기존처럼 ModelLoadManager로 pyannote 파이프라인을 로드하고,
        별도 스레드에서 실행해 이벤트 루프를 블로킹하지 않는다.

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

        if self._should_use_zoom_protected_worker():
            result = await self._run_zoom_protected_worker(audio_path)
            if not result.segments:
                raise EmptyAudioError(
                    "화자를 식별할 수 없습니다. "
                    "오디오에 명확한 음성이 포함되어 있는지 확인해주세요."
                )
            logger.info(
                f"화자분리 완료: {audio_path.name} | "
                f"화자 수: {result.num_speakers} | "
                f"세그먼트 수: {len(result.segments)} | "
                f"전체 길이: {result.total_duration:.1f}초"
            )
            return result

        try:
            async with self._manager.acquire("pyannote", self._load_pipeline) as pipeline:
                # 화자분리를 별도 스레드에서 실행 (CPU 집약 작업)
                # 타임아웃으로 무한 대기 방지 (STAB-029)
                try:
                    annotation = await asyncio.wait_for(
                        asyncio.to_thread(self._run_pipeline, pipeline, audio_path),
                        timeout=self._timeout_seconds,
                    )
                except TimeoutError as e:
                    raise DiarizationError(
                        f"화자분리 시간이 초과되었습니다 ({self._timeout_seconds}초). "
                        f"오디오 파일이 너무 길 수 있습니다. 1시간 이하 파일을 권장합니다."
                    ) from e
        except (ModelNotAvailableError, TokenNotConfiguredError):
            raise
        except DiarizationError:
            raise
        except Exception as e:
            raise DiarizationError(f"화자분리 중 오류가 발생했습니다: {e}") from e

        # 결과 파싱
        segments = self._parse_annotation(annotation)

        if not segments:
            # 오디오 길이를 확인하여 친절한 에러 메시지 제공
            try:
                import subprocess

                probe = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "quiet",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "csv=p=0",
                        str(audio_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                duration = float(probe.stdout.strip()) if probe.stdout.strip() else 0
            except Exception:
                duration = 0

            if duration < 10:
                raise EmptyAudioError(
                    f"오디오가 너무 짧습니다 ({duration:.0f}초). "
                    f"화자분리에는 최소 10초 이상의 음성이 필요합니다."
                )
            else:
                raise EmptyAudioError(
                    "화자를 식별할 수 없습니다. "
                    "오디오에 명확한 음성이 포함되어 있는지 확인해주세요."
                )

        # 화자 수 계산
        unique_speakers = set(seg.speaker for seg in segments)
        num_speakers = len(unique_speakers)

        result = DiarizationResult(
            segments=segments,
            num_speakers=num_speakers,
            audio_path=str(audio_path),
            model_name=self._model_name,
            output_mode=self._selected_output_mode,
        )

        logger.info(
            f"화자분리 완료: {audio_path.name} | "
            f"화자 수: {num_speakers} | "
            f"세그먼트 수: {len(segments)} | "
            f"전체 길이: {result.total_duration:.1f}초"
        )

        return result
