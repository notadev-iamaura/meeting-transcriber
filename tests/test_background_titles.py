"""AI 제목 백그라운드 접수·수동 변경 보존·복원 회귀 테스트."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from api.routers import meeting_titles
from config import AppConfig, PathsConfig
from core.job_queue import JobQueue
from tests.test_meeting_title_actions import MID, env  # noqa: F401


@pytest.fixture
def setup_titles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """모델만 대체하고 실제 SQLite 큐와 HTTP 라우터를 준비한다."""
    app = FastAPI()
    app.include_router(meeting_titles.router, prefix="/api")
    queue = JobQueue(tmp_path / "jobs.db")
    queue.initialize()
    app.state.job_queue = queue
    app.state.config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    app.state.running_tasks = set()
    revisions: dict[str, str] = {}
    for mid in ["one", "two", "three"]:
        queue.add_job(mid, str(tmp_path / f"{mid}.wav"), initial_status="completed")
        queue.update_title(mid, f"original {mid}")
        revisions[mid] = "v1"

    async def source(request: Any, mid: str) -> str:
        return revisions[mid]

    async def suggest(request: Any, mid: str) -> Any:
        return SimpleNamespace(title=f"2026-09-23 · generated {mid}")

    monkeypatch.setattr(meeting_titles, "_source_revision", source)
    monkeypatch.setattr(meeting_titles, "suggest_meeting_title", suggest)
    monkeypatch.setattr(
        meeting_titles,
        "get_model_manager",
        lambda: SimpleNamespace(get_status=lambda: {"native_cleanup_pending": False}),
    )
    yield app, queue, revisions
    queue.close()


def client_for(app: FastAPI) -> httpx.AsyncClient:
    """loopback 요청으로 보안 경계를 유지한다."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


async def drain(app: FastAPI) -> None:
    """생성 worker 종료를 기다린다."""
    await app.state.title_worker.task


async def test_ack_sequential_idempotent_overlap(setup_titles: Any, monkeypatch: Any) -> None:
    app, queue, _ = setup_titles
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def slow(request: Any, mid: str) -> Any:
        calls.append(mid)
        entered.set()
        await finish.wait()
        return SimpleNamespace(title=f"date · {mid}")

    monkeypatch.setattr(meeting_titles, "suggest_meeting_title", slow)
    async with client_for(app) as client:
        body = dict(request_id="r1", meeting_ids=["one", "two"])
        response = await client.post("/api/meetings/titles", json=body)
        assert response.status_code == 200
        await entered.wait()
        assert calls == ["one"]
        assert queue.get_job_by_meeting_id("one").title == "original one"
        repeated = await client.post("/api/meetings/titles", json=body)
        assert repeated.json() == response.json()
        overlap = await client.post(
            "/api/meetings/titles", json=dict(request_id="r2", meeting_ids=["one"])
        )
        assert overlap.json()["skipped"] == 1
        mismatch = await client.post(
            "/api/meetings/titles", json=dict(request_id="r1", meeting_ids=["three"])
        )
        assert mismatch.status_code == 409
        finish.set()
        await drain(app)
    assert calls == ["one", "two"]
    assert queue.get_job_by_meeting_id("two").title == "date · two"
    assert [e["status"] for e in queue.get_batch_receipts("r1")[0]["events"]] == [
        "running",
        "completed",
        "running",
        "completed",
    ]


@pytest.mark.parametrize("change", ["manual", "source", "replacement"])
async def test_changes_during_generation_are_preserved(
    setup_titles: Any, monkeypatch: Any, change: str
) -> None:
    app, queue, revisions = setup_titles

    async def racing(request: Any, mid: str) -> Any:
        if change == "manual":
            queue.update_title(mid, "My manual title")
        elif change == "source":
            revisions[mid] = "v2"
        else:
            conn = queue._ensure_connection()
            with conn:
                conn.execute("UPDATE jobs SET id=id+100 WHERE meeting_id=?", (mid,))
        return SimpleNamespace(title="AI would overwrite")

    monkeypatch.setattr(meeting_titles, "suggest_meeting_title", racing)
    async with client_for(app) as client:
        await client.post("/api/meetings/titles", json=dict(request_id="r", meeting_ids=["one"]))
        await drain(app)
    assert queue.get_job_by_meeting_id("one").title != "AI would overwrite"
    assert queue.get_batch_receipts("r")[0]["events"][-1]["status"] == "skipped"


