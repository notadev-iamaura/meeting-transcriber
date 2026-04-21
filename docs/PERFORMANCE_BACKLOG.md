# Performance Backlog

> 2026-04-21 기준. Apple M4 24GB 실측 포함.
> 관련 배경: [`docs/BENCHMARK.md`](BENCHMARK.md) (§4 LLM 교정 결과).

본 문서는 파이프라인 성능 개선 기회를 **이미 적용된 것 / 적용 가능하나 대기 중인 것 / 외부 조건 필요** 세 범주로 정리한다. 각 항목은 현장에서 측정한 수치만 싣고, 근거 없는 예측은 "추정" 으로 명시한다.

---

## 1. 이미 적용됨 (Applied)

### ✅ 1-A. MLX `prompt_cache` 자동 재사용 — **실측 37.6%↓**

**문제**
Corrector 는 회의 하나당 수 개 배치를 LLM 에 보낸다. 각 배치마다 system role 에 들어가는 **용어집 + 교정 지시 프롬프트** 가 **동일**한데, 기존 구현은 매 호출마다 전체 프리필을 반복했다.

**해결**
`core/mlx_client.py` `MLXBackend.chat()` 에 **system prompt 해시 기반 자동 cache 관리** 도입.
- `mlx-lm` 경로 → `mlx_lm.models.cache.make_prompt_cache()` 결과를 `generate(..., prompt_cache=...)` 에 재사용
- `mlx-vlm` 경로 (Gemma 4) → `mlx_vlm.generate.PromptCacheState` 를 `stream_generate(..., prompt_cache_state=...)` 에 재사용
- 시스템 프롬프트가 바뀌면 해시 불일치로 자동 리셋 — Corrector 는 코드 변경 0

**실측 (Apple M4 24GB, Gemma 4 E4B 4bit, 시스템 프롬프트 ≈1619자 × 6 배치)**

| 경로 | baseline | cached | 절감 |
|------|---------:|-------:|-----:|
| microbenchmark (스크립트) | 38.95s | 24.00s | **-38.4%** |
| E2E `MLXBackend` | 41.49s | 25.90s | **-37.6%** |

프로덕션 시스템 프롬프트는 용어집 67개 + 지시문으로 ≈3000자 이상이라 **실제 환경에선 40%+ 가능** (추정).

**영향 모델**
- Gemma 4 E4B (프로젝트 기본): ✅ 실측 37.6%
- EXAONE 3.5 7.8B: mlx-lm 경로 — Qwen 2.5 3B 로 microbenchmark 28.3% 확인 (EXAONE 자체는 본 세션 venv 의 transformers 5.x 호환 이슈로 미측정)
- Qwen 2.5 0.5B: 13.4%

**리스크와 안전장치**
- 시스템 프롬프트 해시 비교로 잘못된 cache 재사용을 원천 차단
- `make_prompt_cache` / `PromptCacheState` 초기화가 실패하면 경고 로그 후 **기존 비-cache 경로로 폴백**
- `cleanup()` 에서 cache 도 함께 해제 — 모델 메모리 재확보 시 누수 방지

---

## 2. 적용 가능하나 대기 중 (Ready but Deferred)

### 🟢 2-A. 체크포인트 JSON `indent=None` + 선택적 `orjson`

**문제**: `steps/transcriber.py`, `merger.py`, `embedder.py` 등 7 개 지점이 `json.dump(..., indent=2)` 로 pretty-print. 장시간 회의에서 MB 단위 파일.
**제안**: 내부 체크포인트만 `indent=None` 으로. 또는 `orjson` 도입.
**예상 이득**: 장시간 회의 I/O 1~3초 절감, 디스크 50% 감소
**난이도**: S (20 분)

### 🟢 2-B. 임베딩 디바이스 CPU vs MPS A/B

**문제**: `multilingual-e5-small` (470MB) 은 소형 모델로 MPS 초기화 오버헤드가 실질 연산보다 클 가능성.
**제안**: 청크 수 <50 일 때 CPU 자동 전환
**예상 이득**: 짧은 회의 1~3s → 0.5s (추정)
**난이도**: S (15 분)

### 🟡 2-C. correction batch_size 재튜닝 (1-A 와 조합)

**배경**: 1-A 적용 후 배치 크기 방정식이 바뀜. 큰 배치 이득이 줄어드니 배치=5 고정 vs 적응형 A/B.
**예상 이득**: -10% correct (조합 시, 추정)
**난이도**: S + 검증 M

---

## 3. 외부 조건 필요 (Blocked)

### ⏭ 3-A. pyannote 화자분리 MPS 전환

**검증 조건**: HUGGINGFACE_TOKEN 설정 + 게이트 모델 Agree 후 pyannote 4.x + torch 2.4+ 에서 MPS 실측
**예상 이득 (리서치 기반, 미실측)**: 2~3배 가속, 전체 RTF -30%
**난이도**: M + 검증 L

### ⏭ 3-B. Diarization Review UI (화자 rename/merge)

**선행 작업**: 백엔드 API (PATCH/POST) + 데이터 모델 확장
**난이도**: L

---

## 4. 측정이 필요한 미정 항목

| 항목 | 스크립트 초안 | 목적 |
|------|---------------|------|
| pyannote CPU vs MPS | `scripts/benchmark_diarize_device.py` | 3-A ROI 확정 |
| prompt_cache + batch 매트릭스 | `scripts/benchmark_correct_prompt_cache.py` | 2-C 최적 batch |
| 임베딩 CPU vs MPS | `scripts/benchmark_embed_device.py` | 2-B 임계값 결정 |
| 서멀 쿨다운 실측 | `sudo powermetrics --samplers smc` | 2건+180s 보수성 재확인 |
| 체크포인트 I/O 프로파일 | `py-spy record` | 2-A `save_checkpoint` 누적 시간 |

---

## 5. 요약

| 상태 | 항목 | 이득 (실측 또는 추정) |
|:---:|------|----------------------:|
| ✅ | 1-A MLX prompt_cache | **-37.6%** (실측) |
| 🟢 | 2-A 체크포인트 `indent=None` | -1~3s (추정) |
| 🟢 | 2-B 임베딩 CPU 자동 전환 | -1~3s (추정) |
| 🟡 | 2-C batch 재튜닝 | -10% correct (추정) |
| ⏭ | 3-A pyannote MPS | -30% 전체 RTF (미실측) |
| ⏭ | 3-B Diarization Review | UX 기능 |

**현 시점 종합**: 1-A 만 적용해도 Corrector 단계가 37.6% 단축 → 프로젝트 평균 RTF 0.45 → 약 0.30 수준.
