# 디자인 이터레이션 마스터 계획서 (2026-04-08)

> **대상**: Meeting Transcriber 웹 UI (SPA)
> **참고**: `docs/design.md` — macOS 네이티브 디자인 가이드
> **팀**: 디자이너 3 · 프론트엔드 3 · QA 3
> **작성자**: pm (디자이너 출신 PM)
> **문서 성격**: 7개 개선 작업의 병렬 실행을 위한 단일 진실 공급원(SSOT)

---

## 1. 배경 및 목표

### 1.1 배경
`docs/design.md`가 새로 확정되었다. 이 가이드는 "Apple이 만들었다고 착각할 정도로 자연스러운 macOS 네이티브 웹앱"을 목표로 하며, macos-design-skill + saasui.design + Apple HIG에서 도출한 원칙을 정리한다.

현재 SPA(`ui/web/`)는 기능은 완성되어 있으나, 가이드와의 **6개 핵심 격차**가 확인되었다.

### 1.2 목표
7개 개선 작업을 **3개 워크스트림**으로 병렬 실행하여 가이드와의 격차를 제거한다. 모든 변경은 TDD 시나리오(Red → Green → 회귀 방지)로 검증한다.

### 1.3 비목표 (명시적 제외)
- 드래그앤드롭 오디오 업로드 (design.md §8 🥉 항목 — 별도 이터레이션)
- 우클릭 컨텍스트 메뉴 (별도 이터레이션)
- 전체 색상 팔레트 재정의 (이미 토큰화되어 있어 이번엔 토큰 **추가**만)
- pyproject/Python 코드 변경 (순수 웹 UI 한정)

---

## 2. 현재 코드 진단

### 2.1 대상 파일
| 파일 | 라인 수 | 역할 |
|---|---:|---|
| `ui/web/style.css` | 4,935 | 전체 스타일 (디자인 토큰 + 컴포넌트) |
| `ui/web/spa.js` | 5,382 | SPA 라우터 + HomeView/ViewerView/ChatView |
| `ui/web/index.html` | 112 | SPA 셸 (nav + list-panel + content) |

### 2.2 가이드 격차 진단 결과

| # | 항목 | 가이드 요구 | 현재 상태 | 격차 |
|---|---|---|---|---|
| G1 | macOS easing 토큰 | `--ease-macos`, `--duration-fast/base/slow` | `--transition: 0.2s ease` 단일 변수 | **0건 / 누락** |
| G2 | 1px → 0.5px hairline | 카드/입력 구분선 0.5px | `1px solid` 사용 | **37건** |
| G3 | 모달 backdrop blur | `backdrop-filter: blur(8px)` | `.modal-overlay`에 blur 없음 | **누락** |
| G4 | `<kbd>` 단축키 힌트 | 버튼 옆 kbd 요소 | CSS 스타일 없음, 사용처 0건 | **0건** |
| G5 | Hidden AI | "AI" 단어 UI 비노출 | `spa.js` 내 "AI 채팅/요약/보정" 다수 + `index.html`의 `aria-label="AI 채팅"` | **9건** (기존 8건 + index.html 1건) |
| G6 | Progressive Disclosure | 빈 목록일 때 검색바/정렬 숨김 | 항상 노출 (`index.html:63-72`) | **누락** |
| G7 | Command Palette (⌘K) | Linear/Raycast 표준 | 전역 단축키 전무 | **신규 구현 필요** |

### 2.3 "AI" 단어 노출 위치 상세 (G5)

`ui/web/spa.js` 8곳 + `ui/web/index.html` 1곳 = **총 9곳**:

| 위치 | 원문 (축약) | 분류 |
|---|---|---|
| `index.html:33` | `aria-label="AI 채팅"` | Nav 버튼 |
| `spa.js:1013` | `"...AI Chat에서 자연어로..."` | 빈 검색 결과 안내 |
| `spa.js:1195` | `회의록 (AI 요약)` | 탭 라벨 |
| `spa.js:1247` | `AI 요약을 생성할 수 있습니다.` | 안내문 |
| `spa.js:1856` | `다음부터 AI 보정에 자동 반영` | 체크박스 라벨 |
| `spa.js:2906` | `AI 출력으로 덮어쓰여요` | 확인 다이얼로그 |
| `spa.js:3067` | `AI 회의 어시스턴트` | Chat welcome title |
| `spa.js:3070` | `AI가 답변합니다` | Chat welcome body |
| `spa.js:3113` | `AI가 답변을 생성하고...` | 타이핑 인디케이터 |
| `spa.js:3149` | `document.title = "AI Chat — 회의록"` | 문서 제목 |
| `spa.js:3343` | `⚠ AI 모델 응답 불가` | 에러 알림 |
| `spa.js:3533` | `AI 엔진이 아직 준비되지 않았...` | 에러 토스트 |
| `spa.js:4608` | `AI 채팅에 사용해요` | 설정 설명 |
| `spa.js:5052` | `AI가 자동으로 교정해 드려요` | 빈 상태 설명 |

