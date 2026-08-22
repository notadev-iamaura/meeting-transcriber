"""
A/B 테스트 파일 저장소 모듈.

목적: ~/.meeting-transcriber/ab_tests/{test_id}/ 디렉터리 기반의 단순한 파일
저장소. metadata.json 읽기/쓰기, test_id 화이트리스트 검증, 경로 이탈 방지,
테스트 목록/삭제를 제공한다.

주요 기능:
    - test_id 정규식 화이트리스트 검증 (path traversal 방지)
    - metadata.json 원자적 쓰기 (tmp → rename)
    - variant_a / variant_b 서브 디렉터리 준비
    - 디렉터리 스캔 기반 목록 조회 (최신순)
    - 디렉터리 삭제

의존성: config.AppConfig
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, cast

from config import AppConfig
from core.quarantine import QuarantineError, _open_directory_tree_no_follow

logger = logging.getLogger(__name__)

# test_id 화이트리스트 정규식 — ADR-2
# 형식: ab_{YYYYMMDD-HHMMSS}_{8자 16진수}
_TEST_ID_PATTERN = re.compile(r"^ab_\d{8}-\d{6}_[a-f0-9]{8}$")

# A/B 테스트 저장소 루트 디렉터리명
_AB_TESTS_DIRNAME = "ab_tests"

# metadata 파일명
METADATA_FILENAME = "metadata.json"
VARIANT_DIRS: tuple[str, str] = ("variant_a", "variant_b")
_VARIANT_FILES = frozenset(
    {
        "metrics.json",
        "correct.json",
        "summary.md",
        "transcribe.json",
        "merge.json",
        "stderr.log",
    }
)


def _root_path(config: AppConfig) -> Path:
    """resolve()로 symlink를 숨기지 않은 A/B 저장소 lexical 경로를 반환한다."""
    base = Path(config.paths.base_dir).expanduser()
    if not base.is_absolute():
        base = Path.cwd() / base
    return Path(os.path.abspath(os.fspath(base))) / _AB_TESTS_DIRNAME


def _open_root(config: AppConfig, *, create: bool) -> int:
    """A/B root를 모든 component no-follow 조건으로 열어 fd를 반환한다."""
    try:
        return _open_directory_tree_no_follow(_root_path(config), create=create)
    except (OSError, QuarantineError) as exc:
        raise ValueError("A/B 테스트 저장소 경로가 안전하지 않습니다.") from exc


def _directory_flags() -> int:
    """하위 디렉터리 openat에 사용할 no-follow 플래그를 반환한다."""
    return int(os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))


def _open_test_dir(config: AppConfig, test_id: str, *, create: bool) -> int:
    """검증된 test_id 디렉터리를 root fd 기준으로 열어 반환한다."""
    if not is_valid_test_id(test_id):
        raise ValueError(f"유효하지 않은 test_id: {test_id!r}")
    root_fd = _open_root(config, create=True)
    try:
        if create:
            try:
                os.mkdir(test_id, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
        try:
            test_fd = os.open(test_id, _directory_flags(), dir_fd=root_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError("A/B 테스트 디렉터리가 안전하지 않습니다.") from exc
        if not stat.S_ISDIR(os.fstat(test_fd).st_mode):
            os.close(test_fd)
            raise ValueError("A/B 테스트 경로가 디렉터리가 아닙니다.")
        return test_fd
    finally:
        os.close(root_fd)


def _open_variant_dir_path(variant_dir: Path) -> int:
    """variant lexical 경로를 symlink 없이 열어 fd를 반환한다."""
    try:
        return _open_directory_tree_no_follow(variant_dir, create=False)
    except (OSError, QuarantineError) as exc:
        raise ValueError("A/B variant 저장 경로가 안전하지 않습니다.") from exc


def _atomic_write_text_at(directory_fd: int, filename: str, content: str) -> None:
    """고정 디렉터리 fd 안에서 unique temp를 만들고 원자 교체한다."""
    temporary_name = f".{filename}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    temporary_fd: int | None = None
    try:
        temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as stream:
            temporary_fd = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary_name,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _read_text_at(directory_fd: int, filename: str) -> str:
    """고정 디렉터리 fd의 일반 파일을 no-follow로 읽는다."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        file_fd = os.open(filename, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError("A/B 산출물 파일이 안전하지 않습니다.") from exc
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ValueError("A/B 산출물 경로가 일반 파일이 아닙니다.")
        with os.fdopen(file_fd, encoding="utf-8") as stream:
            file_fd = -1
            return stream.read()
    finally:
        if file_fd >= 0:
            os.close(file_fd)


