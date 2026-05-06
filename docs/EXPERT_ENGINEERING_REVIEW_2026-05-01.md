# 전문가 관점 엔지니어링 평가 보고서

- 작성일: 2026-05-01
- 기준: 현재 작업 디렉토리 기준
- 주요 관점: 코드 품질, 유지보수성, 성능, 아키텍처, 프론트엔드 구조, 백엔드 경계, AI 파이프라인, 운영성
- 보조 검증:
  - `ruff check .` 통과
  - `ruff format --check .` 통과
  - `.venv/bin/python -m pytest -q` → `2624 passed, 140 deselected`
- 관련 문서:
  - [`PROJECT_EVALUATION_MAIN_2026-04-30.md`](./PROJECT_EVALUATION_MAIN_2026-04-30.md)
  - [`CODE_QUALITY_MAINTAINABILITY_PERFORMANCE_REVIEW_2026-05-01.md`](./CODE_QUALITY_MAINTAINABILITY_PERFORMANCE_REVIEW_2026-05-01.md)
  - [`STATUS.md`](./STATUS.md)

## 1. Executive Summary

이 프로젝트는 일반적인 개인 오픈소스 프로젝트 수준을 이미 넘었습니다. 제품 목표가 명확하고, 로컬 AI 회의 처리라는 까다로운 문제를 녹음, 감시, 큐, 전사, 화자분리, 교정, 요약, 임베딩, 검색, 채팅, 웹 UI까지 end-to-end로 연결하고 있습니다.

현재 작업 디렉토리 기준으로는 이전 평가에서 가장 컸던 안정성 리스크도 상당히 줄었습니다. `runtime_profile` 도입, native GPU cleanup 격리, 기본 pytest의 native/ui/e2e 분리, `api/routers/meetings_batch.py` 분리, `tokens.css`/`bulk-actions.css` 분리 등은 모두 올바른 방향입니다.

다만 전문가 관점에서 보면, 다음 단계의 병목은 테스트 통과 여부가 아니라 **구조적 확장성**입니다.

핵심 판단은 다음과 같습니다.

> 이 코드는 "고장난 코드"가 아니라, **제품이 너무 빨리 성장해서 이제 architecture boundary를 따라잡아야 하는 코드**입니다.

가장 큰 개선 지점은 세 가지입니다.

1. `api/routes.py`, `ui/web/spa.js`, `core/pipeline.py` 같은 중심 허브를 기능 경계로 분해한다.
2. `app.state`, `Any`, lazy import, broad exception 기반의 암묵 계약을 명시적 interface/service로 바꾼다.
3. 성능 최적화는 개별 함수 튜닝보다 metadata index, render granularity, pipeline metric, cache invalidation 같은 시스템 경계에서 잡는다.

## 2. 종합 점수

| 영역 | 점수 | 전문가 판단 |
|---|---:|---|
| 제품 기술 완성도 | **8.0 / 10** | 로컬 AI 회의 앱으로 기능 범위와 구현 밀도가 높음 |
| 코드 품질 | **7.1 / 10** | lint/format/test는 좋지만 타입·책임 경계가 약함 |
| 유지보수성 | **6.6 / 10** | 분리 작업이 시작됐지만 핵심 monolith가 아직 큼 |
| 백엔드 아키텍처 | **7.2 / 10** | FastAPI/lifespan/profile 방향은 좋음, service layer 부족 |
| 프론트엔드 아키텍처 | **6.5 / 10** | UI 품질은 올라왔지만 `spa.js` 중심 구조가 장기 부담 |
| AI 파이프라인 설계 | **8.1 / 10** | Apple Silicon local AI에 맞춘 실전 감각이 좋음 |
| 성능 설계 | **7.6 / 10** | cache/WAL/batch/MLX 최적화 의식이 높음, 측정 체계는 더 필요 |
| 운영성/관측성 | **6.4 / 10** | 상태 문서와 profile은 좋음, metrics/diagnostics는 아직 약함 |
| 오픈소스 유지 가능성 | **7.3 / 10** | 문서와 테스트가 강점, onboarding 복잡도와 큰 파일이 약점 |

