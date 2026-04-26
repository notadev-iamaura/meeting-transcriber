"""
한국어 숫자 정규화 모듈.

목적: STT 결과에서 한글 숫자(삼십, 이백오십 등)를 아라비아 숫자(30, 250)로 변환한다.
     화이트리스트 기반 보수적 접근법으로 고유명사 오변환을 방지한다.

주요 기능:
    - Level 1 (보수적): 한글숫자 + 안전한 단위어 조합만 변환
    - Level 2 (중간): 복합 숫자 + 추가 단위어 포함
    - 고유명사 보호 (삼성, 이마트 등)
    - 혼합형 처리 (3십 → 30)

의존성: 없음 (순수 Python + regex)
"""

from __future__ import annotations

import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

# ============================================================
# 한글 숫자 매핑 테이블
# ============================================================

# 기본 숫자 (일~구) — 한자식
_DIGITS: dict[str, int] = {
    "일": 1,
    "이": 2,
    "삼": 3,
    "사": 4,
    "오": 5,
    "육": 6,
    "칠": 7,
    "팔": 8,
    "구": 9,
}

# 고유어(순한국어) 숫자. STT 출력 빈도 높음.
# 한정사 형태("한 명", "두 개", "세 시간", "네 분")는 동음이의 위험이 있으므로
# 반드시 단위어가 동반될 때만 변환되도록 _UNIT_PATTERN 에 결합한다.
_NATIVE_DIGITS: dict[str, int] = {
    # 1~10
    "하나": 1,
    "한": 1,  # 한정사: 한 명, 한 개
    "둘": 2,
    "두": 2,  # 한정사: 두 개, 두 명
    "셋": 3,
    "세": 3,  # 한정사: 세 개
    "넷": 4,
    "네": 4,  # 한정사: 네 시간 — 단, "네"는 응답 부사("네")와 충돌 가능 → 단위어 동반 필수
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
    "열": 10,
    # 10의 자리 (스물~아흔)
    "스물": 20,
    "스무": 20,  # 한정사: 스무 명
    "서른": 30,
    "마흔": 40,
    "쉰": 50,
    "예순": 60,
    "일흔": 70,
    "여든": 80,
    "아흔": 90,
}

# 자리수 단위 (십, 백, 천)
_POSITIONS: dict[str, int] = {
    "십": 10,
    "백": 100,
    "천": 1000,
}

# 큰 단위 (만, 억, 조)
_LARGE_UNITS: dict[str, int] = {
    "만": 10_000,
    "억": 100_000_000,
    "조": 1_000_000_000_000,
}

# ============================================================
# 안전한 단위어 (화이트리스트)
# ============================================================

# Level 1: 가장 보수적 — 오변환 위험이 극히 낮은 단위어
_SAFE_UNITS_L1: set[str] = {
    "퍼센트",
    "%",
    "프로",  # 비율
    "년",
    "월",
    "분기",  # 시간 (큰 단위)
    "개",
    "개월",
    "건",  # 수량
    "명",
    "인",  # 인원 ("분"은 시간 단위와 충돌 가능)
    "원",  # 금액
    "번",
    "차",
    "회",
    "호",
    "층",  # 순서/건물
    "배",  # 배수
    "대",  # 연령대/대수
    "일",  # 날짜의 "일" (숫자 뒤에 올 때)
}

# Level 2: 중간 — 추가 단위어 포함
_SAFE_UNITS_L2: set[str] = _SAFE_UNITS_L1 | {
    "분",  # 시간(분)
    "초",  # 시간(초)
    "시간",
    "시",  # 시간
    "주",
    "주일",  # 주
    "킬로",
    "미터",
    "킬로미터",  # 거리
    "그램",
    "킬로그램",  # 무게
    "리터",  # 부피
    "달러",
    "엔",
    "유로",
    "위안",  # 외화
    "조원",
    "만원",
    "억원",  # 금액 단위
    "평",
    "제곱미터",  # 면적
    "도",  # 온도/각도
    "점",  # 점수
    "위",  # 순위
    "세",
    "살",  # 나이
    "장",
    "권",
    "편",
    "곡",
    "척",  # 수량사
    "가지",
    "종",  # 종류
}

# ============================================================
# 고유명사 보호 목록
# ============================================================

