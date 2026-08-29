"""
원자적 파일 I/O 유틸리티 모듈 (Atomic File I/O Utilities)

목적: 파일 쓰기 도중 프로세스가 죽거나 디스크가 가득 차도 기존 파일이
손상되지 않도록 보장하는 공용 헬퍼를 제공한다.

전략:
    1. 같은 디렉토리에 임시 파일(`{name}.tmp.{pid}.{rand}`) 생성
    2. 내용 쓰기 → flush → fsync (디스크에 강제 동기)
    3. `os.replace()` 로 원자적 교체 (POSIX 보장)
    4. (선택) 기존 파일을 `.bak` 로 백업

지연 LLM처럼 기존 entry를 절대 교체하면 안 되는 경로는 별도
`publish_text_no_replace()`를 사용한다. 최종 이름 자체를 `O_EXCL|O_NOFOLLOW`로 열어
source-name 재해석 없이 기록하고, 경쟁 entry가 있으면 보존한 채 실패한다. 실패한
부분 파일은 자동 신뢰하거나 제거하지 않는다. 반복 갱신되는 pipeline state는 부모
디렉터리를 no-follow FD로 고정하고 기존 상태와 새 상태를 원자 교환한다. 교환 뒤
이전 상태는 숨김 파일로 보존하므로 source-name 경쟁이 생겨도 기존 상태를 제거하지 않는다.

이 모듈을 만들기 전에는 같은 패턴이 `api/routes.py::_atomic_write_text` 와
`core/user_settings.py::_atomic_write_json` 두 곳에 별도 구현되어 있었고,
`api/routes.py::update_settings` / `activate_stt_model` 의 `config.yaml` 쓰기는
원시 `open("w")` 를 사용해 손상 위험이 있었다. 이 모듈은 그 세 가지 경로를
모두 통합한다.

의존성: 표준 라이브러리만 사용 (ctypes, os, platform, shutil, tempfile, json, pathlib).
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import logging
import os
import platform
import shutil
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _normalize_absolute_target(path: Path) -> Path:
    """단일 파일 대상 경로를 절대 lexical 경로로 정규화한다."""
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = target.absolute()
    if (
        "\x00" in str(target)
        or ".." in target.parts
        or target.name in {"", ".", ".."}
        or Path(target.name).name != target.name
    ):
        raise OSError(f"안전하지 않은 파일 경로입니다: {path}")
    return target


def _open_directory_no_follow(path: Path) -> int:
    """절대 경로의 모든 디렉터리 component를 no-follow로 열어 반환한다."""
    raw_path = path.expanduser()
    if not raw_path.is_absolute():
        raw_path = raw_path.absolute()
    if "\x00" in str(raw_path) or ".." in raw_path.parts:
        raise OSError(f"안전하지 않은 디렉터리 경로입니다: {path}")

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("O_NOFOLLOW를 지원하지 않아 안전한 파일 게시가 불가합니다")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= no_follow

    current_fd = os.open(raw_path.anchor, flags)
    try:
        for component in raw_path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            opened = os.fstat(next_fd)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_fd)
                raise OSError(f"디렉터리가 아닌 경로 component입니다: {path}")
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _ensure_directory_no_follow_fd(path: Path, *, mode: int = 0o700) -> int:
    """symlink를 따르지 않고 절대 디렉터리를 구성해 마지막 FD를 반환한다.

    각 component는 이미 고정한 부모 FD를 기준으로 `mkdirat`/`openat` 하므로 검사와
    생성 사이에 lexical 부모가 외부 symlink로 바뀌어도 외부 하위 디렉터리를 만들지
    않는다. 반환 FD의 소유권은 호출자에게 있다.
    """
    raw_path = path.expanduser()
    if not raw_path.is_absolute():
        raw_path = raw_path.absolute()
    if "\x00" in str(raw_path) or ".." in raw_path.parts:
        raise OSError(f"안전하지 않은 디렉터리 경로입니다: {path}")

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("O_NOFOLLOW를 지원하지 않아 안전한 디렉터리 생성이 불가합니다")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= no_follow

    current_fd = os.open(raw_path.anchor, flags)
    try:
        for component in raw_path.parts[1:]:
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=mode, dir_fd=current_fd)
                except FileExistsError:
                    # 다른 실행자가 먼저 만든 경우에도 아래 no-follow open으로
                    # 실제 디렉터리인지 다시 확인한다.
                    pass
                next_fd = os.open(component, flags, dir_fd=current_fd)
            opened = os.fstat(next_fd)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_fd)
                raise OSError(f"디렉터리가 아닌 경로 component입니다: {path}")
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def ensure_directory_no_follow(path: Path, *, mode: int = 0o700) -> None:
    """symlink를 따르지 않고 절대 디렉터리 경로를 구성한다."""
    directory_fd = _ensure_directory_no_follow_fd(path, mode=mode)
    os.close(directory_fd)


def _write_all(file_fd: int, content: str, *, target: Path) -> os.stat_result:
    """열린 일반 파일 FD에 UTF-8 전체 내용을 쓰고 동기화한다."""
    payload = memoryview(content.encode("utf-8"))
    while payload:
        written = os.write(file_fd, payload)
        if written <= 0:
            raise OSError(f"파일 쓰기가 중단되었습니다: {target}")
        payload = payload[written:]
    os.fsync(file_fd)
    written_stat = os.fstat(file_fd)
    if not stat.S_ISREG(written_stat.st_mode):
        raise OSError(f"쓰기 대상이 일반 파일이 아닙니다: {target}")
    return written_stat


def _rename_exchange(parent_fd: int, source_name: str, target_name: str) -> None:
    """두 directory entry를 플랫폼 원자 교환 syscall로 맞바꾼다."""
    libc = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(source_name)
    target = os.fsencode(target_name)
    exchange_flag = 0x00000002  # Darwin RENAME_SWAP / Linux RENAME_EXCHANGE
    system = platform.system()

    if system == "Darwin":
        rename_exchange = getattr(libc, "renameatx_np", None)
        if rename_exchange is None:
            raise OSError(errno.ENOTSUP, "renameatx_np를 지원하지 않습니다")
    elif system == "Linux":
        rename_exchange = getattr(libc, "renameat2", None)
        if rename_exchange is None:
            raise OSError(errno.ENOTSUP, "renameat2를 지원하지 않습니다")
    else:
        raise OSError(errno.ENOTSUP, "원자 entry 교환을 지원하지 않는 플랫폼입니다")

    rename_exchange.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename_exchange.restype = ctypes.c_int
    if rename_exchange(parent_fd, source, parent_fd, target, exchange_flag) != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            f"{source_name}<->{target_name}",
        )


def read_text_no_follow(path: Path) -> str:
    """symlink를 따르지 않는 고정 FD에서 UTF-8 텍스트를 읽는다."""
    target = _normalize_absolute_target(path)
    parent_fd: int | None = None
    reopened_parent_fd: int | None = None
    target_fd: int | None = None
    try:
        parent_fd = _open_directory_no_follow(target.parent)
        parent_stat = os.fstat(parent_fd)
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError("O_NOFOLLOW를 지원하지 않아 안전한 파일 읽기가 불가합니다")
        target_fd = os.open(
            target.name,
            os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        target_stat = os.fstat(target_fd)
        if not stat.S_ISREG(target_stat.st_mode):
            raise OSError(f"읽기 대상이 일반 파일이 아닙니다: {target}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(target_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)

        final_stat = os.stat(
            target.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (final_stat.st_dev, final_stat.st_ino) != (
            target_stat.st_dev,
            target_stat.st_ino,
        ):
            raise OSError(f"읽기 대상 identity가 검사 중 변경되었습니다: {target}")

        reopened_parent_fd = _open_directory_no_follow(target.parent)
        reopened_parent_stat = os.fstat(reopened_parent_fd)
        if (reopened_parent_stat.st_dev, reopened_parent_stat.st_ino) != (
            parent_stat.st_dev,
            parent_stat.st_ino,
        ):
            raise OSError(f"읽기 상위 디렉터리가 검사 중 변경되었습니다: {target.parent}")
        return b"".join(chunks).decode("utf-8")
    finally:
        if target_fd is not None:
            os.close(target_fd)
        if reopened_parent_fd is not None:
            os.close(reopened_parent_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def publish_text_no_replace(path: Path, content: str) -> None:
    """완성된 텍스트를 기존 entry를 교체하지 않고 새 파일로 게시한다.

    최종 이름 자체를 ``O_EXCL|O_NOFOLLOW``로 열어 source-name을 다시 해석하는
    hard-link/rename 단계를 없앤다. 최종 이름이 먼저 생기면 기존 entry를 보존한 채
    ``FileExistsError``로 실패한다. 쓰기 도중 실패한 파일은 보존하며 이후 자동
    복구의 provenance로 신뢰하지 않는다.

    Args:
        path: 아직 존재하지 않아야 하는 최종 파일 경로.
        content: UTF-8로 게시할 전체 텍스트.

    Raises:
        FileExistsError: 최종 entry가 이미 있거나 경쟁 중 먼저 생성된 경우.
        OSError: 경로 검증, 쓰기, fsync 또는 게시 검증 실패.
    """
    target = _normalize_absolute_target(path)

    parent_fd: int | None = None
    reopened_parent_fd: int | None = None
    target_fd: int | None = None
    try:
        parent_fd = _ensure_directory_no_follow_fd(target.parent)
        parent_identity = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_identity.st_mode):
            raise OSError(f"게시 상위 경로가 디렉터리가 아닙니다: {target.parent}")

        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError("O_NOFOLLOW를 지원하지 않아 안전한 파일 게시가 불가합니다")
        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow
        create_flags |= getattr(os, "O_CLOEXEC", 0)
        target_fd = os.open(target.name, create_flags, 0o600, dir_fd=parent_fd)
        published_stat = _write_all(target_fd, content, target=target)
        final_stat = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(final_stat.st_mode) or (final_stat.st_dev, final_stat.st_ino) != (
            published_stat.st_dev,
            published_stat.st_ino,
        ):
            raise OSError(f"게시된 최종 entry identity가 변경되었습니다: {target}")

        reopened_parent_fd = _open_directory_no_follow(target.parent)
        reopened_parent = os.fstat(reopened_parent_fd)
        if (reopened_parent.st_dev, reopened_parent.st_ino) != (
            parent_identity.st_dev,
            parent_identity.st_ino,
        ):
            raise OSError(f"게시 중 상위 디렉터리가 변경되었습니다: {target.parent}")
        os.fsync(parent_fd)
    finally:
        if target_fd is not None:
            os.close(target_fd)
        if reopened_parent_fd is not None:
            os.close(reopened_parent_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def atomic_write_text_pinned(
    path: Path,
    content: str,
    *,
    backup: bool = False,
) -> None:
    """고정한 부모 디렉터리 FD 안에서 텍스트를 원자 교체한다.

    lexical 부모가 검사 뒤 symlink로 바뀌어도 외부 경로를 따라가지 않는다. 최초
    상태는 최종 이름 자체를 ``O_EXCL``로 만들고, 기존 상태 갱신은 플랫폼의 원자
    exchange syscall을 사용한다. 교환 뒤 hidden entry에 남은 이전 상태는 자동
    삭제하지 않는다. 사후 identity 불일치는 실패로 보고하되, 그 사이
    다른 정상 writer가 게시한 최신 상태를 과거 상태로 rollback하지 않는다.
    """
    target = _normalize_absolute_target(path)
    if backup:
        try:
            previous_content = read_text_no_follow(target)
        except FileNotFoundError:
            previous_content = None
        if previous_content is not None:
            # 백업도 동일한 no-follow/exchange writer를 사용한다. 기존
            # .bak이 symlink이거나 비정상 entry이면 원본을 바꾸기 전에 실패한다.
            atomic_write_text_pinned(
                target.with_name(f"{target.name}.bak"),
                previous_content,
            )
    parent_fd: int | None = None
    reopened_parent_fd: int | None = None
    generation_fd: int | None = None
    previous_fd: int | None = None
    generation_name = f".{target.name}.{uuid.uuid4().hex}.state-previous"
    try:
        # 생성한 최종 parent FD를 닫았다가 같은 lexical 경로로 다시 열지 않는다.
        # 따라서 mkdir 검증과 state mutation 사이의 regular-directory swap도 외부
        # 디렉터리로 쓰기를 전환하지 못한다.
        parent_fd = _ensure_directory_no_follow_fd(target.parent)
        parent_stat = os.fstat(parent_fd)
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError("O_NOFOLLOW를 지원하지 않아 안전한 상태 저장이 불가합니다")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow
        flags |= getattr(os, "O_CLOEXEC", 0)
        read_flags = (
            os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            previous_fd = os.open(target.name, read_flags, dir_fd=parent_fd)
        except FileNotFoundError:
            # 최초 생성에는 source 경로가 없으므로 최종 이름을 직접 열어 기록한다.
            generation_fd = os.open(target.name, flags, 0o600, dir_fd=parent_fd)
            generation_stat = _write_all(generation_fd, content, target=target)
        else:
            previous_stat = os.fstat(previous_fd)
            if not stat.S_ISREG(previous_stat.st_mode) or previous_stat.st_nlink != 1:
                raise OSError(f"기존 상태가 단일 일반 파일이 아닙니다: {target}")
            generation_fd = os.open(generation_name, flags, 0o600, dir_fd=parent_fd)
            generation_stat = _write_all(generation_fd, content, target=target)
            if generation_stat.st_nlink != 1:
                raise OSError(f"새 상태 generation에 예상하지 않은 hard-link가 있습니다: {target}")

            before_generation = os.stat(
                generation_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            before_target = os.stat(
                target.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (before_generation.st_dev, before_generation.st_ino) != (
                generation_stat.st_dev,
                generation_stat.st_ino,
            ) or (before_target.st_dev, before_target.st_ino) != (
                previous_stat.st_dev,
                previous_stat.st_ino,
            ):
                raise OSError(f"상태 entry identity가 교환 전에 변경되었습니다: {target}")

            _rename_exchange(parent_fd, generation_name, target.name)

        final_stat = os.stat(
            target.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        target_matches_generation = (
            stat.S_ISREG(final_stat.st_mode)
            and final_stat.st_nlink == 1
            and (final_stat.st_dev, final_stat.st_ino)
            == (generation_stat.st_dev, generation_stat.st_ino)
        )
        if not target_matches_generation:
            # 이 writer의 exchange 뒤 다른 정상 writer가 완료될 수 있다. 이것을
            # 이전 상태로 rollback하면 더 최신의 성공한 저장을 잃으므로,
            # 관측된 최신 entry를 그대로 두고 현재 호출만 실패시킨다.
            raise OSError(f"저장된 상태 entry identity가 변경되었습니다: {target}")
        reopened_parent_fd = _open_directory_no_follow(target.parent)
        reopened_parent_stat = os.fstat(reopened_parent_fd)
        if (reopened_parent_stat.st_dev, reopened_parent_stat.st_ino) != (
            parent_stat.st_dev,
            parent_stat.st_ino,
        ):
            raise OSError(f"상태 저장 중 상위 디렉터리가 변경되었습니다: {target.parent}")
        os.fsync(parent_fd)
    finally:
        if generation_fd is not None:
            os.close(generation_fd)
        if previous_fd is not None:
            os.close(previous_fd)
        if reopened_parent_fd is not None:
            os.close(reopened_parent_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def atomic_write_json_pinned(
    path: Path,
    data: Any,
    *,
    backup: bool = False,
    indent: int | None = 2,
) -> None:
    """JSON을 no-follow 부모 FD 안에서 원자 교체한다.

    수동 산출물 편집처럼 기존 entry를 바꾸는 경로에서 사용하며,
    ``backup=True``면 직전 내용을 ``{name}.bak``에 같은 계약으로 보존한다.
    """
    if indent is None:
        content = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    else:
        content = json.dumps(data, ensure_ascii=False, indent=indent)
    atomic_write_text_pinned(path, content, backup=backup)


def atomic_write_text(
    path: Path,
    content: str,
    *,
    backup: bool = True,
) -> None:
    """텍스트 파일을 원자적으로 덮어쓴다.

    부모 디렉토리가 없으면 생성한다. `backup=True` 이면 기존 파일을 같은
    디렉토리의 `{name}.bak` 로 복사한 뒤 새 내용을 쓴다. 임시 파일은 같은
    디렉토리에 만들어 같은 파일시스템에서 `os.replace()` 가 원자적임을 보장한다.

    Args:
        path: 최종 대상 파일 경로 (절대 경로 권장).
        content: 새로 쓸 텍스트.
        backup: True 이면 `.bak` 백업 생성.

    Raises:
        OSError: 디스크 쓰기 실패 (권한, 디스크 풀 등).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if backup and path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        try:
            shutil.copy2(path, backup_path)
        except OSError as exc:
            logger.warning(f"백업 생성 실패 (진행 계속): {exc}")

    # delete=False 로 NamedTemporaryFile 을 만들고 즉시 tmp_name 캡처.
    # 이렇게 해야 write/flush/fsync 어디서 실패해도 finally 가 정리할 수 있다.
    tf = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    tmp_name: str | None = tf.name
    try:
        try:
            tf.write(content)
            tf.flush()
            os.fsync(tf.fileno())
        finally:
            tf.close()
        assert tmp_name is not None
        os.replace(tmp_name, path)
        tmp_name = None  # 성공 — finally 에서 unlink 안 함
    finally:
        if tmp_name is not None:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink(missing_ok=True)


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    backup: bool = True,
    indent: int = 2,
) -> None:
    """JSON 데이터를 원자적으로 덮어쓴다.

    `atomic_write_text` 의 thin wrapper. 직렬화 옵션은 한국어 보존을 위해
    `ensure_ascii=False` 고정.

    Args:
        path: 최종 대상 파일 경로.
        data: JSON 으로 직렬화 가능한 객체.
        backup: True 이면 `.bak` 백업 생성.
        indent: JSON pretty-print 들여쓰기.

    Raises:
        OSError: 디스크 쓰기 실패.
        TypeError: data 가 JSON 직렬화 불가능.
    """
    content = json.dumps(data, ensure_ascii=False, indent=indent)
    atomic_write_text(path, content, backup=backup)
