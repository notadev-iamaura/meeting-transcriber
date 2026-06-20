# Meeting Transcriber 심층 코드리뷰 합의 제안서

작성일: 2026-06-20
대상 저장소: `/Users/youngouksong/projects/meeting-transcriber`
방식: AI 설계, 백엔드 아키텍처, 하드웨어 리소스 관리 3개 관점의 독립 감사 후 만장일치 합의

## 1. 결론

세 관점이 최종 합의한 핵심 판단은 다음과 같다.

> 이 시스템의 가장 큰 개선 지점은 모델 자체 성능보다, 회의 상태와 산출물, 검색 인덱스, 모델 메모리 수명을 하나의 운영 불변식으로 묶는 것이다.

현재 코드는 기능 폭이 넓고 로컬 우선 설계도 잘 유지하고 있지만, 몇몇 경계에서 "완료 상태"와 실제 산출물이 분리된다. 이 분리는 세 가지 제품 리스크로 이어진다.

1. 삭제되거나 재처리된 회의가 검색/RAG에 남을 수 있다.
2. 검색 근거가 없거나 검색이 실패했는데도 LLM이 일반 지식으로 답할 수 있다.
3. `keep_loaded=True` 최적화가 특정 스킵/체크포인트 경로에서 메모리 상주로 바뀔 수 있다.

따라서 개선 방향은 새 기능 추가가 아니라, 아래 다섯 개 계약을 먼저 고정하는 것이다.

| 합의 계약 | 요지 |
| --- | --- |
| Meeting Lifecycle Contract | 회의 삭제, 재전사, 재요약, 재색인은 DB, 체크포인트, 출력 파일, ChromaDB, FTS5를 함께 갱신한다. |
| RAG Grounding Contract | 검색 실패 또는 근거 0건이면 LLM을 호출하지 않고 "회의 근거 없음"으로 종료한다. |
| Model Residency Contract | `keep_loaded=True`는 연속 LLM 체인 내부 최적화일 뿐이며, 체인 종료/스킵/예외/체크포인트 복원 경로는 반드시 언로드한다. |
| Runtime Config Contract | 설정 변경은 모든 장기 객체에 반영되거나, 명시적으로 `restart_required`로 응답한다. |
| Resource Boundary Contract | 장시간 외부 프로세스와 대형 오디오/모델은 스트리밍, 타임아웃, 관측 가능한 상태를 가진다. |

## 2. 감사 범위와 근거

읽은 주요 문서:

- [AGENTS.md](/Users/youngouksong/projects/meeting-transcriber/AGENTS.md:1)
- [docs/STATUS.md](/Users/youngouksong/projects/meeting-transcriber/docs/STATUS.md:1)
- [docs/BENCHMARK.md](/Users/youngouksong/projects/meeting-transcriber/docs/BENCHMARK.md:1)
- [harness/README.md](/Users/youngouksong/projects/meeting-transcriber/harness/README.md:1)

읽은 주요 코드 영역:

- 파이프라인 및 상태: [core/pipeline.py](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1)
- 모델 수명: [core/model_manager.py](/Users/youngouksong/projects/meeting-transcriber/core/model_manager.py:1)
- 작업 큐/오케스트레이터: [core/job_queue.py](/Users/youngouksong/projects/meeting-transcriber/core/job_queue.py:1), [core/orchestrator.py](/Users/youngouksong/projects/meeting-transcriber/core/orchestrator.py:1)
- 검색/RAG: [search/hybrid_search.py](/Users/youngouksong/projects/meeting-transcriber/search/hybrid_search.py:1), [search/chat.py](/Users/youngouksong/projects/meeting-transcriber/search/chat.py:1)
- STT/LLM/임베딩 단계: [steps/transcriber.py](/Users/youngouksong/projects/meeting-transcriber/steps/transcriber.py:1), [steps/corrector.py](/Users/youngouksong/projects/meeting-transcriber/steps/corrector.py:1), [steps/summarizer.py](/Users/youngouksong/projects/meeting-transcriber/steps/summarizer.py:1), [steps/embedder.py](/Users/youngouksong/projects/meeting-transcriber/steps/embedder.py:1)
- API 설정/회의/재색인: [api/routers/settings.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/settings.py:1), [api/routers/meeting_detail.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/meeting_detail.py:1), [api/routers/reindex.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/reindex.py:1)
- 녹음/서멀: [steps/recorder.py](/Users/youngouksong/projects/meeting-transcriber/steps/recorder.py:1), [core/thermal_manager.py](/Users/youngouksong/projects/meeting-transcriber/core/thermal_manager.py:1)

비범위:

- 새 프로덕션 의존성 추가
- 외부 API 도입
- HuggingFace/SSL/네트워크 우회
- 모델 기본값 즉시 변경
- UI 개편

## 3. 3인 토론 요약

