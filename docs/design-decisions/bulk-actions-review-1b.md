# Phase 1B 검토 결과 — bulk-actions (Designer-A 산출물 독립 검토)

**티켓**: bulk-actions
**Phase**: 1B (Designer-B 독립 검토)
**검토 대상**:
- `docs/design-decisions/bulk-actions.md`
- `docs/design-decisions/bulk-actions-mockup.md`
- `docs/design-decisions/bulk-actions-handoff.md`
**검토 기준**: `docs/design.md` + `ui/web/style.css`
**검토 일자**: 2026-04-29

---

## 검토 축 1: 토큰 일관성 — **WARN**

### 1.1 신규 토큰 도입 — **PASS**
산출물 자체에서 신규 CSS 변수를 만들겠다고 선언한 부분은 없음. `bulk-actions.md:280` "신규 토큰 도입 0건" 명시. 기존 `:root` 의 토큰만 재조합. **이 부분은 합격**.

### 1.2 존재하지 않는 토큰을 존재하는 것처럼 인용 — **FAIL → WARN 으로 강등**
세 산출물 모두 `--radius-md` (=8px) 를 사용 가능한 토큰으로 인용했지만, **`ui/web/style.css` 에는 `--radius-md` 가 정의되어 있지 않다** (`style.css:64-65, 149` — 정의된 radius 토큰은 `--radius` 6px, `--radius-lg` 10px, `--radius-pill` 10px 뿐).

| 인용 위치 | 인용한 토큰 | style.css 실제 정의 |
|---|---|---|
| `bulk-actions.md:194` "Radius `8px` (=`--radius-md`)" | `--radius-md` | **존재하지 않음** |
| `bulk-actions-mockup.md:110` "border-radius: var(--radius-lg)→8px" | `--radius-lg` 가 8px 라고 표기 | 실제 `--radius-lg` = **10px** (`style.css:65`) |
| `bulk-actions-handoff.md:300, 506-507, 530` | `--radius-lg` 사용을 권장하면서도 "10px" 와 "권장 8px" 이 혼재 | 동일 |

다행히 핸드오프 §6 (`bulk-actions-handoff.md:528-531`) 에서 "디자인 결정 §3.2 에서 8px 를 명시했으나, style.css 에는 6/10 만 정의" 갭을 직접 지적하고 `var(--radius-lg)` (10px) 사용을 결론지었으므로 **사실관계 모순**은 없음. 다만 `bulk-actions-mockup.md:110` 의 "var(--radius-lg)→8px" 화살표 표기는 **잘못된 정보** (실제 변환은 10px). 수정 필요.

### 1.3 design.md 가 인용한 토큰 vs style.css 실제 정의의 불일치
`docs/design.md §2.4` 가 `--radius-md` (8px) 를 토큰으로 명시하지만 `style.css` 는 그 토큰을 도입하지 않았다. 이는 **design.md ↔ style.css 자체의 갭**이며, 산출물의 잘못은 아니다. 단, 산출물이 design.md 만 보고 "이미 정의된 토큰" 으로 단정한 것은 단일 진실 공급원 추적에서 미흡.

### 1.4 하드코딩된 픽셀 값 — **WARN**
산출물 전반에서 `padding: 6px 12px`, `gap: 8px`, `padding: 4px 8px`, `padding: 0 16px` 같은 raw 픽셀이 다수 등장한다. style.css 의 `--space-N` 토큰 (`style.css:135-146`) 을 사용하지 않음. 단, 기존 컴포넌트 (`.home-action-btn` `padding: 8px 14px` `style.css:1707`, `.meeting-item` `padding: 10px 12px` `style.css:3959`) 도 동일하게 raw 값을 쓰고 있어 프로젝트 일관성 자체는 유지됨. design.md §2.3 의 토큰 스케일과 style.css §1-B 의 토큰 스케일이 **서로 다른 값** 으로 정의되어 있어 (design.md `--space-2`=8px vs style.css `--space-2`=4px), 토큰 사용을 강제하면 오히려 혼란. **현 상태에서는 raw px 가 안전한 선택** — 다만 산출물 어디에도 "왜 토큰 대신 raw px 를 쓰는가" 의 명시가 없는 점이 미흡.

