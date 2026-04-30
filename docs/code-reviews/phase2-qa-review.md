# Phase 2B 검토 보고서 — bulk-actions 시나리오 40건

**티켓**: bulk-actions / Phase 2B
**검토자**: qa-b
**검토 일자**: 2026-04-29
**검토 대상**: qa-a 가 작성한 시나리오 40건 (24 behavior + 10 a11y + 6 visual) + conftest.py 확장

---

## 0. 최종 판정

**판정**: **수정 후 재검토 (changes_requested)**

| 통과 축 | 결과 |
|---|---|
| 1. 시나리오 완전성 | PASS |
| 2. Red 의도성 | **FAIL — AA5 시나리오 거짓 Red** |
| 3. 축 분리 | PASS |
| 4. 엣지 케이스 누락 | **WARN — 5건 누락** |
| 5. 고정 selector 컨벤션 | **WARN — 사이드바 컨테이너 클래스 불일치** |
| 6. API 모킹 정확성 | PASS |
| 7. 회귀 위험 | **WARN — `browser` fixture 의존 패턴** |

**통과 축**: 3/7 ✅, 3/7 ⚠️, 1/7 ❌

핵심 issue:
- **AA5a/b/c (axe-core 위반 0건)** 가 기존 SPA 의 16건 color-contrast 위반으로 인해 bulk-actions 구현으로는 절대 Green 으로 전환 불가능한 "거짓 Red"
- spec §3 의 빈 상태/0개 회의 케이스 누락 (Cmd+A, 액션 바 진입 모두 5건 시드 가정)
- 사이드바 컨테이너 클래스가 실제로는 `.list-content` (id `#listContent`) 인데 시나리오에서 `.meetings-list` 로 부여하라고 요구하는 부분 일관성 부족

---

## 1. 검토 자동화 — Red 의도성 실측

본 검토는 `git stash` 대신 **현재 main 브랜치(미구현 baseline)에서 실제 pytest 실행으로 검증**했다. 결과:

### 1.1 정상 Red 사례 (B1, H1, V1)
```
tests/ui/behavior/test_bulk_actions_behavior.py::TestSidebarMultiSelect::test_B1_체크박스가_hover_시_나타난다
  → playwright._impl._errors.TimeoutError: Locator(".meeting-item-checkbox") 30s timeout (selector 부재로 깨끗한 FAIL)

tests/ui/behavior/test_bulk_actions_behavior.py::TestHomeBulkDropdowns::test_H1_홈에_두_드롭다운_트리거가_존재한다
  → AssertionError: Locator expected count 2, actual 0 (명확한 assertion FAIL)

tests/ui/visual/test_bulk_actions_visual.py::test_V1_unselected_light_desktop
  → expect(...).to_have_count(2) FAIL (사전 가시성 검증으로 깨끗한 FAIL,
    baseline 자동 생성 단계 진입조차 못 함 — `harness.snapshot.assert_visual_match` 가
    baseline 부재 시 첫 캡처를 자동 저장해 통과시키는 동작과 무관)
```

→ **Collection 통과, fixture/import 오류 없음**, 서버 subprocess 정상 기동, 사이드바 5건 시드 정상 렌더 확인됨.

### 1.2 거짓 Red 사례 (AA5a)
```
tests/ui/a11y/test_bulk_actions_a11y.py::test_AA5a_홈_초기상태_axe_위반_0
  → AssertionError: color-contrast (serious): 16 nodes
```

**문제**: 이 16건은 bulk-actions 와 무관한 기존 SPA 의 color-contrast 위반이다. bulk-actions 구현이 완료되어도 이 위반은 사라지지 않으므로 AA5a 는 **영구적으로 FAIL** 하는 시나리오가 된다 (=Green 진입 경로 없음). AA5b, AA5c 도 동일 페이지를 스캔하므로 같은 위반을 상속한다.

---

## 2. 7-축 검토 결과

### 축 1: 시나리오 완전성 — PASS

