"""원문 ID를 보존하면서 회의 전체에서 일관된 화자 표시명을 만든다."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)


def build_speaker_labels(utterances: Iterable[Any]) -> dict[str, str]:
    """전체 발화의 첫 등장 순서로 표시명을 만들고 수동 이름은 보존한다.

    기존 Viewer 본문·복사의 번호를 유지하도록 UNKNOWN도 번호 자리를 차지한다.
    이 번호는 해당 회의 안의 표시일 뿐 실명이나 회의 간 동일 인물을 뜻하지 않는다.
    """
    labels: dict[str, str] = {}
    for utterance in utterances:
        speaker = (
            utterance.get("speaker")
            if isinstance(utterance, Mapping)
            else getattr(utterance, "speaker", None)
        )
        if not isinstance(speaker, str) or not speaker.strip() or speaker in labels:
            continue
        number = len(labels) + 1
        if speaker == "UNKNOWN":
            labels[speaker] = "참석자 ?"
        elif re.fullmatch(r"SPEAKER_\d+", speaker, flags=re.IGNORECASE):
            labels[speaker] = f"참석자 {number}"
        else:
            labels[speaker] = speaker
    return labels


def read_speaker_labels(config: Any, meeting_ids: Iterable[str]) -> dict[str, dict[str, str]]:
    """Viewer와 같은 우선순위·no-follow 원문에서 회의별 표시명만 읽는다.

    원문을 안전하게 확인하지 못하면 빈 맵을 돌려 숫자나 신원을 추측하지 않는다.
    요청 안에서 회의별 한 번만 읽으며 원문·인덱스·화자 필터는 수정하지 않는다.
    """
    from core.search_revision import read_source

    mappings: dict[str, dict[str, str]] = {}
    for meeting_id in dict.fromkeys(meeting_ids):
        try:
            _, source = read_source(config, meeting_id)
            utterances = source.get("utterances")
            if not isinstance(utterances, list):
                raise ValueError("전체 발화 목록을 확인할 수 없습니다.")
            mappings[meeting_id] = build_speaker_labels(utterances)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            logger.warning(f"화자 표시명 원문 조회 실패: {meeting_id} ({type(exc).__name__})")
            mappings[meeting_id] = {}
    return mappings
