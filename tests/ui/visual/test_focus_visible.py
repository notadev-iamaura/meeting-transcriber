"""focus-visible — 첫 인터랙티브 요소에 키보드 focus 적용 후 시각 회귀 (T-201).

검증 대상:
    `tests/ui/_fixtures/focus-visible-preview.html` 가 mockup §2.1 글로벌
    selector + §1.3 토큰을 상속받아 #first-button 에 focus 적용 시점의
    렌더링이 `tests/ui/visual/baselines/focus-visible-{light,dark,mobile}.png`
    와 픽셀 diff < 0.1% (spec §5.3) 로 일치하는가.

DPR (device_scale_factor) 일치:
    Designer-A 가 베이스라인을 Retina(DPR=2) 로 캡처했다.
        light/dark : viewport 520×560 → PNG 1040×1120
        mobile     : viewport 375×720 → PNG 750×1440
    pytest-playwright 의 기본 `page` fixture 는 DPR=1 이라 베이스라인과
    픽셀 크기 자체가 다르다. 본 모듈은 `browser` fixture 로부터 변종별
    `device_scale_factor=2` context 를 직접 만들어 baseline 과 동일한
    물리 픽셀 크기를 확보한다.

캡처 시점 고정:
    page.locator("#first-button").focus() 후 즉시 screenshot 으로 ring 가시
    상태를 baseline 과 동일한 단일 frame 에 고정한다.

Red 의도성:
    fixture 와 baseline 이 동일한 selector·토큰 정의를 공유하므로 본
    시나리오는 PASS 가 정상이다. Frontend-A 가 style.css §15 섹션을
    mockup §2.1 룰로 마이그레이션한 뒤, fixture 의 인라인 <style> 를
    제거해도 같은 렌더링이 나오면 시각 계약이 유지된다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from playwright.sync_api import Browser, Page

from harness import snapshot

pytestmark = [pytest.mark.ui]

# 프로젝트 루트 — tests/ui/visual/test_focus_visible.py 기준 3 단계 위.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_URL = (PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "focus-visible-preview.html").as_uri()
ACTUAL_DIR = PROJECT_ROOT / "tests" / "ui" / "visual" / "_actual"

# Designer-A baseline 이 DPR=2 (Retina) 로 캡처되어 있어 동일 DPR 강제.
DEVICE_SCALE_FACTOR = 2


def _make_page(
    browser: Browser,
    *,
    width: int,
    height: int,
    color_scheme: Literal["light", "dark", "no-preference"],
) -> Page:
    """variant 별 viewport·color-scheme·DPR 일치 context 에서 새 Page 생성."""
    context = browser.new_context(
        viewport={"width": width, "height": height},
        device_scale_factor=DEVICE_SCALE_FACTOR,
        color_scheme=color_scheme,
    )
    return context.new_page()


def _focus_and_compare(page: Page, variant: str) -> None:
    """현재 페이지의 #first-button 에 focus 적용 후 baseline 과 픽셀 비교.

    Raises:
        AssertionError: pixel diff > 0.1% 일 때 (spec §5.3)
    """
    ACTUAL_DIR.mkdir(parents=True, exist_ok=True)
    actual = ACTUAL_DIR / f"focus-visible-{variant}.png"
    page.goto(PREVIEW_URL)
    page.wait_for_load_state("networkidle")
    page.locator("#first-button").focus()
    page.screenshot(path=str(actual))
    baseline = snapshot.baseline_path("focus-visible", variant)
    snapshot.assert_visual_match(actual, baseline, max_diff_pixel_ratio=0.001)


def test_focus_visible_light(browser: Browser) -> None:
    """라이트 모드 520×560 @2x — baseline-light(1040×1120) 와 픽셀 일치."""
    page = _make_page(browser, width=520, height=560, color_scheme="light")
    try:
        _focus_and_compare(page, "light")
    finally:
        page.context.close()


def test_focus_visible_dark(browser: Browser) -> None:
    """다크 모드 520×560 @2x — baseline-dark(1040×1120) 와 픽셀 일치."""
    page = _make_page(browser, width=520, height=560, color_scheme="dark")
    try:
        _focus_and_compare(page, "dark")
    finally:
        page.context.close()


def test_focus_visible_mobile(browser: Browser) -> None:
    """모바일 라이트 375×720 @2x — baseline-mobile(750×1440) 과 픽셀 일치."""
    page = _make_page(browser, width=375, height=720, color_scheme="light")
    try:
        _focus_and_compare(page, "mobile")
    finally:
        page.context.close()
