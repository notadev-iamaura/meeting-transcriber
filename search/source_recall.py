"""사실 답변에 필요한 같은 회의의 실제 색인 인접·원인·후속 구간만 보충한다."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.io_utils import _open_directory_no_follow
from core.reindex_recovery import _configured_storage_root
from core.search_revision import filter_current_results
from core.speaker_labels import read_speaker_labels
from search.hybrid_search import SearchResult
from search.source_query import content_terms, cross_meeting_comparison

logger = logging.getLogger(__name__)
_ACTION_SEARCH = '"조치"* OR "액션"* OR "해결"* OR "완화"* OR "중재"*'


def _intent_searches(query: str) -> list[str]:
    """질문이 요구한 원인·시간순서·요청·후속을 서로 다른 보충 축으로 유지한다."""
    searches = []
    causal = re.search(r"원인|분석|장애|재발|조치|\bRCA\b", query, re.IGNORECASE) is not None
    if causal:
        searches.append('"RCA" OR "원인"* OR "분석"* OR "재발"* OR "조치"*')
        if re.search(r"완료|끝|이미|앞으로", query):
            # 조치 관련 구간을 따로 찾을 뿐 완료 여부를 키워드로 판정하지 않는다.
            searches.append(_ACTION_SEARCH)
    if causal or re.search(r"타임라인|경위|시각|시간대|순서|시점", query):
        searches.append('"타임라인"* OR "경위"* OR "순서"*')
        searches.append('"발생"* OR "시각"* OR "시간대"*')
    if causal or re.search(r"요청|부탁|담당", query):
        searches.append('"요청"* OR "부탁"* OR "해달"* OR "해주"* OR "하라"* OR "해보"*')
    if re.search(r"후속|공유|회신|보고|일정|언제|확인|하기로|완료", query):
        searches.append('"공유"* OR "회신"* OR "보고"* OR "일정"* OR "확인"* OR "계획"*')
    return searches


def _assert_index_stable(path: Path, parent_fd: int, file_fd: int) -> None:
    """읽기 전후 lexical 부모 체인·일반 파일 inode가 고정 FD와 같은지 재확인한다."""
    current_parent = _open_directory_no_follow(path.parent)
    try:
        expected_parent, actual_parent = os.fstat(parent_fd), os.fstat(current_parent)
        pinned, entry = (
            os.fstat(file_fd),
            os.stat(path.name, dir_fd=current_parent, follow_symlinks=False),
        )
        if (
            (expected_parent.st_dev, expected_parent.st_ino)
            != (actual_parent.st_dev, actual_parent.st_ino)
            or not stat.S_ISREG(entry.st_mode)
            or (pinned.st_dev, pinned.st_ino) != (entry.st_dev, entry.st_ino)
        ):
            raise OSError("source_index_namespace_changed")
    finally:
        os.close(current_parent)


@contextmanager
def _readonly_index(config: Any) -> Iterator[sqlite3.Connection]:
    """기존 lexical/no-follow 경계를 재사용해 DB를 읽기 전용으로 열고 inode를 재확인한다."""
    path = _configured_storage_root(config, "meetings_db", config.paths.resolved_meetings_db)
    parent_fd = _open_directory_no_follow(path.parent)
    file_fd: int | None = None
    connection: sqlite3.Connection | None = None
    try:
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise OSError("source_index_not_regular")
        _assert_index_stable(path, parent_fd, file_fd)
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        _assert_index_stable(path, parent_fd, file_fd)
        yield connection
        _assert_index_stable(path, parent_fd, file_fd)
    finally:
        if connection is not None:
            connection.close()
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)


def _read_supplement(
    config: Any,
    query: str,
    results: list[SearchResult],
    *,
    meeting_id_filter: str | None,
    date_filter: str | None,
    speaker_filter: str | None,
    max_chunks: int,
    neighbor_window: int,
) -> list[SearchResult]:
    """원래 RRF 순서를 보존하고 읽기 전용 FTS의 유효한 실제 청크만 덧붙인다."""
    results = [
        row
        for row in results
        if (meeting_id_filter is None or row.meeting_id == meeting_id_filter)
        and (date_filter is None or row.date == date_filter)
        and (speaker_filter is None or speaker_filter in row.speakers)
    ]
    intent_searches = _intent_searches(query)
    if not results or max_chunks <= 1 or (len(results) >= max_chunks and not intent_searches):
        return list(results[:max_chunks])
    # explicit 회의가 없을 때에는 기존 검색이 선택한 첫 회의 안에서만 문맥을 보충한다.
    mid = meeting_id_filter or results[0].meeting_id
    anchors = [r for r in results if r.meeting_id == mid]
    if not anchors:
        return list(results[:max_chunks])
    columns = "chunk_id, text, meeting_id, date, speakers, start_time, end_time, chunk_index"
    conditions = "meeting_id = ?"
    filters: list[Any] = [mid]
    if date_filter:
        conditions += " AND date = ?"
        filters.append(date_filter)
    if speaker_filter:
        conditions += " AND instr(',' || speakers || ',', ?) > 0"
        filters.append(f",{speaker_filter},")
    try:
        with _readonly_index(config) as conn:
            # 명시한 주제는 AND 검색으로 좁히되 기본 검색·모델의 의미 해석을 대체하지 않는다.
            quoted = re.findall(r"(?<!\w)['\"‘“]([^'\"’”\r\n]+)['\"’”]", query)
            searches = list(intent_searches)
            if len(quoted) == 1:
                tokens = re.findall(r"[^\W_]+", quoted[0], flags=re.UNICODE)
                if tokens:
                    searches.append(" AND ".join(f'"{token}"' for token in tokens))
            # 질문에 실제 들어간 내용 단어를 독립 facet으로 보충한다. 샘플 주제 사전은 만들지 않는다.
            searches.extend(f'"{term}"*' for term in content_terms(query)[:max_chunks])
            facet_rows: list[list[dict[str, Any]]] = []
            for search in searches:
                rows = conn.execute(
                    f"SELECT {columns} FROM chunks_fts WHERE (chunks_fts MATCH ?) AND ({conditions}) ORDER BY bm25(chunks_fts) LIMIT ?",
                    [search, *filters, max_chunks],
                ).fetchall()
                facet_rows.append([dict(row) for row in rows])
            # 바로 이어진 응답이 앞 문맥보다 먼저 들어가도록 다음 청크를 우선한다.
            neighbors: list[dict[str, Any]] = []
            # 새 조치 anchor의 경계에서 이어지는 발언도 기존 인접 범위 안에서 보존한다.
            if _ACTION_SEARCH in intent_searches and neighbor_window > 0:
                action_rows = filter_current_results(
                    config, facet_rows[intent_searches.index(_ACTION_SEARCH)]
                )
                if action_rows:
                    action_index = int(action_rows[0]["chunk_index"])
                    for offset in range(1, neighbor_window + 1):
                        for chunk_index in (action_index + offset, action_index - offset):
                            if chunk_index >= 0:
                                rows = conn.execute(
                                    f"SELECT {columns} FROM chunks_fts WHERE {conditions} AND CAST(chunk_index AS INTEGER) = ? LIMIT ?",
                                    [*filters, chunk_index, max_chunks],
                                ).fetchall()
                                neighbors.extend(dict(row) for row in rows)
            for offset in range(1, neighbor_window + 1):
                for anchor in anchors:
                    for chunk_index in (anchor.chunk_index + offset, anchor.chunk_index - offset):
                        if chunk_index >= 0:
                            rows = conn.execute(
                                f"SELECT {columns} FROM chunks_fts WHERE {conditions} AND CAST(chunk_index AS INTEGER) = ? LIMIT ?",
                                [*filters, chunk_index, max_chunks],
                            ).fetchall()
                            neighbors.extend(dict(row) for row in rows)
            # 서로 다른 facet·인접 문맥을 보존하고 하나의 긴 facet이 나머지 자리를 독점하지 않는다.
            additions = []
            queues = [*[list(group) for group in facet_rows], list(neighbors)]
            while any(queues):
                for values in queues:
                    if values:
                        additions.append(values.pop(0))
        valid = filter_current_results(config, additions)
        labels = read_speaker_labels(config, [mid]).get(mid, {})
        output = list(results[:max_chunks])
        originals = {r.chunk_id: r for r in results}
        converted: dict[str, SearchResult] = {}
        for row in valid:
            if row["chunk_id"] in converted:
                continue
            speakers = row["speakers"]
            converted[row["chunk_id"]] = originals.get(row["chunk_id"]) or SearchResult(
                chunk_id=row["chunk_id"],
                text=row["text"],
                score=0.0,
                meeting_id=row["meeting_id"],
                date=row["date"],
                speakers=[s for s in speakers.split(",") if s],
                start_time=float(row["start_time"]),
                end_time=float(row["end_time"]),
                chunk_index=int(row["chunk_index"]),
                source="fts",
                speaker_labels=dict(labels),
            )
        # 상위 anchor는 유지하되 필요한 축이 없으면 문맥 끝의 비보호 슬롯 하나만 쓴다.
        # 이는 이 답변의 문맥 선택이며 원 검색의 RRF 점수·순서는 바꾸지 않는다.
        protected = {output[0].chunk_id}
        for group in facet_rows[: len(intent_searches)]:
            candidates = [
                converted[row["chunk_id"]] for row in group if row["chunk_id"] in converted
            ]
            covered = next(
                (r for r in output if candidates and r.chunk_id == candidates[0].chunk_id), None
            )
            if covered is not None:
                protected.add(covered.chunk_id)
                continue
            if not candidates:
                continue
            if len(output) >= max_chunks:
                replace = next(
                    (
                        i
                        for i in range(len(output) - 1, -1, -1)
                        if output[i].chunk_id not in protected
                    ),
                    None,
                )
                if replace is None:
                    continue
                output.pop(replace)
            output.append(candidates[0])
            protected.add(candidates[0].chunk_id)
        seen = {r.chunk_id for r in output}
        supplement_order = dict.fromkeys([row["chunk_id"] for row in neighbors] + list(converted))
        for chunk_id in supplement_order:
            if chunk_id not in converted:
                continue
            candidate = converted[chunk_id]
            if chunk_id not in seen and len(output) < max_chunks:
                output.append(candidate)
                seen.add(chunk_id)
        # 남은 기존 청크끼리의 상대 순서와 객체·메타데이터는 그대로 유지한다.
        original_order = {r.chunk_id: i for i, r in enumerate(results)}
        output.sort(key=lambda r: original_order.get(r.chunk_id, len(results)))
        return output
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        logger.warning(f"같은 회의 원문 문맥 보충 실패: {type(exc).__name__}")
        return list(results[:max_chunks])


def _read_comparison_supplement(
    config: Any,
    query: str,
    results: list[SearchResult],
    *,
    meeting_id_filter: str | None,
    date_filter: str | None,
    speaker_filter: str | None,
    max_chunks: int,
    neighbor_window: int,
) -> list[SearchResult]:
    """이미 검색된 비교 회의별로 예산을 나눠 한 회의의 보충이 다른 회의를 밀어내지 않는다."""
    groups: dict[str, list[SearchResult]] = {}
    for row in results:
        if (
            (meeting_id_filter is not None and row.meeting_id != meeting_id_filter)
            or (date_filter is not None and row.date != date_filter)
            or (speaker_filter is not None and speaker_filter not in row.speakers)
        ):
            continue
        groups.setdefault(row.meeting_id, []).append(row)
    if not groups or len(groups) > max_chunks:
        return []
    output: dict[str, SearchResult] = {}
    for index, (mid, rows) in enumerate(groups.items()):
        budget = max_chunks // len(groups) + int(index < max_chunks % len(groups))
        for row in _read_supplement(
            config,
            query,
            rows,
            meeting_id_filter=mid,
            date_filter=date_filter,
            speaker_filter=speaker_filter,
            max_chunks=budget,
            neighbor_window=neighbor_window,
        ):
            output.setdefault(row.chunk_id, row)
    order = {row.chunk_id: index for index, row in enumerate(results)}
    return sorted(output.values(), key=lambda row: order.get(row.chunk_id, len(results)))


async def supplement_results(
    config: Any, query: str, results: list[SearchResult], **kwargs: Any
) -> list[SearchResult]:
    """기존 검색·필터·색인 버전을 바꾸지 않고 로컬 I/O를 서버 루프 밖에서 수행한다."""
    reader = (
        _read_comparison_supplement
        if cross_meeting_comparison(query) and not kwargs.get("meeting_id_filter")
        else _read_supplement
    )
    return await asyncio.to_thread(reader, config, query, results, **kwargs)