async def test_individual_failure_continues_and_undo_respects_edits(
    setup_titles: Any, monkeypatch: Any
) -> None:
    app, queue, _ = setup_titles

    async def suggest(request: Any, mid: str) -> Any:
        if mid == "one":
            raise HTTPException(503, "model failed")
        return SimpleNamespace(title=f"AI {mid}")

    monkeypatch.setattr(meeting_titles, "suggest_meeting_title", suggest)
    async with client_for(app) as client:
        await client.post(
            "/api/meetings/titles", json=dict(request_id="r", meeting_ids=["one", "two", "three"])
        )
        await drain(app)
        queue.update_title("two", "hand edited")
        undone = await client.post("/api/title-jobs/r/undo")
        assert undone.json() == dict(restored=1, skipped=2)
        assert (await client.post("/api/title-jobs/r/undo")).json() == dict(restored=0, skipped=3)
    assert queue.get_job_by_meeting_id("three").title == "original three"
    assert queue.get_job_by_meeting_id("two").title == "hand edited"
    events = queue.get_batch_receipts("r")[0]["events"]
    completed = next(e for e in events if e.get("status") == "completed")
    assert completed["write_revision"] and completed["previous_title"] == "original two"


async def test_cancel_running_and_pending_never_publish(
    setup_titles: Any, monkeypatch: Any
) -> None:
    app, queue, _ = setup_titles
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def slow(request: Any, mid: str) -> Any:
        calls.append(mid)
        entered.set()
        await finish.wait()
        return SimpleNamespace(title="should never apply")

    monkeypatch.setattr(meeting_titles, "suggest_meeting_title", slow)
    async with client_for(app) as client:
        await client.post(
            "/api/meetings/titles", json=dict(request_id="r", meeting_ids=["one", "two"])
        )
        await entered.wait()
        assert (await client.post("/api/title-jobs/r/cancel")).json() == dict(cancelled=2)
        assert (await client.post("/api/title-jobs/r/cancel")).json() == dict(cancelled=0)
        finish.set()
        await drain(app)
    assert calls == ["one"]
    assert queue.get_job_by_meeting_id("one").title == "original one"
    assert queue.get_job_by_meeting_id("two").title == "original two"
    assert [e["status"] for e in queue.get_batch_receipts("r")[0]["events"]] == [
        "running",
        "cancelled",
        "cancelled",
    ]


async def test_shutdown_records_interrupted_and_does_not_publish(
    setup_titles: Any, monkeypatch: Any
) -> None:
    app, queue, _ = setup_titles
    entered = asyncio.Event()

    async def wait(request: Any, mid: str) -> Any:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(meeting_titles, "suggest_meeting_title", wait)
    async with client_for(app) as client:
        await client.post(
            "/api/meetings/titles", json=dict(request_id="r", meeting_ids=["one", "two"])
        )
        await entered.wait()
        app.state.title_worker.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain(app)
    events = queue.get_batch_receipts("r")[0]["events"]
    assert [e["status"] for e in events] == ["running", "interrupted", "interrupted"]
    assert queue.get_job_by_meeting_id("one").title == "original one"


async def test_pipeline_events_do_not_enter_title_receipt(setup_titles: Any) -> None:
    app, queue, _ = setup_titles
    queue.save_batch_receipt(dict(request_id="pipeline", action="full", meeting_ids=["one"]))
    async with client_for(app) as client:
        await client.post(
            "/api/meetings/titles", json=dict(request_id="title", meeting_ids=["one"])
        )
        await drain(app)
    queue.record_batch_event("one", dict(status="completed", marker="pipeline"))
    assert queue.get_batch_receipts("pipeline")[0]["events"][-1]["marker"] == "pipeline"
    assert all("marker" not in e for e in queue.get_batch_receipts("title")[0]["events"])


async def test_invalid_empty_and_remote_requests(setup_titles: Any) -> None:
    app, queue, _ = setup_titles
    async with client_for(app) as client:
        assert (
            await client.post("/api/meetings/titles", json=dict(request_id="r", meeting_ids=[]))
        ).status_code == 422
        assert (
            await client.post(
                "/api/meetings/titles", json=dict(request_id="r", meeting_ids=["../x"])
            )
        ).status_code == 400
        assert (
            await client.post(
                "/api/meetings/titles",
                headers={"origin": "https://example.com"},
                json=dict(request_id="r", meeting_ids=["one"]),
            )
        ).status_code == 403
        response = await client.post(
            "/api/meetings/titles", json=dict(request_id="missing", meeting_ids=["missing"])
        )
        assert response.json()["status"] == "no_targets"
        assert (await client.post("/api/title-jobs/missing-request/undo")).status_code == 404
    assert queue.get_batch_receipts("missing")[0]["skipped"] == 1


