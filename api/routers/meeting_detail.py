"""단일 회의 상세 API 라우터.

목록/일괄 작업은 ``api.routes`` 와 전용 batch router 에 남기고, 단일 회의 상세 조회,
상태 전이, 재전사, 오디오 스트리밍, 전사/요약 조회 및 편집, 단건 요약 실행을 담당한다.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import stat
import threading
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.dependencies import get_job_queue as _get_job_queue
from api.dependencies import (
    get_meeting_mutation_coordinator as _get_meeting_mutation_coordinator,
)
from api.dependencies import get_pipeline_manager as _get_pipeline_manager
from core.audio_quality import (
    AudioFailureKind,
    AudioQualityStatus,
    validate_audio_quality,
)
from core.io_utils import atomic_write_json as _atomic_write_json  # noqa: F401
from core.io_utils import atomic_write_json_pinned as _atomic_write_json_pinned
from core.io_utils import atomic_write_text as _atomic_write_text  # noqa: F401
from core.io_utils import atomic_write_text_pinned as _atomic_write_text_pinned
from core.io_utils import read_text_no_follow
from core.job_queue import (
    RETRANSCRIBE_OUTPUT_FILES,
    JobQueueError,
    _entry_stat,
    _move_entry_checked,
    _open_child_directory,
    _open_pinned_retranscribe_root,
    _require_open_entry_identity,
    _restore_checked_moves,
    _rollback_retranscribe_staging_fds,
    _verify_pinned_retranscribe_root,
    cleanup_retranscribe_staging,
    lexical_root_no_symlinks,
    parse_delete_claim,
    parse_retranscribe_claim,
    retranscribe_staging_paths,
    rollback_retranscribe_staging,
)
from steps.embedder import IndexPurgeError, IndexPurgeResult, purge_meeting_index
from steps.transcriber import (
    AudioAdmissionError,
    AudioFileIdentity,
    EmptyAudioError,
    inspect_audio_path_no_symlinks,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def _meeting_mutation_lease(
    request: Request,
    meeting_id: str,
) -> AsyncIterator[None]:
    """회의별 편집·재전사·삭제를 상태 확인 전부터 완료까지 직렬화한다."""
    _validate_meeting_id(meeting_id)
    coordinator = _get_meeting_mutation_coordinator(request)
    async with coordinator.lease(meeting_id):
        yield


async def _run_llm_steps_with_meeting_lease(
    request: Request,
    pipeline: Any,
    meeting_id: str,
    admission_started: asyncio.Event,
) -> Any:
    """background LLM task를 같은 회의 mutation lease 안에서 실행한다.

    `admission_started`는 lease 진입을 시작하기 직전에 설정한다. 이 task가 다음
    await에서 제어를 돌려줄 때에는 lock을 이미 획득했거나 FIFO waiter로 등록되어
    있으므로, 응답 직후 도착한 재전사/편집이 먼저 추월할 수 없다.
    """
    coordinator = _get_meeting_mutation_coordinator(request)
    admission_started.set()
    async with coordinator.lease(meeting_id):
        return await pipeline.run_llm_steps(meeting_id)


class _RetranscribeStagingIntegrityError(HTTPException):
    """staging root identity 변경으로 durable claim을 유지해야 하는 충돌."""

    def __init__(self, detail: str, *, restore_via_open_fds: bool = True) -> None:
        super().__init__(status_code=409, detail=detail)
        self.restore_via_open_fds = restore_via_open_fds


class _JsonFileCache:
    """JSON 파일을 mtime 기반으로 캐싱하는 스레드 안전 캐시."""

    def __init__(self, max_size: int = 64) -> None:
        self._cache: dict[str, tuple[tuple[int, int, int, int, int], Any]] = {}
        self._max_size = max_size
        self._lock = threading.Lock()

    def get(self, file_path: Path) -> Any:
        """캐시된 JSON 데이터를 반환한다. 변경 시 자동 갱신한다."""
        key = str(file_path)
        stat = file_path.stat()
        current_identity = _file_identity(stat)

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                cached_identity, cached_data = cached
                if cached_identity == current_identity:
                    return cached_data

        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)

        with self._lock:
            if len(self._cache) >= self._max_size and key not in self._cache:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
            self._cache[key] = (current_identity, data)

        return data

    def get_from_fd(
        self,
        file_path: Path,
        file_fd: int,
        file_stat: os.stat_result,
    ) -> Any:
        """이미 no-follow로 연 descriptor에서만 JSON을 읽고 캐시한다."""
        key = str(file_path)
        current_identity = _file_identity(file_stat)

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                cached_identity, cached_data = cached
                if cached_identity == current_identity:
                    return cached_data

        with os.fdopen(os.dup(file_fd), encoding="utf-8") as opened_file:
            data = json.load(opened_file)

        with self._lock:
            if len(self._cache) >= self._max_size and key not in self._cache:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
            self._cache[key] = (current_identity, data)
        return data

    def invalidate(self, file_path: Path) -> None:
        """특정 파일의 캐시를 무효화한다."""
        with self._lock:
            self._cache.pop(str(file_path), None)


_json_cache = _JsonFileCache()


def _validate_meeting_id(meeting_id: str) -> None:
    """단일 path segment meeting_id 계약을 검증한다."""
    if (
        not meeting_id
        or meeting_id in {".", ".."}
        or "/" in meeting_id
        or "\\" in meeting_id
        or "\x00" in meeting_id
    ):
        raise HTTPException(
            status_code=400,
            detail=f"유효하지 않은 회의 ID 형식입니다: {meeting_id}",
        )


def _safe_child_path(root: Path, *parts: str) -> Path:
    """root 하위 경로만 허용하는 안전한 파일 경로를 만든다."""
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*parts).resolve(strict=False)
    if not candidate.is_relative_to(root_resolved):
        raise HTTPException(
            status_code=400,
            detail="유효하지 않은 파일 경로입니다.",
        )
    return candidate


def _artifact_io_http_exception(operation: str, exc: OSError) -> HTTPException:
    """산출물 I/O 실패를 보안 차단과 일반 저장 실패로 구분한다."""
    security_markers = (
        "identity",
        "symlink",
        "안전하지 않",
        "일반 파일이 아닙",
        "단일 일반 파일",
        "상위 디렉터리가 변경",
        "경로 component",
    )
    is_security_error = exc.errno in {errno.ELOOP, errno.ENOTDIR} or any(
        marker in str(exc) for marker in security_markers
    )
    if is_security_error:
        return HTTPException(
            status_code=409,
            detail=f"SECURITY_BLOCKED: {operation} 경로가 안전하지 않습니다.",
        )
    return HTTPException(
        status_code=500,
        detail=f"{operation} 중 저장소 오류가 발생했습니다: {exc}",
    )


def _get_config(request: Request) -> Any:
    """app.state 에서 AppConfig 를 가져온다."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="서버 설정이 초기화되지 않았습니다.",
        )
    return config


_AUDIO_FAILURE_HTTP_STATUS = {
    AudioFailureKind.MEDIA_INVALID: 422,
    AudioFailureKind.SOURCE_BUSY: 409,
    AudioFailureKind.INFRA_UNAVAILABLE: 503,
    AudioFailureKind.SECURITY_BLOCKED: 400,
}


def _configured_lexical_path(config: Any, child_attribute: str | None = None) -> Path:
    """raw path config를 resolve하지 않고 no-follow 검증한 절대 경로로 만든다."""
    try:
        paths = config.paths
        # 실제 AppConfig는 raw 값을 항상 제공한다. 테스트·임베디드 호출자가
        # 기존 resolved_* 계약만 제공하는 경우에도 같은 containment 검증을
        # 적용해 하위 호환을 유지한다.
        raw_base = getattr(paths, "base_dir", None)
        if raw_base is None:
            raw_base = paths.resolved_base_dir
        base = lexical_root_no_symlinks(Path(raw_base))
        if child_attribute is None:
            return base
        raw_child = getattr(paths, child_attribute, None)
        if raw_child is None:
            raw_child = getattr(paths, f"resolved_{child_attribute}")
        configured_child = Path(str(raw_child)).expanduser()
        raw_path = configured_child if configured_child.is_absolute() else base / configured_child
        child = lexical_root_no_symlinks(raw_path)
    except JobQueueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"SECURITY_BLOCKED: 안전하지 않은 설정 경로입니다: {exc}",
        ) from exc
    if not child.is_relative_to(base):
        raise HTTPException(
            status_code=400,
            detail="SECURITY_BLOCKED: 설정 경로가 base_dir 밖에 있습니다.",
        )
    return child


def _require_audio_in_config_base(config: Any, audio_path: Path) -> Path:
    """오디오 lexical 경로가 raw base_dir 내부인지 확인한다."""
    base = _configured_lexical_path(config)
    raw = audio_path.expanduser()
    if "\x00" in str(raw) or ".." in raw.parts:
        raise HTTPException(
            status_code=400,
            detail="SECURITY_BLOCKED: 안전하지 않은 오디오 경로입니다.",
        )
    candidate = raw.absolute()
    if not candidate.is_relative_to(base):
        raise HTTPException(
            status_code=400,
            detail="SECURITY_BLOCKED: 오디오 파일이 설정된 base_dir 밖에 있습니다.",
        )
    return candidate


