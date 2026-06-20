# AI 시스템 최종 개선 제안서

> 작성일: 2026-06-20
> 대상: Meeting Transcriber 로컬 AI 파이프라인
> 범위: 전사(STT), 화자분리, LLM 교정/요약/RAG
> 기준 환경: Apple Silicon MacBook Air, 16GB 통합 메모리, 100% 로컬 실행

## 0. 결론

현재 시스템은 이미 "한 번에 하나의 대형 모델만 로드"하는 구조, MLX prompt cache,
Gemma 4 E4B 기본값, pyannote CPU 강제, VAD 기본 OFF 같은 중요한 안전장치를 갖고 있다.
따라서 다음 개선은 모델을 성급히 바꾸는 방향이 아니라, **측정 가능한 병목을 작게 잘라서
품질 게이트를 통과한 것만 기본값으로 승격**하는 방식이어야 한다.

최종 우선순위는 다음이다.

| 우선순위 | 제안 | 기대 효과 | 위험 |
|---:|---|---|---|
| P0 | AI 단계별 측정 하네스 정비 | 이후 의사결정 오류 감소 | 낮음 |
| P1 | `pyannote/speaker-diarization-community-1` + exclusive diarization A/B | 병합 정확도 개선, UNKNOWN 감소 가능 | 중간 |
| P1 | STT `word_timestamps` 선택화 | 전사 속도/메모리 절감 가능 | 낮음~중간 |
| P1 | VAD를 조건부/병합형으로 재설계 | 환각 억제와 속도 손실 균형 | 중간 |
| P2 | LLM 교정 changed-only 모드 | 출력 토큰/시간/메모리 감소 | 중간 |
| P2 | LLM adaptive `max_tokens` | 폭주 방지, 메모리 안정성 | 낮음 |
| P3 | 저메모리 모드(E2B/QAT/Ollama)는 옵션화 | 8GB/메모리 압박 환경 대응 | 중간~높음 |

기본값 즉시 변경은 권장하지 않는다. 특히 **VAD ON, pyannote MPS, 12B/QAT 모델 기본 전환,
Ollama 기본 전환은 현 시점에서 보류**한다.

## 1. 현재 시스템 기준선

로컬 코드 기준으로 확인한 현재 AI 파이프라인의 핵심 상태는 다음이다.

| 영역 | 현재 구현 | 의미 |
|---|---|---|
| STT | `mlx-community/whisper-large-v3-turbo`, `condition_on_previous_text: false`, `word_timestamps: True` | 현 벤치마크 1위 모델. 단 word-level timestamp 사용 필요성은 재검토 가능 |
| VAD | `vad.enabled: false` | 기존 실험에서 VAD ON이 3~6배 느려지고 coverage가 낮아진 기록 반영 |
| 화자분리 | `pyannote/speaker-diarization-3.1`, CPU 강제 | MPS 버그 회피가 우선인 안정 지향 구성 |
| LLM | MLX + `mlx-community/gemma-4-e4b-it-4bit` | 16GB 로컬 기본값으로 적절 |
| LLM cache | system prompt hash 기반 prompt cache | 이미 큰 개선 적용됨 |
| 메모리 정책 | `ModelLoadManager`로 대형 모델 단일 로드 | 16GB에서 필수 |

주요 근거 위치:

- [`config.yaml`](../config.yaml): STT/화자분리/LLM/VAD/메모리 기본값
- [`steps/transcriber.py`](../steps/transcriber.py): MLX Whisper 호출, `word_timestamps`, `clip_timestamps`
- [`steps/diarizer.py`](../steps/diarizer.py): pyannote CPU 강제, worker 실행
- [`steps/merger.py`](../steps/merger.py): STT segment와 diarization segment의 시간 겹침 기반 병합
- [`core/mlx_client.py`](../core/mlx_client.py): MLX/MLX-VLM 분기, prompt cache
- [`steps/corrector.py`](../steps/corrector.py): 교정 배치, `keep_loaded=True`
- [`docs/BENCHMARK.md`](BENCHMARK.md): STT/VAD/LLM 실측 기준
- [`docs/PERFORMANCE_BACKLOG.md`](PERFORMANCE_BACKLOG.md): prompt cache 적용 결과
- [`docs/GEMMA4_12B_ADOPTION.md`](GEMMA4_12B_ADOPTION.md): 12B 불채택 근거

