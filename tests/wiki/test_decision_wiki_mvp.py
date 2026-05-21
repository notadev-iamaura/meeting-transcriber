"""Decision Wiki MVP 평가/회귀 테스트."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from core.wiki.chat_integration import HybridChatService
from core.wiki.decision_record import DecisionRecord
from core.wiki.extractors.decision import ActionItemRef, ExtractedDecision
from core.wiki.models import Citation
from core.wiki.router import RouteDecision, RouterVerdict
from core.wiki.search_index import WikiSearchIndex
from core.wiki.store import WikiStore


def _load_gold() -> dict[str, Any]:
    """Decision Wiki gold fixture 를 로드한다."""
    fixture = Path(__file__).parent / "fixtures" / "decision_wiki_gold.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def _seed_decision(store: WikiStore) -> None:
    """verified citation 을 가진 canonical decision page 를 만든다."""
    store.write_page(
        Path("decisions/2026-05-21-q3-launch-date.md"),
        """---
type: decision
id: decision-2026-05-21-q3-launch-date
title: Q3 출시일 확정
status: decided
decision_date: 2026-05-21
project: Apollo
participants: [민수, 지연]
owners: [지연]
confidence: 9
source_meetings: [1234abcd]
supersedes: []
superseded_by:
last_updated: 2026-05-21T10:00:00
---

# Q3 출시일 확정

## 결정 내용
Q3 출시일을 7월 15일로 확정했다. [meeting:1234abcd@00:01:20]
""",
    )


def test_decision_record는_verified_citation이_있는_decision만_canonical로_렌더한다() -> None:
    """accepted decision 은 8자리 hex citation 을 frontmatter/body 에 보존한다."""
    gold = _load_gold()
    citation = Citation(
        meeting_id=gold["meeting_id"],
        timestamp_str="00:01:20",
        timestamp_seconds=80,
    )
    decision = ExtractedDecision(
        title="Q3 출시일 확정",
        slug="q3-launch-date",
        decision_text=f"Q3 출시일을 7월 15일로 확정했다. {gold['expected_decisions'][0]['citation']}",
        background="일정 리스크를 낮추기 위해 단일 날짜로 합의했다.",
        participants=["민수", "지연"],
        projects=["Apollo"],
        follow_ups=[
            ActionItemRef(owner="지연", description="릴리즈 캘린더 갱신", citation=citation)
        ],
        citations=[citation],
        confidence=9,
    )

    record = DecisionRecord.from_extracted(
        decision=decision,
        meeting_id=gold["meeting_id"],
        meeting_date=date(2026, 5, 21),
    )

    rendered = record.to_markdown()
    assert record.has_verified_candidate_citation is True
    assert "id: decision-2026-05-21-q3-launch-date" in rendered
    assert "status: decided" in rendered
    assert "source_meetings: [1234abcd]" in rendered
    assert gold["expected_decisions"][0]["citation"] in rendered


def test_wiki_search_bm25_recall_at_1과_필터를_검증한다(tmp_path: Path) -> None:
    """gold query 가 기대 decision 을 recall@1 로 찾아야 한다."""
    gold = _load_gold()
    wiki_root = tmp_path / "wiki"
    store = WikiStore(wiki_root)
    store.init_repo()
    _seed_decision(store)
    store.write_page(
        Path("decisions/2026-05-21-budget.md"),
        "---\ntype: decision\ntitle: 예산 보류\nstatus: pending\nconfidence: 5\n---\n\n# 예산 보류\n",
    )

    index = WikiSearchIndex(wiki_root)
    assert index.rebuild(store) >= 2
    results = index.search(
        gold["query"],
        page_types=["decision"],
        status="decided",
        project="Apollo",
        participant="민수",
        min_confidence=7,
        top_k=1,
    )

    assert len(results) == 1
    assert results[0].page_path == "decisions/2026-05-21-q3-launch-date.md"
    assert gold["expected_decisions"][0]["citation"] in results[0].citations


@dataclass
class _FakeChatResponse:
    answer: str


class _FakeChat:
    def __init__(self) -> None:
        self.calls = 0

    async def respond(self, query: str, **kwargs: Any) -> _FakeChatResponse:
        self.calls += 1
        return _FakeChatResponse(answer=f"rag:{query}")


class _FakeRouter:
    def __init__(self, verdict: RouterVerdict) -> None:
        self.verdict = verdict

    async def classify(self, query: str) -> RouterVerdict:
        return self.verdict


class _CitationDroppingLLM:
    async def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        return "근거 표시는 생략하고 Q3 출시일은 확정됐습니다."


def test_decision_chat은_bm25_결과만_근거로_쓰고_citation_누락시_fallback한다(
    tmp_path: Path,
) -> None:
    """Wiki 답변은 검색된 decision source 만 쓰고 citation fallback 을 보존한다."""
    wiki_root = tmp_path / "wiki"
    store = WikiStore(wiki_root)
    store.init_repo()
    _seed_decision(store)

    verdict = RouterVerdict(
        decision=RouteDecision.WIKI,
        confidence=9,
        reason="decision wiki query",
        matched_signals=["time_range_decisions"],
        used_llm=False,
    )
    chat = _FakeChat()
    service = HybridChatService(
        chat_service=chat,
        router=_FakeRouter(verdict),
        wiki_store=store,
        wiki_llm=_CitationDroppingLLM(),
    )

    response = asyncio.run(service.respond("Q3 출시일 결정 알려줘"))

    assert response.source_type == "wiki"
    assert chat.calls == 0
    assert response.wiki_sources
    assert response.wiki_sources[0].page_path == "decisions/2026-05-21-q3-launch-date.md"
    assert "[meeting:1234abcd@00:01:20]" in (response.wiki_answer or "")


def test_decision_chat은_정렬상_앞선_무관한_페이지가_아닌_bm25_결과만_사용한다(
    tmp_path: Path,
) -> None:
    """DW-G01: first-3-pages 정책이 아닌 검색 관련도 기반 source를 사용한다."""
    wiki_root = tmp_path / "wiki"
    store = WikiStore(wiki_root)
    store.init_repo()
    for idx in range(3):
        store.write_page(
            Path(f"decisions/2026-05-01-irrelevant-{idx}.md"),
            f"""---