async def _inspect_audio_identity(
    audio_path: Path,
    *,
    changed_is_busy: bool,
) -> AudioFileIdentity:
    """공통 no-follow 검사를 실행하고 typed HTTP 오류로 변환한다."""
    try:
        return await asyncio.to_thread(inspect_audio_path_no_symlinks, audio_path)
    except AudioAdmissionError as exc:
        raise HTTPException(
            status_code=_AUDIO_FAILURE_HTTP_STATUS[exc.failure_kind],
            detail=f"{exc.failure_kind.name}: {exc}",
        ) from exc
    except EmptyAudioError as exc:
        status = 409 if changed_is_busy else 422
        failure = "SOURCE_BUSY" if changed_is_busy else "MEDIA_INVALID"
        raise HTTPException(status_code=status, detail=f"{failure}: {exc}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=f"SOURCE_BUSY: {exc}") from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"INFRA_UNAVAILABLE: {exc}") from exc


async def _require_audio_identity_unchanged(
    audio_path: Path,
    expected_identity: AudioFileIdentity,
) -> None:
    """claim 이후 source swap을 staging 전에 차단한다."""
    current_identity = await _inspect_audio_identity(audio_path, changed_is_busy=True)
    if current_identity != expected_identity:
        raise HTTPException(
            status_code=409,
            detail="SOURCE_BUSY: 검증 후 오디오 파일 identity가 변경되었습니다.",
        )


async def _require_audio_quality_accept(config: Any, audio_path: Path) -> AudioFileIdentity:
    """no-follow identity와 활성화된 품질 gate를 HTTP 오류로 변환한다."""
    audio_path = _require_audio_in_config_base(config, audio_path)
    before_identity = await _inspect_audio_identity(audio_path, changed_is_busy=False)

    quality_config = getattr(config, "audio_quality", None)
    if quality_config is None or getattr(quality_config, "enabled", False) is not True:
        return before_identity

    try:
        result = await asyncio.to_thread(
            validate_audio_quality,
            audio_path,
            min_mean_db=quality_config.min_mean_volume_db,
            min_duration_s=quality_config.min_duration_seconds,
            expected_identity=before_identity,
            decode_timeout_base_seconds=quality_config.decode_timeout_base_seconds,
            decode_timeout_factor=quality_config.decode_timeout_factor,
            decode_timeout_cap_seconds=quality_config.decode_timeout_cap_seconds,
        )
    except Exception as exc:
        try:
            after_identity = await _inspect_audio_identity(
                audio_path,
                changed_is_busy=True,
            )
        except HTTPException as identity_exc:
            raise identity_exc from exc
        if after_identity != before_identity:
            raise HTTPException(
                status_code=409,
                detail="SOURCE_BUSY: 품질 검증 중 오디오 파일 identity가 변경되었습니다.",
            ) from exc
        raise HTTPException(
            status_code=503,
            detail=f"INFRA_UNAVAILABLE: 오디오 품질 검증 실행 실패: {exc}",
        ) from exc

    after_identity = await _inspect_audio_identity(audio_path, changed_is_busy=True)
    if after_identity != before_identity:
        raise HTTPException(
            status_code=409,
            detail="SOURCE_BUSY: 검증 중 오디오 파일 identity가 변경되었습니다.",
        )
    if result.status is AudioQualityStatus.ACCEPT:
        return after_identity

    failure_kind = result.failure_kind or AudioFailureKind.INFRA_UNAVAILABLE
    raise HTTPException(
        status_code=_AUDIO_FAILURE_HTTP_STATUS[failure_kind],
        detail=f"{failure_kind.name}: {result.reason or '오디오 품질 검증 비수락'}",
    )


def _file_identity(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    """파일 내용과 directory entry 경합을 감지할 identity를 반환한다."""
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _read_pipeline_state_json_pinned(config: Any, meeting_id: str) -> Any | None:
    """pinned checkpoint root와 이미 연 file descriptor에서 pipeline state를 읽는다."""
    _validate_meeting_id(meeting_id)
    checkpoints_root = _configured_lexical_path(config, "checkpoints_dir")
    state_path = _lexical_artifact_path(
        checkpoints_root,
        meeting_id,
        "pipeline_state.json",
    )
    try:
        lexical, root_fd, root_identity = _open_pinned_retranscribe_root(
            checkpoints_root,
            create=False,
        )
    except JobQueueError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return None
        raise

    meeting_fd: int | None = None
    state_fd: int | None = None
    try:
        meeting_stat = _entry_stat(root_fd, meeting_id)
        if meeting_stat is None:
            _verify_pinned_retranscribe_root(lexical, root_fd, root_identity)
            return None
        if not stat.S_ISDIR(meeting_stat.st_mode):
            raise JobQueueError("pipeline state 회의 경로가 안전한 디렉터리가 아닙니다")
        meeting_fd, opened_meeting = _open_child_directory(root_fd, meeting_id)
        state_stat = _entry_stat(meeting_fd, "pipeline_state.json")
        if state_stat is None:
            _require_open_entry_identity(root_fd, meeting_id, opened_meeting)
            _verify_pinned_retranscribe_root(lexical, root_fd, root_identity)
            return None
        if not stat.S_ISREG(state_stat.st_mode):
            raise JobQueueError("pipeline_state.json이 안전한 일반 파일이 아닙니다")

        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise JobQueueError("O_NOFOLLOW를 지원하지 않아 pipeline state를 읽을 수 없습니다")
        try:
            state_fd = os.open(
                "pipeline_state.json",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | int(no_follow),
                dir_fd=meeting_fd,
            )
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                raise JobQueueError("pipeline_state.json entry가 여는 중 교체되었습니다") from exc
            raise
        opened_state = os.fstat(state_fd)
        if not stat.S_ISREG(opened_state.st_mode) or _file_identity(
            opened_state
        ) != _file_identity(state_stat):
            raise JobQueueError("pipeline_state.json entry가 여는 중 변경되었습니다")
        data = _json_cache.get_from_fd(state_path, state_fd, opened_state)
        after_fd = os.fstat(state_fd)
        try:
            after_entry = os.stat(
                "pipeline_state.json",
                dir_fd=meeting_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise JobQueueError("pipeline_state.json이 조회 중 변경되었습니다") from exc
        if _file_identity(after_fd) != _file_identity(opened_state) or _file_identity(
            after_entry
        ) != _file_identity(opened_state):
            raise JobQueueError("pipeline_state.json이 조회 중 변경되었습니다")
        _require_open_entry_identity(root_fd, meeting_id, opened_meeting)
        _verify_pinned_retranscribe_root(lexical, root_fd, root_identity)
        return data
    finally:
        if state_fd is not None:
            os.close(state_fd)
        if meeting_fd is not None:
            os.close(meeting_fd)
        os.close(root_fd)


def _read_pipeline_state_for_response(config: Any, meeting_id: str) -> dict[str, Any] | None:
    """응답 보정용 pipeline_state.json 을 descriptor-relative 방식으로 읽는다."""
    try:
        data = _read_pipeline_state_json_pinned(config, meeting_id)
    except JobQueueError as exc:
        message = str(exc)
        if "변경" in message or "교체" in message:
            raise HTTPException(
                status_code=409,
                detail="pipeline_state.json이 조회 중 변경되었습니다.",
            ) from exc
        if "안전" in message or "O_NOFOLLOW" in message:
            raise HTTPException(
                status_code=400,
                detail="pipeline_state.json이 안전한 일반 파일이 아닙니다.",
            ) from exc
        raise HTTPException(
            status_code=503,
            detail=f"pipeline state 경로 상태 확인 실패: {exc}",
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"pipeline_state.json 응답 보정 읽기 실패: {meeting_id}, error={exc}")
        return None

    return data if isinstance(data, dict) else None


def _has_transcript_artifact(config: Any, meeting_id: str) -> bool:
    """회의 전사 탭을 구성할 수 있는 산출물이 있는지 확인한다."""
    outputs_dir = _configured_lexical_path(config, "outputs_dir")
    checkpoints_dir = _configured_lexical_path(config, "checkpoints_dir")
    candidates = (
        _lexical_artifact_path(outputs_dir, meeting_id, "corrected.json"),
        _lexical_artifact_path(checkpoints_dir, meeting_id, "correct.json"),
        _lexical_artifact_path(checkpoints_dir, meeting_id, "merge.json"),
    )
    for path in candidates:
        try:
            candidate_stat = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"전사 산출물 경로 상태 확인 실패: {exc}",
            ) from exc
        if stat.S_ISREG(candidate_stat.st_mode):
            return True
        raise HTTPException(
            status_code=400,
            detail=f"전사 산출물이 안전한 일반 파일이 아닙니다: {path}",
        )
    return False


async def _purge_meeting_search_index(config: Any, meeting_id: str, operation: str) -> Any:
    """회의 삭제/재전사 전 검색 인덱스를 정리하고 실패 시 HTTP 500으로 중단한다."""
    try:
        result = await asyncio.to_thread(purge_meeting_index, config, meeting_id)
    except IndexPurgeError as exc:
        logger.error(
            "%s 전 검색 인덱스 정리 실패: meeting_id=%s, error=%s",
            operation,
            meeting_id,
            exc,
        )
        raise HTTPException(
            status_code=500,
            detail=f"{operation} 전 검색 인덱스 정리에 실패했습니다: {exc}",
        ) from exc

    logger.info(
        "%s 전 검색 인덱스 정리 완료: meeting_id=%s, chroma_deleted=%s, fts_deleted=%s",
        operation,
        meeting_id,
        result.chroma_deleted,
        result.fts_deleted,
    )
    return result


def _path_contains(parent: Path, candidate: Path) -> bool:
    """candidate가 parent 자신 또는 하위 경로인지 반환한다."""
    return candidate == parent or candidate.is_relative_to(parent)


def _lexical_artifact_path(root: Path, *parts: str) -> Path:
    """root 하위 artifact 경로를 resolve하지 않고 no-follow 검사한다."""
    try:
        root_path = lexical_root_no_symlinks(root)
    except JobQueueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"안전하지 않은 재전사 산출물 root입니다: {exc}",
        ) from exc
    candidate = root_path.joinpath(*parts)
    if not candidate.is_relative_to(root_path):
        raise HTTPException(status_code=400, detail="유효하지 않은 재전사 산출물 경로입니다.")

    current = root_path
    for index, component in enumerate(candidate.relative_to(root_path).parts):
        current /= component
        try:
            entry_stat = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"재전사 산출물 경로 상태 확인 실패: {current} ({exc})",
            ) from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            raise HTTPException(
                status_code=400,
                detail=f"재전사 산출물 경로에 심볼릭 링크를 사용할 수 없습니다: {current}",
            )
        is_final = index == len(candidate.relative_to(root_path).parts) - 1
        if not is_final and not stat.S_ISDIR(entry_stat.st_mode):
            raise HTTPException(
                status_code=400,
                detail=f"재전사 산출물 상위 경로가 디렉터리가 아닙니다: {current}",
            )
    return candidate