# 한글 숫자로 시작하지만 숫자가 아닌 단어들
_BRAND_PATTERNS: set[str] = {
    # 삼(3)으로 시작
    "삼성",
    "삼양",
    "삼화",
    "삼천리",
    "삼립",
    "삼국",
    # 일(1)로 시작
    "일동",
    "일진",
    "일양",
    "일단",
    "일반",
    "일부",
    "일종",
    "일정",
    "일시",
    "일괄",
    "일체",
    "일치",
    "일률",
    "일상",
    "일방",
    "일련",
    "일요일",
    "일본",
    # 이(2)로 시작
    "이마트",
    "이화",
    "이랜드",
    "이번",
    "이후",
    "이전",
    "이상",
    "이하",
    "이미",
    "이제",
    "이런",
    "이것",
    "이때",
    "이날",
    "이를",
    "이유",
    "이해",
    "이익",
    "이용",
    "이동",
    "이어",
    "이내",
    "이래",
    # 사(4)로 시작
    "사실",
    "사업",
    "사용",
    "사람",
    "사이",
    "사항",
    "사무",
    "사과",
    "사장",
    "사회",
    "사건",
    "사례",
    "사전",
    "사후",
    "사태",
    "사망",
    "사고",
    "사기",
    "사정",
    "사진",
    # 오(5)로 시작
    "오히려",
    "오늘",
    "오전",
    "오후",
    "오래",
    "오뚜기",
    "오리온",
    "오류",
    "오해",
    "오직",
    # 육(6)으로 시작
    "육성",
    "육아",
    "육지",
    # 칠(7)로 시작
    "칠성",
    # 팔(8)로 시작
    "팔도",
    "팔자",
    # 구(9)로 시작
    "구미",
    "구조",
    "구현",
    "구체",
    "구간",
    "구성",
    "구분",
    "구매",
    "구역",
    "구상",
    "구축",
    "구두",
    "구급",
    "구독",
}

# ============================================================
# 정규식 패턴 (모듈 레벨 사전 컴파일)
# ============================================================

# 한글 숫자 문자 집합: 일~구 + 십백천 + 만억조
_KR_NUM_CHARS = "일이삼사오육칠팔구십백천만억조"

# 한글 숫자 패턴: 최소 1글자, 최대 합리적인 길이
# 예: "삼십", "이백오십", "이천이십육", "오백만"
_KR_NUMBER_PATTERN = re.compile(rf"([{_KR_NUM_CHARS}]{{1,12}})")

# 혼합형 패턴: 아라비아 숫자 + 한글 자리수 (예: "3십", "2백", "15만")
_MIXED_PATTERN = re.compile(r"(\d{1,15})(십|백|천|만|억|조)")

# 고유명사 보호 패턴: 가장 긴 것부터 매칭 (탐욕적)
_BRAND_REGEX = re.compile(
    "|".join(re.escape(brand) for brand in sorted(_BRAND_PATTERNS, key=len, reverse=True))
)

# 플레이스홀더 포맷 (고유명사 보호용)
_PLACEHOLDER_FMT = "\x00BRAND_{idx}\x00"
_PLACEHOLDER_REGEX = re.compile(r"\x00BRAND_(\d{1,10})\x00")


# ============================================================
# 한글 숫자 → 정수 변환
# ============================================================


def _korean_number_to_int(korean: str) -> int | None:
    """한글 숫자 문자열을 정수로 변환한다.

    파싱 알고리즘:
        1. 큰 단위(만/억/조) 기준으로 분할
        2. 각 부분의 십/백/천 단위 계산
        3. 큰 단위와 곱하여 합산

    Args:
        korean: 한글 숫자 문자열 (예: "삼십", "이백오십", "오백만")

    Returns:
        변환된 정수. 변환 실패 시 None 반환.

    Examples:
        >>> _korean_number_to_int("삼십")
        30
        >>> _korean_number_to_int("이백오십")
        250
        >>> _korean_number_to_int("이천이십육")
        2026
        >>> _korean_number_to_int("오백만")
        5000000
    """
    if not korean:
        return None

    # 모든 문자가 한글 숫자 문자인지 검증
    for ch in korean:
        if ch not in _DIGITS and ch not in _POSITIONS and ch not in _LARGE_UNITS:
            return None

    try:
        return _parse_korean_number(korean)
    except (ValueError, KeyError):
        logger.debug(f"한글 숫자 변환 실패: '{korean}'")
        return None


