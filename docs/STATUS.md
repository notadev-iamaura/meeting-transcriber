# 프로젝트 상태

- 기준일: 2026-08-22
- 기준 브랜치: `main`
- 시작 기준 커밋: `05682dd4703ad0e0bc873f85adad2ac79a52ea05`
- 최근 정리 wave: #41 → #38 → #39 → #40 → #42 → #43 → #44 → #45 → #46 → #47 → #48 → #52 → #53 모두 main 반영

## 현재 판단

이번 정리 wave 이후 프로젝트는 이전 평가에서 지적된 가장 큰 구조적 리스크를
상당 부분 해소한 상태입니다.

- 기본 테스트 프로파일은 native/MLX/Metal 의존 테스트를 명시 marker로 분리합니다.
- API 테스트는 `api-test`/`unit-test` 런타임 프로파일로 데스크톱 부작용을 줄입니다.
- `api/routes.py`는 app-state dependency helper, meetings batch, STT models,
  wiki/reindex, settings/user-settings, search/chat, meeting detail, system,
  uploads, recording router
  분리를 완료했습니다.
- `ui/web/spa.js`는 route view와 global shell controller 대부분을 feature module로 위임합니다.
- CI는 lint, mypy 타입 검사, Python 3.11/3.12 테스트, Swift compile gate를 통과한 PR만 main에 반영했습니다.
- consensus harness 문서와 CLI/test support가 main에 포함되어 다음 phase를 같은 방식으로 반복할 수 있습니다.
- native diagnostic gate는 `tests/native/test_preflight_native.py` smoke test를 통해
  실제 preflight 경로를 검증합니다.
- `.venv` 밖의 Python 캐시 산출물은 Git 추적 대상이 아닙니다.

## 완료된 주요 작업

### 로컬 우선 OpenAI 전사 선택 (2026-08-22)

- `stt.provider`의 기본값은 `local`이며 자동 cloud fallback은 없습니다.
- 설정 화면에서 macOS Keychain 기반 OpenAI API 키 등록/삭제와 기본 전사 처리 위치를
  선택할 수 있습니다. OpenAI 기본값은 한 번 동의하면 로컬로 되돌릴 때까지 이후 새
  전사에 적용됩니다. 키 원문은 설정 응답, YAML, DB, 체크포인트, 로그에 남기지 않습니다.
- OpenAI 기본 전사 전환은 loopback 서버, 등록된 키, 외부 업로드 동의를 모두 요구합니다.
- 전사가 완료된 회의 뷰어의 **다른 모델로 텍스트 변환하기…**는 현재 로컬 모델과
  `gpt-4o-transcribe-diarize`를 격리 A/B 작업으로 실행합니다. 기존 전사·요약·검색
  인덱스는 보존하고 파일마다 외부 전송 동의를 다시 받습니다.
- OpenAI 응답의 화자/시간 세그먼트는 단일 client-side 청크에서 pyannote를 우회합니다.
  여러 청크에서는 provider 화자 ID의 전역 일관성을 가정하지 않고 기존 로컬
  pyannote를 사용합니다.
- CI/회귀 검증은 mock HTTPS/Keychain/ffmpeg로 수행하며 실제 OpenAI 요청은 포함하지
  않습니다. 한국어 회의 정확도 우열은 독립적인 사람 정답 벤치마크 전까지 미확정입니다.

### Audio Admission Hardening (2026-08-18)

- 전사 최소 길이 기본값을 30초로 올리고 앱 녹음 조기 파기 기준도 30초로 맞췄습니다.
  품질 gate는 성공한 ffprobe duration과 16 kHz mono full-decode sample duration 중
  짧은 값을 사용하므로 잘린 파일, WebM preroll, AAC encoder padding을 보수적으로
  처리합니다. 정확히 30초는 통과하며 저장 미디어가 30초 미만일 때만 길이 정책으로
  거부합니다.
- `MEDIA_INVALID`만 복구 가능한 `audio_quarantine/`으로 이동합니다. 도구 부재·timeout,
  writable/growing source, symlink·경로 차단은 원본을 보존하고 queue/STT만 막습니다.
  quarantine 이동은 no-follow, 동일 파일시스템, 고유 목적지, identity 재검증 계약을
  사용해 외부 target 이동과 동일명 덮어쓰기를 방지합니다.