### 1.5 인용 라인 번호 정확도 — **WARN**
`bulk-actions.md:65-67` 가 `style.css:740 .sidebar-item.active { background: rgba(0,122,255,0.12) }` 로 인용 — `style.css:740` 실제 내용 일치 (검증 완료). 다만 본 티켓의 대상은 `.meeting-item` (`style.css:3955+`) 이고 `style.css:3977 .meeting-item.active { background: var(--bg-active) }` 가 더 정확한 인용. `.sidebar-item` 은 별도 컴포넌트. 인용 자체는 틀리지 않았지만 적용 대상이 헷갈리게 표현됨.

`bulk-actions.md:126` 의 `style.css:1718-1721 .home-action-btn:hover` 인용도 검증함. 실제 라인:
```
.home-action-btn:hover:not(:disabled) {
  background: var(--bg-secondary);
  border-color: var(--accent, var(--border));
}
```
산출물 §2.3 표는 `.bulk-action-btn:hover` 의 background 를 `var(--bg-hover)` 로 명세했지만 인용한 `.home-action-btn` 은 `var(--bg-secondary)` 를 쓴다. **두 컴포넌트가 다른 hover 톤을 쓸 충분한 이유** 가 없는데 산출물은 이 차이를 설명하지 않았다. 의도된 차이인지 누락된 차이인지 불명확.

### 수정 권고 (검토 축 1)
- [ ] `bulk-actions-mockup.md:110` 의 `var(--radius-lg)→8px` 를 `var(--radius-lg) → 10px` 로 정정 (또는 `border-radius: 8px` 인라인 채택을 본문에 명기)
- [ ] `bulk-actions.md:194` 의 "`8px` (=`--radius-md`)" 를 `--radius-md` 가 style.css 에 미정의된 사실과 함께 표기 (예: "8px — design.md §2.4 권장값. 본 프로젝트 style.css 에는 `--radius-md` 미정의로 인라인 8px 또는 `--radius-lg` (10px) 중 택일")
- [ ] `bulk-actions.md` §2.3 의 `.bulk-action-btn:hover` 가 `var(--bg-hover)` 를 쓰는지, 인용한 `.home-action-btn:hover` 의 `var(--bg-secondary)` 를 따르는지 명확히 결정
- [ ] (선택) raw 픽셀 사용에 대한 정책 한 줄 추가 — "design.md §2.3 ↔ style.css §1-B 의 space 토큰 정의가 다르므로 raw px 우선" 같은 결정 근거

---

## 검토 축 2: 색 대비 (WCAG AA) — **WARN**

### 2.1 산출물의 자가 검증 — 부분 통과
`bulk-actions-mockup.md:363` 시각 검증 체크리스트가 "WCAG AA (4.5:1)" 항목을 두지만 다음 두 핵심 케이스의 **실제 대비 수치 계산이 빠져 있다**.

### 2.2 검증 누락 케이스 (직접 계산)

**Case A — 라이트 모드, 액션 바의 카운트 숫자 (`var(--accent)` = `#007AFF`) on vibrancy 배경 `rgba(255,255,255,0.72)` (실제 합성색은 vibrancy 가 깔리는 콘텐츠 영역 색에 의존)**
- 가장 보수적 가정: 콘텐츠 영역 흰색 (`#FFFFFF`) 위에 `rgba(255,255,255,0.72)` → 합성 결과 사실상 `#FFFFFF` 에 가까움.
- `#007AFF` on `#FFFFFF` 대비 = **3.95:1 → AA 4.5:1 미달** (이는 design.md §2.2 가 본문 텍스트용 별도 토큰 `--accent-text: #0066CC` (5.57:1) 를 만든 이유와 동일).
- **산출물의 결정**: `.bulk-action-bar__count-num` 의 `color: var(--accent)` (`bulk-actions.md:117`, `bulk-actions-handoff.md:221`).
- **평가**: 카운트 숫자는 본문 텍스트 (작은 폰트 13px) 이므로 `--accent` 가 아닌 `--accent-text` 를 써야 design.md §2.2 의 "본문 텍스트용 5.57:1" 원칙에 부합한다. **불일치**.

