"""harness.review — 리뷰 이벤트 기록·조회 단위 테스트."""
from __future__ import annotations

import sqlite3

import pytest

pytestmark = pytest.mark.harness


def test_record_review_event(db_conn: sqlite3.Connection) -> None:
    from harness import review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    review.record(
        db_conn,
        ticket_id=t.id,
        agent="designer-b",
        kind="peer-review",
        status="approved",
    )
    rows = db_conn.execute(
        "SELECT type, payload FROM events WHERE ticket_id = ? AND type = 'review.peer-review'",
        (t.id,),
    ).fetchall()
    assert len(rows) == 1


def test_record_review_with_note(db_conn: sqlite3.Connection) -> None:
    from harness import review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    review.record(
        db_conn,
        ticket_id=t.id,
        agent="frontend-b",
        kind="peer-review",
        status="changes_requested",
        note="ui/web/spa.js:1234 — 중복 라우터 정의",
    )
    row = db_conn.execute(
        "SELECT payload FROM events WHERE ticket_id = ? AND type = 'review.peer-review'",
        (t.id,),
    ).fetchone()
    assert "중복 라우터 정의" in row["payload"]
    assert "frontend-b" in row["payload"]


def test_invalid_status_raises(db_conn: sqlite3.Connection) -> None:
    from harness import review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    with pytest.raises(ValueError, match="status must be"):
        review.record(
            db_conn, ticket_id=t.id, agent="qa-b", kind="peer-review", status="maybe",
        )


def test_invalid_kind_raises(db_conn: sqlite3.Connection) -> None:
    from harness import review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    with pytest.raises(ValueError, match="kind must be"):
        review.record(
            db_conn, ticket_id=t.id, agent="qa-b", kind="weird-thing", status="approved",
        )


def test_latest_status_for_kind(db_conn: sqlite3.Connection) -> None:
    """가장 최근 review.peer-review 이벤트의 status 를 조회."""
    from harness import review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    review.record(db_conn, ticket_id=t.id, agent="designer-b", kind="peer-review", status="changes_requested")
    review.record(db_conn, ticket_id=t.id, agent="designer-b", kind="peer-review", status="approved")
    assert review.latest_status(db_conn, ticket_id=t.id, kind="peer-review") == "approved"


def test_all_reviews_passed(db_conn: sqlite3.Connection) -> None:
    """all_passed() 는 모든 필수 review 종류가 approved 일 때만 True."""
    from harness import review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    assert review.all_passed(db_conn, ticket_id=t.id) is False
    review.record(db_conn, ticket_id=t.id, agent="designer-b", kind="peer-review", status="approved")
    review.record(db_conn, ticket_id=t.id, agent="qa-b", kind="peer-review", status="approved")
    review.record(db_conn, ticket_id=t.id, agent="frontend-b", kind="peer-review", status="approved")
    assert review.all_passed(db_conn, ticket_id=t.id) is False
    review.record(db_conn, ticket_id=t.id, agent="pm-b", kind="merge-final", status="approved")
    assert review.all_passed(db_conn, ticket_id=t.id) is True
