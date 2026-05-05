"""harness.cli — CLI 명령 라우팅 단위 테스트.

CliRunner 패턴 대신 monkeypatch 로 sys.argv + db_path 환경변수 주입.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.harness


def _run_cli(monkeypatch: pytest.MonkeyPatch, db_path: Path, argv: list[str]) -> int:
    """harness.cli.main() 을 격리된 인자로 실행하고 returncode 반환."""
    from harness import cli

    monkeypatch.setenv("HARNESS_DB", str(db_path))
    monkeypatch.setattr("sys.argv", ["harness", *argv])
    try:
        cli.main()
        return 0
    except SystemExit as e:
        return int(e.code) if e.code is not None else 0


def _record_consensus(
    db_path: Path,
    *,
    ticket_id: str,
    target: str,
    scope_hash: str = "H1",
) -> None:
    from harness import consensus, db

    conn = db.connect(db_path)
    db.init_schema(conn)
    consensus.require_role(conn, ticket_id=ticket_id, target=target, role="qa")
    for agent_id in ("qa-a", "qa-b"):
        consensus.submit_review(
            conn,
            ticket_id=ticket_id,
            target=target,
            role="qa",
            agent_id=agent_id,
            status="approved",
            scope_hash=scope_hash,
        )


def test_cli_ticket_open_prints_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    db = tmp_path / "harness.db"
    rc = _run_cli(monkeypatch, db, ["ticket", "open", "--wave", "1", "--component", "empty-state"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "T-101"


def test_cli_ticket_list_after_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    db = tmp_path / "harness.db"
    _run_cli(monkeypatch, db, ["ticket", "open", "--wave", "1", "--component", "a"])
    _run_cli(monkeypatch, db, ["ticket", "open", "--wave", "2", "--component", "b"])
    capsys.readouterr()  # drain
    _run_cli(monkeypatch, db, ["ticket", "list"])
    out = capsys.readouterr().out
    assert "T-101" in out
    assert "T-201" in out


def test_cli_ticket_show_missing_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "harness.db"
    rc = _run_cli(monkeypatch, db, ["ticket", "show", "T-999"])
    assert rc != 0


def test_cli_board_rebuild_creates_overview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "harness.db"
    overview = tmp_path / "overview.md"
    monkeypatch.setenv("HARNESS_BOARD_PATH", str(overview))
    _run_cli(monkeypatch, db, ["ticket", "open", "--wave", "1", "--component", "x"])
    rc = _run_cli(monkeypatch, db, ["board", "rebuild"])
    assert rc == 0
    assert overview.exists()
    assert "T-101" in overview.read_text()


def test_cli_no_args_prints_usage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "harness.db"
    rc = _run_cli(monkeypatch, db, [])
    # argparse 가 인자 없을 때 비정상 종료
    assert rc != 0


def test_cli_review_record_and_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """review record / status 흐름: 둘 다 approved 일 때만 'all reviews approved'."""
    db = tmp_path / "harness.db"
    _run_cli(monkeypatch, db, ["ticket", "open", "--wave", "1", "--component", "x"])
    capsys.readouterr()

    # peer-review 만 approved → status 미충족
    _run_cli(
        monkeypatch,
        db,
        [
            "review",
            "record",
            "--ticket",
            "T-101",
            "--agent",
            "designer-b",
            "--kind",
            "peer-review",
            "--status",
            "approved",
        ],
    )
    capsys.readouterr()
    rc = _run_cli(monkeypatch, db, ["review", "status", "--ticket", "T-101"])
    assert rc != 0  # incomplete
    assert "incomplete" in capsys.readouterr().out

    # merge-final 까지 approved → status 충족
    _run_cli(
        monkeypatch,
        db,
        [
            "review",
            "record",
            "--ticket",
            "T-101",
            "--agent",
            "pm-b",
            "--kind",
            "merge-final",
            "--status",
            "approved",
        ],
    )
    capsys.readouterr()
    rc = _run_cli(monkeypatch, db, ["review", "status", "--ticket", "T-101"])
    assert rc == 0
    assert "all reviews approved" in capsys.readouterr().out


def test_cli_review_record_with_note(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--note 인자로 짧은 사유 기록."""
    db = tmp_path / "harness.db"
    _run_cli(monkeypatch, db, ["ticket", "open", "--wave", "1", "--component", "x"])
    rc = _run_cli(
        monkeypatch,
        db,
        [
            "review",
            "record",
            "--ticket",
            "T-101",
            "--agent",
            "frontend-b",
            "--kind",
            "peer-review",
            "--status",
            "changes_requested",
            "--note",
            "ui/web/spa.js:1234 — 중복 라우터 정의",
        ],
    )
    assert rc == 0