def _reattach_open_directory(
    parent_fd: int,
    opened_fd: int,
    canonical_name: str,
    opened_identity: os.stat_result,
    token: str,
) -> None:
    """이동된 열린 디렉터리를 같은 부모의 canonical 이름으로 재부착한다."""
    current = _entry_stat(parent_fd, canonical_name)
    if current is not None:
        if (current.st_dev, current.st_ino) == (
            opened_identity.st_dev,
            opened_identity.st_ino,
        ):
            return
        raise JobQueueError(f"canonical 디렉터리 이름이 이미 교체되었습니다: {canonical_name}")

    opened_current = os.fstat(opened_fd)
    if not stat.S_ISDIR(opened_current.st_mode) or (
        opened_current.st_dev,
        opened_current.st_ino,
    ) != (opened_identity.st_dev, opened_identity.st_ino):
        raise JobQueueError(f"열린 디렉터리 identity가 변경되었습니다: {canonical_name}")

    candidates: list[tuple[str, os.stat_result]] = []
    for candidate_name in os.listdir(parent_fd):
        try:
            candidate = os.stat(
                candidate_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        if (candidate.st_dev, candidate.st_ino) == (
            opened_current.st_dev,
            opened_current.st_ino,
        ):
            candidates.append((candidate_name, candidate))
    if len(candidates) != 1:
        raise JobQueueError(
            f"열린 디렉터리의 재부착 경로를 단일하게 확정할 수 없습니다: {canonical_name}"
        )

    displaced_name, displaced_identity = candidates[0]
    _move_entry_checked(
        parent_fd,
        parent_fd,
        displaced_name,
        displaced_identity,
        token,
        destination_name=canonical_name,
    )
    _require_open_entry_identity(parent_fd, canonical_name, opened_current)


def _stage_retranscribe_artifacts(
    config: Any,
    meeting_id: str,
    audio_path: Path,
    token: str,
) -> None:
    """재전사 대상 산출물을 동일 FS staging 경로로 원자 이동한다.

    원본 오디오가 삭제 대상 안에 있으면 안전하게 요청을 거부한다. 단계 중 하나라도
    실패하면 이미 이동한 항목을 즉시 원위치한다.
    """
    checkpoints_root = _configured_lexical_path(config, "checkpoints_dir")
    outputs_root = _configured_lexical_path(config, "outputs_dir")
    checkpoint_dir = _lexical_artifact_path(checkpoints_root, meeting_id)
    checkpoint_stage, output_stage_path = retranscribe_staging_paths(
        checkpoints_root,
        outputs_root,
        meeting_id,
        token,
    )
    _lexical_artifact_path(checkpoints_root, checkpoint_stage.name)
    _lexical_artifact_path(outputs_root, output_stage_path.name)

    audio_lexical = audio_path.expanduser().absolute()
    if _path_contains(checkpoint_dir, audio_lexical):
        raise HTTPException(
            status_code=409,
            detail="원본 오디오가 체크포인트 디렉토리 안에 있어 안전하게 재전사할 수 없습니다.",
        )

    checkpoints_lexical, checkpoints_fd, checkpoints_identity = _open_pinned_retranscribe_root(
        checkpoints_root, create=True
    )
    outputs_lexical: Path | None = None
    outputs_fd: int | None = None
    outputs_identity: os.stat_result | None = None
    checkpoint_source_fd: int | None = None
    checkpoint_stage_fd: int | None = None
    output_source_fd: int | None = None
    output_stage_fd: int | None = None
    opened_output_stage: os.stat_result | None = None
    moved_output_entries: dict[str, os.stat_result] = {}
    mutation_started = False
    try:
        outputs_lexical, outputs_fd, outputs_identity = _open_pinned_retranscribe_root(
            outputs_root,
            create=True,
        )
        if (
            _entry_stat(checkpoints_fd, checkpoint_stage.name) is not None
            or _entry_stat(outputs_fd, output_stage_path.name) is not None
        ):
            raise HTTPException(
                status_code=409,
                detail="동일한 재전사 staging token이 이미 존재합니다.",
            )

        checkpoint_stat = _entry_stat(checkpoints_fd, meeting_id)
        if checkpoint_stat is not None:
            if not stat.S_ISDIR(checkpoint_stat.st_mode):
                raise HTTPException(
                    status_code=400, detail="checkpoint 경로가 디렉터리가 아닙니다."
                )
            checkpoint_source_fd, opened_checkpoint = _open_child_directory(
                checkpoints_fd,
                meeting_id,
            )
            _require_open_entry_identity(checkpoints_fd, meeting_id, opened_checkpoint)
            os.mkdir(checkpoint_stage.name, mode=0o700, dir_fd=checkpoints_fd)
            mutation_started = True
            checkpoint_stage_fd, opened_checkpoint_stage = _open_child_directory(
                checkpoints_fd,
                checkpoint_stage.name,
            )
            checkpoint_entries = os.listdir(checkpoint_source_fd)
            initial_checkpoint_entries: dict[str, os.stat_result] = {}
            moved_checkpoint_entries: dict[str, os.stat_result] = {}
            try:
                for name in checkpoint_entries:
                    entry = os.stat(
                        name,
                        dir_fd=checkpoint_source_fd,
                        follow_symlinks=False,
                    )
                    if stat.S_ISLNK(entry.st_mode):
                        raise JobQueueError(
                            f"checkpoint staging entry가 심볼릭 링크입니다: {name}"
                        )
                    initial_checkpoint_entries[name] = entry
                for name, expected in initial_checkpoint_entries.items():
                    moved_checkpoint_entries[name] = _move_entry_checked(
                        checkpoint_source_fd,
                        checkpoint_stage_fd,
                        name,
                        expected,
                        token,
                    )
                    try:
                        _require_open_entry_identity(
                            checkpoints_fd,
                            meeting_id,
                            opened_checkpoint,
                        )
                    except JobQueueError as source_error:
                        try:
                            _reattach_open_directory(
                                checkpoints_fd,
                                checkpoint_source_fd,
                                meeting_id,
                                opened_checkpoint,
                                token,
                            )
                        except (OSError, JobQueueError) as reattach_error:
                            raise _RetranscribeStagingIntegrityError(
                                "checkpoint source entry가 이동되어 durable staging "
                                f"복구가 필요합니다: {reattach_error}",
                                restore_via_open_fds=False,
                            ) from source_error
                        raise _RetranscribeStagingIntegrityError(
                            "checkpoint source entry가 staging 중 이동되어 "
                            "정확한 inode를 재부착한 후 중단했습니다."
                        ) from source_error
                    try:
                        _require_open_entry_identity(
                            checkpoints_fd,
                            checkpoint_stage.name,
                            opened_checkpoint_stage,
                        )
                    except JobQueueError as stage_error:
                        raise _RetranscribeStagingIntegrityError(
                            "checkpoint stage entry가 staging 중 이동되어 중단했습니다."
                        ) from stage_error
            except BaseException as operation_error:
                restore_via_open_fds = (
                    not isinstance(
                        operation_error,
                        _RetranscribeStagingIntegrityError,
                    )
                    or operation_error.restore_via_open_fds
                )
                if restore_via_open_fds:
                    try:
                        _restore_checked_moves(
                            checkpoint_stage_fd,
                            checkpoint_source_fd,
                            moved_checkpoint_entries,
                            token,
                        )
                        _require_open_entry_identity(
                            checkpoints_fd,
                            meeting_id,
                            os.fstat(checkpoint_source_fd),
                        )
                        current_stage = _entry_stat(checkpoints_fd, checkpoint_stage.name)
                        if current_stage is not None and (
                            current_stage.st_dev,
                            current_stage.st_ino,
                        ) == (
                            opened_checkpoint_stage.st_dev,
                            opened_checkpoint_stage.st_ino,
                        ):
                            os.rmdir(checkpoint_stage.name, dir_fd=checkpoints_fd)
                    except (OSError, JobQueueError) as restore_error:
                        raise JobQueueError(
                            f"checkpoint staging entry 원복 실패: {restore_error}"
                        ) from operation_error
                raise

        output_stat = _entry_stat(outputs_fd, meeting_id)
        if output_stat is not None:
            if not stat.S_ISDIR(output_stat.st_mode):
                raise HTTPException(
                    status_code=400,
                    detail="output 경로가 디렉터리가 아닙니다.",
                )
            output_source_fd, opened_output = _open_child_directory(outputs_fd, meeting_id)
            for filename in RETRANSCRIBE_OUTPUT_FILES:
                source_stat = _entry_stat(output_source_fd, filename)
                if source_stat is None:
                    continue
                if not stat.S_ISREG(source_stat.st_mode):
                    raise HTTPException(
                        status_code=400,
                        detail=f"재전사 산출물이 일반 파일이 아닙니다: {filename}",
                    )
                if outputs_lexical / meeting_id / filename == audio_lexical:
                    raise HTTPException(
                        status_code=409,
                        detail=f"원본 오디오와 재전사 산출물 경로가 충돌합니다: {filename}",
                    )
                if output_stage_fd is None:
                    os.mkdir(output_stage_path.name, mode=0o700, dir_fd=outputs_fd)
                    mutation_started = True
                    output_stage_fd, opened_output_stage = _open_child_directory(
                        outputs_fd,
                        output_stage_path.name,
                    )
                if opened_output_stage is None:
                    raise JobQueueError("output stage identity를 확정할 수 없습니다")
                moved_output_entries[filename] = _move_entry_checked(
                    output_source_fd,
                    output_stage_fd,
                    filename,
                    source_stat,
                    token,
                )
                try:
                    _require_open_entry_identity(
                        outputs_fd,
                        output_stage_path.name,
                        opened_output_stage,
                    )
                except JobQueueError as stage_error:
                    raise _RetranscribeStagingIntegrityError(
                        "output stage entry가 staging 중 이동되어 중단했습니다."
                    ) from stage_error
            _require_open_entry_identity(outputs_fd, meeting_id, opened_output)

        _verify_pinned_retranscribe_root(
            checkpoints_lexical,
            checkpoints_fd,
            checkpoints_identity,
        )
        _verify_pinned_retranscribe_root(outputs_lexical, outputs_fd, outputs_identity)
        if output_stage_fd is not None and opened_output_stage is not None:
            try:
                _require_open_entry_identity(
                    outputs_fd,
                    output_stage_path.name,
                    opened_output_stage,
                )
            except JobQueueError as stage_error:
                raise _RetranscribeStagingIntegrityError(
                    "output stage entry가 성공 반환 직전 이동되어 중단했습니다."
                ) from stage_error
    except BaseException as operation_error:
        if (
            moved_output_entries
            and output_stage_fd is not None
            and output_source_fd is not None
            and outputs_fd is not None
        ):
            try:
                _restore_checked_moves(
                    output_stage_fd,
                    output_source_fd,
                    moved_output_entries,
                    token,
                )
                moved_output_entries.clear()
                _require_open_entry_identity(
                    outputs_fd,
                    meeting_id,
                    os.fstat(output_source_fd),
                )
            except (OSError, JobQueueError) as restore_error:
                raise JobQueueError(
                    f"output staging entry FD 원복 실패: {restore_error}"
                ) from operation_error
        if mutation_started and outputs_fd is not None:
            try:
                _rollback_retranscribe_staging_fds(
                    checkpoints_fd,
                    outputs_fd,
                    meeting_id,
                    token,
                )
            except Exception as rollback_error:
                raise JobQueueError(
                    f"재전사 staging 실패 후 descriptor rollback 실패: {rollback_error}"
                ) from operation_error
        if isinstance(operation_error, _RetranscribeStagingIntegrityError):
            raise
        if isinstance(operation_error, JobQueueError):
            status_code = (
                409 if "변경" in str(operation_error) or "교체" in str(operation_error) else 400
            )
            raise HTTPException(
                status_code=status_code, detail=str(operation_error)
            ) from operation_error
        raise
    finally:
        if checkpoint_stage_fd is not None:
            os.close(checkpoint_stage_fd)
        if output_stage_fd is not None:
            os.close(output_stage_fd)
        if output_source_fd is not None:
            os.close(output_source_fd)
        if checkpoint_source_fd is not None:
            os.close(checkpoint_source_fd)
        if outputs_fd is not None:
            os.close(outputs_fd)
        os.close(checkpoints_fd)


def _write_retranscribe_recovery_marker(
    config: Any,
    meeting_id: str,
    reason: str,
    purge_result: Any,
) -> None:
    """인덱스 purge 이후 marker를 pinned checkpoint root에 no-clobber 기록한다."""
    checkpoints_root = _configured_lexical_path(config, "checkpoints_dir")
    marker_path = _lexical_artifact_path(
        checkpoints_root,
        meeting_id,
        "reindex_required.json",
    )
    payload = json.dumps(
        {
            "meeting_id": meeting_id,
            "reason": reason,
            "chroma_deleted": int(getattr(purge_result, "chroma_deleted", 0) or 0),
            "fts_deleted": int(getattr(purge_result, "fts_deleted", 0) or 0),
            "created_at": datetime.now().isoformat(),
            "recommended_action": f"POST /api/meetings/{meeting_id}/reindex",
        },
        ensure_ascii=False,
        indent=2,
    )
    encoded = f"{payload}\n".encode()
    lexical, root_fd, root_identity = _open_pinned_retranscribe_root(
        checkpoints_root,
        create=True,
    )
    meeting_fd: int | None = None
    temp_fd: int | None = None
    temp_name: str | None = None
    temp_identity: os.stat_result | None = None
    marker_owned = False
    created_meeting = False
    try:
        meeting_stat = _entry_stat(root_fd, meeting_id)
        if meeting_stat is None:
            os.mkdir(meeting_id, mode=0o700, dir_fd=root_fd)
            created_meeting = True
            os.fsync(root_fd)
        meeting_fd, opened_meeting = _open_child_directory(root_fd, meeting_id)
        existing_marker = _entry_stat(meeting_fd, marker_path.name)
        if existing_marker is not None:
            if not stat.S_ISREG(existing_marker.st_mode):
                raise JobQueueError("reindex recovery marker가 안전한 일반 파일이 아닙니다")
            _require_open_entry_identity(root_fd, meeting_id, opened_meeting)
            _verify_pinned_retranscribe_root(lexical, root_fd, root_identity)
            return

        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise JobQueueError("O_NOFOLLOW를 지원하지 않아 recovery marker를 쓸 수 없습니다")
        for _ in range(100):
            candidate = f".reindex-required-{uuid.uuid4().hex}.tmp"
            try:
                temp_fd = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | int(no_follow),
                    0o600,
                    dir_fd=meeting_fd,
                )
            except FileExistsError:
                continue
            temp_name = candidate
            break
        if temp_fd is None or temp_name is None:
            raise JobQueueError("reindex recovery marker 임시 이름을 할당하지 못했습니다")

        remaining = memoryview(encoded)
        while remaining:
            written = os.write(temp_fd, remaining)
            if written <= 0:
                raise OSError("reindex recovery marker write가 진행되지 않았습니다")
            remaining = remaining[written:]
        os.fsync(temp_fd)
        temp_identity = os.fstat(temp_fd)
        temp_entry = os.stat(temp_name, dir_fd=meeting_fd, follow_symlinks=False)
        if not stat.S_ISREG(temp_identity.st_mode) or _file_identity(temp_entry) != _file_identity(
            temp_identity
        ):
            raise JobQueueError("reindex recovery marker temp identity가 변경되었습니다")

        try:
            os.link(
                temp_name,
                marker_path.name,
                src_dir_fd=meeting_fd,
                dst_dir_fd=meeting_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            existing_marker = os.stat(
                marker_path.name,
                dir_fd=meeting_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(existing_marker.st_mode):
                raise JobQueueError("동시 생성된 recovery marker가 일반 파일이 아닙니다") from exc
        else:
            marker_owned = True
            published_entry = os.stat(
                marker_path.name,
                dir_fd=meeting_fd,
                follow_symlinks=False,
            )
            if (published_entry.st_dev, published_entry.st_ino) != (
                temp_identity.st_dev,
                temp_identity.st_ino,
            ):
                raise JobQueueError("reindex recovery marker publish identity 검증 실패")

        current_temp = os.stat(temp_name, dir_fd=meeting_fd, follow_symlinks=False)
        if (current_temp.st_dev, current_temp.st_ino) != (
            temp_identity.st_dev,
            temp_identity.st_ino,
        ):
            raise JobQueueError("reindex recovery marker temp entry가 교체되었습니다")
        os.unlink(temp_name, dir_fd=meeting_fd)
        temp_name = None
        os.fsync(meeting_fd)
        _require_open_entry_identity(root_fd, meeting_id, opened_meeting)
        _verify_pinned_retranscribe_root(lexical, root_fd, root_identity)
    except BaseException:
        if meeting_fd is not None:
            if marker_owned and temp_identity is not None:
                try:
                    current = os.stat(
                        marker_path.name,
                        dir_fd=meeting_fd,
                        follow_symlinks=False,
                    )
                    if (current.st_dev, current.st_ino) == (
                        temp_identity.st_dev,
                        temp_identity.st_ino,
                    ):
                        os.unlink(marker_path.name, dir_fd=meeting_fd)
                except OSError:
                    pass
            if temp_name is not None and temp_identity is not None:
                try:
                    current = os.stat(temp_name, dir_fd=meeting_fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (
                        temp_identity.st_dev,
                        temp_identity.st_ino,
                    ):
                        os.unlink(temp_name, dir_fd=meeting_fd)
                except OSError:
                    pass
        if created_meeting:
            removed_meeting = False
            try:
                os.rmdir(meeting_id, dir_fd=root_fd)
                removed_meeting = True
            except OSError:
                pass
            if removed_meeting:
                try:
                    os.fsync(root_fd)
                except OSError:
                    pass
        raise
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if meeting_fd is not None:
            os.close(meeting_fd)
        os.close(root_fd)


def _build_meeting_item(
    job: Any,
    *,
    pipeline_state: dict[str, Any] | None = None,
    status_detail: str = "",
) -> MeetingItem:
    """Job 과 pipeline_state 를 API 응답 스키마로 변환한다."""
    from core.transcription_models import (
        OPENAI_PROVIDER,
        OPENAI_TRANSCRIBE_DIARIZE_MODEL,
    )

    def _valid_stt_snapshot(provider: Any, model: Any) -> tuple[str, str] | None:
        """응답에 노출해도 되는 provider/model 쌍만 반환한다."""
        if provider not in {"local", OPENAI_PROVIDER} or not isinstance(model, str) or not model:
            return None
        if provider == OPENAI_PROVIDER and model != OPENAI_TRANSCRIBE_DIARIZE_MODEL:
            return None
        return str(provider), model

    skipped_steps = []
    degraded = False
    stt_provider = ""
    stt_model = ""
    if pipeline_state is not None:
        raw_skipped = pipeline_state.get("skipped_steps", [])
        if isinstance(raw_skipped, list):
            skipped_steps = [str(step) for step in raw_skipped]
        degraded = bool(pipeline_state.get("degraded", False))
        state_snapshot = _valid_stt_snapshot(
            pipeline_state.get("stt_provider", ""),
            pipeline_state.get("stt_model", ""),
        )
        if state_snapshot is not None:
            stt_provider, stt_model = state_snapshot

    if not stt_provider:
        job_snapshot = _valid_stt_snapshot(
            getattr(job, "stt_provider", ""),
            getattr(job, "stt_model", ""),
        )
        if job_snapshot is not None:
            stt_provider, stt_model = job_snapshot

    return MeetingItem(
        id=job.id,
        meeting_id=job.meeting_id,
        audio_path=job.audio_path,
        status=job.status,
        retry_count=job.retry_count,
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
        title=getattr(job, "title", "") or "",
        degraded=degraded,
        skipped_steps=skipped_steps,
        status_detail=status_detail,
        stt_provider=stt_provider,
        stt_model=stt_model,
    )


async def reconcile_job_state_for_response(
    raw_queue: Any,
    config: Any,
    job: Any,
    *,
    include_pipeline_state: bool = True,
) -> tuple[Any, dict[str, Any] | None, str]:
    """완료 체크포인트와 실패 job.status 가 충돌하면 DB 상태를 복구한다."""
    from core.job_queue import JobStatus

    if job.status != JobStatus.FAILED.value:
        pipeline_state = (
            _read_pipeline_state_for_response(config, job.meeting_id)
            if include_pipeline_state
            else None
        )
        return job, pipeline_state, ""

    pipeline_state = _read_pipeline_state_for_response(config, job.meeting_id)
    if pipeline_state is None or pipeline_state.get("status") != "completed":
        return job, pipeline_state, ""

    if not _has_transcript_artifact(config, job.meeting_id):
        return job, pipeline_state, ""

    reason = (
        "pipeline_state.status=completed 와 전사 산출물이 확인되어 "
        "failed job.status 를 completed 로 복구함"
    )
    try:
        updated_job = await asyncio.to_thread(
            raw_queue.force_set_status,
            job.id,
            JobStatus.COMPLETED,
            "",
        )
        logger.warning(
            "회의 상태 불일치 자동 복구: meeting_id=%s, job_id=%s, failed → completed",
            job.meeting_id,
            job.id,
        )
        return updated_job, pipeline_state, reason
    except Exception as exc:
        logger.error(
            "회의 상태 불일치 자동 복구 실패: meeting_id=%s, job_id=%s, error=%s",
            job.meeting_id,
            job.id,
            exc,
        )
        return job, pipeline_state, "상태 불일치 감지됨: DB 복구 실패"


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """백그라운드 태스크의 미처리 예외를 로깅한다."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            f"백그라운드 태스크 실패: {task.get_name()}: {exc}",
            exc_info=exc,
        )


class MeetingItem(BaseModel):
    """회의 목록 아이템 스키마.

    Attributes:
        id: 작업 ID
        meeting_id: 회의 고유 식별자
        audio_path: 오디오 파일 경로
        status: 현재 상태
        retry_count: 재시도 횟수
        error_message: 에러 메시지
        created_at: 생성 시각
        updated_at: 수정 시각
        title: 사용자 정의 제목 (빈 문자열이면 프론트가 타임스탬프 폴백)
    """

    id: int
    meeting_id: str
    audio_path: str
    status: str
    retry_count: int = 0
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""
    title: str = ""
    degraded: bool = False
    skipped_steps: list[str] = Field(default_factory=list)
    status_detail: str = ""
    stt_provider: str = ""
    stt_model: str = ""


class TranscribeMeetingRequest(BaseModel):
    """개별 회의 전사에만 적용할 모델 선택 요청."""

    model_id: str | None = None
    external_upload_confirmed: bool = False


def _parse_transcribe_meeting_request(body: Any) -> TranscribeMeetingRequest:
    """비밀값을 오류 응답에 반사하지 않고 개별 전사 요청을 검증한다."""
    if body is None:
        return TranscribeMeetingRequest()
    if not isinstance(body, dict) or not set(body).issubset(
        {"model_id", "external_upload_confirmed"}
    ):
        raise HTTPException(status_code=400, detail="개별 전사 요청 형식이 올바르지 않습니다.")

    model_id = body.get("model_id")
    consent = body.get("external_upload_confirmed", False)
    if model_id is not None and not isinstance(model_id, str):
        raise HTTPException(status_code=400, detail="전사 모델 ID 형식이 올바르지 않습니다.")
    if type(consent) is not bool:
        raise HTTPException(status_code=400, detail="외부 전송 동의 값은 boolean이어야 합니다.")
    return TranscribeMeetingRequest(
        model_id=model_id,
        external_upload_confirmed=consent,
    )


class TranscriptUtteranceItem(BaseModel):
    """전사문 개별 발화 스키마.

    Attributes:
        text: 보정된 발화 텍스트
        original_text: 원본 STT 텍스트
        speaker: 화자 라벨 (예: "SPEAKER_00")
        start: 발화 시작 시간 (초)
        end: 발화 종료 시간 (초)
        was_corrected: LLM 보정 적용 여부
    """

    text: str
    original_text: str = ""
    speaker: str = "UNKNOWN"
    start: float = 0.0
    end: float = 0.0
    was_corrected: bool = False


class TranscriptResponse(BaseModel):
    """전사문 응답 스키마.

    Attributes:
        utterances: 보정된 발화 목록
        meeting_id: 회의 고유 식별자
        num_speakers: 감지된 화자 수
        speakers: 화자 라벨 목록
        total_utterances: 전체 발화 수
        source_stage: 응답을 구성한 산출물 단계
        readonly: 편집 가능한 최종/보정 전사인지 여부
    """

    utterances: list[TranscriptUtteranceItem] = Field(default_factory=list)
    meeting_id: str
    num_speakers: int = 0
    speakers: list[str] = Field(default_factory=list)
    total_utterances: int = 0
    source_stage: str = "corrected"
    readonly: bool = False


class SummaryResponse(BaseModel):
    """회의록 요약 응답 스키마.

    Attributes:
        markdown: 마크다운 형식의 회의록
        meeting_id: 회의 고유 식별자
        num_speakers: 화자 수
        speakers: 화자 라벨 목록
        num_utterances: 발화 수
        created_at: 회의록 생성 시각
    """

    markdown: str
    meeting_id: str
    num_speakers: int = 0
    speakers: list[str] = Field(default_factory=list)
    num_utterances: int = 0
    created_at: str = ""


@router.get("/meetings/{meeting_id}", response_model=MeetingItem)
async def get_meeting(request: Request, meeting_id: str) -> MeetingItem:
    """특정 회의의 상세 정보를 반환한다.

    meeting_id로 작업을 조회하여 상세 정보를 반환한다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 회의 고유 식별자

    Returns:
        MeetingItem: 회의 상세 정보

    Raises:
        HTTPException: 회의를 찾을 수 없을 때 (404)
    """
    queue = _get_job_queue(request)

    try:
        # meeting_id로 작업 조회 (동기 함수를 비동기로 래핑)
        import asyncio

        job = await asyncio.to_thread(
            queue.queue.get_job_by_meeting_id,
            meeting_id,
        )

        if job is None:
            raise HTTPException(
                status_code=404,
                detail=f"회의를 찾을 수 없습니다: {meeting_id}",
            )

        config = _get_config(request)
        raw_queue = getattr(queue, "queue", queue)
        job, pipeline_state, status_detail = await reconcile_job_state_for_response(
            raw_queue,
            config,
            job,
        )
        return _build_meeting_item(
            job,
            pipeline_state=pipeline_state,
            status_detail=status_detail,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"회의 상세 조회 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"회의 상세 조회 중 오류가 발생했습니다: {e}",
        ) from e


class MeetingPatchRequest(BaseModel):
    """PATCH /api/meetings/{meeting_id} 요청 본문 (부분 업데이트)."""

    title: str | None = Field(
        default=None,
        max_length=200,
        description="사용자 정의 제목 (빈 문자열이면 자동 타임스탬프 복귀)",
    )


@router.patch("/meetings/{meeting_id}", response_model=MeetingItem)
async def patch_meeting(
    request: Request,
    meeting_id: str,
    body: MeetingPatchRequest,
) -> MeetingItem:
    """회의 메타데이터를 부분 업데이트한다. 현재는 title 만 지원.

    빈 문자열을 보내면 title 이 초기화되어 프론트엔드가 자동 타임스탬프 제목으로
    돌아간다. 다른 필드(status, audio_path 등)는 이 엔드포인트로 수정할 수 없다.

    Raises:
        HTTPException 400: 유효하지 않은 meeting_id 또는 title 길이 초과
        HTTPException 404: 회의 없음
        HTTPException 503: JobQueue 미초기화
    """
    _validate_meeting_id(meeting_id)
    queue = _get_job_queue(request)

    try:
        # 기존 라우트들과 동일 패턴: queue.queue 로 raw JobQueue 접근
        raw_queue = getattr(queue, "queue", queue)
        job = await asyncio.to_thread(raw_queue.get_job_by_meeting_id, meeting_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"회의를 찾을 수 없습니다: {meeting_id}")

        if body.title is not None:
            try:
                job = await asyncio.to_thread(raw_queue.update_title, meeting_id, body.title)
            except Exception as exc:  # JobQueueError 또는 기타 검증 오류
                from core.job_queue import JobQueueError as _JQErr

                if isinstance(exc, _JQErr):
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                raise

        pipeline_state = _read_pipeline_state_for_response(
            _get_config(request),
            meeting_id,
        )
        return _build_meeting_item(job, pipeline_state=pipeline_state)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"회의 메타데이터 업데이트 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"회의 메타데이터 업데이트 중 오류가 발생했습니다: {e}",
        ) from e


@router.post("/meetings/{meeting_id}/retry")
async def retry_meeting(request: Request, meeting_id: str) -> MeetingItem:
    """실패한 회의를 재시도한다.

    meeting_id로 작업을 찾아 상태를 queued로 되돌리고 파이프라인을 재실행한다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 재시도할 회의 고유 식별자

    Returns:
        MeetingItem: 업데이트된 회의 정보

    Raises:
        HTTPException: 회의를 찾을 수 없을 때 (404), 재시도 불가 시 (409)
    """
    from core.job_queue import InvalidTransitionError, JobNotFoundError, MaxRetriesExceededError

    _validate_meeting_id(meeting_id)
    queue = _get_job_queue(request)

    try:
        import asyncio

        # meeting_id로 작업 조회
        job = await asyncio.to_thread(
            queue.queue.get_job_by_meeting_id,
            meeting_id,
        )
        if job is None:
            raise HTTPException(
                status_code=404,
                detail=f"회의를 찾을 수 없습니다: {meeting_id}",
            )

        config = _get_config(request)
        raw_queue = getattr(queue, "queue", queue)
        job, pipeline_state, status_detail = await reconcile_job_state_for_response(
            raw_queue,
            config,
            job,
        )
        if status_detail:
            if job.status == "completed":
                return _build_meeting_item(
                    job,
                    pipeline_state=pipeline_state,
                    status_detail=status_detail,
                )
            raise HTTPException(
                status_code=409,
                detail=(
                    "회의 산출물은 완료 상태로 보이지만 작업 큐 상태 복구에 실패했습니다. "
                    f"{status_detail}"
                ),
            )

        from core.transcription_models import selection_from_state_or_config

        try:
            stt_selection = selection_from_state_or_config(
                config,
                pipeline_state,
                job=job,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if stt_selection.external_upload:
            from api.routers.transcription_models import require_loopback_server

            require_loopback_server(config, request)

        # 완료 산출물 복구가 아니라 실제 파이프라인 재실행일 때만 admission을 요구한다.
        await _require_audio_quality_accept(config, Path(job.audio_path))

        # 재시도 실행 (job_id 기반)
        updated_job = await asyncio.to_thread(
            queue.queue.retry_job,
            job.id,
            stt_provider=stt_selection.provider,
            stt_model=stt_selection.model,
        )

        # 이전 취소 요청이 set 에 남아있을 수 있으니 정리 (stale 방어)
        job_processor = getattr(request.app.state, "job_processor", None)
        if job_processor is not None:
            job_processor._cancellation_requests.discard(meeting_id)

        logger.info(f"회의 재시도 요청: {meeting_id} (job_id={job.id})")

        return _build_meeting_item(updated_job, pipeline_state=pipeline_state)
    except HTTPException:
        raise
    except (InvalidTransitionError, MaxRetriesExceededError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"회의 재시도 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"회의 재시도 중 오류가 발생했습니다: {e}",
        ) from e


@router.post("/meetings/{meeting_id}/transcribe")
async def transcribe_meeting(
    request: Request,
    meeting_id: str,
    force: bool = False,
    body: Annotated[Any | None, Body()] = None,
) -> MeetingItem:
    """녹음 완료된 회의의 전사를 시작한다.

    recorded 상태의 작업을 queued로 전환하여 전사 파이프라인을 트리거한다.
    이슈 J 대응: ``force=true`` 를 전달하면 ``failed`` 상태에서도 재시도를 시작한다.
    이때 기존 에러 메시지는 지우고 retry_count 는 그대로 유지한다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 전사할 회의 고유 식별자
        force: True이면 failed 상태도 강제로 재시도한다 (쿼리파라미터)
        body: 이 회의에만 적용할 전사 모델과 외부 전송 동의

    Returns:
        MeetingItem: 업데이트된 회의 정보

    Raises:
        HTTPException: 회의를 찾을 수 없을 때 (404), 상태 전이 불가 시 (409)
    """
    from core.job_queue import InvalidTransitionError, JobNotFoundError, JobStatus

    _validate_meeting_id(meeting_id)
    queue = _get_job_queue(request)
    config = _get_config(request)
    parsed_body = _parse_transcribe_meeting_request(body)

    # 명시적 개별 선택은 회의/파일/Keychain을 읽기 전에 공개 model_id
    # 화이트리스트와 외부 전송 동의를 먼저 검증한다. body가 없으면 기존처럼
    # pipeline state → job snapshot → 전역 설정 순서로 선택한다.
    requested_selection = None
    if parsed_body.model_id is not None:
        from core.transcription_models import selection_from_id

        try:
            requested_selection = selection_from_id(
                parsed_body.model_id,
                local_model=str(config.stt.model_name),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if requested_selection.external_upload:
            if not parsed_body.external_upload_confirmed:
                raise HTTPException(
                    status_code=400,
                    detail="이 음성 파일을 OpenAI로 전송하는 데 동의해야 합니다.",
                )
            from api.routers.transcription_models import require_loopback_server
            from security.openai_keychain import get_status

            require_loopback_server(config, request)
            if not get_status().configured:
                raise HTTPException(
                    status_code=400,
                    detail="OpenAI API 키를 먼저 등록해 주세요.",
                )

    try:
        import asyncio

        job = await asyncio.to_thread(
            queue.queue.get_job_by_meeting_id,
            meeting_id,
        )
        if job is None:
            raise HTTPException(
                status_code=404,
                detail=f"회의를 찾을 수 없습니다: {meeting_id}",
            )

        force_failed = job.status == JobStatus.FAILED.value and force
        if job.status != JobStatus.RECORDED.value and not force_failed:
            detail = f"전사를 시작할 수 없는 상태입니다: {job.status} (recorded 상태만 가능)"
            if job.status == JobStatus.FAILED.value:
                # 힌트: force=true 로 재시도 가능
                detail += ". 실패한 회의를 재시도하려면 ?force=true 를 붙여 요청하세요."
            raise HTTPException(status_code=409, detail=detail)

        pipeline_state = _read_pipeline_state_for_response(config, meeting_id)
        from core.transcription_models import selection_from_state_or_config

        try:
            pinned_or_default_selection = selection_from_state_or_config(
                config,
                pipeline_state,
                job=job,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # 취소 후 재개되는 recorded 작업은 pipeline/job에 기존 모델이 고정돼
        # 있을 수 있다. 이때 다른 모델을 큐잉하면 worker 단계에서 뒤늦게 실패하므로
        # API에서 먼저 차단한다. 새 recorded 작업(고정값 없음)은 개별 선택을 허용한다.
        state_provider = str((pipeline_state or {}).get("stt_provider", "") or "")
        state_model = str((pipeline_state or {}).get("stt_model", "") or "")
        if bool(state_provider) != bool(state_model):
            raise HTTPException(
                status_code=409,
                detail="이 회의의 파이프라인 전사 모델 기록이 불완전합니다.",
            )
        # worker가 시작되기 전 queued 취소는 job snapshot만 남기므로 새 선택으로
        # 안전하게 덮어쓸 수 있다. 실제 pipeline state가 생성된 재개 작업만 hard pin이다.
        has_pinned_selection = bool(state_provider and state_model)
        if requested_selection is not None and has_pinned_selection:
            if (
                requested_selection.provider == "local"
                and pinned_or_default_selection.provider == "local"
            ):
                # 공개 `local` ID는 현재 기본 로컬 모델로 해석된다. 하지만
                # 이미 pipeline state가 있는 재개 작업은 기본값이 바뀌어도
                # 기존 pinned 로컬 모델을 계속 사용해야 체크포인트가 일관된다.
                requested_selection = pinned_or_default_selection
            elif (
                requested_selection.provider != pinned_or_default_selection.provider
                or requested_selection.model != pinned_or_default_selection.model
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "이 회의의 진행 기록에는 다른 전사 모델이 고정되어 있습니다. "
                        "기존 모델로 재개하거나 처음부터 다시 전사해 주세요."
                    ),
                )

        stt_selection = requested_selection or pinned_or_default_selection
        if stt_selection.external_upload:
            from api.routers.transcription_models import require_loopback_server

            require_loopback_server(config, request)
        await _require_audio_quality_accept(config, Path(job.audio_path))

        # failed → queued 를 한 번의 조건부 UPDATE로 수행해 recorded 중간 상태를
        # 다른 워커가 관찰하거나 선점할 수 없게 한다.
        if force_failed:
            logger.info(
                f"failed 상태 강제 재시도: {meeting_id} (job_id={job.id}, "
                f"retry_count={job.retry_count})"
            )
            updated_job = await asyncio.to_thread(
                queue.queue.queue_failed_job,
                job.id,
                stt_provider=stt_selection.provider,
                stt_model=stt_selection.model,
            )
        else:
            updated_job = await asyncio.to_thread(
                queue.queue.queue_job,
                job.id,
                "",
                stt_provider=stt_selection.provider,
                stt_model=stt_selection.model,
            )

        # 이전 취소 요청이 set 에 남아있을 수 있으니 정리 (stale 방어)
        job_processor = getattr(request.app.state, "job_processor", None)
        if job_processor is not None:
            job_processor._cancellation_requests.discard(meeting_id)

        logger.info(
            "전사 시작 요청: %s (job_id=%s, provider=%s, model=%s)",
            meeting_id,
            job.id,
            stt_selection.provider,
            stt_selection.model,
        )

        return _build_meeting_item(updated_job, pipeline_state=pipeline_state)
    except HTTPException:
        raise
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (InvalidTransitionError, JobQueueError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"전사 시작 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"전사 시작 중 오류가 발생했습니다: {e}",
        ) from e


@router.post("/meetings/{meeting_id}/cancel")
async def cancel_meeting(request: Request, meeting_id: str) -> MeetingItem:
    """진행 중(또는 대기 중)인 회의 전사를 취소하고 recorded 로 되돌린다.

    동작:
        - status == queued: 아직 워커가 잡지 않았으면 CAS로 즉시 recorded.
        - status in (transcribing, diarizing, merging, embedding):
          DB cancellation claim과 JobProcessor flag를 함께 등록.
          현재 실행 중인 단계가 끝난 뒤 다음 단계 경계에서 CancelledError 가 발생하여
          orchestrator 가 status 를 recorded 로 되돌리고 brodcast.
        - 그 외 상태: 409 (취소 대상 아님)

    Args:
        request: FastAPI Request
        meeting_id: 취소할 회의 ID

    Returns:
        업데이트된 MeetingItem (queued 였다면 즉시 recorded, 진행 중이었다면
        아직 recorded 가 아닐 수 있음 — 프론트가 폴링/브로드캐스트로 갱신)

    Raises:
        HTTPException: 회의 없음(404), 취소 대상 상태 아님(409)
    """
    from core.job_queue import InvalidTransitionError, JobNotFoundError, JobStatus

    queue = _get_job_queue(request)

    in_progress_states = {
        JobStatus.QUEUED.value,
        JobStatus.TRANSCRIBING.value,
        JobStatus.DIARIZING.value,
        JobStatus.MERGING.value,
        JobStatus.EMBEDDING.value,
    }

    try:
        job = await asyncio.to_thread(queue.queue.get_job_by_meeting_id, meeting_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail=f"회의를 찾을 수 없습니다: {meeting_id}",
            )

        if job.status not in in_progress_states:
            raise HTTPException(
                status_code=409,
                detail=f"취소할 수 있는 상태가 아닙니다: {job.status}",
            )

        job_processor = getattr(request.app.state, "job_processor", None)

        # queued: recorded 전환을 먼저 CAS한다. 워커가 그 사이 작업을
        # 선점했다면 durable cancellation claim을 남긴 뒤 process-local flag도
        # 등록한다. claim은 요청 처리 직후 프로세스가 종료돼도 복구된다.
        if job.status == JobStatus.QUEUED.value:
            try:
                updated_job = await queue.cancel_queued_job(
                    job.id,
                    "사용자가 취소함 (대기 중)",
                )
            except InvalidTransitionError:
                updated_job = await asyncio.to_thread(queue.queue.get_job, job.id)
                if updated_job.status not in {
                    JobStatus.TRANSCRIBING.value,
                    JobStatus.DIARIZING.value,
                    JobStatus.MERGING.value,
                    JobStatus.EMBEDDING.value,
                }:
                    raise HTTPException(
                        status_code=409,
                        detail=f"취소할 수 있는 상태가 아닙니다: {updated_job.status}",
                    ) from None
                if job_processor is None:
                    raise HTTPException(
                        status_code=503,
                        detail="JobProcessor 가 초기화되지 않아 취소할 수 없습니다.",
                    ) from None
                updated_job = await asyncio.to_thread(
                    queue.queue.claim_active_job_for_cancellation,
                    job.id,
                    uuid.uuid4().hex,
                )
                job_processor.request_cancellation(meeting_id)
        else:
            # 실행 중: DB claim을 먼저 남기고 메모리 flag도 등록한다. API task가
            # claim 직후 취소되어도 pipeline callback은 DB claim을 직접 확인한다.
            if job_processor is None:
                raise HTTPException(
                    status_code=503,
                    detail="JobProcessor 가 초기화되지 않아 취소할 수 없습니다.",
                )
            updated_job = await asyncio.to_thread(
                queue.queue.claim_active_job_for_cancellation,
                job.id,
                uuid.uuid4().hex,
            )
            job_processor.request_cancellation(meeting_id)

        logger.info(f"취소 요청 처리: {meeting_id} (이전 status={job.status})")

        pipeline_state = _read_pipeline_state_for_response(
            _get_config(request),
            meeting_id,
        )
        return _build_meeting_item(
            updated_job,
            pipeline_state=pipeline_state,
            status_detail=(
                "취소 요청이 저장되었습니다. 현재 외부 요청이 끝나는 즉시 중단합니다."
                if updated_job.status == JobStatus.RECORDING.value
                else ""
            ),
        )
    except HTTPException:
        raise
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"취소 처리 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"취소 처리 중 오류가 발생했습니다: {e}",
        ) from e


@router.post(
    "/meetings/{meeting_id}/re-transcribe",
    dependencies=[Depends(_meeting_mutation_lease)],
)
async def re_transcribe_meeting(request: Request, meeting_id: str) -> MeetingItem:
    """기존 전사 결과를 폐기하고 처음부터 다시 전사한다.

    completed/failed 상태의 작업을 대상으로:
        1. ChromaDB/FTS5 의 stale 청크 삭제
        2. 체크포인트 디렉토리 전체 삭제 (pipeline_state.json 포함)
        3. 출력 디렉토리의 전사·요약 산출물 staging (오디오는 보존)
        4. job 상태를 queued 로 강제 전환 (retry_count 0 으로 리셋)

    Args:
        request: FastAPI Request 객체
        meeting_id: 재전사할 회의 고유 식별자

    Returns:
        MeetingItem: 업데이트된 회의 정보 (status=queued)

    Raises:
        HTTPException: 회의를 찾을 수 없을 때 (404), 재전사 불가 상태 (409)
    """
    from core.job_queue import InvalidTransitionError, JobNotFoundError, JobStatus

    _validate_meeting_id(meeting_id)
    queue = _get_job_queue(request)
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="설정이 초기화되지 않았습니다.")
    from core.transcription_models import selection_from_config

    try:
        stt_selection = selection_from_config(config)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if stt_selection.external_upload:
        from api.routers.transcription_models import require_loopback_server

        require_loopback_server(config, request)

    try:
        job = await asyncio.to_thread(queue.queue.get_job_by_meeting_id, meeting_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail=f"회의를 찾을 수 없습니다: {meeting_id}",
            )

        allowed_statuses = {JobStatus.COMPLETED.value, JobStatus.FAILED.value}
        if job.status not in allowed_statuses:
            raise HTTPException(
                status_code=409,
                detail=(
                    "재전사를 시작할 수 없는 상태입니다: "
                    f"{job.status} (completed 또는 failed 상태만 가능)"
                ),
            )

        checkpoints_root = _configured_lexical_path(config, "checkpoints_dir")
        outputs_root = _configured_lexical_path(config, "outputs_dir")
        audio_path = _require_audio_in_config_base(config, Path(job.audio_path))
        admission_identity = await _require_audio_quality_accept(config, audio_path)

        # admission 이후에도 상태가 그대로인지 조건부 UPDATE로 확인하고 예약한다.
        # 예약 상태(recording)는 일반 retry/transcribe/batch가 큐에 넣을 수 없어
        # 파일 staging/purge와 다른 요청의 상태 전이가 경합하지 않는다.
        claim_token = uuid.uuid4().hex
        await asyncio.to_thread(
            queue.queue.claim_for_retranscribe,
            job.id,
            claim_token,
            stt_provider=stt_selection.provider,
            stt_model=stt_selection.model,
        )

        purge_result: Any | None = None
        try:
            # 품질 검증과 claim 사이에 source가 교체될 수 있으므로 destructive
            # staging 직전에 동일 identity인지 다시 확인한다.
            await _require_audio_identity_unchanged(audio_path, admission_identity)
            await asyncio.to_thread(
                queue.queue.update_retranscribe_claim_phase,
                job.id,
                claim_token,
                "staging",
            )
            # 로컬 산출물은 삭제하지 않고 같은 파일시스템의 숨김 staging 경로로
            # rename한다. 이후 단계가 실패하면 원자 rename으로 복구할 수 있다.
            await asyncio.to_thread(
                _stage_retranscribe_artifacts,
                config,
                meeting_id,
                audio_path,
                claim_token,
            )

            # staging 완료 뒤 인덱스를 purge하고, 마지막에 claim을 queued로 commit한다.
            await asyncio.to_thread(
                queue.queue.update_retranscribe_claim_phase,
                job.id,
                claim_token,
                "purging",
            )
            purge_result = await _purge_meeting_search_index(config, meeting_id, "재전사")
            await asyncio.to_thread(
                queue.queue.update_retranscribe_claim_phase,
                job.id,
                claim_token,
                "committing",
            )
            # queued 공개 전에 이전 세대 staging을 엄격히 지운다. 중간 실패 시
            # committing claim을 유지해 startup recovery가 멱등하게 이어서 정리한다.
            await asyncio.to_thread(
                cleanup_retranscribe_staging,
                checkpoints_root,
                outputs_root,
                meeting_id,
                claim_token,
            )
            updated_job = await asyncio.to_thread(
                queue.queue.reset_for_retranscribe,
                job.id,
                claim_token,
            )
        except BaseException as operation_error:
            recovery_errors: list[str] = []
            durable_phase: str | None = None
            preserve_integrity_claim = isinstance(
                operation_error,
                _RetranscribeStagingIntegrityError,
            )
            try:
                claimed_job = await asyncio.to_thread(queue.queue.get_job, job.id)
                durable_claim = parse_retranscribe_claim(
                    str(getattr(claimed_job, "requested_action", ""))
                )
                if durable_claim is not None and durable_claim.token == claim_token:
                    durable_phase = durable_claim.phase
                else:
                    recovery_errors.append("재전사 durable claim을 확인할 수 없습니다")
            except Exception as claim_read_error:
                recovery_errors.append(f"재전사 claim phase 조회 실패: {claim_read_error}")

            # committing은 purge가 완료된 뒤의 roll-forward 전용 상태다. cleanup이나
            # finalize가 실패해도 산출물을 rollback하지 않고 startup recovery가
            # strict cleanup → queued finalize 순서로 이어간다.
            if durable_phase == "committing":
                if not isinstance(operation_error, Exception):
                    raise
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "재전사 commit을 완료하지 못해 startup 복구 대기 상태로 보존했습니다: "
                        f"{operation_error}"
                    ),
                ) from operation_error

            rollback_succeeded = False
            if durable_phase in {"claimed", "staging", "purging"}:
                try:
                    await asyncio.to_thread(
                        rollback_retranscribe_staging,
                        checkpoints_root,
                        outputs_root,
                        meeting_id,
                        claim_token,
                    )
                    rollback_succeeded = True
                except Exception as rollback_error:
                    recovery_errors.append(str(rollback_error))

            marker_succeeded = durable_phase != "purging"
            if rollback_succeeded and durable_phase == "purging":
                try:
                    await asyncio.to_thread(
                        _write_retranscribe_recovery_marker,
                        config,
                        meeting_id,
                        f"재전사 commit 실패: {operation_error}",
                        purge_result or IndexPurgeResult(meeting_id=meeting_id),
                    )
                    marker_succeeded = True
                except Exception as marker_error:
                    recovery_errors.append(f"reindex marker 기록 실패: {marker_error}")

            # 파일 rollback과 (purging이면) recovery marker가 durable해진 뒤에만
            # completed/failed 상태를 다시 노출한다.
            if rollback_succeeded and marker_succeeded and not preserve_integrity_claim:
                try:
                    await asyncio.to_thread(
                        queue.queue.restore_retranscribe_claim,
                        job.id,
                        claim_token,
                    )
                except Exception as restore_error:
                    recovery_errors.append(f"job claim 복구 실패: {restore_error}")

            if recovery_errors:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"재전사 실패 후 복구가 완전하지 않습니다: {operation_error}; "
                        + "; ".join(recovery_errors)
                    ),
                ) from operation_error
            raise

        # 이전 취소 요청이 set 에 남아있을 수 있으니 정리 (stale 방어)
        job_processor = getattr(request.app.state, "job_processor", None)
        if job_processor is not None:
            job_processor._cancellation_requests.discard(meeting_id)

        logger.info(f"재전사 요청: {meeting_id} (job_id={job.id})")

        return _build_meeting_item(updated_job)
    except HTTPException:
        raise
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"재전사 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"재전사 중 오류가 발생했습니다: {e}",
        ) from e


@router.get("/meetings/{meeting_id}/pipeline-state")
async def get_pipeline_state(request: Request, meeting_id: str) -> dict[str, Any]:
    """파이프라인 실행 상태 (단계별 소요시간 포함) 를 반환한다.

    `~/.meeting-transcriber/checkpoints/{meeting_id}/pipeline_state.json` 을 그대로 반환한다.
    프론트엔드 로그 탭에서 단계별 elapsed_seconds 와 총 소요시간을 표시한다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 회의 고유 식별자

    Returns:
        PipelineState 직렬화 dict + total_elapsed_seconds (편의 필드)

    Notes:
        오래된 회의나 수동 시드 데이터에는 pipeline_state.json 이 없을 수 있다.
        이 경우 프론트엔드 콘솔에 404 노이즈를 남기지 않도록 빈 상태를 반환한다.
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="설정이 초기화되지 않았습니다.")

    try:
        loaded = await asyncio.to_thread(_read_pipeline_state_json_pinned, config, meeting_id)
    except (JobQueueError, OSError, json.JSONDecodeError) as e:
        logger.exception(f"pipeline_state.json 읽기 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"파이프라인 상태를 읽을 수 없습니다: {e}",
        ) from e

    if loaded is None:
        return {
            "status": "missing",
            "step_results": [],
            "skipped_steps": [],
            "warnings": [],
            "total_elapsed_seconds": 0.0,
        }

    data = cast(dict[str, Any], loaded)

    # 편의: 총 소요시간 계산 (step_results 의 elapsed_seconds 합산)
    step_results = data.get("step_results", []) or []
    total_elapsed = sum(float(step.get("elapsed_seconds") or 0.0) for step in step_results)
    data["total_elapsed_seconds"] = round(total_elapsed, 2)
    return data


# === 회의 음성 재생 ===


# 재생 가능한 오디오 확장자 (HTML <audio> 호환)
_PLAYABLE_AUDIO_EXTS: tuple[str, ...] = (".wav", ".mp3", ".m4a", ".flac", ".ogg")

# 확장자 → MIME 매핑 (표준 우선)
_AUDIO_MIME_BY_EXT: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
}


def _find_meeting_audio_path(config: Any, meeting_id: str) -> Path | None:
    """회의의 재생 가능한 오디오 파일을 찾는다.

    탐색 우선순위:
        1. checkpoints/{id}/pipeline_state.json 의 ``wav_path`` (16kHz 변환본 — 회의록 화자분리·STT 의 정답 시간축과 동일)
        2. checkpoints/{id}/pipeline_state.json 의 ``audio_path`` (원본)
        3. outputs/{id}/ 디렉토리 내 ``*_16k.wav`` 또는 임의 ``*.wav`` (폴백)

    Args:
        config: AppConfig
        meeting_id: 회의 고유 식별자 (이미 검증된 값)

    Returns:
        실제 존재하는 오디오 파일 Path, 못 찾으면 None.
    """
    try:
        loaded_state = _read_pipeline_state_json_pinned(config, meeting_id)
        data = loaded_state if isinstance(loaded_state, dict) else {}
    except (JobQueueError, OSError, json.JSONDecodeError):
        data = {}

    # wav_path 가 회의록 시간축과 일치하므로 우선 사용
    for key in ("wav_path", "audio_path"):
        value = data.get(key)
        if isinstance(value, str) and value:
            candidate = Path(value)
            if candidate.is_file() and candidate.suffix.lower() in _PLAYABLE_AUDIO_EXTS:
                return candidate

    # 폴백: outputs/{id}/ 디렉토리 글롭
    outputs_root = config.paths.resolved_outputs_dir / meeting_id
    if outputs_root.is_dir():
        # 16kHz 변환본을 우선, 없으면 임의 wav
        for pattern in ("*_16k.wav", "*.wav"):
            matches = sorted(outputs_root.glob(pattern))
            if matches:
                return cast(Path, matches[0])

    return None


def _parse_range_header(range_header: str, file_size: int) -> tuple[int, int] | None:
    """HTTP Range 헤더를 파싱한다 (단일 range 만 지원).

    지원 형식:
        - ``bytes=START-END`` — 명시적 범위
        - ``bytes=START-`` — START 부터 끝까지
        - ``bytes=-N`` — 마지막 N 바이트 (suffix range)

    multipart range (``bytes=0-100,200-300``) 는 복잡도 대비 활용도가 낮아 미지원.

    Args:
        range_header: Range 헤더 원본 문자열
        file_size: 대상 파일 크기 (바이트)

    Returns:
        (start, end) 튜플 — 둘 다 inclusive. 형식 불량·범위 초과 시 None.
    """
    if not range_header.lower().startswith("bytes="):
        return None

    spec = range_header[len("bytes=") :].strip()
    if "," in spec:
        # multipart range 미지원
        return None

    parts = spec.split("-", 1)
    if len(parts) != 2:
        return None

    start_s, end_s = parts[0].strip(), parts[1].strip()
    try:
        if start_s == "":
            # suffix range: 마지막 N 바이트
            if end_s == "":
                return None
            n = int(end_s)
            if n <= 0:
                return None
            start = max(0, file_size - n)
            end = file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s != "" else file_size - 1
    except ValueError:
        return None

    if start < 0 or start >= file_size or end < start:
        return None

    end = min(end, file_size - 1)
    return (start, end)


@router.get("/meetings/{meeting_id}/audio")
async def get_meeting_audio(request: Request, meeting_id: str) -> Any:
    """회의의 원본 음성을 재생용으로 스트리밍한다 (HTTP Range 지원).

    프론트엔드 ViewerView 에서 utterance 별 ▶ 버튼이 클릭되면
    ``<audio>`` 요소가 ``currentTime = u.start`` 으로 seek 한 뒤 play 한다.
    Range 헤더 (``Accept-Ranges: bytes``) 를 응답하므로 브라우저가 임의 시점으로
    바로 점프할 수 있다.

    Args:
        request: FastAPI Request
        meeting_id: 회의 고유 식별자

    Returns:
        FileResponse (전체 파일, 200) 또는 StreamingResponse (Range, 206)

    Raises:
        HTTPException: 잘못된 ID 형식 (400), 음성 파일 없음 (404), 설정 미초기화 (503)
    """
    from fastapi.responses import FileResponse, Response, StreamingResponse

    _validate_meeting_id(meeting_id)
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")

    audio_path = await asyncio.to_thread(_find_meeting_audio_path, config, meeting_id)
    if audio_path is None:
        raise HTTPException(
            status_code=404,
            detail=f"재생 가능한 음성 파일이 없습니다: {meeting_id}",
        )

    file_size = audio_path.stat().st_size
    media_type = _AUDIO_MIME_BY_EXT.get(audio_path.suffix.lower(), "application/octet-stream")

    # Range 요청 처리
    range_header = request.headers.get("range") or request.headers.get("Range")
    if range_header:
        parsed = _parse_range_header(range_header, file_size)
        if parsed is None:
            # 416 Range Not Satisfiable — 클라이언트가 잘못된 범위를 요청
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        start, end = parsed
        length = end - start + 1

        def _iter_range():
            """파일을 64KB 청크로 부분 스트리밍한다."""
            with open(audio_path, "rb") as f:
                f.seek(start)
                remaining = length
                chunk_size = 64 * 1024
                while remaining > 0:
                    chunk = f.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            _iter_range(),
            status_code=206,
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                # 같은 파일에 대한 반복 seek 시 브라우저 캐시 활용
                "Cache-Control": "private, max-age=3600",
            },
        )

    # 전체 파일 응답 (Range 헤더 없는 첫 요청 또는 단순 다운로드)
    return FileResponse(
        path=audio_path,
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.delete(
    "/meetings/{meeting_id}",
    dependencies=[Depends(_meeting_mutation_lease)],
)
async def delete_meeting(request: Request, meeting_id: str) -> dict[str, str]:
    """회의를 삭제한다 (검색 인덱스 + DB 레코드 + 오디오 파일 → quarantine).

    Phase 1-7: 오디오 파일이 watcher에 의해 재감지되는 문제를 차단하기 위해
    검색 인덱스/DB 삭제와 함께 원본 오디오 파일을 quarantine 디렉토리로 이동한다.
    파일 이동 실패는 best-effort(경고 로그만) 처리하여 DB 삭제 자체는
    항상 성공시킨다. 파일이 이미 없는 경우(사용자가 직접 삭제했거나,
    예전에 격리되었거나)도 마찬가지로 DB 삭제는 성공 처리한다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 삭제할 회의 고유 식별자

    Returns:
        삭제 완료 메시지

    Raises:
        HTTPException: 회의를 찾을 수 없을 때 (404) 또는 DB 삭제 실패 시 (500)
    """
    import asyncio

    from core.job_queue import InvalidTransitionError, JobNotFoundError, JobStatus
    from core.quarantine import (
        move_to_quarantine_exact,
        restore_from_quarantine,
    )

    _validate_meeting_id(meeting_id)
    queue = _get_job_queue(request)
    config = _get_config(request)
    delete_claim_token: str | None = None
    delete_claim_job_id: int | None = None
    delete_claim_committed = False
    purge_result: IndexPurgeResult | None = None

    try:
        # meeting_id로 작업 조회
        job = await asyncio.to_thread(
            queue.queue.get_job_by_meeting_id,
            meeting_id,
        )
        if job is None:
            raise HTTPException(
                status_code=404,
                detail=f"회의를 찾을 수 없습니다: {meeting_id}",
            )
        if str(getattr(job, "status", "") or "") in {
            "queued",
            "recording",
            "transcribing",
            "diarizing",
            "merging",
            "embedding",
        }:
            raise HTTPException(
                status_code=409,
                detail="처리 중인 회의는 취소 완료 후 삭제할 수 있습니다.",
            )

        candidate_token = uuid.uuid4().hex
        job = await asyncio.to_thread(
            queue.queue.claim_for_deletion,
            job.id,
            candidate_token,
        )
        delete_claim_token = candidate_token
        delete_claim_job_id = job.id

        # 삭제 전 audio_path와 identity를 no-follow로 확보한다. raw base/quarantine
        # 경로도 destructive 작업 전에 검사해 symlink target으로 이동하지 않는다.
        audio_path_str = getattr(job, "audio_path", None)
        audio_path: Path | None = None
        audio_identity: AudioFileIdentity | None = None
        durable_audio_identity: tuple[int, int, int, int] | None = None
        quarantined_audio_path: Path | None = None
        quarantine_dir = _configured_lexical_path(config, "audio_quarantine_subdir")
        if audio_path_str:
            audio_path = _require_audio_in_config_base(config, Path(audio_path_str))
            try:
                audio_identity = await asyncio.to_thread(
                    inspect_audio_path_no_symlinks,
                    audio_path,
                )
            except FileNotFoundError:
                audio_path = None
            except EmptyAudioError:
                # helper가 모든 path component와 final regular-file 여부를 이미
                # no-follow 검사했다. 빈 파일도 안전하게 격리할 identity를 만든다.
                empty_stat = await asyncio.to_thread(audio_path.lstat)
                audio_identity = (
                    empty_stat.st_dev,
                    empty_stat.st_ino,
                    empty_stat.st_size,
                    empty_stat.st_mtime_ns,
                    empty_stat.st_ctime_ns,
                )
            except AudioAdmissionError as exc:
                raise HTTPException(
                    status_code=_AUDIO_FAILURE_HTTP_STATUS[exc.failure_kind],
                    detail=f"{exc.failure_kind.name}: {exc}",
                ) from exc
            except OSError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"INFRA_UNAVAILABLE: {exc}",
                ) from exc

        # source를 입력 감시 폴더 밖으로 먼저 옮긴다. DB 행을 지운 뒤
        # quarantine이 실패하면 watcher가 삭제한 회의를 재등록할 수 있다.
        if audio_path is not None and audio_identity is not None:
            durable_audio_identity = audio_identity[:4]
            quarantined_audio_path = quarantine_dir / f"deleted-{candidate_token}.audio"
            await asyncio.to_thread(
                queue.queue.prepare_delete_quarantine,
                job.id,
                candidate_token,
                source_path=str(audio_path),
                quarantine_path=str(quarantined_audio_path),
                source_identity=durable_audio_identity,
            )
            await asyncio.to_thread(
                move_to_quarantine_exact,
                audio_path,
                quarantined_audio_path,
                reason=f"사용자 삭제 준비: meeting_id={meeting_id}",
                expected_identity=audio_identity,
            )
        await asyncio.to_thread(
            queue.queue.update_delete_claim_phase,
            job.id,
            candidate_token,
            "quarantined",
        )

        # 검색 인덱스 삭제. 실패하면 DB 레코드와 오디오 파일을 보존한다.
        await asyncio.to_thread(
            queue.queue.update_delete_claim_phase,
            job.id,
            candidate_token,
            "purging",
        )
        purge_result = await _purge_meeting_search_index(config, meeting_id, "삭제")

        # 이 단계부터는 rollback하지 않고 startup이 삭제를 끝까지 roll-forward한다.
        # raw provider cache를 먼저 비운 뒤에만 DB tombstone을 제거하므로,
        # cleanup 실패나 SIGKILL에도 숨은 전사 원문이 고아로 남지 않는다.
        await asyncio.to_thread(
            queue.queue.update_delete_claim_phase,
            job.id,
            candidate_token,
            "committing",
        )
        from steps.openai_transcriber import cleanup_meeting_openai_resume_caches

        await asyncio.to_thread(
            cleanup_meeting_openai_resume_caches,
            config,
            meeting_id,
        )

        # claim token이 일치하는 DB 행만 삭제한다. 인덱스 purge 중 수동 전사나
        # 자동처리가 queued로 바꾸는 경쟁을 durable recording claim이 막는다.
        await asyncio.to_thread(
            queue.queue.delete_claimed_job,
            job.id,
            candidate_token,
        )
        delete_claim_committed = True
        logger.info(f"회의 DB 삭제: {meeting_id} (job_id={job.id})")

        if quarantined_audio_path is not None:
            logger.info("오디오 파일 격리와 DB 삭제 완료: %s", quarantined_audio_path)
        elif audio_path_str:
            logger.debug(f"오디오 파일이 이미 존재하지 않음: {audio_path_str}")

        return {"message": f"회의가 삭제되었습니다: {meeting_id}"}
    except HTTPException:
        raise
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"회의 삭제 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"회의 삭제 중 오류가 발생했습니다: {e}",
        ) from e
    finally:
        if (
            delete_claim_token is not None
            and delete_claim_job_id is not None
            and not delete_claim_committed
        ):
            recovery_ready = True
            try:
                claimed_job = await asyncio.to_thread(queue.queue.get_job, delete_claim_job_id)
                durable_claim = parse_delete_claim(
                    str(getattr(claimed_job, "requested_action", "") or "")
                )
                if durable_claim is None or durable_claim.token != delete_claim_token:
                    raise JobQueueError("회의 삭제 durable claim을 확인할 수 없습니다")
                if durable_claim.phase == "committing":
                    raise JobQueueError(
                        "회의 삭제 commit 단계는 startup roll-forward 대상으로 보존합니다"
                    )
                if durable_claim.source_identity is not None:
                    await asyncio.to_thread(
                        restore_from_quarantine,
                        Path(durable_claim.source_path),
                        Path(durable_claim.quarantine_path),
                        expected_identity=durable_claim.source_identity,
                        reason=f"회의 삭제 rollback: meeting_id={meeting_id}",
                    )
                if (
                    durable_claim.phase == "purging"
                    and durable_claim.original_status == JobStatus.COMPLETED.value
                ):
                    await asyncio.to_thread(
                        _write_retranscribe_recovery_marker,
                        config,
                        meeting_id,
                        "회의 삭제 index purge 중 중단되어 재색인이 필요합니다.",
                        purge_result or IndexPurgeResult(meeting_id=meeting_id),
                    )
                    from core.reindex_recovery import (
                        consume_reindex_required_marker,
                        reindex_meeting_artifacts,
                    )

                    pipeline_manager = _get_pipeline_manager(request)
                    await reindex_meeting_artifacts(
                        config,
                        pipeline_manager._model_manager,
                        meeting_id,
                        meeting_mutation_coordinator=(_get_meeting_mutation_coordinator(request)),
                    )
                    await asyncio.to_thread(
                        consume_reindex_required_marker,
                        config,
                        meeting_id,
                    )
            except Exception as recovery_error:  # noqa: BLE001 - durable claim을 보존한다.
                recovery_ready = False
                logger.error(
                    "회의 삭제 파일/marker 복구 실패: job_id=%s, error=%s",
                    delete_claim_job_id,
                    recovery_error,
                )
            if recovery_ready:
                try:
                    await asyncio.to_thread(
                        queue.queue.restore_delete_claim,
                        delete_claim_job_id,
                        delete_claim_token,
                    )
                except Exception as restore_error:  # noqa: BLE001 - 원래 오류를 보존한다.
                    logger.error(
                        "회의 삭제 claim 복구 실패: job_id=%s, error=%s",
                        delete_claim_job_id,
                        restore_error,
                    )


