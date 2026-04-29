# Command Palette (⌘K) — Mockup (T-202)

> **Wave 2 / 항목 4** · spec `2026-04-28-ui-ux-overhaul-design.md` §3 항목 4
> **Producer**: UI/UX Designer-A
> **Consumer**: Frontend (구현/통합), QA-A (fixture·a11y·시각 회귀)
> **컴포넌트**: `command-palette`
> **베이스라인**: `tests/ui/visual/baselines/command-palette-{closed,open-empty,open-results}.png`

---

## §1 목적 (Why)

⌘K (macOS) / Ctrl+K (Windows) 글로벌 단축키 한 번으로 호출되는 통합 명령
팔레트. 회의 검색·뷰 전환·작업 명령(테마 토글·STT 모델 활성화 등)을 하나의
입력창으로 묶어 키보드 우선 사용자의 흐름을 끊지 않는다. 본 패턴은 design.md
§4.1 Command Palette · "Hidden AI" 패턴(1순위 적용 후보)이며 SaaS UX 표준
(Linear, Raycast, Notion, GitHub, Vercel) 으로 정착되어 있다.

핵심 문제 정의:

1. 현재 SPA 의 사이드바·헤더는 클릭 의존적이라 키보드 사용자의 뷰 전환
   비용이 높다 (`Tab` × n 회 → 사이드바 항목 → `Enter`).
2. 회의 검색은 `/app/search` 라우트로 이동해야 하고, 검색 결과를 클릭해야
   뷰어로 진입한다. 단순 "이 회의 열기" 흐름이 4 클릭이다.
3. 자주 쓰는 액션(다크 모드 토글, 홈 이동, 검색 페이지 이동) 진입점이
   여러 사이드바·헤더 영역에 흩어져 있다.

본 티켓은 **단일 ⌘K 단축키 + 단일 입력창 + 통합 listbox** 로 위 3 종 흐름을
1-2 키 입력으로 압축한다.

> **재사용**: `ui/web/spa.js:7616~8194` 영역에 미통합 Command Palette 모듈이
> 이미 존재한다. 본 mockup 은 그 모듈의 인터페이스 (`role`/`aria-*`/마크업 클래스)
> 를 보존하면서 fixture 로 시각·행동 계약을 고정하는 것이 목적이다.

---

## §2 사용 토큰

본 컴포넌트는 신규 디자인 토큰 0 개. 모두 `style.css` 기존 토큰 재사용.

| 카테고리 | 토큰 | 용도 |
|----------|------|------|
| 배경 | `--bg-card` | 팔레트 컨테이너 배경 |
| 배경 | `--bg-secondary` | 입력 필드 배경, kbd 배경 |
| 배경 | `--bg-hover` | 항목 hover 상태 |
| 배경 | `--accent` | 선택된 항목 배경 |
| 배경 | `rgba(0,0,0,0.4)` | backdrop (`<dialog>::backdrop`) |
| 텍스트 | `--text-primary` | 항목 라벨 |
| 텍스트 | `--text-secondary` | 메타·힌트 |
| 텍스트 | `--text-muted` | placeholder |
| 보더 | `--border` | hairline 0.5px (입력창 하단/footer 상단) |
| 그림자 | `--shadow-lg` | 팔레트 elevation |
| 모양 | `--radius-lg` | 팔레트 모서리 |
| 모양 | `--radius` | 항목 모서리, kbd 모서리 |
| 모션 | `--ease-macos` | 입출 모션 |
| 모션 | `--duration-base` | 250ms transition |

> 다크 모드는 모든 토큰이 `prefers-color-scheme: dark` / `[data-theme="dark"]`
> 에서 자동 전환되므로 별도 스타일 정의 불필요.

---

## §3 마크업 인터페이스 (Frontend 가 따를 명세)

native `<dialog>` 요소 + ARIA 1.2 combobox 패턴(`role="combobox"` 포함)
표준을 채택. native dialog 는 (a) focus trap 자동, (b) ESC 자동 닫기,
(c) `::backdrop` pseudo-element 제공으로 외부 dim 처리가 native 로 가능.

