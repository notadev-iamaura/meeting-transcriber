"""Wiki 인용 마커 파싱·검증 모듈 (D1 인용 강제 후처리)

목적: PRD §4.3 의 인용 형식 표준 `[meeting:{id}@{HH:MM:SS}]` 를 파싱·검증하고,
WikiGuard D1 단계의 인용 강제 후처리를 담당한다.

주요 기능:
    - CITATION_PATTERN: 8자리 hex meeting_id + HH:MM:SS 만 매칭
    - PAGE_LINK_PATTERN: 페이지 간 상대 링크 (`[../people/철수.md]`)
    - parse_citation(text): 단일 인용 → (meeting_id, timestamp_str) 튜플 (실패 시 None)
    - is_factual_statement(line): 인용 의무 대상 판별 (제목/메타/링크 면제)
    - enforce_citations(content, meeting_id): D1 후처리 — 인용 없는 사실 문장 제거
    - WikiGuardError: D1/D2/D3 모두에서 발생하는 검증 실패 예외

의존성: 표준 라이브러리(re, logging) + core.wiki.models. WikiStore 는 import 하지 않음.
"""

from __future__ import annotations

import logging
import re

from core.wiki.models import Citation  # noqa: F401  (외부 사용용 재노출 보존)

logger = logging.getLogger(__name__)


# PRD §4.3 검증 정규식 — 8자리 소문자 hex meeting_id, HH:MM:SS (각 자릿수 고정 2자리).
# 대문자 hex, 짧은 ID, 잘못된 시각 형식은 모두 거부된다.
CITATION_PATTERN: re.Pattern[str] = re.compile(
    r"\[meeting:([a-f0-9]{8})@(\d{2}):(\d{2}):(\d{2})\]"
)

# 페이지 간 상대 링크 (frontmatter 헤더의 참고 회의, 후속 결정 링크 등).
# 예: "[../people/철수.md]", "[../../decisions/2026-04-15-x.md]"
PAGE_LINK_PATTERN: re.Pattern[str] = re.compile(
    r"\[(\.\./)+[a-z_]+/[^\]]+\.md\]"
)

# D1 거부율 임계 — 의무 대상 줄 중 거부된 줄이 30% 를 초과하면 페이지 자체 무효화
D1_REJECTION_THRESHOLD: float = 0.30

# 임계 검사를 적용할 최소 의무 대상 줄 수.
# 표본이 너무 작으면(예: 의무 1줄) 통계적 거부율이 의미가 없어 페이지가 부당하게 무효화되므로,
# PRD §6 D1 의 임계 검사는 충분한 표본이 있을 때만 발동한다.
_D1_MIN_SAMPLE_SIZE: int = 4


class WikiGuardError(Exception):
    """5중 방어(D1/D2/D3) 가 실패했을 때 던지는 예외.

    `reason` 코드는 호출자가 분기 처리할 수 있도록 안정적인 식별자를 사용한다.
    예시 코드:
        - "too_many_uncited_statements": D1 거부율 30% 초과
        - "phantom_citation": D2 timestamp 가 실제 발화와 불일치
        - "low_confidence": D3 confidence < threshold
        - "malformed_confidence": D3 confidence 마커 누락 또는 비정수
        - "decide_pages_failed": _decide_pages JSON 파싱 2회 실패
    """

    def __init__(self, reason: str, detail: str | None = None) -> None:
        """검증 실패 사유 코드와 상세 메시지를 받는다.

        Args:
            reason: 안정적 코드(snake_case). 호출자 분기 처리에 사용.
            detail: 사람이 읽는 상세 메시지. log.md 기록·테스트 assert 에 사용.
        """
        # args[0] 으로 reason 을 노출하여 표준 Exception 호환성 유지
        super().__init__(reason)
        # 명시적 속성 노출 — hasattr(error, "reason") 에서 True 를 반환하도록
        self.reason: str = reason
        self.detail: str | None = detail


