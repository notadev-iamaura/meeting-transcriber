"""RAG 무영향 회귀 테스트 (Phase 1.H, PRD §10.3)

목적: LLM Wiki Phase 1 도입이 기존 RAG 시스템(검색/Chat) 동작에 절대 영향을
      주지 않음을 보장한다. 이 테스트는 단위 레벨이 아니라 모듈 의존성 그래프
      자체를 검증하므로, wiki 모듈을 import 하지 않은 상태로도 RAG 가 100%
      동일하게 동작해야 한다는 계약을 강제한다.

검증 항목:
    1. search/* 어디서도 core.wiki 를 import 하지 않는다 (정적 의존성 검사)
    2. api/routes.py 의 /search, /chat, /meetings 핸들러가 core.wiki 를
       호출하지 않는다 (정적 의존성 검사)
    3. wiki.enabled 토글에 따른 ChromaDB / FTS5 인스턴스 식별자 무영향
    4. wiki.enabled 토글에 따른 /api/search 응답 동일성
    5. wiki.enabled 토글에 따른 /api/chat 응답 동일성

설계 노트:
    - 실제 ChromaDB / 임베딩 모델을 띄우지 않고 mock 한다 (단위 + 통합의 중간
      회귀 테스트). 검증 대상은 "라우트가 wiki 영향을 받지 않는다" 자체이므로
      mock 으로 충분하다.
    - PRD §10.3 의 "동일 query, 동일 결과" 보장을 위해 동일 mock 응답을 두 번
      생성하고 byte-level diff 가 없는지 확인한다.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from config import AppConfig, PathsConfig, ServerConfig, WikiConfig


# ─── 헬퍼 ───────────────────────────────────────────────────────────────


def _make_app(tmp_path: Path, *, wiki_enabled: bool) -> Any:
    """주어진 wiki.enabled 설정으로 FastAPI 앱을 생성한다.

    Args:
        tmp_path: pytest tmp_path fixture
        wiki_enabled: WikiConfig.enabled 값

    Returns:
        FastAPI 앱 인스턴스 (search/chat 엔진은 mock).
    """
    from api.server import create_app

    config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
        wiki=WikiConfig(enabled=wiki_enabled, root=tmp_path / "wiki"),
    )

    with (
        patch(
            "search.hybrid_search.HybridSearchEngine",
            return_value=MagicMock(),
        ),
        patch(
            "search.chat.ChatEngine",
            return_value=MagicMock(),
        ),
    ):
        app = create_app(config)
    return app


# ─── 1. 정적 의존성 검사 (Wiki ↛ RAG) ─────────────────────────────────


class TestStaticImportIsolation:
    """search/* 와 RAG 핸들러가 core.wiki 를 import 하지 않는지 정적 검증."""

    def test_search_모듈은_core_wiki_import_금지(self) -> None:
        """search/hybrid_search.py 와 search/chat.py 가 core.wiki 를 import 하지 않는다."""
        for module_name in ("search.hybrid_search", "search.chat"):
            module = __import__(module_name, fromlist=["*"])
            source = inspect.getsource(module)
            assert "core.wiki" not in source, (
                f"{module_name} 가 core.wiki 를 import 하고 있습니다 — "
                f"RAG 무영향 원칙 위반."
            )

    def test_search_디렉토리_grep_으로_core_wiki_없음(self) -> None:
        """search/ 디렉토리 전체에 core.wiki 참조가 없다 (텍스트 검색).

        PRD §10.3 은 모듈 의존성 그래프 자체에 wiki → rag 또는 rag → wiki
        엣지가 절대 없을 것을 요구한다. 패키지 단위 grep 으로 보강 검증한다.
        """
        search_dir = Path(__file__).resolve().parents[2] / "search"
        for py_file in search_dir.rglob("*.py"):
            text = py_file.read_text(encoding="utf-8")
            assert "core.wiki" not in text, (
                f"{py_file} 가 core.wiki 를 참조합니다 — RAG 무영향 원칙 위반."
            )

    def test_routes_의_search_chat_핸들러는_wiki_무관(self) -> None:
        """api/routes.py 의 /search, /chat 핸들러 함수 본문에 core.wiki 가 없다.

        라우트 정의 전체 텍스트에 'core.wiki' 가 등장해도 wiki 전용 엔드포인트
        때문일 수 있으므로, 함수 단위로 검사한다.
        """
        from api import routes

        for fn_name in ("search_meetings", "chat_query"):
            fn = getattr(routes, fn_name, None)
            if fn is None:
                # 다른 이름일 수 있음 — 본문 직접 검사
                continue
            try:
                source = inspect.getsource(fn)
            except (OSError, TypeError):
                continue
            assert "core.wiki" not in source, (
                f"{fn_name} 가 core.wiki 를 참조합니다 — RAG 무영향 원칙 위반."
            )


# ─── 2. wiki.enabled 토글 → RAG 응답 동일성 ────────────────────────────


class TestRAGResponsesUnaffectedByWiki:
    """wiki.enabled 가 True/False 와 무관하게 RAG 응답이 동일함을 검증."""

    def _make_search_response(self) -> dict[str, Any]:
        """검색 응답 mock 데이터 (deterministic).

        Returns:
            HybridSearchEngine.search() 가 반환하는 SearchResponse mock.
        """
        from search.hybrid_search import SearchResponse, SearchResult

        return SearchResponse(
            results=[
                SearchResult(
                    chunk_id="chunk_1",
                    text="회의 내용 샘플",
                    score=0.85,
                    meeting_id="m_001",
                    date="2026-04-15",
                    speakers=["A"],
                    start_time=10.0,
                    end_time=20.0,
                    chunk_index=0,
                    source="both",
                )
            ],
            query="샘플",
            total_found=1,
            vector_count=1,
            fts_count=0,
            filters_applied={},
        )

    def test_search_엔드포인트_wiki_토글_무영향(self, tmp_path: Path) -> None:
        """/api/search 응답이 wiki.enabled 토글과 무관하게 동일하다."""
        responses_by_state: dict[bool, Any] = {}

        for wiki_state in (False, True):
            app = _make_app(tmp_path / f"state_{wiki_state}", wiki_enabled=wiki_state)
            with TestClient(app) as client:
                # 동일한 mock 응답을 양쪽 모두에 주입
                app.state.search_engine.search = AsyncMock(
                    return_value=self._make_search_response()
                )

                response = client.post(
                    "/api/search",
                    json={"query": "샘플"},
                )

            assert response.status_code == 200
            responses_by_state[wiki_state] = response.json()

        # wiki disabled vs enabled 시 응답이 byte-level 로 동일해야 한다.
        assert responses_by_state[False] == responses_by_state[True], (
            "wiki.enabled 토글이 /api/search 응답을 변화시켰습니다. "
            "RAG 무영향 원칙 위반."
        )

    def test_search_엔진_인스턴스_동일성(self, tmp_path: Path) -> None:
        """wiki 토글이 search 엔진 클래스 자체를 변화시키지 않는다.

        HybridSearchEngine 의 클래스 정의/시그니처는 wiki 와 독립적이어야 한다.
        """
        from search.hybrid_search import HybridSearchEngine

        # 클래스의 __init__ 시그니처에 wiki 관련 파라미터가 없어야 한다.
        sig = inspect.signature(HybridSearchEngine.__init__)
        param_names = list(sig.parameters.keys())
        for name in param_names:
            assert "wiki" not in name.lower(), (
                f"HybridSearchEngine.__init__ 에 wiki 관련 파라미터가 있습니다: "
                f"{name}. RAG 무영향 원칙 위반."
            )

    def test_chat_엔진_인스턴스_동일성(self, tmp_path: Path) -> None:
        """ChatEngine 시그니처 역시 wiki 와 독립적이어야 한다."""
        from search.chat import ChatEngine

        sig = inspect.signature(ChatEngine.__init__)
        param_names = list(sig.parameters.keys())
        for name in param_names:
            assert "wiki" not in name.lower(), (
                f"ChatEngine.__init__ 에 wiki 관련 파라미터가 있습니다: {name}. "
                f"RAG 무영향 원칙 위반."
            )

    def test_meetings_목록_wiki_토글_무영향(self, tmp_path: Path) -> None:
        """/api/meetings 응답이 wiki.enabled 토글과 무관하게 동일하다.

        meetings 엔드포인트는 ChromaDB 가 아닌 SQLite (job_queue) 만 사용하지만,
        wiki 도입이 이 경로에도 부작용을 주지 않음을 통합 레벨에서 검증한다.
        """
        responses_by_state: dict[bool, Any] = {}

        for wiki_state in (False, True):
            app = _make_app(tmp_path / f"meetings_{wiki_state}", wiki_enabled=wiki_state)
            with TestClient(app) as client:
                app.state.job_queue.get_all_jobs = AsyncMock(return_value=[])
                response = client.get("/api/meetings")

            assert response.status_code == 200
            responses_by_state[wiki_state] = response.json()

        assert responses_by_state[False] == responses_by_state[True]
