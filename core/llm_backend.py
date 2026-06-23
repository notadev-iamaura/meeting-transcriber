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
import queue
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol, TypeVar, cast, runtime_checkable

logger = logging.getLogger(__name__)

T = TypeVar("T")


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
        temperature: float | None = None,
        num_ctx: int | None = None,
        max_tokens: int | None = None,
        timeout: int | None = None,
    ) -> str:
        """LLM에 메시지를 보내고 전체 응답 텍스트를 반환한다.

        Args:
            messages: 대화 메시지 목록 (role, content 쌍)
            temperature: 생성 온도 (None이면 기본값 사용)
            num_ctx: 컨텍스트 윈도우 크기 (None이면 기본값 사용)
            max_tokens: 응답 생성 토큰 상한 (None이면 기본값 사용)
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
        temperature: float | None = None,
        num_ctx: int | None = None,
        max_tokens: int | None = None,
        timeout: int | None = None,
    ) -> Iterator[str]:
        """LLM에 메시지를 보내고 토큰을 스트리밍으로 반환한다.

        Args:
            messages: 대화 메시지 목록 (role, content 쌍)
            temperature: 생성 온도 (None이면 기본값 사용)
            num_ctx: 컨텍스트 윈도우 크기 (None이면 기본값 사용)
            max_tokens: 응답 생성 토큰 상한 (None이면 기본값 사용)
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

        logger.info(f"OllamaBackend 초기화: model={self._model}, host={self._host}")

    def chat(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        num_ctx: int | None = None,
        max_tokens: int | None = None,
        timeout: int | None = None,
    ) -> str:
        """Ollama /api/chat 엔드포인트를 호출하여 응답을 반환한다.

        Args:
            messages: 대화 메시지 목록
            temperature: 생성 온도 (None이면 초기화 시 설정값 사용)
            num_ctx: 컨텍스트 윈도우 크기
            max_tokens: 응답 생성 토큰 상한
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
            max_tokens=max_tokens,
            timeout=timeout if timeout is not None else self._timeout,
        )

    def chat_stream(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        num_ctx: int | None = None,
        max_tokens: int | None = None,
        timeout: int | None = None,
    ) -> Iterator[str]:
        """Ollama /api/chat 엔드포인트를 스트리밍 모드로 호출한다.

        Args:
            messages: 대화 메시지 목록
            temperature: 생성 온도
            num_ctx: 컨텍스트 윈도우 크기
            max_tokens: 응답 생성 토큰 상한
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
            max_tokens=max_tokens,
            timeout=timeout if timeout is not None else self._timeout,
        )

    def cleanup(self) -> None:
        """Ollama는 독립 프로세스이므로 정리할 리소스가 없다."""
        pass


class ThreadBoundLLMBackend:
    """LLM 백엔드를 단일 전용 스레드에 고정해 실행한다.

    MLX-VLM은 모델 로드와 첫 생성 호출이 서로 다른 thread에서 일어나면
    Metal stream을 찾지 못하는 경우가 있다. 이 wrapper는 백엔드 생성,
    chat/chat_stream, cleanup을 모두 같은 worker thread에서 실행해 그 경계를
    명확하게 고정한다.
    """

    def __init__(
        self,
        backend_factory: Callable[[], LLMBackend],
        *,
        thread_name_prefix: str = "llm-backend",
    ) -> None:
        """전용 worker thread에서 실제 backend를 생성한다."""
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=thread_name_prefix,
        )
        self._worker_thread_id: int | None = None
        self._backend: LLMBackend | None = None
        self._closed = False

        try:
            self._backend = self._executor.submit(
                self._create_backend,
                backend_factory,
            ).result()
        except BaseException:
            self._closed = True
            self._executor.shutdown(wait=True, cancel_futures=True)
            raise

    def _create_backend(self, backend_factory: Callable[[], LLMBackend]) -> LLMBackend:
        """worker thread에서 실제 backend를 생성하고 thread id를 기록한다."""
        self._worker_thread_id = threading.get_ident()
        return backend_factory()

    def _require_backend(self) -> LLMBackend:
        """정리되지 않은 실제 backend를 반환한다."""
        if self._closed or self._backend is None:
            raise LLMGenerationError("LLM backend가 이미 정리되었습니다")
        return self._backend

    def _run_on_worker(self, func: Callable[[LLMBackend], T]) -> T:
        """func를 backend 전용 worker thread에서 실행한다."""
        if threading.get_ident() == self._worker_thread_id:
            return func(self._require_backend())
        return self._executor.submit(lambda: func(self._require_backend())).result()

    def chat(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        num_ctx: int | None = None,
        max_tokens: int | None = None,
        timeout: int | None = None,
    ) -> str:
        """전용 worker thread에서 동기 chat 호출을 실행한다."""
        return self._run_on_worker(
            lambda backend: backend.chat(
                messages=messages,
                temperature=temperature,
                num_ctx=num_ctx,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        )

    def chat_stream(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        num_ctx: int | None = None,
        max_tokens: int | None = None,
        timeout: int | None = None,
    ) -> Iterator[str]:
        """전용 worker thread에서 streaming 호출을 실행하고 token을 전달한다."""
        if threading.get_ident() == self._worker_thread_id:
            yield from self._require_backend().chat_stream(
                messages=messages,
                temperature=temperature,
                num_ctx=num_ctx,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            return

        events: queue.Queue[tuple[str, str | BaseException | None]] = queue.Queue()

        def produce() -> None:
            try:
                backend = self._require_backend()
                for token in backend.chat_stream(
                    messages=messages,
                    temperature=temperature,
                    num_ctx=num_ctx,
                    max_tokens=max_tokens,
                    timeout=timeout,
                ):
                    events.put(("token", token))
            except BaseException as exc:
                events.put(("error", exc))
            else:
                events.put(("done", None))

        future = self._executor.submit(produce)

        while True:
            kind, payload = events.get()
            if kind == "token":
                yield cast(str, payload)
                continue
            if kind == "error":
                future.result()
                raise cast(BaseException, payload)
            future.result()
            return

    def cleanup(self) -> None:
        """전용 worker thread에서 backend cleanup을 실행하고 executor를 종료한다."""
        if self._closed:
            return

        called_from_worker = threading.get_ident() == self._worker_thread_id

        def cleanup_backend() -> None:
            backend = self._backend
            if backend is not None:
                backend.cleanup()
            self._backend = None

        try:
            if called_from_worker:
                cleanup_backend()
            else:
                self._executor.submit(cleanup_backend).result()
        finally:
            self._closed = True
            self._executor.shutdown(wait=not called_from_worker, cancel_futures=True)


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
        return ThreadBoundLLMBackend(
            lambda: MLXBackend(config),
            thread_name_prefix="mlx-llm",
        )

    if backend_type == "ollama":
        logger.info("Ollama 백엔드 선택됨")
        return OllamaBackend(config)

    raise ValueError(
        f"지원하지 않는 LLM 백엔드입니다: '{backend_type}'. 'ollama' 또는 'mlx'를 사용하세요."
    )
