"""
Wiki 인용 마커 파싱·검증 테스트 모듈 (TDD Red 단계)

목적: core/wiki/citations.py 의 CITATION_PATTERN, parse_citation,
  is_factual_statement, enforce_citations, WikiGuardError 인터페이스를
  TDD Red 단계로 검증한다. core/wiki/ 패키지가 아직 존재하지 않으므로
  모든 테스트는 ImportError 로 실패해야 한다.
주요 기능:
  - CITATION_PATTERN 정규식 valid/invalid 매칭 검증
  - parse_citation() 정상·오류 경로 검증
  - is_factual_statement() 면제/의무 구분 검증
  - enforce_citations() D1 후처리 알고리즘 검증 (제거·보존·30% 임계)
  - WikiGuardError reason 코드 검증
의존성: pytest (stdlib 외 금지), re (stdlib)
"""

from __future__ import annotations

import pytest

# ──────────────────────────────────────────────────────────────────────────────
# [TDD Red] core/wiki/ 패키지가 아직 없으므로 이 import 자체가 ImportError 를 일으킨다.
# 모든 테스트가 collection 오류 또는 ImportError 로 실패해야 한다.
# ──────────────────────────────────────────────────────────────────────────────
from core.wiki.citations import (  # noqa: E402
    CITATION_PATTERN,
    WikiGuardError,
    enforce_citations,
    is_factual_statement,
    parse_citation,
)

# ──────────────────────────────────────────────────────────────────────────────
# 상수: 인터페이스 계약에서 유래한 숫자를 하드코딩하지 않기 위한 명시적 정의
# PRD §6 D1: 거부율이 의무 대상의 30% 를 초과하면 WikiGuardError 발생
# ──────────────────────────────────────────────────────────────────────────────
D1_REJECTION_THRESHOLD = 0.30  # 30% 초과 시 WikiGuardError

# 테스트에서 일관되게 사용할 가짜 meeting_id (8자리 소문자 hex)
FAKE_MEETING_ID = "abc12345"


# ════════════════════════════════════════════════════════════════════
# 1. CITATION_PATTERN 정규식 테스트
# ════════════════════════════════════════════════════════════════════


class TestCitationPatternValid:
    """CITATION_PATTERN 이 유효한 인용 형식과 매칭되는지 검증한다."""

    @pytest.mark.parametrize(
        "valid_citation",
        [
            "[meeting:abc12345@00:23:45]",  # PRD §4.3 기본 예시
            "[meeting:00000000@00:00:00]",  # 최소값: 전부 0
            "[meeting:ffffffff@23:59:59]",  # 최대 hex 값 + 최대 시각
            "[meeting:a1b2c3d4@01:30:00]",  # 혼합 hex
            "결정 [meeting:abc12345@00:23:45] 입니다.",  # 문장 중간 삽입
            "[meeting:abc12345@00:23:45], [meeting:def67890@01:00:00]",  # 두 개 연속
        ],
    )
    def test_citation_pattern_유효_형식에_매칭된다(self, valid_citation: str) -> None:
        """CITATION_PATTERN 이 올바른 인용 형식 문자열과 매칭된다."""
        # Arrange & Act
        match = CITATION_PATTERN.search(valid_citation)
        # Assert
        assert match is not None, f"유효한 인용 '{valid_citation}' 에서 매칭이 실패함"


class TestCitationPatternInvalid:
    """CITATION_PATTERN 이 잘못된 형식을 거부하는지 검증한다."""

    @pytest.mark.parametrize(
        "invalid_citation, reason",
        [
            ("[meeting:abc@00:23:45]", "id 길이 3자리 — 8자리 미만"),
            ("[meeting:abc1234@00:23:45]", "id 길이 7자리 — 8자리 미만"),
            ("[meeting:ABC12345@00:23:45]", "대문자 hex — 거부 대상"),
            ("[meeting:ABC12345@00:23:45]", "대문자 포함 — 거부 대상"),
            ("[meeting:abc1234Z@00:23:45]", "Z는 hex 아님"),
            ("[meeting:abc12345@0:23:45]", "시 자릿수 1자리 — 2자리 필요"),
            ("[meeting:abc12345@00:2:45]", "분 자릿수 1자리 — 2자리 필요"),
            ("[meeting:abc12345@00:23:4]", "초 자릿수 1자리 — 2자리 필요"),
            ("[meeting:abc12345 00:23:45]", "@ 구분자 누락"),
            ("meeting:abc12345@00:23:45", "[ ] 브래킷 누락"),
            ("[meeting:abc12345@00:23:45", "닫힌 브래킷 누락"),
            ("", "빈 문자열"),
            ("아무 관련 없는 문장.", "인용 없는 일반 텍스트"),
        ],
    )
    def test_citation_pattern_잘못된_형식을_거부한다(
        self, invalid_citation: str, reason: str
    ) -> None:
        """CITATION_PATTERN 이 잘못된 형식 문자열과 매칭되지 않는다."""
        # Arrange & Act
        match = CITATION_PATTERN.search(invalid_citation)
        # Assert — 이유 설명 포함한 명확한 오류 메시지
        assert match is None, (
            f"잘못된 인용 '{invalid_citation}' 이 매칭되면 안 됨 (이유: {reason})"
        )


