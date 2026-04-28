"""Wiki API 엔드포인트 테스트 모듈 (Phase 1.H, TDD Red 단계)

목적: api/routes.py 에 추가되는 두 개의 Wiki Phase 1 엔드포인트
      (`GET /api/wiki/pages`, `GET /api/wiki/health`) 를 검증한다.

테스트 시나리오 (총 8 건):
    1. /pages — wiki disabled 시 빈 목록 반환
    2. /pages — enabled 인데 디렉토리 없음 → 빈 목록
    3. /pages — 디렉토리에 5개 페이지 존재 → 5개 반환
    4. /pages — special files (log.md/index.md/HEALTH.md/CLAUDE.md/action_items.md) 제외
    5. /pages — 200 응답 + JSON content-type
    6. /health — HEALTH.md 없을 때 status=no_lint_yet
    7. /health — HEALTH.md 존재 시 raw_markdown 반환
    8. /health — 200 응답

의존성: pytest, fastapi.TestClient, AppConfig, WikiConfig
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from config import AppConfig, PathsConfig, ServerConfig, WikiConfig


# ─── 헬퍼 ───────────────────────────────────────────────────────────────


def _make_test_config(
    tmp_path: Path,
    *,
    wiki_enabled: bool = False,
    wiki_root: Path | None = None,
) -> AppConfig:
    """Wiki 라우트 테스트용 AppConfig 를 생성한다.

    Args:
        tmp_path: pytest tmp_path fixture
        wiki_enabled: WikiConfig.enabled 값
        wiki_root: WikiConfig.root 경로 (기본 tmp_path/wiki)

    Returns:
        Wiki 설정이 적용된 AppConfig 인스턴스.
    """
    if wiki_root is None:
        wiki_root = tmp_path / "wiki"
    return AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
        wiki=WikiConfig(enabled=wiki_enabled, root=wiki_root),
    )


def _make_test_app(config: AppConfig) -> Any:
    """테스트용 FastAPI 앱을 생성한다.

    외부 의존성(검색 엔진/Chat 엔진)을 mocking 하여 라이프스팬이 정상 종료되도록 한다.

    Args:
        config: AppConfig 인스턴스

    Returns:
        FastAPI 앱 인스턴스
    """
    from api.server import create_app

    with (
        patch(
            "search.hybrid_search.HybridSearchEngine",
            return_value=MagicMock(),
        ),
        patch(
            "search.chat.ChatEngine",
            return_value=MagicMock(),
        ),
    ):
        app = create_app(config)
    return app


def _seed_wiki_pages(wiki_root: Path, page_paths: list[str]) -> None:
    """wiki_root 하위에 단순 마크다운 페이지를 만들어 둔다.

    이 헬퍼는 WikiStore.init_repo() 를 사용하지 않고 디스크 파일만 생성한다 —
    엔드포인트 자체는 git 동작에 의존하지 않으므로 테스트가 더 빠르고 단순해진다.

    Args:
        wiki_root: wiki 루트 경로
        page_paths: 루트 기준 상대 경로 리스트 (예: "decisions/foo.md")
    """
    wiki_root.mkdir(parents=True, exist_ok=True)
    for rel in page_paths:
        abs_path = wiki_root / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(
            "---\ntitle: 샘플\n---\n\n# 샘플 페이지\n",
            encoding="utf-8",
        )


# ─── /api/wiki/pages 테스트 ────────────────────────────────────────────


class TestWikiPagesEndpoint:
    """GET /api/wiki/pages 엔드포인트 테스트."""

    def test_pages_disabled_시_빈_목록(self, tmp_path: Path) -> None:
        """wiki.enabled=False 면 항상 빈 목록을 반환한다."""
        config = _make_test_config(tmp_path, wiki_enabled=False)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/pages")

        assert response.status_code == 200
        data = response.json()
        assert data == {"pages": [], "total": 0}

    def test_pages_enabled_이지만_디렉토리_없음(self, tmp_path: Path) -> None:
        """wiki.enabled=True 인데 wiki 디렉토리 자체가 없으면 빈 목록 반환."""
        wiki_root = tmp_path / "no-such-dir"
        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/pages")

        assert response.status_code == 200
        data = response.json()
        assert data == {"pages": [], "total": 0}

    def test_pages_디렉토리에_5개_페이지(self, tmp_path: Path) -> None:
        """wiki 디렉토리에 5개의 일반 페이지가 있으면 모두 반환한다."""
        wiki_root = tmp_path / "wiki"
        page_paths = [
            "decisions/2026-04-15-foo.md",
            "decisions/2026-04-16-bar.md",
            "people/alice.md",
            "projects/proj-alpha.md",
            "topics/topic-x.md",
        ]
        _seed_wiki_pages(wiki_root, page_paths)

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/pages")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 5
        assert len(data["pages"]) == 5
        # 응답 페이지 path 가 상대경로로 직렬화돼 있어야 한다.
        returned_paths = {p["path"] for p in data["pages"]}
        assert returned_paths == set(page_paths)
        # type 필드는 PageType.value 문자열이어야 한다.
        for page in data["pages"]:
            assert page["type"] in {"decision", "person", "project", "topic"}

    def test_pages_특수파일_제외(self, tmp_path: Path) -> None:
        """루트 직속 특수 파일 (log.md / index.md / HEALTH.md / CLAUDE.md /
        action_items.md) 은 결과에 포함되지 않는다."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        # 특수 파일 5개 + 일반 페이지 1개
        for special in ("log.md", "index.md", "HEALTH.md", "CLAUDE.md", "action_items.md"):
            (wiki_root / special).write_text("# special\n", encoding="utf-8")
        (wiki_root / "decisions").mkdir()
        (wiki_root / "decisions" / "real.md").write_text(
            "# 실제 페이지\n", encoding="utf-8"
        )

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/pages")

        data = response.json()
        # 특수 파일은 모두 제외되고, 일반 페이지 1건만 남아야 한다.
        assert data["total"] == 1
        assert len(data["pages"]) == 1
        assert data["pages"][0]["path"] == "decisions/real.md"

    def test_pages_응답_content_type(self, tmp_path: Path) -> None:
        """응답이 200 OK + application/json 인지 확인."""
        config = _make_test_config(tmp_path, wiki_enabled=False)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/pages")

        assert response.status_code == 200
        assert "application/json" in response.headers["content-type"]