type: decision
title: 무관한 예산 결정 {idx}
status: decided
decision_date: 2026-05-01
confidence: 9
source_meetings: [aaaa1111]
last_updated: 2026-05-01T10:00:00
---

# 무관한 예산 결정 {idx}
예산 항목만 논의했다. [meeting:aaaa1111@00:00:0{idx}]
<!-- confidence: 9 -->
""",
        )
    _seed_decision(store)

    verdict = RouterVerdict(
        decision=RouteDecision.WIKI,
        confidence=9,
        reason="decision wiki query",
        matched_signals=["time_range_decisions"],
        used_llm=False,
    )
    service = HybridChatService(
        chat_service=_FakeChat(),
        router=_FakeRouter(verdict),
        wiki_store=store,
        wiki_llm=None,
    )

    response = asyncio.run(service.respond("Q3 출시일 결정 알려줘"))

    assert response.source_type == "wiki"
    assert response.wiki_sources
    assert response.wiki_sources[0].page_path == "decisions/2026-05-21-q3-launch-date.md"
    assert all("irrelevant" not in src.page_path for src in response.wiki_sources[:1])


def test_router_disabled는_기존_rag_응답만_사용한다() -> None:
    """router=None 기본값은 기존 RAG 동작을 바꾸지 않는다."""
    chat = _FakeChat()
    service = HybridChatService(chat_service=chat, router=None)

    response = asyncio.run(service.respond("이번 회의 요약"))

    assert response.source_type == "rag"
    assert response.rag_response.answer == "rag:이번 회의 요약"
    assert chat.calls == 1
