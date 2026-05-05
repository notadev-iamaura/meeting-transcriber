# 메인 브랜치 기준 프로젝트 객관 평가 보고서

- 평가일: 2026-04-30
- 기준 브랜치: `main`
- 기준 커밋: `4d7908353a34204c74042acbc65021c1742b15dc`
- `origin/main`: `4d7908353a34204c74042acbc65021c1742b15dc`
- 실행 Python: `.venv/bin/python` 3.12.8
- 평가 범위: 프론트엔드, UI/UX, 백엔드/API, AI 파이프라인, 아키텍처, 테스트/QA, 보안/프라이버시, 문서화
- 주의: 현재 작업 디렉토리에는 untracked 문서와 `harness.db`가 있으나, 추적 파일 기준 `main` 코드는 깨끗한 상태에서 평가했습니다.

## 1. 한 줄 결론

`main` 브랜치는 이전 작업 브랜치 평가보다 훨씬 안정적입니다. lint, formatter, route 테스트, bulk actions UI behavior/a11y/visual gate가 모두 통과했습니다. 다만 기본 전체 pytest 실행은 `core.model_manager`의 MLX/GPU cache cleanup 경로에서 `Fatal Python error: Aborted`로 끝나므로, **아직 완전한 릴리스 안정 상태는 아닙니다.**

## 2. 종합 점수

| 항목 | 점수 |
|---|---:|
| 종합 점수 | **7.6 / 10** |
| 제품 완성도 | **베타 초입-중반** |
| 기능 완성도 | **높음** |
| 현재 메인 브랜치 안정성 | **보통 이상** |
| 오픈소스 공개 준비도 | **양호하나 전체 테스트 안정화 필요** |

### 종합 판단

이 프로젝트는 단순 전사 도구가 아니라 **로컬 AI 회의 지식 관리 앱**에 가깝습니다. Recorder, folder watcher, job queue, STT, diarization, local LLM correction/summarization, RAG, web UI, command palette, bulk actions까지 end-to-end 제품 구성이 갖춰져 있습니다.

메인 브랜치 기준으로는 품질 게이트가 상당히 개선되어 있습니다. 특히 bulk actions 관련 behavior/a11y/visual 테스트가 모두 통과한다는 점은 UI 작업이 어느 정도 안정화되었음을 보여줍니다.

하지만 전체 테스트 스위트를 한 번에 돌렸을 때 Python 프로세스가 abort되는 문제는 심각합니다. 이는 특정 테스트 하나의 실패라기보다, MLX/Metal/native extension과 test lifecycle 경계가 아직 충분히 격리되지 않았다는 신호입니다.

## 3. 검증 결과

### 통과한 검증

| 명령 | 결과 | 해석 |
|---|---:|---|
| `.venv/bin/ruff check .` | `All checks passed!` | lint 기준 통과 |
| `.venv/bin/ruff format --check .` | `245 files already formatted` | formatter 기준 통과 |
| `.venv/bin/python -m pytest tests/test_config.py tests/test_job_queue.py tests/test_hybrid_search.py -q` | `190 passed` | 설정/큐/검색 핵심 unit 안정 |
| `.venv/bin/python -m pytest -m harness -q` | `54 passed` | UI/UX 하네스 기반 테스트 통과 |
| `.venv/bin/python -m pytest tests/test_routes_meetings_batch.py -q` | `25 passed` | batch API route 테스트 통과 |
| `.venv/bin/python -m pytest tests/test_routes_home_dashboard.py tests/test_routes.py -q` | `101 passed` | 주요 route 테스트 통과 |
| `.venv/bin/python -m pytest -m ui tests/ui/behavior/test_bulk_actions_behavior.py -q` | `29 passed` | bulk actions 동작 테스트 통과 |
| `.venv/bin/python -m pytest -m ui tests/ui/a11y/test_bulk_actions_a11y.py -q` | `10 passed` | bulk actions 접근성 테스트 통과 |
| `.venv/bin/python -m pytest -m ui tests/ui/visual/test_bulk_actions_visual.py -q` | `6 passed` | bulk actions visual baseline 테스트 통과 |

