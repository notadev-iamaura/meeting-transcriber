# UI/UX Overhaul 디자인 스펙

> **작성일**: 2026-04-28
> **범위**: meeting-transcriber 웹 UI(SPA) 의 디자인·UX·접근성 미흡 7개 항목 완전 개선
> **베이스**: `docs/SYSTEM_AUDIT_2026-04-28.md` 의 §1 UI/UX 미흡 항목
> **상태**: 디자인 단계 — 사용자 리뷰 후 `writing-plans` 로 진행

---

## 1. 목표 / 비목표

### 1.1 목표
- `docs/SYSTEM_AUDIT_2026-04-28.md` §1 의 UI/UX 미흡 7개 항목 완전 해소
- 본 작업 종료 후 다른 영역 작업에도 그대로 재사용 가능한 **풀스택 하네스 시스템** 구축
- 시각·행동·접근성 3축이 동시에 게이트 통과한 변경만 머지

### 1.2 비목표 (이번 작업 범위 밖)
- 백엔드 API 변경 (예: 파일 업로드 엔드포인트 신설은 별도 작업)
- 미구현 기능 추가 (Archive, 라이프사이클 자동화 등은 별도 작업)
- 디자인 언어 자체의 전면 개편 (`docs/design.md` 의 토큰을 보강할 뿐 새 언어 도입 없음)
- 신규 런타임 의존성 추가 — 추가 허용 항목은 `axe-playwright-python` (테스트 전용) 단 하나로 한정

---

## 2. 핵심 결정사항 (브레인스토밍 결과)

| # | 결정 | 선택 |
|---|------|------|
| Q1 | 하네스 시스템 형태 | **풀스택 하네스** — Python CLI + SQLite 영속 + QA 자동화 |
| Q2 | 작업 묶음 단위 | **3-Wave** — Visual Polish → Interaction & Focus → Accessibility & Mobile |
| Q3 | TDD 첫 테스트 형태 | **Hybrid 3축 Red** — 행동 + 시각 + 접근성 동시 FAIL 후 통과 |
| Q4 | 오케스트레이터 형태 | **Hybrid Conductor** — 메인 Claude 세션이 PM + Python CLI 헬퍼 + SQLite |

---

## 3. 작업 범위 — 7개 UI/UX 항목

`docs/SYSTEM_AUDIT_2026-04-28.md` §1 에 명시된 항목을 그대로 가져옵니다.

### Wave 1 · Visual Polish (3개)
| 항목 | 위치 | 완료 정의 |
|------|------|----------|
| 1. 빈 상태(Empty State) 패턴 | `ui/web/spa.js:950~952` | `design.md §3.7` 기준(48px 아이콘 + 제목 + 설명 + CTA) 적용 — 회의 0개일 때 / 검색 결과 0개일 때 / 채팅 빈 상태 모두 |
| 2. 스켈레톤 shimmer 애니메이션 | `ui/web/app.js:585~605` + `style.css` | `@keyframes shimmer` 정의, 회의 목록·뷰어 본문·검색 결과 로딩 시 적용 |
| 3. 다크모드 톤 격차 | `ui/web/style.css:149~230` | `design.md §1.1` 단계별 배경(`#1C1C1E/#2C2C2E/#3A3A3C`) 반영, 라이트도 톤 단계 재정렬 |

### Wave 2 · Interaction & Focus (2개)
| 항목 | 위치 | 완료 정의 |
|------|------|----------|
| 4. Command Palette (⌘K) | `ui/web/spa.js:7616~8194` | ⌘K 단축키 바인딩, 회의 검색·뷰 전환·작업 명령 통합, ESC 닫기, 화살표 ↑↓ 탐색, Enter 실행. **기존 검색 API 만 사용, 신규 백엔드 변경 없음** |
| 5. `:focus-visible` 일관성 | `ui/web/style.css:515` | 모든 인터랙티브 요소(`button`, `a`, `input`, `[role="button"]`)에 통일된 포커스 링 토큰 |

### Wave 3 · Accessibility & Mobile (2개)
| 항목 | 위치 | 완료 정의 |
|------|------|----------|
| 6. ARIA 동기화 | `ui/web/index.html:90`, `spa.js` 선택 핸들러 | `aria-selected` / `aria-current` / `aria-expanded` 동적 업데이트, `role` 의미 정합성 |
| 7. 모바일 반응형 진입로 | `ui/web/style.css:3717~3758` | 768px 이하에서 햄버거 → 사이드바 시트(드로어), 컨텐츠 영역 패딩 정리 |

