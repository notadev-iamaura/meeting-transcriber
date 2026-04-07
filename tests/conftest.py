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


@pytest.fixture(autouse=True)
def _isolate_stt_manual_import_dir(tmp_path_factory, monkeypatch):
    """STT 수동 임포트 디렉토리를 tmp 경로로 격리한다.

    `core.stt_model_status._check_manual_import` 는 기본적으로
    `~/.meeting-transcriber/stt_models/{id}-manual/` 을 확인하므로, 사용자 환경에
    이미 파일이 있으면 테스트가 오염된다. 모든 테스트에서 이 경로를 테스트 세션용
    tmp 디렉토리로 강제 치환해 hermeticity 를 보장한다.

    개별 테스트가 필요하면 local monkeypatch 로 덮어쓸 수 있다 (pytest 의
    monkeypatch 가 LIFO 로 적용되므로 안전).
    """
    fake_base = tmp_path_factory.mktemp("isolated-stt-manual")

    def _isolated(spec, base_dir=None):
        return str(fake_base / f"{spec.id}-manual")

    # 모든 호출 지점을 한 번에 덮어쓴다
    monkeypatch.setattr(
        "core.stt_model_status.get_manual_import_dir", _isolated, raising=False
    )
    monkeypatch.setattr(
        "core.stt_model_registry.get_manual_import_dir", _isolated, raising=False
    )
    monkeypatch.setattr(
        "core.stt_model_downloader.get_manual_import_dir", _isolated, raising=False
    )
