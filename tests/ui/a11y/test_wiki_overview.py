"""wiki-overview-search — axe-core 룰셋 + ARIA 탭/카드 계약 검증.

티켓: T-303 (wave 3, component `wiki-overview-search`)
계획서: memorable-wiki §5 C3 "UI — 현황 화면 + 검색 메타"
디자인 산출물(목업): wiki-overview-search 마크다운 목업 + design.md 토큰 매핑

검증 룰셋: spec §5.3 + harness.a11y.DEFAULT_RULESET = (wcag2a, wcag2aa, wcag21aa).
wcag21aaa 는 spec 범위 밖이므로 비활성(절대 금지).

검증 시나리오:
    AX1 — 탭은 role=tab + aria-selected + aria-controls + tablist 안에 위치
    AX2 — tabpanel 은 role=tabpanel + aria-labelledby(탭과 연결)
    AX3 — 아이콘 전용 버튼(인용 등)·인터랙티브 요소 접근가능한 이름 보유
    AX4 — 현황 탭 + 탭 바 영역 axe scoped scan 위반 0 (WCAG 2.1 AA)
    AX5 — 검색 결과 카드 영역 axe scoped scan 위반 0
    AX6 — 키보드만으로 현황↔검색 탭 + 결과 카드 + 인용 링크 도달 가능
    AX7 — 인터랙티브 요소(탭/카드/인용)에 :focus-visible 포커스 링 표시

review 거짓 Red 해소:
    페이지 전체 axe 스캔은 기존 SPA(사이드바·헤더)의 color-contrast 위반을
    상속해 영구 FAIL 한다(bulk-actions a11y review-2b §2 와 동일 함정). 따라서
    AX4/AX5 는 `context.include` 로 wiki-overview-search 컴포넌트 영역만 한정
    스캔한다. 컴포넌트 마커(`data-component='wiki-overview-search'`) 가
    미부여된 경우를 대비해 fallback selector(`.wiki-overview`, `.wiki-tabs`,
    `.wiki-result-card`) 도 함께 시도한다.

Red 의도성 (구현 전):
    현재 `ui/web/wiki-view.js` 에 탭 구조·카드 구조 자체가 없으므로 본
    모듈은 모두 selector 부재 / 속성 부재로 깨끗한 FAIL 한다. AX4/AX5 는
    컴포넌트 DOM 미존재로 "matched node 0" 사전 검증에서 실패(거짓 PASS
    방지). 개별 ARIA 계약(AX1~AX3, AX6~AX7)도 명확히 FAIL.

fixture 전략:
    behavior 와 동일하게 conftest 의 `ui_bulk_base_url` 실서버를 재사용하고
    digest/search 응답을 `page.route` 로 mock 해 결정론적 DOM 을 만든다.
"""

from __future__ import annotations

import json

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser, Page

from harness.a11y import DEFAULT_RULESET

pytestmark = [pytest.mark.ui]


# ============================================================================
# Mock 데이터 (behavior 모듈과 동일 계약 — a11y 자급자족 위해 재정의)
# ============================================================================

_MOCK_MEETING_ID = "meeting_20260605_143000"

_DIGEST_RESPONSE = {
    "generated_for": "2026-06-08",
    "total_open_actions": 3,
    "open_actions": [
        {
            "owner": "김민수",
            "items": [
                {
                    "description": "런칭 날짜 확정 후 공유",
                    "citations": [f"[meeting:{_MOCK_MEETING_ID}@00:23:45]"],
                    "due_date": "2026-06-15",
                },
            ],
        },
        {
            "owner": "이지은",
            "items": [
                {
                    "description": "디자인 토큰 정리",
                    "citations": [f"[meeting:{_MOCK_MEETING_ID}@00:08:11]"],
                    "due_date": None,
                },
            ],
        },
    ],
    "recent_decisions": [
        {
            "page_path": "decisions/2026-06-05-launch-date.md",
            "title": "런칭일을 6월 30일로 확정",
            "decision_date": "2026-06-05",
            "status": "decided",
            "project": "recap-launch",
            "citations": [f"[meeting:{_MOCK_MEETING_ID}@00:23:45]"],
        },
    ],
    "project_status": [
        {
            "project": "recap-launch",
            "last_title": "런칭일을 6월 30일로 확정",
            "last_date": "2026-06-05",
            "status": "decided",
            "page_path": "decisions/2026-06-05-launch-date.md",
        },
    ],
}

