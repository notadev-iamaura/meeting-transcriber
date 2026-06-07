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
