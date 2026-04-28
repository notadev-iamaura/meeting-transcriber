# 디자인 가이드 — Meeting Transcriber

> macOS 네이티브 스타일 SaaS의 디자인 원칙, 패턴, 토큰 모음.
> 모든 UI 작업은 이 문서를 우선 참고한다.

**최종 업데이트**: 2026-04-08
**대상**: 회의 전사 SPA (3-Column Layout, Apple Silicon 전용)
**참고 출처**: macos-design-skill, saasui.design, Linear/Notion/Raycast 패턴 분석

---

## 0. 핵심 디자인 철학

이 앱은 **"Apple이 만들었다고 착각할 정도로 자연스러운 macOS 네이티브 웹앱"** 을 목표로 한다.

다음 3가지 원칙을 모든 UI 결정의 기준으로 삼는다:

1. **Familiarity**: macOS 사용자가 처음 봐도 직관적이어야 한다 (Mail, Notes, Finder 패턴 차용)
2. **Restraint**: 화려함보다 절제. 색은 강조에만 쓰고, 여백과 타이포그래피로 구조를 만든다
3. **Responsiveness**: 모든 인터랙션은 즉각 시각 피드백 (150~250ms 이내)

---

## 1. macOS 네이티브 핵심 원칙 (macos-design-skill)

### 1.1 Independent Dark Mode Design
다크 모드를 라이트 모드의 색상 반전으로 만들지 말 것. 각각 독립적으로 디자인한다.

| 모드 | 배경 톤 분리 패턴 |
|------|---------------|
| **Light** | 작은 격차 — `#FFFFFF`, `#F5F5F7`, `#FAFAFA` |
| **Dark** | 큰 격차 — `#1C1C1E`, `#2C2C2E`, `#3A3A3C` |

→ 다크 모드일수록 컴포넌트 간 톤 차이를 더 크게 줘야 깊이감이 산다.

### 1.2 Vibrancy & Backdrop Effects
사이드바, 툴바, 토스트, 모달 오버레이 등 **반투명이 어울리는 표면**은 다음 패턴을 사용한다:

```css
backdrop-filter: blur(20px) saturate(180%);
-webkit-backdrop-filter: blur(20px) saturate(180%);
background: rgba(255, 255, 255, 0.72);  /* 라이트 */
background: rgba(28, 28, 30, 0.72);     /* 다크 */
```

### 1.3 0.5px Hairline Edges
border 1px는 macOS Retina에서 두꺼워 보인다. 모든 구분선은 **0.5px**로 통일한다:

```css
border-bottom: 0.5px solid var(--border);
box-shadow: 0 0 0 0.5px var(--border);
```

### 1.4 Keyboard-First Interaction
모든 주요 액션은 **키보드 단축키**를 가져야 한다. UI에는 `<kbd>` 요소로 힌트를 표시한다.

```html
<button>회의록 검색 <kbd>⌘K</kbd></button>
<button>설정 <kbd>⌘,</kbd></button>
```

### 1.5 Progressive UI Disclosure
콘텐츠가 없을 때는 검색바, 필터, 정렬 셀렉트 등 부수적 컨트롤을 **숨긴다**. 빈 화면을 더 깨끗하게.

### 1.6 macOS Easing
transition 이징을 `ease`나 `linear` 대신 macOS 네이티브 곡선으로 통일:

```css
:root {
  --ease-macos: cubic-bezier(0.25, 0.46, 0.45, 0.94);
  --duration-fast: 150ms;
  --duration-base: 250ms;
  --duration-slow: 400ms;
}

button {
  transition: all var(--duration-base) var(--ease-macos);
}
```

---

## 2. 디자인 토큰

### 2.1 타이포그래피

| 항목 | 값 |
|------|-----|
| Font Family | `-apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif` |
| Base Size | `13px` (macOS 표준) |
| Weights | 400 (Regular) / 500 (Medium) / 600 (Semibold) / 700 (Bold) |
| Line Height | 본문 1.5 / 전사문 **1.75~1.8** (장문 가독성) |
| Letter Spacing | 본문 0 / 헤딩 -0.01em |
| Numeric | `font-variant-numeric: tabular-nums` (시간/숫자 정렬) |
| Code | `"SF Mono", Menlo, Consolas, monospace` |

> ⚠️ Inter, Roboto, Arial 같은 일반적 산세리프 금지 — 시스템 폰트가 macOS 네이티브 느낌의 핵심.

### 2.2 컬러 (CSS 변수)

라이트 모드 기본, 다크 모드는 `[data-theme="dark"]` 또는 `@media (prefers-color-scheme: dark)`로 오버라이드.