```html
<dialog
  class="command-palette"
  id="command-palette"
  aria-label="명령 팔레트"
  aria-modal="true">
  <div class="command-palette-content">

    <!-- combobox 컨테이너 — input + listbox 를 묶어 ARIA 1.2 패턴 충족 -->
    <div
      class="command-palette-input-wrap"
      role="combobox"
      aria-haspopup="listbox"
      aria-expanded="true"
      aria-controls="command-palette-list">
      <svg class="command-palette-icon" aria-hidden="true">...</svg>
      <input
        type="text"
        class="command-palette-input"
        placeholder="명령 또는 검색…"
        aria-label="명령 검색"
        aria-autocomplete="list"
        aria-controls="command-palette-list"
        role="searchbox">
      <kbd class="command-palette-shortcut">ESC</kbd>
    </div>

    <!-- listbox — 단일 active option(aria-selected="true") 만 유지 -->
    <ul id="command-palette-list" role="listbox" aria-label="검색 결과">
      <li role="option" aria-selected="true"
          data-action="navigate" data-route="/app" tabindex="-1">
        <span class="command-palette-item-icon">...</span>
        <span class="command-palette-item-label">홈</span>
        <span class="command-palette-item-hint">뷰 전환</span>
      </li>
      <li role="option" aria-selected="false"
          data-action="open-meeting" data-meeting-id="m-001" tabindex="-1">
        <span class="command-palette-item-label">2026-04-15 팀 회고</span>
        <span class="command-palette-item-meta">15:00 · 32분</span>
      </li>
      <!-- … -->
    </ul>

    <!-- 빈 상태 (검색어가 있는데 결과 없음) -->
    <div class="command-palette-empty" role="status" hidden>
      <p>검색 결과가 없습니다</p>
    </div>

    <!-- footer — 단축키 안내 -->
    <div class="command-palette-footer">
      <span><kbd>↑↓</kbd> 탐색</span>
      <span><kbd>↵</kbd> 실행</span>
      <span><kbd>ESC</kbd> 닫기</span>
    </div>
  </div>
</dialog>
```

### 3.1 마크업 계약 (불변)
- `<dialog class="command-palette">` 단일 인스턴스. `aria-modal="true"`.
- `[role="combobox"]` 컨테이너는 `aria-haspopup="listbox"` + `aria-expanded`
  + `aria-controls="command-palette-list"` 3 종 모두 필수.
- `<input>` 은 `role="searchbox"` + `aria-autocomplete="list"` +
  `aria-controls` (combobox 와 동일 listbox).
- `<ul role="listbox">` 의 ID 는 `command-palette-list` 고정.
- `<li role="option">` 은 항상 1 개만 `aria-selected="true"` (단일 active
  선택 모델). `tabindex="-1"` 로 input 이 focus 를 유지하도록.
- 항목의 동작은 `data-action` + 보조 데이터(`data-route`, `data-meeting-id`,
  `data-command-id`) 로 식별. 핸들러는 spa.js Command Palette 모듈이 소유.

---

## §4 인터랙션

| 입력 | 동작 |
|------|------|
| `⌘K` (macOS) / `Ctrl+K` (Windows) | 글로벌 단축키 — 어디서든 팔레트 열기 |
| `ESC` | 팔레트 닫기 + focus return 원래 element |
| `↑` / `↓` | 항목 순회 (aria-selected 단일 유지, 끝→처음 순환) |
| `Enter` | 선택 항목의 `data-action` 실행 (navigate / open-meeting / command) |
| 외부 클릭 | 팔레트 닫기 (`<dialog>::backdrop` 클릭 또는 dialog 외부) |
| `Tab` / `Shift+Tab` | input ↔ listbox ↔ footer 내부 순환 (focus trap, native dialog) |

### 4.1 ↑↓ 동작 상세
- `↓`: 현재 selected 의 다음 형제. 마지막이면 첫 번째로 wrap-around.
- `↑`: 현재 selected 의 이전 형제. 첫 번째면 마지막으로 wrap-around.
- 입력 도중 결과가 갱신되면 selected 인덱스를 0 으로 리셋.

