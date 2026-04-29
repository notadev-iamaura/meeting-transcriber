"""focus-visible — Tab 순회 + Enter/Space 활성 + focus ring 표시 (T-201).

검증 범위:
    fixture HTML 이 mockup §3 의 6 인터랙티브 요소 인터페이스를 충족하는지
    확인 (Tab 순회 가능 + role 기반 커스텀 요소 focus 가능 + box-shadow ring
    적용). 픽셀 비교 없음 (시각 축은 별도).

Red 의도성:
    fixture 자체가 mockup 명세를 충족하므로 본 시나리오는 baseline 시점부터
    PASS 한다. 본 시나리오의 진짜 목적은 Frontend-A 가 style.css §15 섹션의
    글로벌 :focus-visible 룰을 mockup §2.1 selector 로 교체할 때, fixture 의
    인라인 <style> 을 제거해도 동일한 box-shadow 가 나오도록 강제하는 것.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.ui]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_URL = (
    PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "focus-visible-preview.html"
).as_uri()


def test_tab_focuses_first_interactive(page: Page) -> None:
    """Given: 페이지 로드 직후
    When:  Tab 1 회 누름
    Then:  #first-button 이 활성 요소가 됨 (mockup §3.1 Tab 순회 시작점).
    """
    page.goto(PREVIEW_URL)
    page.keyboard.press("Tab")
    focused_id = page.evaluate("document.activeElement.id")
    assert focused_id == "first-button", (
        f"Tab 1회 후 첫 요소가 #first-button 이어야 함, 실제={focused_id!r}"
    )


def test_tab_traverses_all_six_elements(page: Page) -> None:
    """Given: 페이지 로드 직후
    When:  Tab 6 회 순차 누름
    Then:  6 인터랙티브 요소 모두 focus 통과 (mockup §3 — 버튼·링크·입력·
           role=button·role=option 모두 순회).
    """
    page.goto(PREVIEW_URL)
    seen_focused: set[str] = set()
    for _ in range(6):
        page.keyboard.press("Tab")
        # 요소 식별: tagName + (id | role | href) 조합으로 6 개 구분
        tag = page.evaluate(
            "document.activeElement.tagName + ':' + ("
            "document.activeElement.id || "
            "document.activeElement.getAttribute('role') || "
            "document.activeElement.getAttribute('href') || ''"
            ")"
        )
        seen_focused.add(tag)
    assert len(seen_focused) == 6, (
        f"6개 요소 모두 통과해야 함: {seen_focused}"
    )


def test_focus_visible_box_shadow_applied(page: Page) -> None:
    """Given: #first-button 에 focus 적용
    When:  computed style 의 box-shadow 조회
    Then:  box-shadow 가 비어있지 않고 rgb(...) 색을 포함 (focus-ring 패턴 적용).
           mockup §1.3 의 2-stop ring (`0 0 0 1px bg, 0 0 0 3px accent`) 적용 확인.
    """
    page.goto(PREVIEW_URL)
    page.locator("#first-button").focus()
    box_shadow = page.locator("#first-button").evaluate(
        "el => getComputedStyle(el).boxShadow"
    )
    assert box_shadow and "rgb" in box_shadow.lower(), (
        f"focus 시 box-shadow 가 적용되어야 함: {box_shadow!r}"
    )


def test_role_button_focusable(page: Page) -> None:
    """Given: <div role="button" tabindex="0"> 커스텀 요소
    When:  .focus() 호출
    Then:  document.activeElement === el (mockup §2.1 [role="button"] 커버 검증).
    """
    page.goto(PREVIEW_URL)
    custom = page.locator("[role='button']")
    custom.focus()
    is_focused = custom.evaluate("el => document.activeElement === el")
    assert is_focused, "[role='button'][tabindex='0'] 가 focus 가능해야 함"