- watcher는 생성·이동·수정 이벤트와 bounded readiness timeout을 사용합니다. 레거시
  queued/failed 작업은 실행 의도를 durable hold payload에 저장한 뒤 재감사하여 입력
  오류가 UI `failed` 상태로 반복되지 않게 합니다.
- 기존 DB 작업의 미디어 거부는 `claim → exact quarantine move → CAS finalize` journal로
  처리합니다. claim과 파일 이동 또는 DB 정리 사이 앱이 종료돼도 startup recovery가
  source/quarantine inode를 대조해 멱등하게 완료하며, 모호한 상태에서는 양쪽을 보존합니다.
- 브라우저 업로드는 raw `base_dir`부터 no-follow로 입력 디렉터리를 열고, 예측 불가능한
  0600 temp inode를 완전히 기록·fsync한 뒤 same-directory hardlink로 무덮어쓰기
  publish합니다. API는 직접 queue에 넣지 않으며 publish된 파일도 watcher의 동일 gate를
  통과해야 합니다.
- Pipeline/Transcriber 최종 장벽과 retry/force/re-transcribe/STT A/B/batch preflight가
  같은 typed gate를 사용합니다. API는 media 422, busy 409, infra 503, security 400으로
  구분하며 비수락 시 상태·검색 인덱스·기존 산출물을 먼저 변경하지 않습니다.
- 재전사는 token CAS와 `claimed → staging → purging → committing` durable phase를
  사용합니다. 앱 종료 또는 파일/인덱스 작업 실패 시 startup recovery가 rollback하거나
  commit을 이어서 완료합니다. staging·rollback·checkpoint 조회와 recovery marker 기록은
  열린 root/file descriptor 기준으로 수행합니다. batch queue mutation은 전체 preflight 뒤
  단일 SQLite transaction으로 수행합니다.
- full-decode timeout은 `audio_quality.decode_timeout_*` 설정으로 길이에 비례해 계산하며,
  secure file identity와 gate 설정이 같은 ACCEPT 결과만 bounded process-local LRU에서
  재사용합니다.

오디오 admission 집중 검증:

```bash
pytest tests/test_audio_quality.py tests/test_audio_quality_real_media.py tests/test_audio_converter.py tests/test_quarantine.py tests/test_watcher.py tests/test_audio_admission_recovery.py tests/test_pipeline.py tests/test_transcriber.py tests/test_diarizer.py tests/test_diarization_worker.py tests/test_orchestrator.py tests/test_ab_test_api.py tests/test_ab_test_runner.py tests/test_routes.py tests/test_routes_home_dashboard.py tests/test_routes_meetings_batch.py tests/test_routes_reindex.py tests/test_job_queue.py tests/test_config.py -q
```

### Backend Reliability Hardening (2026-06-20)

- `/api/chat` 응답에 `llm_called`, `grounding_status`, `repair_actions`를 추가했습니다.
  RAG 검색 실패 또는 검색 결과 0건이면 LLM을 호출하지 않고 근거 없음/검색 오류 응답으로 종료합니다.
- 스트리밍 채팅도 같은 grounding 계약을 따릅니다. 근거가 없으면 token stream을 열지 않고
  `grounding`/`done` 이벤트로 종료합니다.
- Wiki router `source_type="both"` 응답은 RAG와 Wiki 근거를 통합 집계합니다.
  RAG 검색 0건이어도 Wiki 근거가 있으면 전체 `grounding_status="grounded"`로 응답합니다.
- `DELETE /api/meetings/{meeting_id}`와 `POST /api/meetings/{meeting_id}/re-transcribe`는
  DB 삭제, 체크포인트 삭제, job 리셋 전에 ChromaDB/SQLite FTS5 검색 인덱스를 먼저 purge합니다.
  purge 실패 시 기존 회의 레코드와 산출물을 보존하고 500으로 중단합니다.
- `pipeline.skip_llm_steps=True` 또는 메모리 부족으로 correct 단계가 스킵되어도
  `merge.json` 기반 pass-through `correct.json`을 저장합니다. 이후 resume, chunk, reindex는
  항상 `CorrectedResult` 계약을 사용할 수 있습니다.
- 온디맨드 LLM 후처리(`run_llm_steps`)는 skip으로 생성된 pass-through `correct.json`을
  실제 LLM 보정으로 갱신하고, 기존 chunk/embed 인덱스가 있던 회의는 LLM 결과 기준으로
  chunk/embed를 자동 재생성합니다. 재생성 중 실패하면 `chunk`/`embed` 완료 마커를 제거하고
  상태를 `failed`로 저장해 stale 완료 표시를 방지합니다.
