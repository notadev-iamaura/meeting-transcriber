"""전체 회의의 주제 검색 결과를 회의별로 묶고 관련 논의의 원문을 구성한다."""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from itertools import pairwise

from config import AppConfig
from core.search_revision import filter_current_results, read_source
from core.speaker_labels import read_speaker_labels
from search.hybrid_search import SearchResult
from search.source_answers import SourceCandidate, build_source_candidates
from search.source_query import topic_content_terms
from search.source_recall import _readonly_index

logger = logging.getLogger(__name__)

_COLUMNS = "chunk_id, text, meeting_id, date, speakers, start_time, end_time, chunk_index"
_OUTCOME = re.compile(
    r"하기로|키로\s*했|하겠습니다|하겠습|할게요|드릴게요|해볼게요|공유드|요청드|"
    r"결정|확정|잠정|보류|합의|예정|계획|목표|마감|기한|얼마나\s*걸|언제까지|"
    r"이번\s*주|다음\s*(?:주|달)|내일|요일|\d+\s*(?:월|일|주|시|개월|분기)"
)


def _rows(config: AppConfig, rows: Iterable[sqlite3.Row]) -> list[SearchResult]:
    """현재 색인 영수증이 인정하는 청크만 원래 메타데이터와 함께 돌려준다."""
    records = filter_current_results(config, [dict(row) for row in rows])
    labels = read_speaker_labels(config, {r["meeting_id"] for r in records})
    return [
        SearchResult(
            chunk_id=r["chunk_id"],
            text=r["text"],
            score=0.0,
            meeting_id=r["meeting_id"],
            date=r["date"],
            speakers=[s for s in r["speakers"].split(",") if s],
            start_time=float(r["start_time"]),
            end_time=float(r["end_time"]),
            chunk_index=int(r["chunk_index"]),
            source="fts",
            speaker_labels=labels.get(r["meeting_id"], {}),
        )
        for r in records
    ]


def _term_matches(terms: list[re.Pattern[str]], text: str) -> set[int]:
    """영문 약어의 부분 문자열 오인과 한글의 조사 차이를 구분한다."""
    text = unicodedata.normalize("NFC", text).casefold()
    return {i for i, term in enumerate(terms) if term.search(text)}


