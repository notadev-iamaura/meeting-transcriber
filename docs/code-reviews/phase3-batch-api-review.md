# Phase 3 코드 리뷰: `POST /api/meetings/batch` (bulk-actions)

- **리뷰 대상 PR/티켓**: bulk-actions Phase 3 — 통합 일괄 처리 엔드포인트
- **리뷰 일자**: 2026-04-29
- **리뷰어**: code-review-expert (독립 리뷰)
- **검토 파일**:
  - `/Users/youngouksong/projects/meeting-transcriber/api/routes.py` (라인 3046–3396)
  - `/Users/youngouksong/projects/meeting-transcriber/tests/test_routes_meetings_batch.py` (727줄, 21 테스트)
- **참조**:
  - `/Users/youngouksong/projects/meeting-transcriber/CLAUDE.md`
  - `/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1239,1801,539,589,1505`
  - 기존 패턴: `/Users/youngouksong/projects/meeting-transcriber/api/routes.py:2952` (`summarize_batch`)

---

## 0. 요약 (Executive Summary)

| 항목 | 결과 |
|------|------|
| 7 축 종합 | **PASS (조건부)** — 본 PR 단독 머지 가능, Phase 4 진행 OK |
| Critical | 0 |
| Major | 2 (중복 ID 큐잉, queued 카운트와 실제 실행 불일치) |
| Minor | 4 |
| Nit | 3 |
| pytest 재실행 | 21 / 21 통과 (1.36s) |
| import 안전성 검증 | OK (Pydantic Literal 모듈 로드 정상) |

**최종 판정**: ✅ **PASS — Phase 4 진행 가능**

발견한 Major 이슈 2건은 모두 **응답 정확성**에 관한 것으로, 백그라운드 실행의 안정성·보안에는 영향 없음. UI(Phase 4)에서 사용자에게 잘못된 카운트가 표시될 수 있으므로 **Phase 4 시작 시 또는 Phase 4 끝나기 전에** 수정 권고. 다만 Phase 3 단독으로는 정상 동작하며, 후속 PR로 분리해도 무방.

---

## 1. 정합성 (구현 vs 명세) — **WARN**

### 1.1 액션·스코프 라우팅
- ✅ 3 액션(transcribe/summarize/full) × 3 scope(all/recent/selected) 모두 라우팅 가능. (`api/routes.py:3226-3268`)
- ✅ `_is_meeting_eligible` 가 액션별 분류 매칭을 정확히 수행. (`api/routes.py:3126-3146`)
  - `transcribe` → classification=="transcribe" 만
  - `summarize`  → classification=="summarize" 만
  - `full`       → classification ∈ {"transcribe", "summarize"} (이미 요약된 항목 자동 skip)

