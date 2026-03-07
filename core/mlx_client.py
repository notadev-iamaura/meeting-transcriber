"""
MLX 백엔드 모듈 (MLX Backend Module)

목적: Apple Silicon에서 mlx-lm 라이브러리를 사용하여 LLM 추론을 수행한다.
주요 기능:
    - MLXBackend 클래스 (LLMBackend 프로토콜 구현)
    - mlx_lm.load()로 모델 로드 (in-process, 통합 메모리 활용)
    - mlx_lm.generate()로 텍스트 생성
    - mlx_lm.stream_generate()로 스트리밍 생성
    - cleanup()으로 모델 메모리 해제 + Metal 캐시 정리
의존성: mlx-lm (선택적), core/llm_backend 모듈
"""

from __future__ import annotations

import gc
import logging
from collections.abc import Iterator
from typing import Any

from core.llm_backend import (
    LLMBackendError,
    LLMGenerationError,
    LLMLoadError,
)

logger = logging.getLogger(__name__)


# === MLX 전용 에러 계층 ===


class MLXError(LLMBackendError):
    """MLX 관련 에러의 기본 클래스."""


class MLXLoadError(MLXError, LLMLoadError):
    """MLX 모델 로드 실패 시 발생한다."""


class MLXGenerationError(MLXError, LLMGenerationError):
    """MLX 텍스트 생성 실패 시 발생한다."""


# === MLX 백엔드 구현 ===


class MLXBackend:
    """Apple Silicon MLX 프레임워크 기반 LLM 백엔드.

    mlx-lm 라이브러리를 사용하여 모델을 in-process로 로드하고 추론을 수행한다.
    통합 메모리(Unified Memory)를 네이티브로 활용하여 Ollama 대비 10~30% 빠르다.

    주의사항:
        - mlx-lm 패키지가 설치되어 있어야 한다 (pip install mlx-lm)
        - Apple Silicon Mac에서만 동작한다
        - 모델이 in-process로 로드되므로 ~5GB 메모리를 사용한다
        - ModelLoadManager를 통해 관리해야 동시 로드를 방지할 수 있다
    """

    def __init__(self, config: Any) -> None:
        """MLXBackend를 초기화하고 모델을 메모리에 로드한다.

        Args:
            config: LLMConfig 인스턴스 (mlx_model_name, temperature 등)

        Raises:
            MLXLoadError: mlx-lm 미설치 또는 모델 로드 실패 시
        """
        self._model: Any = None
        self._tokenizer: Any = None
        self._temperature = config.temperature
        self._max_tokens = getattr(config, "mlx_max_tokens", 2000)
        self._model_name = getattr(
            config,
            "mlx_model_name",
            "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit",
        )

        try:
            from mlx_lm import load  # type: ignore[import-untyped]

            logger.info(f"MLX 모델 로드 시작: {self._model_name}")
            self._model, self._tokenizer = load(
                self._model_name,
                tokenizer_config={"trust_remote_code": True},
            )
            logger.info(f"MLX 모델 로드 완료: {self._model_name}")

        except ImportError as e:
            raise MLXLoadError(
                "mlx-lm 패키지가 설치되지 않았습니다. 'pip install mlx-lm' 으로 설치하세요."
            ) from e
        except Exception as e:
            raise MLXLoadError(f"MLX 모델 로드 실패: {self._model_name} — {e}") from e

    def _apply_chat_template(
        self,
        messages: list[dict[str, str]],
    ) -> str:
        """메시지 목록을 모델의 챗 템플릿으로 변환한다.

        tokenizer.apply_chat_template()을 사용하여
        system/user/assistant 역할에 맞는 프롬프트를 생성한다.

        Args:
            messages: 대화 메시지 목록 (role, content 쌍)

        Returns:
            모델 입력용 프롬프트 문자열
        """
        return self._tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )

    def chat(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        num_ctx: int | None = None,
        timeout: int | None = None,
    ) -> str:
        """MLX로 텍스트를 생성하여 전체 응답을 반환한다.

        mlx_lm.generate()를 사용하여 한 번에 전체 응답을 생성한다.
        num_ctx와 timeout은 MLX에서는 사용하지 않지만 인터페이스 호환을 위해 받는다.

        Args:
            messages: 대화 메시지 목록
            temperature: 생성 온도 (None이면 초기화 시 설정값 사용)
            num_ctx: 미사용 (인터페이스 호환용)
            timeout: 미사용 (인터페이스 호환용)

        Returns:
            LLM 응답 텍스트

        Raises:
            MLXGenerationError: 텍스트 생성 실패 시
        """
        if self._model is None or self._tokenizer is None:
            raise MLXGenerationError("MLX 모델이 로드되지 않았습니다")

        try:
            from mlx_lm import generate  # type: ignore[import-untyped]

            prompt = self._apply_chat_template(messages)
            temp = temperature if temperature is not None else self._temperature

            response = generate(
                self._model,
                self._tokenizer,
                prompt=prompt,
                max_tokens=self._max_tokens,
                temp=temp,
            )

            return response

        except MLXGenerationError:
            raise
        except Exception as e:
            raise MLXGenerationError(f"MLX 텍스트 생성 실패: {e}") from e

    def chat_stream(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        num_ctx: int | None = None,
        timeout: int | None = None,
    ) -> Iterator[str]:
        """MLX로 텍스트를 스트리밍 생성한다.

        mlx_lm.stream_generate()를 사용하여 토큰 단위로 응답을 yield한다.

        Args:
            messages: 대화 메시지 목록
            temperature: 생성 온도
            num_ctx: 미사용 (인터페이스 호환용)
            timeout: 미사용 (인터페이스 호환용)

        Yields:
            토큰 문자열

        Raises:
            MLXGenerationError: 텍스트 생성 실패 시
        """
        if self._model is None or self._tokenizer is None:
            raise MLXGenerationError("MLX 모델이 로드되지 않았습니다")

        try:
            from mlx_lm import stream_generate  # type: ignore[import-untyped]

            prompt = self._apply_chat_template(messages)
            temp = temperature if temperature is not None else self._temperature

            for response in stream_generate(
                self._model,
                self._tokenizer,
                prompt=prompt,
                max_tokens=self._max_tokens,
                temp=temp,
            ):
                # stream_generate는 GenerateStepOutput 객체를 반환
                # .text 속성에서 토큰 텍스트를 추출
                text = getattr(response, "text", str(response))
                if text:
                    yield text

        except MLXGenerationError:
            raise
        except Exception as e:
            raise MLXGenerationError(f"MLX 스트리밍 생성 실패: {e}") from e

    def cleanup(self) -> None:
        """MLX 모델을 메모리에서 해제하고 Metal GPU 캐시를 정리한다.

        ModelLoadManager의 _unload_current()에서 호출된다.
        ~5GB의 모델 메모리를 해제하여 다음 모델 로드를 위한 공간을 확보한다.
        """
        model_name = self._model_name
        logger.info(f"MLX 모델 정리 시작: {model_name}")

        self._model = None
        self._tokenizer = None

        gc.collect()

        try:
            import mlx.core as mx  # type: ignore[import-untyped]

            mx.metal.clear_cache()
            logger.debug("Metal GPU 캐시 정리 완료")
        except ImportError:
            logger.debug("mlx 미설치 — Metal 캐시 정리 건너뜀")
        except Exception as e:
            logger.warning(f"Metal 캐시 정리 중 오류 (무시): {e}")

        logger.info(f"MLX 모델 정리 완료: {model_name}")
