"""같은 안건의 취소·일정 변경을 앞서 선택한 발언 및 회의록에 반영한다."""

from __future__ import annotations

import re
from dataclasses import replace
from itertools import combinations

from search.source_answers import SourceCandidate
from search.topic_brief import (
    _TRANSITION,
    FACETS,
    MeetingBrief,
    TopicFact,
    _grounded_fact,
    _planned_time,
)
from search.topic_minutes import TopicMinutes, _terms

_WHEN = re.compile(
    r"(?:(?:이번|다음|지난)\s*주\s*)?[월화수목금토일]요일|\d{1,2}월\s*\d{1,2}일|"
    r"(?:오[전후]\s*)?\d{1,2}시(?:\s*\d{1,2}분)?|오늘|내일|모레"
)
_ACTIVITY = re.compile(
    r"설치|교체|검토|검사|점검|테스트|작성|제출|전달|공유|발송|배포|회의|방문|교육|시연|예약|발주|입고|납품|촬영"
)
_CHANGE = re.compile(
    r"(취소|철회|무산|변경|연기|앞당|미[뤘루])(?:가\s*|를\s*|로\s*)?(?:됐|되었|될\s*예정|했|하였|되었습니다|합니다|하기로\s*(?:했|결정)|졌|겼|습니다)"
)
_UNSCHEDULED = re.compile(
    r"(?:새|다음|변경|향후|대체)?\s*(?:일정|날짜|시점|기한|시간).{0,16}(?:미정|미확정|정하지\s*않|정해지지\s*않|확정되지\s*않)"
)
_QUALIFIED = re.compile(
    r"않(?:기로|았|습)|안\s*(?:했|됐)|(?:만약|혹시).{0,35}(?:다면|경우)|"
    r"(?:다면|라면|으면|되면|오면|나면|하면|[할일된될인]\s*경우)(?=\s|[,，]|에는)|"
    r"(?:취소|변경|연기)(?:되면|할까요|할지|될지)|[?？]"
)
_NOISE = re.compile(
    r"^(?:그런데|그러면|그래서|그리고|이제|아니라|대신|새로운|다음|이번|기존|일정|계획|시간|날짜|원래|변경|취소|철회|연기|합의|결정|하기로|으로|하기|다시|아직|(?:진행|예정|계획|결정)(?:으로|하기로|할|하기)?|[가-힣]+(?:습니다|합니다|예요|했어요))$"
)


def _dates(text: str) -> set[str]:
    """반복하는 요일의 주차와 오전·오후를 보존한다."""
    return {re.sub(r"\s+", "", m.group()) for m in _WHEN.finditer(text)}


def _compatible_dates(left: set[str], right: set[str]) -> bool:
    """양쪽 모두 밝힌 날짜와 시각이 각각 양립하는지 확인한다."""
    for clock in (False, True):
        a = {d for d in left if ("시" in d) == clock}
        b = {d for d in right if ("시" in d) == clock}
        if a and b and not a & b:
            # '금요일'과 '다음 주 금요일'은 호환되지만 서로 다른 주차는 구분한다.
            unqualified_a = {
                re.sub(r"^(?:이번주|다음주|지난주|오전|오후)", "", value) for value in a
            }
            unqualified_b = {
                re.sub(r"^(?:이번주|다음주|지난주|오전|오후)", "", value) for value in b
            }
            if not (unqualified_a & b or a & unqualified_b):
                return False
    return True


def _subject(text: str) -> set[str]:
    """작업·일정·변경 어미를 제외한 실제 대상 식별어를 구한다."""
    cleaned = _ACTIVITY.sub(" ", _WHEN.sub(" ", text))
    return {
        t
        for t in _terms(cleaned)
        if not _NOISE.fullmatch(t) and not t.startswith(("취소", "철회", "변경", "연기"))
    }


def _same_plan(old: str, new: str, topic: str, *, adjacent: bool) -> bool:
    """별도로 진행하는 같은 주제의 작업과, 다른 대상의 동명 작업을 보호한다."""
    left, right = set(_ACTIVITY.findall(old)), set(_ACTIVITY.findall(new))
    if left and right and left.isdisjoint(right):
        return False
    a, b = _subject(old), _subject(new)
    before, after = _dates(old), _dates(new)
    same_date = bool(
        {"시" in value for value in before} & {"시" in value for value in after}
        and _compatible_dates(before, after)
    )
    if b:
        if not a & b:
            return False
        # 같은 시설 아래 여러 물건이 있을 때 한 물건의 취소를 다른 물건에 붙이지 않는다.
        if (a - b) and (b - a) and not ((a & b) - set(_terms(topic))):
            return False
        return bool(a & b and (left & right or same_date or adjacent))
    return adjacent and bool(same_date or left & right)


