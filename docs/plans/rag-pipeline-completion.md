# RAG 파이프라인 완성 + 백필 UI

> 단일 PR. 메인 파이프라인의 누락된 chunk/embed 단계를 정식 통합하고,
> 기존 회의를 인덱싱하는 백필 API + 설정 UI 를 추가한다.

## 1. 배경

`core/pipeline.py:299-306` 의 `PIPELINE_STEPS` 에는 다음 6단계만 정의돼 있다.

```
CONVERT → TRANSCRIBE → DIARIZE → MERGE → CORRECT → SUMMARIZE
```

`steps/chunker.py`, `steps/embedder.py` 모듈은 존재하지만 **메인 파이프라인에 한 번도 import 되지 않는다**.
결과적으로:

- 회의는 `completed` 로 표시되지만 ChromaDB / SQLite FTS5 인덱스는 비어 있음
- `/api/search`, `/api/chat` 모두 0건 반환
- 사용자에게는 "회의의 전사문이 제공되지 않았습니다" 답변 (LLM 컨텍스트 없음)

추가로 `steps/embedder.py:649-671` 는 ChromaDB / FTS5 저장 실패를 `try/except StorageError` 로 삼키고
`chroma_stored=False` 만 기록한 채 단계를 성공 처리한다. 이는 chunk/embed 가 정식 단계로 추가된 뒤에도
"인덱스 없이 completed" 상태가 발생할 수 있는 잠재 버그.

## 2. 목표

1. 메인 파이프라인을 8단계로 확장: `... → SUMMARIZE → CHUNK → EMBED`
2. embedder fail-loud: ChromaDB / FTS5 중 한 쪽이라도 실패하면 `StorageError` 전파
3. 기존 회의 백필 API (3개 엔드포인트)
4. 설정 화면에 "검색 인덱스 관리" 탭 추가
5. 회귀 방지 테스트: pipeline.run() 후 ChromaDB 청크 카운트 > 0

## 3. 비목표

- 새 임베딩 모델 도입 (intfloat/multilingual-e5-small 유지)
- 청킹 전략 변경 (기존 정책 그대로)
- ChromaDB → 다른 벡터 DB 마이그레이션
- 이미 완료된 회의의 자동 백필 (사용자가 UI 에서 명시적으로 트리거)
- Wiki 파이프라인 (Phase 1 LLM Wiki) 변경

## 4. 아키텍처 변경

### 4.1 파이프라인 단계 (core/pipeline.py)

```python
# 변경 전 (line 299-306)
PIPELINE_STEPS = [CONVERT, TRANSCRIBE, DIARIZE, MERGE, CORRECT, SUMMARIZE]

# 변경 후
PIPELINE_STEPS = [CONVERT, TRANSCRIBE, DIARIZE, MERGE, CORRECT, SUMMARIZE, CHUNK, EMBED]
```

**왜 SUMMARIZE 이후인가**:
- 검색 인덱스가 회의록 생성을 차단하면 안 됨 (검색은 부가 기능, 회의록은 핵심)
- chunk/embed 단계 실패 시에도 사용자는 회의록은 받을 수 있음
- 단, 인덱스 누락은 명확하게 보고됨 (job status 또는 state.warnings 에 기록)

**메모리 가드**:
- EMBED 진입 직전 `check_memory()` 로 가용 메모리 검증
- e5-small 은 약 500MB 라 여유롭지만 전체 파이프라인 누적 메모리 압박 가능
- 메모리 부족 시 `state.skipped_steps` 에 추가하고 경고 (회의록은 보존)

### 4.2 embedder fail-loud (steps/embedder.py)

```python
# 변경 전 (line 649-671)
try:
    await asyncio.to_thread(_store_chunks_chroma, ...)
    result.chroma_stored = True
except StorageError:
    logger.exception(f"ChromaDB 저장 실패: meeting_id={meeting_id}")
    # 단계는 성공 처리됨 ← 잠재 버그

# 변경 후 — 한쪽이라도 실패하면 raise
await asyncio.to_thread(_store_chunks_chroma, ...)
result.chroma_stored = True

await asyncio.to_thread(_store_chunks_fts, ...)
result.fts_stored = True

# StorageError 가 자연스럽게 raise → 재시도 루프가 받아 재시도
```

### 4.3 백필 API (api/routes.py)

세 가지 엔드포인트:

| 엔드포인트 | 동작 |
|---|---|
| `GET /api/meetings/index-status` | 회의별 청크 카운트 집계, `{total, indexed, missing, missing_meeting_ids[]}` 반환 |
| `POST /api/meetings/{id}/reindex` | 단일 회의 재색인. correct.json 우선, 없으면 merge.json 폴백 |
| `POST /api/meetings/reindex-all` | 백그라운드 task. 글로벌 `asyncio.Lock` 으로 단일 동시 실행 강제. 순차 처리. WebSocket `reindex_progress` 이벤트 broadcast |

**동시성 정책**: 백필은 신규 회의 처리와 큐를 공유하지 않는다. 별도 백그라운드 task 로 실행하되,
ChromaDB / FTS5 동시 쓰기 충돌을 막기 위해 백필 자체는 한 번에 한 회의씩 순차 처리.

### 4.4 설정 UI (ui/web/spa.js)

기존 SettingsView (3탭: 일반/프롬프트/용어집) 에 4번째 탭 "검색 인덱스" 추가.

