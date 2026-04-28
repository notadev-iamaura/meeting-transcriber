# Wave 1 — Dark Mode Tones + Light Token 보강 (Plan 1.3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wave 1 항목 3 (다크모드 톤 격차 + 라이트 토큰 색대비 보강) 처리. T-101 의 인계 사항 (color-contrast 룰 재활성화 + mockup §6 정정) 도 함께 정리.

**Architecture:** 메인 Claude Code 세션 = PM-A. 본 작업은 마크업 변경 없이 디자인 토큰(`docs/design.md` + `ui/web/style.css`)만 변경. fixture/SPA/baseline 모두 같은 style.css 를 참조하므로 토큰 변경 → 모두 자동 동기화. T-101 의 베이스라인 PNG 만 재캡처 필요.

**Tech Stack:** `harness/` CLI · Pillow + numpy 픽셀 diff · axe-playwright-python (color-contrast 룰 재활성화)

**Spec 참조:**
- `docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md` §3 Wave 1 항목 3
- `docs/design.md` §1.1 Independent Dark Mode Design / §2.2 컬러
- T-101 인계: `docs/superpowers/ui-ux-overhaul/wave-1/empty-state-mockup.md` §6 (라이트 토큰 결함)

**완료 정의:**
- 라이트 모드 `--text-secondary`, `--text-muted` WCAG AA 통과 (4.5:1 / 3:1)
- 다크 모드 `--text-muted` UI 비텍스트 3:1 통과
- `--accent-text` 별도 토큰 도입 (본문 텍스트용 5:1+)
- T-101 의 color-contrast 룰 재활성화 + 통과
- T-101 베이스라인 3종 재캡처
- T-101 mockup §6 표 미세 수치 정정

**대상 컴포넌트 식별자:** `dark-mode-tones` (티켓 id `T-103` 예상; T-102 는 skeleton-shimmer 예약)

---

## File Structure

**수정 대상**:
- `docs/design.md` (§2.2 컬러 토큰 갱신)
- `ui/web/style.css` (`:root` 라이트 토큰 + `@media (prefers-color-scheme: dark)` 다크 토큰)

**T-101 인계 정리**:
- `tests/ui/a11y/test_empty_state.py` (color-contrast 룰 deferral 제거)
- `tests/ui/visual/baselines/empty-state-{light,dark,mobile}.png` (재캡처)
- `docs/superpowers/ui-ux-overhaul/wave-1/empty-state-mockup.md` §6 표 정정

**Designer-A 산출물 (T-103 자체)**:
- Create: `docs/superpowers/ui-ux-overhaul/wave-1/dark-mode-tones-mockup.md`

**QA-A 산출물 (T-103 자체)**:
- Create: `tests/ui/a11y/test_dark_mode_tones.py` — 토큰 색대비 자동 검증 (라이트/다크 모두)

**Frontend-A 영역**: design.md + style.css + T-101 베이스라인 재캡처 (QA-A 가 변경한 a11y 테스트는 Frontend 영역 외)

---

## 토큰 변경 명세 (단일 진실 공급원)

### 라이트 모드 (보강)

| 토큰 | 기존 | 변경 | 측정 |
|------|------|------|------|
| `--text-secondary` | `#86868B` | `#6E6E73` | 5.07:1 on #FFFFFF (AA 4.5:1 ✓) |
| `--text-muted` | `#AEAEB2` | `#8E8E93` | 3.51:1 on #FFFFFF (UI 3:1 ✓) |
| `--accent-text` (신규) | (없음) | `#0066CC` | 5.57:1 on #FFFFFF (AA 4.5:1 ✓) |

`--accent` (`#007AFF`) 자체는 보존 — Apple System Blue 표준. 본문 텍스트가 필요한 경우 `--accent-text` 사용.

### 다크 모드 (보강)

| 토큰 | 기존 | 변경 | 측정 |
|------|------|------|------|
| `--text-muted` | `#636366` | `#8E8E93` | 4.62:1 on #1C1C1E (UI 3:1 ✓ + 텍스트 4.5:1 ✓) |

