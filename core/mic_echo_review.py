"""분리 녹음 전사문에서 사용자가 원음을 비교할 마이크·상대 발화 쌍을 찾는다."""

from __future__ import annotations

import math
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import Protocol

from core.recording_channels import SELF_SPEAKER

_WINDOW_SECONDS = 3.0
_MIN_CHARS = 12
_MAX_CHARS = 512
_MIN_RATIO = 0.85
_MAX_NEIGHBORS = 12
# 반복 문자열의 최악 비용과 과밀한 타임스탬프에서의 후보 수를 제한한다.
_MAX_FUZZY_WORK = 8_000_000


class _Utterance(Protocol):
    """화면에 표시할 전사 발화에서 비교에 필요한 세 필드."""

    text: str
    speaker: str
    start: float


def find_mic_echo_candidates(utterances: Sequence[_Utterance]) -> dict[int, int]:
    """유사도·시간 차 순서로 중복 없이 마이크 행 번호를 상대 행 번호에 연결한다.

    녹음 출처를 확인한 호출자만 사용한다. 시작 시각 차이가 3초 이내이며 양쪽
    표기가 12~512자로 유사도 85% 이상인 경우를 반환한다. 후보는 청취 검토용이다.
    """
    mic: list[tuple[float, int, str]] = []
    remote: list[tuple[float, int, str]] = []
    for index, row in enumerate(utterances):
        if not math.isfinite(row.start) or row.start < 0 or len(row.text) > _MAX_CHARS * 4:
            continue
        speaker = row.speaker.strip()
        if not speaker or speaker in {"UNKNOWN", "참석자 ?"}:
            continue
        text = unicodedata.normalize("NFC", row.text).casefold()
        text = "".join(ch for ch in text if ch.isalnum())
        if _MIN_CHARS <= len(text) <= _MAX_CHARS:
            (mic if speaker == SELF_SPEAKER else remote).append((row.start, index, text))
    if not mic or not remote:
        return {}

    remote.sort()
    starts = [row[0] for row in remote]
    pairs: list[tuple[float, float, int, int]] = []
    remaining_work = _MAX_FUZZY_WORK
    for start, i, text in mic:
        lo = bisect_left(starts, start - _WINDOW_SECONDS)
        hi = bisect_right(starts, start + _WINDOW_SECONDS)
        if hi - lo > _MAX_NEIGHBORS:
            continue
        for other_start, j, other in remote[lo:hi]:
            ratio = 1.0
            if text != other:
                cost = len(text) * len(other)
                if cost > remaining_work:
                    continue
                matcher = SequenceMatcher(None, text, other, autojunk=False)
                if matcher.real_quick_ratio() < _MIN_RATIO or matcher.quick_ratio() < _MIN_RATIO:
                    continue
                remaining_work -= cost
                ratio = matcher.ratio()
            if ratio >= _MIN_RATIO:
                pairs.append((-ratio, abs(start - other_start), i, j))

    matches: dict[int, int] = {}
    used_remote: set[int] = set()
    for _, _, i, j in sorted(pairs):
        if i not in matches and j not in used_remote:
            matches[i] = j
            used_remote.add(j)
    return matches
