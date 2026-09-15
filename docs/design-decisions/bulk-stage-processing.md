# 로컬 단계별 일괄 처리

## 구현 범위

로컬 `full` 요청 중 같은 STT 모델을 사용하는 대기 회의가 두 건 있으면 다음 순서로
처리할 수 있다. 한 회의 안의 단계 순서는 그대로다.

```text
전사 A → 전사 B
화자분리·병합 A → 화자분리·병합 B
교정·요약 A → 교정·요약 B
검색 인덱싱 A → 검색 인덱싱 B
```

Whisper와 LLM은 각 묶음 안에서 모델을 재사용하고 묶음이 끝나면 정리한다.
pyannote의 회의별 CPU subprocess와 e5의 회의별 CPU/MPS 선택은 유지한다.
동시에 두 회의를 추론하거나 두 대형 모델을 적재하는 기능은 아니다.

`pipeline.bulk_stage_batching`은 시험 기능으로 기본 `false`다. 로컬 모델과 실제
회의에서 검증할 때 `config.yaml`에서 켠다. `bulk_max_items`는 1~2이며 기존 서멀
휴식까지 남은 처리 여유가 두 건보다 적으면 단건 경로를 사용한다. OpenAI 전사,
전사만 요청한 작업, 서로 다른 STT 모델, 원본 없는 텍스트 재개는 기존 경로를 사용한다.

```yaml
pipeline:
  checkpoint_enabled: true
  bulk_stage_batching: true
  bulk_max_items: 2
```

이 방식은 묶음 전체의 모델 전환을 줄인다. 첫 회의의 교정본은 두 번째 회의의 전사와
화자분리도 끝나야 나오므로 단건 결과가 급한 경우에는 기존 순서가 유리하다.

## 중간 저장과 재개

- `PipelineManager.run(stop_after=...)`는 지정 단계까지 체크포인트를 저장한 뒤
  `paused`를 반환한다. `skip_llm_steps`나 완료 표시로 대기를 표현하지 않는다.
- 다음 호출은 저장된 완료 단계 다음부터 실행한다. 이미 지나간 중간 경계의 요청은
  모델을 다시 실행하지 않는다. 순서가 끊긴 완료 단계 목록은 변경하지 않고 거부한다.
- 기존 JobQueue와 파이프라인 체크포인트가 재시작 복구의 기준이다. 앱 종료 시 남은
  작업을 queued로 보존하며, 새 프로세서는 기존 복구 계약을 통해 이어간다.
- SQLite `StageWorkQueue`는 유한 묶음·단계·의존성·입력 fingerprint·모델 snapshot·
  실행 owner/token·시도 횟수를 기록한다. 원본이나 응답 텍스트를 기록하지 않는다.
  작업 claim과 완료는 트랜잭션 및 owner 비교로 보호한다.
- 이 단계 큐는 이번 구현에서 실행 기록이다. 과거 프로세스의 미확인 running 기록을
  경과 시간만 보고 회수하지 않는다. `recover_session`은 호출자가 native 종료를
  확인한 경우에만 쓸 수 있으며 자동 재시작 경로에 연결하지 않았다.
- 회의별 mutation lease, 원본 교체 감지, 기존 no-follow·체크포인트 게시 검사를
  유지한다. 지연 교정의 기존 부분 산출물을 새 단계 큐만 믿고 덮어쓰지 않는다.

## 모델 수명과 취소

동기 모델 로딩과 정리의 기다림을 서버 이벤트 루프 밖으로 옮겼다. MLX LLM과
Whisper의 실제 로딩·추론·캐시 정리는 각각 같은 전용 스레드에서 실행한다.

`ModelLoadManager.residency_scope(name, reuse_key=...)`는 같은 모델을 유지할 정책만
설정한다. 묶음 전체에 걸쳐 모델 잠금을 잡지 않는다. 각 작업은 기존 순서대로
회의 lease와 모델 사용 잠금을 얻는다. 재사용은 이름과 key가 모두 같은 경우만 허용하며,
범위 종료 시 다른 요청이 교체한 모델을 정리하지 않도록 실제 객체 identity도 비교한다.

취소와 timeout은 native 연산 종료를 의미하지 않는다. 로딩 중 취소도 반환될 모델을
받아서 정리할 때까지 admission을 유지한다. 연산 중 취소는 기존 deferred cleanup이
끝날 때까지 모델과 잠금을 보유하며, 새 작업에 겹쳐서 모델을 적재하지 않는다.

## 계측과 캐시

- 매니저 상태에 모델 load/reuse 횟수와 마지막 load/unload/잠금 대기 시간을 추가했다.
  Whisper 매니저 로딩은 모듈 wrapper 생성까지이며, 라이브러리의 실제 가중치 지연
  로딩은 전사 시간에 포함된다. 이 수치를 Whisper 가중치 로딩 비용으로 해석하지 않는다.
- `backend.get_generation_metrics()`는 마지막 생성 시간, 라이브러리가 제공한 입력·
  출력 토큰 수, 처리 속도, MLX peak memory를 반환한다. 미제공 값은 `None`이며
  프롬프트와 응답은 저장하지 않는다. MLX peak는 전체 앱과 subprocess의 RAM이 아니다.
- Gemma의 VLM cache는 라이브러리의 실제 prefix 비교를 사용한다. 전체 prompt를 넣는
  mlx-lm 경로는 이전 요청의 raw KV cache를 이어 쓰지 않고 요청별 새 cache를 사용한다.
- VLM 생성에도 설정한 temperature를 전달한다. 기본값 0.0은 유지한다.
- `scripts/benchmark_bulk_models.py`는 격리된 고정 입력으로 reload/retained를 비교한다.
  로드 비용, 생성 시간, 버전, 출력 hash, 제공된 생성 통계를 별도로 남긴다.

## 검증 범위

안전성 테스트와 실제 모델 실행 결과는 [프로젝트 상태](../STATUS.md)에 기록한다.
짧은 합성 입력의 속도 차이를 긴 회의의 개선율로 일반화하지 않는다. 다른 사용자의
원본 회의와 16GB Mac에서의 장시간 처리, 실제 pyannote·e5까지 포함한 전체 실행은
별도 검증 대상이다. 프로젝트 의존성은 mlx 0.32.2 / mlx-vlm 0.6.17을 유지한다.

설계 근거와 다음 실험은 [효율 개선 분석](../plans/2026-09-15-bulk-efficiency-analysis.md)에 있다.