### 3.1 AI 설계 관점

AI 설계 관점의 주장은 "이 제품은 회의 근거 기반 답변 제품이므로, 근거가 없으면 답하지 않는 것이 품질의 출발점"이라는 것이다.

핵심 근거:

- 채팅 시스템 프롬프트는 "회의 내용에 없는 정보는 추측하지 마세요"라고 한다. [core/defaults/prompts.default.json](/Users/youngouksong/projects/meeting-transcriber/core/defaults/prompts.default.json:9)
- 하지만 검색 컨텍스트가 없으면 사용자 프롬프트는 "알고 있는 범위에서 답변"을 요구한다. [search/chat.py](/Users/youngouksong/projects/meeting-transcriber/search/chat.py:270)
- 검색 실패도 경고 후 컨텍스트 없이 진행한다. [search/chat.py](/Users/youngouksong/projects/meeting-transcriber/search/chat.py:618)

AI 설계 관점은 RAG 품질 개선보다 먼저 "무근거 응답 금지"를 P0로 보아야 한다고 주장했다.

### 3.2 백엔드 아키텍처 관점

백엔드 관점의 주장은 "회의의 상태 전이는 DB 상태가 아니라 산출물 전체의 원자적 상태여야 한다"는 것이다.

핵심 근거:

- 회의 삭제는 Job DB와 오디오 quarantine을 처리하지만 체크포인트, 출력, ChromaDB, FTS5 정리를 같은 계약으로 다루지 않는다. [api/routers/meeting_detail.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/meeting_detail.py:1043)
- 재전사는 체크포인트와 일부 출력만 삭제하고 검색 인덱스 정리는 포함하지 않는다. [api/routers/meeting_detail.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/meeting_detail.py:740)
- `skip_llm_steps` 또는 메모리 부족 경로는 `correct`/`summarize`를 완료 단계로 기록하지만 해당 체크포인트를 만들지 않는다. [core/pipeline.py](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1457)
- 이후 `chunk`/`embed`는 `corrected_result` 또는 복원 가능한 체크포인트가 있다고 가정한다. [core/pipeline.py](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1588)

백엔드 관점은 Meeting Lifecycle Contract를 최우선으로 고정해야 한다고 주장했다.

### 3.3 하드웨어 리소스 관점

하드웨어 관점의 주장은 "`keep_loaded=True`는 속도 최적화이지만, MacBook Air 16GB에서는 명시적 종료 조건 없이는 운영 리스크"라는 것이다.

핵심 근거:

- Corrector는 Summarizer 재사용을 위해 LLM을 유지한다. [steps/corrector.py](/Users/youngouksong/projects/meeting-transcriber/steps/corrector.py:497)
- ModelLoadManager는 `keep_loaded=True`이고 예외가 없으면 언로드를 건너뛴다. [core/model_manager.py](/Users/youngouksong/projects/meeting-transcriber/core/model_manager.py:462)
- Summarizer가 체크포인트로 스킵되거나 LLM 단계가 중간에 스킵되면 일반 언로드 경로가 실행되지 않을 수 있다. [core/pipeline.py](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1968)
- Wiki V2도 `keep_loaded=True`를 사용하며 호출자가 명시적 unload 책임을 진다는 주석이 있다. [core/wiki/llm_client.py](/Users/youngouksong/projects/meeting-transcriber/core/wiki/llm_client.py:390)

하드웨어 관점은 Model Residency Contract를 파이프라인과 Wiki 모두에 적용해야 한다고 주장했다.

### 3.4 끝장토론에서 합의된 설득 흐름

쟁점 1: "검색 실패 시에도 친절하게 LLM 답변을 주는 것이 낫지 않은가?"

- AI 설계: 이 제품은 회의 기반 답변 제품이므로 일반 지식 답변은 오답보다 더 위험하다.
- 백엔드: 검색 실패와 검색 결과 0건은 서로 다른 상태여야 하며, API 응답에 repair/reindex 안내를 포함하면 UX를 해치지 않는다.
- 하드웨어: 근거 없는 LLM 호출을 막으면 메모리와 발열도 줄어든다.
- 만장일치: LLM 호출 전 `search_status` 게이트를 둔다. 실패/0건이면 LLM을 호출하지 않는다.

쟁점 2: "LLM 후처리 후 chunk/embed까지 다시 돌리면 느려지지 않는가?"

- 백엔드: 느리더라도 검색 인덱스가 원문 기반이면 제품 신뢰가 깨진다.
- AI 설계: 요약/교정 품질을 반영하지 않는 RAG는 사용자에게 설명하기 어렵다.
- 하드웨어: JobQueue와 서멀 게이트 안에서 순차 실행하면 리소스 문제는 관리 가능하다.
- 만장일치: 온디맨드 LLM 완료 후 검색 인덱스 재생성 또는 `index_stale=true` 표시를 필수로 한다.

