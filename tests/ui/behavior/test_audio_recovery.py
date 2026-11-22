"""합성 서버와 실제 SPA로 마이크 복구·녹음 저장 실패의 사용자 흐름을 검증한다."""

from __future__ import annotations

import io
import wave

import pytest
from playwright.sync_api import Browser, Page, Route, expect

from tests.ui.audio_recovery_fixture import MEETING_ID, audio_recovery_server

pytestmark = pytest.mark.ui


def _control(page: Page, action: str) -> None:
    """제품 API와 분리한 합성 검증 버튼을 누른다."""
    page.locator(f'[data-fixture="{action}"]').click()


def test_review_switches_tracks_at_the_same_missing_interval(browser: Browser) -> None:
    """키보드로 두 번째 구간을 재생하고 트랙 전환 시 똑같은 시각부터 비교한다."""
    with audio_recovery_server("partial") as fixture:
        fixture.mic.update(split_playback_available=True, uncovered_intervals=[[20, 22], [51, 59]])
        with io.BytesIO() as buffer:
            with wave.open(buffer, "wb") as wav:
                wav.setparams((1, 2, 8000, 0, "NONE", ""))
                wav.writeframes(bytes(8 * 8000 * 2))
            payload = buffer.getvalue()
        context = browser.new_context()
        page = context.new_page()
        try:
            page.route(
                "**/audio/clip?*",
                lambda route: route.fulfill(content_type="audio/wav", body=payload),
            )
            page.goto(f"{fixture.url}/app/viewer/{MEETING_ID}")
            page.get_by_text("의심 구간 2개 듣기", exact=True).press("Enter")
            track = page.get_by_role("combobox", name="재생 트랙")
            track.select_option("mic")
            with page.expect_request("**/audio/clip?track=mic&start=51&end=59"):
                page.get_by_role("button", name="00:51–00:59 듣기").press("Enter")
            expect(page.get_by_role("button", name="재생 정지")).to_be_visible()
            for selected in ("system", "mix"):
                with page.expect_request(f"**/audio/clip?track={selected}&start=51&end=59"):
                    track.select_option(selected)
            page.get_by_role("button", name="재생 정지").click()
            expect(page.locator("audio")).to_have_count(0)
            expect(page.get_by_role("button", name="00:51–00:59 듣기")).to_be_visible()
        finally:
            context.close()


def test_home_mic_warning_opens_the_affected_meeting(browser: Browser) -> None:
    """홈에서 최초 누락 알림을 받고 해당 회의의 상태·재시도 화면으로 이동한다."""
    with audio_recovery_server() as fixture:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{fixture.url}/app")
            _control(page, "mic-warning")
            expect(page.locator("#micGlobalWarning")).to_be_visible()
            page.get_by_role("button", name="해당 회의 확인", exact=True).click()
            expect(page).to_have_url(f"{fixture.url}/app/viewer/{MEETING_ID}")
            expect(page.locator("#viewerMicTranscription")).to_be_visible()
            expect(page.locator("#viewerMicRetry")).to_be_disabled()
            expect(page.locator("#micGlobalWarning")).to_be_hidden()
            _control(page, "mic-ready")
            expect(page.locator("#viewerMicRetry")).to_be_enabled()
        finally:
            context.close()


def test_global_mic_warning_can_close_or_yield_to_the_meeting_sidebar(browser: Browser) -> None:
    """홈 알림을 닫거나 목록으로 회의에 진입해도 회의별 복구 안내는 보존한다."""
    with audio_recovery_server() as fixture:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{fixture.url}/app")
            _control(page, "mic-warning")
            page.get_by_role("button", name="마이크 알림 닫기").press("Enter")
            notice = page.locator("#micGlobalWarning")
            expect(notice).to_be_hidden()
            _control(page, "mic-warning")
            expect(notice).to_be_visible()
            page.locator(f'.meeting-item[data-meeting-id="{MEETING_ID}"]').click()
            expect(page.locator("#viewerMicTranscription")).to_be_visible()
            expect(notice).to_be_hidden()
        finally:
            context.close()


