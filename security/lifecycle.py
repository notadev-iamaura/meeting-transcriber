"""
데이터 라이프사이클 관리 모듈 (Data Lifecycle Manager Module)

목적: 회의 데이터를 Hot/Warm/Cold 3단계로 자동 분류하고 보존 상태를 점검한다.
주요 기능:
    - Hot (기본 30일): 원본 WAV 유지, 모든 데이터 보존
    - Warm (기본 30~90일): 보존 상태 분류 및 보고
    - Cold (기본 90일+): 보존 상태 분류 및 보고
    - 자동 실행은 파일을 압축·삭제하지 않는 preserve-only 계약
    - 파괴적 수동 유지보수는 명시적 capability가 있을 때만 허용
의존성: config 모듈 (LifecycleConfig, PathsConfig), ffmpeg (시스템 바이너리)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from config import AppConfig

logger = logging.getLogger(__name__)

# meeting_id 유효성 검증 정규식 (path traversal 방지)
_MEETING_ID_PATTERN = re.compile(r"^[\w\-\.]+$")

# FLAC 변환 대상 오디오 확장자
_COMPRESSIBLE_EXTENSIONS = {".wav"}

# 삭제 대상 오디오 확장자
_AUDIO_EXTENSIONS = {".wav", ".flac", ".mp3", ".m4a", ".ogg", ".webm"}

# 동일 UID 프로세스가 namespace를 바꾸는 경쟁 조건에서는 POSIX unlink/rename만으로
# 검증한 inode와 제거할 directory entry를 원자적으로 결합할 수 없다. 따라서 자동
# lifecycle은 분류·보고만 하고 모든 Warm/Cold 파일을 보존한다.
_AUTOMATIC_MUTATION_SKIP_REASON = "automatic_destructive_lifecycle_disabled_same_uid_namespace"


class DataTier(StrEnum):
    """데이터 라이프사이클 등급을 정의하는 열거형.

    Hot: 최근 데이터, 원본 유지
    Warm: 중간 데이터, 보존 점검
    Cold: 오래된 데이터, 보존 점검
    """

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class ColdAction(StrEnum):
    """Cold 등급 데이터에 적용할 정책.

    delete_audio: 오디오 파일만 삭제, 메타데이터(JSON/MD) 보존
    archive: 외장 디스크로 이동 (향후 구현)
    """

    DELETE_AUDIO = "delete_audio"
    ARCHIVE = "archive"


# === 에러 계층 ===


class LifecycleError(Exception):
    """라이프사이클 관리 중 발생하는 에러의 기본 클래스."""


class CompressionError(LifecycleError):
    """FLAC 압축 실패 시 발생한다."""


class DeletionError(LifecycleError):
    """파일 삭제 실패 시 발생한다."""


def _file_identity(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    """파일 교체를 감지할 수 있는 no-follow identity를 반환한다."""
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _directory_identity(directory_stat: os.stat_result) -> tuple[int, int]:
    """directory entry 교체를 감지할 inode identity를 반환한다."""
    return (directory_stat.st_dev, directory_stat.st_ino)


def _file_object_identity(file_stat: os.stat_result) -> tuple[int, int]:
    """내용 변경과 무관하게 같은 file object인지 확인할 identity를 반환한다."""
    return (file_stat.st_dev, file_stat.st_ino)


def _directory_open_flags() -> int:
    """디렉터리 tree를 no-follow로 열기 위한 flags를 반환한다."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise LifecycleError("O_NOFOLLOW를 지원하지 않아 안전한 lifecycle 처리가 불가합니다")
    return int(
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | no_follow
    )


def _open_directory_tree_no_follow(path: Path) -> int:
    """루트부터 모든 directory component를 no-follow로 열어 fd를 반환한다."""
    raw_path = path.expanduser()
    if not raw_path.is_absolute():
        raw_path = raw_path.absolute()
    if "\x00" in str(raw_path) or ".." in raw_path.parts:
        raise LifecycleError(f"안전하지 않은 lifecycle 디렉터리 경로입니다: {path}")

    flags = _directory_open_flags()
    current_fd = os.open(raw_path.anchor, flags)
    try:
        for component in raw_path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            opened = os.fstat(next_fd)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_fd)
                raise LifecycleError(f"디렉터리가 아닌 lifecycle 경로 component입니다: {path}")
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _entry_stat(directory_fd: int, name: str) -> os.stat_result | None:
    """열린 디렉터리 기준으로 entry를 no-follow stat한다."""
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _validate_entry_name(name: str) -> None:
    """dir_fd 연산에 쓸 단일 파일명을 검증한다."""
    if Path(name).name != name or name in {".", ".."}:
        raise LifecycleError(f"안전하지 않은 lifecycle 파일명입니다: {name}")


