"""
폴더 감시 모듈 (Folder Watcher Module)

목적: watchdog 라이브러리로 오디오 입력 폴더를 감시하여
     새 오디오 파일 감지 시 작업 큐에 자동 등록한다.
주요 기능:
    - watchdog Observer로 폴더 실시간 감시 (macOS FSEvents 활용)
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
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from config import AppConfig, get_config
from core.job_queue import AsyncJobQueue, JobQueueError

logger = logging.getLogger(__name__)


# === 에러 계층 ===


class WatcherError(Exception):
    """폴더 감시기에서 발생하는 에러의 기본 클래스."""


class AlreadyWatchingError(WatcherError):
    """감시기가 이미 실행 중일 때 start()를 호출한 경우."""


class WatchDirectoryError(WatcherError):
    """감시 대상 디렉토리 관련 에러."""


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

            file_path = Path(event.src_path)

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
        self._watch_dir: Path = self._config.paths.resolved_audio_input_dir
        self._debounce_seconds: float = self._config.watcher.debounce_seconds
        self._check_interval: float = self._config.watcher.check_interval_seconds

        # 지원 확장자 집합 (점 포함 소문자)
        self._supported_extensions: set[str] = {
            f".{fmt.lower()}" for fmt in self._config.audio.supported_input_formats
        }

        # Phase 1: 제외 서브디렉토리 + 오디오 품질 게이트
        # 저볼륨/너무 짧은 파일을 큐 진입 전 차단하여 STT 크래시 방지
        self._excluded_subdirs: set[str] = set(self._config.watcher.excluded_subdirs)
        self._quarantine_dir: Path = self._config.paths.resolved_audio_quarantine_dir

        # 품질 검증 콜러블 (enabled=False면 None으로 유지하여 오버헤드 제거)
        self._audio_validator: Callable[[Path], Any] | None = None
        if self._config.audio_quality.enabled:
            from functools import partial

            from core.audio_quality import validate_audio_quality

            self._audio_validator = partial(
                validate_audio_quality,
                min_mean_db=self._config.audio_quality.min_mean_volume_db,
                min_duration_s=self._config.audio_quality.min_duration_seconds,
            )

        # 상태 관리
        self._is_watching: bool = False
        self._observer: Observer | None = None
        self._handler: _AudioFileHandler | None = None

        # debounce 중인 파일 추적 (경로 → 마지막 크기 확인 시각)
        self._pending_files: dict[Path, float] = {}

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
            self._async_callbacks.append(callback)  # type: ignore[arg-type]
        else:
            self._sync_callbacks.append(callback)  # type: ignore[arg-type]
        logger.debug(f"파일 등록 콜백 등록: {cb_name}")

    async def _notify_callbacks(self, file_path: Path) -> None:
        """등록된 콜백들에 파일 등록 완료를 알린다.

        콜백 실행 중 발생하는 에러는 로깅하고 무시한다.

        Args:
            file_path: 큐에 등록된 오디오 파일 경로
        """
        # 동기 콜백 실행
        for cb in self._sync_callbacks:
            try:
                cb(file_path)
            except Exception as e:
                cb_name = getattr(cb, "__name__", repr(cb))
                logger.error(f"동기 콜백 실행 에러 ({cb_name}): {e}")

        # 비동기 콜백 실행
        for cb in self._async_callbacks:
            try:
                await cb(file_path)
            except Exception as e:
                cb_name = getattr(cb, "__name__", repr(cb))
                logger.error(f"비동기 콜백 실행 에러 ({cb_name}): {e}")

    def _is_excluded(self, path: Path) -> bool:
        """경로가 제외 서브디렉토리에 속하는지 판정한다.

        Phase 1: quarantine 같은 격리 폴더 내 파일은 재감지하지 않는다.
        실수로 watcher가 base_dir 전체를 재귀 감시하게 되어도
        이 방어 계층이 격리 폴더의 파일을 큐에 다시 등록하는 것을 막는다.

        심볼릭 링크 안전을 위해 resolve()로 정규화한 뒤 base_dir 기준
        상대 경로 첫 파트가 excluded_subdirs 목록에 속하는지 확인한다.

        Args:
            path: 검사할 파일 경로

        Returns:
            제외 대상이면 True, 아니면 False (base_dir 바깥 경로도 False)
        """
        try:
            rel = path.resolve().relative_to(self._config.paths.resolved_base_dir)
        except ValueError:
            # base_dir 바깥 경로는 제외 대상 아님
            return False
        # 경로 parts 중 첫 번째가 excluded_subdirs에 포함되면 True
        return bool(rel.parts) and rel.parts[0] in self._excluded_subdirs

    def _generate_meeting_id(self, file_path: Path) -> str:
        """파일 경로에서 meeting_id를 생성한다.

        파일명(확장자 제외)을 meeting_id로 사용한다.

        Args:
            file_path: 오디오 파일 경로

        Returns:
            생성된 meeting_id 문자열
        """
        return file_path.stem

    async def _wait_for_stable_size(self, file_path: Path) -> bool:
        """파일 크기가 안정될 때까지 대기한다.

        파일 복사 중에는 크기가 계속 변하므로,
        debounce_seconds 동안 크기가 변하지 않으면 복사 완료로 판단한다.

        Args:
            file_path: 대기할 파일 경로

        Returns:
            True면 안정화 완료, False면 파일 사라짐/접근 불가
        """
        last_size: int = -1
        stable_since: float = 0.0

        while True:
            try:
                if not file_path.exists():
                    logger.warning(f"파일이 사라짐: {file_path.name}")
                    return False

                current_size = file_path.stat().st_size

                if current_size == 0:
                    # 빈 파일은 아직 쓰기 시작 전일 수 있음
                    await asyncio.sleep(self._check_interval)
                    continue

                if current_size == last_size:
                    # 크기 동일 — 안정화 확인
                    elapsed = time.monotonic() - stable_since
                    if elapsed >= self._debounce_seconds:
                        logger.debug(
                            f"파일 크기 안정화 확인: {file_path.name} "
                            f"({current_size} bytes, {elapsed:.1f}초 대기)"
                        )
                        return True
                else:
                    # 크기 변화 — 타이머 리셋
                    last_size = current_size
                    stable_since = time.monotonic()

            except OSError as e:
                logger.warning(f"파일 상태 확인 실패: {file_path.name} — {e}")
                return False

            await asyncio.sleep(self._check_interval)

    async def _handle_new_file(self, file_path: Path) -> None:
        """새로 감지된 오디오 파일을 처리한다.

        1. 제외 경로 필터링 (Phase 1)
        2. 중복 등록 방지 (이미 pending 중이거나 큐에 있으면 스킵)
        3. debounce (파일 크기 안정화 대기)
        4. 오디오 품질 게이트 (Phase 1) — REJECT 시 quarantine 이동
        5. 작업 큐 등록
        6. 콜백 알림

        Args:
            file_path: 새로 감지된 오디오 파일 경로
        """
        resolved = file_path.resolve()

        # Phase 1: 제외 경로 무시 (quarantine 등)
        if self._is_excluded(resolved):
            logger.debug(f"제외 경로, 무시: {resolved}")
            return

        # debounce 중인 파일 중복 방지
        if resolved in self._pending_files:
            logger.debug(f"이미 처리 중인 파일: {resolved.name}")
            return

        self._pending_files[resolved] = time.monotonic()

        try:
            # meeting_id 생성
            meeting_id = self._generate_meeting_id(resolved)

            # 이미 큐에 등록된 회의인지 확인
            existing = await asyncio.to_thread(
                self._job_queue.queue.get_job_by_meeting_id,
                meeting_id,
            )
            if existing is not None:
                logger.info(f"이미 등록된 회의: {meeting_id} — 건너뜀")
                return

            # 파일 크기 안정화 대기
            is_stable = await self._wait_for_stable_size(resolved)
            if not is_stable:
                logger.warning(f"파일 안정화 실패: {resolved.name} — 등록 건너뜀")
                return

            # Phase 1: 오디오 품질 게이트
            # REJECT → quarantine 이동 + 큐 등록 차단
            # ERROR → 보수적 통과 (판단 보류, 큐 등록 허용)
            # ACCEPT → 정상 큐 등록
            if self._audio_validator is not None:
                from core.audio_quality import AudioQualityStatus

                try:
                    result = await asyncio.to_thread(self._audio_validator, resolved)
                except Exception as e:
                    # 품질 측정 자체 예외는 보수적 통과 (판단 보류)
                    logger.warning(
                        f"품질 측정 예외, 보수적 진행: {resolved} ({e})"
                    )
                    result = None

                if result is not None and result.status == AudioQualityStatus.REJECT:
                    from core.quarantine import QuarantineError, move_to_quarantine

                    try:
                        new_path = await asyncio.to_thread(
                            move_to_quarantine,
                            resolved,
                            self._quarantine_dir,
                            reason=result.reason,
                        )
                        logger.warning(
                            f"품질 게이트 거부: {resolved.name} "
                            f"({result.reason}) — quarantine 이동: {new_path}"
                        )
                    except QuarantineError as e:
                        logger.error(f"Quarantine 이동 실패: {e}")
                    return  # 큐 등록하지 않음

            # 작업 큐에 recorded 상태로 등록 (전사는 수동 요청 시 시작)
            from core.job_queue import JobStatus

            job_id = await self._job_queue.add_job(
                meeting_id=meeting_id,
                audio_path=str(resolved),
                initial_status=JobStatus.RECORDED.value,
            )
            logger.info(
                f"작업 큐 등록 (녹음 완료, 전사 대기): job_id={job_id}, "
                f"meeting_id={meeting_id}, "
                f"file={resolved.name}"
            )

            # 콜백 알림
            await self._notify_callbacks(resolved)

        except JobQueueError as e:
            logger.error(f"작업 큐 등록 실패: {resolved.name} — {e}")
        except Exception as e:
            logger.error(f"파일 처리 중 예상치 못한 에러: {resolved.name} — {e}")
        finally:
            # pending 목록에서 제거
            self._pending_files.pop(resolved, None)

    async def start(self) -> None:
        """폴더 감시를 시작한다.

        감시 대상 디렉토리가 없으면 자동 생성한다.
        watchdog Observer를 시작하여 백그라운드에서 파일 이벤트를 감시한다.

        Raises:
            AlreadyWatchingError: 이미 감시 중인 경우
            WatchDirectoryError: 감시 디렉토리 생성 실패 시
        """
        if self._is_watching:
            raise AlreadyWatchingError("폴더 감시기가 이미 실행 중입니다.")

        # 감시 디렉토리 생성 (존재하지 않으면)
        try:
            self._watch_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"감시 디렉토리 확인: {self._watch_dir}")
        except OSError as e:
            raise WatchDirectoryError(f"감시 디렉토리 생성 실패: {self._watch_dir} — {e}") from e

        # 현재 이벤트 루프 획득
        loop = asyncio.get_running_loop()

        # watchdog 핸들러 및 Observer 생성
        self._handler = _AudioFileHandler(
            supported_extensions=self._supported_extensions,
            on_new_file=self._handle_new_file,
            loop=loop,
        )

        self._observer = Observer()
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
        self._pending_files.clear()

        logger.info("폴더 감시 중지")

    async def scan_existing(self) -> list[int]:
        """감시 폴더에 이미 존재하는 오디오 파일을 스캔하여 큐에 등록한다.

        감시 시작 전 폴더에 이미 있는 파일을 처리할 때 사용한다.

        Phase 1 (2026-04-21): 앱 재기동 경로에서도 `_handle_new_file` 과 동일하게
        품질 게이트와 제외 경로를 적용한다. 크래시 후 launchd 재기동 시 저볼륨
        파일이 검증 없이 큐 재진입하여 동일 크래시를 유발하던 누수를 차단한다.

        Returns:
            등록된 작업 ID 리스트 (REJECT 파일은 격리되고 리스트에 포함되지 않음)
        """
        if not self._watch_dir.exists():
            logger.warning(f"감시 디렉토리가 존재하지 않습니다: {self._watch_dir}")
            return []

        registered_ids: list[int] = []

        for file_path in sorted(self._watch_dir.iterdir()):
            if not file_path.is_file():
                continue

            if file_path.suffix.lower() not in self._supported_extensions:
                continue

            resolved = file_path.resolve()

            # Phase 1: 제외 경로 무시
            if self._is_excluded(resolved):
                logger.debug(f"기존 파일 스캔: 제외 경로 무시 — {resolved}")
                continue

            meeting_id = self._generate_meeting_id(file_path)

            # 이미 등록된 회의 건너뜀
            existing = await asyncio.to_thread(
                self._job_queue.queue.get_job_by_meeting_id,
                meeting_id,
            )
            if existing is not None:
                logger.debug(f"기존 파일 이미 등록됨: {meeting_id}")
                continue

            # 빈 파일 건너뜀
            try:
                if file_path.stat().st_size == 0:
                    logger.debug(f"빈 파일 건너뜀: {file_path.name}")
                    continue
            except OSError:
                continue

            # Phase 1: 오디오 품질 게이트 (재기동 경로 누수 차단)
            if self._audio_validator is not None:
                try:
                    result = await asyncio.to_thread(self._audio_validator, resolved)
                except Exception as e:  # noqa: BLE001 — 품질 측정 예외는 보수적 통과
                    logger.warning(
                        f"기존 파일 품질 측정 예외, 보수적 진행: {resolved} ({e})"
                    )
                    result = None

                if result is not None and result.status.value == "reject":
                    from core.quarantine import QuarantineError, move_to_quarantine

                    try:
                        new_path = await asyncio.to_thread(
                            move_to_quarantine,
                            resolved,
                            self._quarantine_dir,
                            reason=f"재기동 스캔 거부: {result.reason}",
                        )
                        logger.warning(
                            f"기존 파일 품질 게이트 거부: {resolved.name} "
                            f"({result.reason}) — quarantine 이동: {new_path}"
                        )
                    except QuarantineError as e:
                        logger.error(f"기존 파일 Quarantine 이동 실패: {e}")
                    continue

            try:
                from core.job_queue import JobStatus

                job_id = await self._job_queue.add_job(
                    meeting_id=meeting_id,
                    audio_path=str(resolved),
                    initial_status=JobStatus.RECORDED.value,
                )
                registered_ids.append(job_id)
                logger.info(
                    f"기존 파일 등록 (녹음 완료, 전사 대기): {file_path.name} → job_id={job_id}"
                )
            except JobQueueError as e:
                logger.error(f"기존 파일 등록 실패: {file_path.name} — {e}")

        if registered_ids:
            logger.info(f"기존 파일 스캔 완료: {len(registered_ids)}건 등록")

        return registered_ids
