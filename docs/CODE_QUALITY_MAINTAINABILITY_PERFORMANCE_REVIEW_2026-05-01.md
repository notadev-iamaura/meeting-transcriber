# 코드 품질·유지보수성·성능 개선 분석 보고서

- 작성일: 2026-05-01
- 기준 브랜치: `main`
- 기준 커밋: `4d7908353a34204c74042acbc65021c1742b15dc`
- 분석 관점: 테스트 안정성보다 **코드의 구조적 품질, 장기 유지보수성, 성능 개선 여지**에 초점
- 관련 종합 평가 문서: [`PROJECT_EVALUATION_MAIN_2026-04-30.md`](./PROJECT_EVALUATION_MAIN_2026-04-30.md)

## 1. 요약

이 프로젝트는 기능 범위와 제품 구현 밀도가 높은 편입니다. 특히 로컬 AI 파이프라인, 검색/RAG, 녹음/감시, 정적 SPA UI가 한 저장소 안에서 end-to-end로 연결되어 있습니다.

다만 코드 관점에서 보면 현재 가장 큰 문제는 **기능이 빠르게 쌓이면서 중심 파일들이 허브처럼 커졌고, domain/service/UI boundary가 충분히 분리되지 않았다는 점**입니다.

성능 측면에서는 이미 좋은 선택도 있습니다. MLX prompt cache, SQLite WAL, FTS 연결 캐시, embedding batch 처리, `asyncio.to_thread` 기반 blocking IO 분리 같은 개선이 보입니다. 하지만 남은 병목은 대부분 "알고리즘 하나를 바꾸면 끝"이 아니라 **데이터 흐름, 캐싱 경계, UI 렌더링 단위, native AI runtime 격리**를 정리해야 얻을 수 있는 유형입니다.

## 2. 종합 점수

| 관점 | 점수 | 평가 |
|---|---:|---|
| 코드 품질 | **6.8 / 10** | lint/format 기준은 좋지만 타입 경계와 책임 분리가 약함 |
| 유지보수성 | **6.2 / 10** | 기능별 구조는 있으나 핵심 파일 비대화가 큰 부담 |
| 성능 설계 | **7.4 / 10** | 성능 의식은 높고 일부 최적화가 적용됨. 추가 개선 여지도 큼 |
| 종합 개선 준비도 | **7.0 / 10** | 개선할 지점이 명확하고, 기존 테스트/문서가 받쳐줌 |

## 3. 코드베이스 구조 관찰

### 3.1 큰 파일 집중도

| 파일 | 라인 수 | 역할 | 평가 |
|---|---:|---|---|
| `ui/web/spa.js` | 10,798 | 정적 SPA 전체 상태/렌더링/이벤트 | 지나치게 큼 |
| `ui/web/style.css` | 7,627 | 전체 디자인 토큰/레이아웃/컴포넌트/기능 스타일 | cascade 관리 부담 큼 |
| `api/routes.py` | 6,618 | 대부분의 API route, DTO, helper | route monolith |
| `core/pipeline.py` | 1,960 | 파이프라인 상태, step 실행, 복구 | domain 핵심이지만 책임이 많음 |
| `core/job_queue.py` | 1,071 | SQLite queue + async wrapper | 구조는 명확하나 파일이 큼 |
| `search/hybrid_search.py` | 906 | vector/FTS/RRF/search engine | 성능 의식 좋음, 타입 경계 약함 |
| `search/chat.py` | 877 | chat session, prompt, LLM call, streaming | 역할 분리 여지 있음 |

### 3.2 현재 구조의 성격

현재 코드는 "작은 모듈이 조합되는 구조"라기보다, **기능별 중심 파일에 세부 구현이 많이 모이는 구조**입니다.

이 방식은 초기 개발 속도에는 유리합니다. 한 파일에서 흐름을 따라가기 쉽고, 기능을 빠르게 붙일 수 있습니다. 하지만 지금처럼 기능이 많이 쌓인 상태에서는 다음 문제가 커집니다.

