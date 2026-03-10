# STT 3대 개선 적용 계획서

> **위원회 검증 완료**: 4명의 전문 에이전트(컨텍스트 바이어싱, 후처리, VAD, 아키텍처 리뷰어)의
> 독립 조사 결과를 교차 검증하여 작성됨.

## 현황 요약

| 항목 | 현재 값 | 목표 |
|------|---------|------|
| CER | 11.60% | 7~8% (gpt-4o-transcribe 수준) |
| 디코딩 | greedy (beam search 미구현) | 변경 없음 |
| 후처리 | NFC 정규화만 | + 숫자 정규화 |
| 환각 방지 | 없음 | VAD 전처리 |
| 컨텍스트 | 없음 | initial_prompt |
| `skip_llm_steps` | **true** (기본값) | 변경 없음 |

---

## 위원회 핵심 합의사항

### 합의 1: VAD는 clip_timestamps 방식으로 구현 (방식 B)

**4명 만장일치.** 무음 제거 방식(방식 A)은 타임스탬프 불일치로 화자분리가 완전히 깨진다.

```
[방식 A — 금지]
VAD가 무음 제거 → 새 WAV 생성 → STT 타임스탬프가 원본과 달라짐
→ pyannote는 원본 WAV 사용 → merger에서 겹침=0 → 모든 화자 UNKNOWN

[방식 B — 채택]
VAD가 음성 구간 감지 → clip_timestamps=[s1,e1,s2,e2,...] → Whisper에 전달
→ Whisper가 원본 WAV에서 해당 구간만 처리 → 타임스탬프 원본 기준 유지
→ pyannote도 원본 WAV 사용 → merger 정상 동작
```

### 합의 2: 숫자 정규화는 화이트리스트 기반 보수적 접근

**후처리 전문가 + 아키텍처 리뷰어 합의.** 한국어 숫자의 중의성 문제가 심각하다.

```
치명적 오변환 사례:
"일을 마쳤습니다" → "1을 마쳤습니다"  (일=work, 숫자 아님)
"삼성전자"        → "3성전자"         (고유명사)
"이 프로젝트"     → "2 프로젝트"      (이=this, 숫자 아님)
"사과"            → "4과"             (과일)
```

**대책**: 단독 숫자("일","이","사","오")는 절대 변환 금지.
반드시 **한글숫자 + 단위어** 조합일 때만 변환.

### 합의 3: initial_prompt는 greedy에서도 작동하나 효과 제한적

**컨텍스트 전문가 확인.** mlx-whisper 소스 코드 추적 결과:
- `initial_prompt` → 토큰화 → `<|startofprev|>` + prompt_tokens → 디코더 attention에 영향
- greedy decoding(temperature=0)에서 argmax만 선택하므로 beam search 대비 효과 약함
- 첫 30초 윈도우에만 직접 적용, 이후는 간접 전파 (점차 소멸)
- 잘못된 키워드는 오히려 정확도를 낮출 수 있음

### 합의 4: 세 개선을 동시 적용하되, 각각 config로 독립 비활성화 가능하게

**아키텍처 리뷰어의 우려사항 반영.** 각 개선은 `enabled: false`로 즉시 롤백 가능해야 한다.

---

## 아키텍처 리뷰어의 주요 우려와 대응

| 우려 | 심각도 | 대응 |
|------|--------|------|
| VAD 타임스탬프 불일치 | **치명적** | clip_timestamps 방식으로 해결. 원본 WAV 기준 유지 |
| VAD + Context Biasing 상호 약화 | 중간 | VAD는 Whisper 내부 세그먼트를 건드리지 않음. clip_timestamps는 Whisper가 내부적으로 30초 윈도우를 관리 |
| 숫자 정규화 + LLM 보정 이중 처리 | 중간 | 정규화를 LLM 이전에 적용 + LLM 프롬프트에 "숫자 역변환 금지" 규칙 추가 |
| PipelineStep 열거형 변경 | 높음 | VAD를 별도 단계로 추가하지 않음. TRANSCRIBE 내부에서 처리 |
| 세 곳이 동시에 config.py 수정 | 중간 | 설계를 확정한 뒤 한 명이 config를 일괄 수정 |
| 기존 1,165개 테스트 파괴 | 높음 | 변경 파일별 영향 테스트 목록을 사전 파악하고 업데이트 |
| `skip_llm_steps: true` 기본값 | 중간 | 숫자 정규화는 LLM 독립 동작. LLM 스킵해도 정규화 실행 |

