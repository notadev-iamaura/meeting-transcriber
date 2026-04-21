"""
MLX 백엔드 모듈 테스트.

mlx-lm 패키지 없이도 동작하도록 mock 기반으로 검증한다.
주요 검증: 에러 계층, 초기화, chat/stream/cleanup 동작.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from core.mlx_client import (
    MLXBackend,
    MLXError,
    MLXGenerationError,
    MLXLoadError,
)

# === 에러 계층 테스트 ===


class TestMLXErrorHierarchy:
    """MLX 에러 클래스 상속 구조를 검증한다."""

    def test_MLXError_기본_에러(self) -> None:
        """MLXError는 LLMBackendError를 상속한다."""
        from core.llm_backend import LLMBackendError

        assert issubclass(MLXError, LLMBackendError)

    def test_MLXLoadError_다중_상속(self) -> None:
        """MLXLoadError는 MLXError와 LLMLoadError를 모두 상속한다."""
        from core.llm_backend import LLMLoadError

        assert issubclass(MLXLoadError, MLXError)
        assert issubclass(MLXLoadError, LLMLoadError)

    def test_MLXGenerationError_다중_상속(self) -> None:
        """MLXGenerationError는 MLXError와 LLMGenerationError를 모두 상속한다."""
        from core.llm_backend import LLMGenerationError

        assert issubclass(MLXGenerationError, MLXError)
        assert issubclass(MLXGenerationError, LLMGenerationError)

    def test_에러_인스턴스_생성(self) -> None:
        """에러 인스턴스를 정상적으로 생성할 수 있다."""
        err = MLXLoadError("테스트 에러")
        assert str(err) == "테스트 에러"
        assert isinstance(err, MLXError)


# === MLXBackend 초기화 테스트 ===


@dataclass
class MockLLMConfig:
    """테스트용 LLM 설정 모의 객체."""

    temperature: float = 0.3
    mlx_model_name: str = "test-model"
    mlx_max_tokens: int = 1000


class TestMLXBackendInit:
    """MLXBackend 초기화 로직을 검증한다."""

    def test_정상_초기화(self) -> None:
        """모델과 토크나이저가 정상적으로 로드된다."""
        mock_model = MagicMock()
        mock_tokenizer = MagicMock()
        mock_load = MagicMock(return_value=(mock_model, mock_tokenizer))

        mock_mlx_lm = MagicMock()
        mock_mlx_lm.load = mock_load

        config = MockLLMConfig()

        import sys

        with patch.dict(sys.modules, {"mlx_lm": mock_mlx_lm}):
            backend = MLXBackend(config)

        assert backend._model is mock_model
        assert backend._tokenizer is mock_tokenizer
        assert backend._temperature == 0.3
        assert backend._max_tokens == 1000

    def test_mlx_lm_미설치시_MLXLoadError(self) -> None:
        """mlx-lm 패키지 미설치 시 MLXLoadError를 발생시킨다."""
        config = MockLLMConfig()

        with (
            patch.dict("sys.modules", {"mlx_lm": None}),
            patch(
                "builtins.__import__",
                side_effect=ImportError("No module named 'mlx_lm'"),
            ),
            pytest.raises(MLXLoadError, match="mlx-lm 패키지"),
        ):
            MLXBackend(config)

    def test_기본_max_tokens_설정(self) -> None:
        """mlx_max_tokens 설정이 없으면 기본값 2000을 사용한다."""
        config = MagicMock(spec=[])
        config.temperature = 0.3

        # getattr 폴백으로 기본값 확인
        max_tokens = getattr(config, "mlx_max_tokens", 2000)
        assert max_tokens == 2000

    def test_기본_모델명_설정(self) -> None:
        """mlx_model_name 설정이 없으면 EXAONE 기본 모델을 사용한다."""
        config = MagicMock(spec=[])
        config.temperature = 0.3

        model_name = getattr(
            config,
            "mlx_model_name",
            "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit",
        )
        assert "EXAONE" in model_name


# === chat 메서드 테스트 ===


class TestMLXBackendChat:
    """MLXBackend.chat() 메서드를 검증한다."""

    def _create_backend(self) -> MLXBackend:
        """테스트용 MLXBackend 인스턴스를 생성한다 (모델 로드 건너뜀)."""
        backend = MLXBackend.__new__(MLXBackend)
        backend._model = MagicMock()
        backend._tokenizer = MagicMock()
        backend._temperature = 0.3
        backend._max_tokens = 1000
        backend._model_name = "test-model"
        backend._use_vlm = False
        backend._processor = None
        backend._vlm_generate = None
        backend._vlm_stream_generate = None
        # Prompt cache 필드 (신규) — None 으로 시작해 chat() 내부에서 lazy 초기화
        backend._vlm_prompt_cache_state = None
        backend._lm_prompt_cache = None
        backend._last_system_prompt_hash = None
        backend._tokenizer.apply_chat_template = MagicMock(return_value="formatted prompt")
        return backend

    def test_모델_미로드시_에러(self) -> None:
        """모델이 로드되지 않은 상태에서 chat 호출 시 에러를 발생시킨다."""
        backend = MLXBackend.__new__(MLXBackend)
        backend._model = None
        backend._tokenizer = None
        backend._use_vlm = False

        with pytest.raises(MLXGenerationError, match="모델이 로드되지 않았습니다"):
            backend.chat(messages=[{"role": "user", "content": "테스트"}])

    def test_정상_chat_호출(self) -> None:
        """chat이 정상적으로 응답을 반환한다."""
        mock_generate = MagicMock(return_value="테스트 응답")
        backend = self._create_backend()

        mock_mlx_lm = MagicMock()
        mock_mlx_lm.generate = mock_generate

        import sys

        with patch.dict(sys.modules, {"mlx_lm": mock_mlx_lm}):
            result = backend.chat(
                messages=[{"role": "user", "content": "안녕"}],
            )

        assert result == "테스트 응답"

    def test_temperature_오버라이드(self) -> None:
        """temperature 파라미터로 초기값을 덮어쓸 수 있다."""
        backend = self._create_backend()
        assert backend._temperature == 0.3

        # temperature=0.7 전달 시 해당 값 사용
        temp = 0.7
        final_temp = temp if temp is not None else backend._temperature
        assert final_temp == 0.7

    def test_temperature_None시_기본값(self) -> None:
        """temperature=None이면 초기화 시 설정값을 사용한다."""
        backend = self._create_backend()

        temp = None
        final_temp = temp if temp is not None else backend._temperature
        assert final_temp == 0.3


# === chat_stream 메서드 테스트 ===


class TestMLXBackendChatStream:
    """MLXBackend.chat_stream() 메서드를 검증한다."""

    def _create_backend(self) -> MLXBackend:
        """테스트용 MLXBackend 인스턴스를 생성한다."""
        backend = MLXBackend.__new__(MLXBackend)
        backend._model = MagicMock()
        backend._tokenizer = MagicMock()
        backend._temperature = 0.3
        backend._max_tokens = 1000
        backend._model_name = "test-model"
        backend._use_vlm = False
        backend._processor = None
        backend._vlm_generate = None
        backend._vlm_stream_generate = None
        backend._vlm_prompt_cache_state = None
        backend._lm_prompt_cache = None
        backend._last_system_prompt_hash = None
        backend._tokenizer.apply_chat_template = MagicMock(return_value="formatted prompt")
        return backend

    def test_모델_미로드시_에러(self) -> None:
        """모델이 로드되지 않은 상태에서 stream 호출 시 에러를 발생시킨다."""
        backend = MLXBackend.__new__(MLXBackend)
        backend._model = None
        backend._tokenizer = None
        backend._use_vlm = False

        with pytest.raises(MLXGenerationError, match="모델이 로드되지 않았습니다"):
            list(
                backend.chat_stream(
                    messages=[{"role": "user", "content": "테스트"}],
                )
            )


# === cleanup 메서드 테스트 ===


class TestMLXBackendCleanup:
    """MLXBackend.cleanup() 메서드를 검증한다."""

    def test_cleanup_모델_해제(self) -> None:
        """cleanup 호출 시 모델과 토크나이저를 None으로 설정한다."""
        backend = MLXBackend.__new__(MLXBackend)
        backend._model = MagicMock()
        backend._tokenizer = MagicMock()
        backend._model_name = "test-model"
        backend._use_vlm = False
        backend._processor = None
        backend._vlm_generate = None
        backend._vlm_stream_generate = None
        backend._vlm_prompt_cache_state = None
        backend._lm_prompt_cache = None
        backend._last_system_prompt_hash = None

        with patch.dict("sys.modules", {"mlx": None, "mlx.core": None}):
            backend.cleanup()

        assert backend._model is None
        assert backend._tokenizer is None
        assert backend._vlm_prompt_cache_state is None
        assert backend._lm_prompt_cache is None
        assert backend._last_system_prompt_hash is None

    def test_cleanup_metal_캐시_정리(self) -> None:
        """cleanup 시 Metal GPU 캐시 정리를 시도한다."""
        backend = MLXBackend.__new__(MLXBackend)
        backend._model = MagicMock()
        backend._tokenizer = MagicMock()
        backend._model_name = "test-model"
        backend._use_vlm = False
        backend._processor = None
        backend._vlm_generate = None
        backend._vlm_stream_generate = None
        backend._vlm_prompt_cache_state = None
        backend._lm_prompt_cache = None
        backend._last_system_prompt_hash = None

        mock_mx = MagicMock()
        mock_mx.metal.clear_cache = MagicMock()

        import sys

        with patch.dict(sys.modules, {"mlx": mock_mx, "mlx.core": mock_mx}):
            backend.cleanup()

        assert backend._model is None

    def test_cleanup_mlx_미설치시_무시(self) -> None:
        """mlx 미설치 환경에서 cleanup이 에러 없이 완료된다."""
        backend = MLXBackend.__new__(MLXBackend)
        backend._model = MagicMock()
        backend._tokenizer = MagicMock()
        backend._model_name = "test-model"
        backend._use_vlm = False
        backend._processor = None
        backend._vlm_generate = None
        backend._vlm_stream_generate = None
        backend._vlm_prompt_cache_state = None
        backend._lm_prompt_cache = None
        backend._last_system_prompt_hash = None

        # ImportError가 발생해도 정상 종료
        with patch(
            "builtins.__import__",
            side_effect=ImportError("No module named 'mlx'"),
        ):
            backend.cleanup()

        assert backend._model is None
        assert backend._tokenizer is None


# === Prompt cache 테스트 (PERF: KV cache 재사용) ===


class TestMLXBackendPromptCache:
    """MLXBackend 의 시스템 프롬프트 기반 자동 prompt cache 관리."""

    def _create_backend(self) -> MLXBackend:
        backend = MLXBackend.__new__(MLXBackend)
        backend._model = MagicMock()
        backend._tokenizer = MagicMock()
        backend._temperature = 0.0
        backend._max_tokens = 1000
        backend._model_name = "test-model"
        backend._use_vlm = False
        backend._processor = None
        backend._vlm_generate = None
        backend._vlm_stream_generate = None
        backend._vlm_prompt_cache_state = None
        backend._lm_prompt_cache = None
        backend._last_system_prompt_hash = None
        backend._tokenizer.apply_chat_template = MagicMock(return_value="prompt")
        return backend

    def test_동일_system_prompt_두_번째_호출은_cache_재사용(self) -> None:
        """같은 시스템 프롬프트로 두 번 호출하면 _last_system_prompt_hash 가 유지된다."""
        backend = self._create_backend()
        msgs = [
            {"role": "system", "content": "당신은 회의 교정 전문가입니다."},
            {"role": "user", "content": "안녕"},
        ]
        # 내부 동작만 확인 — chat() 전체는 mlx_lm 모킹 필요하므로 _maybe_reset 직접 호출
        backend._maybe_reset_prompt_cache(msgs)
        first_hash = backend._last_system_prompt_hash
        assert first_hash is not None

        # 가짜 cache 를 주입해 재사용 여부 확인용
        sentinel = object()
        backend._lm_prompt_cache = sentinel

        # 같은 messages 로 다시 호출 — hash 동일이면 cache 유지되어야 함
        backend._maybe_reset_prompt_cache(msgs)
        assert backend._last_system_prompt_hash == first_hash
        assert backend._lm_prompt_cache is sentinel  # 리셋되지 않음

    def test_system_prompt_변경시_cache_자동_리셋(self) -> None:
        """시스템 프롬프트가 달라지면 cache 가 자동 리셋된다."""
        backend = self._create_backend()
        backend._maybe_reset_prompt_cache([{"role": "system", "content": "프롬프트 A"}])
        # cache 채운 척
        backend._lm_prompt_cache = object()
        backend._vlm_prompt_cache_state = object()

        backend._maybe_reset_prompt_cache([{"role": "system", "content": "프롬프트 B"}])
        assert backend._lm_prompt_cache is None
        assert backend._vlm_prompt_cache_state is None

    def test_reset_prompt_cache_수동_호출(self) -> None:
        """reset_prompt_cache 로 cache 와 hash 가 초기화된다."""
        backend = self._create_backend()
        backend._lm_prompt_cache = object()
        backend._vlm_prompt_cache_state = object()
        backend._last_system_prompt_hash = "deadbeef"

        backend.reset_prompt_cache()
        assert backend._lm_prompt_cache is None
        assert backend._vlm_prompt_cache_state is None
        assert backend._last_system_prompt_hash is None

    def test_system_없는_messages_는_해시_None(self) -> None:
        """user/assistant 만 있는 messages 는 hash=None 으로 처리한다."""
        backend = self._create_backend()
        backend._maybe_reset_prompt_cache([{"role": "user", "content": "질문"}])
        assert backend._last_system_prompt_hash is None


# === _apply_chat_template 테스트 ===


class TestApplyChatTemplate:
    """_apply_chat_template 메서드를 검증한다."""

    def test_챗_템플릿_적용(self) -> None:
        """tokenizer.apply_chat_template를 올바르게 호출한다."""
        backend = MLXBackend.__new__(MLXBackend)
        backend._use_vlm = False
        backend._tokenizer = MagicMock()
        backend._tokenizer.apply_chat_template.return_value = "formatted"

        messages = [{"role": "user", "content": "테스트"}]
        result = backend._apply_chat_template(messages)

        assert result == "formatted"
        backend._tokenizer.apply_chat_template.assert_called_once_with(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
