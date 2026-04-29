# 모바일 반응형 진입로 — Mockup (T-302)

> **Wave 3 / 항목 7** · spec `2026-04-28-ui-ux-overhaul-design.md` §3 항목 7
> **Producer**: UI/UX Designer-A
> **Consumer**: Frontend (구현), QA-A (fixture · a11y · 시각 회귀)
> **컴포넌트**: `mobile-responsive`
> **베이스라인**: `tests/ui/visual/baselines/mobile-responsive-{closed,open}.png`

---

## §1 목적 (Why)

768px 이하 모바일 뷰포트에서 사이드바를 **햄버거 토글로 열고 닫는 drawer (시트)** 로
전환한다. 데스크톱(769px 이상)에서는 일체의 영향 없음. 기존 `style.css:3717~3758`
의 모바일 분기는 사이드바 폭/패딩 정리 정도에 그쳤기 때문에, 본 티켓에서
드로어 토글 + 백드롭 + 키보드/포커스 처리 까지를 단일 계약으로 통일한다.

핵심 사용자 가치:
- 좁은 화면에서 회의 목록(사이드바)이 콘텐츠 영역을 잠식하지 않음
- 햄버거 → drawer 패턴은 macOS Safari/Chrome 모바일 사용자에게 친숙
- 키보드만으로도 ESC/Tab 으로 탈출 가능 (WCAG 2.1.2 No Keyboard Trap)

---

## §2 사용 토큰 (변경 0)

신규 토큰을 만들지 않는다. 기존 `style.css :root` 의 변수만 사용.

| 토큰 | 용도 |
|------|------|
| `--bg-sidebar` | drawer 본체 배경 |
| `--bg-canvas` | 콘텐츠 영역 배경 (변화 없음) |
| `--border` | drawer 내부 구분선 (기존과 동일) |
| `--accent` | 햄버거 버튼 hover/focus 강조 |
| `--ease-macos` | drawer slide-in/out easing (`cubic-bezier(0.25, 0.46, 0.45, 0.94)`) |
| `--duration-base` | drawer transition 시간 (`250ms`) |
| `--shadow-lg` | 열린 drawer 의 그림자 (`0 8px 24px rgba(0,0,0,0.12)`) |
| `--radius` | 햄버거 버튼 hover 배경 모서리 |

> ⚠️ **신규 토큰 금지** — Wave 1 의 token 검증을 그대로 통과해야 한다.

---

## §3 마크업 인터페이스 (Frontend 가 따를 명세)

본 mockup 의 단일 진실 공급원. spa.js 가 동적으로 만드는 사이드바와 통합 시
다음 마크업 계약을 그대로 따른다.

### §3.1 햄버거 버튼

```html
<button
  id="mobile-menu-toggle"
  class="mobile-menu-toggle"
  type="button"
  aria-label="메뉴 열기"
  aria-expanded="false"
  aria-controls="sidebar"
>
  ☰
</button>
```

- `aria-controls` 는 사이드바 id (`sidebar`) 를 가리킨다.
- `aria-expanded` 는 drawer 의 현재 열림 상태와 항상 동기화 (string `"true"` / `"false"`).
- `aria-label` 은 상태에 따라 `"메뉴 열기"` / `"메뉴 닫기"` 토글.

### §3.2 사이드바 (drawer 가 되는 본체)

```html
<aside id="sidebar" aria-label="회의 목록">
  <!-- 회의 목록 등 기존 콘텐츠 -->
</aside>
```

- **시각적 토글 상태는 `.is-open` 클래스로 관리** — `<aside>` 의 native role
  은 `complementary` 이며, axe `aria-allowed-attr` 룰이 `aria-expanded`,
  `aria-modal` 을 `complementary` 에서 거부한다. 따라서 사이드바에는 ARIA
  상태 속성을 **부여하지 않고** 시각적 변화는 클래스로만 한다.
- 닫힘 상태: `class=""`, CSS 가 `transform: translateX(-100%)` 적용
- 열림 상태: `class="is-open"`, CSS 가 `transform: translateX(0)` 적용
- 데스크톱(>=769px) 에서는 CSS 가 `transform` 을 무력화 → 기존 레이아웃 유지

> 💡 **진실의 원천(source of truth) 은 햄버거 버튼의 `aria-expanded`** —
> 보조 기술은 햄버거 버튼을 통해 drawer 의 열림/닫힘을 인지한다 (mockup §6).
> 사이드바에 중복 ARIA 속성을 두면 axe 위반 + 스크린리더 발화 중복 위험.

### §3.3 백드롭

```html
<div class="drawer-backdrop" id="drawer-backdrop"></div>
```

- 열림 상태에서만 `.visible` 클래스 부여 → opacity 1 + pointer-events auto
- 클릭 시 drawer 를 닫는 핸들러 부착

### §3.4 마크업 트리 요약