## 2. 기존 리서치에 대한 비판적 검토

### 2.1 STT: 모델 교체보다 타임스탬프 비용 검증이 우선

`whisper-large-v3-turbo` 유지 판단은 타당하다. OpenAI의 turbo 발표는 decoder layer 축소로
속도를 끌어올린 모델이라는 방향성을 뒷받침하고, 프로젝트 자체 6개 회의 벤치마크에서도
현재 기본값이 가장 좋은 결과를 냈다.

하지만 현재 병목을 "더 작은 STT 모델로 교체"로 풀면 품질 손실이 커질 가능성이 높다.
프로젝트의 실제 회의 벤치마크에서 4bit 한국어 fine-tune 모델은 깨끗한 벤치마크 숫자와 달리
회의 오디오에서 환각/누락 위험이 확인됐다. 따라서 STT 모델 교체는 1순위가 아니다.

더 비판적으로 봐야 할 부분은 `word_timestamps=True`다. 현재 `TranscriptSegment`는
`text/start/end/avg_logprob/no_speech_prob`만 저장하고, `Merger`도 segment-level timestamp만
사용한다. 즉, word-level timestamp를 실제 기능이 사용하지 않는다면 비용만 내고 있을 수 있다.
다만 word timestamp를 끄면 MLX Whisper의 segment boundary가 달라질 수 있으므로,
단순 코드 변경이 아니라 A/B 실험으로 판단해야 한다.

권장 판단:

- `whisper-large-v3-turbo`는 유지한다.
- `stt.word_timestamps` 설정 키를 추가해 실험 가능하게 만든다.
- 기본값 변경은 CER, coverage, 병합 UNKNOWN 비율, 요약 품질까지 확인한 뒤 결정한다.

### 2.2 VAD: "Silero는 빠르다"와 "우리 파이프라인이 빨라진다"는 다른 주장

Silero VAD 자체가 작고 CPU에서 빠르다는 외부 자료는 맞다. 그러나 이 프로젝트의 핵심 비용은
VAD 모델 실행이 아니라, VAD 결과를 `clip_timestamps`로 넘긴 뒤 MLX Whisper가 여러 구간을
처리하는 방식에서 생긴다.

기존 벤치마크는 VAD ON에서 실행 시간이 3~6배 증가하고 coverage가 낮아졌다고 기록한다.
이 결과가 더 강한 근거다. 따라서 "VAD를 켜자"는 결론은 잘못이다. 올바른 결론은
**VAD를 켜야 하는 파일만 고르고, 너무 잘게 쪼개진 구간을 병합하며, 이득이 작으면 전체 전사로
폴백**하는 것이다.

권장 판단:

- VAD 기본 OFF는 유지한다.
- VAD는 `auto` 모드를 새로 설계한다.
- `max_clip_segments`, `merge_gap_seconds`, `min_silence_saved_ratio` 같은 방어 설정을 둔다.
- VAD 결과가 많은 작은 조각으로 쪼개지면 전체 전사로 폴백한다.

### 2.3 화자분리: community-1은 가장 유력하지만, 기본 전환은 실측 후

`pyannote/speaker-diarization-community-1`은 검토 가치가 높다. 공식 model card와 pyannote
블로그 모두 speaker assignment/counting 개선과 exclusive diarization을 강조한다. 특히
exclusive diarization은 "한 시점에 하나의 대표 화자" 형태를 제공해 STT timestamp와 결합하기
쉽다. 이 프로젝트의 `Merger`가 시간 겹침 기반으로 단순 할당하는 구조이므로, community-1의
exclusive output은 구조적으로 잘 맞는다.

하지만 바로 기본값으로 바꾸면 안 된다.

- pyannote 4.x 계열 의존성 변화와 torchcodec/ffmpeg 요구사항이 설치 경로에 영향을 줄 수 있다.
- 기존 `steps/diarizer.py`는 `speaker_diarization`만 추출한다. `exclusive_speaker_diarization`
  사용은 파싱 경로 확장이 필요하다.
