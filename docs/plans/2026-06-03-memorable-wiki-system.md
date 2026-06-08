# Memorable Wiki — 에이전트 장기메모리화 구현 계획 (축소 코어판)

**작성일**: 2026-06-03 (rev. 축소 코어 — 로컬·팬리스 현실성 반영)
**상태**: 기획 확정안
**목적**: Decision Wiki를 "기억 장부"에서 "살아있는 기억 시스템"으로 끌어올리되, 1인·로컬·팬리스(MacBook Air 16GB) 환경에 *실제로 맞는* 범위로만 진행한다.
**관련 문서**: `docs/plans/2026-05-21-decision-wiki-product-plan.md`, `docs/plans/2026-04-28-llm-wiki-hybrid.md`
**근거 리서치(메모리)**: `wiki-as-agent-memory` (MemGPT/Mem0/Generative Agents/Hermes/OpenClaw/MemWeave)

---

## 0. 한 문장 정의

> Recap의 메모리 시스템은 회의 의사결정을 **검증된 인용과 함께 축적**하고, 거기에 **(a) 더 잘 떠올리는 다중신호 검색**과 **(b) 항상 보이는 작은 현황 요약(working set)**을 얹어, 사용자가 "내 업무가 지금 어떤 상태이고 무엇이·왜 결정됐는지"를 즉시 알게 하는 로컬 의사결정 기억 시스템이다.

---

## 1. 핵심 재프레임 — 무엇을 *덜* 하기로 했나

리서치가 제시한 4대 격차는 **MemGPT·Hermes 처럼 메모리를 컨텍스트 창에 *통째로 주입*하는 시스템**에서 나온 처방이다. 그들은 **유한한 컨텍스트 창**(Hermes 800토큰 캡) 때문에 *강제로 잊어야* 한다.

**그러나 우리 위키는 주입되지 않고 *검색*된다.** 로컬 디스크의 마크다운 결정 페이지 수백 개는 용량·검색 부담이 아니다. 따라서:

| 리서치 격차 | 클라우드/주입형의 동기 | 우리(검색형·로컬·소규모) 재평가 |
|---|---|---|
| ① 자동 망각/압축 | 유한 컨텍스트 창 압박 | **압박 없음** → 진짜 니즈는 "압축"이 아니라 **항상 보이는 작은 현황 요약** (LLM 불필요) |
| ② 다중신호 랭킹 | 대규모 회상 정확도 | **유효** → 싸고 효과 큼. **코어** |
| ③ 메모리 계층화 | main/external 페이징 | **부분만** → index/digest를 "core"로, 상세는 검색. 페이징 자체는 불필요 |
| ④ 벡터 시맨틱 회상 | 의미 회상 | **불확실** → 소규모·정제된 결정문엔 BM25+동의어+랭킹이면 충분할 수 있음. **측정 후 결정** |

**결론**: 무거운 LLM 루프(consolidation, self-edit)와 쿼리당 모델 로드(벡터)는 **증거가 나올 때까지 미룬다.** 모델 로드를 유발하지 않는 코어부터 한다.

---

## 2. 목표 사용자 경험

**A. 업무·회의 현황 파악 (working memory)** — "내 미해결 액션은?", "이번 주 결정 요약?", "A 프로젝트 현재 상태?"
→ **현황 다이제스트**(집계, LLM 없음)를 항상 최신으로 첫 화면에 노출.

**B. 의사결정 검색 (retrieval)** — "가격 정책 왜 바뀌었어?", "이 결정 뒤집힌 적 있어?", "김영욱 후속액션?"
→ **다중신호 랭킹**으로 top-k 정확도 확보.

---

## 3. 불변식 (절대 깨지 않음 — 매 변경마다 검증)

1. **인용 무결성 최우선**: 어떤 요약·재랭킹도 `[meeting:{id}@HH:MM:SS]` 인용을 손실시키지 않는다.
2. **원문 보존, 점수만 조정**: recency/decay는 *검색 랭킹 점수*에만. 디스크 페이지는 변경 안 함(git 영구 아카이브).
3. **100% 로컬**: 외부 메모리 SaaS·임베딩 API 금지. e5-small(MPS)+ChromaDB+SQLite FTS5만.
4. **모델 로드 최소화 우선**: 코어는 LLM/임베딩 로드 0. 모델 로드를 유발하는 작업은 게이트 뒤로.
5. **기존 모듈 재사용**: `search_index.py`·`lint.py` 확장, (게이트 시) `hybrid_search.py`·`embedder.py` 재사용.
6. **fail-loud, 자동수정 금지(R1)**: lint/요약은 *제안*만, canon 자동 덮어쓰기 금지.
7. **단일 대형모델 적재**(ModelLoadManager 뮤텍스), 피크 RAM ≤9.5GB, 발열 정책(2건 후 3분 쿨다운) 존중.
8. **설정 하드코딩 금지**: 가중치·반감기·임계는 `config.yaml` `wiki.*`.

