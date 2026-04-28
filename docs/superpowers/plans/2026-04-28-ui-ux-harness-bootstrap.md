# UI/UX Harness Bootstrap (Plan 0/4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** UI/UX Overhaul 의 4개 plan 시리즈 중 첫 번째. 풀스택 하네스 시스템(Python CLI + SQLite 영속 + 3축 QA 게이트 + 4 서브에이전트 정의) 을 셋업하고, 샘플 컴포넌트로 end-to-end 사이클이 동작함을 증명한다.

**Architecture:** 루트 패키지 `harness/` (Python) + SQLite 단일 파일(`state/harness.db`) 영속 + Playwright 내장 시각 회귀(`expect.to_have_screenshot()`) + `axe-playwright-python` 접근성 + 마크다운 보드 자동 생성. `python -m harness <verb> <noun>` CLI 진입.

**Tech Stack:** Python 3.11 / SQLite 3 (stdlib `sqlite3`) / Playwright (Python, 이미 dev dep) / `axe-playwright-python` (신규 의존성, 테스트 전용) / pytest.

**Spec 참조:** `docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md` §4 시스템 아키텍처

**후속 plans (별도 작성):**
- Plan 1: Wave 1 — Visual Polish (`2026-04-28-ui-ux-wave-1-visual-polish.md`)
- Plan 2: Wave 2 — Interaction & Focus
- Plan 3: Wave 3 — Accessibility & Mobile

**Spec 보정 메모:** Spec §4.1 의 `scripts/harness/` 는 본 plan 에서 **루트 패키지 `harness/`** 로 보정한다. 이유: `python -m harness` CLI 가 깔끔하고 `pyproject.toml`의 `packages.find.include` 패턴과 일치한다. Spec 의 의도(독립 CLI 도구)는 동일하게 유지된다.

---

## File Structure

신규 생성:
- `harness/` — 루트 패키지
  - `harness/__init__.py` — 버전·공용 임포트
  - `harness/__main__.py` — `python -m harness` 진입
  - `harness/cli.py` — argparse 라우팅
  - `harness/db.py` — SQLite 스키마 DDL · 연결 헬퍼
  - `harness/ticket.py` — 티켓 모델 + CRUD
  - `harness/snapshot.py` — Playwright 시각 회귀 베이스라인 헬퍼
  - `harness/behavior.py` — Playwright 행동 시나리오 실행 래퍼
  - `harness/a11y.py` — `axe-playwright-python` 통합
  - `harness/gate.py` — 3축 통합 게이트 오케스트레이터
  - `harness/board.py` — 마크다운 진행 보드 생성기
- `tests/harness/` — 하네스 자체 단위 테스트
  - `tests/harness/__init__.py` (빈 파일)
  - `tests/harness/conftest.py` — 임시 DB fixture
  - `tests/harness/test_db.py`
  - `tests/harness/test_ticket.py`
  - `tests/harness/test_snapshot.py`
  - `tests/harness/test_behavior.py`
  - `tests/harness/test_a11y.py`
  - `tests/harness/test_gate.py`
  - `tests/harness/test_board.py`
  - `tests/harness/test_cli.py`
- `tests/ui/` — UI 테스트 디렉토리 (스켈레톤만, Wave 1+ 에서 채움)
  - `tests/ui/__init__.py`
  - `tests/ui/conftest.py` — Playwright fixture
  - `tests/ui/visual/__init__.py`
  - `tests/ui/visual/baselines/.gitkeep`
  - `tests/ui/behavior/__init__.py`
  - `tests/ui/a11y/__init__.py`
- `state/.gitkeep` — `harness.db` 가 생성될 디렉토리
- `.claude/agents/ui-ux/` — 4 서브에이전트 프롬프트
  - `pm.md` · `designer.md` · `frontend.md` · `qa.md`
- `docs/superpowers/ui-ux-overhaul/` — 자동 생성 진행 보드 디렉토리
  - `00-overview.md` (Task 7 에서 첫 생성)
  - `wave-1/.gitkeep` · `wave-2/.gitkeep` · `wave-3/.gitkeep`

수정:
- `pyproject.toml` — `axe-playwright-python` dev dep 추가, `packages.find.include` 에 `harness*` 추가, pytest markers 에 `harness`/`ui` 추가
- `.gitignore` — `state/harness.db`, `tests/ui/visual/diffs/`, `tests/ui/__snapshots__/` 제외

---

## Task 1: 디렉토리 골격 + 의존성 추가

**Files:**
- Create: `harness/__init__.py`, `harness/__main__.py`, `tests/harness/__init__.py`, `tests/ui/__init__.py`, `tests/ui/visual/__init__.py`, `tests/ui/visual/baselines/.gitkeep`, `tests/ui/behavior/__init__.py`, `tests/ui/a11y/__init__.py`, `state/.gitkeep`, `docs/superpowers/ui-ux-overhaul/wave-1/.gitkeep`, `docs/superpowers/ui-ux-overhaul/wave-2/.gitkeep`, `docs/superpowers/ui-ux-overhaul/wave-3/.gitkeep`
- Modify: `pyproject.toml`, `.gitignore`

- [ ] **Step 1: 디렉토리 + 빈 패키지 파일 생성**

```bash
mkdir -p harness tests/harness tests/ui/visual/baselines tests/ui/behavior tests/ui/a11y state docs/superpowers/ui-ux-overhaul/wave-1 docs/superpowers/ui-ux-overhaul/wave-2 docs/superpowers/ui-ux-overhaul/wave-3
touch harness/__init__.py tests/harness/__init__.py tests/ui/__init__.py tests/ui/visual/__init__.py tests/ui/behavior/__init__.py tests/ui/a11y/__init__.py
touch tests/ui/visual/baselines/.gitkeep state/.gitkeep
touch docs/superpowers/ui-ux-overhaul/wave-1/.gitkeep docs/superpowers/ui-ux-overhaul/wave-2/.gitkeep docs/superpowers/ui-ux-overhaul/wave-3/.gitkeep
```

- [ ] **Step 2: `harness/__init__.py` 작성**

```python
"""UI/UX Overhaul 풀스택 하네스 — 티켓·게이트·보드 통합 CLI.

Spec: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md
"""

__version__ = "0.1.0"
```

- [ ] **Step 3: `harness/__main__.py` 작성 (CLI 진입점, 실제 라우팅은 Task 9 에서)**

```python
"""`python -m harness ...` 진입점.

라우팅 본체는 harness.cli.main() 으로 위임한다.
"""

from harness.cli import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: `pyproject.toml` 수정 — dev dep + packages 등록 + pytest marker**

`pyproject.toml`의 `[project.optional-dependencies]` `dev` 리스트에 다음 한 줄을 `pytest-playwright>=0.7.2` 다음에 추가:

```toml
    "axe-playwright-python>=0.1.4",
```

`[tool.setuptools.packages.find]` 의 `include` 를 다음으로 교체:

```toml
include = ["core*", "steps*", "search*", "api*", "ui*", "security*", "harness*"]
```

`[tool.pytest.ini_options]` 의 `markers` 에 다음 두 줄 추가:

```toml
    "harness: 하네스 자체 단위 테스트",
    "ui: UI/UX overhaul 테스트 (Wave 1/2/3 산출물)",
```

- [ ] **Step 5: `.gitignore` 에 무시 항목 추가**

`.gitignore` 끝에 다음 블록 추가:

```gitignore

# UI/UX Harness — 영속 DB 와 시각 회귀 임시 산출물
state/harness.db
state/harness.db-journal
tests/ui/visual/diffs/
tests/ui/__snapshots__/
```

- [ ] **Step 6: 의존성 설치 + import 가능 확인**

Run:
```bash
pip install -e ".[dev]"
python -c "import harness; print(harness.__version__)"
```
Expected: `0.1.0` 출력 (오류 없음)

- [ ] **Step 7: Commit**

```bash
git add harness/ tests/harness/ tests/ui/ state/ docs/superpowers/ui-ux-overhaul/ pyproject.toml .gitignore
git commit -m "기능: UI/UX 하네스 패키지 골격 + axe-playwright-python 의존성 추가

루트 패키지 harness/ 를 신설하고 python -m harness 로 호출 가능하도록
pyproject.toml 에 등록. tests/harness/ + tests/ui/ 디렉토리 스켈레톤 생성."
```

---

## Task 2: SQLite 스키마 + 연결 헬퍼

**Files:**
- Create: `harness/db.py`, `tests/harness/conftest.py`, `tests/harness/test_db.py`

- [ ] **Step 1: `tests/harness/conftest.py` 작성 (임시 DB fixture)**

```python
"""tests/harness 공용 fixture — 임시 SQLite DB.

각 테스트마다 격리된 DB 파일을 생성하고 종료 시 자동 삭제한다.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """테스트별 임시 DB 파일 경로."""
    return tmp_path / "harness.db"


@pytest.fixture
def db_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """초기화된 DB 연결을 yield 하고 종료 시 닫는다.

    db.init_schema() 를 호출해 스키마가 반영된 상태로 시작한다.
    """
    from harness import db as db_module

    conn = db_module.connect(db_path)
    db_module.init_schema(conn)
    try:
        yield conn
    finally:
        conn.close()
```

- [ ] **Step 2: `tests/harness/test_db.py` 작성 (실패 테스트)**

```python
"""harness.db — 스키마 초기화·연결 헬퍼 단위 테스트."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.harness


def test_connect_creates_db_file(tmp_path: Path) -> None:
    """connect() 는 부모 디렉토리가 없어도 DB 파일을 생성한다."""
    from harness import db

    target = tmp_path / "nested" / "harness.db"
    conn = db.connect(target)
    assert target.exists()
    assert isinstance(conn, sqlite3.Connection)
    conn.close()


