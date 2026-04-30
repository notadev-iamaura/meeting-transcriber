# 프론트엔드 핸드오프 — 사이드바 다중 선택 + 컨텍스트 액션 바 + 홈 드롭다운

**티켓**: bulk-actions
**Phase**: 1A → 1A 수정 (review-1b 반영, 2026-04-30) → 프론트엔드 구현 참조용
**대상**: `ui/web/spa.js` + `ui/web/style.css` 변경 담당자
**원칙**: 본 문서는 **참고용**. 실제 코드 수정은 디자이너 단계에서 하지 않는다.

---

## 0. 변경 범위 요약

| 영역 | 변경 종류 | 위치 (참고) |
|---|---|---|
| `ui/web/index.html` 또는 `spa.js` 동적 렌더 | DOM 추가: 체크박스, 액션 바, 드롭다운 컨테이너 | 사이드바 항목 렌더 함수, 콘텐츠 영역 상단, 홈뷰 액션 영역 |
| `ui/web/style.css` | CSS 클래스 추가만 (기존 토큰 재조합, 새 변수 0건) | 새 섹션 권장: `/* 11. 일괄 작업 (bulk actions) */` |
| `ui/web/spa.js` | 선택 상태 관리, 키보드 핸들러, 드롭다운 토글 | `Sidebar`, `HomeView` 컨트롤러 |

**신규 CSS 변수: 0건. 모든 색·duration·easing 은 `style.css :root` 기존 토큰 사용.**

---

## 1. DOM 구조 (HTML 스니펫 — 참고용, 실제 변경 금지)

### 1.1 사이드바 항목 (`.meeting-item` 구조 변경)

**현재 (참고)**:
```html
<a class="meeting-item" data-meeting-id="...">
  <span class="meeting-item-dot completed"></span>
  <div class="meeting-item-text">
    <div class="meeting-item-title">회의 A</div>
    <div class="meeting-item-preview">Apr 28, 14:30</div>
  </div>
</a>
```

**변경 후 (참고)**:
```html
<a class="meeting-item" data-meeting-id="..." role="option" aria-selected="false">
  <input
    type="checkbox"
    class="meeting-item-checkbox"
    aria-label="회의 선택: 회의 A"
    tabindex="-1"
  />
  <span class="meeting-item-dot completed"></span>
  <div class="meeting-item-text">
    <div class="meeting-item-title">회의 A</div>
    <div class="meeting-item-preview">Apr 28, 14:30</div>
  </div>
</a>
```

**부모 컨테이너에 `aria-multiselectable` 추가**:
```html
<div class="meetings-list" role="listbox" aria-multiselectable="true">
  <!-- 선택 모드 활성 시 .meetings-list--selecting 클래스 추가 -->
</div>
```

### 1.2 컨텍스트 액션 바 (콘텐츠 영역 최상단)

```html
<!-- 0개 선택 시 hidden, 1개+ 선택 시 표시 -->
<!-- 모바일에서 라벨/`<kbd>` 숨김을 위해 .label-text 스팬 분리 필수 -->
<div
  class="bulk-action-bar"
  role="toolbar"
  aria-label="선택된 회의 일괄 작업"
  hidden
>
  <div class="bulk-action-bar__count" aria-live="polite" aria-label="0개 선택됨">
    <svg class="icon icon--people" aria-hidden="true"><!-- 사람 아이콘 (모바일 시각 단서) --></svg>
    <span class="bulk-action-bar__count-num">0</span>
    <span class="label-text">개 선택됨</span>  <!-- 모바일에서 display:none -->
  </div>
  <div class="bulk-action-bar__actions">
    <button class="bulk-action-btn" data-action="transcribe" aria-label="전사">
      <svg class="icon" aria-hidden="true"><!-- ✎ 또는 마이크 아이콘 --></svg>
      <span class="label-text">전사</span>  <!-- 모바일에서 display:none -->
    </button>
    <button class="bulk-action-btn" data-action="summarize" aria-label="요약">
      <svg class="icon" aria-hidden="true"><!-- ≡ 또는 문서 아이콘 --></svg>
      <span class="label-text">요약</span>
    </button>
    <button class="bulk-action-btn" data-action="both" aria-label="전사+요약">
      <svg class="icon" aria-hidden="true"><!-- ✎+≡ 결합 아이콘 --></svg>
      <span class="label-text">전사+요약</span>
    </button>
  </div>
  <button class="bulk-action-bar__dismiss" aria-label="선택 해제">
    <svg class="icon" aria-hidden="true"><!-- ✕ 아이콘 --></svg>
    <span class="label-text">해제</span>  <!-- 모바일에서 display:none -->
    <kbd>Esc</kbd>  <!-- 모바일에서 display:none -->
  </button>
</div>

> **마크업 원칙 (review-1b §2 반영)**: 라벨 텍스트는 항상 `<span class="label-text">` 으로 감싼다. 모바일 미디어 쿼리에서 `display: none` 으로 숨겨도 `aria-label` 이 부모 버튼에 있어 스크린리더와 long-press 툴팁 모두 보존된다. 카운트 라벨은 `aria-label="N개 선택됨"` 을 부모 div 에 두어 모바일에서 텍스트가 숨겨져도 스크린리더 정보 유지.
```

