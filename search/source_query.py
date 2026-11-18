"""사실 질문의 실제 회의 범위와 내용 주제를 지시문·날짜 검색어에서 분리한다."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, time
from typing import Any

from core.io_utils import read_text_no_follow
from core.search_revision import (
    checkpoint_dir,
    filter_current_results,
    read_source,
    revision_status,
)
from search.hybrid_search import SearchResult

_MEETING_DATE = re.compile(
    r"(?P<year>\d{4})(?:년|[-./])\s*(?P<month>\d{1,2})(?:월|[-./])\s*"
    r"(?P<day>\d{1,2})일?\s*(?:(?P<hour>\d{1,2})\s*[:시]\s*(?P<minute>\d{1,2})분?)?\s*회의"
)
_TIMESTAMPED_MEETING_ID = re.compile(
    r"meeting_(\d{8})_(\d{2})(\d{2})(\d{2})(?:_zoom_[0-9a-f]{32})?"
)
_QUOTED = re.compile(r"(?<!\w)['\"‘“]([^'\"’”\r\n]+)['\"’”]")
_NOISE = frozenset(
    (
        "최근",
        "최신",
        "현재",
        "확정",
        "결정",
        "관련",
        "내용",
        "무엇",
        "어떻게",
        "누가",
        "누구",
        "언제",
        "어디",
        "어떤",
        "근거",
        "인용",
        "원문",
        "그대로",
        "문장",
        "번호",
        "참조",
        "추측",
        "정리",
        "설명",
        "알려",
        "주세요",
        "주세",
        "부탁",
        "구분",
        "대해",
        "회의",
        "기준",
        "후속",
        "질문",
        "이미",
        "앞으로",
        "한",
        "할",
        "일",
        "끝",
        "끝난",
        "건지",
        "건가",
        "아니면",
        "있었던",
        "없는",
        "못한",
        "있는",
        "향후",
        "확인할",
        "확인",
        "하나",
        "한번",
        "한문장",
        "이",
        "그",
        "뒤",
        "때",
        "수",
        "있",
        "없",
        "또",
        "그럼",
        "그건",
        "는",
        "은",
        "를",
        "가",
        "발언",
        "검색어",
        "하거나",
        "바꿔",
        "쓰지",
        "마세",
        "마세요",
    )
)
_SUFFIXES = tuple(
    sorted(
        (
            "해주세요",
            "인가요",
            "일까요",
            "하나요",
            "했나요",
            "해요",
            "하세요",
            "주세요",
            "으로",
            "까지",
            "에서",
            "에게",
            "관련",
            "대해",
            "하고",
            "해서",
            "하면",
            "인지",
            "이나",
            "이라도",
            "의",
            "은",
            "는",
            "을",
            "를",
            "이",
            "가",
            "과",
            "와",
            "에",
            "만",
            "도",
            "한",
            "된",
            "될",
            "할",
            "나",
        ),
        key=len,
        reverse=True,
    )
)


def content_terms(query: str) -> list[str]:
    """인용 주제를 우선하고 조사·요청 문구만 제거한 실제 입력 단어를 돌려준다."""
    query = unicodedata.normalize("NFC", query)
    query = _MEETING_DATE.sub(" ", query)
    query = re.sub(r"\[참조\s+N\]", " ", query, flags=re.IGNORECASE)
    quoted = _QUOTED.findall(query)
    pieces = [*quoted, _QUOTED.sub(" ", query)]
    output: list[str] = []
    for piece in pieces:
        for word in re.findall(r"[^\W_]+", piece, re.UNICODE):
            if re.fullmatch(
                r"(?:정리|설명|알려|구분|인용|추측)(?:해|하여|해줘|해주세요|하고|하거나|하|할)?",
                word,
            ) or word in {"일과", "일을"}:
                continue
            stem = word
            for suffix in _SUFFIXES:
                if stem.endswith(suffix) and len(stem) - len(suffix) >= 2:
                    stem = stem[: -len(suffix)]
                    break
            if stem in _NOISE or word in _NOISE or not stem:
                continue
            if stem.casefold() not in {v.casefold() for v in output}:
                output.append(stem)
    return output


def focused_source_query(query: str, max_terms: int) -> str:
    """검색 지시와 날짜가 드문 내용 단어를 압도하지 않도록 내용 주제만 구성한다."""
    quotes = _QUOTED.findall(query)
    if len(quotes) > 1 or len(list(_MEETING_DATE.finditer(query))) > 1:
        return ""
    terms = content_terms(quotes[0]) if len(quotes) == 1 else content_terms(query)
    generic = _NOISE | {
        "일정",
        "공유",
        "방식",
        "방법",
        "담당",
        "검토",
        "완료",
        "요청",
        "보고",
        "결과",
        "자료",
        "업무",
        "작업",
        "회신",
        "정책",
    }
    generic_action = r"(?:공유|확인|검토|완료|담당|처리|보고|요청|회신|결정)(?:하기로|하|해|했|할|하는지|했는지|됐는지|했나|하나|할지)?"
    if not quotes and not any(
        term not in generic and re.fullmatch(generic_action, term) is None for term in terms
    ):
        return ""
    return " ".join(terms[:max_terms])


def cross_meeting_comparison(query: str) -> bool:
    """서로 다른 회의를 비교하라는 명시 요청만 식별하고 인용 주제의 단어는 제외한다."""
    scope = _QUOTED.sub(" ", query)
    if re.search(r"비교|차이|달라|달랐|변경|변했|바뀌|바뀐|변화|전후", scope) is None:
        return False
    return bool(
        len(re.findall(r"회의(?!실|록|적)", scope)) >= 2
        or re.search(r"(?:회의\s*(?:간|별)|(?:여러|두|세|각)\s*회의(?!실|록|적))", scope)
        or re.search(
            r"(?:지난|이전|앞선)\s*(?:과|와|및)\s*(?:이번|최근|최신|현재)\s*회의(?!실|록|적)",
            scope,
        )
    )


def _balanced_comparison_results(rows: list[SearchResult], max_chunks: int) -> list[SearchResult]:
    """비교할 각 회의의 첫 근거를 우선 보존하고 전체 문맥 상한 안에서 나머지를 채운다."""
    by_id: dict[str, SearchResult] = {}
    first: dict[str, str] = {}
    for row in rows:
        by_id.setdefault(row.chunk_id, row)
        first.setdefault(row.meeting_id, row.chunk_id)
    unique = list(by_id.values())
    if len(first) > max_chunks:
        return []  # 한쪽만 남겨 비교 근거가 충분한 것처럼 전달하지 않는다.
    selected = set(first.values())
    for row in unique:
        if len(selected) >= max_chunks:
            break
        selected.add(row.chunk_id)
    return [row for row in unique if row.chunk_id in selected]


@dataclass(frozen=True)
class SourceScope:
    """사용자 필터를 우선하며 실제 색인에서 확인한 자연어 회의 범위만 보관한다."""

    meeting_id: str | None
    date_filter: str | None
    allowed_meetings: frozenset[str] | None = None
    blocked: bool = False


def resolve_source_scope(
    config: Any, query: str, meeting_id_filter: str | None, date_filter: str | None
) -> SourceScope:
    """명시 API 필터를 바꾸지 않고 날짜·시각이 붙은 회의 표현만 실제 색인과 대조한다."""
    if meeting_id_filter or date_filter:
        return SourceScope(
            meeting_id_filter,
            date_filter,
            frozenset([meeting_id_filter]) if meeting_id_filter else None,
        )
    matches = list(_MEETING_DATE.finditer(query))
    if len(matches) > 1:
        return SourceScope(None, None, frozenset(), True)
    if not matches:
        return SourceScope(None, None)
    match = matches[0]
    try:
        parsed_date = date(int(match["year"]), int(match["month"]), int(match["day"]))
        prefix = None
        if match["hour"] is not None:
            clock = time(int(match["hour"]), int(match["minute"]))
            prefix = f"meeting_{parsed_date:%Y%m%d}_{clock:%H%M}"
        from search.source_recall import _readonly_index

        with _readonly_index(config) as conn:
            rows = conn.execute(
                "SELECT DISTINCT meeting_id FROM chunks_fts WHERE date = ?",
                (parsed_date.isoformat(),),
            ).fetchall()
        valid_mids: set[str] = set()
        for row in rows:
            mid = row[0]
            if prefix is not None and not mid.startswith(prefix):
                continue
            if revision_status(config, mid) not in {"legacy", "current"}:
                continue
            _, source = read_source(config, mid)
            encoded = _TIMESTAMPED_MEETING_ID.fullmatch(mid)
            if prefix is not None and encoded is None:
                continue
            if encoded:
                try:
                    time(int(encoded[2]), int(encoded[3]), int(encoded[4]))
                except ValueError:
                    continue
            if encoded and encoded[1] != f"{parsed_date:%Y%m%d}":
                continue
            preserved = source.get("meeting_date")
            try:
                saved = json.loads(
                    read_text_no_follow(checkpoint_dir(config, mid) / "meeting_date.json")
                )
                if not isinstance(saved, dict) or not isinstance(saved.get("date"), str):
                    continue
                if preserved is not None and preserved != saved["date"]:
                    continue
                preserved = saved["date"]
            except FileNotFoundError:
                pass
            if preserved is not None and preserved != parsed_date.isoformat():
                continue
            with _readonly_index(config) as conn:
                records = conn.execute(
                    "SELECT chunk_id,text,meeting_id,date,speakers,start_time,end_time,chunk_index FROM chunks_fts WHERE meeting_id = ? AND date = ? LIMIT ?",
                    (mid, parsed_date.isoformat(), config.chat.source_context_max_chunks),
                ).fetchall()
            if not filter_current_results(config, [dict(item) for item in records]):
                continue
            valid_mids.add(mid)
        mids = frozenset(valid_mids)
        return SourceScope(
            next(iter(mids)) if len(mids) == 1 else None,
            parsed_date.isoformat(),
            mids,
            len(mids) != 1,
        )
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return SourceScope(None, None, frozenset(), True)


def topic_context_results(
    query: str,
    original: list[SearchResult],
    focused: list[SearchResult],
    scope: SourceScope,
    max_chunks: int,
    *,
    config: Any | None = None,
) -> list[SearchResult]:
    """주제 검색의 회의 안에서 원 검색 순서·메타데이터를 유지하고 누락 청크만 보충한다."""
    permitted = [
        r
        for r in [*focused, *original]
        if scope.allowed_meetings is None or r.meeting_id in scope.allowed_meetings
        if scope.meeting_id is None or r.meeting_id == scope.meeting_id
        if scope.date_filter is None or r.date == scope.date_filter
    ]
    if not permitted:
        return []
    comparison = cross_meeting_comparison(query) and not scope.meeting_id
    if comparison:
        permitted_ids = {row.chunk_id for row in permitted}
        return _balanced_comparison_results(
            [row for row in [*original, *focused] if row.chunk_id in permitted_ids], max_chunks
        )
    latest = (
        config is not None
        and re.search(r"최근|최신", query) is not None
        and not scope.meeting_id
        and not scope.date_filter
        and scope.allowed_meetings is None
    )
    if not focused and not latest:
        return permitted[:max_chunks]
    if scope.meeting_id:
        anchor = scope.meeting_id
    else:
        quoted = _QUOTED.findall(query)
        terms = content_terms(quoted[0]) if len(quoted) == 1 else content_terms(query)
        by_meeting: dict[str, set[str]] = {}
        for r in permitted:
            text = unicodedata.normalize("NFC", r.text).casefold()
            by_meeting.setdefault(r.meeting_id, set()).update(
                t.casefold() for t in terms if t.casefold() in text
            )
        # 단어 일치는 주제 anchor 순서만 정하고 주장 지지나 실명을 판정하지 않는다.
        if latest:
            latest_anchor = _latest_related_meeting(config, query, permitted)
            if latest_anchor is None:
                return []
            anchor = latest_anchor
        else:
            anchor = max(by_meeting, key=lambda mid: len(by_meeting[mid]))
    output: list[SearchResult] = []
    seen: set[str] = set()
    for r in [*original, *focused]:
        if r.meeting_id == anchor and r.chunk_id not in seen and len(output) < max_chunks:
            output.append(r)
            seen.add(r.chunk_id)
    return output


def _latest_related_meeting(config: Any, query: str, results: list[SearchResult]) -> str | None:
    """이미 검색된 관련 회의의 현재 원문·색인·보존 날짜가 맞을 때만 최신 날짜를 고른다."""
    quoted = _QUOTED.findall(query)
    terms = content_terms(quoted[0]) if len(quoted) == 1 else content_terms(query)
    generic = {
        "일정",
        "공유",
        "방식",
        "방법",
        "담당",
        "검토",
        "완료",
        "요청",
        "보고",
        "결과",
        "자료",
        "업무",
        "작업",
        "회신",
        "정책",
    }
    terms = [term for term in terms if term not in generic]
    if not terms:
        return None
    meetings = list(dict.fromkeys(row.meeting_id for row in results))
    candidates: list[tuple[date, int, str]] = []
    for mid in meetings:
        try:
            rows = [row for row in results if row.meeting_id == mid]
            dates = {row.date for row in rows}
            if len(dates) != 1:
                continue
            raw_date = next(iter(dates))
            parsed = date.fromisoformat(raw_date)
            if parsed.isoformat() != raw_date or revision_status(config, mid) not in {
                "legacy",
                "current",
            }:
                continue
            _, source = read_source(config, mid)
            preserved = source.get("meeting_date")
            try:
                saved = json.loads(
                    read_text_no_follow(checkpoint_dir(config, mid) / "meeting_date.json")
                )
                if not isinstance(saved, dict) or not isinstance(saved.get("date"), str):
                    continue
                if preserved is not None and preserved != saved["date"]:
                    continue
                preserved = saved["date"]
            except FileNotFoundError:
                pass
            encoded = _TIMESTAMPED_MEETING_ID.fullmatch(mid)
            if encoded:
                time(int(encoded[2]), int(encoded[3]), int(encoded[4]))
                if encoded[1] != f"{parsed:%Y%m%d}":
                    continue
                if preserved is None:
                    preserved = parsed.isoformat()
            if preserved != raw_date:
                continue
            current = filter_current_results(
                config, [dict(row.to_dict(), speakers=",".join(row.speakers)) for row in rows]
            )
            valid_ids = {row["chunk_id"] for row in current}
            texts = [
                unicodedata.normalize("NFC", row.text).casefold()
                for row in rows
                if row.chunk_id in valid_ids
            ]
            matched = {
                term.casefold()
                for term in terms
                if any(
                    re.search(rf"(?<![a-z0-9]){re.escape(term.casefold())}(?![a-z0-9])", text)
                    if re.fullmatch(r"[a-zA-Z0-9]+", term)
                    else term.casefold() in text
                    for text in texts
                )
            }
            # 이미 실제 의미 검색이 반환한 후보는 언어 간 문자 일치가 없어도 비교한다.
            # 검색되지 않은 회의를 날짜만으로 새로 탐색하거나 사실 지지로 판정하지 않는다.
            semantic = any(
                row.chunk_id in valid_ids and row.source in {"vector", "both"} for row in rows
            )
            if matched or semantic:
                candidates.append((parsed, len(matched), mid))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    # 동일 날짜·주제 일치 수에서는 기존 검색의 회의 순서를 보존한다.
    return max(candidates, key=lambda item: item[:2])[2] if candidates else None
