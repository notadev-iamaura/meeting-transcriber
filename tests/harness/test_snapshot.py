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


def test_register_baseline_stores_sha256(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    from harness import snapshot, ticket

    t = ticket.open_ticket(db_conn, wave=1, component="x")
    fake_png = tmp_path / "img.png"
    fake_png.write_bytes(b"hello world")

    snapshot.register_baseline(db_conn, ticket_id=t.id, path=fake_png, variant="dark")

    row = db_conn.execute("SELECT sha256 FROM artifacts WHERE ticket_id = ?", (t.id,)).fetchone()
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


# === pixel_diff_ratio tests ===


def test_pixel_diff_identical_images(tmp_path: Path) -> None:
    """동일한 이미지는 diff 0.0."""
    from PIL import Image

    from harness import snapshot

    img = Image.new("RGB", (10, 10), color=(255, 0, 0))
    p1 = tmp_path / "a.png"
    p2 = tmp_path / "b.png"
    img.save(p1)
    img.save(p2)
    assert snapshot.pixel_diff_ratio(p1, p2) == 0.0


def test_pixel_diff_completely_different_images(tmp_path: Path) -> None:
    """완전히 다른 이미지는 diff 1.0 (모든 픽셀 차이)."""
    from PIL import Image

    from harness import snapshot

    p1 = tmp_path / "red.png"
    p2 = tmp_path / "blue.png"
    Image.new("RGB", (10, 10), color=(255, 0, 0)).save(p1)
    Image.new("RGB", (10, 10), color=(0, 0, 255)).save(p2)
    assert snapshot.pixel_diff_ratio(p1, p2) == 1.0


def test_pixel_diff_different_sizes(tmp_path: Path) -> None:
    """크기가 다르면 1.0 (비교 불가, 100% 차이로 처리)."""
    from PIL import Image

    from harness import snapshot

    p1 = tmp_path / "small.png"
    p2 = tmp_path / "big.png"
    Image.new("RGB", (10, 10), color=(0, 0, 0)).save(p1)
    Image.new("RGB", (20, 20), color=(0, 0, 0)).save(p2)
    assert snapshot.pixel_diff_ratio(p1, p2) == 1.0


def test_pixel_diff_partial_difference(tmp_path: Path) -> None:
    """절반만 다른 이미지는 diff 약 0.5."""
    import numpy as np
    from PIL import Image

    from harness import snapshot

    a = np.zeros((10, 10, 3), dtype=np.uint8)
    b = np.zeros((10, 10, 3), dtype=np.uint8)
    b[:, :5] = [255, 0, 0]  # 왼쪽 절반만 빨강
    p1 = tmp_path / "a.png"
    p2 = tmp_path / "b.png"
    Image.fromarray(a).save(p1)
    Image.fromarray(b).save(p2)
    ratio = snapshot.pixel_diff_ratio(p1, p2)
    assert 0.49 <= ratio <= 0.51


# === assert_visual_match tests (페이지 캡처 동작은 모킹) ===


def test_assert_visual_match_creates_baseline_when_missing(tmp_path: Path) -> None:
    """베이스라인 미존재 시 자동 생성하고 PASS."""
    from PIL import Image

    from harness import snapshot

    baseline = tmp_path / "baselines" / "x-light.png"
    assert not baseline.exists()

    # 가짜 page.screenshot() 동작: 임시 PNG 파일을 직접 작성
    fake_capture = tmp_path / "capture.png"
    Image.new("RGB", (10, 10), color=(50, 50, 50)).save(fake_capture)

    # 헬퍼 시그니처: assert_visual_match(actual_path, baseline_path, max_diff_pixel_ratio)
    # 베이스라인 미존재 → fake_capture 를 baseline 으로 복사 + PASS (예외 없음)
    snapshot.assert_visual_match(fake_capture, baseline, max_diff_pixel_ratio=0.001)
    assert baseline.exists()


def test_assert_visual_match_raises_on_diff(tmp_path: Path) -> None:
    """베이스라인 존재 + 차이 > 임계 → AssertionError."""
    import pytest as pt
    from PIL import Image

    from harness import snapshot

    baseline = tmp_path / "baselines" / "x-light.png"
    baseline.parent.mkdir(parents=True)
    Image.new("RGB", (10, 10), color=(0, 0, 0)).save(baseline)

    actual = tmp_path / "capture.png"
    Image.new("RGB", (10, 10), color=(255, 255, 255)).save(actual)  # 100% 차이

    with pt.raises(AssertionError, match="visual diff"):
        snapshot.assert_visual_match(actual, baseline, max_diff_pixel_ratio=0.001)
