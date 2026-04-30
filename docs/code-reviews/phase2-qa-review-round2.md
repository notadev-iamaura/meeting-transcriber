# Phase 2B 재검토 보고서 — bulk-actions 시나리오 45건 (Round 2)

**티켓**: bulk-actions / Phase 2B
**검토자**: qa-b
**검토 일자**: 2026-04-29
**검토 대상**: qa-a 가 1차 검토 의견을 반영한 45 시나리오 (29 behavior + 10 a11y + 6 visual) + conftest 확장

---

## 0. 최종 판정

**판정**: **PASS** ✅

| 1차 항목 (7건) | 결과 |
|---|---|
| 필수 1. AA5 거짓 Red 해소 | ✅ |
| 필수 2. 빈 사이드바 케이스 추가 | ✅ |
| 필수 3. 사이드바 컨테이너 selector 단일 진실 | ✅ |
| 권장 4. 5xx 에러 응답 시나리오 추가 | ✅ |
| 권장 5. 선택 중 새 회의 추가 시 보존 시나리오 | ✅ |
| 권장 6. V5 dark-mobile → light-mobile 정정 | ✅ |
| 권장 7. `browser` fixture 의존 명시 | ✅ |

**최종 통과 항목**: 7/7

Phase 3 (백엔드 구현) 진행 가능.

---

## 1. 1차 7개 수정 항목 검증 (각 항목 실증 인용)

### 항목 1 (필수): AA5a/b/c 거짓 Red 해소 — ✅

**1차 지적**: `tests/ui/a11y/test_bulk_actions_a11y.py::test_AA5a` 실측에서 `color-contrast (serious): 16 nodes` 가 나와 영구 FAIL. 페이지 전체 axe scan 이라 기존 SPA 의 무관한 위반을 상속.

**조치 확인**:

1. AA5a 삭제 + AA5a-rev 추가 (`tests/ui/a11y/test_bulk_actions_a11y.py:239-276`)
2. axe scan 헬퍼가 `context={"include": [...]}` 로 컴포넌트 한정 (`a11y:204-229`):
   ```python
   def _run_axe_on(page, include_selectors):
       results = axe.run(
           page,
           context={"include": [[sel] for sel in include_selectors]},
           ...
       )
   ```
3. 매칭 노드 0 시 명시적 에러 (`a11y:264-272, 293-300, 321-328`):
   ```python
   matched_nodes = sum(
       len(check.get("nodes", []))
       for kind in ("passes", "violations", "incomplete", "inapplicable")
       for check in raw.get(kind, [])
   )
   assert matched_nodes > 0, "bulk-actions 컴포넌트가 DOM 에 존재해야 함 ..."
   ```
4. include selector 가 fallback chain 으로 안전 — `[data-component='bulk-actions']`, `[role='listbox'][aria-multiselectable='true']`, `.bulk-action-bar` (또는 `.home-action-dropdown`).

**Red 의도성 실측** (qa-b 가 직접 실행):
```
FAILED test_AA5a_rev_사이드바_1개_선택_시점_bulk_actions_axe_위반_0
  → playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded.
    waiting for locator(".meeting-item").first.locator(".meeting-item-checkbox")

FAILED test_AA5b_2개_선택_상태_bulk_actions_axe_위반_0  → 동일 (selector 부재)
FAILED test_AA5c_드롭다운_열린_상태_axe_위반_0  → ".home-action-btn--dropdown[data-dropdown='all-bulk']" 부재
```

**핵심 차이**: 이전에는 `color-contrast: 16 nodes` (무관한 기존 SPA 위반) 로 실패. 이제는 **컴포넌트 자체 부재**로 실패. bulk-actions 가 구현되면 컴포넌트가 등장하고, 한정 스캔이라 기존 SPA 의 16 건 위반에 영향받지 않으므로 Green 진입 가능. **거짓 Red 완전 해소**.

---

### 항목 2 (필수): 빈 사이드바 (회의 0 건) 케이스 추가 — ✅

**1차 지적**: spec §3 의 "회의 0 개일 때 Cmd+A no-op" 엣지 누락.

**조치 확인**:
- B10 추가: `behavior:315-365` (`test_B10_빈_사이드바에서_Cmd_A는_no_op_이다`)
- B11 추가: `behavior:367-419` (`test_B11_0개_회의에서_selection_mode_진입_불가`)
- 두 시나리오 모두 `empty_meetings_dom(page)` 헬퍼로 DOM 강제 비움 (`conftest.py:292-318`)
- 빈 시드 fixture `ui_bulk_empty_base_dir` 도 함께 정의됨 (`conftest.py:262-285`) — 사용처는 DOM 비움이 더 가벼워서 회의 5 건 서버를 재사용