- 프로젝트 오디오는 한국어 Zoom 회의이고, 공개 DER 벤치마크와 데이터 조건이 다르다.
- CPU 강제 정책은 유지해야 한다. MPS 전환은 안정성 리스크가 크고 프로젝트 규칙과 충돌한다.

권장 판단:

- community-1을 실험 모델로 추가한다.
- regular diarization과 exclusive diarization을 모두 저장하거나 선택 가능하게 한다.
- 승격 기준은 DER만이 아니라 `UNKNOWN` 비율, speaker count 정확도, 병합 후 사람 검수 결과로 둔다.

### 2.4 LLM: 모델 교체보다 생성해야 하는 토큰을 줄이는 것이 우선

LLM은 이미 상당히 최적화되어 있다. `core/mlx_client.py`는 Gemma 4를 `mlx-vlm`으로 라우팅하고,
system prompt hash 기반 prompt cache를 사용한다. `steps/corrector.py`는 `keep_loaded=True`로
교정 후 요약 단계에서 모델 재로드를 피한다.

따라서 다음 이득은 "더 큰 모델"이 아니라 "더 적게 생성"에서 나온다.

현재 교정 프롬프트는 모든 입력 라인을 다시 출력하게 한다. 이 방식은 안정적이지만, 보정이 필요
없는 문장까지 출력 토큰을 소비한다. changed-only 모드로 바꾸면 LLM이 수정된 라인만 반환하고,
누락된 번호는 원문 유지로 처리할 수 있다. 기존 graceful degradation 철학과도 맞는다.

단, changed-only는 다음 위험이 있다.

- 모델이 수정이 필요한 문장도 누락할 수 있다.
- 교정률 지표가 낮아 보일 수 있다.
- 파서 의미가 "누락=실패"에서 "누락=원문 유지"로 바뀐다.

따라서 기존 full-output 모드를 유지한 채 실험 모드로 넣고, 파싱 실패나 교정률 이상치가 감지되면
full-output으로 자동 폴백하는 방식이 맞다.

### 2.5 KV cache quantization과 QAT는 R&D 항목이지 단기 기본값이 아니다

`mlx-lm`은 rotating KV cache와 prompt cache를 제공한다. 그러나 현재 기본 Gemma 4 경로는
`mlx-vlm`이다. `mlx-lm`의 `max-kv-size`, `kv_bits`류 최적화가 기본 경로에 그대로 적용된다고
가정하면 안 된다.

Gemma 4 QAT는 메모리 절감 가능성이 크지만, 공식 자료상 배포 경로가 GGUF, vLLM/SGLang,
LiteRT-LM, 변환용 checkpoint로 나뉜다. 프로젝트의 기본 경로인 MLX-VLM에서 즉시 동일한
메모리 이득을 얻는다고 볼 근거는 부족하다. `docs/GEMMA4_12B_ADOPTION.md`도 12B는 E4B 대비
품질 이득이 충분하지 않다고 결론낸다.

권장 판단:

- 단기 기본값은 Gemma 4 E4B 유지.
- 저메모리 옵션으로 Gemma 4 E2B를 명확히 노출한다.
- QAT/12B/Ollama는 옵트인 실험 경로로만 다룬다.

### 2.6 현재 벤치마크의 한계도 개선 대상이다

기존 벤치마크는 귀중하지만 한계가 있다.

- 표본 수가 작다.
- 일부 정답지가 외부 모델 또는 특정 작성 스타일에 의존한다.
- CER/WER 숫자는 회의 품질 체감과 완전히 일치하지 않는다.
- LLM 교정 평가는 `SequenceMatcher`에 치우칠 수 있다.
- MLX 통합 메모리는 `psutil` RSS만으로는 부족하다.

따라서 최우선 작업은 최적화 자체가 아니라 **최적화가 맞는지 판단할 측정 계층**이다.

## 3. 최종 개선안

### 3.1 P0: AI 성능/메모리 측정 하네스

모든 후속 개선의 선행 조건이다.

측정해야 할 값:

