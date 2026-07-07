# 최초 설정 Readiness API 결정

**상태**: API/UI 연결 완료
**최종 업데이트**: 2026-07-07

---

## 결정 요약

경량 런처 `.app`와 최초 설정 마법사는 앱 내부의 read-only HTTP 계약을 재사용한다.
첫 계약은 `GET /api/setup/readiness`이며, 설치나 권한 보정 없이 현재 로컬 환경의
준비 상태만 반환한다.

이 endpoint는 `/api/health`를 대체하지 않는다. `/api/health`는 계속 서버 생존만
확인하는 liveness endpoint이고, readiness는 마법사용 진단 정보다.

웹 UI의 `/app/setup` 화면도 같은 endpoint만 호출한다. 화면의 새로고침은 GET을
반복할 뿐이며, 설치, 권한 변경, 모델 다운로드, 오디오 장치 생성 같은 mutation은
설정 화면이나 별도 셋업 절차로 분리한다.
각 점검 항목은 표시 전용 `actions` metadata를 가질 수 있다. 이 값은 외부 링크,
내부 설정 화면 이동, 터미널 명령 예시를 구조화해 보여주기 위한 것이며 API나 UI가
대신 실행하지 않는다.

서버 시작 전 경량 `.app` 런처는 `python -m ui.launcher` 계약을 사용할 수 있다.
이 계약은 `main.py --no-menubar` 실행 argv, 작업 디렉토리, 비밀 없는 환경변수
override, `/app` 및 `/app/setup` URL을 JSON으로 반환한다. 런처 preflight는
프로젝트 디렉토리, `main.py`, Python 실행 파일 존재/실행 권한, loopback host/port
형식만 read-only로 확인한다.
`runtime` metadata는 선택된 Python source(`explicit`, `project_venv`,
`managed_venv`, `current_interpreter`)와 후보별 존재/파일/실행권한 여부를 노출해
관리형 venv 상태를 설명할 수 있게 한다. 이 판정은 파일 메타데이터와 실행 권한만
확인하며 venv 생성, 패키지 설치, Python 실행, `uv`/`pip` 실행은 하지 않는다.
서버를 실행할 Python path는 venv의 `bin/python` symlink를 보존해 기록하고 실행한다.
이를 base framework interpreter로 resolve하면 venv site-packages를 잃을 수 있기 때문이다.
생성된 `.app` wrapper는 지원되는 환경에서 `/usr/bin/arch -arm64`로 이 Python을 실행해
LaunchServices/Rosetta가 arm64 wheel을 x86_64 Python으로 로드하는 mismatch를 피한다.
서버가 기동된 뒤의 환경 진단은 계속 `GET /api/setup/readiness`가 담당한다.
런처가 서버를 시작할 때는 `MT_LAUNCHER_PYTHON_SOURCE`,
`MT_LAUNCHER_PYTHON_EXECUTABLE`, `MT_LAUNCHER_PROJECT_DIR`를 비밀 없는 environment
override로 넘긴다. 이 값은 런처가 선택한 Python source/path를 setup wizard에 설명하기
위한 진단 handoff이며, 서버가 프로세스를 다시 spawn하거나 설정을 바꾸는 데 사용하지
않는다.
이 endpoint의 `python_runtime` check는 유효한 handoff가 있으면
`runtime_scope: "launcher_handoff"`로 보고한다. handoff가 없거나 source가 허용 목록 밖이거나
source/path 중 하나가 빠진 경우에는 서버 프로세스 안에서 같은 런처 후보 선택 계약을
재구성하고 `runtime_scope: "server_reconstructed"`로 보고한다. 두 경우 모두 선택 후보와
현재 실행 중인 `sys.executable` 일치 여부를 함께 보고한다. 이 check는 top-level
`configured`/`ready`를 새로 막지 않는다.