### 실패 또는 주의가 필요한 검증

| 명령 | 결과 | 해석 |
|---|---|---|
| `.venv/bin/python -m pytest -q` | `Fatal Python error: Aborted` | 전체 스위트 실행 중 native/MLX cleanup 경로에서 프로세스 abort |

전체 pytest의 abort stack은 다음 경로를 가리켰습니다.

- `tests/wiki/test_llm_client.py`
- `core/wiki/llm_client.py`
- `core/model_manager.py`
- `core/model_manager.py`의 `_clear_gpu_cache()`에서 `import mlx.core as mx` 시도

관련 코드 위치:

- [`core/model_manager.py:152`](/Users/youngouksong/projects/meeting-transcriber/core/model_manager.py:152)
- [`tests/wiki/test_llm_client.py:294`](/Users/youngouksong/projects/meeting-transcriber/tests/wiki/test_llm_client.py:294)

### 테스트 결과 해석

메인 브랜치는 **개별 기능 게이트 기준으로는 건강한 편**입니다. 하지만 전체 스위트 실행이 abort되는 한, CI/CD나 릴리스 신뢰성 점수는 제한됩니다. 특히 AI/native dependency를 포함한 테스트는 subprocess 격리 또는 mock 경계를 더 강하게 둘 필요가 있습니다.

## 4. 카테고리별 점수

| 카테고리 | 점수 | 등급 | 요약 |
|---|---:|---|---|
| 프론트엔드 구현 | **7.4 / 10** | 좋음 | 기능 밀도와 테스트 통과는 좋지만 단일 파일 과밀 |
| UI/UX | **7.3 / 10** | 좋음 | bulk actions behavior/a11y/visual gate 통과, 제품성 있음 |
| 백엔드/API | **7.8 / 10** | 좋음 | route 테스트 통과, API 기능 폭 넓음, route monolith는 부담 |
| AI/도메인 파이프라인 | **8.0 / 10** | 좋음-우수 | 로컬 AI 제품으로 구현 깊이가 높음, native dependency 리스크 존재 |
| 아키텍처/유지보수성 | **6.2 / 10** | 보통 | 구조는 잡혀 있으나 핵심 모듈 비대화와 lifecycle 결합이 큼 |
| 테스트/QA | **7.2 / 10** | 좋음 | 테스트 체계는 강하지만 전체 pytest abort가 큰 감점 |
| 보안/프라이버시 | **7.2 / 10** | 좋음 | local-first와 경로 검증 방향은 좋음, 운영 경계 문서화 필요 |
| 문서화/커뮤니티 | **8.0 / 10** | 좋음 | 문서와 설계 기록이 풍부함, 최신 상태 표시는 더 필요 |

## 5. 프론트엔드 구현 평가

### 점수: 7.4 / 10

### 좋은 점

- 정적 SPA임에도 실제 앱 수준의 기능이 많습니다.
- 회의 목록, 상세 뷰, 검색, command palette, bulk actions, 모바일 대응, empty state가 포함되어 있습니다.
- bulk actions UI는 DOM, state, CSS, behavior test, a11y test, visual baseline이 함께 관리됩니다.
- 메인 기준 bulk actions behavior 테스트가 `29 passed`, visual 테스트가 `6 passed`로 통과합니다.

관련 위치:

- [`ui/web/index.html:124`](/Users/youngouksong/projects/meeting-transcriber/ui/web/index.html:124)
- [`ui/web/spa.js:386`](/Users/youngouksong/projects/meeting-transcriber/ui/web/spa.js:386)
- [`ui/web/spa.js:1441`](/Users/youngouksong/projects/meeting-transcriber/ui/web/spa.js:1441)
- [`ui/web/style.css:7996`](/Users/youngouksong/projects/meeting-transcriber/ui/web/style.css:7996)

### 아쉬운 점

- `ui/web/spa.js`가 약 10,798 LOC입니다.
- `ui/web/style.css`가 약 8,343 LOC입니다.
- 프레임워크 없는 정적 SPA 구조에서 이 정도 규모가 되면 상태 전이, 이벤트 핸들러, CSS cascade의 회귀 위험이 높습니다.
- 기능별 module boundary가 약해 신규 contributor가 특정 UI만 수정하기 어렵습니다.

