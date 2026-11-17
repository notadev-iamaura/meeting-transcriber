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
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from core.chat_scope import ChatSourceScope
from core.wiki.router import QueryRouter, RouteDecision, RouterVerdict

logger = logging.getLogger(__name__)

_QUOTED_TOPIC = re.compile(
    r"(?<!\w)(?:'([^'\r\n]+)'|\"([^\"\r\n]+)\"|‘([^’\r\n]+)’|“([^”\r\n]+)”)",
)
_TOPIC_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class _WikiStatement:
    """게시 문장의 내용과 그 문장에 원래 붙어 있던 원문 인용을 묶는다."""

    text: str
    primary: str
    citations: tuple[str, ...]
    witness: str = ""
    context: tuple[str, ...] = ()


def _evidence_tokens(text: str) -> set[str]:
    """조사·띄어쓰기 차이를 완화한 토큰을 만든다. 인용 순위에만 사용한다."""
    normalized = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())
    return {normalized[index : index + 2] for index in range(len(normalized) - 1)}


def _statement_text(text: str) -> str:
    """내용을 바꾸지 않는 목록·공백·문장 끝 표기만 정규화한다.

    소수점·음수·비교 연산자 같은 내용상의 기호는 보존한다. 의역의 의미를
    문자열로 추정하지 않으며, 확인할 수 없으면 게시 문장으로 돌아간다.
    """
    plain = re.sub(r"^\s*[-*]\s+", "", text).strip().rstrip(".。!")
    return re.sub(r"\s+", "", plain)


def _filler_utterance(text: str) -> bool:
    """내용 없는 맞장구·군말을 단독 사실 근거에서 제외한다."""
    normalized = re.sub(r"[\W_]+", "", text)
    return bool(re.fullmatch(r"(?:네|예|음|어|아|응)+", normalized)) or normalized in {
        "네",
        "예",
        "음",
        "어",
        "아",
        "그뭐지",
        "그뭐지요",
        "알겠습니다",
        "감사합니다",
        "맞아요",
        "맞습니다",
        "그렇죠",
        "그렇습니다",
    }


def _question_utterance(text: str) -> bool:
    """확인 질문은 맥락으로 보존하되 확정 진술의 주 인용으로 고르지 않는다."""
    return text.rstrip(" \t\r\n\"'”’").endswith(("?", "？"))


