# Phase 1B 재검토 (Round 2) — bulk-actions

**티켓**: bulk-actions
**Phase**: 1B Round 2 (Designer-A 수정본 재검토)
**검토 대상**:
- `docs/design-decisions/bulk-actions.md`
- `docs/design-decisions/bulk-actions-mockup.md`
- `docs/design-decisions/bulk-actions-handoff.md`
**원본 검토**: `docs/design-decisions/bulk-actions-review-1b.md` (13개 체크리스트)
**재검토 일자**: 2026-04-29
**검토자**: Designer-B

---

## 0. 종합 판정 — **PASS (13/13 ✅)**

13개 체크리스트 모두 실증 검증 결과 처리 완료. 회귀(regression) 점검 4건 모두 무회귀. 1차 PASS 였던 검토 축 3 (다크 단계 톤) / 검토 축 4 (design.md 적합성) 도 수정 과정에서 깨지지 않았다.

**Phase 2 (QA) 진행 가능.**

---

## 1. 13개 체크리스트 실증 검증

### 즉시 수정 (4건)

#### ✅ [1] 카운트 숫자 색 `var(--accent)` → `var(--accent-text)` 교체
- **bulk-actions.md:158-160**: "**숫자만** `color: var(--accent-text)` + `font-weight: 600` + tabular-nums" + 사유 박스에 라이트 5.57:1 / 다크 6.37:1 / `--accent` 3.95:1 미달 명기
- **bulk-actions-handoff.md:249-254**: `.bulk-action-bar__count-num` 의 `color: var(--accent-text)` 명세 + 토큰 정의 위치 (`style.css:36, 175, 220`) 인용
- **bulk-actions-handoff.md:585**: 토큰 매핑 표에 "카운트 숫자 색 → `var(--accent-text)` (라이트 `#0066CC` 5.57:1 / 다크 `#4DA1FF` 6.37:1)" 명기
- **bulk-actions-mockup.md:53-55**: "color: var(--accent-text) // 본문 5.57:1 (design.md §2.2)" 시각 주석 추가
- **검증**: `style.css:36 --accent-text: #0066CC` / `style.css:175,220 --accent-text: #4DA1FF` 모두 정의 확인

#### ✅ [2] mockup 의 `var(--radius-lg)→8px` 잘못된 화살표 표기 정정
- **bulk-actions-mockup.md:110**: `border-radius: var(--radius-lg)→10px│  // --radius-md 미정의` 로 수정. 10px 가 `--radius-lg` 의 실제 값임을 명시
- **회귀 점검**: 본문에 8px 잔존 검색 → mockup.md 의 라인 110 외에는 모두 정정됨

#### ✅ [3] `--radius-md` 미정의 사실 명기 + 토큰 매핑 표 보강
- **bulk-actions.md:289**: §3.2 표 Radius 행에 "`docs/design.md §2.4` 가 카드/토글 8px 를 권장하지만 `style.css :root` 에는 `--radius-md` 미정의 — 기존 토큰 중 가장 가까운 `--radius-lg` (10px, `style.css:65`) 채택. **신규 토큰 도입 금지** 원칙 준수" 명기
- **bulk-actions.md:374**: §4 토큰 매핑 표에 "`--radius` (6), `--radius-lg` (10) | … **`--radius-md` (8px) 는 design.md 에만 존재, style.css 미정의** — 본 결정에서는 `--radius-lg` 채택" 명기
- **검증**: `style.css` 에 `--radius-md` 검색 → 0건 (기대값 일치). 정의된 radius 토큰은 `--radius` (6px, line 64), `--radius-lg` (10px, line 65), `--radius-pill` (10px, line 149) 뿐

#### ✅ [4] `.bulk-action-btn:hover` background 토큰 결정 (`--bg-hover` 채택)
- **bulk-actions.md:169**: §2.3 표 hover 행에 "**결정**: 행/버튼 단위 hover 는 `--bg-hover` … 인용된 `.home-action-btn:hover` (`style.css:1719`) 가 `--bg-secondary` 를 쓰는 것은 카드형 버튼 컨벤션의 잔재 — frontend-a 가 `--bg-hover` 로 통일하도록 핸드오프 §6 에 명시" 명기
- **bulk-actions-handoff.md:271-274**: `.bulk-action-btn:hover { background: var(--bg-hover); }` 명세 + 결정 사유 박스
- **bulk-actions-handoff.md:620**: §6 갭 표에 "`.home-action-btn:hover` 의 `--bg-secondary` ↔ `.bulk-action-btn:hover` 의 `--bg-hover` … `.home-action-btn` 통일 여부는 별도 결정. 본 티켓 범위 밖" 명기

