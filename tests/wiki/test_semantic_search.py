"""G1 위키 하이브리드(벡터+BM25 RRF) 융합 + C1 재랭킹 합성 테스트.

순수 함수 `fuse_and_rerank` 를 검증한다 — ChromaDB/임베더 없이 BM25 랭킹·벡터
랭킹·후보 메타를 plain 데이터로 주입해 결정적으로 검증한다(불변식: 로컬·시크릿/
네이티브모델 없이 검증).

검증 포인트:
    - 벡터 전용 매치(어휘 비매칭)가 결과에 유입된다.
    - graceful 폴백(벡터 0건) = BM25-only 순서.
    - RRF 융합: 양쪽 상위가 한쪽 상위보다 위.
    - C1 재랭킹 신호(superseded 구조적 하향)가 융합 후에도 보존.
"""

from __future__ import annotations

from datetime import date

from config import WikiRankingConfig, WikiSemanticConfig
from core.wiki.search_index import _Candidate
from core.wiki.semantic_search import fuse_and_rerank

_NOW = date(2026, 6, 1)


def _cand(
    path: str,
    *,
    status: str = "decided",
    decision_date: str = "2026-05-21",
    confidence: int = 9,
    citation_count: int = 1,
) -> _Candidate:
    """융합 입력용 후보 메타(검색 신호는 동일하게 두어 RRF 효과를 격리)."""
    return _Candidate(
        page_path=path,
        page_type="decision",
        title=path,
        snippet="예산 합의",
        bm25=0.0,  # 융합에서 RRF 점수로 대체됨
        status=status,
        decision_date=decision_date,
        last_updated=decision_date,
        confidence=confidence,
        citations=["[meeting:1234abcd@00:01:20]"] * citation_count,
        citation_count=citation_count,
        metadata={"status": status, "decision_date": decision_date},
    )


def _fuse(bm25_ranked, vector_ranked, cands, *, ranking=None, top_k=10):
    """기본 semantic/ranking 설정으로 fuse_and_rerank 호출 헬퍼."""
    by_path = {c.page_path: c for c in cands}
    return fuse_and_rerank(
        bm25_ranked,
        vector_ranked,
        by_path,
        semantic=WikiSemanticConfig(),
        ranking=ranking or WikiRankingConfig(),
        now=_NOW,
        top_k=top_k,
    )


def test_벡터_전용_매치가_결과에_유입된다() -> None:
    """BM25 0건이지만 벡터가 찾은 페이지가 결과에 포함된다(시맨틱 회상 핵심)."""
    cands = [_cand("decisions/a.md"), _cand("decisions/b.md")]
    # a.md 만 BM25 매치, b.md 는 벡터 전용
    results = _fuse(
        bm25_ranked=[("decisions/a.md", 1)],
        vector_ranked=[("decisions/a.md", 2), ("decisions/b.md", 1)],
        cands=cands,
    )

    paths = [r.page_path for r in results]
    assert "decisions/b.md" in paths  # 벡터 전용 매치 유입
    assert "decisions/a.md" in paths


def test_벡터_0건이면_BM25_순서로_graceful_폴백한다() -> None:
    """임베더 불가(vector_ranked=[]) → 순수 BM25 순서(현재 동작)와 동일."""
    cands = [_cand("decisions/a.md"), _cand("decisions/b.md"), _cand("decisions/c.md")]
    results = _fuse(
        bm25_ranked=[("decisions/a.md", 1), ("decisions/b.md", 2), ("decisions/c.md", 3)],
        vector_ranked=[],  # 폴백
        cands=cands,
    )

    # 메타 동일 → RRF(=FTS only) 가 BM25 순위를 보존
    assert [r.page_path for r in results] == [
        "decisions/a.md",
        "decisions/b.md",
        "decisions/c.md",
    ]


def test_RRF는_양쪽_상위를_한쪽_상위보다_위로_올린다() -> None:
    """양쪽(벡터+BM25)에서 상위인 페이지가 한쪽에서만 상위인 페이지보다 위."""
    cands = [
        _cand("decisions/both.md"),
        _cand("decisions/bm25only.md"),
        _cand("decisions/veconly.md"),
    ]
    results = _fuse(
        bm25_ranked=[("decisions/both.md", 1), ("decisions/bm25only.md", 2)],
        vector_ranked=[("decisions/both.md", 1), ("decisions/veconly.md", 2)],
        cands=cands,
    )

    assert results[0].page_path == "decisions/both.md"


def test_superseded_구조적_하향이_융합_후에도_보존된다() -> None:
    """RRF 1위라도 superseded 면 C1 구조적 floor 로 live 결정 아래."""
    cands = [
        _cand("decisions/superseded.md", status="superseded"),
        _cand("decisions/live.md", status="decided"),
    ]
    # superseded 가 양쪽 1위(RRF 최상)지만 패널티로 하향되어야
    results = _fuse(
        bm25_ranked=[("decisions/superseded.md", 1), ("decisions/live.md", 2)],
        vector_ranked=[("decisions/superseded.md", 1), ("decisions/live.md", 2)],
        cands=cands,
    )

    assert results[0].page_path == "decisions/live.md"
    assert results[-1].page_path == "decisions/superseded.md"


def test_후보_메타가_없는_경로는_건너뛴다() -> None:
    """랭킹 리스트에 있으나 메타 조회 실패한 page_path 는 안전히 제외(크래시 없음)."""
    cands = [_cand("decisions/a.md")]
    results = _fuse(
        bm25_ranked=[("decisions/a.md", 1), ("decisions/missing.md", 2)],
        vector_ranked=[("decisions/missing.md", 1)],
        cands=cands,
    )

    assert [r.page_path for r in results] == ["decisions/a.md"]


def test_빈_입력은_빈_결과를_반환한다() -> None:
    """양쪽 모두 비면 빈 리스트."""
    assert _fuse(bm25_ranked=[], vector_ranked=[], cands=[]) == []


def test_ranking_enabled_False면_재랭킹없이_융합점수순_escape_hatch() -> None:
    """재랭킹 OFF: superseded 구조적 floor 미적용, 융합점수(=BM25 순위) 순서 유지."""
    cands = [
        _cand("decisions/super.md", status="superseded"),
        _cand("decisions/live.md"),
    ]
    results = _fuse(
        bm25_ranked=[("decisions/super.md", 1), ("decisions/live.md", 2)],
        vector_ranked=[],
        cands=cands,
        ranking=WikiRankingConfig(enabled=False),
    )

    # enabled=False → floor 강등 없음 → BM25 1위인 superseded 가 그대로 1위
    assert results[0].page_path == "decisions/super.md"


def test_top_k_절단이_적용된다() -> None:
    """top_k 로 결과 수를 제한한다."""
    cands = [_cand(f"decisions/{i}.md") for i in range(5)]
    results = _fuse(
        bm25_ranked=[(f"decisions/{i}.md", i + 1) for i in range(5)],
        vector_ranked=[],
        cands=cands,
        top_k=2,
    )

    assert len(results) == 2
