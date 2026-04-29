"""Phase 5.D — /api/chat 라우터 통합 테스트 모듈.

목적: api/routes.py 의 /api/chat 엔드포인트가 config.wiki.router_enabled 분기에
따라 올바르게 동작하는지 검증한다.

테스트 시나리오 (총 5건):
    1. router_enabled=False (default) — 응답이 기존 ChatService 와 동일,
       source_type/router_verdict/wiki_sources 가 모두 None (회귀 0건 보장)
    2. router_enabled=True + WIKI 라우팅 — source_type="wiki",
       router_verdict 메타 채워짐, references 비어 있음
    3. router_enabled=True + RAG 라우팅 — chat_service 호출 + source_type="rag",
       router_verdict 메타 채워짐
    4. router_enabled=True + 모호한 질의 → LLM 폴백 → confidence < 7 →
       RAG fallback (source_type="rag", reason="llm_low_confidence_fallback")
    5. ChatResponse 의 새 필드(source_type/router_verdict/wiki_sources) 가
       Optional 로 동작 (제공 안 해도 ChatResponse 직렬화 성공)

설계 원칙:
    - search/chat.py 직접 import 금지 (RAG 격리 보장).
    - ChatEngine 은 mock 으로 대체 → 외부 의존성(LLM, ChromaDB) 차단.
    - QueryRouter 의 휴리스틱 매칭은 그대로 사용 (실제 동작 검증).
    - LLM 폴백은 MockWikiLLMClient 로 통제.

의존성:
    - pytest, fastapi.TestClient, AppConfig/WikiConfig
    - core.wiki.router.QueryRouter (실제 사용 — 휴리스틱 검증)
    - core.wiki.llm_client.MockWikiLLMClient (LLM 폴백 시뮬레이션)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from config import AppConfig, PathsConfig, ServerConfig, WikiConfig


# ─────────────────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────────────────


def _make_chat_response_mock(
    answer: str = "기본 RAG 답변입니다.",
    *,
    has_context: bool = True,
    llm_used: bool = True,
    references: list[Any] | None = None,
) -> Any:
    """search.chat.ChatResponse 와 동등한 인터페이스의 mock 객체를 생성한다.

    routes.py 의 _build_chat_references 가 r.chunk_id 등 attribute 로 접근하므로
    SimpleNamespace 로 ChatReference 모방.

    Args:
        answer: 답변 텍스트.
        has_context: has_context 플래그.
        llm_used: llm_used 플래그.
        references: ChatReference 모방 객체 리스트.

    Returns:
        ChatResponse 형태 mock.
    """
    from types import SimpleNamespace

    if references is None:
        references = []

    return SimpleNamespace(
        answer=answer,
        references=references,
        query="dummy",
        has_context=has_context,
        llm_used=llm_used,
        error_message=None,
    )


def _make_chat_reference_mock(
    chunk_id: str = "chunk_1",
    meeting_id: str = "m_001",
    date: str = "2026-04-15",
    speakers: list[str] | None = None,
) -> Any:
    """ChatReference 모방 SimpleNamespace.

    Args:
        chunk_id: 청크 ID.
        meeting_id: 회의 ID.
        date: 회의 날짜.
        speakers: 화자 목록.

    Returns:
        ChatReference 형태 객체.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        chunk_id=chunk_id,
        meeting_id=meeting_id,
        date=date,
        speakers=speakers or ["A"],
        start_time=10.0,
        end_time=20.0,
        text_preview="샘플 발화",
        score=0.85,
    )


