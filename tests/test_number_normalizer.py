"""
한국어 숫자 정규화 모듈 테스트.

목적: steps/number_normalizer.py의 기능을 검증한다.
테스트 구분:
    - 정상 변환: 한글 숫자 → 아라비아 숫자 변환 정확성
    - 변환 금지: 고유명사/일반 단어 오변환 방지
    - 경계 케이스: 빈 문자열, level 설정, NFC 등

의존성: pytest, steps/number_normalizer
"""

import unicodedata

import pytest

from steps.number_normalizer import (
    _korean_number_to_int,
    _normalize_mixed,
    normalize_numbers,
)


# ============================================================
# 내부 함수 테스트: _korean_number_to_int
# ============================================================


class TestKoreanNumberToInt:
    """한글 숫자 → 정수 변환 함수 테스트."""

    def test_단일_숫자_일(self) -> None:
        """'일' → 1 변환을 검증한다."""
        assert _korean_number_to_int("일") == 1

    def test_단일_숫자_구(self) -> None:
        """'구' → 9 변환을 검증한다."""
        assert _korean_number_to_int("구") == 9

    def test_십(self) -> None:
        """'십' → 10 변환을 검증한다."""
        assert _korean_number_to_int("십") == 10

    def test_십오(self) -> None:
        """'십오' → 15 변환을 검증한다."""
        assert _korean_number_to_int("십오") == 15

    def test_삼십(self) -> None:
        """'삼십' → 30 변환을 검증한다."""
        assert _korean_number_to_int("삼십") == 30

    def test_이백오십(self) -> None:
        """'이백오십' → 250 변환을 검증한다."""
        assert _korean_number_to_int("이백오십") == 250

    def test_천(self) -> None:
        """'천' → 1000 변환을 검증한다."""
        assert _korean_number_to_int("천") == 1000

    def test_이천이십육(self) -> None:
        """'이천이십육' → 2026 변환을 검증한다."""
        assert _korean_number_to_int("이천이십육") == 2026

    def test_오백만(self) -> None:
        """'오백만' → 5000000 변환을 검증한다."""
        assert _korean_number_to_int("오백만") == 5_000_000

    def test_삼억(self) -> None:
        """'삼억' → 300000000 변환을 검증한다."""
        assert _korean_number_to_int("삼억") == 300_000_000

    def test_빈_문자열(self) -> None:
        """빈 문자열은 None을 반환한다."""
        assert _korean_number_to_int("") is None

    def test_비숫자_문자(self) -> None:
        """숫자가 아닌 한글은 None을 반환한다."""
        assert _korean_number_to_int("안녕") is None

    def test_만(self) -> None:
        """'만' 단독 → 10000 변환을 검증한다."""
        assert _korean_number_to_int("만") == 10_000


# ============================================================
# 정상 변환 테스트
# ============================================================


class TestNormalConversion:
    """한글 숫자 + 단위어 정상 변환 테스트."""

    def test_삼십_퍼센트(self) -> None:
        """'삼십 퍼센트' → '30 퍼센트' 변환을 검증한다."""
        assert normalize_numbers("삼십 퍼센트") == "30 퍼센트"

    def test_이백오십_명(self) -> None:
        """'이백오십 명' → '250 명' 변환을 검증한다."""
        assert normalize_numbers("이백오십 명") == "250 명"

    def test_이천이십육_년(self) -> None:
        """'이천이십육 년' → '2026 년' 변환을 검증한다."""
        assert normalize_numbers("이천이십육 년") == "2026 년"

    def test_오백만_원(self) -> None:
        """'오백만 원' → 숫자로 변환되는지 검증한다."""
        result = normalize_numbers("오백만 원")
        # "오백만" → 5000000
        assert "5000000" in result

    def test_혼합형_3십(self) -> None:
        """'3십 퍼센트' → '30 퍼센트' 혼합형 변환을 검증한다."""
        assert normalize_numbers("3십 퍼센트") == "30 퍼센트"

    def test_이미_아라비아_유지(self) -> None:
        """이미 아라비아 숫자인 경우 그대로 유지한다."""
        assert normalize_numbers("30 퍼센트") == "30 퍼센트"

    def test_복합문장(self) -> None:
        """한 문장에 여러 숫자가 있을 때 모두 변환한다."""
        result = normalize_numbers("매출 삼십 퍼센트 성장, 직원 이백 명")
        assert "30" in result
        assert "200" in result

    def test_십오_일까지(self) -> None:
        """'십오 일까지' → '15 일까지' 변환을 검증한다."""
        result = normalize_numbers("십오 일까지")
        assert "15" in result

    def test_삼_개월(self) -> None:
        """'삼 개월' → '3 개월' 변환을 검증한다."""
        assert normalize_numbers("삼 개월") == "3 개월"

    def test_오십_건(self) -> None:
        """'오십 건' → '50 건' 변환을 검증한다."""
        assert normalize_numbers("오십 건") == "50 건"


