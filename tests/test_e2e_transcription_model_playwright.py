"""회의별 전사 모델 선택 Playwright E2E.

실제 FastAPI 서버와 임시 SQLite를 연결하되, OpenAI 네트워크와
macOS Keychain은 test double로 차단한다. 사용자 오디오·설정·키를 읽거나
변경하지 않는다.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
import uvicorn
from playwright.sync_api import Browser, Page, Request, expect, sync_playwright

from api.server import create_app
from config import AppConfig, PathsConfig, ServerConfig
from core.job_queue import JobQueue, JobStatus
from security import openai_keychain

pytestmark = pytest.mark.e2e

_TEST_HOST = "127.0.0.1"
_TEST_PORT = 8768
_BASE_URL = f"http://{_TEST_HOST}:{_TEST_PORT}"
_FAKE_OPENAI_KEY = "sk-e2e-placeholder-not-a-real-secret-0000000000"


@dataclass(frozen=True)
class _E2EContext:
    """회의별 모델 E2E의 격리 서버 정보."""

    base_dir: Path
    app: Any


def _port_in_use(port: int) -> bool:
    """테스트 전용 포트가 이미 사용 중인지 확인한다."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        return stream.connect_ex((_TEST_HOST, port)) == 0


def _wait_for_server(timeout_seconds: float = 15.0) -> None:
    """FastAPI health endpoint가 응답할 때까지 대기한다."""
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{_BASE_URL}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise RuntimeError(f"E2E 서버 시작 timeout: {last_error}")


def _seed_recorded_job(base_dir: Path, meeting_id: str) -> None:
    """실제 SQLite에 녹음 완료 회의를 추가한다."""
    audio_path = base_dir / "audio_input" / f"{meeting_id}.wav"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"isolated-e2e-audio-sentinel")

    queue = JobQueue(base_dir / "pipeline.db")
    queue.initialize()
    try:
        queue.add_job(
            meeting_id,
            str(audio_path),
            initial_status=JobStatus.RECORDED.value,
        )
    finally:
        queue.close()


def _read_job_snapshot(base_dir: Path, meeting_id: str) -> tuple[str, str, str]:
    """SQLite에 저장된 status/provider/model snapshot을 읽는다."""
    with sqlite3.connect(base_dir / "pipeline.db") as connection:
        row = connection.execute(
            "SELECT status, stt_provider, stt_model FROM jobs WHERE meeting_id=?",
            (meeting_id,),
        ).fetchone()
    assert row is not None
    return str(row[0]), str(row[1]), str(row[2])


