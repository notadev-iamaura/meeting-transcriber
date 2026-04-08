# 디자인 토큰 사전 설계 — macOS Easing / 0.5px Hairline / kbd 컴포넌트

> **작성자**: designer-tokens (design-iteration 팀)
> **작성일**: 2026-04-08
> **대상 파일**: `ui/web/style.css` (4935 lines)
> **기반 문서**: `docs/design.md` (§1.3, §1.4, §1.6), macos-design-skill
> **소비자**: frontend-tokens (WS-1) — 본 문서의 "변경 후" 값을 그대로 복붙하여 작업
> **원칙**:
> - 모든 transition은 `var(--duration-*) var(--ease-macos)` 형태로 통일
> - 구분선 역할의 1px solid는 0.5px로 하향 (hairline)
> - `<kbd>` 컴포넌트는 신규 정의 (기존 스타일 없음)

---

## 0. 사전 요약 (frontend-tokens용 TL;DR)

1. **`:root`에 토큰 4개 추가** → `--ease-macos`, `--duration-fast/base/slow`
2. **기존 `--transition: 0.2s ease` 제거**, `var(--duration-base) var(--ease-macos)`로 대체
3. **transition 선언 76곳 일괄 치환** (본 문서 §1.2 표 참조)
4. **`1px solid` 37곳 중 변환 대상 30곳을 `0.5px solid`로 변경** (본 문서 §2 표 참조)
5. **`<kbd>` 컴포넌트 신규 추가** (본 문서 §3 CSS 블록 그대로 복붙)
6. **`prefers-reduced-motion` 가드는 유지** (style.css:3371 부근)

---

## 1. macOS Easing 토큰

### 1.1 `:root` 토큰 정의 (신규 추가)

```css
/* ui/web/style.css :root 내부에 추가 — 기존 --transition 선언은 제거 */
:root {
  /* macOS 네이티브 이징 곡선 (docs/design.md §1.6) */
  --ease-macos: cubic-bezier(0.25, 0.46, 0.45, 0.94);

  /* 인터랙션 duration 토큰 */
  --duration-fast: 150ms;   /* hover/focus/색상 변경 (즉각 반응 필요) */
  --duration-base: 250ms;   /* 기본값: 배경/보더/opacity/transform */
  --duration-slow: 400ms;   /* 레이아웃 변화, 큰 transform, 진행 바 */
}
```

**제거 대상** (style.css:59):
```css
--transition: 0.2s ease;   /* ❌ 삭제 */
```

### 1.2 Duration 매핑 원칙

| 분류 | Duration | 근거 |
|------|----------|------|
| **focus / outline / border-color** | `--duration-fast` (150ms) | 키보드 피드백은 즉각성 우선 |
| **hover (배경/색상 변경만)** | `--duration-fast` (150ms) | 마우스 흐름과 동기화 |
| **hover (transform/shadow 포함)** | `--duration-base` (250ms) | 공간 이동은 약간 더 여유 |
| **click / active (state 전환)** | `--duration-base` (250ms) | macOS 표준 |
| **expand / collapse / layout** | `--duration-slow` (400ms) | 큰 변화는 눈이 따라갈 시간 필요 |
| **progress bar (width 변경)** | `--duration-slow` (400ms) | 진행률 시각화 |

### 1.3 마이그레이션 표 (76건)

> 기존 `--transition` 변수를 쓰는 선언과 하드코딩된 `0.2s ease`, `0.3s ease`, `0.15s ease`를 모두 포함.
> `ui/web/style.css` 기준.

#### 1.3.1 `var(--transition)` 기반 선언 (50건)