| 그룹 | spec §3 (B1-B9, A1-A9, H1-H6, AA1-AA8, V1-V6) | 시나리오 ID | 매핑 |
|---|---|---|---|
| Sidebar Multi-Select | B1 hover-reveal | test_B1_체크박스가_hover_시_나타난다 | ✅ behavior:113-128 |
|  | B2 체크박스 클릭은 뷰어 이동 X | test_B2 | ✅ behavior:130-147 |
|  | B3 본문 클릭은 뷰어 이동 | test_B3 | ✅ behavior:149-163 |
|  | B4 Cmd+클릭 토글 | test_B4 | ✅ behavior:165-187 |
|  | B5 Shift+클릭 범위 | test_B5 | ✅ behavior:189-211 |
|  | B6 selection mode 진입 시 모든 체크박스 상시 | test_B6 | ✅ behavior:213-239 |
|  | B7 마지막 해제 → 자동 OFF | test_B7 | ✅ behavior:241-263 |
|  | B8 Esc 전체 해제 | test_B8 | ✅ behavior:265-286 |
|  | B9 Cmd+A 사이드바 한정 | test_B9 | ✅ behavior:288-304 |
| Bulk Action Bar | A1 0개 → hidden | test_A1 | ✅ behavior:315-325 |
|  | A2 1개 → slide-down | test_A2 | ✅ behavior:327-344 |
|  | A3 N개 카운트 갱신 | test_A3 | ✅ behavior:346-364 |
|  | A4 [전사] → POST batch | test_A4 | ✅ behavior:366-390 |
|  | A5 [요약] → POST batch | test_A5 | ✅ behavior:392-412 |
|  | A6 [전사+요약] → action="full" | test_A6 | ✅ behavior:414-436 |
|  | A7 [✕]해제 | test_A7 | ✅ behavior:438-461 |
|  | A8 toast 메시지 | test_A8 | ✅ behavior:463-488 |
|  | A9 액션 후 자동 종료 | test_A9 | ✅ behavior:490-511 |
| Home Dropdown | H1 두 트리거 존재 | test_H1 | ✅ behavior:522-537 |
|  | H2 메뉴 3 항목 | test_H2 | ✅ behavior:539-561 |
|  | H3 scope=all | test_H3 | ✅ behavior:563-582 |
|  | H4 scope=recent hours=24 | test_H4 | ✅ behavior:584-604 |
|  | H5 외부 클릭 닫힘 | test_H5 | ✅ behavior:606-625 |
|  | H6 키보드 조작 | test_H6 | ✅ behavior:627-658 |
| A11y | AA1 체크박스 ARIA | test_AA1 | ✅ a11y:80-106 |
|  | AA2 toolbar role | test_AA2 | ✅ a11y:114-131 |
|  | AA3 aria-live polite | test_AA3 | ✅ a11y:139-153 |
|  | AA4 menu/menuitemradio | test_AA4 | ✅ a11y:161-184 |
|  | AA5 axe 위반 0 (3 시점) | test_AA5a/b/c | ✅ a11y:211-238 (단, 거짓 Red — 축 2 참조) |
|  | AA6 Tab 도달 가능 | test_AA6 | ✅ a11y:246-287 |
|  | AA7 focus-visible ring | test_AA7 | ✅ a11y:295-308 |
|  | AA8 reduced-motion | test_AA8 | ✅ a11y:316-343 |
| Visual | V1-V6 6변종 | test_V1~V6 | ✅ visual:110-257 |

**결론**: spec §3 의 모든 완료 정의가 1:1 매핑되어 있고 누락 없음. **그러나 spec §3 의 일반 정의를 넘는 엣지 케이스는 누락 (축 4 참조)**.

---

### 축 2: Red 의도성 — FAIL ❌

**검증 자동화 결과**:

| 시나리오 | 실측 결과 | 평가 |
|---|---|---|
| B1 (체크박스 hover) | TimeoutError: locator `.meeting-item-checkbox` 30s 부재 | ✅ 깨끗한 Red |
| H1 (드롭다운 2개) | AssertionError: count 2 expected, 0 actual | ✅ 깨끗한 Red |
| V1 (비선택 light desktop) | `expect(.home-action-btn--dropdown).to_have_count(2)` 사전 가시성 FAIL | ✅ 깨끗한 Red |
| **AA5a (홈 초기 axe 위반 0)** | **`color-contrast (serious): 16 nodes` — 기존 SPA 위반** | ❌ **거짓 Red** |

