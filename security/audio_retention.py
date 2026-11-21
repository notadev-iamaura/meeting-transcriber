"""녹음 후 45일이 경과한 회의의 앱 소유 음성만 회수한다."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import stat
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter

from config import AppConfig
from core.io_utils import read_text_no_follow, rename_no_replace
from core.job_queue import AsyncJobQueue, Job, JobQueue, JobQueueError, lexical_root_no_symlinks
from core.meeting_mutation import MeetingMutationCoordinator
from core.meeting_progress import recording_start
from core.nightly_window import finish_started_write
from core.quarantine import (
    QuarantineError,
    _open_directory_tree_no_follow,
    _verify_lexical_directory_identity,
)

logger = logging.getLogger(__name__)
AUDIO_RETENTION_DAYS = 45
_AUDIO_SUFFIXES = frozenset({".wav", ".flac", ".m4a", ".mp3", ".ogg", ".webm"})
_TRACK_DIR_PREFIXES = (".recap-tracks-", ".recap-mic-retry-")
_CONVERSION_TEMP = re.compile(r"\..+_16k\.wav\.[0-9a-f]{32}\.tmp")


@dataclass
class AudioRetentionResult:
    """이번 점검에서 실제 제거한 파일과 처리가 끝난 회의 수."""

    deleted_files: int = 0
    bytes_freed: int = 0
    expired_meetings: int = 0
    deferred_meetings: int = 0
    errors: int = 0


class _AudioEntry(BaseModel):
    """재시작 뒤에도 승인된 부모와 음성 inode만 제거하기 위한 DB 기록."""

    path: str
    parent_inode: tuple[int, int]
    identity: tuple[int, int, int, int]
    token: str = Field(pattern=r"^[0-9a-f]{32}$")


_PLAN = TypeAdapter(list[_AudioEntry])


def _path_key(path: Path) -> str:
    """macOS의 대소문자·유니코드 표기 차이가 있는 다른 회의 참조도 보호한다."""
    return unicodedata.normalize("NFC", os.path.normpath(path)).casefold()


@dataclass(frozen=True)
class _Roots:
    inputs: Path
    outputs: Path
    checkpoints: Path

    @classmethod
    def from_config(cls, config: AppConfig) -> _Roots:
        """서로 겹치지 않고 앱 데이터 폴더 아래에 있는 설정 경로만 허용한다."""
        base = lexical_root_no_symlinks(Path(config.paths.base_dir))
        roots = [
            lexical_root_no_symlinks(base / child)
            for child in (
                config.paths.audio_input_dir,
                config.paths.outputs_dir,
                config.paths.checkpoints_dir,
            )
        ]
        storage_keys = [Path(_path_key(root)) for root in roots]
        for i, root in enumerate(roots):
            if base not in root.parents or any(
                i != j and storage_keys[i].is_relative_to(other)
                for j, other in enumerate(storage_keys)
            ):
                raise ValueError("음성 보관 경로가 저장소 외부이거나 서로 겹칩니다")
        return cls(*roots)

    def permits(self, path: Path, job: Job) -> bool:
        """DB에 지정된 입력 또는 해당 회의가 소유한 음성 파일인지 검사한다."""
        if ".." in path.parts:
            return False
        if path.parent == self.inputs:
            return path.suffix.lower() in _AUDIO_SUFFIXES and (
                path == Path(job.audio_path) or path.name == f"{job.meeting_id}.wav"
            )
        for parent in (self.outputs / job.meeting_id, self.checkpoints / job.meeting_id):
            if path.parent == parent or (
                path.parent.parent == parent and path.parent.name.startswith(_TRACK_DIR_PREFIXES)
            ):
                return path.suffix.lower() in _AUDIO_SUFFIXES or bool(
                    _CONVERSION_TEMP.fullmatch(path.name)
                )
        return False


def _entry_stat(fd: int, name: str) -> os.stat_result | None:
    """링크를 따라가지 않고 현재 entry를 조회한다."""
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _identity(entry: os.stat_result) -> tuple[int, int, int, int]:
    """rename으로 변할 수 있는 ctime을 제외한 파일 지문."""
    return entry.st_dev, entry.st_ino, entry.st_size, entry.st_mtime_ns


def _plan_folder(directory: Path, name: str | None, *, tracks: bool = False) -> list[_AudioEntry]:
    """지정 입력 한 개 또는 회의 전용 디렉터리 안의 음성 파일을 수집한다."""
    try:
        fd = _open_directory_tree_no_follow(directory, create=False)
    except FileNotFoundError:
        return []
    entries = []
    try:
        parent = os.fstat(fd)
        for item in [name] if name is not None else os.listdir(fd):
            path = directory / item
            entry = _entry_stat(fd, item)
            if entry is None:
                continue
            if tracks and item.startswith(_TRACK_DIR_PREFIXES) and stat.S_ISDIR(entry.st_mode):
                entries.extend(_plan_folder(path, None))
            elif (
                (path.suffix.lower() in _AUDIO_SUFFIXES or _CONVERSION_TEMP.fullmatch(item))
                and stat.S_ISREG(entry.st_mode)
                and entry.st_nlink == 1
            ):
                entries.append(
                    _AudioEntry(
                        path=str(path),
                        parent_inode=(parent.st_dev, parent.st_ino),
                        identity=_identity(entry),
                        token=uuid.uuid4().hex,
                    )
                )
        _verify_lexical_directory_identity(directory, parent)
        return entries
    finally:
        os.close(fd)


def _remove_claimed_file(entry: _AudioEntry) -> int | None:
    """검증한 파일을 비공개 이름으로 원자 이동한 뒤 다시 확인하고 제거한다."""
    path = Path(entry.path)
    try:
        fd = _open_directory_tree_no_follow(path.parent, create=False)
    except FileNotFoundError:
        return None
    stage = f".recap-expired-{entry.token}.pending"
    try:
        parent = os.fstat(fd)
        if (parent.st_dev, parent.st_ino) != entry.parent_inode:
            raise OSError("만료 대상 부모 폴더가 교체되었습니다")
        _verify_lexical_directory_identity(path.parent, parent)
        moved = _entry_stat(fd, stage)
        if moved is None:
            current = _entry_stat(fd, path.name)
            if current is None:
                return None  # 이전 실행에서 이미 제거됨
            if (
                _identity(current) != entry.identity
                or current.st_nlink != 1
                or not stat.S_ISREG(current.st_mode)
            ):
                raise OSError("만료 처리 도중 음성이 교체 또는 공유되었습니다")
            rename_no_replace(fd, path.name, stage)
            moved = os.stat(stage, dir_fd=fd, follow_symlinks=False)
            if (
                _identity(moved) != entry.identity
                or moved.st_nlink != 1
                or not stat.S_ISREG(moved.st_mode)
            ):
                # 이동 직전 파일이 교체됐으면 새 원본을 덮어쓰지 않고 복원한다.
                rename_no_replace(fd, stage, path.name)
                raise OSError("보관 처리 중 교체된 entry를 복원했습니다")
        if (
            _identity(moved) != entry.identity
            or moved.st_nlink != 1
            or not stat.S_ISREG(moved.st_mode)
        ):
            raise OSError("임시 보관 entry의 지문이 다릅니다")
        _verify_lexical_directory_identity(path.parent, parent)
        os.unlink(stage, dir_fd=fd)
        os.fsync(fd)
        return moved.st_size
    finally:
        os.close(fd)


def _claim_one(queue: JobQueue, roots: _Roots, job: Job, now: datetime) -> bool:
    """회의별 앱 lease를 보유한 동안 안정된 작업의 파일 지문과 만료 상태를 확정한다."""
    if (
        not job.meeting_id
        or job.meeting_id in {".", ".."}
        or (
            Path(job.meeting_id).name != job.meeting_id
            or "\\" in job.meeting_id
            or "\x00" in job.meeting_id
        )
    ):
        raise ValueError("유효하지 않은 회의 ID")
    if not job.audio_expired_at:
        source = Path(job.audio_path)
        plan = (
            _plan_folder(source.parent, source.name)
            if roots.permits(source, job) and source.parent == roots.inputs
            else []
        )
        for root in (roots.outputs, roots.checkpoints):
            plan.extend(_plan_folder(root / job.meeting_id, None, tracks=True))
        # 이전 버전이 audio_input에 둔 WAV는 체크포인트의 정확한 파일 참조가 있을 때만 소유권을 인정한다.
        legacy = roots.inputs / f"{job.meeting_id}.wav"
        if source != legacy:
            state_path = roots.checkpoints / job.meeting_id / "pipeline_state.json"
            try:
                state = json.loads(read_text_no_follow(state_path))
            except (FileNotFoundError, ValueError):
                state = None
            if isinstance(state, dict) and state.get("wav_path") == str(legacy):
                plan.extend(_plan_folder(roots.inputs, legacy.name))
        payload = _PLAN.dump_json(plan).decode("utf-8")
        return queue.claim_audio_retention(job, now.isoformat(), payload)
    return job.audio_retention_pending


def _finish_one(queue: JobQueue, roots: _Roots, job: Job, result: AudioRetentionResult) -> bool:
    """최근 참조가 남은 파일은 지문을 보존해 마지막 참조가 만료된 뒤 다시 처리한다."""

    def remove_audio(payload: str, other_recordings: list[tuple[str, str]]) -> str:
        """DB 쓰기 잠금 안에서 다른 회의가 참조하는 파일과 신규 교체 파일을 보호한다."""
        protected_paths = {_path_key(Path(source)) for _, source in other_recordings}
        # BINARY SQLite ID 두 개가 APFS에서는 같은 outputs·checkpoints 폴더 및
        # 과거 변환 WAV 이름이 된다. 이 파일들은 다른 회의의 DB audio_path에 나타나지 않는다.
        meeting_key = _path_key(Path(job.meeting_id))
        shared_meeting_folder = any(
            _path_key(Path(other_id)) == meeting_key for other_id, _ in other_recordings
        )
        if shared_meeting_folder:
            protected_paths.add(_path_key(roots.inputs / f"{job.meeting_id}.wav"))
        remaining = []
        for entry in _PLAN.validate_json(payload):
            path = Path(entry.path)
            if not roots.permits(path, job):
                raise ValueError("보관 기록이 회의 음성 경계 밖을 가리킵니다")
            if _path_key(path) in protected_paths or (
                shared_meeting_folder and path.parent != roots.inputs
            ):
                remaining.append(entry)
                continue
            freed = _remove_claimed_file(entry)
            if freed is not None:
                result.deleted_files += 1
                result.bytes_freed += freed
        return _PLAN.dump_json(remaining).decode("utf-8") if remaining else ""

    if queue.finish_audio_retention(job, remove_audio):
        result.expired_meetings += 1
        return True
    return False


def _expire_meeting(
    queue: JobQueue, roots: _Roots, job: Job, now: datetime, result: AudioRetentionResult
) -> bool:
    """중단 신호가 와도 한 회의의 예약과 삭제를 마친다. 공유 파일만 같은 점검 끝에 재시도한다."""
    if not _claim_one(queue, roots, job, now):
        result.deferred_meetings += 1
        return False
    return not _finish_one(queue, roots, job, result)


async def expire_recording_audio(
    config: AppConfig,
    queue: AsyncJobQueue,
    coordinator: MeetingMutationCoordinator,
    *,
    now: datetime | None = None,
) -> AudioRetentionResult:
    """녹음 후 45일 이상인 음성만 제거한다. 녹음일 미상은 최초 등록 시각을 사용한다."""
    result = AudioRetentionResult()
    if not config.lifecycle.audio_retention_enabled:
        return result
    current = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = current - timedelta(days=AUDIO_RETENTION_DAYS)
    roots = _Roots.from_config(config)
    pending = []
    for job in await queue.get_all_jobs():
        if not config.lifecycle.audio_retention_enabled:
            return result
        if job.audio_expired_at and not job.audio_retention_pending:
            continue
        try:
            started = recording_start(job.meeting_id) or datetime.fromisoformat(job.created_at)
            if not job.audio_retention_pending and started.astimezone(UTC) > cutoff:
                continue
            if job.status not in {"completed", "failed", "recorded"} or coordinator.locked(
                job.meeting_id
            ):
                result.deferred_meetings += 1
                continue
            async with coordinator.lease(job.meeting_id):
                if await finish_started_write(
                    asyncio.to_thread(_expire_meeting, queue.queue, roots, job, current, result)
                ):
                    pending.append(job)
        except (ValueError, OSError, JobQueueError, QuarantineError, sqlite3.Error) as exc:
            result.errors += 1
            logger.warning("회의 음성 만료 예약 보류: %s (%s)", job.meeting_id, exc)
    for job in pending:
        if not config.lifecycle.audio_retention_enabled:
            break
        try:
            async with coordinator.lease(job.meeting_id):
                if not await finish_started_write(
                    asyncio.to_thread(_finish_one, queue.queue, roots, job, result)
                ):
                    result.deferred_meetings += 1
        except (ValueError, OSError, JobQueueError, QuarantineError, sqlite3.Error) as exc:
            result.errors += 1
            logger.warning("회의 음성 만료 처리 보류: %s (%s)", job.meeting_id, exc)
    return result
