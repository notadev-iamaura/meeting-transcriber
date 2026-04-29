# ARIA 동기화 — Mockup (T-301)

> **Wave 3 / 항목 6** · spec `2026-04-28-ui-ux-overhaul-design.md` §3 항목 6
> **Producer**: UI/UX Designer-A
> **Consumer**: Frontend (구현), QA-A (fixture · a11y · 시각 회귀)
> **컴포넌트**: `aria-sync`
> **베이스라인**: `tests/ui/visual/baselines/aria-sync-{light,dark,mobile}.png`

---

## 0. 배경 (Why)

`ui/web/index.html` + `ui/web/spa.js` 분석 결과, ARIA 동적 상태가 **부분적으로만**
동기화되고 있다. spec §3 항목 6 이 요구하는 3 가지 속성(`aria-selected`,
`aria-current`, `aria-expanded`) 중 현재 상태:

| 속성 | 위치 | 현재 동기화 여부 |
|------|------|-----------------|
| `aria-selected` | 사이드바 회의 항목 (`#listContent` 내 `[role="option"]`) | ✅ `MeetingList.render()` 1006-1011, `MeetingList.setActive()` 1086-1099 |
| `aria-selected` | 뷰어 탭 (`role="tab"`) | ✅ 1652/1656 (정적) + 동적 갱신 |
| `aria-selected` | 프롬프트 서브탭 | ✅ 7154-7156, 7278 |
| **`aria-current`** | **`#nav-bar` 의 `.nav-btn`** | ❌ **누락** — `NavBar.setActiveFromPath()` (133-149) 가 `.active` 클래스만 토글, ARIA 속성 미설정 |
| **`aria-expanded`** | **사이드바 검색·정렬·`<details>` 등 토글류** | ❌ **부분 누락** — `<details class="stt-manual-download">` (spa.js:6783) 는 native 요소라 브라우저가 자동 처리하나, JS 로 만든 토글(설정 패널 등)은 누락 |
| `role="listbox"` ↔ `role="option"` 부모-자식 | `index.html:90` + `spa.js:1001` | ✅ DOM 구조 정합 |

본 티켓은 위 **3 누락 케이스를 단일 ARIA 동기화 계약으로 통일**하고, axe-core
의 `aria-allowed-attr`, `aria-required-parent`, `aria-valid-attr-value` 룰을
무결성 통과시키는 것이 목표다.

---

## §1 ARIA 의미 매핑 표

본 mockup 의 단일 진실 공급원. Frontend 구현·QA fixture·axe 룰셋 모두 이 표를
참조한다.

| 영역 | role | 동적 속성 | 값 / 갱신 트리거 |
|------|------|-----------|----------------|
| 사이드바 회의 목록 컨테이너 | `listbox` | `aria-label="회의 목록"` | 정적 (이미 `index.html:90`) |
| 사이드바 회의 항목 | `option` | `aria-selected` | `true`/`false` — `MeetingList.setActive(meetingId)` 호출마다 모든 항목 갱신 (이미 구현) |
| 사이드바 회의 항목 | `option` | `tabindex` | `0` (이미 구현) |
| 네비게이션 링크 (`.nav-btn`) | (`button` 의 native role 유지) | **`aria-current`** | `"page"` (활성) / 속성 제거 (비활성) — `NavBar.setActiveFromPath(path)` 에서 활성 1 개에만 부여 |
| 뷰어 상단 탭 | `tab` | `aria-selected` | `true`/`false` — 탭 전환 시 (이미 구현) |
| 뷰어 탭 패널 | `tabpanel` | `aria-labelledby` | 정적 (이미 구현 1662) |
| 프롬프트 서브탭 | `tab` | `aria-selected` | `true`/`false` (이미 구현 7278) |
| 토글 가능 메뉴·패널 (JS 제어) | `button` | **`aria-expanded`** | `"true"` (열림) / `"false"` (닫힘) — 토글 핸들러에서 동기화 |
| Native `<details>` | (`group` native role) | (browser 가 `aria-expanded` 자동 동기) | 추가 작업 불필요 |

### §1.1 속성 값 정규화 규칙

axe-core `aria-valid-attr-value` 통과를 위한 엄격 규칙.

- `aria-selected`: 반드시 문자열 `"true"` 또는 `"false"` (boolean 직접 전달 금지)
- `aria-current`: 활성일 때만 `"page"` 부여, 비활성은 **속성 자체를 제거** (`removeAttribute`)
  → `aria-current="false"` 는 의미상 valid 하나 macOS VoiceOver 가 "current page" 를
  여전히 읽는 버그 가능성이 있어 제거 패턴 강제