**Case B — 다크 모드 동일 케이스**
- `#0A84FF` on vibrancy `rgba(28,28,30,0.72)` over `#1C1C1E` ≈ `#1C1C1E` 가까움.
- design.md §2.2 가 다크 본문 텍스트용 `--accent-text: #4DA1FF` (6.37:1) 를 별도 정의함.
- 산출물은 라이트와 동일한 `var(--accent)` 사용 → 다크에서는 `#0A84FF` on `#1C1C1E` ≈ **5.51:1 → AA 통과** (다크는 acceptable, 라이트가 미달).

**Case C — 드롭다운 메뉴 hover 의 흰 텍스트 on `var(--accent)` 채움**
- `#FFFFFF` on `#007AFF` = **4.04:1**, `#FFFFFF` on `#0A84FF` = **3.74:1** — **둘 다 AA 4.5:1 미달**.
- 단, 14pt 미만의 굵은 글씨 (font-weight ≥600) 또는 18pt 이상이면 AA Large 3:1 적용 → 통과 가능. 산출물 핸드오프 `bulk-actions-handoff.md:325` 메뉴 항목 `font-size: 13px` 로 13px 는 **굵은 글씨가 아니면 일반 텍스트 기준 적용**. font-weight 가 명시되지 않아 모호.
- **macOS NSMenu 하이라이트의 표준 동작** 이 정확히 이 패턴 (accent 채움 + 흰 텍스트) 이라 사용자 친숙도 측면에서는 정당화 가능. 하지만 WCAG AA 자체로는 위반 위험 → 산출물에 대비 수치와 정당화 근거 명시 필요.

### 2.3 selected state 강조색 vs background 구분
- 라이트: `--bg-active` = `rgba(0,122,255,0.12)` on `--bg-sidebar` `#F5F5F7` → 합성 후 매우 옅은 파란빛. 텍스트 `var(--text-primary)` `#1D1D1F` 로 유지하므로 텍스트 대비는 충분 (~14:1).
- 다크: `--bg-active` = `rgba(10,132,255,0.18)` on `--bg-sidebar` `#2C2C2E` → 약간 더 진한 파란빛. 텍스트 `#F5F5F7` 대비 ~13:1.
- 단, **selected 와 hover 의 시각 구분**: 라이트 hover `rgba(0,0,0,0.04)` (회색 톤) vs selected `rgba(0,122,255,0.12)` (파란 톤) → 구분됨. **PASS**.
- 산출물 §1.3 표가 "selected + active (라우팅)" 케이스에서 background 를 동일 `var(--bg-active)` 로 두고 좌측 3px accent 보더로 차별화 — 디자인적으로 합리적. **PASS**.

### 수정 권고 (검토 축 2)
- [ ] **(중요)** `bulk-actions.md:117`, `bulk-actions-handoff.md:221` 의 `.bulk-action-bar__count-num { color: var(--accent) }` 를 **`color: var(--accent-text)`** 로 변경. design.md §2.2 의 "본문 텍스트용 5.57:1" 원칙 준수.
- [ ] `bulk-actions.md:209-210` 의 "본문 텍스트용 `--accent-text` 가 아닌 `--accent` 를 그대로 쓴다 (배경이 흰 텍스트 위 채움이라 대비 OK)" 부분에 **실제 대비 수치 (4.04:1 라이트, 3.74:1 다크) 와 AA Large 적용 조건 (font-weight ≥600 또는 18pt+) 을 명시**. 폰트가 13px 일반 굵기라면 font-weight 를 600 으로 올리거나 메뉴 항목 텍스트색을 `#fff` 가 아닌 더 어두운 톤 검토.
- [ ] 산출물에 "WCAG AA 통과" 라고 단언한 항목들에 실제 대비 수치 표 추가 (라이트/다크 각각).