# ════════════════════════════════════════════════════════════════════
# 2. parse_citation() 테스트
# ════════════════════════════════════════════════════════════════════


class TestParseCitation:
    """parse_citation() 의 정상 파싱과 오류 처리를 검증한다."""

    def test_parse_citation_유효한_인용_마커를_튜플로_반환한다(self) -> None:
        """유효한 인용 마커를 (meeting_id, timestamp_str) 튜플로 반환한다."""
        # Arrange
        text = "결정 [meeting:abc12345@00:23:45] 참조."
        # Act
        result = parse_citation(text)
        # Assert
        assert result is not None, "유효한 인용에서 None 을 반환하면 안 됨"
        assert result == ("abc12345", "00:23:45"), (
            f"반환값이 ('abc12345', '00:23:45') 여야 하나 {result!r} 임"
        )

    def test_parse_citation_여러_인용이_있으면_첫_번째를_반환한다(self) -> None:
        """re.search 기반이므로 여러 인용 중 첫 번째만 반환한다."""
        # Arrange
        text = "[meeting:aaa00001@00:01:00] 그리고 [meeting:bbb00002@00:02:00]"
        # Act
        result = parse_citation(text)
        # Assert — 인터페이스 문서에 'first match' 명시
        assert result is not None
        assert result[0] == "aaa00001", (
            f"첫 번째 meeting_id 가 'aaa00001' 여야 하나 {result[0]!r} 임"
        )

    @pytest.mark.parametrize(
        "garbage_input",
        [
            "",  # 빈 문자열
            "일반 사실 진술 문장",  # 인용 없는 일반 텍스트
            "[meeting:ABC12345@00:23:45]",  # 대문자 hex — 거부
            "[meeting:abc@00:23:45]",  # id 길이 부족
            "   \t\n",  # 공백/탭/개행만 있음
        ],
    )
    def test_parse_citation_잘못된_입력에_None을_반환한다(self, garbage_input: str) -> None:
        """인용 마커가 없거나 형식이 잘못된 입력에 대해 None 을 반환한다."""
        # Arrange & Act
        result = parse_citation(garbage_input)
        # Assert
        assert result is None, f"'{garbage_input!r}' 에 대해 None 이어야 하나 {result!r} 반환됨"


# ════════════════════════════════════════════════════════════════════
# 3. is_factual_statement() 테스트
# ════════════════════════════════════════════════════════════════════


class TestIsFactualStatement:
    """is_factual_statement() 의 면제/의무 구분 로직을 검증한다."""

    # 면제 대상 — False 가 반환되어야 하는 줄
    @pytest.mark.parametrize(
        "exempt_line, reason",
        [
            ("", "빈 줄 — 면제"),
            ("   ", "공백만 있는 줄 — 면제"),
            ("# 결정 내용", "H1 제목 — 면제"),
            ("## 배경", "H2 제목 — 면제"),
            ("### 세부 항목", "H3 제목 — 면제"),
            ("---", "frontmatter 구분자 — 면제"),
            ("[../people/철수.md]", "순수 페이지 링크 — 면제"),
            ("[../../decisions/x.md]", "깊은 상대 페이지 링크 — 면제"),
            ("<!-- confidence: 9 -->", "HTML 주석(confidence 마커) — 면제"),
            ("|---|---|---|", "표 구분자 줄 — 면제"),
            ("```python", "코드블록 펜스 시작 — 면제"),
            ("```", "코드블록 펜스 종료 — 면제"),
        ],
    )
    def test_is_factual_statement_면제_줄은_False를_반환한다(
        self, exempt_line: str, reason: str
    ) -> None:
        """면제 대상 줄에 대해 False 를 반환한다."""
        # Arrange & Act
        result = is_factual_statement(exempt_line)
        # Assert
        assert result is False, (
            f"'{exempt_line!r}' 은 면제 대상이어야 하나 True 반환됨 (이유: {reason})"
        )

    # 인용 의무 대상 — True 가 반환되어야 하는 줄
    @pytest.mark.parametrize(
        "factual_line, reason",
        [
            ("5월 1일 출시를 결정했다.", "평문 사실 진술"),
            ("- 5월 1일 출시 결정", "리스트 항목 본문"),
            ("| 항목 | 값 |", "표 셀 본문"),
            (
                "결정 [meeting:abc12345@00:23:45].",
                "인용이 이미 있는 사실 진술 — D1 통과 대상",
            ),
            ("배포 일정이 변경되었다.", "한국어 일반 사실 진술"),
        ],
    )
    def test_is_factual_statement_의무_줄은_True를_반환한다(
        self, factual_line: str, reason: str
    ) -> None:
        """인용 의무 대상 줄에 대해 True 를 반환한다."""
        # Arrange & Act
        result = is_factual_statement(factual_line)
        # Assert
        assert result is True, (
            f"'{factual_line!r}' 은 의무 대상이어야 하나 False 반환됨 (이유: {reason})"
        )


