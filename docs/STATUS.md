# 프로젝트 상태

- 기준일: 2026-04-30
- 기준 브랜치: `main`
- 기준 커밋: `4d7908353a34204c74042acbc65021c1742b15dc`
- 상세 평가: [`docs/PROJECT_EVALUATION_MAIN_2026-04-30.md`](PROJECT_EVALUATION_MAIN_2026-04-30.md)

## 현재 판단

현재 작업트리는 평가 문서에서 가장 큰 리스크였던 전체 pytest abort를 제거한 상태입니다. 기본 테스트 프로파일은 native MLX/Metal cleanup을 비활성화하고, native 런타임 검증은 명시 marker로 분리합니다. `api-test`/`unit-test` 런타임 프로파일도 추가되어 API 테스트가 recorder, watcher, pipeline processor 같은 데스크톱 부작용 없이 실행될 수 있습니다.

2026-05-03 추가 개선으로 실제 제품 코드에도 Wave 1 경계 정리 작업을 반영했습니다. API 라우트의 `app.state` 의존성 접근은 `api/dependencies.py`로 모았고, 웹 UI의 fetch/error 처리 경계는 `ui/web/api-client.js`로 분리했습니다. 파이프라인 상태 저장은 기본 동작을 유지하면서 `pipeline.checkpoint_json_indent: null` 설정 시 compact JSON으로 저장할 수 있게 했습니다.

Wave 2 첫 단계로 `spa.js`의 명령 팔레트 모듈을 `ui/web/command-palette.js`로 분리했습니다. `spa.js`는 라우터와 전역 단축키 연결만 유지하고, 팔레트 DOM 생성·검색·최근 액션 저장·동적 항목 로딩은 새 모듈의 `window.MeetingCommandPalette` factory가 담당합니다.

이어서 `spa.js`의 회의 목록 패널을 `ui/web/list-panel.js`로 분리했습니다. `ListPanel`의 공개 API(`init`, `loadMeetings`, `setActiveFromPath`, `clearSelection`, `getSelectedIds` 등)는 유지하고, `spa.js`는 `window.MeetingListPanel` factory를 통해 라우터와 전역 상태만 주입합니다. 회의 선택과 bulk actions 동작은 Playwright behavior 테스트로 재검증했습니다.

다음으로 설정 화면을 `ui/web/settings-view.js`로 분리했습니다. 설정 셸, 검색 인덱스 백필 패널, 일반 설정, 프롬프트 편집, 용어집 CRUD 패널을 새 factory(`window.MeetingSettingsView`)로 옮기고, A/B 테스트 뷰는 기존 `spa.js`에 남겨 라우트 영향 범위를 줄였습니다. `/app/settings/prompts` 라우트 렌더링 통합 테스트를 추가해 모듈 로드와 탭 활성화를 검증합니다.

회의록 뷰어도 `ui/web/viewer-view.js`로 분리했습니다. `ViewerView`의 기존 생성자 계약(`new ViewerView(meetingId)`)은 유지하고, `spa.js`는 `window.MeetingViewerView` factory에 `App`, `Router`, `ListPanel`, `Icons`, `PIPELINE_STEPS`, `errorBanner`를 주입합니다. `/app/viewer/{id}` 라우트의 스켈레톤/탭 마크업은 SPA 통합 테스트로 재검증했습니다.

채팅 화면도 `ui/web/chat-view.js`로 분리했습니다. `ChatView`의 기존 생성자 계약(`new ChatView()`)과 `window.SPA.ChatView` 공개 API는 유지하고, `spa.js`는 `window.MeetingChatView` factory에 `App`, `Router`, `Icons`, `errorBanner`를 주입합니다. 위원회식 교차 검토에서 지적된 `/api/chat` payload, 세션 유지/초기화, 회의 필터, 모듈 로드 순서 계약은 SPA 통합 테스트로 고정했습니다. 또한 `api-client.js`가 `AbortError`를 네트워크 오류로 감싸지 않도록 보존해 채팅 취소 흐름의 기존 의도를 실제 동작과 맞췄습니다.

위키 화면도 `ui/web/wiki-view.js`로 분리했습니다. `WikiView`의 기존 생성자 계약(`new WikiView()`)과 `window.SPA.WikiView` 공개 API는 유지하고, `spa.js`는 `window.MeetingWikiView` factory에 `App`, `Router`를 주입합니다. 위원회 검토에서 P1로 지적된 destroy 이후 async DOM write, `CSS.escape` 직접 의존, 한국어 nested slug 인코딩, health modal cleanup은 구현과 테스트에 함께 반영했습니다. 또한 루프 지속 판단 기준을 `goals/continuation.md`로 추가했습니다.

