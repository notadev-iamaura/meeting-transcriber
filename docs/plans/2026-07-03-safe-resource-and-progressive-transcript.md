# 안전 리소스 최적화 및 전사 초안 조기 노출 계획

상태: 구현 및 QA 완료
작성일: 2026-07-03
대상 환경: Apple Silicon MacBook M4, 16GB RAM
원칙: 회의록 품질, 보안, 사용자 편의의 반대급부가 거의 없는 변경만 기본값 후보로 반영한다.

## 1. 목표

이 계획은 두 가지 사용자 문제를 동시에 해결한다.

1. MacBook 리소스를 더 안전하게 사용한다.
   - 불필요한 STT 비용을 줄인다.
   - 멈춘 작업 또는 실패 반복으로 밤새 리소스를 쓰는 상황을 줄인다.
   - 모델 품질을 낮추거나 보안 정책을 우회하지 않는다.

2. 전체 파이프라인 완료 전에도 전사 결과를 볼 수 있게 한다.
   - 현재는 `transcribe.json`이 저장되어도 `/api/meetings/{id}/transcript`가 이를 읽지 않아 UI가 빈 상태로 남는다.
   - 전사 단계가 끝나면 "전사 초안"을 먼저 보여주고, 병합/보정 결과가 생기면 자동으로 더 높은 단계 결과로 교체한다.

## 2. 반대급부 최소 변경 원칙

기본값 또는 구현 대상으로 포함할 수 있는 항목:

| 항목 | 포함 여부 | 이유 |
|---|---:|---|
| `stt.word_timestamps=false` 기본값 | 보류 | 세그먼트 경계가 달라져 화자 병합/UNKNOWN에 영향을 줄 수 있다. A/B 품질 게이트 통과 전에는 기본값을 바꾸지 않는다. |
| 체크포인트 재사용 유지/검증 | 포함 | 이미 끝난 STT를 재실행하지 않아 시간/GPU/메모리를 줄인다. 결과 품질 변화가 없다. |
| 모델 로딩 전 사전 점검 | 포함 | 실패할 작업을 무겁게 시작하지 않는다. 결과 품질 변화가 없다. |
| 단계 완료 후 모델 언로드 유지/검증 | 포함 | 16GB 환경에서 메모리 피크를 낮춘다. 단, `mlx-whisper` 내부 모델 캐시 정리는 별도 검증 전까지 범위 밖으로 둔다. |
| `transcribe.json` 읽기 전용 초안 노출 | 포함 | 이미 생성된 중간 결과를 더 빨리 보여준다. 최종 산출물 우선순위는 유지한다. |
| 처리 중 UI 자동 재조회 | 포함 | 새 모델/새 계산 없이 기존 API를 다시 조회한다. |
| STT 동적 타임아웃 유지 | 포함 | 이미 구현된 안전장치다. 기본값을 더 줄이는 것은 오탐 위험이 있어 이번 범위에서는 유지한다. |
| `meeting_id` 점 세그먼트 차단 | 포함 | 경로 탐색 위험을 줄인다. 정상 meeting_id 사용성 반대급부가 없다. |
| `transcribe.json` 원자적 저장 | 포함 | UI 조기 폴링 중 부분 JSON을 읽는 문제를 줄인다. 결과 품질 변화가 없다. |

이번 기본 변경에서 제외할 항목:

| 항목 | 제외 이유 |
|---|---|
| STT 모델 다운그레이드 | 정확도 손실 가능성이 있다. |
| pyannote MPS 전환 | 안정성 리스크가 있다. pyannote는 CPU 강제를 유지한다. |
| VAD 기본 ON 강제 | 음성을 잘라낼 가능성이 있어 조건부/실험 대상으로 유지한다. |
| 화자 수 힌트 강제 | 사용자 편의 반대급부가 크다. |
| SSL 검증 OFF | 보안상 명확한 손해가 있다. |
| 자동처리 batch 추가 축소 | 처리량 손해가 있다. |
| changed-only보다 더 공격적인 LLM 교정 | 품질/스타일 변화 가능성이 있다. |

## 3. 현재 증거

확인된 현재 구조:

- STT 결과는 `checkpoints/{meeting_id}/transcribe.json`에 저장된다.
- `/api/meetings/{meeting_id}/transcript`는 현재 `corrected.json` → `correct.json` → `merge.json`까지만 조회한다.
- Viewer는 `/api/meetings/{meeting_id}/transcript` 404를 받으면 "전사문 없음" 빈 상태를 보여준다.
- Viewer의 처리 중 폴링은 최종 `completed` 상태일 때 전사문을 다시 로드한다. `transcribe` 완료 직후 초안을 적극 로드하지 않는다.
- `config.yaml`과 `STTConfig` 기본값은 현재 `word_timestamps: true`다.
- STT 동적 타임아웃과 타임아웃 비재시도 정책은 이미 구현되어 있다.

## 4. 구현 범위

### 4.1 STT 기본값

- 이번 구현에서는 `STTConfig.word_timestamps`와 `config.yaml` 기본값을 바꾸지 않는다.
- `word_timestamps=false`는 여전히 설정으로 사용할 수 있으나, 기본값 변경은 아래 A/B 승인 게이트 통과 뒤 별도 변경으로 진행한다.
- 이유: 현재 파서는 `words[]`가 있으면 세그먼트 시작/끝 경계를 단어 timestamp로 보정하고, 병합기는 그 경계의 overlap으로 화자를 배정한다. 따라서 기본 OFF는 STT 비용 절감 가능성이 있지만 화자 병합 품질의 숨은 반대급부가 있다.

불변 조건:

- STT 모델은 `mlx-community/whisper-large-v3-turbo` 유지.
- `condition_on_previous_text=false` 유지.
- segment-level timestamp는 계속 사용 가능해야 한다.

기본값 변경 승인 게이트:

- 같은 대표 회의 파일에서 `word_timestamps=true/false`를 A/B 실행한다.
- 비교 지표: STT wall time, peak RSS/MLX memory, segment count, monotonic `start/end`, temporal coverage, 최대 경계 drift, merge UNKNOWN ratio, speaker distribution, reference가 있으면 CER/WER.
- 승인 기준: CER +1%p 이내, temporal coverage/UNKNOWN ratio 악화 없음, speaker distribution의 의미 있는 악화 없음, 리소스 이득 명확.
- A/B 게이트 정비: benchmark override는 부분 `Namespace`에서도 안전하게 동작하도록 보강했다.

### 4.2 전사 초안 API

`GET /api/meetings/{meeting_id}/transcript`의 폴백 순서를 확장한다.

1. `outputs/{meeting_id}/corrected.json`
2. `checkpoints/{meeting_id}/correct.json`
3. `checkpoints/{meeting_id}/merge.json`
4. `checkpoints/{meeting_id}/transcribe.json`

`transcribe.json` fallback 규칙:

- 응답 필드에 모든 source 공통으로 `source_stage`와 `readonly`를 추가한다.
- `source_stage`: `"corrected" | "correct" | "merge" | "transcribe"`.
- `readonly=false`: `corrected`, `correct`.
- `readonly=true`: `merge`, `transcribe`.
- `segments`를 `utterances`로 변환한다.
- speaker는 `UNKNOWN`으로 둔다.
- `num_speakers=0`, `speakers=[]`로 둔다.
- `was_corrected=false`로 둔다.

불변 조건:

- `corrected/correct/merge`가 있으면 기존 우선순위가 반드시 유지된다.
- 기존 클라이언트가 추가 필드를 몰라도 깨지지 않아야 한다.
- 편집 대상 탐색은 `transcribe.json`을 포함하지 않는다.
- `transcribe.json`은 완료 복구, 편집 대상, 요약 eligibility, reindex eligibility에 포함하지 않는다.
- `merge.json`도 기존처럼 읽기 전용으로 유지한다.
- 높은 우선순위 JSON이 깨져 있으면 낮은 단계 초안으로 조용히 fallback하지 않고 fail-loud 한다.

### 4.2.1 경로 및 캐시 안전성

- `_validate_meeting_id`에서 `"."`, `".."`, dot segment가 포함된 ID를 거부한다.
- transcript candidate 경로는 configured outputs/checkpoints root 아래에만 있어야 한다.
- JSON cache는 `mtime_ns`와 file size를 같이 본다.
- `TranscriptResult.save_checkpoint()`는 원자적 JSON 쓰기를 사용한다. 조기 폴링 중 부분 JSON을 읽는 일을 막기 위함이다.

### 4.3 Viewer UI

