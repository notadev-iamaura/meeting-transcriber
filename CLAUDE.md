# Meeting Transcriber — AI 에이전트 프로젝트 가이드

> 이 파일은 Claude Code, Cursor, Windsurf 등 AI 코딩 에이전트가
> 프로젝트를 이해하고 셋업/개발/디버깅할 때 참조하는 문서입니다.

## 프로젝트 요약

MacBook Air (Apple Silicon, 16GB)에서 100% 로컬로 동작하는 한국어 회의 전사 시스템.
외부 API 호출 없이 음성→텍스트→화자분리→AI교정→벡터검색→AI채팅까지 처리한다.

---

## 셋업 가이드 (AI 에이전트용)

> **"이 프로젝트 셋업해줘"** 라고 하면 아래 순서대로 실행하세요.

### 전제 조건 확인

```bash
# 1. macOS + Apple Silicon 확인
uname -m  # arm64여야 함

# 2. 하드웨어 사양 확인 (LLM 백엔드 선택에 필요)
sysctl -n machdep.cpu.brand_string   # 칩 종류 (M1/M2/M3/M4)
echo "$(( $(sysctl -n hw.memsize) / 1073741824 ))GB"  # RAM 용량

# 3. Python 3.11+ 확인
python3 --version

# 4. Homebrew 확인
brew --version

# 5. ffmpeg 확인 (없으면: brew install ffmpeg)
ffmpeg -version

# 6. (Ollama 백엔드 선택 시만) Ollama 확인
ollama --version
```

> **중요**: 2단계에서 확인한 칩 종류와 RAM을 기반으로
> "LLM 백엔드 선택 가이드" 섹션을 참고하여 적합한 백엔드를 결정하세요.
> M3/M4 + 16GB 이상이면 MLX 백엔드를 권장합니다.

### 자동 셋업 (권장)

```bash
# 가상환경 생성 + 의존성 + 시스템 도구 + AI 모델 다운로드
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
bash scripts/install.sh
```

### 수동 셋업 (단계별)

```bash
# 1단계: 가상환경
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2단계: ffmpeg
brew install ffmpeg

# 3단계: LLM 모델 (MLX 기본, 추가 설치 불필요)
# mlx-lm(EXAONE 등) 과 mlx-vlm(Gemma 4 등) 둘 다 pip install -e ".[dev]" 에 포함됨
# 첫 실행 시 EXAONE 3.5 7.8B 4bit 모델이 자동 다운로드됨
#
# [선택] Gemma 4로 모델 변경 시:
# config.yaml의 llm.mlx_model_name을 변경하면 자동 다운로드
# 예: mlx-community/gemma-4-e4b-it-4bit
# Gemma 4 는 멀티모달 모델이라 mlx-vlm 패키지가 자동 사용됨 (core/mlx_client.py 가 분기).
#
# [선택] Ollama 백엔드 사용 시 (별도 서버 프로세스):
# ollama pull exaone3.5:7.8b-instruct-q4_K_M
# config.yaml의 llm.backend: "ollama"로 변경

# 4단계: HuggingFace 토큰 (화자분리용, 아래 "수동 개입 필요" 참고)
export HUGGINGFACE_TOKEN=hf_xxxxx
export HF_TOKEN=hf_xxxxx

# 5단계: 데이터 디렉토리 생성
mkdir -p ~/.meeting-transcriber/audio_input
chmod 700 ~/.meeting-transcriber
```

### 수동 개입이 필요한 항목

> AI 에이전트가 자동으로 완료할 수 없는 단계입니다. 사용자에게 안내하세요.
> 네트워크·SSL·방화벽 문제가 생기면 **"🤖 AI 에이전트용: 네트워크·다운로드 장애 처리 원칙"
> 섹션을 반드시 먼저 읽고** 우회 시도 금지 규칙을 지키세요.