### 최종 종합 점수

**7.3 / 10**

이 점수는 "현재 코드가 나쁘다"는 뜻이 아닙니다. 오히려 기능 구현과 테스트 투자는 강합니다. 감점의 대부분은 장기 유지보수와 확장성에서 옵니다.

## 3. 분석 방법

이번 평가는 다음 근거를 함께 사용했습니다.

- 코드 정적 분석: 파일 크기, 함수 길이, class/function 수, `Any`, `noqa`, `type: ignore`, broad exception 사용량
- 구조 분석: API route, app lifecycle, pipeline, search/chat, UI module boundary
- 성능 분석: 파일 IO, SQLite/FTS, Chroma, embedding, LLM, UI rendering, cache invalidation
- 운영성 분석: runtime profile, native dependency 격리, CI gate, 상태 문서
- 프로젝트 문서 분석: README, AGENTS, STATUS, performance backlog, design docs
- 실제 검증: lint/format/basic pytest

사용 가능한 관련 역량은 모두 적용했지만, 이 프로젝트 분석에 직접 의미가 없는 메일, 프레젠테이션, 스프레드시트, 이미지 생성 계열 역량은 억지로 쓰지 않았습니다. 이 평가는 repository 자체의 코드와 문서, 테스트 하네스를 중심으로 합니다.

## 4. 현재 작업 디렉토리의 중요한 변화

현재 작업 디렉토리는 깨끗한 `main`이 아니라 active worktree입니다.

주요 변경 방향:

| 변경 | 평가 |
|---|---|
| `api/routers/meetings_batch.py` 추가 | 매우 좋은 방향. route monolith 분해의 첫 실질 단계 |
| `api/routes.py`에서 batch logic 제거 | 유지보수성 개선. 단, 아직 6천 LOC 이상 |
| `api/server.py`에 `runtime_profile` 추가 | 매우 좋은 방향. desktop runtime과 test runtime 경계가 생김 |
| `core/model_manager.py`에 GPU cleanup injection/env flag 추가 | native crash 리스크를 잘 줄인 설계 |
| `pyproject.toml`에서 `native/ui/e2e` 기본 제외 | 실용적. CI 안정성에 유리 |
| `tokens.css`, `bulk-actions.css` 분리 | CSS monolith 분해의 좋은 출발 |
| `docs/STATUS.md` 추가 | 상태 문서 단일화 측면에서 좋음 |

주의할 점:

- `api/routers/__pycache__/`가 untracked로 보입니다. 커밋 대상에서 제외해야 합니다.
- `AGENTS.md`는 유용하지만 700라인 규모라, contributor용 핵심 가이드와 agent-specific 운영 지침을 나눌지 검토할 만합니다.
- 현재 개선 방향은 좋지만, 아직 "첫 분리" 단계입니다. 구조적 부채가 사라진 것은 아닙니다.

## 5. 정량 지표

### 5.1 큰 파일

| 파일 | 라인 수 | 평가 |
|---|---:|---|
| `ui/web/spa.js` | 10,798 | 프론트엔드 앱 전체가 한 파일에 가까움 |
| `ui/web/style.css` | 7,627 | 일부 분리됐지만 여전히 큼 |
| `api/routes.py` | 6,618 | batch 분리 후에도 route monolith |
| `core/pipeline.py` | 1,960 | 핵심 orchestration이 과밀 |
| `core/job_queue.py` | 1,071 | SQLite queue + async wrapper가 한 파일 |
| `search/hybrid_search.py` | 906 | 검색 기능 단위로는 크지만 응집도는 비교적 좋음 |
| `search/chat.py` | 877 | chat/session/prompt/streaming이 함께 있음 |

### 5.2 Python 영역별 위험 신호

| 영역 | 파일 수 | 함수 수 | 80라인 이상 함수 | `Any` | `noqa` | `type: ignore` | `except Exception` |
|---|---:|---:|---:|---:|---:|---:|---:|
| `api` | 6 | 162 | 20 | 69 | 41 | 3 | 60 |
| `core` | 44 | 489 | 37 | 184 | 80 | 16 | 126 |
| `search` | 3 | 38 | 7 | 27 | 0 | 0 | 10 |
| `steps` | 16 | 189 | 15 | 79 | 0 | 11 | 28 |
| `ui` Python | 3 | 24 | 0 | 9 | 1 | 0 | 4 |

