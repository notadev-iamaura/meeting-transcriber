"""HuggingFace CLI 토큰 캐시의 비밀 없는 검증 테스트."""

from __future__ import annotations

from pathlib import Path

from core.huggingface_credentials import (
    inspect_huggingface_cli_token_cache,
    read_huggingface_cli_token,
)


def test_private_regular_cache_is_usable(tmp_path: Path) -> None:
    """소유자 전용 일반 파일만 런타임 credential으로 읽는다."""
    token_path = tmp_path / "token"
    token_path.write_text("test-cli-token\n", encoding="utf-8")
    token_path.chmod(0o600)

    status = inspect_huggingface_cli_token_cache(token_path)

    assert status.exists is True
    assert status.private is True
    assert status.usable is True
    assert read_huggingface_cli_token(token_path) == "test-cli-token"


def test_group_or_other_readable_cache_is_rejected(tmp_path: Path) -> None:
    """다른 사용자가 읽을 수 있는 cache는 앱·LaunchAgent가 사용하지 않는다."""
    token_path = tmp_path / "token"
    token_path.write_text("test-cli-token\n", encoding="utf-8")
    token_path.chmod(0o644)

    status = inspect_huggingface_cli_token_cache(token_path)

    assert status.usable is False
    assert status.reason == "permissions"
    assert read_huggingface_cli_token(token_path) is None


def test_symlink_cache_is_rejected_without_reading_target(tmp_path: Path) -> None:
    """토큰 cache 경로의 symlink는 검사·읽기 모두 거부한다."""
    target = tmp_path / "target"
    target.write_text("test-cli-token\n", encoding="utf-8")
    target.chmod(0o600)
    token_path = tmp_path / "token"
    token_path.symlink_to(target)

    status = inspect_huggingface_cli_token_cache(token_path)

    assert status.exists is True
    assert status.usable is False
    assert status.reason == "symlink"
    assert read_huggingface_cli_token(token_path) is None


def test_owner_unreadable_cache_is_rejected(tmp_path: Path) -> None:
    """소유자 읽기 비트가 없는 cache는 준비됨으로 오인하지 않는다."""
    token_path = tmp_path / "token"
    token_path.write_text("test-cli-token\n", encoding="utf-8")
    token_path.chmod(0o200)

    status = inspect_huggingface_cli_token_cache(token_path)

    assert status.usable is False
    assert status.reason == "owner_not_readable"
    assert read_huggingface_cli_token(token_path) is None


def test_oversized_cache_is_rejected_by_inspection_and_runtime(tmp_path: Path) -> None:
    """런타임 제한보다 큰 cache를 readiness가 준비됨으로 오인하지 않는다."""
    token_path = tmp_path / "token"
    token_path.write_bytes(b"x" * 4097)
    token_path.chmod(0o600)

    status = inspect_huggingface_cli_token_cache(token_path)

    assert status.usable is False
    assert status.reason == "too_large"
    assert read_huggingface_cli_token(token_path) is None


def test_blank_cache_is_rejected_by_inspection_and_runtime(tmp_path: Path) -> None:
    """공백뿐인 cache를 readiness가 준비됨으로 오인하지 않는다."""
    token_path = tmp_path / "token"
    token_path.write_text(" \n\t", encoding="utf-8")
    token_path.chmod(0o600)

    status = inspect_huggingface_cli_token_cache(token_path)

    assert status.usable is False
    assert status.reason == "blank"
    assert read_huggingface_cli_token(token_path) is None


def test_invalid_utf8_cache_is_rejected_by_inspection_and_runtime(tmp_path: Path) -> None:
    """UTF-8이 아닌 cache를 readiness가 준비됨으로 오인하지 않는다."""
    token_path = tmp_path / "token"
    token_path.write_bytes(b"\xff\xfe")
    token_path.chmod(0o600)

    status = inspect_huggingface_cli_token_cache(token_path)

    assert status.usable is False
    assert status.reason == "invalid_encoding"
    assert read_huggingface_cli_token(token_path) is None