1. **Ollama 앱 설치**: https://ollama.com 에서 macOS 앱 다운로드 (brew 불가)
2. **HuggingFace 토큰**: pyannote 화자분리 모델은 게이트 모델(gated model)이라 사용자가 직접:
   - https://huggingface.co/join 가입
   - https://huggingface.co/pyannote/speaker-diarization-3.1 → "Agree" 클릭
   - https://huggingface.co/pyannote/segmentation-3.0 → "Agree" 클릭
   - https://huggingface.co/settings/tokens → Read 토큰 발급
   - `export HUGGINGFACE_TOKEN=hf_xxxxx` 및 `export HF_TOKEN=hf_xxxxx` 설정
   - ⚠️ 토큰 없이 우회 금지 — 게이트 모델은 약관 동의가 필수이며 에이전트가
     대신 동의하거나 공개 미러를 찾아 사용하면 안 됩니다.

### 셋업 검증

```bash
# 단위 테스트 (1700+개)
pytest tests/ -x -q

# 실행 테스트
python main.py --no-menubar  # Ctrl+C로 종료
```

---

## 🤖 AI 에이전트용: 네트워크·다운로드 장애 처리 원칙

> **다른 Claude Code 인스턴스가 이 프로젝트를 셋업하거나 STT 모델을 다운로드할 때
> 반드시 이 규칙을 따르세요.** 이 섹션의 규칙은 사용자의 보안·시스템 안정성을 보호하며,
> 우회 시도가 오히려 더 큰 문제를 일으킬 수 있습니다.

### 절대 금지 — 에이전트가 스스로 하면 안 되는 것

HuggingFace·`pip install`·네트워크 호출이 SSL/인증/방화벽/프록시 이유로 실패할 때,
**다음 우회 시도는 절대 하지 마세요**:

- ❌ `pip install --trusted-host` / `--index-url http://...` 같은 SSL 우회
- ❌ `REQUESTS_CA_BUNDLE=""` / `PYTHONHTTPSVERIFY=0` 같은 환경변수 설정
- ❌ `verify=False` / `ssl._create_unverified_context()` 같은 코드 수정
- ❌ `/etc/hosts` 편집, DNS 변경, VPN 설정
- ❌ `curl -k`, `wget --no-check-certificate` 같은 인증서 검증 스킵
- ❌ HuggingFace 토큰·API 키를 사용자 몰래 추측·생성·재사용
- ❌ 사용자 방화벽·코퍼릿 프록시 설정 수정

**이유**: 대부분의 네트워크 실패는 사용자의 회사·학교 네트워크 정책, 국가 단위 차단,
또는 잠깐의 서비스 장애 때문입니다. 에이전트가 우회를 시도하면 (1) 보안 정책 위반,
(2) 맬웨어 설치 위험, (3) 디버깅 어려워진 상태로 진행되는 문제를 일으킵니다.

### 대신 해야 할 것 — 브라우저 다운로드 안내

자동 다운로드가 실패하면 **사용자에게 브라우저로 직접 받도록 안내**하세요.
이 프로젝트는 이 흐름을 위한 API + UI를 이미 제공합니다.

#### Python 패키지 설치 실패 시 (`pip install` SSL/네트워크 오류)

1. 사용자에게 에러 전체를 보여주고 원인을 한국어로 설명
2. `pip install` 은 건들지 말고, 사용자에게 다음을 권장:
   - 회사 네트워크면 IT팀에 `pypi.org` / `files.pythonhosted.org` / `huggingface.co`
     화이트리스트 요청
   - 개인 네트워크면 모바일 핫스팟·다른 네트워크 시도
   - 또는 [`uv`](https://github.com/astral-sh/uv) 같은 대체 패키지 매니저 사용
3. 해결 안 되면 **원인을 명확히 문서화**하고 작업 중단, 사용자 판단 대기

#### STT 모델 다운로드 실패 시 (3가지 모델 모두 동일)

앱 내 자동 다운로드가 실패하면, **로컬 양자화를 시도하거나 SSL 우회하지 말고**
브라우저 다운로드 경로를 이용:

```bash
# 1) API 로 수동 다운로드 정보 조회
curl -s http://127.0.0.1:8765/api/stt-models/seastar-medium-4bit/manual-download-info | jq
# 응답 예시:
# {
#   "supported": true,
#   "files": [
#     {"name": "config.json", "url": "https://huggingface.co/youngouk/seastar-medium-ko-4bit-mlx/resolve/main/config.json"},
#     {"name": "weights.safetensors", "url": "https://huggingface.co/.../weights.safetensors"}
#   ],
#   "target_directory": "/Users/{user}/.meeting-transcriber/stt_models/seastar-medium-4bit-manual",
#   "instructions": "..."
# }
```

2) 에이전트는 사용자에게 **URL 2개를 그대로 보여주고** 다음과 같이 안내:
   ```
   자동 다운로드가 실패했어요. 아래 두 URL을 브라우저로 열어 파일을 받으신 후,
   한 폴더(예: ~/Downloads/seastar)에 같이 저장해 주세요:

   1. [config.json URL]
   2. [weights.safetensors URL]

   저장 후 다음 명령으로 가져오기를 할 수 있어요:
     curl -X POST http://127.0.0.1:8765/api/stt-models/seastar-medium-4bit/import-manual \
       -H "Content-Type: application/json" \
       -d '{"source_dir": "/Users/{user}/Downloads/seastar"}'
   ```

