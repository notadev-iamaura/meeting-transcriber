"""
테스트 공통 설정 (Test Configuration)

목적: 테스트 간 공유 상태 오염을 방지하는 autouse fixture를 정의한다.
주요 기능: Ollama 연결 캐시 초기화 (PERF-024 캐시가 테스트 간 누수되는 문제 해결)
의존성: core.ollama_client
"""

from __future__ import annotations

import pytest

from core.ollama_client import clear_connection_cache


@pytest.fixture(autouse=True)
def _clear_ollama_cache() -> None:
    """각 테스트 실행 전 Ollama 연결 캐시를 초기화한다.

    PERF-024에서 추가된 check_connection 캐시가 테스트 간 오염되어
    비정상 상태코드 테스트 등이 캐시 히트로 건너뛰는 문제를 방지한다.
    """
    clear_connection_cache()