- ChromaDB 또는 SQLite FTS5 저장 중 하나라도 실패하면 회의별 양쪽 검색 인덱스를 purge해
  stale/partial index 대신 명시 실패 상태로 수렴합니다.
- ChromaDB/FTS5 purge는 삭제 전 양쪽 저장소 접근성을 먼저 점검합니다. 삭제 도중 한쪽만
  성공한 드문 부분 실패나 재색인 저장 실패로 기존 인덱스가 제거된 경우
  `checkpoints/{meeting_id}/reindex_required.json` marker를 남겨 복구 필요 상태를 보존합니다.
- `ModelLoadManager.unload_if_current(name)`를 추가해 LLM 후처리 체인 종료 시 현재 모델이
  `exaone`일 때만 조건부로 언로드합니다.
- MLX LLM 백엔드는 `ThreadBoundLLMBackend` wrapper를 통해 모델 로드, `chat`,
  `chat_stream`, `cleanup`을 단일 worker thread에 고정합니다. Gemma 4 E4B에서
  worker thread 첫 generation 시 발생하던 `There is no Stream(gpu, 1) in current thread`
  오류를 방지합니다.
- direct pyannote 경로도 worker 경로처럼 실제 선택된 diarization `output_mode`를 결과
  메타데이터에 저장합니다.
- `scripts/benchmark_ai_pipeline.py`를 추가해 STT/VAD/화자분리/교정/요약 단계의 시간,
  RSS/가용 메모리/swap/MLX 메모리와 품질 지표를 로컬 JSON 리포트로 남길 수 있습니다.
- 기본 화자분리는 `pyannote/speaker-diarization-community-1` + `output_mode=auto`
  로 전환했습니다. exclusive 출력이 있으면 우선 사용하고, pyannote는 계속 CPU 강제입니다.
- `HF_HUB_OFFLINE` 또는 `TRANSFORMERS_OFFLINE` 상태에서 pyannote 캐시가 불완전하면
  화자분리 모델 로드까지 진행하지 않고 사전 검증 단계에서 명확히 실패합니다.
- 화자 수는 사용자가 직접 기억해 입력하지 않아도 되도록 내부 기본 2~4 bounded auto를
  유지합니다. 완전 자동(`min_speakers/max_speakers=null`)은 가능하지만 동일 샘플에서
  속도 이득이 없어 기본값으로 두지 않았습니다.
- 화자분리용 긴 무음 압축을 추가했습니다. STT 원본 타임라인은 유지하고, pyannote 입력
  사본만 조건부 압축한 뒤 결과 시간을 원본으로 복원합니다. 기본 임계값은 60초 이상 및
  전체 5% 이상 절약입니다.
- LLM 교정 기본값은 `changed_only`로 전환했습니다. 줄 밀림/병합/파괴적 축약 guard를
  통과한 수정만 반영하고, guard 폐기가 많은 배치는 full 모드로 1회 fallback합니다.
- 자동 전사/요약은 안전 점검을 통과한 경우에만 실행합니다. 기본값은 1회 1건 처리이며,
  HF offline + pyannote 캐시 누락 또는 `thermal.batch_size > 2` / `cooldown_seconds < 180`
  조합에서는 실행을 보류하고 API 결과의 `errors`에 이유를 남깁니다.
- `stt.word_timestamps=false` 기본값 전환은 보류했습니다. 단어 timestamp가 저장 산출물에
  직접 노출되지는 않지만 STT 세그먼트 경계를 보정하고, 이후 화자 병합 overlap과
  UNKNOWN 비율에 영향을 줄 수 있기 때문입니다. 기본값 변경은 `true/false` A/B에서
  시간/메모리 이득과 segment drift, temporal coverage, merge UNKNOWN, speaker distribution,
  reference CER/WER를 검증한 뒤 별도 판단합니다.
- 전사 단계가 끝나면 전체 파이프라인 완료 전에도 `checkpoints/{meeting_id}/transcribe.json`
  기반 전사 초안을 Viewer에서 볼 수 있습니다. `/api/meetings/{id}/transcript`는
  `corrected → correct → merge → transcribe` 순서로 산출물을 찾고,
  `source_stage`와 `readonly`를 반환합니다. `merge`/`transcribe`는 읽기 전용이며,
  초안은 UI에서 검색/복사/다운로드만 허용하고 인라인 편집/모두 바꾸기를 차단합니다.
  또한 `correct`/`corrected` 산출물이 먼저 생겼더라도 job 상태가 `completed`가 아니면
  전사문 PUT/replace는 409로 거부되어 처리 중 결과를 수정하지 않습니다.
