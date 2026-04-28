"""empty-state 시각 회귀 (T-101) — 라이트/다크/모바일 3 변종.

검증 대상:
    `tests/ui/_fixtures/empty-state-preview.html` 가 `ui/web/style.css` 의
    .empty-state* 토큰·레이아웃을 상속받아 렌더된 결과가
    `tests/ui/visual/baselines/empty-state-{light,dark,mobile}.png` 와
    픽셀 diff < 0.1% (spec §5.3) 로 일치하는가.

Red 의도성:
    fixture HTML 은 inline 스타일을 두지 않고 style.css 에 의존한다.
    Frontend-A 가 style.css 에 mockup §3 의 .empty-state* 골격을 추가하기
    전까지는 폰트 크기·색·여백이 baseline 과 다르게 렌더되어 visual 이
    정확히 FAIL 한다. Frontend-A 가 추가하면 자동으로 PASS.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page

from harness import snapshot

pytestmark = [pytest.mark.ui]

# 프로젝트 루트 — tests/ui/visual/test_empty_state.py 기준 3 단계 위.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_URL = (
    PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "empty-state-preview.html"
).as_uri()
ACTUAL_DIR = PROJECT_ROOT / "tests" / "ui" / "visual" / "_actual"


def _capture_and_compare(page: Page, variant: str) -> None:
    """현재 페이지를 캡처해 baseline 과 픽셀 비교.

    Args:
        page: Playwright Page (테스트별로 viewport·color-scheme 사전 설정됨)
        variant: light / dark / mobile

    Raises:
        AssertionError: pixel diff > 0.1% 일 때 (spec §5.3)
    """
    ACTUAL_DIR.mkdir(parents=True, exist_ok=True)
    actual = ACTUAL_DIR / f"empty-state-{variant}.png"
    # full_page=True — baseline 도 full_page 로 캡처되었음 (1024×768 / 375×667).
    # offscreen 변종(검색·채팅)은 top: 2000px+ 라 viewport 밖이지만 full_page 는
    # body 의 실제 높이까지 캡처할 수 있으므로 body 의 min-height 를 768px 로
    # 고정해 baseline 과 일치하도록 했다.
    page.screenshot(path=str(actual), full_page=False)
    baseline = snapshot.baseline_path("empty-state", variant)
    snapshot.assert_visual_match(actual, baseline, max_diff_pixel_ratio=0.001)


def test_empty_state_visual_light(page: Page) -> None:
    """라이트 모드 1024×768 — baseline-light 와 픽셀 일치."""
    page.set_viewport_size({"width": 1024, "height": 768})
    page.emulate_media(color_scheme="light")
    page.goto(PREVIEW_URL)
    page.wait_for_load_state("networkidle")
    _capture_and_compare(page, "light")


def test_empty_state_visual_dark(page: Page) -> None:
    """다크 모드 1024×768 — baseline-dark 와 픽셀 일치."""
    page.set_viewport_size({"width": 1024, "height": 768})
    page.emulate_media(color_scheme="dark")
    page.goto(PREVIEW_URL)
    page.wait_for_load_state("networkidle")
    _capture_and_compare(page, "dark")


def test_empty_state_visual_mobile(page: Page) -> None:
    """모바일 라이트 375×667 — baseline-mobile 과 픽셀 일치."""
    page.set_viewport_size({"width": 375, "height": 667})
    page.emulate_media(color_scheme="light")
    page.goto(PREVIEW_URL)
    page.wait_for_load_state("networkidle")
    _capture_and_compare(page, "mobile")