def _parse_korean_number(korean: str) -> int:
    """한글 숫자를 파싱하여 정수로 반환한다 (내부 구현).

    큰 단위(만/억/조)를 구분자로 사용하여 재귀적으로 처리한다.

    Args:
        korean: 검증된 한글 숫자 문자열

    Returns:
        변환된 정수

    Raises:
        ValueError: 파싱 불가능한 문자열
    """
    result = 0
    remaining = korean

    # 큰 단위(조 → 억 → 만) 순서로 처리
    for unit_name, unit_value in sorted(_LARGE_UNITS.items(), key=lambda x: x[1], reverse=True):
        if unit_name in remaining:
            parts = remaining.split(unit_name, 1)
            prefix = parts[0]
            remaining = parts[1] if len(parts) > 1 else ""

            # 큰 단위 앞의 숫자 계산
            if prefix:
                prefix_val = _parse_small_number(prefix)
            else:
                # "만" 단독 → 1만
                prefix_val = 1

            result += prefix_val * unit_value

    # 남은 부분 (천 이하) 처리
    if remaining:
        result += _parse_small_number(remaining)

    if result == 0 and korean:
        raise ValueError(f"변환 결과가 0: '{korean}'")

    return result


def _parse_small_number(korean: str) -> int:
    """천 이하의 한글 숫자를 정수로 변환한다.

    Args:
        korean: 천 이하의 한글 숫자 문자열 (예: "삼십", "이백오십", "천이백")

    Returns:
        변환된 정수

    Raises:
        ValueError: 파싱 불가능한 문자열
    """
    if not korean:
        return 0

    result = 0
    current = 0  # 현재 자리수 앞의 숫자

    for ch in korean:
        if ch in _DIGITS:
            current = _DIGITS[ch]
        elif ch in _POSITIONS:
            pos_value = _POSITIONS[ch]
            if current == 0:
                # "십" 단독 → 10, "백" 단독 → 100
                current = 1
            result += current * pos_value
            current = 0
        else:
            raise ValueError(f"예상치 못한 문자: '{ch}' in '{korean}'")

    # 마지막에 자리수 없이 끝나는 숫자 (예: "이백오십삼"의 "삼")
    result += current

    return result


# ============================================================
# 고유명사 보호/복원
# ============================================================


def _protect_brands(text: str) -> tuple[str, list[str]]:
    """고유명사를 플레이스홀더로 대체하여 보호한다.

    Args:
        text: 원본 텍스트

    Returns:
        (플레이스홀더가 적용된 텍스트, 원본 고유명사 리스트) 튜플
    """
    brands_found: list[str] = []

    def _replace_brand(match: re.Match) -> str:
        """매칭된 고유명사를 플레이스홀더로 교체한다."""
        brand = match.group(0)
        idx = len(brands_found)
        brands_found.append(brand)
        return _PLACEHOLDER_FMT.format(idx=idx)

    protected = _BRAND_REGEX.sub(_replace_brand, text)
    return protected, brands_found


def _restore_brands(text: str, brands: list[str]) -> str:
    """플레이스홀더를 원래 고유명사로 복원한다.

    Args:
        text: 플레이스홀더가 포함된 텍스트
        brands: 원본 고유명사 리스트

    Returns:
        복원된 텍스트
    """

    def _restore(match: re.Match) -> str:
        """플레이스홀더를 원래 고유명사로 교체한다."""
        idx = int(match.group(1))
        if 0 <= idx < len(brands):
            return brands[idx]
        return match.group(0)  # 인덱스 범위 밖이면 그대로 유지

    return _PLACEHOLDER_REGEX.sub(_restore, text)


# ============================================================
# 혼합형 숫자 처리
# ============================================================


def _normalize_mixed(text: str) -> str:
    """혼합형 숫자를 처리한다.

    아라비아 숫자 + 한글 자리수 조합을 완전한 아라비아 숫자로 변환한다.

    Args:
        text: 입력 텍스트

    Returns:
        혼합형 숫자가 변환된 텍스트

    Examples:
        >>> _normalize_mixed("3십 퍼센트")
        '30 퍼센트'
        >>> _normalize_mixed("2백만 원")
        '200만 원'
    """

    def _replace_mixed(match: re.Match) -> str:
        """혼합형 숫자 매칭을 변환한다."""
        num_str = match.group(1)
        unit_char = match.group(2)

        try:
            num = int(num_str)
            if unit_char in _POSITIONS:
                return str(num * _POSITIONS[unit_char])
            elif unit_char in _LARGE_UNITS:
                return str(num * _LARGE_UNITS[unit_char])
        except (ValueError, OverflowError):
            pass

        # 변환 실패 시 원본 유지
        return match.group(0)

    return _MIXED_PATTERN.sub(_replace_mixed, text)


# ============================================================
# 메인 정규화 함수
# ============================================================


