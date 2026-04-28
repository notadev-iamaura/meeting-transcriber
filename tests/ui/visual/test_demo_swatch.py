"""Wave 1 시작 전 하네스 동작 검증용 demo — 시각 회귀 축.

본 파일은 Plan 1 (Wave 1 Visual Polish) 시작 시 제거 예정.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from harness import snapshot

pytestmark = [pytest.mark.ui]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS_DIR = PROJECT_ROOT / "tests" / "ui" / "visual" / "_actual"


def _capture_and_compare(page: Page, variant: str) -> None:
    """공용 — page.screenshot() 후 베이스라인과 픽셀 비교."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    actual = ARTIFACTS_DIR / f"demo-swatch-{variant}.png"
    page.screenshot(path=str(actual), full_page=True)
    baseline = snapshot.baseline_path("demo-swatch", variant)
    snapshot.assert_visual_match(actual, baseline, max_diff_pixel_ratio=0.001)


def test_demo_swatch_light(page: Page, demo_swatch_url: str) -> None:
    """라이트 모드 — 베이스라인 비교."""
    page.goto(demo_swatch_url)
    expect(page.locator("h1")).to_have_text("디자인 토큰 견본")
    _capture_and_compare(page, "light")


def test_demo_swatch_dark(page: Page, demo_swatch_url: str) -> None:
    page.emulate_media(color_scheme="dark")
    page.goto(demo_swatch_url)
    _capture_and_compare(page, "dark")


def test_demo_swatch_mobile(page: Page, demo_swatch_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto(demo_swatch_url)
    _capture_and_compare(page, "mobile")
