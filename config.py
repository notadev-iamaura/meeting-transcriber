"""
설정 관리 모듈 (Configuration Management Module)

목적: config.yaml에서 모든 시스템 설정을 로드하고 환경변수 오버라이드를 지원.
주요 기능: YAML 파싱, 기본값 적용, 환경변수 오버라이드, 경로 확장, 검증
의존성: pydantic, pyyaml
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# 설정 파일 기본 경로 (프로젝트 루트의 config.yaml)
_DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"


class PathsConfig(BaseModel):
    """파일 시스템 경로 설정"""

    base_dir: str = "~/.meeting-transcriber"
    audio_input_dir: str = "audio_input"
    outputs_dir: str = "outputs"
    checkpoints_dir: str = "checkpoints"
    chroma_db_dir: str = "chroma_db"
    pipeline_db: str = "pipeline.db"
    meetings_db: str = "meetings.db"
    recordings_temp_dir: str = "recordings_temp"
    audio_quarantine_subdir: str = Field(
        default="audio_quarantine",
        description="거부/삭제된 오디오 파일 격리 서브디렉토리 (base_dir 하위)",
    )

    def resolve_path(self, relative: str) -> Path:
        """base_dir 기준 상대 경로를 절대 경로로 변환한다.

        Args:
            relative: base_dir 하위의 상대 경로 문자열

        Returns:
            확장된 절대 경로 (Path 객체)
        """
        return Path(self.base_dir).expanduser().resolve() / relative

    @property
    def resolved_base_dir(self) -> Path:
        """확장된 base_dir 절대 경로"""
        return Path(self.base_dir).expanduser().resolve()

    @property
    def resolved_audio_input_dir(self) -> Path:
        """오디오 입력 폴더 절대 경로"""
        return self.resolve_path(self.audio_input_dir)

    @property
    def resolved_outputs_dir(self) -> Path:
        """출력 폴더 절대 경로"""
        return self.resolve_path(self.outputs_dir)

    @property
    def resolved_checkpoints_dir(self) -> Path:
        """체크포인트 폴더 절대 경로"""
        return self.resolve_path(self.checkpoints_dir)

    @property
    def resolved_chroma_db_dir(self) -> Path:
        """ChromaDB 저장소 절대 경로"""
        return self.resolve_path(self.chroma_db_dir)

    @property
    def resolved_pipeline_db(self) -> Path:
        """파이프라인 DB 절대 경로"""
        return self.resolve_path(self.pipeline_db)

    @property
    def resolved_meetings_db(self) -> Path:
        """회의 DB 절대 경로"""
        return self.resolve_path(self.meetings_db)

    @property
    def resolved_recordings_temp_dir(self) -> Path:
        """녹음 임시 폴더 절대 경로"""
        return self.resolve_path(self.recordings_temp_dir)

    @property
    def resolved_audio_quarantine_dir(self) -> Path:
        """거부/삭제된 오디오 파일 격리 디렉토리 절대 경로"""
        return self.resolve_path(self.audio_quarantine_subdir)


class VADConfig(BaseModel):
    """VAD (Voice Activity Detection) 설정.

    Silero VAD를 사용하여 오디오에서 음성 구간만 감지한다.
    감지된 구간을 Whisper의 clip_timestamps로 전달하여
    무음 구간에서의 환각(hallucination)을 방지한다.
    """

    enabled: bool = False
    threshold: float = Field(default=0.5, ge=0.0, le=1.0, description="음성 확률 임계값")
    min_speech_duration_ms: int = Field(
        default=250, ge=50, le=2000, description="최소 음성 구간 길이(ms)"
    )
    min_silence_duration_ms: int = Field(
        default=100, ge=30, le=2000, description="최소 무음 구간 길이(ms)"
    )
    speech_pad_ms: int = Field(default=30, ge=0, le=500, description="음성 구간 전후 패딩(ms)")


class HallucinationFilterConfig(BaseModel):
    """환각 필터링 설정.

    Whisper 모델이 무음/잡음 구간에서 생성하는 환각(hallucination) 세그먼트를
    compression_ratio, avg_logprob, no_speech_prob, 반복 패턴 기준으로 필터링한다.
    """

    enabled: bool = True
    compression_ratio_threshold: float = Field(
        default=2.4,
        ge=1.0,
        le=10.0,
        description="압축비 임계값 (초과 시 환각으로 판정)",
    )
    logprob_threshold: float = Field(
        default=-1.0,
        ge=-5.0,
        le=0.0,
        description="평균 로그 확률 임계값 (미만 시 저신뢰도)",
    )
    no_speech_threshold: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description=(
            "무음 확률 임계값 (초과 시 무음 세그먼트 제거). "
            "0.6은 너무 공격적이라 실제 발화도 대량 삭제되는 문제가 있어 0.9로 상향. "
            "더 공격적으로 환각을 제거하려면 0.8, 보수적으로는 0.95 이상을 사용한다."
        ),
    )
    repetition_threshold: int = Field(
        default=3,
        ge=2,
        le=10,
        description="반복 감지 임계값 (동일 패턴 N회 이상 반복 시 환각)",
    )


class AudioQualityConfig(BaseModel):
    """오디오 품질 게이트 설정.

    큐잉 시점에 저볼륨/극단적으로 짧은 파일을 차단하여
    STT 디코더 루프와 MLX Metal 크래시를 예방한다.

    근거: 2026-04-21 meeting_20260420_100536.wav (mean_volume=-48.6dB) 크래시.
         실측 정상 113건 중 최저 -32.1dB 기준 8dB 마진으로 -40dB 임계값 설정.
    """

    enabled: bool = Field(default=True, description="품질 게이트 활성화")
    min_mean_volume_db: float = Field(
        default=-40.0,
        description="허용 최소 mean_volume (dB)",
    )
    min_duration_seconds: float = Field(
        default=5.0, ge=1.0, description="허용 최소 재생 시간 (초)"
    )


class TextPostprocessingConfig(BaseModel):
    """텍스트 후처리 설정.

    Whisper 출력 텍스트의 공백 정규화, 줄바꿈 정리 등을 수행한다.
    """

    enabled: bool = True


class NumberNormalizationConfig(BaseModel):
    """한국어 숫자 정규화 설정.

    한글 숫자(삼십, 이백오십 등)를 아라비아 숫자(30, 250)로 변환한다.
    안전한 단위어가 동반될 때만 변환하여 고유명사 오변환을 방지한다.
    """

    enabled: bool = True
    level: int = Field(
        default=1,
        ge=0,
        le=2,
        description="변환 수준: 0=비활성, 1=보수적(단위어 동반만), 2=중간(복합 수)",
    )


class STTConfig(BaseModel):
    """STT (Speech-to-Text) 모델 설정"""

    model_name: str = "mlx-community/whisper-large-v3-turbo"
    language: str = "ko"
    beam_size: int = Field(default=5, ge=1, le=20)
    batch_size: int = Field(
        default=12,
        ge=1,
        le=64,
        description=(
            "mlx_whisper.transcribe가 batch_size를 명시 지원하는 버전에서만 전달한다. "
            "0.4.x 계열처럼 **decode_options만 받는 버전에는 전달하지 않는다."
        ),
    )
    auto_detect_chipset: bool = False  # True: 칩셋 기반 batch_size 자동 설정
    initial_prompt: str | None = Field(
        default=None,
        description="전사 컨텍스트 힌트 (고유명사, 전문용어). None이면 비활성화.",
    )
    # 전사 작업 타임아웃 (초) — 무한 대기 방지 (STAB)
    transcribe_timeout_seconds: int = Field(
        default=1800, ge=60, description="전사 타임아웃 (초, 기본 30분)"
    )
    # 이전 윈도우 텍스트 전파 제어 (False: 각 윈도우 독립 전사, 오류 전파 방지)
    condition_on_previous_text: bool = Field(
        default=False,
        description="True: 이전 윈도우 텍스트를 다음 윈도우 prompt로 전달. "
        "False: 각 윈도우 독립 전사 (오류 전파 방지, 위원회 권장).",
    )

    @field_validator("initial_prompt")
    @classmethod
    def normalize_initial_prompt(cls, v: str | None) -> str | None:
        """빈 문자열을 None으로 정규화한다.

        Args:
            v: initial_prompt 값

        Returns:
            정규화된 값 (빈 문자열/공백만 있으면 None)
        """
        if v is not None and v.strip() == "":
            return None
        return v

    def resolve_model_path(self, base_dir: str | Path | None = None) -> str:
        """모델 경로를 해석한다.

        tilde(~) 확장 및 로컬 경로 존재 확인을 수행한다.
        로컬 경로가 존재하면 확장된 절대 경로를 반환하고,
        존재하지 않으면 HuggingFace 모델 ID로 간주하여 원본을 반환한다.

        Returns:
            해석된 모델 경로 문자열 (절대 경로 또는 HF 모델 ID)
        """
        expanded = Path(self.model_name).expanduser()
        if expanded.exists():
            resolved = str(expanded.resolve())
            logger.debug(f"로컬 모델 경로 해석: {self.model_name} → {resolved}")
            return resolved
        # tilde가 포함된 경우 로컬 경로로 의도된 것이므로 경고
        if "~" in self.model_name:
            logger.warning(
                f"로컬 모델 경로가 존재하지 않습니다: {self.model_name} "
                f"(확장: {expanded}). HuggingFace에서 다운로드를 시도합니다."
            )
        try:
            from core.stt_model_registry import STT_MODELS
            from core.stt_model_status import get_effective_hf_model_path, get_effective_model_path

            for spec in STT_MODELS:
                if self.model_name in {spec.model_path, spec.hf_source}:
                    effective_spec = get_effective_model_path(spec, base_dir=base_dir)
                    if effective_spec != spec.model_path:
                        logger.debug(
                            f"등록 STT 모델 경로 해석: {self.model_name} → {effective_spec}"
                        )
                        return effective_spec
            effective = get_effective_hf_model_path(self.model_name)
        except Exception as e:
            logger.debug(f"HF 캐시 모델 경로 해석 건너뜀: {e}")
            effective = self.model_name
        if effective != self.model_name:
            logger.debug(f"HF 캐시 모델 경로 해석: {self.model_name} → {effective}")
            return effective
        return self.model_name


class DiarizationConfig(BaseModel):
    """화자분리 모델 설정"""

    model_name: str = "pyannote/speaker-diarization-3.1"
    device: str = "cpu"  # pyannote MPS 버그 회피: 런타임은 CPU 강제
    min_speakers: int = Field(default=2, ge=1)
    max_speakers: int = Field(default=4, ge=1, le=20)
    huggingface_token: str | None = None
    timeout_seconds: int = Field(default=1800, ge=60, description="화자분리 타임아웃 (초)")
    protect_zoom_meetings: bool = True
    zoom_protection_mode: str = Field(default="pause", pattern="^(pause|off)$")
    zoom_protection_poll_seconds: float = Field(default=1.0, ge=0.5, le=10.0)

    @field_validator("device")
    @classmethod
    def validate_device(cls, v: str) -> str:
        """디바이스 값을 검증한다. auto/mps/cpu만 허용.

        Args:
            v: 장치 문자열

        Returns:
            검증된 장치 문자열 (소문자)

        Raises:
            ValueError: 허용되지 않은 디바이스 값
        """
        allowed = {"auto", "mps", "cpu"}
        if v.lower() not in allowed:
            raise ValueError(f"device는 {allowed} 중 하나여야 합니다: '{v}'")
        return v.lower()


class LLMConfig(BaseModel):
    """LLM 백엔드 설정 (MLX 기본, Ollama 선택 가능)

    MLX 지원 모델:
        - mlx-community/gemma-4-e4b-it-4bit (Google Gemma 4, 다국어, 기본값)
        - mlx-community/EXAONE-3.5-7.8B-Instruct-4bit (한국어 특화)
        - mlx-community/gemma-4-e2b-it-4bit (경량, 저사양용)
    """

    # 백엔드 선택: "mlx" (기본, in-process Apple Silicon) 또는 "ollama" (외부 서버)
    backend: str = Field(default="mlx")

    # Ollama 전용 설정 (backend: "ollama" 시 사용)
    model_name: str = "exaone3.5:7.8b-instruct-q4_K_M"
    host: str = "http://127.0.0.1:11434"

    # MLX 전용 설정 (backend: "mlx" 시 사용)
    mlx_model_name: str = "mlx-community/gemma-4-e4b-it-4bit"
    mlx_max_tokens: int = Field(default=2000, ge=100)
    correction_max_tokens: int = Field(
        default=800,
        ge=100,
        description="전사문 교정 단계 응답 토큰 상한. None이 아닌 경우 mlx_max_tokens보다 우선한다.",
    )
    summarize_max_tokens: int = Field(
        default=1200,
        ge=100,
        description="회의록 요약 단계 응답 토큰 상한. None이 아닌 경우 mlx_max_tokens보다 우선한다.",
    )
    chat_max_tokens: int = Field(
        default=1000,
        ge=100,
        description="RAG 채팅 응답 토큰 상한. None이 아닌 경우 mlx_max_tokens보다 우선한다.",
    )

    # 공통 설정
    max_context_tokens: int = Field(default=6144, ge=1024)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    correction_batch_size: int = Field(default=5, ge=1, le=50)
    request_timeout_seconds: int = Field(default=120, ge=10)

    @field_validator("backend")
    @classmethod
    def validate_backend(cls, v: str) -> str:
        """백엔드 값을 검증한다.

        Args:
            v: 백엔드 문자열

        Returns:
            검증된 백엔드 문자열

        Raises:
            ValueError: 지원하지 않는 백엔드 값
        """
        allowed = {"ollama", "mlx"}
        if v not in allowed:
            raise ValueError(f"backend는 {allowed} 중 하나여야 합니다: '{v}'")
        return v


class EmbeddingConfig(BaseModel):
    """임베딩 모델 설정"""

    model_name: str = "intfloat/multilingual-e5-small"
    dimension: int = 384
    device: str = "mps"  # Apple Silicon MPS 가속
    query_prefix: str = "query: "
    passage_prefix: str = "passage: "
    batch_size: int = Field(default=64, ge=1, le=128)


class ChunkingConfig(BaseModel):
    """텍스트 청크 분할 설정"""

    max_tokens: int = Field(default=300, ge=50, le=1000)
    min_tokens: int = Field(default=50, ge=10, le=200)
    time_gap_threshold_seconds: int = Field(default=30, ge=5)
    overlap_tokens: int = Field(default=30, ge=0, le=100)


class SearchConfig(BaseModel):
    """하이브리드 검색 설정"""

    vector_weight: float = Field(default=0.6, ge=0.0, le=1.0)
    fts_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    rrf_k: int = Field(default=60, ge=1)
    top_k: int = Field(default=5, ge=1, le=20)
    fts_tokenizer: str = "unicode61"


class ChatConfig(BaseModel):
    """AI Chat 설정"""

    max_history_pairs: int = Field(default=3, ge=1, le=10)
    system_prompt: str = (
        "당신은 회의 내용을 기반으로 질문에 답변하는 AI 어시스턴트입니다.\n"
        "제공된 회의 전사문을 참고하여 정확하게 답변하세요.\n"
        "회의 내용에 없는 정보는 추측하지 마세요."
    )


class AudioConfig(BaseModel):
    """오디오 변환 설정"""

    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    channels: int = Field(default=1, ge=1, le=2)
    format: str = "wav"
    supported_input_formats: list[str] = Field(
        default=["wav", "mp3", "m4a", "flac", "ogg", "webm"]
    )


class PipelineConfig(BaseModel):
    """파이프라인 실행 설정"""

    peak_ram_limit_gb: float = Field(default=9.5, ge=1.0, le=16.0)
    checkpoint_enabled: bool = True
    checkpoint_json_indent: int | None = Field(
        default=2,
        ge=0,
        description="pipeline_state.json 들여쓰기. null 이면 compact JSON 으로 저장",
    )
    retry_max_count: int = Field(
        default=1,  # Phase 1: 3 → 1 (타임아웃 재시도가 MLX Metal 크래시 유발)
        ge=1,
        le=5,
        description="파이프라인 단계별 최대 재시도 횟수",
    )
    dynamic_timeout_enabled: bool = Field(
        default=True,
        description="오디오 길이에 비례한 동적 타임아웃 사용 여부",
    )
    dynamic_timeout_multiplier: float = Field(
        default=3.0,
        ge=1.0,
        description="타임아웃 = max(min, duration × multiplier)",
    )
    dynamic_timeout_min_seconds: int = Field(
        default=600,  # 10분 최소 (모델 로드 시간 포함)
        ge=60,
        description="동적 타임아웃 최소값 (초)",
    )
    dynamic_timeout_max_seconds: int = Field(
        default=10800,  # 3시간 상한
        ge=600,
        description="동적 타임아웃 최대값 (초, 폭주 방지)",
    )
    min_disk_free_gb: float = Field(default=1.0, ge=0.5, le=16.0)
    # 실측 21건 중 최대 가용 메모리 1.9GB → 2.0GB 임계치 영구 미충족 문제 해결을 위해 1.5로 완화
    # 엣지 케이스(1.5GB 이하)는 각 단계 직전 실시간 check_memory()로 추가 보호
    min_memory_free_gb: float = Field(default=1.5, ge=0.5, le=16.0)
    skip_llm_steps: bool = False  # 기본값: 6단계 모두 실행 (LLM 교정·요약 포함)

    # LLM 단계 전 가용 메모리 사전 경고 — 16GB 맥 환경 가이드.
    # MLX peak 측정: Gemma 4 4bit ≈ 5GB, EXAONE 3.5 4bit ≈ 4.3GB.
    # 추론 + activation 안전 마진 1.5GB 추가 → 권장 가용 6.5GB.
    # 미만이면 사용자에게 경고만 보내고 진행 (skip 은 min_memory_free_gb 가 별도 처리).
    llm_recommended_memory_gb: float = Field(
        default=6.5,
        ge=2.0,
        le=16.0,
        description=(
            "LLM 단계 진입 전 권장 가용 메모리 (GB). "
            "이 값 미만이면 'memory_low_warning' 경고를 발송한다."
        ),
    )

    # LLM 단계 하드 타임아웃 — 모델 무한 루프/환각 폭주 대응
    # 값은 해당 단계 전체 실행 (모델 로드 + 모든 배치 + I/O 포함) 기준.
    # 일반 1시간 회의 기준 실측: correct ~180s, summarize ~60s. 여유 포함 약 4배.
    correct_timeout_seconds: int = Field(
        default=1800,  # 30분
        ge=60,
        description="correct 단계(LLM 보정) 전체 하드 타임아웃 (초)",
    )
    summarize_timeout_seconds: int = Field(
        default=600,  # 10분
        ge=60,
        description="summarize 단계(LLM 요약) 전체 하드 타임아웃 (초)",
    )
    # _llm_lock 획득 타임아웃 — 선행 작업이 비정상 장기화 시 무한 대기 방지.
    # 실용상 단일 LLM 단계 타임아웃 + 약간의 여유 이상이면 충분.
    llm_lock_acquire_timeout_seconds: int = Field(
        default=3600,  # 1시간
        ge=60,
        description="LLM 락 획득 대기 하드 타임아웃 (초). 초과 시 PipelineError.",
    )


class ThermalConfig(BaseModel):
    """서멀 관리 설정 (팬리스 MacBook Air 대응)"""

    batch_size: int = Field(default=2, ge=1, le=10)
    cooldown_seconds: int = Field(default=180, ge=30)
    cpu_temp_throttle_celsius: int = Field(default=85, ge=60, le=100)
    cpu_temp_halt_celsius: int = Field(default=95, ge=70, le=105)


class ServerConfig(BaseModel):
    """FastAPI 서버 설정"""

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    log_level: str = "info"


class WindowConfig(BaseModel):
    """네이티브 창 설정.

    PyWebView를 통한 네이티브 macOS 창의 크기, 제목, 사용 여부를 관리한다.
    use_native=False이면 기본 브라우저로 폴백한다.
    """

    title: str = "Recap"
    width: int = Field(default=1200, ge=400)
    height: int = Field(default=800, ge=300)
    min_width: int = Field(default=800, ge=400)
    min_height: int = Field(default=600, ge=300)
    use_native: bool = True


class ZoomConfig(BaseModel):
    """Zoom 프로세스 감지 설정"""

    process_name: str = "CptHost"
    poll_interval_seconds: int = Field(default=5, ge=1, le=60)
    detection_backend: str = Field(default="coreaudio", pattern="^(coreaudio|process)$")


class WatcherConfig(BaseModel):
    """폴더 감시 설정"""

    debounce_seconds: float = Field(default=2.0, ge=0.5, le=30.0)
    check_interval_seconds: float = Field(default=0.5, ge=0.1, le=5.0)
    excluded_subdirs: list[str] = Field(
        default_factory=lambda: ["audio_quarantine"],
        description="watcher가 감시에서 제외할 서브디렉토리 이름 목록",
    )


class SecurityConfig(BaseModel):
    """보안 설정"""

    data_dir_permissions: int = 0o700
    exclude_from_spotlight: bool = True
    exclude_from_timemachine: bool = True


class RecordingConfig(BaseModel):
    """오디오 녹음 설정 (Zoom 자동 녹음 포함)"""

    enabled: bool = True
    auto_record_on_zoom: bool = True
    prefer_system_audio: bool = True  # BlackHole 설치 시 시스템 오디오 우선
    # 명시적 장치명. 빈 문자열이면 자동 선택(Aggregate > BlackHole > 물리 마이크).
    # 정확 매칭 우선, 없으면 부분 매칭. 예: "Meeting Transcriber Aggregate"
    preferred_device_name: str = ""
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    channels: int = Field(default=1, ge=1, le=2)
    max_duration_seconds: int = Field(default=14400, ge=60)  # 4시간
    min_duration_seconds: int = Field(default=5, ge=1)  # 최소 길이 미달 시 파기
    ffmpeg_graceful_timeout_seconds: int = Field(default=10, ge=1, le=60)
    multi_track: bool = False  # True: BlackHole + 마이크 동시 녹음
    silence_threshold_rms: float = Field(default=0.001, ge=0.0, le=1.0)  # 무음 판정 RMS 임계값
    # Aggregate Device(3채널: 마이크 + BlackHole L/R) 를 모노로 다운믹스할 때
    # 단순 평균(-ac 1) 을 쓰면 마이크 채널이 1/3 로 희석되어 본인 목소리가 약 10dB
    # 저하된다. True 이면 ffmpeg pan 필터로 가중치 다운믹스(c0=0.5 + c1=0.25 + c2=0.25)
    # 를 적용하여 마이크 채널을 보호한다.
    # 2채널/4채널 비정형 Aggregate 환경에서 ffmpeg 에러가 나면 False 로 비활성화.
    aggregate_mic_boost: bool = True


class LifecycleConfig(BaseModel):
    """데이터 라이프사이클 관리 설정"""

    enabled: bool = Field(
        default=False,
        description="자동 라이프사이클 실행 여부. 기존 오디오 자동 삭제를 피하기 위해 기본값은 false.",
    )
    hot_days: int = Field(default=30, ge=1)
    warm_days: int = Field(default=90, ge=1)
    cold_action: str = "delete_audio"
    interval_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="자동 라이프사이클 점검 주기(시간).",
    )
    run_on_startup: bool = Field(
        default=False,
        description="서버 시작 직후 1회 실행 여부. 삭제 작업이므로 기본값은 false.",
    )

    @field_validator("cold_action")
    @classmethod
    def validate_cold_action(cls, v: str) -> str:
        """cold_action이 허용된 값인지 검증한다.

        Args:
            v: cold_action 설정값

        Returns:
            검증된 cold_action 문자열

        Raises:
            ValueError: 허용되지 않는 값일 때
        """
        allowed = {"delete_audio", "archive"}
        if v not in allowed:
            raise ValueError(f"cold_action은 {allowed} 중 하나여야 합니다. 입력값: {v}")
        return v

    @model_validator(mode="after")
    def validate_day_order(self) -> LifecycleConfig:
        """warm_days가 hot_days보다 작지 않도록 검증한다."""
        if self.warm_days < self.hot_days:
            raise ValueError("warm_days는 hot_days 이상이어야 합니다.")
        return self


class AutoProcessingConfig(BaseModel):
    """자동 전사/요약 실행 설정"""

    enabled: bool = Field(
        default=False,
        description="매일 지정 시각에 최근 회의 중 누락된 전사/요약을 자동 처리할지 여부.",
    )
    run_at: str = Field(
        default="02:00",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="자동 처리 실행 시각(HH:MM, 로컬 시간).",
    )
    recent_hours: int = Field(
        default=48,
        ge=1,
        le=720,
        description="자동 처리 대상 최근 시간 범위.",
    )
    action: Literal["transcribe", "summarize", "full"] = Field(
        default="full",
        description="자동 처리 동작: 전사만, 요약만, 또는 누락분 전체.",
    )
    run_on_startup_if_missed: bool = Field(
        default=False,
        description="앱 시작 시 오늘 실행 시각이 이미 지났으면 한 번 실행할지 여부.",
    )


class WikiRankingConfig(BaseModel):
    """Decision Wiki 검색 다중신호 재랭킹 설정 (Memorable Wiki C1).

    BM25 단일 점수를 recency·confidence·인용빈도·superseded 패널티(+선택적 MMR
    다양성)와 결합해 검색 순위를 보정한다. 원문은 변경하지 않고 검색 점수만
    조정한다(불변식 #2 — 점수만 조정). enabled=False 면 순수 BM25 정렬(기존
    동작)로 폴백하는 escape hatch 를 제공한다.

    필드:
        enabled: 다중신호 재랭킹 활성화. False 면 BM25 단일 정렬을 그대로 사용.
        candidate_pool: 재랭킹 입력 후보 풀 크기. 검색 시 BM25 상위 N건을 가져와
            그 안에서만 재랭킹한다. N 을 초과해 한 쿼리에 매칭되는 페이지가 많은
            경우(흔한 토큰·광범위 동의어), BM25 하위지만 최신/고신뢰인 결정이
            재랭킹 전에 누락될 수 있다. corpus·쿼리 매칭 폭이 커지면 상향 조정(C4 측정).
        w_bm25: 정규화된 BM25 관련도 가중치.
        w_recency: 최신성(반감기 감쇠) 가중치.
        w_confidence: 페이지 confidence(0~10 정규화) 가중치.
        w_citation: 정규화된 인용 개수 가중치.
        superseded_penalty: status=superseded 페이지를 live 결정 점수대 아래로 추가로
            내리는 간격. 재랭킹은 superseded 를 구조적으로 모든 비-superseded 아래로
            강제하므로(가중치와 무관하게 '역전 0%' 보장), 이 값은 그 아래에서의 여유.
        recency_half_life_days: 최신성 점수 반감기(일). 작을수록 오래된 결정이 빠르게 감쇠.
        mmr_enabled: MMR 다양성 재정렬 활성화(C1c, 기본 OFF — C4 검증 전까지 보류).
        mmr_lambda: MMR 관련도/다양성 트레이드오프. 1.0=관련도만, 0.0=다양성만.
    """

    enabled: bool = True
    candidate_pool: int = Field(default=50, ge=1)
    w_bm25: float = Field(default=1.0, ge=0.0)
    w_recency: float = Field(default=0.5, ge=0.0)
    w_confidence: float = Field(default=0.3, ge=0.0)
    w_citation: float = Field(default=0.2, ge=0.0)
    superseded_penalty: float = Field(default=0.5, ge=0.0)
    recency_half_life_days: float = Field(default=90.0, gt=0.0)
    mmr_enabled: bool = False
    mmr_lambda: float = Field(default=0.7, ge=0.0, le=1.0)


class WikiConfig(BaseModel):
    """LLM Wiki Phase 1 설정 (PRD §5.1, §9 Phase 1).

    LLM Wiki 는 회의 요약 결과를 영구 위키 페이지(decisions/people/projects/
    topics) 로 컴파일하는 9단계 파이프라인 확장이다. Phase 1 에서는 실제 LLM
    호출 없이 골격만 통합하므로 기본값은 `enabled=False`, `dry_run=True` 로
    안전하게 비활성화되어 있다.

    필드:
        enabled: 9단계(WIKI_COMPILE) 활성화 여부. False 이면 PipelineManager 가
            wiki 단계를 호출하지 않고 곧바로 종료한다.
        root: wiki 루트 디렉토리. `~` 확장은 호출 측이 명시적으로 수행해야 한다.
        compiler_model: Wiki 컴파일러 LLM 모델. Gemma 4 사용 (사용자 환경에
            맞춤). EXAONE 도 가능하지만 별도 설치가 필요하다. 8단계(요약)에서
            이미 Gemma 가 메모리에 로드되어 있으면 9단계(Wiki) 가 그대로
            재사용한다.
        lint_interval: D4 (cross-page lint) 를 N 회의마다 실행. Phase 2 도입.
        confidence_threshold: D3 (페이지 confidence 컷오프). 0~10 정수 중 7 이상이
            기본값. Phase 2 도입.
        dry_run: Phase 1 골격에서는 항상 True. 실제 LLM 호출/페이지 작성은 안 함.

    환경변수 오버라이드 (PRD §9 Phase 1.B + Phase 5):
        - MT_WIKI_ENABLED=true|false → enabled
        - MT_WIKI_ROOT=/path → root
        - MT_WIKI_DRY_RUN=true|false → dry_run
        - MT_WIKI_ROUTER_ENABLED=true|false → router_enabled (Phase 5)
        - MT_WIKI_ROUTER_LLM_FALLBACK=true|false → router_llm_fallback (Phase 5)
    """

    enabled: bool = False
    root: Path = Path("~/.meeting-transcriber/wiki/")
    compiler_model: str = "mlx-community/gemma-4-e4b-it-4bit"
    lint_interval: int = Field(default=5, ge=1)
    confidence_threshold: int = Field(default=7, ge=0, le=10)
    dry_run: bool = True

    # ─── Phase 5 신규 ─────────────────────────────────────────────────
    router_enabled: bool = False
    """질의 라우터(QueryRouter) 활성화 여부. False(default) 면 라우터 코드는
    로드되지만 ChatEngine 이 그대로 호출된다 — 기존 RAG 채팅 100% 무영향
    (PRD §10.3 회귀 테스트 보장). 사용자가 명시적으로 True 로 바꾼 경우만
    HybridChatService 가 라우팅을 수행한다.

    환경변수: MT_WIKI_ROUTER_ENABLED=true|false
    """

    router_llm_fallback: bool = True
    """라우터의 LLM 폴백(휴리스틱 매칭 0건일 때 LLM 분류) 활성화 여부.
    False 면 매칭 실패 시 즉시 RAG fallback (저비용 + 결정성 우선).

    환경변수: MT_WIKI_ROUTER_LLM_FALLBACK=true|false
    """

    # ─── Memorable Wiki C1 — 검색 다중신호 재랭킹 ───────────────────────
    ranking: WikiRankingConfig = Field(default_factory=WikiRankingConfig)
    """Decision Wiki 검색(BM25) 후처리 재랭킹 설정. config.yaml `wiki.ranking`
    하위에서 가중치/반감기/MMR 을 조정한다. 누락 시 코드 기본값(enabled=True)."""

    @property
    def resolved_root(self) -> Path:
        """root 의 ~ 확장 + 절대 경로 변환.

        Returns:
            확장된 wiki 루트의 절대 경로.
        """
        return Path(self.root).expanduser().resolve()


class AppConfig(BaseModel):
    """애플리케이션 전체 설정을 관리하는 최상위 모델.

    config.yaml 파싱 결과를 pydantic 모델로 변환하고,
    환경변수를 통한 개별 설정 오버라이드를 지원한다.
    """

    paths: PathsConfig = Field(default_factory=PathsConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    thermal: ThermalConfig = Field(default_factory=ThermalConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    zoom: ZoomConfig = Field(default_factory=ZoomConfig)
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    lifecycle: LifecycleConfig = Field(default_factory=LifecycleConfig)
    auto_processing: AutoProcessingConfig = Field(default_factory=AutoProcessingConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    window: WindowConfig = Field(default_factory=WindowConfig)
    vad: VADConfig = Field(default_factory=VADConfig)
    hallucination_filter: HallucinationFilterConfig = Field(
        default_factory=HallucinationFilterConfig
    )
    text_postprocessing: TextPostprocessingConfig = Field(default_factory=TextPostprocessingConfig)
    number_normalization: NumberNormalizationConfig = Field(
        default_factory=NumberNormalizationConfig
    )
    audio_quality: AudioQualityConfig = Field(default_factory=AudioQualityConfig)
    wiki: WikiConfig = Field(default_factory=WikiConfig)


def _parse_bool(value: str) -> bool:
    """문자열을 bool 로 변환한다 (환경변수 오버라이드 헬퍼).

    `true`, `1`, `yes`, `on` (대소문자 무시) → True. 그 외 (`false`, `0`, `no`,
    `off`, 빈 문자열 등) → False. 복잡한 인자 검증 라이브러리 의존성을 피하기
    위해 단순 매칭 사용.

    Args:
        value: 환경변수에서 읽은 문자열.

    Returns:
        파싱된 bool 값.
    """
    return value.strip().lower() in {"true", "1", "yes", "on"}


def _normalize_ollama_host(value: str) -> str:
    """OLLAMA_HOST 값을 AppConfig의 URL 형식으로 정규화한다."""
    stripped = value.strip()
    if stripped.startswith(("http://", "https://")):
        return stripped
    return f"http://{stripped}"


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """환경변수로 설정값을 오버라이드한다.

    지원하는 환경변수:
        MT_BASE_DIR: 기본 데이터 디렉토리
        MT_SERVER_PORT: 서버 포트
        MT_SERVER_HOST: 서버 호스트
        MT_LLM_HOST: Ollama 호스트 URL
        MT_LLM_BACKEND: LLM 백엔드 ("ollama" 또는 "mlx")
        MT_LLM_MODEL: MLX 모델명
        MT_LOG_LEVEL: 로그 레벨
        HUGGINGFACE_TOKEN: HuggingFace 인증 토큰
        HF_TOKEN: HuggingFace 인증 토큰 (HUGGINGFACE_TOKEN 미설정 시 사용)
        OLLAMA_HOST: Ollama 호스트 (MT_LLM_HOST 미설정 시 사용)

    Args:
        data: YAML에서 파싱된 설정 딕셔너리

    Returns:
        환경변수가 반영된 설정 딕셔너리
    """
    # 경로 오버라이드
    if env_base := os.environ.get("MT_BASE_DIR"):
        data.setdefault("paths", {})["base_dir"] = env_base

    # 서버 오버라이드
    if env_port := os.environ.get("MT_SERVER_PORT"):
        data.setdefault("server", {})["port"] = int(env_port)
    if env_host := os.environ.get("MT_SERVER_HOST"):
        data.setdefault("server", {})["host"] = env_host

    # LLM 호스트 오버라이드
    if env_llm_host := os.environ.get("MT_LLM_HOST"):
        data.setdefault("llm", {})["host"] = env_llm_host
    elif env_ollama := os.environ.get("OLLAMA_HOST"):
        data.setdefault("llm", {})["host"] = _normalize_ollama_host(env_ollama)

    # 로그 레벨 오버라이드
    if env_log := os.environ.get("MT_LOG_LEVEL"):
        data.setdefault("server", {})["log_level"] = env_log

    # LLM 백엔드 오버라이드
    if env_backend := os.environ.get("MT_LLM_BACKEND"):
        data.setdefault("llm", {})["backend"] = env_backend

    # LLM 모델명 오버라이드 (MLX 백엔드)
    if env_model := os.environ.get("MT_LLM_MODEL"):
        data.setdefault("llm", {})["mlx_model_name"] = env_model

    # LLM Wiki 오버라이드 (Phase 1)
    # `MT_WIKI_ENABLED`, `MT_WIKI_ROOT`, `MT_WIKI_DRY_RUN` 환경변수 처리.
    # _parse_bool 헬퍼는 "true"/"false"/"1"/"0"/대소문자 혼용을 모두 받는다.
    if (env_wiki_enabled := os.environ.get("MT_WIKI_ENABLED")) is not None:
        data.setdefault("wiki", {})["enabled"] = _parse_bool(env_wiki_enabled)
    if env_wiki_root := os.environ.get("MT_WIKI_ROOT"):
        data.setdefault("wiki", {})["root"] = env_wiki_root
    if (env_wiki_dry := os.environ.get("MT_WIKI_DRY_RUN")) is not None:
        data.setdefault("wiki", {})["dry_run"] = _parse_bool(env_wiki_dry)

    # LLM Wiki Phase 5 오버라이드 — 라우터 활성화 + LLM 폴백 제어
    if (env_router := os.environ.get("MT_WIKI_ROUTER_ENABLED")) is not None:
        data.setdefault("wiki", {})["router_enabled"] = _parse_bool(env_router)
    if (env_router_fb := os.environ.get("MT_WIKI_ROUTER_LLM_FALLBACK")) is not None:
        data.setdefault("wiki", {})["router_llm_fallback"] = _parse_bool(env_router_fb)

    # HuggingFace 토큰 (민감 정보이므로 환경변수 권장)
    # 우선순위: 환경변수 → huggingface-cli 저장 토큰
    env_hf = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
    if not env_hf:
        hf_token_path = Path.home() / ".cache" / "huggingface" / "token"
        if hf_token_path.exists():
            env_hf = hf_token_path.read_text().strip()
    if env_hf:
        data.setdefault("diarization", {})["huggingface_token"] = env_hf

    return data


def load_config(config_path: Path | None = None) -> AppConfig:
    """설정 파일을 로드하고 AppConfig 인스턴스를 반환한다.

    1. YAML 파일이 존재하면 파싱
    2. 환경변수 오버라이드 적용
    3. pydantic 모델로 검증 및 변환

    Args:
        config_path: 설정 파일 경로. None이면 기본 경로 사용.

    Returns:
        검증된 AppConfig 인스턴스

    Raises:
        yaml.YAMLError: YAML 파싱 실패 시
        pydantic.ValidationError: 설정값 검증 실패 시
    """
    path = config_path or (
        Path(env_config).expanduser().resolve()
        if (env_config := os.environ.get("MT_CONFIG_PATH"))
        else _DEFAULT_CONFIG_PATH
    )

    if path.exists():
        logger.info(f"설정 파일 로드: {path}")
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        logger.warning(f"설정 파일 없음: {path}. 기본값으로 동작합니다.")
        data = {}

    # 환경변수 오버라이드 적용
    data = _apply_env_overrides(data)

    config = AppConfig(**data)

    # 칩셋 자동 감지 적용 (활성화 시)
    if config.stt.auto_detect_chipset:
        config = _apply_chipset_overrides(config)

    logger.info(f"설정 로드 완료. base_dir={config.paths.resolved_base_dir}")
    return config


def _apply_chipset_overrides(config: AppConfig) -> AppConfig:
    """칩셋 감지 결과로 STT 설정값을 오버라이드한다.

    ChipsetDetector를 사용해 현재 시스템의 칩셋과 RAM을 감지하고,
    최적의 batch_size를 자동 설정한다.

    Args:
        config: 현재 AppConfig 인스턴스

    Returns:
        칩셋 최적화가 적용된 AppConfig (실패 시 원본 반환)
    """
    try:
        from core.chipset_detector import ChipsetDetector

        detector = ChipsetDetector()
        profile = detector.get_optimal_profile()
        logger.info(f"칩셋 최적화 적용: batch_size={profile.batch_size}")
        new_stt = config.stt.model_copy(update={"batch_size": profile.batch_size})
        return config.model_copy(update={"stt": new_stt})
    except Exception as e:
        logger.warning(f"칩셋 자동 감지 실패, 기존 설정 유지: {e}")
        return config


# 모듈 수준 싱글턴 인스턴스
_config_instance: AppConfig | None = None


def get_config(config_path: Path | None = None) -> AppConfig:
    """싱글턴 패턴으로 AppConfig 인스턴스를 반환한다.

    첫 호출 시 설정을 로드하고, 이후에는 캐시된 인스턴스를 반환.

    Args:
        config_path: 설정 파일 경로 (첫 호출에서만 유효)

    Returns:
        AppConfig 싱글턴 인스턴스
    """
    global _config_instance
    if _config_instance is None:
        _config_instance = load_config(config_path)
    return _config_instance


def reset_config() -> None:
    """싱글턴 인스턴스를 초기화한다. 테스트 용도로만 사용."""
    global _config_instance
    _config_instance = None
