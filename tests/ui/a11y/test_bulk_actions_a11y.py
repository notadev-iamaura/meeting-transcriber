"""bulk-actions — axe-core 룰셋 + 컴포넌트별 ARIA 계약 검증.

티켓: bulk-actions / Phase 2A
디자인 산출물:
  - docs/design-decisions/bulk-actions.md           (정책)
  - docs/design-decisions/bulk-actions-handoff.md   (DOM/ARIA 명세)

검증 룰셋: spec §5.3 + harness.a11y.DEFAULT_RULESET = (wcag2a, wcag2aa, wcag21aa).
wcag21aaa 는 spec 범위 밖이므로 비활성.

검증 시나리오 (mockup §6, handoff §1, Phase 4B 옵션 B):
    AA1 — 체크박스는 부모 .meeting-item (role=option) 의 aria-checked 로 SR 안내.
          시각 마크는 <span aria-hidden="true">. (axe nested-interactive 회피용
          ARIA-only 패턴 — WAI-ARIA APG multi-select listbox 권장)
    AA2 — 컨텍스트 액션 바 role="toolbar" + aria-label
    AA3 — 카운트 라벨 aria-live="polite"
    AA4 — 드롭다운 메뉴 role="menu" + 항목 role="menuitemradio"
    AA5a-rev — 사이드바 1 개 선택 시점에 bulk-actions 컴포넌트 영역 axe 위반 0
    AA5b — 2 개 선택 + 액션 바 노출 시점에 bulk-actions 컴포넌트 영역 axe 위반 0
    AA5c — [전체 일괄] 드롭다운 열린 시점에 home-action-dropdown 영역 axe 위반 0
    AA6 — 모든 인터랙티브 요소 키보드 도달 가능 (Tab order)
    AA7 — focus-visible 표시 (포커스 ring box-shadow)
    AA8 — prefers-reduced-motion 활성 시 액션 바 translate 애니메이션 제거 (transform: none)

review-2b §2 거짓 Red 해소:
    이전 AA5a (홈 초기 axe 위반 0) 는 페이지 전체를 스캔해 기존 SPA 의 16 건
    color-contrast 위반을 상속, bulk-actions 구현이 완료되어도 영구 FAIL.
    수정: AA5a 삭제 + AA5a-rev 도입 (사이드바 selection 진입 후의 bulk-actions
    DOM 영역만 한정 스캔). AA5b/c 도 동일 패턴 — `context={"include": [...]}`
    로 bulk-actions 컴포넌트 (액션 바 / 사이드바 listbox / 드롭다운 wrapper) 만
    스캔. frontend-a 가 `data-component="bulk-actions"` 마커를 부여하지 않은
    경우를 대비해 fallback selector 도 함께 시도.

Red 의도성:
    현재 SPA 에 `.bulk-action-bar`, `.home-action-dropdown`, `.meeting-item-checkbox`
    가 존재하지 않으므로 본 시나리오는 모두 selector 부재 / 속성 부재로 실패한다.
    AA5a-rev/b/c 는 bulk-actions DOM 자체가 미존재해 axe scan 컨텍스트에서
    "no nodes matched" 또는 사전 검증 단계 실패로 깨끗한 Red 가 발생.
    individual ARIA 계약 시나리오들 (AA1~AA4, AA6~AA8) 도 명확히 FAIL 한다.
"""

from __future__ import annotations

import json

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser, Page

from harness.a11y import DEFAULT_RULESET

pytestmark = [pytest.mark.ui]


@pytest.fixture
def ui_page(browser: Browser, ui_bulk_base_url: str) -> Page:
    """ui_bulk_server 가 띄운 SPA 의 홈(`/app`) 으로 이동한 Page (axe 주입 대상)."""
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        color_scheme="light",
    )
    page = context.new_page()
    page.goto(f"{ui_bulk_base_url}/app", wait_until="networkidle")
    page.wait_for_selector(".meeting-item", timeout=10000)
    yield page
    context.close()


