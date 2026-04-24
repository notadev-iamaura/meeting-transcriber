"""
파이프라인 매니저 모듈 (Pipeline Manager Module)

목적: asyncio 기반 파이프라인 오케스트레이터로 오디오 파일에서
     회의록 자동 생성까지의 전체 과정을 순차 실행한다.
주요 기능:
    - 6단계 순차 실행: 변환 → 전사 → 화자분리 → 병합 → 보정 → 요약
    - 단계별 JSON 체크포인트 저장으로 중간 결과 보존
    - 실패 시 마지막 성공 단계부터 재개 가능
    - 재시도 로직 (config.pipeline.retry_max_count)
    - 체크포인트 활성화/비활성화 설정 지원
의존성: config 모듈, core/model_manager 모듈, steps 모듈 전체
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import psutil

from config import AppConfig, get_config
from core.audio_quality import AudioMeasurementError, measure_audio_duration
from core.model_manager import ModelLoadManager, get_model_manager
from core.retry_policy import should_retry

logger = logging.getLogger(__name__)


# === 동적 타임아웃 계산 ===


def compute_dynamic_timeout(
    *,
    duration_seconds: float,
    multiplier: float,
    min_seconds: int,
    max_seconds: int,
) -> int:
    """오디오 길이에 비례한 전사 타임아웃을 계산한다.

    공식: clamp(duration × multiplier, min, max)

    짧은 파일은 모델 로드 시간까지 고려한 최소값으로 클램핑하고,
    지나치게 긴 파일은 폭주 방지를 위해 상한으로 클램핑한다.
    RTF 1.19(관측치) 기준 multiplier=3.0은 약 2.5배의 여유를 제공한다.

    Args:
        duration_seconds: 오디오 재생 시간 (초)
        multiplier: RTF 여유 배수 (예: 3.0)
        min_seconds: 최소 타임아웃 (짧은 파일 보호, 모델 로드 시간 포함)
        max_seconds: 최대 타임아웃 (폭주 방지 안전판)

    Returns:
        계산된 타임아웃 (정수 초). `int()` 절삭 방식.
    """
    computed = duration_seconds * multiplier
    clamped = max(float(min_seconds), min(float(max_seconds), computed))
    return int(clamped)


# === 리소스 모니터링 ===


# 리소스 경고 콜백 타입: (경고 메시지, 경고 수준)
ResourceWarningCallback = Callable[[str, str], None]

# LLM을 사용하는 단계 (메모리 부족 시 스킵 대상)
_LLM_STEPS = frozenset({"correct", "summarize"})


@dataclass
class ResourceStatus:
    """시스템 리소스 상태를 나타내는 데이터클래스.

    Attributes:
        disk_ok: 디스크 여유 공간 충분 여부
        disk_free_gb: 디스크 여유 공간 (GB)
        memory_ok: 가용 메모리 충분 여부
        memory_free_gb: 가용 메모리 (GB)
    """

    disk_ok: bool
    disk_free_gb: float
    memory_ok: bool
    memory_free_gb: float

    @property
    def all_ok(self) -> bool:
        """모든 리소스가 충분한지 반환한다.

        Returns:
            모든 리소스 충분 여부
        """
        return self.disk_ok and self.memory_ok

    @property
    def llm_available(self) -> bool:
        """LLM 실행에 필요한 메모리가 충분한지 반환한다.

        Returns:
            LLM 실행 가능 여부
        """
        return self.memory_ok


class ResourceGuard:
    """파이프라인 실행 전/중 리소스 상태를 점검하는 클래스.

    디스크 여유 공간과 가용 메모리를 확인하여
    Graceful Degradation 판단 근거를 제공한다.

    Args:
        config: 애플리케이션 설정
        on_warning: 리소스 경고 발생 시 호출할 콜백 (선택)

    사용 예시:
        guard = ResourceGuard(config)
        status = guard.check_all()
        if not status.disk_ok:
            raise PipelineError("디스크 부족")
    """

    def __init__(
        self,
        config: AppConfig,
        on_warning: ResourceWarningCallback | None = None,
    ) -> None:
        self._min_disk_gb = config.pipeline.min_disk_free_gb
        self._min_memory_gb = config.pipeline.min_memory_free_gb
        self._base_dir = config.paths.resolved_base_dir
        self._on_warning = on_warning

    def check_disk(self) -> tuple[bool, float]:
        """디스크 여유 공간을 확인한다.

        base_dir가 존재하지 않으면 존재하는 상위 디렉토리까지 탐색한다.

        Returns:
            (충분 여부, 여유 공간 GB) 튜플

        Raises:
            OSError: 디스크 정보 조회 실패 시 (내부에서 처리됨)
        """
        check_path = self._base_dir
        while not check_path.exists() and check_path.parent != check_path:
            check_path = check_path.parent
        if not check_path.exists():
            check_path = Path.home()

        try:
            usage = shutil.disk_usage(str(check_path))
            free_gb = round(usage.free / (1024**3), 2)
            ok = free_gb >= self._min_disk_gb
            return (ok, free_gb)
        except OSError as e:
            logger.warning(f"디스크 용량 확인 실패: {e}")
            # 확인 실패 시 안전하게 OK로 처리 (체크 실패로 파이프라인 중단 방지)
            return (True, 0.0)

    def check_memory(self) -> tuple[bool, float]:
        """시스템 가용 메모리를 확인한다.

        psutil.virtual_memory().available을 사용한다.

        Returns:
            (충분 여부, 가용 메모리 GB) 튜플

        Raises:
            Exception: 메모리 정보 조회 실패 시 (내부에서 처리됨)
        """
        try:
            mem = psutil.virtual_memory()
            available_gb = round(mem.available / (1024**3), 2)
            ok = available_gb >= self._min_memory_gb
            return (ok, available_gb)
        except (OSError, psutil.Error) as e:
            logger.warning(f"메모리 확인 실패: {e}")
            # 확인 실패 시 안전하게 OK로 처리
            return (True, 0.0)

    def check_all(self) -> ResourceStatus:
        """디스크와 메모리를 모두 확인한다.

        Returns:
            종합 리소스 상태 (ResourceStatus)
        """
        disk_ok, disk_free = self.check_disk()
        memory_ok, memory_free = self.check_memory()

        status = ResourceStatus(
            disk_ok=disk_ok,
            disk_free_gb=disk_free,
            memory_ok=memory_ok,
            memory_free_gb=memory_free,
        )

        # 경고 콜백 호출
        if not disk_ok:
            msg = f"디스크 여유 공간 부족: {disk_free:.1f}GB (최소 {self._min_disk_gb}GB 필요)"
            logger.warning(msg)
            if self._on_warning:
                self._on_warning(msg, "disk_low")

        if not memory_ok:
            msg = (
                f"가용 메모리 부족: {memory_free:.1f}GB "
                f"(최소 {self._min_memory_gb}GB 필요). "
                f"LLM 단계를 건너뜁니다."
            )
            logger.warning(msg)
            if self._on_warning:
                self._on_warning(msg, "memory_low")

        return status

    def is_llm_step(self, step_name: str) -> bool:
        """해당 단계가 LLM을 사용하는 단계인지 확인한다.

        Args:
            step_name: 파이프라인 단계 이름

        Returns:
            LLM 사용 단계이면 True
        """
        return step_name in _LLM_STEPS


# === 파이프라인 단계 정의 ===


class PipelineStep(StrEnum):
    """파이프라인 실행 단계를 정의하는 열거형.

    각 단계는 순서대로 실행되며, 이전 단계의 출력이
    다음 단계의 입력이 된다.
    """

    CONVERT = "convert"  # 오디오 → 16kHz WAV 변환
    TRANSCRIBE = "transcribe"  # WAV → STT 세그먼트
    DIARIZE = "diarize"  # WAV → 화자분리 세그먼트
    MERGE = "merge"  # STT + 화자분리 → 병합 발화
    CORRECT = "correct"  # 병합 발화 → LLM 보정
    SUMMARIZE = "summarize"  # 보정 발화 → 마크다운 회의록


# 실행 순서를 보장하는 단계 목록
PIPELINE_STEPS: list[PipelineStep] = [
    PipelineStep.CONVERT,
    PipelineStep.TRANSCRIBE,
    PipelineStep.DIARIZE,
    PipelineStep.MERGE,
    PipelineStep.CORRECT,
    PipelineStep.SUMMARIZE,
]


# === 데이터 클래스 ===


@dataclass
class StepResult:
    """단일 파이프라인 단계의 실행 결과.

    Attributes:
        step: 실행된 단계 이름
        success: 성공 여부
        elapsed_seconds: 소요 시간 (초)
        error_message: 실패 시 에러 메시지
        checkpoint_path: 체크포인트 파일 경로
    """

    step: str
    success: bool
    elapsed_seconds: float = 0.0
    error_message: str = ""
    checkpoint_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다."""
        return asdict(self)


