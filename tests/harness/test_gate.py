"""harness.gate — 3축 통합 게이트 + review 통과 강제 단위 테스트.

실제 pytest subprocess 호출은 monkeypatch 로 차단하고
PASS/FAIL 행 기록 + review 강제 로직만 검증한다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.harness


def _stub_axis(passed: bool, detail_path=None):
    """축 함수의 모킹 헬퍼 — 새 시그니처 (ticket_id, component)."""
    from harness import gate

    def _fn(ticket_id: str, component: str) -> gate.AxisResult:
        return gate.AxisResult(passed=passed, detail_path=detail_path)

    return _fn


def _record_execute_consensus(
    db_conn: sqlite3.Connection, ticket_id: str, scope_hash: str = "H1"
) -> None:
    """green 게이트 실행을 위해 execute consensus 승인 기록."""
    from harness import consensus

    consensus.require_role(db_conn, ticket_id=ticket_id, target="execute", role="qa")
    for agent_id in ("qa-a", "qa-b"):
        consensus.submit_review(
            db_conn,
            ticket_id=ticket_id,
            target="execute",
            role="qa",
            agent_id=agent_id,
            status="approved",
            scope_hash=scope_hash,
        )


def test_run_gate_records_three_axes(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    _record_execute_consensus(db_conn, t.id)

    monkeypatch.setattr(gate, "_run_visual_axis", _stub_axis(True))
    monkeypatch.setattr(gate, "_run_behavior_axis", _stub_axis(True))
    monkeypatch.setattr(gate, "_run_a11y_axis", _stub_axis(True))

    result = gate.run_gate(db_conn, ticket_id=t.id, phase="green", scope_hash="H1")
    assert result.all_passed is True

    row = db_conn.execute(
        "SELECT visual_pass, behavior_pass, a11y_pass, phase FROM gate_runs WHERE ticket_id = ?",
        (t.id,),
    ).fetchone()
    assert row["visual_pass"] == 1
    assert row["behavior_pass"] == 1
    assert row["a11y_pass"] == 1
    assert row["phase"] == "green"


def test_run_gate_records_failures_with_detail(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from harness import gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="y")
    diff = tmp_path / "diff.png"
    diff.write_bytes(b"\x89PNG")
    log = tmp_path / "behavior.log"
    log.write_text("FAIL")
    a11y_json = tmp_path / "a11y.json"
    a11y_json.write_text("[]")

    monkeypatch.setattr(gate, "_run_visual_axis", _stub_axis(False, diff))
    monkeypatch.setattr(gate, "_run_behavior_axis", _stub_axis(False, log))
    monkeypatch.setattr(gate, "_run_a11y_axis", _stub_axis(False, a11y_json))

    result = gate.run_gate(db_conn, ticket_id=t.id, phase="red")
    assert result.all_passed is False
    row = db_conn.execute(
        "SELECT visual_pass, behavior_pass, a11y_pass, "
        "visual_diff, behavior_log, a11y_violations FROM gate_runs "
        "WHERE ticket_id = ?",
        (t.id,),
    ).fetchone()
    assert row["visual_pass"] == 0
    assert row["behavior_pass"] == 0
    assert row["a11y_pass"] == 0
    assert str(diff) in row["visual_diff"]
    assert str(log) in row["behavior_log"]
    assert str(a11y_json) in row["a11y_violations"]


def test_run_gate_invalid_phase_raises(db_conn: sqlite3.Connection) -> None:
    from harness import gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="z")
    with pytest.raises(ValueError, match="phase must be"):
        gate.run_gate(db_conn, ticket_id=t.id, phase="middle")


def test_green_gate_blocked_when_reviews_pending(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """execute consensus 미완료 상태에서 green 게이트 시도하면 ReviewIncomplete."""
    from harness import gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    monkeypatch.setattr(gate, "_run_visual_axis", _stub_axis(True))
    monkeypatch.setattr(gate, "_run_behavior_axis", _stub_axis(True))
    monkeypatch.setattr(gate, "_run_a11y_axis", _stub_axis(True))
    with pytest.raises(gate.ReviewIncomplete):
        gate.run_gate(db_conn, ticket_id=t.id, phase="green")


def test_green_gate_rejects_legacy_review_all_passed(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import gate, review, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    review.record(
        db_conn, ticket_id=t.id, agent="designer-b", kind="peer-review", status="approved"
    )
    review.record(db_conn, ticket_id=t.id, agent="pm-b", kind="merge-final", status="approved")

    called = False

    def _should_not_run(ticket_id: str, component: str) -> gate.AxisResult:
        nonlocal called
        called = True
        return gate.AxisResult(passed=True, detail_path=None)

    monkeypatch.setattr(gate, "_run_visual_axis", _should_not_run)
    monkeypatch.setattr(gate, "_run_behavior_axis", _should_not_run)
    monkeypatch.setattr(gate, "_run_a11y_axis", _should_not_run)

    with pytest.raises(gate.ReviewIncomplete, match="target=execute consensus"):
        gate.run_gate(db_conn, ticket_id=t.id, phase="green")
    assert called is False


def test_green_gate_uses_execute_consensus_not_merge_consensus(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import consensus, gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    consensus.require_role(db_conn, ticket_id=t.id, target="merge", role="qa")
    for agent_id in ("qa-a", "qa-b"):
        consensus.submit_review(
            db_conn,
            ticket_id=t.id,
            target="merge",
            role="qa",
            agent_id=agent_id,
            status="approved",
            scope_hash="H1",
        )
    monkeypatch.setattr(gate, "_run_visual_axis", _stub_axis(True))
    monkeypatch.setattr(gate, "_run_behavior_axis", _stub_axis(True))
    monkeypatch.setattr(gate, "_run_a11y_axis", _stub_axis(True))

    with pytest.raises(gate.ReviewIncomplete):
        gate.run_gate(db_conn, ticket_id=t.id, phase="green", scope_hash="H1")


def test_green_gate_uses_explicit_scope_hash(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import consensus, gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    _record_execute_consensus(db_conn, t.id, scope_hash="H1")
    consensus.submit_review(
        db_conn,
        ticket_id=t.id,
        target="execute",
        role="qa",
        agent_id="qa-a",
        status="approved",
        scope_hash="H2",
    )
    monkeypatch.setattr(gate, "_run_visual_axis", _stub_axis(True))
    monkeypatch.setattr(gate, "_run_behavior_axis", _stub_axis(True))
    monkeypatch.setattr(gate, "_run_a11y_axis", _stub_axis(True))

    assert gate.run_gate(db_conn, ticket_id=t.id, phase="green", scope_hash="H1").all_passed
    with pytest.raises(gate.ReviewIncomplete):
        gate.run_gate(db_conn, ticket_id=t.id, phase="green")


def _stub_command_results(returncodes: dict[str, int] | None = None):
    from harness import gate

    codes = returncodes or {}

    def _fn(spec: gate.CommandSpec, *, ticket_id: str, profile: str) -> gate.CommandResult:
        returncode = codes.get(spec.name, 0)
        return gate.CommandResult(
            name=spec.name,
            argv=spec.argv,
            passed=returncode == 0,
            returncode=returncode,
            detail_path=Path(f"state/gate-logs/{ticket_id}-{profile}-{spec.name}.log"),
        )

    return _fn


@pytest.mark.parametrize("profile", ["backend", "frontend", "pipeline", "docs", "release"])
def test_non_ui_profiles_run_command_specs_and_record_event(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    import json

    from harness import gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    monkeypatch.setattr(gate, "_run_command", _stub_command_results())

    result = gate.run_gate(db_conn, ticket_id=t.id, phase="red", profile=profile)

    assert result.all_passed is True
    assert result.profile == profile
    assert result.commands
    row = db_conn.execute(
        "SELECT payload FROM events WHERE ticket_id = ? AND type = 'gate.profile'",
        (t.id,),
    ).fetchone()
    assert row is not None
    payload = json.loads(row["payload"])
    assert payload["phase"] == "red"
    assert payload["profile"] == profile
    assert payload["passed"] is True
    assert payload["commands"][0]["name"] == result.commands[0].name
    assert payload["commands"][0]["argv"] == list(result.commands[0].argv)


def test_non_ui_profile_failure_records_event_and_failed_result(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from harness import gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    monkeypatch.setattr(gate, "_run_command", _stub_command_results({"api-tests": 1}))

    result = gate.run_gate(db_conn, ticket_id=t.id, phase="red", profile="backend")

    assert result.all_passed is False
    row = db_conn.execute(
        "SELECT payload FROM events WHERE ticket_id = ? AND type = 'gate.profile'",
        (t.id,),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["passed"] is False
    assert any(command["returncode"] == 1 for command in payload["commands"])


def test_green_non_ui_profile_checks_execute_consensus_before_commands(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    called = False

    def _should_not_run(
        spec: gate.CommandSpec, *, ticket_id: str, profile: str
    ) -> gate.CommandResult:
        nonlocal called
        called = True
        return gate.CommandResult(spec.name, spec.argv, True, 0, Path("unused.log"))

    monkeypatch.setattr(gate, "_run_command", _should_not_run)

    with pytest.raises(gate.ReviewIncomplete):
        gate.run_gate(db_conn, ticket_id=t.id, phase="green", profile="backend")
    assert called is False
    row = db_conn.execute(
        "SELECT 1 FROM events WHERE ticket_id = ? AND type = 'gate.profile'",
        (t.id,),
    ).fetchone()
    assert row is None


def test_gate_rejects_unknown_profile_without_running_commands(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    called = False

    def _should_not_run(
        spec: gate.CommandSpec, *, ticket_id: str, profile: str
    ) -> gate.CommandResult:
        nonlocal called
        called = True
        return gate.CommandResult(spec.name, spec.argv, True, 0, Path("unused.log"))

    monkeypatch.setattr(gate, "_run_command", _should_not_run)

    with pytest.raises(gate.GateMisconfigured, match="profile"):
        gate.run_gate(db_conn, ticket_id=t.id, phase="red", profile="unknown")
    assert called is False


def test_profile_commands_are_interpreter_safe() -> None:
    import sys

    from harness import gate

    for profile in ("backend", "frontend", "pipeline", "docs", "release"):
        for spec in gate._profile_commands(profile):
            assert spec.argv[0] == sys.executable


def test_red_gate_does_not_check_reviews(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """red 단계는 review 강제 안 함 (Producer 산출물 직후 실행되므로)."""
    from harness import gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="y")
    monkeypatch.setattr(gate, "_run_visual_axis", _stub_axis(False))
    monkeypatch.setattr(gate, "_run_behavior_axis", _stub_axis(False))
    monkeypatch.setattr(gate, "_run_a11y_axis", _stub_axis(False))
    # 예외 없이 실행되어야 함
    result = gate.run_gate(db_conn, ticket_id=t.id, phase="red")
    assert result.all_passed is False


def test_run_gate_raises_when_visual_test_missing(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """visual test 파일 부재 시 GateMisconfigured (NO-OP PASS 방지)."""
    from harness import gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="nonexistent-component")
    # axes 는 실제 구현 사용 — _component_to_filename + Path.exists() 검증
    # behavior / a11y 도 없을 것이므로 visual 에서 가장 먼저 raise
    with pytest.raises(gate.GateMisconfigured, match="visual test missing"):
        gate.run_gate(db_conn, ticket_id=t.id, phase="red")


def test_run_gate_raises_when_ticket_missing(db_conn: sqlite3.Connection) -> None:
    """ticket lookup 실패 시 ValueError."""
    from harness import gate

    with pytest.raises(ValueError, match="ticket not found"):
        gate.run_gate(db_conn, ticket_id="T-NEVER", phase="red")


def test_component_to_filename_normalizes_dashes() -> None:
    from harness import gate

    assert gate._component_to_filename("empty-state") == "empty_state"
    assert gate._component_to_filename("cmd-palette") == "cmd_palette"
    assert gate._component_to_filename("simple") == "simple"