| 범주 | 지표 |
|---|---|
| 시간 | 단계별 wall time, RTF, 모델 load/unload 시간 |
| STT 품질 | CER, WER, coverage, hallucination filter count, 빈 segment count |
| 화자분리 | speaker count, UNKNOWN 비율, 짧은 speaker turn 수, overlap 처리 결과 |
| LLM | 입력 추정 토큰, 출력 문자/토큰 수, 파싱 성공률, 재시도 수, fallback 수 |
| 메모리 | `psutil` RSS/available, MLX active/peak memory, swap delta |
| 열/운영 | 연속 처리 시 성능 저하, cooldown 전후 RTF |

구현 제안:

- `scripts/benchmark_ai_pipeline.py` 또는 기존 benchmark script 확장
- 동일 회의 corpus에 대해 여러 variant 실행
- 결과를 JSONL/CSV로 저장
- raw output과 metric summary를 분리
- 기본값 승격은 이 하네스 결과 없이는 금지

승격 게이트:

- 16GB 환경에서 새 swap delta가 job당 250MB 이하
- `pipeline.peak_ram_limit_gb: 9.5`를 넘지 않음
- E2E wall time이 baseline 대비 악화되지 않음. 품질 개선 목적 실험은 최대 +10%까지 허용
- 품질 지표가 baseline과 동등 이상

### 3.2 P1: 화자분리 community-1 A/B

목표는 "화자분리 자체의 DER 개선"보다 **전사 segment와 결합했을 때 최종 utterance 품질을 높이는 것**이다.

실험 variant:

| Variant | pyannote 모델 | 출력 |
|---|---|---|
| D0 | `speaker-diarization-3.1` | `speaker_diarization` |
| D1 | `speaker-diarization-community-1` | `speaker_diarization` |
| D2 | `speaker-diarization-community-1` | `exclusive_speaker_diarization` |

구현 제안:

- `diarization.model_name`으로 community-1을 지정 가능하게 한다.
- `diarization.output_mode: regular | exclusive | auto` 설정을 추가한다.
- `DiarizeOutput`에서 `exclusive_speaker_diarization`이 있으면 선택적으로 사용한다.
- checkpoint에는 사용 모델, output mode, speaker count를 기록한다.

승격 기준:

- UNKNOWN utterance 비율 baseline 대비 30% 이상 감소하거나, 동일하면서 speaker assignment 검수 결과 개선
- speaker count가 실제 참석자 수와 더 가까움
- CPU wall time이 baseline 대비 15% 이상 느려지지 않음
- 설치/토큰/게이트 모델 안내가 깨지지 않음

보류:

- pyannote MPS 기본 전환
- pyannote cloud/precision 모델 사용
- 사용자 동의 없는 외부 API 경로

### 3.3 P1: STT word timestamp 선택화

현재 저장 데이터 구조와 병합 로직은 word-level timestamp를 직접 사용하지 않는다. 그러므로
`word_timestamps=True`가 실제로 비용을 만든다면 제거 효과가 클 수 있다.

구현 제안:

- `config.yaml`에 `stt.word_timestamps: true`를 추가한다.
- `Transcriber._build_transcribe_kwargs()`에서 하드코딩 대신 설정값을 사용한다.
- UI word highlight 같은 기능이 없다면 실험 variant에서는 `false`를 테스트한다.

실험 variant:

| Variant | word_timestamps | VAD |
|---|---:|---:|
| S0 | true | off |
| S1 | false | off |
| S2 | false | auto/coalesced |

승격 기준:

- CER 악화가 +1%p 이하
- segment boundary 변화로 UNKNOWN 비율이 악화되지 않음
- STT wall time 또는 peak memory가 유의미하게 감소
- 회의록/검색/채팅 산출물에 기능 손실 없음

### 3.4 P1: VAD auto/coalesced 모드

VAD는 "항상 켠다"가 아니라 "전사 비용을 줄일 가능성이 큰 경우만 켠다"로 재설계한다.

구현 제안:

- `vad.mode: off | on | auto`
- `vad.merge_gap_seconds`: 가까운 speech segment 병합
- `vad.max_clip_segments`: 너무 많은 segment면 전체 전사 폴백
- `vad.min_silence_saved_ratio`: 제거되는 무음 비율이 낮으면 전체 전사 폴백
- 마지막 timestamp duration 일치 회피 로직은 유지

