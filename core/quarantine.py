"""
Quarantine 디렉토리 관리

목적: 품질 불량·사용자 삭제 오디오 파일을 입력 감시 폴더 밖의
     격리실로 이동하여 watcher 재감지를 차단한다.

근거: 2026-04-21 DELETE /api/meetings/{id}가 DB만 삭제하여 오디오 파일이
     잔존 → watcher가 재등록 → 동일 크래시 반복. 이 헬퍼가 이동까지 담당.
"""

from __future__ import annotations

import logging
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class QuarantineError(Exception):
    """Quarantine 이동 실패."""


@dataclass(frozen=True)
class _FileIdentity:
    """경로 교체 공격과 처리 중 변경을 탐지하기 위한 파일 지문."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    def as_tuple(self) -> tuple[int, int, int, int, int]:
        """watcher와 공유할 수 있는 불변 tuple을 반환한다."""
        return (self.device, self.inode, self.size, self.mtime_ns, self.ctime_ns)


def _identity(file_stat: os.stat_result) -> _FileIdentity:
    """stat 결과를 비교 가능한 파일 지문으로 변환한다."""
    return _FileIdentity(
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
        ctime_ns=file_stat.st_ctime_ns,
    )


def _directory_open_flags() -> int:
    """directory walk에 사용할 no-follow flags를 반환한다."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise QuarantineError("O_NOFOLLOW를 지원하지 않아 안전한 격리가 불가합니다")
    return int(
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | no_follow
    )


def _lexical_absolute(path: Path) -> Path:
    """symlink를 해석하지 않고 parent traversal을 거부한 절대 경로를 만든다."""
    raw = Path(path).expanduser()
    if "\x00" in os.fspath(raw):
        raise QuarantineError(f"NUL이 포함된 경로는 허용하지 않습니다: {path}")
    components = raw.parts[1:] if raw.is_absolute() else raw.parts
    if any(part in {"", ".", ".."} for part in components):
        raise QuarantineError(f"안전하지 않은 경로 component입니다: {path}")
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    return Path(os.path.abspath(os.fspath(raw)))