---

## 검토 축 3: 다크모드 단계 톤 — **PASS**

### 3.1 톤 분리 검증
| 표면 | Light | Dark | 톤 격차 |
|---|---|---|---|
| canvas | `#FFFFFF` | `#1C1C1E` | 명도 자체 반전 |
| sidebar | `#F5F5F7` | `#2C2C2E` | sidebar > canvas (다크는 더 큰 격차) |
| card | `#FFFFFF` | `#2C2C2E` | sidebar 와 동일 톤 (다크) |

`style.css:159-168, 204-213` 에서 다크 정의 검증 — **모두 design.md §1.1 의 "Dark 는 큰 격차" 원칙을 따름**.

### 3.2 산출물의 다크 모드 명세
- `bulk-actions.md:148-155` "다크 모드 톤 격차가 큰 만큼 콘텐츠 영역(#1C1C1E) 과 sidebar(#2C2C2E) 사이의 중간 톤으로 자연스럽게 자리잡음" — **Independent Dark Mode 원칙 정확히 인용**.
- `bulk-actions-mockup.md:148-164` 다크 데스크톱 변종이 sidebar `#2C2C2E` ↔ content `#1C1C1E` 톤 격차를 시각적으로 구분 — **PASS**.
- 액션 바의 vibrancy 배경 `rgba(28,28,30,0.72)` 가 sidebar 와 content 사이의 톤에 자연스럽게 위치 — **PASS**.

### 3.3 다크에서 `--bg-card` (`#2C2C2E`) = `--bg-sidebar` (`#2C2C2E`) 동일값 — 잠재적 시각 동일화
드롭다운 메뉴는 `var(--bg-card)` 를 쓴다. 다크에서 `--bg-card` 와 `--bg-sidebar` 가 **같은 `#2C2C2E`** 이지만, 드롭다운은 항상 `var(--shadow-lg)` (`0 8px 24px rgba(0,0,0,0.4)` 다크) 와 0.5px 보더로 떠 있어 시각적으로 구분됨. **PASS**.

### 수정 권고 (검토 축 3)
**없음**. 다크 단계 톤은 정확히 처리됨.

---

## 검토 축 4: design.md 적합성 (Vibrancy/Hairline/easing/안티 패턴) — **PASS**

### 4.1 Vibrancy 패턴
- `bulk-actions.md:99-107` 의 `backdrop-filter: blur(20px) saturate(180%)` + `rgba(255,255,255,0.72)` / `rgba(28,28,30,0.72)` — **design.md §1.2 (`design.md:39-44`) 정확히 그대로 인용**. **PASS**.
- 핸드오프 `bulk-actions-handoff.md:198-204` 에 `-webkit-backdrop-filter` 까지 함께 적어 Safari 호환성 챙김 — **추가 점수**.

### 4.2 0.5px Hairline
- 모든 보더가 `0.5px solid var(--border)` — design.md §1.3 그대로. **PASS**.

### 4.3 macOS Easing
- `var(--ease-macos) cubic-bezier(0.25, 0.46, 0.45, 0.94)` 와 `var(--duration-fast) 150ms`, `var(--duration-base) 250ms` 사용 — design.md §1.6 그대로. **PASS**.
- `bulk-actions-handoff.md:531` 에서 "요구사항 200ms ease-out 을 250ms ease-macos 로 보정 — 토큰 우선" 결정을 명시한 점 — **올바른 판단**.