**핵심 문제**:
- `harness.snapshot.assert_visual_match` 는 baseline 부재 시 자동 생성 후 PASS 시키는 fail-safe 동작을 가짐 (`harness/snapshot.py:118-122`). 따라서 V1-V6 시나리오는 baseline 부재만으로는 FAIL 하지 않는다. qa-a 가 시나리오에 추가한 **사전 가시성 검증** (`expect(...).to_be_visible(timeout=2000)`, `expect(...).to_have_count(2)`) 이 Red 의도성을 만든다 — **이 부분 설계는 정확함**.
- 그러나 **AA5a/b/c 는 axe-core 가 페이지 전체를 스캔**하므로 기존 SPA 의 16건 color-contrast 위반(검색 카드, 사이드바 텍스트 등)을 수집한다. bulk-actions 구현이 완료되어도 이 16건은 사라지지 않아 영구 FAIL.
- 의도는 "bulk-actions 컴포넌트의 ARIA 위반 검증" 인데, 페이지 전체 스캔이라 의도 표현이 부정확.

**수정 권장사항**: Axe 의 `include` / `context` 옵션으로 bulk-actions 컴포넌트 영역만 스캔하도록 한정. 예:
```python
results = axe.run(
    page,
    context={"include": [[".bulk-action-bar"]]},  # 또는 [".home-action-dropdown"]
    options={"runOnly": {"type": "tag", "values": list(DEFAULT_RULESET)}},
)
```
또는 `runOnly`에서 `color-contrast` 룰만 disable. AA5a (홈 초기) 는 bulk-actions DOM 자체가 없는 시점이라 시나리오 의의 약하므로 **AA5a 는 삭제 권장**, AA5b/c 만 유지하되 컨텍스트 한정.

---

### 축 3: 축 분리 — PASS

| 검사 | 결과 | 근거 |
|---|---|---|
| behavior 가 색/shadow 검증? | ✅ 없음 | behavior 파일 전체 grep 결과 `getComputedStyle.*color`, `box-shadow`, `background-color` 등 시각 속성 검증 0건 |
| visual 이 클릭/상태 변화 검증? | ✅ 없음 (사전 가시성만 있음) | visual:117-122, 144-146, 166-170, 215-225, 246-256 — 모두 가시성/카운트 검증으로 캡처 전제만 만들고, 클릭 후 동작 검증은 없음. `_select_n` 헬퍼는 캡처 셋업이지 검증 대상 X |
| a11y 가 비-a11y 로직 검증? | ✅ 없음 | a11y 파일은 role / aria-label / aria-live / aria-checked / focus-ring / Tab order / reduced-motion 만 검증. 비즈니스 로직 (예: API 호출, 카운트 갱신) 없음 |

**참고**: a11y:104-106 의 `cb.click()` + `el.checked` 검증은 a11y 의 영역 (네이티브 input 의 checked 속성이 ARIA 와 동기화되는지) 이므로 적절.

---

### 축 4: 엣지 케이스 누락 — WARN ⚠️

| 케이스 | 검증되었는가? | 비고 |
|---|---|---|
| 0개 회의 (빈 사이드바) 에서 Cmd+A | ❌ **누락** | 시드는 5건 회의이므로 빈 상태 미검증. spec §3 의 "회의 0개일 때" 엣지 |
| 선택 중 자동 새로고침 (워처가 새 회의 추가) | ❌ **누락** | bulk-actions.md §1.5 "selection mode 활성 중 새 회의 도착 → mode 유지, 새 항목 자동 선택 안 함" 정책 검증 없음 |
| 액션 in-flight 중 추가 액션 클릭 | ❌ **누락** | A4-A6 은 단일 클릭 후 응답 대기. 디바운스 / 버튼 disable 정책 미검증 |
| 모바일 viewport 에서 selection mode 진입 시 스크롤 | ❌ 누락 | V5 가 모바일 캡처만 — 스크롤 동작은 별도 |
| dark mobile 변종 (V5) 만 있고 light mobile 없음 | ⚠️ 의도적 가능 | mockup §3 은 light-mobile 만 명시 — V5 가 dark-mobile 인 것은 mockup 과 불일치. 의도라면 명시 필요 |
| axe-core 가 페이지 전체 스캔 vs 사이드바만 스캔 | ⚠️ 페이지 전체 (축 2 거짓 Red 와 직접 연결) | AA5 는 액션 바 진입 후의 **bulk-actions 컴포넌트 a11y 위반** 만 검증해야 하는데 페이지 전체를 보고 있음 |
| 동일 항목 재선택 (체크 → 해제 → 체크) | ⚠️ B4 가 부분 검증 | Cmd+클릭 토글 1회만, 같은 항목의 체크박스 재진입 idempotent 미검증 |
| 부분 적합성 (선택 5개 중 3개만 적합) toast | ⚠️ A8 부분 검증 | mock 응답이 `{queued: 3, skipped: 1}` 이지만, **클라이언트가 skipped 를 toast 에 정확히 노출하는지** 만 검증. 클라이언트가 적합성을 사전 필터링하는 로직(handoff §3.1 `executeAction` 의 `eligible.filter`) 은 검증 안됨 |
| 액션 실행 실패 (5xx 응답) | ❌ 누락 | 모든 mock 이 200 OK. 에러 toast / 액션 바 유지 / 재시도 정책 없음 |
| Shift+클릭 후 다시 Cmd+클릭 (앵커 갱신) | ❌ 누락 | B5 는 단일 흐름 (Cmd → Shift) 만 검증. 앵커 갱신 로직 미검증 |

