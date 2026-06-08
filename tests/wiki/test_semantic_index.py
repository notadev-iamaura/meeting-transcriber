"""WikiSemanticIndex(위키 벡터 스토어 래퍼) 테스트.

WikiSemanticIndex 는 임베딩(text→vector)을 하지 않고 벡터 스토어(ChromaDB)의
upsert/query 만 담당한다. `collection_factory` 주입으로 가짜 컬렉션을 넣어 ChromaDB·
모델 없이 결정적으로 검증한다(불변식: 로컬·네이티브모델/시크릿 없이 검증).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from core.wiki.semantic_index import (
    WikiSemanticIndex,
    rebuild_semantic_index,
    reindex_incremental,
)
from core.wiki.store import WikiStore


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
        self._meta: dict[str, dict[str, Any]] = {}

    def upsert(
        self,
        *,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]] | None = None,
        **_: Any,
    ) -> None:
        metas = metadatas if metadatas is not None else [{} for _ in ids]
        for i, e, m in zip(ids, embeddings, metas, strict=False):
            self._emb[i] = e
            self._meta[i] = m

    def delete(self, *, ids: list[str]) -> None:
        for i in ids:
            self._emb.pop(i, None)
            self._meta.pop(i, None)

    def count(self) -> int:
        return len(self._emb)

    def get(self, **_: Any) -> dict[str, Any]:
        ids = list(self._emb)
        return {"ids": ids, "metadatas": [self._meta.get(i, {}) for i in ids]}

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


def test_rebuild_semantic_index_모든_페이지를_임베딩_upsert(tmp_path: Path) -> None:
    """스토어의 모든 페이지를 주입 임베딩으로 벡터 스토어에 upsert한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("decisions/a.md"), _md("결정 A", "예산 합의"))
    store.write_page(Path("decisions/b.md"), _md("결정 B", "다른 안건"))
    index = _index(tmp_path, _FakeCollection())

    calls: list[list[str]] = []

    def _fake_embed(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [[float(len(t)), 0.0] for t in texts]

    n = rebuild_semantic_index(store, semantic_index=index, embed_documents=_fake_embed)

    assert n == 2
    assert index.count() == 2
    assert len(calls) == 1 and len(calls[0]) == 2  # 두 페이지 텍스트 1회 배치 임베딩


def test_rebuild_semantic_index_임베딩_실패시_0(tmp_path: Path) -> None:
    """임베딩(e5)이 실패하면 graceful 0 반환(검색은 BM25 폴백)."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("decisions/a.md"), _md("결정 A", "예산 합의"))
    index = _index(tmp_path, _FakeCollection())

    def _raise_embed(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("e5 로드 불가")

    n = rebuild_semantic_index(store, semantic_index=index, embed_documents=_raise_embed)

    assert n == 0
    assert index.count() == 0


def _inc_setup(tmp_path: Path) -> tuple[WikiStore, WikiSemanticIndex, Any, list[list[str]]]:
    """증분 테스트용 store(2페이지) + index + (호출 기록하는)임베더."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("decisions/a.md"), _md("결정 A", "예산 합의"))
    store.write_page(Path("decisions/b.md"), _md("결정 B", "일정 합의"))
    index = _index(tmp_path, _FakeCollection())
    calls: list[list[str]] = []

    def _embed(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [[float(len(t)), 0.0] for t in texts]

    return store, index, _embed, calls


def test_reindex_incremental_최초는_전체_임베딩(tmp_path: Path) -> None:
    """빈 인덱스 → 모든 페이지 임베딩(embedded=total)."""
    store, index, embed, _calls = _inc_setup(tmp_path)
    stats = reindex_incremental(store, semantic_index=index, embed_documents=embed)
    assert stats == {"embedded": 2, "deleted": 0, "total": 2}
    assert index.count() == 2


def test_reindex_incremental_무변경시_재임베딩_안한다(tmp_path: Path) -> None:
    """content_hash 동일 → 임베더 미호출, embedded=0(전체 재임베딩 회피)."""
    store, index, embed, calls = _inc_setup(tmp_path)
    reindex_incremental(store, semantic_index=index, embed_documents=embed)
    calls.clear()
    stats = reindex_incremental(store, semantic_index=index, embed_documents=embed)
    assert stats["embedded"] == 0
    assert calls == []  # 임베더(=e5 로드) 미호출


def test_reindex_incremental_변경된_페이지만_임베딩한다(tmp_path: Path) -> None:
    """1개 페이지 본문 변경 → 그 페이지만 재임베딩(embedded=1)."""
    store, index, embed, calls = _inc_setup(tmp_path)
    reindex_incremental(store, semantic_index=index, embed_documents=embed)
    calls.clear()
    store.write_page(Path("decisions/a.md"), _md("결정 A", "예산을 2억으로 상향"))  # a 변경
    stats = reindex_incremental(store, semantic_index=index, embed_documents=embed)
    assert stats["embedded"] == 1
    assert len(calls) == 1 and len(calls[0]) == 1  # 변경된 a 한 건만 임베딩


def test_reindex_incremental_삭제된_페이지의_벡터를_제거한다(tmp_path: Path) -> None:
    """스토어에서 사라진 페이지(거부/이동)는 orphan 으로 삭제 — stale 벡터 방지."""
    store, index, embed, _calls = _inc_setup(tmp_path)
    reindex_incremental(store, semantic_index=index, embed_documents=embed)
    store.delete_page(Path("decisions/b.md"))  # b 제거
    stats = reindex_incremental(store, semantic_index=index, embed_documents=embed)
    assert stats["deleted"] == 1
    assert stats["total"] == 1
    assert index.count() == 1  # b 벡터 제거됨
