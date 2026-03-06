"""
LLM 백엔드 추상화 모듈 (LLM Backend Abstraction Module)

목적: Ollama와 MLX 등 다양한 LLM 백엔드를 교체 가능한 프로토콜로 추상화한다.
주요 기능:
    - LLMBackend 프로토콜 (chat, chat_stream, cleanup)
    - 통합 에러 계층 (LLMBackendError, LLMConnectionError, LLMGenerationError, LLMLoadError)
    - OllamaBackend 구현 (기존 ollama_client 래핑)
    - create_backend() 팩토리 함수 (config.llm.backend로 백엔드 선택)
의존성: config 모듈, core/ollama_client 모듈
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# === 통합 에러 계층 ===
# Ollama, MLX 등 모든 백엔드 에러의 공통 기본 클래스.
# 소비자 코드에서 백엔드 종류에 무관하게 에러를 잡을 수 있다.


class LLMBackendError(Exception):
    """LLM 백엔드 관련 에러의 기본 클래스."""


class LLMConnectionError(LLMBackendError):
    """LLM 백엔드에 연결할 수 없을 때 발생한다."""


class LLMGenerationError(LLMBackendError):
    """LLM 응답 생성 실패 시 발생한다 (타임아웃, 파싱 오류 등)."""


class LLMLoadError(LLMBackendError):
    """LLM 모델 로드 실패 시 발생한다."""


# === LLM 백엔드 프로토콜 ===


@runtime_checkable
class LLMBackend(Protocol):
    """LLM 백엔드 인터페이스 프로토콜.

    Ollama, MLX 등 서로 다른 LLM 백엔드가 동일한 인터페이스를 제공하도록 한다.
    소비자 코드(corrector, summarizer, chat)는 이 프로토콜에만 의존한다.
    """

    def chat(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: Optional[float] = None,
        num_ctx: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """LLM에 메시지를 보내고 전체 응답 텍스트를 반환한다.

        Args:
            messages: 대화 메시지 목록 (role, content 쌍)
            temperature: 생성 온도 (None이면 기본값 사용)
            num_ctx: 컨텍스트 윈도우 크기 (None이면 기본값 사용)
            timeout: 요청 타임아웃 초 (None이면 기본값 사용)

        Returns:
            LLM 응답 텍스트

        Raises:
            LLMConnectionError: 연결 실패 시
            LLMGenerationError: 생성 실패 시 (타임아웃, 파싱 오류 등)
        """
        ...

    def chat_stream(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: Optional[float] = None,
        num_ctx: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> Iterator[str]:
        """LLM에 메시지를 보내고 토큰을 스트리밍으로 반환한다.

        Args:
            messages: 대화 메시지 목록 (role, content 쌍)
            temperature: 생성 온도 (None이면 기본값 사용)
            num_ctx: 컨텍스트 윈도우 크기 (None이면 기본값 사용)
            timeout: 요청 타임아웃 초 (None이면 기본값 사용)

        Yields:
            토큰 문자열

        Raises:
            LLMConnectionError: 연결 실패 시
            LLMGenerationError: 생성 실패 시
        """
        ...

    def cleanup(self) -> None:
        """백엔드별 리소스 정리를 수행한다.

        Ollama: no-op (독립 프로세스)
        MLX: 모델 메모리 해제, Metal 캐시 정리
        """
        ...


# === Ollama 백엔드 구현 ===


class OllamaBackend:
    """Ollama 서버 기반 LLM 백엔드.

    기존 core.ollama_client 모듈의 chat(), chat_stream() 함수를 래핑한다.
    LLMBackend 프로토콜을 구현하여 소비자 코드에서 동일한 인터페이스로 사용 가능하다.
    """

    def __init__(self, config: Any) -> None:
        """OllamaBackend를 초기화한다.

        Ollama 서버 연결을 확인하고 설정값을 저장한다.

        Args:
            config: LLMConfig 인스턴스 (host, model_name, temperature 등)

        Raises:
            LLMConnectionError: Ollama 서버에 연결할 수 없을 때
        """
        from core.ollama_client import check_connection

        check_connection(config.host)

        self._host = config.host
        self._model = config.model_name
        self._temperature = config.temperature
        self._num_ctx = config.max_context_tokens
        self._timeout = config.request_timeout_seconds

        logger.info(
            f"OllamaBackend 초기화: model={self._model}, host={self._host}"
        )

    def chat(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: Optional[float] = None,
        num_ctx: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """Ollama /api/chat 엔드포인트를 호출하여 응답을 반환한다.

        Args:
            messages: 대화 메시지 목록
            temperature: 생성 온도 (None이면 초기화 시 설정값 사용)
            num_ctx: 컨텍스트 윈도우 크기
            timeout: 요청 타임아웃 초

        Returns:
            LLM 응답 텍스트

        Raises:
            LLMConnectionError: 연결 실패 시 (OllamaConnectionError)
            LLMGenerationError: 타임아웃/파싱 실패 시 (OllamaTimeoutError/OllamaResponseError)
        """
        from core.ollama_client import chat as ollama_chat

        return ollama_chat(
            host=self._host,
            model=self._model,
            messages=messages,
            temperature=temperature if temperature is not None else self._temperature,
            num_ctx=num_ctx if num_ctx is not None else self._num_ctx,
            timeout=timeout if timeout is not None else self._timeout,
        )

    def chat_stream(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: Optional[float] = None,
        num_ctx: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> Iterator[str]:
        """Ollama /api/chat 엔드포인트를 스트리밍 모드로 호출한다.

        Args:
            messages: 대화 메시지 목록
            temperature: 생성 온도
            num_ctx: 컨텍스트 윈도우 크기
            timeout: 요청 타임아웃 초

        Yields:
            토큰 문자열

        Raises:
            LLMConnectionError: 연결 실패 시
            LLMGenerationError: 타임아웃 시
        """
        from core.ollama_client import chat_stream as ollama_chat_stream

        yield from ollama_chat_stream(
            host=self._host,
            model=self._model,
            messages=messages,
            temperature=temperature if temperature is not None else self._temperature,
            num_ctx=num_ctx if num_ctx is not None else self._num_ctx,
            timeout=timeout if timeout is not None else self._timeout,
        )

    def cleanup(self) -> None:
        """Ollama는 독립 프로세스이므로 정리할 리소스가 없다."""
        pass


# === 팩토리 함수 ===


def create_backend(config: Any) -> LLMBackend:
    """설정에 따라 적절한 LLM 백엔드를 생성하여 반환한다.

    config.backend 값에 따라 Ollama 또는 MLX 백엔드를 선택한다.

    Args:
        config: LLMConfig 인스턴스 (backend 필드로 백엔드 종류 결정)

    Returns:
        LLMBackend 프로토콜을 구현한 백엔드 인스턴스

    Raises:
        LLMConnectionError: 백엔드 연결/초기화 실패 시
        LLMLoadError: 모델 로드 실패 시 (MLX)
        ValueError: 지원하지 않는 backend 값
    """
    backend_type = getattr(config, "backend", "ollama")

    if backend_type == "mlx":
        from core.mlx_client import MLXBackend
        logger.info("MLX 백엔드 선택됨")
        return MLXBackend(config)

    if backend_type == "ollama":
        logger.info("Ollama 백엔드 선택됨")
        return OllamaBackend(config)

    raise ValueError(
        f"지원하지 않는 LLM 백엔드입니다: '{backend_type}'. "
        f"'ollama' 또는 'mlx'를 사용하세요."
    )
