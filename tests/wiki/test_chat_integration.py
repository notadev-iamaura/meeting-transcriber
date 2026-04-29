"""HybridChatService TDD Red 단계 테스트 모듈 (Phase 5)

목적: core/wiki/chat_integration.py 의 HybridChatService.respond() 인터페이스를
  TDD Red 단계로 검증한다. core/wiki/chat_integration.py 가 아직 존재하지 않으므로
  모든 테스트는 ImportError 로 실패해야 한다.

테스트 범주:
    1. router=None (default) — chat_service 100% 위임 (1건)
    2. router 활성 + RAG 결정 — chat_service 호출, source_type="rag" (1건)
    3. router 활성 + WIKI 결정 — wiki 합성, source_type="wiki" (1건)
    4. router 활성 + LLM 라우터 + chat_service 응답 byte-identical 회귀 (1건)

총 4건

설계 원칙:
    - search/chat.py 는 절대 import 하지 않는다 (RAG 격리 보장).
    - chat_service 는 MockChatService 로 대체.
    - WIKI 라우팅 테스트는 WikiStore 실제 인스턴스 사용 (tmp_path).

의존성:
    - pytest, pytest-asyncio (asyncio_mode=auto)
    - core.wiki.chat_integration (Phase 5, 아직 미구현 → ImportError Red)
    - core.wiki.router.{QueryRouter, RouteDecision, RouterVerdict}
    - core.wiki.store.WikiStore (tmp_path 기반 실제 인스턴스)

작성자: TDD Red Author
날짜: 2026-04-29
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

# ─── Phase 5 대상 모듈 (아직 미구현 → ImportError Red) ─────────────────────
from core.wiki.chat_integration import (  # type: ignore[import]  # noqa: E402
    HybridChatResponse,
    HybridChatService,
)

# ─── Phase 1+2+3+4 실제 구현 (변경 금지) ────────────────────────────────────
from core.wiki.router import (  # type: ignore[import]
    QueryRouter,
    RouteDecision,
    RouterVerdict,
)
from core.wiki.store import WikiStore


# ─────────────────────────────────────────────────────────────────────────────
# Mock 정의
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _FakeChatResponse:
    """search.chat.ChatResponse 의 최소 mock 데이터 클래스.

    search/chat.py 를 import 하지 않기 위해 응답 구조를 모방한다.
    """

    answer: str
    sources: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리 직렬화 (API 호환)."""
        return {"answer": self.answer, "sources": self.sources}


class MockChatService:
    """기존 ChatService mock — respond() 만 정의.

    search/chat.py 의 ChatEngine.chat() 시그니처와 유사하게 구성해
    HybridChatService 의 위임 동작을 검증한다.
    """

    def __init__(self, fixed_response: Any) -> None:
        """고정 응답 객체를 설정한다.

        Args:
            fixed_response: respond() 가 항상 반환할 객체.
        """
        self._response = fixed_response
        self.call_count = 0
        self.last_query: str | None = None

    async def respond(self, query: str, **kwargs: Any) -> Any:
        """고정 응답을 반환하고 호출 기록을 남긴다.

        Args:
            query: 사용자 질문.
            **kwargs: session_id 등 추가 파라미터 (무시).

        Returns:
            초기화 시 설정된 고정 응답.
        """
        self.call_count += 1
        self.last_query = query
        return self._response


class MockQueryRouter:
    """고정 verdict 를 반환하는 QueryRouter mock.

    classify() 가 항상 미리 설정된 RouterVerdict 를 반환한다.
    """

    def __init__(self, verdict: RouterVerdict) -> None:
        """반환할 verdict 를 설정한다.

        Args:
            verdict: classify() 가 반환할 RouterVerdict.
        """
        self._verdict = verdict
        self.call_count = 0

    async def classify(self, query: str) -> RouterVerdict:
        """고정 verdict 를 반환한다.

        Args:
            query: 사용자 질문 (사용하지 않음).

        Returns:
            초기화 시 설정된 RouterVerdict.
        """
        self.call_count += 1
        return self._verdict


# ─────────────────────────────────────────────────────────────────────────────
# 1. router=None (default) — 100% chat_service 위임
# ─────────────────────────────────────────────────────────────────────────────


