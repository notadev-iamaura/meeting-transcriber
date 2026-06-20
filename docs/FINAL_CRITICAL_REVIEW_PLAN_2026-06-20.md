# Meeting Transcriber 최종 비판 재검증 계획서

작성일: 2026-06-20
상태: `docs/CONSENSUS_DEEP_CODE_REVIEW_2026-06-20.md`의 재검증판

## 1. 최종 판정

기존 합의문의 큰 방향은 맞다. 다만 몇몇 표현은 강도를 조정해야 한다.

최종 결론은 다음과 같다.

> 지금 가장 먼저 고쳐야 할 것은 모델 성능이 아니라 "회의 데이터의 진실성 계약"이다. 삭제, 재전사, 재요약, 검색, RAG 답변, 모델 메모리 수명이 각각 따로 움직이면 사용자는 완료/삭제/검색 결과를 믿을 수 없다.

재검증 결과, 즉시 계획에 반영해야 할 최상위 문제는 4개다.

1. 삭제된 회의의 검색 인덱스가 정리된다는 보장이 없다.
2. RAG 채팅은 검색 실패/근거 없음 상태에서도 LLM 답변을 생성한다.
3. LLM 스킵/온디맨드 LLM 경로가 산출물과 검색 인덱스 최신성을 명확히 보장하지 않는다.
4. `keep_loaded=True` 모델 최적화가 특정 경로에서 명시적으로 닫히지 않는다.

## 2. 재검증 결과 요약

| 기존 주장 | 최종 판정 | 수정된 표현 |
| --- | --- | --- |
| 삭제된 회의가 검색에 남을 수 있다 | 확인 | 삭제 API는 DB와 오디오 quarantine 중심이며 ChromaDB/FTS5 purge 계약이 없다. |
| `skip_llm_steps`가 resume을 깨뜨릴 수 있다 | 조건부 확인 | 전체 실행이 끝나면 테스트상 completed가 맞다. 다만 correct/summarize 스킵 후 chunk 전 실패/중단 시 resume에 필요한 `correct.json`이 없을 수 있다. |
| RAG가 근거 없이 답한다 | 확인 | 현재 테스트도 검색 실패 시 컨텍스트 없이 LLM 호출을 기대한다. 이 테스트 계약을 바꿔야 한다. |
| 온디맨드 LLM 후 인덱스가 stale일 수 있다 | 확인 | `run_llm_steps()`는 correct/summarize만 실행하고 chunk/embed를 갱신하지 않는다. |
| ChromaDB/FTS5 실패가 조용히 성공한다 | 수정 | embed 단계는 fail-loud다. 정확한 문제는 ChromaDB와 FTS5가 transaction/generation 단위로 원자적이지 않아 실패 후 반쪽 세대가 남을 수 있다는 점이다. |
| 검색 장애가 빈 결과로 축소된다 | 부분 확인 | vector/FTS 내부 검색 실패는 빈 결과로 degradation된다. 반면 query embedding/model load 실패는 FTS fallback 전에 전체 search 예외로 갈 수 있다. |
| 설정 변경이 장기 서비스에 반영되지 않는다 | 확인 | `app.state.config`와 일부 scheduler만 갱신한다. Pipeline/Search/Chat 재구성 계약은 없다. |
| JobQueue claim이 원자적이지 않다 | 확인 | 현재 `get_pending_jobs()` 후 별도 status update 구조다. 단일 processor에서는 낮은 위험이나 자동/수동 경로가 늘면 커진다. |
| AutoProcessing이 JobProcessor를 우회한다 | 확인 | 자동 처리는 `pipeline.run()`을 직접 호출한다. progress/thermal/cancel/status 경로가 분리된다. |
| `keep_loaded=True` LLM이 남을 수 있다 | 확인 | Corrector/Wiki V2에서 keep-loaded 경로가 있고 chain 종료 finally unload가 없다. |
| STT compression_ratio 필터가 비활성일 수 있다 | 확인 | `TranscriptSegment`에 `compression_ratio` 필드가 없다. |
| VAD/default 문서가 충돌한다 | 확인 | 실제 config는 OFF이며 일부 문서는 ON처럼 읽힌다. |
| diarization timeout 600초가 적용되지 않는다 | 확인 | config.yaml 위치는 `pipeline.*`이고 실제 필드는 `diarization.timeout_seconds`다. |
| recorder ffmpeg pipe 위험 | 확인 | 장시간 pipe drain/timeout/전체 WAV 로딩 리스크가 있다. |