@pytest.fixture(scope="module")
def transcription_e2e_server(tmp_path_factory: pytest.TempPathFactory) -> _E2EContext:
    """임시 FastAPI·SQLite 서버를 시작하고 항상 종료한다."""
    if _port_in_use(_TEST_PORT):
        pytest.fail(f"E2E 전용 포트 {_TEST_PORT}가 이미 사용 중입니다.")

    base_dir = tmp_path_factory.mktemp("e2e-transcription-model")
    for meeting_id in (
        "meeting_oneoff_e2e",
        "meeting_mobile_e2e",
        "meeting_no_key_e2e",
    ):
        _seed_recorded_job(base_dir, meeting_id)

    config = AppConfig(
        paths=PathsConfig(base_dir=str(base_dir)),
        server=ServerConfig(host=_TEST_HOST, port=_TEST_PORT, log_level="warning"),
    )
    config.audio_quality.enabled = False
    config.stt.provider = "local"

    with (
        patch.object(openai_keychain, "_read_keychain_api_key", return_value=None),
        patch.dict(os.environ, {"OPENAI_API_KEY": _FAKE_OPENAI_KEY}),
    ):
        app = create_app(config, runtime_profile="api-test")
        uvicorn_config = uvicorn.Config(
            app,
            host=_TEST_HOST,
            port=_TEST_PORT,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(uvicorn_config)
        server_thread = threading.Thread(
            target=server.run,
            name="transcription-model-e2e-server",
            daemon=True,
        )
        server_thread.start()
        _wait_for_server()
        try:
            yield _E2EContext(base_dir=base_dir, app=app)
        finally:
            server.should_exit = True
            server_thread.join(timeout=10)
            assert not server_thread.is_alive(), "E2E FastAPI 서버가 종료되지 않았습니다."


@pytest.fixture(scope="module")
def browser() -> Browser:
    """E2E용 headless Chromium을 하나만 생성한다."""
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(headless=True)
        yield chromium
        chromium.close()


def _open_recorded_viewer(page: Page, meeting_id: str) -> None:
    """녹음 완료 회의 뷰어를 열고 주요 CTA를 대기한다."""
    page.goto(f"{_BASE_URL}/app/viewer/{meeting_id}", wait_until="domcontentloaded")
    expect(page.locator(".viewer-action-btn.transcribe")).to_be_visible(timeout=10_000)


def test_회의별_OpenAI선택은_실제_UI_API_SQLite를_관통한다(
    transcription_e2e_server: _E2EContext,
    browser: Browser,
) -> None:
    """로컬 기본에서 회의 하나만 OpenAI snapshot으로 queue한다."""
    meeting_id = "meeting_oneoff_e2e"
    requests: list[dict[str, Any]] = []
    settings_mutations: list[str] = []
    console_errors: list[str] = []
    http_errors: list[tuple[int, str]] = []
    page_errors: list[str] = []

    context = browser.new_context(viewport={"width": 1024, "height": 768})
    page = context.new_page()

    def _capture_request(request: Request) -> None:
        parsed = urlparse(request.url)
        if parsed.path == f"/api/meetings/{meeting_id}/transcribe" and request.method == "POST":
            requests.append(request.post_data_json)
        if parsed.path == "/api/settings" and request.method != "GET":
            settings_mutations.append(request.method)

    def _capture_console(message: Any) -> None:
        if message.type != "error":
            return
        if message.text.startswith("Failed to load resource"):
            return
        console_errors.append(message.text)

    page.on("request", _capture_request)
    page.on("console", _capture_console)
    page.on(
        "response",
        lambda response: (
            http_errors.append((response.status, urlparse(response.url).path))
            if response.status >= 400
            else None
        ),
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    try:
        _open_recorded_viewer(page, meeting_id)
        trigger = page.locator(".viewer-action-btn.transcribe")
        trigger.click()
        dialog = page.locator("dialog.start-transcription-dialog")
        expect(dialog).to_be_visible()

        assert dialog.locator('input[value="local"]').is_checked()
        assert dialog.locator("#meetingExternalUploadWarning").is_hidden()
        assert dialog.locator(".transcription-dialog-submit").is_enabled()

        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)
        assert page.evaluate("() => document.activeElement?.classList.contains('transcribe')")

        trigger.click()
        dialog = page.locator("dialog.start-transcription-dialog")
        expect(dialog).to_be_visible()
        dialog.locator('input[value="openai:gpt-4o-transcribe-diarize"]').check()
        submit = dialog.locator(".transcription-dialog-submit")
        assert dialog.locator("#meetingExternalUploadWarning").is_visible()
        assert submit.is_disabled()

        dialog.locator("#meetingExternalUploadConsent").check()
        assert submit.is_enabled()
        submit.click()
        expect(dialog).to_have_count(0)
        expect(page.locator("#viewerMetaTranscription")).to_contain_text("OpenAI 서버")
        expect(page.get_by_role("button", name="✕ 전사 취소")).to_be_visible()

        assert requests == [
            {
                "model_id": "openai:gpt-4o-transcribe-diarize",
                "external_upload_confirmed": True,
            }
        ]
        assert settings_mutations == []
        assert transcription_e2e_server.app.state.config.stt.provider == "local"
        assert _read_job_snapshot(transcription_e2e_server.base_dir, meeting_id) == (
            "queued",
            "openai",
            "gpt-4o-transcribe-diarize",
        )
        assert sorted(http_errors) == sorted(
            [
                (503, "/api/recording/status"),
                (404, f"/api/meetings/{meeting_id}/summary"),
                (404, f"/api/meetings/{meeting_id}/transcript"),
            ]
        )
        assert console_errors == []
        assert page_errors == []
    finally:
        context.close()


def test_회의별_모델_대화상자는_390px에서_넘치지않는다(
    transcription_e2e_server: _E2EContext,
    browser: Browser,
) -> None:
    """모바일 viewport에서 카드·동의·버튼을 가로 scroll 없이 표시한다."""
    del transcription_e2e_server
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        _open_recorded_viewer(page, "meeting_mobile_e2e")
        page.locator(".viewer-action-btn.transcribe").click()
        dialog = page.locator("dialog.start-transcription-dialog")
        expect(dialog).to_be_visible()
        dialog.locator('input[value="openai:gpt-4o-transcribe-diarize"]').check()

        layout = page.evaluate(
            """() => {
                const dialog = document.querySelector('dialog.start-transcription-dialog');
                const rect = dialog.getBoundingClientRect();
                return {
                    clientWidth: document.documentElement.clientWidth,
                    scrollWidth: document.documentElement.scrollWidth,
                    dialogLeft: rect.left,
                    dialogRight: rect.right,
                    consentVisible: !document.querySelector('#meetingExternalUploadConsentLabel').hidden,
                };
            }"""
        )
        assert layout["scrollWidth"] == layout["clientWidth"] == 390
        assert 0 <= layout["dialogLeft"] < layout["dialogRight"] <= 390
        assert layout["consentVisible"] is True
        assert dialog.locator(".transcription-dialog-submit").is_disabled()
    finally:
        context.close()


def test_OpenAI키가_없으면_실제_UI에서_선택과_POST를_막는다(
    transcription_e2e_server: _E2EContext,
    browser: Browser,
) -> None:
    """Keychain·env 키가 없는 상태에서 OpenAI를 disabled로 표시한다."""
    del transcription_e2e_server
    mutation_requests: list[str] = []
    previous_key = os.environ.pop("OPENAI_API_KEY", None)
    context = browser.new_context(viewport={"width": 1024, "height": 768})
    page = context.new_page()
    page.on(
        "request",
        lambda request: (
            mutation_requests.append(request.method)
            if urlparse(request.url).path == "/api/meetings/meeting_no_key_e2e/transcribe"
            else None
        ),
    )
    try:
        _open_recorded_viewer(page, "meeting_no_key_e2e")
        page.locator(".viewer-action-btn.transcribe").click()
        dialog = page.locator("dialog.start-transcription-dialog")
        expect(dialog).to_be_visible()
        assert dialog.locator('input[value="openai:gpt-4o-transcribe-diarize"]').is_disabled()
        assert dialog.locator("#meetingMissingOpenAIKey").is_visible()
        assert dialog.locator('input[value="local"]').is_checked()
        assert dialog.locator(".transcription-dialog-submit").is_enabled()
        assert mutation_requests == []
    finally:
        context.close()
        if previous_key is not None:
            os.environ["OPENAI_API_KEY"] = previous_key
