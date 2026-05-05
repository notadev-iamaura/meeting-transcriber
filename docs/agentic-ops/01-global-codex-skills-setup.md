# 01. 글로벌 Codex Skills 셋업 계획

## 목적

반복될 리팩터링, 리뷰, 성능 측정, 릴리스 검증 절차를 매번 프롬프트로 다시 설명하지 않기 위해 Codex skill로 고정한다. Skill은 전역 설치가 가능하지만, source of truth는 repo 안에 둔다.

## 설계 원칙

`skill-creator` 지침 기준:

- `SKILL.md`는 짧고 절차 중심이어야 한다.
- 상세 reference는 별도 파일로 분리한다.
- 반복 가능한 workflow만 skill로 만든다.
- 프로젝트 지식은 reference로 두고, 매번 필요한 부분만 읽게 한다.
- 스킬은 한 번에 모든 것을 하지 않고, 특정 역할과 작업 유형에 집중한다.

## 권장 Skill 묶음

프로젝트 전용 skill은 이름 충돌을 피하기 위해 `meeting-transcriber-` prefix를 사용한다. 전역 재사용 가능성이 있는 skill만 더 일반 이름을 쓴다.

### 1. `meeting-transcriber-quality-gates`

**위치**  
프로젝트 로컬.

**트리거**  
검증, 리뷰, PR 준비, 리팩터링 안전성 확인, merge 가능성 판단.

**핵심 절차**

1. `docs/STATUS.md`의 최신 gate 확인
2. 변경 범위별 테스트 선택
3. generated file 포함 여부 확인
4. role consensus 상태 확인
5. 실패 또는 불확실성은 문서에 명시

**참조 문서**

- `docs/STATUS.md`
- `docs/EXPERT_ENGINEERING_REVIEW_2026-05-01.md`
- `docs/PERFORMANCE_BACKLOG.md`

### 2. `meeting-transcriber-web-ui`

**위치**  
프로젝트 로컬.

**트리거**  
UI/CSS/SPA/접근성/시각 회귀/디자인 토큰 작업.

**핵심 절차**

1. `docs/design.md` 확인
2. `ui/web/spa.js` state ownership 확인
3. JS와 CSS 변경을 분리
4. behavior/a11y/visual gate 실행
5. visual drift는 의도 변경과 회귀를 구분해 기록

**참조 문서**

- `docs/design.md`
- `docs/plans/design-tokens-spec.md`
- `docs/plans/command-palette-spec.md`
- `harness/README.md`

### 3. `meeting-transcriber-pipeline-dev`

**위치**  
프로젝트 로컬.

**트리거**  
STT/diarize/merge/correct/summarize/chunk/embed 파이프라인 변경, 모델 로딩, 체크포인트, 재처리.

**핵심 절차**

1. pipeline step 순서 확인
2. checkpoint 호환성 확인
3. native/backend adapter 경계 확인
4. 관련 targeted tests 실행
5. 성능 또는 정확도 변화는 문서화

**참조 문서**

- `core/pipeline.py`
- `docs/plans/rag-pipeline-completion.md`
- `docs/PERFORMANCE_BACKLOG.md`

### 4. `meeting-transcriber-rag-search`

**위치**  
프로젝트 로컬.

**트리거**  
RAG, ChromaDB, FTS5, reindex, chat, wiki hybrid, 검색 누락.

**핵심 절차**

1. Chroma/FTS 양쪽 계약 확인
2. query prefix/passsage prefix 유지
3. reindex/backfill API 영향 확인
4. cache invalidation 정책 확인
5. search/chat/wiki targeted tests 실행

**참조 문서**

- `docs/plans/rag-pipeline-completion.md`
- `docs/plans/2026-04-28-llm-wiki-hybrid.md`
- `search/hybrid_search.py`

### 5. `meeting-transcriber-setup`

**위치**  
프로젝트 로컬.

**트리거**  
프로젝트 셋업, 환경 구성, 실행 실패, 의존성 설치.

**핵심 절차**

1. Apple Silicon/Python/Homebrew/ffmpeg 확인
2. `.venv` 생성
3. `pip install -e ".[dev]"`
4. `scripts/install.sh`
5. HF token, BlackHole, Aggregate Device처럼 사용자 수동 개입 필요 항목 안내
6. `docs/STATUS.md`의 검증 gate 실행

**참조 문서**

- `AGENTS.md`
- `README.md`
- `docs/STATUS.md`

### 6. `meeting-transcriber-audio-capture`

**위치**  
프로젝트 로컬.

**트리거**  
오디오 녹음 셋업, Aggregate Device, BlackHole, Zoom 녹음.

**핵심 절차**

1. `scripts/setup_audio.sh --check`
2. 필요 시 `scripts/setup_audio.sh`
3. 실패 원인별 안내
4. Zoom 설정은 자동화하지 않고 문서 절차 안내

**참조 문서**

- `docs/AGGREGATE_DEVICE_SETUP.md`
- `scripts/setup_audio.sh`

