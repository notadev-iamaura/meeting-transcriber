"""command-palette — closed/open-empty/open-results 시각 회귀 (T-202).

검증 대상:
    `tests/ui/_fixtures/command-palette-preview.html` 가 mockup §3 마크업
    + §2 토큰을 그대로 렌더링한 결과가 베이스라인 PNG 와 픽셀 diff < 0.1%
    (spec §5.3) 로 일치하는가.

DPR 일치:
    Designer-A baseline 이 viewport 1024×768 @ DPR=2 → PNG 2048×1536 으로
    캡처되어 있어 동일 DPR/viewport 강제.

변종 (mockup §7):
    closed       : <dialog> 의 open 속성 제거 — 페이지 본문만 캡처
    open-empty   : <dialog open> 기본 상태 — input 비어있음, 정적 카테고리 4 항목
    open-results : <dialog open> + input.value="회의" — 정적 + mock 검색 결과 추가

baseline 경로 직접 구성:
    `harness.snapshot.baseline_path()` 헬퍼는 SUPPORTED_VARIANTS = (light,
    dark, mobile) 만 허용하므로, 본 티켓은 mobile-responsive 패턴(T-302)
    처럼 `BASELINES_DIR / f"command-palette-{variant}.png"` 로 직접 구성한다.

Red 의도성:
    fixture 와 baseline 이 동일한 마크업·토큰 정의를 공유하므로 본 시나리오는
    PASS 가 정상 (fixture-as-source-of-truth). Frontend-A 가 spa.js 모듈을
    활성화한 뒤에도 동일 마크업이 유지되면 시각 계약이 유지된다.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page

from harness import snapshot

pytestmark = [pytest.mark.ui]

# 프로젝트 루트 — tests/ui/visual/test_command_palette.py 기준 3 단계 위.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
PREVIEW_URL = (
    PROJECT_ROOT / "tests" / "ui" / "_fixtures" / "command-palette-preview.html"
).as_uri()
ACTUAL_DIR = PROJECT_ROOT / "tests" / "ui" / "visual" / "_actual"
BASELINES_DIR = PROJECT_ROOT / "tests" / "ui" / "visual" / "baselines"

# Designer-A baseline 이 DPR=2 (Retina) 로 캡처되어 있어 동일 DPR 강제.
DEVICE_SCALE_FACTOR = 2

# Mock 검색 결과 4 건 — open-results 변종에서 listbox 끝에 주입 (mockup §5.2).
_MOCK_SEARCH_RESULTS_JS = """
() => {
  const list = document.getElementById('command-palette-list');
  if (!list) return;
  const mocks = [
    { id: 'm-001', title: '2026-04-15 팀 회고', meta: '15:00 · 32분' },
    { id: 'm-002', title: '2026-04-14 디자인 리뷰', meta: '11:00 · 48분' },
    { id: 'm-003', title: '2026-04-13 백엔드 회의', meta: '14:00 · 25분' },
    { id: 'm-004', title: '2026-04-12 1:1', meta: '10:00 · 28분' },
  ];
  for (const m of mocks) {
    const li = document.createElement('li');
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', 'false');
    li.setAttribute('data-action', 'open-meeting');
    li.setAttribute('data-meeting-id', m.id);
    li.setAttribute('tabindex', '-1');
    li.innerHTML =
      '<span class="command-palette-item-label">' + m.title + '</span>' +
      '<span class="command-palette-item-meta">' + m.meta + '</span>';
    list.appendChild(li);
  }
  const input = document.querySelector('.command-palette-input');
  if (input) input.value = '회의';
}
"""


@contextmanager
def _make_page(browser: Browser, *, color_scheme: str = "light") -> Iterator[Page]:
    """1024×768 + DPR=2 context 에서 새 Page 생성."""
    context = browser.new_context(
        viewport={"width": 1024, "height": 768},
        device_scale_factor=DEVICE_SCALE_FACTOR,
        color_scheme=color_scheme,
    )
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


def _capture_and_compare(page: Page, variant: str) -> None:
    """fixture 페이지를 캡처해 baseline 과 픽셀 비교.

    Raises:
        AssertionError: pixel diff > 0.1% 일 때 (spec §5.3)
    """
    ACTUAL_DIR.mkdir(parents=True, exist_ok=True)
    actual = ACTUAL_DIR / f"command-palette-{variant}.png"
    page.screenshot(path=str(actual))
    baseline = BASELINES_DIR / f"command-palette-{variant}.png"
    snapshot.assert_visual_match(actual, baseline, max_diff_pixel_ratio=0.001)


def test_command_palette_closed(browser: Browser) -> None:
    """closed 변종 — <dialog> open 속성 제거 후 페이지 본문 캡처."""
    with _make_page(browser) as page:
        page.goto(PREVIEW_URL)
        page.wait_for_load_state("networkidle")
        # <dialog> 닫기 — open 속성 제거 (close() 는 표준 API)
        page.evaluate(
            "() => { const d = document.getElementById('command-palette');"
            " if (d && d.open) d.close(); }"
        )
        _capture_and_compare(page, "closed")


def test_command_palette_open_empty(browser: Browser) -> None:
    """open-empty 변종 — <dialog open> + 입력 비어있음, 정적 카테고리만."""
    with _make_page(browser) as page:
        page.goto(PREVIEW_URL)
        page.wait_for_load_state("networkidle")
        # 기본 fixture 가 이미 open=true + 정적 4 항목 상태
        _capture_and_compare(page, "open-empty")


def test_command_palette_open_results(browser: Browser) -> None:
    """open-results 변종 — input.value="회의" + mock 검색 결과 4 건 추가."""
    with _make_page(browser) as page:
        page.goto(PREVIEW_URL)
        page.wait_for_load_state("networkidle")
        page.evaluate(_MOCK_SEARCH_RESULTS_JS)
        _capture_and_compare(page, "open-results")