```
header
  └─ mobile-menu-toggle       (모바일에서만 표시)
drawer-backdrop                (열림 상태에서만 보임)
aside#sidebar                  (모바일: drawer / 데스크톱: 영구 사이드바)
main                            (콘텐츠 — 변화 없음)
```

---

## §4 CSS 패턴

`style.css:3717~3758` 의 모바일 분기를 다음으로 보강한다.

```css
/* ────────────────────────────────────────────────────────────────────────
 * 모바일 (≤768px) — 햄버거 + drawer 패턴
 * ──────────────────────────────────────────────────────────────────────── */
@media (max-width: 768px) {
  .mobile-menu-toggle {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: none;
    padding: 8px;
    cursor: pointer;
    color: var(--text-primary);
    font-size: 18px;
    border-radius: var(--radius);
    transition: background var(--duration-fast) var(--ease-macos);
  }
  .mobile-menu-toggle:hover { background: var(--bg-hover); }
  .mobile-menu-toggle:focus-visible { box-shadow: var(--focus-ring); outline: none; }

  #sidebar {
    position: fixed;
    top: 0;
    left: 0;
    height: 100vh;
    width: 280px;
    transform: translateX(-100%);
    transition: transform var(--duration-base) var(--ease-macos);
    z-index: 100;
    background: var(--bg-sidebar);
  }
  #sidebar.is-open {
    transform: translateX(0);
    box-shadow: var(--shadow-lg);
  }

  .drawer-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.4);
    z-index: 99;
    opacity: 0;
    pointer-events: none;
    transition: opacity var(--duration-base) var(--ease-macos);
  }
  .drawer-backdrop.visible {
    opacity: 1;
    pointer-events: auto;
  }
}

/* ────────────────────────────────────────────────────────────────────────
 * 데스크톱 (≥769px) — drawer 비활성, 기존 레이아웃 유지
 * ──────────────────────────────────────────────────────────────────────── */
@media (min-width: 769px) {
  .mobile-menu-toggle { display: none; }
  .drawer-backdrop { display: none; }
}
```

> 💡 **transform 기반 슬라이드** — `display: none/block` 토글 대신 transform 을
> 사용해야 transition 이 매끄럽게 보인다. transform 은 GPU 가속이라 60fps 보장.

> 💡 **z-index 99/100** — backdrop 99, drawer 100 으로 drawer 가 위에 떠야
> 하므로 순서 고정.

---

## §5 인터랙션

| 트리거 | 결과 |
|--------|------|
| 햄버거 클릭 (닫힘 상태) | drawer 열림, 첫 항목으로 focus 이동, `body { overflow: hidden }` 적용 |
| 햄버거 클릭 (열림 상태) | drawer 닫힘, 햄버거 버튼으로 focus 복귀, `body.overflow` 복원 |
| ESC 키 (열림 상태) | drawer 닫힘, 햄버거 버튼으로 focus 복귀 |
| 백드롭 클릭 (열림 상태) | drawer 닫힘, 햄버거 버튼으로 focus 복귀 |
| Tab 순환 (열림 상태) | drawer 내부 첫↔끝 항목에서 순환 (focus trap) |
| 768px → 769px 리사이즈 | drawer 자동 닫힘 (CSS 미디어 쿼리가 transform 무력화) |

### §5.1 body scroll lock

drawer 가 열려 있을 때 콘텐츠 영역 스크롤이 따라 움직이면 사용자 혼란.
열림 시 `document.body.style.overflow = "hidden"`, 닫힘 시 `""` 로 복원.

### §5.2 focus trap

```javascript
// 의사코드 (Frontend 가 구현)
function trapFocus(drawer) {
  const focusables = drawer.querySelectorAll(
    'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'
  );
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  drawer.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    if (e.shiftKey && document.activeElement === first) {
      last.focus(); e.preventDefault();
    } else if (!e.shiftKey && document.activeElement === last) {
      first.focus(); e.preventDefault();
    }
  });
}
```

---

## §6 a11y

### §6.1 ARIA 속성

| 속성 | 위치 | 값 / 트리거 |
|------|------|-------------|
| `aria-expanded` | 햄버거 버튼 (단일 진실의 원천) | drawer 상태와 항상 동기화 (string `"true"`/`"false"`) |
| `aria-controls` | 햄버거 버튼 | 사이드바 id (`sidebar`) |
| `aria-label` | 햄버거 버튼 | 상태별 토글 (`"메뉴 열기"` / `"메뉴 닫기"`) |
| `aria-label` | 사이드바 | `"회의 목록"` (정적) |
| (시각 토글) | 사이드바 | `.is-open` 클래스 — ARIA 속성 아님 |

> ⚠️ **사이드바에 `aria-expanded`/`aria-modal` 부여 금지** —
> `<aside>` 의 native role 은 `complementary` 이며 axe `aria-allowed-attr`
> 룰이 두 속성 모두 거부한다. ARIA 표준상 `aria-modal` 은 `dialog`/
> `alertdialog` role 에서만 유효, `aria-expanded` 도 `complementary` 미허용.
> 따라서 시각 상태는 `.is-open` 클래스로 토글하고 ARIA 속성은 햄버거 버튼의
> `aria-expanded` 만 진실의 원천(source of truth) 으로 둔다.

