# Memorable Wiki — Continuation Goal (축소 코어판)

이 프롬프트는 Decision Wiki를 "기억 장부"에서 "살아있는 기억 시스템"으로
끌어올리는 작업을 단계 완료까지 자가 구동시킨다. 단, 1인·로컬·팬리스
(MacBook Air 16GB) 환경에 *실제로 맞는* 범위로만. 매 턴 저장소 상태를
점검하고 계속·중단·에스컬레이션을 결정한다.

**단일 진실 공급원**: `docs/plans/2026-06-03-memorable-wiki-system.md`
(충돌 시 계획서 우선). **근거 리서치(메모리)**: `wiki-as-agent-memory`

---

## 핵심 재프레임 (왜 축소했나)

리서치 4대 격차는 MemGPT·Hermes 처럼 메모리를 컨텍스트 창에 *통째로 주입*
하는 시스템의 처방이다(유한 컨텍스트 창 → 강제 망각). **우리 위키는 주입이
아니라 검색된다.** 로컬 디스크의 결정 페이지 수백 개는 부담이 아니다. 그래서:
- ① 망각/압축 → 진짜 니즈는 "압축"이 아니라 **항상 보이는 작은 현황 요약**(LLM 불필요).
- ② 다중신호 랭킹 → 유효·저렴. **코어**.
- ③ 계층화 → index/digest를 core로, 상세는 검색. 페이징 불필요.
- ④ 벡터 회상 → 소규모·정제된 결정문엔 BM25면 충분할 수 있음. **측정 후 결정**
  (단, 2026-06-07 사용자 지시로 G1은 게이트 오버라이드 활성 — 아래 기록).

원칙: **모델 로드를 유발하지 않는 코어부터.** 무거운 LLM 루프와 쿼리당 임베딩은
증거가 나올 때까지 미룬다(G1은 예외적으로 이미 활성).

## Baseline

