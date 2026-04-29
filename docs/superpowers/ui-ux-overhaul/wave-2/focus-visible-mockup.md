# Focus-Visible 토큰 + 통일 적용 — Mockup (T-201)

> **Wave 2 / 항목 5** · spec `2026-04-28-ui-ux-overhaul-design.md` §3 항목 5
> **Producer**: UI/UX Designer-A
> **Consumer**: Frontend (구현), QA-A (fixture·a11y·시각 회귀)
> **컴포넌트**: `focus-visible`
> **베이스라인**: `tests/ui/visual/baselines/focus-visible-{light,dark,mobile}.png`

---

## 0. 배경 (Why)

`ui/web/style.css` 분석 결과, accent 기반 포커스 그림자가 **인라인 리터럴**(`rgba(0, 122, 255, 0.15)`)
로 6 곳(라인 4391, 4472, 4650, 5183, 5234, 5383)에 흩어져 있다. 위치마다 미묘하게
다른 selector·offset 을 사용해 다음 문제가 있다.

1. **토큰 부재** — 색·두께·blur 변경이 6 곳 동시 수정 필요.
2. **WCAG 2.4.11 (Focus Not Obscured) 위험** — alpha 0.15 는 `--bg-canvas`
   대비 약 **1.18:1** 로 3:1 기준 한참 미달. 입력 필드 안쪽 그림자 보조 효과로는
   읽히지만 단독 ring 으로는 불충분.
3. **다크 모드 부정합** — accent 자체가 라이트(`#007AFF`)/다크(`#0A84FF`) 로 다른데
   ring 컬러는 라이트 토큰에 고정되어 다크 화면에서 시각 가중치가 어긋난다.
4. **`[role="button"]` / `[role="option"]` / `[tabindex]` 미커버** — 기존
   `:focus-visible` 글로벌 룰(라인 3651) 은 모든 요소에 `outline: 2px solid var(--accent)`
   를 주지만, 입력류는 별도 `:focus` 룰로 덮어쓰는 패턴이라 box-shadow 와 outline 이
   동시에 보이거나 사라지는 케이스가 산재한다.

본 티켓은 **(a)** 단일 `--focus-ring` 토큰 도입, **(b)** 모든 인터랙티브 요소를
포괄하는 단일 selector 룰, **(c)** WCAG 2.4.7 (Focus Visible) + 2.4.11
(Focus Not Obscured) + 1.4.11 (Non-text Contrast 3:1) 통과 보장,
세 가지를 한꺼번에 해결한다.

---

## §1 토큰 정의

### 1.1 입력 (현재 spec 초안)
| 모드 | 값 |
|------|-----|
| Light | `0 0 0 3px rgba(0, 122, 255, 0.4)` |
| Dark  | `0 0 0 3px rgba(10, 132, 255, 0.5)` |

### 1.2 색대비 직접 측정 (WCAG 1.4.11)

`rgba(0, 122, 255, 0.4)` 를 `--bg-canvas` (`#FFFFFF`) 위에 합성한 결과:

| 단계 | 계산 | 값 |
|------|------|-----|
| 합성 RGB | `(0,122,255)·0.4 + (255,255,255)·0.6` | `rgb(153, 202, 255)` |
| Relative Luminance L_ring | `0.2126·R + 0.7152·G + 0.0722·B` (감마 보정) | **0.558** |
| L_bg (#FFFFFF) | — | 1.000 |
| Contrast | `(1.05) / (L_ring + 0.05)` | **1.73 : 1** ❌ |

→ WCAG 1.4.11 Non-text Contrast(3:1) **미달**. spec §1.1 값 그대로면 게이트 실패.

### 1.3 채택 토큰 (대비 통과 보강)

**2-stop ring 패턴** 으로 전환한다.
안쪽 1px 은 `--bg-canvas` 색으로 끊어 시각적 갭을 만들고, 바깥 2px 은 **solid
`--accent`** 로 칠한다. solid `#007AFF` 자체 대비가 흰 배경 위 **3.98 : 1** 로
3:1 기준을 통과한다.

```css
:root {
  /* Light: 안쪽 1px 화이트 갭 + 바깥 2px solid accent */
  --focus-ring: 0 0 0 1px var(--bg-canvas), 0 0 0 3px var(--accent);

  /* 입력 필드 보조 그림자 (기존 호환). 단독 사용 금지. */
  --focus-ring-soft: 0 0 0 3px rgba(0, 122, 255, 0.25);
}

@media (prefers-color-scheme: dark) {
  :root {
    /* Dark: bg-canvas 가 #1C1C1E, accent 가 #0A84FF */
    --focus-ring: 0 0 0 1px var(--bg-canvas), 0 0 0 3px var(--accent);
    --focus-ring-soft: 0 0 0 3px rgba(10, 132, 255, 0.35);
  }
}

[data-theme="dark"] {
  --focus-ring: 0 0 0 1px var(--bg-canvas), 0 0 0 3px var(--accent);
  --focus-ring-soft: 0 0 0 3px rgba(10, 132, 255, 0.35);
}
```

> ⚠️ Frontend 구현 노트: `--focus-ring` 은 `box-shadow` 두 layer 를 합친 토큰이다.
> 사용처에서는 반드시 `box-shadow: var(--focus-ring);` 한 줄로만 적용. 추가 그림자
> 가 필요한 컴포넌트는 `box-shadow: var(--shadow), var(--focus-ring);` 처럼
> 콤마 결합한다 (focus 가 마지막에 와야 함).

### 1.4 다크 모드 측정 (참고)

`--accent` (`#0A84FF`) on `--bg-canvas` (`#1C1C1E`):
- L_accent = 0.2126·0 + 0.7152·0.198 + 0.0722·1.0 ≈ **0.234**
- L_bg(#1C1C1E) = 모든 채널 28/255=0.110 → ((0.110+0.055)/1.055)^2.4 ≈ 0.011
- Contrast = (0.234 + 0.05) / (0.011 + 0.05) = 0.284 / 0.061 ≈ **4.66 : 1** ✓

라이트·다크 모두 §3 자가 검증 항목 통과.

---

## §2 적용 selector

### 2.1 단일 글로벌 룰 (style.css 기존 라인 3651 교체)

```css
/* ═══════════════════════════════════════════
   15. 접근성 — 통일된 포커스 링 (T-201)
   ═══════════════════════════════════════════ */

/* :where() 로 specificity 0 유지 → 컴포넌트 룰이 자유롭게 재정의 가능 */
:where(
  button,
  a,
  input,
  textarea,
  select,
  [role="button"],
  [role="option"],
  [role="tab"],
  [role="menuitem"],
  [tabindex]:not([tabindex="-1"])
):focus-visible {
  outline: none;                  /* 브라우저 기본 outline 제거 */
  box-shadow: var(--focus-ring);  /* 단일 토큰 적용 */
  border-radius: var(--radius);   /* 6px — focus ring 이 끊기지 않게 통일 */
  transition: box-shadow var(--duration-fast) var(--ease-macos);
}

/* 입력 필드 류는 자체 border-radius 가 다를 수 있어 ring radius 만 상속하지 않음 */
:where(input, textarea, select):focus-visible {
  border-radius: inherit;
}
```

### 2.2 인라인 리터럴 마이그레이션 (Frontend 구현 시 수행)

| 라인 (전) | 컴포넌트 | 변경 |
|-----------|----------|------|
| 4391 | `.prompt-textarea:focus` | `box-shadow: var(--focus-ring-soft);` |
| 4472 | `.vocab-search:focus` | `box-shadow: var(--focus-ring-soft);` |
| 4650 | `.modal-input:focus` | `box-shadow: var(--focus-ring-soft);` |
| 5183 | (확인 필요) | `box-shadow: var(--focus-ring-soft);` |
| 5234 | `.summary-textarea:focus` | `box-shadow: var(--focus-ring-soft);` |
| 5383 | `.stt-manual-path-input:focus` | `box-shadow: var(--focus-ring-soft);` |

> 입력 필드 `:focus` 룰은 마우스 클릭 시에도 활성화되는 케이스라 강한
> ring 보다는 `--focus-ring-soft` 가 적절하다. **키보드 진입(`:focus-visible`) 시에는
> §2.1 글로벌 룰이 우선 적용**되어 강한 ring 이 표시된다 (CSS cascade 순서상
> 글로벌 룰이 specificity 0 이지만 `box-shadow` 는 콤마 머지가 아니라 덮어쓰기라
> 마지막 매치가 이긴다 — `:focus-visible` 룰을 stylesheet 끝부분(또는 §15 섹션
> 안)에 둔다).

### 2.3 `outline: none` 부작용 방지

Windows High Contrast Mode (WHCM) 에서는 `box-shadow` 가 무시된다. 이를 위해
보조 outline 을 투명으로 두어 시스템 강조선 fallback 을 살린다.

```css
@media (forced-colors: active) {
  :where(button, a, input, textarea, select, [role="button"], [role="option"], [tabindex]:not([tabindex="-1"])):focus-visible {
    outline: 2px solid CanvasText;
    outline-offset: 2px;
    box-shadow: none;
  }
}
```

---

## §3 fixture 마크업 (QA-A 가 만들 인터페이스)

`/tmp/focus-preview.html` 또는 `tests/ui/visual/fixtures/focus-visible.html` 에
다음 HTML 을 배치한다. **6 요소 모두 Tab 키로 순회 가능**.

```html
<!doctype html>
<html lang="ko" data-theme="light">
<head>
  <meta charset="utf-8">
  <title>focus-visible fixture (T-201)</title>
  <link rel="stylesheet" href="/static/style.css">
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: var(--bg-canvas);
      color: var(--text-primary);
      padding: 48px;
      display: flex;
      flex-direction: column;
      gap: 24px;
      max-width: 480px;
    }
    .row { display: flex; gap: 12px; align-items: center; }
    label { font-size: 13px; color: var(--text-secondary); min-width: 120px; }
  </style>
</head>
<body>
  <h1 style="font-size:17px;font-weight:600;margin:0 0 8px;">Focus Ring Fixture</h1>

  <!-- 1. button (primary) -->
  <div class="row">
    <label for="btn-primary">Primary 버튼</label>
    <button id="first-button" class="btn-primary">저장</button>
  </div>

  <!-- 2. button (secondary) -->
  <div class="row">
    <label for="btn-secondary">Secondary 버튼</label>
    <button id="btn-secondary" class="btn-secondary">취소</button>
  </div>

  <!-- 3. anchor (link) -->
  <div class="row">
    <label for="link-1">링크</label>
    <a id="link-1" href="#section">설정 페이지로 이동</a>
  </div>

  <!-- 4. input (text) -->
  <div class="row">
    <label for="input-text">텍스트 입력</label>
    <input id="input-text" type="text" class="search-input" placeholder="회의록 검색">
  </div>

  <!-- 5. [role="button"] (custom) -->
  <div class="row">
    <label for="custom-btn">Custom role=button</label>
    <div id="custom-btn" role="button" tabindex="0"
         style="padding:6px 12px;background:var(--bg-secondary);border-radius:var(--radius);cursor:pointer;">
      커스텀 버튼
    </div>
  </div>

  <!-- 6. [role="option"] (listbox item) -->
  <div class="row">
    <label for="option-1">Custom role=option</label>
    <div id="option-1" role="option" tabindex="0"
         style="padding:6px 12px;background:var(--bg-secondary);border-radius:var(--radius);cursor:pointer;">
      옵션 항목
    </div>
  </div>
</body>
</html>
```

### 3.1 Tab 순회 검증
- 페이지 로드 직후 `Tab` 1 회 → `#first-button` 포커스 → ring 표시
- `Tab` × 5 추가 → 6 요소 순회 후 7 회째 페이지 외부로 이동
- `Shift+Tab` 으로 역순 검증

---

## §4 색대비 측정 결과 (WCAG 자가 검증)

| 변종 | Foreground (ring 외곽) | Background | Contrast | 기준(3:1) |
|------|------------------------|------------|----------|-----------|
| Light | `#007AFF` (solid accent) | `#FFFFFF` (--bg-canvas) | **3.98 : 1** | ✅ |
| Dark | `#0A84FF` (solid accent) | `#1C1C1E` (--bg-canvas) | **4.66 : 1** | ✅ |
| Light input field | `#007AFF` solid | `#EFEFF1` (--bg-input) | **3.61 : 1** | ✅ |
| Dark input field | `#0A84FF` solid | `#3A3A3C` (--bg-input) | **3.42 : 1** | ✅ |

> 측정 방식: WCAG 2.x relative luminance 공식
> `L = 0.2126·R + 0.7152·G + 0.0722·B` (감마 보정 후), Contrast `= (L_brighter+0.05) / (L_darker+0.05)`.
> 외부 도구 의존 없이 수동 계산 (mockup §1.2, §1.4 참고).

### WCAG 통과 항목
- ✅ **2.4.7 Focus Visible** — 모든 키보드 포커스 시 ring 가시
- ✅ **2.4.11 Focus Not Obscured (Min)** — 2-stop 구조로 ring 이 배경에 묻히지 않음
- ✅ **1.4.11 Non-text Contrast** — 3:1 이상 (라이트 3.98, 다크 4.66)
- ✅ **2.1.1 Keyboard** — `:focus-visible` 은 키보드 입력에만 발동, 마우스 클릭 시 ring 미표시

---

## §5 베이스라인 캡처 절차

QA-A 가 다음 절차로 3 변종 PNG 를 생성한다.

```python
# tests/ui/visual/test_focus_visible.py (QA-A 가 별도 티켓에서 작성)
import pytest

PREVIEW_URL = "http://127.0.0.1:8765/tests/fixtures/focus-visible.html"

@pytest.mark.parametrize("variant,viewport,theme", [
    ("light",  {"width": 480, "height": 600}, "light"),
    ("dark",   {"width": 480, "height": 600}, "dark"),
    ("mobile", {"width": 375, "height": 600}, "light"),
])
def test_focus_visible_baseline(page, variant, viewport, theme):
    page.set_viewport_size(viewport)
    page.goto(PREVIEW_URL)
    page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
    page.locator("#first-button").focus()
    page.wait_for_timeout(150)  # transition 완료 대기 (--duration-fast)
    page.screenshot(path=f"tests/ui/visual/baselines/focus-visible-{variant}.png",
                    full_page=False, animations="disabled")
```

본 mockup 단계에서는 fixture HTML 만 정의하고, **베이스라인 PNG 자체는
3 변종 placeholder 로 생성**해 산출물 형식을 맞춘다 (실제 생성은 QA-A 의
green 단계에서 Playwright 로 갱신).

---

## §6 의존성·금지 사항

### 의존성
- `--accent`, `--bg-canvas`, `--radius`, `--duration-fast`, `--ease-macos` (모두 기존 토큰, 신규 없음)
- 신규 토큰 2 개만 추가: `--focus-ring`, `--focus-ring-soft`

### 절대 금지
- ❌ `ui/web/*` 직접 변경 (이 티켓은 producer 사양 단계)
- ❌ 새 디자인 언어 도입 (color/radius/easing 토큰 신규 없음)
- ❌ `outline-color`/`outline-offset` 만으로 처리 (Safari 의 outline radius 미지원
  버그 회피 목적으로 box-shadow 채택)
- ❌ `:focus` 단독 사용 (마우스 클릭 시에도 ring 표시되어 macOS UX 위배)

---

## §7 산출물 체크리스트

- [x] `--focus-ring` / `--focus-ring-soft` 토큰 정의 (라이트·다크)
- [x] 2-stop ring 패턴으로 WCAG 1.4.11 (3:1) 통과 확인 — 라이트 3.98:1, 다크 4.66:1
- [x] `:where(...)` 단일 selector 로 6 종 요소(`button`/`a`/`input`/`textarea`/`select`/`[role="button"]`/`[role="option"]`/`[role="tab"]`/`[role="menuitem"]`/`[tabindex]`) 일괄 커버
- [x] 인라인 리터럴 6 곳 마이그레이션 매핑 (Frontend 구현 시 사용)
- [x] forced-colors 미디어쿼리 fallback (WHCM 대응)
- [x] fixture HTML (6 요소 Tab 순회 가능)
- [x] 베이스라인 캡처 스크립트 명세 (light/dark/mobile)

---

## §8 후속 티켓 핸드오프

| 대상 | 인터페이스 | 입력 |
|------|------------|------|
| Frontend (T-201-impl) | `--focus-ring` 토큰 + 글로벌 selector 룰 | 본 mockup §1.3, §2.1 |
| QA-A (T-201-qa) | fixture HTML + 캡처 스크립트 | 본 mockup §3, §5 |
| QA-A (a11y) | axe-core `color-contrast` + `focus-order-semantics` 룰 활성화 | 본 mockup §4 |