`scripts/build_launcher_app.py`는 위 계약을 소비하는 unsigned local `.app` 번들을
생성한다. 빌드 단계는 `Info.plist`, `Contents/MacOS/<executable>`,
`Resources/launcher-metadata.json`만 지정 output 디렉토리 아래에 쓰고 앱을 실행하지
않는다. 생성된 executable은 사용자가 앱을 열 때 기존 loopback 서버를 먼저 확인하고,
없을 때만 `main.py --no-menubar`를 시작한 뒤 `/app/setup`을 연다. executable의
runtime spec은 metadata에 기록된 같은 host, port, log path를 사용하므로 non-default
로컬 포트 번들도 다른 포트로 되돌아가지 않는다.
새 서버 프로세스를 시작하는 경로에서는 child stdout/stderr를 같은 로컬 launcher log에
append한다. 이 로그는 사용자의 로컬 파일일 뿐 원격 수집되지 않으며, 이미 떠 있는 서버를
여는 경로나 wrapper preflight 이전 실패를 모두 포착한다는 의미는 아니다.
직접 `.app` builder도 output 디렉토리 symlink, 파일형 output 디렉토리, target `.app`
symlink, non-directory target overwrite를 거부해 산출물이 지정 output 디렉토리 밖으로
escape하지 않게 한다.
생성된 executable은 staging bundle 안에서 `/bin/bash -n` syntax 검증을 통과한 뒤
최종 `.app` 경로로 이동한다. 검증 실패 시 partial bundle을 남기지 않고, `--force`
교체 대상이 있더라도 기존 bundle은 보존한다.

`--bundle-source`를 명시하면 `Contents/Resources/project`에 런타임 소스 스냅샷을
포함한다. 기본값은 off다. 스냅샷은 allowlist 기반으로 `main.py`, `config.py`,
`config.yaml`, `pyproject.toml`, 런타임 패키지 디렉토리(`api`, `core`, `steps`,
`search`, `security`, `ui`)만 복사한다. `.env*`, `.git`, `.venv`, `__pycache__`,
캐시, 테스트/하네스, benchmark/build/dist/output/state, 모델/오디오/DB 산출물,
번들 밖을 가리키는 symlink는 포함하지 않는다. 번들 내부 `config.yaml`은 원본을
수정하지 않고 복사본에서 HuggingFace 토큰 값과 토큰 안내 comment를 제거한다.
bundled executable은 이동 가능한 방식으로 실행 시점의 `Contents/Resources/project`를
계산하고, 존재할 때만 `PROJECT_DIR`로 우선 사용한다.

`scripts/validate_launcher_app.py`는 생성된 `.app`를 read-only로 검사한다.
Info.plist 앱 번들 계약, executable 존재/실행 권한과 bash syntax, launcher metadata JSON,
bundle 내부 secret marker, optional `codesign --verify` 결과를 안정 JSON으로
보고한다. unsigned local prototype은 local readiness와 distribution readiness를
분리해 표현하며, 검증기는 앱 실행, 서버 기동, 서명, 공증, 네트워크, 파일 mutation을
수행하지 않는다. source bundle이 활성화되어 있으면 필수 런타임 소스와 제외 규칙도
검증한다.
`CFBundleExecutable`은 `Contents/MacOS` 아래 단일 파일명만 허용하며, 절대경로나
`..`가 포함된 값으로 bundle 밖 파일을 stat/read/bash-probe하지 않는다.
validator는 serialized `launcher-metadata.json` 안에서 `environment_overrides`의
`MT_LAUNCHER_PYTHON_SOURCE`, `MT_LAUNCHER_PYTHON_EXECUTABLE`,
`MT_LAUNCHER_PROJECT_DIR`가 `launcher.runtime` 및 top-level launcher path metadata와
일관적인지도 검사한다. 이 검사는 release artifact의 metadata handoff coherence를
보는 것이며, `.app`을 실제 실행했을 때 wrapper가 넘길 runtime env 값을 증명하지 않는다.
`launcher.runtime.candidates`는 각 후보의 `id`, `path`, `exists`, `is_file`,
`is_executable`, `selected` shape를 가져야 하고, 정확히 하나의 selected 후보가
`runtime.python_source` 및 `runtime.python_executable`과 일치해야 한다.
실패 details에는 값 자체가 아니라 누락/불일치 field name만 기록한다.