---

### 명세 보강 (3건)

#### ✅ [5] 체크박스 `:active` 상태 명세
- **bulk-actions.md:33**: §1.1 표에 "`:active` (눌리는 순간, 50ms) | 미세 스케일 다운 + accent 강조 | `transform: scale(0.96)`, `border-color: var(--accent)` … `transition: transform var(--duration-fast) var(--ease-macos)`" 행 추가
- **bulk-actions-handoff.md:175-178**: `.meeting-item-checkbox:active` CSS 명세 추가
- **6 상태 완비**: default / hover / `:active` / checked / disabled / focus-visible 모두 명세됨

#### ✅ [6] reduced-motion `.bulk-action-bar` 처리
- **bulk-actions.md:185**: §2.4 표 마지막 행에 "`prefers-reduced-motion: reduce` | **translateY 제거, opacity 만 페이드** | `transform: none` 강제, `transition: opacity var(--duration-fast) linear`" 명시
- **bulk-actions.md:190-205**: §2.4.1 신규 서브섹션으로 구체 CSS 블록 (`@media (prefers-reduced-motion: reduce) { .bulk-action-bar { transition: opacity var(--duration-fast) linear; transform: none !important; } … }`) 명기
- **bulk-actions-handoff.md:237-242**: `@media (prefers-reduced-motion: reduce) .bulk-action-bar` 블록 + 사유 박스 ("translateY 모션은 제거하되 opacity 페이드는 유지해 시각 신호는 보존")

#### ✅ [7] 키보드 단축키 표 (Cmd+A 사이드바 한정)
- **bulk-actions.md:51-64**: §1.2.1 신규 서브섹션 — Tab / ↑↓ / Space / Enter / Esc / Cmd+A (사이드바 한정) / ←→ (toolbar) 모두 행렬화
- **bulk-actions.md:60**: "`Cmd+A` (macOS) / `Ctrl+A` (기타) | **사이드바에 포커스가 있을 때만** | 현재 렌더된 사이드바 회의 항목 전체 선택. 다른 영역 (콘텐츠, 입력 필드 등) 에서는 브라우저 기본 동작 (텍스트 전체 선택) 유지"
- **bulk-actions.md:63**: 결정 사유 박스에 "macOS Finder/Mail 의 '현재 리스트 전체 선택' 패턴 … `role='listbox'` 또는 그 자손에 포커스가 있을 때만 가로채고 그 외에는 기본 동작 유지" 명기
- **bulk-actions-handoff.md:447-461**: JS 구현 예시까지 동봉 (`isInSidebar` 가드 검증)
- **macOS 컨벤션 점검**: Cmd 표기 ✓, ↑↓ 화살표 표기 ✓, Tab order 정의 ✓ — 모두 macOS HIG 일치

---

### 정책 반영 (4건)

#### ✅ [8] 부분 적합성 정책 (자동 필터링)
- **bulk-actions.md:217-237**: §2.5.1 신규 섹션 — "자동 필터링 정책 (옵션 B) 채택. 액션 버튼 **항상 활성**. 클릭 시 적합한 항목만 자동으로 처리하고 나머지는 skip" + toast 패턴 ("3개 처리, 2개 건너뜀") + 결정 사유 (`docs/design.md §7` 안티 패턴 회피)
- **bulk-actions-handoff.md:475-486**: JS 구현 예시 `executeAction()` 의 `eligible.filter()` + `showToast()` 동봉

#### ✅ [9] 모바일 라벨 정책
- **bulk-actions.md:241-249**: §2.5.2 신규 표 — 데스크톱 ↔ 모바일 (≤640px) 라벨 처리. 액션 버튼은 아이콘만 + `aria-label`, 카운트는 사람 아이콘+숫자, "전사+요약" 글자 잘림 방지 정당화 (375px / 110px / 80px 분석)
- **bulk-actions-mockup.md:222-260**: 변종 3 (Light Mobile) 의 ASCII 다이어그램 + 모바일 적응 규칙 명세
- **bulk-actions-handoff.md:386-399**: `@media (max-width: 640px)` 블록 — `.label-text { display: none; }` + `.bulk-action-btn { flex: 1; min-width: 0; }` (잘림 방지 백업)