완전히 철회할 항목은 없다. 다만 "조용히 성공"처럼 과한 표현은 위처럼 고쳤다.

## 3. 검증 근거

### 3.1 삭제/재전사 수명주기

삭제 API는 job 삭제와 오디오 quarantine을 수행한다.

- 구현: [api/routers/meeting_detail.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/meeting_detail.py:1043)
- DB 삭제: [api/routers/meeting_detail.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/meeting_detail.py:1086)
- JobQueue delete는 `jobs` row만 삭제한다. [core/job_queue.py](/Users/youngouksong/projects/meeting-transcriber/core/job_queue.py:948)

재전사는 체크포인트와 일부 output 파일을 삭제하고 queued로 되돌린다.

- 구현 주석: [api/routers/meeting_detail.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/meeting_detail.py:704)
- 체크포인트 삭제: [api/routers/meeting_detail.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/meeting_detail.py:740)
- 출력 일부 삭제: [api/routers/meeting_detail.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/meeting_detail.py:746)

현재 테스트도 DB/오디오/체크포인트/summary 중심이다. 검색 인덱스 purge 테스트는 없다.

- 삭제 테스트: [tests/test_routes.py](/Users/youngouksong/projects/meeting-transcriber/tests/test_routes.py:1588)
- 재전사 테스트: [tests/test_routes.py](/Users/youngouksong/projects/meeting-transcriber/tests/test_routes.py:2032)

### 3.2 RAG 무근거 답변

컨텍스트가 없으면 사용자 프롬프트가 "알고 있는 범위" 답변을 유도한다.

- 프롬프트 구성: [search/chat.py](/Users/youngouksong/projects/meeting-transcriber/search/chat.py:253)
- 검색 실패 후 진행: [search/chat.py](/Users/youngouksong/projects/meeting-transcriber/search/chat.py:618)
- LLM 호출: [search/chat.py](/Users/youngouksong/projects/meeting-transcriber/search/chat.py:650)
- 스트리밍도 같은 구조: [search/chat.py](/Users/youngouksong/projects/meeting-transcriber/search/chat.py:700)

현재 테스트는 이 동작을 정상으로 본다.

- 검색 실패 시 LLM 호출 테스트: [tests/test_chat.py](/Users/youngouksong/projects/meeting-transcriber/tests/test_chat.py:366)

### 3.3 LLM 스킵과 온디맨드 LLM

`skip_llm_steps`는 correct/summarize를 skipped와 completed에 모두 기록한다.

- 스킵 처리: [core/pipeline.py](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1447)
- completed 기록: [core/pipeline.py](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1483)
- chunk는 `corrected_result`가 있다고 assert한다. [core/pipeline.py](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1588)
- resume 복원은 correct checkpoint가 없으면 `corrected_result`를 채우지 않는다. [core/pipeline.py](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1801)

현재 테스트는 전체 run이 끝나는 정상 케이스를 검증한다.

- skip 시 정상 완료 테스트: [tests/test_pipeline.py](/Users/youngouksong/projects/meeting-transcriber/tests/test_pipeline.py:3209)

`run_llm_steps()`는 correct/summarize만 수행한다.

- 함수 설명: [core/pipeline.py](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1867)
- 상태 업데이트: [core/pipeline.py](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1996)
- 테스트도 correct/summarize 실행만 본다. [tests/test_pipeline.py](/Users/youngouksong/projects/meeting-transcriber/tests/test_pipeline.py:3479)

### 3.4 검색 인덱스 원자성

embed 단계는 fail-loud다. 이것은 좋은 점이다.

- ChromaDB 저장 후 FTS5 저장: [steps/embedder.py](/Users/youngouksong/projects/meeting-transcriber/steps/embedder.py:649)
- ChromaDB 실패 테스트: [tests/test_embedder.py](/Users/youngouksong/projects/meeting-transcriber/tests/test_embedder.py:490)
- FTS5 실패 테스트: [tests/test_embedder.py](/Users/youngouksong/projects/meeting-transcriber/tests/test_embedder.py:518)

하지만 기존 데이터를 먼저 삭제하고 새 데이터를 넣는 방식이라, 실패 시 두 저장소가 같은 generation임을 보장하지 않는다.