`scripts/build_launcher_dmg.py`는 validator의 `local_ready`를 통과한 unsigned `.app`만
`hdiutil create -format UDZO`로 unsigned local DMG에 패키징한다. 출력 경로는 `.dmg`
확장자여야 하며 source `.app` 내부를 가리킬 수 없다. 기존 산출물은 `--force`가 있을 때도
일반 파일만 overwrite 대상으로 허용하고, 디렉토리나 symlink는 거부한다. `hdiutil`이 0을
반환한 뒤에도 실제 `.dmg`가 일반 파일이며 non-empty인지 확인한다. JSON 출력은 success,
returncode, validation summary를 포함하되 path, volume, command 문자열의 token marker는
redaction한다. 이 DMG는 unsigned local artifact이며 distribution readiness, signing,
notarization, stapling은 별도 범위로 남긴다.

`scripts/build_release_manifest.py`는 이미 생성된 unsigned local `.app`와 `.dmg`를
read-only로 식별해 release manifest를 출력한다. `.app`는 `validate_launcher_app`의
`check_codesign=True` 결과를 사용해 `local_ready`, `distribution_ready`, codesign summary를
그대로 기록한다. `.dmg`는 존재하는 일반 non-empty 파일인지 확인하고 mount/attach/open은
하지 않는다. manifest에는 generated timestamp, artifact type/path/byte size/SHA-256,
app file count, validation summary가 포함된다. unsigned `.app`는 `local_ready=true`,
`distribution_ready=false`인 manifest 생성을 허용하지만, 이 manifest 자체가 signing,
notarization, stapling 또는 배포 승인 상태를 의미하지 않는다.

`scripts/build_unsigned_release.py`는 위 safe builder들을 순서대로 호출하는 orchestration
layer다. 기본 산출물은 output 디렉토리 안의 `Recap.app`, `Recap.dmg`,
`Recap.release-manifest.json`이다. `--force`가 없으면 기존 산출물을 거부하고,
`--force`가 있어도 symlink/디렉토리 같은 잘못된 대상은 덮어쓰지 않는다. output 디렉토리
symlink도 거부해 산출물이 다른 위치로 escape하지 않게 한다. 결과 JSON은
`release_type: "unsigned_local"`, `local_ready`, `distribution_ready`를 명시한다.
이 명령도 signing/notarization/distribution-ready release를 만들지 않는다.

## 응답 계약

최상위 필드:

- `status`: `pass` 또는 `fail`
- `configured`: 필수 설정 전제가 준비되었는지 여부
- `ready`: 첫 회의 처리 시도를 할 수 있는지 여부
- `capabilities`: 녹음/전체 회의 캡처/STT 모델 준비 capability
- `checks`: `base_dir`, `python_runtime`, `ffmpeg`, `hf_token_env`, `audio_devices`,
  `stt_model`

체크 상태값은 `pass`, `warn`, `fail`, `unknown` 중 하나다. `warn`은 사용자가
확인해야 하지만 즉시 진행을 막지 않는 상태이고, `unknown`은 timeout이나 권한 문제처럼
안전하게 판정할 수 없었던 상태다.

각 check는 다음 필드를 갖는다.

- `id`, `status`, `ready`, `message`
- `action_hint`: 이전 UI와 호환되는 짧은 안내 문구
- `details`: 진단에 필요한 비밀 없는 key/value
- `actions`: 표시 전용 다음 단계 목록

`python_runtime.details`는 `runtime_scope`(`launcher_handoff` 또는 `server_reconstructed`),
`python_source`, `python_executable`, `running_python`,
`selected_matches_running_python`, `selected_is_file`, `selected_is_executable`,
`candidates`를 포함한다. 경로와 후보 상태만 담고 환경 전체, `.env` 값,
HuggingFace 토큰 값은 포함하지 않는다.
`launcher_handoff`일 때는 `handoff_python_source`, `handoff_python_executable`,
선택적으로 `handoff_project_dir`, 그리고 비교용 `reconstructed_candidates`를 추가할 수 있다.
선택된 Python 후보가 파일이 아니거나 실행 권한이 없거나 `current_interpreter`
fallback을 사용 중이면 `python_runtime.actions`에 Python 버전 확인, 프로젝트 `.venv`,
관리형 venv 준비 명령 예시를 표시할 수 있다. 이 명령은 placeholder 기반의 안내
문자열이며 readiness API와 웹 UI는 venv 생성, `pip install`, 네트워크 접근을 대신
실행하지 않는다. 따라서 action이 응답에 있다는 사실은 사용자가 해당 명령을
실행했거나 런처 환경이 보정되었다는 증거가 아니다.

