"""역할별 서브에이전트 배정 이벤트.

기존 harness 스키마를 깨지 않기 위해 assignments 전용 테이블 대신
events(type='assignment.added') payload 에 기록한다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

VALID_DUTIES: tuple[str, ...] = ("producer", "reviewer", "qa", "final")


@dataclass(frozen=True)
class Assignment:
    ticket_id: str
    role: str
    agent_id: str
    duty: str
    write_scope: str | None
    created_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def add(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    role: str,
    agent_id: str,
    duty: str,
    write_scope: str | None = None,
) -> Assignment:
    """역할 배정 이벤트를 기록한다."""
    if duty not in VALID_DUTIES:
        raise ValueError(f"duty must be one of {VALID_DUTIES}, got {duty!r}")
    rows = conn.execute(
        "SELECT payload FROM events WHERE ticket_id = ? AND type = 'assignment.added'",
        (ticket_id,),
    ).fetchall()
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        if (
            payload.get("role") == role
            and payload.get("agent_id") == agent_id
            and payload.get("duty") == duty
        ):
            raise ValueError(
                f"duplicate assignment: ticket={ticket_id}, role={role}, "
                f"agent_id={agent_id}, duty={duty}"
            )
    payload: dict[str, str] = {
        "role": role,
        "agent_id": agent_id,
        "duty": duty,
    }
    if write_scope:
        payload["write_scope"] = write_scope
    created_at = _now()
    conn.execute(
        "INSERT INTO events (ticket_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, "assignment.added", json.dumps(payload, ensure_ascii=False), created_at),
    )
    conn.commit()
    return Assignment(
        ticket_id=ticket_id,
        role=role,
        agent_id=agent_id,
        duty=duty,
        write_scope=write_scope,
        created_at=created_at,
    )


def list_for_ticket(conn: sqlite3.Connection, *, ticket_id: str) -> list[Assignment]:
    """티켓에 기록된 assignment.added 이벤트를 시간순으로 반환한다."""
    rows = conn.execute(
        "SELECT payload, created_at FROM events "
        "WHERE ticket_id = ? AND type = 'assignment.added' ORDER BY id",
        (ticket_id,),
    ).fetchall()
    output: list[Assignment] = []
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        output.append(
            Assignment(
                ticket_id=ticket_id,
                role=str(payload["role"]),
                agent_id=str(payload["agent_id"]),
                duty=str(payload["duty"]),
                write_scope=payload.get("write_scope"),
                created_at=str(row["created_at"]),
            )
        )
    return output