def _matches_quoted_topic(query: str, title: str, content: str) -> bool:
    """명시한 따옴표 주제의 단어가 실제 게시 본문에 있는지 확인한다.

    일반 질문의 의미 검색은 유지한다. 따옴표 주제가 있으면 검색 의도어만
    일치한 다른 결정을 합성하지 않으며, 색인 snippet과 숨김 메타데이터는
    주제 일치의 근거로 삼지 않는다.
    """
    from core.wiki.citations import CITATION_PATTERN

    topics = [
        next(group for group in match.groups() if group) for match in _QUOTED_TOPIC.finditer(query)
    ]
    if len(topics) != 1:
        # 여러 주제를 비교하는 질문은 서로 다른 문서를 함께 검색할 수 있다.
        return True
    tokens = _TOPIC_TOKEN.findall(unicodedata.normalize("NFKC", " ".join(topics)).casefold())
    if not tokens:
        return True
    visible = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    visible = CITATION_PATTERN.sub("", visible)
    actual = unicodedata.normalize("NFKC", f"{title}\n{visible}").casefold()
    for token in tokens:
        if token.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", actual) is None:
                return False
        elif token not in actual:
            return False
    return True


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
        citations: 페이지에 포함된 [meeting:{회의 ID}@HH:MM:SS] 마커 raw 문자열 리스트.
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
        config: Any | None = None,
    ) -> None:
        """HybridChatService 인스턴스를 생성한다.

        Args:
            chat_service: respond() 메서드를 가진 객체 (ChatService duck-type).
            router: QueryRouter 인스턴스 또는 None.
            wiki_store: WikiStore 인스턴스 또는 None.
            wiki_llm: WikiLLMClient 인스턴스 또는 None.
            config: AppConfig (G1 시맨틱 검색용). None 이면 위키 검색은 BM25-only
                — get_config() 싱글톤을 쓰지 않아 테스트가 실 ChromaDB 를 오염시키지 않는다.
                운영(api/routers/search_chat)은 app config 를 주입한다.
        """
        self._chat_service = chat_service
        self._router = router
        self._wiki_store = wiki_store
        self._wiki_llm = wiki_llm
        self._config = config

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
        if self._router is None or any(
            kwargs.get(key) for key in ("meeting_id_filter", "date_filter", "speaker_filter")
        ):
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
            rag_response = await self._chat_service.respond(
                query, **{**kwargs, "source_scope": ChatSourceScope.WIKI_FALLBACK}
            )
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

        gathered: tuple[Any, Any] = cast(
            tuple[Any, Any],
            await asyncio.gather(rag_task, wiki_task, return_exceptions=True),
        )
        rag_result, wiki_result = gathered

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

        구현:
            - wiki_store 가 None 이면 RuntimeError raise → 호출자가 RAG 폴백.
            - FTS5/BM25 검색 결과만 컨텍스트로 사용한다.
            - 검색 결과가 0개면 RuntimeError raise.
            - wiki_llm 이 있으면 검색된 본문으로 합성하되, citation 을 잃으면
              검색 결과 기반 fallback 을 반환한다.

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

        from config import WikiRankingConfig  # noqa: PLC0415
        from core.wiki.search_index import WikiSearchIndex  # noqa: PLC0415

        # 검색 UI는 검토 중/과거 결정을 모두 보여주지만, 답변 근거는 게시된
        # 현재 결정만 쓴다. 먼저 기존 후보 풀 크기만큼 제한해서 가져와야 상위
        # 초안 5개가 실제 게시된 결정을 모두 가리는 일을 피할 수 있다.
        source_limit = 5
        candidate_limit = max(source_limit, WikiRankingConfig().candidate_pool)

        try:
            index = WikiSearchIndex(self._wiki_store.root)
            index.rebuild(self._wiki_store)
            # G1 — config 주입 + semantic 활성 시 하이브리드(벡터+BM25). config 미주입
            # (예: 단위 테스트)이면 BM25-only — 실 ChromaDB 접근/오염 없음. 벡터 미색인이면
            # 내부에서 e5 로드 없이 BM25 폴백 → 응답 동일.
            cfg = self._config
            if cfg is not None and cfg.wiki.semantic.enabled:
                from core.wiki.semantic_index import WikiSemanticIndex  # noqa: PLC0415
                from core.wiki.semantic_search import wiki_hybrid_search  # noqa: PLC0415

                semantic_index = WikiSemanticIndex(
                    cfg.paths.resolved_chroma_db_dir,
                    collection_name=cfg.wiki.semantic.collection_name,
                )
                search_results = await wiki_hybrid_search(
                    query,
                    search_index=index,
                    semantic_index=semantic_index,
                    config=cfg,
                    top_k=candidate_limit,
                    page_types=["decision"],
                )
            else:
                search_results = index.search(
                    query, top_k=candidate_limit, page_types=["decision"]
                )
        except Exception as exc:  # noqa: BLE001 — 검색 실패는 호출자에서 폴백
            raise RuntimeError(f"wiki_search_failed: {exc}") from exc

        if not search_results:
            raise RuntimeError("wiki_no_search_results")

        sources: list[WikiAnswerSource] = []
        body_parts: list[str] = []
        statements: list[_WikiStatement] = []
        verifier: Any | None = None
        if self._config is not None:
            from core.wiki.citation_verifier import CheckpointCitationVerifier

            verifier = CheckpointCitationVerifier(
                checkpoints_dir=self._config.paths.resolved_checkpoints_dir
            )

        for result in search_results:
            rel_path = Path(result.page_path)
            if (
                rel_path.is_absolute()
                or not rel_path.parts
                or rel_path.parts[0] != "decisions"
                or ".." in rel_path.parts
                or rel_path.suffix != ".md"
            ):
                continue
            try:
                page = self._wiki_store.read_page(rel_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("wiki 페이지 읽기 실패: %s (%s)", rel_path, exc)
                continue

            # 색인 메타데이터는 오래되거나 잘못될 수 있으므로 방금 읽은 원본
            # 페이지의 게시 상태를 최종 판정한다. legacy confirmed도 인정한다.
            metadata = page.frontmatter
            status = metadata.get("status")
            superseded_by = metadata.get("superseded_by")
            if (
                metadata.get("type") != "decision"
                or not isinstance(status, str)
                or status.strip().lower() not in {"decided", "confirmed"}
                or (
                    superseded_by is not None
                    and not (isinstance(superseded_by, str) and not superseded_by.strip())
                )
                or not page.citations
            ):
                continue

            title = self._extract_title(page, rel_path)
            if not _matches_quoted_topic(query, title, page.content):
                continue
            if self._config is not None:
                from core.search_revision import source_was_edited

                if any(source_was_edited(self._config, c.meeting_id) for c in page.citations):
                    # 편집 전 원문에서 합성한 페이지를 최신 결정으로 답하지 않는다.
                    continue
            page_statements = await self._prepare_statements(page.content, verifier)
            if not page_statements:
                continue
            # fallback과 출처에도 낡은 색인 snippet/citation을 재사용하지 않는다.
            snippet = self._make_snippet(page_statements[0].text, max_chars=200)
            citation_strs = list(
                dict.fromkeys(marker for item in page_statements for marker in item.citations)
            )

            sources.append(
                WikiAnswerSource(
                    page_path=str(rel_path),
                    page_type="decision",
                    title=title,
                    snippet=snippet,
                    citations=citation_strs,
                )
            )
            evidence_parts = []
            for item in page_statements:
                evidence_parts.append(
                    f"게시된 사실: {item.text}\n주 인용: {item.primary}\n"
                    f"직접 원문: {item.witness}\n" + "\n".join(item.context)
                )
            body_parts.append(f"## {title}\n\n" + "\n\n".join(evidence_parts))
            statements.extend(page_statements)
            if len(sources) >= source_limit:
                break

        if not body_parts:
            raise RuntimeError("wiki_no_readable_search_results")

        fallback_answer = self._build_search_fallback_answer(query, sources, statements)

        # answer 합성 — wiki_llm 이 있으면 합성, 없으면 raw 결합
        if self._wiki_llm is not None:
            try:
                answer = await self._llm_synthesize(query, body_parts)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM 위키 합성 실패, 검색 결과 fallback 사용: %s", exc)
                answer = fallback_answer
        else:
            answer = fallback_answer

        if not self._answer_has_statement_citations(answer, sources, statements):
            logger.info("LLM 위키 답변의 문장별 인용이 불완전해 게시 문장 fallback 사용")
            answer = fallback_answer

        return answer, sources

    @staticmethod
    async def _prepare_statements(content: str, verifier: Any | None) -> list[_WikiStatement]:
        """문장에 딸린 기존 인용만 정리하고 직접 발화를 앞에 둔 읽기 전용 입력을 만든다.

        토큰 겹침은 이미 같은 문장에 인용된 원문들의 순위를 정하는 용도다.
        이것을 의미 검수나 새로운 인용의 근거로 사용하지 않는다. 확인 질문은
        후속 약속의 범위를 설명할 수 있으므로 보조 문맥으로 보존한다.
        """
        from core.wiki.citations import CITATION_PATTERN, is_factual_statement

        prepared = []
        evidence_cache: dict[str, list[str]] = {}
        for line in content.splitlines():
            matches = list(CITATION_PATTERN.finditer(line))
            if not matches:
                continue
            text = re.sub(r"^\s*[-*]\s+", "", line[: matches[0].start()]).strip()
            if not text or not is_factual_statement(text):
                continue
            markers = list(dict.fromkeys(match.group(0) for match in matches))
            if verifier is None:
                # config가 없는 독립 사용자는 게시 문장의 기존 인용만 보존한다.
                prepared.append(_WikiStatement(text, markers[0], tuple(markers)))
                continue
            direct: list[tuple[str, str]] = []
            contexts: list[tuple[str, str]] = []
            for match in matches:
                marker = match.group(0)
                if marker not in evidence_cache:
                    seconds = int(match[2]) * 3600 + int(match[3]) * 60 + int(match[4])
                    evidence = await verifier.fetch_evidence(match[1], seconds)
                    segments = evidence.get("segments", []) if isinstance(evidence, dict) else []
                    evidence_cache[marker] = [
                        f"[{segment.get('speaker_id') or '화자 미확인'}] {segment['text']}"
                        for segment in segments
                        if isinstance(segment, dict)
                        and isinstance(segment.get("text"), str)
                        and not _filler_utterance(segment["text"])
                    ]
                for source_text in evidence_cache[marker]:
                    item = (marker, source_text)
                    if item in contexts:
                        continue
                    contexts.append(item)
                    if not _question_utterance(source_text):
                        direct.append(item)
            if not direct:
                # 질문만으로 게시 사실을 확인했다고 답하지 않는다.
                continue
            claim_tokens = _evidence_tokens(text)
            primary, witness = max(
                direct, key=lambda item: len(claim_tokens & _evidence_tokens(item[1]))
            )
            citations = tuple(dict.fromkeys([primary, *(marker for marker, _ in contexts)]))
            supporting = tuple(
                f"{'확인 질문 문맥' if _question_utterance(source_text) else '보조 원문'}: "
                f"{source_text} {marker}"
                for marker, source_text in contexts
                if (marker, source_text) != (primary, witness)
            )
            prepared.append(_WikiStatement(text, primary, citations, witness, supporting))
        return prepared

    @staticmethod
    def _answer_has_statement_citations(
        answer: str, sources: list[WikiAnswerSource], statements: list[_WikiStatement]
    ) -> bool:
        """모든 사실 줄이 허용된 직접 원문 인용을 갖는지 검사한다. 의미 증명은 아니다."""
        from core.wiki.citations import CITATION_PATTERN

        allowed = {marker for source in sources for marker in source.citations}
        primaries = {statement.primary for statement in statements}
        titles = {source.title for source in sources}
        if "<!--" in answer:
            return False
        if any(match.group(0) not in allowed for match in CITATION_PATTERN.finditer(answer)):
            return False
        factual_lines = 0
        for line in answer.splitlines():
            if line.strip().startswith("#"):
                if line.strip().lstrip("#").strip() not in titles:
                    return False
                continue
            if not line.strip():
                continue
            matches = list(CITATION_PATTERN.finditer(line))
            if not CITATION_PATTERN.sub("", line).strip(" -*:，,."):
                continue
            if not matches:
                return False
            groups: list[tuple[str, list[str]]] = []
            last_end = 0
            for match in matches:
                fragment = re.sub(r"^\s*[-*]\s+", "", line[last_end : match.start()]).strip()
                if re.search(r"\w", fragment):
                    groups.append((fragment, []))
                if not groups:
                    return False
                groups[-1][1].append(match.group(0))
                last_end = match.end()
            if line[last_end:].strip(" -*:，,.!?"):
                return False
            for text, markers in groups:
                if _question_utterance(text):
                    return False
                if re.search(r"[.!?](?:\s+\S|[가-힣A-Z])", text):
                    # 한 인용 뒤에 여러 사실 문장을 묶어 근거를 상속하지 않는다.
                    return False
                if not any(marker in primaries for marker in markers):
                    return False
                if not any(
                    _statement_text(text) == _statement_text(item.text)
                    and item.primary in markers
                    and set(markers).issubset(item.citations)
                    for item in statements
                ):
                    # 유효한 인용의 존재를 새 주장이나 다른 문장의 지지로 간주하지 않는다.
                    return False
            factual_lines += 1
        return factual_lines > 0

    @staticmethod
    def _answer_preserves_any_citation(answer: str, sources: list[WikiAnswerSource]) -> bool:
        """답변이 검색 근거 citation 을 하나 이상 보존했는지 확인한다."""
        citation_markers = [marker for source in sources for marker in source.citations]
        if not citation_markers:
            return True
        return any(marker in answer for marker in citation_markers)

    @staticmethod
    def _build_search_fallback_answer(
        query: str,
        sources: list[WikiAnswerSource],
        statements: list[_WikiStatement] | None = None,
    ) -> str:
        """LLM 이 citation 을 잃을 때 쓰는 검색 결과 기반 답변을 만든다."""
        if statements:
            return "공개 Wiki에 기록된 내용입니다.\n\n" + "\n".join(
                f"- {item.text} {' '.join(item.citations)}" for item in statements
            )
        lines: list[str] = [f"질문과 관련된 Decision Wiki 검색 결과입니다: {query}"]
        for idx, source in enumerate(sources, start=1):
            citations = ", ".join(source.citations[:3])
            snippet = source.snippet.strip()
            if citations:
                lines.append(f"{idx}. {source.title}: {snippet} {citations}")
            else:
                lines.append(f"{idx}. {source.title}: {snippet}")
        return "\n".join(lines)

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
            "인용 마커 [meeting:{회의 ID}@HH:MM:SS] 는 그대로 유지하라. "
            "한 줄에 하나의 사실 문장만 쓰고, 각 문장 끝에 그 사실의 주 인용을 붙여라. "
            "확인 질문 문맥은 약속의 대상이나 조건을 설명할 수 있지만 그 자체를 확정으로 바꾸지 마라. "
            "게시된 사실의 범위·담당자·기한을 새로 만들거나 강화하지 마라."
        )
        user_prompt = f"질문: {query}\n\n위키 페이지 발췌:\n\n" + "\n\n---\n\n".join(body_parts)
        wiki_llm = self._wiki_llm
        if wiki_llm is None:
            return "\n\n".join(body_parts)
        return cast(
            str,
            await wiki_llm.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            ),
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
                return str(stripped.lstrip("#").strip())

        # path stem 폴백
        from pathlib import Path

        return str(Path(str(rel_path)).stem)

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
