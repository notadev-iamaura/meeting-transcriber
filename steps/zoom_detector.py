"""
Zoom 프로세스 감지기 모듈 (Zoom Process Detector Module)

목적: macOS에서 Zoom 미팅 프로세스(CptHost)를 폴링 방식으로 감지하여
     미팅 시작/종료 이벤트를 발행한다.
주요 기능:
    - pgrep -f CptHost로 Zoom 미팅 프로세스 감지 (5초 폴링)
    - asyncio.Event로 미팅 시작/종료 이벤트 발행
    - 콜백 패턴으로 외부 컴포넌트에 상태 변화 알림
    - graceful start/stop 지원
    - 중복 이벤트 방지 (상태 변화 시에만 이벤트 발행)
의존성: config 모듈 (zoom 섹션 설정)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from config import AppConfig, get_config

logger = logging.getLogger(__name__)


# === 에러 계층 ===


class ZoomDetectorError(Exception):
    """Zoom 감지기에서 발생하는 에러의 기본 클래스."""


class ProcessCheckError(ZoomDetectorError):
    """프로세스 확인 중 에러가 발생했을 때."""

    def __init__(self, message: str, original_error: Exception | None = None) -> None:
        self.original_error = original_error
        super().__init__(message)


class AlreadyRunningError(ZoomDetectorError):
    """감지기가 이미 실행 중일 때 start()를 호출한 경우."""


# === 콜백 타입 정의 ===

# 동기 콜백: (meeting_started: bool) -> None
SyncCallback = Callable[[bool], None]
# 비동기 콜백: (meeting_started: bool) -> Coroutine
AsyncCallback = Callable[[bool], Coroutine[Any, Any, None]]


class ZoomDetector:
    """Zoom 미팅 프로세스 감지기.

    macOS에서 pgrep 명령을 사용하여 Zoom 미팅 프로세스(CptHost)의
    존재 여부를 주기적으로 폴링하고, 미팅 시작/종료 이벤트를 발행한다.

    이벤트 구독 방법:
        1. asyncio.Event: meeting_started_event / meeting_ended_event
        2. 콜백: on_meeting_change(callback) 등록

    Args:
        config: 애플리케이션 설정 (None이면 싱글턴 사용)

    사용 예시:
        detector = ZoomDetector(config)
        detector.on_meeting_change(my_callback)
        await detector.start()
        # ... 감지 루프 실행 중 ...
        await detector.stop()
    """

    def __init__(self, config: AppConfig | None = None) -> None:
        """ZoomDetector를 초기화한다.

        Args:
            config: 애플리케이션 설정 (None이면 get_config() 사용)
        """
        self._config = config or get_config()

        # Zoom 설정 로드
        self._process_name: str = self._config.zoom.process_name
        self._poll_interval: int = self._config.zoom.poll_interval_seconds

        # 상태 관리
        self._is_meeting_active: bool = False
        self._is_running: bool = False
        self._poll_task: asyncio.Task[None] | None = None

        # 이벤트 (asyncio.Event)
        self.meeting_started_event: asyncio.Event = asyncio.Event()
        self.meeting_ended_event: asyncio.Event = asyncio.Event()

        # 콜백 목록
        self._sync_callbacks: list[SyncCallback] = []
        self._async_callbacks: list[AsyncCallback] = []

        logger.info(
            f"ZoomDetector 초기화: "
            f"process_name={self._process_name}, "
            f"poll_interval={self._poll_interval}초"
        )

    @property
    def is_meeting_active(self) -> bool:
        """현재 Zoom 미팅이 진행 중인지 반환한다."""
        return self._is_meeting_active

    @property
    def is_running(self) -> bool:
        """감지 루프가 실행 중인지 반환한다."""
        return self._is_running

    def on_meeting_change(self, callback: SyncCallback | AsyncCallback) -> None:
        """미팅 상태 변화 콜백을 등록한다.

        콜백은 미팅 시작 시 True, 종료 시 False를 인자로 받는다.
        동기 함수와 비동기 코루틴 모두 지원한다.

        Args:
            callback: 미팅 상태 변화 시 호출될 함수 또는 코루틴
        """
        cb_name = getattr(callback, "__name__", repr(callback))
        if asyncio.iscoroutinefunction(callback):
            self._async_callbacks.append(callback)  # type: ignore[arg-type]
        else:
            self._sync_callbacks.append(callback)  # type: ignore[arg-type]
        logger.debug(f"미팅 상태 변화 콜백 등록: {cb_name}")

    async def _check_zoom_process(self) -> bool:
        """pgrep으로 Zoom 미팅 프로세스 존재 여부를 확인한다.

        pgrep -f <process_name> 명령으로 프로세스를 검색한다.
        반환 코드 0이면 프로세스 존재, 1이면 미존재.

        Returns:
            프로세스 존재 시 True, 미존재 시 False

        Raises:
            ProcessCheckError: pgrep 실행 자체가 실패한 경우
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep",
                "-f",
                self._process_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            returncode = await asyncio.wait_for(proc.wait(), timeout=10.0)
            # pgrep: 0 = 매칭 프로세스 존재, 1 = 미존재
            return returncode == 0
        except TimeoutError:
            logger.warning("pgrep 명령 타임아웃 (10초 초과)")
            # 타임아웃된 pgrep 프로세스를 강제 종료하여 고아 프로세스 방지
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass  # 이미 종료된 경우 무시
            # 타임아웃 시 이전 상태 유지 (안전한 기본값)
            return self._is_meeting_active
        except FileNotFoundError as e:
            raise ProcessCheckError(
                "pgrep 명령을 찾을 수 없습니다. macOS 환경을 확인하세요."
            ) from e
        except OSError as e:
            raise ProcessCheckError(
                f"프로세스 확인 중 OS 에러 발생: {e}",
                original_error=e,
            ) from e

    async def _notify_callbacks(self, meeting_started: bool) -> None:
        """등록된 콜백들에 미팅 상태 변화를 알린다.

        콜백 실행 중 발생하는 에러는 로깅하고 무시한다.
        (콜백 에러가 감지 루프를 중단시키지 않도록)

        Args:
            meeting_started: True면 미팅 시작, False면 미팅 종료
        """
        # 동기 콜백 실행
        for cb in self._sync_callbacks:
            try:
                cb(meeting_started)
            except Exception as e:
                cb_name = getattr(cb, "__name__", repr(cb))
                logger.error(f"동기 콜백 실행 에러 ({cb_name}): {e}")

        # 비동기 콜백 실행
        for cb in self._async_callbacks:
            try:
                await cb(meeting_started)
            except Exception as e:
                cb_name = getattr(cb, "__name__", repr(cb))
                logger.error(f"비동기 콜백 실행 에러 ({cb_name}): {e}")

    async def _handle_state_change(self, is_active: bool) -> None:
        """미팅 상태 변화를 처리한다.

        이전 상태와 다른 경우에만 이벤트를 발행하고 콜백을 호출한다.
        (중복 이벤트 방지)

        Args:
            is_active: 현재 미팅 활성 상태
        """
        if is_active == self._is_meeting_active:
            return  # 상태 변화 없음

        self._is_meeting_active = is_active

        if is_active:
            # 미팅 시작
            logger.info("Zoom 미팅 시작 감지")
            self.meeting_started_event.set()
            self.meeting_ended_event.clear()
            await self._notify_callbacks(True)
        else:
            # 미팅 종료
            logger.info("Zoom 미팅 종료 감지")
            self.meeting_ended_event.set()
            self.meeting_started_event.clear()
            await self._notify_callbacks(False)

    async def _poll_loop(self) -> None:
        """Zoom 프로세스 감지 폴링 루프.

        지정된 간격(poll_interval_seconds)으로 프로세스 존재 여부를
        반복 확인하고, 상태 변화 시 이벤트를 발행한다.
        """
        logger.info(
            f"감지 루프 시작: {self._poll_interval}초 간격으로 "
            f"'{self._process_name}' 프로세스 감시"
        )

        # 연속 에러 카운터 (과도한 로깅 방지 및 백오프)
        consecutive_errors: int = 0
        max_logged_errors: int = 5  # 이 횟수 초과 시 로그 레벨 낮춤
        # 연속 에러 시 폴링 간격 배수 상한 (원래 간격의 최대 6배)
        max_backoff_multiplier: int = 6

        while self._is_running:
            try:
                is_active = await self._check_zoom_process()
                await self._handle_state_change(is_active)

                # 프로세스 확인 성공 시 에러 카운터 초기화
                if consecutive_errors > 0:
                    logger.info(f"프로세스 확인 복구 (연속 {consecutive_errors}회 에러 후)")
                    consecutive_errors = 0

            except ProcessCheckError as e:
                consecutive_errors += 1
                if consecutive_errors <= max_logged_errors:
                    logger.error(f"프로세스 확인 실패 ({consecutive_errors}회 연속): {e}")
                elif consecutive_errors == max_logged_errors + 1:
                    logger.error("프로세스 확인 반복 실패. 이후 에러는 DEBUG 레벨로 기록합니다.")
                else:
                    logger.debug(f"프로세스 확인 실패 ({consecutive_errors}회 연속): {e}")

            except asyncio.CancelledError:
                logger.info("감지 루프 취소됨")
                break

            # 연속 에러 시 폴링 간격을 점진적으로 늘려 리소스 낭비 방지
            backoff_multiplier = min(1 + consecutive_errors, max_backoff_multiplier)
            sleep_interval = self._poll_interval * backoff_multiplier

            # 다음 폴링까지 대기
            try:
                await asyncio.sleep(sleep_interval)
            except asyncio.CancelledError:
                logger.info("감지 루프 대기 중 취소됨")
                break

    async def start(self) -> None:
        """Zoom 프로세스 감지를 시작한다.

        비동기 폴링 태스크를 생성하여 백그라운드에서 실행한다.

        Raises:
            AlreadyRunningError: 이미 감지가 실행 중인 경우
        """
        if self._is_running:
            raise AlreadyRunningError("Zoom 감지기가 이미 실행 중입니다.")

        self._is_running = True

        # 초기 상태 확인
        # 회귀 방지: 앱 시작 시 Zoom 회의가 이미 진행 중이면 _handle_state_change 로
        # 시작 콜백을 발화해야 자동 녹음이 트리거된다. 단순히 _is_meeting_active 를
        # 직접 True 로 설정하면 폴링 루프의 단락(if is_active == self._is_meeting_active)
        # 때문에 콜백이 영원히 호출되지 않는 버그가 있었다.
        try:
            initial_active = await self._check_zoom_process()
            if initial_active:
                # 기본값 self._is_meeting_active=False 와 다르므로 _handle_state_change 가
                # 시작 콜백(_notify_callbacks(True)) 을 호출한다.
                logger.info("초기 상태: Zoom 미팅 진행 중 (시작 콜백 호출)")
                await self._handle_state_change(True)
            else:
                # 초기에 활성 아님 → 상태 전이 없으므로 콜백 미호출이 정상.
                # 다만 종료 이벤트는 명시적으로 설정해 대기 중인 코드가 즉시 진행되도록 한다.
                self.meeting_ended_event.set()
                logger.info("초기 상태: Zoom 미팅 없음")
        except ProcessCheckError as e:
            logger.warning(f"초기 프로세스 확인 실패 (감지는 계속 시도): {e}")

        # 폴링 태스크 시작
        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name="zoom_detector_poll",
        )
        logger.info("Zoom 감지기 시작")

    async def stop(self) -> None:
        """Zoom 프로세스 감지를 중지한다.

        실행 중인 폴링 태스크를 취소하고 정리한다.
        이미 중지된 상태에서 호출해도 에러 없이 무시한다.
        """
        if not self._is_running:
            logger.debug("Zoom 감지기가 이미 중지 상태입니다.")
            return

        self._is_running = False

        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None

        logger.info("Zoom 감지기 중지")

    async def check_once(self) -> bool:
        """Zoom 미팅 상태를 1회 확인한다.

        폴링 루프와 독립적으로 현재 상태를 즉시 확인할 때 사용한다.

        Returns:
            미팅 진행 중이면 True, 아니면 False

        Raises:
            ProcessCheckError: pgrep 실행 실패 시
        """
        is_active = await self._check_zoom_process()
        await self._handle_state_change(is_active)
        return is_active

    def reset_events(self) -> None:
        """이벤트 상태를 초기화한다. 테스트 용도."""
        self.meeting_started_event.clear()
        self.meeting_ended_event.clear()
        self._is_meeting_active = False
