# Skeleton Shimmer 컴포넌트 시각 정의 (T-103)

> **Wave**: 1 · **Component**: skeleton-shimmer · **Author**: ui-ux-designer-a
> **Reference**: `docs/design.md` §3.8 (스켈레톤 로딩), §2.2 (컬러 토큰), §2.4 (Radius)
> **Spec**: `docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md` §3 Wave 1 항목 2
> **Plan**: `docs/superpowers/plans/2026-04-28-ui-ux-wave-1-skeleton-shimmer.md`
> **베이스라인 PNG**: `tests/ui/visual/baselines/skeleton-shimmer-{light|dark|mobile}.png`

---

## 1. 목적

이미 정의된 `.skeleton-card` / `.skeleton-line` / `@keyframes shimmer` 시스템(`ui/web/style.css` §11-10, line 2358~2395 + `ui/web/app.js` line 585~605 의 `createSkeletonCards()`)을 SPA 의 **로딩 spinner 가 노출되는 4 위치**에 실제로 연결한다. 본 컴포넌트의 마크업·CSS 자체는 이미 design.md §3.8 의 패턴(gradient 200% + shimmer 1.5s + stagger delay)을 1:1 로 구현하고 있으므로, Designer-A 의 산출물은 (1) 4 위치별 변종 정의, (2) 베이스라인 PNG 3 변종, (3) 토큰·인터랙션 검증이다. Frontend-A 가 `ui/web/spa.js` 4 곳의 로딩 마크업을 본 mockup 의 변종으로 교체할 때 따를 시각 계약이다.

**적용 4 위치**
| # | 위치 | spa.js 라인 | 변종 |
|---|------|------------|------|
| 1 | 회의 목록 (HomeView 초기 로딩) | ~921 (`_fetchAndRender`) | 카드형 × 4 |
| 2 | 검색 결과 로딩 | 1278 | 카드형 × 3 |
| 3 | 뷰어 transcript 로딩 | 1644 | 라인형 × 5~8 |
| 4 | 뷰어 summary 로딩 | 1668 | 라인형 × 4~6 |

---

## 2. 사용 토큰

`docs/design.md` 에 정의된 토큰만 사용한다. **새 토큰 도입 없음** — 본 컴포넌트는 기존 §11-10 구현을 그대로 활용한다.

| 영역 | 토큰 | 라이트 값 | 다크 값 | 출처 |
|------|------|----------|--------|------|
| 카드 배경 | `--bg-card` | `#FFFFFF` | `#1C1C1E` | §2.2 |
| 카드 보더 (0.5px hairline) | `--border` | `#D1D1D6` | `#38383A` | §2.2 |
| 카드 보더 반경 | `--radius-lg` | 10px | — | §2.4 |
| Shimmer gradient base (25% / 75%) | `--bg-secondary` | `#F5F5F7` | `#2C2C2E` | §2.2 |
| Shimmer gradient peak (50%) | `--bg-hover` | `rgba(0,0,0,0.04)` | `rgba(255,255,255,0.06)` | §2.2 |
| 카드 내 padding | 16px (직접값) | — | — | §11-10 (기존 구현) |
| 카드 내 line gap | 10px (직접값) | — | — | §11-10 (기존 구현) |
| 라인 height | 12px (직접값) | — | — | §11-10 (기존 구현) |
| 라인 border-radius | 6px (직접값) | — | — | §11-10 (기존 구현) |

> 🔍 **검증**: 위 토큰 모두 `ui/web/style.css` 에 이미 선언돼 있음. `--bg-secondary` / `--bg-hover` 는 다크 모드에서 `@media (prefers-color-scheme: dark)` 로 자동 전환 (§1.1 Independent Dark Mode 준수).

### 타이포그래피

스켈레톤은 텍스트를 가지지 않으므로 font-* 토큰 사용 없음. `aria-hidden="true"` 로 스크린 리더가 무시하므로 sr-only 텍스트는 **컨테이너 측**(spinner 를 대체할 외부 wrapper)에서 `role="status"` + `aria-live="polite"` + `<span class="sr-only">로딩 중…</span>` 으로 제공한다 (Frontend-A 의 §B-2~B-4 마이그레이션 가이드 참조).