- `aria-expanded`: 반드시 `"true"`/`"false"` (제거 금지 — 트리거 버튼은 항상 상태 명시)

### §1.2 SR (스크린리더) 발화 기대

| 사용자 액션 | macOS VoiceOver 기대 발화 |
|------------|--------------------------|
| 사이드바 회의 항목 포커스 (활성) | "회의 제목, 선택됨, 옵션, 1/N" |
| 사이드바 회의 항목 포커스 (비활성) | "회의 제목, 옵션, 2/N" |
| nav-btn `회의록` 포커스 (현재 라우트) | "회의록, 현재 페이지, 버튼" |
| nav-btn `검색` 포커스 (다른 라우트) | "검색, 버튼" |
| 토글 버튼 포커스 (열림) | "라벨, 확장됨, 버튼" |
| 토글 버튼 포커스 (닫힘) | "라벨, 축소됨, 버튼" |

---

## §2 적용 위치 (구현 매핑)

### §2.1 listbox + option (이미 구현 — 회귀 방지만 추가)

`spa.js:1086-1099` 의 `MeetingList.setActive()` 가 모든 항목 순회 후 `aria-selected`
를 갱신한다. **본 티켓 변경 없음**. QA-A 가 회귀 방지 시나리오만 추가.

```javascript
// 이미 구현된 형태 (참고용 — 변경 금지)
function setActive(meetingId) {
    _activeId = meetingId;
    var items = _listEl.querySelectorAll(".meeting-item");
    items.forEach(function (item) {
        var itemId = item.getAttribute("data-meeting-id");
        if (itemId === meetingId) {
            item.classList.add("active");
            item.setAttribute("aria-selected", "true");
        } else {
            item.classList.remove("active");
            item.setAttribute("aria-selected", "false");
        }
    });
}
```

### §2.2 `aria-current="page"` — nav-btn 라우터 동기화 (신규)

**현재 코드 (spa.js:133-149)**:
```javascript
function setActiveFromPath(path) {
    var pathname = path.split("?")[0];
    _buttons.forEach(function (btn) {
        var route = btn.getAttribute("data-route");
        btn.classList.remove("active");

        if (route === "/app") {
            if (pathname === "/app" || pathname === "/app/" || pathname.indexOf("/app/viewer/") === 0) {
                btn.classList.add("active");
            }
        } else if (route === pathname) {
            btn.classList.add("active");
        }
    });
}
```

**Frontend 구현 시 변경 (T-301-impl)**:
```javascript
function setActiveFromPath(path) {
    var pathname = path.split("?")[0];
    _buttons.forEach(function (btn) {
        var route = btn.getAttribute("data-route");
        btn.classList.remove("active");
        btn.removeAttribute("aria-current");  // ← 신규: 비활성에서 속성 제거

        var isActive = false;
        if (route === "/app") {
            isActive = (pathname === "/app" || pathname === "/app/" ||
                        pathname.indexOf("/app/viewer/") === 0);
        } else if (route === pathname) {
            isActive = true;
        }

        if (isActive) {
            btn.classList.add("active");
            btn.setAttribute("aria-current", "page");  // ← 신규: 활성에만 부여
        }
    });
}
```

> ⚠️ **반드시 `setAttribute("aria-current", "page")` — boolean `true` 금지**.
> `aria-current` 는 enumerated value (`page`/`step`/`location`/`date`/`time`/`true`/`false`).
> SPA 라우트는 의미상 "현재 페이지" 이므로 `"page"` 가 정답.

### §2.3 `aria-expanded` — JS 제어 토글 (신규)

대상 후보 (Frontend 가 grep 으로 확인 후 적용):

```bash
# 토글 핸들러 후보 (JS 로 열림/닫힘 상태를 직접 관리하는 버튼들)
grep -n "aria-expanded\|toggleAttribute\|classList.toggle" ui/web/spa.js
```

발견 예상 패턴:
1. **사이드바 폴더·그룹 토글** (있는 경우)
2. **뷰어 액션 메뉴 (overflow)** — `viewerActions` 내 추가 액션 드롭다운 (있는 경우)
3. **설정 페이지 collapsible 섹션** — JS 로 만든 경우만 (native `<details>` 는 자동)

