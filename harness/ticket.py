"""티켓 CRUD + 상태 전이.

티켓 id 형식: T-{wave}{NN} — Wave 1 은 T-101, T-102, ... ,
Wave 2 는 T-201, ... , Wave 3 은 T-301, ...

상태 전이 (단방향):
    pending -> design -> red -> green -> refactor -> merged -> closed
    closed 는 종착 상태이며 그 이후 변경 금지.

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §4.2, §4.4
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from harness import consensus


class InvalidStatusTransition(Exception):
    """closed 또는 비허용 상태에서의 변경 시도."""


class ConsensusIncomplete(Exception):
    """티켓 종료 전 merge consensus 가 통과하지 않은 경우."""


@dataclass(frozen=True)
class Ticket:
    """tickets 테이블 한 행의 read-only 표현."""

    id: str
    wave: int
    component: str
    status: str
    pr_number: int | None
    created_at: str
    updated_at: str


# 허용된 status 값 (db.py 의 CHECK 제약과 일치).
_VALID_STATUSES = {
    "pending",
    "design",
    "red",
    "green",
    "refactor",
    "merged",
    "closed",
}


def _now() -> str:
    """ISO-8601 UTC 타임스탬프."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _next_ticket_id(conn: sqlite3.Connection, wave: int) -> str:
    """T-{wave}{NN} 형식의 다음 id 를 발급한다.

    같은 wave 내에서 가장 큰 번호 + 1 을 사용. 시작은 01.
    """
    prefix = f"T-{wave}"
    row = conn.execute(
        "SELECT id FROM tickets WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    if row is None:
        return f"{prefix}01"
    last_n = int(row["id"][len(prefix) :])
    return f"{prefix}{last_n + 1:02d}"


def _emit_event(
    conn: sqlite3.Connection,
    ticket_id: str | None,
    type_: str,
    payload: dict | None = None,
) -> None:
    """events 테이블에 한 줄을 기록한다."""
    conn.execute(
        "INSERT INTO events (ticket_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, type_, json.dumps(payload) if payload else None, _now()),
    )


def open_ticket(
    conn: sqlite3.Connection,
    *,
    wave: int,
    component: str,
    domain: str | None = None,
    risk: str | None = None,
    write_scope: str | None = None,
) -> Ticket:
    """새 티켓을 발급하고 events 에 ticket.opened 를 기록한다."""
    if wave not in (1, 2, 3):
        raise ValueError(f"wave must be 1/2/3, got {wave!r}")
    ticket_id = _next_ticket_id(conn, wave)
    now = _now()
    conn.execute(
        "INSERT INTO tickets (id, wave, component, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (ticket_id, wave, component, now, now),
    )
    payload: dict[str, object] = {"wave": wave, "component": component}
    if domain:
        payload["domain"] = domain
    if risk:
        payload["risk"] = risk
    if write_scope:
        payload["write_scope"] = write_scope
    _emit_event(conn, ticket_id, "ticket.opened", payload)
    conn.commit()
    return Ticket(
        id=ticket_id,
        wave=wave,
        component=component,
        status="pending",
        pr_number=None,
        created_at=now,
        updated_at=now,
    )


def get_ticket(conn: sqlite3.Connection, ticket_id: str) -> Ticket | None:
    """id 로 한 건 조회. 없으면 None."""
    row = conn.execute(
        "SELECT id, wave, component, status, pr_number, created_at, updated_at "
        "FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    if row is None:
        return None
    return Ticket(**dict(row))


def list_tickets(
    conn: sqlite3.Connection,
    *,
    wave: int | None = None,
    status: str | None = None,
) -> list[Ticket]:
    """선택적으로 wave / status 로 필터링한 티켓 리스트."""
    sql = (
        "SELECT id, wave, component, status, pr_number, created_at, updated_at "
        "FROM tickets WHERE 1=1"
    )
    params: list[object] = []
    if wave is not None:
        sql += " AND wave = ?"
        params.append(wave)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    return [Ticket(**dict(r)) for r in rows]


def update_status(conn: sqlite3.Connection, ticket_id: str, new_status: str) -> None:
    """티켓 status 를 변경한다.

    closed 티켓은 변경 불가. 비허용 status 는 ValueError.
    """
    if new_status not in _VALID_STATUSES:
        raise ValueError(f"invalid status: {new_status!r}")
    current = get_ticket(conn, ticket_id)
    if current is None:
        raise ValueError(f"ticket not found: {ticket_id}")
    if current.status == "closed":
        raise InvalidStatusTransition(
            f"ticket {ticket_id} is closed; cannot change to {new_status!r}"
        )
    now = _now()
    conn.execute(
        "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, now, ticket_id),
    )
    _emit_event(
        conn,
        ticket_id,
        "status.changed",
        {"from": current.status, "to": new_status},
    )
    conn.commit()


def close_ticket(
    conn: sqlite3.Connection,
    ticket_id: str,
    *,
    pr_number: int,
    scope_hash: str | None = None,
) -> None:
    """티켓을 closed 상태로 옮기고 PR 번호를 기록한다."""
    current = get_ticket(conn, ticket_id)
    if current is None:
        raise ValueError(f"ticket not found: {ticket_id}")
    if current.status == "closed":
        raise InvalidStatusTransition(f"ticket {ticket_id} already closed")
    if not consensus.can_merge(conn, ticket_id=ticket_id, scope_hash=scope_hash):
        raise ConsensusIncomplete(
            f"ticket {ticket_id}: close requires target=merge consensus. "
            f"Run `python -m harness consensus status --ticket {ticket_id} --target merge`."
        )
    now = _now()
    conn.execute(
        "UPDATE tickets SET status = 'closed', pr_number = ?, updated_at = ? WHERE id = ?",
        (pr_number, now, ticket_id),
    )
    payload: dict[str, object] = {"pr_number": pr_number}
    if scope_hash:
        payload["scope_hash"] = scope_hash
    _emit_event(conn, ticket_id, "ticket.closed", payload)
    conn.commit()
