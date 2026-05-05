"""harness.consensus — 역할별 정족수 합의 테스트."""

from __future__ import annotations

import json
import sqlite3

import pytest

pytestmark = pytest.mark.harness


def _ticket(db_conn: sqlite3.Connection) -> str:
    from harness import ticket

    return ticket.open_ticket(db_conn, wave=1, component="consensus").id


def test_execute_requires_two_distinct_agents_same_scope_per_role(
    db_conn: sqlite3.Connection,
) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="execute", role="qa")
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-a",
        status="approved",
        scope_hash="H1",
    )
    assert consensus.can_execute(db_conn, ticket_id=ticket_id, scope_hash="H1") is False

    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-b",
        status="approved",
        scope_hash="H1",
    )
    assert consensus.can_execute(db_conn, ticket_id=ticket_id, scope_hash="H1") is True


def test_duplicate_agent_id_counts_once(db_conn: sqlite3.Connection) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="execute", role="qa")
    for _ in range(2):
        consensus.submit_review(
            db_conn,
            ticket_id=ticket_id,
            target="execute",
            role="qa",
            agent_id="qa-a",
            status="approved",
            scope_hash="H1",
        )

    result = consensus.status(db_conn, ticket_id=ticket_id, target="execute", scope_hash="H1")
    assert result.passed is False
    assert result.roles[0].approvals == ("qa-a",)


def test_approvals_with_different_scope_hash_do_not_combine(
    db_conn: sqlite3.Connection,
) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="execute", role="qa")
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-a",
        status="approved",
        scope_hash="H1",
    )
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-b",
        status="approved",
        scope_hash="H2",
    )

    assert consensus.can_execute(db_conn, ticket_id=ticket_id, scope_hash="H1") is False
    assert consensus.can_execute(db_conn, ticket_id=ticket_id, scope_hash="H2") is False


def test_latest_scope_does_not_reuse_stale_quorum(db_conn: sqlite3.Connection) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="execute", role="qa")
    for agent_id in ("qa-a", "qa-b"):
        consensus.submit_review(
            db_conn,
            ticket_id=ticket_id,
            target="execute",
            role="qa",
            agent_id=agent_id,
            status="approved",
            scope_hash="H1",
        )
    assert consensus.can_execute(db_conn, ticket_id=ticket_id) is True

    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-a",
        status="approved",
        scope_hash="H2",
    )

    assert consensus.can_execute(db_conn, ticket_id=ticket_id) is False


def test_min_approvals_must_be_at_least_two(db_conn: sqlite3.Connection) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)

    with pytest.raises(ValueError, match="min_approvals must be >= 2"):
        consensus.require_role(
            db_conn,
            ticket_id=ticket_id,
            target="execute",
            role="qa",
            min_approvals=1,
        )


def test_legacy_requirement_payload_with_single_approval_is_clamped(
    db_conn: sqlite3.Connection,
) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    payload = {
        "role": "qa",
        "target": "execute",
        "min_approvals": 1,
        "required": True,
    }
    db_conn.execute(
        "INSERT INTO events (ticket_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, "consensus.requirement", json.dumps(payload), "2026-05-03T00:00:00Z"),
    )
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-a",
        status="approved",
        scope_hash="H1",
    )

    result = consensus.status(db_conn, ticket_id=ticket_id, target="execute", scope_hash="H1")
    assert result.passed is False
    assert result.roles[0].min_approvals == 2


def test_unresolved_changes_requested_blocks_even_when_quorum_met(
    db_conn: sqlite3.Connection,
) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="execute", role="qa")
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-a",
        status="approved",
        scope_hash="H1",
    )
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-b",
        status="approved",
        scope_hash="H1",
    )
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-c",
        status="changes_requested",
        scope_hash="H1",
    )

    result = consensus.status(db_conn, ticket_id=ticket_id, target="execute", scope_hash="H1")
    assert result.passed is False
    assert result.roles[0].blockers == ("qa-c",)