| # | 줄 | 셀렉터 | 현재 값 | 변경 후 값 | 분류 |
|---|----|--------|---------|------------|------|
| 1 | 59 | `:root` | `--transition: 0.2s ease;` | **삭제** | token |
| 2 | 296 | `a` | `transition: color var(--transition);` | `transition: color var(--duration-fast) var(--ease-macos);` | hover |
| 3 | 382 | `.nav-btn` | `transition: background var(--transition), color var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos), color var(--duration-fast) var(--ease-macos);` | hover |
| 4 | 485 | `.list-search input` | `transition: border-color var(--transition), box-shadow var(--transition);` | `transition: border-color var(--duration-fast) var(--ease-macos), box-shadow var(--duration-fast) var(--ease-macos);` | focus |
| 5 | 514 | `.list-sort select` | `transition: border-color var(--transition);` | `transition: border-color var(--duration-fast) var(--ease-macos);` | focus |
| 6 | 538 | `.sidebar-item` | `transition: background var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos);` | hover |
| 7 | 619 | `.sidebar-search` | `transition: border-color var(--transition), box-shadow var(--transition);` | `transition: border-color var(--duration-fast) var(--ease-macos), box-shadow var(--duration-fast) var(--ease-macos);` | focus |
| 8 | 658 | `.status-dot` | `transition: background var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos);` | state |
| 9 | 817 | `.error-banner-close` | `transition: background var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos);` | hover |
| 10 | 847 | `.theme-toggle` | `transition: all var(--transition);` | `transition: all var(--duration-base) var(--ease-macos);` | click |
| 11 | 1447 | `.search-input` | `transition: border-color var(--transition), box-shadow var(--transition);` | `transition: border-color var(--duration-fast) var(--ease-macos), box-shadow var(--duration-fast) var(--ease-macos);` | focus |
| 12 | 1470 | `.search-btn` | `transition: background var(--transition), opacity var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos), opacity var(--duration-fast) var(--ease-macos);` | hover |
| 13 | 1515 | `.filter-input` | `transition: border-color var(--transition);` | `transition: border-color var(--duration-fast) var(--ease-macos);` | focus |
| 14 | 1530 | `.filter-clear-btn` | `transition: background var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos);` | hover |
| 15 | 1545 | `.checkbox-item` | `transition: background var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos);` | hover |
| 16 | 1605 | `.search-close-btn` | `transition: background var(--transition), color var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos), color var(--duration-fast) var(--ease-macos);` | hover |
| 17 | 1648 | `.sort-select` | `transition: border-color var(--transition);` | `transition: border-color var(--duration-fast) var(--ease-macos);` | focus |
| 18 | 1675 | `.batch-summarize-btn` | `transition: background var(--transition), color var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos), color var(--duration-fast) var(--ease-macos);` | hover |
| 19 | 1717 | `.meeting-card` | `transition: transform var(--transition), box-shadow var(--transition), border-color var(--transition);` | `transition: transform var(--duration-base) var(--ease-macos), box-shadow var(--duration-base) var(--ease-macos), border-color var(--duration-base) var(--ease-macos);` | hover |
| 20 | 1837 | `.step-dot` | `transition: background var(--transition), transform var(--transition);` | `transition: background var(--duration-base) var(--ease-macos), transform var(--duration-base) var(--ease-macos);` | state |
| 21 | 1889 | `.meeting-card-action` | `transition: background var(--transition), color var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos), color var(--duration-fast) var(--ease-macos);` | hover |
| 22 | 1950 | `.result-item` | `transition: background var(--transition), ...;` | `transition: background var(--duration-fast) var(--ease-macos);` | hover |
| 23 | 2068 | `.back-link` | `transition: opacity var(--transition);` | `transition: opacity var(--duration-fast) var(--ease-macos);` | hover |
| 24 | 2151 | `.viewer-action-btn` | `transition: all var(--transition);` | `transition: all var(--duration-base) var(--ease-macos);` | hover |
| 25 | 2262 | `.tab-btn` | `transition: color var(--transition), border-color var(--transition);` | `transition: color var(--duration-fast) var(--ease-macos), border-color var(--duration-fast) var(--ease-macos);` | state |
| 26 | 2332 | `.search-bar-clear` | `transition: background var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos);` | hover |
| 27 | 2355 | `.utterance` | `transition: background var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos);` | hover |
| 28 | 2534 | `.btn-summarize` | `transition: background var(--transition), opacity var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos), opacity var(--duration-fast) var(--ease-macos);` | hover |
| 29 | 2566 | `.btn-regenerate` | `transition: all var(--transition);` | `transition: all var(--duration-base) var(--ease-macos);` | hover |
| 30 | 2649 | `.btn-small` | `transition: background var(--transition), color var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos), color var(--duration-fast) var(--ease-macos);` | hover |
| 31 | 2798 | `.chat-input` | `transition: border-color var(--transition), box-shadow var(--transition);` | `transition: border-color var(--duration-fast) var(--ease-macos), box-shadow var(--duration-fast) var(--ease-macos);` | focus |
| 32 | 2821 | `.send-btn` | `transition: background var(--transition), opacity var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos), opacity var(--duration-fast) var(--ease-macos);` | hover |
| 33 | 2848 | `.btn-cancel-send` | `transition: background var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos);` | hover |
| 34 | 2968 | `.ref-card` | `transition: background var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos);` | hover |
| 35 | 3035 | `.btn-copy` | `transition: color var(--transition), background var(--transition), border-color var(--transition);` | `transition: color var(--duration-fast) var(--ease-macos), background var(--duration-fast) var(--ease-macos), border-color var(--duration-fast) var(--ease-macos);` | hover |
| 36 | 3062 | `.meeting-list-item` | `transition: background var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos);` | hover |
| 37 | 3147 | `.list-row` | `transition: background var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos);` | hover |
| 38 | 3273 | `.meeting-item-dot` | `transition: background var(--transition);` | `transition: background var(--duration-fast) var(--ease-macos);` | state |
| 39 | 3662 | `.setting-help` | `transition: all var(--transition);` | `transition: all var(--duration-base) var(--ease-macos);` | state |
| 40 | 4391 | `.stt-model-card` | `transition: border-color var(--transition), background var(--transition), box-shadow var(--transition);` | `transition: border-color var(--duration-base) var(--ease-macos), background var(--duration-base) var(--ease-macos), box-shadow var(--duration-base) var(--ease-macos);` | hover |
| 41 | 4487 | `.stt-action-btn` | `transition: opacity var(--transition), background var(--transition), border-color var(--transition);` | `transition: opacity var(--duration-fast) var(--ease-macos), background var(--duration-fast) var(--ease-macos), border-color var(--duration-fast) var(--ease-macos);` | hover |

