"""명시적으로 선택된 경우에만 동작하는 OpenAI 회의 전사 어댑터.

로컬에서 음성을 작은 generic MP3 청크로 만든 뒤 고정된 OpenAI HTTPS
엔드포인트로 순차 전송한다. API 키, 회의 ID, 원본 파일명, 로컬 prompt는
요청·로그·체크포인트에 기록하지 않는다.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import logging
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from config import AppConfig
from core.audio_quality import (
    AudioFailureKind,
    AudioQualityStatus,
    measure_audio_duration,
    validate_audio_quality,
)
from core.quarantine import (
    QuarantineError,
    _directory_open_flags,
    _open_directory_tree_no_follow,
)
from core.retry_policy import NonRetryableError
from core.transcription_models import OPENAI_TRANSCRIBE_DIARIZE_MODEL, is_loopback_host
from security.openai_keychain import get_api_key, validated_api_key
from steps.transcriber import (
    AudioAdmissionError,
    AudioFileIdentity,
    EmptyAudioError,
    TranscriptionError,
    TranscriptResult,
    TranscriptSegment,
    inspect_audio_path_no_symlinks,
    open_audio_path_no_symlinks,
)

logger = logging.getLogger(__name__)

_OPENAI_HOST = "api.openai.com"
_OPENAI_PATH = "/v1/audio/transcriptions"
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_CACHE_BYTES = 20 * 1024 * 1024
_ENCODING_SIZE_SAFETY_FACTOR = 0.95
_CACHE_SCHEMA_VERSION = 1
_TEMP_DIR_PATTERN = re.compile(r"^\.openai-stt-(?P<pid>[1-9][0-9]*)-[A-Za-z0-9_-]+$")
_TEMP_AUDIO_PATTERN = re.compile(r"^audio_[0-9]{4}\.mp3$")

OpenAITransport = Callable[[bytes, str, str, int], dict[str, Any]]
CancellationCheck = Callable[[], bool]


class OpenAITranscriptionError(TranscriptionError, NonRetryableError):
    """자동 재호출이 중복 과금으로 이어질 수 있는 외부 전사 오류."""


def _pid_is_alive(pid: int) -> bool:
    """PID가 현재 실행 중인지 권한을 변경하지 않고 확인한다."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cleanup_stale_openai_temp_dirs(config: AppConfig) -> int:
    """비정상 종료로 남은 외부 전송용 MP3 임시 디렉터리를 안전하게 제거한다.

    PID가 살아 있는 디렉터리는 다른 앱 인스턴스가 사용할 수 있어 건드리지
    않는다. 0700 디렉터리와 예상한 ``audio_XXXX.mp3`` 일반 파일만 삭제하며,
    symlink나 알 수 없는 entry가 하나라도 있으면 해당 디렉터리를 보존한다.
    """
    base_dir = config.paths.resolved_base_dir
    try:
        base_fd = _open_directory_tree_no_follow(base_dir, create=False)
    except FileNotFoundError:
        return 0
    except (OSError, QuarantineError) as exc:
        raise OpenAITranscriptionError(
            "OpenAI 임시 오디오 정리 경로가 안전하지 않습니다."
        ) from exc

    removed = 0
    try:
        for name in os.listdir(base_fd):
            match = _TEMP_DIR_PATTERN.fullmatch(name)
            if match is None:
                continue
            owner_pid = int(match.group("pid"))
            if _pid_is_alive(owner_pid):
                continue
            child_fd: int | None = None
            try:
                entry = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(entry.st_mode)
                    or entry.st_uid != os.getuid()
                    or stat.S_IMODE(entry.st_mode) != 0o700
                ):
                    continue
                child_fd = os.open(name, _directory_open_flags(), dir_fd=base_fd)
                opened_dir = os.fstat(child_fd)
                if (opened_dir.st_dev, opened_dir.st_ino) != (entry.st_dev, entry.st_ino):
                    continue

                files = os.listdir(child_fd)
                validated: list[tuple[str, os.stat_result]] = []
                unsafe = False
                for filename in files:
                    if _TEMP_AUDIO_PATTERN.fullmatch(filename) is None:
                        unsafe = True
                        break
                    file_entry = os.stat(
                        filename,
                        dir_fd=child_fd,
                        follow_symlinks=False,
                    )
                    if not stat.S_ISREG(file_entry.st_mode) or file_entry.st_uid != os.getuid():
                        unsafe = True
                        break
                    file_fd = os.open(
                        filename,
                        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=child_fd,
                    )
                    try:
                        opened_file = os.fstat(file_fd)
                    finally:
                        os.close(file_fd)
                    if (opened_file.st_dev, opened_file.st_ino) != (
                        file_entry.st_dev,
                        file_entry.st_ino,
                    ):
                        unsafe = True
                        break
                    validated.append((filename, file_entry))
                if unsafe:
                    logger.warning("안전하지 않은 OpenAI 임시 오디오 디렉터리 보존: %s", name)
                    continue

                for filename, expected in validated:
                    current = os.stat(
                        filename,
                        dir_fd=child_fd,
                        follow_symlinks=False,
                    )
                    if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                        raise OpenAITranscriptionError(
                            "OpenAI 임시 오디오가 정리 중 변경되었습니다."
                        )
                    os.unlink(filename, dir_fd=child_fd)
                os.fsync(child_fd)
                current_dir = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
                if (current_dir.st_dev, current_dir.st_ino) != (
                    opened_dir.st_dev,
                    opened_dir.st_ino,
                ):
                    raise OpenAITranscriptionError(
                        "OpenAI 임시 오디오 디렉터리가 정리 중 변경되었습니다."
                    )
                os.rmdir(name, dir_fd=base_fd)
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise OpenAITranscriptionError(
                    "OpenAI 임시 오디오를 안전하게 정리하지 못했습니다."
                ) from exc
            finally:
                if child_fd is not None:
                    os.close(child_fd)
        if removed:
            os.fsync(base_fd)
    finally:
        os.close(base_fd)
    return removed