- 조기 전사 초안 노출을 안전하게 하기 위해 meeting_id의 dot segment를 차단하고,
  transcript JSON cache를 `mtime_ns + size` 기준으로 갱신하며, STT `transcribe.json`
  체크포인트 저장을 원자적 JSON 쓰기로 변경했습니다.

### Frontend Architecture

`ui/web/spa.js`에서 다음 모듈을 분리했습니다.

- `api-client.js`
- `list-panel.js`
- `command-palette.js`
- `settings-view.js`
- `viewer-view.js`
- `chat-view.js`
- `wiki-view.js`
- `ab-test-view.js`
- `search-view.js`
- `empty-view.js`
- `global-resource-bar.js`
- `bulk-action-bar.js`
- `theme-controller.js`
- `mobile-drawer.js`
- `shortcut-controller.js`

회의 목록 사이드바는 `/api/meetings?offset=...&limit=50` 페이지 조회를 사용해
최초 50건만 렌더링하고, 목록 하단 스크롤 또는 "더 보기" 액션으로 50건씩 추가 로드합니다.
장기 사용자의 수백~수천 건 회의에서 초기 DOM/렌더 비용이 한 번에 커지지 않도록 제한합니다.

각 모듈은 `window.Meeting*` factory boundary를 통해 `spa.js`에 주입되며,
기존 `window.SPA.*` 공개 계약은 유지합니다.

React/TypeScript 점진 전환을 준비하기 위해 legacy `window.*` 전역 노출면을
`tests/harness/test_frontend_boundaries.py` allowlist로 고정했습니다. 신규
React island 코드는 `window.*`/`globalThis` 직접 의존을 추가하지 않는 규약을
`docs/design-decisions/frontend-react-migration.md`와 `CLAUDE.md`/`AGENTS.md`에
명시했습니다.
React/Vite asset-only scaffold를 `ui/web-src`에 추가했습니다. Vite는 dedicated
`ui/web-src/index.html`을 entry로 사용하고, `base: "/app-assets/"`와
`build.outDir: "../web-dist"`로 산출물을 `ui/web-dist`에 생성합니다. FastAPI는 이
build output이 존재할 때만 `/app-assets`에서 no-cache 정적 파일로 서빙합니다.
기존 `/static` legacy asset과 `/app` SPA catch-all은 그대로 유지되며,
legacy `ui/web/index.html`에는 React asset을 주입하지 않았습니다. `ui/web-dist`는
generated artifact로 ignore하고 launcher source bundle/validator exclusion 목록에서도
제외합니다.

bulk actions 시각 회귀 기준 이미지 6종은 2026-08-11 현재 UI로 재캡처했습니다.
5월 기준 이미지 이후 반영된 빈 상태 UX와 `/app/setup` 준비 상태 내비게이션을 포함하며,
라이트/다크 데스크톱과 모바일 화면을 직접 검토했습니다. 픽셀 비교 재현성을 위해 dev
extra의 Playwright를 `1.60.0`, pytest-playwright를 `0.8.0`으로 고정하고 Chromium 148이
아닌 브라우저로 bulk actions visual gate를 실행하면 기준 이미지 비교 전에 명확히
실패하도록 런타임 가드를 추가했습니다. Playwright를 올릴 때는 브라우저 렌더링을 검토한
뒤 이 버전 계약과 기준 이미지 6종을 함께 갱신해야 합니다.

`/app/setup` 준비 상태 화면을 추가했습니다. 이 화면은
`GET /api/setup/readiness`만 호출해 데이터 디렉토리, Python 런타임 후보, ffmpeg,
HuggingFace 토큰, BlackHole/Aggregate 장치, 활성 STT 모델 상태를 표시하며,
설치/권한 변경/모델 다운로드
같은 setup mutation은 수행하지 않습니다.
각 점검 항목은 표시 전용 `actions` metadata를 포함할 수 있습니다. 웹 UI는
HuggingFace `https://huggingface.co/...` 링크, `/app/settings` 내부 이동, 터미널
명령 예시 텍스트만 렌더링하며 action을 자동 실행하지 않습니다.