### 개선 제안

1. `spa.js`를 기능별 모듈로 나눕니다.
   - `api-client`
   - `meetings-list`
   - `meeting-detail`
   - `bulk-actions`
   - `command-palette`
   - `settings`
2. `style.css`를 토큰/레이아웃/컴포넌트/기능별 CSS로 분리합니다.
3. UI state mutation을 중앙화하고, DOM 직접 조작 범위를 줄입니다.

## 6. UI/UX 평가

### 점수: 7.3 / 10

### 좋은 점

- macOS 데스크톱 앱에 가까운 조용한 생산성 도구 톤을 지향합니다.
- bulk actions는 회의 관리 앱에서 실제로 필요한 워크플로입니다.
- 접근성 테스트가 통과합니다.
- focus-visible, mobile responsive, dark mode, reduced motion 대응 흔적이 있습니다.
- command palette는 고급 사용자에게 유용한 탐색/실행 모델입니다.

관련 위치:

- [`ui/web/style.css:4066`](/Users/youngouksong/projects/meeting-transcriber/ui/web/style.css:4066)
- [`ui/web/style.css:8317`](/Users/youngouksong/projects/meeting-transcriber/ui/web/style.css:8317)
- [`ui/web/spa.js:985`](/Users/youngouksong/projects/meeting-transcriber/ui/web/spa.js:985)

### 아쉬운 점

- 실제 앱 전체를 수동으로 장시간 사용해 확인한 것은 아니므로, 테스트가 커버하지 못하는 UX 마찰은 남아 있을 수 있습니다.
- 프레임워크 없는 SPA에서 복잡한 keyboard interaction과 modal/overlay가 늘어나면 focus trap, z-index, pointer event 문제가 반복될 가능성이 있습니다.
- UI 테스트가 bulk actions 중심이라, 전체 앱의 모든 화면 품질을 동일 수준으로 보장한다고 보긴 어렵습니다.

### 평가

메인 기준 UI/UX는 이전 작업 브랜치보다 확실히 좋습니다. 특히 behavior/a11y/visual이 모두 green인 점은 객관적인 근거입니다. 다만 장기적으로는 UI 구조를 쪼개지 않으면 현재 품질을 유지하는 비용이 계속 커질 가능성이 높습니다.

## 7. 백엔드/API 평가

### 점수: 7.8 / 10

### 좋은 점

- FastAPI 기반 API 구조가 명확합니다.
- upload, meetings, batch, dashboard, search, settings, websocket 등 제품에 필요한 API 범위가 넓습니다.
- batch API route 테스트가 `25 passed`로 통과합니다.
- 주요 route 묶음도 `101 passed`로 통과합니다.
- 서버 lifespan에서 검색 엔진, chat engine, recorder, watcher, pipeline, job processor 등을 초기화합니다.

관련 위치:

- [`api/server.py:80`](/Users/youngouksong/projects/meeting-transcriber/api/server.py:80)
- [`api/server.py:186`](/Users/youngouksong/projects/meeting-transcriber/api/server.py:186)
- [`api/server.py:200`](/Users/youngouksong/projects/meeting-transcriber/api/server.py:200)
- [`api/routes.py:3359`](/Users/youngouksong/projects/meeting-transcriber/api/routes.py:3359)

### 아쉬운 점

- `api/routes.py`가 약 7,078 LOC입니다.
- route layer가 request validation, file IO, pipeline orchestration, response mapping을 너무 많이 담당합니다.
- 기능별 router 분리는 장기 유지보수 관점에서 필요합니다.
- native runtime dependency가 app/test lifecycle과 만나는 지점은 아직 위험합니다.

### 개선 제안

1. `api/routes.py`를 domain router로 분리합니다.
   - `routes/meetings.py`
   - `routes/uploads.py`
   - `routes/batch.py`
   - `routes/search.py`
   - `routes/settings.py`
   - `routes/wiki.py`
