"""Wiki의 안건별 기록을 현 전사·회의록과 대조하여 주제 채팅에 제공한다."""

from __future__ import annotations

import re
from dataclasses import replace

from config import AppConfig
from core.agenda_records import agenda_for_topic
from search.hybrid_search import SearchResult
from search.source_answers import SourceCandidate, build_source_candidates
from search.topic_brief import (
    _TRANSITION,
    FACETS,
    MeetingBrief,
    TopicFact,
    _fact_context,
    _grounded_fact,
    _planned_time,
)
from search.topic_evidence import agenda_evidence
from search.topic_minutes import MinutesExcerpt, TopicMinutes, topic_minutes
from search.topic_passages import _CANCELLED, Passage, _matches, _terms, read_passage_brief


def _meeting_followthrough(
    config: AppConfig,
    topic: str,
    parents: list[SearchResult],
    brief: MeetingBrief,
    minutes: TopicMinutes,
    first_range: tuple[int, ...],
) -> tuple[list[SearchResult], MeetingBrief]:
    """같은 안건을 다시 명명한 토론에서 회의 제안과 뒤의 같은 날 시간대·초대를 연결한다."""
    minutes_text = " ".join(e.text for e in minutes.facts.get("schedule", ()))
    days = set(re.findall(r"[월화수목금토일]요일|내일|모레|오늘", minutes_text))
    if not days or not re.search(r"회의|미팅|만나|면담", minutes_text):
        return parents, brief
    anchor = brief.anchor
    later = agenda_for_topic(config, anchor.meeting_id, topic)
    if not later or later.turn_indices[0] <= first_range[-1]:
        return parents, brief
    terms = _terms(topic)
    shared = _matches(terms, anchor.quote.text) & _matches(terms, later.title)
    if not shared or len(_matches(terms, later.title)) < min(2, len(terms)):
        return parents, brief
    indices = range(later.turn_indices[0], later.turn_indices[-1] + 14)
    extra, _ = agenda_evidence(config, anchor.meeting_id, tuple(indices))
    merged = [*parents, *(p for p in extra if p not in parents)]
    rows = [r for r in build_source_candidates(config, merged) if r.source_index in indices]
    rows.sort(key=lambda r: r.source_index)
    event = next(
        (
            idx
            for idx, row in enumerate(rows)
            if row.source_index in later.turn_indices
            and len(_matches(terms, row.quote.text)) >= min(2, len(terms))
        ),
        None,
    )
    if event is None:
        return parents, brief
    spoken_day = ""
    invites = []
    for r in rows[event + 1 :]:
        text = r.quote.text
        if _TRANSITION.search(text) or re.search(
            r"별개|별건|다른\s*(?:팀의|회의|약속|미팅|일정)|(?:취소|철회)(?:됐|했)", text
        ):
            break
        if not spoken_day:
            matched_day = next((day for day in sorted(days) if day in text), "")
            if matched_day and re.search(r"미팅|만나|회의|면담", text):
                spoken_day = matched_day
            continue
        if re.search(r"초대|인비(?:테이션)?|참석자", text) and not _CANCELLED.search(text):
            invites.append(r)
        if not (
            spoken_day in text
            and re.search(r"\d{1,2}\s*시.{0,10}(?:부터|에서|~|～|-)\s*\d{1,2}\s*시", text)
        ):
            continue
        # 초대 확인과 회의록의 회의 날짜가 모두 이어진 경우에만 단독 시간표현을 인정한다.
        close = [x for x in invites if 0 < r.source_index - x.source_index <= 5]
        if close:
            facts = dict(brief.facts)
            facts["schedule"] = TopicFact(r.quote.text, (r,))
            facts["action"] = _grounded_fact("action", tuple(close[-2:]))
            return merged, replace(brief, facts=facts)
    return parents, brief