def _require_regular_file_entry_stat(
    directory_fd: int,
    name: str,
    *,
    label: str,
    display_path: Path,
    require_nonempty: bool = False,
) -> os.stat_result:
    """열린 directory FD 아래의 일반 파일 entry를 no-follow로 검증한다."""
    _validate_entry_name(name)
    try:
        file_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise CompressionError(f"{label} 파일 없음: {display_path}") from exc
    except OSError as exc:
        raise CompressionError(f"{label} 파일 상태 확인 실패: {display_path} - {exc}") from exc

    if not stat.S_ISREG(file_stat.st_mode):
        raise CompressionError(f"{label} 파일이 안전한 일반 파일이 아닙니다: {display_path}")
    if require_nonempty and file_stat.st_size <= 0:
        raise CompressionError(f"{label} 파일이 비어 있습니다: {display_path}")
    return file_stat


def _open_regular_file_entry(
    directory_fd: int,
    name: str,
    *,
    expected: os.stat_result,
    label: str,
    display_path: Path,
) -> int:
    """no-follow로 일반 파일을 열고 scan한 entry와 같은 object인지 확인한다."""
    _validate_entry_name(name)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise CompressionError("O_NOFOLLOW를 지원하지 않아 안전한 lifecycle 처리가 불가합니다")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow
    try:
        file_fd = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError as exc:
        raise CompressionError(f"{label} 파일 없음: {display_path}") from exc
    except OSError as exc:
        raise CompressionError(
            f"{label} 파일을 안전하게 열 수 없습니다: {display_path} - {exc}"
        ) from exc

    opened = os.fstat(file_fd)
    if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(expected):
        os.close(file_fd)
        raise CompressionError(
            f"{label} 파일이 여는 중 변경되었거나 안전하지 않습니다: {display_path}"
        )
    return file_fd


def _file_descriptor_path(file_fd: int) -> str:
    """상속한 일반 file descriptor를 ffmpeg에 전달할 macOS 경로를 반환한다."""
    return f"/dev/fd/{file_fd}"


# === 데이터 클래스 ===


@dataclass
class MeetingInfo:
    """회의 데이터의 라이프사이클 정보를 담는 데이터 클래스.

    Attributes:
        meeting_id: 회의 고유 식별자
        meeting_dir: 회의 데이터 디렉토리 경로
        created_at: 회의 생성 시각
        age_days: 생성 후 경과 일수
        tier: 현재 라이프사이클 등급
        has_wav: WAV 파일 존재 여부
        has_flac: FLAC 파일 존재 여부
        audio_files: 오디오 파일 목록
        directory_identity: scan 시점 meeting directory inode identity
    """

    meeting_id: str
    meeting_dir: Path
    created_at: datetime
    age_days: int
    tier: DataTier
    has_wav: bool = False
    has_flac: bool = False
    audio_files: list[Path] = field(default_factory=list)
    directory_identity: tuple[int, int] | None = None


@dataclass(frozen=True)
class _PinnedMeetingDirectory:
    """lifecycle mutation 동안 고정한 outputs/meeting directory descriptor."""

    outputs_fd: int
    meeting_fd: int
    outputs_identity: tuple[int, int]
    meeting_identity: tuple[int, int]
    meeting_id: str


@dataclass
class LifecycleResult:
    """라이프사이클 실행 결과를 담는 데이터 클래스.

    Attributes:
        total_scanned: 스캔한 회의 수
        compressed: FLAC 압축한 회의 수
        deleted: 오디오 삭제한 회의 수
        skipped: 처리 불필요하여 스킵한 회의 수
        skipped_reasons: 보존한 회의와 구체적인 스킵 사유
        errors: 에러 발생 회의 목록 (meeting_id, 에러 메시지)
        bytes_saved: 절약한 바이트 수
    """

    total_scanned: int = 0
    compressed: int = 0
    deleted: int = 0
    skipped: int = 0
    skipped_reasons: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    bytes_saved: int = 0


# === 메인 클래스 ===


