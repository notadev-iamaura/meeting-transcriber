"""
Ollama HTTP 클라이언트 통합 모듈 테스트 (Unified Ollama HTTP Client Test Module)

목적: core/ollama_client.py의 통합 에러 계층 및 HTTP 함수를 검증한다.
주요 테스트:
    - 에러 계층 구조 (OllamaError → OllamaConnectionError, OllamaTimeoutError, OllamaResponseError)
    - check_connection: 연결 성공/실패
    - chat: 정상 호출, JSON 파싱 실패, 타임아웃, 연결 실패, 빈 content
    - chat_stream: 정상 스트리밍, 타임아웃, 연결 실패
의존성: pytest
"""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from core.ollama_client import (
    OllamaConnectionError,
    OllamaError,
    OllamaResponseError,
    OllamaTimeoutError,
    chat,
    chat_stream,
    check_connection,
    clear_connection_cache,
)
from core.llm_backend import LLMBackendError, LLMConnectionError, LLMGenerationError


# === 헬퍼 함수 ===


def _make_ollama_response(content: str) -> bytes:
    """Ollama API 응답 JSON을 생성한다.

    Args:
        content: LLM 응답 텍스트

    Returns:
        JSON 인코딩된 바이트열
    """
    response = {
        "model": "test-model",
        "message": {
            "role": "assistant",
            "content": content,
        },
        "done": True,
    }
    return json.dumps(response).encode("utf-8")


def _make_mock_urlopen(response_bytes: bytes) -> MagicMock:
    """urllib.request.urlopen의 모킹 응답을 생성한다.

    Args:
        response_bytes: 응답 바이트열

    Returns:
        컨텍스트 매니저 프로토콜을 지원하는 MagicMock
    """
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read.return_value = response_bytes
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


def _make_stream_response(tokens: list[str]) -> MagicMock:
    """스트리밍 응답을 생성한다.

    Args:
        tokens: 토큰 문자열 리스트

    Returns:
        컨텍스트 매니저 프로토콜 + 이터레이터를 지원하는 MagicMock
    """
    lines = []
    for token in tokens:
        chunk = json.dumps({
            "message": {"content": token},
            "done": False,
        })
        lines.append(chunk.encode("utf-8"))
    # 마지막 done 청크
    done_chunk = json.dumps({
        "message": {"content": ""},
        "done": True,
    })
    lines.append(done_chunk.encode("utf-8"))

    mock_response = MagicMock()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_response.__iter__ = MagicMock(return_value=iter(lines))
    return mock_response


# === 에러 계층 테스트 ===


class TestOllamaErrorHierarchy:
    """에러 클래스 계층 구조 테스트."""

    def test_OllamaError_기본(self) -> None:
        """OllamaError는 Exception의 하위 클래스이다."""
        assert issubclass(OllamaError, Exception)

    def test_OllamaConnectionError_상속(self) -> None:
        """OllamaConnectionError는 OllamaError의 하위 클래스이다."""
        assert issubclass(OllamaConnectionError, OllamaError)

    def test_OllamaTimeoutError_상속(self) -> None:
        """OllamaTimeoutError는 OllamaError의 하위 클래스이다."""
        assert issubclass(OllamaTimeoutError, OllamaError)

    def test_OllamaResponseError_상속(self) -> None:
        """OllamaResponseError는 OllamaError의 하위 클래스이다."""
        assert issubclass(OllamaResponseError, OllamaError)

    def test_에러_메시지(self) -> None:
        """에러에 메시지가 올바르게 전달된다."""
        error = OllamaConnectionError("연결 실패")
        assert str(error) == "연결 실패"


# === check_connection 테스트 ===


