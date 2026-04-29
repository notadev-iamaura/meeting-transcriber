"""aria-sync — listbox/option, nav/current, expandable 동기 (T-301).

행동 시나리오 (mockup §3.1):
    1. listbox 안에 정확히 1 option 이 aria-selected="true"
    2. nav 안에 정확히 1 button 이 aria-current="page"
    3. aria-expanded="true" 인 버튼의 aria-controls 패널이 보임
    4. role="option" 의 부모가 role="listbox" (axe aria-required-parent 룰)
"""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.ui]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_URL = (
    PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "aria-sync-preview.html"
).as_uri()


def test_listbox_has_exactly_one_selected_option(page: Page) -> None:
    """listbox 안에 정확히 1 option 이 aria-selected='true'."""
    page.goto(PREVIEW_URL)
    selected = page.locator("[role='option'][aria-selected='true']")
    expect(selected).to_have_count(1)


def test_nav_has_exactly_one_current_page(page: Page) -> None:
    """nav 안에 정확히 1 button 이 aria-current='page'."""
    page.goto(PREVIEW_URL)
    current = page.locator("nav button[aria-current='page']")
    expect(current).to_have_count(1)


def test_expanded_button_controls_visible_panel(page: Page) -> None:
    """aria-expanded='true' 인 버튼의 aria-controls 패널이 보임."""
    page.goto(PREVIEW_URL)
    btn = page.locator("button[aria-expanded='true']").first
    panel_id = btn.get_attribute("aria-controls")
    assert panel_id, "aria-controls 누락"
    panel = page.locator(f"#{panel_id}")
    expect(panel).to_be_visible()


def test_listbox_role_required_parent(page: Page) -> None:
    """role='option' 의 부모가 role='listbox' (axe aria-required-parent)."""
    page.goto(PREVIEW_URL)
    options = page.locator("[role='option']")
    count = options.count()
    assert count > 0, "fixture 에 role=option 이 하나도 없음"
    for i in range(count):
        parent_role = options.nth(i).evaluate(
            "el => el.parentElement.getAttribute('role')"
        )
        assert parent_role == "listbox", (
            f"option {i} 의 부모가 listbox 가 아님 (got: {parent_role!r})"
        )