def parse_citation(text: str) -> tuple[str, str] | None:
    """단일 인용 마커 문자열을 (meeting_id, timestamp_str) 튜플로 파싱한다.

    검증 범위:
        - 형식(8 hex + HH:MM:SS) 만 검사. 시·분·초 값의 의미적 정합성
          (분 < 60, 초 < 60 등) 은 검사하지 않으며 D2 단계 (utterance 와의
          실제 발화 시각 비교) 에서 phantom_citation 으로 거부된다.

    Args:
        text: 인용 마커를 포함할 수 있는 임의의 문자열. re.search 기반이므로
            첫 번째 매칭만 반환한다.

    Returns:
        매칭 성공 시 (meeting_id, "HH:MM:SS") 튜플. 매칭 실패 또는 형식 불량
        시 None.
    """
    if not text:
        return None
    match = CITATION_PATTERN.search(text)
    if match is None:
        return None
    # 그룹 1=id, 그룹 2/3/4=H/M/S — 원문 형태 그대로 보존하여 반환
    meeting_id = match.group(1)
    timestamp_str = f"{match.group(2)}:{match.group(3)}:{match.group(4)}"
    return (meeting_id, timestamp_str)


def is_factual_statement(line: str) -> bool:
    """줄(line) 이 인용 의무 대상(사실 진술) 인지 판정한다.

    면제 대상 (False):
        - 빈 줄 또는 공백만 있는 줄
        - 마크다운 제목(#, ##, ...)
        - YAML frontmatter 구분자(---)
        - 순수 페이지 링크([../people/x.md])
        - HTML 주석(<!-- ... -->)
        - 표 구분자 줄(|---|---|)
        - 코드블록 펜스(```)

    의무 대상 (True):
        - 평문 사실 진술
        - 리스트 항목 본문(- ...)
        - 표 셀 본문
        - 인용 마커가 이미 있는 줄도 True

    Args:
        line: 줄바꿈을 포함하지 않는 단일 라인 문자열.

    Returns:
        인용 의무 대상이면 True, 면제면 False.
    """
    stripped = line.strip()

    # 빈 줄·공백만 있는 줄은 면제
    if not stripped:
        return False

    # 코드블록 펜스 (``` 시작·종료) — 펜스 자체는 면제
    if stripped.startswith("```"):
        return False

    # YAML frontmatter 구분자 (--- 단독)
    if stripped == "---":
        return False

    # 마크다운 제목 (#, ##, ### ...) — 공백이 뒤따르거나 단독으로 시작
    if stripped.startswith("#"):
        return False

    # HTML 주석 (<!-- ... -->) — confidence 마커 등
    if stripped.startswith("<!--") and stripped.endswith("-->"):
        return False

    # 표 구분자 줄 (|---|---|) — 파이프와 하이픈/공백/콜론만 구성된 줄
    # |---|---| 또는 | --- | --- | 같은 형태 (정렬 콜론 포함 가능)
    if stripped.startswith("|") and stripped.endswith("|"):
        # 표 구분자 패턴: 파이프·하이픈·공백·콜론만 있는지 확인
        inner = stripped.strip("|")
        if all(ch in "-: |" for ch in inner) and "-" in inner:
            return False

    # 순수 페이지 링크만 있는 줄 (`[../people/철수.md]`)
    if PAGE_LINK_PATTERN.fullmatch(stripped):
        return False

    return True


def _is_frontmatter_keyvalue(line: str) -> bool:
    """frontmatter 영역 내 key: value 줄인지 보수적으로 판정한다.

    호출자가 frontmatter 영역을 사전 분리하지 못한 경우의 폴백.

    Args:
        line: 검사할 단일 라인 문자열.

    Returns:
        `key: value` 형태의 줄로 판단되면 True.
    """
    stripped = line.strip()
    # `식별자: 값` 패턴 — key 는 알파벳/숫자/언더스코어만
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*:\s*", stripped))


