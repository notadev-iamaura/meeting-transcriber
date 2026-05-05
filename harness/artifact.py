"""계획·diff·테스트 로그 등 합의 대상 산출물 기록.

UI 하네스의 artifacts 테이블은 kind/author_agent 제약이 UI 전용이다.
일반 엔지니어링 하네스는 events(type='artifact.added') 로 산출물을 기록한다.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Artifact:
    ticket_id: str
    kind: str
    path: str
    sha256: str | None
    author_agent: str
    created_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def file_sha256(path: Path) -> str:
    """파일 내용을 sha256 hex digest 로 계산한다."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_values(values: list[str]) -> str:
    """계획, 명령, diff, 테스트 로그 해시를 안정적으로 합쳐 scope_hash 를 만든다."""
    h = hashlib.sha256()
    for value in values:
        h.update(value.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def add(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    kind: str,
    path: str,
    author_agent: str,
    sha256: str | None = None,
    compute_hash: bool = False,
) -> Artifact:
    """산출물 이벤트를 기록한다.

    compute_hash=True 이면 path 파일이 존재해야 한다.
    """
    digest = sha256
    if compute_hash:
        digest = file_sha256(Path(path))
    payload: dict[str, str] = {
        "kind": kind,
        "path": path,
        "author_agent": author_agent,
    }
    if digest:
        payload["sha256"] = digest
    created_at = _now()
    conn.execute(
        "INSERT INTO events (ticket_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, "artifact.added", json.dumps(payload, ensure_ascii=False), created_at),
    )
    conn.commit()
    return Artifact(
        ticket_id=ticket_id,
        kind=kind,
        path=path,
        sha256=digest,
        author_agent=author_agent,
        created_at=created_at,
    )


def list_for_ticket(conn: sqlite3.Connection, *, ticket_id: str) -> list[Artifact]:
    """티켓에 기록된 artifact.added 이벤트를 시간순으로 반환한다."""
    rows = conn.execute(
        "SELECT payload, created_at FROM events "
        "WHERE ticket_id = ? AND type = 'artifact.added' ORDER BY id",
        (ticket_id,),
    ).fetchall()
    output: list[Artifact] = []
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        output.append(
            Artifact(
                ticket_id=ticket_id,
                kind=str(payload["kind"]),
                path=str(payload["path"]),
                sha256=payload.get("sha256"),
                author_agent=str(payload["author_agent"]),
                created_at=str(row["created_at"]),
            )
        )
    return output
