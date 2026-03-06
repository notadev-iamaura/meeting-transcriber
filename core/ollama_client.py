"""
Ollama HTTP 클라이언트 통합 모듈 (Unified Ollama HTTP Client)

목적: Ollama API와의 HTTP 통신을 단일 모듈로 통합한다.
주요 기능:
    - 서버 연결 확인 (health check, PERF-024: 캐싱 지원)
    - /api/chat 엔드포인트 호출 (동기, stream=false)
    - /api/chat 스트리밍 호출 (동기, stream=true)
    - 통합 에러 계층 (OllamaConnectionError, OllamaTimeoutError)
의존성: urllib (stdlib만 사용)
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# PERF-024: 연결 확인 캐시 — 파이프라인 실행 중 반복 호출 방지
# (host → 마지막 성공 시각) 매핑. 캐시 유효 시간 내에는 재확인하지 않는다.
_connection_cache: dict[str, float] = {}
# 캐시 유효 시간 (초): 파이프라인 1회 실행 중에는 재확인 불필요
_CONNECTION_CACHE_TTL_SECONDS: float = 300.0  # 5분


def clear_connection_cache() -> None:
    """연결 확인 캐시를 초기화한다. 테스트 용도로만 사용."""
    _connection_cache.clear()


# === 에러 계층 ===


class OllamaError(Exception):
    """Ollama 관련 에러의 기본 클래스."""


class OllamaConnectionError(OllamaError):
    """Ollama 서버에 연결할 수 없을 때 발생한다."""


class OllamaTimeoutError(OllamaError):
    """Ollama 요청이 타임아웃되었을 때 발생한다."""


class OllamaResponseError(OllamaError):
    """Ollama 응답 파싱 또는 내용 오류 시 발생한다."""


# === Ollama 클라이언트 함수 ===


def check_connection(host: str, timeout: int = 10) -> None:
    """Ollama 서버 연결을 확인한다.

    /api/tags 엔드포인트에 GET 요청을 보내 연결 가능 여부를 확인한다.
    PERF-024: 캐시 유효 시간 내 동일 호스트에 대한 반복 확인을 건너뛴다.
    파이프라인 실행 중 매 배치마다 호출되는 불필요한 네트워크 I/O를 제거한다.

    Args:
        host: Ollama 서버 호스트 URL (예: "http://127.0.0.1:11434")
        timeout: 연결 확인 타임아웃 (초)

    Raises:
        OllamaConnectionError: 서버에 연결할 수 없을 때
    """
    # PERF-024: 캐시 유효 시간 내이면 재확인 건너뛰기
    now = time.monotonic()
    last_check = _connection_cache.get(host)
    if last_check is not None and (now - last_check) < _CONNECTION_CACHE_TTL_SECONDS:
        logger.debug(f"Ollama 연결 확인 캐시 히트: {host}")
        return

    try:
        url = f"{host}/api/tags"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise OllamaConnectionError(
                    f"Ollama 서버 응답 오류: status={resp.status}"
                )
    except urllib.error.URLError as e:
        # 연결 실패 시 캐시 무효화
        _connection_cache.pop(host, None)
        raise OllamaConnectionError(
            f"Ollama 서버에 연결할 수 없습니다: {host} — {e}"
        ) from e
    except OllamaConnectionError:
        _connection_cache.pop(host, None)
        raise
    except Exception as e:
        _connection_cache.pop(host, None)
        raise OllamaConnectionError(
            f"Ollama 서버 연결 확인 실패: {e}"
        ) from e

    # 성공 시 캐시 갱신
    _connection_cache[host] = now
    logger.info(f"Ollama 서버 연결 확인 완료: {host}")


def chat(
    *,
    host: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.3,
    num_ctx: int = 8192,
    timeout: int = 120,
) -> str:
    """Ollama /api/chat 엔드포인트를 호출하여 응답 텍스트를 반환한다.

    stream=false로 전체 응답을 한 번에 수신한다.

    Args:
        host: Ollama 서버 호스트 URL
        model: 모델 이름
        messages: Ollama messages 형식의 대화 목록
        temperature: 생성 온도
        num_ctx: 컨텍스트 윈도우 크기
        timeout: 요청 타임아웃 (초)

    Returns:
        LLM 응답 텍스트

    Raises:
        OllamaConnectionError: 연결 실패 시
        OllamaTimeoutError: 타임아웃 시
        OllamaResponseError: 응답 파싱 실패 시
    """
    url = f"{host}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
        },
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        cause = str(e).lower()
        if "timed out" in cause or "timeout" in cause:
            raise OllamaTimeoutError(
                f"Ollama 요청 타임아웃 ({timeout}초)"
            ) from e
        raise OllamaConnectionError(
            f"Ollama API 호출 실패: {e}"
        ) from e
    except TimeoutError as e:
        raise OllamaTimeoutError(
            f"Ollama 요청 타임아웃 ({timeout}초)"
        ) from e
    except json.JSONDecodeError as e:
        raise OllamaResponseError(
            f"Ollama 응답 JSON 파싱 실패: {e}"
        ) from e

    content = response_data.get("message", {}).get("content", "")
    if not content:
        raise OllamaResponseError("Ollama 응답에 content가 없습니다")

    return content


def chat_stream(
    *,
    host: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.3,
    num_ctx: int = 8192,
    timeout: int = 120,
) -> Iterator[str]:
    """Ollama /api/chat 엔드포인트를 스트리밍 모드로 호출한다.

    stream=true로 응답을 토큰 단위로 수신하는 이터레이터를 반환한다.
    동기 함수이므로 asyncio.to_thread로 래핑하여 사용한다.

    Args:
        host: Ollama 서버 호스트 URL
        model: 모델 이름
        messages: Ollama messages 형식의 대화 목록
        temperature: 생성 온도
        num_ctx: 컨텍스트 윈도우 크기
        timeout: 요청 타임아웃 (초)

    Yields:
        토큰 문자열

    Raises:
        OllamaConnectionError: 연결 실패 시
        OllamaTimeoutError: 타임아웃 시
    """
    url = f"{host}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
        },
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for line in resp:
                line_str = line.decode("utf-8").strip()
                if not line_str:
                    continue
                try:
                    chunk_data = json.loads(line_str)
                except json.JSONDecodeError:
                    continue
                token = chunk_data.get("message", {}).get("content", "")
                if token:
                    yield token
                if chunk_data.get("done", False):
                    break
    except urllib.error.URLError as e:
        cause = str(e).lower()
        if "timed out" in cause or "timeout" in cause:
            raise OllamaTimeoutError(
                f"Ollama 스트리밍 타임아웃 ({timeout}초)"
            ) from e
        raise OllamaConnectionError(
            f"Ollama 스트리밍 호출 실패: {e}"
        ) from e
    except TimeoutError as e:
        raise OllamaTimeoutError(
            f"Ollama 스트리밍 타임아웃 ({timeout}초)"
        ) from e
