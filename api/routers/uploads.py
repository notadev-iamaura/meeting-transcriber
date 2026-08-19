"""오디오 업로드 API 라우터."""

from __future__ import annotations

import errno
import logging
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.quarantine import (
    QuarantineError,
    _lexical_absolute,
    _open_directory_tree_no_follow,
    _same_inode,
    _same_validated_content,
    _unlink_if_inode,
)

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


class _UploadSecurityError(Exception):
    """업로드 경로·inode 보안 계약 위반."""


def _configured_upload_dir(config: Any) -> Path:
    """raw base_dir에서 symlink를 해석하지 않고 입력 경로를 계산한다."""
    raw_base = getattr(config.paths, "base_dir", None)
    raw_child = getattr(config.paths, "audio_input_dir", None)
    if not isinstance(raw_base, (str, Path)) or not isinstance(raw_child, (str, Path)):
        raise _UploadSecurityError("base_dir/audio_input_dir 설정 형식이 올바르지 않습니다")

    try:
        base_dir = _lexical_absolute(Path(raw_base))
    except QuarantineError as exc:
        raise _UploadSecurityError(f"base_dir가 안전하지 않습니다: {exc}") from exc

    raw_child_text = os.fspath(raw_child)
    child = Path(raw_child)
    if (
        not raw_child_text
        or raw_child_text in {".", ".."}
        or raw_child_text.startswith("~")
        or child.is_absolute()
        or ".." in child.parts
    ):
        raise _UploadSecurityError("audio_input_dir는 base_dir 하위의 상대 경로여야 합니다")

    try:
        upload_dir = _lexical_absolute(base_dir / child)
    except QuarantineError as exc:
        raise _UploadSecurityError(f"audio_input_dir가 안전하지 않습니다: {exc}") from exc
    if not upload_dir.is_relative_to(base_dir):
        raise _UploadSecurityError("audio_input_dir가 base_dir 밖을 가리킵니다")
    return upload_dir


def _path_error_is_security_block(error: OSError) -> bool:
    """no-follow 경로 열기에서 symlink/비디렉터리 차단인지 판별한다."""
    return error.errno in {errno.ELOOP, errno.ENOTDIR}


def _verify_directory_identity(
    upload_dir: Path,
    directory_fd: int,
    expected: os.stat_result,
) -> None:
    """lexical 경로가 열어 둔 감시 디렉터리와 계속 같은지 확인한다."""
    if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
        raise _UploadSecurityError("열린 업로드 경로가 디렉터리가 아닙니다")

    reopened_fd: int | None = None
    try:
        reopened_fd = _open_directory_tree_no_follow(upload_dir, create=False)
        reopened = os.fstat(reopened_fd)
    except (OSError, QuarantineError) as exc:
        raise _UploadSecurityError(f"업로드 디렉터리 재검증 실패: {exc}") from exc
    finally:
        if reopened_fd is not None:
            os.close(reopened_fd)
    if not _same_inode(expected, reopened):
        raise _UploadSecurityError("업로드 도중 입력 디렉터리가 교체되었습니다")


