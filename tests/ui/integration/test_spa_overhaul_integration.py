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

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
from playwright.sync_api import Browser, Page, Route

pytestmark = [pytest.mark.ui]


def _mock_api(route: Route) -> None:
    """SPA 의 fetch /api/* 호출을 빈 응답으로 mock."""
    url = route.request.url
    if "/api/wiki/search" in url:
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"results": []}',
        )
    elif "/api/wiki/health" in url:
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"status": "no_lint_yet", "raw_markdown": null}',
        )
    elif "/api/wiki/pages/" in url:
        route.fulfill(
            status=404,
            content_type="application/json",
            body='{"detail": "not found"}',
        )
    elif "/api/wiki/pages" in url:
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"pages": []}',
        )
    elif "/api/ab-tests/" in url:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_ab_test_detail()),
        )
    elif "/api/ab-tests" in url:
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"tests": []}',
        )
    elif "/api/meetings" in url:
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


def _ab_test_detail(status: str = "completed") -> dict:
    """A/B 결과 뷰 테스트용 최소 응답."""
    return {
        "metadata": {
            "test_id": "ab_20260505-120000_abcdef12",
            "test_type": "llm",
            "status": status,
            "source_meeting_id": "meeting-a",
            "started_at": "2026-05-05T12:00:00",
            "variant_a": {"label": "모델 A", "model_id": "model-a"},
            "variant_b": {"label": "모델 B", "model_id": "model-b"},
            "current_variant": "A",
            "current_step": "correct",
            "progress_pct": 35,
        },
        "variant_a": {
            "correct": {
                "utterances": [{"start": 0, "speaker": "SPEAKER_00", "text": "안녕하세요"}]
            },
            "metrics": {
                "elapsed_seconds": {"total": 10},
                "char_count": {"correct": 5},
                "forbidden_patterns": {"total": 0},
            },
        },
        "variant_b": {
            "correct": {
                "utterances": [{"start": 0, "speaker": "SPEAKER_00", "text": "안녕합니다"}]
            },
            "metrics": {
                "elapsed_seconds": {"total": 12},
                "char_count": {"correct": 5},
                "forbidden_patterns": {"total": 1},
            },
        },
    }


@contextmanager
def _spa_page(
    browser: Browser,
    base_url: str,
    viewport: dict,
    scheme: str = "light",
    path: str = "/",
    api_handler: Callable[[Route], None] | None = None,
) -> Iterator[Page]:
    """SPA 페이지 컨텍스트 — viewport, color-scheme, API mock 자동 설정."""
    ctx = browser.new_context(
        viewport=viewport,
        device_scale_factor=2,
        color_scheme=scheme,
    )
    page = ctx.new_page()
    page.route("**/api/**", api_handler or _mock_api)
    try:
        page.goto(base_url + path)
        page.wait_for_load_state("networkidle")
        yield page
    finally:
        ctx.close()


# ============================================================
# T-101 empty-state — 3 위치
# ============================================================


def test_t101_meeting_list_empty_state(browser: Browser, spa_static_server: str) -> None:
    """회의 0개 → '아직 회의가 없어요' empty-state 노출."""
    with _spa_page(browser, spa_static_server, {"width": 1024, "height": 768}) as page:
        # SPA 가 listContent 에 회의 목록 fetch 후 empty 렌더링
        page.wait_for_timeout(800)
        empty = page.locator('[data-empty="meeting-list"]')
        if empty.count() == 0:
            pytest.fail("회의 목록 empty-state 미렌더 — [data-empty='meeting-list'] 부재")
        title = empty.locator(".empty-state-title").first
        assert title.inner_text().strip() == "아직 회의가 없어요", (
            f"empty-state 제목 불일치: {title.inner_text()!r}"
        )


