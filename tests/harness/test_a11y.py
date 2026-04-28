"""harness.a11y — axe-core 결과 기록 단위 테스트."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.harness


def test_record_a11y_run_no_violations(db_conn: sqlite3.Connection) -> None:
    from harness import a11y, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    a11y.record_run(db_conn, ticket_id=t.id, violations=[])
    row = db_conn.execute(
        "SELECT payload FROM events WHERE ticket_id = ? AND type = 'a11y.run'",
        (t.id,),
    ).fetchone()
    assert row is not None
    payload = json.loads(row["payload"])
    assert payload["violation_count"] == 0
    assert payload["passed"] is True


def test_record_a11y_run_with_violations(db_conn: sqlite3.Connection) -> None:
    from harness import a11y, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    violations = [
        {"id": "aria-current", "impact": "serious", "nodes": [{"target": [".item"]}]},
    ]
    a11y.record_run(db_conn, ticket_id=t.id, violations=violations)
    row = db_conn.execute(
        "SELECT payload FROM events WHERE ticket_id = ? AND type = 'a11y.run'",
        (t.id,),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["violation_count"] == 1
    assert payload["passed"] is False
    assert payload["violations"][0]["id"] == "aria-current"


def test_default_ruleset() -> None:
    """스펙 §5.3: wcag2a + wcag2aa + wcag21aa."""
    from harness import a11y

    assert a11y.DEFAULT_RULESET == ("wcag2a", "wcag2aa", "wcag21aa")