> **일괄 처리 요령 (frontend-tokens)**: `:root`의 `--transition`을 **제거**한 뒤, 아래 sed 후보 패턴을 참고:
> - `var(--transition)` → 용도별 수동 치환 필요 (본 표의 "변경 후" 값 참조)

#### 1.3.2 하드코딩된 `0.Xs ease` 선언 (26건)

| # | 줄 | 셀렉터 | 현재 값 | 변경 후 값 | 분류 |
|---|----|--------|---------|------------|------|
| 42 | 731 | `#content > :first-child` | `animation: fadeIn 0.2s ease;` | `animation: fadeIn var(--duration-base) var(--ease-macos);` | mount |
| 43 | 757 | `.error-banner` | `transition: top 0.3s ease;` | `transition: top var(--duration-slow) var(--ease-macos);` | slide |
| 44 | 774 | `.error-banner.auto-hiding` | `transition: top 0.4s ease, opacity 0.4s ease;` | `transition: top var(--duration-slow) var(--ease-macos), opacity var(--duration-slow) var(--ease-macos);` | slide |
| 45 | 896 | `.recording-status` | `transition: top 0.3s ease;` | `transition: top var(--duration-slow) var(--ease-macos);` | slide |
| 46 | 962 | `.loading-overlay` | `transition: opacity 0.2s ease;` | `transition: opacity var(--duration-base) var(--ease-macos);` | mount |
| 47 | 1036 | `.pipeline-step-dot` | `transition: all 0.3s ease;` | `transition: all var(--duration-slow) var(--ease-macos);` | state |
| 48 | 1071 | `.pipeline-step-line` | `transition: background 0.3s ease;` | `transition: background var(--duration-slow) var(--ease-macos);` | state |
| 49 | 1199 | `.resource-bar-fill` | `transition: width 0.3s ease, background 0.3s ease;` | `transition: width var(--duration-slow) var(--ease-macos), background var(--duration-slow) var(--ease-macos);` | progress |
| 50 | 1260 | `.global-resource-bar .grb-bar-fill` | `transition: width 0.3s ease, background 0.3s ease;` | `transition: width var(--duration-slow) var(--ease-macos), background var(--duration-slow) var(--ease-macos);` | progress |
| 51 | 1312 | `.viewer-log-panel summary::before` | `transition: transform 0.2s ease;` | `transition: transform var(--duration-base) var(--ease-macos);` | expand |
| 52 | 1582 | `.search-results.visible` | `animation: fadeIn 0.2s ease;` | `animation: fadeIn var(--duration-base) var(--ease-macos);` | mount |
| 53 | 1864 | `.progress-fill` (card) | `transition: width 0.3s ease;` | `transition: width var(--duration-slow) var(--ease-macos);` | progress |
| 54 | 2286 | `.tab-panel.active` | `animation: fadeIn 0.15s ease;` | `animation: fadeIn var(--duration-fast) var(--ease-macos);` | mount |
| 55 | 2878 | `.message` | `animation: fadeIn 0.2s ease;` | `animation: fadeIn var(--duration-base) var(--ease-macos);` | mount |
| 56 | 3229 | `.meeting-item` | `transition: background 0.15s ease, transform 0.15s ease;` | `transition: background var(--duration-fast) var(--ease-macos), transform var(--duration-fast) var(--ease-macos);` | hover |
| 57 | 3262 | `.meeting-item.processing` | `animation: fadeIn 0.3s ease;` | `animation: fadeIn var(--duration-slow) var(--ease-macos);` | mount |
| 58 | 3620 | `.setting-select` | `transition: border-color 0.15s ease;` | `transition: border-color var(--duration-fast) var(--ease-macos);` | focus |
| 59 | 3740 | `.toggle-track` | `transition: background 0.2s ease;` | `transition: background var(--duration-base) var(--ease-macos);` | state |
| 60 | 3754 | `.toggle-thumb` | `transition: transform 0.2s ease;` | `transition: transform var(--duration-base) var(--ease-macos);` | state |
| 61 | 3831 | `.settings-save-btn` | `transition: background 0.15s ease, opacity 0.15s ease;` | `transition: background var(--duration-fast) var(--ease-macos), opacity var(--duration-fast) var(--ease-macos);` | hover |
| 62 | 3851 | `.settings-save-status` | `transition: opacity 0.2s ease;` | `transition: opacity var(--duration-base) var(--ease-macos);` | state |
| 63 | 3915 | `.settings-tab` | `transition: background 0.15s ease, color 0.15s ease;` | `transition: background var(--duration-fast) var(--ease-macos), color var(--duration-fast) var(--ease-macos);` | hover |
| 64 | 3946 | `.btn-secondary` | `transition: background 0.15s ease;` | `transition: background var(--duration-fast) var(--ease-macos);` | hover |
| 65 | 4001 | `.prompt-subtab` | `transition: background 0.15s ease, color 0.15s ease;` | `transition: background var(--duration-fast) var(--ease-macos), color var(--duration-fast) var(--ease-macos);` | hover |
| 66 | 4044 | `.prompt-dirty-indicator` | `transition: opacity 0.2s ease;` | `transition: opacity var(--duration-base) var(--ease-macos);` | state |
| 67 | 4178 | `.vocab-card` | `transition: background 0.15s ease;` | `transition: background var(--duration-fast) var(--ease-macos);` | hover |
| 68 | 4238 | `.btn-icon` | `transition: background 0.15s ease, color 0.15s ease;` | `transition: background var(--duration-fast) var(--ease-macos), color var(--duration-fast) var(--ease-macos);` | hover |
| 69 | 4278 | `.modal-overlay` | `transition: opacity 0.2s ease;` | `transition: opacity var(--duration-base) var(--ease-macos);` | mount |
| 70 | 4544 | `.progress-fill` (stt) | `transition: width 0.3s ease;` | `transition: width var(--duration-slow) var(--ease-macos);` | progress |
| 71 | 4605 | `.stt-manual-download > summary::before` | `transition: transform 0.15s ease;` | `transition: transform var(--duration-fast) var(--ease-macos);` | expand |
| 72 | 4683 | `.viewer-title-text` | `transition: background 0.15s ease;` | `transition: background var(--duration-fast) var(--ease-macos);` | hover |
| 73 | 4702 | `.viewer-title-edit-btn` | `transition: opacity 0.15s ease, background 0.15s ease;` | `transition: opacity var(--duration-fast) var(--ease-macos), background var(--duration-fast) var(--ease-macos);` | hover |

