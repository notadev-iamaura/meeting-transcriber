"""empty-state 행동 시나리오 (T-101) — Given-When-Then.

3 위치(meeting-list / search / chat) 별 의도된 메시지·CTA·아이콘이
mockup §5 명세대로 노출되는지 검증. 픽셀 비교 없음 (시각 축은 별도).

Red 의도성:
    fixture HTML 의 마크업 인터페이스(`.empty-state-*` 클래스 + data-empty
    속성 + 텍스트) 가 mockup 과 일치하므로 fixture 단독으로는 PASS 한다.
    그러나 본 시나리오의 진짜 목적은 Frontend-A 의 SPA 구현이 fixture 와
    같은 마크업·텍스트·class 를 사용하도록 강제하는 것. SPA 가 다르게
    구현되면 Wave 1 통합 e2e 단계에서 잡힌다.

    fixture 가 mockup 과 다르면 (예: 텍스트 오타) 본 시나리오가 즉시 FAIL —
    그 경우 fixture 를 수정해야 함 (mockup 이 source of truth).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.ui]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_URL = (
    PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "empty-state-preview.html"
).as_uri()


def test_meeting_list_empty_shows_recording_cta(page: Page) -> None:
    """Given: 회의가 0개인 빈 상태 (mockup §5.1)
    When:  사용자가 회의 목록 사이드바 빈 상태를 본다
    Then:  '아직 회의가 없어요' 제목 + 녹음 안내 설명 + '녹음 시작' CTA 가 보인다
    """
    page.goto(PREVIEW_URL)
    section = page.locator('[data-empty="meeting-list"]')
    expect(section).to_be_visible()
    expect(section.locator(".empty-state-title")).to_have_text("아직 회의가 없어요")
    expect(section.locator(".empty-state-description")).to_contain_text("첫 회의를 녹음")
    expect(section.locator(".empty-state-description")).to_contain_text("자동으로 전사")
    cta = section.locator(".empty-state-cta")
    expect(cta).to_be_visible()
    expect(cta).to_have_text("녹음 시작")


def test_search_empty_shows_keyword_guidance(page: Page) -> None:
    """Given: 검색 결과 0개 (mockup §5.2)
    When:  사용자가 검색 빈 상태 영역을 본다
    Then:  '검색 결과가 없어요' 제목 + 키워드 변경 안내 설명 + CTA 없음
    """
    page.goto(PREVIEW_URL)
    section = page.locator('[data-empty="search"]')
    expect(section.locator(".empty-state-title")).to_have_text("검색 결과가 없어요")
    expect(section.locator(".empty-state-description")).to_contain_text("다른 키워드")
    expect(section.locator(".empty-state-description")).to_contain_text("띄어쓰기")
    # CTA 없음 — 검색은 입력 필드에 다시 입력하는 게 자연스럽다 (mockup §5.2)
    expect(section.locator(".empty-state-cta")).to_have_count(0)


def test_chat_empty_shows_invitation_message(page: Page) -> None:
    """Given: 채팅이 비어있는 초기 상태 (mockup §5.3)
    When:  사용자가 채팅 빈 상태 영역을 본다
    Then:  '대화를 시작해 보세요' 제목 + 회의 질문 안내 설명 + CTA 없음 + 'AI' 단어 미사용
    """
    page.goto(PREVIEW_URL)
    section = page.locator('[data-empty="chat"]')
    expect(section.locator(".empty-state-title")).to_have_text("대화를 시작해 보세요")
    expect(section.locator(".empty-state-description")).to_contain_text("회의 내용")
    # design.md §5.1 Hidden AI 원칙 — 'AI' 단어 미사용 (mockup §5.3 주의사항)
    description_text = section.locator(".empty-state-description").inner_text()
    assert "AI" not in description_text, (
        f"채팅 빈 상태 설명에 'AI' 단어가 들어있음 (Hidden AI 원칙 위반): {description_text!r}"
    )
    # 채팅은 페이지 하단에 입력창이 항상 있으므로 별도 CTA 불필요 (mockup §5.3)
    expect(section.locator(".empty-state-cta")).to_have_count(0)


def test_all_empty_states_have_48px_icon(page: Page) -> None:
    """Given: 3 개의 빈 상태 (mockup §3)
    When:  각 영역의 아이콘을 본다
    Then:  모두 48×48 SVG 아이콘을 보유한다 (design.md §3.7 명세)
    """
    page.goto(PREVIEW_URL)
    icons = page.locator(".empty-state-icon")
    expect(icons).to_have_count(3)
    for i in range(3):
        icon = icons.nth(i)
        # SVG 의 width/height 속성이 48 (CSS 가 어떻게 변경되든 마크업 계약)
        assert icon.get_attribute("width") == "48", (
            f"icon[{i}] width != 48"
        )
        assert icon.get_attribute("height") == "48", (
            f"icon[{i}] height != 48"
        )
