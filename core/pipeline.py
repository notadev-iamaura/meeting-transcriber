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
import errno
import json
import logging
import os
import shutil
import stat
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import psutil

from config import AppConfig, get_config
from core.audio_quality import (
    AudioFailureKind,
    AudioMeasurementError,
    AudioQualityStatus,
    measure_audio_duration,
    validate_audio_quality,
)
from core.io_utils import (
    atomic_write_json,
    atomic_write_text,
    atomic_write_text_pinned,
    ensure_directory_no_follow,
    publish_text_no_replace,
    read_text_no_follow,
)
from core.meeting_mutation import MeetingMutationCoordinator
from core.model_manager import ModelLoadManager, get_model_manager
from core.retry_policy import should_retry
from steps.transcriber import (
    AudioAdmissionError,
    AudioFileIdentity,
    EmptyAudioError,
    inspect_audio_path_no_symlinks,
    open_audio_path_no_symlinks,
)

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
        # LLM 단계 사전 경고용 권장 메모리 (skip 임계치보다 큰 값)
        self._llm_recommended_gb = getattr(config.pipeline, "llm_recommended_memory_gb", 6.5)
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

    def check_llm_capacity(self) -> tuple[bool, float, str | None]:
        """LLM 단계 진입 전 가용 메모리를 점검하고 사전 경고를 결정한다.

        세 가지 결과:
            - 충분: 가용 ≥ llm_recommended_memory_gb → (True, free_gb, None)
            - 빠듯: min_memory < 가용 < llm_recommended → (True, free_gb, 경고메시지)
              (실행은 진행, 사용자에게 알림만)
            - 부족: 가용 ≤ min_memory_free_gb → (False, free_gb, 차단메시지)
              (다음 단계의 mem_ok 체크가 실제 skip 결정을 내림)

        Returns:
            (실행 가능 여부, 가용 메모리 GB, 경고 메시지) 튜플.
            메시지는 None 이면 경고 없음.
        """
        ok, free_gb = self.check_memory()
        if not ok:
            # 하드 차단은 기존 check_memory 가 처리 — 여기는 보고만
            msg = f"LLM 단계 메모리 부족: 가용 {free_gb:.1f}GB < 필수 {self._min_memory_gb:.1f}GB"
            if self._on_warning:
                self._on_warning(msg, "llm_memory_blocked")
            return (False, free_gb, msg)

        if free_gb < self._llm_recommended_gb:
            # 빠듯: 진행은 하되 사용자에게 사전 알림
            msg = (
                f"LLM 단계 가용 메모리 부족 위험: 가용 {free_gb:.1f}GB < "
                f"권장 {self._llm_recommended_gb:.1f}GB. "
                "다른 무거운 앱(브라우저·IDE 등)을 종료하면 안정성이 향상됩니다."
            )
            logger.warning(msg)
            if self._on_warning:
                self._on_warning(msg, "llm_memory_low_warning")
            return (True, free_gb, msg)

        return (True, free_gb, None)


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
    CHUNK = "chunk"  # 보정 발화 → RAG 청크 (검색 인덱스용)
    EMBED = "embed"  # RAG 청크 → ChromaDB + SQLite FTS5 (검색 인덱스 영구화)
    # Phase 1 (LLM Wiki) — non-fatal 9단계. PIPELINE_STEPS 메인 루프에는 포함되지
    # 않으며 run() 끝에서 별도로 호출된다 (실패해도 RAG 결과는 정상 반환).
    WIKI_COMPILE = "wiki_compile"  # 요약/발화 → 영구 wiki 페이지 (Phase 1 dry-run)


