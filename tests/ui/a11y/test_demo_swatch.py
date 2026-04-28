"""Wave 1 시작 전 하네스 동작 검증용 demo — 접근성 축.

axe-playwright-python 의 Axe 클래스를 통해 wcag2a + wcag2aa + wcag21aa
룰셋 위반이 0건임을 검증.
"""
from __future__ import annotations

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page

pytestmark = [pytest.mark.ui]


def test_swatch_page_has_no_a11y_violations(page: Page, demo_swatch_url: str) -> None:
    page.goto(demo_swatch_url)
    axe = Axe()
    results = axe.run(
        page,
        options={"runOnly": {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag21aa"]}},
    )
    violations = results.response.get("violations", [])
    assert violations == [], (
        "a11y violations found:\n"
        + "\n".join(f"  - {v['id']}: {v['help']}" for v in violations)
    )