def test_home_route_stats_actions_dropdowns_and_public_boundary(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """EmptyView 모듈 분리 후 홈 stats/action/dropdown 계약을 검증."""
    folder_posts: list[str] = []
    batch_payloads: list[dict] = []

    def home_api(route: Route) -> None:
        url = route.request.url
        if "/api/dashboard/stats" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "this_week_meetings": 2,
                        "total_meetings": 9,
                        "queue_pending": 3,
                        "untranscribed_recordings": 4,
                        "active_processing": 1,
                    }
                ),
            )
            return
        if "/api/system/open-audio-folder" in url:
            folder_posts.append(url)
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"opened": true, "path": "/tmp/audio_input"}',
            )
            return
        if "/api/meetings/batch" in url:
            batch_payloads.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"queued": 2, "skipped": 1}',
            )
            return
        _mock_api(route)

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app",
        api_handler=home_api,
    ) as page:
        page.wait_for_selector(".home-view", state="attached")
        page.wait_for_function(
            "() => document.querySelector('#homeStatThisWeek').textContent === '2'"
        )

        assert page.evaluate(
            "() => Boolean(window.SPA && window.SPA.EmptyView && window.MeetingEmptyView)"
        )
        assert page.locator(".home-stats").count() == 1
        assert page.locator(".empty-view").count() == 1
        assert page.locator("#homeStatTotal").inner_text() == "9"
        assert page.locator("#homeStatQueue").inner_text() == "3"
        assert page.locator("#homeStatQueueSub").inner_text() == "미전사 4"
        assert page.locator("#homeStatActive").inner_text() == "1"
        assert (
            page.evaluate(
                "() => document.querySelector('#homeStatQueue').parentElement.getAttribute('aria-label')"
            )
            == "처리 대기 3개, 미전사 녹음 4개"
        )

        page.locator("#homeActionImport").click()
        assert "hidden" not in (page.locator("#importModal").get_attribute("class") or "")
        assert page.evaluate("() => document.activeElement.id") == "importDropzone"
        page.evaluate("() => document.querySelector('#importModal').classList.add('hidden')")

        page.locator("#homeActionOpenFolder").click()
        page.wait_for_function(
            "() => document.querySelector('#homeStatusMessage').textContent.includes('/tmp/audio_input')"
        )
        assert folder_posts
        assert "전사 폴더 열기" in page.locator("#homeActionOpenFolder").inner_text()

        page.locator(".home-action-btn--dropdown[data-dropdown='all-bulk']").click()
        page.locator(".home-action-dropdown [role='menuitemradio'][data-option='both']").click()
        page.wait_for_function(
            "() => document.querySelector('#homeStatusMessage').textContent.includes('2건 처리')"
        )

        page.evaluate("() => window.SPA.Router.navigate('/app/chat')")
        page.wait_for_url("**/app/chat")
        page.evaluate("() => window.SPA.Router.navigate('/app')")
        page.wait_for_selector(".home-view", state="attached")
        page.locator(".home-action-btn--dropdown[data-dropdown='recent-24h']").click()
        page.locator(
            ".home-action-dropdown [role='menuitemradio'][data-option='transcribe']"
        ).click()
        page.wait_for_function(
            "() => document.querySelector('#homeStatusMessage').textContent.includes('2건 처리')"
        )

    assert batch_payloads == [
        {"action": "full", "scope": "all"},
        {"action": "transcribe", "scope": "recent", "hours": 24},
    ]


def test_home_stale_async_does_not_mutate_after_destroy(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """stats/open-folder 지연 응답이 destroy 이후 현재 route 를 오염시키지 않는다."""
    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app",
    ) as page:
        page.wait_for_selector(".home-view", state="attached")
        page.evaluate(
            """
            () => {
                window.__homeStatsRequests = [];
                window.__homeFolderRequests = [];
                window.MeetingApp.apiRequest = function(endpoint) {
                    if (endpoint === '/dashboard/stats') {
                        return new Promise((resolve) => {
                            window.__homeStatsRequests.push(resolve);
                        });
                    }
                    return Promise.resolve({});
                };
                window.MeetingApp.apiPost = function(endpoint, body) {
                    if (endpoint === '/system/open-audio-folder') {
                        return new Promise((resolve) => {
                            window.__homeFolderRequests.push(resolve);
                        });
                    }
                    return Promise.resolve({});
                };
            }
            """
        )
        page.evaluate("() => document.dispatchEvent(new Event('recap:dashboard-refresh'))")
        page.wait_for_function("() => window.__homeStatsRequests.length === 1")
        page.locator("#homeActionOpenFolder").click()
        page.wait_for_function("() => window.__homeFolderRequests.length === 1")

        page.evaluate("() => window.SPA.Router.navigate('/app/chat')")
        page.wait_for_url("**/app/chat")
        page.evaluate(
            """
            () => {
                window.__homeStatsRequests[0]({
                    this_week_meetings: 99,
                    total_meetings: 99,
                    queue_pending: 99,
                    untranscribed_recordings: 99,
                    active_processing: 99
                });
                window.__homeFolderRequests[0]({
                    opened: true,
                    path: '/tmp/late-audio'
                });
            }
            """
        )
        page.wait_for_timeout(150)

        assert page.locator(".home-view").count() == 0
        assert page.locator("#homeStatusMessage").count() == 0
        assert "/tmp/late-audio" not in page.locator("body").inner_text()


def test_global_resource_bar_renders_resources_and_ignores_failures(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """전역 리소스 바 factory 분리 후 렌더링/실패 무시 계약을 검증."""
    fail_resources = {"enabled": False}

    def resource_api(route: Route) -> None:
        url = route.request.url
        if "/api/system/resources" in url:
            if fail_resources["enabled"]:
                route.fulfill(
                    status=500,
                    content_type="application/json",
                    body='{"detail": "boom"}',
                )
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "ram_used_gb": 8,
                        "ram_total_gb": 10,
                        "cpu_percent": 91,
                        "loaded_model": "gemma-test",
                    }
                ),
            )
            return
        _mock_api(route)

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app",
        api_handler=resource_api,
    ) as page:
        page.wait_for_selector("#globalResourceBar", state="attached")
        page.wait_for_function(
            "() => document.querySelector('#grb-ram-text').textContent === '8/10G'"
        )

        assert page.evaluate("() => Boolean(window.MeetingGlobalResourceBar)")
        assert page.locator("#globalResourceBar").get_attribute("role") == "status"
        assert page.locator("#globalResourceBar").get_attribute("aria-live") == "polite"
        assert page.locator("#grb-ram-text").inner_text() == "8/10G"
        assert "warning" in (page.locator("#grb-ram-bar").get_attribute("class") or "")
        assert page.locator("#grb-cpu-text").inner_text() == "91%"
        assert "danger" in (page.locator("#grb-cpu-bar").get_attribute("class") or "")
        assert page.locator("#grb-model-text").inner_text() == "gemma-test"

        fail_resources["enabled"] = True
        page.evaluate(
            """
            () => {
                const bar = window.MeetingGlobalResourceBar.create({
                    App: window.MeetingApp,
                    intervalMs: 999999
                });
                bar.start();
            }
            """
        )
        page.wait_for_timeout(150)
        assert "visible" not in (page.locator("#errorBanner").get_attribute("class") or "")