### 1.3 홈 드롭다운

```html
<!-- 기존 .home-actions 그룹 안에 -->
<div class="home-actions">
  <button class="home-action-btn">전사 폴더 열기</button>
  <button class="home-action-btn">일괄 업로드</button>

  <!-- 신규 드롭다운 트리거 -->
  <div class="home-action-dropdown-wrapper">
    <button
      class="home-action-btn home-action-btn--dropdown"
      aria-haspopup="menu"
      aria-expanded="false"
      data-dropdown="all-bulk"
    >
      전체 일괄
      <svg class="home-action-btn__chevron">▾</svg>
    </button>
    <div
      class="home-action-dropdown"
      role="menu"
      aria-label="전체 일괄 옵션"
      hidden
    >
      <button role="menuitemradio" aria-checked="true" data-option="both">
        ✓ 전사+요약 (통합)
      </button>
      <button role="menuitemradio" aria-checked="false" data-option="transcribe">
        전사만
      </button>
      <button role="menuitemradio" aria-checked="false" data-option="summarize">
        요약만
      </button>
    </div>
  </div>

  <div class="home-action-dropdown-wrapper">
    <button
      class="home-action-btn home-action-btn--dropdown"
      aria-haspopup="menu"
      aria-expanded="false"
      data-dropdown="recent-24h"
    >
      최근 24시간
      <svg class="home-action-btn__chevron">▾</svg>
    </button>
    <!-- 동일 메뉴 -->
  </div>
</div>
```

---

## 2. 신규 CSS 클래스 명세

### 2.1 사이드바 체크박스

#### `.meeting-item-checkbox`
- **크기**: `width: 16px; height: 16px;`
- **모양**: `border-radius: 3px;` (macOS NSButton checkbox — 사각형, 아주 살짝 둥근)
- **보더**: `border: 0.5px solid var(--border);`
- **배경**: `background: var(--bg-canvas);`
- **위치**: 사이드바 항목 좌측, `flex-shrink: 0;`, `margin-top: 4px;` (상태 도트와 정렬)
- **opacity 기본**: `opacity: 0;`
- **transition**: `transition: opacity var(--duration-base) var(--ease-macos), background var(--duration-fast) var(--ease-macos);`
- **`appearance: none;`** + 커스텀 ✓ 글리프 (`background-image: url(...)` 또는 `::before` SVG)

#### `.meeting-item:hover .meeting-item-checkbox`
- `opacity: 1;`

#### `.meetings-list--selecting .meeting-item-checkbox`
- `opacity: 1;` (selection mode 활성 시 모든 항목 체크박스 상시)

#### `.meeting-item-checkbox:active`
- `transform: scale(0.96);`  /* 눌리는 순간 미세 피드백 (50ms) */
- `border-color: var(--accent);`
- `transition: transform var(--duration-fast) var(--ease-macos), border-color var(--duration-fast) var(--ease-macos);`

#### `.meeting-item-checkbox:checked`
- `background: var(--accent);`
- `border-color: var(--accent);`
- `color: #fff;` (✓ 글리프 색)