@pytest.mark.parametrize("initial_status", ["empty", "low_volume"])
def test_mic_retry_preserves_remote_text_and_refreshes_only_after_success(
    browser: Browser, initial_status: str
) -> None:
    """실제 버튼 클릭 한 번으로 재시도하며 진행 중에도 기존 발화를 읽을 수 있다."""
    with audio_recovery_server(initial_status) as fixture:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{fixture.url}/app/viewer/{MEETING_ID}")
            notice = page.locator("#viewerMicTranscription")
            expect(notice).to_be_visible()
            expect(page.locator(".utterance-text")).to_have_text(["원래 상대방 발화"])
            retry = page.get_by_role("button", name="내 마이크만 다시 전사", exact=True)
            retry.click()
            expect(page.locator("#viewerMicRetry")).to_be_disabled()
            expect(notice).to_contain_text("기존 전사문은 계속 확인")
            assert fixture.retry_posts == 1
            _control(page, "mic-running")
            expect(notice).to_contain_text("다시 전사하고 있습니다")
            expect(page.locator(".utterance-text")).to_have_text(["원래 상대방 발화"])
            _control(page, "mic-complete")
            expect(notice).to_contain_text("내 마이크 전사를 반영했습니다")
            expect(page.locator(".utterance-text")).to_have_text(
                ["원래 상대방 발화", "복구된 내 발화"]
            )
            expect(page.locator("#viewerMicRetry")).to_be_hidden()
        finally:
            context.close()


def test_mic_retry_request_and_worker_failures_keep_retry_action(browser: Browser) -> None:
    """등록 오류와 실행 오류가 성공으로 보이지 않고 재시도 버튼이 회복된다."""
    with audio_recovery_server() as fixture:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{fixture.url}/app/viewer/{MEETING_ID}")
            _control(page, "request-failed")
            retry = page.locator("#viewerMicRetry")
            retry.click()
            expect(page.locator("#viewerMicError")).to_contain_text("합성 작업 등록 오류")
            expect(retry).to_be_enabled()
            fixture.retry_error = False
            retry.click()
            expect(retry).to_be_disabled()
            _control(page, "mic-failed")
            expect(page.locator("#viewerMicTitle")).to_have_text(
                "내 마이크 재전사를 완료하지 못했습니다"
            )
            expect(retry).to_be_enabled()
            expect(page.locator(".utterance-text")).to_have_text(["원래 상대방 발화"])
        finally:
            context.close()


def test_pending_mic_retry_recovers_without_websocket_event(browser: Browser) -> None:
    """화면을 다시 열었어도 진행 상태를 복원하고 이벤트 없이 폴링으로 결과를 갱신한다."""
    with audio_recovery_server("retry_queued") as fixture:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{fixture.url}/app/viewer/{MEETING_ID}")
            expect(page.locator("#viewerMicRetry")).to_be_disabled()
            _control(page, "mic-poll-complete")
            expect(page.locator("#viewerMicTitle")).to_have_text(
                "내 마이크 전사를 반영했습니다", timeout=5000
            )
            expect(page.locator(".utterance-text")).to_have_text(
                ["원래 상대방 발화", "복구된 내 발화"]
            )
        finally:
            context.close()


def test_partial_mic_retry_keeps_existing_self_and_remote_speech(browser: Browser) -> None:
    """일부 누락 경고에서 재시도해도 원래 내 발화·상대방 발화를 그대로 표시한다."""
    with audio_recovery_server("partial") as fixture:
        fixture.mic["uncovered_intervals"].extend([[None, 2], ["bad", 3], [-1, 2], [4, 3]])
        fixture.utterances.append({"speaker": "나", "text": "원래 내 발화", "start": 0, "end": 1})
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{fixture.url}/app/viewer/{MEETING_ID}")
            expect(page.locator("#viewerMicTitle")).to_contain_text("일부가 전사되지 않았을 수")
            intervals = page.locator("#viewerMicIntervals")
            intervals.get_by_text("의심 구간 2개 듣기").click()
            expect(intervals.get_by_role("button")).to_have_text(
                ["00:20–00:23 듣기", "00:51–00:55 듣기"]
            )
            page.locator("#viewerMicRetry").click()
            expect(page.locator("#viewerMicRetry")).to_be_disabled()
            _control(page, "mic-complete")
            expect(page.locator("#viewerMicTitle")).to_have_text("내 마이크 전사를 반영했습니다")
            expect(page.locator(".utterance-text")).to_have_text(
                ["원래 상대방 발화", "원래 내 발화", "복구된 내 발화"]
            )
        finally:
            context.close()