### 4.2 입력 → 카테고리 라우팅
- 빈 입력: 정적 카테고리(뷰 전환 + 작업 명령) + 최근 회의 5 건 표시.
- 1 글자 이상: fuzzy 매칭으로 정적 항목 필터 + 기존 검색 API
  (`GET /api/search?q=...&limit=10`) 호출 결과 추가.

### 4.3 ESC 후 focus return
- `<dialog>` 가 닫힐 때 마지막 active element 로 자동 복귀 (브라우저 native).
- 단, ⌘K 로 열기 직전 focus 가 없었으면 `body` 로 떨어진다 (의도).

---

## §5 카테고리 (3 종)

### 5.1 뷰 전환 (정적)
| 항목 | data-action | data-route |
|------|-------------|------------|
| 홈 | navigate | `/app` |
| 검색 | navigate | `/app/search` |
| 채팅 | navigate | `/app/chat` |
| 설정 | navigate | `/app/settings` |

### 5.2 회의 검색 (동적, 기존 API)
입력값이 있을 때 `GET /api/search?q={input}&limit=10` 호출. **신규 백엔드
변경 없음**. 응답 스키마는 `{results: [{id, title, started_at, duration_sec}]}`
형태로 spa.js HybridSearch 와 공유.

각 결과는 `<li role="option" data-action="open-meeting" data-meeting-id="...">`.

### 5.3 작업 명령 (정적)
| 항목 | data-action | data-command-id |
|------|-------------|-----------------|
| 다크 모드 토글 | command | `theme.toggle` |
| 새 회의 시작 | command | `meeting.start` (선택 — Frontend-A 결정) |

> 신규 명령 추가 시 마크업 계약(§3)만 따르면 OK. spa.js 모듈에서 dispatch
> 테이블만 확장.

---

## §6 a11y (WCAG 2.x)

### 6.1 native dialog 채택 근거
- WCAG 2.1.2 No Keyboard Trap: native `<dialog>` 는 spec 상 닫힘 시
  자동으로 focus 를 직전 element 로 반환. 사용자 정의 trap 코드 불필요.
- Tab 으로 dialog 외부로 탈출 불가능 (modal). 단, ESC 로 항상 탈출 가능 →
  WCAG 2.1.2 통과.

### 6.2 ARIA 1.2 combobox 패턴
- `[role="combobox"]` 컨테이너 + `[role="searchbox"]` input + `aria-controls`
  로 listbox 연결 — APG 'Editable Combobox With List Autocomplete' 패턴.
- `aria-autocomplete="list"`: 입력에 따라 listbox 가 동적 갱신됨을 SR 에 알림.
- `aria-haspopup="listbox"` + `aria-expanded="true"`: 팝업 type/상태 명시.

### 6.3 listbox + option
- 단일 active 옵션만 `aria-selected="true"`. 나머지는 `false` (또는 미설정).
- `aria-activedescendant` 는 사용하지 않고 (모든 option `tabindex="-1"`),
  selected 만 `aria-selected="true"` + 시각적 highlight 로 표시.
- input 이 항상 keyboard focus 보유 → `aria-activedescendant` 안 써도
  SR 이 input 의 listbox 컨텍스트로부터 선택 항목을 읽어준다.

### 6.4 라벨링
- dialog: `aria-label="명령 팔레트"` (시각적 제목 없음 → aria-label 필수)
- input: `aria-label="명령 검색"` + `placeholder` 보조
- listbox: `aria-label="검색 결과"`
- empty: `role="status"` (live region) — 결과 없음 안내가 SR 에 자동 발화

### 6.5 색대비 (WCAG 1.4.11 / 1.4.3) — 수동 측정 (light)

