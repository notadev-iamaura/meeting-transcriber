# 프로젝트 객관 평가 보고서

> 이 문서는 2026-04-30 기준 `main` 브랜치 평가로 갱신되었습니다.  
> 상세 보고서는 [`PROJECT_EVALUATION_MAIN_2026-04-30.md`](./PROJECT_EVALUATION_MAIN_2026-04-30.md)를 기준 문서로 사용합니다.

## 기준

- 기준 브랜치: `main`
- 기준 커밋: `4d7908353a34204c74042acbc65021c1742b15dc`
- `origin/main`: `4d7908353a34204c74042acbc65021c1742b15dc`
- 실행 Python: `.venv/bin/python` 3.12.8

## 수정 사유

이전 버전의 이 문서에는 작업 브랜치/중간 상태 기준의 평가가 남아 있었습니다. 해당 평가는 `main` 브랜치 재검증 결과와 달라 혼동을 줄 수 있으므로, 이 파일은 메인 브랜치 기준 요약으로 교체했습니다.

`main` 브랜치 기준 재검증 결과는 다음과 같습니다.

| 검증 | 결과 |
|---|---:|
| `ruff check .` | 통과 |
| `ruff format --check .` | 통과 |
| 핵심 unit/search/queue 테스트 | `190 passed` |
| harness 테스트 | `54 passed` |
| batch route 테스트 | `25 passed` |
| 주요 route 테스트 | `101 passed` |
| bulk actions behavior 테스트 | `29 passed` |
| bulk actions a11y 테스트 | `10 passed` |
| bulk actions visual 테스트 | `6 passed` |
| 전체 `pytest -q` | `Fatal Python error: Aborted` |

## 메인 브랜치 종합 평가

| 항목 | 점수 |
|---|---:|
| 종합 점수 | **7.6 / 10** |
| 제품 완성도 | **베타 초입-중반** |
| 기능 완성도 | **높음** |
| 현재 메인 브랜치 안정성 | **보통 이상** |
| 오픈소스 공개 준비도 | **양호하나 전체 테스트 안정화 필요** |

## 최종 결론

`main` 브랜치는 lint, formatter, route 테스트, bulk actions UI behavior/a11y/visual gate가 모두 통과하여 이전 중간 평가보다 훨씬 안정적입니다.

다만 기본 전체 `pytest -q` 실행은 `core/model_manager.py`의 MLX/GPU cache cleanup 경로에서 `Fatal Python error: Aborted`로 종료되었습니다. 따라서 현재 프로젝트는 기능 범위와 테스트 체계는 좋지만, 릴리스 안정성 측면에서는 native AI runtime과 테스트 환경의 격리가 아직 필요합니다.

상세한 카테고리별 분석, 점수 근거, 리스크, 개선 순서는 [`PROJECT_EVALUATION_MAIN_2026-04-30.md`](./PROJECT_EVALUATION_MAIN_2026-04-30.md)를 확인하세요.