`ui/launcher.py`를 추가해 경량 `.app` 런처가 서버 시작 전에 사용할 read-only
preflight/command 계약을 분리했습니다. 이 모듈은 `main.py --no-menubar` 실행 argv,
cwd, 비밀 없는 환경변수 override, `/app` 및 `/app/setup` URL을 JSON으로 반환하지만
프로세스를 시작하거나 설치/권한 변경/네트워크/모델 작업을 수행하지 않습니다.
JSON의 `runtime` 필드는 선택된 Python source(`explicit`, `project_venv`,
`managed_venv`, `current_interpreter`)와 후보별 존재/파일/실행권한 여부를 함께 노출해
최초 설정 마법사와 네이티브 런처가 관리형 venv 상태를 같은 계약으로 판단할 수
있게 합니다. 이 판정도 파일 메타데이터와 실행 권한만 확인하며 venv 생성,
패키지 설치, Python 실행은 하지 않습니다.
서버 실행에 쓰는 Python path는 venv의 `bin/python` symlink를 보존합니다. symlink를
base framework interpreter로 resolve하면 venv site-packages를 잃을 수 있기 때문입니다.
생성된 `.app` wrapper는 지원되는 환경에서 `/usr/bin/arch -arm64`로 이 Python을 실행해
LaunchServices/Rosetta가 arm64 wheel을 x86_64 Python으로 로드하는 mismatch를 피합니다.
선택 후보가 없거나 실행 권한이 없거나 `current_interpreter` fallback을 쓰는 경우에는
Python 버전 확인, 프로젝트 `.venv`, 관리형 venv 준비 명령 예시를
`python_runtime.actions`로 표시합니다. 이 값은 placeholder 기반 텍스트 안내일 뿐이며
readiness API나 웹 UI가 venv 생성, `pip install`, 네트워크 접근을 실행했다는 의미가
아닙니다. 실행 권한 누락은 `python_runtime` check를 실패로 표시하지만, 이 check는
계속 advisory이므로 top-level `configured`/`ready` 판정은 기존 설치 필수 조건을 유지합니다.
런처가 서버를 시작할 때는 `MT_LAUNCHER_PYTHON_SOURCE`,
`MT_LAUNCHER_PYTHON_EXECUTABLE`, `MT_LAUNCHER_PROJECT_DIR` 비밀 없는 handoff 값을
environment override로 전달합니다. `/api/setup/readiness`의 `python_runtime` check는
이 handoff가 유효하면 `runtime_scope=launcher_handoff`로 표시하고, 없거나 불완전하면
서버 프로세스 안에서 런처 후보를 재구성해 `runtime_scope=server_reconstructed`로
표시합니다. 두 경우 모두 실제 실행 중인 `sys.executable`과 선택 후보의 일치 여부를
함께 보여주지만, top-level `configured`/`ready` 판정은 기존 설치 필수 조건을 유지합니다.
`scripts/build_launcher_app.py`는 이 계약을 사용해 unsigned local `.app` 번들을
지정 output 디렉토리에 생성합니다. 빌드 스크립트는 앱을 실행하지 않으며, 생성된
bundle은 `Info.plist`, `Contents/MacOS/<executable>`,
`Resources/launcher-metadata.json`으로 구성됩니다.
생성된 executable은 metadata와 같은 host, port, log path를 런타임 spec에 전달해
non-default 로컬 포트 번들도 빌드 시 지정한 endpoint를 그대로 사용합니다.
새 서버 프로세스를 시작하는 경로에서는 child stdout/stderr를 같은 로컬 launcher log에
append합니다. 이 로그는 사용자 로컬 파일에만 남으며 원격 수집되지 않습니다. 이미 떠 있는
서버를 여는 경로나 wrapper preflight 이전 실패까지 모두 포착한다는 의미는 아닙니다.
직접 `.app` builder도 output 디렉토리 symlink, 파일형 output 디렉토리, target `.app`
symlink, non-directory overwrite를 거부해 산출물이 지정 위치 밖으로 빠져나가지 않게 합니다.
생성된 executable은 staging bundle에서 `/bin/bash -n` syntax 검증을 통과한 뒤에만
최종 `.app` 경로로 설치됩니다. 검증 실패 시 partial bundle을 남기지 않으며,
`--force` 교체 대상의 기존 bundle도 보존합니다.
명시적으로 `--bundle-source`를 사용하면 `Contents/Resources/project`에 런타임
소스 스냅샷을 포함합니다. 이 스냅샷은 allowlist 기반으로 `main.py`, `config.py`,
`config.yaml`, `pyproject.toml`, 런타임 패키지 디렉토리(`api`, `core`, `steps`,
`search`, `security`, `ui`)만 복사하며 `.env*`, `.git`, `.venv`, 캐시, 테스트,
벤치마크, build/dist/output/state, 모델/오디오/DB 산출물, symlink escape는 제외합니다.
번들 내부 `config.yaml`은 원본을 수정하지 않고 복사본에서 HuggingFace 토큰 값과
토큰 안내 comment를 제거합니다.
생성된 executable은 앱 이동을 고려해 실행 시점에 `Contents/Resources/project`를
계산하고, 존재할 때만 이를 `PROJECT_DIR`로 사용합니다.
`scripts/validate_launcher_app.py`는 생성된 bundle을 read-only로 검사해
Info.plist 계약, executable 존재/권한과 bash syntax, launcher metadata, secret marker 미노출,
optional `codesign --verify` 결과를 JSON으로 보고합니다. unsigned local prototype은
local readiness 통과와 distribution readiness 미충족으로 구분합니다. validator는
serialized `launcher-metadata.json` 안의 `MT_LAUNCHER_*` handoff key와 `runtime`
metadata가 서로 일관적인지도 확인합니다. 이 검사는 metadata coherence 검증이며 실제
`.app` 실행 시점의 handoff 값을 증명하지는 않습니다. runtime 후보 목록은 각 후보의
`id`, `path`, `exists`, `is_file`, `is_executable`, `selected` shape와 정확히 하나의
selected 후보가 top-level runtime source/path와 일치하는지도 serialized metadata 안에서만
검사합니다. source bundle이 활성화된 경우
필수 런타임 소스와 제외 규칙도 검사하며, 검증 중 앱 실행, 서명, 공증, 네트워크,
파일 mutation은 하지 않습니다.
`CFBundleExecutable`은 `Contents/MacOS` 아래 단일 파일명만 허용하며, 절대경로나
`..`가 포함된 값으로 bundle 밖 파일을 stat/read/bash-probe하지 않습니다.
`scripts/build_launcher_dmg.py`는 local_ready를 통과한 unsigned `.app`만
`hdiutil create -format UDZO`로 unsigned local DMG에 패키징합니다. 산출물은 실제
일반 파일이고 non-empty인지 확인하며, `.app` 내부 출력, symlink/디렉토리 overwrite,
앱 실행, 서명, 공증, 네트워크, 설치 작업은 거부합니다. JSON 출력은 command/path/volume에
secret marker가 있으면 redaction합니다. 이 DMG는 distribution_ready를 의미하지 않으며
서명/공증은 별도 단계로 남아 있습니다.
`scripts/build_release_manifest.py`는 생성된 `.app`과 `.dmg`를 read-only로 검사해
unsigned local release manifest를 출력합니다. `.app`는 `validate_launcher_app(...,
check_codesign=True)`로 검증하고, `.dmg`는 존재하는 일반 non-empty 파일인지 확인합니다.
manifest에는 generated timestamp, artifact path/type/byte size/SHA-256, app file count,
local_ready/distribution_ready, codesign summary가 포함됩니다. unsigned app은
local_ready=true, distribution_ready=false 상태로 허용되며, DMG mount/open, app 실행,
서명/공증, 네트워크, 설치 작업은 수행하지 않습니다.
`scripts/build_unsigned_release.py`는 위 세 단계를 조합해 지정 output 디렉토리 안에
`Recap.app`, `Recap.dmg`, `Recap.release-manifest.json`을 생성합니다. 이 스크립트는
새 packaging semantics를 만들지 않고 Python API(`build_launcher_app`,
`build_launcher_dmg`, `build_release_manifest`)만 호출합니다. output 디렉토리 symlink,
산출물 symlink/디렉토리, `--force` 없는 기존 산출물은 사전에 거부하며, 결과 JSON은
`release_type=unsigned_local`, `local_ready`, `distribution_ready`를 명시합니다.
앱 실행, 서버 실행, DMG attach/open, 서명/공증, 네트워크, 설치 작업은 하지 않습니다.

