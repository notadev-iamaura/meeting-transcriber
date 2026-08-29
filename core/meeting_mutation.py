"""회의별 산출물 mutation을 직렬화하는 프로세스 로컬 coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _LeaseState:
    """단일 회의 lock과 현재 task의 재진입 상태."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: asyncio.Task[Any] | None = None
    depth: int = 0


class MeetingMutationCoordinator:
    """같은 회의의 편집·재전사·삭제·지연 LLM을 한 번에 하나만 실행한다.

    앱은 파일 기반 single-instance 계약으로 한 프로세스만 실행되고, FastAPI와
    scheduler는 같은 asyncio event loop를 사용한다. `run_llm_steps()`가 API
    background wrapper 안에서 같은 lease를 다시 요청할 수 있어 task 단위 재진입을
    지원한다. 다른 task의 동일 meeting lease는 FIFO `asyncio.Lock`에서 대기한다.
    """

    def __init__(self) -> None:
        self._states: dict[str, _LeaseState] = {}

    def _state_for(self, meeting_id: str) -> _LeaseState:
        """회의 ID에 대응하는 process-local lease 상태를 반환한다."""
        state = self._states.get(meeting_id)
        if state is None:
            state = _LeaseState()
            self._states[meeting_id] = state
        return state

    @asynccontextmanager
    async def lease(self, meeting_id: str) -> AsyncIterator[None]:
        """현재 task가 회의별 mutation lease를 보유하는 동안 제어를 넘긴다."""
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("회의 mutation lease는 asyncio task 안에서만 사용할 수 있습니다")

        state = self._state_for(meeting_id)
        if state.owner is current_task:
            state.depth += 1
            try:
                yield
            finally:
                state.depth -= 1
            return

        await state.lock.acquire()
        if state.owner is not None or state.depth != 0:
            state.lock.release()
            raise RuntimeError("회의 mutation lease 소유권 상태가 일치하지 않습니다")
        state.owner = current_task
        state.depth = 1
        try:
            yield
        finally:
            if state.owner is not current_task or state.depth != 1:
                raise RuntimeError("회의 mutation lease 재진입 깊이가 일치하지 않습니다")
            state.depth = 0
            state.owner = None
            state.lock.release()

    def locked(self, meeting_id: str) -> bool:
        """회의별 lease가 현재 보유되었는지 반환한다."""
        state = self._states.get(meeting_id)
        return bool(state is not None and state.lock.locked())
