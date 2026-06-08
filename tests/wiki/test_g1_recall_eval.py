"""G1 효과 정량화 — 골든셋 recall@5 (BM25 vs 하이브리드, native 실 e5).

적대 리뷰 후속 TODO 우선순위 2: 게이트 오버라이드(벡터 켜기)의 비용 대비 이득을
수치로 고정한다. 어휘가 거의 겹치지 않는 패러프레이즈 쿼리 셋으로, 정답 결정 페이지를
top-5 안에 회상하는 비율을 BM25 단독과 하이브리드(벡터+BM25)에서 각각 측정·대조한다.

`pytest -m native -s` 로 실행하면 recall 수치가 출력된다.
"""

from __future__ import annotations

import asyncio
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

# (page_id, 결정 본문, 패러프레이즈 쿼리) — 쿼리는 본문과 어휘를 거의 안 겹치게 설계.
_GOLDEN: list[tuple[str, str, str]] = [
    ("budget", "올해 예산을 1억원으로 확정했다.", "재정 규모는 얼마로 정했나"),
    ("schedule", "제품 출시 일정을 7월로 잡았다.", "런칭 시점을 언제로 결정했나"),
    ("hiring", "백엔드 개발자 2명을 채용하기로 했다.", "인력 충원 인원을 몇으로 정했나"),
    ("vendor", "클라우드 공급자로 AWS를 선정했다.", "인프라 업체를 어디로 골랐나"),
    ("database", "데이터베이스를 PostgreSQL로 교체하기로 했다.", "저장소 기술을 무엇으로 바꾸나"),
    ("marketing", "홍보를 인스타그램 광고에 집중하기로 했다.", "마케팅 채널을 어디로 정했나"),
    ("meeting", "정기 회의를 주 2회로 축소하기로 했다.", "미팅 빈도를 어떻게 조정했나"),
    ("refund", "환불 정책을 30일 이내 전액으로 정했다.", "반품 규정 기간을 며칠로 했나"),
]

_TOP_K = 5


def _md(page_id: str, body: str) -> str:
    return f"""---
type: decision
title: {page_id} 결정
status: decided
decision_date: 2026-05-21
project: Apollo
participants: [민수]
owners: [민수]
confidence: 9
source_meetings: [1234abcd]
last_updated: 2026-05-21T10:00:00
---

# {page_id} 결정

{body} [meeting:1234abcd@00:01:20]
"""


def test_recall_at_5_하이브리드가_BM25보다_시맨틱_회상이_높다(tmp_path: Path) -> None:
    """골든셋에서 하이브리드 recall@5 ≥ BM25 + 하이브리드가 어휘비매칭을 추가 회상."""
    cfg = get_config()
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    for page_id, body, _q in _GOLDEN:
        store.write_page(Path(f"decisions/{page_id}.md"), _md(page_id, body))

    search_index = WikiSearchIndex(store.root)
    search_index.rebuild(store)
    semantic_index = WikiSemanticIndex(tmp_path / "chroma")
    rebuild_semantic_index(
        store, semantic_index=semantic_index, embed_documents=make_default_embed_documents(cfg)
    )

    bm25_hits = 0
    hybrid_hits = 0
    for page_id, _body, query in _GOLDEN:
        target = f"decisions/{page_id}.md"

        bm25 = search_index.search(query, top_k=_TOP_K)
        if target in [r.page_path for r in bm25]:
            bm25_hits += 1

        hybrid = asyncio.run(
            wiki_hybrid_search(
                query,
                search_index=search_index,
                semantic_index=semantic_index,
                config=cfg,
                top_k=_TOP_K,
            )
        )
        if target in [r.page_path for r in hybrid]:
            hybrid_hits += 1

    n = len(_GOLDEN)
    bm25_recall = bm25_hits / n
    hybrid_recall = hybrid_hits / n
    print(
        f"\n[recall@{_TOP_K}] BM25={bm25_recall:.0%} ({bm25_hits}/{n})  "
        f"HYBRID={hybrid_recall:.0%} ({hybrid_hits}/{n})  Δ=+{hybrid_recall - bm25_recall:.0%}"
    )

    # 하이브리드가 BM25 대비 손해 없고(>=) 시맨틱으로 추가 회상(>)함을 고정.
    assert hybrid_recall >= bm25_recall, (
        f"하이브리드 recall@{_TOP_K}({hybrid_recall:.0%})가 BM25({bm25_recall:.0%})보다 낮음"
    )
    assert hybrid_recall > bm25_recall, (
        f"하이브리드가 어휘비매칭을 추가 회상하지 못함 (BM25={bm25_hits}, HYBRID={hybrid_hits})"
    )
