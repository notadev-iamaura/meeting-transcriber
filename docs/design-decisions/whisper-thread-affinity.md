# Whisper MLX 스레드 호환성

## 조사 범위

2026-09-15, `20e4806`, Apple Silicon Python 3.12에서 mlx/metal 0.32.2,
mlx-vlm 0.6.17, mlx-whisper 0.4.3으로 조사했다. 제보자의 원본 회의와 로그는
다른 장비에 있어 접근하지 않았다. 아래 결과는 로컬 재현 결과다.

## 확인한 원인

`ModelLoadManager.acquire()`는 호출을 직렬화하지만 `asyncio.to_thread()`가
항상 같은 OS 스레드를 선택하지는 않는다. 첫 beam 시도에서 Whisper의 전역
`ModelHolder`에 모델이 저장된 뒤 `NotImplementedError`가 발생한다.
폴백이 다른 스레드로 배정되면 이전 스레드의 지연 연산을 평가하다 실패한다.

실제 캐시 모델과 1초 무음 입력을 사용했다. 앱의 `acquire()`와
`await_native_inference()`를 거치되 각 호출의 기본 executor를 별도
`ThreadPoolExecutor(max_workers=1)`로 바꾸어 스레드 변경을 강제했다.
첫 호출은 beam_size=5, 두 번째 호출은 beam_size를 생략했다.
같은 스레드를 사용한 대조 실행은 greedy 추론을 완료했다.

재현 traceback의 호출 경로:

```text
core/model_manager.py:77 await_native_inference
core/model_manager.py:857 _await_native_inference -> asyncio.shield(task)
asyncio/threads.py:25 to_thread -> run_in_executor
concurrent/futures/thread.py:59 run
core/model_manager.py:841 run_worker -> func(*args, **kwargs)
mlx_whisper/transcribe.py:297 transcribe -> decode_with_fallback(mel_segment)
mlx_whisper/transcribe.py:224 decode_with_fallback -> model.decode(segment, options)
mlx_whisper/decoding.py:740 decode -> DecodingTask(model, options).run(mel)
mlx_whisper/decoding.py:650 run -> self._main_loop(audio_features, tokens)
mlx_whisper/decoding.py:600 _main_loop
    mx.async_eval(completed, tokens, sum_logprobs, no_speech_probs)
RuntimeError: There is no Stream(cpu, 0) in current thread.
```

재현 시 첫 worker ID는 6177255424, 폴백 worker ID는 13581234176이었다.
Stream 번호는 실행 환경에 따라 달라진다. 제보의 `cpu, 6`과 동일한
스레드 소유권 오류를 재현했지만 제보자의 정확한 traceback은 확보하지 못했다.
오류가 발생한 평가 연산은 확인했으며 그래프 안의 단일 원인 tensor까지는
분리하지 않았다. 모델 로더는 `model.parameters()`를 평가하지만 encoder의
private positional embedding과 decoder의 private causal mask 등은 별도 지연
상태일 수 있다. 추가 대조에서 생성 스레드의 `decoder._mask`만 `mx.eval()`로
평가해도 동일 오류가 남았다. 특정 tensor 하나를 평가하는 패치는 채택하지 않았다.

[MLX 공식 이슈 #3529](https://github.com/ml-explore/mlx/issues/3529)는
다른 스레드로 옮긴 지연 연산의 같은 종류 오류를 보여준다. 앱 원인의 증거는
위 로컬 대조 실행이며 이슈만으로 특정 버전의 회귀라고 확정하지 않는다.

## 변경

- Whisper 모듈 import·모델 생성·beam/greedy·추론은 전용 worker에서 실행한다.
- 매니저가 cleanup을 호출할 때 같은 worker에서 `ModelHolder.model/model_path`,
  `mel_filters`와 `hanning` 캐시를 해제한다. 다음 회의가 새 worker를 사용해도
  이전 모델·오디오 캐시를 재사용하지 않는다.
- 이전에는 매니저가 모듈 참조만 해제해 전역 Whisper 모델이 남을 수 있었다.
- 기존 native lease는 wrapper의 동기 호출 전체를 감싼다. timeout/cancel 뒤에도
  내부 executor의 실제 추론이 끝나야 wrapper가 반환한다. 따라서 매니저가
  모델 잠금과 참조를 유지하는 기존 deferred cleanup 동작을 보존한다.
- Gemma는 기존 `ThreadBoundLLMBackend`를 그대로 사용한다. 의존성 변경이나
  MLX 다운그레이드는 하지 않는다.
- 체크포인트·작업 DB·복구 로직은 변경하지 않는다. convert 완료 상태이면
  기존 `Pipeline._find_resume_step()`은 transcribe부터 재개한다.

## 검증 방법과 범위

```bash
.venv/bin/python -m pytest tests/test_whisper_backend.py tests/test_transcriber.py \
  tests/test_model_manager.py tests/test_mlx_client.py tests/test_llm_backend.py \
  tests/test_corrector.py tests/test_quality_evals.py -q
.venv/bin/python -m pytest tests/test_pipeline.py -q -k 'resume or checkpoint'
HF_HUB_OFFLINE=1 PYTHONPATH=. .venv/bin/python scripts/verify_whisper_gemma_threads.py \
  /absolute/sample-a.wav /absolute/sample-b.wav --output /absolute/new-output
```

실제 모델 검증 스크립트는 앱의 `Transcriber.transcribe()`와
`Corrector.correct()`를 같은 매니저에서 교대로 실행한다. 교정이 실패해 원본으로
폴백하면 검증 실패로 판정한다. 입력마다 전사·교정 JSON을 새 폴더에 보존한다.
화자는 UNKNOWN으로 전달하므로 pyannote, 전체 JobProcessor/HTTP 실행,
제보자의 두 회의 복구 완료를 증명하는 테스트는 아니다.

2026-09-15 검증 결과:

- 관련 단위·품질 테스트 211 passed, native projection 1개는 이 실행에서 제외.
- 재개·체크포인트 테스트 32 passed, harness 152 passed / 1 skipped.
- 변경 파일 Ruff 검사·포맷 및 신규 실행 코드 2개 Python 3.12 mypy 통과.
- 47.016초 한국어 합성 WAV 하나를 두 번 처리했다. 두 실행 모두 Whisper
  12구간 → Gemma E4B 교정 3배치 완료, 실패 폴백 0건이었다.
- 첫 실행 17:23:28–17:26:04, 두 번째 실행 17:26:04–17:27:48 KST.
  마지막 Gemma 정리는 17:27:49에 완료했다. 모델을 다시 로드하는 경계와
  한 모델 안에서 3개 교정 배치가 prompt cache를 재사용하는 경계를 확인했다.
- 수정된 문장은 0개다. 이 결과는 실행 호환성 검증이며 교정 품질 개선을
  입증하지 않는다. 의존성 버전은 유지했고 전체 Gemma 모델을 받아 검증했다.
- 원본 녹음·DB·기존 체크포인트를 변경하지 않았다. 결과는 새 임시 폴더에 저장했다.
