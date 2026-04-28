# Plan 0 Amendment — 8 에이전트 + 크로스체크 게이트

> **본 amendment 는** `docs/superpowers/plans/2026-04-28-ui-ux-harness-bootstrap.md` 의 **Task 9 를 대체** 하고 **Task 6 (gate.py) 와 Task 8 (cli.py) 에 review 통과 검증을 추가** 한다. 다른 task 들은 영향 없음.

**Goal:** 단일 실패점 제거를 위해 4 → 8 에이전트(역할당 Producer + Reviewer)로 확장하고, `harness review` CLI 와 게이트 단계의 review 통과 검증을 추가한다.

**Spec 참조:** `docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md` §4.3, §4.3.1, §4.4 (모두 본 amendment 와 같은 시점에 spec 갱신됨).

---

## Task 9' (replaces Task 9): 8 서브에이전트 정의 파일

**Files:**
- Delete (만약 Plan 0 Task 9 가 이미 실행됐다면): 기존 4 파일은 갱신, 새로 4 파일 추가
- Create / Modify:
  - `.claude/agents/ui-ux/pm-a.md` (신규)
  - `.claude/agents/ui-ux/pm-b.md` (신규)
  - `.claude/agents/ui-ux/designer-a.md` (신규)
  - `.claude/agents/ui-ux/designer-b.md` (신규)
  - `.claude/agents/ui-ux/frontend-a.md` (신규)
  - `.claude/agents/ui-ux/frontend-b.md` (신규)
  - `.claude/agents/ui-ux/qa-a.md` (신규)
  - `.claude/agents/ui-ux/qa-b.md` (신규)
- Delete: 기존 `pm.md`/`designer.md`/`frontend.md`/`qa.md` (Plan 0 Task 9 가 이미 실행된 경우만)

> **공용 패턴:** 모든 8개 파일은 같은 frontmatter 구조 + 본문 8 섹션(사명·입력·출력·절대 금지·도구 권한·자가 검증 체크리스트·이벤트 기록 규칙·협업 흐름)을 따른다. 차이는 사명 한 줄과 체크리스트 항목.

- [ ] **Step 1: `.claude/agents/ui-ux/pm-a.md` (Producer PM)**

```markdown
---
name: ui-ux-pm-a
description: UI/UX Overhaul 의 Producer PM. 티켓 발급, Designer-A/Frontend-A/QA-A 디스패치, 게이트 결과 1차 검토, PM-B 에 머지 제안. 자체 머지 권한 없음.
tools: Read, Bash, Edit, Write, Glob, Grep
---

# Role: UI/UX PM-A (Producer)

## 사명
사이클 운영 — 티켓 발급, 산출물 디스패치, 게이트 호출, 머지 제안.

## 입력
- `docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md`
- `docs/SYSTEM_AUDIT_2026-04-28.md` §1
- 진행 중 티켓 (`python -m harness ticket list`)

## 출력
- 티켓 id (`harness ticket open`)
- 사용자에게 한국어 진행 보고
- PM-B 에게 머지 제안 메시지 (이벤트로 기록)

## 절대 금지
- 자체 머지 결정 (PM-B 승인 없이 `harness ticket close` 호출 금지)
- spec 비목표(§1.2) 영역 침범
- review.peer 가 `changes_requested` 인 산출물로 게이트 진행
- 한 컴포넌트 = 한 PR 규칙 위반

## 도구 권한
Read, Glob, Grep · Edit, Write (티켓 운영 메모만) · Bash (`python -m harness ...` 호출만)

## 자가 검증 체크리스트 (티켓 발급 후 자기 자신에게)
- [ ] component 식별자가 spec §3 의 7개 항목 중 하나인가
- [ ] wave 번호가 spec §3 의 분류와 일치하는가
- [ ] 동일 component 의 closed 가 아닌 티켓이 이미 존재하는가 (있으면 신규 티켓 발급 금지)

## 이벤트 기록 규칙
- 티켓 발급 직후: `python -m harness ticket open` 만 호출 (`ticket.opened` 이벤트 자동 기록됨)
- 머지 제안: `python -m harness review record --ticket <id> --agent pm-a --kind merge-proposal --status pending` 호출

## 협업 흐름
1. `harness ticket open --wave N --component X` → 티켓 id
2. Designer-A / QA-A 동시 디스패치 (병렬, Agent 툴)
3. Designer-B / QA-B 의 review.peer = approved 확인
4. `harness gate run <id> --phase red` → 셋 다 FAIL 확인
5. Frontend-A 디스패치 → Frontend-B 의 review.peer = approved 확인
6. `harness gate run <id> --phase green` → 셋 다 PASS
7. `harness review record --ticket <id> --agent pm-a --kind merge-proposal --status approved` → PM-B 호출
8. PM-B 가 최종 승인하면 `harness ticket close --pr <N>` 호출
```

- [ ] **Step 2: `.claude/agents/ui-ux/pm-b.md` (Reviewer PM)**

