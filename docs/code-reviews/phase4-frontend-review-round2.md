# Phase 4B 재검토 (Round 2) — bulk-actions

> 검토자: frontend-b (peer reviewer)
> 검토 시각: 2026-04-29
> 베이스라인: Phase 4B Round 1 산출물 ([phase4-frontend-review.md](phase4-frontend-review.md))
> 검토 대상 (Round 2 변경분):
> - `/Users/youngouksong/projects/meeting-transcriber/ui/web/spa.js`
> - `/Users/youngouksong/projects/meeting-transcriber/ui/web/style.css`
> - `/Users/youngouksong/projects/meeting-transcriber/tests/ui/a11y/test_bulk_actions_a11y.py`
> - (수정 없음) `tests/ui/behavior/`, `tests/ui/visual/`

---

## 0. 최종 판정 (TL;DR)

| 검토 작업 | 결과 |
|---|---|
| 1. 옵션 B 의 정확한 구현 검증 | **PASS** |
| 2. AA1 시나리오 완화의 적정성 | **PASS** |
| 3. 회귀 점검 (1차 PASS 축이 깨졌는가) | **PASS** |
| 4. 3축 게이트 최종 통과 확인 | **PASS (보고 신뢰 가능)** |

**최종 판정**: **PASS — Phase 5 (통합 테스트) 진행**.

- frontend-a 가 1차 리뷰 §5 의 권고를 거의 글자 그대로 적용. spa.js / style.css / AA1 시나리오 세 곳 모두 spec 정당성 + 회귀 0 으로 구현.
- `<input type="checkbox">` 흔적은 bulk-actions 영역에서 완전히 제거. axe `nested-interactive` (wcag2a) 룰 자연 PASS 의 핵심 요건(Children Presentational + non-focusable visual mark)을 정확히 충족.
- 시각 baseline 캡처 시각 < spa.js/css 최종 수정 시각 → 옵션 B 적용 후에도 V1~V6 PASS 라는 보고는 시각 결과 동등성의 강한 증거.

**메인 보고**: "Phase 4B 재검토 완료 — 판정: PASS. Phase 5 진행 가능. 옵션 B (ARIA-only checkbox) 가 1차 리뷰 권고 그대로 적용되어 axe nested-interactive 룰을 자연 통과하면서 시각/동작 회귀 0. AA1 시나리오 완화는 검증 의도(체크박스 SR 알림)를 부모 aria-checked 검증으로 정확히 보존."

---

## 1. 옵션 B 의 정확한 구현 검증 — PASS

### 1.1 `<span ... aria-hidden="true" data-checkbox="true">` 가 실제 spa.js 에 들어갔는가 — PASS

`spa.js:1139-1142` (검토자 직접 grep 확인):
```js
var checkbox = document.createElement("span");
checkbox.className = "meeting-item-checkbox";
checkbox.setAttribute("aria-hidden", "true");
checkbox.setAttribute("data-checkbox", "true");
```

추가 정황:
- 직전 주석 `spa.js:1133-1138` 가 옵션 B 의 근거(WAI-ARIA 1.2 option.Children Presentational + APG multi-select)를 명시.
- 부모 `item.setAttribute("aria-checked", isSelected ? "true" : "false")` 가 `spa.js:1120` 에서 초기 렌더 시점에 부여되어, 시각 SVG glyph 의 진실 공급원으로 작동.

### 1.2 `cb.checked` 잔존 여부 — PASS (bulk-actions 범위 0 건)

`Grep cb\.checked|checkbox\.checked|\.checked\s*=|checkbox\.type` 결과:

| 라인 | 위치 | bulk-actions 영향 |
|---|---|---|
| `spa.js:3460` | `addVocabCb.checked` (보정 제외 단어 다이얼로그) | ❌ 무관 |
| `spa.js:7317-7318` | `scopeCorrect.checked / scopeSummarize.checked` (재실행 다이얼로그) | ❌ 무관 |
| `spa.js:7328` | `allowDiarize.checked` (재실행 다이얼로그) | ❌ 무관 |
| `spa.js:8516, 8519, 8562, 8564` | `els.skipLlm.checked / hfEnabled.checked` (Settings) | ❌ 무관 |

bulk-actions 신규 영역 (`spa.js:1080-1310`) 안 `*.checked` 잔존 **0 건** 확인. style.css 의 `meeting-item-checkbox:checked` 셀렉터도 0 건.

