# Wave 1 — Empty State 컴포넌트 (Plan 1.1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wave 1 의 첫 컴포넌트 `empty-state` 를 8 에이전트 페어 풀 사이클로 처리해 main 머지. Demo 잔존물 정리 포함. 8 에이전트 시스템의 첫 실전 가동.

**Architecture:** 메인 Claude Code 세션 = PM-A. 각 단계마다 Agent 툴로 designer-a / designer-b / qa-a / qa-b / frontend-a / frontend-b / pm-b 를 디스패치. 모든 산출물은 SQLite 영속 + events 자동 기록. 한 컴포넌트 = 한 PR.

**Tech Stack:** Plan 0 의 `harness/` CLI · Playwright + Pillow (시각 회귀) · axe-playwright-python (접근성) · pytest

**Spec 참조:**
- `docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md` §3 (Wave 1 항목 1: 빈 상태)
- `docs/SYSTEM_AUDIT_2026-04-28.md` §1.2 빈 상태 패턴 미적용
- `docs/design.md` §3.7 빈 상태 패턴 (48px 아이콘 + 제목 + 설명 + CTA)

**완료 정의 (spec §3):** `ui/web/spa.js:950~952` 의 단순 "회의 없음" 텍스트를 design.md §3.7 기준으로 교체. 회의 0개 / 검색 결과 0개 / 채팅 빈 상태 모두 적용.

**대상 컴포넌트 식별자:** `empty-state` (티켓 id 는 `T-101` 예상)

---

## File Structure

본 plan 에서 만들거나 수정될 파일:

**제거 (Task 0):**
- `ui/web/_demo/swatch.html`
- `tests/ui/visual/test_demo_swatch.py`
- `tests/ui/behavior/test_demo_swatch.py`
- `tests/ui/a11y/test_demo_swatch.py`
- `tests/ui/visual/baselines/demo-swatch-{light,dark,mobile}.png` (3 파일)

**Designer-A 산출물 (Task 1):**
- Create: `docs/superpowers/ui-ux-overhaul/wave-1/empty-state-mockup.md`
- Create: `tests/ui/visual/baselines/empty-state-light.png`
- Create: `tests/ui/visual/baselines/empty-state-dark.png`
- Create: `tests/ui/visual/baselines/empty-state-mobile.png`

**QA-A 산출물 (Task 1):**
- Create: `tests/ui/visual/test_empty_state.py` (3 케이스: light/dark/mobile)
- Create: `tests/ui/behavior/test_empty_state.py` (회의 0개 / 검색 0개 / 채팅 빈 상태 시나리오)
- Create: `tests/ui/a11y/test_empty_state.py` (axe-core 검증)

**Frontend-A 변경 (Task 1):**
- Modify: `ui/web/spa.js` (회의 목록 빈 상태, 검색 빈 상태, 채팅 빈 상태 — `:950~952` 외 검색·채팅 위치도 손댐)
- Modify: `ui/web/style.css` (`.empty-state` 컴포넌트 스타일)

**기타:**
- `state/harness.db` — 티켓·게이트 결과 영속 (커밋 안 됨, 로컬 상태)
- `docs/superpowers/ui-ux-overhaul/00-overview.md` — `harness board rebuild` 결과 (PR 머지 후 별도 commit)

---

## Task 0: Wave 1 시작 준비 — 데모 잔존물 제거

**Goal:** Plan 0 데모 placeholder 를 제거해 깨끗한 baseline 확보.

**Files:**
- Delete: 7 files listed above

- [ ] **Step 1: 데모 파일·디렉토리 제거**

```bash
git rm tests/ui/visual/test_demo_swatch.py
git rm tests/ui/behavior/test_demo_swatch.py
git rm tests/ui/a11y/test_demo_swatch.py
git rm tests/ui/visual/baselines/demo-swatch-light.png
git rm tests/ui/visual/baselines/demo-swatch-dark.png
git rm tests/ui/visual/baselines/demo-swatch-mobile.png
git rm ui/web/_demo/swatch.html
# _demo 디렉토리가 비면 자동 제거됨; 수동 정리:
rmdir ui/web/_demo 2>/dev/null || true
```