### Backend/API

- `api/dependencies.py`로 FastAPI `app.state` 접근을 모았습니다.
- `api/routers/meetings_batch.py`로 batch action router를 분리했습니다.
- `api/routers/stt_models.py`로 STT 모델 관리 API를 분리했습니다.
- `api/routers/wiki.py`와 `api/routers/reindex.py`로 지식베이스/재색인 API를 분리했습니다.
- `api/routers/settings.py`와 `api/routers/user_settings.py`로 설정/프롬프트/용어집 API를 분리했습니다.
- `api/routers/search_chat.py`로 검색/RAG 채팅 API를 분리했습니다.
- `api/routers/meeting_detail.py`로 단일 회의 상세/전사/요약/오디오 API를 분리했습니다.
- `api/routers/setup_readiness.py`로 최초 설정 마법사용
  `GET /api/setup/readiness` API를 추가했습니다. 이 endpoint는 데이터 디렉토리,
  ffmpeg, HuggingFace 토큰 설정 여부, BlackHole/Aggregate 장치, 활성 STT 모델의
  read-only 상태만 반환하며 설치, 권한 보정, 네트워크 호출, 모델 로드를 수행하지 않습니다.
  웹 UI `/app/setup`도 이 read-only 계약만 사용합니다. 구조화된 `actions`는
  안내 표시 전용이며 외부 링크는 HuggingFace 도메인으로 제한됩니다.
