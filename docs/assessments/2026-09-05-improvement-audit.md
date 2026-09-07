# Meeting Transcriber 개선 필요사항 심층 평가

- 평가일: 2026-09-05
- 기준: `/Users/youngouksong/projects/meeting-transcriber`, HEAD `e3e4b61f2d5730e257fa46b79a06bcf1e09d575e`, 시작 시 git clean.
- 적용 스킬: `/Users/youngouksong/projects/meeting-transcriber/.codex/skills/meeting-transcriber-release-harness/SKILL.md`.
- 범위: 제품 UX, 온보딩, 배포, 운영, 자원 관리, 품질평가, 테스트. 정적 읽기 전용 감사. 실제 사용자 데이터·비밀 파일·모델 다운로드·서버 실행 없음.
- 아래 항목은 확인된 제품 제약에 대한 개선안이다. 관찰된 매출·전환율 자료는 없으므로 사용자 가치와 사업 효과는 검증할 가설이다. 비용 S/M/L은 상대적 규모이며 일정 견적이 아니다.

## 판단

체크포인트, 회의별 mutation coordinator, 모델 직렬화, 취소 후 안전한 정리, 외부 업로드 동의, UI 접근성 및 단위 테스트 등 엔지니어링 기반은 강하다. `docs/STATUS.md:91`의 3915개 기본 테스트 통과는 과거 기록이며 이번 환경에서 재현한 수치가 아니다. 다음 투자 우선순위는 기능 수를 늘리는 것보다 **비개발자의 첫 성공, 실제 품질 입증, 재처리와 복구 경험**이다. 현재 저장소만으로 일반 소비자 배포 완료 또는 사람 정답 기준 인식 품질을 입증할 수 없다.

## 우선순위 표

| 순위 | 개선 | 규모 | 사용자 가치 | 검증 지표 |
|---|---|---|---|---|
| P1-1 | 설치 가능한 완성 앱과 배포 검증 | L | 터미널 없이 사용 시작 | 다운로드→첫 실행→첫 회의록 전환, 설치 실패율, 지원 요청/설치 |
| P1-2 | 사람 정답 기반 전사·요약·검색 평가 | M→L | 정확도와 신뢰를 실제로 개선 | CER/WER, DER/JER, 고유명사 정확도, 사실 오류·누락, 교정 소요 시간 |
| P1-3 | 목적별 온보딩과 양방향 테스트 녹음 | M | 첫 회의에서 상대 목소리 누락 방지 | 첫 정상 녹음률, 양방향 누락률, 첫 결과까지 시간 |
| P1-4 | 메모리·시간초과·정리대기 운영 UX | M | 오래 걸리는 작업을 이해하고 복구 | p50/p95 완료시간, OOM/timeout, 반복 실패, 자원 피크 |
| P1-5 | 명시적 백업·복원 경험 | M→L | 장기간 축적한 회의 자산 보호 | 복원 성공률, 마지막 검증 백업 나이, 복구 시간 |
| P2-6 | 버전 보존형 요약 재생성 | M→L | 프롬프트·모델 개선을 기존 회의에 적용 | 재생성→채택률, 편집 시간, 이전 버전 복원 성공 |
| P2-7 | 회의록·액션아이템 내보내기 | S→M | 결과를 동료와 업무 도구에 재사용 | 결과 생성→내보내기율, 재편집 시간, 반복 사용 |
| P2-8 | 핵심 사용자 여정 CI와 실제 Mac 실행 게이트 | M | 업그레이드 후 작업 중단 방지 | 핵심 여정 성공률, 출시 후 오류율, 추론·녹음 회귀 |

## 1. 설치 가능한 완성 앱과 배포 검증

**시나리오:** 비개발자 PM이 소개받아 앱을 설치하지만 Python, venv, Homebrew, ffmpeg, HuggingFace 로그인과 모델 다운로드에서 중단한다.

**현재 근거:**

