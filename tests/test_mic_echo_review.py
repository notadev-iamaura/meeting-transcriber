"""마이크와 상대방 녹음에서 비슷하게 보이는 문장을 검토하는 기준을 확인한다."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

import pytest

from core.mic_echo_review import find_mic_echo_candidates


@dataclass
class Row:
    """테스트용 합성 발화."""

    speaker: str
    start: float
    text: str


_LINE = "월요일 오전 회의 자료를 공유하겠습니다"


def test_order_and_best_one_to_one_match() -> None:
    """목록 순서와 무관하게 완전 일치를 우선하고 한 상대 발화를 재사용하지 않는다."""
    rows = [
        Row("SPEAKER_0", 20, _LINE),
        Row("나", 21, _LINE + "요"),
        Row("나", 17, unicodedata.normalize("NFD", _LINE.replace(" ", "") + ".")),
        Row("나", 40, _LINE),
        Row("SPEAKER_0", 41, _LINE + "요"),
    ]
    assert find_mic_echo_candidates(rows) == {2: 0, 3: 4}


@pytest.mark.parametrize(
    ("other", "at", "self_text", "remote_text"),
    [
        ("SPEAKER_0", 20, "네 맞습니다", "네 맞습니다"),
        ("SPEAKER_0", 23.1, _LINE, _LINE),
        ("SPEAKER_0", 20, _LINE, "저는 화요일 오후에 다른 안건을 논의하고 싶습니다"),
        ("UNKNOWN", 20, _LINE, _LINE),
        ("참석자 ?", 20, _LINE, _LINE),
        ("", 20, _LINE, _LINE),
        ("나", 20, _LINE, _LINE),
        ("SPEAKER_0", float("nan"), _LINE, _LINE),
        ("SPEAKER_0", 20, "가" * 513, "가" * 513),
    ],
)
def test_rejects_unreliable_matches(
    other: str, at: float, self_text: str, remote_text: str
) -> None:
    """불명 화자·짧은 맞장구·다른 동시 발화·장문은 숨김 후보에 포함하지 않는다."""
    assert find_mic_echo_candidates([Row("나", 20, self_text), Row(other, at, remote_text)]) == {}


def test_dense_timestamp_is_unreviewable_but_later_turn_still_matches() -> None:
    """시간이 몰린 목록에서 임의 매칭을 피하고 다른 시간대는 독립적으로 비교한다."""
    rows = [Row("나", 0, _LINE), *[Row("SPEAKER_0", 0, _LINE) for _ in range(30)]]
    rows.extend([Row("나", 10, _LINE), Row("SPEAKER_1", 10, _LINE)])
    assert find_mic_echo_candidates(rows) == {31: 32}