def is_valid_test_id(test_id: str) -> bool:
    """test_id 가 화이트리스트 정규식과 일치하는지 검증한다.

    Args:
        test_id: 검증 대상 문자열

    Returns:
        유효하면 True, 아니면 False
    """
    if not isinstance(test_id, str) or not test_id:
        return False
    return bool(_TEST_ID_PATTERN.match(test_id))


def get_ab_test_root(config: AppConfig) -> Path:
    """A/B 테스트 저장소 루트 디렉터리를 반환한다 (없으면 생성).

    Args:
        config: 앱 설정 인스턴스

    Returns:
        절대 경로 (Path)
    """
    root = _root_path(config)
    root_fd = _open_root(config, create=True)
    os.close(root_fd)
    return root


def resolve_test_dir(config: AppConfig, test_id: str) -> Path:
    """test_id 를 검증한 뒤 테스트 디렉터리 경로를 반환한다.

    - 정규식 화이트리스트 검증
    - Path.resolve() 후 루트 하위 여부 재검사 (심볼릭 링크/역참조 방어)

    Args:
        config: 앱 설정
        test_id: 테스트 식별자

    Returns:
        테스트 디렉터리 절대 경로 (존재 여부 보장 X)

    Raises:
        ValueError: test_id 가 유효하지 않거나 루트 이탈 시
    """
    if not is_valid_test_id(test_id):
        raise ValueError(f"유효하지 않은 test_id: {test_id!r}")

    root = get_ab_test_root(config)
    candidate = root / test_id
    root_fd = _open_root(config, create=False)
    try:
        try:
            entry = os.stat(test_id, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return candidate
        if not stat.S_ISDIR(entry.st_mode):
            raise ValueError("A/B 테스트 경로가 안전한 디렉터리가 아닙니다.")
        test_fd = os.open(test_id, _directory_flags(), dir_fd=root_fd)
        os.close(test_fd)
    except OSError as exc:
        raise ValueError("A/B 테스트 경로가 안전하지 않습니다.") from exc
    finally:
        os.close(root_fd)

    return candidate


def create_test_dir(config: AppConfig, test_id: str) -> Path:
    """테스트 루트 및 variant 서브 디렉터리를 생성한다.

    Args:
        config: 앱 설정
        test_id: 테스트 식별자

    Returns:
        테스트 디렉터리 절대 경로

    Raises:
        ValueError: test_id 부적합
        OSError: 디렉터리 생성 실패
    """
    test_dir = get_ab_test_root(config) / test_id
    test_fd = _open_test_dir(config, test_id, create=True)
    try:
        for variant in VARIANT_DIRS:
            try:
                os.mkdir(variant, mode=0o700, dir_fd=test_fd)
            except FileExistsError:
                pass
            variant_fd = os.open(variant, _directory_flags(), dir_fd=test_fd)
            os.close(variant_fd)
    except OSError as exc:
        raise ValueError("A/B variant 디렉터리가 안전하지 않습니다.") from exc
    finally:
        os.close(test_fd)
    logger.debug(f"A/B 테스트 디렉터리 생성: {test_dir}")
    return test_dir


def resolve_variant_dir(config: AppConfig, test_id: str, variant: str) -> Path:
    """고정 variant 디렉터리를 no-follow로 검증하고 lexical 경로를 반환한다."""
    if variant not in VARIANT_DIRS:
        raise ValueError(f"유효하지 않은 variant 디렉터리: {variant!r}")
    test_fd = _open_test_dir(config, test_id, create=False)
    try:
        variant_fd = os.open(variant, _directory_flags(), dir_fd=test_fd)
        os.close(variant_fd)
    except OSError as exc:
        raise ValueError("A/B variant 디렉터리가 안전하지 않습니다.") from exc
    finally:
        os.close(test_fd)
    return _root_path(config) / test_id / variant


def write_variant_text(variant_dir: Path, filename: str, content: str) -> None:
    """검증된 variant 파일을 no-follow + atomic replace로 기록한다."""
    if filename not in _VARIANT_FILES:
        raise ValueError(f"허용되지 않은 A/B 산출물 파일명: {filename!r}")
    variant_fd = _open_variant_dir_path(variant_dir)
    try:
        _atomic_write_text_at(variant_fd, filename, content)
    finally:
        os.close(variant_fd)


def write_variant_json(variant_dir: Path, filename: str, data: Any) -> None:
    """variant JSON을 안전하게 직렬화해 기록한다."""
    write_variant_text(
        variant_dir,
        filename,
        json.dumps(data, ensure_ascii=False, indent=2),
    )


def read_variant_text(variant_dir: Path, filename: str) -> str:
    """검증된 variant 일반 파일을 no-follow로 읽는다."""
    if filename not in _VARIANT_FILES:
        raise ValueError(f"허용되지 않은 A/B 산출물 파일명: {filename!r}")
    variant_fd = _open_variant_dir_path(variant_dir)
    try:
        return _read_text_at(variant_fd, filename)
    finally:
        os.close(variant_fd)


def read_variant_json(variant_dir: Path, filename: str) -> Any:
    """variant JSON을 no-follow로 읽어 파싱한다."""
    return json.loads(read_variant_text(variant_dir, filename))


def read_metadata(config: AppConfig, test_id: str) -> dict[str, Any]:
    """metadata.json 을 읽어 딕셔너리로 반환한다.

    Args:
        config: 앱 설정
        test_id: 테스트 식별자

    Returns:
        metadata 딕셔너리

    Raises:
        FileNotFoundError: 파일이 없을 때
        ValueError: JSON 파싱 실패 또는 test_id 부적합
    """
    test_fd = _open_test_dir(config, test_id, create=False)
    try:
        raw = _read_text_at(test_fd, METADATA_FILENAME)
    except FileNotFoundError:
        raise FileNotFoundError(f"metadata.json 이 없습니다: {test_id}") from None
    finally:
        os.close(test_fd)
    try:
        return cast(dict[str, Any], json.loads(raw))
    except json.JSONDecodeError as exc:
        logger.error(f"metadata.json 파싱 실패: {test_id} ({exc})")
        raise ValueError(f"metadata.json 파싱 실패: {exc}") from exc


def write_metadata(config: AppConfig, test_id: str, data: dict[str, Any]) -> None:
    """metadata.json 을 원자적으로 쓴다 (tmp → rename).

    Args:
        config: 앱 설정
        test_id: 테스트 식별자
        data: 저장할 딕셔너리
    """
    test_fd = _open_test_dir(config, test_id, create=True)
    try:
        _atomic_write_text_at(
            test_fd,
            METADATA_FILENAME,
            json.dumps(data, ensure_ascii=False, indent=2),
        )
    except OSError as exc:
        logger.error(f"metadata.json 저장 실패: {test_id} ({exc})")
        raise
    finally:
        os.close(test_fd)


def update_metadata(config: AppConfig, test_id: str, **patch: Any) -> dict[str, Any]:
    """metadata.json 을 읽고 patch 를 병합한 뒤 다시 쓴다.

    단순한 read-modify-write 이며, 동시성 보호는 호출자의 asyncio.Lock
    (러너의 `_ab_test_lock`) 에 의존한다.

    Args:
        config: 앱 설정
        test_id: 테스트 식별자
        **patch: 덮어쓸 필드

    Returns:
        갱신된 metadata 딕셔너리
    """
    try:
        data = read_metadata(config, test_id)
    except FileNotFoundError:
        data = {}
    data.update(patch)
    write_metadata(config, test_id, data)
    return data


def list_test_ids(config: AppConfig, source_meeting_id: str | None = None) -> list[str]:
    """저장된 모든 테스트 ID 를 최신순으로 반환한다.

    Args:
        config: 앱 설정
        source_meeting_id: 지정되면 해당 소스 회의에 속한 테스트만 필터

    Returns:
        test_id 리스트 (최신순)
    """
    root_fd = _open_root(config, create=True)
    ids: list[str] = []
    try:
        for name in os.listdir(root_fd):
            if not is_valid_test_id(name):
                continue
            try:
                entry = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if not stat.S_ISDIR(entry.st_mode):
                    continue
                test_fd = os.open(name, _directory_flags(), dir_fd=root_fd)
                os.close(test_fd)
            except OSError:
                continue
            if source_meeting_id is not None:
                try:
                    meta = read_metadata(config, name)
                except (FileNotFoundError, ValueError):
                    continue
                if meta.get("source_meeting_id") != source_meeting_id:
                    continue
            ids.append(name)
    finally:
        os.close(root_fd)

    # test_id 에 타임스탬프가 내장되어 있으므로 문자열 역순이 곧 최신순
    ids.sort(reverse=True)
    return ids


def _remove_directory_contents(directory_fd: int) -> None:
    """열어 둔 디렉터리 안의 entry를 symlink 추적 없이 재귀 삭제한다."""
    for name in os.listdir(directory_fd):
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISDIR(entry.st_mode):
            os.unlink(name, dir_fd=directory_fd)
            continue

        child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
        try:
            opened = os.fstat(child_fd)
            if opened.st_dev != entry.st_dev or opened.st_ino != entry.st_ino:
                raise ValueError("A/B 삭제 대상 디렉터리가 처리 중 교체되었습니다.")
            _remove_directory_contents(child_fd)
        finally:
            os.close(child_fd)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if current.st_dev != entry.st_dev or current.st_ino != entry.st_ino:
            raise ValueError("A/B 삭제 대상 디렉터리가 처리 중 교체되었습니다.")
        os.rmdir(name, dir_fd=directory_fd)


def delete_test_dir(config: AppConfig, test_id: str) -> None:
    """테스트 디렉터리를 통째로 삭제한다.

    Args:
        config: 앱 설정
        test_id: 테스트 식별자

    Raises:
        ValueError: test_id 부적합
    """
    if not is_valid_test_id(test_id):
        raise ValueError(f"유효하지 않은 test_id: {test_id!r}")
    root_fd = _open_root(config, create=True)
    try:
        try:
            entry = os.stat(test_id, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            logger.warning(f"삭제 대상 디렉터리가 없음: {test_id}")
            return
        if not stat.S_ISDIR(entry.st_mode):
            raise ValueError("A/B 삭제 대상이 안전한 디렉터리가 아닙니다.")
        test_fd = os.open(test_id, _directory_flags(), dir_fd=root_fd)
        try:
            opened = os.fstat(test_fd)
            if opened.st_dev != entry.st_dev or opened.st_ino != entry.st_ino:
                raise ValueError("A/B 삭제 대상이 처리 중 교체되었습니다.")
            _remove_directory_contents(test_fd)
        finally:
            os.close(test_fd)
        current = os.stat(test_id, dir_fd=root_fd, follow_symlinks=False)
        if current.st_dev != entry.st_dev or current.st_ino != entry.st_ino:
            raise ValueError("A/B 삭제 대상이 처리 중 교체되었습니다.")
        os.rmdir(test_id, dir_fd=root_fd)
        logger.info(f"A/B 테스트 디렉터리 삭제: {test_id}")
    except OSError as exc:
        logger.error(f"A/B 테스트 디렉터리 삭제 실패: {test_id} ({exc})")
        raise
    finally:
        os.close(root_fd)
