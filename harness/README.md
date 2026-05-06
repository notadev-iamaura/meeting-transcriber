# UI/UX Overhaul 풀스택 하네스

`docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md` 의 구현체.

## 빠른 시작

```bash
pip install -e ".[dev]"
playwright install chromium

# 티켓 발급
python -m harness ticket open --wave 1 --component empty-state
# T-101

# 게이트 실행 (Red — 구현 전)
python -m harness gate run T-101 --phase red

# 실행 합의 기록 (역할별 최소 2명)
python -m harness consensus require --ticket T-101 --target execute --role qa
python -m harness review submit --ticket T-101 --target execute --role qa --agent-id qa-a --status approved --scope-hash H1
python -m harness review submit --ticket T-101 --target execute --role qa --agent-id qa-b --status approved --scope-hash H1

# 게이트 실행 (Green — 구현 후, execute consensus 통과 강제)
python -m harness gate run T-101 --phase green --scope-hash H1

# 머지 합의 기록 (티켓 종료 전)
python -m harness consensus require --ticket T-101 --target merge --role qa
python -m harness review submit --ticket T-101 --target merge --role qa --agent-id qa-a --status approved --scope-hash H2
python -m harness review submit --ticket T-101 --target merge --role qa --agent-id qa-b --status approved --scope-hash H2

# 보드 재생성
python -m harness board rebuild
cat docs/superpowers/ui-ux-overhaul/00-overview.md

# 티켓 종료
python -m harness ticket close T-101 --pr 42 --scope-hash H2
```

## 8 서브에이전트 (역할당 Producer + Reviewer)

`.claude/agents/ui-ux/` 아래 8 개 정의 파일:

| 페어 | Producer (-a) | Reviewer (-b) |
|------|---------------|---------------|
| **PM** | 티켓 발급, 디스패치, 게이트 실행 | 머지 최종 승인, spec 비목표 침범 감시 |
| **Designer** | 마크다운 목업 + 시각 베이스라인 (라이트/다크/모바일) | 토큰 일관성·색 대비·다크모드 톤 검토 |
| **Frontend** | `ui/web/*` 최소 변경 구현 | 코드 리뷰 (DRY·SPA 라우터 영향·회귀 위험) |
| **QA** | Playwright 행동 시나리오 + axe-core 룰셋 | 시나리오 완전성·Red 의도성·축 분리 검토 |

크로스체크 게이트: `target=execute` consensus 가 통과해야 `phase=green` 이 실행되고,
`target=merge` consensus 가 통과해야 티켓을 닫을 수 있다. 기존
`review record/status` 명령은 historical compatibility 용으로만 유지한다.

## 환경변수

- `HARNESS_DB` — SQLite 파일 경로 (기본 `state/harness.db`)
- `HARNESS_BOARD_PATH` — 보드 마크다운 경로 (기본 `docs/superpowers/ui-ux-overhaul/00-overview.md`)

## 테스트

```bash
# 하네스 자체 단위 테스트 (45 케이스)
pytest -m harness -v

# UI 게이트 (Wave 1+ 의 시각/행동/a11y)
pytest -m ui -v
```

## 데이터 모델

`state/harness.db` (SQLite) — 4 테이블:

- `tickets` — 한 컴포넌트 = 한 티켓 (`T-{wave}{NN}` 형식)
- `artifacts` — 목업 / 베이스라인 / 시나리오 / 룰셋 / 구현 파일 레퍼런스
- `gate_runs` — 매 red/green 실행 결과
- `events` — 감사 로그 (티켓 상태 변경, 리뷰 이벤트, 게이트 실행 등)

## 알려진 제약

### 데모 잔존물

`ui/web/_demo/swatch.html`, `tests/ui/*/test_demo_swatch.py`,
`tests/ui/visual/baselines/demo-swatch-{light,dark,mobile}.png` 는 Plan 0
검증용 placeholder. **Plan 1 (Wave 1 Visual Polish) 시작 시 제거 예정**.
