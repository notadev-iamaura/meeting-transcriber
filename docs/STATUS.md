# 프로젝트 상태

- 기준일: 2026-05-06
- 기준 브랜치: `main`
- 기준 커밋: `8243dbfaafaf60620c271ba42094005def710276`
- 최근 정리 wave: #41 → #38 → #39 → #40 모두 main 반영

## 현재 판단

이번 정리 wave 이후 프로젝트는 이전 평가에서 지적된 가장 큰 구조적 리스크를
상당 부분 해소한 상태입니다.

- 기본 테스트 프로파일은 native/MLX/Metal 의존 테스트를 명시 marker로 분리합니다.
- API 테스트는 `api-test`/`unit-test` 런타임 프로파일로 데스크톱 부작용을 줄입니다.
- `api/routes.py`는 app-state dependency helper와 meetings batch router 분리를 완료했습니다.
- `ui/web/spa.js`는 route view와 global shell controller 대부분을 feature module로 위임합니다.
- CI는 lint, Python 3.11/3.12 테스트, Swift compile gate를 통과한 PR만 main에 반영했습니다.
- consensus harness 문서와 CLI/test support가 main에 포함되어 다음 phase를 같은 방식으로 반복할 수 있습니다.

## 완료된 주요 작업

### Frontend Architecture

`ui/web/spa.js`에서 다음 모듈을 분리했습니다.

- `api-client.js`
- `list-panel.js`
- `command-palette.js`
- `settings-view.js`
- `viewer-view.js`
- `chat-view.js`
- `wiki-view.js`
- `ab-test-view.js`
- `search-view.js`
- `empty-view.js`
- `global-resource-bar.js`
- `bulk-action-bar.js`
- `theme-controller.js`
- `mobile-drawer.js`
- `shortcut-controller.js`

각 모듈은 `window.Meeting*` factory boundary를 통해 `spa.js`에 주입되며,
기존 `window.SPA.*` 공개 계약은 유지합니다.

### Backend/API

- `api/dependencies.py`로 FastAPI `app.state` 접근을 모았습니다.
- `api/routers/meetings_batch.py`로 batch action router를 분리했습니다.
- `api/server.py`는 router 등록과 dependency wiring을 더 명확히 갖습니다.
- 관련 테스트는 `tests/test_api_dependencies.py`, `tests/test_server.py`,
  `tests/test_routes_meetings_batch.py`에 반영되어 있습니다.

### Runtime, CI, Docs

- model/pipeline runtime gate와 테스트 프로파일을 정리했습니다.
- CI는 기본 안정 gate와 UI bulk actions gate를 구분합니다.
- README, PR template, AGENTS.md, 평가 문서를 최신 정책에 맞췄습니다.
- `harness/*`와 `docs/agentic-ops/*`가 main에 포함되어 consensus 기반 작업 흐름을 지원합니다.

## 현재 Phase A

Phase A의 목적은 최신 main 상태와 문서/goal의 드리프트를 제거하고, 작은 UX 개선을
별도 검증 가능한 단위로 고정하는 것입니다.

이번 Phase A에서 다루는 UX 개선:

- 실패한 회의의 `재시도`는 기존 결과와 진행 기록을 유지하고 실패 지점부터 다시 처리한다는 의미로 노출합니다.
- `재전사`는 기존 전사문, 요약, 진행 기록을 삭제하고 오디오부터 새로 처리한다는 의미로 노출합니다.
- 두 액션은 버튼 class, title, aria-label, confirm copy로 구분합니다.

## 권장 검증 게이트

일반 변경:

```bash
ruff check .
ruff format --check .
pytest tests/ -v --tb=short
pytest -m harness -q
```

API/router 변경:

```bash
pytest tests/test_api_dependencies.py tests/test_server.py tests/test_routes_meetings_batch.py tests/test_routes_stt_models.py tests/test_routes_reindex.py -q
pytest tests/wiki/test_routes.py tests/wiki/test_routes_phase2.py tests/wiki/test_routes_backfill.py tests/wiki/test_rag_unchanged.py -q
pytest tests/test_routes.py -q
```

Frontend shell/view 변경:

```bash
node --check ui/web/spa.js
node --check ui/web/viewer-view.js
pytest tests/harness/test_frontend_boundaries.py -q
pytest -m ui tests/ui/integration/test_spa_overhaul_integration.py -q
```

Fixed-port bulk actions UI tests는 순차 실행합니다.

```bash
pytest -m ui tests/ui/behavior/test_bulk_actions_behavior.py -q
pytest -m ui tests/ui/a11y/test_bulk_actions_a11y.py -q
pytest -m ui tests/ui/visual/test_bulk_actions_visual.py -q
```

환경 의존 gate는 명시적으로 실행합니다.

```bash
pytest -m e2e tests/test_e2e_edit_playwright.py -v
pytest -m ui tests/ui/ -v
pytest -m native tests/ -v
```

## 알려진 우선 과제

1. `api/routes.py` domain router 분리를 계속합니다. STT models는 #43에서
   완료됐고, wiki/reindex는 Phase C에서 분리했습니다. 다음 후보는 settings,
   search/chat, meeting detail routes입니다.
2. `ui/web/style.css`를 component CSS로 나눕니다. 다음 후보는 viewer, settings,
   command palette, recording, layout shell입니다.
3. native marker 대상 테스트를 CI에서 required/manual/scheduled 중 어떤 방식으로
   운용할지 결정합니다.
4. `docs/plans/issue-b-transcription-coverage.md`의 STT 누락/환각 개선 계획을
   실험 harness와 메트릭 기반 phase로 전환합니다.