@pytest.mark.parametrize("initial_status", ["complete", "unknown"])
def test_manual_mic_recheck_has_no_false_warning_or_false_addition(
    browser: Browser, initial_status: str
) -> None:
    """완료·과거 회의는 작은 수동 작업만 제공하고 추가0 결과를 성공 문구와 구별한다."""
    with audio_recovery_server(initial_status) as fixture:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{fixture.url}/app/viewer/{MEETING_ID}")
            expect(page.locator(".utterance-text")).to_have_text(["원래 상대방 발화"])
            expect(page.locator("#viewerMicTranscription")).to_be_hidden()
            page.locator(".viewer-more-actions summary").click()
            page.locator(".mic-track-retry").click()
            expect(page.locator("#viewerMicRetry")).to_be_disabled()
            _control(page, "mic-no-new")
            expect(page.locator("#viewerMicTitle")).to_have_text("새로 추가할 내 발화가 없습니다")
            expect(page.locator("#viewerMicMessage")).to_contain_text(
                "기존 발화와 편집을 그대로 보존"
            )
            expect(page.locator(".utterance-text")).to_have_text(["원래 상대방 발화"])
        finally:
            context.close()


def test_late_transcript_response_cannot_hide_recovered_mic_speech(browser: Browser) -> None:
    """복구 전 요청의 늦은 응답이 성공 후 추가된 내 발화를 되돌리지 않는다."""
    with audio_recovery_server() as fixture:
        context = browser.new_context()
        page = context.new_page()
        held: list[Route] = []

        def hold_one(route: Route) -> None:
            if not held:
                held.append(route)
            else:
                route.continue_()

        try:
            page.goto(f"{fixture.url}/app/viewer/{MEETING_ID}")
            expect(page.locator(".utterance-text")).to_have_text(["원래 상대방 발화"])
            old_transcript = {
                "utterances": list(fixture.utterances),
                "speakers": ["SPEAKER_00"],
                "source_stage": "corrected",
            }
            page.locator("#viewerMicRetry").click()
            page.route("**/transcript", hold_one)
            with page.expect_request(lambda request: request.url.endswith("/transcript")):
                _control(page, "mic-running")
            expect(page.locator("#viewerMicTitle")).to_have_text(
                "내 마이크를 다시 전사하고 있습니다"
            )
            _control(page, "mic-complete")
            expect(page.locator(".utterance-text")).to_have_text(
                ["원래 상대방 발화", "복구된 내 발화"]
            )
            assert len(held) == 1
            held[0].fulfill(json=old_transcript)
            expect(page.locator(".utterance-text")).to_have_text(
                ["원래 상대방 발화", "복구된 내 발화"]
            )
        finally:
            context.close()


def test_late_mic_status_does_not_erase_immediate_warning(browser: Browser) -> None:
    """늦은 최초 상태 조회가 방금 전달받은 마이크 누락 경고를 숨기지 않는다."""
    with audio_recovery_server("not_applicable") as fixture:
        context = browser.new_context()
        page = context.new_page()
        held: list[Route] = []
        try:
            page.route("**/mic-transcription", lambda route: held.append(route))
            page.goto(f"{fixture.url}/app/viewer/{MEETING_ID}")
            expect(page.locator(".utterance-text")).to_have_text(["원래 상대방 발화"])
            _control(page, "mic-warning")
            expect(page.locator("#viewerMicTranscription")).to_be_visible()
            assert held
            held[0].fulfill(json=fixture.mic_state("not_applicable"))
            expect(page.locator("#viewerMicTranscription")).to_be_visible()
            expect(page.locator("#viewerMicTitle")).to_contain_text("반영되지 않았습니다")
        finally:
            context.close()


@pytest.mark.parametrize(
    "initial_status", ["not_applicable", "no_speech", "processing", "complete"]
)
def test_mic_notice_does_not_invent_missing_speech(browser: Browser, initial_status: str) -> None:
    """실제 누락 판정이 없는 회의를 실패로 표시하지 않는다."""
    with audio_recovery_server(initial_status) as fixture:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{fixture.url}/app/viewer/{MEETING_ID}")
            expect(page.locator(".utterance-text")).to_have_text(["원래 상대방 발화"])
            expect(page.locator("#viewerMicTranscription")).to_be_hidden()
        finally:
            context.close()


