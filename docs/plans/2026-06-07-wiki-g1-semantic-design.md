# G1 — 위키 시맨틱 회상 (하이브리드 벡터+BM25) 설계

> 상태: 설계 확정 (2026-06-07). 단일 진실 공급원 `docs/plans/2026-06-03-memorable-wiki-system.md` 의 게이트 항목 **G1** 을 사용자 지시로 활성화(게이트 오버라이드). C1(다중신호 랭킹) 위에 스택.

## 0. 한 문장

위키 검색을 BM25 단일에서 **벡터(e5-small) + BM25 하이브리드(RRF 융합) → C1 다중신호 재랭킹** 으로 확장한다. 어휘가 달라도 의미가 비슷한 결정을 끌어온다. 100% 로컬, graceful 폴백.

## 1. 결정 (브레인스토밍 합의)

- **구조**: 하이브리드 RRF (벡터∪BM25 → RRF 융합 → C1 재랭킹). *벡터를 C1 점수의 한 항으로만 넣는 대안은 BM25 후보 풀 밖을 못 찾아 기각.*
- **운영**: 기본 ON + **graceful 폴백** — 임베더/ChromaDB 로드 불가(파이프라인 점유·RAM 부족·Python3.13 SIGSEGV preflight) 시 조용히 BM25-only(=현재 `search()`)로 폴백.
- **벡터 단위**: 페이지당 1개 (결정 페이지는 짧음, title+body, `passage:` 접두사).

## 2. 불변식 준수 (계획서 8개)

1. 인용 무결성: 결과의 `citations` 는 C1 경로와 동일하게 보존 (벡터는 *검색*에만 관여, 본문 불변).
2. 점수만 조정·원문 보존: 벡터/RRF 는 검색 점수에만. 디스크/페이지 불변.
3. 100% 로컬: e5-small(MPS) + ChromaDB PersistentClient + SQLite FTS5. 외부 API 0.
4. 모델 로드: G1 은 게이트 항목 — 사용자 오버라이드. 쿼리 임베딩은 `ModelLoadManager.acquire()` 뮤텍스로 일원화, 실패 시 폴백 → 코어(BM25) 무영향.
5. 기존 모듈 재사용: `search/hybrid_search._compute_rrf_score`, `steps/embedder.Embedder`(모델 로드·passage 임베딩), `core/preflight.can_use_chromadb`.
6. fail-loud·자동수정 금지: 벡터 색인 실패는 경고 로그 + 폴백(검색은 fail-loud 아닌 graceful — 기존 hybrid_search 와 동일 정책). canon 불변.
7. 단일 대형모델·피크RAM≤9.5GB·발열: e5-small(~470MB) 은 뮤텍스로 직렬화. 파이프라인 점유 중이면 폴백.
8. 설정 하드코딩 금지: `config.yaml` `wiki.semantic.*`.

## 3. 컴포넌트

### 3.1 설정 — `WikiSemanticConfig` (config.py, `wiki.semantic`)
```yaml
wiki:
  semantic:
    enabled: true          # false면 순수 BM25 (현재 동작)
    vector_weight: 0.6
    fts_weight: 0.4
    rrf_k: 60
    top_k_vector: 20        # 벡터 검색 후보 수
    collection_name: "wiki_pages"
```

### 3.2 `core/wiki/semantic_index.py` — `WikiSemanticIndex`
- **별도 ChromaDB 컬렉션** `wiki_pages` (transcript 컬렉션과 분리), `paths.resolved_chroma_db_dir` 하위.
- `index_page(page)` / `rebuild(store)`: 페이지 title+body 임베딩(passage:) → 컬렉션 upsert. 메타에 page_path·page_type·status 등. **임베더/chromadb 불가 시 skip+경고**.
- `async vector_search(query, top_k) -> list[(page_path, rank)]`: `ModelLoadManager.acquire("e5_search", load)` 로 e5 로드 → query 임베딩(query:) → `collection.query` → page_path 랭킹. 실패 시 `[]`.
- preflight(`can_use_chromadb`) 가드, PersistentClient 캐싱(PERF-011 패턴).