# ════════════════════════════════════════════════════════════════════
# 4. enforce_citations() 테스트
# ════════════════════════════════════════════════════════════════════


class TestEnforceCitationsBasic:
    """enforce_citations() 의 핵심 동작(보존·제거·반환)을 검증한다."""

    def test_인용_있는_사실_문장은_보존된다(self) -> None:
        """인용 마커가 포함된 사실 진술 줄은 결과 content 에 그대로 남아야 한다."""
        # Arrange
        cited_line = f"출시일이 5월 1일로 결정됐다. [meeting:{FAKE_MEETING_ID}@00:10:00]"
        content = f"# 결정 내용\n\n{cited_line}\n"
        # Act
        result_content, rejected = enforce_citations(content, FAKE_MEETING_ID)
        # Assert
        assert cited_line in result_content, "인용이 있는 사실 진술 줄이 결과에서 사라짐"
        assert len(rejected) == 0, f"인용 있는 줄인데 거부 목록에 포함됨: {rejected}"

    def test_인용_없는_사실_문장은_제거되고_거부_목록에_기록된다(self) -> None:
        """인용 마커가 없는 사실 진술 줄이 제거되고 rejected 목록에 원문으로 기록된다."""
        # Arrange
        uncited_line = "팀 역량이 부족하다고 판단했다."
        content = f"# 배경\n\n{uncited_line}\n"
        # Act
        result_content, rejected = enforce_citations(content, FAKE_MEETING_ID)
        # Assert
        assert uncited_line not in result_content, (
            "인용 없는 사실 진술 줄이 결과 content 에 남아 있음"
        )
        assert uncited_line in rejected, f"거부된 줄이 rejected 목록에 없음: {rejected}"

    def test_메타_제목_링크는_인용_없어도_보존된다(self) -> None:
        """frontmatter 구분자, 제목, 페이지 링크는 인용 없어도 결과에 남아야 한다."""
        # Arrange
        content = "---\ntype: decision\n---\n\n# 결정 내용\n\n[../people/철수.md]\n"
        # Act
        result_content, rejected = enforce_citations(content, FAKE_MEETING_ID)
        # Assert — 면제 줄들이 모두 결과에 포함되어야 한다
        assert "---" in result_content, "frontmatter 구분자가 제거됨"
        assert "# 결정 내용" in result_content, "H1 제목이 제거됨"
        assert "[../people/철수.md]" in result_content, "페이지 링크가 제거됨"
        assert len(rejected) == 0, f"면제 줄이 거부 목록에 잘못 포함됨: {rejected}"

    def test_한_줄에_여러_인용이_있어도_사실_진술로_보존된다(self) -> None:
        """콤마로 구분된 여러 인용이 포함된 줄도 인용 있는 줄로 간주해 보존한다."""
        # Arrange
        multi_cited = (
            f"두 차례 논의 끝에 합의했다. "
            f"[meeting:{FAKE_MEETING_ID}@00:05:00], "
            f"[meeting:{FAKE_MEETING_ID}@00:10:00]"
        )
        content = f"## 결정\n\n{multi_cited}\n"
        # Act
        result_content, rejected = enforce_citations(content, FAKE_MEETING_ID)
        # Assert
        assert multi_cited in result_content, "여러 인용이 포함된 줄이 잘못 제거됨"
        assert len(rejected) == 0

    def test_빈_content에_대해_빈_content와_빈_rejected를_반환한다(self) -> None:
        """입력 content 가 빈 문자열이면 ('', []) 를 반환한다."""
        # Arrange & Act
        result_content, rejected = enforce_citations("", FAKE_MEETING_ID)
        # Assert
        assert result_content == "", f"빈 입력에서 result_content='{result_content!r}'"
        assert rejected == [], f"빈 입력에서 rejected={rejected!r}"