이 숫자의 해석:

- `ruff`가 통과하므로 표면적 스타일 품질은 좋습니다.
- 하지만 `Any`, broad exception, 긴 함수가 많아 **정적 안정성보다 runtime 방어에 기대는 영역**이 꽤 있습니다.
- 특히 `api`와 `core`는 제품의 중심인데, 암묵 계약이 많아 refactor 위험이 큽니다.

### 5.3 가장 긴 핵심 함수 후보

| 파일 | 함수 | 길이 | 평가 |
|---|---|---:|---|
| `core/pipeline.py` | `run` | 425 | pipeline orchestration 분리 필요 |
| `core/wiki/compiler.py` | `compile_meeting` | 308 | wiki compile workflow를 단계 객체로 분리할 후보 |
| `api/routes.py` | `update_settings` | 298 | settings service/schema 분리 필요 |
| `api/server.py` | `_lifespan` | 261 | runtime component bootstrapper 분리 필요 |
| `core/ab_test_runner.py` | `run_stt_ab_test` | 201 | runner 단계 모델화 필요 |
| `core/ab_test_runner.py` | `run_llm_ab_test` | 192 | STT/LLM 공통 runner 추출 가능 |
| `api/routers/meetings_batch.py` | `batch_action` | 176 | router 분리는 좋지만 service 추출은 아직 필요 |
| `search/chat.py` | `stream_chat` | 150 | streaming transport와 LLM orchestration 분리 후보 |

## 6. 코드 품질 평가

### 점수: 7.1 / 10

### 강점

1. `ruff check`와 `ruff format`을 통과합니다.
2. dataclass/Pydantic 모델을 적극적으로 사용합니다.
3. domain-specific exception이 여러 곳에 정의되어 있습니다.
4. 파일 쓰기 안정성을 위해 `core/io_utils.py` 같은 helper가 있습니다.
5. 최근 변경에서 native dependency injection과 runtime profile이 추가되어 테스트 가능성이 올라갔습니다.

### 약점

#### 6.1 타입 경계가 아직 얇다

`Any` 사용은 일부 외부 라이브러리와 ML/Chroma/MLX 때문에 피하기 어렵습니다. 문제는 `Any`가 외부 경계에만 머무르지 않고 route/service 내부까지 들어온다는 점입니다.

가장 먼저 줄일 곳:

- `api/routes.py`의 `request.app.state` 접근 함수
- `search/hybrid_search.py`의 Chroma/embedding model adapter
- `search/chat.py`의 backend interface
- `steps/embedder.py`의 sentence-transformers model adapter

권장:

```python
class ChatBackend(Protocol):
    def chat(self, messages: list[dict[str, str]], **kwargs: object) -> str: ...

class SearchEngine(Protocol):
    async def search(self, query: str, **kwargs: object) -> SearchResponse: ...
```

#### 6.2 broad exception이 많다

`except Exception`이 많은 것은 desktop/local AI 앱에서 어느 정도 현실적인 선택입니다. 녹음 장치, 모델, 파일 시스템, 외부 process는 예외 종류가 다양합니다.

하지만 지금은 "복구 가능한 예외"와 "프로그래밍 오류"가 같은 방식으로 처리될 가능성이 있습니다.

권장 분류:

| 예외 유형 | 처리 |
|---|---|
| 사용자 환경 문제 | HTTP 4xx/설정 가이드/상태 UI |
| 외부 dependency 실패 | degraded mode + warning |
| 파일 손상/누락 | quarantine/retry |
| 프로그래밍 오류 | fail fast + traceback |
| native runtime crash 위험 | subprocess/integration profile로 격리 |

#### 6.3 route-level helper가 너무 많다

`api/routes.py`에 helper, schema, endpoint가 함께 있습니다. batch route 분리는 좋지만 아직 대부분의 도메인이 남아 있습니다.