#### `.meeting-item-checkbox:focus-visible`
- `outline: none;`
- `box-shadow: var(--focus-ring);` (`style.css:60`)

#### `.meeting-item-checkbox:disabled`
- `opacity: 0.5;`
- `cursor: not-allowed;`

#### `@media (hover: none)` (모바일)
- `.meeting-item-checkbox { opacity: 1; }` — hover 부재 환경에서 항상 표시

### 2.2 사이드바 항목 selected 상태

#### `.meeting-item.selected`
- `background: var(--bg-active);` (= 라이트 `rgba(0,122,255,0.12)`, 다크 `rgba(10,132,255,0.18)`)
- 별도 transform 없음 (기존 `:hover` 의 `translateX(2px)` 와 충돌 방지)
- `transition: background var(--duration-fast) var(--ease-macos);`

#### `.meeting-item.selected .meeting-item-title`
- `color: var(--text-primary);` (active 의 accent 색과 다름 — selected 와 active 시각 구분)
- `font-weight: 500;` (active 의 600 과 차등)

#### `.meeting-item.selected.active`
- `border-left: 3px solid var(--accent);`
- `padding-left: 9px;` (기존 12px - 3px 보더)

### 2.3 컨텍스트 액션 바

#### `.bulk-action-bar`
- `position: sticky; top: 0; z-index: 50;`
- `display: flex; align-items: center; justify-content: space-between;`
- `height: 44px;`
- `padding: 0 16px;`
- `background: rgba(255, 255, 255, 0.72);` (라이트)
- `backdrop-filter: blur(20px) saturate(180%);`
- `-webkit-backdrop-filter: blur(20px) saturate(180%);`
- `border-bottom: 0.5px solid var(--border);`
- `transition: transform var(--duration-base) var(--ease-macos), opacity var(--duration-base) var(--ease-macos);`

#### `[data-theme="dark"] .bulk-action-bar`, `@media (prefers-color-scheme: dark) .bulk-action-bar`
- `background: rgba(28, 28, 30, 0.72);`

#### `.bulk-action-bar[hidden]`
- (브라우저 기본 `display: none`)

#### `.bulk-action-bar.is-entering`
- 초기: `transform: translateY(-8px); opacity: 0;`
- 종료: `transform: translateY(0); opacity: 1;`

#### `.bulk-action-bar.is-leaving`
- 역방향

#### `@media (prefers-reduced-motion: reduce) .bulk-action-bar`
- `transition: opacity var(--duration-fast) linear;`
- `transform: none !important;` (translateY 제거 — 전정 자극 회피)
- `.bulk-action-bar.is-entering`, `.bulk-action-bar.is-leaving` 도 `transform: none !important;`

> **사유**: `docs/design.md §6` 의 reduced-motion 원칙. translateY 모션은 제거하되 opacity 페이드는 유지해 시각 신호는 보존 (즉시 깜빡임은 사용자 인지 단절을 만들 수 있음).

#### `.bulk-action-bar__count`
- `font-size: 13px;`
- `color: var(--text-secondary);`

#### `.bulk-action-bar__count-num`
- `color: var(--accent-text);`  /* 본문 5.57:1 (라이트) / 6.37:1 (다크) — design.md §2.2, style.css:36/175/220 */
- `font-weight: 600;`
- `font-variant-numeric: tabular-nums;` (`style.css:780`)
- `margin-right: 2px;`

> **사유**: 13px 본문 텍스트는 WCAG AA 4.5:1 적용 대상. `--accent` (`#007AFF` on `#FFFFFF` ≈ 3.95:1) 는 미달. `--accent-text` 토큰이 `style.css :root` 라이트(`#0066CC` 5.57:1) / 다크(`#4DA1FF` 6.37:1) 양쪽에 이미 정의되어 신규 토큰 추가 불필요.

#### `.bulk-action-bar__actions`
- `display: flex; gap: 8px;`

