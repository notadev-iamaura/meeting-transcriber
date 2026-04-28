# Dark Mode Tones + Light Token 보강 시각 정의 (T-102)

> Designer-A 산출물. 본 컴포넌트는 **마크업 없는 토큰 정리** 작업이라 베이스라인 PNG 없음 (mockup 1개만).
>
> 참조:
> - Plan: `docs/superpowers/plans/2026-04-28-ui-ux-wave-1-dark-mode-tones.md`
> - 디자인 가이드: `docs/design.md` §1.1 / §2.2
> - T-101 인계: `docs/superpowers/ui-ux-overhaul/wave-1/empty-state-mockup.md` §6
> - Spec: `docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md` §1.2 비목표

---

## 1. 목적

세 가지 목적을 동시에 달성한다.

1. **T-101 의 라이트 모드 색대비 결함 정리** — T-101 mockup §6.3 이 후속 티켓화 권고한 3가지 위반 (라이트 `--text-secondary` 3.62:1, `--text-muted` 2.21:1, `--accent` on body text 4.02:1) 을 토큰 보강으로 해소한다. 본 컴포넌트만의 개별 색이 아닌 **design.md 토큰 갱신** 으로 SPA 전역 secondary 텍스트 100+ 개소가 자동 동기화되게 한다.

2. **design.md §1.1 다크 단계 톤 (`#1C1C1E / #2C2C2E / #3A3A3C`) 확인** — 다크 배경 톤 격차는 §1.1 의 "큰 톤 격차" 패턴을 이미 충족하고 있음을 본 mockup §3 측정값으로 재확인한다. 다크 배경 토큰은 **변경하지 않는다**.

3. **신규 `--accent-text` 토큰 도입** — Apple System Blue (`#007AFF`) 자체는 보존하되, **본문 텍스트(body text) 용도** 의 AA 통과 색상을 별도 토큰으로 분리한다. 이는 design.md 토큰 *보강* 이며 새 디자인 언어 도입 아님 (spec §1.2 비목표 위반 아님 — §6 에서 정당화).

---

## 2. 토큰 변경 표 (라이트)

`@bg-canvas: #FFFFFF` 위에 측정.

| 토큰 | 기존 | 변경 | 측정값 | 기준 | 결과 |
|------|------|------|--------|------|------|
| `--text-secondary` | `#86868B` | **`#6E6E73`** | **5.07:1** | AA 본문 4.5:1 | ✅ 통과 |
| `--text-muted` | `#AEAEB2` | **`#8E8E93`** | **3.26:1** | UI 비텍스트 1.4.11 3:1 | ✅ 통과 |
| `--accent-text` (신규) | (없음) | **`#0066CC`** | **5.57:1** | AA 본문 4.5:1 | ✅ 통과 |
| `--accent` (보존) | `#007AFF` | (변경 없음) | 4.02:1 | UI 컴포넌트 3:1 | ✅ 통과 (보더용) |

**`--accent` 보존 이유**: Apple System Blue (`#007AFF`) 는 macOS 시스템 표준이며 디자인 언어 일관성의 핵심. UI 컴포넌트 보더(3:1) 기준은 통과하므로 보더·아이콘·시스템 강조용으로 유지. 본문 텍스트 가독성이 필요한 위치만 신설된 `--accent-text` (`#0066CC`) 를 사용한다.

> ⚠️ **plan 1.3 토큰 명세 와의 미세 차이**:
> Plan 의 `--text-muted #8E8E93 on #FFFFFF = 3.51:1` 값은 본 mockup 실측 (Python WCAG 2.x 공식) 결과 **3.26:1** 으로 나타난다. 차이의 원인은 plan 작성 시 추정값을 표기한 것. 두 값 모두 UI 비텍스트 3:1 기준은 통과하므로 토큰 변경 자체는 유효하며, **본 mockup 측정값을 진실 공급원으로 채택**. Plan 의 §3.5 "토큰 변경 명세" 표는 본 mockup 측정값으로 정정 권고 (Designer-B 검토 시 PM-A 에 회부).

---

## 3. 토큰 변경 표 (다크)

`@bg-canvas: #1C1C1E` (design.md §1.1) 위에 측정.

| 토큰 | 기존 | 변경 | 측정값 | 기준 | 결과 |
|------|------|------|--------|------|------|
| `--text-muted` | `#636366` | **`#8E8E93`** | **5.22:1** | UI 3:1 + AA 본문 4.5:1 | ✅ 통과 |
| `--accent-text` (신규) | (없음) | **`#4DA1FF`** | **6.37:1** | AA 본문 4.5:1 | ✅ 통과 (AAA 미달) |

