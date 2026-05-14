"""A/B 테스트 API 라우터."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api.dependencies import get_config as _get_config
from core import ab_test_store
from core.ab_test_runner import LlmScope, ModelSpec
from core.ab_test_runner import cancel_test as _runner_cancel_test
from core.ab_test_runner import delete_test as _runner_delete_test
from core.ab_test_runner import get_test_result as _runner_get_test_result
from core.ab_test_runner import list_tests as _runner_list_tests
from core.ab_test_runner import new_test_id as _runner_new_test_id
from core.ab_test_runner import run_llm_ab_test as _runner_run_llm_ab_test
from core.ab_test_runner import run_stt_ab_test as _runner_run_stt_ab_test

logger = logging.getLogger(__name__)

router = APIRouter()

_MEETING_ID_PATTERN = re.compile(r"^[\w\-\.]+$")


class ModelSpecPayload(BaseModel):
    """A/B 비교 대상 모델 스펙."""

    label: str
    model_id: str
    backend: Literal["mlx", "ollama"] = "mlx"


class LlmScopePayload(BaseModel):
    """LLM A/B 테스트 실행 범위."""

    correct: bool = True
    summarize: bool = True


class ABTestLLMRequest(BaseModel):
    """LLM A/B 테스트 요청 바디."""

    source_meeting_id: str
    variant_a: ModelSpecPayload
    variant_b: ModelSpecPayload
    scope: LlmScopePayload = Field(default_factory=LlmScopePayload)


class ABTestSTTRequest(BaseModel):
    """STT A/B 테스트 요청 바디."""

    source_meeting_id: str
    variant_a: ModelSpecPayload
    variant_b: ModelSpecPayload
    allow_diarize_rerun: bool = False


class ABTestStartedResponse(BaseModel):
    """A/B 테스트 시작 응답."""

    test_id: str
    status: str = "running"


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """백그라운드 A/B 태스크의 미처리 예외를 로깅한다."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            f"백그라운드 태스크 실패: {task.get_name()}: {exc}",
            exc_info=exc,
        )


def _validate_meeting_id(meeting_id: str) -> None:
    """meeting_id 형식을 검증한다."""
    if not _MEETING_ID_PATTERN.match(meeting_id):
        raise HTTPException(
            status_code=400,
            detail=f"유효하지 않은 회의 ID 형식입니다: {meeting_id}",
        )


def _validate_test_id(test_id: str) -> None:
    """test_id 를 화이트리스트로 검증한다."""
    if not ab_test_store.is_valid_test_id(test_id):
        raise HTTPException(
            status_code=400,
            detail=f"유효하지 않은 A/B 테스트 ID 형식입니다: {test_id}",
        )


def _validate_variant(variant: str) -> None:
    """variant 경로 파라미터가 ``a`` 또는 ``b`` 인지 검증한다."""
    if variant not in ("a", "b"):
        raise HTTPException(
            status_code=400,
            detail=f"variant 는 'a' 또는 'b' 만 허용됩니다: {variant}",
        )


def _get_ws_manager(request: Request) -> Any | None:
    """app.state 에서 WebSocket ConnectionManager 를 가져온다."""
    return getattr(request.app.state, "ws_manager", None)


async def _make_ab_broadcaster(request: Request) -> Any | None:
    """A/B 테스트 러너에 주입할 WebSocket broadcaster를 생성한다."""
    ws_manager = _get_ws_manager(request)
    if ws_manager is None:
        return None

    async def _broadcast(payload: dict[str, Any]) -> None:
        """러너 payload 를 WebSocket 이벤트로 브로드캐스트한다."""
        try:
            from api.websocket import EventType, WebSocketEvent

            event = WebSocketEvent(
                event_type=EventType.STEP_PROGRESS.value,
                data=payload,
            )
            await ws_manager.broadcast_event(event)
        except Exception as exc:  # noqa: BLE001 - 브로드캐스트 실패는 비치명적이다.
            logger.warning(f"A/B 테스트 WS 브로드캐스트 실패(무시): {exc}")

    return _broadcast