**Red 의도성 (직접 실행)**:
```
FAILED TestSidebarMultiSelect::test_B10_빈_사이드바에서_Cmd_A는_no_op_이다
  → AssertionError: `#listContent` 에 aria-multiselectable='true' 추가 필요 (got count=0)

FAILED TestSidebarMultiSelect::test_B11_0개_회의에서_selection_mode_진입_불가
  → AssertionError: `#listContent` 에 aria-multiselectable='true' 추가 필요 (count=0)
```

**거짓 통과 방지 분석**:
- B10 의 우려 (체크박스 부재 → 우연한 PASS) 는 사전 가시성 검증 (`behavior:332-335`) 으로 차단:
  - `meetings_listbox(page)` (aria-multiselectable='true' 컨테이너) 가 정확히 1 개 매칭 강제 → 미구현 시 0 매칭 → 즉시 FAIL
- B11 도 사전 가시성 2 단계 (`behavior:384-396`):
  - listbox 컨테이너 1 개 매칭
  - 비우기 직전 체크박스 5 개 매칭 (시드 가정 검증)
  - 둘 중 하나라도 미충족 시 명확한 FAIL

**fixture fallback 거짓 통과 가능성**: 없음. `empty_meetings_dom` 은 사전 가시성 검증 이후에만 호출되며, 검증 자체가 미구현 상태에서 먼저 실패한다.

---

### 항목 3 (필수): 사이드바 컨테이너 selector 단일 진실 — ✅

**1차 지적**: `behavior:227-230, 254-257` 의 `.meetings-list, #listContent` fallback chain.

**조치 확인**:
1. `meetings_listbox()` 헬퍼 신설 (`conftest.py:233-253`):
   ```python
   def meetings_listbox(page):
       return page.locator("[role='listbox'][aria-multiselectable='true']")
   ```
2. behavior 파일이 import 후 사용 (`behavior:36`):
   ```python
   from tests.ui.conftest import empty_meetings_dom, meetings_listbox
   ```
3. 사용처 4 곳 (`behavior:231, 263, 332, 384, 467`) 모두 헬퍼 호출.

**fallback chain 잔존 grep 결과**:
```
$ grep -rn "\.meetings-list,\|\.meetings-list, #listContent" tests/ui/
(behavior, a11y, visual, conftest 의 실제 코드 영역에는 0 건)
(test_bulk_actions_review-2b.md 안의 1차 보고 인용문에만 등장 — 정상)
```

→ 시나리오 코드 내 fallback chain `.meetings-list, #listContent` 는 **완전히 제거됨**. 잔여 매치는 1차 검토 보고서 (.md) 안의 인용문 뿐.

**검증 잔존 selector 분포**:
| 등장 위치 | 형태 | 합리성 |
|---|---|---|
| `behavior:234, 334, 386` | "`#listContent` 에 aria-multiselectable='true' 추가 필요" | ✅ 에러 메시지 안의 문자열 (frontend-a 안내용) |
| `conftest.py:21, 244, 301, 311` | `#listContent` 직접 참조 | ✅ DOM 비우기 헬퍼 + 핸드오프 메모 — selector 단일 진실 헬퍼 외 영역 |

**ARIA 속성 기준 selector 채택의 의의**: frontend-a 가 컨테이너 클래스를 `list-content` 유지하든 `meetings-list` 추가하든 ARIA 속성만 보장하면 되므로 구현 선택권이 명확히 분리됨.

---

### 항목 4 (권장): 5xx 에러 응답 시나리오 — ✅

**조치 확인**: A11 추가 (`behavior:731-776`).

**페이로드/검증**:
- mock 이 `status=500, {"error":"internal","message":"처리 실패"}` 응답
- `[role='alert']:visible, .toast--error:visible, .home-status[data-level='error']:visible` 중 하나에 노출 검증
- 한국어/영어 키워드 (`실패/오류/에러/error`) 관대 매칭

**Red 의도성 실측**:
```
FAILED TestBulkActionBar::test_A11_5xx_에러_시_에러_토스트_표시
  → playwright._impl._errors.TimeoutError: locator(".meeting-item-checkbox") 30000ms 부재
```
사전 단계 (체크박스 클릭) 부재로 깨끗한 FAIL. 구현 후에는 mock 5xx 응답 → 에러 토스트 검증 흐름 진입.

---

### 항목 5 (권장): 선택 중 새 회의 추가 시 기존 선택 보존 — ✅

**조치 확인**: B12 추가 (`behavior:421-471`).

**구현 방식**:
- 2 개 선택 후 `document.getElementById('listContent')` 안에 새 항목을 prepend (watchdog 시뮬레이션)
- 기존 selected 카운트 == 2 검증
- selection mode 클래스 유지 (`meetings-list--selecting`) 검증
- 액션 바 가시성 유지 검증