@router.get(
    "/meetings/{meeting_id}/transcript",
    response_model=TranscriptResponse,
)
async def get_transcript(
    request: Request,
    meeting_id: str,
) -> TranscriptResponse:
    """특정 회의의 전사문(보정된 발화 목록)을 반환한다.

    다음 순서로 폴백하여 데이터를 찾는다:
      1. outputs/{meeting_id}/corrected.json (LLM 보정 완료)
      2. checkpoints/{meeting_id}/correct.json (보정 체크포인트)
      3. checkpoints/{meeting_id}/merge.json (병합 결과, 미보정)
      4. checkpoints/{meeting_id}/transcribe.json (화자분리 전 전사 초안, 읽기 전용)

    Args:
        request: FastAPI Request 객체
        meeting_id: 회의 고유 식별자

    Returns:
        TranscriptResponse: 전사문 데이터

    Raises:
        HTTPException: 유효하지 않은 ID(400), 파일 미존재(404), 서버 에러(500)
    """
    _validate_meeting_id(meeting_id)
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")

    outputs_dir = _configured_lexical_path(config, "outputs_dir")
    checkpoints_dir = _configured_lexical_path(config, "checkpoints_dir")

    # 폴백 순서: corrected.json → correct.json → merge.json → transcribe.json
    candidates = [
        (
            outputs_dir / meeting_id / "corrected.json",
            "corrected",
            False,
        ),
        (
            checkpoints_dir / meeting_id / "correct.json",
            "correct",
            False,
        ),
        (
            checkpoints_dir / meeting_id / "merge.json",
            "merge",
            True,
        ),
        (
            checkpoints_dir / meeting_id / "transcribe.json",
            "transcribe",
            True,
        ),
    ]

    transcript_path: Path | None = None
    data: dict[str, Any] | None = None
    source_stage = ""
    readonly = True
    for candidate, candidate_stage, candidate_readonly in candidates:
        try:
            raw_candidate = await asyncio.to_thread(read_text_no_follow, candidate)
        except FileNotFoundError:
            continue
        except UnicodeDecodeError as exc:
            logger.exception("전사문 UTF-8 디코딩 실패: %s", candidate)
            raise HTTPException(
                status_code=500,
                detail=f"전사문 조회 중 오류가 발생했습니다: {exc}",
            ) from exc
        except OSError as exc:
            raise _artifact_io_http_exception("전사문 조회", exc) from exc
        transcript_path = candidate
        source_stage = candidate_stage
        readonly = candidate_readonly
        try:
            data = cast(dict[str, Any], json.loads(raw_candidate))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.exception("전사문 JSON 파싱 실패: %s", candidate)
            raise HTTPException(
                status_code=500,
                detail=f"전사문 조회 중 오류가 발생했습니다: {exc}",
            ) from exc
        break

    if transcript_path is None:
        raise HTTPException(
            status_code=404,
            detail=f"전사문을 찾을 수 없습니다: {meeting_id}",
        )

    try:
        assert data is not None

        if source_stage == "transcribe":
            utterances = [
                TranscriptUtteranceItem(
                    text=segment.get("text", ""),
                    original_text=segment.get("text", ""),
                    speaker="UNKNOWN",
                    start=segment.get("start", 0.0),
                    end=segment.get("end", 0.0),
                    was_corrected=False,
                )
                for segment in data.get("segments", [])
            ]
        else:
            # merge.json은 original_text/was_corrected 필드가 없으므로 폴백 처리
            is_merge_fallback = source_stage == "merge"
            utterances = [
                TranscriptUtteranceItem(
                    text=u.get("text", ""),
                    original_text=u.get("original_text", u.get("text", "")),
                    speaker=u.get("speaker", "UNKNOWN"),
                    start=u.get("start", 0.0),
                    end=u.get("end", 0.0),
                    was_corrected=(
                        u.get("was_corrected", False) if not is_merge_fallback else False
                    ),
                )
                for u in data.get("utterances", [])
            ]

        # 화자 목록 추출 (UNKNOWN 제외, 순서 보존)
        seen: set[str] = set()
        speakers: list[str] = []
        for u in utterances:
            if u.speaker != "UNKNOWN" and u.speaker not in seen:
                seen.add(u.speaker)
                speakers.append(u.speaker)

        return TranscriptResponse(
            utterances=utterances,
            meeting_id=meeting_id,
            num_speakers=(
                0 if source_stage == "transcribe" else data.get("num_speakers", len(speakers))
            ),
            speakers=speakers,
            total_utterances=len(utterances),
            source_stage=source_stage,
            readonly=readonly,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"전사문 조회 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"전사문 조회 중 오류가 발생했습니다: {e}",
        ) from e


