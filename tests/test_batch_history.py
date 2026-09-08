"""일괄 접수 기록과 단계별 상태의 회귀 테스트."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.job_queue import JobQueue, JobQueueError
from core.meeting_progress import meeting_progress
from tests.test_routes_meetings_batch import _make_test_app, _setup_pipeline_mock


def test_receipt_and_queue_commit_or_rollback_together(tmp_path: Path) -> None:
    """큐 CAS 실패·중복 요청에는 접수 성공 내역이나 일부 큐잉을 남기지 않는다."""
    db = tmp_path / "jobs.db"
    queue = JobQueue(db)
    queue.initialize()
    one = queue.add_job("one", "one.wav", initial_status="recorded")
    two = queue.add_job("two", "two.wav", initial_status="failed")
    receipt = dict(request_id="r1", meeting_ids=["one"], candidates=[], queued=1)
    with pytest.raises(JobQueueError):
        queue.queue_jobs_atomically([one, two], "full", receipt=receipt)
    assert not queue.get_batch_receipts()
    assert queue.get_job(one).status == "recorded"
    queue.queue_jobs_atomically([one], "full", receipt=receipt)
    queue.record_batch_event("one", dict(status="failed", failed_step="correct"))
    # 뒤에 온 개별 재시도가 과거 일괄 실패 기록을 완료로 덮어쓰면 안 된다.
    queue.record_batch_event("one", dict(status="completed"))
    queue.close()
    queue = JobQueue(db)
    queue.initialize()
    saved = queue.get_batch_receipts("r1")[0]
    assert saved["queued"] == 1
    assert [e["status"] for e in saved["events"]] == ["failed"]
    assert queue.get_job(one).requested_action == "full"
    queue.close()


def test_review_identifies_bad_file_and_receipt_deduplicates(tmp_path: Path) -> None:
    """미리보기에서는 문제 파일을 선택 해제할 수 있고 실제 실패는 전체 무접수다."""
    app = _make_test_app(tmp_path)
    with TestClient(app) as client:
        _setup_pipeline_mock(app)
        queue = app.state.job_queue.queue
        audio = tmp_path / "good.wav"
        audio.write_bytes(b"test audio")
        queue.add_job("good", str(audio), initial_status="recorded")
        queue.add_job("bad", str(tmp_path / "missing.wav"), initial_status="recorded")
        body = dict(
            action="full", scope="selected", meeting_ids=["good", "bad"], request_id="bad-request"
        )
        review = client.post("/api/meetings/batch/review", json=body).json()
        assert (review["matched"], review["queued"], review["skipped"]) == (2, 1, 1)
        bad = next(c for c in review["candidates"] if c["meeting_id"] == "bad")
        assert bad["blocked"] and "SOURCE_BUSY" in bad["reason"]
        rejected = client.post("/api/meetings/batch", json=body)
        assert rejected.status_code == 409
        assert "bad" in rejected.json()["detail"]
        assert queue.get_job_by_meeting_id("good").status == "recorded"
        assert queue.get_batch_receipts("bad-request")[0]["queued"] == 0
        body.update(meeting_ids=["good"], request_id="good-request")
        accepted = client.post("/api/meetings/batch", json=body).json()
        repeated = client.post("/api/meetings/batch", json=body).json()
        assert accepted == repeated
        assert accepted["queued"] == 1
        body["meeting_ids"] = ["bad"]
        assert client.post("/api/meetings/batch", json=body).status_code == 409
        assert len(client.get("/api/batch-requests").json()["requests"]) == 2


@pytest.mark.parametrize("db_status", ["failed", "completed"])
def test_list_detail_and_review_use_same_failure_stage(tmp_path: Path, db_status: str) -> None:
    """부분 성공 표시가 목록·상세·일괄 확인에서 일치한다."""
    app = _make_test_app(tmp_path)
    with TestClient(app) as client:
        _setup_pipeline_mock(app)
        queue = app.state.job_queue.queue
        queue.add_job("partial", str(tmp_path / "audio.wav"), initial_status=db_status)
        cp = tmp_path / "checkpoints" / "partial"
        cp.mkdir(parents=True)
        (cp / "merge.json").write_text("{}")
        (cp / "pipeline_state.json").write_text(
            json.dumps(
                dict(
                    meeting_id="partial",
                    status="failed",
                    current_step="correct",
                    completed_steps=["convert", "transcribe", "diarize", "merge"],
                )
            )
        )
        detail = client.get("/api/meetings/partial").json()
        listed = client.get("/api/meetings").json()["meetings"][0]
        review = client.post(
            "/api/meetings/batch/review",
            json=dict(action="full", scope="selected", meeting_ids=["partial"]),
        ).json()["candidates"][0]
        for item in [detail, listed, review]:
            assert item["status"] == "failed"
            assert item["status_label"] == "전사 완료 · AI 교정 실패"
            assert item["retry_label"] == "교정 재시도"
            assert item["transcript_available"]
        assert not review["eligible"]


def test_restarted_background_receipt_is_interrupted(tmp_path: Path) -> None:
    """후처리 task가 사라진 앱 재시작을 접수 성공·실행 중으로 오인하지 않는다."""
    app = _make_test_app(tmp_path)
    with TestClient(app) as client:
        queue = app.state.job_queue.queue
        queue.save_batch_receipt(
            dict(
                request_id="before-restart",
                meeting_ids=["one"],
                candidates=[],
                queued=1,
                server_session="old-session",
                background_ids=["one"],
            )
        )
        receipt = client.get("/api/batch-requests").json()["requests"][0]
        assert receipt["events"][-1]["status"] == "interrupted"


def test_cancelled_queued_job_is_terminal_in_receipt(tmp_path: Path) -> None:
    """worker가 실행하지 않은 취소도 접수 내역에 남는다."""
    app = _make_test_app(tmp_path)
    with TestClient(app) as client:
        queue = app.state.job_queue.queue
        jid = queue.add_job("one", str(tmp_path / "one.wav"), initial_status="recorded")
        queue.queue_jobs_atomically(
            [jid],
            "full",
            receipt=dict(
                request_id="cancel",
                meeting_ids=["one"],
                candidates=[],
                queued=1,
            ),
        )
        assert client.post("/api/meetings/one/cancel").status_code == 200
        assert queue.get_batch_receipts("cancel")[0]["events"][-1]["status"] == "cancelled"


def test_progress_does_not_treat_skipped_or_stale_steps_as_success() -> None:
    """건너뛴 교정은 완료에 넣지 않고 재대기 상태에서는 과거 실패를 숨긴다."""
    state = dict(
        current_step="correct", completed_steps=["merge", "correct", {}], skipped_steps=["correct"]
    )
    assert meeting_progress("completed", state)["status_label"] == "전사 완료 · 교정·요약 대기"
    assert meeting_progress("queued", state)["failed_step"] == ""
    assert (
        meeting_progress("failed", dict(completed_steps=None, skipped_steps=None))["status_label"]
        == "처리 실패"
    )
