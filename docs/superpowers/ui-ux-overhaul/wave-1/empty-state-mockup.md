# Empty State 컴포넌트 시각 정의 (T-101)

> **Wave**: 1 · **Component**: empty-state · **Author**: ui-ux-designer-a
> **Reference**: `docs/design.md` §1.1 (Independent Dark Mode), §2.2 (컬러 토큰), §3.2 (버튼), §3.7 (빈 상태)
> **Spec**: `docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md` §3 항목 1
> **베이스라인 PNG**: `tests/ui/visual/baselines/empty-state-{light|dark|mobile}.png`

---

## 1. 목적

회의 목록·검색 결과·채팅 3 위치의 "콘텐츠 없음" 상황을, design.md §3.7 의 빈 상태 패턴(48px 아이콘 + 제목 + 설명 + 선택 CTA, 콘텐츠 영역 정중앙·위쪽 1/3 배치)으로 통일한다. 메시지·아이콘·CTA 의 차이만 위치별로 가지며, 토큰·레이아웃·타이포그래피·인터랙션 규칙은 모두 동일하게 공유한다. 본 목업은 Frontend-A 가 `ui/web/spa.js` 의 빈 상태 마크업을 교체할 때 따를 시각 계약이다.

---

## 2. 사용 토큰

`docs/design.md` 에 정의된 토큰만 사용한다. 새 토큰은 도입하지 않는다.

| 영역 | 토큰 | 라이트 값 | 다크 값 | 출처 |
|------|------|----------|--------|------|
| 컨테이너 배경 | `--bg-canvas` | `#FFFFFF` | `#1C1C1E` | §2.2 |
| 사이드바·리스트 패널 배경 | `--bg-sidebar` | `#F5F5F7` | `#2C2C2E` | §2.2 |
| 아이콘 색 (48px SVG) | `--text-muted` | `#AEAEB2` | `#636366` | §2.2 / §3.7 |
| 제목 색 (14px) | `--text-primary` | `#1D1D1F` | `#F5F5F7` | §2.2 / §3.7 |
| 설명 색 (13px) | `--text-secondary` | `#86868B` | `#98989D` | §2.2 / §3.7 |
| CTA 강조색 (Secondary 보더·텍스트) | `--accent` | `#007AFF` | `#0A84FF` | §2.2 / §3.2 |
| CTA 호버 배경 | `--bg-hover` | `rgba(0,0,0,0.04)` | `rgba(255,255,255,0.06)` | §2.2 |
| 보더 (CTA·구분) | `--border` | `#D1D1D6` | `#38383A` | §2.2 |
| 간격 (요소 사이) | `--space-2` (8px) | — | — | §2.3 |
| 간격 (CTA 위 마진) | `--space-3` (12px) | — | — | §2.3 |
| 좌우 안전 패딩 | `--space-4` (16px) | — | — | §2.3 |
| Radius (CTA) | `--radius` (6px) | — | — | §2.4 |
| Easing | `--ease-macos` | — | — | §1.6 |
| Duration (호버) | `--duration-fast` (150ms) | — | — | §1.6 |

> 🔍 **검증**: 위 토큰 모두 `ui/web/style.css` 에 이미 선언돼 있음 (Grep `--text-muted` → 55 hits, `--accent` → 약 100+ hits 등). 새 변수 도입 없음.

### 타이포그래피 (design.md §2.1)

| 요소 | font-size | font-weight | color | letter-spacing |
|------|-----------|-------------|-------|----------------|
| 제목 | 14px | 600 (Semibold) | `--text-primary` | -0.01em |
| 설명 | 13px | 400 (Regular) | `--text-secondary` | 0 |
| CTA 라벨 | 13px | 500 (Medium) | `--accent` | 0 |

font-family 는 SPA 전역 `-apple-system, BlinkMacSystemFont, "SF Pro Text"` 를 그대로 상속 (§2.1).

