"""녹음 저장 장애를 macOS 알림 센터에 전달한다."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys

logger = logging.getLogger(__name__)


def _notify() -> None:
    """회의 정보가 없는 고정 안내를 보내고 OS 알림 실패를 기록한다."""
    try:
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                'display notification "녹음 저장이 중단됐습니다. Recap에서 녹음 상태를 확인하세요." '
                'with title "Recap"',
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"macOS 녹음 중단 알림을 보내지 못했습니다: {type(exc).__name__}")


def notify_recording_failure() -> None:
    """녹음 정리와 화면 알림이 OS 알림 전달을 기다리지 않도록 한다."""
    if sys.platform == "darwin":
        asyncio.get_running_loop().run_in_executor(None, _notify)