class TestEnforceCitationsThreshold:
    """enforce_citations() 의 30% 임계 초과 시 WikiGuardError 발생을 검증한다."""

    def test_거부율_30퍼센트_초과_시_WikiGuardError_발생한다(self) -> None:
        """의무 대상 10줄 중 4줄 거부(40%) → D1_REJECTION_THRESHOLD 초과 → WikiGuardError."""
        # Arrange: 인용 있는 6줄 + 인용 없는 4줄 = 의무 대상 10줄
        # 거부율 = 4/10 = 0.40 > D1_REJECTION_THRESHOLD(0.30)
        cited_lines = "\n".join(
            f"인용 있는 진술 {i}. [meeting:{FAKE_MEETING_ID}@00:0{i}:00]"
            for i in range(1, 7)  # 6줄
        )
        uncited_lines = "\n".join(
            f"인용 없는 진술 {i}."
            for i in range(1, 5)  # 4줄
        )
        content = f"{cited_lines}\n{uncited_lines}\n"
        # Act & Assert
        with pytest.raises(WikiGuardError) as exc_info:
            enforce_citations(content, FAKE_MEETING_ID)
        # reason 코드 검증 — 인터페이스 계약에 명시된 안정적 식별자
        assert exc_info.value.args[0] == "too_many_uncited_statements" or (
            hasattr(exc_info.value, "reason")
            and exc_info.value.reason == "too_many_uncited_statements"
        ), (
            f"WikiGuardError.reason 이 'too_many_uncited_statements' 여야 하나 "
            f"{exc_info.value!r} 임"
        )

    def test_거부율_정확히_30퍼센트는_WikiGuardError_발생하지_않는다(self) -> None:
        """의무 대상 10줄 중 3줄 거부(30%) — 초과가 아니므로 예외 없이 처리된다."""
        # Arrange: 인용 있는 7줄 + 인용 없는 3줄 = 의무 대상 10줄
        # 거부율 = 3/10 = 0.30 — "초과"가 아닌 "정확히" 30%이므로 예외 없어야 함
        cited_lines = "\n".join(
            f"인용 있는 진술 {i}. [meeting:{FAKE_MEETING_ID}@00:0{i}:00]"
            for i in range(1, 8)  # 7줄
        )
        uncited_lines = "\n".join(
            f"인용 없는 진술 {i}."
            for i in range(1, 4)  # 3줄
        )
        content = f"{cited_lines}\n{uncited_lines}\n"
        # Act — 예외가 발생하지 않아야 한다 (30% 초과가 아님)
        result_content, rejected = enforce_citations(content, FAKE_MEETING_ID)
        # Assert
        assert len(rejected) == 3, f"거부된 줄이 3개여야 하나 {len(rejected)}개 임"


class TestEnforceCitationsFrontmatterAndCodeblock:
    """enforce_citations() 의 frontmatter/코드블록 스킵 동작을 검증한다."""

    def test_frontmatter_내부_줄은_사실_진술로_취급하지_않는다(self) -> None:
        """--- 로 둘러싸인 frontmatter 내부의 key: value 줄은 제거하지 않는다."""
        # Arrange
        content = "---\ntype: decision\ndate: 2026-04-28\nconfidence: 9\n---\n\n# 결정 내용\n"
        # Act
        result_content, rejected = enforce_citations(content, FAKE_MEETING_ID)
        # Assert — frontmatter 내 키값 줄이 rejected 에 들어가면 안 됨
        assert "type: decision" in result_content, "frontmatter type 줄이 제거됨"
        assert "confidence: 9" in result_content, "frontmatter confidence 줄이 제거됨"
        assert len(rejected) == 0, f"frontmatter 줄이 거부 목록에 포함됨: {rejected}"

    def test_코드블록_내부_줄은_사실_진술로_취급하지_않는다(self) -> None:
        """``` 로 둘러싸인 코드블록 내부의 줄은 인용 의무 대상에서 제외된다."""
        # Arrange — 코드블록 내 줄에는 인용 없음
        content = "# 예제\n\n```python\nx = 인용없는코드줄()\nreturn x\n```\n"
        # Act
        result_content, rejected = enforce_citations(content, FAKE_MEETING_ID)
        # Assert — 코드 줄이 제거되거나 거부되면 안 됨
        assert "x = 인용없는코드줄()" in result_content, "코드블록 내 줄이 제거됨"
        assert len(rejected) == 0, f"코드블록 내 줄이 거부 목록에 포함됨: {rejected}"


