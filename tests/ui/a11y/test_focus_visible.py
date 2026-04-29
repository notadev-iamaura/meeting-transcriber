"""focus-visible — axe-core wcag2a + wcag2aa + wcag21aa, focus indicator 위반 0 (T-201).

검증 룰셋: spec §5.3 + harness.a11y.DEFAULT_RULESET.
wcag21aaa 는 spec 범위 밖이므로 활성화하지 않음.

Red 의도성:
    fixture 의 6 인터랙티브 요소는 모두 native focusable (button/a/input) 이거나
    tabindex=0 + role 명시 (커스텀 요소) 라 axe 의 focus-order-semantics 룰을
    통과해야 한다. 본 시나리오의 진짜 목적은 Frontend-A 가 SPA 통합 시
    [role="button"]/[role="option"] 마크업에서 tabindex 를 누락하면 통합 e2e
    가 잡아내도록 하는 마크업 계약 확립.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page

from harness.a11y import DEFAULT_RULESET

pytestmark = [pytest.mark.ui]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_URL = (PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "focus-visible-preview.html").as_uri()


def test_focus_visible_no_axe_violations(page: Page) -> None:
    """axe-core wcag2a + wcag2aa + wcag21aa 룰 위반 0 검증 (spec §5.3).

    Given: focus-visible fixture 페이지
    When:  axe-core 를 wcag2a + wcag2aa + wcag21aa 룰셋으로 실행
    Then:  위반 0 건 — focus indicator 제공 + 키보드 진입 가능 + 색대비 통과.
    """
    page.goto(PREVIEW_URL)
    axe = Axe()
    results = axe.run(
        page,
        options={
            "runOnly": {"type": "tag", "values": list(DEFAULT_RULESET)},
        },
    )
    violations = results.response.get("violations", [])
    assert violations == [], "a11y violations found:\n" + "\n".join(
        f"  - {v['id']} ({v['impact']}): {v['help']}\n    nodes: {len(v.get('nodes', []))}"
        for v in violations
    )


def test_all_interactive_elements_can_receive_focus(page: Page) -> None:
    """Given: 6 인터랙티브 요소 (mockup §3)
    When:  각 요소에 .focus() 호출
    Then:  document.activeElement 가 해당 요소와 일치 — 키보드 진입 보장.
           native focusable 4 종(button×2/a/input) + tabindex=0 커스텀 2 종
           ([role='button']/[role='option']) 모두 검증.
    """
    page.goto(PREVIEW_URL)
    selectors = [
        "#first-button",
        "button:not(#first-button)",
        "a",
        "input[type='text']",
        "[role='button']",
        "[role='option']",
    ]
    for sel in selectors:
        el = page.locator(sel).first
        el.focus()
        is_focused = el.evaluate("el => document.activeElement === el")
        assert is_focused, f"{sel} 에 focus 안 됨 (키보드 진입 불가)"
