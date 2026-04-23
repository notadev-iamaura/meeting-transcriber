# 이슈 B — 전사 누락 18% 해결 가이드 (다음 세션 재개용)

> 작성: 2026-04-22  
> 목적: 이 문서는 **이슈 B (전사 누락 18%)** 를 다음 세션에서 바로 재개할 수 있도록  
> 선결 조건 → 진단 → 파라미터 실험 → 평가 기준까지 순서대로 기술한다.  
> 독자: 다음 세션의 Claude 또는 사용자 — 바로 실행할 수 있어야 한다.

---

## 1. 요약

**증상**: 14분 45초 회의 파일에서 실제 발화 대비 **28% 구간 전사 누락** 확인 (오디오 전체의 18%). 누락된 7개 구간 중 3개를 재전사하면 모두 명확한 발화가 존재함.

**이 문서의 목적**: 이슈 A (Aggregate Device 지원) 배포 완료를 전제로, 남은 누락의 원인을 `no_speech_threshold` 파라미터 실험으로 체계적으로 해결하기 위한 절차를 기술한다.

---

## 2. 선결 조건

아래 항목이 모두 충족된 후에 이 이슈를 진행한다.  
이슈 A가 해결되기 전에는 "원인 A의 기여분"과 "원인 B의 기여분"을 분리 측정할 수 없다.

| 상태 | 항목 |
|------|------|
| ✅ | **이슈 A (Aggregate Device 지원) 배포 완료** — `feat/coreaudio-aggregate-detection` 브랜치, main에 병합 확인 필요 |
| ⏳ | **사용자 환경에서 Aggregate Device 설정 완료** — 재부팅 후 `bash scripts/setup_audio.sh` 실행 |
| ⏳ | **이슈 A 배포 효과를 실환경에서 새 녹음 1건 이상으로 측정** — 본인 목소리가 정상 캡처되는지 채널별 볼륨 확인 (아래 §3 참조) |

---

## 3. 진단: 이슈 A 후 남은 누락 측정 방법

이슈 A 해결 후 **누락률이 28% → 5~10%** 수준으로 감소할 것으로 추정된다. 이 범위 확인이 이슈 B 진입 전 첫 단계이다.

### 3-1. 새 녹음의 채널별 RMS 확인

Aggregate Device가 정상 동작하면 ch0(마이크)에도 -20dB 수준의 볼륨이 잡혀야 한다.

```bash
# 새 녹음 파일 경로 예시
NEW_FILE="$HOME/.meeting-transcriber/audio_input/meeting_$(date +%Y%m%d)_*.wav"

# 채널별 볼륨 분석 (ch0=마이크, ch1=시스템오디오 추정)
ffmpeg -i "$NEW_FILE" -filter_complex \
  "[0:a]channelsplit=channel_layout=stereo[ch0][ch1]; \
   [ch0]volumedetect[vol0]; \
   [ch1]volumedetect[vol1]" \
  -map "[vol0]" -f null /dev/null \
  -map "[vol1]" -f null /dev/null 2>&1 | grep -E "mean_volume|max_volume"
```

**판단 기준**:
- `ch0 mean_volume` ≥ `-32dB` → 마이크 정상 캡처 ✅
- `ch0 mean_volume` < `-40dB` → 마이크 캡처 여전히 누락 → 이슈 A 재점검 필요 ⚠️

### 3-2. 전사 결과와 발화 구간 비교

1. 새 녹음을 앱에 드롭하거나 자동 감지로 파이프라인 실행
2. 전사 결과(`~/.meeting-transcriber/outputs/{meeting_id}/transcript_merged.json`)에서 발화 타임스탬프 목록 확인
3. ffmpeg `silencedetect`로 오디오 내 발화 구간 추출

```bash
ffmpeg -i "$NEW_FILE" -af silencedetect=noise=-30dB:d=0.5 -f null /dev/null 2>&1 \
  | grep -E "silence_end|silence_start"
```

4. 발화 구간 중 전사 결과가 없는 구간 수 / 전체 발화 구간 수 = **누락률**

### 3-3. 기대 결과

| 시점 | 추정 누락률 |
|------|------------|
| 이슈 A 해결 전 (현재) | 18~28% |
| 이슈 A 해결 후 (추정) | 5~10% |
| 이슈 B 목표 | < 10% |

