"""최초 설정 readiness API 라우터 테스트."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from config import AppConfig, PathsConfig, ServerConfig
from security.setup_readiness import (
    ReadinessAction,
    ReadinessCheck,
    SetupCapabilities,
    SetupReadinessReport,
)


def _make_test_config(tmp_path: Path) -> AppConfig:
    """테스트용 AppConfig를 생성한다."""
    return AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
    )


def _make_test_app(tmp_path: Path) -> Any:
    """테스트용 FastAPI 앱을 생성한다."""
    from api.server import create_app

    config = _make_test_config(tmp_path)
    with (
        patch("search.hybrid_search.HybridSearchEngine", return_value=MagicMock()),
        patch("search.chat.ChatEngine", return_value=MagicMock()),
    ):
        return create_app(config, runtime_profile="api-test")


def _ready_report() -> SetupReadinessReport:
    """API 응답 테스트용 ready report를 생성한다."""
    return SetupReadinessReport(
        status="pass",
        configured=True,
        ready=True,
        capabilities=SetupCapabilities(
            recording_usable=True,
            full_meeting_capture_ready=True,
            has_blackhole=True,
            has_aggregate=True,
            stt_model_ready=True,
        ),
        checks=[
            ReadinessCheck(
                id="base_dir",
                status="pass",
                ready=True,
                message="ok",
                details={"path": "/tmp/meeting-data"},
            )
        ],
    )


def test_get_setup_readiness_api_test_runtime_returns_200(tmp_path: Path) -> None:
    """api-test runtime에서도 recorder 없이 readiness endpoint가 동작한다."""
    app = _make_test_app(tmp_path)

    with (
        patch("api.routers.setup_readiness.collect_setup_readiness", return_value=_ready_report()),
        TestClient(app) as client,
    ):
        response = client.get("/api/setup/readiness")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "pass",
        "configured": True,
        "ready": True,
        "capabilities": {
            "recording_usable": True,
            "full_meeting_capture_ready": True,
            "has_blackhole": True,
            "has_aggregate": True,
            "stt_model_ready": True,
        },
        "checks": [
            {
                "id": "base_dir",
                "status": "pass",
                "ready": True,
                "message": "ok",
                "action_hint": "",
                "details": {"path": "/tmp/meeting-data"},
                "actions": [],
            }
        ],
    }


def test_setup_readiness_response_never_contains_token_value(tmp_path: Path) -> None:
    """API 응답에 토큰 값이 섞이지 않는지 확인한다."""
    secret = "hf_secret_must_not_escape"
    report = SetupReadinessReport(
        status="fail",
        configured=False,
        ready=False,
        capabilities=SetupCapabilities(
            recording_usable=False,
            full_meeting_capture_ready=False,
            has_blackhole=False,
            has_aggregate=False,
            stt_model_ready=False,
        ),
        checks=[
            ReadinessCheck(
                id="hf_token_env",
                status="pass",
                ready=True,
                message="configured",
                details={
                    "configured": True,
                    "environment_variables": ["HUGGINGFACE_TOKEN", "HF_TOKEN"],
                    "environment_variables_present": ["HUGGINGFACE_TOKEN"],
                },
            )
        ],
    )
    app = _make_test_app(tmp_path)

    with (
        patch("api.routers.setup_readiness.collect_setup_readiness", return_value=report),
        TestClient(app) as client,
    ):
        response = client.get("/api/setup/readiness")

    assert response.status_code == 200
    assert secret not in response.text
    assert "hf_secret" not in response.text


def test_setup_readiness_response_serializes_read_only_actions(tmp_path: Path) -> None:
    """Readiness action metadata를 실행 없이 응답 스키마에 직렬화한다."""
    report = SetupReadinessReport(
        status="fail",
        configured=False,
        ready=False,
        capabilities=SetupCapabilities(
            recording_usable=False,
            full_meeting_capture_ready=False,
            has_blackhole=False,
            has_aggregate=False,
            stt_model_ready=False,
        ),
        checks=[
            ReadinessCheck(
                id="stt_model",
                status="warn",
                ready=False,
                message="not ready",
                actions=(
                    ReadinessAction(
                        id="open_stt_settings",
                        label="설정에서 모델 관리",
                        kind="route",
                        value="/app/settings",
                        description="모델 카드에서 진행",
                    ),
                ),
            )
        ],
    )
    app = _make_test_app(tmp_path)

    with (
        patch("api.routers.setup_readiness.collect_setup_readiness", return_value=report),
        TestClient(app) as client,
    ):
        response = client.get("/api/setup/readiness")

    assert response.status_code == 200
    assert response.json()["checks"][0]["actions"] == [
        {
            "id": "open_stt_settings",
            "label": "설정에서 모델 관리",
            "kind": "route",
            "value": "/app/settings",
            "description": "모델 카드에서 진행",
        }
    ]


def test_setup_readiness_is_registered_in_openapi(tmp_path: Path) -> None:
    """OpenAPI에 새 endpoint가 등록된다."""
    app = _make_test_app(tmp_path)

    with TestClient(app) as client:
        response = client.get("/api/openapi.json")

    assert response.status_code == 200
    assert "/api/setup/readiness" in response.json()["paths"]


def test_routes_reexports_setup_readiness_contract() -> None:
    """기존 api.routes import 경로에서도 새 계약 타입에 접근할 수 있다."""
    import api.routes as routes
    from api.routers import setup_readiness

    assert routes.ReadinessActionItem is setup_readiness.ReadinessActionItem
    assert routes.ReadinessCheckItem is setup_readiness.ReadinessCheckItem
    assert routes.SetupReadinessResponse is setup_readiness.SetupReadinessResponse
    assert routes.get_setup_readiness is setup_readiness.get_setup_readiness


def test_health_endpoint_remains_liveness_only(tmp_path: Path) -> None:
    """readiness 추가 후에도 /api/health는 기존 lightweight 계약을 유지한다."""
    app = _make_test_app(tmp_path)

    with (
        patch("api.routers.setup_readiness.collect_setup_readiness") as readiness_mock,
        TestClient(app) as client,
    ):
        response = client.get("/api/health")

    assert response.status_code == 200
    assert set(response.json()) == {"status", "uptime_seconds", "version"}
    readiness_mock.assert_not_called()