### 4.4 안티 패턴 위반 검사 (design.md §7)
| 안티 패턴 | 산출물 사용 여부 |
|---|---|
| 1px solid 보더 | 없음 (모두 0.5px) ✅ |
| `transition: all 0.3s ease` | 없음 (`--ease-macos`, `--duration-base`) ✅ |
| 보라색/네온 그라디언트 | 없음 ✅ |
| 큰 둥근 모서리 (16px+) | 없음 (최대 10px `--radius-lg`) ✅ |
| 화려한 호버 (회전, scale 1.2 등) | 없음 (`scale(0.97)`, `translateY(-8px)` 미세) ✅ |
| 토스트 남발 | 본 티켓 범위 밖 ✅ |
| 모든 액션에 확인 모달 | 산출물이 명시적으로 거부 (`bulk-actions.md:172`) ✅ |
| 다크 모드 = 라이트 색상 반전 | 톤 격차 분리 처리됨 ✅ |
| neumorphism / 과한 그림자 | 없음 (`--shadow-lg` 만 사용, design.md §2.5 정의값) ✅ |
| 200ms 미만의 부자연스러운 트랜지션 | 150ms (드롭다운 열림) 사용 — design.md §1.6 의 `--duration-fast` 토큰 그대로이므로 OK ✅ |

**모든 안티 패턴 회피.** **PASS**.

### 수정 권고 (검토 축 4)
**없음**.

---

## 검토 축 5: 인터랙션 명세 완전성 — **WARN**

### 5.1 체크박스 6 상태 (default/hover/active/checked/disabled/focus)
산출물 §1.1 표 (`bulk-actions.md:28-36`) 검증:
- default ✅
- hover ✅ (selection mode OFF 케이스)
- **active (`:active` 누르는 순간)** ❌ — 명세 없음. 체크박스를 클릭하는 순간 시각 피드백이 어떻게 되는지 (예: 미세 scale 또는 background 변경) 언급 없음.
- checked ✅
- disabled ✅
- focus-visible ✅

핸드오프 `bulk-actions-handoff.md:141-174` 도 `:active` 상태가 빠짐. **6 상태 중 5 상태만 명세** → **WARN**.

### 5.2 컨텍스트 액션 바 reduced-motion 대안
- `bulk-actions.md:142` "prefers-reduced-motion → 즉시 표시/제거" — **PASS**.
- `bulk-actions-mockup.md:365` 도 reduced-motion 0.01ms 명시 — **PASS**.
- 핸드오프 `bulk-actions-handoff.md:201` 의 `.bulk-action-bar` 트랜지션이 정의되어 있지만, **reduced-motion 미디어쿼리 안의 .bulk-action-bar 처리가 명시적으로 빠져 있음** — style.css 의 글로벌 reduced-motion 처리 (`style.css:1550, 4117, 6165` 의 패턴) 를 따른다면 자동 적용되겠지만, 핸드오프에는 명시 필요. **WARN**.

### 5.3 키보드 흐름
- Tab order ✅ (`bulk-actions.md:75`, `bulk-actions-handoff.md:340-350`)
- Esc ✅
- Cmd+A 전체선택 ❌ — **명시 없음**. macOS 표준 (Finder, Mail) 에서 Cmd+A 가 전체선택인데 산출물 어디에도 정의되지 않음. design.md §1.4 "Keyboard-First" 원칙에 비춰 누락. **WARN**.
- ↑↓ 화살표로 항목 이동 ✅
- Space=토글, Enter=뷰어 이동 ✅
- 컨텍스트 액션 바 내부 화살표 키 이동 ✅ (`bulk-actions-mockup.md:347`)
- Shift+클릭 범위 선택 ✅
- Cmd+클릭 토글 추가 ✅

### 5.4 드롭다운 키보드
- Enter/Space 열기, ↑↓ 이동, Esc 닫기, Tab 닫고 다음 컨트롤로 — 모두 명시 ✅.
- 첫 글자 매칭 (메뉴 항목 첫 글자 누르면 해당 항목 포커스) ❌ — macOS NSMenu 표준이지만 산출물 누락. **WARN** (낮은 우선순위, 단 누락 사실은 표기 필요).