- `/Users/youngouksong/projects/meeting-transcriber/README.md:123` — 가상환경 생성과 `pip install -e ".[dev]"`가 사용자 설치 경로다. `:154`는 양방향 녹음에 Aggregate Device, `:165`는 첫 LLM 실행 때 약 6GB 다운로드를 안내한다.
- `/Users/youngouksong/projects/meeting-transcriber/ui/launcher.py:225` — 런처는 프로젝트 venv, 사용자 관리 venv, 현재 인터프리터를 선택한다. 자체 포함 런타임 설치 흐름이 아니다.
- `/Users/youngouksong/projects/meeting-transcriber/docs/STATUS.md:409` — DMG는 unsigned local이며 서명/공증은 별도 미완 범위다.
- `/Users/youngouksong/projects/meeting-transcriber/scripts/validate_launcher_app.py:188` — `distribution_ready`는 구조 검사와 codesign pass 조합. `:831`의 실행 검사는 `codesign --verify --deep --strict`이며 공증·Gatekeeper·실제 깨끗한 Mac 첫 실행 검사는 이 계약에 없다. 따라서 이 필드만으로 소비자 배포 가능을 판정하지 않아야 한다.

**제안:** 지원 Python/런타임을 일관되게 준비하는 배포 패키지, 서명·공증·깨끗한 macOS 설치/업데이트/삭제 검증, 모델 다운로드 진행·용량·중단 재개, 에러별 복구 안내를 한 제품 흐름으로 묶는다. 먼저 지원자에게 설치시키는 소규모 테스트로 가장 큰 이탈 단계를 계측한다. 서명·공증 실행과 외부 배포는 별도 승인 범위다.

## 2. 사람 정답 기반 품질평가

**시나리오:** 사용자가 회의록에서 담당자나 숫자를 잘못 발견해 신뢰를 잃지만, 문자열 유사도는 높은 점수를 내거나 mocked 테스트는 통과한다.

**현재 근거:**

- `/Users/youngouksong/projects/meeting-transcriber/docs/BENCHMARK.md:103` — 6회의 CER 기준은 OpenAI `gpt-4o-transcribe` 출력이다. `:114`의 49.80%는 독립적인 사람 정답 오류율이 아니며 품질 절대치로 해석할 수 없다.
- 같은 문서 `:305` — LLM 기준은 Claude 교정 결과이며 `:307`에서 SequenceMatcher 사용. `:31`의 LLM 비교는 2회의 44발화이고 `:25`는 M4/16GB 단일 하드웨어다.
- 같은 문서 `:49` — 운영 OpenAI 옵션은 독립 사람 정답 CER/WER·DER/JER 비교가 아직 없다.
- `/Users/youngouksong/projects/meeting-transcriber/tests/test_quality_evals.py:1` — 실제 STT/LLM 모델을 호출하지 않는 계약 테스트임을 명시. 요약 테스트는 fallback에 원문이 남는지, RAG 테스트는 프롬프트에 근거가 포함되는지 확인한다. 이는 유용하지만 실제 생성의 사실성 검증을 대신하지 못한다.

**제안:** 동의받은 다양한 회의 표본을 분리된 평가 세트로 구성하고, 두 사람이 독립적으로 전사·화자·핵심 결정·할 일을 라벨링한 뒤 불일치를 조정한다. 개발용과 held-out 평가를 분리한다. 숫자·고유명사·담당자·마감일 가중 오류, 환각/누락, 인용 정확도, 답할 수 없는 질문의 거절까지 측정한다. 30/60/120분, 1:1·여러 화자·영한 혼용·잡음 등 조건을 분리 보고한다. 모델별 점수뿐 아니라 실제 사람이 최종 회의록을 고치는 시간을 핵심 가치 지표로 둔다.

## 3. 목적별 온보딩과 양방향 테스트 녹음

**시나리오:** 파일을 전사하려는 사람에게 녹음 장치 설정이 함께 제시되거나, 장치가 감지되어 준비 완료로 보였지만 실제 Zoom 출력 경로가 달라 상대 음성이 녹음되지 않는다.