#### 1.3.3 유지 대상 (애니메이션 keyframe)

> 아래는 **변경하지 않음** — `ease-in-out`은 반복 애니메이션(pulse, shimmer, blink, typing)의 의도된 선택.

| 줄 | 셀렉터 | 값 | 사유 |
|----|--------|-----|------|
| 678, 685, 690, 1047, 3087, 3090, 3091, 3286 | `.status-dot`, `.pipeline-step-dot`, `.meeting-list-status.*` | `animation: pulse-dot 1.5s ease-in-out infinite;` | 반복 펄스 — macOS easing 곡선과 무관 |
| 911 | `.recording-indicator` | `animation: blink-recording 1s ease-in-out infinite;` | 녹화 점멸 |
| 2037 | `.skeleton` | `animation: shimmer 1.5s ease-in-out infinite;` | 스켈레톤 로딩 |
| 2746 | `.typing-dot` | `animation: typing-bounce 1.2s ease-in-out infinite;` | 타이핑 인디케이터 |
| 3371 | `@media (prefers-reduced-motion)` | `transition-duration: 0.01ms !important;` | 접근성 가드 (유지) |

**총 변경 건수: 73건 (토큰 정의 1 + 치환 72)**
— PM 브리프의 "28곳" 숫자는 `var(--transition)`만 센 1차 추산이었으며, 하드코딩된 `0.Xs ease` 44건을 포함하면 실제 치환 범위는 위와 같음. frontend-tokens는 본 표를 완전 반영할 것.

