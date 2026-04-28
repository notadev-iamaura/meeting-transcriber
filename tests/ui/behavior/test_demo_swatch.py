"""Wave 1 시작 전 하네스 동작 검증용 demo — 행동 축."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.ui]


def test_swatch_page_renders_three_tokens(page: Page, demo_swatch_url: str) -> None:
    """Given: demo swatch 페이지를 연다
    When:  렌더 완료를 기다린다
    Then:  3 개의 토큰 견본이 표시된다.
    """
    page.goto(demo_swatch_url)
    items = page.locator("ul[role='list'] li")
    expect(items).to_have_count(3)


def test_swatch_page_has_main_landmark(page: Page, demo_swatch_url: str) -> None:
    """Given: demo swatch 페이지
    When:  ARIA landmark 를 찾는다
    Then:  단 하나의 main 이 존재한다.
    """
    page.goto(demo_swatch_url)
    expect(page.locator("[role='main']")).to_have_count(1)