def _pack(
    turns: list[SourceCandidate],
    terms: list[re.Pattern[str]],
    weights: list[float],
    max_chars: int,
    max_turns: int,
) -> list[SourceCandidate]:
    """관련 발화와 그 뒤의 약속·일정을 우선 보존하고 원래 시간 순서로 표시한다."""
    original = [t for t in turns if not t.context_only]
    turns = original
    if not turns:
        return []
    strength = {
        t.source_index: sum(weights[i] for i in _term_matches(terms, t.quote.text)) for t in turns
    }
    strongest = max(strength.values(), default=0)
    anchors = [idx for idx, value in strength.items() if value and value >= strongest * 0.5]
    if anchors:
        # 지시어로 이어지는 결론을 남기되 같은 회의 안에서도 멀리 떨어진 안건은 빼낸다.
        turns = [t for t in turns if any(-4 <= t.source_index - a <= 14 for a in anchors)]

    def priority(t: SourceCandidate) -> float:
        # 뒤에서 주어를 생략하고 답하는 발화도 가까운 주제 발화의 문맥으로 인정한다.
        distance = min((abs(t.source_index - i) for i in anchors), default=0)
        context = 1 / (1 + distance / 5)
        density = sum(strength[a] / (1 + abs(a - t.source_index)) for a in anchors)
        return (
            strength[t.source_index]
            + density
            + context * (1.5 if _OUTCOME.search(t.quote.text) else 1)
        )

    ordered = sorted(turns, key=priority, reverse=True)
    # 주제어를 반복하는 배경 설명이 짧은 약속과 간접적인 일정 답변을 밀어내지 않게 한다.
    outcomes = [t for t in ordered if _OUTCOME.search(t.quote.text)][: max_turns // 3]

    def row_size(turn: SourceCandidate) -> int:
        return 1 + len(
            json.dumps(
                [turn.id, turn.display_speaker, turn.quote.text],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    selected: list[SourceCandidate] = []
    used = 2
    for turn in dict.fromkeys([*ordered[:1], *outcomes, *ordered]):
        size = row_size(turn)
        if used + size <= max_chars:
            selected.append(turn)
            used += size
            if len(selected) == max_turns:
                break
    # 가까운 두 근거 사이를 통째로 읽을 수 있을 때만 보충한다. 숫자와 조치를
    # 중간 발언에서 설명하는 경우에도 모델이 앞뒤를 별개 안건으로 오해하지 않게 한다.
    positions = sorted({t.source_index for t in selected})
    for left, right in sorted(pairwise(positions), key=lambda pair: pair[1] - pair[0]):
        if not 1 < right - left <= 12:
            continue
        bridge = [t for t in original if left < t.source_index < right]
        cost = sum(row_size(t) for t in bridge)
        if used + cost <= max_chars:
            selected.extend(bridge)
            used += cost
    return sorted(selected, key=lambda t: (t.source_index, t.id))


def any_indexed_topic_term(config: AppConfig, topic: str) -> bool | None:
    """조회가 성공했을 때만 식별 단어가 전사 색인에 실제로 존재하는지 알려 준다."""
    from search.topic_minutes import _terms

    terms = _terms(topic)
    if not terms:
        return None
    escaped = [term.replace('"', '""') for term in terms]
    expression = " OR ".join(f'text : "{term}"*' for term in escaped)
    try:
        with _readonly_index(config) as db:
            return (
                db.execute(
                    "SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT 1", (expression,)
                ).fetchone()
                is not None
            )
    except (OSError, sqlite3.Error, ValueError):
        return None


def topic_evidence(
    config: AppConfig, topic: str, hits: list[SearchResult], max_chars: int
) -> list[tuple[list[SearchResult], list[SourceCandidate]]]:
    """혼합 검색과 주제 단어의 문서 빈도로 회의를 고르고 각 회의 안의 근거를 분리한다."""
    limit = config.chat.source_context_max_chunks
    words = [w.casefold() for w in topic_content_terms(topic)[:limit]]
    specific = [
        w for w in words if w not in {"관련", "작업", "논의", "진행", "사항", "건", "내용", "이슈"}
    ]
    words = specific or words
    patterns = [
        re.compile(
            r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])" if w.isascii() else re.escape(w)
        )
        for w in words
    ]
    weights = [1.0] * len(words)
    frequencies: dict[int, int] = {}
    found = {r.chunk_id: r for r in hits}
    try:
        with _readonly_index(config) as db:
            extra: dict[str, sqlite3.Row] = {}
            for i, word in enumerate(words):
                match = f'text : "{word}"*'
                count = db.execute(
                    "SELECT count(DISTINCT meeting_id) FROM chunks_fts WHERE chunks_fts MATCH ?",
                    (match,),
                ).fetchone()[0]
                weights[i] = 1 / math.log2(2 + count)
                if count:
                    frequencies[i] = count
                for row in db.execute(
                    f"SELECT {_COLUMNS} FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
                    (match, limit),
                ):
                    extra[row["chunk_id"]] = row
            for result in _rows(config, extra.values()):
                found.setdefault(result.chunk_id, result)
    except (OSError, sqlite3.Error, ValueError) as exc:
        logger.warning(f"주제 검색의 추가 키워드 근거 조회 실패: {type(exc).__name__}")

    groups: dict[str, list[SearchResult]] = defaultdict(list)
    for row in found.values():
        groups[row.meeting_id].append(row)
    relevance = {}
    for row in found.values():
        body = unicodedata.normalize("NFC", row.text).casefold()
        # 같은 낱말이 여러 차례 등장한 토론과 다른 안건에서 한 번 스쳐 간 언급을 구별한다.
        relevance[row.chunk_id] = sum(
            weight * (1 - 0.5 ** len(pattern.findall(body)))
            for pattern, weight in zip(patterns, weights, strict=True)
        )
    scores: dict[str, float] = {}
    for mid, parents in groups.items():
        parents.sort(key=lambda r: (relevance[r.chunk_id], r.score), reverse=True)
        # 가장 밀도 높은 토론을 우선하고, 반복해서 다룬 회의에만 가점을 준다.
        scores[mid] = sum(relevance[r.chunk_id] / (3**i) for i, r in enumerate(parents[:3]))
    best = max(scores.values(), default=0)
    ranked = sorted(groups, key=lambda mid: (scores[mid], groups[mid][0].score), reverse=True)
    ranked = [mid for mid in ranked if not best or scores[mid] >= best * 0.55]
    phrase_match = False
    if ranked and len(words) >= 4:
        # 긴 주제에 들어 있는 연속된 표현은 따로 흩어진 단어보다 특정 안건을 잘 구별한다.
        def phrases(size: int) -> re.Pattern[str]:
            return re.compile(
                "|".join(
                    r"[\s·/-]*".join(re.escape(w) for w in words[i : i + size])
                    for i in range(len(words) - size + 1)
                ),
                re.IGNORECASE,
            )

        triples = phrases(3)
        phrase_match = any(triples.search(r.text) for r in groups[ranked[0]])
        if phrase_match:
            pairs = phrases(2)
            ranked = [mid for mid in ranked if any(pairs.search(r.text) for r in groups[mid])]
    if ranked and len(frequencies) >= 3:
        # 널리 쓰는 단어가 같은 회의와 드문 식별 단서까지 함께 논의한 회의를 구분한다.
        distinctive = {i for i, n in frequencies.items() if n * 3 <= max(frequencies.values())}
        if distinctive:
            covered = {
                mid: set.union(*(_term_matches(patterns, r.text) for r in groups[mid]))
                for mid in ranked
            }
            if set(frequencies) <= covered[ranked[0]]:
                ranked = [mid for mid in ranked if distinctive & covered[mid]]
    if (
        hits
        and best
        and not phrase_match
        and not any(len(_term_matches(patterns, r.text)) == len(words) for r in found.values())
    ):
        # 일부 표현만 일치하는 상황에서는 상위 의미 검색 결과도 원문 판단을 거친다.
        semantic = next((r for r in hits if r.source in {"both", "vector"}), None)
        if semantic is not None and semantic.meeting_id not in ranked[:2]:
            ranked = [mid for mid in ranked if mid != semantic.meeting_id]
            ranked.insert(1, semantic.meeting_id)
    output = []
    for mid in ranked:
        anchors = [r for r in groups[mid] if relevance[r.chunk_id]] or groups[mid]
        parents = _expand(config, anchors, limit)
        turns = _pack(
            build_source_candidates(config, parents), patterns, weights, max_chars, 4 * limit
        )
        if turns:
            output.append((parents, turns))
        if len(output) >= min(3, config.chat.source_focus_max_calls):
            break
    return output


def _expand(config: AppConfig, anchors: list[SearchResult], limit: int) -> list[SearchResult]:
    """같은 회의의 인접 청크를 확장하되 다른 회의에서 문맥을 가져오지 않는다."""
    anchors = anchors[:limit]
    by_index = {r.chunk_index: r for r in anchors}
    radius = config.chat.source_neighbor_window
    indices = list(
        dict.fromkeys(
            r.chunk_index + offset
            for offset in (0, *range(1, 2 * radius + 1), *range(-1, -radius - 1, -1))
            for r in anchors
            if r.chunk_index + offset >= 0
        )
    )[:limit]
    missing = [i for i in indices if i not in by_index]
    if missing:
        try:
            slots = ",".join("?" for _ in missing)
            with _readonly_index(config) as db:
                rows = db.execute(
                    f"SELECT {_COLUMNS} FROM chunks_fts WHERE meeting_id = ? AND CAST(chunk_index AS INTEGER) IN ({slots})",
                    (anchors[0].meeting_id, *missing),
                )
                for row in _rows(config, rows):
                    by_index[row.chunk_index] = row
        except (OSError, sqlite3.Error, ValueError) as exc:
            logger.warning(f"주제 인접 원문 조회 실패: {type(exc).__name__}")
    return [by_index[i] for i in sorted(indices) if i in by_index]


def agenda_evidence(
    config: AppConfig, meeting_id: str, turn_indices: tuple[int, ...]
) -> tuple[list[SearchResult], list[SourceCandidate]]:
    """안건에 속한 현재 전사 범위를 덮는 색인 청크와 검증된 발언을 읽는다."""
    if not turn_indices:
        return [], []
    try:
        _, source = read_source(config, meeting_id)
        utterances = source["utterances"]
        if min(turn_indices) < 0 or min(turn_indices) >= len(utterances):
            return [], []
        start = float(utterances[min(turn_indices)]["start"])
        end = float(utterances[min(max(turn_indices), len(utterances) - 1)]["end"])
        with _readonly_index(config) as db:
            parents = _rows(
                config,
                db.execute(
                    f"SELECT {_COLUMNS} FROM chunks_fts WHERE meeting_id = ? "
                    "AND CAST(end_time AS REAL) >= ? AND CAST(start_time AS REAL) <= ? "
                    "ORDER BY CAST(chunk_index AS INTEGER)",
                    (meeting_id, start, end),
                ),
            )
    except (OSError, sqlite3.Error, ValueError, IndexError, KeyError, TypeError):
        return [], []
    indices = set(turn_indices)
    return parents, [
        t for t in build_source_candidates(config, parents) if t.source_index in indices
    ]
