"""HybridChatService — 라우터 기반 RAG/Wiki 통합 응답 (Phase 5)

목적: QueryRouter 의 결정에 따라 기존 ChatService(RAG) 또는 Wiki 페이지 검색
결과를 합성해 사용자에게 통합 응답을 반환한다. 라우터 비활성 시 100%
chat_service.respond() 그대로 위임 — 기존 RAG 회귀 테스트 보장.

설계 원칙:
    - search/chat.py 무변경: 이 모듈은 search/* 를 import 하지 않으며,
      chat_service 는 duck-typing 으로 받는다 (Any 타입).
    - 라우터 비활성 default: router=None 이면 항상 RAG.
    - WIKI 응답 형식: 위키 페이지 본문 + 페이지 링크 list (인용 메타데이터 포함).
    - BOTH 응답: rag_response + wiki_answer 둘 다 채워, UI 가 선택 노출.
    - graceful degradation: WIKI 답변 생성 실패 → 자동으로 RAG 응답으로 폴백.

의존성:
    - core.wiki.router.{QueryRouter, RouteDecision, RouterVerdict}
    - core.wiki.store.WikiStore (위키 페이지 read — duck-typing)
    - core.wiki.models.PageType (인용 메타데이터)

**search/chat.py 자체는 절대 수정하거나 import 하지 않는다.**
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from core.wiki.router import QueryRouter, RouteDecision, RouterVerdict

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# 3.1 응답 데이터 클래스
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class WikiAnswerSource:
    """WIKI 응답의 인용 출처 (UI 가 페이지 링크 + 인용 마커로 렌더링).

    Attributes:
        page_path: wiki 루트 기준 상대 경로 (예: "decisions/2026-04-15-x.md").
        page_type: PageType enum 값 ("decision" / "person" / ...).
        title: 페이지 frontmatter 의 title 또는 첫 # 제목.
        snippet: 답변 합성에 인용된 본문 일부 (최대 200자).
        citations: 페이지에 포함된 [meeting:id@HH:MM:SS] 마커 raw 문자열 리스트.
    """

    page_path: str
    page_type: str
    title: str
    snippet: str
    citations: list[str] = field(default_factory=list)


# 기본 RAG verdict — router=None 일 때 노출용.
# confidence=10 으로 명시 — 사용자가 명시적으로 라우터를 비활성화했으므로
# "RAG 100% 위임" 임을 분명히 한다.
_DEFAULT_RAG_VERDICT: RouterVerdict = RouterVerdict(
    decision=RouteDecision.RAG,
    confidence=10,
    reason="router_disabled_default_rag",
    matched_signals=[],
    used_llm=False,
)


@dataclass
class HybridChatResponse:
    """라우터 기반 통합 응답.

    UI 가 source_type 으로 렌더링 분기:
        - "rag":  rag_response 만 채워짐 (기존 ChatResponse 100% 호환).
        - "wiki": wiki_answer + wiki_sources 채워짐.
        - "both": 둘 다 채워짐 (UI 가 탭/병렬 표시).

    router_verdict 는 항상 채워져 디버깅/운영 telemetry 용.

    Attributes:
        source_type: "rag" | "wiki" | "both".
        router_verdict: 라우터 결정 (라우터 비활성 시 default_rag verdict).
        rag_response: chat_service.respond() 결과 (RAG/BOTH 분기에서 채움).
        wiki_answer: WIKI/BOTH 분기에서 합성된 답변 본문.
        wiki_sources: 인용 출처 페이지 목록.
        error_message: 합성 실패 시 메시지 (graceful degradation 시 채움).
    """

    source_type: str
    router_verdict: RouterVerdict
    rag_response: Any = None
    wiki_answer: str | None = None
    wiki_sources: list[WikiAnswerSource] = field(default_factory=list)
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """API JSON 직렬화. rag_response.to_dict() 가 있으면 호출.

        Returns:
            JSON 직렬화 가능한 dict.
        """
        rag_dict: Any = None
        if self.rag_response is not None:
            to_dict_method = getattr(self.rag_response, "to_dict", None)
            if callable(to_dict_method):
                rag_dict = to_dict_method()
            else:
                rag_dict = self.rag_response

        return {
            "source_type": self.source_type,
            "router_verdict": {
                "decision": str(self.router_verdict.decision),
                "confidence": self.router_verdict.confidence,
                "reason": self.router_verdict.reason,
                "matched_signals": list(self.router_verdict.matched_signals),
                "used_llm": self.router_verdict.used_llm,
            },
            "rag_response": rag_dict,
            "wiki_answer": self.wiki_answer,
            "wiki_sources": [
                {
                    "page_path": s.page_path,
                    "page_type": s.page_type,
                    "title": s.title,
                    "snippet": s.snippet,
                    "citations": list(s.citations),
                }
                for s in self.wiki_sources
            ],
            "error_message": self.error_message,
        }


# ─────────────────────────────────────────────────────────────────────────
# 3.2 핵심 클래스 — HybridChatService
# ─────────────────────────────────────────────────────────────────────────


class HybridChatService:
    """라우터 기반 RAG/Wiki 통합 응답 오케스트레이터.

    동작 모드:
        1. router=None:
           → 100% chat_service.respond() 위임. 회귀 테스트 보장 모드.
        2. router 활성:
           → router.classify() → RAG/WIKI/BOTH 분기.

    Threading: chat_service 와 wiki_llm 의 직렬화는 외부 ModelLoadManager 책임.
    라우터의 휴리스틱 매칭은 stateless.

    Args:
        chat_service: 기존 ChatService — duck-typing 으로 respond() 만 호출.
        router: QueryRouter. None 이면 라우팅 비활성 (항상 RAG).
        wiki_store: WikiStore. router 활성 시 WIKI 분기에 필요.
        wiki_llm: WikiLLMClient. WIKI 답변 합성용 (옵션).
            None 이면 페이지 본문 그대로 반환.
    """

    def __init__(
        self,
        chat_service: Any,
        router: QueryRouter | None = None,
        wiki_store: Any | None = None,
        wiki_llm: Any | None = None,
    ) -> None:
        """HybridChatService 인스턴스를 생성한다.

        Args:
            chat_service: respond() 메서드를 가진 객체 (ChatService duck-type).
            router: QueryRouter 인스턴스 또는 None.
            wiki_store: WikiStore 인스턴스 또는 None.
            wiki_llm: WikiLLMClient 인스턴스 또는 None.
        """
        self._chat_service = chat_service
        self._router = router
        self._wiki_store = wiki_store
        self._wiki_llm = wiki_llm

    async def respond(
        self,
        query: str,
        **kwargs: Any,
    ) -> HybridChatResponse:
        """질의에 대한 통합 응답을 생성한다.

        라우터 비활성 시: chat_service.respond() 결과를 HybridChatResponse 로 wrap
        (source_type="rag", router_verdict=default_rag verdict).

        라우터 활성 시:
            1. router.classify(query) 호출
            2. RouteDecision 에 따라 분기:
               - RAG  → chat_service.respond() + wrap
               - WIKI → _synthesize_from_wiki() + wrap
               - BOTH → 둘 다 호출 (asyncio.gather) + 합치기
            3. WIKI 합성 실패 시 자동으로 RAG 폴백 (graceful degradation).

        Args:
            query: 사용자 질문.
            **kwargs: chat_service.respond() 에 전달할 추가 파라미터.

        Returns:
            HybridChatResponse.
        """
        # 1. 라우터 비활성 → 100% chat_service 위임 (PRD §10.3)
        if self._router is None:
            rag_response = await self._chat_service.respond(query, **kwargs)
            return HybridChatResponse(
                source_type="rag",
                router_verdict=_DEFAULT_RAG_VERDICT,
                rag_response=rag_response,
            )

        # 2. 라우터 활성 → 분류
        verdict = await self._router.classify(query)

        if verdict.decision == RouteDecision.RAG:
            return await self._handle_rag(query, verdict, **kwargs)

        if verdict.decision == RouteDecision.WIKI:
            return await self._handle_wiki(query, verdict, **kwargs)

        # BOTH
        return await self._handle_both(query, verdict, **kwargs)

    # ─── 분기 헬퍼 ──────────────────────────────────────────────────────

    async def _handle_rag(
        self,
        query: str,
        verdict: RouterVerdict,
        **kwargs: Any,
    ) -> HybridChatResponse:
        """RAG 분기 — chat_service.respond() 호출."""
        rag_response = await self._chat_service.respond(query, **kwargs)
        return HybridChatResponse(
            source_type="rag",
            router_verdict=verdict,
            rag_response=rag_response,
        )

    async def _handle_wiki(
        self,
        query: str,
        verdict: RouterVerdict,
        **kwargs: Any,
    ) -> HybridChatResponse:
        """WIKI 분기 — wiki_store 에서 페이지 합성. 실패 시 RAG 폴백."""
        try:
            answer, sources = await self._synthesize_from_wiki(query, verdict)
        except Exception as exc:  # noqa: BLE001 — graceful degradation
            logger.warning("WIKI 합성 실패, RAG 폴백 수행: %s", exc, exc_info=True)
            rag_response = await self._chat_service.respond(query, **kwargs)
            return HybridChatResponse(
                source_type="rag",
                router_verdict=verdict,
                rag_response=rag_response,
                error_message=f"wiki_synthesis_failed: {exc}",
            )

        return HybridChatResponse(
            source_type="wiki",
            router_verdict=verdict,
            wiki_answer=answer,
            wiki_sources=sources,
        )

    async def _handle_both(
        self,
        query: str,
        verdict: RouterVerdict,
        **kwargs: Any,
    ) -> HybridChatResponse:
        """BOTH 분기 — RAG + WIKI 병렬 호출 후 합치기."""
        rag_task = self._chat_service.respond(query, **kwargs)
        wiki_task = self._synthesize_from_wiki(query, verdict)

        rag_result, wiki_result = await asyncio.gather(rag_task, wiki_task, return_exceptions=True)

        # RAG 실패 시 — None 으로 두고 error_message 기록
        rag_response: Any = None
        error_msgs: list[str] = []
        if isinstance(rag_result, BaseException):
            logger.warning("BOTH 분기 RAG 호출 실패: %s", rag_result)
            error_msgs.append(f"rag_failed: {rag_result}")
        else:
            rag_response = rag_result

        # WIKI 실패 시 — answer/sources 비움
        wiki_answer: str | None = None
        wiki_sources: list[WikiAnswerSource] = []
        if isinstance(wiki_result, BaseException):
            logger.warning("BOTH 분기 WIKI 합성 실패: %s", wiki_result)
            error_msgs.append(f"wiki_failed: {wiki_result}")
        else:
            wiki_answer, wiki_sources = wiki_result

        return HybridChatResponse(
            source_type="both",
            router_verdict=verdict,
            rag_response=rag_response,
            wiki_answer=wiki_answer,
            wiki_sources=wiki_sources,
            error_message="; ".join(error_msgs) if error_msgs else None,
        )

    # ─── 위키 합성 ──────────────────────────────────────────────────────

    async def _synthesize_from_wiki(
        self,
        query: str,
        verdict: RouterVerdict,
    ) -> tuple[str, list[WikiAnswerSource]]:
        """위키 페이지를 검색·합성해 (answer, sources) 반환한다.

        구현 (Phase 5 최소 동작):
            - wiki_store 가 None 이면 RuntimeError raise → 호출자가 RAG 폴백.
            - wiki_store.all_pages() 로 페이지 목록을 받아 첫 N개 페이지를 읽어
              본문을 이어붙인다 (Phase 5 단순 정책).
            - 페이지가 0개면 RuntimeError raise.
            - wiki_llm 이 있으면 본문을 합성에 사용 (Phase 5.D 확장 영역).
              현재는 페이지 raw 본문을 그대로 answer 로 반환.

        Args:
            query: 사용자 질문.
            verdict: 라우터 결정 (matched_signals 활용 가능).

        Returns:
            (answer, sources) 튜플.

        Raises:
            RuntimeError: wiki_store 가 None 이거나 페이지가 0개일 때.
        """
        if self._wiki_store is None:
            raise RuntimeError("wiki_store_not_provided")

        # 페이지 목록 수집 — all_pages() 는 Iterator[Path] 반환
        try:
            page_paths = list(self._wiki_store.all_pages())
        except Exception as exc:  # noqa: BLE001 — store 실패는 호출자에서 폴백
            raise RuntimeError(f"wiki_store_list_failed: {exc}") from exc

        if not page_paths:
            raise RuntimeError("wiki_no_pages_found")

        # Phase 5 최소 정책: 상위 3개 페이지만 읽어 합치기
        # (Phase 5.D 에서 verdict.matched_signals 기반 정교한 검색 도입 예정)
        max_pages = 3
        sources: list[WikiAnswerSource] = []
        body_parts: list[str] = []

        for rel_path in sorted(page_paths)[:max_pages]:
            try:
                page = self._wiki_store.read_page(rel_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("wiki 페이지 읽기 실패: %s (%s)", rel_path, exc)
                continue

            # 제목 추출 — frontmatter.title > 첫 # 제목 > path stem
            title = self._extract_title(page, rel_path)
            snippet = self._make_snippet(page.content, max_chars=200)
            citation_strs = [f"[meeting:{c.meeting_id}@{c.timestamp_str}]" for c in page.citations]

            sources.append(
                WikiAnswerSource(
                    page_path=str(rel_path),
                    page_type=str(page.page_type),
                    title=title,
                    snippet=snippet,
                    citations=citation_strs,
                )
            )
            body_parts.append(f"## {title}\n\n{page.content.strip()}\n")

        # answer 합성 — wiki_llm 이 있으면 합성, 없으면 raw 결합
        if self._wiki_llm is not None:
            try:
                answer = await self._llm_synthesize(query, body_parts)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM 위키 합성 실패, raw 본문 사용: %s", exc)
                answer = "\n\n".join(body_parts)
        else:
            answer = "\n\n".join(body_parts)

        return answer, sources

    async def _llm_synthesize(
        self,
        query: str,
        body_parts: list[str],
    ) -> str:
        """LLM 으로 위키 본문을 합성한다.

        Args:
            query: 사용자 질문.
            body_parts: 페이지 본문 리스트.

        Returns:
            합성된 답변 문자열.
        """
        system_prompt = (
            "당신은 회의 위키 검색 도우미다. "
            "주어진 위키 페이지 발췌를 근거로 사용자 질문에 한국어로 답하라. "
            "인용 마커 [meeting:id@HH:MM:SS] 는 그대로 유지하라."
        )
        user_prompt = f"질문: {query}\n\n위키 페이지 발췌:\n\n" + "\n\n---\n\n".join(body_parts)
        return await self._wiki_llm.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

    # ─── 정적 헬퍼 ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_title(page: Any, rel_path: Any) -> str:
        """페이지 제목 추출 — frontmatter.title > 첫 # 헤더 > path stem.

        Args:
            page: WikiPage 객체.
            rel_path: 페이지 상대 경로.

        Returns:
            제목 문자열.
        """
        # frontmatter 우선
        fm_title = page.frontmatter.get("title") if page.frontmatter else None
        if fm_title:
            return str(fm_title)

        # 첫 # 헤더 탐색
        for line in page.content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped.lstrip("#").strip()

        # path stem 폴백
        from pathlib import Path

        return Path(str(rel_path)).stem

    @staticmethod
    def _make_snippet(content: str, max_chars: int = 200) -> str:
        """본문에서 frontmatter/헤더 제거 후 max_chars 까지 잘라낸 snippet 생성.

        Args:
            content: 페이지 본문 (frontmatter 는 이미 제외된 상태).
            max_chars: 최대 문자 수.

        Returns:
            snippet 문자열.
        """
        # 빈 줄 + 헤더 라인 스킵 후 첫 본문 추출
        lines = [
            ln.strip()
            for ln in content.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        joined = " ".join(lines)
        if len(joined) <= max_chars:
            return joined
        return joined[: max_chars - 1].rstrip() + "…"

    @staticmethod
    def _wrap_rag(
        rag_response: Any,
        verdict: RouterVerdict,
    ) -> HybridChatResponse:
        """rag_response 를 HybridChatResponse 로 감싸는 정적 헬퍼.

        Args:
            rag_response: chat_service.respond() 결과.
            verdict: RouterVerdict.

        Returns:
            HybridChatResponse (source_type='rag').
        """
        return HybridChatResponse(
            source_type="rag",
            router_verdict=verdict,
            rag_response=rag_response,
        )
