"""3축 통합 게이트 + 리뷰 통과 강제.

한 티켓에 대해 visual / behavior / a11y 3 영역을 모두 실행하고
결과를 gate_runs 테이블에 한 행으로 기록한다.

phase='green' 진입 전에 review.all_passed() 를 강제 — peer-review 와
merge-final 의 최신 status 가 모두 approved 가 아니면 ReviewIncomplete.
phase='red' 는 Producer 산출물 직후 실행되므로 review 강제 안 함.

각 축은 별도 subprocess(pytest) 로 격리 실행하여 fixture/세션 충돌을 방지.

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §4.4, §4.5, §4.3.1
"""
from __future__ import annotations

import shlex
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from harness import review
from harness import ticket as _ticket

VALID_PHASES: tuple[str, ...] = ("red", "green")


class ReviewIncomplete(Exception):
    """green 단계 진입 전에 모든 review 가 approved 가 아닌 경우."""


class GateMisconfigured(Exception):
    """게이트 실행 전 필수 테스트 파일이 누락된 경우 — silent NO-OP PASS 방지."""


@dataclass(frozen=True)
class AxisResult:
    """단일 축(visual/behavior/a11y) 결과."""

    passed: bool
    detail_path: Path | None  # 실패 시 diff/log/violations 파일 경로


@dataclass(frozen=True)
class GateResult:
    """3축 통합 결과."""

    visual: AxisResult
    behavior: AxisResult
    a11y: AxisResult

    @property
    def all_passed(self) -> bool:
        """3축 모두 통과 여부."""
        return self.visual.passed and self.behavior.passed and self.a11y.passed


def _now() -> str:
    """ISO-8601 UTC 타임스탬프."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _run_pytest(test_path: str, ticket_id: str) -> tuple[bool, Path | None]:
    """pytest 를 별도 프로세스에서 실행하고 (passed, log_path) 반환.

    실패 시에만 log_path 를 반환하고 통과 시 None.
    """
    log_path = Path(f"state/gate-logs/{ticket_id}-{Path(test_path).name}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # PATH 의존 제거 — 가상환경 미활성 상태에서도 동일 인터프리터 사용.
    cmd = [sys.executable, "-m", "pytest", test_path, "-v", "-m", "ui"]
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    log_path.write_text(
        f"# command: {shlex.join(cmd)}\n"
        f"# returncode: {completed.returncode}\n\n"
        f"## stdout\n{completed.stdout}\n\n"
        f"## stderr\n{completed.stderr}\n"
    )
    return completed.returncode == 0, log_path if completed.returncode != 0 else None


def _component_to_filename(component: str) -> str:
    """component 식별자를 테스트 파일명으로 변환 (`demo-swatch` -> `demo_swatch`)."""
    return component.replace("-", "_")


def _run_visual_axis(ticket_id: str, component: str) -> AxisResult:
    """tests/ui/visual/test_{component}.py 를 실행. 파일 미존재 시 GateMisconfigured."""
    test_file = Path(f"tests/ui/visual/test_{_component_to_filename(component)}.py")
    if not test_file.exists():
        raise GateMisconfigured(
            f"visual test missing for component {component!r}: {test_file}. "
            "QA-A 가 시나리오 작성 전에 게이트 실행 시도."
        )
    passed, log = _run_pytest(str(test_file), ticket_id)
    return AxisResult(passed=passed, detail_path=log)


def _run_behavior_axis(ticket_id: str, component: str) -> AxisResult:
    """tests/ui/behavior/test_{component}.py 를 실행. 파일 미존재 시 GateMisconfigured."""
    test_file = Path(f"tests/ui/behavior/test_{_component_to_filename(component)}.py")
    if not test_file.exists():
        raise GateMisconfigured(
            f"behavior test missing for component {component!r}: {test_file}."
        )
    passed, log = _run_pytest(str(test_file), ticket_id)
    return AxisResult(passed=passed, detail_path=log)


def _run_a11y_axis(ticket_id: str, component: str) -> AxisResult:
    """tests/ui/a11y/test_{component}.py 를 실행. 파일 미존재 시 GateMisconfigured."""
    test_file = Path(f"tests/ui/a11y/test_{_component_to_filename(component)}.py")
    if not test_file.exists():
        raise GateMisconfigured(
            f"a11y test missing for component {component!r}: {test_file}."
        )
    passed, log = _run_pytest(str(test_file), ticket_id)
    return AxisResult(passed=passed, detail_path=log)


def run_gate(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    phase: str,
) -> GateResult:
    """visual / behavior / a11y 3축을 실행하고 gate_runs 에 기록한다.

    phase='green' 진입 전 review.all_passed() 강제. 통과 안 되면 ReviewIncomplete.
    phase='red' 는 review 강제 안 함.

    Args:
        conn: SQLite 연결.
        ticket_id: 대상 티켓 (반드시 존재해야 함).
        phase: 'red' (Frontend 구현 전) 또는 'green' (구현 후).

    Returns:
        GateResult — all_passed 프로퍼티로 통합 통과 여부 확인 가능.

    Raises:
        ValueError: phase 가 'red'/'green' 외 값일 때.
        ReviewIncomplete: phase='green' 인데 리뷰가 모두 approved 가 아닐 때.
    """
    if phase not in VALID_PHASES:
        raise ValueError(f"phase must be {VALID_PHASES}, got {phase!r}")

    t = _ticket.get_ticket(conn, ticket_id)
    if t is None:
        raise ValueError(f"ticket not found: {ticket_id}")

    if phase == "green" and not review.all_passed(conn, ticket_id=ticket_id):
        raise ReviewIncomplete(
            f"ticket {ticket_id}: green gate requires all peer-review and "
            f"merge-final to be 'approved'. "
            f"Run `python -m harness review status --ticket {ticket_id}` to inspect."
        )

    visual = _run_visual_axis(ticket_id, t.component)
    behavior = _run_behavior_axis(ticket_id, t.component)
    a11y = _run_a11y_axis(ticket_id, t.component)

    conn.execute(
        "INSERT INTO gate_runs ("
        "    ticket_id, phase, visual_pass, behavior_pass, a11y_pass,"
        "    visual_diff, behavior_log, a11y_violations, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ticket_id, phase,
            int(visual.passed), int(behavior.passed), int(a11y.passed),
            str(visual.detail_path) if visual.detail_path else None,
            str(behavior.detail_path) if behavior.detail_path else None,
            str(a11y.detail_path) if a11y.detail_path else None,
            _now(),
        ),
    )
    conn.commit()
    return GateResult(visual=visual, behavior=behavior, a11y=a11y)
