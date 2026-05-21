# Decision Wiki 제품화 개선안

**작성일**: 2026-05-21
**상태**: 기획 확정안
**목적**: 전사된 회의 내용을 검증 가능한 의사결정 위키로 만들고, 사용자가 언제든 의사결정사항을 쉽게 찾게 한다.
**관련 문서**: `docs/plans/2026-04-28-llm-wiki-hybrid.md`, `docs/plans/rag-pipeline-completion.md`
**검토 방식**: 에이전트팀 독립 검토 후 만장일치 위원회 합의

---

## 0. 결론

이 프로젝트의 핵심 제품 가치는 "AI와 채팅하기"가 아니다. 핵심은 회의에서 나온 의사결정이 사라지지 않도록 구조화하고, 나중에 사용자가 "무엇을, 언제, 왜, 누가 결정했는지"를 원문 근거와 함께 찾게 하는 것이다.

따라서 기존 LLM Wiki × RAG 계획은 다음 방향으로 좁힌다.

1. MVP 범위는 범용 Wiki가 아니라 **Decision Wiki**다.
2. `decisions/`를 1급 데이터로 승격하고, `people/`, `projects/`, `topics/`는 우선 필터/메타데이터로만 사용한다.
3. 채팅은 의사결정 검색을 보조하는 인터페이스이며, 주된 제품 표면은 **결정사항 목록/검색/상세 화면**이다.
4. Wiki 검색은 현재의 substring scan 또는 "앞 3개 페이지 합성"이 아니라, 별도 BM25/FTS5 인덱스를 사용한다.
5. 전사 원문 RAG와 Decision Wiki는 서로 다른 지식 레이어로 유지한다.
6. Wiki에 승격되는 모든 결정은 최소 1개 이상의 검증된 `[meeting:{id}@HH:MM:SS]` 인용을 가져야 한다.

---

## 1. 만장일치 위원회 검토

### 1.1 참여 관점

| 검토자 | 관점 | 핵심 결론 |
|---|---|---|
| 제품 검토 | 사용자가 원하는 실제 가치 | "범용 Wiki"가 아니라 "근거 달린 의사결정 DB"가 제품이다. |
| 아키텍처 검토 | RAG/Wiki 계층 분리 | 전사 원문 증거 레이어와 Decision Wiki 레이어를 분리하고, Wiki 전용 BM25 인덱스를 둔다. |
| 운영/UX 검토 | 실제 사용성과 복구 가능성 | 자동 생성, 백필, 검색 UI, citation 검증, 실패 복구가 없으면 제품으로 신뢰하기 어렵다. |

### 1.2 합의된 원칙

세 검토자는 다음 원칙에 만장일치로 동의했다.

1. **RAG는 대체하지 않는다.**
   RAG는 정확한 발화 찾기, 단일 회의 회상, citation 검증에 강하다. Decision Wiki는 누적 의사결정 검색에 집중한다.

2. **의사결정이 최상위 단위다.**
   Wiki 페이지, 채팅 답변, 검색 결과의 중심 객체는 회의 청크가 아니라 decision record다.

3. **citation은 신뢰의 원자 단위다.**
   원문 발화로 돌아갈 수 없는 결정은 canonical decision으로 승격하지 않는다.

4. **자동 생성은 보수적이어야 한다.**
   낮은 confidence, citation 불일치, 모호한 결정은 `pending/` 또는 rejected 상태로 남긴다.

5. **넓은 Wiki보다 좁은 완성도가 우선이다.**
   `topics/` 자동 생성, 사람 프로필, 프로젝트 내러티브, LLM contradiction lint는 MVP 이후로 미룬다.

6. **검색은 채팅보다 먼저다.**
   사용자가 결정사항을 직접 탐색할 수 있어야 하며, 채팅은 그 위에 얹는 보조 인터페이스다.

### 1.3 토론 중 제기된 이견과 결론

| 쟁점 | 초기 의견 | 합의 결론 |
|---|---|---|
| 5종 Wiki를 모두 MVP에 포함할지 | 기존 계획은 decisions/action_items/people/projects/topics 전체를 다룸 | MVP는 `decisions/`와 decision-linked action만 포함한다. 나머지는 facets로 시작한다. |
| BM25를 어디에 넣을지 | RAG FTS5 대체 또는 Wiki 검색 도입 | 먼저 Wiki 전용 BM25/FTS5 인덱스를 만든다. 기존 transcript RAG는 유지한다. |
| QueryRouter를 먼저 켤지 | 라우터로 RAG/Wiki 자동 선택 | 관련도 검색 없는 라우팅은 위험하다. Wiki BM25 검색 이후에 켠다. |
| 백필을 언제 할지 | 전체 과거 회의 일괄 백필 | 먼저 20개 내외 골든셋으로 품질을 측정한 뒤, durable backfill로 확장한다. |
| 생성된 Wiki 답변을 바로 신뢰할지 | LLM 합성 답변 제공 | 답변보다 decision record와 citation을 우선 노출한다. 답변은 citation preserving 검증 후 제공한다. |