class TestCheckConnection:
    """check_connection 함수 테스트."""

    def setup_method(self) -> None:
        """각 테스트 전 Ollama 연결 캐시를 초기화한다."""
        clear_connection_cache()

    def test_연결_성공(self) -> None:
        """서버 연결 성공 시 정상 반환한다."""
        tags_response = json.dumps({"models": []}).encode("utf-8")
        mock_resp = _make_mock_urlopen(tags_response)

        with patch("core.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            # 예외 없이 반환되면 성공
            check_connection("http://127.0.0.1:11434")

    def test_연결_실패(self) -> None:
        """서버 연결 실패 시 OllamaConnectionError를 발생한다."""
        with patch(
            "core.ollama_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            with pytest.raises(OllamaConnectionError, match="연결할 수 없습니다"):
                check_connection("http://127.0.0.1:99999")

    def test_비정상_상태코드(self) -> None:
        """200이 아닌 상태코드는 OllamaConnectionError를 발생한다."""
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("core.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(OllamaConnectionError, match="응답 오류"):
                check_connection("http://127.0.0.1:11434")


# === chat 테스트 ===


class TestChat:
    """chat 함수 테스트."""

    def test_정상_호출(self) -> None:
        """정상 호출 시 응답 텍스트를 반환한다."""
        response = _make_ollama_response("응답 텍스트")
        mock_resp = _make_mock_urlopen(response)

        with patch("core.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            result = chat(
                host="http://127.0.0.1:11434",
                model="test",
                messages=[{"role": "user", "content": "질문"}],
            )

        assert result == "응답 텍스트"

    def test_한국어_응답(self) -> None:
        """한국어 응답이 올바르게 반환된다."""
        response = _make_ollama_response("한국어 응답입니다")
        mock_resp = _make_mock_urlopen(response)

        with patch("core.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            result = chat(
                host="http://127.0.0.1:11434",
                model="test",
                messages=[{"role": "user", "content": "질문"}],
            )

        assert result == "한국어 응답입니다"

    def test_JSON_파싱_실패(self) -> None:
        """응답 JSON 파싱 실패 시 OllamaResponseError를 발생한다."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("core.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(OllamaResponseError, match="JSON 파싱 실패"):
                chat(
                    host="http://127.0.0.1:11434",
                    model="test",
                    messages=[{"role": "user", "content": "질문"}],
                )

    def test_빈_content(self) -> None:
        """응답에 content가 비어있으면 OllamaResponseError를 발생한다."""
        empty_response = json.dumps({
            "model": "test",
            "message": {"role": "assistant", "content": ""},
            "done": True,
        }).encode("utf-8")
        mock_resp = _make_mock_urlopen(empty_response)

        with patch("core.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(OllamaResponseError, match="content가 없습니다"):
                chat(
                    host="http://127.0.0.1:11434",
                    model="test",
                    messages=[{"role": "user", "content": "질문"}],
                )

    def test_연결_실패(self) -> None:
        """연결 실패 시 OllamaConnectionError를 발생한다."""
        with patch(
            "core.ollama_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            with pytest.raises(OllamaConnectionError):
                chat(
                    host="http://127.0.0.1:11434",
                    model="test",
                    messages=[{"role": "user", "content": "질문"}],
                )

    def test_타임아웃_URLError(self) -> None:
        """URLError(timeout) 시 OllamaTimeoutError를 발생한다."""
        with patch(
            "core.ollama_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("timed out"),
        ):
            with pytest.raises(OllamaTimeoutError):
                chat(
                    host="http://127.0.0.1:11434",
                    model="test",
                    messages=[{"role": "user", "content": "질문"}],
                )

    def test_타임아웃_TimeoutError(self) -> None:
        """TimeoutError 발생 시 OllamaTimeoutError를 발생한다."""
        with patch(
            "core.ollama_client.urllib.request.urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            with pytest.raises(OllamaTimeoutError):
                chat(
                    host="http://127.0.0.1:11434",
                    model="test",
                    messages=[{"role": "user", "content": "질문"}],
                )

    def test_옵션_전달(self) -> None:
        """temperature, num_ctx 옵션이 payload에 올바르게 전달된다."""
        response = _make_ollama_response("응답")
        mock_resp = _make_mock_urlopen(response)

        captured = {}

        def capture_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return mock_resp

        with patch("core.ollama_client.urllib.request.urlopen", side_effect=capture_urlopen):
            chat(
                host="http://127.0.0.1:11434",
                model="test-model",
                messages=[{"role": "user", "content": "질문"}],
                temperature=0.5,
                num_ctx=4096,
                timeout=60,
            )

        assert captured["body"]["model"] == "test-model"
        assert captured["body"]["options"]["temperature"] == 0.5
        assert captured["body"]["options"]["num_ctx"] == 4096
        assert captured["body"]["stream"] is False
        assert captured["timeout"] == 60


# === chat_stream 테스트 ===


class TestChatStream:
    """chat_stream 함수 테스트."""

    def test_정상_스트리밍(self) -> None:
        """정상 스트리밍 시 토큰들을 순서대로 반환한다."""
        tokens = ["안녕", "하세", "요"]
        mock_resp = _make_stream_response(tokens)

        with patch("core.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            result = list(chat_stream(
                host="http://127.0.0.1:11434",
                model="test",
                messages=[{"role": "user", "content": "질문"}],
            ))

        assert result == ["안녕", "하세", "요"]

    def test_빈_스트리밍(self) -> None:
        """done만 있는 스트리밍 응답은 빈 리스트를 반환한다."""
        done_chunk = json.dumps({
            "message": {"content": ""},
            "done": True,
        }).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.__iter__ = MagicMock(return_value=iter([done_chunk]))

        with patch("core.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            result = list(chat_stream(
                host="http://127.0.0.1:11434",
                model="test",
                messages=[{"role": "user", "content": "질문"}],
            ))

        assert result == []

    def test_연결_실패(self) -> None:
        """스트리밍 연결 실패 시 OllamaConnectionError를 발생한다."""
        with patch(
            "core.ollama_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            with pytest.raises(OllamaConnectionError):
                list(chat_stream(
                    host="http://127.0.0.1:11434",
                    model="test",
                    messages=[{"role": "user", "content": "질문"}],
                ))

    def test_타임아웃(self) -> None:
        """스트리밍 타임아웃 시 OllamaTimeoutError를 발생한다."""
        with patch(
            "core.ollama_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("timed out"),
        ):
            with pytest.raises(OllamaTimeoutError):
                list(chat_stream(
                    host="http://127.0.0.1:11434",
                    model="test",
                    messages=[{"role": "user", "content": "질문"}],
                ))

    def test_TimeoutError_직접(self) -> None:
        """TimeoutError 발생 시 OllamaTimeoutError로 변환한다."""
        with patch(
            "core.ollama_client.urllib.request.urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            with pytest.raises(OllamaTimeoutError):
                list(chat_stream(
                    host="http://127.0.0.1:11434",
                    model="test",
                    messages=[{"role": "user", "content": "질문"}],
                ))


class TestLLMBackendErrorIntegration:
    """Ollama 에러가 LLMBackendError 계층을 올바르게 상속하는지 검증."""

    def test_OllamaError는_LLMBackendError_하위(self) -> None:
        """OllamaError는 LLMBackendError의 하위 클래스이다."""
        assert issubclass(OllamaError, LLMBackendError)

    def test_OllamaConnectionError는_LLMConnectionError_하위(self) -> None:
        """OllamaConnectionError는 LLMConnectionError의 하위 클래스이다."""
        assert issubclass(OllamaConnectionError, LLMConnectionError)

    def test_OllamaTimeoutError는_LLMGenerationError_하위(self) -> None:
        """OllamaTimeoutError는 LLMGenerationError의 하위 클래스이다."""
        assert issubclass(OllamaTimeoutError, LLMGenerationError)

    def test_OllamaResponseError는_LLMGenerationError_하위(self) -> None:
        """OllamaResponseError는 LLMGenerationError의 하위 클래스이다."""
        assert issubclass(OllamaResponseError, LLMGenerationError)

    def test_LLMBackendError로_OllamaConnectionError_잡기(self) -> None:
        """소비자 코드에서 LLMBackendError로 Ollama 에러를 잡을 수 있는지 검증."""
        with pytest.raises(LLMBackendError):
            raise OllamaConnectionError("테스트")

    def test_LLMConnectionError로_OllamaConnectionError_잡기(self) -> None:
        """소비자 코드에서 LLMConnectionError로 연결 에러를 잡을 수 있는지 검증."""
        with pytest.raises(LLMConnectionError):
            raise OllamaConnectionError("테스트")
