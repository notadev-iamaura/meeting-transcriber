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

## 핵심 발견

### ghost613 모델의 긴 오디오 한계

ghost613 모델은 **Zeroth-Korean 짧은 클립 데이터로만 학습**되어,
30초 윈도우 내에서 짧은 발화 후 즉시 종료 토큰을 생성한다.
이는 MLX 변환 오류가 아니라 **모델 자체의 근본적 한계**임을
HF transformers 직접 테스트로 확인했다.

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
