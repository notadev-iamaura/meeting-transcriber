"""assignment/artifact event helpers."""

from __future__ import annotations

import sqlite3

import pytest

pytestmark = pytest.mark.harness


def _ticket(db_conn: sqlite3.Connection) -> str:
    from harness import ticket

    return ticket.open_ticket(db_conn, wave=1, component="artifact").id


def test_assignment_add_and_list(db_conn: sqlite3.Connection) -> None:
    from harness import assignment

    ticket_id = _ticket(db_conn)
    assignment.add(
        db_conn,
        ticket_id=ticket_id,
        role="backend",
        agent_id="backend-a",
        duty="producer",
        write_scope="api/routes.py",
    )

    rows = assignment.list_for_ticket(db_conn, ticket_id=ticket_id)
    assert len(rows) == 1
    assert rows[0].role == "backend"
    assert rows[0].write_scope == "api/routes.py"


def test_assignment_duplicate_rejected(db_conn: sqlite3.Connection) -> None:
    from harness import assignment

    ticket_id = _ticket(db_conn)
    assignment.add(
        db_conn,
        ticket_id=ticket_id,
        role="qa",
        agent_id="qa-a",
        duty="reviewer",
    )
    with pytest.raises(ValueError, match="duplicate assignment"):
        assignment.add(
            db_conn,
            ticket_id=ticket_id,
            role="qa",
            agent_id="qa-a",
            duty="reviewer",
        )


def test_artifact_add_with_computed_hash(db_conn: sqlite3.Connection, tmp_path) -> None:
    from harness import artifact

    ticket_id = _ticket(db_conn)
    path = tmp_path / "plan.md"
    path.write_text("plan", encoding="utf-8")

    recorded = artifact.add(
        db_conn,
        ticket_id=ticket_id,
        kind="plan",
        path=str(path),
        author_agent="pm-a",
        compute_hash=True,
    )
    rows = artifact.list_for_ticket(db_conn, ticket_id=ticket_id)
    assert rows[0].sha256 == recorded.sha256
    assert rows[0].kind == "plan"


def test_artifact_hash_values_changes_with_order() -> None:
    from harness import artifact

    assert artifact.hash_values(["a", "b"]) != artifact.hash_values(["b", "a"])
