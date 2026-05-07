"""오디오 업로드 API 라우터."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


class UploadResponse(BaseModel):
    """오디오 업로드 결과 응답 스키마.

    Attributes:
        filename: 저장된 파일명 (충돌 방지로 변경된 경우 변경된 이름)
        path: 저장 후 절대 경로 (audio_input_dir 하위)
        size: 저장된 파일 크기 (바이트)
    """

    filename: str
    path: str
    size: int


def _get_config(request: Request) -> Any:
    """app.state 에서 AppConfig 를 가져온다."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="서버 설정이 초기화되지 않았습니다.",
        )
    return config


# 업로드 제한 — 사용자가 한 회의를 통째로 업로드하는 시나리오를 고려해 2 GB.
# audio_input 폴더 자체가 회의 전용이라 더 큰 파일은 watcher 가 거부할 가능성이 높다.
_UPLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# 파일명에서 안전한 문자만 허용 (path traversal · 제어문자 차단).
# 한글/공백/대시/언더스코어/괄호/점은 허용하되 슬래시·백슬래시·NUL 은 거부.
_FILENAME_FORBIDDEN_PATTERN = re.compile(r"[\x00-\x1f/\\]")


def _sanitize_upload_filename(raw: str, supported_exts: set[str]) -> str:
    """업로드 파일명을 정제·검증한다.

    Args:
        raw: X-Filename 헤더로 전달된 원본 파일명 (URL 디코딩 이후).
        supported_exts: 허용 확장자 집합 (점 제외, 소문자, 예: {"wav", "mp3"}).

    Returns:
        정제된 파일명 (앞뒤 공백·점 제거).

    Raises:
        HTTPException 400: 빈 문자열, 금지 문자, 미지원 확장자.
    """
    cleaned = (raw or "").strip().strip(".")
    if not cleaned:
        raise HTTPException(status_code=400, detail="파일명이 비어 있습니다.")
    if _FILENAME_FORBIDDEN_PATTERN.search(cleaned):
        raise HTTPException(
            status_code=400,
            detail="파일명에 사용할 수 없는 문자가 포함되어 있습니다.",
        )
    # path traversal 추가 방어 — basename 만 사용
    basename = Path(cleaned).name
    if basename != cleaned:
        raise HTTPException(
            status_code=400,
            detail="파일명에 경로 구분자가 포함되어 있습니다.",
        )

    suffix = Path(basename).suffix.lower().lstrip(".")
    if suffix not in supported_exts:
        raise HTTPException(
            status_code=400,
            detail=(
                f"지원하지 않는 확장자입니다: .{suffix or '(없음)'} "
                f"(지원 형식: {sorted(supported_exts)})"
            ),
        )
    return basename


def _resolve_unique_upload_path(target_dir: Path, filename: str) -> Path:
    """동일한 파일명이 이미 존재하면 `name (1).ext`, `name (2).ext` 식으로 중복 회피.

    Args:
        target_dir: 저장 대상 디렉토리.
        filename: 정제 완료된 파일명.

    Returns:
        실제로 저장될 절대 경로 (중복 회피 적용 후).
    """
    candidate = target_dir / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    for i in range(1, 1000):
        alt = target_dir / f"{stem} ({i}){suffix}"
        if not alt.exists():
            return alt
    # 비현실적 시나리오 — 1000 개 같은 이름이 쌓여 있을 때만 도달
    raise HTTPException(status_code=409, detail="동일한 이름의 파일이 너무 많습니다.")


