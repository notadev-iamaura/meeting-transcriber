"""실제 재검수에서 확인한 액션 중복과 완료 업무 재등장을 검증한다."""

from dataclasses import replace

import pytest

from core.wiki.extractors.action_item import (
    ActionItemExtractor,
    ClosedActionItem,
    NewActionItem,
    OpenActionItem,
)
from core.wiki.llm_client import MockWikiLLMClient
from core.wiki.models import Citation


def _original() -> OpenActionItem:
    """출처와 기한이 있는 기존 업무를 만든다."""
    return OpenActionItem(
        item_id="item-1",
        owner="SPEAKER_01",
        description="검토 자료 전달",
        from_meeting_id="meeting_20260528_100000",
        from_date="2026-05-28",
        citation=Citation("meeting_20260528_100000", "00:01:20", 80),
        due_date="2026-05-29",
        project_slug="review",
    )


def _replay(item: OpenActionItem) -> NewActionItem:
    """같은 회의에서 다시 추출된 업무를 만든다."""
    return NewActionItem(
        owner=item.owner,
        description=item.description,
        citation=item.citation,
        due_date=item.due_date,
        project_slug=item.project_slug,
        confidence=9,
    )


@pytest.mark.asyncio
async def test_replay_collapses_identical_existing_and_new_actions() -> None:
    """기존 중복과 재추출 중복은 한 업무로 표시하고 입력 원장은 바꾸지 않는다."""
    llm = MockWikiLLMClient()
    original = _original()
    existing = [original, replace(original, item_id="old-duplicate")]
    new = [_replay(original), _replay(original)]
    rendered = await ActionItemExtractor(llm).render_unified_page(
        existing_open=existing,
        new_open=new,
        existing_closed=[],
        newly_closed=[],
        last_compiled_at="2026-11-04T12:00:00",
    )
    assert "## Open (1)" in rendered
    assert rendered.count("- [ ]") == 1
    assert len(existing) == 2 and len(new) == 2
    assert llm.calls == []


@pytest.mark.parametrize("newly_closed", [False, True])
@pytest.mark.asyncio
async def test_replaying_source_does_not_reopen_closed_action(newly_closed: bool) -> None:
    """완료 기록의 원래 회의를 재검수해도 같은 출처의 업무를 다시 열지 않는다."""
    original = _original()
    closed = ClosedActionItem(
        original=original,
        closed_by_speaker="SPEAKER_02",
        closed_at_meeting_id="meeting_20260529_100000",
        closed_citation=Citation("meeting_20260529_100000", "00:02:00", 120),
    )
    rendered = await ActionItemExtractor(MockWikiLLMClient()).render_unified_page(
        existing_open=[original],
        new_open=[_replay(original)],
        existing_closed=[] if newly_closed else [closed],
        newly_closed=[closed] if newly_closed else [],
        last_compiled_at="2026-11-04T12:00:00",
    )
    assert "## Open (0)" in rendered
    assert "## Closed (1)" in rendered
    assert "- [ ]" not in rendered
    assert "[meeting:meeting_20260529_100000@00:02:00]" in rendered


@pytest.mark.parametrize(
    "change",
    [
        {"owner": "SPEAKER_02"},
        {"description": "검토 자료 수정"},
        {"due_date": "2026-05-30"},
        {"project_slug": "other"},
        {"citation": Citation("meeting_20260529_100000", "00:01:20", 80)},
        {"citation": Citation("meeting_20260528_100000", "00:01:21", 81)},
    ],
)
@pytest.mark.asyncio
async def test_similar_actions_with_distinct_facts_or_sources_remain_separate(
    change: dict,
) -> None:
    """유사한 문장이라도 담당·기한·프로젝트·출처가 다르면 합치지 않는다."""
    original = _original()
    rendered = await ActionItemExtractor(MockWikiLLMClient()).render_unified_page(
        existing_open=[original],
        new_open=[replace(_replay(original), **change)],
        existing_closed=[],
        newly_closed=[],
        last_compiled_at="2026-11-04T12:00:00",
    )
    assert "## Open (2)" in rendered
    assert rendered.count("- [ ]") == 2


@pytest.mark.asyncio
async def test_same_legacy_id_does_not_close_a_different_source_action() -> None:
    """시각을 포함하지 않는 과거 ID가 같아도 출처가 다른 업무는 닫지 않는다."""
    original = _original()
    other = replace(original, citation=Citation(original.citation.meeting_id, "00:02:00", 120))
    closed = ClosedActionItem(
        original=original,
        closed_by_speaker="SPEAKER_02",
        closed_at_meeting_id="meeting_20260529_100000",
        closed_citation=Citation("meeting_20260529_100000", "00:02:00", 120),
    )
    rendered = await ActionItemExtractor(MockWikiLLMClient()).render_unified_page(
        existing_open=[original, other],
        new_open=[],
        existing_closed=[],
        newly_closed=[closed],
        last_compiled_at="2026-11-04T12:00:00",
    )
    assert "## Open (1)" in rendered
    assert (
        "SPEAKER_01: 검토 자료 전달 (due: 2026-05-29) [meeting:meeting_20260528_100000@00:02:00]"
        in rendered
    )


@pytest.mark.parametrize("distinct", [False, True])
@pytest.mark.asyncio
async def test_identical_close_event_is_not_appended_again(distinct: bool) -> None:
    """동일 종료 기록은 한 번 남기되 별도 종료 근거는 보존한다."""
    closed = ClosedActionItem(
        original=_original(),
        closed_by_speaker="SPEAKER_02",
        closed_at_meeting_id="meeting_20260529_100000",
        closed_citation=Citation("meeting_20260529_100000", "00:02:00", 120),
    )
    other = (
        replace(closed, closed_citation=Citation("meeting_20260529_100000", "00:03:00", 180))
        if distinct
        else closed
    )
    rendered = await ActionItemExtractor(MockWikiLLMClient()).render_unified_page(
        existing_open=[],
        new_open=[],
        existing_closed=[closed],
        newly_closed=[other],
        last_compiled_at="2026-11-04T12:00:00",
    )
    expected = 2 if distinct else 1
    assert f"## Closed ({expected})" in rendered
    assert rendered.count("  - Closed by:") == expected