def _install_batch_mock(page: Page) -> None:
    """A8 시나리오를 위한 batch API 정상 응답 mock — 모든 a11y 시나리오 공통."""
    page.route(
        "**/api/meetings/batch",
        lambda route, _req: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"queued": 2, "skipped": 0, "message": "2개 처리"}),
        ),
    )


def _select_two(page: Page) -> None:
    """다중 선택 상태로 진입 (액션 바 + selection mode 활성화)."""
    items = page.locator(".meeting-item")
    items.nth(0).locator(".meeting-item-checkbox").click()
    items.nth(1).locator(".meeting-item-checkbox").click()
    page.wait_for_timeout(300)


# ============================================================================
# AA1 — 체크박스 ARIA 계약
# ============================================================================


def test_AA1_체크박스는_부모_option_의_aria_checked_로_SR_안내된다(ui_page: Page) -> None:
    """Given: 사이드바 회의 항목들
    When:  `.meeting-item-checkbox` 의 ARIA 계약 + 부모 .meeting-item 의 aria-checked 조회
    Then:  옵션 B (ARIA-only) — 시각 체크박스는 aria-hidden="true" <span> 이고,
           부모 .meeting-item (role="option") 의 aria-checked 가 SR 안내 단일 진실.

    근거: WAI-ARIA 1.2 option.Children Presentational: True,
          APG multi-select listbox 권장 ("aria-checked for multi-select widgets").
          axe nested-interactive (wcag2a) 룰을 자연스럽게 통과한다.
    """
    items = ui_page.locator(".meeting-item")
    first_item = items.nth(0)
    cb = first_item.locator(".meeting-item-checkbox")

    # 시각 체크박스는 <span> + aria-hidden 이어야 함 (focusable 아님)
    tag = cb.evaluate("el => el.tagName.toLowerCase()")
    assert tag == "span", (
        f".meeting-item-checkbox 는 <span> 이어야 함 — 옵션 B (ARIA-only) (tag={tag!r})"
    )
    aria_hidden = cb.get_attribute("aria-hidden")
    assert aria_hidden == "true", (
        f"시각 체크박스는 aria-hidden='true' 여야 함 — SR 은 부모 aria-checked 만 듣는다 (got {aria_hidden!r})"
    )

    # 부모 .meeting-item 은 role=option + 초기 aria-checked='false'
    role = first_item.get_attribute("role")
    assert role == "option", f".meeting-item role='option' 필수 (got {role!r})"
    initial_checked = first_item.get_attribute("aria-checked")
    assert initial_checked == "false", (
        f"초기 상태 부모 aria-checked='false' (got {initial_checked!r})"
    )

    # 클릭 → 부모 aria-checked 가 'true' 로 동기화되는지
    cb.click()
    ui_page.wait_for_timeout(150)
    after_checked = first_item.get_attribute("aria-checked")
    assert after_checked == "true", (
        f"클릭 후 부모 .meeting-item aria-checked='true' 필수 (got {after_checked!r})"
    )


# ============================================================================
# AA2 — 컨텍스트 액션 바 toolbar role
# ============================================================================


def test_AA2_액션_바는_role_toolbar와_aria_label을_보유한다(ui_page: Page) -> None:
    """Given: 1+ 선택됨 → 액션 바 표시
    When:  `.bulk-action-bar` 의 role / aria-label 조회
    Then:  role="toolbar" + 비어있지 않은 aria-label.

    근거: handoff §1.2 "<div class='bulk-action-bar' role='toolbar'
          aria-label='선택된 회의 일괄 작업'>".
    """
    items = ui_page.locator(".meeting-item")
    items.nth(0).locator(".meeting-item-checkbox").click()
    ui_page.wait_for_timeout(300)
    bar = ui_page.locator(".bulk-action-bar")
    role = bar.get_attribute("role")
    aria_label = bar.get_attribute("aria-label")
    assert role == "toolbar", f"액션 바 role='toolbar' 필수 (got {role!r})"
    assert aria_label and aria_label.strip(), (
        f"액션 바 aria-label 비어있지 않아야 함 (got {aria_label!r})"
    )