auto 판단 예시:

1. Silero로 speech segment 산출
2. pad 적용 후 gap이 짧은 segment 병합
3. 전체 오디오 대비 제거 가능한 무음 비율 계산
4. segment 수가 많거나 절감 비율이 작으면 `clip_timestamps` 미사용
5. 조건을 만족할 때만 MLX Whisper에 전달

승격 기준:

- 기존 VAD OFF 대비 STT wall time 악화 없음
- hallucination filter count 감소
- coverage 악화 없음
- `clip_timestamps` hang 회피 테스트 유지

### 3.5 P2: LLM changed-only 교정 모드

교정 단계의 가장 큰 낭비는 변경이 없는 라인까지 모델이 다시 생성한다는 점이다. changed-only 모드는
수정된 번호만 반환하게 하고, 누락 번호는 원문 유지로 처리한다.

구현 제안:

- `llm.correction_mode: full | changed_only | auto`
- changed-only 전용 system prompt 추가
- parser는 `[번호] 수정문`만 받는다.
- full mode parser와 의미를 분리한다.
- changed-only parse 실패, 출력 과소, 이상 교정률 감지 시 full mode로 재시도한다.

기대 효과:

- 출력 토큰 감소
- 생성 시간 감소
- peak KV cache 증가 억제
- 교정 batch size를 더 안정적으로 키울 여지

승격 기준:

- 파싱 성공률 99% 이상
- 사람 검수 기준 의미 변경 증가 없음
- 보정 누락이 baseline보다 늘지 않음
- LLM 단계 wall time 20% 이상 감소

### 3.6 P2: adaptive `max_tokens`

현재 교정/요약/채팅 token cap은 안전하지만 정적이다. 작은 교정 batch에도 `correction_max_tokens: 800`을
열어두면 모델 폭주 여지가 남는다.

구현 제안:

- 교정 batch는 입력 줄 수와 입력 문자 수 기반으로 `max_tokens`를 낮춘다.
- 요약은 transcript token estimate와 요청한 출력 형식에 따라 cap을 조정한다.
- 최소/최대 clamp는 유지한다.

예시:

| 단계 | cap 계산 방향 |
|---|---|
| correction full | `min(config_cap, base + per_utterance * n + input_chars_ratio)` |
| correction changed-only | full 대비 더 낮은 cap |
| summarize | transcript 길이와 chunk count 기반 |
| chat | 검색 context 길이와 UI 응답 목적 기반 |

승격 기준:

- truncation으로 인한 파싱 실패 증가 없음
- 평균 출력 토큰과 latency 감소
- fallback 횟수 증가 없음

### 3.7 P3: 저메모리 모드

8GB 또는 메모리 압박 환경을 위한 별도 운영 모드다. 16GB 기본값을 흔들지 않는 범위에서 제공한다.

구성 후보:

| 모드 | 설정 |
|---|---|
| 기본 | Gemma 4 E4B, STT turbo, VAD off |
| 저메모리 | Gemma 4 E2B, `word_timestamps=false`, LLM changed-only, adaptive cap |
| 전사 우선 | `pipeline.skip_llm_steps=true`, 화자분리 optional |
| 고품질 실험 | community-1 exclusive, E4B 유지 |

QAT/Ollama/12B는 여기서도 기본값이 아니라 실험 옵션이다. Ollama는 별도 앱/서버 프로세스 의존성이 있고,
프로젝트의 기본 철학인 in-process MLX와 운영 특성이 다르다.

## 4. 구현 순서

### Wave 0: 측정 기반 정비

- AI benchmark harness 확장
- MLX active/peak memory와 swap delta 기록
- `docs/BENCHMARK.md`의 VAD 기본값 불일치 정리
- 기존 corpus 기준 baseline 재생성

완료 조건:

- baseline JSONL/CSV 생성
- STT/diarization/LLM 단계별 수치 확인 가능
- 한 번의 명령으로 variant 비교 가능

### Wave 1: 화자분리 실험

- community-1 모델명 허용
- exclusive output parsing 추가
- D0/D1/D2 비교