@router.get(
    "/meetings/{meeting_id}/summary",
    response_model=SummaryResponse,
)
async def get_summary(
    request: Request,
    meeting_id: str,
) -> SummaryResponse:
    """특정 회의의 AI 요약(회의록)을 반환한다.

    outputs/{meeting_id}/summary.json 메타데이터와
    summary.md 마크다운 파일에서 회의록을 읽어 반환한다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 회의 고유 식별자

    Returns:
        SummaryResponse: 회의록 데이터

    Raises:
        HTTPException: 유효하지 않은 ID(400), 파일 미존재(404), 서버 에러(500)
    """
    _validate_meeting_id(meeting_id)
    config = _get_config(request)
    outputs_dir = _configured_lexical_path(config, "outputs_dir")
    meeting_dir = outputs_dir / meeting_id

    # 폴백 순서: summary.md → meeting_minutes.md → summary.json → checkpoints/summarize.json
    summary_md_path = meeting_dir / "summary.md"
    minutes_md_path = meeting_dir / "meeting_minutes.md"
    summary_json_path = meeting_dir / "summary.json"
    # 체크포인트 폴백
    checkpoints_dir = _configured_lexical_path(config, "checkpoints_dir")
    checkpoint_path = checkpoints_dir / meeting_id / "summarize.json"

    try:
        markdown = ""
        meta: dict[str, Any] = {}
        found_artifact = False

        # 마크다운 파일 읽기 (폴백 순서: summary.md → meeting_minutes.md)
        for md_file in (summary_md_path, minutes_md_path):
            try:
                markdown = await asyncio.to_thread(read_text_no_follow, md_file)
            except FileNotFoundError:
                continue
            found_artifact = True
            break

        try:
            raw_meta = await asyncio.to_thread(read_text_no_follow, summary_json_path)
        except FileNotFoundError:
            pass
        else:
            found_artifact = True
            meta = cast(dict[str, Any], json.loads(raw_meta))
            if not markdown and meta.get("markdown"):
                markdown = str(meta["markdown"])

        # 체크포인트 폴백 (outputs에 없을 때)
        if not markdown:
            try:
                raw_checkpoint = await asyncio.to_thread(
                    read_text_no_follow,
                    checkpoint_path,
                )
            except FileNotFoundError:
                pass
            else:
                found_artifact = True
                cp_data = cast(dict[str, Any], json.loads(raw_checkpoint))
                if cp_data.get("markdown"):
                    markdown = str(cp_data["markdown"])
                    meta = cp_data

        if not found_artifact:
            raise HTTPException(
                status_code=404,
                detail=f"회의록을 찾을 수 없습니다: {meeting_id}",
            )

        return SummaryResponse(
            markdown=markdown,
            meeting_id=meeting_id,
            num_speakers=meta.get("num_speakers", 0),
            speakers=meta.get("speakers", []),
            num_utterances=meta.get("num_utterances", 0),
            created_at=meta.get("created_at", ""),
        )
    except HTTPException:
        raise
    except OSError as exc:
        logger.warning("회의록 no-follow 조회 차단: %s", exc)
        raise _artifact_io_http_exception("회의록 조회", exc) from exc
    except Exception as e:
        logger.exception(f"회의록 조회 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"회의록 조회 중 오류가 발생했습니다: {e}",
        ) from e


