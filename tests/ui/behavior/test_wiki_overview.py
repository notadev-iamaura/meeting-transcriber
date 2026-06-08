"""wiki-overview-search — 현황(Overview) 탭 + 검색 결과 메타 행동 검증.

티켓: T-303 (wave 3, component `wiki-overview-search`)
계획서: memorable-wiki §5 C3 "UI — 현황 화면 + 검색 메타"
디자인 산출물(목업): wiki-overview-search 마크다운 목업 + design.md 토큰 매핑
  - ARIA 탭 바(`현황`/`검색`) — 뷰 내부 상태, SPA 라우터 미변경
  - 현황 탭 → GET /api/wiki/digest 4섹션 렌더
  - 검색 탭 → 결과 카드 메타(score·snippet·status·citations) 승격
  - 현황/검색 인용 → /app/viewer/{id}?t=초 deep link (기존 위임 패턴 재사용)

검증 범위 (단일 컴포넌트 `wiki-overview-search`, 단일 책임):
    T*  — ARIA 탭 구조 + 키보드 + 기본 활성 탭
    O*  — 현황 탭 digest 4섹션 렌더 + 빈 다이제스트 + 인용 deep link
    S*  — 검색 결과 카드 메타(score/snippet/status/citations) + 인용 deep link
    R*  — 회귀 방지(기존 검색/필터/상세/Health 모달 + destroy 정리)

Red 의도성 (구현 전):
    현재 `ui/web/wiki-view.js` 에는 탭 구조 자체가 없다. `_render()` 가
    그리는 DOM 은 `.wiki-header` + `.wiki-decision-filters` + `.wiki-body`
    (트리 + 미리보기) 뿐이며, 아래 selector 가 전부 부재하다:
        - `[data-component='wiki-overview-search']`
        - `[role='tablist']`, `[role='tab']` (#wikiTabOverview / #wikiTabSearch)
        - `[role='tabpanel']` (#wikiOverviewPanel / #wikiSearchPanel)
        - `.wiki-ov-card`, `.wiki-ov-section`, `.wiki-status-badge`
        - `.wiki-result-card`, `.wiki-result-card__score`
    `_renderSearchTree` (wiki-view.js 496~532줄) 는 현재 title + snippet 만
    렌더하고 score/status/citations 를 표시하지 않는다. 따라서 본 모듈의
    모든 시나리오는 selector 부재 / 속성 부재로 깨끗한 assertion FAIL 이
    발생한다 (import 오류·fixture ERROR 가 아니라 FAIL).

fixture 전략:
    conftest.py 의 `ui_bulk_base_url` (실제 FastAPI 서버 subprocess) 를
    재사용한다. 단, 테스트 환경의 wiki 디렉토리에는 digest/검색 데이터가
    없을 수 있으므로 `/api/wiki/digest` 와 `/api/wiki/search` 응답을
    `page.route` 로 mock 해 결정론적 데이터를 주입한다 (bulk-actions 가
    `/api/meetings/batch` 를 mock 하는 패턴과 동일). 이렇게 하면 백엔드
    계약(이미 머지됨)을 변경하지 않고 소비 측 렌더만 검증한다.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Browser, Page, expect

pytestmark = [pytest.mark.ui]


# ============================================================================
# Mock 데이터 — digest / search 응답 단일 진실 공급원
# ============================================================================

# 두 명의 owner(미해결 액션) + 결정 2건 + 프로젝트 2건을 포함한 digest.
# 인용 마커는 백엔드 계약 형식 "[meeting:{id}@HH:MM:SS]".
_MOCK_MEETING_ID = "meeting_20260605_143000"

_DIGEST_RESPONSE = {
    "generated_for": "2026-06-08",
    "total_open_actions": 5,
    "open_actions": [
        {
            "owner": "김민수",
            "items": [
                {
                    "description": "런칭 날짜 확정 후 공유",
                    "citations": [f"[meeting:{_MOCK_MEETING_ID}@00:23:45]"],
                    "due_date": "2026-06-15",
                },
                {
                    "description": "API 스펙 리뷰",
                    "citations": [f"[meeting:{_MOCK_MEETING_ID}@00:41:02]"],
                    "due_date": None,
                },
            ],
        },
        {
            "owner": "이지은",
            "items": [
                {
                    "description": "디자인 토큰 정리",
                    "citations": [f"[meeting:{_MOCK_MEETING_ID}@00:08:11]"],
                    "due_date": "2026-06-10",
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
        {
            "page_path": "decisions/2026-06-03-stt-model.md",
            "title": "STT 기본 모델 large-v3-turbo 채택",
            "decision_date": "2026-06-03",
            "status": "decided",
            "project": "stt",
            "citations": [f"[meeting:{_MOCK_MEETING_ID}@00:12:08]"],
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
        {
            "project": "memorable-wiki",
            "last_title": "C3 UI 현황 화면 + 검색 메타",
            "last_date": "2026-06-08",
            "status": "pending",
            "page_path": "decisions/2026-06-08-c3-ui.md",
        },
    ],
}

# 빈 다이제스트(위키 비활성/부재) — 백엔드는 200 + 빈 본문을 반환.
_DIGEST_EMPTY_RESPONSE = {
    "generated_for": "",
    "total_open_actions": 0,
    "open_actions": [],
    "recent_decisions": [],
    "project_status": [],
}

# 검색 결과 — status 가 있는 항목(결정)과 없는 항목(주제) 혼합.
# 백엔드는 score 내림차순으로 정렬해 응답한다.
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
# Route mock 헬퍼
# ============================================================================


def _install_wiki_mocks(
    page: Page,
    *,
    digest: dict | None = None,
    search: dict | None = None,
) -> dict[str, list[dict]]:
    """`/api/wiki/digest` 와 `/api/wiki/search` 호출을 가로채 결정론적 응답을 준다.

    Args:
        page: Playwright Page.
        digest: digest 응답(미지정 시 `_DIGEST_RESPONSE`).
        search: search 응답(미지정 시 `_SEARCH_RESPONSE`).

    Returns:
        captured — {"digest": [...요청], "search": [...요청]} 호출 추적용.
    """
    digest_body = _DIGEST_RESPONSE if digest is None else digest
    search_body = _SEARCH_RESPONSE if search is None else search
    captured: dict[str, list[dict]] = {"digest": [], "search": []}

    def _handle_digest(route, request):
        captured["digest"].append({"method": request.method, "url": request.url})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(digest_body, ensure_ascii=False),
        )

    def _handle_search(route, request):
        captured["search"].append({"method": request.method, "url": request.url})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(search_body, ensure_ascii=False),
        )

    # 구체적 경로(digest)를 search 보다 먼저 등록 — Playwright 는 마지막 등록
    # 핸들러를 우선하므로, 둘은 경로가 겹치지 않아 순서 무관하지만 명시적으로 분리.
    page.route("**/api/wiki/digest", _handle_digest)
    page.route("**/api/wiki/search?**", _handle_search)
    page.route("**/api/wiki/search", _handle_search)
    return captured


# ============================================================================
# 페이지 헬퍼
# ============================================================================


@pytest.fixture
def wiki_page(browser: Browser, ui_bulk_base_url: str) -> Page:
    """`/app/wiki` (WikiView) 를 로드한 Page — digest/search mock 설치 포함.

    각 테스트는 새 context 로 격리된 route handler 를 받는다. mock 은
    `page.goto` 이전에 설치해 최초 digest fetch 도 가로챈다.
    """
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        color_scheme="light",
    )
    page = context.new_page()
    _install_wiki_mocks(page)
    # networkidle 은 부하(전체 수트 동시 실행) 하에서 flaky → domcontentloaded +
    # 명시적 셀렉터 대기로 견고화. 현황 패널까지 그려진 뒤 테스트에 넘긴다.
    page.goto(f"{ui_bulk_base_url}/app/wiki", wait_until="domcontentloaded")
    page.wait_for_selector(".wiki-view", timeout=15000)
    page.wait_for_selector("#wikiOverviewPanel", timeout=15000)
    yield page
    context.close()


def _component_root(page: Page):
    """컴포넌트 루트 locator — 목업이 명시한 `data-component` 마커."""
    return page.locator("[data-component='wiki-overview-search']")


def _tablist(page: Page):
    """ARIA 탭 바 locator (목업 §1 — role=tablist)."""
    return page.locator("[role='tablist']")


def _overview_tab(page: Page):
    """현황 탭 버튼 (목업 §1 — id=wikiTabOverview, role=tab)."""
    return page.locator("#wikiTabOverview[role='tab']")


def _search_tab(page: Page):
    """검색 탭 버튼 (목업 §1 — id=wikiTabSearch, role=tab)."""
    return page.locator("#wikiTabSearch[role='tab']")


def _overview_panel(page: Page):
    """현황 tabpanel (목업 §1 — id=wikiOverviewPanel, role=tabpanel)."""
    return page.locator("#wikiOverviewPanel[role='tabpanel']")


def _search_panel(page: Page):
    """검색 tabpanel (목업 §1 — id=wikiSearchPanel, role=tabpanel)."""
    return page.locator("#wikiSearchPanel[role='tabpanel']")


def _result_cards(page: Page):
    """검색 결과 카드 locator (목업 §3 — `.wiki-result-card`)."""
    return page.locator(".wiki-result-card")


def _switch_to_search_tab(page: Page) -> None:
    """검색 탭으로 전환하고 결과 카드가 렌더될 때까지 검색을 수행한다.

    검색 탭은 기존 검색 input(#wikiSearchInput)을 사용한다. 탭 전환 후
    검색어를 입력하면 200ms debounce 뒤 mock 된 /api/wiki/search 응답이
    카드로 렌더된다.
    """
    _search_tab(page).click()
    page.wait_for_timeout(150)
    page.fill("#wikiSearchInput", "런칭")
    # debounce 200ms + 렌더 — 카드가 나타날 때까지 대기.
    page.wait_for_selector(".wiki-result-card", timeout=5000)


# ============================================================================
# 그룹 T — ARIA 탭 구조 + 기본 활성 탭 + 키보드
# ============================================================================


class TestTabStructure:
    """role=tablist/tab/tabpanel 구조 + 기본 활성 + 키보드 이동."""

    def test_T1_탭바에_현황_검색_두개의_탭이_있다(self, wiki_page: Page) -> None:
        """Given: /app/wiki 진입 (WikiView 마운트)
        When:  role=tablist 안의 role=tab 카운트 조회
        Then:  정확히 2 개 — '현황'(wikiTabOverview)·'검색'(wikiTabSearch).

        근거: 목업 §1 "role=tablist 안에 wiki-tab 2 개".
        """
        tablist = _tablist(wiki_page)
        expect(tablist).to_have_count(1)
        tabs = tablist.locator("[role='tab']")
        expect(tabs).to_have_count(2)
        expect(_overview_tab(wiki_page)).to_have_count(1)
        expect(_search_tab(wiki_page)).to_have_count(1)

    def test_T2_기본_활성탭은_현황이다(self, wiki_page: Page) -> None:
        """Given: /app/wiki 진입 직후
        When:  두 탭의 aria-selected + 패널 가시성 조회
        Then:  현황 탭 aria-selected=true, 검색 탭 aria-selected=false,
               현황 패널만 표시(검색 패널 hidden).

        근거: 목업 §1 "기본 진입 탭 = 현황", §0 "현황 활성 시 검색 패널 hidden".
        """
        expect(_overview_tab(wiki_page)).to_have_attribute("aria-selected", "true")
        expect(_search_tab(wiki_page)).to_have_attribute("aria-selected", "false")
        expect(_overview_panel(wiki_page)).to_be_visible()
        # 검색 패널은 hidden 속성 또는 display:none.
        search_panel = _search_panel(wiki_page)
        assert search_panel.is_hidden(), "기본 진입 시 검색 패널은 hidden 이어야 함"

    def test_T3_탭은_aria_controls로_패널과_연결된다(self, wiki_page: Page) -> None:
        """Given: 탭 + 패널
        When:  탭의 aria-controls 가 가리키는 패널 ID 조회
        Then:  현황 탭 → wikiOverviewPanel, 검색 탭 → wikiSearchPanel 연결.

        근거: 목업 §1 ARIA 계약 "aria-controls=wikiOverviewPanel / wikiSearchPanel".
        """
        assert _overview_tab(wiki_page).get_attribute("aria-controls") == "wikiOverviewPanel", (
            "현황 탭 aria-controls='wikiOverviewPanel' 필수"
        )
        assert _search_tab(wiki_page).get_attribute("aria-controls") == "wikiSearchPanel", (
            "검색 탭 aria-controls='wikiSearchPanel' 필수"
        )

    def test_T4_검색탭_클릭_시_패널이_전환된다(self, wiki_page: Page) -> None:
        """Given: 현황 탭 활성
        When:  검색 탭 클릭
        Then:  검색 탭 aria-selected=true + 검색 패널 표시 + 현황 패널 hidden.

        근거: 목업 §0 "검색 활성 시 기존 헤더/필터/트리/미리보기 표시".
        """
        _search_tab(wiki_page).click()
        wiki_page.wait_for_timeout(150)
        expect(_search_tab(wiki_page)).to_have_attribute("aria-selected", "true")
        expect(_overview_tab(wiki_page)).to_have_attribute("aria-selected", "false")
        expect(_search_panel(wiki_page)).to_be_visible()
        assert _overview_panel(wiki_page).is_hidden(), "검색 활성 시 현황 패널 hidden"

    def test_T5_좌우_화살표키로_탭_이동(self, wiki_page: Page) -> None:
        """Given: 현황 탭에 포커스
        When:  → 키 입력 (다음 탭으로 roving tabindex)
        Then:  검색 탭이 활성(aria-selected=true) + 포커스 이동.

        근거: 목업 §1 "키보드: ← → 로 탭 이동(roving tabindex)".
        """
        _overview_tab(wiki_page).focus()
        wiki_page.keyboard.press("ArrowRight")
        wiki_page.wait_for_timeout(150)
        expect(_search_tab(wiki_page)).to_have_attribute("aria-selected", "true")
        focused_id = wiki_page.evaluate(
            "() => document.activeElement && document.activeElement.id"
        )
        assert focused_id == "wikiTabSearch", (
            f"→ 키 후 포커스가 검색 탭으로 이동해야 함 (got {focused_id!r})"
        )

    def test_T6_Home_End_키로_처음_끝_탭_이동(self, wiki_page: Page) -> None:
        """Given: 검색 탭에 포커스(End 후)
        When:  Home 키 입력
        Then:  현황 탭(첫 탭)이 활성 + 포커스 이동.

        근거: 목업 §1 "Home/End 처음·끝".
        """
        _overview_tab(wiki_page).focus()
        # End → 마지막(검색) 탭
        wiki_page.keyboard.press("End")
        wiki_page.wait_for_timeout(120)
        expect(_search_tab(wiki_page)).to_have_attribute("aria-selected", "true")
        # Home → 첫(현황) 탭
        wiki_page.keyboard.press("Home")
        wiki_page.wait_for_timeout(120)
        expect(_overview_tab(wiki_page)).to_have_attribute("aria-selected", "true")
        focused_id = wiki_page.evaluate(
            "() => document.activeElement && document.activeElement.id"
        )
        assert focused_id == "wikiTabOverview", (
            f"Home 키 후 포커스가 현황 탭으로 복귀해야 함 (got {focused_id!r})"
        )

    def test_T7_roving_tabindex_적용(self, wiki_page: Page) -> None:
        """Given: 기본 진입(현황 활성)
        When:  두 탭의 tabindex 조회
        Then:  활성 탭 tabindex=0, 비활성 탭 tabindex=-1 (roving 모델).

        근거: 목업 §1 ARIA 계약 "tabindex=0 / tabindex=-1".
        """
        assert _overview_tab(wiki_page).get_attribute("tabindex") == "0", (
            "활성(현황) 탭 tabindex=0 필수"
        )
        assert _search_tab(wiki_page).get_attribute("tabindex") == "-1", (
            "비활성(검색) 탭 tabindex=-1 필수 (roving tabindex)"
        )


# ============================================================================
# 그룹 O — 현황(Overview) 탭 digest 4섹션 렌더
# ============================================================================


class TestOverviewPanel:
    """GET /api/wiki/digest 4섹션 렌더 + 빈 다이제스트 + 인용 deep link."""

    def test_O1_현황탭은_digest_API를_1회_호출한다(self, wiki_page: Page) -> None:
        """Given: 새 context + digest mock (호출 추적)
        When:  /app/wiki 진입 → 현황 탭 자동 활성
        Then:  GET /api/wiki/digest 가 정확히 1 회 호출.

        근거: acceptance [behavior] "현황 탭이 GET /api/wiki/digest 를 1회 호출".
        주의: wiki_page fixture 가 이미 mock 을 설치하고 진입했으므로 본
              테스트는 별도 context 로 호출 카운트를 직접 검증한다.
        """
        # 별도 page 로 호출 카운트 정밀 검증 (wiki_page 는 fixture 내부에서 진입 완료).
        context = wiki_page.context.browser.new_context(
            viewport={"width": 1280, "height": 800}, color_scheme="light"
        )
        page = context.new_page()
        captured = _install_wiki_mocks(page)
        base = wiki_page.url.split("/app/wiki")[0]
        try:
            # networkidle 은 부하(전체 수트 동시 실행 시 다수 서버 인스턴스 경합) 하에서
            # flaky → domcontentloaded + digest 호출 폴링으로 견고화(격리 실행은 통과).
            page.goto(f"{base}/app/wiki", wait_until="domcontentloaded")
            page.wait_for_selector("#wikiOverviewPanel", timeout=15000)
            for _ in range(50):
                if len(captured["digest"]) >= 1:
                    break
                page.wait_for_timeout(100)
            assert len(captured["digest"]) == 1, (
                f"현황 탭은 digest 를 1회 호출해야 함 (got {len(captured['digest'])})"
            )
        finally:
            context.close()

    def test_O2_total_open_actions가_표시된다(self, wiki_page: Page) -> None:
        """Given: digest 응답(total_open_actions=5)
        When:  현황 패널 텍스트 조회
        Then:  '5' 가 미해결 액션 요약(lede)에 표시.

        근거: 목업 §2 ".wiki-overview__lede ← total_open_actions".
        """
        panel = _overview_panel(wiki_page)
        text = panel.text_content() or ""
        assert "5" in text, f"미해결 액션 총수 '5' 가 현황 패널에 표시되어야 함 (text={text!r})"

    def test_O3_owner별_미해결_액션_그룹이_렌더된다(self, wiki_page: Page) -> None:
        """Given: digest open_actions(owner 2명: 김민수·이지은)
        When:  현황 패널의 owner 카드 조회
        Then:  두 owner 이름과 액션 설명이 모두 표시.

        근거: 목업 §2 ① "미해결 액션 · owner별", acceptance [behavior]
              "open_actions(owner별) 렌더".
        """
        panel = _overview_panel(wiki_page)
        text = panel.text_content() or ""
        assert "김민수" in text, "owner '김민수' 표시 필요"
        assert "이지은" in text, "owner '이지은' 표시 필요"
        assert "런칭 날짜 확정 후 공유" in text, "김민수 액션 설명 표시 필요"
        assert "디자인 토큰 정리" in text, "이지은 액션 설명 표시 필요"

    def test_O4_recent_decisions_섹션이_렌더된다(self, wiki_page: Page) -> None:
        """Given: digest recent_decisions(2건)
        When:  현황 패널 텍스트 조회
        Then:  두 결정 제목이 모두 표시.

        근거: 목업 §2 ② "최근 결정", acceptance [behavior] "recent_decisions 렌더".
        """
        panel = _overview_panel(wiki_page)
        text = panel.text_content() or ""
        assert "런칭일을 6월 30일로 확정" in text, "결정 1 제목 표시 필요"
        assert "STT 기본 모델 large-v3-turbo 채택" in text, "결정 2 제목 표시 필요"

    def test_O5_project_status_섹션이_렌더된다(self, wiki_page: Page) -> None:
        """Given: digest project_status(2건: recap-launch·memorable-wiki)
        When:  현황 패널 텍스트 조회
        Then:  두 프로젝트명이 모두 표시.

        근거: 목업 §2 ③ "프로젝트별 현황", acceptance [behavior] "project_status 렌더".
        """
        panel = _overview_panel(wiki_page)
        text = panel.text_content() or ""
        assert "recap-launch" in text, "프로젝트 'recap-launch' 표시 필요"
        assert "memorable-wiki" in text, "프로젝트 'memorable-wiki' 표시 필요"

    def test_O6_결정_상태_배지가_렌더된다(self, wiki_page: Page) -> None:
        """Given: recent_decisions[0].status="decided"
        When:  현황 패널의 .wiki-status-badge 조회
        Then:  최소 1 개 상태 배지가 존재하고 '확정' 라벨을 가짐.

        근거: 목업 §3 status 매핑 "decided → 확정", §5 ".wiki-status-badge".
        """
        panel = _overview_panel(wiki_page)
        badges = panel.locator(".wiki-status-badge")
        assert badges.count() >= 1, "현황 결정/프로젝트에 상태 배지(.wiki-status-badge) 필요"
        combined = " ".join((badges.nth(i).text_content() or "") for i in range(badges.count()))
        assert "확정" in combined, (
            f"decided 상태는 '확정' 라벨로 매핑되어야 함 (badges={combined!r})"
        )

    def test_O7_현황탭_인용_클릭은_뷰어로_이동한다(self, wiki_page: Page) -> None:
        """Given: 현황 패널의 액션 인용 마커([meeting:{id}@00:23:45])
        When:  첫 인용 anchor(.wiki-citation) 클릭
        Then:  /app/viewer/{id}?t=1425 로 라우팅 (00:23:45 = 1425초).

        근거: acceptance [behavior] "현황 탭 인용 클릭 → /app/viewer/{id}?t={초}",
              목업 §5 "인용 deep link 기존 위임 패턴 재사용".
        """
        panel = _overview_panel(wiki_page)
        citation = panel.locator(".wiki-citation").first
        assert citation.count() >= 1, "현황 패널에 인용 anchor(.wiki-citation) 필요"
        citation.click()
        wiki_page.wait_for_url(lambda url: "/app/viewer/" in url, timeout=5000)
        url = wiki_page.url
        assert f"/app/viewer/{_MOCK_MEETING_ID}" in url, (
            f"인용 클릭 시 해당 회의 뷰어로 이동해야 함 (url={url})"
        )
        # 00:23:45 = 1425 초
        assert "t=1425" in url, f"인용 timestamp 가 t=1425 (초) 로 전달되어야 함 (url={url})"

    def test_O8_빈_다이제스트는_빈상태_메시지를_표시한다(
        self, browser: Browser, ui_bulk_base_url: str
    ) -> None:
        """Given: 빈 digest 응답(total=0, 모든 배열 빈)
        When:  /app/wiki 진입 → 현황 탭
        Then:  에러 없이 빈 상태 카피가 표시(예: '없습니다'/'현황').

        근거: acceptance [behavior] "빈 다이제스트 200 응답 시 빈 상태 메시지 표시,
              에러 없음", 목업 §2 "전체 빈 → .wiki-empty-state 재사용".
        """
        context = browser.new_context(
            viewport={"width": 1280, "height": 800}, color_scheme="light"
        )
        page = context.new_page()
        _install_wiki_mocks(page, digest=_DIGEST_EMPTY_RESPONSE)
        try:
            page.goto(f"{ui_bulk_base_url}/app/wiki", wait_until="domcontentloaded")
            page.wait_for_selector("#wikiOverviewPanel", timeout=15000)
            page.wait_for_timeout(500)
            panel = page.locator("#wikiOverviewPanel[role='tabpanel']")
            text = panel.text_content() or ""
            # 빈 상태 카피 — '없습니다' 또는 '현황이 없' 등 (목업 §2 빈상태 카피)
            assert "없습니다" in text or "없음" in text or "아직" in text, (
                f"빈 다이제스트 시 빈 상태 메시지 필요 (text={text!r})"
            )
            # JS 에러로 패널이 비어버리지 않았는지 — 컴포넌트 루트는 존재.
            assert page.locator("[data-component='wiki-overview-search']").count() == 1, (
                "빈 다이제스트에서도 컴포넌트 루트는 정상 마운트되어야 함"
            )
        finally:
            context.close()


# ============================================================================
# 그룹 S — 검색 결과 카드 메타 승격
# ============================================================================


class TestSearchResultMeta:
    """검색 결과 카드의 score·snippet·status·citations 메타 + 인용 deep link."""

    def test_S1_검색결과가_카드로_렌더된다(self, wiki_page: Page) -> None:
        """Given: 검색 탭 + 검색 mock(2건)
        When:  검색어 입력 → 결과 렌더
        Then:  .wiki-result-card 가 정확히 2 개 (snippet-only 트리 → 카드 승격).

        근거: acceptance [behavior] "snippet 을 카드 레이아웃으로 승격",
              목업 §3 ".wiki-result-card (button, data-path/data-type)".
        """
        _switch_to_search_tab(wiki_page)
        cards = _result_cards(wiki_page)
        expect(cards).to_have_count(2)
        # 카드는 클릭 가능한 버튼 + data-path 보유(트리 미리보기 동작 유지).
        first = cards.first
        assert first.get_attribute("data-path"), "결과 카드에 data-path 필요(미리보기 위임)"

    def test_S2_score가_카드에_표시된다(self, wiki_page: Page) -> None:
        """Given: 검색 결과(score 0.92, 0.78)
        When:  첫 카드의 score 요소 조회
        Then:  '.wiki-result-card__score' 에 score 수치가 표시.

        근거: acceptance [behavior] "score(수치/배지) 표시",
              search_meta_status "(1) score → 정렬/배지 시각화".
        """
        _switch_to_search_tab(wiki_page)
        first_card = _result_cards(wiki_page).first
        score_el = first_card.locator(".wiki-result-card__score")
        assert score_el.count() >= 1, "결과 카드에 .wiki-result-card__score 요소 필요"
        score_text = score_el.first.text_content() or ""
        # 0.92 또는 92 등 — 숫자가 표시되어야 함.
        assert re.search(r"0?\.?9\d", score_text) or "92" in score_text, (
            f"첫 카드 score 에 수치(0.92 계열)가 표시되어야 함 (got {score_text!r})"
        )

    def test_S3_snippet이_카드에_표시된다(self, wiki_page: Page) -> None:
        """Given: 검색 결과(snippet 포함)
        When:  카드 텍스트 조회
        Then:  snippet 본문이 표시(기존 snippet 표시 유지).

        근거: acceptance [behavior] "snippet 표시", 목업 §3 ".wiki-result-card__snippet".
        """
        _switch_to_search_tab(wiki_page)
        cards_text = _result_cards(wiki_page).first.text_content() or ""
        assert "런칭에 합의했고" in cards_text or "마케팅" in cards_text, (
            f"첫 카드에 snippet 본문 표시 필요 (text={cards_text!r})"
        )

    def test_S4_status_배지가_카드에_표시된다(self, wiki_page: Page) -> None:
        """Given: 검색 결과[0].metadata.status="decided"
        When:  첫 카드의 .wiki-status-badge 조회
        Then:  status 배지가 '확정' 라벨로 표시.

        근거: acceptance [behavior] "status 배지 표시 + 사람이 읽는 라벨로 매핑",
              목업 §3 status 매핑 "decided → 확정".
        """
        _switch_to_search_tab(wiki_page)
        first_card = _result_cards(wiki_page).first
        badge = first_card.locator(".wiki-status-badge")
        assert badge.count() >= 1, "status 있는 결과 카드에 .wiki-status-badge 필요"
        badge_text = badge.first.text_content() or ""
        assert "확정" in badge_text, (
            f"metadata.status='decided' → '확정' 라벨 매핑 필요 (got {badge_text!r})"
        )

    def test_S5_status_부재시_배지를_생략한다(self, wiki_page: Page) -> None:
        """Given: 검색 결과[1].metadata 에 status 없음(주제 페이지)
        When:  두 번째 카드의 .wiki-status-badge 조회
        Then:  status 배지가 생략됨(카운트 0).

        근거: open_questions 응답 — "status 없으면 배지 생략"(목업 §3),
              design.md §7 "배지 남발 금지".
        """
        _switch_to_search_tab(wiki_page)
        cards = _result_cards(wiki_page)
        expect(cards).to_have_count(2)
        second_card = cards.nth(1)
        # status 가 없는 결과(topics)는 배지를 그리지 않는다.
        badge = second_card.locator(".wiki-status-badge")
        assert badge.count() == 0, (
            f"status 부재 결과는 배지를 생략해야 함 (got count={badge.count()})"
        )

    def test_S6_citations가_카드에_deep_link로_표시된다(self, wiki_page: Page) -> None:
        """Given: 검색 결과[0].citations(2개)
        When:  첫 카드의 .wiki-citation anchor 조회
        Then:  인용 anchor 가 2 개 + href 가 /app/viewer/{id} 를 포함.

        근거: acceptance [behavior] "citations 표시", search_meta_status
              "(2) citations → renderMarkdownWithCitations 동일 패턴 deep link".
        """
        _switch_to_search_tab(wiki_page)
        first_card = _result_cards(wiki_page).first
        citations = first_card.locator(".wiki-citation")
        assert citations.count() == 2, (
            f"첫 카드 citations 2 개 표시 필요 (got {citations.count()})"
        )
        href = citations.first.get_attribute("href") or ""
        assert f"/app/viewer/{_MOCK_MEETING_ID}" in href, (
            f"인용 anchor href 가 deep link 여야 함 (got {href!r})"
        )

    def test_S7_검색카드_인용_클릭은_뷰어로_이동한다(self, wiki_page: Page) -> None:
        """Given: 첫 카드의 인용([meeting:{id}@00:23:45] = 1425초)
        When:  인용 anchor 클릭
        Then:  /app/viewer/{id}?t=1425 로 라우팅.

        근거: acceptance [behavior] "검색 결과 카드 인용 클릭 → /app/viewer/{id}?t={초}".
        """
        _switch_to_search_tab(wiki_page)
        first_card = _result_cards(wiki_page).first
        first_card.locator(".wiki-citation").first.click()
        wiki_page.wait_for_url(lambda url: "/app/viewer/" in url, timeout=5000)
        url = wiki_page.url
        assert f"/app/viewer/{_MOCK_MEETING_ID}" in url, (
            f"검색 카드 인용 클릭 시 뷰어로 이동해야 함 (url={url})"
        )
        assert "t=1425" in url, f"인용 timestamp 가 t=1425 로 전달되어야 함 (url={url})"


# ============================================================================
# 그룹 R — 회귀 방지(기존 WikiView 동작 + destroy 정리)
# ============================================================================


class TestNoRegression:
    """기존 검색/필터/상세/Health 모달 + destroy 정리가 회귀 없이 유지."""

    def test_R1_Health_배지가_여전히_존재한다(self, wiki_page: Page) -> None:
        """Given: /app/wiki 진입
        When:  기존 Health 배지 조회
        Then:  #wikiHealthBadge 가 여전히 존재(탭 추가가 헤더를 깨지 않음).

        근거: acceptance [behavior] "health 모달 등 기존 WikiView 동작 회귀 없이 유지".
        """
        expect(wiki_page.locator("#wikiHealthBadge")).to_have_count(1)

    def test_R2_검색탭에_기존_트리와_미리보기가_유지된다(self, wiki_page: Page) -> None:
        """Given: 검색 탭 활성
        When:  기존 트리/미리보기 컨테이너 조회
        Then:  #wikiTree(nav) + #wikiPreview(section) 가 검색 패널 안에 존재.

        근거: 목업 §0 "검색 활성 시 기존 헤더/필터/트리/미리보기 그대로(회귀 0)".
        """
        _search_tab(wiki_page).click()
        wiki_page.wait_for_timeout(150)
        assert wiki_page.locator("#wikiTree").count() == 1, "검색 탭에 기존 위키 트리 유지 필요"
        assert wiki_page.locator("#wikiPreview").count() == 1, "검색 탭에 기존 미리보기 유지 필요"

    def test_R3_Health_모달이_여전히_열린다(self, wiki_page: Page) -> None:
        """Given: /app/wiki
        When:  Health 배지 클릭
        Then:  role=dialog 모달이 열림(기존 동작 보존).

        근거: acceptance [behavior] "health 모달 동작 회귀 없이 유지".
        """
        wiki_page.locator("#wikiHealthBadge").click()
        wiki_page.wait_for_timeout(150)
        modal = wiki_page.locator(".wiki-health-modal[role='dialog']")
        expect(modal).to_be_visible()

    def test_R4_뷰_이탈_시_컴포넌트가_정리된다(self, wiki_page: Page) -> None:
        """Given: /app/wiki (현황 컴포넌트 마운트 + digest fetch 진행 가능)
        When:  다른 뷰(/app)로 라우팅(destroy 호출)
        Then:  컴포넌트 루트가 DOM 에서 제거되고 콘솔 에러 없음.

        근거: acceptance [behavior] "destroy 시 리스너·타이머·fetch 전부 정리".

        Red 의도성:
            미구현 시 컴포넌트 루트가 애초에 없어 `count()==0` 만 검증하면
            거짓 PASS 가 발생한다. 이를 막기 위해 라우팅 이전에 컴포넌트
            루트가 정확히 1 개 마운트되어 있었음을 먼저 강제 검증한다 —
            구현 전에는 이 사전 검증에서 깨끗한 FAIL.
        """
        errors: list[str] = []
        wiki_page.on("pageerror", lambda exc: errors.append(str(exc)))

        # 사전 검증 — 라우팅 전에 현황 컴포넌트가 마운트되어 있어야 함.
        assert _component_root(wiki_page).count() == 1, (
            "뷰 이탈 검증 전, wiki-overview-search 컴포넌트가 마운트되어 있어야 함 "
            "(미구현 시 0 → 거짓 PASS 방지)"
        )

        # 다른 뷰로 라우팅 — WikiView.destroy() 가 호출되어야 함.
        wiki_page.evaluate("() => window.SPA.Router.navigate('/app')")
        wiki_page.wait_for_timeout(400)
        # 컴포넌트 루트가 사라졌는지(뷰 교체 완료).
        assert _component_root(wiki_page).count() == 0, (
            "뷰 이탈 후 wiki-overview-search 루트가 제거되어야 함"
        )
        assert not errors, f"뷰 이탈 시 콘솔 에러가 없어야 함 (errors={errors!r})"
