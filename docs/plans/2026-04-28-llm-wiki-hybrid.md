# LLM Wiki × RAG 하이브리드 — 회의 누적 지식 시스템 설계

**작성일**: 2026-04-28
**상태**: 기획 (Phase 1 시작 전)
**작성자**: youngouk + Claude (Opus 4.7)
**관련 PR**: 없음 (신규)
**선행 의존성**: PR #20 (회의록 발화별 음성 재생) — 인용 검증 UX 기반

---

## 0. TL;DR

Andrej Karpathy 가 2026-04-03 공개한 **LLM Wiki 패턴**(영속 마크다운 지식 베이스로
RAG 합성 결과를 누적)을 우리 시스템의 기존 RAG 위에 **추가층(additive layer)** 으로
도입한다. 기존 RAG(ChromaDB + FTS5 + e5-small)는 **무변경**으로 유지하고, Wiki는
회의 ingest 후 자동 컴파일되는 9번째 파이프라인 단계로 동작한다.

LLM Wiki 의 본질적 한계인 **환각 누적**과 **출처 추적성 손실**을 막기 위해 사용자
승인 없이 자동 운영하되 **5중 자동 방어**(인용 강제 + 인용 실재성 검증 + confidence
임계 + 자동 lint + git 자동 커밋) 를 적용한다. Wiki 컴파일은 한국어 고유명사 정확성을
위해 **EXAONE 3.5** 를 사용하고(일반 회의 요약은 Gemma 4 그대로), 회의록의
화자/타임스탬프 정보를 활용해 **모든 위키 문장에 `[meeting:id@HH:MM:SS]` 인용을
강제**한다 — PR #20 의 음성 재생과 결합되어 위키 주장을 클릭 한 번으로 원본
음성으로 검증할 수 있다.

Phase 1~4 를 순차 구현하고 (자동 운영 + 사용자 검토 0), Phase 5(질의 라우팅)는
운영 데이터로 재평가한다.

---

## 1. 배경 및 동기

### 1.1 Karpathy 의 문제 제기 (LLM Wiki Gist, 2026-04-03)

> *"LLM 이 매 질문마다 처음부터 지식을 재발견(rediscover)하고 있다. 축적이 없다."*

전형적인 RAG 는 매 질의마다 임베딩 검색 → 청크 회수 → 컨텍스트 조립 → 답변 생성을
반복하지만, **합성(synthesis) 결과 자체는 어디에도 누적되지 않는다**. 같은 주제를
100 번 질문하면 100 번 같은 합성을 수행한다.

Karpathy 가 제안한 LLM Wiki 는 합성 결과를 **영속적 마크다운 페이지로 컴파일**해
다음 질의가 그 위에 올라타게 한다. 1,600만 뷰를 기록한 GitHub Gist 가 시발점.

### 1.2 우리 시스템의 현재 RAG 구조

```
파이프라인 8단계 (core/pipeline.py):
오디오 → STT(mlx-whisper) → 화자분리(pyannote, CPU) → 병합 → LLM 교정
       → 청킹 → 임베딩(e5-small) → ChromaDB+FTS5 저장 → AI 요약

검색 (search/hybrid_search.py):
  - 벡터(0.6) + FTS5(0.4) + RRF (k=60)
  - 회의 단위 청크 회수에 최적화

채팅 (search/chat.py):
  - 검색 → 컨텍스트 조립 → LLM 답변
  - 매 질의마다 합성 반복 (누적 없음)
```

### 1.3 RAG 의 구조적 한계 — 우리 도메인에서

회의 전사 도메인은 **누적성**(compounding)이 가치를 만드는 영역이다:

| 사용자 질의 패턴 | RAG 단독 | Wiki 가 추가될 때 |
|---|---|---|
| "이번 회의 뭐 얘기했어?" | ✅ 강함 (회의 단위 검색) | 동일 |
| "지난 3개월간 결정사항 정리해줘" | ⚠️ 매번 재합성 (느림 + 일관성 ↓) | ✅ `decisions/*.md` 즉시 답변 |
| "철수가 최근 결정한 것들?" | ⚠️ 화자 필터 + 검색 + 합성 매번 | ✅ `people/철수.md` 즉시 |
| "프로젝트 X 진행 상황?" | ⚠️ 키워드 검색 후 합성 | ✅ `projects/x.md` 즉시 |
| "정확히 누가 X 라고 말했어?" | ✅ 강함 (정확 인용) | RAG 그대로 사용 |
| 액션아이템 트래킹 | ❌ 안됨 (state 없음) | ✅ `action_items.md` open/closed |

**핵심 통찰**: 우리는 RAG 를 *대체* 하는 게 아니라 RAG 가 못하는 영역(누적·상태·교차회의 합성)을 *보완* 한다.

### 1.4 LLM Wiki 의 본질적 위험 (한계 검토 결과)

이전 리서치에서 식별한 6가지 한계 중 우리에게 가장 치명적인 것:

| 한계 | 위험도 | 우리 시스템에서 |
|---|---|---|
| **환각 누적** | 🔴 치명 | 잘못된 합성이 영구화 → 미래 질의 오염 |
| **출처 추적성 손실** | 🔴 치명 | 회의 인용 깨지면 신뢰 붕괴 |
| 컨텍스트 윈도우 천장 | 🟡 중간 | 회의 100건 ≈ 인덱스 50KB, 한동안 안전 |
| 동시성 | 🟢 낮음 | `single_instance.py` 로 회피 |
| 거버넌스/보안 | 🟢 낮음 | 100% 로컬 단일 사용자 |
| 운영 비용 | 🟢 낮음 | MLX 로컬, 비용 0 |

→ **빨간색 두 개를 막는 것이 본 설계의 최우선 과제**.

### 1.5 우리 시스템이 가진 차별적 이점

다른 LLM Wiki 구현이 못 가지는 자산:

1. **화자 정보 자동화** (pyannote) — `people/*` 페이지 자동 생성 가능
2. **타임스탬프 자연 인용** — 모든 발화에 `HH:MM:SS` 부여, 인용 형식 `[meeting:id@HH:MM:SS]` 가 1차 데이터에서 직접 도출됨
3. **이미 존재하는 검증 인프라** — FTS5/ChromaDB 가 인용 실재성 검증을 무료로 제공
4. **PR #20 의 ▶ 음성 재생** — 위키 주장을 클릭하면 원본 음성 즉시 재생
   → **환각 의심을 사용자가 1초 안에 해소**할 수 있는 신뢰성 부스터.
   이 점이 본 설계를 가능하게 만든 결정적 이유.