def _get_units_for_level(level: int) -> set[str]:
    """레벨에 따른 허용 단위어 집합을 반환한다.

    Args:
        level: 변환 수준 (1=보수적, 2=중간)

    Returns:
        허용 단위어 집합
    """
    if level >= 2:
        return _SAFE_UNITS_L2
    return _SAFE_UNITS_L1


def _build_unit_pattern(units: set[str]) -> re.Pattern:
    """단위어 집합으로 정규식 패턴을 생성한다.

    단일 한글 숫자(일, 이, 삼 등)는 단위어에 **바로 붙어있을 때만** 매칭한다.
    2글자 이상의 숫자(삼십, 이백 등)는 공백이 있어도 매칭한다.
    이를 통해 "이 프로젝트"의 관사 "이"가 오변환되는 것을 방지한다.

    Args:
        units: 허용 단위어 집합

    Returns:
        컴파일된 정규식 패턴 (한글숫자 + 공백? + 단위어)
    """
    # 단위어를 길이 내림차순으로 정렬 (탐욕적 매칭)
    sorted_units = sorted(units, key=len, reverse=True)
    units_pattern = "|".join(re.escape(u) for u in sorted_units)

    # 단위어 뒤에 올 수 있는 허용 조사/어미 패턴
    # 이 패턴이 매칭되면 단위어 뒤에 한글이 와도 허용한다
    # 예: "일까지", "명이", "원을", "퍼센트도", "개가"
    allowed_suffix = r"(?:까지|에서|에|도|이|가|은|는|을|를|의|과|와|로|부터|만|씩|이나|이면)"

    # 단위어 뒤 경계 조건:
    # - 문자열 끝 or 공백 or 숫자 or 구두점: 무조건 허용
    # - 허용 조사가 바로 오는 경우: 허용
    # - 그 외 한글: 차단 (단위어가 더 긴 단어의 접두사일 가능성)
    unit_boundary = rf"(?={allowed_suffix}|[^가-힣]|\s|$)"

    # 패턴 설명:
    # - 2글자 이상 한글숫자 + 선택적 공백 + 단위어 + 경계 (예: "삼십 퍼센트")
    # - 또는 단일 한글숫자 + 단위어 + 경계 — 앞에 한글 없을 때만
    # 이렇게 하면 "이 프로젝트"의 "이"+"프로"는 "프로젝트" 때문에 차단됨
    pattern = (
        rf"(?:([{_KR_NUM_CHARS}]{{2,12}})\s*({units_pattern}){unit_boundary}"  # 2글자 이상
        rf"|(?<![가-힣])([{_KR_NUM_CHARS}])\s*({units_pattern}){unit_boundary})"  # 단일 글자
    )
    return re.compile(pattern)


# 레벨별 패턴 사전 컴파일
_UNIT_PATTERN_L1 = _build_unit_pattern(_SAFE_UNITS_L1)
_UNIT_PATTERN_L2 = _build_unit_pattern(_SAFE_UNITS_L2)


def _build_native_pattern(units: set[str]) -> re.Pattern:
    """고유어 숫자 + 단위어 패턴을 생성한다.

    한자식 숫자(_DIGITS)와 별도로 처리한다. 길이 내림차순 매칭으로
    "스물" 이 "스" 보다 먼저 잡히게 한다.

    Args:
        units: 허용 단위어 집합

    Returns:
        컴파일된 정규식 패턴. 그룹(1)=고유어, 그룹(2)=단위어.
    """
    sorted_natives = sorted(_NATIVE_DIGITS.keys(), key=len, reverse=True)
    natives_pattern = "|".join(re.escape(n) for n in sorted_natives)

    sorted_units = sorted(units, key=len, reverse=True)
    units_pattern = "|".join(re.escape(u) for u in sorted_units)

    allowed_suffix = r"(?:까지|에서|에|도|이|가|은|는|을|를|의|과|와|로|부터|만|씩|이나|이면)"
    unit_boundary = rf"(?={allowed_suffix}|[^가-힣]|\s|$)"

    # 고유어 앞에는 한글이 없어야 함 (예: "친한 명" 의 "한" 은 변환 안 됨)
    pattern = rf"(?<![가-힣])({natives_pattern})\s*({units_pattern}){unit_boundary}"
    return re.compile(pattern)


_NATIVE_PATTERN_L1 = _build_native_pattern(_SAFE_UNITS_L1)
_NATIVE_PATTERN_L2 = _build_native_pattern(_SAFE_UNITS_L2)


