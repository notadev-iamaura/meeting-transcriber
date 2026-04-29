"""harness.gate — 3축 통합 게이트 + review 통과 강제 단위 테스트.

실제 pytest subprocess 호출은 monkeypatch 로 차단하고
PASS/FAIL 행 기록 + review 강제 로직만 검증한다.
"""

from __future__ import annotations

import sqlite3

import pytest

pytestmark = pytest.mark.harness


def _stub_axis(passed: bool, detail_path=None):
    """축 함수의 모킹 헬퍼 — 새 시그니처 (ticket_id, component)."""
    from harness import gate

    def _fn(ticket_id: str, component: str) -> gate.AxisResult:
        return gate.AxisResult(passed=passed, detail_path=detail_path)

    return _fn


def _record_all_reviews_approved(db_conn: sqlite3.Connection, ticket_id: str) -> None:
    """green 게이트 통과를 위해 peer-review + merge-final 모두 approved 기록."""
    from harness import review

    review.record(
        db_conn, ticket_id=ticket_id, agent="designer-b", kind="peer-review", status="approved"
    )
    review.record(
        db_conn, ticket_id=ticket_id, agent="pm-b", kind="merge-final", status="approved"
    )


def test_run_gate_records_three_axes(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    _record_all_reviews_approved(db_conn, t.id)

    monkeypatch.setattr(gate, "_run_visual_axis", _stub_axis(True))
    monkeypatch.setattr(gate, "_run_behavior_axis", _stub_axis(True))
    monkeypatch.setattr(gate, "_run_a11y_axis", _stub_axis(True))

    result = gate.run_gate(db_conn, ticket_id=t.id, phase="green")
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
    """리뷰 미완료 상태에서 green 게이트 시도하면 ReviewIncomplete."""
    from harness import gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    monkeypatch.setattr(gate, "_run_visual_axis", _stub_axis(True))
    monkeypatch.setattr(gate, "_run_behavior_axis", _stub_axis(True))
    monkeypatch.setattr(gate, "_run_a11y_axis", _stub_axis(True))
    with pytest.raises(gate.ReviewIncomplete):
        gate.run_gate(db_conn, ticket_id=t.id, phase="green")


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
