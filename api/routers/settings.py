"""시스템 설정 API 라우터."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.config_yaml import get_config_path as _get_config_path
from api.config_yaml import replace_yaml_value as _replace_yaml_value
from core.io_utils import atomic_write_text as _atomic_write_text

logger = logging.getLogger(__name__)

router = APIRouter()

_ALLOWED_MLX_MODELS = {
    "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit",
    "mlx-community/gemma-4-e4b-it-4bit",
    "mlx-community/gemma-4-e2b-it-4bit",
}

# BCP-47 언어 코드 화이트리스트 패턴 (보안: YAML 인젝션 차단)
# 예: "ko", "en", "en-US", "zh-Hant", "ja-JP-x-keb"
_STT_LANGUAGE_PATTERN = re.compile(r"^[a-zA-Z]{2,8}(-[a-zA-Z0-9]{2,8})*$")

# 프론트엔드 드롭다운용 모델 프리셋 (읽기 전용)
_AVAILABLE_MODELS = [
    {
        "id": "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit",
        "label": "EXAONE 3.5 7.8B (한국어 특화)",
        "size": "~5GB",
        "description": "LG AI Research가 한국어에 특화시킨 모델로, 회의록 보정·요약에 검증된 안정적인 선택입니다.",
    },
    {
        "id": "mlx-community/gemma-4-e4b-it-4bit",
        "label": "Gemma 4 E4B (다국어, 빠름)",
        "size": "~5.3GB",
        "description": "Google이 만든 최신 경량 모델로, EXAONE보다 약 50% 빠르며 다국어를 골고루 잘 처리합니다.",
    },
    {
        "id": "mlx-community/gemma-4-e2b-it-4bit",
        "label": "Gemma 4 E2B (경량)",
        "size": "~3GB",
        "description": "Gemma 4 의 가벼운 버전으로, 8GB RAM 환경에서도 안정적으로 동작합니다.",
    },
]


class SettingsResponse(BaseModel):
    """설정 응답 스키마.

    현재 시스템 설정값을 프론트엔드에 전달한다.

    Attributes:
        llm_backend: LLM 백엔드 ("mlx" 또는 "ollama")
        llm_mlx_model_name: MLX 모델명
        llm_temperature: 생성 온도 (0.0~2.0)
        llm_mlx_max_tokens: MLX 최대 생성 토큰
        llm_skip_steps: LLM 단계 스킵 여부 (pipeline.skip_llm_steps)
        stt_language: STT 언어 코드
        available_models: 선택 가능한 모델 프리셋 목록 (읽기 전용)
    """

    llm_backend: str = "mlx"
    llm_mlx_model_name: str = "mlx-community/gemma-4-e4b-it-4bit"
    llm_temperature: float = 0.0
    llm_mlx_max_tokens: int = 2000
    llm_skip_steps: bool = False
    stt_language: str = "ko"
    # 환각 필터 (hallucination_filter)
    hf_enabled: bool = True
    hf_no_speech_threshold: float = 0.9
    hf_compression_ratio_threshold: float = 2.4
    hf_repetition_threshold: int = 3
    # 데이터 라이프사이클
    lifecycle_enabled: bool = False
    lifecycle_hot_days: int = 30
    lifecycle_warm_days: int = 90
    lifecycle_cold_action: str = "delete_audio"
    lifecycle_interval_hours: int = 24
    lifecycle_run_on_startup: bool = False
    available_models: list[dict] = Field(default_factory=lambda: _AVAILABLE_MODELS)


class SettingsUpdateRequest(BaseModel):
    """설정 업데이트 요청 스키마.

    변경하려는 필드만 전송하면 된다 (부분 업데이트).

    Attributes:
        llm_backend: LLM 백엔드 ("mlx" 또는 "ollama")
        llm_mlx_model_name: MLX 모델명 (허용 목록 내)
        llm_temperature: 생성 온도 (0.0~2.0)
        llm_mlx_max_tokens: MLX 최대 생성 토큰 (100 이상)
        llm_skip_steps: LLM 단계 스킵 여부
        stt_language: STT 언어 코드
    """

    llm_backend: str | None = None
    llm_mlx_model_name: str | None = None
    llm_temperature: float | None = None
    llm_mlx_max_tokens: int | None = None
    llm_skip_steps: bool | None = None
    stt_language: str | None = None
    # 환각 필터
    hf_enabled: bool | None = None
    hf_no_speech_threshold: float | None = None
    hf_compression_ratio_threshold: float | None = None
    hf_repetition_threshold: int | None = None
    # 데이터 라이프사이클
    lifecycle_enabled: bool | None = None
    lifecycle_hot_days: int | None = None
    lifecycle_warm_days: int | None = None
    lifecycle_cold_action: str | None = None
    lifecycle_interval_hours: int | None = None
    lifecycle_run_on_startup: bool | None = None


class SettingsUpdateResponse(BaseModel):
    """설정 업데이트 응답 스키마.

    Attributes:
        settings: 업데이트된 설정값
        message: 결과 메시지
        changed_fields: 변경된 필드 목록
    """

    settings: SettingsResponse
    message: str = "설정이 저장되었습니다."
    changed_fields: list[str] = Field(default_factory=list)


def _build_settings_response(config: Any) -> SettingsResponse:
    """현재 config 객체를 SettingsResponse로 변환한다."""
    return SettingsResponse(
        llm_backend=config.llm.backend,
        llm_mlx_model_name=config.llm.mlx_model_name,
        llm_temperature=config.llm.temperature,
        llm_mlx_max_tokens=config.llm.mlx_max_tokens,
        llm_skip_steps=config.pipeline.skip_llm_steps,
        stt_language=config.stt.language,
        hf_enabled=config.hallucination_filter.enabled,
        hf_no_speech_threshold=config.hallucination_filter.no_speech_threshold,
        hf_compression_ratio_threshold=config.hallucination_filter.compression_ratio_threshold,
        hf_repetition_threshold=config.hallucination_filter.repetition_threshold,
        lifecycle_enabled=config.lifecycle.enabled,
        lifecycle_hot_days=config.lifecycle.hot_days,
        lifecycle_warm_days=config.lifecycle.warm_days,
        lifecycle_cold_action=config.lifecycle.cold_action,
        lifecycle_interval_hours=config.lifecycle.interval_hours,
        lifecycle_run_on_startup=config.lifecycle.run_on_startup,
        available_models=_AVAILABLE_MODELS,
    )


@router.get("/settings", response_model=SettingsResponse)
async def get_settings(request: Request) -> SettingsResponse:
    """현재 시스템 설정을 반환한다.

    app.state.config에서 설정값을 읽어 SettingsResponse로 매핑한다.

    Args:
        request: FastAPI Request 객체

    Returns:
        현재 설정값

    Raises:
        HTTPException: 설정이 초기화되지 않았을 때 (503)
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="서버 설정이 초기화되지 않았습니다.",
        )

    return _build_settings_response(config)


