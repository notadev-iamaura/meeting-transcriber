"""회의 주제의 조건부 판단과 같은 담당자의 연속 후속 작업을 원문으로 검증한다."""

import pytest

from search.source_answers import SourceCandidate, SourceQuote
from search.topic_brief import parse_topic_brief


def _turn(i: int, text: str, speaker: str = "A") -> SourceCandidate:
    """서로 다른 참여자가 실제로 발언한 합성 문장을 만든다."""
    return SourceCandidate(i, 0, i, "synthetic", SourceQuote(text, speaker, i, i + 1), speaker)


def test_conditional_limit_and_booked_followup_survive_missing_model_fields() -> None:
    turns = [
        _turn(0, "반송 식별자는 현재 확인하기 어렵습니다."),
        _turn(1, "그게 별도 이력이 없으면 구분할 수는 없을 것 같아요.", "B"),
        _turn(2, "일단 담당자분께 문의하고"),
        _turn(3, "제가 내일 면담을 하나 세팅을 했어요."),
    ]
    brief = parse_topic_brief("반송 식별자", "", turns)
    assert brief and brief.facts["decision"].evidence == tuple(turns[:2])
    assert "확정 여부" in brief.facts["decision"].text
    assert turns[2] in brief.facts["action"].evidence
    assert brief.facts["schedule"].evidence == (turns[3],)


@pytest.mark.parametrize(
    ("text", "speaker"),
    [
        ("제가 내일 면담을 하나 세팅을 했어요.", "B"),
        ("다음 안건은 교육입니다. 제가 내일 미팅을 하나 세팅을 했어요.", "A"),
        ("다른 업체 직원은 내일 면담을 세팅했어요.", "A"),
    ],
)
def test_booking_outside_the_followup_is_excluded(text: str, speaker: str) -> None:
    turns = [
        _turn(0, "반송 식별자는 조회할 수 없습니다."),
        _turn(1, "아직 그렇습니다.", "B"),
        _turn(2, "담당자에게 문의하고"),
        _turn(3, text, speaker),
    ]
    brief = parse_topic_brief("반송 식별자", "", turns)
    assert brief and "schedule" not in brief.facts


def test_concrete_resolution_target_remains_a_goal() -> None:
    turns = [
        _turn(0, "반송 식별자에 문제가 있습니다."),
        _turn(1, "이거를 이번 주 안에 해결하는 걸 목표로 하고 있는 상태입니다."),
    ]
    brief = parse_topic_brief("반송 식별자", "", turns)
    assert brief and brief.facts["schedule"].evidence == (turns[1],)
    assert "목표" in brief.facts["schedule"].text