3) 또는 GUI에서: `http://127.0.0.1:8765/app/settings` → 음성 인식 모델 → 해당 카드
   → "▸ 브라우저로 직접 받기" 펼침 → 링크 클릭 → 다운로드 → 폴더 경로 입력 → "가져오기"

4) **절대 금지**: `~/.meeting-transcriber/stt_models/` 아래에 직접 파일 생성/복사
   하지 마세요. 반드시 `POST /api/stt-models/{id}/import-manual` 엔드포인트를 통해
   가져와야 앱이 올바른 위치(`{id}-manual/`)에 배치하고 상태를 갱신합니다.

#### `pip install` 자체가 완전히 불가능할 때

의존성 설치 자체가 안 되면 앱을 실행할 수 없으므로:
1. 문제 원인을 사용자에게 보고 (네트워크 타입, 에러 메시지, 시도한 방법)
2. **작업 중단** — 추측으로 진행하지 말 것
3. 사용자가 환경 문제를 해결한 뒤 재개

### 수동 다운로드 엔드포인트 계약 요약

| 엔드포인트 | 역할 |
|---|---|
| `GET /api/stt-models` | 전체 모델 목록 + 상태 (`ready` / `not_downloaded` / `downloading` / `error`) |
| `POST /api/stt-models/{id}/download` | 자동 다운로드 시작 (HF snapshot_download) |
| `GET /api/stt-models/{id}/manual-download-info` | HF 직접 URL 2개 + 타겟 폴더 경로 반환 |
| `POST /api/stt-models/{id}/import-manual` | `{source_dir: "..."}` 받아서 파일 검증 후 복사 |
| `POST /api/stt-models/{id}/activate` | `config.yaml` 의 `stt.model_name` 업데이트 |

수동 임포트된 모델은 `get_effective_model_path()`가 자동으로 감지하여
`{id}-manual/` 로컬 경로를 HF repo ID보다 우선 사용합니다.

---

## 기술 스택 (절대 변경 금지)

| 영역 | 기술 | 디바이스 | 비고 |
|------|------|---------|------|
| STT | mlx-whisper + 한국어 fine-tune 모델 3종(GUI 선택) | MPS(GPU) | Apple MLX 가속, 웹 설정에서 다운로드/활성화 |
| 화자분리 | pyannote-audio 3.1 | **CPU 강제** | MPS 버그 있음 |
| LLM | EXAONE 3.5 7.8B 또는 Gemma 4 — **MLX 기본** (Ollama 선택 가능) | GPU | `config.yaml`의 `llm.mlx_model_name` |
| 임베딩 | intfloat/multilingual-e5-small (384차원) | MPS(GPU) | query:/passage: 접두사 필수 |
| 벡터DB | ChromaDB PersistentClient | — | |
| 키워드검색 | SQLite FTS5 (unicode61) | — | |
| 웹서버 | FastAPI + uvicorn | — | 단일 프로세스, 데몬 스레드 |
| macOS UI | rumps | — | 메인 스레드 점유 |
| 네이티브 창 | pywebview 4.x | — | 서브프로세스로 실행 (rumps와 메인스레드 충돌 방지) |
| 프론트엔드 | 순수 HTML/CSS/JS (SPA) | — | 프레임워크 없음, History API 라우팅 |

