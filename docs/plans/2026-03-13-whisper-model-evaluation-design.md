# Whisper 한국어 모델 평가 및 MLX 변환 설계

## 배경

MacBook Air M4 16GB에서 동작하는 한국어 회의 전사 시스템의 STT 모델 선정을 위해
여러 Whisper 모델을 벤치마크하고, HuggingFace PyTorch 모델을 MLX 포맷으로 변환하여 평가했다.

## 평가 대상 모델

| 모델 | 크기 | 학습 데이터 | 포맷 |
|------|------|-----------|------|
| `youngouk/whisper-medium-komixv2-mlx` | 1.5GB | AI-Hub 회의/전화/방송 | MLX |
| `mlx-community/whisper-large-v3-turbo` | 1.5GB | 다국어 범용 | MLX |
| `ghost613/whisper-large-v3-turbo-korean` | 1.5GB | Zeroth-Korean | PyTorch → MLX 변환 |

## 변환 파이프라인 (PyTorch HF → MLX)

### 키 리매핑 규칙

- `model.{encoder|decoder}.layers.N.` → `{encoder|decoder}.blocks.N.`
- `.self_attn.{q,k,v}_proj` → `.attn.{query,key,value}`
- `.encoder_attn.` → `.cross_attn.`
- `.fc1/.fc2` → `.mlp1/.mlp2`
- `.self_attn_layer_norm` → `.attn_ln`
- `.final_layer_norm` → `.mlp_ln`
- Conv1d 가중치: `(out, in, kernel)` → `(out, kernel, in)` 축 전치

### 특수 처리

- `alignment_heads`: HF 모델에 없음 → 레퍼런스 MLX 모델에서 복사
- `encoder.embed_positions`: HF에만 존재 → MLX에서는 계산하므로 무시
- config.json: HF `d_model` → MLX `n_audio_state` 등 필드명 변환

### 변환 스크립트

`scripts/convert_hf_to_mlx_whisper.py` — 범용 HF Whisper → MLX 변환 도구.

## 벤치마크 결과

### Zeroth-Korean 테스트셋 (20샘플, 짧은 클립)

| 모델 | CER | WER | 속도 |
|------|-----|-----|------|
| turbo-korean-mlx (변환) | 1.29% | 3.13% | 15.9초 |
| komixv2-mlx | 14.24% | 35.64% | 18.7초 |
| large-v3-turbo (범용) | 15.60% | 37.69% | 17.3초 |

### 실제 회의 녹음 (92초)

| 모델 | 전사 길이 | 품질 |
|------|----------|------|
| komixv2-mlx | 246자 | 정상 (대부분 정확) |
| large-v3-turbo (범용) | 801자 | 후반부 환각 ("감사합니다" 반복) |
| turbo-korean-mlx (변환) | 28자 | 첫 문장만 전사 후 조기 종료 |

## 핵심 발견: 조기 종료 근본 원인 분석

### 검증 방법론

3가지 실행 모드로 동일한 92초 오디오를 동일한 ghost613 가중치로 처리하여 비교:
1. MLX 변환 + 타임스탬프 모드 (mlx-whisper 기본)
2. MLX 변환 + without_timestamps 모드
3. HF transformers 원본 (notimestamps 기본)

### 30초 윈도우별 토큰 생성 비교

| 윈도우 | MLX timestamps | MLX no-timestamps | HF transformers |
|--------|---------------|-------------------|-----------------|
| 0-30s  | 15 토큰       | **39 토큰**       | **39 토큰**     |
| 30-60s | **0 토큰**    | 13 토큰           | 21 토큰         |
| 60-90s | **0 토큰**    | 12 토큰           | 13 토큰         |
| 90-91.9s | **0 토큰** | 8 토큰            | 60 토큰 (환각)  |

참고: komixv2-mlx는 첫 윈도우에서 **105 토큰** 생성.

### 근본 원인: 2가지 요인의 결합

**요인 1 — 모델 학습 데이터 편향 (기본 원인)**:
ghost613은 Zeroth-Korean **짧은 클립(3~20초)으로만 학습**되어,
without_timestamps 모드에서도 30초 윈도우에 39토큰만 생성한다 (komixv2는 105토큰).
윈도우가 진행될수록 토큰 수가 급감한다 (39 → 21 → 13).

**요인 2 — 타임스탬프 모드에서의 치명적 악화 (직접 원인)**:
짧은 클립 학습으로 모델이 `<|0.00|> text <|3.50|>` 같은 짧은 타임스탬프 구간 패턴을 학습.
타임스탬프 모드에서 첫 몇 초만 전사 후 **즉시 EOT 생성**.
후속 윈도우에서는 condition_on_previous_text로 이전 짧은 텍스트가 prompt → **바로 EOT** (0토큰).

### 배제된 가설

| 가설 | 검증 결과 |
|------|----------|
| MLX 변환 오류 | **배제** — without_timestamps에서 HF 원본과 동일 결과 |
| 토크나이저 vocab 불일치 | **배제** — ghost613 vocab=51866 = 표준 multilingual |
| no_speech_prob 스킵 | **배제** — no_speech_prob ≈ 0.0 (전 윈도우) |
| logprob 임계값 초과 | **배제** — avg_logprob > -1.0 (임계값 통과) |

### 결론

ghost613 모델의 긴 오디오 조기 종료는 **MLX 변환의 문제가 아니라**,
**짧은 클립 학습 데이터 + Whisper 타임스탬프 디코딩의 상호작용**이 근본 원인이다.
HuggingFace의 모든 한국어 large-v3-turbo 모델이 Zeroth-Korean 기반이므로
동일한 문제를 공유할 가능성이 높다.

### komixv2가 회의 전사에 최적인 이유

`seastar105/whisper-medium-komixv2`는 AI-Hub 다중 도메인
(회의, 전화, 방송) 데이터로 학습되어:
- 긴 오디오 세그먼트 전사 능력 유지
- 한국어 회의 도메인에 특화된 어휘
- medium 크기로 16GB MacBook Air에서 안정적

## 결론

| 기준 | 최적 모델 |
|------|---------|
| 짧은 클립 정확도 | ghost613 turbo-korean (CER 1.29%) |
| **실제 회의 전사** | **komixv2-mlx (CER 14.24%, 안정적)** |
| 속도 | large-v3-turbo (15.9초) |

**최종 선정**: `youngouk/whisper-medium-komixv2-mlx` — 현재 config.yaml 설정 유지.

## 향후 개선 방향

1. AI-Hub 회의 데이터로 large-v3-turbo 파인튜닝 (GPU 서버 필요)
2. Whisper large-v3-turbo + LoRA 경량 파인튜닝 (MLX 프레임워크 활용)
3. 숫자 정규화 후처리 활성 유지 (number_normalization.enabled: true)