def test_init_schema_creates_four_tables(db_conn: sqlite3.Connection) -> None:
    """init_schema() 는 tickets / artifacts / gate_runs / events 4개 테이블을 만든다."""
    cursor = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]
    assert tables == ["artifacts", "events", "gate_runs", "tickets"]


def test_init_schema_is_idempotent(db_conn: sqlite3.Connection) -> None:
    """init_schema() 는 두 번 호출해도 오류 없이 동작한다."""
    from harness import db

    db.init_schema(db_conn)  # 두 번째 호출
    cursor = db_conn.execute("SELECT count(*) FROM tickets")
    assert cursor.fetchone()[0] == 0


def test_tickets_status_constraint(db_conn: sqlite3.Connection) -> None:
    """tickets.status 는 허용된 enum 값만 받는다."""
    db_conn.execute(
        "INSERT INTO tickets (id, wave, component, status, created_at, updated_at) "
        "VALUES ('T-001', 1, 'empty-state', 'pending', '2026-04-28T00:00:00', '2026-04-28T00:00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO tickets (id, wave, component, status, created_at, updated_at) "
            "VALUES ('T-002', 1, 'x', 'INVALID_STATUS', '2026-04-28T00:00:00', '2026-04-28T00:00:00')"
        )


def test_artifacts_foreign_key(db_conn: sqlite3.Connection) -> None:
    """artifacts.ticket_id 는 존재하지 않는 티켓을 참조할 수 없다."""
    db_conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO artifacts (ticket_id, kind, path, author_agent, created_at) "
            "VALUES ('T-MISSING', 'mockup', 'docs/x.md', 'designer', '2026-04-28T00:00:00')"
        )
```

- [ ] **Step 3: 테스트 실패 확인**

Run:
```bash
pytest tests/harness/test_db.py -v
```
Expected: 5개 테스트 모두 FAIL — `ModuleNotFoundError: No module named 'harness.db'`

- [ ] **Step 4: `harness/db.py` 작성 (최소 구현)**

```python
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
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
```

- [ ] **Step 5: 테스트 통과 확인**

Run:
```bash
pytest tests/harness/test_db.py -v
```
Expected: 5개 테스트 모두 PASS

- [ ] **Step 6: Commit**

```bash
git add harness/db.py tests/harness/conftest.py tests/harness/test_db.py
git commit -m "기능: 하네스 SQLite 스키마 + 연결 헬퍼 (db.py)

tickets / artifacts / gate_runs / events 4 개 테이블 정의.
CHECK 제약으로 status / kind / author_agent enum 강제.
외래키 활성화. tests/harness/test_db.py 5 케이스 통과."
```

---

## Task 3: 티켓 모델 + CRUD

**Files:**
- Create: `harness/ticket.py`, `tests/harness/test_ticket.py`

- [ ] **Step 1: `tests/harness/test_ticket.py` 작성 (실패 테스트)**

```python
"""harness.ticket — 티켓 CRUD 단위 테스트."""
from __future__ import annotations

import sqlite3

import pytest

pytestmark = pytest.mark.harness


def test_open_ticket_assigns_id_and_status(db_conn: sqlite3.Connection) -> None:
    """open_ticket() 은 id 를 자동 발급하고 status='pending' 으로 시작한다."""
    from harness import ticket

    t = ticket.open_ticket(db_conn, wave=1, component="empty-state")
    assert t.id.startswith("T-")
    assert t.wave == 1
    assert t.component == "empty-state"
    assert t.status == "pending"
    assert t.pr_number is None


def test_open_ticket_id_format(db_conn: sqlite3.Connection) -> None:
    """티켓 id 형식: T-{wave}{NN} (Wave 1 -> T-101, T-102, ...)."""
    from harness import ticket

    t1 = ticket.open_ticket(db_conn, wave=1, component="empty-state")
    t2 = ticket.open_ticket(db_conn, wave=1, component="skeleton")
    t3 = ticket.open_ticket(db_conn, wave=2, component="cmd-palette")
    assert t1.id == "T-101"
    assert t2.id == "T-102"
    assert t3.id == "T-201"


def test_get_ticket_returns_none_when_missing(db_conn: sqlite3.Connection) -> None:
    from harness import ticket

    assert ticket.get_ticket(db_conn, "T-999") is None


def test_list_tickets_filters_by_wave_and_status(db_conn: sqlite3.Connection) -> None:
    from harness import ticket

    t1 = ticket.open_ticket(db_conn, wave=1, component="a")
    ticket.open_ticket(db_conn, wave=1, component="b")
    ticket.open_ticket(db_conn, wave=2, component="c")
    ticket.update_status(db_conn, t1.id, "design")

    wave1 = ticket.list_tickets(db_conn, wave=1)
    assert len(wave1) == 2

    designs = ticket.list_tickets(db_conn, status="design")
    assert len(designs) == 1
    assert designs[0].id == t1.id


def test_update_status_persists_and_emits_event(db_conn: sqlite3.Connection) -> None:
    from harness import ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    ticket.update_status(db_conn, t.id, "red")
    refreshed = ticket.get_ticket(db_conn, t.id)
    assert refreshed is not None
    assert refreshed.status == "red"

    events = db_conn.execute(
        "SELECT type, payload FROM events WHERE ticket_id = ? ORDER BY id", (t.id,)
    ).fetchall()
    types = [e["type"] for e in events]
    assert "ticket.opened" in types
    assert "status.changed" in types


def test_close_ticket_sets_pr_and_status(db_conn: sqlite3.Connection) -> None:
    from harness import ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    ticket.update_status(db_conn, t.id, "green")
    ticket.close_ticket(db_conn, t.id, pr_number=42)
    refreshed = ticket.get_ticket(db_conn, t.id)
    assert refreshed is not None
    assert refreshed.status == "closed"
    assert refreshed.pr_number == 42


def test_invalid_status_transition_raises(db_conn: sqlite3.Connection) -> None:
    """closed 티켓은 다시 status 변경 불가."""
    from harness import ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    ticket.update_status(db_conn, t.id, "green")
    ticket.close_ticket(db_conn, t.id, pr_number=1)
    with pytest.raises(ticket.InvalidStatusTransition):
        ticket.update_status(db_conn, t.id, "red")