# ===========================================================================
# 회의록 / 전사문 편집 엔드포인트
# ===========================================================================
# 사용자가 AI 생성 결과물을 수동으로 수정하거나, 자주 틀리는 전사 패턴을
# 한 번에 치환하면서 용어집에도 자동 등록할 수 있도록 지원한다.
#
# 저장 원칙:
#   - 기존 파일(meeting_minutes.md, correct.json)을 직접 덮어쓴다.
#   - 부모 디렉터리와 파일을 no-follow FD로 고정해 원자 교체한다.
#   - 직전 버전은 같은 no-follow 계약으로 {파일}.bak 에 백업한다.
#   - 기존 요약 산출물의 force 재생성은 데이터 보존을 위해 fail-closed한다.
# ===========================================================================


# === 요약 편집 ===


class SummaryUpdateRequest(BaseModel):
    """PUT /api/meetings/{meeting_id}/summary 요청."""

    markdown: str = Field(
        ...,
        min_length=1,
        max_length=200000,
        description="수정된 회의록 마크다운 본문",
    )


@router.put(
    "/meetings/{meeting_id}/summary",
    response_model=SummaryResponse,
    dependencies=[Depends(_meeting_mutation_lease)],
)
async def update_summary(
    request: Request,
    meeting_id: str,
    body: SummaryUpdateRequest,
) -> SummaryResponse:
    """사용자가 편집한 회의록(마크다운) 본문을 저장한다.

    기존 `meeting_minutes.md` (없으면 `summary.md`) 파일을 덮어쓰고,
    직전 버전을 `.bak` 로 백업한다. 이후 `GET /summary` 는 수정본을 반환한다.

    기존 요약 산출물이 있는 `POST /summarize?force=true` 요청은 현재 수정본을
    보존하고 409로 거부한다.

    Raises:
        HTTPException 400: 유효하지 않은 meeting_id
        HTTPException 404: 회의 디렉토리 없음
        HTTPException 500: 파일 쓰기 실패
    """
    _validate_meeting_id(meeting_id)
    config = _get_config(request)
    outputs_dir = _configured_lexical_path(config, "outputs_dir")
    meeting_dir = outputs_dir / meeting_id

    try:
        meeting_stat = meeting_dir.lstat()
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"회의 출력 폴더를 찾을 수 없습니다: {meeting_id}",
        ) from None
    if not stat.S_ISDIR(meeting_stat.st_mode):
        raise HTTPException(
            status_code=409,
            detail="SECURITY_BLOCKED: 회의 출력 경로가 안전한 디렉터리가 아닙니다.",
        )

    await _ensure_completed_meeting_mutation_allowed(
        request,
        meeting_id,
        artifact_label="회의록 편집",
    )

    # 기존 파일 결정: meeting_minutes.md 우선, 없으면 summary.md. 존재 확인도
    # no-follow로 수행해 정적 symlink를 다른 산출물로 오인하지 않는다.
    minutes_md = meeting_dir / "meeting_minutes.md"
    summary_md = meeting_dir / "summary.md"

    try:
        try:
            await asyncio.to_thread(read_text_no_follow, minutes_md)
        except FileNotFoundError:
            try:
                await asyncio.to_thread(read_text_no_follow, summary_md)
            except FileNotFoundError:
                # 둘 다 없으면 meeting_minutes.md 로 새로 생성한다.
                target = minutes_md
            else:
                target = summary_md
        else:
            target = minutes_md

        await asyncio.to_thread(
            _atomic_write_text_pinned,
            target,
            body.markdown,
            backup=True,
        )
        # JSON 캐시 무효화 (다음 GET 에서 수정본 반영되도록)
        _json_cache.invalidate(target)
    except OSError as exc:
        logger.exception(f"회의록 저장 실패: {exc}")
        raise _artifact_io_http_exception("회의록 저장", exc) from exc

    logger.info(
        "회의록 수동 편집 저장: meeting_id=%s, path=%s, length=%d",
        meeting_id,
        target.name,
        len(body.markdown),
    )
    return SummaryResponse(
        markdown=body.markdown,
        meeting_id=meeting_id,
        num_speakers=0,
        speakers=[],
        num_utterances=0,
        created_at="",
    )