---

## 3. 변종

본 티켓의 컴포넌트 마크업·CSS 정의는 이미 존재한다. 본 절은 4 위치별 **사용 패턴 차이**를 정의한다.

### 3.1 카드형 (회의 목록 / 검색 결과)

**구조**: `.skeleton-card` × N 개 (목록 4 / 검색 3) 를 세로로 나열. 각 카드는 `.skeleton-line.short` (40% 너비) → `.skeleton-line.medium` (70%) → `.skeleton-line` (100%) 3 개 라인을 포함.

```html
<div class="skeleton-card" aria-hidden="true">
  <div class="skeleton-line short"></div>   <!-- 제목 자리 -->
  <div class="skeleton-line medium"></div>  <!-- 부제·메타 자리 -->
  <div class="skeleton-line"></div>         <!-- 본문 한 줄 자리 -->
</div>
```

**의미 매핑**
- `short` (40%) → 회의 제목 / 검색 결과 제목
- `medium` (70%) → 타임스탬프 + 화자 수 / 검색 발화자·시간
- `full` (100%) → 본문 미리보기 한 줄 / 검색 매칭 텍스트 한 줄

**stagger delay**: 2~4 번째 카드의 `.skeleton-line` 에 0.1s / 0.2s / 0.3s 애니메이션 지연. 자연스럽게 위에서 아래로 흐르는 인상을 준다 (§11-10 line 2387~2390 이미 정의).

**컨테이너**: 검색은 외부 wrapper 에 `role="status" aria-live="polite"` + `<span class="sr-only">검색 중…</span>` 추가 (Frontend-A B-2). 회의 목록은 SPA 의 `_listEl` 안에 직접 fragment 삽입.

### 3.2 라인형 (뷰어 transcript / summary)

**구조**: `.skeleton-card` 래퍼 **없이** `.skeleton-line` 만 다양한 너비 클래스로 5~8 줄 나열. 본문 단락 모양을 모방.

```html
<section data-skeleton="lines" aria-hidden="true">
  <div class="skeleton-line"></div>          <!-- 100% -->
  <div class="skeleton-line"></div>          <!-- 100% -->
  <div class="skeleton-line medium"></div>   <!-- 70% -->
  <div class="skeleton-line"></div>          <!-- 100% -->
  <div class="skeleton-line short"></div>    <!-- 40% (단락 끝) -->
</section>
```

**의미 매핑**
- transcript: 5~8 줄 — 화자 발화 한 묶음 모방
- summary: 4~6 줄 — 요약 단락 모방
- `short` 줄을 마지막에 배치하여 자연스러운 단락 끝맺음 표현

**컨테이너 스타일** (Frontend-A 가 `style.css` 에 추가 예정 — 기존 §11-10 정의 변경 없이 신규 클래스만 추가):

```css
/* skeleton-container — spinner 대체 wrapper */
.skeleton-container {
  display: flex;
  flex-direction: column;
  gap: 8px;        /* 라인형 */
  padding: 16px;
}
.skeleton-container.cards {
  gap: 12px;       /* 카드형 */
}
```

> ⚠️ 본 컨테이너 클래스는 Frontend-A 영역. Designer-A 는 fixture 에 인라인 `<style>` 로만 시각 보장.

### 3.3 변종 → 4 위치 매핑

| 위치 | 변종 | 카운트 | role/aria | sr-only 텍스트 |
|------|------|--------|----------|---------------|
| 회의 목록 | 카드형 | 4 | 컨테이너에 status 필요 없음 (홈뷰 자체가 status) | — |
| 검색 결과 | 카드형 | 3 | `role="status" aria-live="polite"` | "검색 중…" |
| 뷰어 transcript | 라인형 | 5~8 | `role="status" aria-live="polite"` | "전사 불러오는 중…" |
| 뷰어 summary | 라인형 | 4~6 | `role="status" aria-live="polite"` | "요약 불러오는 중…" |