@dataclass
class PipelineState:
    """파이프라인 전체 실행 상태를 추적하는 데이터 클래스.

    체크포인트로 저장/복원되어 실패 시 재개를 지원한다.

    Attributes:
        meeting_id: 회의 고유 식별자
        audio_path: 원본 오디오 파일 경로
        status: 현재 상태 (pending/running/completed/failed)
        current_step: 현재 실행 중인 단계
        completed_steps: 완료된 단계 목록
        step_results: 각 단계의 실행 결과
        created_at: 파이프라인 생성 시각 (ISO 형식)
        updated_at: 마지막 업데이트 시각 (ISO 형식)
        error_message: 실패 시 에러 메시지
        wav_path: 변환된 WAV 파일 경로 (멀티트랙 시 merged 경로)
        output_dir: 이 회의의 출력 디렉토리
        wav_paths: 멀티트랙 WAV 경로 딕셔너리 (예: {"system": "/path", "mic": "/path"})
        is_multitrack: 멀티트랙 녹음 여부
    """

    meeting_id: str
    audio_path: str
    status: str = "pending"
    current_step: str = ""
    completed_steps: list[str] = field(default_factory=list)
    step_results: list[dict[str, Any]] = field(default_factory=list)
    # 성능 예측/이상 탐지용 입력 메트릭 (진행률 바, ETA 에 사용)
    audio_duration_seconds: float = 0.0
    utterance_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    error_message: str = ""
    wav_path: str = ""
    output_dir: str = ""
    degraded: bool = False
    skipped_steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    wav_paths: dict[str, str] = field(
        default_factory=dict
    )  # {"system": "/path/system.wav", "mic": "/path/mic.wav"}
    is_multitrack: bool = False

    def __post_init__(self) -> None:
        """생성/업데이트 시각 자동 설정."""
        now = datetime.now().isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화용)."""
        return asdict(self)

    def save(self, output_path: Path) -> None:
        """파이프라인 상태를 JSON 파일로 원자적으로 저장한다.

        임시 파일에 먼저 기록한 뒤 os.replace()로 원자적 교체를 수행한다.
        프로세스 크래시 시에도 기존 체크포인트가 손상되지 않는다.

        Args:
            output_path: 저장할 JSON 파일 경로
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now().isoformat()

        # 임시 파일에 먼저 쓴 후 원자적으로 교체 (크래시 시 데이터 손상 방지)
        tmp_path = output_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            # POSIX에서 os.replace()는 원자적 연산
            os.replace(str(tmp_path), str(output_path))
        except OSError:
            # 실패 시 임시 파일 정리
            tmp_path.unlink(missing_ok=True)
            raise
        logger.debug(f"파이프라인 상태 저장 (원자적 쓰기): {output_path}")

    @classmethod
    def from_file(cls, state_path: Path) -> PipelineState:
        """JSON 파일에서 파이프라인 상태를 복원한다.

        Args:
            state_path: 상태 JSON 파일 경로

        Returns:
            복원된 PipelineState 인스턴스

        Raises:
            FileNotFoundError: 파일이 없을 때
            json.JSONDecodeError: JSON 파싱 실패 시
        """
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
        return cls(**data)


# === 에러 계층 ===


class PipelineError(Exception):
    """파이프라인 실행 중 발생하는 에러의 기본 클래스."""


class PipelineStepError(PipelineError):
    """특정 파이프라인 단계에서 실패했을 때 발생한다.

    Attributes:
        step: 실패한 단계 이름
    """

    def __init__(self, step: str, message: str) -> None:
        self.step = step
        super().__init__(f"[{step}] {message}")


class InvalidInputError(PipelineError):
    """파이프라인 입력이 유효하지 않을 때 발생한다."""


# === 메인 클래스 ===


