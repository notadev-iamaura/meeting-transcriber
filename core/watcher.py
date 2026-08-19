"""
폴더 감시 모듈 (Folder Watcher Module)

목적: watchdog 라이브러리로 오디오 입력 폴더를 감시하여
     새 오디오 파일 감지 시 작업 큐에 자동 등록한다.
주요 기능:
    - watchdog PollingObserver로 폴더 감시 (macOS FSEvents native crash 회피)
    - 오디오 확장자 화이트리스트 필터링
    - debounce로 파일 복사 완료 대기 (크기 안정화 확인)
    - AsyncJobQueue에 자동 등록
    - 중복 등록 방지 (meeting_id 기준)
    - start()/stop() 생명주기 관리
의존성: watchdog, config 모듈, core.job_queue
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import subprocess
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

from config import AppConfig, get_config
from core.job_queue import (
    AsyncJobQueue,
    JobQueueError,
    parse_audio_admission_hold,
    parse_audio_rejection_claim,
    parse_retranscribe_claim,
)
from core.quarantine import (
    QuarantineError,
    _lexical_absolute,
    _open_directory_tree_no_follow,
    _same_inode,
)

logger = logging.getLogger(__name__)


# === 에러 계층 ===


class WatcherError(Exception):
    """폴더 감시기에서 발생하는 에러의 기본 클래스."""


class AlreadyWatchingError(WatcherError):
    """감시기가 이미 실행 중일 때 start()를 호출한 경우."""


class WatchDirectoryError(WatcherError):
    """감시 대상 디렉토리 관련 에러."""


@dataclass(frozen=True)
class _FileFingerprint:
    """파일 identity와 writer 변경을 함께 탐지하는 lstat 지문."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    def as_tuple(self) -> tuple[int, int, int, int, int]:
        """quarantine helper에 전달할 불변 identity tuple을 반환한다."""
        return (self.device, self.inode, self.size, self.mtime_ns, self.ctime_ns)

    def as_recovery_tuple(self) -> tuple[int, int, int, int]:
        """hardlink ctime 변화에 안전한 durable recovery 지문을 반환한다."""
        return (self.device, self.inode, self.size, self.mtime_ns)


class _OpenWriterState(Enum):
    """lsof writable handle 검사 결과."""

    CLEAR = "clear"
    BUSY = "busy"
    INDETERMINATE = "indeterminate"


_CHECKPOINT_ARTIFACT_ALLOWLIST = frozenset(
    {
        "transcribe.json",
        "diarize.json",
        "merge.json",
        "correct.json",
        "summarize.json",
        "chunk.json",
        "embed.json",
    }
)
_OUTPUT_ARTIFACT_ALLOWLIST = frozenset(
    {
        "corrected.json",
        "summary.json",
        "summary.md",
        "meeting_minutes.md",
    }
)


def _configured_lexical_path(
    paths: Any,
    *,
    relative_attr: str | None,
    resolved_attr: str,
) -> Path:
    """설정 경로를 symlink 해석 없이 절대 경로로 만든다.

    실제 ``PathsConfig``에서는 raw ``base_dir``와 상대 경로를 조합해
    ``resolved_*`` property의 ``Path.resolve()``가 보안 검사 전에 symlink를
    따라가는 것을 막는다. 기존 테스트 double은 resolved property로 폴백한다.
    """
    raw_base = getattr(paths, "base_dir", None)
    raw_relative = getattr(paths, relative_attr, None) if relative_attr is not None else None
    if isinstance(raw_base, (str, Path)) and (
        relative_attr is None or isinstance(raw_relative, (str, Path))
    ):
        try:
            base_path = _lexical_absolute(Path(raw_base))
            if relative_attr is None:
                return base_path

            assert isinstance(raw_relative, (str, Path))
            raw_relative_text = os.fspath(raw_relative)
            relative_path = Path(raw_relative)
            if (
                not raw_relative_text
                or raw_relative_text in {".", ".."}
                or raw_relative_text.startswith("~")
                or relative_path.is_absolute()
                or ".." in relative_path.parts
            ):
                raise WatcherError(
                    f"{relative_attr}는 base_dir 하위 상대경로여야 합니다: {raw_relative_text!r}"
                )
            candidate = _lexical_absolute(base_path / relative_path)
        except QuarantineError as exc:
            raise WatcherError(
                f"{relative_attr or 'base_dir'} 경로가 base_dir 계약에 안전하지 않습니다: {exc}"
            ) from exc
        if not candidate.is_relative_to(base_path):
            raise WatcherError(f"{relative_attr}가 base_dir 밖을 가리킵니다: {candidate}")
        return candidate

    try:
        candidate = _lexical_absolute(Path(getattr(paths, resolved_attr)))
        resolved_base = getattr(paths, "resolved_base_dir", None)
        if relative_attr is not None and isinstance(resolved_base, (str, Path)):
            base_path = _lexical_absolute(Path(resolved_base))
            if not candidate.is_relative_to(base_path):
                raise WatcherError(f"{resolved_attr}가 base_dir 밖을 가리킵니다: {candidate}")
        return candidate
    except QuarantineError as exc:
        raise WatcherError(
            f"{resolved_attr} 경로가 base_dir 계약에 안전하지 않습니다: {exc}"
        ) from exc


# === 콜백 타입 정의 ===

# 동기 콜백: (file_path: Path) -> None
SyncCallback = Callable[[Path], None]
# 비동기 콜백: (file_path: Path) -> Coroutine
AsyncCallback = Callable[[Path], Coroutine[Any, Any, None]]