def test_global_resource_bar_singleton_and_stop_guard(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """중복 start 와 stop 이후 늦은 응답의 DOM mutation 방지를 검증."""
    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app",
    ) as page:
        page.wait_for_selector("#globalResourceBar", state="attached")
        result = page.evaluate(
            """
            async () => {
                const pending = [];
                const activeTimers = new Set();
                let nextTimerId = 1;
                const bar = window.MeetingGlobalResourceBar.create({
                    App: {
                        apiRequest(endpoint) {
                            return new Promise((resolve) => {
                                pending.push({ endpoint, resolve });
                            });
                        }
                    },
                    intervalMs: 10,
                    setInterval(fn, ms) {
                        const id = nextTimerId++;
                        activeTimers.add(id);
                        return id;
                    },
                    clearInterval(id) {
                        activeTimers.delete(id);
                    }
                });

                bar.start();
                bar.start();
                const domCountAfterStart = document.querySelectorAll('#globalResourceBar').length;
                const activeAfterStart = activeTimers.size;
                bar.stop();
                const activeAfterStop = activeTimers.size;
                pending[pending.length - 1].resolve({
                    ram_used_gb: 99,
                    ram_total_gb: 100,
                    cpu_percent: 99,
                    loaded_model: 'late-model'
                });
                await Promise.resolve();
                return {
                    domCountAfterStart,
                    activeAfterStart,
                    activeAfterStop,
                    ramText: document.querySelector('#grb-ram-text').textContent,
                    modelText: document.querySelector('#grb-model-text').textContent
                };
            }
            """
        )

        assert result["domCountAfterStart"] == 1
        assert result["activeAfterStart"] == 1
        assert result["activeAfterStop"] == 0
        assert result["ramText"] != "99/100G"
        assert result["modelText"] != "late-model"


def test_t101_search_empty_markup_present(browser: Browser, spa_static_server: str) -> None:
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
            pytest.fail("검색 empty-state 마크업 미존재 — [data-empty='search'] 또는 #searchEmpty")
        # 텍스트 확인 — 표시되지 않더라도 마크업 안에 텍스트는 있어야 함
        title = empty.locator(".empty-state-title").first
        assert "검색 결과가 없어요" in title.inner_text(), (
            f"검색 empty-state 제목 불일치: {title.inner_text()!r}"
        )


def test_search_route_renders_public_module_boundary(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """SearchView 모듈 분리 후 /app/search shell 과 공개 API 계약을 검증."""
    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/search",
    ) as page:
        page.wait_for_selector("#searchQuery", state="attached")

        assert page.title() == "검색 · Recap"
        assert page.locator(".search-view").count() == 1
        assert page.locator("#navSearch").get_attribute("aria-current") == "page"
        assert "active" in (page.locator("#navSearch").get_attribute("class") or "")
        assert page.evaluate("() => document.activeElement.id") == "searchQuery"
        assert page.evaluate(
            "() => Boolean(window.SPA && window.SPA.SearchView && window.MeetingSearchView)"
        )


def test_search_submit_payload_empty_state_and_error_banner(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """검색 payload, 필터 초기화, 빈 결과 문구, 503 배너 계약을 검증."""
    payloads: list[dict] = []
    respond_503 = {"enabled": False}

    def search_api(route: Route) -> None:
        url = route.request.url
        if "/api/search" in url:
            payloads.append(json.loads(route.request.post_data or "{}"))
            if respond_503["enabled"]:
                route.fulfill(
                    status=503,
                    content_type="application/json",
                    body='{"detail": "search engine unavailable"}',
                )
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"results": [], "vector_count": 0, "fts_count": 0}',
            )
            return
        _mock_api(route)

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/search",
        api_handler=search_api,
    ) as page:
        page.wait_for_selector("#searchQuery", state="attached")
        page.locator("#searchFilterDate").fill("2026-05-05")
        page.locator("#searchFilterSpeaker").fill(" SPEAKER_00 ")
        page.locator("#searchQuery").fill("  출시 일정  ")
        page.keyboard.press("Enter")
        page.wait_for_selector("#searchEmpty", state="attached")
        page.wait_for_function(
            "() => document.querySelector('#searchEmpty').style.display === 'block'"
        )

        assert payloads[0] == {
            "query": "출시 일정",
            "date_filter": "2026-05-05",
            "speaker_filter": "SPEAKER_00",
        }
        assert (
            "'출시 일정'에 대한 검색 결과가 없습니다"
            in page.locator("#searchEmptyText").inner_text()
        )
        assert "날짜/화자 필터" in page.locator("#searchEmptySub").inner_text()

        page.locator("#searchFilterClearBtn").click()
        assert page.locator("#searchFilterDate").input_value() == ""
        assert page.locator("#searchFilterSpeaker").input_value() == ""
        page.locator("#searchQuery").fill("   ")
        page.keyboard.press("Enter")
        page.wait_for_timeout(150)
        assert len(payloads) == 1

        respond_503["enabled"] = True
        page.locator("#searchQuery").fill("검색 장애")
        page.keyboard.press("Enter")
        page.wait_for_function(
            "() => document.querySelector('#errorBanner').classList.contains('visible')"
        )
        assert (
            "검색 엔진이 아직 초기화되지 않았습니다." in page.locator("#errorMessage").inner_text()
        )