---

## 3. 레이아웃 구조

```
┌─────────────────────── (콘텐츠 영역 width 변동) ───────────────────────┐
│                                                                       │
│                                                                       │  ← 위쪽 1/3 까지 빈 공간 (§3.7)
│                                                                       │
│                              ┌──────┐                                 │
│                              │ icon │  48 × 48 SVG · color=text-muted │
│                              └──────┘                                 │
│                                                                       │  ← gap: 8px (--space-2)
│                          제목 14px Semibold                            │
│                                                                       │  ← gap: 8px
│                  설명 13px Regular · max-width 280px · text-align center
│                                                                       │  ← gap: 12px (--space-3)
│                          ┌───────────────┐                            │
│                          │   CTA 버튼     │   (선택, 위치별로 다름)       │
│                          └───────────────┘                            │
│                                                                       │
│                                                                       │  ← 아래는 자유 공간
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
```

### CSS 골격 (Frontend-A 가 style.css 에 추가할 형태)

```css
/* 빈 상태 컨테이너 */
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;        /* 위쪽 1/3 시작 */
  padding-top: 25%;                   /* 콘텐츠 영역 위쪽 1/3 지점 */
  padding-left: var(--space-4);
  padding-right: var(--space-4);
  padding-bottom: var(--space-4);
  gap: var(--space-2);                /* 아이콘·제목·설명 사이 8px */
  text-align: center;
  min-height: 100%;                   /* 부모 영역 가득 (사이드바 / content-panel 모두) */
  box-sizing: border-box;
}

.empty-state-icon {
  width: 48px;
  height: 48px;
  color: var(--text-muted);           /* SVG stroke/fill 모두 currentColor */
  margin-bottom: 0;                   /* gap 으로 처리 */
}

.empty-state-title {
  font-size: 14px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--text-primary);
  margin: 0;
}

.empty-state-description {
  font-size: 13px;
  font-weight: 400;
  color: var(--text-secondary);
  max-width: 280px;
  margin: 0;
  line-height: 1.5;
}

.empty-state-cta {
  /* design.md §3.2 Secondary 버튼 — 빈 상태에서 상황 강조 없이 권유 톤 */
  margin-top: var(--space-3);         /* 설명과 12px 분리 */
  padding: 8px 16px;
  background: transparent;
  color: var(--accent);
  border: 0.5px solid var(--accent);  /* §1.3 hairline */
  border-radius: var(--radius);       /* 6px */
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease-macos),
              transform var(--duration-fast) var(--ease-macos);
}

.empty-state-cta:hover {
  background: var(--bg-hover);
}

.empty-state-cta:active {
  transform: scale(0.97);
}

.empty-state-cta:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.15);  /* §3.3 입력 필드와 동일 */
}

@media (prefers-reduced-motion: reduce) {
  .empty-state-cta {
    transition: none;
  }
}
```

> ⚠️ Frontend-A 는 위 CSS 가 spa.js 가 이미 사용 중인 `.list-empty` (현재 `_listEl` 안에 회색 텍스트 한 줄만 출력) 를 대체할 때, 회의 목록 사이드바 (320px width) 안에서도 같은 비율로 동작하는지 직접 렌더 확인할 것.

---

## 4. 라이트 / 다크 / 모바일 변종

### 4.1 라이트 (1024 × 768, color-scheme: light)

ASCII (회의 목록 사이드바 320px 폭 시뮬레이션):

