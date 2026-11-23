"""전사·회의록의 현재 버전에만 유효한 안건별 Wiki 근거 색인."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import AppConfig
from core.io_utils import atomic_write_json_pinned, read_text_no_follow
from core.nightly_window import finish_started_write
from core.search_revision import checkpoint_dir, read_source
from search import topic_brief as semantics
from search import topic_minutes as minutes
from search.source_answers import _pure_acknowledgement, _unfinished_clause

logger = logging.getLogger(__name__)
_VERSION = 2
_CLOCK = re.compile(r"(?:오전\s*|오후\s*)?\d{1,2}\s*시(?:\s*\d+\s*분|\s*반)?")
_CONDITION = re.compile(
    r"[^.?!,。\n]{0,75}?(?:준다면|하면|해\s*주시면|되면|받으면|전제로|조건으로|에\s*따라)"
)
_REPORTED = re.compile(r"자고|제안|어떨까|하면\s*좋|검토\s*중|하는\s*방향")
_UNCERTAIN = re.compile(
    r"미정|미확정|확인.{0,4}필요|아직.{0,10}(?:없|안|못)|정해지지|(?:확정|결정).{0,8}(?:않|못|없)"
)
_EXPLICIT_ACTION = re.compile(
    r"(?:확인|전달|공유|검토|정리|발송|초대|준비|조율|보내).{0,9}(?:할게|드릴|하기로|해\s*주|해요|겠|할\s*예정)"
)
_CHANGE = re.compile(
    r"^(?:그리고\s+|자[,.]?\s*|그러면\s+)?(?:다음으로|그다음(?:은|으로)|이제\s*다음|또\s*다른\s*(?:건|주제)|별건으로)"
)
_TOPIC_OPENING = re.compile(
    r"(?:관련해서|관련하여|관해서|대해\s*(?:말씀|얘기|논의)|"
    r"(?:피드백|의견|문의|요청|답변|제안)(?:이|가|을|를)?\s*(?:왔|들어왔|받았)|"
    r"다른\s*(?:분|팀|업체).{0,18}(?:얘기|이야기|통화))"
)
_REPLY = re.compile(r"^(?:네[,.\s]|맞[아습]|그렇|그런|그러면|그거|그걸|그래서|알겠|예[,.\s])")
_OWNER = re.compile(
    r"(?:^|\s)([가-힣A-Za-z0-9]{1,12}(?:팀|님)|담당자|참석자\s*\d+)(?:이|가|은|는|께서|\s*[:：])\s*"
)
_DISCOURSE = frozenset(
    [
        "일단",
        "그냥",
        "근데",
        "이제",
        "지금",
        "그러면",
        "사실",
        "그런",
        "이런",
        "저희",
        "제가",
        "같다",
        "거거든요",
        "그래서",
        "그리고",
        "저희가",
        "저는",
        "그거",
        "이거",
        "그게",
        "이게",
        "이번",
        "관련",
        "대한",
        "하는",
        "있는",
        "같은",
        "것들",
        "여기",
        "저기",
        "맞아요",
        "보시면",
        "부분",
        "한번",
        "그렇게",
    ]
)


@dataclass(frozen=True)
class AgendaFact:
    """검증된 단일 출처와 발언에 명시된 상태·담당·시점."""

    kind: str
    text: str
    source_index: int | None
    minutes_excerpt: str | None
    status: str
    owner: str | None
    time_text: str | None
    condition_text: str | None = None


@dataclass(frozen=True)
class AgendaRecord:
    """같은 회의의 연속 안건과 현재 원문에서 복원한 인용 후보."""

    title: str
    turn_indices: tuple[int, ...]
    facts: tuple[AgendaFact, ...]
    source_digest: str
    minutes_digest: str
    minutes_excerpts: tuple[str, ...]
    minutes_bound: bool


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _index_path(config: AppConfig, meeting_id: str) -> Path:
    """ID·원문 중복 보관 없이 Wiki의 파생 색인에 저장한다."""
    cp = checkpoint_dir(config, meeting_id)
    if not config.wiki.enabled:
        return cp / ".wiki-agenda.json"
    root = Path(config.wiki.root).expanduser().absolute()
    if "root" not in config.wiki.model_fields_set:
        root = Path(config.paths.base_dir).expanduser().absolute() / "wiki"
    return root / ".index" / "agenda_records" / f"{_digest(str(cp))}.json"


def _snapshot(
    config: AppConfig, meeting_id: str, *, markdown: str | None = None
) -> tuple[list[dict[str, Any]], str, str, str]:
    _, source = read_source(config, meeting_id)
    turns = source.get("utterances")
    if not isinstance(turns, list) or not all(isinstance(t, dict) for t in turns):
        raise ValueError("안건 색인을 위한 발언 형식이 잘못되었습니다")
    if markdown is None:
        markdown = minutes.current_minutes_markdown(config, meeting_id) or ""
    digest = _digest(json.dumps(turns, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return turns, markdown, digest, str(source.get("search_revision") or "")


def _words(text: str) -> set[str]:
    return set(minutes._terms(text)) - _DISCOURSE


def _sections(texts: list[str], tokens: list[set[str]]) -> list[tuple[int, int]]:
    """명시적 전환을 보존하고 긴 논의는 인접 발언 어휘의 결속이 약한 지점에서 나눈다."""
    if not texts:
        return []
    cuts = [0]
    for i, text in enumerate(texts[1:], 1):
        if (
            semantics._TRANSITION.search(text[:60])
            or _CHANGE.match(text)
            or (
                _TOPIC_OPENING.search(text)
                and len(tokens[i] - set[str]().union(*tokens[max(0, i - 3) : i])) >= 3
            )
        ):
            cuts.append(i)
    cuts.append(len(texts))
    ranges = []
    for left, right in zip(cuts, cuts[1:], strict=False):
        while right - left > 20:

            def cohesion(i: int, left: int = left, right: int = right) -> float:
                before = set.union(*tokens[max(left, i - 4) : i])
                after = set.union(*tokens[i : min(right, i + 4)])
                continued = bool(
                    _REPLY.match(texts[i])
                    or _unfinished_clause(texts[i - 1])
                    or texts[i - 1].endswith("?")
                )
                return len(before & after) / max(1, len(before | after)) + continued

            boundary = min(range(left + 8, min(left + 20, right - 3)), key=cohesion)
            ranges.append((left, boundary))
            left = boundary
        ranges.append((left, right))
    return ranges


def _spoken_kinds(text: str) -> list[str]:
    """요청과 결정, 미래 시점을 각각 분류한다. 순수 질문·취소를 확정으로 만들지 않는다."""
    if not text.strip() or _pure_acknowledgement(text):
        return []
    kinds = []
    if not (semantics._NONFINAL.search(text) or semantics._WITHDRAWN.search(text)):
        if (
            semantics._DECIDED.search(text)
            or semantics._SETTLED.search(text)
            or semantics._stated_rule(text)
        ):
            kinds.append("decision")
        if (
            semantics._ACTION.search(text)
            or semantics._HANDOFF.search(text)
            or _EXPLICIT_ACTION.search(text)
        ):
            kinds.append("action")
    if semantics._planned_time(text):
        kinds.append("schedule")
    return kinds


def _compile(turns: list[dict[str, Any]], markdown: str) -> list[dict[str, Any]]:
    texts = [t["text"] if isinstance(t.get("text"), str) else "" for t in turns]
    tokens = [_words(text) for text in texts]
    ranges = _sections(texts, tokens)
    vocabulary = [set.union(*tokens[a:b]) for a, b in ranges]
    frequencies = Counter(word for words in vocabulary for word in words)
    groups: list[dict[str, Any]] = [
        {
            "range": [a, b],
            "minutes": [],
            "facts": [
                _fact_ref(k, texts[i], i, "spoken")
                for i in range(a, b)
                for k in _spoken_kinds(texts[i])
            ],
        }
        for a, b in ranges
    ]
    for row_index, row in enumerate(minutes._rows(markdown)):
        terms = _words(row.fact.text)
        matches = []
        for j, ((a, b), words) in enumerate(zip(ranges, vocabulary, strict=True)):
            shared = terms & words
            if len(shared) < 2:
                continue
            # 한 구간에 흩어져 우연히 등장하는 단어보다 같은 발언에서 확인된 근거가 우선한다.
            local = max(len(terms & seen) for seen in tokens[a:b])
            distinct = sum(math.log1p(len(ranges) / frequencies[t]) for t in shared)
            if local >= 2:
                matches.append((distinct * math.sqrt(local), j))
        matches.sort(reverse=True)
        if matches and (len(matches) == 1 or matches[0][0] > 1.25 * matches[1][0]):
            group = groups[matches[0][1]]
            group["minutes"].append(row_index)
            group["facts"].extend(
                _fact_ref(k, row.fact.text, row_index, "minutes")
                for k in sorted(minutes._categories(row))
            )
    return groups


def _read_index(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(read_text_no_follow(path))
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("version") == _VERSION else {}


def minutes_match_transcript(config: AppConfig, meeting_id: str, markdown: str) -> bool:
    """전사 편집 전에 만든 회의록을 최신 전사의 결정·후속 근거로 재사용하지 않는다."""
    try:
        prior = _read_index(_index_path(config, meeting_id))
        try:
            _, _, digest, revision = _snapshot(config, meeting_id, markdown=markdown)
        except FileNotFoundError:
            return not prior  # 전사 파일이 없는 과거 독립 회의록
        if not prior:
            return not revision
        return bool(
            prior.get("minutes_bound") is True
            and prior.get("revision") == revision
            and prior.get("source_digest") == digest
            and prior.get("minutes_digest") == _digest(markdown)
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def refresh_agenda_records(
    config: AppConfig,
    meeting_id: str,
    *,
    minutes_edited: bool = False,
    published_markdown: str | None = None,
) -> int:
    """회의 쓰기 잠금 안에서 현재 원문의 분석 결과만 게시한다. 재시도 때 동일본은 재사용한다."""
    path = _index_path(config, meeting_id)
    turns, markdown, digest, revision = _snapshot(config, meeting_id)
    prior = _read_index(path)
    minutes_digest = _digest(markdown)
    # 전사 편집 전 작성한 회의록이 수정된 결정·기한을 다시 끌어오지 못하게 한다.
    bound = (
        minutes_edited
        or (published_markdown is not None and published_markdown == markdown)
        or (
            not prior
            and not revision
            or prior.get("revision") == revision
            and prior.get("source_digest") == digest
            and prior.get("minutes_bound") is True
        )
    )
    seal = {
        "version": _VERSION,
        "source_digest": digest,
        "minutes_digest": minutes_digest,
        "revision": revision,
        "minutes_bound": bound,
    }
    if all(prior.get(k) == v for k, v in seal.items()) and isinstance(prior.get("groups"), list):
        return len(prior["groups"])
    groups = _compile(turns, markdown if bound else "")
    atomic_write_json_pinned(path, {**seal, "groups": groups})
    return len(groups)


async def refresh_after_write(
    config: AppConfig,
    meeting_id: str,
    *,
    minutes_edited: bool = False,
    published_markdown: str | None = None,
) -> None:
    """전사·회의록의 발행은 유지하고 부가 색인의 쓰기 실패를 보고한다."""
    if not isinstance(config, AppConfig) or not isinstance(config.paths.base_dir, str):
        return
    try:
        await finish_started_write(
            asyncio.to_thread(
                refresh_agenda_records,
                config,
                meeting_id,
                minutes_edited=minutes_edited,
                published_markdown=published_markdown,
            )
        )
    except (OSError, ValueError) as exc:
        logger.warning(f"안건 색인 갱신 보류: meeting_id={meeting_id}, {type(exc).__name__}")


def _fact_ref(kind: str, text: str, index: int, source: str) -> dict[str, Any]:
    """담당·시간·조건을 나타내는 근거의 문자 위치만 영속화한다."""
    status = "원문 기재"
    condition = _CONDITION.search(text)
    if semantics._WITHDRAWN.search(text) or _UNCERTAIN.search(text):
        status = "미확정"
    elif condition:
        status = "조건부"
    elif _REPORTED.search(text):
        status = "제안"
    elif semantics._DECIDED.search(text) or semantics._BOOKED.search(text):
        status = "확정"
    elif "목표" in text:
        status = "목표"
    elif "예정" in text:
        status = "예정"
    timing = [
        m.span()
        for m in re.finditer(f"(?:{_CLOCK.pattern})|(?:{semantics._CALENDAR.pattern})", text)
    ]
    named = _OWNER.search(text) if kind == "action" else None
    return {
        "source": source,
        "index": index,
        "kind": kind,
        "status": status,
        "time": timing,
        "owner": named.span(1) if named else None,
        "self_assigned": bool(
            source == "spoken"
            and kind == "action"
            and re.search(r"제가.{0,60}(?:하겠|할게|드릴게|보낼게)", text)
        ),
        "condition": condition.span() if condition else None,
    }


def _restore_fact(
    ref: dict[str, Any], text: str, *, speaker: str | None = None, excerpt: str | None = None
) -> AgendaFact:
    """같은 지문의 원문에서만 안건의 필드 값을 복원한다."""

    def span(pair: list[int]) -> str:
        start, end = pair
        if not 0 <= start < end <= len(text):
            raise ValueError("안건 근거 위치가 원문 범위를 벗어났습니다")
        return text[start:end].strip()

    owner = span(ref["owner"]) if ref["owner"] else speaker if ref["self_assigned"] else None
    return AgendaFact(
        ref["kind"],
        text,
        ref["index"] if excerpt is None else None,
        excerpt,
        ref["status"],
        owner,
        " ".join(span(p) for p in ref["time"]) or None,
        span(ref["condition"]) if ref["condition"] else None,
    )


def agenda_for_topic(
    config: AppConfig, meeting_id: str, topic: str, *, anchor_index: int | None = None
) -> AgendaRecord | None:
    """선택된 원문 발언을 포함하는 안건을 읽는다. 지문이 다르거나 주제가 모호하면 사용하지 않는다."""
    try:
        prior = _read_index(_index_path(config, meeting_id))
        if not prior:
            return None
        turns, markdown, digest, revision = _snapshot(config, meeting_id)
        if (
            prior.get("source_digest") != digest
            or prior.get("minutes_digest") != _digest(markdown)
            or revision != prior.get("revision")
        ):
            return None
        rows = minutes._rows(markdown) if prior["minutes_bound"] else []
        query = _words(topic)
        ranked = []
        for group in prior["groups"]:
            a, b = group["range"]
            if not (0 <= a < b <= len(turns)) or (
                anchor_index is not None and not a <= anchor_index < b
            ):
                continue
            evidence = [(i, str(turns[i].get("text", ""))) for i in range(a, b)]
            hits = [
                sum(minutes._present(w, minutes._normalized(text)) for w in query)
                for _, text in evidence
            ]
            if not max(hits):
                continue
            best = max(range(len(hits)), key=hits.__getitem__)
            ranked.append(((hits[best], sum(hits)), group, evidence[best][1]))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
            return None
        _, group, title = ranked[0]
        a, b = group["range"]
        associated = [rows[i] for i in group["minutes"] if 0 <= i < len(rows)]
        facts = []
        for ref in group["facts"]:
            i = ref["index"]
            if ref["kind"] not in semantics.FACETS:
                raise ValueError("알 수 없는 안건 근거 유형")
            if ref["source"] == "spoken" and a <= i < b:
                facts.append(_restore_fact(ref, turns[i]["text"], speaker=turns[i].get("speaker")))
            elif ref["source"] == "minutes" and i in group["minutes"] and 0 <= i < len(rows):
                facts.append(_restore_fact(ref, rows[i].fact.text, excerpt=rows[i].fact.excerpt))
        return AgendaRecord(
            title,
            tuple(range(a, b)),
            tuple(facts),
            digest,
            prior["minutes_digest"],
            tuple(r.fact.excerpt for r in associated),
            prior["minutes_bound"],
        )
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return None
