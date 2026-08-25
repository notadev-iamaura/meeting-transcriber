"""로컬/외부 전사 모델 카탈로그와 OpenAI 자격 증명 API."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

from api.openai_settings_guard import get_openai_settings_mutation_lock
from core.stt_model_registry import STT_MODELS
from core.transcription_models import (
    LOCAL_TRANSCRIPTION_ID,
    OPENAI_TRANSCRIPTION_ID,
    default_selection_id,
    is_loopback_host,
)
from security import openai_keychain

router = APIRouter()


class OpenAICredentialInfo(BaseModel):
    """비밀값을 포함하지 않는 OpenAI 자격 증명 상태."""

    configured: bool
    source: str | None = None


class TranscriptionModelInfo(BaseModel):
    """설정과 파일별 비교 UI에서 공통으로 사용하는 모델 항목."""

    id: str
    label: str
    provider: str
    model: str
    external_upload: bool
    available: bool
    unavailable_reason: str | None = None
    is_default: bool = False


class TranscriptionModelsResponse(BaseModel):
    """통합 전사 모델 카탈로그 응답."""

    default_model_id: str
    openai_key: OpenAICredentialInfo
    models: list[TranscriptionModelInfo]


def _loopback_authority(value: str, *, origin: bool) -> bool:
    """Host/Origin 헤더가 문법적으로 안전한 loopback authority인지 확인한다."""
    if not value or any(char in value for char in ("\\", "\x00", "\r", "\n")):
        return False
    try:
        parsed = urlsplit(value if origin else f"//{value}")
        if origin and parsed.scheme not in {"http", "https"}:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        hostname = (parsed.hostname or "").lower()
        # 잘못된 port 문법도 여기서 강제로 평가해 거부한다.
        _port = parsed.port
    except ValueError:
        return False
    return is_loopback_host(hostname)


def require_loopback_server(
    config: Any,
    request: Request,
    *,
    feature_label: str = "OpenAI 기능",
) -> None:
    """bind 설정과 실제 Host/Origin이 모두 loopback일 때만 민감 기능을 허용한다."""
    host = str(getattr(getattr(config, "server", None), "host", ""))
    request_host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if (
        not is_loopback_host(host.lower())
        or not _loopback_authority(request_host, origin=False)
        or (origin is not None and not _loopback_authority(origin, origin=True))
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"{feature_label}은 로컬 주소(127.0.0.1, localhost 또는 ::1)에서 "
                "연 앱 요청으로만 사용할 수 있습니다."
            ),
        )


def _credential_info() -> OpenAICredentialInfo:
    """Keychain helper의 공개 가능한 상태만 응답 모델로 변환한다."""
    status = openai_keychain.get_status()
    return OpenAICredentialInfo(configured=status.configured, source=status.source)


def _local_label(config: Any) -> str:
    """현재 활성 로컬 모델의 사람이 읽을 수 있는 라벨을 반환한다."""
    configured = str(config.stt.model_name)
    for spec in STT_MODELS:
        if configured in {spec.id, spec.model_path, spec.hf_source} or spec.id in configured:
            return f"이 Mac에서 처리 · {spec.label}"
    return f"이 Mac에서 처리 · {configured.rsplit('/', maxsplit=1)[-1]}"


@router.get("/transcription-models", response_model=TranscriptionModelsResponse)
async def list_transcription_models(request: Request) -> TranscriptionModelsResponse:
    """로컬 기본 모델과 회의용 OpenAI 모델을 별도 카탈로그로 반환한다."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")
    require_loopback_server(config, request)
    credential = _credential_info()
    default_id = default_selection_id(getattr(config.stt, "provider", "local"))
    models = [
        TranscriptionModelInfo(
            id=LOCAL_TRANSCRIPTION_ID,
            label=_local_label(config),
            provider="local",
            model=config.stt.model_name,
            external_upload=False,
            available=True,
            is_default=default_id == LOCAL_TRANSCRIPTION_ID,
        ),
        TranscriptionModelInfo(
            id=OPENAI_TRANSCRIPTION_ID,
            label="OpenAI 서버에서 처리 · GPT-4o Transcribe Diarize · 외부 전송",
            provider="openai",
            model=getattr(
                config.stt,
                "openai_model",
                "gpt-4o-transcribe-diarize",
            ),
            external_upload=True,
            available=credential.configured,
            unavailable_reason=None
            if credential.configured
            else "설정에서 OpenAI API 키를 등록하세요.",
            is_default=default_id == OPENAI_TRANSCRIPTION_ID,
        ),
    ]
    return TranscriptionModelsResponse(
        default_model_id=default_id,
        openai_key=credential,
        models=models,
    )


@router.get("/openai-credentials", response_model=OpenAICredentialInfo)
async def get_openai_credential_status(request: Request) -> OpenAICredentialInfo:
    """API 키 값 없이 Keychain/환경변수 설정 상태만 반환한다."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")
    require_loopback_server(config, request)
    return _credential_info()


@router.put("/openai-credentials", response_model=OpenAICredentialInfo)
async def put_openai_credential(
    request: Request,
    body: Annotated[Any, Body()],
) -> OpenAICredentialInfo:
    """API 키를 응답에 반사하지 않고 macOS Keychain에 저장한다."""
    if (
        not isinstance(body, dict)
        or set(body) != {"api_key"}
        or not isinstance(body.get("api_key"), str)
    ):
        raise HTTPException(status_code=400, detail="OpenAI API 키 요청 형식이 올바르지 않습니다.")
    async with get_openai_settings_mutation_lock(request):
        config = getattr(request.app.state, "config", None)
        if config is None:
            raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")
        require_loopback_server(config, request)
        try:
            await asyncio.to_thread(
                openai_keychain.set_api_key,
                body["api_key"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except openai_keychain.OpenAIKeychainError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return _credential_info()


@router.delete("/openai-credentials", response_model=OpenAICredentialInfo)
async def delete_openai_credential(request: Request) -> OpenAICredentialInfo:
    """Keychain 항목을 삭제하고 환경변수 폴백을 포함한 새 상태를 반환한다."""
    async with get_openai_settings_mutation_lock(request):
        config = getattr(request.app.state, "config", None)
        if config is None:
            raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")
        require_loopback_server(config, request)
        if getattr(config.stt, "provider", "local") == "openai":
            raise HTTPException(
                status_code=409,
                detail="기본 전사 모델을 로컬로 변경한 뒤 OpenAI API 키를 삭제해 주세요.",
            )
        try:
            await asyncio.to_thread(openai_keychain.delete_api_key)
        except openai_keychain.OpenAIKeychainError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return _credential_info()
