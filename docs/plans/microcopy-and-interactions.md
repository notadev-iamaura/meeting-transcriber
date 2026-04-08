# Microcopy & Interactions Spec (Designer 3 / UX)

> Task #4 — AI 단어 제거, Progressive Disclosure, Modal Blur, Hover Reveal Actions
> 참조: `docs/design.md` §5.1 Hidden AI, §5.3 Progressive Onboarding, §5.4 Micro-Animations Restraint
> 구현 대상: `ui/web/spa.js`, `ui/web/index.html`, `ui/web/style.css`
> 구현 담당: Frontend 2 (WS-2)

---

## 1. "AI" 단어 제거 매핑표 (Hidden AI)

원칙: **"AI"는 인프라이지 마케팅 단어가 아니다.** 사용자 노출 카피에서 제거하되, 문장이 어색해지면 동사/명사만 남긴다. 코드 주석/내부 식별자는 유지.

### 1.1 사용자 노출 텍스트 (변경 필수)

| # | 파일 | 라인 | 현재 텍스트 | 새 텍스트 | 이유 |
|---|------|------|------------|-----------|------|
| 1 | spa.js | 1013 | `"다른 키워드를 사용하거나, AI Chat에서 자연어로 질문해 보세요"` | `"다른 키워드를 사용하거나, 채팅에서 자연어로 질문해 보세요"` | "AI Chat" → "채팅". 맥락상 기능명 참조, nav 라벨과 통일 |
| 2 | spa.js | 1195 | `회의록 (AI 요약)` (탭 라벨) | `요약` | 탭은 이미 "회의록"과 분리되어 있으므로 "요약"만으로 충분. 괄호 군더더기 제거 |
| 3 | spa.js | 1247 | `전사가 완료된 후 아래 버튼을 눌러 AI 요약을 생성할 수 있습니다.` | `전사가 완료된 후 아래 버튼을 눌러 요약을 생성할 수 있습니다.` | "AI" 삭제. 의미 변화 없음 |
| 4 | spa.js | 1856 | `<span>용어집에도 추가 (다음부터 AI 보정에 자동 반영)</span>` | `<span>용어집에도 추가 (다음부터 보정에 자동 반영)</span>` | "AI" 삭제. "보정"만으로 기능 전달됨 |
| 5 | spa.js | 2906 | `"AI 출력으로 덮어쓰여요 (.bak 에 백업). 계속할까요?"` | `"보정 결과로 덮어쓰여요 (.bak 에 백업). 계속할까요?"` | "AI 출력" 이라는 기술적 표현 → 행위 기반 "보정 결과"로 사용자 친화화 |
| 6 | spa.js | 3067 | `<div class="welcome-title">AI 회의 어시스턴트</div>` | `<div class="welcome-title">회의 어시스턴트</div>` | 채팅 Welcome 타이틀. "AI" 제거, 본질인 "회의"를 전면 배치 |
| 7 | spa.js | 3070 | `'관련 회의 내용을 검색하여 AI가 답변합니다.'` | `'관련 회의 내용을 검색해 답변을 드려요.'` | "AI가" 주어 삭제 + 서비스 톤 일치(해요체) |
| 8 | spa.js | 3113 | `'    <span class="typing-text">AI가 답변을 생성하고 있습니다...</span>'` | `'    <span class="typing-text">답변을 생성하고 있어요…</span>'` | 주어 생략, 말줄임표(`…`) 문자로 타이포그래피 개선 |
| 9 | spa.js | 3149 | `document.title = "AI Chat — 회의록";` | `document.title = "채팅 — 회의록";` | 브라우저 탭 제목. nav 라벨과 일치 |
| 10 | spa.js | 3343 | `notice.textContent = "\u26A0 AI 모델 응답 불가: " + data.error_message;` | `notice.textContent = "\u26A0 응답을 받지 못했어요: " + data.error_message;` | "AI 모델"이라는 기술 누출 제거, 사용자 관점 에러 |
| 11 | spa.js | 3533 | `errorBanner.show("AI 엔진이 아직 준비되지 않았습니다. 잠시 후 다시 시도해 주세요.");` | `errorBanner.show("아직 답변 준비가 덜 됐어요. 잠시 후 다시 시도해 주세요.");` | "AI 엔진"이라는 내부 컴포넌트명 제거 |
| 12 | spa.js | 4608 | `desc: "회의록을 검색해 답변하는 AI 채팅에 사용해요."` | `desc: "회의록을 검색해 답변하는 채팅에 사용해요."` | 설정 설명. 기능명만 유지 |
| 13 | spa.js | 5052 | `'자주 잘못 인식되는 이름·전문용어를 추가하면 AI가 자동으로 교정해 드려요.'` | `'자주 잘못 인식되는 이름·전문용어를 추가하면 자동으로 교정해 드려요.'` | "AI가" 주어 삭제. 자동화가 주어로 대체됨 |
| 14 | index.html | 33 | `<button ... id="navChat" aria-label="AI 채팅">` | `<button ... id="navChat" aria-label="채팅">` | 좌측 nav 버튼 스크린리더 라벨. §5.1 핵심 예시 |