다음 추출 우선순위:

1. `api/dependencies.py`
2. `api/schemas/settings.py`
3. `api/services/settings_service.py`
4. `api/services/meeting_service.py`
5. `api/services/wiki_backfill_service.py`

## 7. 유지보수성 평가

### 점수: 6.6 / 10

### 좋아진 점

최근 변경은 매우 의미 있습니다.

- batch router 분리로 API monolith를 줄이기 시작했습니다.
- token CSS와 bulk actions CSS 분리로 cascade 범위를 줄이기 시작했습니다.
- runtime profile 도입으로 desktop app runtime과 test runtime이 분리됐습니다.
- 기본 pytest에서 native/ui/e2e를 분리해 일상 개발 루프가 안정화됐습니다.

이건 단순 정리가 아니라 "다음 리팩터링을 가능하게 하는 기반 작업"입니다.

### 아직 위험한 점

#### 7.1 `spa.js`는 아직 가장 큰 유지보수 리스크다

`spa.js`는 약 10,798라인이고, 검색 결과 기준 함수/콜백 후보가 900개 이상입니다. DOM query, event handler, `innerHTML`, fetch, timer, keyboard handler가 한 파일에 섞여 있습니다.

가장 큰 문제는 라인 수 자체가 아니라 **상태 전이의 소유권이 흐릿하다**는 점입니다.

예:

- list selection state
- router active state
- websocket-driven job state
- recording overlay state
- import modal state
- command palette state
- bulk action inflight state

이 상태들이 한 파일에서 DOM class와 attribute로 직접 반영됩니다. 지금은 테스트가 지켜주지만, 기능이 더 늘면 회귀 추적 비용이 커집니다.

권장 분리 순서:

1. `api-client.js`
2. `event-bus.js` 또는 작고 명시적인 custom event wrapper
3. `bulk-actions.js`
4. `recording-overlay.js`
5. `meetings-list.js`
6. `command-palette.js`
7. `router.js`

#### 7.2 CSS 분리는 시작됐지만 아직 중간 단계다

`tokens.css`와 `bulk-actions.css` 분리는 좋은 출발입니다. 특히 `bulk-actions.css`를 `style.css` 뒤에 로드해 기존 cascade 위치를 유지한 점은 실용적입니다.

남은 문제:

- `style.css`가 여전히 7,627라인입니다.
- 일부 comment에 encoding artifact가 남아 있습니다.
- token과 component style의 책임이 아직 완전히 분리되지는 않았습니다.

다음 분리 후보:

1. `layout.css`
2. `nav.css`
3. `meetings-list.css`
4. `viewer.css`
5. `settings.css`
6. `wiki.css`
7. `modals.css`

#### 7.3 `core/pipeline.py`는 domain 중심이지만 너무 많은 결정을 가진다

`PipelineManager.run()`이 425라인입니다. 이 함수는 step 실행, 상태 저장, 복구, backoff, timeout, 리소스 체크, 실패 처리, 이벤트 처리까지 담당합니다.

권장 분해:

```text
core/pipeline/
  state.py
  manager.py
  step_runner.py
  recovery.py
  resources.py
  checkpoints.py
  events.py
```

가장 먼저 분리할 것은 `state/checkpoints`입니다. 이 부분은 비교적 순수하고 테스트하기 쉽습니다.

## 8. 백엔드 아키텍처 평가

### 점수: 7.2 / 10

### 강점

- FastAPI app factory 구조가 있습니다.
- `runtime_profile`로 desktop/test runtime을 나누기 시작했습니다.
- route tests가 경량 profile에서 안정적으로 돌 수 있는 방향입니다.
- WebSocket, queue, recorder, watcher, pipeline, search/chat initialization이 명시적으로 관리됩니다.

### 핵심 문제

#### 8.1 lifespan이 composition root와 bootstrapper를 동시에 한다

`api/server.py::_lifespan`은 261라인입니다. 지금은 잘 작동하지만, component가 더 늘면 다음 문제가 생깁니다.