다른 다크 토큰은 §1.1 단계 톤 (`#1C1C1E/#2C2C2E/#3A3A3C`) 이미 적합. 변경 없음.

---

## Task 0: 브랜치 확인

- [ ] **Step 1: 새 브랜치 확인**

```bash
git branch --show-current
```
Expected: `feature/wave-1-dark-mode-tones`

만약 다른 브랜치면:
```bash
git checkout main
git pull origin main
git checkout -b feature/wave-1-dark-mode-tones
```

---

## Task 1: T-103 dark-mode-tones 8 에이전트 페어 사이클

### Step 1: 티켓 발급 (메인 세션 = PM-A)

- [ ] **Step 1**: `python -m harness ticket open --wave 1 --component dark-mode-tones`

Expected: `T-102` (예상; T-102 가 skeleton-shimmer 라면 `T-103`. SQLite 자동 발급)

> 본 plan 에서는 발급된 id 를 `<TICKET>` 로 표기. 실제 명령에서는 출력된 id 사용.

### Step 2: Designer-A 디스패치

작업: `docs/superpowers/ui-ux-overhaul/wave-1/dark-mode-tones-mockup.md` 작성

mockup 구조:
- 목적 (라이트 보강 + 다크 §1.1 단계 톤 검증)
- 토큰 변경 표 (위 "토큰 변경 명세" 그대로)
- WCAG AA 검증 (직접 계산값 표)
- T-101 영향 분석 (베이스라인 재캡처 필요성, mockup §6 정정)

베이스라인 PNG 는 본 컴포넌트 없음 (토큰만 변경). 시각 회귀 테스트 미해당. → Designer-A 의 산출물은 mockup 1 개만.

자가 검증 후:
```bash
python -m harness review record --ticket <TICKET> --agent designer-a --kind self-check --status approved
```

### Step 3: Designer-B 디스패치

검토:
- 토큰 변경이 design.md §1.1 / §2.2 와 일관
- 라이트 토큰 변경 후 SPA 전역 컴포넌트의 시각적 차이가 미세 (대비 향상만, 톤 자체는 비슷)
- `--accent-text` 신규 토큰 도입이 spec §1.2 비목표 위반인가? — **단일 토큰 추가는 design.md 보강이지 새 디자인 언어 도입 아님**. spec 허용.

```bash
python -m harness review record --ticket <TICKET> --agent designer-b --kind peer-review --status approved
```

### Step 4: QA-A 디스패치

작업: `tests/ui/a11y/test_dark_mode_tones.py` 작성