def test_superseded_changes_requested_no_longer_blocks(db_conn: sqlite3.Connection) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="execute", role="qa")
    blocker = consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-c",
        status="changes_requested",
        scope_hash="H1",
    )
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-a",
        status="approved",
        scope_hash="H1",
    )
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-b",
        status="approved",
        scope_hash="H1",
        supersedes_event_id=blocker.event_id,
    )

    assert consensus.can_execute(db_conn, ticket_id=ticket_id, scope_hash="H1") is True


def test_pending_supersede_does_not_clear_blocker(db_conn: sqlite3.Connection) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="execute", role="qa")
    blocker = consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-c",
        status="blocker",
        scope_hash="H1",
    )
    for agent_id in ("qa-a", "qa-b"):
        consensus.submit_review(
            db_conn,
            ticket_id=ticket_id,
            target="execute",
            role="qa",
            agent_id=agent_id,
            status="approved",
            scope_hash="H1",
        )
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-d",
        status="pending",
        scope_hash="H1",
        supersedes_event_id=blocker.event_id,
    )

    result = consensus.status(db_conn, ticket_id=ticket_id, target="execute", scope_hash="H1")
    assert result.passed is False
    assert result.roles[0].blockers == ("qa-c",)


def test_different_role_approval_cannot_supersede_blocker(
    db_conn: sqlite3.Connection,
) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="execute", role="qa")
    blocker = consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-c",
        status="changes_requested",
        scope_hash="H1",
    )
    for agent_id in ("qa-a", "qa-b"):
        consensus.submit_review(
            db_conn,
            ticket_id=ticket_id,
            target="execute",
            role="qa",
            agent_id=agent_id,
            status="approved",
            scope_hash="H1",
        )
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="security",
        agent_id="sec-a",
        status="approved",
        scope_hash="H1",
        supersedes_event_id=blocker.event_id,
    )

    assert consensus.can_execute(db_conn, ticket_id=ticket_id, scope_hash="H1") is False


def test_future_event_id_cannot_be_superseded_retroactively(
    db_conn: sqlite3.Connection,
) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="execute", role="qa")
    current_event_id = db_conn.execute("SELECT MAX(id) FROM events").fetchone()[0]
    future_blocker_event_id = int(current_event_id) + 3
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-a",
        status="approved",
        scope_hash="H1",
        supersedes_event_id=future_blocker_event_id,
    )
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-b",
        status="approved",
        scope_hash="H1",
    )
    blocker = consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="execute",
        role="qa",
        agent_id="qa-c",
        status="blocker",
        scope_hash="H1",
    )
    assert blocker.event_id == future_blocker_event_id

    assert consensus.can_execute(db_conn, ticket_id=ticket_id, scope_hash="H1") is False


def test_unresolved_blocker_blocks_consensus(db_conn: sqlite3.Connection) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="merge", role="release")
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="merge",
        role="release",
        agent_id="rel-a",
        status="approved",
        scope_hash="H1",
    )
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="merge",
        role="release",
        agent_id="rel-b",
        status="approved",
        scope_hash="H1",
    )
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="merge",
        role="release",
        agent_id="rel-c",
        status="blocker",
        scope_hash="H1",
    )

    assert consensus.can_merge(db_conn, ticket_id=ticket_id, scope_hash="H1") is False


def test_execute_approvals_do_not_satisfy_merge(db_conn: sqlite3.Connection) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="merge", role="qa")
    for agent_id in ("qa-a", "qa-b"):
        consensus.submit_review(
            db_conn,
            ticket_id=ticket_id,
            target="execute",
            role="qa",
            agent_id=agent_id,
            status="approved",
            scope_hash="H1",
        )

    assert consensus.can_merge(db_conn, ticket_id=ticket_id, scope_hash="H1") is False


