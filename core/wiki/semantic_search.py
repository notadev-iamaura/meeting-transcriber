"""G1 위키 하이브리드(벡터+BM25 RRF) 융합 + C1 재랭킹 합성.

벡터 검색 랭킹과 BM25 랭킹을 RRF(Reciprocal Rank Fusion)로 융합한 뒤, 그 융합
점수를 '검색 관련도' 신호로 C1 다중신호 재랭킹(`_rerank`)에 넘긴다. 융합 핵심은
순수 함수라 ChromaDB/임베더 없이 결정적으로 검증된다(불변식: 로컬·시크릿 없이 검증).

벡터 검색 결과가 빈 리스트면(임베더/ChromaDB 불가) RRF 는 FTS 항만 남아 BM25 순위를
보존 → 자연스러운 graceful 폴백(= 현재 BM25-only 동작).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING

from core.wiki.search_index import (
    WikiSearchResult,
    _Candidate,
    _mmr_rerank,
    _rerank,
)

if TYPE_CHECKING:
    from config import WikiRankingConfig, WikiSemanticConfig

logger = logging.getLogger(__name__)


def _rrf_score(
    vector_rank: int | None,
    fts_rank: int | None,
    vector_weight: float,
    fts_weight: float,
    k: int,
) -> float:
    """RRF 점수. score = w_v/(k+rank_v) + w_f/(k+rank_f). 한쪽 None 이면 그 항은 0.

    `search/hybrid_search._compute_rrf_score` 와 동일 공식. core/wiki 를 search
    모듈의 무거운 임포트(model_manager·embedder)와 분리하기 위해 5줄 표준 공식을 복제.
    """
    score = 0.0
    if vector_rank is not None:
        score += vector_weight * (1.0 / (k + vector_rank))
    if fts_rank is not None:
        score += fts_weight * (1.0 / (k + fts_rank))
    return score


def fuse_and_rerank(
    bm25_ranked: list[tuple[str, int]],
    vector_ranked: list[tuple[str, int]],
    candidates_by_path: dict[str, _Candidate],
    *,
    semantic: WikiSemanticConfig,
    ranking: WikiRankingConfig,
    now: date,
    top_k: int,
) -> list[WikiSearchResult]:
    """BM25·벡터 랭킹을 RRF 로 융합하고 C1 재랭킹을 적용해 top_k 결과를 만든다.

    Args:
        bm25_ranked: (page_path, 1-based rank) BM25 순.
        vector_ranked: (page_path, 1-based rank) 벡터 순. 임베더 불가 시 [](graceful 폴백).
        candidates_by_path: page_path → 후보 메타. 랭킹에 있으나 여기 없으면 skip.
        semantic: RRF 가중치/파라미터.
        ranking: C1 다중신호 재랭킹 설정.
        now: recency 기준일.
        top_k: 반환 최대 수.

    Returns:
        융합·재랭킹된 WikiSearchResult 리스트(점수 내림차순, top_k).
    """
    bm25_rank = {path: rank for path, rank in bm25_ranked}
    vector_rank = {path: rank for path, rank in vector_ranked}

    candidates: list[_Candidate] = []
    for path in {*bm25_rank, *vector_rank}:
        cand = candidates_by_path.get(path)
        if cand is None:
            # 랭킹엔 있으나 메타 조회 실패 → 안전히 제외(검색은 fail-loud 아닌 graceful).
            logger.warning("위키 하이브리드: 후보 메타 없음, 제외 page=%s", path)
            continue
        rrf = _rrf_score(
            vector_rank.get(path),
            bm25_rank.get(path),
            semantic.vector_weight,
            semantic.fts_weight,
            semantic.rrf_k,
        )
        # 융합 점수를 '검색 관련도' 신호로 주입(_rerank 의 bm25 자리). 나머지 메타 보존.
        candidates.append(replace(cand, bm25=rrf))

    if not candidates:
        return []

    scored = _rerank(candidates, ranking, now)
    limit = max(1, int(top_k))
    if ranking.mmr_enabled:
        scored = _mmr_rerank(scored, ranking, limit)
    ordered = scored[:limit]

    return [
        WikiSearchResult(
            page_path=c.page_path,
            page_type=c.page_type,
            title=c.title,
            snippet=c.snippet,
            score=score,
            citations=c.citations,
            metadata=c.metadata,
        )
        for c, score in ordered
    ]