2. route handler는 thin controller로 낮추고 service layer를 둡니다.
3. 테스트용 app profile에서 recorder/watcher/native model cleanup을 기본 비활성화합니다.

## 8. AI/도메인 파이프라인 평가

### 점수: 8.0 / 10

### 좋은 점

- Apple Silicon 로컬 AI라는 기술 방향이 명확합니다.
- MLX Whisper, pyannote diarization, local LLM correction/summarization, RAG 검색까지 범위가 넓습니다.
- ChromaDB와 SQLite FTS5를 조합한 hybrid search 방향은 회의 지식 검색에 적합합니다.
- thermal management와 model manager를 고려한 점은 실제 로컬 AI 앱에 필요한 판단입니다.

관련 위치:

- [`core/pipeline.py`](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py)
- [`core/job_queue.py`](/Users/youngouksong/projects/meeting-transcriber/core/job_queue.py)
- [`search/hybrid_search.py`](/Users/youngouksong/projects/meeting-transcriber/search/hybrid_search.py)
- [`search/chat.py`](/Users/youngouksong/projects/meeting-transcriber/search/chat.py)
- [`core/model_manager.py:152`](/Users/youngouksong/projects/meeting-transcriber/core/model_manager.py:152)

### 아쉬운 점

- MLX/Metal/native extension은 테스트 프로세스 안정성에 직접 영향을 줍니다.
- 전체 pytest abort는 AI runtime cleanup 경계가 충분히 격리되지 않았다는 신호입니다.
- 모델, 토큰, 하드웨어 조건이 맞지 않으면 onboarding 난도가 높습니다.
- heavy dependency와 mock/test backend의 경계를 더 명확히 해야 합니다.

### 개선 제안

1. CI와 일반 unit test에서는 MLX import를 절대 수행하지 않는 mock path를 강제합니다.
2. native model cleanup은 subprocess integration test로 분리합니다.
3. `run_preflight()` 결과를 테스트에서 명시적으로 주입할 수 있게 합니다.
4. model manager에 `disable_gpu_cache_cleanup_for_tests` 같은 config 경계를 둡니다.

## 9. 아키텍처/유지보수성 평가

### 점수: 6.2 / 10

### 좋은 점

- 큰 디렉터리 구조는 이해하기 쉽습니다.
- `api`, `core`, `search`, `steps`, `ui`, `tests`, `docs` 영역이 구분됩니다.
- job queue, watcher, pipeline, search, wiki 등 도메인 개념이 명확합니다.
- lint와 formatter가 메인에서 통과합니다.

### 주요 리스크

| 리스크 | 근거 | 영향 |
|---|---|---|
| 핵심 파일 비대화 | `api/routes.py` 7,078 LOC, `spa.js` 10,798 LOC, `style.css` 8,343 LOC | review, 테스트, 회귀 분석 비용 증가 |
| runtime/test lifecycle 혼합 | server lifespan에서 watcher, recorder, model, job processor 초기화 | 테스트 환경에서 native side effect 발생 가능 |
| native cleanup abort | 전체 pytest 중 `core/model_manager.py` 경로에서 abort | CI 신뢰도 저하 |
| Apple Silicon 중심성 | MLX/Metal 의존 | 다른 환경 contributor 진입 장벽 증가 |

### 개선 제안

1. app factory에 명시적 runtime profile을 둡니다.
   - `desktop`
   - `api-test`
   - `ui-test`
   - `unit-test`
2. native dependency는 profile별로 lazy-load 또는 no-op adapter를 사용합니다.
3. `routes.py`, `spa.js`, `style.css`를 먼저 나눕니다.
4. public interface와 internal helper를 명확히 분리합니다.

## 10. 테스트/QA 평가

### 점수: 7.2 / 10

### 좋은 점

- 테스트 체계가 넓습니다.
- lint/format이 메인에서 통과합니다.
- 핵심 unit, route, harness, UI behavior, UI a11y, UI visual 테스트가 모두 개별 통과했습니다.
- UI visual baseline 테스트까지 있는 점은 오픈소스 프로젝트 기준으로 좋은 편입니다.