def _validate_meeting_exists(config: Any, meeting_id: str, test_type: str = "llm") -> None:
    """원본 회의가 A/B 테스트 입력 조건을 만족하는지 검증한다."""
    _validate_meeting_id(meeting_id)

    if test_type == "stt":
        wav = config.paths.resolved_audio_input_dir / f"{meeting_id}.wav"
        if not wav.exists():
            raise HTTPException(
                status_code=404,
                detail=f"오디오 파일을 찾을 수 없습니다: {meeting_id}",
            )
        return

    ckpt_dir = config.paths.resolved_checkpoints_dir / meeting_id
    out_dir = config.paths.resolved_outputs_dir / meeting_id
    if not ckpt_dir.exists() and not out_dir.exists():
        raise HTTPException(
            status_code=404,
            detail=f"원본 회의를 찾을 수 없습니다: {meeting_id}",
        )


_LLM_PRESETS = [
    {"label": "EXAONE 3.5 7.8B 4bit", "id": "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit"},
    {"label": "Gemma 4 E4B 4bit", "id": "mlx-community/gemma-4-e4b-it-4bit"},
    {"label": "Gemma 4 E2B 4bit", "id": "mlx-community/gemma-4-e2b-it-4bit"},
    {"label": "Gemma 4 E4B UD 4bit (Unsloth)", "id": "unsloth/gemma-4-E4B-it-UD-MLX-4bit"},
    {"label": "Gemma 4 E2B UD 4bit (Unsloth)", "id": "unsloth/gemma-4-E2B-it-UD-MLX-4bit"},
]


def _check_hf_cache_exists(repo_id: str) -> bool:
    """HF 캐시에 모델이 존재하는지 확인한다."""
    cache_dir = (
        Path.home()
        / ".cache"
        / "huggingface"
        / "hub"
        / f"models--{repo_id.replace('/', '--')}"
        / "snapshots"
    )
    if not cache_dir.exists():
        return False
    return any(snap.is_dir() and any(snap.iterdir()) for snap in cache_dir.iterdir())


@router.get(
    "/llm-models/available",
    summary="A/B 테스트용 LLM 모델 목록",
    description="로컬 HF 캐시 보유 여부를 포함한 LLM 프리셋 목록을 반환한다.",
)
async def list_available_llm_models() -> list[dict[str, Any]]:
    """프리셋 LLM 모델 목록과 로컬 보유 여부를 반환한다."""
    result = []
    for preset in _LLM_PRESETS:
        result.append(
            {
                "label": preset["label"],
                "model_id": preset["id"],
                "available": _check_hf_cache_exists(preset["id"]),
            }
        )
    return result


@router.post(
    "/ab-tests/llm",
    status_code=202,
    response_model=ABTestStartedResponse,
    summary="LLM A/B 테스트 시작",
    description="동일 회의에 대해 LLM 모델 2종의 교정/요약을 순차 비교 실행한다.",
)
async def start_llm_ab_test(
    body: ABTestLLMRequest,
    request: Request,
) -> ABTestStartedResponse:
    """LLM A/B 테스트를 백그라운드로 시작한다."""
    config = _get_config(request)

    if (
        body.variant_a.model_id == body.variant_b.model_id
        and body.variant_a.backend == body.variant_b.backend
    ):
        raise HTTPException(
            status_code=400,
            detail="variant_a 와 variant_b 가 동일합니다.",
        )

    _validate_meeting_exists(config, body.source_meeting_id)

    selected_id = _runner_new_test_id()
    broadcaster = await _make_ab_broadcaster(request)
    model_manager = getattr(request.app.state, "model_manager", None)

    task = asyncio.create_task(
        _runner_run_llm_ab_test(
            config=config,
            source_meeting_id=body.source_meeting_id,
            variant_a=ModelSpec(
                label=body.variant_a.label,
                model_id=body.variant_a.model_id,
                backend=body.variant_a.backend,
            ),
            variant_b=ModelSpec(
                label=body.variant_b.label,
                model_id=body.variant_b.model_id,
                backend=body.variant_b.backend,
            ),
            scope=LlmScope(
                correct=body.scope.correct,
                summarize=body.scope.summarize,
            ),
            ws_broadcaster=broadcaster,
            model_manager=model_manager,
            test_id=selected_id,
        ),
        name=f"ab-test-llm-{selected_id}",
    )
    task.add_done_callback(_log_task_exception)

    return ABTestStartedResponse(test_id=selected_id)


