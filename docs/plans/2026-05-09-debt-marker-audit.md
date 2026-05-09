# 부채 마커 감사 계획

작성일: 2026-05-09

## 목적

`TODO`, `type: ignore`, `noqa`, 빈 `pass` 같은 마커를 한 번에 제거하지 않고,
실제 위험도와 제거 가능성을 기준으로 분류한다. 목표는 "숫자 줄이기"가 아니라
유지보수자가 왜 예외가 필요한지 빠르게 판단할 수 있는 상태를 만드는 것이다.

## 현재 수량

대상 경로:

```bash
api core steps search ui security scripts
```

관측 결과:

| 분류 | 건수 | 의미 |
|---|---:|---|
| `noqa` | 137 | lint 규칙 예외. 대부분 지연 import, broad exception, 재노출 호환성이다. |
| `type: ignore` | 23 | 타입 검사 예외. 외부 ML/native 라이브러리 stub 부재가 많다. |
| 빈 `pass` | 37 | 의도적 no-op, best-effort cleanup, 테스트/벤치마크 fallback이 섞여 있다. |
| `TODO/FIXME/HACK/XXX` | 2 | 실제 후속 개선 메모. 수량은 낮다. |
| `pragma: no cover` | 2 | 버전 차이 방어 경로라 테스트로 강제하기 어려운 항목이다. |
| 합계 | 201 | 전부 결함은 아니며, 우선순위 분류가 필요하다. |

## 집중 파일

| 파일 | 건수 | 1차 판정 |
|---|---:|---|
| `api/routes.py` | 23 | router 분리 과정의 잔여 import 예외. 기능 안정 후 추가 분리 후보. |
| `core/wiki/compiler.py` | 15 | wiki 컴파일 fallback이 많아 예외 범위 축소 후보. |
| `core/wiki/lint.py` | 13 | lint 자체가 방어적으로 동작해야 하므로 유지 가능성이 높다. |
| `core/mlx_client.py` | 13 | MLX/MLX-VLM untyped import와 버전 방어. 유지 가능성이 높다. |
| `core/wiki/extractors/person.py` | 11 | 추출 실패 격리와 fallback. 일부 예외 범위 축소 후보. |
| `api/routers/wiki.py` | 11 | lazy import와 백그라운드 작업 격리. 일부 구조 분리 후보. |

## 제거하면 안 되는 항목

- 외부 라이브러리 stub 부재 때문에 필요한 `type: ignore[import-untyped]`
- public API 재노출을 보존하기 위한 `noqa: F401`
- optional native/MLX 버전 차이를 방어하는 `pragma: no cover`
- 백그라운드 브로드캐스트, best-effort cleanup, 깨진 wiki 페이지 격리처럼 실패가 전체 흐름을 막으면 안 되는 `BLE001`

## 먼저 개선할 항목

1. `type: ignore[arg-type]`, `type: ignore[return-value]`, `type: ignore[attr-defined]`
   - 외부 stub 부재가 아니라 내부 타입 표현 부족일 가능성이 있다.
   - mypy green을 유지하면서 제거 가능한지 파일별로 확인한다.
2. 빈 `pass`
   - 의도가 명확한 경우 `# 의도적 no-op` 수준의 짧은 주석을 붙인다.
   - 불필요한 경우 logging 또는 구체 예외 처리로 바꾼다.
3. `noqa: BLE001`
   - API boundary, background task, graceful degradation은 유지한다.
   - 순수 변환/파싱 내부에서는 예외 타입을 좁힐 수 있는지 본다.

## 후속 Phase 제안

### Phase 3A. 내부 타입 ignore 제거

대상:

- 완료: `steps/transcriber.py`
- 완료: `steps/diarizer.py`
- 완료: `core/job_queue.py`
- 완료: `steps/zoom_detector.py`
- 완료: `core/watcher.py`
- `api/routers/wiki.py`

검증:

```bash
.venv/bin/python -m mypy config.py api core steps search ui security --no-error-summary
```

### Phase 3B. broad exception 감사

대상:

- `core/wiki/compiler.py`
- `core/wiki/extractors/*.py`
- `api/routers/wiki.py`
- `core/ab_test_runner.py`

검증:

```bash
.venv/bin/python -m pytest tests/wiki -q
.venv/bin/python -m pytest tests/test_ab_test_runner.py -q
```

### Phase 3C. `api/routes.py` 잔여 import 예외 축소

대상:

- A/B test routes
- 남은 legacy route registration

검증:

```bash
.venv/bin/python -m pytest tests/test_server.py tests/test_api_dependencies.py -q
```

## 이번 작업의 결론

이번 라운드에서는 마커를 무리하게 제거하지 않는다. 먼저 CI 타입 게이트와 캐시 위생을
고정했고, 내부 타입 예외 일부를 제거했다. 남은 마커는 위 기준에 따라 별도 PR 단위로
줄인다.