def test_recording_failure_is_persistent_and_late_events_cannot_restart_timer(
    browser: Browser,
) -> None:
    """확인 전에는 새로고침 후에도 오류를 알리고, 새 녹음에서는 과거 이벤트를 무시한다."""
    with audio_recovery_server() as fixture:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{fixture.url}/app")
            _control(page, "record-start")
            expect(page.locator("#recordingOverlay")).to_have_class("recording-overlay visible")
            _control(page, "record-failed")
            alert = page.locator("#recordingFailure")
            expect(alert).to_be_visible()
            expect(alert).to_contain_text("자동 전사하지 않았습니다")
            expect(page.locator("#recordingOverlay")).not_to_have_class(
                "recording-overlay visible"
            )
            expect(page.locator("#recordingDuration")).to_have_text("00:00")
            _control(page, "late-duration")
            expect(page.locator("#recordingDuration")).to_have_text("00:00")
            page.reload()
            expect(alert).to_be_visible()
            alert.get_by_text("보존된 부분 파일", exact=True).click()
            expect(alert.locator("li")).to_have_text(
                ["synthetic.mic.partial.wav", "<b>system</b>.partial.wav"]
            )
            expect(alert.locator("li b")).to_have_count(0)
            _control(page, "record-start")
            expect(alert).to_be_hidden()
            _control(page, "old-failure")
            expect(alert).to_be_hidden()
            expect(page.locator("#recordingOverlay")).to_have_class("recording-overlay visible")
        finally:
            context.close()


def test_dismissing_a_recording_failure_acknowledges_only_that_incident(browser: Browser) -> None:
    """닫기는 동일 오류의 재조회·다른 창·재진입에 적용하고 새 오류는 즉시 알린다."""
    with audio_recovery_server() as fixture:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{fixture.url}/app")
            _control(page, "record-start")
            _control(page, "record-failed")
            alert = page.locator("#recordingFailure")
            expect(alert).to_be_visible()
            expect(alert.locator("#recordingFailureTime")).to_contain_text("중단 시각")
            other = context.new_page()
            other.goto(f"{fixture.url}/app/viewer/{MEETING_ID}")
            expect(other.locator("#recordingFailure")).to_be_visible()
            with page.expect_response("**/api/recording/error/acknowledge") as ack:
                alert.get_by_role("button", name="녹음 오류 닫기").click()
            assert ack.value.json() == {"acknowledged": True}
            expect(alert).to_be_hidden()
            expect(other.locator("#recordingFailure")).to_be_hidden()
            other.close()
            _control(page, "replay-failure")
            expect(alert).to_be_hidden()
            with browser.new_context() as private_window:
                fresh = private_window.new_page()
                with fresh.expect_response("**/api/recording/status") as status:
                    fresh.goto(f"{fixture.url}/app")
                assert status.value.json()["last_error"]["acknowledged"]
                expect(fresh.locator("#recordingFailure")).to_be_hidden()
            with page.expect_response("**/api/recording/status"):
                page.goto(f"{fixture.url}/app/viewer/{MEETING_ID}")
            expect(page.locator("#viewerMicTranscription")).to_be_visible()
            expect(alert).to_be_hidden()
            assert fixture.recording["last_error"]["partial_files_preserved"]
            # 동일 회의 식별자라도 다른 시각에 일어난 장애는 다시 알려야 한다.
            fixture.recording["last_error"]["occurred_at"] = "2026-11-18T12:01:00+09:00"
            fixture.recording["last_error"]["acknowledged"] = False
            with page.expect_response("**/api/recording/status"):
                page.reload()
            expect(alert).to_be_visible()
            alert.locator("summary").press("Escape")
            expect(alert).to_be_hidden()
            _control(page, "record-start")
            _control(page, "record-failed")
            expect(alert).to_be_visible()
            expect(page.locator("#recordingOutcome")).to_be_hidden()
        finally:
            context.close()


def test_stop_http_failure_restores_persistent_capture_warning(browser: Browser) -> None:
    """WebSocket 실패 알림을 놓쳐도 정지 API 오류 뒤 상태를 조회한다."""
    with audio_recovery_server() as fixture:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{fixture.url}/app")
            _control(page, "record-start")
            _control(page, "record-stop-failed")
            page.locator("#recordingOverlayStopBtn").click()
            expect(page.locator("#recordingFailure")).to_be_visible()
            expect(page.locator("#recordingDuration")).to_have_text("00:00")
            expect(page.locator("#recordingFailure")).not_to_contain_text("폐기")
        finally:
            context.close()