> 모든 변종에서 `.skeleton-card` / `.skeleton-line` 에는 `aria-hidden="true"` 유지. 외부 status wrapper 의 sr-only 텍스트만 스크린 리더에 전달된다.

---

## 4. design.md §3.8 적합성 검증

§3.8 은 4 가지 핵심 요소를 명시한다. 기존 §11-10 구현이 모두 1:1 일치하는지 픽셀·코드 단위로 확인.

| 요소 | design.md §3.8 사양 | style.css §11-10 구현 (line) | 일치 |
|------|---------------------|------------------------------|------|
| **gradient 컬러 stops** | `var(--bg-secondary) 25%` → `var(--bg-hover) 50%` → `var(--bg-secondary) 75%` (§3.8 line 332~336) | `linear-gradient(90deg, var(--bg-secondary) 25%, var(--bg-hover) 50%, var(--bg-secondary) 75%)` (line 2373~2378) | ✅ |
| **gradient size 200%** | `background-size: 200% 100%` (§3.8 line 336) | `background-size: 200% 100%` (line 2379) | ✅ |
| **shimmer animation** | `animation: shimmer 1.5s ease-in-out infinite` (§3.8 line 337) | `animation: shimmer 1.5s ease-in-out infinite` (line 2381) + `@keyframes shimmer { 0% { background-position: -200% 0 } 100% { background-position: 200% 0 } }` (line 2392~2395) | ✅ |
| **stagger delay** | `:nth-child(2) { animation-delay: 0.1s }`, `:nth-child(3) { 0.2s }` (§3.8 line 340~341) | `.skeleton-card:nth-child(2) .skeleton-line { animation-delay: 0.1s }` 등 2/3/4번 카드까지 0.1/0.2/0.3s (line 2388~2390) | ✅ (확장: 4번째 카드까지 정의) |

**결론**: 기존 구현이 design.md §3.8 의 모든 요소를 정확히 따른다. **본 티켓에서 §11-10 정의를 변경하지 않는다** — Designer-A / Frontend-A 모두 정의 외 영역만 작업.

---

## 5. 인터랙션 노트

### 5.1 애니메이션 사양
- **duration**: 1.5s
- **easing**: ease-in-out (CSS 키워드)
- **iteration**: infinite
- **direction**: normal (기본) — `background-position: -200% 0` → `200% 0` 좌→우
- **stagger**: 카드형에서 2~4 번째 카드의 라인 시작 시점이 각 0.1/0.2/0.3s 지연

### 5.2 다크 모드
- 별도 정의 **불필요**. `--bg-secondary` (`#F5F5F7` ↔ `#2C2C2E`), `--bg-hover` (`rgba(0,0,0,0.04)` ↔ `rgba(255,255,255,0.06)`), `--bg-card`, `--border` 모두 `@media (prefers-color-scheme: dark)` 로 자동 전환 (§1.1 Independent Dark Mode).
- 라이트 모드: 회색 위 더 밝은 회색 띠가 흐름.
- 다크 모드: 어두운 회색 위 살짝 밝은 띠가 흐름. 콘트라스트는 의도적으로 낮음 (눈부심 방지).

### 5.3 접근성
- **aria-hidden="true"** 모든 `.skeleton-card` / 컨테이너에 필수. 스크린 리더는 시각 placeholder 를 텍스트로 읽지 않는다.
- **role="status" aria-live="polite"** 외부 wrapper 에서 제공. sr-only 텍스트(예: "검색 중…")로 로딩 상황 안내.
- **WCAG AA**: 텍스트가 없는 placeholder 이므로 색 대비 기준은 비적용 (텍스트 4.5:1 N/A). 단 `--bg-secondary` / `--bg-card` / `--border` 가 라이트·다크 모두 §2.2 토큰을 그대로 사용하므로 시스템 전체 대비 기준은 충족.
- **prefers-reduced-motion**: 본 티켓에서 추가하지 않음 — Wave 4 의 Reduced Motion 컴포넌트(별도 티켓)에서 일괄 처리. 본 mockup 은 reduced-motion 시 shimmer 가 그대로 동작하는 현재 상태를 베이스라인으로 캡처한다.

