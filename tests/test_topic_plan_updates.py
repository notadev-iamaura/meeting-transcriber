"""이미 결정된 계획의 취소·대체와 다른 안건의 취소를 표시란별로 구분한다."""

import re
from pathlib import Path

import pytest

from config import AppConfig, PathsConfig
from core.agenda_records import refresh_agenda_records
from search.chat import _minutes_fast_context, _render_topic_briefs
from search.topic_brief import MeetingBrief, TopicFact
from search.topic_updates import reconcile_plan_updates
from tests.test_topic_evidence import _meeting
from tests.test_topic_passages import turn


@pytest.mark.parametrize("with_index", [False, True])
@pytest.mark.parametrize("reschedule", [False, True])
def test_last_decision_overrides_prior_weekday_and_minutes(
    tmp_path: Path, with_index: bool, reschedule: bool
) -> None:
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    mid, topic = "meeting_20260105_130000", "온실 차광막"
    first = "온실 차광막은 다음 주 금요일에 설치하기로 했습니다."
    replacement = (
        "온실 차광막은 금요일이 아니라 수요일로 변경하기로 했습니다."
        if reschedule
        else "그런데 온실 차광막 설치는 취소됐습니다."
    )
    extra = [] if reschedule else ["새 일정은 아직 정하지 않았습니다."]
    _meeting(config, mid, first, replacement, *extra)
    out = config.paths.resolved_outputs_dir / mid
    out.mkdir(parents=True)
    (out / "meeting_minutes.md").write_text(
        "## 결정 사항\n- 온실 차광막 설치는 금요일 진행으로 결정했습니다.\n"
        "## 액션 아이템\n- 온실 차광막 설치는 금요일 진행할 예정입니다.\n"
        "## 일정\n- 온실 차광막 설치는 금요일에 진행합니다."
    )
    if with_index:
        refresh_agenda_records(config, mid)
    result = _minutes_fast_context(config, topic, 8200, allow_partial=True)
    assert result
    answer, refs = _render_topic_briefs(topic, result[0], result[1])
    slots = dict(re.findall(r"^• (.*?) · (.+)$", answer, re.M))
    assert first[:-1] not in answer
    assert "금요일 진행" not in answer and "금요일에 진행" not in answer
    assert all(("수요일" if reschedule else "취소") in body for body in slots.values())
    assert any(r.quote and r.quote.text == replacement for r in refs)
    if extra:
        assert "새 일정은 아직 정하지 않았습니다" in slots["일정"]


@pytest.mark.parametrize(
    "new",
    [
        "온실 차광막 도면 제출은 취소됐습니다.",
        "다음 안건은 정원입니다. 온실 차광막 설치는 취소됐습니다.",
        "온실 차광막 설치가 취소되면 공유하겠습니다.",
        "온실 차광막 설치는 취소하지 않았습니다.",
        "비가 오면 온실 차광막 설치는 금요일만 취소하기로 했습니다.",
    ],
)
def test_other_activity_or_tentative_update_preserves_active_task(new: str) -> None:
    first = turn(0, "온실 차광막은 금요일에 설치하기로 했습니다.")
    original = MeetingBrief(first, {"schedule": TopicFact(first.quote.text, (first,))}, True)
    result, _ = reconcile_plan_updates("온실 차광막", original, None, [first, turn(1, new)])
    assert result == original


def test_unspecified_week_does_not_cancel_both_fridays() -> None:
    """다른 주에 같은 활동이 있으면 어느 주인지 밝히지 않은 취소를 전파하지 않는다."""
    first = turn(0, "온실 차광막 설치는 이번 주 금요일에 진행하기로 했습니다.")
    second = turn(1, "온실 차광막 설치는 다음 주 금요일에도 진행하기로 했습니다.")
    original = MeetingBrief(first, {"schedule": TopicFact("두 회차", (first, second))}, True)
    change = turn(2, "온실 차광막 설치는 금요일만 취소됐습니다.")
    result, _ = reconcile_plan_updates("온실 차광막", original, None, [first, second, change])
    assert result == original


@pytest.mark.parametrize("with_index", [False, True])
@pytest.mark.parametrize(
    ("first_slot", "second_slot", "update"),
    [
        ("이번 주 금요일", "다음 주 금요일", "다음 주 금요일만 취소됐습니다."),
        ("월요일", "금요일", "금요일이 아니라 수요일로 변경하기로 했습니다."),
        ("금요일 오전 2시", "금요일 오후 2시", "금요일 오후 2시만 취소됐습니다."),
    ],
)
def test_one_occurrence_changes_without_removing_another(
    tmp_path: Path, with_index: bool, first_slot: str, second_slot: str, update: str
) -> None:
    """같은 활동을 두 번 예약한 뒤 한 회차만 바꾸면 나머지 발언과 일정이 보존된다."""
    cfg = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    mid, topic = "meeting_20260105_130000", "온실 차광막"
    first = f"온실 차광막 설치는 {first_slot}에 진행하기로 했습니다."
    second = f"온실 차광막 설치는 {second_slot}에도 진행하기로 했습니다."
    change = f"온실 차광막 설치는 {update}"
    _meeting(cfg, mid, first, second, change)
    out = cfg.paths.resolved_outputs_dir / mid
    out.mkdir(parents=True)
    (out / "meeting_minutes.md").write_text(
        f"## 결정 사항\n- {first}\n- {second}\n## 일정\n- {first}\n- {second}"
    )
    if with_index:
        refresh_agenda_records(cfg, mid)
    result = _minutes_fast_context(cfg, topic, 8200, allow_partial=True)
    assert result
    answer, refs = _render_topic_briefs(topic, result[0], result[1])
    schedule = answer.split("• 일정 · ")[1]
    assert first[:-1] in schedule and change[:-1] in schedule
    assert second[:-1] not in answer
    assert any(ref.quote and ref.quote.text == change for ref in refs)
