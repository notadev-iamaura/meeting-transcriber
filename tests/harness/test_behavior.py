"""harness.behavior — 행동 시나리오 결과 기록 단위 테스트."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.harness


def test_record_behavior_run_pass(db_conn: sqlite3.Connection) -> None:
    from harness import behavior, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    behavior.record_run(db_conn, ticket_id=t.id, passed=True, log_path=None)
    row = db_conn.execute(
        "SELECT type, payload FROM events WHERE ticket_id = ? AND type = 'behavior.run'",
        (t.id,),
    ).fetchone()
    assert row is not None
    assert '"passed": true' in row["payload"]


def test_record_behavior_run_fail_with_log(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harness import behavior, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    log = tmp_path / "behavior.log"
    log.write_text("scenario A failed at step 3\n")
    behavior.record_run(db_conn, ticket_id=t.id, passed=False, log_path=log)
    row = db_conn.execute(
        "SELECT payload FROM events WHERE ticket_id = ? AND type = 'behavior.run'",
        (t.id,),
    ).fetchone()
    assert '"passed": false' in row["payload"]
    assert str(log) in row["payload"]


def test_register_scenario_artifact(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    from harness import behavior, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    scenario_file = tmp_path / "test_x.py"
    scenario_file.write_text("# Given-When-Then\n")
    behavior.register_scenario(db_conn, ticket_id=t.id, path=scenario_file)
    row = db_conn.execute(
        "SELECT kind, author_agent FROM artifacts WHERE ticket_id = ?", (t.id,)
    ).fetchone()
    assert row["kind"] == "behavior_scenario"
    assert row["author_agent"] == "qa"
