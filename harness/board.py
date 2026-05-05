"""마크다운 진행 보드 자동 생성.

SQLite 의 tickets / gate_runs / events 를 읽어
docs/superpowers/ui-ux-overhaul/00-overview.md 를 재생성한다.

Wave 별 그룹핑 + 상태 이모지 + 최근 게이트 요약 + 리뷰 상태 (P:peer-review, M:merge-final).

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §4.5
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from harness import consensus, review

# Wave 별 표시명 (스펙 §3 와 일치).
WAVE_TITLES: dict[int, str] = {
    1: "Wave 1 · Visual Polish",
    2: "Wave 2 · Interaction & Focus",
    3: "Wave 3 · Accessibility & Mobile",
}

# 상태별 이모지 — 텍스트 보드에서 빠르게 인식.
STATUS_EMOJI: dict[str, str] = {
    "pending": "📋",
    "design": "🎨",
    "red": "🔴",
    "green": "🟢",
    "refactor": "♻️",
    "merged": "🔀",
    "closed": "✅",
}


def _latest_gate_summary(conn: sqlite3.Connection, ticket_id: str) -> str:
    """가장 최근 게이트 결과를 'V✓ B✗ A✓' 형태로 표시."""
    gate_row = conn.execute(
        "SELECT visual_pass, behavior_pass, a11y_pass, phase, created_at "
        "FROM gate_runs WHERE ticket_id = ? ORDER BY id DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()
    profile_row = conn.execute(
        "SELECT payload, created_at FROM events "
        "WHERE ticket_id = ? AND type = 'gate.profile' ORDER BY id DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()
    if profile_row is not None and (
        gate_row is None or profile_row["created_at"] >= gate_row["created_at"]
    ):
        payload = json.loads(profile_row["payload"] or "{}")
        state = "✓" if payload.get("passed") else "✗"
        return f"{payload.get('phase', '?')}:{payload.get('profile', 'profile')} {state}"
    if gate_row is None:
        return "—"

    def m(v: int) -> str:
        return "✓" if v else "✗"

    return (
        f"{gate_row['phase']}: V{m(gate_row['visual_pass'])} "
        f"B{m(gate_row['behavior_pass'])} A{m(gate_row['a11y_pass'])}"
    )


def _review_glyph(status: str | None) -> str:
    """리뷰 status 를 글리프 한 글자로."""
    if status is None:
        return "—"
    return {
        "approved": "✓",
        "changes_requested": "✗",
        "pending": "…",
    }.get(status, "?")


def _consensus_glyph(conn: sqlite3.Connection, *, ticket_id: str, target: str) -> str:
    """consensus 상태를 보드용 한 글자로 표시."""
    result = consensus.status(conn, ticket_id=ticket_id, target=target)
    if result.reason:
        return "—"
    return "✓" if result.passed else "✗"


def render_overview(conn: sqlite3.Connection) -> str:
    """현재 SQLite 상태로부터 보드 마크다운을 생성한다."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    lines: list[str] = [
        "# UI/UX Overhaul — 진행 보드",
        "",
        f"> 자동 생성 (`harness board rebuild`) · 갱신 {now}",
        "",
    ]

    for wave in (1, 2, 3):
        lines.append(f"## {WAVE_TITLES[wave]}")
        lines.append("")
        rows = conn.execute(
            "SELECT id, component, status, pr_number FROM tickets WHERE wave = ? ORDER BY id",
            (wave,),
        ).fetchall()
        if not rows:
            lines.append("_티켓 없음_")
            lines.append("")
            continue
        lines.append("| 티켓 | 컴포넌트 | 상태 | 최근 게이트 | 합의 | 리뷰 | PR |")
        lines.append("|------|----------|------|-------------|------|------|----|")
        for r in rows:
            emoji = STATUS_EMOJI.get(r["status"], "")
            gate = _latest_gate_summary(conn, r["id"])
            pr = f"#{r['pr_number']}" if r["pr_number"] else "—"

            peer = review.latest_status(conn, ticket_id=r["id"], kind="peer-review")
            merge = review.latest_status(conn, ticket_id=r["id"], kind="merge-final")
            review_cell = f"P:{_review_glyph(peer)} M:{_review_glyph(merge)}"
            consensus_cell = (
                f"E:{_consensus_glyph(conn, ticket_id=r['id'], target='execute')} "
                f"M:{_consensus_glyph(conn, ticket_id=r['id'], target='merge')}"
            )

            lines.append(
                f"| {r['id']} | `{r['component']}` | {emoji} {r['status']} | "
                f"{gate} | {consensus_cell} | {review_cell} | {pr} |"
            )
        lines.append("")

    return "\n".join(lines)


def write_overview(conn: sqlite3.Connection, path: Path) -> None:
    """render_overview() 결과를 파일에 저장한다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_overview(conn))