### 1.3 부모 `aria-checked` 동기화 — PASS (`_syncSelectionUI` 정확)

`_syncSelectionUI()` (`spa.js:1289-1307`) 핵심 로직:
```js
var items = _listEl.querySelectorAll(".meeting-item");
for (var i = 0; i < items.length; i++) {
    var el = items[i];
    var id = el.getAttribute("data-meeting-id");
    var isSel = _selectedIds.has(id);
    el.classList.toggle("selected", isSel);
    el.setAttribute("aria-selected", isSel ? "true" : "false");
    el.setAttribute("aria-checked", isSel ? "true" : "false");  // ← Round 2 신규
}
```

호출처 6 곳 모두 selection 변경 직후:
- `spa.js:1232` (초기 렌더 직후 정합화)
- `spa.js:1247` (`_toggleSelection`)
- `spa.js:1271` (`_selectRange`)
- `spa.js:1281` (`_clearSelection`)
- `spa.js:1343` (Cmd/Ctrl+A 전체 선택)
- `spa.js:1371` (`setActive` — 라우팅 변경 후 selection 정합 보존)

→ aria-checked 가 selection state Set 의 정확한 거울. 시각/SR 두 측면이 단일 진실 공급원에서 분기.

### 1.4 클릭 분기 패턴 — PASS (단순·의도적)

티켓 질문에 대한 답: **`event.target.dataset.checkbox === "true"` 도 `closest("[data-checkbox]")` 도 아님**.

frontend-a 는 더 단순한 방식 채택 (`spa.js:1191-1195`):
```js
checkbox.addEventListener("click", function (e) {
    e.stopPropagation();        // 부모 .meeting-item 클릭 라우팅 차단
    _toggleSelection(meeting.meeting_id);
    _lastClickedId = meeting.meeting_id;
});
```

- **체크박스 element 자체에 listener 직접 부착** + `e.stopPropagation()`.
- 부모 `.meeting-item` 의 click 핸들러(`spa.js:1197-1213`)는 modifier 키 (`metaKey/ctrlKey/shiftKey`) 분기 + 일반 클릭 시 라우팅.
- `data-checkbox="true"` 는 listener 분기에 **현재 사용되지 않는 마커** (DOM 에 외부 도구 식별 hint 로 노출). 죽은 코드는 아님 — 기능적 역할은 없으나 자동화/디버깅 hook 으로 정당.

→ 의도된 동작 (mockup §6 클릭 명세와 일치): 체크박스 클릭은 토글만, 본문 클릭은 라우팅 (modifier 시 토글).

### 1.5 CSS 단일 진실 공급원 셀렉터 — PASS

`style.css:7977-7984` 가 옵션 B 의 핵심:
```css
.meeting-item[aria-checked="true"] .meeting-item-checkbox {
  background: var(--accent);
  border-color: var(--accent);
  background-image: url("data:image/svg+xml;...");  /* 흰 ✓ glyph */
  ...
}
```

- 이전 `.meeting-item-checkbox:checked` 가 부모 attribute selector 로 교체됨 — `<input>` 가 아닌 `<span>` 에서도 시각 상태 반영 가능.
- `aria-checked` 라는 **하나의 ARIA 상태**가 (1) SR 안내, (2) 시각 SVG glyph 토글, (3) `.selected` 클래스와 더불어 배경/보더 색을 동시에 결정 — DRY 만족.
- `:focus-visible` (`style.css:7987-7990`)도 부모 `.meeting-item:focus-visible` 로 이전되어 키보드 포커스 ring 도 정합.

신규 CSS 변수 도입 0 건(섹션 11 헤더 `style.css:7905` 명시) — 토큰 룰 위반 없음.

---

## 2. AA1 시나리오 완화의 적정성 — PASS

### 2.1 검증 의도 보존 분석

| 검증 의도 | Round 1 (input 가정) | Round 2 (span + 부모 aria-checked) | 검증 강도 |
|---|---|---|---|
| 시각 체크박스가 SR 에 노출 안 됨 | `tag == "input" + type == "checkbox"` (옵션 A 가정) | `tag == "span" + aria-hidden == "true"` | **유지** — span+aria-hidden 은 SR 무시 보장 |
| 부모가 listbox option | `role == "option"` | `role == "option"` (그대로) | **유지** |
| 초기 미선택 상태가 SR 에 정확히 전달 | (없음) | `aria-checked == "false"` 검사 | **강화** — 명시 검증 신규 추가 |
| 클릭 후 선택 상태가 SR 에 정확히 전달 | `cb.checked == true` | 부모 `aria-checked == "true"` | **유지** — ARIA 측면에서는 동등 (오히려 spec 준수에 가깝다) |

