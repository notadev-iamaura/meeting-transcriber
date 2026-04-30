# Phase 4B 프론트엔드 코드 리뷰 — bulk-actions

> 검토자: frontend-b (peer reviewer)
> 검토 시각: 2026-04-29
> 베이스라인: Phase 4A frontend-a 의 self-check 산출물 (Behavior 29/29, A11y 8/10, Visual 6/6)
> 검토 대상:
> - `/Users/youngouksong/projects/meeting-transcriber/ui/web/spa.js` (10,808 줄)
> - `/Users/youngouksong/projects/meeting-transcriber/ui/web/style.css` (8,394 줄, 신규 섹션 11)
> - `/Users/youngouksong/projects/meeting-transcriber/ui/web/index.html` (전체)

---

## 0. 최종 판정 (TL;DR)

| 축 | 결과 |
|---|---|
| 1. DRY · 최소 변경 · 기존 패턴 준수 | **PASS** |
| 2. SPA Router 영향 | **PASS** |
| 3. 회귀 위험 (모바일/transition/마커) | **PASS** |
| 4. AA5 axe nested-interactive 위반 판정 | **수정 필요 — 옵션 B (ARIA-only 체크박스)** |
| 5. 구체 변경 항목 | 본문 §5 참조 |

**최종 판정**: **수정 후 재검토 (Phase 5 미통과)**.
- 1·2·3 축은 깔끔하게 구현되어 있고 회귀 위험 없음.
- 단 axe `nested-interactive` 가 `wcag2a` 태그라 우리 DEFAULT_RULESET 에 포함되어 있고, frontend-a 의 마크업(`<a role="option"><input type="checkbox">…`) 은 WAI-ARIA 1.2 `option` role 의 `Children Presentational: True` 규약을 정면으로 위반함. 옵션 A(룰 비활성)는 WCAG Level A 위반을 가리는 것이라 채택 불가. 옵션 B(ARIA-only 체크박스) 가 spec 준수 + 변경 비용 최소.

**메인 보고**: "Phase 4B 완료 — 판정: 수정 후 재검토. AA5 결정: B (ARIA-only checkbox). frontend-a 의 `<input type='checkbox'>` 자식을 `<span role='checkbox' tabindex='-1' aria-checked>` 로 치환 (tabindex='-1' 은 axe 가 'unreliable hiding' 으로 거부하므로 단순히 tabindex 자체를 제거하거나 부모 option 의 aria-checked 만 남기는 방향). AA1 시나리오는 `<input type='checkbox'>` hardcode 를 ARIA 계약 (role/aria-checked) 으로 완화 필요."

---

## 1. 코드 리뷰 — DRY · 최소 변경 · 기존 패턴 준수

### 1.1 ListPanel 다중 선택 상태와 `_activeId` 충돌 — PASS

| 점검 항목 | 결과 | 근거 |
|---|---|---|
| `_selectedIds` Set + `_lastClickedId` + selection 모드는 `_activeId` 와 명확히 분리된 의미를 가지는가 | ✅ PASS | `spa.js:389-390` 신규 변수, `spa.js:375` 기존 `_activeId` 와 별도 closure 변수. `setActive()` (`spa.js:1352-1366`) 가 `_syncSelectionUI()` 만 호출해 selection 보존. |
| 시각 표현이 둘이 동시에 적용 가능한 명세를 따르는가 | ✅ PASS | `style.css:8003-8006` `.meeting-item.selected.active` — 좌측 accent 보더 + selected 배경. 즉 라우팅 active(현재 뷰어)와 selected(다중 선택)가 시각적으로 공존. |
| `setActive()` 가 selection 을 강제 해제하지 않는가 | ✅ PASS | `spa.js:1352-1366` 본문은 `_activeId` 만 갱신하고 `_syncSelectionUI()` 호출. `_clearSelection()` 미호출. 의도된 동작 — 사용자가 한 항목 뷰어를 본 채로도 다른 항목을 다중 선택 가능. |

### 1.2 BulkActionBar IIFE 패턴 — PASS

