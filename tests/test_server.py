"""
FastAPI 백엔드 서버 테스트 모듈 (FastAPI Backend Server Test Module)

목적: api/server.py의 FastAPI 서버 기능을 검증한다.
주요 테스트:
    - 앱 생성 및 설정 확인
    - lifespan 이벤트 (startup/shutdown)
    - 헬스체크 엔드포인트 (/api/health)
    - CORS 미들웨어 설정
    - 정적 파일 서빙
    - 글로벌 예외 핸들러
의존성: pytest, httpx, fastapi (TestClient)
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from config import AppConfig, ServerConfig

# === 헬퍼 함수 ===


def _make_test_config(tmp_path: Path) -> AppConfig:
    """테스트용 AppConfig를 생성한다.

    Args:
        tmp_path: pytest 임시 디렉토리

    Returns:
        테스트용 AppConfig 인스턴스
    """
    from config import PathsConfig

    return AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
    )


# === TestAppCreation ===


class TestAppCreation:
    """앱 팩토리 및 기본 설정 테스트."""

    def test_create_app_기본_설정(self, tmp_path: Path) -> None:
        """create_app이 FastAPI 인스턴스를 올바르게 생성하는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        assert app.title == "회의 전사 시스템 API"
        assert app.version == "0.1.0"

    def test_create_app_config_state에_저장(self, tmp_path: Path) -> None:
        """create_app 후 app.state.config에 설정이 저장되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        assert app.state.config is config
        assert app.state.config.server.port == 8765

    def test_create_app_docs_url_설정(self, tmp_path: Path) -> None:
        """API 문서 URL이 /api/ 하위에 설정되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        assert app.docs_url == "/api/docs"
        assert app.redoc_url == "/api/redoc"
        assert app.openapi_url == "/api/openapi.json"


# === TestHealthEndpoint ===