> **주의**: 원 태스크 메시지는 "8건"이라 했으나 정밀 그렙 결과 **14건**이 발견됨 (일부는 개발자용 주석이라 제외 판단 필요). **코드 주석(`// === ChatView (AI 채팅) ===` 등)은 유지**, **사용자 노출 텍스트만 교체**.

### 2.4 `transition:` 사용 현황 (G1)

`style.css`에서 `transition:` 총 **약 50건** 확인 (head 40 기준). `var(--transition)` 사용 28건, 하드코딩 `0.3s ease` 약 10건, `0.4s ease` 2건 등 혼재.

→ 1차 교체 대상: `var(--transition)` 기반 28곳. 하드코딩 duration은 2차로 분류.

### 2.5 `1px solid` 사용 현황 (G2)

총 **37건**. 이번 이터레이션 범위는 **카드/입력만** (약 15~20건 예상). 컨테이너/나누기선(`nav-bar`, `list-panel` 간 경계)은 시각 회귀 위험이 커 **2차 이터레이션으로 보류**.

---

## 3. 7개 작업 상세

각 작업의 포맷:
- **(a) 현재 위치**: 파일:라인 또는 식별자
- **(b) 변경 내용**: 구체적 코드 변화
- **(c) TDD 시나리오**: Red(실패해야 하는 기대) / Green(통과해야 하는 기대) / 회귀 방지
- **(d) Before/After 검증 기준**: 시각/기능/접근성

---

### 작업 1 — macOS Easing 토큰 통일 (WS-1)

**(a) 현재 위치**
- `style.css:59` — `--transition: 0.2s ease;`
- `style.css` 전역 — `var(--transition)` 참조 28곳

**(b) 변경 내용**
`:root` 블록에 토큰 신규 추가:
```css
--ease-macos: cubic-bezier(0.25, 0.46, 0.45, 0.94);
--duration-fast: 150ms;
--duration-base: 250ms;
--duration-slow: 400ms;
/* 하위 호환: 기존 --transition 유지하되 내부값 교체 */
--transition: var(--duration-base) var(--ease-macos);
```

> **핵심 전략**: `--transition` 변수값만 교체하면 28곳이 자동으로 새 이징을 따른다. 리스크 최소화 + 원자적 롤백 가능.

**(c) TDD 시나리오**
- **Red**: `style.css`에서 `--ease-macos`를 grep → 0건 (변경 전)
- **Green**:
  - `--ease-macos` 정의 1건 존재
  - `--duration-fast/base/slow` 3개 모두 존재
  - `--transition` 값에 `var(--ease-macos)` 포함
- **회귀 방지**: CI grep 가드 — `style.css`에 `cubic-bezier(0.25, 0.46, 0.45, 0.94)` 문자열 최소 1회 등장해야 통과

**(d) Before/After 검증 기준**
- **시각**: 버튼 호버, 카드 호버, 토글 스위치 전환 시 곡선이 약간 더 "부드럽고 자연스러움" (Chrome DevTools Performance로 easing 곡선 캡처)
- **기능**: 모든 `var(--transition)` 사용처가 정상 동작 (회귀 0건)
- **접근성**: `prefers-reduced-motion` 미디어 쿼리가 이미 있으면 유지, 없으면 이번 작업에 추가

---

### 작업 2 — 0.5px Hairline 마이그레이션 (WS-1)

**(a) 현재 위치**
`style.css` 내 `1px solid var(--border)` 또는 `1px solid var(--border-light)` 패턴 — **카드 및 입력 필드 한정** 약 15~20건.

**(b) 변경 내용**
카드/입력 한정 교체:
```css
/* Before */
border: 1px solid var(--border);
/* After */
border: 0.5px solid var(--border);
```

**범위 명확화** (이번 이터레이션 포함):
- `.card`, `.meeting-card`, `.viewer-card` 등 카드류
- `.input`, `input[type="search"]`, `textarea`, `select` 등 입력류
- `.modal-content`

**범위 제외** (2차 이터레이션):
- `#nav-bar`, `#list-panel`, `#content` 간 레이아웃 경계선
- `.toast`, `.recording-status` 등 플로팅 요소 (별도 검토)