**다른 다크 토큰은 변경 없음**. design.md §1.1 의 "큰 톤 격차" 패턴 (`#1C1C1E / #2C2C2E / #3A3A3C`) 은 이미 깊이감 디자인 의도를 충족.

> ⚠️ **plan 1.3 토큰 명세 와의 미세 차이**:
> - 다크 `--text-muted #8E8E93` on `#1C1C1E`: plan 명시 **4.62:1** vs 실측 **5.22:1** — 실측이 더 좋음 (UI 3:1 + AA 4.5:1 모두 충족).
> - 다크 `--accent-text #4DA1FF` on `#1C1C1E`: plan 명시 **≥7:1 (AAA)** vs 실측 **6.37:1** — AAA(7:1) 에는 미달이며 **AA(4.5:1) 통과** 로 정정. 본 작업의 원래 목표는 AA 통과이므로 토큰은 그대로 유지하고 §3 표의 기준 라벨만 "AA 본문 4.5:1" 로 정정.

### 3.1 다크 단계 톤 (§1.1) 검증 — 변경 없음 확인

| 비교 | 비율 | 의도 |
|------|------|------|
| `--bg-canvas` `#1C1C1E` ↔ `--bg-card` `#2C2C2E` | 1.221:1 | 카드가 캔버스 위로 살짝 부상 — ✓ |
| `--bg-card` `#2C2C2E` ↔ `--bg-input` `#3A3A3C` | 1.228:1 | 입력 필드가 카드 안에서 한 단계 더 부상 — ✓ |
| `--bg-canvas` `#1C1C1E` ↔ `--bg-input` `#3A3A3C` | 1.499:1 | 캔버스→입력 누적 격차 — ✓ |

라이트 모드 (`#FFFFFF / #F5F5F7 / #FAFAFA`) 와 비교해 다크가 의도적으로 큰 격차를 가지므로 깊이감이 유지된다. 토큰 변경 불필요.

---

## 4. WCAG 검증 (직접 계산)

### 4.1 검증 알고리즘

WCAG 2.x relative luminance 공식 (W3C 정의):

```python
def relative_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i+2], 16) / 255 for i in (0, 2, 4))
    def chan(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)

def contrast(c1: str, c2: str) -> float:
    L1, L2 = sorted([relative_luminance(c1), relative_luminance(c2)], reverse=True)
    return (L1 + 0.05) / (L2 + 0.05)
```

### 4.2 측정값 (Python 실행 결과)

```
LIGHT MODE — bg-canvas = #FFFFFF
  --text-secondary  #6E6E73 on #FFFFFF = 5.07:1   (AA 4.5:1 ✓)
  --text-muted      #8E8E93 on #FFFFFF = 3.26:1   (UI 3:1   ✓)
  --accent-text     #0066CC on #FFFFFF = 5.57:1   (AA 4.5:1 ✓)
  # 참고
  --accent          #007AFF on #FFFFFF = 4.02:1   (UI 3:1   ✓ 보더용)

DARK MODE — bg-canvas = #1C1C1E
  --text-muted      #8E8E93 on #1C1C1E = 5.22:1   (UI 3:1 + AA 4.5:1 ✓)
  --accent-text     #4DA1FF on #1C1C1E = 6.37:1   (AA 4.5:1 ✓, AAA 7:1 미달)
  # 참고 (변경 없는 토큰 검증)
  --text-primary    #F5F5F7 on #1C1C1E = 15.63:1  (✓)
  --text-secondary  #98989D on #1C1C1E = 5.93:1   (✓)
```

### 4.3 통합 표 (라이트 3 + 다크 2)

| 모드 | 토큰 | 색상 | 배경 | 측정 | 기준 | 결과 |
|------|------|------|------|------|------|------|
| Light | `--text-secondary` | `#6E6E73` | `#FFFFFF` | **5.07:1** | AA 4.5:1 | ✅ |
| Light | `--text-muted` | `#8E8E93` | `#FFFFFF` | **3.26:1** | 1.4.11 3:1 | ✅ |
| Light | `--accent-text` | `#0066CC` | `#FFFFFF` | **5.57:1** | AA 4.5:1 | ✅ |
| Dark | `--text-muted` | `#8E8E93` | `#1C1C1E` | **5.22:1** | 1.4.11 3:1 + AA 4.5:1 | ✅ |
| Dark | `--accent-text` | `#4DA1FF` | `#1C1C1E` | **6.37:1** | AA 4.5:1 | ✅ |

**모든 변경 토큰이 의도한 기준을 통과한다.**

---

## 5. T-101 영향 분석

본 토큰 변경이 T-101 (empty-state) 산출물에 미치는 영향 3가지를 정리한다. Frontend-A 가 본 작업 7.4 ~ 7.6 단계에서 처리한다.