---

## 구현 계획

### 변경 파일 목록

| 파일 | 변경 유형 | 영향 범위 |
|------|-----------|-----------|
| `config.py` | 수정 | STTConfig + VADConfig + NumberNormConfig 추가 |
| `config.yaml` | 수정 | stt.initial_prompt + vad 섹션 + number_normalization 섹션 |
| `steps/transcriber.py` | 수정 | initial_prompt 전달 + VAD 결과 수신 |
| `steps/vad_detector.py` | **신규** | Silero VAD 래퍼 |
| `steps/number_normalizer.py` | **신규** | 한글 숫자 → 아라비아 숫자 변환 |
| `steps/corrector.py` | 수정 | 시스템 프롬프트에 숫자 역변환 금지 규칙 추가 |
| `core/pipeline.py` | 수정 | TRANSCRIBE 내 VAD 호출 + CORRECT 전 숫자 정규화 호출 |
| `tests/test_vad_detector.py` | **신규** | VAD 단위 테스트 |
| `tests/test_number_normalizer.py` | **신규** | 숫자 정규화 단위 테스트 |
| `tests/test_transcriber.py` | 수정 | initial_prompt 파라미터 추가 반영 |
| `tests/test_config.py` | 수정 | 새 config 필드 검증 |

---

### Step 1: config.py — 3개 설정 모델 추가

```python
# === VAD 설정 ===
class VADConfig(BaseModel):
    """VAD (Voice Activity Detection) 설정."""
    enabled: bool = False  # 기본 비활성화 (안전 우선)
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    min_speech_duration_ms: int = Field(default=250, ge=50, le=2000)
    min_silence_duration_ms: int = Field(default=100, ge=30, le=2000)
    speech_pad_ms: int = Field(default=30, ge=0, le=500)

# === 숫자 정규화 설정 ===
class NumberNormalizationConfig(BaseModel):
    """한국어 숫자 정규화 설정."""
    enabled: bool = False  # 기본 비활성화 (안전 우선)
    level: int = Field(default=1, ge=0, le=2)
    # level 0: 비활성화
    # level 1: 보수적 (한글숫자+단위어 조합만 변환)
    # level 2: 중간 (복합 숫자 + 단위 포함)

# === STTConfig 필드 추가 ===
class STTConfig(BaseModel):
    # ... 기존 필드 유지 ...
    initial_prompt: str | None = Field(
        default=None,
        description="전사 컨텍스트 힌트. None 또는 빈 문자열이면 비활성화."
    )

    @field_validator("initial_prompt")
    @classmethod
    def normalize_initial_prompt(cls, v: str | None) -> str | None:
        """빈 문자열을 None으로 정규화한다."""
        if v is not None and v.strip() == "":
            return None
        return v

# === AppConfig에 추가 ===
class AppConfig(BaseModel):
    # ... 기존 필드 ...
    vad: VADConfig = VADConfig()
    number_normalization: NumberNormalizationConfig = NumberNormalizationConfig()
```

**설계 근거**:
- 모든 개선이 `enabled: false`가 기본값 → 배포 직후 기존 동작 100% 유지
- Pydantic Field 범위 검증으로 잘못된 설정값 방지
- initial_prompt의 빈 문자열 → None 변환으로 일관된 처리

---

### Step 2: config.yaml — 설정 추가

```yaml
stt:
  model_name: "youngouk/whisper-medium-komixv2-mlx"
  language: "ko"
  beam_size: 5
  batch_size: 12
  initial_prompt: null  # 비활성화. 예: "분기 매출 KPI 스프린트 리뷰"

vad:
  enabled: false         # 먼저 false로 배포, 검증 후 true 전환
  threshold: 0.5
  min_speech_duration_ms: 250
  min_silence_duration_ms: 100
  speech_pad_ms: 30

number_normalization:
  enabled: false         # 먼저 false로 배포, 검증 후 true 전환
  level: 1               # 보수적 (단위어 동반 시만 변환)
```