# === 전사문 편집 ===


class TranscriptUtterancePatch(BaseModel):
    """전사문 수정 시 단일 발화 스키마.

    기존 구조와 호환: speaker, start, end, text 등 필수 필드.
    """

    text: str = Field(..., max_length=10000)
    original_text: str = ""
    speaker: str = "UNKNOWN"
    start: float = 0.0
    end: float = 0.0
    was_corrected: bool = False


class TranscriptUpdateRequest(BaseModel):
    """PUT /api/meetings/{meeting_id}/transcript 요청."""

    utterances: list[TranscriptUtterancePatch] = Field(..., min_length=1)


class TranscriptReplaceRequest(BaseModel):
    """POST /api/meetings/{meeting_id}/transcript/replace 요청."""

    find: str = Field(..., min_length=1, max_length=500, description="치환 대상 패턴 (정확 매칭)")
    replace: str = Field(..., min_length=1, max_length=500, description="치환 후 문자열")
    add_to_vocabulary: bool = Field(
        default=False,
        description="True면 자동으로 용어집에 등록 (replace=term, find=alias)",
    )


class TranscriptReplaceResponse(BaseModel):
    """POST /api/meetings/{meeting_id}/transcript/replace 응답."""

    changes: int = 0
    updated_utterances: int = 0
    vocabulary_action: str | None = None
    vocabulary_term_id: str | None = None