- 어떤 component가 어떤 profile에서 시작되는지 파악하기 어렵다.
- startup 실패가 degraded mode인지 fatal인지 판단하기 어렵다.
- 테스트에서 patch해야 할 대상이 많아진다.
- app.state contract가 계속 커진다.

권장:

```text
api/runtime/
  profiles.py
  bootstrap.py
  components.py
  shutdown.py
```

그리고 component를 이런 구조로 다룹니다.

```python
@dataclass
class RuntimeComponents:
    job_queue: AsyncJobQueue
    ws_manager: ConnectionManager
    search_engine: SearchEngine | None
    chat_engine: ChatEngine | None
    recorder: Recorder | None
    watcher: FolderWatcher | None
    processor: JobProcessor | None
```

#### 8.2 service layer가 부족하다

현재 API route가 domain logic을 많이 갖고 있습니다. 백엔드가 커질수록 route는 HTTP 경계만 담당해야 합니다.

권장 service:

| Service | 책임 |
|---|---|
| `MeetingService` | 목록/상세/삭제/재전사/파일 lookup |
| `UploadService` | 업로드 검증/저장/큐 등록 |
| `SettingsService` | config load/update/validation |
| `DashboardService` | 통계/캐시/요약 |
| `WikiBackfillService` | backfill job state/progress |
| `SearchService` | query validation/result normalization |

`api/routers/meetings_batch.py`는 router 분리의 좋은 시작이지만, `batch_action()` 내부가 176라인이라 service 추출이 다음 단계입니다.

## 9. 프론트엔드 아키텍처 평가

### 점수: 6.5 / 10

### 강점

- 정적 SPA로 많은 기능을 구현해 배포 복잡도를 낮췄습니다.
- a11y/visual/behavior 테스트가 있어 프론트엔드 품질을 지키는 장치가 있습니다.
- bulk actions CSS 분리와 token CSS 분리가 시작됐습니다.
- macOS native-like UI 방향성이 일관됩니다.

### 약점

#### 9.1 DOM 직접 조작이 많다

`querySelector`, `innerHTML`, direct class mutation, event listener가 많이 쓰입니다. 프레임워크 없는 SPA에서는 자연스럽지만, 지금 규모에서는 자체 규칙이 필요합니다.

권장 규칙:

- view module은 자기 root element 밖을 직접 수정하지 않는다.
- global state mutation은 `state.js`를 거친다.
- custom event 이름은 `events.js`에 상수화한다.
- `innerHTML` 사용은 template helper로 모은다.
- render와 bind를 분리한다.

#### 9.2 상태 모델이 DOM에 흩어져 있다

현재 selection state, route state, active item, recording state가 class/attribute와 JS closure state에 동시에 존재합니다.

권장:

```js
const appState = {
  route: { path, meetingId },
  meetings: { items, activeId, selectedIds, sort, query },
  recording: { active, mode, startedAt },
  bulk: { inflight, visible },
}
```

대형 상태관리 라이브러리까지 갈 필요는 없습니다. 작은 observable/store만 있어도 충분합니다.

#### 9.3 성능 병목은 목록 렌더링에서 먼저 올 가능성이 높다

회의 수가 많아지면 full re-render와 event rebinding이 먼저 체감될 가능성이 큽니다.

권장:

- selection 변경 시 list 전체 렌더링 금지
- item 단위 DOM update helper 도입
- 검색 입력 debounce 유지/강화
- 300개 이상 회의에서 virtualization 검토

## 10. AI 파이프라인 평가

### 점수: 8.1 / 10

### 강점

- Apple Silicon 로컬 AI라는 명확한 기술 전략이 있습니다.
- STT, diarization, correction, summarization, chunking, embedding, wiki compile까지 이어집니다.
- resource guard, dynamic timeout, checkpoint/resume이 있습니다.
- MLX prompt cache 같은 실측 기반 성능 개선이 이미 반영되어 있습니다.
- native cleanup을 test profile에서 격리하는 방향이 들어갔습니다.

### 약점

#### 10.1 pipeline step contract가 더 명시적이어야 한다

