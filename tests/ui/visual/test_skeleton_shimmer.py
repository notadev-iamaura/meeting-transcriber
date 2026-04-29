"""skeleton-shimmer 시각 회귀 (T-103) — 라이트/다크/모바일 3 변종.

검증 대상:
    `tests/ui/_fixtures/skeleton-shimmer-preview.html` 가 `ui/web/style.css`
    §11-10 의 .skeleton-card / .skeleton-line 정의 + @keyframes shimmer 를
    상속받아 렌더된 결과가 `tests/ui/visual/baselines/skeleton-shimmer-
    {light,dark,mobile}.png` 와 픽셀 diff < 0.1% (spec §5.3) 로 일치하는가.

shimmer 애니메이션 고정:
    fixture HTML 의 인라인 <style> 이 `.skeleton-line` 에 대해
    `animation-play-state: paused + animation-delay: 0s +
    background-position: 0% 0` 을 강제하므로, Playwright 캡처 시점에
    baseline 캡처와 동일한 한 frame 에 고정된다 → 픽셀 재현성 확보.

Red 의도성 (fixture-as-source-of-truth):
    fixture · baseline · style.css 가 모두 동일한 marker 를 사용하므로 본
    시각 시나리오는 baseline 캡처 시점부터 PASS 가 정상이다. 해당 PASS 의
    의미: SPA(spa.js) 의 4 위치 로딩 마크업이 fixture 와 동일하게 .skeleton-card
    / .skeleton-line + width 변종을 사용하도록 Frontend-A 가 마이그레이션할
    때 시각 계약(가시적 형태)의 ground truth 를 본 fixture 가 제공한다.
    SPA 통합 시점에 spa.js 가 다른 마크업을 쓰면 SPA 통합 e2e 가 잡아낸다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page

from harness import snapshot

pytestmark = [pytest.mark.ui]

# 프로젝트 루트 — tests/ui/visual/test_skeleton_shimmer.py 기준 3 단계 위.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_URL = (
    PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "skeleton-shimmer-preview.html"
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
    actual = ACTUAL_DIR / f"skeleton-shimmer-{variant}.png"
    page.screenshot(path=str(actual))
    baseline = snapshot.baseline_path("skeleton-shimmer", variant)
    snapshot.assert_visual_match(actual, baseline, max_diff_pixel_ratio=0.001)


def test_skeleton_shimmer_light(page: Page) -> None:
    """라이트 모드 1024×768 — baseline-light 와 픽셀 일치."""
    page.set_viewport_size({"width": 1024, "height": 768})
    page.emulate_media(color_scheme="light")
    page.goto(PREVIEW_URL)
    page.wait_for_load_state("networkidle")
    _capture_and_compare(page, "light")


def test_skeleton_shimmer_dark(page: Page) -> None:
    """다크 모드 1024×768 — baseline-dark 와 픽셀 일치."""
    page.set_viewport_size({"width": 1024, "height": 768})
    page.emulate_media(color_scheme="dark")
    page.goto(PREVIEW_URL)
    page.wait_for_load_state("networkidle")
    _capture_and_compare(page, "dark")


def test_skeleton_shimmer_mobile(page: Page) -> None:
    """모바일 라이트 375×667 — baseline-mobile 과 픽셀 일치."""
    page.set_viewport_size({"width": 375, "height": 667})
    page.emulate_media(color_scheme="light")
    page.goto(PREVIEW_URL)
    page.wait_for_load_state("networkidle")
    _capture_and_compare(page, "mobile")