쟁점 3: "`keep_loaded=True`를 없애면 성능이 나빠지지 않는가?"

- 하드웨어: 완전 제거가 아니라 체인 범위가 명시되어야 한다.
- 백엔드: `finally` 안전망과 체인 토큰을 두면 최적화와 안정성을 양립할 수 있다.
- AI 설계: STT, LLM, e5가 번갈아 뜨는 제품에서 예측 가능한 메모리가 더 중요하다.
- 만장일치: `keep_loaded=True`는 명시적 LLM chain 내부에서만 허용하고, 체인 종료 시 강제 언로드한다.

## 4. 우선순위별 발견 사항

### P0. 삭제/재처리된 회의가 검색 근거로 남을 수 있다

위험:

- 삭제된 회의 내용이 RAG 검색으로 노출될 수 있다.
- 재전사/재요약 후 검색 인덱스가 이전 텍스트를 계속 사용할 수 있다.
- 사용자 입장에서는 "삭제했다"와 "검색에서 사라졌다"가 같은 의미인데, 현재는 그렇지 않을 수 있다.

근거:

- 삭제 API는 DB 삭제와 오디오 quarantine 중심이다. [api/routers/meeting_detail.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/meeting_detail.py:1086)
- 재전사는 체크포인트와 `corrected.json`, `summary.md` 일부만 삭제한다. [api/routers/meeting_detail.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/meeting_detail.py:746)
- ChromaDB 저장은 meeting_id 기존 데이터를 먼저 삭제 후 새로 넣지만, 삭제 API와 공용 수명주기 서비스로 묶여 있지 않다. [steps/embedder.py](/Users/youngouksong/projects/meeting-transcriber/steps/embedder.py:340)

제안:

1. `MeetingLifecycleService`를 만든다.
2. `delete_meeting`, `retranscribe`, `reindex`, `run_llm_steps`, auto-processing이 모두 이 서비스를 통해 산출물과 인덱스를 갱신하게 한다.
3. 삭제 시 DB, checkpoints, outputs, ChromaDB, FTS5를 모두 정리한다.
4. 재전사/재요약 시 기존 index를 즉시 삭제하거나 `index_stale=true`로 표시하고 새 index 성공 후만 ready로 전환한다.

필수 테스트:

- 삭제 후 `HybridSearchEngine.search(meeting_id_filter=id)`가 0건을 반환한다.
- 재전사 시작 직후 기존 인덱스가 `stale` 또는 제거 상태가 된다.
- ChromaDB와 FTS5 중 하나만 남은 반쪽 상태를 health/reindex가 감지한다.

### P0. `skip_llm_steps`가 상태와 산출물을 불일치시킨다

위험:

- `correct`/`summarize`가 완료 처리되지만 checkpoint가 없어 resume 또는 이후 단계가 실패할 수 있다.
- "전사만 진행" 모드가 실제로는 chunk/embed 단계와 결합될 때 깨지기 쉽다.

근거:

- LLM 단계 스킵 시 `state.completed_steps.append(step.value)`가 실행된다. [core/pipeline.py](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1483)
- `correct` 스킵은 메모리상의 `merged_result` 패스스루일 뿐 `correct.json`을 저장하지 않는다. [core/pipeline.py](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1471)
- `chunk`는 `corrected_result`가 있다고 assert한다. [core/pipeline.py](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1591)

제안:

1. 스킵된 단계는 `completed_steps`가 아니라 `skipped_steps`와 `available_artifacts`로 표현한다.
2. `correct` 스킵 시 패스스루 `correct.json`을 저장하거나, `chunk`가 명시적으로 `merge.json`을 입력으로 받을 수 있게 한다.
3. `summarize` 스킵은 "회의록 없음" 산출물 상태를 별도 값으로 기록한다.
4. resume은 상태 파일보다 실제 체크포인트 존재 여부를 우선 검증한다.

필수 테스트:

- `skip_llm_steps=True` 후 resume이 실패하지 않아야 한다.
- `correct` checkpoint가 없는데 `completed_steps`에만 있는 상태를 복구하거나 실패 메시지로 명확히 차단해야 한다.

### P0. RAG가 검색 실패/무근거 상태에서도 LLM 답변을 생성한다

위험:

- 회의에 없는 내용을 답변할 수 있다.
- 시스템 프롬프트와 사용자 프롬프트가 충돌한다.
- 검색 장애가 "그럴듯한 답변"으로 숨겨진다.

근거:

- 검색 실패는 warning 후 컨텍스트 없이 진행한다. [search/chat.py](/Users/youngouksong/projects/meeting-transcriber/search/chat.py:618)
- 컨텍스트가 없으면 "알고 있는 범위에서 답변" 프롬프트를 만든다. [search/chat.py](/Users/youngouksong/projects/meeting-transcriber/search/chat.py:270)
- 시스템 프롬프트는 회의 내용에 없는 정보 추측 금지를 요구한다. [core/defaults/prompts.default.json](/Users/youngouksong/projects/meeting-transcriber/core/defaults/prompts.default.json:10)