### 수정 권고 (검토 축 5)
- [ ] 체크박스 `:active` (눌리는 순간) 상태 명세 추가 — `transform: scale(0.95)` 또는 `background: var(--bg-active)` 같은 미세 피드백
- [ ] 핸드오프 §2.3 `.bulk-action-bar` 항목에 `@media (prefers-reduced-motion: reduce)` 처리 명시 (`transform: none; transition: none;`)
- [ ] `bulk-actions.md` §1.2 또는 §2.6 표에 **`Cmd+A` = 사이드바 전체 선택** 키 단축 추가 (또는 명시적으로 v2 로 보류 표기)
- [ ] (선택) 드롭다운 메뉴 첫 글자 매칭은 v2 로 보류 명시

---

## 검토 축 6: 누락된 엣지 케이스 — **WARN**

### 6.1 0개 / 1개 / 다수 / 모두 선택 시 액션 바 활성/비활성
- 0개 선택: `hidden` (`bulk-actions-handoff.md:70`) ✅
- 1개+ 선택: 표시 + 카운트 갱신 ✅
- **모든 항목 선택 시 액션 바의 시각 변화** ❌ — 명세 없음. 예를 들어 "전체 선택" 상태를 사용자에게 알리는 시각 피드백이 있는지 (예: "전체 N개 선택됨" 라벨로 변경) 정의되지 않음.
- **선택된 항목이 시야 밖** (스크롤로 가려짐) 일 때 동작도 미정의. **WARN**.

### 6.2 부분만 전사 가능한 경우 (혼합 상태)
산출물 어디에도 **선택된 회의 중 일부는 이미 전사 완료, 일부는 전사 가능** 인 경우 액션 버튼의 활성/비활성 정책이 정의되지 않음.
- 옵션 A: [전사] 버튼 disabled (전사 가능 항목이 0개일 때만)
- 옵션 B: [전사] 버튼 활성 (전사 가능 항목만 자동 필터링하여 처리)
- 옵션 C: [전사] 클릭 시 모달/툴팁으로 "X개 항목은 이미 전사 완료, Y개만 처리" 표시

산출물은 **`.bulk-action-btn:disabled` 시각 명세는 있지만 (`bulk-actions.md:128`), 어떤 조건에서 disabled 가 되는지의 정책이 빠짐**. design.md §7 안티 패턴 "모든 액션에 확인 모달" 회피와 일관되려면 옵션 B (자동 필터링) 가 자연스럽지만 산출물은 결정하지 않음. **FAIL → WARN 강등** (디자인 결정문은 정책 결정의 자리. 핸드오프에 "JS 결정사항" 으로 미루기엔 디자인 의도가 깨질 수 있음).

### 6.3 모바일 viewport 좁을 때 컨텍스트 액션 바 처리
`bulk-actions-mockup.md:222-244` 모바일 변종이 2-row stack 으로 적응 — 명세는 있음 ✅.
다만:
- 액션 버튼 3개가 `flex: 1` 균등 분할로 ~33% 씩 (모바일 375px 기준 약 110px). 라벨 "전사+요약" 이 가장 길어 글자 잘림 위험. 산출물은 이 점을 고려하지 않음. **WARN**.
- 컨텍스트 액션 바 height 가 `auto (44 → 88px)` 로 늘어날 때 그 아래 사이드바·콘텐츠의 sticky 처리가 깨지지 않는지 명세 없음. **WARN**.
- `[✕] 해제 ⌘Esc` 의 `<kbd>Esc</kbd>` 가 모바일에서는 의미 없음 (모바일에 키보드 없음). 모바일에서는 `<kbd>` 숨기는 미디어 쿼리 필요. **WARN**.