# 실행 순서를 보장하는 단계 목록
#
# 순서 정책:
#   ... → CORRECT → SUMMARIZE → CHUNK → EMBED
#
# CHUNK / EMBED 가 SUMMARIZE 이후에 위치하는 이유:
#   1) 회의록(SUMMARIZE) 은 핵심 출력물 — 검색 인덱싱 실패가 회의록 생성을 차단하면 안 된다
#   2) chunk/embed 단계가 실패해도 사용자는 회의록·전사문은 받을 수 있다
#   3) 백필 API 는 correct.json 체크포인트만 있으면 chunk/embed 만 재실행해 인덱스 복구 가능
PIPELINE_STEPS: list[PipelineStep] = [
    PipelineStep.CONVERT,
    PipelineStep.TRANSCRIBE,
    PipelineStep.DIARIZE,
    PipelineStep.MERGE,
    PipelineStep.CORRECT,
    PipelineStep.SUMMARIZE,
    PipelineStep.CHUNK,
    PipelineStep.EMBED,
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
    stt_provider: str = ""
    stt_model: str = ""

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

    def save(self, output_path: Path, *, indent: int | None = 2) -> None:
        """파이프라인 상태를 JSON 파일로 원자적으로 저장한다.

        no-follow로 고정한 부모 디렉터리 FD 안에서 최초 상태를 직접 만들거나 기존
        상태와 새 상태를 원자 교환한다. lexical parent/source-name 경쟁이 발생해도
        외부 경로 또는 이전 상태 entry를 제거하지 않는다.

        Args:
            output_path: 저장할 JSON 파일 경로
            indent: JSON 들여쓰기. None 이면 compact JSON 으로 저장한다.
        """
        self.updated_at = datetime.now().isoformat()
        dump_kwargs: dict[str, Any] = {"ensure_ascii": False, "indent": indent}
        if indent is None:
            dump_kwargs["separators"] = (",", ":")

        # 부모 디렉터리 생성부터 no-follow FD chain을 사용하고 같은 FD 안에서
        # 상태를 교환한다. 검사 뒤 외부 symlink로 바뀐 lexical 경로는 따르지 않는다.
        atomic_write_text_pinned(
            output_path,
            json.dumps(self.to_dict(), **dump_kwargs),
        )
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
        data = json.loads(read_text_no_follow(state_path))
        return cls(**data)


# === 에러 계층 ===


class PipelineError(Exception):
    """파이프라인 실행 중 발생하는 에러의 기본 클래스."""


class PipelineStateWriteError(PipelineError):
    """파이프라인 상태 저장의 경로·원자성 검증이 실패했음을 나타낸다."""


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

    def __init__(
        self,
        message: str,
        *,
        failure_kind: AudioFailureKind | None = None,
    ) -> None:
        self.failure_kind = failure_kind
        super().__init__(message)


def _normalize_checkpoint_json_indent(value: object) -> int | None:
    """체크포인트 JSON 들여쓰기 설정을 안전하게 정규화한다."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 2
    if isinstance(value, int) and value >= 0:
        return value
    return 2


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
        meeting_mutation_coordinator: MeetingMutationCoordinator | None = None,
    ) -> None:
        """PipelineManager를 초기화한다.

        Args:
            config: 애플리케이션 설정 (None이면 get_config() 사용)
            model_manager: 모델 매니저 (None이면 get_model_manager() 사용)
            on_resource_warning: 리소스 경고 발생 시 호출할 콜백
            meeting_mutation_coordinator: API·scheduler와 공유할 회의별 mutation coordinator
        """
        self._config = config or get_config()
        self._model_manager = model_manager or get_model_manager()
        self._meeting_mutation_coordinator = (
            meeting_mutation_coordinator or MeetingMutationCoordinator()
        )

        # 파이프라인 설정 캐시
        self._checkpoint_enabled = self._config.pipeline.checkpoint_enabled
        self._checkpoint_json_indent = _normalize_checkpoint_json_indent(
            getattr(self._config.pipeline, "checkpoint_json_indent", 2),
        )
        self._retry_max = self._config.pipeline.retry_max_count

        # 경로 설정
        self._outputs_dir = self._configured_storage_root(
            "outputs_dir",
            self._config.paths.resolved_outputs_dir,
        )
        self._checkpoints_dir = self._configured_storage_root(
            "checkpoints_dir",
            self._config.paths.resolved_checkpoints_dir,
        )

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

    def update_stt_config(self, stt_config: Any) -> None:
        """다음 전사부터 사용할 STT 설정을 원자적인 모델 복사로 교체한다."""
        self._config = self._config.model_copy(update={"stt": stt_config})

    @property
    def meeting_mutation_coordinator(self) -> MeetingMutationCoordinator:
        """API·scheduler가 공유하는 회의별 mutation coordinator를 반환한다."""
        return self._meeting_mutation_coordinator

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

    def _configured_storage_root(self, field_name: str, resolved_fallback: Path) -> Path:
        """resolve()가 숨길 수 있는 base symlink를 보존한 lexical root를 만든다."""
        paths = self._config.paths
        raw_base = getattr(paths, "base_dir", None)
        raw_child = getattr(paths, field_name, None)
        if isinstance(raw_base, (str, Path)) and isinstance(raw_child, (str, Path)):
            lexical_base = Path(raw_base).expanduser().absolute()
            child = Path(raw_child).expanduser()
            if child == Path(".") or ".." in child.parts or "\x00" in str(child):
                raise InvalidInputError(
                    f"{field_name}은 base_dir 하위 상대경로여야 합니다: {raw_child!r}",
                    failure_kind=AudioFailureKind.SECURITY_BLOCKED,
                )
            candidate = (
                child.absolute() if child.is_absolute() else (lexical_base / child).absolute()
            )
            try:
                relative = candidate.relative_to(lexical_base)
            except ValueError as exc:
                raise InvalidInputError(
                    f"{field_name}이 base_dir 밖을 가리킵니다: {candidate}",
                    failure_kind=AudioFailureKind.SECURITY_BLOCKED,
                ) from exc
            if not relative.parts:
                raise InvalidInputError(
                    f"{field_name}은 base_dir의 직접/하위 경로여야 합니다",
                    failure_kind=AudioFailureKind.SECURITY_BLOCKED,
                )
            return candidate
        return Path(resolved_fallback).expanduser().absolute()

    @staticmethod
    def _validate_meeting_id(meeting_id: str) -> None:
        """회의 ID가 경로 요소 하나로만 구성됐는지 검증한다."""
        if (
            not isinstance(meeting_id, str)
            or not meeting_id
            or meeting_id in {".", ".."}
            or "\x00" in meeting_id
            or "\\" in meeting_id
            or Path(meeting_id).name != meeting_id
        ):
            raise InvalidInputError(f"유효하지 않은 회의 ID입니다: {meeting_id!r}")

    @staticmethod
    def _validate_storage_directory(path: Path, *, label: str) -> Path:
        """openat dirfd chain으로 저장 디렉터리의 기존 요소를 검증한다."""
        lexical_path = path.expanduser().absolute()
        if ".." in lexical_path.parts:
            raise InvalidInputError(
                f"{label} 경로에 상위 디렉터리 요소가 포함되어 있습니다: {lexical_path}",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )

        parts = lexical_path.parts[1:] if lexical_path.is_absolute() else lexical_path.parts
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        current = Path(lexical_path.anchor)
        current_fd: int | None = None
        try:
            try:
                current_fd = os.open(lexical_path.anchor, flags)
            except OSError as exc:
                raise InvalidInputError(
                    f"{label} root를 안전하게 열 수 없습니다: {lexical_path.anchor} ({exc})",
                    failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
                ) from exc

            for component in parts:
                current /= component
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    # admission 뒤 생성할 경로는 missing을 허용한다.
                    break
                except OSError as exc:
                    try:
                        entry_stat = os.stat(
                            component,
                            dir_fd=current_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        break
                    except OSError as stat_exc:
                        raise InvalidInputError(
                            f"{label} 경로 상태를 확인할 수 없습니다: {current} ({stat_exc})",
                            failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
                        ) from stat_exc
                    if stat.S_ISLNK(entry_stat.st_mode):
                        raise InvalidInputError(
                            f"{label} 경로에 심볼릭 링크가 포함되어 있습니다: {current}",
                            failure_kind=AudioFailureKind.SECURITY_BLOCKED,
                        ) from exc
                    if not stat.S_ISDIR(entry_stat.st_mode):
                        raise InvalidInputError(
                            f"{label} 경로 요소가 디렉터리가 아닙니다: {current}",
                            failure_kind=AudioFailureKind.SECURITY_BLOCKED,
                        ) from exc
                    kind = (
                        AudioFailureKind.SECURITY_BLOCKED
                        if exc.errno in {errno.ELOOP, errno.ENOTDIR}
                        else AudioFailureKind.INFRA_UNAVAILABLE
                    )
                    raise InvalidInputError(
                        f"{label} 경로를 안전하게 열 수 없습니다: {current} ({exc})",
                        failure_kind=kind,
                    ) from exc
                os.close(current_fd)
                current_fd = next_fd
        finally:
            if current_fd is not None:
                os.close(current_fd)
        return lexical_path

    def _get_storage_child(self, root: Path, meeting_id: str, *, label: str) -> Path:
        """설정 root와 회의별 direct child를 no-follow로 검증한다."""
        self._validate_meeting_id(meeting_id)
        lexical_root = self._validate_storage_directory(root, label=f"{label} root")
        child = lexical_root / meeting_id
        if child.parent != lexical_root:
            raise InvalidInputError(
                f"{label} 경로가 설정 root 밖을 가리킵니다: {child}",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )
        self._validate_storage_directory(child, label=f"{label} meeting")
        return child

    @staticmethod
    def _validate_storage_artifact(path: Path, *, label: str) -> Path:
        """openat dirfd chain으로 기존 artifact를 no-follow 검사한다."""
        lexical_path = path.expanduser().absolute()
        parts = lexical_path.parts[1:] if lexical_path.is_absolute() else lexical_path.parts
        if not lexical_path.is_absolute() or not parts or ".." in parts:
            raise InvalidInputError(
                f"유효하지 않은 {label} 경로입니다: {lexical_path}",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        file_flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= os.O_CLOEXEC
            file_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NONBLOCK"):
            file_flags |= os.O_NONBLOCK

        current_fd: int | None = None
        artifact_fd: int | None = None
        try:
            current_fd = os.open(lexical_path.anchor, directory_flags)
            for component in parts[:-1]:
                try:
                    next_fd = os.open(component, directory_flags, dir_fd=current_fd)
                except FileNotFoundError:
                    return lexical_path
                except OSError as exc:
                    try:
                        entry_stat = os.stat(
                            component,
                            dir_fd=current_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        return lexical_path
                    if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
                        raise InvalidInputError(
                            f"{label} 상위 경로가 안전한 디렉터리가 아닙니다: {component}",
                            failure_kind=AudioFailureKind.SECURITY_BLOCKED,
                        ) from exc
                    raise InvalidInputError(
                        f"{label} 상위 경로를 열 수 없습니다: {component} ({exc})",
                        failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
                    ) from exc
                os.close(current_fd)
                current_fd = next_fd
            try:
                artifact_fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
            except FileNotFoundError:
                return lexical_path
            except OSError as exc:
                try:
                    entry_stat = os.stat(
                        parts[-1],
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return lexical_path
                if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISREG(entry_stat.st_mode):
                    raise InvalidInputError(
                        f"{label} 파일에 심볼릭 링크/비정규 파일을 사용할 수 없습니다: "
                        f"{lexical_path}",
                        failure_kind=AudioFailureKind.SECURITY_BLOCKED,
                    ) from exc
                raise InvalidInputError(
                    f"{label} 파일을 열 수 없습니다: {lexical_path} ({exc})",
                    failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
                ) from exc
            entry_stat = os.fstat(artifact_fd)
            if not stat.S_ISREG(entry_stat.st_mode):
                raise InvalidInputError(
                    f"{label} 경로가 일반 파일이 아닙니다: {lexical_path}",
                    failure_kind=AudioFailureKind.SECURITY_BLOCKED,
                )
        except InvalidInputError:
            raise
        except OSError as exc:
            raise InvalidInputError(
                f"{label} 경로를 확인할 수 없습니다: {lexical_path} ({exc})",
                failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
            ) from exc
        finally:
            if artifact_fd is not None:
                os.close(artifact_fd)
            if current_fd is not None:
                os.close(current_fd)
        return lexical_path

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
        checkpoint_dir = self._get_storage_child(
            self._checkpoints_dir,
            meeting_id,
            label="체크포인트",
        )
        return self._validate_storage_artifact(
            checkpoint_dir / f"{step.value}.json",
            label=f"{step.value} 체크포인트",
        )

    def _get_state_path(self, meeting_id: str) -> Path:
        """파이프라인 상태 파일 경로를 반환한다.

        Args:
            meeting_id: 회의 고유 식별자

        Returns:
            상태 JSON 파일 경로
        """
        checkpoint_dir = self._get_storage_child(
            self._checkpoints_dir,
            meeting_id,
            label="체크포인트",
        )
        return self._validate_storage_artifact(
            checkpoint_dir / "pipeline_state.json",
            label="파이프라인 상태",
        )

    def _get_llm_deferred_marker_path(self, meeting_id: str) -> Path:
        """correct 의존 산출물의 지연 생성을 나타내는 영속 marker 경로를 반환한다."""
        checkpoint_dir = self._get_storage_child(
            self._checkpoints_dir,
            meeting_id,
            label="체크포인트",
        )
        return self._validate_storage_artifact(
            checkpoint_dir / "llm_deferred.json",
            label="지연 LLM marker",
        )

    @staticmethod
    def _llm_deferred_marker_payload(meeting_id: str) -> dict[str, Any]:
        """회의에 결합된 지연 LLM marker의 고정 payload를 반환한다."""
        return {
            "schema_version": 1,
            "meeting_id": meeting_id,
            "deferred_steps": [
                PipelineStep.CORRECT.value,
                PipelineStep.SUMMARIZE.value,
                PipelineStep.CHUNK.value,
                PipelineStep.EMBED.value,
            ],
        }

    def _validate_llm_deferred_marker(self, marker_path: Path, meeting_id: str) -> None:
        """marker가 해당 회의의 정확한 지연 의도인지 fail-closed로 검증한다."""
        try:
            payload = json.loads(read_text_no_follow(marker_path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PipelineError(
                "SECURITY_BLOCKED: 지연 LLM marker를 안전하게 검증할 수 없습니다"
            ) from exc
        if payload != self._llm_deferred_marker_payload(meeting_id):
            raise PipelineError(
                "SECURITY_BLOCKED: 지연 LLM marker가 현재 회의의 고정 계약과 다릅니다"
            )

    def _ensure_llm_deferred_marker(self, meeting_id: str) -> None:
        """state 유실 뒤에도 지연 LLM/index 의도를 알릴 no-replace marker를 만든다."""
        marker_path = self._get_llm_deferred_marker_path(meeting_id)
        try:
            publish_text_no_replace(
                marker_path,
                json.dumps(
                    self._llm_deferred_marker_payload(meeting_id),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        except FileExistsError:
            # 기존 entry를 교체하지 않고 정확한 고정 marker인지 읽기만 한다.
            self._validate_llm_deferred_marker(marker_path, meeting_id)

    def _llm_deferred_marker_exists(self, meeting_id: str) -> bool:
        """현재 회의에 결합된 지연 LLM marker가 존재하는지 반환한다."""
        marker_path = self._get_llm_deferred_marker_path(meeting_id)
        try:
            self._validate_llm_deferred_marker(marker_path, meeting_id)
        except PipelineError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                return False
            raise
        except FileNotFoundError:
            return False
        return True

    def _llm_deferred_pending(self, meeting_id: str) -> bool:
        """marker 기반 지연 의도가 남아 있는지 반환한다.

        writable namespace의 기존 산출물은 provenance로 신뢰하지 않는다. state가
        유실된 경우 marker가 있으면 skipped 의도를 복원하고, 이미 생긴 산출물은
        이후 conflict 검사에서 모두 보존한 채 차단한다.
        """
        return self._llm_deferred_marker_exists(meeting_id)

    def _llm_refresh_artifact_paths(self, meeting_id: str) -> tuple[Path, ...]:
        """지연 LLM 실행이 새로 게시할 모든 canonical 산출물 경로를 반환한다."""
        output_dir = self._get_output_dir(meeting_id)
        return (
            self._get_checkpoint_path(meeting_id, PipelineStep.CORRECT),
            self._get_checkpoint_path(meeting_id, PipelineStep.SUMMARIZE),
            self._get_checkpoint_path(meeting_id, PipelineStep.CHUNK),
            self._get_checkpoint_path(meeting_id, PipelineStep.EMBED),
            self._validate_storage_artifact(
                output_dir / "summary.md",
                label="지연 요약 출력",
            ),
            self._validate_storage_artifact(
                output_dir / "meeting_minutes.md",
                label="지연 요약 출력",
            ),
        )

    def _save_state(self, state: PipelineState, state_path: Path) -> None:
        """파이프라인 상태를 설정된 JSON 형식으로 저장한다."""
        expected_path = self._get_state_path(state.meeting_id)
        if state_path.expanduser().absolute() != expected_path:
            raise InvalidInputError(
                f"상태 파일이 설정된 체크포인트 경로 밖을 가리킵니다: {state_path}",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )
        if self._checkpoint_json_indent == 2:
            try:
                state.save(expected_path)
            except OSError as exc:
                raise PipelineStateWriteError(
                    f"파이프라인 상태를 안전하게 저장할 수 없습니다: {expected_path}"
                ) from exc
            return
        try:
            state.save(expected_path, indent=self._checkpoint_json_indent)
        except OSError as exc:
            raise PipelineStateWriteError(
                f"파이프라인 상태를 안전하게 저장할 수 없습니다: {expected_path}"
            ) from exc

    def _save_result_checkpoint(
        self,
        result: Any,
        checkpoint_path: Path,
        *,
        exclusive_publish: bool = False,
    ) -> None:
        """결과 checkpoint를 검증한 저장소에 저장한다.

        ``exclusive_publish``는 지연 LLM처럼 기존 산출물을 절대 교체하면 안 되는
        경로에서 사용한다. 이 경우 최종 이름을 직접 no-replace로 게시하며 경쟁
        entry 또는 실패한 부분 산출물이 있으면 그대로 보존하고 실패한다.
        """
        safe_path = self._validate_storage_artifact(
            checkpoint_path,
            label="저장 대상 체크포인트",
        )
        to_dict = getattr(result, "to_dict", None)
        payload = to_dict() if callable(to_dict) else None
        if isinstance(payload, dict):
            if self._checkpoint_json_indent is None:
                content = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if exclusive_publish:
                    publish_text_no_replace(safe_path, content)
                else:
                    atomic_write_text(safe_path, content, backup=False)
                return
            if exclusive_publish:
                publish_text_no_replace(
                    safe_path,
                    json.dumps(payload, ensure_ascii=False, indent=self._checkpoint_json_indent),
                )
                return
            atomic_write_json(
                safe_path,
                payload,
                backup=False,
                indent=self._checkpoint_json_indent,
            )
            return

        # 테스트 double/legacy 결과 타입 호환. production 결과는 모두 to_dict()를 제공한다.
        if exclusive_publish:
            raise PipelineError("exclusive checkpoint 결과를 JSON으로 직렬화할 수 없습니다")
        result.save_checkpoint(safe_path)

    async def _acquire_llm_lock_with_timeout(self) -> None:
        """_llm_lock 을 타임아웃과 함께 획득한다.

        선행 LLM 작업이 비정상적으로 장기화되거나 데드락이 발생해도
        무한 대기하지 않도록 config.pipeline.llm_lock_acquire_timeout_seconds
        내에서 획득을 시도한다. 실패 시 PipelineError.

        Raises:
            PipelineError: 타임아웃 내 획득 실패 시
        """
        timeout = self._config.pipeline.llm_lock_acquire_timeout_seconds
        try:
            await asyncio.wait_for(self._llm_lock.acquire(), timeout=timeout)
        except TimeoutError as e:
            raise PipelineError(
                f"LLM 락 획득 타임아웃 ({timeout}s). "
                "선행 LLM 작업이 비정상 장기화되었을 수 있습니다."
            ) from e

    async def _run_llm_step_with_timeout(
        self,
        step_name: str,
        coro_factory: Callable[[], Awaitable[Any]],
        timeout_seconds: int,
    ) -> Any:
        """LLM 단계를 _llm_lock + 타임아웃 조합으로 실행한다.

        1. _acquire_llm_lock_with_timeout() 로 락 획득 (하드 타임아웃)
        2. 락 보유 상태에서 coro_factory() 호출 → asyncio.wait_for 로 감싸
           단계 자체의 무한 대기(모델 환각 폭주·MLX hang)도 차단
        3. 타임아웃 시 PipelineError 발생, finally 블록에서 락 해제

        Args:
            step_name: 로깅용 단계명 (correct/summarize)
            coro_factory: 실제 LLM 작업을 만드는 함수 (인자 없음)
            timeout_seconds: 단계 실행 하드 타임아웃

        Returns:
            coro_factory() 의 결과

        Raises:
            PipelineError: 락 획득 실패 또는 단계 타임아웃 시
        """
        await self._acquire_llm_lock_with_timeout()
        try:
            try:
                return await asyncio.wait_for(coro_factory(), timeout=timeout_seconds)
            except TimeoutError as e:
                raise PipelineError(
                    f"{step_name} 단계 타임아웃 ({timeout_seconds}s). "
                    "LLM 모델이 비정상적으로 오래 실행되었습니다."
                ) from e
        finally:
            self._llm_lock.release()

    async def _unload_llm_model_if_current(self) -> None:
        """현재 로드 모델이 LLM이면 조건부로 언로드한다."""
        unload_if_current = getattr(self._model_manager, "unload_if_current", None)
        if not callable(unload_if_current):
            return

        result = unload_if_current("exaone")
        if asyncio.iscoroutine(result):
            await result

    def _transcript_checkpoint_selection(self, restored: Any) -> tuple[str, str]:
        """전사 체크포인트의 provider/model provenance를 안전하게 정규화한다."""
        provider = str(getattr(restored, "provider", "local") or "local")
        model = str(getattr(restored, "model", "") or "")
        if provider not in {"local", "openai"}:
            raise InvalidInputError(
                "전사 체크포인트의 provider가 허용 목록과 다릅니다.",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )
        if provider == "openai":
            allowed_model = getattr(
                self._config.stt,
                "openai_model",
                "gpt-4o-transcribe-diarize",
            )
            if model != allowed_model:
                raise InvalidInputError(
                    "전사 체크포인트의 OpenAI 모델이 허용 목록과 다릅니다.",
                    failure_kind=AudioFailureKind.SECURITY_BLOCKED,
                )
            return provider, model
        return provider, model or "legacy-local"

    def _validate_transcript_checkpoint_selection(
        self,
        restored: Any,
        *,
        selected_provider: str,
        selected_model: str,
    ) -> None:
        """복원할 전사 결과가 파이프라인에 고정된 선택과 같은지 확인한다."""
        restored_provider, restored_model = self._transcript_checkpoint_selection(restored)
        mismatch = restored_provider != selected_provider
        if not mismatch and selected_provider == "openai":
            mismatch = restored_model != selected_model
        elif not mismatch and selected_model != "legacy-local":
            expected_models = {selected_model}
            try:
                selected_config = self._config.stt.model_copy(
                    update={"provider": "local", "model_name": selected_model}
                )
                resolved_model = selected_config.resolve_model_path(
                    base_dir=self._config.paths.resolved_base_dir
                )
                if isinstance(resolved_model, str) and resolved_model:
                    expected_models.add(resolved_model)
            except (AttributeError, OSError, TypeError, ValueError):
                # provenance 비교는 모델 다운로드/로드를 유발하지 않는다. 설정 double이나
                # 사라진 로컬 캐시는 요청 당시의 canonical model ID로만 비교한다.
                pass
            mismatch = restored_model not in expected_models
        if mismatch:
            raise InvalidInputError(
                "전사 체크포인트의 provider/model이 고정된 파이프라인 선택과 다릅니다.",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )

    def _read_transcript_checkpoint(self, meeting_id: str) -> Any | None:
        """존재하는 전사 체크포인트를 고정 저장소 경로에서 읽는다."""
        checkpoint_path = self._get_checkpoint_path(meeting_id, PipelineStep.TRANSCRIBE)
        if not checkpoint_path.exists():
            return None
        from steps.transcriber import TranscriptResult

        return TranscriptResult.from_checkpoint(checkpoint_path)

    def _rebuild_state_from_checkpoints(
        self,
        meeting_id: str,
        *,
        trust_llm_checkpoints: bool = True,
        persist: bool = True,
    ) -> PipelineState:
        """pipeline_state.json 이 유실되었을 때 기존 체크포인트로 상태를 재구성한다.

        이슈 I 대응: 과거 파이프라인 초기 버전에서 생성된 회의 등은 merge 체크포인트는
        있지만 state 파일이 없을 수 있다. 이 경우 summarize 요청이 404 로 차단되던
        문제를 해결하기 위해, 존재하는 체크포인트를 스캔하여 completed_steps 를 복원한다.

        Args:
            meeting_id: 회의 ID
            trust_llm_checkpoints: false이면 state 유실 지연 실행에서 기존
                correct/summarize/chunk/embed를 완료 근거로 사용하지 않음
            persist: false이면 복구 상태를 메모리에서만 구성하고 파일을
                생성하지 않음

        Returns:
            재구성된 PipelineState. ``persist=True``이면 파일에도 저장됨.

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
            if not trust_llm_checkpoints and step in {
                PipelineStep.CORRECT,
                PipelineStep.SUMMARIZE,
                PipelineStep.CHUNK,
                PipelineStep.EMBED,
            }:
                continue
            cp = self._get_checkpoint_path(meeting_id, step)
            if cp.exists() and step.value not in state.completed_steps:
                state.completed_steps.append(step.value)

        # skip 의도는 pipeline_state.json과 독립된 no-replace marker로 복구한다.
        # 기존 부분 산출물은 provenance로 재사용하지 않고 conflict 검사에서 보존·차단한다.
        if self._llm_deferred_pending(meeting_id):
            for step in (
                PipelineStep.CORRECT,
                PipelineStep.SUMMARIZE,
                PipelineStep.CHUNK,
                PipelineStep.EMBED,
            ):
                if step.value not in state.completed_steps:
                    state.completed_steps.append(step.value)
                if step.value not in state.skipped_steps:
                    state.skipped_steps.append(step.value)

        if PipelineStep.TRANSCRIBE.value in state.completed_steps:
            restored = self._read_transcript_checkpoint(meeting_id)
            if restored is not None:
                state.stt_provider, state.stt_model = self._transcript_checkpoint_selection(
                    restored
                )

        # 지연 실행은 충돌 검사가 끝나기 전에 state를 생성하지 않도록
        # persist=False로 구성한 뒤 실행 직전에 처음 저장한다.
        if persist:
            self._save_state(state, state_path)
            logger.info(
                f"상태 파일 재구성 완료: meeting_id={meeting_id}, "
                f"완료 단계={state.completed_steps}"
            )
        return state

    def _get_output_dir(self, meeting_id: str) -> Path:
        """회의별 출력 디렉토리 경로를 반환한다.

        Args:
            meeting_id: 회의 고유 식별자

        Returns:
            출력 디렉토리 경로
        """
        return self._get_storage_child(self._outputs_dir, meeting_id, label="출력")

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

    def _build_passthrough_corrected_result(self, merged_result: Any) -> Any:
        """LLM 보정 스킵 시 MergedResult를 CorrectedResult로 변환한다."""
        from steps.corrector import CorrectedResult, CorrectedUtterance

        utterances = [
            CorrectedUtterance(
                text=str(utterance.text),
                original_text=str(utterance.text),
                speaker=str(utterance.speaker),
                start=float(utterance.start),
                end=float(utterance.end),
                was_corrected=False,
            )
            for utterance in getattr(merged_result, "utterances", [])
        ]
        audio_path = getattr(merged_result, "audio_path", "")
        if not isinstance(audio_path, str):
            audio_path = ""
        num_speakers = getattr(merged_result, "num_speakers", 0)
        if not isinstance(num_speakers, int):
            num_speakers = 0

        return CorrectedResult(
            utterances=utterances,
            num_speakers=num_speakers,
            audio_path=audio_path,
            total_corrected=0,
            total_failed=0,
        )

    def _validate_input(self, audio_path: Path) -> AudioFileIdentity:
        """입력 오디오 파일의 유효성을 검증한다.

        Args:
            audio_path: 검증할 오디오 파일 경로

        Raises:
            InvalidInputError: 파일이 없거나 유효하지 않을 때
        """
        try:
            return inspect_audio_path_no_symlinks(audio_path)
        except EmptyAudioError as exc:
            raise InvalidInputError(str(exc), failure_kind=AudioFailureKind.MEDIA_INVALID) from exc
        except AudioAdmissionError as exc:
            raise InvalidInputError(
                str(exc),
                failure_kind=exc.failure_kind,
            ) from exc
        except FileNotFoundError as exc:
            raise InvalidInputError(str(exc)) from exc

    def _assert_input_identity(
        self,
        audio_path: Path,
        expected_identity: AudioFileIdentity,
    ) -> None:
        """입력 파일이 최초 no-follow 검사와 같은 inode/metadata인지 확인한다."""
        try:
            current_identity = inspect_audio_path_no_symlinks(audio_path)
        except AudioAdmissionError as exc:
            raise InvalidInputError(str(exc), failure_kind=exc.failure_kind) from exc
        except (EmptyAudioError, FileNotFoundError) as exc:
            raise InvalidInputError(
                f"오디오 파일이 품질 검증 중 사라지거나 변경되었습니다: {audio_path}",
                failure_kind=AudioFailureKind.SOURCE_BUSY,
            ) from exc

        if current_identity != expected_identity:
            raise InvalidInputError(
                f"오디오 파일이 품질 검증 중 변경되었습니다: {audio_path}",
                failure_kind=AudioFailureKind.SOURCE_BUSY,
            )

    async def _validate_audio_duration(
        self,
        audio_path: Path,
        expected_identity: AudioFileIdentity,
    ) -> None:
        """공통 오디오 품질 gate에서 ACCEPT인 입력만 통과시킨다."""
        self._assert_input_identity(audio_path, expected_identity)
        enabled = getattr(self._config.audio_quality, "enabled", False)
        if enabled is not True:
            return

        try:
            result = await asyncio.to_thread(
                validate_audio_quality,
                audio_path,
                min_mean_db=self._config.audio_quality.min_mean_volume_db,
                min_duration_s=self._config.audio_quality.min_duration_seconds,
                expected_identity=expected_identity,
                decode_timeout_base_seconds=(
                    self._config.audio_quality.decode_timeout_base_seconds
                ),
                decode_timeout_factor=self._config.audio_quality.decode_timeout_factor,
                decode_timeout_cap_seconds=(self._config.audio_quality.decode_timeout_cap_seconds),
            )
        except Exception as exc:
            try:
                self._assert_input_identity(audio_path, expected_identity)
            except InvalidInputError as identity_exc:
                raise identity_exc from exc
            raise InvalidInputError(
                f"오디오 품질을 검증할 수 없어 전사를 시작하지 않습니다: {audio_path} ({exc})",
                failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
            ) from exc

        self._assert_input_identity(audio_path, expected_identity)

        if result.status is AudioQualityStatus.ACCEPT:
            return

        reason = result.reason or "오디오 품질 검증 비수락"
        failure_kind = result.failure_kind or AudioFailureKind.INFRA_UNAVAILABLE
        duration_seconds = result.duration_seconds
        min_duration = self._config.audio_quality.min_duration_seconds
        if duration_seconds is not None and duration_seconds < min_duration:
            raise InvalidInputError(
                f"오디오가 너무 짧아 전사를 시작하지 않습니다: {reason}",
                failure_kind=failure_kind,
            )
        raise InvalidInputError(
            f"오디오 품질 검증을 통과하지 못해 전사를 시작하지 않습니다: {reason}",
            failure_kind=failure_kind,
        )

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
        wav_path = self._localize_converted_wav(audio_path, output_dir, wav_path)
        logger.info(f"변환 완료: {wav_path}")
        return wav_path

    def _validate_pipeline_wav_path(
        self,
        output_dir: Path,
        wav_path: Path,
    ) -> Path:
        """변환 WAV가 해당 회의 output의 안전한 direct regular child인지 검증한다."""
        safe_output_dir = self._validate_storage_directory(
            output_dir,
            label="pipeline WAV output",
        )
        candidate = wav_path.expanduser().absolute()
        if candidate.parent != safe_output_dir or candidate.suffix.lower() != ".wav":
            raise InvalidInputError(
                f"변환 WAV가 회의 output direct child가 아닙니다: {candidate}",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )
        self._validate_storage_artifact(candidate, label="pipeline WAV")
        self._validate_input(candidate)
        return candidate

    def _localize_converted_wav(
        self,
        audio_path: Path,
        output_dir: Path,
        converter_path: Path,
    ) -> Path:
        """converter의 WAV를 회의 output 아래로 고정하고 외부 반환값은 거부한다."""
        safe_output_dir = self._validate_storage_directory(
            output_dir,
            label="pipeline WAV output",
        )
        source = converter_path.expanduser().absolute()
        if source.parent == safe_output_dir:
            return self._validate_pipeline_wav_path(safe_output_dir, source)

        original = audio_path.expanduser().absolute()
        if source != original or source.suffix.lower() != ".wav":
            raise InvalidInputError(
                f"converter가 회의 output 밖의 WAV를 반환했습니다: {source}",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )

        # AudioConverter는 이미 16k mono PCM인 원본 WAV를 그대로 반환한다.
        # 재개 상태가 외부 audio_input을 신뢰하지 않도록 안전한 fd에서 회의 output으로
        # 복제하고, 이후 단계에는 이 로컬 snapshot만 전달한다.
        destination = safe_output_dir / f"{source.stem}_16k.wav"
        self._validate_storage_artifact(destination, label="pipeline localized WAV")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= os.O_CLOEXEC
        output_fd: int | None = None
        temporary_fd: int | None = None
        temporary_name = f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            output_fd = os.open(safe_output_dir, directory_flags)
            temporary_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                temporary_flags |= os.O_CLOEXEC
            temporary_fd = os.open(
                temporary_name,
                temporary_flags,
                0o600,
                dir_fd=output_fd,
            )
            with open_audio_path_no_symlinks(source) as (source_fd, source_identity):
                with (
                    os.fdopen(os.dup(source_fd), "rb") as source_file,
                    os.fdopen(temporary_fd, "wb", closefd=False) as destination_file,
                ):
                    shutil.copyfileobj(source_file, destination_file)
                    destination_file.flush()
                    os.fsync(destination_file.fileno())
                after_stat = os.fstat(source_fd)
                after_identity: AudioFileIdentity = (
                    after_stat.st_dev,
                    after_stat.st_ino,
                    after_stat.st_size,
                    after_stat.st_mtime_ns,
                    after_stat.st_ctime_ns,
                )
                if after_identity != source_identity:
                    raise InvalidInputError(
                        f"원본 WAV가 output snapshot 생성 중 변경되었습니다: {source}",
                        failure_kind=AudioFailureKind.SOURCE_BUSY,
                    )
            os.close(temporary_fd)
            temporary_fd = None
            os.replace(
                temporary_name,
                destination.name,
                src_dir_fd=output_fd,
                dst_dir_fd=output_fd,
            )
            os.fsync(output_fd)
        except InvalidInputError:
            raise
        except (AudioAdmissionError, EmptyAudioError, FileNotFoundError) as exc:
            failure_kind = getattr(exc, "failure_kind", AudioFailureKind.SOURCE_BUSY)
            raise InvalidInputError(str(exc), failure_kind=failure_kind) from exc
        except OSError as exc:
            raise InvalidInputError(
                f"변환 WAV를 회의 output으로 고정하지 못했습니다: {exc}",
                failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
            ) from exc
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            if output_fd is not None:
                try:
                    os.unlink(temporary_name, dir_fd=output_fd)
                except FileNotFoundError:
                    pass
                finally:
                    os.close(output_fd)

        return self._validate_pipeline_wav_path(safe_output_dir, destination)

    async def _run_step_transcribe(
        self,
        wav_path: Path,
        checkpoint_path: Path,
        *,
        stt_provider: str | None = None,
        stt_model: str | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Any:
        """전사 단계: 선택된 로컬 또는 OpenAI STT를 수행한다.

        VAD가 활성화되어 있으면 전사 전에 음성 구간을 감지하여
        clip_timestamps로 전달한다. 무음 구간의 환각을 방지한다.

        Args:
            wav_path: WAV 오디오 파일 경로
            checkpoint_path: 체크포인트 저장 경로
            should_cancel: 외부 청크 사이 사용자 취소 여부 확인 콜백

        Returns:
            TranscriptResult 인스턴스
        """
        from steps.transcriber import Transcriber, TranscriptResult

        configured_provider = getattr(self._config.stt, "provider", "local")
        if configured_provider not in {"local", "openai"}:
            configured_provider = "local"
        selected_provider = stt_provider or configured_provider
        selected_model = stt_model or (
            getattr(
                self._config.stt,
                "openai_model",
                "gpt-4o-transcribe-diarize",
            )
            if selected_provider == "openai"
            else getattr(
                self._config.stt,
                "model_name",
                "mlx-community/whisper-large-v3-turbo",
            )
        )
        if not isinstance(selected_model, str) or not selected_model:
            selected_model = "mlx-community/whisper-large-v3-turbo"
        if selected_provider not in {"local", "openai"}:
            raise InvalidInputError(
                "지원하지 않는 전사 provider입니다.",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )

        # 체크포인트 복원 시도
        if self._checkpoint_enabled and checkpoint_path.exists():
            logger.info(f"전사 체크포인트 복원: {checkpoint_path}")
            restored = TranscriptResult.from_checkpoint(checkpoint_path)
            self._validate_transcript_checkpoint_selection(
                restored,
                selected_provider=selected_provider,
                selected_model=selected_model,
            )
            return restored

        # 컨테이너 헤더 duration과 실제 디코딩 결과가 다른 손상 파일을 막기 위해
        # 변환된 WAV를 STT 모델 로드 전에 한 번 더 검증한다.
        wav_identity = self._validate_input(wav_path)
        await self._validate_audio_duration(wav_path, wav_identity)

        # VAD 전처리: 음성 구간 감지 (enabled=false이면 None 반환)
        vad_clip_timestamps: list[float] | None = None
        vad_config = getattr(self._config, "vad", None)
        vad_enabled = False
        if vad_config is not None:
            vad_enabled_flag = getattr(vad_config, "enabled", False)
            vad_mode = getattr(vad_config, "mode", "off")
            vad_enabled = bool(vad_enabled_flag) or (
                isinstance(vad_mode, str) and vad_mode.lower() != "off"
            )
        if vad_enabled and selected_provider == "local":
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

        if selected_provider == "openai":
            from steps.openai_transcriber import OpenAITranscriber

            stt_config = self._config.stt.model_copy(
                update={"provider": "openai", "openai_model": selected_model}
            )
            execution_config = self._config.model_copy(update={"stt": stt_config})
            transcriber: Any = OpenAITranscriber(execution_config)
        else:
            stt_config = self._config.stt.model_copy(
                update={"provider": "local", "model_name": selected_model}
            )
            execution_config = self._config.model_copy(update={"stt": stt_config})
            transcriber = Transcriber(execution_config, self._model_manager)

        # 로컬 STT의 전체 단계 타임아웃만 오디오 길이에 비례해 계산한다.
        # OpenAI 어댑터는 각 업로드 청크 요청마다 stt.openai_timeout_seconds를
        # 적용하므로 전체 오디오 길이 기반 값을 전달하면 안 된다.
        timeout_override: int | None = None
        if selected_provider == "local" and self._config.pipeline.dynamic_timeout_enabled:
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

        openai_resume_dir = checkpoint_path.parent / ".openai-transcribe-parts"
        if selected_provider == "openai":
            result = await transcriber.transcribe(
                wav_path,
                vad_clip_timestamps=vad_clip_timestamps,
                timeout_override=None,
                resume_dir=openai_resume_dir,
                should_cancel=should_cancel,
                expected_audio_identity=wav_identity,
            )
        else:
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
            self._save_result_checkpoint(result, checkpoint_path)

        if selected_provider == "openai":
            try:
                transcriber.cleanup_resume_cache(openai_resume_dir)
            except Exception as exc:  # noqa: BLE001 - 최종 체크포인트는 이미 안전하다.
                logger.warning(f"OpenAI 전사 재개 캐시 정리 실패: {exc}")

        return result

    async def _run_step_diarize(
        self,
        wav_path: Path,
        checkpoint_path: Path,
        transcript_result: Any | None = None,
    ) -> Any:
        """화자분리 단계: pyannote-audio로 화자를 분리한다.

        Args:
            wav_path: WAV 오디오 파일 경로
            checkpoint_path: 체크포인트 저장 경로

        Returns:
            DiarizationResult 인스턴스
        """
        from steps.diarizer import (
            DiarizationResult,
            DiarizationSegment,
            Diarizer,
        )

        # 체크포인트 복원 시도
        if self._checkpoint_enabled and checkpoint_path.exists():
            logger.info(f"화자분리 체크포인트 복원: {checkpoint_path}")
            return DiarizationResult.from_checkpoint(checkpoint_path)

        transcript_segments = list(getattr(transcript_result, "segments", []) or [])
        provider = str(getattr(transcript_result, "provider", "") or "")
        if (
            provider == "openai"
            and transcript_segments
            and all(getattr(segment, "speaker", None) for segment in transcript_segments)
        ):
            diarization_segments = [
                DiarizationSegment(
                    speaker=str(segment.speaker),
                    start=float(segment.start),
                    end=float(segment.end),
                )
                for segment in transcript_segments
            ]
            speakers = {segment.speaker for segment in diarization_segments}
            result = DiarizationResult(
                segments=diarization_segments,
                num_speakers=len(speakers),
                audio_path=str(wav_path),
                model_name=f"openai:{getattr(transcript_result, 'model', '')}",
                output_mode="provider",
            )
            if self._checkpoint_enabled:
                self._save_result_checkpoint(result, checkpoint_path)
            logger.info("OpenAI provider 화자 구간 사용: speakers=%d", len(speakers))
            return result

        # 재개 경로에서도 pyannote 모델을 열기 전에 변환 WAV 전체를 검증한다.
        wav_identity = self._validate_input(wav_path)
        await self._validate_audio_duration(wav_path, wav_identity)

        diarization_audio_path = wav_path
        silence_plan: Any | None = None
        if getattr(self._config.diarization, "silence_compression_enabled", False) is True:
            try:
                from steps.diarization_silence import prepare_diarization_audio

                silence_plan = prepare_diarization_audio(
                    audio_path=wav_path,
                    output_path=checkpoint_path.with_name("diarization_input.compressed.wav"),
                    diarization_config=self._config.diarization,
                )
                diarization_audio_path = silence_plan.audio_path
            except Exception as e:
                logger.warning(f"화자분리 긴 무음 압축 준비 실패, 원본 WAV 사용: {e}")

        timeout_duration_seconds = (
            float(silence_plan.original_duration) if silence_plan is not None else None
        )
        diarizer = Diarizer(self._config, self._model_manager)
        if timeout_duration_seconds is None:
            result = await diarizer.diarize(diarization_audio_path)
        else:
            result = await diarizer.diarize(
                diarization_audio_path,
                timeout_duration_seconds=timeout_duration_seconds,
            )

        if silence_plan is not None and getattr(silence_plan, "applied", False):
            from steps.diarization_silence import remap_diarization_result

            result = remap_diarization_result(
                result,
                silence_plan,
                original_audio_path=wav_path,
            )

        # 체크포인트 저장
        if self._checkpoint_enabled:
            self._save_result_checkpoint(result, checkpoint_path)

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
        from steps.merger import MergedResult, MergedUtterance, Merger

        # 체크포인트 복원 시도
        if self._checkpoint_enabled and checkpoint_path.exists():
            logger.info(f"병합 체크포인트 복원: {checkpoint_path}")
            return MergedResult.from_checkpoint(checkpoint_path)

        transcript_segments = list(getattr(transcript_result, "segments", []) or [])
        provider_speakers = (
            str(getattr(diarization_result, "output_mode", "") or "") == "provider"
            and str(getattr(transcript_result, "provider", "") or "") == "openai"
            and transcript_segments
            and all(getattr(segment, "speaker", None) for segment in transcript_segments)
        )
        if provider_speakers:
            # OpenAI diarized_json은 텍스트·시간·화자가 같은 provider segment의
            # 원자적 속성이다. 이를 동일 시간의 별도 diarization interval로 바꿔
            # overlap 기반 Merger에 다시 넣으면 겹말에서 다른 화자가 선택될 수
            # 있으므로 provider 화자를 그대로 보존한다.
            utterances = [
                MergedUtterance(
                    text=str(segment.text),
                    speaker=str(segment.speaker),
                    start=float(segment.start),
                    end=float(segment.end),
                )
                for segment in transcript_segments
            ]
            result = MergedResult(
                utterances=utterances,
                num_speakers=len({utterance.speaker for utterance in utterances}),
                audio_path=str(getattr(transcript_result, "audio_path", "") or ""),
                unknown_count=0,
            )
        else:
            merger = Merger()
            result = await merger.merge(transcript_result, diarization_result)

        # 체크포인트 저장
        if self._checkpoint_enabled:
            self._save_result_checkpoint(result, checkpoint_path)

        return result

    async def _run_step_correct(
        self,
        merged_result: Any,
        checkpoint_path: Path,
        *,
        restore_checkpoint: bool = True,
        exclusive_publish: bool = False,
    ) -> Any:
        """보정 단계: EXAONE LLM으로 전사문을 보정한다.

        Args:
            merged_result: 병합 결과
            checkpoint_path: 체크포인트 저장 경로
            restore_checkpoint: 기존 체크포인트 복원 여부
            exclusive_publish: 기존 entry를 교체하지 않는 새 파일 게시 여부

        Returns:
            CorrectedResult 인스턴스
        """
        from steps.corrector import CorrectedResult, Corrector

        # 체크포인트 복원 시도
        if self._checkpoint_enabled and restore_checkpoint and checkpoint_path.exists():
            logger.info(f"보정 체크포인트 복원: {checkpoint_path}")
            if exclusive_publish:
                raise PipelineError(
                    "SECURITY_BLOCKED: 기존 지연 보정 산출물의 provenance를 확인할 수 없어 "
                    "자동 복구하지 않습니다"
                )
            return CorrectedResult.from_checkpoint(checkpoint_path)

        corrector = Corrector(self._config, self._model_manager)
        result = await corrector.correct(merged_result)

        # 체크포인트 저장
        if self._checkpoint_enabled:
            self._save_result_checkpoint(
                result,
                checkpoint_path,
                exclusive_publish=exclusive_publish,
            )

        return result

    async def _run_step_summarize(
        self,
        corrected_result: Any,
        checkpoint_path: Path,
        output_dir: Path,
        *,
        restore_checkpoint: bool = True,
        exclusive_publish: bool = False,
    ) -> Any:
        """요약 단계: EXAONE LLM으로 마크다운 회의록을 생성한다.

        Args:
            corrected_result: 보정 결과
            checkpoint_path: 체크포인트 저장 경로
            output_dir: 회의록 마크다운 저장 디렉토리
            restore_checkpoint: 기존 체크포인트 복원 여부
            exclusive_publish: 기존 entry를 교체하지 않는 새 파일 게시 여부

        Returns:
            SummaryResult 인스턴스
        """
        from steps.summarizer import Summarizer, SummaryResult

        # 체크포인트 복원 시도
        if self._checkpoint_enabled and restore_checkpoint and checkpoint_path.exists():
            logger.info(f"요약 체크포인트 복원: {checkpoint_path}")
            if exclusive_publish:
                raise PipelineError(
                    "SECURITY_BLOCKED: 기존 지연 요약 산출물의 provenance를 확인할 수 없어 "
                    "자동 복구하지 않습니다"
                )
            return SummaryResult.from_checkpoint(checkpoint_path)

        summarizer = Summarizer(self._config, self._model_manager)
        result = await summarizer.summarize(corrected_result)

        # 체크포인트 저장
        if self._checkpoint_enabled:
            self._save_result_checkpoint(
                result,
                checkpoint_path,
                exclusive_publish=exclusive_publish,
            )

        # 마크다운 회의록 파일 저장
        markdown_path = self._validate_storage_artifact(
            output_dir / "meeting_minutes.md",
            label="요약 출력",
        )
        if exclusive_publish:
            markdown = getattr(result, "markdown", None)
            if not isinstance(markdown, str):
                raise PipelineError("exclusive 요약 결과에 markdown 문자열이 없습니다")
            publish_text_no_replace(markdown_path, markdown)
        else:
            result.save_markdown(markdown_path)

        return result

    def _derive_meeting_date(self, meeting_id: str, audio_path: Path) -> str:
        """meeting_id 또는 오디오 파일에서 회의 날짜를 도출한다.

        우선순위:
          1) meeting_id 가 "meeting_YYYYMMDD_HHMMSS" 또는 "YYYYMMDD_HHMMSS_*" 패턴이면
             해당 날짜 사용
          2) 오디오 파일 mtime 사용
          3) 현재 시각

        Args:
            meeting_id: 회의 식별자
            audio_path: 오디오 파일 경로

        Returns:
            "YYYY-MM-DD" 형식의 날짜 문자열
        """
        import re
        from datetime import datetime

        # 1) meeting_id 패턴 — meeting_YYYYMMDD_... 또는 YYYYMMDD_HHMMSS_...
        match = re.search(r"(\d{4})(\d{2})(\d{2})_\d{6}", meeting_id)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

        # 2) 오디오 파일 mtime
        try:
            entry_stat = audio_path.lstat()
            if stat.S_ISREG(entry_stat.st_mode):
                mtime = datetime.fromtimestamp(entry_stat.st_mtime)
                return mtime.strftime("%Y-%m-%d")
        except OSError:
            pass

        # 3) 현재 날짜 폴백
        return datetime.now().strftime("%Y-%m-%d")

    async def _run_step_chunk(
        self,
        corrected_result: Any,
        checkpoint_path: Path,
        meeting_id: str,
        date: str,
        *,
        restore_checkpoint: bool = True,
        exclusive_publish: bool = False,
    ) -> Any:
        """청크 분할 단계: 보정된 전사문을 RAG 검색용 청크로 분할한다.

        외부 모델 로드가 필요 없는 순수 텍스트 처리 단계.
        실패해도 회의록은 이미 생성된 상태이므로 검색 기능만 영향을 받는다.

        Args:
            corrected_result: 보정 결과
            checkpoint_path: 체크포인트 저장 경로
            meeting_id: 회의 식별자
            date: 회의 날짜 (YYYY-MM-DD)
            restore_checkpoint: 기존 체크포인트 복원 여부
            exclusive_publish: 기존 entry를 교체하지 않는 새 파일 게시 여부

        Returns:
            ChunkedResult 인스턴스
        """
        from steps.chunker import ChunkedResult, Chunker

        # 체크포인트 복원 시도
        if self._checkpoint_enabled and restore_checkpoint and checkpoint_path.exists():
            logger.info(f"청크 체크포인트 복원: {checkpoint_path}")
            if exclusive_publish:
                raise PipelineError(
                    "SECURITY_BLOCKED: 기존 지연 청크 산출물의 provenance를 확인할 수 없어 "
                    "자동 복구하지 않습니다"
                )
            return ChunkedResult.from_checkpoint(checkpoint_path)

        chunker = Chunker(self._config)
        result = await chunker.chunk(corrected_result, meeting_id, date)

        # 체크포인트 저장
        if self._checkpoint_enabled:
            self._save_result_checkpoint(
                result,
                checkpoint_path,
                exclusive_publish=exclusive_publish,
            )

        return result

    async def _run_step_embed(
        self,
        chunked_result: Any,
        checkpoint_path: Path,
        *,
        restore_checkpoint: bool = True,
        exclusive_publish: bool = False,
    ) -> Any:
        """임베딩 단계: 청크를 벡터화하고 ChromaDB + SQLite FTS5 에 저장한다.

        e5-small (~500MB) 임베딩 모델을 ModelLoadManager 로 로드한다.
        ChromaDB 또는 SQLite FTS5 저장 중 하나라도 실패하면 StorageError 가
        전파되어 재시도 루프가 받는다 (fail-loud).

        Args:
            chunked_result: 청크 분할 결과
            checkpoint_path: 체크포인트 저장 경로
            restore_checkpoint: 기존 체크포인트 복원 여부
            exclusive_publish: 기존 entry를 교체하지 않는 새 파일 게시 여부

        Returns:
            EmbeddedResult 인스턴스
        """
        from steps.embedder import EmbeddedResult, Embedder

        # 체크포인트 복원 시도
        if self._checkpoint_enabled and restore_checkpoint and checkpoint_path.exists():
            logger.info(f"임베딩 체크포인트 복원: {checkpoint_path}")
            if exclusive_publish:
                raise PipelineError(
                    "SECURITY_BLOCKED: 기존 지연 임베딩 산출물의 provenance를 확인할 수 없어 "
                    "자동 복구하지 않습니다"
                )
            return EmbeddedResult.from_checkpoint(checkpoint_path)

        embedder = Embedder(self._config, self._model_manager)
        result = await embedder.embed(chunked_result)

        # 체크포인트 저장
        if self._checkpoint_enabled:
            self._save_result_checkpoint(
                result,
                checkpoint_path,
                exclusive_publish=exclusive_publish,
            )

        return result

    def _llm_refresh_conflicts(self, state: PipelineState, meeting_id: str) -> list[Path]:
        """지연 LLM 실행이 기존 산출물을 교체해야 하는 경로를 반환한다."""
        deferred_origin = self._llm_deferred_marker_exists(meeting_id)
        correct_requires_creation = (
            deferred_origin
            or PipelineStep.CORRECT.value in state.skipped_steps
            or PipelineStep.CORRECT.value not in state.completed_steps
        )
        summarize_requires_creation = (
            correct_requires_creation
            or PipelineStep.SUMMARIZE.value in state.skipped_steps
            or PipelineStep.SUMMARIZE.value not in state.completed_steps
        )
        candidates: list[Path] = []

        if correct_requires_creation:
            candidates.extend(
                self._get_checkpoint_path(meeting_id, step)
                for step in (
                    PipelineStep.CORRECT,
                    PipelineStep.SUMMARIZE,
                    PipelineStep.CHUNK,
                    PipelineStep.EMBED,
                )
            )
        elif summarize_requires_creation:
            candidates.append(self._get_checkpoint_path(meeting_id, PipelineStep.SUMMARIZE))

        if summarize_requires_creation:
            output_dir = self._get_output_dir(meeting_id)
            candidates.extend(
                self._validate_storage_artifact(
                    output_dir / filename,
                    label="지연 요약 출력",
                )
                for filename in ("summary.md", "meeting_minutes.md")
            )

        conflicts: list[Path] = []
        for candidate in candidates:
            if not candidate.exists():
                continue
            conflicts.append(candidate)
        return conflicts

    def validate_llm_steps_non_destructive(self, meeting_id: str) -> None:
        """지연 LLM 실행이 기존 파일을 교체하지 않는지 read-only로 검증한다.

        동일 UID namespace에서는 compare-and-delete/replace를 원자적으로 보장할 수
        없다. 과거 버전이 만든 pass-through 또는 stale 산출물이 있으면 모두 보존하고
        명시적인 보안 충돌로 중단한다.
        """
        self._validate_meeting_id(meeting_id)
        state_path = self._get_state_path(meeting_id)
        if not state_path.exists():
            conflicts = [
                path for path in self._llm_refresh_artifact_paths(meeting_id) if path.exists()
            ]
            if conflicts:
                names = ", ".join(path.name for path in conflicts)
                raise PipelineError(
                    "SECURITY_BLOCKED: 상태가 유실된 회의의 기존 지연 LLM 산출물은 "
                    "provenance를 확인할 수 없어 재구성하지 않습니다. 기존 파일은 "
                    f"보존됩니다: {names}"
                )
            return
        state = PipelineState.from_file(state_path)
        deferred_origin = self._llm_deferred_marker_exists(meeting_id)

        # 완료로 기록된 실제 산출물이 사라졌다면 임의 재생성으로 상태를 덮지 않는다.
        # 이름이 검사 직후 바뀌는 경우도 실행 단계의 no-replace/restore가 다시
        # 실패하므로 여기서는 read-only 일관성만 확인한다.
        missing_completed: list[str] = []
        completed_artifact_steps = (
            (
                PipelineStep.CORRECT,
                PipelineStep.SUMMARIZE,
                PipelineStep.CHUNK,
                PipelineStep.EMBED,
            )
            if deferred_origin
            else (PipelineStep.CORRECT, PipelineStep.SUMMARIZE)
        )
        for step in completed_artifact_steps:
            if (
                step.value in state.completed_steps
                and step.value not in state.skipped_steps
                and not self._get_checkpoint_path(meeting_id, step).exists()
            ):
                missing_completed.append(f"{step.value}.json")

        correct_requires_creation = (
            deferred_origin
            or PipelineStep.CORRECT.value in state.skipped_steps
            or PipelineStep.CORRECT.value not in state.completed_steps
        )
        stale_markers: list[str] = []
        if correct_requires_creation:
            for step in (
                PipelineStep.SUMMARIZE,
                PipelineStep.CHUNK,
                PipelineStep.EMBED,
            ):
                if step.value in state.completed_steps and step.value not in state.skipped_steps:
                    stale_markers.append(step.value)

        if missing_completed or stale_markers:
            details = ", ".join(
                [
                    *(f"missing:{name}" for name in missing_completed),
                    *(f"stale-marker:{name}" for name in stale_markers),
                ]
            )
            raise PipelineError(
                "SECURITY_BLOCKED: 지연 LLM 상태와 산출물이 일치하지 않아 "
                f"비파괴 재실행을 중단했습니다. 기존 데이터는 보존됩니다: {details}"
            )

        conflicts = self._llm_refresh_conflicts(state, meeting_id)
        if conflicts:
            names = ", ".join(path.name for path in conflicts)
            raise PipelineError(
                "SECURITY_BLOCKED: 지연 LLM 처리가 기존 산출물을 안전하게 교체할 수 없어 "
                f"중단했습니다. 기존 파일은 보존됩니다: {names}"
            )

    @staticmethod
    def _remove_step_marker(state: PipelineState, step: PipelineStep) -> None:
        """재실행할 단계의 완료/스킵 마커를 상태에서 제거한다."""
        step_name = step.value
        if step_name in state.completed_steps:
            state.completed_steps.remove(step_name)
        if step_name in state.skipped_steps:
            state.skipped_steps.remove(step_name)

    def _should_rebuild_search_index_after_llm(
        self,
        state: PipelineState,
        meeting_id: str,
    ) -> bool:
        """온디맨드 LLM 후처리 후 검색 인덱스를 재생성해야 하는지 판단한다."""
        if self._llm_deferred_marker_exists(meeting_id):
            return True
        if PipelineStep.CHUNK.value in state.completed_steps:
            return True
        if PipelineStep.EMBED.value in state.completed_steps:
            return True
        return any(
            self._get_checkpoint_path(meeting_id, step).exists()
            for step in (PipelineStep.CHUNK, PipelineStep.EMBED)
        )

    async def _rebuild_search_index_after_llm(
        self,
        *,
        meeting_id: str,
        audio_path: Path,
        corrected_result: Any,
        state: PipelineState,
        state_path: Path,
        on_step_start: Callable[[str], Awaitable[None]] | None,
    ) -> None:
        """온디맨드 LLM 보정 뒤 새 chunk/embed를 no-replace로 생성한다."""
        if not self._should_rebuild_search_index_after_llm(state, meeting_id):
            return

        for step in (PipelineStep.CHUNK, PipelineStep.EMBED):
            self._remove_step_marker(state, step)

        try:
            chunk_cp = self._get_checkpoint_path(meeting_id, PipelineStep.CHUNK)
            embed_cp = self._get_checkpoint_path(meeting_id, PipelineStep.EMBED)

            meeting_date = self._derive_meeting_date(meeting_id, audio_path)

            if on_step_start is not None:
                try:
                    await on_step_start(PipelineStep.CHUNK.value)
                except Exception as e:
                    logger.warning(f"on_step_start 콜백 예외 (무시): {e}")

            state.current_step = PipelineStep.CHUNK.value
            self._save_state(state, state_path)
            chunked_result = await self._run_step_chunk(
                corrected_result,
                chunk_cp,
                meeting_id,
                meeting_date,
                restore_checkpoint=chunk_cp.exists(),
                exclusive_publish=True,
            )
            if PipelineStep.CHUNK.value not in state.completed_steps:
                state.completed_steps.append(PipelineStep.CHUNK.value)
            self._save_state(state, state_path)

            if on_step_start is not None:
                try:
                    await on_step_start(PipelineStep.EMBED.value)
                except Exception as e:
                    logger.warning(f"on_step_start 콜백 예외 (무시): {e}")

            state.current_step = PipelineStep.EMBED.value
            self._save_state(state, state_path)
            await self._run_step_embed(
                chunked_result,
                embed_cp,
                restore_checkpoint=embed_cp.exists(),
                exclusive_publish=True,
            )
            if PipelineStep.EMBED.value not in state.completed_steps:
                state.completed_steps.append(PipelineStep.EMBED.value)
            self._save_state(state, state_path)
        except Exception as e:
            state.status = "failed"
            state.error_message = f"LLM 후처리 검색 인덱스 재생성 실패: {e}"
            self._save_state(state, state_path)
            raise PipelineError(state.error_message) from e

    async def _run_step_wiki_compile(
        self,
        *,
        meeting_id: str,
        meeting_date: str,
        summary_result: Any | None,
        corrected_result: Any | None,
        state: PipelineState,
        state_path: Path,
    ) -> None:
        """9단계 Wiki 컴파일 (non-fatal).

        wiki.dry_run=False 인 실제 컴파일 경로에서는 summary markdown 과
        corrected utterances 를 WikiCompilerV2 까지 전달한다. 이 단계는 여전히
        non-fatal 이므로 Wiki 실패가 메인 파이프라인의 completed 상태를 깨지 않는다.

        Args:
            meeting_id: 회의 ID — wiki log 마커에 기록.
            meeting_date: 회의 날짜 (YYYY-MM-DD).
            summary_result: 요약 단계 결과. markdown 또는 summary 속성을 사용한다.
            corrected_result: 보정 단계 결과. utterances 속성을 사용한다.
            state: 파이프라인 상태 — step_results / warnings 에 결과 기록.
            state_path: 상태 파일 경로 — non-fatal 실패 시도 저장.
        """
        # 지연 import — wiki 패키지가 없는 테스트 환경에서 import 비용 회피
        from datetime import date as date_cls

        from steps.wiki_compiler import WikiCompiler  # noqa: PLC0415

        step_start = time.monotonic()
        step_name = PipelineStep.WIKI_COMPILE.value
        try:
            wiki = WikiCompiler(self._config, self._model_manager)
            summary_text = ""
            if summary_result is not None:
                summary_text = str(
                    getattr(
                        summary_result,
                        "markdown",
                        getattr(summary_result, "summary", ""),
                    )
                    or ""
                )
            utterances = []
            if corrected_result is not None:
                utterances = list(getattr(corrected_result, "utterances", []) or [])
            parsed_meeting_date = date_cls.fromisoformat(meeting_date)
            wiki_result = await wiki.run(
                meeting_id=meeting_id,
                summary=summary_text or None,
                utterances=utterances,
                meeting_date=parsed_meeting_date,
            )
            elapsed = time.monotonic() - step_start
            logger.info(
                "wiki 9단계 완료 (non-fatal): %s — %s",
                wiki_result.get("status"),
                wiki_result,
            )
            state.step_results.append(
                StepResult(
                    step=step_name,
                    success=True,
                    elapsed_seconds=round(elapsed, 2),
                    error_message="",
                    checkpoint_path="",
                ).to_dict()
            )
        except Exception as exc:  # noqa: BLE001 — non-fatal 9단계 catch-all
            elapsed = time.monotonic() - step_start
            logger.error(
                "wiki 9단계 실패 (non-fatal): meeting_id=%s, error=%s",
                meeting_id,
                exc,
            )
            state.warnings.append(f"wiki 9단계 실패 (non-fatal): {exc}")
            state.step_results.append(
                StepResult(
                    step=step_name,
                    success=False,
                    elapsed_seconds=round(elapsed, 2),
                    error_message=str(exc),
                    checkpoint_path="",
                ).to_dict()
            )
        finally:
            # 9단계 결과 반영 — state 는 항상 저장 (성공/실패 무관)
            self._save_state(state, state_path)

    async def run(
        self,
        audio_path: Path,
        meeting_id: str | None = None,
        on_step_start: Callable[[str], Awaitable[None]] | None = None,
        on_step_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        skip_llm_steps: bool | None = None,
        should_cancel: Callable[[], bool] | None = None,
        stt_provider: str | None = None,
        stt_model: str | None = None,
    ) -> PipelineState:
        """회의 전체 파이프라인을 같은 회의의 다른 mutation과 직렬화해 실행한다.

        편집·삭제·재전사·지연 LLM이 체크포인트를 동시에 바꾸지 못하도록 회의 ID를
        확정하고 검증한 뒤 전체 실행 구간 동안 공유 lease를 유지한다.
        """
        # ID 자동 생성도 실제 본문과 동일한 lexical 입력 경로를 사용한다.
        normalized_audio_path = audio_path.expanduser().absolute()
        effective_meeting_id = meeting_id or self._generate_meeting_id(normalized_audio_path)
        self._validate_meeting_id(effective_meeting_id)

        async with self._meeting_mutation_coordinator.lease(effective_meeting_id):
            return await self._run_with_meeting_lease_held(
                normalized_audio_path,
                meeting_id=effective_meeting_id,
                on_step_start=on_step_start,
                on_step_progress=on_step_progress,
                skip_llm_steps=skip_llm_steps,
                should_cancel=should_cancel,
                stt_provider=stt_provider,
                stt_model=stt_model,
            )

    async def _run_with_meeting_lease_held(
        self,
        audio_path: Path,
        meeting_id: str | None = None,
        on_step_start: Callable[[str], Awaitable[None]] | None = None,
        on_step_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        skip_llm_steps: bool | None = None,
        should_cancel: Callable[[], bool] | None = None,
        stt_provider: str | None = None,
        stt_model: str | None = None,
    ) -> PipelineState:
        """공유 회의 mutation lease를 보유한 상태에서 파이프라인 본문을 실행한다.

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
            should_cancel: OpenAI 청크 사이 사용자 취소 여부 확인 콜백
            stt_provider: 큐 등록 시점에 고정한 provider (legacy는 None)
            stt_model: 큐 등록 시점에 고정한 실제 모델 (legacy는 None)

        Returns:
            최종 파이프라인 상태 (PipelineState)

        Raises:
            InvalidInputError: 입력 파일이 유효하지 않을 때
            PipelineStepError: 특정 단계 실행 실패 시 (재시도 모두 실패)
            PipelineError: 기타 파이프라인 오류 시
        """
        # 최종 direntry의 symlink 여부를 보존하기 위해 resolve()하지 않는다.
        audio_path = audio_path.expanduser().absolute()

        # 회의 ID를 먼저 확정·검증해 상태 경로가 base 밖으로 나가지 않게 한다.
        if meeting_id is None:
            meeting_id = self._generate_meeting_id(audio_path)
        self._validate_meeting_id(meeting_id)

        # 기존 상태를 입력 gate보다 먼저 읽는다. 전사·화자분리가 끝난 text-only
        # 재개는 원본 오디오가 삭제됐더라도 체크포인트만으로 계속할 수 있다.
        state_path = self._get_state_path(meeting_id)
        output_dir = self._get_output_dir(meeting_id)
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

        requested_provider = str(stt_provider or "")
        requested_model = str(stt_model or "")
        if bool(requested_provider) != bool(requested_model):
            raise InvalidInputError(
                "큐에 고정된 전사 provider/model snapshot이 불완전합니다.",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )
        if requested_provider and requested_provider not in {"local", "openai"}:
            raise InvalidInputError(
                "큐에 고정된 전사 provider가 유효하지 않습니다.",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )

        # 첫 실행 시 실제 전사 provider/model을 체크포인트 상태에 고정한다.
        # 이후 설정이 바뀌어도 재개 작업은 같은 선택을 사용한다.
        stt_selection_initialized = False
        transcript_already_completed = PipelineStep.TRANSCRIBE.value in state.completed_steps
        restored_transcript = (
            self._read_transcript_checkpoint(meeting_id) if transcript_already_completed else None
        )
        restored_selection = (
            self._transcript_checkpoint_selection(restored_transcript)
            if restored_transcript is not None
            else None
        )
        if not state.stt_provider:
            # 상태 파일이 유실/구버전이어도 전사 체크포인트 provenance를 우선한다.
            # provenance 자체가 없던 과거 결과만 legacy local로 이관한다.
            if restored_selection is not None:
                state.stt_provider = restored_selection[0]
            elif transcript_already_completed:
                state.stt_provider = "local"
            elif requested_provider:
                state.stt_provider = requested_provider
            else:
                configured_provider = getattr(self._config.stt, "provider", "local")
                state.stt_provider = (
                    configured_provider if configured_provider in {"local", "openai"} else "local"
                )
            stt_selection_initialized = True
        if not state.stt_model:
            if restored_selection is not None:
                state.stt_model = restored_selection[1]
            elif transcript_already_completed and state.stt_provider == "local":
                state.stt_model = "legacy-local"
            elif requested_model and state.stt_provider == requested_provider:
                state.stt_model = requested_model
            elif state.stt_provider == "openai":
                state.stt_model = getattr(
                    self._config.stt,
                    "openai_model",
                    "gpt-4o-transcribe-diarize",
                )
            else:
                configured_model = getattr(
                    self._config.stt,
                    "model_name",
                    "mlx-community/whisper-large-v3-turbo",
                )
                state.stt_model = (
                    configured_model
                    if isinstance(configured_model, str) and configured_model
                    else "mlx-community/whisper-large-v3-turbo"
                )
            stt_selection_initialized = True
        if state.stt_provider not in {"local", "openai"}:
            raise InvalidInputError(
                "파이프라인 상태의 전사 provider가 유효하지 않습니다.",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )
        if requested_provider and (
            state.stt_provider != requested_provider or state.stt_model != requested_model
        ):
            raise InvalidInputError(
                "큐에 고정된 전사 선택과 파이프라인 상태의 선택이 다릅니다.",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )
        allowed_openai_model = getattr(
            self._config.stt,
            "openai_model",
            "gpt-4o-transcribe-diarize",
        )
        if state.stt_provider == "openai" and state.stt_model != allowed_openai_model:
            raise InvalidInputError(
                "파이프라인 상태의 OpenAI 전사 모델이 허용 목록과 다릅니다.",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )
        if restored_transcript is not None:
            self._validate_transcript_checkpoint_selection(
                restored_transcript,
                selected_provider=state.stt_provider,
                selected_model=state.stt_model,
            )

        resume_idx = self._find_resume_step(state)
        if resume_idx is None:
            logger.info("모든 단계가 이미 완료되었습니다.")
            if (
                state.status != "completed"
                or state.current_step
                or state.error_message
                or stt_selection_initialized
            ):
                state.status = "completed"
                state.current_step = ""
                state.error_message = ""
                self._save_state(state, state_path)
            return state

        diarize_idx = PIPELINE_STEPS.index(PipelineStep.DIARIZE)
        if resume_idx is not None and resume_idx <= diarize_idx:
            admission_path = audio_path
            if resume_idx > 0:
                if not state.wav_path:
                    raise InvalidInputError(
                        "convert 완료 상태에 변환 WAV 경로가 없습니다.",
                        failure_kind=AudioFailureKind.SECURITY_BLOCKED,
                    )
                admission_path = self._validate_pipeline_wav_path(
                    output_dir,
                    Path(state.wav_path),
                )
            admission_identity = self._validate_input(admission_path)
            await self._validate_audio_duration(admission_path, admission_identity)

        # admission 통과 후에만 새 출력 디렉터리를 no-follow로 만든다.
        ensure_directory_no_follow(output_dir)
        output_dir = self._get_output_dir(meeting_id)

        # === Graceful Degradation: 시작 전 리소스 점검 ===
        resource_status = self._resource_guard.check_all()

        if not resource_status.disk_ok:
            state.status = "failed"
            state.error_message = (
                f"디스크 여유 공간 부족으로 파이프라인 중단: {resource_status.disk_free_gb:.1f}GB"
            )
            state.warnings.append(state.error_message)
            self._save_state(state, state_path)
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
        self._save_state(state, state_path)

        # 오디오 길이 선 획득 (ETA 예측용). 실패해도 치명적이지 않음.
        if state.audio_duration_seconds <= 0:
            try:
                from steps.audio_converter import AudioConverter

                _converter_probe = AudioConverter(self._config)
                _info = _converter_probe.probe(audio_path)
                if _info is not None and _info.duration > 0:
                    state.audio_duration_seconds = float(_info.duration)
                    self._save_state(state, state_path)
            except Exception as e:
                logger.debug(f"오디오 길이 사전 조회 실패(무시): {e}")

        logger.info(
            f"파이프라인 시작: meeting_id={meeting_id}, "
            f"audio={audio_path.name}"
            f"{', degraded=True' if state.degraded else ''}"
        )

        if resume_idx > 0:
            logger.info(f"단계 {PIPELINE_STEPS[resume_idx].value}부터 재개")

        # 중간 결과 저장용
        wav_path: Path | None = None
        transcript_result: Any = None
        diarization_result: Any = None
        merged_result: Any = None
        corrected_result: Any = None
        _summary_result: Any = None
        chunked_result: Any = None

        # 이전에 완료된 단계의 결과 복원
        if resume_idx > 0:
            try:
                (
                    wav_path,
                    transcript_result,
                    diarization_result,
                    merged_result,
                    corrected_result,
                    chunked_result,
                ) = await self._restore_intermediate_results(
                    meeting_id,
                    resume_idx,
                    audio_path,
                    state,
                )
            except PipelineError as exc:
                state.status = "failed"
                state.current_step = PIPELINE_STEPS[resume_idx].value
                state.error_message = str(exc)
                self._save_state(state, state_path)
                raise

        # 각 단계 순차 실행
        pipeline_start = time.monotonic()

        for step_idx in range(resume_idx, len(PIPELINE_STEPS)):
            step = PIPELINE_STEPS[step_idx]
            checkpoint_path = self._get_checkpoint_path(meeting_id, step)

            # === 숫자 정규화: CORRECT 단계 진입 직전 (LLM 독립, skip_llm에서도 동작) ===
            if step == PipelineStep.CORRECT and merged_result is not None:
                self._apply_number_normalization(merged_result)

            # CORRECT가 실행되지 않은 결과로 요약/검색 산출물을 만들면 이후의
            # 지연 LLM 처리가 기존 파일을 교체해야 한다. 동일 UID namespace에서
            # 안전한 교체를 보장할 수 없으므로 의존 단계도 함께 건너뛰고, 나중에
            # merge.json에서 모든 산출물을 처음 한 번만 생성하도록 남겨 둔다.
            if PipelineStep.CORRECT.value in state.skipped_steps and step in (
                PipelineStep.SUMMARIZE,
                PipelineStep.CHUNK,
                PipelineStep.EMBED,
            ):
                skip_msg = (
                    f"{PipelineStep.CORRECT.value} 단계가 건너뛰어져 "
                    f"{step.value} 단계도 함께 건너뜀"
                )
                logger.warning(skip_msg)
                if step.value not in state.skipped_steps:
                    state.skipped_steps.append(step.value)
                state.degraded = True
                if skip_msg not in state.warnings:
                    state.warnings.append(skip_msg)
                if step == PipelineStep.SUMMARIZE:
                    await self._unload_llm_model_if_current()
                state.step_results.append(
                    StepResult(
                        step=step.value,
                        success=True,
                        elapsed_seconds=0.0,
                        error_message=f"건너뜀: {skip_msg}",
                        checkpoint_path="",
                    ).to_dict()
                )
                if step.value not in state.completed_steps:
                    state.completed_steps.append(step.value)
                self._save_state(state, state_path)
                continue

            # === Graceful Degradation / LLM 스킵: 단계별 리소스 재점검 ===
            if self._resource_guard.is_llm_step(step.value):
                # 단계 직전 실시간 메모리 재확인 — state.degraded(초기 진단값)와 무관하게 독립 판단.
                # state.degraded=True 이더라도 실시간으로 메모리가 회복되었으면 LLM 단계를 실행한다.
                # 반대로 초기에 OK였어도 실시간 mem_ok=False면 스킵한다.
                # check_llm_capacity 가 사전 경고(빠듯) + 차단(부족) 둘 다 처리.
                _, _, llm_warning = self._resource_guard.check_llm_capacity()
                if llm_warning is not None and llm_warning not in state.warnings:
                    state.warnings.append(llm_warning)
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
                    if step.value not in state.skipped_steps:
                        state.skipped_steps.append(step.value)
                    state.degraded = True
                    if skip_msg not in state.warnings:
                        state.warnings.append(skip_msg)

                    skipped_checkpoint_path = ""

                    # correct 스킵 시 현재 실행 안에서만 pass-through 결과를 사용한다.
                    # refresh 대상 checkpoint를 저장하지 않아 지연 LLM이 기존 파일을
                    # 삭제하거나 교체하지 않고 새 산출물을 게시할 수 있게 한다.
                    if step == PipelineStep.CORRECT:
                        assert merged_result is not None
                        if self._checkpoint_enabled:
                            try:
                                self._ensure_llm_deferred_marker(meeting_id)
                            except Exception as e:
                                state.status = "failed"
                                state.current_step = PipelineStep.CORRECT.value
                                state.error_message = f"지연 LLM marker 저장 실패: {e}"
                                self._save_state(state, state_path)
                                raise PipelineError(state.error_message) from e
                        corrected_result = self._build_passthrough_corrected_result(merged_result)
                    elif step == PipelineStep.SUMMARIZE:
                        await self._unload_llm_model_if_current()

                    step_result = StepResult(
                        step=step.value,
                        success=True,
                        elapsed_seconds=0.0,
                        error_message=f"건너뜀: {skip_msg}",
                        checkpoint_path=skipped_checkpoint_path,
                    )
                    state.step_results.append(step_result.to_dict())
                    if step.value not in state.completed_steps:
                        state.completed_steps.append(step.value)
                    self._save_state(state, state_path)
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
            self._save_state(state, state_path)

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
                            stt_provider=state.stt_provider,
                            stt_model=state.stt_model,
                            should_cancel=should_cancel,
                        )

                    elif step == PipelineStep.DIARIZE:
                        assert wav_path is not None
                        diarization_result = await self._run_step_diarize(
                            wav_path,
                            checkpoint_path,
                            transcript_result,
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

                        # 이슈 H + 안정성: _llm_lock (동시 MLX 차단) + 하드 타임아웃
                        # (모델 환각 폭주/hang 차단) 을 헬퍼에서 한꺼번에 적용한다.
                        # 지역 async 헬퍼는 현재 루프 값을 기본 인자로 바인딩한다 (B023 회피).
                        async def _run_correct_step(
                            m: Any = merged_result,
                            cp: Path = checkpoint_path,
                        ) -> Any:
                            return await self._run_step_correct(m, cp)

                        corrected_result = await self._run_llm_step_with_timeout(
                            "correct",
                            _run_correct_step,
                            self._config.pipeline.correct_timeout_seconds,
                        )

                    elif step == PipelineStep.SUMMARIZE:
                        assert corrected_result is not None

                        async def _run_summarize_step(
                            c: Any = corrected_result,
                            cp: Path = checkpoint_path,
                            od: Path = output_dir,
                        ) -> Any:
                            return await self._run_step_summarize(c, cp, od)

                        _summary_result = await self._run_llm_step_with_timeout(
                            "summarize",
                            _run_summarize_step,
                            self._config.pipeline.summarize_timeout_seconds,
                        )

                    elif step == PipelineStep.CHUNK:
                        # CHUNK 는 외부 모델 로드 불필요한 순수 텍스트 처리 단계
                        # corrected_result 는 CORRECT 단계 또는 _restore_intermediate_results 에서 복원됨
                        assert corrected_result is not None
                        meeting_date = self._derive_meeting_date(meeting_id, audio_path)
                        chunked_result = await self._run_step_chunk(
                            corrected_result,
                            checkpoint_path,
                            meeting_id,
                            meeting_date,
                        )

                    elif step == PipelineStep.EMBED:
                        # EMBED 는 e5-small 임베딩 모델 로드 + ChromaDB/FTS5 저장
                        # ModelLoadManager 가 모델 라이프사이클 관리 (acquire/release)
                        # chunked_result 는 CHUNK 단계 또는 _restore_intermediate_results 에서 복원됨
                        # 반환값은 체크포인트로 저장되며 직접 참조 없음 (마지막 단계).
                        assert chunked_result is not None
                        await self._run_step_embed(
                            chunked_result,
                            checkpoint_path,
                        )

                    success = True
                    last_error = None
                    break  # 성공 시 재시도 루프 탈출

                except (AudioAdmissionError, InvalidInputError) as e:
                    # 결정적 입력 차단은 retry/failed 카드로 바꾸지 않는다. 기존
                    # 체크포인트는 유지하고 JobProcessor가 recorded로 돌릴 수 있게
                    # typed InvalidInputError를 즉시 전파한다.
                    state.status = "pending"
                    state.current_step = ""
                    state.error_message = ""
                    self._save_state(state, state_path)
                    failure_kind = getattr(e, "failure_kind", None)
                    logger.info(
                        f"오디오 admission 차단으로 파이프라인 보류: "
                        f"step={step.value}, failure_kind={failure_kind}, reason={e}"
                    )
                    if isinstance(e, InvalidInputError):
                        raise
                    raise InvalidInputError(
                        str(e),
                        failure_kind=failure_kind,
                    ) from e

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
                self._save_state(state, state_path)
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
                self._save_state(state, state_path)

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
        state.error_message = ""
        self._save_state(state, state_path)

        # ── Step 9 (Phase 1): Wiki Compile (dry-run) ─────────────────────
        # PRD §5.1 호출 위치 + §9 Phase 1: 요약 단계 직후 영구 위키 컴파일 트리거.
        # 9단계는 **non-fatal** — Wiki 실패가 메인 RAG 파이프라인을 중단시키지 않으며,
        # 단계 결과는 state.step_results 에 기록되지만 status="completed" 는 유지된다.
        # config.wiki.enabled=False 가 기본값이라 활성화 전까지는 즉시 no-op.
        # `is True` 명시 비교는 MagicMock 등 Truthy 객체가 우회 진입하는 것을 차단
        # (기존 mock_config 기반 단위 테스트가 9단계를 실수로 실행하지 않도록).
        wiki_cfg = getattr(self._config, "wiki", None)
        if wiki_cfg is not None and getattr(wiki_cfg, "enabled", False) is True:
            meeting_date = self._derive_meeting_date(meeting_id, audio_path)
            await self._run_step_wiki_compile(
                meeting_id=meeting_id,
                meeting_date=meeting_date,
                summary_result=_summary_result,
                corrected_result=corrected_result,
                state=state,
                state_path=state_path,
            )

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
    ) -> tuple[Path | None, Any, Any, Any, Any, Any]:
        """이전에 완료된 단계의 중간 결과를 체크포인트에서 복원한다.

        재개 시 이전 단계의 출력이 필요하므로 체크포인트에서 복원한다.
        EMBED 단계 재개를 위해 chunked_result 도 복원한다 (EMBED 가 마지막
        단계이므로 embedded_result 는 복원 대상이 아님).

        Args:
            meeting_id: 회의 ID
            resume_idx: 재개할 단계 인덱스
            audio_path: 원본 오디오 파일 경로
            state: 파이프라인 상태

        Returns:
            (wav_path, transcript_result, diarization_result,
             merged_result, corrected_result, chunked_result) 튜플
        """
        wav_path: Path | None = None
        transcript_result: Any = None
        diarization_result: Any = None
        merged_result: Any = None
        corrected_result: Any = None

        # convert 완료 시 wav_path 복원
        if PipelineStep.CONVERT.value in state.completed_steps:
            diarize_idx = PIPELINE_STEPS.index(PipelineStep.DIARIZE)
            if resume_idx <= diarize_idx:
                if not state.wav_path:
                    raise InvalidInputError(
                        "convert 완료 상태에 변환 WAV 경로가 없습니다.",
                        failure_kind=AudioFailureKind.SECURITY_BLOCKED,
                    )
                wav_path = self._validate_pipeline_wav_path(
                    self._get_output_dir(meeting_id),
                    Path(state.wav_path),
                )
            else:
                # text-only 재개는 WAV를 다시 열지 않으며 기존 상태 문자열만 보존한다.
                wav_path = Path(state.wav_path) if state.wav_path else audio_path

        # transcribe 완료 시 복원
        if PipelineStep.TRANSCRIBE.value in state.completed_steps:
            cp = self._get_checkpoint_path(
                meeting_id,
                PipelineStep.TRANSCRIBE,
            )
            if not cp.exists():
                raise PipelineError(
                    "전사 완료 상태이지만 transcribe 체크포인트가 없습니다. "
                    "기존 오디오와 상태는 보존되며 화자분리를 시작하지 않습니다."
                )
            from steps.transcriber import TranscriptResult

            transcript_result = TranscriptResult.from_checkpoint(cp)
            self._validate_transcript_checkpoint_selection(
                transcript_result,
                selected_provider=state.stt_provider,
                selected_model=state.stt_model,
            )
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
            elif PipelineStep.CORRECT.value in state.skipped_steps and merged_result is not None:
                # 새 skip 계약은 refresh 대상 correct.json을 남기지 않는다.
                # 같은 파이프라인의 text-only 재개에는 merge 결과로 만든
                # in-memory pass-through만 사용한다.
                corrected_result = self._build_passthrough_corrected_result(merged_result)
                logger.info("스킵된 보정 결과를 merge 체크포인트에서 재구성")

        # chunk 완료 시 복원 (EMBED 단계 재개에 필요)
        chunked_result: Any = None
        if PipelineStep.CHUNK.value in state.completed_steps:
            cp = self._get_checkpoint_path(
                meeting_id,
                PipelineStep.CHUNK,
            )
            if cp.exists():
                from steps.chunker import ChunkedResult

                chunked_result = ChunkedResult.from_checkpoint(cp)
                logger.info("청크 결과 체크포인트에서 복원")

        return (
            wav_path,
            transcript_result,
            diarization_result,
            merged_result,
            corrected_result,
            chunked_result,
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
        self._validate_meeting_id(meeting_id)
        state_path = self._get_state_path(meeting_id)

        if not state_path.exists():
            raise PipelineError(f"파이프라인 상태 파일을 찾을 수 없습니다: {meeting_id}")

        state = PipelineState.from_file(state_path)
        audio_path = Path(state.audio_path)

        logger.info(
            f"파이프라인 재개: meeting_id={meeting_id}, 완료 단계: {state.completed_steps}"
        )

        return await self.run(audio_path, meeting_id=meeting_id)

    def _record_llm_steps_failure(
        self,
        meeting_id: str,
        error: BaseException,
    ) -> None:
        """실행 중 실패한 온디맨드 LLM 상태를 best-effort로 failed에 수렴시킨다."""
        try:
            state_path = self._get_state_path(meeting_id)
            if not state_path.exists():
                return
            state = PipelineState.from_file(state_path)
            if state.status != "running":
                return
            message = str(error).strip() or "온디맨드 LLM 작업이 취소되었습니다"
            state.status = "failed"
            state.error_message = message
            self._save_state(state, state_path)
        except Exception:
            # 상태 기록 실패가 원래 모델/게시 오류를 가리면 안 된다.
            logger.exception(f"온디맨드 LLM 실패 상태 저장 실패: meeting_id={meeting_id}")

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
        락 획득 자체에도 하드 타임아웃이 걸려 선행 작업이 비정상 장기화될 때
        무한 대기 대신 PipelineError 로 종료된다.

        Args:
            meeting_id: 회의 ID
            on_step_start: 단계 시작 콜백

        Returns:
            업데이트된 PipelineState

        Raises:
            PipelineError: 상태 파일/merge 체크포인트 미존재, 락 획득 타임아웃,
                          단계 실행 타임아웃 시
        """
        async with self._meeting_mutation_coordinator.lease(meeting_id):
            await self._acquire_llm_lock_with_timeout()
            try:
                try:
                    return await self._run_llm_steps_inner(meeting_id, on_step_start)
                except asyncio.CancelledError as e:
                    self._record_llm_steps_failure(meeting_id, e)
                    raise
                except PipelineStateWriteError:
                    # 경로/교환 검증이 실패한 canonical state를 다시 읽거나 쓰지 않는다.
                    raise
                except Exception as e:
                    self._record_llm_steps_failure(meeting_id, e)
                    raise
            finally:
                try:
                    await self._unload_llm_model_if_current()
                finally:
                    # 모델 cleanup이 실패하거나 deferred native lease가 소유권을
                    # 유지하더라도 프로세스 전역 LLM 직렬화 lock은 반드시 반납한다.
                    self._llm_lock.release()

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
        state_was_missing = not state_path.exists()
        if state_was_missing:
            logger.warning(f"상태 파일 유실 — 체크포인트에서 재구성: meeting_id={meeting_id}")
            self.validate_llm_steps_non_destructive(meeting_id)
            state = self._rebuild_state_from_checkpoints(
                meeting_id,
                trust_llm_checkpoints=False,
                persist=False,
            )
        else:
            state = PipelineState.from_file(state_path)
        # 과거 skip 구현 또는 실패한 지연 실행이 남긴 산출물은 provenance를
        # 인증할 수 없다. 상태 변경이나 모델 실행 전에 모두 보존한 채 중단한다.
        # 검사 뒤 생기는 경쟁 entry는 아래 no-replace 게시가 EEXIST로 다시 차단한다.
        self.validate_llm_steps_non_destructive(meeting_id)

        from steps.merger import MergedResult

        merged_result = MergedResult.from_checkpoint(merge_cp)
        logger.info(f"merge 체크포인트 로드 완료: {merge_cp}")

        # 2.5. 숫자 정규화 (LLM 독립, correct 전에 적용)
        self._apply_number_normalization(merged_result)

        # 3. 출력 디렉토리 확인
        output_dir = self._get_output_dir(meeting_id)
        ensure_directory_no_follow(output_dir)
        output_dir = self._get_output_dir(meeting_id)

        if state_was_missing:
            # prevalidation과 메모리 복구 사이에 부분 산출물이 생겨도
            # canonical state를 생성하기 전에 다시 차단한다.
            self.validate_llm_steps_non_destructive(meeting_id)
        state.status = "running"
        self._save_state(state, state_path)

        # 4. correct 단계 실행
        correct_cp = self._get_checkpoint_path(meeting_id, PipelineStep.CORRECT)
        deferred_origin = self._llm_deferred_marker_exists(meeting_id)
        correct_was_skipped = PipelineStep.CORRECT.value in state.skipped_steps
        correct_requires_creation = (
            deferred_origin
            or correct_was_skipped
            or PipelineStep.CORRECT.value not in state.completed_steps
        )
        if not correct_requires_creation:
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
            self._save_state(state, state_path)

            # 안정성: 단계 자체에도 하드 타임아웃 (모델 환각 폭주/hang 차단).
            # 락은 상위에서 이미 보유 중이므로 재획득하지 않는다.
            try:
                corrected_result = await asyncio.wait_for(
                    self._run_step_correct(
                        merged_result,
                        correct_cp,
                        restore_checkpoint=correct_cp.exists(),
                        exclusive_publish=True,
                    ),
                    timeout=self._config.pipeline.correct_timeout_seconds,
                )
            except TimeoutError as e:
                raise PipelineError(
                    f"correct 단계 타임아웃 ({self._config.pipeline.correct_timeout_seconds}s)"
                ) from e

        # 5. summarize 단계 실행
        summarize_cp = self._get_checkpoint_path(meeting_id, PipelineStep.SUMMARIZE)
        summarize_was_skipped = PipelineStep.SUMMARIZE.value in state.skipped_steps
        summarize_requires_creation = (
            deferred_origin
            or correct_requires_creation
            or summarize_was_skipped
            or PipelineStep.SUMMARIZE.value not in state.completed_steps
        )

        if summarize_requires_creation:
            if on_step_start is not None:
                try:
                    await on_step_start(PipelineStep.SUMMARIZE.value)
                except Exception as e:
                    logger.warning(f"on_step_start 콜백 예외 (무시): {e}")

            state.current_step = PipelineStep.SUMMARIZE.value
            self._save_state(state, state_path)

            try:
                await asyncio.wait_for(
                    self._run_step_summarize(
                        corrected_result,
                        summarize_cp,
                        output_dir,
                        restore_checkpoint=summarize_cp.exists(),
                        exclusive_publish=True,
                    ),
                    timeout=self._config.pipeline.summarize_timeout_seconds,
                )
            except TimeoutError as e:
                raise PipelineError(
                    f"summarize 단계 타임아웃 ({self._config.pipeline.summarize_timeout_seconds}s)"
                ) from e
        else:
            logger.info(f"summarize 체크포인트 복원: {summarize_cp}")

        # 6. 상태 업데이트: skipped_steps에서 제거, completed_steps에 추가
        for step_name in ("correct", "summarize"):
            if step_name in state.skipped_steps:
                state.skipped_steps.remove(step_name)
            if step_name not in state.completed_steps:
                state.completed_steps.append(step_name)

        # 7. 기존 검색 인덱스가 있던 회의는 LLM 보정 결과 기준으로 chunk/embed 재생성
        if (
            deferred_origin or correct_was_skipped
        ) and self._should_rebuild_search_index_after_llm(
            state,
            meeting_id,
        ):
            await self._unload_llm_model_if_current()
            await self._rebuild_search_index_after_llm(
                meeting_id=meeting_id,
                audio_path=Path(state.audio_path)
                if state.audio_path
                else Path("__missing_audio__"),
                corrected_result=corrected_result,
                state=state,
                state_path=state_path,
                on_step_start=on_step_start,
            )

        state.status = "completed"
        state.current_step = ""
        self._save_state(state, state_path)

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