def test_cli_ticket_close_incomplete_consensus_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    db = tmp_path / "harness.db"
    _run_cli(monkeypatch, db, ["ticket", "open", "--wave", "1", "--component", "x"])
    capsys.readouterr()

    rc = _run_cli(monkeypatch, db, ["ticket", "close", "T-101", "--pr", "42"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "target=merge consensus" in captured.err
    assert "Traceback" not in captured.err


def test_cli_ticket_close_with_scope_hash_uses_that_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    db = tmp_path / "harness.db"
    _run_cli(monkeypatch, db, ["ticket", "open", "--wave", "1", "--component", "x"])
    _record_consensus(db, ticket_id="T-101", target="merge", scope_hash="H1")

    from harness import consensus
    from harness import db as harness_db

    conn = harness_db.connect(db)
    consensus.submit_review(
        conn,
        ticket_id="T-101",
        target="merge",
        role="qa",
        agent_id="qa-a",
        status="approved",
        scope_hash="H2",
    )
    capsys.readouterr()

    rc = _run_cli(
        monkeypatch,
        db,
        ["ticket", "close", "T-101", "--pr", "42", "--scope-hash", "H1"],
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "closed T-101" in out


def test_cli_gate_green_incomplete_consensus_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from harness import gate

    db = tmp_path / "harness.db"
    _run_cli(monkeypatch, db, ["ticket", "open", "--wave", "1", "--component", "x"])
    monkeypatch.setattr(gate, "_run_visual_axis", lambda *_args: pytest.fail("axis ran"))
    capsys.readouterr()

    rc = _run_cli(monkeypatch, db, ["gate", "run", "T-101", "--phase", "green"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "target=execute consensus" in captured.err
    assert "Traceback" not in captured.err


def test_cli_gate_green_with_scope_hash_uses_that_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from harness import consensus, gate
    from harness import db as harness_db

    db = tmp_path / "harness.db"
    _run_cli(monkeypatch, db, ["ticket", "open", "--wave", "1", "--component", "x"])
    _record_consensus(db, ticket_id="T-101", target="execute", scope_hash="H1")
    conn = harness_db.connect(db)
    consensus.submit_review(
        conn,
        ticket_id="T-101",
        target="execute",
        role="qa",
        agent_id="qa-a",
        status="approved",
        scope_hash="H2",
    )
    monkeypatch.setattr(gate, "_run_visual_axis", lambda *_args: gate.AxisResult(True, None))
    monkeypatch.setattr(gate, "_run_behavior_axis", lambda *_args: gate.AxisResult(True, None))
    monkeypatch.setattr(gate, "_run_a11y_axis", lambda *_args: gate.AxisResult(True, None))
    capsys.readouterr()

    rc = _run_cli(
        monkeypatch,
        db,
        ["gate", "run", "T-101", "--phase", "green", "--profile", "ui", "--scope-hash", "H1"],
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "gate green for T-101" in out


def test_cli_gate_non_ui_profile_prints_command_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from harness import gate

    db = tmp_path / "harness.db"
    _run_cli(monkeypatch, db, ["ticket", "open", "--wave", "1", "--component", "x"])

    def _fake_run_command(
        spec: gate.CommandSpec, *, ticket_id: str, profile: str
    ) -> gate.CommandResult:
        return gate.CommandResult(
            name=spec.name,
            argv=spec.argv,
            passed=True,
            returncode=0,
            detail_path=Path(f"state/gate-logs/{ticket_id}-{profile}-{spec.name}.log"),
        )

    monkeypatch.setattr(gate, "_run_command", _fake_run_command)
    capsys.readouterr()

    rc = _run_cli(
        monkeypatch, db, ["gate", "run", "T-101", "--phase", "red", "--profile", "backend"]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "ruff-check" in out
    assert "api-tests" in out
    assert "visual" not in out


def test_cli_scope_check_reports_violations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    db = tmp_path / "harness.db"
    _run_cli(
        monkeypatch,
        db,
        [
            "ticket",
            "open",
            "--wave",
            "1",
            "--component",
            "x",
            "--write-scope",
            "api",
        ],
    )
    capsys.readouterr()

    ok = _run_cli(
        monkeypatch, db, ["scope", "check", "--ticket", "T-101", "--changed", "api/x.py"]
    )
    bad = _run_cli(
        monkeypatch,
        db,
        ["scope", "check", "--ticket", "T-101", "--changed", "ui/web/spa.js"],
    )

    assert ok == 0
    assert bad == 1
    assert "ui/web/spa.js" in capsys.readouterr().out


def test_cli_scope_check_can_read_changed_paths_from_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from harness import scope

    db = tmp_path / "harness.db"
    _run_cli(
        monkeypatch,
        db,
        [
            "ticket",
            "open",
            "--wave",
            "1",
            "--component",
            "x",
            "--write-scope",
            "api",
        ],
    )
    monkeypatch.setattr(
        scope,
        "changed_paths_from_git",
        lambda base_ref="main": ["api/routes.py", "ui/web/spa.js"],
    )
    capsys.readouterr()

    rc = _run_cli(
        monkeypatch,
        db,
        ["scope", "check", "--ticket", "T-101", "--from-git", "--base-ref", "origin/main"],
    )

    out = capsys.readouterr().out
    assert rc == 1
    assert "ui/web/spa.js" in out


def test_cli_scope_check_missing_ticket_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    db = tmp_path / "harness.db"

    rc = _run_cli(
        monkeypatch,
        db,
        ["scope", "check", "--ticket", "T-404", "--changed", "api/routes.py"],
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "ticket not found: T-404" in captured.err
    assert "Traceback" not in captured.err


def test_cli_assignment_artifact_consensus_flow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """assign/artifact/review submit/consensus status 의 최소 happy path."""
    db = tmp_path / "harness.db"
    plan = tmp_path / "plan.md"
    plan.write_text("plan", encoding="utf-8")

    _run_cli(monkeypatch, db, ["ticket", "open", "--wave", "1", "--component", "x"])
    capsys.readouterr()

    rc = _run_cli(
        monkeypatch,
        db,
        [
            "assign",
            "add",
            "--ticket",
            "T-101",
            "--role",
            "qa",
            "--agent-id",
            "qa-a",
            "--duty",
            "reviewer",
        ],
    )
    assert rc == 0

    rc = _run_cli(
        monkeypatch,
        db,
        [
            "artifact",
            "add",
            "--ticket",
            "T-101",
            "--kind",
            "plan",
            "--path",
            str(plan),
            "--author-agent",
            "pm-a",
            "--compute-hash",
        ],
    )
    assert rc == 0

    rc = _run_cli(
        monkeypatch,
        db,
        [
            "consensus",
            "require",
            "--ticket",
            "T-101",
            "--target",
            "execute",
            "--role",
            "qa",
        ],
    )
    assert rc == 0

    for agent_id in ("qa-a", "qa-b"):
        rc = _run_cli(
            monkeypatch,
            db,
            [
                "review",
                "submit",
                "--ticket",
                "T-101",
                "--target",
                "execute",
                "--role",
                "qa",
                "--agent-id",
                agent_id,
                "--status",
                "approved",
                "--scope-hash",
                "H1",
            ],
        )
        assert rc == 0

    capsys.readouterr()
    rc = _run_cli(
        monkeypatch,
        db,
        ["consensus", "status", "--ticket", "T-101", "--target", "execute", "--scope-hash", "H1"],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out


def test_cli_consensus_execute_does_not_satisfy_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    db = tmp_path / "harness.db"
    _run_cli(monkeypatch, db, ["ticket", "open", "--wave", "1", "--component", "x"])
    _run_cli(
        monkeypatch,
        db,
        ["consensus", "require", "--ticket", "T-101", "--target", "merge", "--role", "qa"],
    )
    for agent_id in ("qa-a", "qa-b"):
        _run_cli(
            monkeypatch,
            db,
            [
                "review",
                "submit",
                "--ticket",
                "T-101",
                "--target",
                "execute",
                "--role",
                "qa",
                "--agent-id",
                agent_id,
                "--status",
                "approved",
                "--scope-hash",
                "H1",
            ],
        )
    capsys.readouterr()

    rc = _run_cli(
        monkeypatch,
        db,
        ["consensus", "status", "--ticket", "T-101", "--target", "merge", "--scope-hash", "H1"],
    )
    assert rc == 1
    assert "FAIL" in capsys.readouterr().out


def test_cli_consensus_rejects_single_agent_quorum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "harness.db"
    _run_cli(monkeypatch, db, ["ticket", "open", "--wave", "1", "--component", "x"])

    rc = _run_cli(
        monkeypatch,
        db,
        [
            "consensus",
            "require",
            "--ticket",
            "T-101",
            "--target",
            "execute",
            "--role",
            "qa",
            "--min-approvals",
            "1",
        ],
    )

    assert rc != 0


def test_cli_review_submit_missing_ticket_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    db = tmp_path / "harness.db"

    rc = _run_cli(
        monkeypatch,
        db,
        [
            "review",
            "submit",
            "--ticket",
            "T-404",
            "--target",
            "execute",
            "--role",
            "qa",
            "--agent-id",
            "qa-a",
            "--status",
            "approved",
            "--scope-hash",
            "H1",
        ],
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "ticket not found: T-404" in captured.err
    assert "Traceback" not in captured.err


def test_cli_consensus_require_missing_ticket_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    db = tmp_path / "harness.db"

    rc = _run_cli(
        monkeypatch,
        db,
        [
            "consensus",
            "require",
            "--ticket",
            "T-404",
            "--target",
            "execute",
            "--role",
            "qa",
        ],
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "ticket not found: T-404" in captured.err
    assert "Traceback" not in captured.err