_SEARCH_RESPONSE = {
    "query": "런칭",
    "total": 2,
    "results": [
        {
            "path": "decisions/2026-06-05-launch-date.md",
            "type": "decisions",
            "title": "런칭일을 6월 30일로 확정",
            "snippet": "팀은 6월 30일 런칭에 합의했고 마케팅은…",
            "score": 0.92,
            "citations": [
                f"[meeting:{_MOCK_MEETING_ID}@00:23:45]",
                f"[meeting:{_MOCK_MEETING_ID}@00:41:02]",
            ],
            "metadata": {"status": "decided", "project": "recap-launch"},
        },
        {
            "path": "topics/launch-planning.md",
            "type": "topics",
            "title": "런칭 준비 주제",
            "snippet": "런칭 준비 항목 정리 — QA, 마케팅, 인프라…",
            "score": 0.78,
            "citations": [f"[meeting:{_MOCK_MEETING_ID}@00:12:08]"],
            "metadata": {"project": "recap-launch"},
        },
    ],
}


# ============================================================================
# Route mock + 헬퍼
# ============================================================================


def _install_wiki_mocks(page: Page) -> None:
    """`/api/wiki/digest`·`/api/wiki/search` 응답을 결정론적으로 고정한다."""

    def _digest(route, _req):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_DIGEST_RESPONSE, ensure_ascii=False),
        )

    def _search(route, _req):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_SEARCH_RESPONSE, ensure_ascii=False),
        )

    page.route("**/api/wiki/digest", _digest)
    page.route("**/api/wiki/search?**", _search)
    page.route("**/api/wiki/search", _search)


@pytest.fixture
def wiki_page(browser: Browser, ui_bulk_base_url: str) -> Page:
    """`/app/wiki` (WikiView) 를 로드한 Page — axe 주입 대상."""
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        color_scheme="light",
    )
    page = context.new_page()
    _install_wiki_mocks(page)
    # networkidle 은 부하 하 flaky → domcontentloaded + 명시적 셀렉터 대기.
    page.goto(f"{ui_bulk_base_url}/app/wiki", wait_until="domcontentloaded")
    page.wait_for_selector(".wiki-view", timeout=15000)
    page.wait_for_selector("#wikiOverviewPanel", timeout=15000)
    yield page
    context.close()


def _switch_to_search_tab(page: Page) -> None:
    """검색 탭으로 전환하고 결과 카드가 렌더될 때까지 검색을 수행한다."""
    page.locator("#wikiTabSearch[role='tab']").click()
    page.wait_for_timeout(150)
    page.fill("#wikiSearchInput", "런칭")
    page.wait_for_selector(".wiki-result-card", timeout=5000)


def _run_axe_on(page: Page, include_selectors: list[str]) -> tuple[list[dict], dict]:
    """axe-core 를 컴포넌트 한정(`context.include`) 으로 실행.

    bulk-actions a11y(review-2b §2) 와 동일하게 페이지 전체가 아닌
    wiki-overview-search 컴포넌트 영역만 스캔해 기존 SPA 위반을 상속하지
    않는다. DEFAULT_RULESET(wcag2a + wcag2aa + wcag21aa) 만 사용 —
    wcag21aaa 비활성.

    Args:
        page: Playwright Page.
        include_selectors: include scope CSS selector 리스트.

    Returns:
        (violations, raw) — violations 리스트 + axe raw 응답(matched node 추적).
    """
    axe = Axe()
    results = axe.run(
        page,
        context={"include": [[sel] for sel in include_selectors]},
        options={
            "runOnly": {"type": "tag", "values": list(DEFAULT_RULESET)},
        },
    )
    raw = results.response
    return raw.get("violations", []), raw


def _matched_nodes(raw: dict) -> int:
    """axe raw 응답에서 스캔된 노드 총수(컴포넌트 존재 여부 판정용)."""
    return sum(
        len(check.get("nodes", []))
        for kind in ("passes", "violations", "incomplete", "inapplicable")
        for check in raw.get(kind, [])
    )


def _format_violations(violations: list[dict]) -> str:
    return "\n".join(
        f"  - {v['id']} ({v.get('impact')}): {v.get('help')} — nodes: {len(v.get('nodes', []))}"
        for v in violations
    )


# ============================================================================
# AX1 — 탭 ARIA 계약
# ============================================================================


def test_AX1_탭은_role_tab과_aria_selected_aria_controls를_보유한다(wiki_page: Page) -> None:
    """Given: /app/wiki 진입(탭 바 마운트)
    When:  두 탭의 role / aria-selected / aria-controls 조회
    Then:  role='tab', aria-selected 보유, aria-controls 가 패널 ID 를 가리킴.

    근거: acceptance [a11y] "탭은 role=tab/tablist/tabpanel + aria-selected +
          aria-controls 연결", 목업 §1 ARIA 계약.
    """
    tablist = wiki_page.locator("[role='tablist']")
    assert tablist.count() == 1, "role='tablist' 컨테이너 정확히 1 개 필요"
    tabs = tablist.locator("[role='tab']")
    assert tabs.count() == 2, f"탭 2 개 필요 (got {tabs.count()})"

    overview = wiki_page.locator("#wikiTabOverview[role='tab']")
    search = wiki_page.locator("#wikiTabSearch[role='tab']")
    # aria-selected 는 두 탭 모두 보유(true/false).
    assert overview.get_attribute("aria-selected") in ("true", "false"), (
        "현황 탭 aria-selected 보유 필수"
    )
    assert search.get_attribute("aria-selected") in ("true", "false"), (
        "검색 탭 aria-selected 보유 필수"
    )
    # aria-controls 가 실제 패널 ID 를 가리킴.
    assert overview.get_attribute("aria-controls") == "wikiOverviewPanel"
    assert search.get_attribute("aria-controls") == "wikiSearchPanel"