**구현 패턴 (재사용 가능 헬퍼)**:
```javascript
/**
 * 토글 버튼의 aria-expanded 상태를 갱신한다.
 * @param {HTMLElement} button - 트리거 버튼
 * @param {boolean} isOpen - 패널 열림 여부
 */
function syncExpanded(button, isOpen) {
    button.setAttribute("aria-expanded", isOpen ? "true" : "false");
}

// 사용처
button.addEventListener("click", function () {
    var open = panel.classList.toggle("open");
    syncExpanded(button, open);
});
```

> 💡 **Native `<details>` 는 건드리지 말 것**. spa.js:6783 의
> `details.className = "stt-manual-download"` 같은 native `<details>` 는
> 브라우저가 자동으로 `aria-expanded` 동기화한다. JS 로 추가 처리하면
> 오히려 중복·충돌 위험.

### §2.4 role 의미 정합성 검증

axe-core 의 `aria-required-parent` / `aria-required-children` 룰 통과 보장.

| 부모 role | 직계 자식 role | 현재 |
|-----------|---------------|-----|
| `listbox` (`#listContent`) | `option` (`.meeting-item`) | ✅ |
| `tablist` (`#viewerTabNav`) | `tab` (`.tab-btn`) | ✅ |
| `tab` (`#viewerTabTranscript`) | (없음 — leaf) | ✅ |
| `tabpanel` (`#viewerPanelTranscript`) | (자유) | ✅ |

> ⚠️ **중첩 금지 케이스**: `role="option"` 안에 다시 `role="button"` 을 넣으면
> axe `aria-allowed-role` 위반. 회의 항목(`.meeting-item`) 안의 액션 아이콘은
> `<button>` native 요소로 두되, 이벤트 버블링은 `event.stopPropagation()` 으로
> 차단해 listbox 동작과 분리.

---

## §3 fixture 마크업 (QA-A 가 만들 인터페이스)

`tests/ui/visual/fixtures/aria-sync.html` 또는 `/tmp/aria-sync-preview.html` 에
다음 HTML 을 배치한다. **3 영역(listbox / nav / expandable) 동시 표시**.

