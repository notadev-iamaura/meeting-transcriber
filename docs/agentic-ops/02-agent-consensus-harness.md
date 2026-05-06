# 02. 서브에이전트 합의 하네스 설계

## 목표

여러 서브에이전트를 병렬로 운영하되, 각 역할당 최소 2명 이상의 독립 판단을 거쳐 합의된 작업만 실제 변경/통합되도록 한다.

기존 `harness/`는 UI/UX overhaul용으로 ticket, review, gate, board를 제공한다. 새 하네스는 이 구조를 일반 엔지니어링 작업으로 확장한다.

## 핵심 보정: 단순 최신 리뷰 상태는 불충분하다

현재 `harness/review.py`는 `peer-review`와 `merge-final`의 최신 status만 확인한다. 이 방식은 "역할별 최소 2명 합의"를 보장하지 못한다. 일반 엔지니어링 하네스에서는 다음이 반드시 필요하다.

- 역할별 정족수
- 서로 다른 `agent_id`
- 승인 대상의 `scope_hash`
- 실행 승인과 머지 승인 분리
- unresolved `changes_requested`와 `blocker` 추적

즉, 합의는 "승인 이벤트가 있다"가 아니라 "같은 대상에 대해 필요한 역할의 서로 다른 에이전트들이 충분히 승인했다"여야 한다.

## 역할 체계

각 역할은 최소 Producer와 Reviewer를 가진다.

| 역할 | 최소 인원 | 기본 정족수 | 책임 |
|---|---:|---:|---|
| PM/Conductor | 2 | 2 | 작업 범위, 티켓 상태, 최종 진행 판단 |
| Architect | 2 | 2 | 설계 방향, 모듈 경계, 장기 유지보수성 |
| Implementer | 2 | 2 | 구현 계획/코드 변경 검토, 기존 패턴 준수 |
| QA/Test | 2 | 2 | 테스트 전략, red/green 타당성, 회귀 범위 |
| Security/Risk | 2 | 2 | 데이터/권한/네트워크/파괴적 작업 위험 검토 |
| Release/Ops | 2 | 2 | 실행 명령, CI, 배포/머지 가능성 검토 |
| Domain optional | 2 | 2 | UI/UX, ML, 성능, 문서 등 작업별 전문 검토 |

작업별로 필수 역할을 다르게 둔다. 예를 들어 문서만 바꾸는 작업은 Security/Risk가 선택 역할일 수 있지만, 네트워크/토큰/파일 삭제/권한 상승이 포함되면 필수 역할이 된다.

고위험 작업은 Reviewer를 한 명 더 둔다.

| 위험 조건 | 추가 승인 |
|---|---|
| `api/server.py` lifespan 변경 | Architect-B + QA-B |
| `core/pipeline.py` orchestration 변경 | Pipeline-B + Perf-B |
| `ui/web/spa.js` global state 변경 | Frontend-B + QA-B |
| CI gate 변경 | PM-B + QA-B |
| 보안 경계 변경 | Security-B + PM-B |

## 합의 규칙

### 실행 가능 조건

명령 실행 또는 구현 시작 조건:

1. 작업별 필수 역할이 정의되어 있어야 한다.
2. 각 필수 역할에서 서로 다른 `agent_id` 2명 이상이 같은 `scope_hash`에 대해 `target=execute` 승인.
3. 해당 scope에 unresolved `changes_requested` 또는 `blocker`가 없어야 한다.
4. 실행 명령이 등록된 계획과 일치해야 한다.
5. 파괴적 명령, 네트워크, 권한 상승은 하네스 합의와 별개로 사용자 승인이 필요하다.

### merge 가능 조건

작업 티켓은 아래 조건이 모두 만족되어야 merge 가능하다.

1. 최신 실행 gate가 pass.
2. 최신 테스트/CI 결과가 pass.
3. 모든 필수 역할에서 `target=merge` 기준 서로 다른 `agent_id` 2명 이상 승인.
4. PM/Release 역할도 각각 2명 이상 승인.
5. 모든 승인이 동일한 최종 `scope_hash`에 묶여 있음.
6. unresolved `changes_requested` 또는 `blocker`가 없음.
7. write-scope 위반 없음.

### 불일치 처리

| 상황 | 처리 |
|---|---|
| Reviewer가 `changes_requested` | Producer가 수정하거나 PM-A가 scope 조정 |
| 두 Reviewer 의견 충돌 | Architect-B 또는 PM-B가 arbitration |
| gate 실패 | 구현 에이전트로 되돌림 |
| scope 외 파일 수정 | PM-A가 분리 PR 요구 |
| 같은 파일 병렬 수정 충돌 | 더 낮은 우선순위 티켓 pause |

## 기존 하네스에서 재사용할 것

| 기존 기능 | 재사용 |
|---|---|
| `ticket open/list/show/close` | workstream ticket으로 확장 |
| `review record/status` | role-scoped review로 확장 |
| `gate run` | UI 3축 외 engineering gate 추가 |
| `board rebuild` | 전체 workstream board로 확장 |
| SQLite events table | 감사 로그로 유지 |

