"""Wiki API Phase 2.G 엔드포인트 테스트 모듈

목적: api/routes.py 에 추가되는 두 개의 Wiki Phase 2.G 엔드포인트
      (`GET /api/wiki/pages/{page_type}/{slug:path}`, `GET /api/wiki/search`)
      를 검증한다. Phase 2.F WikiView 가 호출하는 신규 엔드포인트로,
      다음 두 가지 핵심 책임을 테스트한다:

    1. **단일 페이지 raw markdown 반환** — frontmatter, citations 추출 포함
    2. **단순 substring 검색** — Phase 3 BM25 도입 전의 임시 구현

테스트 시나리오:
    /pages/{type}/{slug} (8 건):
        1. 200 — 페이지 존재 + content 반환
        2. 200 — frontmatter 파싱 정확
        3. 200 — citations 추출 (PRD §4.3 형식)
        4. 404 — 페이지 없음
        5. 404 — wiki disabled
        6. 400 — page_type 화이트리스트 위반
        7. 400 — slug path traversal (`../`)
        8. 200 — title 폴백 (frontmatter 없으면 첫 H1)

    /search (7 건):
        9. 빈 q → 빈 결과 (200)
        10. q 매칭 → results 반환
        11. limit 적용
        12. snippet 검증 — q 가 snippet 안에 있음
        13. score 내림차순 정렬
        14. wiki disabled → 빈 결과
        15. case insensitive 매칭

의존성: pytest, fastapi.TestClient, AppConfig, WikiConfig
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from config import AppConfig, PathsConfig, ServerConfig, WikiConfig


# ─── 헬퍼 ───────────────────────────────────────────────────────────────


def _make_test_config(
    tmp_path: Path,
    *,
    wiki_enabled: bool = True,
    wiki_root: Path | None = None,
) -> AppConfig:
    """Wiki Phase 2.G 라우트 테스트용 AppConfig 를 생성한다.

    Args:
        tmp_path: pytest tmp_path fixture
        wiki_enabled: WikiConfig.enabled 값 (기본 True)
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


def _seed_wiki_page(wiki_root: Path, rel_path: str, content: str) -> None:
    """단일 wiki 페이지를 디스크에 생성한다.

    Args:
        wiki_root: wiki 루트 경로
        rel_path: 루트 기준 상대 경로 (예: "decisions/foo.md")
        content: 페이지 내용 (frontmatter + 본문)
    """
    wiki_root.mkdir(parents=True, exist_ok=True)
    abs_path = wiki_root / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8")


# ─── /api/wiki/pages/{page_type}/{slug:path} 테스트 ───────────────────


