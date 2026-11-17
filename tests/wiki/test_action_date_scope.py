"""기한과 종료 화자를 해당 인용의 실제 발화 범위에서만 판정하는 회귀."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from core.wiki.extractors.action_item import ActionItemExtractor
from core.wiki.models import Citation


def _row(text: str, start: float, end: float, speaker: str = "SPEAKER_01") -> SimpleNamespace:
    """개인 자료가 없는 가상 발화다."""
    return SimpleNamespace(text=text, start=start, end=end, speaker=speaker)


def _due(
    rows: list[SimpleNamespace], timestamp: str = "00:00:20", raw_due: str | None = "2026-01-02"
) -> str | None:
    """모델 호출 없이 단일 추출 결과의 기한 검증만 수행한다."""
    result = ActionItemExtractor._build_new_action_item(
        {
            "owner": "SPEAKER_01",
            "description": "가상 자료 전달",
            "citation_ts": timestamp,
            "due_date": raw_due,
        },
        meeting_id="date_fixture",
        meeting_date=date(2026, 1, 1),
        speakers={"SPEAKER_01"},
        all_text=" ".join(row.text for row in rows),
        utterances=rows,
        fallback_citation=Citation("date_fixture", "00:00:10", 10),
    )
    assert result is not None
    return result.due_date


@pytest.mark.parametrize(
    "text,expected",
    [
        ("오늘 자료를 전달할게요.", "2026-01-01"),
        ("내일 자료를 전달할게요.", "2026-01-02"),
        ("자료를 전달할게요.", None),
        ("내일이나 모레 전달할게요.", None),
    ],
)
def test_other_tasks_dates_do_not_propagate(text: str, expected: str | None) -> None:
    """회의 다른 발화의 날짜로 해당 업무의 기한을 채우지 않는다."""
    assert _due([_row("내일 다른 점검을 하겠습니다.", 10, 11), _row(text, 20, 21)]) == expected


def test_floor_exact_target_beats_overlapping_previous_utterance() -> None:
    """정수초 직전 요청의 내일 대신 해당 초에 시작한 약속의 오늘을 사용한다."""
    rows = [
        _row("내일 자료를 전달해 주세요.", 10, 20.5, "SPEAKER_00"),
        _row("오늘 전달할게요.", 20.2, 21),
    ]
    assert _due(rows) == "2026-01-01"
    assert ActionItemExtractor._find_speaker_at(rows, 20) == "SPEAKER_01"


def test_same_second_multiple_tasks_leave_due_and_speaker_unresolved() -> None:
    """같은 정수초의 다른 업무·화자를 임의 첫 발화로 정하지 않는다."""
    rows = [_row("내일 전달할게요.", 20.1, 20.4), _row("오늘 전달할게요.", 20.5, 21, "SPEAKER_02")]
    assert _due(rows) is None
    assert ActionItemExtractor._find_speaker_at(rows, 20) == ""


@pytest.mark.parametrize("timestamp", ["", "invalid", "00:00:50"])
def test_invalid_or_unmapped_citation_does_not_borrow_first_date(timestamp: str) -> None:
    """인용 fallback은 날짜·수행 화자 추정의 근거가 아니다."""
    rows = [_row("내일 전달할게요.", 10, 11)]
    assert _due(rows, timestamp) is None
    assert ActionItemExtractor._find_speaker_at(rows, 50) == ""


def test_explicit_date_in_other_task_does_not_validate_model_date() -> None:
    """명시 날짜의 존재 여부도 해당 인용 발화에서만 검사한다."""
    rows = [_row("2026-01-02까지 점검하겠습니다.", 10, 11), _row("자료를 전달할게요.", 20, 21)]
    assert _due(rows) is None


def test_speaker_edge_tolerance_is_bounded_and_invalid_rows_are_ignored() -> None:
    """실제 구간이나 2초 경계 밖의 화자·비정상 시각은 사용하지 않는다."""
    rows = [_row("가상 발화", float("nan"), 10), _row("가상 발화", 20, 21)]
    assert ActionItemExtractor._find_speaker_at(rows, 23) == "SPEAKER_01"
    assert ActionItemExtractor._find_speaker_at(rows, 24) == ""


@pytest.mark.parametrize(
    "text,raw_due,expected",
    [
        ("2026-05-01까지 전달할게요.", "2026-05-01", "2026-05-01"),
        ("2026-05-01까지 전달할게요.", "2026-05-10", None),
        ("2027/5/1까지 전달할게요.", "2027-05-01", "2027-05-01"),
        ("2027년 5월 1일까지 전달할게요.", "2026-05-01", None),
        ("2027년 5월 1일까지 전달할게요.", "2027-05-01", "2027-05-01"),
        ("5월 1일까지 전달할게요.", "2026-05-01", "2026-05-01"),
        ("5월 1일까지 전달할게요.", "2026-05-10", None),
        ("5/1까지 전달할게요.", "2026-05-01", "2026-05-01"),
        ("5/1까지 전달할게요.", "2027-05-01", None),
        ("내년 5월 1일까지 전달할게요.", "2027-05-01", None),
        ("5/1이나 5/2까지 전달할게요.", "2026-05-01", None),
        ("내일인 2026-01-03까지 전달할게요.", "2026-01-03", None),
        ("내일인 2026-01-02까지 전달할게요.", "2026-01-02", "2026-01-02"),
        ("13월 40일까지 전달할게요.", "2026-05-01", None),
        ("5월 1일까지 전달할게요.", "invalid", None),
    ],
)
def test_explicit_date_must_match_quoted_day_and_year(
    text: str, raw_due: str, expected: str | None
) -> None:
    """날짜가 언급됐다는 이유만으로 다른 일자·연도의 모델 응답을 수용하지 않는다."""
    assert _due([_row(text, 20, 21)], raw_due=raw_due) == expected