class _AudioFileHandler(FileSystemEventHandler):
    """오디오 파일 생성/이동 이벤트를 처리하는 핸들러.

    watchdog의 FileSystemEventHandler를 상속하여
    오디오 파일 감지 시 콜백을 호출한다.
    watchdog Observer는 별도 스레드에서 실행되므로,
    asyncio 이벤트 루프에 작업을 위임한다.

    Args:
        supported_extensions: 허용할 오디오 파일 확장자 집합 (소문자, 점 포함)
        on_new_file: 새 오디오 파일 감지 시 호출할 콜백
        loop: asyncio 이벤트 루프 (비동기 콜백 위임용)
    """

    def __init__(
        self,
        supported_extensions: set[str],
        on_new_file: Callable[[Path], Coroutine[Any, Any, None]],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """_AudioFileHandler를 초기화한다.

        Args:
            supported_extensions: 허용할 오디오 확장자 집합 (예: {".wav", ".mp3"})
            on_new_file: 새 파일 감지 시 호출할 비동기 콜백
            loop: asyncio 이벤트 루프
        """
        super().__init__()
        self._extensions = supported_extensions
        self._on_new_file = on_new_file
        self._loop = loop

    def _is_audio_file(self, path: Path) -> bool:
        """오디오 파일 확장자 여부를 확인한다.

        Args:
            path: 확인할 파일 경로

        Returns:
            오디오 파일이면 True
        """
        return path.suffix.lower() in self._extensions

    def on_created(self, event: FileSystemEvent) -> None:
        """파일 생성 이벤트를 처리한다.

        디렉토리 이벤트는 무시하고, 오디오 파일만 처리한다.
        watchdog 스레드에서 호출되므로 asyncio 루프에 작업을 위임한다.
        (STAB: Observer 스레드 예외 격리 — 예외 전파 시 Observer 전체가 중단되는 것을 방지)

        Args:
            event: watchdog 파일 시스템 이벤트
        """
        try:
            if event.is_directory:
                return

            src_path = event.src_path
            if isinstance(src_path, bytes):
                src_path = os.fsdecode(src_path)
            file_path = Path(src_path)

            if not self._is_audio_file(file_path):
                logger.debug(f"비오디오 파일 무시: {file_path.name}")
                return

            logger.info(f"새 오디오 파일 감지: {file_path.name}")
            asyncio.run_coroutine_threadsafe(
                self._on_new_file(file_path),
                self._loop,
            )
        except Exception as e:
            # Observer 스레드에서 예외가 전파되면 감시가 중단되므로
            # 여기서 반드시 잡아서 로깅만 한다
            logger.error(
                f"on_created 이벤트 처리 중 예외 (Observer 보호): {type(e).__name__}: {e}"
            )

    def on_moved(self, event: FileSystemEvent) -> None:
        """파일 이동 이벤트를 처리한다.

        감시 폴더로 파일이 이동된 경우를 처리한다.
        (예: Finder에서 드래그 앤 드롭)
        (STAB: Observer 스레드 예외 격리)

        Args:
            event: watchdog 파일 시스템 이벤트
        """
        try:
            if event.is_directory:
                return

            # dest_path가 있는 이동 이벤트만 처리
            dest_path = Path(getattr(event, "dest_path", ""))
            if not dest_path.name:
                return

            if not self._is_audio_file(dest_path):
                logger.debug(f"비오디오 파일 이동 무시: {dest_path.name}")
                return

            logger.info(f"오디오 파일 이동 감지: {dest_path.name}")
            asyncio.run_coroutine_threadsafe(
                self._on_new_file(dest_path),
                self._loop,
            )
        except Exception as e:
            # Observer 스레드에서 예외가 전파되면 감시가 중단되므로
            # 여기서 반드시 잡아서 로깅만 한다
            logger.error(f"on_moved 이벤트 처리 중 예외 (Observer 보호): {type(e).__name__}: {e}")

    def on_modified(self, event: FileSystemEvent) -> None:
        """쓰기 중 변경 이벤트를 dirty 재검사로 전달한다.

        생성 이벤트 처리 중 writer가 계속 쓰는 경우 같은 경로의 호출은
        FolderWatcher에서 하나의 dirty retry로 합쳐진다.
        """
        try:
            if event.is_directory:
                return

            src_path = event.src_path
            if isinstance(src_path, bytes):
                src_path = os.fsdecode(src_path)
            file_path = Path(src_path)
            if not self._is_audio_file(file_path):
                return

            asyncio.run_coroutine_threadsafe(
                self._on_new_file(file_path),
                self._loop,
            )
        except Exception as e:
            logger.error(
                f"on_modified 이벤트 처리 중 예외 (Observer 보호): {type(e).__name__}: {e}"
            )


class FolderWatcher:
    """오디오 입력 폴더 감시기.

    watchdog 라이브러리를 사용하여 지정된 폴더를 실시간으로 감시하고,
    새 오디오 파일이 감지되면 debounce 후 작업 큐에 자동 등록한다.

    Args:
        async_job_queue: 작업을 등록할 비동기 작업 큐
        config: 애플리케이션 설정 (None이면 싱글턴 사용)

    사용 예시:
        watcher = FolderWatcher(async_job_queue, config)
        watcher.on_file_registered(my_callback)
        await watcher.start()
        # ... 감시 중 ...
        await watcher.stop()
    """

    def __init__(
        self,
        async_job_queue: AsyncJobQueue,
        config: AppConfig | None = None,
    ) -> None:
        """FolderWatcher를 초기화한다.

        Args:
            async_job_queue: 비동기 작업 큐
            config: 애플리케이션 설정 (None이면 get_config() 사용)
        """
        self._config = config or get_config()
        self._job_queue = async_job_queue

        # 감시 설정 로드
        self._base_dir = _configured_lexical_path(
            self._config.paths,
            relative_attr=None,
            resolved_attr="resolved_base_dir",
        )
        self._watch_dir = _configured_lexical_path(
            self._config.paths,
            relative_attr="audio_input_dir",
            resolved_attr="resolved_audio_input_dir",
        )
        self._debounce_seconds: float = self._config.watcher.debounce_seconds
        self._check_interval: float = self._config.watcher.check_interval_seconds
        self._file_ready_timeout_seconds: float = self._config.watcher.file_ready_timeout_seconds

        # 지원 확장자 집합 (점 포함 소문자)
        self._supported_extensions: set[str] = {
            f".{fmt.lower()}" for fmt in self._config.audio.supported_input_formats
        }

        # Phase 1: 제외 서브디렉토리 + 오디오 품질 게이트
        # 저볼륨/너무 짧은 파일을 큐 진입 전 차단하여 STT 크래시 방지
        self._excluded_subdirs: set[str] = set(self._config.watcher.excluded_subdirs)
        self._quarantine_dir = _configured_lexical_path(
            self._config.paths,
            relative_attr="audio_quarantine_subdir",
            resolved_attr="resolved_audio_quarantine_dir",
        )
        self._checkpoints_dir = _configured_lexical_path(
            self._config.paths,
            relative_attr="checkpoints_dir",
            resolved_attr="resolved_checkpoints_dir",
        )
        self._outputs_dir = _configured_lexical_path(
            self._config.paths,
            relative_attr="outputs_dir",
            resolved_attr="resolved_outputs_dir",
        )

        # 품질 검증 콜러블 (enabled=False면 None으로 유지하여 오버헤드 제거)
        self._audio_validator: Callable[..., Any] | None = None
        if self._config.audio_quality.enabled:
            from core.audio_quality import validate_audio_quality

            self._audio_validator = validate_audio_quality

        # 상태 관리
        self._is_watching: bool = False
        self._observer: Any | None = None
        self._handler: _AudioFileHandler | None = None

        # debounce 중인 파일 추적 (경로 → 마지막 크기 확인 시각)
        self._pending_files: dict[Path, float] = {}
        # 처리 중 들어온 modified 이벤트는 파일별 1회의 후속 검사로 합친다.
        self._dirty_files: set[Path] = set()
        self._dirty_retry_tasks: set[asyncio.Task[None]] = set()
        # writable/indeterminate timeout은 close 이벤트가 없어도 파일별 1회 재검사한다.
        self._close_retry_paths: set[Path] = set()
        self._scheduled_retry_paths: set[Path] = set()

        # 콜백 목록
        self._sync_callbacks: list[SyncCallback] = []
        self._async_callbacks: list[AsyncCallback] = []

        logger.info(
            f"FolderWatcher 초기화: "
            f"watch_dir={self._watch_dir}, "
            f"debounce={self._debounce_seconds}초, "
            f"extensions={sorted(self._supported_extensions)}"
        )

    @property
    def is_watching(self) -> bool:
        """현재 감시가 진행 중인지 반환한다."""
        return self._is_watching

    @property
    def watch_dir(self) -> Path:
        """감시 대상 디렉토리 경로를 반환한다."""
        return self._watch_dir

    def on_file_registered(self, callback: SyncCallback | AsyncCallback) -> None:
        """파일 등록 완료 콜백을 등록한다.

        콜백은 파일이 작업 큐에 등록된 후 호출되며,
        등록된 파일의 Path를 인자로 받는다.

        Args:
            callback: 파일 등록 시 호출될 함수 또는 코루틴
        """
        cb_name = getattr(callback, "__name__", repr(callback))
        if asyncio.iscoroutinefunction(callback):
            self._async_callbacks.append(cast(AsyncCallback, callback))
        else:
            self._sync_callbacks.append(cast(SyncCallback, callback))
        logger.debug(f"파일 등록 콜백 등록: {cb_name}")

    async def _notify_callbacks(self, file_path: Path) -> None:
        """등록된 콜백들에 파일 등록 완료를 알린다.

        콜백 실행 중 발생하는 에러는 로깅하고 무시한다.

        Args:
            file_path: 큐에 등록된 오디오 파일 경로
        """
        # 동기 콜백 실행
        for sync_cb in self._sync_callbacks:
            try:
                sync_cb(file_path)
            except Exception as e:
                cb_name = getattr(sync_cb, "__name__", repr(sync_cb))
                logger.error(f"동기 콜백 실행 에러 ({cb_name}): {e}")

        # 비동기 콜백 실행
        for async_cb in self._async_callbacks:
            try:
                await async_cb(file_path)
            except Exception as e:
                cb_name = getattr(async_cb, "__name__", repr(async_cb))
                logger.error(f"비동기 콜백 실행 에러 ({cb_name}): {e}")

    def _is_excluded(self, path: Path) -> bool:
        """경로가 제외 서브디렉토리에 속하는지 판정한다.

        Phase 1: quarantine 같은 격리 폴더 내 파일은 재감지하지 않는다.
        실수로 watcher가 base_dir 전체를 재귀 감시하게 되어도
        이 방어 계층이 격리 폴더의 파일을 큐에 다시 등록하는 것을 막는다.

        symlink target을 따라가지 않는 lexical 절대 경로를 사용하여 base_dir
        기준 상대 경로 첫 파트가 excluded_subdirs 목록에 속하는지 확인한다.

        Args:
            path: 검사할 파일 경로

        Returns:
            제외 대상이면 True, 아니면 False (base_dir 바깥 경로도 False)
        """
        lexical_path = Path(os.path.abspath(os.fspath(path)))
        lexical_base = self._base_dir
        try:
            rel = lexical_path.relative_to(lexical_base)
        except ValueError:
            # base_dir 바깥 경로는 제외 대상 아님
            return False
        # 경로 parts 중 첫 번째가 excluded_subdirs에 포함되면 True
        return bool(rel.parts) and rel.parts[0] in self._excluded_subdirs

    @staticmethod
    def _fingerprint(file_stat: os.stat_result) -> _FileFingerprint:
        """lstat 결과를 비교 가능한 파일 지문으로 변환한다."""
        return _FileFingerprint(
            device=file_stat.st_dev,
            inode=file_stat.st_ino,
            size=file_stat.st_size,
            mtime_ns=file_stat.st_mtime_ns,
            ctime_ns=file_stat.st_ctime_ns,
        )

    def _inspect_input_file(self, file_path: Path) -> tuple[Path, _FileFingerprint] | None:
        """입력 root의 직접 자식인 일반 파일만 no-follow 방식으로 검사한다.

        symlink, 디렉토리, root 밖 경로는 target을 열지 않고 차단한다.
        """
        try:
            candidate = _lexical_absolute(Path(file_path))
            watch_dir = _lexical_absolute(self._watch_dir)
        except QuarantineError as e:
            logger.error(f"입력 경로 보안 차단: {file_path} ({e})")
            return None
        if candidate.parent != watch_dir or candidate.name in {"", ".", ".."}:
            logger.error(f"입력 경로 보안 차단 (direct child 아님): {candidate}")
            return None

        root_fd: int | None = None
        try:
            root_fd = _open_directory_tree_no_follow(watch_dir, create=False)
            file_stat = os.stat(
                candidate.name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            logger.debug(f"입력 파일이 존재하지 않음: {candidate}")
            return None
        except (OSError, QuarantineError) as e:
            logger.warning(f"입력 파일 no-follow 검사 실패: {candidate} — {e}")
            return None
        finally:
            if root_fd is not None:
                try:
                    os.close(root_fd)
                except OSError:
                    pass

        if stat.S_ISLNK(file_stat.st_mode):
            logger.error(f"입력 symlink 보안 차단 (target 미접근): {candidate}")
            return None
        if not stat.S_ISREG(file_stat.st_mode):
            logger.warning(f"일반 파일이 아닌 입력 무시: {candidate}")
            return None
        return candidate, self._fingerprint(file_stat)

    async def _probe_writable_open(self, file_path: Path, *, timeout: float) -> _OpenWriterState:
        """macOS lsof access mode로 writable fd 보유 여부를 검사한다.

        `aw`/`au`만 busy, `ar`만 존재하면 clear이다. 도구 부재·timeout·
        해석 불가능한 결과는 안전하게 indeterminate로 반환한다.
        """
        preferred_lsof = Path("/usr/sbin/lsof")
        if not preferred_lsof.is_file() or not os.access(preferred_lsof, os.X_OK):
            logger.error(
                "고정 system lsof를 찾을 수 없어 writable writer 여부를 판단할 수 없습니다"
            )
            return _OpenWriterState.INDETERMINATE
        lsof = str(preferred_lsof)

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [lsof, "-n", "-P", "-F", "a", "--", str(file_path)],
                capture_output=True,
                text=True,
                timeout=max(0.01, timeout),
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.error(f"lsof writable writer 검사 timeout: {file_path}")
            return _OpenWriterState.INDETERMINATE
        except OSError as e:
            logger.error(f"lsof writable writer 검사 실패: {file_path} — {e}")
            return _OpenWriterState.INDETERMINATE

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        access_lines = {line.strip() for line in stdout.splitlines() if line.startswith("a")}
        if "aw" in access_lines or "au" in access_lines:
            return _OpenWriterState.BUSY
        if result.returncode == 1 and not stdout.strip() and not stderr.strip():
            return _OpenWriterState.CLEAR
        if result.returncode == 0 and access_lines and access_lines <= {"ar"}:
            return _OpenWriterState.CLEAR

        logger.error(
            f"lsof writable writer 결과 해석 불가: returncode={result.returncode}, "
            f"stderr={stderr[-200:]}"
        )
        return _OpenWriterState.INDETERMINATE

    async def _confirm_closed_unchanged(
        self,
        file_path: Path,
        expected: _FileFingerprint,
        *,
        schedule_retry: bool,
    ) -> bool:
        """writer CLEAR와 validation fingerprint를 행동 직전에 한 번 더 확인한다."""
        before = self._inspect_input_file(file_path)
        if before is None or before[1] != expected:
            if schedule_retry:
                self._close_retry_paths.add(Path(os.path.abspath(os.fspath(file_path))))
            logger.warning(f"최종 readiness 전 파일 변경/교체 감지: {file_path.name}")
            return False

        writer_state = await self._probe_writable_open(
            before[0],
            timeout=min(2.0, max(0.5, self._check_interval)),
        )
        after = self._inspect_input_file(before[0])
        if after is None or after[1] != expected:
            if schedule_retry:
                self._close_retry_paths.add(before[0])
            logger.warning(f"최종 readiness 중 파일 변경/교체 감지: {file_path.name}")
            return False
        if writer_state is not _OpenWriterState.CLEAR:
            if schedule_retry:
                self._close_retry_paths.add(before[0])
            logger.warning(
                f"최종 readiness writer 미해소: {file_path.name} ({writer_state.value})"
            )
            return False
        return True

    def _schedule_deferred_retry(self, file_path: Path, *, delay: float) -> None:
        """dirty/close-only 이벤트를 파일별 1회의 bounded retry로 합친다."""
        if file_path in self._scheduled_retry_paths:
            return
        self._scheduled_retry_paths.add(file_path)

        async def _retry_once() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                await self._handle_new_file(file_path, _dirty_retry=True)
            finally:
                self._scheduled_retry_paths.discard(file_path)
                self._close_retry_paths.discard(file_path)

        retry_task = asyncio.create_task(_retry_once())
        self._dirty_retry_tasks.add(retry_task)
        retry_task.add_done_callback(self._dirty_retry_tasks.discard)

    async def _validate_unchanged(
        self,
        file_path: Path,
        expected: _FileFingerprint,
    ) -> tuple[bool, Any | None, _FileFingerprint | None]:
        """validation 전후 파일 지문이 동일할 때만 결과를 반환한다."""
        before = self._inspect_input_file(file_path)
        if before is None or before[1] != expected:
            self._close_retry_paths.add(Path(os.path.abspath(os.fspath(file_path))))
            logger.warning(f"품질 검증 전 파일 변경/교체 감지: {file_path.name}")
            return False, None, None

        if self._audio_validator is None:
            return True, None, before[1]

        try:
            result = await asyncio.to_thread(
                self._audio_validator,
                file_path,
                min_mean_db=self._config.audio_quality.min_mean_volume_db,
                min_duration_s=self._config.audio_quality.min_duration_seconds,
                expected_identity=before[1].as_tuple(),
                decode_timeout_base_seconds=(
                    self._config.audio_quality.decode_timeout_base_seconds
                ),
                decode_timeout_factor=self._config.audio_quality.decode_timeout_factor,
                decode_timeout_cap_seconds=(self._config.audio_quality.decode_timeout_cap_seconds),
            )
        except Exception as e:
            logger.exception(f"품질 측정 예외, 큐 등록 차단: {file_path} ({e})")
            return False, None, None

        after = self._inspect_input_file(file_path)
        if after is None or after[1] != before[1]:
            self._close_retry_paths.add(before[0])
            logger.warning(f"품질 검증 중 파일 변경/교체 감지: {file_path.name}")
            return False, None, None
        return True, result, after[1]

    def _has_transcript_artifacts(self, meeting_id: str) -> bool:
        """기존 transcript/output 산출물이 하나라도 있으면 보존 대상으로 판정한다."""
        if not self._is_valid_meeting_id(meeting_id):
            return True

        roots = (
            (
                self._checkpoints_dir,
                _CHECKPOINT_ARTIFACT_ALLOWLIST,
            ),
            (
                self._outputs_dir,
                _OUTPUT_ARTIFACT_ALLOWLIST,
            ),
        )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        for root, allowlist in roots:
            root_fd: int | None = None
            meeting_fd: int | None = None
            try:
                root_fd = _open_directory_tree_no_follow(Path(root), create=False)
                meeting_fd = os.open(meeting_id, flags, dir_fd=root_fd)
            except FileNotFoundError:
                continue
            except (OSError, QuarantineError):
                return True
            try:
                names = set(os.listdir(meeting_fd))
                if names & allowlist:
                    return True
            except OSError:
                return True
            finally:
                if meeting_fd is not None:
                    os.close(meeting_fd)
                if root_fd is not None:
                    os.close(root_fd)
        return False

    async def _release_legacy_job(self, job: Any, *, reason: str) -> None:
        """자동 실행/실패 UI를 막기 위해 queued·failed legacy job을 recorded로 돌린다."""
        from core.job_queue import JobStatus

        try:
            current = await asyncio.to_thread(self._job_queue.queue.get_job, job.id)
        except JobQueueError as e:
            logger.error(f"legacy job 현재 상태 조회 실패: {job.meeting_id} — {e}")
            return

        hold = parse_audio_admission_hold(str(current.requested_action or ""))
        if current.status == JobStatus.RECORDED.value and hold is not None:
            logger.warning(f"legacy job 보류 유지: {job.meeting_id} → recorded ({reason})")
            return
        if current.status not in {JobStatus.QUEUED.value, JobStatus.FAILED.value}:
            return
        try:
            await asyncio.to_thread(
                self._job_queue.queue.hold_job_for_audio_admission,
                current.id,
                uuid.uuid4().hex,
            )
            logger.warning(f"legacy job 보류 전환: {job.meeting_id} → recorded ({reason})")
        except JobQueueError as e:
            logger.error(f"legacy job 보류 전환 실패: {job.meeting_id} — {e}")

    async def _prepare_existing_audit(self, job: Any, safe_path: Path) -> bool:
        """legacy row가 자동 audit 대상인지 확인하고 queued 실행을 일시 보류한다."""
        from core.job_queue import JobStatus

        # 재전사 claim은 startup recovery가 staging·DB를 함께 복구해야
        # 하는 durable transaction이다. nominal ``recording`` 상태뿐 아니라
        # 이전 버전/부분 마이그레이션의 다른 상태에 남아도
        # watcher가 legacy media-invalid row로 오인해 삭제하지 않는다.
        retranscribe_claim = parse_retranscribe_claim(str(job.requested_action or ""))
        if retranscribe_claim is not None:
            logger.info(
                f"재전사 recovery claim은 legacy audit에서 제외: "
                f"{job.meeting_id} ({job.status}, phase={retranscribe_claim.phase})"
            )
            return False

        eligible_statuses = {
            JobStatus.RECORDED.value,
            JobStatus.QUEUED.value,
            JobStatus.FAILED.value,
        }
        if job.status not in eligible_statuses:
            logger.debug(f"기존 job 상태 보존: {job.meeting_id} ({job.status})")
            return False

        try:
            existing_audio_path = _lexical_absolute(Path(job.audio_path))
        except QuarantineError as e:
            logger.error(f"기존 job audio_path 보안 차단: {job.meeting_id} — {e}")
            return False
        if existing_audio_path != safe_path:
            logger.error(
                f"동일 meeting_id의 audio_path 불일치로 자동 정리 차단: "
                f"{job.meeting_id} ({existing_audio_path} != {safe_path})"
            )
            return False
        if self._has_transcript_artifacts(job.meeting_id):
            logger.info(f"기존 산출물 보유 job 자동 정리 제외: {job.meeting_id}")
            return False

        if job.status == JobStatus.RECORDED.value:
            return True

        if job.status not in {JobStatus.QUEUED.value, JobStatus.FAILED.value}:
            return False

        try:
            await asyncio.to_thread(
                self._job_queue.queue.hold_job_for_audio_admission,
                job.id,
                uuid.uuid4().hex,
            )
        except JobQueueError as e:
            logger.error(f"legacy audit 보류 실패: {job.meeting_id} — {e}")
            return False

        logger.info(f"legacy job 품질 audit 보류: {job.meeting_id} → recorded")
        return True

    async def _restore_held_queue(self, job: Any) -> None:
        """ACCEPT된 legacy hold를 DB payload의 원래 실행 계약으로 finalize한다."""
        try:
            current = await asyncio.to_thread(self._job_queue.queue.get_job, job.id)
        except JobQueueError as e:
            logger.error(f"legacy audit hold 조회 실패: {job.meeting_id} — {e}")
            return

        hold = parse_audio_admission_hold(str(current.requested_action or ""))
        if hold is None:
            return
        try:
            restored = await asyncio.to_thread(
                self._job_queue.queue.finalize_audio_admission_hold,
                current.id,
                hold.token,
            )
        except JobQueueError as e:
            logger.error(f"queued audit 의도 복원 실패: {job.meeting_id} — {e}")
            return
        logger.info(f"legacy job 품질 audit 통과 및 복원: {job.meeting_id} → {restored.status}")

    def _audio_rejection_destination(self, source: Path, token: str) -> Path:
        """durable claim에 저장할 token 기반 exact 목적지를 만든다."""
        candidate_name = f"{source.stem}_{token}{source.suffix}"
        # 정상적인 입력 파일명이 NAME_MAX에 가까운 경우 claim 뒤 영구 실패하지
        # 않도록 짧고 여전히 token-unique한 이름으로 결정적으로 폴백한다.
        if len(os.fsencode(candidate_name)) > 240:
            candidate_name = f"audio-rejection_{token}{source.suffix}"
        destination = self._quarantine_dir / candidate_name
        if destination.parent != self._quarantine_dir or destination.name in {"", ".", ".."}:
            raise WatcherError(f"안전한 media rejection 목적지를 만들 수 없습니다: {source}")
        return destination

    async def _reject_existing_media_invalid(
        self,
        job: Any,
        safe_path: Path,
        expected: _FileFingerprint,
        *,
        reason: str,
    ) -> bool:
        """legacy row를 claim→exact move→token finalize 순서로 정리한다.

        어느 단계에서든 실패하면 DB claim 또는 원본/격리 파일을 그대로 두어
        다음 startup recovery가 상태를 판별할 수 있게 한다.
        """
        if self._has_transcript_artifacts(job.meeting_id):
            logger.warning(f"media-invalid 정리 직전 산출물 발견, 원본·row 보존: {job.meeting_id}")
            return False

        current_source = self._inspect_input_file(safe_path)
        if current_source is None or current_source[1] != expected:
            logger.warning(
                f"media-invalid claim 직전 source identity 변경, 보존: {job.meeting_id}"
            )
            return False

        token = uuid.uuid4().hex
        destination = self._audio_rejection_destination(safe_path, token)
        try:
            claimed = await asyncio.to_thread(
                self._job_queue.queue.claim_for_audio_rejection,
                job.id,
                token,
                source_path=str(safe_path),
                source_identity=expected.as_recovery_tuple(),
                quarantine_path=str(destination),
            )
        except JobQueueError as e:
            logger.error(f"media-invalid durable claim 실패, 원본 보존: {job.meeting_id} — {e}")
            return False

        # 품질 검사와 claim 사이 또는 claim 직후 사용자 산출물이 생기면
        # 자동 격리보다 사용자 데이터 보존을 우선한다. claim은 다음 startup에도
        # 같은 보류 상태로 남는다.
        if self._has_transcript_artifacts(job.meeting_id):
            logger.warning(
                f"media-invalid claim 뒤 산출물 발견, claim·원본 보존: {job.meeting_id}"
            )
            return False

        moved = await self._move_to_quality_quarantine(
            safe_path,
            reason=reason,
            expected=expected,
            destination=destination,
        )
        if not moved:
            logger.error(f"media-invalid exact 격리 실패, durable claim 보존: {job.meeting_id}")
            return False

        try:
            await asyncio.to_thread(
                self._job_queue.queue.finalize_audio_rejection,
                claimed.id,
                token,
            )
        except JobQueueError as e:
            logger.error(
                f"media-invalid finalize 실패, quarantine·claim 보존: {job.meeting_id} — {e}"
            )
            return False

        logger.warning(f"legacy media-invalid job durable 정리 완료: {job.meeting_id}")
        return True

    async def _finish_ready_file(
        self,
        safe_path: Path,
        meeting_id: str,
        expected: _FileFingerprint,
        *,
        existing: Any | None,
        quarantine_reason_prefix: str,
        notify: bool,
    ) -> int | None:
        """안정화된 파일을 검증하고 마지막 readiness 확인 뒤 한 번만 행동한다."""
        if expected.size == 0 and self._audio_validator is None:
            logger.warning(f"0-byte 입력 보존·등록 차단 (품질 게이트 비활성): {safe_path.name}")
            if existing is not None:
                await self._release_legacy_job(existing, reason="closed zero-byte")
            return None

        validation_ok, result, validated_fingerprint = await self._validate_unchanged(
            safe_path,
            expected,
        )
        if not validation_ok or validated_fingerprint is None:
            if existing is not None:
                await self._release_legacy_job(existing, reason="validator infra/파일 변경")
            return None

        if result is not None:
            from core.audio_quality import AudioQualityStatus

            if result.status != AudioQualityStatus.ACCEPT:
                if getattr(result, "quarantine_safe", False) is True:
                    if not await self._confirm_closed_unchanged(
                        safe_path,
                        validated_fingerprint,
                        schedule_retry=True,
                    ):
                        if existing is not None:
                            await self._release_legacy_job(
                                existing,
                                reason="quarantine 직전 source busy/변경",
                            )
                        return None
                    rejection_reason = (
                        f"{quarantine_reason_prefix}: {result.reason}"
                        if quarantine_reason_prefix
                        else result.reason or f"품질 검증 상태: {result.status.value}"
                    )
                    if existing is not None:
                        await self._reject_existing_media_invalid(
                            existing,
                            safe_path,
                            validated_fingerprint,
                            reason=rejection_reason,
                        )
                    else:
                        await self._move_to_quality_quarantine(
                            safe_path,
                            reason=rejection_reason,
                            expected=validated_fingerprint,
                        )
                elif existing is not None:
                    await self._release_legacy_job(
                        existing,
                        reason=str(getattr(result, "failure_kind", "infra")),
                    )
                else:
                    logger.warning(
                        f"품질 게이트 판단 보류, 원본 보존: {safe_path.name} "
                        f"({getattr(result, 'failure_kind', 'unknown')})"
                    )
                return None

        if not await self._confirm_closed_unchanged(
            safe_path,
            validated_fingerprint,
            schedule_retry=True,
        ):
            if existing is not None:
                await self._release_legacy_job(existing, reason="queue 직전 source busy/변경")
            return None

        if existing is not None:
            await self._restore_held_queue(existing)
            return None

        from core.job_queue import JobStatus

        job_id = await self._job_queue.add_job(
            meeting_id=meeting_id,
            audio_path=str(safe_path),
            initial_status=JobStatus.RECORDED.value,
        )
        logger.info(
            f"작업 큐 등록 (녹음 완료, 전사 대기): job_id={job_id}, "
            f"meeting_id={meeting_id}, file={safe_path.name}"
        )
        if notify:
            await self._notify_callbacks(safe_path)
        return job_id

    def _generate_meeting_id(self, file_path: Path) -> str:
        """파일 경로에서 meeting_id를 생성한다.

        파일명(확장자 제외)을 meeting_id로 사용한다.

        Args:
            file_path: 오디오 파일 경로

        Returns:
            생성된 meeting_id 문자열
        """
        meeting_id = file_path.stem
        if not self._is_valid_meeting_id(meeting_id):
            raise WatcherError(f"안전하지 않은 meeting_id입니다: {meeting_id!r}")
        return meeting_id

    @staticmethod
    def _is_valid_meeting_id(meeting_id: str) -> bool:
        """DB·산출물 경로에 안전한 단일 이름인지 확인한다."""
        return bool(
            meeting_id
            and meeting_id not in {".", ".."}
            and "\x00" not in meeting_id
            and Path(meeting_id).name == meeting_id
            and "/" not in meeting_id
            and "\\" not in meeting_id
        )

    async def _wait_for_stable_size(self, file_path: Path) -> bool:
        """파일 크기가 안정될 때까지 대기한다.

        파일 복사 중에는 크기가 계속 변하므로,
        debounce_seconds 동안 지문이 변하지 않고 writable fd도 없을 때만
        완료로 판단한다. 모든 대기는 file_ready_timeout_seconds로 제한된다.

        Args:
            file_path: 대기할 파일 경로

        Returns:
            True면 안정화 완료, False면 busy/도구 불능/파일 교체/접근 불가
        """
        deadline = time.monotonic() + self._file_ready_timeout_seconds
        last_fingerprint: _FileFingerprint | None = None
        stable_since: float | None = None
        writer_waiting = False

        while time.monotonic() < deadline:
            inspected = self._inspect_input_file(file_path)
            if inspected is None:
                return False
            safe_path, fingerprint = inspected
            now = time.monotonic()

            if fingerprint.size == 0:
                # closed 0-byte는 안정화될 수 없는 media이므로 validator에
                # 즉시 보낸다. writable/indeterminate인 경우만 보존·재검사한다.
                writer_state = await self._probe_writable_open(
                    safe_path,
                    timeout=min(2.0, max(0.1, deadline - now)),
                )
                after_probe = self._inspect_input_file(safe_path)
                if after_probe is None:
                    return False
                if after_probe[1] != fingerprint:
                    last_fingerprint = after_probe[1]
                    stable_since = time.monotonic()
                elif writer_state is _OpenWriterState.CLEAR:
                    return True
                elif writer_state is _OpenWriterState.INDETERMINATE:
                    self._close_retry_paths.add(safe_path)
                    return False
                else:
                    writer_waiting = True
            elif fingerprint == last_fingerprint:
                if stable_since is not None and now - stable_since >= self._debounce_seconds:
                    remaining = deadline - now
                    if remaining <= 0:
                        break
                    writer_state = await self._probe_writable_open(
                        safe_path,
                        timeout=min(2.0, remaining),
                    )
                    after_probe = self._inspect_input_file(safe_path)
                    if after_probe is None:
                        return False
                    if after_probe[1] != fingerprint:
                        last_fingerprint = after_probe[1]
                        stable_since = time.monotonic()
                    elif writer_state == _OpenWriterState.CLEAR:
                        logger.debug(
                            f"파일 readiness 확인: {safe_path.name} ({fingerprint.size} bytes)"
                        )
                        return True
                    elif writer_state == _OpenWriterState.INDETERMINATE:
                        self._close_retry_paths.add(safe_path)
                        return False
                    # BUSY면 deadline까지 writer close를 관찰한다.
                    else:
                        writer_waiting = True
            else:
                last_fingerprint = fingerprint
                stable_since = now

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(self._check_interval, remaining))

        if writer_waiting:
            self._close_retry_paths.add(Path(os.path.abspath(os.fspath(file_path))))
        logger.warning(
            f"파일 readiness timeout: {file_path.name} ({self._file_ready_timeout_seconds:.1f}초)"
        )
        return False

    async def _handle_new_file(
        self,
        file_path: Path,
        *,
        _dirty_retry: bool = False,
    ) -> None:
        """새로 감지된 오디오 파일을 처리한다.

        1. 제외 경로 필터링 (Phase 1)
        2. 중복 등록 방지 (이미 pending 중이거나 큐에 있으면 스킵)
        3. debounce (파일 크기 안정화 대기)
        4. 오디오 품질 게이트 (Phase 1) — 비정상 결과는 quarantine 이동
        5. 작업 큐 등록
        6. 콜백 알림

        Args:
            file_path: 새로 감지된 오디오 파일 경로
        """
        inspected = self._inspect_input_file(file_path)
        if inspected is None:
            return
        safe_path, _ = inspected

        # Phase 1: 제외 경로 무시 (quarantine 등)
        if self._is_excluded(safe_path):
            logger.debug(f"제외 경로, 무시: {safe_path}")
            return

        # debounce 중인 파일 중복 방지
        if safe_path in self._pending_files:
            self._dirty_files.add(safe_path)
            logger.debug(f"이미 처리 중인 파일, dirty 표시: {safe_path.name}")
            return

        self._pending_files[safe_path] = time.monotonic()

        try:
            # meeting_id 생성
            meeting_id = self._generate_meeting_id(safe_path)

            # 이미 큐에 등록된 회의인지 확인
            existing = await asyncio.to_thread(
                self._job_queue.queue.get_job_by_meeting_id,
                meeting_id,
            )
            if existing is not None and not await self._prepare_existing_audit(
                existing,
                safe_path,
            ):
                return

            # 파일 크기 안정화 대기
            is_stable = await self._wait_for_stable_size(safe_path)
            if not is_stable:
                logger.warning(f"파일 안정화 실패: {safe_path.name} — 원본 보존")
                if existing is not None:
                    await self._release_legacy_job(
                        existing,
                        reason="source busy/indeterminate",
                    )
                return

            stable_inspection = self._inspect_input_file(safe_path)
            if stable_inspection is None:
                if existing is not None:
                    await self._release_legacy_job(existing, reason="source identity 변경")
                return

            await self._finish_ready_file(
                safe_path,
                meeting_id,
                stable_inspection[1],
                existing=existing,
                quarantine_reason_prefix="",
                notify=True,
            )

        except JobQueueError as e:
            logger.error(f"작업 큐 등록 실패: {safe_path.name} — {e}")
        except WatcherError as e:
            logger.error(f"입력 파일 식별자 거부: {safe_path.name} — {e}")
        except Exception as e:
            logger.exception(f"파일 처리 중 예상치 못한 에러: {safe_path.name} — {e}")
        finally:
            # pending 목록에서 제거
            self._pending_files.pop(safe_path, None)
            should_retry = safe_path in self._dirty_files or safe_path in self._close_retry_paths
            self._dirty_files.discard(safe_path)
            self._close_retry_paths.discard(safe_path)
            # return 경로에서도 후속 검사가 실행되도록 task로 예약한다.
            if should_retry and not _dirty_retry:
                self._schedule_deferred_retry(
                    safe_path,
                    delay=self._check_interval,
                )
            elif _dirty_retry:
                self._dirty_files.discard(safe_path)
                self._close_retry_paths.discard(safe_path)

    async def start(self) -> None:
        """폴더 감시를 시작한다.

        감시 대상 디렉토리가 없으면 자동 생성한다.
        watchdog PollingObserver를 시작하여 백그라운드에서 파일 이벤트를 감시한다.
        macOS FSEvents 기반 Observer는 테스트/앱 종료 시 native extension
        세그폴트가 재현되어 사용하지 않는다.

        Raises:
            AlreadyWatchingError: 이미 감시 중인 경우
            WatchDirectoryError: 감시 디렉토리 생성 실패 시
        """
        if self._is_watching:
            raise AlreadyWatchingError("폴더 감시기가 이미 실행 중입니다.")

        # 감시 디렉토리 생성/검증: 모든 중간 component를 no-follow로 연다.
        try:
            watch_fd = _open_directory_tree_no_follow(self._watch_dir, create=True)
            os.close(watch_fd)
            logger.info(f"감시 디렉토리 확인: {self._watch_dir}")
        except (OSError, QuarantineError) as e:
            raise WatchDirectoryError(f"감시 디렉토리 생성 실패: {self._watch_dir} — {e}") from e

        # 현재 이벤트 루프 획득
        loop = asyncio.get_running_loop()

        # watchdog 핸들러 및 PollingObserver 생성
        self._handler = _AudioFileHandler(
            supported_extensions=self._supported_extensions,
            on_new_file=self._handle_new_file,
            loop=loop,
        )

        # macOS FSEvents native observer는 shutdown 경계에서 Python 프로세스를
        # 세그폴트시킬 수 있다. 입력 폴더 규모가 작으므로 polling 안정성을 우선한다.
        self._observer = PollingObserver(timeout=max(0.2, self._check_interval))
        self._observer.schedule(
            self._handler,
            str(self._watch_dir),
            recursive=False,  # 하위 폴더 미감시
        )

        self._observer.start()
        self._is_watching = True

        logger.info(f"폴더 감시 시작: {self._watch_dir}")

    async def stop(self) -> None:
        """폴더 감시를 중지한다.

        실행 중인 Observer를 정지하고 정리한다.
        이미 중지된 상태에서 호출해도 에러 없이 무시한다.
        """
        retry_tasks = list(self._dirty_retry_tasks)
        for task in retry_tasks:
            task.cancel()
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)
        self._dirty_retry_tasks.clear()
        self._scheduled_retry_paths.clear()
        self._close_retry_paths.clear()
        self._pending_files.clear()
        self._dirty_files.clear()

        if not self._is_watching:
            logger.debug("폴더 감시기가 이미 중지 상태입니다.")
            return

        self._is_watching = False

        if self._observer is not None:
            self._observer.stop()
            # Observer 스레드 종료 대기 (블로킹 방지)
            await asyncio.to_thread(self._observer.join, timeout=5.0)
            self._observer = None

        self._handler = None
        logger.info("폴더 감시 중지")

    @staticmethod
    def _recovery_identity_matches(
        file_stat: os.stat_result,
        expected: tuple[int, int, int, int],
    ) -> bool:
        """일반 파일의 durable dev/ino/size/mtime identity가 같은지 반환한다."""
        return bool(
            stat.S_ISREG(file_stat.st_mode)
            and (
                file_stat.st_dev,
                file_stat.st_ino,
                file_stat.st_size,
                file_stat.st_mtime_ns,
            )
            == expected
        )

    @staticmethod
    def _stat_recovery_entry(directory_fd: int, name: str) -> os.stat_result | None:
        """pinned directory의 direct child를 target 미추적 상태로 검사한다."""
        try:
            return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    @staticmethod
    def _recovery_root_matches_fd(root: Path, directory_fd: int) -> bool:
        """raw lexical root가 여전히 pinned directory inode인지 확인한다."""
        check_fd: int | None = None
        try:
            check_fd = _open_directory_tree_no_follow(root, create=False)
            return _same_inode(os.fstat(check_fd), os.fstat(directory_fd))
        except (OSError, QuarantineError):
            return False
        finally:
            if check_fd is not None:
                os.close(check_fd)

    @staticmethod
    def _recovery_root_remains_absent(root: Path) -> bool:
        """초기 검사에서 없던 raw lexical root가 계속 없는지 확인한다."""
        check_fd: int | None = None
        try:
            check_fd = _open_directory_tree_no_follow(root, create=False)
        except FileNotFoundError:
            return True
        except (OSError, QuarantineError):
            return False
        else:
            return False
        finally:
            if check_fd is not None:
                os.close(check_fd)

    def _validated_audio_rejection_paths(
        self,
        job: Any,
        claim: Any,
    ) -> tuple[Path, Path] | None:
        """claim 경로가 configured root의 lexical direct child인지 검증한다."""
        try:
            watch_root = _lexical_absolute(self._watch_dir)
            quarantine_root = _lexical_absolute(self._quarantine_dir)
            source = _lexical_absolute(Path(claim.source_path))
            destination = _lexical_absolute(Path(claim.quarantine_path))
            job_source = _lexical_absolute(Path(job.audio_path))
        except QuarantineError as e:
            logger.warning(f"audio rejection claim 경로 거부: {job.meeting_id} — {e}")
            return None

        if watch_root == quarantine_root:
            logger.warning(f"audio rejection root 충돌로 복구 보류: {job.meeting_id}")
            return None
        if (
            source.parent != watch_root
            or destination.parent != quarantine_root
            or source.name in {"", ".", ".."}
            or destination.name in {"", ".", ".."}
            or source != job_source
            or source.stem != job.meeting_id
        ):
            logger.warning(
                f"audio rejection claim direct-child 계약 불일치, 보존: {job.meeting_id}"
            )
            return None
        return source, destination

    async def _recover_audio_rejection_claim(self, job: Any, claim: Any) -> None:
        """단일 durable media-rejection claim을 fail-closed로 복구한다."""
        from core.job_queue import JobStatus

        if job.status != JobStatus.RECORDING.value:
            logger.warning(
                f"audio rejection claim 상태 불일치, 보존: {job.meeting_id} ({job.status})"
            )
            return
        if self._has_transcript_artifacts(job.meeting_id):
            logger.warning(f"audio rejection claim에 산출물이 존재해 복구 보류: {job.meeting_id}")
            return

        validated = self._validated_audio_rejection_paths(job, claim)
        if validated is None:
            return
        source, destination = validated

        source_fd: int | None = None
        quarantine_fd: int | None = None
        source_root_missing = False
        try:
            try:
                source_fd = _open_directory_tree_no_follow(source.parent, create=False)
            except FileNotFoundError:
                source_root_missing = True
            quarantine_fd = _open_directory_tree_no_follow(
                destination.parent,
                create=True,
            )

            source_stat = (
                None if source_fd is None else self._stat_recovery_entry(source_fd, source.name)
            )
            destination_stat = self._stat_recovery_entry(
                quarantine_fd,
                destination.name,
            )
            source_present = source_stat is not None
            destination_present = destination_stat is not None
            source_matches = bool(
                source_stat is not None
                and self._recovery_identity_matches(source_stat, claim.source_identity)
            )
            destination_matches = bool(
                destination_stat is not None
                and self._recovery_identity_matches(
                    destination_stat,
                    claim.source_identity,
                )
            )

            if source_present and not destination_present and source_matches:
                assert source_fd is not None
                if not self._recovery_root_matches_fd(source.parent, source_fd) or not (
                    self._recovery_root_matches_fd(destination.parent, quarantine_fd)
                ):
                    logger.warning(
                        f"audio rejection recovery root 교체 감지, 보존: {job.meeting_id}"
                    )
                    return
                current_source = self._stat_recovery_entry(source_fd, source.name)
                current_destination = self._stat_recovery_entry(
                    quarantine_fd,
                    destination.name,
                )
                if (
                    current_source is None
                    or current_destination is not None
                    or not self._recovery_identity_matches(
                        current_source,
                        claim.source_identity,
                    )
                ):
                    logger.warning(
                        f"audio rejection recovery 직전 상태 변경, 보존: {job.meeting_id}"
                    )
                    return
                moved = await self._move_to_quality_quarantine(
                    source,
                    reason="startup durable media-invalid recovery",
                    expected=self._fingerprint(current_source),
                    destination=destination,
                )
                if not moved:
                    logger.warning(
                        f"audio rejection source-only 이동 실패, claim 보존: {job.meeting_id}"
                    )
                    return
                after_source = self._stat_recovery_entry(source_fd, source.name)
                after_destination = self._stat_recovery_entry(
                    quarantine_fd,
                    destination.name,
                )
                if (
                    after_source is not None
                    or after_destination is None
                    or not self._recovery_identity_matches(
                        after_destination,
                        claim.source_identity,
                    )
                    or not self._recovery_root_matches_fd(
                        destination.parent,
                        quarantine_fd,
                    )
                ):
                    logger.warning(
                        f"audio rejection 이동 후 identity 불일치, claim 보존: {job.meeting_id}"
                    )
                    return
                await asyncio.to_thread(
                    self._job_queue.queue.finalize_audio_rejection,
                    job.id,
                    claim.token,
                )
                logger.warning(f"audio rejection source-only 복구 완료: {job.meeting_id}")
                return

            if not source_present and destination_present and destination_matches:
                source_absence_stable = (
                    self._recovery_root_remains_absent(source.parent)
                    if source_root_missing
                    else source_fd is not None
                    and self._recovery_root_matches_fd(source.parent, source_fd)
                    and self._stat_recovery_entry(source_fd, source.name) is None
                )
                current_destination = self._stat_recovery_entry(
                    quarantine_fd,
                    destination.name,
                )
                if (
                    not source_absence_stable
                    or current_destination is None
                    or not self._recovery_identity_matches(
                        current_destination,
                        claim.source_identity,
                    )
                    or not self._recovery_root_matches_fd(
                        destination.parent,
                        quarantine_fd,
                    )
                ):
                    logger.warning(
                        f"audio rejection quarantine-only 재검증 실패, 보존: {job.meeting_id}"
                    )
                    return
                await asyncio.to_thread(
                    self._job_queue.queue.finalize_audio_rejection,
                    job.id,
                    claim.token,
                )
                logger.warning(f"audio rejection quarantine-only 복구 완료: {job.meeting_id}")
                return

            logger.warning(
                f"audio rejection 복구 상태 모호, 파일·claim 보존: {job.meeting_id} "
                f"(source_present={source_present}, source_match={source_matches}, "
                f"quarantine_present={destination_present}, "
                f"quarantine_match={destination_matches})"
            )
        except (OSError, QuarantineError, JobQueueError) as e:
            logger.warning(f"audio rejection claim 복구 실패, 보존: {job.meeting_id} — {e}")
        finally:
            if source_fd is not None:
                os.close(source_fd)
            if quarantine_fd is not None:
                os.close(quarantine_fd)

    async def recover_audio_rejection_claims(self) -> None:
        """startup에서 durable media-rejection claim을 다른 audit보다 먼저 복구한다."""
        try:
            jobs = await self._job_queue.get_all_jobs()
        except JobQueueError as e:
            logger.error(f"audio rejection recovery job 조회 실패: {e}")
            return

        for job in jobs:
            claim = parse_audio_rejection_claim(str(job.requested_action or ""))
            if claim is None:
                continue
            try:
                await self._recover_audio_rejection_claim(job, claim)
            except Exception as e:
                # 손상된 한 row가 나머지 recovery와 일반 startup scan을 막지 않는다.
                logger.exception(f"audio rejection recovery 예외 격리: {job.meeting_id} — {e}")

    async def scan_existing(self) -> list[int]:
        """감시 폴더에 이미 존재하는 오디오 파일을 스캔하여 큐에 등록한다.

        감시 시작 전 폴더에 이미 있는 파일을 처리할 때 사용한다.

        Phase 1 (2026-04-21): 앱 재기동 경로에서도 `_handle_new_file` 과 동일하게
        품질 게이트와 제외 경로를 적용한다. 크래시 후 launchd 재기동 시 저볼륨
        파일이 검증 없이 큐 재진입하여 동일 크래시를 유발하던 누수를 차단한다.

        Returns:
            등록된 작업 ID 리스트 (비정상 파일은 격리되고 리스트에 포함되지 않음)
        """
        registered_ids: list[int] = []
        retry_paths: set[Path] = set()

        # filesystem 목록 조회보다 먼저 DB journal을 복구해야 quarantine-only
        # transaction이 입력 root 부재에도 finalize되고, source-only claim이
        # 일반 legacy audit/validator로 다시 들어가지 않는다.
        await self.recover_audio_rejection_claims()

        watch_fd: int | None = None
        try:
            watch_fd = _open_directory_tree_no_follow(self._watch_dir, create=False)
            candidates = [self._watch_dir / name for name in sorted(os.listdir(watch_fd))]
        except FileNotFoundError:
            logger.warning(f"감시 디렉토리가 존재하지 않습니다: {self._watch_dir}")
            return []
        except (OSError, QuarantineError) as e:
            logger.error(f"기존 파일 목록 조회 실패: {self._watch_dir} — {e}")
            return []
        finally:
            if watch_fd is not None:
                try:
                    os.close(watch_fd)
                except OSError:
                    pass

        # startup에서는 파일마다 최대 readiness timeout을 순차 대기하지 않는다.
        # 한 번의 공통 debounce 뒤 동일 fingerprint인 파일만 검사한다.
        pending: list[tuple[Path, str, _FileFingerprint, Any | None]] = []
        for file_path in candidates:
            try:
                if file_path.suffix.lower() not in self._supported_extensions:
                    continue

                # meeting_id/DB 조회는 lexical 파일명만 사용하므로 symlink target에
                # 접근하지 않는다. SECURITY_BLOCKED legacy queued/failed도 여기서
                # recorded로 돌려 startup processor 진입을 막는다.
                meeting_id = self._generate_meeting_id(file_path)
                existing = await asyncio.to_thread(
                    self._job_queue.queue.get_job_by_meeting_id,
                    meeting_id,
                )
                inspected = self._inspect_input_file(file_path)
                if inspected is None:
                    if existing is not None and not self._has_transcript_artifacts(meeting_id):
                        try:
                            blocked_path = _lexical_absolute(file_path)
                            existing_path = _lexical_absolute(Path(existing.audio_path))
                        except QuarantineError:
                            pass
                        else:
                            if existing_path == blocked_path:
                                await self._release_legacy_job(
                                    existing,
                                    reason="security blocked",
                                )
                    continue
                safe_path, fingerprint = inspected
                if self._is_excluded(safe_path):
                    continue

                if existing is not None and not await self._prepare_existing_audit(
                    existing,
                    safe_path,
                ):
                    continue

                if fingerprint.size != 0:
                    pending.append((safe_path, meeting_id, fingerprint, existing))
                    continue

                # closed 0-byte는 공통 debounce 없이 즉시 validator로 보낸다.
                if not await self._confirm_closed_unchanged(
                    safe_path,
                    fingerprint,
                    schedule_retry=True,
                ):
                    if existing is not None:
                        await self._release_legacy_job(
                            existing,
                            reason="source busy/indeterminate",
                        )
                    if safe_path in self._close_retry_paths:
                        self._close_retry_paths.discard(safe_path)
                        retry_paths.add(safe_path)
                    continue

                job_id = await self._finish_ready_file(
                    safe_path,
                    meeting_id,
                    fingerprint,
                    existing=existing,
                    quarantine_reason_prefix="재기동 스캔 차단",
                    notify=False,
                )
                if job_id is not None:
                    registered_ids.append(job_id)
                if safe_path in self._close_retry_paths:
                    self._close_retry_paths.discard(safe_path)
                    retry_paths.add(safe_path)
            except JobQueueError as e:
                logger.error(f"기존 파일 등록 실패: {file_path.name} — {e}")
            except WatcherError as e:
                logger.error(f"기존 입력 파일 식별자 거부: {file_path.name} — {e}")
            except Exception as e:
                # 한 파일의 quarantine/mkdir/검증 실패가 다음 파일 스캔을 막지 않는다.
                logger.exception(f"기존 파일 스캔 중 예외 격리: {file_path.name} — {e}")

        if pending:
            await asyncio.sleep(self._debounce_seconds)

        for safe_path, meeting_id, expected, existing in pending:
            try:
                after_debounce = self._inspect_input_file(safe_path)
                if after_debounce is None or after_debounce[1] != expected:
                    logger.warning(f"startup debounce 중 파일 변경 감지: {safe_path.name}")
                    if existing is not None:
                        await self._release_legacy_job(existing, reason="source growing/변경")
                    retry_paths.add(safe_path)
                    continue
                if not await self._confirm_closed_unchanged(
                    safe_path,
                    expected,
                    schedule_retry=True,
                ):
                    if existing is not None:
                        await self._release_legacy_job(
                            existing,
                            reason="source busy/indeterminate",
                        )
                    if safe_path in self._close_retry_paths:
                        self._close_retry_paths.discard(safe_path)
                        retry_paths.add(safe_path)
                    continue

                job_id = await self._finish_ready_file(
                    safe_path,
                    meeting_id,
                    expected,
                    existing=existing,
                    quarantine_reason_prefix="재기동 스캔 차단",
                    notify=False,
                )
                if job_id is not None:
                    registered_ids.append(job_id)
                    logger.info(
                        f"기존 파일 등록 (녹음 완료, 전사 대기): "
                        f"{safe_path.name} → job_id={job_id}"
                    )
                if safe_path in self._close_retry_paths:
                    self._close_retry_paths.discard(safe_path)
                    retry_paths.add(safe_path)
            except JobQueueError as e:
                logger.error(f"기존 파일 등록 실패: {safe_path.name} — {e}")
            except Exception as e:
                logger.exception(f"기존 파일 스캔 중 예외 격리: {safe_path.name} — {e}")

        for retry_path in retry_paths:
            self._schedule_deferred_retry(retry_path, delay=self._check_interval)

        if registered_ids:
            logger.info(f"기존 파일 스캔 완료: {len(registered_ids)}건 등록")

        return registered_ids

    async def _move_to_quality_quarantine(
        self,
        file_path: Path,
        *,
        reason: str,
        expected: _FileFingerprint,
        destination: Path | None = None,
    ) -> bool:
        """품질 검증 실패 파일을 격리하고 성공 여부를 반환한다."""
        from core.quarantine import move_to_quarantine, move_to_quarantine_exact

        try:
            if destination is None:
                new_path = await asyncio.to_thread(
                    move_to_quarantine,
                    file_path,
                    self._quarantine_dir,
                    reason=reason,
                    expected_identity=expected.as_tuple(),
                )
            else:
                new_path = await asyncio.to_thread(
                    move_to_quarantine_exact,
                    file_path,
                    destination,
                    reason=reason,
                    expected_identity=expected.as_tuple(),
                )
            logger.warning(
                f"품질 게이트 차단: {file_path.name} ({reason}) — quarantine 이동: {new_path}"
            )
            return True
        except QuarantineError as e:
            logger.error(f"Quarantine 이동 실패: {e}")
            return False
