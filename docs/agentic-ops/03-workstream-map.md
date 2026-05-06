# 03. 병렬·순차 Workstream Map

## 전체 목표

전문가 평가에서 도출된 주요 개선 축을 병렬로 추진하되, 충돌 위험이 큰 파일은 순차 wave로 묶는다.

## Workstream 목록

| ID | 이름 | 주요 파일 | 병렬 가능성 | 위험도 |
|---|---|---|---|---|
| WS0 | Agentic harness & skills setup | `docs/agentic-ops/*`, `harness/*`, `codex-skills/*` | 선행 필수 | 중 |
| WS1 | Backend service boundary | `api/routes.py`, `api/dependencies.py`, `api/services/*`, `api/routers/*` | WS2/WS3와 병렬 가능 | 높음 |
| WS2 | Frontend modularization | `ui/web/spa.js`, `ui/web/*.js`, `ui/web/*.css` | WS1/WS3와 병렬 가능 | 높음 |
| WS3 | Pipeline & performance metrics | `core/pipeline.py`, `steps/*`, `search/*`, `docs/PERFORMANCE_BACKLOG.md` | WS1/WS2와 병렬 가능 | 높음 |
| WS4 | Runtime/CI/release gates | `api/server.py`, `pyproject.toml`, `.github/workflows/ci.yml`, `tests/*` | 부분 병렬 | 중-높음 |
| WS5 | Docs/onboarding/status | `README.md`, `AGENTS.md`, `docs/STATUS.md`, `CONTRIBUTING.md` | 대부분 병렬 | 중 |
| WS6 | Observability/doctor/diagnostics | `main.py`, `scripts/*`, `core/preflight.py`, docs | WS1/WS3 이후 | 중 |

## 선행 의존성

```text
WS0
 ├─ WS1 Backend
 ├─ WS2 Frontend
 ├─ WS3 Pipeline/Perf
 ├─ WS5 Docs
 └─ WS4 Runtime/CI
      └─ WS6 Doctor/Diagnostics
```

WS0는 반드시 먼저 끝나야 한다. 이유는 이후 작업이 모두 ticket, role assignment, consensus, gate profile에 의존하기 때문이다.

## 병렬 실행 가능한 묶음

### 병렬 묶음 A: 낮은 충돌 기반 정리

동시에 진행 가능:

- WS1-A: `api/dependencies.py` 도입
- WS2-A: `ui/web/api-client.js` 설계
- WS3-A: checkpoint compact JSON 설계
- WS5-A: `docs/STATUS.md` canonical화

충돌 위험:

- 낮음. 주 파일이 분리되어 있다.

### 병렬 묶음 B: 기능별 모듈 분리

동시에 진행 가능:

- WS1-B: `settings_service.py` 추출
- WS2-B: `bulk-actions.js` 추출
- WS3-B: pipeline checkpoint/state 분리
- WS4-B: native marker/CI profile 정리

충돌 위험:

- 중간. tests와 docs는 충돌 가능성이 있다.

### 병렬 묶음 C: 성능 기반 개선

동시에 진행 가능:

- WS3-C: pipeline step metrics
- WS2-C: UI list render metrics
- WS1-C: dashboard stats TTL cache
- WS6-C: `doctor` command 설계

충돌 위험:

- 중간. metrics naming과 status docs를 PM-A가 조율해야 한다.

## 반드시 순차로 해야 할 작업

### `api/routes.py` 분해

권장 순서:

1. `api/dependencies.py`
2. `api/routers/uploads.py`
3. `api/services/upload_service.py`
4. `api/routers/settings.py`
5. `api/services/settings_service.py`
6. `api/routers/wiki.py`

이 파일은 여러 agent가 동시에 수정하면 충돌이 거의 확실하다. 한 wave에 하나의 route domain만 이동한다.

### `ui/web/spa.js` 분해

권장 순서:

1. `api-client.js`
2. `events.js`
3. `bulk-actions.js`
4. `recording-overlay.js`
5. `meetings-list.js`
6. `command-palette.js`

`bulk-actions.js`가 먼저인 이유는 현재 테스트 안전망이 가장 잘 갖춰져 있기 때문이다.

### `core/pipeline.py` 분해

권장 순서:

1. state/checkpoint IO 분리
2. resource guard 분리
3. step runner abstraction
4. recovery/resume 분리
5. metrics/event hooks 추가

pipeline은 제품 핵심이므로 성능 개선과 구조 개선을 한 PR에 섞지 않는다.

## 역할 배정 매트릭스

| Workstream | Producer | Reviewer | QA | Final |
|---|---|---|---|---|
| WS0 Harness/Skills | PM-A, Architect-A | PM-B, Architect-B | QA-B | PM-B |
| WS1 Backend | Backend-A | Backend-B, Architect-B | QA-A | PM-B |
| WS2 Frontend | Frontend-A | Frontend-B, Designer-B | QA-A, QA-B | PM-B |
| WS3 Pipeline/Perf | Pipeline-A, Perf-A | Pipeline-B, Perf-B | QA-B | PM-B |
| WS4 Runtime/CI | Backend-A, QA-A | Backend-B, PM-B | QA-B | PM-B |
| WS5 Docs | Docs-A | Docs-B, PM-B | QA-B | PM-B |
| WS6 Observability | Backend-A, Docs-A | Backend-B, Security-B | QA-B | PM-B |

각 역할은 최소 2명이다. 고위험 workstream은 reviewer를 2명 둔다.

## Workstream별 완료 게이트

### WS0

- agentic ops 문서 작성
- skill blueprint 작성
- harness extension 설계
- PM-B 승인

### WS1

- route handler가 얇아짐
- service unit test 또는 route test 추가
- `ruff check .`
- targeted API tests

### WS2

- feature module 분리
- UI behavior/a11y/visual targeted gate
- no visual drift unless documented

### WS3

- metric 또는 performance change가 문서화됨
- targeted pipeline/search tests
- benchmark smoke 또는 synthetic measurement

### WS4

- `pytest -q`
- CI workflow sanity
- native/ui/e2e marker policy 유지

### WS5

- README/AGENTS/STATUS 간 모순 제거
- 오래된 문서 superseded link
- setup path 최신화

### WS6

- doctor command 또는 diagnostics plan
- sensitive data redaction policy
- local environment checks

## 충돌 위험 파일

| 파일 | 정책 |
|---|---|
| `api/routes.py` | 한 wave에 한 agent만 write |
| `api/server.py` | runtime/CI owner만 write |
| `ui/web/spa.js` | feature owner 1명만 write |
| `ui/web/style.css` | CSS owner 1명만 write |
| `core/pipeline.py` | pipeline owner 1명만 write |
| `pyproject.toml` | release/QA owner 승인 필요 |
| `.github/workflows/ci.yml` | PM-B + QA-B 승인 필요 |
| `README.md`, `AGENTS.md` | Docs-A owner, PM-B final |