- FTS5 기존 meeting 삭제 후 삽입: [steps/embedder.py](/Users/youngouksong/projects/meeting-transcriber/steps/embedder.py:240)
- ChromaDB 기존 meeting 삭제 후 삽입: [steps/embedder.py](/Users/youngouksong/projects/meeting-transcriber/steps/embedder.py:340)

검색 자체는 vector/FTS 한쪽 실패를 graceful degradation으로 본다.

- vector 실패 시 빈 결과: [search/hybrid_search.py](/Users/youngouksong/projects/meeting-transcriber/search/hybrid_search.py:401)
- FTS 실패 시 빈 결과: [search/hybrid_search.py](/Users/youngouksong/projects/meeting-transcriber/search/hybrid_search.py:517)
- 테스트: [tests/test_hybrid_search.py](/Users/youngouksong/projects/meeting-transcriber/tests/test_hybrid_search.py:700)

### 3.5 설정 반영

설정 저장은 `app.state.config`를 교체하고 lifecycle/auto-processing scheduler 일부만 갱신한다.

- 설정 변경 감지: [api/routers/settings.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/settings.py:365)
- config 교체: [api/routers/settings.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/settings.py:619)
- 일부 scheduler 갱신: [api/routers/settings.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/settings.py:622)
- "다음 LLM 호출" 메시지: [api/routers/settings.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/settings.py:630)

하지만 장기 객체는 startup에서 생성된다.

- SearchEngine 생성: [api/server.py](/Users/youngouksong/projects/meeting-transcriber/api/server.py:121)
- ChatEngine 생성: [api/server.py](/Users/youngouksong/projects/meeting-transcriber/api/server.py:131)
- PipelineManager 생성: [api/server.py](/Users/youngouksong/projects/meeting-transcriber/api/server.py:232)

ModelLoadManager도 같은 model key면 기존 인스턴스를 재사용한다.

- 재사용 조건: [core/model_manager.py](/Users/youngouksong/projects/meeting-transcriber/core/model_manager.py:311)

### 3.6 모델 메모리

Corrector는 `keep_loaded=True`를 사용한다.

- Corrector acquire: [steps/corrector.py](/Users/youngouksong/projects/meeting-transcriber/steps/corrector.py:497)
- Summarizer acquire: [steps/summarizer.py](/Users/youngouksong/projects/meeting-transcriber/steps/summarizer.py:560)
- keep-loaded exit 동작: [core/model_manager.py](/Users/youngouksong/projects/meeting-transcriber/core/model_manager.py:462)
- 명시 unload API는 존재한다. [core/model_manager.py](/Users/youngouksong/projects/meeting-transcriber/core/model_manager.py:356)

Wiki V2도 keep-loaded 책임을 호출자에게 둔다.

- Wiki LLM client: [core/wiki/llm_client.py](/Users/youngouksong/projects/meeting-transcriber/core/wiki/llm_client.py:390)
- Wiki compiler wrapper: [steps/wiki_compiler.py](/Users/youngouksong/projects/meeting-transcriber/steps/wiki_compiler.py:276)

### 3.7 리소스/보안/설정 불일치

STT hallucination filter는 `compression_ratio`를 읽지만 segment에 저장하지 않는다.

- Segment fields: [steps/transcriber.py](/Users/youngouksong/projects/meeting-transcriber/steps/transcriber.py:34)
- parse fields: [steps/transcriber.py](/Users/youngouksong/projects/meeting-transcriber/steps/transcriber.py:312)
- filter: [steps/hallucination_filter.py](/Users/youngouksong/projects/meeting-transcriber/steps/hallucination_filter.py:195)

VAD와 diarization timeout은 문서/설정 정리가 필요하다.

- VAD default OFF: [config.py](/Users/youngouksong/projects/meeting-transcriber/config.py:98)
- `config.yaml` VAD OFF: [config.yaml](/Users/youngouksong/projects/meeting-transcriber/config.yaml:217)
- `pipeline.diarization_timeout_seconds`: [config.yaml](/Users/youngouksong/projects/meeting-transcriber/config.yaml:146)
- 실제 diarization timeout field: [config.py](/Users/youngouksong/projects/meeting-transcriber/config.py:289)

Local-only 정책은 기본값은 안전하지만 override가 열려 있다.

