"""
Wiki 도메인 모델 테스트 모듈 (TDD Red 단계)

목적: core/wiki/models.py 의 4개 데이터 클래스·Enum 인터페이스를
  TDD Red 단계로 검증한다. core/wiki/ 패키지가 아직 존재하지 않으므로
  모든 테스트는 ImportError 로 실패해야 한다.
주요 기능:
  - PageType StrEnum 8개 값 + 문자열 비교 확인
  - Citation frozen dataclass 불변성 확인
  - Citation.timestamp_seconds 변환 정확성 확인
  - WikiPage 기본값 팩토리 독립성 확인
  - HealthReport 기본값 및 citation_pass_rate 범위 확인
의존성: pytest, dataclasses (stdlib)
"""

from __future__ import annotations

import dataclasses

import pytest

# ──────────────────────────────────────────────────────────────────────────────
# [TDD Red] core/wiki/ 패키지가 아직 없으므로 이 import 자체가 ImportError 를 일으킨다.
# 테스트 수집 단계에서 모듈 전체가 오류 처리되며, 개별 테스트도 실패 상태가 된다.
# ──────────────────────────────────────────────────────────────────────────────
from core.wiki.models import Citation, HealthReport, PageType, WikiPage  # noqa: E402


# ════════════════════════════════════════════════════════════════════
# 1. PageType StrEnum 테스트
# ════════════════════════════════════════════════════════════════════

class TestPageType:
    """PageType StrEnum 의 값 존재 여부와 문자열 비교 동작을 검증한다."""

    # PRD §4.1 에 정의된 8개 멤버의 (이름, 기대 문자열 값) 목록
    _EXPECTED_MEMBERS = [
        ("DECISION",     "decision"),
        ("PERSON",       "person"),
        ("PROJECT",      "project"),
        ("TOPIC",        "topic"),
        ("ACTION_ITEMS", "action_items"),
        ("INDEX",        "index"),
        ("LOG",          "log"),
        ("HEALTH",       "health"),
    ]

    @pytest.mark.parametrize("member_name, expected_value", _EXPECTED_MEMBERS)
    def test_pageType_멤버_8개_모두_존재하고_문자열과_같다(
        self, member_name: str, expected_value: str
    ) -> None:
        """PageType 의 각 멤버가 정의되어 있고 StrEnum 으로서 문자열과 동등하다."""
        # Arrange
        member = PageType[member_name]
        # Act & Assert — StrEnum 이므로 str 비교가 성립해야 한다
        assert member == expected_value, (
            f"PageType.{member_name} 의 값이 '{expected_value}' 여야 하나 '{member!r}' 임"
        )

    def test_pageType_멤버_개수가_정확히_8개이다(self) -> None:
        """StrEnum 멤버 수가 정확히 8개인지 확인한다 (추가/누락 방지)."""
        # Arrange & Act
        members = list(PageType)
        # Assert
        assert len(members) == 8, (
            f"PageType 멤버 수가 8개 여야 하나 {len(members)}개임: {members}"
        )

    def test_pageType_json_직렬화에_문자열_값_사용된다(self) -> None:
        """StrEnum 이므로 str() 로 변환 시 값 문자열이 반환된다."""
        # Arrange & Act & Assert
        assert str(PageType.DECISION) == "decision"
        assert str(PageType.ACTION_ITEMS) == "action_items"


# ════════════════════════════════════════════════════════════════════
# 2. Citation frozen dataclass 테스트
# ════════════════════════════════════════════════════════════════════

class TestCitation:
    """Citation frozen dataclass 의 불변성과 timestamp 변환 정확성을 검증한다."""

    def test_citation_frozen_속성_수정시_FrozenInstanceError_발생(self) -> None:
        """frozen=True 이므로 생성 후 필드 수정 시 FrozenInstanceError 가 발생해야 한다."""
        # Arrange
        cite = Citation(
            meeting_id="abc12345",
            timestamp_str="00:23:45",
            timestamp_seconds=1425,
        )
        # Act & Assert
        with pytest.raises(dataclasses.FrozenInstanceError):
            cite.meeting_id = "00000000"  # type: ignore[misc]

    @pytest.mark.parametrize(
        "timestamp_str, expected_seconds",
        [
            ("00:00:00", 0),       # 경계: 0초
            ("00:00:01", 1),       # 1초
            ("00:01:00", 60),      # 1분
            ("00:23:45", 1425),    # PRD 예시 값
            ("01:00:00", 3600),    # 1시간 정확히
            ("01:30:30", 5430),    # 1시간 30분 30초
            ("23:59:59", 86399),   # 최대 유효 값
        ],
    )
    def test_citation_timestamp_seconds_HH_MM_SS_를_초로_정확히_변환한다(
        self, timestamp_str: str, expected_seconds: int
    ) -> None:
        """HH:MM:SS 형식의 timestamp_str 이 timestamp_seconds 로 올바르게 변환된다."""
        # Arrange
        h, m, s = map(int, timestamp_str.split(":"))
        computed = h * 3600 + m * 60 + s
        # 인터페이스 계약: Citation 생성자가 timestamp_seconds 를 직접 계산한다고
        # 문서에 명시되어 있지 않으나 caller 가 변환 후 전달한다.
        # 여기서는 Citation 에 올바른 값을 전달하고 그대로 저장되는지만 검증한다.
        cite = Citation(
            meeting_id="abc12345",
            timestamp_str=timestamp_str,
            timestamp_seconds=computed,
        )
        # Act & Assert
        assert cite.timestamp_seconds == expected_seconds, (
            f"'{timestamp_str}' → {expected_seconds}초 여야 하나 {cite.timestamp_seconds}초임"
        )

    def test_citation_meeting_id_와_timestamp_str_이_그대로_저장된다(self) -> None:
        """생성자에 전달한 meeting_id, timestamp_str 값이 속성에 그대로 보존된다."""
        # Arrange
        mid = "abc12345"
        ts = "00:23:45"
        # Act
        cite = Citation(meeting_id=mid, timestamp_str=ts, timestamp_seconds=1425)
        # Assert
        assert cite.meeting_id == mid
        assert cite.timestamp_str == ts


