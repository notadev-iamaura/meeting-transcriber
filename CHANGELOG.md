# 변경 이력 (Changelog)

이 프로젝트의 모든 주요 변경 사항을 기록합니다.

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.0.0/)를 따르며,
버전 관리는 [시맨틱 버저닝(Semantic Versioning)](https://semver.org/lang/ko/)을 준수합니다.

---

## [미출시 (Unreleased)]

---

## [0.1.0] — 2026-03-06

### 1단계: 핵심 기반 구축 (설정, 파이프라인, 작업 큐)

#### 추가됨
- `config.yaml` 및 `config.py` — Pydantic 기반 설정 관리 시스템 도입. 모든 설정값의 단일 진실 공급원으로 YAML 파일 사용, 환경변수 오버라이드 지원
- `core/pipeline.py` — 8단계 순차 전사 파이프라인 오케스트레이터. STT → 화자분리 → 병합 → 교정 → 청크 → 임베딩 → 저장 → 요약 흐름 구현
- `core/job_queue.py` — SQLite 기반 작업 큐. 외부 메시지 큐 의존성 없이 단일 프로세스에서 작업 상태 관리
- `core/model_manager.py` — `ModelLoadManager` 뮤텍스 기반 모델 수명 관리. RAM 16GB 환경에서 피크 9.5GB 이하 유지를 위한 순차 모델 로드/언로드 보장
- `core/thermal_manager.py` — 팬리스 MacBook Air 대응 서멀 관리. 2-job 배치 처리 후 3분 쿨다운 패턴 구현
- `core/watcher.py` — watchdog 기반 폴더 감시. `audio_input/` 폴더에 파일 추가 시 파이프라인 자동 트리거

### 2단계: STT, 화자분리, 교정, 요약, 청킹, 임베딩 단계 구현

#### 추가됨
- `steps/transcriber.py` — mlx-whisper 기반 한국어 STT. Apple Silicon MLX 가속, `mlx-community/whisper-medium-mlx` 모델 사용
- `steps/diarizer.py` — pyannote-audio 3.1 기반 화자 분리. MPS 버그 회피를 위해 `device="cpu"` 강제 적용
- `steps/merger.py` — 전사 결과와 화자 분리 결과의 시간 겹침(overlap) 기반 병합
- `steps/corrector.py` — EXAONE 3.5 7.8B LLM을 통한 전사 오류 배치 교정
- `steps/chunker.py` — 화자 발화 그룹핑 기반 시맨틱 청크 분할. 최대 300토큰, 30초 간격 토픽 경계 처리
- `steps/embedder.py` — `intfloat/multilingual-e5-small`(384차원) MPS 가속 임베딩. ChromaDB 및 SQLite FTS5 저장 통합
- `steps/summarizer.py` — EXAONE LLM을 통한 회의 내용 AI 요약 생성
- JSON 체크포인트 시스템 — 각 파이프라인 단계의 중간 결과를 JSON으로 저장, 실패 시 마지막 완료 단계부터 재개 가능

### 3단계: 하이브리드 검색, RAG 채팅, 오디오 변환, Zoom 감지, 폴더 감시

#### 추가됨
- `search/hybrid_search.py` — RRF(Reciprocal Rank Fusion) 기반 하이브리드 검색. 벡터 검색(가중치 0.6) + SQLite FTS5(가중치 0.4), k=60, 상위 5개 결과 반환
- `search/chat.py` — RAG 채팅 구현. 검색 → 컨텍스트 구성 → EXAONE 답변 생성. 대화 이력 최근 3쌍 슬라이딩 윈도우 유지
- `steps/audio_converter.py` — ffmpeg 기반 오디오 포맷 변환. 다양한 입력 포맷(WAV, MP3, M4A 등)을 16kHz 모노 WAV로 정규화
- `steps/zoom_detector.py` — CptHost 프로세스 폴링 방식의 Zoom 회의 시작/종료 자동 감지

### 4단계: 보안, 라이프사이클, 상태 점검, API, WebSocket, 메뉴바, 웹 UI, 앱 진입점

#### 추가됨
- `security/secure_dir.py` — 데이터 디렉토리 권한 설정(chmod 700), macOS Spotlight 색인 제외(`/.noindex`) 처리
- `security/lifecycle.py` — 데이터 수명주기 관리. hot(활성) → warm(보관) → cold(아카이브) 단계별 정책 적용
- `security/health_check.py` — 시스템 전반 상태 점검. 디스크 공간, 모델 가용성, DB 무결성 등 확인
- `api/server.py` — FastAPI 앱 팩토리. 데몬 스레드에서 uvicorn 실행, rumps 메인 스레드와 공존
- `api/routes.py` — REST API 라우트 정의. 회의 목록, 상세 조회, 검색, 채팅, 작업 상태 등 엔드포인트
- `api/websocket.py` — WebSocket 기반 실시간 파이프라인 진행 상황 통신. 프론트엔드에 단계별 이벤트 전송
- `ui/menubar.py` — rumps 기반 macOS 메뉴바 앱. 처리 상태 실시간 아이콘 표시, 메인 스레드 점유
- `ui/web/index.html` — 회의록 목록 대시보드
- `ui/web/viewer.html` — 회의록 상세 뷰어. 화자별 색상 구분, 타임스탬프 표시
- `ui/web/chat.html` — AI 채팅 인터페이스
- `ui/web/style.css` — 웹 UI 스타일시트
- `ui/web/app.js` — 프론트엔드 JavaScript 로직. WebSocket 연결 및 실시간 업데이트 처리
- `main.py` — 앱 진입점. rumps 메인 스레드 + FastAPI 데몬 스레드 동시 실행, CLI 인자 처리(`--no-menubar`, `--port`, `--log-level`)
- `scripts/install.sh` — 통합 설치 스크립트. 의존성 확인, 모델 다운로드, 데이터 디렉토리 보안 설정 자동화
- `scripts/setup_launchagent.sh` — macOS LaunchAgent 등록. 로그인 시 자동 시작 설정
- `tests/` — pytest 기반 단위 테스트 1,165개. 각 모듈별 독립 테스트 커버리지 확보

### 5단계: 오디오 녹음 기능 (자동 Zoom 녹음)

#### 추가됨
- `steps/recorder.py` — ffmpeg AVFoundation 기반 macOS 오디오 녹음. BlackHole 2ch 가상 오디오 장치 자동 감지 및 시스템 오디오 캡처, 미설치 시 기본 마이크 폴백
- 녹음 파일 스테이징 흐름 — 녹음 중 `recordings_temp/`에 임시 저장, 완료 후 `audio_input/`으로 원자적 이동하여 FolderWatcher 트리거
- ffmpeg 안전 종료 절차 — stdin 'q' 신호 → graceful 타임아웃(10초) → SIGTERM → SIGKILL 순서로 프로세스 정리
- BlackHole 자동 감지 — `prefer_system_audio=true` 설정 시 AVFoundation 장치 목록을 스캔하여 BlackHole 장치를 우선 선택
- 녹음 REST API 엔드포인트 추가
  - `GET /api/recording/status` — 현재 녹음 상태 조회
  - `POST /api/recording/start` — 수동 녹음 시작
  - `POST /api/recording/stop` — 수동 녹음 정지
  - `GET /api/recording/devices` — 사용 가능한 오디오 장치 목록 조회
- Zoom 자동 녹음 연동 — `ZoomDetector`가 CptHost 프로세스 감지 시 `AudioRecorder.start_recording()` 자동 호출, 회의 종료 시 `stop_recording()` 호출
- 메뉴바 녹음 상태 표시 — 녹음 중 메뉴바 아이콘 실시간 업데이트
- WebSocket 녹음 이벤트 — `recording_started`, `recording_stopped` 이벤트 프론트엔드 전송
- 최대 녹음 시간 제한 — `recording.max_duration_seconds`(기본값 14400초, 4시간) 초과 시 자동 정지

### 6단계: 코드베이스 분석 및 개선 (4개 위원회)

#### 개선됨
- **아키텍처 위원회** — 전체 모듈 간 의존성 검토 및 계층 분리 명확화. `ModelLoadManager` 뮤텍스 패턴 일관성 강화, 체크포인트 복구 경로 안정화
- **안정성 위원회** — 예외 처리 누락 지점 보완. bare except 제거, 모든 예외에 구체적 타입 명시, 에러 전파 체계 일관화. ffmpeg 종료 시퀀스 엣지 케이스 처리
- **성능 위원회** — 임베딩 배치 크기 최적화, ChromaDB 쿼리 효율 개선, FTS5 인덱스 활용 쿼리 정비, 파이프라인 단계별 소요 시간 로깅 추가
- **UI/UX 위원회** — 웹 UI 반응성 개선. WebSocket 재연결 로직 안정화, 뷰어 페이지 화자 색상 일관성 유지, 채팅 입력창 UX 개선
- 테스트 커버리지 확대 — 1,165개 → 1,215개로 50개 테스트 추가. recorder, Zoom 감지, 하이브리드 검색, 채팅 RAG 경로 테스트 강화

#### 수정됨
- pyannote-audio 4.x 호환성 패치 — `Pipeline.from_pretrained()` 인증 파라미터를 `use_auth_token`에서 `token=`으로 수정
- mlx-whisper 0.4.x 호환성 패치 — 미구현 `beam_size` 파라미터 제거, greedy 디코딩만 사용하도록 수정
- ChromaDB 메타데이터 타입 제한 대응 — `datetime` 객체를 `str()` 변환 후 저장하도록 임베더 수정

### 7단계: LLM 백엔드 추상화 (Ollama + MLX 듀얼 백엔드)

#### 추가됨
- LLM 백엔드 추상화 레이어 — `corrector.py` 및 `summarizer.py`가 Ollama와 MLX 백엔드를 동일한 인터페이스로 사용할 수 있도록 백엔드 팩토리 패턴 도입
- MLX 백엔드 구현 — `mlx-lm` 라이브러리를 통해 EXAONE 3.5 7.8B 모델을 파이썬 프로세스 내에서 직접 로드. 별도 Ollama 서버 프로세스 불필요
- `config.yaml` LLM 백엔드 설정 추가
  - `llm.backend` — `"ollama"` 또는 `"mlx"` 선택 (기본값: `"ollama"`)
  - `llm.mlx_model_name` — MLX 백엔드 모델명 (`mlx-community/EXAONE-3.5-7.8B-Instruct-4bit`)
  - `llm.mlx_max_tokens` — MLX 백엔드 최대 생성 토큰 수 (기본값: 2000)
- 환경변수 `MT_LLM_BACKEND` — `config.yaml` 수정 없이 백엔드 전환 가능
- `pyproject.toml`에 `mlx-lm` 선택적 의존성 추가 — `pip install -e ".[dev]"` 시 자동 설치

#### 변경됨
- Ollama 백엔드가 기본값으로 유지 — 기존 사용자의 설정 변경 불필요
- `ModelLoadManager` — MLX 백엔드의 in-process 모델 로드를 뮤텍스 범위 내에서 관리하도록 확장. Ollama 백엔드는 외부 서버로 관리 위임
- `scripts/install.sh` — 하드웨어 감지(칩 종류 + RAM) 후 권장 백엔드 안내 메시지 추가
- `README.md` 및 `CLAUDE.md` — LLM 백엔드 선택 가이드, 하드웨어별 권장 사항, 설정 방법 문서화

---

[미출시 (Unreleased)]: https://github.com/notadev-iamaura/meeting-transcriber/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/notadev-iamaura/meeting-transcriber/releases/tag/v0.1.0
