"""DecisionRecord canonical schema and dedupe tests."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.wiki.citations import WikiGuardError, enforce_citations
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


def test_decision_record_preserves_uncited_fields_without_borrowing_follow_up_citation() -> None:
    """후속 액션의 시각을 본문·배경에 붙이지 않고 미해결 원문 그대로 보류한다."""
    decision = _decision(citation="")

    record = DecisionRecord.from_extracted(
        decision=decision,
        meeting_id="1234abcd",
        meeting_date=date(2026, 5, 21),
    )

    rendered = record.to_markdown()

    assert record.decision_text == decision.decision_text
    assert record.background == decision.background
    assert record.citation_unresolved is True
    assert record.unresolved_citation_fields == ("background", "decision_text")
    assert record.status == "pending"
    assert "status: pending" in rendered
    assert "review_reason: citation_unresolved" in rendered
    assert "citation_unresolved: true" in rendered
    assert f"## 결정 내용\n{decision.decision_text}\n" in rendered
    assert f"## 배경\n{decision.background}\n" in rendered
    assert "지연: 릴리즈 캘린더 갱신 [meeting:1234abcd@00:01:20]" in rendered
    with pytest.raises(WikiGuardError, match="too_many_uncited_statements"):
        enforce_citations(rendered, "1234abcd")


def test_record_never_borrows_decision_citation_for_background_or_uncited_line() -> None:
    """한 필드의 정상 인용이나 같은 필드의 첫 인용으로 다른 사실을 보강하지 않는다."""
    decision = _decision()
    decision.background = (
        "  첫 배경 설명 [meeting:1234abcd@00:02:00]\n두 번째 배경 설명은 미인용이다.  "
    )
    record = DecisionRecord.from_extracted(
        decision=decision, meeting_id="1234abcd", meeting_date=date(2026, 5, 21)
    )
    assert record.background == decision.background
    assert record.decision_text == decision.decision_text
    assert record.unresolved_citation_fields == ("background",)
    assert record.citation_unresolved is True


@pytest.mark.parametrize("background", ["", "없음", "- 없음", "_(없음)_", "N/A", "없음."])
def test_empty_or_placeholder_background_does_not_require_invented_citation(
    background: str,
) -> None:
    """빈 배경·없음 표시는 근거가 빠진 사실로 취급하거나 인용을 새로 붙이지 않는다."""
    decision = _decision()
    decision.background = background
    record = DecisionRecord.from_extracted(
        decision=decision, meeting_id="1234abcd", meeting_date=date(2026, 5, 21)
    )
    assert record.background == background
    assert record.citation_unresolved is False
    assert record.unresolved_citation_fields == ()
    assert record.status == "decided"
    assert "review_reason: citation_unresolved" not in record.to_markdown()


@pytest.mark.parametrize("citation", [None, SimpleNamespace(meeting_id="1234abcd")])
def test_uncited_follow_up_is_preserved_without_zero_or_other_field_fallback(
    citation: object,
) -> None:
    """누락·불완전 후속 인용은 실제 설명을 남기고 다른 시각이나 0초를 만들지 않는다."""
    decision = _decision()
    decision.follow_ups = [
        SimpleNamespace(
            owner="가상 담당자", description="가상 목록을 정리한다.", citation=citation
        )
    ]
    record = DecisionRecord.from_extracted(
        decision=decision, meeting_id="1234abcd", meeting_date=date(2026, 5, 21)
    )
    assert record.follow_ups == ["가상 담당자: 가상 목록을 정리한다."]
    assert record.unresolved_citation_fields == ("follow_ups.0",)
    assert record.citation_unresolved is True
    assert "00:00:00" not in record.to_markdown()


def test_fully_uncited_candidate_and_extractor_unresolved_flags_remain_reviewable() -> None:
    """전체 무인용도 본문을 버리지 않고, 추출기가 전달한 미해결 필드 역시 유지한다."""
    decision = SimpleNamespace(
        decision_text="가상 행사 배치를 정했다.",
        background="",
        follow_ups=[],
        unresolved_citation_fields=("follow_ups.2",),
        citation_unresolved=True,
    )
    record = DecisionRecord.from_extracted(
        decision=decision, meeting_id="1234abcd", meeting_date=date(2026, 5, 21)
    )
    assert record.decision_text == decision.decision_text
    assert record.citations == []
    assert record.has_verified_candidate_citation is False
    assert record.unresolved_citation_fields == ("decision_text", "follow_ups.2")
    assert record.citation_unresolved is True
    assert "가상 행사 배치를 정했다." in record.to_markdown()


def test_many_uncited_lines_remain_pending_without_running_d1_overflow_guard() -> None:
    """다수 무인용도 렌더 예외·삭제 없이 본문과 배경 원문 전체를 보존한다."""
    decision = SimpleNamespace(
        decision_text="가상 안건 첫 문장.\n가상 안건 두 번째 문장.\n가상 안건 세 번째 문장.",
        background="첫 배경 설명.\n두 번째 배경 설명.\n세 번째 배경 설명.",
        follow_ups=[],
    )
    record = DecisionRecord.from_extracted(
        decision=decision, meeting_id="1234abcd", meeting_date=date(2026, 5, 21)
    )
    assert record.decision_text == decision.decision_text
    assert record.background == decision.background
    assert record.citation_unresolved is True
    assert record.unresolved_citation_fields == ("background", "decision_text")
    rendered = record.to_markdown()
    assert decision.decision_text in rendered and decision.background in rendered
    assert "status: pending" in rendered


def test_follow_up_inline_marker_is_preserved_without_requiring_timestamp_field() -> None:
    """인용 보강이 설명에 반환한 실제 복수 마커는 변형하거나 첫 시각으로 대체하지 않는다."""
    decision = SimpleNamespace(**vars(_decision()))
    description = "가상 목록을 정리한다. [meeting:1234abcd@00:02:00] [meeting:1234abcd@00:02:10]"
    decision.follow_ups = [
        SimpleNamespace(owner="가상 담당자", description=description, citation=None)
    ]
    record = DecisionRecord.from_extracted(
        decision=decision, meeting_id="1234abcd", meeting_date=date(2026, 5, 21)
    )
    assert record.follow_ups == [f"가상 담당자: {description}"]
    assert record.citation_unresolved is False
    assert record.unresolved_citation_fields == ()
    assert "[meeting:1234abcd@00:02:00]" in record.citations
    assert "[meeting:1234abcd@00:02:10]" in record.citations


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