- transcript API 응답이 `source_stage="transcribe"`이면 전사 탭에 초안 배지를 표시한다.
- 초안 상태의 설명은 짧고 기능 설명 과잉 없이 표시한다.
- 초안 상태에서도 검색, 복사, 다운로드는 가능해야 한다.
- 초안 상태에서는 서버 편집 저장을 열지 않는다.
- `readonly=true`이면 발화 더블클릭 인라인 편집과 모두 바꾸기 액션을 비활성화한다.
- Viewer는 `_transcriptSourceStage`와 `_transcriptReadonly`를 1급 상태로 저장한다.
- legacy 응답은 기본적으로 `source_stage="corrected"`, `readonly=false`로 정규화한다.
- source precedence는 `transcribe < merge < correct < corrected`로 판단해 낮은 단계의 늦은 응답이 높은 단계 렌더를 덮지 못하게 한다.
- 조용한 폴링 경로를 두어 이미 전사문이 보이는 상태에서는 스켈레톤/스크롤/검색/재생을 불필요하게 리셋하지 않는다.
- `readonly=true`이면 `_beginEditUtterance`, `_saveTranscript`, `_openReplaceModal` 모두에서 mutation을 하드 차단한다.
- `readonly=false`인 `correct/corrected` 응답이어도 회의 상태가 `completed`가 아니면 mutation을 하드 차단한다.
- 초안 배지는 visible text와 `role="status"`/`aria-live="polite"`를 가진다.
- 전사 초안이 보여도 현재 처리 단계 상태를 compact하게 유지한다.
- 처리 중 폴링 중에도 transcript API를 재조회해 초안이 생기면 즉시 표시한다.
- 이후 `merge/correct` 결과가 생기면 같은 API 우선순위에 의해 자동으로 더 나은 결과를 표시한다.

불변 조건:

- 최종 완료 후 기존 전사문 UI와 액션 그룹은 유지한다.
- 전사문이 전혀 없을 때의 빈 상태와 전사 시작 버튼은 유지한다.
- macOS 디자인 토큰과 기존 viewer 레이아웃을 유지한다.

## 5. 검증 계획

### 5.1 단위/API

- `tests/test_transcriber.py`
  - `words[]`가 있으면 세그먼트 경계를 word timestamp로 보정하는 기존 동작을 검증한다.
  - `words[]`가 없으면 segment timestamp fallback이 유지되는지 검증한다.
  - `word_timestamps=false` 설정값 전달 경로는 유지 검증한다.

- `tests/test_routes.py`
  - `transcribe.json`만 있을 때 `/api/meetings/{id}/transcript`가 200을 반환한다.
  - `source_stage="transcribe"`, `readonly=true`, speaker `UNKNOWN`을 검증한다.
  - `source_stage`/`readonly`가 모든 source에서 실제 편집 가능성과 일치하는지 확인한다.
  - `corrected > correct > merge > transcribe` 우선순위를 확인한다.
  - `transcribe.json`만 있을 때 PUT/replace가 404를 반환하고 파일을 바꾸지 않는지 확인한다.
  - `"."`, `".."` 또는 dot segment meeting_id가 transcript 파일을 읽지 못하는지 확인한다.
  - draft 조회 뒤 higher-stage 파일이 생기면 cache가 higher-stage를 반환하는지 확인한다.
  - higher-stage JSON이 깨져 있으면 낮은 단계 초안으로 fallback하지 않는지 확인한다.

### 5.2 UI

- `node --check ui/web/viewer-view.js`
- `pytest -m ui tests/ui/integration/test_spa_overhaul_integration.py -q`
  - 초안 응답을 받으면 타임라인이 표시된다.
  - 처리 중 상태에서 초안 응답이 생기면 빈 상태가 타임라인으로 전환된다.
  - 초안 배지가 표시된다.
  - 초안 상태에서 검색/복사/다운로드는 보이고, replace/inline edit/PUT/replace POST는 발생하지 않는다.
  - `source_stage="correct"`, `readonly=false`라도 job 상태가 `embedding`이면 표시/복사/다운로드만 가능하고 inline edit/replace는 막힌다.
  - 초안 이후 merge/correct 응답이 오면 source와 액션이 upgrade된다.
  - completed `readonly=false`는 기존 편집/replace 동작을 유지한다.
  - 초안 배지의 live status semantics를 확인한다.

### 5.3 회귀 게이트

- `ruff check` touched Python files.
- API/router 변경 범위에 맞춰 `pytest tests/test_routes.py tests/test_transcriber.py -q`.
- UI 변경 범위에 맞춰 JS syntax와 UI integration gate.
- 최종 QA 검토 후 필요하면 추가 targeted gate 실행.

## 6. 위원회 검토 체계

기능별 3인 검토를 수행한다.