- `api/routers/system.py`, `api/routers/uploads.py`, `api/routers/recording.py`로
  시스템 상태/대시보드/업로드/녹음 API를 분리했습니다.
- `api/server.py`는 router 등록과 dependency wiring을 더 명확히 갖습니다.
- 관련 테스트는 `tests/test_api_dependencies.py`, `tests/test_server.py`,
  `tests/test_routes_meetings_batch.py`에 반영되어 있습니다.

### Runtime, CI, Docs

- model/pipeline runtime gate와 테스트 프로파일을 정리했습니다.
- CI는 기본 안정 gate, UI bulk actions gate, mypy 타입 검사 gate를 구분합니다.
- Ruff는 개발 extra와 CI 모두 `0.15.13`으로 고정해 로컬/CI 포맷 결과의 버전별 변동을
  차단합니다. 포맷터 버전 업그레이드는 전체 저장소 diff를 확인하는 별도 변경으로 진행합니다.
- README, PR template, AGENTS.md, 평가 문서를 최신 정책에 맞췄습니다.
- `harness/*`와 `docs/agentic-ops/*`가 main에 포함되어 consensus 기반 작업 흐름을 지원합니다.
- 데이터 디렉토리 보안 설정의 Time Machine 제외 명령은
  `security.timemachine_exclusion_timeout_seconds` 기본 0.5초 안에 끝나지 않으면
  경고만 남기고 앱 시작을 계속합니다. 권한 설정, Spotlight 제외, `.gitignore`
  생성은 기존처럼 즉시 적용됩니다.
- `scripts/measure_startup.py`는 임시 `MT_BASE_DIR`와 임시 포트로
  `main.py --no-menubar` 실제 경로를 실행해 `/api/health`와 `/app` 양쪽 200
  응답까지의 콜드 스타트를 측정합니다. 기본 목표는 3초입니다.
- `HybridSearchEngine`과 `ChatEngine`은 FastAPI startup에서 만들지 않고
  `/api/search` 또는 `/api/chat` 첫 요청 시점에 지연 초기화합니다. `/api/health`
  및 UI shell 응답은 검색/Chat 엔진 생성 없이 처리되며, 생성 실패는 해당 API에서
  503으로 반환됩니다.
- `ui/launcher.py`는 향후 경량 `.app`가 사용할 서버 시작 전 계약입니다.
  `/api/setup/readiness`는 서버 기동 후 최초 설정 화면의 진단 계약으로 유지됩니다.
- `scripts/build_launcher_app.py`는 `ui.launcher` 계약을 소비하는 unsigned local
  `.app` prototype builder입니다. `scripts/validate_launcher_app.py`는 이 산출물의
  구조, optional bundled source 계약, optional codesign readiness를 read-only로
  검사합니다. `scripts/build_launcher_dmg.py`는 local_ready `.app`을 unsigned local
  DMG로 감쌀 뿐이며, `scripts/build_release_manifest.py`는 `.app`/`.dmg`의 hash,
  size, local/distribution readiness를 기록합니다. `scripts/build_unsigned_release.py`는
  이 단계들을 한 번에 조합하는 unsigned_local 산출물 builder입니다. 서명/공증과
  distribution-ready release는 아직 별도입니다.
- 최초 설정 readiness 계약은 `docs/design-decisions/setup-readiness-api.md`에
  기록했습니다. `/api/health`는 계속 liveness 전용이며 readiness 진단을 호출하지 않습니다.