- 작은 수정의 영향 범위를 예측하기 어렵다.
- 리뷰어가 변경 의도를 빠르게 파악하기 어렵다.
- 타입/테스트가 있어도 파일 내부 coupling이 강하면 refactor 비용이 크다.
- 신규 contributor가 "어디를 고치면 되는지" 찾기 어렵다.
- UI/CSS에서는 cascade와 event ordering 문제가 반복될 수 있다.

## 4. 코드 품질 분석

### 점수: 6.8 / 10

### 좋은 점

| 항목 | 평가 |
|---|---|
| lint/format | `ruff check`, `ruff format` 기준을 통과하는 상태 |
| 도메인 모델 | `Job`, `PipelineState`, `SearchResult`, `ChatResponse` 등 dataclass/Pydantic 모델이 존재 |
| 예외 타입 | `PipelineError`, `JobQueueError`, `SearchError`, `ChatError` 등 domain-specific exception이 있음 |
| 원자적 파일 쓰기 | `core/io_utils.py`로 atomic write helper가 분리됨 |
| 성능 주석 | `to_thread`, cache, WAL 등 의도 설명이 꽤 남아 있음 |
| 부분 모듈화 | `api/routers/meetings_batch.py`로 batch route 분리 시작 |

관련 파일:

- [`api/routers/meetings_batch.py`](../api/routers/meetings_batch.py)
- [`core/io_utils.py`](../core/io_utils.py)
- [`core/job_queue.py`](../core/job_queue.py)
- [`core/pipeline.py`](../core/pipeline.py)

### 개선이 필요한 점

#### 4.1 `Any`와 동적 `app.state` 의존이 많다

`api/routes.py`, `search/hybrid_search.py`, `search/chat.py`, `steps/embedder.py`, `core/watcher.py` 등에서 `Any`, `type: ignore`, `noqa`가 넓게 사용됩니다.

특히 API route에서 다음 패턴이 많습니다.

- `request.app.state`에서 객체를 꺼냄
- 반환 타입은 `Any`
- 실제 객체 interface는 암묵적
- 실패 시 runtime에서야 드러남

이 구조는 기능이 적을 때는 간단하지만, 지금처럼 app state에 queue, recorder, pipeline, search engine, chat engine, watcher, config가 많이 올라가면 refactor 안정성이 떨어집니다.

개선 방향:

```python
from typing import Protocol

class PipelineManagerProtocol(Protocol):
    async def run(self, audio_path: Path, *, meeting_id: str | None = None) -> object: ...
    async def resume(self, meeting_id: str) -> object: ...

class JobQueueProtocol(Protocol):
    async def get_job(self, job_id: int) -> object: ...
    async def get_pending_jobs(self) -> list[object]: ...
```

이런 Protocol을 한 번에 완벽하게 도입할 필요는 없습니다. 먼저 `api/dependencies.py`에 `get_pipeline_manager()`, `get_job_queue()`를 옮기고, 반환 타입만 좁혀도 효과가 큽니다.

#### 4.2 route layer가 너무 많은 책임을 가진다

`api/routes.py`는 API handler, Pydantic schema, 파일 탐색, JSON 로딩, pipeline orchestration, 설정 저장, wiki/status/backfill logic까지 포함합니다.

현재 이미 `api/routers/meetings_batch.py` 분리가 시작되어 있어 방향은 좋습니다. 다음 단계는 route를 더 얇게 만드는 것입니다.

권장 구조:

```text
api/
  dependencies.py
  schemas/
    meetings.py
    search.py
    settings.py
    wiki.py
  routers/
    meetings.py
    meetings_batch.py
    uploads.py
    search.py
    settings.py
    wiki.py
  services/
    meeting_service.py
    upload_service.py
    dashboard_service.py
    wiki_backfill_service.py
```