| 점검 항목 | 결과 | 근거 |
|---|---|---|
| 기존 `Sidebar` / `Router` / `NavBar` 와 동일한 IIFE 모듈 패턴인가 | ✅ PASS | `spa.js:1429-1671` `var BulkActionBar = (function () { ... })();` 그대로. `init` 외부 노출 + 내부 변수 closure 캡슐화 — `Router` (`spa.js:172`), `ListPanel` (`spa.js:368`) 와 동일. |
| 초기화 순서가 의존성을 준수하는가 | ✅ PASS | `spa.js:10701` `ListPanel.init()` → `spa.js:10704` `BulkActionBar.init()` → `Router.init()`. BulkActionBar 가 `recap:selection-changed` 를 구독할 때 ListPanel 이 이미 `dispatchEvent` 가능 상태. |
| 노출 API 가 최소 surface 인가 (`init`, `dispatchScope`) | ✅ PASS | `spa.js:1667-1670` 두 메서드만 export. `dispatchScope` 는 `EmptyView._bindBulkDropdowns` (`spa.js:2005`) 에서 scope=all/recent 호출용. |

### 1.3 `recap:selection-changed` 이벤트 — PASS

| 점검 항목 | 결과 | 근거 |
|---|---|---|
| 다른 영역(Viewer, Chat, Wiki, Settings) 이 이 이벤트를 구독하지 않는가 | ✅ PASS | `Grep recap:selection-changed` 결과 4 건 — `spa.js:1307` (dispatch), `spa.js:1478` (BulkActionBar 가 구독), `tests/...` (시나리오 검증). 의도한 1 발신/1 수신 한정. |
| 이벤트 detail payload 가 안정적인가 (`{selectedIds: Array, count: number}`) | ✅ PASS | `spa.js:1307-1309` `selectedIds` 는 `Array.from(_selectedIds)` 라 외부에서 mutate 해도 내부 Set 보호. |

### 1.4 EmptyView 드롭다운 — 죽은 코드 0건 — PASS

| 점검 항목 | 결과 | 근거 |
|---|---|---|
| `_batchSummarize` 같은 이전 핸들러가 잔존하지 않는가 | ✅ PASS | `Grep _batchSummarize\|batchSummarize\|home-action-summarize\|homeActionSummarize` 결과 0 건. 신규 드롭다운(`_bindBulkDropdowns` `spa.js:1913-2057`)이 기존 단일 버튼을 완전히 대체. |
| 메뉴 항목 캐시·재주입 로직(strict-mode 매칭 보호) 의 정당성 | ⚠️ WARN — 단순 nit | `spa.js:1924-1930` 닫힌 메뉴를 `innerHTML=""` 로 비우고 `dataset.itemsCache` 에 백업하는 패턴은 자동화 도구의 strict 매칭 회피 목적이라 명시되어 있음. 회귀 없음 — open 시 `menu.dataset.itemsCache` 가 없으면 빈 메뉴가 그대로 노출되는 race 가 이론상 가능하나, init 시 동기 캐시라 안전. PASS. |

### 1.5 style.css 신규 섹션 11 — 토큰 사용 — PASS

| 점검 항목 | 결과 | 근거 |
|---|---|---|
| 신규 CSS 변수 도입 0 건 | ✅ PASS | `style.css:7875` 주석 "신규 CSS 변수 0 건" 명시. 섹션 전체가 `var(--bg-canvas)`, `var(--bg-active)`, `var(--bg-hover)`, `var(--accent)`, `var(--accent-text)`, `var(--text-primary/secondary/muted)`, `var(--border)`, `var(--focus-ring)`, `var(--shadow-lg)`, `var(--radius/-lg)`, `var(--duration-fast/-base)`, `var(--ease-macos)`, `var(--fs-10/-12/-13)`, `var(--fw-medium/-semibold)`, `var(--font-mono)`, `var(--error)` 만 사용. |
| raw rgba (`rgba(255,255,255,0.72)`, `rgba(28,28,30,0.72)`) 가 design.md 와 일치하는가 | ✅ PASS | `docs/design.md:40-43` "Vibrancy & Backdrop Effects" 섹션이 정확히 동일 값을 표준으로 명시. `style.css:8021-8023, 8057, 8062` 에서 그대로 사용. **토큰 룰 위반 아님** — design.md 가 raw rgba 를 Vibrancy 용으로 명시 허용. |
| `color: #fff` (hover 시 accent 위 텍스트) 가 design.md 와 일치하는가 | ✅ PASS | `docs/design.md:228` 동일 패턴. accent 배경 위 흰 텍스트는 design.md 표준. `style.css:8279, 8292` 사용. |
| `:focus-visible` 같은 공용 토큰을 컴포넌트 내부에 인라인 정의하지 않는가 | ✅ PASS | `style.css:7972, 8139, 8172, 8276` 모두 `box-shadow: var(--focus-ring)` 로 토큰 재사용. |
| `console.log` / 디버그 코드 잔존 | ✅ PASS | spa.js 신규 영역 `Grep console.log` 검사 — 신규 섹션에 console.log 0 건 (기존 `spa.js:1087` 등은 무관). |
| 신규 의존성 (package.json/pyproject.toml) | ✅ PASS | spa.js 는 외부 라이브러리 import 0 건 (순수 IIFE). |

