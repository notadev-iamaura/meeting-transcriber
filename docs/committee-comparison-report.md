# MacWhisper vs meeting-transcriber 위원회 비교 분석 보고서

> **작성일**: 2026-03-09
> **분석 방법**: 4명의 독립 전문가 에이전트(STT/오디오, AI/데이터, 아키텍처, UX/제품)가
> 실제 소스 코드를 읽고 1~10점 척도로 채점. 위원장이 종합.
> **대상**: MacWhisper (Argmax WhisperKit CoreML) vs meeting-transcriber (MLX+pyannote Python)

---

## 1. 종합 점수표

| 영역 | MacWhisper | meeting-transcriber | 승자 | 격차 |
|------|:---------:|:-------------------:|:----:|:----:|
| **STT/오디오** | **8.25** | 6.04 | MacWhisper | +2.21 |
| **AI/데이터 인텔리전스** | 2.9 | **9.3** | meeting-transcriber | +6.4 |
| **시스템 아키텍처** | 6.8 | **7.3** | meeting-transcriber | +0.5 |
| **UX/제품** | 7.7 | **7.8** | meeting-transcriber | +0.1 |
| **총 평균** | **6.41** | **7.61** | **meeting-transcriber** | **+1.2** |

---

## 2. 영역별 상세 평가

### 2.1 STT/오디오 (STT 전문가)

MacWhisper가 **STT 속도(+3점)**, **리소스 효율(+3점)**, **화자분리 속도(+6점)**, **녹음 품질(+4점)**에서 압도적.

| 항목 | MW | MT | 차이 |
|------|:--:|:--:|:----:|
| STT 정확도 (한국어) | 8.0 | **8.5** | MT +0.5 |
| STT 속도 | **9.0** | 6.0 | MW +3.0 |
| 리소스 효율 | **9.0** | 6.0 | MW +3.0 |
| 화자분리 정확도 | 7.5 | **8.0** | MT +0.5 |
| 화자분리 속도 | **9.0** | 3.0 | MW +6.0 |
| 녹음 품질 | **9.0** | 5.0 | MW +4.0 |
| 녹음 편의성 | **9.0** | 5.0 | MW +4.0 |

**핵심 판정**: 화자분리 CPU 강제가 가장 치명적인 병목 (CoreML 대비 10~12배 느림).

---

### 2.2 AI/데이터 인텔리전스 (AI 전문가)

meeting-transcriber가 **모든 항목**에서 압도적 우위. MacWhisper에는 LLM/RAG/벡터검색이 아예 없음.

| 항목 | MW | MT | 차이 |
|------|:--:|:--:|:----:|
| 발화 보정 | 2.0 | **9.0** | MT +7.0 |
| 회의록 생성 | 3.0 | **9.0** | MT +6.0 |
| 프라이버시 | 6.0 | **10.0** | MT +4.0 |
| 키워드 검색 | 7.0 | **9.0** | MT +2.0 |
| 의미 검색 | 1.0 | **9.0** | MT +8.0 |
| 하이브리드 검색 | 2.0 | **10.0** | MT +8.0 |
| RAG 채팅 | 0.0 | **10.0** | MT +10.0 |

**핵심 판정**: 두 시스템은 완전히 다른 카테고리. MacWhisper=전사 도구, MT=회의 인텔리전스 플랫폼.

---

### 2.3 시스템 아키텍처 (아키텍처 전문가)

MacWhisper가 **하드웨어 최적화**(양자화, 칩셋 적응)에서 우위.
meeting-transcriber가 **소프트웨어 엔지니어링**(장애복구, 상태관리, 테스트)에서 우위.

| 항목 | MW | MT | 차이 |
|------|:--:|:--:|:----:|
| 양자화 전략 | **9.0** | 2.0 | MW +7.0 |
| 칩셋 적응 | **9.0** | 5.0 | MW +4.0 |
| 장애 복구 | 6.0 | **9.0** | MT +3.0 |
| 리소스 보호 | 5.0 | **9.0** | MT +4.0 |
| 상태 관리 | 6.0 | **10.0** | MT +4.0 |
| 테스트 커버리지 | 5.0 | **8.0** | MT +3.0 |
| 코드 품질 | 7.0 | **9.0** | MT +2.0 |