핵심은 "파일을 나누자"가 아니라, **route가 HTTP 경계만 담당하고 domain 작업은 service가 담당하게 하는 것**입니다.

#### 4.3 import 위치와 선택적 dependency 경계가 불규칙하다

일부 import는 파일 중간에 있고 `noqa: E402`, `PLC0415`가 붙어 있습니다. 이는 lazy import와 optional dependency 때문에 필요한 경우가 있지만, 지금은 정책이 파일 전반에 섞여 있습니다.

개선 방향:

- optional/native dependency import는 adapter module로 모읍니다.
- route 파일 내부 lazy import는 줄입니다.
- "실패해도 폴백 가능한 dependency"와 "없으면 실행 불가능한 dependency"를 구분합니다.

예:

```text
core/llm/
  backend.py
  mlx_adapter.py
  ollama_adapter.py
  errors.py
```

#### 4.4 logging과 CLI/debug 출력이 섞여 있다

`search/chat.py`, `search/hybrid_search.py`, `core/pipeline.py`, `steps/*` 일부에 `print()`가 남아 있습니다. 샘플 실행용이면 괜찮지만, 앱 코드와 섞이면 GUI/daemon 실행에서 출력 관리가 어렵습니다.

개선 방향:

- 라이브러리/앱 경로는 `logger` 사용
- `if __name__ == "__main__"` 데모 출력은 유지 가능
- 사용자에게 보여줄 메시지는 API response/event로 분리

## 5. 유지보수성 분석

### 점수: 6.2 / 10

### 좋은 점

- 기능 영역은 디렉터리 단위로 어느 정도 나뉘어 있습니다.
- `core`, `steps`, `search`, `api`, `ui`, `docs`, `tests`의 큰 구분은 이해하기 쉽습니다.
- pipeline step이 `convert → transcribe → diarize → merge → correct → summarize → chunk → embed → wiki` 흐름으로 명확합니다.
- 성능/설계/보안 문서가 있어 의사결정 맥락이 남아 있습니다.

### 유지보수 비용을 키우는 요인

#### 5.1 `spa.js`가 사실상 프론트엔드 애플리케이션 전체다

현재 `spa.js`는 상태 저장, DOM 렌더링, keyboard handler, bulk actions, command palette, API 호출, UI event wiring을 모두 포함합니다.

권장 분리 순서:

| 우선순위 | 분리 대상 | 이유 |
|---:|---|---|
| 1 | `api-client.js` | fetch/error handling 중복 제거, 테스트 쉬움 |
| 2 | `bulk-actions.js` | 최근 기능이고 테스트가 있어 분리 안전 |
| 3 | `command-palette.js` | 독립 UI/상태 모델로 분리하기 좋음 |
| 4 | `meetings-list.js` | 렌더링 성능 개선과 연결됨 |
| 5 | `state.js` | 전역 상태 mutation 추적 가능 |

분리할 때 대형 refactor를 한 번에 하지 않는 편이 좋습니다. 이미 UI 테스트가 있으므로, 한 모듈씩 추출하고 behavior/a11y/visual gate를 돌리는 방식이 안전합니다.

#### 5.2 `style.css`가 디자인 시스템과 기능 스타일을 모두 품고 있다

현재 CSS에는 토큰, dark mode, layout, settings, wiki, bulk actions, responsive rule이 함께 들어 있습니다.

권장 구조:

```text
ui/web/styles/
  tokens.css
  base.css
  layout.css
  components/
    buttons.css
    list.css
    modal.css
    toolbar.css
  features/
    meetings.css
    bulk-actions.css
    command-palette.css
    wiki.css
    settings.css
  responsive.css
```

이 분리는 단순 취향 문제가 아닙니다. CSS가 커질수록 시각 회귀는 "어떤 selector가 이겼는지"를 찾는 시간이 비용이 됩니다. 기능별 CSS 파일은 회귀 범위를 줄입니다.

