"""HuggingFace CLI 자격 증명 캐시를 안전하게 검사한다.

토큰 값은 이 모듈의 상태 검사 결과나 로그에 포함하지 않는다. LaunchAgent처럼
셸 초기화 파일을 읽지 않는 실행 경로에서는 HuggingFace CLI의 사용자 캐시만
지속 자격 증명으로 사용한다.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

_MAX_TOKEN_BYTES = 4096


@dataclass(frozen=True)
class HuggingFaceCliCacheStatus:
    """HuggingFace CLI 토큰 캐시의 비밀 없는 검사 결과."""

    exists: bool
    usable: bool
    private: bool
    reason: str | None = None


def get_huggingface_cli_token_path(home: Path | None = None) -> Path:
    """기본 HuggingFace CLI 토큰 캐시 경로를 반환한다.

    Args:
        home: 테스트용 홈 디렉토리. None이면 현재 사용자 홈을 사용한다.

    Returns:
        HuggingFace CLI가 기본적으로 사용하는 token 파일 경로.
    """
    base_home = home if home is not None else Path.home()
    return base_home / ".cache" / "huggingface" / "token"


def _inspect_token_contents(path: Path, expected_metadata: os.stat_result) -> str | None:
    """토큰 값을 노출하지 않고 런타임과 같은 내용 제약을 검사한다."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        return "no_follow_unsupported"
    flags = os.O_RDONLY | int(no_follow) | getattr(os, "O_CLOEXEC", 0)
    try:
        file_fd = os.open(path, flags)
    except OSError:
        return "unreadable"
    try:
        metadata = os.fstat(file_fd)
        mode = stat.S_IMODE(metadata.st_mode)
        if not stat.S_ISREG(metadata.st_mode):
            return "not_regular_file"
        if (metadata.st_dev, metadata.st_ino) != (
            expected_metadata.st_dev,
            expected_metadata.st_ino,
        ):
            return "changed_during_inspection"
        if metadata.st_size <= 0:
            return "empty"
        if metadata.st_size > _MAX_TOKEN_BYTES:
            return "too_large"
        if (mode & 0o077) != 0:
            return "permissions"
        if (mode & stat.S_IRUSR) == 0:
            return "owner_not_readable"
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            return "not_owned_by_current_user"

        raw = os.read(file_fd, _MAX_TOKEN_BYTES + 1)
        if len(raw) > _MAX_TOKEN_BYTES:
            return "too_large"
        try:
            token = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            return "invalid_encoding"
        if not token:
            return "blank"
        return None
    finally:
        os.close(file_fd)


def inspect_huggingface_cli_token_cache(
    token_path: Path | None = None,
) -> HuggingFaceCliCacheStatus:
    """CLI 캐시가 런타임에서 읽힐 수 있는지 값을 노출하지 않고 검사한다."""
    path = token_path or get_huggingface_cli_token_path()
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return HuggingFaceCliCacheStatus(
            exists=False,
            usable=False,
            private=False,
            reason="missing",
        )
    except OSError:
        return HuggingFaceCliCacheStatus(
            exists=False,
            usable=False,
            private=False,
            reason="unreadable",
        )

    if stat.S_ISLNK(metadata.st_mode):
        return HuggingFaceCliCacheStatus(
            exists=True,
            usable=False,
            private=False,
            reason="symlink",
        )
    if not stat.S_ISREG(metadata.st_mode):
        return HuggingFaceCliCacheStatus(
            exists=True,
            usable=False,
            private=False,
            reason="not_regular_file",
        )
    if metadata.st_size <= 0:
        return HuggingFaceCliCacheStatus(
            exists=True,
            usable=False,
            private=False,
            reason="empty",
        )

    mode = stat.S_IMODE(metadata.st_mode)
    is_private = (mode & 0o077) == 0
    if not is_private:
        return HuggingFaceCliCacheStatus(
            exists=True,
            usable=False,
            private=False,
            reason="permissions",
        )
    if (mode & stat.S_IRUSR) == 0:
        return HuggingFaceCliCacheStatus(
            exists=True,
            usable=False,
            private=True,
            reason="owner_not_readable",
        )
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        return HuggingFaceCliCacheStatus(
            exists=True,
            usable=False,
            private=True,
            reason="not_owned_by_current_user",
        )

    content_issue = _inspect_token_contents(path, metadata)
    if content_issue is not None:
        return HuggingFaceCliCacheStatus(
            exists=True,
            usable=False,
            private=True,
            reason=content_issue,
        )

    return HuggingFaceCliCacheStatus(
        exists=True,
        usable=True,
        private=True,
    )


def read_huggingface_cli_token(token_path: Path | None = None) -> str | None:
    """안전한 HuggingFace CLI 캐시에서만 토큰을 읽는다.

    호출자는 반환값을 로그·응답·설정 파일에 기록해서는 안 된다.
    """
    path = token_path or get_huggingface_cli_token_path()
    if not inspect_huggingface_cli_token_cache(path).usable:
        return None
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        return None
    flags = os.O_RDONLY | int(no_follow) | getattr(os, "O_CLOEXEC", 0)
    try:
        file_fd = os.open(path, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(file_fd)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_TOKEN_BYTES
            or (mode & 0o077) != 0
            or (mode & stat.S_IRUSR) == 0
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            return None
        raw = os.read(file_fd, _MAX_TOKEN_BYTES + 1)
        if len(raw) > _MAX_TOKEN_BYTES:
            return None
        try:
            token = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
        return token or None
    finally:
        os.close(file_fd)


def _main(argv: list[str]) -> int:
    """LaunchAgent 스크립트가 비밀 노출 없이 동일 검증을 재사용하게 한다."""
    if len(argv) != 2 or argv[0] != "--check":
        return 2
    status = inspect_huggingface_cli_token_cache(Path(argv[1]))
    return 0 if status.usable else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
