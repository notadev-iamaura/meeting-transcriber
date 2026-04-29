"""WikiGuard — 5중 방어의 D1 (citations) + D2 (실재성) + D3 (confidence) 통합 게이트.

목적: 단일 페이지 갱신 결과에 대해 D1~D3 를 순차 적용하고 최종 GuardVerdict 를
반환한다. WikiCompilerV2 가 매 페이지 갱신 직후 verify() 를 호출하며, 통과한
페이지만 디스크에 쓰여 git_commit_atomic 으로 커밋된다.

방어 순서 (PRD §6 5중 방어 요약 표):
    D1 (인용 강제) → D2 (인용 실재성) → D3 (confidence)

의존성:
    - core.wiki.citations (enforce_citations, parse_citation, WikiGuardError)
    - core.wiki.models (Citation)
"""

from __future__ import annotations

import logging
import re
from typing import NamedTuple, Protocol, runtime_checkable

from core.wiki.citations import (
    CITATION_PATTERN,
    WikiGuardError,
    enforce_citations,
)
from core.wiki.models import Citation  # noqa: F401  (외부 노출용)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# 2.1 D2 검증 추상화 — RAG 또는 in-memory mock
# ─────────────────────────────────────────────────────────────────────────


@runtime_checkable
class CitationVerifier(Protocol):
    """D2 인용 실재성 검증 추상화.

    Phase 2 골격에서는 in-memory mock(`InMemoryCitationVerifier`) 으로 모든
    테스트를 통과시키고, Phase 2.E 에서 `RagCitationVerifier` 가 ChromaDB 등으로
    구현된다.
    """

    async def verify_exists(
        self,
        meeting_id: str,
        timestamp_seconds: int,
    ) -> bool:
        """주어진 (meeting_id, timestamp) 가 실제 발화에 매핑되는지 검사한다."""
        ...

    async def fetch_utterance(
        self,
        meeting_id: str,
        timestamp_seconds: int,
    ) -> str | None:
        """(선택) 매핑된 발화 텍스트를 반환. 미발견 시 None."""
        ...


class InMemoryCitationVerifier:
    """테스트용 in-memory 구현. (meeting_id, ts_seconds) → text 매핑 dict.

    Attributes:
        known: (meeting_id, ts_seconds) → 발화 텍스트.
        tolerance_sec: verify_exists 의 ±초 허용치 (기본 2).
    """

    def __init__(
        self,
        known: dict[tuple[str, int], str] | None = None,
        tolerance_sec: int = 2,
    ) -> None:
        """known 매핑과 tolerance 를 받는다."""
        self.known: dict[tuple[str, int], str] = known or {}
        self.tolerance_sec: int = tolerance_sec

    async def verify_exists(self, meeting_id: str, timestamp_seconds: int) -> bool:
        """±tolerance 내 발화가 존재하면 True."""
        for ts in range(
            timestamp_seconds - self.tolerance_sec,
            timestamp_seconds + self.tolerance_sec + 1,
        ):
            if (meeting_id, ts) in self.known:
                return True
        return False

    async def fetch_utterance(
        self, meeting_id: str, timestamp_seconds: int
    ) -> str | None:
        """매핑된 발화 텍스트 반환."""
        for ts in range(
            timestamp_seconds - self.tolerance_sec,
            timestamp_seconds + self.tolerance_sec + 1,
        ):
            text = self.known.get((meeting_id, ts))
            if text is not None:
                return text
        return None


# ─────────────────────────────────────────────────────────────────────────
# 2.2 GuardVerdict — verify() 의 단일 반환 타입
# ─────────────────────────────────────────────────────────────────────────


class GuardVerdict(NamedTuple):
    """단일 페이지에 대한 5중 방어 D1~D3 의 종합 판정.

    NamedTuple 로 구현하여 진정한 immutability 를 보장한다 (frozen dataclass 는
    object.__setattr__ 로 우회 가능하지만 NamedTuple 은 모든 setattr 차단).

    필드 의미:
        passed: D1·D2·D3 모두 통과 시 True. False 면 reason 으로 사유 분기.
        reason: 안정적 코드 (snake_case). 가능 값:
            - "passed": 정상 통과.
            - "low_confidence": D3 confidence < threshold.
            - "phantom_citation": D2 매핑 실패 인용이 있음.
            - "uncited_overflow": D1 거부율 30% 초과.
            - "malformed_confidence": D3 confidence 마커 누락/비정수.
        confidence: 추출된 0~10 정수. 추출 실패 시 -1.
        rejected_citations: D2 phantom 으로 판정된 인용 raw 문자열 목록.
        cleaned_content: D1 후처리 결과. uncited_overflow 면 None.
        d1_dropped_lines: D1 이 제거한 줄 수.
    """

    passed: bool
    reason: str
    confidence: int
    rejected_citations: list[str] = []
    cleaned_content: str | None = None
    d1_dropped_lines: int = 0


# ─────────────────────────────────────────────────────────────────────────
# 2.3 confidence 마커 추출 — D3 헬퍼
# ─────────────────────────────────────────────────────────────────────────

# `<!-- confidence: 8 -->` 형식 — 공백 허용, 앞뒤 공백 모두 optional
# `<!--confidence:8-->` 같이 공백 없는 형태도 허용
_CONFIDENCE_PATTERN: re.Pattern[str] = re.compile(
    r"<!--\s*confidence\s*:\s*(\d{1,2})\s*-->"
)