**현재 근거:**

- `/Users/youngouksong/projects/meeting-transcriber/security/setup_readiness.py:106` — base dir, Python, ffmpeg, HF, audio, STT를 한 번에 수집한다. `:121`과 `:127`의 top-level 준비 판정은 HF와 오디오/STT 준비 상태를 조합한다.
- 같은 파일 `:599` — 장치 목록 문자열과 aggregate 이름으로 감지하며 `:605`에서 설정과 존재 여부로 full capture 상태를 계산한다. 실제 양쪽 입력의 샘플 음량·재생 확인은 이 readiness 검사에 없다.
- `/Users/youngouksong/projects/meeting-transcriber/ui/web/spa.js:434` — 설치/권한/다운로드를 실행하지 않는 상태 화면이다. `:699`의 command 액션은 코드 문자열 표시다.
- `/Users/youngouksong/projects/meeting-transcriber/AGENTS.md:169` — Zoom 스피커 등 실제 앱 설정은 사용자가 직접 수행하도록 한다.

**제안:** 첫 화면에서 ‘파일 전사 / 대면 녹음 / 온라인 회의 녹음’을 고르면 필요한 단계만 보여준다. 명시적 사용자 시작으로 짧은 테스트 녹음을 제공하고, 본인·상대 소스별 레벨과 재생 확인을 거친다. 단계별 체크 완료와 ‘지금 사용 가능한 기능’을 분리한다. 사용자 오디오 장치 설정이나 모델 약관 동의를 몰래 자동화하는 방식으로 해결하지 않는다. 제품 계측은 전사 원문·음성을 수집하지 않는 로컬 기록 또는 선택 동의 방식으로 설계한다.

## 4. 메모리·시간초과·정리대기 운영 UX

**시나리오:** 긴 회의를 처리하다 timeout이 나고 이후 재시도도 실패하는데 하단에는 RAM/CPU/모델 이름만 보여 이유를 알 수 없다. 동시에 다른 업무 앱을 쓰면 피크 메모리가 달라진다.

**현재 근거:**

- `/Users/youngouksong/projects/meeting-transcriber/config.py:625` — 9.5GB 설정. `/Users/youngouksong/projects/meeting-transcriber/core/model_manager.py:328` — 초과 시 경고만 남기며 강제 중단하지 않음을 명시한다. `:230`의 측정은 현재 프로세스 RSS다. 따라서 9.5GB는 절대 상한을 보장하는 admission 제어가 아니다.
- `/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:266` — LLM 진입 전 가용 메모리를 검사하고, `:2900`에서 실제 단계 직전 재점검하는 보호는 이미 있다.
- `/Users/youngouksong/projects/meeting-transcriber/config.py:417` — diarize 최소 1800초, 배수 1.25, 동적 상한 10800초. 이는 실행 예산이지 완료시간 예측이나 장시간 성능 입증이 아니다.
- `/Users/youngouksong/projects/meeting-transcriber/core/model_manager.py:528` — 취소된 worker 정리가 끝나기 전 신규 모델 작업을 typed 오류로 즉시 거부한다. 모델 중복 로드를 막는 올바른 보호다.
- `/Users/youngouksong/projects/meeting-transcriber/api/routers/system.py:297` — 정리대기 여부/모델/worker 수/시작 시각을 이미 반환한다. `/Users/youngouksong/projects/meeting-transcriber/ui/web/global-resource-bar.js:82` 이후 렌더링은 RAM/CPU/loaded_model만 읽는다. `rg native_cleanup ui/web` 결과 없음.

**제안:** 기존 API 상태로 ‘이전 작업 정리 중, 새 작업 대기’와 경과시간·재시도 가능 상태를 표시한다. timeout, 열 식히기, Zoom 보호 pause, 모델 다운로드, 실제 추론을 구분한다. 메모리 관리에서는 부모+worker와 MLX 자원 지표를 중복 합산 없이 따로 보고하고 하드웨어·오디오 길이별 peak/RTF를 측정한다. 현재 작업을 위험하게 강제 해제하지 않고, 안전한 대기/다음 회의 시작 유예 및 소형 모델 선택을 지원한다.