**최우선 누락 (수정 요구)**:
1. **빈 사이드바 (회의 0건) 케이스** — 추가 시나리오 또는 기존 conftest 에 빈 시드 fixture 추가
2. **AA5 의 axe 컨텍스트 한정** — 축 2 와 동일 항목 (페이지 전체 → 컴포넌트 한정)
3. **에러 응답 시 toast / 상태 처리** — 새 시나리오 1건 (예: A10 — batch API 5xx 응답 시 사용자 피드백)

---

### 축 5: 고정 selector 컨벤션 — WARN ⚠️

| selector | handoff §1 명세 | 실제 SPA 코드 | 시나리오 사용 |
|---|---|---|---|
| `.meeting-item` | div / a (handoff §1.1 은 a 표기) | **div** (`spa.js:1085`) | ✅ 일관 |
| `.meeting-item-checkbox` | `<input type="checkbox">` | 미구현 | ✅ 명세 |
| `.meeting-item-text` | div | div (`spa.js:1128`) | ✅ |
| 사이드바 컨테이너 | handoff §1.1 `<div class="meetings-list" role="listbox" aria-multiselectable>` | **`#listContent.list-content`** (`index.html:119`) | ⚠️ **불일치** — behavior:228 / 254 가 `.meetings-list, #listContent` fallback 사용, 명세상 `meetings-list--selecting` 클래스 부여 위치 모호 |
| `.bulk-action-bar` 와 BEM 자식 | `__count`, `__count-num`, `__actions`, `__dismiss` | 미구현 | ✅ |
| `.bulk-action-btn[data-action]` | `transcribe`, `summarize`, **`both`** | 미구현 | ✅ — A6 의 selector data-action="both" 와 페이로드 action="full" 의 차이 명시되어 정상 |
| `.home-action-btn--dropdown[data-dropdown]` | `all-bulk`, `recent-24h` | 미구현 | ✅ |
| `[role="menuitemradio"][data-option]` | `both`, `transcribe`, `summarize` | 미구현 | ✅ |

**핵심 issue**:

handoff §1.1 은 **사이드바 부모 컨테이너에 `class="meetings-list"` 와 `aria-multiselectable="true"` 를 추가** 하라고 명시한다. 그러나 실제 SPA 의 부모 컨테이너 클래스는 `list-content` (id 는 `listContent`) 이다. 시나리오 B6 (behavior:227-230) 는 이 갭을 인식해 `.meetings-list, #listContent` 둘 다 시도하는 fallback selector 를 쓰지만, 다음과 같은 모호함이 남는다:

```python
# behavior:227
list_panel = ui_page.locator(".meetings-list, #listContent")
cls = list_panel.first.get_attribute("class") or ""
assert "meetings-list--selecting" in cls, ...
```

frontend-a 가 구현 시:
- 옵션 A) 부모에 `meetings-list` 클래스 추가 (handoff §1.1 그대로)
- 옵션 B) `list-content` 유지하고 `meetings-list--selecting` 만 추가
- 옵션 C) BEM 일관성을 위해 `list-content--selecting` 으로 변경

세 옵션 모두 spec / handoff 와 부분 일치하므로 **시나리오에서 단일 진실 정의 필요**. 권장: 시나리오 헬퍼를 `_meetings_list_panel(page)` 로 추출하고, 클래스 검증을 `meetings-list--selecting` 으로 고정하되 컨테이너는 `[role="listbox"][aria-multiselectable="true"]` 같은 ARIA 속성으로 한정 (CSS 클래스 의존 회피).

---

### 축 6: API 모킹 정확성 — PASS

