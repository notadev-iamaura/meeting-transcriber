"""DecisionRecord canonical schema and dedupe tests."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

from core.wiki.citations import enforce_citations
from core.wiki.decision_record import DecisionRecord
from core.wiki.extractors.decision import ActionItemRef, DecisionExtractor, ExtractedDecision
from core.wiki.models import Citation
from core.wiki.store import WikiStore


def _citation(ts: str = "00:01:20") -> Citation:
    """테스트용 verified citation 객체."""
    h, m, s = [int(part) for part in ts.split(":")]
    return Citation(
        meeting_id="1234abcd",
        timestamp_str=ts,
        timestamp_seconds=h * 3600 + m * 60 + s,
    )


def _decision(*, slug: str = "q3-launch-date", citation: str | None = None) -> ExtractedDecision:
    """테스트용 ExtractedDecision."""
    marker = citation if citation is not None else "[meeting:1234abcd@00:01:20]"
    return ExtractedDecision(
        title="Q3 출시일 확정",
        slug=slug,
        decision_text=f"Q3 출시일을 7월 15일로 확정했다. {marker}".strip(),
        background=f"릴리즈 리스크를 낮추기 위한 결정이다. {marker}".strip(),
        follow_ups=[
            ActionItemRef(owner="지연", description="릴리즈 캘린더 갱신", citation=_citation())
        ],
        participants=["민수", "지연", "민수"],
        projects=["Apollo"],
        citations=[_citation()],
        confidence=9,
    )


def test_decision_record_frontmatter_필수_필드를_렌더한다() -> None:
    """DW-B01: canonical decision frontmatter 필수 필드를 안정적으로 포함한다."""
    record = DecisionRecord.from_extracted(
        decision=_decision(),
        meeting_id="1234abcd",
        meeting_date=date(2026, 5, 21),
    )

    rendered = record.to_markdown()

    for field in (
        "id:",
        "title:",
        "status:",
        "decision_date:",
        "project:",
        "participants:",
        "owners:",
        "confidence:",
        "source_meetings:",
        "supersedes:",
        "superseded_by:",
        "last_updated:",
    ):
        assert field in rendered
    assert "participants: [민수, 지연]" in rendered
    assert "owners: [지연]" in rendered
    assert "## 결정 내용" in rendered
    assert "## 근거" in rendered


def test_8자리_hex가_아닌_citation은_verified_candidate로_보지_않는다() -> None:
    """DW-B02: citation 형식이 엄격하지 않으면 accepted candidate가 될 수 없다."""
    decision = ExtractedDecision(
        title="Q3 출시일 확정",
        slug="q3-launch-date",
        decision_text="Q3 출시일을 확정했다. [meeting:not-hex@00:01:20]",
        background="근거도 잘못된 citation이다. [meeting:not-hex@00:01:20]",
        follow_ups=[],
        participants=["민수"],
        projects=["Apollo"],
        citations=[],
        confidence=9,
    )

    record = DecisionRecord.from_extracted(
        decision=decision,
        meeting_id="not-hex",
        meeting_date=date(2026, 5, 21),
    )

    assert record.has_verified_candidate_citation is False
    assert record.citations == []


def test_follow_up_owner와_citation을_보존한다() -> None:
    """DW-B04: 후속 액션 owner와 citation이 markdown에 보존된다."""
    record = DecisionRecord.from_extracted(
        decision=_decision(),
        meeting_id="1234abcd",
        meeting_date=date(2026, 5, 21),
    )

    rendered = record.to_markdown()

    assert "지연: 릴리즈 캘린더 갱신 [meeting:1234abcd@00:01:20]" in rendered
    assert rendered.count("[meeting:1234abcd@00:01:20]") >= 2


def test_decision_record는_본문과_배경_누락_인용을_후보_citation으로_보강한다() -> None:
    """LLM 이 일부 줄의 인용을 누락해도 canonical 렌더는 D1 통과 가능한 형태를 만든다."""
    decision = _decision(citation="")

    record = DecisionRecord.from_extracted(
        decision=decision,
        meeting_id="1234abcd",
        meeting_date=date(2026, 5, 21),
    )

    rendered = record.to_markdown()

    assert "Q3 출시일을 7월 15일로 확정했다. [meeting:1234abcd@00:01:20]" in rendered
    assert "릴리즈 리스크를 낮추기 위한 결정이다. [meeting:1234abcd@00:01:20]" in rendered
    _cleaned, rejected = enforce_citations(rendered, "1234abcd")
    assert rejected == []


def test_existing_decision_slug는_기존_경로를_재사용하고_source_meetings를_누적한다(
    tmp_path: Path,
) -> None:
    """DW-B03/B05: 같은 slug는 기존 page를 갱신하고 created_at/source를 보존한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("decisions/2026-05-01-q3-launch-date.md"),
        """---
type: decision
id: decision-2026-05-01-q3-launch-date
title: Q3 출시일 확정
status: decided
decision_date: 2026-05-01
project: Apollo
participants: [민수]
owners: [지연]
confidence: 8
source_meetings: [aaaa1111]
created_at: 2026-05-01T09:00:00
last_updated: 2026-05-01T09:00:00
---

# Q3 출시일 확정
기존 결정 [meeting:aaaa1111@00:01:00]
<!-- confidence: 8 -->
""",
    )
    extractor = DecisionExtractor(llm=None)  # type: ignore[arg-type]

    pages = asyncio.run(
        extractor.render_pages(
            decisions=[_decision()],
            meeting_id="1234abcd",
            meeting_date=date(2026, 5, 21),
            existing_store=store,
        )
    )

    assert len(pages) == 1
    rel_path, content = pages[0]
    assert rel_path == "decisions/2026-05-01-q3-launch-date.md"
    assert "id: decision-2026-05-01-q3-launch-date" in content
    assert "created_at: 2026-05-01T09:00:00" in content
    assert "source_meetings: [1234abcd, aaaa1111]" in content