완료 조건:

- 기존 3.1과 community-1 결과를 같은 포맷으로 비교
- UNKNOWN 비율과 speaker count 리포트 자동 생성

### Wave 2: STT/VAD 실험

- `stt.word_timestamps` 설정화
- VAD auto/coalesced 구현
- S0/S1/S2 비교

완료 조건:

- word timestamp OFF가 안전한지 판단
- VAD가 켜지는 조건과 폴백 조건이 수치로 설명됨

### Wave 3: LLM 출력 절감

- changed-only correction mode
- adaptive max token
- full mode fallback

완료 조건:

- parse failure 증가 없음
- correction latency와 output token 감소 확인

### Wave 4: 저메모리 옵션

- Gemma 4 E2B 옵션 검증
- 저메모리 preset 문서화
- 8GB/16GB별 권장 설정 업데이트

완료 조건:

- 기본값은 E4B 유지
- 저메모리 모드의 품질 저하와 이득이 문서화됨

## 5. 기본값 승격 기준

새 AI 설정을 기본값으로 바꾸려면 아래 조건을 모두 만족해야 한다.

| 조건 | 기준 |
|---|---|
| 재현성 | 동일 corpus에서 3회 이상 변동 폭 허용 범위 내 |
| 메모리 | 16GB 기준 peak budget 초과 없음, swap delta 작음 |
| 품질 | CER/WER 또는 사람 검수에서 악화 없음 |
| 병합 | UNKNOWN 비율 또는 speaker assignment 악화 없음 |
| 안정성 | timeout, hang, parse failure 증가 없음 |
| 운영 | 설치/토큰/권한/외부 앱 요구사항 증가 없음 |
| 문서 | README/AGENTS/config/docs 동기화 완료 |

## 6. 하지 말아야 할 변경

아래 변경은 단기 개선처럼 보이지만 현재 제약에서는 위험하다.

| 변경 | 보류 이유 |
|---|---|
| VAD 기본 ON | 프로젝트 실측에서 느려지고 coverage가 낮아짐 |
| pyannote MPS 기본 사용 | 기존 정책과 충돌, 안정성 리스크 |
| 12B/QAT/Ollama 기본 전환 | E4B 대비 기본 품질 이득 불충분, 운영 복잡도 증가 |
| STT 4bit fine-tune 기본 전환 | 실제 회의에서 환각/누락 리스크 확인 |
| `beam_size` 튜닝 | 현재 mlx-whisper 경로에서 미지원 폴백 존재 |
| LLM 동시 실행 | MLX/Metal 안정성 및 메모리 정책 위반 |
| 외부 cloud diarization | 100% 로컬 원칙 위반 |

## 7. 예상 성과

보수적 추정:

| 개선 | 기대 효과 |
|---|---|
| word timestamp 선택화 | STT 시간/메모리 일부 절감 가능 |
| community-1 exclusive | 병합 품질 개선, UNKNOWN 감소 가능 |
| VAD auto/coalesced | 무음 많은 파일에서 환각 감소, 기존 VAD ON보다 속도 손실 완화 |
| changed-only correction | LLM 출력 토큰 30~60% 감소 가능 |
| adaptive max_tokens | LLM 폭주와 peak memory 완화 |
| 측정 하네스 | 잘못된 기본값 변경 방지 |

공격적 추정은 문서에 기본값 근거로 쓰지 않는다. 이 프로젝트에서는 벤치마크 표본이 작고,
하드웨어/회의 유형/오디오 품질에 따라 결과가 크게 흔들릴 수 있다.

## 8. 최종 권장안

1. **기본 모델은 유지한다.**
   - STT: `whisper-large-v3-turbo`
   - Diarization: community-1 A/B 통과 전까지 `speaker-diarization-3.1`
   - LLM: Gemma 4 E4B

2. **첫 PR은 측정 하네스와 설정 선택화만 한다.**
   - `stt.word_timestamps`
   - `diarization.output_mode`
   - `vad.mode`
   - LLM 토큰/교정 metric logging

3. **두 번째 PR에서 community-1 exclusive를 실험 경로로 넣는다.**

4. **세 번째 PR에서 LLM changed-only와 adaptive token cap을 넣는다.**