#### Light
```css
--bg-canvas: #FFFFFF;
--bg-sidebar: #F5F5F7;
--bg-nav: #F0F0F2;
--bg-card: #FFFFFF;
--bg-secondary: #F5F5F7;
--bg-input: #EFEFF1;
--bg-hover: rgba(0, 0, 0, 0.04);
--bg-active: rgba(0, 122, 255, 0.12);

--text-primary: #1D1D1F;
--text-secondary: #6E6E73;
--text-muted: #8E8E93;

--accent: #007AFF;
--accent-text: #0066CC;     /* 본문 텍스트용 (WCAG AA 5.57:1 on #FFFFFF) */
--accent-hover: #0056CC;
--success: #34C759;
--warning: #FF9500;
--error: #FF3B30;

--border: #D1D1D6;          /* macOS separator */
--border-light: #E5E5EA;
```

#### Dark (큰 톤 격차)
```css
--bg-canvas: #1C1C1E;
--bg-sidebar: #2C2C2E;
--bg-nav: #1C1C1E;
--bg-card: #2C2C2E;
--bg-secondary: #2C2C2E;
--bg-input: #3A3A3C;
--bg-hover: rgba(255, 255, 255, 0.06);
--bg-active: rgba(10, 132, 255, 0.18);

--text-primary: #F5F5F7;
--text-secondary: #98989D;
--text-muted: #8E8E93;

--accent: #0A84FF;
--accent-text: #4DA1FF;     /* 본문 텍스트용 (WCAG AA 6.37:1 on #1C1C1E) */
--border: #38383A;
```

### 2.3 스페이싱

macOS 4px 그리드 기준:

| 토큰 | 값 | 사용처 |
|------|-----|--------|
| `--space-1` | 4px | 최소 간격 (아이콘 ↔ 텍스트) |
| `--space-2` | 8px | 작은 패딩, gap |
| `--space-3` | 12px | 카드 내부 패딩 |
| `--space-4` | 16px | 카드/섹션 패딩 |
| `--space-5` | 20px | 발화 블록 패딩 |
| `--space-6` | 24px | 콘텐츠 좌우 패딩 |
| `--space-8` | 32px | 섹션 간 간격 |

### 2.4 Radius

| 토큰 | 값 | 사용처 |
|------|-----|--------|
| `--radius-sm` | 4px | 작은 배지 |
| `--radius` | 6px | 입력 필드, 작은 버튼 |
| `--radius-md` | 8px | 카드, 토글 |
| `--radius-lg` | 10px | 큰 카드, 모달 |
| `--radius-xl` | 16px | 메시지 버블 |
| `--radius-full` | 50% | 원형 배지, 토글 |

### 2.5 Shadow

| 토큰 | 값 | 사용처 |
|------|-----|--------|
| `--shadow-sm` | `0 1px 2px rgba(0, 0, 0, 0.05)` | 카드 기본 |
| `--shadow` | `0 2px 8px rgba(0, 0, 0, 0.08)` | 호버, 강조 |
| `--shadow-lg` | `0 8px 24px rgba(0, 0, 0, 0.12)` | 모달, 토스트 |
| `--shadow-hairline` | `0 0 0 0.5px var(--border)` | 보더 대신 사용 |

다크 모드는 그림자 alpha를 2배(0.1, 0.16, 0.24)로 키운다.

---

## 3. 컴포넌트 패턴

### 3.1 카드 (Card)

```css
.card {
  background: var(--bg-card);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-md);
  padding: var(--space-4);
  transition: all var(--duration-base) var(--ease-macos);
}

.card:hover {
  transform: translateY(-1px);
  box-shadow: var(--shadow);
  border-color: var(--accent);
}
```

### 3.2 버튼 (Button)

세 가지 단계:

| 종류 | 스타일 | 사용처 |
|------|--------|--------|
| **Primary** | accent 배경, 흰 텍스트 | "저장", "활성화" |
| **Secondary** | 투명 배경, accent 보더 + 텍스트 | "다운로드", "취소" |
| **Ghost** | 투명, hover 시 bg-hover | 액션 버튼, 메뉴 |

```css
.btn-primary {
  background: var(--accent);
  color: #fff;
  padding: 8px 16px;
  border-radius: var(--radius);
  font-weight: 500;
}

.btn-primary:active {
  transform: scale(0.97);
}
```

### 3.3 입력 필드 (Input)

```css
.input {
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 12px;
  font-size: 13px;
  transition: all var(--duration-fast) var(--ease-macos);
}

.input:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.15);
}
```

### 3.4 토글 스위치 (Toggle)

macOS System Preferences 스타일 — **34px × 20px**.

```css
.toggle-track {
  width: 34px;
  height: 20px;
  border-radius: 10px;
  background: var(--bg-input);
  transition: background var(--duration-fast) var(--ease-macos);
}

input:checked + .toggle-track {
  background: var(--accent);
}
```

### 3.5 모달 (Modal)