## 확장해야 할 데이터 모델

현재 review는 `peer-review`, `merge-final` 최신 상태만 보는 단순 모델이다. 일반 엔지니어링에는 역할별 합의가 필요하다.

### 권장 테이블 확장

```sql
ALTER TABLE tickets ADD COLUMN domain TEXT;
ALTER TABLE tickets ADD COLUMN risk TEXT;
ALTER TABLE tickets ADD COLUMN write_scope TEXT;
ALTER TABLE tickets ADD COLUMN depends_on TEXT;
```

새 테이블:

```sql
CREATE TABLE assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT NOT NULL,
  role TEXT NOT NULL,
  agent TEXT NOT NULL,
  duty TEXT NOT NULL, -- producer | reviewer | qa | final
  write_scope TEXT,
  status TEXT NOT NULL DEFAULT 'assigned',
  created_at TEXT NOT NULL
);

CREATE TABLE consensus_requirements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT NOT NULL,
  role TEXT NOT NULL,
  min_approvals INTEGER NOT NULL DEFAULT 1,
  required_duties TEXT NOT NULL
);
```

review event payload 확장:

```json
{
  "agent": "backend-b",
  "agent_id": "backend-b-001",
  "role": "backend",
  "duty": "reviewer",
  "target": "merge",
  "scope_hash": "sha256:...",
  "artifact": "api/services/settings_service.py",
  "status": "approved",
  "risk": "medium",
  "note": "route handler remains thin"
}
```

권장 `review.submitted` payload:

```json
{
  "action_id": "A-20260502-001",
  "target": "merge",
  "role": "qa",
  "agent_id": "qa-2",
  "status": "approved",
  "scope_hash": "git-diff-sha256",
  "artifact_hashes": ["plan-sha", "test-log-sha"],
  "round": 2,
  "findings": [],
  "confidence": 0.82,
  "supersedes_event_id": 123
}
```

계획, diff, 테스트 로그가 바뀌면 `scope_hash`가 바뀌므로 이전 승인은 자동 무효가 되어야 한다.

## 확장 CLI 설계

```bash
python -m harness ticket open \
  --domain backend \
  --component settings-service \
  --risk medium \
  --write-scope "api/routes.py,api/services/settings_service.py,tests/test_routes_settings.py"

python -m harness assign add \
  --ticket T-201 \
  --role backend \
  --producer backend-a \
  --reviewer backend-b

python -m harness consensus status --ticket T-201 --target execute
python -m harness consensus status --ticket T-201 --target merge
python -m harness consensus require --ticket T-201 --role backend --min-approvals 2
python -m harness gate run T-201 --profile backend
```

필수 신규 모듈:

```text
harness/
  consensus.py   # can_execute / can_merge
  artifact.py    # artifact add/hash/list
  assignment.py  # role/agent assignment
  gate_profiles.py
```

## Gate Profile

기존 UI gate는 visual/behavior/a11y 3축이다. 일반 작업에는 domain별 gate가 필요하다.

| Profile | Commands |
|---|---|
| `backend` | `ruff check .`, `ruff format --check .`, targeted route/service tests |
| `frontend` | UI behavior/a11y/visual targeted tests |
| `pipeline` | pipeline/checkpoint/steps targeted tests |
| `performance` | benchmark smoke + regression threshold |
| `docs` | link/path sanity + status consistency |
| `release` | full default pytest + UI targeted + generated file check |

## 파일 충돌 방지 규칙

1. 티켓은 반드시 `write_scope`를 선언한다.
2. 같은 wave 안에서 동일 write_scope를 가진 티켓은 동시에 실행하지 않는다.
3. `api/routes.py`, `ui/web/spa.js`, `core/pipeline.py`, `ui/web/style.css`는 고충돌 파일로 지정한다.
4. 고충돌 파일은 wave owner를 1명만 둔다.
5. 다른 에이전트는 같은 파일에 대해 read-only review만 가능하다.

## 운영 사이클

```text
PM-A ticket open
→ Architect-A/B scope review
→ Producer-A implementation
→ Producer-A self-check
→ Reviewer-B peer-review
→ QA-A gate run
→ QA-B gate review
→ PM-B merge-final
→ board rebuild
→ ticket close
```

## 최소 구현 단계

1. `assign add`로 역할/agent/duty/write_scope를 기록한다.
2. `artifact add`로 합의 대상 산출물과 hash를 기록한다.
3. `consensus require`와 `review submit`으로 execute/merge 합의를 분리한다.
4. `gate run --profile ... --scope-hash ...`로 검증 명령을 실행한다.
5. `scope check --from-git`로 write_scope 위반을 확인한다.

기존 legacy review 명령은 historical compatibility 용으로만 남긴다. 새 운영 문서는
역할 기반 consensus CLI를 기준으로 작성한다.
