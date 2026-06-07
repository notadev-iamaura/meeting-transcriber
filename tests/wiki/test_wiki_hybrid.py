"""G1 하이브리드 오케스트레이션(`fuse_hybrid`) 통합 테스트.

실제 WikiSearchIndex(BM25, tmp 스토어) + 가짜 시맨틱 인덱스(고정 벡터 랭킹)로
하이브리드 경로를 end-to-end 검증한다. 임베딩(모델 로드)은 주입된 query_embedding
으로 대체 → 네이티브 모델/ChromaDB 없이 결정적.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

from config import WikiRankingConfig, WikiSemanticConfig, get_config
from core.wiki.search_index import WikiSearchIndex
from core.wiki.semantic_search import fuse_hybrid, wiki_hybrid_search
from core.wiki.store import WikiStore

_NOW = date(2026, 6, 1)


def _md(title: str, body: str) -> str:
    return f"""---
type: decision
title: {title}
status: decided
decision_date: 2026-05-21
project: Apollo
participants: [민수]
owners: [민수]
confidence: 9
source_meetings: [1234abcd]
last_updated: 2026-05-21T10:00:00
---

# {title}

{body}
"""


class _FakeSemantic:
    """semantic_index.query 만 흉내내는 가짜(고정 랭킹 반환)."""

    def __init__(self, ranked: list[tuple[str, int]]) -> None:
        self._ranked = ranked

    def query(self, embedding: list[float], top_k: int) -> list[tuple[str, int]]:
        return list(self._ranked[:top_k])


def _build_index(tmp_path: Path) -> WikiSearchIndex:
    """a.md(예산 매칭), b.md(예산 비매칭) 로 BM25 인덱스를 만든다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("decisions/a.md"),
        _md("결정 A", "예산 배정을 합의했다. [meeting:1234abcd@00:01:20]"),
    )
    store.write_page(
        Path("decisions/b.md"),
        _md("결정 B", "전혀 다른 안건 별개 사항. [meeting:1234abcd@00:02:00]"),
    )
    index = WikiSearchIndex(store.root)
    index.rebuild(store)
    return index


def test_벡터_전용_매치가_실제_fetch_candidates로_유입된다(tmp_path: Path) -> None:
    """BM25 는 a.md 만 매칭하지만, 벡터가 찾은 b.md 가 결과에 유입된다."""
    index = _build_index(tmp_path)
    semantic_index = _FakeSemantic([("decisions/b.md", 1)])

    results = fuse_hybrid(
        "예산",
        search_index=index,
        semantic_index=semantic_index,
        query_embedding=[1.0, 0.0],
        semantic=WikiSemanticConfig(),
        ranking=WikiRankingConfig(),
        now=_NOW,
    )

    paths = [r.page_path for r in results]
    assert "decisions/a.md" in paths  # BM25 매치
    assert "decisions/b.md" in paths  # 벡터 전용 매치 유입(fetch_candidates 보강)


def test_query_embedding_None이면_BM25_only_폴백(tmp_path: Path) -> None:
    """임베더 불가(query_embedding=None) → 벡터 무시, BM25 결과만."""
    index = _build_index(tmp_path)
    semantic_index = _FakeSemantic([("decisions/b.md", 1)])

    results = fuse_hybrid(
        "예산",
        search_index=index,
        semantic_index=semantic_index,
        query_embedding=None,  # 폴백
        semantic=WikiSemanticConfig(),
        ranking=WikiRankingConfig(),
        now=_NOW,
    )

    paths = [r.page_path for r in results]
    assert paths == ["decisions/a.md"]  # b.md(벡터 전용)는 유입 안 됨


def test_semantic_disabled면_벡터를_사용하지_않는다(tmp_path: Path) -> None:
    """semantic.enabled=False 면 query_embedding 이 있어도 벡터 무시."""
    index = _build_index(tmp_path)
    semantic_index = _FakeSemantic([("decisions/b.md", 1)])

    results = fuse_hybrid(
        "예산",
        search_index=index,
        semantic_index=semantic_index,
        query_embedding=[1.0, 0.0],
        semantic=WikiSemanticConfig(enabled=False),
        ranking=WikiRankingConfig(),
        now=_NOW,
    )

    assert [r.page_path for r in results] == ["decisions/a.md"]


def test_필터는_BM25_후보에_적용된다(tmp_path: Path) -> None:
    """page_types 필터가 BM25 후보 추출에 전달된다(person 타입 제외 확인)."""
    index = _build_index(tmp_path)
    semantic_index = _FakeSemantic([])

    results = fuse_hybrid(
        "예산",
        search_index=index,
        semantic_index=semantic_index,
        query_embedding=None,
        semantic=WikiSemanticConfig(),
        ranking=WikiRankingConfig(),
        now=_NOW,
        page_types=["person"],  # decision 페이지뿐이라 결과 0
    )

    assert results == []


def test_async_래퍼는_주입_embed로_벡터전용_매치를_유입한다(tmp_path: Path) -> None:
    """wiki_hybrid_search: 주입된 embed_query 로 실모델 없이 벡터 유입 검증."""
    index = _build_index(tmp_path)
    semantic_index = _FakeSemantic([("decisions/b.md", 1)])

    async def _fake_embed(_q: str) -> list[float]:
        return [1.0, 0.0]

    results = asyncio.run(
        wiki_hybrid_search(
            "예산",
            search_index=index,
            semantic_index=semantic_index,
            config=get_config(),
            now=_NOW,
            embed_query=_fake_embed,
        )
    )

    paths = [r.page_path for r in results]
    assert "decisions/a.md" in paths
    assert "decisions/b.md" in paths  # 벡터 전용 매치 유입


def test_async_래퍼는_임베딩_실패시_BM25_only_폴백한다(tmp_path: Path) -> None:
    """embed_query 가 예외를 던지면(임베더 불가) graceful BM25-only 폴백."""
    index = _build_index(tmp_path)
    semantic_index = _FakeSemantic([("decisions/b.md", 1)])

    async def _raise_embed(_q: str) -> list[float]:
        raise RuntimeError("임베더 로드 불가")

    results = asyncio.run(
        wiki_hybrid_search(
            "예산",
            search_index=index,
            semantic_index=semantic_index,
            config=get_config(),
            now=_NOW,
            embed_query=_raise_embed,
        )
    )

    # 임베딩 실패 → 벡터 무시 → BM25 결과만(b.md 유입 안 됨)
    assert [r.page_path for r in results] == ["decisions/a.md"]