def extract_confidence(content: str) -> int:
    """페이지 본문에서 `<!-- confidence: N -->` 를 추출한다.

    Args:
        content: 페이지 마크다운 본문 (frontmatter 포함 여부 무관).

    Returns:
        매칭 성공 시 정수 (0~10). 실패 또는 범위 밖이면 -1.
    """
    if not content:
        return -1
    match = _CONFIDENCE_PATTERN.search(content)
    if match is None:
        return -1
    try:
        value = int(match.group(1))
    except ValueError:
        return -1
    # 0~10 범위 검증 — 범위 밖이면 비정수와 동일 처리
    if value < 0 or value > 10:
        return -1
    return value


# ─────────────────────────────────────────────────────────────────────────
# 2.4 WikiGuard — D1+D2+D3 통합 게이트
# ─────────────────────────────────────────────────────────────────────────


class WikiGuard:
    """5중 방어의 D1 + D2 + D3 통합 검증자.

    인스턴스는 무상태. WikiCompilerV2 가 단일 인스턴스를 공유.

    Attributes:
        verifier: D2 검증을 위한 RAG 또는 mock.
        confidence_threshold: D3 통과 컷오프 (0~10 정수, 기본 7).
        d1_min_sample_size: citations 모듈의 _D1_MIN_SAMPLE_SIZE 와 동일한 의미.
    """

    def __init__(
        self,
        verifier: CitationVerifier,
        *,
        confidence_threshold: int = 7,
        d1_min_sample_size: int = 4,
    ) -> None:
        """검증자와 임계값을 받는다.

        Args:
            verifier: CitationVerifier (Protocol).
            confidence_threshold: D3 컷오프.
            d1_min_sample_size: citations._D1_MIN_SAMPLE_SIZE 와 일치 권장.
        """
        self._verifier: CitationVerifier = verifier
        self._confidence_threshold: int = confidence_threshold
        self._d1_min_sample_size: int = d1_min_sample_size

    async def verify(
        self,
        *,
        page_path: str,
        new_content: str,
        meeting_id: str,
    ) -> GuardVerdict:
        """단일 페이지에 대해 D1 → D2 → D3 를 순차 적용한다.

        Args:
            page_path: 위키 루트 기준 상대 경로 (로깅용).
            new_content: LLM 이 출력한 raw 페이지 본문.
            meeting_id: 페이지 갱신을 트리거한 회의 ID.

        Returns:
            GuardVerdict — 모든 분기 결과를 단일 dataclass 로 반환.

        Note:
            본 메서드는 절대 예외를 raise 하지 않는다.
        """
        # ── D1: 인용 강제 ────────────────────────────────────────────
        try:
            cleaned_content, _rejected = enforce_citations(new_content, meeting_id)
        except WikiGuardError as exc:
            # D1 임계 초과 → 페이지 자체 무효화
            logger.warning(
                "D1 임계 초과: page=%s, reason=%s, detail=%s",
                page_path,
                exc.reason,
                exc.detail,
            )
            return GuardVerdict(
                passed=False,
                reason="uncited_overflow",
                confidence=-1,
                cleaned_content=None,
            )
        except Exception as exc:  # noqa: BLE001 — guard 는 절대 raise 하지 않음
            logger.error(
                "D1 처리 중 예상치 못한 오류: page=%s, error=%r", page_path, exc
            )
            return GuardVerdict(
                passed=False,
                reason="uncited_overflow",
                confidence=-1,
                cleaned_content=None,
            )

        # ── D2: 인용 실재성 검증 ─────────────────────────────────────
        rejected_citations: list[str] = []
        # cleaned_content 의 모든 인용을 verifier.verify_exists 로 검사
        for match in CITATION_PATTERN.finditer(cleaned_content):
            cit_meeting_id = match.group(1)
            hh, mm, ss = match.group(2), match.group(3), match.group(4)
            ts_seconds = int(hh) * 3600 + int(mm) * 60 + int(ss)
            try:
                exists = await self._verifier.verify_exists(cit_meeting_id, ts_seconds)
            except Exception as exc:  # noqa: BLE001 — verifier 오류는 phantom 으로 처리
                logger.warning(
                    "D2 verifier 오류 — phantom 처리: page=%s, citation=%s, error=%r",
                    page_path,
                    match.group(0),
                    exc,
                )
                rejected_citations.append(match.group(0))
                continue
            if not exists:
                rejected_citations.append(match.group(0))

        if rejected_citations:
            logger.warning(
                "D2 phantom citation 발견: page=%s, count=%d",
                page_path,
                len(rejected_citations),
            )
            return GuardVerdict(
                passed=False,
                reason="phantom_citation",
                confidence=-1,
                rejected_citations=rejected_citations,
                cleaned_content=cleaned_content,
            )

        # ── D3: confidence 마커 추출 + 임계 비교 ─────────────────────
        confidence = extract_confidence(cleaned_content)
        if confidence == -1:
            logger.warning("D3 confidence 마커 누락/비정수: page=%s", page_path)
            return GuardVerdict(
                passed=False,
                reason="malformed_confidence",
                confidence=-1,
                cleaned_content=cleaned_content,
            )
        if confidence < self._confidence_threshold:
            logger.info(
                "D3 confidence 미달: page=%s, confidence=%d, threshold=%d",
                page_path,
                confidence,
                self._confidence_threshold,
            )
            return GuardVerdict(
                passed=False,
                reason="low_confidence",
                confidence=confidence,
                cleaned_content=cleaned_content,
            )

        # ── 모두 통과 ────────────────────────────────────────────────
        return GuardVerdict(
            passed=True,
            reason="passed",
            confidence=confidence,
            cleaned_content=cleaned_content,
        )
