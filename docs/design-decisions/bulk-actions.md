# 디자인 결정 — 사이드바 다중 선택 + 컨텍스트 액션 바 + 홈 드롭다운

**티켓**: bulk-actions
**Phase**: 1A (Designer-A 산출물) → 1A 수정 (review-1b 반영)
**상태**: 1B 재검토 대기
**최종 업데이트**: 2026-04-29

---

## 0. 결정 요약

기존 `meeting-item` / `home-action-btn` 토큰만 재조합해 다음 3가지 컴포넌트를 추가한다.
**신규 CSS 변수 도입 없음.** 모든 색·반투명·트랜지션은 `style.css :root` 와
`docs/design.md §1.6 macOS Easing` 에 이미 정의된 토큰을 사용한다.

| 컴포넌트 | 핵심 패턴 | 인용 출처 |
|---|---|---|
| 사이드바 체크박스 | hover-reveal + selection-mode persist | `style.css:3966` `meeting-item:hover { transform: translateX(2px) }` |
| 컨텍스트 액션 바 | sticky top, slide-down, accent-tinted bar | `docs/design.md §1.2 Vibrancy` + `style.css :root --bg-active` |
| 홈 드롭다운 | macOS 메뉴 (8px radius + 0.5px hairline + shadow-lg) | `docs/design.md §3.5 Modal` + `--shadow-lg` |

---

## 1. 사이드바 다중 선택 (체크박스)

### 1.1 시각 변종 — `.meeting-item-checkbox`

| 상태 | 시각 명세 | 사용 토큰 |
|---|---|---|
| **default (selection mode OFF, 비-hover)** | `opacity: 0` (보이지 않음, 폭은 차지) | — |
| **hover (selection mode OFF)** | `opacity: 1` (200ms fade-in) | `transition: opacity var(--duration-base) var(--ease-macos)` (`style.css:69-72`) |
| **selection mode ON (1+ 선택됨, 모든 항목)** | `opacity: 1` (영구 표시) | `:has(.meeting-item-checkbox:checked) ~ ...` 또는 부모에 `.meetings-list--selecting` 클래스 |
| **`:active` (눌리는 순간, 50ms)** | 미세 스케일 다운 + accent 강조 | `transform: scale(0.96)`, `border-color: var(--accent)` (or `background: var(--bg-active)` if not yet checked); `transition: transform var(--duration-fast) var(--ease-macos)` |
| **checked** | accent 채움 + 흰 ✓ 글리프 | `background: var(--accent)`, `border-color: var(--accent)`, `color: #fff` |
| **disabled** | 50% opacity | `opacity: 0.5`, `cursor: not-allowed` |
| **focus-visible** | 2-stop 포커스 링 | `box-shadow: var(--focus-ring)` (`style.css:60`) |

> **인용**: `docs/design.md §3.4 Toggle` 의 macOS 컨트롤 비율(20px) 을 체크박스에 그대로 차용.
> 체크박스는 16×16 (macOS 표준 NSButton checkbox) 으로 설정, 여백 4px 좌측에 둔다.

### 1.2 상호작용

| 액션 | 동작 | 결정 근거 |
|---|---|---|
| 체크박스 클릭 | 해당 항목 토글 (뷰어 이동 X) | 체크박스는 `<input>` 자체이므로 `pointer-events: auto` + `event.stopPropagation()` 으로 부모 클릭 차단 (JS 책임, 디자인은 hit-area 만 보장) |
| 항목 본문 클릭 | 기존대로 뷰어 이동 (`.meeting-item.active`) | `style.css:3976` 기존 라우팅 유지 — 시각 변경 없음 |
| Cmd/Ctrl + 클릭 (본문) | 토글 (선택 모드 진입/추가) | macOS 표준 (Finder/Mail 동일) |
| Shift + 클릭 (본문) | 마지막 클릭 ↔ 현재 항목 사이 범위 선택 | macOS 표준 |
| `Esc` | 전체 해제 + selection mode 종료 | `docs/design.md §1.4 Keyboard-First` |

#### 1.2.1 키보드 단축키 표 (전체)

| 키 | 컨텍스트 (포커스 위치) | 동작 |
|---|---|---|
| `Tab` | 전역 | 다음 인터랙티브 요소로 포커스 이동 (사이드바 → 콘텐츠) |
| `↑` / `↓` | 사이드바 항목 | 이전/다음 항목으로 포커스 이동 |
| `Space` | 사이드바 항목 | 해당 항목 체크박스 토글 (selection mode 진입 또는 추가) |
| `Enter` | 사이드바 항목 | 해당 항목으로 라우팅 (뷰어 열기). 선택 상태는 변경 X |
| `Esc` | 전역 | **모든 선택 해제 + selection mode 종료**. 사이드바 헤더의 [✕ 해제] 버튼과 동일 동작 |
| `Cmd+A` (macOS) / `Ctrl+A` (기타) | **사이드바에 포커스가 있을 때만** | 현재 렌더된 사이드바 회의 항목 전체 선택. 다른 영역 (콘텐츠, 입력 필드 등) 에서는 브라우저 기본 동작 (텍스트 전체 선택) 유지 |
| `←` / `→` | 컨텍스트 액션 바 내부 | toolbar 내 버튼 간 포커스 이동 (WAI-ARIA toolbar 패턴) |