def _find_transcript_file(config: Any, meeting_id: str) -> tuple[Path | None, str]:
    """전사 편집 대상 파일을 찾는다.

    편집 시에는 readonly 폴백(merge.json)을 사용하지 않고,
    correct.json(우선) 또는 corrected.json 만 대상으로 한다.

    Returns:
        (파일 경로, 'output'|'checkpoint') 튜플, 없으면 (None, "")
    """
    outputs_dir = _configured_lexical_path(config, "outputs_dir")
    checkpoints_dir = _configured_lexical_path(config, "checkpoints_dir")

    # 1순위: outputs/{id}/corrected.json
    corrected = outputs_dir / meeting_id / "corrected.json"
    try:
        read_text_no_follow(corrected)
    except FileNotFoundError:
        pass
    except UnicodeDecodeError:
        # 안전한 regular file entry는 존재한다. 실제 편집 로드에서 손상 응답을 낸다.
        return corrected, "output"
    else:
        return corrected, "output"

    # 2순위: checkpoints/{id}/correct.json
    checkpoint = checkpoints_dir / meeting_id / "correct.json"
    try:
        read_text_no_follow(checkpoint)
    except FileNotFoundError:
        pass
    except UnicodeDecodeError:
        return checkpoint, "checkpoint"
    else:
        return checkpoint, "checkpoint"

    return None, ""


async def _ensure_completed_meeting_mutation_allowed(
    request: Request,
    meeting_id: str,
    *,
    artifact_label: str,
) -> None:
    """완료된 DB 회의에 대해서만 후속 산출물 mutation을 허용한다."""
    from core.job_queue import JobStatus

    queue = _get_job_queue(request)
    raw_queue = getattr(queue, "queue", queue)
    job = await asyncio.to_thread(raw_queue.get_job_by_meeting_id, meeting_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"회의를 찾을 수 없습니다: {meeting_id}",
        )
    if job.status != JobStatus.COMPLETED.value:
        raise HTTPException(
            status_code=409,
            detail=(
                f"파이프라인 처리 중에는 {artifact_label} 작업을 시작할 수 없습니다. "
                "완료 후 다시 시도해 주세요."
            ),
        )


@router.put(
    "/meetings/{meeting_id}/transcript",
    response_model=TranscriptResponse,
    dependencies=[Depends(_meeting_mutation_lease)],
)
async def update_transcript(
    request: Request,
    meeting_id: str,
    body: TranscriptUpdateRequest,
) -> TranscriptResponse:
    """사용자가 편집한 전사문 전체(발화 목록)를 저장한다.

    Raises:
        HTTPException 400: 유효하지 않은 meeting_id
        HTTPException 404: 편집 가능한 전사 파일 없음
        HTTPException 500: 파일 쓰기 실패
    """
    _validate_meeting_id(meeting_id)
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")

    await _ensure_completed_meeting_mutation_allowed(
        request,
        meeting_id,
        artifact_label="전사문 편집",
    )

    try:
        target, _ = await asyncio.to_thread(_find_transcript_file, config, meeting_id)
    except OSError as exc:
        logger.warning("전사문 편집 대상 no-follow 확인 차단: %s", exc)
        raise _artifact_io_http_exception("전사문 편집 대상 확인", exc) from exc
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"편집 가능한 전사 파일이 없습니다: {meeting_id} (먼저 파이프라인을 실행하세요)",
        )

    try:
        # 기존 데이터 로드 (num_speakers 등 메타 필드 보존)
        def _load() -> dict[str, Any]:
            return cast(dict[str, Any], json.loads(read_text_no_follow(target)))

        existing = await asyncio.to_thread(_load)

        # 발화 목록 교체
        new_utterances = [u.model_dump() for u in body.utterances]
        existing["utterances"] = new_utterances

        # 화자 수 재계산
        speakers = sorted({u["speaker"] for u in new_utterances if u["speaker"] != "UNKNOWN"})
        existing["num_speakers"] = len(speakers)

        await asyncio.to_thread(
            _atomic_write_json_pinned,
            target,
            existing,
            backup=True,
        )
        _json_cache.invalidate(target)
    except OSError as exc:
        logger.exception(f"전사문 저장 실패: {exc}")
        raise _artifact_io_http_exception("전사문 저장", exc) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.exception("전사문 편집 원본 파싱 실패: %s", target)
        raise HTTPException(
            status_code=500,
            detail=f"전사문 저장 중 오류가 발생했습니다: {exc}",
        ) from exc

    logger.info(
        "전사문 수동 편집 저장: meeting_id=%s, utterances=%d",
        meeting_id,
        len(new_utterances),
    )

    return TranscriptResponse(
        utterances=[
            TranscriptUtteranceItem(
                text=u["text"],
                original_text=u.get("original_text", u["text"]),
                speaker=u["speaker"],
                start=u["start"],
                end=u["end"],
                was_corrected=u.get("was_corrected", False),
            )
            for u in new_utterances
        ],
        meeting_id=meeting_id,
        num_speakers=existing.get("num_speakers", 0),
        speakers=speakers,
        total_utterances=len(new_utterances),
        source_stage="corrected" if target.name == "corrected.json" else "correct",
        readonly=False,
    )


@router.post(
    "/meetings/{meeting_id}/transcript/replace",
    response_model=TranscriptReplaceResponse,
    dependencies=[Depends(_meeting_mutation_lease)],
)
async def replace_transcript_pattern(
    request: Request,
    meeting_id: str,
    body: TranscriptReplaceRequest,
) -> TranscriptReplaceResponse:
    """전사문에서 특정 패턴을 모두 찾아 치환한다.

    자주 틀리는 오인식(예: '파이선' → 'FastAPI')을 한 번에 수정하고,
    옵션으로 용어집에 자동 등록하여 앞으로의 보정에 반영되게 한다.

    동작:
        1. 편집 대상 전사 파일(correct.json 또는 corrected.json) 로드
        2. 각 발화의 text 에서 `find` 를 `replace` 로 문자열 치환 (대소문자 구분)
        3. 변경된 발화의 `was_corrected=True` 로 마크
        4. `add_to_vocabulary=True` 면 `core.user_settings.add_vocabulary_term` 또는
           기존 동일 term 의 aliases 에 find 추가
        5. 원자적 파일 저장 + 결과 요약 반환

    Raises:
        HTTPException 400: 유효하지 않은 meeting_id 또는 빈 find/replace
        HTTPException 404: 편집 가능한 전사 파일 없음
        HTTPException 500: 파일 쓰기 실패
    """
    _validate_meeting_id(meeting_id)
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")

    if body.find == body.replace:
        raise HTTPException(
            status_code=400,
            detail="find와 replace가 같습니다. 다른 값을 입력해 주세요.",
        )

    await _ensure_completed_meeting_mutation_allowed(
        request,
        meeting_id,
        artifact_label="전사문 편집",
    )

    try:
        target, _ = await asyncio.to_thread(_find_transcript_file, config, meeting_id)
    except OSError as exc:
        logger.warning("전사문 치환 대상 no-follow 확인 차단: %s", exc)
        raise _artifact_io_http_exception("전사문 치환 대상 확인", exc) from exc
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"편집 가능한 전사 파일이 없습니다: {meeting_id}",
        )

    try:

        def _load() -> dict[str, Any]:
            return cast(dict[str, Any], json.loads(read_text_no_follow(target)))

        existing = await asyncio.to_thread(_load)
        utterances = existing.get("utterances", [])

        total_changes = 0
        updated_count = 0
        for u in utterances:
            text = u.get("text", "")
            if body.find in text:
                new_text = text.replace(body.find, body.replace)
                change_count = text.count(body.find)
                total_changes += change_count
                updated_count += 1
                u["text"] = new_text
                u["was_corrected"] = True

        if total_changes == 0:
            return TranscriptReplaceResponse(
                changes=0,
                updated_utterances=0,
                vocabulary_action=None,
                vocabulary_term_id=None,
            )

        existing["utterances"] = utterances
        await asyncio.to_thread(
            _atomic_write_json_pinned,
            target,
            existing,
            backup=True,
        )
        _json_cache.invalidate(target)

        # 용어집 자동 등록
        vocab_action: str | None = None
        vocab_term_id: str | None = None
        if body.add_to_vocabulary:
            try:
                from core import user_settings as _us

                vocab = _us.load_vocabulary(force_reload=True)
                # 기존에 같은 term 이 있으면 alias 에 find 추가
                existing_term = None
                for t in vocab.terms:
                    if t.term.strip().lower() == body.replace.strip().lower():
                        existing_term = t
                        break

                if existing_term is not None:
                    if body.find not in existing_term.aliases:
                        new_aliases = list(existing_term.aliases) + [body.find]
                        _us.update_vocabulary_term(term_id=existing_term.id, aliases=new_aliases)
                        vocab_action = "alias_added"
                    else:
                        vocab_action = "alias_already_exists"
                    vocab_term_id = existing_term.id
                else:
                    new_term = _us.add_vocabulary_term(
                        term=body.replace,
                        aliases=[body.find],
                        note=f"'{meeting_id}' 전사 편집에서 자동 등록",
                    )
                    vocab_action = "term_created"
                    vocab_term_id = new_term.id
                logger.info(
                    "용어집 자동 등록: action=%s, term=%s, alias=%s",
                    vocab_action,
                    body.replace,
                    body.find,
                )
            except Exception as exc:
                # 용어집 등록 실패는 전사 수정 자체를 실패시키지 않는다
                logger.warning(f"용어집 자동 등록 실패 (전사 수정은 유지): {exc}")
                vocab_action = "failed"

    except OSError as exc:
        logger.exception(f"전사문 치환 실패: {exc}")
        raise _artifact_io_http_exception("전사문 치환", exc) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.exception("전사문 치환 원본 파싱 실패: %s", target)
        raise HTTPException(
            status_code=500,
            detail=f"전사문 치환 중 오류가 발생했습니다: {exc}",
        ) from exc

    logger.info(
        "전사문 패턴 치환: meeting_id=%s, find=%r, replace=%r, changes=%d",
        meeting_id,
        body.find,
        body.replace,
        total_changes,
    )

    return TranscriptReplaceResponse(
        changes=total_changes,
        updated_utterances=updated_count,
        vocabulary_action=vocab_action,
        vocabulary_term_id=vocab_term_id,
    )


@router.post(
    "/meetings/{meeting_id}/summarize",
    dependencies=[Depends(_meeting_mutation_lease)],
)
async def summarize_meeting(
    request: Request,
    meeting_id: str,
    force: bool = False,
) -> dict[str, str]:
    """온디맨드로 회의 요약(LLM 후처리)을 실행한다.

    skip_llm_steps=True로 파이프라인을 실행한 뒤,
    나중에 LLM 단계(correct + summarize)만 별도 실행할 때 사용한다.
    백그라운드 태스크로 비동기 실행된다.

    Args:
        request: FastAPI Request 객체
        meeting_id: 회의 고유 식별자
        force: 현재는 데이터 보존을 위해 항상 409로 거부한다.

    Returns:
        요약 시작 확인 메시지

    Raises:
        HTTPException: 유효하지 않은 ID(400), 상태 파일 미존재(404),
                       체크포인트 미존재(400), 비파괴 실행 충돌(409),
                       파이프라인 미초기화(503)
    """
    import asyncio

    from core.pipeline import PipelineError, PipelineStep

    _validate_meeting_id(meeting_id)
    if force:
        raise HTTPException(
            status_code=409,
            detail=(
                "SECURITY_BLOCKED: 기존 요약을 지우는 force 재생성은 비활성화되어 있습니다. "
                "현재 산출물은 그대로 보존됩니다."
            ),
        )
    pipeline = _get_pipeline_manager(request)
    await _ensure_completed_meeting_mutation_allowed(
        request,
        meeting_id,
        artifact_label="요약 생성",
    )

    # 상태 파일 / 체크포인트 존재 여부를 사전 검증
    try:
        merge_cp = pipeline._get_checkpoint_path(meeting_id, PipelineStep.MERGE)
        if not merge_cp.exists():
            # 이슈 I: merge 체크포인트가 없다면 state 파일 유무와 상관없이 404
            state_path = pipeline._get_state_path(meeting_id)
            if not state_path.exists():
                raise HTTPException(
                    status_code=404,
                    detail=f"회의를 찾을 수 없습니다: {meeting_id}",
                )
            raise HTTPException(
                status_code=400,
                detail=f"merge 체크포인트가 없습니다. 파이프라인을 먼저 실행하세요: {meeting_id}",
            )

        # 이슈 I: merge 체크포인트는 있는데 state 파일만 유실된 경우도
        # 404로 차단하지 않는다. 다만 요청 핸들러에서 state를 먼저 쓰지 않고,
        # 백그라운드 실행이 비파괴 충돌 검사를 끝낸 뒤 재구성한다.
        state_path = pipeline._get_state_path(meeting_id)
        if not state_path.exists():
            try:
                pipeline.validate_llm_steps_non_destructive(meeting_id)
            except PipelineError as e:
                raise HTTPException(status_code=409, detail=str(e)) from e
            logger.warning(f"state 파일 유실, 백그라운드 재구성 예정: {meeting_id}")

        # 백그라운드 task를 만들기 전에 레거시 pass-through/stale 산출물
        # 충돌을 동기적으로 알려준다. 기존 파일은 어떤 것도 변경하지 않는다.
        try:
            pipeline.validate_llm_steps_non_destructive(meeting_id)
        except PipelineError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"요약 사전 검증 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"요약 사전 검증 중 오류가 발생했습니다: {e}",
        ) from e

    # 백그라운드 태스크로 LLM 단계 실행
    admission_started = asyncio.Event()
    task = asyncio.create_task(
        _run_llm_steps_with_meeting_lease(
            request,
            pipeline,
            meeting_id,
            admission_started,
        ),
        name=f"llm-steps-{meeting_id}",
    )
    task.add_done_callback(_log_task_exception)
    running_tasks = getattr(request.app.state, "running_tasks", None)
    if running_tasks is not None:
        running_tasks.add(task)
        task.add_done_callback(running_tasks.discard)

    # background task가 shared meeting lease를 획득했거나 FIFO 대기열에 들어간
    # 뒤에만 성공 응답한다. 응답과 task 시작 사이의 재전사 추월 창을 닫는다.
    await admission_started.wait()

    logger.info(f"온디맨드 요약 시작: {meeting_id} (force={force})")

    return {
        "status": "ok",
        "message": "요약 생성을 시작합니다.",
        "meeting_id": meeting_id,
    }