#### 5.3 `PipelineManager`가 orchestration과 recovery를 모두 담당한다

`core/pipeline.py`는 파이프라인 핵심이라 어느 정도 큰 것은 자연스럽습니다. 다만 현재는 step 실행, checkpoint 복구, input validation, timeout, resource guard, intermediate restore까지 한 클래스에 모입니다.

권장 구조:

```text
core/pipeline/
  manager.py
  state.py
  steps.py
  recovery.py
  resources.py
  timeouts.py
```

또는 파일 분리 전이라도 내부 책임을 먼저 나눌 수 있습니다.

- `PipelineState`와 checkpoint IO
- step runner
- recovery/resume
- resource guard
- LLM lock/timeout

#### 5.4 service boundary가 더 필요하다

현재 route와 core 사이에 service layer가 얇습니다. 예를 들어 "회의 삭제", "회의 재전사", "대시보드 통계", "wiki backfill"은 HTTP와 무관한 domain operation입니다.

이것들을 service로 옮기면 다음 이점이 있습니다.

- API와 CLI/menubar/native UI가 같은 logic을 재사용 가능
- route 테스트보다 빠른 service unit test 가능
- 파일 시스템과 queue side effect를 더 좁게 mock 가능
- handler가 짧아져 API 리뷰가 쉬워짐

## 6. 성능 분석

### 점수: 7.4 / 10

### 이미 잘 되어 있는 점

| 영역 | 현재 장점 |
|---|---|
| LLM | `docs/PERFORMANCE_BACKLOG.md` 기준 MLX prompt cache로 corrector 단계 실측 37.6% 개선 |
| SQLite/FTS | WAL 사용, FTS 연결 캐시, RRF 결합 |
| Embedding | batch 처리, 대량 chunk adaptive batch size, 짧은 chunk CPU 전환 로직 |
| Async API | blocking queue/file/search 작업 일부를 `asyncio.to_thread`로 분리 |
| File write | atomic write helper 존재 |
| Pipeline | resource guard, dynamic timeout, resume/checkpoint 구조 |

관련 문서:

- [`docs/PERFORMANCE_BACKLOG.md`](./PERFORMANCE_BACKLOG.md)
- [`docs/performance/phase6-bulk-actions-perf-audit.md`](./performance/phase6-bulk-actions-perf-audit.md)

### 성능 병목 후보

#### 6.1 API route의 파일 시스템 스캔과 JSON 로딩

회의 목록, 대시보드, transcript/summary 조회, wiki status 쪽은 파일 시스템과 JSON 파일을 자주 읽습니다. `_JsonFileCache`가 있지만 route-local helper라 캐시 정책이 제한적입니다.

개선 방향:

- meeting metadata index를 별도로 유지합니다.
- 파일 mtime 기반 cache invalidation을 service layer로 이동합니다.
- dashboard stats는 짧은 TTL cache를 둡니다.
- 대량 목록 API는 pagination/limit/sort key를 명확히 합니다.

예상 효과:

- 회의 수가 늘어날수록 홈/목록 로딩 시간 안정화
- UI interaction 시 불필요한 disk IO 감소
- API route 코드 단순화

#### 6.2 SPA 목록 렌더링 비용

회의 목록이 커지면 `spa.js`의 전체 목록 재렌더링과 event binding 비용이 커질 수 있습니다.

개선 방향:

- list rendering을 keyed update로 전환
- 최소한 selected/active 상태 변경 시 전체 item HTML 재생성 방지
- 회의 수가 많아질 경우 list virtualization 도입
- 검색/필터 입력에 debounce 적용

권장 순서:

1. render 함수에서 "데이터 변경"과 "선택 상태 변경" 분리
2. item DOM update helper 추가
3. 300개 이상 목록에서 virtualization 검토

#### 6.3 JSON checkpoint pretty-print 비용