### 1.2 코드 주석 (변경 선택/권장 보류)

| # | 파일 | 라인 | 텍스트 | 처리 |
|---|------|------|--------|------|
| C1 | spa.js | 3020 | `// === ChatView (AI 채팅) ===` | **유지** — 개발자용, 사용자 미노출 |
| C2 | spa.js | 3024 | `* AI 채팅 뷰: RAG 기반 질문/답변, 참조 카드, 세션 관리.` | **유지** — docstring, 기술 맥락 전달에 "AI" 유익 |
| C3 | spa.js | 3313 | `* AI 답변 메시지를 추가한다.` | **유지** |
| C4 | spa.js | 3525 | `// AI 답변 표시` | **유지** |
| C5 | spa.js | 3549 | `* 진행 중인 AI 응답 요청을 취소한다.` | **유지** |
| C6 | index.html | 32 | `<!-- AI 채팅 버튼 -->` | **유지** (HTML 주석, 렌더 X) |

근거: 코드 주석은 개발자 인지 모델을 명확히 한다. "채팅" 단독으로는 일반 메신저와 혼동될 수 있으므로, 소스코드 내부에서는 "AI 채팅/답변"이 가독성에 유리하다.

### 1.3 요약

- **변경**: 14건 (사용자 노출 텍스트 전부)
- **유지**: 6건 (코드 주석·HTML 주석)
- **범위 확장 사유**: PM 요구 8건 초과 발견(welcome, typing, error, title 등). 모두 최종 사용자가 읽게 되는 DOM/타이틀이므로 누락 시 §5.1 불완전. Frontend 2에게 **14건 전부 적용**을 요청.

---

## 2. Progressive UI Disclosure — 빈 상태 처리

목적: 회의가 하나도 없을 때 대시보드의 **검색/정렬/카운트 UI가 사용자를 압도**하지 않도록 숨긴다.

### 2.1 조건

```js
const isEmpty = meetings.length === 0;
```

### 2.2 요소별 동작

| 요소 | selector (현재) | 비어있을 때 | 있을 때 |
|------|----------------|------------|---------|
| 검색바 | `#listSearch` / `.list-search-wrap` | **숨김** (`display: none`) | 표시 |
| 정렬 셀렉트 | `#listSort` / `.list-sort-wrap` | **숨김** | 표시 |
| 목록 카운트 | `#listCount` | **숨김** | 표시 (예: `3개`) |
| 빈 상태 블록 | `.list-empty` | **확장 강조** (최대 높이, 중앙 정렬) | 숨김 |

### 2.3 빈 상태 카피

```
제목: 첫 회의를 추가해 보세요
설명: 오디오 파일을 드래그하거나 아래 버튼으로 시작할 수 있어요.
CTA : [ 파일 추가 ]  [ 폴더 열기 ]
```

- 제목: 명령형 → 행동 유도
- 설명: "AI" 언급 금지(§5.1)
- CTA 2개: 첫 CTA는 primary, 두 번째는 secondary
- 아이콘: 공백이 아닌 일러스트 대신 **굵은 원형 아웃라인 `+`** (hairline 1px, design token 활용)

### 2.4 전환 트리거

회의 1건이 추가되는 순간(`meetings.length === 0 → 1`):
1. 검색/정렬/카운트 페이드 인 (`opacity 0→1`, `duration 180ms`, ease-out)
2. 빈 상태 블록 페이드 아웃 (`opacity 1→0`, `duration 120ms`)
3. `prefers-reduced-motion: reduce` 시 즉시 전환(애니메이션 생략)

### 2.5 구현 가이드 (Frontend 2 참고)

- `ListPanel.render()` 내부에서 `isEmpty` 플래그로 조건부 렌더
- CSS 클래스 토글: `.list-panel[data-empty="true"]` → 하위 검색/정렬 숨김
- 카운트는 `hidden` 속성 사용 (접근성 우호)

---

## 3. 모달 Backdrop Blur 강도 결정

§5.4(Micro-Animations Restraint) 기준: **"작업 인지에 필요한 최소한"**.

### 3.1 비교 테이블

| 강도 | 시각 효과 | 장단점 | 적합도 |
|------|----------|--------|--------|
| 4px | 거의 선명 | 모달 분리감 약함, 배경 노이즈 잔존 | ✗ |
| **8px** | macOS 시스템 모달과 동일 | 분리감+맥락 유지의 균형, Apple HIG 관행 | ✓ **채택** |
| 12px | 배경 콘텐츠 인식 어려움 | 집중도↑, 그러나 맥락 단절 느낌 | △ |
| 16px | 불투명에 가까움 | 성능 부담, overlay 색상으로 대체 가능 | ✗ |

### 3.2 결정: **8px**

