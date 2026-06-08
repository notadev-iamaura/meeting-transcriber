"""G1 실 e5 시맨틱 회상 검증 (native — 기본 게이트 제외).

실제 multilingual-e5-small + ChromaDB 로 end-to-end 하이브리드 검색을 검증한다.
어휘가 겹치지 않는 쿼리("재정 규모")가 의미상 관련된 페이지("예산")를 벡터로
회상하는지, BM25-only 와 대조해 확인한다. `pytest -m native` 로만 실행된다.
"""

from __future__ import annotations

import asyncio
import resource
from pathlib import Path

import pytest

from config import get_config
from core.wiki.search_index import WikiSearchIndex
from core.wiki.semantic_index import (
    WikiSemanticIndex,
    make_default_embed_documents,
    rebuild_semantic_index,
)
from core.wiki.semantic_search import wiki_hybrid_search
from core.wiki.store import WikiStore

pytestmark = pytest.mark.native

# e5(470MB) + ChromaDB 로드 후에도 피크 RSS 상한(불변식 #7: ≤9.5GB). 실측 ~1.35GB 기준
# 넉넉한 3GB soft-ceiling — 모델 누수/이중적재 같은 gross 회귀를 자동 감지한다.
_RSS_PEAK_CEILING_MB = 3072.0


def _peak_rss_mb() -> float:
    """프로세스 피크 RSS(MB). macOS 는 ru_maxrss 가 바이트 단위."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


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


def test_실_e5_어휘비매칭_시맨틱_회상(tmp_path: Path) -> None:
    """'재정 규모'(어휘 비매칭)가 '예산' 페이지를 벡터로 회상한다 — BM25 는 못 찾음."""
    cfg = get_config()
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("decisions/budget.md"),
        _md("예산 확정", "올해 예산을 1억원으로 확정했다. [meeting:1234abcd@00:01:20]"),
    )
    store.write_page(
        Path("decisions/schedule.md"),
        _md("일정 확정", "제품 출시 일정을 7월로 잡았다. [meeting:1234abcd@00:02:00]"),
    )

    search_index = WikiSearchIndex(store.root)
    search_index.rebuild(store)

    semantic_index = WikiSemanticIndex(tmp_path / "chroma")
    indexed = rebuild_semantic_index(
        store,
        semantic_index=semantic_index,
        embed_documents=make_default_embed_documents(cfg),
    )
    assert indexed == 2
    assert semantic_index.count() == 2

    query = "재정 규모는 얼마로 정했나"  # '예산' 과 글자가 겹치지 않음

    # BM25 단독: 어휘 비매칭이라 budget 을 못 찾음(시맨틱 회상의 필요성 증명)
    bm25 = search_index.search(query, top_k=5)
    assert "decisions/budget.md" not in [r.page_path for r in bm25]

    # 하이브리드(실 e5): 벡터가 의미로 budget 을 회상
    hybrid = asyncio.run(
        wiki_hybrid_search(
            query,
            search_index=search_index,
            semantic_index=semantic_index,
            config=cfg,
            top_k=5,
        )
    )
    paths = [r.page_path for r in hybrid]
    assert "decisions/budget.md" in paths, f"시맨틱 회상 실패: {paths}"
    assert paths[0] == "decisions/budget.md", f"budget 이 1위가 아님: {paths}"

    # RAM 회귀 자동 감지: e5 문서 임베딩 + 쿼리 임베딩 + ChromaDB 후 피크 RSS 상한.
    peak_mb = _peak_rss_mb()
    assert peak_mb < _RSS_PEAK_CEILING_MB, (
        f"e5 하이브리드 피크 RSS {peak_mb:.0f}MB > {_RSS_PEAK_CEILING_MB:.0f}MB "
        f"— RAM 회귀(모델 누수/이중적재) 의심"
    )