### LLM 모델 선택 가이드

> MLX가 기본 백엔드입니다. 추가 설치 없이 `pip install -e ".[dev]"`로 완료됩니다.
> 모델은 첫 실행 시 HuggingFace에서 자동 다운로드됩니다.

**지원 모델:**

| 모델 | config.yaml `mlx_model_name` | 크기 | 특징 |
|------|------------------------------|------|------|
| **EXAONE 3.5** (기본) | `mlx-community/EXAONE-3.5-7.8B-Instruct-4bit` | ~5GB | 한국어 특화, 검증됨 |
| **Gemma 4 E4B** | `mlx-community/gemma-4-e4b-it-4bit` | ~6GB | Google, 다국어 140+, Thinking 모드 |
| **Gemma 4 E2B** | `mlx-community/gemma-4-e2b-it-4bit` | ~3GB | 경량, 8GB RAM 가능 |

**모델 변경 방법:**

```yaml
# config.yaml — mlx_model_name만 교체하면 됨
llm:
  backend: "mlx"
  mlx_model_name: "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit"  # ← 여기를 변경
```

```bash
# 또는 환경변수로 오버라이드
export MT_LLM_MODEL=mlx-community/gemma-4-e4b-it-4bit
```

**하드웨어별 권장 모델:**

| 조건 | 권장 모델 | 이유 |
|------|----------|------|
| M3/M4 + 16GB 이상 | EXAONE 3.5 또는 Gemma 4 E4B | 둘 다 여유 있게 실행 |
| M1/M2 + 16GB 이상 | EXAONE 3.5 | 검증된 성능 |
| M1/M2 + 8GB | Gemma 4 E2B | ~3GB로 메모리 절약 |

**Ollama 백엔드 (선택, 별도 서버 필요):**

```yaml
# Ollama를 사용하려면 backend를 변경하고 Ollama 앱 설치 필요
llm:
  backend: "ollama"
  model_name: "exaone3.5:7.8b-instruct-q4_K_M"
  host: "http://127.0.0.1:11434"
```

```bash
# Ollama 앱 설치: https://ollama.com
ollama pull exaone3.5:7.8b-instruct-q4_K_M
```

### STT 모델 선택 가이드 (한국어 음성 인식)

> **웹 UI에서 다운로드/활성화 가능** (`/app/settings` → "음성 인식 모델 (STT)")
> 또는 `config.yaml`의 `stt.model_name`을 직접 수정.

**지원 모델 (한국어 fine-tune, 모두 사전 양자화 HF 배포):**