제안:

1. 검색 실패, index unavailable, 결과 0건을 구분한 `SearchGroundingStatus`를 만든다.
2. `status != grounded`면 LLM을 호출하지 않는다.
3. 사용자에게는 "해당 회의 근거를 찾지 못했습니다"와 재색인/필터 해제 안내를 반환한다.
4. 스트리밍 채팅도 같은 계약을 적용한다.

필수 테스트:

- 검색 엔진 예외 시 `_call_llm_chat`이 호출되지 않는다.
- 검색 결과 0건이면 references는 빈 배열이고 answer는 무근거 안내여야 한다.
- 시스템 프롬프트와 사용자 프롬프트에 상충 문구가 없어야 한다.

### P1. 온디맨드 LLM 후처리 후 검색 인덱스가 갱신되지 않는다

위험:

- 사용자는 교정/요약이 완료됐다고 보지만 RAG는 이전 raw/merge 기반 chunk를 사용할 수 있다.

근거:

- `run_llm_steps()`는 `correct`와 `summarize`만 실행한다. [core/pipeline.py](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1867)
- 완료 후 상태는 `completed`로 바뀌지만 chunk/embed 재실행은 없다. [core/pipeline.py](/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:1996)

제안:

1. `run_llm_steps()` 완료 후 `chunk`와 `embed`를 이어 실행한다.
2. 또는 API 응답에 `index_stale=true`를 반환하고 별도 reindex job을 큐잉한다.
3. UI에는 "요약 완료, 검색 인덱스 재생성 중" 상태를 노출한다.

### P1. 런타임 설정 변경이 장기 객체에 일관되게 반영되지 않는다

위험:

- 설정 API는 "모델 변경은 다음 LLM 호출 시 적용"이라고 말하지만, 기존 `PipelineManager`, `ChatEngine`, `HybridSearchEngine`, `ModelLoadManager`가 이전 config/model을 계속 들고 있을 수 있다.

근거:

- 설정 업데이트는 `request.app.state.config`를 교체한다. [api/routers/settings.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/settings.py:619)
- lifecycle/auto-processing scheduler만 부분적으로 update된다. [api/routers/settings.py](/Users/youngouksong/projects/meeting-transcriber/api/routers/settings.py:622)
- ModelLoadManager는 같은 모델 key면 현재 인스턴스를 재사용한다. [core/model_manager.py](/Users/youngouksong/projects/meeting-transcriber/core/model_manager.py:312)

제안:

1. 설정 변경 필드를 `live_reloadable`, `requires_service_rebuild`, `requires_restart`로 분류한다.
2. LLM/STT/embedding 모델 변경 시 ModelLoadManager의 현재 모델을 언로드하고 모델 key에 config fingerprint를 포함한다.
3. `PipelineManager`, `ChatEngine`, `HybridSearchEngine`에 `update_config()` 또는 재생성 경로를 둔다.
4. 즉시 반영할 수 없는 값은 API가 `restart_required=true`를 반환한다.

### P1. JobQueue claim과 상태 전이가 원자적이지 않다

위험:

- 프로세서가 복수 실행되거나 자동 처리/수동 처리 경로가 겹치면 같은 queued job을 중복 claim할 수 있다.
- 상태 검증 시점과 UPDATE 시점 사이에 다른 전이가 끼어들 수 있다.

근거:

- pending job 조회는 단순 SELECT다. [core/job_queue.py](/Users/youngouksong/projects/meeting-transcriber/core/job_queue.py:509)
- `update_status`는 현재 상태를 읽은 뒤, WHERE 조건 없이 id만으로 update한다. [core/job_queue.py](/Users/youngouksong/projects/meeting-transcriber/core/job_queue.py:655)

제안:

1. `claim_next_job(expected_status=queued, next_status=transcribing)`를 단일 SQL 트랜잭션으로 구현한다.
2. 모든 상태 전이는 `WHERE id=? AND status=?`로 조건부 update한다.
3. update rowcount가 0이면 stale transition으로 실패시킨다.
4. JobProcessor, AutoProcessingRunner, batch API가 같은 claim API를 사용한다.

### P1. 자동 처리가 JobProcessor/thermal/progress 경로를 우회한다

위험:

- 자동 처리 작업이 JobQueue 상태, WebSocket 진행률, thermal gate, cancellation과 분리된다.
- 수동 작업과 자동 작업이 같은 meeting_id를 동시에 처리할 수 있다.

근거:

- AutoProcessingRunner가 `pipeline.run()`을 직접 호출한다. [core/auto_processing.py](/Users/youngouksong/projects/meeting-transcriber/core/auto_processing.py:164)
- AutoProcessingScheduler는 runner를 직접 구성한다. [core/auto_processing_scheduler.py](/Users/youngouksong/projects/meeting-transcriber/core/auto_processing_scheduler.py:88)

제안:

1. 자동 처리는 실제 실행 대신 JobQueue에 requested_action을 넣는다.
2. 실행은 JobProcessor 단일 경로로 통일한다.
3. auto-processing은 "몇 건 큐잉했는지"를 보고하고, 완료는 기존 progress/WebSocket으로 추적한다.

### P1. 검색 인덱스 저장이 ChromaDB/FTS5 원자성을 갖지 않는다

위험:

- ChromaDB 저장 성공 후 FTS5 저장 실패 또는 반대 상황에서 검색 품질이 조용히 떨어진다.
- reindex/health가 한쪽 저장소만 보고 정상으로 판단할 수 있다.

근거:

- embed 단계는 ChromaDB 저장 후 FTS5 저장을 순차 실행한다. [steps/embedder.py](/Users/youngouksong/projects/meeting-transcriber/steps/embedder.py:649)
- 벡터 검색 실패는 예외를 로깅하고 빈 결과를 반환한다. [search/hybrid_search.py](/Users/youngouksong/projects/meeting-transcriber/search/hybrid_search.py:401)

제안:

1. meeting_id별 `index_generation` 또는 `index_version` 메타데이터를 둔다.
2. ChromaDB와 FTS5 모두 같은 generation을 가진 경우만 queryable로 본다.
3. 저장은 staging 후 commit marker를 쓰는 방식으로 바꾼다.
4. 검색 실패를 빈 결과와 구분해 `degraded` 또는 `unavailable`로 반환한다.

### P1. LLM `keep_loaded=True`가 체인 종료를 보장하지 않는다

위험:

- Gemma 4 E4B 또는 EXAONE이 계속 상주하면 다음 STT/e5/pyannote 단계의 가용 메모리가 줄어든다.
- 16GB MacBook Air에서 swap, thermal throttle, 실패율이 증가할 수 있다.

근거:

- Corrector는 `keep_loaded=True`로 LLM을 유지한다. [steps/corrector.py](/Users/youngouksong/projects/meeting-transcriber/steps/corrector.py:500)
- ModelLoadManager는 정상 종료 시 언로드하지 않는다. [core/model_manager.py](/Users/youngouksong/projects/meeting-transcriber/core/model_manager.py:465)
- Summarizer의 acquire는 일반 언로드 경로지만, checkpoint 복원 시 실행되지 않을 수 있다. [steps/summarizer.py](/Users/youngouksong/projects/meeting-transcriber/steps/summarizer.py:561)

제안:

1. `PipelineManager`에 LLM chain context를 둔다.
2. correct와 summarize 전체를 `try/finally`로 감싸고 finally에서 `unload_model("exaone")` 또는 현재 LLM key unload를 호출한다.
3. `keep_loaded=True` 사용은 chain context 내부에서만 허용한다.
4. Wiki V2도 compile wrapper finally에서 unload한다.

필수 테스트:

- correct 성공 후 summarize checkpoint가 이미 있어도 LLM이 내려간다.
- summarize timeout/skip/예외 후 LLM이 내려간다.
- Wiki V2 compile 실패/성공 모두 unload를 호출한다.

### P1. 장시간 녹음 ffmpeg와 오디오 품질 검사가 리소스 위험을 가진다

위험:

- ffmpeg stderr/stdout pipe가 drain되지 않으면 긴 녹음 중 버퍼 포화로 멈출 수 있다.
- 녹음 후 무음 감지가 전체 WAV를 `float32`로 읽어 장시간 회의에서 큰 메모리를 할당한다.

근거:

- 장치 감지는 `communicate()`에 timeout이 없다. [steps/recorder.py](/Users/youngouksong/projects/meeting-transcriber/steps/recorder.py:253)
- 녹음 프로세스는 stdout/stderr를 PIPE로 연다. [steps/recorder.py](/Users/youngouksong/projects/meeting-transcriber/steps/recorder.py:628)
- 종료 시 `wait()`만 호출한다. [steps/recorder.py](/Users/youngouksong/projects/meeting-transcriber/steps/recorder.py:906)
- 무음 감지는 전체 파일을 읽는다. [steps/recorder.py](/Users/youngouksong/projects/meeting-transcriber/steps/recorder.py:298)

제안:

1. ffmpeg에 `-hide_banner -nostats -loglevel warning`을 적용한다.
2. stderr/stdout은 DEVNULL, 로그 파일, 또는 drain task로 처리한다.
3. 종료는 `communicate()` 기반으로 pipe를 비운다.
4. 무음 감지는 ffmpeg `volumedetect` 또는 chunked RMS로 바꾼다.