### §6.2 표준 준수

- WCAG 2.1.2 **No Keyboard Trap** — ESC 또는 Tab 으로 탈출 가능
- WCAG 2.4.3 **Focus Order** — drawer 열림 시 첫 focusable 로 focus 이동
- WCAG 4.1.2 **Name, Role, Value** — 햄버거 버튼은 `<button>` native + `aria-label` + `aria-expanded`

### §6.3 focus return

drawer 닫힘 시 항상 햄버거 버튼으로 focus 복귀. 사용자가 drawer 안에서
ESC 를 눌렀거나 백드롭을 클릭한 경우 모두 동일.

```javascript
const close = () => {
  // ... drawer 닫기 로직 ...
  toggleBtn.focus();  // 반드시 마지막에 호출
};
```

### §6.4 axe-core 룰셋

QA-A 가 실행할 룰 (DEFAULT_RULESET = wcag2a/aa/21aa) 에서 위반 0 건이어야 한다.
특히 다음이 통과해야 함:

- `aria-valid-attr-value` — `aria-expanded` 값은 string `"true"`/`"false"`
- `aria-required-parent` — 사이드바 자체는 부모 요구 없음
- `button-name` — 햄버거 버튼의 `aria-label` 또는 텍스트로 접근 가능 이름 보장
- `color-contrast` — 햄버거 버튼 텍스트 vs 배경 4.5:1 이상

---

## §7 베이스라인 캡처

본 티켓은 **2 변종 (closed / open)** 을 모두 모바일 뷰포트(375×667 @ DPR=2)
에서 캡처한다. light/dark/mobile 의 3 변종 패턴 대신, "drawer 닫힘 vs 열림"
의 상태 차이가 핵심 시각 단서이기 때문이다.

| 변종 | viewport | DPR | 캡처 PNG | 핵심 시각 단서 |
|------|----------|-----|----------|---------------|
| `closed` | 375×667 | 2 | `mobile-responsive-closed.png` (750×1334) | 햄버거만 보임, 사이드바·백드롭 미표시 |
| `open` | 375×667 | 2 | `mobile-responsive-open.png` (750×1334) | drawer 좌측 슬라이드인, 백드롭 dim, 햄버거 `aria-expanded="true"` |

### §7.1 캡처 절차

QA-A 의 `tests/ui/visual/test_mobile_responsive.py` 가 다음을 수행한다:

1. `mobile-responsive-preview.html` 을 file:// 로 로드
2. viewport 375×667, DPR=2 로 컨텍스트 생성
3. `closed` 변종: 페이지 로드 직후 캡처
4. `open` 변종: `#mobile-menu-toggle` 클릭 → transition (~250ms) 대기 → 캡처

### §7.2 baseline_path() 헬퍼 분기

`harness/snapshot.py` 의 `baseline_path()` 헬퍼는 SUPPORTED_VARIANTS = (light,
dark, mobile) 만 허용하므로, 본 티켓은 **baseline 경로를 직접 구성**한다
(`Path("tests/ui/visual/baselines") / f"mobile-responsive-{variant}.png"`).
`assert_visual_match()` 는 baseline path 를 인자로 받기 때문에 직접 경로
구성으로도 픽셀 비교가 동일하게 동작한다.

---

## §8 핸드오프

| 대상 | 인터페이스 | 입력 |
|------|------------|------|
| Frontend (T-302-impl) | `spa.js` 또는 `app.js` 에 햄버거 토글 + ESC 핸들러 + body scroll lock + focus trap + focus return | 본 mockup §3, §5, §6 |
| Frontend (CSS) | `style.css:3717~3758` 의 모바일 분기에 §4 패턴 추가 | 본 mockup §4 |
| QA-A (T-302-qa) | `mobile-responsive-preview.html` fixture + Playwright 베이스라인 2 변종 + 행동 시나리오 5 + axe 룰셋 | 본 mockup §3, §5, §6, §7 |
| Designer-B (review) | 마크업 인터페이스가 spec §3 항목 7 의 "햄버거 → 사이드바 시트" 요구를 모두 충족하는지 검토 | 본 mockup §1, §3, §5, §6 |

### §8.1 절대 금지

- ❌ 신규 디자인 토큰 추가 (Wave 1 token 검증 무력화)
- ❌ `display: none/block` 으로 drawer 토글 (transition 부드럽지 않음)
- ❌ 769px 이상에서 햄버거 표시 (사용자 혼란)
- ❌ `aria-expanded="true"/"false"` 외 값 사용 (axe `aria-valid-attr-value` 위반)
- ❌ drawer 열림 시 body 스크롤 허용 (UX 혼란)
- ❌ drawer 닫힘 시 focus 를 다른 곳에 두기 (WCAG 2.4.3 위반)