class TestHybridChatServiceRouterDisabled:
    """router=None 일 때 HybridChatService 가 chat_service 에 100% 위임함을 검증한다."""

    async def test_router가_none이면_chat_service_respond에_100퍼센트_위임한다(
        self,
    ) -> None:
        """router=None → respond() 가 chat_service.respond() 결과를 source_type='rag' 로 wrap.

        PRD §10.3, §7.4: 라우터 비활성 default 일 때 기존 RAG 100% 무영향 보장.
        rag_response 가 chat_service.respond() 의 직접 반환값과 동일해야 한다.

        Arrange: MockChatService(fixed_response), router=None HybridChatService
        Act:     respond('오늘 회의 요약해줘') 호출
        Assert:  source_type='rag', rag_response == fixed_response, chat_service.call_count == 1
        """
        # Arrange
        fixed_response = _FakeChatResponse(
            answer="오늘 회의에서 출시일을 5월 1일로 확정했습니다.",
            sources=[{"meeting_id": "abc12345", "timestamp": "00:01:00"}],
        )
        mock_chat = MockChatService(fixed_response=fixed_response)
        service = HybridChatService(chat_service=mock_chat, router=None)

        # Act
        result = await service.respond("오늘 회의 요약해줘")

        # Assert
        assert isinstance(result, HybridChatResponse), (
            f"respond() 반환값이 HybridChatResponse 여야 하나 {type(result)!r}"
        )
        assert result.source_type == "rag", (
            f"router=None 이면 source_type='rag' 여야 하나 '{result.source_type}'"
        )
        assert result.rag_response is fixed_response, (
            "rag_response 가 chat_service.respond() 반환값과 동일해야 함 (identity 검사)"
        )
        assert mock_chat.call_count == 1, (
            f"chat_service.respond() 가 정확히 1회 호출되어야 하나 {mock_chat.call_count}회"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. router 활성 + RAG 결정
# ─────────────────────────────────────────────────────────────────────────────


class TestHybridChatServiceRagRouting:
    """라우터가 RAG 결정을 반환할 때 HybridChatService 가 chat_service 를 호출하는지 검증한다."""

    async def test_router가_rag_결정하면_chat_service_호출하고_source_type이_rag이다(
        self,
    ) -> None:
        """router → RAG 결정 → chat_service.respond() 호출 + source_type='rag'.

        §3.2 HybridChatService 동작 모드 2:
        RAG 분기에서 ChatEngine.chat() (여기서는 MockChatService.respond()) 를
        호출하고 결과를 source_type='rag' 로 wrap 해야 한다.

        Arrange: MockQueryRouter(RAG verdict), MockChatService
        Act:     respond('이번 회의 뭐 얘기했어?') 호출
        Assert:  source_type='rag', chat_service.call_count == 1
        """
        # Arrange
        rag_verdict = RouterVerdict(
            decision=RouteDecision.RAG,
            confidence=9,
            reason="single_meeting_scope 시그널 매칭",
            matched_signals=["single_meeting_scope"],
            used_llm=False,
        )
        mock_router = MockQueryRouter(verdict=rag_verdict)
        fixed_response = _FakeChatResponse(
            answer="이번 회의에서 API 설계를 논의했습니다.",
            sources=[],
        )
        mock_chat = MockChatService(fixed_response=fixed_response)
        service = HybridChatService(
            chat_service=mock_chat,
            router=mock_router,
        )

        # Act
        result = await service.respond("이번 회의 뭐 얘기했어?")

        # Assert
        assert result.source_type == "rag", (
            f"RAG 결정 시 source_type='rag' 여야 하나 '{result.source_type}'"
        )
        assert mock_chat.call_count == 1, (
            f"RAG 분기에서 chat_service 가 1회 호출되어야 하나 {mock_chat.call_count}회"
        )
        assert result.rag_response is fixed_response, (
            "rag_response 가 chat_service.respond() 반환값과 동일해야 함"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. router 활성 + WIKI 결정
# ─────────────────────────────────────────────────────────────────────────────


class TestHybridChatServiceWikiRouting:
    """라우터가 WIKI 결정을 반환할 때 Wiki 합성 경로로 분기하는지 검증한다."""

    async def test_router가_wiki_결정하면_wiki_합성하고_source_type이_wiki이다(
        self,
        tmp_path: Path,
    ) -> None:
        """router → WIKI 결정 → wiki_store 에서 페이지 읽어 답변, source_type='wiki'.

        §3.2 WIKI 분기: _synthesize_from_wiki() 호출 → wiki_answer + wiki_sources 채움.
        chat_service 는 호출되지 않아야 한다.

        WikiStore 는 실제 인스턴스를 사용 (tmp_path). 페이지 1개를 미리 작성해
        WIKI 합성 경로가 데이터를 읽는지 확인한다.

        Arrange:
            - WikiStore(tmp_path/wiki) 실제 인스턴스 + decisions/2026-04-29-test.md 작성
            - MockQueryRouter(WIKI verdict)
            - MockChatService (호출되면 안 됨)
        Act:     respond('지난주 결정사항 정리해줘') 호출
        Assert:  source_type='wiki', wiki_answer is not None, chat_service.call_count == 0
        """
        # Arrange — WikiStore 실제 인스턴스 생성
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir(parents=True, exist_ok=True)
        store = WikiStore(root=wiki_root)
        # init_repo() 는 동기 메서드 — Red 단계 테스트가 잘못 await 했었음.
        # Phase 1+2+3+4 store.py 인터페이스(동기)를 따른다.
        store.init_repo()

        # 테스트 결정 페이지 작성
        decisions_dir = wiki_root / "decisions"
        decisions_dir.mkdir(parents=True, exist_ok=True)
        page_content = (
            "---\n"
            "type: decision\n"
            "title: 출시일 확정\n"
            "date: 2026-04-29\n"
            "---\n\n"
            "# 출시일 확정\n\n"
            "5월 1일로 출시일을 확정했다. [meeting:abc12345@00:01:00]\n\n"
            "<!-- confidence: 8 -->\n"
        )
        (decisions_dir / "2026-04-29-test.md").write_text(page_content, encoding="utf-8")

        wiki_verdict = RouterVerdict(
            decision=RouteDecision.WIKI,
            confidence=9,
            reason="time_range_decisions 시그널 매칭",
            matched_signals=["time_range_decisions"],
            used_llm=False,
        )
        mock_router = MockQueryRouter(verdict=wiki_verdict)
        mock_chat = MockChatService(
            fixed_response=_FakeChatResponse(answer="RAG 답변", sources=[])
        )
        service = HybridChatService(
            chat_service=mock_chat,
            router=mock_router,
            wiki_store=store,
        )

        # Act
        result = await service.respond("지난주 결정사항 정리해줘")

        # Assert
        assert result.source_type == "wiki", (
            f"WIKI 결정 시 source_type='wiki' 여야 하나 '{result.source_type}'"
        )
        assert result.wiki_answer is not None, (
            "WIKI 분기에서 wiki_answer 가 None 이 아니어야 함"
        )
        assert isinstance(result.wiki_sources, list), (
            "wiki_sources 가 list 여야 함"
        )
        assert mock_chat.call_count == 0, (
            f"WIKI 분기에서 chat_service 는 호출되지 않아야 하나 {mock_chat.call_count}회 호출됨"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. 회귀 보장 — LLM 라우터 + chat_service 응답 byte-identical
# ─────────────────────────────────────────────────────────────────────────────


class TestHybridChatServiceRagRegressionGuard:
    """라우터가 RAG 로 결정해도 chat_service 응답이 라우터 비활성 시와 동일함을 검증한다."""

    async def test_router_enabled_rag_결정_시_chat_service_응답이_비활성과_동일하다(
        self,
    ) -> None:
        """router_enabled=True + RAG verdict 인 경우 chat_service 응답이 byte-identical.

        PRD §10.3, §8.4: "router_enabled=True 라도 default_rag verdict 인 경우
        ChatEngine.chat() 응답이 라우터 비활성 시와 동일해야 함".

        두 경로(router=None, router=mock_rag) 에서 동일 query 를 실행했을 때
        rag_response 가 identity 동일한지 확인한다.

        Arrange:
            - fixed_response 하나 준비
            - 경로 A: router=None (비활성)
            - 경로 B: MockQueryRouter(RAG verdict, used_llm=True) (활성, LLM 사용)
        Act:     두 경로 각각 respond() 호출
        Assert:  두 rag_response 가 동일한 객체 (identity) 또는 동일한 answer 값
        """
        # Arrange
        fixed_response = _FakeChatResponse(
            answer="결정사항: API 버전 2.0 확정.",
            sources=[{"meeting_id": "abc12345", "timestamp": "00:05:00"}],
        )

        # 경로 A — router=None (비활성)
        mock_chat_a = MockChatService(fixed_response=fixed_response)
        service_a = HybridChatService(chat_service=mock_chat_a, router=None)

        # 경로 B — RAG verdict (LLM 폴백 채택, used_llm=True)
        rag_verdict_with_llm = RouterVerdict(
            decision=RouteDecision.RAG,
            confidence=8,
            reason="llm_fallback_high_confidence",
            matched_signals=[],
            used_llm=True,
        )
        mock_router_b = MockQueryRouter(verdict=rag_verdict_with_llm)
        mock_chat_b = MockChatService(fixed_response=fixed_response)
        service_b = HybridChatService(
            chat_service=mock_chat_b,
            router=mock_router_b,
        )

        query = "회의 결과 알려줘"

        # Act
        result_a = await service_a.respond(query)
        result_b = await service_b.respond(query)

        # Assert — 두 경로 모두 동일한 chat_service 응답을 wrap 해야 함
        assert result_a.source_type == "rag", (
            f"경로 A source_type 이 'rag' 여야 하나 '{result_a.source_type}'"
        )
        assert result_b.source_type == "rag", (
            f"경로 B source_type 이 'rag' 여야 하나 '{result_b.source_type}'"
        )
        # byte-identical: 동일한 fixed_response 객체를 wrap 했으므로 answer 도 같아야 함
        assert result_a.rag_response.answer == result_b.rag_response.answer, (
            "라우터 비활성(A)과 활성 RAG(B) 의 rag_response.answer 가 동일해야 함 "
            f"(A='{result_a.rag_response.answer}', B='{result_b.rag_response.answer}')"
        )
        # chat_service 가 각 경로에서 1회씩 호출됐는지 확인
        assert mock_chat_a.call_count == 1, (
            f"경로 A chat_service 가 1회 호출되어야 하나 {mock_chat_a.call_count}회"
        )
        assert mock_chat_b.call_count == 1, (
            f"경로 B chat_service 가 1회 호출되어야 하나 {mock_chat_b.call_count}회"
        )