### 1.2 카운트 정확성
- ✅ `matched = len(candidate_ids)` — scope 통과 후보 수 (`api/routes.py:3270`)
- ✅ `queued = len(queued_ids)`     — 액션 적합 + 큐잉된 수 (`api/routes.py:3290`)
- ✅ `skipped = matched - queued`   — 항등식 보존 (`api/routes.py:3291`)
- ⚠️ **WARN (Major #2)**: transcribe 분기에서 `audio_path` 부재 시 `continue`로 조용히 skip 되지만 (`api/routes.py:3337-3342`), 응답의 `queued` 카운트에는 이미 +1 반영된 상태. 즉 사용자에게 "1건 처리"라고 알려주지만 실제로는 0건 실행될 수 있음. 매칭은 됐으나 실행이 안 된 회의 수를 별도로 응답에 포함시키지 않음.

### 1.3 hours 기본값·범위
- ✅ `hours: int = Field(default=24, ge=1, le=720)` — 명세대로. (`api/routes.py:3161`)
- ✅ 422 검증 테스트 존재 (`tests/test_routes_meetings_batch.py:202-220`)

### 1.4 path traversal 방지
- ✅ `scope=selected`의 모든 `meeting_ids` 에 `_validate_meeting_id` 적용 (`api/routes.py:3228-3229`).
- ✅ `scope ∈ {all, recent}` 의 디스크/큐 출처 ID 에도 한 번 더 검증 (silent skip + warn). (`api/routes.py:3278-3284`)
- ✅ 400 응답·detail 검증 테스트 존재 (`tests/test_routes_meetings_batch.py:535-552`)

### 1.5 "full" 액션의 의미 — **WARN (Minor #1)**
- 코드 주석(`api/routes.py:3056`)에서 "full = transcribe ∪ summarize — 회의별 적합한 단계부터 자동 시작"이라고 명시.
- 그러나 transcribe로 분류된 회의는 **항상** `pipeline.run(skip_llm_steps=True)` 로 호출되어 LLM 단계가 실행되지 않음 (`api/routes.py:3347-3351`).
- **즉 "full"은 한 회의의 전체 파이프라인(STT→화자→LLM)을 의미하지 않음**. 신규 회의는 전사·병합까지만 + 다른 호출에서 LLM 보충, 기존 merge 있는 회의는 LLM만 실행. 명세상 모호함.
- 의도(메모리 안전·MLX Metal 충돌 회피·동시성 단순화) 자체는 합리적이나, 다음 중 하나가 필요:
  1. docstring·OpenAPI 설명에 "full=각 회의의 다음 단계 1개를 실행하므로 신규 회의는 LLM 단계가 즉시 따라오지 않음" 명시
  2. transcribe 후 자동으로 LLM 단계를 큐잉하는 follow-up 로직 추가 (Phase 4 이후 검토)

---

## 2. 동시성 / 안정성 — **PASS**

### 2.1 백그라운드 task 등록·정리
- ✅ `running_tasks.add(task)` + `task.add_done_callback(running_tasks.discard)` 패턴 정확 (`api/routes.py:3377-3380`).
- ✅ `_log_task_exception` 콜백 등록 (`api/routes.py:3376`).
- ✅ 기존 `summarize_batch` 와 동일 패턴 (`api/routes.py:3032-3036`).

### 2.2 한 회의 실패가 다음을 차단하지 않음
- ✅ `for mid, classification in items` 루프 안에서 `try/except Exception` (`api/routes.py:3318-3370`).
- ✅ `logger.exception()` 으로 traceback 보존, 다음 회의로 진행.
- ✅ 테스트로 검증 (`tests/test_routes_meetings_batch.py:695-726`).

### 2.3 LLM lock 위임
- ✅ `pipeline.run_llm_steps()` 가 내부에서 `_acquire_llm_lock_with_timeout()` + `release()` 수행 (`core/pipeline.py:1828,1832`).
- ✅ `pipeline.run()` 의 CORRECT/SUMMARIZE 단계도 `_run_llm_step_with_timeout` 헬퍼에서 lock 획득 (`core/pipeline.py:1505-1525`).
- ✅ 따라서 batch 엔드포인트는 lock 을 직접 잡을 필요 없음. **CLAUDE.md 의 "한 번에 한 모델만 메모리 적재" 원칙을 위반하지 않음**.
- ✅ 백그라운드 단일 task 안에서 회의를 순차 처리하므로 동일 batch 내에서는 추가 직렬화도 불필요.

### 2.4 다른 사용자의 단독 호출과의 동시성
- ✅ 두 명의 사용자가 batch 와 단독 LLM 호출을 동시에 보내도 `_llm_lock` 이 직렬화. 메모리 폭주 위험 없음.
- ✅ transcribe 단계에서도 `pipeline.run()` 내부의 ModelLoadManager 뮤텍스가 모델 로드를 직렬화함.

### 2.5 "full" 액션의 LLM 단계 트리거
- ⚠️ **WARN (Minor #1 재언급)**: "full" 이라도 transcribe 로 분류된 회의는 `pipeline.run(skip_llm_steps=True)` 로 끝남. 자동 follow-up 없음. 명시적 의도면 OK.

---

## 3. 보안 — **PASS**

### 3.1 사용자 입력 검증
- ✅ `_validate_meeting_id` 가 `selected.meeting_ids` 의 모든 원소에 적용 (`api/routes.py:3228-3229`).
- ✅ Pydantic `Literal["transcribe","summarize","full"]` / `Literal["all","recent","selected"]` 가 422 로 잘못된 값 차단.
- ✅ `Field(ge=1, le=720)` 으로 hours 범위 보호.

### 3.2 응답에 노출되는 경로 정보
- ✅ 응답 스키마(`BatchActionResponse`)는 `meeting_ids` 만 노출. 절대 경로/audio_path/체크포인트 경로 미노출.
- ✅ 500 에러 detail 에 `e` 가 포함되지만, 이는 디렉토리 스캔 실패 시 OS 에러 메시지로, 호스트 디렉토리 구조는 이미 `config.paths` 에 의해 결정되어 있어 기존 다른 엔드포인트와 노출 수준 동일.

### 3.3 `audio_path` 부재 시 silent skip 의 안전성
- ✅ **보안 측면**: 안전. 다른 회의의 audio_path 가 노출되지 않으며, 디스크에 추측한 경로로 접근하지 않음.
- ⚠️ **UX 측면**: Major #2 로 별도 처리 (응답 카운트 부정확).

### 3.4 selected 외 디스크 출처 ID
- ✅ `scope ∈ {all, recent}` 의 디스크 폴더명·DB ID 도 `_validate_meeting_id` 재검증 (silent skip + warn). (`api/routes.py:3278-3284`) — 디스크가 손상되어 `..` 같은 폴더가 있어도 안전.

---

## 4. 회귀 위험 — **PASS**

### 4.1 기존 엔드포인트 충돌
- ✅ `POST /meetings/summarize-batch` 와 `POST /meetings/batch` 는 별개 경로 — 충돌 없음 (`api/routes.py:2952, 3189`).
- ✅ `summarize_batch` 의 동작/시그니처 변경 없음 — 본 PR diff 는 라인 ~3046 이후 신규 코드만.

### 4.2 헬퍼 시그니처 보존
- ✅ `_validate_meeting_id(meeting_id: str) -> None` — 변경 없음 (`api/routes.py:503`).
- ✅ `_get_pipeline_manager(request: Request) -> Any` — 변경 없음 (`api/routes.py:2819`).
- ✅ `_get_job_queue(request: Request) -> Any` — 변경 없음 (`api/routes.py:457`).

### 4.3 신규 import 충돌 — **WARN (Minor #2)**
- ⚠️ `Literal` 이 `BatchActionRequest`/`BatchActionResponse` 정의(라인 3159, 3160, 3179)에서 사용되지만, 실제 `from typing import Literal` 은 라인 4955 에 있음.
- 이론상 NameError 위험이지만 실제로는 안전:
  - 라인 16 `from __future__ import annotations` 가 모든 type annotation 을 lazy string 으로 평가.
  - Pydantic v2 가 model_rebuild 시점에 `module.__dict__` 에서 `Literal` 을 찾는데, 이때는 이미 모듈 로드가 완료되어 있어 namespace 에 존재.
- **검증**: 실제 `from api import routes` 후 Pydantic validation 정상 동작 확인 (NameError·ValidationError 모두 정상 케이스 대응).
- **Nit (Nit #1)**: 가독성을 위해 라인 27 `from typing import Any, Literal` 로 통합하는 것이 권장. A/B 테스트 라우팅 코드는 monkeypatch 호환성 주석 때문에 noqa E402 로 별도 처리 중이지만, `Literal` 자체는 표준 라이브러리이므로 monkeypatch 대상이 아님 — 상단 import 가능.

### 4.4 `from datetime import datetime, timedelta` 가 함수 본문 안 — **Nit (Nit #2)**
- 라인 3242, 함수 내부에 import. 다른 routes.py 패턴은 모듈 상단에 통일. 큰 문제는 아니지만 일관성 위해 모듈 상단으로 이동 권고.

---

## 5. 테스트 품질 — **PASS (Minor 보강 권고)**

### 5.1 21 테스트의 의미성
- ✅ 입력 검증 4 (action/scope/hours/missing fields) — 의미 있음.
- ✅ 액션 필터링 5 (transcribe/summarize 분기 + meeting_minutes.md 인식 + full 합집합 + full skip) — 의미 있음, 분류 3 분기 모두 커버.
- ✅ scope 정책 3 (recent hours, recent default, selected) — 의미 있음.
- ✅ 응답 형식 4 (no_targets, counts, traversal, schema) — 의미 있음, 항등식 검증 포함.
- ✅ 백그라운드 실행 3 (task 등록, full 라우팅, 503) — 의미 있음, `assert_called_once_with(meeting_id)` 와 `kwargs.get("skip_llm_steps") is True` 정확.
- ✅ 통합 시나리오 2 (audio missing, 실패 후 진행) — 의미 있음.

### 5.2 mock 의 정확성
- ✅ `mock_pipeline.run = AsyncMock()` / `mock_pipeline.run_llm_steps = AsyncMock()` 로 코루틴 흉내 정확 (`tests/test_routes_meetings_batch.py:141-142`).
- ✅ `_drain_background_tasks` 가 portal.call() 로 동일 이벤트 루프에서 await — TestClient 호환 패턴. (`tests/test_routes_meetings_batch.py:160-169`)
- ✅ `assert_called_once_with("m_pending")` 로 인자까지 검증 (`tests/test_routes_meetings_batch.py:297, 603, 645`).
- ✅ `run_call.kwargs.get("skip_llm_steps") is True` 등으로 키워드 인자 검증 (`tests/test_routes_meetings_batch.py:642`).

### 5.3 입력 검증 테스트의 status code 와 detail
- ✅ 422 검증 4건 (`tests/test_routes_meetings_batch.py:189, 200, 213, 220, 228`).
- ✅ 400 검증 + detail 메시지 동시 검증 (`tests/test_routes_meetings_batch.py:551-552`).
- ✅ 503 검증 + detail 부분 매칭 (`tests/test_routes_meetings_batch.py:658-659`).

### 5.4 누락된 테스트 케이스 — **Minor (Minor #3, #4)**
- ⚠️ **Minor #3**: `scope=selected` + `meeting_ids=[]` (빈 리스트) 케이스 미검증. 코드는 `candidate_ids=[]` → `matched=0` → no_targets 로 잘 처리되지만 명시적 테스트 부재.
- ⚠️ **Minor #4**: 동일 `meeting_id` 가 `meeting_ids` 에 중복 포함되는 케이스 미검증. 현재 구현은 중복 제거를 하지 않음 — Major #1 참조.
- ⚠️ **Minor #5**: scope=recent 에서 `created_at=""` 또는 ISO 형식 깨진 케이스 — 코드(`api/routes.py:3247-3253`)는 silent skip 으로 처리하지만 테스트 부재.

---

## 6. 코딩 스타일 / CLAUDE.md 준수 — **PASS (Minor)**

| 항목 | 결과 | 근거 |
|------|------|------|
| 한국어 docstring | ✅ | `api/routes.py:3068, 3081, 3098, 3126, 3149, 3166, 3194, 3308` 모두 한국어 |
| Args/Returns/Raises 섹션 | ✅ | 모든 신규 함수에 표기 |
| f-string 사용 | ✅ | 라인 3236, 3239, 3267, 3283, 3334, 3338, 3344, 3352 등 |
| `pathlib.Path` 사용 | ✅ | 라인 3331, 3220-3221 (`config.paths.resolved_*`) |
| bare except 없음 | ✅ | 모든 except 가 구체 타입(`OSError`, `Exception` + 즉시 logger.exception) |
| `print()` 없음 | ✅ | logger 사용 일관 |
| 하드코딩 경로 없음 | ✅ | `config.paths.resolved_checkpoints_dir`, `config.paths.resolved_outputs_dir` 사용 |
| 외부 API 호출 없음 | ✅ | 100% 로컬 — pipeline 메서드만 호출 |
| 타입 힌트 | ✅ | 모든 함수 시그니처에 타입 |
| BaseModel 스키마 | ✅ | Pydantic v2 `Field`, `Literal` 적절히 사용 |

- ⚠️ **Minor #2 (재언급)**: `Literal` import 위치 (앞 4.3 항목 참조).
- ⚠️ **Nit #2 (재언급)**: `from datetime import` 가 함수 본문 안.
- ⚠️ **Nit #3**: `_run_batch` 내부 한국어 주석은 일관되나, 라인 3360-3364 의 fallback 분기("_is_meeting_eligible 통과 후 여기 도달하면 분류 로직 버그") 는 데드 코드. 방어적 코드로 유용하지만 `assert` 또는 `logger.error` 가 더 명확.

---

## 7. 엣지 케이스 / 잠재적 버그 — **WARN**

### 7.1 scope="all" + checkpoints_dir 가 비어 있을 때
- ✅ `checkpoints_dir.exists()` 체크 후 `iterdir()` (`api/routes.py:3259`).
- ✅ 비어 있어도 `candidate_ids=[]` → no_targets.
- ✅ `OSError` 캐치 (`api/routes.py:3263-3268`) 로 권한 문제 등 안전.

### 7.2 scope="recent" 의 created_at 출처
- ✅ `JobQueue.get_all_jobs()` → Job.created_at (ISO 8601 문자열).
- ✅ `datetime.fromisoformat()` 파싱, 실패 시 silent skip (`api/routes.py:3247-3253`).
- ⚠️ **잠재적 이슈**: JobQueue 에 등록되지 않은 회의(예: 외부 도구로 직접 추가된 폴더)는 `scope=recent` 에서 누락. 명세 모호.

### 7.3 scope="selected" + meeting_ids 빈 리스트
- ✅ `for mid in []: pass` → `candidate_ids=[]` → no_targets. 안전.
- ⚠️ **Minor #3 재언급**: 명시적 테스트 권고.

### 7.4 "full" + audio_path 부재 — **Major #2**
- 시나리오: `m_no_merge` 회의가 `_classify_meeting_for_batch` 에서 "transcribe" 로 분류 → 응답 `queued` 에 포함 → 백그라운드에서 `audio_path` 조회 실패 → silent `continue`.
- **사용자 영향**: API 응답에는 "1건 처리 시작" 인데 실제로는 처리 안 됨. WebSocket 진행 이벤트도 발행되지 않으므로 UI 가 무한 대기 상태.
- **테스트 검증**: `test_batch_transcribe_skips_when_audio_missing` (`tests/test_routes_meetings_batch.py:670-693`) 가 이 동작을 명시적으로 보장하지만, 이는 **현재 동작을 그대로 인정**할 뿐 사용자 관점 정확성은 보장하지 않음.

### 7.5 동일 meeting_id 가 meeting_ids 에 중복 — **Major #1**
- 시나리오: 사용자가 UI 버그·실수로 `meeting_ids=["m1", "m1"]` 전송.
- **현재 동작**: candidate_ids=["m1","m1"] → 분류 2회 → eligible=[("m1","summarize"),("m1","summarize")] → `pipeline.run_llm_steps("m1")` 가 **2번 호출**.
- 두 번째 호출은 `_llm_lock` 으로 첫 번째가 끝날 때까지 대기 → 끝나면 두 번째도 실행 → **이미 요약된 회의를 다시 요약**.
- 백그라운드 단일 task 안이라 race condition 은 없지만 자원 낭비 + 사용자 혼란.
- **권고**: `_validate_meeting_id` 직후 `dict.fromkeys(...)` 로 중복 제거.

### 7.6 transcribe 가 audio_path 가 없는 회의를 응답에서 제외하는 옵션 미제공
- 현재 `queued` 카운트에 잡히지만 실행 안 됨. 응답에 "executable" / "skipped_at_runtime" 같은 별도 필드를 두거나, `_run_batch` 안에서 재검증 후 응답 구성 시 제외하는 것이 정확.
- **단**: 비동기로 큐잉 후 응답을 반환하므로 응답 시점에는 미실행. 동기적 사전 검증을 큐잉 전에 수행하면 깔끔.

---

## 이슈 우선순위 분류

### Critical (0건)
없음.

### Major (2건)

**M1. 중복 meeting_id 가 큐잉되어 동일 회의를 2회 처리**
- 위치: `api/routes.py:3226-3230`, `3274-3287`
- 영향: LLM 토큰 낭비, 사용자 혼란, summarize 의 경우 기존 summary.md 가 덮어써질 수 있음.
- 권고:
  ```python
  # scope == "selected" 분기
  for mid in body.meeting_ids:
      _validate_meeting_id(mid)
  candidate_ids = list(dict.fromkeys(body.meeting_ids))  # 순서 보존 + 중복 제거
  ```
  또는 `eligible` 작성 시점에 `seen: set[str]` 으로 dedupe.

**M2. `transcribe` 분기에서 audio_path 부재 시 응답 카운트가 부정확**
- 위치: `api/routes.py:3270, 3289-3291` (응답 빌드) vs `api/routes.py:3337-3342` (백그라운드 skip)
- 영향: 사용자에게 "N건 처리 시작" 알림 후 실제로는 처리 안 됨. UI 가 진행 이벤트를 받지 못함.
- 권고: 큐잉 전에(2단계 필터링 안에서) audio_path 존재 여부를 미리 검증하고 부재 회의는 `eligible` 에서 제외 → `queued` 카운트가 실제 실행 가능한 회의 수와 일치하도록.
  ```python
  # eligible 생성 후, transcribe 분류 항목만 audio_path 사전 검증
  validated_eligible: list[tuple[str, str]] = []
  for mid, classification in eligible:
      if classification == "transcribe":
          job = await asyncio.to_thread(queue.queue.get_job_by_meeting_id, mid)
          if job is None or not job.audio_path or not Path(job.audio_path).exists():
              logger.warning(f"일괄 처리: 오디오 부재로 사전 제외 — {mid}")
              continue
      validated_eligible.append((mid, classification))
  eligible = validated_eligible
  ```

### Minor (5건)

**m1. "full" 액션의 의미 명확화 필요**
- 위치: `api/routes.py:3056, 3194, 3347-3351`
- 권고: docstring·OpenAPI 설명에 "full=각 회의의 다음 단계 1개를 실행. 신규 회의는 LLM 단계가 별도 트리거 필요" 명시. 또는 transcribe 후 자동으로 LLM 큐잉(별 PR).

**m2. `Literal` import 위치 일관성**
- 위치: `api/routes.py:27, 4955` vs 사용 라인 3159
- 권고: 라인 27 을 `from typing import Any, Literal` 로 통합.

**m3. `scope=selected` + `meeting_ids=[]` 명시 테스트 추가**
- 위치: `tests/test_routes_meetings_batch.py`
- 권고: `TestBatchScope` 또는 `TestBatchResponseShape` 에 케이스 추가.

**m4. 중복 `meeting_id` 케이스 테스트 추가**
- 위치: `tests/test_routes_meetings_batch.py`
- 권고: M1 수정 후 회귀 테스트로.

**m5. `scope=recent` + 손상된 created_at 케이스 테스트 추가**
- 위치: `tests/test_routes_meetings_batch.py`
- 권고: `created_at="not-a-datetime"` Job 을 mock 으로 넣고 silent skip 검증.

### Nit (3건)

**n1. `Literal` 을 모듈 상단 import 로 (Minor m2 와 동일).**

**n2. `from datetime import datetime, timedelta` 를 함수 본문 → 모듈 상단으로 이동.**
- 위치: `api/routes.py:3242`

**n3. `_run_batch` 의 fallback 분기를 `assert` 또는 `logger.error` 로 변경.**
- 위치: `api/routes.py:3359-3364`
- 현재 `logger.warning` 인데, 도달 불가능한 코드이므로 `logger.error("배치 분류 로직 버그: ...")` 로 격상 권고.

---

## 수정 권고 (정확한 변경 항목)

본 PR 머지 전 권고 (Major 2건):

```python
# api/routes.py:3226-3230 (scope == "selected" 분기)
if body.scope == "selected":
    for mid in body.meeting_ids:
        _validate_meeting_id(mid)
    # 중복 제거 (순서 보존)
    candidate_ids = list(dict.fromkeys(body.meeting_ids))
```

```python
# api/routes.py:3287 직후 (eligible 작성 후, queued_ids 작성 전)
# transcribe 분류 항목은 audio_path 사전 검증
validated: list[tuple[str, str]] = []
for mid, classification in eligible:
    if classification == "transcribe":
        try:
            job = await asyncio.to_thread(queue.queue.get_job_by_meeting_id, mid)
        except Exception as job_err:
            logger.warning(f"일괄 처리: Job 조회 실패 — {mid}: {job_err}")
            continue
        if job is None or not job.audio_path or not Path(job.audio_path).exists():
            logger.warning(f"일괄 처리: 오디오 부재로 사전 제외 — {mid}")
            continue
    validated.append((mid, classification))
eligible = validated
queued_ids = [mid for mid, _ in eligible]
queued = len(queued_ids)
skipped = matched - queued
```

본 PR 머지 후 (Phase 4 진행 중) 권고 (Minor 5건):

- `Literal` import 통합 (Minor m2)
- `datetime` import 모듈 상단 이동 (Nit n2)
- 누락 테스트 3건 추가 (Minor m3, m4, m5)
- "full" 액션 docstring 보강 (Minor m1)
- fallback 분기 logger 격상 (Nit n3)

---

## 최종 판정

✅ **PASS — Phase 4 (프론트엔드) 진행 가능**

**근거**:
1. CLAUDE.md 핵심 규칙(외부 API 금지, ModelLoadManager 활용, `_llm_lock` 위임, 한 번에 한 모델, 한국어 docstring) 모두 준수.
2. 동시성·보안 측면 결함 없음. 한 회의 실패가 다음 회의를 막지 않으며, path traversal·SQL injection 위험 없음.
3. 21 테스트가 핵심 분기를 커버하며, 실제 재실행 결과 21/21 통과 (1.36s).
4. 발견된 Major 2건은 모두 **응답 정확성** 측면이며, **백엔드 안정성**에는 영향 없음.

**메인에게 제안**:
- Phase 4 (프론트엔드) 즉시 진행 OK.
- Phase 4 시작 전 또는 Phase 4 진행 중 별도 후속 PR 로 Major #1 (중복 dedupe) + Major #2 (사전 audio_path 검증) 보완 권고.
- 사용자 UX 측면에서 Phase 4 가 "N건 처리 시작" 카운트를 신뢰하므로, Major #2 는 늦어도 Phase 4 완료 직전에는 수정.