```html
<!doctype html>
<html lang="ko" data-theme="light">
<head>
  <meta charset="utf-8">
  <title>aria-sync fixture (T-301)</title>
  <link rel="stylesheet" href="/static/style.css">
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: var(--bg-canvas);
      color: var(--text-primary);
      padding: 32px;
      display: flex;
      flex-direction: column;
      gap: 32px;
      max-width: 520px;
      margin: 0 auto;
    }
    section { display: flex; flex-direction: column; gap: 12px; }
    h2 { font-size: 13px; font-weight: 600; color: var(--text-secondary); margin: 0;
         text-transform: uppercase; letter-spacing: 0.04em; }
    .listbox-demo { background: var(--bg-sidebar); border-radius: var(--radius);
                    padding: 8px; border: 0.5px solid var(--border); }
    .listbox-demo [role="option"] {
      padding: 10px 12px; border-radius: var(--radius); cursor: pointer;
      display: flex; gap: 8px; align-items: center;
    }
    .listbox-demo [role="option"][aria-selected="true"] {
      background: var(--accent); color: #fff;
    }
    .nav-demo { display: flex; gap: 4px; padding: 8px; background: var(--bg-sidebar);
                border-radius: var(--radius); border: 0.5px solid var(--border); }
    .nav-demo .nav-btn {
      padding: 8px 14px; border: none; background: transparent;
      color: var(--text-primary); border-radius: var(--radius);
      cursor: pointer; font-size: 13px;
    }
    .nav-demo .nav-btn[aria-current="page"] {
      background: var(--accent); color: #fff;
    }
    .toggle-demo { display: flex; flex-direction: column; gap: 8px; }
    .toggle-demo button {
      padding: 8px 12px; background: var(--bg-secondary);
      border: 0.5px solid var(--border); border-radius: var(--radius);
      cursor: pointer; text-align: left; font-size: 13px; color: var(--text-primary);
    }
    .toggle-demo .panel {
      padding: 12px; background: var(--bg-secondary);
      border-radius: var(--radius); font-size: 12px; color: var(--text-secondary);
    }
    .toggle-demo .panel[hidden] { display: none; }
  </style>
</head>
<body>
  <h1 style="font-size:17px;font-weight:600;margin:0;">ARIA Sync Fixture</h1>

  <!-- 1. listbox + 3 option (1개 selected) -->
  <section>
    <h2>Listbox</h2>
    <div role="listbox" aria-label="회의 목록" class="listbox-demo">
      <div role="option" tabindex="0" aria-selected="true" data-id="m-1">
        <span style="width:6px;height:6px;border-radius:50%;background:#34C759;"></span>
        2026-04-27 오후 2시 회의
      </div>
      <div role="option" tabindex="0" aria-selected="false" data-id="m-2">
        <span style="width:6px;height:6px;border-radius:50%;background:#8E8E93;"></span>
        2026-04-26 스프린트 회고
      </div>
      <div role="option" tabindex="0" aria-selected="false" data-id="m-3">
        <span style="width:6px;height:6px;border-radius:50%;background:#FF9500;"></span>
        2026-04-25 디자인 리뷰
      </div>
    </div>
  </section>

  <!-- 2. nav 링크 3개 (1개 current page) -->
  <section>
    <h2>Navigation (aria-current)</h2>
    <nav aria-label="주요 내비게이션" class="nav-demo">
      <button class="nav-btn" data-route="/app" aria-current="page">회의록</button>
      <button class="nav-btn" data-route="/app/search">검색</button>
      <button class="nav-btn" data-route="/app/chat">채팅</button>
    </nav>
  </section>

  <!-- 3. expandable button (collapsed/expanded 양쪽 동시 표시) -->
  <section>
    <h2>Expandable (aria-expanded)</h2>
    <div class="toggle-demo">
      <button type="button" aria-expanded="true" aria-controls="panel-open">
        ▾ 열린 패널 (aria-expanded="true")
      </button>
      <div id="panel-open" class="panel">
        패널 본문 — 활성 상태로 시각/청각 모두 "확장됨" 으로 발화됨.
      </div>

      <button type="button" aria-expanded="false" aria-controls="panel-closed">
        ▸ 닫힌 패널 (aria-expanded="false")
      </button>
      <div id="panel-closed" class="panel" hidden>
        (가려진 본문 — fixture 에서는 같은 frame 에 두 상태 모두 표시 위해 hidden)
      </div>
    </div>
  </section>

  <script>
    // QA-A 가 동작 시나리오 검증 시 사용. 베이스라인 캡처에는 영향 없음.
    document.querySelectorAll('[role="option"]').forEach(function (el) {
      el.addEventListener('click', function () {
        document.querySelectorAll('[role="option"]').forEach(function (other) {
          other.setAttribute('aria-selected', other === el ? 'true' : 'false');
        });
      });
    });
    document.querySelectorAll('.toggle-demo button[aria-expanded]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var open = btn.getAttribute('aria-expanded') === 'true';
        btn.setAttribute('aria-expanded', open ? 'false' : 'true');
        var panel = document.getElementById(btn.getAttribute('aria-controls'));
        if (panel) panel.hidden = open;
      });
    });
  </script>
</body>
</html>
```

### §3.1 fixture 검증 시나리오 (QA-A 가 작성)

1. **Listbox**: Tab → option 1 포커스 → ↓ → option 2 포커스 → 모든 option 의
   `aria-selected` 가 클릭 직후 갱신되는지 확인 (행동 시나리오)
2. **Nav**: 활성 버튼 1개에만 `aria-current="page"` 존재, 나머지에는 속성 자체가 없음
3. **Expandable**: 두 패널 동시 표시 — 한쪽은 `true` 한쪽은 `false`
4. **axe-core**: 페이지 로드 후 `aria-allowed-attr`, `aria-required-parent`,
   `aria-valid-attr-value` 룰 위반 0 건

---

## §4 베이스라인 캡처

```python
# tests/ui/visual/test_aria_sync.py (QA-A 가 별도 티켓에서 작성)
import pytest

PREVIEW_URL = "http://127.0.0.1:8765/tests/fixtures/aria-sync.html"

@pytest.mark.parametrize("variant,viewport,theme", [
    ("light",  {"width": 520, "height": 700}, "light"),
    ("dark",   {"width": 520, "height": 700}, "dark"),
    ("mobile", {"width": 375, "height": 720}, "light"),
])
def test_aria_sync_baseline(page, variant, viewport, theme):
    page.set_viewport_size(viewport)
    page.goto(PREVIEW_URL)
    page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
    # 활성 selected/current/expanded 모두 표시된 한 frame 캡처
    page.locator("[aria-selected='true']").first.wait_for()
    page.locator("[aria-current='page']").first.wait_for()
    page.locator("[aria-expanded='true']").first.wait_for()
    page.wait_for_timeout(150)  # transition 완료 (--duration-fast)
    page.screenshot(
        path=f"tests/ui/visual/baselines/aria-sync-{variant}.png",
        full_page=False, animations="disabled",
    )
```