---

## 2. 현재 구현 진단

### 2.1 이미 구축된 자산

현재 코드베이스에는 Decision Wiki 제품화를 위한 기반이 이미 있다.

- 파이프라인은 `SUMMARIZE -> CHUNK -> EMBED` 순서로 확장되어 RAG 인덱스를 만든다.
- `steps/wiki_compiler.py`와 `core/wiki/compiler.py`에는 WikiCompiler와 extractor 골격이 있다.
- `core/wiki/extractors/decision.py`에는 `ExtractedDecision` 모델과 JSON 기반 결정 추출 흐름이 있다.
- `core/wiki/citation_verifier.py`에는 발화 timestamp 기반 citation 검증자가 있다.
- `api/routers/wiki.py`에는 Wiki 페이지 조회, 검색, 백필 API가 있다.
- `core/wiki/chat_integration.py`에는 RAG/Wiki/Both 라우팅 통합 경로가 있다.

### 2.2 핵심 갭

현재 상태는 "제품 목적에 부합하는 완성"으로 보기 어렵다. 가장 중요한 갭은 다음이다.

1. **자동 ingest wiring이 불완전하다.**
   `PipelineManager`가 WikiCompiler를 호출하지만, 실제 compile에 필요한 `summary`와 `utterances`를 넘기지 않는 경로가 존재한다. 이 상태에서는 `dry_run=False`여도 실제 decision page 생성으로 이어지지 않을 수 있다.

2. **Wiki 검색이 제품 검색이 아니다.**
   `/api/wiki/search`는 단순 substring scan이다. 한국어 어미 변화, 동의어, 제목/본문 가중치, decision type 필터를 다루지 못한다.

3. **Wiki 채팅 합성이 관련도 기반이 아니다.**
   `HybridChatService._synthesize_from_wiki()`는 현재 Wiki 페이지를 정렬한 뒤 앞 3개를 합성하는 최소 정책이다. 사용자의 질문과 무관한 페이지가 답변에 섞일 수 있다.

4. **Decision UI가 1급이 아니다.**
   현재 `/app/wiki`는 일반 Wiki tree + raw markdown viewer 성격이다. 사용자가 의사결정사항을 빠르게 찾는 전용 테이블/필터/상세 화면이 필요하다.

5. **Wiki backfill은 운영 기능으로 부족하다.**
   백필 API는 있으나 in-memory job 상태라 서버 재시작에 취약하고, 설정 UI나 durable retry/resume 흐름이 부족하다.

6. **품질 기준이 없다.**
   STT 품질 평가는 있지만, 의사결정 추출 precision/recall, citation validity, search recall@k, answer citation coverage를 측정하는 골든셋이 없다.

---

## 3. 목표 제품 경험

### 3.1 사용자가 하고 싶은 일

사용자는 다음 질문에 빠르게 답을 얻어야 한다.

- "지난달 결정사항만 보여줘."
- "가격 정책은 언제, 왜 바뀌었어?"
- "A 프로젝트 일정은 최종적으로 어떻게 결정됐어?"
- "누가 이 결정을 제안했어?"
- "이 결정의 원문 근거를 들려줘."
- "이 결정이 나중에 뒤집힌 적 있어?"
- "김영욱이 담당하기로 한 결정이나 후속 액션은?"

### 3.2 제품 표면

MVP의 제품 표면은 다음 세 가지다.

1. **Decision List**
   - 날짜, 제목, 상태, 프로젝트, 관련자, confidence, source meeting 표시
   - 날짜 범위, 프로젝트, 관련자, 상태, confidence 필터
   - 검색어 하이라이트

2. **Decision Detail**
   - 결정 내용
   - 배경/이유
   - 후속 액션
   - 관련 프로젝트/사람
   - 검증된 citation 목록
   - citation 클릭 시 회의 viewer의 해당 timestamp로 이동
   - 변경 이력: supersedes / superseded_by

3. **Decision Chat**
   - Wiki BM25 검색 결과를 기반으로 답변
   - 답변에는 사용된 decision source와 citation을 반드시 표시
   - 답변이 citation을 유지하지 못하면 "검색 결과"만 보여주고 LLM 답변은 보류