```
┌─ 검색 인덱스 ─────────────────────┐
│ 카드 1: 현황 요약                 │
│   전체 N / 인덱싱됨 M / 누락 K    │
│                                   │
│ 카드 2: 일괄 백필                 │
│   [전체 누락분 백필 시작]         │
│   진행 상황 progress bar          │
│                                   │
│ 카드 3: 누락 회의 목록            │
│   meeting_id ... [재색인]         │
│   meeting_id ... [재색인]         │
└───────────────────────────────────┘
```

`docs/design.md` 토큰만 사용. 새 디자인 패턴 도입 금지.

## 5. Phase 별 작업

| Phase | 내용 | 의존성 |
|---|---|---|
| 0 | 사전 코드 검증 (chunker/embedder API, JobStatus enum, 헬퍼 패턴) | — |
| 1 RED | tests/test_pipeline_chunk_embed.py — pipeline.run() 후 ChromaDB count > 0 | 0 |
| 1 GREEN | PIPELINE_STEPS 확장 + _run_step_chunk/_run_step_embed | 1 RED |
| 2 | embedder fail-loud + RED/GREEN 테스트 | 1 GREEN |
| 3 RED | tests/test_routes_reindex.py — 3 엔드포인트 RED | 2 |
| 3 GREEN | 백필 API + WebSocket 이벤트 | 3 RED |
| 4 | 설정 UI 검색 인덱스 탭 | 3 GREEN |
| 5 | 통합 회귀 테스트 + smoke test | 4 |
| 6 | 문서 업데이트 (CLAUDE.md, README, design.md) | 4 |
| 7 | 코드 리뷰 + 최종 검증 + 커밋 | 5, 6 |

## 6. 테스트 전략

### 6.1 RED → GREEN 사이클 강제

각 Phase 의 RED 테스트는 첫 실행에서 **정확한 이유로 FAIL** 해야 한다.

- Phase 1 RED: `assert chroma_count > 0` 가 `chroma_count == 0` 으로 실패
- Phase 2 RED: `pytest.raises(StorageError)` 가 `StorageError 미발생` 으로 실패
- Phase 3 RED: API 호출이 404 (라우트 미존재) 로 실패

### 6.2 회귀 방지 게이트

- `pytest tests/ -x -q` 전체 패스 (1700+ 기존 테스트)
- E2E (Playwright) 가 있다면 수동 트리거로 분리 실행

### 6.3 Smoke Test (수동, README 부록)

```bash
# 1. 백필 시나리오
curl -X POST http://127.0.0.1:8765/api/meetings/{id}/reindex
curl -s "http://127.0.0.1:8765/api/search?query=결정&meeting_id_filter={id}" | jq '.total_found'
# 결과: > 0 이어야 함

# 2. 신규 회의 시나리오
# 짧은 WAV 를 audio_input/ 에 떨어뜨리고
# 파이프라인 완료 대기 → /api/chat 으로 질문 → 컨텍스트 있는 답변 검증
```

## 7. 롤백 계획

- 단일 PR 이므로 머지 후 회귀가 발견되면 `git revert` 한 번으로 복귀
- 백필 작업 자체는 idempotent: `embedder` 가 동일 meeting_id 의 기존 청크를 삭제 후 재삽입 (`steps/embedder.py:340-348`)
- DB 스키마 변경 없음 (FTS5 테이블, ChromaDB 컬렉션 모두 기존)

## 8. 리스크와 완화

| 리스크 | 영향 | 완화 |
|---|---|---|
| EMBED 단계가 메모리 부족 | 회의록까지 영향 | EMBED 는 SUMMARIZE 이후 → 이미 회의록 작성 완료 |
| 백필 중 신규 회의 큐 처리 충돌 | 데이터 손상 | ChromaDB / SQLite WAL 모드 → 동시 쓰기 안전. 백필은 자체적으로 순차 |
| 1700+ 기존 테스트 회귀 | 머지 차단 | Phase 5 에서 전체 pytest 실행 + Phase 7 코드 리뷰 |
| 사용자가 백필 미실행 | 기존 회의 검색 불가 | 설정 UI 진입 시 누락 K > 0 이면 경고 배너 |
| chunker/embedder 의 from_checkpoint 가 부재 | 재개 불가 | Phase 0 에서 검증 → 부재 시 추가 구현 |

## 9. 마이그레이션 가이드 (PR 본문에 포함)

> 이 PR 머지 후, **기존 회의는 자동으로 검색되지 않습니다**. 다음 단계로 백필하세요.
>
> 1. `python main.py` 실행
> 2. 메뉴바 → 웹 UI 열기 → 설정 → 검색 인덱스
> 3. "전체 누락분 백필 시작" 클릭
> 4. 진행 상황은 progress bar 로 표시 (백그라운드 실행, 창을 닫아도 계속됨)
>
> 신규 회의는 자동으로 인덱싱되므로 별도 조치 불필요.

## 10. 완료 정의 (DoD)

- [ ] `PIPELINE_STEPS` 8단계로 확장
- [ ] embedder fail-loud
- [ ] 3 백필 엔드포인트 + WebSocket 이벤트
- [ ] 설정 UI 검색 인덱스 탭
- [ ] `pytest tests/ -x -q` 전체 패스
- [ ] CLAUDE.md / README 업데이트
- [ ] python-reviewer + code-reviewer 리뷰 통과
- [ ] PR 본문에 마이그레이션 가이드 포함
