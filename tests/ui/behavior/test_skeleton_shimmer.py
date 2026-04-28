"""skeleton-shimmer 행동 시나리오 (T-103) — 카드형/라인형 마크업 인터페이스 검증.

검증 범위:
    fixture HTML 이 mockup §3.1 (카드형) / §3.2 (라인형) 의 마크업 인터페이스를
    충족하는지 확인. 픽셀 비교 없음 (시각 축은 별도).

Red 의도성:
    fixture 자체는 mockup 명세를 충족하므로 본 시나리오는 baseline 시점부터
    PASS 한다. 본 시나리오의 진짜 목적은 Frontend-A 가 SPA(spa.js) 의 4 위치
    로딩 마크업(목록/검색/transcript/summary)을 본 fixture 와 같은 클래스
    구조 (.skeleton-card / .skeleton-line + short/medium width 변종) 로
    교체하도록 강제하는 것. SPA 통합 시점에 다르게 구현되면 통합 e2e 단계에서
    잡힌다 (mockup §3.3 표).

    fixture 가 mockup 과 다르면 (예: 카드 카운트 변경) 본 시나리오가 즉시
    FAIL — 그 경우 fixture 를 수정해야 함 (mockup 이 source of truth).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.ui]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_URL = (
    PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "skeleton-shimmer-preview.html"
).as_uri()


def test_card_list_section_has_three_skeleton_cards(page: Page) -> None:
    """Given: card-list 섹션 (mockup §3.1)
    When:  skeleton-card 셀렉터 카운트
    Then:  3 개 보유 (검색 결과 변종 카운트와 동일).
    """
    page.goto(PREVIEW_URL)
    cards = page.locator('[data-skeleton="card-list"] .skeleton-card')
    expect(cards).to_have_count(3)


def test_each_card_has_three_lines(page: Page) -> None:
    """Given: card-list 섹션의 각 .skeleton-card (mockup §3.1)
    When:  카드 내부 .skeleton-line 카운트
    Then:  카드마다 3 개 라인 (short / medium / full) — 회의 제목·메타·본문 1 줄 모방.
    """
    page.goto(PREVIEW_URL)
    cards = page.locator('[data-skeleton="card-list"] .skeleton-card')
    count = cards.count()
    assert count == 3, f"카드 카운트가 3 이어야 함, 실제={count}"
    for i in range(count):
        lines = cards.nth(i).locator(".skeleton-line")
        expect(lines).to_have_count(3)


def test_lines_section_has_five_skeleton_lines(page: Page) -> None:
    """Given: 라인형 섹션 (mockup §3.2)
    When:  .skeleton-line 카운트
    Then:  5 개 보유 (transcript 5~8 / summary 4~6 의 중간값으로 5 채택).
    """
    page.goto(PREVIEW_URL)
    lines = page.locator('[data-skeleton="lines"] .skeleton-line')
    expect(lines).to_have_count(5)


def test_short_and_medium_width_modifiers_present(page: Page) -> None:
    """Given: 라인형 섹션 (mockup §3.2)
    When:  width 변종 클래스 카운트 (short = 40%, medium = 70%)
    Then:  최소 1 개의 .short, 1 개의 .medium 보유 — 단락 끝맺음 표현.
    """
    page.goto(PREVIEW_URL)
    lines_section = page.locator('[data-skeleton="lines"]')
    short = lines_section.locator(".skeleton-line.short")
    medium = lines_section.locator(".skeleton-line.medium")
    expect(short).to_have_count(1)
    expect(medium).to_have_count(1)
