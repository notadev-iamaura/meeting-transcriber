"""녹음 실패 안내가 새로고침과 늦은 정지 요청에도 유지되는 API 계약 검사."""

from __future__ import annotations

import asyncio
import shutil
import time
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers.recording import router
from config import AppConfig, PathsConfig
from core.zoom_topic import ZoomTopicObservation
from steps.recorder import AudioRecorder, FFmpegRecordError, RecordingState


def _capture(recorder: AudioRecorder, duration: int, sample: bytes) -> Path:
    """오디오 장치 대신 16 kHz PCM 신호를 쓰고 실제 저장·검사 경로에 연결한다."""
    recorder._temp_dir.mkdir(parents=True, exist_ok=True)
    source = recorder._temp_dir / "synthetic.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, 16_000, 0, "NONE", ""))
        wav.writeframes(sample * (duration * 16_000 // (len(sample) // 2)))
    recorder._state = RecordingState.RECORDING
    recorder._start_time = time.time() - duration
    recorder._meeting_id = source.stem
    recorder._current_file = source
    proc = MagicMock(returncode=None)
    proc.stdin.is_closing.return_value = False
    proc.stdin.drain = AsyncMock()
    proc.wait = AsyncMock(return_value=0)
    recorder._process = proc
    return source


def _api(recorder: AudioRecorder) -> FastAPI:
    """실제 종료·상태 라우터를 임시 디렉터리 녹음기에 연결한다."""
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.recorder = recorder
    return app


@pytest.mark.parametrize(
    ("duration", "sample", "saved", "reason"),
    [
        (31, b"\x00\x10\x00\xf0", True, ""),
        (30, b"\x00\x10\x00\xf0", True, ""),
        (29, b"\x00\x00", True, "30초"),
        (1, b"\x00\x10", True, "30초"),
        (31, b"\x00\x00", False, "소리가 감지되지"),
    ],
)
def test_saved_and_discarded_receipts_match_disk_and_are_repeatable(
    tmp_path: Path, duration: int, sample: bytes, saved: bool, reason: str
) -> None:
    """저장·짧은 녹음·무음에 대한 최초 응답과 재조회 값을 WAV 게시 결과와 비교한다."""
    recorder = AudioRecorder(config=AppConfig(paths=PathsConfig(base_dir=str(tmp_path))))
    source = _capture(recorder, duration, sample)
    audio = source.read_bytes()
    done = AsyncMock()
    recorder.on_recording_complete(done)
    with TestClient(_api(recorder)) as client:
        stale = client.post("/api/recording/stop", json={"meeting_id": "previous"})
        assert stale.status_code == 409
        assert recorder.is_recording and source.exists()
        response = client.post("/api/recording/stop", json={"meeting_id": source.stem})
        assert response.status_code == 200
        outcome = client.get("/api/recording/status").json()["last_stop"]
        repeat = client.post("/api/recording/stop", json={"meeting_id": source.stem}).json()
        assert outcome == {key: response.json()[key] for key in outcome}
        assert outcome == {key: repeat[key] for key in outcome}
        assert outcome["saved"] is saved and outcome["discarded"] is not saved
        assert outcome["files"] == ([source.name] if saved else [])
        assert outcome["expected_files"] == 1 and reason in outcome["reason"]
        assert not source.exists()
        short = duration < recorder._min_duration
        assert outcome["short_recording"] is short
        dest = (
            Path(response.json()["file_path"])
            if saved
            else recorder._audio_input_dir / source.name
        )
        assert dest.exists() is saved
        if saved:
            assert dest.read_bytes() == audio
        if short:
            assert dest.parent == recorder._config.paths.short_recordings_dir
            assert not (recorder._audio_input_dir / source.name).exists()
            with wave.open(str(dest), "rb") as wav:
                assert wav.getnframes() == duration * 16_000
            assert AudioRecorder(recorder._config).get_status()["has_short_recordings"]
        if saved and not short:
            done.assert_awaited_once()
        else:
            done.assert_not_called()


@pytest.mark.asyncio
async def test_zoom_stop_preserves_live_short_stereo_capture(tmp_path: Path) -> None:
    """실제 ffmpeg가 두 채널을 기록하는 동안 Zoom 종료를 전달하고 완성된 WAV를 디코드한다."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg 미설치")
    recorder = AudioRecorder(AppConfig(paths=PathsConfig(base_dir=str(tmp_path))))
    recorder._temp_dir.mkdir(parents=True)
    source = recorder._temp_dir / "zoom.wav"
    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-stats_period",
        "0.1",
        "-progress",
        "pipe:1",
        "-re",
        "-f",
        "lavfi",
        "-i",
        "aevalsrc=0.05*sin(440*2*PI*t)|0.1*sin(660*2*PI*t):s=16000",
        "-c:a",
        "pcm_s16le",
        str(source),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None
        await asyncio.wait_for(proc.stdout.readuntil(b"progress=continue"), timeout=8)
        recorder._state = RecordingState.RECORDING
        recorder._start_time = time.time() - 31  # 권한 확인을 포함한 벽시계는 30초를 넘었다.
        recorder._process = proc
        recorder._current_file = source
        recorder._meeting_id = source.stem
        session = ZoomTopicObservation()
        recorder.track_zoom_topic(session)
        done = AsyncMock()
        recorder.on_recording_complete(done)
        result = await recorder.stop_recording(zoom_session=session)
        assert result and result.short_recording
        assert result.file_path.parent == recorder._config.paths.short_recordings_dir
        with wave.open(str(result.file_path), "rb") as wav:
            assert wav.getnchannels() == 2 and 0 < wav.getnframes() < 30 * 16_000
            pcm = wav.readframes(wav.getnframes())
            assert pcm[0::4] != pcm[2::4] and any(pcm[0::4]) and any(pcm[2::4])
        done.assert_not_called()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


def test_short_multitrack_preserves_both_wavs_and_name_collision(tmp_path: Path) -> None:
    """기존 동명 원본을 보존하고 작은 마이크 및 상대 트랙의 PCM을 그대로 읽는다."""
    recorder = AudioRecorder(AppConfig(paths=PathsConfig(base_dir=str(tmp_path))))
    mic = _capture(recorder, 2, b"\x01\x00")
    system = mic.with_name("synthetic_system.wav")
    with wave.open(str(system), "wb") as wav:
        wav.setparams((1, 2, 16_000, 0, "NONE", ""))
        wav.writeframes(b"\x00\x10" * 32_000)
    recorder._processes = {"mic": recorder._process, "system": recorder._process}
    recorder._current_files = {"mic": mic, "system": system}
    folder = recorder._config.paths.short_recordings_dir
    folder.mkdir(parents=True)
    previous = folder / mic.name
    previous.write_bytes(b"previous")
    with TestClient(_api(recorder)) as client:
        receipt = client.post("/api/recording/stop").json()
    assert receipt["short_recording"] and receipt["saved"] and len(receipt["files"]) == 2
    assert previous.read_bytes() == b"previous"
    for name, sample in zip(receipt["files"], (b"\x01\x00", b"\x00\x10"), strict=True):
        with wave.open(str(folder / name), "rb") as wav:
            assert wav.readframes(32_000) == sample * 32_000


@pytest.mark.parametrize("two_tracks", [False, True])
def test_disk_write_error_reports_only_published_files_and_preserves_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, two_tracks: bool
) -> None:
    """파일 이동이 실패하면 남은 원본을 보존하고 이미 저장한 트랙만 응답에 포함한다."""
    recorder = AudioRecorder(config=AppConfig(paths=PathsConfig(base_dir=str(tmp_path))))
    source = _capture(recorder, 31, b"\x00\x10\x00\xf0")
    if two_tracks:
        system_file = source.with_name("synthetic_system.wav")
        system_file.write_bytes(source.read_bytes())
        recorder._processes = {"system": recorder._process, "mic": recorder._process}
        recorder._current_files = {"system": system_file, "mic": source}
    original_move = shutil.move

    def move(src: str, dst: str) -> str:
        if Path(src) == source:
            raise OSError("synthetic disk error")
        return original_move(src, dst)

    monkeypatch.setattr("steps.recorder.shutil.move", move)
    with TestClient(_api(recorder)) as client:
        response = client.post("/api/recording/stop")
        status = client.get("/api/recording/status").json()
        assert status["state"] == "idle" and source.exists()
        if two_tracks:
            assert response.status_code == 200
            outcome = status["last_stop"]
            assert outcome["expected_files"] == 2
            assert outcome["files"] == [system_file.name]
            assert source.name in outcome["reason"]
            assert (recorder._audio_input_dir / system_file.name).exists()
        else:
            assert response.status_code == 500
            assert status["last_stop"] is None
            assert status["last_error"]["partial_file_names"] == [source.name]
            assert not (recorder._audio_input_dir / source.name).exists()


def test_status_preserves_safe_failure_and_late_stop_does_not_claim_discard(
    tmp_path: Path,
) -> None:
    """실패 후 상태 조회는 부분 파일 안내를 반환하고 늦은 stop은 폐기로 응답하지 않는다."""
    recorder = AudioRecorder(config=AppConfig(paths=PathsConfig(base_dir=str(tmp_path))))
    partial = tmp_path / "capture.wav"
    partial.write_bytes(b"partial")
    failure = recorder._remember_recording_error("capture", [partial])
    with TestClient(_api(recorder)) as client:
        for _ in range(2):
            response = client.get("/api/recording/status")
            assert response.status_code == 200
            assert response.json()["state"] == "idle"
            assert response.json()["last_error"] == failure
            assert str(tmp_path) not in response.text
        response = client.post("/api/recording/stop")
        assert response.status_code == 500
        assert "폐기" not in response.text
        assert "discarded" not in response.json()
        assert client.get("/api/recording/status").json()["last_error"] == failure


def test_capture_error_http_response_does_not_expose_internal_path(tmp_path: Path) -> None:
    """예외에 경로나 시스템 오류가 있어도 API 오류 본문에는 사용자 안내만 반환한다."""
    recorder = AudioRecorder(config=AppConfig(paths=PathsConfig(base_dir=str(tmp_path))))
    recorder.stop_recording_with_outcome = AsyncMock(
        side_effect=FFmpegRecordError(f"internal detail {tmp_path}")
    )
    with TestClient(_api(recorder)) as client:
        response = client.post("/api/recording/stop")
    assert response.status_code == 500
    assert str(tmp_path) not in response.text


def test_acknowledgement_preserves_files_and_does_not_hide_new_failure(tmp_path: Path) -> None:
    """같은 오류를 여러 번 닫아도 안전하고, 이전 창이 새 오류를 닫을 수 없다."""
    recorder = AudioRecorder(config=AppConfig(paths=PathsConfig(base_dir=str(tmp_path))))
    partial = tmp_path / "partial.wav"
    partial.write_bytes(b"partial")
    failure = recorder._remember_recording_error("capture", [partial])
    identity = {key: failure[key] for key in ("meeting_id", "occurred_at")}
    endpoint = "/api/recording/error/acknowledge"
    with TestClient(_api(recorder)) as client:
        wrong_meeting = {**identity, "meeting_id": "other"}
        assert client.post(endpoint, json=wrong_meeting).json() == {"acknowledged": False}
        for _ in range(2):
            assert client.post(endpoint, json=identity).json() == {"acknowledged": True}
        acknowledged = client.get("/api/recording/status").json()["last_error"]
        assert acknowledged == {**failure, "acknowledged": True}
        new = recorder._remember_recording_error("capture", [partial])
        assert new["occurred_at"] != identity["occurred_at"]
        assert client.post(endpoint, json=identity).json() == {"acknowledged": False}
        assert client.get("/api/recording/status").json()["last_error"] == new
        assert not new["acknowledged"]
        assert partial.read_bytes() == b"partial"
