"""
SQLite 기반 작업 큐 모듈 (Job Queue Module)

목적: 회의 전사 파이프라인 작업을 SQLite 테이블로 관리한다.
주요 기능:
    - 작업 등록 (add_job)
    - 상태 머신 기반 상태 전이 (update_status)
    - 재시도 로직 (retry_count, max_retries)
    - WAL 모드로 읽기/쓰기 동시성 확보
    - asyncio.to_thread로 이벤트 루프 블로킹 방지
의존성: sqlite3 (stdlib), config 모듈
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import stat
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from core.quarantine import (
    QuarantineError,
    _open_directory_tree_no_follow,
    _same_inode,
)

logger = logging.getLogger(__name__)

_RETRANSCRIBE_CLAIM_KIND = "retranscribe_pending"
_RETRANSCRIBE_CLAIM_VERSION = 1
_RETRANSCRIBE_PHASES = frozenset({"claimed", "staging", "purging", "committing"})
_AUDIO_REJECTION_CLAIM_KIND = "audio_rejection_claim"
_AUDIO_REJECTION_CLAIM_VERSION = 1


# === 작업 상태 정의 ===


class JobStatus(StrEnum):
    """작업 큐의 상태를 정의하는 열거형.

    상태 전이 규칙:
        recorded → queued (수동 전사 요청 시)
        queued → recording → transcribing → diarizing → merging → embedding → completed
        어떤 상태에서든 → failed 전이 가능
        failed → queued (재시도 시)
    """

    RECORDED = "recorded"
    QUEUED = "queued"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    DIARIZING = "diarizing"
    MERGING = "merging"
    EMBEDDING = "embedding"
    COMPLETED = "completed"
    FAILED = "failed"


# 유효한 상태 전이 맵 (현재 상태 → 전이 가능한 상태 집합)
VALID_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.RECORDED: {JobStatus.QUEUED, JobStatus.FAILED},  # 수동 전사 요청 시
    JobStatus.QUEUED: {JobStatus.RECORDING, JobStatus.TRANSCRIBING, JobStatus.FAILED},
    JobStatus.RECORDING: {JobStatus.TRANSCRIBING, JobStatus.FAILED},
    JobStatus.TRANSCRIBING: {JobStatus.DIARIZING, JobStatus.FAILED},
    JobStatus.DIARIZING: {JobStatus.MERGING, JobStatus.FAILED},
    JobStatus.MERGING: {
        JobStatus.EMBEDDING,
        JobStatus.COMPLETED,
        JobStatus.FAILED,
    },  # skip_llm_steps 시 merging→completed 직행
    JobStatus.EMBEDDING: {JobStatus.COMPLETED, JobStatus.FAILED},
    JobStatus.COMPLETED: set(),  # 완료 후 전이 불가
    JobStatus.FAILED: {JobStatus.QUEUED},  # 재시도만 가능
}


# === 데이터 클래스 ===


@dataclass
class Job:
    """작업 큐의 단일 작업을 나타내는 데이터 클래스.

    Attributes:
        id: 작업 고유 식별자 (자동 증가)
        meeting_id: 회의 고유 식별자
        audio_path: 오디오 파일 절대 경로
        status: 현재 작업 상태
        retry_count: 현재 재시도 횟수
        max_retries: 최대 재시도 횟수
        error_message: 마지막 에러 메시지
        created_at: 작업 생성 시각 (ISO 형식)
        updated_at: 마지막 업데이트 시각 (ISO 형식)
        requested_action: 작업 실행 의도 ("", "transcribe", "full")
    """

    id: int
    meeting_id: str
    audio_path: str
    status: str = JobStatus.QUEUED.value
    retry_count: int = 0
    max_retries: int = 3
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""
    # 사용자 정의 제목 (빈 문자열이면 프론트엔드가 meeting_id 기반 타임스탬프 폴백 사용)
    title: str = ""
    requested_action: str = ""


@dataclass(frozen=True)
class RetranscribeClaim:
    """중단 후에도 원상 복구할 수 있는 재전사 예약 payload."""

    original_status: str
    original_requested_action: str
    token: str
    phase: str

    def to_requested_action(self) -> str:
        """DB requested_action 컬럼에 저장할 versioned JSON을 반환한다."""
        return json.dumps(
            {
                "v": _RETRANSCRIBE_CLAIM_VERSION,
                "kind": _RETRANSCRIBE_CLAIM_KIND,
                "original_status": self.original_status,
                "original_requested_action": self.original_requested_action,
                "token": self.token,
                "phase": self.phase,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True)
class AudioAdmissionHold:
    """레거시 오디오 재감사를 위해 원래 큐 의도를 보존하는 payload."""

    original_status: str
    original_requested_action: str
    token: str

    def to_requested_action(self) -> str:
        """DB requested_action 컬럼용 versioned JSON을 반환한다."""
        return json.dumps(
            {
                "v": 1,
                "kind": "audio_admission_hold",
                "original_status": self.original_status,
                "original_requested_action": self.original_requested_action,
                "token": self.token,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True)
class AudioRejectionClaim:
    """격리와 DB 삭제 사이 crash를 복구하기 위한 미디어 거부 payload."""

    original_status: str
    original_requested_action: str
    token: str
    source_path: str
    source_identity: tuple[int, int, int, int]
    quarantine_path: str

    def to_requested_action(self) -> str:
        """DB requested_action 컬럼에 저장할 strict v1 JSON을 반환한다."""
        return json.dumps(
            {
                "v": _AUDIO_REJECTION_CLAIM_VERSION,
                "kind": _AUDIO_REJECTION_CLAIM_KIND,
                "original_status": self.original_status,
                "original_requested_action": self.original_requested_action,
                "token": self.token,
                "source_path": self.source_path,
                "source_dev": self.source_identity[0],
                "source_ino": self.source_identity[1],
                "source_size": self.source_identity[2],
                "source_mtime_ns": self.source_identity[3],
                "quarantine_path": self.quarantine_path,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


def _valid_audio_rejection_absolute_path(value: object) -> bool:
    """journal에 저장할 경로가 모호성 없는 lexical absolute path인지 반환한다."""
    if not isinstance(value, str) or not value or "\x00" in value or value.startswith("~"):
        return False
    path = Path(value)
    if not path.is_absolute():
        return False
    raw_components = value.split("/")
    return all(component not in {"", ".", ".."} for component in raw_components[1:])


def parse_audio_rejection_claim(requested_action: str) -> AudioRejectionClaim | None:
    """requested_action의 미디어 거부 journal payload를 엄격히 파싱한다."""
    try:
        payload = json.loads(requested_action)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    expected_keys = {
        "v",
        "kind",
        "original_status",
        "original_requested_action",
        "token",
        "source_path",
        "source_dev",
        "source_ino",
        "source_size",
        "source_mtime_ns",
        "quarantine_path",
    }
    if set(payload) != expected_keys:
        return None
    if (
        payload.get("v") != _AUDIO_REJECTION_CLAIM_VERSION
        or payload.get("kind") != _AUDIO_REJECTION_CLAIM_KIND
    ):
        return None

    original_status = payload.get("original_status")
    original_action = payload.get("original_requested_action")
    token = payload.get("token")
    source_path = payload.get("source_path")
    quarantine_path = payload.get("quarantine_path")
    source_dev = payload.get("source_dev")
    source_ino = payload.get("source_ino")
    source_size = payload.get("source_size")
    source_mtime_ns = payload.get("source_mtime_ns")
    if original_status not in {
        JobStatus.RECORDED.value,
        JobStatus.QUEUED.value,
        JobStatus.FAILED.value,
    }:
        return None
    if not isinstance(original_action, str):
        return None
    if not isinstance(token, str):
        return None
    try:
        _validate_claim_token(token, "미디어 거부 claim token")
    except JobQueueError:
        return None
    if not isinstance(source_path, str) or not _valid_audio_rejection_absolute_path(source_path):
        return None
    if not isinstance(quarantine_path, str) or not _valid_audio_rejection_absolute_path(
        quarantine_path
    ):
        return None
    if (
        type(source_dev) is not int
        or source_dev < 0
        or type(source_ino) is not int
        or source_ino < 0
        or type(source_size) is not int
        or source_size < 0
        or type(source_mtime_ns) is not int
        or source_mtime_ns < 0
    ):
        return None
    return AudioRejectionClaim(
        original_status=original_status,
        original_requested_action=original_action,
        token=token,
        source_path=source_path,
        source_identity=(
            source_dev,
            source_ino,
            source_size,
            source_mtime_ns,
        ),
        quarantine_path=quarantine_path,
    )


def parse_audio_admission_hold(requested_action: str) -> AudioAdmissionHold | None:
    """requested_action의 audio-admission hold payload를 엄격히 파싱한다."""
    try:
        payload = json.loads(requested_action)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    expected_keys = {
        "v",
        "kind",
        "original_status",
        "original_requested_action",
        "token",
    }
    if set(payload) != expected_keys:
        return None
    if payload.get("v") != 1 or payload.get("kind") != "audio_admission_hold":
        return None
    original_status = payload.get("original_status")
    original_action = payload.get("original_requested_action")
    token = payload.get("token")
    if original_status not in {JobStatus.QUEUED.value, JobStatus.FAILED.value}:
        return None
    if not isinstance(original_action, str):
        return None
    if not isinstance(token, str) or not token or len(token) > 128:
        return None
    if not all(character.isalnum() or character in {"-", "_"} for character in token):
        return None
    return AudioAdmissionHold(
        original_status=original_status,
        original_requested_action=original_action,
        token=token,
    )


def parse_retranscribe_claim(requested_action: str) -> RetranscribeClaim | None:
    """requested_action의 versioned 재전사 claim을 엄격하게 파싱한다."""
    try:
        payload = json.loads(requested_action)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    expected_keys = {
        "v",
        "kind",
        "original_status",
        "original_requested_action",
        "token",
        "phase",
    }
    if set(payload) != expected_keys:
        return None
    if payload.get("v") != _RETRANSCRIBE_CLAIM_VERSION:
        return None
    if payload.get("kind") != _RETRANSCRIBE_CLAIM_KIND:
        return None

    original_status = payload.get("original_status")
    original_action = payload.get("original_requested_action")
    token = payload.get("token")
    phase = payload.get("phase")
    if original_status not in {JobStatus.COMPLETED.value, JobStatus.FAILED.value}:
        return None
    if not isinstance(original_action, str):
        return None
    if not isinstance(token, str) or not token or len(token) > 128:
        return None
    if not all(character.isalnum() or character in {"-", "_"} for character in token):
        return None
    if phase not in _RETRANSCRIBE_PHASES:
        return None
    return RetranscribeClaim(
        original_status=original_status,
        original_requested_action=original_action,
        token=token,
        phase=phase,
    )


RETRANSCRIBE_OUTPUT_FILES = (
    "corrected.json",
    "summary.md",
    "meeting_minutes.md",
    "summary.json",
)


def _validate_retranscribe_path_component(value: str, label: str) -> None:
    """staging 경로에 사용할 단일 path component를 검증한다."""
    if not value or "\x00" in value or value in {".", ".."} or Path(value).name != value:
        raise JobQueueError(f"유효하지 않은 {label}: {value!r}")
    if "/" in value or "\\" in value:
        raise JobQueueError(f"유효하지 않은 {label}: {value!r}")


def _validate_claim_token(token: str, label: str) -> None:
    """DB payload와 staging 경로에 공통으로 사용할 opaque token을 검증한다."""
    if (
        not token
        or len(token) > 128
        or not all(character.isalnum() or character in {"-", "_"} for character in token)
    ):
        raise JobQueueError(f"유효하지 않은 {label}")


def lexical_root_no_symlinks(root: Path) -> Path:
    """root와 기존 intermediate를 resolve하지 않고 symlink 여부를 검사한다."""
    lexical_root = root.expanduser().absolute()
    if ".." in lexical_root.parts:
        raise JobQueueError(f"staging root에 상위 경로 요소를 사용할 수 없습니다: {root}")
    current = Path(lexical_root.anchor)
    parts = lexical_root.parts[1:] if lexical_root.is_absolute() else lexical_root.parts
    for index, component in enumerate(parts):
        current /= component
        try:
            entry_stat = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise JobQueueError(f"staging root 상태 확인 실패: {current} ({exc})") from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            raise JobQueueError(f"staging root에 심볼릭 링크를 사용할 수 없습니다: {current}")
        if not stat.S_ISDIR(entry_stat.st_mode):
            position = "root" if index == len(parts) - 1 else "root 상위 경로"
            raise JobQueueError(f"staging {position}가 디렉터리가 아닙니다: {current}")
    return lexical_root


def retranscribe_staging_paths(
    checkpoints_root: Path,
    outputs_root: Path,
    meeting_id: str,
    token: str,
) -> tuple[Path, Path]:
    """token으로 결정되는 checkpoint/output staging 경로를 반환한다."""
    _validate_retranscribe_path_component(meeting_id, "meeting_id")
    _validate_claim_token(token, "재전사 claim token")
    checkpoints_lexical = lexical_root_no_symlinks(checkpoints_root)
    outputs_lexical = lexical_root_no_symlinks(outputs_root)
    return (
        checkpoints_lexical / f".retranscribe-{meeting_id}-{token}-checkpoints",
        outputs_lexical / f".retranscribe-{meeting_id}-{token}-outputs",
    )


def _open_pinned_retranscribe_root(
    root: Path, *, create: bool
) -> tuple[Path, int, os.stat_result]:
    """재전사 작업 동안 사용할 root descriptor와 최초 identity를 반환한다."""
    lexical = lexical_root_no_symlinks(root)
    try:
        root_fd = _open_directory_tree_no_follow(lexical, create=create)
    except (OSError, QuarantineError) as exc:
        raise JobQueueError(f"재전사 root 열기 실패: {lexical} ({exc})") from exc
    opened = os.fstat(root_fd)
    if not stat.S_ISDIR(opened.st_mode):
        os.close(root_fd)
        raise JobQueueError(f"재전사 root가 디렉터리가 아닙니다: {lexical}")
    return lexical, root_fd, opened


def _verify_pinned_retranscribe_root(
    root: Path,
    root_fd: int,
    expected: os.stat_result,
) -> None:
    """lexical root entry가 작업 시작 때 연 디렉터리와 같은지 재검증한다."""
    current = os.fstat(root_fd)
    if not stat.S_ISDIR(current.st_mode) or not _same_inode(current, expected):
        raise JobQueueError(f"열어 둔 재전사 root identity가 변경되었습니다: {root}")
    reopened_fd: int | None = None
    try:
        reopened_fd = _open_directory_tree_no_follow(root, create=False)
        reopened = os.fstat(reopened_fd)
    except (OSError, QuarantineError) as exc:
        raise JobQueueError(f"재전사 root 재검증 실패: {root} ({exc})") from exc
    finally:
        if reopened_fd is not None:
            os.close(reopened_fd)
    if not _same_inode(reopened, expected):
        raise JobQueueError(f"재전사 도중 root entry가 교체되었습니다: {root}")


def _entry_stat(directory_fd: int, name: str) -> os.stat_result | None:
    """directory descriptor 기준 entry의 no-follow stat을 반환한다."""
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_child_directory(directory_fd: int, name: str) -> tuple[int, os.stat_result]:
    """부모 descriptor 아래 디렉터리를 no-follow로 열고 entry identity를 고정한다."""
    before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise JobQueueError(f"재전사 경로가 안전한 디렉터리가 아닙니다: {name}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    child_fd = os.open(name, flags, dir_fd=directory_fd)
    opened = os.fstat(child_fd)
    if not stat.S_ISDIR(opened.st_mode) or not _same_inode(before, opened):
        os.close(child_fd)
        raise JobQueueError(f"재전사 디렉터리 entry가 여는 중 교체되었습니다: {name}")
    return child_fd, opened


def _require_open_entry_identity(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
) -> None:
    """열어 둔 entry가 여전히 부모 디렉터리의 같은 이름에 연결됐는지 확인한다."""
    current = _entry_stat(directory_fd, name)
    if current is None or not _same_inode(current, expected):
        raise JobQueueError(f"재전사 도중 디렉터리 entry가 교체되었습니다: {name}")


def _full_entry_identity(file_stat: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """rename 직전 교체까지 감지하는 entry의 전체 identity와 타입을 반환한다."""
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mode,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _same_published_entry(expected: os.stat_result, current: os.stat_result) -> bool:
    """rename이 바꿀 수 있는 ctime을 제외하고 게시된 inode/타입/내용을 검증한다."""
    return (
        expected.st_dev == current.st_dev
        and expected.st_ino == current.st_ino
        and expected.st_mode == current.st_mode
        and expected.st_size == current.st_size
        and expected.st_mtime_ns == current.st_mtime_ns
    )


def _rollback_conflict_name(token: str, name: str) -> str:
    """이름 충돌 시 foreign entry를 보존할 결정적 recovery 이름을 반환한다."""
    digest = hashlib.sha256(name.encode()).hexdigest()[:16]
    return f".rollback-conflict-{token[:64]}-{digest}"


def _recover_mismatched_moved_entry(
    source_fd: int,
    destination_fd: int,
    source_name: str,
    destination_name: str,
    token: str,
    moved: os.stat_result,
) -> None:
    """검증과 rename 사이 바뀌어 잘못 게시된 entry를 source 쪽에 보존한다."""
    destination_current = os.stat(
        destination_name,
        dir_fd=destination_fd,
        follow_symlinks=False,
    )
    if not _same_published_entry(moved, destination_current):
        raise JobQueueError(f"잘못 게시된 entry도 다시 교체되어 회수할 수 없습니다: {source_name}")
    recovery_name = (
        source_name
        if _entry_stat(source_fd, source_name) is None
        else _rollback_conflict_name(token, source_name)
    )
    if _entry_stat(source_fd, recovery_name) is not None:
        raise JobQueueError(f"rollback conflict 보존 경로가 이미 존재합니다: {recovery_name}")
    os.rename(
        destination_name,
        recovery_name,
        src_dir_fd=destination_fd,
        dst_dir_fd=source_fd,
    )
    recovered = os.stat(recovery_name, dir_fd=source_fd, follow_symlinks=False)
    if not _same_published_entry(moved, recovered):
        raise JobQueueError(f"잘못 게시된 entry 회수 identity 검증 실패: {source_name}")


def _move_entry_checked(
    source_fd: int,
    destination_fd: int,
    name: str,
    expected: os.stat_result,
    token: str,
    *,
    destination_name: str | None = None,
) -> os.stat_result:
    """entry를 descriptor-relative rename하고 pre/post identity를 검증한다."""
    published_name = destination_name or name
    current = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
    if _full_entry_identity(current) != _full_entry_identity(expected):
        raise JobQueueError(f"재전사 entry가 rename 직전 교체되었습니다: {name}")
    if _entry_stat(destination_fd, published_name) is not None:
        raise JobQueueError(f"재전사 rename 목적지가 이미 존재합니다: {published_name}")
    os.rename(
        name,
        published_name,
        src_dir_fd=source_fd,
        dst_dir_fd=destination_fd,
    )
    moved = os.stat(published_name, dir_fd=destination_fd, follow_symlinks=False)
    if not _same_published_entry(expected, moved):
        _recover_mismatched_moved_entry(
            source_fd,
            destination_fd,
            name,
            published_name,
            token,
            moved,
        )
        raise JobQueueError(f"재전사 rename 중 entry가 교체되었습니다: {name}")
    return moved


def _restore_checked_moves(
    source_fd: int,
    destination_fd: int,
    moved_entries: dict[str, os.stat_result],
    token: str,
) -> None:
    """부분 이동된 entry를 역순으로 원래 descriptor에 되돌린다."""
    errors: list[str] = []
    for name, moved in reversed(tuple(moved_entries.items())):
        try:
            _move_entry_checked(source_fd, destination_fd, name, moved, token)
        except (OSError, JobQueueError) as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        raise JobQueueError("부분 rename 원복 실패: " + "; ".join(errors))


def _remove_tree_contents(directory_fd: int) -> None:
    """열린 디렉터리의 내용을 심볼릭 링크 없이 descriptor-relative 삭제한다."""
    for name in os.listdir(directory_fd):
        _validate_retranscribe_path_component(name, "staging entry")
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(entry.st_mode):
            child_fd, opened = _open_child_directory(directory_fd, name)
            try:
                _remove_tree_contents(child_fd)
                _require_open_entry_identity(directory_fd, name, opened)
                os.rmdir(name, dir_fd=directory_fd)
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(entry.st_mode):
            raise JobQueueError(f"재전사 staging에 안전하지 않은 entry가 있습니다: {name}")
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not _same_inode(entry, current):
            raise JobQueueError(f"재전사 staging entry가 삭제 전 교체되었습니다: {name}")
        os.unlink(name, dir_fd=directory_fd)


def _rollback_retranscribe_staging_fds(
    checkpoints_fd: int,
    outputs_fd: int,
    meeting_id: str,
    token: str,
) -> None:
    """이미 고정한 root descriptors 안에서 staging 산출물을 원복한다."""
    checkpoint_stage = f".retranscribe-{meeting_id}-{token}-checkpoints"
    output_stage = f".retranscribe-{meeting_id}-{token}-outputs"
    errors: list[str] = []

    try:
        stage_stat = _entry_stat(checkpoints_fd, checkpoint_stage)
        if stage_stat is not None:
            if not stat.S_ISDIR(stage_stat.st_mode):
                raise JobQueueError("checkpoint staging이 안전한 디렉터리가 아닙니다")
            stage_fd, opened_stage = _open_child_directory(checkpoints_fd, checkpoint_stage)
            try:
                original_stat = _entry_stat(checkpoints_fd, meeting_id)
                created_original = False
                if original_stat is None:
                    os.mkdir(meeting_id, mode=0o700, dir_fd=checkpoints_fd)
                    created_original = True
                    original_stat = os.stat(
                        meeting_id,
                        dir_fd=checkpoints_fd,
                        follow_symlinks=False,
                    )
                if not stat.S_ISDIR(original_stat.st_mode):
                    raise JobQueueError("checkpoint rollback 대상이 디렉터리가 아닙니다")
                original_fd, opened_original = _open_child_directory(
                    checkpoints_fd,
                    meeting_id,
                )
                moved_entries: dict[str, os.stat_result] = {}
                try:
                    staged_entries = os.listdir(stage_fd)
                    initial_entries: dict[str, os.stat_result] = {}
                    discard_names: set[str] = set()
                    for name in staged_entries:
                        _validate_retranscribe_path_component(name, "checkpoint entry")
                        source = os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
                        if stat.S_ISLNK(source.st_mode):
                            raise JobQueueError("checkpoint staging에 심볼릭 링크가 있습니다")
                        initial_entries[name] = source
                        destination = _entry_stat(original_fd, name)
                        if destination is None:
                            continue
                        if name != "reindex_required.json" or not stat.S_ISREG(source.st_mode):
                            raise JobQueueError(
                                f"checkpoint rollback 대상이 이미 존재합니다: {name}"
                            )
                        discard_names.add(name)

                    for name, expected in initial_entries.items():
                        if name in discard_names:
                            continue
                        moved_entries[name] = _move_entry_checked(
                            stage_fd,
                            original_fd,
                            name,
                            expected,
                            token,
                        )

                    for name in discard_names:
                        expected = initial_entries[name]
                        current = os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
                        if _full_entry_identity(current) != _full_entry_identity(expected):
                            raise JobQueueError(
                                f"checkpoint staging entry가 삭제 전 교체되었습니다: {name}"
                            )
                        os.unlink(name, dir_fd=stage_fd)

                    _require_open_entry_identity(
                        checkpoints_fd,
                        meeting_id,
                        opened_original,
                    )
                    _require_open_entry_identity(
                        checkpoints_fd,
                        checkpoint_stage,
                        opened_stage,
                    )
                    os.rmdir(checkpoint_stage, dir_fd=checkpoints_fd)
                except BaseException as operation_error:
                    try:
                        _restore_checked_moves(
                            original_fd,
                            stage_fd,
                            moved_entries,
                            token,
                        )
                    except (OSError, JobQueueError) as restore_error:
                        raise JobQueueError(
                            f"checkpoint rollback entry 원복 실패: {restore_error}"
                        ) from operation_error
                    if created_original:
                        try:
                            os.rmdir(meeting_id, dir_fd=checkpoints_fd)
                        except OSError as remove_error:
                            raise JobQueueError(
                                f"checkpoint rollback 임시 original 정리 실패: {remove_error}"
                            ) from operation_error
                    raise
                finally:
                    os.close(original_fd)
            finally:
                os.close(stage_fd)
    except (OSError, JobQueueError) as exc:
        errors.append(f"checkpoint rollback 실패: {exc}")

    try:
        stage_stat = _entry_stat(outputs_fd, output_stage)
        if stage_stat is not None:
            if not stat.S_ISDIR(stage_stat.st_mode):
                raise JobQueueError("output staging이 안전한 디렉터리가 아닙니다")
            stage_fd, opened_stage = _open_child_directory(outputs_fd, output_stage)
            try:
                staged_names = [
                    name for name in RETRANSCRIBE_OUTPUT_FILES if _entry_stat(stage_fd, name)
                ]
                unexpected = set(os.listdir(stage_fd)) - set(staged_names)
                if unexpected:
                    raise JobQueueError(
                        f"output staging에 예상하지 못한 entry가 있습니다: {sorted(unexpected)}"
                    )
                original_stat = _entry_stat(outputs_fd, meeting_id)
                created_original = False
                if staged_names and original_stat is None:
                    os.mkdir(meeting_id, mode=0o700, dir_fd=outputs_fd)
                    created_original = True
                    original_stat = os.stat(
                        meeting_id,
                        dir_fd=outputs_fd,
                        follow_symlinks=False,
                    )
                if original_stat is not None:
                    if not stat.S_ISDIR(original_stat.st_mode):
                        raise JobQueueError("output rollback 대상이 디렉터리가 아닙니다")
                    original_fd, opened_original = _open_child_directory(outputs_fd, meeting_id)
                    output_moved_entries: dict[str, os.stat_result] = {}
                    try:
                        output_initial_entries: dict[str, os.stat_result] = {}
                        for name in staged_names:
                            source = os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
                            if not stat.S_ISREG(source.st_mode):
                                raise JobQueueError(
                                    f"output rollback entry가 일반 파일이 아닙니다: {name}"
                                )
                            if _entry_stat(original_fd, name) is not None:
                                raise JobQueueError(
                                    f"output rollback 대상이 이미 존재합니다: {name}"
                                )
                            output_initial_entries[name] = source
                        for name, expected in output_initial_entries.items():
                            output_moved_entries[name] = _move_entry_checked(
                                stage_fd,
                                original_fd,
                                name,
                                expected,
                                token,
                            )
                        _require_open_entry_identity(outputs_fd, meeting_id, opened_original)
                    except BaseException as operation_error:
                        try:
                            _restore_checked_moves(
                                original_fd,
                                stage_fd,
                                output_moved_entries,
                                token,
                            )
                        except (OSError, JobQueueError) as restore_error:
                            raise JobQueueError(
                                f"output rollback entry 원복 실패: {restore_error}"
                            ) from operation_error
                        if created_original:
                            try:
                                os.rmdir(meeting_id, dir_fd=outputs_fd)
                            except OSError as remove_error:
                                raise JobQueueError(
                                    f"output rollback 임시 original 정리 실패: {remove_error}"
                                ) from operation_error
                        raise
                    finally:
                        os.close(original_fd)
                _require_open_entry_identity(outputs_fd, output_stage, opened_stage)
                os.rmdir(output_stage, dir_fd=outputs_fd)
            finally:
                os.close(stage_fd)
    except (OSError, JobQueueError) as exc:
        errors.append(f"output rollback 실패: {exc}")

    if errors:
        raise JobQueueError("; ".join(errors))


def rollback_retranscribe_staging(
    checkpoints_root: Path,
    outputs_root: Path,
    meeting_id: str,
    token: str,
) -> None:
    """결정적 staging 경로의 재전사 산출물을 pinned root 안에서 원복한다."""
    retranscribe_staging_paths(checkpoints_root, outputs_root, meeting_id, token)
    checkpoints_lexical, checkpoints_fd, checkpoints_identity = _open_pinned_retranscribe_root(
        checkpoints_root, create=True
    )
    outputs_lexical: Path | None = None
    outputs_fd: int | None = None
    outputs_identity: os.stat_result | None = None
    try:
        outputs_lexical, outputs_fd, outputs_identity = _open_pinned_retranscribe_root(
            outputs_root,
            create=True,
        )
        _rollback_retranscribe_staging_fds(
            checkpoints_fd,
            outputs_fd,
            meeting_id,
            token,
        )
        _verify_pinned_retranscribe_root(
            checkpoints_lexical,
            checkpoints_fd,
            checkpoints_identity,
        )
        _verify_pinned_retranscribe_root(outputs_lexical, outputs_fd, outputs_identity)
    finally:
        os.close(checkpoints_fd)
        if outputs_fd is not None:
            os.close(outputs_fd)


def cleanup_retranscribe_staging(
    checkpoints_root: Path,
    outputs_root: Path,
    meeting_id: str,
    token: str,
) -> None:
    """commit 직전 재전사 staging을 no-follow 방식으로 엄격히 정리한다.

    일부 디렉터리가 이미 정리된 경우에도 성공하므로 startup recovery에서
    반복 실행할 수 있다. 예상치 못한 파일·심볼릭 링크·삭제 오류는 claim을
    유지할 수 있도록 호출자에게 전파한다.
    """
    stage_paths = retranscribe_staging_paths(
        checkpoints_root,
        outputs_root,
        meeting_id,
        token,
    )
    for root, stage_path in zip((checkpoints_root, outputs_root), stage_paths, strict=True):
        try:
            lexical, root_fd, root_identity = _open_pinned_retranscribe_root(
                root,
                create=False,
            )
        except JobQueueError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                continue
            raise
        try:
            stage_stat = _entry_stat(root_fd, stage_path.name)
            if stage_stat is None:
                _verify_pinned_retranscribe_root(lexical, root_fd, root_identity)
                continue
            if not stat.S_ISDIR(stage_stat.st_mode):
                raise JobQueueError(f"재전사 staging이 안전한 디렉터리가 아닙니다: {stage_path}")
            stage_fd, opened_stage = _open_child_directory(root_fd, stage_path.name)
            try:
                _remove_tree_contents(stage_fd)
                _require_open_entry_identity(root_fd, stage_path.name, opened_stage)
                os.rmdir(stage_path.name, dir_fd=root_fd)
            finally:
                os.close(stage_fd)
            _verify_pinned_retranscribe_root(lexical, root_fd, root_identity)
        except OSError as exc:
            raise JobQueueError(f"재전사 staging 정리 실패: {stage_path} ({exc})") from exc
        finally:
            os.close(root_fd)


# === 에러 계층 ===


class JobQueueError(Exception):
    """작업 큐 관련 에러의 기본 클래스."""


class InvalidTransitionError(JobQueueError):
    """유효하지 않은 상태 전이 시도 시 발생한다.

    Attributes:
        job_id: 대상 작업 ID
        current_status: 현재 상태
        target_status: 시도한 상태
    """

    def __init__(
        self,
        job_id: int,
        current_status: str,
        target_status: str,
    ) -> None:
        self.job_id = job_id
        self.current_status = current_status
        self.target_status = target_status
        super().__init__(f"작업 {job_id}: 상태 전이 불가 ({current_status} → {target_status})")


class JobNotFoundError(JobQueueError):
    """존재하지 않는 작업을 조회할 때 발생한다.

    Attributes:
        job_id: 조회 시도한 작업 ID
    """

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        super().__init__(f"작업을 찾을 수 없습니다: {job_id}")


class MaxRetriesExceededError(JobQueueError):
    """최대 재시도 횟수를 초과했을 때 발생한다.

    Attributes:
        job_id: 대상 작업 ID
        retry_count: 현재 재시도 횟수
        max_retries: 최대 재시도 횟수
    """

    def __init__(
        self,
        job_id: int,
        retry_count: int,
        max_retries: int,
    ) -> None:
        self.job_id = job_id
        self.retry_count = retry_count
        self.max_retries = max_retries
        super().__init__(f"작업 {job_id}: 최대 재시도 횟수 초과 ({retry_count}/{max_retries})")


# === 메인 클래스 ===


class JobQueue:
    """SQLite 기반 작업 큐 매니저.

    회의 전사 파이프라인의 작업을 SQLite 테이블로 관리한다.
    WAL 모드를 사용하여 읽기/쓰기 동시성을 확보하고,
    상태 머신으로 유효한 상태 전이만 허용한다.

    Args:
        db_path: SQLite 데이터베이스 파일 경로
        max_retries: 최대 재시도 횟수 (기본값: 3)

    사용 예시:
        queue = JobQueue(Path("jobs.db"))
        queue.initialize()
        job_id = queue.add_job("meeting_001", "/path/to/audio.m4a")
        queue.update_status(job_id, JobStatus.RECORDING)
    """

    # 테이블 생성 SQL
    # 주의: title 은 마이그레이션을 통해 추가되므로 여기의 CREATE 문에도 포함.
    # 기존 DB 는 initialize() 의 _ensure_schema_migrations() 에서 ALTER TABLE 로 추가된다.
    _CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id TEXT NOT NULL UNIQUE,
        audio_path TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        retry_count INTEGER NOT NULL DEFAULT 0,
        max_retries INTEGER NOT NULL DEFAULT 3,
        error_message TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        requested_action TEXT NOT NULL DEFAULT ''
    )
    """

    _CREATE_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status)
    """

    def __init__(
        self,
        db_path: Path,
        max_retries: int = 3,
    ) -> None:
        """JobQueue를 초기화한다.

        Args:
            db_path: SQLite DB 파일 경로
            max_retries: 최대 재시도 횟수
        """
        self._db_path = db_path
        self._max_retries = max_retries
        # 스레드별 connection — sqlite3.Connection 은 같은 conn 객체를
        # 여러 스레드에서 동시에 사용하면 SQLITE_MISUSE 가 발생할 수 있다.
        # asyncio.to_thread 로 워커 스레드 풀에서 호출되는 모든 메서드가
        # 자기 스레드의 독립 connection 을 사용하도록 한다.
        self._local = threading.local()
        # 모든 스레드 conn 을 추적해 close() 시 정리할 수 있게 한다.
        self._all_conns: list[sqlite3.Connection] = []
        self._all_conns_lock = threading.Lock()
        self._initialized = False
        # 쓰기 직렬화 락 — 동시 쓰기로 인한 "database is locked" 방지
        self._write_lock = threading.Lock()

        logger.info(f"JobQueue 초기화: db_path={db_path}, max_retries={max_retries}")

    @property
    def db_path(self) -> Path:
        """데이터베이스 파일 경로를 반환한다."""
        return self._db_path

    def _create_connection(self) -> sqlite3.Connection:
        """새 sqlite3 connection 을 생성하고 PRAGMA 를 적용한다.

        WAL 모드 + busy_timeout + 외래키 제약을 모든 connection 에 일관되게 적용.
        스레드별 connection 패턴이라 호출자가 conn 을 _local 또는 _all_conns 에
        등록하는 책임을 진다.
        """
        conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        # WAL 모드 설정 (읽기/쓰기 동시성 향상). 한 번 설정되면 DB 전체에 적용되지만
        # connection 별로도 안전하게 호출할 수 있다.
        conn.execute("PRAGMA journal_mode=WAL")
        # 동시 쓰기 충돌 시 5초간 재시도 (STAB-011)
        conn.execute("PRAGMA busy_timeout=5000")
        # 외래 키 제약 활성화
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        """데이터베이스를 초기화한다.

        DB 파일과 테이블을 생성하고 WAL 모드를 설정한다.
        이미 존재하는 DB에 대해서는 멱등하게 동작한다.
        스키마 생성/마이그레이션은 메인 스레드의 connection 에서만 1회 수행되며,
        이후 다른 스레드의 connection 은 _ensure_connection 에서 lazy 생성된다.

        Raises:
            JobQueueError: DB 초기화 실패 시
        """
        try:
            # 부모 디렉토리 생성
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

            conn = self._create_connection()
            self._local.conn = conn
            with self._all_conns_lock:
                self._all_conns.append(conn)

            # 테이블 + 인덱스 생성 + 마이그레이션 (쓰기 직렬화, 메인 conn 에서 1회)
            with self._write_lock:
                conn.execute(self._CREATE_TABLE_SQL)
                conn.execute(self._CREATE_INDEX_SQL)
                self._ensure_schema_migrations(conn=conn)
                conn.commit()

            self._initialized = True
            logger.info(f"JobQueue DB 초기화 완료: {self._db_path}")

        except sqlite3.Error as e:
            raise JobQueueError(f"DB 초기화 실패: {e}") from e

    def close(self) -> None:
        """모든 스레드의 데이터베이스 connection 을 종료한다."""
        with self._all_conns_lock:
            for conn in self._all_conns:
                try:
                    conn.close()
                except sqlite3.Error as e:
                    logger.warning(f"connection 종료 실패 (무시): {e}")
            self._all_conns.clear()
        # _local 의 conn 참조도 제거 (재초기화 대비)
        if hasattr(self._local, "conn"):
            del self._local.conn
        self._initialized = False
        logger.info("JobQueue DB 모든 connection 종료")

    def _ensure_connection(self) -> sqlite3.Connection:
        """현재 스레드의 DB 연결을 반환한다. 없으면 새로 생성한다.

        threading.local 에 conn 을 보관하여 스레드 간 conn 공유를
        방지한다 (SQLITE_MISUSE 회피). 새 스레드에서 처음 호출되면
        새 connection 을 만들어 _all_conns 에 등록한다.

        Returns:
            현재 스레드 전용 sqlite3.Connection

        Raises:
            JobQueueError: initialize() 가 호출되지 않았을 때
        """
        if not self._initialized:
            raise JobQueueError("DB 연결이 초기화되지 않았습니다. initialize()를 먼저 호출하세요.")
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._create_connection()
            self._local.conn = conn
            with self._all_conns_lock:
                self._all_conns.append(conn)
            logger.debug(
                f"스레드별 conn 생성: thread={threading.current_thread().name}, "
                f"total={len(self._all_conns)}"
            )
        return conn

    @staticmethod
    def _now_iso() -> str:
        """현재 시각을 ISO 형식 문자열로 반환한다."""
        return datetime.now().isoformat()

    def _ensure_schema_migrations(self, conn: sqlite3.Connection | None = None) -> None:
        """기존 DB 스키마를 최신 형태로 마이그레이션한다.

        SQLite는 DROP COLUMN 지원이 제한적이므로, 새 컬럼 추가는 ALTER TABLE ADD COLUMN
        방식으로만 수행한다. PRAGMA table_info 로 현재 컬럼을 확인하고 누락분만 추가한다.

        Args:
            conn: 사용할 connection. None 이면 _ensure_connection() 으로 가져온다.
                  initialize() 도중 호출 시에는 _initialized 가 False 이므로
                  명시적으로 conn 을 전달해야 한다.

        마이그레이션 목록:
            - v1: title TEXT NOT NULL DEFAULT '' (사용자 정의 회의 제목)
            - v2: requested_action TEXT NOT NULL DEFAULT '' (전사만/full 큐 실행 의도)
        """
        if conn is None:
            conn = self._ensure_connection()
        cursor = conn.execute("PRAGMA table_info(jobs)")
        existing_columns = {row["name"] for row in cursor.fetchall()}

        if "title" not in existing_columns:
            logger.info("JobQueue 마이그레이션: jobs.title 컬럼 추가")
            conn.execute("ALTER TABLE jobs ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        if "requested_action" not in existing_columns:
            logger.info("JobQueue 마이그레이션: jobs.requested_action 컬럼 추가")
            conn.execute("ALTER TABLE jobs ADD COLUMN requested_action TEXT NOT NULL DEFAULT ''")

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        """sqlite3.Row를 Job 데이터 클래스로 변환한다.

        Args:
            row: SQLite 조회 결과 행

        Returns:
            Job 인스턴스
        """
        # 마이그레이션 전 DB 를 읽을 가능성에 대비해 title 은 방어적으로 조회
        try:
            title = row["title"]
        except (KeyError, IndexError):
            title = ""
        try:
            requested_action = row["requested_action"]
        except (KeyError, IndexError):
            requested_action = ""

        return Job(
            id=row["id"],
            meeting_id=row["meeting_id"],
            audio_path=row["audio_path"],
            status=row["status"],
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            title=title or "",
            requested_action=requested_action or "",
        )

    def add_job(
        self,
        meeting_id: str,
        audio_path: str,
        initial_status: str = JobStatus.QUEUED.value,
    ) -> int:
        """새 작업을 큐에 등록한다.

        Args:
            meeting_id: 회의 고유 식별자
            audio_path: 오디오 파일 경로
            initial_status: 초기 상태 (기본값: "queued", "recorded"로 설정 시 전사 대기)

        Returns:
            생성된 작업 ID

        Raises:
            JobQueueError: 중복 meeting_id 또는 DB 오류 시
        """
        conn = self._ensure_connection()
        now = self._now_iso()

        try:
            # 쓰기 직렬화 (STAB-017)
            with self._write_lock:
                cursor = conn.execute(
                    """
                    INSERT INTO jobs
                        (meeting_id, audio_path, status, retry_count,
                         max_retries, error_message, created_at, updated_at)
                    VALUES (?, ?, ?, 0, ?, '', ?, ?)
                    """,
                    (
                        meeting_id,
                        audio_path,
                        initial_status,
                        self._max_retries,
                        now,
                        now,
                    ),
                )
                conn.commit()
                job_id = cursor.lastrowid
                if job_id is None:
                    raise JobQueueError("작업 등록 실패: SQLite lastrowid가 비어 있습니다")

            logger.info(
                f"작업 등록: id={job_id}, meeting_id={meeting_id}, status={initial_status}, audio={audio_path}"
            )
            return job_id

        except sqlite3.IntegrityError as e:
            raise JobQueueError(f"작업 등록 실패 (중복 meeting_id?): {meeting_id} — {e}") from e

    def get_job(self, job_id: int) -> Job:
        """작업 ID로 작업을 조회한다.

        Args:
            job_id: 작업 ID

        Returns:
            Job 인스턴스

        Raises:
            JobNotFoundError: 작업이 존재하지 않을 때
        """
        conn = self._ensure_connection()
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

        if row is None:
            raise JobNotFoundError(job_id)

        return self._row_to_job(row)

    def get_job_by_meeting_id(self, meeting_id: str) -> Job | None:
        """meeting_id로 작업을 조회한다.

        Args:
            meeting_id: 회의 고유 식별자

        Returns:
            Job 인스턴스. 없으면 None.
        """
        conn = self._ensure_connection()
        row = conn.execute("SELECT * FROM jobs WHERE meeting_id = ?", (meeting_id,)).fetchone()

        if row is None:
            return None

        return self._row_to_job(row)

    def get_jobs_by_status(self, status: JobStatus) -> list[Job]:
        """특정 상태의 작업 목록을 조회한다.

        Args:
            status: 필터링할 작업 상태

        Returns:
            해당 상태의 Job 리스트 (created_at 오름차순)
        """
        conn = self._ensure_connection()
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC",
            (status.value,),
        ).fetchall()

        return [self._row_to_job(row) for row in rows]

    def get_pending_jobs(self) -> list[Job]:
        """대기 중(queued) 작업 목록을 조회한다.

        Returns:
            queued 상태의 Job 리스트 (큐 진입 시각 오름차순)
        """
        conn = self._ensure_connection()
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = ?
            ORDER BY updated_at ASC, id ASC
            """,
            (JobStatus.QUEUED.value,),
        ).fetchall()

        return [self._row_to_job(row) for row in rows]

    def get_all_jobs(self) -> list[Job]:
        """모든 작업을 조회한다.

        Returns:
            전체 Job 리스트 (meeting_id 내림차순, 최신순)
            meeting_id에 녹음 시작 시각이 포함되어 있으므로 (meeting_YYYYMMDD_HHMMSS)
            created_at(DB 등록 시각)보다 실제 회의 시간순 정렬에 적합하다.
        """
        conn = self._ensure_connection()
        rows = conn.execute("SELECT * FROM jobs ORDER BY meeting_id DESC").fetchall()

        return [self._row_to_job(row) for row in rows]

    def update_title(self, meeting_id: str, title: str) -> Job:
        """회의의 사용자 정의 제목을 업데이트한다.

        빈 문자열("")을 저장하면 기본값 표시(프론트엔드가 meeting_id 기반
        타임스탬프를 사용)로 돌아간다. 이 메서드는 상태 전이 규칙과 무관하다.

        Args:
            meeting_id: 회의 식별자
            title: 새 제목 (최대 200자, 앞뒤 공백 제거됨)

        Returns:
            업데이트된 Job 인스턴스

        Raises:
            JobNotFoundError: meeting_id 로 작업을 찾을 수 없을 때
            JobQueueError: title 이 너무 길거나 DB 오류 시
        """
        conn = self._ensure_connection()

        # 정제 + 검증
        cleaned = (title or "").strip()
        if len(cleaned) > 200:
            raise JobQueueError(f"제목이 너무 깁니다 ({len(cleaned)}자, 최대 200자)")

        # 대상 작업 조회
        job = self.get_job_by_meeting_id(meeting_id)
        if job is None:
            raise JobNotFoundError(0)  # meeting_id 전용 에러 타입이 없으므로 0 사용

        now = self._now_iso()

        with self._write_lock:
            conn.execute(
                """
                UPDATE jobs
                SET title = ?, updated_at = ?
                WHERE meeting_id = ?
                """,
                (cleaned, now, meeting_id),
            )
            conn.commit()

        updated_job = self.get_job_by_meeting_id(meeting_id)
        if updated_job is None:
            raise JobQueueError(f"제목 업데이트 후 작업을 찾을 수 없습니다: {meeting_id}")

        logger.info("제목 업데이트: meeting_id=%s, title=%r", meeting_id, cleaned)
        return updated_job

    def update_status(
        self,
        job_id: int,
        new_status: JobStatus,
        error_message: str = "",
    ) -> Job:
        """작업 상태를 변경한다.

        상태 머신 규칙에 따라 유효한 전이만 허용한다.
        failed 상태로 전이 시 에러 메시지를 기록한다.

        Args:
            job_id: 대상 작업 ID
            new_status: 전이할 상태
            error_message: 에러 메시지 (failed 전이 시)

        Returns:
            업데이트된 Job 인스턴스

        Raises:
            JobNotFoundError: 작업이 없을 때
            InvalidTransitionError: 유효하지 않은 전이 시
        """
        conn = self._ensure_connection()

        # 현재 작업 조회
        job = self.get_job(job_id)
        current_status = JobStatus(job.status)

        # 상태 전이 검증
        valid_targets = VALID_TRANSITIONS.get(current_status, set())
        if new_status not in valid_targets:
            raise InvalidTransitionError(
                job_id,
                current_status.value,
                new_status.value,
            )

        now = self._now_iso()

        # 쓰기 직렬화 (STAB-017)
        with self._write_lock:
            # failed 전이 시 에러 메시지 기록
            if new_status == JobStatus.FAILED:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, error_message = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_status.value, error_message, now, job_id),
                )
            else:
                if new_status == JobStatus.QUEUED:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = ?,
                            requested_action = '',
                            error_message = '',
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (new_status.value, now, job_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = ?, error_message = '', updated_at = ?
                        WHERE id = ?
                        """,
                        (new_status.value, now, job_id),
                    )

            conn.commit()

        logger.info(f"작업 상태 변경: id={job_id}, {current_status.value} → {new_status.value}")

        return self.get_job(job_id)

    def queue_job(
        self,
        job_id: int,
        requested_action: str = "",
    ) -> Job:
        """작업을 queued 상태로 전환하면서 실행 의도를 저장한다.

        Args:
            job_id: 대상 작업 ID
            requested_action: "", "transcribe", "full" 중 하나. 빈 문자열은
                JobProcessor 가 config.pipeline.skip_llm_steps 를 따르게 한다.

        Returns:
            업데이트된 Job 인스턴스

        Raises:
            JobQueueError: requested_action 이 유효하지 않을 때
            InvalidTransitionError: 현재 상태에서 queued 전이가 불가능할 때
        """
        allowed_actions = {"", "transcribe", "full"}
        if requested_action not in allowed_actions:
            raise JobQueueError(f"유효하지 않은 requested_action: {requested_action}")

        conn = self._ensure_connection()
        job = self.get_job(job_id)
        current_status = JobStatus(job.status)
        if JobStatus.QUEUED not in VALID_TRANSITIONS.get(current_status, set()):
            raise InvalidTransitionError(
                job_id,
                current_status.value,
                JobStatus.QUEUED.value,
            )

        now = self._now_iso()
        with self._write_lock:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    requested_action = ?,
                    error_message = '',
                    updated_at = ?
                WHERE id = ?
                """,
                (JobStatus.QUEUED.value, requested_action, now, job_id),
            )
            conn.commit()

        logger.info(
            "작업 큐잉: id=%s, %s → queued, requested_action=%r",
            job_id,
            current_status.value,
            requested_action,
        )
        return self.get_job(job_id)

    def queue_failed_job(self, job_id: int) -> Job:
        """실패 작업을 중간 상태 없이 원자적으로 queued 로 전환한다.

        ``force=true`` 전사 요청용 전이다. retry_count 는 그대로 보존하고,
        이전 실행의 오류와 requested_action 만 같은 UPDATE에서 초기화한다.
        ``WHERE status = 'failed'`` 조건을 포함하므로 조회 이후 다른 워커가
        상태를 바꾼 경우에도 새 상태를 덮어쓰지 않는다.

        Args:
            job_id: 큐에 다시 넣을 실패 작업 ID

        Returns:
            queued 로 전환된 Job 인스턴스

        Raises:
            JobNotFoundError: 작업이 없을 때
            InvalidTransitionError: 현재 상태가 failed 가 아닐 때
        """
        conn = self._ensure_connection()
        now = self._now_iso()

        with self._write_lock:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    requested_action = '',
                    error_message = '',
                    updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.QUEUED.value,
                    now,
                    job_id,
                    JobStatus.FAILED.value,
                ),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    "SELECT status FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                conn.rollback()
                if row is None:
                    raise JobNotFoundError(job_id)
                raise InvalidTransitionError(
                    job_id,
                    str(row["status"]),
                    JobStatus.QUEUED.value,
                )
            conn.commit()

        logger.info("실패 작업 원자적 큐잉: id=%s, failed → queued", job_id)
        return self.get_job(job_id)

    def queue_jobs_atomically(
        self,
        job_ids: list[int],
        requested_action: str = "",
    ) -> list[Job]:
        """recorded 작업 묶음을 단일 SQL transaction으로 queued 전환한다.

        하나라도 누락됐거나 더 이상 recorded가 아니면 전체 UPDATE를 rollback한다.
        """
        allowed_actions = {"", "transcribe", "full"}
        if requested_action not in allowed_actions:
            raise JobQueueError(f"유효하지 않은 requested_action: {requested_action}")
        unique_ids = list(dict.fromkeys(job_ids))
        if len(unique_ids) != len(job_ids):
            raise JobQueueError("일괄 큐잉 job_id가 중복되었습니다")
        if not unique_ids:
            return []

        conn = self._ensure_connection()
        now = self._now_iso()
        placeholders = ", ".join("?" for _ in unique_ids)
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    f"""
                    UPDATE jobs
                    SET status = ?, requested_action = ?, error_message = '', updated_at = ?
                    WHERE id IN ({placeholders}) AND status = ?
                    """,
                    (
                        JobStatus.QUEUED.value,
                        requested_action,
                        now,
                        *unique_ids,
                        JobStatus.RECORDED.value,
                    ),
                )
                if cursor.rowcount != len(unique_ids):
                    conn.rollback()
                    raise JobQueueError("일괄 큐잉 CAS 실패: 일부 작업이 recorded 상태가 아닙니다")
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise JobQueueError(f"일괄 큐잉 SQLite transaction 실패: {exc}") from exc

        logger.info(
            "작업 일괄 원자 큐잉: ids=%s, requested_action=%r",
            unique_ids,
            requested_action,
        )
        return [self.get_job(job_id) for job_id in unique_ids]

    def hold_job_for_audio_admission(self, job_id: int, token: str) -> Job:
        """queued/failed 작업을 원래 실행 의도와 함께 recorded에 보류한다.

        보류 정보는 versioned ``requested_action`` payload에 저장한다. 프로세스가
        중단돼도 다음 시작 시 원래 상태와 실행 의도를 재감사할 수 있고, token
        CAS가 늦게 끝난 validator의 복원을 차단한다.
        """
        _validate_claim_token(token, "audio admission hold token")
        conn = self._ensure_connection()
        now = self._now_iso()
        with self._write_lock:
            row = conn.execute(
                "SELECT status, requested_action FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)
            original_status = str(row["status"])
            if original_status not in {JobStatus.QUEUED.value, JobStatus.FAILED.value}:
                raise InvalidTransitionError(
                    job_id,
                    original_status,
                    JobStatus.RECORDED.value,
                )
            original_action = str(row["requested_action"] or "")
            payload = AudioAdmissionHold(
                original_status=original_status,
                original_requested_action=original_action,
                token=token,
            ).to_requested_action()
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = ?, requested_action = ?, error_message = '', updated_at = ?
                WHERE id = ? AND status = ? AND requested_action = ?
                """,
                (
                    JobStatus.RECORDED.value,
                    payload,
                    now,
                    job_id,
                    original_status,
                    original_action,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise JobQueueError("audio admission hold CAS가 경합으로 실패했습니다")
            conn.commit()
        return self.get_job(job_id)

    def finalize_audio_admission_hold(self, job_id: int, token: str) -> Job:
        """ACCEPT된 hold를 token CAS로 원래 실행 계약에 맞게 finalize한다.

        queued-origin은 원래 action으로 queued에 복귀한다. failed-origin은
        recorded 대기 상태로 두고 이전 오류와 action을 지운다.
        """
        _validate_claim_token(token, "audio admission hold token")
        conn = self._ensure_connection()
        now = self._now_iso()
        with self._write_lock:
            row = conn.execute(
                "SELECT status, requested_action FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)
            payload = str(row["requested_action"] or "")
            hold = parse_audio_admission_hold(payload)
            if row["status"] != JobStatus.RECORDED.value or hold is None:
                raise InvalidTransitionError(
                    job_id,
                    str(row["status"]),
                    "audio admission hold finalize",
                )
            if hold.token != token:
                raise JobQueueError("audio admission hold token이 일치하지 않습니다")

            queued_origin = hold.original_status == JobStatus.QUEUED.value
            target_status = JobStatus.QUEUED.value if queued_origin else JobStatus.RECORDED.value
            target_action = hold.original_requested_action if queued_origin else ""
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = ?, requested_action = ?, error_message = '', updated_at = ?
                WHERE id = ? AND status = ? AND requested_action = ?
                """,
                (
                    target_status,
                    target_action,
                    now,
                    job_id,
                    JobStatus.RECORDED.value,
                    payload,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise JobQueueError("audio admission hold finalize CAS가 경합으로 실패했습니다")
            conn.commit()
        return self.get_job(job_id)

    def claim_for_audio_rejection(
        self,
        job_id: int,
        token: str,
        *,
        source_path: str,
        source_identity: tuple[int, int, int, int],
        quarantine_path: str,
    ) -> Job:
        """확정된 미디어 거부를 격리 전에 durable token CAS로 예약한다."""
        _validate_claim_token(token, "미디어 거부 claim token")
        if not _valid_audio_rejection_absolute_path(source_path):
            raise JobQueueError("유효하지 않은 미디어 거부 source_path")
        if not _valid_audio_rejection_absolute_path(quarantine_path):
            raise JobQueueError("유효하지 않은 미디어 거부 quarantine_path")
        if len(source_identity) != 4 or any(
            type(value) is not int or value < 0 for value in source_identity
        ):
            raise JobQueueError("유효하지 않은 미디어 거부 source identity")

        conn = self._ensure_connection()
        now = self._now_iso()
        with self._write_lock:
            row = conn.execute(
                "SELECT status, requested_action, audio_path FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)
            current_status = str(row["status"])
            current_action = str(row["requested_action"] or "")
            if str(row["audio_path"]) != source_path:
                raise JobQueueError("미디어 거부 source_path가 job audio_path와 일치하지 않습니다")
            hold = parse_audio_admission_hold(current_action)
            if current_status == JobStatus.RECORDED.value and hold is not None:
                original_status = hold.original_status
                original_action = hold.original_requested_action
            elif current_status in {
                JobStatus.RECORDED.value,
                JobStatus.QUEUED.value,
                JobStatus.FAILED.value,
            }:
                original_status = current_status
                original_action = current_action
            else:
                raise InvalidTransitionError(
                    job_id,
                    current_status,
                    JobStatus.RECORDING.value,
                )

            claim = AudioRejectionClaim(
                original_status=original_status,
                original_requested_action=original_action,
                token=token,
                source_path=source_path,
                source_identity=source_identity,
                quarantine_path=quarantine_path,
            )
            payload = claim.to_requested_action()
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = ?, requested_action = ?, error_message = '', updated_at = ?
                WHERE id = ? AND status = ? AND requested_action = ?
                """,
                (
                    JobStatus.RECORDING.value,
                    payload,
                    now,
                    job_id,
                    current_status,
                    current_action,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise JobQueueError("미디어 거부 claim CAS가 경합으로 실패했습니다")
            conn.commit()
        return self.get_job(job_id)

    def finalize_audio_rejection(self, job_id: int, token: str) -> None:
        """정확한 미디어 거부 token+payload가 유지될 때만 job row를 삭제한다."""
        _validate_claim_token(token, "미디어 거부 claim token")
        conn = self._ensure_connection()
        with self._write_lock:
            row = conn.execute(
                "SELECT status, requested_action FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)
            payload = str(row["requested_action"] or "")
            claim = parse_audio_rejection_claim(payload)
            if row["status"] != JobStatus.RECORDING.value or claim is None:
                raise InvalidTransitionError(
                    job_id,
                    str(row["status"]),
                    "audio rejection finalize",
                )
            if claim.token != token:
                raise JobQueueError("미디어 거부 claim token이 일치하지 않습니다")
            cursor = conn.execute(
                """
                DELETE FROM jobs
                WHERE id = ? AND status = ? AND requested_action = ?
                """,
                (job_id, JobStatus.RECORDING.value, payload),
            )
            if cursor.rowcount != 1:
                # CAS를 깨뜨린 동시 변경은 이 메서드가 되돌리면 안 된다.
                conn.commit()
                raise JobQueueError("미디어 거부 finalize CAS가 경합으로 실패했습니다")
            conn.commit()

    def retry_job(self, job_id: int) -> Job:
        """실패한 작업을 재시도한다.

        retry_count를 증가시키고 상태를 queued로 되돌린다.
        max_retries 초과 시 MaxRetriesExceededError를 발생시킨다.

        Args:
            job_id: 재시도할 작업 ID

        Returns:
            업데이트된 Job 인스턴스

        Raises:
            JobNotFoundError: 작업이 없을 때
            InvalidTransitionError: failed 상태가 아닐 때
            MaxRetriesExceededError: 최대 재시도 초과 시
        """
        conn = self._ensure_connection()

        job = self.get_job(job_id)

        # failed 상태만 재시도 가능
        if job.status != JobStatus.FAILED.value:
            raise InvalidTransitionError(
                job_id,
                job.status,
                JobStatus.QUEUED.value,
            )

        # 최대 재시도 초과 확인
        if job.retry_count >= job.max_retries:
            raise MaxRetriesExceededError(
                job_id,
                job.retry_count,
                job.max_retries,
            )

        now = self._now_iso()
        new_retry_count = job.retry_count + 1

        # 쓰기 직렬화 (STAB-017)
        with self._write_lock:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, retry_count = ?, error_message = '', updated_at = ?
                WHERE id = ?
                """,
                (JobStatus.QUEUED.value, new_retry_count, now, job_id),
            )
            conn.commit()

        logger.info(f"작업 재시도: id={job_id}, retry_count={new_retry_count}/{job.max_retries}")

        return self.get_job(job_id)

    def force_set_status(
        self,
        job_id: int,
        new_status: JobStatus,
        error_message: str = "",
    ) -> Job:
        """전이 규칙을 우회하여 작업 상태를 강제로 변경한다.

        VALID_TRANSITIONS 검증을 건너뛰므로 호출자가 안전성을 책임진다.
        주 사용처는 사용자 취소 (in-progress → recorded), 외부 강제 종료 등.

        Args:
            job_id: 변경할 작업 ID
            new_status: 새 상태
            error_message: 에러/사유 메시지

        Returns:
            업데이트된 Job 인스턴스

        Raises:
            JobNotFoundError: 작업이 없을 때
        """
        conn = self._ensure_connection()
        # 존재 확인 (없으면 JobNotFoundError)
        self.get_job(job_id)

        now = self._now_iso()
        with self._write_lock:
            if new_status == JobStatus.RECORDED:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        requested_action = '',
                        error_message = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (new_status.value, error_message, now, job_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, error_message = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_status.value, error_message, now, job_id),
                )
            conn.commit()

        logger.info(
            f"작업 상태 강제 변경: id={job_id} → {new_status.value} ({error_message or '사유 없음'})"
        )
        return self.get_job(job_id)

    def reset_for_retranscribe(self, job_id: int, token: str) -> Job:
        """재전사 claim을 token CAS로 queued 상태에 commit한다.

        completed/failed에서 직접 queued로 우회할 수 없으며, 반드시
        ``claim_for_retranscribe``가 만든 recording claim만 finalize한다.
        """
        conn = self._ensure_connection()
        now = self._now_iso()
        with self._write_lock:
            row = conn.execute(
                "SELECT status, requested_action FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)
            claim = parse_retranscribe_claim(str(row["requested_action"]))
            if row["status"] != JobStatus.RECORDING.value or claim is None:
                raise InvalidTransitionError(job_id, str(row["status"]), JobStatus.QUEUED.value)
            if claim.token != token or claim.phase != "committing":
                raise JobQueueError("재전사 claim token/phase가 일치하지 않습니다")

            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    retry_count = 0,
                    requested_action = '',
                    error_message = '',
                    updated_at = ?
                WHERE id = ? AND status = ? AND requested_action = ?
                """,
                (
                    JobStatus.QUEUED.value,
                    now,
                    job_id,
                    JobStatus.RECORDING.value,
                    str(row["requested_action"]),
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise JobQueueError("재전사 claim finalize CAS가 경합으로 실패했습니다")
            conn.commit()

        logger.info("재전사 claim commit: id=%s, token=%s → queued", job_id, token)
        return self.get_job(job_id)

    def claim_for_retranscribe(self, job_id: int, token: str) -> Job:
        """completed/failed 작업을 원상태 보존 payload로 조건부 예약한다."""
        _validate_claim_token(token, "재전사 claim token")

        conn = self._ensure_connection()
        now = self._now_iso()
        with self._write_lock:
            row = conn.execute(
                "SELECT status, requested_action FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)
            original_status = str(row["status"])
            if original_status not in {JobStatus.COMPLETED.value, JobStatus.FAILED.value}:
                raise InvalidTransitionError(
                    job_id,
                    original_status,
                    JobStatus.RECORDING.value,
                )
            original_action = str(row["requested_action"] or "")
            claim = RetranscribeClaim(
                original_status=original_status,
                original_requested_action=original_action,
                token=token,
                phase="claimed",
            )
            payload = claim.to_requested_action()
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = ?, requested_action = ?, updated_at = ?
                WHERE id = ? AND status = ? AND requested_action = ?
                """,
                (
                    JobStatus.RECORDING.value,
                    payload,
                    now,
                    job_id,
                    original_status,
                    original_action,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise JobQueueError("재전사 claim CAS가 경합으로 실패했습니다")
            conn.commit()

        logger.info("재전사 작업 예약: id=%s, token=%s", job_id, token)
        return self.get_job(job_id)

    def update_retranscribe_claim_phase(
        self,
        job_id: int,
        token: str,
        phase: str,
    ) -> Job:
        """재전사 claim phase를 token CAS로 갱신한다."""
        if phase not in _RETRANSCRIBE_PHASES - {"claimed"}:
            raise JobQueueError(f"유효하지 않은 재전사 claim phase: {phase}")

        conn = self._ensure_connection()
        now = self._now_iso()
        with self._write_lock:
            row = conn.execute(
                "SELECT status, requested_action FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)
            claim = parse_retranscribe_claim(str(row["requested_action"]))
            if row["status"] != JobStatus.RECORDING.value or claim is None:
                raise InvalidTransitionError(job_id, str(row["status"]), JobStatus.RECORDING.value)
            if claim.token != token:
                raise JobQueueError("재전사 claim token이 일치하지 않습니다")
            expected_phase = {
                "staging": "claimed",
                "purging": "staging",
                "committing": "purging",
            }[phase]
            if claim.phase != expected_phase:
                raise JobQueueError(f"재전사 claim phase 전이 불가: {claim.phase} → {phase}")
            next_claim = RetranscribeClaim(
                original_status=claim.original_status,
                original_requested_action=claim.original_requested_action,
                token=claim.token,
                phase=phase,
            )
            cursor = conn.execute(
                """
                UPDATE jobs SET requested_action = ?, updated_at = ?
                WHERE id = ? AND status = ? AND requested_action = ?
                """,
                (
                    next_claim.to_requested_action(),
                    now,
                    job_id,
                    JobStatus.RECORDING.value,
                    str(row["requested_action"]),
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise JobQueueError("재전사 claim phase CAS가 경합으로 실패했습니다")
            conn.commit()
        return self.get_job(job_id)

    def restore_retranscribe_claim(self, job_id: int, token: str) -> Job:
        """재전사 claim payload의 원래 status/action을 token CAS로 복구한다."""
        conn = self._ensure_connection()
        now = self._now_iso()
        with self._write_lock:
            row = conn.execute(
                "SELECT status, requested_action FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)
            claim = parse_retranscribe_claim(str(row["requested_action"]))
            if row["status"] != JobStatus.RECORDING.value or claim is None:
                raise InvalidTransitionError(job_id, str(row["status"]), "claim restore")
            if claim.token != token:
                raise JobQueueError("재전사 claim token이 일치하지 않습니다")

            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = ?, requested_action = ?, updated_at = ?
                WHERE id = ? AND status = ? AND requested_action = ?
                """,
                (
                    claim.original_status,
                    claim.original_requested_action,
                    now,
                    job_id,
                    JobStatus.RECORDING.value,
                    str(row["requested_action"]),
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise JobQueueError("재전사 claim restore CAS가 경합으로 실패했습니다")
            conn.commit()

        logger.warning("재전사 작업 예약 복구: id=%s → %s", job_id, claim.original_status)
        return self.get_job(job_id)

    def retry_all_failed(self) -> list[int]:
        """재시도 가능한 모든 실패 작업을 재시도한다.

        max_retries를 초과하지 않은 failed 작업만 재시도한다.
        PERF: 단일 SQL 배치 UPDATE로 N+1 쿼리 패턴을 제거한다.

        Returns:
            재시도된 작업 ID 리스트
        """
        conn = self._ensure_connection()
        now = self._now_iso()

        # PERF: 단일 쿼리로 재시도 가능한 작업 조회 + 일괄 UPDATE
        with self._write_lock:
            # 재시도 가능한 작업 ID를 한 번에 조회
            rows = conn.execute(
                """
                SELECT id FROM jobs
                WHERE status = ? AND retry_count < max_retries
                """,
                (JobStatus.FAILED.value,),
            ).fetchall()

            retried_ids = [row["id"] for row in rows]

            if retried_ids:
                # 단일 UPDATE로 일괄 상태 변경
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        retry_count = retry_count + 1,
                        error_message = '',
                        updated_at = ?
                    WHERE status = ? AND retry_count < max_retries
                    """,
                    (JobStatus.QUEUED.value, now, JobStatus.FAILED.value),
                )
                conn.commit()

        if retried_ids:
            logger.info(f"일괄 재시도 완료: {len(retried_ids)}건 — ids={retried_ids}")

        return retried_ids

    def count_by_status(self) -> dict[str, int]:
        """상태별 작업 수를 집계한다.

        Returns:
            상태 문자열 → 작업 수 딕셔너리
        """
        conn = self._ensure_connection()
        rows = conn.execute("SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status").fetchall()

        result: dict[str, int] = {}
        for row in rows:
            result[row["status"]] = row["cnt"]

        return result

    def delete_job(self, job_id: int) -> None:
        """작업을 삭제한다.

        Args:
            job_id: 삭제할 작업 ID

        Raises:
            JobNotFoundError: 작업이 없을 때
        """
        conn = self._ensure_connection()

        # 존재 확인
        self.get_job(job_id)

        # 쓰기 직렬화 (STAB-017)
        with self._write_lock:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()

        logger.info(f"작업 삭제: id={job_id}")

    def cleanup_completed(self, before_days: int = 30) -> int:
        """오래된 완료 작업을 정리한다.

        Args:
            before_days: 이 일수보다 오래된 completed 작업 삭제

        Returns:
            삭제된 작업 수
        """
        conn = self._ensure_connection()

        # PERF: 상단 import 사용, cutoff 계산 간소화
        cutoff_str = (datetime.now() - timedelta(days=before_days)).isoformat()

        # 쓰기 직렬화 (STAB-017)
        with self._write_lock:
            cursor = conn.execute(
                """
                DELETE FROM jobs
                WHERE status = ? AND updated_at < ?
                """,
                (JobStatus.COMPLETED.value, cutoff_str),
            )
            conn.commit()

        deleted = cursor.rowcount
        if deleted > 0:
            logger.info(f"완료 작업 정리: {deleted}건 삭제 (기준: {before_days}일 이전)")

        return deleted


# === 비동기 래퍼 ===


class AsyncJobQueue:
    """JobQueue의 비동기 래퍼.

    asyncio.to_thread를 사용하여 SQLite 블로킹 호출을
    이벤트 루프에서 분리한다.

    Args:
        job_queue: 래핑할 JobQueue 인스턴스

    사용 예시:
        queue = JobQueue(Path("jobs.db"))
        async_queue = AsyncJobQueue(queue)
        await async_queue.initialize()
        job_id = await async_queue.add_job("meeting_001", "/path/audio.m4a")
    """

    def __init__(self, job_queue: JobQueue) -> None:
        """AsyncJobQueue를 초기화한다.

        Args:
            job_queue: 동기 JobQueue 인스턴스
        """
        self._queue = job_queue

    @property
    def queue(self) -> JobQueue:
        """내부 동기 JobQueue를 반환한다."""
        return self._queue

    async def initialize(self) -> None:
        """비동기로 DB를 초기화한다."""
        import asyncio

        await asyncio.to_thread(self._queue.initialize)

    async def close(self) -> None:
        """비동기로 DB 연결을 종료한다."""
        import asyncio

        await asyncio.to_thread(self._queue.close)

    async def add_job(
        self,
        meeting_id: str,
        audio_path: str,
        initial_status: str = JobStatus.QUEUED.value,
    ) -> int:
        """비동기로 새 작업을 등록한다.

        Args:
            meeting_id: 회의 고유 식별자
            audio_path: 오디오 파일 경로
            initial_status: 초기 상태 (기본값: "queued", "recorded"로 설정 시 전사 대기)

        Returns:
            생성된 작업 ID
        """
        import asyncio

        return await asyncio.to_thread(
            self._queue.add_job,
            meeting_id,
            audio_path,
            initial_status,
        )

    async def get_job(self, job_id: int) -> Job:
        """비동기로 작업을 조회한다.

        Args:
            job_id: 작업 ID

        Returns:
            Job 인스턴스
        """
        import asyncio

        return await asyncio.to_thread(self._queue.get_job, job_id)

    async def get_pending_jobs(self) -> list[Job]:
        """비동기로 대기 중 작업 목록을 조회한다.

        Returns:
            queued 상태의 Job 리스트
        """
        import asyncio

        return await asyncio.to_thread(self._queue.get_pending_jobs)

    async def update_status(
        self,
        job_id: int,
        new_status: JobStatus,
        error_message: str = "",
    ) -> Job:
        """비동기로 작업 상태를 변경한다.

        Args:
            job_id: 대상 작업 ID
            new_status: 전이할 상태
            error_message: 에러 메시지

        Returns:
            업데이트된 Job 인스턴스
        """
        import asyncio

        return await asyncio.to_thread(
            self._queue.update_status,
            job_id,
            new_status,
            error_message,
        )

    async def queue_job(
        self,
        job_id: int,
        requested_action: str = "",
    ) -> Job:
        """비동기로 작업을 queued 상태로 전환하면서 실행 의도를 저장한다."""
        import asyncio

        return await asyncio.to_thread(
            self._queue.queue_job,
            job_id,
            requested_action,
        )

    async def queue_failed_job(self, job_id: int) -> Job:
        """실패 작업을 중간 상태 없이 비동기로 queued 로 전환한다."""
        import asyncio

        return await asyncio.to_thread(self._queue.queue_failed_job, job_id)

    async def retry_job(self, job_id: int) -> Job:
        """비동기로 실패 작업을 재시도한다.

        Args:
            job_id: 재시도할 작업 ID

        Returns:
            업데이트된 Job 인스턴스
        """
        import asyncio

        return await asyncio.to_thread(self._queue.retry_job, job_id)

    async def retry_all_failed(self) -> list[int]:
        """비동기로 모든 실패 작업을 재시도한다.

        Returns:
            재시도된 작업 ID 리스트
        """
        import asyncio

        return await asyncio.to_thread(self._queue.retry_all_failed)

    async def count_by_status(self) -> dict[str, int]:
        """비동기로 상태별 작업 수를 집계한다.

        Returns:
            상태 문자열 → 작업 수 딕셔너리
        """
        import asyncio

        return await asyncio.to_thread(self._queue.count_by_status)

    async def get_all_jobs(self) -> list[Job]:
        """비동기로 전체 작업을 조회한다.

        Returns:
            전체 Job 리스트
        """
        import asyncio

        return await asyncio.to_thread(self._queue.get_all_jobs)

    async def delete_job(self, job_id: int) -> None:
        """비동기로 작업을 삭제한다.

        Args:
            job_id: 삭제할 작업 ID
        """
        import asyncio

        await asyncio.to_thread(self._queue.delete_job, job_id)

    async def cleanup_completed(self, before_days: int = 30) -> int:
        """비동기로 오래된 완료 작업을 정리한다.

        Args:
            before_days: 기준 일수

        Returns:
            삭제된 작업 수
        """
        import asyncio

        return await asyncio.to_thread(
            self._queue.cleanup_completed,
            before_days,
        )