# ============================================================================
# AA3 — 카운트 라벨 aria-live
# ============================================================================


def test_AA3_카운트_라벨은_aria_live_polite_이다(ui_page: Page) -> None:
    """Given: 1+ 선택됨 → 액션 바 표시
    When:  `.bulk-action-bar__count` 의 aria-live 조회
    Then:  aria-live="polite" — 카운트 변화 시 SR 자동 안내.

    근거: handoff §1.2 "<div class='bulk-action-bar__count' aria-live='polite'>".
    """
    items = ui_page.locator(".meeting-item")
    items.nth(0).locator(".meeting-item-checkbox").click()
    ui_page.wait_for_timeout(250)
    count_el = ui_page.locator(".bulk-action-bar__count")
    aria_live = count_el.get_attribute("aria-live")
    assert aria_live == "polite", (
        f"카운트 라벨 aria-live='polite' 필수 — SR 안내 보장 (got {aria_live!r})"
    )


# ============================================================================
# AA4 — 드롭다운 메뉴 role
# ============================================================================


def test_AA4_드롭다운은_role_menu와_menuitemradio_를_사용한다(ui_page: Page) -> None:
    """Given: HomeView 로드
    When:  [전체 일괄 ▾] 클릭 → 메뉴 열기
    Then:  컨테이너 role="menu" + 항목 role="menuitemradio" + aria-checked 동기화.

    근거: handoff §1.3, bulk-actions.md §3.6 "트리거 aria-haspopup='menu',
          메뉴 role='menu', 항목 role='menuitemradio'".
    """
    trigger = ui_page.locator(".home-action-btn--dropdown[data-dropdown='all-bulk']")
    # 트리거 자체 ARIA
    assert trigger.get_attribute("aria-haspopup") == "menu", "트리거에 aria-haspopup='menu' 필수"
    trigger.click()
    ui_page.wait_for_timeout(200)
    menu = ui_page.locator(".home-action-dropdown[role='menu']")
    assert menu.count() >= 1, "메뉴 컨테이너 role='menu' 필수"
    items = menu.first.locator("[role='menuitemradio']")
    assert items.count() == 3, f"메뉴 항목 3 개 (menuitemradio) 필수 (got {items.count()})"
    # 정확히 1 개가 aria-checked="true"
    checked = menu.first.locator("[role='menuitemradio'][aria-checked='true']")
    assert checked.count() == 1, (
        f"기본 옵션 1 개에 aria-checked='true' 필수 (got {checked.count()})"
    )


# ============================================================================
# AA5 — axe-core 위반 0 (bulk-actions 컴포넌트 한정 스캔)
# ============================================================================