### 5.1 베이스라인 PNG 3종 재캡처 필요

T-101 은 `tests/ui/visual/baselines/empty-state-{light,dark,mobile}.png` 3종을 베이스라인으로 두며, fixture HTML 이 `ui/web/style.css` 의 토큰을 그대로 참조하는 구조 (T-101 mockup §9 참조). 본 작업에서:

- 라이트 `--text-secondary` 색상이 `#86868B` → `#6E6E73` 로 어두워진다 → 빈 상태 설명 텍스트의 픽셀 RGB 가 변한다.
- 다크 `--text-muted` 색상이 `#636366` → `#8E8E93` 로 밝아진다 → 빈 상태 아이콘 SVG 의 `currentColor` 픽셀 RGB 가 변한다.
- 라이트 `--text-muted` 색상이 `#AEAEB2` → `#8E8E93` 로 어두워진다 → 빈 상태 아이콘 픽셀 RGB 가 변한다.

→ visual diff 가 ≥1% 발생할 것이 자명하므로 **3종 모두 재캡처 필수**. plan §7.5 의 `/tmp/recapture_t101_for_t103.py` 스크립트가 이 작업을 담당한다 (Frontend-A).

### 5.2 a11y 테스트 color-contrast 룰 재활성화 가능

T-101 의 `tests/ui/a11y/test_empty_state.py::test_empty_state_no_a11y_violations` 는 라이트 모드 색대비 미달 (T-101 mockup §6.1) 때문에 `axe-playwright-python` 의 `"rules": {"color-contrast": {"enabled": False}}` 로 룰을 비활성화한 채 통과시켰다 (T-103 인계 메모).

본 작업으로 라이트 토큰이 AA 통과로 보강되므로:

- `tests/ui/a11y/test_empty_state.py` 에서 `color-contrast: enabled: False` 라인 제거
- docstring / assert 메시지의 "deferred to T-103" 표현을 "재활성화 완료" 로 정정
- 룰 재활성화 후 a11y 테스트 3/3 PASS 가 자연스럽게 달성됨 (라이트 secondary/muted/accent 모두 AA 통과로 axe-core 도 합격 처리)

→ Frontend-A 가 plan §7.4 단계에서 처리.

### 5.3 T-101 mockup §6 표 미세 수치 정정

T-101 mockup §6 의 색대비 측정값 표는 두 가지 수정이 필요:

**5.3.1 기존 측정값 미세 정정** (Designer-B 가 T-101 검토 시 권고했던 사항):

| 표 | 셀 | 기존 표기 | 정정 |
|----|----|---------|------|
| §6.1 | 설명 (text-secondary `#86868B` on `#F5F5F7`) | 3.45:1 | 3.33:1 |
| §6.2 | 설명 (text-secondary `#98989D` on `#2C2C2E`) | 5.29:1 | 4.85:1 |

**5.3.2 본 토큰 변경 후 새 측정값으로 갱신**:

라이트 모드 §6.1 표의 후속 갱신값 (Frontend-A 가 §7.6 에서 반영):

| 요소 | 색상 | 배경 | 신규 측정 | 기준 | 결과 |
|------|------|------|----------|------|------|
| 설명 (text-secondary) | `#6E6E73` | `#FFFFFF` | **5.07:1** | AA 4.5:1 | ✅ |
| 설명 (text-secondary) | `#6E6E73` | `#F5F5F7` | (T-101 재측정 필요) | AA 4.5:1 | (예상 ≈ 4.83:1, 통과) |
| 아이콘 (text-muted) | `#8E8E93` | `#FFFFFF` | **3.26:1** | 1.4.11 3:1 | ✅ |
| CTA 라벨 (accent-text) | `#0066CC` | `#FFFFFF` | **5.57:1** | AA 4.5:1 | ✅ |

→ T-101 mockup §6.3 의 "라이트 모드의 3 가지 잠재 위반" 절은 **본 작업 완료 후 효력 상실** 이므로 §6.3·§6.4 본문도 "라이트/다크 모두 AA 통과 (T-103 토큰 보강 후)" 로 갱신.

---

## 6. spec §1.2 비목표 점검

### 6.1 신규 `--accent-text` 토큰 도입의 정당성

Spec §1.2 비목표 4번째 항목: *"디자인 언어 자체의 전면 개편 (`docs/design.md` 의 토큰을 보강할 뿐 새 언어 도입 없음)"*.