#### `.bulk-action-btn`
- `display: inline-flex; align-items: center;`
- `padding: 6px 12px;`
- `font-size: 13px; font-weight: 500;`
- `color: var(--text-primary);`
- `background: transparent;`
- `border: 0.5px solid var(--border);`
- `border-radius: var(--radius);`
- `cursor: pointer;`
- `transition: background var(--duration-fast) var(--ease-macos), border-color var(--duration-fast) var(--ease-macos), transform var(--duration-fast) var(--ease-macos);`

#### `.bulk-action-btn:hover`
- `background: var(--bg-hover);`  /* 행/버튼 단위 hover — `style.css:24` 라이트 `rgba(0,0,0,0.04)` / 다크 `rgba(255,255,255,0.06)` */
- `border-color: var(--accent);`

> **결정**: 단일 버튼 단위 hover 는 `--bg-hover`. 인용된 `.home-action-btn:hover` (`style.css:1719`) 가 `--bg-secondary` 를 쓰는 것은 카드형 버튼 컨벤션의 잔재 — frontend-a 가 동일 기회에 `.home-action-btn` 도 `--bg-hover` 로 통일 검토. 단, 본 티켓은 `.bulk-action-btn` 만 변경하므로 `.home-action-btn` 변경은 별도 결정.

#### `.bulk-action-btn:active`
- `background: var(--bg-active);`
- `transform: scale(0.97);` (`docs/design.md §3.2`)

#### `.bulk-action-btn:disabled`
- `color: var(--text-muted);`
- `opacity: 0.5;`
- `cursor: not-allowed;`

#### `.bulk-action-btn:focus-visible`
- `outline: none;`
- `box-shadow: var(--focus-ring);`

#### `.bulk-action-bar__dismiss`
- `.home-action-btn` 의 ghost variant 와 유사
- `display: inline-flex; align-items: center; gap: 4px;`
- `padding: 4px 8px;`
- `font-size: 12px;`
- `color: var(--text-secondary);`
- `background: transparent;`
- `border: none;`
- `cursor: pointer;`
- hover: `color: var(--text-primary); background: var(--bg-hover);`

#### `.bulk-action-bar__dismiss kbd`
- 기존 `<kbd>` 스타일 그대로 (이미 정의되어 있음 — `docs/design.md §1.4`)

### 2.4 홈 드롭다운

#### `.home-action-btn--dropdown`
- 기존 `.home-action-btn` 상속
- 우측 chevron 표시: `gap: 6px;`

#### `.home-action-btn--dropdown[aria-expanded="true"]`
- `background: var(--bg-active);`
- `border-color: var(--accent);`

#### `.home-action-btn__chevron`
- `width: 12px; height: 12px;`
- `color: var(--text-secondary);`
- `transition: transform var(--duration-fast) var(--ease-macos);`

#### `.home-action-btn--dropdown[aria-expanded="true"] .home-action-btn__chevron`
- `transform: rotate(180deg);`

#### `.home-action-dropdown-wrapper`
- `position: relative;`

#### `.home-action-dropdown`
- `position: absolute;`
- `top: calc(100% + 4px);`
- `left: 0;` (또는 `right: 0` — 화면 가장자리에서 자동 반전, JS 책임)
- `min-width: 200px;`
- `padding: 4px 0;`
- `background: var(--bg-card);`
- `border: 0.5px solid var(--border);`
- `border-radius: var(--radius-lg);` (= 10px, 단 권장은 `var(--radius-md)` = 8px — `style.css :root` 에 `--radius-md` 가 없다면 `var(--radius-lg)` 또는 인라인 `8px`)
- `box-shadow: var(--shadow-lg);`
- `z-index: 100;`
- `transition: opacity var(--duration-fast) var(--ease-macos), transform var(--duration-fast) var(--ease-macos);`
- 초기: `opacity: 0; transform: translateY(-4px); pointer-events: none;`

> **주의**: 디자인 결정 §3.2 에서 8px (`--radius-md`) 를 명시했으나, `style.css :root` 에는 현재
> `--radius` (6px) 와 `--radius-lg` (10px) 만 정의되어 있다. **신규 토큰 도입 금지** 원칙에 따라
> 두 가지 옵션 중 선택:
> 1. `var(--radius-lg)` (10px) — 가장 가까운 기존 토큰
> 2. 인라인 `border-radius: 8px;` — design.md §2.4 의 `--radius-md` 권장값을 인라인으로
>
> Designer-B 와 협의 후 결정 (이 결정이 단일 변경 사항).

