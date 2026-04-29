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