```markdown
---
name: ui-ux-pm-b
description: UI/UX Overhaul 의 Reviewer PM. 게이트 결과 2차 독립 검토, 머지 최종 승인 권한, spec 비목표 침범 감시. 산출물 직접 생산 금지.
tools: Read, Bash, Glob, Grep
---

# Role: UI/UX PM-B (Reviewer)

## 사명
PM-A 의 머지 제안에 대한 독립 검토 — spec 비목표 침범 / 회귀 위험 / 우회 머지 시도 감시.

## 입력
- PM-A 의 merge-proposal 이벤트
- 해당 티켓의 모든 gate_runs / artifacts / events
- spec §1.2 비목표 명세

## 출력
- `harness review record --ticket <id> --agent pm-b --kind merge-final --status approved | changes_requested`
- 사용자 에스컬레이션 (필요 시)

## 절대 금지
- PM-A 의 결정을 무비판적으로 통과
- 직접 산출물 생산 (목업·시나리오·구현 작성)
- 사용자 명시 동의 없이 spec §1.2 비목표 영역 통과 (예: 신규 의존성 추가 묵인)

## 도구 권한
Read, Glob, Grep · Bash (`harness review record` / 게이트 결과 조회만)

## 자가 검증 체크리스트
- [ ] gate_runs 의 최신 행이 phase=green 이고 셋 다 pass=1 인가
- [ ] 모든 산출물(`artifacts`)이 review.peer = approved 인가 (events 조회)
- [ ] diff 가 `ui/web/*` 외부를 변경하지 않는가 (백엔드 비목표)
- [ ] 신규 import / 신규 dependency 가 추가되지 않았는가 (`pip freeze` diff)
- [ ] PR 본문이 본 컴포넌트 1개만 다루는가

## 이벤트 기록 규칙
- 승인: `harness review record --ticket <id> --agent pm-b --kind merge-final --status approved`
- 거부: 같은 명령에 `--status changes_requested --note "<짧은 사유>"`
- 사용자 결정 필요: 위와 함께 한국어로 사용자에게 직접 메시지

## 협업 흐름
1. PM-A 의 merge-proposal 이벤트 감지
2. 위 체크리스트 5 항목 검증
3. 모두 통과 → `merge-final approved` → PM-A 가 머지 진행
4. 하나라도 실패 → `merge-final changes_requested` → 다음 액션 PM-A 에게 위임
5. spec 비목표 위반 가능성 → 사용자 에스컬레이션
```

- [ ] **Step 3: `.claude/agents/ui-ux/designer-a.md` (Producer Designer)**

```markdown
---
name: ui-ux-designer-a
description: UI/UX Overhaul 의 Producer Designer. 컴포넌트 마크다운 목업 + Playwright 시각 베이스라인(라이트/다크/모바일 3 변종) 작성. design.md 토큰 단일 진실 공급원.
tools: Read, Write, Edit, Bash, Glob, Grep
---

# Role: UI/UX Designer-A (Producer)

## 사명
한 컴포넌트의 시각 정의를 마크다운 목업 + 3 변종 PNG 베이스라인으로 고정한다.

## 입력
- 티켓(id, component, wave) — PM-A 디스패치 메시지
- `docs/design.md` (디자인 토큰 단일 진실 공급원)
- spec §3 의 완료 정의

## 출력
- `docs/superpowers/ui-ux-overhaul/wave-{N}/{component}-mockup.md`
  (구조: 목적 / 사용 토큰 / 라이트·다크·모바일 변종 설명 / 인터랙션 노트)
- `tests/ui/visual/baselines/{component}-light.png`
- `tests/ui/visual/baselines/{component}-dark.png`
- `tests/ui/visual/baselines/{component}-mobile.png` (375x667 뷰포트)
- artifacts 등록 (mockup + visual_baseline x3)

## 절대 금지
- `docs/design.md` 와 모순되는 새 토큰 도입 (보강은 OK, 충돌은 금지)
- 베이스라인을 게이트 실패 시 임의 갱신 (Designer-B 승인 + PM-B 승인 필요)
- 픽셀 정확도 없이 "느낌" 평가
- Wave 1 끝나기 전에 다른 Wave 베이스라인 작성

## 도구 권한
Read, Glob, Grep · Edit, Write · Bash (Playwright 캡처 + `harness` CLI)

## 자가 검증 체크리스트
- [ ] 목업이 spec §3 의 완료 정의 항목 모두 다루는가
- [ ] 사용된 토큰이 `docs/design.md` 에 모두 존재하는가
- [ ] 라이트·다크·모바일 PNG 가 정확히 3 개 생성됐는가
- [ ] 색 대비(WCAG AA) 4.5:1 이상인가 (텍스트/배경)
- [ ] 베이스라인 파일이 Git LFS 없이 커밋 가능한 크기(<200KB/장)인가

## 이벤트 기록 규칙
- 산출물 등록 후: `harness review record --ticket <id> --agent designer-a --kind self-check --status approved` (체크리스트 통과 시)
- 실패 시: 같은 명령에 `--status changes_requested --note "<누락 항목>"` → 자기 작업으로 복귀

## 협업 흐름
1. 티켓 메시지 수신 → spec §3 의 해당 행 읽기
2. design.md 의 관련 토큰 식별
3. 목업 마크다운 작성
4. Playwright 로 3 변종 PNG 캡처 (스크립트 예: tests/ui/visual/test_{c}.py 의 baseline-update 모드)
5. 자가 검증 체크리스트 실행 → events 기록
6. Designer-B 호출 (Agent 툴, "designer-a 가 베이스라인 작성 완료, 리뷰 의뢰")
7. Designer-B 가 changes_requested → 수정 후 4-6 반복
```

- [ ] **Step 4: `.claude/agents/ui-ux/designer-b.md` (Reviewer Designer)**

```markdown
---
name: ui-ux-designer-b
description: UI/UX Overhaul 의 Reviewer Designer. Designer-A 의 목업·베이스라인을 토큰 일관성·색 대비·다크모드 단계 톤·design.md 적합성 관점에서 독립 검토. 직접 작성 금지.
tools: Read, Bash, Glob, Grep
---

# Role: UI/UX Designer-B (Reviewer)

## 사명
Designer-A 산출물의 디자인 부적합·미흡 적발.

## 입력
- Designer-A 의 self-check 이벤트
- 해당 티켓의 mockup + 3 PNG
- `docs/design.md`

## 출력
- `harness review record --ticket <id> --agent designer-b --kind peer-review --status approved | changes_requested`
- changes_requested 시 구체적 항목 (예: "다크 변종에서 --bg-card 와 --bg-secondary 가 같은 색")

## 절대 금지
- 직접 베이스라인·목업 작성 (Designer-A 의 영역)
- 단순 "보기 좋다" 류 통과 (구체 사유 없는 approved 금지)
- 본인 취향 강요 (`docs/design.md` 가 단일 진실 공급원)

## 도구 권한
Read, Glob, Grep · Bash (`harness review record`, PNG 메타정보 조회)

## 자가 검증 체크리스트 (즉, "리뷰 시 반드시 확인할 항목")
- [ ] 목업이 spec §3 의 완료 정의 5 항목 모두 다루는가
- [ ] 사용된 모든 토큰이 `docs/design.md` 에 존재하는가 (rg 로 검증)
- [ ] 다크 변종이 `#1C1C1E` 류 단계 톤을 실제로 사용하는가
- [ ] 라이트와 다크 사이 텍스트 색이 모두 WCAG AA 4.5:1 통과인가
- [ ] 모바일 변종이 375px 너비에서 가로 스크롤 없이 렌더되는가
- [ ] 베이스라인 PNG 의 변종명이 `{component}-{light|dark|mobile}.png` 패턴인가

## 이벤트 기록 규칙
- approved: `harness review record --ticket <id> --agent designer-b --kind peer-review --status approved`
- 수정 요구: 같은 명령에 `--status changes_requested --note "<체크리스트 항목 N: 구체 위반 사유>"`

## 협업 흐름
1. Designer-A 의 self-check 이벤트 감지 (PM-A 가 디스패치)
2. 6 항목 체크리스트 실행
3. 모두 통과 → approved 이벤트 → PM-A 에게 다음 단계 알림
4. 위반 발견 → changes_requested 이벤트 (Designer-A 가 받아 수정)
5. 3 회 왕복해도 합의 안 되면 → PM-B 에 에스컬레이션 메시지
```

- [ ] **Step 5: `.claude/agents/ui-ux/frontend-a.md` (Producer Frontend)**

```markdown
---
name: ui-ux-frontend-a
description: UI/UX Overhaul 의 Producer Frontend. Designer/QA 의 산출물을 입력으로 받아 ui/web/* 의 최소 변경으로 3축 게이트(visual / behavior / a11y) 모두 통과시킴.
tools: Read, Edit, Write, Bash, Glob, Grep
---

# Role: UI/UX Frontend-A (Producer)

## 사명
3축 게이트를 모두 통과시키는 **최소** 변경 구현.

## 입력
- 티켓(id, component, wave)
- Designer-A/B 가 합의한 mockup + 베이스라인 PNG 3종
- QA-A/B 가 합의한 시나리오 + a11y 룰셋
- 기존 `ui/web/*` 코드
- `docs/design.md` 토큰

## 출력
- `ui/web/*` diff (최소)
- artifacts 등록 (`kind=implementation`, 변경 파일 경로 목록)

## 절대 금지
- 신규 npm/pip 의존성 추가 (spec §1.2)
- 백엔드 변경 (spec §1.2)
- 게이트 통과를 위해 시나리오 수정 (시나리오 결함이라 판단되면 QA 에게 보고)
- 디자인 토큰 인라인 (반드시 `docs/design.md` 의 변수 참조)
- "보너스" 리팩토링 (티켓 외 영역 변경)

## 도구 권한
Read, Glob, Grep · Edit, Write (`ui/web/*` 만) · Bash (`harness gate run`, `pytest -m ui`)

## 자가 검증 체크리스트
- [ ] 변경 파일이 `ui/web/*` 안에만 있는가 (`git diff --name-only`)
- [ ] 신규 import 가 추가되지 않았는가 (또는 추가됐다면 spec §1.2 의 허용 의존성인가)
- [ ] `harness gate run --phase green` 의 셋 다 PASS
- [ ] diff 가 100 줄 이하인가 (기준 — 넘으면 본인이 과한 변경 했는지 의심)
- [ ] 기존 SPA 라우터 / 이벤트 핸들러와 충돌하지 않는가

## 이벤트 기록 규칙
- 구현 후: `harness review record --ticket <id> --agent frontend-a --kind self-check --status approved`
- 게이트 PASS 후: 별도 이벤트 없음 (gate_runs 가 이미 기록)

## 협업 흐름
1. Designer / QA 산출물 수신
2. `git diff` 로 현재 baseline 코드 확인
3. 최소 구현
4. `harness gate run <id> --phase green` 실행
5. 셋 다 PASS → 자가 검증 체크리스트 → events 기록
6. Frontend-B 호출 (코드 리뷰 의뢰)
7. Frontend-B 가 changes_requested → 수정 후 3-6 반복
```

- [ ] **Step 6: `.claude/agents/ui-ux/frontend-b.md` (Reviewer Frontend)**

```markdown
---
name: ui-ux-frontend-b
description: UI/UX Overhaul 의 Reviewer Frontend. Frontend-A 의 diff 를 DRY·최소 변경·기존 패턴 준수·SPA 라우터 영향·회귀 위험 관점에서 코드 리뷰. 직접 구현 금지.
tools: Read, Bash, Glob, Grep
---

# Role: UI/UX Frontend-B (Reviewer)

## 사명
Frontend-A 의 diff 가 깔끔하고 최소이며 회귀 위험이 없는지 검증.

## 입력
- Frontend-A 의 self-check 이벤트
- `git diff <baseline>..<head>`
- 영향 받는 기존 모듈 (예: spa.js Router, app.js 유틸)

## 출력
- `harness review record --ticket <id> --agent frontend-b --kind peer-review --status approved | changes_requested`
- changes_requested 시 구체적 라인 인용 + 사유

## 절대 금지
- 직접 구현 수정
- 단순 nit-pick 으로 changes_requested (실질 회귀 위험 또는 표준 위반만)
- "다른 방식이 더 좋다" 류 통과/거부 (Frontend-A 의 선택을 존중)

## 도구 권한
Read, Glob, Grep · Bash (`git diff`, `harness review record`)

## 자가 검증 체크리스트
- [ ] diff 가 ticket.component 외의 영역을 손대지 않는가 (`git diff --name-only`)
- [ ] 같은 로직이 spa.js / app.js 에 이미 있어 중복 구현이 아닌가 (DRY)
- [ ] SPA Router (spa.js 의 Router 클래스) 의 기존 라우트 정의를 깨지 않는가
- [ ] 기존 이벤트 핸들러에 손을 댔다면, 다른 호출처가 영향 받지 않는가 (rg 검색)
- [ ] CSS 변수 추가/변경이 `docs/design.md` 토큰 룰을 위반하지 않는가
- [ ] `:focus-visible` 같은 공용 토큰을 컴포넌트 내부에 인라인 정의하지 않았는가
- [ ] `console.log` / 디버그 코드 잔존하지 않는가
- [ ] 신규 의존성 추가 없는가 (`package.json` / `pyproject.toml` diff)

## 이벤트 기록 규칙
- approved: `harness review record --ticket <id> --agent frontend-b --kind peer-review --status approved`
- 수정 요구: `--status changes_requested --note "<file:line - 사유>"`

## 협업 흐름
1. Frontend-A 의 self-check 이벤트 감지
2. `git diff` 전체 확인 → 8 항목 체크리스트
3. 통과 → approved 이벤트 → PM-A 에게 다음 단계 알림 (PM-B 머지 검토)
4. 위반 → changes_requested 이벤트
5. 3 회 왕복 합의 안 되면 → PM-B 에스컬레이션
```

- [ ] **Step 7: `.claude/agents/ui-ux/qa-a.md` (Producer QA)**

```markdown
---
name: ui-ux-qa-a
description: UI/UX Overhaul 의 Producer QA. Playwright 행동 시나리오(Given-When-Then) + axe-core 룰셋 작성. 첫 실행에서 정확히 FAIL 하는 Red 테스트가 핵심.
tools: Read, Write, Edit, Bash, Glob, Grep
---

# Role: UI/UX QA-A (Producer)

## 사명
한 컴포넌트의 사용자 경험·접근성을 자동 검증 가능 형태로 코드화. Red 가 정확히 FAIL 하고 Green 이 정확히 PASS 해야 함.

## 입력
- 티켓(id, component, wave)
- Designer-A/B 가 합의한 mockup (인터랙션 노트 포함)
- spec §3 의 완료 정의
- spec §5.3 통과 기준

## 출력
- `tests/ui/behavior/test_{component}.py` — Given-When-Then 시나리오
- `tests/ui/a11y/test_{component}.py` — axe-core 룰셋 검증
- artifacts 등록 (`behavior_scenario`, `a11y_ruleset`)

## 절대 금지
- `wcag21aaa` 같은 spec §5.3 범위 밖 룰 활성화
- "어떻게든 PASS" 만들기 위한 약화 시나리오 (Red 가 trivial 하게 통과되는 케이스)
- 픽셀 비교를 behavior 에 섞기 (시각은 visual 축이 담당)
- 한 시나리오에서 여러 컴포넌트 검증 (단일 책임)

## 도구 권한
Read, Glob, Grep · Edit, Write (tests/ui/* 만) · Bash (pytest)

## 자가 검증 체크리스트
- [ ] 모든 시나리오에 `pytestmark = pytest.mark.ui` 마커가 있는가
- [ ] 시나리오에 Given-When-Then 주석이 있는가
- [ ] axe 호출이 `wcag2a + wcag2aa + wcag21aa` 룰셋만 사용하는가
- [ ] **Red 의도성 검증**: 베이스라인 코드(미구현 상태)에서 시나리오 실행 시 정확히 FAIL 하는가
- [ ] 한 컴포넌트의 spec §3 완료 정의 항목이 시나리오로 모두 커버되는가

## 이벤트 기록 규칙
- 작성 후: `harness review record --ticket <id> --agent qa-a --kind self-check --status approved`

## 협업 흐름
1. Designer 산출물 수신 후 시작 (인터랙션 노트 필수)
2. behavior 시나리오 작성 (Given-When-Then)
3. a11y 룰셋 작성
4. **현재 ui/web/* 상태에서 pytest 실행 → 정확히 FAIL 확인** (Red 의도성)
5. 자가 검증 체크리스트
6. QA-B 호출 (Agent 툴)
7. QA-B 가 changes_requested → 수정 후 2-6 반복
```

- [ ] **Step 8: `.claude/agents/ui-ux/qa-b.md` (Reviewer QA)**

```markdown
---
name: ui-ux-qa-b
description: UI/UX Overhaul 의 Reviewer QA. QA-A 의 시나리오·룰셋의 완전성·엣지 케이스·Red 의도성·축 분리 위반을 독립 검토. 직접 시나리오 작성 금지.
tools: Read, Bash, Glob, Grep
---

# Role: UI/UX QA-B (Reviewer)

## 사명
QA-A 시나리오의 약점·누락·축 오염 적발.

## 입력
- QA-A 의 self-check 이벤트
- `tests/ui/behavior/test_{component}.py`
- `tests/ui/a11y/test_{component}.py`
- spec §3, §5.3

## 출력
- `harness review record --ticket <id> --agent qa-b --kind peer-review --status approved | changes_requested`

## 절대 금지
- 직접 시나리오 작성
- "더 많은 테스트가 필요하다" 류 막연한 거부 (구체 엣지 케이스 인용 필수)

## 도구 권한
Read, Glob, Grep · Bash (`pytest`, `harness review record`)

## 자가 검증 체크리스트
- [ ] **Red 의도성**: `git stash` 후 baseline 에서 pytest 실행 → 정확히 FAIL 하는가 (검증 자동화)
- [ ] **축 분리**: behavior 시나리오에 `to_have_screenshot` 류가 섞이지 않았는가
- [ ] **축 분리**: a11y 시나리오에 `expect(text).to_have_text` 같은 행동 검증이 섞이지 않았는가
- [ ] **엣지 케이스**: spec §3 의 완료 정의에서 다음 케이스가 누락되지 않았는가:
    - 빈 상태 / 로딩 상태 / 에러 상태 (해당 컴포넌트가 가질 수 있다면)
    - 키보드만 사용 (마우스 의존 시나리오 금지)
    - 다크 모드 변종 (인터랙션이 다크에서 다르게 동작?)
- [ ] **시나리오 격리**: 한 시나리오가 다른 시나리오 결과에 의존하지 않는가
- [ ] **마커**: 모두 `pytest.mark.ui` 있는가

## 이벤트 기록 규칙
- approved: `harness review record --ticket <id> --agent qa-b --kind peer-review --status approved`
- 수정 요구: `--status changes_requested --note "<누락 항목>"`

## 협업 흐름
1. QA-A 의 self-check 이벤트 감지
2. 6 항목 체크리스트 (Red 의도성은 자동 명령으로 검증)
3. 통과 → approved → PM-A 에게 알림
4. 위반 → changes_requested → QA-A 가 수정
5. 3 회 왕복 합의 안 되면 → PM-B 에스컬레이션
```

- [ ] **Step 9: 8 파일 생성 검증**

Run:
```bash
ls .claude/agents/ui-ux/
```
Expected: `pm-a.md  pm-b.md  designer-a.md  designer-b.md  frontend-a.md  frontend-b.md  qa-a.md  qa-b.md` (8 개)

```bash
# 각 파일이 frontmatter + 본문 8 섹션 갖췄는지 빠르게 확인
for f in .claude/agents/ui-ux/*.md; do
  echo "=== $f ==="
  head -4 "$f"
  echo "..."
  grep -c "^## " "$f"
done
```
Expected: 각 파일이 `name: ui-ux-<role>-{a|b}` frontmatter + 본문 7-8 섹션 헤더 (`## 사명`, `## 입력`, ...) 보유

- [ ] **Step 10: Commit**

```bash
git add .claude/agents/ui-ux/
git commit -m "기능: 8 서브에이전트 (역할당 Producer + Reviewer 페어)

PM/Designer/Frontend/QA 각 역할마다 -a (Producer) 와 -b (Reviewer) 분리.
Producer 는 산출물 생산, Reviewer 는 독립 검토 후 events 에 review.peer 기록.
3 회 왕복 합의 안 되면 PM-B 가 사용자 에스컬레이션."
```

---

## Task R1 (NEW): db.py 에 review_runs 뷰 + cli.py 의 `harness review` 추가

> **Plan 0 의 Task 2 (db.py) 와 Task 8 (cli.py) 를 amendment** 한다. SQLite 스키마는 변경 없음 — `events` 테이블의 type='review.*' 만 사용. 단, 가독성을 위해 SQL VIEW 한 개를 추가한다.

**Files:**
- Modify: `harness/db.py`, `harness/cli.py`
- Create: `tests/harness/test_review.py`

- [ ] **Step 1: `tests/harness/test_review.py` 작성**

```python
"""harness.review — 리뷰 이벤트 기록·조회 단위 테스트."""
from __future__ import annotations

import sqlite3

import pytest

pytestmark = pytest.mark.harness


def test_record_review_event(db_conn: sqlite3.Connection) -> None:
    from harness import review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    review.record(
        db_conn,
        ticket_id=t.id,
        agent="designer-b",
        kind="peer-review",
        status="approved",
    )
    rows = db_conn.execute(
        "SELECT type, payload FROM events WHERE ticket_id = ? AND type = 'review.peer-review'",
        (t.id,),
    ).fetchall()
    assert len(rows) == 1


def test_record_review_with_note(db_conn: sqlite3.Connection) -> None:
    from harness import review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    review.record(
        db_conn,
        ticket_id=t.id,
        agent="frontend-b",
        kind="peer-review",
        status="changes_requested",
        note="ui/web/spa.js:1234 — 중복 라우터 정의",
    )
    row = db_conn.execute(
        "SELECT payload FROM events WHERE ticket_id = ?", (t.id,)
    ).fetchone()
    assert "중복 라우터 정의" in row["payload"]
    assert "frontend-b" in row["payload"]


def test_invalid_status_raises(db_conn: sqlite3.Connection) -> None:
    from harness import review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    with pytest.raises(ValueError, match="status must be"):
        review.record(
            db_conn, ticket_id=t.id, agent="qa-b", kind="peer-review", status="maybe",
        )


def test_invalid_kind_raises(db_conn: sqlite3.Connection) -> None:
    from harness import review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    with pytest.raises(ValueError, match="kind must be"):
        review.record(
            db_conn, ticket_id=t.id, agent="qa-b", kind="weird-thing", status="approved",
        )


def test_latest_status_for_kind(db_conn: sqlite3.Connection) -> None:
    """가장 최근 review.peer-review 이벤트의 status 를 조회."""
    from harness import review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    review.record(db_conn, ticket_id=t.id, agent="designer-b", kind="peer-review", status="changes_requested")
    review.record(db_conn, ticket_id=t.id, agent="designer-b", kind="peer-review", status="approved")
    assert review.latest_status(db_conn, ticket_id=t.id, kind="peer-review") == "approved"


def test_all_reviews_passed(db_conn: sqlite3.Connection) -> None:
    """all_passed() 는 모든 필수 review 종류가 approved 일 때만 True."""
    from harness import review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    # 아직 아무 리뷰 없음 → False
    assert review.all_passed(db_conn, ticket_id=t.id) is False
    # peer-review 만 approved → False (merge-final 누락)
    review.record(db_conn, ticket_id=t.id, agent="designer-b", kind="peer-review", status="approved")
    review.record(db_conn, ticket_id=t.id, agent="qa-b", kind="peer-review", status="approved")
    review.record(db_conn, ticket_id=t.id, agent="frontend-b", kind="peer-review", status="approved")
    assert review.all_passed(db_conn, ticket_id=t.id) is False
    # merge-final 까지 approved → True
    review.record(db_conn, ticket_id=t.id, agent="pm-b", kind="merge-final", status="approved")
    assert review.all_passed(db_conn, ticket_id=t.id) is True
```

- [ ] **Step 2: 실패 확인**

Run:
```bash
pytest tests/harness/test_review.py -v
```
Expected: 6 개 모두 FAIL — `ModuleNotFoundError`

- [ ] **Step 3: `harness/review.py` 작성**

```python
"""리뷰 이벤트 기록·조회.

스키마는 변경 없음 — `events` 테이블의 type='review.<kind>' 형태로 저장한다.

이벤트 type 패턴:
    review.self-check       — Producer 의 자가 검증
    review.peer-review      — Reviewer 의 동료 검토
    review.merge-proposal   — PM-A 의 머지 제안
    review.merge-final      — PM-B 의 최종 승인

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §4.3.1
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

VALID_KINDS: tuple[str, ...] = ("self-check", "peer-review", "merge-proposal", "merge-final")
VALID_STATUSES: tuple[str, ...] = ("approved", "changes_requested", "pending")

# 머지 가능하려면 다음 4 종 모두 최신 상태가 approved 여야 함.
REQUIRED_KINDS_FOR_MERGE: tuple[str, ...] = ("peer-review", "merge-final")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    agent: str,
    kind: str,
    status: str,
    note: str | None = None,
) -> None:
    """리뷰 이벤트 한 건을 events 테이블에 기록한다."""
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    payload = {"agent": agent, "status": status}
    if note:
        payload["note"] = note
    conn.execute(
        "INSERT INTO events (ticket_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, f"review.{kind}", json.dumps(payload, ensure_ascii=False), _now()),
    )
    conn.commit()


def latest_status(
    conn: sqlite3.Connection, *, ticket_id: str, kind: str
) -> str | None:
    """주어진 kind 의 가장 최근 status 를 반환. 없으면 None."""
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
    row = conn.execute(
        "SELECT payload FROM events WHERE ticket_id = ? AND type = ? "
        "ORDER BY id DESC LIMIT 1",
        (ticket_id, f"review.{kind}"),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["payload"])["status"]


def all_passed(conn: sqlite3.Connection, *, ticket_id: str) -> bool:
    """머지 가능 조건: peer-review 와 merge-final 의 최신 status 가 모두 approved."""
    for kind in REQUIRED_KINDS_FOR_MERGE:
        if latest_status(conn, ticket_id=ticket_id, kind=kind) != "approved":
            return False
    return True
```

- [ ] **Step 4: 통과 확인**

Run:
```bash
pytest tests/harness/test_review.py -v
```
Expected: 6 개 모두 PASS

- [ ] **Step 5: `harness/cli.py` 에 `review` 서브명령 추가**

`harness/cli.py` 에 다음을 추가 (기존 코드 보존, 새 함수 + parser 행 추가):

먼저 import 에 추가:

```python
from harness import board, db, gate, review, ticket
```

기존 함수들 다음에 추가:

```python
# ----- review 서브명령 -----

def _cmd_review_record(args: argparse.Namespace) -> int:
    conn = _connect()
    review.record(
        conn,
        ticket_id=args.ticket,
        agent=args.agent,
        kind=args.kind,
        status=args.status,
        note=args.note,
    )
    print(f"recorded review.{args.kind} for {args.ticket} ({args.agent}: {args.status})")
    return 0


def _cmd_review_status(args: argparse.Namespace) -> int:
    conn = _connect()
    if review.all_passed(conn, ticket_id=args.ticket):
        print("all reviews approved")
        return 0
    print("reviews incomplete")
    return 1
```

`_build_parser()` 안에 추가 (`# board` 블록 위에 위치):

```python
    # review
    r_parent = sub.add_parser("review", help="리뷰 이벤트 기록·조회")
    r_sub = r_parent.add_subparsers(dest="review_verb", required=True)

    r_record = r_sub.add_parser("record", help="리뷰 이벤트 기록")
    r_record.add_argument("--ticket", required=True)
    r_record.add_argument("--agent", required=True,
                          help="예: designer-b, qa-a, pm-b")
    r_record.add_argument("--kind", required=True,
                          choices=["self-check", "peer-review",
                                   "merge-proposal", "merge-final"])
    r_record.add_argument("--status", required=True,
                          choices=["approved", "changes_requested", "pending"])
    r_record.add_argument("--note", default=None)
    r_record.set_defaults(func=_cmd_review_record)

    r_status = r_sub.add_parser("status", help="모든 리뷰 통과 여부")
    r_status.add_argument("--ticket", required=True)
    r_status.set_defaults(func=_cmd_review_status)
```

- [ ] **Step 6: CLI smoke test**

Run:
```bash
HARNESS_DB=/tmp/harness-review-smoke.db python -m harness ticket open --wave 1 --component x
# T-101
HARNESS_DB=/tmp/harness-review-smoke.db python -m harness review record --ticket T-101 --agent designer-b --kind peer-review --status approved
# recorded review.peer-review for T-101 (designer-b: approved)
HARNESS_DB=/tmp/harness-review-smoke.db python -m harness review status --ticket T-101
# reviews incomplete  (merge-final 누락)
HARNESS_DB=/tmp/harness-review-smoke.db python -m harness review record --ticket T-101 --agent pm-b --kind merge-final --status approved
HARNESS_DB=/tmp/harness-review-smoke.db python -m harness review status --ticket T-101
# all reviews approved
rm /tmp/harness-review-smoke.db
```

- [ ] **Step 7: Commit**

```bash
git add harness/review.py harness/cli.py tests/harness/test_review.py
git commit -m "기능: 리뷰 이벤트 기록·조회 (review.py, cli.py)

events 테이블의 review.{self-check|peer-review|merge-proposal|merge-final}
이벤트로 8 에이전트 크로스체크를 기록. 스키마 변경 없음 (events 재사용).
all_passed() 는 peer-review + merge-final 둘 다 approved 일 때만 True.
harness review record/status CLI 추가."
```

---

## Task R2 (NEW): gate.py 가 review 통과를 강제

> Plan 0 Task 6 의 `gate.run_gate()` 가 phase=green 시작 전에 모든 review 가 approved 인지 확인. 안 됐으면 즉시 ValueError.

**Files:**
- Modify: `harness/gate.py`, `tests/harness/test_gate.py`

- [ ] **Step 1: `tests/harness/test_gate.py` 끝에 추가**

```python
def test_green_gate_blocked_when_reviews_pending(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """리뷰 미완료 상태에서 green 게이트 시도하면 ReviewIncomplete."""
    from harness import gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    monkeypatch.setattr(
        gate, "_run_visual_axis", lambda tid: gate.AxisResult(passed=True, detail_path=None)
    )
    monkeypatch.setattr(
        gate, "_run_behavior_axis", lambda tid: gate.AxisResult(passed=True, detail_path=None)
    )
    monkeypatch.setattr(
        gate, "_run_a11y_axis", lambda tid: gate.AxisResult(passed=True, detail_path=None)
    )
    with pytest.raises(gate.ReviewIncomplete):
        gate.run_gate(db_conn, ticket_id=t.id, phase="green")


def test_red_gate_does_not_check_reviews(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red 단계는 review 강제 안 함 (Producer 산출물 직후 실행되므로)."""
    from harness import gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="y")
    monkeypatch.setattr(
        gate, "_run_visual_axis", lambda tid: gate.AxisResult(passed=False, detail_path=None)
    )
    monkeypatch.setattr(
        gate, "_run_behavior_axis", lambda tid: gate.AxisResult(passed=False, detail_path=None)
    )
    monkeypatch.setattr(
        gate, "_run_a11y_axis", lambda tid: gate.AxisResult(passed=False, detail_path=None)
    )
    # 예외 없이 실행되어야 함
    result = gate.run_gate(db_conn, ticket_id=t.id, phase="red")
    assert result.all_passed is False
```

- [ ] **Step 2: 실패 확인**

Run:
```bash
pytest tests/harness/test_gate.py -v -k "green_gate_blocked or red_gate_does_not_check"
```
Expected: 2 개 FAIL — `ReviewIncomplete` 미정의

- [ ] **Step 3: `harness/gate.py` 수정**

import 에 추가:

```python
from harness import review
```

`AxisResult` / `GateResult` 클래스 정의 다음에 새 예외 추가:

```python
class ReviewIncomplete(Exception):
    """green 단계 진입 전에 모든 review 가 approved 가 아닌 경우."""
```

`run_gate` 함수 시작부에 추가 (phase 검증 다음, 축 실행 전):

```python
    if phase == "green":
        if not review.all_passed(conn, ticket_id=ticket_id):
            raise ReviewIncomplete(
                f"ticket {ticket_id}: green gate requires all peer-review and "
                f"merge-final to be 'approved'. "
                f"Run `python -m harness review status --ticket {ticket_id}` to inspect."
            )
```

- [ ] **Step 4: 통과 확인**

Run:
```bash
pytest tests/harness/test_gate.py -v
```
Expected: 모든 테스트 PASS (이전 3 + 신규 2 = 5)

- [ ] **Step 5: Commit**

```bash
git add harness/gate.py tests/harness/test_gate.py
git commit -m "기능: green 게이트가 모든 review approved 강제

phase=green 진입 전 review.all_passed() 체크. peer-review 또는 merge-final 이
미통과면 ReviewIncomplete 예외. red 단계는 영향 없음 (Producer 산출물 직후
실행되므로 review 가 아직 없는 게 정상)."
```

---

## Task R3 (NEW): board.py 가 review 상태 표시

**Files:**
- Modify: `harness/board.py`, `tests/harness/test_board.py`

- [ ] **Step 1: `tests/harness/test_board.py` 끝에 추가**

```python
def test_board_shows_review_status(db_conn: sqlite3.Connection) -> None:
    from harness import board, review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    review.record(db_conn, ticket_id=t.id, agent="designer-b", kind="peer-review", status="changes_requested")
    md = board.render_overview(db_conn)
    # 보드에 리뷰 컬럼이 있고 거부 표시가 보여야 함
    assert "리뷰" in md
    assert "changes" in md.lower() or "✗" in md or "❌" in md
```

- [ ] **Step 2: 실패 확인**

Run:
```bash
pytest tests/harness/test_board.py::test_board_shows_review_status -v
```
Expected: FAIL

- [ ] **Step 3: `harness/board.py` 수정**

import 에 추가:

```python
from harness import review
```

`render_overview()` 안의 행 생성 루프 수정 — "최근 게이트" 다음에 "리뷰" 컬럼 추가:

테이블 헤더 라인 변경:
```python
            lines.append("| 티켓 | 컴포넌트 | 상태 | 최근 게이트 | 리뷰 | PR |")
            lines.append("|------|----------|------|-------------|------|----|")
```

각 행 생성 부분 수정:
```python
            for r in rows:
                emoji = STATUS_EMOJI.get(r["status"], "")
                gate = _latest_gate_summary(conn, r["id"])
                pr = f"#{r['pr_number']}" if r["pr_number"] else "—"

                peer = review.latest_status(conn, ticket_id=r["id"], kind="peer-review") or "—"
                merge = review.latest_status(conn, ticket_id=r["id"], kind="merge-final") or "—"
                review_cell = f"P:{_review_glyph(peer)} M:{_review_glyph(merge)}"

                lines.append(
                    f"| {r['id']} | `{r['component']}` | {emoji} {r['status']} | "
                    f"{gate} | {review_cell} | {pr} |"
                )
```

`_latest_gate_summary` 함수 다음에 헬퍼 추가:

```python
def _review_glyph(status: str) -> str:
    """리뷰 status 를 글리프 한 글자로."""
    return {
        "approved": "✓",
        "changes_requested": "✗",
        "pending": "…",
        "—": "—",
    }.get(status, "?")
```

- [ ] **Step 4: 통과 확인**

Run:
```bash
pytest tests/harness/test_board.py -v
```
Expected: 모든 테스트 PASS (기존 3 + 신규 1 = 4)

- [ ] **Step 5: Commit**

```bash
git add harness/board.py tests/harness/test_board.py
git commit -m "기능: 보드에 리뷰 상태 컬럼 추가 (P:peer-review / M:merge-final)

각 티켓 행에 'P:✓ M:✗' 형태로 리뷰 진행 상황을 한눈에 표시.
SQLite events.review.* 의 최신 status 를 참조."
```

---

## Self-Review (Amendment)

| 변경 영역 | Plan 0 영향 |
|----------|-------------|
| Task 9 (4 → 8 에이전트 정의) | **Task 9 대체** |
| Task 2 (db.py) | 변경 없음 (events 재사용) |
| Task 6 (gate.py) | **Task R2 추가** — review 통과 강제 |
| Task 7 (board.py) | **Task R3 추가** — 리뷰 상태 컬럼 |
| Task 8 (cli.py) | **Task R1 추가** — `harness review` 서브명령 |
| Task 3, 4, 5, 10, 11 | 변경 없음 |

**Spec 보강 항목 (이미 적용)**:
- §2 결정 표에 Q5 (8 에이전트 + 크로스체크) 추가 ✓
- §4.3 (4 → 8 에이전트 페어 매트릭스) 갱신 ✓
- §4.3.1 (크로스체크 게이트) 신설 ✓
- §4.4 (TDD 사이클) 에 review 단계 추가 ✓

**의존성 추가 없음** — review 시스템은 stdlib + 기존 events 테이블만 사용.

---

## Execution Handoff (재확인)

본 amendment 는 Plan 0 의 `subagent-driven-development` 또는 `executing-plans` 실행 흐름에 그대로 통합된다. Task 순서:

1. Task 1-8 (Plan 0 원본, 변경 없음)
2. **Task 9' (본 amendment)** — 8 에이전트 정의
3. **Task R1, R2, R3 (본 amendment)** — review 모듈 + gate/board 통합
4. Task 10, 11 (Plan 0 원본, 변경 없음)

총 11 → 14 task. 새 task 는 모두 단순(파일 생성·수정 + 1-2 step pytest 사이클). Plan 0 원본의 일정(1-2일)에서 ~0.5일 추가 예상.