**핵심 판정**: MacWhisper의 "하드웨어 최적화 사고방식"을 meeting-transcriber에 이식할 여지 큼.

---

### 2.4 UX/제품 (UX 전문가)

근소한 차이. MacWhisper=완성된 제품, meeting-transcriber=성장하는 플랫폼.

| 항목 | MW | MT | 차이 |
|------|:--:|:--:|:----:|
| 시각적 완성도 | **9.0** | 6.5 | MW +2.5 |
| 완성도 | **9.5** | 7.5 | MW +2.0 |
| 검색/분석 흐름 | 5.0 | **8.5** | MT +3.5 |
| 확장 가능성 | 4.0 | **8.5** | MT +4.5 |

**핵심 판정**: MacWhisper는 더 이상 발전 방향이 제한적. meeting-transcriber는 성장 여지가 큼.

---

## 3. 위원회 만장일치 합의사항

### 합의 1: 두 시스템은 경쟁이 아니라 다른 카테고리

```
MacWhisper    = "고속 전사 도구" (STT Tool)
meeting-trans = "회의 인텔리전스 플랫폼" (Meeting Intelligence Platform)
```

### 합의 2: 각 시스템의 절대적 강점

| MacWhisper 절대 우위 | meeting-transcriber 절대 우위 |
|:-------------------:|:---------------------------:|
| CoreML + Neural Engine 속도 | 로컬 LLM 통합 (보정/요약/채팅) |
| 멀티트랙 분리 녹음 | RRF 하이브리드 검색 (벡터+FTS5) |
| 다단계 양자화 (W8A16) | JSON 체크포인트 장애 복구 |
| 상용 제품 완성도 | 완전 로컬 프라이버시 |

### 합의 3: meeting-transcriber의 유일한 치명적 약점

> **화자분리 CPU 강제** — 4명 전원 지적. pyannote MPS 버그로 인한 CPU 강제 실행은
> CoreML 대비 10~12배 느린 속도를 초래하며, 이는 실사용성에 심각한 영향을 미침.

---

## 4. MacWhisper에서 차용 가능한 기술 (M4 최적화 로드맵)

### 4.1 현재 시스템 진단 (코드 기반 사실)

| 항목 | 현 상태 | 코드 위치 |
|------|--------|----------|
| **STT 모델** | whisper-medium-ko-zeroth (full precision, ~1.5GB) | config.yaml L16 |
| **화자분리** | pyannote 3.1, CPU 강제 | diarizer.py L199, config.py L106 |
| **LLM** | EXAONE 3.5 7.8B 4bit (~5GB) | config.yaml L34 |
| **메모리 제한** | peak_ram_limit_gb: 9.5 | config.yaml L88 |
| **배치 크기** | 12 (설정만 존재, **실제 전달 안됨**) | transcriber.py L312-319 |
| **Neural Engine** | 미활용 (MLX는 Metal GPU만 사용) | model_manager.py |
| **양자화** | EXAONE만 4bit, whisper/pyannote는 없음 | — |

---

### 4.2 M4 MacBook Air 최적화 로드맵

MacWhisper의 기술을 참고하여, M4 16GB 환경에서 meeting-transcriber를 최적화하기 위한 구체적 로드맵입니다.

#### Phase A: 즉시 적용 가능 (코드 변경 최소)

##### A-1. batch_size 파라미터 실제 전달 (난이도: 낮음)

**문제**: config.yaml에 `batch_size: 12`가 있지만, transcriber.py에서 **실제로 전달하지 않음**.

**현재 코드** (transcriber.py L312-319):
```python
raw_result = await asyncio.to_thread(
    whisper_module.transcribe,
    str(audio_path),
    path_or_hf_repo=self._model_name,
    language=self._language,
    word_timestamps=False,  # batch_size 전달 없음
)
```

**개선**: mlx-whisper API 확인 후 `batch_size` 파라미터 추가.
M4 16GB에서 최적값은 **프로파일링 필요** (8~16 범위 테스트).

**기대 효과**: STT 속도 10~30% 향상 가능

##### A-2. Metal 캐시 관리 최적화 (난이도: 낮음)

