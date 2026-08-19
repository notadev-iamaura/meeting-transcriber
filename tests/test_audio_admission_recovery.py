"""미디어 거부 격리와 DB 정리 사이의 crash recovery 계약 테스트."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import job_queue as job_queue_module
from core.audio_quality import AudioFailureKind, AudioQualityResult, AudioQualityStatus
from core.job_queue import AsyncJobQueue, JobQueue, JobQueueError, JobStatus
from core.orchestrator import JobProcessor
from core.watcher import FolderWatcher, _OpenWriterState

SourceIdentity = tuple[int, int, int, int]


@pytest.fixture
def queue(tmp_path: Path) -> JobQueue:
    """durable claim을 실제 SQLite 재접속 경계에서 검증할 큐를 만든다."""
    instance = JobQueue(tmp_path / "jobs.db", max_retries=3)
    instance.initialize()
    yield instance
    instance.close()


@pytest.fixture
def watcher(tmp_path: Path, queue: JobQueue) -> FolderWatcher:
    """startup recovery만 격리해서 실행할 watcher를 만든다."""
    return _make_watcher(tmp_path, queue)


def _make_watcher(
    base_dir: Path,
    queue: JobQueue,
    *,
    audio_input_dir: str = "audio_input",
    quarantine_dir: str = "audio_quarantine",
) -> FolderWatcher:
    """raw lexical 경로 계약을 사용하는 watcher를 만든다."""
    watch_dir = base_dir / audio_input_dir
    quarantine_path = base_dir / quarantine_dir
    watch_dir.mkdir(parents=True, exist_ok=True)
    quarantine_path.mkdir(parents=True, exist_ok=True)

    config = MagicMock()
    config.paths.base_dir = base_dir
    config.paths.audio_input_dir = audio_input_dir
    config.paths.audio_quarantine_subdir = quarantine_dir
    config.paths.checkpoints_dir = "checkpoints"
    config.paths.outputs_dir = "outputs"
    config.paths.resolved_base_dir = base_dir
    config.paths.resolved_audio_input_dir = watch_dir
    config.paths.resolved_audio_quarantine_dir = quarantine_path
    config.paths.resolved_checkpoints_dir = base_dir / "checkpoints"
    config.paths.resolved_outputs_dir = base_dir / "outputs"
    config.audio.supported_input_formats = ["wav"]
    config.audio_quality.enabled = False
    config.watcher.debounce_seconds = 0.01
    config.watcher.check_interval_seconds = 0.01
    config.watcher.file_ready_timeout_seconds = 0.1
    config.watcher.excluded_subdirs = ["audio_quarantine"]

    instance = FolderWatcher(AsyncJobQueue(queue), config=config)
    instance._probe_writable_open = AsyncMock(return_value=_OpenWriterState.CLEAR)
    return instance


def _identity(path: Path) -> SourceIdentity:
    """ctime을 제외한 복구용 source identity를 반환한다."""
    file_stat = path.stat(follow_symlinks=False)
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _claim_method(queue: JobQueue) -> Callable[..., Any]:
    """RED 단계에서도 나머지 테스트가 수집되도록 예정 API를 지연 조회한다."""
    method = getattr(queue, "claim_for_audio_rejection", None)
    assert callable(method), "JobQueue.claim_for_audio_rejection API가 필요합니다"
    return method


def _claim_parser() -> Callable[[str], Any]:
    """예정된 strict payload parser를 지연 조회한다."""
    parser = getattr(job_queue_module, "parse_audio_rejection_claim", None)
    assert callable(parser), "parse_audio_rejection_claim API가 필요합니다"
    return parser


def _claim_payload(
    *,
    token: str,
    source: Path,
    source_identity: SourceIdentity,
    destination: Path,
    original_status: str = "recorded",
    original_requested_action: str = "",
) -> str:
    """복구 경로의 fail-closed 동작을 검증할 strict v1 payload를 만든다."""
    return json.dumps(
        {
            "v": 1,
            "kind": "audio_rejection_claim",
            "original_status": original_status,
            "original_requested_action": original_requested_action,
            "token": token,
            "source_path": str(source),
            "source_dev": source_identity[0],
            "source_ino": source_identity[1],
            "source_size": source_identity[2],
            "source_mtime_ns": source_identity[3],
            "quarantine_path": str(destination),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _force_claim_payload(queue: JobQueue, job_id: int, requested_action: str) -> None:
    """손상·위조 payload의 startup fail-closed 동작을 위해 DB row를 seed한다."""
    conn = queue._ensure_connection()
    conn.execute(
        "UPDATE jobs SET status = ?, requested_action = ? WHERE id = ?",
        (JobStatus.RECORDING.value, requested_action, job_id),
    )
    conn.commit()


def _media_invalid_result() -> AudioQualityResult:
    """격리 가능한 짧은 미디어 결과를 반환한다."""
    return AudioQualityResult(
        status=AudioQualityStatus.REJECT,
        mean_volume_db=-20.0,
        duration_seconds=1.0,
        reason="decoded duration < 30 seconds",
        failure_kind=AudioFailureKind.MEDIA_INVALID,
    )


def _recover_method(watcher: FolderWatcher) -> Callable[..., Any]:
    """watcher startup recovery API를 지연 조회한다."""
    method = getattr(watcher, "recover_audio_rejection_claims", None)
    assert callable(method), "FolderWatcher.recover_audio_rejection_claims API가 필요합니다"
    return method


def _claim_rejection(
    queue: JobQueue,
    *,
    job_id: int,
    token: str,
    source: Path,
    destination: Path,
) -> Any:
    """확정된 keyword-only claim 계약으로 media rejection을 예약한다."""
    return _claim_method(queue)(
        job_id,
        token,
        source_path=str(source),
        source_identity=_identity(source),
        quarantine_path=str(destination),
    )


def _new_recorded_claim(
    queue: JobQueue,
    *,
    tmp_path: Path,
    meeting_id: str,
    token: str,
    payload: bytes = b"confirmed media invalid",
) -> tuple[int, Path, Path, SourceIdentity, str]:
    """recorded row와 결정적 quarantine 목적지를 claim 상태로 만든다."""
    source = tmp_path / "audio_input" / f"{meeting_id}.wav"
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(payload)
    destination = tmp_path / "audio_quarantine" / f"{meeting_id}_{token}.wav"
    destination.parent.mkdir(exist_ok=True)
    job_id = queue.add_job(meeting_id, str(source), initial_status="recorded")
    expected_identity = _identity(source)
    claimed = _claim_rejection(
        queue,
        job_id=job_id,
        token=token,
        source=source,
        destination=destination,
    )
    return (
        job_id,
        source,
        destination,
        expected_identity,
        claimed.requested_action,
    )


def test_audio_rejection_claim은_hold의_original_intent와_exact_paths를_보존(
    queue: JobQueue,
    tmp_path: Path,
) -> None:
    """queued hold를 recording claim으로 CAS하며 recovery 입력을 모두 직렬화한다."""
    source = tmp_path / "audio_input" / "queued-origin.wav"
    source.parent.mkdir()
    source.write_bytes(b"short but confirmed invalid")
    destination = tmp_path / "audio_quarantine" / "queued-origin_reject-token-1.wav"

    job_id = queue.add_job("queued-origin", str(source), initial_status="recorded")
    queue.queue_job(job_id, requested_action="full")
    queue.hold_job_for_audio_admission(job_id, "admission-hold-1")
    expected_identity = _identity(source)

    claimed = _claim_rejection(
        queue,
        job_id=job_id,
        token="reject-token-1",
        source=source,
        destination=destination,
    )

    assert claimed.status == "recording"
    assert claimed.error_message == ""
    raw = json.loads(claimed.requested_action)
    assert raw == {
        "v": 1,
        "kind": "audio_rejection_claim",
        "original_status": "queued",
        "original_requested_action": "full",
        "token": "reject-token-1",
        "source_path": str(source),
        "source_dev": expected_identity[0],
        "source_ino": expected_identity[1],
        "source_size": expected_identity[2],
        "source_mtime_ns": expected_identity[3],
        "quarantine_path": str(destination),
    }

    parsed = _claim_parser()(claimed.requested_action)
    assert parsed is not None
    assert parsed.token == "reject-token-1"
    assert parsed.original_status == "queued"
    assert parsed.original_requested_action == "full"
    assert parsed.source_path == str(source)
    assert parsed.source_identity == expected_identity
    assert parsed.quarantine_path == str(destination)


def test_audio_rejection_finalize는_token_CAS로만_row를_삭제(
    queue: JobQueue,
    tmp_path: Path,
) -> None:
    """stale token은 claim을 보존하고 정확한 token만 terminal delete한다."""
    job_id, _source, _destination, _identity_value, requested_action = _new_recorded_claim(
        queue,
        tmp_path=tmp_path,
        meeting_id="finalize-cas",
        token="reject-finalize",
    )
    finalize = getattr(queue, "finalize_audio_rejection", None)
    assert callable(finalize), "JobQueue.finalize_audio_rejection API가 필요합니다"

    with pytest.raises(JobQueueError):
        finalize(job_id, "stale-token")

    preserved = queue.get_job(job_id)
    assert preserved.status == "recording"
    assert preserved.requested_action == requested_action

    finalize(job_id, "reject-finalize")
    assert queue.get_job_by_meeting_id("finalize-cas") is None


def test_audio_rejection_claim은_너무긴_token을_거부(
    queue: JobQueue,
    tmp_path: Path,
) -> None:
    """경로와 DB payload에 들어가는 token은 128자를 넘길 수 없다."""
    source = tmp_path / "audio_input" / "long-token.wav"
    source.parent.mkdir()
    source.write_bytes(b"invalid")
    job_id = queue.add_job("long-token", str(source), initial_status="recorded")
    token = "x" * 129
    destination = tmp_path / "audio_quarantine" / f"long-token_{token}.wav"

    with pytest.raises(JobQueueError):
        _claim_rejection(
            queue,
            job_id=job_id,
            token=token,
            source=source,
            destination=destination,
        )

    assert queue.get_job(job_id).status == "recorded"


def test_audio_rejection_claim은_job_audio_path불일치와_completed를_거부(
    queue: JobQueue,
    tmp_path: Path,
) -> None:
    """DB가 소유하지 않은 경로와 완료 산출물 row는 claim할 수 없다."""
    source = tmp_path / "audio_input" / "owned.wav"
    foreign = tmp_path / "audio_input" / "foreign.wav"
    source.parent.mkdir()
    source.write_bytes(b"owned invalid audio")
    foreign.write_bytes(b"foreign audio")
    destination = tmp_path / "audio_quarantine" / "owned_token.wav"
    destination.parent.mkdir()
    recorded_id = queue.add_job("owned", str(source), initial_status="recorded")
    completed_id = queue.add_job("completed", str(source), initial_status="completed")

    with pytest.raises(JobQueueError):
        _claim_method(queue)(
            recorded_id,
            "mismatched-source",
            source_path=str(foreign),
            source_identity=_identity(foreign),
            quarantine_path=str(destination),
        )
    with pytest.raises(JobQueueError):
        _claim_method(queue)(
            completed_id,
            "completed-row",
            source_path=str(source),
            source_identity=_identity(source),
            quarantine_path=str(destination),
        )

    assert queue.get_job(recorded_id).status == "recorded"
    assert queue.get_job(completed_id).status == "completed"
    assert source.read_bytes() == b"owned invalid audio"
    assert foreign.read_bytes() == b"foreign audio"


def test_audio_rejection_claim_CAS는_동시호출중_하나만_성공(
    queue: JobQueue,
    tmp_path: Path,
) -> None:
    """서로 다른 connection의 동시 claim은 정확히 한 payload만 소유한다."""
    source = tmp_path / "audio_input" / "concurrent.wav"
    source.parent.mkdir()
    source.write_bytes(b"confirmed invalid")
    job_id = queue.add_job("concurrent", str(source), initial_status="recorded")
    second = JobQueue(queue.db_path, max_retries=3)
    second.initialize()
    barrier = threading.Barrier(2)

    def _attempt(instance: JobQueue, token: str) -> tuple[str, str]:
        barrier.wait(timeout=5.0)
        try:
            result = _claim_method(instance)(
                job_id,
                token,
                source_path=str(source),
                source_identity=_identity(source),
                quarantine_path=str(tmp_path / "audio_quarantine" / f"concurrent_{token}.wav"),
            )
        except (AssertionError, JobQueueError) as exc:
            return ("error", str(exc))
        return ("ok", result.requested_action)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(_attempt, queue, "claim-a")
            second_future = executor.submit(_attempt, second, "claim-b")
            results = [first_future.result(timeout=10), second_future.result(timeout=10)]
    finally:
        second.close()

    assert sorted(result[0] for result in results) == ["error", "ok"]
    winner = queue.get_job(job_id)
    assert winner.status == "recording"
    parsed = _claim_parser()(winner.requested_action)
    assert parsed is not None
    assert parsed.token in {"claim-a", "claim-b"}


def test_audio_rejection_finalize는_SELECT후_payload교체를_삭제하지_않음(
    queue: JobQueue,
    tmp_path: Path,
) -> None:
    """같은 token의 ABA라도 DELETE는 읽었던 exact payload를 CAS한다."""
    job_id, source, destination, source_identity, requested_action = _new_recorded_claim(
        queue,
        tmp_path=tmp_path,
        meeting_id="payload-cas",
        token="same-token",
    )
    replacement = _claim_payload(
        token="same-token",
        source=source,
        source_identity=source_identity,
        destination=destination.with_name("replacement-destination.wav"),
    )
    real_connection = queue._ensure_connection()

    class _PayloadSwapConnection:
        """DELETE 직전 같은 token의 다른 payload를 주입하는 connection proxy."""

        def __init__(self) -> None:
            self.mutated = False

        def execute(self, sql: str, parameters: Any = ()) -> Any:
            if not self.mutated and sql.lstrip().upper().startswith("DELETE FROM JOBS"):
                real_connection.execute(
                    "UPDATE jobs SET requested_action = ? WHERE id = ?",
                    (replacement, job_id),
                )
                self.mutated = True
            return real_connection.execute(sql, parameters)

        def __getattr__(self, name: str) -> Any:
            return getattr(real_connection, name)

    proxy = _PayloadSwapConnection()
    queue._local.conn = proxy
    try:
        finalize = getattr(queue, "finalize_audio_rejection", None)
        assert callable(finalize), "JobQueue.finalize_audio_rejection API가 필요합니다"
        with pytest.raises(JobQueueError):
            finalize(job_id, "same-token")
    finally:
        queue._local.conn = real_connection

    preserved = queue.get_job(job_id)
    assert proxy.mutated is True
    assert preserved.status == "recording"
    assert preserved.requested_action == replacement
    assert preserved.requested_action != requested_action


@pytest.mark.asyncio
async def test_audio_rejection_claim은_DB재접속후_새watcher가_복구(
    queue: JobQueue,
    tmp_path: Path,
) -> None:
    """claim은 실제 connection 종료와 새 프로세스 상당 경계를 견딘다."""
    _job_id, source, destination, expected_identity, _requested_action = _new_recorded_claim(
        queue,
        tmp_path=tmp_path,
        meeting_id="reopened",
        token="restart-token",
        payload=b"survives sqlite reconnect",
    )
    db_path = queue.db_path
    queue.close()
    reopened = JobQueue(db_path, max_retries=3)
    reopened.initialize()
    restarted_watcher = _make_watcher(tmp_path, reopened)
    try:
        await _recover_method(restarted_watcher)()
        assert reopened.get_job_by_meeting_id("reopened") is None
    finally:
        reopened.close()

    assert not source.exists()
    assert destination.read_bytes() == b"survives sqlite reconnect"
    assert _identity(destination) == expected_identity


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", [None, "claim", "move", "finalize"])
async def test_live_MEDIA_INVALID는_claim_move_finalize순서와_crash경계를_보존(
    queue: JobQueue,
    watcher: FolderWatcher,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_at: str | None,
) -> None:
    """live reject는 DB claim 전 이동하거나 generic delete로 끝내지 않는다."""
    meeting_id = f"live-{failure_at or 'success'}"
    source = tmp_path / "audio_input" / f"{meeting_id}.wav"
    payload = f"invalid-{failure_at or 'success'}".encode()
    source.write_bytes(payload)
    job_id = queue.add_job(meeting_id, str(source), initial_status="recorded")
    events: list[str] = []
    original_claim = _claim_method(queue)
    original_finalize = getattr(queue, "finalize_audio_rejection", None)
    assert callable(original_finalize), "JobQueue.finalize_audio_rejection API가 필요합니다"

    def _claim(*args: Any, **kwargs: Any) -> Any:
        events.append("claim")
        if failure_at == "claim":
            raise JobQueueError("injected claim failure")
        claimed = original_claim(*args, **kwargs)
        assert claimed.status == "recording"
        assert _claim_parser()(claimed.requested_action) is not None
        return claimed

    async def _move(*_args: Any, **_kwargs: Any) -> bool:
        events.append("move")
        current = queue.get_job(job_id)
        claim = _claim_parser()(current.requested_action)
        assert current.status == "recording"
        assert claim is not None, "filesystem move보다 durable DB claim이 먼저여야 합니다"
        if failure_at == "move":
            return False
        destination = Path(claim.quarantine_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, destination, follow_symlinks=False)
        source.unlink()
        return True

    def _finalize(*args: Any, **kwargs: Any) -> Any:
        events.append("finalize")
        current = queue.get_job(job_id)
        claim = _claim_parser()(current.requested_action)
        assert claim is not None
        assert not source.exists()
        assert Path(claim.quarantine_path).read_bytes() == payload
        if failure_at == "finalize":
            raise JobQueueError("injected finalize failure")
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(queue, "claim_for_audio_rejection", _claim)
    monkeypatch.setattr(queue, "finalize_audio_rejection", _finalize)
    monkeypatch.setattr(watcher, "_move_to_quality_quarantine", _move)
    monkeypatch.setattr(watcher, "_wait_for_stable_size", AsyncMock(return_value=True))
    monkeypatch.setattr(
        watcher, "_audio_validator", MagicMock(return_value=_media_invalid_result())
    )
    monkeypatch.setattr(
        watcher._job_queue,
        "delete_job",
        AsyncMock(side_effect=AssertionError("generic delete_job은 사용하면 안 됩니다")),
    )
    monkeypatch.setattr(
        "core.watcher.uuid.uuid4",
        lambda: SimpleNamespace(hex=f"reject-{failure_at or 'success'}"),
    )

    await watcher._handle_new_file(source)

    row = queue.get_job_by_meeting_id(meeting_id)
    if failure_at == "claim":
        assert events == ["claim"]
        assert row is not None and row.status == "recorded"
        assert source.read_bytes() == payload
    elif failure_at == "move":
        assert events == ["claim", "move"]
        assert row is not None and row.status == "recording"
        assert _claim_parser()(row.requested_action) is not None
        assert source.read_bytes() == payload
    elif failure_at == "finalize":
        assert events == ["claim", "move", "finalize"]
        assert row is not None and row.status == "recording"
        claim = _claim_parser()(row.requested_action)
        assert claim is not None
        assert not source.exists()
        assert Path(claim.quarantine_path).read_bytes() == payload
    else:
        assert events == ["claim", "move", "finalize"]
        assert row is None
        assert not source.exists()

    watcher._job_queue.delete_job.assert_not_called()


@pytest.mark.asyncio
async def test_live_reject검증중_산출물생성race는_claim_move_delete를_차단(
    queue: JobQueue,
    watcher: FolderWatcher,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """품질 검증과 claim 사이 생긴 사용자 산출물은 completed와 동일하게 보존한다."""
    meeting_id = "artifact-race"
    source = tmp_path / "audio_input" / f"{meeting_id}.wav"
    source.write_bytes(b"media invalid but artifact appeared")
    job_id = queue.add_job(meeting_id, str(source), initial_status="recorded")
    artifact = tmp_path / "checkpoints" / meeting_id / "transcribe.json"

    def _validator(*_args: Any, **_kwargs: Any) -> AudioQualityResult:
        artifact.parent.mkdir(parents=True)
        artifact.write_text('{"segments": []}', encoding="utf-8")
        return _media_invalid_result()

    claim = MagicMock(side_effect=AssertionError("산출물 생성 뒤 claim하면 안 됩니다"))
    monkeypatch.setattr(queue, "claim_for_audio_rejection", claim, raising=False)
    monkeypatch.setattr(watcher, "_audio_validator", _validator)
    monkeypatch.setattr(watcher, "_wait_for_stable_size", AsyncMock(return_value=True))
    move = AsyncMock(side_effect=AssertionError("산출물 생성 뒤 이동하면 안 됩니다"))
    monkeypatch.setattr(watcher, "_move_to_quality_quarantine", move)
    delete = AsyncMock(side_effect=AssertionError("산출물 생성 뒤 삭제하면 안 됩니다"))
    monkeypatch.setattr(watcher._job_queue, "delete_job", delete)

    await watcher._handle_new_file(source)

    preserved = queue.get_job(job_id)
    assert preserved.status == "recorded"
    assert source.read_bytes() == b"media invalid but artifact appeared"
    assert artifact.read_text(encoding="utf-8") == '{"segments": []}'
    claim.assert_not_called()
    move.assert_not_called()
    delete.assert_not_called()


@pytest.mark.asyncio
async def test_startup_source_only_claim은_exact_destination으로_이동후_finalize(
    queue: JobQueue,
    watcher: FolderWatcher,
    tmp_path: Path,
) -> None:
    """claim 뒤 이동 전 crash는 같은 inode를 정확한 예약 경로로 복구한다."""
    payload = b"source survived claim crash"
    job_id, source, destination, expected_identity, _requested_action = _new_recorded_claim(
        queue,
        tmp_path=tmp_path,
        meeting_id="source-only",
        token="reject-source-only",
        payload=payload,
    )

    await _recover_method(watcher)()

    assert not source.exists()
    assert destination.read_bytes() == payload
    assert _identity(destination) == expected_identity
    assert queue.get_job_by_meeting_id("source-only") is None
    assert job_id > 0


@pytest.mark.asyncio
async def test_startup_quarantine_only_claim은_기존격리를_검증후_finalize(
    queue: JobQueue,
    watcher: FolderWatcher,
    tmp_path: Path,
) -> None:
    """이동 뒤 DB delete 전 crash는 목적지를 다시 옮기지 않고 row만 정리한다."""
    payload = b"quarantine survived delete crash"
    _job_id, source, destination, expected_identity, _requested_action = _new_recorded_claim(
        queue,
        tmp_path=tmp_path,
        meeting_id="quarantine-only",
        token="reject-quarantine-only",
        payload=payload,
    )
    os.link(source, destination, follow_symlinks=False)
    source.unlink()
    # hardlink 생성은 ctime을 바꾸므로 durable identity는 의도대로 ctime을 제외한다.
    assert _identity(destination) == expected_identity

    assert await watcher.scan_existing() == []

    assert not source.exists()
    assert destination.read_bytes() == payload
    assert queue.get_job_by_meeting_id("quarantine-only") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_state", ["both", "neither", "source_mismatch"])
async def test_startup_ambiguous_claim은_파일과_row를_보존(
    queue: JobQueue,
    watcher: FolderWatcher,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    crash_state: str,
) -> None:
    """양쪽/양쪽 없음/identity 불일치는 추측 정리 없이 다음 startup에 남긴다."""
    original = b"original invalid audio"
    foreign = b"foreign file must never be overwritten"
    job_id, source, destination, _expected_identity, requested_action = _new_recorded_claim(
        queue,
        tmp_path=tmp_path,
        meeting_id=f"ambiguous-{crash_state}",
        token=f"reject-{crash_state}",
        payload=original,
    )

    if crash_state == "both":
        destination.write_bytes(foreign)
    elif crash_state == "neither":
        source.unlink()
    else:
        source.unlink()
        source.write_bytes(b"replacement with a different inode")

    caplog.set_level(logging.WARNING, logger="core.watcher")
    await _recover_method(watcher)()

    preserved = queue.get_job(job_id)
    assert preserved.status == "recording"
    assert preserved.requested_action == requested_action
    assert any(preserved.meeting_id in record.getMessage() for record in caplog.records)

    if crash_state == "both":
        assert source.read_bytes() == original
        assert destination.read_bytes() == foreign
        assert sorted(path.name for path in destination.parent.glob("*.wav")) == [destination.name]
    elif crash_state == "neither":
        assert not source.exists()
        assert not destination.exists()
    else:
        assert source.read_bytes() == b"replacement with a different inode"
        assert not destination.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash_state",
    [
        "quarantine_mismatch",
        "both_same_inode",
        "source_mismatch_quarantine_match",
        "both_mismatch",
    ],
)
async def test_startup_identity_matrix의_모호한_나머지상태도_모두보존(
    queue: JobQueue,
    watcher: FolderWatcher,
    tmp_path: Path,
    crash_state: str,
) -> None:
    """source-match/q-absent와 source-absent/q-match 외에는 추측하지 않는다."""
    original = b"original identity"
    job_id, source, destination, _expected_identity, requested_action = _new_recorded_claim(
        queue,
        tmp_path=tmp_path,
        meeting_id=f"matrix-{crash_state}",
        token=f"matrix-{crash_state}",
        payload=original,
    )

    if crash_state == "quarantine_mismatch":
        os.link(source, destination, follow_symlinks=False)
        source.unlink()
        destination.write_bytes(b"quarantine changed size and mtime")
    elif crash_state == "both_same_inode":
        os.link(source, destination, follow_symlinks=False)
    elif crash_state == "source_mismatch_quarantine_match":
        os.link(source, destination, follow_symlinks=False)
        source.unlink()
        source.write_bytes(b"replacement source")
    else:
        source.unlink()
        source.write_bytes(b"replacement source")
        destination.write_bytes(b"foreign quarantine")

    await _recover_method(watcher)()

    preserved = queue.get_job(job_id)
    assert preserved.status == "recording"
    assert preserved.requested_action == requested_action
    if crash_state == "quarantine_mismatch":
        assert not source.exists()
        assert destination.read_bytes() == b"quarantine changed size and mtime"
    elif crash_state == "both_same_inode":
        assert source.read_bytes() == original
        assert destination.read_bytes() == original
        assert _identity(source) == _identity(destination)
    elif crash_state == "source_mismatch_quarantine_match":
        assert source.read_bytes() == b"replacement source"
        assert destination.read_bytes() == original
    else:
        assert source.read_bytes() == b"replacement source"
        assert destination.read_bytes() == b"foreign quarantine"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_case",
    [
        "source_outside",
        "source_dotdot",
        "quarantine_outside",
        "source_final_symlink",
        "quarantine_final_symlink",
        "intermediate_symlink",
    ],
)
async def test_startup_위조claim경로는_외부target에_접근하지않고_row를_보존(
    queue: JobQueue,
    tmp_path: Path,
    unsafe_case: str,
) -> None:
    """복구 payload는 raw configured root의 no-follow direct child만 허용한다."""
    base = tmp_path / "base"
    current_watcher = _make_watcher(base, queue)
    input_dir = base / "audio_input"
    quarantine_dir = base / "audio_quarantine"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_target = outside / "target.wav"
    outside_target.write_bytes(b"EXTERNAL-SENTINEL")
    meeting_id = f"unsafe-{unsafe_case}"
    source = input_dir / f"{meeting_id}.wav"
    destination = quarantine_dir / f"{meeting_id}_token.wav"
    source_identity: SourceIdentity

    if unsafe_case == "source_outside":
        source = outside_target
        source_identity = _identity(outside_target)
    elif unsafe_case == "source_dotdot":
        source = input_dir / ".." / ".." / "outside" / "target.wav"
        source_identity = _identity(outside_target)
    elif unsafe_case == "quarantine_outside":
        source.write_bytes(b"internal source")
        source_identity = _identity(source)
        destination = outside / "outside-quarantine.wav"
    elif unsafe_case == "source_final_symlink":
        source.symlink_to(outside_target)
        source_identity = _identity(outside_target)
    elif unsafe_case == "quarantine_final_symlink":
        source.write_bytes(b"internal source")
        source_identity = _identity(source)
        outside_hardlink = outside / "outside-hardlink.wav"
        os.link(source, outside_hardlink, follow_symlinks=False)
        source.unlink()
        destination.symlink_to(outside_hardlink)
    else:
        safe_parent = base / "safe"
        safe_input = safe_parent / "audio_input"
        safe_quarantine = safe_parent / "audio_quarantine"
        safe_input.mkdir(parents=True)
        safe_quarantine.mkdir()
        outside_tree = outside / "tree"
        outside_input = outside_tree / "audio_input"
        outside_quarantine = outside_tree / "audio_quarantine"
        outside_input.mkdir(parents=True)
        outside_quarantine.mkdir()
        source = safe_input / f"{meeting_id}.wav"
        source.write_bytes(b"trusted source")
        source_identity = _identity(source)
        destination = safe_quarantine / f"{meeting_id}_token.wav"
        safe_parent.rename(base / "safe-original")
        safe_parent.symlink_to(outside_tree, target_is_directory=True)
        current_watcher = _make_watcher(
            base,
            queue,
            audio_input_dir="safe/audio_input",
            quarantine_dir="safe/audio_quarantine",
        )
        # watcher construction must never create/mutate target entries beyond
        # the already-present directories; sentinel catches recovery access.
        external_same_name = outside_input / source.name
        external_same_name.write_bytes(b"EXTERNAL-INTERMEDIATE")

    job_id = queue.add_job(meeting_id, str(source), initial_status="recorded")
    requested_action = _claim_payload(
        token=f"unsafe-{unsafe_case}",
        source=source,
        source_identity=source_identity,
        destination=destination,
    )
    _force_claim_payload(queue, job_id, requested_action)

    await _recover_method(current_watcher)()

    preserved = queue.get_job(job_id)
    assert preserved.status == "recording"
    assert preserved.requested_action == requested_action
    assert outside_target.read_bytes() == b"EXTERNAL-SENTINEL"
    if unsafe_case == "quarantine_outside":
        assert source.read_bytes() == b"internal source"
        assert not destination.exists()
    elif unsafe_case == "source_final_symlink":
        assert source.is_symlink()
    elif unsafe_case == "quarantine_final_symlink":
        assert destination.is_symlink()
        assert destination.read_bytes() == b"internal source"
    elif unsafe_case == "intermediate_symlink":
        assert (base / "safe-original" / "audio_input" / source.name).read_bytes() == (
            b"trusted source"
        )
        assert (outside / "tree" / "audio_input" / source.name).read_bytes() == (
            b"EXTERNAL-INTERMEDIATE"
        )
        assert list((outside / "tree" / "audio_quarantine").iterdir()) == []


@pytest.mark.asyncio
async def test_startup_recovery중_intermediate_directory교체도_외부부작용없이_실패(
    queue: JobQueue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """component open 직후 lexical parent가 symlink로 바뀌어도 fd chain을 이탈하지 않는다."""
    base = tmp_path / "race-base"
    safe_parent = base / "safe"
    current_watcher = _make_watcher(
        base,
        queue,
        audio_input_dir="safe/audio_input",
        quarantine_dir="safe/audio_quarantine",
    )
    source = safe_parent / "audio_input" / "swap.wav"
    source.write_bytes(b"TRUSTED-SOURCE")
    destination = safe_parent / "audio_quarantine" / "swap_token.wav"
    job_id = queue.add_job("swap", str(source), initial_status="recorded")
    requested_action = _claim_payload(
        token="swap-token",
        source=source,
        source_identity=_identity(source),
        destination=destination,
    )
    _force_claim_payload(queue, job_id, requested_action)

    external = tmp_path / "race-external"
    (external / "audio_input").mkdir(parents=True)
    (external / "audio_quarantine").mkdir()
    external_source = external / "audio_input" / "swap.wav"
    external_source.write_bytes(b"EXTERNAL-SOURCE")
    displaced = base / "safe-original"
    real_open = os.open
    swapped = False

    def _racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if not swapped and os.fspath(path) == "safe" and dir_fd is not None:
            safe_parent.rename(displaced)
            safe_parent.symlink_to(external, target_is_directory=True)
            swapped = True
        return descriptor

    monkeypatch.setattr(os, "open", _racing_open)
    await _recover_method(current_watcher)()

    preserved = queue.get_job(job_id)
    assert swapped is True
    assert preserved.status == "recording"
    assert preserved.requested_action == requested_action
    assert (displaced / "audio_input" / "swap.wav").read_bytes() == b"TRUSTED-SOURCE"
    assert external_source.read_bytes() == b"EXTERNAL-SOURCE"
    assert list((external / "audio_quarantine").iterdir()) == []


@pytest.mark.asyncio
async def test_scan_existing은_audio_rejection_recovery를_legacy_audit보다_먼저실행(
    queue: JobQueue,
    watcher: FolderWatcher,
    tmp_path: Path,
) -> None:
    """startup scan은 recording claim을 단순 중복으로 건너뛰기 전에 복구한다."""
    payload = b"claimed source pending exact quarantine"
    _job_id, source, destination, _expected_identity, _requested_action = _new_recorded_claim(
        queue,
        tmp_path=tmp_path,
        meeting_id="recovery-before-audit",
        token="reject-before-audit",
        payload=payload,
    )
    validator = MagicMock(side_effect=AssertionError("claim source를 재검증하면 안 됩니다"))
    watcher._audio_validator = validator

    assert await watcher.scan_existing() == []

    assert not source.exists()
    assert destination.read_bytes() == payload
    assert queue.get_job_by_meeting_id("recovery-before-audit") is None
    validator.assert_not_called()


@pytest.mark.asyncio
async def test_scan_existing은_첫claim오류뒤_다음복구와_일반등록을_계속함(
    queue: JobQueue,
    watcher: FolderWatcher,
    tmp_path: Path,
) -> None:
    """ambiguous/malformed row 하나가 전체 startup scan을 중단하지 않는다."""
    ambiguous_id, ambiguous_source, ambiguous_destination, _identity_value, action = (
        _new_recorded_claim(
            queue,
            tmp_path=tmp_path,
            meeting_id="00-ambiguous",
            token="ambiguous-token",
            payload=b"ambiguous source",
        )
    )
    ambiguous_destination.write_bytes(b"foreign destination")

    malformed_source = tmp_path / "audio_input" / "01-malformed.wav"
    malformed_source.write_bytes(b"malformed claim source")
    malformed_id = queue.add_job(
        "01-malformed",
        str(malformed_source),
        initial_status="recorded",
    )
    _force_claim_payload(
        queue,
        malformed_id,
        '{"kind":"audio_rejection_claim","v":1,"unexpected":true}',
    )

    _valid_id, valid_source, valid_destination, _valid_identity, _valid_action = (
        _new_recorded_claim(
            queue,
            tmp_path=tmp_path,
            meeting_id="02-valid",
            token="valid-token",
            payload=b"valid recovery source",
        )
    )
    ordinary_source = tmp_path / "audio_input" / "03-ordinary.wav"
    ordinary_source.write_bytes(b"ordinary recording")

    registered = await watcher.scan_existing()

    ambiguous = queue.get_job(ambiguous_id)
    malformed = queue.get_job(malformed_id)
    ordinary = queue.get_job_by_meeting_id("03-ordinary")
    assert ambiguous.status == "recording"
    assert ambiguous.requested_action == action
    assert ambiguous_source.read_bytes() == b"ambiguous source"
    assert ambiguous_destination.read_bytes() == b"foreign destination"
    assert malformed.status == "recording"
    assert malformed_source.read_bytes() == b"malformed claim source"
    assert queue.get_job_by_meeting_id("02-valid") is None
    assert not valid_source.exists()
    assert valid_destination.read_bytes() == b"valid recovery source"
    assert ordinary is not None and ordinary.status == "recorded"
    assert registered == [ordinary.id]


@pytest.mark.asyncio
async def test_scan_existing은_watch_root목록실패보다_quarantine_only복구가_먼저(
    queue: JobQueue,
    watcher: FolderWatcher,
    tmp_path: Path,
) -> None:
    """입력 root가 사라져도 이미 끝난 격리 transaction은 DB finalize한다."""
    _job_id, source, destination, expected_identity, _requested_action = _new_recorded_claim(
        queue,
        tmp_path=tmp_path,
        meeting_id="missing-watch-root",
        token="missing-root-token",
        payload=b"already quarantined",
    )
    os.link(source, destination, follow_symlinks=False)
    source.unlink()
    assert _identity(destination) == expected_identity
    (tmp_path / "audio_input").rmdir()

    assert await watcher.scan_existing() == []

    assert destination.read_bytes() == b"already quarantined"
    assert queue.get_job_by_meeting_id("missing-watch-root") is None


@pytest.mark.asyncio
async def test_startup_claim후_산출물이생겼으면_source_row_artifact를_모두보존(
    queue: JobQueue,
    watcher: FolderWatcher,
    tmp_path: Path,
) -> None:
    """claim 직후 생긴 transcript/output은 자동 격리보다 사용자 데이터 보존이 우선이다."""
    job_id, source, destination, _expected_identity, requested_action = _new_recorded_claim(
        queue,
        tmp_path=tmp_path,
        meeting_id="artifact-after-claim",
        token="artifact-token",
        payload=b"claimed source with artifact",
    )
    artifact = tmp_path / "checkpoints" / "artifact-after-claim" / "transcribe.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"segments": [{"text": "KEEP"}]}', encoding="utf-8")

    await _recover_method(watcher)()

    preserved = queue.get_job(job_id)
    assert preserved.status == "recording"
    assert preserved.requested_action == requested_action
    assert source.read_bytes() == b"claimed source with artifact"
    assert not destination.exists()
    assert artifact.read_text(encoding="utf-8") == ('{"segments": [{"text": "KEEP"}]}')


@pytest.mark.parametrize(
    "mutation",
    [
        "relative_source",
        "nul_destination",
        "extra_key",
        "bool_identity",
        "negative_size",
        "float_mtime",
        "invalid_original_status",
    ],
)
def test_audio_rejection_parser는_malformed_path와_identity를_엄격거부(
    tmp_path: Path,
    mutation: str,
) -> None:
    """strict v1 parser는 모호한 경로·타입·schema를 recovery에 전달하지 않는다."""
    source = tmp_path / "audio_input" / "strict.wav"
    destination = tmp_path / "audio_quarantine" / "strict_token.wav"
    payload: dict[str, Any] = json.loads(
        _claim_payload(
            token="strict-token",
            source=source,
            source_identity=(1, 2, 3, 4),
            destination=destination,
        )
    )
    if mutation == "relative_source":
        payload["source_path"] = "audio_input/strict.wav"
    elif mutation == "nul_destination":
        payload["quarantine_path"] = f"{destination}\x00suffix"
    elif mutation == "extra_key":
        payload["unexpected"] = True
    elif mutation == "bool_identity":
        payload["source_dev"] = True
    elif mutation == "negative_size":
        payload["source_size"] = -1
    elif mutation == "float_mtime":
        payload["source_mtime_ns"] = 4.5
    else:
        payload["original_status"] = "completed"

    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    assert _claim_parser()(encoded) is None


@pytest.mark.asyncio
async def test_orchestrator는_watcher비활성시에도_audio_rejection_claim을_명시보존(
    queue: JobQueue,
    tmp_path: Path,
) -> None:
    """generic orphan recovery의 상태 집합이 바뀌어도 durable claim을 풀지 않는다."""
    rejection_id, _source, _destination, _identity_value, requested_action = _new_recorded_claim(
        queue,
        tmp_path=tmp_path,
        meeting_id="orchestrator-preserve",
        token="orchestrator-token",
    )
    ordinary_id = queue.add_job(
        "ordinary-orphan",
        str(tmp_path / "audio_input" / "ordinary-orphan.wav"),
        initial_status="transcribing",
    )
    async_queue = AsyncJobQueue(queue)
    pipeline = MagicMock()
    pipeline._config.paths.base_dir = tmp_path
    pipeline._config.paths.checkpoints_dir = "checkpoints"
    pipeline._config.paths.outputs_dir = "outputs"
    pipeline._config.paths.resolved_checkpoints_dir = tmp_path / "checkpoints"
    pipeline._config.paths.resolved_outputs_dir = tmp_path / "outputs"
    with patch("core.orchestrator.PerfStats.load", return_value=None):
        processor = JobProcessor(
            job_queue=async_queue,
            pipeline=pipeline,
            thermal_manager=MagicMock(),
            poll_interval=0.01,
        )

    parser = _claim_parser()
    with patch(
        "core.orchestrator.parse_audio_rejection_claim",
        create=True,
        wraps=parser,
    ) as explicit_parser:
        await processor._recover_orphaned_jobs()

    rejection = queue.get_job(rejection_id)
    ordinary = queue.get_job(ordinary_id)
    assert rejection.status == "recording"
    assert rejection.requested_action == requested_action
    assert ordinary.status == "queued"
    explicit_parser.assert_any_call(requested_action)