`docs/PERFORMANCE_BACKLOG.md`에도 이미 언급되어 있듯, 내부 checkpoint JSON에 `indent=2`를 계속 쓰면 긴 회의에서 파일 크기와 IO가 늘어납니다.

개선 방향:

- 내부 checkpoint는 compact JSON 사용
- 사람이 읽는 export 결과만 pretty 유지
- `orjson`은 optional dependency로 검토

예상 효과:

- 장시간 회의 checkpoint 저장 시간 감소
- 디스크 사용량 감소
- resume 로딩 시간 소폭 개선

#### 6.4 검색/RAG의 warmup과 cache 정책

`HybridSearchEngine`은 embedding model, Chroma collection, FTS connection lazy cache를 가지고 있어 방향은 좋습니다. 다음 개선은 query/result cache입니다.

개선 방향:

- 동일 query + filter 조합에 짧은 TTL cache
- embedding query vector cache
- 빈 query/짧은 query fast path 강화
- search result serialization 비용 측정

주의:

검색 cache는 meeting index가 업데이트될 때 invalidation이 필요합니다. chunk/embed 단계 완료 이벤트와 연결하는 방식이 좋습니다.

#### 6.5 AI pipeline 단계별 병렬성

현재 pipeline은 단계 순서가 강한 편입니다. 일부 단계는 본질적으로 순차적이지만, 다음은 병렬화 또는 overlap 여지가 있습니다.

| 후보 | 가능성 | 주의점 |
|---|---|---|
| chunk embedding batch | 높음 | 메모리/Metal pressure 관리 필요 |
| wiki compile 후 index update | 중간 | 실패 복구/부분 반영 정책 필요 |
| summary와 embedding 일부 overlap | 낮음-중간 | correct 결과 의존성 확인 필요 |
| batch meetings 처리 | 중간 | thermal manager와 job queue fairness 필요 |

권장 방향은 무조건 병렬화가 아닙니다. 이 앱은 로컬 Apple Silicon에서 돌기 때문에, 병렬화가 체감 성능보다 thermal throttling과 memory pressure를 키울 수 있습니다. 먼저 단계별 duration metric을 수집한 뒤 병렬화해야 합니다.

## 7. 개선 우선순위

### P0: 지금 바로 효과가 큰 개선

| 작업 | 관점 | 이유 | 난이도 |
|---|---|---|---|
| `api/dependencies.py` 도입 | 코드 품질 | `app.state` 접근과 `Any` 반환을 한곳으로 모음 | S |
| `api/routes.py`에서 batch 외 route 추가 분리 | 유지보수 | monolith 감소, 리뷰 범위 축소 | M |
| `spa.js`에서 `api-client.js` 분리 | 유지보수 | fetch/error handling 경계 확보 | S |
| 내부 checkpoint compact JSON 전환 | 성능 | 낮은 위험으로 IO/용량 개선 | S |
| `print()`를 logger 또는 demo block으로 정리 | 코드 품질 | 앱 실행 출력 관리 개선 | S |

### P1: 구조 개선 효과가 큰 작업

| 작업 | 관점 | 이유 | 난이도 |
|---|---|---|---|
| `bulk-actions.js` 분리 | 유지보수 | 테스트가 있어 안전하게 추출 가능 | M |
| `style.css`를 feature CSS로 분리 | 유지보수 | visual 회귀 분석 비용 감소 | M |
| meeting metadata index/service 도입 | 성능 | 목록/대시보드 FS scan 비용 감소 | M |
| `PipelineManager` recovery/state 분리 | 유지보수 | pipeline refactor 기반 마련 | M |
| search query embedding cache | 성능 | 반복 검색 체감 개선 | M |

### P2: 장기 품질을 올리는 작업

