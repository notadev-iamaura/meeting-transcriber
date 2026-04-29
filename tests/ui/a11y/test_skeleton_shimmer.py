"""skeleton-shimmer 접근성 (T-103) — axe-core wcag2a + wcag2aa + wcag21aa.

검증 룰셋: spec §5.3 + harness.a11y.DEFAULT_RULESET.
wcag21aaa 는 spec 범위 밖이므로 활성화하지 않음.

Red 의도성:
    스켈레톤은 시각 placeholder 이므로 텍스트가 없고, 모든 .skeleton-card /
    lines 컨테이너에 aria-hidden="true" 가 명시되어 스크린 리더가 무시한다.
    텍스트 색 대비 룰은 텍스트가 없어 N/A. 따라서 axe 위반이 0 인 게 정상.

    본 시나리오의 진짜 의도: Frontend-A 가 SPA 구현 시 fixture 와 같은
    aria-hidden 속성을 누락하면 통합 e2e 가 잡아내도록 하는 마크업 계약.
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
    PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "skeleton-shimmer-preview.html"
).as_uri()


def test_skeleton_shimmer_no_axe_violations(page: Page) -> None:
    """axe-core wcag2a + wcag2aa + wcag21aa 룰 위반 0 검증 (spec §5.3).

    Given: 스켈레톤 fixture 페이지
    When:  axe-core 를 wcag2a + wcag2aa + wcag21aa 룰셋으로 실행
    Then:  위반 0건
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
    assert violations == [], "a11y violations found:\n" + "\n".join(
        f"  - {v['id']} ({v['impact']}): {v['help']}\n    nodes: {len(v.get('nodes', []))}"
        for v in violations
    )


def test_all_skeleton_containers_aria_hidden(page: Page) -> None:
    """Given: 카드형 .skeleton-card × 3 + 라인형 lines 섹션 1 개 (mockup §5.3)
    When:  aria-hidden 속성을 확인
    Then:  모두 'true' — 스크린 리더가 시각 placeholder 를 텍스트로 읽지 않는다.
           외부 status wrapper 의 sr-only 텍스트만 읽힘 (Frontend-A 가 SPA 통합
           시 추가, 본 fixture 는 단독 컴포넌트 시각 계약만 다룸).
    """
    page.goto(PREVIEW_URL)
    cards = page.locator(".skeleton-card")
    lines_section = page.locator('[data-skeleton="lines"]')

    card_count = cards.count()
    assert card_count == 3, f"카드 카운트가 3 이어야 함, 실제={card_count}"
    for i in range(card_count):
        attr = cards.nth(i).get_attribute("aria-hidden")
        assert attr == "true", f"skeleton-card[{i}] aria-hidden='{attr}' (요구: 'true')"

    section_attr = lines_section.get_attribute("aria-hidden")
    assert section_attr == "true", f"lines 섹션 aria-hidden='{section_attr}' (요구: 'true')"