| 검증 | 결과 |
|---|---|
| 단일 진실 응답 (`_BATCH_OK_RESPONSE`) | ✅ behavior:44 — `{queued: 3, skipped: 1, message: "..."}` |
| selected scope 페이로드 | ✅ A4 — `{action, scope: "selected", meeting_ids: [...]}` |
| all scope 페이로드 | ✅ H3 — `{action, scope: "all"}` (meeting_ids 없음) |
| recent scope + hours | ✅ H4 — `{action, scope: "recent", hours: 24}` |
| action 매핑 | ✅ transcribe / summarize / **full** (selector "both" 와 페이로드 "full" 분리 명확히 코멘트됨, A6:422-424) |
| 응답 키 (queued, skipped, message) | ✅ Phase 3 백엔드 계약과 합리적 일치 |
| route 핸들러 격리 | ✅ 각 테스트마다 새 context — 호출 카운트 누적 안 됨 |

**참고**: 응답 스키마는 Phase 3 백엔드가 만들 것이므로 **합리성 판단** 으로 PASS. 제안: `message` 가 다국어 (한국어) 라 향후 i18n 시 변경 가능 — A8 검증이 "처리"/"건너뜀"/"queued" 셋 중 하나만 있으면 통과로 관대하게 작성된 것은 적절한 안전장치.

---

### 축 7: 회귀 위험 — WARN ⚠️

| 검사 | 결과 |
|---|---|
| `tests/ui/conftest.py` 가 기존 fixture 와 충돌? | ✅ `demo_swatch_url` (function-scope) 와 새 `ui_bulk_*` (session-scope) 분리됨, 이름 충돌 없음 |
| `browser` fixture 의 출처 | ⚠️ **암묵 의존** — `tests/ui/conftest.py` 는 `browser` fixture 를 정의하지 않음. 시나리오들은 `browser: Browser` 파라미터를 사용하는데, 이는 **pytest-playwright 플러그인 빌트인 fixture** 에 의존. `test_e2e_edit_playwright.py` 는 자체 session-scoped `browser` fixture 정의(`tests/test_e2e_edit_playwright.py:240-246`) 와 다른 패턴 |
| 포트 격리 | ✅ 8765 (개발) / 8766 (e2e edit) / **8767 (ui bulk)** — 충돌 없음 |
| MT_BASE_DIR 격리 | ✅ `ui_bulk_base_dir` 가 `tmp_path_factory.mktemp` 로 분리 |
| 시드 패턴 | ✅ `_seed_meetings_for_bulk` 가 `test_e2e_edit_playwright._seed_meeting` 와 동일 sqlite + checkpoint 패턴 |
| pytest 마커 | ✅ 모든 파일에 `pytestmark = [pytest.mark.ui]` |
| 로컬 실행 시 서버 부팅 비용 | ⚠️ session-scope server fixture 는 처음 호출 시 ~7초 + pyannote 모델 로드 시간 소요. 40 시나리오 실행 1회 부팅이라 OK |

**개선 제안**: `tests/ui/conftest.py` 에 `pytest-playwright` 플러그인 명시 의존을 docstring 에 추가하거나, 만약 자체 `browser` fixture 가 필요하면 `test_e2e_edit_playwright.py:240` 패턴 차용 (현재로는 plugin 빌트인 사용이 더 간결).

---

## 3. qa-a 가 수정해야 할 항목 (changes_requested)

### 필수 (Phase 3 진행 차단)

1. **AA5a/b/c 거짓 Red 해소**
   - `_run_axe(page)` 헬퍼에 `context={"include": [[".bulk-action-bar"]]}` 또는 `[".home-action-dropdown"]` 추가하여 컴포넌트 한정 스캔
   - 또는 `color-contrast` 룰을 `disabledRules` 로 제외 (단, bulk-action-btn 의 hover 채움 4.04:1 이슈는 별도 alert)
   - **AA5a (홈 초기 상태) 는 bulk-actions DOM 부재 시점이라 검증 의의 약함 — 삭제 권장**
   - 근거: `tests/ui/a11y/test_bulk_actions_a11y.py::test_AA5a` 실측 결과 기존 SPA 의 16건 color-contrast violation 으로 영구 FAIL

