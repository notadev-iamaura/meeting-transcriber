"""명시 검색어의 원문 근거를 한 개 보존하는 RRF 경계를 검사한다."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from search.hybrid_search import _combine_rrf, _lexical_anchors


def candidate(identity: str, text: str) -> dict[str, Any]:
    """서로 다른 가상 원문 후보를 만든다."""
    return {
        "chunk_id": identity,
        "text": text,
        "meeting_id": "meeting_allowed",
        "date": "2026-11-04",
        "speakers": "SPEAKER_01",
        "start_time": 100.25,
        "end_time": 110.5,
        "chunk_index": 2,
    }


def combine(
    query: str | None,
    fts: list[dict[str, Any]] | None = None,
    **options: Any,
) -> list[Any]:
    """기본 가중치의 벡터 후보 15개를 원문 후보와 결합한다."""
    return _combine_rrf(
        [candidate(f"vector_{index}", "semantic context") for index in range(15)],
        fts if fts is not None else [candidate("anchor", "Alpha and BETA")],
        vector_weight=0.6,
        fts_weight=options.pop("fts_weight", 0.4),
        rrf_k=60,
        top_k=options.pop("top_k", 5),
        query=query,
        **options,
    )


@pytest.mark.parametrize(
    "query",
    [
        "alpha beta",
        "ALPHA beta",
        "'ALPHA beta'와 관련된 내용을 알려줘",
        "검색어 'alpha beta'를 인용해 줘",
    ],
)
def test_missing_exact_anchor_replaces_one_slot_without_changing_scores(query: str) -> None:
    """원문 키워드가 잘려도 한 자리만 보존하고 점수와 메타데이터는 유지한다."""
    result = combine(query)
    assert len(result) == 5 and result[-1].chunk_id == "anchor"
    assert [row.chunk_id for row in result[:-1]] == [f"vector_{i}" for i in range(4)]
    baseline = {row.chunk_id: row.score for row in combine(None, top_k=20)}
    assert all(row.score == baseline[row.chunk_id] for row in result)
    assert result[-1].source == "fts"
    assert result[-1].meeting_id == "meeting_allowed"
    assert (result[-1].start_time, result[-1].end_time) == (100.25, 110.5)
    assert [row.score for row in result] == sorted((row.score for row in result), reverse=True)


def test_only_all_terms_match_can_be_retained_and_highest_fts_match_wins() -> None:
    """한 단어만 맞는 후보는 제외하고 FTS 순서의 첫 완전 일치만 보존한다."""
    fts = [
        candidate("partial", "alpha only"),
        candidate("best", "alpha beta"),
        candidate("other", "alpha beta"),
    ]
    before = deepcopy(fts)
    result = combine("alpha beta", fts)
    baseline = {row.chunk_id: row.score for row in combine(None, fts, top_k=20)}
    assert result[-1].chunk_id == "best" and result[-1].score == baseline["best"]
    assert fts == before


@pytest.mark.parametrize(
    "query",
    [
        None,
        "",
        "일반 자연어 질문에 대한 의미 검색",
        "'alpha'와 'beta'를 비교해 줘",
        "'{query}'를 찾아줘",
    ],
)
def test_unscoped_or_placeholder_queries_keep_original_rrf(query: str | None) -> None:
    """긴 자연어·여러 따옴표·placeholder에는 새로운 보존 규칙을 적용하지 않는다."""
    result = combine(query)
    assert [row.chunk_id for row in result] == [f"vector_{i}" for i in range(5)]


@pytest.mark.parametrize(
    ("field", "value", "options"),
    [
        ("meeting_id", "meeting_other", {"meeting_id_filter": "meeting_allowed"}),
        ("date", "2026-11-03", {"date_filter": "2026-11-04"}),
        ("speakers", "SPEAKER_02", {"speaker_filter": "SPEAKER_01"}),
    ],
)
def test_anchor_cannot_be_promoted_across_existing_filters(
    field: str, value: str, options: dict[str, str]
) -> None:
    """다른 회의·날짜·화자의 후보를 새 보존 규칙으로 승격하지 않는다."""
    raw = candidate("outside", "alpha beta")
    raw[field] = value
    assert all(row.chunk_id != "outside" for row in combine("alpha beta", [raw], **options))


def test_zero_fts_weight_empty_results_and_top_k_boundaries() -> None:
    """FTS 비활성·근거 없음·0개·1개 경계에서 정책을 지킨다."""
    assert all(row.chunk_id != "anchor" for row in combine("alpha beta", fts_weight=0))
    assert all(row.chunk_id != "anchor" for row in combine("alpha beta", []))
    assert combine("alpha beta", top_k=0) == []
    assert [row.chunk_id for row in combine("alpha beta", top_k=1)] == ["anchor"]


def test_existing_anchor_in_top_k_preserves_original_selection() -> None:
    """기존 top-k가 이미 원문을 포함하면 순위와 개수를 바꾸지 않는다."""
    raw = [candidate("vector_0", "alpha beta")]
    assert [row.chunk_id for row in combine("alpha beta", raw)] == [
        f"vector_{i}" for i in range(5)
    ]


def test_anchor_terms_preserve_korean_and_casefold() -> None:
    """명시 주제의 한국어와 영문 검색어를 정규화한다."""
    assert _lexical_anchors("'GPU 트래킹'과 관련된 발언") == ["gpu", "트래킹"]


def test_ascii_anchor_cannot_match_inside_an_unrelated_word() -> None:
    """다른 FTS 단어만 맞고 영문 검색어는 긴 단어 일부일 때 승격하지 않는다."""
    raw = [candidate("partial_ascii", "capitol data")]
    assert all(row.chunk_id != "partial_ascii" for row in combine("api data", raw))
