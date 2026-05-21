"""Durable Wiki backfill state tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.wiki.backfill_state import WikiBackfillStateStore


@dataclass
class _FakeBackfillError:
    meeting_id: str
    error_type: str
    message: str


@dataclass
class _FakeBackfillResult:
    total: int
    succeeded: int
    skipped: int
    failed: int
    errors: list[_FakeBackfillError]
    duration_seconds: float


def test_backfill_job_state를_sqlite에_저장하고_조회한다(tmp_path: Path) -> None:
    """DW-F01: 진행/완료/오류 상태가 durable DB에 보존된다."""
    store = WikiBackfillStateStore(tmp_path / "wiki")
    store.create_job(
        job_id="job1",
        started_at="2026-05-21T10:00:00",
        request={"since": "2026-05-01", "dry_run": False},
    )
    store.update_progress(
        job_id="job1",
        processed=1,
        total=2,
        current_meeting_id="1234abcd",
    )
    store.complete_job(
        job_id="job1",
        status="failed",
        finished_at="2026-05-21T10:01:00",
        result=_FakeBackfillResult(
            total=2,
            succeeded=1,
            skipped=0,
            failed=1,
            errors=[
                _FakeBackfillError(
                    meeting_id="deadbeef",
                    error_type="summary_missing",
                    message="요약 없음",
                )
            ],
            duration_seconds=60.0,
        ),
    )

    snapshot = store.get_job("job1")

    assert snapshot is not None
    assert snapshot.status == "failed"
    assert snapshot.total == 2
    assert snapshot.processed == 2
    assert snapshot.succeeded == 1
    assert snapshot.failed == 1
    assert snapshot.request["since"] == "2026-05-01"
    assert snapshot.errors[0]["meeting_id"] == "deadbeef"
    assert store.failed_meeting_ids("job1") == ["deadbeef"]


def test_running_job은_재시작_후_interrupted로_복구된다(tmp_path: Path) -> None:
    """DW-F02: running + finished_at 없음 상태는 interrupted로 노출된다."""
    root = tmp_path / "wiki"
    store = WikiBackfillStateStore(root)
    store.create_job(
        job_id="job2",
        started_at="2026-05-21T10:00:00",
        request={"meeting_ids": ["1234abcd"]},
    )
    store.update_progress(
        job_id="job2",
        processed=1,
        total=3,
        current_meeting_id="1234abcd",
    )

    restarted_store = WikiBackfillStateStore(root)
    snapshot = restarted_store.get_job("job2")

    assert snapshot is not None
    assert snapshot.status == "interrupted"
    assert snapshot.processed == 1
    assert snapshot.current_meeting_id == "1234abcd"


def test_cancel_job은_terminal_status를_durable하게_반영한다(tmp_path: Path) -> None:
    """DW-F05: cancel 요청은 DB status와 finished_at에 남는다."""
    store = WikiBackfillStateStore(tmp_path / "wiki")
    store.create_job(job_id="job3", started_at="2026-05-21T10:00:00", request={})

    store.cancel_job(job_id="job3", finished_at="2026-05-21T10:02:00")

    snapshot = store.get_job("job3")
    assert snapshot is not None
    assert snapshot.status == "cancelled"
    assert snapshot.finished_at == "2026-05-21T10:02:00"