### P1. STT 환각 필터와 chunk overlap 설정이 의도대로 작동하지 않는다

위험:

- compression_ratio 기반 환각 필터가 실제 segment에 값이 없으면 무력화된다.
- `overlap_tokens` 설정이 있지만 청크 생성에서 경계 오버랩이 사실상 구현되지 않아 RAG boundary recall이 낮아질 수 있다.

근거:

- hallucination filter는 `compression_ratio`를 읽지만 기본값 0.0을 사용한다. [steps/hallucination_filter.py](/Users/youngouksong/projects/meeting-transcriber/steps/hallucination_filter.py:195)
- chunk split 함수는 `overlap_tokens` 인자를 받는다. [steps/chunker.py](/Users/youngouksong/projects/meeting-transcriber/steps/chunker.py:270)
- 확인한 범위에서 마지막 작은 청크 병합은 있지만 경계 오버랩 생성은 보이지 않는다. [steps/chunker.py](/Users/youngouksong/projects/meeting-transcriber/steps/chunker.py:407)

제안:

1. `TranscriptSegment`에 compression_ratio를 저장하거나 필터 설정에서 해당 조건을 비활성 상태로 명시한다.
2. chunk overlap을 실제로 구현하고, 중복 텍스트가 검색 결과에 과도하게 노출되지 않도록 chunk metadata를 둔다.
3. STT 품질 테스트에 omission, repetition, compression_ratio, coverage metric을 포함한다.

### P1. 로컬 전용/설정 정책이 override 경로에서 흐려진다

위험:

- 제품 설명은 로컬/localhost를 전제로 하지만, 환경변수나 설정으로 host를 넓히면 LAN 노출 가능성이 있다.
- 사용자는 CORS를 인증 경계로 오해할 수 있다.

제안:

1. 기본 host는 loopback으로 강제한다.
2. `0.0.0.0` 또는 LAN host bind는 `MT_ALLOW_NON_LOOPBACK=true` 같은 별도 명시 플래그가 있어야 한다.
3. 설정 UI와 docs에 "CORS는 인증이 아니다"를 분명히 둔다.

### P2. VAD와 diarization timeout 문서/설정이 충돌한다

위험:

- AGENTS와 benchmark 일부는 VAD ON을 기본처럼 설명하지만 실제 config는 OFF다.
- `pipeline.diarization_timeout_seconds: 600`은 실제 `DiarizationConfig.timeout_seconds`와 다른 위치에 있다.

근거:

- config의 VAD 기본값은 OFF다. [config.yaml](/Users/youngouksong/projects/meeting-transcriber/config.yaml:217)
- pipeline 아래에 diarization timeout이 있다. [config.yaml](/Users/youngouksong/projects/meeting-transcriber/config.yaml:146)
- DiarizationConfig의 timeout 기본은 별도 필드다. [config.py](/Users/youngouksong/projects/meeting-transcriber/config.py:297)

제안:

1. VAD 정책을 "기본 large-v3-turbo는 OFF, 4bit 모델 활성화 시 경고/프리셋 ON"처럼 명확히 정한다.
2. diarization timeout은 `diarization.timeout_seconds`로 옮긴다.
3. 알 수 없는 YAML key를 경고 또는 실패로 처리하는 config validation을 도입한다.

### P2. health, shutdown, thermal 관측성이 약하다

위험:

- `/api/health`는 항상 `ok`에 가깝고, Chroma/FTS/job queue/model/cache 상태를 노출하지 않는다.
- HybridSearchEngine은 Chroma/FTS 연결을 캐시하지만 server shutdown에서 close하지 않는다.
- ThermalManager는 Apple Silicon 온도를 실제로 읽지 못해 batch-count 기반 쿨다운에 의존한다.

근거:

- health 응답은 status, uptime, version 중심이다. [api/server.py](/Users/youngouksong/projects/meeting-transcriber/api/server.py:558)
- HybridSearchEngine은 Chroma client와 FTS connection을 캐시한다. [search/hybrid_search.py](/Users/youngouksong/projects/meeting-transcriber/search/hybrid_search.py:612)
- shutdown은 JobQueue close는 하지만 search engine close는 보이지 않는다. [api/server.py](/Users/youngouksong/projects/meeting-transcriber/api/server.py:405)
- ThermalManager의 temperature reader는 현재 None을 반환한다. [core/thermal_manager.py](/Users/youngouksong/projects/meeting-transcriber/core/thermal_manager.py:237)

제안:

1. `/api/health`를 `ok/degraded/error`와 components로 확장한다.
2. `HybridSearchEngine.close()`를 만들고 shutdown에서 호출한다.
3. 쿨다운은 완료 이벤트를 지연시키지 말고, 완료 통지 후 다음 job 시작 전에 대기한다.
4. thermal status에 "temperature_unavailable, batch_policy_active"를 노출한다.

