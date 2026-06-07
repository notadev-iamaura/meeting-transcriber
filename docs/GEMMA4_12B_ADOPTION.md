# Gemma 4 12B 도입 검토 및 계획

> 작성: 2026-06-05 (1차 메모 통합·검증 보강)
> 작성자: AI 에이전트 (딥리서치 표적 웹검증 + 코드베이스 분석)
> 결론 상태: **최종(2026-06-07): E4B 단독으로 충분(wiki 포함) — 12B 불채택. §0.6 참조**
> 대상 환경: Apple Silicon MacBook Air, 16GB 통합 메모리, 100% 로컬, MLX/Ollama 백엔드
> 관련: [`docs/BENCHMARK.md`](BENCHMARK.md) §4(LLM 비교), [`CLAUDE.md`](../CLAUDE.md) "LLM 모델 선택 가이드"

이 문서는 2026-06-03 공개된 Google **Gemma 4 12B** 를 Meeting Transcriber 의 로컬 LLM
백엔드에 적용할지 판단하기 위한 근거와 실행 계획을 정리한다.

---

## 0. 두괄식 결론 (TL;DR)

> ⭐ **최종 결론(2026-06-07, §0.6 참조)**: 다중 TC 실측 결과 **wiki·요약에서도 12B 가 E4B 보다 낫지 않아
> 12B 불채택**. E4B(개선 프롬프트)로 충분하며 최소 구성 **~9GB**. 아래 §0~§0.5 는 12B 검토 경위(이력)다.

**Gemma 4 12B 는 "기본 모델 교체" 대상이 아니라, 검증 게이트를 통과해야 켜지는 "옵트인 고품질
실험 백엔드" 로만 조건부 도입을 권장한다.** 16GB 에서의 1순위 실측 경로는 **MLX 가 아니라
Ollama(`gemma4:12b`, 7.6GB)** 다.

근거 4줄 요약:

1. **메모리 🔴**: 우리 기본 경로인 MLX in-process 4bit(`mlx-community/gemma-4-12B-it-4bit`)는
   **≈11GB** 로, 파이프라인 가드 `pipeline.peak_ram_limit_gb: 9.5`([`config.yaml`](../config.yaml) L105)를
   **초과**한다. 반면 Ollama/GGUF 4bit 는 **7.6GB** 로 16GB 에서 현실적이다.
2. **새 파서 리스크 🔴**: Gemma 4 12B 는 **thinking 모드**를 가지며, **thinking 을 꺼도 `<|channel>thought`
   태그를 출력**한다(E2B/E4B 는 예외). 현재 기본 E4B 에는 없던 문제로, Corrector/Summarizer/Wiki
   파서가 깨질 수 있어 **백엔드 경계에서 thought-block 제거가 선행되어야 한다.**
3. **품질 이득은 태스크 의존**: **교정**은 이미 E4B 가 정답지 유사도 92.9% 로 충분(§2). 12B 의
   강점은 추론·지식이 필요한 **요약**에서 더 의미 있을 수 있으나, **한국어 회의 데이터 실측 우위는
   아직 미검증**이다.
4. **신선도**: 출시 2일차(2026-06-03)라 한국어 품질·환각·고유명사 처리에 대한 독립 검증이 사실상 없다.

**권장 경로: 옵션 B(§5)** — `config.yaml`/환경변수로만 켜지는 실험 백엔드로 추가하고, UI 화이트리스트와
기본값은 유지한다. **§6 검증 게이트(특히 G0 thought-block, G1 메모리, G4 고유명사)** 통과 후에만
UI 노출(옵션 C)·기본 교체(옵션 D)를 재검토한다.

> **2026-06-06 실측 업데이트**: 실제 측정 결과(§0.5) — (a) **버전 충돌은 해결됨**: mlx-vlm **0.6.1**
> 이 E4B 와 12B 를 **둘 다** 로드·생성한다(0.6.2 가 E4B 를 깨뜨린 건 0.6.2 고유 회귀). 즉 **mlx-vlm
> 을 0.6.1 로 핀하면 MLX 에서 E4B 유지 + 12B 옵트인이 가능**하다. 다만 (b) 12B 는 **E4B 대비 ~4배
> 느리고**(5.9 vs 22.7 tok/s), (c) **MLX peak 11.25GB**(메모리 여유 적을 때 스왑 유발). → 기본 교체는
> 여전히 비권장, **옵트인(0.6.1 핀)** 은 24GB+ 에서 현실적.

---

## 0.5. 실측 결과 (2026-06-06, Apple M4 / 24GB / macOS 26.5.1)

> 측정 하네스: MLX 측정 하네스(검증용 1회성, 저장소 미수록) (MLX 메모리 카운터
> `mx.get_peak_memory()` 사용 — psutil RSS 는 MLX 통합메모리를 과소측정하므로 신뢰 불가).
> 결과 원본: (측정 결과 원본, 미수록). 입력: §측정 동일 한국어 회의 전사문 + 실제
> summarizer 시스템 프롬프트. **주의: 이 머신은 16GB 가 아니라 24GB 다**(16GB 결론은 더 보수적으로 적용).
> E4B 는 mlx-vlm 0.5.0, 12B 는 0.6.2 에서 측정(버전 공유 불가 — §7).

