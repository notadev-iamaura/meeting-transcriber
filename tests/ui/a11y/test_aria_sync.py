"""aria-sync — axe-core wcag2a/aa/21aa 위반 0 (T-301).

검증 룰셋: spec §5.3 + harness.a11y.DEFAULT_RULESET.

Red 의도성:
    mockup §1.1 / §1.3 의 ARIA 속성 계약 위반을 잡기 위한 축약 계약:
        - aria-current 는 enumerated value 만 (page/step/location/...)
        - aria-selected/aria-expanded 는 string "true"/"false" 만
        - role="option" 의 부모는 listbox/group 필수 (aria-required-parent)
        - role 별 허용 ARIA 속성 외 사용 차단 (aria-allowed-attr)
"""
from __future__ import annotations

from pathlib import Path

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page

from harness.a11y import DEFAULT_RULESET

pytestmark = [pytest.mark.ui]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_URL = (
    PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "aria-sync-preview.html"
).as_uri()


def test_aria_sync_no_axe_violations(page: Page) -> None:
    """axe-core wcag2a + wcag2aa + wcag21aa 룰 위반 0 검증 (spec §5.3).

    Given: aria-sync fixture 페이지
    When:  axe-core 를 DEFAULT_RULESET 으로 실행
    Then:  위반 0 건 — aria-allowed-attr / aria-required-parent /
           aria-valid-attr-value (mockup §5) 모두 통과.
    """
    page.goto(PREVIEW_URL)
    axe = Axe()
    results = axe.run(
        page,
        options={
            "runOnly": {"type": "tag", "values": list(DEFAULT_RULESET)},
        },
    )
    violations = results.response.get("violations", [])
    assert violations == [], (
        "a11y violations found:\n"
        + "\n".join(
            f"  - {v['id']} ({v['impact']}): {v['help']}\n"
            f"    nodes: {len(v.get('nodes', []))}"
            for v in violations
        )
    )


def test_aria_attribute_values_valid(page: Page) -> None:
    """aria-selected, aria-current, aria-expanded 값이 ARIA 표준 enum.

    Given: aria-sync fixture 의 ARIA 속성 모음
    When:  각 속성 값을 ARIA 사양 enum 과 비교
    Then:  - aria-selected ∈ {"true", "false"}
           - aria-current ∈ {"page", "step", "location", "date", "time",
                             "true", "false"}
           - aria-expanded ∈ {"true", "false"}
    """
    page.goto(PREVIEW_URL)

    # aria-selected: true|false
    for el in page.locator("[aria-selected]").all():
        v = el.get_attribute("aria-selected")
        assert v in ("true", "false"), f"aria-selected 값 오류: {v!r}"

    # aria-current: enumerated (mockup §1.1 — boolean true 금지)
    for el in page.locator("[aria-current]").all():
        v = el.get_attribute("aria-current")
        assert v in ("page", "step", "location", "date", "time", "true", "false"), (
            f"aria-current 값 오류: {v!r}"
        )

    # aria-expanded: true|false (mockup §1.1 — 제거 금지)
    for el in page.locator("[aria-expanded]").all():
        v = el.get_attribute("aria-expanded")
        assert v in ("true", "false"), f"aria-expanded 값 오류: {v!r}"