### 3.3 하이브리드 함수 — `core/wiki/semantic_search.py` `async wiki_hybrid_search(...)`
```
async def wiki_hybrid_search(query, *, search_index, semantic_index, ranking, semantic_cfg, now, top_k):
  bm25_ranked = search_index.bm25_candidates(query, limit)      # [(page_path, rank)] + _Candidate 메타
  vec_ranked  = await semantic_index.vector_search(query, top_k_vector)  # [] 가능(폴백)
  fused = RRF(bm25_ranked, vec_ranked, v_w, f_w, k)             # _compute_rrf_score 재사용
  candidates = 메타 조합(벡터 전용 page_path 는 wiki_page_meta 에서 보강)
  scored = _rerank(candidates with retrieval=fused_score, ranking, now)   # C1 재랭킹 재사용
  return top_k WikiSearchResult
```
- `WikiSearchIndex.search()` (sync) 는 **그대로 보존**(BM25+C1, 100% 하위호환). `bm25_candidates()` 헬퍼만 추출해 재사용.

### 3.4 호출자 연결
- `api/routers/wiki.py` (`/api/wiki/search`), `core/wiki/chat_integration.py`: `semantic.enabled` 면 `await wiki_hybrid_search(...)`, 아니면 기존 sync. **응답 스키마(WikiSearchResult) 불변 → 공개 API 계약 보존.**

## 4. 테스트 (불변식: 로컬·시크릿/네이티브모델 없이 결정적)
- 임베더를 **목 주입**(가짜 384-d 벡터 함수)으로:
  - RRF 융합 순위 정확성 (벡터 랭킹 + BM25 랭킹 → 기대 순서).
  - **벡터 전용 매치 유입**: BM25 0건이지만 의미 유사한 페이지가 결과에 포함.
  - graceful 폴백: vector_search 가 `[]`(임베더 불가) → BM25-only 와 동일 결과.
  - C1 재랭킹 합성: 융합 후에도 superseded 하향·recency 상향 유지.
- 실모델 통합테스트 1개: `-m native`(또는 ui) 마킹, e5-small 실제 로드 (선택, CI 게이트 분리).
- 비회귀: `tests/wiki/` 전체, transcript RAG(`search/hybrid_search`)·`/api/wiki/search` 계약.

## 5. 인덱싱 시점 / 백필
- 페이지 쓰기(compiler) 시 FTS 색인과 함께 벡터 색인. 임베더 가용 시.
- 기존 위키 페이지 백필: 기존 reindex 메커니즘 확장 또는 `WikiSemanticIndex.rebuild(store)` 1회.

## 6. 리스크
| 리스크 | 대응 |
|---|---|
| e5 로드가 파이프라인(Gemma)과 RAM/발열 경합 | 뮤텍스 직렬화 + 실패 시 폴백. e5 470MB 소형. |
| Python 3.13 chromadb SIGSEGV | `preflight.can_use_chromadb` 가드 → 폴백 |
| 벡터 인덱스 stale(페이지 변경 후 미갱신) | upsert 시 동기 갱신, 백필 제공 |
| 공개 API 계약 변경 | 응답 스키마 불변(내부 검색만 교체) |

## 7. 수락 기준
- 의미 유사(어휘 비매칭) 결정이 결과에 유입됨을 단위테스트로 증명.
- graceful 폴백 = BM25-only 동일 결과 증명.
- C1 재랭킹 신호(superseded/recency) 융합 후 보존.
- 가중치·임계 전부 config. `tests/wiki/` 비회귀. 공개 API 계약 보존.
- 검증은 목 임베더로 로컬·결정적. 실모델은 마킹 분리.

## 8. 적대 리뷰 반영 (2026-06-07, 4-렌즈)

리뷰 결과 blocker 0. correctness=pass, 나머지 concerns. 반영/판단:

- **수정됨**:
  - `fuse_and_rerank`에 `ranking.enabled=False` escape hatch 추가 → BM25-only 경로(`search()`)와 동작 일치(재랭킹 OFF면 융합점수 순서만).
  - 테스트 격리: `tests/conftest.py`에 `_isolate_chroma_db` autouse fixture — chat_integration의 `get_config()` 싱글톤이 실 `~/.meeting-transcriber/chroma_db`를 오염시키던 회귀 차단(tmp 강제).
  - 인용 보존 단언(벡터 전용 페이지의 `citations`) + escape hatch 단위테스트 추가.
  - 명세 정합: 계획서 §8 `semantic.enabled` false→true + G1 게이트 오버라이드 명문화.

- **정당화(설계 결정)**:
  - **문서 임베더 뮤텍스 우회**: `make_default_embed_documents`는 ModelLoadManager 없이 e5를 직접 로드한다(쿼리 임베더는 뮤텍스 사용). 정당한 이유 — compiler `_reindex_semantic`은 `core/pipeline.py`에서 메인 시퀀스(STT~EMBED) **완료 후** WIKI_COMPILE 단계에서 호출되므로 다른 대형 모델이 언로드된 상태다. 즉 동시 적재 위험이 구조적으로 낮다. (향후 wiki compile이 다른 모델과 병렬화되면 뮤텍스 경유로 통일 필요.)
  - **매 ingest 전체 재임베딩**: `rebuild_semantic_index`가 전체 페이지를 재임베딩(BM25 rebuild와 동일 패턴). 소규모 corpus(<1000) 가정에서 수용. e5 인코딩 비용이 FTS보다 크므로, 코퍼스 증가 시 **변경 페이지만 증분 upsert**(이미 있는 `WikiSemanticIndex.upsert`)로 전환은 C4 측정 후 검토.
  - **"비용 0" 단락은 e5 한정**: `count()==0` 단락은 e5(470MB) 로드만 막고, ChromaDB PersistentClient 인스턴스화 비용은 검색당 발생(소규모에선 무시). 벡터 미색인 환경에서 순수 BM25와 완전 동일 비용을 원하면 운영상 `semantic.enabled: false` 유지.

## 9. 후속 TODO 처리 현황 (우선순위 순)

- ✅ **① wiring 단위테스트 + RAM 자동측정** (2026-06-08): `tests/wiki/test_g1_wiring.py`(compiler `_reindex_semantic` 임베더 주입/미주입/비활성 분기 + chat config 주입/미주입 분기, 가짜 주입 5건). `test_semantic_real_e5.py`에 피크 RSS < 3GB soft-assert(불변식 #7 회귀 가드).
- ✅ **② C4 recall@5 정량화** (2026-06-08): `tests/wiki/test_g1_recall_eval.py`(native). 8개 패러프레이즈 골든셋(어휘 비매칭 쿼리↔결정) 실측 — **BM25 recall@5 = 0% (0/8), HYBRID = 100% (8/8), Δ=+100%**. 게이트 오버라이드(벡터 켜기)의 가치를 수치로 고정: BM25가 원천적으로 못 하는 시맨틱 회상을 e5가 전부 해냄. `hybrid ≥ bm25` 회귀 가드 포함.
- ✅ **③ 증분 upsert** (2026-06-08): `reindex_incremental` — content_hash(sha256) 를 ChromaDB 메타에 저장해 신규/변경 페이지만 재임베딩(전체 재임베딩 회피 → 발열·시간 절감), 스토어에서 사라진 페이지(거부/이동)는 orphan 으로 삭제해 stale 벡터 방지(일관성). compiler `_reindex_semantic` 이 사용. `rebuild_semantic_index` 는 백필용으로 유지(hash 저장). 단위 4건(최초 전체·무변경 0·변경분만·orphan 삭제) + native 1건(실 chroma 메타 라운드트립).
- ⏳ **④ 발열/동시성 가드**: 파이프라인 점유 중 e5 acquire 직렬화 검증(model_manager 단일 lock은 구조적으로 보장됨, 테스트만 미작성 — 우선순위 낮음).