- [ ] **Step 2: 회귀 테스트 — harness 단위 + UI 디렉토리는 비어있어야 함**

Run:
```bash
.venv/bin/python -m pytest tests/harness/ -q
```
Expected: 54 PASS

Run:
```bash
.venv/bin/python -m pytest tests/ui/ -q -m ui 2>&1 | tail -5
```
Expected: `no tests ran` 또는 `0 collected` (test_demo_swatch 가 없으므로)

- [ ] **Step 3: 게이트가 빈 디렉토리에서 GateMisconfigured 시끄럽게 실패하는지 검증**

```bash
rm -f /tmp/wave1-bootstrap.db
HARNESS_DB=/tmp/wave1-bootstrap.db .venv/bin/python -m harness ticket open --wave 1 --component test-empty-dir
HARNESS_DB=/tmp/wave1-bootstrap.db .venv/bin/python -m harness gate run T-101 --phase red 2>&1 | head -3
rm -f /tmp/wave1-bootstrap.db
```
Expected: `GateMisconfigured: visual test missing for component 'test-empty-dir'` (NO-OP PASS 안 일어남)

- [ ] **Step 4: Commit**

```bash
git add -A tests/ui/ ui/web/
git commit -m "정리: Plan 0 데모 잔존물 제거 (Wave 1 시작 준비)

ui/web/_demo/swatch.html, tests/ui/{visual,behavior,a11y}/test_demo_swatch.py,
tests/ui/visual/baselines/demo-swatch-{light,dark,mobile}.png 일괄 삭제.
Plan 0 의 인프라 검증용 placeholder. Wave 1 의 첫 실 컴포넌트(empty-state)
작업 시작 직전이므로 깨끗한 baseline 확보.

게이트의 NO-OP PASS 차단(GateMisconfigured) 동작 재확인 완료."
```

---

## Task 1: empty-state 풀 8 에이전트 사이클

**Goal:** `empty-state` 컴포넌트가 visual + behavior + a11y 3축 게이트를 통과하고 PM-B 의 머지 승인까지 받아 main 으로 머지.

**12 단계 사이클:** 티켓 → Designer-A → Designer-B → QA-A → QA-B → Red gate → Frontend-A → Frontend-B → Green gate → PM-B → PR → Close

### Step 1: 티켓 발급 (메인 세션 = PM-A)

- [ ] **Step 1: 티켓 발급**

```bash
.venv/bin/python -m harness ticket open --wave 1 --component empty-state
```
Expected output: `T-101`

Verify:
```bash
.venv/bin/python -m harness ticket show T-101
```
Expected: JSON with `"status": "pending"`, `"wave": 1`, `"component": "empty-state"`

### Step 2: Designer-A 디스패치 — 목업 + 시각 베이스라인

- [ ] **Step 2: Designer-A 디스패치**

Use the Agent tool with subagent_type `general-purpose` (Claude Code 의 ui-ux-designer-a 에이전트는 `.claude/agents/ui-ux/designer-a.md` 의 prompt 를 자동 로드하지만, plan 0 셋업에서 `name` 매칭으로 dispatch 가능한지 확인 필요. 매칭 안 되면 `general-purpose` + 본문 prompt 를 explicit 하게 전달).

Dispatch prompt (summary):

