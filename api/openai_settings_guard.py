"""OpenAI 자격 증명과 기본 provider 변경을 직렬화하는 API 잠금."""

from __future__ import annotations

import asyncio

from fastapi import Request


def get_openai_settings_mutation_lock(request: Request) -> asyncio.Lock:
    """현재 서버 이벤트 루프에 바인딩된 설정/키 공용 잠금을 반환한다."""
    loop = asyncio.get_running_loop()
    state = request.app.state
    lock = getattr(state, "openai_settings_mutation_lock", None)
    lock_loop = getattr(state, "openai_settings_mutation_lock_loop", None)
    if not isinstance(lock, asyncio.Lock) or lock_loop is not loop:
        lock = asyncio.Lock()
        state.openai_settings_mutation_lock = lock
        state.openai_settings_mutation_lock_loop = loop
    return lock
