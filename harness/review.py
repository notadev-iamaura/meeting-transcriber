"""리뷰 이벤트 기록·조회.

스키마는 변경 없음 — `events` 테이블의 type='review.<kind>' 형태로 저장한다.

이벤트 type 패턴:
    review.self-check       — Producer 의 자가 검증
    review.peer-review      — Reviewer 의 동료 검토
    review.merge-proposal   — PM-A 의 머지 제안
    review.merge-final      — PM-B 의 최종 승인

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §4.3.1
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

VALID_KINDS: tuple[str, ...] = ("self-check", "peer-review", "merge-proposal", "merge-final")
VALID_STATUSES: tuple[str, ...] = ("approved", "changes_requested", "pending")

# 머지 가능하려면 다음 2 종 모두 최신 상태가 approved 여야 함.
REQUIRED_KINDS_FOR_MERGE: tuple[str, ...] = ("peer-review", "merge-final")


def _now() -> str:
    """ISO-8601 UTC 타임스탬프."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def record(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    agent: str,
    kind: str,
    status: str,
    note: str | None = None,
) -> None:
    """리뷰 이벤트 한 건을 events 테이블에 기록한다.

    Args:
        conn: SQLite 연결
        ticket_id: 대상 티켓 id (반드시 존재해야 함, FK 제약)
        agent: 리뷰 수행 에이전트 식별자 (예: "designer-b")
        kind: VALID_KINDS 중 하나
        status: VALID_STATUSES 중 하나
        note: 선택. 변경 요청 시 위치·이유 등을 기록.

    Raises:
        ValueError: kind 또는 status 가 허용 값이 아닐 때.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    payload: dict[str, str] = {"agent": agent, "status": status}
    if note:
        payload["note"] = note
    conn.execute(
        "INSERT INTO events (ticket_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, f"review.{kind}", json.dumps(payload, ensure_ascii=False), _now()),
    )
    conn.commit()


def latest_status(
    conn: sqlite3.Connection, *, ticket_id: str, kind: str
) -> str | None:
    """주어진 kind 의 가장 최근 status 를 반환. 이벤트가 없으면 None."""
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
    row = conn.execute(
        "SELECT payload FROM events WHERE ticket_id = ? AND type = ? "
        "ORDER BY id DESC LIMIT 1",
        (ticket_id, f"review.{kind}"),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["payload"])["status"]


def all_passed(conn: sqlite3.Connection, *, ticket_id: str) -> bool:
    """머지 가능 조건: peer-review 와 merge-final 의 최신 status 가 모두 approved."""
    for kind in REQUIRED_KINDS_FOR_MERGE:
        if latest_status(conn, ticket_id=ticket_id, kind=kind) != "approved":
            return False
    return True
