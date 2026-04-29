"""mobile-responsive — axe-core wcag2a/aa/21aa 위반 0 (T-302).

검증 룰셋: spec §5.3 + harness.a11y.DEFAULT_RULESET.

Red 의도성 (mockup §6.4):
    drawer 의 닫힘/열림 두 상태 모두 axe 무결성 통과해야 한다.
        - aria-valid-attr-value: aria-expanded 값은 string "true"/"false"
        - button-name: 햄버거 버튼은 aria-label 로 접근 가능 이름 보장
        - aria-allowed-attr: aside 에 aria-modal/aria-expanded 모두 허용
        - color-contrast: 햄버거 텍스트 vs 배경 4.5:1 이상
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser, Page

from harness.a11y import DEFAULT_RULESET

pytestmark = [pytest.mark.ui]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_URL = (
    PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "mobile-responsive-preview.html"
).as_uri()


@contextmanager
def _mobile_page(browser: Browser) -> Iterator[Page]:
    """모바일 viewport(375×667) context — 햄버거가 보이는 분기 활성화."""
    context = browser.new_context(
        viewport={"width": 375, "height": 667},
        color_scheme="light",
    )
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


def _run_axe(page: Page) -> list[dict]:
    axe = Axe()
    results = axe.run(
        page,
        options={
            "runOnly": {"type": "tag", "values": list(DEFAULT_RULESET)},
        },
    )
    return results.response.get("violations", [])


def test_no_axe_violations_closed(browser: Browser) -> None:
    """drawer 닫힘 상태 — axe wcag2a + wcag2aa + wcag21aa 위반 0."""
    with _mobile_page(browser) as page:
        page.goto(PREVIEW_URL)
        page.wait_for_load_state("networkidle")
        violations = _run_axe(page)
        assert violations == [], (
            "a11y violations (closed) found:\n"
            + "\n".join(
                f"  - {v['id']} ({v['impact']}): {v['help']}\n"
                f"    nodes: {len(v.get('nodes', []))}"
                for v in violations
            )
        )


def test_no_axe_violations_open(browser: Browser) -> None:
    """drawer 열림 상태 — aria-modal/aria-expanded='true' 부여 후 위반 0."""
    with _mobile_page(browser) as page:
        page.goto(PREVIEW_URL)
        page.wait_for_load_state("networkidle")
        page.locator("#mobile-menu-toggle").click()
        page.wait_for_timeout(400)  # transition 완료 대기
        violations = _run_axe(page)
        assert violations == [], (
            "a11y violations (open) found:\n"
            + "\n".join(
                f"  - {v['id']} ({v['impact']}): {v['help']}\n"
                f"    nodes: {len(v.get('nodes', []))}"
                for v in violations
            )
        )