#### ✅ [10] 모바일 `<kbd>` 숨김
- **bulk-actions.md:246**: §2.5.2 표에 "단축키 `<kbd>Esc</kbd>` | 데스크톱에서만 표시 | **숨김** … 미디어 쿼리: `@media (max-width: 640px) { .bulk-action-bar__dismiss kbd { display: none; } }`"
- **bulk-actions-handoff.md:394-395**: `.bulk-action-bar__dismiss .label-text, .bulk-action-bar__dismiss kbd { display: none; }` 명세
- **bulk-actions-mockup.md:257-258**: 모바일 변종 본문에서 미디어 쿼리 인용

#### ✅ [11] selection mode 상태 전이 + 새 항목 도착 처리
- **bulk-actions.md:95-118**: §1.5 신규 상태 다이어그램 — OFF/ON 전이 + 7개 트리거 (첫 체크 / 마지막 체크 해제 / Esc / `[✕]` / 액션 실행 후 / 새 회의 도착 / 스크롤 시야 밖) 모두 명세
- **bulk-actions.md:113**: "마지막 항목 체크 해제 (count→0) | 자동 OFF | … **명시적 dismiss 액션 없이도 자동 종료**"
- **bulk-actions.md:116**: "액션 실행 완료 … **전체 해제 + OFF (자동 종료)** … 진행 상태와 결과 카드가 콘텐츠 영역에 표시되므로 selection mode 유지가 시각 잡음"
- **bulk-actions.md:117**: "새 회의 도착 … **mode 유지, 새 항목은 자동 선택 안 함**"
- **bulk-actions-handoff.md:464-486**: JS 구현 예시 (`toggleSelection()`, `executeAction()`) 에 자동 종료 로직 동봉
- **bulk-actions-mockup.md:360-361**: 키보드 시퀀스 t=4.0s 에 "액션 완료 후 자동으로 selection mode 해제 (전체 해제 + 액션 바 slide-up). 진행/결과는 콘텐츠 영역에 표시됨 (bulk-actions.md §1.5 결정)" 명기

---

### 선택적 (2건)

#### ✅ [12] raw 픽셀 사용 정책
- **bulk-actions.md:407-414**: §6.1 신규 — design.md §2.3 ↔ style.css §1-B 의 `--space-2` 정의 불일치 (8px vs 4px) 사유 + 기존 컴포넌트 일관성 (`.home-action-btn` 8px 14px / `.meeting-item` 10px 12px) 인용 + 예외 케이스 (0.5px hairline 등) 명시 + 향후 개선 방향 (T-103 예상)

#### ✅ [13] v2 보류 항목 명시
- **bulk-actions.md:418-427**: §6.2 신규 표 — 일괄 삭제 / 일괄 재처리 / 드롭다운 typeahead / 전체선택 라벨 변환 / `<kbd>` 일람 / 글로벌 Cmd+A 모두 보류 사유와 함께 명기

---

## 2. 회귀 점검 (regression check)

### ⚠ → ✅ 회귀 점검 1: `--accent-text` 가 정말 style.css 에 정의되어 있는가
Designer-A 인용 라인 36/175/220 을 grep 으로 검증:

| 라인 | 정의 | 컨텍스트 | 검증 결과 |
|---|---|---|---|
| `style.css:36` | `--accent-text: #0066CC;` | 라이트 모드 `:root` | ✅ 일치 (5.57:1) |
| `style.css:175` | `--accent-text: #4DA1FF;` | `@media (prefers-color-scheme: dark)` | ✅ 일치 (6.37:1) |
| `style.css:220` | `--accent-text: #4DA1FF;` | `:root[data-theme="dark"]` (수동 다크) | ✅ 일치 (6.37:1) |

추가로 `style.css:7562 color: var(--accent-text);` 에서 토큰이 실사용되고 있어 토큰이 죽은 (dead) 상태가 아님을 확인. **회귀 없음**.

