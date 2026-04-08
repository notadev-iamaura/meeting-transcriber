# Command Palette (⌘K) UX 스펙

> 작성자: designer-cmdk (인터랙션 디자이너)
> 대상 구현자: frontend-cmdk (Task #7 / WS-3)
> 참조: `docs/design.md` §4.1, Linear/Raycast/Notion
> 작성일: 2026-04-05

---

## 0. 한 줄 요약

⌘K를 누르면 화면 중앙에 **단일 통합 팔레트**가 떠서 — 회의 탐색, 앱 액션 실행, STT 모델 전환, AI 채팅 진입을 **한 입력창**으로 수행한다. 키보드만으로 완전 조작 가능하며, 기존 "⌘K → 검색 페이지 이동" 동작은 **완전히 대체**한다.

---

## 1. 호출 / 닫기 (Lifecycle)

### 1.1 열기

| 트리거 | 동작 | 비고 |
|--------|------|------|
| `⌘K` (macOS) / `Ctrl+K` (기타) | 팔레트 오픈 | `document` 레벨 `keydown` 리스너 |
| 상단바 검색 아이콘 클릭 | 팔레트 오픈 | 기존 검색 엔트리 포인트 통합 |
| 사이드바 "무엇이든 실행..." 힌트 클릭 (선택) | 팔레트 오픈 | Progressive Disclosure |

**편집 컨텍스트 예외 처리 (필수)**:
`e.target`이 `INPUT` / `TEXTAREA` / `contentEditable`인 경우에도 `⌘K`는 가로챈다.
단, 현재 편집 중이던 텍스트는 보존해야 하며 팔레트 닫힘 시 포커스를 원 요소로 복귀시킨다.
(※ 기존 `spa.js:5286`는 편집 중 `⌘K`를 무시했으나, Command Palette는 전역 단축키이므로 **항상 동작**한다. 데이터 손실 방지는 팔레트가 모달이고 텍스트를 건드리지 않기 때문에 해결됨.)

### 1.2 닫기

| 트리거 | 동작 |
|--------|------|
| `Esc` | 닫고 원 포커스 복귀 |
| 오버레이(모달 배경) 클릭 | 닫기 |
| 항목 실행(Enter / 클릭) | 실행 후 자동 닫기 |
| 라우트 변경 | 자동 닫기 |

### 1.3 기존 동작 제거

- `spa.js:5284~5309`의 "⌘K → `/app/search` 이동 + `#searchQuery` 포커스" 로직은 **삭제**한다.
- 기존 `SearchView`는 유지하되, Command Palette가 주 진입점이 된다. `/app/search`는 여전히 딥링크로 접근 가능 (검색 결과 상세 페이지 역할).

---

## 2. UI 구조

### 2.1 레이아웃

```
┌─────────────────────────────────────────────────┐
│                                                 │
│          [오버레이: rgba(0,0,0,0.4)               │
│           + backdrop-filter: blur(8px)]          │
│                                                 │
│     ┌───────────────────────────────────────┐  │  ← 600px 너비
│     │ 🔍  무엇을 도와드릴까요?            ⌘K │  │  ← 검색 입력 + ⌘K kbd 힌트
│     ├───────────────────────────────────────┤  │
│     │ 최근                                   │  │  ← 카테고리 헤더 (11px uppercase)
│     │   🎤 새 녹음 시작              ⌘R     │  │
│     │ ───────────────────────────────────── │  │  ← 0.5px hairline
│     │ 회의 (3)                               │  │
│     │   📄 meeting_20260310_193619     ⏎   │  │  ← 선택됨 (bg-active)
│     │   📄 meeting_20260309_200620          │  │
│     │   📄 meeting_20260308_140221          │  │
│     │ 액션                                    │  │
│     │   🎤 새 녹음 시작              ⌘R     │  │
│     │   ⚙ 설정 열기                 ⌘,     │  │
│     │   🌙 다크 모드 전환                     │  │
│     │   ↻ 목록 새로고침             ⌘R     │  │
│     │ STT 모델                                │  │
│     │   ▶ seastar 활성화                      │  │
│     │   ▶ komixv2 활성화                      │  │
│     │ 채팅                                    │  │
│     │   💬 "{검색어}" 질문하기                │  │  ← 검색어 prefill
│     ├───────────────────────────────────────┤  │
│     │ ↑↓ 이동   ⏎ 실행   esc 닫기             │  │  ← 푸터 힌트
│     └───────────────────────────────────────┘  │
│                                                 │
└─────────────────────────────────────────────────┘
```

### 2.2 치수 & 위치

| 속성 | 값 |
|------|-----|
| 너비 | `600px` (고정), 화면 < 680px 시 `calc(100vw - 40px)` |
| 최대 높이 | `60vh` |
| 세로 위치 | `top: 15vh` (중앙보다 살짝 위 — Raycast 패턴) |
| 수평 위치 | 중앙 정렬 |
| z-index | `1000` |
| 리스트 영역 | `max-height: calc(60vh - 56px - 32px)`, `overflow-y: auto` |
| 입력창 높이 | `56px` |
| 푸터 높이 | `32px` |
| 항목 높이 | `36px` |
| 카테고리 헤더 높이 | `28px` |

### 2.3 시각 스타일

```css
.cmdk-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgba(0, 0, 0, 0.4);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 15vh;
  animation: cmdk-overlay-in 150ms var(--ease-macos);
}

.cmdk-panel {
  width: 600px;
  max-width: calc(100vw - 40px);
  max-height: 60vh;
  background: rgba(255, 255, 255, 0.85);
  backdrop-filter: blur(20px) saturate(180%);
  -webkit-backdrop-filter: blur(20px) saturate(180%);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-lg); /* 10px */
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  animation: cmdk-panel-in 200ms var(--ease-macos);
}

[data-theme="dark"] .cmdk-panel,
@media (prefers-color-scheme: dark) {
  .cmdk-panel {
    background: rgba(28, 28, 30, 0.85);
  }
}

@keyframes cmdk-overlay-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
@keyframes cmdk-panel-in {
  from { opacity: 0; transform: translateY(-8px) scale(0.98); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

.cmdk-input-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: 0 var(--space-4);
  height: 56px;
  border-bottom: 0.5px solid var(--border);
}

.cmdk-input {
  flex: 1;
  border: none;
  background: transparent;
  font-size: 16px; /* 입력창만 예외적으로 크게 — Raycast 패턴 */
  color: var(--text-primary);
  outline: none;
}
.cmdk-input::placeholder { color: var(--text-muted); }

.cmdk-icon-search {
  width: 18px; height: 18px;
  color: var(--text-secondary);
}

.cmdk-list {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-2) 0;
}

.cmdk-group-header {
  padding: var(--space-2) var(--space-4) var(--space-1);
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  user-select: none;
}

.cmdk-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  height: 36px;
  padding: 0 var(--space-4);
  margin: 0 var(--space-2);
  border-radius: var(--radius); /* 6px */
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease-macos);
}

.cmdk-item[aria-selected="true"] {
  background: var(--bg-active);
  box-shadow: inset 0 0 0 0.5px var(--accent);
}

.cmdk-item-icon {
  width: 16px; height: 16px;
  color: var(--text-secondary);
  flex-shrink: 0;
}

.cmdk-item-label {
  flex: 1;
  font-size: 13px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cmdk-item-kbd {
  font-size: 11px;
  color: var(--text-muted);
}

.cmdk-footer {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  height: 32px;
  padding: 0 var(--space-4);
  border-top: 0.5px solid var(--border);
  font-size: 11px;
  color: var(--text-muted);
}

.cmdk-footer kbd {
  display: inline-block;
  padding: 1px 5px;
  font-size: 10px;
  background: var(--bg-input);
  border: 0.5px solid var(--border);
  border-radius: 3px;
  margin-right: 4px;
}

/* reduced motion */
@media (prefers-reduced-motion: reduce) {
  .cmdk-overlay, .cmdk-panel { animation: none; }
}
```

---

## 3. 카테고리 정의

### 3.1 카테고리 순서 (고정)

`빈 검색어` 상태:
1. **최근** — 최근 사용 액션 최대 5개 (LocalStorage)
2. **회의** — 최신 5개
3. **액션** — 전체
4. **STT 모델** — 활성 가능한 모델
5. **채팅** — "채팅 열기" 단일 항목

`검색어 입력` 상태 (점수순 병합 — 단, 카테고리 그룹은 유지):
1. **회의** — fuzzy 매칭 결과 (최대 8개)
2. **액션** — 라벨 매칭 결과
3. **STT 모델** — 매칭 결과
4. **채팅** — 항상 맨 아래에 "{검색어} 질문하기" 표시

### 3.2 카테고리별 항목 스키마

```ts
type CmdkItem = {
  id: string;             // 고유 id (예: "action.new-recording")
  category: "recent" | "meetings" | "actions" | "stt" | "chat";
  icon: string;           // emoji 또는 SVG 이름
  label: string;          // 화면 표시용 (한글)
  keywords?: string[];    // 추가 검색 키워드 (영어 별칭 등)
  kbd?: string;           // 단축키 힌트 (예: "⌘R")
  disabled?: boolean;     // STT 모델 이미 활성 시 true
  subtitle?: string;      // 보조 설명 (회의 날짜 등)
  execute: () => void;    // 실행 핸들러
};
```

### 3.3 카테고리별 상세

#### (A) 회의 `category: "meetings"`
- **데이터 소스**: `GET /api/meetings` (이미 `Sidebar`에서 캐시된 목록 재사용)
- **id**: `meeting.{meeting_id}`
- **icon**: `📄`
- **label**: `meeting_id` (예: `meeting_20260310_193619`)
- **subtitle**: 포맷팅된 날짜 (예: `3월 10일 19:36`)
- **keywords**: `[meeting_id, 날짜 YYYY-MM-DD, 날짜 숫자만]`
- **execute**: `Router.navigate("/app/viewer/{meeting_id}")`
- **검색 매칭**: `meeting_id` 전체 / 날짜 부분 / "20260310" 같은 숫자열
- **표시 수**: 빈 검색어일 때 5개, 검색 중에는 최대 8개

#### (B) 액션 `category: "actions"`
고정 액션 목록 (모든 뷰에서 항상 노출):

| id | icon | label | keywords | kbd | execute |
|----|------|-------|----------|-----|---------|
| `action.new-recording` | 🎤 | 새 녹음 시작 | `record, 녹음, rec` | `⌘R` | `Router.navigate("/app") + startRecording()` |
| `action.stop-recording` | ⏹ | 녹음 중지 | `stop, 중지` | `⌘⇧R` | `stopRecording()` — 녹음 중일 때만 노출 |
| `action.open-settings` | ⚙ | 설정 열기 | `settings, preferences, 환경설정, 설정` | `⌘,` | `Router.navigate("/app/settings")` |
| `action.toggle-theme` | 🌙 | 다크 모드 전환 | `theme, dark, light, 테마` | — | `toggleTheme()` |
| `action.refresh` | ↻ | 목록 새로고침 | `reload, refresh, 새로고침` | `⌘⇧R` | `Sidebar.refresh()` |
| `action.open-home` | 🏠 | 홈으로 이동 | `home, 홈, 대시보드` | — | `Router.navigate("/app")` |

> **동적 노출 규칙**:
> - `action.stop-recording`은 녹음 중일 때만 포함
> - `action.new-recording`은 녹음 중일 때 제외
> - `action.toggle-theme`의 `label`은 현재 테마 반대 상태로 표기 ("다크 모드 전환" / "라이트 모드 전환")

#### (C) STT 모델 `category: "stt"`
- **데이터 소스**: `GET /api/stt/models` (또는 기존 설정 페이지에서 쓰는 엔드포인트 재사용)
- **id**: `stt.{model_name}`
- **icon**: `▶` (비활성) / `●` (활성 — disabled 처리)
- **label**: `{model_name} 활성화` (비활성) / `{model_name} (활성)` (현재 활성)
- **keywords**: `[model_name, "stt", "모델"]`
- **execute**: `POST /api/stt/activate {model: model_name}` → 성공 시 토스트 + 팔레트 닫기
- **disabled**: 현재 활성 모델인 경우 `true` (선택 불가, 회색)

#### (D) 채팅 `category: "chat"`
- **id**: `chat.open`
- **icon**: `💬`
- **label**:
  - 빈 검색어: `채팅으로 질문하기`
  - 검색어 있음: `"{검색어}" 채팅으로 질문하기`
- **keywords**: `[chat, 채팅, 질문, ask, ai]` (단, UI 라벨에는 "AI" 금지 — §5.1 Hidden AI)
- **execute**:
  - 빈 검색어: `Router.navigate("/app/chat")`
  - 검색어 있음: `Router.navigate("/app/chat?prefill=" + encodeURIComponent(query))`
- **항상 표시**: 검색 결과가 0개여도 "채팅으로 질문하기"는 폴백 액션으로 노출

---

## 4. 키보드 조작

### 4.1 키 매핑

| 키 | 동작 | 비고 |
|----|------|------|
| `↓` | 다음 항목 선택 | 헤더 스킵, 리스트 끝에서 맨 위로 순환 |
| `↑` | 이전 항목 선택 | 헤더 스킵, 맨 위에서 맨 아래로 순환 |
| `Enter` | 선택 항목 실행 | disabled 항목이면 무시 |
| `Esc` | 팔레트 닫기 | 검색어 있으면 1차: 검색어 클리어, 2차: 닫기 (선택 — 기본은 즉시 닫기) |
| `Tab` | **사용 안 함** | 브라우저 기본 포커스 이동 방지 (`e.preventDefault()`) |
| `⌘↑` / `⌘↓` | 카테고리 경계로 점프 | (선택 구현 — v1에서는 생략 가능) |
| 그 외 문자 키 | 검색어에 입력 | 자동으로 `.cmdk-input`에 반영 |

### 4.2 선택 인덱스 관리

- **flat index**: 렌더링된 실제 항목(disabled 포함 X) 배열의 인덱스 `selectedIndex`.
- 검색어가 변하면 `selectedIndex = 0`으로 리셋하고, 리스트가 비면 `-1`.
- 선택된 항목은 `aria-selected="true"` + `.cmdk-item[aria-selected="true"]` 스타일.
- `scrollIntoView({ block: "nearest" })`로 가시 영역 유지.

### 4.3 첫 글자 매칭

별도 구현 불필요 — 사용자가 문자를 입력하면 `oninput`에서 fuzzy 검색이 돌면서 자동으로 첫 매치가 상단에 오도록 한다 (§5 정렬 규칙).

---

## 5. Fuzzy 검색 & 랭킹

### 5.1 매칭 대상 필드

- `label` (가중치 × 3)
- `keywords[]` (가중치 × 2)
- `subtitle` (가중치 × 1)
- `meeting_id` 숫자 부분 (회의 카테고리 전용, 가중치 × 2)

### 5.2 점수 공식

```
score =
  (exactMatch ? 100 : 0) +              // 완전 일치
  (startsWith ? 50 : 0) +                // 시작 매치
  (wordBoundary ? 25 : 0) +              // 단어 경계 매치 (공백/언더스코어 뒤)
  (subsequence ? 10 : 0) +               // 부분 문자열 매치
  fuzzyBonus                             // 문자 순서 유지된 매치 (글자당 +2, 연속 매치 +5)

finalScore = score × fieldWeight
```

- `query`와 `label`은 둘 다 **소문자 정규화** 후 비교.
- 한글은 자소 분해 없이 그대로 비교 (v1). 향후 `hangul-js` 도입 가능.
- `exactMatch` / `startsWith`에 해당하면 fuzzy 점수는 건너뛰고 상한 점수 부여.

### 5.3 랭킹 규칙

1. 카테고리 그룹은 **유지**(섞지 않음). 그룹 내에서 점수 내림차순 정렬.
2. 그룹 순서: 회의 → 액션 → STT → 채팅 (고정).
3. 점수 `< 5`는 제외 (노이즈 컷).
4. 단, **채팅**은 점수와 무관하게 항상 노출.

---

## 6. 최근 사용 (LocalStorage)

### 6.1 저장 스키마

- **key**: `"cmdk-recent-actions"`
- **value**: JSON array, 최신이 앞쪽
  ```json
  [
    { "id": "action.new-recording", "at": 1712284800000 },
    { "id": "meeting.meeting_20260310_193619", "at": 1712281200000 }
  ]
  ```
- 최대 20개 보관, 초과 시 오래된 항목부터 제거.

### 6.2 기록 시점

- 항목 `execute()` 직전에 `recordRecent(item.id)` 호출.
- 회의 항목도 기록 (최근 열어본 회의가 최상단에 뜨도록).

### 6.3 표시 규칙

- **빈 검색어일 때**: `최근` 카테고리로 상단에 최대 5개 표시.
  - 유효하지 않은 id(삭제된 회의 등)는 필터링.
  - 현재 녹음 상태와 맞지 않는 액션(예: 녹음 중이 아닌데 "녹음 중지")도 제외.
- **검색어 입력 시**: `최근` 카테고리는 숨김 (검색 결과에 집중).

---

## 7. 접근성 (a11y)

| 요소 | 속성 |
|------|------|
| 오버레이 | `role="presentation"` |
| 패널 | `role="dialog"` `aria-modal="true"` `aria-label="명령 팔레트"` |
| 검색 입력 | `role="combobox"` `aria-expanded="true"` `aria-controls="cmdk-listbox"` `aria-autocomplete="list"` `aria-activedescendant="{선택 항목 id}"` `aria-label="검색 또는 명령 실행"` |
| 리스트 컨테이너 | `id="cmdk-listbox"` `role="listbox"` |
| 카테고리 그룹 | `role="group"` `aria-label="{카테고리명}"` |
| 카테고리 헤더 | `aria-hidden="true"` (장식용) |
| 항목 | `role="option"` `id="cmdk-item-{id}"` `aria-selected="{true|false}"` `aria-disabled="{true|false}"` |
| 푸터 힌트 | `aria-hidden="true"` |

**포커스 관리**:
1. 오픈 시 `document.activeElement`를 `previousFocus`로 저장.
2. 패널 내 `.cmdk-input`에 포커스.
3. 닫힘 시 `previousFocus.focus()` 복귀 (null 체크).
4. `Tab`은 `e.preventDefault()` — 포커스를 패널 밖으로 나가지 못하게 (포커스 트랩).

**reduced motion**: `@media (prefers-reduced-motion: reduce)` 에서 애니메이션 제거.

**컬러 대비**: 선택 항목 `bg-active` + `accent` 보더 조합은 WCAG AA 준수.

**스크린 리더 읽기**: `aria-activedescendant` 사용으로 `↑↓` 이동 시 현재 선택 항목을 SR이 읽음.

---

## 8. 빈 상태 & 엣지 케이스

### 8.1 빈 상태 메시지

| 상황 | 표시 |
|------|------|
| 빈 검색어 + 최근 0개 + 회의 0개 | 액션/STT/채팅만 노출, 상단에 안내 없음 |
| 빈 검색어 + 최근 0개 + 회의 0개 + 모델 로딩 중 | 액션 + "회의가 아직 없습니다. 녹음을 시작해보세요." 인라인 힌트 (선택) |
| 검색어 있음 + 결과 0개 (채팅 제외) | "{query}"와 일치하는 결과가 없습니다.<br>다른 검색어를 시도하거나 아래에서 채팅으로 질문해보세요." |
| 검색어 있음 + 결과 있음 | 평소대로 |

### 8.2 엣지 케이스

| 케이스 | 처리 |
|--------|------|
| API 호출 실패 (`/api/meetings`) | 캐시된 목록 사용, 실패 시 "회의" 카테고리 비움 (팔레트 자체는 동작) |
| STT 모델 API 실패 | "STT 모델" 카테고리 숨김 |
| 팔레트 오픈 중 라우트 변경 | 자동 닫힘 (popstate 리스너) |
| 팔레트 오픈 중 WebSocket 이벤트 | 무시 (다시 열 때 최신 데이터 로드) |
| 매우 빠른 타이핑 | `oninput` 핸들러 내 debounce **없음** (로컬 필터이므로 즉시 반영) |
| `⌘K` 이미 열린 상태에서 `⌘K` 재입력 | 닫기 (toggle) — Raycast 패턴 |
| 여러 모달 스택 | 다른 모달 열림 상태에서는 `⌘K` 무시 (z-index 충돌 방지) |

---

## 9. 상태 머신 (요약)

```
[closed]
   │  ⌘K
   ▼
[opening] ─ animation 150ms ─▶ [open: empty-query]
                                   │
                     타이핑 ──────▶ [open: filtering]
                     Esc/클릭/라우트 변경 ─▶ [closing]
                     Enter ─▶ execute() ─▶ [closing]
                                   │
[closing] ─ animation 150ms ─▶ [closed]
```

---

## 10. 파일 구조 제안 (frontend-cmdk 참고)

> spa.js의 다른 뷰 컨트롤러 패턴을 따른다 (`Sidebar`, `SearchView` 등).

```js
// ui/web/spa.js 내부에 추가
var CommandPalette = (function () {
    var state = {
        isOpen: false,
        query: "",
        items: [],          // 필터링된 flat 리스트 (헤더 제외)
        groups: [],         // [{ category, label, items: [...] }]
        selectedIndex: 0,
        previousFocus: null,
        meetingsCache: [],  // Sidebar에서 공유
        sttModels: [],
        isRecording: false,
    };

    function open() { /* ... */ }
    function close() { /* ... */ }
    function toggle() { /* ... */ }
    function render() { /* ... */ }
    function onInput(e) { /* ... */ }
    function onKeydown(e) { /* ... */ }
    function moveSelection(delta) { /* ... */ }
    function executeSelected() { /* ... */ }
    function fuzzyScore(query, text) { /* ... */ }
    function buildItems(query) { /* ... */ }
    function recordRecent(id) { /* ... */ }
    function getRecent() { /* ... */ }

    return { open, close, toggle, init };
})();

// 초기화 (DOMContentLoaded 후)
CommandPalette.init();
```

**DOM 삽입 위치**: `<body>` 최상위에 `<div id="cmdk-root" hidden>` 추가. 열릴 때 `hidden` 제거 + 내용 렌더.

---

## 11. TDD 시나리오 (QA 참고)

### 단위 (fuzzy)
- `fuzzyScore("new", "새 녹음 시작")` → 0 (한글 label 매칭 X)
- `fuzzyScore("녹음", "새 녹음 시작")` → ≥ 50 (startsWith 단어 경계)
- `fuzzyScore("record", action.keywords=["record"])` → 높은 점수
- `fuzzyScore("0310", "meeting_20260310_193619")` → 부분 매치 점수

### 통합
1. `⌘K` 누르면 `.cmdk-panel`이 DOM에 나타나고 `role="dialog"`를 가진다.
2. `Esc` 누르면 패널이 사라지고 원래 포커스로 복귀한다.
3. 오버레이 클릭으로 닫힌다.
4. 입력 컨텍스트(`<input>`)에서도 `⌘K`가 팔레트를 연다.
5. 타이핑하면 `.cmdk-item` 수가 변한다.
6. `↓` 키 3회 후 `Enter`로 4번째 항목 `execute()`가 호출된다.
7. 회의 항목 실행 시 `Router.navigate("/app/viewer/{id}")`가 호출되고 팔레트가 닫힌다.
8. 빈 검색어 + 이전에 실행한 액션이 "최근" 카테고리 최상단에 나타난다.
9. `action.toggle-theme` 실행 후 `data-theme` 속성이 토글된다.
10. STT 모델 활성화 시 `POST /api/stt/activate` 호출 후 성공 토스트 노출.

### 회귀 방지
- 기존 `⌘K → /app/search 이동` 동작 제거 확인.
- `⌘F`(페이지 내 검색) 등 다른 단축키와 충돌 없음 확인.
- 녹음 중 `⌘R`이 팔레트 내부가 아닌 글로벌 핸들러에서 여전히 작동하는지 확인 (또는 팔레트 통해서만 작동하도록 통일).

---

## 12. Before / After 시각 검증 기준

| 항목 | Before | After |
|------|--------|-------|
| `⌘K` 동작 | `/app/search` 페이지로 이동 | 중앙 팔레트 오픈 |
| 회의 탐색 | 사이드바 스크롤 또는 검색 페이지 이동 | 팔레트에서 즉시 필터 |
| 액션 실행 | 각 버튼/메뉴 클릭 | 팔레트에서 키보드만으로 실행 |
| 다크 모드 전환 | 시스템 설정 | 팔레트 내 "다크 모드 전환" |
| 시각 | — | `backdrop-filter: blur(20px) saturate(180%)`, 반투명 85% |

Playwright 검증:
- `await page.keyboard.press("Meta+k"); await expect(page.getByRole("dialog")).toBeVisible();`
- 스크린샷 비교 (light/dark 각각).

---

## 13. 프론트엔드 구현자에게 전달

### 13.1 필요한 기존 유틸
- `Router.navigate(path)` — 이미 `spa.js`에 존재
- `escapeHtml(str)` — `app.js`에 존재
- `Sidebar.getMeetings()` — 없다면 `window.SPA.Sidebar`에 공용 getter 추가 필요
- 테마 토글 — 기존 `toggleTheme()` 함수 재사용 (없으면 `document.documentElement.setAttribute("data-theme", ...)` 직접 조작)

### 13.2 신규 API 엔드포인트 의존성
- `/api/stt/models` — STT 모델 목록이 없다면, **v1에서는 STT 카테고리 생략**하고 v2에서 추가. 없어도 팔레트는 완성도 있게 동작해야 함.
- 모든 다른 카테고리는 기존 API만으로 구현 가능.

### 13.3 CSS 추가 위치
- `ui/web/style.css` 하단에 `/* === Command Palette === */` 섹션 추가.
- 기존 토큰(`--bg-active`, `--accent`, `--ease-macos` 등)을 그대로 사용.

### 13.4 HTML 스니펫 (index.html `<body>` 끝에 추가)
```html
<div id="cmdk-root" hidden>
  <div class="cmdk-overlay" data-cmdk-overlay>
    <div class="cmdk-panel" role="dialog" aria-modal="true" aria-label="명령 팔레트">
      <div class="cmdk-input-row">
        <svg class="cmdk-icon-search" ...></svg>
        <input class="cmdk-input"
               type="text"
               role="combobox"
               aria-expanded="true"
               aria-controls="cmdk-listbox"
               aria-autocomplete="list"
               aria-label="검색 또는 명령 실행"
               placeholder="무엇을 도와드릴까요?"
               autocomplete="off"
               spellcheck="false" />
      </div>
      <div id="cmdk-listbox" class="cmdk-list" role="listbox"></div>
      <div class="cmdk-footer" aria-hidden="true">
        <span><kbd>↑</kbd><kbd>↓</kbd> 이동</span>
        <span><kbd>⏎</kbd> 실행</span>
        <span><kbd>esc</kbd> 닫기</span>
      </div>
    </div>
  </div>
</div>
```

### 13.5 성능 목표
- 팔레트 오픈 → 첫 페인트: **< 100ms**
- 타이핑 → 필터 결과 반영: **< 16ms** (1 frame, 로컬 필터이므로 쉬움)
- 회의 1000개 기준 fuzzy 검색: **< 10ms**

---

## 14. v2 후속 (스코프 외)

- `⌘↑` / `⌘↓` 카테고리 점프
- 한글 자소 분해 검색 (`ㄴㄱ` → "녹음")
- 회의 내부 전사문 검색 (현재는 회의 메타만)
- Drag-and-drop 파일 업로드 항목
- "최근" 항목에 사용 빈도 가중치 (recency × frequency)
- 팔레트 내부 서브 페이지 (예: "설정 → 오디오 장치 선택")

---

**끝.** — 문의/변경 요청은 team-lead 경유.
