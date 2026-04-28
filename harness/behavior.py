"""행동 시나리오 (Playwright Given-When-Then) 결과 기록.

QA 에이전트가 tests/ui/behavior/test_*.py 에 시나리오를 작성하면
gate.py 가 pytest 로 실행하고 그 결과를 본 모듈을 통해 SQLite 에 기록한다.

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §4.4, §5.3
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def register_scenario(
    conn: sqlite3.Connection, *, ticket_id: str, path: Path
) -> None:
    """QA 가 작성한 시나리오 파일을 artifacts 에 등록한다."""
    if not path.exists():
        raise FileNotFoundError(f"scenario file not found: {path}")
    conn.execute(
        "INSERT INTO artifacts (ticket_id, kind, path, author_agent, created_at) "
        "VALUES (?, 'behavior_scenario', ?, 'qa', ?)",
        (ticket_id, str(path), _now()),
    )
    conn.commit()


def record_run(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    passed: bool,
    log_path: Path | None,
) -> None:
    """행동 시나리오 실행 결과를 events 에 기록한다.

    실제 게이트 결과(visual/behavior/a11y 통합 PASS/FAIL) 는 gate.py 에서
    gate_runs 테이블에 별도 기록되며, 본 함수는 events 감사 로그용이다.
    """
    payload = {"passed": passed, "log_path": str(log_path) if log_path else None}
    conn.execute(
        "INSERT INTO events (ticket_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, "behavior.run", json.dumps(payload), _now()),
    )
    conn.commit()