---

### Step 3: steps/vad_detector.py — 신규 작성

**핵심 설계 결정**:

| 결정 | 선택 | 근거 |
|------|------|------|
| ModelLoadManager 사용 | **안 함** | 1.8MB 소형 모델. acquire/release 오버헤드 불필요 |
| 디바이스 | **CPU 강제** | pyannote MPS 버그 정책과 동일. CPU에서 1ms/30ms 충분 |
| 출력 형식 | **clip_timestamps** | 원본 타임스탬프 보존. merger 수정 불필요 |
| PipelineStep 추가 | **안 함** | TRANSCRIBE 내부에서 호출. 체크포인트 호환성 유지 |
| torch.set_num_threads | **호출 안 함** | 전역 설정 변경은 임베딩 단계에 영향. 제거 |
| 음성 구간 0개 | **None 반환** | Transcriber가 전체 오디오 처리로 폴백 |

**clip_timestamps 무한 루프 버그 안전장치**:
```python
# mlx-whisper 버그: 마지막 end == audio_duration이면 무한 루프
if is_last_segment and abs(end - audio_duration) < 0.2:
    end = max(0.0, audio_duration - 0.1)
```

**메모리 영향 계산**:
```
Silero VAD:  ~1.8MB
+ Whisper:   ~1,500MB
= 합계:     ~1,502MB  (9.5GB 제한의 15.8%)
→ 안전
```

---

### Step 4: steps/number_normalizer.py — 신규 작성

**화이트리스트 기반 보수적 변환 (Level 1)**:

```python
# 절대 단독 변환 금지 목록 (중의성 높은 한글 숫자)
_NEVER_STANDALONE = {"일", "이", "삼", "사", "오", "육", "칠", "팔", "구"}

# 안전한 단위어 목록 (이 단위가 뒤따를 때만 변환)
_SAFE_UNITS = {
    "퍼센트", "%", "프로",        # 비율
    "년", "월", "분기",           # 시간
    "개", "개월", "건",           # 수량
    "명", "분", "인",             # 인원
    "만", "억", "조", "원",       # 금액
    "번", "차", "회", "호", "층", # 순서/건물
    "km", "m", "cm", "kg", "g",  # SI 단위
    "배", "배로",                 # 배수
}

# 고유명사 보호 목록 (이 패턴은 무조건 스킵)
_BRAND_PREFIXES = {
    "삼성", "삼양", "삼화", "삼천리",
    "일동", "일진", "일양",
    "이마트", "이화", "이랜드",
    "사조", "오뚜기", "오리온",
    "칠성", "팔도", "구미",
}
```

**변환 흐름**:
```
입력: "삼십 퍼센트 성장, 삼성전자 주가"

1. 고유명사 보호: "삼성전자" → 스킵 마킹
2. 패턴 매칭: "삼십" + "퍼센트" → 안전한 단위 조합
3. 변환: "삼십" → "30"
4. 보호 해제

출력: "30 퍼센트 성장, 삼성전자 주가"
```

**변환 규칙 (Level 1)**:

| 패턴 | 예시 | 변환 |
|------|------|------|
| 십/백/천 + 단위어 | "삼십 퍼센트" | "30 퍼센트" |
| 천/만/억 + 단위어 | "이천 만 원" | "2000 만 원" |
| 복합 수 + 단위어 | "이백오십 명" | "250 명" |
| 혼합형 + 단위어 | "3십 퍼센트" | "30 퍼센트" |
| 단독 숫자 | "일을 마쳤다" | **변환 안 함** |
| 고유명사 | "삼성전자" | **변환 안 함** |

**파이프라인 삽입 위치**: `_run_step_correct()` 호출 직전

```
MERGE 결과 → [숫자 정규화] → CORRECT (LLM 보정 또는 스킵) → SUMMARIZE
```

