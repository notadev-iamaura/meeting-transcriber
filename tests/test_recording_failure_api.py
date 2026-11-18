"""녹음 실패 안내가 새로고침과 늦은 정지 요청에도 유지되는 API 계약 검사."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers.recording import router
from config import AppConfig, PathsConfig
from steps.recorder import AudioRecorder, FFmpegRecordError


def test_status_preserves_safe_failure_and_late_stop_does_not_claim_discard(
    tmp_path: Path,
) -> None:
    """실패 후 상태 조회는 부분 파일 안내를 반환하고 늦은 stop은 폐기로 응답하지 않는다."""
    recorder = AudioRecorder(config=AppConfig(paths=PathsConfig(base_dir=str(tmp_path))))
    partial = tmp_path / "capture.wav"
    partial.write_bytes(b"partial")
    failure = recorder._remember_recording_error("capture", [partial])
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.recorder = recorder

    with TestClient(app) as client:
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
    recorder.stop_recording = AsyncMock(
        side_effect=FFmpegRecordError(f"internal detail {tmp_path}")
    )
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.recorder = recorder
    with TestClient(app) as client:
        response = client.post("/api/recording/stop")
    assert response.status_code == 500
    assert str(tmp_path) not in response.text
