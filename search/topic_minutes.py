"""현재 뷰어의 회의록에서 주제별 결정·후속·일정의 실제 Markdown 근거를 읽는다."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from itertools import pairwise
from pathlib import Path

from config import AppConfig
from core.io_utils import read_text_no_follow
from core.reindex_recovery import _configured_storage_root
from core.search_revision import checkpoint_dir
from search.source_query import topic_content_terms


@dataclass(frozen=True)
class MinutesExcerpt:
    """현재 Markdown에 있는 내용과 그 내용을 포함하는 인용 범위."""

    text: str
    excerpt: str
    section: str
    source_kind: str = "minutes"


@dataclass(frozen=True)
class TopicMinutes:
    """검증 시점의 뷰어 본문 지문과 항목별 인용 후보."""

    meeting_id: str
    digest: str
    facts: dict[str, tuple[MinutesExcerpt, ...]]


def _json_markdown(path: Path) -> str:
    try:
        data = json.loads(read_text_no_follow(path))
    except FileNotFoundError:
        return ""
    markdown = data.get("markdown", "") if isinstance(data, dict) else None
    if not isinstance(markdown, str):
        raise ValueError("회의록 JSON 형식이 올바르지 않습니다")
    return markdown


def current_minutes_markdown(config: AppConfig, meeting_id: str) -> str | None:
    """GET summary의 읽기·편집 우선순위를 따르며 손상된 현재본은 건너뛰지 않는다."""
    try:
        cp = checkpoint_dir(config, meeting_id)
        root = _configured_storage_root(config, "outputs_dir", config.paths.resolved_outputs_dir)
        directory = root / meeting_id
        markdown = ""
        for name in ("summary.md", "meeting_minutes.md"):
            try:
                markdown = read_text_no_follow(directory / name)
            except FileNotFoundError:
                continue
            break
        # 뷰어는 Markdown 파일이 있어도 함께 제공하는 summary.json을 검증한다.
        meta = _json_markdown(directory / "summary.json")
        return markdown or meta or _json_markdown(cp / "summarize.json") or None
    except (OSError, ValueError):
        return None


_GENERIC = {
    "방식",
    "방법",
    "진행",
    "논의",
    "공유",
    "적용",
    "계획",
    "일정",
    "담당",
    "담당자",
    "자료",
    "주제",
    "요청",
    "검토",
    "주요",
    "상태",
    "항목",
    "정리",
    "사항",
    "다음",
    "작업",
    "필요",
    "내용",
    "관련",
    "부분",
    "실시",
    "미정",
    "예정",
    "대상",
    "기간",
    "참석자",
}
_DECISION = re.compile(
    r"하기로|결정|확정|원칙|유지|채택|적용(?:함|한다)|기준.{0,15}판단|설정함|"
    r"결론|제한|불가|수\s*없|제안|미정|의견이|방향으로|검토하기로|가능성이\s*논의|"
    r"변경함|수정함|적용\s*완료|(?:확인|검토|협의)(?:이|가)?\s*필요"
)
_ACTION = re.compile(
    r"하기로|예정|계획|요청|공유.{0,14}필요|전달.{0,14}필요|확인.{0,14}필요|"
    r"후속|추진|담당.{0,14}(?:맡|수행)|약속"
)
_TIMING = re.compile(
    r"오늘|내일|모레|당일|다음\s*(?:날|주|달|분기)|이번\s*(?:주|달|분기)|"
    r"[월화수목금토일]요일|\d+\s*(?:월|일|주|개월|시간|분기)(?![가-힣]{2})|"
    r"(?:수신|완료|검토|승인|수령|제출|접수)\s*(?:이후|직후|후)|전까지|"
    r"(?:기한|일정|시점|기간).{0,12}(?:미정|미확정|정해지지|없음|없다)"
)
_PLAN = re.compile(
    r"예정|목표|전망|미정|미확정|기한|일정.{0,12}확정|하기로|요청|계획|검토|설정|시작|진행할|소요|걸릴"
)
_HISTORICAL = re.compile(r"지난|작년|어제|전날|당시|소요되었|소요됐|걸렸")
_HEADING = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s+(?:\[ \]\s*)?")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.+)$")


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _terms(text: str) -> tuple[str, ...]:
    return tuple(
        term
        for term in map(_normalized, topic_content_terms(text))
        if len(term) >= 2 and term not in _GENERIC and not term.isnumeric()
    )


def _section_kind(heading: str) -> str:
    heading = re.sub(r"[\s_*]+", "", heading)
    if re.search(r"액션|후속|할일|실행항목|작업목록", heading):
        return "action"
    if re.search(r"결정|합의", heading):
        return "decision"
    if re.search(r"일정|기한", heading):
        return "schedule"
    if re.search(r"안건|의제", heading):
        return "agenda"
    if re.search(r"결론|논의|요약", heading):
        return "summary"
    return ""


@dataclass(frozen=True)
class _Row:
    fact: MinutesExcerpt
    kind: str
    agenda: str


def _rows(markdown: str) -> list[_Row]:
    rows = []
    section = kind = agenda = ""
    level = 0
    fenced = False
    for raw in markdown.splitlines():
        clean = raw.strip()
        if clean.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or not clean:
            continue
        if heading := _HEADING.match(raw):
            depth, label = len(heading[1]), heading[2]
            if kind == "agenda" and depth > level:
                agenda = label
            else:
                level, section, kind, agenda = depth, label, _section_kind(label), ""
            continue
        if not kind:
            continue
        if numbered := _NUMBERED.match(raw):
            if kind == "agenda":
                agenda = numbered[1]
                continue
            clean = numbered[1]
        bullet = _BULLET.match(raw)
        body = raw[bullet.end() :].strip() if bullet else clean
        # 목록의 후속 문장은 조건·예외·미확정을 담을 수 있어 같은 항목으로 보존한다.
        pieces = [body] if bullet or numbered else re.split(r"(?<=[.!?。])\s+", body)
        for piece in pieces:
            if piece:
                excerpt = raw.strip() if bullet or numbered else piece
                path = f"{section} / {agenda}" if agenda else section
                rows.append(_Row(MinutesExcerpt(piece, excerpt, path), kind, agenda))
    return rows


def _present(term: str, text: str) -> bool:
    # 전사는 영문 조직명과 한국어 조직 단위를 띄어 쓰는 경우가 있다.
    # 'AB팀'이 'CAB 팀'이나 다른 팀 이름과 일치하지 않도록 경계를 유지한다.
    if team := re.fullmatch(r"([a-z0-9]+)(팀|그룹|센터|본부)", term):
        return bool(re.search(rf"(?<![a-z0-9]){team[1]}\s*{team[2]}", text))
    if term.isascii():
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text))
    return term in text


def _score(text: str, weights: dict[str, float]) -> float:
    text = _normalized(text)
    hits = [term for term in weights if _present(term, text)]
    compact = re.sub(r"[\s·/_-]+", "", text)
    adjacent = sum((a + b) in compact for a, b in pairwise(weights))
    return sum(weights[term] for term in hits) + adjacent * 2.0


def _scope_peers(rows: list[_Row], topic: str) -> tuple[set[str], set[str], set[str]]:
    """같은 소제목의 대조 항목(예: 양측의 동일 서비스)을 문서에서 발견한다."""
    groups: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        label = row.fact.text.split(":", 1)[0] if ":" in row.fact.text else ""
        for candidate in (label.strip("* "), row.agenda):
            match = re.fullmatch(r"([\w-]{2,24})\s+([\w -]{2,24})", candidate)
            if match and not re.search(r"\d|참석자|담당자", candidate):
                groups[_normalized(match[2])].add(_normalized(match[1]))
            elif role := re.fullmatch(r"([가-힣]{2,10}(?:님|팀|지사|그룹|지역))", candidate):
                suffix = re.search(r"님|팀|지사|그룹|지역", role[1])
                if suffix:
                    groups[suffix[0]].add(_normalized(role[1]))
    requested = set()
    others = set()
    shared: set[str] = set()
    for label, alternatives in groups.items():
        matches = {term for term in alternatives if _present(term, _normalized(topic))}
        if matches and len(alternatives) > len(matches):
            requested.update(matches)
            others.update(alternatives - matches)
            shared.update(_terms(label))
    return requested, others, shared


def _categories(row: _Row) -> set[str]:
    text = row.fact.text
    categories = {row.kind} & {"action", "decision"}
    if row.kind in {"summary", "agenda"} and _DECISION.search(text):
        categories.add("decision")
    if row.kind in {"summary", "agenda", "decision"} and _ACTION.search(text):
        categories.add("action")
    if _TIMING.search(text) and (row.kind == "schedule" or _PLAN.search(text)):
        if not _HISTORICAL.search(text) or re.search(r"내일|다음\s*(?:주|날)|예정|기한", text):
            categories.add("schedule")
    return categories


def _redundant(candidate: str, previous: str) -> bool:
    """수치·조건·담당자가 추가되면 보존하고 표현만 거의 같은 근거를 줄인다."""
    detail = r"\d+(?:[.~:%~-]\d+)*\s*(?:일|주|개월|명|건|%|시|분)?|\w+님|미정|미확정|조건|경우|예외|이후|이전|않|아님|제외|단계|가능|요청"
    if set(re.findall(detail, candidate)) - set(re.findall(detail, previous)):
        return False
    left = re.sub(r"\W+", "", _normalized(candidate))
    right = re.sub(r"\W+", "", _normalized(previous))
    return SequenceMatcher(None, left, right).ratio() >= 0.8


def topic_minutes(config: AppConfig, meeting_id: str, topic: str) -> TopicMinutes | None:
    """해당 주제를 직접 다루는 현재 회의록 항목을 분야별 최대 두 개 선택한다."""
    from core.agenda_records import minutes_match_transcript

    markdown = current_minutes_markdown(config, meeting_id)
    if not markdown or not minutes_match_transcript(config, meeting_id, markdown):
        return None
    rows = _rows(markdown)
    texts = [_normalized(row.fact.text + " " + row.agenda) for row in rows]
    weights = {
        term: 1 + math.log((1 + len(rows)) / (1 + sum(_present(term, item) for item in texts)))
        for term in _terms(topic)
        if any(_present(term, item) for item in texts)
    }
    ranked: dict[str, list[tuple[float, int, MinutesExcerpt]]] = {
        key: [] for key in ("decision", "action", "schedule")
    }
    direct = [_score(row.fact.text, weights) for row in rows]
    agenda_titles = {row.agenda for row in rows if row.agenda}
    scored_titles = {title: _score(title, weights) for title in agenda_titles}
    best_title = max(scored_titles.values(), default=0)
    target_titles = {s for s, n in scored_titles.items() if n == best_title and n > 0}
    title_terms = {t for s in target_titles for t in _terms(s)}
    rival_terms = {t for s in agenda_titles - target_titles for t in _terms(s)} - title_terms
    title_terms.intersection_update(weights)
    target, competing, shared = _scope_peers(rows, topic)
    subjects = weights.keys() - {"자동", "수동", "처리", "운영", "시스템", "기능", "사용", "관리"}
    # 서로 다른 소안건에서 쓰이는 같은 명사 하나는 일정·후속 연결의 근거가 안 된다.
    # 상위 안건 하나에 여러 문제를 나열하는 회의록도 있어 목록 항목별로 센다.
    agenda_bodies = [_normalized(r.fact.text + " " + r.agenda) for r in rows if r.kind == "agenda"]
    agenda_frequency = {t: sum(_present(t, text) for text in agenda_bodies) for t in subjects}
    single_anchor = {t for t, count in agenda_frequency.items() if count == 1}
    threshold = max(direct, default=0) * 0.42
    for index, row in enumerate(rows):
        score = direct[index]
        body = _normalized(row.fact.text).strip("* ")
        if row.kind == "action" and ":" in body:
            owner, _, detail = body.partition(":")
            if not any(_present(term, owner) for term in target | competing):
                body = detail.lstrip()
        # 안건 전체에 공통인 '티켓'처럼 한 단어가 맞더라도, 다른 안건의 고유한
        # 대상이 명시된 작업을 선택한 안건의 약속으로 옮기지 않는다.
        if (
            title_terms
            and any(_present(t, body) for t in rival_terms)
            and not any(_present(t, body) for t in title_terms)
        ):
            continue
        if competing:
            indicated = {term for term in target if _present(term, body)}
            contrary = {term for term in competing if _present(term, body)}
            if any(body.startswith(term) for term in contrary) or (contrary and not indicated):
                continue
            if any(_normalized(row.agenda).startswith(term + " ") for term in competing):
                continue
            # 병렬 소제목에 공통으로 쓰인 서비스명만으로 다른 범위의 후속을 승계하지 않는다.
            if not indicated and not any(_present(term, body) for term in weights.keys() - shared):
                continue
        # 안건 제목은 자기 목록에만 유효하다. 다른 섹션의 담당자를 유추하지 않는다.
        if row.agenda:
            title = _score(row.agenda, weights)
            if score or (title and all(_present(t, _normalized(row.agenda)) for t in weights)):
                score += title * 0.45
        floor = threshold * 0.55 if row.kind == "action" else threshold
        if score <= 0 or score < floor:
            continue
        for key in _categories(row):
            if key in {"action", "schedule"} and len(subjects) > 1 and single_anchor:
                subject_hits = {term for term in subjects if _present(term, body)}
                if len(subject_hits) < 2 and not subject_hits.intersection(single_anchor | target):
                    if not (row.agenda and _score(row.agenda, weights) >= threshold and score > 0):
                        continue
            # 명시 액션을 먼저, 그 뒤 같은 내용의 요약·결정 문장을 보충한다.
            bonus = max(direct, default=0) if row.kind == key else 0
            fact = row.fact
            if title_terms and rival_terms and any(_present(t, body) for t in rival_terms):
                clauses = re.split(r"(?<=하며),\s+|(?<=했고),\s+|(?<=하였고),\s+", fact.text)
                focused = [
                    c for c in clauses if any(_present(t, _normalized(c)) for t in title_terms)
                ]
                if len(clauses) > 1 and len(focused) == 1:
                    fact = replace(fact, text=focused[0])
            ranked[key].append((score + bonus, index, fact))
    facts: dict[str, tuple[MinutesExcerpt, ...]] = {}
    for key, found in ranked.items():
        seen: set[str] = set()
        selected: list[MinutesExcerpt] = []
        if key == "action" and any(rows[index].kind == "action" for _, index, _ in found):
            found = [item for item in found if rows[item[1]].kind == "action"]
        for _, _, fact in sorted(found, key=lambda item: (-item[0], item[1])):
            if key == "action" and selected and fact in facts.get("decision", ()):
                continue
            if fact.text not in seen and not any(
                _redundant(fact.text, old.text) for old in selected
            ):
                selected.append(fact)
                seen.add(fact.text)
            if len(selected) == 2:
                break
        facts[key] = tuple(selected)
    return TopicMinutes(meeting_id, hashlib.sha256(markdown.encode("utf-8")).hexdigest(), facts)