def test_search_results_are_safe_and_navigate_to_viewer(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """검색 결과 렌더링, XSS 방어, 클릭/키보드 viewer 딥링크를 검증."""
    captured_payloads: list[dict] = []

    def result_api(route: Route) -> None:
        url = route.request.url
        if "/api/search" in url:
            captured_payloads.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "vector_count": 1,
                        "fts_count": 1,
                        "results": [
                            {
                                "meeting_id": "회의/알파",
                                "score": 0.98765,
                                "text": "<img src=x onerror=window.__xss=1>본문",
                                "date": "2026-05-05<script>window.__xss=1</script>",
                                "speakers": ["SPEAKER_00<img src=x onerror=window.__xss=1>"],
                                "start_time": 12.5,
                                "end_time": 25.0,
                                "source": 'bad"><img src=x onerror=window.__xss=1>',
                            }
                        ],
                    }
                ),
            )
            return
        _mock_api(route)

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/search",
        api_handler=result_api,
    ) as page:
        page.wait_for_selector("#searchQuery", state="attached")
        page.evaluate("() => { window.__xss = 0; }")
        page.locator("#searchQuery").fill("프로젝트 일정")
        page.keyboard.press("Enter")
        page.wait_for_selector(".result-item", state="attached")

        assert captured_payloads[0] == {"query": "프로젝트 일정"}
        assert "1건 검색됨" in page.locator("#searchStats").inner_text()
        assert "점수 0.9877" in page.locator(".result-score").inner_text()
        assert page.locator(".result-item img").count() == 0
        assert page.evaluate("() => window.__xss") == 0
        assert "bad" in page.locator(".result-source-tag").inner_text().lower()
        assert "both" in (page.locator(".result-source-tag").get_attribute("class") or "")

        page.locator(".result-item").press("Enter")
        page.wait_for_url("**/app/viewer/**")
        assert "/app/viewer/%ED%9A%8C%EC%9D%98%2F%EC%95%8C%ED%8C%8C" in page.url
        assert "q=%ED%94%84%EB%A1%9C%EC%A0%9D%ED%8A%B8%20%EC%9D%BC%EC%A0%95" in page.url
        assert "t=12.5" in page.url


def test_search_stale_requests_do_not_mutate_after_newer_search_or_destroy(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """느린 이전 검색과 destroy 이후 실패가 현재 뷰를 오염시키지 않는지 검증."""
    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/search",
    ) as page:
        page.wait_for_selector("#searchQuery", state="attached")
        page.evaluate(
            """
            () => {
                window.__searchRequests = [];
                window.MeetingApp.apiPost = function(endpoint, body) {
                    return new Promise((resolve, reject) => {
                        window.__searchRequests.push({ endpoint, body, resolve, reject });
                    });
                };
            }
            """
        )

        page.locator("#searchQuery").fill("느린 검색")
        page.evaluate(
            """
            () => document.querySelector('#searchForm').dispatchEvent(
                new Event('submit', { bubbles: true, cancelable: true })
            )
            """
        )
        page.locator("#searchQuery").fill("최신 검색")
        page.evaluate(
            """
            () => document.querySelector('#searchForm').dispatchEvent(
                new Event('submit', { bubbles: true, cancelable: true })
            )
            """
        )
        page.wait_for_function("() => window.__searchRequests.length === 2")
        page.evaluate(
            """
            () => window.__searchRequests[1].resolve({
                vector_count: 1,
                fts_count: 0,
                results: [{
                    meeting_id: 'latest',
                    score: 1,
                    text: '최신 결과',
                    date: '2026-05-05',
                    speakers: ['SPEAKER_00'],
                    start_time: 1,
                    end_time: 2,
                    source: 'vector'
                }]
            })
            """
        )
        page.wait_for_selector(".result-item", state="attached")
        assert "최신 결과" in page.locator("#searchResultsList").inner_text()

        page.evaluate(
            """
            () => window.__searchRequests[0].resolve({
                vector_count: 1,
                fts_count: 0,
                results: [{
                    meeting_id: 'stale',
                    score: 1,
                    text: '이전 결과',
                    date: '2026-05-05',
                    speakers: [],
                    start_time: 1,
                    end_time: 2,
                    source: 'fts'
                }]
            })
            """
        )
        page.wait_for_timeout(150)
        assert "최신 결과" in page.locator("#searchResultsList").inner_text()
        assert "이전 결과" not in page.locator("#searchResultsList").inner_text()

        page.locator("#searchQuery").fill("파괴 후 실패")
        page.evaluate(
            """
            () => document.querySelector('#searchForm').dispatchEvent(
                new Event('submit', { bubbles: true, cancelable: true })
            )
            """
        )
        page.wait_for_function("() => window.__searchRequests.length === 3")
        page.evaluate("() => window.SPA.Router.navigate('/app/chat')")
        page.wait_for_url("**/app/chat")
        page.evaluate(
            """
            () => {
                const err = new Error('late failure');
                err.status = 503;
                window.__searchRequests[2].reject(err);
            }
            """
        )
        page.wait_for_timeout(150)
        assert "visible" not in (page.locator("#errorBanner").get_attribute("class") or "")


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