- LLM host default: [config.py](/Users/youngouksong/projects/meeting-transcriber/config.py:328)
- server host default: [config.py](/Users/youngouksong/projects/meeting-transcriber/config.py:521)
- env override: [config.py](/Users/youngouksong/projects/meeting-transcriber/config.py:918)
- LAN LLM host 허용 테스트: [tests/test_config.py](/Users/youngouksong/projects/meeting-transcriber/tests/test_config.py:207)
- 보안 문서의 localhost 전제: [SECURITY.md](/Users/youngouksong/projects/meeting-transcriber/SECURITY.md:81)

Recorder는 장시간 subprocess와 전체 WAV 로딩 리스크가 있다.

- device scan pipe: [steps/recorder.py](/Users/youngouksong/projects/meeting-transcriber/steps/recorder.py:253)
- full WAV read: [steps/recorder.py](/Users/youngouksong/projects/meeting-transcriber/steps/recorder.py:298)
- recording pipe: [steps/recorder.py](/Users/youngouksong/projects/meeting-transcriber/steps/recorder.py:629)
- wait-only shutdown: [steps/recorder.py](/Users/youngouksong/projects/meeting-transcriber/steps/recorder.py:909)

## 4. 최종 우선순위

### P0: 제품 신뢰와 데이터 삭제 계약

#### P0-1. RAG 무근거 답변 차단

현재 동작:

- 검색 실패 또는 결과 없음이어도 LLM 호출 가능.
- 테스트도 이 동작을 정상으로 고정.

목표 동작:

- 검색 실패: `grounding_status="search_error"`, `llm_called=false`
- 검색 결과 0건: `grounding_status="no_results"`, `llm_called=false`
- 인덱스 미구성: `grounding_status="index_missing"`, `llm_called=false`
- 근거 있음: `grounding_status="grounded"`, LLM 호출 허용

수정 파일:

- `search/chat.py`
- `api/routers/search_chat.py`
- `tests/test_chat.py`
- `tests/test_routes.py`
- 필요 시 UI chat view

필수 테스트:

```bash
.venv/bin/pytest tests/test_chat.py -k '검색_실패 or 컨텍스트_없음 or stream' -q
.venv/bin/pytest tests/test_routes.py -k 'chat' -q
```

#### P0-2. 삭제 시 검색 인덱스 purge

현재 동작:

- DB row와 audio quarantine 중심.
- ChromaDB/FTS5 삭제 계약 없음.

목표 동작:

- 삭제 API가 Job DB, audio quarantine, checkpoints, outputs, ChromaDB, FTS5를 하나의 meeting lifecycle 경로로 정리.
- 검색 인덱스 purge 실패 시 정책을 명확히 정한다.
  - 권장: DB 삭제 전에 purge를 시도하고, purge 실패 시 500/409로 삭제 실패 처리.
  - 개인정보 삭제 의미가 강하므로 best-effort로 숨기지 않는다.

수정 파일:

- `api/routers/meeting_detail.py`
- `core/job_queue.py`
- `steps/embedder.py` 또는 새 `core/meeting_lifecycle.py`
- `search/hybrid_search.py`
- `tests/test_routes.py`
- `tests/test_embedder.py`

필수 테스트:

```bash
.venv/bin/pytest tests/test_routes.py -k '삭제' -q
.venv/bin/pytest tests/test_embedder.py -k 'purge or 삭제' -q
.venv/bin/pytest tests/test_hybrid_search.py -k 'meeting_id_filter' -q
```

### P1: 상태/산출물/인덱스 일관성

#### P1-1. `skip_llm_steps` artifact contract 수정

최종 판정:

- 전체 정상 실행은 현재 테스트대로 completed가 된다.
- 문제는 중간 실패/재개 시점이다.

목표 동작:

- 스킵된 correct는 다음 중 하나를 반드시 만족한다.
  1. `correct.json` 호환 pass-through checkpoint를 저장한다.
  2. chunk 단계가 `merge.json`을 명시적으로 fallback 입력으로 받을 수 있다.
- `completed_steps`와 `available_artifacts`를 분리한다.

필수 테스트:

```bash
.venv/bin/pytest tests/test_pipeline.py -k 'skip_llm_steps or restore or resume' -q
.venv/bin/pytest tests/test_graceful_degradation.py -k 'skip' -q
```

#### P1-2. 온디맨드 LLM 후 index stale/reindex 처리