def _make_app_with_chat_engine(
    tmp_path: Path,
    *,
    router_enabled: bool = False,
    router_llm_fallback: bool = False,
    wiki_enabled: bool = False,
    chat_response: Any = None,
) -> Any:
    """라우터 토글이 가능한 FastAPI 앱을 생성한다.

    chat_engine 은 항상 mock 으로 대체. wiki_enabled 가 True 인 경우 WikiStore
    가 빈 페이지 디렉토리로 초기화된다.

    Args:
        tmp_path: pytest tmp_path.
        router_enabled: WikiConfig.router_enabled.
        router_llm_fallback: WikiConfig.router_llm_fallback.
        wiki_enabled: WikiConfig.enabled.
        chat_response: chat_engine.chat() 가 반환할 mock (None 이면 default).

    Returns:
        FastAPI 앱 — chat_engine 은 mock 으로 교체된 상태.
    """
    from api.server import create_app

    if chat_response is None:
        chat_response = _make_chat_response_mock()

    config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
        wiki=WikiConfig(
            enabled=wiki_enabled,
            root=tmp_path / "wiki",
            router_enabled=router_enabled,
            router_llm_fallback=router_llm_fallback,
        ),
    )

    # ChatEngine 인스턴스 mock — chat() 가 chat_response 반환
    chat_engine_mock = MagicMock()
    chat_engine_mock.chat = AsyncMock(return_value=chat_response)

    with (
        patch(
            "search.hybrid_search.HybridSearchEngine",
            return_value=MagicMock(),
        ),
        patch(
            "search.chat.ChatEngine",
            return_value=chat_engine_mock,
        ),
    ):
        app = create_app(config)

    # app.state.chat_engine 을 mock 으로 보장 (lifespan 이 None 으로 셋팅했을 수도)
    app.state.chat_engine = chat_engine_mock
    return app