#### `.home-action-dropdown[hidden]`
- 브라우저 기본 + `pointer-events: none;`

#### `.home-action-dropdown.is-open`
- `opacity: 1; transform: translateY(0); pointer-events: auto;`

#### `.home-action-dropdown [role="menuitemradio"]`
- `display: flex; align-items: center; gap: 8px;`
- `width: 100%;`
- `padding: 6px 12px;`
- `font-size: 13px;`
- `color: var(--text-primary);`
- `background: transparent;`
- `border: none;`
- `text-align: left;`
- `cursor: pointer;`
- `transition: background var(--duration-fast) var(--ease-macos), color var(--duration-fast) var(--ease-macos);`

#### `.home-action-dropdown [role="menuitemradio"]:hover`
- `background: var(--accent);`
- `color: #fff;` (NSMenu 표준)

#### `.home-action-dropdown [role="menuitemradio"]:focus-visible`
- 위 hover 와 동일 (포커스 = 시각상 hover)
- `outline: none;`

#### `.home-action-dropdown [role="menuitemradio"][aria-checked="true"]::before`
- `content: "✓";`
- `color: var(--accent);`
- `font-weight: 600;`
- `width: 12px;`

#### `.home-action-dropdown [role="menuitemradio"]:hover[aria-checked="true"]::before`
- `color: #fff;` (hover 시 ✓ 색 반전)

#### `.home-action-dropdown [role="menuitemradio"]:disabled`
- `color: var(--text-muted);`
- `opacity: 0.5;`
- `cursor: not-allowed;`

#### `@media (max-width: 640px)` (모바일)
- `.home-action-dropdown { min-width: calc(100vw - 32px); max-width: 320px; }`
- `.bulk-action-bar { flex-direction: column; height: auto; padding: 8px 12px; gap: 8px; }`
- `.bulk-action-bar__count .label-text { display: none; }`  /* "개 선택됨" 텍스트 숨김 */
- `.bulk-action-bar__count::before { content: ""; /* 사람 아이콘 SVG */ }`  /* 시각 단서 */
- `.bulk-action-btn .label-text { display: none; }`  /* 라벨 텍스트 숨김 */
- `.bulk-action-btn .icon { display: inline-flex; }`  /* 아이콘만 표시 */
- `.bulk-action-btn[aria-label]` 가 long-press 시 OS 기본 툴팁 트리거
- `.bulk-action-bar__dismiss .label-text,
   .bulk-action-bar__dismiss kbd { display: none; }`  /* "해제" 텍스트와 ⌘Esc <kbd> 모두 숨김, ✕ 아이콘만 */
- `.bulk-action-bar__actions { width: 100%; }`
- `.bulk-action-btn { flex: 1; min-width: 0; }`  /* 균등 분할 + 잘림 방지 백업 */

> **모바일 라벨 정책 (review-1b §2 반영)**: 라벨 텍스트는 시각상 숨기되 `aria-label` 로 보존하여 스크린리더와 long-press 툴팁 양쪽에 노출. 카운트는 사람 아이콘 + 숫자 (`<span class="label-text">개 선택됨</span>` 부분만 숨김), `<kbd>` 는 모바일에 키보드 입력기가 없어 노이즈이므로 완전 숨김.

---

## 3. JS 동작 규칙 (참고용)

본 문서는 디자인 산출물이므로 JS 구현은 별도. 단, 디자인 의도가 깨지지 않도록 다음 동작 규칙을 메모.

### 3.1 선택 상태 관리