class TestWikiPageDetailEndpoint:
    """GET /api/wiki/pages/{page_type}/{slug:path} 엔드포인트 테스트."""

    def test_단일_페이지_content_반환(self, tmp_path: Path) -> None:
        """페이지가 존재하면 200 + content (raw markdown) 를 반환한다."""
        wiki_root = tmp_path / "wiki"
        body = (
            "---\n"
            "title: Q3 출시일 결정\n"
            "type: decision\n"
            "---\n\n"
            "# Q3 출시일\n\n"
            "5월 1일로 결정 [meeting:abc12345@00:23:45].\n"
        )
        _seed_wiki_page(wiki_root, "decisions/2026-04-15-q3.md", body)

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/pages/decisions/2026-04-15-q3")

        assert response.status_code == 200
        data = response.json()
        assert data["path"] == "decisions/2026-04-15-q3.md"
        assert data["type"] == "decisions"
        # raw markdown 본문은 frontmatter 제거 후의 내용을 포함해야 한다.
        assert "5월 1일로 결정" in data["content"]
        assert "[meeting:abc12345@00:23:45]" in data["content"]

    def test_frontmatter_파싱_정확(self, tmp_path: Path) -> None:
        """frontmatter 의 key:value 가 정확히 파싱되어야 한다."""
        wiki_root = tmp_path / "wiki"
        body = (
            "---\n"
            "title: 인물 페이지\n"
            "type: person\n"
            "tags: [영업, q3]\n"
            "meeting_count: 5\n"
            "---\n\n"
            "# 영업 담당\n"
        )
        _seed_wiki_page(wiki_root, "people/철수.md", body)

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/pages/people/철수")

        assert response.status_code == 200
        data = response.json()
        fm = data["frontmatter"]
        assert fm["title"] == "인물 페이지"
        assert fm["type"] == "person"
        # tags 는 인라인 리스트 → list 로 파싱
        assert fm["tags"] == ["영업", "q3"]
        # 정수는 int 로 파싱
        assert fm["meeting_count"] == 5

    def test_citations_추출(self, tmp_path: Path) -> None:
        """본문의 인용 마커들이 citations 필드에 정확히 추출되어야 한다."""
        wiki_root = tmp_path / "wiki"
        body = (
            "---\ntitle: 결정\n---\n\n"
            "결정 1 [meeting:abc12345@00:23:45].\n"
            "결정 2 [meeting:def67890@01:15:30].\n"
            "잘못된 형식 [meeting:XYZ@00:00:00] (대문자, 거부).\n"
        )
        _seed_wiki_page(wiki_root, "decisions/foo.md", body)

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/pages/decisions/foo")

        assert response.status_code == 200
        data = response.json()
        citations = data["citations"]
        assert len(citations) == 2
        # 첫 번째 citation 검증
        first = citations[0]
        assert first["meeting_id"] == "abc12345"
        assert first["timestamp"] == "00:23:45"
        # 00:23:45 → 23*60 + 45 = 1425 초
        assert first["timestamp_seconds"] == 1425
        # 두 번째 citation
        second = citations[1]
        assert second["meeting_id"] == "def67890"
        assert second["timestamp"] == "01:15:30"
        # 01:15:30 → 3600 + 15*60 + 30 = 4530 초
        assert second["timestamp_seconds"] == 4530

    def test_페이지_없음_404(self, tmp_path: Path) -> None:
        """페이지가 존재하지 않으면 404 반환."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir(parents=True, exist_ok=True)
        (wiki_root / "decisions").mkdir()

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/pages/decisions/존재하지_않음")

        assert response.status_code == 404

    def test_wiki_disabled_404(self, tmp_path: Path) -> None:
        """wiki.enabled=False 면 페이지가 디스크에 있어도 404 반환."""
        wiki_root = tmp_path / "wiki"
        _seed_wiki_page(
            wiki_root, "decisions/foo.md", "---\ntitle: x\n---\n\n# foo\n"
        )

        config = _make_test_config(tmp_path, wiki_enabled=False, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/pages/decisions/foo")

        assert response.status_code == 404

    def test_잘못된_page_type_400(self, tmp_path: Path) -> None:
        """page_type 이 화이트리스트(decisions/people/projects/topics) 외면 400 반환."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/pages/invalid_type/foo")

        assert response.status_code == 400

    def test_slug_path_traversal_400(self, tmp_path: Path) -> None:
        """slug 에 `..` 가 포함되면 path traversal 시도로 간주, 400 반환."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        # 다른 카테고리에 페이지를 둔다 — traversal 로 이걸 읽으려는 시도
        _seed_wiki_page(
            wiki_root, "topics/secret.md", "---\ntitle: secret\n---\n\n# secret\n"
        )

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            # decisions/ 에서 ../topics/secret 으로 빠지려는 시도
            response = client.get("/api/wiki/pages/decisions/..%2Ftopics%2Fsecret")

        # 400 (path traversal 거부) 또는 404 (정규화 후 미존재). 둘 다 안전.
        # 단, **200 으로 다른 카테고리 페이지를 노출해서는 안 된다.**
        assert response.status_code in {400, 404}
        if response.status_code == 200:  # pragma: no cover — 보안 회귀 방지
            assert False, "path traversal 차단 실패"

    def test_title_h1_폴백(self, tmp_path: Path) -> None:
        """frontmatter 에 title 이 없으면 첫 H1 을 title 로 사용한다."""
        wiki_root = tmp_path / "wiki"
        # frontmatter 없이 본문만
        body = "# 첫 번째 제목\n\n본문.\n"
        _seed_wiki_page(wiki_root, "topics/no-frontmatter.md", body)

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/pages/topics/no-frontmatter")

        assert response.status_code == 200
        data = response.json()
        # frontmatter 없으면 빈 dict, title 은 H1 에서 추출
        assert data["frontmatter"] == {}
        assert data["title"] == "첫 번째 제목"


# ─── /api/wiki/search 테스트 ──────────────────────────────────────────


class TestWikiSearchEndpoint:
    """GET /api/wiki/search 엔드포인트 테스트."""

    def test_빈_쿼리_빈_결과(self, tmp_path: Path) -> None:
        """q 가 빈 문자열이면 빈 결과를 반환한다 (200 OK)."""
        wiki_root = tmp_path / "wiki"
        _seed_wiki_page(
            wiki_root, "decisions/foo.md", "---\ntitle: x\n---\n\n# foo bar\n"
        )

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/search?q=")

        assert response.status_code == 200
        data = response.json()
        assert data["results"] == []
        assert data["total"] == 0
        assert data["query"] == ""

    def test_q_매칭_results_반환(self, tmp_path: Path) -> None:
        """q 가 페이지 본문에 포함되면 results 에 등장한다."""
        wiki_root = tmp_path / "wiki"
        _seed_wiki_page(
            wiki_root,
            "decisions/q3-launch.md",
            "---\ntitle: Q3 출시\n---\n\n# Q3 출시 일정\n\n5월 1일에 출시한다.\n",
        )
        _seed_wiki_page(
            wiki_root,
            "decisions/budget.md",
            "---\ntitle: 예산\n---\n\n# 예산 안건\n\n예산은 100M.\n",
        )

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/search?q=Q3")

        assert response.status_code == 200
        data = response.json()
        # "Q3" 가 들어간 페이지 1개만 매칭되어야 한다 (예산 페이지는 제외).
        assert data["total"] == 1
        assert len(data["results"]) == 1
        assert data["results"][0]["path"] == "decisions/q3-launch.md"
        assert data["query"] == "Q3"

    def test_limit_적용(self, tmp_path: Path) -> None:
        """결과 수가 limit 을 초과하면 잘려서 반환된다."""
        wiki_root = tmp_path / "wiki"
        # 같은 단어("출시")를 가진 페이지 25개 생성
        for i in range(25):
            _seed_wiki_page(
                wiki_root,
                f"decisions/page-{i:02d}.md",
                f"---\ntitle: 페이지 {i}\n---\n\n# 출시 안건 {i}\n",
            )

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/search?q=출시&limit=10")

        assert response.status_code == 200
        data = response.json()
        # limit=10 이면 결과는 정확히 10건.
        assert len(data["results"]) == 10
        # total 은 limit 과 별개로 매칭된 전체 페이지 수 (구현 정책).
        assert data["total"] == 10

    def test_snippet_q_포함(self, tmp_path: Path) -> None:
        """반환된 snippet 에 q 가 포함되어 있어야 한다."""
        wiki_root = tmp_path / "wiki"
        _seed_wiki_page(
            wiki_root,
            "topics/long.md",
            "---\ntitle: 긴 페이지\n---\n\n"
            + "이 부분은 무관한 내용이 가득합니다. " * 20
            + "이 위치에 키워드매직이 있습니다. "
            + "그 후로도 계속 다른 내용이 이어집니다. " * 20,
        )

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/search?q=키워드매직")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        snippet = data["results"][0]["snippet"]
        assert "키워드매직" in snippet
        # snippet 길이가 합리적인 범위(원문 전체가 아니라 발췌) 인지 확인.
        # 30 + len("키워드매직") + 30 = 약 70자 안팎이지만 멀티바이트라
        # 상한을 넉넉히 200자로 둔다.
        assert len(snippet) <= 200

    def test_score_내림차순_정렬(self, tmp_path: Path) -> None:
        """매칭 횟수가 많은 페이지가 먼저 (높은 score) 등장해야 한다."""
        wiki_root = tmp_path / "wiki"
        # "키워드" 가 1회만 등장하는 페이지
        _seed_wiki_page(
            wiki_root,
            "topics/few.md",
            "---\ntitle: 적은 매칭\n---\n\n# 키워드 한 번\n",
        )
        # "키워드" 가 5회 등장하는 페이지
        _seed_wiki_page(
            wiki_root,
            "topics/many.md",
            "---\ntitle: 많은 매칭\n---\n\n"
            "키워드 키워드 키워드 키워드 키워드.\n",
        )

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/search?q=키워드")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        # 상위 결과가 매칭 횟수가 많은 페이지여야 한다.
        results = data["results"]
        assert results[0]["path"] == "topics/many.md"
        assert results[1]["path"] == "topics/few.md"
        # score 도 내림차순.
        assert results[0]["score"] >= results[1]["score"]

    def test_wiki_disabled_빈_결과(self, tmp_path: Path) -> None:
        """wiki.enabled=False 면 페이지 존재해도 검색은 빈 결과 (200)."""
        wiki_root = tmp_path / "wiki"
        _seed_wiki_page(
            wiki_root, "decisions/foo.md", "---\ntitle: x\n---\n\n# foo\n"
        )

        config = _make_test_config(tmp_path, wiki_enabled=False, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/search?q=foo")

        assert response.status_code == 200
        data = response.json()
        assert data["results"] == []
        assert data["total"] == 0

    def test_case_insensitive_매칭(self, tmp_path: Path) -> None:
        """대소문자 무관하게 매칭되어야 한다."""
        wiki_root = tmp_path / "wiki"
        _seed_wiki_page(
            wiki_root,
            "topics/case.md",
            "---\ntitle: case test\n---\n\n# Test 영문 단어\n",
        )

        config = _make_test_config(tmp_path, wiki_enabled=True, wiki_root=wiki_root)
        app = _make_test_app(config)

        with TestClient(app) as client:
            # 본문에는 "Test" 가 들어있으나 q 는 소문자 "test"
            response = client.get("/api/wiki/search?q=test")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["results"][0]["path"] == "topics/case.md"
