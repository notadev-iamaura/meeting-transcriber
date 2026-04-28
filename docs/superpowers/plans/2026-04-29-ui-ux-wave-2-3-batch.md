# Wave 2-3 Batch — 4 컴포넌트 통합 Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. 8 에이전트 페어 사이클 4 회 가동.

**Goal:** Wave 2 (Interaction & Focus) + Wave 3 (Accessibility & Mobile) 4 컴포넌트를 한 브랜치 `feature/wave-2-3-batch` 에서 순차 사이클로 처리.

**Architecture:** Plan 1.1/1.2/1.3 와 동일 패턴. fixture-as-source-of-truth + 8 에이전트 페어. 한 PR (4 컴포넌트).

**Tech Stack:** 기존 harness CLI · Pillow + numpy · axe-playwright-python · Playwright sync.

---

## 4 컴포넌트 (순차)

### T-104: `:focus-visible` 일관성 (Wave 2 항목 5)

**Spec §3 항목 5**: 모든 인터랙티브 요소(`button`, `a`, `input`, `[role="button"]`)에 통일된 포커스 링 토큰.

**작업**:
- `docs/design.md` `--focus-ring` 토큰 추가 (기존 `--accent` 위에 specific shadow)
- `ui/web/style.css` 에 공용 `:focus-visible` 정의 + 컴포넌트별 인라인 제거
- 기존 inline focus shadow 제거 (`grep "rgba(0, 122, 255, 0.15)"` 결과)

**Designer-A**: mockup §1-§5 + 3 베이스라인 (focus 적용 페이지)
**QA-A**: fixture HTML (인터랙티브 요소 6+ 개) + visual 3 + behavior 3 (Tab/Shift+Tab/Enter) + a11y 2 (focus-visible 위반 0)
**Frontend-A**: design.md 토큰 + style.css 공용 정의 + 인라인 제거

### T-105: ARIA 동기화 (Wave 3 항목 6)

**Spec §3 항목 6**: `aria-selected` / `aria-current` / `aria-expanded` 동적 업데이트, `role` 의미 정합성. `ui/web/index.html:90`, `spa.js` 선택 핸들러.

**작업**:
- `index.html` 의 `role="listbox"` 또는 `role="list"` 의미 재검토
- `spa.js` 의 select 핸들러 (`_activeId` 변경 시) 에 `aria-selected="true"` 동적 추가
- 사이드바 회의 항목 = `role="option"` (이미 적용된 듯) → `aria-selected` 만 보강
- 네비 바 / 라우터 활성 상태에 `aria-current="page"` 적용

**Designer-A**: mockup (ARIA 의미 매핑 표) + 3 베이스라인 (활성 상태 시각 동일)
**QA-A**: fixture (선택 토글 가능) + behavior 4 (선택 → aria-selected 갱신) + a11y 2 (axe role 매핑 일관성)
**Frontend-A**: spa.js select 핸들러 + index.html role 보정

### T-106: 모바일 반응형 진입로 (Wave 3 항목 7)

**Spec §3 항목 7**: 768px 이하에서 햄버거 → 사이드바 시트(드로어), 컨텐츠 영역 패딩 정리.

**작업**:
- `style.css:3717~3758` 의 768px 반응형 규칙 보강
- `spa.js` 또는 `app.js` 에 햄버거 버튼 + drawer 토글 추가
- 사이드바가 `transform: translateX(-100%)` 로 숨겨지고 햄버거 클릭 시 슬라이드인

**Designer-A**: mockup (햄버거 버튼 + drawer 패턴) + 3 베이스라인 (mobile 상태 — 닫힘/열림)
**QA-A**: fixture (햄버거 동작) + behavior 4 (클릭/ESC/외부 클릭/swipe) + a11y 2 (drawer aria-modal)
**Frontend-A**: 햄버거 버튼 + drawer 토글 + 768px 미디어쿼리

### T-107: Command Palette (⌘K) (Wave 2 항목 4)

**Spec §3 항목 4**: ⌘K 단축키 바인딩, 회의 검색·뷰 전환·작업 명령 통합, ESC 닫기, 화살표 ↑↓ 탐색, Enter 실행. **기존 검색 API 만 사용**, 신규 백엔드 변경 없음.

**작업**:
- `spa.js:7616~8194` 의 미통합 Command Palette 모듈 → 메인 SPA 라우터에 통합
- ⌘K 글로벌 단축키 바인딩 (`document.addEventListener('keydown')`)
- ESC 닫기, ↑↓ 화살표 탐색, Enter 실행
- 검색 API: 기존 `/api/search` 또는 `/api/meetings` 활용 (신규 백엔드 0)

**Designer-A**: mockup (팔레트 UI 디자인) + 3 베이스라인 (열림 상태)
**QA-A**: fixture (정적 팔레트) + behavior 6 (열기/닫기/탐색/실행) + a11y 3 (focus trap, role=combobox/listbox, aria-expanded)
**Frontend-A**: spa.js 의 미통합 모듈 활성화 + 단축키 + 라우터 통합

---

## 사이클 패턴 (각 컴포넌트 동일, Plan 1.1/1.2/1.3 와 같음)

12 step:
1. `python -m harness ticket open --wave N --component <component>`
2. Designer-A 디스패치 (mockup + 3 베이스라인)
3. Designer-B 디스패치 (peer-review)
4. QA-A 디스패치 (fixture + visual/behavior/a11y 시나리오)
5. QA-B 디스패치 (peer-review)
6. Designer + QA 산출물 commit
7. `harness gate run --phase red`
8. Frontend-A 디스패치 (spa.js / style.css / 등)
9. Frontend-B 디스패치 (peer-review)
10. Frontend-A commit + self-check
11. PM-B 디스패치 (merge-final)
12. `harness gate run --phase green` → close 는 PR 머지 후

## 한 PR 로 묶음

본 batch 끝나면 한 PR 생성 (4 컴포넌트 모두). PR 머지 후:
```bash
python -m harness ticket close T-104 --pr <N>
python -m harness ticket close T-105 --pr <N>
python -m harness ticket close T-106 --pr <N>
python -m harness ticket close T-107 --pr <N>
python -m harness board rebuild
```

## Self-Review

- Spec §3 Wave 2 / Wave 3 4 항목 모두 커버
- 각 컴포넌트의 fixture 패턴 (Plan 1.x 반복) — 안전
- 신규 의존성 0 (기존 axe-playwright-python + Pillow 만)

## Execution Handoff

본 batch 는 메인 세션이 PM-A 로 4 사이클 순차 운영.
