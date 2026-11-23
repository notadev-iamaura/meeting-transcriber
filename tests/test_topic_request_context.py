"""생략된 약속과 요청의 실제 범위를 합성 회의로 검증한다."""

import json

import pytest

from search.source_answers import SourceCandidate, SourceQuote
from search.topic_brief import _fact_context, _planned_time
from search.topic_passages import Passage, read_passage_brief


def _turn(i: int, text: str, speaker: str = "A", meeting: str = "alpha") -> SourceCandidate:
    """원문 순번·시각·화자가 고유한 합성 발언을 만든다."""
    return SourceCandidate(i, 0, i, meeting, SourceQuote(text, speaker, i, i + 1), speaker)


def test_reported_request_adds_object_and_stays_an_action() -> None:
    """문단 판독의 action이 원문 세 개를 직접 인용하고 합의를 만들지 않는다."""
    a = _turn(0, "아까 공방과 통화를 했는데요.")
    b = _turn(1, "재단 폭을 알고 싶다고 하더라고요.")
    c = _turn(2, "네, 제가 1번 정리해서 보내겠습니다.", "B")
    raw = json.dumps({"topic": {"ids": [0]}, "action": {"ids": [2], "quote": c.quote.text}})
    brief = read_passage_brief("공방 재단", raw, [Passage((a, b, c), 2)])
    assert brief and brief.facts["action"].evidence == (a, b, c)
    assert all(t.quote.text in brief.facts["action"].text for t in (a, b, c))
    assert "decision" not in brief.facts and "schedule" not in brief.facts


@pytest.mark.parametrize(
    "previous",
    [
        _turn(3, "이전에는 폭을 알고 싶다고 하셨어요."),
        _turn(3, "다음 안건은 견본이고 고객은 폭을 알고 싶다고 하더라고요."),
        _turn(2, "폭을 알고 싶다고 하더라고요."),
        _turn(3, "폭을 알고 싶다고 하더라고요.", "UNKNOWN"),
        _turn(3, "폭을 알고 싶다고 하더라고요.", meeting="beta"),
    ],
)
def test_report_across_scope_does_not_attach(previous: SourceCandidate) -> None:
    """안건·시점·연속성·화자·회의가 달라지는 인접 보고는 약속에 붙이지 않는다."""
    action = _turn(4, "이거 한번 정리해서 보내겠습니다.", "B")
    assert _fact_context("action", (action,), [previous, action]) == (action,)


def test_clear_object_does_not_attach_unrelated_request() -> None:
    previous = _turn(3, "다른 규격을 알고 싶다고 하시네요.")
    action = _turn(4, "제가 재고표를 보내겠습니다.", "B")
    assert _fact_context("action", (action,), [previous, action]) == (action,)
    assert _fact_context("schedule", (action,), [previous, action]) == (action,)


def test_missed_meeting_needs_actual_rescheduling() -> None:
    """불참으로 끝난 약속과 그 뒤 실제로 잡은 새 시간을 구분한다."""
    assert not _planned_time("오늘 업체와 얘기하기로 했는데 안 오셨네요.")
    assert not _planned_time("오늘 업체와 얘기하기로 했는데 안 오셔서 다시 연락하겠습니다.")
    assert _planned_time("오늘 업체와 얘기하기로 했는데 안 오셔서 내일 미팅을 다시 잡았어요.")