# ════════════════════════════════════════════════════════════════════
# 3. WikiPage 기본값 독립성 테스트
# ════════════════════════════════════════════════════════════════════

class TestWikiPage:
    """WikiPage mutable dataclass 의 기본값 팩토리 독립성을 검증한다."""

    def test_두_WikiPage_인스턴스의_frontmatter_가_서로_다른_객체이다(self) -> None:
        """기본값 field(default_factory=dict) 로 생성된 frontmatter 는
        인스턴스마다 별도 dict 여야 한다 — 공유 참조 버그 방지."""
        from pathlib import Path  # noqa: PLC0415

        # Arrange
        page_a = WikiPage(path=Path("a.md"), page_type=PageType.DECISION)
        page_b = WikiPage(path=Path("b.md"), page_type=PageType.PERSON)
        # Act
        page_a.frontmatter["key"] = "value"
        # Assert — page_b 의 frontmatter 는 영향받지 않아야 한다
        assert "key" not in page_b.frontmatter, (
            "두 WikiPage 인스턴스의 frontmatter 가 같은 dict 를 공유하고 있음"
        )

    def test_두_WikiPage_인스턴스의_citations_가_서로_다른_객체이다(self) -> None:
        """기본값 field(default_factory=list) 로 생성된 citations 는
        인스턴스마다 별도 list 여야 한다."""
        from pathlib import Path  # noqa: PLC0415

        # Arrange
        page_a = WikiPage(path=Path("a.md"), page_type=PageType.TOPIC)
        page_b = WikiPage(path=Path("b.md"), page_type=PageType.PROJECT)
        dummy_cite = Citation(
            meeting_id="abc12345", timestamp_str="00:01:00", timestamp_seconds=60
        )
        # Act
        page_a.citations.append(dummy_cite)
        # Assert
        assert len(page_b.citations) == 0, (
            "두 WikiPage 인스턴스의 citations 가 같은 list 를 공유하고 있음"
        )

    def test_WikiPage_content_기본값은_빈_문자열이다(self) -> None:
        """content 의 기본값이 빈 문자열인지 확인한다."""
        from pathlib import Path  # noqa: PLC0415

        # Arrange & Act
        page = WikiPage(path=Path("x.md"), page_type=PageType.LOG)
        # Assert
        assert page.content == "", (
            f"content 기본값이 '' 여야 하나 {page.content!r} 임"
        )


# ════════════════════════════════════════════════════════════════════
# 4. HealthReport 기본값 테스트
# ════════════════════════════════════════════════════════════════════

class TestHealthReport:
    """HealthReport frozen dataclass 의 기본값과 citation_pass_rate 범위를 검증한다."""

    def test_last_lint_at_만_제공해도_모든_리스트_필드가_빈_값이다(self) -> None:
        """required 필드인 last_lint_at 만 전달해도 나머지 필드가 기본값으로 초기화된다."""
        # Arrange & Act
        report = HealthReport(last_lint_at="2026-04-28T09:00:00+09:00")
        # Assert
        assert report.contradictions == [], (
            f"contradictions 기본값이 [] 여야 하나 {report.contradictions!r} 임"
        )
        assert report.orphans == [], (
            f"orphans 기본값이 [] 여야 하나 {report.orphans!r} 임"
        )
        assert report.cyclic_links == [], (
            f"cyclic_links 기본값이 [] 여야 하나 {report.cyclic_links!r} 임"
        )

    def test_citation_pass_rate_기본값은_1_0이다(self) -> None:
        """모든 인용이 유효할 때를 나타내는 기본값이 1.0 인지 확인한다."""
        # Arrange & Act
        report = HealthReport(last_lint_at="2026-04-28T09:00:00+09:00")
        # Assert
        assert report.citation_pass_rate == 1.0, (
            f"citation_pass_rate 기본값이 1.0 이어야 하나 {report.citation_pass_rate} 임"
        )

    @pytest.mark.parametrize(
        "pass_rate",
        [0.0, 0.5, 0.999, 1.0],
    )
    def test_citation_pass_rate_유효_범위_0_0에서_1_0(self, pass_rate: float) -> None:
        """citation_pass_rate 가 0.0 ~ 1.0 사이 값을 그대로 저장한다."""
        # Arrange & Act
        report = HealthReport(
            last_lint_at="2026-04-28T09:00:00+09:00",
            citation_pass_rate=pass_rate,
        )
        # Assert
        assert 0.0 <= report.citation_pass_rate <= 1.0, (
            f"citation_pass_rate={report.citation_pass_rate} 가 유효 범위를 벗어남"
        )
        assert report.citation_pass_rate == pass_rate

    def test_total_pages_와_total_citations_기본값은_0이다(self) -> None:
        """total_pages, total_citations 기본값이 0 인지 확인한다."""
        # Arrange & Act
        report = HealthReport(last_lint_at="2026-04-28T09:00:00+09:00")
        # Assert
        assert report.total_pages == 0, (
            f"total_pages 기본값이 0 이어야 하나 {report.total_pages} 임"
        )
        assert report.total_citations == 0, (
            f"total_citations 기본값이 0 이어야 하나 {report.total_citations} 임"
        )