> **Cmd+A 범위 결정**: macOS Finder/Mail 의 "현재 리스트 전체 선택" 패턴. 사이드바가 비어있거나 다른 영역에 포커스가 있을 때 적용하면 사용자 입력 흐름을 끊는다. 따라서 **사이드바 컨테이너 (`role="listbox"`) 또는 그 자손에 포커스가 있을 때만** 가로채고 그 외에는 기본 동작 유지.
> **드롭다운 첫 글자 매칭**: macOS NSMenu 표준이지만 본 티켓에서는 v2 보류 (대안 흐름이 충분).

### 1.3 selection mode 시각 신호

선택이 1개 이상일 때 사이드바 전체에 selection mode 가 적용된다.

- **사이드바 항목 자체는 시각 변경 없음** (체크박스만 항상 보임). 이유: 사이드바 톤이 바뀌면
  사용자 시선이 분산되고 macOS Mail 의 다중 선택 UX 와 어긋난다 (`docs/design.md §4.3 List Patterns`).
- **개별 선택 항목 시각**: `.meeting-item.selected` 클래스 추가. 시각 변종은 다음 표.

| 항목 상태 | 배경 | 좌측 보더 | 비고 |
|---|---|---|---|
| 비선택, 비활성 | 기본 (`transparent`) | 없음 | `style.css:3955` |
| 비선택, **hover** | `var(--bg-hover)` | 없음 | 기존 동작 유지 |
| **selected** (체크됨), 비활성 | `var(--bg-active)` (= `rgba(0,122,255,0.12)`) | 없음 | 기존 `.active` 와 동일한 반투명 — 라우팅 active 는 좌측 3px accent 보더로 구분 |
| **selected + active** (현재 뷰어 항목 + 선택됨) | `var(--bg-active)` | `border-left: 3px solid var(--accent)` | 두 개념을 시각적으로 분리 |

> **인용**: `style.css:740 .sidebar-item.active { background: rgba(0,122,255,0.12) }` 가 이미
> `--bg-active` 토큰과 동일 값. 따라서 selected 도 같은 톤을 쓰되, 라우팅 active 는
> `border-left: 3px solid var(--accent)` 로 구분 (기존 사이드바 패턴 차용).

### 1.4 접근성

| 항목 | 결정 | 인용 |
|---|---|---|
| `role` | 사이드바 컨테이너 `role="listbox"`, 항목 `role="option"`, `aria-selected` 동기화 | `docs/design.md §6 A11y` |
| 다중 선택 ARIA | `aria-multiselectable="true"` 컨테이너에 부여 | WAI-ARIA 1.2 listbox 패턴 |
| 키보드 | Tab으로 사이드바 진입, ↑↓ 항목 이동, Space=토글, Enter=뷰어 이동 | `docs/design.md §1.4` |
| 체크박스 라벨 | 시각상 hidden, 스크린리더용 `aria-label="회의 선택: <제목>"` | `docs/design.md §6 A11y` "ARIA 라벨" 행 |
| `prefers-reduced-motion` | fade-in 200ms → 0.01ms (기존 미디어 쿼리 그대로 적용) | `docs/design.md §6` |

### 1.5 Selection Mode 상태 전이 (라이프사이클)

selection mode 는 사이드바의 부모 컨테이너에 부여되는 `.meetings-list--selecting` 클래스로 표현되며, 다음 규칙으로 진입/유지/종료한다.

```
[OFF]  ── 첫 항목 체크 ──▶  [ON, count=N]
                                │
                                ├── 항목 추가 체크 ──▶  count++
                                ├── 항목 체크 해제   ──▶  count--
                                │     └── count==0 ──▶  자동 OFF (액션 바 슬라이드 업)
                                ├── Esc            ──▶  전체 해제 + OFF
                                ├── [✕ 해제] 클릭  ──▶  전체 해제 + OFF
                                └── 액션 실행 완료 ──▶  전체 해제 + OFF (진행/결과는 콘텐츠 영역 표시)
```

| 트리거 | 결과 | 시각 변화 |
|---|---|---|
| OFF 상태에서 첫 체크박스 토글 | ON 진입, `count=1` | `.bulk-action-bar` slide-down (250ms), 사이드바에 `.meetings-list--selecting` 부여 |
| ON 상태에서 마지막 항목 체크 해제 (count→0) | 자동 OFF | `.bulk-action-bar` slide-up (250ms), `.meetings-list--selecting` 제거. **명시적 dismiss 액션 없이도 자동 종료** — 사용자가 다시 selection mode 에 들어오려면 새 체크박스를 다시 눌러야 함 |
| `Esc` | 전체 해제 + OFF | 액션 바 slide-up, 모든 체크 해제. **포커스는 Esc 누른 시점의 항목으로 유지** (사용자 흐름 끊지 않음) |
| 사이드바 헤더의 `[✕ 해제]` 버튼 클릭 | 전체 해제 + OFF | Esc 와 동일. 포커스는 헤더 버튼이 사라지므로 사이드바 첫 항목으로 이동 |
| 액션 실행 완료 ([전사]/[요약]/[전사+요약] 클릭 후) | **전체 해제 + OFF (자동 종료)** | 액션 바 slide-up. 이유: 진행 상태와 결과 카드가 콘텐츠 영역에 표시되므로 selection mode 유지가 시각 잡음. 사용자가 같은 항목들을 재선택할 가능성보다 새 작업 시작 가능성이 큼 |
| selection mode 활성 중 새 회의 도착 (워처가 사이드바 상단에 추가) | **mode 유지, 새 항목은 자동 선택 안 함** | 새 항목의 체크박스는 `.meetings-list--selecting` 효과로 즉시 표시되지만 unchecked. count 변화 없음 |
| 선택된 항목이 스크롤로 시야 밖으로 가려질 때 | mode 유지, 카운트는 그대로 | 액션 바 카운트가 시각 단서를 계속 제공하므로 추가 처리 불필요 |

