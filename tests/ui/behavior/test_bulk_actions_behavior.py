"""bulk-actions — 사이드바 다중 선택 + 컨텍스트 액션 바 + 홈 드롭다운 행동 검증.

티켓: bulk-actions / Phase 2A
디자인 산출물:
  - docs/design-decisions/bulk-actions.md           (정책/상태 전이/단축키)
  - docs/design-decisions/bulk-actions-mockup.md    (시각 명세)
  - docs/design-decisions/bulk-actions-handoff.md   (DOM/CSS/JS 명세)

검증 범위 (3 컴포넌트, 각 그룹은 단일 책임):
    1. Sidebar 다중 선택 (B*) — 체크박스 hover-reveal, 체크/Cmd/Shift, Esc, Cmd+A
    2. Bulk Action Bar (A*)   — slide-down, 카운트, 액션 → POST /api/meetings/batch
    3. Home Dropdown (H*)     — [전체 일괄 ▾] / [최근 24시간 ▾]

Red 의도성:
    현재 `ui/web/spa.js` + `index.html` + `style.css` 에는
    `.meeting-item-checkbox`, `.bulk-action-bar`, `.home-action-dropdown`
    같은 클래스/마크업 자체가 존재하지 않는다. 따라서 본 모듈의 모든
    시나리오는 **selector 부재 + 동작 미구현** 으로 깨끗한 assertion 실패가
    발생한다 (import 오류·fixture 누락 ERROR 가 아니라 FAIL).

기존 시나리오와의 차이:
    기존 ui dir 테스트는 file:// fixture HTML 패턴 (T-201/202/301/302) 이지만
    bulk-actions 는 실제 SPA 의 다중 모듈 (Sidebar / HomeView / 단축키 핸들러)
    상호작용을 검증해야 하므로 fixture-as-source-of-truth 패턴이 부적합.
    `ui_bulk_server` fixture (conftest.py) 가 실제 FastAPI 서버 subprocess +
    회의 5 건 시드 + 포트 8767 격리를 제공한다.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.ui.conftest import empty_meetings_dom, meetings_listbox

pytestmark = [pytest.mark.ui]


# ============================================================================
# 라우트 mock 헬퍼
# ============================================================================

# `/api/meetings/batch` 응답에 대한 단일 진실 공급원 — 모든 시나리오가 동일 응답을 기대.
_BATCH_OK_RESPONSE = {"queued": 3, "skipped": 1, "message": "3개 처리, 1개 건너뜀"}


def _install_batch_route_mock(page: Page) -> dict[str, list[dict]]:
    """`POST /api/meetings/batch` 호출을 가로채 captured 리스트에 기록한다.

    Returns:
        captured["calls"] — 각 호출의 {action, scope, meeting_ids?, hours?} 페이로드.
    """
    captured: dict[str, list[dict]] = {"calls": []}

    def _handle(route, request):
        try:
            body = request.post_data_json or {}
        except Exception:
            body = {}
        captured["calls"].append({"method": request.method, "url": request.url, "body": body})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_BATCH_OK_RESPONSE),
        )

    page.route("**/api/meetings/batch", _handle)
    return captured


# ============================================================================
# 페이지 헬퍼
# ============================================================================


@pytest.fixture
def ui_page(browser: Browser, ui_bulk_base_url: str) -> Page:
    """ui_bulk_server 가 띄운 SPA 의 홈 화면(`/app`) 을 로드한 Page.

    각 테스트마다 새 context 로 격리된 storage / route handler 를 받는다.
    """
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        color_scheme="light",
    )
    page = context.new_page()
    # 뷰포트는 데스크톱 — 모바일 적응은 별도 시나리오에서 viewport 변경.
    page.goto(f"{ui_bulk_base_url}/app", wait_until="networkidle")
    # 사이드바에 회의 5 건이 모두 렌더될 때까지 대기.
    page.wait_for_selector(".meeting-item", timeout=10000)
    yield page
    context.close()


def _meeting_items(page: Page):
    """사이드바의 회의 항목 locator (시드된 5 건)."""
    return page.locator(".meeting-item")


def _bulk_bar(page: Page):
    """컨텍스트 액션 바 locator (mockup §3 — `.bulk-action-bar`)."""
    return page.locator(".bulk-action-bar")


# ============================================================================
# 그룹 1 — 사이드바 다중 선택 (B*)
# ============================================================================


class TestSidebarMultiSelect:
    """사이드바 체크박스 hover-reveal + 다중 선택 + 키보드 단축키."""

    def test_B1_체크박스가_hover_시_나타난다(self, ui_page: Page) -> None:
        """Given: selection mode OFF, 회의 항목 비-hover 상태
        When:  첫 항목 위에 마우스 hover
        Then:  해당 항목의 `.meeting-item-checkbox` opacity 가 1 (페이드 인 완료).

        근거: bulk-actions.md §1.1 "default opacity 0 → hover opacity 1".
        """
        first_item = _meeting_items(ui_page).first
        first_item.hover()
        # transition 250ms 완료 대기.
        ui_page.wait_for_timeout(350)
        cb = first_item.locator(".meeting-item-checkbox")
        opacity = cb.evaluate("el => getComputedStyle(el).opacity")
        assert float(opacity) >= 0.99, (
            f"hover 시 체크박스 opacity 가 1 이어야 함 (got {opacity!r})"
        )

    def test_B2_체크박스_클릭은_뷰어로_이동하지_않는다(self, ui_page: Page) -> None:
        """Given: 회의 5 건 사이드바, URL `/app`
        When:  첫 항목의 체크박스만 클릭 (본문 영역 X)
        Then:  URL 변화 없음 + 항목 `.selected` 클래스 적용.

        근거: bulk-actions.md §1.2 "체크박스 클릭 → 토글 (뷰어 이동 X) —
              event.stopPropagation() 으로 부모 클릭 차단".
        """
        original_url = ui_page.url
        first_item = _meeting_items(ui_page).first
        cb = first_item.locator(".meeting-item-checkbox")
        cb.click()
        # 라우팅이 일어나면 URL 변경 — 발생하면 안 됨.
        ui_page.wait_for_timeout(200)
        assert ui_page.url == original_url, (
            f"체크박스 클릭은 뷰어로 이동하면 안 됨 (was {original_url}, now {ui_page.url})"
        )
        expect(first_item).to_have_class(__import__("re").compile(r".*\bselected\b.*"))

    def test_B3_본문_클릭은_뷰어로_이동한다(
        self, ui_page: Page, ui_bulk_meeting_ids: list[str]
    ) -> None:
        """Given: 회의 5 건 사이드바
        When:  첫 항목의 본문(.meeting-item-text) 영역을 일반 클릭 (modifier 없음)
        Then:  URL 이 `/app/viewer/{id}` 로 이동.

        근거: bulk-actions.md §1.2 "항목 본문 클릭 → 기존대로 뷰어 이동".
        """
        first_item = _meeting_items(ui_page).first
        text_area = first_item.locator(".meeting-item-text")
        text_area.click()
        # 첫 항목은 시드 ID 중 created_at 이 가장 큰 것 (최신순 정렬 가정).
        ui_page.wait_for_url(lambda url: "/app/viewer/" in url, timeout=5000)
        assert "/app/viewer/" in ui_page.url, f"뷰어 라우팅 실패: {ui_page.url}"

    def test_B4_Cmd_클릭은_항목을_토글한다(self, ui_page: Page) -> None:
        """Given: 사이드바, 비선택 상태
        When:  Cmd(Meta) 누른 채 첫 항목 본문 클릭 두 번
        Then:  첫 클릭에 selected, 두 번째 클릭에 해제 (URL 변경 없음).

        근거: bulk-actions.md §1.2 "Cmd/Ctrl + 클릭 (본문) → 토글".
        """
        original_url = ui_page.url
        first_item = _meeting_items(ui_page).first
        text_area = first_item.locator(".meeting-item-text")

        text_area.click(modifiers=["Meta"])
        ui_page.wait_for_timeout(150)
        expect(first_item).to_have_class(__import__("re").compile(r".*\bselected\b.*"))

        text_area.click(modifiers=["Meta"])
        ui_page.wait_for_timeout(150)
        # 두 번째 토글 후 selected 클래스 제거.
        cls = first_item.get_attribute("class") or ""
        assert "selected" not in cls.split(), (
            f"Cmd 클릭 두 번이면 해제되어야 함 (class={cls!r})"
        )
        assert ui_page.url == original_url, "Cmd+클릭은 URL 변경 금지"

    def test_B5_Shift_클릭은_범위를_선택한다(self, ui_page: Page) -> None:
        """Given: 1번째 항목이 이미 선택됨 (앵커)
        When:  4번째 항목을 Shift + 클릭
        Then:  1~4 번째가 모두 selected (범위 선택).

        근거: bulk-actions.md §1.2 "Shift + 클릭 → 마지막 클릭 ↔ 현재 항목 사이 범위".
        """
        items = _meeting_items(ui_page)
        # 앵커 — 1번째 본문 Cmd+클릭 (단일 선택, 토글로 진입)
        items.nth(0).locator(".meeting-item-text").click(modifiers=["Meta"])
        ui_page.wait_for_timeout(100)
        # 4번째를 Shift+클릭
        items.nth(3).locator(".meeting-item-text").click(modifiers=["Shift"])
        ui_page.wait_for_timeout(150)
        # 1~4 가 모두 selected
        for i in range(4):
            cls = items.nth(i).get_attribute("class") or ""
            assert "selected" in cls.split(), (
                f"index={i} 가 selected 이어야 하는데 class={cls!r}"
            )
        # 5번째는 selected 가 아니어야 함
        cls5 = items.nth(4).get_attribute("class") or ""
        assert "selected" not in cls5.split(), f"index=4 (5번째) 는 범위 밖이어야 함 (class={cls5!r})"

    def test_B6_selection_mode_진입_시_모든_체크박스_상시_표시(
        self, ui_page: Page
    ) -> None:
        """Given: 1 개 선택됨 → selection mode ON
        When:  비-hover 상태의 다른 항목의 체크박스 opacity 조회
        Then:  모든 항목 체크박스 opacity == 1.

        근거: bulk-actions.md §1.1 "selection mode ON (1+ 선택됨) → 모든 항목 opacity 1".
        """
        items = _meeting_items(ui_page)
        # 1번째 체크박스 클릭 → selection mode 진입
        items.nth(0).locator(".meeting-item-checkbox").click()
        ui_page.wait_for_timeout(150)
        # 사이드바 listbox 컨테이너 단일 진실 헬퍼 사용 (review-2b §3 결정).
        # ARIA 속성 `[role='listbox'][aria-multiselectable='true']` 만 보장하면
        # 컨테이너 클래스 (list-content / meetings-list) 는 frontend-a 가 결정.
        list_panel = meetings_listbox(ui_page)
        assert list_panel.count() == 1, (
            f"사이드바 listbox 컨테이너는 정확히 1 개 (got {list_panel.count()}) — "
            f"`#listContent` 에 aria-multiselectable='true' 추가 필요"
        )
        cls = list_panel.first.get_attribute("class") or ""
        assert "meetings-list--selecting" in cls, (
            f"selection mode 활성 시 부모에 .meetings-list--selecting 부여 필요 (class={cls!r})"
        )
        # 비-hover 상태의 4번째 체크박스도 opacity 1
        ui_page.mouse.move(0, 0)  # hover 해제
        ui_page.wait_for_timeout(100)
        cb4 = items.nth(3).locator(".meeting-item-checkbox")
        opacity = cb4.evaluate("el => getComputedStyle(el).opacity")
        assert float(opacity) >= 0.99, (
            f"selection mode ON 에서 비-hover 항목 체크박스도 opacity 1 (got {opacity!r})"
        )

    def test_B7_마지막_체크해제_시_selection_mode_자동_종료(self, ui_page: Page) -> None:
        """Given: 1 개 선택됨 (selection mode ON)
        When:  유일하게 선택된 체크박스를 다시 클릭 (해제, count → 0)
        Then:  부모에서 .meetings-list--selecting 제거, 액션 바 hidden.

        근거: bulk-actions.md §1.5 "마지막 항목 해제 → 자동 OFF (액션 바 슬라이드 업)".
        """
        items = _meeting_items(ui_page)
        cb = items.nth(0).locator(".meeting-item-checkbox")
        cb.click()
        ui_page.wait_for_timeout(150)
        cb.click()  # 해제
        ui_page.wait_for_timeout(350)  # slide-up transition 완료
        # 단일 진실 헬퍼 사용 — selection mode OFF 시 클래스 제거 검증
        list_panel = meetings_listbox(ui_page).first
        cls = list_panel.get_attribute("class") or ""
        assert "meetings-list--selecting" not in cls, (
            f"마지막 해제 후 .meetings-list--selecting 제거 필요 (class={cls!r})"
        )
        # 액션 바는 hidden 또는 absent
        bar = _bulk_bar(ui_page)
        bar_count = bar.count()
        if bar_count > 0:
            assert bar.first.is_hidden(), "selection mode 종료 후 액션 바 hidden 필요"

    def test_B8_Esc는_전체_해제하고_selection_mode_종료(self, ui_page: Page) -> None:
        """Given: 3 개 선택됨
        When:  Esc 키 입력
        Then:  모든 선택 해제 + 액션 바 hidden.

        근거: bulk-actions.md §1.2.1 "Esc → 모든 선택 해제 + selection mode 종료".
        """
        items = _meeting_items(ui_page)
        for i in range(3):
            items.nth(i).locator(".meeting-item-checkbox").click()
            ui_page.wait_for_timeout(80)
        ui_page.keyboard.press("Escape")
        ui_page.wait_for_timeout(300)
        # 어떤 항목도 selected 아님
        for i in range(5):
            cls = items.nth(i).get_attribute("class") or ""
            assert "selected" not in cls.split(), (
                f"Esc 후 index={i} 는 비선택이어야 함 (class={cls!r})"
            )
        bar = _bulk_bar(ui_page)
        if bar.count() > 0:
            assert bar.first.is_hidden(), "Esc 후 액션 바 hidden 필요"

    def test_B9_사이드바_포커스_상태에서_Cmd_A는_전체_선택(self, ui_page: Page) -> None:
        """Given: 사이드바에 포커스가 있는 상태
        When:  Cmd+A (또는 Ctrl+A) 입력
        Then:  렌더된 5 건 모두 selected.

        근거: bulk-actions.md §1.2.1 "Cmd+A — 사이드바에 포커스가 있을 때만 가로채기".
        """
        # 사이드바 첫 항목 focus → 사이드바 컨테이너에 포커스가 있는 상태로 만듦
        items = _meeting_items(ui_page)
        items.nth(0).focus()
        ui_page.keyboard.press("Meta+a")
        ui_page.wait_for_timeout(200)
        for i in range(5):
            cls = items.nth(i).get_attribute("class") or ""
            assert "selected" in cls.split(), (
                f"Cmd+A 후 index={i} 가 selected 여야 함 (class={cls!r})"
            )

    def test_B10_빈_사이드바에서_Cmd_A는_no_op_이다(self, ui_page: Page) -> None:
        """Given: 사이드바 회의 0 건 (DOM 강제로 비움)
        When:  사이드바 컨테이너에 포커스 + Cmd+A
        Then:  selection 0 개 + 액션 바 미노출 + selection mode 미진입.

        근거: spec §3 "회의 0 개일 때 Cmd+A 는 no-op (선택할 항목이 없으므로
              아무 일도 일어나지 않아야 함)" 엣지 (review-2b §3.2 필수 추가).

        Red 의도성:
            현재 SPA 의 Cmd+A 핸들러가 미구현이라 본 시나리오는 "체크박스 부재
            덕에" 통과해버릴 위험. 이를 막기 위해 사전 가시성 검증으로
            `meetings_listbox()` (aria-multiselectable='true' 컨테이너) 가 정확히
            1 개 매칭되어야 함을 강제. 미구현 상태에서는 listbox 가
            aria-multiselectable 속성을 갖지 않아 깨끗한 FAIL.
        """
        # 사전 가시성 — listbox 컨테이너 단일 진실 selector 가 매칭되어야 함.
        # 미구현 시 aria-multiselectable 부재로 0 매칭 → 명확한 Red.
        list_panel = meetings_listbox(ui_page)
        assert list_panel.count() == 1, (
            f"`#listContent` 에 aria-multiselectable='true' 추가 필요 (got count={list_panel.count()})"
        )

        # 회의 5 건 시드 페이지의 DOM 을 강제로 비움 (0 건 시뮬레이션).
        empty_meetings_dom(ui_page)
        # 비움 후 사이드바에 .meeting-item 이 없어야 함
        n_items = ui_page.evaluate(
            "() => document.querySelectorAll('.meeting-item').length"
        )
        assert n_items == 0, f"DOM 비움 후 회의 0 건 보장 필요 (got {n_items})"

        # 사이드바 컨테이너에 포커스 부여 — Cmd+A 가로채기 조건 충족
        ui_page.evaluate("() => document.getElementById('listContent').focus()")
        ui_page.keyboard.press("Meta+a")
        ui_page.wait_for_timeout(200)

        # selection 0 개 검증 — .meeting-item.selected 가 0 개
        n_selected = ui_page.evaluate(
            "() => document.querySelectorAll('.meeting-item.selected').length"
        )
        assert n_selected == 0, f"빈 사이드바에서 Cmd+A 는 no-op (got {n_selected} selected)"

        # 액션 바 미노출 검증
        bar = _bulk_bar(ui_page)
        if bar.count() > 0:
            assert bar.first.is_hidden(), "빈 사이드바에서 액션 바는 hidden 이어야 함"

        # selection mode 클래스 미부여 (listbox 헬퍼는 위에서 1 개 검증 완료)
        cls = list_panel.first.get_attribute("class") or ""
        assert "meetings-list--selecting" not in cls, (
            f"빈 사이드바에서 selection mode 진입 금지 (class={cls!r})"
        )

    def test_B11_0개_회의에서_selection_mode_진입_불가(self, ui_page: Page) -> None:
        """Given: 사이드바 회의 0 건
        When:  사이드바 영역 어디든 클릭 (모든 .meeting-item 은 이미 제거됨)
        Then:  체크박스 자체가 DOM 에 없으므로 selection mode 진입 자체가 불가능.
               부수 검증으로 부모 컨테이너에 selection mode 클래스 미부여 확인.

        근거: spec §3 "회의 0 개" 엣지 — 체크박스 부재 시 selection mode 진입 경로
              자체가 없음을 명시적으로 보호 (review-2b §3.2 필수 추가).

        Red 의도성:
            B10 과 동일하게, 미구현 상태에서도 "체크박스 부재" 가정 자체가 일치
            해 거짓 PASS 가 발생할 수 있음. 사전 가시성 검증으로
            `meetings_listbox()` 가 정확히 1 개 + 5 건 시드된 페이지에서
            체크박스가 5 개 렌더되어야 함을 먼저 검증 (아직 비우기 전).
            미구현 시 사전 검증이 깨끗한 FAIL.
        """
        # 사전 가시성 1 — listbox 컨테이너 단일 진실 selector
        list_panel = meetings_listbox(ui_page)
        assert list_panel.count() == 1, (
            f"`#listContent` 에 aria-multiselectable='true' 추가 필요 (count={list_panel.count()})"
        )

        # 사전 가시성 2 — 시드된 5 건의 체크박스가 정확히 5 개여야 함
        # (이게 있어야 비우는 것이 의미가 있고, 없으면 시나리오 가정 불성립)
        n_checkboxes_before = ui_page.evaluate(
            "() => document.querySelectorAll('.meeting-item-checkbox').length"
        )
        assert n_checkboxes_before == 5, (
            f"시드 페이지에 체크박스 5 개 필요 — 미구현 시 0 (got {n_checkboxes_before})"
        )

        # 이제 비움 후 검증
        empty_meetings_dom(ui_page)
        n_checkboxes = ui_page.evaluate(
            "() => document.querySelectorAll('.meeting-item-checkbox').length"
        )
        assert n_checkboxes == 0, (
            f"0 건 사이드바에 체크박스가 있으면 안 됨 (got {n_checkboxes})"
        )

        # 액션 바 미노출 / selection 0 / selection mode 클래스 부재
        bar = _bulk_bar(ui_page)
        if bar.count() > 0:
            assert bar.first.is_hidden(), "0 건 사이드바에서 액션 바는 hidden"
        n_selected = ui_page.evaluate(
            "() => document.querySelectorAll('.meeting-item.selected').length"
        )
        assert n_selected == 0, "0 건 사이드바에서 selected 항목은 0"

        cls = list_panel.first.get_attribute("class") or ""
        assert "meetings-list--selecting" not in cls, (
            f"0 건 사이드바에서 selection mode 클래스 부재 보장 (class={cls!r})"
        )

    def test_B12_selection_중_새_회의_추가_시_기존_선택_보존(
        self, ui_page: Page
    ) -> None:
        """Given: 2 개 선택됨 (selection mode ON)
        When:  watchdog 등이 새 회의 1 건을 사이드바 상단에 prepend (DOM 직접 시뮬레이션)
        Then:  기존 선택 2 개 보존 + 새 항목은 미선택 상태 + selection mode 유지.

        근거: bulk-actions.md §1.5 "selection mode 활성 중 새 회의 도착 →
              mode 유지, 새 항목 자동 선택 안 함" (review-2b §4 WARN 필수 추가).
        주의: 실제 SSE/watchdog 통합은 Phase 3 백엔드 영역. 본 시나리오는
              SPA 의 DOM 갱신 정책 (selection 보존) 만 검증.
        """
        items = _meeting_items(ui_page)
        # 1, 2 번째 선택 (앵커 + 추가)
        items.nth(0).locator(".meeting-item-checkbox").click()
        items.nth(1).locator(".meeting-item-checkbox").click()
        ui_page.wait_for_timeout(200)

        # 새 회의 1 건을 사이드바 최상단에 prepend (watchdog 시뮬레이션)
        ui_page.evaluate(
            """() => {
                const list = document.getElementById('listContent');
                if (!list) return;
                // 기존 첫 항목을 복제 + meeting-id 만 변경 (실제 SPA 렌더링 결과를 모방)
                const first = list.querySelector('.meeting-item');
                if (!first) return;
                const clone = first.cloneNode(true);
                clone.setAttribute('data-meeting-id', 'meeting_NEW_INCOMING');
                clone.classList.remove('selected');  // 새 항목은 미선택
                // selection mode 클래스 보존을 위해 부모 클래스는 건드리지 않음
                list.insertBefore(clone, first);
            }"""
        )
        ui_page.wait_for_timeout(150)

        # 기존 2 개 선택 보존 + 새 항목 미선택 → 총 selected 카운트 == 2
        n_selected = ui_page.evaluate(
            "() => document.querySelectorAll('.meeting-item.selected').length"
        )
        assert n_selected == 2, (
            f"새 회의 추가 시 기존 선택 2 개 보존 + 새 항목 미선택 (got {n_selected})"
        )

        # selection mode 유지 (액션 바 노출, 컨테이너 클래스 유지)
        bar = _bulk_bar(ui_page)
        expect(bar).to_be_visible()
        list_panel = meetings_listbox(ui_page).first
        cls = list_panel.get_attribute("class") or ""
        assert "meetings-list--selecting" in cls, (
            f"새 회의 추가 후에도 selection mode 유지 (class={cls!r})"
        )


# ============================================================================
# 그룹 2 — 컨텍스트 액션 바 (A*)
# ============================================================================


class TestBulkActionBar:
    """선택 카운트 기반 sticky 액션 바 + POST /api/meetings/batch 디스패치."""

    def test_A1_0개_선택_시_액션_바_비표시(self, ui_page: Page) -> None:
        """Given: 페이지 로드 직후 (선택 0)
        When:  `.bulk-action-bar` 가시성 조회
        Then:  hidden 또는 DOM 부재.

        근거: bulk-actions.md §1.5 "[OFF] 상태 → 액션 바 비표시".
        """
        bar = _bulk_bar(ui_page)
        if bar.count() == 0:
            return  # absent 도 정책상 OK
        assert bar.first.is_hidden(), "선택 0 일 때 액션 바는 hidden 이어야 함"

    def test_A2_1개_선택_시_액션_바_슬라이드_다운(self, ui_page: Page) -> None:
        """Given: 비선택 상태
        When:  첫 체크박스 클릭
        Then:  `.bulk-action-bar` 표시 + transform translateY 가 0 (슬라이드 완료).

        근거: bulk-actions.md §2.4 "0 → 1 선택: slide-down + fade-in".
        """
        items = _meeting_items(ui_page)
        items.nth(0).locator(".meeting-item-checkbox").click()
        ui_page.wait_for_timeout(350)  # 슬라이드 250ms 완료
        bar = _bulk_bar(ui_page)
        expect(bar).to_be_visible()
        # transform 검증 — translateY 0 (matrix(1,0,0,1,0,0) 또는 none)
        transform = bar.evaluate("el => getComputedStyle(el).transform")
        # matrix 값에서 6번째 (translateY) 가 0 인지 — none 도 OK
        assert transform == "none" or "0, 0)" in transform or transform.endswith(", 0)"), (
            f"슬라이드 완료 후 translateY 0 이어야 함 (transform={transform!r})"
        )

    def test_A3_N개_선택_카운트_라벨_갱신(self, ui_page: Page) -> None:
        """Given: 비선택 상태
        When:  3 개 체크박스 순차 클릭
        Then:  `.bulk-action-bar__count` 가 "3" 을 포함 (또는 carrying aria-label "3개 선택됨").

        근거: bulk-actions.md §2.2 "[N개 선택됨] — 카운트 숫자 + 라벨".
        """
        items = _meeting_items(ui_page)
        for i in range(3):
            items.nth(i).locator(".meeting-item-checkbox").click()
            ui_page.wait_for_timeout(80)
        ui_page.wait_for_timeout(300)
        count_el = ui_page.locator(".bulk-action-bar__count")
        expect(count_el).to_be_visible()
        text = count_el.text_content() or ""
        aria = count_el.get_attribute("aria-label") or ""
        assert "3" in text or "3" in aria, (
            f"카운트 라벨에 '3' 이 보여야 함 (text={text!r}, aria-label={aria!r})"
        )

    def test_A4_전사_버튼은_action_transcribe_로_batch_API_호출(
        self, ui_page: Page, ui_bulk_meeting_ids: list[str]
    ) -> None:
        """Given: 2 개 선택 + route mock 설치
        When:  `.bulk-action-btn[data-action="transcribe"]` 클릭
        Then:  POST /api/meetings/batch 호출 + 페이로드:
                {"action":"transcribe","scope":"selected","meeting_ids":[...]}.

        근거: bulk-actions.md §2.2 "[전사] 클릭 → POST /api/meetings/batch".
        """
        captured = _install_batch_route_mock(ui_page)
        items = _meeting_items(ui_page)
        items.nth(0).locator(".meeting-item-checkbox").click()
        items.nth(1).locator(".meeting-item-checkbox").click()
        ui_page.wait_for_timeout(200)

        ui_page.locator(".bulk-action-btn[data-action='transcribe']").click()
        ui_page.wait_for_timeout(500)

        assert len(captured["calls"]) == 1, f"batch API 1회 호출 기대 (got {captured['calls']})"
        body = captured["calls"][0]["body"]
        assert body.get("action") == "transcribe", f"action='transcribe' 기대: {body!r}"
        assert body.get("scope") == "selected", f"scope='selected' 기대: {body!r}"
        ids = body.get("meeting_ids") or []
        assert isinstance(ids, list) and len(ids) == 2, f"meeting_ids 길이 2 기대: {body!r}"

    def test_A5_요약_버튼은_action_summarize_로_batch_API_호출(
        self, ui_page: Page
    ) -> None:
        """Given: 2 개 선택 + route mock
        When:  `.bulk-action-btn[data-action="summarize"]` 클릭
        Then:  POST /api/meetings/batch with action="summarize", scope="selected".

        근거: bulk-actions.md §2.2 "[요약] 클릭".
        """
        captured = _install_batch_route_mock(ui_page)
        items = _meeting_items(ui_page)
        items.nth(0).locator(".meeting-item-checkbox").click()
        items.nth(1).locator(".meeting-item-checkbox").click()
        ui_page.wait_for_timeout(200)

        ui_page.locator(".bulk-action-btn[data-action='summarize']").click()
        ui_page.wait_for_timeout(500)
        assert len(captured["calls"]) == 1, "batch API 1회 호출 기대"
        body = captured["calls"][0]["body"]
        assert body.get("action") == "summarize"
        assert body.get("scope") == "selected"

    def test_A6_전사_요약_버튼은_action_full_로_batch_API_호출(
        self, ui_page: Page
    ) -> None:
        """Given: 2 개 선택 + route mock
        When:  `.bulk-action-btn[data-action="both"]` 클릭
        Then:  POST /api/meetings/batch with action="full".

        근거: bulk-actions.md §2.2 "[전사+요약] 클릭".
        주의: 핸드오프 §1.2 가 data-action="both" 이므로, 페이로드의 action 값은
              "full" (백엔드 계약). UI selector 와 페이로드 키가 다른 정상 케이스.
        """
        captured = _install_batch_route_mock(ui_page)
        items = _meeting_items(ui_page)
        items.nth(0).locator(".meeting-item-checkbox").click()
        items.nth(1).locator(".meeting-item-checkbox").click()
        ui_page.wait_for_timeout(200)

        ui_page.locator(".bulk-action-btn[data-action='both']").click()
        ui_page.wait_for_timeout(500)
        assert len(captured["calls"]) == 1, "batch API 1회 호출 기대"
        body = captured["calls"][0]["body"]
        assert body.get("action") == "full", f"action='full' 기대 (selector data-action='both'): {body!r}"
        assert body.get("scope") == "selected"

    def test_A7_해제_버튼은_선택을_해제하고_액션_바를_숨긴다(
        self, ui_page: Page
    ) -> None:
        """Given: 3 개 선택됨 + 액션 바 표시
        When:  `.bulk-action-bar__dismiss` 클릭
        Then:  모든 선택 해제 + 액션 바 hidden.

        근거: bulk-actions.md §1.5 "[✕ 해제] 버튼 클릭 → 전체 해제 + OFF".
        """
        items = _meeting_items(ui_page)
        for i in range(3):
            items.nth(i).locator(".meeting-item-checkbox").click()
            ui_page.wait_for_timeout(80)
        ui_page.wait_for_timeout(300)
        ui_page.locator(".bulk-action-bar__dismiss").click()
        ui_page.wait_for_timeout(350)
        for i in range(5):
            cls = items.nth(i).get_attribute("class") or ""
            assert "selected" not in cls.split(), (
                f"[✕] 해제 후 index={i} 는 비선택이어야 함 (class={cls!r})"
            )
        bar = _bulk_bar(ui_page)
        if bar.count() > 0:
            assert bar.first.is_hidden(), "[✕] 해제 후 액션 바 hidden 필요"

    def test_A8_액션_실행_후_toast_메시지_표시(self, ui_page: Page) -> None:
        """Given: 2 개 선택 + route mock (응답에 message 포함)
        When:  [전사] 클릭 → 응답 수신
        Then:  toast 메시지에 "처리" / "건너뜀" 키워드 표시.

        근거: bulk-actions.md §2.5.1 "N개 처리, M개 건너뜀 (이미 처리됨)" 패턴.
        """
        _install_batch_route_mock(ui_page)
        items = _meeting_items(ui_page)
        items.nth(0).locator(".meeting-item-checkbox").click()
        items.nth(1).locator(".meeting-item-checkbox").click()
        ui_page.wait_for_timeout(200)
        ui_page.locator(".bulk-action-btn[data-action='transcribe']").click()
        ui_page.wait_for_timeout(800)
        # toast 는 .toast / .home-status / role="status" 중 하나에 출력 — 라벨 검색
        toast_locator = ui_page.locator(
            "[role='status']:visible, .toast:visible, .home-status:visible"
        )
        # 적어도 하나는 처리/건너뜀 메시지 포함해야 함
        assert toast_locator.count() > 0, "toast/status 영역이 보여야 함"
        combined = " ".join(
            (toast_locator.nth(i).text_content() or "") for i in range(toast_locator.count())
        )
        assert "처리" in combined or "건너뜀" in combined or "queued" in combined.lower(), (
            f"toast 에 처리/건너뜀 키워드 필요 (text={combined!r})"
        )

    def test_A9_액션_실행_후_selection_mode_자동_종료(self, ui_page: Page) -> None:
        """Given: 2 개 선택 + route mock
        When:  [요약] 클릭 → 응답 수신
        Then:  모든 선택 해제 + 액션 바 hidden.

        근거: bulk-actions.md §1.5 "액션 실행 완료 → 전체 해제 + OFF (자동 종료)".
        """
        _install_batch_route_mock(ui_page)
        items = _meeting_items(ui_page)
        items.nth(0).locator(".meeting-item-checkbox").click()
        items.nth(1).locator(".meeting-item-checkbox").click()
        ui_page.wait_for_timeout(200)
        ui_page.locator(".bulk-action-btn[data-action='summarize']").click()
        ui_page.wait_for_timeout(800)
        for i in range(5):
            cls = items.nth(i).get_attribute("class") or ""
            assert "selected" not in cls.split(), (
                f"액션 후 index={i} 는 비선택이어야 함 (class={cls!r})"
            )
        bar = _bulk_bar(ui_page)
        if bar.count() > 0:
            assert bar.first.is_hidden(), "액션 후 액션 바 hidden 필요"

    def test_A10_액션_실행_중_재클릭은_무시된다(self, ui_page: Page) -> None:
        """Given: 2 개 선택 + 응답 지연 1500ms route mock
        When:  [전사] 클릭 (in-flight) → 응답 도착 전 [요약] 또는 [전사] 재클릭
        Then:  두 번째 요청은 발사되지 않음 — batch 호출 카운트 == 1.

        근거: review-2b §4 WARN "액션 in-flight debounce" 필수 추가.
              UX 정책: 다중 동시 호출은 큐 폭주 + 사용자 혼란 → 첫 요청 응답
              까지 액션 버튼은 disabled 또는 클릭 무시.
        """
        captured: dict[str, list[dict]] = {"calls": []}

        def _delayed_handle(route, request):
            try:
                body = request.post_data_json or {}
            except Exception:
                body = {}
            captured["calls"].append({"body": body})
            # 1500ms 지연 후 200 응답
            import time as _t

            _t.sleep(1.5)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(_BATCH_OK_RESPONSE),
            )

        ui_page.route("**/api/meetings/batch", _delayed_handle)

        items = _meeting_items(ui_page)
        items.nth(0).locator(".meeting-item-checkbox").click()
        items.nth(1).locator(".meeting-item-checkbox").click()
        ui_page.wait_for_timeout(200)

        # 첫 클릭 (in-flight 진입)
        ui_page.locator(".bulk-action-btn[data-action='transcribe']").click()
        # 응답 도착 전 재클릭 (200ms 후)
        ui_page.wait_for_timeout(200)
        # 동일 [전사] 재클릭 시도 (force=True 로 disabled 라도 클릭 시도)
        ui_page.locator(".bulk-action-btn[data-action='transcribe']").click(force=True)
        # 다른 액션 [요약] 도 클릭 시도
        ui_page.locator(".bulk-action-btn[data-action='summarize']").click(force=True)

        # 첫 응답 도착 + 추가 요청 가능성 모두 흡수 위해 충분히 대기
        ui_page.wait_for_timeout(2000)

        assert len(captured["calls"]) == 1, (
            f"in-flight 중 재클릭은 무시되어야 함 (호출 카운트={len(captured['calls'])}, "
            f"calls={captured['calls']!r})"
        )

    def test_A11_5xx_에러_시_에러_토스트_표시(self, ui_page: Page) -> None:
        """Given: 2 개 선택 + 500 응답 mock
        When:  [전사] 클릭 → 5xx 응답 수신
        Then:  role='alert' 토스트 노출 + 에러 메시지 (실패/오류/error 키워드).

        근거: review-2b §4 WARN "에러 응답 정책" 필수 추가.
              UX 정책: 5xx 시 사용자가 재시도 여부를 결정할 수 있도록 명시적
              알림 + selection 상태는 보존 (사용자가 다시 시도 가능).
        """
        captured: list[dict] = []

        def _err_handle(route, request):
            captured.append({"method": request.method})
            route.fulfill(
                status=500,
                content_type="application/json",
                body=json.dumps({"error": "internal", "message": "처리 실패"}),
            )

        ui_page.route("**/api/meetings/batch", _err_handle)

        items = _meeting_items(ui_page)
        items.nth(0).locator(".meeting-item-checkbox").click()
        items.nth(1).locator(".meeting-item-checkbox").click()
        ui_page.wait_for_timeout(200)
        ui_page.locator(".bulk-action-btn[data-action='transcribe']").click()
        ui_page.wait_for_timeout(800)

        # 500 응답이 한 번 도달했는지 확인 (mock 호출 발생)
        assert len(captured) == 1, f"5xx mock 가 호출되어야 함 (calls={captured!r})"

        # 에러 토스트 노출 — role='alert' 또는 .toast--error 또는 .home-status[data-level='error']
        alert_locator = ui_page.locator(
            "[role='alert']:visible, .toast--error:visible, "
            ".home-status[data-level='error']:visible"
        )
        assert alert_locator.count() > 0, "5xx 응답 시 role='alert' 에러 토스트 노출 필요"
        combined = " ".join(
            (alert_locator.nth(i).text_content() or "") for i in range(alert_locator.count())
        )
        assert (
            "실패" in combined
            or "오류" in combined
            or "에러" in combined
            or "error" in combined.lower()
        ), f"에러 토스트에 실패/오류/error 키워드 필요 (text={combined!r})"


# ============================================================================
# 그룹 3 — 홈 화면 일괄 액션 (H*)
# ============================================================================


class TestHomeBulkDropdowns:
    """홈 화면의 [전체 일괄 ▾] / [최근 24시간 ▾] 드롭다운."""

    def test_H1_홈에_두_드롭다운_트리거가_존재한다(self, ui_page: Page) -> None:
        """Given: HomeView 로드
        When:  드롭다운 트리거 buttons 카운트
        Then:  `.home-action-btn--dropdown` 이 정확히 2 개 (전체 일괄 / 최근 24시간).

        근거: bulk-actions.md §3.1 "버튼 라인 변경 — 드롭다운 트리거 2 개 추가".
        """
        triggers = ui_page.locator(".home-action-btn--dropdown")
        expect(triggers).to_have_count(2)
        # data-dropdown 식별자가 두 트리거에 정확히 부여되어야 함
        assert (
            ui_page.locator(".home-action-btn--dropdown[data-dropdown='all-bulk']").count() == 1
        ), "[전체 일괄] 트리거 (data-dropdown='all-bulk') 필요"
        assert (
            ui_page.locator(".home-action-btn--dropdown[data-dropdown='recent-24h']").count() == 1
        ), "[최근 24시간] 트리거 (data-dropdown='recent-24h') 필요"

    def test_H2_전체_일괄_클릭_시_메뉴_3개_항목_노출(self, ui_page: Page) -> None:
        """Given: 드롭다운 닫힘
        When:  [전체 일괄 ▾] 클릭
        Then:  메뉴가 열리고 3 항목 (전사+요약 / 전사만 / 요약만) 표시,
               aria-expanded="true".

        근거: bulk-actions.md §3.4 "두 드롭다운 모두 동일한 3 옵션".
        """
        trigger = ui_page.locator(".home-action-btn--dropdown[data-dropdown='all-bulk']")
        trigger.click()
        ui_page.wait_for_timeout(200)
        expect(trigger).to_have_attribute("aria-expanded", "true")
        # 트리거 부모 wrapper 안의 메뉴
        wrapper = trigger.locator("..")
        menu = wrapper.locator(".home-action-dropdown")
        expect(menu).to_be_visible()
        items = menu.locator("[role='menuitemradio']")
        expect(items).to_have_count(3)
        # 데이터 식별자 검증
        for opt in ("both", "transcribe", "summarize"):
            assert (
                menu.locator(f"[role='menuitemradio'][data-option='{opt}']").count() == 1
            ), f"메뉴에 data-option='{opt}' 항목 필요"

    def test_H3_전체_일괄_메뉴_항목_클릭은_scope_all_로_batch_API_호출(
        self, ui_page: Page
    ) -> None:
        """Given: [전체 일괄] 메뉴 열림 + route mock
        When:  "전사+요약" (data-option="both") 항목 클릭
        Then:  POST /api/meetings/batch with scope="all", action="full".

        근거: bulk-actions.md §3 "전체 범위 메뉴".
        """
        captured = _install_batch_route_mock(ui_page)
        ui_page.locator(".home-action-btn--dropdown[data-dropdown='all-bulk']").click()
        ui_page.wait_for_timeout(200)
        ui_page.locator(
            ".home-action-dropdown [role='menuitemradio'][data-option='both']"
        ).click()
        ui_page.wait_for_timeout(500)
        assert len(captured["calls"]) == 1, f"batch API 1회 호출 기대 (got {captured['calls']})"
        body = captured["calls"][0]["body"]
        assert body.get("scope") == "all", f"scope='all' 기대: {body!r}"
        assert body.get("action") == "full", f"action='full' 기대: {body!r}"

    def test_H4_최근_24시간_드롭다운은_scope_recent_hours_24_로_호출(
        self, ui_page: Page
    ) -> None:
        """Given: [최근 24시간] 메뉴 열림 + route mock
        When:  "전사만" (data-option="transcribe") 항목 클릭
        Then:  POST /api/meetings/batch with scope="recent", hours=24, action="transcribe".

        근거: bulk-actions.md §3 "최근 24시간 범위 메뉴".
        """
        captured = _install_batch_route_mock(ui_page)
        ui_page.locator(".home-action-btn--dropdown[data-dropdown='recent-24h']").click()
        ui_page.wait_for_timeout(200)
        ui_page.locator(
            ".home-action-dropdown [role='menuitemradio'][data-option='transcribe']"
        ).click()
        ui_page.wait_for_timeout(500)
        assert len(captured["calls"]) == 1, "batch API 1회 호출 기대"
        body = captured["calls"][0]["body"]
        assert body.get("scope") == "recent", f"scope='recent' 기대: {body!r}"
        assert body.get("hours") == 24, f"hours=24 기대: {body!r}"
        assert body.get("action") == "transcribe"

    def test_H5_드롭다운_외부_클릭_시_메뉴_닫힘(self, ui_page: Page) -> None:
        """Given: [전체 일괄] 메뉴 열림
        When:  메뉴 밖 영역 클릭 (사이드바)
        Then:  메뉴 hidden, aria-expanded="false".

        근거: bulk-actions.md §3.6 "외부 클릭 → 닫기 (포커스는 트리거로 복귀)".
        """
        trigger = ui_page.locator(".home-action-btn--dropdown[data-dropdown='all-bulk']")
        trigger.click()
        ui_page.wait_for_timeout(200)
        # 외부 영역 클릭 — 사이드바 헤더
        ui_page.locator(".list-header").first.click(position={"x": 5, "y": 5})
        ui_page.wait_for_timeout(200)
        expect(trigger).to_have_attribute("aria-expanded", "false")
        wrapper = trigger.locator("..")
        menu = wrapper.locator(".home-action-dropdown")
        # menu 가 hidden 또는 .is-open 클래스 제거.
        assert menu.is_hidden() or "is-open" not in (menu.get_attribute("class") or ""), (
            "외부 클릭 후 메뉴 닫힘 필요"
        )

    def test_H6_키보드로_드롭다운_조작이_가능하다(self, ui_page: Page) -> None:
        """Given: [전체 일괄] 트리거에 포커스
        When:  Enter 로 열기 → ↓ 로 다음 항목 → Esc 로 닫기
        Then:  열기/이동/닫기 모두 동작 + Esc 후 포커스가 트리거로 복귀.

        근거: bulk-actions.md §3.6 "키보드: Enter/Space=열기, ↑↓=이동, Esc=닫기".
        """
        trigger = ui_page.locator(".home-action-btn--dropdown[data-dropdown='all-bulk']")
        trigger.focus()
        ui_page.keyboard.press("Enter")
        ui_page.wait_for_timeout(200)
        expect(trigger).to_have_attribute("aria-expanded", "true")

        # ↓ 로 다음 항목 이동 — 첫 진입 시 첫 항목 또는 두 번째로 이동했는지 확인
        ui_page.keyboard.press("ArrowDown")
        ui_page.wait_for_timeout(120)
        focused_role = ui_page.evaluate(
            "() => document.activeElement && document.activeElement.getAttribute('role')"
        )
        assert focused_role == "menuitemradio", (
            f"↓ 후 활성 요소는 menuitemradio 여야 함 (got role={focused_role!r})"
        )

        ui_page.keyboard.press("Escape")
        ui_page.wait_for_timeout(150)
        expect(trigger).to_have_attribute("aria-expanded", "false")
        focused_id = ui_page.evaluate(
            "() => document.activeElement && (document.activeElement.getAttribute('data-dropdown') || '')"
        )
        assert focused_id == "all-bulk", (
            f"Esc 후 포커스는 트리거로 복귀해야 함 (got data-dropdown={focused_id!r})"
        )