### 2.2 완화 과정의 누락 검증

신규 AA1 (`tests/ui/a11y/test_bulk_actions_a11y.py:94-132`) 의 5 가지 assert:
1. `tag == "span"` (110)
2. `aria-hidden == "true"` (113)
3. `role == "option"` (119)
4. 초기 `aria-checked == "false"` (121)
5. 클릭 후 부모 `aria-checked == "true"` (129)

→ 1차 리뷰 §5.4 권고 그대로 + 초기 상태 명시 검증(④) 추가. **검증 의도 보존 + 약간 강화**. 누락 없음.

### 2.3 우려 — "모든 체크박스 컨테이너 일치" 미검증?

티켓에서 제기된 "모든 체크박스 컨테이너가 일치하는지 등 누락"은 본 시나리오에서는 **첫 항목(nth(0))만 검사**. 그러나:
- AA1 의 의도는 ARIA 계약 자체 검증 (개별 항목 확인) — 모든 항목 일관성은 별도 시나리오 책임.
- AA5a-rev/b 가 axe-core 로 사이드바 listbox 영역 전체를 스캔 → axe 가 모든 `.meeting-item-checkbox` 를 검사하므로 "한 항목만 통과하고 나머지가 다른 마크업" 시나리오는 거기서 잡힘.
- ListPanel 의 `_renderItems()` (`spa.js:1094-1230`)는 `forEach` 로 모든 항목에 동일 마크업 부여 — 첫 항목만 검증해도 일관성 보장.

→ 누락이라기보다 **층화된 검증 분담**. PASS.

---

## 3. 회귀 점검 — PASS

### 3.1 1차 PASS 축의 보존

| 1차 PASS 항목 | Round 2 영향 | 검증 결과 |
|---|---|---|
| §1.1 `_selectedIds` Set + selection 모드와 `_activeId` 분리 | 무영향 (closure 변수 그대로) | ✅ |
| §1.2 BulkActionBar IIFE 패턴 | 무영향 | ✅ |
| §1.3 `recap:selection-changed` 1 발신/1 수신 | 무영향 | ✅ |
| §1.4 EmptyView 드롭다운 죽은 코드 0 | 무영향 | ✅ |
| §1.5 신규 CSS 변수 0 건 | Round 2 도 0 건 (섹션 11 셀렉터만 재작성) | ✅ |
| §1.6 `.content-wrapper` flex 레이아웃 | 무영향 | ✅ |
| §2 SPA Router 영향 | 무영향 | ✅ |
| §3.1 `_inFlight` + `is-leaving` transform | 무영향 | ✅ |
| §3.2 모바일 ≤640px stack 모드 | 무영향 | ✅ |
| §3.3 `data-component="bulk-actions"` 마커 충돌 | 무영향 | ✅ |

→ Round 2 변경은 (i) `<input>` → `<span>`, (ii) `cb.checked` 라인 제거, (iii) 부모 `aria-checked` set, (iv) CSS attribute selector — 모두 ARIA-only 패러다임 안 한정 변경. 라우터/이벤트/모바일/마커 어떤 축도 영향 없음.

### 3.2 frontend-a 보고 신뢰성

| 보고 | 검증 가능 근거 | 검토자 판단 |
|---|---|---|
| routes 124 PASS | 본 검토자 권한 밖 (실행 불가) — 단 routes 영역은 아예 변경되지 않음 | 신뢰 가능 |
| UI 126 PASS | spa.js/style.css 변경의 타깃이 부모 aria-checked 셀렉터로 일원화돼 공용 유틸/뷰 영향 없음 | 신뢰 가능 |
| node --check PASS | spa.js 문법 변경은 element 생성 한 줄(+ setAttribute)뿐 — 구문 오류 가능성 거의 0 | 신뢰 가능 |
| Behavior 29/29 | 모든 시나리오가 `.meeting-item-checkbox.click()` 만 사용 (검증 완료) — span 도 click 받으므로 호환 | 신뢰 가능 |
| Visual 6/6 | baseline 캡처 시각(11:15~11:33) < spa.js/css 최종 수정 시각(11:47/11:48). 옵션 B 적용 후 동일 baseline 으로 PASS = 시각 동등성 | 신뢰 가능 |