### 1.6 거부된 대안

| 대안 | 거부 이유 |
|---|---|
| 사이드바 헤더에 `[선택]` 모드 토글 버튼 | 모드 전환 step 1회가 추가되어 macOS Mail 의 즉시-hover-reveal UX 보다 느리다. `docs/design.md §1.5 Progressive UI Disclosure` 의 "콘텐츠가 없을 때 컨트롤 숨김" 원칙과도 어긋난다 (선택은 콘텐츠 위에서 항상 가능해야 함). |
| 체크박스 대신 우클릭 컨텍스트 메뉴 | 우클릭은 `docs/design.md §8 우선순위`의 🥉 큰 작업이고, 다중 선택의 즉시성을 떨어뜨린다. 본 티켓 범위 밖. |
| 항목 좌측 도트(`.meeting-item-dot`)를 체크박스로 교체 | 도트는 상태(완료/실패/처리중) 신호 — 의미 충돌. 체크박스는 도트 좌측에 별도로 둔다. |
| selection mode 진입 시 사이드바 헤더에 "N개 선택됨" 라벨 | 컨텍스트 액션 바에 이미 동일 정보가 있어 중복. macOS 의 절제 원칙 (`docs/design.md §0` "Restraint") 위배. |

---

## 2. 컨텍스트 액션 바 (`.bulk-action-bar`)

### 2.1 위치 & 레이아웃

- **컨테이너**: 콘텐츠 영역(`.content-area`) 최상단, `position: sticky; top: 0; z-index: 50`
- **높이**: 44px (macOS 툴바 표준 — Finder 툴바와 동일)
- **배경**: vibrancy 패턴 적용 — `docs/design.md §1.2`

```
background: rgba(255, 255, 255, 0.72);  /* 라이트 — design.md §1.2 인용 */
backdrop-filter: blur(20px) saturate(180%);
border-bottom: 0.5px solid var(--border);
```

다크 모드:
```
background: rgba(28, 28, 30, 0.72);
```

> **인용**: `docs/design.md §1.2` 의 Vibrancy 패턴 그대로. 신규 토큰 없음.

### 2.2 슬롯 구조 (3-Column)

| 좌측 | 중앙 | 우측 |
|---|---|---|
| `[N개 선택됨]` | `[전사] [요약] [전사+요약]` | `[✕] 해제` |

- **좌측**: `font-size: 13px`, `color: var(--text-secondary)`, **숫자만** `color: var(--accent-text)` + `font-weight: 600` + `font-variant-numeric: tabular-nums` (`style.css:780`)
  - **사유**: 카운트 숫자는 13px 본문 텍스트이므로 `docs/design.md §2.2` 의 "본문 텍스트용 5.57:1 (라이트) / 6.37:1 (다크)" 원칙에 따라 `--accent-text` 사용. `--accent` (`#007AFF` on `#FFFFFF` ≈ 3.95:1) 는 본문 폰트 대비 AA 4.5:1 미달.
  - **검증**: `style.css:36` 라이트 `--accent-text: #0066CC` (5.57:1), `style.css:175,220` 다크 `--accent-text: #4DA1FF` (6.37:1) — 둘 다 AA 통과.
- **중앙**: 3개 버튼 + 8px gap, 각각 `.bulk-action-btn`
- **우측**: ghost 버튼 `[✕]` + "해제" 라벨, `font-size: 12px`, `color: var(--text-secondary)`

### 2.3 `.bulk-action-btn` 시각 명세

| 상태 | 배경 | 보더 | 텍스트 | 인용 |
|---|---|---|---|---|
| **default** | `transparent` | `0.5px solid var(--border)` | `var(--text-primary)` | `docs/design.md §3.2 Button` 의 Secondary 패턴 |
| **hover** | `var(--bg-hover)` | `0.5px solid var(--accent)` | `var(--text-primary)` | **결정**: 행/버튼 단위 hover 는 `--bg-hover` (`rgba(0,0,0,0.04)` 라이트 / `rgba(255,255,255,0.06)` 다크), 섹션/카드 단위 hover 는 `--bg-secondary`. `.bulk-action-btn` 은 단일 버튼 단위 hover 이므로 `--bg-hover`. 인용된 `.home-action-btn:hover` (`style.css:1719`) 가 `--bg-secondary` 를 쓰는 것은 카드형 버튼 컨벤션의 잔재 — frontend-a 가 `--bg-hover` 로 통일하도록 핸드오프 §6 에 명시 |
| **active(:active)** | `var(--bg-active)` | `0.5px solid var(--accent)` | `var(--text-primary)` | `transform: scale(0.97)` (`docs/design.md §3.2`) |
| **disabled** | `transparent` | `0.5px solid var(--border)` | `var(--text-muted)`, `opacity: 0.5` | `style.css:1727-1730` |
| **focus-visible** | 동일 + ring | — | — | `box-shadow: var(--focus-ring)` (`style.css:60`) |

