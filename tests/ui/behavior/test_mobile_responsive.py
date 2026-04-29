"""mobile-responsive — 햄버거 토글, ESC, 백드롭, focus return (T-302).

행동 시나리오 (mockup §5):
    1. 페이지 로드 직후 drawer 는 닫힘 (aria-expanded="false")
    2. 햄버거 클릭 → drawer 열림 (aria-expanded="true" + aria-modal="true")
    3. 열림 상태에서 ESC → drawer 닫힘
    4. 열림 상태에서 백드롭 클릭 → drawer 닫힘
    5. drawer 닫힘 시 focus 가 햄버거 버튼으로 복귀 (mockup §6.3)
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

pytestmark = [pytest.mark.ui]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_URL = (
    PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "mobile-responsive-preview.html"
).as_uri()


@contextmanager
def _mobile_page(browser: Browser) -> Iterator[Page]:
    """모바일 viewport(375×667) context 에서 새 Page 생성.

    햄버거 + drawer 는 ≤768px 미디어 쿼리에서만 활성화되므로 모바일
    viewport 가 필수.
    """
    context = browser.new_context(
        viewport={"width": 375, "height": 667},
        color_scheme="light",
    )
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


def test_drawer_initially_closed(browser: Browser) -> None:
    """페이지 로드 직후 drawer 는 닫힘 — 햄버거 aria-expanded='false', 사이드바 .is-open 없음."""
    with _mobile_page(browser) as page:
        page.goto(PREVIEW_URL)
        page.wait_for_load_state("networkidle")
        expect(page.locator("#mobile-menu-toggle")).to_have_attribute("aria-expanded", "false")
        # 사이드바는 ARIA 상태 속성을 갖지 않고 .is-open 클래스로 토글 (mockup §3.2)
        expect(page.locator("#sidebar")).not_to_have_class("is-open")


def test_hamburger_click_opens_drawer(browser: Browser) -> None:
    """햄버거 클릭 → drawer 열림 (햄버거 aria-expanded='true', 사이드바 .is-open)."""
    with _mobile_page(browser) as page:
        page.goto(PREVIEW_URL)
        page.wait_for_load_state("networkidle")
        page.locator("#mobile-menu-toggle").click()
        expect(page.locator("#mobile-menu-toggle")).to_have_attribute("aria-expanded", "true")
        # 사이드바는 클래스 토글로 시각 상태 표현 (mockup §3.2 / §6.1)
        expect(page.locator("#sidebar")).to_have_class("is-open")


def test_escape_closes_drawer(browser: Browser) -> None:
    """열림 상태에서 ESC → drawer 닫힘 (WCAG 2.1.2 No Keyboard Trap)."""
    with _mobile_page(browser) as page:
        page.goto(PREVIEW_URL)
        page.wait_for_load_state("networkidle")
        page.locator("#mobile-menu-toggle").click()
        page.wait_for_timeout(50)
        page.keyboard.press("Escape")
        expect(page.locator("#mobile-menu-toggle")).to_have_attribute("aria-expanded", "false")
        expect(page.locator("#sidebar")).not_to_have_class("is-open")


def test_backdrop_click_closes_drawer(browser: Browser) -> None:
    """열림 상태에서 백드롭 클릭 → drawer 닫힘 (사이드바 밖 영역)."""
    with _mobile_page(browser) as page:
        page.goto(PREVIEW_URL)
        page.wait_for_load_state("networkidle")
        page.locator("#mobile-menu-toggle").click()
        page.wait_for_timeout(400)  # transition 완료 대기 (pointer-events 활성)
        # 백드롭의 사이드바 밖 영역(우측 하단) 클릭 — 사이드바 폭 280px 너머
        page.locator("#drawer-backdrop").click(position={"x": 350, "y": 600})
        expect(page.locator("#mobile-menu-toggle")).to_have_attribute("aria-expanded", "false")
        expect(page.locator("#sidebar")).not_to_have_class("is-open")


def test_close_returns_focus_to_toggle(browser: Browser) -> None:
    """drawer 닫힘 시 focus 가 햄버거 버튼으로 복귀 (mockup §6.3)."""
    with _mobile_page(browser) as page:
        page.goto(PREVIEW_URL)
        page.wait_for_load_state("networkidle")
        page.locator("#mobile-menu-toggle").click()
        page.wait_for_timeout(50)
        page.keyboard.press("Escape")
        # 닫힘 직후 document.activeElement 가 햄버거 버튼이어야 함
        focused_id = page.evaluate("document.activeElement.id")
        assert focused_id == "mobile-menu-toggle", (
            f"focus 가 햄버거 버튼이 아님 (got: {focused_id!r})"
        )
