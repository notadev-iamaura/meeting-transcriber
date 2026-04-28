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