현재 동작:

- `run_llm_steps()`는 correct/summarize만 수행.

목표 동작:

- correct/summarize 성공 후 `chunk/embed`를 이어 실행하거나,
- meeting에 `index_stale=true`를 기록하고 reindex job을 enqueue한다.

권장:

- 즉시 동기 reindex보다 queue 기반 reindex를 권장한다. LLM 직후 e5 로드가 이어지므로 thermal/메모리 정책 아래에서 실행해야 한다.

필수 테스트:

```bash
.venv/bin/pytest tests/test_pipeline.py -k 'run_llm_steps' -q
.venv/bin/pytest tests/test_routes_reindex.py -q
```

#### P1-3. ChromaDB/FTS5 generation 도입

최종 판정:

- 현재 fail-loud는 맞다.
- 하지만 실패 후 두 저장소가 같은 세대인지 보장하지 않는다.

목표 동작:

- meeting_id별 `index_generation`을 둔다.
- ChromaDB와 FTS5가 같은 generation일 때만 queryable.
- 저장은 staging generation에 쓰고 마지막 commit marker로 promote한다.

필수 테스트:

```bash
.venv/bin/pytest tests/test_embedder.py tests/test_hybrid_search.py tests/test_routes_reindex.py -q
```

#### P1-4. JobQueue atomic claim

현재 동작:

- `get_pending_jobs()` 후 첫 job을 골라 별도 status update.

목표 동작:

- `claim_next_job()`이 SQL transaction으로 queued -> transcribing을 한 번에 처리.
- 상태 update는 `WHERE id=? AND status=?` 조건부로 처리.

필수 테스트:

```bash
.venv/bin/pytest tests/test_job_queue.py tests/test_orchestrator.py -q
```

#### P1-5. AutoProcessing을 JobProcessor 경로로 통합

현재 동작:

- AutoProcessingRunner가 `pipeline.run()`을 직접 호출.

목표 동작:

- auto-processing은 requested_action을 붙여 JobQueue에 넣는다.
- 실행은 JobProcessor만 담당한다.
- WebSocket progress, cancellation, thermal gate, status transition을 공유한다.

필수 테스트:

```bash
.venv/bin/pytest tests/test_auto_processing.py tests/test_orchestrator.py -q
```

### P1: 모델/하드웨어 리소스 안정화

#### P1-6. LLM chain finally unload

현재 동작:

- Corrector는 `keep_loaded=True`.
- Summarizer가 실행되면 일반적으로 unload된다.
- 하지만 summarize checkpoint/skip/Wiki V2 같은 경로는 chain 종료 unload가 명시적이지 않다.

목표 동작:

- correct/summarize/Wiki compile chain 전체를 `try/finally`로 감싸고 `model_manager.unload_model()`을 호출.
- `keep_loaded=True`는 chain 내부에서만 허용.

필수 테스트:

```bash
.venv/bin/pytest tests/test_model_manager.py tests/test_pipeline.py tests/wiki/test_llm_client.py -q
```

#### P1-7. Recorder subprocess와 audio energy streaming

현재 동작:

- ffmpeg stdout/stderr pipe를 열고 wait 중심으로 종료.
- 무음 감지는 전체 WAV를 `float32`로 읽음.

목표 동작:

- `-hide_banner -nostats -loglevel warning`
- stderr/stdout drain task 또는 DEVNULL/log file
- 종료는 `communicate()` 또는 pipe drain 보장
- audio energy는 ffmpeg `volumedetect` 또는 chunked RMS

필수 테스트:

```bash
.venv/bin/pytest tests/test_recorder.py tests/test_audio_quality.py -q
```

### P1/P2: 설정과 보안 계약

#### P1-8. Runtime config reconfigure/restart contract

목표 동작:

- 설정 필드를 다음 셋으로 분류한다.
  - live reload
  - service rebuild required
  - restart required
- LLM model/backend 변경 시 현재 모델 unload 또는 service rebuild.
- 즉시 반영 불가 시 API가 `restart_required=true`를 반환.

필수 테스트:

```bash
.venv/bin/pytest tests/test_user_settings_api.py tests/test_server.py tests/test_config.py -q
```

#### P1-9. Local-only unsafe opt-in

목표 동작:

- 기본값은 유지.
- non-loopback server host 또는 LLM host는 명시적 unsafe opt-in 없이는 거부하거나 경고.
- CORS가 인증이 아님을 UI/docs에 표시.

