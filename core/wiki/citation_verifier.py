"""Wiki citation verifier — D2 인용 실재성 실제 검증 (Phase 4)

목적: Phase 3 의 `_NullVerifier` (steps/wiki_compiler.py) 를 교체한다.
LLM 이 출력한 [meeting:{id}@HH:MM:SS] 인용이 실제 회의의 utterances 시간대에
존재하는 발화를 가리키는지 timestamp 매칭으로 확인한다.

PRD §6 D2 핵심 요구사항:
    - meeting_id 가 알려진 회의(이번 ingest 의 회의)인지
    - timestamp ±tolerance(기본 2초) 윈도우 안에 실제 발화가 존재하는지
    - 보수적 정책: 검증 정보가 없는 회의의 인용은 False (phantom 처리)

Phase 4 범위 (단일 회의):
    utterances_by_meeting 은 **현재 회의만** 보장한다. cross-meeting 인용
    검증(예: 결정 페이지가 다른 회의를 참조) 은 Phase 5 에서 ChromaDB
    메타데이터 기반 verifier 로 별도 구현된다.

의존성:
    - core.wiki.guard.CitationVerifier (Protocol — 만족시킴)
    - corrector.CorrectedUtterance 호환 (duck-typing: speaker/text/start/end)
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class Utterance(Protocol):
    """corrector 단계의 발화. duck-typing 계약.

    Attributes:
        speaker: 화자 레이블.
        text:    발화 텍스트.
        start:   발화 시작 (초).
        end:     발화 종료 (초).
    """

    speaker: str
    text: str
    start: float
    end: float


# ─────────────────────────────────────────────────────────────────────────
# 1. UtterancesCitationVerifier — 단일 회의 utterances 기반 검증자
# ─────────────────────────────────────────────────────────────────────────


class UtterancesCitationVerifier:
    """utterances 기반 timestamp 매칭 검증.

    `core.wiki.guard.CitationVerifier` Protocol 을 만족하므로
    `WikiGuard(verifier=UtterancesCitationVerifier(...))` 로 즉시 주입 가능.

    Threading: 인스턴스는 immutable 설계. 같은 회의 ingest 동안 단일 코루틴에서
    호출되며, 회의별로 새 인스턴스를 생성한다.

    Attributes:
        _utterances_by_meeting: meeting_id → 정렬된 Utterance 리스트 (start asc).
            보수적 정책상 이 dict 에 키가 없는 meeting_id 의 인용은
            모두 False (phantom 처리) 로 판정.
        _tolerance_seconds: timestamp 허용 오차 (±초).
    """

    def __init__(
        self,
        utterances_by_meeting: dict[str, list[Utterance]],
        tolerance_seconds: int = 2,
    ) -> None:
        """utterances 매핑과 tolerance 를 받아 인덱스를 사전 빌드한다.

        보수적 정책 보장:
            - utterances_by_meeting 에 키가 없는 meeting_id → 항상 False.
            - utterances 가 빈 리스트인 meeting_id → 항상 False.
            - tolerance_seconds < 0 → ValueError.

        Args:
            utterances_by_meeting: meeting_id → Utterance 시퀀스. 생성자가
                start 기준 정렬·인덱스를 빌드한다.
            tolerance_seconds: ±허용 오차 (기본 2). PRD §6 D2.

        Raises:
            ValueError: tolerance_seconds < 0.
        """
        if tolerance_seconds < 0:
            raise ValueError(
                f"tolerance_seconds 는 0 이상이어야 합니다: {tolerance_seconds}"
            )

        # 외부 변경 영향 차단을 위해 즉시 복사 + 정렬 인덱스 빌드
        self._utterances_by_meeting: dict[str, list[Utterance]] = {}
        for meeting_id, utts in utterances_by_meeting.items():
            # list() 로 복사 + start 기준 오름차순 정렬
            sorted_utts = sorted(list(utts), key=lambda u: float(u.start))
            self._utterances_by_meeting[meeting_id] = sorted_utts

        self._tolerance_seconds: int = tolerance_seconds

    async def verify_exists(
        self,
        meeting_id: str,
        timestamp_seconds: int,
    ) -> bool:
        """주어진 (meeting_id, ts) 가 실제 발화에 매핑되는지 검사한다.

        알고리즘:
            1. meeting_id ∈ _utterances_by_meeting 인가? — 아니면 즉시 False.
            2. utterances 가 비어있는가? — 비었으면 False.
            3. ts ± tolerance 윈도우 내 발화 존재 여부 검사:
               (utt.start ≤ ts + tolerance) AND (utt.end ≥ ts - tolerance).

        Args:
            meeting_id: 8자리 hex.
            timestamp_seconds: 인용의 timestamp 를 초 단위로 변환한 정수.

        Returns:
            True: tolerance 윈도우 내 발화 존재.
            False: 알 수 없는 meeting_id, 빈 utterances, 또는 매칭 발화 없음.

        Note:
            절대 예외를 raise 하지 않는다 (WikiGuard.verify 의 graceful 정책 호환).
        """
        # 1. 알려지지 않은 meeting_id — 보수적 phantom
        utts = self._utterances_by_meeting.get(meeting_id)
        if utts is None:
            logger.warning(
                "D2 phantom: meeting_id=%s, ts=%d, reason=unknown_meeting",
                meeting_id,
                timestamp_seconds,
            )
            return False

        # 2. 빈 utterances — phantom
        if not utts:
            logger.warning(
                "D2 phantom: meeting_id=%s, ts=%d, reason=empty_utterances",
                meeting_id,
                timestamp_seconds,
            )
            return False

        # 3. 구간 겹침 검사 — 전체 순회 (단일 회의 발화 수백~수천 건 가정)
        tol = self._tolerance_seconds
        ts = timestamp_seconds
        for utt in utts:
            try:
                start = float(utt.start)
                end = float(utt.end)
            except (TypeError, ValueError):
                # 비정상 발화는 skip
                continue
            # (utt.start ≤ ts + tol) AND (utt.end ≥ ts - tol)
            if start <= ts + tol and end >= ts - tol:
                return True

        logger.warning(
            "D2 phantom: meeting_id=%s, ts=%d, reason=no_utterance",
            meeting_id,
            timestamp_seconds,
        )
        return False

    async def fetch_utterance(
        self,
        meeting_id: str,
        timestamp_seconds: int,
    ) -> str | None:
        """매핑된 발화 텍스트를 반환한다.

        verify_exists 와 동일한 매칭 로직을 적용하되, 텍스트를 반환한다.
        매칭 발화가 여러 건이면 ts 와 가장 가까운 발화의 text 를 반환.

        Args:
            meeting_id: 8자리 hex.
            timestamp_seconds: 초 단위 정수.

        Returns:
            매칭 발화의 text. 없으면 None.
        """
        utts = self._utterances_by_meeting.get(meeting_id)
        if not utts:
            return None

        tol = self._tolerance_seconds
        ts = timestamp_seconds

        # 매칭되는 발화 중 ts 와 가장 가까운(거리 최소) 발화를 선택
        best_text: str | None = None
        best_distance: float = float("inf")
        for utt in utts:
            try:
                start = float(utt.start)
                end = float(utt.end)
                text = str(utt.text)
            except (TypeError, ValueError, AttributeError):
                continue
            if start <= ts + tol and end >= ts - tol:
                # 발화 구간 중심점과의 거리 계산
                center = (start + end) / 2.0
                distance = abs(center - ts)
                if distance < best_distance:
                    best_distance = distance
                    best_text = text

        return best_text