### ✅ 회귀 점검 2: 키보드 단축키 표가 macOS 컨벤션과 일치하는가
- Cmd 표기: bulk-actions.md:60 "`Cmd+A` (macOS) / `Ctrl+A` (기타)" — macOS 우선 + 크로스 플랫폼 fallback ✅
- ↑↓ 화살표: bulk-actions.md:57 "`↑` / `↓` | 사이드바 항목 | 이전/다음 항목으로 포커스 이동" ✅
- Tab order: bulk-actions.md:55 "Tab | 전역 | 다음 인터랙티브 요소로 포커스 이동 (사이드바 → 콘텐츠)" + bulk-actions-mockup.md:351-358 의 키보드 시퀀스로 Tab 흐름 검증 ✅
- ←→ 화살표 (toolbar 내부): bulk-actions.md:61 "WAI-ARIA toolbar 패턴" 명기 ✅
- Esc / Space / Enter 모두 macOS NSTableView 표준 일치 ✅
- 첫 글자 typeahead 는 §6.2 v2 보류로 명시 — 누락이 아닌 의도된 deferral ✅

### ✅ 회귀 점검 3: 신규 §1.5 / §2.5.1 / §2.5.2 / §6 가 design.md 의 기존 원칙과 충돌하지 않는가

| 신규 섹션 | design.md 원칙 충돌 검토 | 결과 |
|---|---|---|
| §1.5 selection mode 자동 종료 | §0 Restraint (절제) — 자동 종료가 사용자에게 추가 의사결정 부담 X. §1.5 Progressive UI Disclosure — count==0 자동 OFF 가 "콘텐츠 없을 때 컨트롤 숨김" 원칙과 일치 | ✅ 부합 |
| §2.5.1 자동 필터링 정책 | §7 안티 패턴 "모든 액션에 확인 모달 → undo 토스트로 대체" 와 일치. 회색 disabled + 모달 설명 대신 자동 필터링 + toast | ✅ 부합 |
| §2.5.2 모바일 라벨 정책 | §6 A11y "ARIA 라벨" 행 — 시각 라벨 숨김 시 `aria-label` 보존 원칙 일치. 데스크톱/모바일 분기는 §1.4 Keyboard-First 와 충돌 없음 (모바일은 키보드 부재 환경) | ✅ 부합 |
| §6.1 raw 픽셀 정책 | design.md §2.3 토큰 정책의 갭을 솔직히 인정하고 향후 별도 티켓 (T-103) 으로 분리 — 단일 진실 공급원 추적 원칙에 부합 | ✅ 부합 |
| §6.2 v2 보류 | §0 Restraint — 모든 기능을 1차에 담지 않고 단계적 출시. 보류 사유 명시는 디자인 결정문의 모범 패턴 | ✅ 부합 |

**모든 신규 섹션이 design.md 원칙과 일관 또는 강화 방향**. 회귀 없음.

### ✅ 회귀 점검 4: 1차 PASS 였던 축 (검토 축 3 다크 단계 톤 / 검토 축 4 design.md 적합성) 보존 검증

#### 검토 축 3 (다크 단계 톤)
- **bulk-actions-mockup.md:131-165**: 변종 2 (Dark Desktop) 가 `--bg-canvas #1C1C1E` ↔ `--bg-sidebar #2C2C2E` 톤 격차를 명시 그대로 유지. "design.md §1.1 — dark 는 격차를 더 크게" 인용 보존
- **bulk-actions-mockup.md:370**: 시각 검증 체크리스트 "사이드바 톤 격차 | ✓ #FFFFFF ↔ #F5F5F7 (작은 격차) | ✓ #1C1C1E ↔ #2C2C2E (큰 격차)" 유지
- **bulk-actions.md:212**: §2.5 라이트/다크 차이 표에 "다크 모드 톤 격차가 큰 만큼 콘텐츠 영역(#1C1C1E)과 sidebar(#2C2C2E) 사이의 중간 톤으로 자연스럽게 자리잡음" 그대로 보존
- **회귀 없음** ✅

#### 검토 축 4 (design.md 적합성: Vibrancy / Hairline / easing / 안티 패턴)
- **Vibrancy**: bulk-actions.md:140-148 (`rgba(255,255,255,0.72) + blur(20px) saturate(180%)`) + 다크 `rgba(28,28,30,0.72)` 그대로 — design.md §1.2 인용 보존 ✅
- **0.5px Hairline**: bulk-actions.md:142, 168, 171, 288 모두 `0.5px solid var(--border)` 유지. handoff §2.3 에서도 동일 ✅
- **macOS Easing**: bulk-actions.md:183-185 `var(--duration-base) var(--ease-macos)` (250ms cubic-bezier) 그대로 ✅
- **안티 패턴 회피 검증**: 신규 섹션에 1px solid / `transition: all 0.3s ease` / 보라색 그라디언트 / 16px+ radius / 회전/scale 1.2 / 모달 강제 등 패턴 도입된 흔적 grep 결과 0건 ✅
- **회귀 없음** ✅