```

- [ ] **Step 2: 테스트 실패 확인**

Run:
```bash
pytest tests/harness/test_ticket.py -v
```
Expected: 7개 모두 FAIL — `ModuleNotFoundError`

- [ ] **Step 3: `harness/ticket.py` 작성**

```python
"""티켓 CRUD + 상태 전이.

티켓 id 형식: T-{wave}{NN} — Wave 1 은 T-101, T-102, ... ,
Wave 2 는 T-201, ... , Wave 3 은 T-301, ...

상태 전이 (단방향):
    pending -> design -> red -> green -> refactor -> merged -> closed
    closed 는 종착 상태이며 그 이후 변경 금지.

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §4.2, §4.4
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


class InvalidStatusTransition(Exception):
    """closed 또는 비허용 상태에서의 변경 시도."""


@dataclass(frozen=True)
class Ticket:
    """tickets 테이블 한 행의 read-only 표현."""

    id: str
    wave: int
    component: str
    status: str
    pr_number: int | None
    created_at: str
    updated_at: str


# 허용된 status 값 (db.py 의 CHECK 제약과 일치).
_VALID_STATUSES = {
    "pending", "design", "red", "green", "refactor", "merged", "closed",
}


def _now() -> str:
    """ISO-8601 UTC 타임스탬프."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _next_ticket_id(conn: sqlite3.Connection, wave: int) -> str:
    """T-{wave}{NN} 형식의 다음 id 를 발급한다.

    같은 wave 내에서 가장 큰 번호 + 1 을 사용. 시작은 01.
    """
    prefix = f"T-{wave}"
    row = conn.execute(
        "SELECT id FROM tickets WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    if row is None:
        return f"{prefix}01"
    last_n = int(row["id"][len(prefix):])
    return f"{prefix}{last_n + 1:02d}"


def _emit_event(
    conn: sqlite3.Connection,
    ticket_id: str | None,
    type_: str,
    payload: dict | None = None,
) -> None:
    """events 테이블에 한 줄을 기록한다."""
    conn.execute(
        "INSERT INTO events (ticket_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, type_, json.dumps(payload) if payload else None, _now()),
    )


def open_ticket(conn: sqlite3.Connection, *, wave: int, component: str) -> Ticket:
    """새 티켓을 발급하고 events 에 ticket.opened 를 기록한다."""
    if wave not in (1, 2, 3):
        raise ValueError(f"wave must be 1/2/3, got {wave!r}")
    ticket_id = _next_ticket_id(conn, wave)
    now = _now()
    conn.execute(
        "INSERT INTO tickets (id, wave, component, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (ticket_id, wave, component, now, now),
    )
    _emit_event(conn, ticket_id, "ticket.opened", {"wave": wave, "component": component})
    conn.commit()
    return Ticket(
        id=ticket_id, wave=wave, component=component, status="pending",
        pr_number=None, created_at=now, updated_at=now,
    )


def get_ticket(conn: sqlite3.Connection, ticket_id: str) -> Ticket | None:
    """id 로 한 건 조회. 없으면 None."""
    row = conn.execute(
        "SELECT id, wave, component, status, pr_number, created_at, updated_at "
        "FROM tickets WHERE id = ?", (ticket_id,),
    ).fetchone()
    if row is None:
        return None
    return Ticket(**dict(row))


def list_tickets(
    conn: sqlite3.Connection,
    *,
    wave: int | None = None,
    status: str | None = None,
) -> list[Ticket]:
    """선택적으로 wave / status 로 필터링한 티켓 리스트."""
    sql = (
        "SELECT id, wave, component, status, pr_number, created_at, updated_at "
        "FROM tickets WHERE 1=1"
    )
    params: list[object] = []
    if wave is not None:
        sql += " AND wave = ?"
        params.append(wave)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    return [Ticket(**dict(r)) for r in rows]


def update_status(conn: sqlite3.Connection, ticket_id: str, new_status: str) -> None:
    """티켓 status 를 변경한다.

    closed 티켓은 변경 불가. 비허용 status 는 ValueError.
    """
    if new_status not in _VALID_STATUSES:
        raise ValueError(f"invalid status: {new_status!r}")
    current = get_ticket(conn, ticket_id)
    if current is None:
        raise ValueError(f"ticket not found: {ticket_id}")
    if current.status == "closed":
        raise InvalidStatusTransition(
            f"ticket {ticket_id} is closed; cannot change to {new_status!r}"
        )
    now = _now()
    conn.execute(
        "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, now, ticket_id),
    )
    _emit_event(
        conn, ticket_id, "status.changed",
        {"from": current.status, "to": new_status},
    )
    conn.commit()


def close_ticket(conn: sqlite3.Connection, ticket_id: str, *, pr_number: int) -> None:
    """티켓을 closed 상태로 옮기고 PR 번호를 기록한다."""
    current = get_ticket(conn, ticket_id)
    if current is None:
        raise ValueError(f"ticket not found: {ticket_id}")
    if current.status == "closed":
        raise InvalidStatusTransition(f"ticket {ticket_id} already closed")
    now = _now()
    conn.execute(
        "UPDATE tickets SET status = 'closed', pr_number = ?, updated_at = ? WHERE id = ?",
        (pr_number, now, ticket_id),
    )
    _emit_event(conn, ticket_id, "ticket.closed", {"pr_number": pr_number})
    conn.commit()
```

- [ ] **Step 4: 테스트 통과 확인**

Run:
```bash
pytest tests/harness/test_ticket.py -v
```
Expected: 7개 모두 PASS

- [ ] **Step 5: Commit**

```bash
git add harness/ticket.py tests/harness/test_ticket.py
git commit -m "기능: 하네스 티켓 CRUD + 상태 전이 (ticket.py)

T-{wave}{NN} 형식 id 자동 발급. pending->design->red->green->refactor->
merged->closed 단방향 전이. closed 티켓은 변경 불가 (InvalidStatusTransition).
모든 변경은 events 테이블에 자동 기록."
```

---

## Task 4: 시각 회귀 베이스라인 헬퍼 (snapshot.py)

**Files:**
- Create: `harness/snapshot.py`, `tests/harness/test_snapshot.py`

**컨텍스트:** Playwright 의 `expect(page).to_have_screenshot()` 가 실제 비교를 담당. 본 모듈은 (1) 베이스라인 PNG 의 경로 규칙을 정의하고, (2) `harness snapshot baseline --component X --variant Y` CLI 가 페이지를 렌더해 PNG 를 저장하는 헬퍼를 제공.

- [ ] **Step 1: `tests/harness/test_snapshot.py` 작성 (실패 테스트)**

```python
"""harness.snapshot — 시각 회귀 베이스라인 경로·메타데이터 단위 테스트.

실제 Playwright 캡처는 통합 테스트(Task 10) 에서 검증.
본 단위 테스트는 경로 규칙·아티팩트 등록만 검증한다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.harness


def test_baseline_path_for_variant() -> None:
    """베이스라인 경로: tests/ui/visual/baselines/{component}-{variant}.png"""
    from harness import snapshot

    p = snapshot.baseline_path("empty-state", "light")
    assert p == Path("tests/ui/visual/baselines/empty-state-light.png")


def test_supported_variants() -> None:
    """3 개 변종 지원: light / dark / mobile"""
    from harness import snapshot

    assert snapshot.SUPPORTED_VARIANTS == ("light", "dark", "mobile")


def test_register_baseline_creates_artifact_row(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """register_baseline() 은 기존 PNG 파일을 artifacts 테이블에 등록한다."""
    from harness import snapshot, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="empty-state")
    fake_png = tmp_path / "fake-baseline.png"
    fake_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    snapshot.register_baseline(
        db_conn,
        ticket_id=t.id,
        path=fake_png,
        variant="light",
    )

    rows = db_conn.execute(
        "SELECT kind, path, author_agent FROM artifacts WHERE ticket_id = ?",
        (t.id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "visual_baseline"
    assert rows[0]["author_agent"] == "designer"
    assert str(fake_png) in rows[0]["path"]


def test_register_baseline_stores_sha256(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harness import snapshot, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    fake_png = tmp_path / "img.png"
    fake_png.write_bytes(b"hello world")

    snapshot.register_baseline(db_conn, ticket_id=t.id, path=fake_png, variant="dark")

    row = db_conn.execute(
        "SELECT sha256 FROM artifacts WHERE ticket_id = ?", (t.id,)
    ).fetchone()
    # sha256("hello world") = b94d27b9...
    assert row["sha256"] == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_register_baseline_invalid_variant_raises(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harness import snapshot, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    png = tmp_path / "x.png"
    png.write_bytes(b"x")
    with pytest.raises(ValueError, match="variant must be one of"):
        snapshot.register_baseline(db_conn, ticket_id=t.id, path=png, variant="huge")
```

- [ ] **Step 2: 테스트 실패 확인**

Run:
```bash
pytest tests/harness/test_snapshot.py -v
```
Expected: 5개 모두 FAIL

- [ ] **Step 3: `harness/snapshot.py` 작성**

```python
"""시각 회귀 베이스라인 경로 + 아티팩트 등록.

베이스라인 PNG 캡처 자체는 Playwright (`expect(page).to_have_screenshot()`)
가 담당하며, Wave 1+ 의 tests/ui/visual/test_*.py 에서 호출된다.
본 모듈은 경로 규칙·아티팩트 등록만 책임진다.

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §4.5, §5.3
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# 지원 변종: 라이트 모드 / 다크 모드 / 모바일 (768px 이하).
SUPPORTED_VARIANTS: tuple[str, ...] = ("light", "dark", "mobile")

# 베이스라인 PNG 가 저장될 루트.
BASELINES_ROOT = Path("tests/ui/visual/baselines")


def baseline_path(component: str, variant: str) -> Path:
    """베이스라인 PNG 의 표준 경로를 반환한다.

    Args:
        component: 컴포넌트 식별자 (예: "empty-state")
        variant: light / dark / mobile 중 하나

    Returns:
        tests/ui/visual/baselines/{component}-{variant}.png
    """
    if variant not in SUPPORTED_VARIANTS:
        raise ValueError(
            f"variant must be one of {SUPPORTED_VARIANTS}, got {variant!r}"
        )
    return BASELINES_ROOT / f"{component}-{variant}.png"


def _sha256_of(path: Path) -> str:
    """파일의 SHA-256 hex 다이제스트."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def register_baseline(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    path: Path,
    variant: str,
) -> None:
    """기존 PNG 파일을 artifacts 테이블에 visual_baseline 으로 등록한다.

    Designer 에이전트가 Playwright 로 PNG 를 저장한 뒤 이 함수를 호출한다.
    """
    if variant not in SUPPORTED_VARIANTS:
        raise ValueError(
            f"variant must be one of {SUPPORTED_VARIANTS}, got {variant!r}"
        )
    if not path.exists():
        raise FileNotFoundError(f"baseline image not found: {path}")
    sha = _sha256_of(path)
    conn.execute(
        "INSERT INTO artifacts (ticket_id, kind, path, sha256, author_agent, created_at) "
        "VALUES (?, 'visual_baseline', ?, ?, 'designer', ?)",
        (ticket_id, str(path), sha, _now()),
    )
    conn.commit()
```

- [ ] **Step 4: 테스트 통과 확인**

Run:
```bash
pytest tests/harness/test_snapshot.py -v
```
Expected: 5개 모두 PASS

- [ ] **Step 5: Commit**

```bash
git add harness/snapshot.py tests/harness/test_snapshot.py
git commit -m "기능: 시각 회귀 베이스라인 경로·등록 헬퍼 (snapshot.py)

tests/ui/visual/baselines/{component}-{variant}.png 경로 규칙 정의.
3 개 변종(light/dark/mobile) 지원. SHA-256 으로 변경 추적.
실제 PNG 캡처는 Playwright 가 담당, 본 모듈은 등록만."
```

---

## Task 5: 행동 시나리오 + 접근성 모듈

**Files:**
- Create: `harness/behavior.py`, `harness/a11y.py`, `tests/harness/test_behavior.py`, `tests/harness/test_a11y.py`

**컨텍스트:** behavior/a11y 모듈은 Playwright 행동 시나리오와 axe-core 결과를 SQLite 에 기록하는 얇은 래퍼. 실제 시나리오 작성은 QA 에이전트가 `tests/ui/behavior/test_*.py` / `tests/ui/a11y/test_*.py` 에서 담당.

- [ ] **Step 1: `tests/harness/test_behavior.py` 작성**

```python
"""harness.behavior — 행동 시나리오 결과 기록 단위 테스트."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.harness


def test_record_behavior_run_pass(db_conn: sqlite3.Connection) -> None:
    from harness import behavior, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    behavior.record_run(db_conn, ticket_id=t.id, passed=True, log_path=None)
    row = db_conn.execute(
        "SELECT type, payload FROM events WHERE ticket_id = ? AND type = 'behavior.run'",
        (t.id,),
    ).fetchone()
    assert row is not None
    assert '"passed": true' in row["payload"]


def test_record_behavior_run_fail_with_log(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harness import behavior, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    log = tmp_path / "behavior.log"
    log.write_text("scenario A failed at step 3\n")
    behavior.record_run(db_conn, ticket_id=t.id, passed=False, log_path=log)
    row = db_conn.execute(
        "SELECT payload FROM events WHERE ticket_id = ? AND type = 'behavior.run'",
        (t.id,),
    ).fetchone()
    assert '"passed": false' in row["payload"]
    assert str(log) in row["payload"]


def test_register_scenario_artifact(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    from harness import behavior, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    scenario_file = tmp_path / "test_x.py"
    scenario_file.write_text("# Given-When-Then\n")
    behavior.register_scenario(db_conn, ticket_id=t.id, path=scenario_file)
    row = db_conn.execute(
        "SELECT kind, author_agent FROM artifacts WHERE ticket_id = ?", (t.id,)
    ).fetchone()
    assert row["kind"] == "behavior_scenario"
    assert row["author_agent"] == "qa"
```

- [ ] **Step 2: `tests/harness/test_a11y.py` 작성**

```python
"""harness.a11y — axe-core 결과 기록 단위 테스트."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.harness


def test_record_a11y_run_no_violations(db_conn: sqlite3.Connection) -> None:
    from harness import a11y, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    a11y.record_run(db_conn, ticket_id=t.id, violations=[])
    row = db_conn.execute(
        "SELECT payload FROM events WHERE ticket_id = ? AND type = 'a11y.run'",
        (t.id,),
    ).fetchone()
    assert row is not None
    payload = json.loads(row["payload"])
    assert payload["violation_count"] == 0
    assert payload["passed"] is True


def test_record_a11y_run_with_violations(db_conn: sqlite3.Connection) -> None:
    from harness import a11y, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    violations = [
        {"id": "aria-current", "impact": "serious", "nodes": [{"target": [".item"]}]},
    ]
    a11y.record_run(db_conn, ticket_id=t.id, violations=violations)
    row = db_conn.execute(
        "SELECT payload FROM events WHERE ticket_id = ? AND type = 'a11y.run'",
        (t.id,),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["violation_count"] == 1
    assert payload["passed"] is False
    assert payload["violations"][0]["id"] == "aria-current"


def test_default_ruleset() -> None:
    """스펙 §5.3: wcag2a + wcag2aa + wcag21aa."""
    from harness import a11y

    assert a11y.DEFAULT_RULESET == ("wcag2a", "wcag2aa", "wcag21aa")
```

- [ ] **Step 3: 두 테스트 파일 모두 실패 확인**

Run:
```bash
pytest tests/harness/test_behavior.py tests/harness/test_a11y.py -v
```
Expected: 6개 모두 FAIL

- [ ] **Step 4: `harness/behavior.py` 작성**

```python
"""행동 시나리오 (Playwright Given-When-Then) 결과 기록.

QA 에이전트가 tests/ui/behavior/test_*.py 에 시나리오를 작성하면
gate.py 가 pytest 로 실행하고 그 결과를 본 모듈을 통해 SQLite 에 기록한다.

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §4.4, §5.3
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def register_scenario(
    conn: sqlite3.Connection, *, ticket_id: str, path: Path
) -> None:
    """QA 가 작성한 시나리오 파일을 artifacts 에 등록한다."""
    if not path.exists():
        raise FileNotFoundError(f"scenario file not found: {path}")
    conn.execute(
        "INSERT INTO artifacts (ticket_id, kind, path, author_agent, created_at) "
        "VALUES (?, 'behavior_scenario', ?, 'qa', ?)",
        (ticket_id, str(path), _now()),
    )
    conn.commit()


def record_run(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    passed: bool,
    log_path: Path | None,
) -> None:
    """행동 시나리오 실행 결과를 events 에 기록한다.

    실제 게이트 결과(visual/behavior/a11y 통합 PASS/FAIL) 는 gate.py 에서
    gate_runs 테이블에 별도 기록되며, 본 함수는 events 감사 로그용이다.
    """
    payload = {"passed": passed, "log_path": str(log_path) if log_path else None}
    conn.execute(
        "INSERT INTO events (ticket_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, "behavior.run", json.dumps(payload), _now()),
    )
    conn.commit()
```

- [ ] **Step 5: `harness/a11y.py` 작성**

```python
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
```

- [ ] **Step 6: 테스트 통과 확인**

Run:
```bash
pytest tests/harness/test_behavior.py tests/harness/test_a11y.py -v
```
Expected: 6개 모두 PASS

- [ ] **Step 7: Commit**

```bash
git add harness/behavior.py harness/a11y.py tests/harness/test_behavior.py tests/harness/test_a11y.py
git commit -m "기능: 행동 시나리오·접근성 결과 기록 모듈 (behavior.py, a11y.py)

QA 에이전트의 Playwright 시나리오·axe-core violations 를 events 테이블에
JSON 페이로드로 기록. 기본 axe 룰셋: wcag2a + wcag2aa + wcag21aa.
artifacts 등록 헬퍼 포함."
```

---

## Task 6: 3축 통합 게이트 (gate.py)

**Files:**
- Create: `harness/gate.py`, `tests/harness/test_gate.py`

**컨텍스트:** gate.py 는 visual / behavior / a11y 3 영역의 pytest 호출을 조율하고 결과를 `gate_runs` 테이블에 단일 행으로 기록한다. 실제 pytest 호출은 `subprocess.run` 으로 격리한다 (현재 pytest 세션 내부에서 또 다른 pytest 를 직접 호출하면 fixture 충돌 위험).

- [ ] **Step 1: `tests/harness/test_gate.py` 작성**

```python
"""harness.gate — 3축 통합 게이트 단위 테스트.

실제 pytest subprocess 호출은 monkeypatch 로 차단하고
PASS/FAIL 행 기록 로직만 검증한다.
"""
from __future__ import annotations

import sqlite3

import pytest

pytestmark = pytest.mark.harness


def test_run_gate_records_three_axes(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import gate, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")

    # 3개 호출 모두 PASS 로 모킹
    def fake_visual(ticket_id: str) -> gate.AxisResult:
        return gate.AxisResult(passed=True, detail_path=None)

    def fake_behavior(ticket_id: str) -> gate.AxisResult:
        return gate.AxisResult(passed=True, detail_path=None)

    def fake_a11y(ticket_id: str) -> gate.AxisResult:
        return gate.AxisResult(passed=True, detail_path=None)

    monkeypatch.setattr(gate, "_run_visual_axis", fake_visual)
    monkeypatch.setattr(gate, "_run_behavior_axis", fake_behavior)
    monkeypatch.setattr(gate, "_run_a11y_axis", fake_a11y)

    result = gate.run_gate(db_conn, ticket_id=t.id, phase="green")
    assert result.all_passed is True

    row = db_conn.execute(
        "SELECT visual_pass, behavior_pass, a11y_pass, phase FROM gate_runs "
        "WHERE ticket_id = ?", (t.id,),
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

    monkeypatch.setattr(
        gate, "_run_visual_axis",
        lambda tid: gate.AxisResult(passed=False, detail_path=diff),
    )
    monkeypatch.setattr(
        gate, "_run_behavior_axis",
        lambda tid: gate.AxisResult(passed=False, detail_path=log),
    )
    monkeypatch.setattr(
        gate, "_run_a11y_axis",
        lambda tid: gate.AxisResult(passed=False, detail_path=a11y_json),
    )

    result = gate.run_gate(db_conn, ticket_id=t.id, phase="red")
    assert result.all_passed is False
    row = db_conn.execute(
        "SELECT visual_pass, behavior_pass, a11y_pass, "
        "visual_diff, behavior_log, a11y_violations FROM gate_runs "
        "WHERE ticket_id = ?", (t.id,),
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
```

- [ ] **Step 2: 실패 확인**

Run:
```bash
pytest tests/harness/test_gate.py -v
```
Expected: 3개 모두 FAIL

- [ ] **Step 3: `harness/gate.py` 작성**

```python
"""3축 통합 게이트.

한 티켓에 대해 visual / behavior / a11y 3 영역을 모두 실행하고
결과를 gate_runs 테이블에 한 행으로 기록한다.

각 축은 별도 subprocess(pytest) 로 격리 실행하여 fixture/세션 충돌을 방지.

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §4.4, §4.5
"""
from __future__ import annotations

import shlex
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

VALID_PHASES: tuple[str, ...] = ("red", "green")


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
        return self.visual.passed and self.behavior.passed and self.a11y.passed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_pytest(test_path: str, ticket_id: str) -> tuple[bool, Path | None]:
    """pytest 를 별도 프로세스에서 실행하고 (passed, log_path) 반환."""
    log_path = Path(f"state/gate-logs/{ticket_id}-{Path(test_path).name}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["pytest", test_path, "-v", "-m", "ui"]
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


def _run_visual_axis(ticket_id: str) -> AxisResult:
    """tests/ui/visual/test_{component}.py 호출 — 실제 컴포넌트별 매핑은 Wave 1+ 에서."""
    test_dir = Path("tests/ui/visual")
    if not any(test_dir.glob("test_*.py")):
        # Wave 1 시작 전: 시각 테스트가 아직 없으므로 NO-OP PASS.
        return AxisResult(passed=True, detail_path=None)
    passed, log = _run_pytest(str(test_dir), ticket_id)
    return AxisResult(passed=passed, detail_path=log)


def _run_behavior_axis(ticket_id: str) -> AxisResult:
    test_dir = Path("tests/ui/behavior")
    if not any(test_dir.glob("test_*.py")):
        return AxisResult(passed=True, detail_path=None)
    passed, log = _run_pytest(str(test_dir), ticket_id)
    return AxisResult(passed=passed, detail_path=log)


def _run_a11y_axis(ticket_id: str) -> AxisResult:
    test_dir = Path("tests/ui/a11y")
    if not any(test_dir.glob("test_*.py")):
        return AxisResult(passed=True, detail_path=None)
    passed, log = _run_pytest(str(test_dir), ticket_id)
    return AxisResult(passed=passed, detail_path=log)


def run_gate(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    phase: str,
) -> GateResult:
    """visual / behavior / a11y 3축을 실행하고 gate_runs 에 기록한다.

    Args:
        ticket_id: 대상 티켓 (반드시 존재해야 함)
        phase: 'red' (Frontend 구현 전) 또는 'green' (구현 후)

    Returns:
        GateResult with all_passed property.
    """
    if phase not in VALID_PHASES:
        raise ValueError(f"phase must be {VALID_PHASES}, got {phase!r}")

    visual = _run_visual_axis(ticket_id)
    behavior = _run_behavior_axis(ticket_id)
    a11y = _run_a11y_axis(ticket_id)

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
```

- [ ] **Step 4: 통과 확인**

Run:
```bash
pytest tests/harness/test_gate.py -v
```
Expected: 3개 모두 PASS

- [ ] **Step 5: Commit**

```bash
git add harness/gate.py tests/harness/test_gate.py
git commit -m "기능: 3축 통합 게이트 (gate.py)

visual / behavior / a11y 3 영역을 별도 pytest subprocess 로 격리 실행하고
결과를 gate_runs 단일 행으로 기록. red / green 두 단계만 허용.
실패 시 diff PNG / log / violations JSON 경로를 함께 보존."
```

---

## Task 7: 마크다운 진행 보드 생성기 (board.py)

**Files:**
- Create: `harness/board.py`, `tests/harness/test_board.py`

- [ ] **Step 1: `tests/harness/test_board.py` 작성**

```python
"""harness.board — 마크다운 진행 보드 생성 단위 테스트."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.harness


def test_render_overview_lists_all_tickets(db_conn: sqlite3.Connection) -> None:
    from harness import board, ticket

    ticket.open_ticket(db_conn, wave=1, component="empty-state")
    t2 = ticket.open_ticket(db_conn, wave=2, component="cmd-palette")
    ticket.update_status(db_conn, t2.id, "design")

    md = board.render_overview(db_conn)
    assert "# UI/UX Overhaul — 진행 보드" in md
    assert "T-101" in md
    assert "empty-state" in md
    assert "T-201" in md
    assert "cmd-palette" in md
    assert "design" in md


def test_render_overview_groups_by_wave(db_conn: sqlite3.Connection) -> None:
    from harness import board, ticket

    ticket.open_ticket(db_conn, wave=1, component="a")
    ticket.open_ticket(db_conn, wave=3, component="b")

    md = board.render_overview(db_conn)
    assert "## Wave 1 · Visual Polish" in md
    assert "## Wave 3 · Accessibility & Mobile" in md
    # Wave 2 도 헤더는 표시되어야 한다 (티켓 0개여도)
    assert "## Wave 2 · Interaction & Focus" in md


def test_write_overview_creates_file(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from harness import board, ticket

    ticket.open_ticket(db_conn, wave=1, component="x")
    target = tmp_path / "00-overview.md"
    board.write_overview(db_conn, target)
    assert target.exists()
    content = target.read_text()
    assert "T-101" in content
```

- [ ] **Step 2: 실패 확인**

Run:
```bash
pytest tests/harness/test_board.py -v
```
Expected: 3개 모두 FAIL

- [ ] **Step 3: `harness/board.py` 작성**

```python
"""마크다운 진행 보드 자동 생성.

SQLite 의 tickets / gate_runs 를 읽어
docs/superpowers/ui-ux-overhaul/00-overview.md 를 재생성한다.

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §4.5
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

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
    row = conn.execute(
        "SELECT visual_pass, behavior_pass, a11y_pass, phase "
        "FROM gate_runs WHERE ticket_id = ? ORDER BY id DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()
    if row is None:
        return "—"
    def m(v: int) -> str:
        return "✓" if v else "✗"
    return (
        f"{row['phase']}: V{m(row['visual_pass'])} "
        f"B{m(row['behavior_pass'])} A{m(row['a11y_pass'])}"
    )


def render_overview(conn: sqlite3.Connection) -> str:
    """현재 SQLite 상태로부터 보드 마크다운을 생성한다."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
            "SELECT id, component, status, pr_number FROM tickets "
            "WHERE wave = ? ORDER BY id",
            (wave,),
        ).fetchall()
        if not rows:
            lines.append("_티켓 없음_")
            lines.append("")
            continue
        lines.append("| 티켓 | 컴포넌트 | 상태 | 최근 게이트 | PR |")
        lines.append("|------|----------|------|-------------|----|")
        for r in rows:
            emoji = STATUS_EMOJI.get(r["status"], "")
            gate = _latest_gate_summary(conn, r["id"])
            pr = f"#{r['pr_number']}" if r["pr_number"] else "—"
            lines.append(
                f"| {r['id']} | `{r['component']}` | {emoji} {r['status']} | {gate} | {pr} |"
            )
        lines.append("")

    return "\n".join(lines)


def write_overview(conn: sqlite3.Connection, path: Path) -> None:
    """render_overview() 결과를 파일에 저장한다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_overview(conn))
```

- [ ] **Step 4: 통과 확인**

Run:
```bash
pytest tests/harness/test_board.py -v
```
Expected: 3개 모두 PASS

- [ ] **Step 5: Commit**

```bash
git add harness/board.py tests/harness/test_board.py
git commit -m "기능: 마크다운 진행 보드 생성기 (board.py)

SQLite 에서 tickets / gate_runs 읽어
docs/superpowers/ui-ux-overhaul/00-overview.md 자동 생성.
Wave 별 그룹핑, 상태 이모지, 최근 게이트 요약 포함."
```

---

## Task 8: CLI 라우팅 (cli.py)

**Files:**
- Create: `harness/cli.py`, `tests/harness/test_cli.py`

**컨텍스트:** `python -m harness <verb> <noun> [args]` 형태. argparse 서브명령. 단순한 verb-noun 매핑이라 의존성 없이 stdlib argparse 만 사용.

지원 명령:
- `harness ticket open --wave N --component X` (출력: 티켓 id)
- `harness ticket list [--wave N] [--status S]`
- `harness ticket show <id>`
- `harness ticket close <id> --pr N`
- `harness gate run <ticket-id> --phase red|green`
- `harness board rebuild`

- [ ] **Step 1: `tests/harness/test_cli.py` 작성**

```python
"""harness.cli — CLI 명령 라우팅 단위 테스트.

CliRunner 패턴 대신 monkeypatch 로 sys.argv + db_path 환경변수 주입.
"""
from __future__ import annotations

import os
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


def test_cli_no_args_prints_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    db = tmp_path / "harness.db"
    rc = _run_cli(monkeypatch, db, [])
    # argparse 가 인자 없을 때 usage 를 stderr 에 인쇄하고 비정상 종료.
    assert rc != 0
```

- [ ] **Step 2: 실패 확인**

Run:
```bash
pytest tests/harness/test_cli.py -v
```
Expected: 5개 모두 FAIL

- [ ] **Step 3: `harness/cli.py` 작성**

```python
"""argparse 기반 CLI 라우팅.

명령 구조: `python -m harness <verb> <noun> [args]`

환경변수:
    HARNESS_DB           — SQLite 파일 경로 (기본: state/harness.db)
    HARNESS_BOARD_PATH   — 보드 파일 경로 (기본: docs/superpowers/ui-ux-overhaul/00-overview.md)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from harness import board, db, gate, ticket

DEFAULT_DB_PATH = Path("state/harness.db")
DEFAULT_BOARD_PATH = Path("docs/superpowers/ui-ux-overhaul/00-overview.md")


def _db_path() -> Path:
    return Path(os.environ.get("HARNESS_DB", DEFAULT_DB_PATH))


def _board_path() -> Path:
    return Path(os.environ.get("HARNESS_BOARD_PATH", DEFAULT_BOARD_PATH))


def _connect() -> "object":
    """DB 연결 + 스키마 초기화."""
    conn = db.connect(_db_path())
    db.init_schema(conn)
    return conn


# ----- ticket 서브명령 -----

def _cmd_ticket_open(args: argparse.Namespace) -> int:
    conn = _connect()
    t = ticket.open_ticket(conn, wave=args.wave, component=args.component)
    print(t.id)
    return 0


def _cmd_ticket_list(args: argparse.Namespace) -> int:
    conn = _connect()
    rows = ticket.list_tickets(conn, wave=args.wave, status=args.status)
    if not rows:
        print("(no tickets)")
        return 0
    print(f"{'ID':<8} {'WAVE':<6} {'STATUS':<10} COMPONENT")
    for r in rows:
        print(f"{r.id:<8} {r.wave:<6} {r.status:<10} {r.component}")
    return 0


def _cmd_ticket_show(args: argparse.Namespace) -> int:
    conn = _connect()
    t = ticket.get_ticket(conn, args.ticket_id)
    if t is None:
        print(f"ticket not found: {args.ticket_id}", file=sys.stderr)
        return 1
    print(json.dumps(
        {
            "id": t.id, "wave": t.wave, "component": t.component,
            "status": t.status, "pr_number": t.pr_number,
            "created_at": t.created_at, "updated_at": t.updated_at,
        },
        indent=2,
    ))
    return 0


def _cmd_ticket_close(args: argparse.Namespace) -> int:
    conn = _connect()
    ticket.close_ticket(conn, args.ticket_id, pr_number=args.pr)
    print(f"closed {args.ticket_id} -> PR #{args.pr}")
    return 0


# ----- gate 서브명령 -----

def _cmd_gate_run(args: argparse.Namespace) -> int:
    conn = _connect()
    result = gate.run_gate(conn, ticket_id=args.ticket_id, phase=args.phase)
    print(f"gate {args.phase} for {args.ticket_id}")
    print(f"  visual    {'PASS' if result.visual.passed else 'FAIL'}")
    print(f"  behavior  {'PASS' if result.behavior.passed else 'FAIL'}")
    print(f"  a11y      {'PASS' if result.a11y.passed else 'FAIL'}")
    return 0 if result.all_passed else 2


# ----- board 서브명령 -----

def _cmd_board_rebuild(args: argparse.Namespace) -> int:
    conn = _connect()
    board.write_overview(conn, _board_path())
    print(f"board written to {_board_path()}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="UI/UX Overhaul 풀스택 하네스")
    sub = p.add_subparsers(dest="verb", required=True)

    # ticket
    t_parent = sub.add_parser("ticket", help="티켓 라이프사이클")
    t_sub = t_parent.add_subparsers(dest="ticket_verb", required=True)

    t_open = t_sub.add_parser("open", help="새 티켓 발급")
    t_open.add_argument("--wave", type=int, required=True, choices=[1, 2, 3])
    t_open.add_argument("--component", required=True)
    t_open.set_defaults(func=_cmd_ticket_open)

    t_list = t_sub.add_parser("list", help="티켓 목록")
    t_list.add_argument("--wave", type=int, choices=[1, 2, 3])
    t_list.add_argument("--status")
    t_list.set_defaults(func=_cmd_ticket_list)

    t_show = t_sub.add_parser("show", help="티켓 상세")
    t_show.add_argument("ticket_id")
    t_show.set_defaults(func=_cmd_ticket_show)

    t_close = t_sub.add_parser("close", help="티켓 종료 (머지)")
    t_close.add_argument("ticket_id")
    t_close.add_argument("--pr", type=int, required=True)
    t_close.set_defaults(func=_cmd_ticket_close)

    # gate
    g_parent = sub.add_parser("gate", help="3축 게이트 실행")
    g_sub = g_parent.add_subparsers(dest="gate_verb", required=True)
    g_run = g_sub.add_parser("run", help="게이트 1회 실행")
    g_run.add_argument("ticket_id")
    g_run.add_argument("--phase", required=True, choices=["red", "green"])
    g_run.set_defaults(func=_cmd_gate_run)

    # board
    b_parent = sub.add_parser("board", help="마크다운 진행 보드")
    b_sub = b_parent.add_subparsers(dest="board_verb", required=True)
    b_rebuild = b_sub.add_parser("rebuild", help="보드 재생성")
    b_rebuild.set_defaults(func=_cmd_board_rebuild)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    rc = args.func(args)
    if rc:
        sys.exit(rc)
```

- [ ] **Step 4: 통과 확인**

Run:
```bash
pytest tests/harness/test_cli.py -v
```
Expected: 5개 모두 PASS

- [ ] **Step 5: 실제 CLI 동작 smoke test**

Run:
```bash
HARNESS_DB=/tmp/harness-smoke.db python -m harness ticket open --wave 1 --component empty-state
```
Expected: `T-101`

```bash
HARNESS_DB=/tmp/harness-smoke.db python -m harness ticket list
```
Expected: 헤더 + `T-101 1 pending empty-state` 라인

```bash
rm /tmp/harness-smoke.db
```

- [ ] **Step 6: Commit**

```bash
git add harness/cli.py tests/harness/test_cli.py
git commit -m "기능: 하네스 CLI 라우팅 (cli.py)

argparse 기반 verb-noun 명령 구조. ticket open/list/show/close,
gate run, board rebuild 지원. HARNESS_DB / HARNESS_BOARD_PATH 환경변수로
경로 주입. python -m harness 로 호출."
```

---

## Task 9: 4 서브에이전트 정의 파일

**Files:**
- Create: `.claude/agents/ui-ux/pm.md`, `.claude/agents/ui-ux/designer.md`, `.claude/agents/ui-ux/frontend.md`, `.claude/agents/ui-ux/qa.md`

**컨텍스트:** Claude Code 의 `.claude/agents/<path>/<name>.md` 는 frontmatter + 본문 구조다. Agent 툴로 디스패치할 때 frontmatter 의 `description` 이 selector 역할을 한다.

- [ ] **Step 1: `.claude/agents/ui-ux/pm.md` 작성**

```markdown
---
name: ui-ux-pm
description: UI/UX Overhaul 의 PM. 티켓 발급, 게이트 결과 검토, Designer/Frontend/QA 에이전트 디스패치 순서 결정. spec(2026-04-28-ui-ux-overhaul-design.md) 의 단일 진실 공급원.
tools: Read, Bash, Edit, Write
---

# Role: UI/UX Overhaul PM

## 사명
`docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md` 의 7개 컴포넌트가
3축 게이트(visual / behavior / a11y) 모두 통과하도록 사이클을 운영한다.

## 입력
- 본 spec
- `docs/SYSTEM_AUDIT_2026-04-28.md` 의 §1 미흡 항목
- 진행 중인 티켓들의 SQLite 상태 (`python -m harness ticket list`)

## 출력 (산출물)
- `python -m harness ticket open --wave N --component X` 으로 발급한 티켓 id
- 게이트 결과를 사용자에게 한국어로 요약
- 다음 액션 제안 (재구현 / 베이스라인 갱신 / 시나리오 보정)

## 절대 금지
- spec 의 비목표(§1.2) 영역 침범 — 백엔드 API 변경, 미구현 기능 추가, 신규 의존성
- 게이트 결과 무시 머지
- 한 컴포넌트 = 한 PR 규칙 위반

## 도구 사용 규칙
- `python -m harness ticket open|list|show|close` 로 티켓 라이프사이클 관리
- `python -m harness gate run <id> --phase red|green` 로 게이트 실행
- `python -m harness board rebuild` 후 `docs/superpowers/ui-ux-overhaul/00-overview.md` 확인

## 협업 흐름 (한 컴포넌트 단위)
1. `harness ticket open` → ticket id 확보
2. **Designer 디스패치** — 목업 + 시각 베이스라인 (라이트/다크/모바일 3변종)
3. **QA 디스패치** — Playwright 행동 시나리오 + axe-core 룰셋
4. `harness gate run <id> --phase red` → 3축 모두 FAIL 확인 (확인 안 되면 시나리오 결함)
5. **Frontend 디스패치** — 3축 모두 PASS 시키는 최소 구현
6. `harness gate run <id> --phase green` → 3축 모두 PASS 확인
7. 합동 리팩터 리뷰 (Designer/Frontend/QA 에게 짧게 검토 의뢰)
8. PR 생성 → 머지 → `harness ticket close <id> --pr <N>`
9. `harness board rebuild` 로 보드 갱신
```

- [ ] **Step 2: `.claude/agents/ui-ux/designer.md` 작성**

```markdown
---
name: ui-ux-designer
description: UI/UX Overhaul 의 디자이너. 컴포넌트 마크다운 목업 작성, Playwright 시각 베이스라인 PNG 캡처(라이트/다크/모바일 3 변종), 디자인 토큰 보강. docs/design.md 의 룰을 단일 진실 공급원으로 따름.
tools: Read, Write, Edit, Bash, Glob
---

# Role: UI/UX Overhaul Designer

## 사명
한 티켓의 컴포넌트에 대해 마크다운 목업 + 3 변종 시각 베이스라인을 작성한다.
**디자인 미흡함을 정확히 정의** 하는 것이 본 역할의 핵심.

## 입력
- 티켓 id, component, wave (PM 으로부터)
- `docs/design.md` (디자인 토큰, 컴포넌트 패턴)
- `docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md` §3 의 완료 정의

## 출력
- `docs/superpowers/ui-ux-overhaul/wave-{N}/{component}-mockup.md`
  - 목적, 디자인 토큰 사용, 라이트/다크/모바일 변종 설명, 인터랙션 노트
- `tests/ui/visual/baselines/{component}-light.png`
- `tests/ui/visual/baselines/{component}-dark.png`
- `tests/ui/visual/baselines/{component}-mobile.png` (768px 뷰포트)
- artifacts 등록: `python -m harness` 직접 호출 또는 PM 에 등록 의뢰

## 절대 금지
- `docs/design.md` 와 모순되는 새 토큰 도입 (보강은 OK, 충돌은 금지)
- 베이스라인을 게이트 실패 시 마음대로 갱신 (의도된 변경일 때만 갱신, PM 승인 필요)
- pixel-perfect 가 아닌 "느낌" 기반 평가

## 도구 사용 규칙
- 베이스라인 캡처: Playwright `expect(page).to_have_screenshot()` 호출
- 변종 전환: `page.emulate_media(color_scheme="dark")` (다크), `page.set_viewport_size({"width": 375, "height": 667})` (모바일)
```

- [ ] **Step 3: `.claude/agents/ui-ux/frontend.md` 작성**

```markdown
---
name: ui-ux-frontend
description: UI/UX Overhaul 의 프론트엔드 구현자. Designer 의 목업 + QA 의 시나리오를 입력으로 받아 ui/web/* 의 최소 변경으로 3축 게이트(visual / behavior / a11y) 모두 통과시킴. 기존 SPA·CSS 패턴 준수.
tools: Read, Edit, Write, Bash, Glob, Grep
---

# Role: UI/UX Overhaul Frontend

## 사명
Designer 베이스라인과 QA 시나리오·a11y 룰을 모두 통과시키는 **최소** 구현.

## 입력
- 티켓 id, component, wave
- Designer 산출물: `docs/superpowers/ui-ux-overhaul/wave-{N}/{component}-mockup.md` + 베이스라인 PNG 3종
- QA 산출물: `tests/ui/behavior/test_{component}.py`, `tests/ui/a11y/test_{component}.py`
- 기존 코드: `ui/web/spa.js`, `ui/web/style.css`, `ui/web/index.html`, `ui/web/app.js`
- `docs/design.md` (토큰 단일 진실 공급원)

## 출력
- `ui/web/*` 변경 (최소 diff)
- 변경된 파일을 artifacts(`kind=implementation`) 로 등록

## 절대 금지
- 신규 npm/pip 의존성 추가 (spec §1.2)
- 백엔드 코드 변경 (spec §1.2)
- 게이트 통과시키려고 시나리오 약화
- 디자인 토큰을 컴포넌트 안에 인라인 (반드시 `docs/design.md` 의 토큰 사용)

## 도구 사용 규칙
- 변경 전 `git diff` 로 현재 상태 확인
- `python -m harness gate run <id> --phase green` 으로 직접 검증
- 통과 안 되면 PM 에게 보고 (자체 판단으로 시나리오 수정 금지)
```

- [ ] **Step 4: `.claude/agents/ui-ux/qa.md` 작성**

```markdown
---
name: ui-ux-qa
description: UI/UX Overhaul 의 QA. Playwright 행동 시나리오(Given-When-Then) + axe-core 룰셋 작성. 첫 실행에서 반드시 FAIL 하는 Red 테스트, 구현 후 PASS 되는 Green 테스트를 책임짐.
tools: Read, Write, Edit, Bash, Glob
---

# Role: UI/UX Overhaul QA

## 사명
한 컴포넌트의 사용자 경험과 접근성을 자동 검증 가능한 형태로 코드화한다.
**Red 가 정확히 FAIL 하고 Green 이 정확히 PASS** 하는 시나리오가 핵심.

## 입력
- 티켓 id, component, wave
- Designer 의 마크다운 목업 (인터랙션 노트 포함)
- spec §3 의 완료 정의
- spec §5.3 통과 기준

## 출력
- `tests/ui/behavior/test_{component}.py` — Playwright 행동 시나리오
- `tests/ui/a11y/test_{component}.py` — axe-core 룰셋 검증
- artifacts 등록: `behavior_scenario`, `a11y_ruleset`

## 절대 금지
- `wcag21aaa` 같은 spec 범위 밖 룰 활성화
- "어떻게든 PASS" 만들기 위한 약화된 시나리오
- 픽셀 비교를 behavior 시나리오에 섞기 (시각은 visual 축이 담당)

## 도구 사용 규칙
- 행동 시나리오는 `@pytest.mark.ui` 마커 필수
- a11y 검사는 `axe-playwright-python` 의 `Axe` 클래스 사용
- Given-When-Then 주석으로 시나리오 의도 명시
```

- [ ] **Step 5: 4개 파일이 생성됐는지 검증**

Run:
```bash
ls -la .claude/agents/ui-ux/
```
Expected: `pm.md`, `designer.md`, `frontend.md`, `qa.md` 4개 파일

```bash
head -5 .claude/agents/ui-ux/pm.md
```
Expected: frontmatter 의 `name: ui-ux-pm` 출력

- [ ] **Step 6: Commit**

```bash
git add .claude/agents/ui-ux/
git commit -m "기능: 4 서브에이전트 정의 파일 (PM/Designer/Frontend/QA)

.claude/agents/ui-ux/ 아래 frontmatter + 본문 구조로 4 역할 정의.
각 에이전트의 사명·입력·출력·절대 금지·도구 사용 규칙 명시.
PM 이 spec 단일 진실 공급원, Designer/Frontend/QA 는 한 사이클의 3 축 분담."
```

---

## Task 10: End-to-End 데모 (샘플 컴포넌트)

**Files:**
- Create: `tests/ui/conftest.py`, `tests/ui/visual/test_demo_swatch.py`, `tests/ui/behavior/test_demo_swatch.py`, `tests/ui/a11y/test_demo_swatch.py`

**목표:** 하네스가 실제로 동작함을 증명. 본격 Wave 1 시작 전, "디자인 토큰 swatch 페이지" 라는 가짜 컴포넌트로 red→green 사이클을 한 번 돌린다. Wave 1 시작 시 본 데모 테스트는 삭제(또는 keep-as-example) 결정.

**Demo 컴포넌트:** `ui/web/_demo/swatch.html` — 디자인 토큰 색상 견본 페이지. 본 demo 는 영구 코드가 아니므로 정리 가능하지만, 본 plan 에서는 추가 후 다음 plan 시작 시 제거.

- [ ] **Step 1: `ui/web/_demo/swatch.html` 작성 (Demo 페이지)**

```bash
mkdir -p ui/web/_demo
```

`ui/web/_demo/swatch.html`:

```html
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Demo Swatch — 하네스 검증용</title>
<link rel="stylesheet" href="../style.css">
</head>
<body data-demo="swatch">
<main role="main" aria-label="디자인 토큰 견본">
  <h1>디자인 토큰 견본</h1>
  <ul role="list">
    <li><span class="swatch" style="background:var(--accent)"></span>--accent</li>
    <li><span class="swatch" style="background:var(--bg-card)"></span>--bg-card</li>
    <li><span class="swatch" style="background:var(--text-primary)"></span>--text-primary</li>
  </ul>
</main>
<style>
  body { padding: 24px; }
  .swatch { display: inline-block; width: 24px; height: 24px; border: 0.5px solid var(--border); margin-right: 8px; vertical-align: middle; }
</style>
</body>
</html>
```

- [ ] **Step 2: `tests/ui/conftest.py` 작성 (Playwright fixture)**

```python
"""tests/ui — 공용 Playwright fixture.

테스트 서버는 별도로 띄우지 않고 file:// URL 로 정적 페이지 직접 로드.
이렇게 하면 본 데모는 FastAPI 의존성 없이 동작.
실제 Wave 1+ 의 테스트는 동일 파일 또는 file:// 로드.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

# Wave 1+ 에서 webapp 띄우는 서버 fixture 가 필요해지면 그때 도입.
# 현재는 정적 HTML 만 검증.

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def demo_swatch_url() -> str:
    """`ui/web/_demo/swatch.html` 의 file:// URL."""
    p = PROJECT_ROOT / "ui" / "web" / "_demo" / "swatch.html"
    return p.as_uri()
```

- [ ] **Step 3: `tests/ui/visual/test_demo_swatch.py` 작성**

```python
"""Wave 1 시작 전 하네스 동작 검증용 demo — 시각 회귀 축.

본 파일은 Plan 1 (Wave 1 Visual Polish) 시작 시 제거 예정.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.ui]


def test_demo_swatch_light(page: Page, demo_swatch_url: str) -> None:
    """라이트 모드 베이스라인."""
    page.goto(demo_swatch_url)
    expect(page.locator("h1")).to_have_text("디자인 토큰 견본")
    # Playwright 가 첫 실행 시 베이스라인을 자동 생성.
    expect(page).to_have_screenshot("demo-swatch-light.png", max_diff_pixel_ratio=0.001)


def test_demo_swatch_dark(page: Page, demo_swatch_url: str) -> None:
    page.emulate_media(color_scheme="dark")
    page.goto(demo_swatch_url)
    expect(page).to_have_screenshot("demo-swatch-dark.png", max_diff_pixel_ratio=0.001)


def test_demo_swatch_mobile(page: Page, demo_swatch_url: str) -> None:
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto(demo_swatch_url)
    expect(page).to_have_screenshot("demo-swatch-mobile.png", max_diff_pixel_ratio=0.001)
```

- [ ] **Step 4: `tests/ui/behavior/test_demo_swatch.py` 작성**

```python
"""Wave 1 시작 전 하네스 동작 검증용 demo — 행동 축."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.ui]


def test_swatch_page_renders_three_tokens(page: Page, demo_swatch_url: str) -> None:
    """Given: demo swatch 페이지를 연다
    When:  렌더 완료를 기다린다
    Then:  3 개의 토큰 견본이 표시된다.
    """
    page.goto(demo_swatch_url)
    items = page.locator("ul[role='list'] li")
    expect(items).to_have_count(3)


def test_swatch_page_has_main_landmark(page: Page, demo_swatch_url: str) -> None:
    """Given: demo swatch 페이지
    When:  ARIA landmark 를 찾는다
    Then:  단 하나의 main 이 존재한다.
    """
    page.goto(demo_swatch_url)
    expect(page.locator("[role='main']")).to_have_count(1)
```

- [ ] **Step 5: `tests/ui/a11y/test_demo_swatch.py` 작성**

```python
"""Wave 1 시작 전 하네스 동작 검증용 demo — 접근성 축.

axe-playwright-python 의 Axe 클래스를 통해 wcag2a + wcag2aa + wcag21aa
룰셋 위반이 0건임을 검증.
"""
from __future__ import annotations

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page

pytestmark = [pytest.mark.ui]


def test_swatch_page_has_no_a11y_violations(page: Page, demo_swatch_url: str) -> None:
    page.goto(demo_swatch_url)
    axe = Axe()
    results = axe.run(
        page,
        options={"runOnly": {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag21aa"]}},
    )
    violations = results.response.get("violations", [])
    assert violations == [], (
        f"a11y violations found:\n"
        + "\n".join(f"  - {v['id']}: {v['help']}" for v in violations)
    )
```

- [ ] **Step 6: Playwright 브라우저 설치 확인**

Run:
```bash
playwright install chromium
```
Expected: 설치 완료 메시지 (이미 설치되어 있으면 즉시 종료)

- [ ] **Step 7: Demo 게이트 — Red phase 시뮬레이션**

샘플 컴포넌트는 이미 구현되어 있어 정상적으로는 PASS 한다. Red 단계를 시뮬레이션하기 위해 의도적으로 깨뜨린 후 게이트를 돌린다.

```bash
# Demo 티켓 발급
python -m harness ticket open --wave 1 --component demo-swatch
# Expected: T-101 (또는 다음 번호)
```

`ui/web/_demo/swatch.html` 의 `<h1>` 텍스트를 일시적으로 변경:

```bash
# h1 을 의도적으로 깨뜨림 (일시적)
sed -i.bak 's|디자인 토큰 견본|BROKEN|' ui/web/_demo/swatch.html
```

게이트 red 실행:

```bash
TICKET_ID=$(python -m harness ticket list --wave 1 --status pending | tail -1 | awk '{print $1}')
python -m harness gate run "$TICKET_ID" --phase red
```

Expected: `behavior FAIL` (h1 텍스트 어설션 실패), `visual FAIL` 가능, returncode 2

- [ ] **Step 8: Demo 게이트 — Green phase**

깨뜨린 부분 복원:

```bash
mv ui/web/_demo/swatch.html.bak ui/web/_demo/swatch.html
```

```bash
python -m harness gate run "$TICKET_ID" --phase green
```

Expected: `visual PASS` (베이스라인 자동 생성), `behavior PASS`, `a11y PASS`, returncode 0

> **참고:** Playwright 시각 회귀의 첫 실행은 항상 PASS 처리하며 동시에 베이스라인 PNG 를 생성한다. 본 데모에서는 그것을 활용한다.

- [ ] **Step 9: 보드 재생성 + 확인**

```bash
python -m harness board rebuild
cat docs/superpowers/ui-ux-overhaul/00-overview.md
```
Expected: `T-101 demo-swatch ... green: V✓ B✓ A✓` 라인 포함

- [ ] **Step 10: Commit (데모 결과 + 베이스라인)**

```bash
git add tests/ui/ ui/web/_demo/ docs/superpowers/ui-ux-overhaul/00-overview.md state/.gitkeep
git commit -m "기능: 하네스 end-to-end 데모 (demo-swatch 컴포넌트)

3축 게이트 PASS 검증용 샘플. Plan 1 (Wave 1 Visual Polish) 시작 시
ui/web/_demo/, tests/ui/*/test_demo_swatch.py 제거 예정.
첫 시각 회귀 베이스라인(라이트/다크/모바일) 생성 확인."
```

---

## Task 11: 회귀 테스트 일괄 실행 + README 추가

**Files:**
- Create: `harness/README.md`
- Modify: `Makefile`

- [ ] **Step 1: `harness/README.md` 작성**

````markdown
# UI/UX Overhaul 풀스택 하네스

`docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md` 의 구현체.

## 빠른 시작

```bash
pip install -e ".[dev]"
playwright install chromium

# 티켓 발급
python -m harness ticket open --wave 1 --component empty-state
# T-101

# 게이트 실행 (Red — 구현 전)
python -m harness gate run T-101 --phase red

# 게이트 실행 (Green — 구현 후)
python -m harness gate run T-101 --phase green

# 보드 재생성
python -m harness board rebuild
cat docs/superpowers/ui-ux-overhaul/00-overview.md

# 티켓 종료
python -m harness ticket close T-101 --pr 42
```

## 4 서브에이전트

`.claude/agents/ui-ux/` 아래 4 개 정의 파일:

- **PM** (`pm.md`) — 티켓 발급, 게이트 결과 검토, 다른 에이전트 디스패치
- **Designer** (`designer.md`) — 마크다운 목업 + Playwright 시각 베이스라인 (라이트/다크/모바일 3 변종)
- **Frontend** (`frontend.md`) — `ui/web/*` 의 최소 변경으로 3축 PASS 시키기
- **QA** (`qa.md`) — Playwright 행동 시나리오 (Given-When-Then) + axe-core 룰셋

## 환경변수

- `HARNESS_DB` — SQLite 파일 경로 (기본 `state/harness.db`)
- `HARNESS_BOARD_PATH` — 보드 마크다운 경로 (기본 `docs/superpowers/ui-ux-overhaul/00-overview.md`)

## 테스트

```bash
# 하네스 자체 단위 테스트
pytest -m harness -v

# UI 게이트 (Wave 1+ 의 시각/행동/a11y)
pytest -m ui -v
```

## 데이터 모델

`state/harness.db` (SQLite) — 4 테이블:

- `tickets` — 한 컴포넌트 = 한 티켓
- `artifacts` — 목업 / 베이스라인 / 시나리오 / 룰셋 / 구현 파일 레퍼런스
- `gate_runs` — 매 red/green 실행 결과
- `events` — 감사 로그
````

- [ ] **Step 2: `Makefile` 에 하네스 타겟 추가**

`Makefile` 끝에 추가:

```makefile

# === UI/UX Overhaul 하네스 ===

.PHONY: harness-test harness-board harness-clean

harness-test:
	pytest -m harness -v

harness-board:
	python -m harness board rebuild
	@echo "📋 보드: docs/superpowers/ui-ux-overhaul/00-overview.md"

harness-clean:
	@echo "⚠️  state/harness.db 와 모든 시각 회귀 임시 산출물을 삭제합니다."
	rm -f state/harness.db state/harness.db-journal
	rm -rf tests/ui/visual/diffs tests/ui/__snapshots__
	rm -rf state/gate-logs
```

- [ ] **Step 3: 전체 회귀 실행 — 모든 단위 테스트 통과 확인**

Run:
```bash
pytest -m harness -v
```
Expected: 모든 하네스 단위 테스트 PASS (db / ticket / snapshot / behavior / a11y / gate / board / cli)

- [ ] **Step 4: ruff + mypy 통과 확인**

Run:
```bash
ruff check harness/ tests/harness/
```
Expected: `All checks passed!` (또는 0 오류)

```bash
mypy harness/
```
Expected: `Success: no issues found` (또는 외부 의존성 missing import 만)

- [ ] **Step 5: Commit**

```bash
git add harness/README.md Makefile
git commit -m "문서: 하네스 README + Makefile 타겟 (harness-test/board/clean)

빠른 시작, 4 에이전트 안내, 환경변수, 데이터 모델 요약.
make harness-test / harness-board / harness-clean 단축 명령 추가."
```

---

## Self-Review (Plan 작성자 자체 검토)

### 1. Spec 커버리지

| Spec 섹션 | Plan Task | 상태 |
|----------|-----------|------|
| §3 작업 범위 (7개 항목) | Plan 1/2/3 (별도) — Plan 0 은 인프라만 | ✓ Plan 0 범위 밖, 후속 plan 에서 |
| §4.1 디렉토리 구조 | Task 1 | ✓ |
| §4.2 데이터 모델 (SQLite 4 테이블) | Task 2 | ✓ |
| §4.3 4 서브에이전트 책임 | Task 9 | ✓ |
| §4.4 TDD 사이클 (Red/Green) | Task 6 + Task 10 (데모) | ✓ |
| §4.5 CLI 명령 (ticket/gate/snapshot/board) | Task 8 (cli), Task 4 (snapshot 헬퍼) | ✓ — `harness snapshot baseline` CLI 는 Wave 1 plan 으로 미룸 (Designer 가 실제로 페이지를 렌더할 때 필요) |
| §5 테스트 전략 | Task 6 (gate), Task 10 (데모) | ✓ |
| §6 에러 처리 | Task 6 (gate 실패 시 detail_path 기록) | ✓ |
| §7 마이그레이션 | Plan 0 범위는 신규 파일만, 기존 코드 수정 없음 | ✓ |

**갭:** spec §4.5 의 `harness snapshot baseline --component X --variant Y` CLI 명령이 Plan 0 에서 누락. 이는 Designer 가 Wave 1 첫 컴포넌트 작업 시 필요한 기능이지만, "Designer 가 직접 Playwright 코드를 실행한다"는 Wave 1 plan 결정에 따라 옮길 수 있다. **Plan 1 의 첫 Task 에서 추가** 로 해결한다.

### 2. Placeholder 스캔

- 모든 step 이 구체 코드/명령/예상 결과 포함 ✓
- `TBD`/`TODO`/`implement later` 없음 ✓
- 모든 함수·클래스 시그니처 명시 ✓

### 3. Type 일관성

- `db.connect()` → `sqlite3.Connection` ✓
- `ticket.open_ticket()` → `Ticket` (frozen dataclass) ✓
- `gate.run_gate()` → `GateResult` (`AxisResult` 3개 + `all_passed` property) ✓
- `snapshot.register_baseline()` 시그니처: `(conn, *, ticket_id, path, variant)` ✓ — 모든 호출처와 일치

### 4. 의존성 방향

```
db.py (no internal deps)
  ↑
ticket.py / snapshot.py / behavior.py / a11y.py
  ↑
gate.py (orchestrates the four above)
  ↑
board.py (reads db, no other internal deps)
  ↑
cli.py (uses db, ticket, gate, board)
  ↑
__main__.py (uses cli)
```

순환 없음 ✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-28-ui-ux-harness-bootstrap.md`. 두 가지 실행 옵션이 있습니다:

**1. Subagent-Driven (권장)** — 각 Task 마다 새 서브에이전트 디스패치, Task 사이에 리뷰. 빠른 반복.
**2. Inline Execution** — 본 세션에서 `executing-plans` 스킬로 일괄 실행, 체크포인트마다 사용자 검토.

본 plan 은 11 task / 약 60 step 이며, **하나의 큰 PR (혹은 11 개의 작은 PR) 로 머지** 하는 것을 권장합니다. 합쳐서 한 PR 이 된다면 PR 제목: "feat: UI/UX Overhaul 풀스택 하네스 셋업 (Plan 0)".