A/B 테스트 화면도 `ui/web/ab-test-view.js`로 분리했습니다. 목록/생성/결과 세 뷰는 하나의 feature module factory(`window.MeetingAbTestView`)로 묶고, `spa.js`는 route-local 생성자만 주입받습니다. 위원회 검토에서 지적된 markdown summary 응답 처리, result polling/WebSocket cleanup, submit payload, stale async guard는 구현과 SPA 통합 테스트에 함께 반영했습니다.

검색 화면도 `ui/web/search-view.js`로 분리했습니다. `SearchView`의 기존 생성자 계약(`new SearchView()`)과 `window.SPA.SearchView` 공개 API는 유지하고, `spa.js`는 `window.MeetingSearchView` factory에 `App`, `Router`, `Icons`, `errorBanner`를 주입합니다. 검색 제출 payload, 503 배너, 결과 딥링크, XSS-safe 렌더링, 최신 요청만 렌더링하는 `_searchSeq` guard를 테스트로 고정했습니다. 이 단계 후 `ui/web/spa.js`는 1401줄입니다.

홈/빈 화면도 `ui/web/empty-view.js`로 분리했습니다. `EmptyView`의 기존 생성자 계약(`new EmptyView()`)과 `window.SPA.EmptyView` 공개 API는 유지하고, `spa.js`는 `window.MeetingEmptyView` factory에 `App`, `Router`, `Icons`, `_showBulkToast`를 주입합니다. 홈 통계, 폴더 열기, import modal, 전체/최근 24시간 드롭다운 payload, dashboard refresh cleanup, destroy 이후 stale async guard를 테스트로 고정했습니다. 이 단계 후 `ui/web/spa.js`는 977줄이며, 큰 route-specific view 생성자는 별도 모듈로 분리된 상태입니다.

다음 Phase 첫 단계로 전역 리소스 바를 `ui/web/global-resource-bar.js`로 분리했습니다. `spa.js`는 `window.MeetingGlobalResourceBar` factory bridge와 `GlobalResourceBar.start()` 호출만 유지합니다. 중복 start 시 DOM/interval singleton을 유지하고, `stop()` 이후 늦게 도착한 `/system/resources` 응답이 DOM을 갱신하지 않도록 `_refreshSeq`/`_stopped` guard를 추가했습니다.

다음 활성 목표는 `BulkActionBar` 분리입니다. 새 workstream은 `ui/web/spa.js`에 남은 전역 bulk selection/action 로직을 `window.MeetingBulkActionBar.create({ ... })` 형태의 독립 모듈로 옮기는 것을 목표로 합니다. 기존 `window.SPA.BulkActionBar`, 선택 카운트, clear selection, batch payload, dropdown, toast/status, a11y 계약은 유지해야 하며, fixed-port UI 테스트는 충돌을 피하기 위해 순차 실행합니다.

## 권장 검증 게이트

```bash
ruff check .
ruff format --check .
pytest tests/ -v --tb=short
pytest -m harness -q
pytest tests/test_routes_home_dashboard.py tests/test_routes.py tests/test_routes_meetings_batch.py -q
pytest -m ui tests/ui/behavior/test_bulk_actions_behavior.py -q
pytest -m ui tests/ui/a11y/test_bulk_actions_a11y.py -q
pytest -m ui tests/ui/visual/test_bulk_actions_visual.py -q
```

## 최근 검증 결과

