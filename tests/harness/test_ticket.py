"""harness.ticket — 티켓 CRUD 단위 테스트."""

from __future__ import annotations

import sqlite3

import pytest

pytestmark = pytest.mark.harness


def _record_merge_consensus(
    db_conn: sqlite3.Connection, ticket_id: str, scope_hash: str = "H1"
) -> None:
    from harness import consensus

    consensus.require_role(db_conn, ticket_id=ticket_id, target="merge", role="qa")
    for agent_id in ("qa-a", "qa-b"):
        consensus.submit_review(
            db_conn,
            ticket_id=ticket_id,
            target="merge",
            role="qa",
            agent_id=agent_id,
            status="approved",
            scope_hash=scope_hash,
        )


def test_open_ticket_assigns_id_and_status(db_conn: sqlite3.Connection) -> None:
    """open_ticket() 은 id 를 자동 발급하고 status='pending' 으로 시작한다."""
    from harness import ticket

    t = ticket.open_ticket(db_conn, wave=1, component="empty-state")
    assert t.id.startswith("T-")
    assert t.wave == 1
    assert t.component == "empty-state"
    assert t.status == "pending"
    assert t.pr_number is None


def test_open_ticket_id_format(db_conn: sqlite3.Connection) -> None:
    """티켓 id 형식: T-{wave}{NN} (Wave 1 -> T-101, T-102, ...)."""
    from harness import ticket

    t1 = ticket.open_ticket(db_conn, wave=1, component="empty-state")
    t2 = ticket.open_ticket(db_conn, wave=1, component="skeleton")
    t3 = ticket.open_ticket(db_conn, wave=2, component="cmd-palette")
    assert t1.id == "T-101"
    assert t2.id == "T-102"
    assert t3.id == "T-201"


def test_get_ticket_returns_none_when_missing(db_conn: sqlite3.Connection) -> None:
    from harness import ticket

    assert ticket.get_ticket(db_conn, "T-999") is None


def test_list_tickets_filters_by_wave_and_status(db_conn: sqlite3.Connection) -> None:
    from harness import ticket

    t1 = ticket.open_ticket(db_conn, wave=1, component="a")
    ticket.open_ticket(db_conn, wave=1, component="b")
    ticket.open_ticket(db_conn, wave=2, component="c")
    ticket.update_status(db_conn, t1.id, "design")

    wave1 = ticket.list_tickets(db_conn, wave=1)
    assert len(wave1) == 2

    designs = ticket.list_tickets(db_conn, status="design")
    assert len(designs) == 1
    assert designs[0].id == t1.id


def test_update_status_persists_and_emits_event(db_conn: sqlite3.Connection) -> None:
    from harness import ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    ticket.update_status(db_conn, t.id, "red")
    refreshed = ticket.get_ticket(db_conn, t.id)
    assert refreshed is not None
    assert refreshed.status == "red"

    events = db_conn.execute(
        "SELECT type, payload FROM events WHERE ticket_id = ? ORDER BY id", (t.id,)
    ).fetchall()
    types = [e["type"] for e in events]
    assert "ticket.opened" in types
    assert "status.changed" in types


def test_close_ticket_sets_pr_and_status(db_conn: sqlite3.Connection) -> None:
    from harness import ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    ticket.update_status(db_conn, t.id, "green")
    _record_merge_consensus(db_conn, t.id)
    ticket.close_ticket(db_conn, t.id, pr_number=42)
    refreshed = ticket.get_ticket(db_conn, t.id)
    assert refreshed is not None
    assert refreshed.status == "closed"
    assert refreshed.pr_number == 42


def test_close_ticket_requires_merge_consensus(db_conn: sqlite3.Connection) -> None:
    from harness import ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    with pytest.raises(ticket.ConsensusIncomplete, match="target=merge consensus"):
        ticket.close_ticket(db_conn, t.id, pr_number=42)

    refreshed = ticket.get_ticket(db_conn, t.id)
    assert refreshed is not None
    assert refreshed.status == "pending"
    assert refreshed.pr_number is None
    closed_events = db_conn.execute(
        "SELECT 1 FROM events WHERE ticket_id = ? AND type = 'ticket.closed'",
        (t.id,),
    ).fetchall()
    assert closed_events == []


def test_close_ticket_execute_consensus_does_not_satisfy_merge(
    db_conn: sqlite3.Connection,
) -> None:
    from harness import consensus, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    consensus.require_role(db_conn, ticket_id=t.id, target="execute", role="qa")
    for agent_id in ("qa-a", "qa-b"):
        consensus.submit_review(
            db_conn,
            ticket_id=t.id,
            target="execute",
            role="qa",
            agent_id=agent_id,
            status="approved",
            scope_hash="H1",
        )

    with pytest.raises(ticket.ConsensusIncomplete):
        ticket.close_ticket(db_conn, t.id, pr_number=42, scope_hash="H1")


def test_close_ticket_rejects_legacy_review_all_passed(db_conn: sqlite3.Connection) -> None:
    from harness import review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    review.record(
        db_conn, ticket_id=t.id, agent="designer-b", kind="peer-review", status="approved"
    )
    review.record(db_conn, ticket_id=t.id, agent="pm-b", kind="merge-final", status="approved")

    with pytest.raises(ticket.ConsensusIncomplete):
        ticket.close_ticket(db_conn, t.id, pr_number=42)


def test_close_ticket_records_scope_hash_when_consensus_passes(
    db_conn: sqlite3.Connection,
) -> None:
    import json

    from harness import ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    _record_merge_consensus(db_conn, t.id, scope_hash="H1")
    ticket.close_ticket(db_conn, t.id, pr_number=42, scope_hash="H1")

    row = db_conn.execute(
        "SELECT payload FROM events WHERE ticket_id = ? AND type = 'ticket.closed'",
        (t.id,),
    ).fetchone()
    assert row is not None
    assert json.loads(row["payload"])["scope_hash"] == "H1"


def test_invalid_status_transition_raises(db_conn: sqlite3.Connection) -> None:
    """closed 티켓은 다시 status 변경 불가."""
    from harness import ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    ticket.update_status(db_conn, t.id, "green")
    _record_merge_consensus(db_conn, t.id)
    ticket.close_ticket(db_conn, t.id, pr_number=1)
    with pytest.raises(ticket.InvalidStatusTransition):
        ticket.update_status(db_conn, t.id, "red")