- **토큰 1개 추가는 보강이지 개편이 아니다**. design.md §2.2 는 이미 `--accent`, `--accent-hover`, `--success`, `--warning`, `--error` 등 의미 토큰을 가진 구조이며, `--accent-text` 도입은 *동일 의미 그룹 내 본문 텍스트 용도 분리* 에 불과.
- Apple HIG 자체가 시스템 컬러 (System Blue) 와 본문 컬러 (Label Color) 를 분리하는 패턴을 따르므로, design.md 의 macOS 정합성 강화 목적과 부합.
- 새 컬러 팔레트·테마·스킴 도입 없음. 새 디자인 언어 도입 없음. → spec §1.2 허용 범위.

### 6.2 SPA 마크업 변경 0

본 컴포넌트는 토큰만 변경하고 `ui/web/spa.js` 마크업은 한 줄도 건드리지 않는다 (Frontend-A 작업 영역에서도 `style.css` + `design.md` 만 수정). T-101 의 a11y 테스트 코드 변경은 *테스트 인프라* 이지 *마크업* 이 아니므로 spec §1.2 비목표 1번째 (백엔드 API 변경) 영향 없음.

### 6.3 신규 런타임 의존성 0

Plan 에서 추가 의존성 없음을 확인. 본 mockup 작성·검증에 사용한 Python `int`/`float` 산술은 표준 라이브러리. axe-playwright-python·Pillow 는 spec §1.2 가 명시 허용한 테스트 전용 의존성. → spec §1.2 비목표 4번째 (신규 런타임 의존성 추가) 위반 없음.

---

## 7. 자가 검증 체크리스트 결과

본 mockup 의 자가 검증.

- [x] **모든 토큰 16진 값이 plan 1.3 토큰 명세와 1:1 일치** — §2 라이트 3개 (`#6E6E73 / #8E8E93 / #0066CC`) + §3 다크 2개 (`#8E8E93 / #4DA1FF`) 모두 plan §3.5 와 hex 단위 일치. 측정값 차이는 §2/§3 의 ⚠️ 박스에서 정직 보고.
- [x] **WCAG 검증 직접 계산값 표 포함** — §4.1 알고리즘 + §4.2 Python 실행 출력 + §4.3 통합 표 (라이트 3 + 다크 2 = 5개 측정).
- [x] **T-101 영향 분석 3 항목 명시** — §5.1 베이스라인 재캡처 + §5.2 룰 재활성화 + §5.3 mockup §6 정정.
- [x] **spec §1.2 비목표 점검 명시** — §6.1 토큰 보강의 정당성 + §6.2 SPA 마크업 변경 0 + §6.3 신규 런타임 의존성 0.
- [x] **Designer-A 절대 금지 조항 준수** — `ui/web/*` 직접 변경 0, `docs/design.md` 직접 변경 0, 베이스라인 PNG 신설 0.
- [x] **본 컴포넌트는 시각 베이스라인 없음** — 토큰만 변경하므로 `tests/ui/visual/baselines/dark-mode-tones-*.png` 산출 없음 (plan 명시).

---

## 8. Frontend-A 에게 — 구현 시 유의점

토큰 변경은 다음 5 위치에 동기화된다 (plan §7.1 ~ §7.6 그대로).

1. **`docs/design.md` §2.2** Light 블록의 `--text-secondary`, `--text-muted` 갱신 + `--accent-text` 신규 추가.
2. **`docs/design.md` §2.2** Dark 블록의 `--text-muted` 갱신 + `--accent-text` 신규 추가.
3. **`ui/web/style.css` `:root`** 동일 변경 (라이트).
4. **`ui/web/style.css`** `@media (prefers-color-scheme: dark)` 또는 `[data-theme="dark"]` 블록 (다크).
5. T-101 영향 정리 — `tests/ui/a11y/test_empty_state.py` 룰 재활성화, baseline 3종 재캡처, T-101 mockup §6 정정.

**금지 사항**:
- `--accent` (`#007AFF` / `#0A84FF`) 자체는 절대 변경 금지 — Apple System Blue 표준 보존.
- 다크 모드 배경 토큰 (`--bg-canvas / --bg-card / --bg-input`) 절대 변경 금지 — §1.1 단계 톤 의도성 보존.
- design.md 새 의미 토큰 도입 금지 — `--accent-text` 1 개만 신설.

`--accent-text` 가 실제로 *어디에 적용되는지* 는 본 mockup 의 책임 범위 외 (Frontend-A 가 SPA 코드 grep 으로 결정). 일반적으로 `<a>` 링크, CTA 버튼 라벨, "더 보기" 류 본문 텍스트형 액센트 위치에 점진 적용.

---

## 9. 변경 이력

| 일시 | 변경 | 작성자 |
|------|------|--------|
| 2026-04-28 | 초안 작성 (T-102 Designer-A 산출물) | designer-a |