---

## 4. 시스템 아키텍처

### 4.1 디렉토리 구조

```
.claude/agents/ui-ux/        # 4 서브에이전트 정의 (Markdown frontmatter)
  ├─ pm.md                   # PM (티켓 발급·게이트 결과 검토)
  ├─ designer.md             # Designer (목업·시각 토큰·스냅샷 베이스라인)
  ├─ frontend.md             # Frontend (구현·리팩터)
  └─ qa.md                   # QA (행동 시나리오·접근성 룰셋)

scripts/harness/             # Python CLI 하네스
  ├─ __main__.py             # `python -m harness` 진입
  ├─ cli.py                  # argparse 라우팅
  ├─ db.py                   # SQLite 스키마·마이그레이션·쿼리
  ├─ ticket.py               # 티켓 모델·상태 전이
  ├─ gate.py                 # QA 게이트 오케스트레이터
  ├─ snapshot.py             # Playwright 시각 회귀 베이스라인 관리
  ├─ a11y.py                 # axe-core 통합
  ├─ behavior.py             # Playwright 행동 시나리오 통합
  └─ board.py                # docs 마크다운 진행 보드 자동 생성

state/
  └─ harness.db              # SQLite — 티켓·산출물 레퍼런스·게이트 결과·이벤트

tests/ui/                    # UI 테스트 디렉토리 (신설)
  ├─ conftest.py             # Playwright fixture·테스트 서버 기동
  ├─ visual/
  │   ├─ baselines/          # PNG 스냅샷 (라이트/다크/모바일 변종)
  │   └─ test_*.py
  ├─ behavior/
  │   └─ test_*.py           # Given-When-Then 시나리오
  └─ a11y/
      └─ test_*.py           # axe-core 룰셋 통과 검증

docs/superpowers/ui-ux-overhaul/   # 자동 생성 진행 보드
  ├─ 00-overview.md          # 전체 진행 상황 (board.py 가 SQLite 에서 생성)
  ├─ wave-1/                 # Wave 1 산출물 (목업·시나리오·게이트 결과)
  ├─ wave-2/
  └─ wave-3/

docs/superpowers/specs/
  └─ 2026-04-28-ui-ux-overhaul-design.md   # 본 문서
```

### 4.2 데이터 모델 (SQLite)

```sql
-- 티켓: 한 컴포넌트 단위의 작업 단위 (예: empty-state, command-palette)
CREATE TABLE tickets (
    id              TEXT PRIMARY KEY,           -- 'T-001'
    wave            INTEGER NOT NULL,           -- 1·2·3
    component       TEXT NOT NULL,              -- 'empty-state'
    status          TEXT NOT NULL,              -- pending|design|red|green|refactor|merged|closed
    pr_number       INTEGER,
    created_at      TEXT NOT NULL,              -- ISO-8601
    updated_at      TEXT NOT NULL
);

-- 산출물 레퍼런스 (실제 파일은 docs/superpowers/ui-ux-overhaul/ 또는 tests/ui/)
CREATE TABLE artifacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       TEXT NOT NULL REFERENCES tickets(id),
    kind            TEXT NOT NULL,              -- mockup|visual_baseline|behavior_scenario|a11y_ruleset|implementation
    path            TEXT NOT NULL,              -- 상대 경로
    sha256          TEXT,                       -- 변경 추적
    author_agent    TEXT NOT NULL,              -- pm|designer|frontend|qa
    created_at      TEXT NOT NULL
);

-- 게이트 결과 (Red/Green 의 매 사이클 기록)
CREATE TABLE gate_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       TEXT NOT NULL REFERENCES tickets(id),
    phase           TEXT NOT NULL,              -- red|green
    visual_pass     INTEGER NOT NULL,           -- 0|1
    behavior_pass   INTEGER NOT NULL,
    a11y_pass       INTEGER NOT NULL,
    visual_diff     TEXT,                       -- 실패 시 diff 이미지 경로
    behavior_log    TEXT,                       -- 실패 시 로그 경로
    a11y_violations TEXT,                       -- JSON
    created_at      TEXT NOT NULL
);

-- 이벤트 (감사 로그·자동 보드 생성용)
CREATE TABLE events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       TEXT REFERENCES tickets(id),
    type            TEXT NOT NULL,              -- ticket.opened|artifact.added|gate.run|status.changed|merged
    payload         TEXT,                       -- JSON
    created_at      TEXT NOT NULL
);
```

