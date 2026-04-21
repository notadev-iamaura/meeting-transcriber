"""Quarantine 이동 헬퍼 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.quarantine import QuarantineError, move_to_quarantine


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