크기: `padding: 6px 12px`, `border-radius: var(--radius)` (=6px), `font-size: 13px`, `font-weight: 500`.

> **거부된 대안**: Primary 톤 (accent 채움) 으로 만들면 컨텍스트 액션 바가 시각적으로 너무 무거워진다.
> `docs/design.md §0` Restraint 원칙. 3개 액션이 동등한 비중이라 모두 Secondary 톤으로 통일.

### 2.4 슬라이드 트랜지션

| 트리거 | 애니메이션 | 토큰 |
|---|---|---|
| 0 → 1 선택 | slide-down + fade-in | `transform: translateY(-8px) → 0`, `opacity: 0 → 1`, `var(--duration-base) var(--ease-macos)` |
| N → 0 선택 | slide-up + fade-out | 역방향, `var(--duration-base) var(--ease-macos)` |
| `prefers-reduced-motion: reduce` | **translateY 제거, opacity 만 페이드 (또는 즉시 표시/제거)** | `transform: none` 강제, `transition: opacity var(--duration-fast) linear` 로 대체. `docs/design.md §6` |

> **인용**: `docs/design.md §1.6` "transition: all var(--duration-base) var(--ease-macos)".
> 200ms ease-out 명세를 250ms ease-macos 로 보정 — 기존 토큰을 우선한다 (요구사항 "신규 토큰 도입 금지").

#### 2.4.1 reduced-motion 구체 명세

```css
@media (prefers-reduced-motion: reduce) {
  .bulk-action-bar {
    transition: opacity var(--duration-fast) linear;
    transform: none !important;
  }
  .bulk-action-bar.is-entering,
  .bulk-action-bar.is-leaving {
    transform: none !important;
  }
}
```

translateY 모션을 제거해 전정 자극을 줄이고, opacity 페이드만 유지해 시각 신호는 보존한다 (즉시 깜빡임은 사용자 인지 단절을 만들 수 있어 페이드 유지가 더 안전).

### 2.5 라이트 / 다크 차이

| 모드 | 배경 | 톤 격차 근거 |
|---|---|---|
| Light | `rgba(255, 255, 255, 0.72)` | `--bg-canvas` 위에 미묘한 vibrancy. 콘텐츠와 동톤이라 sticky 가 자연스럽게 떠 보임 |
| Dark | `rgba(28, 28, 30, 0.72)` | `--bg-canvas: #1C1C1E` 위에 동일 vibrancy. 다크 모드 톤 격차가 큰 만큼 콘텐츠 영역(`#1C1C1E`)과 sidebar(`#2C2C2E`) 사이의 중간 톤으로 자연스럽게 자리잡음 |

`docs/design.md §1.1 Independent Dark Mode` — 다크는 단순 반전이 아니라 톤 격차를 더 크게 줘야 깊이감이 산다.
액션 바의 vibrancy 가 다크 모드에서 더 강하게 느껴지도록 saturate(180%) 가 효과적.

### 2.5.1 부분 적합성 정책 (선택 항목 ↔ 액션 일치 여부)

선택된 회의 중 일부는 이미 전사·요약이 완료된 상태일 수 있다. 본 결정에서는 **자동 필터링 정책 (옵션 B)** 을 채택한다.

| 케이스 | 정책 |
|---|---|
| 선택 항목 중 일부만 액션 적합 (예: 5개 중 3개 미전사) | 액션 버튼 **항상 활성**. 클릭 시 적합한 항목만 자동으로 처리하고 나머지는 skip |
| 선택 항목 전부 부적합 | 액션 버튼 **항상 활성** (회색 처리 X). 클릭 시 0개 처리 + 안내 toast (위 표 외 케이스) |
| 선택 항목 0개 | 액션 바 자체가 hidden 이므로 버튼 비활성 상태가 의미 없음 |

#### 액션 실행 후 toast 메시지 패턴

```
"3개 처리, 2개 건너뜀 (이미 전사 완료)"
"전사 시작: 5개"   ← 모두 적합한 경우
"건너뜀: 2개 (이미 처리됨)"   ← 부적합만 있는 경우
```

> **결정 사유**: 액션 버튼을 회색 처리하면 (1) 사용자에게 "왜 비활성인가" 의 이유를 모달/툴팁으로 추가 설명해야 하는데 이는 `docs/design.md §7` 안티 패턴 "모든 액션에 확인 모달" 회피와 충돌, (2) 일부 항목만 적합한 흔한 케이스에서 사용자가 "어떤 항목 때문에 비활성?" 을 추적해야 함. 자동 필터링은 macOS Mail 의 "여러 메일 일괄 처리" 패턴과 동일.

