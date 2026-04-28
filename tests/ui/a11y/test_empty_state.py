"""empty-state 접근성 (T-101) — axe-core wcag2a + wcag2aa + wcag21aa.

검증 룰셋: spec §5.3 + harness.a11y.DEFAULT_RULESET.
wcag21aaa 는 spec 범위 밖이므로 활성화하지 않음.

Red 의도성:
    fixture HTML 이 role=status, aria-hidden, lang 속성 등을 갖추고
    있으므로 자체로는 axe 위반이 0 일 가능성이 높다. 이는 NO-OP PASS 가
    아니라, **Frontend-A 가 SPA 구현 시 fixture 와 같은 ARIA 속성을
    누락하면 통합 e2e 가 잡아내도록 하는 계약** 이다.

    a11y 위반이 baseline 에서 0 인 게 정상 — 시각 축이 Red, 행동 축이
    PASS, a11y 축은 PASS 또는 색대비 위반(text-secondary 3.62:1 — mockup
    §6.3) 으로 FAIL 할 수 있다. axe-core 는 색대비 룰을 wcag2aa 에 포함하므로
    설명문의 #86868B on #FFFFFF 가 4.5:1 미달이면 violations 에 잡힌다.
    이는 design.md 토큰의 알려진 한계이며 spec §6.3 에 후속 티켓 권장 사항
    으로 명시됨. 본 시나리오는 axe 의 정확한 결과를 그대로 보고 — 결과를
    숨기지 않는다.
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
    PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "empty-state-preview.html"
).as_uri()


def test_empty_state_no_axe_violations(page: Page) -> None:
    """Given: 빈 상태 fixture 페이지
    When:  axe-core 를 wcag2a + wcag2aa + wcag21aa 룰셋으로 실행
    Then:  위반 0건 (spec §5.3)
    """
    page.goto(PREVIEW_URL)
    axe = Axe()
    results = axe.run(
        page,
        options={"runOnly": {"type": "tag", "values": list(DEFAULT_RULESET)}},
    )
    violations = results.response.get("violations", [])
    assert violations == [], (
        "a11y violations found:\n"
        + "\n".join(
            f"  - {v['id']} ({v['impact']}): {v['help']}\n    nodes: {len(v.get('nodes', []))}"
            for v in violations
        )
    )


def test_empty_state_role_status_present(page: Page) -> None:
    """Given: 3 위치의 빈 상태
    When:  role=status 셀렉터로 찾는다
    Then:  3 개 모두 발견된다 (스크린 리더가 비동기 로드 후 빈 상태를 인식)
    """
    page.goto(PREVIEW_URL)
    statuses = page.locator('[data-empty] [role="status"]')
    assert statuses.count() == 3, (
        f"role=status 가 3 개여야 함 (meeting-list / search / chat), 실제={statuses.count()}"
    )


def test_empty_state_decorative_icons_aria_hidden(page: Page) -> None:
    """Given: 빈 상태의 장식용 SVG 아이콘
    When:  aria-hidden 속성을 확인
    Then:  모두 'true' 로 스크린 리더가 무시한다 (텍스트가 의미 전달, WCAG 1.1.1)
    """
    page.goto(PREVIEW_URL)
    icons = page.locator(".empty-state-icon")
    count = icons.count()
    assert count == 3, f"icon 셀렉터가 3 개를 찾아야 함, 실제={count}"
    for i in range(count):
        attr = icons.nth(i).get_attribute("aria-hidden")
        assert attr == "true", (
            f"icon[{i}] aria-hidden='{attr}' (요구: 'true')"
        )
