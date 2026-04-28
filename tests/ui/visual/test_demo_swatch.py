"""Wave 1 시작 전 하네스 동작 검증용 demo — 시각 회귀 축.

Python Playwright sync API 는 Node 진영의 `expect(page).to_have_screenshot()`
와 동등한 자동 비교 헬퍼를 제공하지 않는다. 따라서 본 데모는 `page.screenshot()`
으로 PNG 를 `tests/ui/visual/baselines/` 에 저장하고, Wave 1+ 의 실제 회귀
테스트에서는 이미지 diff 라이브러리(예: pixelmatch / Pillow) 또는 별도
테스트 러너로 비교 로직을 추가할 예정이다.

본 파일은 Plan 1 (Wave 1 Visual Polish) 시작 시 제거 예정.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from harness.snapshot import baseline_path

pytestmark = [pytest.mark.ui]


def _capture(page: Page, component: str, variant: str) -> Path:
    """베이스라인 PNG 저장 후 경로 반환."""
    out = baseline_path(component, variant)
    out.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out), full_page=True)
    return out


def test_demo_swatch_light(page: Page, demo_swatch_url: str) -> None:
    """라이트 모드 베이스라인 생성 + 기본 렌더 확인."""
    page.goto(demo_swatch_url)
    expect(page.locator("h1")).to_have_text("디자인 토큰 견본")
    out = _capture(page, "demo-swatch", "light")
    assert out.exists() and out.stat().st_size > 0


def test_demo_swatch_dark(page: Page, demo_swatch_url: str) -> None:
    """다크 모드 베이스라인 생성."""
    page.emulate_media(color_scheme="dark")
    page.goto(demo_swatch_url)
    out = _capture(page, "demo-swatch", "dark")
    assert out.exists() and out.stat().st_size > 0


def test_demo_swatch_mobile(page: Page, demo_swatch_url: str) -> None:
    """모바일 뷰포트 베이스라인 생성."""
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto(demo_swatch_url)
    out = _capture(page, "demo-swatch", "mobile")
    assert out.exists() and out.stat().st_size > 0