@router.put("/settings", response_model=SettingsUpdateResponse)
async def update_settings(
    request: Request,
    body: SettingsUpdateRequest,
) -> SettingsUpdateResponse:
    """시스템 설정을 업데이트한다.

    전달된 필드만 config.yaml에 반영하고 런타임 config도 갱신한다.
    모델이 변경된 경우 안내 메시지를 포함한다.

    Args:
        request: FastAPI Request 객체
        body: 변경할 설정 필드 (Optional — 전달된 것만 반영)

    Returns:
        업데이트 결과 (변경된 설정, 메시지, 변경 필드 목록)

    Raises:
        HTTPException: 검증 실패(400), 설정 미초기화(503), 파일 저장 실패(500)
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="서버 설정이 초기화되지 않았습니다.",
        )

    # 변경할 필드만 추출 (None이 아닌 값)
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return SettingsUpdateResponse(
            settings=_build_settings_response(config),
            message="변경할 설정이 없습니다.",
            changed_fields=[],
        )

    # === 입력 검증 ===
    if "llm_backend" in updates and updates["llm_backend"] not in ("mlx", "ollama"):
        raise HTTPException(
            status_code=400,
            detail="llm_backend는 'mlx' 또는 'ollama'만 허용됩니다.",
        )

    if "llm_mlx_model_name" in updates:
        if updates["llm_mlx_model_name"] not in _ALLOWED_MLX_MODELS:
            raise HTTPException(
                status_code=400,
                detail=f"허용되지 않은 모델입니다. 허용 목록: {sorted(_ALLOWED_MLX_MODELS)}",
            )

    if "llm_temperature" in updates:
        temp = updates["llm_temperature"]
        if not (0.0 <= temp <= 2.0):
            raise HTTPException(
                status_code=400,
                detail="llm_temperature는 0.0~2.0 범위여야 합니다.",
            )

    if "llm_mlx_max_tokens" in updates and updates["llm_mlx_max_tokens"] < 100:
        raise HTTPException(
            status_code=400,
            detail="llm_mlx_max_tokens는 100 이상이어야 합니다.",
        )

    if "stt_language" in updates:
        lang = updates["stt_language"]
        # 보안: BCP-47 형식만 허용. 따옴표·개행·#·콜론 등이 들어가면
        # _replace_yaml_value 가 그대로 config.yaml 에 삽입해 YAML 파일이
        # 손상될 수 있음 (예: "en\": y\n#" 같은 입력으로 부팅 불가).
        # 따라서 알파벳 + 선택적 BCP-47 서브태그(`-`로 분리)만 허용한다.
        if not lang or not _STT_LANGUAGE_PATTERN.match(lang):
            raise HTTPException(
                status_code=400,
                detail=(
                    "stt_language 는 BCP-47 언어 코드 형식만 허용됩니다 "
                    "(예: ko, en, en-US, zh-Hant)."
                ),
            )

    # 환각 필터 파라미터 검증 (Pydantic Field 의 ge/le 와 동일 범위)
    if "hf_no_speech_threshold" in updates:
        v = updates["hf_no_speech_threshold"]
        if not (0.0 <= v <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="hf_no_speech_threshold 는 0.0~1.0 범위여야 합니다.",
            )
    if "hf_compression_ratio_threshold" in updates:
        v = updates["hf_compression_ratio_threshold"]
        if not (1.0 <= v <= 10.0):
            raise HTTPException(
                status_code=400,
                detail="hf_compression_ratio_threshold 는 1.0~10.0 범위여야 합니다.",
            )
    if "hf_repetition_threshold" in updates:
        v = updates["hf_repetition_threshold"]
        if not (isinstance(v, int) and 2 <= v <= 10):
            raise HTTPException(
                status_code=400,
                detail="hf_repetition_threshold 는 2~10 범위의 정수여야 합니다.",
            )

    if "lifecycle_hot_days" in updates:
        v = updates["lifecycle_hot_days"]
        if not (isinstance(v, int) and 1 <= v <= 3650):
            raise HTTPException(
                status_code=400,
                detail="lifecycle_hot_days 는 1~3650 범위의 정수여야 합니다.",
            )
    if "lifecycle_warm_days" in updates:
        v = updates["lifecycle_warm_days"]
        if not (isinstance(v, int) and 1 <= v <= 3650):
            raise HTTPException(
                status_code=400,
                detail="lifecycle_warm_days 는 1~3650 범위의 정수여야 합니다.",
            )
    effective_hot_days = updates.get("lifecycle_hot_days", config.lifecycle.hot_days)
    effective_warm_days = updates.get("lifecycle_warm_days", config.lifecycle.warm_days)
    if effective_warm_days < effective_hot_days:
        raise HTTPException(
            status_code=400,
            detail="lifecycle_warm_days 는 lifecycle_hot_days 이상이어야 합니다.",
        )
    if "lifecycle_cold_action" in updates and updates["lifecycle_cold_action"] not in (
        "delete_audio",
        "archive",
    ):
        raise HTTPException(
            status_code=400,
            detail="lifecycle_cold_action 은 'delete_audio' 또는 'archive' 만 허용됩니다.",
        )
    if "lifecycle_interval_hours" in updates:
        v = updates["lifecycle_interval_hours"]
        if not (isinstance(v, int) and 1 <= v <= 168):
            raise HTTPException(
                status_code=400,
                detail="lifecycle_interval_hours 는 1~168 범위의 정수여야 합니다.",
            )

    # === config.yaml 파일 업데이트 ===
    config_path = _get_config_path()
    # YAML 필드 매핑 (API 필드명 → YAML 경로)
    changed_fields: list[str] = []
    model_changed = False

    if "llm_backend" in updates:
        changed_fields.append("llm_backend")

    if "llm_mlx_model_name" in updates:
        changed_fields.append("llm_mlx_model_name")
        model_changed = True

    if "llm_temperature" in updates:
        changed_fields.append("llm_temperature")

    if "llm_mlx_max_tokens" in updates:
        changed_fields.append("llm_mlx_max_tokens")

    if "llm_skip_steps" in updates:
        changed_fields.append("llm_skip_steps")

    if "stt_language" in updates:
        changed_fields.append("stt_language")

    if "hf_enabled" in updates:
        changed_fields.append("hf_enabled")
    if "hf_no_speech_threshold" in updates:
        changed_fields.append("hf_no_speech_threshold")
    if "hf_compression_ratio_threshold" in updates:
        changed_fields.append("hf_compression_ratio_threshold")
    if "hf_repetition_threshold" in updates:
        changed_fields.append("hf_repetition_threshold")
    if "lifecycle_enabled" in updates:
        changed_fields.append("lifecycle_enabled")
    if "lifecycle_hot_days" in updates:
        changed_fields.append("lifecycle_hot_days")
    if "lifecycle_warm_days" in updates:
        changed_fields.append("lifecycle_warm_days")
    if "lifecycle_cold_action" in updates:
        changed_fields.append("lifecycle_cold_action")
    if "lifecycle_interval_hours" in updates:
        changed_fields.append("lifecycle_interval_hours")
    if "lifecycle_run_on_startup" in updates:
        changed_fields.append("lifecycle_run_on_startup")

    # YAML 파일 저장 (주석 보존: 정규식으로 해당 키의 값만 교체)
    try:
        try:
            with open(config_path, encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            logger.warning(f"config.yaml 미발견: {config_path}. 새로 생성합니다.")
            content = ""

        # 정규식 기반 값 교체 (모듈 레벨 _replace_yaml_value 사용 — 주석 보존)
        if "llm_backend" in updates:
            content = _replace_yaml_value(content, "llm", "backend", f'"{updates["llm_backend"]}"')
        if "llm_mlx_model_name" in updates:
            content = _replace_yaml_value(
                content, "llm", "mlx_model_name", f'"{updates["llm_mlx_model_name"]}"'
            )
        if "llm_temperature" in updates:
            content = _replace_yaml_value(
                content, "llm", "temperature", str(updates["llm_temperature"])
            )
        if "llm_mlx_max_tokens" in updates:
            content = _replace_yaml_value(
                content, "llm", "mlx_max_tokens", str(updates["llm_mlx_max_tokens"])
            )
        if "llm_skip_steps" in updates:
            val = "true" if updates["llm_skip_steps"] else "false"
            content = _replace_yaml_value(content, "pipeline", "skip_llm_steps", val)
        if "stt_language" in updates:
            content = _replace_yaml_value(
                content, "stt", "language", f'"{updates["stt_language"]}"'
            )
        if "hf_enabled" in updates:
            val = "true" if updates["hf_enabled"] else "false"
            content = _replace_yaml_value(content, "hallucination_filter", "enabled", val)
        if "hf_no_speech_threshold" in updates:
            content = _replace_yaml_value(
                content,
                "hallucination_filter",
                "no_speech_threshold",
                str(updates["hf_no_speech_threshold"]),
            )
        if "hf_compression_ratio_threshold" in updates:
            content = _replace_yaml_value(
                content,
                "hallucination_filter",
                "compression_ratio_threshold",
                str(updates["hf_compression_ratio_threshold"]),
            )
        if "hf_repetition_threshold" in updates:
            content = _replace_yaml_value(
                content,
                "hallucination_filter",
                "repetition_threshold",
                str(updates["hf_repetition_threshold"]),
            )
        if "lifecycle_enabled" in updates:
            val = "true" if updates["lifecycle_enabled"] else "false"
            content = _replace_yaml_value(content, "lifecycle", "enabled", val)
        if "lifecycle_hot_days" in updates:
            content = _replace_yaml_value(
                content, "lifecycle", "hot_days", str(updates["lifecycle_hot_days"])
            )
        if "lifecycle_warm_days" in updates:
            content = _replace_yaml_value(
                content, "lifecycle", "warm_days", str(updates["lifecycle_warm_days"])
            )
        if "lifecycle_cold_action" in updates:
            content = _replace_yaml_value(
                content, "lifecycle", "cold_action", f'"{updates["lifecycle_cold_action"]}"'
            )
        if "lifecycle_interval_hours" in updates:
            content = _replace_yaml_value(
                content,
                "lifecycle",
                "interval_hours",
                str(updates["lifecycle_interval_hours"]),
            )
        if "lifecycle_run_on_startup" in updates:
            val = "true" if updates["lifecycle_run_on_startup"] else "false"
            content = _replace_yaml_value(content, "lifecycle", "run_on_startup", val)

        # 원자적 쓰기 + .bak 백업 (도중 죽어도 config.yaml 손상 방지)
        await asyncio.to_thread(_atomic_write_text, config_path, content)
        logger.info(f"config.yaml 저장 완료 (원자적, 주석 보존). 변경 필드: {changed_fields}")
    except OSError as e:
        logger.exception(f"config.yaml 저장 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"설정 파일 저장에 실패했습니다: {e}",
        ) from e

    # === 런타임 config 갱신 ===
    if "llm_backend" in updates:
        new_llm = config.llm.model_copy(update={"backend": updates["llm_backend"]})
        config = config.model_copy(update={"llm": new_llm})

    if "llm_mlx_model_name" in updates:
        new_llm = config.llm.model_copy(update={"mlx_model_name": updates["llm_mlx_model_name"]})
        config = config.model_copy(update={"llm": new_llm})

    if "llm_temperature" in updates:
        new_llm = config.llm.model_copy(update={"temperature": updates["llm_temperature"]})
        config = config.model_copy(update={"llm": new_llm})

    if "llm_mlx_max_tokens" in updates:
        new_llm = config.llm.model_copy(update={"mlx_max_tokens": updates["llm_mlx_max_tokens"]})
        config = config.model_copy(update={"llm": new_llm})

    if "llm_skip_steps" in updates:
        new_pipeline = config.pipeline.model_copy(
            update={"skip_llm_steps": updates["llm_skip_steps"]}
        )
        config = config.model_copy(update={"pipeline": new_pipeline})

    if "stt_language" in updates:
        new_stt = config.stt.model_copy(update={"language": updates["stt_language"]})
        config = config.model_copy(update={"stt": new_stt})

    # 환각 필터 런타임 갱신
    hf_updates: dict[str, Any] = {}
    if "hf_enabled" in updates:
        hf_updates["enabled"] = updates["hf_enabled"]
    if "hf_no_speech_threshold" in updates:
        hf_updates["no_speech_threshold"] = updates["hf_no_speech_threshold"]
    if "hf_compression_ratio_threshold" in updates:
        hf_updates["compression_ratio_threshold"] = updates["hf_compression_ratio_threshold"]
    if "hf_repetition_threshold" in updates:
        hf_updates["repetition_threshold"] = updates["hf_repetition_threshold"]
    if hf_updates:
        new_hf = config.hallucination_filter.model_copy(update=hf_updates)
        config = config.model_copy(update={"hallucination_filter": new_hf})

    lifecycle_updates: dict[str, Any] = {}
    if "lifecycle_enabled" in updates:
        lifecycle_updates["enabled"] = updates["lifecycle_enabled"]
    if "lifecycle_hot_days" in updates:
        lifecycle_updates["hot_days"] = updates["lifecycle_hot_days"]
    if "lifecycle_warm_days" in updates:
        lifecycle_updates["warm_days"] = updates["lifecycle_warm_days"]
    if "lifecycle_cold_action" in updates:
        lifecycle_updates["cold_action"] = updates["lifecycle_cold_action"]
    if "lifecycle_interval_hours" in updates:
        lifecycle_updates["interval_hours"] = updates["lifecycle_interval_hours"]
    if "lifecycle_run_on_startup" in updates:
        lifecycle_updates["run_on_startup"] = updates["lifecycle_run_on_startup"]
    if lifecycle_updates:
        new_lifecycle = config.lifecycle.model_copy(update=lifecycle_updates)
        config = config.model_copy(update={"lifecycle": new_lifecycle})

    # app.state.config 갱신
    request.app.state.config = config

    lifecycle_scheduler = getattr(request.app.state, "lifecycle_scheduler", None)
    if lifecycle_updates and lifecycle_scheduler is not None:
        await lifecycle_scheduler.update_config(config)

    # 응답 메시지 구성
    message = "설정이 저장되었습니다."
    if model_changed:
        message += " 모델 변경은 다음 LLM 호출 시 적용됩니다."

    return SettingsUpdateResponse(
        settings=_build_settings_response(config),
        message=message,
        changed_fields=changed_fields,
    )