## 권장 검증 게이트

일반 변경:

```bash
ruff check .
ruff format --check .
mypy config.py api core steps search ui security --no-error-summary
pytest tests/ -v --tb=short
pytest -m harness -q
```

API/router 변경:

```bash
pytest tests/test_setup_readiness.py tests/test_routes_setup_readiness.py -q
pytest tests/test_api_dependencies.py tests/test_server.py tests/test_routes_meetings_batch.py tests/test_routes_stt_models.py tests/test_routes_reindex.py tests/test_user_settings_api.py tests/test_user_settings_e2e.py tests/test_security_fixes.py -q
pytest tests/wiki/test_routes.py tests/wiki/test_routes_phase2.py tests/wiki/test_routes_backfill.py tests/wiki/test_rag_unchanged.py tests/wiki/test_routes_chat_router.py -q
pytest tests/test_routes.py -q
```

Frontend shell/view 변경:

```bash
pytest tests/test_server.py -q
node --check ui/web/spa.js
node --check ui/web/viewer-view.js
pytest tests/harness/test_frontend_boundaries.py -q
pytest -m ui tests/ui/integration/test_spa_overhaul_integration.py -q
```

Setup readiness UI 변경:

```bash
node --check ui/web/spa.js
pytest tests/harness/test_frontend_boundaries.py -q
pytest -m ui tests/ui/integration/test_spa_overhaul_integration.py -k "setup_route or t301" -q
pytest tests/test_setup_readiness.py tests/test_routes_setup_readiness.py -q
```

Launcher/setup preflight 변경:

```bash
.venv/bin/python -m py_compile scripts/build_unsigned_release.py scripts/build_release_manifest.py scripts/build_launcher_app.py scripts/validate_launcher_app.py scripts/build_launcher_dmg.py ui/launcher.py
.venv/bin/python -m pytest tests/test_build_unsigned_release.py tests/test_build_release_manifest.py tests/test_build_launcher_dmg.py tests/test_validate_launcher_app.py tests/test_build_launcher_app.py tests/test_launcher.py tests/test_native_window.py tests/test_setup_launchagent.py tests/test_install.py -q
.venv/bin/python -m pytest tests/test_setup_readiness.py tests/test_routes_setup_readiness.py -q
bash -n scripts/install.sh && bash -n scripts/setup_launchagent.sh
```

Fixed-port bulk actions UI tests는 순차 실행합니다.

```bash
pytest -m ui tests/ui/behavior/test_bulk_actions_behavior.py -q
pytest -m ui tests/ui/a11y/test_bulk_actions_a11y.py -q
pytest -m ui tests/ui/visual/test_bulk_actions_visual.py -q
```

환경 의존 gate는 명시적으로 실행합니다. CI에서는 PR required gate로 묶지 않고,
`workflow_dispatch` 또는 주간 schedule diagnostic gate로 운용합니다.

```bash
pytest -m e2e tests/test_e2e_edit_playwright.py -v
pytest -m ui tests/ui/ -v
pytest -m native tests/ -v
```

## 알려진 우선 과제

1. 부채 마커를 작은 PR 단위로 줄입니다. 현재 관측 기준은 `type: ignore` 23건,
   `noqa` 137건, 빈 `pass` 37건, `TODO/FIXME/HACK/XXX` 2건,
   `pragma: no cover` 2건입니다. 우선순위는 내부 타입 예외, 좁힐 수 있는
   `BLE001`, 의도가 불명확한 빈 `pass`입니다.
2. `ui/web/style.css`를 component CSS로 계속 나눕니다. 완료: bulk actions,
   A/B test, Wiki, recording HUD, settings/STT model UI. 다음 후보는 viewer,
   command palette, layout shell입니다.
3. native marker 대상 테스트는 CI required gate가 아닌 manual/scheduled diagnostic
   gate로 운용합니다. 현재 smoke test는 preflight subprocess 경로를 검증하며,
   실제 장치/Metal 이상 여부를 주간 점검 기준으로 봅니다.
4. STT 누락/환각 개선은 `core/stt_quality_metrics.py`와
   `scripts/evaluate_stt_quality.py` 기반 metric harness로 진행합니다.
   다음 단계는 실제 회의 reference interval fixture를 추가해 baseline 리포트를
   `docs/BENCHMARK.md`에 반영하는 것입니다.
