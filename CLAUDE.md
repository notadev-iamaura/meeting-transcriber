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

# 2. Python 3.11+ 확인
python3 --version

# 3. Homebrew 확인
brew --version

# 4. ffmpeg 확인 (없으면: brew install ffmpeg)
ffmpeg -version

# 5. Ollama 확인 (없으면: https://ollama.com 에서 설치)
ollama --version
```

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

# 3단계: Ollama + EXAONE 모델 (약 5GB)
ollama pull exaone3.5:7.8b-instruct-q4_K_M

# 4단계: HuggingFace 토큰 (화자분리용, 아래 "수동 개입 필요" 참고)
export HUGGINGFACE_TOKEN=hf_xxxxx
export HF_TOKEN=hf_xxxxx

# 5단계: 데이터 디렉토리 생성
mkdir -p ~/.meeting-transcriber/audio_input
chmod 700 ~/.meeting-transcriber
```

### 수동 개입이 필요한 항목

> AI 에이전트가 자동으로 완료할 수 없는 단계입니다. 사용자에게 안내하세요.

1. **Ollama 앱 설치**: https://ollama.com 에서 macOS 앱 다운로드 (brew 불가)
2. **HuggingFace 토큰**: pyannote 화자분리 모델은 게이트 모델(gated model)이라 사용자가 직접:
   - https://huggingface.co/join 가입
   - https://huggingface.co/pyannote/speaker-diarization-3.1 → "Agree" 클릭
   - https://huggingface.co/pyannote/segmentation-3.0 → "Agree" 클릭
   - https://huggingface.co/settings/tokens → Read 토큰 발급
   - `export HUGGINGFACE_TOKEN=hf_xxxxx` 및 `export HF_TOKEN=hf_xxxxx` 설정

### 셋업 검증

```bash
# 단위 테스트 (1165개)
pytest tests/ -x -q

# 실행 테스트
python main.py --no-menubar  # Ctrl+C로 종료
```

---

## 기술 스택 (절대 변경 금지)

| 영역 | 기술 | 디바이스 | 비고 |
|------|------|---------|------|
| STT | mlx-whisper + `mlx-community/whisper-medium-mlx` | MPS(GPU) | Apple MLX 가속 |
| 화자분리 | pyannote-audio 3.1 | **CPU 강제** | MPS 버그 있음 |
| LLM | EXAONE 3.5 7.8B Q4_K_M via Ollama | GPU | localhost:11434 |
| 임베딩 | intfloat/multilingual-e5-small (384차원) | MPS(GPU) | query:/passage: 접두사 필수 |
| 벡터DB | ChromaDB PersistentClient | — | |
| 키워드검색 | SQLite FTS5 (unicode61) | — | |
| 웹서버 | FastAPI + uvicorn | — | 단일 프로세스, 데몬 스레드 |
| macOS UI | rumps | — | 메인 스레드 점유 |
| 프론트엔드 | 순수 HTML/CSS/JS | — | 프레임워크 없음 |

---

## 아키텍처

### 핵심 규칙

1. **한 번에 하나의 대형 모델만 메모리 적재** — `ModelLoadManager` 뮤텍스
2. **순차 실행**: STT → 화자분리 → 병합 → LLM보정 → 청크 → 임베딩 → 저장
3. **피크 RAM 9.5GB 이하** 유지 (16GB 중 나머지는 OS + 앱)
4. **pyannote는 반드시 CPU** (`device="cpu"`) — MPS 버그
5. **Ollama는 localhost만** (`http://127.0.0.1:11434`)
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
[5] Corrector       ──→  CorrectedResult (EXAONE via Ollama)
    │                     모델 로드 → 배치 교정 → 모델 언로드
    ▼
[6] Chunker         ──→  ChunkedResult (화자+시간 기반 분할)
    │
    ▼
[7] Embedder        ──→  EmbeddedResult (e5-small, MPS)
    │                     모델 로드 → 벡터화 → ChromaDB+FTS5 저장 → 모델 언로드
    ▼
[8] Summarizer      ──→  요약 텍스트 (EXAONE via Ollama)
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
│   └── watcher.py           # 폴더 감시 (watchdog)
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
│   ├── menubar.py           # macOS 메뉴바 (rumps)
│   └── web/                 # 웹 UI (순수 HTML/CSS/JS)
│       ├── index.html       # 대시보드
│       ├── viewer.html      # 회의록 뷰어
│       ├── chat.html        # AI 채팅
│       ├── style.css        # 스타일
│       └── app.js           # 프론트엔드 로직
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
├── tests/                   # 테스트 (1165개)
├── pyproject.toml           # PEP 621 패키지 설정
├── config.yaml              # 애플리케이션 설정
└── CLAUDE.md                # 이 파일 (AI 에이전트용 가이드)
```

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

# 테스트 실행 (1165개)
pytest tests/ -v

# 빠른 테스트
pytest tests/ -x -q

# 특정 모듈 테스트
pytest tests/test_diarizer.py -v

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
| Ollama 연결 실패 | Ollama 미실행 | Ollama 앱 실행 또는 `ollama serve` |
| EXAONE 모델 없음 | 모델 미다운로드 | `ollama pull exaone3.5:7.8b-instruct-q4_K_M` |
| MPS 관련 크래시 | pyannote MPS 버그 | config.yaml에서 `diarization.device: "cpu"` 확인 |
| ChromaDB ValueError | datetime 메타데이터 | `str()` 변환 확인 |