| 지표 | E4B (현 기본) | **Gemma 4 12B** | 비고 |
|------|--------------|-----------------|------|
| 디스크 크기 | 4.89 GB | **10.26 GB** | 2.1배 |
| 로드 시간 | 4.2 s | 8.5 s | — |
| MLX active(로드후) | 4.86 GB | 10.24 GB | 디스크와 일치 |
| **MLX peak(생성중) ★G1** | **5.76 GB** | **11.25 GB** | 모델 실측 footprint |
| 시스템 used 증가 | +2.7 GB | +7.7 GB → 피크 17.3 GB | 세션 점유 포함 |
| 최소 가용 메모리 | 3.59 GB | **1.85 GB** | 24GB서도 임박 |
| **새 스왑 발생 ★G1** | **0 GB** | **+4.13 GB** | 24GB서도 압박 |
| 요약 속도 [G2] | 23.8 tok/s | **5.4 tok/s** | **4.4배 느림** |
| 평균 속도 [G2] | ~26.5 tok/s | **6.1 tok/s** | ~4.3배 느림 |
| 요약 소요(393~365 tok) | 20 s | **88 s** | 1건 기준 |
| thinking 태그 [G0] | 없음 | **없음** | 기본 템플릿 경로 |
| 고유명사 병기 [G4] | 0 건 | **1 건** | "옵트인(Opt-in)" 병기 |
| 요약 품질 [G3] (주관) | 양호 | **우수** | 구조·정확도·액션아이템 추출 우수 |

**실측 해석:**

1. **[G1 메모리] 24GB 에서 로드·생성 완주(크래시 없음)** 했으나, MLX peak **11.25GB** + 세션 점유로
   **+4.13GB 새 스왑**이 발생하고 가용이 1.85GB 까지 떨어졌다. → **16GB 환경에서는 사용 불가**가
   사실상 확정. 24GB 이상 + 다른 대형 앱 종료 시에만 현실적.
2. **[G2 속도] 6.1 tok/s 평균은 도입 문서 추정(2~3배)보다 나쁜 ~4.3배 저하.** 요약 1건 88초.
   교정은 발화 배치 × 수십 회라 회의당 LLM 시간이 크게 늘고, 팬리스 Air 서멀 쿨다운과 겹친다.
3. **[G3 요약] 12B 요약 품질은 명확히 우수**(안건/결정/액션아이템 구조화 정확). **요약 태스크에서 12B
   이득 가설은 실측으로 지지됨** — 비용을 감수할 가치가 있는 유일한 영역.
4. **[G0 thinking] 현재 호출 경로(mlx-vlm 기본 템플릿)에서는 채널 태그 미출현.** 즉각적 파서 붕괴
   위험은 낮음. 단 모델 자체는 thinking capable 이므로 방어 코드는 여전히 권장(§3.2).
5. **[G4 고유명사] 12B 가 병기 1건 생성(E4B 0건).** 표본은 작지만 Gemma 병기 경향이 12B 에서도
   나타남 확인 → 회의록 고유명사 정확도 관점에서 이득이 아니라 소폭 위험.
6. **[버전 호환 — 해결됨] mlx-vlm 0.6.1 이 E4B 와 12B 를 모두 지원한다.** 버전별 로드/생성 실측
   (버전 호환 확인 스크립트(1회성, 미수록)):

   | mlx-vlm | E4B | 12B | 비고 |
   |---------|-----|-----|------|
   | 0.5.0 | ✅ | ❌ | `gemma4_unified not supported` |
   | 0.6.0 | ✅ | ❌ | 동일(12B 미지원) |
   | **0.6.1** | **✅** | **✅** | **둘 다 로드·생성 정상 — 권장 핀** |
   | 0.6.2 | ❌ | ✅ | E4B `Received 126 parameters not in model`(QK-norm 회귀 추정) |

   - **mlx-lm(0.31.3) 로도 12B 로드 불가**(`gemma4_unified` 미지원) → 텍스트 전용 우회 경로 없음.
   - ⇒ **버전 충돌은 0.6.2 고유 회귀**이며, **mlx-vlm 을 0.6.1 에 핀하면 MLX 에서 E4B(기본)를 유지하면서
     12B 를 옵트인으로 추가할 수 있다.** (측정 환경은 0.6.1 로 유지함.)

### 0.5.1 Ollama gemma4:12b 실측 + 3자 비교 (2026-06-06, M4/24GB)

> 측정: Ollama 측정 하네스(1회성, 미수록) (Ollama `/api/chat` + `ollama ps`).
> 결과 원본: (측정 결과 원본, 미수록). 동일 프롬프트/입력.

| 지표 | E4B (MLX 0.6.1) | **MLX 12B** (0.6.1) | **Ollama 12B** (GGUF q4_K_M) |
|------|-----------------|---------------------|------------------------------|
| 모델 메모리 | 5.76 GB (MLX peak) | **11.25 GB** (MLX peak) | **8.0 GB** (`ollama ps` SIZE) |
| 디스크 | 4.89 GB | 10.26 GB | 7.6 GB |
| 평균 속도 | 22.7 tok/s | 5.9 tok/s | **7.4 tok/s** |
| 요약 속도 | 23.8 | 5.4 | 7.2 tok/s |
| 새 스왑 | 0 | +0.4~4.1 GB(상황의존) | ~0 |
| thinking 처리 | — | 기본경로 미출현(태그제거 권장) | **`think:false` 필수**(안 하면 빈 출력) |
| 고유명사 병기 | 0 | 1 | 1 |
| 요약 품질(주관) | 양호 | 우수 | 우수 |
| 별도 서버 | 불필요(in-process) | 불필요(in-process) | **필요(공식 .app)** |
| 버전 제약 | — | mlx-vlm **0.6.1** 핀 | Ollama 최신 **공식 .app** |

**실측 해석 (Ollama vs MLX):**

1. **Ollama 가 RAM 을 덜 쓴다 — 8.0GB vs MLX 11.25GB**(약 3GB 절감). GGUF(7.6GB) < MLX(11GB) 차이가
   별도 프로세스 오버헤드를 압도. "~8GB" 사전 추정이 `ollama ps` SIZE 8.0GB 로 정확히 일치.
   (RSS 로는 ollama 서버 0.18GB 만 잡힘 — MLX 처럼 모델은 GPU/런너에 있어 서버 RSS 과소측정. `ollama ps` 가 정답.)
