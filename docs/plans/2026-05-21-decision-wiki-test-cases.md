# Decision Wiki 테스트 케이스 설계

작성일: 2026-05-21

목표는 Decision Wiki MVP가 "전사된 회의 내용을 검증 가능한 의사결정 위키로 만들고, 사용자가 언제든 의사결정사항을 쉽게 찾게 한다"는 제품 목적을 안정적으로 만족하는지 검증하는 것이다. 범용 Wiki 확장 테스트가 아니라 `decisions/`, verified citation, BM25 검색, durable backfill, Decision Chat 비회귀에 집중한다.

---

## 1. 주요 기능 맵

| 기능 | 코드 경계 | 현재 테스트 | 주요 리스크 |
|---|---|---|---|
| 자동 Decision 생성 | `core/pipeline.py`, `steps/wiki_compiler.py`, `core/wiki/compiler.py` | `test_pipeline_integration.py`, `test_decision_wiki_mvp.py` | summary/utterances 누락, 회의 날짜 오판, dry_run=false인데 파일 미생성 |
| DecisionRecord 정규화 | `core/wiki/decision_record.py`, `core/wiki/extractors/decision.py` | `test_decision_wiki_mvp.py` | frontmatter 누락, citation 없는 결정 저장, 반복 decision 중복 폭증 |
| Guard/pending/rejected | `core/wiki/compiler.py`, `core/wiki/guard.py` | 기존 guard 테스트 일부 | low confidence가 `status: decided`로 보임, rejected가 디스크에 남음 |
| Wiki BM25/FTS5 검색 | `core/wiki/search_index.py`, `/api/wiki/search` | `test_routes_phase2.py::TestWikiSearchEndpoint`, `test_decision_wiki_mvp.py` | 한국어 어미/동의어 recall 부족, filter 오작동, stale index |
| Decision UI | `ui/web/wiki-view.js`, `ui/web/wiki.css` | 현재 없음 | 필터 query param 누락, pending 표시 불가, citation 이동 깨짐 |
| Durable backfill | `core/wiki/backfill_state.py`, `/api/wiki/backfill*` | 기존 `test_routes_backfill.py`는 in-memory 중심 | 재시작 후 상태 유실, cancel/resume/retry failed 오작동 |
| Decision Chat | `core/wiki/chat_integration.py`, `api/routers/search_chat.py` | `test_chat_integration.py`, `test_decision_wiki_mvp.py` | 관련 없는 page 사용, citation 누락 LLM 답변 노출, router disabled 회귀 |
| 기존 RAG 비회귀 | `search/*`, `/api/search`, `/api/chat` | `test_rag_unchanged.py` | Wiki import/상태가 기존 검색과 채팅에 영향 |

---

## 2. 테스트 전략

1. **Unit**
   - `DecisionRecord`, `WikiSearchIndex`, `WikiBackfillStateStore`처럼 I/O 경계가 작고 deterministic한 모듈을 직접 검증한다.
2. **Integration**
   - `DecisionExtractor.render_pages()` → `WikiGuard` → `WikiStore` → `WikiSearchIndex`까지 실제 markdown/SQLite를 사용한다.
3. **API**
   - FastAPI `TestClient`로 `/api/wiki/search`, `/api/wiki/backfill*`, `/api/chat` router-disabled path를 검증한다.
4. **UI Contract**
   - JS DOM 단위 또는 Playwright로 `/app/wiki`, `/app/settings/wiki-backfill`의 query param, 상세 렌더, citation navigation을 검증한다.
5. **Eval Fixture**
   - 3~5개 gold meeting fixture를 두고 recall@k, accepted citation validity, pending/rejected 분류를 측정한다.

---

## 3. TC 목록

### A. 자동 Decision 생성