def _seed_wiki_page(
    wiki_root: Path,
    *,
    rel_path: str = "decisions/2026-04-29-test.md",
    title: str = "출시일 확정",
    body: str = "5월 1일로 출시일을 확정했다.",
) -> None:
    """WikiStore 가 인식할 수 있도록 frontmatter 가 있는 페이지를 만든다.

    Args:
        wiki_root: wiki 루트 디렉토리.
        rel_path: 루트 기준 상대 경로.
        title: 페이지 제목.
        body: 본문.
    """
    abs_path = wiki_root / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        "type: decision\n"
        f"title: {title}\n"
        "date: 2026-04-29\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{body} [meeting:abc12345@00:01:00]\n\n"
        "<!-- confidence: 8 -->\n"
    )
    abs_path.write_text(content, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────
# 1. router_enabled=False (default) — 회귀 0건 보장
# ─────────────────────────────────────────────────────────────────────────


class TestRouterDisabledRegression:
    """router_enabled=False 일 때 응답이 기존 ChatService 와 동등함을 검증한다."""

    def test_router_disabled_default_시_source_type이_None이고_chat_engine만_호출된다(
        self,
        tmp_path: Path,
    ) -> None:
        """router_enabled=False → source_type=None, router_verdict=None,
        wiki_sources=None.

        Phase 5 회귀 0건 보장 — 라우터 비활성 시 기존 응답 형태 100% 보존.
        """
        # Arrange
        chat_response = _make_chat_response_mock(
            answer="이번 회의 요약입니다.",
            references=[_make_chat_reference_mock()],
        )
        app = _make_app_with_chat_engine(
            tmp_path,
            router_enabled=False,  # default
            chat_response=chat_response,
        )

        # Act
        with TestClient(app) as client:
            response = client.post(
                "/api/chat",
                json={"query": "이번 회의 요약 알려줘"},
            )

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["answer"] == "이번 회의 요약입니다."
        # 새 필드는 None 이어야 함 (회귀 보장)
        assert body["source_type"] is None
        assert body["router_verdict"] is None
        assert body["wiki_sources"] is None
        # 기존 필드 유지
        assert len(body["references"]) == 1
        assert body["references"][0]["meeting_id"] == "m_001"
        # chat_engine.chat() 이 1회 호출됨
        assert app.state.chat_engine.chat.call_count == 1


# ─────────────────────────────────────────────────────────────────────────
# 2. router_enabled=True + WIKI 라우팅
# ─────────────────────────────────────────────────────────────────────────


class TestRouterEnabledWikiRouting:
    """라우터 활성 + WIKI 결정 시 위키 합성 분기로 진입하는지 검증한다."""

    def test_router_enabled_wiki_시_source_type이_wiki이고_chat_engine은_호출되지_않는다(
        self,
        tmp_path: Path,
    ) -> None:
        """WIKI 휴리스틱 시그널 매칭 → source_type='wiki', chat_engine.chat() 미호출.

        '지난주 결정사항 정리해줘' 는 PRD §2.1 time_range_decisions 시그널에
        매칭되는 문구. 휴리스틱이 명확히 WIKI 로 분류한다.
        """
        # Arrange
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir(parents=True, exist_ok=True)
        _seed_wiki_page(wiki_root)

        # WikiStore.init_repo() 가 git 초기화하므로 사전 호출
        from core.wiki.store import WikiStore

        WikiStore(root=wiki_root).init_repo()
        # 다시 페이지 작성 (init_repo 가 디렉토리 비울 수도)
        _seed_wiki_page(wiki_root)

        chat_response = _make_chat_response_mock(answer="이건 RAG 답변 — 호출되면 안됨")
        app = _make_app_with_chat_engine(
            tmp_path,
            router_enabled=True,
            router_llm_fallback=False,  # LLM 폴백 비활성 (휴리스틱만)
            wiki_enabled=True,
            chat_response=chat_response,
        )

        # Act
        with TestClient(app) as client:
            response = client.post(
                "/api/chat",
                json={"query": "지난주 결정사항 정리해줘"},
            )

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["source_type"] == "wiki", (
            f"WIKI 라우팅 시 source_type='wiki' 여야 하나 '{body['source_type']}'"
        )
        assert body["router_verdict"] is not None
        assert body["router_verdict"]["decision"] == "wiki"
        assert body["router_verdict"]["used_llm"] is False
        # WIKI 분기에서 RAG chat_engine 은 호출되지 않아야 함
        assert app.state.chat_engine.chat.call_count == 0
        # wiki_sources 가 채워져야 함
        assert body["wiki_sources"] is not None
        assert len(body["wiki_sources"]) >= 1


# ─────────────────────────────────────────────────────────────────────────
# 3. router_enabled=True + RAG 라우팅
# ─────────────────────────────────────────────────────────────────────────


class TestRouterEnabledRagRouting:
    """라우터 활성 + RAG 결정 시 chat_engine 이 호출되는지 검증한다."""

    def test_router_enabled_rag_시_chat_engine_호출되고_source_type이_rag이다(
        self,
        tmp_path: Path,
    ) -> None:
        """RAG 휴리스틱 매칭 → chat_engine.chat() 호출 + source_type='rag'.

        '이번 회의에서 뭐 얘기했어?' 는 PRD §2.2 single_meeting_scope 시그널에
        매칭. 휴리스틱이 RAG 로 분류한다.
        """
        # Arrange
        chat_response = _make_chat_response_mock(
            answer="이번 회의에서 API 설계를 논의했습니다.",
            references=[_make_chat_reference_mock()],
        )
        app = _make_app_with_chat_engine(
            tmp_path,
            router_enabled=True,
            router_llm_fallback=False,
            wiki_enabled=False,
            chat_response=chat_response,
        )

        # Act
        with TestClient(app) as client:
            response = client.post(
                "/api/chat",
                json={"query": "이번 회의에서 뭐 얘기했어?"},
            )

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["source_type"] == "rag", (
            f"RAG 라우팅 시 source_type='rag' 여야 하나 '{body['source_type']}'"
        )
        assert body["router_verdict"] is not None
        assert body["router_verdict"]["decision"] == "rag"
        assert body["answer"] == "이번 회의에서 API 설계를 논의했습니다."
        assert len(body["references"]) == 1
        # chat_engine 이 정확히 1회 호출됨
        assert app.state.chat_engine.chat.call_count == 1


# ─────────────────────────────────────────────────────────────────────────
# 4. 모호한 질의 + LLM 폴백 + 낮은 confidence → RAG fallback
# ─────────────────────────────────────────────────────────────────────────


class TestRouterAmbiguousQueryFallback:
    """휴리스틱 매칭 0 + LLM confidence < 7 → RAG fallback 검증."""

    def test_ambiguous_query_llm_low_confidence_시_rag_fallback된다(
        self,
        tmp_path: Path,
    ) -> None:
        """휴리스틱 매칭 0 + LLM 응답 confidence=3 → RAG fallback.

        라우터는 confidence_threshold=7 미만일 때 보수적으로 RAG 로 fallback.
        reason='llm_low_confidence_fallback' 으로 기록된다.

        구현: routes._build_hybrid_chat_service 를 직접 patch 하여
        mock_llm 이 주입된 HybridChatService 를 반환하게 한다 — 가장 간단·결정적.
        """
        from core.wiki.chat_integration import HybridChatService
        from core.wiki.llm_client import MockResponse, MockWikiLLMClient
        from core.wiki.router import QueryRouter

        # Arrange — confidence=3 (< 7) 이므로 fallback 트리거
        mock_llm_response = (
            '{"decision": "wiki", "confidence": 3, "reason": "확신 낮음"}'
        )
        mock_llm = MockWikiLLMClient(
            responses=[MockResponse(body=mock_llm_response)]
        )

        chat_response = _make_chat_response_mock(
            answer="모호한 질문에 대한 RAG 답변", references=[]
        )

        # _build_hybrid_chat_service 를 mock LLM 사용 버전으로 교체
        from api import routes as routes_module

        original_builder = routes_module._build_hybrid_chat_service

        def _patched_builder(req: Any, chat_engine: Any) -> Any:
            chat_adapter = routes_module._ChatEngineAdapter(chat_engine)
            router_obj = QueryRouter(
                llm=mock_llm,
                enable_llm_fallback=True,
                confidence_threshold=7,
            )
            return HybridChatService(
                chat_service=chat_adapter,
                router=router_obj,
                wiki_store=None,
                wiki_llm=mock_llm,
            )

        app = _make_app_with_chat_engine(
            tmp_path,
            router_enabled=True,
            router_llm_fallback=True,
            wiki_enabled=False,
            chat_response=chat_response,
        )

        # Act — 휴리스틱 매칭이 안 되는 모호한 질의
        ambiguous_query = "그거 어떻게 됐어"
        with patch.object(
            routes_module,
            "_build_hybrid_chat_service",
            side_effect=_patched_builder,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/api/chat",
                    json={"query": ambiguous_query},
                )

        # 후처리 — original 복원 (안전성)
        routes_module._build_hybrid_chat_service = original_builder

        # Assert
        assert response.status_code == 200
        body = response.json()
        # confidence < 7 → RAG fallback
        assert body["source_type"] == "rag", (
            f"낮은 confidence → RAG fallback 이 발생해야 하나 source_type='{body['source_type']}'"
        )
        assert body["router_verdict"] is not None
        assert body["router_verdict"]["decision"] == "rag"
        # reason 은 llm_low_confidence_fallback (raw RouteDecision.RAG fallback)
        reason = body["router_verdict"]["reason"]
        assert reason in (
            "llm_low_confidence_fallback",
            "default_conservative",
        ), f"예상치 못한 reason: '{reason}'"
        # chat_engine 이 호출되어 응답이 채워졌음
        assert app.state.chat_engine.chat.call_count == 1
        assert body["answer"] == "모호한 질문에 대한 RAG 답변"


# ─────────────────────────────────────────────────────────────────────────
# 5. ChatResponse 의 새 필드 Optional 동작 검증
# ─────────────────────────────────────────────────────────────────────────


class TestChatResponseOptionalFields:
    """ChatResponse 의 source_type/router_verdict/wiki_sources 가 Optional 임을 검증."""

    def test_chat_response_새필드_제공없이도_직렬화_성공한다(self) -> None:
        """ChatResponse 인스턴스를 새 필드 없이 만들어도 dict() 직렬화 정상 동작.

        Pydantic Field default 가 None 이라 옵션 필드 누락이 허용되어야 한다.
        기존 호출자(테스트, 외부 API 사용자) 가 영향받지 않음을 보장한다.
        """
        from api.routes import ChatResponse

        # 새 필드 모두 누락 — 기존 ChatResponse 사용 패턴 그대로
        instance = ChatResponse(
            answer="응답",
            references=[],
            query="질문",
            has_context=True,
            llm_used=True,
        )

        # dict 직렬화 성공
        d = instance.model_dump()
        assert d["answer"] == "응답"
        assert d["source_type"] is None
        assert d["router_verdict"] is None
        assert d["wiki_sources"] is None

    def test_chat_response_새필드_명시적_설정_시_dict에_반영된다(self) -> None:
        """source_type='wiki' 와 router_verdict 를 명시 설정 시 dict 에 그대로 노출."""
        from api.routes import ChatResponse

        instance = ChatResponse(
            answer="위키 답변",
            references=[],
            query="질문",
            source_type="wiki",
            router_verdict={
                "decision": "wiki",
                "confidence": 9,
                "reason": "test",
                "matched_signals": ["time_range_decisions"],
                "used_llm": False,
            },
            wiki_sources=[
                {
                    "page_path": "decisions/x.md",
                    "page_type": "decision",
                    "title": "X",
                    "snippet": "본문",
                    "citations": [],
                }
            ],
        )

        d = instance.model_dump()
        assert d["source_type"] == "wiki"
        assert d["router_verdict"]["decision"] == "wiki"
        assert d["router_verdict"]["confidence"] == 9
        assert len(d["wiki_sources"]) == 1
        assert d["wiki_sources"][0]["title"] == "X"