---

## 4. 목표 아키텍처

### 4.1 두 개의 지식 레이어

```text
Layer A. Transcript Evidence Layer
  Source:
    corrected utterances, chunks, timestamps
  Index:
    existing ChromaDB vector + SQLite FTS5/RRF
  Used for:
    exact quote search
    single-meeting recall
    citation verification
    source playback

Layer B. Decision Wiki Layer
  Source:
    verified decision records rendered from transcripts
  Index:
    new Wiki SQLite FTS5/BM25 index
  Used for:
    cumulative decisions
    decision status
    project/person/date filtering
    decision chat context
```

두 레이어는 서로 import 경계를 지킨다.

- `search/hybrid_search.py`: transcript search만 담당한다.
- `core/wiki/search_index.py`: Wiki page/decision search를 담당한다.
- `core/wiki/chat_integration.py`: orchestration만 담당한다.
- `steps/wiki_compiler.py`: extraction/write path만 담당한다.

### 4.2 Decision ingest 흐름

```text
Corrected utterances + Summary
  -> DecisionExtractor
  -> structured ExtractedDecision[]
  -> CitationVerifier
  -> WikiGuard
  -> DecisionRecord normalize/dedupe
  -> WikiStore write
  -> git commit
  -> WikiSearchIndex upsert
```

### 4.3 Decision search/chat 흐름

```text
User query
  -> QueryRouter
    -> RAG: Transcript HybridSearchEngine
    -> WIKI: WikiSearchService(BM25)
    -> BOTH: run both, present separate evidence
  -> LLM synthesis only from retrieved records
  -> answer citation preservation check
  -> response with decision sources
```

---

## 5. Decision Record 스키마

MVP에서 canonical decision은 Markdown 페이지로 저장하되, frontmatter와 본문 구조는 기계적으로 파싱 가능해야 한다.

```yaml
---
type: decision
id: decision-2026-05-21-pricing-policy
title: 가격 정책 변경
status: decided
decision_date: 2026-05-21
project: Recap
participants: [김영욱, SPEAKER_01]
owners: [김영욱]
confidence: 8
source_meetings: [20260521_weekly]
supersedes: []
superseded_by:
last_updated: 2026-05-21T17:00:00
---

# 가격 정책 변경

## 결정 내용

엔터프라이즈 플랜 가격을 월 99달러로 확정한다. [meeting:20260521_weekly@00:14:32]

## 배경

고객 지원 비용 증가와 경쟁사 가격대를 고려했다. [meeting:20260521_weekly@00:18:05]

## 후속 액션

- 김영욱: 가격표 문구를 업데이트한다. [meeting:20260521_weekly@00:20:41]

## 근거

- [meeting:20260521_weekly@00:14:32]
- [meeting:20260521_weekly@00:18:05]

<!-- confidence: 8 -->
```

### 5.1 필수 필드

accepted decision은 반드시 다음을 가져야 한다.

- `id`
- `title`
- `status`
- `decision_date`
- `decision_text`
- `source_meetings`
- 최소 1개 이상의 verified citation
- `confidence >= config.wiki.confidence_threshold`

### 5.2 상태 모델

| 상태 | 의미 |
|---|---|
| `proposed` | 제안되었으나 확정 표현이 부족함 |
| `decided` | 회의에서 결정/합의/확정됨 |
| `superseded` | 이후 결정으로 대체됨 |
| `rejected` | 논의되었으나 채택하지 않음 |
| `pending` | confidence 또는 citation 검증 미달 |

---

## 6. BM25 도입안

### 6.1 도입 위치

BM25는 기존 transcript RAG를 대체하지 않는다. 먼저 Wiki 전용 인덱스로 도입한다.

신규 모듈:

```text
core/wiki/search_index.py
  WikiSearchIndex
    - rebuild()
    - upsert_page(rel_path)
    - delete_page(rel_path)
    - search(query, page_types, filters, top_k)
```

### 6.2 FTS5 테이블 초안

```sql
CREATE VIRTUAL TABLE wiki_fts
USING fts5(
  page_path,
  page_type,
  title,
  body,
  project,
  participants,
  owners,
  status,
  citations,
  tokenize='unicode61'
);
```

별도 metadata 테이블:

```sql
CREATE TABLE wiki_page_meta (
  page_path TEXT PRIMARY KEY,
  page_type TEXT NOT NULL,
  title TEXT,
  status TEXT,
  project TEXT,
  decision_date TEXT,
  confidence INTEGER,
  last_updated TEXT
);
```

### 6.3 검색 정책

MVP 검색 점수:

```text
score = BM25(title/body/project/participants) + filter boosts
```

필터:

- page_type: MVP는 `decision` 우선
- date range
- project
- participant/owner
- status
- confidence minimum

한국어 보완:

- 동의어 확장 사전 도입
  - 결정: 결정, 확정, 합의, 결론
  - 액션: 액션아이템, 할 일, 담당, TODO
  - 일정: 일정, 마감, 데드라인, 출시일
  - 보류: 보류, 미정, 재논의
- 형태소 분석기는 MVP에서 도입하지 않는다. SQLite FTS5 + 동의어 확장으로 시작한다.

---

## 7. 실행 계획

### Phase 0. 기준선과 안전장치

목표: 현재 상태를 측정 가능하게 만든다.

작업:

- Python 3.11/3.12 가상환경 기준 테스트 실행 문서화
- Wiki 관련 smoke test 세트 정의
- existing RAG 비회귀 테스트 고정
- Decision gold set 포맷 정의

완료 기준:

- wiki disabled/router disabled에서 기존 `/api/chat`, `/api/search` 응답 계약이 유지된다.
- 관련 테스트 목록과 실행 명령이 문서화된다.

### Phase 1. 자동 Decision 생성 경로 복구

목표: 새 회의 완료 시 canonical decision page가 실제 생성된다.

작업:

- `PipelineManager`가 WikiCompiler에 `summary`와 `corrected_result.utterances`를 넘기도록 수정
- `WikiCompiler.run()` 결과에 created/updated/pending/rejected counts를 명확히 반환
- 회의 실제 날짜를 `date.today()`가 아니라 회의 metadata에서 전달
- accepted decision은 verified citation이 없으면 생성하지 않음

완료 기준:

- 샘플 회의 ingest 후 `decisions/*.md`가 생성된다.
- 생성된 decision마다 verified citation이 1개 이상 있다.
- citation timestamp가 viewer deep link로 연결된다.

### Phase 2. Decision 스키마와 dedupe

목표: decision record가 파싱 가능하고 중복/변경을 추적한다.

작업:

- canonical decision frontmatter 스키마 확정
- `DecisionRecord` 내부 모델 추가
- slug/id 생성 규칙 안정화
- 기존 decision과 신규 decision의 중복 병합
- supersedes/superseded_by 수동 또는 semi-auto 연결 기반 추가

완료 기준:

- 같은 결정이 여러 회의에서 반복되어도 중복 페이지가 무한 생성되지 않는다.
- 변경된 결정은 이전 결정과 연결된다.

### Phase 3. Wiki BM25 검색

목표: 사용자가 decision을 빠르게 찾는다.

작업:

- `core/wiki/search_index.py` 추가
- Wiki page write 후 index upsert
- `/api/wiki/search`를 substring scan에서 BM25 검색으로 교체
- 검색 결과에 score, snippet, matched fields, citations 포함
- decision filters 추가

완료 기준:

- "지난달 결정사항", "가격 정책", "A 프로젝트 일정" 같은 질의가 관련 decision을 top-k에 반환한다.
- 기존 substring-only 검색의 한계가 제거된다.

### Phase 4. Decision UI

목표: 채팅 없이도 결정사항을 찾을 수 있다.

작업:

- `/app/wiki/decisions` 또는 Wiki 내 Decisions 탭 추가
- 목록 테이블: 날짜, 제목, 프로젝트, 상태, 관련자, confidence, source
- 필터: 날짜, 프로젝트, 사람, 상태, confidence
- 상세 패널: 결정 내용, 배경, 액션, 근거 citation, 변경 이력
- pending/rejected/health 상태 표시

완료 기준:

- 사용자가 최근 결정사항을 검색/필터링하고, 근거 발화로 이동할 수 있다.

### Phase 5. Durable Backfill

목표: 기존 회의도 Decision Wiki로 복구한다.

작업:

- Wiki backfill job 상태를 SQLite에 저장
- cancel/resume/retry 지원
- Settings UI에 Wiki 백필 패널 추가
- 실패 회의별 error reason 저장
- dry-run으로 예상 decision count 확인

완료 기준:

- 서버 재시작 후에도 백필 진행 상태가 보존된다.
- 실패한 회의만 재시도할 수 있다.

### Phase 6. Decision Chat

목표: 채팅이 Decision Wiki 검색 결과만 근거로 답한다.

작업:

- `HybridChatService`의 first-3-pages 정책 제거
- `WikiSearchService` 결과를 컨텍스트로 사용
- Wiki answer citation preservation check 추가
- RAG/Wiki/Both source를 UI에서 분리 표시
- 라우터는 Wiki BM25 검색 품질 검증 후 활성화