def test_all_required_roles_must_meet_quorum(db_conn: sqlite3.Connection) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="merge", role="qa")
    consensus.require_role(db_conn, ticket_id=ticket_id, target="merge", role="release")
    for agent_id in ("qa-a", "qa-b"):
        consensus.submit_review(
            db_conn,
            ticket_id=ticket_id,
            target="merge",
            role="qa",
            agent_id=agent_id,
            status="approved",
            scope_hash="H1",
        )
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="merge",
        role="release",
        agent_id="rel-a",
        status="approved",
        scope_hash="H1",
    )

    result = consensus.status(db_conn, ticket_id=ticket_id, target="merge", scope_hash="H1")
    assert result.passed is False
    by_role = {role.role: role for role in result.roles}
    assert by_role["qa"].passed is True
    assert by_role["release"].passed is False


def test_optional_roles_do_not_block_consensus(db_conn: sqlite3.Connection) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="merge", role="qa")
    consensus.require_role(
        db_conn,
        ticket_id=ticket_id,
        target="merge",
        role="security",
        required=False,
    )
    for agent_id in ("qa-a", "qa-b"):
        consensus.submit_review(
            db_conn,
            ticket_id=ticket_id,
            target="merge",
            role="qa",
            agent_id=agent_id,
            status="approved",
            scope_hash="H1",
        )

    assert consensus.can_merge(db_conn, ticket_id=ticket_id, scope_hash="H1") is True


def test_optional_role_blocker_still_blocks_consensus(db_conn: sqlite3.Connection) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="merge", role="qa")
    consensus.require_role(
        db_conn,
        ticket_id=ticket_id,
        target="merge",
        role="security",
        required=False,
    )
    for agent_id in ("qa-a", "qa-b"):
        consensus.submit_review(
            db_conn,
            ticket_id=ticket_id,
            target="merge",
            role="qa",
            agent_id=agent_id,
            status="approved",
            scope_hash="H1",
        )
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="merge",
        role="security",
        agent_id="sec-a",
        status="blocker",
        scope_hash="H1",
    )

    result = consensus.status(db_conn, ticket_id=ticket_id, target="merge", scope_hash="H1")
    assert result.passed is False
    by_role = {role.role: role for role in result.roles}
    assert by_role["qa"].passed is True
    assert by_role["security"].passed is False


def test_unregistered_role_blocker_still_blocks_consensus(
    db_conn: sqlite3.Connection,
) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="merge", role="qa")
    for agent_id in ("qa-a", "qa-b"):
        consensus.submit_review(
            db_conn,
            ticket_id=ticket_id,
            target="merge",
            role="qa",
            agent_id=agent_id,
            status="approved",
            scope_hash="H1",
        )
    consensus.submit_review(
        db_conn,
        ticket_id=ticket_id,
        target="merge",
        role="security",
        agent_id="sec-a",
        status="changes_requested",
        scope_hash="H1",
    )

    result = consensus.status(db_conn, ticket_id=ticket_id, target="merge", scope_hash="H1")
    assert result.passed is False
    by_role = {role.role: role for role in result.roles}
    assert by_role["qa"].passed is True
    assert by_role["security"].blockers == ("sec-a",)


def test_legacy_review_events_do_not_satisfy_can_merge(db_conn: sqlite3.Connection) -> None:
    from harness import consensus, review

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="merge", role="qa")
    review.record(
        db_conn, ticket_id=ticket_id, agent="designer-b", kind="peer-review", status="approved"
    )
    review.record(
        db_conn, ticket_id=ticket_id, agent="pm-b", kind="merge-final", status="approved"
    )

    assert consensus.can_merge(db_conn, ticket_id=ticket_id) is False


def test_malformed_review_submitted_missing_scope_hash_does_not_pass(
    db_conn: sqlite3.Connection,
) -> None:
    from harness import consensus

    ticket_id = _ticket(db_conn)
    consensus.require_role(db_conn, ticket_id=ticket_id, target="merge", role="qa")
    payload = {
        "schema_version": 1,
        "target": "merge",
        "role": "qa",
        "agent_id": "qa-a",
        "status": "approved",
    }
    db_conn.execute(
        "INSERT INTO events (ticket_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, "review.submitted", json.dumps(payload), "2026-05-03T00:00:00Z"),
    )

    result = consensus.status(db_conn, ticket_id=ticket_id, target="merge")
    assert result.passed is False
    assert result.reason == "no reviews"
