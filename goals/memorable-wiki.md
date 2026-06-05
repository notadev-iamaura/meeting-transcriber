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
- ④ 벡터 회상 → 소규모·정제된 결정문엔 BM25면 충분할 수 있음. **측정 후 결정**.

원칙: **모델 로드를 유발하지 않는 코어부터.** 무거운 LLM 루프와 쿼리당 임베딩은
증거가 나올 때까지 미룬다.

## Baseline

- Date: 2026-06-03, Branch `main` (Phase별 격리 브랜치 → PR)
- `core/wiki/*` 구현됨(store git원자커밋/schema/decision_record supersedes/
  search_index FTS5·BM25/lint D4/guard D1~D5). 검색은 BM25 단일점수만.
  위키 시맨틱 회상 없음(transcript RAG `search/hybrid_search.py` 벡터0.6+FTS0.4
  RRF k=60와 분리). consolidation·다이제스트·다중신호 랭킹 없음.

## 불변식 (매 변경 검증)

1. 인용 무결성 최우선: 어떤 요약/재랭킹도 `[meeting:{id}@HH:MM:SS]` 손실 금지.
2. 원문 보존·점수만 조정: decay/recency는 검색 점수에만, 디스크 불변(git 아카이브).
3. 100% 로컬: 외부 메모리 SaaS·임베딩 API 금지. e5-small+ChromaDB+SQLite FTS5만.
4. 모델 로드 최소화: 코어는 LLM/임베딩 로드 0. 로드 유발 작업은 게이트 뒤로.
5. 기존 모듈 재사용: search_index/lint 확장, (게이트 시) hybrid_search/embedder.
6. fail-loud·자동수정 금지(R1): lint/요약은 제안만, canon 자동 덮어쓰기 금지.
7. 단일 대형모델 적재(ModelLoadManager 뮤텍스), 피크 RAM≤9.5GB, 발열 정책
   (2건 후 3분 쿨다운) 존중.
8. 설정 하드코딩 금지: 가중치·반감기·임계는 config.yaml `wiki.*`.

## 범위 3단 분류

- ✅ **코어(즉시)**: C1 다중신호 랭킹 · C2 현황 다이제스트(집계) · C3 UI ·
  C4 소규모 골든셋 — **모델 로드 0**.
- ⏸ **게이트(측정 후)**: G1 벡터 회상(조건: C4에서 BM25 recall@5 목표 미달 시만) ·
  G2 LLM consolidation(조건: 아카이브가 실제로 검색/UX 해칠 때만).
- ⛔ **보류(무기한)**: D1 채팅 self-edit.

## Current Phase: C1 — 다중 신호 검색 랭킹 (격차 ②)

Goal: `core/wiki/search_index.py` BM25 단일점수를 recency·confidence·인용빈도·
superseded 패널티와 결합. 인덱스 스키마 무변경(메타 `wiki_page_meta`에 존재),
후처리 재랭킹 레이어.

권장 순서:
1. C1a: `config.yaml` `wiki.ranking` 블록 + `config.py` Pydantic 모델
   (가중치·반감기·MMR λ, 기본값 계획서 §8).
2. C1b: `search()` 재랭킹 = BM25정규화 + recency exp(−λ·age_days) + confidence
   + 인용빈도 − superseded 패널티.
3. C1c: (선택) MMR 다양성으로 동일 프로젝트/주제 중복 억제.
4. C1d: 단위 테스트로 랭킹 효과 증명 + 기존 위키 테스트 비회귀.

완료 기준:
- 최근·고confidence 상향·superseded 하향을 단위 테스트로 증명.
- `search()` 시그니처 하위호환(기존 필터 인자 유지).
- 인덱스 스키마 무변경. `tests/wiki/` 비회귀.
- 가중치·반감기 전부 config 로드(하드코딩 0). 로컬 비용 0(순수 산술).

## 이후 코어 (계획서 §5)

- C2 현황 다이제스트(LLM 없음): 신규 `core/wiki/digest.py`. 미해결 액션/최근
  결정/프로젝트 상태 집계(인용 보존, 누락 0, LLM 호출 0).
- C3 UI: `/app/wiki` 현황 탭 + 검색 메타(score/snippet/citations/status),
  `docs/design.md` 준수.
- C4 골든셋(10~20 회의): 랭킹 효과·**BM25 recall@5**·다이제스트 누락 측정 →
  G1/G2 착수 게이트.

## Continue When

- 변경이 단일 모듈(또는 직접 결합 쌍)으로 스코프되고 테스트 커버리지 명확.
- 불변식 8개 보존, **코어는 모델 로드 0** 유지.
- 로컬에서 네이티브 모델 다운로드·시크릿 없이 검증 가능.

## Stop Or Pause When

- 인용 무결성·100% 로컬·자동수정 금지·모델로드0(코어) 중 하나라도 위협.
- 게이트(G1/G2) 착수 근거(C4 수치)가 아직 없음 → 코어 먼저.
- (G1) 위키 임베딩이 회의 처리 RAM/발열과 경합할 위험.
- 공개 API(`/api/wiki/*`,`/api/chat`) 계약이 마이그레이션 계획 없이 변경.
- broad Wiki 재확장(topics 지식그래프 등)으로 범위 번짐.

## 검증 게이트

- `pytest tests/wiki/ -q`·전체 `pytest tests/ -x -q` / `py_compile <변경파일>`.
- rumps·soundfile 누락성 실패는 무시, CI 신뢰(`test_env_quirks` 메모리).
- 비회귀: transcript RAG·`/api/chat`·`/api/wiki/search` 응답 계약 유지.
- PR CI green 후 머지.

## 품질 목표

- superseded가 confirmed보다 상위에 오는 비율: 0% (C1).
- 다이제스트 미해결 액션 누락: 0건 (C2).
- 인용 보존(요약/재랭킹 전후): 100%.
- BM25 recall@5: 측정 → G1 게이트 판단 (C4).
