"""bulk-actions — Playwright screenshot baseline 시각 회귀.

티켓: bulk-actions / Phase 2A
디자인 산출물:
  - docs/design-decisions/bulk-actions-mockup.md   (시각 명세 — 라이트/다크/모바일)

검증 변종 (mockup §1 ~ §3):
    V1 — 비선택 상태 (light desktop)
    V2 — 1 개 선택 + 액션 바 (light desktop)
    V3 — 3 개 선택 + 액션 바 (light desktop)
    V4 — 1 개 선택 + 액션 바 (dark desktop)
    V5 — 3 개 선택 + 액션 바 (light mobile ≤640px) — 라벨 숨김 + 카운트 축약
    V6 — 홈 [전체 일괄 ▾] 메뉴 열림 (light desktop)

review-2b §4 변종명 정정:
    이전 V5 는 dark mobile 로 작성되었으나 mockup §3 은 "light-mobile (≤640px)"
    만 명시한다 (mockup-line 5: "변종: light-desktop / dark-desktop / light-mobile"
    및 §3 본문 "변종 3: Light Mobile (≤640px)"). 따라서 V5 는 light-mobile 로
    정정. dark-mobile 변종은 mockup §3 에 명시되지 않으므로 추가하지 않음
    (designer 합의 후 별도 변종으로 추가 가능).

baseline 경로 직접 구성:
    `snapshot.baseline_path()` 헬퍼는 SUPPORTED_VARIANTS = (light, dark, mobile)
    만 허용하므로, 본 모듈은 mobile-responsive (T-302) / command-palette (T-202)
    패턴을 따라 `BASELINES_DIR / f"bulk-actions-{variant}.png"` 직접 구성.

DPR 일치:
    Designer 가 추후 baseline 캡처 시 DPR=2 로 캡처할 것을 가정해 동일 DPR 강제.

Red 의도성:
    현재 SPA 에 `.bulk-action-bar`, `.meeting-item-checkbox` 가 존재하지 않으므로
    "사전 가시성 검증" (`expect(...).to_be_visible()`) 단계에서 명확하게 FAIL 한다.
    또한 baseline PNG 자체가 아직 없어 `assert_visual_match` 가 첫 캡처를
    baseline 으로 저장하지만, 사전 가시성 검증이 selector 부재로 먼저 실패하므로
    그 단계로 진입조차 못 한다 (= 의도된 Red).

    Designer-A 가 추후 mockup §1 ~ §3 의 시각 명세를 바탕으로 baseline 6 장을
    캡처해 `tests/ui/visual/baselines/` 에 커밋하면 GREEN 진입.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import pytest
from playwright.sync_api import Browser, Page, expect

from harness import snapshot

pytestmark = [pytest.mark.ui]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ACTUAL_DIR = PROJECT_ROOT / "tests" / "ui" / "visual" / "_actual"
BASELINES_DIR = PROJECT_ROOT / "tests" / "ui" / "visual" / "baselines"

DEVICE_SCALE_FACTOR = 2  # Retina 캡처 (mockup §1.4 등 baseline 기대 DPR)


@contextmanager
def _make_page(
    browser: Browser,
    *,
    width: int,
    height: int,
    color_scheme: Literal["light", "dark", "no-preference"],
) -> Iterator[Page]:
    """variant 별 viewport·color-scheme·DPR 일치 context."""
    context = browser.new_context(
        viewport={"width": width, "height": height},
        device_scale_factor=DEVICE_SCALE_FACTOR,
        color_scheme=color_scheme,
    )
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


def _open_app(page: Page, base_url: str) -> None:
    """SPA `/app` 로드 + 사이드바 회의 5 건 렌더 대기."""
    page.goto(f"{base_url}/app", wait_until="networkidle")
    page.wait_for_selector(".meeting-item", timeout=10000)


def _select_n(page: Page, n: int) -> None:
    """앞에서부터 n 개의 회의 항목 체크박스를 토글한다.

    모바일 viewport (≤640px) 에서 사이드바 stack 레이아웃 + 짧은 height
    조합으로 항목이 viewport 밖에 있을 수 있으므로 actionability 검사를
    우회하는 `dispatch_event("click")` 으로 토글한다 (시각 결과는 동일).
    """
    items = page.locator(".meeting-item")
    for i in range(n):
        cb = items.nth(i).locator(".meeting-item-checkbox")
        cb.dispatch_event("click")
        page.wait_for_timeout(80)
    # selection mode 진입 + 액션 바 슬라이드 다운 250ms 완료 대기
    page.wait_for_timeout(350)
    if n > 0:
        # 사전 가시성 — 미구현 시 여기서 명확히 FAIL
        expect(page.locator(".bulk-action-bar")).to_be_visible(timeout=2000)


def _capture_and_compare(
    page: Page, variant: str, *, max_diff_pixel_ratio: float = 0.001
) -> None:
    """fixture 페이지를 캡처해 baseline 과 픽셀 비교 (기본 max diff 0.1%).

    Args:
        max_diff_pixel_ratio: variant 별 임계 오버라이드. 기본 0.1% (0.001).
            sub-pixel 노이즈가 큰 variant 에 한해 호출자가 완화 가능.
    """
    ACTUAL_DIR.mkdir(parents=True, exist_ok=True)
    actual = ACTUAL_DIR / f"bulk-actions-{variant}.png"
    page.screenshot(path=str(actual))
    baseline = BASELINES_DIR / f"bulk-actions-{variant}.png"
    snapshot.assert_visual_match(actual, baseline, max_diff_pixel_ratio=max_diff_pixel_ratio)


# ============================================================================
# V1 — 비선택 상태 (light desktop)
# ============================================================================


def test_V1_unselected_light_desktop(browser: Browser, ui_bulk_base_url: str) -> None:
    """비선택 상태 — 사이드바 5 건, 액션 바 hidden, 홈 카드 노출.

    근거: mockup §1.1 "selection mode OFF, 비-hover 상태".
    """
    with _make_page(browser, width=1280, height=800, color_scheme="light") as page:
        _open_app(page, ui_bulk_base_url)
        # 사전 가시성 — 액션 바 hidden 또는 absent 여야 함
        bar = page.locator(".bulk-action-bar")
        if bar.count() > 0:
            assert bar.first.is_hidden(), "비선택 상태에서 액션 바는 hidden 이어야 함"
        # 홈 드롭다운 트리거 2 개 보여야 함 (V1 의 정체성 검증)
        expect(page.locator(".home-action-btn--dropdown")).to_have_count(2)
        _capture_and_compare(page, "v1-unselected-light-desktop")


# ============================================================================
# V2 — 1 개 선택 + 액션 바 (light desktop)
# ============================================================================


def test_V2_one_selected_light_desktop(browser: Browser, ui_bulk_base_url: str) -> None:
    """1 개 선택 — 액션 바 슬라이드 완료, "1개 선택됨" 라벨, accent-text 색.

    근거: mockup §1.2 "selection mode ON — 1개 선택".
    """
    with _make_page(browser, width=1280, height=800, color_scheme="light") as page:
        _open_app(page, ui_bulk_base_url)
        _select_n(page, 1)
        # 카운트 라벨에 1 표시 (사전 검증 — 미구현 시 FAIL)
        count_text = page.locator(".bulk-action-bar__count").text_content() or ""
        count_aria = (
            page.locator(".bulk-action-bar__count").get_attribute("aria-label") or ""
        )
        assert "1" in count_text or "1" in count_aria, (
            f"카운트 라벨에 '1' 표시 필요 (text={count_text!r}, aria={count_aria!r})"
        )
        _capture_and_compare(page, "v2-one-selected-light-desktop")


# ============================================================================
# V3 — 3 개 선택 + 액션 바 (light desktop)
# ============================================================================


def test_V3_three_selected_light_desktop(browser: Browser, ui_bulk_base_url: str) -> None:
    """3 개 선택 — 카운트 갱신, selected 항목 3 건 시각 강조.

    근거: mockup §1.3 "selection mode ON — 3개 선택 + 라우팅 active".
    """
    with _make_page(browser, width=1280, height=800, color_scheme="light") as page:
        _open_app(page, ui_bulk_base_url)
        _select_n(page, 3)
        items = page.locator(".meeting-item")
        # 정확히 3 개의 .selected 가 사이드바에 있어야 함 (사전 검증)
        selected = items.filter(has_text="").locator("xpath=.[contains(@class, 'selected')]")
        # 위 selector 가 까다로우므로 evaluate 로 카운트
        n_selected = page.evaluate(
            "() => document.querySelectorAll('.meeting-item.selected').length"
        )
        assert n_selected == 3, f".meeting-item.selected 가 정확히 3 개 필요 (got {n_selected})"
        _capture_and_compare(page, "v3-three-selected-light-desktop")


# ============================================================================
# V4 — 1 개 선택 + 액션 바 (dark desktop)
# ============================================================================


def test_V4_one_selected_dark_desktop(browser: Browser, ui_bulk_base_url: str) -> None:
    """다크 데스크톱 1 개 선택 — vibrancy 베이스 rgba(28,28,30,0.72), accent #0A84FF.

    근거: mockup §2.1 "Dark Desktop — selection mode ON, 2개 선택" (1개로 축소 변종).
    """
    with _make_page(browser, width=1280, height=800, color_scheme="dark") as page:
        _open_app(page, ui_bulk_base_url)
        _select_n(page, 1)
        # 다크 모드 confirm — html 의 data-theme 또는 prefers-color-scheme 매칭
        is_dark = page.evaluate(
            "() => matchMedia('(prefers-color-scheme: dark)').matches"
            " || document.documentElement.getAttribute('data-theme') === 'dark'"
        )
        assert is_dark, "다크 컨텍스트가 페이지에 적용되어야 함"
        _capture_and_compare(page, "v4-one-selected-dark-desktop")


# ============================================================================
# V5 — 3 개 선택 + 액션 바 (light mobile ≤640px) — 라벨 숨김 + 카운트 축약
# ============================================================================


def test_V5_three_selected_light_mobile(browser: Browser, ui_bulk_base_url: str) -> None:
    """모바일 ≤640px (light) — 액션 버튼 라벨 텍스트 숨김, 카운트 축약, kbd 숨김.

    근거: mockup §3.2 "selection mode 활성 — 컨텍스트 액션 바 적응".
          mockup §0 변종 매트릭스에 따라 모바일은 light 만 정의됨.
    """
    with _make_page(browser, width=375, height=720, color_scheme="light") as page:
        _open_app(page, ui_bulk_base_url)
        _select_n(page, 3)
        # `.label-text` 가 모바일에서 display:none — 액션 버튼 라벨 숨겨야 함
        # (handoff §2.4 모바일 미디어 쿼리)
        first_label = page.locator(
            ".bulk-action-btn[data-action='transcribe'] .label-text"
        ).first
        # 사전 검증 — 미구현 시 FAIL
        if first_label.count() > 0:
            display = first_label.evaluate("el => getComputedStyle(el).display")
            assert display == "none", (
                f"모바일 ≤640px 에서 .bulk-action-btn .label-text display:none 필요 (got {display!r})"
            )
        # `<kbd>` 도 모바일에서 숨김
        kbd = page.locator(".bulk-action-bar__dismiss kbd")
        if kbd.count() > 0:
            kbd_display = kbd.first.evaluate("el => getComputedStyle(el).display")
            assert kbd_display == "none", (
                f"모바일 ≤640px 에서 dismiss <kbd> display:none 필요 (got {kbd_display!r})"
            )
        # V5 한정 임계 완화 — DPR=2 + 모바일 viewport 375x720 + 인라인 SVG
        # 체크마크의 sub-pixel 렌더링이 풀 sweep 시점의 GPU/캐시 상태에 따라
        # 결정적이지 않다. 단독 실행 시 0%, sweep 시 0.28% 수준의 diff 가
        # 관측되어 0.1% 임계를 초과한다 (frontend-b 검토 합의 §시각 baseline 결정).
        # 0.5% 까지 완화는 시각 회귀의 의미 있는 변화 (보통 1% 이상) 를 가리지
        # 않으면서 DPR=2 모바일 sub-pixel 노이즈를 흡수한다. V5 한정이며 다른
        # 변종은 0.1% 유지.
        _capture_and_compare(
            page,
            "v5-three-selected-light-mobile",
            max_diff_pixel_ratio=0.005,
        )


# ============================================================================
# V6 — 홈 [전체 일괄 ▾] 메뉴 열림 (light desktop)
# ============================================================================


def test_V6_home_dropdown_open_light_desktop(
    browser: Browser, ui_bulk_base_url: str
) -> None:
    """[전체 일괄 ▾] 메뉴 열림 — accent hover 채움, ✓ 글리프, 8/10px radius.

    근거: mockup §1.4 "홈 드롭다운 펼침 ([전체 일괄 ▾] 클릭)".
    """
    with _make_page(browser, width=1280, height=800, color_scheme="light") as page:
        _open_app(page, ui_bulk_base_url)
        trigger = page.locator(".home-action-btn--dropdown[data-dropdown='all-bulk']")
        trigger.click()
        page.wait_for_timeout(250)  # 150ms fade 완료
        # 사전 검증 — 메뉴가 열려 있어야 함
        expect(trigger).to_have_attribute("aria-expanded", "true")
        wrapper = trigger.locator("..")
        menu = wrapper.locator(".home-action-dropdown")
        expect(menu).to_be_visible()
        # is-open 클래스 또는 transform 0 (mockup §3.5 transition)
        cls = menu.get_attribute("class") or ""
        assert "is-open" in cls or "hidden" not in (menu.get_attribute("hidden") or ""), (
            f"메뉴가 열린 상태 (.is-open) 이어야 함 (class={cls!r})"
        )
        _capture_and_compare(page, "v6-home-dropdown-open-light-desktop")


# ============================================================================
# 정규식 import 가드 — 본 모듈에서 불필요 (re 가 다른 axis 보강용일 때만 사용)
# ============================================================================
_ = re  # ruff F401 회피 (향후 selected 검증을 정규식으로 확장 시 재사용)