이 위치가 최적인 이유:
1. `skip_llm_steps: true`여도 정규화는 실행됨
2. LLM에 정규화된 텍스트 전달 → 토큰 효율적
3. merger 출력은 아직 최종 텍스트가 아니므로 수정 적합

---

### Step 5: steps/transcriber.py — 수정

**변경 1: initial_prompt 전달 (fallback 경로만)**

```python
# 현재 (라인 312-320, fallback 경로 — 실제로 항상 이 경로 실행)
raw_result = await asyncio.to_thread(
    whisper_module.transcribe,
    str(audio_path),
    path_or_hf_repo=self._model_name,
    language=self._language,
    word_timestamps=False,
)

# 변경 후
kwargs = {
    "path_or_hf_repo": self._model_name,
    "language": self._language,
    "word_timestamps": False,
}
if self._initial_prompt is not None:
    kwargs["initial_prompt"] = self._initial_prompt
if vad_clip_timestamps is not None:
    kwargs["clip_timestamps"] = vad_clip_timestamps

raw_result = await asyncio.to_thread(
    whisper_module.transcribe,
    str(audio_path),
    **kwargs,
)
```

**변경 2: transcribe() 시그니처에 vad_clip_timestamps 추가**

```python
async def transcribe(
    self,
    audio_path: Path,
    vad_clip_timestamps: list[float] | None = None,
) -> TranscriptResult:
```

**변경 3: __init__에서 initial_prompt 캐싱**

```python
self._initial_prompt = self._config.stt.initial_prompt
```

**위험 완화**: beam_size 전달하는 첫 번째 경로(항상 NotImplementedError)에는 initial_prompt를 추가하지 않음. 어차피 실행되지 않는 경로에 변경을 가하면 혼란만 초래.

---

### Step 6: core/pipeline.py — 수정

**변경 1: TRANSCRIBE 단계에서 VAD 호출**

```python
async def _run_step_transcribe(self, wav_path, ...):
    # VAD 전처리 (enabled=false이면 None 반환)
    vad_clip_timestamps = None
    if self._config.vad.enabled:
        from steps.vad_detector import VoiceActivityDetector
        vad = VoiceActivityDetector(self._config)
        vad_result = await vad.detect(wav_path)
        if vad_result is not None:
            vad_clip_timestamps = vad_result.clip_timestamps

    # 기존 Transcriber 호출 (vad 결과 전달)
    transcriber = Transcriber(self._config, self._model_manager)
    result = await transcriber.transcribe(
        wav_path,
        vad_clip_timestamps=vad_clip_timestamps,
    )
    return result
```

**변경 2: CORRECT 단계 전에 숫자 정규화 적용**

```python
async def _run_step_correct(self, merged_result, ...):
    # 숫자 정규화 전처리 (LLM 독립, skip_llm_steps=true에서도 동작)
    if self._config.number_normalization.enabled:
        from steps.number_normalizer import normalize_numbers
        for utt in merged_result.utterances:
            utt.text = normalize_numbers(
                utt.text,
                level=self._config.number_normalization.level,
            )

    # 기존 LLM 보정 (skip_llm_steps=true이면 패스스루)
    if self._config.skip_llm_steps:
        corrected_result = merged_result  # 패스스루
    else:
        corrector = Corrector(self._config, ...)
        corrected_result = await corrector.correct(merged_result)
    ...
```

---

### Step 7: steps/corrector.py — 시스템 프롬프트 수정

```python
# 기존 규칙에 추가 (라인 38-49)
_SYSTEM_PROMPT = """...기존 규칙...
8. 아라비아 숫자(예: 30, 250)는 절대 한글 숫자로 변환하지 마세요. 숫자 표기를 그대로 유지하세요.
"""
```

---

### Step 8: 테스트 작성

#### tests/test_vad_detector.py (신규, ~12개 테스트)