class TestHealthEndpoint:
    """헬스체크 엔드포인트 테스트."""

    def test_health_check_정상_응답(self, tmp_path: Path) -> None:
        """GET /api/health가 200 OK와 상태 정보를 반환하는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        with TestClient(app) as client:
            response = client.get("/api/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["version"] == "0.1.0"
        assert "uptime_seconds" in data

    def test_health_check_uptime_양수(self, tmp_path: Path) -> None:
        """서버 시작 후 uptime이 0 이상인지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        with TestClient(app) as client:
            response = client.get("/api/health")

        data = response.json()
        assert data["uptime_seconds"] >= 0.0

    def test_health_check_json_content_type(self, tmp_path: Path) -> None:
        """헬스체크 응답의 Content-Type이 JSON인지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        with TestClient(app) as client:
            response = client.get("/api/health")

        assert "application/json" in response.headers["content-type"]


# === TestCORS ===


class TestCORS:
    """CORS 미들웨어 설정 테스트."""

    def test_cors_localhost_허용(self, tmp_path: Path) -> None:
        """localhost 오리진에서의 요청이 허용되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        with TestClient(app) as client:
            response = client.options(
                "/api/health",
                headers={
                    "Origin": "http://localhost:8765",
                    "Access-Control-Request-Method": "GET",
                },
            )

        # CORS preflight 응답에 허용 오리진이 포함되어야 함
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "http://localhost:8765"

    def test_cors_127_0_0_1_허용(self, tmp_path: Path) -> None:
        """127.0.0.1 오리진에서의 요청이 허용되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        with TestClient(app) as client:
            response = client.options(
                "/api/health",
                headers={
                    "Origin": "http://127.0.0.1:8765",
                    "Access-Control-Request-Method": "GET",
                },
            )

        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "http://127.0.0.1:8765"

    def test_cors_외부_오리진_차단(self, tmp_path: Path) -> None:
        """외부 오리진에서의 요청이 차단되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        with TestClient(app) as client:
            response = client.options(
                "/api/health",
                headers={
                    "Origin": "http://evil.example.com",
                    "Access-Control-Request-Method": "GET",
                },
            )

        # 외부 오리진은 access-control-allow-origin 헤더가 없어야 함
        assert response.headers.get("access-control-allow-origin") is None

    def test_cors_허용_메서드_제한(self, tmp_path: Path) -> None:
        """GET, POST, DELETE만 허용되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        with TestClient(app) as client:
            response = client.options(
                "/api/health",
                headers={
                    "Origin": "http://localhost:8765",
                    "Access-Control-Request-Method": "GET",
                },
            )

        allow_methods = response.headers.get("access-control-allow-methods", "")
        assert "GET" in allow_methods
        assert "POST" in allow_methods
        assert "DELETE" in allow_methods


# === TestStaticFiles ===


class TestStaticFiles:
    """정적 파일 서빙 테스트."""

    def test_정적_파일_디렉토리_존재시_마운트(
        self,
        tmp_path: Path,
    ) -> None:
        """ui/web/ 디렉토리가 존재하면 /static에 마운트되는지 확인한다."""
        from api.server import _STATIC_DIR, create_app

        config = _make_test_config(tmp_path)

        # 정적 파일 디렉토리와 테스트 파일 생성
        static_dir = _STATIC_DIR
        if not static_dir.is_dir():
            # 테스트용 임시 정적 디렉토리를 패치
            test_static = tmp_path / "static"
            test_static.mkdir()
            (test_static / "test.txt").write_text("hello", encoding="utf-8")

            with patch("api.server._STATIC_DIR", test_static):
                app = create_app(config)

            with TestClient(app) as client:
                response = client.get("/static/test.txt")

            assert response.status_code == 200
            assert response.text == "hello"
        else:
            # 실제 ui/web/ 존재 시 마운트 확인
            app = create_app(config)
            # 마운트 확인 (라우트 목록에 /static 존재)
            route_paths = [route.path for route in app.routes if hasattr(route, "path")]
            assert any("/static" in p for p in route_paths)

    def test_정적_파일_디렉토리_미존재시_경고(
        self,
        tmp_path: Path,
    ) -> None:
        """ui/web/ 디렉토리가 없으면 경고 로그만 출력하는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)

        # 존재하지 않는 경로로 패치
        nonexistent = tmp_path / "nonexistent_static"

        with patch("api.server._STATIC_DIR", nonexistent):
            app = create_app(config)

        # /static 라우트가 마운트되지 않아야 함
        route_paths = [getattr(route, "path", "") for route in app.routes]
        assert not any(p.startswith("/static") for p in route_paths)

    def test_정적_html_파일_서빙(self, tmp_path: Path) -> None:
        """HTML 정적 파일이 올바르게 서빙되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)

        # 테스트용 HTML 파일 생성
        test_static = tmp_path / "web"
        test_static.mkdir()
        html_content = "<html><body>테스트</body></html>"
        (test_static / "index.html").write_text(
            html_content,
            encoding="utf-8",
        )

        with patch("api.server._STATIC_DIR", test_static):
            app = create_app(config)

        with TestClient(app) as client:
            response = client.get("/static/index.html")

        assert response.status_code == 200
        assert "테스트" in response.text


# === TestLifespan ===


class TestLifespan:
    """lifespan 이벤트 (startup/shutdown) 테스트."""

    def test_startup시_job_queue_초기화(self, tmp_path: Path) -> None:
        """서버 시작 시 JobQueue가 초기화되고 app.state에 저장되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        with TestClient(app) as _client:
            # lifespan startup 완료 후 state 확인
            assert hasattr(app.state, "job_queue")
            assert app.state.job_queue is not None

    def test_startup시_start_time_설정(self, tmp_path: Path) -> None:
        """서버 시작 시 start_time이 설정되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        before = time.time()

        with TestClient(app) as _client:
            assert hasattr(app.state, "start_time")
            assert app.state.start_time >= before

    def test_shutdown시_job_queue_종료(self, tmp_path: Path) -> None:
        """서버 종료 시 JobQueue 연결이 닫히는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        with TestClient(app):
            queue = app.state.job_queue
            assert queue is not None

        # TestClient __exit__ 후 lifespan shutdown 실행됨
        # JobQueue 내부 conn이 None이 되어야 함
        assert queue.queue._conn is None

    def test_lifespan_db_파일_생성(self, tmp_path: Path) -> None:
        """lifespan startup 후 pipeline.db 파일이 생성되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        with TestClient(app):
            db_path = config.paths.resolved_pipeline_db
            assert db_path.exists()


# === TestLifespanPartialFailure ===


class TestLifespanPartialFailure:
    """lifespan 부분 초기화 실패 테스트.

    검색/Chat 엔진 초기화가 실패해도 서버가 정상 시작되는지 확인한다.
    """

    def test_search_engine_초기화_실패(self, tmp_path: Path) -> None:
        """HybridSearchEngine 초기화 실패 시 서버가 정상 시작되고 search_engine이 None인지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        with patch(
            "search.hybrid_search.HybridSearchEngine",
            side_effect=RuntimeError("검색 엔진 초기화 실패"),
        ):
            with TestClient(app) as client:
                # 서버가 정상 작동하는지 헬스체크로 확인
                response = client.get("/api/health")
                assert response.status_code == 200

                # search_engine이 None으로 설정되어야 함
                assert app.state.search_engine is None

    def test_chat_engine_초기화_실패(self, tmp_path: Path) -> None:
        """ChatEngine 초기화 실패 시 서버가 정상 시작되고 chat_engine이 None인지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        with patch(
            "search.chat.ChatEngine",
            side_effect=RuntimeError("Chat 엔진 초기화 실패"),
        ):
            with TestClient(app) as client:
                # 서버가 정상 작동하는지 헬스체크로 확인
                response = client.get("/api/health")
                assert response.status_code == 200

                # chat_engine이 None으로 설정되어야 함
                assert app.state.chat_engine is None

    def test_search_engine과_chat_engine_동시_실패(self, tmp_path: Path) -> None:
        """검색/Chat 엔진 모두 초기화 실패해도 서버가 정상 시작되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        with patch(
            "search.hybrid_search.HybridSearchEngine",
            side_effect=RuntimeError("검색 엔진 실패"),
        ), patch(
            "search.chat.ChatEngine",
            side_effect=RuntimeError("Chat 엔진 실패"),
        ):
            with TestClient(app) as client:
                response = client.get("/api/health")
                assert response.status_code == 200

                assert app.state.search_engine is None
                assert app.state.chat_engine is None


