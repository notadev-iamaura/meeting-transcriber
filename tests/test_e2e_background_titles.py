"""실제 UI·API·SQLite 제목 작업을 연결하고 모델 호출만 대역으로 검증한다."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from api.server import create_app
from config import AppConfig, PathsConfig, ServerConfig
from core.model_manager import ModelLoadManager
from tests.test_e2e_edit_playwright import _seed_meeting

pytestmark = pytest.mark.e2e
IDS = ["meeting_20260921_140000", "meeting_20260922_140000"]


@pytest.fixture
def title_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, ...]]:
    """모델 생성 시점을 제어할 수 있는 격리 서버를 띄운다."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)), server=ServerConfig(port=port))
    for mid in IDS:
        _seed_meeting(tmp_path, mid)
    release, started = threading.Event(), threading.Event()
    calls = []

    class Backend:
        """생성 전 UI를 움직일 시간을 확보하는 테스트 모델."""

        def chat(self, **kwargs: Any) -> str:
            calls.append(kwargs)
            started.set()
            assert release.wait(15), "테스트에서 모델 대기를 해제하지 않았습니다"
            return "상담 자동화 도입 일정 확정"

        def cleanup(self) -> None:
            """실제 모델 메모리를 사용하지 않는다."""

    monkeypatch.setattr("core.meeting_title.create_backend", lambda _: Backend())
    monkeypatch.setattr("core.model_manager.get_config", lambda: config)
    app = create_app(config, runtime_profile="api-test", config_path=tmp_path / "config.yaml")
    app.state.model_manager = ModelLoadManager(gpu_cache_cleanup_enabled=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}", app, release, started, calls
    finally:
        release.set()
        server.should_exit = True
        thread.join(15)
        assert not thread.is_alive()


def test_bulk_titles_survive_navigation_and_restore(
    title_server: tuple[Any, ...], tmp_path: Path
) -> None:
    """두 제목을 화면 이동 후 자동 적용하고 작업 내역에서 함께 되돌린다."""
    url, app, release, started, calls = title_server
    originals = {
        mid: (tmp_path / "checkpoints" / mid / "correct.json").read_bytes() for mid in IDS
    }
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(url + "/app/viewer/" + IDS[0])
        expect(page.locator(".meeting-item")).to_have_count(2)
        for mid in IDS:
            page.locator(f'.meeting-item[data-meeting-id="{mid}"] [data-checkbox]').click()
        page.locator("#bulkTaskSelect").select_option("title")
        page.screenshot(path="/private/tmp/recap-title-bulk-ready.png", animations="disabled")
        page.locator("#bulkActionBar").get_by_role("button", name="실행", exact=True).click()
        assert started.wait(5)
        expect(page.get_by_role("dialog")).to_have_count(0)
        page.screenshot(path="/private/tmp/recap-title-background.png", animations="disabled")
        page.goto(url + "/app/viewer/" + IDS[1])
        page.reload()
        expect(page.locator("#backgroundWorkStatus")).to_be_visible()
        release.set()
        expect(page.locator(".viewer-title-text")).to_contain_text("상담 자동화", timeout=20000)
        expect(page.locator(".meeting-item").filter(has_text="상담 자동화")).to_have_count(
            2, timeout=20000
        )
        receipts = page.request.get(url + "/api/batch-requests").json()["requests"]
        receipt = next(row for row in receipts if row.get("action") == "title")
        assert len(calls) == 2
        assert [e["status"] for e in receipt["events"]].count("completed") == 2
        for mid in IDS:
            assert (tmp_path / "checkpoints" / mid / "correct.json").read_bytes() == originals[mid]
        page.locator("#batchHistoryButton").click()
        history = page.get_by_role("dialog", name="작업 내역")
        history.get_by_role("button", name="이전 제목으로 되돌리기", exact=True).click()
        expect(history).to_contain_text("이전 제목으로 복원", timeout=10000)
        for mid in IDS:
            assert page.request.get(url + "/api/meetings/" + mid).json()["title"] == ""
        browser.close()


def test_single_title_manual_edit_wins_in_background(title_server: tuple[Any, ...]) -> None:
    """개별 제목 정리 중 직접 편집한 제목은 늦게 온 AI 결과보다 우선한다."""
    url, app, release, started, calls = title_server
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(url + "/app/viewer/" + IDS[0])
        page.get_by_role("button", name="제목 자동 정리", exact=True).click()
        assert started.wait(5)
        expect(page.get_by_role("dialog")).to_have_count(0)
        page.locator(".viewer-title-text").click()
        page.locator(".viewer-title-input").fill("사용자가 정한 제목")
        page.locator(".viewer-title-input").press("Enter")
        expect(page.locator(".viewer-title-text")).to_have_text("사용자가 정한 제목")
        release.set()
        page.locator("#batchHistoryButton").click()
        history = page.get_by_role("dialog", name="작업 내역")
        expect(history).to_contain_text("변경", timeout=10000)
        assert (
            page.request.get(url + "/api/meetings/" + IDS[0]).json()["title"]
            == "사용자가 정한 제목"
        )
        assert len(calls) == 1
        browser.close()


def test_unsaved_title_draft_survives_background_completion(
    title_server: tuple[Any, ...],
) -> None:
    """AI 완료 갱신이 아직 저장하지 않은 제목 입력과 초점을 없애지 않는다."""
    url, app, release, started, calls = title_server
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(url + "/app/viewer/" + IDS[0])
        page.get_by_role("button", name="제목 자동 정리", exact=True).click()
        assert started.wait(5)
        page.locator(".viewer-title-text").click()
        draft = page.locator(".viewer-title-input")
        draft.fill("작성 중이던 나의 제목")
        release.set()
        # 목록의 AI 제목 갱신은 서버 저장과 브라우저 완료 polling을 모두 거쳤다는 증거다.
        expect(page.locator(".meeting-item").filter(has_text="상담 자동화")).to_have_count(
            1, timeout=15000
        )
        expect(draft).to_have_value("작성 중이던 나의 제목")
        expect(draft).to_be_focused()
        draft.press("Enter")
        expect(page.locator(".viewer-title-text")).to_have_text("작성 중이던 나의 제목")
        assert (
            page.request.get(url + "/api/meetings/" + IDS[0]).json()["title"]
            == "작성 중이던 나의 제목"
        )
        browser.close()
