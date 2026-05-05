# 07. Consensus 운영 경로 연결

- 구현일: 2026-05-03
- 목적: 최소 합의 reducer를 실제 하네스 운영 경로에 연결하여, 합의 없이 green gate 실행이나 ticket close가 진행되지 않게 한다.

## 연결된 경로

| 경로 | 적용 규칙 |
|---|---|
| `gate run --phase green` | `target=execute` consensus 필요 |
| `ticket close` | `target=merge` consensus 필요 |
| `board rebuild` | execute/merge consensus 상태 표시 |
| `ticket open` | `domain`, `risk`, `write_scope` 메타데이터를 `ticket.opened` 이벤트에 기록 |
| `scope check` | 선언된 `write_scope` 밖 변경 파일을 검출 |
| `gate run --profile ...` | `backend`, `frontend`, `pipeline`, `docs`, `release` 명령 매핑 실행 |

## 왜 green gate는 execute consensus인가

green gate는 검증 명령을 실행하는 단계다. 최종 merge reviewer는 보통 green gate 결과를 보고 merge 승인을 내린다. 따라서 green gate에 `target=merge`를 요구하면, 검증 결과가 나오기 전에 최종 승인을 요구하는 순환 구조가 된다.

이번 구현에서는 다음처럼 분리한다.

- `target=execute`: 계획된 명령이나 검증을 실행해도 되는지 승인
- `target=merge`: 최종 diff를 닫거나 통합해도 되는지 승인

## CLI 예시

```bash
python -m harness ticket open \
  --wave 1 \
  --component settings-service \
  --domain backend \
  --risk medium \
  --write-scope api/routes.py,tests/test_routes.py

python -m harness consensus require \
  --ticket T-101 \
  --target execute \
  --role qa

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

python -m harness gate run T-101 \
  --phase green \
  --profile ui \
  --scope-hash H1

python -m harness gate run T-101 \
  --phase red \
  --profile backend

python -m harness scope check \
  --ticket T-101 \
  --from-git \
  --base-ref origin/main

python -m harness consensus require \
  --ticket T-101 \
  --target merge \
  --role qa

python -m harness ticket close T-101 \
  --pr 42 \
  --scope-hash H2
```

## Board 표시

`harness board rebuild`는 기존 legacy review 표시를 유지하면서 새 consensus 상태를 함께 보여준다.

| 표시 | 의미 |
|---|---|
| `E:✓` | execute consensus 통과 |
| `E:✗` | execute consensus 요구사항은 있으나 blocker/정족수 부족 |
| `E:—` | execute consensus requirement 또는 review 없음 |
| `M:✓` | merge consensus 통과 |
| `M:✗` | merge consensus 요구사항은 있으나 blocker/정족수 부족 |
| `M:—` | merge consensus requirement 또는 review 없음 |

## 검증 결과

```bash
.venv/bin/python -m pytest -m harness -q
# 115 passed, 2710 deselected

.venv/bin/ruff check .
# All checks passed!

.venv/bin/ruff format --check .
# 255 files already formatted
```

## 남은 작업

1. PR/merge 직전 자동 hook 에 `scope check --from-git` 연결
2. legacy `review record/status` CLI는 historical compatibility 용으로 유지한다.
   새 운영 권한은 consensus 모델만 갖고, warning 추가나 제거는 현 단계에서 하지 않는다.