## 5. 제안 아키텍처

### 5.1 MeetingLifecycleService

목표:

- 회의 단위 변경을 한 경로로 모은다.
- DB, 체크포인트, outputs, ChromaDB, FTS5를 같은 lock 아래에서 처리한다.

초기 API 초안:

```python
class MeetingLifecycleService:
    async def delete_meeting(self, meeting_id: str) -> DeleteResult: ...
    async def prepare_retranscribe(self, meeting_id: str) -> LifecycleResult: ...
    async def mark_index_stale(self, meeting_id: str, reason: str) -> None: ...
    async def replace_index(self, meeting_id: str, chunks: list[EmbeddedChunk]) -> IndexCommitResult: ...
    async def purge_index(self, meeting_id: str) -> None: ...
```

핵심 원칙:

- meeting_id별 asyncio lock을 둔다.
- 작업 중인 meeting은 삭제/재색인/재요약을 409로 차단하거나 같은 queue로 serialize한다.
- ChromaDB와 FTS5는 같은 generation marker를 공유한다.

### 5.2 SearchGroundingStatus

목표:

- "검색 결과 없음", "검색 실패", "인덱스 미구성", "정상 근거 있음"을 API 레벨에서 구분한다.

응답 초안:

```json
{
  "answer": "관련 회의 근거를 찾지 못했습니다. 필터를 해제하거나 검색 인덱스를 재생성해 주세요.",
  "grounding_status": "no_results",
  "llm_called": false,
  "references": [],
  "repair_actions": ["clear_filters", "reindex"]
}
```

스트리밍도 첫 이벤트로 grounding status를 보내고, grounded가 아니면 token stream을 열지 않는다.

### 5.3 Model Residency Chain

목표:

- LLM 연속 호출 성능 최적화는 유지하되, 끝나는 시점이 코드로 보장되게 한다.

초안:

```python
async with model_manager.residency_chain("exaone"):
    corrected = await corrector.correct(..., keep_loaded=True)
    summary = await summarizer.summarize(...)
# __aexit__에서 반드시 unload
```

단기 구현은 더 단순하게 시작할 수 있다.

```python
try:
    corrected = await self._run_step_correct(...)
    await self._run_step_summarize(...)
finally:
    await self._model_manager.unload_if_current("exaone")
```

### 5.4 Runtime Config Reconfigure

목표:

- 설정 저장 메시지가 실제 런타임 상태와 일치하게 한다.

분류 예시:

| 설정 | 처리 |
| --- | --- |
| theme, lifecycle schedule | live reload |
| auto_processing schedule | live reload + scheduler restart |
| LLM model/backend | unload current LLM + rebuild pipeline/chat backends |
| STT model | 다음 job부터 적용, 현재 job에는 적용 안 됨을 명시 |
| server host/port, base dir | restart required |

### 5.5 Resource Boundary

목표:

- 장시간 녹음, 오디오 품질 검사, 모델 로드/언로드가 메모리와 file descriptor를 누수하지 않게 한다.

필수 규칙:

- ffmpeg subprocess pipe는 방치하지 않는다.
- 대형 WAV 품질 검사는 전체 로딩하지 않는다.
- 모델 언로드 후 RSS/Metal cache 변화는 로그와 테스트 hook으로 관측한다.
- `peak_ram_limit_gb`가 운영 상한인지 경고선인지 config/docs/test에서 같은 의미를 가진다.

## 6. 실행 로드맵

### Wave 0. 회귀 테스트와 관측 포인트 고정

목표: 동작을 바꾸기 전 실패를 재현한다.

추가할 테스트:

- `test_chat_no_context_does_not_call_llm`
- `test_chat_search_error_returns_grounding_error`
- `test_delete_meeting_purges_search_indexes`
- `test_retranscribe_marks_index_stale`
- `test_skip_llm_resume_has_valid_artifact_contract`
- `test_run_llm_steps_marks_index_stale_or_reindexes`
- `test_keep_loaded_unloads_when_summarize_checkpoint_exists`
- `test_settings_model_change_unloads_cached_model`
- `test_job_queue_atomic_claim`

### Wave 1. P0 신뢰 계약 수정

목표: 삭제/재처리/RAG 무근거 응답을 먼저 막는다.

작업:

1. `SearchGroundingStatus` 도입
2. ChatEngine LLM 호출 게이트
3. 삭제/재전사 시 index purge/stale 처리
4. `skip_llm_steps` artifact contract 수정

권장 검증:

```bash
.venv/bin/pytest tests/test_chat.py tests/test_hybrid_search.py tests/test_embedder.py -q
.venv/bin/pytest tests/test_pipeline.py tests/test_routes.py tests/test_routes_reindex.py -q
```