```css
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  backdrop-filter: blur(8px);
  display: flex;
  align-items: center;
  justify-content: center;
}

.modal-content {
  background: var(--bg-card);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  max-width: 480px;
  width: 90%;
  padding: 24px;
}
```

### 3.6 툴팁 (Tooltip)

`data-tooltip` 속성 + CSS pseudo-element 패턴 (의존성 없음):

```css
[data-tooltip]:hover::after,
[data-tooltip]:focus-visible::after {
  content: attr(data-tooltip);
  position: absolute;
  bottom: calc(100% + 8px);
  left: 50%;
  transform: translateX(-50%);
  background: var(--text-primary);
  color: var(--bg-canvas);
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 12px;
  max-width: 280px;
  z-index: 100;
  pointer-events: none;
}
```

### 3.7 빈 상태 (Empty State)

| 요소 | 내용 |
|------|------|
| **아이콘** | SVG, 48px, `--text-muted` |
| **제목** | 14~16px, `--text-primary` |
| **설명** | 12~13px, `--text-secondary`, max-width 280px |
| **CTA** | (선택) Primary 또는 Secondary 버튼 |

콘텐츠 영역 정중앙 배치, 위쪽 1/3 지점부터 시작.

### 3.8 스켈레톤 로딩

shimmer 애니메이션 사용. **stagger delay**로 자연스럽게:

```css
.skeleton {
  background: linear-gradient(90deg,
    var(--bg-secondary) 25%,
    var(--bg-hover) 50%,
    var(--bg-secondary) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s ease-in-out infinite;
}

.skeleton:nth-child(2) { animation-delay: 0.1s; }
.skeleton:nth-child(3) { animation-delay: 0.2s; }
```

---

## 4. SaaS 패턴 (saasui.design)

### 4.1 Command Palette (⌘K) — 🥇 최우선 적용 후보

Linear, Raycast, Notion, GitHub의 표준. **2026년 SaaS UX 1순위 패턴**.

**원칙:**
- ⌘K로 호출
- 검색 + 모든 액션을 한 곳에서
- 카테고리별 그룹핑 (회의 / 액션 / 설정 / AI)
- 키보드 화살표 + Enter로 완전 조작
- 첫 글자 매칭 + Fuzzy 검색
- 최근 사용 액션 우선 표시

**적용 예시 (우리 앱):**
```
⌘K 열림 →
┌─────────────────────────────────────┐
│ 🔍 무엇을 도와드릴까요?              │
├─────────────────────────────────────┤
│ 회의                                  │
│   📄 meeting_20260310 열기            │
│ 액션                                  │
│   🎤 새 녹음 시작        ⌘R          │
│   ⚙ 설정 열기            ⌘,          │
│ STT 모델                              │
│   ▶ seastar 활성화                    │
│ AI                                    │
│   💬 AI에게 질문하기...                │
└─────────────────────────────────────┘
```

### 4.2 Search Patterns
- 즉시 검색 (debounce 250ms)
- 검색어 하이라이팅 (`<mark>` 또는 CSS background)
- 결과 미리보기 (텍스트 일부 표시)
- "결과 없음" 빈 상태에 대안 제시

### 4.3 List Patterns
- 호버 시 `translateX(2px)` 또는 배경 변화
- 활성 항목은 accent 컬러 보더 + 반투명 배경 (`rgba(accent, 0.12)`)
- 상태 도트 (color-coded): 완료 초록, 진행 파랑, 실패 빨강

### 4.4 Notification / Toast
- **인라인 status 우선** — 토스트는 시야 분산, 가능하면 인라인 status line 사용
- 토스트 필요 시: 화면 상단 중앙, slide-down + fade
- 자동 숨김 4~6초, error는 수동 닫기
- 타입별 색상: success/warning/info/error

### 4.5 Settings Page Patterns
- macOS System Preferences 스타일 (Apple Notes 설정 참고)
- 섹션 카드 + 라벨/컨트롤 행
- 즉시 저장 vs 명시적 저장: **즉시 저장 권장** (Linear 패턴)
- 변경 후 인라인 status로 "저장됨" 표시

---

## 5. 2026 SaaS UX 트렌드

### 5.1 Hidden AI
"AI"라는 단어를 UI에 노출하지 않는다. 자연스러운 기능명으로 표현:
- ❌ "AI 채팅" → ✅ "채팅"
- ❌ "AI 요약" → ✅ "요약"
- ❌ "AI 보정" → ✅ "보정"

AI는 인프라이지 마케팅 단어가 아니다.

### 5.2 Context Persistence
사용자 컨텍스트(현재 회의, 검색어, 필터)를 URL에 인코딩하고 새로고침해도 유지.

### 5.3 Progressive Onboarding
처음 사용자에게 모든 기능을 보여주지 말고, 행동 기반으로 점진적 노출.

