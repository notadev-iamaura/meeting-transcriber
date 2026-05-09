# 프로젝트 상태

- 기준일: 2026-05-10
- 기준 브랜치: `main`
- 기준 커밋: `c497d34d1b41226cca5a824fbaf4ecb9b06119ba`
- 최근 정리 wave: #41 → #38 → #39 → #40 → #42 → #43 → #44 → #45 → #46 → #47 → #48 → #52 → #53 모두 main 반영

## 현재 판단

이번 정리 wave 이후 프로젝트는 이전 평가에서 지적된 가장 큰 구조적 리스크를
상당 부분 해소한 상태입니다.

- 기본 테스트 프로파일은 native/MLX/Metal 의존 테스트를 명시 marker로 분리합니다.
- API 테스트는 `api-test`/`unit-test` 런타임 프로파일로 데스크톱 부작용을 줄입니다.
- `api/routes.py`는 app-state dependency helper, meetings batch, STT models,
  wiki/reindex, settings/user-settings, search/chat, meeting detail, system,
  uploads, recording router
  분리를 완료했습니다.
- `ui/web/spa.js`는 route view와 global shell controller 대부분을 feature module로 위임합니다.
- CI는 lint, mypy 타입 검사, Python 3.11/3.12 테스트, Swift compile gate를 통과한 PR만 main에 반영했습니다.
- consensus harness 문서와 CLI/test support가 main에 포함되어 다음 phase를 같은 방식으로 반복할 수 있습니다.
- `.venv` 밖의 Python 캐시 산출물은 Git 추적 대상이 아니며, 현재 로컬 작업트리도 clean입니다.

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
- `api/routers/stt_models.py`로 STT 모델 관리 API를 분리했습니다.
- `api/routers/wiki.py`와 `api/routers/reindex.py`로 지식베이스/재색인 API를 분리했습니다.
- `api/routers/settings.py`와 `api/routers/user_settings.py`로 설정/프롬프트/용어집 API를 분리했습니다.
- `api/routers/search_chat.py`로 검색/RAG 채팅 API를 분리했습니다.
- `api/routers/meeting_detail.py`로 단일 회의 상세/전사/요약/오디오 API를 분리했습니다.
- `api/routers/system.py`, `api/routers/uploads.py`, `api/routers/recording.py`로
  시스템 상태/대시보드/업로드/녹음 API를 분리했습니다.
- `api/server.py`는 router 등록과 dependency wiring을 더 명확히 갖습니다.
- 관련 테스트는 `tests/test_api_dependencies.py`, `tests/test_server.py`,
  `tests/test_routes_meetings_batch.py`에 반영되어 있습니다.

### Runtime, CI, Docs

- model/pipeline runtime gate와 테스트 프로파일을 정리했습니다.
- CI는 기본 안정 gate, UI bulk actions gate, mypy 타입 검사 gate를 구분합니다.
- README, PR template, AGENTS.md, 평가 문서를 최신 정책에 맞췄습니다.
- `harness/*`와 `docs/agentic-ops/*`가 main에 포함되어 consensus 기반 작업 흐름을 지원합니다.

## 권장 검증 게이트

일반 변경:

```bash
ruff check .
ruff format --check .
mypy config.py api core steps search ui security --no-error-summary
pytest tests/ -v --tb=short
pytest -m harness -q
```

API/router 변경:

```bash
pytest tests/test_api_dependencies.py tests/test_server.py tests/test_routes_meetings_batch.py tests/test_routes_stt_models.py tests/test_routes_reindex.py tests/test_user_settings_api.py tests/test_user_settings_e2e.py tests/test_security_fixes.py -q
pytest tests/wiki/test_routes.py tests/wiki/test_routes_phase2.py tests/wiki/test_routes_backfill.py tests/wiki/test_rag_unchanged.py tests/wiki/test_routes_chat_router.py -q
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

1. `api/routes.py`에 남은 A/B test routes를 별도 router로 분리합니다.
2. 부채 마커를 작은 PR 단위로 줄입니다. 현재 관측 기준은 `type: ignore` 23건,
   `noqa` 137건, 빈 `pass` 37건, `TODO/FIXME/HACK/XXX` 2건,
   `pragma: no cover` 2건입니다. 우선순위는 내부 타입 예외, 좁힐 수 있는
   `BLE001`, 의도가 불명확한 빈 `pass`입니다.
3. `ui/web/style.css`를 component CSS로 나눕니다. 다음 후보는 viewer, settings,
   command palette, recording, layout shell입니다.
4. native marker 대상 테스트를 CI에서 required/manual/scheduled 중 어떤 방식으로
   운용할지 결정합니다.
5. `docs/plans/issue-b-transcription-coverage.md`의 STT 누락/환각 개선 계획을
   실험 harness와 메트릭 기반 phase로 전환합니다.