def cleanup_openai_resume_cache(config: AppConfig, resume_dir: Path) -> None:
    """키 조회나 네트워크 없이 지정한 OpenAI 원문 응답 캐시를 안전하게 비운다."""
    OpenAITranscriber(config, credential_resolver=lambda: None).cleanup_resume_cache(resume_dir)


def cleanup_meeting_openai_resume_caches(config: AppConfig, meeting_id: str) -> None:
    """삭제 commit 전에 한 회의와 연결된 모든 OpenAI raw 응답 캐시를 비운다."""
    if (
        not meeting_id
        or meeting_id in {".", ".."}
        or "/" in meeting_id
        or "\\" in meeting_id
        or "\x00" in meeting_id
    ):
        raise OpenAITranscriptionError("유효하지 않은 회의 ID입니다.")
    from core import ab_test_store

    checkpoint_meeting_dir = config.paths.resolved_checkpoints_dir / meeting_id
    cache_dirs = [
        checkpoint_meeting_dir / ".openai-transcribe-parts",
        checkpoint_meeting_dir / ".openai-ab-transcribe-parts",
    ]
    cache_dirs.extend(
        ab_test_store.resolve_test_dir(config, test_id) / ".openai-transcribe-parts"
        for test_id in ab_test_store.list_test_ids(config, meeting_id)
    )
    for cache_dir in cache_dirs:
        cleanup_openai_resume_cache(config, cache_dir)


def _multipart_body(
    audio_bytes: bytes,
    *,
    model: str,
) -> tuple[bytes, str]:
    """고정 필드와 generic filename으로 multipart 요청 본문을 만든다."""
    boundary = f"recap-{uuid.uuid4().hex}"
    crlf = b"\r\n"
    chunks: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        chunks.extend(
            [
                f"--{boundary}".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"'.encode("ascii"),
                b"",
                value.encode("utf-8"),
            ]
        )

    add_field("model", model)
    add_field("response_format", "diarized_json")
    add_field("chunking_strategy", "auto")
    chunks.extend(
        [
            f"--{boundary}".encode("ascii"),
            b'Content-Disposition: form-data; name="file"; filename="audio.mp3"',
            b"Content-Type: audio/mpeg",
            b"",
            audio_bytes,
            f"--{boundary}--".encode("ascii"),
            b"",
        ]
    )
    return crlf.join(chunks), boundary


