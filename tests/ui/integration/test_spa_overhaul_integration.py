"""SPA 통합 갭 검증 — 7 UI/UX Overhaul 컴포넌트가 실제 SPA 에서 동작하는지 확인.

본 테스트는 fixture-as-source-of-truth 패턴의 한계를 메우는 SPA-level e2e 다.
실제 `ui/web/index.html` 을 정적 서버로 띄우고 `spa.js` 를 그대로 로드한 뒤
`page.route()` 로 `/api/*` 호출을 mock 하여 SPA 의 렌더링 결과를 검증한다.

검증 대상 7 컴포넌트:
    T-101 empty-state (3 위치: meeting-list, search, chat)
    T-102 dark-mode-tones (보강 토큰 적용)
    T-103 skeleton-shimmer (4 위치)
    T-201 focus-visible (Tab focus 링)
    T-202 command-palette (⌘K dialog)
    T-301 aria-sync (nav aria-current 동기화)
    T-302 mobile-responsive (햄버거 + drawer)

SKIP 또는 FAIL → SPA 통합 갭. fixture 가 PASS 했더라도 실제 SPA 미적용일 수 있음.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from playwright.sync_api import Browser, Page, Route

pytestmark = [pytest.mark.ui]


def _mock_api(route: Route) -> None:
    """SPA 의 fetch /api/* 호출을 빈 응답으로 mock."""
    url = route.request.url
    if "/api/meetings" in url:
        # 빈 회의 목록 → empty-state 트리거
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"meetings": []}',
        )
    elif "/api/search" in url:
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"results": []}',
        )
    elif "/api/chat" in url or "/api/sessions" in url:
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"messages": [], "sessions": []}',
        )
    elif "/api/health" in url or "/api/status" in url:
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"status": "ok"}',
        )
    elif "/api/" in url:
        # 그 외 모든 API → 빈 JSON
        route.fulfill(
            status=200,
            content_type="application/json",
            body="{}",
        )
    else:
        route.continue_()


@contextmanager
def _spa_page(
    browser: Browser,
    base_url: str,
    viewport: dict,
    scheme: str = "light",
    path: str = "/",
) -> Iterator[Page]:
    """SPA 페이지 컨텍스트 — viewport, color-scheme, API mock 자동 설정."""
    ctx = browser.new_context(
        viewport=viewport,
        device_scale_factor=2,
        color_scheme=scheme,
    )
    page = ctx.new_page()
    page.route("**/api/**", _mock_api)
    try:
        page.goto(base_url + path)
        page.wait_for_load_state("networkidle")
        yield page
    finally:
        ctx.close()


# ============================================================
# T-101 empty-state — 3 위치
# ============================================================


def test_t101_meeting_list_empty_state(
    browser: Browser, spa_static_server: str
) -> None:
    """회의 0개 → '아직 회의가 없어요' empty-state 노출."""
    with _spa_page(browser, spa_static_server, {"width": 1024, "height": 768}) as page:
        # SPA 가 listContent 에 회의 목록 fetch 후 empty 렌더링
        page.wait_for_timeout(800)
        empty = page.locator('[data-empty="meeting-list"]')
        if empty.count() == 0:
            pytest.fail(
                "회의 목록 empty-state 미렌더 — [data-empty='meeting-list'] 부재"
            )
        title = empty.locator(".empty-state-title").first
        assert title.inner_text().strip() == "아직 회의가 없어요", (
            f"empty-state 제목 불일치: {title.inner_text()!r}"
        )


def test_t101_search_empty_markup_present(
    browser: Browser, spa_static_server: str
) -> None:
    """검색 라우트 진입 시 검색 empty 마크업 존재 (미표시 상태도 OK)."""
    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/search",
    ) as page:
        page.wait_for_timeout(800)
        empty = page.locator('[data-empty="search"], #searchEmpty')
        if empty.count() == 0:
            pytest.fail(
                "검색 empty-state 마크업 미존재 — [data-empty='search'] 또는 #searchEmpty"
            )
        # 텍스트 확인 — 표시되지 않더라도 마크업 안에 텍스트는 있어야 함
        title = empty.locator(".empty-state-title").first
        assert "검색 결과가 없어요" in title.inner_text(), (
            f"검색 empty-state 제목 불일치: {title.inner_text()!r}"
        )


def test_t101_chat_empty_state(browser: Browser, spa_static_server: str) -> None:
    """채팅 라우트 → welcome empty-state 노출."""
    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/chat",
    ) as page:
        page.wait_for_timeout(800)
        empty = page.locator('[data-empty="chat"]')
        if empty.count() == 0:
            pytest.fail("채팅 empty-state 미렌더 — [data-empty='chat'] 부재")
        title = empty.locator(".empty-state-title").first
        assert title.count() > 0, "채팅 empty-state-title 부재"


# ============================================================
# T-102 dark-mode-tones
# ============================================================


def test_t102_dark_text_secondary_token(
    browser: Browser, spa_static_server: str
) -> None:
    """다크 모드에서 --text-secondary 가 보강된 #98989D."""
    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        scheme="dark",
    ) as page:
        text_sec = page.evaluate(
            "() => getComputedStyle(document.documentElement)"
            ".getPropertyValue('--text-secondary').trim().toLowerCase()"
        )
        text_muted = page.evaluate(
            "() => getComputedStyle(document.documentElement)"
            ".getPropertyValue('--text-muted').trim().toLowerCase()"
        )
        # 다크 모드 적용 시 #98989d / #8e8e93 (보강 토큰)
        assert text_sec == "#98989d", (
            f"--text-secondary 다크 토큰 불일치: {text_sec!r} (#98989d 기대)"
        )
        assert text_muted == "#8e8e93", (
            f"--text-muted 다크 토큰 불일치: {text_muted!r} (#8e8e93 기대)"
        )


# ============================================================
# T-103 skeleton-shimmer
# ============================================================


def test_t103_skeleton_card_css_defined(
    browser: Browser, spa_static_server: str
) -> None:
    """style.css 에 .skeleton-card 정의됨."""
    with _spa_page(browser, spa_static_server, {"width": 1024, "height": 768}) as page:
        has_skeleton = page.evaluate(
            "() => Array.from(document.styleSheets).some(s => {"
            "  try { return Array.from(s.cssRules).some(r => "
            "    r.cssText && r.cssText.includes('skeleton-card')); }"
            "  catch(e) { return false; }"
            "})"
        )
        assert has_skeleton, ".skeleton-card CSS 정의 부재"


def test_t103_search_loading_skeleton_markup(
    browser: Browser, spa_static_server: str
) -> None:
    """검색 라우트의 #searchLoading 이 .skeleton-container + 3 .skeleton-card."""
    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/search",
    ) as page:
        page.wait_for_timeout(800)
        loading = page.locator("#searchLoading")
        if loading.count() == 0:
            pytest.fail("#searchLoading 마크업 부재 — search skeleton 미적용")
        klass = loading.first.get_attribute("class") or ""
        assert "skeleton-container" in klass, (
            f"#searchLoading 의 클래스에 'skeleton-container' 누락: {klass!r}"
        )
        cards = loading.first.locator(".skeleton-card")
        assert cards.count() == 3, (
            f"#searchLoading skeleton-card 개수 비정상: {cards.count()} (3 기대)"
        )


def test_t103_viewer_skeleton_markup(
    browser: Browser, spa_static_server: str
) -> None:
    """viewer 라우트 → #viewerTranscriptLoading + #viewerSummaryLoading 마크업."""
    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/viewer/test-id",
    ) as page:
        page.wait_for_timeout(800)
        # ViewerView 의 transcript / summary skeleton
        transcript_loading = page.locator("#viewerTranscriptLoading")
        summary_loading = page.locator("#viewerSummaryLoading")
        if transcript_loading.count() == 0 or summary_loading.count() == 0:
            pytest.fail(
                "viewer skeleton 마크업 부재 "
                f"(transcript={transcript_loading.count()}, "
                f"summary={summary_loading.count()})"
            )
        # 두 컨테이너 모두 skeleton-container 클래스 보유
        for sel in ("#viewerTranscriptLoading", "#viewerSummaryLoading"):
            klass = page.locator(sel).first.get_attribute("class") or ""
            assert "skeleton-container" in klass, (
                f"{sel} 클래스에 'skeleton-container' 누락: {klass!r}"
            )


# ============================================================
# T-201 focus-visible
# ============================================================


def test_t201_focus_ring_token_defined(
    browser: Browser, spa_static_server: str
) -> None:
    """:root 에 --focus-ring 토큰 정의."""
    with _spa_page(browser, spa_static_server, {"width": 1024, "height": 768}) as page:
        focus_ring = page.evaluate(
            "() => getComputedStyle(document.documentElement)"
            ".getPropertyValue('--focus-ring').trim()"
        )
        assert focus_ring, "--focus-ring 토큰 미정의"
        # accent 컬러 또는 hex 포함 (예: 'var(--accent)' or '0 0 0 3px ...')
        assert "(" in focus_ring or "#" in focus_ring or "rgb" in focus_ring, (
            f"--focus-ring 값 비정상: {focus_ring!r}"
        )


def test_t201_first_interactive_focus_visible(
    browser: Browser, spa_static_server: str
) -> None:
    """Tab → 첫 인터랙티브 요소에 box-shadow (focus ring) 적용."""
    with _spa_page(browser, spa_static_server, {"width": 1024, "height": 768}) as page:
        page.wait_for_timeout(500)
        # Tab 으로 첫 활성 요소 도달
        page.keyboard.press("Tab")
        page.wait_for_timeout(100)

        info = page.evaluate(
            "() => { const el = document.activeElement; "
            "if (!el || el === document.body) return null; "
            "return { tag: el.tagName, id: el.id, "
            "boxShadow: getComputedStyle(el).boxShadow }; }"
        )
        if info is None:
            pytest.skip("Tab 으로 활성 요소 도달 못 함 (focusable 요소 부재)")
        # box-shadow 가 'none' 이 아니면 focus-visible 적용
        assert info["boxShadow"] != "none", (
            f"focus 요소({info['tag']}#{info['id']})에 box-shadow none — "
            "focus-visible 미적용"
        )


# ============================================================
# T-202 command-palette
# ============================================================


def test_t202_palette_dialog_in_dom_after_init(
    browser: Browser, spa_static_server: str
) -> None:
    """SPA init → dialog.command-palette 요소 DOM 존재."""
    with _spa_page(browser, spa_static_server, {"width": 1024, "height": 768}) as page:
        page.wait_for_timeout(800)  # CommandPalette 초기화 대기
        # init 시점에 DOM 에 미존재할 수도 있음 — open() 시 lazy create 가능
        # ⌘K 한 번 눌러서 lazy init 트리거
        page.keyboard.press("Meta+k")
        page.wait_for_timeout(300)
        dialog = page.locator("dialog.command-palette")
        if dialog.count() == 0:
            pytest.fail(
                "dialog.command-palette 미존재 — Command Palette init 실패"
            )
        assert dialog.count() == 1, (
            f"command-palette dialog 중복: {dialog.count()}"
        )


def test_t202_cmd_k_opens_palette(
    browser: Browser, spa_static_server: str
) -> None:
    """⌘K (Meta+K) 또는 Ctrl+K → 팔레트 open 상태."""
    with _spa_page(browser, spa_static_server, {"width": 1024, "height": 768}) as page:
        page.wait_for_timeout(800)
        page.keyboard.press("Meta+k")
        page.wait_for_timeout(300)
        is_open = page.evaluate(
            "() => { const d = document.querySelector('dialog.command-palette'); "
            "return d ? d.hasAttribute('open') : false; }"
        )
        if not is_open:
            # Mac 이외 환경 가정 — Ctrl+K 시도
            page.keyboard.press("Control+k")
            page.wait_for_timeout(300)
            is_open = page.evaluate(
                "() => { const d = document.querySelector('dialog.command-palette'); "
                "return d ? d.hasAttribute('open') : false; }"
            )
        assert is_open, "⌘K / Ctrl+K 단축키로 팔레트 open 실패"


# ============================================================
# T-301 aria-sync
# ============================================================


def test_t301_active_nav_has_aria_current(
    browser: Browser, spa_static_server: str
) -> None:
    """초기 라우트(/app)에서 활성 nav-btn 에 aria-current='page'."""
    with _spa_page(browser, spa_static_server, {"width": 1024, "height": 768}) as page:
        page.wait_for_timeout(500)
        active_btn = page.locator("#nav-bar .nav-btn.active").first
        if active_btn.count() == 0:
            pytest.fail("#nav-bar .nav-btn.active 미존재")
        aria_current = active_btn.get_attribute("aria-current")
        assert aria_current == "page", (
            f"활성 nav-btn 에 aria-current='page' 누락 (실제: {aria_current!r})"
        )


def test_t301_inactive_nav_no_aria_current(
    browser: Browser, spa_static_server: str
) -> None:
    """비활성 nav-btn 에 aria-current 속성 자체 없음 (removeAttribute 패턴)."""
    with _spa_page(browser, spa_static_server, {"width": 1024, "height": 768}) as page:
        page.wait_for_timeout(500)
        # JS evaluate 로 모든 비활성 버튼의 aria-current 속성 존재 여부 확인
        result = page.evaluate(
            "() => { const btns = document.querySelectorAll("
            "  '#nav-bar .nav-btn:not(.active)'); "
            "return Array.from(btns).map(b => ({"
            "  id: b.id, "
            "  hasAttr: b.hasAttribute('aria-current'), "
            "  value: b.getAttribute('aria-current') "
            "})); }"
        )
        if not result:
            pytest.skip("비활성 nav-btn 없음")
        violations = [r for r in result if r["hasAttr"]]
        assert not violations, (
            f"비활성 nav-btn 에 aria-current 잔존 (removeAttribute 위반): "
            f"{violations}"
        )


def test_t301_route_change_updates_aria_current(
    browser: Browser, spa_static_server: str
) -> None:
    """라우트 변경 후 활성 nav-btn 의 aria-current 가 동기화됨."""
    with _spa_page(browser, spa_static_server, {"width": 1024, "height": 768}) as page:
        page.wait_for_timeout(500)
        # /app/search 로 이동
        page.click("#navSearch")
        page.wait_for_timeout(500)
        # 새 활성 버튼은 #navSearch
        result = page.evaluate(
            "() => ({"
            "  search: document.querySelector('#navSearch')?.getAttribute('aria-current'), "
            "  home: document.querySelector('#navHome')?.getAttribute('aria-current'), "
            "  searchActive: document.querySelector('#navSearch')?.classList.contains('active'), "
            "  homeActive: document.querySelector('#navHome')?.classList.contains('active') "
            "})"
        )
        assert result["search"] == "page", (
            f"#navSearch 에 aria-current='page' 누락 (실제: {result['search']!r})"
        )
        assert result["home"] is None, (
            f"#navHome 에 aria-current 잔존 (실제: {result['home']!r})"
        )


# ============================================================
# T-302 mobile-responsive
# ============================================================


def test_t302_hamburger_visible_on_mobile(
    browser: Browser, spa_static_server: str
) -> None:
    """375px viewport → 햄버거 버튼 보임."""
    with _spa_page(browser, spa_static_server, {"width": 375, "height": 667}) as page:
        page.wait_for_timeout(500)
        toggle = page.locator("#mobile-menu-toggle")
        assert toggle.count() == 1, "#mobile-menu-toggle 마크업 부재"
        # CSS display:none 인지 확인
        display = page.evaluate(
            "() => getComputedStyle(document.querySelector('#mobile-menu-toggle')).display"
        )
        assert display != "none", (
            f"햄버거 버튼이 모바일에서 display={display!r} (미노출)"
        )


def test_t302_hamburger_click_opens_drawer(
    browser: Browser, spa_static_server: str
) -> None:
    """햄버거 클릭 → 사이드바 .is-open + aria-expanded='true'."""
    with _spa_page(browser, spa_static_server, {"width": 375, "height": 667}) as page:
        page.wait_for_timeout(500)
        toggle = page.locator("#mobile-menu-toggle")
        if toggle.count() == 0:
            pytest.fail("#mobile-menu-toggle 부재")
        toggle.click()
        page.wait_for_timeout(400)
        result = page.evaluate(
            "() => ({"
            "  ariaExpanded: document.querySelector('#mobile-menu-toggle')"
            "    ?.getAttribute('aria-expanded'), "
            "  panelOpen: document.querySelector('#list-panel')"
            "    ?.classList.contains('is-open') "
            "})"
        )
        assert result["ariaExpanded"] == "true", (
            f"햄버거 클릭 후 aria-expanded={result['ariaExpanded']!r} (true 기대)"
        )
        assert result["panelOpen"], (
            "햄버거 클릭 후 #list-panel.is-open 미적용"
        )


def test_t302_escape_closes_drawer(
    browser: Browser, spa_static_server: str
) -> None:
    """ESC → drawer 닫힘."""
    with _spa_page(browser, spa_static_server, {"width": 375, "height": 667}) as page:
        page.wait_for_timeout(500)
        toggle = page.locator("#mobile-menu-toggle")
        if toggle.count() == 0:
            pytest.fail("#mobile-menu-toggle 부재")
        toggle.click()
        page.wait_for_timeout(300)
        # 열린 상태 확인
        is_open = page.evaluate(
            "() => document.querySelector('#mobile-menu-toggle')"
            ".getAttribute('aria-expanded') === 'true'"
        )
        if not is_open:
            pytest.skip("drawer 가 열리지 않아 ESC 테스트 불가")
        # ESC
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        result = page.evaluate(
            "() => ({"
            "  ariaExpanded: document.querySelector('#mobile-menu-toggle')"
            "    ?.getAttribute('aria-expanded'), "
            "  panelOpen: document.querySelector('#list-panel')"
            "    ?.classList.contains('is-open') "
            "})"
        )
        assert result["ariaExpanded"] == "false", (
            f"ESC 후 aria-expanded={result['ariaExpanded']!r} (false 기대)"
        )
        assert not result["panelOpen"], (
            "ESC 후 #list-panel.is-open 잔존"
        )
