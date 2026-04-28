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
