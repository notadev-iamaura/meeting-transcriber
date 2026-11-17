"""실제 ViewerView에서 Wiki 진행·실패·재진입 표시를 격리해 검증한다."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, Page, Route, expect

pytestmark = pytest.mark.ui
WEB = Path(__file__).resolve().parents[3] / "ui" / "web"
MAIN_STEPS = [
    "convert",
    "transcribe",
    "diarize",
    "merge",
    "correct",
    "summarize",
    "chunk",
    "embed",
]
WIKI_LABEL = "전사·요약·검색 완료 · Wiki 정리 중"


def _open_viewer(
    browser: Browser, *, wiki_running: bool = True
) -> tuple[Any, Page, dict[str, Any]]:
    """정적 웹 모듈과 가짜 API를 연결해 실제 앱·사용자 자료·모델을 사용하지 않는다."""
    context = browser.new_context()
    page = context.new_page()
    state: dict[str, Any] = {
        "meeting": {
            "meeting_id": "fixture",
            "status": "embedding",
            "status_label": WIKI_LABEL if wiki_running else "검색 반영 중",
            "current_step": "wiki_compile" if wiki_running else "embed",
            "completed_steps": MAIN_STEPS if wiki_running else MAIN_STEPS[:-1],
            "audio_path": "/fixture/audio.wav",
            "created_at": "2026-11-04T09:00:00",
        },
        "pipeline": {
            "status": "running",
            "current_step": "wiki_compile" if wiki_running else "embed",
            "step_results": [
                {"step": step, "success": True, "elapsed_seconds": 1}
                for step in MAIN_STEPS
                if wiki_running or step != "embed"
            ],
            "skipped_steps": [],
        },
    }

    def respond(route: Route) -> None:
        """정적 파일과 테스트 상태만 응답한다."""
        url = route.request.url
        if "/static/" in url:
            route.fulfill(path=str(WEB / url.rsplit("/", 1)[-1]))
        elif "/pipeline-state" in url:
            route.fulfill(json=state["pipeline"])
        elif "/api/" in url:
            route.fulfill(json=state["meeting"])
        else:
            route.fulfill(
                content_type="text/html",
                body="""<!doctype html><html lang="ko"><meta charset="utf-8">
                <link rel="stylesheet" href="/static/style.css"><main id="fixture"></main>
                <script src="/static/app.js"></script><script src="/static/viewer-view.js"></script>
                <script>
                const View=MeetingViewerView.create({App:MeetingApp,ListPanel:{},
                  PIPELINE_STEPS:MeetingApp.PIPELINE_STEPS,
                  Router:{getContentEl:()=>document.getElementById('fixture'),navigate:()=>{}}});
                View.prototype._loadData=function(){};
                window.testView=new View('fixture');
                testView._loadTranscript=async function(){return true;};
                testView._loadSummary=function(){};
                </script></html>""",
            )

    page.route("http://progress.test/**", respond)
    page.goto("http://progress.test/app/viewer/fixture")
    return context, page, state


def test_wiki_lifecycle_shows_real_elapsed_and_failure_without_estimate(browser: Browser) -> None:
    """실제 이벤트 리스너는 Wiki의 경과만 표시하고 실패 결과를 성공으로 바꾸지 않는다."""
    context, page, _ = _open_viewer(browser)
    try:
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.evaluate("""() => document.dispatchEvent(new CustomEvent('ws:step_progress', {detail:{
            meeting_id:'fixture',step:'wiki_compile',phase:'start',eta_seconds:120}}))""")
        row = page.locator(".log-row").filter(has=page.get_by_text("Wiki 정리", exact=True))
        expect(row).to_have_class("log-row anomaly-normal status-running")
        expect(row.locator(".log-progress-bar, .log-eta")).to_have_count(0)
        page.evaluate("""() => document.dispatchEvent(new CustomEvent('ws:step_progress', {detail:{
            meeting_id:'fixture',step:'wiki_compile',phase:'complete',success:false,
            elapsed_seconds:42,eta_seconds:120,anomaly:'danger'}}))""")
        expect(row).to_have_class("log-row anomaly-normal status-failed")
        expect(row.locator(".log-status")).to_have_text("✗")
        expect(row.locator(".log-elapsed")).to_have_text("42초")
        expect(row.locator(".log-progress-bar, .log-eta")).to_have_count(0)
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("wiki_running", [False, True])
def test_polling_uses_server_label_and_only_observed_wiki_step(
    browser: Browser, wiki_running: bool
) -> None:
    """서버 상태 문구를 보존하며 Wiki를 사용하지 않는 회의에는 기존 8단계만 표시한다."""
    context, page, _ = _open_viewer(browser, wiki_running=wiki_running)
    try:
        page.evaluate("testView._startPipelinePolling()")
        expect(page.locator("#viewerPipelineStatus")).to_have_text(
            WIKI_LABEL if wiki_running else "검색 반영 중"
        )
        expect(page.locator(".pipeline-step")).to_have_count(9 if wiki_running else 8)
        expect(page.locator(".pipeline-step.active .pipeline-step-label")).to_have_text(
            "Wiki 정리" if wiki_running else "검색 준비"
        )
        expect(page.locator("#viewerTranscriptSourceStatus")).to_have_text(
            "Wiki 정리 중" if wiki_running else "검색 준비 진행 중"
        )
        expect(page.locator("#viewerPipelineStatus")).not_to_contain_text("%")
    finally:
        context.close()


def test_wiki_refresh_restores_running_and_missed_completion(browser: Browser) -> None:
    """이벤트를 놓친 재진입도 실제 상태를 복원하고 근거 없는 경과·성공을 표시하지 않는다."""
    context, page, state = _open_viewer(browser)
    try:
        page.evaluate("testView._loadPipelineLog()")
        row = page.locator(".log-row").filter(has=page.get_by_text("Wiki 정리", exact=True))
        expect(row).to_have_class("log-row anomaly-normal status-running")
        expect(row.locator(".log-elapsed")).to_have_text("진행 중")
        state["pipeline"].update(status="completed", current_step="")
        state["pipeline"]["step_results"].append(
            {"step": "wiki_compile", "success": False, "elapsed_seconds": 42}
        )
        state["meeting"].update(
            status="completed",
            current_step="",
            status_label="전사·요약·검색 완료 · Wiki 정리 실패",
        )
        page.evaluate("testView._loadPipelineLog()")
        expect(row).to_have_class("log-row anomaly-normal status-failed")
        expect(row.locator(".log-elapsed")).to_have_text("42초")
        page.evaluate("testView._loadMeetingInfo()")
        expect(page.locator("#viewerMeetingStatus")).to_have_text(
            "전사·요약·검색 완료 · Wiki 정리 실패"
        )
        expect(row.locator(".log-progress-bar, .log-eta")).to_have_count(0)
    finally:
        context.close()


def test_interrupted_wiki_restore_does_not_present_unknown_elapsed_as_zero(
    browser: Browser,
) -> None:
    """복구된 Wiki 중단은 미측정 경과를 표시하고 기존 0초 실측 표시는 유지한다."""
    context, page, state = _open_viewer(browser)
    try:
        state["pipeline"].update(status="completed", current_step="")
        state["pipeline"]["step_results"][0]["elapsed_seconds"] = 0.0
        state["pipeline"]["step_results"].append(
            {
                "step": "wiki_compile",
                "success": False,
                "elapsed_seconds": 0.0,
                "elapsed_known": False,
            }
        )
        page.evaluate("testView._loadPipelineLog()")
        row = page.locator(".log-row").filter(has=page.get_by_text("Wiki 정리", exact=True))
        expect(row).to_have_class("log-row anomaly-normal status-failed")
        expect(row.locator(".log-elapsed")).to_have_text("경과 미확정")
        expect(row.locator(".log-status")).to_have_text("✗")
        expect(row.locator(".log-progress-bar, .log-eta")).to_have_count(0)
        convert = page.locator(".log-row").filter(has=page.get_by_text("변환", exact=True))
        expect(convert.locator(".log-elapsed")).to_have_text("0초")
    finally:
        context.close()