def test_only_one_stop_action_and_actual_transcription_state(browser: Browser) -> None:
    """대형·축소 화면에서 한 번 종료하고 회의 등록·대기·실행·완료를 순서대로 확인한다."""
    with audio_recovery_server() as fixture:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{fixture.url}/app")
            _control(page, "record-start")
            stop = page.get_by_role("button", name="녹음 끝내기", exact=True)
            expect(stop).to_have_count(1)
            page.get_by_role("button", name="녹음 HUD 최소화").click()
            expect(stop).to_have_count(1)
            stop.click()
            notice = page.locator("#recordingOutcome")
            expect(notice).to_contain_text("녹음 파일을 저장했습니다")
            expect(notice).to_contain_text("미등록 · 전사 시작 전")
            assert fixture.stop_posts == 1
            for action, label in [
                ("record-register", "전사 시작 전"),
                ("record-queue", "전사 대기열에 등록됨"),
                ("record-transcribe", "전사 중"),
                ("record-complete", "전사 완료"),
            ]:
                _control(page, action)
                expect(page.locator("#recordingOutcomeMessage")).to_have_text(label)
            expect(notice.get_by_role("link", name="회의 열기")).to_have_attribute(
                "href", "/app/viewer/capture_1"
            )
            page.reload()
            expect(notice).to_contain_text("전사 완료")
            notice.get_by_role("button", name="녹음 결과 닫기").click()
            page.reload()
            expect(notice).to_be_hidden()
        finally:
            context.close()


@pytest.mark.parametrize("automatic", [False, True])
def test_short_recording_has_no_transcription_link(browser: Browser, automatic: bool) -> None:
    """버튼과 자동 종료 모두 짧은 녹음의 저장 여부와 전사 미시작을 안내한다."""
    with audio_recovery_server() as fixture:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{fixture.url}/app")
            _control(page, "record-start")
            with page.expect_response("**/__fixture/record-short"):
                _control(page, "record-short")
            expect(page.locator("#recordingOverlayCountdown")).to_contain_text(
                "전사 최소 길이까지"
            )
            if automatic:
                _control(page, "record-auto-stop")
            else:
                stop = page.get_by_role("button", name="녹음 끝내기", exact=True)
                stop.click()
                dialog = page.get_by_role("dialog", name="지금 녹음을 끝낼까요?")
                keep = dialog.get_by_role("button", name="계속 녹음")
                expect(keep).to_be_focused()
                keep.click()
                expect(dialog).to_be_hidden()
                assert fixture.stop_posts == 0
                stop.click()
                dialog.get_by_role("button", name="저장하고 끝내기").click()
            notice = page.locator("#recordingOutcome")
            expect(notice).to_contain_text("녹음 파일을 저장했습니다")
            expect(notice).to_contain_text("30초 미만이어서 자동 전사하지 않습니다")
            expect(notice).to_contain_text("capture_1.wav")
            expect(notice.get_by_role("link")).to_have_count(0)
            assert fixture.stop_posts == int(not automatic)
            with page.expect_response("**/system/open-audio-folder?short_recordings=true"):
                notice.get_by_role("button", name="짧은 녹음 폴더 열기").click()
            assert fixture.folder_opens == 1
            notice.get_by_role("button", name="녹음 결과 닫기").click()
            page.reload()
            expect(notice).to_be_hidden()
            expect(page.locator("#homeShortFolder")).to_be_visible()
        finally:
            context.close()


def test_audio_warnings_fit_mobile_viewport(browser: Browser) -> None:
    """좁은 화면에서도 오류·재시도 버튼이 잘리지 않는다."""
    with audio_recovery_server() as fixture:
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        try:
            page.goto(f"{fixture.url}/app/viewer/{MEETING_ID}")
            expect(page.locator("#viewerMicRetry")).to_be_visible()
            for selector in ("#viewerMicTranscription", "#viewerMicRetry"):
                bounds = page.locator(selector).bounding_box()
                assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
            _control(page, "record-start")
            _control(page, "record-failed")
            expect(page.locator("#recordingFailure")).to_be_visible()
            bounds = page.locator("#recordingFailure").bounding_box()
            assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
        finally:
            context.close()