---

## 4. 범위 3단 분류 (한눈에)

| 구분 | 항목 | 모델 로드 | 근거 |
|---|---|---|---|
| **✅ 코어 (즉시 진행)** | C1 다중신호 랭킹 · C2 현황 다이제스트(집계) · C3 다이제스트/검색 UI · C4 소규모 골든셋 | **없음** | 싸고 즉효, 로컬 무부담 |
| **⏸ 게이트 (측정 후 결정)** | G1 벡터 시맨틱 회상 · G2 LLM consolidation | 있음 | 코어 효과 측정 뒤 *증거 있을 때만* |
| **⛔ 보류 (무기한)** | D1 채팅 self-edit (LLM 자율 쓰기) | 있음 | 위험 대비 이득 낮음 |

---

## 5. 코어 Phase 실행 계획

### C1. 다중 신호 검색 랭킹 (격차 ②) — 🥇 최우선

`core/wiki/search_index.py`. BM25 단일 점수를 결합 점수로 교체 (스키마 무변경 — 메타는 `wiki_page_meta`에 이미 존재). 후보 풀(BM25 상위 `candidate_pool`건) 내 **후처리 재랭킹**.
```text
positive = w_bm25·norm(bm25) + w_recency·halflife(age_days) + w_conf·(confidence/10)
           + w_cite·norm(citation_count)
final = positive                              (비-superseded)
      = positive − Σw − superseded_penalty    (superseded → 구조적 하향)
```
- 최신성은 반감기 감쇠 `0.5^(age/half_life)` (= `exp(−ln2·age/half_life)`), config 키는 `recency_half_life_days`.
- **superseded 구조적 floor**: superseded 점수를 `positive − Σw − penalty < 0`으로 강제 → 비-superseded(≥0)보다 항상 아래. 가중치와 무관하게 **역전 0% 보장**(선형 패널티는 가중치 따라 보장 불가하여 구조적 floor 채택).
- 가중치·반감기·`candidate_pool`은 `config.yaml` `wiki.ranking`에서 로드.
- `enabled: false` escape hatch → 순수 BM25 정렬(기존 동작). `now` 주입으로 recency 테스트 결정성.
- (선택) MMR 다양성으로 동일 프로젝트/주제 중복 억제 (어휘 Jaccard, 인용 마커 제외, 기본 off).
- **알려진 한계**: 한 쿼리에 `candidate_pool` 초과 매칭 시 BM25 하위·최신 결정이 풀 밖으로 누락 가능 → C4에서 매칭 폭 측정 후 `candidate_pool` 조정.

**완료 기준**: 최근·고confidence 상향·superseded 하향(역전 0%)을 단위 테스트로 증명 / `search()` 시그니처 하위호환 / 인덱스 스키마 무변경·`tests/wiki/` 비회귀 / 가중치·임계 전부 config 로드.
**로컬 비용**: 0 (순수 산술).

### C2. 현황 다이제스트 (격차 ①③의 *실제* 니즈) — LLM 없음

신규 `core/wiki/digest.py`. **집계만**(LLM 호출 0):
- 미해결 액션아이템(owner별), 최근 N일 결정, 프로젝트별 현재 상태/마지막 결정.
- 모든 줄은 원본 인용 보존. `digest.md`로 렌더 + `index.md`와 함께 "core" 표면.

**완료 기준**: 미해결 액션·최근 결정·프로젝트 상태를 인용과 함께 정확히 집계(누락 0) / LLM 호출 0 / 단위 테스트.
**로컬 비용**: 0 (frontmatter 집계).

### C3. UI — 현황 화면 + 검색 메타

`ui/web/wiki-view.js` 등. `/app/wiki` "현황(Overview)" 탭(digest 렌더) + 검색 결과에 score·snippet·citations·status 표시 + citation 클릭→viewer deep link. `docs/design.md` 토큰·컴포넌트 준수(UI 작업 전 필독).

**완료 기준**: 채팅 없이 현황 파악 + 결정 필터/검색 + 근거 이동.

### C4. 소규모 골든셋 (게이트 판단 근거)

10~20개 회의 골든셋. 측정: 랭킹 효과(superseded 역전율), **BM25 recall@5**, 다이제스트 누락. → 이 수치가 **G1/G2 착수 여부의 게이트**.

**완료 기준**: 코어 효과가 수치로 증명되고, recall@5가 목표 미달인지 *판정 가능*.

---

## 6. 게이트 항목 (측정 후 결정 — 기본 OFF)

### G1. 위키 벡터 시맨틱 회상 (격차 ④) — ✅ **2026-06-07 사용자 지시로 활성(게이트 오버라이드)**
위키 페이지를 전용 ChromaDB `wiki_pages`에 e5-small 임베딩(`embedder.py` 재사용), `hybrid_search.py` RRF 패턴 차용. 실패 시 BM25-only graceful degradation.