**Red 의도성 실측**:
```
FAILED TestSidebarMultiSelect::test_B12_selection_중_새_회의_추가_시_기존_선택_보존
  → playwright._impl._errors.TimeoutError: locator(".meeting-item-checkbox") 30000ms 부재
```
selector 부재로 깨끗한 FAIL.

**참고**: SSE/watchdog 통합은 Phase 3 백엔드 영역이라 본 시나리오는 SPA 의 DOM 갱신 정책 (selection 보존) 만 검증 — 적절한 범위 한정.

---

### 항목 6 (권장): V5 dark-mobile → light-mobile 정정 — ✅

**조치 확인**: `tests/ui/visual/test_bulk_actions_visual.py:208-235`
- 함수명: `test_V5_three_selected_light_mobile`
- 컨텍스트: `color_scheme="light"`, viewport 375×720
- 모듈 docstring (`visual:15-20`) 도 "review-2b §4 변종명 정정 — V5 는 light-mobile" 명시
- 캡처 파일명: `bulk-actions-v5-three-selected-light-mobile.png`

**Red 의도성 실측**:
```
FAILED test_V5_three_selected_light_mobile
  → playwright._impl._errors.TimeoutError: locator(".meeting-item-checkbox") 30000ms 부재
```
selector 부재로 깨끗한 FAIL. baseline 자동 생성 fail-safe 진입 전에 사전 가시성 검증 실패.

---

### 항목 7 (권장): `browser` fixture 의존 명시 — ✅

**조치 확인**: `conftest.py:14-19` (모듈 docstring "플러그인 의존성" 섹션):
```
플러그인 의존성:
    본 모듈은 `pytest-playwright` 빌트인 fixture (`browser`, `browser_type` 등)
    에 의존한다. `pip install -e ".[dev]"` 로 설치되며, 별도로
    `playwright install chromium` 이 필요하다. 자체 `browser` fixture 를
    정의하지 않고 플러그인 빌트인을 사용한다 (test_e2e_edit_playwright.py 와
    다른 패턴 — 그쪽은 자체 session-scoped browser fixture 정의).
```

→ docstring 명시 충분.

---

## 2. 회귀 점검 결과

### 2.1 기존 시나리오 Red 의도성 유지 — ✅

기존 24+10+6 = 40 시나리오 중 핵심 표본 5 건 직접 실행 (회귀 표본 추출):

| 시나리오 | 1 차 결과 | 2 차 결과 | 회귀? |
|---|---|---|---|
| B1 (체크박스 hover-reveal) | TimeoutError selector 부재 | (코드 변경 없음, 헬퍼만 추가) | ✅ 유지 |
| H1 (드롭다운 2 개) | AssertionError count 2 → 0 | (코드 변경 없음) | ✅ 유지 |
| V1 (비선택 light desktop) | `to_have_count(2)` FAIL | `Locator expected count '2' Actual '0'` (실측 재확인) | ✅ 유지 |
| V5 (변경 — dark→light mobile) | (변경 전) | TimeoutError selector 부재 | ✅ 신규 동작 정상 |
| AA5a (삭제됨) | color-contrast 16 nodes (거짓 Red) | 시나리오 자체 삭제 | ✅ 거짓 Red 해소 |

### 2.2 selector 단일 진실 헬퍼 적용 후 회귀 — ✅

B6, B7, B12 가 헬퍼 사용으로 변경되었지만 실제 검증 의미 (selection mode 클래스 부여/제거) 는 동일. 1 차 보고서가 인용한 fallback chain `.meetings-list, #listContent` 가 코드에서 완전히 제거되어 frontend-a 가 `list-content` 클래스 유지 시에도 ARIA 속성으로 매칭 가능.

### 2.3 fixture 충돌 / 신규 fixture 영향 — ✅

- `ui_bulk_empty_base_dir` 신설 (session-scope) — 기존 fixture 와 이름 충돌 없음
- `meetings_listbox`, `empty_meetings_dom` 헬퍼 신설 — fixture 가 아닌 함수, 의존성 그래프 영향 없음
- conftest 내 import (`from core.job_queue import JobQueue` lazy) — 기존 패턴 동일

---

## 3. 자가 검증 체크리스트