class LifecycleManager:
    """데이터 라이프사이클을 관리하는 클래스.

    config.yaml의 lifecycle 섹션 설정값을 기반으로
    outputs 디렉토리 내 회의 데이터를 Hot/Warm/Cold로 분류한다. 자동 실행은
    파일을 변경하지 않는다. 파괴적 수동 메서드는 명시적 capability가 필요하다.

    동시 실행 방지: _running 플래그로 중복 실행을 차단한다.

    Args:
        config: 애플리케이션 설정 인스턴스
        now: 현재 시각 (테스트용 주입, None이면 실제 시각 사용)
        allow_uncoordinated_manual_mutation: 자동 스케줄러가 사용하지 않는 명시적
            수동 유지보수 capability. 동일 UID namespace 경쟁 방어를 제공하지 않는다.

    사용 예시:
        config = load_config()
        manager = LifecycleManager(config)
        result = manager.run()
        logger.info(f"분류: {result.total_scanned}, 보존: {result.skipped}")
    """

    # FLAC 변환 시 최소 필요 디스크 여유 공간 (바이트)
    _MIN_DISK_FREE_BYTES = 500 * 1024 * 1024  # 500MB

    def __init__(
        self,
        config: AppConfig,
        now: datetime | None = None,
        *,
        allow_uncoordinated_manual_mutation: bool = False,
    ) -> None:
        self._config = config
        self._outputs_dir = config.paths.resolved_outputs_dir
        self._hot_days = config.lifecycle.hot_days
        self._warm_days = config.lifecycle.warm_days
        self._cold_action = ColdAction(config.lifecycle.cold_action)
        self._now = now or datetime.now()
        self._allow_uncoordinated_manual_mutation = allow_uncoordinated_manual_mutation
        # 동시 실행 방지 플래그
        self._running = False

    @property
    def outputs_dir(self) -> Path:
        """관리 대상 outputs 디렉토리 경로."""
        return self._outputs_dir

    def run(self) -> LifecycleResult:
        """전체 라이프사이클 관리를 실행한다.

        outputs 디렉토리 내 모든 회의를 스캔하고 나이에 따라 분류한다.
        자동 실행에서는 오디오를 압축하거나 삭제하지 않는다.
        동시 실행을 방지하여 파일 충돌을 예방한다.

        Returns:
            실행 결과 (분류/보존 수, 에러 목록)
        """
        # 동시 실행 방지
        if self._running:
            logger.warning("라이프사이클 관리가 이미 실행 중입니다. 중복 실행을 건너뜁니다.")
            return LifecycleResult()

        self._running = True
        try:
            return self._run_internal()
        finally:
            self._running = False

    def _check_disk_space(self) -> bool:
        """FLAC 변환을 위한 디스크 여유 공간을 확인한다.

        Returns:
            충분한 공간이 있으면 True, 부족하면 False
        """
        try:
            usage = shutil.disk_usage(str(self._outputs_dir))
            if usage.free < self._MIN_DISK_FREE_BYTES:
                logger.warning(
                    f"디스크 여유 공간 부족: {usage.free / (1024 * 1024):.0f}MB "
                    f"(최소 {self._MIN_DISK_FREE_BYTES / (1024 * 1024):.0f}MB 필요). "
                    f"FLAC 변환을 건너뜁니다."
                )
                return False
            return True
        except OSError as e:
            logger.warning(f"디스크 공간 확인 실패: {e}")
            # 확인 실패 시 안전하게 진행 (변환 시 실패하면 자체 에러 처리가 됨)
            return True

    def _run_internal(self) -> LifecycleResult:
        """라이프사이클 관리의 실제 실행 로직."""
        result = LifecycleResult()

        if not self._outputs_dir.exists():
            logger.warning(f"outputs 디렉토리 없음: {self._outputs_dir}")
            return result

        meetings = self.scan_meetings()
        result.total_scanned = len(meetings)

        for info in meetings:
            try:
                self._process_meeting(info, result)
            except LifecycleError as e:
                result.errors.append((info.meeting_id, str(e)))
                logger.error(f"라이프사이클 처리 실패: {info.meeting_id} - {e}")
            except OSError as e:
                result.errors.append((info.meeting_id, str(e)))
                logger.error(f"파일 시스템 오류: {info.meeting_id} - {e}")

        logger.info(
            f"라이프사이클 관리 완료: "
            f"스캔={result.total_scanned}, 압축={result.compressed}, "
            f"삭제={result.deleted}, 스킵={result.skipped}, "
            f"에러={len(result.errors)}, "
            f"절약={result.bytes_saved / (1024 * 1024):.1f}MB"
        )
        return result

    async def run_async(self) -> LifecycleResult:
        """run()의 비동기 래퍼.

        이벤트 루프 블로킹을 방지하기 위해 별도 스레드에서 실행한다.

        Returns:
            실행 결과
        """
        return await asyncio.to_thread(self.run)

    def scan_meetings(self) -> list[MeetingInfo]:
        """outputs 디렉토리 내 모든 회의를 스캔하여 정보를 수집한다.

        Returns:
            회의 정보 목록 (나이 기준 내림차순 정렬)
        """
        meetings: list[MeetingInfo] = []

        if not self._outputs_dir.exists():
            return meetings

        for entry in sorted(self._outputs_dir.iterdir()):
            try:
                entry_stat = entry.lstat()
            except OSError as exc:
                logger.warning(f"회의 디렉터리 상태 확인 실패, 스킵: {entry} - {exc}")
                continue

            # Path.is_dir()는 symlink target을 따라간다. lifecycle은 압축/삭제를
            # 수행하므로 outputs 외부 디렉터리를 meeting으로 취급하면 안 된다.
            if stat.S_ISLNK(entry_stat.st_mode):
                logger.warning(f"심볼릭 링크 meeting 디렉터리 스킵: {entry}")
                continue
            if not stat.S_ISDIR(entry_stat.st_mode):
                continue

            meeting_id = entry.name

            # meeting_id 유효성 검증 (path traversal 방지)
            if not _MEETING_ID_PATTERN.match(meeting_id):
                logger.warning(f"유효하지 않은 meeting_id 스킵: {meeting_id}")
                continue

            created_at = self._get_meeting_created_at(entry)
            age_days = (self._now - created_at).days
            tier = self.classify_tier(age_days)

            # 오디오 파일 탐색
            audio_files = self._find_audio_files(entry)
            has_wav = any(f.suffix.lower() == ".wav" for f in audio_files)
            has_flac = any(f.suffix.lower() == ".flac" for f in audio_files)

            meetings.append(
                MeetingInfo(
                    meeting_id=meeting_id,
                    meeting_dir=entry,
                    created_at=created_at,
                    age_days=age_days,
                    tier=tier,
                    has_wav=has_wav,
                    has_flac=has_flac,
                    audio_files=audio_files,
                    directory_identity=_directory_identity(entry_stat),
                )
            )

        # 오래된 회의부터 처리 (age_days 내림차순)
        meetings.sort(key=lambda m: m.age_days, reverse=True)
        return meetings

    def classify_tier(self, age_days: int) -> DataTier:
        """경과 일수를 기반으로 데이터 등급을 분류한다.

        Args:
            age_days: 생성 후 경과 일수

        Returns:
            데이터 라이프사이클 등급
        """
        if age_days < self._hot_days:
            return DataTier.HOT
        elif age_days < self._warm_days:
            return DataTier.WARM
        else:
            return DataTier.COLD

    @contextmanager
    def _open_pinned_meeting(
        self,
        meeting_info: MeetingInfo,
    ) -> Iterator[_PinnedMeetingDirectory]:
        """scan 시점 meeting을 outputs root 아래에서 no-follow FD로 다시 고정한다."""
        if (
            meeting_info.meeting_id in {".", ".."}
            or _MEETING_ID_PATTERN.fullmatch(meeting_info.meeting_id) is None
        ):
            raise LifecycleError(f"유효하지 않은 meeting_id입니다: {meeting_info.meeting_id}")

        outputs_fd: int | None = None
        meeting_fd: int | None = None
        try:
            try:
                outputs_fd = _open_directory_tree_no_follow(self._outputs_dir)
                outputs_stat = os.fstat(outputs_fd)
                if not stat.S_ISDIR(outputs_stat.st_mode):
                    raise LifecycleError(
                        f"outputs 디렉터리가 안전한 디렉터리가 아닙니다: {self._outputs_dir}"
                    )

                meeting_entry = _entry_stat(outputs_fd, meeting_info.meeting_id)
                if meeting_entry is None or not stat.S_ISDIR(meeting_entry.st_mode):
                    raise LifecycleError(
                        f"회의 디렉터리가 처리 전 변경되었거나 안전하지 않습니다: {meeting_info.meeting_id}"
                    )
                if (
                    meeting_info.directory_identity is not None
                    and _directory_identity(meeting_entry) != meeting_info.directory_identity
                ):
                    raise LifecycleError(
                        f"회의 디렉터리가 scan 이후 변경되었습니다: {meeting_info.meeting_id}"
                    )

                meeting_fd = os.open(
                    meeting_info.meeting_id,
                    _directory_open_flags(),
                    dir_fd=outputs_fd,
                )
                opened_meeting = os.fstat(meeting_fd)
                if not stat.S_ISDIR(opened_meeting.st_mode) or _directory_identity(
                    opened_meeting
                ) != _directory_identity(meeting_entry):
                    raise LifecycleError(
                        f"회의 디렉터리가 여는 중 변경되었습니다: {meeting_info.meeting_id}"
                    )
            except OSError as exc:
                raise LifecycleError(
                    f"회의 디렉터리를 안전하게 열 수 없습니다: {meeting_info.meeting_id} - {exc}"
                ) from exc

            yield _PinnedMeetingDirectory(
                outputs_fd=outputs_fd,
                meeting_fd=meeting_fd,
                outputs_identity=_directory_identity(outputs_stat),
                meeting_identity=_directory_identity(opened_meeting),
                meeting_id=meeting_info.meeting_id,
            )
        finally:
            if meeting_fd is not None:
                os.close(meeting_fd)
            if outputs_fd is not None:
                os.close(outputs_fd)

    def _verify_pinned_meeting(self, pinned: _PinnedMeetingDirectory) -> None:
        """열린 meeting이 여전히 configured outputs 아래 같은 entry인지 재검증한다."""
        outputs_current = os.fstat(pinned.outputs_fd)
        meeting_current = os.fstat(pinned.meeting_fd)
        if (
            not stat.S_ISDIR(outputs_current.st_mode)
            or _directory_identity(outputs_current) != pinned.outputs_identity
            or not stat.S_ISDIR(meeting_current.st_mode)
            or _directory_identity(meeting_current) != pinned.meeting_identity
        ):
            raise LifecycleError(
                f"열어 둔 lifecycle 디렉터리 identity가 변경되었습니다: {pinned.meeting_id}"
            )

        reopened_outputs_fd: int | None = None
        try:
            reopened_outputs_fd = _open_directory_tree_no_follow(self._outputs_dir)
            reopened_outputs = os.fstat(reopened_outputs_fd)
        except OSError as exc:
            raise LifecycleError(
                f"outputs 디렉터리 재검증 실패: {self._outputs_dir} - {exc}"
            ) from exc
        finally:
            if reopened_outputs_fd is not None:
                os.close(reopened_outputs_fd)
        if _directory_identity(reopened_outputs) != pinned.outputs_identity:
            raise LifecycleError(f"outputs 디렉터리가 처리 중 변경되었습니다: {self._outputs_dir}")

        attached = _entry_stat(pinned.outputs_fd, pinned.meeting_id)
        if (
            attached is None
            or not stat.S_ISDIR(attached.st_mode)
            or _directory_identity(attached) != pinned.meeting_identity
        ):
            raise LifecycleError(f"회의 디렉터리가 처리 중 변경되었습니다: {pinned.meeting_id}")

    def _validate_flac_integrity_at(
        self,
        directory_fd: int,
        flac_name: str,
        flac_path: Path,
    ) -> os.stat_result:
        """pinned directory FD 아래 FLAC을 끝까지 decode해 무결성을 확인한다."""
        before = _require_regular_file_entry_stat(
            directory_fd,
            flac_name,
            label="FLAC",
            display_path=flac_path,
            require_nonempty=True,
        )
        flac_fd = _open_regular_file_entry(
            directory_fd,
            flac_name,
            expected=before,
            label="FLAC",
            display_path=flac_path,
        )
        try:
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-xerror",
                    "-nostdin",
                    "-i",
                    _file_descriptor_path(flac_fd),
                    "-map",
                    "0:a:0",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                timeout=300,
                pass_fds=(flac_fd,),
            )
        except FileNotFoundError as exc:
            raise CompressionError(
                "ffmpeg이 설치되어 있지 않습니다. brew install ffmpeg으로 설치해주세요."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CompressionError(f"FLAC 무결성 검증 타임아웃 (5분 초과): {flac_path}") from exc
        finally:
            os.close(flac_fd)

        if proc.returncode != 0:
            stderr = proc.stderr.strip()[:200] if isinstance(proc.stderr, str) else ""
            raise CompressionError(
                f"FLAC 무결성 검증 실패: {flac_path}{f' - {stderr}' if stderr else ''}"
            )

        after = _require_regular_file_entry_stat(
            directory_fd,
            flac_name,
            label="FLAC",
            display_path=flac_path,
            require_nonempty=True,
        )
        if _file_identity(after) != _file_identity(before):
            raise CompressionError(f"FLAC 파일이 무결성 검증 중 변경되었습니다: {flac_path}")
        return after

    def _create_flac_output_entry(
        self,
        directory_fd: int,
        flac_name: str,
        flac_path: Path,
    ) -> tuple[int, os.stat_result]:
        """이번 변환 전용 FLAC entry를 no-follow와 O_EXCL로 생성한다."""
        _validate_entry_name(flac_name)
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise CompressionError("O_NOFOLLOW를 지원하지 않아 안전한 FLAC 생성이 불가합니다")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | no_follow
        try:
            flac_fd = os.open(flac_name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError as exc:
            raise CompressionError(f"FLAC 파일이 변환 중 생성되었습니다: {flac_path}") from exc
        except OSError as exc:
            raise CompressionError(f"FLAC 파일 생성 실패: {flac_path} - {exc}") from exc

        created = os.fstat(flac_fd)
        if not stat.S_ISREG(created.st_mode):
            os.close(flac_fd)
            raise CompressionError(f"FLAC 파일이 안전한 일반 파일이 아닙니다: {flac_path}")
        return flac_fd, created

    def _unlink_created_flac(
        self,
        directory_fd: int,
        flac_name: str,
        flac_path: Path,
        *,
        expected: os.stat_result,
    ) -> None:
        """이번 호출이 만든 동일 file object만 descriptor-relative로 정리한다."""
        try:
            current = _entry_stat(directory_fd, flac_name)
        except OSError as exc:
            logger.warning(f"불완전 FLAC 상태 확인 실패: {flac_path} - {exc}")
            return
        if current is None:
            return
        if not stat.S_ISREG(current.st_mode) or _file_object_identity(
            current
        ) != _file_object_identity(expected):
            logger.warning(f"변환 중 생성한 FLAC entry가 변경되어 정리를 건너뜁니다: {flac_path}")
            return
        try:
            os.unlink(flac_name, dir_fd=directory_fd)
        except OSError as exc:
            logger.warning(f"불완전 FLAC 정리 실패: {flac_path} - {exc}")

    def _compress_to_flac_at(
        self,
        directory_fd: int,
        wav_name: str,
        wav_path: Path,
        *,
        pinned: _PinnedMeetingDirectory | None = None,
    ) -> Path:
        """pinned directory FD에서 WAV를 FLAC으로 변환하고 원본을 삭제한다."""
        flac_name = Path(wav_name).with_suffix(".flac").name
        flac_path = wav_path.with_suffix(".flac")
        if pinned is not None:
            self._verify_pinned_meeting(pinned)

        try:
            existing_flac = _entry_stat(directory_fd, flac_name)
        except OSError as exc:
            raise CompressionError(f"FLAC 파일 상태 확인 실패: {flac_path} - {exc}") from exc
        if existing_flac is not None:
            validated_flac = self._validate_flac_integrity_at(directory_fd, flac_name, flac_path)
            if pinned is not None:
                self._verify_pinned_meeting(pinned)
            logger.debug(f"FLAC 이미 존재, 스킵: {flac_path}")
            try:
                wav_before = _require_regular_file_entry_stat(
                    directory_fd,
                    wav_name,
                    label="WAV",
                    display_path=wav_path,
                )
            except CompressionError as exc:
                if isinstance(exc.__cause__, FileNotFoundError):
                    return flac_path
                raise
            wav_current = _require_regular_file_entry_stat(
                directory_fd,
                wav_name,
                label="WAV",
                display_path=wav_path,
            )
            if _file_identity(wav_current) != _file_identity(wav_before):
                raise CompressionError(f"WAV 파일이 삭제 전 변경되었습니다: {wav_path}")
            flac_current = _require_regular_file_entry_stat(
                directory_fd,
                flac_name,
                label="FLAC",
                display_path=flac_path,
                require_nonempty=True,
            )
            if _file_identity(flac_current) != _file_identity(validated_flac):
                raise CompressionError(f"FLAC 파일이 삭제 전 변경되었습니다: {flac_path}")
            if pinned is not None:
                self._verify_pinned_meeting(pinned)
            wav_size = wav_current.st_size
            os.unlink(wav_name, dir_fd=directory_fd)
            logger.info(f"잔여 WAV 삭제: {wav_path} ({wav_size} bytes)")
            return flac_path

        wav_before = _require_regular_file_entry_stat(
            directory_fd,
            wav_name,
            label="WAV",
            display_path=wav_path,
        )
        wav_size = wav_before.st_size
        if pinned is not None:
            self._verify_pinned_meeting(pinned)

        flac_fd: int | None = None
        wav_fd: int | None = None
        created_flac: os.stat_result | None = None
        conversion_finished = False
        completed = False
        try:
            flac_fd, created_flac = self._create_flac_output_entry(
                directory_fd,
                flac_name,
                flac_path,
            )
            wav_fd = _open_regular_file_entry(
                directory_fd,
                wav_name,
                expected=wav_before,
                label="WAV",
                display_path=wav_path,
            )
            if pinned is not None:
                self._verify_pinned_meeting(pinned)
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                _file_descriptor_path(wav_fd),
                "-c:a",
                "flac",
                "-compression_level",
                "8",
                "-f",
                "flac",
                _file_descriptor_path(flac_fd),
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                pass_fds=(wav_fd, flac_fd),
            )
            if proc.returncode != 0:
                stderr = proc.stderr.strip()[:200] if isinstance(proc.stderr, str) else ""
                raise CompressionError(f"ffmpeg FLAC 변환 실패 (코드 {proc.returncode}): {stderr}")
            os.fsync(flac_fd)
            conversion_finished = True
        except FileNotFoundError as exc:
            raise CompressionError(
                "ffmpeg이 설치되어 있지 않습니다. brew install ffmpeg으로 설치해주세요."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CompressionError(f"ffmpeg 변환 타임아웃 (5분 초과): {wav_path}") from exc
        except CompressionError:
            raise
        except OSError as exc:
            raise CompressionError(f"FLAC 파일 생성 실패: {flac_path} - {exc}") from exc
        finally:
            if wav_fd is not None:
                os.close(wav_fd)
            if flac_fd is not None:
                os.close(flac_fd)
            if not conversion_finished and created_flac is not None:
                self._unlink_created_flac(
                    directory_fd,
                    flac_name,
                    flac_path,
                    expected=created_flac,
                )

        try:
            if created_flac is None:
                raise CompressionError(f"FLAC 파일 생성 상태를 확인할 수 없습니다: {flac_path}")
            if pinned is not None:
                self._verify_pinned_meeting(pinned)
            generated_flac = _require_regular_file_entry_stat(
                directory_fd,
                flac_name,
                label="FLAC",
                display_path=flac_path,
                require_nonempty=True,
            )
            if _file_object_identity(generated_flac) != _file_object_identity(created_flac):
                raise CompressionError(f"FLAC 파일이 변환 중 변경되었습니다: {flac_path}")
            validated_flac = self._validate_flac_integrity_at(directory_fd, flac_name, flac_path)
            if _file_object_identity(validated_flac) != _file_object_identity(created_flac):
                raise CompressionError(f"FLAC 파일이 무결성 검증 중 변경되었습니다: {flac_path}")

            wav_current = _require_regular_file_entry_stat(
                directory_fd,
                wav_name,
                label="WAV",
                display_path=wav_path,
            )
            if _file_identity(wav_current) != _file_identity(wav_before):
                raise CompressionError(f"WAV 파일이 변환 중 변경되었습니다: {wav_path}")
            flac_current = _require_regular_file_entry_stat(
                directory_fd,
                flac_name,
                label="FLAC",
                display_path=flac_path,
                require_nonempty=True,
            )
            if _file_identity(flac_current) != _file_identity(validated_flac):
                raise CompressionError(f"FLAC 파일이 삭제 전 변경되었습니다: {flac_path}")
            if pinned is not None:
                self._verify_pinned_meeting(pinned)

            os.unlink(wav_name, dir_fd=directory_fd)
            completed = True
            flac_size = validated_flac.st_size
            saved = wav_size - flac_size
            logger.info(
                f"FLAC 압축 완료: {wav_path.name} → {flac_path.name} "
                f"({wav_size:,} → {flac_size:,} bytes, "
                f"{saved:,} bytes 절약, {saved / wav_size * 100:.1f}% 감소)"
            )
            return flac_path
        finally:
            if not completed and created_flac is not None:
                self._unlink_created_flac(
                    directory_fd,
                    flac_name,
                    flac_path,
                    expected=created_flac,
                )

    def compress_to_flac(self, wav_path: Path) -> Path:
        """명시적 capability가 있을 때만 수동 FLAC 유지보수를 수행한다."""
        if not self._allow_uncoordinated_manual_mutation:
            raise CompressionError(
                "파괴적 수동 압축은 기본 비활성화되어 있습니다. "
                "동일 UID namespace 경쟁을 조정한 유지보수 환경에서만 명시적으로 허용하세요."
            )
        directory_fd: int | None = None
        try:
            directory_fd = _open_directory_tree_no_follow(wav_path.parent)
            return self._compress_to_flac_at(directory_fd, wav_path.name, wav_path)
        except FileNotFoundError as exc:
            raise CompressionError(f"WAV 파일 없음: {wav_path}") from exc
        except LifecycleError as exc:
            if isinstance(exc, CompressionError):
                raise
            raise CompressionError(str(exc)) from exc
        except OSError as exc:
            raise CompressionError(
                f"WAV 상위 디렉터리 상태 확인 실패: {wav_path} - {exc}"
            ) from exc
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

    def apply_cold_policy(self, meeting_info: MeetingInfo) -> int:
        """Cold 등급 회의에 정책을 적용한다.

        Args:
            meeting_info: 회의 정보

        Returns:
            삭제/이동으로 절약한 바이트 수

        Raises:
            DeletionError: 파일 삭제 실패 시
        """
        if not self._allow_uncoordinated_manual_mutation:
            raise DeletionError(
                "파괴적 수동 Cold 정책은 기본 비활성화되어 있습니다. "
                "동일 UID namespace 경쟁을 조정한 유지보수 환경에서만 명시적으로 허용하세요."
            )
        if self._cold_action == ColdAction.DELETE_AUDIO:
            return self._delete_audio_files(meeting_info)
        elif self._cold_action == ColdAction.ARCHIVE:
            logger.info(f"아카이브 정책은 아직 미구현입니다: {meeting_info.meeting_id}")
            return 0
        return 0

    def get_summary(self) -> dict[str, int]:
        """현재 데이터 등급별 회의 수를 요약한다.

        Returns:
            등급별 회의 수 딕셔너리
            예: {"hot": 5, "warm": 3, "cold": 2, "total": 10}
        """
        meetings = self.scan_meetings()
        summary: dict[str, int] = {"hot": 0, "warm": 0, "cold": 0, "total": len(meetings)}
        for m in meetings:
            summary[m.tier.value] += 1
        return summary

    # === 내부 메서드 ===

    def _process_meeting(
        self,
        info: MeetingInfo,
        result: LifecycleResult,
    ) -> None:
        """개별 회의를 분류하고 파일 변경 없이 보존 결과를 기록한다.

        Args:
            info: 회의 정보
            result: 실행 결과 (누적)
        """
        if info.tier == DataTier.HOT:
            result.skipped += 1
            return

        result.skipped += 1
        result.skipped_reasons.append((info.meeting_id, _AUTOMATIC_MUTATION_SKIP_REASON))
        logger.warning(
            "자동 lifecycle 파괴 작업 보류: meeting=%s, tier=%s, reason=%s",
            info.meeting_id,
            info.tier.value,
            _AUTOMATIC_MUTATION_SKIP_REASON,
        )

    def _get_meeting_created_at(self, meeting_dir: Path) -> datetime:
        """회의의 생성 시각을 결정한다.

        pipeline_state.json의 created_at 필드를 우선 사용하고,
        없으면 디렉토리의 수정 시각을 사용한다.

        Args:
            meeting_dir: 회의 데이터 디렉토리

        Returns:
            회의 생성 시각
        """
        state_path = meeting_dir / "pipeline_state.json"

        if state_path.exists():
            try:
                with open(state_path, encoding="utf-8") as f:
                    data = json.load(f)
                created_str = data.get("created_at", "")
                if created_str:
                    return datetime.fromisoformat(created_str)
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                logger.warning(
                    f"pipeline_state.json 파싱 실패, 디렉토리 mtime 사용: {meeting_dir} - {e}"
                )

        # 폴백: 디렉토리 수정 시각
        mtime = meeting_dir.stat().st_mtime
        return datetime.fromtimestamp(mtime)

    def _find_audio_files(self, meeting_dir: Path) -> list[Path]:
        """회의 디렉토리에서 오디오 파일을 찾는다.

        Args:
            meeting_dir: 회의 데이터 디렉토리

        Returns:
            오디오 파일 경로 목록
        """
        audio_files: list[Path] = []
        try:
            meeting_stat = meeting_dir.lstat()
        except OSError as exc:
            raise LifecycleError(f"회의 디렉터리 상태 확인 실패: {meeting_dir} - {exc}") from exc
        if stat.S_ISLNK(meeting_stat.st_mode) or not stat.S_ISDIR(meeting_stat.st_mode):
            raise LifecycleError(f"회의 디렉터리가 안전한 일반 디렉터리가 아닙니다: {meeting_dir}")

        for f in meeting_dir.iterdir():
            try:
                file_stat = f.lstat()
            except OSError as exc:
                logger.warning(f"오디오 파일 상태 확인 실패, 스킵: {f} - {exc}")
                continue
            if stat.S_ISREG(file_stat.st_mode) and f.suffix.lower() in _AUDIO_EXTENSIONS:
                audio_files.append(f)
        return sorted(audio_files)

    def _apply_cold_policy_at(
        self,
        pinned: _PinnedMeetingDirectory,
        meeting_info: MeetingInfo,
    ) -> int:
        """이미 고정한 meeting FD 안에서 cold 정책을 적용한다."""
        if self._cold_action == ColdAction.DELETE_AUDIO:
            return self._delete_audio_files_at(pinned, meeting_info)
        if self._cold_action == ColdAction.ARCHIVE:
            logger.info(f"아카이브 정책은 아직 미구현입니다: {meeting_info.meeting_id}")
        return 0

    def _delete_audio_files(self, meeting_info: MeetingInfo) -> int:
        """회의의 오디오 파일을 pinned directory FD 기준으로 삭제한다."""
        try:
            with self._open_pinned_meeting(meeting_info) as pinned:
                return self._delete_audio_files_at(pinned, meeting_info)
        except DeletionError:
            raise
        except LifecycleError as exc:
            raise DeletionError(f"오디오 파일 삭제 전 경로 검증 실패: {exc}") from exc

    def _delete_audio_files_at(
        self,
        pinned: _PinnedMeetingDirectory,
        meeting_info: MeetingInfo,
    ) -> int:
        """현재 pinned meeting FD의 일반 오디오 entry만 identity 확인 후 삭제한다."""
        self._verify_pinned_meeting(pinned)
        audio_files: list[tuple[str, os.stat_result]] = []
        try:
            for name in sorted(os.listdir(pinned.meeting_fd)):
                if Path(name).suffix.lower() not in _AUDIO_EXTENSIONS:
                    continue
                entry = _entry_stat(pinned.meeting_fd, name)
                if entry is not None and stat.S_ISREG(entry.st_mode):
                    audio_files.append((name, entry))
        except OSError as exc:
            raise DeletionError(
                f"오디오 파일 목록 조회 실패: {meeting_info.meeting_dir} - {exc}"
            ) from exc

        if not audio_files:
            logger.debug(f"삭제할 오디오 파일 없음: {meeting_info.meeting_id}")
            return 0

        total_freed = 0
        for name, expected in audio_files:
            self._verify_pinned_meeting(pinned)
            current = _entry_stat(pinned.meeting_fd, name)
            if (
                current is None
                or not stat.S_ISREG(current.st_mode)
                or _file_identity(current) != _file_identity(expected)
            ):
                raise DeletionError(
                    f"오디오 파일이 삭제 중 변경되었거나 안전하지 않습니다: "
                    f"{meeting_info.meeting_dir / name}"
                )
            try:
                os.unlink(name, dir_fd=pinned.meeting_fd)
            except OSError as exc:
                raise DeletionError(
                    f"오디오 파일 삭제 실패: {meeting_info.meeting_dir / name} - {exc}"
                ) from exc
            total_freed += current.st_size
            logger.info(
                f"오디오 삭제: {name} ({current.st_size:,} bytes) - {meeting_info.meeting_id}"
            )

        logger.info(
            f"Cold 정책 적용 완료: {meeting_info.meeting_id}, "
            f"삭제 {len(audio_files)}개 파일, {total_freed:,} bytes 해제"
        )
        return total_freed


def run_lifecycle(config: AppConfig | None = None) -> LifecycleResult:
    """라이프사이클 관리의 편의 함수.

    LifecycleManager 인스턴스를 생성하고 run()을 호출한다.

    Args:
        config: 애플리케이션 설정. None이면 싱글턴에서 가져온다.

    Returns:
        실행 결과
    """
    if config is None:
        from config import get_config

        config = get_config()

    manager = LifecycleManager(config)
    return manager.run()
