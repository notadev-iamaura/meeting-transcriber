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
def _disable_native_gpu_cleanup_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """기본 테스트 세션에서 MLX/Metal cleanup import를 차단한다.

    native cleanup은 테스트 대상이 명시적으로 환경변수를 지우거나
    `gpu_cache_cleanup_enabled=True`를 주입한 경우에만 실행한다.
    """
    monkeypatch.setenv("MT_DISABLE_GPU_CACHE_CLEANUP", "1")


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
    monkeypatch.setattr("core.stt_model_status.get_manual_import_dir", _isolated, raising=False)
    monkeypatch.setattr("core.stt_model_registry.get_manual_import_dir", _isolated, raising=False)
    monkeypatch.setattr(
        "core.stt_model_downloader.get_manual_import_dir", _isolated, raising=False
    )


@pytest.fixture(autouse=True)
def _isolate_chroma_db(tmp_path_factory, monkeypatch):
    """ChromaDB 디렉토리를 tmp 로 격리한다 (hermeticity).

    위키 하이브리드 검색(G1)·transcript RAG 는 config.paths.resolved_chroma_db_dir
    (기본 `~/.meeting-transcriber/chroma_db`) 에 PersistentClient/컬렉션을 생성한다.
    chat_integration 처럼 get_config() 싱글톤을 쓰는 경로가 사용자 실 디렉토리를
    오염(빈 wiki_pages 컬렉션 생성·권한 약화)시킬 수 있어, 모든 테스트에서 tmp 로
    강제 치환한다. 개별 테스트는 chroma_dir 를 명시 인자로 전달하면 그대로 우선한다.
    """
    from config import PathsConfig

    fake_chroma = tmp_path_factory.mktemp("isolated-chroma") / "chroma_db"
    monkeypatch.setattr(
        PathsConfig,
        "resolved_chroma_db_dir",
        property(lambda self: fake_chroma),
        raising=True,
    )