2. **빈 사이드바 (회의 0건) 케이스 추가** — 신규 시나리오 1건
   - 위치 제안: `TestSidebarMultiSelect` 클래스에 `test_B10_빈_사이드바에서_Cmd_A는_no_op_이다` 추가
   - fixture 옵션: `ui_bulk_empty_base_dir` 같은 별도 시드 (회의 0건) — 또는 페이지 로드 후 모든 항목 DOM 제거 후 Cmd+A
   - spec §3 "회의 0개일 때" 엣지를 명시적으로 보호

3. **사이드바 컨테이너 selector 단일 진실**
   - `behavior:227-230`, `:254-257` 의 `.meetings-list, #listContent` fallback 을 헬퍼로 추출
   - 권장: `_meetings_list_panel(page) → page.locator("[role='listbox'][aria-multiselectable='true']")` (ARIA 속성 기준)
   - frontend-a 가 어떤 클래스 명을 채택하든 ARIA 계약은 고정

### 권장 (Phase 3 직전 또는 직후 수정)

4. **에러 응답 시 사용자 피드백 시나리오 추가** — A10
   - mock 이 5xx 응답을 반환할 때 toast / 액션 바 / 선택 상태가 어떻게 처리되는지 검증
   - 현 시나리오는 모두 200 OK 만 — 통신 실패 정책 미정의

5. **선택 중 자동 새로고침 시 selection 유지** — B11 (또는 별도 그룹)
   - bulk-actions.md §1.5 "selection mode 활성 중 새 회의 도착 → mode 유지, 새 항목 자동 선택 안 함" 정책
   - mock 또는 SSE 로 새 회의 추가 후 기존 선택 유지 검증

6. **light-mobile 변종 추가 또는 V5 dark→light 변경**
   - mockup §3 은 light-mobile 만 명시 — V5 가 dark-mobile 인 것은 mockup 과 불일치
   - 의도라면 mockup 에 dark-mobile 케이스 추가 필요 (designer-a 영역). 의도 아니면 V5 를 light-mobile 로 변경

7. **`browser` fixture 의존 명시** — `tests/ui/conftest.py` docstring 에 pytest-playwright 플러그인 의존 한 줄 추가

### 선택 (참고만)

8. A6 의 selector "both" ↔ payload "full" 차이를 docstring 코멘트에서 추가로 강조 (현재도 `behavior:422-424` 에 있음 — 주석은 충분)

9. `_install_batch_route_mock` 의 `_BATCH_OK_RESPONSE` 가 `{queued: 3, skipped: 1}` 고정인데 A6 (action="full") 시나리오에서도 동일 응답을 받는다 — 의미상 self-consistent 하므로 OK 이지만, scope 별 응답 차이가 있다면 mock 분기 필요 (Phase 3 백엔드 결정 후 확인)

---

## 4. 7-축 요약 표

| 축 | 판정 | 근거 (파일경로:라인 또는 실측) |
|---|---|---|
| 1. 시나리오 완전성 | ✅ PASS | spec §3 매핑 표 (보고서 §2.1) — 40/40 매핑됨 |
| 2. Red 의도성 | ❌ FAIL | AA5a 실측 결과 `color-contrast: 16 nodes` 거짓 Red — `tests/ui/a11y/test_bulk_actions_a11y.py:217` |
| 3. 축 분리 | ✅ PASS | grep 로 `getComputedStyle.*color` 등 시각 속성 0건 (behavior), API 호출 0건 (a11y), 클릭 후 동작 0건 (visual) |
| 4. 엣지 케이스 누락 | ⚠️ WARN | 빈 사이드바, 워처 추가, 액션 in-flight, 5xx 응답, 앵커 갱신 — 5건 누락 (보고서 §2.4) |
| 5. selector 컨벤션 | ⚠️ WARN | 사이드바 컨테이너 `.meetings-list` (handoff) ↔ `.list-content` (실제 SPA) 갭, `behavior:227-230` fallback 으로 우회 중 |
| 6. API 모킹 | ✅ PASS | scope (selected/all/recent), action (transcribe/summarize/full), hours, message 모두 합리적 |
| 7. 회귀 위험 | ⚠️ WARN | `browser` fixture 가 pytest-playwright 빌트인에 암묵 의존 (conftest 에 docstring 명시 필요) |

**최종 판정**: **수정 후 재검토 (changes_requested)** — 필수 항목 3건 수정 필요.

---

## 5. 통합 권한

PASS 시 Phase 3 (백엔드) 진행 가능 → **현재 판정으로는 진행 불가**. 필수 3건 수정 후 qa-b 재검토 요청.