```js
// Sidebar 컨트롤러 내부 (참고)
const state = {
  selectedIds: new Set(),
  lastClickedId: null,  // Shift+클릭 범위 선택 기준점
};

// 체크박스 클릭
function onCheckboxClick(meetingId, event) {
  event.stopPropagation();  // 부모 .meeting-item 클릭 차단 (뷰어 이동 막기)
  toggleSelection(meetingId);
  state.lastClickedId = meetingId;
  updateUI();
}

// 본문 클릭
function onItemClick(meetingId, event) {
  if (event.metaKey || event.ctrlKey) {
    toggleSelection(meetingId);
    state.lastClickedId = meetingId;
  } else if (event.shiftKey && state.lastClickedId) {
    selectRange(state.lastClickedId, meetingId);
  } else {
    // 일반 클릭: 기존대로 뷰어 이동, 선택은 변경 X (사용자 확정 동작)
    navigate(`/app/viewer/${meetingId}`);
  }
  updateUI();
}

// Esc 키 — 전체 해제 + selection mode 종료
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && state.selectedIds.size > 0) {
    state.selectedIds.clear();
    updateUI();
  }
});

// Cmd+A / Ctrl+A — 사이드바 포커스 한정 전체 선택
document.addEventListener('keydown', (e) => {
  const isMeta = e.metaKey || e.ctrlKey;
  if (!isMeta || e.key.toLowerCase() !== 'a') return;
  // 사이드바 컨테이너 또는 그 자손에 포커스가 있을 때만 가로채기
  const sidebar = document.querySelector('.meetings-list[role="listbox"]');
  if (!sidebar) return;
  const isInSidebar = sidebar === document.activeElement
    || sidebar.contains(document.activeElement);
  if (!isInSidebar) return;
  e.preventDefault();
  document.querySelectorAll('.meeting-item').forEach(item => {
    state.selectedIds.add(item.dataset.meetingId);
  });
  updateUI();
});

// 마지막 항목 체크 해제 → 자동 selection mode 종료
function toggleSelection(id) {
  if (state.selectedIds.has(id)) {
    state.selectedIds.delete(id);
  } else {
    state.selectedIds.add(id);
  }
  // count==0 이면 updateUI() 가 액션 바를 자동 slide-up (`updateBulkActionBar(0)`)
  // → bulk-actions.md §1.5 의 "마지막 항목 해제 → 자동 OFF" 정책
}

// 액션 실행 후 자동 종료 (bulk-actions.md §1.5)
async function executeAction(actionKind) {
  const ids = [...state.selectedIds];
  // 부분 적합성 정책 — 자동 필터링 (bulk-actions.md §2.5.1)
  const eligible = ids.filter(id => isEligibleForAction(id, actionKind));
  const skipped = ids.length - eligible.length;
  await runBulkAction(actionKind, eligible);
  // toast: "N개 처리, M개 건너뜀 (이미 처리됨)" — UI 정책 §2.5.1
  showToast(`${eligible.length}개 처리${skipped > 0 ? `, ${skipped}개 건너뜀 (이미 처리됨)` : ''}`);
  // 액션 완료 후 자동으로 selection mode 종료
  state.selectedIds.clear();
  updateUI();
}

// UI 갱신
function updateUI() {
  const list = document.querySelector('.meetings-list');
  list.classList.toggle('meetings-list--selecting', state.selectedIds.size > 0);

  document.querySelectorAll('.meeting-item').forEach(item => {
    const id = item.dataset.meetingId;
    const isSelected = state.selectedIds.has(id);
    item.classList.toggle('selected', isSelected);
    item.setAttribute('aria-selected', String(isSelected));
    const cb = item.querySelector('.meeting-item-checkbox');
    if (cb) cb.checked = isSelected;
  });

  updateBulkActionBar(state.selectedIds.size);
}
```

### 3.2 컨텍스트 액션 바 슬라이드

```js
function updateBulkActionBar(count) {
  const bar = document.querySelector('.bulk-action-bar');
  const numEl = bar.querySelector('.bulk-action-bar__count-num');

  if (count > 0) {
    numEl.textContent = count;
    if (bar.hidden) {
      bar.hidden = false;
      // 다음 frame 에서 클래스 추가 → CSS transition 동작
      requestAnimationFrame(() => {
        bar.classList.remove('is-leaving');
        bar.classList.add('is-entering');
      });
    }
  } else if (!bar.hidden) {
    bar.classList.remove('is-entering');
    bar.classList.add('is-leaving');
    bar.addEventListener('transitionend', () => {
      bar.hidden = true;
    }, { once: true });
  }
}
```