**(c) TDD 시나리오**
- **Red**: 카드/입력 선택자 근처에 `1px solid` 존재 확인
- **Green**: 대상 선택자 블록 내 `1px solid` 0건, `0.5px solid` ≥15건
- **회귀 방지**: 스냅샷 테스트 — Playwright로 `HomeView`의 회의 카드 3개 영역 screenshot diff (±2% 허용)

**(d) Before/After 검증 기준**
- **시각**: Retina 디스플레이(2x)에서 카드 보더가 "더 얇고 섬세함". 1x 디스플레이에서는 서브픽셀 렌더링으로 약간 흐려질 수 있음 → **box-shadow fallback 필요 여부 QA가 판정**
- **기능**: 레이아웃 shift 없음 (0.5px은 렌더 후 반올림되지만 box model에서 1px 공간 차지 가능 — layout thrash 체크)
- **접근성**: 포커스 링 대비는 유지 (포커스는 별도 `box-shadow` 사용 중)

---

### 작업 3 — 모달 backdrop blur 추가 (WS-1)

**(a) 현재 위치**
`style.css:4269` — `.modal-overlay` 블록

**(b) 변경 내용**
```css
.modal-overlay {
  /* 기존 속성 유지 */
  background: rgba(0, 0, 0, 0.4);
  backdrop-filter: blur(8px) saturate(180%);
  -webkit-backdrop-filter: blur(8px) saturate(180%);
}
```

**(c) TDD 시나리오**
- **Red**: `.modal-overlay` 블록에 `backdrop-filter` 0건
- **Green**: `backdrop-filter`와 `-webkit-backdrop-filter` 모두 `blur(8px)` 포함
- **회귀 방지**: Playwright로 모달 열기 → 배경 요소(list-panel) 가 blur 필터 적용된 상태 screenshot diff

**(d) Before/After 검증 기준**
- **시각**: 모달 뒤 배경이 흐려짐 → macOS Big Sur+ 느낌
- **기능**: 성능 회귀 체크 — backdrop-filter는 GPU 비용이 있음, pywebview 환경에서 60fps 유지 확인
- **접근성**: `prefers-reduced-transparency` 지원 추가 (있으면 blur 제거)

---

### 작업 4 — kbd 컴포넌트 CSS 추가 (WS-1)

**(a) 현재 위치**
`style.css` — `kbd` 스타일 0건 (신규 추가)

**(b) 변경 내용**
`style.css` 하단 컴포넌트 섹션에 추가:
```css
kbd {
  display: inline-flex;
  align-items: center;
  padding: 2px 6px;
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: 11px;
  font-weight: 500;
  line-height: 1;
  color: var(--text-secondary);
  background: var(--bg-input);
  border: 0.5px solid var(--border);
  border-radius: 4px;
  box-shadow: 0 1px 0 rgba(0, 0, 0, 0.08);
  min-width: 18px;
  justify-content: center;
}

.btn kbd, .nav-btn kbd {
  margin-left: 6px;
  opacity: 0.7;
}
```

**(c) TDD 시나리오**
- **Red**: `style.css`에서 `^kbd\s*\{` 선택자 0건
- **Green**: kbd 선택자 블록 ≥1개, `.btn kbd` 보조 스타일 존재
- **회귀 방지**: Storybook 또는 샌드박스 페이지 `tests/fixtures/kbd-sample.html` 생성하여 렌더 확인

**(d) Before/After 검증 기준**
- **시각**: `<kbd>⌘K</kbd>` 렌더 시 macOS Spotlight 힌트 느낌
- **기능**: (이 작업 단독으론 노출 없음, 작업 7·9에서 사용)
- **접근성**: 명도 대비 AA (텍스트 색 vs 배경) — QA 확인

---

### 작업 5 — Hidden AI 텍스트 교체 (WS-2)

**(a) 현재 위치**
§2.3 표 참고. 14곳 전수.

**(b) 변경 내용**
원칙: "AI"를 기능명으로 대체하거나 삭제.

| 위치 | Before | After |
|---|---|---|
| `index.html:33` | `aria-label="AI 채팅"` | `aria-label="채팅"` |
| `spa.js:1013` | `AI Chat에서 자연어로 질문` | `채팅에서 자연어로 질문` |
| `spa.js:1195` | `회의록 (AI 요약)` | `회의록 (요약)` |
| `spa.js:1247` | `AI 요약을 생성` | `요약을 생성` |
| `spa.js:1856` | `AI 보정에 자동 반영` | `보정에 자동 반영` |
| `spa.js:2906` | `AI 출력으로 덮어쓰여요` | `보정된 출력으로 덮어쓰여요` |
| `spa.js:3067` | `AI 회의 어시스턴트` | `회의 어시스턴트` |
| `spa.js:3070` | `AI가 답변합니다` | `질문에 답변해드립니다` |
| `spa.js:3113` | `AI가 답변을 생성하고 있습니다` | `답변을 생성하고 있습니다` |
| `spa.js:3149` | `AI Chat — 회의록` | `채팅 — 회의록` |
| `spa.js:3343` | `AI 모델 응답 불가` | `응답을 받지 못했습니다` |
| `spa.js:3533` | `AI 엔진이 아직 준비되지 않았` | `엔진이 아직 준비되지 않았` |
| `spa.js:4608` | `AI 채팅에 사용해요` | `채팅에 사용해요` |
| `spa.js:5052` | `AI가 자동으로 교정` | `자동으로 교정` |