def reconcile_plan_updates(
    topic: str, brief: MeetingBrief, notes: TopicMinutes | None, turns: list[SourceCandidate]
) -> tuple[MeetingBrief, TopicMinutes | None]:
    """나중에 확정된 변경을 모든 표시란과 시간 근거에 일관되게 적용한다."""
    rows = sorted(
        (t for t in turns if t.meeting_id == brief.anchor.meeting_id), key=lambda t: t.source_index
    )
    retired: set[SourceCandidate] = set()
    for i, newer in enumerate(rows):
        text = newer.quote.text
        match = _CHANGE.search(text)
        if not match:
            continue
        change_clause = next(
            (s for s in re.split(r"(?<=[.!?。？！])\s+", text) if _CHANGE.search(s)), text
        )
        if _QUALIFIED.search(change_clause) or (
            match[1] == "변경"
            and not _dates(text)
            and not re.search(r"일정|날짜|기한|시점|마감", text)
        ):
            continue
        active = [brief.anchor, *(t for f in brief.facts.values() for t in f.evidence)]
        candidates = {
            t
            for t in rows[:i]
            if t not in retired
            if (_planned_time(t.quote.text) or t in active and _ACTIVITY.search(t.quote.text))
            and _same_plan(
                t.quote.text, text, topic, adjacent=newer.source_index - t.source_index <= 2
            )
            and not any(
                _TRANSITION.search(x.quote.text)
                for x in rows
                if t.source_index < x.source_index <= newer.source_index
            )
        }
        # '금요일이 아니라 수요일로 변경'은 금요일 회차만 대체한다.
        # 출발 시점 없이 목적 시점만 밝힌 변경은 기존 회차가 한 종류일 때만 연결한다.
        source_clause = re.split(
            r"(?:이|가)?\s*아니라|대신|말고|(?<=[일시분])에서", change_clause, maxsplit=1
        )[0]
        dates = _dates(source_clause)
        recurring = {frozenset(_dates(t.quote.text)) for t in candidates} - {frozenset()}
        destination_only = source_clause == change_clause and re.search(
            r"(?:일|시|분)(?:으)?로\s*(?:변경|연기|앞당|미[뤘루])", change_clause
        )
        if destination_only and len(recurring) > 1:
            continue
        old = {
            t
            for t in candidates
            if destination_only or _compatible_dates(_dates(t.quote.text), dates)
        }
        if (
            dates
            and "만" in source_clause
            and any(
                not _compatible_dates(_dates(a.quote.text), _dates(b.quote.text))
                for a, b in combinations(old, 2)
            )
        ):
            # '금요일만'으로 어느 주의 금요일을 뜻하는지 알 수 없으면 둘 다 지우지 않는다.
            continue
        tasks = {frozenset(_ACTIVITY.findall(t.quote.text)) for t in old}
        tasks.discard(frozenset())
        if not old or (not _ACTIVITY.search(text) and len(tasks) > 1):
            continue
        note_facts = {}
        changed_keys = set()
        for key, entries in notes.facts.items() if notes else ():
            retained = tuple(
                e
                for e in entries
                if e.text != text
                and not any(
                    _compatible_dates(_dates(t.quote.text), _dates(e.text))
                    and (
                        _same_plan(t.quote.text, e.text, topic, adjacent=False)
                        or _same_plan(text, e.text, topic, adjacent=False)
                    )
                    for t in old
                )
            )
            if len(retained) < len(entries):
                changed_keys.add(key)
            note_facts[key] = retained
        affected = {k for k, f in brief.facts.items() if any(t in old for t in f.evidence)}
        if not (changed_keys or affected or candidates.intersection(active)):
            continue
        retired.update(old)
        if notes:
            notes = replace(notes, facts=note_facts)
        # 인접한 '새 일정은 미정'도 동일한 취소의 결과일 때만 붙인다.
        update = [newer]
        if i + 1 < len(rows) and rows[i + 1].source_index == newer.source_index + 1:
            following = rows[i + 1]
            if _UNSCHEDULED.search(following.quote.text) and not _TRANSITION.search(
                following.quote.text
            ):
                update.append(following)
        facts = dict(brief.facts)
        changed_keys.update(affected)
        changed_keys.update(("decision", "schedule"))
        for k in FACETS.keys() & changed_keys:
            prior = facts.get(k)
            # 다른 업무의 온전한 발언만 보존하고 이미 취소된 문장은 제거한다.
            keep = [
                t
                for t in (prior.evidence if prior else ())
                if t not in old
                and t not in update
                and not t.context_only
                and (
                    _planned_time(t.quote.text)
                    if k == "schedule"
                    else bool(_ACTIVITY.search(t.quote.text))
                )
            ]
            if k == "schedule":
                for t in sorted(candidates - old, key=lambda t: t.source_index):
                    if t not in keep and not t.context_only and _planned_time(t.quote.text):
                        keep.append(t)
            parts = [_grounded_fact(k, (t,)).text for t in keep]
            parts.append(" ".join(t.quote.text for t in update))
            facts[k] = TopicFact(" ".join(parts), tuple([*keep, *update]))
        brief = replace(brief, anchor=newer if brief.anchor in old else brief.anchor, facts=facts)
    return brief, notes