> 당신은 UI/UX Designer-A (Producer) 입니다. 정의 파일: `.claude/agents/ui-ux/designer-a.md`.
>
> **티켓**: T-101, component=`empty-state`, wave=1
>
> **사명**: Empty State 컴포넌트의 마크다운 목업 + 라이트/다크/모바일 3 변종 시각 베이스라인 PNG 작성.
>
> **참고**:
> - `docs/design.md` §3.7 (48px 아이콘 + 제목 + 설명 + CTA)
> - `docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md` §3 항목 1
> - 적용 위치 3곳: 회의 목록 0개 / 검색 결과 0개 / 채팅 빈 상태
>
> **산출물**:
> 1. `docs/superpowers/ui-ux-overhaul/wave-1/empty-state-mockup.md` — 목업 마크다운 (각 3 위치별 인터랙션 노트 포함)
> 2. `tests/ui/visual/baselines/empty-state-light.png` (Playwright 캡처, 1024x768)
> 3. `tests/ui/visual/baselines/empty-state-dark.png` (다크 미디어 쿼리 적용 후 캡처)
> 4. `tests/ui/visual/baselines/empty-state-mobile.png` (375x667 뷰포트)
>
> **베이스라인 캡처 방법**: 임시 HTML 파일 (`/tmp/empty-state-preview.html`) 생성 → `ui/web/style.css` 를 link → Designer-A 가 직접 작성한 placeholder DOM 으로 3 변종 캡처. **실제 ui/web/spa.js 는 아직 변경 안 함** (Frontend-A 의 영역).
>
> **자가 검증 체크리스트** 통과 후 events 기록:
> ```bash
> python -m harness review record --ticket T-101 --agent designer-a --kind self-check --status approved
> ```
>
> **절대 금지**:
> - `ui/web/spa.js`, `ui/web/style.css` 직접 수정 (Frontend-A 영역)
> - 베이스라인을 docs/design.md 토큰 외부 색상으로 캡처
> - 색 대비 < WCAG AA 4.5:1
>
> **Status 보고**: DONE / DONE_WITH_CONCERNS / BLOCKED / NEEDS_CONTEXT

Wait for Designer-A response. If `NEEDS_CONTEXT` or `BLOCKED`, provide context and re-dispatch. If `DONE_WITH_CONCERNS`, evaluate concerns before continuing.

Verify after Designer-A returns:
```bash
ls docs/superpowers/ui-ux-overhaul/wave-1/empty-state-mockup.md
ls tests/ui/visual/baselines/empty-state-{light,dark,mobile}.png
.venv/bin/python -m harness review record --ticket T-101 --agent designer-a --kind self-check --status approved 2>/dev/null || true
```
Expected: 4 files exist, no fatal errors.

### Step 3: Designer-B 디스패치 — 독립 리뷰

- [ ] **Step 3: Designer-B 디스패치**

Dispatch with subagent_type `general-purpose`. Prompt summary:

> 당신은 UI/UX Designer-B (Reviewer) 입니다. 정의 파일: `.claude/agents/ui-ux/designer-b.md`.
>
> **리뷰 대상 티켓**: T-101 (empty-state)
>
> **검토 항목 (체크리스트 6개)**:
> 1. 목업이 spec §3 항목 1 의 완료 정의 다 다루는가
> 2. 사용 토큰이 모두 `docs/design.md` 에 존재하는가 (rg 로 검증)
> 3. 다크 변종이 `#1C1C1E` 류 단계 톤 사용
> 4. WCAG AA 4.5:1 색 대비 통과
> 5. 모바일 변종(375px) 에서 가로 스크롤 없음
> 6. 베이스라인 PNG 파일명이 `empty-state-{light|dark|mobile}.png` 패턴
>
> **결과 기록**:
> - 모두 통과: `python -m harness review record --ticket T-101 --agent designer-b --kind peer-review --status approved`
> - 위반: `--status changes_requested --note "<체크리스트 항목 N: 구체 위반 사유>"`
>
> **절대 금지**: 직접 베이스라인·목업 작성 (Designer-A 영역). 단순 "보기 좋다" 류 통과.
>
> **Status 보고**: peer-review 결과 + 사유

If `changes_requested`, re-dispatch Designer-A with feedback. Loop until `approved`.

Verify:
```bash
.venv/bin/python -m harness review status --ticket T-101 2>&1
```
After this step expect `reviews incomplete` (peer-review approved 만, merge-final 아직 없음).

### Step 4: QA-A 디스패치 — 행동 시나리오 + a11y 룰셋

- [ ] **Step 4: QA-A 디스패치**

Dispatch prompt summary:

> 당신은 UI/UX QA-A (Producer) 입니다. 정의 파일: `.claude/agents/ui-ux/qa-a.md`.
>
> **티켓**: T-101 (empty-state)
>
> **입력**:
> - Designer-A/B 합의된 mockup: `docs/superpowers/ui-ux-overhaul/wave-1/empty-state-mockup.md`
> - 베이스라인: `tests/ui/visual/baselines/empty-state-{light,dark,mobile}.png`
> - spec §3 항목 1 완료 정의
> - spec §5.3 통과 기준
>
> **산출물 1**: `tests/ui/visual/test_empty_state.py` — 시각 회귀 3 케이스 (light/dark/mobile). 패턴은 데모 (이전 commit 537be82 의 test_demo_swatch.py 참고하되 demo-swatch 가 아닌 empty-state 베이스라인 사용). `harness.snapshot.assert_visual_match()` 호출.
>
> **산출물 2**: `tests/ui/behavior/test_empty_state.py` — Given-When-Then 시나리오 3개:
> 1. 회의가 0개일 때 → "첫 회의를 시작해 보세요" CTA 가 보인다
> 2. 검색 결과가 0개일 때 → "다른 키워드를 시도하세요" 안내가 보인다
> 3. 채팅 비어있을 때 → "회의에 대해 질문해 보세요" 안내가 보인다
>
> **산출물 3**: `tests/ui/a11y/test_empty_state.py` — `axe-playwright-python` 으로 wcag2a + wcag2aa + wcag21aa 룰셋 통과 검증
>
> **Red 의도성 검증**: 현재 ui/web/spa.js 상태(미구현) 에서 pytest 실행 → 정확히 FAIL 확인. (베이스라인은 designer 가 만든 placeholder 와 다를 것, behavior 시나리오는 새 텍스트가 없으므로 FAIL, a11y 는 통과할 수도 있지만 다른 두 축이 FAIL 이면 OK)
>
> **자가 검증 체크리스트** 통과 후:
> ```bash
> python -m harness review record --ticket T-101 --agent qa-a --kind self-check --status approved
> ```
>
> **절대 금지**:
> - `pytest.mark.ui` 마커 누락
> - 한 시나리오에 여러 컴포넌트 검증
> - 픽셀 비교를 behavior 에 섞기
>
> **Status 보고**: DONE / DONE_WITH_CONCERNS / BLOCKED / NEEDS_CONTEXT

Verify after QA-A:
```bash
ls tests/ui/visual/test_empty_state.py tests/ui/behavior/test_empty_state.py tests/ui/a11y/test_empty_state.py
```
Expected: 3 files exist

### Step 5: QA-B 디스패치 — 시나리오 검토

- [ ] **Step 5: QA-B 디스패치**

Dispatch prompt summary:

> 당신은 UI/UX QA-B (Reviewer) 입니다. 정의 파일: `.claude/agents/ui-ux/qa-b.md`.
>
> **리뷰 대상 티켓**: T-101 (empty-state)
>
> **검토 항목 (체크리스트 6개)**:
> 1. **Red 의도성**: `git stash` 후 baseline 에서 pytest 실행 → 정확히 FAIL 하는가
> 2. **축 분리**: behavior 시나리오에 `to_have_screenshot` / 픽셀 비교 섞이지 않음
> 3. **축 분리**: a11y 에 행동 검증 섞이지 않음
> 4. **엣지 케이스**: 키보드만 사용·다크 변종·로딩 상태 누락 없음
> 5. **시나리오 격리**: 한 시나리오가 다른 시나리오 결과에 의존하지 않음
> 6. **마커**: 모두 `pytest.mark.ui` 보유
>
> **결과 기록**: approved 또는 changes_requested + note
>
> **절대 금지**: 직접 시나리오 작성, 막연한 "더 많은 테스트" 류 거부.

If `changes_requested`, re-dispatch QA-A. Loop until `approved`.

Note: QA-A and QA-B's `peer-review` overrides Designer-A/B's previous peer-review record (latest_status() 가 가장 최근만 반환). 이건 정상 동작 — 산출물 단위가 아닌 ticket 단위 합의.

### Step 6: Red gate — 3축 모두 FAIL 확인

- [ ] **Step 6: Red gate 실행**

```bash
.venv/bin/python -m harness gate run T-101 --phase red
```

Expected output:
```
gate red for T-101
  visual    FAIL    ← 베이스라인과 현재 캡처 (placeholder 부재) 차이
  behavior  FAIL    ← 새 텍스트가 없으므로 시나리오 미통과
  a11y      ?       ← 통과할 수도 (현재 빈 상태가 a11y 위반은 아닐 수 있음)
```