### 5.4 Micro-Animations Restraint
- 화려한 트랜지션 금지 (사용자가 알아차리면 실패)
- 0.15~0.25s 이내, ease-out 또는 macOS easing
- "작업이 완료되었음을 인지시키는 정도"가 적정선

---

## 6. 접근성 (A11y)

| 항목 | 요구사항 |
|------|--------|
| **키보드 내비게이션** | 모든 인터랙티브 요소가 Tab으로 접근 가능 |
| **포커스 링** | `:focus-visible`로 명확한 outline 표시 (accent 색상) |
| **ARIA 라벨** | 아이콘만 있는 버튼은 `aria-label` 필수 |
| **role/aria-live** | 동적 컨텐츠에 적절한 role 지정 (status, alert, log) |
| **`prefers-reduced-motion`** | 애니메이션 비활성화 옵션 존중 |
| **컬러 대비** | WCAG AA (4.5:1) 이상 |
| **`<kbd>`** | 키보드 단축키 시각 표시 |

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
```

---

## 7. 안티 패턴 (금지)

다음은 macOS 네이티브 느낌을 망가뜨리는 패턴들이다.

| 안티 패턴 | 대체 |
|----------|------|
| Inter, Roboto, Arial 등 비-시스템 폰트 | `-apple-system` |
| 1px solid 보더 | 0.5px hairline + box-shadow |
| `transition: all 0.3s ease` | `transition: all 250ms var(--ease-macos)` |
| 보라색/네온 그라디언트 배경 | macOS 시스템 컬러 (`#007AFF`) |
| 큰 둥근 모서리 (16px+) — 카드 | 6~10px |
| 화려한 호버 애니메이션 (회전, scale 1.2 등) | 미묘한 translateY(-1px) + shadow |
| 토스트 남발 | 인라인 status 우선 |
| AI 강조 마케팅 텍스트 | 자연스러운 기능명 |
| 모든 액션에 확인 모달 | undo 토스트로 대체 |
| 다크 모드 = 라이트 모드 색상 반전 | 독립적 톤 분리 |

---

## 8. 우리 프로젝트 적용 우선순위

### 🥇 즉시 적용 (1~2시간)
1. **macOS easing 토큰 추가** — 모든 transition을 `var(--ease-macos)`로 통일
2. **`<kbd>` 단축키 힌트 표시** — Command Palette 도입 전 사전 작업
3. **"AI" 단어 제거** — "AI 채팅" → "채팅", "AI 요약" → "요약"
4. **모달 backdrop blur 추가** — `.modal-overlay`에 `backdrop-filter: blur(8px)`

### 🥈 중간 작업 (반나절)
5. **Command Palette (⌘K)** — Linear 스타일 통합 검색 + 액션
6. **Progressive UI Disclosure** — 빈 회의 목록일 때 검색바/정렬 셀렉트 숨김

### 🥉 큰 작업 (1일+)
7. **Drag-and-Drop 오디오** — 콘텐츠 영역에 파일 드래그 → 자동 전사
8. **우클릭 컨텍스트 메뉴** — macOS 네이티브 패턴
9. **호버 액션 reveal** — 카드 호버 시 [복사][삭제] 페이드 인

---

## 9. 참고 자료

### 핵심 출처
- **[macos-design-skill](https://github.com/ceorkm/macos-design-skill)** — macOS 네이티브 웹앱 디자인 가이드
- **[saasui.design](https://www.saasui.design/)** — Linear/Notion/Figma 등 실제 SaaS 패턴 라이브러리
- **[Apple HIG (macOS)](https://developer.apple.com/design/human-interface-guidelines/macos)** — Apple 공식 가이드라인

### Awesome 모음
- [klaufel/awesome-design-systems](https://github.com/klaufel/awesome-design-systems)
- [alexpate/awesome-design-systems](https://github.com/alexpate/awesome-design-systems)
- [gztchan/awesome-design](https://github.com/gztchan/awesome-design)
- [flipflop/Awesome-Design-System](https://github.com/flipflop/Awesome-Design-System)
- [Design Resources for Mac](https://marioaguzman.github.io/design/)

### 영감을 얻을 앱
- **Linear** — 미니멀 + 키보드 우선 (Command Palette 표준)
- **Raycast** — Spotlight 대체, Command Palette UX의 정수
- **Apple Notes** — macOS 네이티브 사이드바 + 콘텐츠 패턴
- **Apple Mail** — 3-Column 레이아웃 (우리 앱과 동일 구조)
- **Apple Finder** — 0.5px hairline, vibrancy의 교과서
- **Notion** — Hidden AI, 인라인 status, Progressive disclosure

---

## 10. 변경 이력

| 일자 | 변경 내용 |
|------|---------|
| 2026-04-08 | 초기 작성 — awesome 리포 리서치 결과 정리 |