# ─── /api/wiki/health 테스트 ───────────────────────────────────────────


class TestWikiHealthEndpoint:
    """GET /api/wiki/health 엔드포인트 테스트."""

    def test_health_HEALTH_md_없음(self, tmp_path: Path) -> None:
        """HEALTH.md 가 없으면 status=no_lint_yet 반환."""
        config = _make_test_config(tmp_path, wiki_enabled=True)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "no_lint_yet"
        assert data["last_lint_at"] is None
        # raw_markdown 필드는 None 이거나 미존재여야 한다.
        assert data.get("raw_markdown") in (None, "")

    def test_health_HEALTH_md_존재(self, tmp_path: Path) -> None:
        """HEALTH.md 가 있으면 raw_markdown 으로 그 내용을 그대로 반환한다."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        health_content = (
            "# HEALTH\n\n"
            "- last_lint_at: 2026-04-28T10:00:00\n"
            "- citation_pass_rate: 1.0\n"
        )
        (wiki_root / "HEALTH.md").write_text(health_content, encoding="utf-8")

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/health")

        assert response.status_code == 200
        data = response.json()
        assert data["raw_markdown"] == health_content
        assert data["status"] in {"ok", "warnings", "no_lint_yet"}

    def test_health_응답_200_json(self, tmp_path: Path) -> None:
        """응답이 200 OK + application/json 인지 확인."""
        config = _make_test_config(tmp_path, wiki_enabled=True)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/health")

        assert response.status_code == 200
        assert "application/json" in response.headers["content-type"]