### 7. `meeting-transcriber-stt-llm-benchmark`

**위치**  
프로젝트 로컬.

**트리거**  
STT 모델 비교, CER/WER, LLM 교정 품질, 모델 기본값 변경 검토.

**핵심 절차**

1. 기존 benchmark 문서 확인
2. 동일 샘플/동일 설정으로 재현
3. 환각/누락률/속도 분리 기록
4. 모델 기본값 변경 전 README/STATUS 업데이트

**참조 문서**

- `docs/BENCHMARK.md`
- `docs/plan-stt-improvements.md`
- `scripts/benchmark_stt.py`
- `scripts/benchmark_llm.py`

### 8. `local-ml-network-safety`

**위치**  
전역 후보.

**트리거**  
`pip`, Hugging Face, STT/LLM 모델 다운로드, SSL, 프록시, 403, token 문제.

**핵심 절차**

1. SSL 우회 금지
2. 에러 원문 보존
3. 브라우저 수동 다운로드 또는 공식 API import 경로 안내
4. token과 gated model 동의는 사용자가 직접 수행
5. 우회가 필요해 보이면 중단하고 사용자 판단 대기

**참조 문서**

- `AGENTS.md`의 네트워크·다운로드 장애 처리 원칙

### 9. `meeting-transcriber-agentic-pm`

**트리거**  
Recap 프로젝트에서 여러 서브에이전트를 배정하거나, workstream을 열고 닫거나, 합의 상태를 관리할 때 사용.

**역할**

- 티켓 생성
- 역할별 에이전트 배정
- write-scope 충돌 검사
- 리뷰 상태 확인
- merge-final 승인 조건 확인

**핵심 절차**

1. `docs/STATUS.md` 확인
2. `docs/agentic-ops/03-workstream-map.md`에서 workstream 선택
3. ticket open
4. 역할별 Producer/Reviewer 지정
5. gate 결과와 review 상태 수집
6. consensus 통과 시 merge proposal 작성

**참조 문서**

- `docs/agentic-ops/02-agent-consensus-harness.md`
- `docs/agentic-ops/03-workstream-map.md`
- `docs/STATUS.md`

### 10. `meeting-transcriber-backend-service-refactor`

**트리거**  
`api/routes.py`, `api/server.py`, `api/routers/*`, service layer, runtime profile, FastAPI route 분리를 수정할 때 사용.

**역할**

- route handler를 얇게 유지
- `app.state` 접근을 `api/dependencies.py`로 이동
- service layer 도입
- API 테스트와 profile gate 유지

**핵심 절차**

1. 변경 대상 route와 service boundary 식별
2. write-scope 선언
3. schema/dependency/service/router 분리
4. route test 실행
5. `ruff check`, `ruff format --check`

**참조 문서**

- `docs/EXPERT_ENGINEERING_REVIEW_2026-05-01.md`
- `docs/CODE_QUALITY_MAINTAINABILITY_PERFORMANCE_REVIEW_2026-05-01.md`
- `api/routers/meetings_batch.py`

### 11. `meeting-transcriber-frontend-modularization`

**트리거**  
`ui/web/spa.js`, `ui/web/style.css`, `tokens.css`, `bulk-actions.css`, UI behavior/a11y/visual gate와 관련된 작업.

**역할**

- JS feature module 분리
- CSS token/component/feature 분리
- UI gate 유지
- DOM state ownership 정리

**핵심 절차**

1. 변경 feature를 하나만 선택
2. 기존 behavior/a11y/visual test 확인
3. JS 또는 CSS 한 축만 먼저 분리
4. UI gate 실행
5. visual diff가 의도 변경인지 회귀인지 기록

**참조 문서**

- `harness/README.md`
- `docs/design.md`
- `docs/design-decisions/bulk-actions.md`
- `ui/web/spa.js`
- `ui/web/bulk-actions.css`

### 12. `meeting-transcriber-pipeline-performance`

**트리거**  
`core/pipeline.py`, `steps/*`, `search/*`, checkpoint, embedding, LLM, RAG, performance backlog 작업.

**역할**

- pipeline step contract 정리
- checkpoint IO 개선
- search/cache/index 개선
- metric 기반 성능 개선

**핵심 절차**

1. 성능 가설을 문서화
2. 측정 metric 정의
3. 최소 변경으로 계측 추가
4. benchmark 또는 targeted test 실행
5. 결과를 `docs/PERFORMANCE_BACKLOG.md` 또는 신규 report에 기록

**참조 문서**

- `docs/PERFORMANCE_BACKLOG.md`
- `core/pipeline.py`
- `steps/embedder.py`
- `search/hybrid_search.py`

### 13. `meeting-transcriber-release-gate`

**트리거**  
PR 준비, merge 전 검증, CI 변경, 릴리스 후보 검토.

**역할**

- lint/test/ui/native gate 분리
- PR template 검증
- STATUS 업데이트
- generated file 차단

**핵심 절차**

