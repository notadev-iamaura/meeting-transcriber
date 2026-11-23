"""주제에 대한 명시적 적용 기준과 한정 운영을 원문 그대로 제시한다."""

import json

import pytest

from search.source_answers import SourceCandidate, SourceQuote
from search.topic_brief import parse_topic_brief
from search.topic_passages import Passage, read_passage_brief


def _row(idx: int, text: str) -> SourceCandidate:
    """실제 발언 대신 합성 과수원 운영 발화를 만든다."""
    return SourceCandidate(idx, 0, idx, "orchard", SourceQuote(text, "S", idx, idx + 1), "S")


@pytest.mark.parametrize(
    "text",
    [
        "배 선별은 겉면의 흠집 기준으로 판단하면 됩니다.",
        "배 선별은 크기가 아니라 겉면의 흠집 기준으로 판단하면 돼요.",
        "배 선별은 기존 방식 그대로 유지합니다.",
        "배 선별 규칙은 직판용 열매에만 적용됩니다.",
    ],
)
def test_model_topic_quote_can_supply_an_explicit_rule(text: str) -> None:
    a = _row(5, text)
    raw = json.dumps({"topic": {"ids": [5], "quote": text}, "decision": None})
    brief = read_passage_brief("배 선별", raw, [Passage((a,), 2)])
    assert brief and brief.facts["decision"].text == text
    assert brief.facts["decision"].evidence == (a,)
    assert "schedule" not in brief.facts


@pytest.mark.parametrize(
    "text",
    [
        "꿀벌 계측은 다음 달부터 2주간 관찰자 세 명으로 시범 운영을 시작합니다.",
        "꿀벌 계측은 2주 동안 관찰자 3명으로 파일럿 진행",
    ],
)
def test_pilot_keeps_the_stated_period_and_size_without_making_dates(text: str) -> None:
    a = _row(12, text)
    brief = parse_topic_brief("꿀벌 계측", "", [a])
    assert brief and set(brief.facts) == {"decision", "action", "schedule"}
    assert all(fact.text == text and fact.evidence == (a,) for fact in brief.facts.values())


@pytest.mark.parametrize(
    "text",
    [
        "배 선별에 기존 방식이 좋은지는 아직 결정하지 않았습니다.",
        "배 선별을 다음 달에 시범 운영하면 어떨까요?",
        "예를 들어 배 선별을 관찰자 일곱 명으로 시범 운영을 시작합니다.",
        "배 선별은 시범 운영하기로 한 건 아니에요.",
        "배 선별은 직판용에만 적용할지 검토 중입니다.",
    ],
)
def test_questions_reports_negation_and_examples_never_become_settled_rules(text: str) -> None:
    brief = parse_topic_brief("배 선별", "", [_row(3, text)])
    assert brief
    fact = brief.facts.get("decision")
    assert fact is None or ("확정 여부" in fact.text and text in fact.text)


def test_other_agenda_pilot_does_not_create_a_decision() -> None:
    rows = [
        _row(0, "꿀벌 계측을 논의했습니다."),
        _row(1, "다음 안건은 방문객 등록입니다."),
        _row(2, "다음 주에 관찰자 다섯 명으로 시범 운영을 시작합니다."),
    ]
    brief = parse_topic_brief("꿀벌 계측", "", rows)
    assert brief and not brief.facts