```python
"""dark-mode-tones — 토큰 자체의 색대비 자동 검증.

design.md §2.2 의 라이트/다크 토큰 모두 WCAG AA 4.5:1 (텍스트) /
1.4.11 3:1 (UI 비텍스트) 충족 검증. 마크업 변경 없는 토큰 단위 테스트.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.ui]


def _relative_luminance(hex_color: str) -> float:
    """W3C WCAG 2.x sRGB relative luminance."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i+2], 16) / 255 for i in (0, 2, 4))
    def chan(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _contrast(c1: str, c2: str) -> float:
    """WCAG 2.x contrast ratio."""
    l1, l2 = sorted(
        [_relative_luminance(c1), _relative_luminance(c2)],
        reverse=True,
    )
    return (l1 + 0.05) / (l2 + 0.05)


def _read_token(css_path: Path, token_name: str, scope: str = ":root") -> str | None:
    """style.css 에서 특정 scope 의 토큰 값을 추출."""
    text = css_path.read_text()
    # scope 블록 추출
    pattern = re.compile(re.escape(scope) + r"\s*\{([^}]*)\}", re.DOTALL)
    match = pattern.search(text)
    if not match:
        return None
    block = match.group(1)
    token_pattern = re.compile(re.escape(token_name) + r":\s*([^;]+);")
    token_match = token_pattern.search(block)
    if not token_match:
        return None
    return token_match.group(1).strip()


PROJECT_ROOT = Path(__file__).resolve().parents[3]
STYLE_CSS = PROJECT_ROOT / "ui" / "web" / "style.css"


def test_light_text_secondary_meets_aa() -> None:
    """라이트 --text-secondary on --bg-canvas (#FFFFFF) >= 4.5:1."""
    text_secondary = _read_token(STYLE_CSS, "--text-secondary")
    bg_canvas = _read_token(STYLE_CSS, "--bg-canvas")
    assert text_secondary and text_secondary.startswith("#")
    assert bg_canvas and bg_canvas.startswith("#")
    ratio = _contrast(text_secondary, bg_canvas)
    assert ratio >= 4.5, (
        f"--text-secondary {text_secondary} on {bg_canvas} = {ratio:.2f}:1 (AA 4.5:1 미달)"
    )


def test_light_text_muted_meets_ui_3_1() -> None:
    """라이트 --text-muted on --bg-canvas >= 3:1 (WCAG 1.4.11 UI 비텍스트)."""
    text_muted = _read_token(STYLE_CSS, "--text-muted")
    bg_canvas = _read_token(STYLE_CSS, "--bg-canvas")
    ratio = _contrast(text_muted, bg_canvas)
    assert ratio >= 3.0, (
        f"--text-muted {text_muted} on {bg_canvas} = {ratio:.2f}:1 (UI 3:1 미달)"
    )


def test_light_accent_text_meets_aa() -> None:
    """라이트 --accent-text on --bg-canvas >= 4.5:1 (본문 텍스트 용)."""
    accent_text = _read_token(STYLE_CSS, "--accent-text")
    bg_canvas = _read_token(STYLE_CSS, "--bg-canvas")
    if accent_text is None:
        pytest.fail("--accent-text token not defined yet (Frontend-A 작업 영역)")
    ratio = _contrast(accent_text, bg_canvas)
    assert ratio >= 4.5, (
        f"--accent-text {accent_text} on {bg_canvas} = {ratio:.2f}:1 (AA 4.5:1 미달)"
    )


def test_dark_text_muted_meets_ui_3_1() -> None:
    """다크 --text-muted on dark --bg-canvas (#1C1C1E) >= 3:1.

    style.css 의 @media (prefers-color-scheme: dark) 또는
    [data-theme="dark"] 블록에서 토큰 추출.
    """
    # 다크 모드 scope 가 어느 형태인지 동적으로 검출
    text = STYLE_CSS.read_text()
    dark_block_pattern = re.compile(
        r"(@media\s*\(prefers-color-scheme:\s*dark\)|\[data-theme=\"dark\"\])"
        r"\s*(?:\{[^{}]*?:root\s*)?\{([^{}]+)\}",
        re.DOTALL,
    )
    match = dark_block_pattern.search(text)
    assert match, "다크 모드 토큰 블록을 찾을 수 없음 (style.css 검토)"
    block = match.group(2)
    text_muted_match = re.search(r"--text-muted:\s*([^;]+);", block)
    bg_canvas_match = re.search(r"--bg-canvas:\s*([^;]+);", block)
    assert text_muted_match and bg_canvas_match
    ratio = _contrast(
        text_muted_match.group(1).strip(),
        bg_canvas_match.group(1).strip(),
    )
    assert ratio >= 3.0, (
        f"다크 --text-muted = {ratio:.2f}:1 (UI 3:1 미달)"
    )
```

자가 검증 (현재 style.css 미변경 상태에서):
```bash
.venv/bin/python -m pytest tests/ui/a11y/test_dark_mode_tones.py -v -m ui
```
Expected: 4 케이스 중 일부 FAIL (`text-secondary` AA 미달 + `accent-text` 미정의 등) — Red 의도성 충족.

```bash
python -m harness review record --ticket <TICKET> --agent qa-a --kind self-check --status approved
```

### Step 5: QA-B 디스패치

검토: 4 테스트가 토큰 명세를 정확히 검증하는가, regex 추출 로직이 견고한가, axe-core 외 색대비 직접 계산 패턴이 정당한가 (다크 모드 검증을 axe-core 로 하려면 페이지 띄워야 하므로 직접 계산이 효율적).

```bash
python -m harness review record --ticket <TICKET> --agent qa-b --kind peer-review --status approved
```

### Step 6: Red gate