def _replace_native_numbers(text: str, pattern: re.Pattern) -> str:
    """고유어 숫자 + 단위어를 아라비아 숫자로 변환.

    예: "여덟 개" → "8 개", "두 명" → "2 명", "스무 살" → "20 살"
    """

    def _sub(m: re.Match) -> str:
        native = m.group(1)
        unit = m.group(2)
        value = _NATIVE_DIGITS.get(native)
        if value is None:
            return m.group(0)
        return f"{value} {unit}"

    return pattern.sub(_sub, text)


def normalize_numbers(text: str, level: int = 1) -> str:
    """텍스트에서 한글 숫자를 아라비아 숫자로 변환한다.

    화이트리스트 기반 보수적 접근법으로, 안전한 단위어가 뒤따르는
    한글 숫자만 변환한다. 고유명사(삼성, 이마트 등)는 보호된다.

    Args:
        text: 입력 텍스트
        level: 변환 수준 (0=비활성, 1=보수적, 2=중간)

    Returns:
        정규화된 텍스트. 변환 실패 시 원본 유지.

    Examples:
        >>> normalize_numbers("삼십 퍼센트")
        '30 퍼센트'
        >>> normalize_numbers("삼성전자 주가")
        '삼성전자 주가'
        >>> normalize_numbers("삼십 퍼센트", level=0)
        '삼십 퍼센트'
    """
    # level=0이면 비활성 → 원본 반환
    if level <= 0:
        return text

    # 빈 문자열 처리
    if not text:
        return text

    try:
        # 1단계: 고유명사 보호
        protected, brands = _protect_brands(text)

        # 2단계: 혼합형 처리 (3십 → 30) — 한글 변환보다 먼저 실행
        # 이유: "3십"이 한글 패턴에 의해 "십"만 변환되면 "310"이 됨
        protected = _normalize_mixed(protected)

        # 3단계: 한자식 한글숫자 + 단위어 패턴 변환 (예: "삼십 퍼센트" → "30 퍼센트")
        unit_pattern = _UNIT_PATTERN_L2 if level >= 2 else _UNIT_PATTERN_L1
        protected = _replace_korean_numbers(protected, unit_pattern)

        # 3.5단계: 고유어 숫자 + 단위어 변환 (예: "여덟 개" → "8 개", "두 명" → "2 명")
        # 한자식 변환 후에 적용: "삼십" 같은 단어가 먼저 변환된 뒤에 "여덟" 등을 처리해
        # 충돌을 방지한다.
        native_pattern = _NATIVE_PATTERN_L2 if level >= 2 else _NATIVE_PATTERN_L1
        protected = _replace_native_numbers(protected, native_pattern)

        # 4단계: 고유명사 복원
        result = _restore_brands(protected, brands)

        # 5단계: NFC 정규화 유지
        result = unicodedata.normalize("NFC", result)

        return result

    except Exception as e:
        # 어떤 예외가 발생하더라도 원본을 손상시키지 않는다
        logger.warning(f"숫자 정규화 중 오류 발생, 원본 유지: {e}")
        return text


def _replace_korean_numbers(text: str, unit_pattern: re.Pattern) -> str:
    """한글 숫자 + 단위어 패턴을 찾아 아라비아 숫자로 변환한다.

    정규식은 4개의 그룹을 가진다:
        - group(1), group(2): 2글자 이상 한글숫자 + 단위어
        - group(3), group(4): 단일 한글숫자 + 단위어 (앞에 한글 없음)

    Args:
        text: 고유명사가 보호된 텍스트
        unit_pattern: 레벨에 맞는 단위어 정규식 패턴

    Returns:
        한글 숫자가 변환된 텍스트
    """

    def _replace_match(match: re.Match) -> str:
        """매칭된 한글 숫자를 아라비아 숫자로 변환한다."""
        # 두 가지 대안 중 매칭된 쪽을 선택
        if match.group(1) is not None:
            korean_num = match.group(1)
            unit = match.group(2)
        else:
            korean_num = match.group(3)
            unit = match.group(4)

        # 한글 숫자 → 정수 변환
        value = _korean_number_to_int(korean_num)

        if value is not None:
            # 원본의 공백 유지: 원본에 공백이 있었으면 변환 후에도 공백 유지
            full_match: str = match.group(0)
            middle = full_match[len(korean_num) : -len(unit)]
            has_space = " " in middle if middle else False

            separator = " " if has_space else ""
            return f"{value}{separator}{unit}"

        # 변환 실패 시 원본 유지
        original: str = match.group(0)
        return original

    return unit_pattern.sub(_replace_match, text)