### 3.3 드롭다운 포커스 트랩 + 외부 클릭 닫기

```js
// 드롭다운 토글
function toggleDropdown(triggerEl) {
  const wrapper = triggerEl.closest('.home-action-dropdown-wrapper');
  const menu = wrapper.querySelector('.home-action-dropdown');
  const isOpen = triggerEl.getAttribute('aria-expanded') === 'true';

  closeAllDropdowns();  // 다른 드롭다운 닫기

  if (!isOpen) {
    triggerEl.setAttribute('aria-expanded', 'true');
    menu.hidden = false;
    requestAnimationFrame(() => menu.classList.add('is-open'));
    // 첫 메뉴 항목으로 포커스
    menu.querySelector('[role="menuitemradio"]').focus();
  }
}

// 키보드 이동
menu.addEventListener('keydown', (e) => {
  const items = [...menu.querySelectorAll('[role="menuitemradio"]')];
  const idx = items.indexOf(document.activeElement);
  if (e.key === 'ArrowDown') items[(idx + 1) % items.length]?.focus();
  if (e.key === 'ArrowUp') items[(idx - 1 + items.length) % items.length]?.focus();
  if (e.key === 'Escape') {
    closeDropdown();
    triggerEl.focus();
  }
  if (e.key === 'Tab') closeDropdown();
});

// 외부 클릭
document.addEventListener('click', (e) => {
  if (!e.target.closest('.home-action-dropdown-wrapper')) closeAllDropdowns();
});
```

---

## 4. 토큰 매핑 표 (구현자 빠른 참조)

| 컴포넌트 | 사용 토큰 | 정의 위치 |
|---|---|---|
| 체크박스 보더 | `var(--border)` | `style.css:50` |
| 체크박스 채움 | `var(--accent)` | `style.css:35` |
| 체크박스 hover | (영향 없음 — 부모 hover) | — |
| selected 배경 | `var(--bg-active)` | `style.css:25` |
| 액션 바 vibrancy 라이트 | `rgba(255,255,255,0.72) + blur(20px) saturate(180%)` | `docs/design.md §1.2` |
| 액션 바 vibrancy 다크 | `rgba(28,28,30,0.72)` | `docs/design.md §1.2` |
| 액션 바 보더 | `0.5px solid var(--border)` | `docs/design.md §1.3` + `style.css:50` |
| 카운트 숫자 색 | `var(--accent-text)` (라이트 `#0066CC` 5.57:1 / 다크 `#4DA1FF` 6.37:1) | `style.css:36, 175, 220` |
| 액션 버튼 | `transparent + 0.5px var(--border) + var(--text-primary)` | `style.css:30, 50` |
| 액션 버튼 hover | `var(--bg-hover) + var(--accent) 보더` | `style.css:24, 35` |
| 액션 버튼 active | `var(--bg-active) + scale(0.97)` | `style.css:25` + `docs/design.md §3.2` |
| 드롭다운 배경 | `var(--bg-card)` | `style.css:27` |
| 드롭다운 보더 | `0.5px solid var(--border)` | `style.css:50` |
| 드롭다운 그림자 | `var(--shadow-lg)` | `style.css:55` |
| 드롭다운 radius | `var(--radius-lg)` (10px) — 또는 인라인 8px | `style.css:65` |
| 드롭다운 hover | `var(--accent) + #fff` | `docs/design.md §3.5` 변형 (NSMenu 표준) |
| 모든 transition duration | `var(--duration-fast)` (150ms) / `var(--duration-base)` (250ms) | `style.css:71-72` |
| 모든 transition easing | `var(--ease-macos)` | `style.css:69` |
| 포커스 링 | `var(--focus-ring)` (인터랙티브 모두) | `style.css:60` |

---

## 5. 구현 순서 권장 (참고)