def _create_upload_temp(directory_fd: int) -> tuple[str, int, os.stat_result]:
    """예측할 수 없는 0600 no-follow 임시 파일을 만든다."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise _UploadSecurityError("O_NOFOLLOW를 지원하지 않아 안전한 업로드가 불가합니다")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(no_follow) | getattr(os, "O_CLOEXEC", 0)
    for _ in range(100):
        name = f".upload-{uuid.uuid4().hex}.part"
        try:
            file_fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            continue
        created: os.stat_result | None = None
        try:
            created = os.fstat(file_fd)
            os.fchmod(file_fd, 0o600)
            created = os.fstat(file_fd)
            if not stat.S_ISREG(created.st_mode):
                raise _UploadSecurityError("업로드 임시 inode가 일반 파일이 아닙니다")
            return name, file_fd, created
        except BaseException:
            if created is not None:
                try:
                    _unlink_if_inode(directory_fd, name, created)
                except OSError:
                    pass
            os.close(file_fd)
            raise
    raise HTTPException(status_code=503, detail="업로드 임시 파일 이름을 할당하지 못했습니다.")


def _write_all(file_fd: int, chunk: bytes) -> None:
    """부분 write를 허용하지 않고 청크 전체를 파일 descriptor에 쓴다."""
    remaining = memoryview(chunk)
    while remaining:
        count = os.write(file_fd, remaining)
        if count <= 0:
            raise OSError(errno.EIO, "업로드 파일 write가 진행되지 않았습니다")
        remaining = remaining[count:]


def _upload_candidate_names(filename: str) -> list[str]:
    """무덮어쓰기 publish에서 순서대로 시도할 파일명을 반환한다."""
    path = Path(filename)
    names = [filename]
    names.extend(f"{path.stem} ({index}){path.suffix}" for index in range(1, 1000))
    return names


def _publish_upload(
    *,
    upload_dir: Path,
    directory_fd: int,
    directory_identity: os.stat_result,
    temp_name: str,
    temp_fd: int,
    temp_identity: os.stat_result,
    filename: str,
) -> str:
    """완성된 temp inode를 same-directory hardlink로 무덮어쓰기 publish한다."""
    _verify_directory_identity(upload_dir, directory_fd, directory_identity)
    temp_entry = os.stat(temp_name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(temp_entry.st_mode) or not _same_validated_content(
        temp_identity,
        temp_entry,
    ):
        raise _UploadSecurityError("업로드 임시 파일이 publish 전 교체되었습니다")

    published_name: str | None = None
    for candidate_name in _upload_candidate_names(filename):
        try:
            os.link(
                temp_name,
                candidate_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            continue
        published_name = candidate_name
        break
    if published_name is None:
        raise HTTPException(status_code=409, detail="동일한 이름의 파일이 너무 많습니다.")

    # 여기서부터는 roll-forward이다. link가 성공한 final은 후속 검증,
    # fsync, 취소, cleanup 실패에서도 절대 삭제하지 않는다.
    final_entry = os.stat(published_name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(final_entry.st_mode) or not _same_validated_content(
        temp_identity,
        final_entry,
    ):
        raise _UploadSecurityError("publish된 final inode identity 검증에 실패했습니다")

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise _UploadSecurityError("O_NOFOLLOW를 지원하지 않아 final 검증이 불가합니다")
    final_fd = os.open(
        published_name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | int(no_follow),
        dir_fd=directory_fd,
    )
    try:
        opened_final = os.fstat(final_fd)
    finally:
        os.close(final_fd)
    if not stat.S_ISREG(opened_final.st_mode) or not _same_validated_content(
        temp_identity,
        opened_final,
    ):
        raise _UploadSecurityError("publish된 final 파일 열기 검증에 실패했습니다")

    current_temp = os.fstat(temp_fd)
    if not _same_validated_content(temp_identity, current_temp):
        raise _UploadSecurityError("publish 중 업로드 inode 내용이 변경되었습니다")
    _verify_directory_identity(upload_dir, directory_fd, directory_identity)
    os.fsync(directory_fd)

    if not _unlink_if_inode(directory_fd, temp_name, temp_identity):
        raise _UploadSecurityError("임시 파일 entry가 다른 inode로 교체되었습니다")
    _verify_directory_identity(upload_dir, directory_fd, directory_identity)
    os.fsync(directory_fd)
    return published_name


def _cleanup_upload_temp(
    directory_fd: int | None,
    temp_name: str | None,
    created_identity: os.stat_result | None,
) -> None:
    """실패/취소 시 자신이 만든 temp inode만 best-effort로 정리한다."""
    if directory_fd is None or temp_name is None or created_identity is None:
        return
    try:
        if _unlink_if_inode(directory_fd, temp_name, created_identity):
            os.fsync(directory_fd)
        else:
            logger.error(f"업로드 temp cleanup inode 불일치: {temp_name}")
    except OSError as exc:
        logger.error(f"업로드 temp cleanup 실패: {temp_name} ({exc})")


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
    if not candidate.exists() and not candidate.is_symlink():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    for i in range(1, 1000):
        alt = target_dir / f"{stem} ({i}){suffix}"
        if not alt.exists() and not alt.is_symlink():
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

    audio_input_dir: Path | None = None
    directory_fd: int | None = None
    directory_identity: os.stat_result | None = None
    temp_name: str | None = None
    temp_fd: int | None = None
    created_identity: os.stat_result | None = None
    published_name: str | None = None
    written = 0
    try:
        audio_input_dir = _configured_upload_dir(config)
        directory_fd = _open_directory_tree_no_follow(audio_input_dir, create=True)
        directory_identity = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_identity.st_mode):
            raise _UploadSecurityError("오디오 입력 경로가 디렉터리가 아닙니다")
        _verify_directory_identity(audio_input_dir, directory_fd, directory_identity)

        temp_name, temp_fd, created_identity = _create_upload_temp(directory_fd)

        # stream을 다 소비하기 전에는 watcher 대상 final을 생성하지 않는다.
        # CancelledError/BaseException은 finally에서 최초 temp inode만 정리한 뒤 전파된다.
        async for chunk in request.stream():
            if not chunk:
                continue
            written += len(chunk)
            if written > _UPLOAD_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"파일이 너무 큽니다 (최대 {_UPLOAD_MAX_BYTES // (1024**3)} GB)",
                )
            _write_all(temp_fd, chunk)

        if written == 0:
            raise HTTPException(status_code=400, detail="요청 본문이 비어 있습니다.")

        os.fsync(temp_fd)
        completed_identity = os.fstat(temp_fd)
        if (
            not stat.S_ISREG(completed_identity.st_mode)
            or not _same_inode(created_identity, completed_identity)
            or completed_identity.st_size != written
        ):
            raise _UploadSecurityError("업로드 완료 파일 identity/size 검증에 실패했습니다")

        published_name = _publish_upload(
            upload_dir=audio_input_dir,
            directory_fd=directory_fd,
            directory_identity=directory_identity,
            temp_name=temp_name,
            temp_fd=temp_fd,
            temp_identity=completed_identity,
            filename=filename,
        )
        # publish 헬퍼가 inode를 확인하고 temp entry를 제거했다.
        temp_name = None
    except HTTPException:
        raise
    except (_UploadSecurityError, QuarantineError) as e:
        logger.warning(f"업로드 경로/inode 보안 차단: {e}")
        raise HTTPException(status_code=400, detail=f"SECURITY_BLOCKED: {e}") from e
    except OSError as e:
        if _path_error_is_security_block(e):
            logger.warning(f"업로드 no-follow 경로 차단: {e}")
            raise HTTPException(
                status_code=400,
                detail=f"SECURITY_BLOCKED: 안전하지 않은 업로드 경로입니다 ({e})",
            ) from e
        logger.error(f"업로드 저장 실패: {audio_input_dir} — {e}")
        raise HTTPException(status_code=500, detail=f"파일 저장 실패: {e}") from e
    finally:
        _cleanup_upload_temp(directory_fd, temp_name, created_identity)
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass

    assert audio_input_dir is not None
    assert published_name is not None
    target_path = audio_input_dir / published_name
    logger.info(
        f"오디오 업로드 완료: filename={published_name}, size={written}, path={target_path}"
    )
    return UploadResponse(filename=published_name, path=str(target_path), size=written)