```
┌───────────── 320 × 600 list-panel ─────────────┐
│ bg: #F5F5F7 (--bg-sidebar)                     │
│                                                │
│ (위쪽 1/3 ≈ 200px 여백)                          │
│                                                │
│             ┌──────────┐                       │
│             │  ░░░░░░  │  ← 48px SVG           │
│             │ ░░ aud ░░│    color: #AEAEB2     │
│             │  ░░░░░░  │    (--text-muted)     │
│             └──────────┘                       │
│                  ↕ 8px                         │
│       아직 회의가 없어요  ← 14px / 600          │
│                  ↕ 8px       color: #1D1D1F   │
│   첫 회의를 녹음하면         ← 13px / 400       │
│   자동으로 전사·요약돼요     color: #86868B    │
│                  ↕ 12px                        │
│             ┌────────────┐                     │
│             │  녹음 시작   │ ← Secondary 버튼    │
│             └────────────┘   color: #007AFF    │
│                              border: #007AFF  │
│                                                │
│                                                │
└────────────────────────────────────────────────┘
```

### 4.2 다크 (1024 × 768, color-scheme: dark)

```
┌───────────── 320 × 600 list-panel ─────────────┐
│ bg: #2C2C2E (--bg-sidebar / Dark)              │
│                                                │
│ (위쪽 1/3 ≈ 200px 여백)                          │
│                                                │
│             ┌──────────┐                       │
│             │  ░░░░░░  │  ← 48px SVG           │
│             │ ░░ aud ░░│    color: #636366     │
│             │  ░░░░░░  │    (--text-muted)     │
│             └──────────┘                       │
│                  ↕ 8px                         │
│       아직 회의가 없어요  ← 14px / 600          │
│                  ↕ 8px       color: #F5F5F7   │
│   첫 회의를 녹음하면         ← 13px / 400       │
│   자동으로 전사·요약돼요     color: #98989D    │
│                  ↕ 12px                        │
│             ┌────────────┐                     │
│             │  녹음 시작   │ ← Secondary 버튼    │
│             └────────────┘   color: #0A84FF    │
│                              border: #0A84FF  │
│                                                │
└────────────────────────────────────────────────┘
```

> 다크 모드 톤 격차 검증: 캔버스 `#1C1C1E` 와 사이드바 `#2C2C2E` 사이 ΔL ≈ 4 (단계 톤). 본 빈 상태는 사이드바·콘텐츠 영역 어디에 들어가도 부모 배경 위에서 self-contrast 가 유지되도록 자체 배경을 깔지 않는다.

### 4.3 모바일 (375 × 667, color-scheme: light)

모바일에서는 list-panel 이 화면 전체 폭을 차지한다 (Wave 3 의 햄버거 드로어 작업 전까지는 375px 가 콘텐츠 폭).

```
┌──────── 375 × 667 viewport ─────────┐
│ bg: #FFFFFF (--bg-canvas)           │
│                                     │
│  (위쪽 1/3 ≈ 220px 여백)              │
│                                     │
│            ┌──────────┐             │
│            │  ░░░░░░  │             │
│            │ ░░ aud ░░│             │
│            │  ░░░░░░  │             │
│            └──────────┘             │
│                ↕ 8px                │
│       아직 회의가 없어요              │
│                ↕ 8px                │
│   첫 회의를 녹음하면 자동으로          │
│         전사·요약돼요                │
│                ↕ 12px               │
│           ┌────────────┐            │
│           │  녹음 시작   │            │
│           └────────────┘            │
│                                     │
└─────────────────────────────────────┘
```

좌우 패딩 `--space-4` (16px) 으로 max-width 280px 설명문이 자동 wrap. 아이콘·제목·CTA 는 가운데 정렬을 유지하고, 짧은 viewport 에서도 위쪽 1/3 비율은 그대로 보존된다.

---

## 5. 인터랙션 노트 (3 위치별 차이)

3 위치 모두 **레이아웃·타이포그래피·CTA 구조는 동일**. 변하는 것은 (1) 아이콘 모양, (2) 제목 텍스트, (3) 설명 텍스트, (4) CTA 라벨·동작 4가지뿐.

### 5.1 회의 목록 0개 (`ui/web/spa.js:949~955`)

