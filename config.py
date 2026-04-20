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
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

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

    enabled: bool = False
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


class TextPostprocessingConfig(BaseModel):
    """텍스트 후처리 설정.

    Whisper 출력 텍스트의 공백 정규화, 줄바꿈 정리 등을 수행한다.
    """

    enabled: bool = False


class NumberNormalizationConfig(BaseModel):
    """한국어 숫자 정규화 설정.

    한글 숫자(삼십, 이백오십 등)를 아라비아 숫자(30, 250)로 변환한다.
    안전한 단위어가 동반될 때만 변환하여 고유명사 오변환을 방지한다.
    """

    enabled: bool = False
    level: int = Field(
        default=1,
        ge=0,
        le=2,
        description="변환 수준: 0=비활성, 1=보수적(단위어 동반만), 2=중간(복합 수)",
    )


class STTConfig(BaseModel):
    """STT (Speech-to-Text) 모델 설정"""

    model_name: str = "whisper-medium-ko-zeroth"
    language: str = "ko"
    beam_size: int = Field(default=5, ge=1, le=20)
    batch_size: int = Field(default=16, ge=1, le=64)
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
        default=True,
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

    def resolve_model_path(self) -> str:
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
        return self.model_name


class DiarizationConfig(BaseModel):
    """화자분리 모델 설정"""

    model_name: str = "pyannote/speaker-diarization-3.1"
    device: str = "auto"  # "auto": MPS 가용 시 MPS, 아니면 CPU / "mps" / "cpu"
    min_speakers: int = Field(default=1, ge=1)
    max_speakers: int = Field(default=10, ge=1, le=20)
    huggingface_token: str | None = None
    timeout_seconds: int = Field(default=1800, ge=60, description="화자분리 타임아웃 (초)")

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
        - mlx-community/EXAONE-3.5-7.8B-Instruct-4bit (한국어 특화, 기본값)
        - mlx-community/gemma-4-e4b-it-4bit (Google Gemma 4, 다국어)
        - mlx-community/gemma-4-e2b-it-4bit (경량, 저사양용)
    """

    # 백엔드 선택: "mlx" (기본, in-process Apple Silicon) 또는 "ollama" (외부 서버)
    backend: str = Field(default="mlx")

    # Ollama 전용 설정 (backend: "ollama" 시 사용)
    model_name: str = "exaone3.5:7.8b-instruct-q4_K_M"
    host: str = "http://127.0.0.1:11434"

    # MLX 전용 설정 (backend: "mlx" 시 사용)
    mlx_model_name: str = "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit"
    mlx_max_tokens: int = Field(default=2000, ge=100)

    # 공통 설정
    max_context_tokens: int = Field(default=8192, ge=1024)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    correction_batch_size: int = Field(default=10, ge=1, le=50)
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
    batch_size: int = Field(default=32, ge=1, le=128)


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
    retry_max_count: int = Field(default=3, ge=0, le=10)
    min_disk_free_gb: float = Field(default=2.0, ge=0.5, le=16.0)
    min_memory_free_gb: float = Field(default=2.0, ge=0.5, le=16.0)
    skip_llm_steps: bool = True  # 기본값: 전사만 진행, LLM 단계(correct, summarize) 스킵


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


class WatcherConfig(BaseModel):
    """폴더 감시 설정"""

    debounce_seconds: float = Field(default=2.0, ge=0.5, le=30.0)
    check_interval_seconds: float = Field(default=0.5, ge=0.1, le=5.0)


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
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    channels: int = Field(default=1, ge=1, le=2)
    max_duration_seconds: int = Field(default=14400, ge=60)  # 4시간
    min_duration_seconds: int = Field(default=5, ge=1)  # 최소 길이 미달 시 파기
    ffmpeg_graceful_timeout_seconds: int = Field(default=10, ge=1, le=60)
    multi_track: bool = False  # True: BlackHole + 마이크 동시 녹음
    silence_threshold_rms: float = Field(default=0.001, ge=0.0, le=1.0)  # 무음 판정 RMS 임계값


class LifecycleConfig(BaseModel):
    """데이터 라이프사이클 관리 설정"""

    hot_days: int = Field(default=30, ge=1)
    warm_days: int = Field(default=90, ge=1)
    cold_action: str = "delete_audio"

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


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """환경변수로 설정값을 오버라이드한다.

    지원하는 환경변수:
        MT_BASE_DIR: 기본 데이터 디렉토리
        MT_SERVER_PORT: 서버 포트
        MT_SERVER_HOST: 서버 호스트
        MT_LLM_HOST: Ollama 호스트 URL
        MT_LLM_BACKEND: LLM 백엔드 ("ollama" 또는 "mlx")
        MT_LOG_LEVEL: 로그 레벨
        HUGGINGFACE_TOKEN: HuggingFace 인증 토큰
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
        data.setdefault("llm", {})["host"] = f"http://{env_ollama}"

    # 로그 레벨 오버라이드
    if env_log := os.environ.get("MT_LOG_LEVEL"):
        data.setdefault("server", {})["log_level"] = env_log

    # LLM 백엔드 오버라이드
    if env_backend := os.environ.get("MT_LLM_BACKEND"):
        data.setdefault("llm", {})["backend"] = env_backend

    # LLM 모델명 오버라이드 (MLX 백엔드)
    if env_model := os.environ.get("MT_LLM_MODEL"):
        data.setdefault("llm", {})["mlx_model_name"] = env_model

    # HuggingFace 토큰 (민감 정보이므로 환경변수 권장)
    # 우선순위: 환경변수 → huggingface-cli 저장 토큰
    env_hf = os.environ.get("HUGGINGFACE_TOKEN")
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
    path = config_path or _DEFAULT_CONFIG_PATH

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