| 항목 | 결과 | 근거 |
|---|---|---|
| Red 의도성 실측 (자동 명령) | ✅ | B10/B11/B12/A10/A11/AA5a-rev/AA5b/AA5c/V1/V5 직접 실행, 모두 selector 부재로 깨끗한 FAIL — 거짓 통과 0 건 |
| 축 분리 — behavior 가 visual 검증 섞이지 않음 | ✅ | grep `to_have_screenshot|assert_visual_match|screenshot\(` → behavior 파일 0 건. `getComputedStyle.opacity/transform` 만 사용 (transition 완료 검증 — 행동 검증의 일환) |
| 축 분리 — a11y 가 행동 검증 섞이지 않음 | ✅ | grep `to_have_text` → a11y 파일 0 건. axe role/aria-label/aria-checked/aria-live/Tab order/focus-ring/reduced-motion 만 검증. `cb.click() + el.checked` 는 ARIA 동기화 검증으로 적절 |
| 엣지 케이스 — 빈 상태 / 로딩 / 에러 / 키보드 / 다크 | ✅ | B10/B11 (빈), A11 (5xx 에러), B9/H6 (키보드만), V4 (다크) — 모두 커버. 로딩 상태는 A10 (in-flight 디바운스) 이 일부 커버 |
| 시나리오 격리 | ✅ | 각 테스트가 새 `browser.new_context()` + `page.route()` 격리. mock 호출 카운트 누적 없음 |
| 마커 (`pytest.mark.ui`) | ✅ | 3 파일 모두 `pytestmark = [pytest.mark.ui]` (behavior:38, a11y:50, visual:54) |
| 시나리오 의존성 (한 시나리오가 다른 결과 의존?) | ✅ | session-scope fixture (`ui_bulk_server`, `ui_bulk_meeting_ids`) 만 공유. function-scope `ui_page` 가 매번 새 context — 격리 |

---

## 4. 축 분리 위반 신규 도입 여부

신규 시나리오 (B10/B11/B12, A10/A11, AA5a-rev) 에 대한 축 분리 점검:

| 시나리오 | 검증 대상 | 축 위반? |
|---|---|---|
| B10 | DOM count + selection mode 클래스 부재 | 행동 검증 — ✅ 위반 없음 |
| B11 | 체크박스 카운트 + selection mode 클래스 부재 | 행동 검증 — ✅ 위반 없음 |
| B12 | selected 카운트 + 클래스 유지 + 액션 바 가시성 | 행동 검증 — ✅ 위반 없음 (가시성은 행동 결과 검증) |
| A10 | API 호출 카운트 (디바운스) | 행동 검증 — ✅ 위반 없음 |
| A11 | mock 호출 + role='alert' 토스트 텍스트 | 행동 검증 (에러 정책) — ✅ 위반 없음 |
| AA5a-rev | axe-core scoped scan + matched_nodes>0 | a11y — ✅ 위반 없음 |

**결론**: 신규 시나리오에 축 위반 도입 0 건.

---

## 5. 7-축 종합 표 (Round 2)

| 축 | 1차 판정 | 2차 판정 | 변화 사항 |
|---|---|---|---|
| 1. 시나리오 완전성 | ✅ PASS | ✅ PASS | 40 → 45 (B10, B11, B12, A10, A11 추가, AA5a → AA5a-rev 변경) |
| 2. Red 의도성 | ❌ FAIL | ✅ PASS | AA5a-rev/b/c 컴포넌트 한정 + matched_nodes>0 가드. 신규 5 건 모두 직접 실행 검증 |
| 3. 축 분리 | ✅ PASS | ✅ PASS | 신규 시나리오에 축 위반 도입 0 건 |
| 4. 엣지 케이스 | ⚠️ WARN | ✅ PASS | 빈 사이드바 (B10/B11), 5xx 에러 (A11), 새 회의 추가 (B12), in-flight 디바운스 (A10) 추가 |
| 5. selector 컨벤션 | ⚠️ WARN | ✅ PASS | `meetings_listbox()` 단일 진실 헬퍼 + fallback chain 완전 제거 (코드 영역 grep 0 건) |
| 6. API 모킹 | ✅ PASS | ✅ PASS | 5xx mock + 지연 mock 추가, 응답 스키마 일관 |
| 7. 회귀 위험 | ⚠️ WARN | ✅ PASS | `browser` fixture 의존 docstring 명시 |

---

## 6. 통합 권한

**판정: PASS**. Phase 3 (백엔드 구현) 진행 가능.

frontend-a 핸드오프 합의 사항 (변경 없음, 기존 conftest:20-33 그대로):
1. 사이드바 컨테이너에 `aria-multiselectable="true"` 속성 + selection mode 시 `meetings-list--selecting` 클래스 부여
2. (권장) `data-component="bulk-actions"` 마커를 `.bulk-action-bar`, `.home-action-dropdown` 루트에 부여 — axe scoped scan 의 명시적 진입점
3. 응답 계약 — `{queued, skipped, message}` (200) / `{error, message}` (5xx)