**캡처 한 frame 에 반드시 포함되어야 할 시각 단서**:
- listbox 의 첫 항목이 accent 배경으로 강조 (= `aria-selected="true"`)
- nav 의 "회의록" 버튼이 accent 배경으로 강조 (= `aria-current="page"`)
- 토글 영역에서 ▾ 표시된 열린 버튼 + 패널 본문 노출 + ▸ 표시된 닫힌 버튼 동시 존재

본 mockup 단계에서는 fixture HTML 만 정의하고, **베이스라인 PNG 자체는
3 변종 placeholder 로 생성**해 산출물 형식을 맞춘다 (실제 생성은 QA-A 의
green 단계에서 Playwright 로 갱신).

---

## §5 axe-core 룰셋 (QA-A 가 a11y 게이트에 등록)

| 룰 | 의도 | 통과 조건 |
|----|------|----------|
| `aria-allowed-attr` | role 에 허용되지 않는 ARIA 속성 사용 차단 | `option` 에 `aria-current` 부여 등 잘못된 매핑 0 건 |
| `aria-required-parent` | `option` 은 `listbox`/`group` 부모 필수 | `#listContent[role="listbox"]` > `[role="option"]` 구조 유지 |
| `aria-valid-attr-value` | enumerated 값 위반 차단 | `aria-current="page"` (boolean 사용 금지), `aria-selected="true"` (string only) |
| `aria-required-children` | `tablist` 에는 `tab` 자식 필수 | 뷰어 탭 영역 검증 |
| `duplicate-id-aria` | `aria-controls` / `aria-labelledby` 가 가리키는 id 중복 금지 | fixture 의 `panel-open`, `panel-closed` id 유일 |

---

## §6 의존성·금지 사항

### 의존성
- 신규 토큰 / 스타일 변경 없음 (시각 디자인 무변경)
- 신규 라이브러리 없음 (axe-core 는 Wave 1 부터 이미 게이트에 등록)

### 절대 금지
- ❌ `ui/web/*` 직접 변경 (이 티켓은 producer 사양 단계)
- ❌ `aria-current="false"` 부여 (속성 제거 패턴 강제 — §1.1)
- ❌ `aria-selected` 에 boolean 직접 전달 (반드시 string `"true"` / `"false"`)
- ❌ Native `<details>` 에 JS 로 `aria-expanded` 추가 설정 (브라우저 기본 동기 충돌)
- ❌ `role="option"` 안에 `role="button"` 중첩 (axe `aria-allowed-role` 위반)

---

## §7 산출물 체크리스트

- [x] mockup §1 의 ARIA 매핑이 spec §3 항목 6 의 3 속성(selected, current, expanded) 모두 다룸
- [x] axe-core `aria-required-parent`, `aria-allowed-attr`, `aria-valid-attr-value` 룰 통과 가능한 마크업
- [x] `role="listbox"` 안에 `role="option"` 배치 (parent-child 강제)
- [x] 베이스라인 PNG 3 변종 < 200KB 목표 (placeholder 단계에서는 형식만 맞춤)
- [x] `ui/web/*` 직접 변경 없음
- [x] fixture HTML 에 3 영역(listbox / nav / expandable) 동시 표시
- [x] §1.2 SR 발화 기대 정의 — VoiceOver 회귀 검증 가능

---

## §8 후속 티켓 핸드오프

| 대상 | 인터페이스 | 입력 |
|------|------------|------|
| Frontend (T-301-impl) | `NavBar.setActiveFromPath()` 에 `aria-current` 동기화 추가 + 토글 핸들러에 `aria-expanded` 동기화 헬퍼 적용 | 본 mockup §2.2, §2.3 |
| QA-A (T-301-qa) | `aria-sync.html` fixture + Playwright 베이스라인 + axe 룰셋 등록 | 본 mockup §3, §4, §5 |
| QA-A (a11y) | axe-core 5 룰 활성화 — `aria-allowed-attr` / `aria-required-parent` / `aria-valid-attr-value` / `aria-required-children` / `duplicate-id-aria` | 본 mockup §5 |
| Designer-B (review) | ARIA 매핑이 디자인 토큰 / 컴포넌트 시각 상태와 모순되는지 검토 (selected ↔ active 클래스 일치) | 본 mockup §1, §2 |