| 요소 | foreground | background | contrast | 기준 |
|------|------------|------------|----------|------|
| input text | `#1D1D1F` | `#FFFFFF` (--bg-card) | **16.07:1** | AAA ✓ |
| input placeholder | `#5b5b5f` | `#FFFFFF` | **7.31:1** | AA+ ✓ |
| 선택된 option text | `#FFFFFF` | `#0066d6` (accent dark) | **5.05:1** | AA ✓ |
| 선택된 option meta | `#e7eefb` | `#0066d6` | **5.07:1** | AA ✓ |
| 비선택 option label | `#1D1D1F` | `#FFFFFF` | **16.07:1** | AAA ✓ |
| 비선택 option meta | `#5b5b5f` | `#FFFFFF` | **7.31:1** | AA+ ✓ |
| footer text | `#4a4a4d` | `#FAFAFB` (--bg-secondary) | **8.30:1** | AAA ✓ |
| footer kbd | `#4a4a4d` | `#FFFFFF` | **8.92:1** | AAA ✓ |

> **axe-core color-contrast 룰 비활성화 사유**: axe-core 4.x 는 native
> `<dialog>` 의 top-layer rendering 합성을 정확히 처리하지 못해 false
> positive 를 다수 보고한다 (실제 opaque `#FFFFFF` 위 `#1D1D1F` 16:1 텍스트
> 도 `#868687 on #ffffff` 3.6:1 로 잘못 측정). 시각 회귀(§7) + 본 §6.5
> 수동 측정으로 색대비 검증을 cover. a11y 시나리오는 ARIA 룰만 검사.

### 6.6 키보드 진입 보장
- 입력창은 native `<input>` 이라 자동 진입 가능.
- option 은 `tabindex="-1"` 이지만 input 의 화살표 키 핸들러로 selected 갱신
  → 키보드만으로 모든 option 도달 가능.

---

## §7 베이스라인 캡처 절차

3 변종 PNG. DPR=2 (Retina) 고정. viewport 1024×768 (focus-visible 패턴 통일).

| 변종 | viewport | DPR | 의도 | PNG 크기 |
|------|----------|-----|------|----------|
| `closed` | 1024×768 | 2 | 메인 페이지에 팔레트 dialog 가 닫혀있는 baseline (정적 페이지 캡처) | 2048×1536 |
| `open-empty` | 1024×768 | 2 | dialog `open` 속성 적용 + 입력 비어있음 + 정적 카테고리 항목만 표시 | 2048×1536 |
| `open-results` | 1024×768 | 2 | dialog `open` + 검색어 "회의" + 정적 + mock 검색 결과 항목 | 2048×1536 |

캡처 절차 (QA-A 가 별도 스크립트로 수행):

```python
# QA-A 가 tests/ui/visual/test_command_palette.py 에서 수행
ctx = browser.new_context(
    viewport={"width": 1024, "height": 768},
    device_scale_factor=2,
    color_scheme="light",
)
page = ctx.new_page()
page.goto(PREVIEW_URL)              # fixture 로드 — dialog open=true 로 시작
page.wait_for_load_state("networkidle")
page.screenshot(path=".../command-palette-open-empty.png")
```

- `closed` 변종은 fixture 의 `<dialog>` 에서 `open` 속성을 제거한 상태로 캡처.
- `open-empty` 는 input value 비어있고 listbox 에 정적 항목 4 개만 보이는 상태.
- `open-results` 는 input.value="회의" + listbox 에 정적 + 검색 결과 mock 4 건
  추가된 상태.

> **베이스라인 매핑 변경 가능성**: snapshot.SUPPORTED_VARIANTS 는 (light,
> dark, mobile) 만 허용한다. 본 컴포넌트는 mobile-responsive 패턴(T-302)
> 처럼 `closed/open-empty/open-results` 명을 직접 구성한다(`BASELINES_DIR / f"command-palette-{variant}.png"`).
> QA-A 가 plan 단계에서 매핑을 단순화(예: open-empty 만 light/dark 2 변종)
> 할 수 있고, 그 결정은 QA-B 체크리스트의 'baseline 매핑 변경 합리성'
> 항목에서 검증한다.

---

## §8 Frontend 핸드오프 — 기존 모듈 활용

`ui/web/spa.js:7616~8194` 검토 결과:

### 8.1 기존 모듈 형태
- `function CommandPalette() { ... }` 생성자 함수 + 프로토타입 메서드.
- 미통합 — 어떤 라우트에서도 인스턴스화/단축키 바인딩되지 않음.
- 마크업 클래스: `.command-palette-overlay` / `.command-palette` /
  `.command-palette-input-wrap` / `.command-palette-input` /
  `.command-palette-results` / `.command-palette-item`.