> **버튼 비활성 조건은 단 하나**: 선택 항목이 0개 (= 액션 바 자체가 hidden). 이외에는 모두 활성.

### 2.5.2 모바일 (≤640px) 라벨/힌트 정책

| 요소 | 데스크톱 | 모바일 (≤640px) |
|---|---|---|
| 액션 버튼 라벨 | "전사" / "요약" / "전사+요약" 텍스트 | **아이콘만** 표시 (라벨 텍스트 숨김). 아이콘 옆 시각상 라벨 X. `aria-label` 로 텍스트 보존 + long-press/hover 시 네이티브 툴팁 |
| 카운트 라벨 | "3개 선택됨" 풀 텍스트 | "3" 숫자 + 사람 아이콘 (👤 또는 SVG)으로 축약. `aria-label="3개 선택됨"` 로 스크린리더 보존 |
| `[✕] 해제` 라벨 | "해제" 텍스트 + `<kbd>Esc</kbd>` | "✕" 아이콘만 (라벨 + `<kbd>` 모두 **숨김**). `aria-label="선택 해제"` 보존 |
| 단축키 `<kbd>Esc</kbd>` | 데스크톱에서만 표시 | **숨김** (모바일에 키보드 입력기 없음). 미디어 쿼리: `@media (max-width: 640px) { .bulk-action-bar__dismiss kbd { display: none; } }` |

> **사유**: 모바일 viewport 375px 기준 액션 3-버튼이 `flex: 1` 균등 분할 시 각 ~110px 인데, "전사+요약" 풀 라벨은 약 80px 가 필요해 패딩과 합쳐지면 잘림 위험. 아이콘만 표시하면 ~32px 로 축소되어 여유. 텍스트 정보는 `aria-label` 로 보존 → 접근성 영향 없음.
> **`<kbd>` 숨김 사유**: 모바일에 물리 키보드가 없어 단축키 시각 힌트가 노이즈. iOS/Android 의 외부 키보드 연결 사용자는 데스크톱 클래스 viewport 로 인식되는 경우가 많으므로 `(max-width: 640px)` 와 `(hover: none) and (pointer: coarse)` 양쪽 조합 가능 (구현은 frontend-a 결정).

### 2.6 접근성

| 항목 | 결정 |
|---|---|
| `role` | `<div role="toolbar" aria-label="선택된 회의 일괄 작업">` |
| `aria-live` | "N개 선택됨" 라벨에 `aria-live="polite"` — 선택 변경 시 스크린리더 안내 |
| 키보드 | Tab으로 진입, ←→ 또는 Tab 으로 버튼 이동, Enter/Space로 액션 실행 |
| 단축키 | `Esc` = 해제 (사이드바 단축키와 동일 동작), 단축키 표시는 v2 |
| `<kbd>` 힌트 | "해제" 옆에 `<kbd>Esc</kbd>` 표시 (`docs/design.md §1.4`) |

### 2.7 거부된 대안

| 대안 | 거부 이유 |
|---|---|
| 콘텐츠 영역 하단 floating action bar (Gmail 스타일) | sticky top 이 macOS 툴바 패턴(Finder/Mail)에 부합. 하단 floating 은 web/Android 패턴 — `docs/design.md §0` Familiarity 위배 |
| 모달 다이얼로그로 액션 확인 | `docs/design.md §7 안티 패턴` "모든 액션에 확인 모달 → undo 토스트로 대체" |
| 액션 바를 사이드바 하단에 부착 | 콘텐츠 영역이 액션 결과를 보여줘야 하므로 시각 동선이 꼬인다. 콘텐츠 상단이 자연스러움 |

---

## 3. 홈 화면 액션 재배치 (드롭다운)

### 3.1 버튼 라인 변경

| 변경 전 | 변경 후 |
|---|---|
| `[전사 폴더 열기] [일괄 업로드] [일괄 요약 생성]` | `[전사 폴더 열기] [일괄 업로드] [전체 일괄 ▾] [최근 24시간 ▾]` |

- 처음 2개는 기존 `.home-action-btn` 그대로 (`style.css:1703`).
- 뒤 2개는 **드롭다운 트리거** — `.home-action-btn` 에 `.home-action-btn--dropdown` modifier 추가, 우측에 `▾` (12px chevron) 가 붙는다.

### 3.2 `.home-action-dropdown` (메뉴) 시각 명세

| 항목 | 명세 | 인용 |
|---|---|---|
| **컨테이너** | `position: absolute` (트리거 기준 `top: calc(100% + 4px)`), `min-width: 200px` | macOS NSMenu 표준 |
| **배경** | `var(--bg-card)` | `style.css:27` |
| **보더** | `0.5px solid var(--border)` | `docs/design.md §1.3` |
| **Radius** | `var(--radius-lg)` (10px) | `docs/design.md §2.4` 가 카드/토글 8px 를 권장하지만 `style.css :root` 에는 `--radius-md` 미정의 — 기존 토큰 중 가장 가까운 `--radius-lg` (10px, `style.css:65`) 채택. **신규 토큰 도입 금지** 원칙 준수. design.md ↔ style.css 갭은 frontend-a 가 토큰 추가 결정 시 핸드오프 §6 참조 |
| **Shadow** | `var(--shadow-lg)` | `docs/design.md §3.5 Modal` |
| **Padding** | `4px 0` (메뉴 외곽), 메뉴 항목은 `6px 12px` | macOS NSMenuItem 표준 (8px = 너무 큼) |
| **Vibrancy** | (선택) `backdrop-filter: blur(20px) saturate(180%)` + `background: rgba(255,255,255,0.92)` | `docs/design.md §1.2` — 단, fallback `var(--bg-card)` 보장 |

