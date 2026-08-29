"""CLI 설정 파일 경로와 API 설정 쓰기 경계 테스트."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers.settings import router as settings_router
from api.routers.stt_models import router as stt_models_router
from api.server import create_app
from config import AppConfig, PathsConfig, STTConfig
from core.stt_model_status import ModelStatus


def _make_config(tmp_path: Path) -> AppConfig:
    """테스트용 설정 객체를 생성한다."""
    return AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path / "meeting-data")),
        stt=STTConfig(model_name="youngouk/whisper-medium-komixv2-mlx"),
    )


def test_create_app은_명시된_config_path를_앱상태에_보존한다(tmp_path: Path) -> None:
    """main.py가 전달한 경로를 후속 API writer가 읽을 수 있어야 한다."""
    custom_config = tmp_path / "custom.yaml"
    app = create_app(
        _make_config(tmp_path),
        runtime_profile="unit-test",
        config_path=custom_config,
    )

    assert app.state.config_path == custom_config.resolve()


def test_settings_writer는_앱상태의_명시_config_path에만_저장한다(tmp_path: Path) -> None:
    """설정 API가 기본 config.yaml 대신 --config 대상 파일을 갱신한다."""
    custom_config = tmp_path / "custom.yaml"
    custom_config.write_text("auto_processing:\n  max_items_per_run: 1\n", encoding="utf-8")
    app = FastAPI()
    app.include_router(settings_router, prefix="/api")
    app.state.config = _make_config(tmp_path)
    app.state.config_path = custom_config

    with TestClient(app) as client:
        response = client.put(
            "/api/settings",
            json={"auto_processing_max_items_per_run": 4},
        )

    assert response.status_code == 200, response.text
    assert "max_items_per_run: 4" in custom_config.read_text(encoding="utf-8")
    assert app.state.config.auto_processing.max_items_per_run == 4


def test_stt_writer는_앱상태의_명시_config_path에만_저장한다(tmp_path: Path) -> None:
    """STT 모델 활성화 writer도 settings writer와 같은 --config 파일을 쓴다."""
    custom_config = tmp_path / "custom.yaml"
    custom_config.write_text(
        'stt:\n  model_name: "youngouk/whisper-medium-komixv2-mlx"\n',
        encoding="utf-8",
    )
    app = FastAPI()
    app.include_router(stt_models_router, prefix="/api")
    app.state.config = _make_config(tmp_path)
    app.state.config_path = custom_config

    with (
        patch("api.routers.stt_models.get_model_status", return_value=ModelStatus.READY),
        TestClient(app) as client,
    ):
        response = client.post("/api/stt-models/seastar-medium-4bit/activate")

    assert response.status_code == 200, response.text
    assert "seastar-medium-ko-4bit" in custom_config.read_text(encoding="utf-8")