현재 step들은 dataclass result와 checkpoint를 사용하지만, orchestration 관점의 공통 interface가 약합니다.

권장:

```python
class PipelineStepRunner(Protocol):
    name: PipelineStep
    async def run(self, context: PipelineContext) -> StepResult: ...
    async def restore(self, context: PipelineContext) -> StepResult | None: ...
```

이렇게 되면 resume, retry, skip, backfill, partial run이 단순해집니다.

#### 10.2 native dependency는 더 강하게 adapter화해야 한다

MLX, pyannote, sentence-transformers, Chroma, Ollama는 각각 실패 양상이 다릅니다.

권장 adapter boundary:

```text
core/ai/
  stt_backend.py
  diarization_backend.py
  llm_backend.py
  embedding_backend.py
  vector_store.py
```

테스트에서는 fake backend를 쓰고, native integration은 marker로 분리합니다.

## 11. 성능 평가

### 점수: 7.6 / 10

### 이미 좋은 선택

| 영역 | 좋은 점 |
|---|---|
| LLM | prompt cache 최적화가 실측 기반으로 문서화됨 |
| SQLite | WAL 사용 |
| FTS | FTS5 기반 full-text search |
| Hybrid search | vector + FTS + RRF 결합 |
| Embedding | batch/adaptive batch/device switch 고려 |
| Async | blocking 작업을 `asyncio.to_thread`로 분리하는 패턴 |
| Runtime | profile로 무거운 component를 비활성화 가능 |

### 성능 개선의 핵심 후보

#### 11.1 meeting metadata index

현재 목록/대시보드/API 조회에서 파일 시스템과 JSON을 읽는 흐름이 여러 곳에 있습니다. 회의 수가 늘면 디스크 scan과 JSON parse가 UI 체감 지연으로 이어질 수 있습니다.

권장:

```text
meetings_index.sqlite
  meeting_id
  title
  created_at
  status
  has_audio
  has_transcript
  has_summary
  updated_at
```

효과:

- 목록 API latency 안정화
- dashboard stats 캐시 가능
- batch candidate collection 단순화
- search/wiki status와 연결 가능

#### 11.2 pipeline metric

성능 최적화는 단계별 duration이 있어야 합니다.

권장 metric:

- `convert_duration_ms`
- `transcribe_duration_ms`
- `diarize_duration_ms`
- `correct_duration_ms`
- `summarize_duration_ms`
- `embed_duration_ms`
- `wiki_compile_duration_ms`
- `checkpoint_write_bytes`
- `checkpoint_write_ms`
- `llm_prompt_cache_hit`

#### 11.3 UI render metric

SPA의 실제 병목은 사용자가 회의 300개, 1000개를 갖기 전까지 잘 보이지 않습니다.

권장:

- `performance.mark()`로 list render 시간 측정
- item count별 render time 기록
- visual test fixture에 대량 meeting scenario 추가

#### 11.4 compact checkpoint

`docs/PERFORMANCE_BACKLOG.md`에도 있는 내용입니다. 내부 checkpoint는 사람이 읽는 산출물이 아니므로 pretty JSON을 유지할 이유가 약합니다.

권장:

- 내부 checkpoint: compact JSON
- 사용자 export/debug 옵션: pretty JSON
- checkpoint 크기와 저장 시간 metric 추가

## 12. 운영성/관측성 평가

### 점수: 6.4 / 10

### 좋아진 점

- `docs/STATUS.md`가 생겼습니다.
- 기본 pytest에서 native/ui/e2e 제외가 명확해졌습니다.
- runtime profile로 실행 모드가 분리됐습니다.
- CI가 lint/test/UI gate를 나눠 실행합니다.

### 부족한 점

#### 12.1 health check가 더 풍부해야 한다

현재 health는 서버 생존 확인 중심입니다. 로컬 AI 앱에는 "무엇이 준비됐고 무엇이 degraded인지"가 중요합니다.

권장 health 항목:

- ffmpeg 사용 가능
- audio device readiness
- HF token presence
- pyannote model access
- STT model present
- LLM backend ready
- Chroma/FTS ready
- disk free
- Metal/MLX usable

