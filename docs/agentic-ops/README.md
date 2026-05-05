# Agentic Operations 계획서

- 작성일: 2026-05-02
- 대상 프로젝트: Recap / meeting-transcriber
- 목적: 코드 품질·유지보수성·성능 개선 작업을 여러 Codex 서브에이전트가 병렬로 수행하되, 역할별 2인 이상 검증과 합의가 된 경우에만 실제 변경을 통합하도록 운영 체계를 세운다.

## 문서 구성

| 문서 | 목적 |
|---|---|
| [`01-global-codex-skills-setup.md`](./01-global-codex-skills-setup.md) | 필요한 Codex skill 묶음, 글로벌 설치 정책, 프로젝트 로컬 skill source 관리 방식 |
| [`02-agent-consensus-harness.md`](./02-agent-consensus-harness.md) | 역할당 최소 2명 서브에이전트, 합의 규칙, 기존 `harness/` 확장 설계 |
| [`03-workstream-map.md`](./03-workstream-map.md) | 코드 품질/유지보수/성능 개선 작업을 병렬·순차 workstream으로 분해 |
| [`04-execution-waves.md`](./04-execution-waves.md) | 실제 실행 순서, 웨이브별 산출물, merge gate, rollback 기준 |
| [`05-subagent-review-synthesis.md`](./05-subagent-review-synthesis.md) | 3개 독립 서브에이전트 검토 결과와 반영 사항 |
| [`06-consensus-harness-implementation.md`](./06-consensus-harness-implementation.md) | 최소 합의 하네스 구현 상태와 CLI 사용법 |
| [`07-consensus-integration-wave.md`](./07-consensus-integration-wave.md) | consensus를 gate/close/board/write_scope 경로에 연결한 상태 |

## 핵심 원칙

1. **한 역할은 최소 2명으로 구성한다.**  
   Producer가 산출물을 만들고 Reviewer가 독립 검증한다. 고위험 작업은 두 명의 Reviewer가 필요하다.

2. **같은 사람이 구현과 최종 승인을 동시에 하지 않는다.**  
   구현자는 self-check만 할 수 있고, peer-review와 merge-final은 다른 에이전트가 맡는다.

3. **작업은 쓰기 범위가 겹치지 않게 병렬화한다.**  
   `api/routes.py`, `ui/web/spa.js`, `core/pipeline.py`처럼 충돌 위험이 큰 파일은 wave별 단일 owner만 둔다.

4. **합의 전에는 실행·머지하지 않는다.**  
   실행 승인과 머지 승인은 분리한다. 실행은 계획된 명령을 돌려도 되는지, 머지는 최종 diff를 통합해도 되는지 판단한다.

5. **스킬은 반복 가능한 절차만 담는다.**  
   한 번 쓰고 버릴 긴 설명이 아니라, 반복 실행할 운영 절차를 `SKILL.md`로 만든다.

6. **하네스는 현재 repo의 `harness/`를 확장한다.**  
   기존 ticket/gate/review/board 개념을 유지하되, UI 전용 3축 gate를 일반 엔지니어링 gate로 확장한다.

## 공식 리서치 근거

- OpenAI Help Center의 Codex 설명에 따르면 Codex 앱은 여러 에이전트를 병렬로 운영할 수 있고, worktree, skills, automation, git 기능을 제공한다.  
  출처: [Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540-codex-in-chatgpt)