1. `git status --short`
2. generated file 여부 확인
3. required gate 실행
4. docs/STATUS 업데이트
5. consensus status 확인

**참조 문서**

- `.github/workflows/ci.yml`
- `.github/pull_request_template.md`
- `docs/STATUS.md`

### 14. `meeting-transcriber-docs-onboarding`

**트리거**  
README, AGENTS, setup, contributor guide, troubleshooting, doctor/diagnostics 문서 작업.

**역할**

- 문서 최신성 유지
- 사용자/개발자/에이전트 문서 분리
- 설치/환경 조건 명확화

**핵심 절차**

1. 문서 대상 독자 확인
2. 중복 문서 검색
3. canonical source 지정
4. 오래된 문서에는 superseded link 추가
5. README에는 핵심만 남기고 상세 문서로 링크

## 글로벌 설치 정책

### 권장 구조

```text
$CODEX_HOME/skills/
  meeting-transcriber-quality-gates/
    SKILL.md
    references/
  meeting-transcriber-web-ui/
    SKILL.md
    references/
  meeting-transcriber-pipeline-dev/
    SKILL.md
    references/
  meeting-transcriber-rag-search/
    SKILL.md
    references/
  meeting-transcriber-setup/
    SKILL.md
    references/
  meeting-transcriber-audio-capture/
    SKILL.md
    references/
  meeting-transcriber-stt-llm-benchmark/
    SKILL.md
    references/
  local-ml-network-safety/
    SKILL.md
    references/
  meeting-transcriber-agentic-pm/
    SKILL.md
    references/
      consensus-harness.md
      workstream-map.md
  meeting-transcriber-backend-service-refactor/
    SKILL.md
    references/
      backend-boundaries.md
  meeting-transcriber-frontend-modularization/
    SKILL.md
    references/
      ui-harness.md
  meeting-transcriber-pipeline-performance/
    SKILL.md
    references/
      performance-metrics.md
  meeting-transcriber-release-gate/
    SKILL.md
  meeting-transcriber-docs-onboarding/
    SKILL.md
```

### Source of Truth

글로벌 설치본은 실행용 복사본으로만 둔다. 원본은 repo 안에 둔다.

권장 repo 위치:

```text
codex-skills/
  meeting-transcriber-quality-gates/
  meeting-transcriber-web-ui/
  meeting-transcriber-pipeline-dev/
  meeting-transcriber-rag-search/
  meeting-transcriber-setup/
  meeting-transcriber-audio-capture/
  meeting-transcriber-stt-llm-benchmark/
  local-ml-network-safety/
  meeting-transcriber-agentic-pm/
  meeting-transcriber-backend-service-refactor/
  meeting-transcriber-frontend-modularization/
  meeting-transcriber-pipeline-performance/
  meeting-transcriber-release-gate/
  meeting-transcriber-docs-onboarding/
```

### 설치 절차

전역 설치는 repo 밖 `$CODEX_HOME/skills`에 쓰므로 사용자 승인 후 수행한다.

```bash
mkdir -p "$CODEX_HOME/skills"
cp -R codex-skills/meeting-transcriber-quality-gates "$CODEX_HOME/skills/"
cp -R codex-skills/meeting-transcriber-web-ui "$CODEX_HOME/skills/"
cp -R codex-skills/meeting-transcriber-pipeline-dev "$CODEX_HOME/skills/"
cp -R codex-skills/meeting-transcriber-rag-search "$CODEX_HOME/skills/"
cp -R codex-skills/meeting-transcriber-setup "$CODEX_HOME/skills/"
cp -R codex-skills/meeting-transcriber-audio-capture "$CODEX_HOME/skills/"
cp -R codex-skills/meeting-transcriber-stt-llm-benchmark "$CODEX_HOME/skills/"
cp -R codex-skills/local-ml-network-safety "$CODEX_HOME/skills/"
cp -R codex-skills/meeting-transcriber-agentic-pm "$CODEX_HOME/skills/"
cp -R codex-skills/meeting-transcriber-backend-service-refactor "$CODEX_HOME/skills/"
cp -R codex-skills/meeting-transcriber-frontend-modularization "$CODEX_HOME/skills/"
cp -R codex-skills/meeting-transcriber-pipeline-performance "$CODEX_HOME/skills/"
cp -R codex-skills/meeting-transcriber-release-gate "$CODEX_HOME/skills/"
cp -R codex-skills/meeting-transcriber-docs-onboarding "$CODEX_HOME/skills/"
```

설치 후 Codex 재시작이 필요하다.

## 플러그인화 기준

OpenAI 공식 설명상 plugin은 skill, app integration, MCP server 설정을 묶는 배포 단위다. 현재 단계에서는 project-local skills가 충분하다.

Plugin으로 올릴 조건:

- 이 workflow를 다른 repo/team에도 재사용해야 한다.
- skill 3개 이상을 하나의 설치 단위로 묶을 필요가 있다.
- MCP server 또는 app integration이 추가된다.
- 팀 단위 배포와 버전 관리가 필요하다.
