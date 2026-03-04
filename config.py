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
from typing import Optional

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


class STTConfig(BaseModel):
    """STT (Speech-to-Text) 모델 설정"""
    model_name: str = "whisper-medium-ko-zeroth"
    language: str = "ko"
    beam_size: int = Field(default=5, ge=1, le=20)
    batch_size: int = Field(default=16, ge=1, le=64)


class DiarizationConfig(BaseModel):
    """화자분리 모델 설정"""
    model_name: str = "pyannote/speaker-diarization-3.1"
    device: str = "cpu"  # MPS 버그로 CPU 강제
    min_speakers: int = Field(default=2, ge=1)
    max_speakers: int = Field(default=10, ge=1, le=20)
    huggingface_token: Optional[str] = None

    @field_validator("device")
    @classmethod
    def validate_device(cls, v: str) -> str:
        """MPS 사용 금지 검증. pyannote는 반드시 CPU로 실행한다."""
        if v.lower() == "mps":
            logger.warning("pyannote에서 MPS 사용 금지. CPU로 강제 변경합니다.")
            return "cpu"
        return v


class LLMConfig(BaseModel):
    """LLM (EXAONE via Ollama) 설정"""
    model_name: str = "exaone3.5:7.8b-instruct-q4_K_M"
    host: str = "http://127.0.0.1:11434"
    max_context_tokens: int = Field(default=8192, ge=1024)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    correction_batch_size: int = Field(default=10, ge=1, le=50)
    request_timeout_seconds: int = Field(default=120, ge=10)


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


class LifecycleConfig(BaseModel):
    """데이터 라이프사이클 관리 설정"""
    hot_days: int = Field(default=30, ge=1)
    warm_days: int = Field(default=90, ge=1)
    cold_action: str = "delete_audio"

    @field_validator("cold_action")
    @classmethod
    def validate_cold_action(cls, v: str) -> str:
        """cold_action이 허용된 값인지 검증한다."""
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


def _apply_env_overrides(data: dict) -> dict:
    """환경변수로 설정값을 오버라이드한다.

    지원하는 환경변수:
        MT_BASE_DIR: 기본 데이터 디렉토리
        MT_SERVER_PORT: 서버 포트
        MT_SERVER_HOST: 서버 호스트
        MT_LLM_HOST: Ollama 호스트 URL
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

    # HuggingFace 토큰 (민감 정보이므로 환경변수 권장)
    if env_hf := os.environ.get("HUGGINGFACE_TOKEN"):
        data.setdefault("diarization", {})["huggingface_token"] = env_hf

    return data


def load_config(config_path: Optional[Path] = None) -> AppConfig:
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
    logger.info(f"설정 로드 완료. base_dir={config.paths.resolved_base_dir}")
    return config


# 모듈 수준 싱글턴 인스턴스
_config_instance: Optional[AppConfig] = None


def get_config(config_path: Optional[Path] = None) -> AppConfig:
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
    """싱글턴 인스턴스를 초기화한다. 테스트 용도."""
    global _config_instance
    _config_instance = None