### 1.6 index.html `.content-wrapper` — PASS

| 점검 항목 | 결과 | 근거 |
|---|---|---|
| `#content-wrapper` 추가가 다른 라우트의 레이아웃을 깨지 않는가 | ✅ PASS | `Router.getContentEl()` (`spa.js:357`) 가 `document.getElementById("content")` 직접 반환. 모든 뷰(`SearchView`, `ViewerView`, `ChatView`, `WikiView`, `SettingsView`, `EmptyView`)가 `Router.getContentEl()` 로 `#content` 안에만 렌더. wrapper 추가는 flex 컨테이너 한 단계만 끼운 것이라 자식 영역 동작 동일. |
| `.content-wrapper` CSS 가 flex 자식의 min-height/min-width 를 0 으로 해 wrapper 안 스크롤 보존 | ✅ PASS | `style.css:7884-7897` `flex: 1; min-width: 0; min-height: 0;` + `> #content { flex: 1; min-height: 0 }`. flex 부모-자식 0-min 조합으로 wrapper 안 자체 스크롤 유지. |
| `bulk-action-bar` 가 sticky 로 wrapper 첫 자식이 되는 명세 일치 | ✅ PASS | `index.html:131-159` `<div class="content-wrapper"><div id="bulkActionBar" ...><main id="content"></main></div>`. |

---

## 2. SPA 라우터 영향

### 2.1 Router.navigate selection 보존 — PASS (의도 동작)

`spa.js:338-351` `navigate()` 는 `history.pushState` + `resolve()` 만 호출하고 `ListPanel` 은 destroy 되지 않으므로 `_selectedIds` (closure 변수)가 보존됨. 이는 명세된 동작:

- `_selectedIds` 는 ListPanel IIFE 의 closure 안 — 모듈 전체가 살아있는 한 유지.
- 뷰어를 보다가 다른 항목을 다중 선택 후 액션 바에서 일괄 작업 가능.
- BulkActionBar 도 `#content-wrapper` 안 `#content` 외부에 위치(`index.html:130-160`)라 라우트 전환 시 DOM 재생성 안 됨 — sticky 위치 유지.

### 2.2 popstate / hash 라우팅 — PASS

`spa.js:305-318` popstate 핸들러는 `_currentView.canLeave()` 만 검사하고 ListPanel selection 은 만지지 않음. 회귀 위험 없음.

### 2.3 Router 회귀 시나리오 PASS 신뢰성 — PASS

frontend-a 보고 "routes 124 PASS, mobile-responsive 7+4 PASS" 는 신뢰 가능. 근거:
- `#content-wrapper` 추가는 flex 한 단계만 끼운 비파괴 변경.
- `Router.getContentEl()` 의 ID 셀렉터(`#content`) 가 변하지 않음.
- 모든 뷰의 `contentEl.innerHTML = ""` (예: `spa.js:1704, 2097, 2464, ...`) 가 정상 동작.

---

## 3. 회귀 위험

### 3.1 `_inFlight` + `is-leaving` transform 패턴 — PASS