```css
/* style.css */
.modal-backdrop {
  backdrop-filter: blur(8px) saturate(180%);
  -webkit-backdrop-filter: blur(8px) saturate(180%);
  background: rgba(0, 0, 0, 0.28); /* light */
}

@media (prefers-color-scheme: dark) {
  .modal-backdrop {
    background: rgba(0, 0, 0, 0.48); /* dark */
  }
}
```

- **blur 값은 light/dark 동일(8px)**. 배경 `rgba`만 다크에서 더 진하게(0.28 → 0.48).
- `saturate(180%)`는 macOS 네이티브 감성(색 과포화)을 추가하는 Apple 관례.
- 성능: 8px는 Safari/Chrome 모두 60fps 유지 안전선.

### 3.3 Fallback

```css
@supports not (backdrop-filter: blur(8px)) {
  .modal-backdrop {
    background: rgba(0, 0, 0, 0.55);
  }
}
```

blur 미지원 브라우저(구 Electron 등)는 불투명도를 올려 분리감 확보.

### 3.4 reduced-motion 처리

`prefers-reduced-motion: reduce` 시 blur는 유지(정적 속성이므로 움직임 아님), 단 모달 열릴 때의 scale/fade 트랜지션은 제거.

---

## 4. Hover Reveal Actions (선택 / Frontend 2 여유 시)

현재 회의 카드의 액션 버튼(삭제, 재처리 등)이 **항상 표시**되어 목록이 시각적으로 무거움. Notion/Linear 패턴을 따라 **호버 시에만 노출**.

### 4.1 대상

- `.meeting-card .meeting-actions` (리스트 카드 우측 액션 그룹)
- 대상 액션: `더보기(…)`, `삭제`, `즐겨찾기` 등

### 4.2 동작 spec

| 상태 | opacity | pointer-events | 비고 |
|------|---------|----------------|------|
| 기본 | 0 | none | 공간은 유지(layout shift 방지) |
| 카드 hover | 1 | auto | 180ms ease-out |
| 카드 focus-within | 1 | auto | **키보드 사용자 접근성 필수** |
| 터치 디바이스 | 1 | auto | `@media (hover: none)` 시 항상 표시 |

### 4.3 CSS 예시

```css
.meeting-actions {
  opacity: 0;
  pointer-events: none;
  transition: opacity 180ms var(--ease-out, ease-out);
}

.meeting-card:hover .meeting-actions,
.meeting-card:focus-within .meeting-actions {
  opacity: 1;
  pointer-events: auto;
}

@media (hover: none) {
  .meeting-actions {
    opacity: 1;
    pointer-events: auto;
  }
}

@media (prefers-reduced-motion: reduce) {
  .meeting-actions { transition: none; }
}
```

### 4.4 접근성 체크리스트

- [x] `:focus-within`으로 키보드 Tab 탐색 시 노출
- [x] 터치 디바이스(`hover: none`)에서 항상 표시
- [x] `pointer-events: none`으로 마우스 오클루전 방지(hover 영역만 차지하지 않도록)
- [x] 스크린리더는 opacity와 무관하게 읽음(DOM 유지)

### 4.5 우선순위

**우선순위 낮음**. WS-2 내 §1(AI 카피)·§2(Progressive Disclosure) 완료 후 시간 여유 있을 때만. QA-3(접근성)에서 focus-within 검증 필수.

---

## 5. Frontend 2 (WS-2) 작업 체크리스트

- [ ] §1.1의 14개 카피 모두 교체 (spa.js 13건 + index.html 1건)
- [ ] §1.2 코드 주석 6건은 변경하지 않음
- [ ] §2 Progressive Disclosure: `ListPanel` 빈 상태 조건부 렌더 + CSS `[data-empty="true"]`
- [ ] §2.3 빈 상태 카피 적용 (명령형 제목 + CTA 2개)
- [ ] §3 모달 backdrop blur 8px 적용은 WS-1(style.css) 범위지만 모달 마크업 정합성 확인
- [ ] §4 Hover Reveal Actions는 시간 여유 시 적용 (접근성 focus-within/hover:none 필수)
- [ ] QA-3(#10) 전달 전 `prefers-reduced-motion`·키보드 탐색 로컬 검증

---

## 6. QA-3 (접근성) 검증 포인트

1. nav `aria-label="채팅"` 스크린리더 낭독 확인 (§1.1 #14)
2. 빈 상태 전환 시 `aria-live` 영역 유지 (§2 Progressive Disclosure)
3. 모달 open 시 포커스 트랩, Esc 닫기 (§3 무관하지만 동시 검증)
4. hover reveal actions는 Tab 키로 접근 가능해야 함 (§4 focus-within)
5. `prefers-reduced-motion: reduce` 활성화 후 모든 트랜지션 제거 확인

---

## 7. 변경 이력

- 2026-04-05: 초안 작성 (designer-ux, Task #4)
- PM(#1) 계획서가 아직 pending 상태이나, 본 spec은 `docs/design.md`를 단일 진실 공급원으로 기반하므로 독립적으로 유효. PM 계획서 확정 후 충돌 시 본 문서 §1 매핑표를 기준으로 재조정.
