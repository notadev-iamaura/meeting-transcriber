"""텍스트 후처리 모듈 테스트.

공백 정규화, 줄바꿈 정리, NFC 정규화 등을 검증한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

from steps.text_postprocessor import postprocess_segments, postprocess_text


@dataclass
class MockSegment:
    """테스트용 전사 세그먼트 모의 객체."""

    text: str
    start: float = 0.0
    end: float = 1.0


@dataclass
class MockPostprocessConfig:
    """테스트용 후처리 설정 모의 객체."""

    enabled: bool = True


class TestPostprocessText:
    """텍스트 후처리 함수 테스트."""

    def test_연속_공백_단일_공백(self) -> None:
        """연속 공백이 단일 공백으로 변환된다."""
        assert postprocess_text("안녕  하세요") == "안녕 하세요"

    def test_줄바꿈_제거(self) -> None:
        """줄바꿈 문자가 공백으로 변환된다."""
        assert postprocess_text("안녕\n하세요") == "안녕 하세요"

    def test_탭_제거(self) -> None:
        """탭 문자가 공백으로 변환된다."""
        assert postprocess_text("안녕\t하세요") == "안녕 하세요"

    def test_앞뒤_공백_제거(self) -> None:
        """앞뒤 공백이 제거된다."""
        assert postprocess_text("  안녕하세요  ") == "안녕하세요"

    def test_복합_정리(self) -> None:
        """여러 문제가 동시에 정리된다."""
        assert postprocess_text("  안녕  \n  하세요  ") == "안녕 하세요"

    def test_빈_문자열(self) -> None:
        """빈 문자열은 그대로 반환한다."""
        assert postprocess_text("") == ""

    def test_NFC_정규화(self) -> None:
        """NFD 인코딩된 한글이 NFC로 정규화된다."""
        # NFD: ㅎ+ㅏ+ㄴ (분리형)
        nfd_text = "\u1112\u1161\u11ab"  # '한' NFD
        result = postprocess_text(nfd_text)
        assert result == "한"

    def test_정상_텍스트_변경_없음(self) -> None:
        """이미 정상인 텍스트는 변경 없이 반환한다."""
        text = "안녕하세요 오늘 회의를 시작하겠습니다"
        assert postprocess_text(text) == text


class TestPostprocessSegments:
    """세그먼트 후처리 함수 테스트."""

    def test_비활성_시_원본_반환(self) -> None:
        """후처리 비활성 시 원본 세그먼트를 그대로 반환한다."""
        segments = [MockSegment("  공백 있음  ")]
        config = MagicMock()
        config.text_postprocessing = MockPostprocessConfig(enabled=False)

        result = postprocess_segments(segments, config)
        assert len(result) == 1
        assert result[0].text == "  공백 있음  "

    def test_config_없을_때_원본_반환(self) -> None:
        """text_postprocessing 설정이 없으면 원본을 반환한다."""
        segments = [MockSegment("  공백 있음  ")]
        config = MagicMock(spec=[])

        result = postprocess_segments(segments, config)
        assert len(result) == 1

    def test_공백_정리(self) -> None:
        """세그먼트 텍스트의 공백이 정리된다."""
        segments = [MockSegment("  안녕  하세요  ")]
        config = MagicMock()
        config.text_postprocessing = MockPostprocessConfig()

        result = postprocess_segments(segments, config)
        assert len(result) == 1
        assert result[0].text == "안녕 하세요"

    def test_빈_세그먼트_제거(self) -> None:
        """후처리 후 텍스트가 비어있는 세그먼트를 제거한다."""
        segments = [
            MockSegment("정상 텍스트"),
            MockSegment("   "),  # 공백만
        ]
        config = MagicMock()
        config.text_postprocessing = MockPostprocessConfig()

        result = postprocess_segments(segments, config)
        assert len(result) == 1
        assert result[0].text == "정상 텍스트"

    def test_빈_리스트(self) -> None:
        """빈 세그먼트 리스트 처리."""
        config = MagicMock()
        config.text_postprocessing = MockPostprocessConfig()

        result = postprocess_segments([], config)
        assert len(result) == 0
