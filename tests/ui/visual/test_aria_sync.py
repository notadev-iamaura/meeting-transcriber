"""aria-sync — 활성 항목·current·expanded 시각 회귀 (T-301).

검증 대상:
    `tests/ui/_fixtures/aria-sync-preview.html` 가 mockup §3 fixture 마크업
    그대로 렌더링한 결과가 `tests/ui/visual/baselines/aria-sync-{light,
    dark,mobile}.png` 와 픽셀 diff < 0.1% (spec §5.3) 로 일치하는가.

DPR (device_scale_factor) 일치:
    Designer-A 가 베이스라인을 Retina(DPR=2) 로 캡처했다.
        light/dark : viewport 520×560 → PNG 1040×1120
        mobile     : viewport 375×720 → PNG 750×1440
    pytest-playwright 의 기본 `page` fixture 는 DPR=1 이라 베이스라인과
    픽셀 크기 자체가 다르다. 본 모듈은 `browser` fixture 로부터 변종별
    `device_scale_factor=2` context 를 직접 만들어 baseline 과 동일한
    물리 픽셀 크기를 확보한다 (focus-visible 패턴 동일).

캡처 시점 고정:
    fixture 가 초기 상태로 aria-selected/current/expanded 모두 표시한
    단일 frame 을 갖도록 마크업이 작성되어 있어 page.goto + networkidle
    만으로 baseline 과 동일한 frame 캡처가 가능하다.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import pytest
from playwright.sync_api import Browser, Page

from harness import snapshot

pytestmark = [pytest.mark.ui]

# 프로젝트 루트 — tests/ui/visual/test_aria_sync.py 기준 3 단계 위.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_URL = (PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "aria-sync-preview.html").as_uri()
ACTUAL_DIR = PROJECT_ROOT / "tests" / "ui" / "visual" / "_actual"

# Designer-A baseline 이 DPR=2 (Retina) 로 캡처되어 있어 동일 DPR 강제.
DEVICE_SCALE_FACTOR = 2


@contextmanager
def _make_page(
    browser: Browser,
    *,
    width: int,
    height: int,
    color_scheme: Literal["light", "dark", "no-preference"],
) -> Iterator[Page]:
    """variant 별 viewport·color-scheme·DPR 일치 context 에서 새 Page 생성."""
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


def _capture_and_compare(page: Page, variant: str) -> None:
    """fixture 페이지 로드 후 baseline 과 픽셀 비교.

    Raises:
        AssertionError: pixel diff > 0.1% 일 때 (spec §5.3)
    """
    ACTUAL_DIR.mkdir(parents=True, exist_ok=True)
    actual = ACTUAL_DIR / f"aria-sync-{variant}.png"
    page.goto(PREVIEW_URL)
    page.wait_for_load_state("networkidle")
    page.screenshot(path=str(actual))
    baseline = snapshot.baseline_path("aria-sync", variant)
    snapshot.assert_visual_match(actual, baseline, max_diff_pixel_ratio=0.001)


def test_aria_sync_light(browser: Browser) -> None:
    """라이트 모드 520×560 @2x — baseline-light(1040×1120) 와 픽셀 일치."""
    with _make_page(browser, width=520, height=560, color_scheme="light") as page:
        _capture_and_compare(page, "light")


def test_aria_sync_dark(browser: Browser) -> None:
    """다크 모드 520×560 @2x — baseline-dark(1040×1120) 와 픽셀 일치."""
    with _make_page(browser, width=520, height=560, color_scheme="dark") as page:
        _capture_and_compare(page, "dark")


def test_aria_sync_mobile(browser: Browser) -> None:
    """모바일 라이트 375×720 @2x — baseline-mobile(750×1440) 과 픽셀 일치."""
    with _make_page(browser, width=375, height=720, color_scheme="light") as page:
        _capture_and_compare(page, "mobile")