# === TestExceptionHandler ===


class TestExceptionHandler:
    """글로벌 예외 핸들러 테스트."""

    def test_처리되지_않은_예외_500_응답(self, tmp_path: Path) -> None:
        """처리되지 않은 예외 발생 시 500 JSON 응답을 반환하는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        # 의도적으로 예외를 발생시키는 엔드포인트 추가
        @app.get("/api/test-error")
        async def raise_error() -> None:
            """테스트용 예외 발생 엔드포인트."""
            raise RuntimeError("테스트 에러")

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/test-error")

        assert response.status_code == 500
        data = response.json()
        assert "error" in data
        assert data["error"] == "서버 내부 오류가 발생했습니다."

    def test_500_응답에_detail_미포함(self, tmp_path: Path) -> None:
        """500 에러 응답에 예외 상세 정보가 노출되지 않아야 한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        @app.get("/api/test-error-detail")
        async def raise_error() -> None:
            """테스트용 예외 발생 엔드포인트."""
            raise RuntimeError("내부 비밀 정보가 담긴 에러")

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/test-error-detail")

        assert response.status_code == 500
        data = response.json()
        # 응답에 detail 필드가 없어야 함 (내부 에러 정보 비노출)
        assert "detail" not in data
        # 예외 메시지 원문이 응답에 포함되지 않아야 함
        assert "내부 비밀 정보가 담긴 에러" not in str(data)

    def test_예외_응답_json_형식(self, tmp_path: Path) -> None:
        """예외 응답의 Content-Type이 JSON인지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        @app.get("/api/test-error-json")
        async def raise_error() -> None:
            """테스트용 예외 발생 엔드포인트."""
            raise ValueError("JSON 테스트")

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/test-error-json")

        assert "application/json" in response.headers["content-type"]


# === TestServerConfig ===


class TestServerConfig:
    """서버 설정 관련 테스트."""

    def test_커스텀_포트_설정(self, tmp_path: Path) -> None:
        """커스텀 포트가 설정에 반영되는지 확인한다."""
        from config import PathsConfig

        config = AppConfig(
            paths=PathsConfig(base_dir=str(tmp_path)),
            server=ServerConfig(host="127.0.0.1", port=9999),
        )

        from api.server import create_app

        app = create_app(config)

        assert app.state.config.server.port == 9999

    def test_호스트_127_0_0_1_설정(self, tmp_path: Path) -> None:
        """서버 호스트가 127.0.0.1로 고정되는지 확인한다."""
        config = _make_test_config(tmp_path)

        assert config.server.host == "127.0.0.1"

    def test_기본_로그_레벨_info(self) -> None:
        """기본 로그 레벨이 info인지 확인한다."""
        config = ServerConfig()

        assert config.log_level == "info"


# === TestNotFoundRoute ===


class TestNotFoundRoute:
    """존재하지 않는 경로 접근 테스트."""

    def test_존재하지_않는_API_경로_404(self, tmp_path: Path) -> None:
        """존재하지 않는 API 경로 접근 시 404를 반환하는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        with TestClient(app) as client:
            response = client.get("/api/nonexistent")

        assert response.status_code == 404


