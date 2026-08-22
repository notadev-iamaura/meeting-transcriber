"""로컬/외부 전사 모델 식별자와 안전한 선택 규칙."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any

LOCAL_TRANSCRIPTION_ID = "local"
OPENAI_PROVIDER = "openai"
OPENAI_TRANSCRIBE_DIARIZE_MODEL = "gpt-4o-transcribe-diarize"
OPENAI_TRANSCRIPTION_ID = f"{OPENAI_PROVIDER}:{OPENAI_TRANSCRIBE_DIARIZE_MODEL}"


@dataclass(frozen=True)
class TranscriptionSelection:
    """검증된 전사 처리 위치와 실제 모델 이름."""

    id: str
    provider: str
    model: str
    external_upload: bool


def selection_from_id(model_id: str, *, local_model: str) -> TranscriptionSelection:
    """공개 model_id를 화이트리스트 기반 실행 선택으로 변환한다."""
    if model_id == LOCAL_TRANSCRIPTION_ID:
        return TranscriptionSelection(
            id=LOCAL_TRANSCRIPTION_ID,
            provider="local",
            model=local_model,
            external_upload=False,
        )
    if model_id == OPENAI_TRANSCRIPTION_ID:
        return TranscriptionSelection(
            id=OPENAI_TRANSCRIPTION_ID,
            provider=OPENAI_PROVIDER,
            model=OPENAI_TRANSCRIBE_DIARIZE_MODEL,
            external_upload=True,
        )
    raise ValueError("지원하지 않는 전사 모델입니다.")


def default_selection_id(provider: str) -> str:
    """설정 provider를 통합 카탈로그 ID로 변환한다."""
    return OPENAI_TRANSCRIPTION_ID if provider == OPENAI_PROVIDER else LOCAL_TRANSCRIPTION_ID


def selection_from_config(config: Any) -> TranscriptionSelection:
    """현재 설정을 큐 등록 시점의 검증된 전사 선택으로 변환한다."""
    stt = getattr(config, "stt", config)
    provider = str(getattr(stt, "provider", "local") or "local")
    if provider == OPENAI_PROVIDER:
        model = str(
            getattr(stt, "openai_model", OPENAI_TRANSCRIBE_DIARIZE_MODEL)
            or OPENAI_TRANSCRIBE_DIARIZE_MODEL
        )
        if model != OPENAI_TRANSCRIBE_DIARIZE_MODEL:
            raise ValueError("허용되지 않은 OpenAI 전사 모델입니다.")
        return TranscriptionSelection(
            id=OPENAI_TRANSCRIPTION_ID,
            provider=OPENAI_PROVIDER,
            model=model,
            external_upload=True,
        )
    if provider != "local":
        raise ValueError("지원하지 않는 전사 provider입니다.")
    local_model = str(getattr(stt, "model_name", "") or "")
    if not local_model:
        raise ValueError("로컬 전사 모델이 설정되지 않았습니다.")
    return selection_from_id(LOCAL_TRANSCRIPTION_ID, local_model=local_model)


def selection_from_state_or_config(
    config: Any,
    state: Any,
    *,
    job: Any | None = None,
) -> TranscriptionSelection:
    """pipeline state → job snapshot → 현재 설정 순으로 전사 선택을 복원한다."""
    if state is not None:
        provider = (
            state.get("stt_provider")
            if isinstance(state, dict)
            else getattr(state, "stt_provider", "")
        )
        model = (
            state.get("stt_model") if isinstance(state, dict) else getattr(state, "stt_model", "")
        )
        if provider in {"local", OPENAI_PROVIDER} and isinstance(model, str) and model:
            if provider == OPENAI_PROVIDER and model != OPENAI_TRANSCRIBE_DIARIZE_MODEL:
                raise ValueError("파이프라인에 고정된 OpenAI 전사 모델이 허용 목록과 다릅니다.")
            return TranscriptionSelection(
                id=(
                    OPENAI_TRANSCRIPTION_ID
                    if provider == OPENAI_PROVIDER
                    else LOCAL_TRANSCRIPTION_ID
                ),
                provider=provider,
                model=model,
                external_upload=provider == OPENAI_PROVIDER,
            )
    if job is not None:
        provider = getattr(job, "stt_provider", "")
        model = getattr(job, "stt_model", "")
        if provider in {"local", OPENAI_PROVIDER} and isinstance(model, str) and model:
            if provider == OPENAI_PROVIDER and model != OPENAI_TRANSCRIBE_DIARIZE_MODEL:
                raise ValueError("큐에 고정된 OpenAI 전사 모델이 허용 목록과 다릅니다.")
            return TranscriptionSelection(
                id=(
                    OPENAI_TRANSCRIPTION_ID
                    if provider == OPENAI_PROVIDER
                    else LOCAL_TRANSCRIPTION_ID
                ),
                provider=str(provider),
                model=model,
                external_upload=provider == OPENAI_PROVIDER,
            )
        if bool(provider) != bool(model):
            raise ValueError("큐의 전사 provider/model snapshot이 손상되었습니다.")
    return selection_from_config(config)


def is_loopback_host(host: str) -> bool:
    """서버 bind host가 loopback인지 엄격하게 확인한다."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