If visual or behavior PASS instead of FAIL, **시나리오가 너무 약함** — QA-A 에게 보강 의뢰 (Step 5 로 회귀).

If 모두 FAIL → 정상. Step 7 진행.

Inspect failure detail:
```bash
ls state/gate-logs/T-101-*.log
cat state/gate-logs/T-101-visual.log | head -30
```

### Step 7: Frontend-A 디스패치 — 최소 구현

- [ ] **Step 7: Frontend-A 디스패치**

Dispatch prompt summary:

> 당신은 UI/UX Frontend-A (Producer) 입니다. 정의 파일: `.claude/agents/ui-ux/frontend-a.md`.
>
> **티켓**: T-101 (empty-state)
>
> **입력**:
> - Designer-A/B 합의된 mockup: `docs/superpowers/ui-ux-overhaul/wave-1/empty-state-mockup.md`
> - QA-A/B 합의된 시나리오: `tests/ui/{visual,behavior,a11y}/test_empty_state.py`
> - 기존 코드: `ui/web/spa.js` (특히 `:950~952` 회의 목록 빈 상태), `ui/web/style.css`
> - `docs/design.md` 토큰
>
> **사명**: 3축 게이트를 모두 통과시키는 **최소** 변경. `ui/web/spa.js` 의 3 위치 (회의 목록 / 검색 결과 / 채팅) 빈 상태를 design.md §3.7 에 맞게 교체. `ui/web/style.css` 에 `.empty-state` 컴포넌트 스타일 추가.
>
> **자가 검증 체크리스트**:
> 1. 변경 파일이 `ui/web/*` 안에만
> 2. 신규 import 없음
> 3. `python -m harness gate run T-101 --phase green` 의 셋 다 PASS (단, review 미완 시 ReviewIncomplete — 이는 정상, 아래 참고)
> 4. diff < 100 줄
> 5. SPA Router 와 충돌 없음
>
> **주의**: green gate 는 review.all_passed() 강제. Frontend-A 단계에서는 아직 frontend-b 리뷰가 없어 ReviewIncomplete 가 정상. 대신 `pytest tests/ui/ -m ui -v` 로 직접 검증.
>
> **자가 검증 체크리스트** 통과 후:
> ```bash
> python -m harness review record --ticket T-101 --agent frontend-a --kind self-check --status approved
> ```
>
> **절대 금지**: 신규 의존성, 백엔드 변경, 시나리오 약화, 토큰 인라인, 보너스 리팩토링

Verify after Frontend-A:
```bash
git diff --stat ui/web/  # change scope
.venv/bin/python -m pytest tests/ui/ -m ui -v
```
Expected: tests pass (visual diff < 0.1%, behavior 시나리오 통과, a11y 0 위반)

### Step 8: Frontend-B 디스패치 — 코드 리뷰

- [ ] **Step 8: Frontend-B 디스패치**

Dispatch prompt summary:

> 당신은 UI/UX Frontend-B (Reviewer) 입니다. 정의 파일: `.claude/agents/ui-ux/frontend-b.md`.
>
> **리뷰 대상**: `git diff` 의 ui/web/* 변경
>
> **검토 항목 (체크리스트 8개)**:
> 1. ticket.component(empty-state) 외 영역 미변경
> 2. 같은 로직 중복 없음 (DRY)
> 3. SPA Router 라우트 정의 깨지 않음
> 4. 기존 이벤트 핸들러 손댔다면 다른 호출처 영향 없음 (rg 검증)
> 5. CSS 변수 추가/변경이 design.md 토큰 룰 위반 없음
> 6. `:focus-visible` 같은 공용 토큰 인라인 안 함
> 7. console.log / 디버그 코드 잔존 없음
> 8. 신규 의존성 (package.json/pyproject.toml) 없음
>
> **결과 기록**: approved 또는 changes_requested + 구체적 file:line 인용

If `changes_requested`, re-dispatch Frontend-A. Loop until `approved`.

### Step 9: Green gate — 3축 PASS

- [ ] **Step 9: Green gate 실행**

```bash
.venv/bin/python -m harness gate run T-101 --phase green
```

Expected:
```
gate green for T-101
  visual    PASS
  behavior  PASS
  a11y      PASS
```

If `ReviewIncomplete` raised → Step 8 의 Frontend-B 가 approved 안 했거나 merge-final 누락. Step 10 (PM-B) 후 재시도.

Note: peer-review 의 latest 가 frontend-b 의 approved 여야 함. 만약 designer-b 또는 qa-b 의 changes_requested 가 latest 면 다시 designer-a / qa-a 로 회귀.

### Step 10: PM-B 디스패치 — 머지 최종 승인

- [ ] **Step 10: PM-B 디스패치**

Dispatch prompt summary:

> 당신은 UI/UX PM-B (Reviewer) 입니다. 정의 파일: `.claude/agents/ui-ux/pm-b.md`.
>
> **머지 제안 검토 대상**: 티켓 T-101 (empty-state)
>
> **체크리스트 5개**:
> 1. `gate_runs` 의 최신이 phase=green + 3 PASS
> 2. 모든 산출물(`artifacts`) 의 review.peer = approved (events 조회)
> 3. `git diff feature/ui-ux-harness..HEAD` 가 ui/web/* 외부 미변경
> 4. 신규 의존성 미추가 (`pip freeze` diff)
> 5. PR 본문이 본 컴포넌트 1개만 다룸
>
> **결과 기록**:
> - 승인: `python -m harness review record --ticket T-101 --agent pm-b --kind merge-final --status approved`
> - 거부: `--status changes_requested --note "<사유>"`

If `changes_requested`, address PM-B's concerns and re-run from Step 7 or earlier as needed.

Verify all reviews approved:
```bash
.venv/bin/python -m harness review status --ticket T-101
```
Expected: `all reviews approved`

### Step 11: PR 생성 + 머지

- [ ] **Step 11a: PR 생성용 브랜치 분기**

T-101 변경사항을 별도 브랜치로 분리해 깔끔한 PR 만들기. (현재 작업이 feature/ui-ux-harness 의 후속이라 base 는 main 이지만 PR 21 머지 전이면 PR 21 위에 쌓이는 형태).

```bash
# 현재 브랜치는 feature/ui-ux-harness 가정
# Wave 1 작업용 새 브랜치
git checkout -b feature/wave-1-empty-state
# Task 0 (데모 제거) + Task 1 (구현) 의 commit 들이 이미 이 위에 쌓여있음
```

If commits are already on `feature/ui-ux-harness`, alternative:
```bash
git push -u origin feature/ui-ux-harness  # 이미 push 됐으면 추가 push
```

- [ ] **Step 11b: PR 생성**

```bash
gh pr create --base main --head feature/wave-1-empty-state \
  --title "기능: Wave 1 - empty-state 컴포넌트 (design.md §3.7 적용)" \
  --body "$(cat <<'PRBODY'
## Summary

Wave 1 의 첫 컴포넌트. \`ui/web/spa.js\` 의 회의 목록 / 검색 결과 / 채팅 빈 상태를
\`docs/design.md\` §3.7 기준 (48px 아이콘 + 제목 + 설명 + CTA) 으로 교체.

8 에이전트 페어 풀 사이클 통과 첫 사례:
- Designer-A 목업 + 베이스라인 → Designer-B 토큰·색대비 검증 ✓
- QA-A 시나리오 + a11y 룰셋 → QA-B 완전성·Red 의도성 검증 ✓
- Frontend-A 구현 → Frontend-B 코드 리뷰 ✓
- PM-B 머지 최종 승인 ✓

## Test plan

- [x] 3축 Red gate 모두 FAIL 확인
- [x] 3축 Green gate 모두 PASS (visual <0.1% / behavior 100% / a11y 0 위반)
- [x] 모든 review.peer-review 와 review.merge-final approved

🤖 Generated with [Claude Code](https://claude.com/claude-code)
PRBODY
)"
```

- [ ] **Step 11c: 사용자에게 보고 + 머지 승인 대기**

Stop. Wait for the human to merge. **DO NOT auto-merge.**

### Step 12: 머지 후 정리 — `harness ticket close`

- [ ] **Step 12: 티켓 closed 로 전환**

After human merges PR (assume #N):

```bash
.venv/bin/python -m harness ticket close T-101 --pr <N>
.venv/bin/python -m harness board rebuild
git add docs/superpowers/ui-ux-overhaul/00-overview.md
git commit -m "문서: T-101(empty-state) closed — 보드 갱신"
git push
```

Verify final state:
```bash
.venv/bin/python -m harness ticket show T-101 | grep status
# Expected: "status": "closed"
```

---

## Self-Review (Plan 작성자 자체 검토)

### 1. Spec coverage

| Spec 항목 | Plan task | 상태 |
|---|---|---|
| §3 Wave 1 빈 상태 패턴 | Task 1 전체 | ✓ |
| §4.3 8 에이전트 페어 모두 가동 | Task 1 Step 2-10 | ✓ (designer-a/b, qa-a/b, frontend-a/b, pm-b — pm-a 는 메인 세션) |
| §4.3.1 크로스체크 게이트 | Step 3, 5, 8, 10 의 review.peer-review + Step 10 의 merge-final | ✓ |
| §4.4 TDD 사이클 | Step 6 (red) → Step 7 (green minimal) → Step 9 (green gate) | ✓ |
| §4.5 CLI 사용 | ticket open/show/close, gate run, review record/status, board rebuild 모두 사용 | ✓ |
| §5.3 통과 기준 | Step 9 의 3 PASS = visual <0.1% / behavior 100% / a11y 0 위반 | ✓ |
| §6 에러 처리 | Red gate 부분 PASS 시 시나리오 보강 회귀, ReviewIncomplete / GateMisconfigured 시끄러운 fail | ✓ |

### 2. Placeholder 스캔

- "TBD" / "TODO" / "implement later" 없음
- Empty State 컴포넌트의 실제 텍스트 (예: "첫 회의를 시작해 보세요") 는 designer/frontend 에이전트가 design.md §3.7 + 컨텍스트 보고 결정 — plan 단계에서 단어 단위 박제는 over-prescription.
- 디자이너의 Playwright 캡처 명령은 Plan 0 의 demo (commit `537be82`) 코드 패턴 참조하라고 명시 — implementer 가 그걸 보면 됨.

### 3. Type 일관성

- 티켓 id `T-101` 일관 사용
- review kind 4종 (`self-check`/`peer-review`/`merge-proposal`/`merge-final`) 와 status 3종 (`approved`/`changes_requested`/`pending`) 모두 Plan 0 의 review.py VALID_* 와 일치
- `harness review record` 옵션명 (`--ticket`, `--agent`, `--kind`, `--status`, `--note`) 모두 cli.py 와 일치
- `harness gate run` 인자: `<ticket-id> --phase red|green` cli.py 와 일치

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-28-ui-ux-wave-1-empty-state.md`. 두 가지 실행 옵션:

**1. Subagent-Driven (권장)** — 본 Plan 의 Step 1-12 가 본질적으로 "에이전트 디스패치 사이클" 이므로 메인 세션이 PM-A 로서 계속 활동. Step 마다 별도 implementer 디스패치보다는 메인 세션이 직접 Agent 툴 호출.

**2. Inline Execution** — 본 세션에서 단계별 진행, 사용자 검토.

본 Plan 의 본질이 "8 에이전트 사이클 가동" 이므로 **Inline 실행이 더 자연스러움**. 메인 세션이 PM-A 로서 직접 Agent 툴로 designer-a/b, qa-a/b, frontend-a/b, pm-b 를 차례로 디스패치하고, harness CLI 로 게이트·리뷰·보드 운영. Plan 1.2 / 1.3 (skeleton-shimmer / dark-mode-tones) 은 Plan 1.1 결과 보고 동일 패턴으로 진행.

후속 plans (Plan 0 완료 후 작성):
- `2026-04-28-ui-ux-wave-1-skeleton-shimmer.md` — Wave 1 항목 2
- `2026-04-28-ui-ux-wave-1-dark-mode-tones.md` — Wave 1 항목 3