def test_chat_route_renders_interactive_shell(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """ChatView 모듈 분리 후 /app/chat 의 주요 DOM 계약을 검증."""
    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/chat",
    ) as page:
        page.wait_for_timeout(800)
        for selector in (
            "#chatInput",
            "#chatSendBtn",
            "#chatCancelBtn",
            "#chatMeetingFilter",
            "#chatMessagesArea",
            "#chatBtnClearChat",
        ):
            assert page.locator(selector).count() == 1, f"{selector} 부재"

        assert page.title() == "채팅 · Recap"
        list_panel_class = page.locator("#list-panel").get_attribute("class") or ""
        assert "chat-mode" in list_panel_class

        has_public_api = page.evaluate(
            "() => Boolean(window.SPA && window.SPA.ChatView && window.MeetingChatView)"
        )
        assert has_public_api, "ChatView 공개 factory/API 계약 누락"


def test_chat_send_preserves_payload_session_and_filter(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """메시지 전송 payload, 회의 필터, 세션 유지/초기화 계약을 검증."""
    captured_payloads: list[dict] = []

    def chat_api(route: Route) -> None:
        url = route.request.url
        if "/api/meetings" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "meetings": [
                            {"meeting_id": "meeting-a", "status": "completed"},
                            {"meeting_id": "meeting-b", "status": "completed"},
                        ]
                    }
                ),
            )
            return

        if "/api/chat" in url:
            captured_payloads.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "answer": f"응답 {len(captured_payloads)}",
                        "references": [],
                        "source_type": "rag",
                        "wiki_sources": [],
                        "llm_used": True,
                    }
                ),
            )
            return

        _mock_api(route)

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/chat",
        api_handler=chat_api,
    ) as page:
        page.wait_for_selector(
            "#chatMeetingFilter option[value='meeting-b']",
            state="attached",
        )
        page.select_option("#chatMeetingFilter", "meeting-b")

        page.locator("#chatInput").fill("첫 질문")
        page.locator("#chatSendBtn").click()
        page.wait_for_function(
            "() => document.querySelectorAll('.message.assistant').length === 1"
        )

        page.locator("#chatInput").fill("두 번째 질문")
        page.keyboard.press("Enter")
        page.wait_for_function(
            "() => document.querySelectorAll('.message.assistant').length === 2"
        )

        assert len(captured_payloads) == 2
        assert captured_payloads[0]["query"] == "첫 질문"
        assert captured_payloads[1]["query"] == "두 번째 질문"
        assert captured_payloads[0]["session_id"] == captured_payloads[1]["session_id"]
        assert captured_payloads[0]["meeting_id_filter"] == "meeting-b"
        assert captured_payloads[0]["date_filter"] is None
        assert captured_payloads[0]["speaker_filter"] is None

        page.locator("#chatBtnClearChat").click()
        page.locator("#chatInput").fill("새 질문")
        page.locator("#chatSendBtn").click()
        page.wait_for_function(
            "() => document.querySelectorAll('.message.assistant').length === 1"
        )

        assert len(captured_payloads) == 3
        assert captured_payloads[2]["query"] == "새 질문"
        assert captured_payloads[2]["session_id"] != captured_payloads[0]["session_id"]
        assert captured_payloads[2]["meeting_id_filter"] == "meeting-b"


def test_wiki_route_renders_shell_tree_and_public_api(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """WikiView 모듈 분리 후 /app/wiki 의 shell, 트리, 공개 API 계약을 검증."""

    def wiki_api(route: Route) -> None:
        url = route.request.url
        if "/api/wiki/health" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"status": "ok", "raw_markdown": "# Health"}),
            )
            return
        if "/api/wiki/pages" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "pages": [
                            {
                                "path": "decisions/launch.md",
                                "type": "decision",
                                "title": "출시 결정",
                                "last_updated": "2026-05-01T00:00:00",
                            },
                            {
                                "path": "people/철수.md",
                                "type": "person",
                                "title": "철수",
                                "last_updated": "2026-05-02T00:00:00",
                            },
                        ]
                    }
                ),
            )
            return
        _mock_api(route)

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/wiki",
        api_handler=wiki_api,
    ) as page:
        page.wait_for_selector(".wiki-tree__item", state="attached")

        for selector in (
            ".wiki-view",
            "#wikiSearchInput",
            "#wikiHealthBadge",
            "#wikiTree",
            "#wikiPreview",
        ):
            assert page.locator(selector).count() == 1, f"{selector} 부재"

        assert page.title() == "위키 · Recap"
        assert page.locator("#navWiki").get_attribute("aria-current") == "page"
        assert "active" in (page.locator("#navWiki").get_attribute("class") or "")
        assert "chat-mode" in (page.locator("#list-panel").get_attribute("class") or "")

        has_public_api = page.evaluate(
            "() => Boolean(window.SPA && window.SPA.WikiView && window.MeetingWikiView)"
        )
        assert has_public_api, "WikiView 공개 factory/API 계약 누락"

        decisions = page.locator('.wiki-tree__category[data-cat="decisions"]')
        assert "결정사항" in decisions.inner_text()
        header = decisions.locator(".wiki-tree__category-header")
        assert header.get_attribute("aria-expanded") == "true"
        header.click()
        assert header.get_attribute("aria-expanded") == "false"
        assert "wiki-tree__category--collapsed" in (decisions.get_attribute("class") or "")