5. **어떤 것도 실측 없이 기본값으로 승격하지 않는다.**

## 8.1 구현 반영 상태

2026-06-20 구현에서 아래 항목을 코드에 반영했다. 기본 모델은 유지했고, 위험한 동작은
설정 또는 하네스 override로만 켜지게 했다.

| 항목 | 설정/파일 | 기본값 |
|---|---|---|
| STT word timestamp 선택화 | `stt.word_timestamps` | `true` |
| VAD mode | `vad.mode: off | on | auto` | `off` |
| VAD 구간 병합 | `vad.merge_gap_seconds` | `2.0` |
| VAD auto 폴백 | `vad.max_clip_segments`, `vad.min_silence_saved_ratio` | `80`, `0.15` |
| pyannote exclusive 출력 선택 | `diarization.output_mode: regular | exclusive | auto` | `regular` |
| LLM changed-only 교정 | `llm.correction_mode: full | changed_only | auto` | `full` |
| LLM adaptive token cap | `llm.correction_adaptive_max_tokens` | `true` |
| 측정 하네스 | `scripts/benchmark_ai_pipeline.py` | 입력 폴더 최신 오디오 사용 |

대표 실행 명령:

```bash
python scripts/benchmark_ai_pipeline.py
python scripts/benchmark_ai_pipeline.py --skip-llm
python scripts/benchmark_ai_pipeline.py --stt-word-timestamps false --vad-mode auto --skip-llm
python scripts/benchmark_ai_pipeline.py \
  --diarization-model pyannote/speaker-diarization-community-1 \
  --diarization-output-mode exclusive \
  --correction-mode changed_only
```

하네스 결과는 기본적으로 `~/.meeting-transcriber/benchmarks/{run_id}/ai_pipeline_benchmark.json`에
저장된다. 각 단계는 wall time, RSS, available memory, swap, MLX active/peak memory, 단계별
품질 관측값을 포함한다.

## 9. 참고 출처

외부 근거:

- OpenAI Whisper turbo 발표: <https://github.com/openai/whisper/discussions/2363>
- MLX Whisper README: <https://github.com/ml-explore/mlx-examples/blob/main/whisper/README.md>
- `mlx-community/whisper-large-v3-turbo`: <https://huggingface.co/mlx-community/whisper-large-v3-turbo>
- pyannote community-1 model card: <https://huggingface.co/pyannote/speaker-diarization-community-1>
- pyannote community-1 release blog: <https://www.pyannote.ai/blog/community-1>
- pyannote.audio repository: <https://github.com/pyannote/pyannote-audio>
- Silero VAD repository: <https://github.com/snakers4/silero-vad>
- Silero VAD PyTorch Hub note: <https://pytorch.org/hub/snakers4_silero-vad_vad/>
- WhisperX README: <https://github.com/m-bain/whisperX>
- MLX-LM prompt cache/KV cache docs: <https://github.com/ml-explore/mlx-lm>
- MLX-LM PyPI docs: <https://pypi.org/project/mlx-lm/>
- Gemma 4 QAT announcement: <https://blog.google/innovation-and-ai/technology/developers-tools/quantization-aware-training-gemma-4/>
- Gemma 4 memory requirements: <https://ai.google.dev/gemma/docs/core>

내부 근거:

- [`docs/BENCHMARK.md`](BENCHMARK.md)
- [`docs/PERFORMANCE_BACKLOG.md`](PERFORMANCE_BACKLOG.md)
- [`docs/GEMMA4_12B_ADOPTION.md`](GEMMA4_12B_ADOPTION.md)
- [`config.yaml`](../config.yaml)
- [`steps/transcriber.py`](../steps/transcriber.py)
- [`steps/vad_detector.py`](../steps/vad_detector.py)
- [`steps/diarizer.py`](../steps/diarizer.py)
- [`steps/merger.py`](../steps/merger.py)
- [`steps/corrector.py`](../steps/corrector.py)
- [`steps/summarizer.py`](../steps/summarizer.py)
- [`core/mlx_client.py`](../core/mlx_client.py)
- [`core/model_manager.py`](../core/model_manager.py)
