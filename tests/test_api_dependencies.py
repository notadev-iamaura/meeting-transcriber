"""api.dependencies — app.state 의존성 접근 헬퍼 테스트."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException


def _request_with_state(**values: object) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**values)))


def test_get_job_queue_returns_state_value() -> None:
    from api import dependencies

    queue = object()
    request = _request_with_state(job_queue=queue)

    assert dependencies.get_job_queue(request) is queue


def test_get_job_queue_raises_503_when_missing() -> None:
    from api import dependencies

    request = _request_with_state()

    with pytest.raises(HTTPException) as exc_info:
        dependencies.get_job_queue(request)

    assert exc_info.value.status_code == 503
    assert "작업 큐" in exc_info.value.detail


def test_get_outputs_dir_returns_config_path() -> None:
    from api import dependencies

    outputs_dir = object()
    config = SimpleNamespace(paths=SimpleNamespace(resolved_outputs_dir=outputs_dir))
    request = _request_with_state(config=config)

    assert dependencies.get_outputs_dir(request) is outputs_dir


def test_get_search_engine_lazily_creates_and_caches() -> None:
    from api import dependencies

    config = object()
    engine = object()
    request = _request_with_state(config=config, search_engine=None)

    with patch("search.hybrid_search.HybridSearchEngine", return_value=engine) as search_cls:
        assert dependencies.get_search_engine(request) is engine
        assert dependencies.get_search_engine(request) is engine

    search_cls.assert_called_once_with(config=config)


def test_get_search_engine_creation_failure_raises_503() -> None:
    from api import dependencies

    request = _request_with_state(config=object(), search_engine=None)

    with (
        patch("search.hybrid_search.HybridSearchEngine", side_effect=RuntimeError("boom")),
        pytest.raises(HTTPException) as exc_info,
    ):
        dependencies.get_search_engine(request)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "검색 엔진이 초기화되지 않았습니다."
    assert request.app.state.search_engine is None


def test_get_chat_engine_lazily_uses_existing_search_engine() -> None:
    from api import dependencies

    config = object()
    search_engine = object()
    chat_engine = object()
    request = _request_with_state(
        config=config,
        search_engine=search_engine,
        chat_engine=None,
    )

    with patch("search.chat.ChatEngine", return_value=chat_engine) as chat_cls:
        assert dependencies.get_chat_engine(request) is chat_engine
        assert dependencies.get_chat_engine(request) is chat_engine

    chat_cls.assert_called_once_with(config=config, search_engine=search_engine)


def test_get_chat_engine_creates_search_engine_when_missing() -> None:
    from api import dependencies

    config = object()
    search_engine = object()
    chat_engine = object()
    request = _request_with_state(config=config, search_engine=None, chat_engine=None)

    with (
        patch("search.hybrid_search.HybridSearchEngine", return_value=search_engine) as search_cls,
        patch("search.chat.ChatEngine", return_value=chat_engine) as chat_cls,
    ):
        assert dependencies.get_chat_engine(request) is chat_engine

    search_cls.assert_called_once_with(config=config)
    chat_cls.assert_called_once_with(config=config, search_engine=search_engine)


def test_get_chat_engine_creation_failure_raises_503() -> None:
    from api import dependencies

    request = _request_with_state(
        config=object(),
        search_engine=MagicMock(),
        chat_engine=None,
    )

    with (
        patch("search.chat.ChatEngine", side_effect=RuntimeError("boom")),
        pytest.raises(HTTPException) as exc_info,
    ):
        dependencies.get_chat_engine(request)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Chat 엔진이 초기화되지 않았습니다."
    assert request.app.state.chat_engine is None
