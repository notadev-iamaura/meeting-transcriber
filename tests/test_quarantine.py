"""Quarantine 이동 헬퍼 테스트."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.quarantine import (
    QuarantineError,
    move_to_quarantine,
    move_to_quarantine_exact,
    restore_from_quarantine,
)


def test_파일을_quarantine으로_이동(tmp_path: Path):
    audio_dir = tmp_path / "audio_input"
    audio_dir.mkdir()
    quarantine_dir = tmp_path / "audio_quarantine"

    src = audio_dir / "meeting_test.wav"
    src.write_bytes(b"fake wav data")

    dest = move_to_quarantine(src, quarantine_dir, reason="저볼륨")

    assert not src.exists()
    assert dest.exists()
    assert dest.parent == quarantine_dir
    assert dest.name == "meeting_test.wav"
    assert dest.read_bytes() == b"fake wav data"


def test_quarantine_디렉토리가_없으면_자동_생성(tmp_path: Path):
    src = tmp_path / "audio.wav"
    src.write_bytes(b"data")
    quarantine_dir = tmp_path / "does" / "not" / "exist"

    dest = move_to_quarantine(src, quarantine_dir, reason="test")

    assert quarantine_dir.exists()
    assert dest.exists()


def test_동일한_이름이_이미_있으면_suffix_추가(tmp_path: Path):
    quarantine_dir = tmp_path / "q"
    quarantine_dir.mkdir()
    existing = quarantine_dir / "meeting.wav"
    existing.write_bytes(b"old")

    src = tmp_path / "meeting.wav"
    src.write_bytes(b"new")

    dest = move_to_quarantine(src, quarantine_dir, reason="중복 테스트")

    assert existing.read_bytes() == b"old"  # 기존 파일 보존
    assert dest.exists()
    assert dest.name != "meeting.wav"  # 이름 변경됨
    assert dest.read_bytes() == b"new"


def test_원본이_없으면_QuarantineError(tmp_path: Path):
    quarantine_dir = tmp_path / "q"
    src = tmp_path / "missing.wav"

    with pytest.raises(QuarantineError):
        move_to_quarantine(src, quarantine_dir, reason="test")


def test_이동_이력을_reason과_함께_로그(tmp_path: Path, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="core.quarantine")

    src = tmp_path / "audio.wav"
    src.write_bytes(b"x")
    quarantine_dir = tmp_path / "q"

    move_to_quarantine(src, quarantine_dir, reason="저볼륨: mean=-48.6dB")

    assert any("저볼륨" in r.message for r in caplog.records)


def test_quarantine_디렉토리_생성_실패는_QuarantineError이고_원본을_보존(
    tmp_path: Path,
) -> None:
    """mkdir 실패를 raw OSError로 누출하거나 원본을 잃어서는 안 된다."""
    src = tmp_path / "audio.wav"
    src.write_bytes(b"source-must-survive")
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_bytes(b"block mkdir")

    with pytest.raises(QuarantineError):
        move_to_quarantine(
            src,
            parent_file / "audio_quarantine",
            reason="mkdir failure",
        )

    assert src.read_bytes() == b"source-must-survive"


def test_동명_세_파일을_동시에_격리해도_모든_내용을_고유하게_보존(
    tmp_path: Path,
) -> None:
    """초 단위 suffix 충돌이나 TOCTOU로 기존 격리 파일을 덮어쓰지 않는다."""
    quarantine_dir = tmp_path / "audio_quarantine"
    quarantine_dir.mkdir()
    sources: list[Path] = []
    expected_contents = {b"first", b"second", b"third"}
    for index, content in enumerate(sorted(expected_contents)):
        source_dir = tmp_path / f"source-{index}"
        source_dir.mkdir()
        source = source_dir / "same.wav"
        source.write_bytes(content)
        sources.append(source)

    barrier = threading.Barrier(len(sources))

    def _move(source: Path) -> Path:
        barrier.wait(timeout=5)
        return move_to_quarantine(source, quarantine_dir, reason="concurrent collision")

    with ThreadPoolExecutor(max_workers=3) as executor:
        destinations = list(executor.map(_move, sources))

    assert len(set(destinations)) == 3
    assert all(destination.is_file() for destination in destinations)
    assert {destination.read_bytes() for destination in destinations} == expected_contents
    assert all(not source.exists() for source in sources)


def test_quarantine_root가_symlink면_거부하고_원본과_target을_보존(
    tmp_path: Path,
) -> None:
    """격리 root symlink를 따라가 허용된 데이터 경계 밖에 쓰지 않는다."""
    src = tmp_path / "audio.wav"
    src.write_bytes(b"source")
    real_target = tmp_path / "external-quarantine"
    real_target.mkdir()
    quarantine_link = tmp_path / "audio_quarantine"
    quarantine_link.symlink_to(real_target, target_is_directory=True)

    with pytest.raises(QuarantineError):
        move_to_quarantine(src, quarantine_link, reason="symlink root")

    assert src.read_bytes() == b"source"
    assert list(real_target.iterdir()) == []


def test_원본_중간_경로_symlink를_따라가지_않음(tmp_path: Path) -> None:
    """원본 parent의 중간 component가 symlink이면 외부 target을 건드리지 않는다."""
    external = tmp_path / "external-source"
    external.mkdir()
    target = external / "audio.wav"
    target.write_bytes(b"external-source")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(external, target_is_directory=True)

    with pytest.raises(QuarantineError):
        move_to_quarantine(
            linked_parent / "audio.wav",
            tmp_path / "quarantine",
            reason="intermediate source symlink",
        )

    assert target.read_bytes() == b"external-source"
    assert not (tmp_path / "quarantine" / "audio.wav").exists()


def test_quarantine_parent_중간_symlink_escape를_거부(tmp_path: Path) -> None:
    """격리 경로의 parent symlink로 base 밖에 쓰는 것을 차단한다."""
    src = tmp_path / "audio.wav"
    src.write_bytes(b"source")
    external = tmp_path / "external-quarantine-parent"
    external.mkdir()
    qparent = tmp_path / "qparent"
    qparent.symlink_to(external, target_is_directory=True)

    with pytest.raises(QuarantineError):
        move_to_quarantine(src, qparent / "quarantine", reason="parent symlink")

    assert src.read_bytes() == b"source"
    assert list(external.iterdir()) == []


def test_quarantine은_placeholder_replace_대신_hardlink_exclusive를_사용(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """placeholder를 교체하는 순간의 TOCTOU를 없애고 os.replace를 쓰지 않는다."""
    src = tmp_path / "audio.wav"
    src.write_bytes(b"source")

    def _replace_forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("os.replace placeholder flow must not be used")

    monkeypatch.setattr(os, "replace", _replace_forbidden)

    dest = move_to_quarantine(src, tmp_path / "quarantine", reason="hardlink")

    assert dest.read_bytes() == b"source"
    assert not src.exists()


def test_quarantine_dir이_이동_후_재생성되면_잘못된_lexical_path를_반환하지_않음(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """열어둔 격리 dir과 반환 경로의 dir identity가 다르면 실패한다."""
    src = tmp_path / "audio.wav"
    src.write_bytes(b"source")
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    renamed = tmp_path / "quarantine-renamed"
    original_unlink = os.unlink
    swapped = False

    def _unlink_and_swap(path: str, *args: object, **kwargs: object) -> None:
        nonlocal swapped
        original_unlink(path, *args, **kwargs)
        if path == src.name and not swapped:
            swapped = True
            quarantine.rename(renamed)
            quarantine.mkdir()

    monkeypatch.setattr(os, "unlink", _unlink_and_swap)

    with pytest.raises(QuarantineError):
        move_to_quarantine(src, quarantine, reason="directory swapped")

    assert src.read_bytes() == b"source"
    assert list(quarantine.iterdir()) == []


def test_validation_지문과_달라진_원본은_격리하지_않음(tmp_path: Path) -> None:
    """검증 뒤 교체·변경된 파일을 stale 판정으로 격리하지 않는다."""
    src = tmp_path / "audio.wav"
    src.write_bytes(b"validated")
    file_stat = src.stat()
    expected = (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )
    src.write_bytes(b"changed after validation")
    quarantine = tmp_path / "quarantine"

    with pytest.raises(QuarantineError, match="변경"):
        move_to_quarantine(
            src,
            quarantine,
            reason="stale validation",
            expected_identity=expected,
        )

    assert src.read_bytes() == b"changed after validation"
    assert list(quarantine.iterdir()) == []


def test_exact_quarantine은_예약된_경로와_4tuple_identity를_사용(
    tmp_path: Path,
) -> None:
    """durable recovery는 ctime 제외 지문으로 exact 목적지에만 이동한다."""
    source_dir = tmp_path / "audio_input"
    source_dir.mkdir()
    source = source_dir / "meeting.wav"
    source.write_bytes(b"durable-source")
    source_stat = source.stat()
    expected = (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
    )
    destination = tmp_path / "audio_quarantine" / "meeting_token.wav"

    moved = move_to_quarantine_exact(
        source,
        destination,
        reason="durable recovery",
        expected_identity=expected,
    )

    assert moved == destination
    assert not source.exists()
    assert destination.read_bytes() == b"durable-source"


def test_exact_quarantine은_기존목적지를_덮어쓰거나_이름변경하지_않음(
    tmp_path: Path,
) -> None:
    """예약 목적지 충돌은 원본·기존 격리 파일을 모두 보존한다."""
    source = tmp_path / "audio.wav"
    source.write_bytes(b"new-source")
    source_stat = source.stat()
    expected = (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
    )
    destination = tmp_path / "quarantine" / "reserved.wav"
    destination.parent.mkdir()
    destination.write_bytes(b"existing-quarantine")

    with pytest.raises(QuarantineError, match="이미 존재"):
        move_to_quarantine_exact(
            source,
            destination,
            reason="collision",
            expected_identity=expected,
        )

    assert source.read_bytes() == b"new-source"
    assert destination.read_bytes() == b"existing-quarantine"
    assert list(destination.parent.iterdir()) == [destination]


def test_delete_rollback은_quarantine_parent_생성전_crash를_멱등복구(
    tmp_path: Path,
) -> None:
    """durable prepare 뒤 mover 진입 전 종료되어도 원본이 있으면 성공한다."""
    source_dir = tmp_path / "audio_input"
    source_dir.mkdir()
    source = source_dir / "meeting.wav"
    source.write_bytes(b"still-source")
    source_stat = source.stat()
    expected = (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
    )
    quarantine = tmp_path / "never-created" / "deleted-token.audio"

    restored = restore_from_quarantine(
        source,
        quarantine,
        expected_identity=expected,
        reason="startup rollback before move",
    )

    assert restored == source
    assert source.read_bytes() == b"still-source"
    assert not quarantine.parent.exists()


def test_delete_rollback은_이동된_exact_quarantine을_원위치로_복구(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "audio_input"
    source_dir.mkdir()
    source = source_dir / "meeting.wav"
    source.write_bytes(b"moved-source")
    source_stat = source.stat()
    expected = (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
    )
    quarantine = tmp_path / "audio_quarantine" / "deleted-token.audio"
    move_to_quarantine_exact(
        source,
        quarantine,
        reason="delete prepare",
        expected_identity=expected,
    )

    restore_from_quarantine(
        source,
        quarantine,
        expected_identity=expected,
        reason="startup rollback after move",
    )

    assert source.read_bytes() == b"moved-source"
    assert not quarantine.exists()
