"""WikiSemanticIndex(위키 벡터 스토어 래퍼) 테스트.

WikiSemanticIndex 는 임베딩(text→vector)을 하지 않고 벡터 스토어(ChromaDB)의
upsert/query 만 담당한다. `collection_factory` 주입으로 가짜 컬렉션을 넣어 ChromaDB·
모델 없이 결정적으로 검증한다(불변식: 로컬·네이티브모델/시크릿 없이 검증).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from core.wiki.semantic_index import WikiSemanticIndex


def _cos(a: list[float], b: list[float]) -> float:
    """코사인 유사도(가짜 컬렉션 랭킹용)."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class _FakeCollection:
    """ChromaDB 컬렉션 최소 인터페이스 in-memory 구현(테스트용)."""

    def __init__(self) -> None:
        self._emb: dict[str, list[float]] = {}

    def upsert(self, *, ids: list[str], embeddings: list[list[float]], **_: Any) -> None:
        for i, e in zip(ids, embeddings, strict=False):
            self._emb[i] = e

    def delete(self, *, ids: list[str]) -> None:
        for i in ids:
            self._emb.pop(i, None)

    def count(self) -> int:
        return len(self._emb)

    def query(
        self, *, query_embeddings: list[list[float]], n_results: int, **_: Any
    ) -> dict[str, Any]:
        q = query_embeddings[0]
        ranked = sorted(self._emb.items(), key=lambda kv: -_cos(q, kv[1]))[:n_results]
        return {"ids": [[pid for pid, _ in ranked]]}


def _index(tmp_path: Path, collection: Any) -> WikiSemanticIndex:
    return WikiSemanticIndex(tmp_path / "chroma", collection_factory=lambda: collection)


def test_upsert_후_query는_유사도순_page_path_랭킹을_반환한다(tmp_path: Path) -> None:
    """벡터 upsert 후 쿼리 임베딩에 가까운 순서로 page_path 가 랭킹된다."""
    index = _index(tmp_path, _FakeCollection())
    index.upsert(
        page_paths=["decisions/a.md", "decisions/b.md", "decisions/c.md"],
        embeddings=[[1.0, 0.0], [0.0, 1.0], [0.9, 0.1]],
        metadatas=[{"page_type": "decision"}] * 3,
    )

    ranked = index.query([1.0, 0.0], top_k=3)

    # [1,0] 에 가까운 순: a(동일) > c(0.9,0.1) > b(직교)
    assert [path for path, _ in ranked] == [
        "decisions/a.md",
        "decisions/c.md",
        "decisions/b.md",
    ]
    # 1-based rank
    assert [rank for _, rank in ranked] == [1, 2, 3]


def test_빈_컬렉션_query는_빈_리스트를_반환한다(tmp_path: Path) -> None:
    """색인 전(빈 컬렉션) 쿼리는 빈 결과."""
    index = _index(tmp_path, _FakeCollection())
    assert index.query([1.0, 0.0], top_k=5) == []


def test_top_k가_벡터_후보_수를_제한한다(tmp_path: Path) -> None:
    index = _index(tmp_path, _FakeCollection())
    index.upsert(
        page_paths=[f"decisions/{i}.md" for i in range(5)],
        embeddings=[[1.0, float(i)] for i in range(5)],
        metadatas=[{} for _ in range(5)],
    )
    assert len(index.query([1.0, 0.0], top_k=2)) == 2


def test_upsert는_멱등적이다_같은_path_재색인시_갱신(tmp_path: Path) -> None:
    """같은 page_path 재upsert 시 벡터가 갱신(중복 생성 안 함)."""
    coll = _FakeCollection()
    index = _index(tmp_path, coll)
    index.upsert(page_paths=["decisions/a.md"], embeddings=[[1.0, 0.0]], metadatas=[{}])
    index.upsert(page_paths=["decisions/a.md"], embeddings=[[0.0, 1.0]], metadatas=[{}])
    assert index.count() == 1


def test_delete_page는_query에서_제외한다(tmp_path: Path) -> None:
    index = _index(tmp_path, _FakeCollection())
    index.upsert(
        page_paths=["decisions/a.md", "decisions/b.md"],
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
        metadatas=[{}, {}],
    )
    index.delete_page("decisions/a.md")
    assert [p for p, _ in index.query([1.0, 0.0], top_k=5)] == ["decisions/b.md"]


def test_컬렉션_불가시_graceful_no_op(tmp_path: Path) -> None:
    """collection_factory 가 None(ChromaDB 불가/preflight 실패) 면 upsert 0·query []."""
    index = WikiSemanticIndex(tmp_path / "chroma", collection_factory=lambda: None)
    assert index.upsert(page_paths=["decisions/a.md"], embeddings=[[1.0]], metadatas=[{}]) == 0
    assert index.query([1.0], top_k=5) == []
    assert index.count() == 0