def test_real_title_service_holds_lease_and_persists_metadata(env: Any) -> None:  # noqa: F811
    """실제 전사문·제안 서비스·모델 lease를 거쳐 자동 적용과 복원을 검증한다."""
    response = env.client.post(
        "/api/meetings/titles", json=dict(request_id="integrated", meeting_ids=[MID])
    )
    assert response.status_code == 200, response.text
    env.client.portal.call(drain, env.app)
    title = env.queue.get_job_by_meeting_id(MID).title
    assert title == "2026-09-15 · 상담 자동화 도입 일정과 담당자 확정"
    event = env.queue.get_batch_receipts("integrated")[0]["events"][-1]
    assert event["status"] == "completed"
    assert event["date_source"] == "filename"
    assert event["sampled"] is False
    assert env.manager.current_model_name is None
    assert env.client.post("/api/title-jobs/integrated/undo").json() == dict(restored=1, skipped=0)
    assert env.queue.get_job_by_meeting_id(MID).title == "기존 제목"


async def test_global_manager_cleanup_wait_after_timeout(
    setup_titles: Any, monkeypatch: Any
) -> None:
    """앱 state manager가 없는 실제 서버 경로도 이전 native 정리를 기다린다."""
    app, queue, _ = setup_titles
    pending = False
    checked = asyncio.Event()
    calls = []

    def status() -> dict[str, bool]:
        if pending:
            checked.set()
        return {"native_cleanup_pending": pending}

    async def suggest(request: Any, mid: str) -> Any:
        nonlocal pending
        calls.append(mid)
        if mid == "one":
            pending = True
            raise HTTPException(504, "첫 요청 시간 초과")
        assert not pending
        return SimpleNamespace(title="정리 후 생성한 제목")

    monkeypatch.setattr(
        meeting_titles, "get_model_manager", lambda: SimpleNamespace(get_status=status)
    )
    monkeypatch.setattr(meeting_titles, "suggest_meeting_title", suggest)
    async with client_for(app) as client:
        await client.post(
            "/api/meetings/titles", json=dict(request_id="r", meeting_ids=["one", "two"])
        )
        await checked.wait()
        assert calls == ["one"]
        pending = False
        await drain(app)
    latest = {e["meeting_id"]: e["status"] for e in queue.get_batch_receipts("r")[0]["events"]}
    assert latest == {"one": "failed", "two": "completed"}


async def test_global_cleanup_wait_is_bounded(setup_titles: Any, monkeypatch: Any) -> None:
    """정리가 끝나지 않으면 제한 시간 뒤 실패로 남기고 새 모델을 호출하지 않는다."""
    app, queue, _ = setup_titles
    app.state.config.llm = app.state.config.llm.model_copy(
        update={"request_timeout_seconds": 0.01}
    )
    monkeypatch.setattr(
        meeting_titles,
        "get_model_manager",
        lambda: SimpleNamespace(get_status=lambda: {"native_cleanup_pending": True}),
    )

    async def forbidden(request: Any, mid: str) -> Any:
        pytest.fail("native 정리 중 모델 호출")

    monkeypatch.setattr(meeting_titles, "suggest_meeting_title", forbidden)
    async with client_for(app) as client:
        await client.post("/api/meetings/titles", json=dict(request_id="r", meeting_ids=["one"]))
        await drain(app)
    assert queue.get_batch_receipts("r")[0]["events"][-1]["status"] == "failed"
    assert queue.get_job_by_meeting_id("one").title == "original one"


@pytest.mark.parametrize("committed", [True, False])
async def test_shutdown_race_with_threaded_title_commit(
    setup_titles: Any, monkeypatch: Any, committed: bool
) -> None:
    """취소와 SQLite 쓰기가 경합해도 완료 기록을 보존하고 미적용 항목만 중단한다."""
    app, queue, _ = setup_titles
    original = queue.apply_generated_title
    entered = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def apply(*args: Any, **kwargs: Any) -> bool:
        result = original(*args, **kwargs) if committed else False
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)
        if not committed:
            result = original(*args, **kwargs)
        loop.call_soon_threadsafe(finished.set)
        return result

    monkeypatch.setattr(queue, "apply_generated_title", apply)
    try:
        async with client_for(app) as client:
            await client.post(
                "/api/meetings/titles", json=dict(request_id="r", meeting_ids=["one", "two"])
            )
            await entered.wait()
            app.state.title_worker.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await drain(app)
            release.set()
            await finished.wait()
    finally:
        release.set()
    latest = {e["meeting_id"]: e["status"] for e in queue.get_batch_receipts("r")[0]["events"]}
    assert latest == {"one": "completed" if committed else "interrupted", "two": "interrupted"}
    assert (queue.get_job_by_meeting_id("one").title != "original one") == committed