def test_wiki_search_detail_citation_and_unicode_slug_contract(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """검색, 상세 조회, 한국어 nested slug, citation 라우팅 계약을 검증."""
    search_urls: list[str] = []
    captured_detail = {"url": ""}

    def wiki_api(route: Route) -> None:
        url = route.request.url
        if "/api/wiki/search" in url:
            search_urls.append(url)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "results": [
                            {
                                "path": "people/team/철수.md",
                                "type": "person",
                                "title": "철수 상세",
                                "snippet": "출시 담당자",
                            }
                        ]
                    }
                ),
            )
            return
        if "/api/wiki/pages/person/team/%EC%B2%A0%EC%88%98" in url:
            captured_detail["url"] = url
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "path": "people/team/철수.md",
                        "type": "person",
                        "title": "철수 상세",
                        "frontmatter": {
                            "title": "철수 상세",
                            "aliases": ["김철수", "CS"],
                            "meta": {"team": "제품"},
                        },
                        "content": ("# 철수\n\n출시 담당자입니다. [meeting:abc12345@01:02:03]"),
                    }
                ),
            )
            return
        if "/api/wiki/health" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"status": "ok", "raw_markdown": "# Health"}),
            )
            return
        if "/api/wiki/pages" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "pages": [
                            {
                                "path": "people/team/철수.md",
                                "type": "person",
                                "title": "철수 상세",
                                "last_updated": "2026-05-02T00:00:00",
                            }
                        ]
                    }
                ),
            )
            return
        _mock_api(route)

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/wiki",
        api_handler=wiki_api,
    ) as page:
        page.wait_for_selector(".wiki-tree__item", state="attached")
        page.locator("#wikiSearchInput").fill("출시")
        page.wait_for_timeout(450)

        assert len(search_urls) == 1
        assert "q=%EC%B6%9C%EC%8B%9C" in search_urls[0]
        assert "limit=20" in search_urls[0]

        page.locator(".wiki-tree__item", has_text="철수 상세").click()
        page.wait_for_selector(".wiki-preview-page-title", state="attached")

        detail_url = captured_detail["url"]
        assert "/api/wiki/pages/person/team/%EC%B2%A0%EC%88%98" in detail_url
        assert "team%2F" not in detail_url
        assert page.locator(".wiki-preview-page-title").inner_text() == "철수 상세"
        assert "김철수, CS" in page.locator(".wiki-preview-frontmatter").inner_text()
        assert '"team":"제품"' in page.locator(".wiki-preview-frontmatter").inner_text()

        citation = page.locator(".wiki-citation").first
        assert citation.get_attribute("data-mid") == "abc12345"
        assert citation.get_attribute("data-seconds") == "3723"
        citation.click()
        page.wait_for_url("**/app/viewer/abc12345?t=3723")


def test_wiki_health_modal_closes_and_destroy_cleans_it(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """Health 모달 focus 복귀와 라우트 이동 시 destroy cleanup 을 검증."""

    def wiki_api(route: Route) -> None:
        url = route.request.url
        if "/api/wiki/health" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"status": "ok", "raw_markdown": "# Health\n\n정상"}),
            )
            return
        if "/api/wiki/pages" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"pages": []}),
            )
            return
        _mock_api(route)

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/wiki",
        api_handler=wiki_api,
    ) as page:
        page.wait_for_selector("#wikiHealthBadge", state="attached")
        page.locator("#wikiHealthBadge").click()
        modal = page.locator(".wiki-health-modal")
        assert modal.count() == 1
        assert modal.get_attribute("role") == "dialog"
        assert modal.get_attribute("aria-modal") == "true"
        assert page.evaluate("() => document.activeElement.className") == (
            "wiki-health-modal-close"
        )

        page.locator(".wiki-health-modal-close").click()
        page.wait_for_selector(".wiki-health-modal", state="detached")
        assert page.evaluate("() => document.activeElement.id") == "wikiHealthBadge"

        page.locator("#wikiHealthBadge").click()
        assert page.locator(".wiki-health-modal").count() == 1
        page.evaluate("() => window.SPA.Router.navigate('/app/chat')")
        page.wait_for_url("**/app/chat")
        assert page.locator(".wiki-health-modal").count() == 0
        assert page.locator(".wiki-view").count() == 0


def test_ab_test_routes_render_and_expose_module_boundary(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """A/B 모듈 분리 후 세 라우트가 ReferenceError 없이 렌더링된다."""
    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/ab-test",
    ) as page:
        page.wait_for_selector(".ab-test-view", state="attached")
        assert page.locator("#abTestList").count() == 1
        assert "chat-mode" in (page.locator("#list-panel").get_attribute("class") or "")
        assert page.evaluate("() => Boolean(window.MeetingAbTestView)")

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/ab-test/new",
    ) as page:
        page.wait_for_selector(".ab-new-form", state="attached")
        assert page.locator("#abSubmitBtn").count() == 1

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/ab-test/ab_20260505-120000_abcdef12",
    ) as page:
        page.wait_for_selector(".ab-result-view", state="attached")
        assert page.locator("#abResultHeader").count() == 1
        assert page.locator(".ab-compare-tab").count() >= 2


