"""개별 녹취의 폴더 열기와 로컬 AI 제목 제안 계약을 검증한다."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import router
from config import AppConfig, LLMConfig, PathsConfig
from core.job_queue import JobQueue, JobStatus
from core.llm_backend import LLMGenerationError
from core.meeting_title import clean_title, sample_transcript, suggest_title
from core.model_manager import ModelLoadManager

MID = "meeting_20260915_143149"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """실제 DB·체크포인트와 가짜 native 백엔드를 격리한다."""
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    audio = tmp_path / "audio_input" / "원본 녹취.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"original-recording")
    cp = config.paths.resolved_checkpoints_dir / MID
    cp.mkdir(parents=True)
    (cp / "transcribe.json").write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "text": "고객 상담 자동화 도입 일정과 담당자를 정했습니다.",
                        "start": 0,
                        "end": 5,
                    }
                ]
            }
        )
    )
    queue = JobQueue(tmp_path / "jobs.db")
    queue.initialize()
    queue.add_job(MID, str(audio), initial_status="completed")
    queue.update_title(MID, "기존 제목")
    backend = Mock()
    backend.chat.return_value = "상담 자동화 도입 일정과 담당자 확정"
    monkeypatch.setattr("core.meeting_title.create_backend", lambda _: backend)
    monkeypatch.setattr("core.model_manager.get_config", lambda: config)
    manager = ModelLoadManager(gpu_cache_cleanup_enabled=False)
    app = FastAPI()
    app.include_router(router)
    app.state.config = config
    app.state.job_queue = SimpleNamespace(queue=queue)
    app.state.model_manager = manager
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield SimpleNamespace(
            client=client,
            queue=queue,
            audio=audio,
            cp=cp,
            backend=backend,
            manager=manager,
            app=app,
            config=config,
        )


def test_suggestion_uses_recording_date_and_does_not_save(env) -> None:
    """제목 제안은 등록일 대신 녹취일을 쓰고 원본·제목·전사문을 유지한다."""
    before = (env.cp / "transcribe.json").read_bytes()
    result = env.client.post(f"/api/meetings/{MID}/title-suggestion")
    assert result.status_code == 200, result.text
    assert result.json() == {
        "title": "2026-09-15 · 상담 자동화 도입 일정과 담당자 확정",
        "recording_date": "2026-09-15",
        "date_source": "filename",
        "sampled": False,
    }
    assert env.queue.get_job_by_meeting_id(MID).title == "기존 제목"
    assert env.audio.read_bytes() == b"original-recording"
    assert (env.cp / "transcribe.json").read_bytes() == before
    env.backend.cleanup.assert_called_once()
    assert env.manager.current_model_name is None
    saved = env.client.patch(f"/api/meetings/{MID}", json={"title": result.json()["title"]})
    assert saved.status_code == 200
    assert env.queue.get_job_by_meeting_id(MID).title == result.json()["title"]


def test_preserved_date_wins_and_labels_fallback(env) -> None:
    """보존한 날짜와 그 출처를 제목 미리보기에 전달한다."""
    (env.cp / "meeting_date.json").write_text(
        json.dumps({"date": "2026-09-01", "source": "audio_mtime"})
    )
    result = env.client.post(f"/api/meetings/{MID}/title-suggestion")
    assert result.json()["title"].startswith("2026-09-01 · ")
    assert result.json()["date_source"] == "audio_mtime"


@pytest.mark.parametrize(
    "status", ["queued", "recording", "transcribing", "diarizing", "merging", "embedding"]
)
def test_active_meeting_is_rejected(env, status: str) -> None:
    """native 작업 중인 상태는 모델 로드 전에 차단한다."""
    env.queue.force_set_status(env.queue.get_job_by_meeting_id(MID).id, JobStatus(status))
    result = env.client.post(f"/api/meetings/{MID}/title-suggestion")
    assert result.status_code == 409
    env.backend.chat.assert_not_called()


@pytest.mark.parametrize("content", ["", "설명입니다.\n제목입니다.", "<think>생각 중", "가" * 81])
def test_invalid_ai_output_preserves_title(env, content: str) -> None:
    """실패하거나 완성되지 않은 응답은 기존 제목을 바꾸지 않는다."""
    env.backend.chat.return_value = content
    result = env.client.post(f"/api/meetings/{MID}/title-suggestion")
    assert result.status_code == 503
    assert env.queue.get_job_by_meeting_id(MID).title == "기존 제목"


def test_empty_transcript_and_unknown_date_do_not_load(env) -> None:
    """내용 또는 날짜가 없으면 추측하여 생성하지 않는다."""
    (env.cp / "transcribe.json").write_text('{"segments": []}')
    assert env.client.post(f"/api/meetings/{MID}/title-suggestion").status_code == 409
    (env.cp / "meeting_date.json").write_text('{"date": "", "source": "unknown"}')
    assert env.client.post(f"/api/meetings/{MID}/title-suggestion").status_code == 409
    env.backend.chat.assert_not_called()


def test_folder_selects_original_without_shell(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """사용자 경로 입력 없이 DB의 원본 파일만 Finder에서 선택한다."""
    run = Mock()
    # 원본 조회는 긴 파이프라인의 회의 잠금에 막히지 않아야 한다.
    monkeypatch.setattr(
        "api.routers.meeting_detail._get_meeting_mutation_coordinator",
        lambda _: pytest.fail("폴더 열기가 전사 완료를 기다립니다"),
    )
    monkeypatch.setattr("api.routers.meeting_detail.sys.platform", "darwin")
    monkeypatch.setattr("api.routers.meeting_detail.subprocess.run", run)
    response = env.client.post(f"/api/meetings/{MID}/open-audio-folder")
    assert response.status_code == 200
    assert response.json() == {"opened": True, "path": str(env.audio.parent)}
    assert run.call_args.args[0] == ["/usr/bin/open", "-R", str(env.audio)]
    assert "shell" not in run.call_args.kwargs
    assert env.audio.read_bytes() == b"original-recording"


def test_folder_missing_symlink_and_execution_failure(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finder 실패와 누락·심볼릭 링크를 구별하고 새 폴더를 만들지 않는다."""
    run = Mock(side_effect=subprocess.TimeoutExpired("open", 5))
    monkeypatch.setattr("api.routers.meeting_detail.sys.platform", "darwin")
    monkeypatch.setattr("api.routers.meeting_detail.subprocess.run", run)
    url = f"/api/meetings/{MID}/open-audio-folder"
    assert env.client.post(url).status_code == 503
    env.audio.unlink()
    run.reset_mock()
    assert env.client.post(url).status_code == 404
    env.audio.symlink_to(env.cp / "transcribe.json")
    assert env.client.post(url).status_code == 409
    run.assert_not_called()


