"""FastAPI app.state 의존성 접근 헬퍼.

라우터가 `request.app.state` 구조를 직접 알지 않도록, 공통 런타임 객체 접근과
503 에러 메시지를 한 곳에 모은다.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, cast

from fastapi import HTTPException, Request

from core.meeting_mutation import MeetingMutationCoordinator

logger = logging.getLogger(__name__)


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
    return cast(Path, get_config(request).paths.resolved_outputs_dir)


def get_job_queue(request: Request) -> Any:
    """AsyncJobQueue 를 반환한다."""
    return require_state(request, "job_queue", "작업 큐가 초기화되지 않았습니다.")


def get_search_engine(request: Request) -> Any:
    """HybridSearchEngine 을 반환하며, 필요하면 요청 시점에 초기화한다."""
    state = request.app.state
    engine = getattr(state, "search_engine", None)
    if engine is not None:
        return engine

    lock = getattr(state, "search_engine_lock", None)
    if lock is None:
        lock = threading.Lock()
        state.search_engine_lock = lock

    with lock:
        engine = getattr(state, "search_engine", None)
        if engine is not None:
            return engine
        try:
            from search.hybrid_search import HybridSearchEngine

            engine = HybridSearchEngine(config=get_config(request))
            state.search_engine = engine
            logger.info("HybridSearchEngine 지연 초기화 완료")
            return engine
        except Exception as e:
            state.search_engine = None
            logger.warning(f"HybridSearchEngine 지연 초기화 실패: {e}")
            raise HTTPException(
                status_code=503,
                detail="검색 엔진이 초기화되지 않았습니다.",
            ) from e


def get_chat_engine(request: Request) -> Any:
    """ChatEngine 을 반환하며, 필요하면 요청 시점에 초기화한다."""
    state = request.app.state
    engine = getattr(state, "chat_engine", None)
    if engine is not None:
        return engine

    lock = getattr(state, "chat_engine_lock", None)
    if lock is None:
        lock = threading.Lock()
        state.chat_engine_lock = lock

    with lock:
        engine = getattr(state, "chat_engine", None)
        if engine is not None:
            return engine
        try:
            from search.chat import ChatEngine

            engine = ChatEngine(
                config=get_config(request), search_engine=get_search_engine(request)
            )
            state.chat_engine = engine
            logger.info("ChatEngine 지연 초기화 완료")
            return engine
        except HTTPException:
            state.chat_engine = None
            raise
        except Exception as e:
            state.chat_engine = None
            logger.warning(f"ChatEngine 지연 초기화 실패: {e}")
            raise HTTPException(
                status_code=503,
                detail="Chat 엔진이 초기화되지 않았습니다.",
            ) from e


def get_pipeline_manager(request: Request) -> Any:
    """PipelineManager 를 반환한다."""
    return require_state(request, "pipeline_manager", "파이프라인이 초기화되지 않았습니다.")


def get_meeting_mutation_coordinator(request: Request) -> MeetingMutationCoordinator:
    """앱과 PipelineManager가 공유하는 회의별 mutation coordinator를 반환한다."""
    coordinator = getattr(request.app.state, "meeting_mutation_coordinator", None)
    if coordinator is None:
        pipeline = getattr(request.app.state, "pipeline_manager", None)
        pipeline_coordinator = getattr(pipeline, "meeting_mutation_coordinator", None)
        if isinstance(pipeline_coordinator, MeetingMutationCoordinator):
            coordinator = pipeline_coordinator
    if coordinator is None:
        # bare FastAPI 단위 테스트도 production과 같은 dependency 계약을 사용한다.
        coordinator = MeetingMutationCoordinator()
        request.app.state.meeting_mutation_coordinator = coordinator
    if not isinstance(coordinator, MeetingMutationCoordinator):
        raise HTTPException(
            status_code=503,
            detail="회의 mutation coordinator가 초기화되지 않았습니다.",
        )
    return coordinator


def get_recorder(request: Request) -> Any:
    """AudioRecorder 를 반환한다."""
    return require_state(request, "recorder", "녹음 기능이 초기화되지 않았습니다.")