# ============================================================================
# AX2 — tabpanel ARIA 계약
# ============================================================================


def test_AX2_tabpanel은_role과_aria_labelledby로_탭과_연결된다(wiki_page: Page) -> None:
    """Given: 현황/검색 패널
    When:  패널의 role / aria-labelledby 조회
    Then:  role='tabpanel' + aria-labelledby 가 대응 탭 ID 를 가리킴.

    근거: 목업 §1 "tabpanel role=tabpanel aria-labelledby=wikiTabOverview".
    """
    overview_panel = wiki_page.locator("#wikiOverviewPanel")
    assert overview_panel.get_attribute("role") == "tabpanel", "현황 패널 role='tabpanel' 필수"
    assert overview_panel.get_attribute("aria-labelledby") == "wikiTabOverview", (
        "현황 패널 aria-labelledby='wikiTabOverview' 필수"
    )
    search_panel = wiki_page.locator("#wikiSearchPanel")
    assert search_panel.get_attribute("role") == "tabpanel", "검색 패널 role='tabpanel' 필수"
    assert search_panel.get_attribute("aria-labelledby") == "wikiTabSearch", (
        "검색 패널 aria-labelledby='wikiTabSearch' 필수"
    )


# ============================================================================
# AX3 — 인용 anchor 접근가능한 이름
# ============================================================================


def test_AX3_인용_anchor는_접근가능한_이름을_보유한다(wiki_page: Page) -> None:
    """Given: 현황 패널의 인용 anchor
    When:  .wiki-citation 의 텍스트/aria-label/title 조회
    Then:  비어있지 않은 접근가능한 이름(타임스탬프 또는 라벨) 보유.

    근거: acceptance [a11y] "아이콘 전용 버튼은 aria-label 보유" — 인용은
          타임스탬프 텍스트 + title 을 가져 SR 에 위치를 안내한다.
    """
    panel = wiki_page.locator("#wikiOverviewPanel[role='tabpanel']")
    citation = panel.locator(".wiki-citation").first
    assert citation.count() >= 1, "현황 패널에 인용 anchor(.wiki-citation) 필요"
    text = (citation.text_content() or "").strip()
    aria_label = citation.get_attribute("aria-label") or ""
    title = citation.get_attribute("title") or ""
    assert text or aria_label or title, (
        "인용 anchor 는 접근가능한 이름(텍스트/aria-label/title)을 가져야 함"
    )


# ============================================================================
# AX4 — 현황 탭 + 탭 바 영역 axe 위반 0
# ============================================================================


def test_AX4_현황탭_영역_axe_위반_0(wiki_page: Page) -> None:
    """Given: /app/wiki 현황 탭 활성
    When:  탭 바 + 현황 패널 영역만 한정 axe 스캔
    Then:  wcag2a + wcag2aa + wcag21aa 위반 0.

    근거: acceptance [a11y] "현황 탭·검색 탭 영역 axe-core scoped scan 위반 0".
    """
    wiki_page.wait_for_selector("#wikiOverviewPanel", timeout=10000)
    wiki_page.wait_for_timeout(200)
    violations, raw = _run_axe_on(
        wiki_page,
        [
            "[data-component='wiki-overview-search']",
            "[role='tablist']",
            "#wikiOverviewPanel",
            ".wiki-overview",
        ],
    )
    assert _matched_nodes(raw) > 0, (
        "wiki-overview-search 컴포넌트(탭 바/현황 패널)가 DOM 에 존재해야 함 "
        "(미존재 시 matched node 0)"
    )
    assert violations == [], "현황 탭 영역 a11y 위반:\n" + _format_violations(violations)


# ============================================================================
# AX5 — 검색 결과 카드 영역 axe 위반 0
# ============================================================================


