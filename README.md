# Meeting Transcriber

**한국어 로컬 AI 회의 전사 시스템** — 100% 오프라인, Apple Silicon 최적화

회의 녹음 파일을 넣으면 자동으로 텍스트 변환, 화자 분리, AI 교정, 요약까지 처리합니다.
모든 데이터는 로컬에서만 처리되며, 외부 서버로 전송되지 않습니다.

## 주요 기능

- **음성 → 텍스트 변환**: mlx-whisper 기반 한국어 STT (Apple Silicon MLX 가속)
- **화자 분리**: pyannote-audio 3.1로 발화자별 자동 분리
- **AI 교정**: EXAONE 3.5 7.8B (Ollama) 로컬 LLM으로 전사 오류 교정
- **시맨틱 검색**: ChromaDB + SQLite FTS5 하이브리드 검색
- **AI 채팅**: 회의 내용 기반 질의응답
- **macOS 메뉴바 앱**: rumps 기반 시스템 트레이 상주
- **웹 UI**: FastAPI + WebSocket 실시간 진행 상황 확인
- **Zoom 감지**: Zoom 회의 종료 시 자동 전사 시작
- **폴더 감시**: 지정 폴더에 파일 추가 시 자동 처리
- **서멀 관리**: 팬리스 MacBook Air 대응, 2-job + 쿨다운 패턴

## 시스템 요구사항

| 항목 | 최소 사양 |
|------|-----------|
| OS | macOS 14 (Sonoma) 이상 |
| 칩 | Apple Silicon (M1, M2, M3, M4) |
| RAM | 16GB 이상 |
| 디스크 | 20GB 이상 여유 공간 |
| Python | 3.11 이상 |
| 기타 | ffmpeg, Ollama |

## 빠른 시작

### 1. 저장소 클론

```bash
git clone https://github.com/notadev-iamaura/meeting-transcriber.git
cd meeting-transcriber
```

### 2. Python 가상환경 생성

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. 의존성 설치

```bash
pip install -e ".[dev]"
```

### 4. 시스템 의존성 설치 (자동)

```bash
bash scripts/install.sh
```

이 스크립트가 자동으로 처리하는 항목:
- Homebrew 확인
- Python 3.11+ 확인
- ffmpeg 설치
- Ollama 확인
- EXAONE 3.5 모델 다운로드
- 데이터 디렉토리 생성 + 보안 설정

### 5. HuggingFace 토큰 설정 (화자 분리에 필요)

```bash
export HUGGINGFACE_TOKEN=hf_xxxxx
```