def _open_directory_tree_no_follow(path: Path, *, create: bool) -> int:
    """루트부터 모든 component를 openat+O_NOFOLLOW로 열어 fd를 반환한다."""
    absolute = _lexical_absolute(path)
    flags = _directory_open_flags()
    current_fd = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, flags, dir_fd=current_fd)
            opened = os.fstat(next_fd)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_fd)
                raise QuarantineError(f"디렉토리가 아닌 경로 component입니다: {absolute}")
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    """두 stat이 동일 inode를 가리키는지 확인한다."""
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _same_validated_content(left: os.stat_result, right: os.stat_result) -> bool:
    """hardlink의 ctime 변경을 제외하고 검증한 inode/내용 identity를 비교한다."""
    return (
        _same_inode(left, right)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _matches_expected_identity(
    file_stat: os.stat_result,
    expected_identity: tuple[int, int, int, int] | tuple[int, int, int, int, int],
) -> bool:
    """live/recovery 지문 길이에 맞춰 파일 identity가 동일한지 반환한다.

    live validation은 ctime까지 포함한 5-tuple을 사용한다. durable recovery는
    hardlink 생성 자체가 ctime을 바꾸므로 dev/ino/size/mtime의 4-tuple만
    저장하고 비교한다.
    """
    actual = (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )
    if len(expected_identity) == 4:
        return actual == expected_identity
    return (*actual, file_stat.st_ctime_ns) == expected_identity


def _verify_lexical_directory_identity(
    path: Path,
    expected: os.stat_result,
) -> None:
    """lexical directory가 아직 열어둔 inode를 가리키는지 재확인한다."""
    check_fd: int | None = None
    try:
        check_fd = _open_directory_tree_no_follow(path, create=False)
        if not _same_inode(os.fstat(check_fd), expected):
            raise QuarantineError(f"디렉토리가 처리 중 교체되었습니다: {path}")
    except QuarantineError:
        raise
    except OSError as exc:
        raise QuarantineError(f"디렉토리 identity 재검증 실패: {path}: {exc}") from exc
    finally:
        if check_fd is not None:
            os.close(check_fd)


def _unlink_if_inode(directory_fd: int, name: str, expected: os.stat_result) -> bool:
    """현재 entry가 우리가 만든 inode일 때만 정리한다."""
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if not _same_inode(current, expected):
        return False
    os.unlink(name, dir_fd=directory_fd)
    return True


def move_to_quarantine(
    src_path: Path,
    quarantine_dir: Path,
    *,
    reason: str,
    expected_identity: (tuple[int, int, int, int] | tuple[int, int, int, int, int] | None) = None,
    exact_destination: Path | None = None,
) -> Path:
    """오디오 파일을 동일 파일시스템의 격리 디렉토리로 안전하게 이동한다.

    목적지 placeholder를 만든 뒤 ``os.replace``하지 않고, 원본 inode의
    hardlink를 존재하지 않는 이름으로 생성한 뒤 identity를 확인하고
    원본 entry를 제거한다. 모든 directory component는 no-follow로 연다.

    Args:
        src_path: 이동할 원본 파일 경로
        quarantine_dir: 격리 디렉토리 (없으면 생성)
        reason: 이동 사유 (로깅용)
        expected_identity: live validation의 5-tuple 또는 durable recovery의
            ``dev, ino, size, mtime`` 4-tuple
        exact_destination: 지정하면 다른 suffix로 대체하지 않고 이 direct-child
            경로 하나에만 exclusive hardlink를 생성한다.

    Returns:
        이동된 파일의 검증된 lexical 경로

    Raises:
        QuarantineError: 원본 파일이 없거나 안전한 이동을 보장할 수 없을 때
    """
    source = _lexical_absolute(Path(src_path))
    quarantine = _lexical_absolute(Path(quarantine_dir))
    if not source.name or source.name in {".", ".."}:
        raise QuarantineError(f"유효하지 않은 원본 경로입니다: {source}")
    exact_name: str | None = None
    if exact_destination is not None:
        destination = _lexical_absolute(Path(exact_destination))
        if destination.parent != quarantine or destination.name in {"", ".", ".."}:
            raise QuarantineError(
                f"정확한 Quarantine 목적지는 root의 direct child여야 합니다: {destination}"
            )
        exact_name = destination.name

    source_dir_fd: int | None = None
    quarantine_dir_fd: int | None = None
    source_file_fd: int | None = None
    destination_name: str | None = None
    linked_stat: os.stat_result | None = None
    source_removed = False
    succeeded = False

    def _rollback() -> None:
        nonlocal source_removed
        if (
            destination_name is None
            or linked_stat is None
            or source_dir_fd is None
            or quarantine_dir_fd is None
        ):
            return

        if source_removed:
            try:
                os.stat(source.name, dir_fd=source_dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.link(
                        destination_name,
                        source.name,
                        src_dir_fd=quarantine_dir_fd,
                        dst_dir_fd=source_dir_fd,
                        follow_symlinks=False,
                    )
                    source_removed = False
                except OSError as exc:
                    logger.error(f"격리 rollback 원본 복원 실패: {source} ({exc})")
                    return
            except OSError:
                return
            else:
                # 같은 이름이 다른 inode로 재생성됐으면 절대 제거하지 않는다.
                return

        try:
            _unlink_if_inode(quarantine_dir_fd, destination_name, linked_stat)
        except OSError as exc:
            logger.error(f"격리 rollback 목적지 정리 실패: {destination_name} ({exc})")

    try:
        quarantine_dir_fd = _open_directory_tree_no_follow(quarantine, create=True)
        quarantine_stat = os.fstat(quarantine_dir_fd)
        source_dir_fd = _open_directory_tree_no_follow(source.parent, create=False)
        source_dir_stat = os.fstat(source_dir_fd)

        no_follow = getattr(os, "O_NOFOLLOW", 0)
        source_file_fd = os.open(
            source.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow,
            dir_fd=source_dir_fd,
        )
        source_stat = os.fstat(source_file_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise QuarantineError(f"원본이 일반 파일이 아닙니다: {source}")
        if expected_identity is not None and not _matches_expected_identity(
            source_stat,
            expected_identity,
        ):
            raise QuarantineError(f"격리 직전 원본 파일이 변경되었습니다: {source}")
        if source_stat.st_dev != quarantine_stat.st_dev:
            raise QuarantineError(
                f"다른 파일시스템으로는 안전하게 격리할 수 없습니다: {source} → {quarantine}"
            )

        if exact_name is not None:
            candidate_names = [exact_name]
        else:
            candidate_names = [source.name]
            candidate_names.extend(
                f"{source.stem}_{uuid.uuid4().hex}{source.suffix}" for _ in range(100)
            )
        for candidate_name in candidate_names:
            try:
                os.link(
                    source.name,
                    candidate_name,
                    src_dir_fd=source_dir_fd,
                    dst_dir_fd=quarantine_dir_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                continue
            destination_name = candidate_name
            # link가 생성된 뒤 stat 자체가 실패해도 우리가 만든 inode만
            # rollback할 수 있도록 원본 identity를 먼저 보관한다.
            linked_stat = source_stat
            break
        if destination_name is None:
            if exact_name is not None:
                raise QuarantineError(f"예약된 Quarantine 목적지가 이미 존재합니다: {exact_name}")
            raise QuarantineError(f"고유한 Quarantine 목적지를 생성하지 못했습니다: {source.name}")

        linked_stat = os.stat(
            destination_name,
            dir_fd=quarantine_dir_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(linked_stat.st_mode) or not _same_validated_content(
            source_stat,
            linked_stat,
        ):
            raise QuarantineError(f"격리 hardlink identity 검증 실패: {destination_name}")

        current_source = os.stat(source.name, dir_fd=source_dir_fd, follow_symlinks=False)
        if not _same_validated_content(source_stat, current_source):
            raise QuarantineError(f"격리 중 원본 파일이 변경되었습니다: {source}")

        # 이동 직전에 source/quarantine lexical root가 열어둔 inode와 같은지
        # 모두 재확인한다. 열린 fd만 신뢰하면 root rename 뒤 외부 경계에 있는
        # inode를 이동한 뒤 잘못된 lexical 경로를 반환할 수 있다.
        _verify_lexical_directory_identity(source.parent, source_dir_stat)
        _verify_lexical_directory_identity(quarantine, quarantine_stat)

        # DB claim을 finalize하기 전에 목적지 directory entry가 crash 뒤에도
        # 남도록 hardlink metadata를 먼저 durable하게 만든다.
        os.fsync(quarantine_dir_fd)
        os.unlink(source.name, dir_fd=source_dir_fd)
        source_removed = True
        # 원본 unlink도 같은 transaction 경계에서 durable하게 만든다. 여기서
        # 실패하면 아래 rollback이 가능한 한 원본을 복구하고 오류를 반환한다.
        os.fsync(source_dir_fd)

        final_source_stat = os.fstat(source_file_fd)
        if not _same_validated_content(source_stat, final_source_stat):
            raise QuarantineError(f"격리 중 원본 내용이 변경되었습니다: {source}")

        # 반환할 lexical path가 여전히 같은 directory/file inode인지 검증한다.
        _verify_lexical_directory_identity(source.parent, source_dir_stat)
        _verify_lexical_directory_identity(quarantine, quarantine_stat)
        check_q_fd = _open_directory_tree_no_follow(quarantine, create=False)
        try:
            lexical_dest_stat = os.stat(
                destination_name,
                dir_fd=check_q_fd,
                follow_symlinks=False,
            )
            if not _same_inode(lexical_dest_stat, linked_stat):
                raise QuarantineError(f"격리 반환 경로가 교체되었습니다: {destination_name}")
        finally:
            os.close(check_q_fd)

        succeeded = True
    except QuarantineError:
        _rollback()
        raise
    except OSError as exc:
        _rollback()
        raise QuarantineError(f"이동 실패: {source} → {quarantine}: {exc}") from exc
    finally:
        if not succeeded:
            _rollback()
        if source_file_fd is not None:
            try:
                os.close(source_file_fd)
            except OSError:
                pass
        if source_dir_fd is not None:
            try:
                os.close(source_dir_fd)
            except OSError:
                pass
        if quarantine_dir_fd is not None:
            try:
                os.close(quarantine_dir_fd)
            except OSError:
                pass

    if destination_name is None:
        raise QuarantineError(f"Quarantine 목적지가 생성되지 않았습니다: {source}")
    destination = quarantine / destination_name
    logger.info(f"Quarantine 이동: {source.name} → {destination} (사유: {reason})")
    return destination


def move_to_quarantine_exact(
    src_path: Path,
    destination_path: Path,
    *,
    reason: str,
    expected_identity: tuple[int, int, int, int] | tuple[int, int, int, int, int],
) -> Path:
    """예약된 direct-child 목적지로만 원본을 안전하게 이동한다.

    목적지가 이미 있으면 이름을 바꾸거나 덮어쓰지 않고 실패한다. durable
    audio-rejection journal이 재기동 후 같은 경로를 판별할 수 있게 하는 API다.
    """
    destination = _lexical_absolute(Path(destination_path))
    return move_to_quarantine(
        src_path,
        destination.parent,
        reason=reason,
        expected_identity=expected_identity,
        exact_destination=destination,
    )