1. **CSS 클래스 정의** (변경 없는 토큰 조합) — `style.css` 신규 섹션 11
2. **DOM 구조 변경** — 체크박스, 액션 바, 드롭다운 트리거/메뉴
3. **JS 선택 상태 관리** — `state.selectedIds` Set + 이벤트 핸들러
4. **키보드 핸들러** — Esc, Tab, ↑↓, Space, Enter
5. **드롭다운 토글 + 외부 클릭 닫기**
6. **ARIA 속성 동기화** — `aria-selected`, `aria-expanded`, `aria-checked`
7. **`prefers-reduced-motion` 검증** — 모든 트랜지션이 0.01ms 로 단축되는지

---

## 6. 디자인 ↔ 구현 핵심 갭 (frontend-a 가 처리해야 할 결정)

| 항목 | 갭 | 본 티켓 (Phase 1A) 의 결정 | frontend-a 가 추가로 처리할 것 |
|---|---|---|---|
| 드롭다운 radius | design.md 는 8px (`--radius-md`) 권장, style.css 는 6/10 만 정의 | `var(--radius-lg)` (10px) 사용 — 가장 가까운 기존 토큰 | 향후 `--radius-md: 8px` 토큰을 `style.css :root` 에 추가하는 별도 PR 검토 권장 (`docs/design.md §2.2` 인용) |
| 액션 바 transition timing | 요구사항 "200ms ease-out" vs `--duration-base` (250ms) | `--duration-base` (250ms macOS easing) 사용 — 토큰 우선 원칙 | 변경 없음 |
| `--bg-active` 다크 alpha | `rgba(10,132,255,0.18)` — 다크 모드에서 selected 가 진해짐 | 의도된 동작 (`docs/design.md §1.1` 톤 격차). 그대로 사용 | 변경 없음 |
| `--accent-text` 토큰 존재 검증 | 본 결정에서 카운트 숫자 색에 `--accent-text` 사용 결정 | `style.css:36, 175, 220` 에 라이트(`#0066CC`)/다크(`#4DA1FF`) 모두 정의 확인됨 — 즉시 사용 가능 | 변경 없음 (토큰 추가 불필요) |
| `.home-action-btn:hover` 의 `--bg-secondary` ↔ `.bulk-action-btn:hover` 의 `--bg-hover` | 단일 버튼 hover 컨벤션 불일치 | `.bulk-action-btn` 은 `--bg-hover` 채택 (행 단위 hover) | `.home-action-btn` 도 `--bg-hover` 로 통일 여부는 별도 결정. 본 티켓 범위 밖 |
| 모바일 라벨 텍스트 ↔ 아이콘 분리 마크업 | DOM 에 `<span class="label-text">` + 아이콘 SVG 가 분리되어 있어야 모바일 미디어 쿼리로 텍스트만 숨김 가능 | 본 결정에서 마크업 패턴 정의 (handoff §1.2) | DOM 렌더 시 `<svg class="icon">` + `<span class="label-text">전사+요약</span>` 패턴 적용 |

---

## 7. 검증 체크리스트

- [x] 모든 신규 클래스 명세에 사용 토큰 출처 인용
- [x] 신규 CSS 변수 0건
- [x] light/dark 차이 자동 처리 (기존 `:root` dark override 활용)
- [x] 모바일 적응 미디어 쿼리 명시 (라벨 텍스트 숨김 + `<kbd>` 숨김)
- [x] 접근성 (ARIA 속성, 키보드 핸들러) 명시
- [x] JS 동작 규칙 (디자인 의도 보존) 메모
- [x] 한국어 작성, 토큰/클래스/속성명은 원문 유지
- [x] 카운트 숫자 색이 `--accent-text` 로 WCAG AA 대비 보장 (라이트 5.57:1 / 다크 6.37:1)
- [x] 체크박스 `:active` 상태 명세
- [x] reduced-motion 의 `.bulk-action-bar` 처리 명시 (transform 제거 + opacity 유지)
- [x] 라벨/아이콘 분리 마크업 (`<span class="label-text">`) 패턴 정의
- [x] Cmd+A 사이드바 한정 핸들러 코드 예시
- [x] 자동 selection mode 종료 (마지막 항목 해제 / Esc / 액션 실행 후) 동작 코드 예시
- [x] 부분 적합성 자동 필터링 정책 코드 예시 (`executeAction` 의 `eligible.filter`)