## 5. 명시적 백업·복원 경험

**시나리오:** 사용자가 Mac을 교체하거나 디스크가 고장 나고, 일반 Time Machine에 회의가 저장됐을 것으로 기대하지만 복원되지 않는다.

**현재 근거:**

- `/Users/youngouksong/projects/meeting-transcriber/config.py:795` — `exclude_from_timemachine=True`가 기본이다.
- `/Users/youngouksong/projects/meeting-transcriber/security/secure_dir.py:125` — 초기화에서 base dir 백업 제외를 적용한다. `:271`은 `tmutil addexclusion` 호출이다. 실제 사용자 시스템에 적용됐는지는 이번 감사에서 확인하지 않았다.
- `/Users/youngouksong/projects/meeting-transcriber/api/routers/meeting_detail.py:2953` — 편집 직전 파일 `.bak`은 있지만 이는 장치 손실 대비 백업이 아니다.
- `/Users/youngouksong/projects/meeting-transcriber/ui/web/viewer-view.js:1524` — 개별 전사문 복사/TXT 내보내기는 있다. API/router·UI 검색 범위에서 원본/회의 메타데이터/요약/수정 이력/검색 재구성까지 다루는 앱 전체 백업·복원 플로우는 확인하지 못했다.

**제안:** 설정에서 현재 백업 제외 정책과 마지막 검증 백업을 알리고, 사용자가 선택한 로컬 위치로 암호화 백업 및 가져오기/복원 검증을 제공한다. 모델 가중치는 재다운로드 대상으로 분리하고 회의 원본·산출물·메타데이터·사용자 용어집은 보존한다. 검색 인덱스는 필요 시 재구성한다. 비밀 키는 별도로 재등록하며 임의 외부 업로드나 자동 백업 정책 변경은 하지 않는다. 실제 격리 디렉터리 복원 리허설을 통과해야 ‘백업 성공’으로 본다.

## 6. 버전 보존형 요약 재생성

**시나리오:** 사용자가 전사 고유명사를 수정하거나 더 나은 요약 프롬프트를 만들었지만 기존 회의록에 다시 적용할 수 없어 직접 전체 내용을 고친다.

**현재 근거:**

- `/Users/youngouksong/projects/meeting-transcriber/api/routers/meeting_detail.py:3435` — `force=true`는 항상 409 SECURITY_BLOCKED다.
- `/Users/youngouksong/projects/meeting-transcriber/ui/web/viewer-view.js:3219` — 기존 산출물 보존 때문에 요약 toolbar는 편집만 노출한다고 명시한다.
- `/Users/youngouksong/projects/meeting-transcriber/docs/STATUS.md:44` — 동일 UID namespace 경쟁으로 파괴적 재생성을 차단한 의도적인 안전 결정이다. 이를 단순 버그로 분류하거나 기존 보호를 제거해서는 안 된다.

**제안:** 새 요약을 별도 불변 generation/version으로 생성하고 원본·모델·프롬프트·전사 버전 연결을 저장한다. 사용자에게 비교/채택/원복을 제공하고 canonical 버전 포인터만 기존 coordinator 안에서 안전하게 전환한다. 반쯤 만들어진 산출물과 사람이 편집한 결과를 자동 삭제하지 않는다. 이후 전사 재교정도 같은 사용 경험으로 확장할 수 있다.

## 7. 회의록·액션아이템의 업무 재사용

**시나리오:** PM이 결과를 팀에 공유하려 하지만 현재 전사 TXT와 요약 편집만으로는 회의 결정·담당자·마감일을 업무 형식에 옮기는 반복 작업이 남는다.