| 항목 | 내용 |
|------|------|
| **아이콘** | SF Symbol 풍 마이크/녹음 아이콘 (`waveform.circle` 변형, 48px stroke) |
| **제목** | `아직 회의가 없어요` |
| **설명** | `첫 회의를 녹음하면 자동으로 전사·요약돼요.` |
| **CTA** | `녹음 시작` → 메뉴바 `_on_start_recording` 트리거 (Frontend-A 가 기존 핸들러 연결) |
| **컨테이너** | 사이드바 list panel (320px 폭, height: 100% 부모 영역) |
| **렌더 트리거** | `meetings.length === 0` |
| **a11y** | `role="status"` + `aria-live="polite"` (목록이 비동기 로드 후 0개로 확정될 때 SR 알림) |

### 5.2 검색 결과 0개

| 항목 | 내용 |
|------|------|
| **아이콘** | 돋보기 (`magnifyingglass` 변형, 48px) |
| **제목** | `검색 결과가 없어요` |
| **설명** | `다른 키워드로 다시 검색해 보세요. 띄어쓰기·맞춤법을 한번 더 확인해 주세요.` |
| **CTA** | (없음) — design.md §3.7 에서 CTA 는 "선택" 으로 명시. 검색은 사용자가 입력 필드에 다시 입력하는 게 자연스러우므로 CTA 생략. |
| **컨테이너** | 검색 결과 영역 (콘텐츠 영역 내) |
| **렌더 트리거** | 검색 응답의 결과 배열 길이가 0 |
| **a11y** | `role="status"` + `aria-live="polite"` (검색 결과는 동적 변경) |

### 5.3 채팅 빈 상태

| 항목 | 내용 |
|------|------|
| **아이콘** | 말풍선 (`bubble.left.and.bubble.right` 변형, 48px) |
| **제목** | `대화를 시작해 보세요` |
| **설명** | `회의 내용에 대해 무엇이든 물어보세요. 화자별 요약·결정사항·다음 액션 등을 정리해 드려요.` |
| **CTA** | (없음) — 채팅 입력창이 페이지 하단에 항상 있어 별도 CTA 불필요 (design.md §1.5 Progressive Disclosure: 부수적 컨트롤 숨김) |
| **컨테이너** | 채팅 메시지 리스트 영역 (콘텐츠 영역 내) |
| **렌더 트리거** | 채팅 메시지 배열 길이가 0 |
| **a11y** | `role="status"` (alert 아님 — 시작 안내는 emergency 가 아님) |

> "AI" 단어 사용 금지 (design.md §5.1 Hidden AI). 채팅 빈 상태 설명에서도 "AI" 가 들어가지 않도록 작성.

---

## 6. WCAG AA 색대비 검증

WCAG 2.1 AA 기준:
- **본문 텍스트(<18pt 또는 <14pt bold)**: 4.5:1 이상
- **대형 텍스트(≥18pt 또는 ≥14pt bold)**: 3:1 이상
- **비-텍스트 콘텐츠 (아이콘·UI 컴포넌트)**: 3:1 이상 (WCAG 1.4.11)

본 빈 상태에서 14px Semibold 제목은 macOS 기준 18pt 이상 굵은 글씨에 해당 → 큰 텍스트 (3:1) 기준 적용. 13px 일반 텍스트 설명은 본문 (4.5:1) 기준.

### 6.1 라이트 모드

