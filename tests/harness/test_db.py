"""harness.db — 스키마 초기화·연결 헬퍼 단위 테스트."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.harness


def test_connect_creates_db_file(tmp_path: Path) -> None:
    """connect() 는 부모 디렉토리가 없어도 DB 파일을 생성한다."""
    from harness import db

    target = tmp_path / "nested" / "harness.db"
    conn = db.connect(target)
    assert target.exists()
    assert isinstance(conn, sqlite3.Connection)
    conn.close()


def test_init_schema_creates_four_tables(db_conn: sqlite3.Connection) -> None:
    """init_schema() 는 tickets / artifacts / gate_runs / events 4개 테이블을 만든다."""
    cursor = db_conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]
    assert tables == ["artifacts", "events", "gate_runs", "tickets"]


def test_init_schema_is_idempotent(db_conn: sqlite3.Connection) -> None:
    """init_schema() 는 두 번 호출해도 오류 없이 동작한다."""
    from harness import db

    db.init_schema(db_conn)  # 두 번째 호출
    cursor = db_conn.execute("SELECT count(*) FROM tickets")
    assert cursor.fetchone()[0] == 0


def test_tickets_status_constraint(db_conn: sqlite3.Connection) -> None:
    """tickets.status 는 허용된 enum 값만 받는다."""
    db_conn.execute(
        "INSERT INTO tickets (id, wave, component, status, created_at, updated_at) "
        "VALUES ('T-001', 1, 'empty-state', 'pending', '2026-04-28T00:00:00', '2026-04-28T00:00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO tickets (id, wave, component, status, created_at, updated_at) "
            "VALUES ('T-002', 1, 'x', 'INVALID_STATUS', '2026-04-28T00:00:00', '2026-04-28T00:00:00')"
        )


def test_artifacts_foreign_key(db_conn: sqlite3.Connection) -> None:
    """artifacts.ticket_id 는 존재하지 않는 티켓을 참조할 수 없다."""
    db_conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO artifacts (ticket_id, kind, path, author_agent, created_at) "
            "VALUES ('T-MISSING', 'mockup', 'docs/x.md', 'designer', '2026-04-28T00:00:00')"
        )
