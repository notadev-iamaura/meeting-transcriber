"""사용자 편집 가능 프롬프트와 용어집 API 라우터."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core import user_settings as _user_settings
from core.user_settings import (
    PromptEntry,
    PromptsData,
    UserSettingsError,
    UserSettingsIOError,
    UserSettingsLockError,
    UserSettingsValidationError,
    VocabularyTerm,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# =========================================================================
# 사용자 편집 가능 프롬프트 & 용어집 엔드포인트
# =========================================================================
# core/user_settings.py를 통해 프롬프트(보정/요약/채팅)와 고유명사 용어집을
# 동적으로 관리한다. 기존 /api/settings와 달리 config.yaml을 수정하지 않고
# ~/.meeting-transcriber/user_data/ 아래 JSON 파일로 영속화한다.
# =========================================================================

# --- 요청/응답 스키마 ---


class PromptEntryPayload(BaseModel):
    """프롬프트 항목 요청/응답 페이로드."""

    system_prompt: str = Field(..., min_length=20, max_length=8000)
    updated_at: str | None = None


class PromptsPayload(BaseModel):
    """프롬프트 전체 응답 페이로드."""

    schema_version: int = 1
    corrector: PromptEntryPayload
    summarizer: PromptEntryPayload
    chat: PromptEntryPayload
    updated_at: str | None = None


class PromptsResponse(BaseModel):
    """GET /api/prompts 응답."""

    prompts: PromptsPayload


class PromptsUpdateRequest(BaseModel):
    """PUT /api/prompts 요청 (부분 업데이트 지원)."""

    corrector: PromptEntryPayload | None = None
    summarizer: PromptEntryPayload | None = None
    chat: PromptEntryPayload | None = None


class VocabularyTermPayload(BaseModel):
    """용어 항목 응답 페이로드."""

    id: str
    term: str
    aliases: list[str] = Field(default_factory=list)
    category: str | None = None
    note: str | None = None
    enabled: bool = True
    created_at: str | None = None


class VocabularyResponse(BaseModel):
    """GET /api/vocabulary 응답."""

    terms: list[VocabularyTermPayload]
    total: int
    schema_version: int = 1


class VocabularyAddRequest(BaseModel):
    """POST /api/vocabulary/terms 요청."""

    term: str = Field(..., min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    category: str | None = Field(default=None, max_length=50)
    note: str | None = Field(default=None, max_length=500)
    enabled: bool = True


class VocabularyUpdateRequest(BaseModel):
    """PUT /api/vocabulary/terms/{id} 요청 (부분 업데이트)."""

    term: str | None = Field(default=None, min_length=1, max_length=100)
    aliases: list[str] | None = Field(default=None, max_length=20)
    category: str | None = Field(default=None, max_length=50)
    note: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None


# --- 변환 헬퍼 ---


def _prompts_to_payload(data: PromptsData) -> PromptsPayload:
    """PromptsData → API 응답 페이로드로 변환한다."""
    raw = data.model_dump(mode="json")
    return PromptsPayload(
        schema_version=raw["schema_version"],
        corrector=PromptEntryPayload(**raw["corrector"]),
        summarizer=PromptEntryPayload(**raw["summarizer"]),
        chat=PromptEntryPayload(**raw["chat"]),
        updated_at=raw.get("updated_at"),
    )


def _term_to_payload(term: VocabularyTerm) -> VocabularyTermPayload:
    """VocabularyTerm → API 응답 페이로드로 변환한다."""
    return VocabularyTermPayload(**term.model_dump(mode="json"))


def _map_user_settings_error(exc: UserSettingsError) -> HTTPException:
    """저장소 예외를 HTTPException으로 매핑한다.

    Args:
        exc: UserSettingsError 인스턴스

    Returns:
        적절한 상태 코드와 한국어 메시지가 담긴 HTTPException
    """
    if isinstance(exc, UserSettingsValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, UserSettingsLockError):
        return HTTPException(status_code=503, detail=f"{exc}. 잠시 후 다시 시도해 주세요.")
    if isinstance(exc, UserSettingsIOError):
        return HTTPException(status_code=500, detail=str(exc))
    return HTTPException(status_code=500, detail=f"내부 저장소 오류: {exc}")


# --- 프롬프트 엔드포인트 ---


@router.get("/prompts", response_model=PromptsResponse)
async def get_prompts() -> PromptsResponse:
    """현재 저장된 프롬프트 3종(보정/요약/채팅)을 조회한다.

    Returns:
        PromptsResponse

    Raises:
        HTTPException: I/O 실패(500)
    """
    try:
        data = _user_settings.load_prompts()
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e
    return PromptsResponse(prompts=_prompts_to_payload(data))


@router.put("/prompts", response_model=PromptsResponse)
async def update_prompts(body: PromptsUpdateRequest) -> PromptsResponse:
    """프롬프트를 부분 업데이트한다 (전달된 필드만 반영).

    Args:
        body: 변경할 프롬프트 (선택적 필드)

    Returns:
        업데이트된 PromptsResponse

    Raises:
        HTTPException: 검증 실패(400), 락 타임아웃(503), I/O 실패(500)
    """
    try:
        current = _user_settings.load_prompts()
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e

    updates: dict[str, Any] = {}
    if body.corrector is not None:
        updates["corrector"] = PromptEntry(system_prompt=body.corrector.system_prompt)
    if body.summarizer is not None:
        updates["summarizer"] = PromptEntry(system_prompt=body.summarizer.system_prompt)
    if body.chat is not None:
        updates["chat"] = PromptEntry(system_prompt=body.chat.system_prompt)

    if not updates:
        return PromptsResponse(prompts=_prompts_to_payload(current))

    try:
        merged = current.model_copy(update=updates)
        saved = _user_settings.save_prompts(merged)
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"프롬프트 검증 실패: {e}") from e

    logger.info("프롬프트 업데이트: %s", ", ".join(sorted(updates.keys())))
    return PromptsResponse(prompts=_prompts_to_payload(saved))


@router.post("/prompts/reset", response_model=PromptsResponse)
async def reset_prompts() -> PromptsResponse:
    """프롬프트를 공장 기본값으로 복원한다.

    Returns:
        복원된 PromptsResponse

    Raises:
        HTTPException: I/O 실패(500)
    """
    try:
        data = _user_settings.reset_prompts_to_default()
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e
    return PromptsResponse(prompts=_prompts_to_payload(data))


# --- 용어집 엔드포인트 ---


@router.get("/vocabulary", response_model=VocabularyResponse)
async def get_vocabulary() -> VocabularyResponse:
    """전체 용어집을 조회한다.

    Returns:
        VocabularyResponse
    """
    try:
        data = _user_settings.load_vocabulary()
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e
    return VocabularyResponse(
        terms=[_term_to_payload(t) for t in data.terms],
        total=len(data.terms),
        schema_version=data.schema_version,
    )


@router.post(
    "/vocabulary/terms",
    response_model=VocabularyTermPayload,
    status_code=201,
)
async def add_vocabulary_term_endpoint(
    body: VocabularyAddRequest,
) -> VocabularyTermPayload:
    """용어를 추가한다 (ULID는 서버가 생성).

    Args:
        body: 추가할 용어 정보

    Returns:
        생성된 VocabularyTermPayload

    Raises:
        HTTPException: 중복·최대 개수 초과·검증 실패(400), 저장 실패(500)
    """
    try:
        new_term = _user_settings.add_vocabulary_term(
            term=body.term,
            aliases=body.aliases,
            category=body.category,
            note=body.note,
            enabled=body.enabled,
        )
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e
    return _term_to_payload(new_term)


@router.put("/vocabulary/terms/{term_id}", response_model=VocabularyTermPayload)
async def update_vocabulary_term_endpoint(
    term_id: str,
    body: VocabularyUpdateRequest,
) -> VocabularyTermPayload:
    """용어를 부분 업데이트한다.

    Args:
        term_id: 대상 용어의 ULID
        body: 변경할 필드 (선택적)

    Returns:
        업데이트된 VocabularyTermPayload

    Raises:
        HTTPException: 대상 없음/중복/검증 실패(400), 저장 실패(500)
    """
    try:
        updated = _user_settings.update_vocabulary_term(
            term_id=term_id,
            term=body.term,
            aliases=body.aliases,
            category=body.category,
            note=body.note,
            enabled=body.enabled,
        )
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e
    return _term_to_payload(updated)


@router.delete("/vocabulary/terms/{term_id}", status_code=204)
async def delete_vocabulary_term_endpoint(term_id: str) -> None:
    """용어를 삭제한다.

    Args:
        term_id: 삭제할 용어의 ULID

    Raises:
        HTTPException: 대상 없음(400), 저장 실패(500)
    """
    try:
        _user_settings.delete_vocabulary_term(term_id)
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e


@router.post("/vocabulary/reset", response_model=VocabularyResponse)
async def reset_vocabulary_endpoint() -> VocabularyResponse:
    """용어집을 공장 기본값(빈 목록)으로 복원한다.

    Returns:
        복원된 VocabularyResponse
    """
    try:
        data = _user_settings.reset_vocabulary_to_default()
    except UserSettingsError as e:
        raise _map_user_settings_error(e) from e
    return VocabularyResponse(
        terms=[_term_to_payload(t) for t in data.terms],
        total=len(data.terms),
        schema_version=data.schema_version,
    )