```bash
ruff check .                         # All checks passed
ruff format --check .                # 247 files already formatted
pytest -q                            # 2624 passed, 140 deselected, 4 warnings
pytest -m ui tests/ui/behavior/test_bulk_actions_behavior.py \
  tests/ui/a11y/test_bulk_actions_a11y.py \
  tests/ui/visual/test_bulk_actions_visual.py -q
                                      # 45 passed

ruff check api ui core config.py tests/test_api_dependencies.py \
  tests/harness/test_frontend_boundaries.py tests/test_pipeline.py
                                      # All checks passed
ruff format --check api ui core config.py tests/test_api_dependencies.py \
  tests/harness/test_frontend_boundaries.py tests/test_pipeline.py
                                      # 58 files already formatted
pytest tests/test_api_dependencies.py tests/test_routes.py \
  tests/test_routes_meetings_batch.py tests/test_server.py \
  tests/harness/test_frontend_boundaries.py tests/test_pipeline.py \
  tests/test_config.py -q
                                      # 332 passed
node --check ui/web/command-palette.js
                                      # passed
node --check ui/web/spa.js
                                      # passed
pytest -m ui tests/ui/integration/test_spa_overhaul_integration.py -q
                                      # 17 passed
node --check ui/web/list-panel.js
                                      # passed
pytest -m ui tests/ui/behavior/test_bulk_actions_behavior.py -q
                                      # 29 passed
node --check ui/web/settings-view.js
                                      # passed
pytest -m ui tests/ui/integration/test_spa_overhaul_integration.py -q
                                      # 18 passed
node --check ui/web/viewer-view.js
                                      # passed
pytest tests/harness/test_frontend_boundaries.py -q
                                      # 7 passed
pytest -m harness -q
                                      # 125 passed
node --check ui/web/chat-view.js
                                      # passed
node --check ui/web/spa.js
                                      # passed
pytest tests/harness/test_frontend_boundaries.py -q
                                      # 9 passed
pytest -m ui tests/ui/integration/test_spa_overhaul_integration.py -q
                                      # 20 passed
pytest -m ui tests/ui/behavior/test_command_palette.py \
  tests/ui/a11y/test_command_palette.py -q
                                      # 8 passed
node --check ui/web/wiki-view.js
                                      # passed
node --check ui/web/spa.js
                                      # passed
pytest tests/harness/test_frontend_boundaries.py -q
                                      # 11 passed
pytest -m ui tests/ui/integration/test_spa_overhaul_integration.py -q
                                      # 23 passed
pytest tests/wiki/test_routes_phase2.py -q
                                      # 15 passed
pytest -m harness -q
                                      # 129 passed
pytest -m ui tests/ui/behavior/test_command_palette.py \
  tests/ui/a11y/test_command_palette.py -q
                                      # 8 passed
node --check ui/web/ab-test-view.js
                                      # passed
node --check ui/web/api-client.js
                                      # passed
pytest tests/test_ab_test_api.py tests/test_ab_test_runner.py -q
                                      # 57 passed
pytest tests/harness/test_frontend_boundaries.py -q
                                      # 14 passed
pytest -m ui tests/ui/integration/test_spa_overhaul_integration.py -q
                                      # 27 passed
pytest -m harness -q
                                      # 132 passed
node --check ui/web/search-view.js
                                      # passed
node --check ui/web/spa.js
                                      # passed
pytest tests/harness/test_frontend_boundaries.py -q
                                      # 16 passed
pytest -m ui tests/ui/integration/test_spa_overhaul_integration.py -q
                                      # 31 passed
pytest tests/test_routes.py::TestSearchEndpoint \
  tests/test_chat.py::TestPhase3APISearchIntegration -q
                                      # 8 passed
pytest -m harness -q
                                      # 134 passed
ruff check .
                                      # All checks passed
ruff format --check .
                                      # 258 files already formatted
node --check ui/web/empty-view.js
                                      # passed
node --check ui/web/spa.js
                                      # passed
pytest tests/harness/test_frontend_boundaries.py -q
                                      # 18 passed
pytest -m ui tests/ui/integration/test_spa_overhaul_integration.py -q
                                      # 33 passed
pytest -m ui tests/ui/behavior/test_bulk_actions_behavior.py -q
                                      # 29 passed
pytest -m ui tests/ui/a11y/test_bulk_actions_a11y.py -q
                                      # 10 passed
pytest tests/test_routes_home_dashboard.py tests/test_routes_meetings_batch.py -q
                                      # 40 passed
pytest -m harness -q
                                      # 136 passed
node --check ui/web/global-resource-bar.js
                                      # passed
node --check ui/web/spa.js
                                      # passed
pytest tests/harness/test_frontend_boundaries.py -q
                                      # 20 passed
pytest -m ui tests/ui/integration/test_spa_overhaul_integration.py -q
                                      # 35 passed
pytest tests/test_routes.py::TestSystemResourcesEndpoint -q
                                      # 3 passed
pytest -m harness -q
                                      # 138 passed
ruff check .
                                      # All checks passed
ruff format --check .
                                      # 258 files already formatted
```

## 기본 실행에서 제외하는 게이트

다음 게이트는 환경 의존성이 크므로 명시적으로 실행합니다.

```bash
pytest -m e2e tests/test_e2e_edit_playwright.py -v
pytest -m ui tests/ui/ -v
pytest -m native tests/ -v
```

## 알려진 우선 과제

1. `api/routes.py`는 batch router와 dependency helper 분리를 시작점으로 domain router 분리를 계속합니다.
2. `ui/web/style.css`는 `tokens.css`/`bulk-actions.css` 이후 나머지 컴포넌트를 점진 분리합니다.
3. `ui/web/spa.js`는 `command-palette.js`, `list-panel.js`, `settings-view.js`, `viewer-view.js`, `chat-view.js`, `wiki-view.js`, `ab-test-view.js`, `search-view.js`, `empty-view.js` 분리를 완료했습니다.
4. 전역 shell 코드 중 `GlobalResourceBar` 분리는 완료했습니다. 다음 활성 후보는 `BulkActionBar`이며, selection/batch/a11y/visual 계약을 유지하는 별도 위원회 합의 후 진행합니다.
5. `ui/web/app.js`는 `api-client.js` 위임을 시작점으로 비즈니스 로직과 뷰 로직을 더 분리합니다.
6. native marker 대상 테스트를 별도 CI job으로 분리할지 검토합니다.
7. 문서의 테스트 명령은 이 파일을 기준으로 최신 상태를 유지합니다.
