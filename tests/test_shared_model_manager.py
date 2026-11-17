"""실제 모델 로드 없이 API·파이프라인·검색·채팅의 매니저 동일성을 검증한다."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from starlette.requests import Request

from api.dependencies import get_chat_engine, get_model_manager, get_search_engine
from api.routers.search_chat import _build_hybrid_chat_service
from api.server import create_app
from config import AppConfig, PathsConfig


def _manager() -> MagicMock:
    """실제 모델 로드를 금지하고 관측 가능한 native cleanup 상태를 제공한다."""
    manager = MagicMock()
    manager.acquire.side_effect = AssertionError("테스트에서 실제 모델을 로드하면 안 됩니다.")
    manager.get_status.return_value = {
        "current_model_name": "wiki-llm",
        "native_cleanup_pending": True,
        "native_cleanup_model_name": "wiki-llm",
        "native_cleanup_pending_workers": 2,
        "native_cleanup_started_at": 123.0,
    }
    return manager


def test_startup과_검색_채팅_Wiki_API가_같은_매니저를_사용한다(tmp_path: Path) -> None:
    """app.state 누락으로 Wiki LLM과 리소스 관측이 비활성화되는 회귀를 막는다."""
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    config.wiki.enabled = True
    config.wiki.router_enabled = True
    config.wiki.router_llm_fallback = True
    config.wiki.root = tmp_path / "wiki"
    manager = _manager()
    app = create_app(config, runtime_profile="api-test")
    with (
        patch("core.model_manager.get_model_manager", return_value=manager),
        patch("search.hybrid_search.HybridSearchEngine") as search_cls,
        patch("search.chat.ChatEngine") as chat_cls,
        TestClient(app) as client,
    ):
        assert app.state.model_manager is manager
        request = Request({"type": "http", "app": app, "headers": []})
        search = get_search_engine(request)
        chat = get_chat_engine(request)
        assert search_cls.call_args.kwargs["model_manager"] is manager
        assert chat_cls.call_args.kwargs["model_manager"] is manager
        assert chat_cls.call_args.kwargs["search_engine"] is search
        service = _build_hybrid_chat_service(request, chat)
        assert service._wiki_llm is not None
        assert service._wiki_llm._model_manager is manager
        response = client.get("/api/system/resources")
        assert response.status_code == 200
        body = response.json()
        assert body["loaded_model"] == "wiki-llm"
        assert body["native_cleanup_pending"] is True
        assert body["native_cleanup_pending_workers"] == 2
        manager.acquire.assert_not_called()


def test_직접주입한_공유매니저는_startup에서_교체하지_않는다(tmp_path: Path) -> None:
    """기존 런타임·테스트의 명시적 매니저 주입을 보존한다."""
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    app = create_app(config, runtime_profile="api-test")
    manager = _manager()
    app.state.model_manager = manager
    with patch("core.model_manager.get_model_manager") as factory, TestClient(app):
        assert app.state.model_manager is manager
        factory.assert_not_called()
        manager.acquire.assert_not_called()


def test_수동앱구성도_기존_파이프라인_매니저를_재사용한다() -> None:
    """리소스 API가 이미 실행 중인 파이프라인과 다른 싱글턴을 만들지 않는다."""
    manager = _manager()
    state = SimpleNamespace(pipeline_manager=SimpleNamespace(_model_manager=manager))
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    with patch("core.model_manager.get_model_manager") as factory:
        assert get_model_manager(request) is manager
        assert state.model_manager is manager
        factory.assert_not_called()
        manager.acquire.assert_not_called()