def enforce_citations(content: str, meeting_id: str) -> tuple[str, list[str]]:
    """D1 인용 강제 후처리 — 인용 없는 사실 진술 줄을 자동 제거한다.

    PRD §6 D1 알고리즘:
        1. 줄 단위로 순회
        2. 메타/면제 줄은 그대로 통과
        3. 사실 진술이지만 CITATION_PATTERN 매칭이 없는 줄은 제거 후 거부 목록에 기록
        4. 거부 줄 비율이 전체 의무 대상의 30% 를 초과하면 페이지 자체 무효
           → WikiGuardError("too_many_uncited_statements") 발생
        5. frontmatter (`--- ... ---`) 와 코드블록(``` ... ```) 영역은 스킵 처리

    Args:
        content: LLM 이 출력한 원본 마크다운 문자열.
        meeting_id: 이 페이지 갱신을 트리거한 회의의 ID. 향후 D1 강화 시 같은
            meeting_id 인용만 허용하도록 사용 가능. 현재는 시그니처 보존만.

    Returns:
        (정제된 content, 거부된 줄 목록).

    Raises:
        WikiGuardError: 거부율이 30% 를 초과할 때 (코드 "too_many_uncited_statements").
    """
    # 빈 입력 처리 — 빈 content 와 빈 rejected 반환
    if not content:
        return ("", [])

    # 줄별 순회 — splitlines(keepends=True) 로 원본 줄바꿈 보존
    lines = content.splitlines(keepends=True)

    kept_lines: list[str] = []
    rejected_lines: list[str] = []
    mandatory_count = 0  # 의무 대상 줄 개수 (거부율 계산용)

    in_frontmatter = False
    in_code_block = False
    # frontmatter 는 **문서 첫 줄이 정확히 `---`** 일 때만 시작된다.
    # 본문 중간에 등장한 `---` (수평선 등) 을 frontmatter 시작으로 오인하지
    # 않도록 사전에 첫 줄을 확인하여 frontmatter_started_once 를 미리 결정.
    first_stripped = lines[0].rstrip("\n").rstrip("\r").strip() if lines else ""
    frontmatter_started_once = first_stripped != "---"

    for raw_line in lines:
        # 줄바꿈 제거된 형태로 판정에 사용
        line_no_newline = raw_line.rstrip("\n").rstrip("\r")
        stripped = line_no_newline.strip()

        # ── frontmatter 영역 진입/탈출 처리 (--- 구분자 기반) ───────
        # 문서 첫 부분의 --- 가 시작 구분자, 다음 --- 가 종료 구분자
        if stripped == "---":
            if not frontmatter_started_once:
                # 첫 등장 — frontmatter 시작
                in_frontmatter = True
                frontmatter_started_once = True
                kept_lines.append(raw_line)
                continue
            if in_frontmatter:
                # frontmatter 종료
                in_frontmatter = False
                kept_lines.append(raw_line)
                continue
            # frontmatter 외부의 --- 는 일반 면제 줄로 처리 (구분자 등)
            kept_lines.append(raw_line)
            continue

        # frontmatter 내부 — 의무 대상으로 간주하지 않고 그대로 보존
        if in_frontmatter:
            kept_lines.append(raw_line)
            continue

        # ── 코드블록 영역 진입/탈출 처리 (``` 펜스 기반) ───────────
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            kept_lines.append(raw_line)
            continue

        # 코드블록 내부 — 의무 대상으로 간주하지 않고 그대로 보존
        if in_code_block:
            kept_lines.append(raw_line)
            continue

        # ── 일반 영역 — is_factual_statement 로 의무 여부 판정 ─────
        if not is_factual_statement(line_no_newline):
            # 면제 줄은 그대로 보존
            kept_lines.append(raw_line)
            continue

        # 의무 대상 — 카운트 증가 + 인용 검사
        mandatory_count += 1
        if CITATION_PATTERN.search(line_no_newline):
            # 인용 있음 — 보존
            kept_lines.append(raw_line)
        else:
            # 인용 없음 — 거부 (rejected 에는 줄바꿈 제외 원문 보존)
            rejected_lines.append(line_no_newline)

    # ── 30% 임계 검사 ────────────────────────────────────────────────
    # 의무 대상 0 이면 비율 계산 불가 — 임계 초과 아님.
    # 표본이 _D1_MIN_SAMPLE_SIZE 미만일 때도 통계적 의미가 없어 발동하지 않는다.
    if mandatory_count >= _D1_MIN_SAMPLE_SIZE:
        rejection_rate = len(rejected_lines) / mandatory_count
        if rejection_rate > D1_REJECTION_THRESHOLD:
            logger.warning(
                "D1 거부율 임계 초과: meeting_id=%s, rejected=%d/%d (%.2f%%)",
                meeting_id,
                len(rejected_lines),
                mandatory_count,
                rejection_rate * 100,
            )
            raise WikiGuardError(
                "too_many_uncited_statements",
                f"의무 대상 {mandatory_count}줄 중 {len(rejected_lines)}줄이 인용 없음 "
                f"(거부율 {rejection_rate * 100:.1f}% > 30%)",
            )

    return ("".join(kept_lines), rejected_lines)
