"""harness.scope — write_scope 선언/검증 테스트."""

from __future__ import annotations

import sqlite3
from subprocess import CompletedProcess

import pytest

pytestmark = pytest.mark.harness


def test_ticket_open_write_scope_is_recorded_in_open_event(
    db_conn: sqlite3.Connection,
) -> None:
    from harness import scope, ticket

    t = ticket.open_ticket(
        db_conn,
        wave=1,
        component="x",
        domain="backend",
        risk="medium",
        write_scope="api/routes.py,tests/test_routes.py",
    )

    assert scope.declared_write_scope(conn=db_conn, ticket_id=t.id) == {
        "api/routes.py",
        "tests/test_routes.py",
    }


def test_assignment_write_scope_participates_in_scope_check(
    db_conn: sqlite3.Connection,
) -> None:
    from harness import assignment, scope, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x", write_scope="api")
    assignment.add(
        db_conn,
        ticket_id=t.id,
        role="qa",
        agent_id="qa-a",
        duty="reviewer",
        write_scope="tests/harness",
    )

    assert scope.within_scope(
        db_conn,
        ticket_id=t.id,
        changed_paths=["api/routes.py", "tests/harness/test_scope.py"],
    )
    assert scope.violations_for_paths(
        db_conn,
        ticket_id=t.id,
        changed_paths=["ui/web/spa.js"],
    ) == ["ui/web/spa.js"]


def test_scope_prefix_boundary_does_not_match_sibling_prefix(
    db_conn: sqlite3.Connection,
) -> None:
    from harness import scope, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x", write_scope="api")

    assert scope.violations_for_paths(
        db_conn,
        ticket_id=t.id,
        changed_paths=["api/routes.py", "api2/routes.py"],
    ) == ["api2/routes.py"]


def test_scope_missing_ticket_raises_clear_error(db_conn: sqlite3.Connection) -> None:
    from harness import scope

    with pytest.raises(ValueError, match="ticket not found"):
        scope.violations_for_paths(db_conn, ticket_id="T-404", changed_paths=["api/routes.py"])


def test_changed_paths_from_git_includes_diff_and_untracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness import scope

    calls: list[list[str]] = []

    def _fake_run_git(args: list[str]) -> CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["diff", "--name-only"]:
            return CompletedProcess(args, 0, "api/routes.py\nold/name.py\n", "")
        if args == ["ls-files", "--others", "--exclude-standard"]:
            return CompletedProcess(args, 0, "scratch/new.py\n", "")
        raise AssertionError(args)

    monkeypatch.setattr(scope, "_run_git", _fake_run_git)

    assert scope.changed_paths_from_git(base_ref="origin/main") == [
        "api/routes.py",
        "old/name.py",
        "scratch/new.py",
    ]
    assert calls[0] == ["diff", "--name-only", "origin/main"]


def test_changed_paths_from_git_surfaces_git_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness import scope

    monkeypatch.setattr(
        scope,
        "_run_git",
        lambda args: CompletedProcess(args, 128, "", "bad revision"),
    )

    with pytest.raises(RuntimeError, match="bad revision"):
        scope.changed_paths_from_git(base_ref="missing")