# ============================================================
# 변환 금지 테스트 (고유명사/일반 단어 보호)
# ============================================================


class TestBrandProtection:
    """고유명사 및 일반 단어 오변환 방지 테스트."""

    def test_삼성전자(self) -> None:
        """'삼성전자'는 변환하지 않는다."""
        assert normalize_numbers("삼성전자 주가") == "삼성전자 주가"

    def test_이_프로젝트(self) -> None:
        """관사 '이'는 변환하지 않는다. (단위어 없음)"""
        assert normalize_numbers("이 프로젝트에서") == "이 프로젝트에서"

    def test_사과(self) -> None:
        """'사과'는 변환하지 않는다."""
        assert normalize_numbers("사과를 먹었다") == "사과를 먹었다"

    def test_이마트(self) -> None:
        """'이마트'는 변환하지 않는다."""
        assert normalize_numbers("이마트에서") == "이마트에서"

    def test_오뚜기(self) -> None:
        """'오뚜기'는 변환하지 않는다."""
        assert normalize_numbers("오뚜기 라면") == "오뚜기 라면"

    def test_이번_달(self) -> None:
        """'이번'은 변환하지 않는다."""
        assert normalize_numbers("이번 달에") == "이번 달에"

    def test_일단(self) -> None:
        """'일단'은 변환하지 않는다."""
        assert normalize_numbers("일단 시작하자") == "일단 시작하자"

    def test_일을_마쳤습니다(self) -> None:
        """'일을'은 단위어가 없으므로 변환하지 않는다."""
        assert normalize_numbers("일을 마쳤습니다") == "일을 마쳤습니다"

    def test_구조_개선(self) -> None:
        """'구조'는 변환하지 않는다."""
        assert normalize_numbers("구조 개선이 필요합니다") == "구조 개선이 필요합니다"

    def test_사업_계획(self) -> None:
        """'사업'은 변환하지 않는다."""
        assert normalize_numbers("사업 계획을 세우자") == "사업 계획을 세우자"


# ============================================================
# 경계 케이스 테스트
# ============================================================


class TestEdgeCases:
    """경계 케이스 테스트."""

    def test_빈_문자열(self) -> None:
        """빈 문자열은 그대로 반환한다."""
        assert normalize_numbers("") == ""

    def test_숫자_없는_문장(self) -> None:
        """숫자가 없는 일반 문장은 그대로 반환한다."""
        text = "오늘 회의를 시작하겠습니다"
        assert normalize_numbers(text) == text

    def test_level_0_비활성화(self) -> None:
        """level=0이면 변환을 수행하지 않는다."""
        assert normalize_numbers("삼십 퍼센트", level=0) == "삼십 퍼센트"

    def test_NFC_정규화_유지(self) -> None:
        """결과가 NFC 정규화 상태를 유지하는지 검증한다."""
        text = "삼십 퍼센트"
        result = normalize_numbers(text)
        assert result == unicodedata.normalize("NFC", result)

    def test_level_2_추가_단위(self) -> None:
        """level=2에서 추가 단위어(분, 초 등)가 동작하는지 검증한다."""
        result = normalize_numbers("삼십 분", level=2)
        assert "30" in result

    def test_level_1에서_분_미변환(self) -> None:
        """level=1에서 '분'은 변환하지 않는다 (시간 단위 충돌 방지)."""
        result = normalize_numbers("삼십 분", level=1)
        # level=1에서는 '분'이 안전 단위어에 포함되지 않으므로 변환 안 됨
        assert "삼십" in result

    def test_고유명사_뒤_숫자_변환(self) -> None:
        """고유명사 뒤에 오는 숫자+단위어는 정상 변환한다."""
        result = normalize_numbers("삼성전자 매출 삼십 퍼센트 성장")
        assert "삼성전자" in result
        assert "30" in result

    def test_혼합형_2백(self) -> None:
        """'2백' → '200' 혼합형 변환을 검증한다."""
        result = normalize_numbers("2백 명")
        assert "200" in result


# ============================================================
# 혼합형 내부 함수 테스트
# ============================================================


class TestNormalizeMixed:
    """혼합형 숫자 변환 내부 함수 테스트."""

    def test_3십(self) -> None:
        """'3십' → '30' 변환을 검증한다."""
        assert _normalize_mixed("3십") == "30"

    def test_2백(self) -> None:
        """'2백' → '200' 변환을 검증한다."""
        assert _normalize_mixed("2백") == "200"

    def test_5천(self) -> None:
        """'5천' → '5000' 변환을 검증한다."""
        assert _normalize_mixed("5천") == "5000"

    def test_15만(self) -> None:
        """'15만' → '150000' 변환을 검증한다."""
        assert _normalize_mixed("15만") == "150000"

    def test_일반_텍스트_유지(self) -> None:
        """혼합형이 없는 텍스트는 그대로 반환한다."""
        assert _normalize_mixed("안녕하세요") == "안녕하세요"