### 3.3 메뉴 항목 (`.home-action-dropdown-item`)

| 상태 | 시각 |
|---|---|
| default | `padding: 6px 12px`, `font-size: 13px`, `color: var(--text-primary)`, `background: transparent` |
| hover / focus-visible | `background: var(--accent)`, `color: #fff` (macOS NSMenu 표준 — accent 채움) |
| disabled | `color: var(--text-muted)`, `opacity: 0.5`, `cursor: not-allowed` |
| 선택된 옵션 (현재 활성) | 좌측에 ✓ 글리프 (12px, `color: var(--accent)`), 호버 시 `color: #fff` |

> **인용**: `docs/design.md §0 Familiarity` — macOS NSMenu 의 hover 패턴(파란 채움)이 사용자 친숙도 1순위.
>
> **대비 분석 (메뉴 항목 hover 흰 텍스트 on accent 채움)**:
> | 모드 | 텍스트 색 | 배경 색 | 대비 | WCAG 평가 |
> |---|---|---|---|---|
> | Light | `#FFFFFF` | `#007AFF` (--accent) | 4.04:1 | AA Normal **미달** (4.5:1) / AA Large 통과 (3:1) |
> | Dark | `#FFFFFF` | `#0A84FF` (--accent dark) | 3.74:1 | AA Normal **미달** / AA Large 통과 |
>
> **결정**: 메뉴 항목 텍스트는 `font-size: 13px` + **`font-weight: 600`** 으로 명시. WCAG AA Large 의 "18.66px / 14pt + 굵은 글씨 (700+)" 기준에는 13px 가 못 미치지만, **macOS NSMenu 의 시스템 표준 hover 패턴을 친숙도 우선으로 채택**. 배경 채움이 가시성을 충분히 확보하며, 사용자가 macOS 환경에서 해당 패턴을 일상적으로 인식. 단, 이 결정은 **AA Normal 4.5:1 기준의 예외 케이스**임을 명시.
>
> **거부된 대안**: 텍스트를 `#fff` 가 아닌 `var(--bg-canvas)` (다크에서 `#1C1C1E`) 로 바꾸면 다크 모드에서 텍스트가 거의 안 보임. 또는 배경을 `--accent-text` (`#0066CC`) 로 하면 NSMenu 표준에서 멀어져 친숙도 손실.

### 3.4 옵션

두 드롭다운 모두 동일한 3개 옵션:

```
[전체 일괄 ▾]                    [최근 24시간 ▾]
  ✓ 전사+요약 (통합) ← 기본       ✓ 전사+요약 (통합) ← 기본
    전사만                            전사만
    요약만                            요약만
```

### 3.5 트랜지션

| 트리거 | 애니메이션 |
|---|---|
| 열림 | `opacity: 0 → 1`, `transform: translateY(-4px) → 0`, `var(--duration-fast) var(--ease-macos)` (150ms — 메뉴는 빠를수록 좋음) |
| 닫힘 | 즉시 (또는 100ms fade-out) |

> **인용**: `docs/design.md §5.4 Micro-Animations Restraint` — 0.15s ease-out, 사용자가 알아차리면 실패.

### 3.6 접근성

| 항목 | 결정 |
|---|---|
| `role` | 트리거 `<button aria-haspopup="menu" aria-expanded="false">`, 메뉴 `role="menu"`, 항목 `role="menuitemradio"` (옵션 그룹) |
| 키보드 | Enter/Space=열기, ↑↓=이동, Enter=선택, Esc=닫기, Tab=닫고 다음 컨트롤로 이동 |
| 외부 클릭 | 닫기 (포커스는 트리거로 복귀) |
| `aria-checked` | 선택된 옵션에 `aria-checked="true"` |

### 3.7 라이트 / 다크 차이

| 모드 | 메뉴 배경 |
|---|---|
| Light | `var(--bg-card)` = `#FFFFFF`, 보더 `#D1D1D6` (`style.css:27,50`) |
| Dark | `var(--bg-card)` = `#2C2C2E`, 보더 `#38383A` (`style.css :root` dark override) |

다크 모드는 메뉴가 `--bg-canvas: #1C1C1E` 위에 떠 있어서 톤 격차(`#2C2C2E` vs `#1C1C1E`)가 자연스럽게 깊이감을 만든다 (`docs/design.md §1.1`).

### 3.8 거부된 대안