### 4.3 4 서브에이전트 — 책임·인터페이스

| 에이전트 | 입력 | 출력 | 도구 |
|---------|------|------|------|
| **PM** (메인 세션) | 본 spec, 우선순위 | 티켓 발급 (`harness ticket open`), 게이트 결과 검토, 머지 결정 | `Agent` 툴로 Designer/Frontend/QA 디스패치 |
| **Designer** | 티켓(componentId, design.md 참조) | 마크다운 목업(`docs/superpowers/ui-ux-overhaul/wave-N/{component}-mockup.md`) + Playwright 시각 베이스라인 PNG | Read, Write, Bash(Playwright snapshot) |
| **Frontend** | 티켓 + Designer 산출물 + QA 산출물 | 구현(`ui/web/*` 변경) | Read, Edit, Write, Bash(테스트 실행) |
| **QA** | 티켓 + Designer 목업 | Playwright 행동 시나리오(`tests/ui/behavior/`) + axe-core 룰셋(`tests/ui/a11y/`) | Read, Write, Bash(테스트 실행) |

### 4.4 TDD 사이클 — Hybrid 3축 Red

```
[PM]                    티켓 발급 (harness ticket open)
   │
   ├─ [Designer]        목업 + 시각 베이스라인 PNG (라이트/다크/모바일)
   │       │
   │       └─ artifact: docs/.../wave-N/{c}-mockup.md, tests/ui/visual/baselines/{c}-{variant}.png
   │
   ├─ [QA]              Playwright 행동 시나리오 + axe-core 룰셋
   │       │
   │       └─ artifact: tests/ui/behavior/test_{c}.py, tests/ui/a11y/test_{c}.py
   │
   ▼
[harness gate run --phase red]
   ┌─ visual    ✗ (베이스라인 vs 현재 = 다름)
   ├─ behavior  ✗ (시나리오 미통과)
   └─ a11y      ✗ (axe 위반)
   → DB.gate_runs INSERT (red)
   → board.py 가 docs/.../00-overview.md 갱신
   │
   ▼
[Frontend]              3개 모두 통과시키는 최소 구현
   │
   ▼
[harness gate run --phase green]
   ┌─ visual    ✓
   ├─ behavior  ✓
   └─ a11y      ✓
   → DB.gate_runs INSERT (green)
   │
   ▼
[Refactor 합동 리뷰]    PM/Designer/QA — 토큰 일관성·회귀 누락·코드 품질
   │
   ▼
[PM] harness ticket close --pr <N>
   → status: merged → closed
```

**PR 단위 규칙**: PR = 티켓 단위 (한 컴포넌트 = 한 PR). Wave 안의 모든 티켓이 closed 되면 `harness board rebuild` 가 자동으로 Wave 완료 보드 스냅샷을 생성하고 별도 docs 커밋으로 영속화.

### 4.5 CLI 명령 (계약)

```bash
# 티켓 라이프사이클
harness ticket open    --wave 1 --component empty-state
harness ticket list    [--wave N] [--status STATUS]
harness ticket show    <ticket-id>
harness ticket close   <ticket-id> --pr <pr-number>

# 산출물 등록
harness artifact add   <ticket-id> --kind mockup --path docs/.../empty-state-mockup.md --agent designer

# 게이트 실행
harness gate run       <ticket-id> --phase red|green
   # 내부: pytest tests/ui/visual/test_<c>.py + behavior + a11y 실행, 결과 DB 기록

# 시각 베이스라인 관리
harness snapshot baseline --component empty-state --variant light|dark|mobile
   # Playwright 가 페이지 렌더 → PNG 저장 → tests/ui/visual/baselines/

harness snapshot verify   <ticket-id>
   # 현재 vs 베이스라인 비교, diff 이미지 생성

# 마크다운 보드 재생성
harness board rebuild
   # docs/superpowers/ui-ux-overhaul/00-overview.md 를 SQLite 에서 재생성
```

---

## 5. 테스트 전략

### 5.1 도구 스택
- **Playwright (Python)** — `pip install -e ".[dev]"` 에 이미 포함. 시각 회귀(`expect(page).to_have_screenshot()`) 와 행동 시나리오 모두 담당.
- **axe-core** — `axe-playwright-python` 라이브러리로 통합.
- **pytest** — 기존 사용 중. `tests/ui/` 는 별도 마커(`@pytest.mark.ui`) 로 격리.

