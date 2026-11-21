"""격리된 SQLite 보관함에서 45일 음성 만료와 중단 복구를 검증한다."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from config import AppConfig, LifecycleConfig, PathsConfig
from core.job_queue import AsyncJobQueue, JobQueue, JobQueueError, JobStatus
from core.meeting_mutation import MeetingMutationCoordinator
from security import audio_retention as retention

NOW = datetime(2026, 11, 21, 14)
Store = tuple[AppConfig, AsyncJobQueue, MeetingMutationCoordinator]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path.resolve())),
        lifecycle=LifecycleConfig(audio_retention_enabled=True),
    )
    queue = JobQueue(config.paths.resolved_pipeline_db)
    queue.initialize()
    try:
        yield config, AsyncJobQueue(queue), MeetingMutationCoordinator()
    finally:
        queue.close()


def _seed(
    store: Store,
    meeting_id: str,
    status: JobStatus = JobStatus.COMPLETED,
    source: Path | None = None,
) -> tuple[int, Path, Path]:
    """임시 보관함에 원음, 변환본, 전사문, 요약을 남긴다."""
    config, jobs, _ = store
    audio = source or config.paths.resolved_audio_input_dir / f"{meeting_id}.wav"
    audio.parent.mkdir(exist_ok=True, parents=True)
    audio.write_bytes(b"recording")
    output = config.paths.resolved_outputs_dir / meeting_id
    output.mkdir(exist_ok=True, parents=True)
    (output / "input_16k.wav").write_bytes(b"converted")
    (output / "summary.md").write_text("회의 요약")
    cp = config.paths.resolved_checkpoints_dir / meeting_id
    cp.mkdir(exist_ok=True, parents=True)
    (cp / "correct.json").write_text('{"text":"전사문"}')
    return jobs.queue.add_job(meeting_id, str(audio), initial_status=status), audio, output


@pytest.mark.asyncio
async def test_45_day_boundary_and_metadata_survive(store: Store) -> None:
    config, queue, _ = store
    cutoff = NOW - timedelta(days=45)
    meetings = []
    for seconds in (1, 0, -1):
        mid = (cutoff + timedelta(seconds=seconds)).strftime("meeting_%Y%m%d_%H%M%S")
        meetings.append((mid, *_seed(store, mid)))
    config.lifecycle.audio_retention_enabled = False
    assert (await retention.expire_recording_audio(*store, now=NOW)).expired_meetings == 0
    config.lifecycle.audio_retention_enabled = True
    result = await retention.expire_recording_audio(*store, now=NOW)
    assert (result.expired_meetings, result.deleted_files, result.bytes_freed) == (2, 4, 36)
    assert result.errors == 0
    for index, (mid, jid, source, root) in enumerate(meetings):
        assert source.exists() is (index == 0)
        assert (root / "input_16k.wav").exists() is (index == 0)
        job = queue.queue.get_job(jid)
        assert bool(job.audio_expired_at) is (index != 0)
        assert job.status == "completed"
        assert (root / "summary.md").read_text() == "회의 요약"
        assert (
            config.paths.resolved_checkpoints_dir / mid / "correct.json"
        ).read_text() == '{"text":"전사문"}'
    assert (await retention.expire_recording_audio(*store, now=NOW)).deleted_files == 0


@pytest.mark.asyncio
async def test_busy_external_shared_and_linked_audio(store: Store) -> None:
    config, queue, locks = store
    root = config.paths.resolved_base_dir
    mid = "meeting_20260105_140000"
    jid, original, output = _seed(store, mid, source=root / "outside" / "other.wav")
    _, recent, _ = _seed(store, "meeting_20261102_121212")
    (output / "indirect.wav").symlink_to(recent)
    os.link(recent, output / "hard-link.wav")
    copied = output / "belongs-to-another.wav"
    _seed(store, "meeting_20261101_120000", source=copied)
    tmp_tracks = output / ".recap-tracks-42"
    tmp_tracks.mkdir()
    (tmp_tracks / "channel-0.wav").write_bytes(b"pcm")
    async with locks.lease(mid):
        assert (await retention.expire_recording_audio(*store, now=NOW)).deferred_meetings == 1
    active = queue.queue.claim_for_stt_ab_test(jid, "a" * 32)
    assert active.status == "recording"
    assert (await retention.expire_recording_audio(*store, now=NOW)).deferred_meetings == 1
    queue.queue.restore_stt_ab_test_claim(jid, "a" * 32)
    result = await retention.expire_recording_audio(*store, now=NOW)
    assert result.deleted_files == 2 and not result.errors
    assert original.read_bytes() == b"recording" and recent.exists() and copied.exists()
    assert (output / "indirect.wav").is_symlink() and (output / "hard-link.wav").exists()
    assert not (tmp_tracks / "channel-0.wav").exists()
    with pytest.raises(JobQueueError):
        queue.queue.claim_for_stt_ab_test(jid, "b" * 32)
    with pytest.raises(JobQueueError):
        queue.queue.claim_for_retranscribe(jid, "b" * 32)
    with pytest.raises(JobQueueError):
        queue.queue.queue_mic_retry(jid, stt_model="local")


@pytest.mark.asyncio
@pytest.mark.parametrize("use_derived", [False, True])
async def test_last_consumer_expiry_releases_shared_audio(store: Store, use_derived: bool) -> None:
    """원본이나 파생 음성을 다른 회의가 가져갔으면 마지막 회의의 45일째에 회수한다."""
    config, queue, _ = store
    old, original, out = _seed(store, "meeting_20260901_141000")
    shared = out / "input_16k.wav" if use_derived else original
    recent, _, recent_out = _seed(store, "meeting_20261120_140000", source=shared)
    first = await retention.expire_recording_audio(*store, now=NOW)
    assert first.deleted_files == 1 and first.deferred_meetings == 1
    assert shared.exists() and queue.queue.get_job(old).audio_retention_pending
    assert not queue.queue.get_job(recent).audio_expired_at
    last = await retention.expire_recording_audio(*store, now=datetime(2027, 1, 4, 14))
    assert last.expired_meetings == 2 and last.deleted_files == 2 and last.errors == 0
    assert not shared.exists() and not (recent_out / "input_16k.wav").exists()
    assert not queue.queue.get_job(old).audio_retention_pending
    assert (
        config.paths.resolved_checkpoints_dir / "meeting_20260901_141000" / "correct.json"
    ).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(("old_id", "recent_id"), [("Project", "project"), ("Cafe\u0301", "Café")])
async def test_alias_meeting_folders_protect_recent_recording(
    store: Store, old_id: str, recent_id: str
) -> None:
    """서로 다른 DB ID가 같은 macOS 폴더를 가리키면 마지막 회의까지 파생 음성을 보존한다."""
    config, queue, _ = store
    inputs = config.paths.resolved_audio_input_dir
    old, original, old_output = _seed(store, old_id, source=inputs / f"{old_id}.mp3")
    recent, recent_source, _ = _seed(store, recent_id)
    # 대소문자를 구별하는 CI에서도 보수적 별칭 처리를 검증한다.
    for root in (config.paths.resolved_outputs_dir, config.paths.resolved_checkpoints_dir):
        (root / old_id / f"{recent_id}_16k.wav").write_bytes(b"recent derivative")
    (config.paths.resolved_checkpoints_dir / old_id / "pipeline_state.json").write_text(
        json.dumps({"wav_path": str(inputs / f"{old_id}.wav")})
    )
    with sqlite3.connect(config.paths.resolved_pipeline_db) as conn:
        conn.executemany(
            "UPDATE jobs SET created_at=? WHERE id=?",
            [
                ("2026-01-01T14:00:00", old),
                ("2026-11-20T14:00:00", recent),
            ],
        )
    result = await retention.expire_recording_audio(*store, now=NOW)
    assert result.deleted_files == 1 and result.deferred_meetings == 1 and result.errors == 0
    assert not original.exists() and recent_source.read_bytes() == b"recording"
    for root in (config.paths.resolved_outputs_dir, config.paths.resolved_checkpoints_dir):
        assert (root / old_id / f"{recent_id}_16k.wav").read_bytes() == b"recent derivative"
    assert queue.queue.get_job(old).audio_retention_pending
    last = await retention.expire_recording_audio(*store, now=datetime(2027, 1, 4, 14))
    assert last.expired_meetings == 2 and last.errors == 0
    for root in (config.paths.resolved_outputs_dir, config.paths.resolved_checkpoints_dir):
        assert not (root / old_id / f"{recent_id}_16k.wav").exists()
    assert not recent_source.exists() and (old_output / "summary.md").exists()


@pytest.mark.asyncio
async def test_configured_storage_alias_is_rejected_before_mutation(store: Store) -> None:
    """서로 다른 앱 루트가 실제로 겹치는 Mac 구성을 거부한다."""
    config, queue, _ = store
    jid, original, _ = _seed(store, "meeting_20260103_140000")
    config.paths.checkpoints_dir = "OUTPUTS/nested"
    with pytest.raises(ValueError, match="겹칩니다"):
        await retention.expire_recording_audio(*store, now=NOW)
    assert original.exists() and not queue.queue.get_job(jid).audio_expired_at


@pytest.mark.asyncio
async def test_confirmed_legacy_wav_and_crashed_audio_copies(store: Store) -> None:
    """관리 경로와 체크포인트가 일치하는 과거 WAV만 선택한다."""
    config, _, _ = store
    inputs = config.paths.resolved_audio_input_dir
    cp_root = config.paths.resolved_checkpoints_dir
    for reference in (True, False):
        mid = f"meeting_20260102_12000{int(reference)}"
        _seed(store, mid, source=inputs / f"{mid}.m4a")
        legacy = inputs / f"{mid}.wav"
        legacy.write_bytes(b"previous conversion")
        (cp_root / mid / "pipeline_state.json").write_text(
            json.dumps({"wav_path": str(legacy if reference else inputs / "unrelated.wav")})
        )
        partial = cp_root / mid / ".recap-mic-retry-03"
        partial.mkdir()
        (partial / "channel-0-speech.wav").write_bytes(b"partial speech")
        output = config.paths.resolved_outputs_dir / mid
        (output / (".input_16k.wav." + "f" * 32 + ".tmp")).write_bytes(b"unfinished pcm")
        (output / "notes.tmp").write_text("작업 메모")
    result = await retention.expire_recording_audio(*store, now=NOW)
    assert result.deleted_files == 9 and not result.errors
    assert not (inputs / "meeting_20260102_120001.wav").exists()
    assert (inputs / "meeting_20260102_120000.wav").read_bytes() == b"previous conversion"
    assert len(list(config.paths.resolved_outputs_dir.glob("*/notes.tmp"))) == 2


@pytest.mark.asyncio
async def test_stop_finishes_current_meeting_only(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """설정 해제나 앱 종료가 삭제 중 도착해도 나머지 회의까지 예약하지 않는다."""
    config, queue, locks = store
    first, _, _ = _seed(store, "meeting_20260102_120001")
    second, source, _ = _seed(store, "meeting_20260101_120001")
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    remove = retention._remove_claimed_file

    def pause(entry: retention._AudioEntry) -> int | None:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)
        return remove(entry)

    monkeypatch.setattr(retention, "_remove_claimed_file", pause)
    task = asyncio.create_task(retention.expire_recording_audio(*store, now=NOW))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        config.lifecycle.audio_retention_enabled = False
        task.cancel()
        await asyncio.sleep(0)
        assert locks.locked("meeting_20260102_120001") and not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (
        queue.queue.get_job(first).audio_expired_at
        and not queue.queue.get_job(first).audio_retention_pending
    )
    assert not queue.queue.get_job(second).audio_expired_at and source.exists()


@pytest.mark.asyncio
async def test_import_age_and_expired_queue_guards(store: Store) -> None:
    config, queue, _ = store
    jid, original, _ = _seed(store, "imported", JobStatus.RECORDED)
    failed, _, _ = _seed(store, "meeting_20260107_090000", JobStatus.FAILED)
    os.utime(original, (0, 0))
    with sqlite3.connect(config.paths.resolved_pipeline_db) as conn:
        conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (NOW.isoformat(), jid))
    assert (await retention.expire_recording_audio(*store, now=NOW)).expired_meetings == 1
    assert original.exists()
    result = await retention.expire_recording_audio(*store, now=NOW + timedelta(days=45))
    assert result.expired_meetings == 1 and not original.exists()
    raw = queue.queue
    with pytest.raises(JobQueueError):
        raw.queue_job(jid)
    with pytest.raises(JobQueueError):
        raw.queue_jobs_atomically([jid], "")
    with pytest.raises(JobQueueError):
        raw.queue_failed_job(failed)
    with pytest.raises(JobQueueError):
        raw.retry_job(failed)
    assert raw.retry_all_failed() == []
    with pytest.raises(JobQueueError):
        raw.update_status(jid, JobStatus.QUEUED)
    with pytest.raises(JobQueueError):
        raw.force_set_status(jid, JobStatus.QUEUED)


@pytest.mark.asyncio
async def test_restarts_after_move_without_touching_replacement(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, queue, _ = store
    jid, source, output = _seed(store, "meeting_20260105_140002")
    rename = retention.rename_no_replace

    def fault(fd: int, old: str, new: str) -> None:
        rename(fd, old, new)
        raise OSError("합성 장애")

    monkeypatch.setattr(retention, "rename_no_replace", fault)
    assert (await retention.expire_recording_audio(*store, now=NOW)).errors == 1
    assert queue.queue.get_job(jid).audio_retention_pending
    assert not source.exists()
    source.write_bytes(b"replacement")
    monkeypatch.setattr(retention, "rename_no_replace", rename)
    result = await retention.expire_recording_audio(*store, now=NOW)
    assert result.expired_meetings == 1 and result.deleted_files == 2 and not result.errors
    assert source.read_bytes() == b"replacement" and not (output / "input_16k.wav").exists()
    assert not list(source.parent.glob(".recap-expired-*"))


@pytest.mark.asyncio
async def test_swap_at_rename_preserves_new_audio(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, queue, _ = store
    jid, source, output = _seed(store, "meeting_20260105_140003")
    rename = retention.rename_no_replace

    def swap(fd: int, old: str, new: str) -> None:
        if old == source.name:
            source.rename(source.with_suffix(".old"))
            source.write_bytes(b"new source")
        rename(fd, old, new)

    monkeypatch.setattr(retention, "rename_no_replace", swap)
    assert (await retention.expire_recording_audio(*store, now=NOW)).errors == 1
    assert source.read_bytes() == b"new source" and (output / "summary.md").exists()
    assert queue.queue.get_job(jid).audio_retention_pending