def test_ab_test_list_card_navigation_and_delete(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """A/B 목록 카드 이동과 삭제 API 계약을 검증."""
    deleted: list[str] = []

    def ab_api(route: Route) -> None:
        url = route.request.url
        if "/api/ab-tests/ab_20260505-120000_abcdef12" in url and route.request.method == "DELETE":
            deleted.append(url)
            route.fulfill(status=204, body="")
            return
        if "/api/ab-tests" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "tests": [
                            {
                                "test_id": "ab_20260505-120000_abcdef12",
                                "test_type": "llm",
                                "status": "completed",
                                "source_meeting_id": "meeting-a",
                                "started_at": "2026-05-05T12:00:00",
                                "variant_a": {"label": "모델 A"},
                                "variant_b": {"label": "모델 B"},
                            }
                        ]
                    }
                ),
            )
            return
        _mock_api(route)

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/ab-test",
        api_handler=ab_api,
    ) as page:
        page.wait_for_selector(".ab-test-card", state="attached")
        page.locator(".ab-test-card-title").click()
        page.wait_for_url("**/app/ab-test/ab_20260505-120000_abcdef12")

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/ab-test",
        api_handler=ab_api,
    ) as page:
        page.on("dialog", lambda dialog: dialog.accept())
        page.wait_for_selector(".ab-test-card-delete", state="attached")
        page.locator(".ab-test-card-delete").click()
        page.wait_for_function("() => true")
        assert deleted


def test_ab_test_new_form_posts_llm_and_stt_payloads(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """A/B 생성 폼의 LLM/STT payload와 query preselect를 검증."""
    payloads: list[dict] = []
    endpoints: list[str] = []

    def form_api(route: Route) -> None:
        url = route.request.url
        if "/api/meetings" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "meetings": [
                            {"meeting_id": "meeting-a", "status": "completed"},
                            {"meeting_id": "meeting-b", "status": "recorded"},
                        ]
                    }
                ),
            )
            return
        if "/api/stt-models" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"models": [{"id": "stt-a"}, {"id": "stt-b"}]}),
            )
            return
        if "/api/llm-models/available" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    [
                        {"label": "LLM A", "model_id": "llm-a", "available": True},
                        {"label": "LLM B", "model_id": "llm-b", "available": True},
                    ]
                ),
            )
            return
        if "/api/ab-tests/llm" in url or "/api/ab-tests/stt" in url:
            endpoints.append(url)
            payloads.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"test_id": "ab_20260505-120000_abcdef12"}),
            )
            return
        _mock_api(route)

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/ab-test/new?source=meeting-a",
        api_handler=form_api,
    ) as page:
        page.wait_for_selector("#abModelASelect option[value='llm-a']", state="attached")
        page.select_option("#abModelASelect", "llm-a")
        page.select_option("#abModelBSelect", "llm-b")
        assert page.locator("#abSourceMeeting").input_value() == "meeting-a"
        page.locator("#abSubmitBtn").click()
        page.wait_for_url("**/app/ab-test/ab_20260505-120000_abcdef12")

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/ab-test/new?source=meeting-b&type=stt",
        api_handler=form_api,
    ) as page:
        page.wait_for_selector("#abModelASelect option[value='stt-a']", state="attached")
        page.select_option("#abModelASelect", "stt-a")
        page.select_option("#abModelBSelect", "stt-b")
        assert page.locator("#abSourceMeeting").input_value() == "meeting-b"
        page.locator("#abSubmitBtn").click()
        page.wait_for_url("**/app/ab-test/ab_20260505-120000_abcdef12")

    assert "/api/ab-tests/llm" in endpoints[0]
    assert payloads[0]["source_meeting_id"] == "meeting-a"
    assert payloads[0]["variant_a"]["model_id"] == "llm-a"
    assert payloads[0]["variant_a"]["backend"] == "mlx"
    assert payloads[0]["scope"] == {"correct": True, "summarize": True}
    assert "/api/ab-tests/stt" in endpoints[1]
    assert payloads[1]["source_meeting_id"] == "meeting-b"
    assert payloads[1]["variant_a"]["model_id"] == "stt-a"
    assert payloads[1]["allow_diarize_rerun"] is True


def test_ab_test_result_cancel_summary_and_cleanup(
    browser: Browser,
    spa_static_server: str,
) -> None:
    """결과 렌더, markdown summary, cancel, timer/listener cleanup smoke."""
    cancelled: list[str] = []

    def result_api(route: Route) -> None:
        url = route.request.url
        if "/api/ab-tests/ab_20260505-120000_abcdef12/cancel" in url:
            cancelled.append(url)
            route.fulfill(status=200, content_type="application/json", body='{"ok": true}')
            return
        if "/variant/a/summary" in url:
            route.fulfill(status=200, content_type="text/markdown", body="## A 요약\nSPEAKER_00")
            return
        if "/variant/b/summary" in url:
            route.fulfill(status=200, content_type="text/markdown", body="## B 요약\n정상")
            return
        if "/api/ab-tests/ab_20260505-120000_abcdef12" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(_ab_test_detail("completed")),
            )
            return
        _mock_api(route)

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/ab-test/ab_20260505-120000_abcdef12",
        api_handler=result_api,
    ) as page:
        page.wait_for_selector(".ab-compare-tab", state="attached")
        assert "모델 A vs 모델 B" in page.locator("#abResultHeader").inner_text()
        assert "안녕하세요" in page.locator("#abCompareContent").inner_text()

        page.locator('.ab-compare-tab[data-tab="summary"]').click()
        page.wait_for_selector("#abSummaryA h2", state="attached")
        assert "로드 실패" not in page.locator("#abCompareContent").inner_text()
        assert page.locator("#abSummaryA mark.forbidden-pattern").count() == 1

        page.evaluate(
            """
            () => {
                window.__abRemoved = 0;
                const oldRemove = document.removeEventListener.bind(document);
                document.removeEventListener = function(type, fn, opts) {
                    if (type === 'ws:step_progress') window.__abRemoved += 1;
                    return oldRemove(type, fn, opts);
                };
            }
            """
        )
        page.evaluate("() => window.SPA.Router.navigate('/app/chat')")
        page.wait_for_url("**/app/chat")
        assert page.evaluate("() => window.__abRemoved") >= 1

    def running_api(route: Route) -> None:
        url = route.request.url
        if "/cancel" in url:
            cancelled.append(url)
            route.fulfill(status=200, content_type="application/json", body='{"ok": true}')
            return
        if "/api/ab-tests/ab_20260505-120000_abcdef12" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(_ab_test_detail("running")),
            )
            return
        _mock_api(route)

    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/ab-test/ab_20260505-120000_abcdef12",
        api_handler=running_api,
    ) as page:
        page.wait_for_selector("#abCancelTestBtn", state="attached")
        page.locator("#abCancelTestBtn").click()
        page.wait_for_function("() => true")
        assert cancelled


