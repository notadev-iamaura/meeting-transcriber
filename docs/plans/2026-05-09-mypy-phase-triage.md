# mypy Phase Triage — 계약 오류와 타입 표현 부채 분리

작성일: 2026-05-09

## 목표

`mypy`를 별도 Phase로 다룬다. 이번 Phase는 런타임 안정성 수정과 섞지 않고, 타입 오류를 두 축으로 분리해 이후 PR을 작게 유지하는 것이 목적이다.

## 현재 관측

실행 명령:

```bash
.venv/bin/python -m mypy config.py api core steps search ui security --no-error-summary
```

초기 상태에서는 `ruff`, `compileall`, 기본/타겟 pytest는 통과 가능했지만, `mypy`는 여러 모듈에서 실패했다. 실패 유형은 실제 데이터 계약 불일치와 타입 힌트 표현 부족이 섞여 있었다.

2026-05-09 처리 후 현재 상태:

```bash
.venv/bin/python -m mypy config.py api core steps search ui security --no-error-summary
# 통과
```

## A. 실제 계약 오류 후보

우선순위가 높다. 타입 체커가 런타임 계약 불일치를 가리킬 가능성이 있어, 테스트 보강과 함께 수정한다.

- `api/routers/reindex.py`: `CorrectedResult` 생성자에 잘못 전달되던 `speakers` 키워드를 제거했다. `speakers`는 `CorrectedResult.speakers` property가 발화 목록에서 계산한다.
- `api/routers/reindex.py`: `WebSocketEvent(type=...)` 호출을 `event_type=...` 계약으로 수정했다.
- `core/mlx_client.py`: MLX load 반환값과 생성 결과를 명시적으로 다뤄 타입 계약을 고정했다.
- `steps/zoom_detector.py`, `steps/recorder.py`, `core/watcher.py`: sync/async callback 루프 변수를 분리하고 등록부 타입을 명시했다.
- `core/wiki/extractors/action_item.py`, `core/wiki/extractors/person.py`: `NewActionItem`과 `OpenActionItem` 루프 변수를 분리해 상태 모델 혼선을 제거했다.

## B. 타입 표현 부채

런타임 결함 가능성은 낮지만, 점진적 타입 게이트를 막는 항목이다. 계약 오류 후보를 먼저 정리한 뒤 일괄 처리한다.

- `Any` 반환: 필요한 곳에 `cast`, `str`, `float` 변환을 명시했다.
- Optional narrowing 부족: `None` 체크와 지역 변수 바인딩으로 좁혔다.
- 외부 라이브러리 typing/stub 부족: 새 의존성 추가 없이 `yaml` import에 한정 ignore를 적용했다.
- 라이브러리 타입과 실제 허용값 차이: ChromaDB `Collection.add()` 호출부는 런타임 계약을 유지하고 타입만 좁혔다.
- FastAPI app state의 동적 속성: 지역 변수에 타입을 부여한 뒤 `app.state`에 할당하는 방식으로 정리했다.

## 권장 작업 순서

1. 완료: `api/routers/reindex.py` 계약 오류 후보 수정.
2. 완료: callback 타입 혼재(`watcher`, `zoom_detector`, `recorder`) 정리.
3. 완료: MLX client 반환 계약 타입 보강.
4. 완료: wiki extractor의 action item 상태 모델 타입 혼선 정리.
5. 완료: `Any` 반환과 외부 stub 문제를 정리해 현재 명령 기준 `mypy` green 달성.

## 남은 판단

현재 `pyproject.toml`에는 mypy 설정만 있고 CI 게이트에는 올라가 있지 않다. 다음 단계에서 `mypy config.py api core steps search ui security --no-error-summary`를 CI에 추가할지, 먼저 별도 워크플로/수동 게이트로 운영할지 결정하면 된다.