#### 12.2 diagnostics bundle이 필요하다

오픈소스 사용자 지원을 하려면 "안 돼요" 상황에서 정보를 모아야 합니다.

권장:

```bash
meeting-transcriber doctor --json
meeting-transcriber diagnostics --output recap-diagnostics.zip
```

포함:

- config redacted
- environment
- dependency versions
- recent logs
- model status
- db status
- audio device list

민감 정보는 반드시 redaction해야 합니다.

## 13. 오픈소스 관점 평가

### 점수: 7.3 / 10

### 강점

- README와 docs가 풍부합니다.
- macOS/Apple Silicon이라는 타깃이 명확합니다.
- 테스트가 많고 UI 하네스도 있습니다.
- AGENTS 문서가 있어 AI coding agent가 프로젝트를 이해하기 쉽습니다.

### 약점

- 설치 조건이 복잡합니다: Apple Silicon, ffmpeg, BlackHole, HF token, gated model, MLX.
- 문서가 많아 최신 문서의 기준점을 놓치기 쉽습니다.
- 큰 파일이 많아 newcomer가 첫 PR을 넣기 어렵습니다.
- CI가 macOS runner에 의존해 비용과 대기 시간이 생길 수 있습니다.

권장:

- `docs/STATUS.md`를 canonical status로 유지
- `CONTRIBUTING.md`에 "첫 PR 추천 영역" 추가
- `good first issue` 후보를 작은 service/CSS 분리 작업으로 지정
- `AGENTS.md`는 유지하되 README에는 핵심만 요약

## 14. 리스크 매트릭스

| 리스크 | 심각도 | 가능성 | 설명 | 대응 |
|---|---:|---:|---|---|
| `spa.js` 상태 결합 | 높음 | 높음 | UI 기능 추가 때 회귀 가능 | feature module/state 분리 |
| `api/routes.py` monolith | 높음 | 높음 | route 변경 충돌/리뷰 비용 증가 | domain router + service layer |
| pipeline orchestration 과밀 | 중간-높음 | 중간 | resume/retry/partial run 복잡도 증가 | step runner/context 도입 |
| native dependency 환경 차이 | 높음 | 중간 | MLX/Metal/pyannote가 환경별 실패 | adapter + marker + doctor |
| meeting count 증가 시 UI/API 지연 | 중간 | 높음 | 파일 scan/render 비용 증가 | metadata index + keyed render |
| 문서 최신성 drift | 중간 | 중간 | 평가/상태 문서가 어긋날 수 있음 | STATUS canonical화 |
| untracked/generated 파일 커밋 | 낮음-중간 | 중간 | `__pycache__`, harness db 등 | gitignore/cleanup |

## 15. 전문가 권장 로드맵

### Phase 0: 현재 개선을 단단히 마감

목표: 지금 들어온 좋은 변경을 안전하게 닫는다.

1. `api/routers/__pycache__/`와 `harness.db` 같은 생성물을 커밋 대상에서 제외한다.
2. `runtime_profile` 설계를 README/STATUS/CONTRIBUTING에 짧게 문서화한다.
3. `api/routers/meetings_batch.py`의 `batch_action()`을 service로 한 번 더 얇게 만든다.
4. `tokens.css`의 encoding artifact comment를 정리한다.
5. 현재 통과한 `2624 passed` 결과를 STATUS에 기준 날짜와 함께 유지한다.

### Phase 1: 낮은 위험의 boundary 만들기

목표: 큰 refactor 없이 결합도를 줄인다.

1. `api/dependencies.py` 추가
2. `api/services/settings_service.py` 추가
3. `ui/web/api-client.js` 추가
4. `ui/web/events.js` 추가
5. `core/pipeline/checkpoints.py` 또는 `core/pipeline_state.py` 분리

### Phase 2: 테스트가 지켜주는 기능부터 분리

목표: 회귀 위험이 낮은 영역부터 실제 modularization을 한다.

1. `bulk-actions.js` 추출
2. `meetings_batch_service.py` 추출
3. `bulk-actions.css` visual baseline 유지 확인
4. command palette JS 분리
5. route 중 uploads/settings 먼저 분리

