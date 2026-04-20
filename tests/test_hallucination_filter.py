"""환각 필터링 모듈 테스트.

환각(hallucination) 감지 및 필터링 로직의 정확성을 검증한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

from steps.hallucination_filter import (
    detect_repetition,
    filter_hallucinations,
)


@dataclass
class MockSegment:
    """테스트용 전사 세그먼트 모의 객체."""

    text: str
    start: float = 0.0
    end: float = 1.0
    avg_logprob: float = -0.5
    no_speech_prob: float = 0.1
    compression_ratio: float = 1.0


@dataclass
class MockFilterConfig:
    """테스트용 환각 필터 설정 모의 객체."""

    enabled: bool = True
    compression_ratio_threshold: float = 2.4
    logprob_threshold: float = -1.0
    no_speech_threshold: float = 0.6
    repetition_threshold: int = 3


class TestDetectRepetition:
    """반복 패턴 감지 함수 테스트."""

    def test_반복_없는_정상_텍스트(self) -> None:
        """정상 텍스트에서는 반복을 감지하지 않는다."""
        assert detect_repetition("안녕하세요 오늘 회의를 시작하겠습니다") is False

    def test_동일_단어_3회_반복(self) -> None:
        """동일 단어가 3회 연속 반복되면 감지한다."""
        assert detect_repetition("감사합니다 감사합니다 감사합니다") is True

    def test_동일_문자_연속_반복(self) -> None:
        """동일 문자가 연속 반복되면 감지한다."""
        assert detect_repetition("확확확확확확") is True

    def test_짧은_텍스트_무시(self) -> None:
        """4자 미만 텍스트는 검사하지 않는다."""
        assert detect_repetition("안녕") is False

    def test_빈_문자열(self) -> None:
        """빈 문자열은 False를 반환한다."""
        assert detect_repetition("") is False

    def test_임계값_2_설정(self) -> None:
        """임계값을 2로 설정하면 2회 반복도 감지한다."""
        assert detect_repetition("네 네", threshold=2) is True

    def test_임계값_미달_반복(self) -> None:
        """임계값 미달 반복은 감지하지 않는다."""
        assert detect_repetition("감사합니다 감사합니다", threshold=3) is False

    def test_패턴_반복_감지(self) -> None:
        """반복 패턴 문자열을 감지한다 (ghost613 환각 패턴)."""
        assert detect_repetition("마칠 마칠 마칠 마칠") is True

    def test_텍스트_중간_반복_감지(self) -> None:
        """텍스트 중간에 위치한 반복도 감지한다."""
        assert detect_repetition("정상텍스트확확확확확") is True

    def test_긴_구문_반복_감지(self) -> None:
        """다음 다음 다음 같은 2자 이상 구문 반복을 감지한다."""
        assert detect_repetition("다음다음다음다음") is True

    def test_정상_유사_패턴_미감지(self) -> None:
        """비슷하지만 동일하지 않은 패턴은 미감지."""
        assert detect_repetition("감사합니다 감사했습니다 감사드립니다") is False


class TestFilterHallucinations:
    """환각 필터링 함수 테스트."""

    def test_비활성_시_원본_반환(self) -> None:
        """필터 비활성 시 원본 세그먼트를 그대로 반환한다."""
        segments = [MockSegment("정상 텍스트")]
        config = MagicMock()
        config.hallucination_filter = MockFilterConfig(enabled=False)

        filtered, removed = filter_hallucinations(segments, config)
        assert len(filtered) == 1
        assert len(removed) == 0

    def test_config_없을_때_원본_반환(self) -> None:
        """hallucination_filter 설정이 없으면 원본을 반환한다."""
        segments = [MockSegment("정상 텍스트")]
        config = MagicMock(spec=[])  # hallucination_filter 속성 없음

        filtered, removed = filter_hallucinations(segments, config)
        assert len(filtered) == 1
        assert len(removed) == 0

    def test_높은_no_speech_prob_제거(self) -> None:
        """no_speech_prob이 높은 세그먼트를 제거한다."""
        segments = [
            MockSegment("정상", no_speech_prob=0.1),
            MockSegment("무음 구간", no_speech_prob=0.8),
        ]
        config = MagicMock()
        config.hallucination_filter = MockFilterConfig()

        filtered, removed = filter_hallucinations(segments, config)
        assert len(filtered) == 1
        assert len(removed) == 1
        assert "no_speech_prob" in removed[0]["reason"]

    def test_낮은_logprob_제거(self) -> None:
        """avg_logprob이 매우 낮은 세그먼트를 제거한다."""
        segments = [
            MockSegment("정상", avg_logprob=-0.5),
            MockSegment("저신뢰", avg_logprob=-1.5),
        ]
        config = MagicMock()
        config.hallucination_filter = MockFilterConfig()

        filtered, removed = filter_hallucinations(segments, config)
        assert len(filtered) == 1
        assert len(removed) == 1
        assert "avg_logprob" in removed[0]["reason"]

    def test_높은_compression_ratio_제거(self) -> None:
        """compression_ratio가 높은 세그먼트를 제거한다."""
        segments = [
            MockSegment("정상", compression_ratio=1.5),
            MockSegment("반복 반복 반복 반복", compression_ratio=3.0),
        ]
        config = MagicMock()
        config.hallucination_filter = MockFilterConfig()

        filtered, removed = filter_hallucinations(segments, config)
        assert len(filtered) == 1
        assert len(removed) == 1
        assert "compression_ratio" in removed[0]["reason"]

    def test_반복_패턴_제거(self) -> None:
        """반복 패턴이 감지된 세그먼트를 제거한다."""
        segments = [
            MockSegment("정상 텍스트입니다"),
            MockSegment("감사합니다 감사합니다 감사합니다"),
        ]
        config = MagicMock()
        config.hallucination_filter = MockFilterConfig()

        filtered, removed = filter_hallucinations(segments, config)
        assert len(filtered) == 1
        assert len(removed) == 1
        assert "repetition" in removed[0]["reason"]

    def test_정상_세그먼트_유지(self) -> None:
        """모든 조건을 통과하는 세그먼트는 유지한다."""
        segments = [
            MockSegment("오늘 회의 안건은 세 가지입니다", avg_logprob=-0.3, no_speech_prob=0.05),
            MockSegment("첫 번째는 매출 보고입니다", avg_logprob=-0.4, no_speech_prob=0.02),
        ]
        config = MagicMock()
        config.hallucination_filter = MockFilterConfig()

        filtered, removed = filter_hallucinations(segments, config)
        assert len(filtered) == 2
        assert len(removed) == 0

    def test_복합_필터링(self) -> None:
        """여러 종류의 환각이 동시에 필터링된다."""
        segments = [
            MockSegment("정상 텍스트"),
            MockSegment("무음", no_speech_prob=0.9),
            MockSegment("저신뢰", avg_logprob=-2.0),
            MockSegment("확확확확확"),
        ]
        config = MagicMock()
        config.hallucination_filter = MockFilterConfig()

        filtered, removed = filter_hallucinations(segments, config)
        assert len(filtered) == 1
        assert len(removed) == 3

    def test_빈_세그먼트_리스트(self) -> None:
        """빈 세그먼트 리스트 처리."""
        config = MagicMock()
        config.hallucination_filter = MockFilterConfig()

        filtered, removed = filter_hallucinations([], config)
        assert len(filtered) == 0
        assert len(removed) == 0