[HuggingFace 토큰 발급](https://huggingface.co/settings/tokens)에서 토큰을 생성하세요.
[pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1) 모델의 사용 동의가 필요합니다.

### 6. 실행

```bash
# 메뉴바 + 웹 서버 실행 (기본)
python main.py

# 헤드리스 모드 (서버만)
python main.py --no-menubar

# 포트 변경
python main.py --port 9000

# 디버그 로깅
python main.py --log-level debug
```

## 상세 설치 가이드

### Homebrew (미설치 시)

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### Python 3.11+

```bash
brew install python@3.11
```

### ffmpeg

```bash
brew install ffmpeg
```

### Ollama

[ollama.com](https://ollama.com)에서 macOS 앱을 다운로드하여 설치합니다.

```bash
# EXAONE 3.5 모델 다운로드 (약 5GB)
ollama pull exaone3.5:7.8b-instruct-q4_K_M
```

### 설치 상태 확인

```bash
bash scripts/install.sh --check
```

## 사용법

### 메뉴바 모드 (기본)

`python main.py` 실행 시 macOS 메뉴바에 아이콘이 나타납니다.
웹 브라우저에서 `http://127.0.0.1:8765` 으로 접속하여 사용할 수 있습니다.

### 헤드리스 모드

```bash
python main.py --no-menubar
```

서버만 실행합니다. SSH 접속이나 서비스 등록 시 유용합니다.

### 자동 전사

1. **폴더 감시**: `~/.meeting-transcriber/audio_input/`에 오디오 파일을 넣으면 자동 전사
2. **Zoom 감지**: Zoom 회의 종료 시 자동으로 녹음 파일 전사

### 검색 및 채팅

웹 UI에서 과거 회의 내용을 검색하거나, AI 채팅으로 질의할 수 있습니다.

### 로그인 시 자동 시작

```bash
bash scripts/setup_launchagent.sh
```

## 설정

`config.yaml` 파일에서 모든 설정을 관리합니다. 주요 항목:

| 설정 | 설명 | 기본값 |
|------|------|--------|
| `paths.base_dir` | 데이터 디렉토리 | `~/.meeting-transcriber` |
| `stt.model_name` | Whisper 모델 | `whisper-medium-ko-zeroth` |
| `llm.model_name` | LLM 모델 | `exaone3.5:7.8b-instruct-q4_K_M` |
| `llm.host` | Ollama 주소 | `http://127.0.0.1:11434` |
| `server.port` | 웹 서버 포트 | `8765` |
| `thermal.batch_size` | 연속 처리 건수 | `2` |
| `thermal.cooldown_seconds` | 쿨다운 시간 | `180` (3분) |

환경변수로 오버라이드 가능:

| 환경변수 | 설명 |
|----------|------|
| `MT_BASE_DIR` | 데이터 디렉토리 |
| `MT_SERVER_PORT` | 서버 포트 |
| `MT_LLM_HOST` | Ollama 호스트 |
| `HUGGINGFACE_TOKEN` | HuggingFace 토큰 |

## 프로젝트 구조

```
meeting-transcriber/
├── main.py                  # 앱 진입점 (rumps + FastAPI)
├── config.py                # 설정 관리 (Pydantic + YAML)
├── config.yaml              # 설정 파일
├── core/                    # 핵심 엔진
│   ├── pipeline.py          # 전사 파이프라인 (8단계 순차 처리)
│   ├── model_manager.py     # 모델 순차 로드 (RAM 9.5GB 제한)
│   ├── job_queue.py         # 작업 큐 관리
│   ├── thermal_manager.py   # 서멀 관리 (2-job + 쿨다운)
│   └── watcher.py           # 폴더 감시
├── steps/                   # 파이프라인 단계
│   ├── audio_converter.py   # 오디오 → WAV 변환
│   ├── transcriber.py       # STT (mlx-whisper)
│   ├── diarizer.py          # 화자 분리 (pyannote)
│   ├── merger.py            # 전사 + 화자 병합
│   ├── corrector.py         # AI 교정 (EXAONE)
│   ├── chunker.py           # 텍스트 청크 분할
│   ├── embedder.py          # 벡터 임베딩
│   ├── summarizer.py        # AI 요약
│   └── zoom_detector.py     # Zoom 회의 감지
├── search/                  # 검색 엔진
│   ├── hybrid_search.py     # 하이브리드 검색 (Vector + FTS5)
│   └── chat.py              # AI 채팅 (RAG)
├── api/                     # REST API
│   ├── server.py            # FastAPI 서버
│   ├── routes.py            # API 라우트
│   └── websocket.py         # WebSocket 실시간 통신
├── ui/                      # 사용자 인터페이스
│   ├── menubar.py           # macOS 메뉴바 (rumps)
│   └── web/                 # 웹 UI
│       ├── index.html       # 대시보드
│       ├── viewer.html      # 회의록 뷰어
│       ├── chat.html        # AI 채팅
│       ├── style.css        # 스타일
│       └── app.js           # 프론트엔드 JS
├── security/                # 보안
│   ├── secure_dir.py        # 디렉토리 보안 설정
│   ├── lifecycle.py         # 데이터 수명주기 관리
│   └── health_check.py      # 시스템 상태 점검
├── scripts/                 # 스크립트
│   ├── install.sh           # 설치 스크립트
│   └── setup_launchagent.sh # 자동 시작 설정
└── tests/                   # 테스트 (1165개)
```

## 기술 스택

| 영역 | 기술 |
|------|------|
| STT | [mlx-whisper](https://github.com/ml-explore/mlx-examples) (Apple MLX) |
| 화자 분리 | [pyannote-audio](https://github.com/pyannote/pyannote-audio) 3.1 (CPU) |
| LLM | [EXAONE 3.5](https://huggingface.co/LGAI-EXAONE) 7.8B Q4 via [Ollama](https://ollama.com) |
| 임베딩 | [multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small) (MPS) |
| 벡터 DB | [ChromaDB](https://www.trychroma.com/) |
| 키워드 검색 | SQLite FTS5 |
| API | [FastAPI](https://fastapi.tiangolo.com/) + WebSocket |
| macOS UI | [rumps](https://github.com/jaredks/rumps) |

## 아키텍처 특징

- **100% 오프라인**: 모든 AI 모델이 로컬에서 실행, 외부 API 호출 없음
- **순차 모델 로드**: RAM 16GB 제한 내에서 피크 9.5GB 유지
- **서멀 관리**: 팬리스 MacBook Air에서도 안정적 실행 (2-job 배치 + 3분 쿨다운)
- **체크포인트 복구**: 파이프라인 중단 시 마지막 단계부터 재개
- **데이터 보안**: chmod 700, Spotlight 제외, localhost only

## 기여하기

[CONTRIBUTING.md](CONTRIBUTING.md)를 참고하세요.

## 라이선스

[MIT License](LICENSE)