---

## 2. 목표

### 2.1 In-Scope

1. WikiCompiler 9단계 신설 — 회의 ingest 후 자동 실행
2. Wiki 저장소 (`~/.meeting-transcriber/wiki/`) git 관리
3. 5종 페이지 자동 생성/유지: `decisions/`, `action_items.md`, `people/`, `projects/`, `topics/`
4. 환각 누적 방지 5중 자동 방어
5. 자동 lint (5회의마다, 결과를 `HEALTH.md` 에 기록)
6. Wiki 뷰어 UI (`/app/wiki`) — 마크다운 렌더링 + 검색
7. 인용 클릭 시 원본 회의 viewer 의 해당 발화로 점프 (PR #20 ▶ 자동 재생 연동)
8. EXAONE 으로 위키 컴파일 (한국어 고유명사 정확성)
9. 백필 스크립트 — 기존 회의들 일괄 위키화

### 2.2 Out-of-Scope (Phase 5 또는 후속)

- **질의 라우팅** (Wiki vs RAG 자동 선택) — Phase 4 완료 후 사용 데이터로 재평가
- 다중 사용자/팀 모드
- Wiki 페이지 사용자 직접 편집 UI (LLM 만 수정)
- 외부 데이터 소스 ingest (Slack, 이메일 등)
- 위키 주제 클러스터링/그래프 뷰
- Marp 슬라이드 생성

### 2.3 비목표 (영원히 안 함)

- 외부 API 호출 (100% 로컬 원칙 유지)
- 환각 자동 수정 (lint 는 보고만, 수정은 차회 ingest 에서 LLM 이 자연스럽게 처리)
- 사용자 검토 의무화 (자동 운영 정책 결정)

---

## 3. 아키텍처

### 3.1 4계층 다이어그램

```
┌─────────────────────────────────────────────────────────────────┐
│ Layer 4 — Schema                                                │
│   ~/.meeting-transcriber/wiki/CLAUDE.md                         │
│   • 위키 작성 규칙                                              │
│   • 페이지 템플릿 (decisions/people/projects/...)               │
│   • 인용 형식 표준 [meeting:id@HH:MM:SS]                        │
│   • LLM 시스템 프롬프트                                         │
└─────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────┐
│ Layer 3 — Wiki (신규, git repo)                                │
│   ~/.meeting-transcriber/wiki/                                  │
│   ├── index.md          한 줄 카탈로그                          │
│   ├── log.md            ingest/lint 시간순 로그                 │
│   ├── HEALTH.md         최근 lint 결과 (모순/고아/순환)         │
│   ├── decisions/        YYYY-MM-DD-{slug}.md                    │
│   ├── people/           {name}.md                               │
│   ├── projects/         {slug}.md                               │
│   ├── topics/           {concept}.md                            │
│   ├── action_items.md   open/closed 통합                        │
│   └── pending/          confidence 미달 격리 페이지             │
└─────────────────────────────────────────────────────────────────┘
                            ▲
                            │ WikiCompiler (9단계, EXAONE)
                            │
┌─────────────────────────────────────────────────────────────────┐
│ Layer 2 — RAG (기존, 무변경)                                   │
│   • ChromaDB (벡터 0.6)                                         │
│   • FTS5 (키워드 0.4)                                           │
│   • RRF k=60                                                    │
│   • search/hybrid_search.py / search/chat.py                    │
│   • /api/chat /api/search 엔드포인트                            │
│   • 기존 채팅 UI                                                │
└─────────────────────────────────────────────────────────────────┘
                            ▲
                            │ Embedder (7단계, e5-small)
                            │
┌─────────────────────────────────────────────────────────────────┐
│ Layer 1 — Raw (변경 없음)                                       │
│   오디오 → 전사 → 화자분리 → 병합 → 교정 결과                   │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 데이터 흐름 (회의 ingest 시)

```
1. 사용자가 오디오 추가
2. 파이프라인 1~7 단계 (기존, 무변경)
3. Summarizer (8단계) — Gemma 4 로 요약 (기존, 무변경)
4. WikiCompiler (9단계, 신규)
   ├─ 4.1 컨텍스트 수집
   │   • 요약 (8단계 결과)
   │   • 발화 목록 with [meeting_id, speaker, start, end, text]
   │   • 기존 index.md (관련 페이지 식별용)
   │
   ├─ 4.2 영향 페이지 결정 (EXAONE)
   │   "이 회의는 다음 페이지를 갱신해야 한다: people/철수, projects/x..."
   │   index.md 검색 + 새 페이지 후보 제안
   │
   ├─ 4.3 페이지별 갱신 (EXAONE, 페이지마다 1회 호출)
   │   각 페이지마다:
   │     기존 페이지 + 회의 컨텍스트 → 갱신된 페이지
   │     출력에 인용 마커 강제
   │
   ├─ 4.4 5중 방어 검증
   │   D1: 인용 강제 후처리
   │   D2: 인용 실재성 RAG 검증
   │   D3: confidence 임계 (≥7)
   │   D4: (5회의마다) 자동 lint
   │   D5: git 단일 커밋
   │
   └─ 4.5 결과 저장
       성공: wiki/ 직접 갱신 + git commit
       confidence 미달: wiki/pending/ 로 격리
       인용 실재성 실패: 페이지 갱신 롤백 + log.md 에 기록
```

### 3.3 데이터 흐름 (사용자 질의 시 — Phase 5 없음)

```
사용자
  ├─ "/app" 채팅      → 기존 RAG (변경 없음)
  └─ "/app/wiki" 뷰   → Wiki 마크다운 직접 렌더 + 검색
                          ↓
                        인용 클릭
                          ↓
                        /app/viewer/{id}#t=HH:MM:SS
                          ↓
                        ▶ 자동 재생 (PR #20)
```

질의 라우팅(Phase 5)이 없어도 사용자가 두 뷰를 명시적으로 선택할 수 있으므로 기능
완결성 확보.

---

## 4. Wiki 저장소 구조

### 4.1 디렉토리 레이아웃

```
~/.meeting-transcriber/wiki/             ← git repo
├── CLAUDE.md                            ← 스키마 (LLM 시스템 프롬프트)
├── index.md                             ← 카탈로그
├── log.md                               ← 시간순 로그
├── HEALTH.md                            ← 최근 lint 결과
│
├── decisions/                           ← 결정사항 (날짜 단위)
│   ├── 2026-04-15-launch-date.md
│   ├── 2026-04-20-vendor-selection.md
│   └── ...
│
├── people/                              ← 인물 (화자별)
│   ├── 철수.md
│   ├── 영희.md
│   └── ...
│
├── projects/                            ← 프로젝트 (slug)
│   ├── new-onboarding.md
│   ├── q3-launch.md
│   └── ...
│
├── topics/                              ← 반복 개념
│   ├── pricing-strategy.md
│   └── ...
│
├── action_items.md                      ← 통합 (open/closed)
│
└── pending/                             ← confidence 미달 격리
    ├── 2026-04-25-doubt-1.md
    └── ...
```

### 4.2 페이지 템플릿

#### `decisions/YYYY-MM-DD-{slug}.md`

```markdown
---
type: decision
date: 2026-04-15
meeting_id: abc123
status: confirmed | superseded
participants: [철수, 영희]
projects: [new-onboarding]
confidence: 9
created_at: 2026-04-15T10:30:00+09:00
updated_at: 2026-04-15T10:30:00+09:00
---

# 신규 온보딩 출시일을 5월 1일로 확정

## 결정 내용
신규 온보딩 기능을 2026-05-01 에 출시하기로 합의 [meeting:abc123@00:23:45].

## 배경
QA 가 5일 추가 필요하다고 보고 [meeting:abc123@00:18:30].
마케팅은 4월 25일을 선호했으나 품질 우선으로 양보 [meeting:abc123@00:21:10].

## 후속 액션
- [ ] 철수: 5월 1일 출시 캘린더 갱신 [meeting:abc123@00:25:12]
- [ ] 영희: 마케팅팀에 일정 변경 공유 [meeting:abc123@00:25:50]

## 참고 회의
- [abc123 — 2026-04-15 주간 PM 미팅](../../../app/viewer/abc123)
```

#### `people/{name}.md`

```markdown
---
type: person
name: 철수
role: PM
first_seen: 2026-04-15
last_seen: 2026-04-22
meetings_count: 8
---

# 철수 (PM)

## 최근 결정 (latest 5)
- 2026-04-22: API v2 마이그레이션 보류 [decisions/2026-04-22-api-v2.md]
- 2026-04-15: 신규 온보딩 출시일 5/1 확정 [decisions/2026-04-15-launch-date.md]
- ...

## 담당 프로젝트
- [new-onboarding](../projects/new-onboarding.md)
- [q3-launch](../projects/q3-launch.md)

## 자주 언급하는 주제
- pricing-strategy [meeting:abc123@00:30:11], [meeting:def456@00:15:22]
- ...

## 미해결 액션아이템
- [ ] 5월 1일 출시 캘린더 갱신 (from 2026-04-15)
```

#### `projects/{slug}.md`

```markdown
---
type: project
slug: new-onboarding
status: in-progress | blocked | shipped | cancelled
owner: 철수
started: 2026-03-01
target: 2026-05-01
last_updated: 2026-04-22
---

# 신규 온보딩 (new-onboarding)

## 현재 상태
**in-progress** — 5월 1일 출시 확정. QA 진행 중 [meeting:abc123@00:23:45].

## 최근 결정사항 (latest 3)
- 2026-04-22: API v2 마이그레이션 보류 → 출시 후 진행 [meeting:def456@00:10:00]
- 2026-04-15: 출시일 5/1 확정 [meeting:abc123@00:23:45]

## 진행 타임라인
- 2026-03-01: 프로젝트 시작 [meeting:001@00:05:00]
- 2026-04-01: MVP 완성 [meeting:200@00:10:30]
- 2026-04-15: QA 시작 [meeting:abc123@00:18:30]

## 미해결 이슈
- 결제 모듈 통합 일정 미정 [meeting:def456@00:25:00]

## 참여자
- 철수 (PM), 영희 (Eng Lead), ...
```

#### `action_items.md`

```markdown
---
type: action_items
last_compiled: 2026-04-28T14:00:00+09:00
---

# 액션아이템 통합

## Open (5)

### 철수
- [ ] 5월 1일 출시 캘린더 갱신
  - From: [meeting:abc123@00:25:12] (2026-04-15)
  - Due: 2026-04-30
  - Project: new-onboarding

### 영희
- [ ] 마케팅팀에 일정 변경 공유 [meeting:abc123@00:25:50]
- [ ] API v2 보류 결정을 외부 파트너에 통보 [meeting:def456@00:30:15]

## Closed (12)

### 2026-04-22 closed
- [x] ~~MVP 데모 자료 작성~~ [meeting:def456@00:00:30]
  - From: [meeting:abc123@01:00:00] (2026-04-15)
  - Closed by: 영희 가 데모 완료 보고 [meeting:def456@00:00:30]

...
```

#### `index.md` (자동 갱신)

```markdown
# Wiki 인덱스

총 페이지: 47 / 회의: 23 / 마지막 갱신: 2026-04-28T14:00

## Decisions (15)
- 2026-04-22-api-v2-hold: API v2 마이그레이션 보류
- 2026-04-15-launch-date: 신규 온보딩 출시일 5/1 확정
- ...

## People (8)
- 철수 (PM, 8회): 최근 — 출시 일정 결정
- 영희 (Eng Lead, 6회): 최근 — API v2 보류 동의
- ...

## Projects (4)
- new-onboarding (in-progress): 5/1 출시
- q3-launch (planning): 7월 시작 예정
- ...

## Topics (12)
- pricing-strategy (5회 언급)
- ...

## Action Items
- Open: 5 / Closed: 12 — [전체 보기](./action_items.md)
```

#### `log.md` (append-only)

```markdown
# Wiki 운영 로그

## 2026-04-28
- [14:00:32] ingest meeting:abc123 → updated: people/철수.md, projects/new-onboarding.md, decisions/2026-04-15-launch-date.md, action_items.md (4 pages, confidence avg 8.5)
- [13:45:10] lint pass → 0 contradictions, 1 orphan (people/김씨), 0 cyclic citations
- [13:30:00] ingest meeting:xyz789 → updated: ... (skipped: 2 pages, confidence 5)

## 2026-04-27
- ...
```

#### `HEALTH.md` (lint 결과)

```markdown
# 위키 건강 보고서

마지막 lint: 2026-04-28T13:45:10 (5회의 주기)

## ✅ 통과
- 0 contradictions
- 0 cyclic citations
- 100% citations have valid timestamps

## ⚠️ 주의
- 1 orphan: people/김씨 (incoming links: 0)
  - 자동 수정 안 함. 다음 ingest 에서 LLM 이 자연스럽게 처리하거나
    사용자가 직접 삭제 권장.

## 📊 통계
- 총 페이지: 47
- 인용 수: 312
- 인용 검증 통과율: 99.7% (1건 RAG 회수 실패 — log.md 참조)
```

### 4.3 인용 형식 표준 (CLAUDE.md 에 정의)

**필수 형식**:
```
[meeting:{meeting_id}@{HH:MM:SS}]
```

**예시**:
```
[meeting:abc123@00:23:45]
```

**규칙**:
1. 모든 사실 진술 문장은 인용 마커 1개 이상 포함
2. 한 문장에 여러 출처 → 콤마 구분: `[m:abc@00:01:00], [m:abc@00:02:30]`
3. `meeting_id` 는 8자리 hex (DB 와 일치)
4. timestamp 는 발화 시작 시각 (`utterance.start`)
5. 페이지 간 링크는 상대경로: `[../people/철수.md]`
6. 제목/메타데이터/타임라인 항목은 인용 면제 (가능한 곳은 포함 권장)

**검증 정규식** (D1 인용 강제 후처리):
```python
CITATION_PATTERN = r'\[meeting:[a-f0-9]{8}@\d{2}:\d{2}:\d{2}\]'
PAGE_LINK_PATTERN = r'\[\.\./[a-z_]+/[^\]]+\.md\]'
```

---

## 5. WikiCompiler — 9단계 통합 설계

### 5.1 호출 위치

```python
# core/pipeline.py
async def run_pipeline(audio_path):
    # 1~7 단계 (기존)
    ...
    summary = await summarizer.run(...)  # 8단계, Gemma 4

    # 9단계 (신규)
    if config.wiki.enabled:
        await wiki_compiler.run(
            meeting_id=meeting_id,
            summary=summary,
            utterances=corrected_utterances,  # 5단계 결과 재사용
        )
```

### 5.2 알고리즘 (의사코드)

```python
# steps/wiki_compiler.py
class WikiCompiler:
    def __init__(self, config, model_manager):
        self.wiki_root = Path(config.wiki.root)  # ~/.meeting-transcriber/wiki/
        self.llm = None  # EXAONE — 4.3 에서 lazy load
        self.rag = HybridSearch(...)  # 인용 검증용
        self.guard = WikiGuard(...)   # 5중 방어

    async def run(self, meeting_id, summary, utterances):
        # 4.1 컨텍스트 수집
        index_md = self.wiki_root / "index.md"
        ctx = self._build_context(meeting_id, summary, utterances, index_md)

        # 4.2 영향 페이지 결정
        # ModelLoadManager 로 EXAONE 로드 (Gemma 언로드 후)
        async with self.model_manager.acquire("exaone") as llm:
            self.llm = llm
            target_pages = await self._decide_pages(ctx)
            # 결과: [{path: "people/철수.md", reason: "..."}, ...]

            # 4.3 페이지별 갱신
            updates = []
            for target in target_pages:
                old_content = self._read_page(target.path)
                new_content, confidence = await self._update_page(
                    target.path, old_content, ctx
                )
                updates.append((target.path, new_content, confidence))

        # 4.4 5중 방어 (LLM 외부)
        verified_updates = []
        for path, content, conf in updates:
            verdict = await self.guard.verify(
                path=path,
                content=content,
                confidence=conf,
                meeting_id=meeting_id,
                utterances=utterances,
            )
            if verdict.passed:
                verified_updates.append((path, content))
            elif verdict.reason == "low_confidence":
                self._save_pending(path, content, conf, meeting_id)
            else:
                self._log_rejection(path, verdict.reason)

        # 4.5 저장 + git commit
        for path, content in verified_updates:
            self._write_page(path, content)
        self._update_index()
        self._append_log(meeting_id, verified_updates)
        self._git_commit_atomic(meeting_id)

        # 5회의마다 lint
        if self._should_lint():
            await self.lint()
```

### 5.3 영향 페이지 결정 — `_decide_pages`

EXAONE 에 다음 프롬프트 (요약):

```
회의 요약 + 화자 목록 + 기존 index.md 가 주어진다.
다음 형식으로 갱신할 페이지 목록을 출력하라:

{
  "decisions": [
    {"new_page": "decisions/2026-04-15-launch-date.md",
     "reason": "신규 온보딩 출시일 결정"}
  ],
  "people": [
    {"path": "people/철수.md", "reason": "결정 발언자"},
    {"path": "people/영희.md", "reason": "참여자"}
  ],
  "projects": [
    {"path": "projects/new-onboarding.md", "reason": "출시일 변경"}
  ],
  "action_items": true,
  "topics": []
}

규칙:
- 새 페이지는 명확한 결정/인물/프로젝트일 때만 생성
- 모호하면 생성하지 말고 빈 배열 반환
- topics 는 3회 이상 반복 등장한 개념만
```

출력 검증: JSON 파싱 실패 시 1회 재시도 후 페이지 갱신 자체 스킵 (보수적).

### 5.4 페이지 갱신 — `_update_page`

페이지마다 별도 LLM 호출. 프롬프트 (요약):

```
[기존 페이지 내용]
{old_content}

[이 회의의 컨텍스트]
- meeting_id: abc123
- 요약: ...
- 관련 발화 (timestamp 포함):
  [00:18:30] 철수: QA 가 5일 더 필요하다고 합니다
  [00:23:45] 철수: 그럼 5월 1일 출시로 가시죠
  ...

[작업]
이 페이지를 회의 내용을 반영하여 갱신하라.

규칙:
1. 모든 사실 문장은 [meeting:abc123@HH:MM:SS] 인용 필수
2. 인용 없는 문장은 출력 금지
3. 기존 사실은 보존, 새 사실만 추가
4. 모순 발견 시 새 사실 우선, 옛 사실은 ~~취소선~~ + 인용 유지
5. 마지막 줄에 confidence 점수 (0~10):
   "<!-- confidence: 8 -->"
6. 한국어 고유명사에 영어/중국어 병기 절대 금지
   (예: "배미령(Baimilong)" ❌ → "배미령" ✅)
```

### 5.5 모델 분리 — Gemma vs EXAONE

| 단계 | 모델 | 이유 |
|---|---|---|
| 8 — Summarizer | Gemma 4 E4B | 사용자 결정 (다국어, Thinking 모드) |
| 9 — WikiCompiler | EXAONE 3.5 7.8B | 한국어 고유명사 정확성 |

**메모리 관리** (ModelLoadManager 활용):
```python
# 8단계 끝나고 9단계 시작 전
await model_manager.unload("gemma")  # 9.1
await model_manager.load("exaone")   # 9.2
# 9단계 내부 모든 LLM 호출은 EXAONE
await model_manager.unload("exaone") # 9 끝
```

피크 RAM 영향: 동시 로드 없음 (기존 규칙 준수). 추가 시간 ~15초 (모델 스왑).

설정:
```yaml
# config.yaml
wiki:
  enabled: true
  root: "~/.meeting-transcriber/wiki/"
  compiler_model: "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit"
  lint_interval: 5  # N 회의마다
  confidence_threshold: 7
```

---

## 6. 환각 누적 방지 5중 방어 (WikiGuard)

`steps/wiki_guard.py` 에 구현. LLM 출력 후, 디스크 쓰기 전 모든 검증을 통과해야 한다.

### D1. 인용 강제 후처리

```python
def enforce_citations(content: str, meeting_id: str) -> tuple[str, list[str]]:
    """
    인용 마커 없는 사실 문장을 자동 제거.
    제목/리스트 메타/링크 줄은 면제.
    """
    REJECTED_LINES = []
    output_lines = []
    for line in content.split("\n"):
        if _is_meta_line(line):  # # 제목, --- frontmatter, [링크]
            output_lines.append(line)
            continue
        if _is_factual_statement(line) and not CITATION_PATTERN.search(line):
            REJECTED_LINES.append(line)
            continue  # drop
        output_lines.append(line)

    if len(REJECTED_LINES) > len(output_lines) * 0.3:
        # 30% 이상 거부되면 페이지 갱신 자체 무효
        raise WikiGuardError("too_many_uncited_statements")

    return "\n".join(output_lines), REJECTED_LINES
```

### D2. 인용 실재성 RAG 검증

```python
async def verify_citations(content: str, rag: HybridSearch) -> bool:
    """
    각 인용을 RAG 에 질의해 timestamp 가 실제 발화와 일치하는지 확인.
    """
    citations = CITATION_PATTERN.findall(content)
    for cite in citations:
        meeting_id, ts = _parse_citation(cite)
        # ChromaDB 메타데이터에서 직접 조회 (검색 아닌 정확 매칭)
        utterance = rag.get_utterance_at(meeting_id, ts, tolerance_sec=2)
        if utterance is None:
            logger.error(f"phantom_citation: {cite}")
            return False
    return True
```

### D3. Confidence 임계치

LLM 이 출력한 `<!-- confidence: N -->` 추출:

```python
def check_confidence(content: str, threshold: int = 7) -> tuple[bool, int]:
    match = re.search(r'<!--\s*confidence:\s*(\d+)\s*-->', content)
    if not match:
        return False, 0  # 명시 없으면 거부
    score = int(match.group(1))
    return score >= threshold, score
```

미달 페이지는 `wiki/pending/{date}-{slug}.md` 에 격리 저장. 사용자가 정기적으로
확인할 수는 있으나 본 위키에는 영향 없음.

### D4. 자동 lint (5회의 주기)

```python
async def lint(self):
    """
    위키 전체 건강 검진. 발견만 하고 자동 수정은 안 함.
    """
    health = HealthReport()

    # 1) 모순 탐지
    for page in self._all_pages():
        contradictions = await self._find_contradictions(page)
        health.contradictions.extend(contradictions)

    # 2) 고아 페이지 (incoming links 0)
    health.orphans = self._find_orphans()

    # 3) 순환 인용
    health.cyclic = self._find_cyclic_links()

    # 4) 인용 검증 통과율 (D2 재실행)
    health.citation_pass_rate = await self._reverify_all_citations()

    # 결과를 HEALTH.md 에 저장
    self._write_health(health)
    self._append_log(f"lint pass → {health.summary()}")
```

### D5. Git 자동 커밋 + 롤백

```python
def git_commit_atomic(self, meeting_id: str):
    """
    매 ingest 를 단일 커밋으로 → git revert <sha> 로 즉시 롤백.
    """
    repo = self.wiki_root
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run([
        "git", "-C", repo, "commit", "-m",
        f"기능: ingest {meeting_id} ({len(self._touched_pages)} 페이지)"
    ], check=True)
```

`wiki/` 가 git repo 가 아니면 자동 `git init`. 사용자는 `git log` 로 모든 변경
이력을 볼 수 있다.

### 5중 방어 요약 표

| # | 시점 | 차단 대상 | 실패 시 동작 |
|---|---|---|---|
| D1 | LLM 출력 직후 | 인용 없는 문장 | 30% 이상 거부 → 페이지 무효 |
| D2 | D1 통과 후 | 가짜 timestamp 인용 | 페이지 무효 |
| D3 | D2 통과 후 | 저신뢰 합성 | `pending/` 격리 |
| D4 | 5회의마다 | 누적된 모순/고아 | `HEALTH.md` 보고만 |
| D5 | 모든 변경 | 잘못된 ingest | `git revert` 로 롤백 |

---

## 7. API 설계

### 7.1 신규 엔드포인트

```python
# api/routes.py

# 위키 페이지 목록
GET /api/wiki/pages
  → [{path, type, title, last_updated}, ...]

# 단일 페이지 (마크다운 raw)
GET /api/wiki/pages/{type}/{slug}
  → {content: "...", frontmatter: {...}, citations: [...]}

# 위키 검색 (FTS over markdown)
GET /api/wiki/search?q=...
  → [{path, snippet, score}, ...]

# 위키 헬스 (HEALTH.md raw)
GET /api/wiki/health
  → {last_lint_at, contradictions, orphans, ...}

# 위키 로그 (log.md tail)
GET /api/wiki/log?limit=50
  → [{timestamp, action, details}, ...]

# 수동 lint 트리거
POST /api/wiki/lint
  → {status: "started" | "running"}

# 백필 (기존 회의들 일괄 위키화)
POST /api/wiki/backfill
  body: {since: "2026-01-01", until: "2026-04-28"}
  → {meetings_to_process: 23, job_id: "..."}

# 백필 진행 상태
GET /api/wiki/backfill/{job_id}
  → {processed: 5, total: 23, current: "abc123", errors: []}
```

### 7.2 변경 없는 엔드포인트

`/api/chat`, `/api/search`, `/api/meetings/*` 모두 무변경.

---

## 8. UI 설계

### 8.1 신규 뷰 — `/app/wiki`

기존 SPA 에 ViewerView 와 같은 패턴으로 `WikiView` 추가.

**레이아웃**:
```
┌──────────┬───────────────────────────────────────────────┐
│ Sidebar  │ Wiki View                                     │
│ (기존)   ├───────────────────────────────────────────────┤
│          │ [Search input  ──────────────] [⚙ Health]    │
│          ├──────────┬────────────────────────────────────┤
│          │          │                                    │
│          │ Tree     │  Markdown Preview                  │
│          │  ▾ Decis │                                    │
│          │    2026  │  # 신규 온보딩 출시일을 5/1로...  │
│          │    ...   │                                    │
│          │  ▾ Peopl │  ## 결정 내용                     │
│          │    철수  │  신규 온보딩 기능을 [m:abc@00:23] │
│          │    ...   │   * 클릭 시 → /app/viewer/abc#23  │
│          │  ▾ Proj  │                                    │
│          │    ...   │                                    │
│          │  ⚠ Heal  │                                    │
│          │  📜 Log  │                                    │
│          └──────────┴────────────────────────────────────┘
```

**핵심 인터랙션**:
1. 인용 마커 `[meeting:abc@00:23:45]` 클릭 → 새 탭으로 `/app/viewer/abc?t=00:23:45`
2. PR #20 의 viewer 가 `?t=` 쿼리 파라미터를 읽어 해당 발화로 스크롤 + ▶ 자동 재생
3. 페이지 트리는 카테고리별 접기/펼치기
4. Health 배지: 🟢 (모순 0) / 🟡 (고아 있음) / 🔴 (인용 검증 실패)

### 8.2 기존 채팅 UI — 무변경

`/app` (채팅) 은 그대로. 다만 Phase 5 에서 라우터를 도입할 경우의 진입점으로
사이드바에 "📚 Wiki" 링크만 추가.

### 8.3 사이드바 추가

```
┌─────────────┐
│ 메뉴         │
│             │
│ 🏠 홈        │  → /app
│ 💬 AI 채팅   │  → /app (기존)
│ 📚 위키     │  → /app/wiki    ← 신규
│ ⚙  설정     │  → /app/settings
└─────────────┘
```

---

## 9. Phase 별 구현 계획

### Phase 1 — 기반 (1주)

**목표**: WikiCompiler 골격 + 빈 위키 생성 + 9단계 통합

**작업**:
1. `core/wiki/` 모듈 신설
   - `wiki/store.py` — 디렉토리 + git 관리
   - `wiki/schema.py` — `CLAUDE.md` 자동 생성
   - `wiki/citations.py` — 인용 파서/검증
2. `steps/wiki_compiler.py` — 골격 (실제 LLM 호출 없이 dry-run)
3. `core/pipeline.py` — 9단계 호출 추가 (config 로 on/off)
4. `config.py` — `WikiConfig` 추가
5. `api/routes.py` — `GET /api/wiki/pages` (빈 목록 응답)
6. 통합 테스트 — 회의 ingest → 빈 위키 + log.md 생성 확인

**검증 지표**:
- 회의 1건 ingest 후 `wiki/log.md` 에 기록 1줄
- `wiki/.git/` 디렉토리 자동 생성
- 기존 RAG/채팅 영향 0

**예상 LOC**: +800

### Phase 2 — Decisions + Action Items (P0, 1.5주)

**목표**: 가장 가치 큰 2개 페이지 종류 자동 생성

**작업**:
1. `wiki/templates/decisions.md` 템플릿
2. `wiki/templates/action_items.md` 템플릿
3. WikiCompiler 의 `_decide_pages` 구현 (decisions/action_items 만)
4. `_update_page` 구현 + 5중 방어 D1, D2, D3, D5 적용
5. EXAONE 모델 통합 (`core/model_manager.py` 의 acquire/release 패턴)
6. `api/routes.py` — `GET /api/wiki/pages/{type}/{slug}` + `GET /api/wiki/search`
7. SPA 에 `WikiView` (decisions + action_items 만 보이는 미니 버전)
8. 단위 테스트 100건 (인용 파싱, D1~D5 각각)
9. 통합 테스트 — 5회의 ingest → 정확한 결정사항/액션아이템 누적 확인

**검증 지표**:
- 10건 회의 ingest 후 사용자 수동 검증 (정확도 ≥80%)
- 인용 검증 통과율 ≥99%
- 페이지 갱신당 평균 시간 ≤90초

**예상 LOC**: +1500

### Phase 3 — People + Projects (P1, 1주)

**목표**: 인물/프로젝트별 페이지 자동 누적

**작업**:
1. `wiki/templates/people.md` 템플릿
2. `wiki/templates/projects.md` 템플릿
3. WikiCompiler 의 `_decide_pages` 확장 (4종 페이지 모두)
4. WikiView UI 확장 (4종 카테고리 표시)
5. A/B 검증 — 같은 질의를 RAG 단독 vs Wiki 결합으로 답변 → 사용자 선호도

**검증 지표**:
- "철수가 최근 결정한 것들" 같은 질의에서 Wiki 답변 정확도 ≥85%
- People 페이지 모순률 ≤5%

**예상 LOC**: +600

### Phase 4 — Lint + Topics + 백필 (1.5주)

**목표**: 운영 자동화 + 기존 회의 위키화

**작업**:
1. `wiki/lint.py` — D4 자동 lint 구현
2. `wiki/templates/topics.md` 템플릿 + 3회 등장 임계
3. `HEALTH.md` 자동 갱신
4. 백필 스크립트 — 기존 N건 회의 일괄 위키화 (`scripts/backfill_wiki.py`)
5. WikiView 에 Health 배지 + Log 뷰
6. 운영 1주일 데이터 수집 → Phase 5 결정

**검증 지표**:
- 5회의 lint 후 `HEALTH.md` 업데이트 자동 발생
- 백필 50건 회의 → 위키 일관성 (모순 ≤3%)

**예상 LOC**: +900

### Phase 5 — 질의 라우팅 (재평가)

**조건부 진행**: Phase 4 운영 1주일 후 결정.

판단 기준:
- 사용자가 Wiki 뷰 vs 채팅 뷰 직접 선택에 어려움 호소? → 진행
- 두 뷰가 분명히 다른 가치 제공으로 인지? → 보류
- Wiki 답변 정확도가 RAG 보다 낮으면? → 라우팅 무의미, 보류

만약 진행 시:
1. 라우터 LLM (EXAONE 짧은 분류)
2. 채팅 UI 가 라우터 결과 기반 답변 (Wiki / RAG / 둘 다)
3. 사용자가 "더 자세히" 클릭 시 다른 뷰로 폴백

---

## 10. 테스트 전략 (TDD)

### 10.1 단위 테스트 (`tests/wiki/`)

**핵심 테스트** (Phase 2 까지 ≥100건):

```python
# tests/wiki/test_citations.py
def test_citation_pattern_matches_valid():
    assert CITATION_PATTERN.search("문장 [meeting:abc12345@00:23:45].")

def test_citation_pattern_rejects_malformed():
    cases = ["[meeting:abc@00:23:45]", "[m:abc12345@00:23]",
             "[meeting:ABC12345@00:23:45]", "[meeting:abc12345@99:99:99]"]
    # ...

# tests/wiki/test_guard.py
async def test_d1_rejects_uncited_factual_statement():
    raw = "# 제목\n\n결정사항이 있다.\n\n다른 결정 [meeting:abc12345@00:01:00]."
    cleaned, rejected = enforce_citations(raw, "abc12345")
    assert "결정사항이 있다." not in cleaned
    assert "다른 결정" in cleaned

async def test_d2_phantom_citation_caught():
    rag = MockRAG(utterances=[("abc12345", 60.0, "...")])
    content = "# 제목\n\n사실 [meeting:abc12345@99:99:99]."
    assert await verify_citations(content, rag) is False

async def test_d3_low_confidence_isolated():
    content = "# 제목\n\n사실 [meeting:abc12345@00:01:00].\n<!-- confidence: 4 -->"
    passed, score = check_confidence(content, threshold=7)
    assert not passed and score == 4

# tests/wiki/test_compiler.py
async def test_compiler_creates_decisions_page_for_clear_decision():
    utterances = [
        Utterance(start=10, end=20, speaker="철수", text="5월 1일 출시로 가시죠"),
        Utterance(start=20, end=25, speaker="영희", text="동의합니다"),
    ]
    summary = "신규 온보딩 출시일을 5월 1일로 결정"
    pages = await compiler._decide_pages(ctx)
    assert any(p["new_page"].startswith("decisions/") for p in pages)
```

### 10.2 통합 테스트

```python
# tests/wiki/test_pipeline_integration.py
async def test_full_pipeline_with_wiki(tmp_path):
    config.wiki.enabled = True
    config.wiki.root = tmp_path / "wiki"
    await run_pipeline("test_audio.wav")
    assert (tmp_path / "wiki" / "log.md").exists()
    assert (tmp_path / "wiki" / ".git").exists()
```

### 10.3 회귀 테스트 (RAG 무영향 보장)

```python
# tests/test_rag_unchanged.py
async def test_existing_chat_unaffected_by_wiki(...):
    """Wiki 활성 vs 비활성에서 같은 질의 → 같은 RAG 답변."""
    config.wiki.enabled = False
    response_a = await chat("이번 회의 뭐 얘기했어?")

    config.wiki.enabled = True
    response_b = await chat("이번 회의 뭐 얘기했어?")
    # /api/chat 은 Wiki 안 봄 — 동일해야 함
    assert response_a.sources == response_b.sources
```

### 10.4 E2E (Playwright)

- `/app/wiki` 진입 → 페이지 트리 표시 확인
- 결정사항 페이지 → 인용 마커 클릭 → `/app/viewer/{id}?t=...` 이동
- viewer 에서 ▶ 자동 재생 확인 (PR #20 연동)

---

## 11. 리스크 및 완화

| # | 리스크 | 확률 | 영향 | 완화 |
|---|---|---|---|---|
| R1 | 환각 누적이 5중 방어를 뚫고 발생 | 중 | 치명 | git revert 즉시 가능. HEALTH.md 사용자 확인 권장 |
| R2 | EXAONE이 인용 누락 빈번 | 중 | 중 | D1 임계 30% 조정. 프롬프트 강화 반복 |
| R3 | 페이지 갱신 시간 폭증 (회의당 5분 초과) | 낮 | 중 | 영향 페이지 상한(예: 8개)으로 제한. 나머지는 다음 ingest |
| R4 | 메모리 부족 (Gemma+EXAONE 스왑 부담) | 낮 | 중 | ResourceGuard 가 이미 LLM 메모리 경고. wiki disable 폴백 |
| R5 | 백필 중 단일 회의 실패 시 멈춤 | 중 | 낮 | 백필 잡이 회의별 트랜잭션 + skip on error |
| R6 | 인용된 회의 삭제 시 dangling link | 중 | 낮 | 회의 삭제 hook → 해당 인용 lint 에서 보고 |
| R7 | git repo 손상 (강제 종료 등) | 낮 | 중 | 원자적 commit. wiki/.git/index.lock 정리 핸들러 |
| R8 | LLM 이 confidence 를 형식 잘못 출력 | 중 | 낮 | 정규식 1회 재시도 후 0 으로 처리 (보수적) |
| R9 | 자동 운영이 사용자 모르게 잘못된 정보 누적 | 중 | 치명 | HEALTH.md 메뉴바 알림. 주간 리포트 알림 (Phase 4) |
| R10 | 한국어 외 회의 (영어 미팅 등) 처리 | 낮 | 낮 | EXAONE 다국어 지원. 인용 형식만 유지되면 OK |

### 가장 본질적 리스크 — R1, R9

이 두 개가 본 설계의 아킬레스건. 완화 전략:

1. **사용자 가시성 강화** (Phase 4): 메뉴바에서 위키 건강 표시 (🟢/🟡/🔴)
2. **인용 클릭 → ▶ 자동 재생** (PR #20): 사용자가 의심하면 1초 안에 검증 가능
3. **git 이력 신뢰**: 어떤 ingest 가 어떤 페이지를 바꿨는지 영구 추적
4. **자동 lint 강제**: 5회의 주기를 사용자가 못 끄게 (config 로도 비활성화 불가)
5. **백필 후 첫 1주 모니터링**: 사용자에게 "최근 7일 위키 변경 요약" 일일 알림

---

## 12. 운영 시나리오 — 가상 사용자 1주일

월요일 09:00 — 첫 회의 ingest:
- 8단계 완료 후 9단계 30초간 실행
- log.md 첫 항목 추가
- 새 페이지 4개 생성: decisions/2026-04-28-q3-launch.md, people/철수.md, people/영희.md, projects/q3-launch.md, action_items.md
- git commit 1개

화요일~금요일 — 5건 회의 추가 ingest:
- 매번 9단계 자동 실행
- 기존 페이지 갱신 + 새 페이지 추가
- 5회의 도달 시 자동 lint → HEALTH.md 갱신

토요일 — 사용자가 위키 뷰 첫 방문:
- /app/wiki 에 47개 페이지 (5회의 + 백필 분)
- 인용 클릭 시 ▶ 음성 재생으로 정확성 검증
- HEALTH.md 에 고아 페이지 1개 발견 → 사용자가 직접 삭제

다음 주 — 사용자가 처음으로 누적 질의:
- "지난주 결정사항 정리해줘" → /app/wiki 에서 decisions/ 카테고리 펼침
  vs
  /app 채팅에서 같은 질의 → RAG 매번 합성 (느림)
- 사용자가 "이건 위키가 빠르네" 인지 → Phase 5 라우터 도입 결정

---

## 13. 성공 지표

### Phase 2 완료 시점 (2주 후)
- [ ] 결정사항 페이지 정확도 ≥80% (사용자 수동 검증)
- [ ] 액션아이템 누락률 ≤10%
- [ ] 인용 검증 통과율 ≥99%
- [ ] 페이지 갱신당 평균 시간 ≤90초
- [ ] 회귀 — 기존 RAG 채팅 응답 100% 동일

### Phase 4 완료 시점 (5주 후)
- [ ] 50건 회의 백필 성공
- [ ] HEALTH.md 자동 갱신 5회 이상
- [ ] 사용자가 위키 뷰를 주 3회 이상 사용
- [ ] 인용 클릭 → ▶ 재생 흐름 1주 내 10회 이상 발생

### Phase 5 진행 결정 기준 (6주 후)
- 위키 답변 정확도 ≥ RAG 답변 정확도 → 진행
- 사용자가 두 뷰 선택 어려움 호소 → 진행
- 그 외 → 보류

---

## 14. 향후 과제 (Out-of-Scope)

- **다중 사용자/팀 모드**: 동시 ingest, conflict resolution
- **외부 데이터 ingest**: Slack 채널, 이메일 → 위키 합성
- **위키 그래프 뷰**: 페이지 간 링크 시각화 (Obsidian Graph 유사)
- **Marp 슬라이드 자동 생성**: 결정사항 → 발표 자료
- **자연어 위키 편집**: 사용자가 채팅으로 "이 페이지 고쳐줘"
- **위키 검색 자체 임베딩**: ChromaDB 별도 컬렉션
- **모바일 위키 뷰**: 반응형 레이아웃
- **다국어 위키**: 영어 회의 → 영문 위키 페이지

---

## 15. 참고 자료

### 1차 자료
- [Karpathy LLM Wiki Gist (2026-04-03)](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — 패턴 원전

### 비판/한계 분석
- [Mehul Gupta — Andrej Karpathy's LLM Wiki is a Bad Idea](https://medium.com/data-science-in-your-pocket/andrej-karpathys-llm-wiki-is-a-bad-idea-8c7e8953c618)
- [Epsilla — The Enterprise Verdict](https://www.epsilla.com/blogs/llm-wiki-kills-rag-karpathy-enterprise-semantic-graph)
- [Atlan — LLM Wiki vs RAG: Enterprise Reality](https://atlan.com/know/llm-wiki-vs-rag-knowledge-base/)

### 확장 시도
- [Rohit Ghumare — LLM Wiki v2 (한계 보완)](https://gist.github.com/rohitg00/2067ab416f7bbe447c1977edaaa681e2)
- [DAIR.AI — LLM Knowledge Bases 분석](https://academy.dair.ai/blog/llm-knowledge-bases-karpathy)

### 우리 프로젝트
- `CLAUDE.md` — 프로젝트 가이드 (LLM 백엔드, 모델 관리)
- `core/pipeline.py` — 8단계 파이프라인 (9단계가 추가될 위치)
- `core/model_manager.py` — ModelLoadManager (Gemma↔EXAONE 스왑)
- `search/hybrid_search.py` — 인용 검증(D2)에 재사용
- PR #20 (회의록 발화별 음성 재생) — 위키 인용 클릭 → ▶ 자동 재생의 기반

---

## 16. 결정 사항 요약

본 PRD 작성 전 사용자(youngouk) 와 합의된 4가지 결정:

| # | 결정 | 합의 내용 |
|---|---|---|
| 1 | 범위 | Phase 1~4 전체. Phase 5 는 운영 데이터로 재평가 |
| 2 | 운영 모드 | 자동 운영 (사용자 검토 큐 없음). 5중 자동 방어로 대체 |
| 3 | 일반 요약 모델 | Gemma 4 (현 시스템 그대로) |
| 4 | 위키 컴파일 모델 | EXAONE 3.5 (한국어 고유명사 정확성 우선) |

### 핵심 안전장치 (재확인)
- D1 인용 강제 후처리
- D2 인용 실재성 RAG 검증
- D3 confidence 임계 7
- D4 5회의마다 자동 lint
- D5 git 단일 커밋 + 즉시 롤백 가능

### RAG 무영향 보장
- ChromaDB / FTS5 / e5-small / hybrid_search 모두 변경 없음
- `/api/chat` 응답이 Wiki 활성/비활성에 동일 (회귀 테스트로 강제)

---

**다음 단계**: Phase 1 착수 — `core/wiki/` 모듈 골격 + 9단계 통합 + 빈 위키 생성.
브랜치 `feat/llm-wiki-phase1` 에서 작업 예정.
