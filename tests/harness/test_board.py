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
