"""통합 전사 모델·자격 증명·외부 전송 동의 API 계약을 검증한다.

Keychain, 오디오 파일 검사, A/B runner는 모두 mock으로 격리한다.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from api.routers import ab_tests, settings, transcription_models
from config import AppConfig, PathsConfig, ServerConfig
from core.transcription_models import OPENAI_TRANSCRIPTION_ID
from security import openai_keychain
from security.openai_keychain import OpenAICredentialStatus


def _make_app(tmp_path: Path, *, host: str = "127.0.0.1") -> FastAPI:
    """필요한 세 라우터만 등록한 외부 의존성 없는 FastAPI 앱을 만든다."""
    config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host=host),
    )
    api_router = APIRouter(prefix="/api")
    api_router.include_router(transcription_models.router)
    api_router.include_router(settings.router)
    api_router.include_router(ab_tests.router)
    app = FastAPI()
    app.include_router(api_router)
    app.state.config = config
    app.state.ws_manager = None
    app.state.model_manager = None
    return app


def _client(app: FastAPI) -> TestClient:
    """실제 앱과 같은 loopback Host를 쓰는 테스트 클라이언트를 만든다."""
    return TestClient(app, base_url="http://127.0.0.1")


def _write_test_config(tmp_path: Path) -> Path:
    """설정 PUT이 실제 프로젝트 config.yaml을 건드리지 않게 임시 YAML을 만든다."""
    path = tmp_path / "config.yaml"
    path.write_text(
        'stt:\n  provider: "local"\n  openai_model: "gpt-4o-transcribe-diarize"\n',
        encoding="utf-8",
    )
    return path


def _openai_ab_body(*, consent: bool) -> dict[str, Any]:
    """OpenAI와 로컬 STT를 비교하는 최소 A/B 요청을 만든다."""
    return {
        "source_meeting_id": "meeting-does-not-need-to-exist",
        "variant_a": {
            "label": "OpenAI",
            "model_id": OPENAI_TRANSCRIPTION_ID,
            "backend": "openai",
        },
        "variant_b": {
            "label": "Local",
            "model_id": "whisper-large-v3-turbo",
            "backend": "mlx",
        },
        "allow_diarize_rerun": False,
        "external_upload_confirmed": consent,
    }


def test_통합_카탈로그는_local을_기본값으로_반환한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """새 설치에서는 외부 전송 모델이 아니라 local 모델이 기본이다."""
    monkeypatch.setattr(
        openai_keychain,
        "get_status",
        lambda: OpenAICredentialStatus(configured=False, source=None),
    )
    app = _make_app(tmp_path)

    with _client(app) as client:
        response = client.get("/api/transcription-models")

    assert response.status_code == 200
    payload = response.json()
    assert payload["default_model_id"] == "local"
    local = next(model for model in payload["models"] if model["id"] == "local")
    external = next(model for model in payload["models"] if model["id"] == OPENAI_TRANSCRIPTION_ID)
    assert local["is_default"] is True
    assert local["external_upload"] is False
    assert external["is_default"] is False
    assert external["external_upload"] is True
    assert external["available"] is False
    assert payload["openai_key"] == {"configured": False, "source": None}


def test_credential_PUT은_키를_응답이나_카탈로그에_반사하지_않는다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """write-only 키는 저장 helper에만 전달되고 모든 HTTP 응답에서 빠진다."""
    secret = "sk-do-not-reflect-this-secret-1234567890"
    set_key = MagicMock()
    monkeypatch.setattr(openai_keychain, "set_api_key", set_key)
    monkeypatch.setattr(
        openai_keychain,
        "get_status",
        lambda: OpenAICredentialStatus(configured=True, source="keychain"),
    )
    app = _make_app(tmp_path)

    with _client(app) as client:
        response = client.put("/api/openai-credentials", json={"api_key": secret})
        catalog = client.get("/api/transcription-models")

    assert response.status_code == 200
    assert response.json() == {"configured": True, "source": "keychain"}
    set_key.assert_called_once_with(secret)
    assert secret not in response.text
    assert secret not in catalog.text
    assert "api_key" not in response.text
    assert "api_key" not in catalog.text


def test_credential_DELETE는_Keychain만_삭제하고_env_폴백_상태를_반환한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """삭제 뒤 env가 유효하면 configured/source 상태가 이를 정확히 표현한다."""
    delete_key = MagicMock(return_value=True)
    monkeypatch.setattr(openai_keychain, "delete_api_key", delete_key)
    monkeypatch.setattr(
        openai_keychain,
        "get_status",
        lambda: OpenAICredentialStatus(configured=True, source="environment"),
    )
    app = _make_app(tmp_path)

    with _client(app) as client:
        response = client.delete("/api/openai-credentials")

    assert response.status_code == 200
    assert response.json() == {"configured": True, "source": "environment"}
    delete_key.assert_called_once_with()


def test_OpenAI가_기본값이면_credential_DELETE를_거부한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """다음 자동 전사가 즉시 실패하는 설정 조합을 만들지 않는다."""
    delete_key = MagicMock(return_value=True)
    monkeypatch.setattr(openai_keychain, "delete_api_key", delete_key)
    app = _make_app(tmp_path)
    app.state.config.stt.provider = "openai"

    with _client(app) as client:
        response = client.delete("/api/openai-credentials")

    assert response.status_code == 409
    assert "로컬" in response.json()["detail"]
    delete_key.assert_not_called()


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({"api_key": "short"}, 400),
        ({"api_key": "sk-valid-mock-value-1234567890", "echo_secret": True}, 400),
        ({"api_key": "x" * 513}, 400),
    ],
)
def test_credential_요청은_키를_반사하지_않고_잘못된_입력을_거부한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    expected_status: int,
) -> None:
    """형식/extra 검증 오류의 HTTP 응답에도 API 키 원문을 포함하지 않는다."""
    original_set_key = openai_keychain.set_api_key
    set_key = MagicMock(side_effect=original_set_key)
    monkeypatch.setattr(openai_keychain, "set_api_key", set_key)
    app = _make_app(tmp_path)

    with _client(app) as client:
        response = client.put("/api/openai-credentials", json=payload)

    assert response.status_code == expected_status
    assert str(payload.get("api_key", "")) not in response.text
    if "echo_secret" in payload:
        set_key.assert_not_called()
    else:
        set_key.assert_called_once()


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": {"nested": "sk-nested-secret-value-1234567890"}},
        ["sk-list-secret-value-1234567890"],
    ],
)
def test_credential_malformed_container도_비밀을_반사하지_않는다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: Any,
) -> None:
    """Pydantic 422 input 경로 없이 고정 오류로 잘못된 container를 거부한다."""
    set_key = MagicMock()
    monkeypatch.setattr(openai_keychain, "set_api_key", set_key)
    app = _make_app(tmp_path)

    with _client(app) as client:
        response = client.put("/api/openai-credentials", json=payload)

    assert response.status_code == 400
    assert "sk-" not in response.text
    set_key.assert_not_called()


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "attacker.example:8765"},
        {"origin": "https://attacker.example"},
    ],
)
def test_OpenAI_API는_악성_Host_Origin을_비밀_접근_전에_거부한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
) -> None:
    """DNS rebinding/교차 출처 요청은 Keychain 값을 읽거나 쓰지 못한다."""
    get_status = MagicMock()
    set_key = MagicMock()
    monkeypatch.setattr(openai_keychain, "get_status", get_status)
    monkeypatch.setattr(openai_keychain, "set_api_key", set_key)
    app = _make_app(tmp_path)

    with _client(app) as client:
        response = client.put(
            "/api/openai-credentials",
            json={"api_key": "sk-valid-mock-value-1234567890"},
            headers=headers,
        )

    assert response.status_code == 403
    get_status.assert_not_called()
    set_key.assert_not_called()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/transcription-models", None),
        ("get", "/api/openai-credentials", None),
        ("put", "/api/openai-credentials", {"api_key": "sk-valid-mock-value-1234567890"}),
        ("delete", "/api/openai-credentials", None),
    ],
)
def test_OpenAI_API는_non_loopback_server에서_거부한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    """원격 bind 상태에서는 상태 조회와 비밀 변경 모두 허용하지 않는다."""
    get_status = MagicMock()
    set_key = MagicMock()
    delete_key = MagicMock()
    monkeypatch.setattr(openai_keychain, "get_status", get_status)
    monkeypatch.setattr(openai_keychain, "set_api_key", set_key)
    monkeypatch.setattr(openai_keychain, "delete_api_key", delete_key)
    app = _make_app(tmp_path, host="0.0.0.0")

    with _client(app) as client:
        response = client.request(method, path, json=body)

    assert response.status_code == 403
    get_status.assert_not_called()
    set_key.assert_not_called()
    delete_key.assert_not_called()


def test_설정에서_OpenAI_전환은_동의를_키_조회보다_먼저_요구한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """동의가 없으면 Keychain·YAML 쓰기 없이 즉시 거부한다."""
    config_path = _write_test_config(tmp_path)
    monkeypatch.setattr(settings, "_get_config_path", lambda: config_path)
    get_status = MagicMock(return_value=OpenAICredentialStatus(configured=True, source="keychain"))
    monkeypatch.setattr(openai_keychain, "get_status", get_status)
    original = config_path.read_text(encoding="utf-8")
    app = _make_app(tmp_path)

    with _client(app) as client:
        response = client.put("/api/settings", json={"stt_provider": "openai"})

    assert response.status_code == 400
    assert "동의" in response.json()["detail"]
    get_status.assert_not_called()
    assert config_path.read_text(encoding="utf-8") == original
    assert app.state.config.stt.provider == "local"


def test_설정에서_OpenAI_전환은_등록된_키도_요구한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """동의만 있고 자격 증명이 없으면 설정을 영속화하지 않는다."""
    config_path = _write_test_config(tmp_path)
    monkeypatch.setattr(settings, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(
        openai_keychain,
        "get_status",
        lambda: OpenAICredentialStatus(configured=False, source=None),
    )
    original = config_path.read_text(encoding="utf-8")
    app = _make_app(tmp_path)

    with _client(app) as client:
        response = client.put(
            "/api/settings",
            json={"stt_provider": "openai", "external_upload_confirmed": True},
        )

    assert response.status_code == 400
    assert "API 키" in response.json()["detail"]
    assert config_path.read_text(encoding="utf-8") == original
    assert app.state.config.stt.provider == "local"


def test_설정에서_동의와_키가_있을_때만_OpenAI를_기본값으로_저장한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """검증을 통과한 전환은 provider만 저장하고 동의 플래그는 YAML에 남기지 않는다."""
    config_path = _write_test_config(tmp_path)
    monkeypatch.setattr(settings, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(
        openai_keychain,
        "get_status",
        lambda: OpenAICredentialStatus(configured=True, source="keychain"),
    )
    app = _make_app(tmp_path)

    with _client(app) as client:
        response = client.put(
            "/api/settings",
            json={
                "stt_provider": "openai",
                "stt_openai_model": "gpt-4o-transcribe-diarize",
                "external_upload_confirmed": True,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["settings"]["stt_provider"] == "openai"
    assert app.state.config.stt.provider == "openai"
    persisted = config_path.read_text(encoding="utf-8")
    assert 'provider: "openai"' in persisted
    assert "external_upload_confirmed" not in persisted
    assert "api_key" not in persisted


def test_OpenAI_기본값_전환과_Keychain_삭제는_공용잠금으로_직렬화한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """동시 요청 뒤 provider=openai/key 없음 불변식 위반을 만들지 않는다."""
    config_path = _write_test_config(tmp_path)
    monkeypatch.setattr(settings, "_get_config_path", lambda: config_path)
    key_present = {"value": True}
    monkeypatch.setattr(
        openai_keychain,
        "get_status",
        lambda: OpenAICredentialStatus(
            configured=key_present["value"],
            source="keychain" if key_present["value"] else None,
        ),
    )
    delete_key = MagicMock(side_effect=lambda: key_present.update(value=False))
    monkeypatch.setattr(openai_keychain, "delete_api_key", delete_key)
    writer_entered = threading.Event()
    writer_release = threading.Event()
    original_writer = settings._atomic_write_text

    def _blocked_writer(path: Path, content: str) -> None:
        writer_entered.set()
        if not writer_release.wait(timeout=5):
            raise RuntimeError("설정 writer barrier timeout")
        original_writer(path, content)

    monkeypatch.setattr(settings, "_atomic_write_text", _blocked_writer)
    app = _make_app(tmp_path)
    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/settings",
            "headers": [(b"host", b"127.0.0.1")],
            "app": app,
        }
    )

    async def _scenario() -> None:
        update_task = asyncio.create_task(
            settings.update_settings(
                request,
                settings.SettingsUpdateRequest(
                    stt_provider="openai",
                    external_upload_confirmed=True,
                ),
            )
        )
        assert await asyncio.to_thread(writer_entered.wait, 5)
        delete_task = asyncio.create_task(transcription_models.delete_openai_credential(request))
        await asyncio.sleep(0)
        delete_key.assert_not_called()
        writer_release.set()
        await update_task
        with pytest.raises(HTTPException) as captured:
            await delete_task
        assert captured.value.status_code == 409

    asyncio.run(_scenario())

    assert app.state.config.stt.provider == "openai"
    assert key_present["value"] is True
    delete_key.assert_not_called()


def test_STT_AB는_외부전송_동의를_파일과_Keychain_접근보다_먼저_검증한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """동의 없는 OpenAI 비교 요청은 source meeting 존재 여부조차 확인하지 않는다."""
    validate_meeting = MagicMock()
    get_status = MagicMock()
    loopback_check = MagicMock()
    busy_check = MagicMock()
    monkeypatch.setattr(ab_tests, "_validate_meeting_exists", validate_meeting)
    monkeypatch.setattr(ab_tests, "is_ab_test_busy", busy_check)
    monkeypatch.setattr(openai_keychain, "get_status", get_status)
    monkeypatch.setattr(transcription_models, "require_loopback_server", loopback_check)
    app = _make_app(tmp_path)

    with _client(app) as client:
        response = client.post("/api/ab-tests/stt", json=_openai_ab_body(consent=False))

    assert response.status_code == 400
    assert "동의" in response.json()["detail"]
    validate_meeting.assert_not_called()
    get_status.assert_not_called()
    loopback_check.assert_not_called()
    busy_check.assert_not_called()


def test_STT_AB는_키_누락도_파일_접근_전에_거부한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """동의 후에도 Keychain 상태 preflight를 통과해야 source를 검사한다."""
    validate_meeting = MagicMock()
    monkeypatch.setattr(ab_tests, "_validate_meeting_exists", validate_meeting)
    monkeypatch.setattr(
        openai_keychain,
        "get_status",
        lambda: OpenAICredentialStatus(configured=False, source=None),
    )
    app = _make_app(tmp_path)

    with _client(app) as client:
        response = client.post("/api/ab-tests/stt", json=_openai_ab_body(consent=True))

    assert response.status_code == 400
    assert "API 키" in response.json()["detail"]
    validate_meeting.assert_not_called()