주의:

- 회사/개인 LAN의 Ollama 서버를 의도적으로 쓰는 사용자가 있을 수 있으므로, 완전 제거보다 opt-in이 낫다.

### P2: 품질/문서 정합성

#### P2-1. STT hallucination filter metric 보강

목표 동작:

- `TranscriptSegment`에 `compression_ratio`를 저장하거나 필터 조건을 명시적으로 비활성화.
- STT quality eval에 repetition, omission, time coverage, no_speech/logprob/compression metric을 포함.

필수 테스트:

```bash
.venv/bin/pytest tests/test_transcriber.py tests/test_hallucination_filter.py tests/test_stt_quality_metrics.py -q
```

#### P2-2. VAD/diarization timeout config 정렬

목표 동작:

- VAD 기본 정책을 하나로 정한다.
  - 권장: 기본 모델 large-v3-turbo는 OFF 유지.
  - 4bit 모델 활성화 시 UI에서 VAD 권장 경고를 제공.
- `pipeline.diarization_timeout_seconds`는 제거하거나 `diarization.timeout_seconds`로 마이그레이션.
- unknown YAML key warning 또는 fail-fast 도입.

필수 테스트:

```bash
.venv/bin/pytest tests/test_config.py tests/test_diarizer.py -q
```

#### P2-3. Health/shutdown 관측성

목표 동작:

- `/api/health`가 component 상태를 포함한다.
  - job_queue
  - search_engine
  - chroma
  - fts
  - pipeline_manager
  - model_loaded
  - thermal_policy
- `HybridSearchEngine.close()`를 추가하고 shutdown에서 호출.

필수 테스트:

```bash
.venv/bin/pytest tests/test_server.py tests/test_hybrid_search.py -q
```

## 5. 최종 실행 순서

### Phase 0. 계약 테스트 먼저 추가

목표:

- 현재 동작을 바꾸기 전에 깨져야 할 테스트를 먼저 만든다.

추가 테스트:

1. 검색 실패 시 ChatEngine이 LLM을 호출하지 않는다.
2. 검색 결과 0건이면 ChatEngine이 LLM을 호출하지 않는다.
3. 삭제 후 ChromaDB/FTS5 purge가 호출된다.
4. 재전사 시작 시 기존 index가 purge 또는 stale 처리된다.
5. skip_llm 후 chunk 전 실패 상태에서 resume이 안전하게 동작한다.
6. run_llm_steps 성공 후 index가 stale/reindex 상태가 된다.
7. correct keep-loaded 후 summarize checkpoint 경로에서도 unload가 호출된다.

### Phase 1. RAG와 삭제 계약 수정

순서:

1. `SearchGroundingStatus` 도입.
2. `ChatResponse`/stream event에 `grounding_status`, `llm_called`, `repair_actions` 추가.
3. 검색 실패/결과 0건에서 LLM 호출 차단.
4. meeting index purge helper 추가.
5. delete/re-transcribe에서 purge 또는 stale 처리.

Phase 1 완료 조건:

- 삭제된 meeting_id가 search/chat reference에 다시 나오지 않는다.
- 검색 실패가 LLM token 사용 없이 사용자에게 드러난다.

### Phase 2. Pipeline 상태와 인덱스 최신성 수정

순서:

1. `PipelineState`에 `available_artifacts` 또는 `index_status` 추가.
2. `skip_llm_steps` pass-through artifact 또는 chunk fallback 구현.
3. `run_llm_steps()` 후 reindex job enqueue.
4. ChromaDB/FTS5 generation marker 설계.

Phase 2 완료 조건:

- `completed_steps`만 보고 산출물 존재를 추정하지 않는다.
- RAG가 stale index를 grounded로 쓰지 않는다.

### Phase 3. 실행 경로 통합

순서:

1. JobQueue atomic claim.
2. meeting-level lock.
3. AutoProcessingRunner를 queue enqueue 방식으로 변경.
4. batch summarize/reindex/retranscribe도 meeting lock 적용.

Phase 3 완료 조건:

- 같은 meeting_id에 대해 두 유지보수 작업이 동시에 산출물을 쓰지 않는다.
- 자동 처리도 수동 처리와 동일한 progress/cancel/thermal 경로를 쓴다.

### Phase 4. 모델/리소스 안정화