| ID | 우선순위 | TC | Given | When | Then |
|---|---:|---|---|---|---|
| DW-A01 | P0 | pipeline이 summary/utterances/date를 WikiCompiler에 전달 | summary markdown, corrected utterances, audio mtime/metadata 존재 | pipeline 완료 | `WikiCompiler.run(summary, utterances, meeting_date)` 호출 |
| DW-A02 | P0 | `wiki.enabled=true`, `dry_run=false`일 때 decision 파일 생성 | verified citation 포함 decision 추출 mock | wiki compile 실행 | `decisions/*.md` 생성, dry-run 로그만 남지 않음 |
| DW-A03 | P0 | citation 없는 decision canonical 저장 금지 | LLM decision에 citation 없음 | render_pages 실행 | 결과 page 0건 또는 rejected/pending 분리 |
| DW-A04 | P1 | 회의 날짜가 `date.today()`에 의존하지 않음 | 오늘과 다른 audio/job 날짜 | pipeline/wiki compile | frontmatter `decision_date`가 실제 회의 날짜 |

### B. DecisionRecord / Dedupe

| ID | 우선순위 | TC | Given | When | Then |
|---|---:|---|---|---|---|
| DW-B01 | P0 | 필수 frontmatter 안정성 | ExtractedDecision fixture | `DecisionRecord.to_markdown()` | `id,title,status,decision_date,project,participants,owners,confidence,source_meetings,supersedes,superseded_by,last_updated` 존재 |
| DW-B02 | P0 | citation 형식 엄격성 | `meeting_id`가 8자리 hex가 아닌 citation | DecisionRecord 생성/guard 검증 | canonical accepted로 저장되지 않음 |
| DW-B03 | P0 | 동일 slug decision dedupe | 기존 `decisions/2026-05-01-same.md` 존재 | 다른 날짜 같은 slug render | 기존 파일 경로 재사용, `source_meetings` 누적 |
| DW-B04 | P1 | owner/follow-up citation 보존 | follow-up action에 citation 존재 | markdown 렌더 | 후속 액션과 근거 섹션 모두 citation 포함 |
| DW-B05 | P1 | 기존 `created_at` 보존 | 기존 decision page 존재 | 업데이트 렌더 | `created_at` 유지, `last_updated` 갱신 |

### C. Guard / Pending / Rejected

| ID | 우선순위 | TC | Given | When | Then |
|---|---:|---|---|---|---|
| DW-C01 | P0 | low-confidence는 pending 저장 | `confidence < threshold` verdict | dispatch | `pending/decisions/*.md`, frontmatter `status: pending` |
| DW-C02 | P0 | phantom citation은 디스크 미저장 | 없는 timestamp/meeting citation | guard verify | `pages_rejected` 기록, canonical 파일 없음 |
| DW-C03 | P1 | pending도 UI 목록에 보임 | pending file 존재 | `/api/wiki/pages` | `type: pending` 반환 |
| DW-C04 | P1 | malformed confidence 처리 | `confidence: unknown` | guard/index | rejected 또는 confidence 0으로 안전 처리 |

### D. BM25 / FTS5 Search

| ID | 우선순위 | TC | Given | When | Then |
|---|---:|---|---|---|---|
| DW-D01 | P0 | rebuild/upsert/delete API | 2개 decision page | rebuild/upsert/delete | 검색 결과가 DB 상태와 일치 |
| DW-D02 | P0 | recall@1 gold decision | gold query `Q3 출시일 결정` | search top_k=1 | 기대 decision path 반환 |
| DW-D03 | P0 | filters 조합 | status/project/person/date/confidence fixture | `/api/wiki/search` | 조건에 맞는 decision만 반환 |
| DW-D04 | P1 | 한국어 어미 prefix recall | 본문 `키워드매직이` | query `키워드매직` | 결과 반환, snippet 포함 |
| DW-D05 | P1 | stale index 방지 | page 수정 후 API search | `/api/wiki/search` | rebuild되어 최신 title/body 반영 |
| DW-D06 | P2 | 깨진 page skip | invalid frontmatter/page read error | rebuild | 전체 검색 실패 없이 warning skip |

### E. Decision UI