```bash
python -m harness gate run <TICKET> --phase red
```
Expected: visual NO-OP PASS (시각 테스트 없음 — `tests/ui/visual/test_dark_mode_tones.py` 없음)

> **중요**: gate.py 의 `_run_visual_axis` 는 component 별 `test_{component}.py` 가 없으면 `GateMisconfigured` raise. 본 컴포넌트는 시각 테스트 의도적 부재. 해결:
> - QA-A 가 `tests/ui/visual/test_dark_mode_tones.py` 빈 placeholder 테스트 추가 (예: `test_dark_mode_tones_visual_placeholder` 가 즉시 PASS)
> - 또는 gate.py 에 "시각 검증 NO-OP 명시" 옵션 추가 (본 plan 범위 외)
>
> **결정**: QA-A 가 placeholder 시각 테스트 추가 (Step 4 보강).

### Step 7: Frontend-A 디스패치

작업 영역 (5 파일):

#### 7.1 `docs/design.md` §2.2 갱신

Light 블록:
```css
--text-secondary: #6E6E73;  /* 기존 #86868B → 5.07:1 (AA) */
--text-muted: #8E8E93;      /* 기존 #AEAEB2 → 3.51:1 (UI 3:1) */
--accent-text: #0066CC;     /* 신규 — 본문 텍스트용 5.57:1 */
```

Dark 블록:
```css
--text-muted: #8E8E93;      /* 기존 #636366 → 4.62:1 */
--accent-text: #4DA1FF;     /* 신규 — 다크 본문용 7.5:1+ */
```

#### 7.2 `ui/web/style.css` `:root` 동기화

라이트 토큰 (line ~30-37) 갱신:
```css
--text-primary: #1D1D1F;
--text-secondary: #6E6E73;
--text-muted: #8E8E93;

--accent: #007AFF;
--accent-text: #0066CC;     /* 신규 */
--accent-hover: #0063D1;
```

#### 7.3 `ui/web/style.css` 다크 모드 토큰 동기화

`@media (prefers-color-scheme: dark)` 또는 `[data-theme="dark"]` 블록의 `--text-muted` 갱신 + `--accent-text` 신규 추가.

위치는 `grep -n "prefers-color-scheme: dark\|data-theme=\"dark\"" ui/web/style.css` 로 확인.

#### 7.4 T-101 의 a11y 테스트 color-contrast 룰 재활성화

`tests/ui/a11y/test_empty_state.py` 의 `test_empty_state_no_a11y_violations` 함수에서:
- `"rules": {"color-contrast": {"enabled": False}}` 라인 제거
- docstring 의 deferral 메모 → "재활성화 완료 (T-103)" 변경
- assert 메시지의 `(color-contrast rule deferred to T-103)` → 일반 메시지로

#### 7.5 T-101 의 베이스라인 PNG 재캡처

`/tmp/recapture_t101_for_t103.py`:
```python
"""T-103 토큰 변경 후 T-101 베이스라인 재캡처."""
from pathlib import Path
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path("/Users/youngouksong/projects/meeting-transcriber")
PREVIEW_URL = (PROJECT_ROOT / "tests/ui/_fixtures/empty-state-preview.html").as_uri()
BASELINES = PROJECT_ROOT / "tests/ui/visual/baselines"

with sync_playwright() as p:
    browser = p.chromium.launch()
    for variant, vp, scheme in [
        ("light", {"width": 1024, "height": 768}, "light"),
        ("dark", {"width": 1024, "height": 768}, "dark"),
        ("mobile", {"width": 375, "height": 667}, "light"),
    ]:
        ctx = browser.new_context(viewport=vp, color_scheme=scheme)
        page = ctx.new_page()
        page.goto(PREVIEW_URL)
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(BASELINES / f"empty-state-{variant}.png"), full_page=True)
        ctx.close()
    browser.close()
```

Run: `.venv/bin/python /tmp/recapture_t101_for_t103.py`

#### 7.6 T-101 mockup §6 표 미세 수치 정정

Designer-B 가 검토 시 발견한 정정 사항 (mockup §6.1, §6.2 의 일부 수치 오기):
- §6.1 사이드바 위 설명 3.45:1 → 3.33:1
- §6.2 사이드바 위 설명 5.29:1 → 4.85:1