### 5.2 테스트 트리거
- **로컬**: `harness gate run` 호출 시
- **PR**: GitHub Actions 에서 `pytest -m ui` 자동 실행 (별도 워크플로우 추가)
- **베이스라인 업데이트**: `harness snapshot baseline` 명시적 호출 시에만 (자동 갱신 금지)

### 5.3 통과 기준
| 게이트 | 기준 |
|--------|------|
| Visual | 픽셀 diff < 0.1% (Playwright `maxDiffPixelRatio`), 라이트·다크·모바일 3 변종 모두 |
| Behavior | Given-When-Then 시나리오 100% 통과 |
| Accessibility | axe-core `wcag2a + wcag2aa + wcag21aa` 룰셋 위반 0개 |

---

## 6. 에러 처리·복구

### 6.1 게이트 실패 시
- DB.gate_runs 에 실패 사유 + 산출물(diff PNG / 로그 / 위반 JSON) 경로 기록
- 보드 자동 갱신 (실패 카드로 표시)
- PM(메인 세션)이 사용자에게 보고 → 사용자 결정으로 다음 액션:
  1. Designer 가 베이스라인 재작성 (의도된 변경)
  2. Frontend 가 추가 구현
  3. QA 가 시나리오 보정

### 6.2 하네스 자체 실패 시
- SQLite 마이그레이션 실패 → DB 재생성 명령(`harness db reset`) 제공
- Playwright 환경 문제 → `scripts/harness/doctor.py` 로 진단

### 6.3 세션 중단 → 재개
- SQLite 가 영속이므로 새 세션에서 `harness ticket list --status in-progress` 로 즉시 복원
- Wave 단위 PR 머지 시점에 보드 스냅샷 git 커밋

---

## 7. 마이그레이션·기존 코드 정리

본 작업은 신규 디렉토리(`scripts/harness/`, `tests/ui/`, `docs/superpowers/ui-ux-overhaul/`, `.claude/agents/ui-ux/`)를 만들고, 기존 `ui/web/*` 만 변경합니다. 다음 부수 정리도 포함합니다 (audit 에서 식별된 작업 영역 내 항목):

- `ui/web/style.css` 의 다크모드 색상 변수 재정렬 (Wave 1)
- `ui/web/spa.js:7616~8194` 의 미통합 Command Palette 모듈 활성화 (Wave 2)
- `ui/web/app.js:585~605` 의 스켈레톤 마크업 + CSS shimmer 짝맞춤 (Wave 1)

---

## 8. 일정 (참고)

| 단계 | 산출물 | 예상 |
|------|--------|------|
| 하네스 셋업 | `scripts/harness/`, SQLite 스키마, 4 에이전트 정의, 첫 게이트 통과 데모 | 1-2일 |
| Wave 1 (Visual Polish, 3 티켓) | 빈 상태 / 스켈레톤 / 다크모드 토큰 | 2-3일 |
| Wave 2 (Interaction & Focus, 2 티켓) | Command Palette / focus-visible | 2일 |
| Wave 3 (Accessibility & Mobile, 2 티켓) | ARIA / 모바일 반응형 | 2일 |
| **합계** | 7-9일 |

---

## 9. 위험·완화

| 위험 | 완화 |
|------|------|
| 시각 회귀 베이스라인 폭주 (PNG 다수) | Wave 별 PR 머지 시점에만 스냅샷 추가, Git LFS 사용은 유보 (PNG 작음) |
| Playwright 환경 차이 (CI vs 로컬) | Docker 이미지 또는 `playwright install` 버전 고정 |
| Command Palette 가 SPA 라우터와 충돌 | 첫 티켓에서 라우터 통합 테스트 작성 후 진행 |
| axe-core 룰 너무 엄격 → 기존 페이지 다 fail | 룰셋을 `wcag2a + wcag2aa + wcag21aa` 로 한정, `wcag21aaa` 는 옵션 |

---

## 10. 다음 단계

1. **사용자 리뷰** — 본 문서 검토, 변경 요청 수렴
2. **승인 후** — `superpowers:writing-plans` 스킬 invoke 하여 상세 구현 계획 작성
3. **실행 단계** — 하네스 셋업 → Wave 1 → Wave 2 → Wave 3

---

*본 spec 은 브레인스토밍 단계의 산출물입니다. 구현 계획·태스크 분해는 `writing-plans` 단계에서 별도 문서로 작성됩니다.*
