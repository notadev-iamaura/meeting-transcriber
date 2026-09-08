"""일괄 접수와 실행 이벤트를 JobQueue와 같은 DB에 보존한다."""

import json
import sqlite3
from typing import Any


def initialize_history(conn: sqlite3.Connection) -> None:
    """기존 작업을 변경하지 않는 부가 테이블을 만든다."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS batch_requests (request_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS batch_events (id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, meeting_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS batch_events_request ON batch_events(request_id, id)")


def insert_receipt(conn: sqlite3.Connection, receipt: dict[str, Any], now: str) -> None:
    """호출자의 큐 트랜잭션 안에 접수 내역을 함께 삽입한다."""
    conn.execute(
        "INSERT INTO batch_requests VALUES (?, ?, ?)",
        (receipt["request_id"], now, json.dumps(receipt, ensure_ascii=False)),
    )


def read_receipts(conn: sqlite3.Connection, request_id: str | None = None) -> list[dict[str, Any]]:
    """최신 접수 30건 또는 지정 접수와 누적 실행 이벤트를 반환한다."""
    rows = (
        conn.execute("SELECT * FROM batch_requests WHERE request_id=?", (request_id,)).fetchall()
        if request_id
        else conn.execute(
            "SELECT * FROM batch_requests ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
    )
    result = []
    for row in rows:
        receipt = json.loads(row["payload"])
        receipt["created_at"] = row["created_at"]
        receipt["events"] = [
            dict(json.loads(e["payload"]), meeting_id=e["meeting_id"], created_at=e["created_at"])
            for e in conn.execute(
                "SELECT * FROM batch_events WHERE request_id=? ORDER BY id", (row["request_id"],)
            )
        ]
        result.append(receipt)
    return result