`actions` 항목의 `kind`는 `external_link`, `route`, `command` 중 하나다.
웹 UI는 `external_link` 중 `https://huggingface.co/...`만 새 창으로 열고,
`route` 중 `/app/settings`만 SPA 내부 이동으로 처리한다. `command`는 `<code>`
텍스트로만 표시하며 실행 버튼이나 clipboard 자동 실행을 제공하지 않는다. 허용되지
않은 action 값은 렌더링하지 않는다.

## 금지 동작

Readiness 조회는 다음 작업을 하지 않는다.

- `brew install`, `scripts/setup_audio.sh`, `swiftc`, Aggregate Device 생성
- `chmod`, 디렉토리 생성, Spotlight/Time Machine 제외 같은 보정 작업
- HuggingFace/Ollama/외부 URL 호출, 모델 다운로드, 모델 로드
- HuggingFace 토큰 값, prefix, 길이, hash, 저장 파일 내용 노출
- `actions`에 포함된 명령 예시나 링크를 자동 실행

토큰은 설정 여부와 환경변수 이름 존재 여부만 노출한다. 값은 응답과 로그에 포함하지 않는다.
토큰 설정 action도 `hf_xxxxx` placeholder만 사용한다.

런처 preflight 역시 `brew`, `pip`, `ollama pull`, `setup_audio.sh`, `launchctl`,
`subprocess.Popen`, Python 후보 실행, 파일 생성/삭제/권한 변경을 수행하지 않는다. 출력 JSON에는
`HUGGINGFACE_TOKEN`, `HF_TOKEN` 값이나 `.env*` 파일 내용을 포함하지 않는다.
런처 host는 로컬 UI 노출을 유지하기 위해 `127.0.0.1`, `localhost`, `::1`만 통과한다.

`.app` builder는 빌드 시점에 앱 실행, 서버 기동, 브라우저 열기, `launchctl`,
`brew`, `pip`, 네트워크 호출, 모델 로드를 수행하지 않는다. 서버 실행과 `/app/setup`
열기는 생성된 bundle executable의 사용자 실행 시점 동작이다.

`.app` validator는 bundle 파일을 읽기만 한다. 서명 상태 확인은 optional
`codesign --verify` read-only probe이며, 실패해도 서명 시도나 keychain 접근 없이
distribution readiness 미충족으로 보고한다.

`.app` DMG builder는 `hdiutil create`만 호출한다. 앱 실행, server launch, `open`,
`hdiutil attach`, `codesign`, `notarytool`, `stapler`, `brew`, `pip`, 네트워크 호출,
설치 작업을 수행하지 않는다.

release manifest builder는 입력 `.app`/`.dmg` 파일을 읽기만 한다. 앱 실행, server launch,
DMG mount/attach/open, signing, notarization, stapling, 네트워크 호출, 설치 작업을 하지 않는다.
성공/실패 JSON 모두 path나 validation detail의 secret marker를 redaction한다.

unsigned release builder는 source tree를 수정하지 않고 output 디렉토리 안의 산출물만 쓴다.
앱 실행, server launch, DMG mount/attach/open, signing, notarization, stapling, 네트워크 호출,
설치 작업을 하지 않는다.

## 검증

```bash
.venv/bin/python -m py_compile scripts/build_unsigned_release.py scripts/build_release_manifest.py scripts/build_launcher_app.py scripts/validate_launcher_app.py scripts/build_launcher_dmg.py ui/launcher.py
.venv/bin/python -m pytest tests/test_build_unsigned_release.py tests/test_build_release_manifest.py tests/test_build_launcher_dmg.py tests/test_validate_launcher_app.py tests/test_build_launcher_app.py tests/test_launcher.py -q
.venv/bin/python -m pytest tests/test_setup_readiness.py tests/test_routes_setup_readiness.py -q
.venv/bin/python -m pytest -m ui tests/ui/integration/test_spa_overhaul_integration.py -k "setup_route" -q
```