### 6.4 selection mode 중 새 항목이 사이드바에 추가되는 경우 (라이브 업데이트)
- 새 회의가 도착하면 사이드바 상단에 추가됨 (워처가 폴더 감시).
- selection mode 활성 중 새 항목이 추가되면 그 항목의 체크박스는 즉시 표시 (`.meetings-list--selecting` 부모 클래스 효과).
- 하지만 **새 항목이 자동 선택되지 않는다는 보장**, 또는 **selection mode 가 자동 종료되지 않는다는 보장**이 산출물에 없음. **WARN**.

### 6.5 액션 실행 후 selection mode 동작
`bulk-actions-mockup.md:349` "액션 완료 후 자동으로 selection mode 해제 또는 유지 — JS 결정 사항" 으로 디자인 결정을 회피. **이는 디자인 의사결정문에서 확정되어야 하는 사항**. 사용자가 액션 실행 후 같은 항목들을 다시 처리할 가능성, 진행 상태 피드백 등을 고려한 결정 필요. **WARN**.

### 수정 권고 (검토 축 6)
- [ ] **(중요)** 부분 전사 가능 케이스의 정책 결정 — `bulk-actions.md` 새 섹션 §1.X 또는 §2.X 에 명시
- [ ] 모바일 액션 버튼 라벨 처리 — "전사+요약" 줄임말 또는 아이콘+라벨 분리 + 글자 잘림 방지 `min-width` 또는 `text-overflow: ellipsis`
- [ ] 모바일에서 `<kbd>` 숨기는 `@media (max-width: 640px)` 또는 `@media (hover: none)` 처리
- [ ] selection mode 활성 중 새 항목 추가 시 동작 정의 (자동 선택 X, mode 유지)
- [ ] 액션 실행 후 selection mode 처리 정책 결정 (해제 권장 — 진행 화면이 콘텐츠 영역에 표시됨)

---

## 베이스라인 PNG 변종명 검증 — **N/A**

본 티켓은 ASCII 다이어그램 기반 mockup (`bulk-actions-mockup.md`) 으로만 산출되었고 PNG 베이스라인은 별도로 생성되지 않음. 이 검토 축은 본 티켓에 적용 불가. **체크리스트 항목 6 은 N/A 처리**.

---

## 종합 판정 — **수정 후 재검토 (changes_requested)**

### 판정 사유
- **검토 축 1 (토큰 일관성)**: WARN — `--radius-md` 미정의 토큰 인용 정정, mockup 의 "var(--radius-lg)→8px" 화살표 오류, hover 톤 정책 (`--bg-hover` vs `--bg-secondary`) 결정 필요
- **검토 축 2 (색 대비)**: WARN — `.bulk-action-bar__count-num` 의 `color: var(--accent)` 를 `var(--accent-text)` 로 변경 필요 (라이트 모드 대비 3.95:1 → 5.57:1). 메뉴 hover 흰 텍스트 on accent 채움의 대비 수치 명시 필요
- **검토 축 3 (다크 단계 톤)**: PASS
- **검토 축 4 (design.md 적합성)**: PASS
- **검토 축 5 (인터랙션 완전성)**: WARN — 체크박스 `:active` 누락, reduced-motion .bulk-action-bar 명시, Cmd+A 키 처리
- **검토 축 6 (엣지 케이스)**: WARN — 부분 전사 케이스 정책, 모바일 라벨 처리, selection mode 종료 정책

설계 큰 줄기는 견고하고 design.md 의 핵심 원칙 (Vibrancy, hairline, easing, 안티 패턴 회피) 를 정확히 따름. 다만 **WCAG AA 색대비 1건 (검토 축 2 의 카운트 숫자)** 은 design.md §2.2 가 명시적으로 만든 `--accent-text` 토큰의 존재 의의를 무력화하는 결정이므로 반드시 수정 필요. 나머지는 정책 명시 / 누락 보강 수준이므로 1 차 수정으로 합의 가능 예상.

---

## Designer-A 가 수정해야 할 변경 항목 (체크리스트)