순서:

1. LLM chain finally unload.
2. Wiki V2 finally unload.
3. ModelLoadManager key에 config fingerprint 또는 settings 변경 시 unload.
4. Recorder pipe drain.
5. Audio energy streaming.
6. health component 확장.

Phase 4 완료 조건:

- LLM 단계가 끝난 뒤 다음 대형 모델이 예측 가능한 메모리 상태에서 로드된다.
- 장시간 녹음이 pipe buffer와 전체 WAV 로딩에 의존하지 않는다.

### Phase 5. 품질/문서 동기화

순서:

1. STT compression_ratio 저장/비활성 명시.
2. VAD 정책 문서 정렬.
3. diarization timeout 마이그레이션.
4. benchmark limitation 업데이트.
5. `docs/STATUS.md`, `AGENTS.md`, `docs/BENCHMARK.md` 동기화.

## 6. 검증 명령 기록

이번 재검증에서 실행한 명령:

```bash
git status --short
.venv/bin/pytest tests/test_chat.py::TestChatEngine::test_검색_실패_시_컨텍스트_없이_진행 -q
.venv/bin/pytest tests/test_pipeline.py -k 'skip_llm_steps시_정상_완료 or run_llm_steps_correct_summarize_실행' -q
.venv/bin/pytest tests/test_hybrid_search.py -k '벡터만_성공_fts_실패 or fts만_성공_벡터_실패 or 양쪽_모두_빈_결과' -q
.venv/bin/pytest tests/test_routes.py -k '삭제 or 재전사' -q
.venv/bin/pytest tests/test_embedder.py -k 'ChromaDB_저장_실패 or fts_실패 or 기존_데이터_삭제' -q
```

결과:

- Chat 검색 실패 현재 동작 테스트: 1 passed
- Pipeline skip/run_llm 현재 동작 테스트: 2 passed
- HybridSearch degradation 현재 동작 테스트: 3 passed
- Delete/re-transcribe 현재 동작 테스트: 7 passed
- Embedder fail-loud/기존 삭제 관련 선택 테스트: 1 passed

선택 실패:

```bash
.venv/bin/pytest tests/test_routes.py -k 'delete_meeting or re_transcribe' -q
```

결과: 94 deselected, exit code 5. 테스트 실패가 아니라 필터가 실제 한글 테스트 이름과 맞지 않았다. 이후 `-k '삭제 or 재전사'`로 재실행해 통과를 확인했다.

실행하지 않은 명령:

- 전체 `pytest tests/ -v`: 문서 재검증 범위보다 크고 시간이 오래 걸려 생략.
- mypy/ruff: 코드 변경이 없어서 생략.
- native/e2e/benchmark: 로컬 장치/브라우저/모델 실측 범위라 생략.

## 7. 변경 금지와 주의사항

다음은 이번 계획에서 하지 않는다.

- 외부 API 도입
- 모델 기본값 변경
- VAD 전역 ON 전환
- HuggingFace/SSL/네트워크 우회
- 삭제 API를 best-effort purge로 조용히 성공시키기
- 인증 없는 non-loopback 서버 노출을 기본 허용하기
- `keep_loaded=True`를 전역 제거해 성능을 불필요하게 망가뜨리기

## 8. 최종 권고

최종 계획의 첫 PR은 작아야 한다.

권장 첫 PR:

1. `SearchGroundingStatus` 추가
2. 검색 실패/결과 0건에서 ChatEngine LLM 호출 차단
3. 기존 `test_검색_실패_시_컨텍스트_없이_진행` 기대값 변경
4. streaming chat에도 동일 계약 적용

이 PR이 가장 먼저인 이유:

- 데이터 파괴가 없다.
- 외부 의존성이 없다.
- 테스트로 명확하게 증명 가능하다.
- LLM 비용, 메모리, 환각 리스크를 동시에 줄인다.
- 이후 MeetingLifecycleService 작업의 사용자-facing 계약을 선명하게 만든다.

두 번째 PR은 삭제/재전사 index lifecycle이다. 이 PR부터는 ChromaDB/FTS5 저장소를 만지므로 fixture와 rollback/stale 정책을 먼저 확정해야 한다.

이 순서로 가면 제품 신뢰도를 빠르게 올리면서도, 큰 리팩터를 한 번에 밀어 넣는 위험을 피할 수 있다.