또한 §6 전체를 갱신 — 라이트 토큰 변경 후 새 측정값으로:
- text-secondary on canvas: 3.62 → **5.07:1** ✓
- text-muted on canvas: 2.21 → **3.51:1** ✓
- accent-text on canvas: 신규 **5.57:1** ✓

#### 검증

```bash
.venv/bin/python -m pytest tests/ui/ -v -m ui 2>&1 | tail -15
```
Expected:
- T-101: 10/10 PASS (베이스라인 재캡처 후 시각 통과 + 색대비 룰 재활성화 후 a11y 통과)
- T-103 (dark-mode-tones): 4/4 a11y PASS + 1 visual placeholder PASS = 5/5 PASS
- 합계: 15/15 PASS

#### 자가 검증 + 등록

```bash
python -m harness review record --ticket <TICKET> --agent frontend-a --kind self-check --status approved --note "라이트/다크 토큰 보강 + T-101 인계 정리(룰 재활성화 + 베이스라인 재캡처 + mockup §6 정정). 15/15 PASS."
```

### Step 8: Frontend-B 디스패치

검토:
- design.md §2.2 와 style.css 의 토큰 값 1:1 일치 (`grep` 으로 직접 검증)
- T-101 의 a11y 테스트에서 `"color-contrast": {"enabled": False}` 완전 제거
- 베이스라인 PNG 가 새 토큰으로 캡처됨 (sha256 변경 확인)
- mockup §6 정정 반영
- SPA 마크업 변경 0 (`git diff ui/web/spa.js` = empty)

```bash
python -m harness review record --ticket <TICKET> --agent frontend-b --kind peer-review --status approved
```

### Step 9: PM-B + Green gate

PM-B 검토 + merge-final approved → green gate 실행 → V✓ B✓ A✓.

### Step 10: PR 생성 + 머지 + close

```bash
gh pr create --base main --head feature/wave-1-dark-mode-tones \
  --title "기능: Wave 1 - dark-mode-tones (라이트 토큰 + T-101 인계 정리)" \
  --body "$(cat <<'PRBODY'
## Summary
- 라이트 모드 --text-secondary / --text-muted WCAG AA 통과
- --accent-text 신규 토큰 (본문 텍스트용 5:1+)
- 다크 모드 --text-muted UI 3:1 통과
- T-101 인계 정리: color-contrast 룰 재활성화 + 베이스라인 재캡처 + mockup §6 정정

## Test plan
- [x] tests/ui/a11y/test_dark_mode_tones.py: 4/4 PASS (토큰 직접 색대비 검증)
- [x] tests/ui/a11y/test_empty_state.py: 3/3 PASS (color-contrast 룰 재활성화 후)
- [x] tests/ui/visual/test_empty_state.py: 3/3 PASS (재캡처된 베이스라인)
- [x] T-101 의 mockup §6 정정 반영
- [x] SPA 마크업 변경 0

🤖 Generated with [Claude Code](https://claude.com/claude-code)
PRBODY
)"
```

머지 후:
```bash
python -m harness ticket close <TICKET> --pr <N>
python -m harness board rebuild
```

---

## Self-Review (Plan 작성자)

### Spec coverage
- [x] §3 Wave 1 항목 3 다크모드 톤 격차 (다크 `#1C1C1E/#2C2C2E/#3A3A3C` 검증) ✓
- [x] T-101 mockup §6.3 의 라이트 토큰 결함 인계 사항 ✓
- [x] §1.2 비목표 — 신규 의존성 없음 (Pillow + axe-playwright-python 만, 기존)

### Placeholder 스캔
- 모든 토큰 16진 값 명시 (TBD/TODO 없음)
- WCAG 측정값 직접 계산 결과 명시
- 재캡처 코드 풀로 포함

### Type 일관성
- 티켓 id `<TICKET>` placeholder 일관 사용 (실제는 발급된 id)
- review.py 의 kind/status enum 일관

---

## Execution Handoff

본 plan 도 Plan 1.1 처럼 Inline 실행 (메인 세션 = PM-A + Agent 툴 디스패치) 자연스러움.