**현재**: model_manager.py에서 `mx.metal.clear_cache()` 호출.
**개선**: 모델 전환 시점에 추가적인 `gc.collect()` + `mx.metal.clear_cache()` 호출 순서 최적화.

**기대 효과**: 모델 전환 시 메모리 회수 속도 개선

---

#### Phase B: MacWhisper 양자화 전략 차용 (난이도: 중)

##### B-1. whisper 양자화 모델 도입

**MacWhisper 참고**: W8A16 양자화로 2.7GB → 626MB (77% 감소)

**meeting-transcriber 적용안**:
- `mlx-community/whisper-medium-mlx` → **4bit 양자화 버전** 탐색
- 현재 ~1.5GB → 양자화 시 ~400MB 예상
- **정확도 영향**: MacWhisper는 W8A16으로 정확도 손실 1~2% 미만 달성

**구현 방안**:
```yaml
# config.yaml
stt:
  model_name: "mlx-community/whisper-medium-ko-4bit"  # 양자화 모델
  # 또는 config에 quantize 옵션 추가
```

**기대 효과**: 메모리 50~70% 절감 → whisper-large-v3 양자화 도입 여지 확보

##### B-2. whisper-large-v3 조건부 지원

**메모리 계산** (순차 실행 기준):
```
whisper-large-v3 (full):  ~2.7GB   → 양자화 시 ~700MB
pyannote (CPU):           ~1.2GB   (whisper 언로드 후)
EXAONE 4bit:              ~5.0GB   (pyannote 언로드 후)
OS + 앱:                  ~3.5GB
─────────────────────────────────
피크: max(2.7, 1.2, 5.0) + 3.5 = 8.5GB < 9.5GB 제한 ✅
```

**구현 방안**: config.yaml에 모델 선택 옵션 추가
```yaml
stt:
  model_name: "mlx-community/whisper-large-v3-mlx"  # large-v3
  # model_name: "mlx-community/whisper-medium-ko-zeroth"  # 기존 한국어 특화
```

**주의**: 한국어 파인튜닝 모델(medium-ko)이 범용 large-v3보다 한국어에서 더 정확할 수 있음.
실제 WER 비교 테스트 후 결정 권장.

---

#### Phase C: 화자분리 가속 (난이도: 높음, 효과: 최대)

##### C-1. pyannote MPS 지원 재테스트

**현재 상태**: diarizer.py와 config.py에서 MPS 강제 차단

**MacWhisper 참고**: CoreML로 pyannote를 포팅하여 10~12배 가속 달성

**meeting-transcriber 적용안** (단계적):

1. **1단계**: pyannote-audio 최신 버전에서 MPS 버그 해결 여부 확인
   ```python
   # 테스트 코드
   import torch
   from pyannote.audio import Pipeline
   pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1")
   pipe.to(torch.device("mps"))  # M4에서 테스트
   ```

2. **2단계**: MPS 지원 시 config validator 수정
   ```python
   # config.py — device 검증 완화
   @field_validator("device")
   def validate_device(cls, v: str) -> str:
       if v.lower() == "mps" and not _mps_available():
           return "cpu"
       return v
   ```

3. **3단계**: MPS 미지원 시 대안 검토
   - ONNX Runtime으로 pyannote 포팅 (CoreML 대신)
   - 또는 pyannote 경량 모델 사용 (속도 우선)

**기대 효과**: MPS 지원 시 화자분리 **5~10배 가속** (3점 → 7~8점)

##### C-2. 화자분리 타임아웃 동적 조정

**현재**: 고정 600초 (10분)
**개선**: 오디오 길이 기반 동적 타임아웃
```python
timeout = max(600, audio_duration_seconds * 2)  # 최소 10분, 오디오 길이의 2배
```

---

#### Phase D: MacWhisper의 멀티트랙 녹음 차용 (난이도: 중~높음)

##### D-1. ffmpeg 멀티트랙 분리 녹음

**MacWhisper 참고**: 앱 오디오(상대방) + 마이크(사용자) + merged 3트랙 동시 저장