> **유지 (주석/식별자)**: `// === ChatView (AI 채팅) ===` 등 코드 내부 주석은 개발자 컨텍스트로 유지.

**(c) TDD 시나리오**
- **Red**: `grep -n "AI" ui/web/spa.js ui/web/index.html` → 현재 14건+
- **Green**:
  - 사용자 노출 문자열(따옴표 안)에서 "AI" 0건
  - 단, 코드 주석(`//`로 시작)은 제외
  - 검증 스크립트: `rg '"[^"]*AI[^"]*"' ui/web/spa.js ui/web/index.html` → 0건
- **회귀 방지**: CI 가드 — 위 grep 패턴이 0건 아니면 빌드 실패

**(d) Before/After 검증 기준**
- **시각**: Nav 버튼 스크린리더 라벨, 채팅 환영 화면, 요약 탭 라벨 모두 "AI" 미노출
- **기능**: `document.title`, `aria-label`, 다국어 호환성 유지
- **접근성**: aria-label 의미 손실 없음 (QA 스크린리더 테스트 필수)

---

### 작업 6 — Progressive Disclosure (빈 목록 시 검색바/정렬 숨김) (WS-2)

**(a) 현재 위치**
- `index.html:63-72` — `.list-search`, `.list-sort` 항상 노출
- `spa.js:~340` — `Sidebar` 모듈의 `_searchEl`, `_sortEl` 초기화

**(b) 변경 내용**
1. `spa.js`의 `Sidebar.render()` 또는 목록 갱신 함수에서 `meetings.length === 0`이면 `.list-search`, `.list-sort` 에 `hidden` 속성 또는 `.is-empty` 클래스 토글.
2. `style.css`에 `#list-panel.is-empty .list-search, #list-panel.is-empty .list-sort { display: none; }` 추가.
3. 목록이 다시 1건 이상이 되면 복원.

**(c) TDD 시나리오**
- **Red**:
  - Playwright: `meetings = []` 상태에서 `.list-search input` `display != "none"` → 실패해야 함
- **Green**:
  - 빈 목록: `.list-search`, `.list-sort` `display === "none"`
  - 1건 이상: `display !== "none"`
- **회귀 방지**: 검색어 입력 중 마지막 결과가 빈 배열이 되어도 **검색 입력창은 유지** (사용자가 지우고 다시 찾을 수 있도록). → 즉 "원본 meetings가 0건"일 때만 숨김, "필터 결과가 0건"일 땐 유지. 이 분기 테스트 포함.

**(d) Before/After 검증 기준**
- **시각**: 최초 설치 직후 빈 리스트 화면이 훨씬 깨끗 (타이틀 + 빈 상태 일러스트만)
- **기능**: 회의 추가 → 검색바 페이드인
- **접근성**: `hidden` 속성 사용 시 스크린리더도 자동 제외

---

### 작업 7 — Nav-bar 및 주요 액션에 kbd 단축키 힌트 추가 (WS-2)

**(a) 현재 위치**
- `index.html:12-55` — nav-bar 버튼 4개 (`navHome`, `navSearch`, `navChat`, `navSettings`)
- `spa.js` 내 주요 액션 버튼 (녹음 시작, 검색 열기 등)

**(b) 변경 내용**
1. nav-bar 버튼 툴팁(`data-tooltip`) 또는 aria 힌트에 단축키 병기.
2. Command Palette 도입 전 **사전 작업**이므로 실제 글로벌 단축키는 이 작업에서 등록하지 **않음** (작업 8·9에서 처리). 이 작업은 **시각 힌트만** 제공.
3. 예: 녹음 시작 버튼 옆에 `<kbd>⌘R</kbd>` 렌더. (호버 시 `data-tooltip`으로도 표시)

**범위 제한**:
- Nav 버튼 4개에 툴팁으로만 힌트 표시 (`data-tooltip="회의록 (⌘1)"` 형태)
- "녹음 시작" 주요 액션 1개에 `<kbd>` 인라인 표시