@router.post(
    "/ab-tests/stt",
    status_code=202,
    response_model=ABTestStartedResponse,
    summary="STT A/B 테스트 시작",
    description="동일 회의에 대해 STT 모델 2종의 전사 결과를 순차 비교 실행한다.",
)
async def start_stt_ab_test(
    body: ABTestSTTRequest,
    request: Request,
) -> ABTestStartedResponse:
    """STT A/B 테스트를 백그라운드로 시작한다."""
    config = _get_config(request)

    if body.variant_a.model_id == body.variant_b.model_id:
        raise HTTPException(
            status_code=400,
            detail="variant_a 와 variant_b 가 동일합니다.",
        )

    _validate_meeting_exists(config, body.source_meeting_id, test_type="stt")

    selected_id = _runner_new_test_id()
    broadcaster = await _make_ab_broadcaster(request)
    model_manager = getattr(request.app.state, "model_manager", None)

    task = asyncio.create_task(
        _runner_run_stt_ab_test(
            config=config,
            source_meeting_id=body.source_meeting_id,
            variant_a=ModelSpec(
                label=body.variant_a.label,
                model_id=body.variant_a.model_id,
                backend=body.variant_a.backend,
            ),
            variant_b=ModelSpec(
                label=body.variant_b.label,
                model_id=body.variant_b.model_id,
                backend=body.variant_b.backend,
            ),
            allow_diarize_rerun=body.allow_diarize_rerun,
            ws_broadcaster=broadcaster,
            model_manager=model_manager,
            test_id=selected_id,
        ),
        name=f"ab-test-stt-{selected_id}",
    )
    task.add_done_callback(_log_task_exception)

    return ABTestStartedResponse(test_id=selected_id)


@router.get(
    "/ab-tests",
    summary="A/B 테스트 목록 조회",
    description="저장된 A/B 테스트 목록을 최신순으로 반환한다. source_meeting_id 쿼리 파라미터로 필터 가능.",
)
async def list_ab_tests(
    request: Request,
    source_meeting_id: str | None = None,
) -> dict[str, Any]:
    """A/B 테스트 목록을 조회한다."""
    config = _get_config(request)
    return {"tests": _runner_list_tests(config, source_meeting_id)}


@router.get(
    "/ab-tests/{test_id}",
    summary="A/B 테스트 상세 조회",
    description="metadata + variant_a/variant_b 산출물을 포함한 테스트 상세를 반환한다.",
)
async def get_ab_test(
    test_id: str,
    request: Request,
) -> dict[str, Any]:
    """특정 A/B 테스트의 상세 결과를 조회한다."""
    _validate_test_id(test_id)
    config = _get_config(request)
    try:
        return _runner_get_test_result(config, test_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"A/B 테스트를 찾을 수 없습니다: {test_id}",
        ) from None


@router.get(
    "/ab-tests/{test_id}/variant/{variant}/summary",
    summary="A/B 테스트 variant 요약 마크다운 조회",
    description="variant_a 또는 variant_b 의 summary.md 를 text/markdown 으로 반환한다.",
)
async def get_ab_test_summary(
    test_id: str,
    variant: str,
    request: Request,
) -> Response:
    """A/B 테스트 variant 의 요약 마크다운을 반환한다."""
    _validate_test_id(test_id)
    _validate_variant(variant)
    config = _get_config(request)

    test_dir = ab_test_store.resolve_test_dir(config, test_id)
    summary_path = test_dir / f"variant_{variant}" / "summary.md"

    if not summary_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"요약 파일이 없습니다: variant_{variant}/summary.md",
        )

    return Response(
        content=summary_path.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
    )


@router.delete(
    "/ab-tests/{test_id}",
    status_code=204,
    summary="A/B 테스트 삭제",
    description="테스트 디렉터리를 통째로 삭제한다.",
)
async def delete_ab_test(
    test_id: str,
    request: Request,
) -> None:
    """A/B 테스트를 삭제한다."""
    _validate_test_id(test_id)
    config = _get_config(request)
    try:
        _runner_delete_test(config, test_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"A/B 테스트를 찾을 수 없습니다: {test_id}",
        ) from None


@router.post(
    "/ab-tests/{test_id}/cancel",
    status_code=202,
    response_model=ABTestStartedResponse,
    summary="A/B 테스트 취소",
    description="진행 중인 A/B 테스트의 취소를 요청한다 (variant 경계에서 중단).",
)
async def cancel_ab_test(
    test_id: str,
    request: Request,
) -> ABTestStartedResponse:
    """A/B 테스트 취소를 요청한다."""
    _validate_test_id(test_id)
    config = _get_config(request)
    try:
        await _runner_cancel_test(config, test_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ABTestStartedResponse(test_id=test_id, status="cancelling")