def recorded_topic_context(
    config: AppConfig, topic: str, original_parents: list[SearchResult], original: MeetingBrief
) -> tuple[list[SearchResult], MeetingBrief, TopicMinutes | None] | None:
    """한 안건에 귀속된 항목을 읽고 실제 표시할 원문으로 참조를 다시 만든다."""
    anchor = original.anchor
    record = agenda_for_topic(config, anchor.meeting_id, topic, anchor_index=anchor.source_index)
    if record is None:
        return None
    parents, turns = agenda_evidence(config, anchor.meeting_id, record.turn_indices)
    current_anchor = next(
        (t for t in turns if t.source_index == anchor.source_index and t.quote == anchor.quote),
        None,
    )
    if current_anchor is None:
        return None
    terms = _terms(topic)
    hits = {t.source_index: _matches(terms, t.quote.text) for t in turns}
    coverage = set().union(*hits.values())
    if len(coverage) * 2 < len(set(terms)) or not hits[current_anchor.source_index]:
        return None
    # 같은 안건 제목 아래 별도 사례가 이어지면 질문에 직접 답한 대화가 우선한다.
    strength = max(map(len, hits.values()))
    anchors = [
        i
        for i, h in hits.items()
        if h and (len(h) >= strength or i == current_anchor.source_index)
    ]
    # 해당 안건을 처음 꺼내기 전의 마무리 약속은 제외한다. 앞서도 같은 안건을
    # 직접 다룬 발언이 있을 때만 그 지점부터 후보를 사용할 수 있다.
    first = min(
        i
        for i in anchors
        if i >= current_anchor.source_index
        or len(hits[i] & hits[current_anchor.source_index]) >= 2
    )
    turns = [t for t in turns if t.source_index >= first]
    anchors = [i for i in anchors if i >= first]
    verified = read_passage_brief(topic, "", [Passage(tuple(turns), 1)])
    if verified is None:
        return None
    indexed = {(t.source_index, t.quote.text): t for t in turns if not t.context_only}
    facts = dict(verified.facts)
    model_facts = False
    if original.used_model:
        by_source = {(t.source_index, t.quote): t for t in turns}
        for key, value in original.facts.items():
            if value.evidence and all(
                (t.source_index, t.quote) in by_source for t in value.evidence
            ):
                facts[key] = replace(
                    value,
                    evidence=tuple(by_source[t.source_index, t.quote] for t in value.evidence),
                )
                model_facts = True
    for key in FACETS:
        candidates: list[tuple[tuple[int, bool, int], SourceCandidate]] = []
        for fact in record.facts:
            if fact.kind != key or fact.source_index is None:
                continue
            row = indexed.get((fact.source_index, fact.text))
            if not row or _CANCELLED.search(row.quote.text):
                continue
            distance = min(abs(row.source_index - i) for i in anchors)
            direct = len(hits[row.source_index])
            if not direct and distance > 7:
                continue
            if key == "schedule" and not _planned_time(row.quote.text):
                continue
            # 앞 문장의 답변/반환물과 결합한 행을, 단순히 '하기로 했다'는 선언보다
            # 우선한다. 정확한 화자·시각은 최종 표시에도 원래 전사와 일치해야 한다.
            named = bool(fact.owner or fact.time_text)
            candidates.append(((direct, named, -distance), row))
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates:
            continue
        prior = facts.get(key)
        selected = list(prior.evidence) if prior else []
        parts = [prior.text] if prior else []
        for _, row in candidates:
            if row in selected:
                continue
            unit = _fact_context(key, (row,), turns, topic=topic)
            if len(parts) >= 2:
                break
            selected.extend(t for t in unit if t not in selected)
            parts.append(_grounded_fact(key, unit).text)
        facts[key] = TopicFact(" ".join(parts), tuple(selected))
    # 한 화자의 '조건/담당 판단 → 요청/약속' 발언이 잘려도 함께 보여 준다.
    # 명시적인 수락까지 이어졌을 때에만 결정란에 그 조건부 처리 방식을 올린다.
    action = facts.get("action")
    if action:
        evidence = set(action.evidence)
        for turn in action.evidence:
            contextual = _fact_context("action", (turn,), turns, topic=topic)
            evidence.update(contextual)
            assigned = re.search(
                r"(?:주시면|주신다면|해주면).{0,45}(?:겠습|할게|하겠)", turn.quote.text
            )
            if not assigned or "decision" in facts or len(contextual) < 2:
                continue
            after = [
                r for r in turns if turn.source_index < r.source_index <= turn.source_index + 2
            ]
            acceptance = next(
                (
                    r
                    for r in after
                    if r.quote.speaker != turn.quote.speaker
                    and re.fullmatch(
                        r"(?:네|예)[.!,\s]*(?:알겠습니다|그렇게\s*할게요)[.!\s]*", r.quote.text
                    )
                ),
                None,
            )
            if acceptance:
                facts["decision"] = TopicFact(
                    "조건부 합의 · " + " ".join(r.quote.text for r in contextual),
                    (*contextual, acceptance),
                )
        if len(evidence) > len(set(action.evidence)):
            ordered = tuple(sorted(evidence, key=lambda t: t.source_index))
            facts["action"] = _grounded_fact("action", ordered)
    # 근거 중간에 나오는 '오늘 5시'가 조건/제안인지 발언 어미와 함께 보존한다.
    brief = replace(verified, anchor=current_anchor, facts=facts, used_model=model_facts)
    if original.clarification and not verified.clarification:
        # 코드를 명시적으로 바로잡은 발언은 안건 색인 조각 뒤쪽에 있을 수 있다.
        # 검색 청크에 이미 대조한 정확한 화자·시각을 새 부모 번호로 연결한다.
        original_row = original.clarification
        original_parent = original_parents[original_row.parent_index]
        if original_parent not in parents:
            parents.append(original_parent)
        correction = replace(original_row, parent_index=parents.index(original_parent))
        brief = replace(
            brief,
            clarification=correction,
            facts={
                k: v
                for k, v in facts.items()
                if not (
                    k == "decision"
                    and any(t.source_index == anchor.source_index for t in v.evidence)
                )
            },
        )
    minutes = topic_minutes(config, anchor.meeting_id, topic)
    if not record.minutes_bound or minutes is None or minutes.digest != record.minutes_digest:
        return parents, brief, None
    allowed = set(record.minutes_excerpts)
    related: dict[str, tuple[MinutesExcerpt, ...]] = {}
    for key in FACETS:
        entries = []
        for entry in minutes.facts.get(key, ()):
            explicit = _matches(terms, entry.text)
            if entry.excerpt in allowed or (
                len(explicit) >= 2 and len(explicit & coverage) * 2 >= len(set(terms))
            ):
                entries.append(entry)
        # 질의 단어가 생략된 회의록의 후속 항목도 전사와 대조되어 이 안건에
        # 명시적으로 귀속된 기록이 있으면 유지한다.
        for fact in record.facts:
            if (
                fact.kind == key
                and fact.minutes_excerpt in allowed
                and fact.minutes_excerpt
                and fact.text in fact.minutes_excerpt
                and not any(x.excerpt == fact.minutes_excerpt for x in entries)
            ):
                entries.append(MinutesExcerpt(fact.text, fact.minutes_excerpt, record.title))
        related[key] = tuple(entries[:2])
    notes = replace(minutes, facts=related)
    parents, brief = _meeting_followthrough(
        config, topic, parents, brief, notes, record.turn_indices
    )
    return parents, brief, notes
