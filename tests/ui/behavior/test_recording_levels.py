"""분리 녹음 중에만 실제 채널 음량이 보이는지 확인한다."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.ui


def test_channels_show_distinct_recording_levels_and_reset_on_stop(
    page: Page, ui_bulk_base_url: str
) -> None:
    """시스템 채널이 무음이어도 마이크 수치는 별도로 보이며, 정지하면 숨겨진다."""
    page.goto(f"{ui_bulk_base_url}/app")
    page.locator("#listContent .meeting-item").first.wait_for()
    page.evaluate(
        """() => {
            document.dispatchEvent(new CustomEvent("ws:recording_started"));
            document.dispatchEvent(new CustomEvent("ws:recording_duration", {
                detail: {duration_seconds: 2, audio_levels: {mic_dbfs: -21, system_dbfs: -90}}
            }));
        }"""
    )
    levels = page.get_by_role("group", name="채널별 입력 음량")
    expect(levels).to_be_visible()
    expect(levels.get_by_label("내 마이크")).to_have_js_property("value", -21)
    expect(levels.get_by_label("Zoom·시스템")).to_have_js_property("value", -72)
    expect(page.locator("#recordingOverlay .recording-wave")).to_be_hidden()
    page.evaluate("""() => document.dispatchEvent(new CustomEvent("ws:recording_stopped"))""")
    expect(levels).to_be_hidden()