**(c) TDD 시나리오**
- **Red**: `data-tooltip` 속성 내 "⌘" 문자 0건
- **Green**: nav-bar 내 `data-tooltip`에 "⌘" 문자 ≥4건, 주요 액션 버튼 내 `<kbd>` 요소 ≥1건
- **회귀 방지**: 툴팁 pseudo-element가 다른 요소를 가리지 않는지 z-index 체크

**(d) Before/After 검증 기준**
- **시각**: nav 버튼 hover 시 "회의록 ⌘1" 툴팁 표시
- **기능**: (이 작업 단독으로 단축키 실제 동작은 **안 함**. 힌트만.)
- **접근성**: `<kbd>`는 semantic 요소라 SR이 "키보드 입력"으로 읽음

---

### 작업 8 — ⌘, (콤마) 키바인딩으로 설정 페이지 이동 (WS-2)

**(a) 현재 위치**
`spa.js` 내 글로벌 `keydown` 핸들러 (없으면 신규 추가)

**(b) 변경 내용**
```js
document.addEventListener("keydown", function (ev) {
  if ((ev.metaKey || ev.ctrlKey) && ev.key === ",") {
    ev.preventDefault();
    Router.navigate("/app/settings");
  }
});
```
- input/textarea 포커스 중에도 동작해야 하는지는 **macOS 관례에 따라 동작** (Spotlight/Preferences는 input 내에서도 동작).
- Router는 기존 SPA 라우터 재사용.

**(c) TDD 시나리오**
- **Red**: Playwright에서 `⌘,` 눌러도 경로 불변
- **Green**: `⌘,` 입력 후 `location.pathname === "/app/settings"`
- **회귀 방지**:
  - input/textarea 내에서도 동작 확인
  - 입력 폼의 콤마 문자 입력은 방해받지 않아야 함 (meta/ctrl 없는 경우)

**(d) Before/After 검증 기준**
- **시각**: 설정 페이지로 즉시 전환
- **기능**: 다른 단축키(⌘K, ⌘R 등)와 충돌 없음
- **접근성**: 힌트 제공 위치 — 작업 7과 통합하여 설정 버튼 툴팁에 `⌘,` 표시

---

### 작업 9 — Command Palette (⌘K) 신규 구현 (WS-3)

**(a) 현재 위치**
신규 모듈. 제안 경로: `ui/web/spa.js` 하단에 `CommandPalette` IIFE 모듈 추가, 또는 (선호) `ui/web/command-palette.js` 별도 파일 생성 후 `index.html`에서 `<script>` 로드.

> **결정**: 이 이터레이션에선 **spa.js 내 모듈로 통합** (파일 분리는 2차). 단일 파일 변경으로 충돌 최소화.

**(b) 변경 내용**

**9-1. HTML 마크업 (spa.js의 `CommandPalette.render()`가 동적 생성)**
```html
<div class="cmdk-overlay hidden" id="cmdkOverlay" role="dialog" aria-modal="true" aria-label="명령어 팔레트">
  <div class="cmdk-panel">
    <div class="cmdk-search">
      <svg>...</svg>
      <input type="text" id="cmdkInput" placeholder="무엇을 도와드릴까요?" autofocus>
    </div>
    <div class="cmdk-results" id="cmdkResults" role="listbox">
      <!-- 카테고리별 그룹 렌더링 -->
    </div>
    <div class="cmdk-footer">
      <kbd>↑↓</kbd> 이동 <kbd>↵</kbd> 선택 <kbd>esc</kbd> 닫기
    </div>
  </div>
</div>
```

**9-2. 명령 카탈로그**
```js
var COMMANDS = [
  { category: "회의", id: "list-home", title: "회의 목록 열기", shortcut: "⌘1", run: function(){ Router.navigate("/app"); } },
  { category: "회의", id: "list-search", title: "회의 검색", shortcut: "⌘F", run: function(){ /* 검색 포커스 */ } },
  { category: "액션", id: "record-start", title: "새 녹음 시작", shortcut: "⌘R", run: function(){ /* ... */ } },
  { category: "액션", id: "settings", title: "설정 열기", shortcut: "⌘,", run: function(){ Router.navigate("/app/settings"); } },
  { category: "채팅", id: "chat-open", title: "채팅 열기", shortcut: "⌘2", run: function(){ Router.navigate("/app/chat"); } },
  // 동적: 최근 회의 Top 5
];
```

**9-3. 기능 요구**
- ⌘K로 열림, ESC로 닫힘
- 텍스트 입력 → Fuzzy 매칭 (간단: substring + 첫 글자 가중치)
- ↑↓ 화살표로 선택 이동 (활성 항목 `aria-selected="true"`)
- Enter → `run()` 실행 + 닫힘
- 카테고리별 그룹 헤더
- 최근 사용 명령 `localStorage` 저장 → 검색어 비었을 때 상단 표시
- 동적 항목: 최근 회의 5개 (meetings state에서 slice)