def _run_axe_on(page: Page, include_selectors: list[str]) -> tuple[list[dict], dict]:
    """axe-core 를 컴포넌트 한정 (`context.include`) 으로 실행.

    review-2b §2 결정에 따라 페이지 전체가 아닌 bulk-actions 컴포넌트 영역만
    스캔. 기존 SPA 의 color-contrast 위반 (검색 카드, 사이드바 텍스트 등) 을
    상속하지 않도록 보장.

    Args:
        page: playwright Page
        include_selectors: include scope 의 CSS selector 리스트
            (예: ["[data-component='bulk-actions']", ".bulk-action-bar"])

    Returns:
        (violations, raw_response) — violations 리스트 + axe 의 raw 응답.
        raw_response 는 디버깅용 (matched node 수 추적).
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


def _format_violations(violations: list[dict]) -> str:
    return "\n".join(
        f"  - {v['id']} ({v.get('impact')}): {v.get('help')} — nodes: {len(v.get('nodes', []))}"
        for v in violations
    )


def test_AA5a_rev_사이드바_1개_선택_시점_bulk_actions_axe_위반_0(
    ui_page: Page,
) -> None:
    """Given: 사이드바 1 개 선택 → selection mode ON + 액션 바 슬라이드 다운 완료
    When:  axe-core 를 사이드바 listbox + 액션 바 영역만 한정 스캔
    Then:  bulk-actions 컴포넌트 영역의 wcag2a + wcag2aa + wcag21aa 위반 0.

    근거: review-2b §2 — 이전 AA5a (홈 초기 페이지 전체 스캔) 는 기존 SPA 의
          color-contrast 16 건 위반을 상속해 영구 FAIL. 본 시나리오는
          bulk-actions 컴포넌트 진입 시점 + 컴포넌트 한정 스캔으로 거짓 Red 해소.
    """
    items = ui_page.locator(".meeting-item")
    items.nth(0).locator(".meeting-item-checkbox").click()
    ui_page.wait_for_timeout(300)
    # 사이드바 listbox + 액션 바 — bulk-actions 컴포넌트 핵심 표면.
    # frontend-a 가 `data-component='bulk-actions'` 마커를 부여하면 더 명확.
    violations, raw = _run_axe_on(
        ui_page,
        [
            "[data-component='bulk-actions']",
            "[role='listbox'][aria-multiselectable='true']",
            ".bulk-action-bar",
        ],
    )
    # raw 응답에 matched node 가 0 이면 컴포넌트 미존재 (Red 의 또 다른 형태)
    matched_nodes = sum(
        len(check.get("nodes", []))
        for kind in ("passes", "violations", "incomplete", "inapplicable")
        for check in raw.get(kind, [])
    )
    assert matched_nodes > 0, (
        "bulk-actions 컴포넌트가 DOM 에 존재해야 함 (`.bulk-action-bar` 또는 "
        "`[data-component='bulk-actions']` 미존재)"
    )
    assert violations == [], (
        "사이드바 1 개 선택 + 액션 바 노출 시점 bulk-actions a11y 위반:\n"
        + _format_violations(violations)
    )


def test_AA5b_2개_선택_상태_bulk_actions_axe_위반_0(ui_page: Page) -> None:
    """Given: 2 개 선택됨 → 액션 바 표시
    When:  bulk-actions 컴포넌트 한정 axe 실행
    Then:  toolbar / checkbox / aria-live 모두 통과 → violations 0.
    """
    _select_two(ui_page)
    violations, raw = _run_axe_on(
        ui_page,
        [
            "[data-component='bulk-actions']",
            "[role='listbox'][aria-multiselectable='true']",
            ".bulk-action-bar",
        ],
    )
    matched_nodes = sum(
        len(check.get("nodes", []))
        for kind in ("passes", "violations", "incomplete", "inapplicable")
        for check in raw.get(kind, [])
    )
    assert matched_nodes > 0, (
        "bulk-actions 컴포넌트가 DOM 에 존재해야 함 — 2 개 선택 후 `.bulk-action-bar` 미존재"
    )
    assert violations == [], "2 개 선택 상태 bulk-actions a11y 위반:\n" + _format_violations(
        violations
    )


def test_AA5c_드롭다운_열린_상태_axe_위반_0(ui_page: Page) -> None:
    """Given: [전체 일괄 ▾] 메뉴 열림
    When:  home-action-dropdown 영역 한정 axe 실행
    Then:  menu / menuitemradio / aria-haspopup 모두 통과 → violations 0.
    """
    ui_page.locator(".home-action-btn--dropdown[data-dropdown='all-bulk']").click()
    ui_page.wait_for_timeout(200)
    violations, raw = _run_axe_on(
        ui_page,
        [
            "[data-component='bulk-actions']",
            ".home-action-dropdown",
            ".home-action-btn--dropdown",
        ],
    )
    matched_nodes = sum(
        len(check.get("nodes", []))
        for kind in ("passes", "violations", "incomplete", "inapplicable")
        for check in raw.get(kind, [])
    )
    assert matched_nodes > 0, (
        "home-action-dropdown 컴포넌트가 DOM 에 존재해야 함 (메뉴 열림 후 미발견)"
    )
    assert violations == [], "메뉴 열린 상태 bulk-actions a11y 위반:\n" + _format_violations(
        violations
    )


# ============================================================================
# AA6 — 키보드 도달 가능성 (Tab order)
# ============================================================================


def test_AA6_액션_바_버튼들이_Tab으로_도달_가능하다(ui_page: Page) -> None:
    """Given: 2 개 선택됨 → 액션 바 표시
    When:  body 부터 Tab 을 순차 입력
    Then:  `.bulk-action-btn` 3 개 + `.bulk-action-bar__dismiss` 가 모두 활성 요소가 됨.

    근거: bulk-actions.md §2.6 "키보드 — Tab 으로 진입, ←→ 또는 Tab 으로 버튼 이동".
    """
    _select_two(ui_page)
    # body 에 포커스 — 사이드바 항목들이 selected 가 되어 있으므로 명시 reset
    ui_page.evaluate("() => document.body.focus()")
    seen: set[str] = set()
    targets = {
        "bulk-action-btn--transcribe",
        "bulk-action-btn--summarize",
        "bulk-action-btn--both",
        "bulk-action-bar__dismiss",
    }
    # 최대 60 회 Tab 으로 모든 타겟 통과 확인 (헤더/사이드바/액션 바 등 포함 가능)
    for _ in range(60):
        ui_page.keyboard.press("Tab")
        marker = ui_page.evaluate(
            """
            () => {
              const el = document.activeElement;
              if (!el) return '';
              if (el.matches('.bulk-action-bar__dismiss, .bulk-action-bar__dismiss *'))
                return 'bulk-action-bar__dismiss';
              const btn = el.closest('.bulk-action-btn');
              if (btn) {
                const da = btn.getAttribute('data-action') || '';
                return 'bulk-action-btn--' + da;
              }
              return '';
            }
            """
        )
        if marker:
            seen.add(marker)
        if targets.issubset(seen):
            break
    missing = targets - seen
    assert not missing, f"Tab 으로 도달하지 못한 타겟: {missing} (seen={seen})"


# ============================================================================
# AA7 — focus-visible ring
# ============================================================================


def test_AA7_액션_버튼은_focus_시_ring_표시(ui_page: Page) -> None:
    """Given: 2 개 선택됨 → 액션 바 표시
    When:  `.bulk-action-btn[data-action='transcribe']` 에 focus
    Then:  computed boxShadow 가 비어있지 않음 (focus-ring 적용).

    근거: handoff §2.3 "`.bulk-action-btn:focus-visible { box-shadow: var(--focus-ring) }`".
    """
    _select_two(ui_page)
    btn = ui_page.locator(".bulk-action-btn[data-action='transcribe']")
    btn.focus()
    box_shadow = btn.evaluate("el => getComputedStyle(el).boxShadow")
    assert box_shadow and box_shadow != "none" and "rgb" in box_shadow.lower(), (
        f"focus 시 box-shadow ring 적용 필요 (got {box_shadow!r})"
    )


# ============================================================================
# AA8 — prefers-reduced-motion
# ============================================================================


def test_AA8_reduced_motion_활성_시_translate_애니메이션_제거(
    browser: Browser, ui_bulk_base_url: str
) -> None:
    """Given: prefers-reduced-motion: reduce 환경 + 1 개 선택됨
    When:  `.bulk-action-bar` 의 computed transform 조회
    Then:  transform == 'none' (translateY 모션 제거).

    근거: handoff §2.3 "@media (prefers-reduced-motion: reduce)
          .bulk-action-bar { transform: none !important }".
    """
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        color_scheme="light",
        reduced_motion="reduce",
    )
    page = context.new_page()
    try:
        page.goto(f"{ui_bulk_base_url}/app", wait_until="networkidle")
        page.wait_for_selector(".meeting-item", timeout=10000)
        page.locator(".meeting-item").nth(0).locator(".meeting-item-checkbox").click()
        page.wait_for_timeout(300)
        bar = page.locator(".bulk-action-bar")
        transform = bar.evaluate("el => getComputedStyle(el).transform")
        assert transform == "none", (
            f"reduced-motion 에서 transform: none 강제 필요 (got {transform!r})"
        )
    finally:
        context.close()