### Wave 2. 상태 전이와 실행 경로 통합

목표: 수동/자동/배치 작업이 같은 상태 머신을 사용한다.

작업:

1. JobQueue atomic claim
2. AutoProcessingRunner를 JobQueue 큐잉 방식으로 변경
3. meeting_id lock 도입
4. on-demand LLM 후 reindex 또는 stale marker 강제

권장 검증:

```bash
.venv/bin/pytest tests/test_job_queue.py tests/test_orchestrator.py tests/test_auto_processing.py -q
.venv/bin/pytest tests/test_routes_meetings_batch.py tests/test_routes_reindex.py -q
```

### Wave 3. 모델/하드웨어 리소스 안정화

목표: Apple Silicon 16GB 운영 불변식을 테스트 가능한 규칙으로 바꾼다.

작업:

1. LLM chain finally unload
2. Wiki V2 unload 보장
3. ffmpeg pipe drain과 timeout
4. chunked audio energy check
5. STT/MLX unload 실측 스크립트 추가
6. thermal 완료 이벤트 지연 제거

권장 검증:

```bash
.venv/bin/pytest tests/test_model_manager.py tests/test_pipeline.py tests/wiki/test_llm_client.py -q
.venv/bin/pytest tests/test_recorder.py tests/test_audio_quality.py tests/test_thermal_manager.py -q
```

### Wave 4. 품질 평가와 문서 동기화

목표: 모델 품질과 운영 문서의 충돌을 줄인다.

작업:

1. VAD 정책 정리
2. diarization timeout config 위치 정리
3. STT hallucination filter metric 보강
4. chunk overlap 구현 또는 config 제거
5. benchmark limitation 보강
6. `docs/STATUS.md`, `AGENTS.md`, `docs/BENCHMARK.md` 동기화

권장 검증:

```bash
.venv/bin/pytest tests/test_config.py tests/test_diarizer.py -q
.venv/bin/pytest tests/test_transcriber.py tests/test_hallucination_filter.py tests/test_chunker.py -q
.venv/bin/pytest -m harness -q
```

## 7. 릴리스 게이트 제안

P0/P1 수정 PR마다 최소 게이트:

```bash
ruff check .
ruff format --check .
mypy config.py api core steps search ui security --no-error-summary
.venv/bin/pytest tests/ -v --tb=short
.venv/bin/pytest -m harness -q
```

변경 영역별 추가 게이트:

| 변경 영역 | 추가 명령 |
| --- | --- |
| RAG/search | `.venv/bin/pytest tests/test_chat.py tests/test_hybrid_search.py tests/test_embedder.py -q` |
| pipeline/checkpoint | `.venv/bin/pytest tests/test_pipeline.py tests/test_job_queue.py tests/test_orchestrator.py -q` |
| settings/runtime config | `.venv/bin/pytest tests/test_user_settings_api.py tests/test_server.py tests/test_config.py -q` |
| recorder/audio | `.venv/bin/pytest tests/test_recorder.py tests/test_audio_quality.py tests/test_coreaudio_helper.py -q` |
| hardware/native | `.venv/bin/pytest -m native tests/native/test_preflight_native.py -v` |
| UI visible behavior | `pytest -m e2e tests/test_e2e_edit_playwright.py -v` |

실측 벤치마크는 PR마다 필수로 두기보다는 release candidate에서 실행한다.

```bash
.venv/bin/python scripts/benchmark_llm.py
.venv/bin/python scripts/benchmark_top2_precise.py
.venv/bin/python scripts/benchmark_stt.py
```

## 8. 만장일치 최종 권고

세 전문 관점은 아래 순서를 만장일치로 권고한다.

1. 먼저 RAG 무근거 응답을 차단한다. 검색 실패와 결과 0건은 LLM 호출 없이 명시 응답한다.
2. 회의 삭제/재전사/재요약/재색인을 `MeetingLifecycleService`로 통합한다.
3. `skip_llm_steps`와 `run_llm_steps`의 상태/산출물 계약을 고친다.
4. `keep_loaded=True`를 명시적 LLM chain 범위로 제한하고 finally unload를 보장한다.
5. JobQueue atomic claim과 meeting-level lock으로 실행 경로를 하나로 모은다.
6. ChromaDB/FTS5 index generation을 도입해 반쪽 인덱스를 queryable로 보지 않는다.
7. ffmpeg 장시간 프로세스와 오디오 품질 검사를 스트리밍/타임아웃 기반으로 바꾼다.
8. VAD, diarization timeout, local-only host 정책을 문서와 config에서 하나의 사실로 정렬한다.

이 순서는 제품 신뢰도, 데이터 안전성, 하드웨어 안정성을 동시에 개선한다. 모델 성능 튜닝이나 UI 확장은 이 기반을 고정한 뒤 진행하는 것이 맞다.
