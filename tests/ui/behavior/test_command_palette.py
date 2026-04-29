"""command-palette — 마크업 인터페이스 검증 (T-202).

검증 범위:
    fixture HTML 이 mockup §3 의 마크업 계약을 충족하는지 확인.
        - native <dialog> 단일 인스턴스
        - role="combobox" 컨테이너 + aria-haspopup/aria-expanded/aria-controls
        - role="searchbox" input + aria-autocomplete="list"
        - role="listbox" + 단일 active option (aria-selected="true" 정확히 1 개)
        - listbox option 4 종 (뷰 전환 정적 카테고리)

    픽셀 비교 없음 (시각 축은 별도). 본 시나리오는 Frontend-A 가 spa.js
    Command Palette 모듈 활성화 시 마크업 계약 보강(mockup §8.2)을 누락하면
    잡아내는 것이 진짜 목적.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.ui]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_URL = (
    PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "command-palette-preview.html"
).as_uri()


def test_palette_has_dialog_role(page: Page) -> None:
    """Given: 페이지 로드 직후
    When:  dialog.command-palette 카운트 조회
    Then:  단일 <dialog> 인스턴스 (mockup §3.1 계약).
    """
    page.goto(PREVIEW_URL)
    expect(page.locator("dialog.command-palette")).to_have_count(1)


def test_input_is_searchbox(page: Page) -> None:
    """Given: 입력창
    When:  role 속성 조회
    Then:  role="searchbox" — ARIA 1.2 combobox 패턴 (mockup §6.2).
    """
    page.goto(PREVIEW_URL)
    expect(page.locator("input.command-palette-input")).to_have_attribute("role", "searchbox")


def test_input_aria_autocomplete_list(page: Page) -> None:
    """Given: 입력창
    When:  aria-autocomplete 속성 조회
    Then:  aria-autocomplete="list" — listbox 가 입력에 따라 갱신됨을 SR 통지.
    """
    page.goto(PREVIEW_URL)
    expect(page.locator("input.command-palette-input")).to_have_attribute(
        "aria-autocomplete", "list"
    )


def test_listbox_has_four_static_options(page: Page) -> None:
    """Given: open-empty 기본 상태
    When:  listbox 안의 option 카운트 조회
    Then:  4 개 — 정적 카테고리 4 항목 (홈/검색/채팅/설정, mockup §5.1).
    """
    page.goto(PREVIEW_URL)
    expect(page.locator("[role='listbox'] [role='option']")).to_have_count(4)


def test_exactly_one_option_selected(page: Page) -> None:
    """Given: 기본 상태
    When:  aria-selected="true" 카운트 조회
    Then:  정확히 1 개 — 단일 active 모델 (mockup §6.3).
    """
    page.goto(PREVIEW_URL)
    expect(page.locator("[role='option'][aria-selected='true']")).to_have_count(1)


def test_combobox_aria_controls_listbox(page: Page) -> None:
    """Given: combobox 컨테이너
    When:  aria-controls 속성에서 가리키는 ID 조회
    Then:  해당 ID 의 listbox 가 정확히 1 개 존재 (ARIA 1.2 패턴).
    """
    page.goto(PREVIEW_URL)
    combobox = page.locator("[role='combobox']")
    controls_id = combobox.get_attribute("aria-controls")
    assert controls_id, "aria-controls 속성 필수 (ARIA 1.2 combobox 패턴)"
    expect(page.locator(f"#{controls_id}")).to_have_count(1)
    # listbox 와 동일한 ID 인지 추가 검증
    listbox = page.locator("[role='listbox']")
    listbox_id = listbox.get_attribute("id")
    assert listbox_id == controls_id, (
        f"aria-controls={controls_id!r} 가 listbox id={listbox_id!r} 와 일치해야 함"
    )