### 문제점

- 기본 전체 pytest가 abort됩니다.
- abort는 assertion failure가 아니라 Python 프로세스 레벨 crash입니다.
- 전체 실행 중간에 실패 표시가 나온 뒤 summary 없이 abort되어, 전체 실패 목록을 안정적으로 수집하기 어렵습니다.
- native dependency 테스트를 일반 unit test 흐름에 섞어 실행하는 구조는 취약합니다.

### 평가

테스트 투자는 높은 편이고, 메인 브랜치의 개별 gate는 상당히 건강합니다. 하지만 전체 스위트가 한 번에 안정적으로 끝나지 않으면 CI 신뢰도는 제한됩니다. 이 프로젝트의 다음 품질 목표는 테스트 추가가 아니라 **테스트 격리와 실행 프로파일 정리**입니다.

### 권장 QA 게이트

릴리스 전 필수 gate:

1. `ruff check .`
2. `ruff format --check .`
3. 핵심 unit tests
4. route smoke tests
5. UI behavior/a11y/visual tests
6. native model integration tests는 별도 job/subprocess에서 실행

## 11. 보안/프라이버시 평가

### 점수: 7.2 / 10

### 좋은 점

- 제품 철학이 local-first입니다.
- 회의 오디오와 전사 결과를 외부 API에 보내지 않는 구조를 지향합니다.
- 파일 경로 검증과 upload/batch 처리 경계를 고려합니다.
- localhost 기반 desktop companion app 모델은 프라이버시 측면에서 설득력이 있습니다.

### 주의할 점

- 로컬 서버 인증 경계가 명확하지 않으면 같은 머신 또는 네트워크 설정 실수에 취약할 수 있습니다.
- 에러 detail, 로컬 path, config 값 노출 정책을 production mode에서 더 보수적으로 가져갈 필요가 있습니다.
- Hugging Face token, model download, external repository trust boundary를 문서화해야 합니다.

### 개선 제안

1. bind address와 CORS 기본값을 문서와 테스트로 고정합니다.
2. production mode의 error detail을 축약합니다.
3. local auth 또는 one-time pairing token을 장기적으로 검토합니다.
4. 모델 다운로드 출처와 토큰 권한 범위를 README에 명확히 설명합니다.

## 12. 문서화/커뮤니티 평가

### 점수: 8.0 / 10

### 좋은 점

- README, CLAUDE, design 문서, audit 문서, performance/security 문서가 풍부합니다.
- bulk actions처럼 기능별 design decision과 review 문서가 남아 있습니다.
- contributor가 프로젝트의 의도와 구조를 파악하기 쉽습니다.
- 한국어 문서가 잘 되어 있어 타깃 사용자와 개발자에게 친화적입니다.

### 아쉬운 점

- 문서가 많아진 만큼 최신 상태를 추적하기 어렵습니다.
- 오래된 audit과 최신 main 상태가 어긋날 수 있습니다.
- PASS/FAIL 상태가 문서마다 다르면 maintainer 판단이 흐려질 수 있습니다.

### 개선 제안

1. `docs/STATUS.md`를 만들어 현재 main의 검증 상태를 한곳에 둡니다.
2. 각 review 문서에 기준 commit과 실행 명령을 명시합니다.
3. 오래된 audit 문서 상단에 superseded notice를 추가합니다.
4. README에 stable branch와 active development branch의 차이를 명확히 씁니다.

## 13. 메인 브랜치의 강점 TOP 5

| 순위 | 강점 | 설명 |
|---:|---|---|
| 1 | end-to-end 제품 골격 | 녹음, 감시, 큐, 전사, 요약, 검색, UI가 이어짐 |
| 2 | local-first AI 방향성 | 프라이버시와 Apple Silicon 성능을 동시에 겨냥 |
| 3 | 테스트 하네스 수준 | behavior/a11y/visual까지 갖춘 UI 품질 체계 |
| 4 | 문서화 | 기능/설계/보안/성능 관련 문서가 풍부함 |
| 5 | 메인 품질 게이트 개선 | lint, format, route, UI gate가 통과 |