완료 기준:

- Wiki 질문에 무관한 페이지가 답변에 섞이지 않는다.
- 답변이 citation을 누락하면 LLM 답변 대신 검색 결과를 반환한다.

### Phase 7. 평가셋과 품질 게이트

목표: "잘 찾는다"를 수치로 증명한다.

작업:

- 20개 이상 회의 gold set 구축
- expected decisions, expected citations, expected queries 정의
- extraction precision/recall 측정
- citation validity 측정
- search recall@k 측정
- router accuracy 측정
- answer citation coverage 측정

완료 기준:

- accepted decision citation validity: 100%
- decision extraction precision: 90% 이상
- decision extraction recall: 80% 이상
- decision search recall@5: 90% 이상
- phantom citation: 0건

---

## 8. 비범위와 후순위

MVP에서 제외한다.

- 범용 `topics/` 자동 지식 그래프
- 사람별 내러티브 프로필 자동 생성
- 프로젝트 상태 페이지를 독립 산출물로 생성
- LLM 기반 contradiction auto-fix
- 외부 데이터 소스 ingest
- 다중 사용자/권한 모델
- Wiki 직접 편집 UI
- Wiki vector search

후순위로 둘 수 있지만, Decision MVP 이후 가치가 있으면 재검토한다.

- people/project page는 decision aggregation view로 먼저 제공
- vector rerank는 BM25 recall이 부족할 때만 도입
- contradiction lint는 citation/decision schema가 안정된 뒤 도입

---

## 9. 리스크와 대응

| 리스크 | 영향 | 대응 |
|---|---|---|
| 잘못된 결정이 canonical Wiki에 저장됨 | 신뢰 붕괴 | citation 필수, confidence threshold, pending 격리, gold set 평가 |
| 범위가 다시 broad Wiki로 확장됨 | 핵심 가치 희석 | MVP는 `decisions/` 중심으로 고정 |
| Wiki chat이 관련 없는 페이지로 답함 | 사용자 혼란 | BM25 검색 전까지 router/Wiki chat 활성화 보류 |
| 백필 실패가 보이지 않음 | 과거 회의 누락 | durable job state, 실패 reason, retry UI |
| Chroma/RAG와 Wiki가 결합되어 회귀 발생 | 기존 기능 손상 | transcript RAG와 Wiki index 모듈 분리 |
| citation이 timestamp 존재만 확인하고 의미 일치를 놓침 | 근거 품질 저하 | Phase 1은 timestamp 존재, Phase 7에서 quote overlap/entailment 평가 추가 |

---

## 10. 최종 수락 기준

이 개선안은 다음이 모두 참일 때 완료로 본다.

1. 새 회의 처리 후 verified citation을 가진 decision page가 자동 생성된다.
2. 사용자는 UI에서 날짜/프로젝트/사람/상태로 결정사항을 필터링할 수 있다.
3. 사용자는 결정 상세에서 원문 timestamp로 이동할 수 있다.
4. Wiki 검색은 BM25 기반이며 관련 decision을 top-k로 반환한다.
5. Wiki chat은 검색된 decision만 근거로 답하고 citation을 표시한다.
6. 기존 RAG 채팅은 Wiki 비활성 시 동작이 바뀌지 않는다.
7. 기존 회의 백필은 재시작 후에도 상태가 보존되고 실패만 재시도할 수 있다.
8. gold set 기준 accepted decision의 phantom citation은 0건이다.

---

## 11. 한 문장 제품 정의

Recap의 Decision Wiki는 회의 전사를 단순히 저장하는 기능이 아니라, 회의에서 나온 의사결정을 검증 가능한 근거와 함께 축적해 사용자가 언제든 "무엇이 결정됐고 왜 그런지"를 찾게 하는 로컬 의사결정 기억 시스템이다.

---

## 12. 구현 중 범위 밖으로 분리한 TODO

MVP 목적과 직접 연결되지 않는 broad Wiki 확장은 이번 구현 범위에서 제외한다.

- Topic/People/Project narrative 자동 확장: Decision 목록의 보조 필터로 충분해진 뒤 재검토한다.
- 의미 기반 contradiction/entailment 검증: citation 존재성 검증과 DecisionRecord 스키마가 안정된 뒤 별도 평가 세트로 도입한다.
- 형태소 분석기 기반 한국어 검색: FTS5 prefix/BM25 recall 지표가 부족할 때만 추가 의존성을 검토한다.
- Wiki vector rerank: BM25 top-k 실패 케이스가 실제 gold set에서 확인될 때 도입한다.