# ============================================================
# T-102 dark-mode-tones
# ============================================================


def test_t102_dark_text_secondary_token(browser: Browser, spa_static_server: str) -> None:
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


def test_t103_skeleton_card_css_defined(browser: Browser, spa_static_server: str) -> None:
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


def test_t103_search_loading_skeleton_markup(browser: Browser, spa_static_server: str) -> None:
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


def test_t103_viewer_skeleton_markup(browser: Browser, spa_static_server: str) -> None:
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


def test_t201_focus_ring_token_defined(browser: Browser, spa_static_server: str) -> None:
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


def test_t201_first_interactive_focus_visible(browser: Browser, spa_static_server: str) -> None:
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
            f"focus 요소({info['tag']}#{info['id']})에 box-shadow none — focus-visible 미적용"
        )


# ============================================================
# T-202 command-palette
# ============================================================


def test_t202_palette_dialog_in_dom_after_init(browser: Browser, spa_static_server: str) -> None:
    """SPA init → dialog.command-palette 요소 DOM 존재."""
    with _spa_page(browser, spa_static_server, {"width": 1024, "height": 768}) as page:
        page.wait_for_timeout(800)  # CommandPalette 초기화 대기
        # init 시점에 DOM 에 미존재할 수도 있음 — open() 시 lazy create 가능
        # ⌘K 한 번 눌러서 lazy init 트리거
        page.keyboard.press("Meta+k")
        page.wait_for_timeout(300)
        dialog = page.locator("dialog.command-palette")
        if dialog.count() == 0:
            pytest.fail("dialog.command-palette 미존재 — Command Palette init 실패")
        assert dialog.count() == 1, f"command-palette dialog 중복: {dialog.count()}"


def test_t202_cmd_k_opens_palette(browser: Browser, spa_static_server: str) -> None:
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
# SettingsView module boundary
# ============================================================


def test_settings_route_renders_tabs(browser: Browser, spa_static_server: str) -> None:
    """설정 라우트 진입 시 settings-view.js factory 결과가 렌더링된다."""
    with _spa_page(
        browser,
        spa_static_server,
        {"width": 1024, "height": 768},
        path="/app/settings/prompts",
    ) as page:
        page.wait_for_timeout(800)
        settings = page.locator(".settings-view")
        if settings.count() == 0:
            pytest.fail("설정 화면 미렌더 — .settings-view 부재")

        tabs = page.locator(".settings-tab")
        assert tabs.count() == 4
        prompts_tab = page.locator('.settings-tab[data-tab="prompts"]')
        assert prompts_tab.get_attribute("aria-selected") == "true"
        assert page.locator("#settingsPanelHost").count() == 1


# ============================================================
# T-301 aria-sync
# ============================================================


def test_t301_active_nav_has_aria_current(browser: Browser, spa_static_server: str) -> None:
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


def test_t301_inactive_nav_no_aria_current(browser: Browser, spa_static_server: str) -> None:
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
            f"비활성 nav-btn 에 aria-current 잔존 (removeAttribute 위반): {violations}"
        )


def test_t301_route_change_updates_aria_current(browser: Browser, spa_static_server: str) -> None:
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
        assert result["home"] is None, f"#navHome 에 aria-current 잔존 (실제: {result['home']!r})"


# ============================================================
# T-302 mobile-responsive
# ============================================================


def test_t302_hamburger_visible_on_mobile(browser: Browser, spa_static_server: str) -> None:
    """375px viewport → 햄버거 버튼 보임."""
    with _spa_page(browser, spa_static_server, {"width": 375, "height": 667}) as page:
        page.wait_for_timeout(500)
        toggle = page.locator("#mobile-menu-toggle")
        assert toggle.count() == 1, "#mobile-menu-toggle 마크업 부재"
        # CSS display:none 인지 확인
        display = page.evaluate(
            "() => getComputedStyle(document.querySelector('#mobile-menu-toggle')).display"
        )
        assert display != "none", f"햄버거 버튼이 모바일에서 display={display!r} (미노출)"


def test_t302_hamburger_click_opens_drawer(browser: Browser, spa_static_server: str) -> None:
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
        assert result["panelOpen"], "햄버거 클릭 후 #list-panel.is-open 미적용"


def test_t302_escape_closes_drawer(browser: Browser, spa_static_server: str) -> None:
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
        assert not result["panelOpen"], "ESC 후 #list-panel.is-open 잔존"
