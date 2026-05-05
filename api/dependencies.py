"""FastAPI app.state 의존성 접근 헬퍼.

라우터가 `request.app.state` 구조를 직접 알지 않도록, 공통 런타임 객체 접근과
503 에러 메시지를 한 곳에 모은다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request


def require_state(request: Request, name: str, detail: str) -> Any:
    """app.state 의 필수 객체를 반환한다.

    Args:
        request: FastAPI Request 객체.
        name: app.state 속성명.
        detail: 객체가 없을 때 사용자에게 반환할 에러 메시지.

    Raises:
        HTTPException: 객체가 없으면 503.
    """
    value = getattr(request.app.state, name, None)
    if value is None:
        raise HTTPException(status_code=503, detail=detail)
    return value


def get_config(request: Request) -> Any:
    """AppConfig 를 반환한다."""
    return require_state(request, "config", "서버 설정이 초기화되지 않았습니다.")


def get_outputs_dir(request: Request) -> Path:
    """설정에서 outputs 디렉토리 경로를 반환한다."""
    return get_config(request).paths.resolved_outputs_dir


def get_job_queue(request: Request) -> Any:
    """AsyncJobQueue 를 반환한다."""
    return require_state(request, "job_queue", "작업 큐가 초기화되지 않았습니다.")


def get_search_engine(request: Request) -> Any:
    """HybridSearchEngine 을 반환한다."""
    return require_state(request, "search_engine", "검색 엔진이 초기화되지 않았습니다.")


def get_chat_engine(request: Request) -> Any:
    """ChatEngine 을 반환한다."""
    return require_state(request, "chat_engine", "Chat 엔진이 초기화되지 않았습니다.")


def get_pipeline_manager(request: Request) -> Any:
    """PipelineManager 를 반환한다."""
    return require_state(request, "pipeline_manager", "파이프라인이 초기화되지 않았습니다.")


def get_recorder(request: Request) -> Any:
    """AudioRecorder 를 반환한다."""
    return require_state(request, "recorder", "녹음 기능이 초기화되지 않았습니다.")