@pytest.mark.parametrize("action", ["title-suggestion", "open-audio-folder"])
def test_actions_reject_remote_origin_and_missing_meeting(env, action: str) -> None:
    """로컬 앱 요청만 허용하고 없는 회의에서 부작용을 만들지 않는다."""
    assert (
        env.client.post(
            f"/api/meetings/{MID}/{action}", headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert env.client.post(f"/api/meetings/meeting_20260101_000000/{action}").status_code == 404


def test_sampling_spans_recording_and_title_cleanup() -> None:
    """긴 녹취의 끝도 발췌하고 날짜는 모델의 추측과 분리한다."""
    excerpt, sampled = sample_transcript(["시작" + "가" * 2000 + "중간" + "나" * 2000 + "끝"], 300)
    assert sampled and len(excerpt) <= 300
    assert excerpt.startswith("시작") and excerpt.endswith("끝") and "중간" in excerpt
    assert (
        clean_title('<think>내부 생각</think>제목: "2020-01-01 · 도입 일정 확정"', 80)
        == "도입 일정 확정"
    )
    with pytest.raises(LLMGenerationError):
        clean_title("안전\x00하지 않은 제목", 80)


@pytest.mark.asyncio
async def test_cancelled_title_retains_model_until_native_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """제목 요청 취소 후에도 실행 중인 모델을 먼저 정리하지 않는다."""
    config = AppConfig()
    monkeypatch.setattr("core.model_manager.get_config", lambda: config)
    manager = ModelLoadManager(gpu_cache_cleanup_enabled=False)
    started, release = threading.Event(), threading.Event()
    backend = Mock()

    def chat(**kwargs) -> str:
        started.set()
        release.wait(3)
        return "도입 일정 확정"

    backend.chat.side_effect = chat
    monkeypatch.setattr("core.meeting_title.create_backend", lambda _: backend)
    task = asyncio.create_task(suggest_title(["도입 일정 회의"], LLMConfig(), manager))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        backend.cleanup.assert_not_called()
        assert manager.get_status()["native_cleanup_pending"]
    finally:
        release.set()
    async with asyncio.timeout(3):
        while manager.get_status()["native_cleanup_pending"]:
            await asyncio.sleep(0.01)
    backend.cleanup.assert_called_once()
