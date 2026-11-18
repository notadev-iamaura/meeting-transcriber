"""기기의 Zoom 창이나 회의 기록을 열지 않고 녹음 출처와 회의명 연결을 검증한다."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from config import AppConfig, PathsConfig
from core.job_queue import AsyncJobQueue, JobQueue
from core.watcher import FolderWatcher, _PreparedReadyAction, _ReadyActionKind
from core.zoom_topic import (
    ZoomTopicObservation,
    normalize_zoom_topic,
    read_recording_topic,
    save_recording_topic,
)
from steps.recorder import AudioDevice, AudioRecorder, RecordingState
from steps.zoom_activity import ZoomActivityError, ZoomActivityResult
from steps.zoom_detector import ZoomDetector


@pytest.mark.parametrize(
    "value", [None, "Zoom Meeting", "Zoom 회의", "  ", "bad\ntitle", "a" * 201]
)
def test_only_an_explicit_displayable_topic_is_accepted(value: object) -> None:
    """일반 창 이름·빈 이름·다중 행은 제목으로 저장하지 않는다."""
    assert not normalize_zoom_topic(value)
    assert normalize_zoom_topic("  주간 제품회의  ") == "주간 제품회의"


@pytest.mark.parametrize("second", ["conflict", "multiple", "unchanged", "denied"])
async def test_auto_recording_topic_follows_active_zoom_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second: str
) -> None:
    """권한 없음·서로 다른 두 주제·복수 창을 관측하면 제목이 비어 있어야 한다."""
    detector = ZoomDetector(AppConfig(paths=PathsConfig(base_dir=str(tmp_path))))
    observed: list[ZoomTopicObservation] = []

    def on_change(active: bool) -> None:
        if active:
            observed.append(detector.observe_recording_topic())

    detector.on_meeting_change(on_change)
    samples = {
        "conflict": [("회의 A", False, True), ("회의 B", False, True)],
        "multiple": [("회의 A", False, True), ("", True, True)],
        "unchanged": [("회의 A", False, True), ("회의 A", False, True)],
        "denied": [("", False, False)],
    }
    reader = AsyncMock(side_effect=samples[second])
    monkeypatch.setattr(detector._activity_checker, "read_meeting_topic", reader)
    steps = iter([True, True, False])
    monkeypatch.setattr(
        detector, "_check_zoom_process", AsyncMock(side_effect=lambda: next(steps))
    )

    async def tick(_: float) -> None:
        if not detector.is_meeting_active:
            detector._is_running = False

    monkeypatch.setattr(asyncio, "sleep", tick)
    detector._is_running = True
    await detector._poll_loop()
    assert reader.await_count == len(samples[second])
    assert observed[0].title == ("회의 A" if second == "unchanged" else "")
    assert detector._recording_topic is None


async def test_auto_capture_and_delayed_watcher_preserve_actual_and_edited_titles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """캡처를 마감한 뒤 별도 watcher가 신규 등록할 때만 같은 파일의 주제를 반영한다."""
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    recorder = AudioRecorder(config=config)
    mid = "meeting_20261118_120000"
    recorder._temp_dir.mkdir(parents=True)
    file = recorder._temp_dir / f"{mid}.wav"
    with wave.open(str(file), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\x00\x20" * (31 * 16000))
    recorder._state = RecordingState.RECORDING
    recorder._current_file = file
    recorder._process = Mock()
    recorder._start_time = time.time() - 31
    monkeypatch.setattr(recorder, "_terminate_one_ffmpeg", AsyncMock())
    topic = ZoomTopicObservation()
    recorder.track_zoom_topic(topic)
    topic.observe("제품 검토 회의")
    result = await recorder._terminate_ffmpeg()
    assert result is not None and result.file_path.is_file()
    raw_queue = JobQueue(tmp_path / "jobs.db")
    queue = AsyncJobQueue(raw_queue)
    await queue.initialize()
    try:
        watcher = FolderWatcher(queue, config=config)
        fp = watcher._fingerprint(result.file_path.stat())
        action = _PreparedReadyAction(
            result.file_path, mid, fp, None, _ReadyActionKind.ACCEPT, "", False
        )
        job_id = await watcher._apply_ready_action(action)
        assert job_id is not None and raw_queue.get_job(job_id).title == "제품 검토 회의"
        raw_queue.update_title(mid, "직접 편집한 이름")
        from dataclasses import replace

        assert (
            await watcher._apply_ready_action(replace(action, existing=raw_queue.get_job(job_id)))
            is None
        )
        assert raw_queue.get_job(job_id).title == "직접 편집한 이름"
    finally:
        await queue.close()


@pytest.mark.parametrize("mode", ["zoom", "mic", "process_only", "denied"])
async def test_manual_recording_requires_system_track_active_zoom_and_explicit_topic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """자동 녹음이 꺼져 있어도 수동 시작부터 파일 게시까지 출처가 확인된 제목만 남긴다."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routers.recording import router

    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    config.recording.auto_record_on_zoom = False
    recorder = AudioRecorder(config)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.recorder = recorder
    mid = "meeting_20261118_120000"

    async def start_synthetic(meeting_id: str) -> None:
        recorder._current_device = AudioDevice(
            index=0, name="Test input", is_blackhole=mode != "mic"
        )
        recorder._current_file = recorder._temp_dir / f"{meeting_id}.wav"
        with wave.open(str(recorder._current_file), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\x00\x20" * (31 * 16000))
        recorder._process = Mock(pid=1, returncode=None)

    checked = asyncio.Event()

    async def read_audio() -> ZoomActivityResult:
        checked.set()
        if mode == "process_only":
            raise ZoomActivityError("오디오 확인 불가")
        return ZoomActivityResult(active=True, source="coreaudio")

    reader = AsyncMock(return_value=("수동 녹음의 회의", False, mode != "denied"))
    fallback = AsyncMock(return_value=ZoomActivityResult(active=True, source="pgrep"))
    monkeypatch.setattr(
        "steps.zoom_activity.ZoomAudioActivityChecker._check_coreaudio", lambda _: read_audio()
    )
    monkeypatch.setattr("steps.zoom_activity.ZoomAudioActivityChecker.read_meeting_topic", reader)
    monkeypatch.setattr(
        "steps.zoom_activity.ZoomAudioActivityChecker._check_process_fallback", fallback
    )
    monkeypatch.setattr(recorder, "_start_singletrack_recording", start_synthetic)
    monkeypatch.setattr(recorder, "_terminate_one_ffmpeg", AsyncMock())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        # 앞선 통화는 자동으로 녹음됐고, 사용자가 버튼으로 먼저 정지했다.
        stale_session = ZoomTopicObservation(title="수동 녹음의 회의")
        await recorder.start_recording(meeting_id="meeting_20261118_112000")
        recorder.track_zoom_topic(stale_session)
        recorder._start_time = time.time() - 31
        assert (await client.post("/api/recording/stop")).status_code == 200
        assert (
            await client.post("/api/recording/start", json={"meeting_id": mid})
        ).status_code == 200
        if mode != "mic":
            await asyncio.wait_for(checked.wait(), 2)
        else:
            assert recorder._manual_zoom_topic_task is None
        # 앞 회의의 종료 알림은 사용자가 새로 시작한 녹음을 종료할 수 없다.
        assert await recorder.stop_recording(zoom_session=stale_session) is None
        assert recorder.is_recording
        recorder._start_time = time.time() - 31
        assert (await client.post("/api/recording/stop")).status_code == 200
    file = config.paths.resolved_audio_input_dir / f"{mid}.wav"
    identity = FolderWatcher._fingerprint(file.stat()).as_recovery_tuple()
    assert read_recording_topic(config, file, identity) == (
        "수동 녹음의 회의" if mode == "zoom" else ""
    )
    assert reader.await_count == int(mode in {"zoom", "denied"})
    fallback.assert_not_awaited()
    assert not recorder.is_recording and recorder._manual_zoom_topic_task is None


@pytest.mark.parametrize("second_meeting", ["different_topic", "new_audio_session"])
async def test_manual_session_does_not_name_recordings_spanning_multiple_meetings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second_meeting: str
) -> None:
    """서로 다른 주제나 Zoom 오디오 재시작을 관측하면 수동 녹음의 제목을 비운다."""
    recorder = AudioRecorder(AppConfig(paths=PathsConfig(base_dir=str(tmp_path))))
    recorder._state = RecordingState.RECORDING
    active = [True, False, True] if second_meeting == "new_audio_session" else [True, True]
    monkeypatch.setattr(
        "steps.zoom_activity.ZoomAudioActivityChecker.check",
        AsyncMock(side_effect=[ZoomActivityResult(active=i, source="coreaudio") for i in active]),
    )
    monkeypatch.setattr(
        "steps.zoom_activity.ZoomAudioActivityChecker.read_meeting_topic",
        AsyncMock(side_effect=[("회의 A", False, True), ("회의 B", False, True)]),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    observation = ZoomTopicObservation()
    await recorder._monitor_manual_zoom_topic(observation)
    assert observation.ambiguous and not observation.title


def test_changed_file_or_missing_metadata_cannot_reuse_topic(tmp_path: Path) -> None:
    """동일 파일명으로 교체하거나 녹음 후 내용을 바꾸면 날짜 표시로 폴백한다."""
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    path = tmp_path / "meeting_20261118_120001.wav"
    path.write_bytes(b"original audio")
    identity = path.stat()
    fingerprint = FolderWatcher._fingerprint(identity).as_recovery_tuple()
    assert not read_recording_topic(config, path, fingerprint)
    save_recording_topic(config, path, identity, "원래 회의")
    assert read_recording_topic(config, path, fingerprint) == "원래 회의"
    new_file = tmp_path / "new.wav"
    new_file.write_bytes(b"new audio")
    os.replace(new_file, path)
    assert not read_recording_topic(config, path, fingerprint)
    assert not read_recording_topic(
        config, path, FolderWatcher._fingerprint(path.stat()).as_recovery_tuple()
    )


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("swiftc"), reason="macOS Swift 필요"
)
def test_zoom_native_topic_extraction_ignores_chat_and_unrelated_windows(tmp_path: Path) -> None:
    """네이티브 필드 탐색기를 합성 한·영 Zoom 창 구조에 그대로 적용한다."""
    source = (
        Path(__file__).resolve().parents[1] / "scripts/zoom_audio_activity.swift"
    ).read_text()
    declarations = source.split(
        'if CommandLine.arguments.count == 2 && CommandLine.arguments[1] == "--meeting-topic"'
    )[0]
    fixture = r"""
struct Node {
    let role: String
    var title: String = ""
    var value: String = ""
    var children: [Node] = []
}
func extract(_ nodes: [Node]) -> (String, Bool) {
    extractZoomTopic(windows: nodes, text: { node, attr in
        switch attr {
        case kAXRoleAttribute: return node.role
        case kAXTitleAttribute: return node.title
        case kAXValueAttribute: return node.value
        default: return ""
        }
    }, childrenOf: { $0.children })
}
let labels = [
    Node(role: "AXStaticText", value: "주제:"),
    Node(role: "AXStaticText", value: "출시 점검")
]
let info = Node(role: "AXGroup", title: "회의 정보", children: labels)
let chat = Node(role: "AXList", children: [Node(role: "AXStaticText", title: "Meeting topic:", value: "wrong")])
let window = Node(role: "AXWindow", title: "Zoom 회의", children: [chat, info])
let separate = Node(role: "AXWindow", title: "Meeting information", children: [info])
precondition(extract([window, separate]) == ("출시 점검", false))
precondition(extract([separate]) == ("", false))
precondition(extract([window, window]) == ("", true))
let field = Node(role: "AXStaticText", title: "Meeting topic:", value: "Roadmap")
let en = Node(role: "AXWindow", title: "Zoom Meeting", children: [field])
precondition(extract([en]) == ("Roadmap", false))
precondition(extract([Node(role: "AXWindow", title: "Zoom Meeting", children: [info, field])]) == ("", true))
precondition(extract([Node(role: "AXWindow", title: "Zoom Meeting", children: [chat])]) == ("", false))
"""
    swift = tmp_path / "fixture.swift"
    swift.write_text(declarations + fixture)
    binary = tmp_path / "topic"
    subprocess.run(
        ["swiftc", "-module-cache-path", str(tmp_path / "cache"), str(swift), "-o", str(binary)],
        check=True,
        timeout=90,
    )
    subprocess.run([str(binary)], check=True, timeout=5)