2. **Ollama 가 오히려 더 빠르다 — 7.4 vs 5.9 tok/s**(약 25%). MLX `gemma4_unified` 경로가 아직 최적화
   덜 된 것으로 보임(MTP/speculative 포함). 단발 측정이므로 경향성으로 해석.
3. **thinking 처리**: Ollama 는 **`think:false` 한 줄**로 깔끔(안 하면 응답이 빈다 — 15~60 토큰을 사고에 소진).
   MLX 는 채널 태그를 수동 제거해야 함. → **Ollama 가 thinking 제어에서 유리.**
4. **🔴 Ollama 설치 함정 (실측)**: Homebrew `ollama` 포뮬러(0.30.6)는 **llama-server 런너 누락**으로
   추론 불가(`llama-server binary not found ... Run 'cmake ...' first`). 또 구버전 .app(0.11.8)은 gemma4
   미지원(`unable to load model`). ⇒ **반드시 ollama.com 의 최신 공식 .app** 를 써야 한다(프로젝트
   [`CLAUDE.md`](../CLAUDE.md) "Ollama 앱 설치 … brew 불가" 와 일치). gemma4 는 thinking 기본 ON 이라
   `/api/chat` 에 `"think": false` 를 넣는 것도 백엔드 연동 시 필수.

**종합 권장 (실측 기반):**

| 상황 | 권장 12B 경로 |
|------|---------------|
| **16GB Mac** + 12B 필요 | **Ollama**(8GB·더 빠름·`think:false`). MLX 11GB 는 16GB 불가 |
| **24GB+** , in-process 선호, 서버 관리 회피 | **MLX 12B (mlx-vlm 0.6.1 핀)** |
| 공통 | **기본은 E4B 유지, 12B 는 옵트인.** 이득은 요약 태스크에 한정 |

### 0.5.2 압축 최적화 실측 — QAT & Dynamic Quant (2026-06-06)

> 질문: 12B 의 메모리(MLX 11GB / GGUF 8GB)를 **더 줄일 수 있는가?** → **가능.** Google QAT(2026-06-05
> 출시) + Unsloth Dynamic GGUF 를 Ollama 로 실측. `ollama ps` SIZE = 실제 적재 RAM.

| 양자화 | 디스크 | RAM | 속도 | 요약 품질 | 비고 |
|--------|--------|-----|------|-----------|------|
| q4_K_M (기준) | 7.6GB | 8.0GB | 7.4 tok/s | 우수 | 일반 PTQ 4bit |
| **QAT Q4_0** (`gemma4:12b-it-qat`) | 7.2GB | 7.6GB | 6.7 tok/s | 우수 | **Google QAT — FP16 근접** |
| UD-Q3_K_XL | 6.2GB | 6.7GB | 4.8 tok/s | 양호 | Unsloth 동적 3bit |
| **UD-IQ2_M** | 4.4GB | **4.9GB** | 6.2 tok/s | 양호(미세 깨짐) | Unsloth 동적 2bit — **E4B(5.76GB)보다 작은 12B** |
| (참고) MLX 12B | 10.26GB | 11.25GB | 5.9 tok/s | 우수 | sub-4bit MLX 미존재 |
| (참고) E4B | 4.89GB | 5.76GB | 22.7 tok/s | 양호 | 현 기본 |

**핵심:**
1. **메모리 문제는 압축으로 해결 가능**: QAT 7.6GB → UD-Q3 6.7GB → **UD-IQ2 4.9GB**(E4B 보다 작음).
   IQ2 는 8GB Mac 에서도 적재 가능 — 16GB 제약이 사라진다.
2. **QAT 가 품질-효율 최적**: Google QAT Q4_0 는 PTQ 대비 perplexity 저하 **−54%**, FP16 수 % 이내.
   q4_K_M 와 비슷한 크기(7.6GB)에 품질↑ → **12B 를 쓴다면 QAT 가 1순위.**
3. **압축은 속도를 못 살린다**: 모든 12B 변종이 ~5~7 tok/s(E4B 22.7 의 ~1/4). 저비트는 **메모리만**
   줄이고 dequant 오버헤드로 오히려 느려질 수 있다(UD-Q3 4.8). 12B 의 느림은 압축으로 해결 불가.
4. **2bit 품질 한계**: UD-IQ2_M 은 회의록 구조는 유지하나 미세 글자 깨짐("GGU_F", "11G") 발생 →
   인명·고유명사 정확도가 핵심인 회의록엔 위험. **UD-Q3(6.7GB)~QAT(7.6GB) 가 안전대.**
5. **MLX 는 압축 여력 없음**: sub-4bit Gemma 4 12B MLX 양자화 미존재 → 압축 최적화는 **GGUF/Ollama 전용.**

**압축 최종 권장:**

| 목표 | 권장 양자화 | RAM |
|------|------------|-----|
| **품질 우선 12B** | **QAT Q4_0** (`gemma4:12b-it-qat`) | 7.6GB |
| **16GB 메모리 빠듯** | UD-Q3_K_XL | 6.7GB |
| **8GB Mac / 극단 압축** | UD-IQ2_M (미세 품질손실 감수) | 4.9GB |

---

## 0.6. 최종 결론 — E4B 단독으로 충분 (wiki 포함) (2026-06-07)

12B 도입 검토는 결국 "wiki/요약에서 12B가 E4B보다 나은가"로 좁혀졌고, 다중 TC 실측으로 **"아니오"** 가 나왔다.

### Wiki 결정사항 추출 — E4B vs 12B (실측)