### Phase 3: 성능 기반 구조 개선

목표: 사용량 증가에 대비한다.

1. meeting metadata index 도입
2. dashboard stats TTL cache
3. search query embedding cache
4. pipeline step metrics
5. UI list render metrics

### Phase 4: 플랫폼 안정화

목표: 오픈소스 사용자 환경 다양성을 견딘다.

1. `doctor` 명령 추가
2. diagnostics bundle 추가
3. native integration test job 분리
4. MLX/Ollama/mock backend adapter 정리
5. setup guide를 "minimal mode"와 "full AI mode"로 나눔

## 16. 가장 높은 ROI 작업 TOP 10

| 순위 | 작업 | 효과 | 난이도 | 이유 |
|---:|---|---|---|---|
| 1 | `api/dependencies.py` | 높음 | 낮음 | `app.state`/`Any` 확산을 바로 줄임 |
| 2 | `ui/web/api-client.js` | 높음 | 낮음 | fetch/error/loading 정책 통일 |
| 3 | generated 파일 정리 | 중간 | 낮음 | 커밋 품질 즉시 개선 |
| 4 | `batch_action` service 추출 | 높음 | 중간 | 이미 router가 분리되어 다음 단계가 쉬움 |
| 5 | `tokens.css` comment encoding 정리 | 낮음-중간 | 낮음 | polish와 문서 품질 개선 |
| 6 | compact checkpoint 옵션 | 중간 | 낮음 | 성능 backlog에 이미 근거 있음 |
| 7 | `bulk-actions.js` 추출 | 높음 | 중간 | UI 테스트가 있어 안전망 있음 |
| 8 | `settings_service.py` 추출 | 높음 | 중간 | `update_settings` 298라인 해소 |
| 9 | pipeline checkpoint/state 분리 | 높음 | 중간 | `PipelineManager.run` 분해의 첫 단추 |
| 10 | meeting metadata index 설계 | 매우 높음 | 중간-높음 | 장기 성능과 API 단순화에 큰 영향 |

## 17. 최종 평가

현재 프로젝트는 이미 제품으로서 설득력이 있습니다. 특히 한국어 회의라는 실제 문제에 대해 로컬 AI, Apple Silicon, 녹음/전사/검색/채팅을 한 흐름으로 묶은 점은 강합니다.

최근 작업 디렉토리의 변경도 방향이 좋습니다. `runtime_profile`, native cleanup 격리, batch router 분리, CSS token/feature 분리는 모두 "나중에 크게 아플 지점"을 미리 줄이는 작업입니다.

하지만 전문가 관점에서 다음 승부처는 분명합니다.

> 기능 추가 속도보다, **경계 설정 속도**가 더 중요해지는 시점에 들어왔습니다.

앞으로의 핵심은 다음입니다.

- API는 route에서 service로 무게중심을 옮긴다.
- UI는 한 파일 앱에서 feature module 앱으로 이동한다.
- Pipeline은 큰 orchestration 함수에서 step contract로 이동한다.
- 성능은 감이 아니라 metric과 index/cache 정책으로 관리한다.
- Open-source 운영은 doctor/diagnostics/status 문서로 지원 비용을 낮춘다.

이 방향으로 2~3개 PR만 잘 쌓아도 점수는 빠르게 올라갈 수 있습니다.

예상 개선 후 점수:

| 항목 | 현재 | 1차 구조 개선 후 |
|---|---:|---:|
| 코드 품질 | 7.1 | 7.8 |
| 유지보수성 | 6.6 | 7.5 |
| 프론트엔드 아키텍처 | 6.5 | 7.4 |
| 백엔드 아키텍처 | 7.2 | 7.9 |
| 성능 설계 | 7.6 | 8.1 |
| 종합 | 7.3 | 7.9-8.1 |

최종적으로 이 프로젝트는 **기능 구현력이 이미 증명된 상태**입니다. 이제 필요한 것은 더 많은 기능보다, 그 기능들이 오래 버틸 수 있는 뼈대를 만드는 일입니다.