| 대안 | 거부 이유 |
|---|---|
| 단일 `[일괄 작업 ▾]` 버튼에 모든 옵션(범위×액션 = 6개) | 옵션 수가 많아 메뉴가 길어지고 "범위/액션" 두 차원이 섞여 가독성 저하. **범위(전체/최근 24h)를 버튼으로 분리, 액션(전사/요약/통합)을 옵션으로** 분리하는 편이 macOS Mail 의 "검색 범위" 토글 + "메일 액션" 메뉴 패턴과 일치 |
| 드롭다운 대신 라디오 버튼 + 실행 버튼 | 클릭 횟수 증가. macOS 네이티브 앱에서 "기본값 + 변경 가능한 옵션" 은 split button 또는 dropdown menu 가 표준 |
| 컨텍스트 액션 바와 동일한 가로 3-버튼 레이아웃 | 홈 화면은 "범위 X 액션" 2차원 결정이라 단순 3-버튼으로 표현 불가. 컨텍스트 바는 "이미 선택된 N개" 라는 범위 고정 후의 단일 차원이라 3-버튼 OK |

---

## 4. 단일 진실 공급원 매핑 표

본 결정에서 사용한 모든 토큰의 출처:

| 토큰 / 패턴 | 정의 위치 |
|---|---|
| `--bg-canvas`, `--bg-sidebar`, `--bg-card`, `--bg-hover`, `--bg-active`, `--bg-input`, `--bg-secondary` | `style.css:18-27`, `docs/design.md §2.2` |
| `--text-primary`, `--text-secondary`, `--text-muted` | `style.css:30-32`, `docs/design.md §2.2` |
| `--accent`, `--accent-text`, `--accent-hover` | `style.css:35-38`, `docs/design.md §2.2` |
| `--border`, `--border-light`, `--border-focus` | `style.css:50-52` |
| `--shadow-sm`, `--shadow`, `--shadow-lg` | `style.css:53-55`, `docs/design.md §2.5` |
| `--radius` (6), `--radius-lg` (10) | `style.css:64-65`, `docs/design.md §2.4`. **`--radius-md` (8px) 는 design.md 에만 존재, style.css 미정의** — 본 결정에서는 `--radius-lg` 채택 |
| `--ease-macos`, `--duration-fast`, `--duration-base`, `--duration-slow` | `style.css:69-73`, `docs/design.md §1.6` |
| `--focus-ring`, `--focus-ring-soft` | `style.css:60-61`, `docs/design.md §2.2` |
| Vibrancy 패턴 (`backdrop-filter: blur(20px) saturate(180%)`) | `docs/design.md §1.2` |
| 0.5px hairline 보더 | `docs/design.md §1.3` |
| `font-variant-numeric: tabular-nums` | `style.css:780`, `docs/design.md §2.1` |
| Independent Dark Mode 톤 격차 | `docs/design.md §1.1` |
| `prefers-reduced-motion` | `docs/design.md §6` |

**신규 토큰 도입 0건. 모든 결정은 위 표의 토큰 조합으로 구현 가능.**

---

## 5. 검증 체크리스트 (자가)

- [x] `docs/design.md` 의 macOS 네이티브 원칙 (Vibrancy, hairline, easing) 모두 인용
- [x] 신규 CSS 변수 0건
- [x] light/dark 차이 명시
- [x] 접근성 (ARIA, 키보드, reduced-motion) 컴포넌트별 명시
- [x] 거부된 대안 컴포넌트별 명시
- [x] 모든 결정에 출처 인용 (style.css 라인 번호 또는 design.md 섹션)
- [x] 한국어 작성, 토큰 식별자는 원문 유지
- [x] WCAG AA 대비 수치 라이트/다크 각각 명시 (카운트 숫자 5.57:1/6.37:1, 메뉴 hover 4.04:1/3.74:1)
- [x] 체크박스 6 상태 (default/hover/active/checked/disabled/focus) 모두 명세
- [x] selection mode 상태 전이 (진입/종료/새 항목 추가) 정의
- [x] 부분 적합성 정책 (자동 필터링) 결정
- [x] 모바일 라벨/툴팁/`<kbd>` 숨김 정책 결정
- [x] reduced-motion 의 translateY 제거 + opacity 만 유지 명시

---

## 6. raw 픽셀 사용 정책 / v2 보류 항목

### 6.1 raw 픽셀 사용 정책

본 결정에서 `padding: 6px 12px`, `gap: 8px`, `padding: 4px 8px`, `padding: 0 16px` 등 raw 픽셀이 다수 등장한다. 이는 다음 두 사유에서 의도된 선택이다.

1. **design.md §2.3 vs style.css §1-B 의 space 토큰 정의 불일치**: design.md 의 `--space-2` = 8px 인 반면 style.css 의 `--space-2` = 4px 로 정의되어 있어, 토큰을 강제하면 시각 결과가 달라질 수 있다. 양쪽 갭이 해소되기 전에는 raw px 가 안전.
2. **기존 컴포넌트 일관성**: `.home-action-btn` (`style.css:1707` `padding: 8px 14px`), `.meeting-item` (`style.css:3959` `padding: 10px 12px`) 등 프로젝트 내 다수 컴포넌트가 이미 raw px 를 사용. 본 컴포넌트만 토큰 사용은 불일치.

**예외 (raw px 가 OK 한 케이스)**: `0.5px` hairline (design.md §1.3 강제), `1px` / `2px` 보더, `3px` accent border-left, `12px`/`16px` 같이 토큰 정의가 일치하는 케이스.