def _default_transport(
    audio_bytes: bytes,
    api_key: str,
    model: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    """시스템 TLS 검증을 사용하는 고정 OpenAI HTTPS 요청을 수행한다."""
    if validated_api_key(api_key) is None:
        raise OpenAITranscriptionError("OpenAI API 키 형식이 올바르지 않습니다.")
    body, boundary = _multipart_body(audio_bytes, model=model)
    connection = http.client.HTTPSConnection(_OPENAI_HOST, 443, timeout=timeout_seconds)
    try:
        connection.request(
            "POST",
            _OPENAI_PATH,
            body=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        payload = response.read(_MAX_RESPONSE_BYTES + 1)
    except (OSError, TimeoutError, ValueError, http.client.HTTPException):
        raise OpenAITranscriptionError(
            "OpenAI 전사 요청을 완료하지 못했습니다. 자동 재시도하지 않습니다."
        ) from None
    finally:
        connection.close()

    if len(payload) > _MAX_RESPONSE_BYTES:
        raise OpenAITranscriptionError("OpenAI 전사 응답이 허용 크기를 초과했습니다.")
    if response.status != 200:
        if response.status in {401, 403}:
            message = "OpenAI API 키를 확인해 주세요."
        elif response.status == 413:
            message = "OpenAI 업로드 허용 크기를 초과했습니다."
        elif response.status == 429:
            message = "OpenAI 요청 한도에 도달했습니다. 잠시 후 사용자가 다시 시도해 주세요."
        else:
            message = f"OpenAI 전사 요청이 실패했습니다 (HTTP {response.status})."
        raise OpenAITranscriptionError(message)
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpenAITranscriptionError("OpenAI 전사 응답 형식이 올바르지 않습니다.") from exc
    if not isinstance(decoded, dict):
        raise OpenAITranscriptionError("OpenAI 전사 응답 형식이 올바르지 않습니다.")
    return decoded


class OpenAITranscriber:
    """OpenAI diarized transcription을 기존 TranscriptResult로 변환한다."""

    def __init__(
        self,
        config: AppConfig,
        *,
        transport: OpenAITransport | None = None,
        credential_resolver: Callable[[], str | None] = get_api_key,
    ) -> None:
        self._config = config
        self._transport = transport or _default_transport
        self._credential_resolver = credential_resolver
        self._model = config.stt.openai_model

    def _validated_resume_dir(self, resume_dir: Path) -> Path:
        """재개 캐시가 앱 base_dir 안의 lexical 경로인지 확인한다."""
        base = Path(os.path.abspath(os.fspath(Path(self._config.paths.base_dir).expanduser())))
        candidate = Path(os.path.abspath(os.fspath(resume_dir.expanduser())))
        if candidate == base or not candidate.is_relative_to(base):
            raise OpenAITranscriptionError("OpenAI 전사 재개 캐시 경로가 앱 저장소 밖에 있습니다.")
        return candidate

    def _open_resume_dir(self, resume_dir: Path, *, create: bool) -> int:
        """재개 캐시 디렉터리를 모든 component no-follow 조건으로 연다."""
        safe_dir = self._validated_resume_dir(resume_dir)
        try:
            return _open_directory_tree_no_follow(safe_dir, create=create)
        except FileNotFoundError:
            raise
        except (OSError, QuarantineError) as exc:
            raise OpenAITranscriptionError(
                "OpenAI 전사 재개 캐시 경로가 안전하지 않습니다."
            ) from exc

    def _read_chunk_for_upload(
        self,
        chunk_path: Path,
        expected_identity: AudioFileIdentity,
    ) -> tuple[bytes, str]:
        """준비 단계에서 고정한 inode만 immutable upload bytes로 읽는다."""
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            chunk_fd = os.open(chunk_path, flags)
        except OSError as exc:
            raise OpenAITranscriptionError(
                "외부 전송용 오디오 청크를 안전하게 읽을 수 없습니다."
            ) from exc
        try:
            opened = os.fstat(chunk_fd)
            if not stat.S_ISREG(opened.st_mode):
                raise OpenAITranscriptionError("외부 전송용 오디오 청크가 일반 파일이 아닙니다.")
            opened_identity: AudioFileIdentity = (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
            if opened_identity != expected_identity:
                raise OpenAITranscriptionError("외부 전송용 오디오 청크가 준비 후 변경되었습니다.")
            if opened.st_size <= 0 or opened.st_size > self._config.stt.openai_max_upload_bytes:
                raise OpenAITranscriptionError(
                    "외부 전송용 오디오 청크 크기가 허용 범위를 벗어났습니다."
                )
            with os.fdopen(chunk_fd, "rb") as stream:
                chunk_fd = -1
                raw = stream.read(self._config.stt.openai_max_upload_bytes + 1)
                after = os.fstat(stream.fileno())
            if (
                len(raw) != opened.st_size
                or len(raw) > self._config.stt.openai_max_upload_bytes
                or (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
                != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
            ):
                raise OpenAITranscriptionError("외부 전송용 오디오 청크가 읽는 중 변경되었습니다.")
            return raw, hashlib.sha256(raw).hexdigest()
        finally:
            if chunk_fd >= 0:
                os.close(chunk_fd)

    @staticmethod
    def _cache_filename(chunk_index: int, audio_sha256: str) -> str:
        """청크 순서와 실제 업로드 바이트를 결합한 캐시 파일명을 만든다."""
        return f"chunk_{chunk_index:04d}_{audio_sha256}.json"

    def _discard_corrupt_cache_entry(
        self,
        directory_fd: int,
        filename: str,
        opened: os.stat_result,
    ) -> None:
        """열어 확인한 동일 inode일 때만 손상 캐시 entry를 제거한다."""
        try:
            current = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if current.st_dev == opened.st_dev and current.st_ino == opened.st_ino:
            os.unlink(filename, dir_fd=directory_fd)

    def _read_cached_response(
        self,
        resume_dir: Path,
        *,
        chunk_index: int,
        audio_sha256: str,
    ) -> dict[str, Any] | None:
        """검증된 청크 응답 캐시를 읽고 없으면 None을 반환한다."""
        directory_fd = self._open_resume_dir(resume_dir, create=True)
        filename = self._cache_filename(chunk_index, audio_sha256)
        file_fd: int | None = None
        opened: os.stat_result | None = None
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            try:
                file_fd = os.open(filename, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise OpenAITranscriptionError(
                    "OpenAI 전사 재개 캐시 파일이 안전하지 않습니다."
                ) from exc
            opened = os.fstat(file_fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > _MAX_CACHE_BYTES:
                raise ValueError("cache file contract")
            with os.fdopen(file_fd, "rb") as stream:
                file_fd = None
                raw = stream.read(_MAX_CACHE_BYTES + 1)
            if len(raw) > _MAX_CACHE_BYTES:
                raise ValueError("cache size")
            decoded = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(decoded, dict)
                or decoded.get("schema_version") != _CACHE_SCHEMA_VERSION
                or decoded.get("model") != self._model
                or decoded.get("chunk_index") != chunk_index
                or decoded.get("audio_sha256") != audio_sha256
                or not isinstance(decoded.get("response"), dict)
            ):
                raise ValueError("cache payload contract")
            return cast(dict[str, Any], decoded["response"])
        except (UnicodeError, json.JSONDecodeError, ValueError):
            if opened is not None:
                self._discard_corrupt_cache_entry(directory_fd, filename, opened)
            raise OpenAITranscriptionError(
                "손상된 OpenAI 전사 재개 캐시를 정리했습니다. 다시 시도해 주세요."
            ) from None
        finally:
            if file_fd is not None:
                os.close(file_fd)
            os.close(directory_fd)

    def _write_cached_response(
        self,
        resume_dir: Path,
        *,
        chunk_index: int,
        audio_sha256: str,
        response: dict[str, Any],
    ) -> None:
        """성공한 외부 응답을 0600 unique-temp + atomic replace로 저장한다."""
        wrapper = {
            "schema_version": _CACHE_SCHEMA_VERSION,
            "model": self._model,
            "chunk_index": chunk_index,
            "audio_sha256": audio_sha256,
            "response": response,
        }
        encoded = json.dumps(
            wrapper,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAX_CACHE_BYTES:
            raise OpenAITranscriptionError(
                "OpenAI 전사 재개 캐시 응답이 허용 크기를 초과했습니다."
            )

        directory_fd = self._open_resume_dir(resume_dir, create=True)
        filename = self._cache_filename(chunk_index, audio_sha256)
        temporary_name = f".{filename}.{uuid.uuid4().hex}.tmp"
        temporary_fd: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            temporary_fd = os.open(
                temporary_name,
                flags,
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(temporary_fd, "wb") as stream:
                temporary_fd = None
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary_name,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        except OSError:
            raise OpenAITranscriptionError(
                "성공한 OpenAI 전사 청크를 로컬 재개 캐시에 저장하지 못했습니다."
            ) from None
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
            os.close(directory_fd)

    def cleanup_resume_cache(self, resume_dir: Path) -> None:
        """최종 전사 체크포인트 저장 뒤 청크 응답 캐시 내용을 제거한다."""
        try:
            directory_fd = self._open_resume_dir(resume_dir, create=False)
        except FileNotFoundError:
            return
        try:
            for filename in os.listdir(directory_fd):
                entry = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(entry.st_mode):
                    raise OpenAITranscriptionError(
                        "OpenAI 전사 재개 캐시에 예상하지 못한 디렉터리가 있습니다."
                    )
                os.unlink(filename, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _raise_if_cancelled(should_cancel: CancellationCheck | None) -> None:
        """다음 외부 청크 전송 전에 사용자 취소 요청을 반영한다."""
        if should_cancel is not None and should_cancel():
            raise asyncio.CancelledError("사용자가 OpenAI 전사를 취소했습니다.")

    def _assert_identity(
        self,
        audio_path: Path,
        expected_identity: AudioFileIdentity,
    ) -> None:
        """외부 전송 전후 입력 파일 identity가 동일한지 확인한다."""
        try:
            current = inspect_audio_path_no_symlinks(audio_path)
        except (FileNotFoundError, EmptyAudioError) as exc:
            raise AudioAdmissionError(
                "외부 전송 준비 중 오디오 파일이 사라지거나 변경되었습니다.",
                failure_kind=AudioFailureKind.SOURCE_BUSY,
            ) from exc
        if current != expected_identity:
            raise AudioAdmissionError(
                "외부 전송 준비 중 오디오 파일이 변경되었습니다.",
                failure_kind=AudioFailureKind.SOURCE_BUSY,
            )

    async def _require_audio_quality(
        self,
        audio_path: Path,
        expected_identity: AudioFileIdentity,
    ) -> None:
        """공통 오디오 품질 gate를 통과한 입력만 외부 전송한다."""
        quality_config = self._config.audio_quality
        if not quality_config.enabled:
            self._assert_identity(audio_path, expected_identity)
            return
        try:
            result = await asyncio.to_thread(
                validate_audio_quality,
                audio_path,
                min_mean_db=quality_config.min_mean_volume_db,
                min_duration_s=quality_config.min_duration_seconds,
                expected_identity=expected_identity,
                decode_timeout_base_seconds=quality_config.decode_timeout_base_seconds,
                decode_timeout_factor=quality_config.decode_timeout_factor,
                decode_timeout_cap_seconds=quality_config.decode_timeout_cap_seconds,
            )
        except Exception as exc:
            self._assert_identity(audio_path, expected_identity)
            raise AudioAdmissionError(
                "외부 전송 전 오디오 품질을 검증할 수 없습니다.",
                failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
            ) from exc
        self._assert_identity(audio_path, expected_identity)
        if result.status is not AudioQualityStatus.ACCEPT:
            raise AudioAdmissionError(
                f"오디오 품질 검증 거부: {result.reason}",
                failure_kind=result.failure_kind or AudioFailureKind.INFRA_UNAVAILABLE,
            )

    @contextmanager
    def _prepared_chunks(
        self,
        audio_path: Path,
        expected_identity: AudioFileIdentity,
    ) -> Iterator[list[tuple[Path, float, AudioFileIdentity]]]:
        """고정된 입력 fd에서 generic MP3 청크를 만들고 종료 시 제거한다."""
        if shutil.which("ffmpeg") is None:
            raise OpenAITranscriptionError(
                "ffmpeg를 찾을 수 없어 외부 전송용 음성을 만들 수 없습니다."
            )
        base_dir = self._config.paths.resolved_base_dir
        base_dir.mkdir(parents=True, exist_ok=True)
        temp_dir_value = tempfile.mkdtemp(
            prefix=f".openai-stt-{os.getpid()}-",
            dir=base_dir,
        )
        temp_dir = Path(temp_dir_value)
        temp_dir.chmod(0o700)
        try:
            output_pattern = temp_dir / "audio_%04d.mp3"
            size_limited_seconds = max(
                60,
                int(
                    self._config.stt.openai_max_upload_bytes
                    * 8
                    * _ENCODING_SIZE_SAFETY_FACTOR
                    / (self._config.stt.openai_audio_bitrate_kbps * 1000)
                ),
            )
            segment_seconds = min(
                self._config.stt.openai_chunk_seconds,
                size_limited_seconds,
            )
            with open_audio_path_no_symlinks(audio_path) as (audio_fd, held_identity):
                if held_identity != expected_identity:
                    raise AudioAdmissionError(
                        "외부 전송 직전 오디오 파일이 변경되었습니다.",
                        failure_kind=AudioFailureKind.SOURCE_BUSY,
                    )
                command = [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-n",
                    "-i",
                    f"/dev/fd/{audio_fd}",
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-sn",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    f"{self._config.stt.openai_audio_bitrate_kbps}k",
                    "-map_metadata",
                    "-1",
                    "-map_chapters",
                    "-1",
                    "-f",
                    "segment",
                    "-segment_time",
                    str(segment_seconds),
                    "-reset_timestamps",
                    "1",
                    str(output_pattern),
                ]
                try:
                    subprocess.run(
                        command,
                        pass_fds=(audio_fd,),
                        check=True,
                        capture_output=True,
                        timeout=self._config.stt.openai_timeout_seconds,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    raise OpenAITranscriptionError(
                        "외부 전송용 오디오 청크 생성에 실패했습니다."
                    ) from exc
                after_stat = os.fstat(audio_fd)
                after_identity: AudioFileIdentity = (
                    after_stat.st_dev,
                    after_stat.st_ino,
                    after_stat.st_size,
                    after_stat.st_mtime_ns,
                    after_stat.st_ctime_ns,
                )
                if after_identity != expected_identity:
                    raise AudioAdmissionError(
                        "외부 전송용 변환 중 오디오 파일이 변경되었습니다.",
                        failure_kind=AudioFailureKind.SOURCE_BUSY,
                    )

            self._assert_identity(audio_path, expected_identity)
            paths = sorted(temp_dir.glob("audio_*.mp3"))
            if not paths:
                raise OpenAITranscriptionError("외부 전송용 오디오 청크가 생성되지 않았습니다.")
            chunks: list[tuple[Path, float, AudioFileIdentity]] = []
            offset = 0.0
            for path in paths:
                path.chmod(0o600)
                chunk_identity = inspect_audio_path_no_symlinks(path)
                size = chunk_identity[2]
                if size <= 0 or size > self._config.stt.openai_max_upload_bytes:
                    raise OpenAITranscriptionError(
                        "외부 전송용 오디오 청크 크기가 허용 범위를 벗어났습니다."
                    )
                chunks.append((path, offset, chunk_identity))
                offset += float(measure_audio_duration(path))
            yield chunks
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @staticmethod
    def _parse_response(
        payload: dict[str, Any],
        offset: float,
        *,
        speaker_map: dict[str, str] | None = None,
    ) -> list[TranscriptSegment]:
        """diarized_json 응답 세그먼트를 검증하고 시간 offset을 적용한다."""
        raw_segments = payload.get("segments")
        if not isinstance(raw_segments, list):
            raise OpenAITranscriptionError("OpenAI 전사 응답에 시간 세그먼트가 없습니다.")
        segments: list[TranscriptSegment] = []
        for raw in raw_segments:
            if not isinstance(raw, dict):
                raise OpenAITranscriptionError("OpenAI 전사 세그먼트 형식이 올바르지 않습니다.")
            text = raw.get("text", "")
            start = raw.get("start")
            end = raw.get("end")
            if (
                not isinstance(text, str)
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
            ):
                raise OpenAITranscriptionError("OpenAI 전사 세그먼트 형식이 올바르지 않습니다.")
            normalized = unicodedata.normalize("NFC", text.strip())
            start_value = float(start)
            end_value = float(end)
            if not normalized:
                continue
            if (
                not math.isfinite(start_value)
                or not math.isfinite(end_value)
                or start_value < 0
                or end_value < start_value
            ):
                raise OpenAITranscriptionError("OpenAI 전사 시간 정보가 올바르지 않습니다.")
            normalized_speaker: str | None = None
            raw_speaker = raw.get("speaker")
            if speaker_map is not None and isinstance(raw_speaker, str) and raw_speaker:
                if raw_speaker not in speaker_map:
                    speaker_map[raw_speaker] = f"SPEAKER_{len(speaker_map):02d}"
                normalized_speaker = speaker_map[raw_speaker]
            segments.append(
                TranscriptSegment(
                    text=normalized,
                    start=round(offset + start_value, 3),
                    end=round(offset + end_value, 3),
                    speaker=normalized_speaker,
                )
            )
        return segments

    async def transcribe(
        self,
        audio_path: Path,
        vad_clip_timestamps: list[float] | None = None,
        *,
        timeout_override: int | None = None,
        resume_dir: Path | None = None,
        should_cancel: CancellationCheck | None = None,
        expected_audio_identity: AudioFileIdentity | None = None,
    ) -> TranscriptResult:
        """명시적 OpenAI 설정으로 음성을 전사하고 로컬 결과 형식으로 반환한다."""
        del vad_clip_timestamps  # OpenAI server VAD/chunking을 사용한다.
        self._raise_if_cancelled(should_cancel)
        if not is_loopback_host(str(self._config.server.host)):
            raise OpenAITranscriptionError(
                "OpenAI 전사는 서버가 로컬 주소로 실행될 때만 사용할 수 있습니다."
            )
        api_key = self._credential_resolver()
        if not api_key:
            raise OpenAITranscriptionError("OpenAI API 키가 등록되어 있지 않습니다.")
        if self._model != OPENAI_TRANSCRIBE_DIARIZE_MODEL:
            raise OpenAITranscriptionError("지원하지 않는 OpenAI 전사 모델입니다.")

        audio_identity = inspect_audio_path_no_symlinks(audio_path)
        if expected_audio_identity is not None and audio_identity != expected_audio_identity:
            raise AudioAdmissionError(
                "외부 전송 동의 후 오디오 파일이 변경되었습니다.",
                failure_kind=AudioFailureKind.SOURCE_BUSY,
            )
        await self._require_audio_quality(audio_path, audio_identity)
        request_timeout = timeout_override or self._config.stt.openai_timeout_seconds
        segments: list[TranscriptSegment] = []
        chunks_context = self._prepared_chunks(audio_path, audio_identity)
        # ffmpeg/subprocess 기반 준비는 이벤트 루프 밖에서 수행한다. 준비 중에도
        # FastAPI 취소 요청이 처리되어 첫 외부 업로드 전에 flag를 세울 수 있다.
        chunks = await asyncio.to_thread(chunks_context.__enter__)
        try:
            await asyncio.sleep(0)
            self._raise_if_cancelled(should_cancel)
            # 파일 하나일 때만 provider 화자 ID가 전체 회의에서 일관되다고 본다.
            # 여러 client-side 청크에서는 각 응답의 A/B 라벨이 재사용될 수 있어
            # speaker=None으로 두고 기존 로컬 pyannote 단계가 담당한다.
            speaker_map: dict[str, str] | None = {} if len(chunks) == 1 else None
            for chunk_index, (chunk_path, offset, chunk_identity) in enumerate(chunks):
                self._raise_if_cancelled(should_cancel)
                audio_bytes, audio_sha256 = self._read_chunk_for_upload(
                    chunk_path,
                    chunk_identity,
                )
                payload = (
                    self._read_cached_response(
                        resume_dir,
                        chunk_index=chunk_index,
                        audio_sha256=audio_sha256,
                    )
                    if resume_dir is not None
                    else None
                )
                from_cache = payload is not None
                if payload is None:
                    request_task = asyncio.create_task(
                        asyncio.to_thread(
                            self._transport,
                            audio_bytes,
                            api_key,
                            self._model,
                            request_timeout,
                        )
                    )
                    task_cancelled = False
                    try:
                        payload = await asyncio.shield(request_task)
                    except asyncio.CancelledError:
                        # to_thread의 HTTPS는 Task 취소만으로 중단되지 않는다. 결과를
                        # 버리면 서버가 처리·과금한 청크를 재시작 때 다시 보내므로,
                        # 고정 request timeout 범위 안에서 완료를 받아 아래 캐시에
                        # 기록한 뒤 원래 취소를 전파한다.
                        task_cancelled = True
                        try:
                            payload = await request_task
                        except Exception:
                            raise asyncio.CancelledError(
                                "OpenAI 요청 완료 확인 중 작업이 취소되었습니다."
                            ) from None
                else:
                    task_cancelled = False
                parsed = self._parse_response(payload, offset, speaker_map=speaker_map)
                if resume_dir is not None and not from_cache:
                    self._write_cached_response(
                        resume_dir,
                        chunk_index=chunk_index,
                        audio_sha256=audio_sha256,
                        response=payload,
                    )
                if task_cancelled:
                    raise asyncio.CancelledError(
                        "OpenAI 요청 응답을 캐시한 뒤 작업을 취소했습니다."
                    )
                # 마지막 청크 요청 중 들어온 취소도 성공 상태로 덮지 않는다.
                # 이미 받은 응답은 먼저 캐시해 사용자가 명시적으로 재시도할 때
                # 같은 청크를 다시 과금하지 않도록 한다.
                self._raise_if_cancelled(should_cancel)
                segments.extend(parsed)
        finally:
            await asyncio.to_thread(chunks_context.__exit__, None, None, None)
        self._assert_identity(audio_path, audio_identity)
        if not segments:
            raise EmptyAudioError("OpenAI 전사 결과가 비어있습니다.")
        full_text = " ".join(segment.text for segment in segments).strip()
        logger.info(
            "OpenAI 전사 완료: model=%s, segments=%d",
            self._model,
            len(segments),
        )
        return TranscriptResult(
            segments=segments,
            full_text=full_text,
            # diarized_json 계약에는 감지 언어 필드가 없다. 로컬 설정의 `ko`를
            # 실제 감지값처럼 기록하지 않고 자동 감지 provenance를 명시한다.
            language="auto",
            audio_path=str(audio_path),
            provider="openai",
            model=self._model,
        )
