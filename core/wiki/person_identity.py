"""회의별 화자 라벨을 여러 회의의 인물 식별자로 쓰지 않도록 구분한다."""

from __future__ import annotations

import re

_ANONYMOUS_LABEL_RE = re.compile(
    r"(?:speaker(?:\d+|xx|unknown|[a-z])?|unknown(?:speaker)?\d*|anonymous(?:speaker)?\d*|"
    r"unk|none|null|n/?a|화자\d*|참석자\d*|미상|익명|알수없음)",
    re.IGNORECASE,
)


def is_named_person_identity(value: object) -> bool:
    """비어 있거나 예약된 익명 라벨이면 인물 이름으로 사용하지 않는다.

    실명 추론이나 이름 유사도 판정은 하지 않는다. 명시된 이름 사이의 기존
    정확 일치 정책은 호출자가 유지하며 이 함수는 예약 라벨만 제외한다.
    """
    if not isinstance(value, str):
        return False
    name = value.strip("\"' \t\r\n")
    if not name:
        return False
    compact = re.sub(r"[\s_\-]+", "", name)
    return _ANONYMOUS_LABEL_RE.fullmatch(compact) is None