class PipelineManager:
    """asyncio 기반 파이프라인 오케스트레이터.

    오디오 파일을 입력받아 6단계 순차 처리를 수행하고,
    각 단계 완료 시 체크포인트를 저장하여 실패 시 재개를 지원한다.

    실행 단계:
        1. convert   — 오디오를 16kHz 모노 WAV로 변환
        2. transcribe — mlx-whisper로 한국어 STT 전사
        3. diarize    — pyannote-audio로 화자분리
        4. merge      — STT 세그먼트 + 화자 세그먼트 병합
        5. correct    — EXAONE LLM으로 전사문 보정
        6. summarize  — EXAONE LLM으로 마크다운 회의록 생성

    Args:
        config: 애플리케이션 설정 (None이면 싱글턴 사용)
        model_manager: 모델 로드 매니저 (None이면 싱글턴 사용)

    사용 예시:
        pipeline = PipelineManager(config, model_manager)
        result = await pipeline.run(Path("meeting.m4a"))
        print(result.status)  # "completed"
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        model_manager: ModelLoadManager | None = None,
        on_resource_warning: ResourceWarningCallback | None = None,
    ) -> None:
        """PipelineManager를 초기화한다.

        Args:
            config: 애플리케이션 설정 (None이면 get_config() 사용)
            model_manager: 모델 매니저 (None이면 get_model_manager() 사용)
            on_resource_warning: 리소스 경고 발생 시 호출할 콜백
        """
        self._config = config or get_config()
        self._model_manager = model_manager or get_model_manager()

        # 파이프라인 설정 캐시
        self._checkpoint_enabled = self._config.pipeline.checkpoint_enabled
        self._retry_max = self._config.pipeline.retry_max_count

        # 경로 설정
        self._outputs_dir = self._config.paths.resolved_outputs_dir
        self._checkpoints_dir = self._config.paths.resolved_checkpoints_dir

        # Graceful Degradation: 리소스 가드 초기화
        self._resource_guard = ResourceGuard(
            self._config,
            on_warning=on_resource_warning,
        )
        self._on_resource_warning = on_resource_warning

        # 이슈 H: LLM 단계(correct+summarize)를 프로세스 전역으로 직렬화하는 락.
        # MLX는 같은 모델 인스턴스에 대해 복수 태스크가 동시에 generate() 호출 시
        # Metal 커맨드 버퍼가 꼬여 SIGABRT 로 죽는다. 아래 모든 경로가 이 락을 공유해
        # MLX 호출이 항상 한 번에 하나만 실행되도록 보장한다:
        #   - run_llm_steps(): 온디맨드 /summarize, /summarize-batch, 배치 백필 스크립트
        #   - run() 내부의 CORRECT/SUMMARIZE 단계: 자동 파이프라인(JobProcessor)
        # JobProcessor._run_loop 자체가 순차(single consumer)라 같은 프로세서가
        # 자기 자신과 경쟁할 일은 없지만, 자동 파이프라인 진행 중에 사용자가
        # 다른 회의 /summarize 를 호출하는 혼합 시나리오에서 락이 결정적이다.
        self._llm_lock = asyncio.Lock()

        logger.info(
            f"PipelineManager 초기화: "
            f"checkpoint={self._checkpoint_enabled}, "
            f"retry_max={self._retry_max}"
        )

    def _generate_meeting_id(self, audio_path: Path) -> str:
        """회의 고유 식별자를 생성한다.

        날짜 + 파일명 기반으로 고유 ID를 생성한다.

        Args:
            audio_path: 오디오 파일 경로

        Returns:
            회의 ID 문자열 (예: "20260304_143000_meeting")
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = audio_path.stem
        return f"{timestamp}_{stem}"

    def _get_checkpoint_path(
        self,
        meeting_id: str,
        step: PipelineStep,
    ) -> Path:
        """단계별 체크포인트 파일 경로를 반환한다.

        Args:
            meeting_id: 회의 고유 식별자
            step: 파이프라인 단계

        Returns:
            체크포인트 JSON 파일 경로
        """
        return self._checkpoints_dir / meeting_id / f"{step.value}.json"

    def _get_state_path(self, meeting_id: str) -> Path:
        """파이프라인 상태 파일 경로를 반환한다.

        Args:
            meeting_id: 회의 고유 식별자

        Returns:
            상태 JSON 파일 경로
        """
        return self._checkpoints_dir / meeting_id / "pipeline_state.json"

    def _rebuild_state_from_checkpoints(self, meeting_id: str) -> PipelineState:
        """pipeline_state.json 이 유실되었을 때 기존 체크포인트로 상태를 재구성한다.

        이슈 I 대응: 과거 파이프라인 초기 버전에서 생성된 회의 등은 merge 체크포인트는
        있지만 state 파일이 없을 수 있다. 이 경우 summarize 요청이 404 로 차단되던
        문제를 해결하기 위해, 존재하는 체크포인트를 스캔하여 completed_steps 를 복원한다.

        Args:
            meeting_id: 회의 ID

        Returns:
            재구성된 PipelineState (파일에도 저장됨)

        Raises:
            PipelineError: 회의 디렉토리조차 없어 재구성이 불가능할 때
        """
        state_path = self._get_state_path(meeting_id)
        checkpoint_dir = self._checkpoints_dir / meeting_id
        if not checkpoint_dir.exists():
            raise PipelineError(f"체크포인트 디렉토리가 없어 상태 재구성 불가: {checkpoint_dir}")

        # 기본값: audio_path 는 알 수 없으므로 빈 문자열
        state = PipelineState(
            meeting_id=meeting_id,
            audio_path="",
            output_dir=str(self._get_output_dir(meeting_id)),
            status="pending",
        )

        # 존재하는 체크포인트를 순회하며 completed_steps 복원
        for step in PipelineStep:
            cp = self._get_checkpoint_path(meeting_id, step)
            if cp.exists() and step.value not in state.completed_steps:
                state.completed_steps.append(step.value)

        # merge 체크포인트가 있으면 최소한 전사까지는 완료된 것으로 간주
        state.save(state_path)
        logger.info(
            f"상태 파일 재구성 완료: meeting_id={meeting_id}, 완료 단계={state.completed_steps}"
        )
        return state

    def _get_output_dir(self, meeting_id: str) -> Path:
        """회의별 출력 디렉토리 경로를 반환한다.

        Args:
            meeting_id: 회의 고유 식별자

        Returns:
            출력 디렉토리 경로
        """
        return self._outputs_dir / meeting_id

    def _apply_number_normalization(self, merged_result: Any) -> None:
        """병합 결과에 숫자 정규화를 적용한다 (in-place).

        config의 number_normalization 설정에 따라
        한글 숫자를 아라비아 숫자로 변환한다.
        실패 시 원본을 유지하고 파이프라인을 중단하지 않는다.

        Args:
            merged_result: 병합된 전사 결과 (utterances 속성 필요)
        """
        norm_config = getattr(self._config, "number_normalization", None)
        if norm_config is None or not norm_config.enabled:
            return

        try:
            from steps.number_normalizer import normalize_numbers

            norm_level = norm_config.level
            norm_count = 0
            for utt in merged_result.utterances:
                original = utt.text
                utt.text = normalize_numbers(utt.text, level=norm_level)
                if utt.text != original:
                    norm_count += 1
                    logger.debug(f"숫자 정규화: '{original}' → '{utt.text}'")
            if norm_count > 0:
                logger.info(f"숫자 정규화 완료: {norm_count}개 발화 변환 (level={norm_level})")
        except Exception as e:
            # 숫자 정규화 실패 시 원본 유지 (파이프라인 중단하지 않음)
            logger.warning(f"숫자 정규화 처리 실패, 원본 유지: {e}")

    def _validate_input(self, audio_path: Path) -> None:
        """입력 오디오 파일의 유효성을 검증한다.

        Args:
            audio_path: 검증할 오디오 파일 경로

        Raises:
            InvalidInputError: 파일이 없거나 유효하지 않을 때
        """
        if not audio_path.exists():
            raise InvalidInputError(f"오디오 파일을 찾을 수 없습니다: {audio_path}")

        if not audio_path.is_file():
            raise InvalidInputError(f"오디오 경로가 파일이 아닙니다: {audio_path}")

        if audio_path.stat().st_size == 0:
            raise InvalidInputError(f"오디오 파일이 비어있습니다: {audio_path}")

    def _find_resume_step(self, state: PipelineState) -> int | None:
        """재개할 단계의 인덱스를 찾는다.

        완료된 단계 다음 단계부터 재개한다.

        Args:
            state: 기존 파이프라인 상태

        Returns:
            재개할 단계 인덱스. 재개 불가 시 None.
        """
        if not state.completed_steps:
            return 0

        # 완료된 단계 중 가장 마지막 인덱스 찾기
        step_names = [s.value for s in PIPELINE_STEPS]
        max_completed_idx = -1

        for completed in state.completed_steps:
            if completed in step_names:
                idx = step_names.index(completed)
                max_completed_idx = max(max_completed_idx, idx)

        # 모든 단계가 완료된 경우
        if max_completed_idx >= len(PIPELINE_STEPS) - 1:
            return None

        return max_completed_idx + 1

    def _compute_step_input_size(
        self,
        step: PipelineStep,
        state: PipelineState,
        audio_path: Path,
        merged_result: Any,
        corrected_result: Any,
    ) -> float:
        """단계별 입력 크기를 단위에 맞춰 계산한다.

        - convert: 파일 크기 MB
        - transcribe / diarize / merge: 오디오 길이(초)
        - correct: merged_result 의 발화 수 (없으면 state.utterance_count)
        - summarize: corrected_result 의 발화 수 (없으면 state.utterance_count)

        입력이 아직 준비되지 않았으면 0.0 반환 (ETA 예측 불가).
        """
        try:
            if step == PipelineStep.CONVERT:
                if audio_path.exists():
                    return round(audio_path.stat().st_size / (1024 * 1024), 3)
                return 0.0
            if step in (
                PipelineStep.TRANSCRIBE,
                PipelineStep.DIARIZE,
                PipelineStep.MERGE,
            ):
                return float(state.audio_duration_seconds or 0.0)
            if step == PipelineStep.CORRECT:
                if merged_result is not None:
                    utterances = getattr(merged_result, "utterances", None) or []
                    return float(len(utterances))
                return float(state.utterance_count or 0)
            if step == PipelineStep.SUMMARIZE:
                if corrected_result is not None:
                    utterances = getattr(corrected_result, "utterances", None) or []
                    return float(len(utterances))
                return float(state.utterance_count or 0)
        except Exception as e:
            logger.debug(f"입력 크기 계산 실패 (step={step.value}): {e}")
        return 0.0

    async def _run_step_convert(
        self,
        audio_path: Path,
        output_dir: Path,
    ) -> Path:
        """변환 단계: 오디오를 16kHz 모노 WAV로 변환한다.

        Args:
            audio_path: 입력 오디오 파일 경로
            output_dir: 출력 디렉토리

        Returns:
            변환된 WAV 파일 경로
        """
        from steps.audio_converter import AudioConverter

        converter = AudioConverter(self._config)
        wav_path = await converter.convert_async(audio_path, output_dir)
        logger.info(f"변환 완료: {wav_path}")
        return wav_path

    async def _run_step_transcribe(
        self,
        wav_path: Path,
        checkpoint_path: Path,
    ) -> Any:
        """전사 단계: mlx-whisper로 한국어 STT를 수행한다.

        VAD가 활성화되어 있으면 전사 전에 음성 구간을 감지하여
        clip_timestamps로 전달한다. 무음 구간의 환각을 방지한다.

        Args:
            wav_path: WAV 오디오 파일 경로
            checkpoint_path: 체크포인트 저장 경로

        Returns:
            TranscriptResult 인스턴스
        """
        from steps.transcriber import Transcriber, TranscriptResult

        # 체크포인트 복원 시도
        if self._checkpoint_enabled and checkpoint_path.exists():
            logger.info(f"전사 체크포인트 복원: {checkpoint_path}")
            return TranscriptResult.from_checkpoint(checkpoint_path)

        # VAD 전처리: 음성 구간 감지 (enabled=false이면 None 반환)
        vad_clip_timestamps: list[float] | None = None
        vad_config = getattr(self._config, "vad", None)
        if vad_config is not None and vad_config.enabled:
            try:
                from steps.vad_detector import VoiceActivityDetector

                vad = VoiceActivityDetector(self._config)
                vad_result = await vad.detect(wav_path)
                if vad_result is not None:
                    vad_clip_timestamps = vad_result.clip_timestamps
                    logger.info(
                        f"VAD 적용: {vad_result.num_segments}개 음성 구간, "
                        f"무음 {vad_result.total_silence_seconds:.1f}초 제거"
                    )
            except Exception as e:
                # VAD 실패 시 전체 오디오로 폴백 (전사는 계속 진행)
                logger.warning(f"VAD 처리 실패, 전체 오디오로 폴백: {e}")
                vad_clip_timestamps = None

        transcriber = Transcriber(self._config, self._model_manager)

        # Phase 1: 동적 타임아웃 계산 — 오디오 길이에 비례
        # 짧은 파일은 1800s 고정 타임아웃보다 빠르게 실패하고,
        # 긴 파일은 충분한 여유를 확보해 불필요한 타임아웃을 방지한다.
        timeout_override: int | None = None
        if self._config.pipeline.dynamic_timeout_enabled:
            try:
                duration = measure_audio_duration(wav_path)
                timeout_override = compute_dynamic_timeout(
                    duration_seconds=duration,
                    multiplier=self._config.pipeline.dynamic_timeout_multiplier,
                    min_seconds=self._config.pipeline.dynamic_timeout_min_seconds,
                    max_seconds=self._config.pipeline.dynamic_timeout_max_seconds,
                )
                logger.info(
                    f"동적 타임아웃: {timeout_override}초 "
                    f"(duration={duration:.1f}s, "
                    f"multiplier={self._config.pipeline.dynamic_timeout_multiplier})"
                )
            except AudioMeasurementError as e:
                # duration 측정 실패 시 config 기본값으로 폴백 (전사는 계속 진행)
                logger.warning(f"duration 측정 실패, 기본 타임아웃 사용: {e}")

        result = await transcriber.transcribe(
            wav_path,
            vad_clip_timestamps=vad_clip_timestamps,
            timeout_override=timeout_override,
        )

        # 환각 필터링 (hallucination_filter 설정에 따라)
        try:
            from steps.hallucination_filter import filter_hallucinations

            filtered_segments, removed = filter_hallucinations(result.segments, self._config)
            result.segments = filtered_segments
            if removed:
                # 환각 제거 시 전체 텍스트 재구성
                result.full_text = " ".join(
                    seg.text for seg in filtered_segments if seg.text
                ).strip()
        except Exception as e:
            logger.warning(f"환각 필터링 중 오류, 원본 유지: {e}")

        # 텍스트 후처리 (text_postprocessing 설정에 따라)
        try:
            from steps.text_postprocessor import postprocess_segments

            result.segments = postprocess_segments(result.segments, self._config)
            # 전체 텍스트 재구성
            result.full_text = " ".join(seg.text for seg in result.segments if seg.text).strip()
        except Exception as e:
            logger.warning(f"텍스트 후처리 중 오류, 원본 유지: {e}")

        # 체크포인트 저장
        if self._checkpoint_enabled:
            result.save_checkpoint(checkpoint_path)

        return result

    async def _run_step_diarize(
        self,
        wav_path: Path,
        checkpoint_path: Path,
    ) -> Any:
        """화자분리 단계: pyannote-audio로 화자를 분리한다.

        Args:
            wav_path: WAV 오디오 파일 경로
            checkpoint_path: 체크포인트 저장 경로

        Returns:
            DiarizationResult 인스턴스
        """
        from steps.diarizer import DiarizationResult, Diarizer

        # 체크포인트 복원 시도
        if self._checkpoint_enabled and checkpoint_path.exists():
            logger.info(f"화자분리 체크포인트 복원: {checkpoint_path}")
            return DiarizationResult.from_checkpoint(checkpoint_path)

        diarizer = Diarizer(self._config, self._model_manager)
        result = await diarizer.diarize(wav_path)

        # 체크포인트 저장
        if self._checkpoint_enabled:
            result.save_checkpoint(checkpoint_path)

        return result

    async def _run_step_merge(
        self,
        transcript_result: Any,
        diarization_result: Any,
        checkpoint_path: Path,
    ) -> Any:
        """병합 단계: STT + 화자분리 결과를 병합한다.

        Args:
            transcript_result: 전사 결과
            diarization_result: 화자분리 결과
            checkpoint_path: 체크포인트 저장 경로

        Returns:
            MergedResult 인스턴스
        """
        from steps.merger import MergedResult, Merger

        # 체크포인트 복원 시도
        if self._checkpoint_enabled and checkpoint_path.exists():
            logger.info(f"병합 체크포인트 복원: {checkpoint_path}")
            return MergedResult.from_checkpoint(checkpoint_path)

        merger = Merger()
        result = await merger.merge(transcript_result, diarization_result)

        # 체크포인트 저장
        if self._checkpoint_enabled:
            result.save_checkpoint(checkpoint_path)

        return result

    async def _run_step_correct(
        self,
        merged_result: Any,
        checkpoint_path: Path,
    ) -> Any:
        """보정 단계: EXAONE LLM으로 전사문을 보정한다.

        Args:
            merged_result: 병합 결과
            checkpoint_path: 체크포인트 저장 경로

        Returns:
            CorrectedResult 인스턴스
        """
        from steps.corrector import CorrectedResult, Corrector

        # 체크포인트 복원 시도
        if self._checkpoint_enabled and checkpoint_path.exists():
            logger.info(f"보정 체크포인트 복원: {checkpoint_path}")
            return CorrectedResult.from_checkpoint(checkpoint_path)

        corrector = Corrector(self._config, self._model_manager)
        result = await corrector.correct(merged_result)

        # 체크포인트 저장
        if self._checkpoint_enabled:
            result.save_checkpoint(checkpoint_path)

        return result

    async def _run_step_summarize(
        self,
        corrected_result: Any,
        checkpoint_path: Path,
        output_dir: Path,
    ) -> Any:
        """요약 단계: EXAONE LLM으로 마크다운 회의록을 생성한다.

        Args:
            corrected_result: 보정 결과
            checkpoint_path: 체크포인트 저장 경로
            output_dir: 회의록 마크다운 저장 디렉토리

        Returns:
            SummaryResult 인스턴스
        """
        from steps.summarizer import Summarizer, SummaryResult

        # 체크포인트 복원 시도
        if self._checkpoint_enabled and checkpoint_path.exists():
            logger.info(f"요약 체크포인트 복원: {checkpoint_path}")
            return SummaryResult.from_checkpoint(checkpoint_path)

        summarizer = Summarizer(self._config, self._model_manager)
        result = await summarizer.summarize(corrected_result)

        # 체크포인트 저장
        if self._checkpoint_enabled:
            result.save_checkpoint(checkpoint_path)

        # 마크다운 회의록 파일 저장
        markdown_path = output_dir / "meeting_minutes.md"
        result.save_markdown(markdown_path)

        return result

    async def run(
        self,
        audio_path: Path,
        meeting_id: str | None = None,
        on_step_start: Callable[[str], Awaitable[None]] | None = None,
        on_step_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        skip_llm_steps: bool | None = None,
    ) -> PipelineState:
        """파이프라인 전체를 실행한다.

        오디오 파일을 입력받아 6단계 순차 처리를 수행한다.
        기존 체크포인트가 있으면 마지막 성공 단계부터 재개한다.

        Args:
            audio_path: 입력 오디오 파일 경로
            meeting_id: 회의 ID (None이면 자동 생성, 재개 시 기존 ID 사용)
            on_step_start: 각 단계 시작 전 호출되는 비동기 콜백 (단계명 문자열 전달)
            on_step_progress: 단계 진행/완료 이벤트 콜백. dict 인자:
                - phase: "start" | "complete"
                - step: 단계명
                - input_size: 입력 크기 (단계별 단위)
                - elapsed: (complete 시) 실제 소요 시간 초
            skip_llm_steps: LLM 단계 스킵 여부 (None이면 config 설정값 사용)

        Returns:
            최종 파이프라인 상태 (PipelineState)

        Raises:
            InvalidInputError: 입력 파일이 유효하지 않을 때
            PipelineStepError: 특정 단계 실행 실패 시 (재시도 모두 실패)
            PipelineError: 기타 파이프라인 오류 시
        """
        audio_path = audio_path.resolve()
        self._validate_input(audio_path)

        # 회의 ID 결정
        if meeting_id is None:
            meeting_id = self._generate_meeting_id(audio_path)

        # 출력/체크포인트 디렉토리 생성
        output_dir = self._get_output_dir(meeting_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        state_path = self._get_state_path(meeting_id)

        # 기존 상태 복원 또는 새로 생성
        if state_path.exists():
            state = PipelineState.from_file(state_path)
            logger.info(
                f"기존 파이프라인 상태 복원: {meeting_id} | 완료 단계: {state.completed_steps}"
            )
        else:
            state = PipelineState(
                meeting_id=meeting_id,
                audio_path=str(audio_path),
                output_dir=str(output_dir),
            )

        # === Graceful Degradation: 시작 전 리소스 점검 ===
        resource_status = self._resource_guard.check_all()

        if not resource_status.disk_ok:
            state.status = "failed"
            state.error_message = (
                f"디스크 여유 공간 부족으로 파이프라인 중단: {resource_status.disk_free_gb:.1f}GB"
            )
            state.warnings.append(state.error_message)
            state.save(state_path)
            raise PipelineError(state.error_message)

        # skip_llm_steps 결정: 명시적 파라미터 > config 설정
        _skip_llm = (
            skip_llm_steps if skip_llm_steps is not None else self._config.pipeline.skip_llm_steps
        )

        # degraded 플래그: 파이프라인 시작 시점 메모리 진단 결과를 기록.
        # 목적: UI/API 보고용 + 경고 로그. LLM 단계 스킵 결정에는 사용하지 않는다.
        # 실제 LLM 스킵 여부는 각 단계 직전 실시간 check_memory() 결과(mem_ok)로만 결정.
        # 이유: 시작 시 메모리 부족이었다가 중반에 회복되면 LLM 단계를 정상 실행해야 함.
        if not resource_status.memory_ok:
            state.degraded = True
            warn_msg = (
                f"가용 메모리 부족({resource_status.memory_free_gb:.1f}GB): "
                f"파이프라인 시작 시점 리소스 압박 감지 (degraded=True 표시, LLM 단계는 실시간 재확인 후 결정)"
            )
            state.warnings.append(warn_msg)
            logger.warning(warn_msg)

        state.status = "running"
        state.save(state_path)

        # 오디오 길이 선 획득 (ETA 예측용). 실패해도 치명적이지 않음.
        if state.audio_duration_seconds <= 0:
            try:
                from steps.audio_converter import AudioConverter

                _converter_probe = AudioConverter(self._config)
                _info = _converter_probe.probe(audio_path)
                if _info is not None and _info.duration > 0:
                    state.audio_duration_seconds = float(_info.duration)
                    state.save(state_path)
            except Exception as e:
                logger.debug(f"오디오 길이 사전 조회 실패(무시): {e}")

        logger.info(
            f"파이프라인 시작: meeting_id={meeting_id}, "
            f"audio={audio_path.name}"
            f"{', degraded=True' if state.degraded else ''}"
        )

        # 재개 시작 단계 결정
        resume_idx = self._find_resume_step(state)
        if resume_idx is None:
            logger.info("모든 단계가 이미 완료되었습니다.")
            state.status = "completed"
            state.save(state_path)
            return state

        if resume_idx > 0:
            logger.info(f"단계 {PIPELINE_STEPS[resume_idx].value}부터 재개")

        # 중간 결과 저장용
        wav_path: Path | None = None
        transcript_result: Any = None
        diarization_result: Any = None
        merged_result: Any = None
        corrected_result: Any = None
        _summary_result: Any = None

        # 이전에 완료된 단계의 결과 복원
        if resume_idx > 0:
            (
                wav_path,
                transcript_result,
                diarization_result,
                merged_result,
                corrected_result,
            ) = await self._restore_intermediate_results(
                meeting_id,
                resume_idx,
                audio_path,
                state,
            )

        # 각 단계 순차 실행
        pipeline_start = time.monotonic()

        for step_idx in range(resume_idx, len(PIPELINE_STEPS)):
            step = PIPELINE_STEPS[step_idx]
            checkpoint_path = self._get_checkpoint_path(meeting_id, step)

            # === 숫자 정규화: CORRECT 단계 진입 직전 (LLM 독립, skip_llm에서도 동작) ===
            if step == PipelineStep.CORRECT and merged_result is not None:
                self._apply_number_normalization(merged_result)

            # === Graceful Degradation / LLM 스킵: 단계별 리소스 재점검 ===
            if self._resource_guard.is_llm_step(step.value):
                # 단계 직전 실시간 메모리 재확인 — state.degraded(초기 진단값)와 무관하게 독립 판단.
                # state.degraded=True 이더라도 실시간으로 메모리가 회복되었으면 LLM 단계를 실행한다.
                # 반대로 초기에 OK였어도 실시간 mem_ok=False면 스킵한다.
                mem_ok, mem_free = self._resource_guard.check_memory()
                if _skip_llm or not mem_ok:
                    # 스킵 사유에 따른 메시지 구분
                    if _skip_llm:
                        skip_msg = f"설정에 의해 {step.value} 단계 건너뜀 (skip_llm_steps=True)"
                    else:
                        skip_msg = (
                            f"메모리 부족으로 {step.value} 단계 건너뜀 (가용: {mem_free:.1f}GB)"
                        )
                    logger.warning(skip_msg)
                    state.skipped_steps.append(step.value)
                    state.degraded = True
                    if skip_msg not in state.warnings:
                        state.warnings.append(skip_msg)

                    # correct 스킵 시 merged_result를 패스스루
                    if step == PipelineStep.CORRECT:
                        corrected_result = merged_result
                    # summarize 스킵은 회의록 없이 종료

                    step_result = StepResult(
                        step=step.value,
                        success=True,
                        elapsed_seconds=0.0,
                        error_message=f"건너뜀: {skip_msg}",
                    )
                    state.step_results.append(step_result.to_dict())
                    state.completed_steps.append(step.value)
                    state.save(state_path)
                    continue

            # 단계 시작 콜백 호출 (예외 발생 시 무시)
            if on_step_start is not None:
                try:
                    await on_step_start(step.value)
                except Exception as e:
                    logger.warning(f"on_step_start 콜백 예외 (무시): {e}")

            # 단계 시작 시점의 입력 크기 계산 (ETA 예측용)
            step_input_size = self._compute_step_input_size(
                step, state, audio_path, merged_result, corrected_result
            )

            # 단계 시작 진행 이벤트 (ETA 예측은 상위 콜백에서 수행)
            if on_step_progress is not None:
                try:
                    await on_step_progress(
                        {
                            "phase": "start",
                            "step": step.value,
                            "input_size": step_input_size,
                        }
                    )
                except Exception as e:
                    logger.warning(f"on_step_progress 콜백 예외 (start, 무시): {e}")

            state.current_step = step.value
            state.save(state_path)

            step_start = time.monotonic()
            last_error: Exception | None = None
            success = False

            # 재시도 루프
            for attempt in range(1, self._retry_max + 1):
                try:
                    logger.info(f"단계 실행: {step.value} (시도 {attempt}/{self._retry_max})")

                    if step == PipelineStep.CONVERT:
                        wav_path = await self._run_step_convert(
                            audio_path,
                            output_dir,
                        )
                        state.wav_path = str(wav_path)

                    elif step == PipelineStep.TRANSCRIBE:
                        assert wav_path is not None
                        transcript_result = await self._run_step_transcribe(
                            wav_path,
                            checkpoint_path,
                        )

                    elif step == PipelineStep.DIARIZE:
                        assert wav_path is not None
                        diarization_result = await self._run_step_diarize(
                            wav_path,
                            checkpoint_path,
                        )

                    elif step == PipelineStep.MERGE:
                        assert transcript_result is not None
                        assert diarization_result is not None
                        merged_result = await self._run_step_merge(
                            transcript_result,
                            diarization_result,
                            checkpoint_path,
                        )

                    elif step == PipelineStep.CORRECT:
                        assert merged_result is not None
                        # 이슈 H: pipeline.run() 내 LLM 단계도 run_llm_steps() 와
                        # 동일 락으로 보호해야 외부 /summarize 요청과 동시 실행 시
                        # MLX Metal 커맨드 버퍼 충돌을 막을 수 있다.
                        async with self._llm_lock:
                            corrected_result = await self._run_step_correct(
                                merged_result,
                                checkpoint_path,
                            )

                    elif step == PipelineStep.SUMMARIZE:
                        assert corrected_result is not None
                        async with self._llm_lock:
                            _summary_result = await self._run_step_summarize(
                                corrected_result,
                                checkpoint_path,
                                output_dir,
                            )

                    success = True
                    last_error = None
                    break  # 성공 시 재시도 루프 탈출

                except Exception as e:  # noqa: BLE001 — 재시도 루프 catch-all
                    last_error = e
                    logger.warning(
                        f"단계 {step.value} 실패 (시도 {attempt}/{self._retry_max}): {e}"
                    )
                    # Phase 1: NonRetryableError(타임아웃 등) 감지 시 즉시 중단
                    # (STAB: MLX Metal 상태 오염 재시도로 인한 SIGSEGV 크래시 차단)
                    if not should_retry(e, attempt=attempt, max_attempts=self._retry_max):
                        logger.info(
                            f"재시도 중단 (타입={type(e).__name__}, "
                            f"시도={attempt}/{self._retry_max})"
                        )
                        break
                    # 재시도 백오프: 1초 → 2초 → 4초 → ...
                    # (STAB: 지수 백오프로 일시적 장애 복구 확률 향상)
                    if attempt < self._retry_max:
                        backoff_seconds = min(2 ** (attempt - 1), 30)
                        logger.info(
                            f"재시도 대기: {backoff_seconds}초 (지수 백오프, 시도 {attempt})"
                        )
                        await asyncio.sleep(backoff_seconds)

            step_elapsed = time.monotonic() - step_start

            # 단계 결과 기록
            step_result = StepResult(
                step=step.value,
                success=success,
                elapsed_seconds=round(step_elapsed, 2),
                error_message=str(last_error) if last_error else "",
                checkpoint_path=str(checkpoint_path) if success else "",
            )
            state.step_results.append(step_result.to_dict())

            # MERGE 완료 시 발화 수를 상태에 저장 (이후 correct/summarize 예측용)
            if success and step == PipelineStep.MERGE and merged_result is not None:
                try:
                    state.utterance_count = len(getattr(merged_result, "utterances", []) or [])
                except Exception:
                    state.utterance_count = 0

            if success:
                state.completed_steps.append(step.value)
                state.save(state_path)
                logger.info(f"단계 완료: {step.value} ({step_elapsed:.1f}초)")

                # 단계 완료 진행 이벤트 (EMA 업데이트 + 브로드캐스트는 상위 콜백)
                if on_step_progress is not None:
                    try:
                        await on_step_progress(
                            {
                                "phase": "complete",
                                "step": step.value,
                                "input_size": step_input_size,
                                "elapsed": round(step_elapsed, 2),
                            }
                        )
                    except Exception as e:
                        logger.warning(f"on_step_progress 콜백 예외 (complete, 무시): {e}")
            else:
                # 실패 시 파이프라인 중단
                state.status = "failed"
                state.error_message = str(last_error)
                state.save(state_path)

                logger.error(
                    f"파이프라인 실패: 단계 {step.value}에서 {self._retry_max}회 재시도 모두 실패"
                )
                raise PipelineStepError(
                    step.value,
                    f"재시도 {self._retry_max}회 모두 실패: {last_error}",
                ) from last_error

        # 전체 완료
        pipeline_elapsed = time.monotonic() - pipeline_start
        state.status = "completed"
        state.current_step = ""
        state.save(state_path)

        # PERF: 파이프라인 성능 프로파일 — 각 단계별 소요 시간 요약 로그
        step_timing_parts: list[str] = []
        for sr in state.step_results:
            elapsed = sr.get("elapsed_seconds", 0.0)
            step_name = sr.get("step", "?")
            step_timing_parts.append(f"{step_name}={elapsed:.1f}s")
        timing_summary = ", ".join(step_timing_parts)

        completion_msg = (
            f"파이프라인 완료: meeting_id={meeting_id}, 총 소요 시간: {pipeline_elapsed:.1f}초"
        )
        if state.degraded:
            completion_msg += f", degraded=True, 건너뛴 단계: {state.skipped_steps}"
        logger.info(completion_msg)
        logger.info(f"단계별 소요 시간: [{timing_summary}]")

        return state

    async def _restore_intermediate_results(
        self,
        meeting_id: str,
        resume_idx: int,
        audio_path: Path,
        state: PipelineState,
    ) -> tuple[Path | None, Any, Any, Any, Any]:
        """이전에 완료된 단계의 중간 결과를 체크포인트에서 복원한다.

        재개 시 이전 단계의 출력이 필요하므로 체크포인트에서 복원한다.

        Args:
            meeting_id: 회의 ID
            resume_idx: 재개할 단계 인덱스
            audio_path: 원본 오디오 파일 경로
            state: 파이프라인 상태

        Returns:
            (wav_path, transcript_result, diarization_result,
             merged_result, corrected_result) 튜플
        """
        wav_path: Path | None = None
        transcript_result: Any = None
        diarization_result: Any = None
        merged_result: Any = None
        corrected_result: Any = None

        # convert 완료 시 wav_path 복원
        if PipelineStep.CONVERT.value in state.completed_steps:
            # wav_path가 저장되지 않았으면 원본 경로 사용
            wav_path = Path(state.wav_path) if state.wav_path else audio_path

        # transcribe 완료 시 복원
        if PipelineStep.TRANSCRIBE.value in state.completed_steps:
            cp = self._get_checkpoint_path(
                meeting_id,
                PipelineStep.TRANSCRIBE,
            )
            if cp.exists():
                from steps.transcriber import TranscriptResult

                transcript_result = TranscriptResult.from_checkpoint(cp)
                logger.info("전사 결과 체크포인트에서 복원")

        # diarize 완료 시 복원
        if PipelineStep.DIARIZE.value in state.completed_steps:
            cp = self._get_checkpoint_path(
                meeting_id,
                PipelineStep.DIARIZE,
            )
            if cp.exists():
                from steps.diarizer import DiarizationResult

                diarization_result = DiarizationResult.from_checkpoint(cp)
                logger.info("화자분리 결과 체크포인트에서 복원")

        # merge 완료 시 복원
        if PipelineStep.MERGE.value in state.completed_steps:
            cp = self._get_checkpoint_path(
                meeting_id,
                PipelineStep.MERGE,
            )
            if cp.exists():
                from steps.merger import MergedResult

                merged_result = MergedResult.from_checkpoint(cp)
                logger.info("병합 결과 체크포인트에서 복원")

        # correct 완료 시 복원
        if PipelineStep.CORRECT.value in state.completed_steps:
            cp = self._get_checkpoint_path(
                meeting_id,
                PipelineStep.CORRECT,
            )
            if cp.exists():
                from steps.corrector import CorrectedResult

                corrected_result = CorrectedResult.from_checkpoint(cp)
                logger.info("보정 결과 체크포인트에서 복원")

        return (
            wav_path,
            transcript_result,
            diarization_result,
            merged_result,
            corrected_result,
        )

    async def resume(self, meeting_id: str) -> PipelineState:
        """실패한 파이프라인을 재개한다.

        기존 체크포인트와 상태를 복원하여 마지막 성공 단계 이후부터
        다시 실행한다.

        Args:
            meeting_id: 재개할 회의 ID

        Returns:
            최종 파이프라인 상태 (PipelineState)

        Raises:
            PipelineError: 상태 파일이 없거나 재개 불가 시
        """
        state_path = self._get_state_path(meeting_id)

        if not state_path.exists():
            raise PipelineError(f"파이프라인 상태 파일을 찾을 수 없습니다: {meeting_id}")

        state = PipelineState.from_file(state_path)
        audio_path = Path(state.audio_path)

        if not audio_path.exists():
            raise InvalidInputError(f"원본 오디오 파일을 찾을 수 없습니다: {audio_path}")

        logger.info(
            f"파이프라인 재개: meeting_id={meeting_id}, 완료 단계: {state.completed_steps}"
        )

        return await self.run(audio_path, meeting_id=meeting_id)

    async def run_llm_steps(
        self,
        meeting_id: str,
        on_step_start: Callable[[str], Awaitable[None]] | None = None,
    ) -> PipelineState:
        """온디맨드 LLM 후처리: merge 체크포인트에서 결과를 로드하여 correct -> summarize를 실행한다.

        skip_llm_steps=True로 파이프라인을 실행한 뒤,
        나중에 LLM 단계만 별도로 실행하고 싶을 때 사용한다.

        이슈 H 대응: MLX Metal 커맨드 버퍼 충돌을 방지하기 위해 프로세스 전역
        _llm_lock 으로 동시 실행을 직렬화한다. 다수 요청이 동시에 도달해도
        내부에서 하나씩 순차 처리되며, 대기 중인 요청은 락을 기다린다.

        Args:
            meeting_id: 회의 ID
            on_step_start: 단계 시작 콜백

        Returns:
            업데이트된 PipelineState

        Raises:
            PipelineError: 상태 파일 또는 merge 체크포인트 미존재 시
        """
        async with self._llm_lock:
            return await self._run_llm_steps_inner(meeting_id, on_step_start)

    async def _run_llm_steps_inner(
        self,
        meeting_id: str,
        on_step_start: Callable[[str], Awaitable[None]] | None = None,
    ) -> PipelineState:
        """run_llm_steps 의 실제 본문. 호출자가 _llm_lock 을 이미 획득한 상태여야 한다."""
        # 2. merge 체크포인트 확인 및 로드 (이슈 I: state 파일보다 먼저 검사)
        merge_cp = self._get_checkpoint_path(meeting_id, PipelineStep.MERGE)
        if not merge_cp.exists():
            raise PipelineError(
                f"merge 체크포인트를 찾을 수 없습니다: {merge_cp}. 파이프라인을 먼저 실행하세요."
            )

        # 1. 상태 파일 확인 및 로드
        # 이슈 I: pipeline_state.json 이 유실되었어도 merge 체크포인트가 있으면
        # 기존 체크포인트 조합으로 state 를 재구성하여 요약을 계속 진행한다.
        state_path = self._get_state_path(meeting_id)
        if not state_path.exists():
            logger.warning(f"상태 파일 유실 — 체크포인트에서 재구성: meeting_id={meeting_id}")
            self._rebuild_state_from_checkpoints(meeting_id)

        state = PipelineState.from_file(state_path)

        from steps.merger import MergedResult

        merged_result = MergedResult.from_checkpoint(merge_cp)
        logger.info(f"merge 체크포인트 로드 완료: {merge_cp}")

        # 2.5. 숫자 정규화 (LLM 독립, correct 전에 적용)
        self._apply_number_normalization(merged_result)

        # 3. 출력 디렉토리 확인
        output_dir = self._get_output_dir(meeting_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        state.status = "running"
        state.save(state_path)

        # 4. correct 단계 실행
        correct_cp = self._get_checkpoint_path(meeting_id, PipelineStep.CORRECT)
        if correct_cp.exists():
            # 이미 correct 체크포인트가 있으면 복원
            from steps.corrector import CorrectedResult

            corrected_result = CorrectedResult.from_checkpoint(correct_cp)
            logger.info(f"correct 체크포인트 복원: {correct_cp}")
        else:
            if on_step_start is not None:
                try:
                    await on_step_start(PipelineStep.CORRECT.value)
                except Exception as e:
                    logger.warning(f"on_step_start 콜백 예외 (무시): {e}")

            state.current_step = PipelineStep.CORRECT.value
            state.save(state_path)

            corrected_result = await self._run_step_correct(
                merged_result,
                correct_cp,
            )

        # 5. summarize 단계 실행
        summarize_cp = self._get_checkpoint_path(meeting_id, PipelineStep.SUMMARIZE)
        if not summarize_cp.exists():
            if on_step_start is not None:
                try:
                    await on_step_start(PipelineStep.SUMMARIZE.value)
                except Exception as e:
                    logger.warning(f"on_step_start 콜백 예외 (무시): {e}")

            state.current_step = PipelineStep.SUMMARIZE.value
            state.save(state_path)

            await self._run_step_summarize(
                corrected_result,
                summarize_cp,
                output_dir,
            )
        else:
            logger.info(f"summarize 체크포인트 복원: {summarize_cp}")

        # 6. 상태 업데이트: skipped_steps에서 제거, completed_steps에 추가
        for step_name in ("correct", "summarize"):
            if step_name in state.skipped_steps:
                state.skipped_steps.remove(step_name)
            if step_name not in state.completed_steps:
                state.completed_steps.append(step_name)

        state.status = "completed"
        state.current_step = ""
        state.save(state_path)

        logger.info(f"온디맨드 LLM 단계 완료: meeting_id={meeting_id}")
        return state

    def get_status(self, meeting_id: str) -> PipelineState | None:
        """특정 회의의 파이프라인 상태를 조회한다.

        Args:
            meeting_id: 회의 ID

        Returns:
            PipelineState 인스턴스. 상태 파일이 없으면 None.
        """
        state_path = self._get_state_path(meeting_id)
        if not state_path.exists():
            return None
        try:
            return PipelineState.from_file(state_path)
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning(f"상태 파일 파싱 실패: {state_path} — {e}")
            return None