| 모델 ID | 베이스 | CER | WER | RAM | 디스크 | HuggingFace | 추천 |
|--------|--------|-----|-----|-----|--------|-------------|------|
| `komixv2` (기본) | Whisper Medium fp16 | 11.88% | 33.26% | 1.88GB | 1.5GB | [`youngouk/whisper-medium-komixv2-mlx`](https://huggingface.co/youngouk/whisper-medium-komixv2-mlx) | 호환성 |
| `seastar-medium-4bit` ⭐ | Medium + Zeroth (4bit) | **1.25%** | **3.21%** | **1.26GB** | **420MB** | [`youngouk/seastar-medium-ko-4bit-mlx`](https://huggingface.co/youngouk/seastar-medium-ko-4bit-mlx) | 정확도 최고 |
| `ghost613-turbo-4bit` | Large-v3-turbo + Zeroth (4bit) | 1.60% | 4.36% | 1.31GB | 442MB | [`youngouk/ghost613-turbo-korean-4bit-mlx`](https://huggingface.co/youngouk/ghost613-turbo-korean-4bit-mlx) | 속도 우선 |

**모델 변경 방법:**

```bash
# 1) GUI: http://127.0.0.1:8765/app/settings → "음성 인식 모델 (STT)" → 다운로드 → 활성화
#    자동 다운로드가 안 되면 "브라우저로 직접 받기" 펼침 → URL 복사 → 브라우저로 받기 → 폴더 경로 입력 → 가져오기

# 2) API
curl -X POST http://127.0.0.1:8765/api/stt-models/seastar-medium-4bit/download
curl -X POST http://127.0.0.1:8765/api/stt-models/seastar-medium-4bit/activate

# 3) config.yaml 직접 수정
# stt.model_name: "youngouk/seastar-medium-ko-4bit-mlx"   # HF repo ID 직접 사용
```

**구현 모듈:**

- `core/stt_model_registry.py` — STTModelSpec + 3종 메타데이터 + 수동 다운로드 URL 헬퍼
- `core/stt_model_status.py` — 상태 판정 (수동 임포트 > HF 캐시 > 로컬 경로)
- `core/stt_model_downloader.py` — 백그라운드 HF 스냅샷 다운로드 + 검증
- `api/routes.py` — `/api/stt-models/*` (list, download, download-status, activate, manual-download-info, import-manual)

**배포 전략:** 모든 모델은 저자가 사전 양자화·업로드한 결과물을 HuggingFace에서 직접 받습니다. 사용자 환경에 `mlx-examples` 같은 추가 의존성이 필요 없고, 다운로드 1회로 완료됩니다. 네트워크·방화벽 이슈가 있는 사용자는 GUI의 "브라우저로 직접 받기" 섹션에서 HF 직접 URL을 받아 수동 다운로드 후 "가져오기"로 설치할 수 있습니다.

---

## 아키텍처

### 핵심 규칙

1. **한 번에 하나의 대형 모델만 메모리 적재** — `ModelLoadManager` 뮤텍스
2. **순차 실행**: STT → 화자분리 → 병합 → LLM보정 → 청크 → 임베딩 → 저장
3. **피크 RAM 9.5GB 이하** 유지 (16GB 중 나머지는 OS + 앱)
4. **pyannote는 반드시 CPU** (`device="cpu"`) — MPS 버그
5. **MLX는 in-process** (기본), Ollama 사용 시 localhost만 (`http://127.0.0.1:11434`)
6. **rumps는 메인 스레드**, FastAPI는 데몬 스레드
7. **모든 중간 결과는 JSON 체크포인트** — 실패 시 재개 가능
8. **서멀 관리**: 2건 처리 후 3분 쿨다운 (팬리스 MacBook Air)

### 파이프라인 흐름

```
오디오 파일 (WAV/MP3/M4A)
    │
    ▼
[1] AudioConverter  ──→  16kHz mono WAV
    │
    ▼
[2] Transcriber     ──→  TranscriptResult (mlx-whisper, MPS)
    │                     모델 로드 → 전사 → 모델 언로드
    ▼
[3] Diarizer        ──→  DiarizationResult (pyannote, CPU 강제)
    │                     모델 로드 → 화자분리 → 모델 언로드
    ▼
[4] Merger          ──→  MergedResult (시간 겹침 기반 매칭)
    │
    ▼
[5] Corrector       ──→  CorrectedResult (EXAONE via Ollama 또는 MLX)
    │                     백엔드 로드 → 배치 교정 → 백엔드 언로드
    ▼
[6] Chunker         ──→  ChunkedResult (화자+시간 기반 분할)
    │
    ▼
[7] Embedder        ──→  EmbeddedResult (e5-small, MPS)
    │                     모델 로드 → 벡터화 → ChromaDB+FTS5 저장 → 모델 언로드
    ▼
[8] Summarizer      ──→  요약 텍스트 (EXAONE via Ollama 또는 MLX)
```

### 디렉토리 구조

```
meeting-transcriber/
├── main.py                  # 앱 진입점 (rumps 메인스레드 + FastAPI 데몬스레드)
├── config.py                # 설정 관리 (Pydantic + YAML + 환경변수 오버라이드)
├── config.yaml              # 설정 파일 (모든 설정값의 단일 진실 공급원)
│
├── core/                    # 핵심 엔진
│   ├── pipeline.py          # 전사 파이프라인 오케스트레이터 (8단계 순차)
│   ├── model_manager.py     # ModelLoadManager — 뮤텍스 기반 모델 수명 관리
│   ├── job_queue.py         # SQLite 기반 작업 큐
│   ├── thermal_manager.py   # 서멀 관리 (2-job 배치 + 쿨다운)
│   ├── watcher.py           # 폴더 감시 (watchdog)
│   ├── stt_model_registry.py    # STT 모델 메타데이터 (komixv2/seastar/ghost613)
│   ├── stt_model_status.py      # STT 모델 다운로드 상태 판정
│   └── stt_model_downloader.py  # 백그라운드 HF snapshot_download + 검증 (사전 양자화 모델 대상)
│
├── steps/                   # 파이프라인 각 단계 (독립 모듈)
│   ├── audio_converter.py   # ffmpeg 기반 WAV 변환
│   ├── transcriber.py       # STT (mlx-whisper)
│   ├── diarizer.py          # 화자분리 (pyannote, CPU 강제)
│   ├── merger.py            # 전사+화자 병합 (시간 겹침 매칭)
│   ├── corrector.py         # LLM 교정 (EXAONE via Ollama)
│   ├── chunker.py           # 시맨틱 청크 분할
│   ├── embedder.py          # 벡터 임베딩 + ChromaDB/FTS5 저장
│   ├── summarizer.py        # AI 요약
│   └── zoom_detector.py     # Zoom 회의 종료 감지
│
├── search/                  # 검색 엔진
│   ├── hybrid_search.py     # 하이브리드 검색 (벡터 0.6 + FTS5 0.4, RRF k=60)
│   └── chat.py              # RAG 채팅 (검색→컨텍스트→EXAONE 답변)
│
├── api/                     # REST API + WebSocket
│   ├── server.py            # FastAPI 앱 팩토리
│   ├── routes.py            # API 라우트 정의
│   └── websocket.py         # WebSocket 실시간 진행 통신
│
├── ui/                      # 사용자 인터페이스
│   ├── menubar.py           # macOS 메뉴바 (rumps) — 네이티브 창 통합
│   ├── native_window.py     # PyWebView 네이티브 창 (서브프로세스 실행)
│   └── web/                 # 웹 UI (SPA, 순수 HTML/CSS/JS)
│       ├── index.html       # SPA 셸 (사이드바 + 콘텐츠 영역)
│       ├── viewer.html      # SPA 리다이렉트 스텁 → /app/viewer/{id}
│       ├── chat.html        # SPA 리다이렉트 스텁 → /app/chat
│       ├── style.css        # macOS 디자인 언어 (light/dark 자동 전환)
│       ├── app.js           # 공용 유틸리티 (API, WebSocket, escapeHtml 등)
│       └── spa.js           # SPA 라우터 + 뷰 컨트롤러 (Home/Viewer/Chat)
│
├── security/                # 보안
│   ├── secure_dir.py        # 디렉토리 권한 설정 (chmod 700)
│   ├── lifecycle.py         # 데이터 수명주기 (hot→warm→cold)
│   └── health_check.py      # 시스템 상태 점검
│
├── scripts/                 # 설치/배포 스크립트
│   ├── install.sh           # 통합 설치 스크립트
│   └── setup_launchagent.sh # macOS 로그인 시 자동 시작
│
├── tests/                   # 테스트 (1231개)
├── pyproject.toml           # PEP 621 패키지 설정
├── config.yaml              # 애플리케이션 설정
└── CLAUDE.md                # 이 파일 (AI 에이전트용 가이드)
```

### 웹 UI 아키텍처 (SPA + 네이티브 창)

#### SPA 구조

웹 UI는 **SPA(Single Page Application)** 로 동작한다. 3개 HTML 페이지가 아닌 `index.html` 하나로 모든 뷰를 처리한다.

```
/app              → HomeView (회의 목록 대시보드)
/app/viewer/{id}  → ViewerView (회의록 뷰어)
/app/chat         → ChatView (AI 채팅)
```

- **서버 라우팅**: `api/server.py`의 `_setup_spa_routes()`가 `/app` 및 `/app/{path:path}` catch-all 라우트로 `index.html` 반환
- **클라이언트 라우팅**: `spa.js`의 `Router`가 History API(`pushState`/`popstate`)로 뷰 전환
- **레이아웃**: 왼쪽 250px 사이드바(회의 목록 항상 표시) + 오른쪽 콘텐츠 영역
- **WebSocket**: 뷰 전환 시에도 연결 유지 (app.js가 관리)
- **하위 호환**: `viewer.html`, `chat.html`은 SPA 경로로 리다이렉트하는 스텁

#### 파일 역할 분리

| 파일 | 역할 | 로드 순서 |
|------|------|----------|
| `app.js` | 공용 유틸리티 (API, WebSocket, escapeHtml, 마크다운 파서) | 1번째 |
| `spa.js` | SPA 라우터, Sidebar, HomeView, ViewerView, ChatView | 2번째 (app.js 의존) |
| `style.css` | macOS 디자인 언어 CSS (light/dark 자동 전환) | — |

#### 네이티브 창

메뉴바 "웹 UI 열기" 클릭 시 PyWebView 네이티브 창으로 열린다. 실패 시 브라우저 폴백.

```
rumps (메인 스레드)
  → _on_open_web_ui()
    → launch_native_window()  # subprocess.Popen으로 별도 프로세스 실행
      → python -m ui.native_window --url http://127.0.0.1:8765/app
        → webview.create_window() + webview.start()
    → 실패 시: webbrowser.open() 폴백
```

- `ui/native_window.py`: `NativeWindowConfig`(dataclass) + `build_window_config()` + `launch_native_window()` + `run_webview_window()`
- `config.py`: `WindowConfig`(Pydantic) — `use_native`, `width`, `height`, `title` 등 설정
- **서브프로세스 필수**: rumps와 pywebview 모두 NSApplication 메인 스레드를 요구하므로 같은 프로세스 불가

#### CSS 디자인 시스템

macOS Finder/메모 앱 스타일. CSS 변수 기반으로 light/dark 자동 전환.

```css
/* 핵심 디자인 토큰 (style.css :root) */
--bg-canvas, --bg-sidebar, --bg-card, --bg-secondary, --bg-input
--text-primary, --text-secondary, --text-muted
--accent (#007aff / #0a84ff)
--border (0.5px hairline), --shadow, --radius

/* @media (prefers-color-scheme: dark) 에서 자동 전환 */
/* [data-theme="dark"] 로 수동 토글도 지원 */
```

> **🎨 디자인 작업 시 반드시 참고**: [`docs/design.md`](docs/design.md)
>
> macOS 네이티브 디자인 원칙(Vibrancy, 0.5px hairline, Independent Dark Mode, macOS easing),
> 디자인 토큰(컬러/타이포/스페이싱/radius/shadow), 컴포넌트 패턴(카드/버튼/입력/모달/툴팁/빈상태/스켈레톤),
> SaaS 패턴(Command Palette, Hidden AI, Progressive Disclosure),
> 안티 패턴 금지 목록, 우리 프로젝트 적용 우선순위까지 전부 정리되어 있다.
>
> **UI/CSS/스타일 관련 작업 전에 반드시 docs/design.md 를 먼저 읽고 시작할 것.**

---

## 코딩 규칙

### 필수 패턴

- Python 타입 힌트 필수 (모든 함수 시그니처)
- docstring은 한국어로 작성
- 로깅: `logging` 모듈, `logger = logging.getLogger(__name__)`
- 에러 처리: 구체적 예외 타입, bare except 금지
- 설정값은 `config.yaml`에서 로드 (하드코딩 금지)
- 비동기: `asyncio` 사용 (threading은 rumps/FastAPI 연동에만)
- 문자열: f-string (`.format()` 또는 `%` 금지)
- 경로: `pathlib.Path` (os.path 금지)
- **UI/CSS/디자인 작업**: 반드시 [`docs/design.md`](docs/design.md) 먼저 읽고 시작 (디자인 토큰/컴포넌트 패턴/안티 패턴)

### 금지 사항

- 외부 API 호출 (인터넷 전송 절대 불가)
- `pyannote`에서 `device="mps"` 사용
- 모델 동시 로드 (반드시 이전 모델 언로드 후 다음 로드)
- `print()` 사용 (logger 사용)
- 하드코딩된 경로/수치 (config.yaml 관리)
- bare except

### 환경변수

| 변수 | 용도 | 예시 |
|------|------|------|
| `HUGGINGFACE_TOKEN` | pyannote 모델 다운로드 인증 | `hf_xxxxx` |
| `HF_TOKEN` | huggingface_hub 라이브러리 인증 | `hf_xxxxx` (위와 동일값) |
| `MT_BASE_DIR` | 데이터 디렉토리 오버라이드 | `~/.meeting-transcriber` |
| `MT_SERVER_PORT` | 웹서버 포트 오버라이드 | `8765` |
| `MT_LLM_HOST` | Ollama 호스트 오버라이드 | `http://127.0.0.1:11434` |
| `MT_LLM_BACKEND` | LLM 백엔드 오버라이드 | `ollama` 또는 `mlx` |
| `MT_LLM_MODEL` | MLX 모델명 오버라이드 | `mlx-community/gemma-4-e4b-it-4bit` |
| `MT_LOG_LEVEL` | 로그 레벨 | `debug` |

---

## 빌드/실행 명령어

```bash
# 가상환경 활성화
source .venv/bin/activate

# 실행 (메뉴바 + 웹서버)
python main.py

# 헤드리스 모드 (서버만)
python main.py --no-menubar

# 단위/통합 테스트 (1800+개, E2E 자동 제외)
pytest tests/ -v

# 빠른 테스트
pytest tests/ -x -q

# 특정 모듈 테스트
pytest tests/test_diarizer.py -v

# Playwright E2E 테스트 (브라우저 기반, 약 24초, 별도 실행)
# 전제: `pip install -e ".[dev]"` + `playwright install chromium`
pytest -m e2e tests/test_e2e_edit_playwright.py -v

# 린트
python -m py_compile config.py
python -m py_compile main.py
```

---

## 알려진 호환성 이슈

### pyannote-audio 4.x

- `Pipeline.from_pretrained()`의 인증 파라미터: `token=` 사용 (`use_auth_token` 아님)
- 반환 타입이 `DiarizeOutput`으로 변경됨 (기존 `Annotation` 아님)
- `DiarizeOutput`에서 `.speaker_diarization` 속성으로 `Annotation` 추출 필요
- `callable(getattr(obj, "itertracks", None))`으로 타입 판별

### mlx-whisper 0.4.x

- `beam_size` 파라미터 미구현 (`NotImplementedError`)
- greedy 디코딩만 지원, beam_size 전달하지 않아야 함

### ChromaDB 메타데이터

- `str`, `int`, `float`, `bool`만 허용
- `datetime` 객체는 `str()`로 변환 후 저장

---

## 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| `TokenNotConfiguredError` | HuggingFace 토큰 미설정 | `export HUGGINGFACE_TOKEN=hf_xxx` |
| pyannote 모델 403 에러 | 게이트 모델 미동의 | HuggingFace에서 모델 페이지 방문 후 Agree 클릭 |
| `NotImplementedError: beam_size` | mlx-whisper 0.4.x | transcriber.py에서 beam_size 파라미터 제거 |
| Ollama 연결 실패 | Ollama 미실행 (backend=ollama일 때) | Ollama 앱 실행 또는 `ollama serve` |
| EXAONE 모델 없음 | 모델 미다운로드 (backend=ollama일 때) | `ollama pull exaone3.5:7.8b-instruct-q4_K_M` |
| MLXLoadError (EXAONE 등) | mlx-lm 미설치 (오래된 설치 환경) | `pip install -e .` 재실행 또는 `pip install mlx-lm` |
| MLXLoadError (Gemma 4) | mlx-vlm 미설치 (오래된 설치 환경) | `pip install -e .` 재실행 또는 `pip install mlx-vlm` |
| MLX 메모리 부족 | RAM 부족 (8GB 이하에서 MLX 사용) | `llm.backend: "ollama"`로 변경 |
| MPS 관련 크래시 | pyannote MPS 버그 | config.yaml에서 `diarization.device: "cpu"` 확인 |
| ChromaDB ValueError | datetime 메타데이터 | `str()` 변환 확인 |
| 네이티브 창 미열림 | pywebview 미설치 | `pip install pywebview` (브라우저 폴백 자동 작동) |
| SPA 라우팅 404 | 서버에 `/app` 라우트 미등록 | `_setup_spa_routes(app)` 호출 확인 (server.py) |
