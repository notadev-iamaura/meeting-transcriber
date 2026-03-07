"""
LLM 백엔드 추상화 테스트 모듈 (LLM Backend Abstraction Tests)

목적: core/llm_backend.py의 에러 계층, OllamaBackend, 팩토리 함수를 검증한다.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.llm_backend import (
    LLMBackend,
    LLMBackendError,
    LLMConnectionError,
    LLMGenerationError,
    LLMLoadError,
    OllamaBackend,
    create_backend,
)
from core.ollama_client import (
    OllamaConnectionError,
    OllamaError,
    OllamaResponseError,
    OllamaTimeoutError,
)

# === 에러 계층 테스트 ===


class TestLLMBackendErrorHierarchy:
    """LLM 백엔드 에러 계층 검증."""

    def test_LLMConnectionError는_LLMBackendError_하위(self) -> None:
        assert issubclass(LLMConnectionError, LLMBackendError)

    def test_LLMGenerationError는_LLMBackendError_하위(self) -> None:
        assert issubclass(LLMGenerationError, LLMBackendError)

    def test_LLMLoadError는_LLMBackendError_하위(self) -> None:
        assert issubclass(LLMLoadError, LLMBackendError)

    def test_OllamaError는_LLMBackendError_하위(self) -> None:
        """Ollama 에러가 LLMBackendError를 상속하는지 검증."""
        assert issubclass(OllamaError, LLMBackendError)

    def test_OllamaConnectionError는_LLMConnectionError_하위(self) -> None:
        """Ollama 연결 에러가 LLMConnectionError를 상속하는지 검증."""
        assert issubclass(OllamaConnectionError, LLMConnectionError)
        assert issubclass(OllamaConnectionError, OllamaError)

    def test_OllamaTimeoutError는_LLMGenerationError_하위(self) -> None:
        """Ollama 타임아웃 에러가 LLMGenerationError를 상속하는지 검증."""
        assert issubclass(OllamaTimeoutError, LLMGenerationError)
        assert issubclass(OllamaTimeoutError, OllamaError)

    def test_OllamaResponseError는_LLMGenerationError_하위(self) -> None:
        """Ollama 응답 에러가 LLMGenerationError를 상속하는지 검증."""
        assert issubclass(OllamaResponseError, LLMGenerationError)
        assert issubclass(OllamaResponseError, OllamaError)

    def test_LLMBackendError로_OllamaConnectionError_잡기(self) -> None:
        """소비자 코드에서 LLMBackendError로 모든 Ollama 에러를 잡을 수 있는지 검증."""
        with pytest.raises(LLMBackendError):
            raise OllamaConnectionError("테스트")

    def test_LLMConnectionError로_OllamaConnectionError_잡기(self) -> None:
        """소비자 코드에서 LLMConnectionError로 연결 에러를 잡을 수 있는지 검증."""
        with pytest.raises(LLMConnectionError):
            raise OllamaConnectionError("테스트")

    def test_LLMGenerationError로_OllamaTimeoutError_잡기(self) -> None:
        """소비자 코드에서 LLMGenerationError로 타임아웃을 잡을 수 있는지 검증."""
        with pytest.raises(LLMGenerationError):
            raise OllamaTimeoutError("테스트")


# === OllamaBackend 테스트 ===


class TestOllamaBackend:
    """OllamaBackend 클래스 검증."""

    def _make_config(self) -> MagicMock:
        """테스트용 LLMConfig Mock을 생성한다."""
        config = MagicMock()
        config.host = "http://127.0.0.1:11434"
        config.model_name = "exaone3.5:7.8b-instruct-q4_K_M"
        config.temperature = 0.3
        config.max_context_tokens = 8192
        config.request_timeout_seconds = 120
        config.backend = "ollama"
        return config

    @patch("core.ollama_client.check_connection")
    def test_초기화_성공(self, mock_check: MagicMock) -> None:
        """Ollama 서버 연결 확인 후 초기화 성공."""
        config = self._make_config()
        backend = OllamaBackend(config)

        mock_check.assert_called_once_with("http://127.0.0.1:11434")
        assert backend._host == "http://127.0.0.1:11434"
        assert backend._model == "exaone3.5:7.8b-instruct-q4_K_M"

    @patch("core.ollama_client.check_connection")
    def test_초기화_연결_실패(self, mock_check: MagicMock) -> None:
        """Ollama 서버 연결 실패 시 예외 전파."""
        mock_check.side_effect = OllamaConnectionError("연결 불가")
        config = self._make_config()

        with pytest.raises(OllamaConnectionError, match="연결 불가"):
            OllamaBackend(config)

    @patch("core.ollama_client.check_connection")
    @patch("core.ollama_client.chat")
    def test_chat_호출(
        self,
        mock_chat: MagicMock,
        mock_check: MagicMock,
    ) -> None:
        """chat() 메서드가 ollama_client.chat()을 올바르게 위임하는지 검증."""
        mock_chat.return_value = "응답 텍스트"
        config = self._make_config()
        backend = OllamaBackend(config)

        messages = [{"role": "user", "content": "안녕"}]
        result = backend.chat(messages=messages)

        assert result == "응답 텍스트"
        mock_chat.assert_called_once_with(
            host="http://127.0.0.1:11434",
            model="exaone3.5:7.8b-instruct-q4_K_M",
            messages=messages,
            temperature=0.3,
            num_ctx=8192,
            timeout=120,
        )

    @patch("core.ollama_client.check_connection")
    @patch("core.ollama_client.chat")
    def test_chat_온도_오버라이드(
        self,
        mock_chat: MagicMock,
        mock_check: MagicMock,
    ) -> None:
        """chat() 호출 시 temperature 오버라이드 검증."""
        mock_chat.return_value = "응답"
        config = self._make_config()
        backend = OllamaBackend(config)

        backend.chat(messages=[{"role": "user", "content": "테스트"}], temperature=0.7)

        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs["temperature"] == 0.7

    @patch("core.ollama_client.check_connection")
    @patch("core.ollama_client.chat_stream")
    def test_chat_stream_호출(
        self,
        mock_stream: MagicMock,
        mock_check: MagicMock,
    ) -> None:
        """chat_stream() 메서드가 올바르게 토큰을 yield하는지 검증."""
        mock_stream.return_value = iter(["토큰1", "토큰2", "토큰3"])
        config = self._make_config()
        backend = OllamaBackend(config)

        messages = [{"role": "user", "content": "안녕"}]
        tokens = list(backend.chat_stream(messages=messages))

        assert tokens == ["토큰1", "토큰2", "토큰3"]

    @patch("core.ollama_client.check_connection")
    def test_cleanup은_no_op(self, mock_check: MagicMock) -> None:
        """Ollama cleanup()은 아무 동작도 하지 않아야 한다."""
        config = self._make_config()
        backend = OllamaBackend(config)
        # 예외 없이 호출 가능
        backend.cleanup()

    @patch("core.ollama_client.check_connection")
    def test_LLMBackend_프로토콜_준수(self, mock_check: MagicMock) -> None:
        """OllamaBackend가 LLMBackend 프로토콜을 구현하는지 검증."""
        config = self._make_config()
        backend = OllamaBackend(config)
        assert isinstance(backend, LLMBackend)


# === MLXBackend 테스트 ===


class TestMLXBackend:
    """MLXBackend 클래스 검증 (mlx_lm Mock 사용)."""

    def _make_config(self) -> MagicMock:
        """테스트용 LLMConfig Mock을 생성한다."""
        config = MagicMock()
        config.backend = "mlx"
        config.mlx_model_name = "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit"
        config.mlx_max_tokens = 2000
        config.temperature = 0.3
        return config

    def _make_mock_mlx_lm(self) -> MagicMock:
        """mlx_lm 모듈을 Mock으로 생성한다."""
        mock_module = MagicMock()
        return mock_module

    def test_초기화_성공(self) -> None:
        """MLX 모델 로드 성공 검증."""
        mock_model = MagicMock()
        mock_tokenizer = MagicMock()
        mock_mlx_lm = self._make_mock_mlx_lm()
        mock_mlx_lm.load.return_value = (mock_model, mock_tokenizer)

        with patch.dict("sys.modules", {"mlx_lm": mock_mlx_lm}):
            from core.mlx_client import MLXBackend

            config = self._make_config()
            backend = MLXBackend(config)

            mock_mlx_lm.load.assert_called_once_with(
                "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit",
                tokenizer_config={"trust_remote_code": True},
            )
            assert backend._model is mock_model
            assert backend._tokenizer is mock_tokenizer

    def test_초기화_mlx_미설치(self) -> None:
        """mlx-lm 미설치 시 MLXLoadError 발생 검증."""
        from core.mlx_client import MLXLoadError

        config = self._make_config()

        with (
            patch.dict("sys.modules", {"mlx_lm": None}),
            pytest.raises(MLXLoadError, match="mlx-lm"),
        ):
            from core.mlx_client import MLXBackend

            MLXBackend(config)

    def test_chat_호출(self) -> None:
        """chat() 메서드가 generate()를 올바르게 호출하는지 검증."""
        mock_model = MagicMock()
        mock_tokenizer = MagicMock()
        mock_tokenizer.apply_chat_template.return_value = "formatted prompt"
        mock_mlx_lm = self._make_mock_mlx_lm()
        mock_mlx_lm.load.return_value = (mock_model, mock_tokenizer)
        mock_mlx_lm.generate.return_value = "생성된 응답"

        with patch.dict("sys.modules", {"mlx_lm": mock_mlx_lm}):
            from core.mlx_client import MLXBackend

            config = self._make_config()
            backend = MLXBackend(config)

            result = backend.chat(messages=[{"role": "user", "content": "테스트"}])

            assert result == "생성된 응답"
            mock_tokenizer.apply_chat_template.assert_called_once()
            mock_mlx_lm.generate.assert_called_once()

    def test_cleanup_모델_해제(self) -> None:
        """cleanup() 호출 시 모델 참조가 제거되는지 검증."""
        mock_mlx_lm = self._make_mock_mlx_lm()
        mock_mlx_lm.load.return_value = (MagicMock(), MagicMock())

        with patch.dict("sys.modules", {"mlx_lm": mock_mlx_lm}):
            from core.mlx_client import MLXBackend

            config = self._make_config()
            backend = MLXBackend(config)

            assert backend._model is not None
            backend.cleanup()
            assert backend._model is None
            assert backend._tokenizer is None

    def test_LLMBackend_프로토콜_준수(self) -> None:
        """MLXBackend가 LLMBackend 프로토콜을 구현하는지 검증."""
        mock_mlx_lm = self._make_mock_mlx_lm()
        mock_mlx_lm.load.return_value = (MagicMock(), MagicMock())

        with patch.dict("sys.modules", {"mlx_lm": mock_mlx_lm}):
            from core.mlx_client import MLXBackend

            config = self._make_config()
            backend = MLXBackend(config)
            assert isinstance(backend, LLMBackend)


# === 팩토리 함수 테스트 ===


class TestCreateBackend:
    """create_backend() 팩토리 함수 검증."""

    @patch("core.ollama_client.check_connection")
    def test_ollama_백엔드_생성(self, mock_check: MagicMock) -> None:
        """backend='ollama' 시 OllamaBackend 생성."""
        config = MagicMock()
        config.backend = "ollama"
        config.host = "http://127.0.0.1:11434"
        config.model_name = "test-model"
        config.temperature = 0.3
        config.max_context_tokens = 8192
        config.request_timeout_seconds = 120

        backend = create_backend(config)
        assert isinstance(backend, OllamaBackend)

    def test_mlx_백엔드_생성(self) -> None:
        """backend='mlx' 시 MLXBackend 생성."""
        mock_mlx_lm = MagicMock()
        mock_mlx_lm.load.return_value = (MagicMock(), MagicMock())

        config = MagicMock()
        config.backend = "mlx"
        config.mlx_model_name = "test-model"
        config.mlx_max_tokens = 2000
        config.temperature = 0.3

        with patch.dict("sys.modules", {"mlx_lm": mock_mlx_lm}):
            from core.mlx_client import MLXBackend

            backend = create_backend(config)
            assert isinstance(backend, MLXBackend)

    def test_지원하지_않는_백엔드(self) -> None:
        """미지원 backend 값 시 ValueError 발생."""
        config = MagicMock()
        config.backend = "unknown"

        with pytest.raises(ValueError, match="지원하지 않는"):
            create_backend(config)

    @patch("core.ollama_client.check_connection")
    def test_backend_필드_없으면_ollama_기본값(
        self,
        mock_check: MagicMock,
    ) -> None:
        """backend 속성이 없으면 기본값 'ollama' 사용."""
        config = MagicMock(spec=[])  # backend 속성 없음
        config.host = "http://127.0.0.1:11434"
        config.model_name = "test-model"
        config.temperature = 0.3
        config.max_context_tokens = 8192
        config.request_timeout_seconds = 120

        backend = create_backend(config)
        assert isinstance(backend, OllamaBackend)
