"""tests/wiki/test_citation_verifier.py — Phase 4 TDD Red 단계

목적: core/wiki/citation_verifier.py 의 UtterancesCitationVerifier 인터페이스를
TDD Red 단계로 검증한다. 아직 core/wiki/citation_verifier.py 가 존재하지 않으므로
ImportError 로 모든 테스트가 Red 상태여야 한다.

테스트 범주:
    1. UtterancesCitationVerifier 기본 동작 (4건)
       - 빈 utterances 보수적 처리
       - 정확 매칭 True
       - ±2초 윈도우 경계값 True
       - 윈도우 밖 False
    2. 다른 회의 처리 (2건)
       - 알려지지 않은 meeting_id 보수적 False
       - 여러 회의 동시 등록 각각 독립 검증
    3. fetch_utterance (2건)
       - 존재하는 timestamp 텍스트 반환
       - 존재하지 않는 timestamp None 반환
    4. Edge cases (4건)
       - 음수 timestamp 보수적 False
       - tolerance_seconds=0 정확 매칭만
       - tolerance < 0 ValueError 발생
       - CitationVerifier Protocol 만족 검증

PRD §6 D2 핵심 요구사항:
    - meeting_id 가 알려진 회의인지
    - timestamp ±tolerance(기본 2초) 윈도우 안에 실제 발화가 존재하는지
    - 보수적 정책: 검증 정보가 없는 회의의 인용은 False

의존성:
    - pytest, pytest-asyncio
    - core.wiki.citation_verifier (Phase 4 미구현 → ImportError Red)
    - core.wiki.guard.CitationVerifier (Phase 2 실제 구현 존재)
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

# ─── Phase 4 대상 모듈 (아직 미구현 → ImportError Red) ──────────────────────
from core.wiki.citation_verifier import (  # type: ignore[import]  # noqa: E402
    UtterancesCitationVerifier,
)

# ─── Phase 2 실제 구현 (변경 금지) ───────────────────────────────────────────
from core.wiki.guard import CitationVerifier

# ─────────────────────────────────────────────────────────────────────────────
# Fixture — 인터페이스 정의 §1.2 Utterance Protocol 에 맞춘 FakeUtterance
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FakeUtterance:
    """테스트 전용 발화 dataclass.

    인터페이스 정의 §1.2 Utterance Protocol (duck-typing) 을 만족한다.
    corrector.CorrectedUtterance, merger.MergedUtterance 모두 동일 필드를 갖는다.

    Attributes:
        start: 발화 시작 (초 단위 float).
        end:   발화 종료 (초 단위 float).
        text:  발화 텍스트 (한국어 또는 혼합).
        speaker: 화자 레이블 (기본 "SPEAKER_00").
    """

    start: float
    end: float
    text: str
    speaker: str = "SPEAKER_00"


def make_utterances() -> list[FakeUtterance]:
    """기본 테스트 발화 목록을 반환한다.

    Returns:
        2건의 발화를 포함한 리스트.
        - 첫 번째: 10.0~15.0초 구간
        - 두 번째: 60.0~65.0초 구간
    """
    return [
        FakeUtterance(start=10.0, end=15.0, text="첫 번째 발화 내용입니다."),
        FakeUtterance(start=60.0, end=65.0, text="두 번째 발화 내용입니다."),
    ]


# 테스트에서 공통으로 사용할 meeting_id 상수
MEETING_M1 = "m1abc123"
MEETING_M2 = "m2def456"


# ─────────────────────────────────────────────────────────────────────────────
# 1. UtterancesCitationVerifier 기본 동작 (4건)
# ─────────────────────────────────────────────────────────────────────────────


class TestUtterancesCitationVerifierBasic:
    """UtterancesCitationVerifier 의 기본 verify_exists 동작을 검증한다."""

    @pytest.mark.asyncio
    async def test_빈_utterances_로_생성_시_verify_exists_false_반환(self) -> None:
        """빈 utterances_by_meeting 으로 생성한 verifier 는 보수적으로 False 를 반환한다.

        PRD §6 D2 보수적 정책: utterances 정보가 없으면 phantom 처리.

        Arrange: UtterancesCitationVerifier({}) — 빈 mapping
        Act:     verify_exists("abc12345", 60) 호출
        Assert:  False (보수적 반환)
        """
        # Arrange
        verifier = UtterancesCitationVerifier({})

        # Act
        result = await verifier.verify_exists("abc12345", 60)

        # Assert
        assert result is False, (
            "빈 utterances_by_meeting 에서 verify_exists 는 False 여야 함 — "
            "보수적 정책 (PRD §6 D2 '없으면 phantom')"
        )

    @pytest.mark.asyncio
    async def test_정확_매칭_utterance_존재_시_true_반환(self) -> None:
        """발화 구간 안에 timestamp 가 정확히 포함되면 True 를 반환한다.

        발화 구간: start=60.0, end=65.0
        timestamp_seconds=60 → 60.0 ≤ 60 ≤ 65.0 → True

        Arrange: utterances=[FakeUtterance(start=60.0, end=65.0, ...)]
        Act:     verify_exists(MEETING_M1, 60) 호출
        Assert:  True
        """
        # Arrange
        utterances = [FakeUtterance(start=60.0, end=65.0, text="두 번째 발화")]
        verifier = UtterancesCitationVerifier({MEETING_M1: utterances})

        # Act
        result = await verifier.verify_exists(MEETING_M1, 60)

        # Assert
        assert result is True, (
            f"timestamp=60 이 발화 구간 [60.0, 65.0] 내에 있으므로 True 여야 함, got {result}"
        )

    @pytest.mark.asyncio
    async def test_tolerance_2초_윈도우_경계값_start_마이너스_tolerance_에서_true(
        self,
    ) -> None:
        """tolerance_seconds=2 일 때 ts=58 은 윈도우 [60-2, 65+2] 안에 있으므로 True.

        인터페이스 §1.3 알고리즘:
            (utt.start ≤ ts + tolerance) AND (utt.end ≥ ts - tolerance)
            60.0 ≤ 58 + 2 = 60  → True
            65.0 ≥ 58 - 2 = 56  → True
            → 매칭 성공

        Arrange: Utterance(start=60.0, end=65.0) + tolerance_seconds=2 (기본값)
        Act:     verify_exists(MEETING_M1, 58) 호출
        Assert:  True
        """
        # Arrange
        utterances = [FakeUtterance(start=60.0, end=65.0, text="두 번째 발화")]
        verifier = UtterancesCitationVerifier({MEETING_M1: utterances})

        # Act
        result = await verifier.verify_exists(MEETING_M1, 58)

        # Assert
        assert result is True, "ts=58, tolerance=2 → 발화 [60.0, 65.0] 와 겹침 → True 여야 함"

    @pytest.mark.asyncio
    async def test_tolerance_2초_윈도우_밖_timestamp_false_반환(self) -> None:
        """ts=56 은 tolerance=2 윈도우 밖이므로 False 를 반환한다.

        인터페이스 §1.3 알고리즘:
            utt.start=60.0 ≤ ts + tol = 56 + 2 = 58 → 60.0 ≤ 58 이 False
            → 매칭 실패

        Arrange: Utterance(start=60.0, end=65.0) + tolerance_seconds=2 (기본값)
        Act:     verify_exists(MEETING_M1, 56) 호출
        Assert:  False
        """
        # Arrange
        utterances = [FakeUtterance(start=60.0, end=65.0, text="두 번째 발화")]
        verifier = UtterancesCitationVerifier({MEETING_M1: utterances})

        # Act
        result = await verifier.verify_exists(MEETING_M1, 56)

        # Assert
        assert result is False, (
            "ts=56, tolerance=2 → 발화 [60.0, 65.0] 와 겹치지 않음 → False 여야 함"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. 다른 회의 처리 (2건)
# ─────────────────────────────────────────────────────────────────────────────


class TestUtterancesCitationVerifierMultipleMeetings:
    """알려지지 않은 meeting_id 및 여러 회의 동시 등록 동작을 검증한다."""

    @pytest.mark.asyncio
    async def test_알려지지_않은_meeting_id_는_보수적으로_false_반환(self) -> None:
        """utterances_by_meeting 에 없는 meeting_id 는 보수적으로 False 를 반환한다.

        Phase 4 도메인: 단일 회의 verifier 가 다른 회의를 마주치면 phantom 처리.
        PRD §6 D2 / 인터페이스 §1.4 설계 결정 1.

        Arrange: m1 만 등록된 verifier 에 m2 로 질의
        Act:     verify_exists(MEETING_M2, 60) 호출
        Assert:  False (m2 를 모름 → phantom)
        """
        # Arrange
        utterances_m1 = [FakeUtterance(start=60.0, end=65.0, text="m1 발화")]
        verifier = UtterancesCitationVerifier({MEETING_M1: utterances_m1})

        # Act
        result = await verifier.verify_exists(MEETING_M2, 60)

        # Assert
        assert result is False, (
            f"verifier 가 '{MEETING_M1}' 만 알 때 '{MEETING_M2}' 질의는 False 여야 함 "
            "— cross-meeting 인용은 Phase 5 책임"
        )

    @pytest.mark.asyncio
    async def test_여러_회의_동시_등록_시_각각_독립적으로_검증한다(self) -> None:
        """utterances_by_meeting 에 m1, m2 동시 등록 시 각각 독립적으로 검증된다.

        Arrange:
            - m1: [FakeUtterance(start=60.0, end=65.0)]
            - m2: [FakeUtterance(start=100.0, end=110.0)]
        Act:     m1/ts=60, m2/ts=100, m1/ts=100 세 번 호출
        Assert:  True, True, False (m1 에는 ts=100 발화 없음)
        """
        # Arrange
        utterances_m1 = [FakeUtterance(start=60.0, end=65.0, text="m1 발화")]
        utterances_m2 = [FakeUtterance(start=100.0, end=110.0, text="m2 발화")]
        verifier = UtterancesCitationVerifier(
            {MEETING_M1: utterances_m1, MEETING_M2: utterances_m2}
        )

        # Act
        result_m1_60 = await verifier.verify_exists(MEETING_M1, 60)
        result_m2_100 = await verifier.verify_exists(MEETING_M2, 100)
        result_m1_100 = await verifier.verify_exists(MEETING_M1, 100)

        # Assert
        assert result_m1_60 is True, "m1 ts=60 → 발화 [60.0, 65.0] 내 → True 여야 함"
        assert result_m2_100 is True, "m2 ts=100 → 발화 [100.0, 110.0] 내 → True 여야 함"
        assert result_m1_100 is False, "m1 에 ts=100 에 해당하는 발화 없음 → False 여야 함"


# ─────────────────────────────────────────────────────────────────────────────
# 3. fetch_utterance (2건)
# ─────────────────────────────────────────────────────────────────────────────


class TestFetchUtterance:
    """fetch_utterance 의 텍스트 반환 및 None 반환 동작을 검증한다."""

    @pytest.mark.asyncio
    async def test_존재하는_timestamp_는_해당_발화_텍스트를_반환한다(self) -> None:
        """발화 구간 내 timestamp 에 대해 fetch_utterance 가 발화 텍스트를 반환한다.

        발화 구간: start=60.0, end=65.0
        timestamp_seconds=62 → 구간 내 → 텍스트 반환

        Arrange: Utterance(start=60.0, end=65.0, text="두 번째 발화 내용입니다.")
        Act:     fetch_utterance(MEETING_M1, 62) 호출
        Assert:  "두 번째 발화 내용입니다." 반환 (None 이 아님)
        """
        # Arrange
        expected_text = "두 번째 발화 내용입니다."
        utterances = [
            FakeUtterance(start=10.0, end=15.0, text="첫 번째 발화"),
            FakeUtterance(start=60.0, end=65.0, text=expected_text),
        ]
        verifier = UtterancesCitationVerifier({MEETING_M1: utterances})

        # Act
        result = await verifier.fetch_utterance(MEETING_M1, 62)

        # Assert
        assert result is not None, "ts=62 는 발화 [60.0, 65.0] 내에 있어야 텍스트 반환"
        assert result == expected_text, (
            f"반환 텍스트가 '{expected_text}' 이어야 하나 '{result}' 반환"
        )

    @pytest.mark.asyncio
    async def test_존재하지_않는_timestamp_는_none을_반환한다(self) -> None:
        """발화 구간 밖의 timestamp 에 대해 fetch_utterance 가 None 을 반환한다.

        Arrange: Utterance(start=60.0, end=65.0) — ts=999 는 범위 밖
        Act:     fetch_utterance(MEETING_M1, 999) 호출
        Assert:  None 반환
        """
        # Arrange
        utterances = [FakeUtterance(start=60.0, end=65.0, text="발화 텍스트")]
        verifier = UtterancesCitationVerifier({MEETING_M1: utterances})

        # Act
        result = await verifier.fetch_utterance(MEETING_M1, 999)

        # Assert
        assert result is None, (
            "ts=999 는 발화 구간 밖이므로 fetch_utterance 는 None 을 반환해야 함"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Edge cases (4건)
# ─────────────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    """음수 timestamp, tolerance=0, ValueError, Protocol 만족 등 엣지 케이스를 검증한다."""

    @pytest.mark.asyncio
    async def test_음수_timestamp_는_보수적으로_false_반환(self) -> None:
        """음수 timestamp_seconds 는 보수적으로 False 를 반환한다.

        인터페이스 §1.3 Note: 절대 예외를 raise 하지 않는다.
        음수 timestamp 는 유효하지 않으므로 매칭 발화가 없다고 판단.

        Arrange: 정상 발화 목록이 있는 verifier
        Act:     verify_exists(MEETING_M1, -10) 호출
        Assert:  False (음수 timestamp 는 발화 구간과 겹치지 않음)
        """
        # Arrange
        utterances = [FakeUtterance(start=0.0, end=5.0, text="초반 발화")]
        verifier = UtterancesCitationVerifier({MEETING_M1: utterances})

        # Act
        result = await verifier.verify_exists(MEETING_M1, -10)

        # Assert
        assert result is False, (
            "음수 timestamp -10 는 발화 구간 밖이거나 유효하지 않아 False 여야 함"
        )

    @pytest.mark.asyncio
    async def test_tolerance_seconds_0_모드_정확_매칭만_true(self) -> None:
        """tolerance_seconds=0 일 때 발화 구간에 정확히 포함되는 경우만 True.

        tolerance=0 이면 구간 겹침 조건:
            (utt.start ≤ ts + 0) AND (utt.end ≥ ts - 0)
            → utt.start ≤ ts ≤ utt.end

        Arrange: Utterance(start=60.0, end=65.0), tolerance_seconds=0
        Act:
            - verify_exists(MEETING_M1, 60)  → True (정확 매칭)
            - verify_exists(MEETING_M1, 58)  → False (tolerance 0이라 윈도우 없음)
        Assert: 각각 True, False
        """
        # Arrange
        utterances = [FakeUtterance(start=60.0, end=65.0, text="발화")]
        verifier = UtterancesCitationVerifier({MEETING_M1: utterances}, tolerance_seconds=0)

        # Act
        result_exact = await verifier.verify_exists(MEETING_M1, 60)
        result_outside = await verifier.verify_exists(MEETING_M1, 58)

        # Assert
        assert result_exact is True, (
            "tolerance=0 + ts=60 → utt.start=60.0 ≤ 60 ≤ 65.0 → True 여야 함"
        )
        assert result_outside is False, "tolerance=0 + ts=58 → 58 < utt.start=60.0 → False 여야 함"

    def test_tolerance_seconds_음수_전달_시_valueerror_발생(self) -> None:
        """tolerance_seconds < 0 으로 생성 시 ValueError 를 발생시킨다.

        인터페이스 §1.3 생성자 설명:
            tolerance_seconds < 0 → ValueError.

        Arrange: 정상 utterances + tolerance_seconds=-1
        Act:     UtterancesCitationVerifier 생성 시도
        Assert:  ValueError 발생
        """
        # Arrange
        utterances = [FakeUtterance(start=60.0, end=65.0, text="발화")]

        # Act & Assert
        with pytest.raises(ValueError, match=r"tolerance"):
            UtterancesCitationVerifier({MEETING_M1: utterances}, tolerance_seconds=-1)

    def test_utterances_citation_verifier_는_citation_verifier_protocol을_만족한다(
        self,
    ) -> None:
        """UtterancesCitationVerifier 가 CitationVerifier Protocol 을 만족한다.

        WikiGuard(verifier=UtterancesCitationVerifier(...)) 로 즉시 주입 가능한지
        isinstance 체크로 검증한다.

        Arrange: UtterancesCitationVerifier({}) 생성
        Act & Assert: isinstance(verifier, CitationVerifier) is True
        """
        # Arrange
        verifier = UtterancesCitationVerifier({})

        # Act & Assert
        assert isinstance(verifier, CitationVerifier), (
            "UtterancesCitationVerifier 가 CitationVerifier Protocol 을 만족하지 않음 "
            "— verify_exists, fetch_utterance async 메서드가 누락됐을 수 있음"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. 생성자 인덱스 불변성 (1건)
# ─────────────────────────────────────────────────────────────────────────────


class TestIndexImmutability:
    """생성자에서 빌드된 인덱스가 외부 변경에 영향을 받지 않음을 검증한다."""

    @pytest.mark.asyncio
    async def test_생성자_후_utterances_리스트_변경은_verifier에_영향없음(
        self,
    ) -> None:
        """생성자 호출 후 utterances 리스트를 변경해도 verifier 는 초기 상태를 유지한다.

        인터페이스 §1.3 설계: 생성자가 인덱스를 사전 빌드하므로 외부 리스트 변경은
        이미 빌드된 _index 와 _utterance_lookup 에 영향을 주지 않아야 한다.

        Arrange:
            - utterances = [Utterance(start=60.0, end=65.0, ...)]
            - verifier 생성 후 utterances 를 clear()
        Act:     verify_exists(MEETING_M1, 60) 호출
        Assert:  True (생성자에서 이미 인덱싱 완료, 외부 변경 무관)
        """
        # Arrange
        utterances: list[FakeUtterance] = [FakeUtterance(start=60.0, end=65.0, text="발화")]
        verifier = UtterancesCitationVerifier({MEETING_M1: utterances})
        utterances.clear()  # 외부에서 원본 리스트 변경

        # Act
        result = await verifier.verify_exists(MEETING_M1, 60)

        # Assert
        assert result is True, (
            "생성자 이후 외부 리스트 변경은 verifier 인덱스에 영향 없어야 함 "
            "— 생성자에서 defensive copy 또는 정렬·인덱스 사전 빌드 필요"
        )