| ID | 우선순위 | TC | Given | When | Then |
|---|---:|---|---|---|---|
| DW-E01 | P0 | 필터 query param 생성 | `/app/wiki` 로드 | 상태/프로젝트/사람/날짜/confidence 입력 | `/api/wiki/search`에 `status,project,person,date_from,date_to,min_confidence,page_type=decision` 전달 |
| DW-E02 | P0 | citation 클릭 이동 | decision 상세 citation 렌더 | citation 클릭 | `/app/viewer/{meeting_id}?t={seconds}`로 이동 |
| DW-E03 | P1 | pending 카테고리 표시 | pending page API 응답 | Wiki tree 렌더 | `검토 필요` 카테고리에 표시 |
| DW-E04 | P1 | 상세 frontmatter 표시 | decision detail API 응답 | page load | status/confidence/source_meetings/owners 표시 |
| DW-E05 | P2 | 필터 초기화 | 검색어/필터 존재 | ESC 또는 필터 제거 | 기본 tree 목록 복귀 |

### F. Durable Backfill

| ID | 우선순위 | TC | Given | When | Then |
|---|---:|---|---|---|---|
| DW-F01 | P0 | job 상태 SQLite 저장 | backfill start | progress/completion | DB에 status/processed/total/errors 보존 |
| DW-F02 | P0 | 서버 재시작 후 interrupted 복구 | DB에 running + finished_at null | 새 store로 get_job | `status: interrupted` |
| DW-F03 | P0 | retry failed meetings | failed errors 2건 | `/retry-failed` | 새 job이 실패 meeting_ids만 전달 |
| DW-F04 | P0 | resume original request | interrupted job request 저장 | `/resume` | 원래 since/until/meeting_ids/dry_run으로 새 job 시작 |
| DW-F05 | P1 | cancel durable 반영 | running job | `/cancel` | cancel_event set, DB status cancelled |
| DW-F06 | P1 | Settings UI 백필 패널 | `/app/settings/wiki-backfill` | start/cancel/resume/retry 클릭 | 대응 API 호출 및 progress 표시 |

### G. Decision Chat

| ID | 우선순위 | TC | Given | When | Then |
|---|---:|---|---|---|---|
| DW-G01 | P0 | first-3-pages 정책 제거 검증 | irrelevant 3 pages + relevant 4th page | Wiki query | BM25 relevant page만 source |
| DW-G02 | P0 | citation 누락 LLM fallback | LLM이 citation 없는 답변 반환 | respond | 검색 결과 기반 fallback, citation 포함 |
| DW-G03 | P0 | router disabled 비회귀 | `router=None` | `/api/chat` 또는 service respond | 기존 RAG 응답 identity 유지 |
| DW-G04 | P1 | BOTH 분기 source 분리 | router BOTH | respond | `rag_response`, `wiki_answer`, `wiki_sources` 모두 독립 |
| DW-G05 | P1 | wiki search 실패 시 RAG fallback | index/search exception | respond | `source_type=rag`, error_message 기록 |

### H. 기존 기능 비회귀

| ID | 우선순위 | TC | Given | When | Then |
|---|---:|---|---|---|---|
| DW-H01 | P0 | `search/*`가 `core.wiki` import 금지 | source tree | static scan | 참조 없음 |
| DW-H02 | P0 | wiki disabled search/chat 동일 | wiki on/off config | `/api/search`, `/api/chat` | byte-equivalent response |
| DW-H03 | P1 | `/api/wiki/search` wiki disabled | wiki files 존재, config disabled | search | 200 + 빈 결과 |
| DW-H04 | P1 | `api.routes` re-export 호환 | legacy import | import api.routes | AttributeError 없음 |

---

## 4. 우선 구현 순서

1. **P0 Unit/API부터 추가**
   - `tests/wiki/test_decision_record.py`
   - `tests/wiki/test_search_index.py`
   - `tests/wiki/test_backfill_state.py`
   - `tests/wiki/test_routes_backfill.py` durable/resume/retry 확장
2. **Decision Chat 품질 회귀 추가**
   - irrelevant first-3 pages + relevant BM25 page TC
   - LLM citation-drop fallback TC 확장
3. **UI Contract**
   - 기존 UI integration test harness에 `/app/wiki` 필터 API param 검사 추가
   - citation click route 검사 추가
4. **Gold fixture 확장**
   - 현재 1건에서 최소 5건으로 확장
   - accepted/pending/rejected를 섞고 recall@3, phantom citation 0건 검증

