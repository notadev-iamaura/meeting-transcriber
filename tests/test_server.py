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

import pytest
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
        assert app.version == "1.0.0"

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
        assert data["version"] == "1.0.0"
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
        assert (
            response.headers.get("access-control-allow-origin")
            == "http://localhost:8765"
        )

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
        assert (
            response.headers.get("access-control-allow-origin")
            == "http://127.0.0.1:8765"
        )

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
        """GET, POST만 허용되는지 확인한다."""
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

        allow_methods = response.headers.get(
            "access-control-allow-methods", ""
        )
        assert "GET" in allow_methods
        assert "POST" in allow_methods


# === TestStaticFiles ===


class TestStaticFiles:
    """정적 파일 서빙 테스트."""

    def test_정적_파일_디렉토리_존재시_마운트(
        self, tmp_path: Path,
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
            route_paths = [
                route.path for route in app.routes
                if hasattr(route, "path")
            ]
            assert any("/static" in p for p in route_paths)

    def test_정적_파일_디렉토리_미존재시_경고(
        self, tmp_path: Path,
    ) -> None:
        """ui/web/ 디렉토리가 없으면 경고 로그만 출력하는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)

        # 존재하지 않는 경로로 패치
        nonexistent = tmp_path / "nonexistent_static"

        with patch("api.server._STATIC_DIR", nonexistent):
            app = create_app(config)

        # /static 라우트가 마운트되지 않아야 함
        route_paths = [
            getattr(route, "path", "")
            for route in app.routes
        ]
        assert not any(
            p.startswith("/static") for p in route_paths
        )

    def test_정적_html_파일_서빙(self, tmp_path: Path) -> None:
        """HTML 정적 파일이 올바르게 서빙되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)

        # 테스트용 HTML 파일 생성
        test_static = tmp_path / "web"
        test_static.mkdir()
        html_content = "<html><body>테스트</body></html>"
        (test_static / "index.html").write_text(
            html_content, encoding="utf-8",
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

        with TestClient(app) as client:
            # lifespan startup 완료 후 state 확인
            assert hasattr(app.state, "job_queue")
            assert app.state.job_queue is not None

    def test_startup시_start_time_설정(self, tmp_path: Path) -> None:
        """서버 시작 시 start_time이 설정되는지 확인한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        app = create_app(config)

        before = time.time()

        with TestClient(app) as client:
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
        assert "테스트 에러" in data["detail"]

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