class TestEnforceCitationsEdgeCases:
    """enforce_citations() 의 경계 케이스와 다국어 입력을 검증한다."""

    def test_한자와_이모지가_포함된_사실_진술도_인용_없으면_제거된다(self) -> None:
        """한자·이모지 포함 사실 진술에도 D1 규칙이 동일하게 적용된다."""
        # Arrange
        # 한국어 + 한자 + 이모지 혼합 줄 — 인용 없음
        cjk_uncited = "結論：출시를 📅 5月 1日로 확정한다."
        content = f"# 국제화\n\n{cjk_uncited}\n"
        # Act
        result_content, rejected = enforce_citations(content, FAKE_MEETING_ID)
        # Assert
        assert cjk_uncited not in result_content, "CJK/이모지 포함 인용 없는 줄이 제거되지 않음"
        assert cjk_uncited in rejected, "CJK/이모지 포함 거부 줄이 rejected 목록에 없음"

    def test_멀티라인_입력_혼합_케이스에서_올바르게_처리된다(self) -> None:
        """제목·메타·인용 있는 줄·인용 없는 줄이 혼합된 실제 페이지 형태를 처리한다."""
        # Arrange
        content = (
            "---\n"
            "type: decision\n"
            "---\n"
            "\n"
            "# 결정 내용\n"
            "\n"
            f"배포 계획을 수립했다. [meeting:{FAKE_MEETING_ID}@00:15:00]\n"
            "\n"
            "인용이 없는 주장이다.\n"
            "\n"
            "## 배경\n"
            "\n"
            f"이전 논의 결과이다. [meeting:{FAKE_MEETING_ID}@00:05:00]\n"
        )
        # Act
        result_content, rejected = enforce_citations(content, FAKE_MEETING_ID)
        # Assert — 인용 있는 두 줄은 보존되어야 한다
        assert f"배포 계획을 수립했다. [meeting:{FAKE_MEETING_ID}@00:15:00]" in result_content
        assert f"이전 논의 결과이다. [meeting:{FAKE_MEETING_ID}@00:05:00]" in result_content
        # 인용 없는 줄은 거부되어야 한다
        assert "인용이 없는 주장이다." in rejected

    def test_의무_대상_줄이_전혀_없는_경우_빈_rejected를_반환한다(self) -> None:
        """제목·메타만 있는 content 는 의무 줄 0개이므로 rejected 도 비어야 한다."""
        # Arrange
        content = "---\ntype: health\n---\n\n# 상태 보고\n\n## 요약\n"
        # Act
        result_content, rejected = enforce_citations(content, FAKE_MEETING_ID)
        # Assert
        assert rejected == [], f"의무 대상 없는 content 에서 rejected={rejected!r} 임"


# ════════════════════════════════════════════════════════════════════
# 5. WikiGuardError 테스트
# ════════════════════════════════════════════════════════════════════


class TestWikiGuardError:
    """WikiGuardError 의 생성과 reason 코드 접근을 검증한다."""

    def test_WikiGuardError_reason_코드가_보존된다(self) -> None:
        """WikiGuardError 생성 시 전달한 reason 이 예외 객체에서 접근 가능하다."""
        # Arrange & Act
        error = WikiGuardError("too_many_uncited_statements", "40% 거부됨")
        # Assert — reason 은 args[0] 또는 전용 속성으로 접근 가능해야 한다
        reason_accessible = (
            len(error.args) > 0 and error.args[0] == "too_many_uncited_statements"
        ) or (hasattr(error, "reason") and error.reason == "too_many_uncited_statements")
        assert reason_accessible, f"reason 코드에 접근할 수 없음: args={error.args!r}"

    def test_WikiGuardError는_Exception의_하위_클래스이다(self) -> None:
        """WikiGuardError 가 Exception 을 상속해야 일반 except 에서 잡힌다."""
        # Arrange & Act & Assert
        assert issubclass(WikiGuardError, Exception), (
            "WikiGuardError 가 Exception 을 상속하지 않음"
        )

    @pytest.mark.parametrize(
        "reason",
        [
            "too_many_uncited_statements",
            "phantom_citation",
            "low_confidence",
            "malformed_confidence",
            "decide_pages_failed",
        ],
    )
    def test_WikiGuardError_모든_예약_reason_코드로_생성_가능하다(self, reason: str) -> None:
        """인터페이스에 문서화된 5개 reason 코드 각각으로 예외를 생성할 수 있다."""
        # Arrange & Act
        error = WikiGuardError(reason)
        # Assert — 예외 생성 자체가 실패하면 안 됨
        assert error is not None