| 팀 | 검토자 | 책임 |
|---|---|---|
| STT/리소스 | STT-A, STT-B, STT-C | `word_timestamps` 기본 OFF, 타임아웃/체크포인트/모델 생명주기 반대급부 검토 |
| API/데이터 | API-A, API-B, API-C | transcript fallback 계약, readonly 초안, 편집 경로 안전성 검토 |
| UI/UX | UI-A, UI-B, UI-C | viewer 처리 중 초안 표시, 최종 결과 자동 교체, 디자인/접근성 검토 |
| QA | QA-A, QA-B, QA-C | 구현 후 독립 검수, 테스트 커버리지, 회귀 위험 검토 |

만장일치 기준:

- 각 기능팀 3명 모두 "진행 가능" 또는 수정 후 "진행 가능" 판정이어야 구현을 완료 처리한다.
- QA 3명 모두 "승인"이어야 최종 완료 처리한다.
- 반대 의견이 있으면 문서의 "이슈 및 반복" 섹션에 기록하고, 수정 후 같은 검증을 다시 수행한다.

## 7. 진행 추적

| 단계 | 상태 | 증거 |
|---|---|---|
| 계획 문서 작성 | 완료 | 이 문서 |
| STT/리소스 3인 검토 | 완료 | 3인 모두 `word_timestamps=false` 기본값 변경은 A/B 전 보류 요구 |
| API/데이터 3인 검토 | 완료 | 3인 모두 `source_stage/readonly`, dot segment 차단, draft readonly, atomic/cache 안전성 요구 |
| UI/UX 3인 검토 | 완료 | 3인 모두 `_transcriptSourceStage/_transcriptReadonly`, mutation guard, silent polling, source precedence 요구 |
| 구현 | 완료 | API `source_stage/readonly`, draft fallback, dot segment 차단, atomic transcribe checkpoint, Viewer readonly/silent polling 구현 |
| 단위/API/UI 테스트 | 완료 | ruff, API/transcriber/config/UI integration/frontend boundary 통과 |
| QA 3인 검수 | 완료 | QA-A/QA-C 승인, QA-B 지적 수정 후 재승인 |
| 문서 동기화 | 완료 | `docs/STATUS.md` 및 이 계획 문서 업데이트 |

## 8. 이슈 및 반복

### 2026-07-03 STT/리소스 위원회

- STT-A: `word_timestamps=false`가 세그먼트 경계와 화자 병합 UNKNOWN에 영향을 줄 수 있어 기본값 변경 반대.
- STT-B: segment timing은 유지될 가능성이 있으나 boundary drift, timeout thread 잔류, `mlx-whisper` 내부 모델 캐시 미정리 가능성 지적.
- STT-C: A/B 품질/리소스 게이트 없이 기본값 OFF를 "안전"으로 승인할 수 없다고 판단.

결정: `word_timestamps=false` 기본값 변경은 이번 구현에서 제외하고, A/B 게이트 정비 후 별도 승인 대상으로 둔다.

### 2026-07-03 API/데이터 위원회

- API-A: `source_stage`/`readonly`를 모든 source에 명시해야 한다고 지적.
- API-B: `_validate_meeting_id`가 dot-only ID를 허용하는 경로 안전성 블로커 지적.
- API-C: `transcribe.json` direct write와 조기 폴링 사이 partial JSON read 가능성 지적.

결정: draft 노출 구현에 `source_stage/readonly`, dot segment 차단, cache key 강화, atomic checkpoint write를 포함한다.

### 2026-07-03 UI/UX 위원회

- UI-A: 버튼 숨김만으로는 부족하고 `_beginEditUtterance`, `_saveTranscript`, `_openReplaceModal` guard가 필요하다고 지적.
- UI-B: copy/download이 `completed`에 묶여 있어 처리 중 draft에서 허용 액션이 사라지는 문제를 지적.
- UI-C: 조용한 폴링, source precedence, 접근성 있는 draft badge, 처리 단계 표시 유지가 필요하다고 지적.

결정: Viewer 구현은 source/readonly를 상태로 저장하고, 읽기 허용 액션과 mutation 차단을 분리한다.

### 2026-07-03 구현 결과

