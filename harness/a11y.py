"""접근성(axe-core) 검사 결과 기록.

axe-playwright-python 라이브러리는 Playwright 페이지에 axe.run() 을 주입하고
violations 배열을 반환한다. 본 모듈은 그 violations 를 받아 events 에 기록.

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §5.3
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

# 스펙 §5.3 — 기본 룰셋.
# wcag21aaa 는 너무 엄격하여 본 작업 범위 밖.
DEFAULT_RULESET: tuple[str, ...] = ("wcag2a", "wcag2aa", "wcag21aa")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_run(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    violations: list[dict],
) -> None:
    """axe-core 결과를 events 에 기록한다.

    Args:
        violations: axe-playwright-python 가 반환한 violations 배열.
                    빈 리스트 = passed=True.
    """
    passed = len(violations) == 0
    payload = {
        "passed": passed,
        "violation_count": len(violations),
        "violations": violations,
    }
    conn.execute(
        "INSERT INTO events (ticket_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, "a11y.run", json.dumps(payload), _now()),
    )
    conn.commit()