| 요소 | 전경색 | 배경색 | 측정 비율 | 기준 | 결과 |
|------|--------|--------|----------|------|------|
| 제목 (14px/600) | `#1D1D1F` (text-primary) | `#FFFFFF` (bg-canvas) | **16.83:1** | ≥3:1 (큰텍스트) / ≥4.5:1 (본문) | ✅ AA 통과 |
| 제목 (14px/600) | `#1D1D1F` | `#F5F5F7` (bg-sidebar) | **16.04:1** | ≥3:1 / ≥4.5:1 | ✅ AA 통과 |
| 설명 (13px/400) | `#86868B` (text-secondary) | `#FFFFFF` | **3.62:1** | ≥4.5:1 (본문) | ❌ AA 미달 — 후술 |
| 설명 (13px/400) | `#86868B` | `#F5F5F7` | **3.45:1** | ≥4.5:1 | ❌ AA 미달 — 후술 |
| 아이콘 (48px) | `#AEAEB2` (text-muted) | `#FFFFFF` | **2.21:1** | ≥3:1 (1.4.11) | ❌ 미달 — 후술 |
| CTA 라벨 (13px/500) | `#007AFF` (accent) | `#FFFFFF` | **4.02:1** | ≥4.5:1 | ❌ AA 미달 — 후술 |
| CTA 보더 0.5px | `#007AFF` | `#FFFFFF` | **4.02:1** | ≥3:1 (UI 컴포넌트) | ✅ 통과 |

### 6.2 다크 모드

| 요소 | 전경색 | 배경색 | 측정 비율 | 기준 | 결과 |
|------|--------|--------|----------|------|------|
| 제목 (14px/600) | `#F5F5F7` (text-primary) | `#1C1C1E` (bg-canvas) | **15.63:1** | ≥3:1 / ≥4.5:1 | ✅ AA 통과 |
| 제목 (14px/600) | `#F5F5F7` | `#2C2C2E` (bg-sidebar) | **13.94:1** | ≥3:1 / ≥4.5:1 | ✅ AA 통과 |
| 설명 (13px/400) | `#98989D` (text-secondary) | `#1C1C1E` | **5.93:1** | ≥4.5:1 | ✅ AA 통과 |
| 설명 (13px/400) | `#98989D` | `#2C2C2E` | **5.29:1** | ≥4.5:1 | ✅ AA 통과 |
| 아이콘 (48px) | `#636366` (text-muted) | `#1C1C1E` | **2.84:1** | ≥3:1 | ❌ 근소 미달 — 후술 |
| 아이콘 (48px) | `#636366` | `#2C2C2E` | **2.54:1** | ≥3:1 | ❌ 근소 미달 — 후술 |
| CTA 라벨 (13px/500) | `#0A84FF` (accent) | `#1C1C1E` | **4.66:1** | ≥4.5:1 | ✅ AA 통과 |
| CTA 보더 0.5px | `#0A84FF` | `#1C1C1E` | **4.66:1** | ≥3:1 | ✅ 통과 |

### 6.3 측정 결과 요약 + 후술

**다크 모드는 본문 색대비 (5.93:1, 5.29:1) 가 AA 4.5:1 을 충족** 하므로 design.md 토큰을 그대로 따를 때 문제 없음.

**라이트 모드의 3 가지 잠재 위반**:

1. **`text-secondary` (#86868B) on white = 3.62:1** — design.md §3.7 이 본문 설명에 `--text-secondary` 를 명시하나, WCAG AA 본문 4.5:1 미달.
   - 동일 이슈가 SPA 의 모든 secondary 텍스트 (현재 약 100+ 개소) 에 이미 존재하며, design.md 토큰 자체의 한계.
   - **권장**: Designer-B 검토에서 design.md 토큰 보강 (Light `--text-secondary` 를 `#6E6E73` ≈ 4.54:1 로 어둡게 조정) 을 별도 후속 티켓으로 제기. 본 티켓에서는 design.md 의 토큰을 정직하게 따름.

2. **`text-muted` (#AEAEB2) on white = 2.21:1** — 아이콘 색에 대한 WCAG 1.4.11 (3:1) 미달.
   - design.md §3.7 가 명시한 색이지만, 1.4.11 기준 미달. SVG stroke 두께를 1.5px 이상으로 두고 형태 인식이 색에 의존하지 않도록 보강 (의미 정보를 색으로만 전달하지 않음 — WCAG 1.4.1).

3. **`accent` (#007AFF) on white = 4.02:1** — CTA 라벨 본문 텍스트 기준 4.5:1 근소 미달.
   - 보더(UI 컴포넌트, 3:1 기준) 는 통과. 라벨 가독성을 위해 `font-weight: 500` 을 유지 (이미 적용) 하고, 사용자가 호버하면 `--bg-hover` 위에서 약간 더 어두워진 배경 대비 라벨이 그대로 4.02:1 → 약 4.06:1 로 거의 변화 없음.
   - **권장**: 본 빈 상태 한정으로는 받아들이고, design.md 토큰 보강을 후속 티켓으로 제기.

### 6.4 본 티켓의 결론

본 빈 상태 컴포넌트 자체는 design.md 토큰을 100% 따른다 (재정의·새 토큰 도입 없음). 다크 모드 본문 텍스트는 AA 통과, 라이트 모드 본문 텍스트는 design.md 토큰의 알려진 한계로 AA 미달. 위 3 항목은 SPA 전역 이슈이므로 본 컴포넌트만 별도 색을 쓰는 것은 design.md 일관성 깨뜨리고 부작용이 더 크다고 판단. **Designer-B 와의 리뷰 + Wave 1 티켓 3 (다크모드 톤 격차) 작업 시 라이트 모드 토큰도 동시 보강** 을 제안하는 것이 옳다.

> 📌 Designer-B 검토 시 우려사항으로 최우선 항목: 라이트 `--text-secondary` (3.62:1) 의 후속 티켓화 여부.

---

## 7. 자가 검증 체크리스트 결과

- [x] 목업이 spec §3 항목 1 의 완료 정의 다 다룸 — 회의 목록 0개 / 검색 0개 / 채팅 빈 상태 3 위치 모두 §5 에 명시
- [x] 사용된 토큰이 `docs/design.md` §2.2 등에 모두 존재함을 grep 으로 확인 — `--text-muted` 55 hits, `--text-primary`/`--text-secondary`/`--accent`/`--bg-canvas`/`--bg-sidebar`/`--bg-hover`/`--border`/`--ease-macos` 등 합계 595 hits in style.css
- [x] 3 변종 PNG 가 정확히 생성됨 — `tests/ui/visual/baselines/empty-state-{light|dark|mobile}.png`
- [x] 색대비 라이트·다크 둘 다 명시적으로 측정 — §6 표 + 한계점 정직 보고
- [x] 베이스라인 PNG 200KB 이하 — 캡처 후 `du -h` 로 검증
- [x] `ui/web/spa.js`, `ui/web/style.css` 직접 변경 없음 — `git diff --stat ui/web/` 가 비어있음 확인

---

## 8. Frontend-A 에게 — 구현 시 유의점

1. 위 §3 의 CSS 골격을 `ui/web/style.css` 에 추가하되 **새 CSS 변수 도입 금지**.
2. `ui/web/spa.js:949~955` 의 `.list-empty` 마크업을 `.empty-state` 구조 (icon + title + description + cta) 로 교체. 검색 결과·채팅 빈 상태도 동일 컴포넌트 함수로 통합.
3. 아이콘은 inline SVG (외부 파일 의존성 없이) 로 작성하고 `currentColor` 를 stroke/fill 에 사용해 `--text-muted` 가 자동 반영되게 한다.
4. CTA 클릭 핸들러는 회의 목록 빈 상태에 한해 메뉴바·녹음 트리거에 연결. 검색·채팅은 CTA 자체가 없으므로 핸들러 불필요.
5. `aria-live="polite"`, `role="status"` 추가 (Wave 3 의 ARIA 동기화 티켓과 충돌 없음).
6. `prefers-reduced-motion: reduce` 시 호버 transition 제거 (이미 §3 CSS 골격에 포함).

---

## 9. 변경 이력

| 일자 | 변경 내용 |
|------|---------|
| 2026-04-27 | 초기 작성 — Designer-A self-check 통과 |