**9-4. CSS (style.css 신규 섹션)**
```css
.cmdk-overlay {
  position: fixed; inset: 0; z-index: 1000;
  background: rgba(0,0,0,0.3);
  backdrop-filter: blur(8px) saturate(180%);
  -webkit-backdrop-filter: blur(8px) saturate(180%);
  display: flex; align-items: flex-start; justify-content: center;
  padding-top: 15vh;
}
.cmdk-overlay.hidden { display: none; }
.cmdk-panel {
  width: 640px; max-width: 90vw;
  background: var(--bg-card);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
  display: flex; flex-direction: column;
  max-height: 60vh;
}
.cmdk-search { /* ... */ }
.cmdk-results { overflow-y: auto; padding: 4px 0; }
.cmdk-group-header { /* 카테고리 라벨 */ }
.cmdk-item { display: flex; padding: 8px 16px; cursor: pointer; }
.cmdk-item[aria-selected="true"] { background: var(--bg-active); }
.cmdk-footer { /* 하단 힌트 */ }
```

**9-5. 접근성**
- `role="dialog"`, `aria-modal="true"`, focus trap
- 열릴 때 input에 포커스, 닫힐 때 이전 포커스 복원
- 결과 리스트는 `role="listbox"`, 각 항목 `role="option"`, `aria-selected` 토글
- `prefers-reduced-motion`: 페이드/슬라이드 애니메이션 비활성화

**(c) TDD 시나리오**
- **Red**:
  - `#cmdkOverlay` 요소 없음
  - `⌘K` 눌러도 아무 변화 없음
- **Green** (Playwright 시나리오):
  1. `⌘K` → overlay 표시, input 포커스
  2. "설정" 입력 → 결과에 "설정 열기" 항목 표시
  3. Enter → 라우터가 `/app/settings`로 이동 + overlay 닫힘
  4. `⌘K` 재오픈 → 검색어 비었는데 "최근: 설정 열기" 상단 표시
  5. ESC → overlay 닫힘
  6. 포커스가 이전 트리거 요소로 복원
- **회귀 방지**:
  - input 포커스 중 `⌘K`도 동작 (예: 회의 검색창에서도)
  - 2번 연속 `⌘K` 눌러도 중복 열림 없음 (이미 열려 있으면 input만 재포커스)

**(d) Before/After 검증 기준**
- **시각**: Linear/Raycast 스타일의 중앙 상단 팔레트. blur 배경.
- **기능**: 10개 이상 명령 등록, Fuzzy 매칭 정상, 최근 명령 저장
- **접근성**: SR로 "dialog, 명령어 팔레트" 읽힘, 옵션 화살표 이동 시 각 항목 읽힘
- **성능**: 열고 닫기 < 100ms

---

## 4. 워크스트림 분할 및 의존성

### 4.1 워크스트림

| WS | 담당 | 파일 | 작업 | 예상 변경 라인 |
|---|---|---|---|---|
| **WS-1** | Frontend 1 (style.css 전담) | `style.css` only | 작업 1,2,3,4 | +80 / -60 |
| **WS-2** | Frontend 2 (spa.js/index.html 텍스트·UX) | `spa.js` + `index.html` | 작업 5,6,7,8 | +120 / -30 |
| **WS-3** | Frontend 3 (신규 모듈) | `spa.js` + `style.css` (Palette 섹션) | 작업 9 | +400 / -0 |

### 4.2 파일 충돌 방지 규칙

**핵심 충돌 지점**: WS-1과 WS-3이 둘 다 `style.css`에 써야 함.

**규칙**:
1. **WS-1 먼저 merge** (토큰 및 기본 컴포넌트가 WS-3의 전제조건)
2. WS-3는 `style.css` **맨 하단에 "/* ======= Command Palette ======= */" 마커** 붙이고 그 아래에만 추가 (상단 토큰 영역 건드리지 않음)
3. WS-2와 WS-3는 둘 다 `spa.js` 수정 → **별도 섹션**으로 분리:
   - WS-2: 기존 모듈(`Sidebar`, `ChatView` 등) 내부 문자열·렌더 로직 수정
   - WS-3: 파일 맨 하단에 `CommandPalette` IIFE 모듈 신규 추가
   - 글로벌 `keydown` 핸들러(작업 8·9): **작업 9 담당자(WS-3)가 통합 핸들러 작성**, 작업 8은 WS-3에 의존

### 4.3 의존성 다이어그램

