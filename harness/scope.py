"""티켓 write_scope 선언과 변경 파일 검증."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import PurePosixPath


def _split_scope(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def _ensure_ticket_exists(conn: sqlite3.Connection, *, ticket_id: str) -> None:
    exists = conn.execute("SELECT 1 FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if exists is None:
        raise ValueError(f"ticket not found: {ticket_id}")


def declared_write_scope(conn: sqlite3.Connection, *, ticket_id: str) -> set[str]:
    """ticket.opened 와 assignment.added 이벤트에서 선언된 write_scope 를 모은다."""
    _ensure_ticket_exists(conn, ticket_id=ticket_id)
    rows = conn.execute(
        "SELECT payload FROM events "
        "WHERE ticket_id = ? AND type IN ('ticket.opened', 'assignment.added') ORDER BY id",
        (ticket_id,),
    ).fetchall()
    output: set[str] = set()
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        output.update(_split_scope(payload.get("write_scope")))
    return output


def _matches_scope(path: str, allowed: str) -> bool:
    normalized_path = PurePosixPath(path).as_posix().strip("/")
    normalized_allowed = PurePosixPath(allowed).as_posix().strip("/")
    if not normalized_allowed:
        return False
    if normalized_path == normalized_allowed:
        return True
    return normalized_path.startswith(f"{normalized_allowed.rstrip('/')}/")


def violations_for_paths(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    changed_paths: list[str],
) -> list[str]:
    """선언된 write_scope 밖에 있는 변경 파일 목록을 반환한다."""
    allowed = declared_write_scope(conn, ticket_id=ticket_id)
    if not allowed:
        return list(changed_paths)
    return [
        path
        for path in changed_paths
        if not any(_matches_scope(path, allowed_path) for allowed_path in allowed)
    ]


def within_scope(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    changed_paths: list[str],
) -> bool:
    """변경 파일이 모두 선언된 write_scope 안에 있는지 여부."""
    return not violations_for_paths(conn, ticket_id=ticket_id, changed_paths=changed_paths)


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def changed_paths_from_git(
    *,
    base_ref: str = "main",
    include_untracked: bool = True,
) -> list[str]:
    """git diff 와 ls-files 로 base_ref 대비 변경 파일 목록을 구한다."""
    diff = _run_git(["diff", "--name-only", base_ref])
    if diff.returncode != 0:
        raise RuntimeError(diff.stderr.strip() or f"git diff failed for {base_ref}")
    paths = {line.strip() for line in diff.stdout.splitlines() if line.strip()}
    if include_untracked:
        untracked = _run_git(["ls-files", "--others", "--exclude-standard"])
        if untracked.returncode != 0:
            raise RuntimeError(untracked.stderr.strip() or "git ls-files failed")
        paths.update(line.strip() for line in untracked.stdout.splitlines() if line.strip())
    return sorted(paths)
