"""실제 브라우저·API·SQLite·파이프라인·worker로 12건 접수를 검증한다.

외부 API/모델 추론만 결정적인 테스트 대역으로 대체한다. WAV 변환, 병합,
체크포인트 보존, 단계 재개와 다음 작업 실행은 실제 구현을 사용한다.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import socket
import threading
import time
import wave
from array import array
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from api.server import create_app
from config import AppConfig, PathsConfig, ServerConfig
from core.orchestrator import JobProcessor
from core.pipeline import PipelineManager
from steps.diarizer import DiarizationResult, DiarizationSegment
from steps.transcriber import TranscriptResult, TranscriptSegment

pytestmark = pytest.mark.e2e


@pytest.fixture
def batch_server(tmp_path: Path):
    """실데이터와 분리한 서버에서 실제 worker를 실행한다."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)), server=ServerConfig(port=port))
    config.pipeline.retry_max_count = 1
    config.pipeline.min_memory_free_gb = 0.5
    config.wiki.enabled = False
    app = create_app(config, runtime_profile="api-test", config_path=tmp_path / "config.yaml")
    original_lifespan = app.router.lifespan_context
    failures = {"batch_00", "batch_01"}
    transcribed: list[str] = []

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            pipeline = PipelineManager(
                config, meeting_mutation_coordinator=app.state.meeting_mutation_coordinator
            )

            async def transcribe(wav: Path, cp: Path, **kwargs: Any) -> TranscriptResult:
                transcribed.append(cp.parent.name)
                result = TranscriptResult(
                    [TranscriptSegment(text="일괄 처리 검증용 전사문입니다.", start=0, end=35)],
                    "일괄 처리 검증용 전사문입니다.",
                    "ko",
                    str(wav),
                    model=kwargs["stt_model"],
                )
                result.save_checkpoint(cp)
                return result

            async def diarize(wav: Path, cp: Path, transcript: Any) -> DiarizationResult:
                result = DiarizationResult(
                    [DiarizationSegment(speaker="SPEAKER_00", start=0, end=35)], 1, str(wav)
                )
                result.save_checkpoint(cp)
                return result

            async def correct(merged: Any, cp: Path, **kwargs: Any) -> Any:
                await asyncio.sleep(0.05)
                if cp.parent.name in failures:
                    raise RuntimeError(
                        "MLX-VLM 모델 로드 실패: Received 2 parameters not in model: language_model.model.per_layer_model_projection.biases language_model.model.per_layer_model_projection.scales"
                    )
                result = pipeline._build_passthrough_corrected_result(merged)
                result.save_checkpoint(cp)
                return result

            async def summarize(result: Any, cp: Path, output: Path, **kwargs: Any) -> str:
                (output / "summary.md").write_text(
                    "## 요약\n일괄 처리 검증 완료", encoding="utf-8"
                )
                cp.write_text("{}")
                return "일괄 처리 검증 완료"

            async def embed(result: Any, cp: Path, **kwargs: Any) -> None:
                cp.write_text("{}")

            pipeline._run_step_transcribe = transcribe
            pipeline._run_step_diarize = diarize
            pipeline._run_step_correct = correct
            pipeline._run_step_summarize = summarize
            pipeline._run_step_embed = embed
            app.state.pipeline_manager = pipeline
            audio_dir = tmp_path / "audio_input"
            audio_dir.mkdir(exist_ok=True)
            samples = array(
                "h", (int(8000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(16000))
            )
            queue = app.state.job_queue.queue
            for i in range(13):
                mid = f"batch_{i:02}"
                audio = audio_dir / f"{mid}.wav"
                with wave.open(str(audio), "wb") as writer:
                    writer.setnchannels(1)
                    writer.setsampwidth(2)
                    writer.setframerate(16000)
                    writer.writeframes(samples.tobytes() * 35)
                queue.add_job(mid, str(audio), initial_status="recorded")
            queue.add_job(
                "missing_audio", str(audio_dir / "missing.wav"), initial_status="recorded"
            )
            queue.add_job(
                "already_failed", str(audio_dir / "batch_00.wav"), initial_status="failed"
            )
            queue.add_job(
                "already_done", str(audio_dir / "batch_00.wav"), initial_status="completed"
            )
            cp = tmp_path / "checkpoints" / "already_done"
            cp.mkdir(parents=True)
            (cp / "merge.json").write_text("{}")
            out = tmp_path / "outputs" / "already_done"
            out.mkdir(parents=True)
            (out / "summary.md").write_text("완료")
            processor = JobProcessor(
                app.state.job_queue,
                pipeline,
                AsyncMock(),
                app.state.ws_manager,
                poll_interval=0.02,
            )
            processor._perf_stats = None
            app.state.job_processor = processor
            await processor.start()
            try:
                yield
            finally:
                await processor.stop()

    app.router.lifespan_context = lifespan
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}", app, failures, transcribed
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def test_twelve_jobs_receipt_failure_preservation_and_resume(
    batch_server, tmp_path: Path, caplog
) -> None:
    """14건 선택→12건 접수·2건 제외→2건 교정 실패·10건 완료→교정만 재시도."""
    url, app, failures, transcribed = batch_server
    caplog.set_level("INFO", logger="core.orchestrator")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url + "/app")
        expect(page.locator(".meeting-item")).to_have_count(16)
        page.locator('.meeting-item[data-meeting-id="batch_00"]').locator(
            "[data-checkbox]"
        ).click()
        expect(page.locator("#selectionActions")).to_be_visible()
        page.locator("#selectionActions").get_by_role("button", name="전사만", exact=True).click()
        dialog = page.get_by_role("dialog", name="일괄 처리 대상 확인")
        expect(dialog).to_contain_text("교정·요약은 나중에 따로 실행")
        dialog.get_by_role("button", name="닫기", exact=True).click()
        assert not transcribed
        page.get_by_role("button", name="최근 등록 24시간").click()
        page.get_by_role("menuitemradio", name="전사 + 교정·요약").click()
        expect(dialog).to_contain_text("녹음 시각이 아닌 앱에 등록된 시각", timeout=30000)
        expect(dialog).to_contain_text("검증에 실패한 파일", timeout=30000)
        dialog.locator('input[value="missing_audio"]').uncheck()
        dialog.locator('input[value="batch_12"]').uncheck()
        expect(dialog).to_contain_text("선택 14건 · 실행 가능 12건 · 제외 2건")
        page.screenshot(path="/private/tmp/recap-batch-review.png", animations="disabled")
        dialog.get_by_role("button", name="12건 대기열에 등록", exact=True).click()
        history = page.get_by_role("dialog", name="일괄 처리 접수 내역")
        expect(history).to_contain_text("12건 대기열 등록 · 2건 제외", timeout=30000)
        expect(
            history.locator(".batch-item-state").filter(has_text="전사 완료 · AI 교정 실패")
        ).to_have_count(2, timeout=120000)
        expect(
            history.locator(".batch-item-state").filter(has_text="완료", has_not_text="실패")
        ).to_have_count(10, timeout=120000)
        page.screenshot(path="/private/tmp/recap-batch-receipt.png", animations="disabled")
        assert len(transcribed) == 12
        receipt = page.request.get(url + "/api/batch-requests").json()["requests"][0]
        assert (receipt["matched"], receipt["queued"], receipt["skipped"]) == (14, 12, 2)
        assert len(receipt["candidates"]) == 14
        queue = app.state.job_queue.queue
        assert queue.get_job_by_meeting_id("batch_12").status == "recorded"
        assert queue.get_job_by_meeting_id("already_failed").status == "failed"
        assert all(
            queue.get_job_by_meeting_id(f"batch_{i:02}").requested_action == "full"
            for i in range(12)
        )
        # 새로고침 후에도 동일한 요청과 실패 단계가 조회된다.
        page.reload()
        page.get_by_role("button", name="접수 내역", exact=True).click()
        expect(history).to_contain_text(receipt["request_id"])
        history.get_by_role("link", name="batch_00", exact=True).click()
        expect(page.locator(".viewer-failure-notice")).to_contain_text("전사 완료 · AI 교정 실패")
        expect(page.locator(".utterance-text").first).to_contain_text("검증용 전사문")
        expect(page.get_by_role("button", name="교정 재시도", exact=True)).to_be_visible()
        page.screenshot(path="/private/tmp/recap-correction-failure.png", animations="disabled")
        merge = tmp_path / "checkpoints" / "batch_00" / "merge.json"
        before = hashlib.sha256(merge.read_bytes()).hexdigest()
        failures.remove("batch_00")
        page.get_by_role("button", name="교정 재시도", exact=True).click()
        expect(page.locator(".viewer-status")).to_have_text("완료", timeout=30000)
        assert transcribed.count("batch_00") == 1
        assert hashlib.sha256(merge.read_bytes()).hexdigest() == before
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(url + "/app")
        page.get_by_role("button", name="메뉴 열기", exact=True).click()
        page.get_by_role("button", name="접수 내역", exact=True).click()
        expect(history).to_be_visible()
        assert history.evaluate("el => el.scrollWidth <= el.clientWidth")
        page.screenshot(path="/private/tmp/recap-batch-mobile.png", animations="disabled")
        assert not errors
        browser.close()