| 테스트 | 검증 |
|--------|------|
| `test_VAD_비활성화시_None_반환` | enabled=false → None |
| `test_파일_미존재시_FileNotFoundError` | 잘못된 경로 |
| `test_clip_timestamps_형식_변환` | [{'start':1,'end':3}] → [1.0,3.0] |
| `test_마지막_타임스탬프_무한루프_방지` | end==duration → end=duration-0.1 |
| `test_빈_음성구간시_None_반환` | 음성 없음 → 전체 오디오 폴백 |
| `test_CPU_강제_실행` | model.device == "cpu" |
| `test_ModelLoadManager_미사용` | acquire 호출 없음 |
| `test_VADResult_데이터_정확성` | total_speech + total_silence ≈ duration |
| `test_config_threshold_범위_검증` | 0.0~1.0 외 값 거부 |
| `test_config_기본값` | enabled=False, threshold=0.5 |
| `test_silero_미설치시_VADError` | ImportError → VADError |
| `test_다중_음성구간_clip_timestamps` | 여러 구간 올바른 변환 |

#### tests/test_number_normalizer.py (신규, ~20개 테스트)

**정상 변환 (Level 1)**:

| 테스트 | 입력 | 기대 출력 |
|--------|------|-----------|
| `test_삼십_퍼센트` | "삼십 퍼센트" | "30 퍼센트" |
| `test_이백오십_명` | "이백오십 명" | "250 명" |
| `test_이천이십육_년` | "이천이십육 년" | "2026 년" |
| `test_오백_만_원` | "오백 만 원" | "500 만 원" |
| `test_혼합형_3십_퍼센트` | "3십 퍼센트" | "30 퍼센트" |
| `test_이미_아라비아_유지` | "30 퍼센트" | "30 퍼센트" |
| `test_복합문장` | "매출 삼십 퍼센트, 직원 이백 명" | "매출 30 퍼센트, 직원 200 명" |

**변환 금지 (위양성 방지)**:

| 테스트 | 입력 | 기대 (변환 없음) |
|--------|------|------------------|
| `test_일을_마쳤습니다` | "일을 마쳤습니다" | "일을 마쳤습니다" |
| `test_삼성전자` | "삼성전자 주가" | "삼성전자 주가" |
| `test_이_프로젝트` | "이 프로젝트에서" | "이 프로젝트에서" |
| `test_사과` | "사과를 먹었다" | "사과를 먹었다" |
| `test_이마트` | "이마트에서" | "이마트에서" |
| `test_오뚜기` | "오뚜기 라면" | "오뚜기 라면" |
| `test_이번_달` | "이번 달에" | "이번 달에" |
| `test_일단` | "일단 시작하자" | "일단 시작하자" |

**경계 케이스**:

| 테스트 | 검증 |
|--------|------|
| `test_빈_문자열` | "" → "" |
| `test_숫자_없는_문장` | 변환 없이 통과 |
| `test_level_0_비활성화` | 아무것도 변환 안 함 |
| `test_enabled_false` | normalize 호출 안 됨 |
| `test_NFC_정규화_유지` | 기존 NFC 정규화 깨지지 않음 |

#### tests/test_transcriber.py (수정)

```python
# 기존 파라미터 검증 테스트 업데이트
async def test_whisper_transcribe_호출_파라미터(self, ...):
    # initial_prompt=None이면 kwargs에 포함되지 않아야 함
    call_kwargs = mock_whisper.transcribe.call_args[1]
    assert "initial_prompt" not in call_kwargs  # None이면 전달 안 함

async def test_initial_prompt_전달(self, ...):
    # config에 initial_prompt 설정 시 kwargs에 포함
    config.stt.initial_prompt = "분기 매출 KPI"
    ...
    call_kwargs = mock_whisper.transcribe.call_args[1]
    assert call_kwargs["initial_prompt"] == "분기 매출 KPI"

async def test_vad_clip_timestamps_전달(self, ...):
    # vad_clip_timestamps 전달 시 kwargs에 포함
    result = await transcriber.transcribe(
        audio_path, vad_clip_timestamps=[1.0, 5.0, 8.0, 12.0]
    )
    call_kwargs = mock_whisper.transcribe.call_args[1]
    assert call_kwargs["clip_timestamps"] == [1.0, 5.0, 8.0, 12.0]
```

