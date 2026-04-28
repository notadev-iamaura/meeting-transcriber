"""SQLite 스키마 정의 + 연결 헬퍼.

본 모듈은 단일 책임: DDL 보관 + Connection 객체 반환.
모든 비즈니스 쿼리는 ticket.py / gate.py / board.py 에서 수행한다.

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §4.2
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# 4 개 테이블 — 스펙 §4.2 와 1:1 일치.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tickets (
    id              TEXT PRIMARY KEY,
    wave            INTEGER NOT NULL CHECK (wave IN (1, 2, 3)),
    component       TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN (
                        'pending', 'design', 'red', 'green',
                        'refactor', 'merged', 'closed'
                    )),
    pr_number       INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id              INTEGER PRIMARY KEY,
    ticket_id       TEXT NOT NULL REFERENCES tickets(id),
    kind            TEXT NOT NULL CHECK (kind IN (
                        'mockup', 'visual_baseline', 'behavior_scenario',
                        'a11y_ruleset', 'implementation'
                    )),
    path            TEXT NOT NULL,
    sha256          TEXT,
    author_agent    TEXT NOT NULL CHECK (author_agent IN ('pm', 'designer', 'frontend', 'qa')),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_runs (
    id              INTEGER PRIMARY KEY,
    ticket_id       TEXT NOT NULL REFERENCES tickets(id),
    phase           TEXT NOT NULL CHECK (phase IN ('red', 'green')),
    visual_pass     INTEGER NOT NULL CHECK (visual_pass IN (0, 1)),
    behavior_pass   INTEGER NOT NULL CHECK (behavior_pass IN (0, 1)),
    a11y_pass       INTEGER NOT NULL CHECK (a11y_pass IN (0, 1)),
    visual_diff     TEXT,
    behavior_log    TEXT,
    a11y_violations TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,
    ticket_id       TEXT REFERENCES tickets(id),
    type            TEXT NOT NULL,
    payload         TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_wave_status ON tickets(wave, status);
CREATE INDEX IF NOT EXISTS idx_artifacts_ticket ON artifacts(ticket_id);
CREATE INDEX IF NOT EXISTS idx_gate_runs_ticket ON gate_runs(ticket_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_id, created_at);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """DB 파일을 열거나 새로 만들어서 연결을 반환한다.

    부모 디렉토리가 없으면 자동으로 생성한다.
    `PRAGMA foreign_keys = ON` 을 활성화해 외래키 제약을 강제한다.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """4 개 테이블 + 인덱스를 멱등적으로 생성한다."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()