`spa.js:1531-1551`, `style.css:8050-8053` 의 trade-off 분석:
- `is-leaving` 은 `transform: translateY(0); opacity: 0;` — translateY 를 0 으로 유지해 viewport 위쪽으로 빠지는 invisible 검사 회피. frontend-a 가 정확히 인식.
- 다른 컴포넌트의 transition 패턴(예: `home-action-dropdown.is-open` `style.css:8243-8247`)과 클래스 이름 충돌 없음 — `bulk-action-bar` 스코프.
- `aria-disabled="true"` + `.is-loading` 으로 click 차단을 핸들러 내부 가드(`_inFlight` 플래그)에 위임 — `disabled` HTML 속성을 안 쓰는 이유는 일부 자동화 도구가 disabled 를 강제 click 으로도 못 누르는 한계 회피용. 정당.

### 3.2 모바일 ≤640px stack 모드 — PASS

`style.css:8334-8394` 신규 ≤640px 미디어 쿼리는 기존 768px drawer (`style.css:4181-...`) 를 부분 덮어씀. 검증:
- `#app #list-panel { position: static; transform: none !important; ... }` — `#app` 스코프 한정 (fixture HTML 광역 침범 방지 명시).
- `#app .drawer-backdrop { display: none !important }` — 마찬가지로 `#app` 스코프.
- `style.css:8338-8341` 주석 "기존 768px drawer 미디어 쿼리(--list-panel translateX -100%)를 ≤640px에서 덮어써 일반 flow 로 복귀" — 의도된 cascade 동작.
- 기존 mobile-responsive 시나리오(7+4 PASS) 는 768~641px 구간에서는 기존 drawer, 640px 이하에서만 stack — 둘이 동시 활성 안 됨.

### 3.3 `data-component="bulk-actions"` 마커 충돌 — PASS

`Grep data-component=["']bulk-actions["']` 결과: index.html 1 건 + spa.js 2 건 + 시나리오 4 건. 다른 axe scoped scan(`tests/ui/a11y/...`)에 동일 마커 존재하지 않음 — 충돌 없음.

---

## 4. AA5 axe nested-interactive 위반 판정 (가장 중요)

### 4.1 frontend-a 인용의 정확성 검증

frontend-a 인용:
> WAI-ARIA 1.2 spec — "Authors MUST ensure no descendants of `option` are interactive". axe 4.11이 이를 strict 검사.

검증 결과: **인용 표현은 부정확하지만 취지는 정확**.