def test_deferred_correction_failure_retries_without_stt(batch_server, tmp_path: Path) -> None:
    """전사만 완료한 뒤 별도로 실패한 교정도 표시하고 LLM만 재시도한다."""
    url, app, failures, transcribed = batch_server
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(url + "/app")
        expect(page.locator(".meeting-item")).to_have_count(16)
        page.locator('.meeting-item[data-meeting-id="batch_00"] [data-checkbox]').click()
        page.locator("#selectionActions").get_by_role("button", name="전사만", exact=True).click()
        dialog = page.get_by_role("dialog", name="일괄 처리 대상 확인")
        dialog.get_by_role("button", name="1건 대기열에 등록", exact=True).click()
        history = page.get_by_role("dialog", name="일괄 처리 접수 내역")
        expect(history).to_contain_text("전사 완료 · 교정·요약 대기", timeout=30000)
        history.get_by_role("button", name="닫기", exact=True).click()
        page.locator('.meeting-item[data-meeting-id="batch_00"] [data-checkbox]').click()
        page.locator("#selectionActions").get_by_role(
            "button", name="교정·요약", exact=True
        ).click()
        dialog.get_by_role("button", name="1건 대기열에 등록", exact=True).click()
        expect(history).to_contain_text("전사 완료 · AI 교정 실패", timeout=30000)
        page.goto(url + "/app/viewer/batch_00")
        expect(page.locator(".viewer-failure-notice")).to_contain_text("전사 완료 · AI 교정 실패")
        expect(page.locator(".utterance-text").first).to_contain_text("검증용 전사문")
        merge = tmp_path / "checkpoints" / "batch_00" / "merge.json"
        before = hashlib.sha256(merge.read_bytes()).hexdigest()
        failures.remove("batch_00")
        page.get_by_role("button", name="교정 재시도", exact=True).click()
        expect(page.locator(".viewer-status")).to_have_text("완료", timeout=30000)
        assert transcribed == ["batch_00"]
        assert hashlib.sha256(merge.read_bytes()).hexdigest() == before
        browser.close()