| 작업 | 관점 | 이유 | 난이도 |
|---|---|---|---|
| service layer 정식 도입 | 유지보수 | API/CLI/native UI logic 재사용 가능 | L |
| runtime profile 체계화 | 품질/성능 | desktop/api-test/ui-test/unit-test 경계 명확화 | L |
| UI list virtualization | 성능 | 대량 회의 목록에서 필요 | M-L |
| pipeline step registry | 유지보수 | step 추가/스킵/재시도 정책 단순화 | L |
| native AI adapter boundary 정리 | 품질/성능 | MLX/Ollama/mock backend 격리 | L |

## 8. 추천 리팩터링 순서

### 1단계: 작은 경계부터 세운다

가장 먼저 큰 파일을 바로 찢기보다, 반복되는 의존성 접근과 API 호출 경계를 분리합니다.

권장 PR:

1. `api/dependencies.py` 추가
2. `ui/web/api-client.js` 추가
3. `print()` 정리
4. checkpoint compact JSON 옵션 추가

이 단계는 리스크가 작고, 이후 큰 분리를 쉽게 만듭니다.

### 2단계: 테스트가 있는 기능부터 분리한다

bulk actions는 behavior/a11y/visual 테스트가 있으므로 분리하기 좋은 후보입니다.

권장 PR:

1. `ui/web/bulk-actions.js` 추출
2. `ui/web/styles/features/bulk-actions.css` 추출
3. 기존 UI 테스트 그대로 통과 확인

### 3단계: API route를 domain별로 나눈다

이미 `api/routers/meetings_batch.py`가 있으므로 같은 패턴을 확장합니다.

권장 분리 순서:

1. `uploads.py`
2. `meetings.py`
3. `dashboard.py`
4. `search.py`
5. `settings.py`
6. `wiki.py`

### 4단계: 성능 측정을 제품 루프에 넣는다

성능 개선은 감으로 하지 말고, 단계별 duration과 cache hit rate를 기록해야 합니다.

권장 metric:

- meeting list API latency
- dashboard stats latency
- pipeline step duration
- checkpoint write/read size and time
- search latency: embedding / vector / FTS / RRF split
- UI list render time
- LLM prompt cache hit rate

## 9. 개선 후 기대 효과

| 개선 영역 | 기대 효과 |
|---|---|
| route 분리 | API 변경 리뷰가 쉬워지고 충돌 감소 |
| service layer | UI/API/CLI 간 로직 중복 감소 |
| JS 모듈화 | UI 회귀 범위 축소, 기능별 테스트 쉬움 |
| CSS 분리 | visual regression 원인 추적 쉬움 |
| metadata index | 회의 수 증가 시 목록/대시보드 성능 안정 |
| compact checkpoint | 장시간 회의 IO와 저장 용량 감소 |
| search cache | 반복 질의 응답성 개선 |
| native adapter 정리 | MLX/Ollama/mock backend 전환 안정화 |

## 10. 결론

이 코드베이스는 "안 돌아가는 코드"가 아니라, **기능이 빠르게 성공적으로 쌓인 뒤 이제 구조 정리가 필요한 코드**에 가깝습니다.

가장 중요한 개선 방향은 세 가지입니다.

1. **큰 파일을 기능 경계로 나눈다.**
2. **route/UI가 직접 많은 일을 하지 않게 service와 module boundary를 세운다.**
3. **성능 개선은 단계별 metric과 cache invalidation 정책을 함께 설계한다.**

현재 프로젝트에는 테스트와 문서가 이미 어느 정도 있으므로, 무리한 대형 개편보다 작은 경계부터 만들고 green 상태를 유지하는 방식이 가장 현실적입니다.

추천 시작점은 다음입니다.

> `api/dependencies.py` 도입 → `api-client.js` 분리 → compact checkpoint 옵션 → bulk-actions JS/CSS 추출 → route domain 분리

이 순서가 코드 품질, 유지보수성, 성능 개선을 동시에 가장 낮은 위험으로 밀어 올릴 가능성이 큽니다.