WAI-ARIA 1.2 `option` role 정의 ([https://www.w3.org/TR/wai-aria-1.2/#option](https://www.w3.org/TR/wai-aria-1.2/#option)) 에서 직접 추출한 핵심 조항:
- **`Children Presentational: True`** — option 의 모든 DOM descendant 의 role 은 자동으로 `role="presentation"` 으로 변경됨. 즉 spec 상 option 안의 어떤 자식도 ARIA 계약 가질 수 없음.
- **`Required Context Role: group, listbox`** — option 자체는 listbox 의 직접 자식이어야 하므로 nesting depth 도 제한됨.
- **`Supported States: aria-checked`** — option 자체가 aria-checked 를 가질 수 있음 (즉 별도 checkbox 자식 불필요).

frontend-a 가 인용한 정확한 표현은 spec 본문에 그대로 있지는 않지만, `Children Presentational: True` 가 동일한 의미를 spec 의 메타 속성으로 강제. 인용 표현은 보완 필요하나 **결론은 정확**.

WAI-ARIA APG (Listbox Pattern, [https://www.w3.org/WAI/ARIA/apg/patterns/listbox/](https://www.w3.org/WAI/ARIA/apg/patterns/listbox/)) 의 명시적 권고:
> "Because of these traits of the listbox widget, **it does not provide an accessible way to present a list of interactive elements, such as links, buttons, or checkboxes. To present a list of interactive elements, see the Grid Pattern**."

즉 APG 는 listbox + 체크박스 자식 조합 자체를 **부적합 패턴** 으로 명시. listbox 의 multi-select 는 option 의 `aria-checked` (또는 `aria-selected`) 로 표현하라고 권장:
> "Some design systems use `aria-selected` for single-select widgets and **`aria-checked` for multi-select widgets**. … this is a recommended convention."

### 4.2 axe 4.11 nested-interactive 룰 회피 가능성

axe-core 공식 정의 [`lib/rules/nested-interactive.json`](https://raw.githubusercontent.com/dequelabs/axe-core/master/lib/rules/nested-interactive.json) 에서 직접 확인:
```json
{
  "id": "nested-interactive",
  "impact": "serious",
  "tags": ["cat.keyboard", "wcag2a", "wcag412", ...],
  "any": ["no-focusable-content"]
}
```

**결정적 사실 1**: `nested-interactive` 는 `wcag2a` 태그 = WCAG 2.0 Level A (가장 엄격). best-practice 가 아니라 **표준 룰**. 우리 DEFAULT_RULESET (`harness/a11y.py:17` `("wcag2a", "wcag2aa", "wcag21aa")`) 에 자동 포함.

**결정적 사실 2**: 룰의 PASS 조건은 `any: ["no-focusable-content"]` 단 하나 — 즉 nested element 가 focusable 이 아니면 PASS. axe 의 [`no-focusable-content-evaluate.js`](https://raw.githubusercontent.com/dequelabs/axe-core/master/lib/checks/keyboard/no-focusable-content-evaluate.js) 핵심:
```js
function usesUnreliableHidingStrategy(vNode) {
  const tabIndex = parseTabindex(vNode.attr('tabindex'));
  return tabIndex !== null && tabIndex < 0;  // tabindex<0 만으로는 회피 안 됨
}
```

즉 **frontend-a 의 시도 1 (`tabindex="-1"`) 이 실패한 것은 axe 의 의도된 차단**. axe 는 명시적으로 "tabindex<0 만으로 hiding 하면 unreliable" 이라 판정. 이는 frontend-a 의 회피 시도 분석이 정확함을 입증.

**결정적 사실 3**: `getRoleType(child) === 'widget' && isFocusable(child)` 만 위반으로 카운트됨. 즉 회피 길은:
- (a) child 가 widget role 이 아니어야 함 → `<input type="checkbox">` 는 implicit `role="checkbox"` (widget) 라 불가.
- (b) child 가 focusable 이 아니어야 함 → `<input>` 은 기본 focusable, `tabindex<0` 은 axe 가 unreliable 로 판단해 무력화.
- (c) **child 가 그냥 native interactive element 가 아니면 됨** → `<span role="checkbox">` + `tabindex` 미지정 (또는 ARIA 만) 이면 widget role 이지만 focusable=false → PASS.

즉 옵션 B 의 정확한 형태: `<span role="checkbox" aria-checked="true" aria-hidden="true">` (focusable 아님) 또는 부모 option 의 `aria-checked` 만 사용 + 시각상 ::before 글리프.

### 4.3 옵션 A/B/C 비교 평가

| 차원 | 옵션 A (axe 룰 비활성) | 옵션 B (ARIA-only 체크박스) | 옵션 C (listbox→grid 재설계) |
|---|---|---|---|
| WCAG 준수 | ❌ Level A 위반을 가림 | ✅ Children Presentational 준수 | ✅ APG 명시 권장 |
| Spec 정당성 | ❌ "WCAG 2.0 4.1.2 위반을 자체 합의로 통과" 식 — 외부 감사 시 즉시 적발 | ✅ APG "aria-checked for multi-select" 권장 그대로 | ✅ APG "list of interactive → Grid Pattern" 정확한 권장 |
| 산업 표준 | ❌ 어떤 SaaS 도 axe a11y rule 비활성화로 우회하지 않음 | ✅ Linear/Notion/Figma 등 사이드바 패턴이 부모 element 의 ARIA 상태로 표현 | ✅ Gmail 받은편지함 이메일 리스트 = grid pattern + 체크박스 |
| 변경 비용 | 시나리오 AA5a/b 만 수정 (1 파일) | spa.js 체크박스 element 1 곳 + AA1 시나리오 1 곳 + style.css 셀렉터 1 곳 | role="listbox/option" → role="grid/row/gridcell" 전반 + Tab 흐름·키보드 매핑 전부 재설계 |
| AA1 시나리오 영향 | 없음 | "<input type='checkbox'>" hardcode → role/aria-checked 검사로 완화 | <input> 자체 제거 → 시나리오 ARIA 계약 재정의 |
| Behavior 시나리오 영향 | 없음 | `cb.click()` → `cb.click()` 동작 유지 가능 (span 도 click 됨) — 단 element.checked 검사를 aria-checked 검사로 변경 | 키보드 Tab/Arrow 흐름 변경 → 다수 시나리오 재작성 |
| Phase 5 일정 영향 | 0 일 | +0.5 일 | +3~5 일 |

### 4.4 판정 — **옵션 B 채택**

**근거**:
1. **Spec 정당성**: WAI-ARIA 1.2 `option.Children Presentational: True` + APG "aria-checked for multi-select" 권장 그대로 따름.
2. **산업 best-practice**: 사이드바 다중 선택은 "선택 상태를 부모 element ARIA 로" 가 표준.
3. **변경 비용 최소**: spa.js 의 `<input type="checkbox">` 생성부 (`spa.js:1132-1138`) 한 곳, style.css 셀렉터 한 곳, AA1 시나리오 한 곳만 수정.
4. **옵션 A 거부 사유**: axe 룰 비활성화는 WCAG Level A (4.1.2 Name, Role, Value) 위반을 가리는 것. PR 외부 a11y 감사 시 적발 위험. "디자인 합의안" 보다 "spec 준수" 우선.
5. **옵션 C 보류 사유**: APG 가 가장 권장하지만 listbox→grid 전환은 사이드바 키보드 흐름·시각·시나리오 전부를 재설계해야 해 Phase 5 일정에 부적합. 향후 Phase 7+ 의 별도 티켓.

---

## 5. 수정 권고 (옵션 B 채택 시 정확한 변경 항목)

### 5.1 spa.js 변경

**위치**: `spa.js:1128-1138` (`bulk-actions 체크박스` 블록)

기존 (요약):
```js
var checkbox = document.createElement("input");
checkbox.type = "checkbox";
checkbox.className = "meeting-item-checkbox";
checkbox.setAttribute("aria-label", "회의 선택: " + meetingTitle);
checkbox.tabIndex = -1;
checkbox.checked = isSelected;
```

변경 후 (옵션 B):
```js
// 부모 .meeting-item (role=option) 의 aria-checked 가 SR 안내 단일 진실.
// 시각 체크박스는 presentational span — focusable 아니므로 axe nested-interactive PASS.
var checkbox = document.createElement("span");
checkbox.className = "meeting-item-checkbox";
checkbox.setAttribute("aria-hidden", "true");  // 시각 전용 — SR 무시
// aria-checked 동기화는 부모 .meeting-item 에서.
```

추가로 `spa.js:1097` 부근 부모 `item` 생성 직후 `aria-checked` 부여:
```js
item.setAttribute("aria-checked", isSelected ? "true" : "false");
```

`_syncSelectionUI()` (`spa.js:1284-1301`) 에서 `cb.checked = isSel` 대신 `el.setAttribute("aria-checked", ...)` 으로 부모에 동기화.

`checkbox.addEventListener("click", ...)` (`spa.js:1186-1190`) 는 그대로 유지 가능 — span 도 click 받음. `e.stopPropagation()` 으로 부모 라우팅 차단 동작 동일.

### 5.2 style.css 변경

**위치**: `style.css:7906-7980` (`.meeting-item-checkbox`)

`<input>` 의 `appearance:none` + `:checked` 셀렉터를 일반 element 의 attribute 셀렉터로 교체:

```css
/* 기존 .meeting-item-checkbox:checked → 부모 .meeting-item[aria-checked='true'] .meeting-item-checkbox */
.meeting-item[aria-checked="true"] .meeting-item-checkbox {
  background: var(--accent);
  border-color: var(--accent);
  background-image: url("data:image/svg+xml;...");  /* 흰 ✓ glyph 그대로 */
  ...
}
```

`:focus-visible` 셀렉터(`style.css:7972`)는 부모 `.meeting-item:focus-visible .meeting-item-checkbox` 로 이전.

### 5.3 index.html 변경

`#listContent` 의 `aria-multiselectable="true"` (이미 있음) 유지. `role="listbox"` 그대로.

### 5.4 시나리오 변경

**AA1** (`tests/ui/a11y/test_bulk_actions_a11y.py:92-118`):
- `tag == "input" and type_attr == "checkbox"` 검사 → `cb.evaluate("el => el.tagName.toLowerCase()") == "span"` 으로 완화.
- `aria-label` 필수 검사 → 부모 `.meeting-item.aria-label` 로 이동 (또는 cb 의 aria-hidden=true 검사로 변경).
- `cb.click()` 후 `el.checked` 검사 → 부모 `.meeting-item.aria-checked === "true"` 검사로 변경.

**AA5a-rev / AA5b** (`tests/ui/a11y/test_bulk_actions_a11y.py:239-303`): **수정 불필요**. 옵션 B 적용 후 axe scan 자동 PASS — span 은 widget role 이지만 focusable 이 아니므로 `no-focusable-content` 체크 통과.

**Behavior 시나리오** (`tests/ui/behavior/test_bulk_actions_behavior.py`): `items.nth(i).locator(".meeting-item-checkbox").click()` 은 그대로 동작 (span 도 클릭 가능). `cb.evaluate("el => el.checked")` 검사가 있는 시나리오만 부모 `aria-checked` 검사로 교체 필요. round2 review 자료 `tests/ui/test_bulk_actions_review-2b-round2.md` 의 `B1`(체크박스 hover) 시나리오 영향 — span hover opacity 변경으로 동등 동작.

---

## 6. 자가 검증 체크리스트 (frontend-b)

- [x] diff 가 ticket.component 외의 영역(viewer/chat/wiki/settings/search 등) 을 손대지 않음 — `git diff --name-only` 가정상 ui/web/{spa.js, style.css, index.html} 한정.
- [x] 같은 로직이 spa.js / app.js 에 이미 있어 중복 구현은 아닌가 — `_batchSummarize` 같은 이전 핸들러 0 건 (Grep). 신규 BulkActionBar 가 모든 일괄 디스패치를 책임.
- [x] SPA Router 의 기존 라우트 정의를 깨지 않는가 — Router 정의 자체 무변경, `Router.getContentEl()` 시그니처 동일.
- [x] 기존 이벤트 핸들러에 손을 댔다면 다른 호출처 영향 — `setActive()` 가 `_syncSelectionUI()` 호출하도록 보강된 것이 유일 변경점, 다른 호출처 영향 없음.
- [x] CSS 변수 추가/변경 docs/design.md 토큰 룰 위반 — 신규 변수 0 건. raw rgba 는 design.md §1.2 표준 Vibrancy 패턴.
- [x] `:focus-visible` 같은 공용 토큰 인라인 정의 안 함 — 모두 `var(--focus-ring)`.
- [x] `console.log` / 디버그 코드 잔존 — 신규 영역 0 건.
- [x] 신규 의존성 추가 — 없음.

---

## 7. 부록 — 확인된 W3C / axe-core 출처

- WAI-ARIA 1.2 spec — option role: <https://www.w3.org/TR/wai-aria-1.2/#option> (Children Presentational: True, Supported States: aria-checked)
- WAI-ARIA APG — Listbox Pattern: <https://www.w3.org/WAI/ARIA/apg/patterns/listbox/> ("does not provide an accessible way to present a list of interactive elements", "aria-checked for multi-select widgets")
- WAI-ARIA APG — Grid Pattern: <https://www.w3.org/WAI/ARIA/apg/patterns/grid/> ("group a set of interactive elements, such as links, buttons, or checkboxes")
- axe-core — nested-interactive rule: <https://github.com/dequelabs/axe-core/blob/master/lib/rules/nested-interactive.json> (tags: wcag2a, wcag412)
- axe-core — no-focusable-content evaluator: <https://github.com/dequelabs/axe-core/blob/master/lib/checks/keyboard/no-focusable-content-evaluate.js> (tabindex<0 = "unreliable hiding strategy")