def test_AX5_검색결과_카드_영역_axe_위반_0(wiki_page: Page) -> None:
    """Given: 검색 탭 + 결과 카드 2 개 렌더
    When:  검색 패널 + 결과 카드 영역만 한정 axe 스캔
    Then:  카드/배지/score/인용 모두 통과 → 위반 0.

    근거: acceptance [a11y] "검색 결과 카드 영역에 대한 axe-core scoped scan 위반 0".
    """
    _switch_to_search_tab(wiki_page)
    wiki_page.wait_for_timeout(200)
    violations, raw = _run_axe_on(
        wiki_page,
        [
            "[data-component='wiki-overview-search']",
            "#wikiSearchPanel",
            ".wiki-result-card",
        ],
    )
    assert _matched_nodes(raw) > 0, (
        "검색 결과 카드(.wiki-result-card)가 DOM 에 존재해야 함 (미존재 시 matched node 0)"
    )
    assert violations == [], "검색 결과 카드 영역 a11y 위반:\n" + _format_violations(violations)


# ============================================================================
# AX6 — 키보드 도달 가능성
# ============================================================================


def test_AX6_키보드로_탭과_결과카드_인용링크에_도달가능하다(wiki_page: Page) -> None:
    """Given: 검색 탭 + 결과 카드 + 인용 링크 렌더
    When:  body 부터 Tab 을 순차 입력
    Then:  검색 탭 버튼·결과 카드·인용 anchor 가 모두 활성 요소가 됨.

    근거: acceptance [a11y] "키보드만으로 현황↔검색 탭 이동·검색 결과 카드·
          인용 링크 도달 가능".
    """
    _switch_to_search_tab(wiki_page)
    wiki_page.evaluate("() => document.body.focus()")

    seen: set[str] = set()
    # 최대 80 회 Tab 으로 3 종 타겟(탭/카드/인용) 통과 확인.
    for _ in range(80):
        wiki_page.keyboard.press("Tab")
        marker = wiki_page.evaluate(
            """
            () => {
              const el = document.activeElement;
              if (!el) return '';
              if (el.closest("[role='tab']")) return 'tab';
              if (el.closest('.wiki-citation')) return 'citation';
              if (el.closest('.wiki-result-card')) return 'card';
              return '';
            }
            """
        )
        if marker:
            seen.add(marker)
        if {"tab", "card", "citation"}.issubset(seen):
            break
    missing = {"tab", "card", "citation"} - seen
    assert not missing, f"Tab 으로 도달하지 못한 인터랙티브 타겟: {missing} (seen={seen})"


# ============================================================================
# AX7 — focus-visible 포커스 링
# ============================================================================


def test_AX7_탭_버튼은_focus_시_가시적_포커스링을_표시한다(wiki_page: Page) -> None:
    """Given: 현황 탭 버튼
    When:  탭에 focus 부여
    Then:  computed outline 또는 box-shadow 로 포커스 링이 가시적으로 표시.

    근거: acceptance [a11y] "모든 인터랙티브 요소에 :focus-visible 포커스 링
          (--focus-ring) 이 가시적으로 표시", 목업 §5 ".wiki-tab:focus-visible".
    """
    tab = wiki_page.locator("#wikiTabOverview[role='tab']")
    tab.focus()
    outline_width = tab.evaluate("el => getComputedStyle(el).outlineWidth")
    outline_style = tab.evaluate("el => getComputedStyle(el).outlineStyle")
    box_shadow = tab.evaluate("el => getComputedStyle(el).boxShadow")

    has_outline = outline_style not in ("none", "") and outline_width not in ("0px", "", None)
    has_shadow = bool(box_shadow) and box_shadow != "none" and "rgb" in box_shadow.lower()
    assert has_outline or has_shadow, (
        "focus 시 outline 또는 box-shadow 포커스 링이 가시적이어야 함 "
        f"(outline={outline_style}/{outline_width}, box-shadow={box_shadow!r})"
    )


def test_AX8_검색결과_카드는_focus_시_가시적_포커스링을_표시한다(wiki_page: Page) -> None:
    """Given: 검색 결과 카드(버튼)
    When:  첫 카드에 focus 부여
    Then:  outline 또는 box-shadow 로 포커스 링 표시.

    근거: acceptance [a11y] "모든 인터랙티브 요소 focus-visible 포커스 링".
    """
    _switch_to_search_tab(wiki_page)
    card = wiki_page.locator(".wiki-result-card").first
    card.focus()
    outline_width = card.evaluate("el => getComputedStyle(el).outlineWidth")
    outline_style = card.evaluate("el => getComputedStyle(el).outlineStyle")
    box_shadow = card.evaluate("el => getComputedStyle(el).boxShadow")

    has_outline = outline_style not in ("none", "") and outline_width not in ("0px", "", None)
    has_shadow = bool(box_shadow) and box_shadow != "none" and "rgb" in box_shadow.lower()
    assert has_outline or has_shadow, (
        "검색 카드 focus 시 outline/box-shadow 포커스 링 표시 필요 "
        f"(outline={outline_style}/{outline_width}, box-shadow={box_shadow!r})"
    )
