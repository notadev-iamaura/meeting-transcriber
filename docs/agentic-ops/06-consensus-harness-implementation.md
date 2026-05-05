# 06. 최소 합의 하네스 구현 기록

- 구현일: 2026-05-02
- 최종 보정: 2026-05-03
- 목적: 역할당 최소 2명 이상의 서브에이전트가 같은 `scope_hash`에 대해 승인한 경우에만 실행/머지 가능 여부를 판단하는 최소 하네스 구현

## 구현 파일

| 파일 | 역할 |
|---|---|
| `harness/assignment.py` | `assignment.added` 이벤트로 역할/agent/duty/write_scope 배정 기록 |
| `harness/artifact.py` | `artifact.added` 이벤트와 파일/값 hash helper |
| `harness/consensus.py` | `consensus.requirement`, `review.submitted` 이벤트 기반 합의 reducer |
| `harness/cli.py` | `assign`, `artifact`, `consensus`, `review submit` CLI 추가 |
| `tests/harness/test_consensus.py` | 정족수, scope, target, blocker 규칙 테스트 |
| `tests/harness/test_assignment_artifact.py` | assignment/artifact helper 테스트 |
| `tests/harness/test_cli.py` | CLI happy path와 execute/merge 분리 테스트 |

## 구현된 합의 규칙

### 1. 역할별 정족수

`consensus.require --role qa --min-approvals 2`처럼 역할별 최소 승인 수를 등록한다. 기본값은 2이며, 1명 정족수는 CLI와 Python API 모두에서 거부한다. 이전 버전이나 수동 삽입으로 저장된 `min_approvals: 1` payload도 reducer가 2로 보정해서 계산한다.

### 2. 서로 다른 `agent_id`만 카운트

같은 `agent_id`가 여러 번 승인해도 승인 수는 1명으로 계산한다.

### 3. 같은 `scope_hash`만 합산

`H1`에 대한 승인과 `H2`에 대한 승인은 섞지 않는다. 계획, 명령, diff, 테스트 로그가 바뀌면 새 `scope_hash`를 써야 하며 이전 승인은 자동으로 현재 합의 대상에서 제외된다.

`scope_hash`를 생략한 상태 조회는 최신 review 이벤트의 scope를 기준으로 계산한다. 따라서 `H1`에서 이미 정족수를 만족했더라도 `H2` 리뷰가 새로 기록되면, `H2`에 대해 다시 2명 이상이 승인해야 통과한다.

### 4. `execute`와 `merge` 분리

`target=execute` 승인은 `target=merge` 승인으로 쓰이지 않는다. 반대도 마찬가지다.

### 5. unresolved blocker 차단

같은 `ticket_id + target + scope_hash`에 `changes_requested` 또는 `blocker`가 있으면 승인 정족수가 충족되어도 실패한다. 이 차단은 required role뿐 아니라 optional role과 미등록 role에도 적용된다.

### 6. 명시적 supersede

`changes_requested` 또는 `blocker`는 이후 승인 이벤트가 `supersedes_event_id`로 해당 이벤트를 명시해야 해소된다. 단순히 나중에 approve를 기록했다고 자동 해소하지 않는다.

해소 이벤트는 반드시 같은 `ticket_id + target + scope_hash + role`의 `approved` 이벤트여야 한다. `pending`, `changes_requested`, 다른 role의 `approved` 이벤트는 기존 blocker를 해소하지 못한다. 또한 해소 이벤트의 event id가 차단 이벤트보다 커야 하므로, 미래 event id를 미리 지정해 나중 차단을 소급 해소할 수 없다.

## CLI 예시

```bash
python -m harness ticket open --wave 1 --component settings-service

python -m harness assign add \
  --ticket T-101 \
  --role backend \
  --agent-id backend-a \
  --duty producer \
  --write-scope api/routes.py,api/services/settings_service.py

python -m harness consensus require \
  --ticket T-101 \
  --target execute \
  --role qa \
  --min-approvals 2

python -m harness review submit \
  --ticket T-101 \
  --target execute \
  --role qa \
  --agent-id qa-a \
  --status approved \
  --scope-hash H1

python -m harness review submit \
  --ticket T-101 \
  --target execute \
  --role qa \
  --agent-id qa-b \
  --status approved \
  --scope-hash H1

python -m harness consensus status \
  --ticket T-101 \
  --target execute \
  --scope-hash H1
```

## 검증 결과

```bash
.venv/bin/ruff check harness tests/harness
# All checks passed!

.venv/bin/ruff format --check harness tests/harness
# 27 files already formatted

.venv/bin/python -m pytest tests/harness -q
# 96 passed
```

## 운영 경로 연결

최소 합의 reducer 이후 운영 경로 연결은
[`07-consensus-integration-wave.md`](./07-consensus-integration-wave.md)에 정리한다.

legacy `review record/status` CLI는 historical compatibility 용으로 유지한다. 다만
green gate, ticket close, board consensus 판정의 운영 권한은 consensus 모델만 갖는다.
