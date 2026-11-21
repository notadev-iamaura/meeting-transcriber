"""Mac의 로컬 시각에 따라 예약 처리의 시작과 마감을 판정한다."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from config import AutoProcessingConfig

T = TypeVar("T")
NIGHTLY_ACTIONS = {"nightly_full", "nightly_transcribe"}
_in_nightly_run = ContextVar("in_nightly_run", default=False)


def current_window(
    config: AutoProcessingConfig, now: datetime
) -> tuple[datetime, datetime] | None:
    """시작은 포함하고 마감은 제외한다. 자정을 넘기는 일정도 시작일에 귀속한다."""
    if not config.enabled or config.run_at == config.stop_at:
        return None
    start = datetime.combine(now.date(), time.fromisoformat(config.run_at))
    end = datetime.combine(now.date(), time.fromisoformat(config.stop_at))
    if end < start:
        if now < end:
            start -= timedelta(days=1)
        else:
            end += timedelta(days=1)
    return (start, end) if start <= now < end else None


class NightlyWindowClosed(asyncio.CancelledError):
    """야간 시간 만료를 사용자의 취소 및 앱 종료와 구분한다."""


async def finish_nightly_write(write: Coroutine[Any, Any, T]) -> T:
    """이미 시작한 WAV·검색 저장은 실제 완료나 복구까지 회의의 쓰기 잠금을 유지한다."""
    if not _in_nightly_run.get():
        return await write
    return await finish_started_write(write)


async def finish_started_write(write: Coroutine[Any, Any, T]) -> T:
    """취소돼도 이미 시작한 쓰기가 끝날 때까지 호출자가 소유한 회의 잠금을 보존한다."""
    task = asyncio.create_task(write)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


async def run_during_window(
    run: Callable[[], Coroutine[Any, Any, T]], seconds_left: Callable[[], float]
) -> T:
    """절전에서 복귀한 뒤에도 벽시계를 다시 확인하고 실행 중인 작업 정리를 기다린다."""
    if seconds_left() <= 0:
        raise NightlyWindowClosed
    token = _in_nightly_run.set(True)
    try:
        task = asyncio.create_task(run())
    finally:
        _in_nightly_run.reset(token)
    try:
        while not task.done():
            remaining = seconds_left()
            if remaining <= 0:
                raise NightlyWindowClosed
            await asyncio.wait({task}, timeout=min(remaining, 1.0))
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