---

## 3. 핵심 강점 (Designer-A 의 우수 처리 사항)

1. **검토에서 직접 요청되지 않은 보강**: bulk-actions.md:459 "추가 보강 (검토에서 직접 요청되지 않은 명세)" 섹션에 메뉴 hover 흰 텍스트 대비 수치 (라이트 4.04:1 / 다크 3.74:1) 와 AA Normal 미달 사실을 자발적으로 명시. NSMenu 친숙도 우선 결정 사유 + font-weight 600 권장까지 추가 — 검토 축 2 의 권고 사항이었지만 핵심 즉시수정으로 분류되지 않았음에도 처리됨
2. **JS 코드 예시 동봉**: handoff.md 에 Cmd+A 사이드바 가드 / 자동 필터링 / selection mode 자동 종료 로직을 실제 JS 스니펫으로 첨부 — frontend-a 의 디자인 의도 보존을 강제
3. **수정 이력 추적**: bulk-actions.md §7 "Phase 1A 수정 이력 (review-1b 반영)" 섹션 신설로 체크리스트 13개 처리를 1:1 매핑
4. **검증 체크리스트 자가 보강**: bulk-actions.md:387-401 / mockup.md:368-380 / handoff.md:625-641 모두 신규 항목 추가 (대비 수치 / 6 상태 / 상태 전이 / 모바일 / reduced-motion)

---

## 4. 사소한 잔존 사항 (FAIL/추가수정 사유 아님, 정보용)

다음은 PASS 판정에 영향을 주지 않는 코스메틱 잔여 항목으로, frontend-a 의 구현 단계에서 자연스럽게 처리되거나 별도 티켓으로 분리될 사항이다:

- **bulk-actions.md:20**: "8px radius + 0.5px hairline + shadow-lg" 표현이 결정 요약 표에 남아있으나, §3.2 본문에서 "10px (`--radius-lg`)" 채택으로 정정되었고 `--radius-md` 미정의 갭이 §4 / handoff §6 에 명시되어 있어 모순 없음. 향후 `--radius-md` 토큰 추가 시 (T-103 예상) 자동 해소
- **bulk-actions-handoff.md:332-344**: 드롭다운 radius 결정에서 "1. var(--radius-lg) (10px) 2. 인라인 8px" 두 옵션을 frontend-a 결정으로 남긴 부분. bulk-actions.md §3.2 에서는 옵션 1 (`var(--radius-lg)`) 단일 결정으로 확정됨. handoff.md 가 옵션을 보존한 것은 frontend-a 가 토큰 추가를 검토할 때를 위한 안내라 의도 충돌은 아니지만, 둘 중 하나로 강제 통일이 필요하면 향후 보강 가능 — **PASS 판정에는 영향 없음**

---

## 5. 최종 판정

| 항목 | 판정 |
|---|---|
| 1차 검토 13개 체크리스트 처리 | **13/13 ✅** |
| 회귀 점검 (1차 PASS 축 보존) | **무회귀 ✅** |
| 신규 섹션의 design.md 원칙 충돌 | **없음 ✅** |
| WCAG AA 색대비 (카운트 숫자) | **수정 완료 ✅** (5.57:1 / 6.37:1) |
| macOS 키보드 컨벤션 일치 | **일치 ✅** |
| 다크 단계 톤 보존 | **보존 ✅** |
| Vibrancy / Hairline / easing | **보존 ✅** |
| 안티 패턴 회피 | **유지 ✅** |

**최종 판정: PASS — Phase 2 (QA) 진행 가능**

### Phase 2 인계 시 권고 사항
- frontend-a 가 구현 시 handoff §6 의 "디자인 ↔ 구현 핵심 갭 (frontend-a 가 처리해야 할 결정)" 표를 우선 검토
- `--radius-md: 8px` 토큰을 `style.css :root` 에 추가하는 별도 PR 검토 (handoff §6 권고 — 본 티켓 범위 밖)
- `.home-action-btn:hover` 의 `--bg-secondary` ↔ `.bulk-action-btn:hover` 의 `--bg-hover` 통일 여부는 별도 티켓
- 모바일 변종 시각 검증은 Phase 2 QA 가 실제 375px 뷰포트로 확인 필요 (bulk-actions-mockup.md §3 ASCII 다이어그램만으로는 한계)
