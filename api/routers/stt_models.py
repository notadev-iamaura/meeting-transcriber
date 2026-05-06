"""STT 모델 선택기 API 라우터."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.config_yaml import (
    get_config_path as _get_config_path,
)
from api.config_yaml import (
    replace_yaml_value as _replace_yaml_value,
)
from core.io_utils import atomic_write_text as _atomic_write_text
from core.stt_model_downloader import DownloadConflictError
from core.stt_model_registry import STT_MODELS, STTModelSpec
from core.stt_model_registry import get_by_id as _stt_get_by_id
from core.stt_model_status import (
    ModelStatus,
    get_actual_size_mb,
    get_model_status,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class STTModelInfo(BaseModel):
    """STT 모델 한 건의 정적 메타데이터 + 런타임 상태."""

    id: str
    label: str
    description: str
    base_model: str
    expected_size_mb: int
    actual_size_mb: float | None = None
    cer_percent: float
    wer_percent: float
    memory_gb: float
    rtf: float
    license: str
    is_default: bool
    is_recommended: bool
    status: str
    is_active: bool
    download_progress: int | None = None
    error_message: str | None = None


class STTModelsResponse(BaseModel):
    """GET /api/stt-models 응답 스키마."""

    models: list[STTModelInfo]
    active_model_id: str
    active_model_path: str


def _is_active_stt_model(spec: STTModelSpec, active_path: str) -> bool:
    """spec 이 현재 활성 STT 모델인지 판정한다."""
    from core.stt_model_status import get_effective_model_path

    candidates: list[str] = [spec.model_path]
    try:
        candidates.append(str(Path(spec.model_path).expanduser()))
    except Exception:  # noqa: BLE001
        pass
    try:
        effective = get_effective_model_path(spec)
        if effective not in candidates:
            candidates.append(effective)
        try:
            expanded = str(Path(effective).expanduser())
            if expanded not in candidates:
                candidates.append(expanded)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass

    return active_path in candidates


@router.get("/stt-models", response_model=STTModelsResponse)
async def list_stt_models(request: Request) -> STTModelsResponse:
    """STT 모델 레지스트리의 모델 목록과 동적 상태를 반환한다."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")

    downloader = getattr(request.app.state, "stt_downloader", None)
    active_path = config.stt.model_name

    models: list[STTModelInfo] = []
    active_id: str | None = None

    for spec in STT_MODELS:
        disk_status = get_model_status(spec)
        job = downloader.get_progress(spec.id) if downloader is not None else None
        runtime_status = job.status if job is not None else disk_status

        if (
            disk_status == ModelStatus.READY
            and runtime_status == ModelStatus.ERROR
            and downloader is not None
        ):
            logger.info("stale ERROR job 제거 (디스크는 READY): %s", spec.id)
            downloader.clear_job(spec.id)
            job = None
            runtime_status = disk_status

        is_active = _is_active_stt_model(spec, active_path)
        if is_active:
            active_id = spec.id

        actual_size: float | None = None
        if disk_status == ModelStatus.READY:
            try:
                actual_size = get_actual_size_mb(spec.model_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("실제 모델 크기 계산 실패 (%s): %s", spec.id, exc)

        models.append(
            STTModelInfo(
                id=spec.id,
                label=spec.label,
                description=spec.description,
                base_model=spec.base_model,
                expected_size_mb=spec.expected_size_mb,
                actual_size_mb=actual_size,
                cer_percent=spec.cer_percent,
                wer_percent=spec.wer_percent,
                memory_gb=spec.memory_gb,
                rtf=spec.rtf,
                license=spec.license,
                is_default=spec.is_default,
                is_recommended=spec.is_recommended,
                status=runtime_status.value,
                is_active=is_active,
                download_progress=job.progress_percent if job is not None else None,
                error_message=job.error_message if job is not None else None,
            )
        )

    return STTModelsResponse(
        models=models,
        active_model_id=active_id or "",
        active_model_path=active_path,
    )


@router.post("/stt-models/{model_id}/download", status_code=202)
async def download_stt_model(request: Request, model_id: str) -> dict[str, Any]:
    """지정한 STT 모델의 다운로드를 백그라운드에서 시작한다."""
    spec = _stt_get_by_id(model_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 STT 모델: {model_id}")

    downloader = getattr(request.app.state, "stt_downloader", None)
    if downloader is None:
        raise HTTPException(status_code=503, detail="STT 다운로더가 초기화되지 않았습니다.")

    try:
        job_id = await downloader.start_download(model_id)
    except DownloadConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    logger.info("STT 모델 다운로드 요청 수락: %s (%s)", model_id, job_id)
    return {
        "job_id": job_id,
        "model_id": model_id,
        "status": "downloading",
        "message": "다운로드를 시작합니다.",
    }


@router.post("/stt-models/{model_id}/download-direct", status_code=202)
async def download_stt_model_direct(request: Request, model_id: str) -> dict[str, Any]:
    """HF 직접 URL 로 STT 모델을 다운로드한다."""
    spec = _stt_get_by_id(model_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 STT 모델: {model_id}")

    downloader = getattr(request.app.state, "stt_downloader", None)
    if downloader is None:
        raise HTTPException(status_code=503, detail="STT 다운로더가 초기화되지 않았습니다.")

    try:
        job_id = await downloader.start_download(model_id, prefer_direct=True)
    except DownloadConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    logger.info("STT 모델 직접 URL 다운로드 요청 수락: %s (%s)", model_id, job_id)
    return {
        "job_id": job_id,
        "model_id": model_id,
        "status": "downloading",
        "message": "직접 URL로 다운로드를 시작합니다.",
        "method": "direct_url",
    }


@router.get("/stt-models/{model_id}/download-status")
async def get_stt_download_status(request: Request, model_id: str) -> dict[str, Any]:
    """STT 모델 다운로드 작업의 진행 상태를 반환한다."""
    if _stt_get_by_id(model_id) is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 STT 모델: {model_id}")

    downloader = getattr(request.app.state, "stt_downloader", None)
    if downloader is None:
        raise HTTPException(status_code=503, detail="STT 다운로더가 초기화되지 않았습니다.")

    job = downloader.get_progress(model_id)
    if job is None:
        raise HTTPException(status_code=404, detail="다운로드 작업을 찾을 수 없습니다.")

    return {
        "model_id": model_id,
        "job_id": job.job_id,
        "status": job.status.value,
        "progress_percent": job.progress_percent,
        "current_step": job.current_step,
        "started_at": job.started_at.isoformat(),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error_message": job.error_message,
    }


@router.post("/stt-models/{model_id}/activate")
async def activate_stt_model(request: Request, model_id: str) -> dict[str, Any]:
    """활성 STT 모델을 변경하고 config.yaml 을 업데이트한다."""
    spec = _stt_get_by_id(model_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 STT 모델: {model_id}")

    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")

    if get_model_status(spec) != ModelStatus.READY:
        raise HTTPException(
            status_code=400,
            detail="모델이 다운로드되지 않았습니다. 먼저 다운로드하세요.",
        )

    previous_model = config.stt.model_name
    from core.stt_model_status import get_effective_model_path

    spec_path = get_effective_model_path(spec)
    if spec_path.startswith(("~", "/", "./", "../")):
        new_path = str(Path(spec_path).expanduser())
    else:
        new_path = spec_path

    config_path = _get_config_path()
    try:
        with open(config_path, encoding="utf-8") as f:
            content = f.read()
        content = _replace_yaml_value(content, "stt", "model_name", f'"{new_path}"')
        await asyncio.to_thread(_atomic_write_text, config_path, content)
        logger.info(
            "활성 STT 모델 변경: %s -> %s (config.yaml 원자적 저장)",
            previous_model,
            new_path,
        )
    except OSError as exc:
        logger.exception("config.yaml 저장 실패: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"설정 파일 저장에 실패했습니다: {exc}",
        ) from exc

    new_stt = config.stt.model_copy(update={"model_name": new_path})
    request.app.state.config = config.model_copy(update={"stt": new_stt})

    return {
        "model_id": model_id,
        "previous_model_path": previous_model,
        "model_path": new_path,
        "message": "활성 모델이 변경되었습니다. 다음 전사부터 적용됩니다.",
    }


class STTManualDownloadFile(BaseModel):
    """수동 다운로드 파일 하나의 URL 정보."""

    name: str
    url: str
    size_bytes: int | None = None


class STTManualDownloadInfo(BaseModel):
    """GET /api/stt-models/{id}/manual-download-info 응답."""

    model_id: str
    label: str
    supported: bool
    files: list[STTManualDownloadFile] = Field(default_factory=list)
    target_directory: str = ""
    instructions: str = ""


class STTImportRequest(BaseModel):
    """POST /api/stt-models/{id}/import-manual 요청 본문."""

    source_dir: str = Field(
        ...,
        description=(
            "사용자가 다운로드한 파일들이 있는 로컬 디렉토리 절대 경로. "
            "해당 디렉토리 안에 config.json 과 weights.safetensors 파일이 있어야 한다."
        ),
    )


class STTImportResponse(BaseModel):
    """POST /api/stt-models/{id}/import-manual 응답."""

    model_id: str
    imported_dir: str
    files_copied: list[str]
    message: str


@router.get(
    "/stt-models/{model_id}/manual-download-info",
    response_model=STTManualDownloadInfo,
)
async def get_stt_manual_download_info(model_id: str) -> STTManualDownloadInfo:
    """수동 다운로드용 HF 직접 URL 목록과 타겟 폴더 경로를 반환한다."""
    from core.stt_model_registry import get_hf_download_urls, get_manual_import_dir

    spec = _stt_get_by_id(model_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 STT 모델: {model_id}")

    urls = get_hf_download_urls(spec)

    target_dir = get_manual_import_dir(spec)
    files = [STTManualDownloadFile(name=u["name"], url=u["url"]) for u in urls]

    return STTManualDownloadInfo(
        model_id=model_id,
        label=spec.label,
        supported=True,
        files=files,
        target_directory=target_dir,
        instructions=(
            "1) 아래 파일들을 브라우저로 각각 다운로드하세요.\n"
            f"2) 다운로드한 파일 2개를 한 폴더에 모으세요 (예: ~/Downloads/{spec.id}/).\n"
            "3) '가져오기' 버튼을 누르고 해당 폴더 경로를 입력하면 앱이 자동으로 "
            f"{target_dir} 로 복사합니다.\n"
            "4) 이후 '활성화' 버튼으로 이 모델을 사용할 수 있어요."
        ),
    )


@router.post(
    "/stt-models/{model_id}/import-manual",
    response_model=STTImportResponse,
)
async def import_stt_manual(
    request: Request, model_id: str, body: STTImportRequest
) -> STTImportResponse:
    """사용자가 브라우저로 받은 모델 파일을 앱 내부 경로로 복사한다."""
    from core.stt_model_registry import get_manual_import_dir

    spec = _stt_get_by_id(model_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 STT 모델: {model_id}")

    source = Path(body.source_dir).expanduser().resolve()
    if not source.exists() or not source.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"폴더를 찾을 수 없어요: {body.source_dir}",
        )

    required = ["config.json", "weights.safetensors"]
    missing = [name for name in required if not (source / name).is_file()]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"다음 파일이 폴더에 없어요: {', '.join(missing)}. "
                "HuggingFace에서 받은 두 파일을 모두 같은 폴더에 넣어 주세요."
            ),
        )

    target_dir = Path(get_manual_import_dir(spec))
    target_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    try:
        for name in required:
            src_file = source / name
            dst_file = target_dir / name
            tmp_file = target_dir / (name + ".tmp")
            shutil.copy2(str(src_file), str(tmp_file))
            tmp_file.replace(dst_file)
            copied.append(name)
        logger.info("STT 모델 수동 가져오기 완료: %s <- %s", target_dir, source)
    except OSError as exc:
        logger.exception("STT 모델 수동 가져오기 실패: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"파일 복사에 실패했어요: {exc}",
        ) from exc

    downloader = getattr(request.app.state, "stt_downloader", None)
    if downloader is not None:
        try:
            downloader.clear_job(model_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("수동 가져오기 후 stale job 정리 실패 (무시): %s", exc)

    return STTImportResponse(
        model_id=model_id,
        imported_dir=str(target_dir),
        files_copied=copied,
        message=(
            f"모델 파일 {len(copied)}개를 가져왔어요. "
            "이제 '활성화' 버튼으로 이 모델을 사용할 수 있어요."
        ),
    )
