# Wave 1 — Skeleton Shimmer (Plan 1.2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wave 1 항목 2 (스켈레톤 shimmer 애니메이션) — 이미 정의된 스켈레톤 시스템(style.css §11-10 + app.js `createSkeletonCards`)을 실제 로딩 위치 4곳에 연결.

**Architecture:** 메인 = PM-A. 스켈레톤 마크업/CSS 는 이미 존재(design.md §3.8 패턴 정확 구현)이므로 Designer-A 는 mockup 만 작성, Frontend-A 가 SPA 4 위치의 spinner → skeleton 교체.

**Tech Stack:** 기존 `harness/` CLI · Pillow + numpy 픽셀 diff · axe-playwright-python.

**완료 정의 (audit §1.3):**
- 회의 목록 로딩 시 `createSkeletonCards()` 실제 호출 (현재 export만 됨)
- 검색 결과 / 뷰어 transcript / 뷰어 summary 로딩 spinner → skeleton 교체
- 다크 모드에서 shimmer 색이 `--bg-secondary`/`--bg-hover` 토큰 자동 전환 (이미 구현됨, 검증)

**대상 컴포넌트 식별자:** `skeleton-shimmer` (티켓 id `T-103` 예상)

---

## 현재 상태 (이미 구현된 부분)

```
✓ style.css:2361~2395   .skeleton-card / .skeleton-line / @keyframes shimmer
                        gradient 200% + stagger delay (design.md §3.8 정확 구현)
✓ app.js:585~605        createSkeletonCards(count) 함수
✓ app.js:653            window.App.createSkeletonCards 으로 export

✗ spa.js                createSkeletonCards 실제 호출 0건
✗ spa.js:1278           검색 로딩이 spinner (skeleton 미사용)
✗ spa.js:1644           viewer transcript 로딩이 spinner
✗ spa.js:1668           viewer summary 로딩이 spinner
```

---

## File Structure

**수정 대상**:
- `ui/web/spa.js` (4 위치 — 회의 목록 로딩 호출 추가 + 3 spinner → skeleton 교체)

**필요 시 minor**:
- `ui/web/style.css` (skeleton 변종 스타일 추가 — viewer 본문용 더 긴 line, 검색 결과용 카드 변형)

**Designer-A 산출물**:
- `docs/superpowers/ui-ux-overhaul/wave-1/skeleton-shimmer-mockup.md`
- `tests/ui/visual/baselines/skeleton-shimmer-{light,dark,mobile}.png` (3 변종)

**QA-A 산출물**:
- `tests/ui/_fixtures/skeleton-shimmer-preview.html`
- `tests/ui/visual/test_skeleton_shimmer.py`
- `tests/ui/behavior/test_skeleton_shimmer.py`
- `tests/ui/a11y/test_skeleton_shimmer.py`

---

## Task 0: 브랜치 확인

```bash
git branch --show-current
```
Expected: `feature/wave-1-skeleton-shimmer` (이미 메인 세션이 만듦)

---

## Task 1: T-103 skeleton-shimmer 8 에이전트 페어 사이클

본 사이클은 Plan 1.1 / 1.3 과 동일 패턴. 12 step 순서:
1. 티켓 발급
2. Designer-A — mockup + 3 베이스라인 PNG
3. Designer-B — 토큰 일관성 + design.md §3.8 적합성 리뷰
4. QA-A — fixture + 시각/행동/a11y 시나리오
5. QA-B — Red 의도성 검증
6. Red gate
7. Frontend-A — spa.js 4 위치 마이그레이션
8. Frontend-B — 코드 리뷰
9. PM-B — merge-final 승인
10. Green gate
11. PR 생성
12. 머지 후 close

---

## Designer-A 작업 가이드

### Mockup §1 목적
이미 존재하는 `.skeleton-card` 시스템을 회의 목록·검색·뷰어 transcript·뷰어 summary 4 위치에 연결.

### Mockup §2 사용 토큰 (기존)
- `--bg-secondary` (gradient 25% + 75%)
- `--bg-hover` (gradient 50% — shimmer peak)
- `--bg-card` (skeleton-card 배경)
- `--border` (skeleton-card 보더)
- `--radius-lg` (skeleton-card 보더 반경)

### Mockup §3 변종
- **카드형** (회의 목록 / 검색 결과): 기존 `.skeleton-card` 그대로
- **라인형** (viewer 본문 / summary): `.skeleton-card` 없이 `.skeleton-line` 만 여러 줄. 더 긴 line (`width: 100%/85%/70%`)

### Mockup §4 베이스라인 캡처

QA-A 의 fixture HTML 참조해서 캡처. 라이트/다크/모바일 3 변종.

shimmer 애니메이션은 정적 PNG 캡처라 한 frame 만 캡처. animation-delay 0 시점 (background-position: -200% 0).

---

## QA-A 작업 가이드

