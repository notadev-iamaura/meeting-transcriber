# Phase 6 bulk-actions 성능 감사 보고서

- 감사 일자: 2026-04-29
- 감사 대상: Phase 6 (bulk-actions) 신규 코드
- 감사자: Performance Engineer (Claude Sonnet 4.6)
- 최종 판정: **수정 권고**

---

## 요약

| 축 | 판정 | 비고 |
|----|------|------|
| 1. 백엔드 응답 시간 | WARN | scope=all + 수만 회의 시 fs stat 집중 + event loop 블로킹 가능성 |
| 2. 메모리 | PASS | 일반/극단 범위 모두 허용 수준 |
| 3. 프론트엔드 렌더링 | WARN | `_syncSelectionUI` 의 전체 DOM 순회가 1만 항목 이상에서 누적 비용 |
| 4. 이벤트 처리 효율 | WARN | `recap:selection-changed` 발사 빈도 + `Array.from(_selectedIds)` 매번 실행 |
| 5. 네트워크 | PASS | 페이로드 크기·polling 중복 부하 모두 허용 수준 |
| 6. 회귀 | PASS | `.content-wrapper` 추가 영향 최소, 기존 라우트 비용 무변화 |

**Critical 핫스팟: 1건 / Major 핫스팟: 2건 / Minor 핫스팟: 2건**

---

## 1축. 백엔드 응답 시간

### 1-A. scope="all" — `checkpoints_dir.iterdir()` + 2단계 per-meeting stat 호출

**판정: WARN**

**위치:**
- `api/routes.py:3317-3328` (1단계 iterdir)
- `api/routes.py:3339-3362` (2단계 eligibility 루프)
- `api/routes.py:3076-3132` (`_classify_meeting_for_batch` → `is_file()` 최대 3회)

**문제:**

`scope="all"` 경로에서 실행 순서는 다음과 같다.

```
checkpoints_dir.iterdir()      ← stat syscall: N회 (디렉토리 엔트리 수)
  └─ for mid in candidate_ids:
       _classify_meeting_for_batch(mid, ...)
         └─ (checkpoints_dir / mid / "merge.json").is_file()   ← stat: N회
         └─ (outputs_dir / mid / "summary.md").is_file()       ← stat: N회 (merge 있을 때)
         └─ (outputs_dir / mid / "meeting_minutes.md").is_file() ← stat: N회 (summary 없을 때)
```

회의 N개 기준 최악 stat 호출 수: `iterdir` N + `is_file` 최대 3N = **최대 4N syscall**.

- 일반 (300건): 약 1,200 syscall → macOS HFS+ 기준 ~5ms, 무시 가능
- 극단 (5,000건): 약 20,000 syscall → ~80-150ms (로컬 SSD 기준)
- 극단 (30,000건): 약 120,000 syscall → ~500ms-1s (Critical 임계)

이 경로 전체가 **동기 호출이며 `asyncio.to_thread` 로 감싸지지 않았다.** FastAPI 의 async 핸들러에서 동기 fs I/O를 직접 호출하면 event loop 스레드가 해당 시간 동안 다른 요청을 처리할 수 없다.

단, 이 앱은 로컬 단독 실행이므로 병렬 사용자가 없어 실제 서비스 장애로 이어지지는 않는다. 그러나 500ms+ 응답 지연이 발생하는 극단 시나리오에서 사용자 체감 반응성이 저하된다.

**추정 비용 (로컬 SSD, Apple Silicon):**

| 회의 수 | 추정 응답 시간 | 평가 |
|---------|--------------|------|
| ~300 | <10ms | 정상 |
| ~3,000 | 50-100ms | 경계 |
| ~10,000 | 200-400ms | WARN |
| ~30,000 | 600ms-1.5s | FAIL 임계 |

---

### 1-B. scope="recent" — `get_all_jobs()` 전체 로드 후 Python 레이어 필터링

**판정: WARN**

**위치:** `api/routes.py:3294` (`all_jobs = await queue.get_all_jobs()`)

**문제:**

`scope="recent"` 는 `hours` 윈도우 내 회의만 필요하지만, `queue.get_all_jobs()` 는 `SELECT * FROM jobs ORDER BY meeting_id DESC` 로 전체 테이블을 메모리에 로드한 뒤 Python 레이어에서 `created_at >= cutoff` 필터링을 적용한다.

`core/job_queue.py:512` 참조:
```sql
SELECT * FROM jobs ORDER BY meeting_id DESC
```