## 14. 메인 브랜치의 리스크 TOP 5

| 순위 | 리스크 | 심각도 | 이유 |
|---:|---|---|---|
| 1 | 전체 pytest abort | 높음 | CI와 릴리스 신뢰도를 직접 훼손 |
| 2 | 핵심 파일 비대화 | 높음 | 장기 유지보수와 review 비용 증가 |
| 3 | native dependency 격리 부족 | 높음 | MLX/Metal 환경 차이로 테스트/실행 불안정 가능 |
| 4 | onboarding 난도 | 중간 | Apple Silicon, HF token, 모델 다운로드, 오디오 장치 조건이 많음 |
| 5 | 문서 최신성 관리 | 중간 | 문서가 많아질수록 실제 main 상태와 어긋날 수 있음 |

## 15. 권장 작업 순서

### 1단계: 전체 pytest abort 제거

가장 먼저 해결해야 합니다.

- `core/model_manager.py`의 `_clear_gpu_cache()`가 unit test에서 `mlx.core`를 import하지 않게 합니다.
- preflight 결과를 테스트에서 명시적으로 mock/inject할 수 있게 합니다.
- native cleanup은 별도 integration marker로 분리합니다.
- abort 재현 테스트는 subprocess로 격리합니다.

### 2단계: 테스트 프로파일 정리

- `unit`: native dependency 없음
- `api`: watcher/recorder/model disabled
- `ui`: static fixture server 중심
- `integration`: 실제 MLX/Metal/model 사용 가능

### 3단계: 모듈 분리

- `api/routes.py` domain router 분리
- `ui/web/spa.js` feature module 분리
- `ui/web/style.css` component CSS 분리
- `core/pipeline.py` 단계별 interface 명확화

### 4단계: 오픈소스 onboarding 정리

- `doctor` 명령 또는 setup check 강화
- Apple Silicon/Intel/CI 환경 차이 명시
- HF token 권한과 모델 다운로드 절차 명확화
- 최소 기능 실행 경로와 full AI 경로를 분리 설명

## 16. 이전 작업 브랜치 평가와의 차이

| 항목 | 이전 평가 | 메인 기준 재평가 |
|---|---|---|
| lint/format | 실패 | 통과 |
| batch route 테스트 | crash 또는 실패 관찰 | `25 passed` |
| 주요 route 테스트 | crash 관찰 | `101 passed` |
| UI behavior | 실패 존재 | `29 passed` |
| UI a11y | 실패 존재 | `10 passed` |
| UI visual | 실패 존재 | 단독 실행 `6 passed` |
| 전체 pytest | 미확정/부분 검증 | abort 발생 |
| 종합 점수 | 약 7.0 | **7.6** |

메인 브랜치는 bulk actions 관련 회귀가 정리되어 있고, 기본 품질 게이트도 더 좋습니다. 다만 전체 pytest abort가 남아 있어 "릴리스 안정"보다는 "강한 베타 브랜치"로 보는 것이 타당합니다.

## 17. 최종 평가

`main` 브랜치는 현재 기준으로 **기능 범위, 문서화, UI 테스트 체계, route 안정성**이 꽤 좋은 프로젝트입니다. 오픈소스 프로젝트로서 매력도도 높습니다. 특히 로컬 한국어 회의 전사/요약/RAG라는 명확한 문제를 end-to-end로 풀고 있다는 점은 강합니다.

하지만 릴리스 품질을 말하려면 전체 테스트 스위트가 안정적으로 종료되어야 합니다. 지금 가장 중요한 문제는 새 기능이 아니라 **native AI runtime과 테스트 환경의 격리**입니다.

최종적으로는 다음과 같이 평가합니다.

> **기능 잠재력: 높음**  
> **메인 브랜치 품질: 보통 이상**  
> **UI bulk actions 품질: 현재 테스트 기준 양호**  
> **테스트 인프라 방향성: 좋음**  
> **릴리스 안정성: 전체 pytest abort 때문에 아직 제한적**  
> **권장 다음 작업: MLX/native cleanup 테스트 격리와 monolith 분해**