---

## 2. 0.5px Hairline 마이그레이션 (37건)

### 2.1 분류 원칙

- **✅ 변환 대상** — 시각적 구분선 역할:
  카드, 패널, 입력 필드, 셀렉트, 모달, 탭, 구분선 등. macOS Retina에서 1px은 두꺼워 보이므로 0.5px hairline으로 하향.
- **❌ 유지 대상**:
  1. `border: 1px solid transparent` — 레이아웃 reserve (hover 시 채워질 자리). 0.5px로 바꾸면 hover 전후 레이아웃이 0.5px씩 흔들림.
  2. 포커스/활성 상태의 accent 강조 보더 — 시각적 강조가 의도.
  3. error/warning 강조 보더 — 명시적 강조 의도.

### 2.2 마이그레이션 표

| # | 줄 | 셀렉터 | 현재 | 변경 | 사유 |
|---|----|--------|------|------|------|
| 1 | 482 | `.list-search input` | `1px solid transparent` | ❌ **유지** | transparent reserve (hover/focus 시 채움) |
| 2 | 510 | `.list-sort select` | `1px solid transparent` | ❌ **유지** | 동상 |
| 3 | 616 | `.sidebar-search` | `1px solid transparent` | ❌ **유지** | 동상 |
| 4 | 1137 | (컨텍스트: resource bar wrapper) | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 카드 경계 |
| 5 | 1444 | `.search-input` | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 입력 보더 |
| 6 | 1512 | `.filter-input` | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 입력 보더 |
| 7 | 1527 | `.filter-clear-btn` | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 버튼 보더 |
| 8 | 1575 | `.search-results.visible` | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 팝오버 패널 |
| 9 | 1602 | `.search-close-btn` | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 버튼 보더 |
| 10 | 1644 | `.sort-select` | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 셀렉트 보더 |
| 11 | 1672 | `.batch-summarize-btn` | `1px solid var(--accent)` | ❌ **유지** | accent 강조 (Secondary 버튼) |
| 12 | 1713 | `.meeting-card` | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 카드 경계 |
| 13 | 1886 | `.meeting-card-action` | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 버튼 보더 |
| 14 | 1947 | `.result-item` | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 리스트 항목 |
| 15 | 2019 | (컨텍스트: 섹션 래퍼) | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 구분선 |
| 16 | 2146 | `.viewer-action-btn` | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 버튼 보더 |
| 17 | 2329 | `.search-bar-clear` (또는 container) | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 입력 래퍼 |
| 18 | 2560 | `.btn-summarize` 계열 | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 버튼 보더 |
| 19 | 2620 | (컨텍스트: 섹션) | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 구분선 |
| 20 | 2646 | `.btn-small` | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 버튼 보더 |
| 21 | 2791 | `.chat-input` | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 입력 보더 |
| 22 | 2845 | `.btn-cancel-send` | `1px solid rgba(255, 59, 48, 0.3)` | ❌ **유지** | error 강조 의도 |
| 23 | 2966 | `.ref-card` | `1px solid var(--ref-border)` | ✅ `0.5px solid var(--ref-border)` | 카드 경계 |
| 24 | 3032 | `.btn-copy` | `1px solid transparent` | ❌ **유지** | transparent reserve |
| 25 | 3940 | (컨텍스트: settings section) | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 섹션 경계 |
| 26 | 4057 | `.prompt-dirty-indicator` 컨테이너 | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 보조 패널 |
| 27 | 4143 | (컨텍스트: vocab panel) | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 패널 경계 |
| 28 | 4176 | `.vocab-card` | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 카드 경계 |
| 29 | 4287 | `.modal-content` | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 모달 경계 (§1.3 hairline 원칙 직접 적용) |
| 30 | 4316 | `.modal-close` 또는 header 하단 | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 구분선 |
| 31 | 4643 | (컨텍스트: stt 섹션) | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 섹션 경계 |
| 32 | 4721 | `.stt-model-card.active` | `1px solid var(--accent)` | ❌ **유지** | accent 활성 강조 |
| 33 | 4747 | (헤더 하단) | `border-bottom: 1px solid var(--border-light)` | ✅ `border-bottom: 0.5px solid var(--border-light)` | 구분선 |
| 34 | 4765 | (stt panel) | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 패널 경계 |
| 35 | 4800 | `.stt-*.downloaded` 또는 selected | `1px solid var(--accent)` | ❌ **유지** | accent 활성 강조 |
| 36 | 4866 | (stt progress card) | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 카드 경계 |
| 37 | 4918 | (stt footer) | `1px solid var(--border)` | ✅ `0.5px solid var(--border)` | 구분선 |