SQLite 에서 직접 `WHERE created_at >= ?` 로 필터링하면 인덱스를 사용할 수 있는 반면, 현재 구현은 모든 행을 Job 객체로 역직렬화한 뒤 Python 루프에서 비교한다. 회의 수가 많을수록 불필요한 객체 생성과 메모리 사용이 증가한다.

단, `asyncio.to_thread` 로 감싸져 있어 event loop는 블로킹되지 않는다. 주요 비용은 SQLite I/O + Python 객체 생성이다.

- 일반 (300건): 무시 가능
- 극단 (10,000건): ~20-50ms (DB 읽기 + 객체 생성)

---

### 1-C. `_resolve_audio_path` — transcribe 분류마다 별도 `asyncio.to_thread` + `path.resolve`

**판정: WARN (중복 비용)**

**위치:** `api/routes.py:3158-3202` (`_resolve_audio_path`)

**문제:**

`classification == "transcribe"` 인 회의마다 `_resolve_audio_path` 를 호출하며, 내부에서:

1. `asyncio.to_thread(queue.queue.get_job_by_meeting_id, meeting_id)` — SQLite 조회 1회
2. `raw_path.exists()` — stat syscall 1회
3. `raw_path.resolve(strict=True)` — 실제 경로 해석 (추가 stat)
4. `base_dir.resolve()` — base_dir 정규화 (매번 재실행)

`base_dir.resolve()` 는 루프마다 동일한 결과를 반환하는 불변 값인데 매 호출마다 재계산된다. 또한 2단계 루프에서 `_classify_meeting_for_batch` 에서 이미 `merge.json` 의 부재를 확인했으므로, `transcribe` 분류인 회의의 audio_path 를 알기 위해 다시 SQLite를 조회하는 이중 접근이 발생한다.

`scope="all"` + 전체 회의가 "transcribe" 분류일 경우(초기 사용자), N개 SQLite 조회가 순차 실행된다.

- 일반 (100건 transcribe): ~50ms (100 × SQLite 조회 + stat)
- 극단 (1,000건 transcribe): ~500ms

---

## 2축. 메모리

**판정: PASS**

**분석:**

- `candidate_ids: list[str]`: 회의 ID가 `meeting_YYYYMMDD_HHMMSS` 형식 약 25자 기준, 10,000건 = ~250KB. 허용 가능.
- `eligible: list[tuple[str, str, Path | None]]`: 최악 10,000 튜플 × 3 필드. `Path` 객체 포함 시 튜플당 약 200-400바이트 추정. 10,000건 = ~4MB. 허용 가능.
- `dict.fromkeys()` 중복 제거: 임시 딕셔너리 키만 보관하므로 메모리 효율적. PASS.
- 백그라운드 클로저 캡처: `_run_batch` 가 `eligible` 리스트와 `body.action` 을 캡처. 리스트 자체는 이미 생성된 객체의 참조이므로 추가 메모리 비용 최소. PASS.

---

## 3축. 프론트엔드 렌더링

### 3-A. `_syncSelectionUI` 전체 DOM 순회

**판정: WARN**

**위치:** `ui/web/spa.js:1289-1307` (`_syncSelectionUI`)

**문제:**

```javascript
var items = _listEl.querySelectorAll(".meeting-item");
for (var i = 0; i < items.length; i++) {
    var el = items[i];
    var id = el.getAttribute("data-meeting-id");
    var isSel = _selectedIds.has(id);
    el.classList.toggle("selected", isSel);
    el.setAttribute("aria-selected", isSel ? "true" : "false");
    el.setAttribute("aria-checked", isSel ? "true" : "false");
}
```

매 선택/해제 동작마다 **전체 `.meeting-item` DOM 노드를 순회**한다. `classList.toggle` 과 `setAttribute` 는 각 항목마다 스타일 재계산을 유발할 수 있다.

- 일반 (50-200개 DOM 노드): <1ms, 무시 가능
- 극단 (1,000+ DOM 노드): 단일 토글에 5-20ms, Shift+클릭 범위 선택에 동일 비용
- 회의 1만개 DOM이 동시에 렌더되는 경우는 실제로는 없다 (페이지네이션/가상 스크롤 여부 확인 필요). 현재 `_renderMeetings` 가 전체 목록을 한꺼번에 렌더하는 구조라면, 수천 개 DOM 노드에서 비용이 누적된다.

**실제 운용 범위:**

`MEETINGS_POLL_INTERVAL = 15000ms` 이고 사이드바는 전체 회의를 DOM에 직접 렌더하는 구조(`spa.js:1060-1233` 추정). 장기 사용자(3,000+ 회의)에서 사이드바 DOM 노드 수가 무제한 증가할 경우 문제가 된다. 단, `Cmd+A` 로 전체 선택 시 `_syncSelectionUI` 1회 + `_emitSelectionChanged` 1회로 처리되어 이벤트 폭발 없이 안전하다.