- Skills는 반복 가능한 workflow를 instruction, example, code와 함께 묶는 재사용 단위이며, Codex와 API에서도 지원된다.  
  출처: [Skills in ChatGPT](https://help.openai.com/en/articles/20001066-skills-in-chatgpt)

- Codex plugin은 하나 이상의 skill, app integration, MCP server 설정을 묶는 배포 단위이고, local skill을 대체하지 않고 보완한다.  
  출처: [Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540-codex-in-chatgpt)

## 현재 repo에서 재사용할 기반

| 기반 | 재사용 방식 |
|---|---|
| `harness/` | ticket, review, gate, board의 기본 구조 재사용 |
| `docs/superpowers/*` | 기존 8-agent UI/UX 워크플로 참고 |
| `docs/STATUS.md` | 현재 테스트/상태의 canonical source로 유지 |
| `AGENTS.md` | 프로젝트 agent onboarding의 상위 문서로 유지 |
| `.github/workflows/ci.yml` | merge gate의 CI 구현 대상으로 사용 |

## 중요한 하네스 보정 사항

현재 `harness/review.py`의 단순 `peer-review + merge-final` 최신 상태 모델은 역할별 2인 합의를 보장하지 못한다. 일반 엔지니어링 하네스에서는 다음 개념이 필요하다.

- `role`
- `agent_id`
- `target`: `execute` 또는 `merge`
- `scope_hash`: 승인 대상 계획/diff/명령의 해시
- `quorum`: 역할별 최소 승인 수, 기본 2
- unresolved `changes_requested` 또는 `blocker`

즉, 승인 이벤트가 여러 개 있다고 충분하지 않다. 같은 `scope_hash`에 대해 같은 역할의 서로 다른 에이전트 2명 이상이 승인해야 한다.

## 산출물 정책

- 계획 문서: `docs/agentic-ops/`
- 장기 운영 문서: `AGENTS.md`, `docs/STATUS.md`, `CONTRIBUTING.md`
- 하네스 코드 확장: `harness/`
- 프로젝트 로컬 skill source: `codex-skills/` 또는 `.codex/skills-src/` 중 하나로 별도 PR에서 결정
- 글로벌 설치 대상: `$CODEX_HOME/skills/<skill-name>`

글로벌 `$CODEX_HOME/skills`에 직접 쓰는 작업은 현재 repo 밖 파일 시스템 변경이므로 별도 사용자 승인 후 진행한다.

## 현재 구현 상태

2026-05-03 기준 최소 합의 하네스와 운영 경로 연결이 구현되었다.

| 영역 | 상태 |
|---|---|
| 역할 배정 이벤트 | `harness/assignment.py` 구현 |
| 산출물 기록 및 hash | `harness/artifact.py` 구현 |
| 역할별 정족수 합의 | `harness/consensus.py` 구현 |
| execute/merge target 분리 | 구현 |
| 같은 `scope_hash` 승인만 합산 | 구현 |
| 같은 `agent_id` 중복 승인 1회 카운트 | 구현 |
| 역할별 승인 하한 2명 강제 | 구현 |
| legacy/malformed requirement payload 하한 보정 | 구현 |
| unresolved `changes_requested`/`blocker` 차단 | 구현 |
| 승인 이벤트의 `supersedes_event_id` 기반 차단 해소 | 구현 |
| supersede 이벤트 순서 검증 | 구현 |
| optional/미등록 role blocker 차단 | 구현 |
| CLI | `assign`, `artifact`, `consensus`, `review submit` 추가 |
| green gate execute consensus 강제 | 구현 |
| ticket close merge consensus 강제 | 구현 |
| explicit `--scope-hash` 전달 | `gate run`, `ticket close` 구현 |
| board consensus 표시 | `E:✓/✗/— M:✓/✗/—` 구현 |
| ticket metadata 기록 | `--domain`, `--risk`, `--write-scope` 구현 |
| write_scope 검증 헬퍼 | `scope check` 구현 |
| gate profile 명령 매핑 | `backend`, `frontend`, `pipeline`, `docs`, `release` 구현 |
| git diff 기반 scope 입력 | `scope check --from-git --base-ref ...` 구현 |
| legacy review compatibility 정책 | 코드 유지, 운영 권한 없음, warning 없음 |
| 테스트 | `tests/harness` 117개 통과 |

아직 남은 작업:

- PR/merge 직전 자동 hook 에 `scope check --from-git` 연결
- legacy `review record/status` CLI는 historical compatibility 용으로 유지한다. 새 운영
  권한은 `consensus` 모델만 갖고, warning 추가나 제거는 현 단계에서 하지 않는다.