### 즉시 수정 (FAIL/WARN 핵심)
- [ ] **(검토 축 2 / 핵심)** `bulk-actions.md:117` 와 `bulk-actions-handoff.md:221` 의 `.bulk-action-bar__count-num { color: var(--accent) }` → `color: var(--accent-text)` 로 교체. 사유 박스에 "본문 텍스트는 design.md §2.2 의 `--accent-text` (5.57:1) 사용" 명기
- [ ] **(검토 축 1)** `bulk-actions-mockup.md:110` 의 `border-radius: var(--radius-lg)→8px` 를 `var(--radius-lg) → 10px` 로 정정 (또는 인라인 `border-radius: 8px` 채택을 명기하고 핸드오프 §6 결정과 동기화)
- [ ] **(검토 축 1)** `bulk-actions.md:194` 의 "(=`--radius-md`)" 뒤에 "style.css 미정의 — `var(--radius-lg)` (10px) 또는 인라인 8px 중 택일" 한 줄 추가
- [ ] **(검토 축 1)** `bulk-actions.md` §2.3 표의 `.bulk-action-btn:hover` 행에 background 토큰을 `var(--bg-hover)` 로 유지할지, 인용한 `.home-action-btn:hover` 의 `var(--bg-secondary)` 를 따를지 명시적 결정 + 사유

### 명세 보강 (WARN)
- [ ] **(검토 축 2)** `bulk-actions.md:209-210` 에 메뉴 hover 흰 텍스트 on accent 의 실제 대비 수치 (4.04:1 라이트 / 3.74:1 다크) 와 AA Large 적용 조건 (font-weight ≥600 권장) 추가
- [ ] **(검토 축 5)** `bulk-actions.md:28-36` 체크박스 시각 변종 표에 `:active` 행 추가 (`transform: scale(0.95)` 등)
- [ ] **(검토 축 5)** `bulk-actions-handoff.md` §2.3 에 `@media (prefers-reduced-motion: reduce) .bulk-action-bar { transition: none; transform: none; }` 명시
- [ ] **(검토 축 5)** `bulk-actions.md` §1.2 또는 §2.6 에 `Cmd+A` = 전체 선택 키 단축 정의 또는 v2 보류 명시

### 정책 결정 (WARN)
- [ ] **(검토 축 6 / 핵심)** "선택된 회의 중 일부만 전사 가능 (다른 일부는 이미 전사 완료)" 케이스의 액션 버튼 정책 결정 — 권장: 옵션 B (자동 필터링) + 인라인 캡션 "Y개 항목 처리". `bulk-actions.md` 신규 섹션으로
- [ ] **(검토 축 6)** 모바일에서 액션 버튼 라벨 잘림 처리 — 핸드오프 §2.3 의 `.bulk-action-btn` 에 `min-width: 0`, `text-overflow: ellipsis`, 또는 모바일 한정 줄임말 ("전사+요약" → "통합") 명세
- [ ] **(검토 축 6)** 모바일 `<kbd>` 숨김 미디어 쿼리 (`@media (max-width: 640px) { .bulk-action-bar__dismiss kbd { display: none; } }`)
- [ ] **(검토 축 6)** selection mode 활성 중 새 항목 추가 시 동작 정의 + 액션 실행 후 selection mode 해제/유지 정책 — `bulk-actions.md` 신규 §1.X 또는 §2.X

### 선택적 개선 (LOW)
- [ ] (검토 축 1) raw 픽셀 사용에 대한 정책 한 줄 추가 — design.md ↔ style.css space 토큰 정의 불일치를 사유로
- [ ] (검토 축 5) 드롭다운 첫 글자 매칭은 v2 보류 명시
- [ ] (검토 축 6) 모든 항목 선택 시 시각 피드백 정의 (예: "전체 N개 선택됨" 라벨)

---

## 최종 판정: **수정 후 재검토 (changes_requested)**

위 즉시 수정 항목 4개 (특히 `--accent-text` 교체) 와 명세 보강 / 정책 결정 항목 처리 후 Phase 1B 재검토 요청 바람.