---

### 3-B. `.bulk-action-bar` 의 `backdrop-filter` 비용

**판정: PASS (Minor 관찰)**

**위치:** `ui/web/style.css:8032-8033`

```css
backdrop-filter: blur(20px) saturate(180%);
-webkit-backdrop-filter: blur(20px) saturate(180%);
```

`position: sticky` + `backdrop-filter` 조합은 새 합성 레이어(compositing layer)를 생성하여 GPU 가속을 받는다. bar 가 숨겨진(`hidden`) 상태에서는 렌더링되지 않으므로 비활성 시 비용 없음. 활성 시에는 GPU에서 처리되어 main thread 부담 없음. PASS.

---

### 3-C. `content-wrapper` flex 중첩 추가 영향

**판정: PASS**

**위치:** `ui/web/style.css:7884-7897`

`.content-wrapper` 는 기존 `#content` 의 직접 부모 위치에 `flex-direction: column` flex 컨테이너를 추가한다. flex 레이아웃 계산 비용은 무시 가능하며, `#content` 의 `overflow-y: auto` 스크롤 컨텍스트가 유지되어 layout 변화 없음. Viewer/Chat 라우트도 동일한 wrapper 안에서 동작하나 `flex: 1 + min-height: 0` 패턴으로 기존 동작 동일. PASS.

---

## 4축. 이벤트 처리 효율

### 4-A. `recap:selection-changed` 이벤트 발사 빈도 + `Array.from(_selectedIds)` 비용

**판정: WARN**

**위치:**
- `ui/web/spa.js:1312-1316` (`_emitSelectionChanged`)
- `ui/web/spa.js:1339-1344` (`_onGlobalKeydown` — Cmd+A 전체 선택)

**문제:**

`_emitSelectionChanged` 는 `Array.from(_selectedIds)` 를 매 호출마다 실행하여 새 배열을 생성하고 이를 `CustomEvent.detail` 에 담아 dispatch 한다. Cmd+A 전체 선택은 `_syncSelectionUI` 후 `_emitSelectionChanged` 1회로 올바르게 처리된다.

그러나 Shift+클릭으로 범위 선택 시 `_selectRange` 내부에서 `_syncSelectionUI` + `_emitSelectionChanged` 가 각 1회 호출되므로 O(1) 이다. 개별 클릭도 토글 1회당 1번 발사하므로 **1회 선택 → 1회 이벤트 발사**로 throttle 없어도 문제없다.

단, 매 이벤트 발사마다 `Array.from(_selectedIds)` 로 Set → Array 복사가 실행된다. 선택 항목이 500개이고 초당 수 번 클릭하면 불필요한 배열 복사가 반복된다. BulkActionBar 구독자는 `detail.count` 만 사용하고 `selectedIds` 배열을 무시하기 때문에 이 복사가 낭비다.

---

### 4-B. 외부 클릭 감지 — document 전역 capture listener

**판정: PASS (Minor 관찰)**

**위치:** `ui/web/spa.js:2073-2078`

```javascript
document.addEventListener("click", self._bulkDropdownDocClick, true);
```

`capture: true` 로 등록되어 있어 모든 클릭 이벤트가 통과한다. 내부 로직은 `.closest(".home-action-dropdown-wrapper")` 단일 DOM 조상 탐색이므로 비용 미미하다. EmptyView.destroy 시 `removeEventListener` 가 호출된다면 누수 없음.

**위치:** `ui/web/spa.js:1925-1938` (destroy 메서드)

`destroy` 에서 `_bulkDropdownDocClick` 제거 여부 확인이 필요하다.

---

## 5축. 네트워크

**판정: PASS**

- `POST /api/meetings/batch` 응답 페이로드: `BatchActionResponse` 에 `meeting_ids: list[str]` 포함. max_length=500 + ID 25자 기준 = 최대 ~13KB. 무시 가능.
- SPA의 15초 대시보드 polling과 배치 처리 응답이 겹쳐도 로컬 서버(FastAPI + uvicorn)에서 concurrency 충돌 없음. 배치는 백그라운드 task 이고 polling 은 별도 엔드포인트.

---

## 6축. 회귀 (성능 측면)

**판정: PASS**