```
WS-1 (style.css 토큰/컴포넌트)
  │
  ├─ 작업 1 (easing 토큰) ────────┐
  ├─ 작업 2 (hairline) ───────────┤
  ├─ 작업 3 (modal blur) ─────────┤
  └─ 작업 4 (kbd CSS) ────────────┤
                                    │ (토큰/kbd 필요)
                                    ▼
WS-2 (spa.js/index.html 텍스트·UX)  │
  │                                 │
  ├─ 작업 5 (Hidden AI) ── 독립     │
  ├─ 작업 6 (Progressive) ── 독립   │
  ├─ 작업 7 (kbd 힌트) ─── 작업 4에 의존 ✓
  └─ 작업 8 (⌘, 바인딩) ── 작업 9에 흡수됨 ↓
                                    │
                                    ▼
WS-3 (Command Palette)              │
  └─ 작업 9 ── 작업 1,4에 의존 ✓
             ── 작업 8 통합 키핸들러 소유
```

**머지 순서 (권장)**:
1. `WS-1` (PR 1) — style.css 토큰 · hairline · modal blur · kbd
2. `WS-2` (PR 2) — spa.js/index.html Hidden AI · Progressive · kbd 힌트 · (작업 8 제외)
3. `WS-3` (PR 3) — Command Palette (작업 8 키바인딩 통합)

각 PR은 독립 리베이스 가능하도록 **파일 내 섹션 경계**를 마커로 명시한다.

---

## 5. 위험 분석

| # | 위험 | 영향 | 완화 |
|---|---|---|---|
| R1 | 0.5px hairline이 1x 디스플레이에서 0px로 렌더되어 보더 사라짐 | 시각 회귀 | QA가 1x 시뮬레이션(Chrome DevTools DPR=1)에서 검증, 필요시 `box-shadow: 0 0 0 0.5px var(--border)` fallback |
| R2 | backdrop-filter 성능 저하 (pywebview/WebKit) | 모달 프레임 드롭 | FPS 측정 후 필요시 blur 반경 축소(4px), `will-change` 활용 |
| R3 | `⌘K` 단축키가 브라우저/WebKit 기본 동작(주소창 검색)과 충돌 | 기능 미동작 | `preventDefault()` 필수. pywebview는 주소창 없어 안전 |
| R4 | Hidden AI 교체 중 JSON/정규식 문자열 실수로 교체 | 런타임 오류 | 사람이 수동 목록 기반 Edit, 자동 sed 금지. PR diff 리뷰 필수 |
| R5 | WS-1 머지 지연 시 WS-3 블로킹 | 일정 지연 | WS-3는 로컬에서 WS-1 브랜치 위에 개발 시작 가능 (WS-1 PR 오픈 즉시) |
| R6 | 글로벌 keydown 핸들러 중복 등록 | 이벤트 2회 발생 | WS-3가 단일 핸들러 소유, WS-2는 글로벌 키 등록 금지 |
| R7 | Hidden AI 교체 후 기존 스크린리더 사용자 혼란 | 접근성 | CHANGELOG에 명시, aria-label만 바뀌고 버튼 위치/기능 동일하므로 저위험 |
| R8 | Progressive Disclosure가 "필터 결과 0건"에서도 잘못 동작 | 기능 회귀 | "원본 meetings.length === 0" 분기만 숨김 (§3 작업 6 회귀 방지 참고) |
| R9 | `style.css` 4,935줄 수정 중 셀렉터 우선순위 꼬임 | 시각 회귀 | 기존 셀렉터는 건드리지 않고 **신규 블록 추가** 우선. 교체는 변수값만. |

---

## 6. QA 검증 매트릭스 (시각 × 기능 × 접근성)

각 작업마다 3축 검증. ✓ = 필수, ○ = 권장.

| # | 작업 | 시각 회귀 | 시각 개선 | 기능 동작 | 성능 | 접근성(SR) | 접근성(키보드) | 다크모드 |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| 1 | macOS easing 토큰 | ✓ | ✓ | ✓ | ○ | - | - | ✓ |
| 2 | 0.5px hairline | ✓ | ✓ | ✓ | - | - | - | ✓ |
| 3 | Modal backdrop blur | ✓ | ✓ | ✓ | ✓ | - | - | ✓ |
| 4 | kbd CSS | - | ✓ | - | - | ○ | - | ✓ |
| 5 | Hidden AI | - | ✓ | ✓ | - | ✓ | - | - |
| 6 | Progressive Disclosure | ✓ | ✓ | ✓ | - | ✓ | - | - |
| 7 | kbd 단축키 힌트 | - | ✓ | ○ | - | ○ | - | ✓ |
| 8 | ⌘, 키바인딩 | - | - | ✓ | - | - | ✓ | - |
| 9 | Command Palette | - | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