### 5.4 성능
- shimmer 는 `background-position` 변화만 사용 → GPU 가속 합성, layout / paint 비용 거의 없음.
- 동시 표시 카드 수가 많아도 비용 선형. 4 위치 모두 한 번에 표시되는 경우는 없으므로(SPA 라우팅 단일 뷰) 실측 부담 무시 가능.

---

## 6. 베이스라인 PNG 캡처 사양

`/tmp/skeleton-preview.html` (Designer-A 가 작성한 임시 fixture, QA-A 가 추후 `tests/ui/_fixtures/` 로 이전) 을 Playwright 로 캡처.

| 변종 | viewport | color-scheme | 파일 |
|------|----------|--------------|------|
| light | 1024 × 768 | light | `tests/ui/visual/baselines/skeleton-shimmer-light.png` |
| dark | 1024 × 768 | dark | `tests/ui/visual/baselines/skeleton-shimmer-dark.png` |
| mobile | 375 × 667 | light | `tests/ui/visual/baselines/skeleton-shimmer-mobile.png` |

**애니메이션 고정**: shimmer 는 무한 애니메이션이므로 픽셀 비교 재현성을 위해 `animation-play-state: paused` + `animation-delay: 0s` + `background-position: 0% 0` 을 캡처 직전 주입. (캡처 스크립트 `/tmp/capture_skeleton.py` 의 `_pause_animation()` 함수)

**예상 차이점**
- light vs dark: 카드 배경(`#FFFFFF` ↔ `#1C1C1E`) + 라인 base(`#F5F5F7` ↔ `#2C2C2E`) 색만 변경, 레이아웃 동일
- desktop vs mobile: 컨테이너 max-width 520px 안에서 모바일 375px 시 좌우 24px 패딩만 적용되어 카드 폭이 327px 로 축소. 카드 내부 비율(short 40%, medium 70%) 유지.

---

## 7. 자가 검증 체크리스트

- [x] §3 의 변종이 실제 4 위치 (목록/검색/viewer transcript/viewer summary) 에 매칭 — §3.3 표
- [x] §4 가 design.md §3.8 의 4 요소 (gradient/200%/animation/stagger) 1:1 일치 명시
- [x] 사용된 토큰이 `docs/design.md` 에 모두 존재 (§2 표)
- [x] 색 대비: 본 컴포넌트는 텍스트 없음 → WCAG 텍스트 대비 N/A. 전역 §2.2 토큰 준수
- [x] 3 베이스라인 PNG 정확히 생성 (light 12KB / dark 16KB / mobile 8KB, `tests/ui/visual/baselines/skeleton-shimmer-{light,dark,mobile}.png`)
- [x] PNG 200KB 이하 (실측 8~16KB, gradient 단순도 덕분에 매우 작음)
- [x] `ui/web/*` 직접 변경 없음 — 본 mockup 은 docs 만 작성
- [x] 새 토큰 도입 없음

---

## 8. 후속 단계

| 단계 | 담당 | 산출물 |
|------|------|--------|
| 베이스라인 PNG 캡처 | Designer-A | 본 mockup 의 §6 사양으로 3 변종 |
| Designer-B 토큰 일관성 리뷰 | Designer-B | review event |
| QA-A fixture + 시각/행동/a11y | QA-A | `tests/ui/_fixtures/skeleton-shimmer-preview.html` 등 |
| Frontend-A 4 위치 마이그레이션 | Frontend-A | `ui/web/spa.js` (line ~921 / 1278 / 1644 / 1668) |
| Frontend-B 코드 리뷰 | Frontend-B | review event |

---

**End of mockup.**