- `GET /api/meetings/{id}/transcript`가 `corrected → correct → merge → transcribe` 순서로 산출물을 찾는다.
- 응답에 `source_stage`와 `readonly`를 추가했다.
- `transcribe.json`은 `segments`를 `UNKNOWN` speaker의 표시용 utterance로 변환한다.
- `merge`와 `transcribe`는 읽기 전용이다. 편집 endpoint는 기존처럼 `corrected/correct`만 대상으로 유지한다.
- `_validate_meeting_id`는 `"."`, `".."`, 빈 dot segment를 차단한다.
- JSON cache는 `mtime_ns + size`를 사용한다.
- `TranscriptResult.save_checkpoint()`는 원자적 JSON 쓰기를 사용한다.
- Viewer는 `_transcriptSourceStage`, `_transcriptReadonly`, source precedence, silent polling, mutation guard를 갖는다.
- 처리 중 초안에서도 검색/복사/다운로드가 가능하고, inline edit/replace는 차단된다.
- 처리 중에는 `correct/corrected` 산출물이 있더라도 UI와 API 모두 전사문 편집을 차단한다.
- benchmark CLI override는 일부 옵션이 없는 `Namespace`에서도 누락 옵션을 "오버라이드 없음"으로 처리한다.

### 2026-07-03 QA-B 지적 및 수정

- 지적: `correct.json`이 생성된 뒤 job 상태가 아직 `embedding`인 경우, API 응답은 `source_stage="correct"`, `readonly=false`가 되고 Viewer의 mutation guard가 이를 편집 가능으로 해석할 수 있었다.
- 영향: 사용자가 전체 파이프라인 완료 전에 보정 체크포인트를 수정하면 이후 요약/임베딩/완료 산출물과 불일치가 생길 수 있다.
- 수정: API `PUT /transcript`와 `POST /transcript/replace`는 job 상태가 `completed`가 아니면 409를 반환한다. Viewer는 `_canEditTranscript()`를 통해 `completed && !readonly`일 때만 inline edit/replace를 허용한다.
- 회귀 테스트: 처리 중 `correct` 응답은 화면에 표시되고 복사/다운로드는 가능하지만, 더블클릭 편집과 모두 바꾸기는 열리지 않는다. API 편집 요청도 파일 변경 없이 409를 반환한다.
- 재검토: QA-B 승인. 이전 blocking issue 해소, 추가 blocking regression 없음.

### 2026-07-03 추가 전체 테스트 재검증

- 발견: 기본 전체 테스트에서 `tests/test_ai_pipeline_benchmark.py::test_apply_overrides_updates_ai_variant_config`가 실패했다. 원인은 benchmark CLI override 함수가 `argparse.Namespace`에 `diarization_min_speakers`/`diarization_max_speakers`가 항상 있다고 가정한 점이었다.
- 영향: 앱 런타임 파이프라인 회귀는 아니지만, A/B benchmark harness가 부분 Namespace 기반 단위 테스트에서 깨져 release readiness 기준을 만족하지 못했다.
- 수정: `_apply_overrides()`가 모든 선택 인자를 `getattr(..., default)`로 읽도록 바꿔 누락된 옵션을 "오버라이드 없음"으로 처리한다.
- 재검증: 실패 테스트 단독 통과 후 기본 전체 테스트가 3015 passed로 통과했다.

검증 결과:

- `node --check ui/web/viewer-view.js` 통과.
- `.venv/bin/python -m ruff check api/routers/meeting_detail.py steps/transcriber.py scripts/benchmark_ai_pipeline.py tests/test_routes.py tests/test_transcriber.py tests/test_meeting_edit.py tests/test_ai_pipeline_benchmark.py tests/ui/integration/test_spa_overhaul_integration.py` 통과.
- `.venv/bin/pytest tests/test_routes.py -q` 통과: 104 passed.
- `.venv/bin/pytest tests/test_meeting_edit.py::TestUpdateTranscript tests/test_meeting_edit.py::TestTranscriptReplace -q` 통과: 10 passed.
- `.venv/bin/pytest tests/test_transcriber.py -q` 통과: 54 passed.
- `.venv/bin/pytest tests/test_ai_pipeline_benchmark.py -q` 통과: 4 passed.
- `.venv/bin/pytest tests/test_config.py -q` 통과: 76 passed.
- `.venv/bin/pytest tests/harness/test_frontend_boundaries.py -q` 통과: 26 passed.
- `.venv/bin/pytest -m harness -q` 통과: 144 passed, 3096 deselected.
- `.venv/bin/pytest -m ui tests/ui/integration/test_spa_overhaul_integration.py -q` 통과: 59 passed.
- `.venv/bin/pytest tests/ -q` 통과: 3015 passed, 225 deselected.
- `git diff --check` 통과.
