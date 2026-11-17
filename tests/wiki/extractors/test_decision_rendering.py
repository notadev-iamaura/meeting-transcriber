"""결정 페이지를 추가 추론 없이 렌더하고 기존 정렬·상한·내용을 보존한다.

canonical 출력과 페이지별 메타데이터를 보존하고 렌더 추론은 0회임을 검증한다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from core.wiki.decision_record import DecisionRecord
from core.wiki.extractors.decision import DecisionExtractor, ExtractedDecision


class _ForbiddenRenderLLM:
    """렌더링 과정에서 불필요한 추론이 호출되면 기록하고 실패시킨다."""

    model_name = "forbidden-render-llm"

    def __init__(self) -> None:
        """호출 기록을 초기화한다."""
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> str:
        """페이지의 고정 스키마 렌더에는 추론이 없어야 한다."""
        self.calls.append(kwargs)
        raise AssertionError("canonical 페이지 렌더에 모델 추론을 호출하면 안 됩니다.")


class _MockStore:
    """없는 페이지만 보고하는 읽기 전용 저장소 대역."""

    def read_page(self, rel_path: str) -> str:
        """새 페이지 생성 흐름을 재현한다."""
        raise KeyError("page_not_found")


def _build_decisions(count: int, base_confidence: int = 5) -> list[ExtractedDecision]:
    """서로 구별되는 경로·내용·신뢰도의 입력을 만든다."""
    return [
        ExtractedDecision(
            title=f"결정사항 {i:02d}",
            slug=f"decision-{i:02d}",
            decision_text=f"결정 본문 {i} [meeting:abc12345@00:23:45].",
            background=f"배경 {i} [meeting:abc12345@00:18:30].",
            confidence=base_confidence + i,
        )
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_render_pages는_추가추론없이_같은_canonical_본문을_반환한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """폐기되던 모델 출력과 무관하게 canonical 경로·본문이 그대로여야 한다."""

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            """생성 시각만 고정하여 실제 저장 문서 전체를 비교한다."""
            return datetime(2026, 4, 28, 10, 0, 0, tzinfo=tz)

    monkeypatch.setattr("core.wiki.decision_record.datetime", FixedDateTime)
    decisions = _build_decisions(3)
    llm = _ForbiddenRenderLLM()
    pages = await DecisionExtractor(llm).render_pages(
        decisions=decisions,
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 28),
        existing_store=_MockStore(),
    )
    expected = [
        (
            f"decisions/2026-04-28-{decision.slug}.md",
            DecisionRecord.from_extracted(
                decision=decision, meeting_id="abc12345", meeting_date=date(2026, 4, 28)
            ).to_markdown(),
        )
        for decision in reversed(decisions)
    ]
    assert pages == expected
    assert llm.calls == []


@pytest.mark.asyncio
async def test_render_pages_caps_at_8_when_more_decisions_provided() -> None:
    """10건 입력에서도 상위 8건만 만들며 모델을 호출하지 않는다."""
    llm = _ForbiddenRenderLLM()
    pages = await DecisionExtractor(llm).render_pages(
        decisions=_build_decisions(10, base_confidence=0),
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 28),
        existing_store=_MockStore(),
    )
    assert len(pages) == 8
    assert llm.calls == []


@pytest.mark.asyncio
async def test_render_pages_selects_top_8_by_confidence_desc() -> None:
    """신뢰도 높은 입력을 먼저 반환하는 기존 결과 순서를 보존한다."""
    llm = _ForbiddenRenderLLM()
    pages = await DecisionExtractor(llm).render_pages(
        decisions=_build_decisions(10, base_confidence=0),
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 28),
        existing_store=_MockStore(),
    )
    assert [path for path, _ in pages] == [
        f"decisions/2026-04-28-decision-{i:02d}.md" for i in range(9, 1, -1)
    ]
    assert llm.calls == []


@pytest.mark.asyncio
async def test_render_pages_below_8_decisions_processes_all() -> None:
    """상한 미만의 입력은 모두 반환하며 렌더링에 모델이 필요하지 않다."""
    llm = _ForbiddenRenderLLM()
    pages = await DecisionExtractor(llm).render_pages(
        decisions=_build_decisions(5, base_confidence=3),
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 28),
        existing_store=_MockStore(),
    )
    assert len(pages) == 5
    assert llm.calls == []