# === TestSPARouting ===


class TestSPARouting:
    """SPA 라우팅 테스트.

    /app 및 /app/{path} 요청에 index.html을 반환하는지 확인한다.
    """

    def test_app_루트_200(self, tmp_path: Path) -> None:
        """GET /app이 200 OK를 반환하는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)

        test_static = tmp_path / "web"
        test_static.mkdir()
        (test_static / "index.html").write_text(
            "<html><body>SPA</body></html>",
            encoding="utf-8",
        )

        with patch("api.server._STATIC_DIR", test_static):
            app = create_app(config)

        with TestClient(app) as client:
            response = client.get("/app")

        assert response.status_code == 200

    def test_app_viewer_경로_200(self, tmp_path: Path) -> None:
        """GET /app/viewer/123이 200 OK를 반환하는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)

        test_static = tmp_path / "web"
        test_static.mkdir()
        (test_static / "index.html").write_text("<html>SPA</html>", encoding="utf-8")

        with patch("api.server._STATIC_DIR", test_static):
            app = create_app(config)

        with TestClient(app) as client:
            response = client.get("/app/viewer/meeting-123")

        assert response.status_code == 200

    def test_app_chat_경로_200(self, tmp_path: Path) -> None:
        """GET /app/chat이 200 OK를 반환하는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)

        test_static = tmp_path / "web"
        test_static.mkdir()
        (test_static / "index.html").write_text("<html>SPA</html>", encoding="utf-8")

        with patch("api.server._STATIC_DIR", test_static):
            app = create_app(config)

        with TestClient(app) as client:
            response = client.get("/app/chat")

        assert response.status_code == 200

    def test_app_깊은_경로_200(self, tmp_path: Path) -> None:
        """GET /app/a/b/c가 200 OK를 반환하는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)

        test_static = tmp_path / "web"
        test_static.mkdir()
        (test_static / "index.html").write_text("<html>SPA</html>", encoding="utf-8")

        with patch("api.server._STATIC_DIR", test_static):
            app = create_app(config)

        with TestClient(app) as client:
            response = client.get("/app/a/b/c")

        assert response.status_code == 200

    def test_app_content_type_html(self, tmp_path: Path) -> None:
        """SPA 응답의 Content-Type이 HTML인지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)

        test_static = tmp_path / "web"
        test_static.mkdir()
        (test_static / "index.html").write_text("<html>SPA</html>", encoding="utf-8")

        with patch("api.server._STATIC_DIR", test_static):
            app = create_app(config)

        with TestClient(app) as client:
            response = client.get("/app")

        assert "text/html" in response.headers["content-type"]

    def test_api_경로_영향_없음(self, tmp_path: Path) -> None:
        """SPA 라우팅이 기존 API 경로에 영향을 주지 않는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)

        test_static = tmp_path / "web"
        test_static.mkdir()
        (test_static / "index.html").write_text("<html>SPA</html>", encoding="utf-8")

        with patch("api.server._STATIC_DIR", test_static):
            app = create_app(config)

        with TestClient(app) as client:
            response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_index_html_없을때_404(self, tmp_path: Path) -> None:
        """index.html이 존재하지 않으면 404를 반환하는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)

        # 디렉토리는 존재하지만 index.html이 없는 경우
        test_static = tmp_path / "web_empty"
        test_static.mkdir()

        with patch("api.server._STATIC_DIR", test_static):
            app = create_app(config)

        with TestClient(app) as client:
            response = client.get("/app")

        assert response.status_code == 404


# === TestLifespanOrchestration ===


class TestLifespanOrchestration:
    """lifespan 오케스트레이션 통합 테스트.

    ThermalManager, PipelineManager, FolderWatcher, JobProcessor가
    lifespan에서 올바르게 초기화/정리되는지 확인한다.
    모든 컴포넌트는 mock으로 대체하여 부작용을 방지한다.
    """

    def _get_orchestration_patches(self) -> dict[str, MagicMock]:
        """오케스트레이션 4개 컴포넌트의 공통 패치를 반환한다.

        Returns:
            패치에 사용할 mock 딕셔너리
        """
        # ThermalManager mock
        mock_thermal_cls = MagicMock()
        mock_thermal_instance = MagicMock()
        mock_thermal_cls.return_value = mock_thermal_instance

        # PipelineManager mock
        mock_pipeline_cls = MagicMock()
        mock_pipeline_instance = MagicMock()
        mock_pipeline_cls.return_value = mock_pipeline_instance

        # FolderWatcher mock
        mock_watcher_cls = MagicMock()
        mock_watcher_instance = MagicMock()
        mock_watcher_instance.start = AsyncMock()
        mock_watcher_instance.stop = AsyncMock()
        mock_watcher_instance.scan_existing = AsyncMock(return_value=[])
        mock_watcher_cls.return_value = mock_watcher_instance

        # JobProcessor mock
        mock_processor_cls = MagicMock()
        mock_processor_instance = MagicMock()
        mock_processor_instance.start = AsyncMock()
        mock_processor_instance.stop = AsyncMock()
        mock_processor_cls.return_value = mock_processor_instance

        return {
            "thermal_cls": mock_thermal_cls,
            "thermal": mock_thermal_instance,
            "pipeline_cls": mock_pipeline_cls,
            "pipeline": mock_pipeline_instance,
            "watcher_cls": mock_watcher_cls,
            "watcher": mock_watcher_instance,
            "processor_cls": mock_processor_cls,
            "processor": mock_processor_instance,
        }

    def test_thermal_manager_초기화(self, tmp_path: Path) -> None:
        """lifespan 후 app.state.thermal_manager가 존재하는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)
        mocks = self._get_orchestration_patches()

        with patch(
            "core.thermal_manager.ThermalManager",
            mocks["thermal_cls"],
        ), patch(
            "core.pipeline.PipelineManager",
            mocks["pipeline_cls"],
        ), patch(
            "core.watcher.FolderWatcher",
            mocks["watcher_cls"],
        ), patch(
            "core.orchestrator.JobProcessor",
            mocks["processor_cls"],
        ):
            with TestClient(app) as _client:
                assert hasattr(app.state, "thermal_manager")
                assert app.state.thermal_manager is not None

    def test_folder_watcher_초기화_및_시작(self, tmp_path: Path) -> None:
        """app.state.folder_watcher가 존재하고 start가 호출되었는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)
        mocks = self._get_orchestration_patches()

        with patch(
            "core.thermal_manager.ThermalManager",
            mocks["thermal_cls"],
        ), patch(
            "core.pipeline.PipelineManager",
            mocks["pipeline_cls"],
        ), patch(
            "core.watcher.FolderWatcher",
            mocks["watcher_cls"],
        ), patch(
            "core.orchestrator.JobProcessor",
            mocks["processor_cls"],
        ):
            with TestClient(app) as _client:
                assert hasattr(app.state, "folder_watcher")
                assert app.state.folder_watcher is not None
                mocks["watcher"].start.assert_called_once()

    def test_pipeline_manager_초기화(self, tmp_path: Path) -> None:
        """app.state.pipeline_manager가 존재하는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)
        mocks = self._get_orchestration_patches()

        with patch(
            "core.thermal_manager.ThermalManager",
            mocks["thermal_cls"],
        ), patch(
            "core.pipeline.PipelineManager",
            mocks["pipeline_cls"],
        ), patch(
            "core.watcher.FolderWatcher",
            mocks["watcher_cls"],
        ), patch(
            "core.orchestrator.JobProcessor",
            mocks["processor_cls"],
        ):
            with TestClient(app) as _client:
                assert hasattr(app.state, "pipeline_manager")
                assert app.state.pipeline_manager is not None

    def test_job_processor_초기화_및_시작(self, tmp_path: Path) -> None:
        """app.state.job_processor가 존재하고 start가 호출되었는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)
        mocks = self._get_orchestration_patches()

        with patch(
            "core.thermal_manager.ThermalManager",
            mocks["thermal_cls"],
        ), patch(
            "core.pipeline.PipelineManager",
            mocks["pipeline_cls"],
        ), patch(
            "core.watcher.FolderWatcher",
            mocks["watcher_cls"],
        ), patch(
            "core.orchestrator.JobProcessor",
            mocks["processor_cls"],
        ):
            with TestClient(app) as _client:
                assert hasattr(app.state, "job_processor")
                assert app.state.job_processor is not None
                mocks["processor"].start.assert_called_once()

    def test_shutdown시_job_processor_stop(self, tmp_path: Path) -> None:
        """종료 시 JobProcessor.stop()이 호출되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)
        mocks = self._get_orchestration_patches()

        with patch(
            "core.thermal_manager.ThermalManager",
            mocks["thermal_cls"],
        ), patch(
            "core.pipeline.PipelineManager",
            mocks["pipeline_cls"],
        ), patch(
            "core.watcher.FolderWatcher",
            mocks["watcher_cls"],
        ), patch(
            "core.orchestrator.JobProcessor",
            mocks["processor_cls"],
        ):
            with TestClient(app) as _client:
                pass  # TestClient __exit__에서 shutdown 실행

        mocks["processor"].stop.assert_called_once()

    def test_shutdown시_folder_watcher_stop(self, tmp_path: Path) -> None:
        """종료 시 FolderWatcher.stop()이 호출되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)
        mocks = self._get_orchestration_patches()

        with patch(
            "core.thermal_manager.ThermalManager",
            mocks["thermal_cls"],
        ), patch(
            "core.pipeline.PipelineManager",
            mocks["pipeline_cls"],
        ), patch(
            "core.watcher.FolderWatcher",
            mocks["watcher_cls"],
        ), patch(
            "core.orchestrator.JobProcessor",
            mocks["processor_cls"],
        ):
            with TestClient(app) as _client:
                pass  # TestClient __exit__에서 shutdown 실행

        mocks["watcher"].stop.assert_called_once()

    def test_초기화_실패시_서버_시작_가능(self, tmp_path: Path) -> None:
        """모든 오케스트레이션 컴포넌트 초기화가 실패해도 서버가 정상 시작되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        with patch(
            "core.thermal_manager.ThermalManager",
            side_effect=RuntimeError("ThermalManager 실패"),
        ), patch(
            "core.pipeline.PipelineManager",
            side_effect=RuntimeError("PipelineManager 실패"),
        ), patch(
            "core.watcher.FolderWatcher",
            side_effect=RuntimeError("FolderWatcher 실패"),
        ):
            with TestClient(app) as client:
                response = client.get("/api/health")
                assert response.status_code == 200

                assert app.state.thermal_manager is None
                assert app.state.pipeline_manager is None
                assert app.state.folder_watcher is None
                assert app.state.job_processor is None

    def test_pipeline_없으면_job_processor_비활성(self, tmp_path: Path) -> None:
        """PipelineManager가 None이면 JobProcessor가 생성되지 않는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)
        mocks = self._get_orchestration_patches()

        with patch(
            "core.thermal_manager.ThermalManager",
            mocks["thermal_cls"],
        ), patch(
            "core.pipeline.PipelineManager",
            side_effect=RuntimeError("PipelineManager 실패"),
        ), patch(
            "core.watcher.FolderWatcher",
            mocks["watcher_cls"],
        ), patch(
            "core.orchestrator.JobProcessor",
            mocks["processor_cls"],
        ):
            with TestClient(app) as _client:
                assert app.state.pipeline_manager is None
                assert app.state.job_processor is None

            # JobProcessor 생성자가 호출되지 않아야 함
            mocks["processor_cls"].assert_not_called()