- 최초: 2026-06-03, Branch `main`.
- 재검토: 2026-09-24, HEAD `75496c8` (#73); C1/C2/C3 + G1 code-verified around
  `0f6f8b9` (#72). 코드 기준 상태는 아래 "진행 현황" / Completed Phases.
- `core/wiki/*` 구현됨(store git원자커밋/schema/decision_record supersedes/
  search_index FTS5·BM25+C1 재랭킹/digest C2/lint D4/guard D1~D5/
  semantic_index·semantic_search G1).
- 위키 시맨틱 회상은 transcript RAG(`search/hybrid_search.py`)와 분리된
  `wiki.semantic` 컬렉션(`wiki_pages`)으로 동작.

## 불변식 (매 변경 검증)

1. 인용 무결성 최우선: 어떤 요약/재랭킹도 `[meeting:{id}@HH:MM:SS]` 손실 금지.
2. 원문 보존·점수만 조정: decay/recency는 검색 점수에만, 디스크 불변(git 아카이브).
3. 100% 로컬: 외부 메모리 SaaS·임베딩 API 금지. e5-small+ChromaDB+SQLite FTS5만.
4. 모델 로드 최소화: 코어(C1–C3)는 LLM/임베딩 로드 0. G1 경로만 임베딩 로드.
5. 기존 모듈 재사용: search_index/lint 확장, hybrid/semantic은 wiki 전용 복제/래퍼.
6. fail-loud·자동수정 금지(R1): lint/요약은 제안만, canon 자동 덮어쓰기 금지.
7. 단일 대형모델 적재(ModelLoadManager 뮤텍스), 피크 RAM≤9.5GB, 발열 정책
   (2건 후 3분 쿨다운) 존중.
8. 설정 하드코딩 금지: 가중치·반감기·임계는 config.yaml `wiki.*`.

## 범위 3단 분류

- ✅ **코어(C1–C3 완료; C4 미완)**: 다중신호 랭킹 · 현황 다이제스트 · UI —
  **모델 로드 0**. C4 골든셋 측정은 **현재 과제**.
- ✅/⏸ **게이트**: G1 벡터 회상 — **코드·설정 활성 완료**(오버라이드) +
  **정합성 부채**. C4 recall@5 비용대비 정량화는 deferred. G2 LLM consolidation
  — **미착수(기본 off)**.
- ⛔ **보류(무기한)**: D1 채팅 self-edit.

## 진행 현황 (코드 증거 기준)

| 단계 | 상태 | 증거 |
|---|---|---|
| C1 다중신호 랭킹 | ✅ 완료 | `core/wiki/search_index.py` `_rerank`/`_mmr_rerank`/`_recency_score`, `WikiRankingConfig`(config.py), `config.yaml wiki.ranking`, `tests/wiki/test_search_ranking.py` |
| C2 현황 다이제스트 | ✅ 완료 | `core/wiki/digest.py` `build_digest`/`render_digest_markdown`, `WikiCompiler._regenerate_digest`(ingest 후 `digest.md`), `GET /api/wiki/digest`(`api/routers/wiki.py`), `tests/wiki/test_digest.py` |
| C3 UI | ✅ 완료 | `ui/web/wiki-view.js` 현황/검색 ARIA 탭, digest 렌더, 결과 카드 score/status/citations, viewer deep link; `tests/ui/{behavior,a11y,visual}/test_wiki_overview.py` |
| C4 골든셋 | 🟡 부분 | `tests/wiki/test_g1_recall_eval.py`(native, 합성 8쌍 recall@5), `tests/wiki/fixtures/decision_wiki_gold.json`(소규모 MVP). 10~20 회의 골든셋·superseded 역전율·다이제스트 누락·candidate_pool 폭 측정 없음 |
| G1 벡터 회상 | ✅ 활성(오버라이드) + 부채 | `core/wiki/semantic_index.py`, `core/wiki/semantic_search.py`(`fuse_hybrid`/`wiki_hybrid_search`), `config.yaml wiki.semantic.enabled: true`, compiler/chat 배선. **결함: 벡터 전용 후보 필터 우회(아래)** |
| G2 LLM consolidation | ⏸ 미착수 | 코드·config 없음(계획서 §8 `consolidation` 블록도 config에 미존재) |
| D1 채팅 self-edit | ⛔ 보류 | — |

## Completed Phases (상세)

### C1 — 다중 신호 검색 랭킹 — DONE

- `config.WikiRankingConfig` + `config.yaml` `wiki.ranking`
  (`enabled`, `candidate_pool`, `w_bm25`/`w_recency`/`w_confidence`/`w_citation`,
  `superseded_penalty`, `recency_half_life_days`, `mmr_enabled`/`mmr_lambda`)
- `core/wiki/search_index.py`: `_rerank`, `_mmr_rerank`, `_recency_score`,
  `WikiSearchIndex.search` / `bm25_candidates`
- 단위 테스트: `tests/wiki/test_search_ranking.py` (recency·confidence·citation·
  superseded 구조적 floor·enabled escape·candidate_pool·MMR)

### C2 — 현황 다이제스트 — DONE

- `core/wiki/digest.py`: `OpenAction`/`RecentDecision`/`ProjectStatus`/`WikiDigest`,
  `parse_open_actions`, `build_digest`, `render_digest_markdown`
- `config.WikiDigestConfig` + `config.yaml` `wiki.digest`
- `WikiCompiler._regenerate_digest` (`core/wiki/compiler.py`)
- API: `GET /api/wiki/digest` → `api/routers/wiki.py`
- 테스트: `tests/wiki/test_digest.py` (+ routes/UI overview)

### C3 — UI 현황/검색 메타 — DONE

- `ui/web/wiki-view.js`: 현황/검색 탭, `_loadDigest` → `/wiki/digest`,
  `_renderDigest`, 검색 카드 score/snippet/citations
- `ui/web/wiki.css`
- UI gates: `tests/ui/behavior|a11y|visual/test_wiki_overview.py`

### G1 — 벡터 시맨틱 회상 — CODE DONE (게이트 오버라이드, 정합성 부채)

- `core/wiki/semantic_index.py`, `core/wiki/semantic_search.py`
  (`fuse_hybrid`, `fuse_and_rerank`, `wiki_hybrid_search`)
- `config.yaml` `wiki.semantic.enabled: true`
- 배선: compiler 증분 색인, wiki/chat 라우터 하이브리드 경로
- 테스트: `tests/wiki/test_semantic_*.py`, `test_wiki_hybrid.py`,
  `test_g1_wiring.py`, `test_g1_concurrency.py`,
  `test_semantic_real_e5.py` (native), `test_g1_recall_eval.py` (native, 합성 —
  **회의 골든셋 아님**)

## Current Phase: C4-H — 하이브리드 정합성 핫픽스 + 골든셋

C1·C2·C3는 완료. G1은 켜져 있지만 C4 근거가 없고 정합성 결함이 있다.
다음 실제 단계는 **G1 정합성 수정 → C4 측정 인프라**다.
(G1 활성은 이미 오버라이드됨 → C4는 “켜기 여부”가 아니라 **이득·회귀·설정 발판** 측정.)

권장 순서:

1. **C4a (핫픽스, 선행 필수)**: `core/wiki/semantic_search.py` `fuse_hybrid` 에서
   벡터 전용 후보(`fetch_candidates`)에도 `page_types/status/project/participant/
   owner/person/date_from/date_to/min_confidence` 필터를 적용한다
   (`fetch_candidates` 에 필터 인자 추가 또는 후처리 필터). 회귀 테스트:
   `tests/wiki/test_wiki_hybrid.py` 에 "벡터 전용 superseded 페이지가 `status=decided`
   필터에서 제외된다" 추가.
2. **C4b**: `/api/wiki/search` 의 요청당 `index.rebuild(store)` 제거 — compiler의
   upsert 경로를 신뢰하거나 staleness 체크(mtime/페이지 수) 후 조건부 rebuild,
   동기 I/O는 `asyncio.to_thread` 로.
3. **C4c**: 골든셋 10~20 회의(결정/superseded 체인/액션 포함) 픽스처 +
   비-native 지표 테스트: superseded 역전율 0%, 다이제스트 미해결 액션 누락 0,
   BM25 recall@5, 쿼리당 매칭 폭 vs `candidate_pool`.
   (`test_g1_recall_eval._GOLDEN` 합성 8문장·`evals/quality_golden_cases.json`은
   wiki C4 대체 불가.)
4. **C4d**: native 하이브리드 recall@5 이득 + RSS 자동 단언(계획서 G1 TODO);
   (측정 후) `wiki.ranking.mmr_enabled` on/off 결정 — MMR이 superseded 구조적
   floor를 깨는 엣지를 테스트로 고정한 뒤 기본값 변경.
5. **C4e**: 결과를 계획서 §9와 이 파일에 기록 → G1 유지/기본 off 복귀, G2 착수
   여부 판정. digest 인용 표면(`ProjectStatus` citations /
   `render_digest_markdown` 프로젝트 섹션)은 제품 결정 후 스키마·렌더·UI 정렬.

완료 기준:

- 하이브리드 경로에서 모든 검색 필터가 BM25-only 경로와 동일하게 적용(테스트).
- `/api/wiki/search` 가 요청마다 전체 재색인하지 않음.
- 골든셋 지표가 수치로 기록되고 G1/G2 판정이 문서화됨.
- `pytest tests/wiki/ -q` 비회귀; native 골든/e5 테스트는 diagnostic gate.
- 코어 경로(C1–C3) 모델 로드 0 유지; G1만 임베딩.

## 보완 백로그 (C1~C3 후속, 작게)

- MMR(`mmr_enabled`) 켜면 superseded 구조적 floor가 깨질 수 있음 →
  `_mmr_rerank` 를 비-superseded/ superseded 파티션별로 적용.
- `ProjectStatus`/`DigestProjectItem` 에 인용 없음 → C2 "모든 줄 인용 보존" 미충족.
- `GET /api/wiki/digest` 동기 디스크 집계를 `asyncio.to_thread` 로.
- UI score 표시가 "0~1 가정"(`wiki-view.js` `wiki-result-card__score`) — 실제는
  0~Σw(기본 2.0), superseded 음수. 표시 정책 결정 필요.
- Memorable Wiki 코어 수락(계획서 §11) 체크리스트를 STATUS에 한 줄로 닫기
  (별도 docs PR; 이 goals 파일만의 범위 밖이면 STATUS 본문은 건드리지 않음).

## Continue When

- 변경이 단일 모듈(또는 직접 결합 쌍)으로 스코프되고 테스트 커버리지 명확.
- 불변식 8개 보존, **코어는 모델 로드 0** 유지.
- 로컬에서 네이티브 모델 다운로드·시크릿 없이 검증 가능(native 측정은 별도 마커).

## Stop Or Pause When

- 인용 무결성·100% 로컬·자동수정 금지·모델로드0(코어) 중 하나라도 위협.
- G2 착수 근거(C4 수치)가 아직 없음 → 중단.
- (G1) 위키 임베딩이 회의 처리 RAM/발열과 경합할 위험 → 배치/뮤텍스/disable 먼저.
- 공개 API(`/api/wiki/*`,`/api/chat`) 계약이 마이그레이션 계획 없이 변경.
- broad Wiki 재확장(topics 지식그래프 등)으로 범위 번짐.
- D1 채팅 self-edit 요청 → 보류 유지.

## 검증 게이트

- `pytest tests/wiki/ -q`·전체 `pytest tests/ -x -q` / `py_compile <변경파일>`.
- UI 변경 시 `tests/ui/{behavior,a11y,visual}/test_wiki_overview.py`.
- native: `pytest -m native tests/wiki/test_semantic_real_e5.py
  tests/wiki/test_g1_recall_eval.py -s` (CI required 아님 —
  `workflow_dispatch`/`schedule`).
- 비회귀: transcript RAG·`/api/chat`·`/api/wiki/search`·`/api/wiki/digest` 계약 유지.
- PR CI green 후 머지.

## 품질 목표

- superseded가 confirmed보다 상위에 오는 비율: 0% (C1 — BM25 경로 테스트 완료;
  하이브리드·MMR 경로 포함해 C4에서 재확인).
- 다이제스트 미해결 액션 누락: 0건 (C2 단위 테스트 완료; 골든셋 측정 대기).
- 인용 보존(요약/재랭킹 전후): 100% (프로젝트 현황 섹션은 C4e에서 정합).
- BM25 recall@5 / 하이브리드 recall@5: C4에서 골든셋 기준 기록 (G2 게이트 입력).

## 이후 (C4-H 이후)

- **G2** LLM consolidation — 아카이브가 검색/UX를 해친다는 C4/운영 증거가 있을 때만.
  `wiki.consolidation` 스키마는 계획서에만 있고 코드 미구현.