- `.content-wrapper` 추가: flex 중첩 1단계 추가이나 Viewer/Chat 레이아웃 비용 무변화. `#content` overflow 컨텍스트 보존.
- `data-component="bulk-actions"` 마커: 단순 속성, 렌더링 비용 없음.
- `keydown` 전역 리스너: `_onGlobalKeydown` 은 `Escape` 또는 `Cmd+A` 외에는 조기 반환하므로 다른 키 입력에 영향 없음.

---

## 핫스팟 우선순위

### Critical

#### C-1. scope="all" + 극단 회의 수에서 동기 fs I/O가 event loop를 블로킹

**파일:** `api/routes.py:3317-3362`
**추정 비용:** 10,000건 기준 200-400ms event loop 블로킹
**영향:** 로컬 단독 실행에서 사용자 체감 응답 지연

**권고:**

`iterdir` + `_classify_meeting_for_batch` 루프 전체를 `asyncio.to_thread` 로 감싸거나, 루프를 별도 동기 헬퍼 함수로 추출하여 스레드풀에서 실행한다.

```python
# 개선 전 (routes.py:3317-3362)
if checkpoints_dir.exists():
    for cp_dir in sorted(checkpoints_dir.iterdir()):
        ...
for mid in candidate_ids:
    classification = _classify_meeting_for_batch(mid, ...)
    ...

# 개선 후: 1단계 + 2단계 fs I/O를 하나의 동기 함수로 묶어 to_thread 적용
def _collect_eligible_sync(
    candidate_ids, checkpoints_dir, outputs_dir, action, scope, validate_fn
):
    eligible = []
    for mid in candidate_ids:
        if scope != "selected":
            try:
                validate_fn(mid)
            except Exception:
                continue
        classification = _classify_meeting_for_batch(mid, checkpoints_dir, outputs_dir)
        if _is_meeting_eligible(action, classification):
            eligible.append((mid, classification))
    return eligible

eligible_pairs = await asyncio.to_thread(
    _collect_eligible_sync,
    candidate_ids, checkpoints_dir, outputs_dir,
    body.action, body.scope, _validate_meeting_id,
)
```

이후 `transcribe` 분류 항목에 대해서만 `_resolve_audio_path` 를 순차 호출하면 된다. 예상 개선 효과: 10,000건 기준 event loop 블로킹 0ms.

---

### Major

#### M-1. `_resolve_audio_path` 내 `base_dir.resolve()` 루프마다 재실행

**파일:** `api/routes.py:3190`, `api/routes.py:3280`

`base_dir_for_audio = Path(config.paths.base_dir)` 는 엔드포인트 시작 시 1회 생성되나, `base_dir.resolve()` 는 `_resolve_audio_path` 내부에서 매 호출마다 재실행된다.

**권고:**

엔드포인트에서 `base_dir_resolved = base_dir_for_audio.resolve()` 를 1회 계산한 뒤 `_resolve_audio_path` 에 전달하거나, 함수 시그니처를 `base_dir: Path` (이미 resolve된 값)로 변경한다.

```python
# routes.py 엔드포인트 내
base_dir_resolved = base_dir_for_audio.resolve()

# _resolve_audio_path 에 base_dir_resolved 전달
# 함수 내 base_dir.resolve() 호출 제거
```

예상 개선 효과: transcribe N건 기준 N회 stat 제거. 작지만 누적 비용.

---

#### M-2. scope="recent" 에서 SQL 레이어 필터링 미적용

**파일:** `api/routes.py:3294`, `core/job_queue.py:503-514`

`get_all_jobs()` 는 전체 행을 로드한다. `hours` 윈도우 필터를 SQLite 레이어에서 적용하면 Python 객체 생성 비용을 줄일 수 있다.

**권고:**

`JobQueue` 에 `get_jobs_since(cutoff_iso: str) -> list[Job]` 메서드 추가:

```python
# core/job_queue.py 추가
def get_jobs_since(self, cutoff_iso: str) -> list[Job]:
    conn = self._ensure_connection()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE created_at >= ? ORDER BY meeting_id DESC",
        (cutoff_iso,),
    ).fetchall()
    return [self._row_to_job(row) for row in rows]
```

`created_at` 컬럼에 인덱스가 없다면 추가 고려:
```sql
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);
```

예상 개선 효과: 10,000건 중 최근 24시간 내 50건만 해당될 경우, Python 객체 생성 9,950건 절약.

---

### Minor

#### m-1. `_emitSelectionChanged` 에서 `Array.from(_selectedIds)` 불필요한 복사

**파일:** `ui/web/spa.js:1312-1316`

BulkActionBar 구독자(`recap:selection-changed`)는 `detail.count` 만 사용하고 `detail.selectedIds` 배열을 실제로 사용하지 않는다 (`spa.js:1484-1487` 참조).