> **추정**: 이슈 A 해결 후 본인 마이크 채널이 정상 녹음되면 누락의 상당 부분이 해소될 것으로 본다. 만약 이슈 A 해결 후에도 여전히 10% 이상 누락이면 이 이슈 B의 `no_speech_threshold` 조정이 필요하다.

---

## 4. 파라미터 실험 매트릭스

이슈 A 해결 후 남은 누락이 10% 이상이면 아래 6-case 실험을 진행한다.  
현재 설정값: `config.yaml` → `hallucination_filter.no_speech_threshold: 0.9`

| Case | `no_speech_threshold` | `condition_on_previous_text` | 의도 |
|------|----------------------|------------------------------|------|
| 0 (baseline) | `0.9` | `false` | 현재 설정 — 기준선 측정 |
| 1 | `0.8` | `false` | 중간 완화 — 환각 증가 최소화하면서 누락 감소 |
| 2 | `0.75` | `false` | 공격적 완화 — 누락 감소 우선 |
| 3 | `0.8` | `true` | 경계 보정 실험 — 30초 윈도우 경계 누락 보완 |
| 4 | `0.75` | `true` | 최대 완화 — 누락 최소화 극단 케이스 |
| 5 | `0.6` | `false` | 참고용 극단값 — `config.yaml` 주석 "너무 공격적" 확인용 |

> ⚠️ **Case 3, 4 주의**: `condition_on_previous_text: true` 토글은 과거 환각 버그와
> 상호작용할 수 있다 (§7 참조). 이 두 case는 환각률을 특히 주의 깊게 측정할 것.

> ⚠️ **Case 5 주의**: `0.6`은 `config.yaml` 주석에 이미 "너무 공격적"으로 표시되어 있다.
> 환각 급증 여부를 반드시 함께 측정할 것. 적용 배포 대상이 아닌 참고용으로만 실행.

### config.yaml 편집 방법

```yaml
# 각 case 전에 아래 값을 수정
hallucination_filter:
  no_speech_threshold: 0.80  # case 1: 0.80, case 2: 0.75, case 5: 0.60

stt:
  condition_on_previous_text: false  # case 3, 4: true
```

---

## 5. 벤치마크 실행 방법

> ⚠️ **주의**: `scripts/benchmark_stt.py`는 **Zeroth-Korean 데이터셋 (깨끗한 읽기 음성)**
> 기반 CER/WER 측정 스크립트이다. `--case`, `--output /tmp/bench-b/...` 같은 인자는
> **지원하지 않는다**. 실제 CLI는 `--samples`, `--output`, `--openai-model`, `--skip-local`,
> `--skip-api` 옵션만 제공한다.

### 5-1. Zeroth-Korean CER/WER 벤치마크 (각 case별 실행)

각 case에서 `config.yaml`을 편집한 후 로컬 STT만 실행한다:

```bash
source .venv/bin/activate
mkdir -p /tmp/bench-b

# Case 0 (baseline): no_speech_threshold=0.9, condition_on_previous_text=false
python scripts/benchmark_stt.py \
  --samples 50 \
  --skip-api \
  --output /tmp/bench-b/case0_baseline.json

# config.yaml 수정 후 Case 1: no_speech_threshold=0.8
python scripts/benchmark_stt.py \
  --samples 50 \
  --skip-api \
  --output /tmp/bench-b/case1_threshold_08.json

# config.yaml 수정 후 Case 2: no_speech_threshold=0.75
python scripts/benchmark_stt.py \
  --samples 50 \
  --skip-api \
  --output /tmp/bench-b/case2_threshold_075.json
```

> **한계**: `benchmark_stt.py`는 `no_speech_threshold` 파라미터를 직접 적용하지 않는다.
> Zeroth-Korean은 깨끗한 읽기 음성이라 `no_speech_threshold` 영향이 적게 나타날 수 있다.
> **CER/WER 외에 반드시 아래 §5-2의 실환경 누락률 측정을 병행할 것.**

### 5-2. 실환경 누락률 측정 (핵심)

Zeroth-Korean CER/WER와 별개로, **실제 Zoom 회의 녹음으로 누락률을 측정하는 것이 이 이슈의 핵심이다**.