#### tests/test_config.py (수정)

```python
def test_VADConfig_기본값(self):
    config = VADConfig()
    assert config.enabled is False
    assert config.threshold == 0.5

def test_NumberNormalizationConfig_기본값(self):
    config = NumberNormalizationConfig()
    assert config.enabled is False
    assert config.level == 1

def test_STTConfig_initial_prompt_빈문자열_None_변환(self):
    config = STTConfig(initial_prompt="")
    assert config.initial_prompt is None

def test_STTConfig_initial_prompt_공백만_None_변환(self):
    config = STTConfig(initial_prompt="   ")
    assert config.initial_prompt is None
```

---

## 서브에이전트 팀 구성

### Wave 1: 병렬 구현

| 에이전트 | 작업 | 파일 |
|---------|------|------|
| **agent-config** | Step 1-2: config 수정 | `config.py`, `config.yaml`, `tests/test_config.py` |
| **agent-vad** | Step 3 + 6(VAD 부분): VAD 모듈 + 파이프라인 VAD 연동 | `steps/vad_detector.py`, `tests/test_vad_detector.py`, `core/pipeline.py`(VAD 부분만) |
| **agent-normalizer** | Step 4 + 6(정규화 부분) + 7: 숫자 정규화 + 파이프라인 연동 + corrector 프롬프트 | `steps/number_normalizer.py`, `tests/test_number_normalizer.py`, `core/pipeline.py`(정규화 부분만), `steps/corrector.py` |
| **agent-transcriber** | Step 5 + 8(transcriber 테스트): transcriber 수정 | `steps/transcriber.py`, `tests/test_transcriber.py` |

### Wave 2: 통합 리뷰

| 에이전트 | 작업 |
|---------|------|
| **agent-reviewer** | 전체 코드 리뷰 + `pytest tests/ --ignore=tests/test_model_manager.py -x -q` + 문제 수정 |

### Wave 3: 반복 (문제 발견 시)

reviewer가 문제 발견 → 해당 에이전트에 수정 요청 → 재리뷰.
**모든 테스트 통과할 때까지 반복.**

---

## 의존성 설치

```bash
# silero-vad 설치 (torch는 이미 pyannote 의존성으로 설치됨)
pip install silero-vad
```

---

## 검증 계획

```bash
# 1. 전체 단위 테스트 (기존 + 신규)
pytest tests/ --ignore=tests/test_model_manager.py -x -q

# 2. 신규 테스트만
pytest tests/test_vad_detector.py tests/test_number_normalizer.py -v

# 3. 영향받는 기존 테스트
pytest tests/test_transcriber.py tests/test_config.py tests/test_corrector.py -v

# 4. 실제 오디오 E2E 테스트 (수동, enabled=true 전환 후)
# config.yaml에서 vad.enabled=true, number_normalization.enabled=true 설정
# python main.py로 실제 회의 녹음 전사하여 결과 비교
```

---

## 롤백 전략

| 상황 | 대응 | 소요 |
|------|------|------|
| VAD가 환각 유발 | `config.yaml: vad.enabled: false` | 1초 |
| 숫자 정규화 오변환 | `config.yaml: number_normalization.enabled: false` | 1초 |
| initial_prompt 악화 | `config.yaml: stt.initial_prompt: null` | 1초 |
| 전체 롤백 | 위 세 줄 모두 원복 | 3초 |
| silero-vad 충돌 | `pip uninstall silero-vad` | 10초 |
| 코드 수준 롤백 | `git revert <commit>` | 30초 |

모든 개선이 **config 1줄 변경으로 즉시 비활성화** 가능. 코드 롤백 불필요.

---

## 예상 신규 테스트 수

| 영역 | 테스트 수 |
|------|----------|
| VAD | ~12개 |
| 숫자 정규화 | ~20개 |
| transcriber 수정 | ~3개 |
| config 수정 | ~4개 |
| **합계** | **~39개** |

최종: 기존 1,165 + ~39 = **~1,204개**
