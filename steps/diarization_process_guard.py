"""
Zoom 보호용 화자분리 worker 프로세스 제어 모듈.

화자분리(pyannote)는 CPU를 오래 점유하므로 Zoom 회의 중에는 별도 worker
프로세스를 멈춰 macOS 스케줄러가 Zoom에 CPU를 우선 배정하도록 한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from typing import Protocol

from steps.zoom_activity import ZoomAudioActivityChecker

logger = logging.getLogger(__name__)


class ProcessLike(Protocol):
    """subprocess.Popen 중 제어에 필요한 최소 인터페이스."""

    pid: int

    def poll(self) -> int | None: ...

    def kill(self) -> None: ...


class ZoomPauseGuard:
    """Zoom 회의 중 worker 프로세스를 일시정지/재개한다."""

    def __init__(
        self,
        process_name: str,
        poll_interval_seconds: float,
        prefer_coreaudio: bool = True,
        activity_checker: ZoomAudioActivityChecker | None = None,
    ) -> None:
        self._process_name = process_name
        self._poll_interval_seconds = poll_interval_seconds
        self._activity_checker = activity_checker or ZoomAudioActivityChecker(
            process_name=process_name,
            prefer_coreaudio=prefer_coreaudio,
        )

    async def is_zoom_active(self) -> bool:
        """Zoom 회의 오디오 활동이 있는지 확인한다."""
        try:
            result = await self._activity_checker.check()
            logger.debug(
                f"Zoom 보호 활동 확인: active={result.active}, "
                f"source={result.source}, processes={result.process_count}"
            )
            return result.active
        except Exception as e:
            logger.warning(f"Zoom 상태 확인 실패. 안전하게 active 로 간주합니다: {e}")
            return True

    async def wait_until_idle(self) -> None:
        """Zoom 회의가 끝날 때까지 worker 시작을 미룬다."""
        logged = False
        while await self.is_zoom_active():
            if not logged:
                logger.info("Zoom 회의 감지: 화자분리 worker 시작을 회의 종료 후로 연기합니다.")
                logged = True
            await asyncio.sleep(self._poll_interval_seconds)

    def pause(self, pid: int) -> None:
        """worker 프로세스를 멈춘다."""
        os.kill(pid, signal.SIGSTOP)

    def resume(self, pid: int) -> None:
        """worker 프로세스를 재개한다."""
        os.kill(pid, signal.SIGCONT)

    async def supervise(self, process: ProcessLike, timeout_seconds: int) -> int:
        """worker를 감시하고 Zoom active 동안 일시정지한다.

        타임아웃은 worker가 실제로 실행 중인 시간만 센다. Zoom 때문에 멈춘
        시간은 제외해 긴 회의 중 불필요한 timeout 실패를 피한다.
        """
        paused = False
        active_elapsed = 0.0
        last_tick = time.monotonic()

        while True:
            now = time.monotonic()
            if not paused:
                active_elapsed += now - last_tick
            last_tick = now

            returncode = process.poll()
            if returncode is not None:
                if paused:
                    try:
                        self.resume(process.pid)
                    except OSError:
                        pass
                return returncode

            zoom_active = await self.is_zoom_active()
            if zoom_active and not paused:
                try:
                    self.pause(process.pid)
                except OSError:
                    if process.poll() is not None:
                        return process.poll() or 0
                    raise
                paused = True
                logger.info("Zoom 회의 시작 감지: 화자분리 worker 일시정지")
            elif not zoom_active and paused:
                try:
                    self.resume(process.pid)
                except OSError:
                    if process.poll() is not None:
                        return process.poll() or 0
                    raise
                paused = False
                logger.info("Zoom 회의 종료 감지: 화자분리 worker 재개")

            if not paused and active_elapsed > timeout_seconds:
                process.kill()
                raise TimeoutError(
                    f"화자분리 worker 시간이 초과되었습니다 ({timeout_seconds}초)."
                )

            await asyncio.sleep(self._poll_interval_seconds)


def terminate_process(process: subprocess.Popen[object]) -> None:
    """남아 있는 worker 프로세스를 정리한다."""
    if process.poll() is not None:
        return
    try:
        process.kill()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.warning("화자분리 worker 종료 대기 타임아웃")
    except OSError:
        return