```bash
# 1. 실환경 녹음 파일 확보 (Zoom 회의 1건, 5~15분 권장)
# 2. config.yaml 파라미터 편집 후 앱 재시작
# 3. 파이프라인 실행 (드롭 또는 자동)
# 4. 결과 확인
MEETING_ID="meeting_YYYYMMDD_HHMMSS"  # 실제 meeting_id로 교체
OUTPUT_DIR="$HOME/.meeting-transcriber/outputs/$MEETING_ID"

# 전사된 세그먼트 수 및 타임라인 확인
python3 -c "
import json, pathlib
data = json.loads(pathlib.Path('$OUTPUT_DIR/transcript_merged.json').read_text())
segs = data.get('segments', [])
print(f'전사 세그먼트 수: {len(segs)}')
if segs:
    total_covered = sum(s['end'] - s['start'] for s in segs)
    print(f'전사 커버 시간: {total_covered:.1f}초')
    print(f'첫 세그먼트: {segs[0][\"start\"]:.1f}s ~ {segs[0][\"end\"]:.1f}s')
    print(f'마지막 세그먼트: {segs[-1][\"start\"]:.1f}s ~ {segs[-1][\"end\"]:.1f}s')
"
```

### 5-3. 전체 실험 절차 요약

```bash
# case 별 실험 반복 (case 0~5)
# 1. config.yaml 편집 (no_speech_threshold, condition_on_previous_text)
# 2. 앱 재시작 (config reload)
# 3. 동일 녹음 파일로 파이프라인 재실행 (체크포인트 리셋 후)
# 4. 누락률 + 환각률 기록
# 5. benchmark_stt.py로 CER/WER 측정

# 체크포인트 초기화 (동일 회의 재처리 시)
rm -rf "$HOME/.meeting-transcriber/checkpoints/$MEETING_ID"
```

---

## 6. 평가 기준

### 6-1. 주요 지표

| 지표 | 설명 | 방향 | 측정 방법 |
|------|------|------|----------|
| 누락률 (%) | 발화 구간 대비 전사 없음 비율 | 낮을수록 좋음 | §3-2 방법으로 계산 |
| CER (Character Error Rate) | 전사 정확도 | 낮을수록 좋음 | `benchmark_stt.py` |
| WER (Word Error Rate) | 단어 단위 오류율 | 낮을수록 좋음 | `benchmark_stt.py` |
| 환각률 (%) | 발화 없는 구간에 텍스트가 생성되는 비율 | 낮을수록 좋음 | 수동 확인 또는 필터 로그 |

### 6-2. 트레이드오프

`no_speech_threshold`를 낮추면 더 많은 구간이 "발화"로 분류되어 **누락률은 감소하지만 환각률은 증가**하는 경향이 있다. 이 실험의 핵심은 **"허용 가능한 환각 증가 내에서 누락 최소화"** 지점 찾기이다.

```
no_speech_threshold 낮춤
  → "무음 아님"으로 판정하는 구간 증가
    → 누락률 ↓ (긍정)
    → 환각률 ↑ (부정)
    → hallucination_filter._remove_cross_segment_repetitions 가 일부 상쇄
```

### 6-3. 수용 기준

| 지표 | 목표 |
|------|------|
| 누락률 | < 10% |
| CER | baseline(case 0) 대비 +5%p 이내 |
| 환각률 증가폭 | baseline 대비 1.5배 이내 |

> 세 조건을 동시에 만족하는 case 중 `no_speech_threshold`가 가장 높은 값(보수적)을 선택한다.

---

## 7. 2차 영향 점검

### condition_on_previous_text 와 환각 버그 관계

커밋 `5c176f1` (2026-04-19) 에서 `steps/hallucination_filter.py`에 **크로스 세그먼트 반복 제거** 기능(`_remove_cross_segment_repetitions`)이 추가되었다. 4bit 양자화 모델의 무음 구간 환각 대응 목적이다.

- `condition_on_previous_text: false` (현재 기본값): 각 30초 윈도우가 독립 전사 → 이전 윈도우 텍스트 오류가 다음 윈도우로 전파되지 않음 → `_remove_cross_segment_repetitions` 필요성 낮음
- `condition_on_previous_text: true`: 이전 윈도우 텍스트가 다음 윈도우 컨텍스트로 전달 → 이전 윈도우에서 환각이 발생하면 다음 윈도우에서도 유사 환각 재발 위험 → `_remove_cross_segment_repetitions`가 필요하지만 완전 차단은 어려움