> 평가: wiki 추출 평가 하네스(1회성, 미수록), 실제 추출 프롬프트
> ([`core/wiki/extractors/decision.py`](../core/wiki/extractors/decision.py)), 어려운 TC(다중·없음·번복·잡음·병기·액션).

- **1차 6 TC**: E4B 5/6 ≈ 12B 5/6 (**동점**). 인용 환각은 양쪽 0. 앞서 보고된 "12B 병기 우세"는
  **측정 오류**(인용 마커 `[meeting:...]`를 한글 뒤에서 병기로 오탐)였고, **실제 영어 병기는 양쪽 0건**.
- **프롬프트 개선 후 10 TC**: `decision.py` 에 규칙 7·8·9(연기·과분할 제외, follow_up 묶기) 추가 →
  E4B **7/10 → 9/10**(3회 결정적). 고친 것: 연기 과추출(TC2)·과분할(TC6). 남은 1건(TC10 "결정 안 하고
  자료 모아 재논의")은 **정의적 회색지대** → 프롬프트로 과교정하면 진짜 보류결정을 놓치므로, wiki
  다운스트림 가드(confidence 임계·lint)에 위임.

### 결론

- **교정·요약·wiki 전부 E4B(개선 프롬프트)로 충분.** 12B 의 유일한 기대 이득(wiki/요약 품질)이 실측에서
  E4B 와 동급 → **12B 불필요.** "교정=E4B / wiki=12B 분업"도 불필요(E4B 가 wiki 도 동급).

### 16GB 최소 구성 (SSD)

| 모델 | 용도 | 크기 |
|------|------|------|
| whisper-large-v3-turbo | STT | 1.5GB |
| pyannote 3.1 + seg-3.0 | 화자분리 | ~0.2GB |
| multilingual-e5-small | 임베딩(검색) | 0.47GB |
| gemma-4-E4B 4bit | LLM(교정·요약·wiki) | 4.9GB |
| **모델 소계** | | **~7.1GB** |
| + 의존성(.venv) | torch·mlx 등 | ~2.0GB |
| **= 최소 시스템** | | **≈ 9GB** |

> 12B(6.3GB)는 선택(불채택). reranker(bge-reranker, 2.1GB)는 코드 미참조 → 불필요.

### (참고) 나중에 12B 가 필요해지면 — llama-cpp-python

"16GB + 서버없음 + 12B" 셋을 동시에 푸는 유일한 길은 **llama-cpp-python**(GGUF in-process). 실측
(llama-cpp 측정 하네스(1회성, 미수록)): QAT GGUF 디스크 6.26GB /
**RSS 7.63GB < 예산 9.5GB** / 7 tok/s / **wiki JSON 정상**(인용·엔티티 정확). MLX 12B(11GB)는 16GB 불가
(sub-4bit MLX 12B 미존재). 단 위 결론상 **현재는 불필요**.

---

## 1. 모델 사실 정리 (검증된 범위)

> 명칭 확인: 본 프로젝트의 "Gemma 4 E4B/E2B" 표기는 **오칭이 아니다.** 2026-04-02 Google 이 실제로
> **Gemma 4** 패밀리(E2B / E4B / 26B MoE / 31B Dense)를 출시했고, 2026-06-03 **Gemma 4 12B** 가
> 추가되었다. (구세대 Gemma 3 는 별개: 1B/4B/12B/27B Dense + Gemma 3n E2B/E4B.) 본 문서 대상은 **신규 Gemma 4 12B**.

| 항목 | 값 | 출처 신뢰도 |
|------|----|-----------|
| 정식 명칭 | **Gemma 4 12B** (`google/gemma-4-12B-it`) | 1차(Google 블로그/HF) |
| 출시일 | **2026-06-03** | 1차(Google) |
| 라이선스 | **Apache 2.0** (상업·로컬 제약 사실상 없음) | 2차(Google Cloud 블로그) |
| 모달리티 | **텍스트+이미지+오디오** 입력 → 텍스트 출력. "encoder-free" 통합 멀티모달(오디오 raw waveform 을 텍스트 토큰과 동일 임베딩 공간으로 투영). 비디오는 프레임 단위 | 1차 |
| 컨텍스트 | 런타임별 128K~256K (우리 사용량 6,144 토큰 대비 충분, 단 KV 캐시 메모리 주의) | 1차/2차 |
| **Thinking 모드** | **있음.** `<|think|>` 토큰/`enable_thinking=True` 로 제어. 끄더라도 12B 는 `<|channel>thought\n<channel|>` 빈 태그를 출력(E2B/E4B 제외) | 1차(Google AI 문서) |
| 품질(2차) | MMLU-Pro **77.2%** 보고(참고용, 한국어/회의 태스크와 직접 상관 아님) | 2차 |
| 16GB 구동 | 벤더 "16GB VRAM/통합 메모리 노트북 구동" 표방(타 대형 앱 없는 이상 상태 기준) | 1차/2차 |

> HF MLX 카드의 "3B params" 표기는 신규 업로드 메타데이터 오류로 보인다. 모델명·공식 가이드 모두 12B 로 일치.

---

## 2. 현재 시스템 컨텍스트 (변경 영향 기준선)

| 항목 | 현재 값 | 위치 |
|------|---------|------|
| 기본 LLM(MLX) | `mlx-community/gemma-4-e4b-it-4bit` (≈6GB, 피크 RAM ≈5GB) | [`config.yaml`](../config.yaml) L40 |
| 대안 LLM | `mlx-community/EXAONE-3.5-7.8B-Instruct-4bit` (한국어 특화) | [`config.py`](../config.py) L339 |
| 파이프라인 RAM 가드 | `peak_ram_limit_gb: 9.5` | [`config.yaml`](../config.yaml) L105 |
| LLM 권장 가용 메모리 | **6.5GB** (= 4bit 피크 5GB + 마진 1.5GB) | [`BENCHMARK.md`](BENCHMARK.md) §요약 |
| 모델 적재 규칙 | **한 번에 대형 모델 1개만** 적재(STT→화자분리→LLM 순차, 사이 언로드) | [`CLAUDE.md`](../CLAUDE.md) 아키텍처 핵심 규칙 |
| LLM 태스크 | (a) 전사 교정 (b) 요약 (c) RAG 채팅 (d) Wiki 추출 | `steps/corrector.py`, `steps/summarizer.py`, `search/chat.py`, `core/wiki/*` |
| 토큰 상한 | 교정 800 / 요약 1200 / 채팅 1000, 컨텍스트 6144 | [`config.yaml`](../config.yaml) L42–45 |
| 서멀 정책 | 2건 처리 후 180초 쿨다운(팬리스) | [`config.yaml`](../config.yaml) L120–124 |
| MLX 백엔드 분기 | 모델명에 `gemma-4`/`gemma4` 포함 시 **mlx-vlm**, 아니면 mlx-lm | [`core/mlx_client.py`](../core/mlx_client.py) L99 |

`mlx-community/gemma-4-12B-it-4bit` 는 `"gemma-4"` 를 포함 → **자동으로 mlx-vlm 경로로 올바르게
라우팅**된다(분기 수정 불필요). 단 mlx-vlm 변환 기준이 **0.6.0** 이므로 설치본 버전 확인 필요(§4).

**기존 LLM 벤치마크** ([`BENCHMARK.md`](BENCHMARK.md) §4, 44 발화, 정답지=Claude 수동 교정):

| 모델 | 평균 교정 유사도 | 속도 | 한국어 고유명사 |
|------|-----------------|------|----------------|
| Gemma 4 E4B 4bit (현 기본) | **92.9%** (44발화 중 41승) | E4B 기준 1x | 영어 병기 경향(프롬프트로 억제) |
| EXAONE 3.5 7.8B 4bit | 47.5% (3승) | 더 빠름 | 정확 |

> 이 결과는 **교정 태스크 한정**이다(한국어 QA/추론은 EXAONE 우위 공개 벤치마크 존재 — [`BENCHMARK.md`](BENCHMARK.md) §한계 5).
> 12B 판단도 동일하게 **태스크별**로 본다.

---

## 3. 적합성 분석

### 3.1 메모리 (가장 큰 제약) 🔴

런타임별 공개 크기(작은 순):

| 경로 | 모델 | 공개 크기/요구량 | 컨텍스트 | 판단 |
|---|---|---:|---|---|
| Ollama | `gemma4:12b` (GGUF Q4) | **7.6GB** | 256K | **16GB 텍스트 실험 1순위** |
| Unsloth GGUF | `UD-Q4_K_XL` | 7.12GB | — | llama.cpp/Ollama 후보 |
| Unsloth GGUF | `Q4_K_M` | 7.4GB | — | Ollama 후보 |
| Ollama MLX | `gemma4:12b-mlx` | 10.0GB | 128K | 16GB 빠듯 |
| MLX HF | `mlx-community/gemma-4-12B-mxfp4` | 10.9GB | — | MLX 실험 후보 |
| MLX HF | `mlx-community/gemma-4-12B-it-4bit` | 11GB | — | MLX 실험 후보(현 분기 기본 경로) |
| (8bit) | — | ≈14GB | — | 16GB 비현실적 |

- **핵심**: 우리 기본 경로(MLX in-process)의 12B 4bit 는 **10–11GB** 로, LLM 권장 여유 6.5GB·가드 9.5GB 를
  크게 넘는다. 모델 적재 규칙상 LLM 단계에서 STT/화자분리는 언로드되지만, 11GB 모델 + KV 캐시 +
  활성값 + Metal 오버헤드 → **피크 ≈12–13GB 추정**. 16GB 에서 OS(≈3–4GB)+앱(≈2–3GB) 제외 시
  **상시 스왑 위험**.
- ⚠️ GGUF/Ollama 의 7.6GB 를 MLX in-process 메모리로 그대로 옮기면 안 된다. 기본값 교체 판단에는
  **MLX 10–11GB 수치를 우선 반영**하되, 실험 1순위는 메모리 친화적인 **Ollama 7.6GB** 로 한다.
- ⚠️ 일부 2차 출처의 "4bit 8GB 구동" 주장은 11GB 다운로드와 모순 → **실측(§6 G1) 전까지 불신.**

### 3.2 Thinking 모드 / channel 태그 (새 파서 리스크) 🔴

- Gemma 4 12B 는 thinking 모드를 가지며, **thinking 을 꺼도 `<|channel>thought\n<channel|>` 태그를
  출력**한다(E2B/E4B 만 예외). 현재 기본 E4B 에는 없던 동작이라, 12B 도입 시 **새로** 발생한다.
- 우리 다운스트림은 태그를 가정하지 않는다:
  - Corrector 는 `[1] ...` 라인만 파싱 → 태그 혼입 시 파싱 실패 위험.
  - Summarizer/Chat 은 `strip()` 수준 정리 → thought 텍스트가 결과에 노출 위험.
  - Wiki extractor 는 JSON 파싱에 민감 → 태그로 JSON 깨질 위험.
- ⇒ **백엔드 경계에서 다음을 선행 구현해야 한다(§6 G0):**
  - `apply_chat_template(enable_thinking=False)` 적용(가능 시) +
  - `<|channel>thought ... <channel|>` thought-block 제거 유틸 +
  - 멀티턴 이력에 thought 내용 미저장(Chat 슬라이딩 윈도우).

### 3.3 속도 ⚠️ (실측 미확보 — 추정)

- 측정값: Gemma 4 E4B 4bit 는 M3 16GB 에서 40–60 tok/s(2차). 12B 개별 실측은 출시 2일차라 신뢰 출처
  미발견. Dense 12B 는 E4B(유효 4B급) 대비 약 3배 연산 → **M3/M4 16GB ≈13–20 tok/s, M1/M2 그 이하**로 **추정**(§6 G2).
- 영향: 교정(발화 배치 5 × 수십 회) + 요약 → 회의당 LLM 시간이 현재 대비 **약 2–3배** 추정. 팬리스
  Air 쿨다운(2건당 180초)과 맞물려 체감 처리 시간 증가 가능.

### 3.4 품질 (태스크별)

- **요약(추론·지식)**: 12B 강점 영역. 일반 벤치마크에서 12B급은 4B급을 큰 폭 상회 → 요약 품질 향상 기대 합리적.
- **교정(보수적 정정)**: 이미 E4B 가 92.9% 로 천장 근접 → 12B 한계효용 작을 가능성, 메모리·속도 비용 대비 정당화 약함.
- 분리 운영(요약만 12B, 교정 E4B)도 옵션이나, 단계 사이 모델 스왑(언로드→로드) 비용·변동성 증가(§7).

### 3.5 한국어 적합성 ⚠️ (미검증)

- Gemma 4 12B 의 KMMLU 등 **한국어 점수 미공개.** 대형·다국어라 4B급보다 나을 것으로 추정되나 근거 부재.
- Gemma 계열 고질 — **한국어 고유명사에 영어/중국어 로마자 병기**(예: `배미령(Baimilong)`). 12B 가
  이를 줄이는지/악화시키는지 실측 필요(§6 G4). 회의록 인명·조직명 정확도가 핵심인 본 프로젝트에서 **채택 실질 결정 요인**.

### 3.6 라이선스 ✅

- **Apache 2.0** — 로컬·상업 사용 제약 사실상 없음. 100% 로컬 원칙과 합치.

### 3.7 STT/오디오 대체 — 보류

- 공식 모델은 ASR 능력을 포함하나, 현재 STT 파이프라인은 mlx-whisper segment timestamp + VAD +
  화자분리 + checkpoint + chunk/embed 와 결합. 12B 오디오 입력이 가능해도 STT 엔진을 바로 대체하면
  timestamp·diarization·partial retry·검색 인덱싱 계약을 재설계해야 함. **운영 파이프라인은 Whisper/pyannote 유지.**
- 향후 후보(우선순위): ① 저신뢰 STT 세그먼트 재검증 → ② 고유명사 후보 검증 → ③ 짧은 클립 ASR 탐색 → ④ 장기적 STT 대체 평가.

---

## 4. 구현 계획 (옵트인 백엔드 추가 — 옵션 B 기준)

> 기본값을 바꾸지 않고, 사용자가 명시적으로만 12B 를 켤 수 있게 하는 최소 변경.

### 4.1 코드 터치포인트

| # | 파일·위치 | 변경 내용 | 비고 |
|---|-----------|----------|------|
| 0 | LLM 백엔드 공통층(`core/mlx_client.py` / `core/ollama_client.py`) | **thought/channel block 제거 유틸 추가**(§3.2) | **선행 필수** |
| 1 | [`core/mlx_client.py`](../core/mlx_client.py) L99 | 변경 불필요 (`gemma-4` → mlx-vlm 라우팅) | — |
| 2 | [`pyproject.toml`](../pyproject.toml) L54 | `mlx-vlm` 를 **`==0.6.1`**(또는 `>=0.6.1,<0.6.2`) 로 핀 — **0.6.2 회피**(E4B 회귀) | 실측 근거 §0.5 |
| 3 | [`config.yaml`](../config.yaml) L40 | 기본값 **유지**(E4B). 주석에 12B 옵트인 방법 1줄 추가 | 기본 교체 아님 |
| 4 | [`config.yaml`](../config.yaml) L105 | 옵트인 사용자용 가드 상향 안내(§4.2) | — |
| 5 | [`api/routers/settings.py`](../api/routers/settings.py) L21–25 `_ALLOWED_MLX_MODELS` | **이번 단계 추가 안 함**(UI 노출은 옵션 C 로 유예) | 게이트 통과 후 |
| 6 | [`scripts/benchmark_llm.py`](../scripts/benchmark_llm.py) | 모델 목록을 CLI 인자로 받도록 개선 → A/B 자동화 | 검증용 |
| 7 | [`CLAUDE.md`](../CLAUDE.md) LLM 표 | Gemma 4 12B 행 + "옵트인·메모리·thought 태그 주의" 추가 | 후속 |
| 8 | [`docs/BENCHMARK.md`](BENCHMARK.md) L7 포인터 | 본 문서로의 포인터(메모리/Ollama 캐비엇 포함) | 완료 |

참고: `_ALLOWED_MLX_MODELS` 현재 3종 — `EXAONE-3.5-7.8B-Instruct-4bit`, `gemma-4-e4b-it-4bit`,
`gemma-4-e2b-it-4bit`. UI 노출(옵션 C) 시 여기에 12B repo ID 를 추가한다.

### 4.2 사용자 전환 방법 (게이트 통과 전 "전문가용" 경로)

**Ollama (16GB 1순위):**
```bash
ollama pull gemma4:12b
```
```yaml
llm:
  backend: "ollama"
  model_name: "gemma4:12b"
  host: "http://127.0.0.1:11434"
  max_context_tokens: 4096   # 1차 smoke (128K/256K는 운영 목표 아님)
```

**MLX (≥24GB 권장, 16GB 는 빠듯):**
```bash
export MT_LLM_MODEL=mlx-community/gemma-4-12B-it-4bit   # 11GB 상주 — 타 대형 앱 종료 권장
```
```yaml
pipeline:
  peak_ram_limit_gb: 13.0   # 가드 의도적 완화 — 옵트인 사용자 책임 하 (기본 배포 금지)
```

---

## 5. 도입 옵션 비교 및 권장

| 옵션 | 내용 | 장점 | 단점 | 판정 |
|------|------|------|------|------|
| A | 도입 안 함 | 단순·안전 | 신규 고품질 모델 미활용 | 보류 가능 |
| **B** ✅ | **config/env 옵트인 백엔드 추가(+thought 정리)** | 기본 안정성 유지, 전문가 실험 허용 | UI 미노출 | **권장** |
| C | UI 화이트리스트 노출 | 일반 접근성 | 11GB 모델 오설정 시 스왑/크래시 | **게이트 통과 후** |
| D | 기본 모델 교체 | 최고 품질 가능성 | 메모리 예산 초과, 16GB 최소사양 위협 | **비권장(현 시점)** |

**권장 순서: B → (§6 게이트) → C → (요약/한국어 명확 우위) → D 재검토.**

---

## 6. 검증 게이트 (채택 전 반드시 통과)

> 측정은 [`BENCHMARK.md`](BENCHMARK.md) 와 동일 하드웨어/재현 스크립트 관행. 표본이 작으므로 경향성으로
> 해석하되, **G0·G1·G4 미달 시 채택 불가.** MLX 전체 파이프라인을 16GB 에서 바로 돌리지 말고
> `scripts/benchmark_llm.py` 좁은 벤치부터 시작한다.

| 게이트 | 측정 항목 | 합격 기준(제안) |
|--------|-----------|----------------|
| **G0 thought 태그** 🔴 | 12B 응답에서 `<|channel>thought` 제거 + 파서 무결성 | Corrector 파싱률 100%, Summary/Chat/Wiki 에 태그 미노출 |
| **G1 메모리** 🔴 | 12B 로드 시 실측 peak RSS + 스왑 발생 여부 | 16GB 실기에서 스왑 미발생(우선 Ollama 7.6GB) |
| **G2 속도** | 회의 1건 교정+요약 총 소요(E4B 대비) | E4B 대비 ≤2배 권장, 3배 초과 시 기본 후보 제외 |
| **G3 품질-요약** | 동일 회의 요약 E4B vs 12B 비교 | 12B 가 안건/결정/액션아이템 누락 명확 감소 |
| **G4 한국어 고유명사** 🔴 | 인명·조직명 영어/중국어 병기 발생률 | E4B 대비 동등 이하(악화 없음) |
| **G5 환각/원문보존** | 교정 재창작·환각률 | E4B 수준 유지, 금지 패턴(`SPEAKER_*`/`UNKNOWN`) 증가 없음 |
| **G6 안정성/서멀** | 연속 3건 이상 처리 + 팬리스 온도 | halt(95℃) 미발생, 전 회의 완주 |

**1차 측정 현황 (2026-06-06, M4/24GB — §0.5):**

| 게이트 | 1차 결과 | 판정 |
|--------|----------|------|
| G0 thinking 태그 | 기본 경로 미출현 | ✅ 현재 호출 경로 통과(방어코드는 권장) |
| G1 메모리 | MLX peak 11.25GB, **+4.13GB 스왑** | ⚠️ 24GB 한정 통과, **16GB 불가** |
| G2 속도 | 6.1 tok/s (E4B 4.3배 느림) | ⚠️ 권장 한계(≤2배) 초과 |
| G3 요약 품질 | 구조·정확도 우수 | ✅ 유일한 명확 이득 |
| G4 고유명사 | 병기 1건(E4B 0건) | ⚠️ 소폭 악화 |
| G5/G6 | 단발 측정만(연속·서멀 미측정) | ⏳ 미완 |
| 버전 호환 | **0.6.1 이 E4B+12B 둘 다 지원**(0.6.2만 회귀) | ✅ 해결(0.6.1 핀) |

> 1차 측정은 회의 1건·각 태스크 1회의 소표본이다. 채택 판단에는 실회의 다건 A/B(§6.1)가 추가로 필요하다.

### 6.1 권장 검증 순서

1. **Ollama smoke** — `ollama pull gemma4:12b` → backend=ollama, ctx=4096. 로드/응답 시간, 스왑,
   교정 파싱률, thought 태그 노출 확인.
2. **MLX smoke** — `mlx-community/gemma-4-12B-mxfp4`(또는 `it-4bit`)를 `scripts/benchmark_llm.py` 좁은 벤치로.
3. **실제 회의 A/B** — A: E4B(MLX) / B: gemma4:12b(Ollama) / C: 12B-mxfp4(MLX). 위 G0~G6 항목 평가.
   데이터: [`BENCHMARK.md`](BENCHMARK.md) §4 동일 회의(44 발화) + 추가 2–3건.

---

## 7. 리스크 및 완화

| 리스크 | 영향 | 완화 |
|--------|------|------|
| **mlx-vlm 버전** (해결됨) | 0.6.2 는 E4B 를 깨뜨림(회귀) | **0.6.1 에 핀**하면 E4B+12B 동시 지원. `<0.6.2` 또는 `==0.6.1` 로 상한 고정, 0.6.2 회피 |
| MLX ~11GB 상주 → 스왑 (실측 +4.1GB) | 지연, 16GB 불가 | Ollama 7.6GB 우선 + 옵트인 한정 + "타 앱 종료" 안내 + G1 |
| 속도 ~4.3배 저하 (실측 6.1 tok/s) | 회의당 처리시간 급증 | 요약 전용 검토, 교정은 E4B 유지(단 분리 시 스왑) |
| thought/channel 태그 (현재 경로 미출현) | 교정 파싱 실패·요약 오염 가능 | 방어적 thought-block 제거 코드 권장(§3.2) |
| 8GB/16GB Mac 오설정 | 즉시 OOM/스왑 | UI 미노출(옵션 B), 24GB 미만 비권장 명시 |
| 출시 직후 검증 부재 | 한국어 품질 불확실 | G3/G4/G5 + 소규모 A/B 후 단계 노출 |
| 다운로드 ~10GB | 네트워크 부담/실패 | [`CLAUDE.md`](../CLAUDE.md) "네트워크·다운로드 장애 처리 원칙" 준수(SSL 우회 금지, 브라우저 수동 경로) |

---

## 8. 기본값 승격(옵션 D) 조건

아래를 **모두** 만족해야 12B 를 기본값 후보로 올린다.

1. 16GB 에서 `correct → summarize → chunk → embed` 전체 경로가 안정적으로 완료(스왑 과다·반응성 저하 없음).
2. E4B 대비 교정/요약 품질이 실제 회의 **10건 이상**에서 명확히 향상.
3. thought 태그 노출·파싱 실패·고유명사 병기 증가 없음.
4. 처리 시간이 E4B 대비 허용 범위(≤2~3배).
5. ResourceGuard 에 12B 권장 메모리 프로파일을 별도 조정 가능.

충족 전까지 12B 는 기본값이 아니라 **선택형 프리셋**이다.

---

## 9. 미해결 / 추가 검증 필요 (정직한 갭)

1. **12B 실측 tok/s·peak RSS** — 신뢰 가능한 Apple Silicon 12B 4bit 측정치 미발견 → 로컬 측정으로만 확정.
2. **한국어 회의 품질** — KMMLU/한국어 점수 미공개 → 자체 데이터 측정 필요.
3. **고유명사 병기** — 12B 가 Gemma 병기 경향을 줄이는지 미확인.
4. **더 작은 양자화** — Gemma 4 12B 용 3bit/DWQ(예산 적합 가능) MLX repo 현재 미확인.
5. **컨텍스트 수치** — 런타임별 128K/256K 혼재. 운영은 4096→8192 로 보수적 시작.

---

## 10. 부록 — 본 리서치의 방법론 한계 (투명성)

- 1차 딥리서치 워크플로(`deep-research`)는 **적대적 검증 단계의 하네스 장애**로 21개 클레임 전부가
  "0-0"(투표자 0명)으로 표기되어 `findings=[]` 가 반환됐다. 이는 **진짜 반박이 아니라 검증 에이전트가
  구조화 출력을 호출하지 못한 위양성**이다.
- 또한 초기 질의에 넣은 "혹시 Gemma 3 아니냐" caveat 때문에 워크플로가 구세대 **Gemma 3(2025-03)**
  출처만 반복 검증하고, 실제 대상인 **Gemma 4 12B(2026-06-03)** 를 놓쳤다.
- 이에 본 문서의 핵심 사실은 **에이전트가 직접 수행한 표적 웹 검증**(아래 출처) + 1차 메모의 런타임
  크기·thinking 태그 분석을 교차검증해 재확보했다.
- 교훈: 출시 직후 모델은 2차 블로그 수치 편차가 크다. **정량 결론은 §6 로컬 측정으로 반드시 교차검증할 것.**

---

## 11. 출처

**1차 (공식/배포처)**
- [Introducing Gemma 4 12B (Google 공식 블로그)](https://blog.google/innovation-and-ai/technology/developers-tools/introducing-gemma-4-12b/)
- [Gemma 4 12B: The Developer Guide (Google Developers)](https://developers.googleblog.com/gemma-4-12b-the-developer-guide/)
- [Thinking mode in Gemma (Google AI for Developers)](https://ai.google.dev/gemma/docs/capabilities/thinking)
- [Gemma 4 model overview (Google AI for Developers)](https://ai.google.dev/gemma/docs/core)
- [Gemma 4 available on Google Cloud](https://cloud.google.com/blog/products/ai-machine-learning/gemma-4-available-on-google-cloud)
- [google/gemma-4-12B-it (HF 모델 카드)](https://huggingface.co/google/gemma-4-12B-it)
- [mlx-community/gemma-4-12B-it-4bit (4bit ≈11GB · mlx-vlm)](https://huggingface.co/mlx-community/gemma-4-12B-it-4bit)
- [mlx-community/gemma-4-12B-mxfp4 (≈10.9GB)](https://huggingface.co/mlx-community/gemma-4-12B-mxfp4)
- [ollama gemma4:12b (7.6GB · 256K · Text/Image)](https://ollama.com/library/gemma4:12b)
- [ollama gemma4 라이브러리](https://ollama.com/library/gemma4)
- [unsloth/gemma-4-12b-it-GGUF](https://huggingface.co/unsloth/gemma-4-12b-it-GGUF)

**참고/벤치마크**
- [Gemma 4 12B Specs & Run Locally (MMLU-Pro 77.2 등, 2차)](https://www.buildfastwithai.com/blogs/gemma-4-12b-guide)
- [Gemma 4 on Mac M1–M4 속도 벤치(E2B/E4B 측정치)](https://gemma4-ai.com/blog/gemma4-mac-performance)
- [KMMLU: Korean MMLU (arXiv 2402.11548)](https://arxiv.org/abs/2402.11548)
- [EXAONE 3.5 Technical Report (arXiv 2412.04862)](https://arxiv.org/abs/2412.04862)

**내부**
- [`docs/BENCHMARK.md`](BENCHMARK.md) §4 — Gemma 4 E4B vs EXAONE 3.5
- [`config.yaml`](../config.yaml), [`config.py`](../config.py), [`core/mlx_client.py`](../core/mlx_client.py), [`api/routers/settings.py`](../api/routers/settings.py)
