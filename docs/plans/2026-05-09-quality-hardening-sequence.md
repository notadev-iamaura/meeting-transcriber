# 품질 하드닝 순차 진행 계획

작성일: 2026-05-09

## 목표

현재 `main`은 테스트와 CI 기준으로 안정적인 상태다. 이 문서는 남은 품질 개선 항목을
작은 단위로 나누어, 기능 변경과 섞지 않고 점진적으로 처리하기 위한 실행 계획이다.

## 운영 원칙

- 각 단계는 문서화, 최소 수정, 검증 순서로 진행한다.
- 저장소에 추적되지 않는 로컬 산출물은 `.gitignore` 적용 여부를 확인한 뒤 삭제한다.
- 런타임 동작을 바꾸는 리팩터링은 별도 Phase와 PR로 분리한다.
- `mypy`, `ruff`, targeted pytest 중 해당 변경을 증명하는 가장 작은 게이트를 우선 실행한다.

## Phase 1. CI 타입 게이트 고정

### 문제

`mypy`는 로컬에서 green이지만, GitHub Actions 필수 게이트에는 아직 포함되어 있지 않다.
따라서 이후 PR에서 타입 계약 오류가 다시 들어와도 CI가 자동으로 막지 못한다.

### 수정 범위

- `.github/workflows/ci.yml`
  - `actions/checkout`과 `actions/setup-python`을 Node 24 기반 버전으로 갱신한다.
  - `mypy config.py api core steps search ui security --no-error-summary`를 별도 job으로 추가한다.

### 검증

```bash
.venv/bin/python -m mypy config.py api core steps search ui security --no-error-summary
```

## Phase 2. 로컬 캐시 위생 정리

### 문제

`__pycache__`와 `.pyc`는 Git에는 추적되지 않지만, 로컬 작업 디렉터리에 남아 있으면 검색,
패키징 점검, 파일 스캔에서 잡음을 만든다.

### 수정 범위

- `.gitignore`에 이미 `__pycache__/`, `*.pyc`가 포함되어 있으므로 정책 추가는 필요 없다.
- 프로젝트 소스/테스트 영역에 남은 로컬 캐시만 삭제한다.
- `.venv` 내부 캐시는 가상환경 자체 산출물이므로 건드리지 않는다.

### 검증

```bash
git ls-files '*__pycache__*' '*.pyc'
find . -path './.venv' -prune -o -type d -name __pycache__ -print
find . -path './.venv' -prune -o -name '*.pyc' -print
```

## Phase 3. 부채 마커 분류

### 문제

`TODO`, `FIXME`, `HACK`, `type: ignore`, `noqa`, `pragma: no cover`, 빈 `pass`는 모두
나쁜 코드는 아니지만, 의도와 제거 조건이 불명확하면 유지보수 부채가 된다.

### 분류 기준

- 유지: 외부 라이브러리 타입 부재, 의도적 fallback, 테스트용 no-op처럼 사유가 명확한 항목.
- 보강: 유지해야 하지만 주석이 부족한 항목.
- 제거: 타입/린트 green 이후 더 이상 필요 없는 ignore, 실제로 죽은 코드.
- 후속 Phase: 구조 분리나 테스트 보강이 필요한 항목.

### 우선순위

1. `type: ignore` 중 외부 라이브러리 stub 부재가 아닌 항목.
2. `noqa: BLE001` 중 실제 예외 범위를 좁힐 수 있는 항목.
3. 빈 `pass` 중 명시적 no-op 주석이 없는 항목.
4. 오래된 TODO/FIXME.

## Phase 4. native/MLX/watchdog smoke gate

### 문제

기본 테스트는 강하지만 실제 macOS native window, watchdog/FSEvents, MLX 모델 로드,
오디오 장치 연동은 CI에서 완전 재현하기 어렵다.

### 방향

- 기본 CI에는 넣지 않는다.
- 릴리스 전 수동/optional smoke checklist로 분리한다.
- runtime profile별 시작/종료 테스트를 먼저 자동화한다.

## Phase 5. 프론트엔드 정적 검증

### 문제

순수 JS SPA는 의존성이 적고 빠르지만, 규모가 커질수록 API 응답 필드 오타나 컨트롤러 간
계약 깨짐을 실행 전에는 잡기 어렵다.

### 방향

- 당장 TypeScript 전면 전환은 하지 않는다.
- 먼저 JS 파일 경계, API 응답 계약, 브라우저 behavior 테스트를 보강한다.
- 필요 시 `eslint` 또는 점진적 `checkJs`를 별도 Phase에서 검토한다.

## 현재 Phase 판정

이번 작업에서는 Phase 1과 Phase 2를 수행하고, Phase 3A의 저위험 내부 타입 예외 일부를
함께 제거한다. Phase 3B 이후는 코드 의미 판단이 필요한 작업이므로 별도 PR에서 작은
묶음으로 처리한다.
