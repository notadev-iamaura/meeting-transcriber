"""harness.board — 마크다운 진행 보드 생성 단위 테스트."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.harness


def test_render_overview_lists_all_tickets(db_conn: sqlite3.Connection) -> None:
    from harness import board, ticket

    ticket.open_ticket(db_conn, wave=1, component="empty-state")
    t2 = ticket.open_ticket(db_conn, wave=2, component="cmd-palette")
    ticket.update_status(db_conn, t2.id, "design")

    md = board.render_overview(db_conn)
    assert "# UI/UX Overhaul — 진행 보드" in md
    assert "T-101" in md
    assert "empty-state" in md
    assert "T-201" in md
    assert "cmd-palette" in md
    assert "design" in md


def test_render_overview_groups_by_wave(db_conn: sqlite3.Connection) -> None:
    from harness import board, ticket

    ticket.open_ticket(db_conn, wave=1, component="a")
    ticket.open_ticket(db_conn, wave=3, component="b")

    md = board.render_overview(db_conn)
    assert "## Wave 1 · Visual Polish" in md
    assert "## Wave 3 · Accessibility & Mobile" in md
    # Wave 2 도 헤더는 표시되어야 한다 (티켓 0개여도)
    assert "## Wave 2 · Interaction & Focus" in md


def test_write_overview_creates_file(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    from harness import board, ticket

    ticket.open_ticket(db_conn, wave=1, component="x")
    target = tmp_path / "00-overview.md"
    board.write_overview(db_conn, target)
    assert target.exists()
    content = target.read_text()
    assert "T-101" in content


def test_board_shows_review_status(db_conn: sqlite3.Connection) -> None:
    from harness import board, review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    review.record(
        db_conn, ticket_id=t.id, agent="designer-b", kind="peer-review", status="changes_requested"
    )
    md = board.render_overview(db_conn)
    # 보드에 리뷰 컬럼이 있고 거부 표시가 보여야 함
    assert "리뷰" in md
    assert "✗" in md or "❌" in md or "changes" in md.lower()


def test_board_shows_execute_and_merge_consensus_status(
    db_conn: sqlite3.Connection,
) -> None:
    from harness import board, consensus, ticket

    passed = ticket.open_ticket(db_conn, wave=1, component="passed")
    failed = ticket.open_ticket(db_conn, wave=1, component="failed")
    incomplete = ticket.open_ticket(db_conn, wave=1, component="incomplete")

    consensus.require_role(db_conn, ticket_id=passed.id, target="execute", role="qa")
    consensus.require_role(db_conn, ticket_id=passed.id, target="merge", role="qa")
    for target in ("execute", "merge"):
        for agent_id in ("qa-a", "qa-b"):
            consensus.submit_review(
                db_conn,
                ticket_id=passed.id,
                target=target,
                role="qa",
                agent_id=f"{target}-{agent_id}",
                status="approved",
                scope_hash="H1",
            )

    consensus.require_role(db_conn, ticket_id=failed.id, target="execute", role="qa")
    consensus.submit_review(
        db_conn,
        ticket_id=failed.id,
        target="execute",
        role="qa",
        agent_id="qa-a",
        status="blocker",
        scope_hash="H1",
    )

    consensus.require_role(db_conn, ticket_id=incomplete.id, target="execute", role="qa")

    md = board.render_overview(db_conn)
    assert "합의" in md
    assert f"| {passed.id} | `passed`" in md
    assert "E:✓ M:✓" in md
    assert f"| {failed.id} | `failed`" in md
    assert "E:✗ M:—" in md
    assert f"| {incomplete.id} | `incomplete`" in md
    assert "E:— M:—" in md


def test_board_shows_latest_profile_gate_summary(db_conn: sqlite3.Connection) -> None:
    import json

    from harness import board, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="backend")
    db_conn.execute(
        "INSERT INTO events (ticket_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (
            t.id,
            "gate.profile",
            json.dumps({"phase": "red", "profile": "backend", "passed": False}),
            "2026-05-03T00:00:00Z",
        ),
    )

    md = board.render_overview(db_conn)

    assert "red:backend ✗" in md


def test_board_uses_newer_ui_gate_over_stale_profile_event(
    db_conn: sqlite3.Connection,
) -> None:
    import json

    from harness import board, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="backend")
    db_conn.execute(
        "INSERT INTO events (ticket_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (
            t.id,
            "gate.profile",
            json.dumps({"phase": "red", "profile": "backend", "passed": False}),
            "2026-05-03T00:00:00Z",
        ),
    )
    db_conn.execute(
        "INSERT INTO gate_runs ("
        "ticket_id, phase, visual_pass, behavior_pass, a11y_pass, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        (t.id, "green", 1, 1, 1, "2026-05-03T00:01:00Z"),
    )

    md = board.render_overview(db_conn)

    assert "green: V✓ B✓ A✓" in md
    assert "red:backend ✗" not in md
