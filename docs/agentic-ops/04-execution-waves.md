# 04. 실행 Wave 계획

## 운영 전제

각 wave는 다음 순서로 진행한다.

```text
scope freeze
→ agent assignment
→ producer work
→ self-check
→ peer-review
→ gate run
→ QA review
→ PM merge-final
→ board/status update
```

## Wave 0: 운영 체계 고정

### 목적

실제 코드 리팩터링 전에 에이전트 운영과 합의 체계를 고정한다.

### 작업

1. `docs/agentic-ops/` 문서 확정
2. skill blueprint 확정
3. 하네스 확장 범위 결정
4. generated file 정책 확정
5. current baseline 기록

### 병렬성

- Docs-A/B: 문서 검토
- Architect-A/B: 하네스 설계 검토
- QA-A/B: gate profile 검토

### 완료 조건

- PM-B merge-final
- `docs/STATUS.md`와 충돌 없음
- 다음 wave ticket 생성 가능

## Wave 1: 낮은 위험 boundary 생성

### 목적

큰 파일을 직접 찢기 전에 새 경계를 만든다.

### 작업

| Ticket | 작업 | Owner |
|---|---|---|
| W1-BE-1 | `api/dependencies.py` 도입 | Backend-A |
| W1-FE-1 | `ui/web/api-client.js` 도입 | Frontend-A |
| W1-PIPE-1 | checkpoint compact JSON 옵션 설계 | Pipeline-A |
| W1-DOC-1 | `docs/STATUS.md` canonical status 정리 | Docs-A |

### 병렬성

네 작업은 동시에 가능하다. 단, `pyproject.toml`이나 CI를 건드리는 변경은 Wave 1에서 금지한다.

### 완료 조건

- 각 ticket peer-review approved
- `ruff check .`
- `ruff format --check .`
- targeted tests

## Wave 2: 테스트가 강한 기능부터 분리

### 목적

테스트 안전망이 있는 영역을 먼저 모듈화한다.

### 작업

| Ticket | 작업 | Owner |
|---|---|---|
| W2-FE-1 | `bulk-actions.js` 추출 | Frontend-A |
| W2-FE-2 | `bulk-actions.css` gate 재검증 | Frontend-B/QA-A |
| W2-BE-1 | `meetings_batch_service.py` 추출 | Backend-A |
| W2-PIPE-1 | pipeline state/checkpoint 분리 | Pipeline-A |

### 순차 의존

- `bulk-actions.js` 추출은 `api-client.js` 이후가 좋다.
- `meetings_batch_service.py`는 현재 `api/routers/meetings_batch.py` 분리 후속이다.
- pipeline state/checkpoint 분리는 metrics보다 먼저다.

### 완료 조건

- UI targeted gate
- batch API tests
- pipeline targeted tests
- PM-B merge-final

## Wave 3: Route/service 대형 분리

### 목적

`api/routes.py` monolith를 domain router/service로 줄인다.

### 작업 순서

1. uploads
2. settings
3. meetings
4. dashboard
5. search/chat
6. wiki/backfill

### 병렬성

동시에 여러 route를 옮기지 않는다. 대신 Frontend/Pipeline/Docs 작업과 병렬 진행한다.

### 완료 조건

- route별 diff가 작을 것
- route handler가 HTTP 경계만 담당할 것
- service tests 또는 route tests 추가
- old behavior 유지

## Wave 4: 성능 계측과 index/cache

### 목적

성능 개선을 감이 아니라 metric으로 추진한다.

### 작업

| 작업 | 설명 |
|---|---|
| pipeline step metrics | 단계별 duration 기록 |
| checkpoint IO metric | bytes/ms 기록 |
| meeting metadata index 설계 | 목록/대시보드/일괄처리 성능 기반 |
| UI render metric | list render time 측정 |
| search query cache | query/filter/embedding cache 정책 |

### 완료 조건

- metric 이름 문서화
- regression threshold 정의
- 성능 개선 전후 수치 기록

## Wave 5: Observability와 오픈소스 운영

### 목적

사용자와 contributor가 문제를 재현하고 보고하기 쉽게 만든다.

### 작업

1. `meeting-transcriber doctor --json`
2. diagnostics bundle 설계
3. redaction policy
4. setup guide minimal/full 분리
5. `CONTRIBUTING.md` 첫 PR 가이드

### 완료 조건

- 민감정보 redaction 검토
- Docs-B 승인
- Security-B 승인
- PM-B final

## 병렬 운영 예시

```text
Day 1
  PM-A: Wave 1 티켓 발급
  Backend-A/B: api/dependencies.py
  Frontend-A/B: api-client.js
  Pipeline-A/B: checkpoint compact option
  Docs-A/B: STATUS 정리

Day 2
  QA-A/B: gate profile 확인
  PM-B: Wave 1 final
  PM-A: Wave 2 scope freeze

Day 3-4
  Frontend-A/B: bulk-actions.js
  Backend-A/B: meetings_batch_service.py
  Pipeline-A/B: pipeline checkpoint 분리

Day 5
  Integration gate
  STATUS update
  merge-final
```

## 중단 기준

다음 상황이면 wave를 멈춘다.

- 같은 파일에 2개 이상의 active writer가 생김
- 기본 pytest 또는 targeted gate가 원인 불명으로 깨짐
- reviewer 2명 이상이 architecture concern을 제기
- scope 외 변경이 전체 diff의 20% 이상
- 성능 개선 PR인데 측정값이 없음

## Merge 기준

```text
all required role reviews approved
AND all required gates pass
AND write_scope respected
AND docs/status updated if behavior/process changed
AND generated files excluded
```

이 조건을 만족하지 않으면 PM-B는 merge-final을 승인하지 않는다.