> **게이트 오버라이드 기록(2026-06-07)**: 원래 조건은 "C4에서 BM25 recall@5 < 목표일 때만". 사용자가 "임베딩도 써"라고 직접 지시해 활성화했다. 즉 **off→on 결정은 오버라이드됐고, C4 recall@5 측정은 착수 전제에서 후속 측정(deferred)으로 변경**됐다. 실측 검증(`tests/wiki/test_semantic_real_e5.py`, native): 어휘 비매칭 쿼리가 의미로 회상(BM25=∅, 하이브리드=정답 1위), RSS 피크 ~1.35GB(9.5GB 한도 안전). **남은 측정 TODO**: C4 골든셋에서 recall@5 이득(비용 대비) 정량화 + RSS 자동 단언. 상세 설계: `docs/plans/2026-06-07-wiki-g1-semantic-design.md`.

### G2. LLM consolidation (격차 ①) — 조건: 아카이브가 실제로 검색/UX를 해칠 때만
오래된 결정 롤업 요약. **전제**: 원본 영구 보존 + 인용 100% 전이 + 자동반영 금지(pending 제안) + summarization drift 회귀 테스트. 검색 기반 시스템에선 필요성이 낮으므로 **증거 우선**.

---

## 7. 보류 항목 (무기한)

- **D1. 채팅 self-edit**: LLM이 가드 통과 후 위키를 직접 수정. 가드가 있어도 자율 쓰기는 위험 대비 이득이 낮아 보류.
- topics 지식그래프, 다중 사용자, 형태소 분석기, 외부 ingest — 기존 product-plan §8 비범위 유지.

---

## 8. 설정 스키마 (`config.yaml` 신설)

```yaml
wiki:
  ranking:                    # C1
    enabled: true             # false=순수 BM25 정렬(escape hatch)
    candidate_pool: 50        # 재랭킹 입력 후보 풀(BM25 상위 N). 광매칭 쿼리 많으면 상향
    w_bm25: 1.0
    w_recency: 0.5
    w_confidence: 0.3
    w_citation: 0.2
    superseded_penalty: 0.5   # superseded 구조적 floor 아래에서의 추가 간격
    recency_half_life_days: 90
    mmr_enabled: false        # 선택 — C4 검증 전까지 off
    mmr_lambda: 0.7           # 선택
  semantic:                   # G1 — 2026-06-07 사용자 오버라이드로 활성(enabled: true)
    enabled: true             # (원래 기본 off → 사용자 지시로 on. C4 recall 측정은 후속)
    vector_weight: 0.6
    fts_weight: 0.4
    rrf_k: 60
    top_k_vector: 20
    collection_name: "wiki_pages"
  consolidation:              # G2 — 기본 off
    enabled: false
```
- `wiki_page_meta`의 기존 컬럼(decision_date·confidence·last_updated·citations)으로 C1 충당 → **스키마 무변경 우선**. 인용빈도가 필요하면 citations 문자열 길이/카운트로 파생.

---

## 9. 평가셋 · 품질 게이트

| 지표 | 목표 | 단계 |
|---|---|---|
| superseded가 confirmed보다 상위에 오는 비율 | 0% (구조적 floor로 가중치 무관 보장) | C1 |
| 다이제스트 미해결 액션 누락 | 0건 | C2 |
| 인용 보존(요약/재랭킹 전후) | 100% | C1·(G2) |
| BM25 recall@5 | 측정 → G1 게이트 | C4 |
| 기존 transcript RAG·`/api/chat`·`/api/wiki/search` 비회귀 | 계약 유지 | 전 단계 |

---

## 10. 리스크 · 대응

| 리스크 | 대응 |
|---|---|
| 랭킹 가중치 과적합 | C4 골든셋으로 검증, 가중치 config화 |
| (G1) 위키 임베딩이 회의 처리 RAM/발열과 경합 | 별도 배치 + 뮤텍스 + 쿨다운, 기본 off |
| (G2) summarization drift·인용 손실 | 원본 보존 + 인용 전이 강제 테스트, 자동반영 금지 |
| 범위가 broad Wiki로 재확장 | 코어/게이트/보류 분류 고정 |

---

## 11. 최종 수락 기준 (코어만)

1. 검색이 최근·확정·근거많은 결정을 상위로, superseded는 하향(C1).
2. 현황 다이제스트가 미해결 액션·최근 결정·프로젝트 상태를 인용과 함께 항상 최신, **LLM 호출 0**(C2).
3. UI에서 채팅 없이 현황 파악 + 검색/필터 + 근거 이동(C3).
4. 골든셋으로 코어 효과가 수치 증명되고 G1/G2 착수 여부를 *판정*(C4).
5. 기존 RAG·채팅 비회귀, 100% 로컬, 피크 RAM ≤9.5GB, **코어 전 구간 모델 로드 0**.
