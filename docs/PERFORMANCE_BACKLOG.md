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

**문제**
`steps/transcriber.py`, `merger.py`, `embedder.py` 등 7 개 지점이 `json.dump(..., indent=2)` 로 pretty-print. 장시간 회의(844s, 수천 세그먼트)에서 MB 단위 파일이 생성되며 `fsync` + atomic replace 비용이 1~3 초.

**제안**
- 내부 체크포인트만 `indent=None` 으로 (디버깅용 pipeline_state.json 은 유지)
- 선택적으로 `orjson` 도입 — 표준 json 대비 2~5배 빠름, NFC 유니코드 안전

**예상 이득**: 장시간 회의 I/O 1~3초 절감, 디스크 50% 감소
**리스크**: 디버깅 가독성 약간 저하 / `orjson` 은 신규 의존성
**난이도**: S (20 분)

### 🟢 2-B. 임베딩 디바이스 CPU vs MPS A/B

**문제**
`embedding.device="mps"` (24 GB M4 에서 기본). 그런데 `multilingual-e5-small` 은 470MB·384차원 소형 모델이라 MPS 초기화 오버헤드가 실질 연산보다 클 가능성. 청크 수가 <50 개인 짧은 회의에서는 CPU 가 오히려 빠를 수 있다.

**제안**
- `embedder.py` 에 `total_chunks < 50` 감지 시 CPU 자동 전환
- 또는 A/B 벤치 후 기본값 재조정

**예상 이득**: 짧은 회의 1~3s → 0.5s, MPS 점유 감소로 LLM 단계 `_clear_gpu_cache` 부담 경감
**리스크**: 거의 없음 (폴백 경로 자명)
**난이도**: S (측정 + 조건부 토글 15 분)

### 🟡 2-C. correction batch_size 재튜닝 (1-A 와 조합)

**배경**
BENCHMARK §3 기준 `batch_size=5` 는 "원문 변형 최소" 관점의 보수 선택. 이제 1-A (prompt_cache) 로 프리필 비용이 거의 사라졌으니, **배치 수를 늘릴 이득이 변경된다** — 기존에는 큰 배치가 전체 시간을 줄였으나 이제는 cache 로 작은 배치도 충분히 빠름.

**제안**: batch=5 고정값 유지 vs 발화 수 기반 적응(`≤20발화: 5, >20: 10`) A/B
**리스크**: 원문 변형 증가 가능 — `difflib.SequenceMatcher` 유사도 재측정 필요
**난이도**: S + 검증 M (벤치 스크립트 필요)

---

## 3. 외부 조건 필요 (Blocked)

### ⏭ 3-A. pyannote 화자분리 MPS 전환

**문제**
`steps/diarizer.py` 의 `device="auto"` 가 MPS 를 시도하지만 pyannote 3.x 의 일부 Conv1D 연산이 MPS 폴백 → CPU 로 떨어지는 사례 빈번. 화자분리가 실행 시간의 상당 비중을 차지한다 (BENCHMARK §2 의 total-transcribe 차감으로 추정).

**검증 조건**
- `HUGGINGFACE_TOKEN` 환경변수 설정 필요 (게이트 모델)
- 사용자가 `pyannote/speaker-diarization-3.1`, `pyannote/segmentation-3.0` 에서 "Agree" 수행
- 이후 `pyannote-audio 4.x` + `PyTorch 2.4+` 조합에서 MPS 실성능 측정

**예상 이득 (에이전트 리서치 기반, 미실측)**: 2~3배 가속, 전체 RTF -30%
**리스크**: MPS 수치 오차로 화자 수 오판 가능 → 정확도 검증 필수
**난이도**: M + 검증 L (정확도 회귀 테스트 필요)

### ⏭ 3-B. Diarization Review UI (화자 rename/merge)

**배경**
`design_handoff_recap_rebrand/ui_kit_reference/DiarizationReview.jsx` 의 화자 정리 UI — 현재 PR #3 (`feat/recap-visual-overhaul`) 머지 시 Non-Goals 로 제외.

**선행 작업**
- `PATCH /api/meetings/{id}/speakers/{speakerId}` (rename)
- `POST /api/meetings/{id}/speakers/merge` (merge)
- 데이터 모델 확장 (speaker_labels)

**난이도**: L (백엔드 설계 + UI 구현)

---

## 4. 측정이 필요한 미정 항목

| 항목 | 스크립트 초안 | 목적 |
|------|---------------|------|
| pyannote CPU vs MPS | `scripts/benchmark_diarize_device.py` | 3-A 의 ROI 확정 |
| prompt_cache + batch 매트릭스 | `scripts/benchmark_correct_prompt_cache.py` | 2-C 최적 batch 찾기 |
| 임베딩 CPU vs MPS | `scripts/benchmark_embed_device.py` | 2-B 임계값 결정 |
| 서멀 쿨다운 실측 | `sudo powermetrics --samplers smc` | `thermal.batch_size=2, cooldown=180` 보수성 재확인 (M4 는 팬리스 MBA 보다 여유 있음) |
| 체크포인트 I/O 프로파일 | `py-spy record -o pipeline.svg` | 2-A 의 `save_checkpoint` 누적 시간 측정 |

---

## 5. 요약

| 상태 | 항목 | 이득 (실측 또는 추정) |
|:---:|------|----------------------:|
| ✅ | 1-A MLX prompt_cache | **-37.6%** (실측) |
| 🟢 | 2-A 체크포인트 `indent=None` | -1~3s (추정) |
| 🟢 | 2-B 임베딩 CPU 자동 전환 | -1~3s (추정) |
| 🟡 | 2-C batch 재튜닝 | -10% correct (조합 시, 추정) |
| ⏭ | 3-A pyannote MPS | -30% 전체 RTF (미실측) |
| ⏭ | 3-B Diarization Review | UX 기능, 성능 영향 없음 |

**현 시점 종합**: 1-A 만 적용해도 Corrector 단계가 37.6% 단축 → 프로젝트 평균 RTF 0.45 → 약 0.30 수준. 나머지 개선들은 측정 후 반영 권장.