---

## 5. 권장 실행 명령

좁은 단위 검증:

```bash
uv run --with pytest --with pytest-asyncio pytest \
  tests/wiki/test_decision_wiki_mvp.py \
  tests/wiki/test_routes_phase2.py::TestWikiSearchEndpoint \
  -q
```

Decision Wiki 관련 회귀 묶음:

```bash
uv run --with pytest --with pytest-asyncio pytest \
  tests/wiki/test_decision_wiki_mvp.py \
  tests/wiki/test_routes_phase2.py::TestWikiSearchEndpoint \
  tests/wiki/test_routes_backfill.py \
  tests/wiki/test_chat_integration.py \
  tests/wiki/test_rag_unchanged.py \
  -q
```

정적/문법 검증:

```bash
python3 -m py_compile \
  api/routers/wiki.py core/wiki/backfill_state.py core/wiki/decision_record.py \
  core/wiki/search_index.py core/wiki/chat_integration.py core/wiki/compiler.py

node --check ui/web/wiki-view.js
node --check ui/web/settings-view.js
node --check ui/web/spa.js

uv run ruff check \
  api/routers/wiki.py core/wiki/backfill_state.py core/wiki/decision_record.py \
  core/wiki/search_index.py core/wiki/chat_integration.py core/wiki/compiler.py
```

---

## 6. 2026-05-21 수행 결과

구현/회귀 검증으로 아래 TC 묶음을 실행했다.

| 검증 | 결과 |
|---|---|
| Decision Wiki 전체 테스트 | `uv run --with pytest --with pytest-asyncio pytest tests/wiki -q` → 405 passed |
| UI 계약 테스트 | `uv run --with pytest --with pytest-playwright pytest -m ui tests/ui/integration/test_spa_overhaul_integration.py::test_wiki_route_renders_shell_tree_and_public_api tests/ui/integration/test_spa_overhaul_integration.py::test_wiki_search_detail_citation_and_unicode_slug_contract tests/ui/integration/test_spa_overhaul_integration.py::test_wiki_decision_filters_and_pending_category_contract tests/ui/integration/test_spa_overhaul_integration.py::test_settings_wiki_backfill_panel_calls_start_cancel_resume_retry -q` → 4 passed |
| Python 문법 검사 | `python3 -m py_compile tests/ui/integration/test_spa_overhaul_integration.py core/wiki/search_index.py tests/wiki/test_decision_record.py tests/wiki/test_search_index.py tests/wiki/test_backfill_state.py tests/wiki/test_decision_guard_dispatch.py tests/wiki/test_routes_backfill.py tests/wiki/test_decision_wiki_mvp.py` → passed |
| JS 문법 검사 | `node --check ui/web/wiki-view.js`, `node --check ui/web/settings-view.js`, `node --check ui/web/spa.js` → passed |
| Ruff | `uv run ruff check core/wiki/extractors/decision.py core/wiki/search_index.py tests/wiki/test_decision_record.py tests/wiki/test_search_index.py tests/wiki/test_backfill_state.py tests/wiki/test_decision_guard_dispatch.py tests/wiki/test_routes_backfill.py tests/wiki/test_decision_wiki_mvp.py tests/ui/integration/test_spa_overhaul_integration.py` → passed |
| 전체 비브라우저 회귀 | `uv run --extra dev pytest tests -q` → 2787 passed, 1 skipped, 165 deselected |
| Native preflight | `uv run --extra dev pytest -m native tests/native -q` → 1 passed |
| UI 전체 | `uv run --extra dev pytest -m ui tests/ui -q` → 150 passed |
| FastAPI/Playwright E2E | `uv run --extra dev pytest -m e2e tests/test_e2e_edit_playwright.py -q` → 14 passed |

잔여 리스크: 테스트 가능한 로컬 자동화 범위는 모두 통과했다. 실제 장시간 회의 오디오, 실제 STT/LLM 모델 다운로드, macOS 메뉴바/pywebview 수동 체감 테스트는 자동 TC 밖의 운영 검증으로 남는다.
