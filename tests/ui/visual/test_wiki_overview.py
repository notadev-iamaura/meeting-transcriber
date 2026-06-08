"""wiki-overview-search — 현황(Overview) 탭 + 검색 카드 시각 회귀 baseline.

티켓: T-303 (wave 3, component `wiki-overview-search`)
계획서: memorable-wiki §5 C3 "UI — 현황 화면 + 검색 메타"

변종(3축 게이트의 visual 축):
    W1 — 현황 탭 (light desktop)   : digest 4섹션 카드
    W2 — 현황 탭 (dark desktop)    : 다크 단계 톤 + 상태 dot
    W3 — 현황 탭 (light mobile ≤640): 반응형 단일 컬럼
    W4 — 검색 결과 카드 (light desktop): score·status 배지·snippet·인용

baseline 메커니즘: `harness.snapshot.assert_visual_match` 가 baseline 부재 시 첫
캡처를 baseline 으로 저장(이후 픽셀 비교, 기본 임계 0.5%). digest/search 응답을
`page.route` 로 mock 해 결정론적 캡처를 만든다(백엔드 무변경).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import pytest
from playwright.sync_api import Browser, Page

from harness import snapshot

pytestmark = [pytest.mark.ui]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ACTUAL_DIR = PROJECT_ROOT / "tests" / "ui" / "visual" / "_actual"
BASELINES_DIR = PROJECT_ROOT / "tests" / "ui" / "visual" / "baselines"

DEVICE_SCALE_FACTOR = 2  # Retina 캡처 (기존 visual baseline 관례와 동일 DPR)

_MEETING_ID = "meeting_20260605_143000"

_DIGEST = {
    "generated_for": "2026-06-08",
    "total_open_actions": 3,
    "open_actions": [
        {
            "owner": "김민수",
            "items": [
                {
                    "description": "런칭 날짜 확정 후 공유",
                    "citations": [f"[meeting:{_MEETING_ID}@00:23:45]"],
                    "due_date": "2026-06-15",
                },
                {
                    "description": "API 스펙 리뷰",
                    "citations": [f"[meeting:{_MEETING_ID}@00:41:02]"],
                    "due_date": None,
                },
            ],
        },
        {
            "owner": "이지은",
            "items": [
                {
                    "description": "디자인 토큰 정리",
                    "citations": [f"[meeting:{_MEETING_ID}@00:08:11]"],
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
            "citations": [f"[meeting:{_MEETING_ID}@00:23:45]"],
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

_SEARCH = {
    "query": "런칭",
    "total": 2,
    "results": [
        {
            "path": "decisions/2026-06-05-launch-date.md",
            "type": "decisions",
            "title": "런칭일을 6월 30일로 확정",
            "snippet": "팀은 6월 30일 런칭에 합의했고 마케팅은…",
            "score": 0.92,
            "citations": [f"[meeting:{_MEETING_ID}@00:23:45]"],
            "metadata": {"status": "decided", "project": "recap-launch"},
        },
        {
            "path": "topics/launch-planning.md",
            "type": "topics",
            "title": "런칭 준비 주제",
            "snippet": "런칭 준비 항목 정리 — QA, 마케팅, 인프라…",
            "score": 0.78,
            "citations": [f"[meeting:{_MEETING_ID}@00:12:08]"],
            "metadata": {"project": "recap-launch"},
        },
    ],
}


def _install_mocks(page: Page) -> None:
    """digest/search 응답을 결정론적으로 주입한다(시각 baseline 안정화)."""

    def _digest(route: object, request: object) -> None:
        route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=json.dumps(_DIGEST, ensure_ascii=False),
        )

    def _search(route: object, request: object) -> None:
        route.fulfill(  # type: ignore[attr-defined]
            status=200,
            content_type="application/json",
            body=json.dumps(_SEARCH, ensure_ascii=False),
        )

    page.route("**/api/wiki/digest", _digest)
    page.route("**/api/wiki/search?**", _search)
    page.route("**/api/wiki/search", _search)


@contextmanager
def _make_page(
    browser: Browser,
    *,
    width: int,
    height: int,
    color_scheme: Literal["light", "dark", "no-preference"],
) -> Iterator[Page]:
    """variant 별 viewport·color-scheme·DPR 일치 context."""
    context = browser.new_context(
        viewport={"width": width, "height": height},
        device_scale_factor=DEVICE_SCALE_FACTOR,
        color_scheme=color_scheme,
    )
    page = context.new_page()
    _install_mocks(page)
    try:
        yield page
    finally:
        context.close()


def _open_wiki(page: Page, base_url: str) -> None:
    """/app/wiki 로드 + 현황 패널 렌더 대기(부하 견고: domcontentloaded)."""
    page.goto(f"{base_url}/app/wiki", wait_until="domcontentloaded")
    page.wait_for_selector("#wikiOverviewPanel", timeout=15000)
    # 현황 카드가 그려질 때까지 대기(빈 패널 캡처 방지).
    page.wait_for_selector(".wiki-status-badge, .wiki-empty-state", timeout=15000)
    page.wait_for_timeout(200)


def _capture_and_compare(page: Page, variant: str, *, max_diff_pixel_ratio: float = 0.005) -> None:
    """캡처해 baseline 과 비교(없으면 첫 캡처를 baseline 으로 저장)."""
    ACTUAL_DIR.mkdir(parents=True, exist_ok=True)
    actual = ACTUAL_DIR / f"wiki-{variant}.png"
    page.screenshot(path=str(actual))
    baseline = BASELINES_DIR / f"wiki-{variant}.png"
    if not baseline.exists():
        snapshot.assert_visual_match(actual, baseline, max_diff_pixel_ratio=max_diff_pixel_ratio)
        return
    ratio = snapshot.pixel_diff_ratio(actual, baseline)
    if ratio > max_diff_pixel_ratio:
        raise AssertionError(
            f"visual diff {ratio:.4%} exceeds threshold {max_diff_pixel_ratio:.4%} "
            f"(actual={actual}, baseline={baseline})"
        )


def test_W1_현황탭_light_desktop(browser: Browser, ui_bulk_base_url: str) -> None:
    """현황 탭(light desktop) — digest 4섹션 카드 baseline."""
    with _make_page(browser, width=1280, height=900, color_scheme="light") as page:
        _open_wiki(page, ui_bulk_base_url)
        _capture_and_compare(page, "overview-light")


def test_W2_현황탭_dark_desktop(browser: Browser, ui_bulk_base_url: str) -> None:
    """현황 탭(dark desktop) — 다크 단계 톤 + 상태 dot baseline."""
    with _make_page(browser, width=1280, height=900, color_scheme="dark") as page:
        _open_wiki(page, ui_bulk_base_url)
        _capture_and_compare(page, "overview-dark")


def test_W3_현황탭_light_mobile(browser: Browser, ui_bulk_base_url: str) -> None:
    """현황 탭(light mobile ≤640px) — 반응형 단일 컬럼 baseline."""
    with _make_page(browser, width=390, height=844, color_scheme="light") as page:
        _open_wiki(page, ui_bulk_base_url)
        _capture_and_compare(page, "overview-mobile")


def test_W4_검색카드_light_desktop(browser: Browser, ui_bulk_base_url: str) -> None:
    """검색 결과 카드(light desktop) — score·status 배지·snippet·인용 baseline."""
    with _make_page(browser, width=1280, height=900, color_scheme="light") as page:
        _open_wiki(page, ui_bulk_base_url)
        page.locator("#wikiTabSearch").click()
        page.wait_for_timeout(150)
        page.fill("#wikiSearchInput", "런칭")
        page.wait_for_selector(".wiki-result-card", timeout=5000)
        page.wait_for_timeout(200)
        _capture_and_compare(page, "search-cards-light")