**Case 3, 4 실행 시 점검 항목**:
- [ ] 연속된 세그먼트에서 동일 구문 반복 여부 (`"ohn ohn"`, `"네 네 네"` 류)
- [ ] 필터 로그에서 `cross_segment` 제거 횟수 증가 여부
- [ ] 총 환각 세그먼트 수(raw - 필터 후)가 case 0 대비 급증하면 `condition_on_previous_text: false`로 고정

---

## 8. 예상 작업량

| 항목 | 담당 | 예상 시간 |
|------|------|----------|
| 실환경 녹음 1건 확보 + 이슈 A 효과 측정 | 사용자 액션 | 0.5일 |
| 파라미터 6-case 벤치마크 (자동, long-running) | 자동화 가능 | 1일 |
| 결과 분석 + 최적 파라미터 결정 | Claude | 0.5일 |
| `config.yaml` 수정 + `BENCHMARK.md` 업데이트 + PR | Claude | 0.5일 |

**총 합계**: 2~2.5일 (실환경 녹음 대기 포함)

---

## 9. 예상 결과물

- [ ] `config.yaml::hallucination_filter.no_speech_threshold` 최적값으로 조정
- [ ] (선택) `config.yaml::stt.condition_on_previous_text` 토글 (case 3, 4 결과에 따라)
- [ ] `docs/BENCHMARK.md` 에 "이슈 B 파라미터 실험" 섹션 추가 — case 별 누락률/CER/환각률 비교표
- [ ] 변경 PR (`feat/issue-b-transcription-coverage` 브랜치 권장)

---

## 10. 예상 실패 시나리오 + 폴백

### 시나리오 1: 이슈 A 해결 후에도 누락률 18% 고정

**원인 추정**: `no_speech_threshold`가 핵심 원인일 가능성 높음. case 2 (`0.75`) 이상으로 내리면 개선될 것으로 추정.

**폴백**: case 5 (`0.6`) 결과 확인 후 환각 증가폭이 허용 범위 내면 `0.65~0.70` 중간값 추가 실험.

### 시나리오 2: 모든 case에서 환각률 급증 (수용 기준 초과)

**원인 추정**: Aggregate Device 3채널 오디오 품질 자체가 낮거나, 모델이 해당 녹음 환경에 적합하지 않을 가능성.

**폴백**:
1. `condition_on_previous_text: false` 고정 + `no_speech_threshold` 만 튜닝 (case 1, 2만 유효 범위로 좁힘)
2. STT 모델 변경 검토 — `CLAUDE.md` "STT 모델 선택 가이드" 참조:
   - 현재 기본: `komixv2` (fp16, 환각 최소, 커버리지 85.1%)
   - 대안: `seastar-medium-4bit` (CER 1.25% 최저, 단 무음 구간 환각 취약)
   - `ghost613-turbo-4bit` 는 실사용 부적합 (대량 환각 확인됨, `docs/BENCHMARK.md` §1 참조)

### 시나리오 3: benchmark_stt.py CER/WER 지표와 실환경 누락률 결과가 상충

**원인**: Zeroth-Korean은 깨끗한 읽기 음성 데이터셋 — 실제 Zoom 회의(에코, 잡음, 원거리 마이크)와 특성이 다름.

**폴백**: CER/WER보다 **실환경 누락률을 우선 기준**으로 파라미터를 결정한다. `docs/BENCHMARK.md` §1에도 동일 경고가 기술되어 있음.

---

## 부록: 관련 파일 경로

| 파일 | 역할 |
|------|------|
| `config.yaml` (L183) | `hallucination_filter.no_speech_threshold: 0.9` |
| `config.yaml` (L22) | `stt.condition_on_previous_text: false` |
| `steps/hallucination_filter.py` | 환각 필터 + `_remove_cross_segment_repetitions` |
| `scripts/benchmark_stt.py` | Zeroth-Korean CER/WER 벤치마크 (`--samples`, `--output`, `--skip-api`) |
| `docs/BENCHMARK.md` | 기존 6-case STT 실험 결과 (§1 커버리지 표 포함) |
| `CLAUDE.md` | "STT 모델 선택 가이드" (모델 변경 폴백 시 참조) |