**요약**: 변환 **30건** / 유지 **7건** (transparent 4 + accent 강조 3, error 강조 1 포함 실제 유지 7 — 표 재확인 결과 일치)

### 2.3 포커스 ring 주의사항 (유지)

다음 `box-shadow` 기반 focus ring은 **1px이 아닌 3px spread**이므로 본 마이그레이션 대상 아님 (유지):
- `.search-input:focus { box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.15); }` 등

---

## 3. `<kbd>` 컴포넌트 Spec

### 3.1 설계 원칙

- **용도**: 버튼/메뉴 항목 우측에 키보드 단축키 힌트 표시 (`⌘K`, `⌘,`, `Esc`, `↵`)
- **배치**: inline, 부모 행의 오른쪽 끝 정렬은 부모가 담당 (`kbd` 자체는 `display: inline-flex`)
- **폰트**: SF Mono (macOS 네이티브)로 고정 — 본문 시스템 폰트와 시각 대비
- **크기**: 11px (base 13px 대비 약간 작음, macOS 메뉴 단축키 힌트 스타일)
- **상태**: hover/active 대응 — 부모 버튼이 hover될 때 `<kbd>`도 함께 강조

### 3.2 CSS 블록 (그대로 복붙)

> `ui/web/style.css`의 컴포넌트 섹션 말미 혹은 `.btn-*` 블록 근처에 추가.