**향후 개선 방향**: design.md ↔ style.css 의 space 토큰 정의를 일치시키는 별도 티켓 (T-103 예상) 이후 raw px 를 토큰으로 점진 교체.

### 6.2 v2 보류 항목 (본 티켓 범위 밖)

| 항목 | 보류 사유 |
|---|---|
| 일괄 삭제 | 데이터 소실 위험 액션 — undo 토스트 + 확인 흐름 별도 설계 필요 |
| 일괄 다시 처리 (재전사) | 진행 중 작업 충돌 / 큐 정책 별도 결정 필요 |
| 드롭다운 첫 글자 매칭 (NSMenu typeahead) | macOS 표준이지만 ↑↓ 흐름으로 충분, 구현 우선순위 낮음 |
| 모든 항목 선택 시 "전체 N개 선택됨" 라벨 변환 | 카운트 표시로 충분, 라벨 분기는 추가 복잡도 |
| 단축키 `<kbd>` 표시 (해제 외 다른 액션) | 가시성 우선순위 낮음, 단축키 일람 모달은 별도 티켓 |
| Cmd+A 의 사이드바 외 영역 적용 | 본 티켓은 사이드바 한정. 글로벌 Cmd+A 는 별도 결정 |

---

## 7. Phase 1A 수정 이력 (review-1b 반영)

**수정 일자**: 2026-04-30
**원본 검토**: `docs/design-decisions/bulk-actions-review-1b.md`
**처리한 체크리스트**: 13/13

### 즉시 수정 (4)
1. **§2.2 카운트 숫자 색**: `var(--accent)` → `var(--accent-text)` 교체. WCAG AA 본문 텍스트 4.5:1 보장 (라이트 5.57:1, 다크 6.37:1). `style.css:36, 175, 220` 에 이미 정의된 토큰 사용 — 신규 토큰 도입 0건 유지
2. **§3.2 드롭다운 radius**: `var(--radius-lg)→8px` 잘못된 표기 정정 → `var(--radius-lg)` (10px) 명시. `--radius-md` 미정의 갭은 frontend-a 가 토큰 추가 결정 시 핸드오프 §6 참조 권고
3. **§4 토큰 매핑 표**: `--radius-md` 미정의 사실 명시 + `--radius-lg` 채택 사유 한 줄 추가
4. **§2.3 hover 토큰 통일**: `.bulk-action-btn:hover` 의 background 를 `var(--bg-hover)` 로 확정 (행/버튼 단위 hover 컨벤션). 인용된 `.home-action-btn:hover` (`--bg-secondary`) 와의 차이 사유 명시

### 명세 보강 (3)
5. **§1.1 체크박스 `:active` 행 추가**: `transform: scale(0.96)` + `border-color: var(--accent)` 미세 피드백 (50ms)
6. **§2.4.1 reduced-motion 대안**: translateY 제거 + opacity 페이드 유지 (`@media (prefers-reduced-motion: reduce)` 블록 명시)
7. **§1.2.1 키보드 단축키 표**: Esc / Cmd+A (사이드바 한정) / Tab / ↑↓ / Space / Enter / 컨텍스트 액션 바의 ←→ 모두 행렬화

### 정책 반영 (4)
8. **§2.5.1 부분 적합성 정책**: 자동 필터링 (옵션 B) 채택. 액션 버튼은 0개 선택 외에는 항상 활성. toast 메시지 패턴 제시
9. **§2.5.2 모바일 라벨/툴팁 정책**: ≤640px 에서 액션 버튼은 아이콘만, 카운트는 숫자+사람 아이콘, `aria-label` 보존
10. **§2.5.2 모바일 `<kbd>` 숨김**: `@media (max-width: 640px) { .bulk-action-bar__dismiss kbd { display: none; } }`
11. **§1.5 selection mode 상태 전이**: 진입/유지/자동 종료 (마지막 항목 해제 시) / Esc / `[✕] 해제` / 액션 실행 후 자동 종료 / 새 회의 도착 시 mode 유지 + 자동 선택 안 함 / 스크롤 시야 밖 처리 모두 명시

### 선택적 (2)
12. **§6.1 raw 픽셀 정책**: design.md ↔ style.css space 토큰 불일치 사유 명시. hairline / 1-2px 보더 예외 인정
13. **§6.2 v2 보류 항목**: 일괄 삭제, 일괄 재처리, 드롭다운 typeahead, 전체선택 라벨 변환, 단축키 `<kbd>` 일람, 글로벌 Cmd+A 모두 명시

### 추가 보강 (검토에서 직접 요청되지 않은 명세)
- **§3.3 메뉴 hover 흰 텍스트 대비 수치**: 라이트 4.04:1 / 다크 3.74:1 명시. AA Normal 미달 사실 + macOS NSMenu 친숙도 우선 결정 사유 + font-weight 600 권장 추가
- **§5 검증 체크리스트**: 13개 항목 추가 (대비 수치 / 6 상태 / 상태 전이 / 부분 적합성 / 모바일 / reduced-motion)

**변경되지 않은 항목 (PASS)**:
- 검토 축 3 (다크 단계 톤): PASS — 변경 없음
- 검토 축 4 (design.md 적합성: Vibrancy/Hairline/easing/안티 패턴): PASS — 변경 없음
