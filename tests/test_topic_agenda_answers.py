"""전사 후 게시한 안건 기록과 사용자가 실제 볼 항목의 연결을 검증한다."""

from pathlib import Path

import pytest

from config import AppConfig, PathsConfig
from core.agenda_records import AgendaRecord, refresh_agenda_records
from search.chat import _minutes_fast_context
from search.topic_agenda import _meeting_followthrough
from search.topic_brief import MeetingBrief
from search.topic_minutes import MinutesExcerpt, TopicMinutes
from tests.test_topic_evidence import _meeting
from tests.test_topic_passages import turn


def test_indexed_scope_excludes_same_meeting_prior_promise(tmp_path: Path) -> None:
    """새 안건 앞에 나온 약속과 다음 안건의 별도 마감을 제외한다."""
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    meeting_id = "meeting_20260105_130000"
    _meeting(
        config,
        meeting_id,
        "입장권 장부는 제가 보내겠습니다.",
        "다음 안건은 도예 전시회입니다. 내일 3시에 만나자고 했습니다.",
        "도예 전시회 안내장을 기획팀이 작성해서 드리겠습니다.",
        "다음 안건은 식당입니다. 월요일까지 급식표를 제출하겠습니다.",
    )
    refresh_agenda_records(config, meeting_id)
    result = _minutes_fast_context(config, "도예 전시회", 8300, allow_partial=True)
    assert result
    _, brief = result[0][0]
    assert "기획팀" in brief.facts["action"].text and "장부" not in brief.facts["action"].text
    timed = brief.facts["schedule"]
    assert "내일 3시" in timed.text and "제안" in timed.text and "월요일" not in timed.text


@pytest.mark.parametrize("switch", [False, True])
def test_repeated_agenda_clock_window_requires_same_event(
    monkeypatch: pytest.MonkeyPatch, switch: bool
) -> None:
    """반복 논의한 안건의 독립 시간 답변과 다음 안건으로 넘어간 경우를 구분한다."""
    topic = "가마 연료 점검"
    anchor = turn(1, "가마 연료 점검은요?")
    repeated = turn(12, "가마 연료 점검 건을 다시 말씀드리면")
    invite = turn(14, "초대받은 사람을 확인해서 더 필요한 분이 있으면 초대를 보내겠습니다.")
    clock = turn(16, "수요일 9시에서 10시")
    lines = [
        repeated,
        turn(13, "수요일 회의에서 논의하기로 했습니다."),
        invite,
        turn(15, "다음 안건은 선박 일정입니다." if switch else "네, 확인합니다."),
        clock,
    ]
    record = AgendaRecord(repeated.quote.text, (12, 13, 14), (), "s", "m", (), True)
    monkeypatch.setattr("search.topic_agenda.agenda_for_topic", lambda *args: record)
    monkeypatch.setattr("search.topic_agenda.agenda_evidence", lambda *args: ([], lines))
    monkeypatch.setattr("search.topic_agenda.build_source_candidates", lambda *args: lines)
    excerpt = MinutesExcerpt("가마 연료 점검은 수요일 회의에서 검토.", "원문", "일정")
    current = TopicMinutes(anchor.meeting_id, "m", {"schedule": (excerpt,)})
    _, out = _meeting_followthrough(
        AppConfig(), topic, [], MeetingBrief(anchor, {}, False), current, (1, 2)
    )
    if switch:
        assert not out.facts
    else:
        assert out.facts["action"].evidence == (invite,)
        assert out.facts["schedule"].evidence == (clock,)