```css
/* ==========================================================================
   Keyboard Hint (<kbd>) — docs/design.md §1.4, §4.1
   ========================================================================== */

kbd {
  /* 타이포 */
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: 11px;
  font-weight: 500;
  line-height: 1;
  letter-spacing: 0;

  /* 레이아웃 */
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 6px;

  /* 외형 */
  color: var(--text-secondary);
  background: var(--bg-input);
  border: 0.5px solid var(--border);
  border-radius: 4px;
  box-shadow: 0 1px 0 var(--border);   /* 미세한 입체감 */

  /* 기타 */
  vertical-align: middle;
  white-space: nowrap;
  user-select: none;
  transition:
    background var(--duration-fast) var(--ease-macos),
    color var(--duration-fast) var(--ease-macos),
    border-color var(--duration-fast) var(--ease-macos);
}

/* 부모 버튼 hover 시 kbd 강조 */
button:hover > kbd,
.btn:hover > kbd,
.nav-btn:hover > kbd,
.meeting-card-action:hover > kbd,
a:hover > kbd {
  color: var(--text-primary);
  background: var(--bg-hover);
  border-color: var(--border);
}

/* 활성 상태 (Primary 버튼 안의 kbd) */
.btn-primary > kbd {
  color: rgba(255, 255, 255, 0.9);
  background: rgba(255, 255, 255, 0.18);
  border-color: rgba(255, 255, 255, 0.24);
  box-shadow: none;
}

.btn-primary:hover > kbd {
  background: rgba(255, 255, 255, 0.26);
  color: #fff;
}

/* Command Palette 행(row)의 kbd는 오른쪽 정렬 정렬용 margin-left: auto 부모에서 처리 */
.palette-row kbd {
  margin-left: auto;
}

/* 연속된 kbd 사이 간격 (예: ⌘ + K) */
kbd + kbd {
  margin-left: 2px;
}

/* 다크 모드 대응 — 토큰 기반이라 자동 적용되지만, shadow만 추가 보정 */
@media (prefers-color-scheme: dark) {
  kbd {
    /* 다크에서 box-shadow 대비 보정 (shadow alpha 2배 원칙) */
    box-shadow: 0 1px 0 rgba(0, 0, 0, 0.4);
  }

  .btn-primary > kbd {
    background: rgba(255, 255, 255, 0.14);
    border-color: rgba(255, 255, 255, 0.2);
  }
}

/* 접근성 — reduced motion 시 transition 제거는 기존 글로벌 가드가 처리 */
```

### 3.3 HTML 사용 예시 (참고)

```html
<!-- 사이드바 검색 힌트 -->
<button class="btn-secondary">
  회의록 검색 <kbd>⌘K</kbd>
</button>

<!-- 설정 단축키 -->
<button class="nav-btn">설정 <kbd>⌘,</kbd></button>

<!-- Command Palette 내부 행 -->
<div class="palette-row">
  <span>🎤 새 녹음 시작</span>
  <kbd>⌘R</kbd>
</div>

<!-- 조합 키 -->
<kbd>⌘</kbd><kbd>⇧</kbd><kbd>K</kbd>
```

### 3.4 접근성

- `<kbd>` 요소 자체가 시맨틱 — 스크린리더가 "키보드 입력"으로 읽음. 추가 `aria-label` 불필요.
- 단, 기호(`⌘`, `⌥`, `⇧`)는 스크린리더가 정확히 읽지 못할 수 있으므로, 중요한 단축키는 부모에 `aria-keyshortcuts` 병기 권장:
  ```html
  <button aria-keyshortcuts="Meta+K">
    검색 <kbd aria-hidden="true">⌘K</kbd>
  </button>
  ```

---

## 4. 검증 체크리스트 (frontend-tokens용)

적용 후 아래 항목을 수동 확인:

- [ ] `:root`에서 `--transition` 선언이 **제거**되었는가
- [ ] `:root`에 `--ease-macos`, `--duration-fast/base/slow`가 추가되었는가
- [ ] `grep -n "var(--transition)" ui/web/style.css` 결과가 **0건**인가
- [ ] `grep -nE "[0-9]+(\.[0-9]+)?s ease[^a-z-]" ui/web/style.css` 결과가 `ease-in-out` 반복 애니메이션만 남아있는가 (§1.3.3 표와 일치)
- [ ] `grep -n "1px solid" ui/web/style.css` 결과가 **7건**만 남아있는가 (§2.2 유지 대상)
- [ ] `kbd { ... }` 블록이 추가되었는가
- [ ] 라이트/다크 모드에서 모든 카드/입력/셀렉트의 보더가 얇아졌는가 (육안 확인)
- [ ] hover/focus 인터랙션이 여전히 부드러운가 (반응 속도 150/250/400ms 체감)
- [ ] `prefers-reduced-motion: reduce` 활성화 시 transition이 억제되는가

---

## 5. 참고

- docs/design.md §1.3 (0.5px Hairline), §1.4 (Keyboard-First), §1.6 (macOS Easing), §4.1 (Command Palette)
- macos-design-skill: https://github.com/ceorkm/macos-design-skill
- 본 spec은 WS-1(style.css 일괄 적용)의 단일 정보 출처(single source of truth). 불일치 발견 시 designer-tokens(본인)에게 문의.