**현재 근거:** `/Users/youngouksong/projects/meeting-transcriber/ui/web/viewer-view.js:1524`는 전사 복사/TXT를 제공한다. 같은 파일 `:3213`부터 요약 toolbar는 편집 버튼만 구성한다. 이는 전체 제품에 내보내기가 전혀 없다는 뜻이 아니라, 핵심 회의록 탭의 후속 업무 동작이 약하다는 뜻이다.

**제안:** 먼저 회의록 Markdown/클립보드, 액션아이템 CSV, 타임스탬프·출처 포함 보고서 템플릿을 로컬로 제공한다. 내보내기 시 모델이 만든 내용과 사람이 승인한 결정을 구분할 수 있게 한다. 이용 빈도를 확인한 뒤 Notion/Slack/Jira 등 연동의 우선순위를 정한다. 자동 외부 전송은 기본값으로 추가하지 않는다. 사용자의 실제 공유율/요청 데이터가 없으므로 특정 외부 연동에 대한 구매 수요는 아직 가설이다.

## 8. 핵심 사용자 여정 CI와 실제 Mac 실행 게이트

**시나리오:** 단위 테스트는 통과하지만 업그레이드 후 첫 설정, 모델 선택, 녹음, 전사, 편집, 검색의 연결이 깨진다.

**현재 근거:**

- `/Users/youngouksong/projects/meeting-transcriber/pyproject.toml:115` — 기본 pytest는 e2e/ui/native를 제외한다.
- `/Users/youngouksong/projects/meeting-transcriber/.github/workflows/ci.yml:121` — 기본 게이트 후 명시적으로 돌리는 UI 테스트는 bulk actions behavior/a11y/visual 세 파일이다. `:155`의 native gate는 수동 또는 schedule 조건이다. 전체 core E2E가 PR 필수 경로에 연결돼 있다는 증거는 없다.
- `/Users/youngouksong/projects/meeting-transcriber/tests/native/test_preflight_native.py:12` — 이 native smoke는 사전진단의 반환 계약 일관성을 검사하며 실제 성공적인 모델 추론·녹음을 입증하지 않는다. 다른 native 테스트가 없다는 주장은 아니다.
- `/Users/youngouksong/projects/meeting-transcriber/docs/STATUS.md:91` — 과거 E2E 17개 통과 기록은 있으므로 E2E 자체 부재가 아니라 **상시 PR 게이트 연결 범위**의 문제다.

**제안:** PR에는 소형 fixture와 mock 모델로 setup→입력→provider 동의→진행→완료→편집→검색/인용의 핵심 경로를 제한된 수로 돌린다. release/nightly에는 실제 Apple Silicon에서 짧은 동의된 샘플 추론, 30/60/120분 자원 평가, 음원 두 입력 검증, 앱 종료/재시작/업데이트 체크를 별도로 실행한다. mock 계약 테스트와 실제 품질/하드웨어 검증 결과를 구분해 배포 보고서에 남긴다.

## 검증 및 한계

- 성공: `git status --short` clean, `git rev-parse HEAD`; 관련 코드·문서·workflow를 `rg`, `sed`, `nl`로 독립 교차 확인.
- 시도했으나 실행 불가: `.venv/bin/python -m pytest tests/test_quality_evals.py tests/test_setup_readiness.py tests/test_launcher.py tests/test_validate_launcher_app.py -q` → exit 127, `.venv/bin/python` 없음.
- 환경 확인: 시스템 Python `/opt/homebrew/opt/python@3.14/bin/python3.14`, pytest 미설치. 프로젝트는 `pyproject.toml:13`에서 Python >=3.11,<3.13을 요구한다. 설치/환경 변경하지 않았다.
- 실제 모델 정확도, 피크 RAM, 장시간 회의 처리속도, 원격 CI 현재 결과, 사용자 이탈률, 실제 백업 적용 여부는 이번 감사에서 측정하지 않았다. 문서상 실험 결과와 이번 정적 관찰을 명확히 구분했다.
- 저장소 코드 변경 없음. 이 보고서만 `/tmp`에 생성.