**권고:**

이벤트 detail에서 `selectedIds` 배열 복사 제거 또는 lazy 변환:

```javascript
// 개선 전
document.dispatchEvent(new CustomEvent("recap:selection-changed", {
    detail: { selectedIds: Array.from(_selectedIds), count: _selectedIds.size }
}));

// 개선 후: count 만 전달 (BulkActionBar 가 필요시 ListPanel.getSelectedIds() 호출)
document.dispatchEvent(new CustomEvent("recap:selection-changed", {
    detail: { count: _selectedIds.size }
}));
```

예상 개선 효과: 선택 토글 빈도에 비례한 GC 압력 소폭 감소.

---

#### m-2. `_syncSelectionUI` 매 토글마다 전체 DOM 순회

**파일:** `ui/web/spa.js:1289-1307`

단일 토글의 경우, 변경된 ID 1개만 업데이트하면 되는데 전체 DOM을 순회한다.

**권고:**

단일 토글(`_toggleSelection`)에서 특정 ID 항목만 직접 업데이트하는 최적화 경로 추가:

```javascript
function _updateItemUI(id, isSelected) {
    if (!_listEl) return;
    var el = _listEl.querySelector('.meeting-item[data-meeting-id="' + id + '"]');
    if (!el) return;
    el.classList.toggle("selected", isSelected);
    el.setAttribute("aria-selected", isSelected ? "true" : "false");
    el.setAttribute("aria-checked", isSelected ? "true" : "false");
}

function _toggleSelection(id) {
    var inSelectionBefore = _selectedIds.size > 0;
    if (_selectedIds.has(id)) {
        _selectedIds.delete(id);
    } else {
        _selectedIds.add(id);
    }
    var inSelectionAfter = _selectedIds.size > 0;
    // mode 변경(0→1 또는 N→0) 시에만 전체 순회 필요
    if (inSelectionBefore !== inSelectionAfter) {
        _syncSelectionUI();
    } else {
        // mode 유지: 변경된 항목 1개만 업데이트
        _listEl.classList.toggle("meetings-list--selecting", inSelectionAfter);
        _updateItemUI(id, _selectedIds.has(id));
    }
    _emitSelectionChanged();
}
```

예상 개선 효과: 1,000개 DOM 노드 기준 단일 토글 비용 O(N) → O(1). Shift+클릭 범위 선택 및 Cmd+A는 기존 전체 순회 유지.

---

## 관련 파일 목록

- `/Users/youngouksong/projects/meeting-transcriber/api/routes.py` (라인 3076-3456)
- `/Users/youngouksong/projects/meeting-transcriber/core/job_queue.py` (라인 461-514, 1037-1045)
- `/Users/youngouksong/projects/meeting-transcriber/ui/web/spa.js` (라인 1235-1345, 1435-1706, 1948-2094)
- `/Users/youngouksong/projects/meeting-transcriber/ui/web/style.css` (라인 7866-8360)

---

## 권고 SLO / 성능 예산

로컬 단독 실행 특성을 고려한 현실적 기준:

| 지표 | 목표 | 현재 추정 (극단 시) |
|------|------|---------------------|
| `POST /api/meetings/batch` 응답 (일반 <300건) | <50ms | <20ms (PASS) |
| `POST /api/meetings/batch` 응답 (극단 <5,000건) | <200ms | 200-400ms (WARN) |
| 단일 선택 토글 반응성 (DOM 100노드 이하) | <16ms (60fps) | <5ms (PASS) |
| 단일 선택 토글 반응성 (DOM 1,000노드) | <16ms | ~10-20ms (경계) |
| Cmd+A 전체 선택 (DOM 1,000노드) | <33ms | ~15-30ms (허용) |

---

## 최종 판정: 수정 권고

**Critical 핫스팟 1건 (C-1)** 이 존재하며, 장기 사용자(수천 회의 이상)의 `scope="all"` 호출에서 수백ms 의 event loop 블로킹이 발생할 수 있다. 이는 로컬 앱이므로 서비스 장애로는 이어지지 않으나 사용자 체감 반응성 저하가 명백하다.

C-1 수정 후 Major 핫스팟 2건도 함께 처리하면 극단 시나리오에서 안정적인 성능을 확보할 수 있다.

**배포 판정:** 일반 사용 범위(~수백 회의)에서는 배포 가능하나, C-1을 수정하지 않으면 장기 사용자에서 반응성 문제가 발생할 수 있으므로 수정 후 재배포를 권고한다.