@router.post("/uploads", response_model=UploadResponse, status_code=201)
async def upload_audio(request: Request) -> UploadResponse:
    """프론트가 fetch 로 전송한 단일 오디오 파일을 audio_input 폴더에 저장한다.

    multipart/form-data 대신 Content-Type=application/octet-stream + X-Filename
    헤더를 사용한다. python-multipart 같은 추가 의존성을 피하면서, 프론트의
    File 객체를 그대로 fetch body 로 전달할 수 있어 단순하다.

    저장된 파일은 `core.watcher.FolderWatcher` 가 자동으로 감지하여 큐에
    `recorded` 상태로 등록한다. 즉 이 엔드포인트는 "큐 진입" 직접 책임을
    지지 않는다 (단일 책임).

    Headers:
        X-Filename: URL 인코딩된 원본 파일명. 예: "회의록 2026-04-29.m4a"
        Content-Length: 본문 크기 (선택, 사전 검증용).

    Returns:
        UploadResponse: 저장된 파일 정보.

    Raises:
        HTTPException 400: 헤더 누락, 잘못된 파일명, 미지원 확장자, 빈 본문.
        HTTPException 413: 본문이 _UPLOAD_MAX_BYTES 초과.
        HTTPException 500: 디스크 쓰기 실패.
    """
    config = _get_config(request)
    audio_input_dir = config.paths.resolved_audio_input_dir
    supported_exts = {fmt.lower().lstrip(".") for fmt in config.audio.supported_input_formats}

    raw_filename = request.headers.get("x-filename")
    if not raw_filename:
        raise HTTPException(status_code=400, detail="X-Filename 헤더가 필요합니다.")

    try:
        decoded = unquote(raw_filename)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"X-Filename 헤더 디코딩 실패: {e}",
        ) from e

    filename = _sanitize_upload_filename(decoded, supported_exts)

    # 본문 크기 사전 검증 — Content-Length 가 있을 때만 (정확하지 않을 수 있음).
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            cl = int(content_length)
            if cl > _UPLOAD_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"파일이 너무 큽니다 (최대 {_UPLOAD_MAX_BYTES // (1024**3)} GB)",
                )
        except ValueError:
            # Content-Length 가 잘못된 경우는 본문 읽으며 실측에 의존
            pass

    # 디렉토리 보장
    try:
        await asyncio.to_thread(audio_input_dir.mkdir, parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"입력 폴더 생성 실패: {e}",
        ) from e

    target_path = _resolve_unique_upload_path(audio_input_dir, filename)

    # 본문을 스트리밍으로 받아 디스크에 직접 쓴다 — 대용량 파일 메모리 폭주 방지.
    written = 0
    tmp_path = target_path.with_suffix(target_path.suffix + ".part")
    try:
        # 동기 파일 I/O 를 to_thread 로 위임하지 않고 그대로 사용하는 이유:
        # FastAPI 의 request.stream() 은 비동기 제너레이터이므로 같은 코루틴에서
        # 청크별로 받아야 한다. write 는 OS 캐시로 빠르게 끝나며,
        # 청크 크기는 starlette 기본(64KB)이라 이벤트 루프 블로킹이 미미하다.
        with open(tmp_path, "wb") as fp:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > _UPLOAD_MAX_BYTES:
                    fp.close()
                    tmp_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"파일이 너무 큽니다 (최대 {_UPLOAD_MAX_BYTES // (1024**3)} GB)",
                    )
                fp.write(chunk)

        if written == 0:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="요청 본문이 비어 있습니다.")

        # 원자적 rename — watcher 가 .part 파일은 무시하고, 최종 이름으로 등장
        # 하는 순간을 새 파일 생성 이벤트로 감지한다.
        tmp_path.rename(target_path)
    except HTTPException:
        # tmp_path 정리는 이미 위에서 처리됨
        raise
    except OSError as e:
        # 미들 단계에서 깨진 .part 정리 (best-effort)
        tmp_path.unlink(missing_ok=True)
        logger.error(f"업로드 저장 실패: {target_path} — {e}")
        raise HTTPException(status_code=500, detail=f"파일 저장 실패: {e}") from e

    logger.info(
        f"오디오 업로드 완료: filename={target_path.name}, size={written}, path={target_path}"
    )
    return UploadResponse(
        filename=target_path.name,
        path=str(target_path),
        size=written,
    )
