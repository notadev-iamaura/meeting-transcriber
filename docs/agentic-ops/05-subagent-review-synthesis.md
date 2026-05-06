# 05. 서브에이전트 검토 종합

## 목적

이번 계획은 세 개의 독립 서브에이전트 검토를 받아 보강했다.

| Agent | 검토 관점 |
|---|---|
| Hooke | 코드 품질/유지보수/성능 workstream 분해 |
| Hypatia | 기존 `harness/`의 합의 모델 한계와 일반 엔지니어링 하네스 설계 |
| Bacon | Codex skill 묶음, 프로젝트 로컬/전역 설치 정책 |

## Hooke 검토 반영

### 핵심 제안

- `api/routes.py`, `ui/web/spa.js`, `ui/web/style.css`, `core/pipeline.py`가 핵심 병목.
- API 의존성/라우터 경계, 프론트 JS 모듈화, CSS 구조 분리, 검색/RAG 성능, checkpoint JSON IO 최적화는 병렬 가능.
- `api/routes.py` 분해와 `spa.js` 분해는 반드시 순차적으로 해야 한다.
- 성능 최적화는 구조 분리와 섞지 말고, 계측 PR과 최적화 PR을 분리해야 한다.

### 반영 위치

- [`03-workstream-map.md`](./03-workstream-map.md)
- [`04-execution-waves.md`](./04-execution-waves.md)

## Hypatia 검토 반영

### 핵심 제안

- 현재 `harness/review.py`는 `peer-review`와 `merge-final`의 최신 status만 보므로 역할별 2명 합의를 보장하지 못한다.
- 실행 승인과 머지 승인을 분리해야 한다.
- 승인 이벤트는 `role + agent_id + target + scope_hash`에 묶여야 한다.
- diff나 계획이 바뀌면 이전 승인은 무효가 되어야 한다.
- `consensus.py`, `artifact.py`, `assignment.py`, `gate_profiles.py`가 필요하다.

### 반영 위치

- [`02-agent-consensus-harness.md`](./02-agent-consensus-harness.md)
- [`README.md`](./README.md)

## Bacon 검토 반영

### 핵심 제안

- `AGENTS.md`를 매번 통째로 넣지 말고, 반복 작업 단위별로 작은 skill로 쪼갠다.
- 프로젝트 전용 skill은 `meeting-transcriber-` prefix를 붙인다.
- 전역 후보는 `local-ml-network-safety` 정도만 분리한다.
- 모델, 토큰, 사용자 데이터는 skill에 포함하지 않는다.
- 긴 지식은 `references/`로 분리한다.

### 반영 위치

- [`01-global-codex-skills-setup.md`](./01-global-codex-skills-setup.md)

## 종합 합의

세 검토 결과가 공통으로 가리키는 방향은 같다.

1. 바로 대형 리팩터링을 시작하지 않는다.
2. 먼저 운영 하네스와 skill을 정리한다.
3. 이후 write-scope가 겹치지 않는 작은 boundary 작업부터 병렬화한다.
4. `api/routes.py`, `spa.js`, `pipeline.py` 같은 고충돌 파일은 순차 wave로만 다룬다.
5. 합의는 "마지막 승인 이벤트"가 아니라 "동일 scope_hash에 대한 역할별 정족수"로 계산한다.

## 계획 수정 결과

이번 서브에이전트 검토 후 다음이 보강됐다.

- role quorum 기본값을 2로 명시
- `target=execute`와 `target=merge` 분리
- `scope_hash` 기반 승인 무효화 원칙 추가
- skill 이름을 `meeting-transcriber-*`로 재정리
- `local-ml-network-safety` 전역 후보 추가
- 기존 하네스의 한계와 필수 확장 모듈 명시