**meeting-transcriber 적용안**:
```bash
# 2개 ffmpeg 인스턴스 동시 실행
ffmpeg -f avfoundation -i ":BlackHole" -ac 1 -ar 16000 system.wav  # 시스템 오디오
ffmpeg -f avfoundation -i ":MacBook Air Microphone" -ac 1 -ar 16000 mic.wav  # 마이크
# 후처리로 merged.wav 생성
ffmpeg -i system.wav -i mic.wav -filter_complex amix=inputs=2 merged.wav
```

**장점**:
- 화자분리 정확도 +5~10% (마이크=사용자, 시스템=상대방 자동 구분)
- STT 정확도 향상 (깨끗한 채널별 전사)

**구현 복잡도**: recorder.py 수정 + diarizer.py 채널 기반 화자 할당 로직 추가

---

#### Phase E: 칩셋 적응 전략 차용 (난이도: 중)

##### E-1. M4 칩셋 감지 + 자동 최적화

**MacWhisper 참고**: A12~M4 자동 감지 후 최적 모델/양자화 선택

**meeting-transcriber 적용안**:
```python
# config.py 또는 별도 모듈
import platform

def get_optimal_config() -> dict:
    """M4 칩셋 감지 후 최적 설정 반환."""
    chip = platform.processor()  # 또는 sysctl 호출
    ram_gb = psutil.virtual_memory().total / (1024**3)

    if ram_gb >= 32:
        return {"model": "whisper-large-v3", "batch_size": 24}
    elif ram_gb >= 16:
        return {"model": "whisper-medium-ko", "batch_size": 12}
    else:
        return {"model": "whisper-small", "batch_size": 8}
```

---

## 5. 우선순위 요약 (M4 16GB 최적화)

| 순위 | 항목 | 현재 → 목표 | 난이도 | 효과 |
|:---:|------|:----------:|:-----:|:----:|
| **1** | batch_size 실제 전달 | 미적용 → 적용 | ★☆☆ | STT +20% |
| **2** | pyannote MPS 재테스트 | CPU → MPS? | ★★★ | 화자분리 5~10x |
| **3** | whisper 양자화 | full → 4bit | ★★☆ | 메모리 -50% |
| **4** | 멀티트랙 녹음 | 모노 → 스테레오 | ★★☆ | 정확도 +5~10% |
| **5** | whisper-large-v3 | medium → large | ★★☆ | 정확도 +1~2% |
| **6** | 칩셋 자동 감지 | 고정 → 동적 | ★★☆ | UX 개선 |

---

## 6. 전략적 결론

### 현재 포지셔닝

```
MacWhisper    = "빠르고 정확한 전사"    (STT 특화 도구)
meeting-trans = "전사 + AI 인텔리전스"  (엔드투엔드 회의 분석 플랫폼)
```

### 목표 포지셔닝 (Phase A~E 완료 후)

```
meeting-trans = "MacWhisper급 전사 속도 + AI 인텔리전스"
```

**Phase A~C 완료 시 예상 점수 변화**:

| 영역 | 현재 | 개선 후 | 변화 |
|------|:----:|:------:|:----:|
| STT/오디오 | 6.04 | **7.5+** | +1.5 |
| AI/데이터 | 9.3 | 9.3 | 유지 |
| 아키텍처 | 7.3 | **8.0+** | +0.7 |
| UX/제품 | 7.8 | 7.8 | 유지 |
| **총 평균** | **7.61** | **8.15+** | **+0.54** |

→ STT/오디오 약점을 보완하면서 AI 인텔리전스 우위를 유지하는 **상위 호환** 전략.

---

## 부록: 위원회 구성

| 전문가 | 분석 영역 | 읽은 파일 수 | 핵심 지적 |
|--------|----------|:----------:|----------|
| STT/오디오 전문가 | STT, 화자분리, 녹음 | 8개 | "CPU 화자분리가 치명적" |
| AI/데이터 전문가 | LLM, 검색, RAG | 7개 | "MacWhisper에 AI 기능 전무" |
| 아키텍처 전문가 | 모델관리, 안정성, 확장성 | 7개 | "양자화 전략 차용 필요" |
| UX/제품 전문가 | UI, 워크플로우, 제품성 | 6개 | "확장 가능성이 핵심 차별화" |
| M4 최적화 조사관 | 코드 기반 기술 가능성 | 8개 | "batch_size 미전달 발견" |