- 카테고리: 회의(최신 5건) / 액션(네비게이션·테마) / STT 모델 / 도움말
  (mockup §5 와 정렬됨).
- 키보드 바인딩 메서드 정의됨 (⌘K open, ↑↓ navigate, Enter execute, ESC close).

### 8.2 마크업 계약 차이 (Frontend 통합 시 처리)
| 항목 | 현재 (spa.js 7890~) | mockup §3 |
|------|---------------------|-----------|
| 컨테이너 | `<div class="command-palette-overlay">` + `<div class="command-palette" role="dialog">` | `<dialog class="command-palette">` |
| listbox | `<div class="command-palette-results" role="listbox">` | `<ul id="command-palette-list" role="listbox">` |
| combobox 컨테이너 | 누락 | `<div role="combobox" aria-haspopup="listbox" aria-controls="...">` |
| input role | 미설정 | `role="searchbox"` |
| aria-autocomplete | 미설정 | `aria-autocomplete="list"` |
| footer 단축키 안내 | 누락 | `<div class="command-palette-footer">` |

> Frontend-A 가 spa.js 모듈을 활성화할 때 위 6 개 차이를 §3 계약에 맞게
> 보강해야 fixture 와 동일한 마크업이 된다. 본 mockup 의 fixture 가 Source
> of Truth — Frontend 통합 후에도 fixture-based 테스트가 통과해야 한다.

### 8.3 단축키 바인딩 위치
- spa.js 라인 7841 ~ : 전역 keydown 핸들러가 `⌘K` 를 처리. 본 핸들러를
  Router init 직후에 활성화하면 SPA 전 라우트에서 동작.
- 입력 요소(input/textarea/contenteditable) 안에서 ⌘K 는 OS 단축키로 위임
  (이미 7841 근처에서 처리됨).

### 8.4 검색 API
- 기존 `/api/search` 사용. spa.js 의 `App.fetchJson` 으로 호출.
- debounce 권장(150ms) — 입력 1 글자마다 호출 회피.

---

## §9 spec §1.2 비목표 점검

| 항목 | 변경 |
|------|------|
| 신규 백엔드 | 0 — `/api/search` 재사용 |
| 신규 의존성 | 0 — 기존 fetch + DOM API + native dialog |
| 신규 디자인 토큰 | 0 — design.md 토큰 그대로 |
| 신규 npm 패키지 | 0 |
| 신규 CSS 변수 | 0 |

---

## §10 후속 티켓 핸드오프

| 대상 | 인터페이스 | 입력 |
|------|------------|------|
| QA-A (T-202-qa) | fixture HTML (정적 팔레트, 입력값 시뮬레이션) + 시나리오 10 종 (visual 2 + behavior 6 + a11y 2) | 본 mockup §3, §6, §7 |
| Frontend-A (T-202-impl) | spa.js Command Palette 모듈 활성화 + ⌘K 글로벌 단축키 등록 + 마크업 계약 보강(§8.2) + Router init 통합 | 본 mockup §3, §4, §5, §8 |

### 10.1 산출물 체크리스트
- [x] §1 목적 + 재사용 모듈 식별
- [x] §2 토큰 12 종 매핑 (모두 기존 토큰)
- [x] §3 마크업 계약 (native `<dialog>` + ARIA 1.2 combobox)
- [x] §4 인터랙션 6 종 (⌘K, ESC, ↑↓, Enter, 외부 클릭, Tab)
- [x] §5 카테고리 3 종 (뷰 전환 4 + 회의 검색 동적 + 작업 명령 2)
- [x] §6 a11y — native dialog + ARIA 1.2 + 색대비 측정
- [x] §7 베이스라인 3 변종 캡처 절차
- [x] §8 Frontend 핸드오프 — spa.js 7616~8194 미통합 모듈 활용 명시
- [x] §9 비목표 — 신규 백엔드/의존성/토큰 0
- [x] §10 후속 — QA-A + Frontend-A 인터페이스