### 6.1 QA 역할 분담 제안
- **QA 1**: 시각 회귀 (Playwright screenshot diff) — 작업 1,2,3,6,9
- **QA 2**: 기능/통합 (E2E 시나리오) — 작업 5,6,8,9
- **QA 3**: 접근성 (VoiceOver + 키보드) — 작업 5,6,7,9

### 6.2 공통 검증 환경
- macOS 14+ (Sonoma 이상)
- pywebview 네이티브 창 + Safari/Chrome 브라우저 폴백
- Light/Dark 모드 각각
- Retina(2x) + 1x DPR 시뮬레이션

---

## 7. Definition of Done

### 7.1 작업별 DoD
모든 작업이 다음을 충족해야 "Done":
1. ✅ TDD 시나리오의 Red → Green 전환 증거 (스크린샷 or 테스트 로그)
2. ✅ QA 매트릭스 해당 축 모두 통과
3. ✅ Light/Dark 모드 양쪽 스크린샷 제출
4. ✅ 회귀 방지 테스트(또는 grep 가드) 추가 커밋
5. ✅ PR 설명에 Before/After GIF or 스크린샷

### 7.2 이터레이션 전체 DoD
1. ✅ 3개 PR 모두 main에 머지
2. ✅ `docs/design.md`와의 격차 G1~G7 **0건**
3. ✅ 기존 1,231개 Python 테스트 회귀 0건 (`pytest tests/ -x -q`)
4. ✅ 1페이지 릴리스 노트 작성 (사용자 관점)
5. ✅ 본 계획서(`2026-04-08-design-iteration-plan.md`)에 최종 결과 체크리스트 추가

### 7.3 자동 가드 (CI 스크립트 제안)
```bash
# ui/web/검증_가드.sh — 향후 회귀 방지
set -e
cd "$(dirname "$0")"

# G1: macOS easing 토큰
grep -q "ease-macos" style.css || { echo "G1 실패: --ease-macos 누락"; exit 1; }

# G3: Modal backdrop blur
grep -A2 "\.modal-overlay" style.css | grep -q "backdrop-filter" || { echo "G3 실패"; exit 1; }

# G4: kbd CSS
grep -qE "^kbd\s*\{" style.css || { echo "G4 실패"; exit 1; }

# G5: Hidden AI (사용자 노출 문자열)
if grep -nE '"[^"]*AI[^"]*"' spa.js index.html; then
  echo "G5 실패: 사용자 노출 'AI' 문자열 잔존"; exit 1
fi

# G7: Command Palette
grep -q "cmdk-overlay" spa.js || { echo "G7 실패"; exit 1; }

echo "✅ 모든 디자인 가드 통과"
```

---

## 8. 팀 할당 제안

| 역할 | 인원 | 담당 |
|---|---|---|
| **PM (pm)** | 1 | 본 계획서 유지보수, 블로커 해결, 데일리 동기화 |
| **Designer 1** | 1 | WS-1 시각 리뷰 + Figma 참조 토큰 검증 |
| **Designer 2** | 1 | WS-2 Hidden AI 카피 리뷰 + UX 문구 감수 |
| **Designer 3** | 1 | WS-3 Command Palette 비주얼 스펙 (Figma) + 최종 리뷰 |
| **Frontend 1** | 1 | WS-1 구현 |
| **Frontend 2** | 1 | WS-2 구현 |
| **Frontend 3** | 1 | WS-3 구현 |
| **QA 1,2,3** | 3 | §6.1 참고 |

---

## 9. 체크리스트 (실행 중 갱신)

### WS-1
- [ ] 작업 1: macOS easing 토큰
- [ ] 작업 2: 0.5px hairline
- [ ] 작업 3: Modal backdrop blur
- [ ] 작업 4: kbd CSS

### WS-2
- [ ] 작업 5: Hidden AI
- [ ] 작업 6: Progressive Disclosure
- [ ] 작업 7: kbd 단축키 힌트
- [ ] 작업 8: ⌘, 키바인딩 (WS-3에 통합 예정)

### WS-3
- [ ] 작업 9: Command Palette (⌘K)

### QA
- [ ] 시각 회귀 (QA 1)
- [ ] 기능 E2E (QA 2)
- [ ] 접근성 (QA 3)

### 이터레이션 종료
- [ ] 자동 가드 스크립트 추가
- [ ] 릴리스 노트
- [ ] 본 문서에 Before/After 스크린샷 첨부

---

**작성 완료**: 2026-04-08
**다음 단계**: team-lead 승인 → 워크스트림 킥오프 → 각 PR 개설