### Fixture HTML
```html
<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <link rel="stylesheet" href="../../../ui/web/style.css">
</head>
<body>
<main role="main">
  <!-- 카드형 (회의 목록 / 검색) -->
  <section data-skeleton="card-list">
    <div class="skeleton-card" aria-hidden="true">
      <div class="skeleton-line short"></div>
      <div class="skeleton-line medium"></div>
      <div class="skeleton-line"></div>
    </div>
    <div class="skeleton-card" aria-hidden="true">
      <div class="skeleton-line short"></div>
      <div class="skeleton-line medium"></div>
      <div class="skeleton-line"></div>
    </div>
    <div class="skeleton-card" aria-hidden="true">
      <div class="skeleton-line short"></div>
      <div class="skeleton-line medium"></div>
      <div class="skeleton-line"></div>
    </div>
  </section>

  <!-- 라인형 (viewer 본문) -->
  <section data-skeleton="lines" aria-hidden="true">
    <div class="skeleton-line"></div>
    <div class="skeleton-line"></div>
    <div class="skeleton-line medium"></div>
    <div class="skeleton-line"></div>
    <div class="skeleton-line short"></div>
  </section>
</main>
<style>
  body { padding: 24px; background: var(--bg-canvas); }
  section { margin-bottom: 32px; max-width: 520px; }
  section[data-skeleton="card-list"] { display: flex; flex-direction: column; gap: 12px; }
  section[data-skeleton="lines"] { display: flex; flex-direction: column; gap: 8px; }
</style>
</body>
</html>
```

### Visual 시나리오 (3 케이스)
- light / dark / mobile

### Behavior 시나리오 (3 케이스)
1. card-list 섹션에 3 개 skeleton-card 가 보임
2. 각 skeleton-card 가 3 개 skeleton-line 보유
3. lines 섹션에 5 개 skeleton-line 보임 (다양한 width 클래스)

### A11y 시나리오 (2 케이스)
1. axe-core wcag2a + wcag2aa + wcag21aa 위반 0
2. 모든 skeleton 컨테이너에 `aria-hidden="true"` (스크린 리더 무시)

---

## Frontend-A 작업 가이드

### B-1: `ui/web/spa.js` 회의 목록 로딩 (line ~921)

회의 목록 fetch 시작 전 스켈레톤 표시 → fetch 완료 후 제거. 정확한 위치는 `_fetchAndRender` 또는 비슷한 함수 grep.

```javascript
// 로딩 시작 시
_listEl.innerHTML = "";
var skeletons = App.createSkeletonCards(4);
_listEl.appendChild(skeletons);

// fetch 완료 후 render() 가 _listEl.innerHTML = "" 으로 자동 제거
```

### B-2: `spa.js:1278` 검색 결과 spinner → skeleton

기존:
```javascript
'    <div class="loading-overlay" id="searchLoading" role="status" aria-live="polite">',
'      <span class="loading-spinner" aria-hidden="true"></span>',
'      <span class="loading-text">검색 중…</span>',
'    </div>',
```

변경:
```javascript
'    <div class="skeleton-container" id="searchLoading" role="status" aria-live="polite" style="display:none;">',
'      <span class="sr-only">검색 중…</span>',
'      <div class="skeleton-card" aria-hidden="true"><div class="skeleton-line short"></div><div class="skeleton-line medium"></div><div class="skeleton-line"></div></div>',
'      <div class="skeleton-card" aria-hidden="true"><div class="skeleton-line short"></div><div class="skeleton-line medium"></div><div class="skeleton-line"></div></div>',
'      <div class="skeleton-card" aria-hidden="true"><div class="skeleton-line short"></div><div class="skeleton-line medium"></div><div class="skeleton-line"></div></div>',
'    </div>',
```

`role="status" + aria-live="polite"` 보존 + `sr-only` 텍스트로 스크린 리더 안내.

### B-3, B-4: viewer transcript / summary 로딩 (line 1644 / 1668)

같은 패턴 — spinner → 라인형 skeleton.

### `ui/web/style.css` 추가 (필요 시)

```css
/* 스켈레톤 컨테이너 (loading-overlay 대체) */
.skeleton-container {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
}

/* sr-only — 스크린 리더 전용 */
.sr-only:not(:focus):not(:active) {
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  height: 1px;
  overflow: hidden;
  position: absolute;
  white-space: nowrap;
  width: 1px;
}
```

> **주의**: `.sr-only` 가 이미 정의되어 있을 수 있음. `grep "^\.sr-only" ui/web/style.css` 로 확인 후 중복 회피.

---

## Self-Review (Plan 작성자)

### Spec coverage
- [x] §3 Wave 1 항목 2 스켈레톤 shimmer 애니메이션 ✓
- [x] design.md §3.8 패턴 (이미 구현됨) 4 위치 적용 ✓

### Placeholder 스캔
- 모든 변경 위치의 file:line 명시
- 마크업 코드 풀 inline (TBD 없음)

### Type 일관성
- `T-103` 예상 (이전 t101=closed, t102=closed)
- review.py kind/status enum 일관

---

## Execution Handoff

Inline 실행 (메인 세션 = PM-A) 자연스러움.
