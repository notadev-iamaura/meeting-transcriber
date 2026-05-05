"""역할별 정족수 기반 합의 계산.

합의는 같은 target(execute/merge)과 같은 scope_hash 에 대해 계산한다.
diff, 계획, 테스트 로그가 바뀌어 scope_hash 가 바뀌면 이전 승인은 자동으로
현재 합의 대상에서 제외된다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

VALID_TARGETS: tuple[str, ...] = ("execute", "merge")
VALID_STATUSES: tuple[str, ...] = ("approved", "changes_requested", "blocker", "pending")
DEFAULT_MIN_APPROVALS = 2


@dataclass(frozen=True)
class Requirement:
    role: str
    target: str
    min_approvals: int = DEFAULT_MIN_APPROVALS
    required: bool = True


@dataclass(frozen=True)
class RoleConsensus:
    role: str
    target: str
    scope_hash: str
    required: bool
    min_approvals: int
    approvals: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def passed(self) -> bool:
        if self.blockers:
            return False
        if not self.required:
            return True
        return len(self.approvals) >= self.min_approvals


@dataclass(frozen=True)
class ConsensusResult:
    ticket_id: str
    target: str
    scope_hash: str | None
    roles: tuple[RoleConsensus, ...]
    reason: str | None = None

    @property
    def passed(self) -> bool:
        return (
            self.scope_hash is not None and not self.reason and all(r.passed for r in self.roles)
        )


@dataclass(frozen=True)
class SubmittedReview:
    event_id: int
    ticket_id: str
    target: str
    role: str
    agent_id: str
    status: str
    scope_hash: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _validate_target(target: str) -> None:
    if target not in VALID_TARGETS:
        raise ValueError(f"target must be one of {VALID_TARGETS}, got {target!r}")


def _validate_status(status: str) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")


def require_role(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    role: str,
    target: str,
    min_approvals: int = DEFAULT_MIN_APPROVALS,
    required: bool = True,
) -> Requirement:
    """특정 target 에 필요한 역할 정족수를 기록한다."""
    _validate_target(target)
    if min_approvals < DEFAULT_MIN_APPROVALS:
        raise ValueError(f"min_approvals must be >= {DEFAULT_MIN_APPROVALS}")
    exists = conn.execute("SELECT 1 FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if exists is None:
        raise ValueError(f"ticket not found: {ticket_id}")
    payload = {
        "role": role,
        "target": target,
        "min_approvals": min_approvals,
        "required": required,
    }
    conn.execute(
        "INSERT INTO events (ticket_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, "consensus.requirement", json.dumps(payload, ensure_ascii=False), _now()),
    )
    conn.commit()
    return Requirement(
        role=role,
        target=target,
        min_approvals=min_approvals,
        required=required,
    )


def submit_review(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    target: str,
    role: str,
    agent_id: str,
    status: str,
    scope_hash: str,
    duty: str | None = None,
    action_id: str | None = None,
    note: str | None = None,
    artifact_hashes: list[str] | None = None,
    confidence: float | None = None,
    round_no: int | None = None,
    supersedes_event_id: int | None = None,
) -> SubmittedReview:
    """역할 기반 review.submitted 이벤트를 기록한다."""
    _validate_target(target)
    _validate_status(status)
    exists = conn.execute("SELECT 1 FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if exists is None:
        raise ValueError(f"ticket not found: {ticket_id}")
    payload: dict[str, object] = {
        "schema_version": 1,
        "target": target,
        "role": role,
        "agent_id": agent_id,
        "status": status,
        "scope_hash": scope_hash,
    }
    if duty:
        payload["duty"] = duty
    if action_id:
        payload["action_id"] = action_id
    if note:
        payload["note"] = note
    if artifact_hashes:
        payload["artifact_hashes"] = artifact_hashes
    if confidence is not None:
        payload["confidence"] = confidence
    if round_no is not None:
        payload["round"] = round_no
    if supersedes_event_id is not None:
        payload["supersedes_event_id"] = supersedes_event_id
    cur = conn.execute(
        "INSERT INTO events (ticket_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, "review.submitted", json.dumps(payload, ensure_ascii=False), _now()),
    )
    conn.commit()
    return SubmittedReview(
        event_id=int(cur.lastrowid),
        ticket_id=ticket_id,
        target=target,
        role=role,
        agent_id=agent_id,
        status=status,
        scope_hash=scope_hash,
    )


def _requirements(conn: sqlite3.Connection, *, ticket_id: str, target: str) -> list[Requirement]:
    rows = conn.execute(
        "SELECT payload FROM events "
        "WHERE ticket_id = ? AND type = 'consensus.requirement' ORDER BY id",
        (ticket_id,),
    ).fetchall()
    latest: dict[tuple[str, str], Requirement] = {}
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        if payload.get("target") != target:
            continue
        req = Requirement(
            role=str(payload["role"]),
            target=str(payload["target"]),
            min_approvals=max(
                int(payload.get("min_approvals", DEFAULT_MIN_APPROVALS)),
                DEFAULT_MIN_APPROVALS,
            ),
            required=bool(payload.get("required", True)),
        )
        latest[(req.target, req.role)] = req
    return list(latest.values())


def _latest_scope_hash(conn: sqlite3.Connection, *, ticket_id: str, target: str) -> str | None:
    row = conn.execute(
        "SELECT payload FROM events "
        "WHERE ticket_id = ? AND type = 'review.submitted' ORDER BY id DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"] or "{}")
    if payload.get("target") != target:
        return _latest_scope_hash_filtered(conn, ticket_id=ticket_id, target=target)
    scope_hash = payload.get("scope_hash")
    if scope_hash is None:
        return _latest_scope_hash_filtered(conn, ticket_id=ticket_id, target=target)
    return str(scope_hash)


def _latest_scope_hash_filtered(
    conn: sqlite3.Connection, *, ticket_id: str, target: str
) -> str | None:
    rows = conn.execute(
        "SELECT payload FROM events "
        "WHERE ticket_id = ? AND type = 'review.submitted' ORDER BY id DESC",
        (ticket_id,),
    ).fetchall()
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        scope_hash = payload.get("scope_hash")
        if payload.get("target") == target and scope_hash is not None:
            return str(scope_hash)
    return None


def _superseded_event_ids(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    target: str,
    scope_hash: str,
) -> set[int]:
    rows = conn.execute(
        "SELECT id, payload FROM events "
        "WHERE ticket_id = ? AND type = 'review.submitted' ORDER BY id",
        (ticket_id,),
    ).fetchall()
    payloads_by_id: dict[int, dict[str, object]] = {}
    for row in rows:
        payloads_by_id[int(row["id"])] = json.loads(row["payload"] or "{}")

    output: set[int] = set()
    for row in rows:
        payload = payloads_by_id[int(row["id"])]
        if payload.get("target") != target or payload.get("scope_hash") != scope_hash:
            continue
        if payload.get("status") != "approved":
            continue
        supersedes = payload.get("supersedes_event_id")
        if supersedes is None:
            continue
        superseded_payload = payloads_by_id.get(int(supersedes))
        if superseded_payload is None:
            continue
        if (
            int(row["id"]) > int(supersedes)
            and superseded_payload.get("target") == target
            and superseded_payload.get("scope_hash") == scope_hash
            and superseded_payload.get("role") == payload.get("role")
            and superseded_payload.get("status") in {"changes_requested", "blocker"}
        ):
            output.add(int(supersedes))
    return output


def status(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    target: str,
    scope_hash: str | None = None,
) -> ConsensusResult:
    """역할별 정족수 합의 상태를 계산한다."""
    _validate_target(target)
    requirements = _requirements(conn, ticket_id=ticket_id, target=target)
    if not requirements:
        return ConsensusResult(
            ticket_id=ticket_id,
            target=target,
            scope_hash=scope_hash,
            roles=(),
            reason="no requirements",
        )
    effective_scope = scope_hash or _latest_scope_hash(conn, ticket_id=ticket_id, target=target)
    if effective_scope is None:
        return ConsensusResult(
            ticket_id=ticket_id,
            target=target,
            scope_hash=None,
            roles=(),
            reason="no reviews",
        )

    rows = conn.execute(
        "SELECT id, payload FROM events "
        "WHERE ticket_id = ? AND type = 'review.submitted' ORDER BY id",
        (ticket_id,),
    ).fetchall()
    superseded = _superseded_event_ids(
        conn, ticket_id=ticket_id, target=target, scope_hash=effective_scope
    )
    latest_by_agent: dict[tuple[str, str], str] = {}
    unresolved_blockers_by_role: dict[str, set[str]] = {}
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        if payload.get("target") != target or payload.get("scope_hash") != effective_scope:
            continue
        event_id = int(row["id"])
        role_value = payload.get("role")
        agent_value = payload.get("agent_id")
        status_value = payload.get("status")
        if role_value is None or agent_value is None or status_value is None:
            continue
        role = str(role_value)
        agent_id = str(agent_value)
        latest_by_agent[(role, agent_id)] = str(status_value)
        if (
            payload.get("status") in {"changes_requested", "blocker"}
            and event_id not in superseded
        ):
            unresolved_blockers_by_role.setdefault(role, set()).add(agent_id)

    role_results: list[RoleConsensus] = []
    required_roles = {req.role for req in requirements}
    for req in requirements:
        approvals = sorted(
            agent_id
            for (role, agent_id), latest_status in latest_by_agent.items()
            if role == req.role and latest_status == "approved"
        )
        blockers = sorted(unresolved_blockers_by_role.get(req.role, set()))
        role_results.append(
            RoleConsensus(
                role=req.role,
                target=target,
                scope_hash=effective_scope,
                required=req.required,
                min_approvals=req.min_approvals,
                approvals=tuple(approvals),
                blockers=tuple(blockers),
            )
        )
    for role in sorted(set(unresolved_blockers_by_role) - required_roles):
        role_results.append(
            RoleConsensus(
                role=role,
                target=target,
                scope_hash=effective_scope,
                required=True,
                min_approvals=DEFAULT_MIN_APPROVALS,
                approvals=(),
                blockers=tuple(sorted(unresolved_blockers_by_role[role])),
            )
        )

    return ConsensusResult(
        ticket_id=ticket_id,
        target=target,
        scope_hash=effective_scope,
        roles=tuple(role_results),
    )


def can_execute(
    conn: sqlite3.Connection, *, ticket_id: str, scope_hash: str | None = None
) -> bool:
    """target=execute 합의 통과 여부."""
    return status(conn, ticket_id=ticket_id, target="execute", scope_hash=scope_hash).passed


def can_merge(conn: sqlite3.Connection, *, ticket_id: str, scope_hash: str | None = None) -> bool:
    """target=merge 합의 통과 여부."""
    return status(conn, ticket_id=ticket_id, target="merge", scope_hash=scope_hash).passed
