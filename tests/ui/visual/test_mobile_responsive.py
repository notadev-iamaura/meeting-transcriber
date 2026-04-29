"""mobile-responsive — drawer closed/open 시각 회귀 (T-302).

검증 대상:
    `tests/ui/_fixtures/mobile-responsive-preview.html` 가 mockup §3 마크업
    인터페이스 그대로 햄버거 + drawer + 백드롭을 렌더링한 결과가
    `tests/ui/visual/baselines/mobile-responsive-{closed,open}.png` 와 픽셀
    diff < 0.1% (spec §5.3) 로 일치하는가.

DPR (device_scale_factor) 일치:
    Designer-A 가 베이스라인을 mobile 375×667 @ DPR=2 로 캡처 → PNG 750×1334.
    pytest-playwright 의 기본 `page` fixture 는 DPR=1 이라 베이스라인과
    픽셀 크기 자체가 다르다. 본 모듈은 `browser` fixture 로부터 변종별
    `device_scale_factor=2` context 를 직접 만들어 baseline 과 동일한
    물리 픽셀 크기를 확보한다 (aria-sync / focus-visible 패턴 동일).

변종 (mockup §7):
    closed : 페이지 로드 직후 — 햄버거만 보임, 사이드바/백드롭 숨김
    open   : 햄버거 클릭 후 transition 완료 (~250ms 대기) — drawer 슬라이드인,
             백드롭 dim, 햄버거 aria-expanded="true"

baseline 경로 직접 구성:
    `harness.snapshot.baseline_path()` 헬퍼는 SUPPORTED_VARIANTS = (light,
    dark, mobile) 만 허용하므로, 본 티켓은 `Path("tests/ui/visual/baselines")
    / f"mobile-responsive-{variant}.png"` 로 직접 구성한다 (mockup §7.2).
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page

from harness import snapshot

pytestmark = [pytest.mark.ui]

# 프로젝트 루트 — tests/ui/visual/test_mobile_responsive.py 기준 3 단계 위.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_URL = (
    PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "mobile-responsive-preview.html"
).as_uri()
ACTUAL_DIR = PROJECT_ROOT / "tests" / "ui" / "visual" / "_actual"
BASELINES_DIR = PROJECT_ROOT / "tests" / "ui" / "visual" / "baselines"

# Designer-A baseline 이 DPR=2 (Retina) 로 캡처되어 있어 동일 DPR 강제.
DEVICE_SCALE_FACTOR = 2


@contextmanager
def _make_page(browser: Browser) -> Iterator[Page]:
    """모바일 viewport(375×667) + DPR=2 context 에서 새 Page 생성."""
    context = browser.new_context(
        viewport={"width": 375, "height": 667},
        device_scale_factor=DEVICE_SCALE_FACTOR,
        color_scheme="light",
    )
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


def _capture_and_compare(page: Page, variant: str) -> None:
    """fixture 페이지를 캡처해 baseline 과 픽셀 비교.

    baseline 경로는 mockup §7.2 의 직접 구성 패턴을 사용한다 — closed/open
    변종이 SUPPORTED_VARIANTS (light/dark/mobile) 에 없기 때문.

    Raises:
        AssertionError: pixel diff > 0.1% 일 때 (spec §5.3)
    """
    ACTUAL_DIR.mkdir(parents=True, exist_ok=True)
    actual = ACTUAL_DIR / f"mobile-responsive-{variant}.png"
    page.screenshot(path=str(actual))
    baseline = BASELINES_DIR / f"mobile-responsive-{variant}.png"
    snapshot.assert_visual_match(actual, baseline, max_diff_pixel_ratio=0.001)


def test_mobile_responsive_closed(browser: Browser) -> None:
    """drawer 닫힘 상태 — 햄버거만 보이는 초기 화면 (mobile 375×667 @2x)."""
    with _make_page(browser) as page:
        page.goto(PREVIEW_URL)
        page.wait_for_load_state("networkidle")
        _capture_and_compare(page, "closed")


def test_mobile_responsive_open(browser: Browser) -> None:
    """drawer 열림 상태 — 햄버거 클릭 후 사이드바 슬라이드인 + 백드롭."""
    with _make_page(browser) as page:
        page.goto(PREVIEW_URL)
        page.wait_for_load_state("networkidle")
        page.locator("#mobile-menu-toggle").click()
        # transition 완료 대기 (--duration-base = 250ms + 여유)
        page.wait_for_timeout(400)
        _capture_and_compare(page, "open")
