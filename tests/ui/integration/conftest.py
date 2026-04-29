"""tests/ui/integration — SPA 실제 통합 검증용 정적 서버 fixture.

목적
----
fixture-as-source-of-truth 패턴(`tests/ui/_fixtures/*.html`) 의 갭을 메우기
위해 실제 SPA(`ui/web/index.html` + `spa.js` + `style.css`) 를 띄우고
7 UI/UX Overhaul 컴포넌트가 정상 동작하는지 확인한다.

서버 전략
---------
FastAPI 백엔드(`api.server.create_app`) 를 띄우면 mlx-whisper 등 무거운
의존성이 import 되어 테스트가 느려진다. 대신 가벼운 `http.server` 로
정적 파일만 서빙하고, SPA 가 fetch 하는 `/api/*` 호출은 Playwright
`page.route()` 로 mock 한다.

URL 매핑:
    GET /            → ui/web/index.html
    GET /static/*    → ui/web/* (style.css, app.js, spa.js, brand/*)
    GET /api/*       → Playwright route 가 가로채서 빈 응답 반환

이 방식이면 실제 SPA 코드를 그대로 검증 가능 — file:// 로는 cross-origin
fetch 가 깨지고, FastAPI 는 너무 무겁다.
"""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEB_DIR = PROJECT_ROOT / "ui" / "web"


def _find_free_port() -> int:
    """OS 가 빈 포트를 할당하도록 위임."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _SPAStaticHandler(SimpleHTTPRequestHandler):
    """ui/web 디렉토리를 root 로 사용.

    SPA 는 절대 경로(`/static/style.css`, `/static/spa.js`) 를 사용하므로
    `/static/X` → `ui/web/X` 로 매핑해야 한다. 또 `/` → `index.html`.

    `/api/*` 는 200 빈 JSON 으로 응답 — Playwright route 가 이걸 가로챌
    것이므로 fallback 일 뿐.
    """

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # 테스트 출력 오염 방지 — 로그 무시
        return

    def translate_path(self, path: str) -> str:
        # /static/X → ui/web/X 매핑
        if path.startswith("/static/"):
            path = path[len("/static") :]
        # / → /index.html
        if path == "/" or path == "":
            path = "/index.html"
        # 부모 디렉토리를 ui/web 으로 강제
        return str(WEB_DIR) + path.split("?", 1)[0].split("#", 1)[0]

    def _serve_index(self) -> None:
        """SPA fallback: 모든 라우트(/app/*) 는 index.html 로 내려보낸다.

        실제 FastAPI 백엔드의 `_setup_spa_routes()` 와 동일한 동작 — SPA
        라우터가 클라이언트 측에서 path 를 해석하므로 서버는 index.html 만
        내주면 된다.
        """
        index_path = WEB_DIR / "index.html"
        try:
            content = index_path.read_bytes()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        # /api/* 는 빈 JSON (Playwright route 미적용 시 fallback)
        if self.path.startswith("/api/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        # /app/* → SPA fallback (index.html)
        # /static/* 는 SimpleHTTPRequestHandler 기본 동작 사용
        if self.path == "/app" or self.path.startswith("/app/"):
            self._serve_index()
            return
        super().do_GET()


@pytest.fixture(scope="session")
def spa_static_server() -> Iterator[str]:
    """SPA 를 정적 파일로 서빙하는 일회성 HTTP 서버.

    Returns:
        SPA index.html 의 base URL (예: http://127.0.0.1:53421).
    """
    port = _find_free_port()
    httpd = HTTPServer(("127.0.0.1", port), _SPAStaticHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    # 서버 실제 listen 까지 잠깐 대기
    time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=2.0)