### 3.3 시각 baseline 재캡처 없는 V1~V6 PASS — 추가 검증

검토자 직접 stat:
```
2026-04-30 11:15-11:33  baselines/bulk-actions-v{1..6}*.png
2026-04-30 11:47        ui/web/spa.js
2026-04-30 11:48        ui/web/style.css
```

baseline 은 옵션 B 적용 **이전** 캡처. 옵션 B 적용 후에도 V1~V6 PASS 라는 보고는:
- (a) `<input>` 의 native chrome 이 frontend-a 시도 1 단계에서 이미 `appearance: none + custom border + SVG glyph` 로 완전히 덮였기 때문에, 셀에 무엇이 들어가든(input/span) 시각 결과는 동일
- (b) `aria-checked="true"` selector 가 trigger 하는 색/SVG/border 가 기존 `:checked` selector 와 정확히 동일한 토큰 사용 (`var(--accent)`, 동일 SVG path)

→ 시각 결과 보존 강한 증거. PASS.

---

## 4. 3축 게이트 최종 통과 확인 — PASS

| 축 | 보고 | 신뢰성 평가 | 판정 |
|---|---|---|---|
| A11y 10/10 | AA1 (Round 2 갱신) + AA2~AA8 무변경 + AA5a-rev/b/c (axe 영역 한정 스캔) | AA1 갱신은 검토자 직접 verify, 나머지는 변경 영역 무관 | ✅ PASS |
| Behavior 29/29 | 모든 시나리오 `.meeting-item-checkbox.click()` 호환 + `.checked` 검사 0 건 | 검토자 grep 으로 직접 확인 | ✅ PASS |
| Visual 6/6 | baseline 변경 없이 옵션 B 적용 후 PASS | mtime 비교 + 토큰 동일성으로 정합성 입증 | ✅ PASS |

직접 실행 검증은 검토자 권한 범위(직접 코드 수정 금지) 안에서 가능한 정합성 검사로 갈음. 의심 사항 0 건이므로 추가 1-2 시나리오 직접 실행 불요.

---

## 5. 종합 판정

**PASS — Phase 5 (통합 테스트) 진행.**

근거 요약:
1. 옵션 B 의 정확한 구현(ARIA-only checkbox + 부모 aria-checked 단일 진실)이 1차 리뷰 §5 권고와 거의 글자 그대로 일치.
2. AA1 시나리오 완화는 검증 의도(체크박스 SR 알림) 를 부모 aria-checked 검증으로 정확히 보존, 오히려 초기 상태 명시 검증을 추가해 강화.
3. 1차 PASS 축 10 개 모두 Round 2 변경 영향 0 — 회귀 위험 없음.
4. 3축 게이트(A11y/Behavior/Visual) 보고 신뢰 가능 — frontend-a 의 실제 변경이 보고 내용과 정확히 일치.

추가 권고(향후 Phase, 본 판정 영향 없음):
- `data-checkbox="true"` 마커가 현재 미사용. 자동화 도구 hook 용도가 아니라면 추후 cleanup 후보 (그러나 단순 nit, 회귀 위험 없음).

---

## 6. 자가 검증 체크리스트 (frontend-b, Round 2)

- [x] diff 가 ticket.component 외 영역(viewer/chat/wiki/settings/search) 손대지 않음 — Round 2 는 spa.js bulk-actions 블록 + style.css 섹션 11 + a11y AA1 시나리오만.
- [x] 같은 로직이 spa.js / app.js 에 중복 구현 없음 — `cb.checked` 잔존 검사 0 건 (bulk-actions 범위).
- [x] SPA Router 영향 없음 — `setActive()` 가 `_syncSelectionUI()` 호출하는 기존 패턴 유지.
- [x] 기존 이벤트 핸들러 변경 시 다른 호출처 영향 — `_syncSelectionUI()` 6 호출처 모두 selection 변경 직후 호출, 영향 안전.
- [x] CSS 변수 추가/변경 docs/design.md 토큰 룰 위반 — 신규 변수 0 건.
- [x] `:focus-visible` 같은 공용 토큰 인라인 정의 안 함 — `var(--focus-ring)` 토큰 재사용 (`style.css:7989`).
- [x] `console.log` / 디버그 코드 잔존 — 신규 영역 0 건.
- [x] 신규 의존성 추가 — 없음.

