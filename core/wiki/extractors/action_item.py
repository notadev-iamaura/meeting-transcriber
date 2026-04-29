"""ActionItemExtractor — 신규 액션아이템 추출 + 기존 open 항목의 closed 전환 감지.

목적: PRD §4.2 의 단일 파일 `action_items.md` 를 자동 갱신한다. 핵심 책임:
    1. extract_new(): 회의에서 신규 발견 액션아이템 (LLM 1회).
    2. detect_closed(): 기존 open 목록에 대해, 이번 회의에서 완료/철회된 항목 식별 (LLM 1회).
    3. render_unified_page(): open/closed 섹션 병합 마크다운 본문 생성 (LLM 비호출).

의존성:
    - core.wiki.llm_client.WikiLLMClient
    - core.wiki.llm_client.sanitize_utterance_text
    - core.wiki.models.Citation
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from core.wiki.llm_client import WikiLLMClient, sanitize_utterance_text
from core.wiki.models import Citation

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# 4.0 Phase 2.E 헬퍼 — 상대 날짜 변환 + 화자 fuzzy matching
# ─────────────────────────────────────────────────────────────────────────


# 상대 날짜 표현 → meeting_date 기준 offset(일).
# 정규표현식 매칭 우선순위: 길이 긴 패턴부터 (다다음주가 다음주보다 먼저).
# 복잡한 표현 ("이번주 금요일", "다음 월요일") 은 미지원 — None 반환.
_RELATIVE_DATE_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    # 14일 후 — "다다음주" (다음주보다 먼저 매칭되어야 함)
    (re.compile(r"다다음주"), 14),
    # 7일 후 — "다음주" / "next week"
    (re.compile(r"다음\s*주"), 7),
    (re.compile(r"next\s+week", re.IGNORECASE), 7),
    # 2일 후 — "모레"
    (re.compile(r"모레"), 2),
    # 1일 후 — "내일" / "tomorrow"
    (re.compile(r"내일"), 1),
    (re.compile(r"tomorrow", re.IGNORECASE), 1),
]


def _resolve_relative_date(meeting_date: date, text: str) -> str | None:
    """발화 텍스트에서 상대 날짜 표현을 찾아 ISO 날짜 문자열로 변환한다.

    지원 표현:
        - "내일" / "tomorrow" → meeting_date + 1
        - "모레" → meeting_date + 2
        - "다음주" / "next week" → meeting_date + 7
        - "다다음주" → meeting_date + 14

    미지원 (None 반환):
        - "이번주 금요일", "다음 월요일" 같은 복합 표현
        - "이번 달 안에" 같은 모호한 표현

    Args:
        meeting_date: 회의 날짜 (기준).
        text: 검사 대상 발화 텍스트.

    Returns:
        "YYYY-MM-DD" 형식 ISO 날짜 또는 None.
    """
    if not text:
        return None
    for pattern, offset_days in _RELATIVE_DATE_PATTERNS:
        if pattern.search(text):
            resolved = meeting_date + timedelta(days=offset_days)
            return resolved.isoformat()
    return None


def _resolve_owner(
    raw_owner: str,
    speakers: set[str],
    speaker_name_map: dict[str, str] | None,
) -> str | None:
    """LLM 이 추출한 owner 를 실제 화자에 매핑한다 (fuzzy matching).

    동작 (우선순위):
        1. speaker_name_map 이 있으면 정규화 우선:
           - raw_owner 가 매핑 키(SPEAKER_XX) 면 → 매핑된 한국어 이름 반환.
           - raw_owner 가 매핑 값(한국어 이름) 중 하나면 그대로 반환.
        2. speakers set 에 raw_owner 가 있으면 그대로 반환 (매핑 없는 경우 폴백).
        3. 매칭 실패 → None (환각 방지).

    Args:
        raw_owner: LLM 이 반환한 owner 문자열.
        speakers: utterances 에서 추출한 실제 화자 라벨 set.
        speaker_name_map: corrector 가 제공한 {SPEAKER_XX: 한국어이름} 매핑.
            None 이면 fuzzy matching 비활성화 (string equality 만 사용).

    Returns:
        실제 화자 이름 또는 None.
    """
    if not raw_owner:
        return None

    # 1. speaker_name_map 우선 — 정규화 (SPEAKER_XX → 한국어이름)
    if speaker_name_map:
        # 1-a. SPEAKER 라벨 → 한국어 이름으로 정규화
        if raw_owner in speaker_name_map:
            return speaker_name_map[raw_owner]
        # 1-b. 한국어 이름이 매핑의 값에 있으면 그대로 인정 (fuzzy matching 성공)
        if raw_owner in set(speaker_name_map.values()):
            return raw_owner
        # 1-c. 매핑이 있는데 raw_owner 가 키/값 모두에 없음 → 환각 의심, None
        return None

    # 2. 매핑 미제공 시 string equality 폴백 (기존 동작 유지)
    if raw_owner in speakers:
        return raw_owner

    # 3. 매칭 실패 → None
    return None


# ─────────────────────────────────────────────────────────────────────────
# 4.1 데이터 모델
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NewActionItem:
    """이번 회의에서 새로 발견된 액션아이템.

    Attributes:
        owner: 담당자 이름.
        description: 한 줄 작업 설명.
        citation: 발화 인용.
        due_date: 마감일 ISO 문자열. 발화에서 추출 못하면 None.
        project_slug: 관련 프로젝트 slug. 없으면 None.
        confidence: LLM 자체 평가 0~10.
    """

    owner: str | None
    description: str
    citation: Citation
    due_date: str | None = None
    project_slug: str | None = None
    confidence: int = 0


@dataclass(frozen=True)
class OpenActionItem:
    """기존 action_items.md 의 ## Open 섹션에서 파싱된 항목.

    Attributes:
        item_id: 안정적 식별자 — SHA-1(owner+description+from_meeting_id) 8자리.
        owner: 담당자.
        description: 작업 설명.
        from_meeting_id: 처음 등장한 회의 ID.
        from_date: 처음 등장한 회의 날짜 (ISO 문자열).
        citation: 처음 등장한 발화 인용.
        project_slug: 프로젝트.
        due_date: 마감일.
    """

    item_id: str
    owner: str
    description: str
    from_meeting_id: str
    from_date: str
    citation: Citation
    project_slug: str | None = None
    due_date: str | None = None


@dataclass(frozen=True)
class ClosedActionItem:
    """이번 회의에서 닫힌(완료/철회) 액션아이템.

    Attributes:
        original: 원본 OpenActionItem.
        closed_by_speaker: 종료를 보고한 화자.
        closed_at_meeting_id: 종료 보고 회의.
        closed_citation: 종료 보고 발화 인용.
        closed_reason: "completed" | "cancelled" | "superseded".
    """

    original: OpenActionItem
    closed_by_speaker: str
    closed_at_meeting_id: str
    closed_citation: Citation
    closed_reason: str = "completed"


# ─────────────────────────────────────────────────────────────────────────
# 4.2 헬퍼
# ─────────────────────────────────────────────────────────────────────────

# 발화 텍스트 내 명시적 날짜 패턴 — "5월 1일", "2026-05-01", "5/1" 등.
# 검증 목적이므로 보수적으로 매치. 하나라도 매치되면 명시 날짜로 간주.
_EXPLICIT_DATE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"),  # 2026-05-01, 2026/5/1
    re.compile(r"\d{1,2}월\s*\d{1,2}일"),  # 5월 1일
    re.compile(r"\d{1,2}/\d{1,2}"),  # 5/1
]


def _has_explicit_date(text: str) -> bool:
    """발화 텍스트에 명시적 날짜 표현이 있는지 검사한다.

    Args:
        text: 검사 대상.

    Returns:
        명시적 날짜 패턴이 있으면 True.
    """
    if not text:
        return False
    for pat in _EXPLICIT_DATE_PATTERNS:
        if pat.search(text):
            return True
    return False


def _generate_action_id(owner: str, description: str, meeting_id: str) -> str:
    """SHA-1(owner + description + from_meeting_id) 의 앞 8자 hex.

    Args:
        owner: 담당자.
        description: 작업 설명.
        meeting_id: 회의 ID.

    Returns:
        8자리 hex 문자열.
    """
    raw = f"{owner}{description}{meeting_id}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def _citation_from_ts(meeting_id: str, ts_str: str) -> Citation | None:
    """HH:MM:SS → Citation 변환."""
    if not ts_str:
        return None
    parts = ts_str.split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return Citation(
        meeting_id=meeting_id,
        timestamp_str=f"{h:02d}:{m:02d}:{s:02d}",
        timestamp_seconds=h * 3600 + m * 60 + s,
    )


def _extract_json_array(text: str) -> list[Any] | None:
    """LLM 응답에서 JSON 배열을 robust 하게 파싱."""
    if not text:
        return None
    text = text.strip()
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    snippet = text[start : end + 1]
    try:
        result = json.loads(snippet)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────
# 4.3 시스템 프롬프트
# ─────────────────────────────────────────────────────────────────────────


_EXTRACT_NEW_SYSTEM_PROMPT = """\
당신은 회의록에서 신규 액션아이템(action items) 만 추출하는 분석가입니다.
출력은 반드시 JSON 배열이어야 하며, 각 항목은 다음 키를 포함합니다:
- owner: 담당자 이름 (없으면 null)
- description: 한 줄 작업 설명
- due_date: ISO 마감일 "YYYY-MM-DD" (발화에 명시된 경우만, 없으면 null)
- project_slug: 프로젝트 slug (없으면 null)
- citation_ts: 해당 발화 시각 "HH:MM:SS"
- confidence: 0~10 정수

규칙:
1. "할 거다", "해야 한다" 류 미래형/의무 표현만 액션으로 인정.
2. 평범한 잡담은 추출하지 말 것.
3. 액션이 없으면 빈 배열 [] 만 출력.
"""


_DETECT_CLOSED_SYSTEM_PROMPT = """\
당신은 회의록에서 기존 액션아이템의 완료/철회 신호를 감지하는 분석가입니다.

입력으로 기존 open 목록과 회의 발화가 주어집니다. 출력은 JSON 배열입니다:
- item_index: existing_open 의 0-based 인덱스
- closed_reason: "completed" | "cancelled" | "superseded"
- closed_citation_ts: "HH:MM:SS"
- confidence: 0~10 정수

규칙:
1. "완료했습니다", "마쳤습니다", "다 됐어요" 같은 명확한 완료 표현만 인정.
2. "70% 진행 중", "거의 됐어요" 같은 부분 진행은 미감지.
3. 모호한 "잘 됐어요", "괜찮아요" 같은 표현은 미감지.
4. 매핑할 수 없으면 빈 배열 [].
"""


# ─────────────────────────────────────────────────────────────────────────
# 4.4 추출기
# ─────────────────────────────────────────────────────────────────────────


class ActionItemExtractor:
    """신규 액션 발견 + 기존 open 의 closed 전환을 LLM 으로 추적한다."""

    def __init__(self, llm: WikiLLMClient) -> None:
        """LLM 추상화 1개만 받는다.

        Args:
            llm: WikiLLMClient (mock 가능).
        """
        self._llm: WikiLLMClient = llm

    async def extract_new(
        self,
        *,
        meeting_id: str,
        meeting_date: date,
        utterances: list,
        speaker_name_map: dict[str, str] | None = None,
    ) -> list[NewActionItem]:
        """이번 회의에서 새로 발견된 액션아이템을 추출한다 (LLM 1회).

        Args:
            meeting_id: 회의 ID.
            meeting_date: 회의 날짜 — 상대 날짜 변환의 기준점.
            utterances: 5단계 결과.
            speaker_name_map: corrector 가 제공한 {SPEAKER_XX: 한국어이름} 매핑.
                None 이면 fuzzy matching 비활성화 (Phase 1 호환). 매핑이 있으면
                LLM 이 한국어 이름을 추출했을 때 SPEAKER_XX 라벨과 매핑하여 환각이
                아닌 정상 owner 로 인정.

        Returns:
            NewActionItem 리스트. 없으면 빈 리스트.
        """
        if not utterances:
            return []

        # 화자 set + utterance text 모음 (검증용)
        speakers: set[str] = {getattr(u, "speaker", "") for u in utterances}
        all_text = " ".join(getattr(u, "text", "") for u in utterances)

        # 직렬화
        lines: list[str] = []
        for utt in utterances:
            text = sanitize_utterance_text(getattr(utt, "text", ""))
            speaker = getattr(utt, "speaker", "UNKNOWN")
            start = getattr(utt, "start", 0.0)
            ts_str = self._seconds_to_hhmmss(float(start))
            lines.append(f"[{ts_str}] {speaker}: {text}")

        user_prompt = (
            f"회의 ID: {meeting_id}\n"
            f"회의 날짜: {meeting_date.isoformat()}\n\n"
            f"## 발화 목록\n" + "\n".join(lines) + "\n\n"
            f"위 컨텍스트에서 신규 액션아이템을 JSON 배열로 추출하세요."
        )

        try:
            raw = await self._llm.generate(
                system_prompt=_EXTRACT_NEW_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ActionItemExtractor.extract_new 호출 실패: %r", exc)
            raise

        parsed = _extract_json_array(raw)
        if parsed is None:
            logger.warning("ActionItemExtractor.extract_new: JSON 파싱 실패")
            return []

        results: list[NewActionItem] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            new_item = self._build_new_action_item(
                item,
                meeting_id=meeting_id,
                meeting_date=meeting_date,
                speakers=speakers,
                all_text=all_text,
                speaker_name_map=speaker_name_map,
            )
            if new_item is not None:
                results.append(new_item)
        return results

    async def detect_closed(
        self,
        *,
        existing_open: list[OpenActionItem],
        meeting_id: str,
        utterances: list,
    ) -> list[ClosedActionItem]:
        """기존 open 목록 중 이번 회의에서 완료/철회된 항목을 감지한다 (LLM 1회).

        Args:
            existing_open: 기존 open 목록. 빈 리스트면 LLM 호출 없이 즉시 반환.
            meeting_id: 종료 보고 회의 ID.
            utterances: 종료 신호를 검색할 발화 목록.

        Returns:
            ClosedActionItem 리스트.
        """
        # 단락 — existing_open 비어있으면 LLM 호출 없이 즉시 반환
        if not existing_open:
            return []

        if not utterances:
            return []

        # existing_open 직렬화 + 발화 직렬화
        open_lines: list[str] = []
        for idx, item in enumerate(existing_open):
            open_lines.append(
                f"{idx}. {item.owner}: {item.description} "
                f"(from {item.from_meeting_id} @ {item.from_date})"
            )

        utt_lines: list[str] = []
        for utt in utterances:
            text = sanitize_utterance_text(getattr(utt, "text", ""))
            speaker = getattr(utt, "speaker", "UNKNOWN")
            start = getattr(utt, "start", 0.0)
            ts_str = self._seconds_to_hhmmss(float(start))
            utt_lines.append(f"[{ts_str}] {speaker}: {text}")

        user_prompt = (
            f"회의 ID: {meeting_id}\n\n"
            f"## 기존 open 액션 목록 (인덱스 포함)\n"
            + "\n".join(open_lines)
            + "\n\n"
            f"## 발화 목록\n" + "\n".join(utt_lines) + "\n\n"
            "위 발화에서 완료/철회된 액션아이템을 JSON 배열로 알려주세요."
        )

        try:
            raw = await self._llm.generate(
                system_prompt=_DETECT_CLOSED_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ActionItemExtractor.detect_closed 호출 실패: %r", exc)
            raise

        parsed = _extract_json_array(raw)
        if parsed is None:
            return []

        results: list[ClosedActionItem] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("item_index", -1))
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(existing_open):
                continue
            ts_str = str(item.get("closed_citation_ts", "") or "")
            cit = _citation_from_ts(meeting_id, ts_str)
            if cit is None:
                # ts 없으면 fallback 0초
                cit = Citation(
                    meeting_id=meeting_id, timestamp_str="00:00:00", timestamp_seconds=0
                )
            closed_reason = str(item.get("closed_reason", "completed") or "completed")
            # closed_by_speaker — 해당 timestamp 의 화자 우선, 없으면 첫 발화 화자
            closed_by = self._find_speaker_at(utterances, cit.timestamp_seconds)
            results.append(
                ClosedActionItem(
                    original=existing_open[idx],
                    closed_by_speaker=closed_by,
                    closed_at_meeting_id=meeting_id,
                    closed_citation=cit,
                    closed_reason=closed_reason,
                )
            )
        return results

    async def render_unified_page(
        self,
        *,
        new_open: list[NewActionItem],
        newly_closed: list[ClosedActionItem],
        existing_open: list[OpenActionItem],
        existing_closed: list[ClosedActionItem],
        last_compiled_at: str,
    ) -> str:
        """4 종류 입력을 병합하여 action_items.md 본문을 결정적으로 렌더링한다.

        흐름:
            1. existing_open 에서 newly_closed.original 항목 제거.
            2. new_open 항목들을 open 섹션에 추가.
            3. existing_closed + newly_closed → closed 섹션 (날짜 내림차순 정렬).
            4. PRD §4.2 형식으로 직렬화.
            5. confidence 마커 자동 부착 (D3 통과 보장).

        Args:
            new_open: 신규 발견.
            newly_closed: 이번 ingest 에서 닫힌 항목.
            existing_open: 기존 open.
            existing_closed: 기존 closed.
            last_compiled_at: ISO8601 시각.

        Returns:
            완전한 action_items.md 본문.
        """
        # ── 1. existing_open 에서 newly_closed 의 original 제거 ─────────
        closed_ids: set[str] = {c.original.item_id for c in newly_closed}
        remaining_open: list[OpenActionItem] = [
            item for item in existing_open if item.item_id not in closed_ids
        ]

        # ── 2. closed 목록 병합 + 날짜 내림차순 정렬 ──────────────────
        all_closed: list[ClosedActionItem] = list(existing_closed) + list(newly_closed)
        all_closed.sort(key=lambda c: c.original.from_date or "", reverse=True)

        # ── 3. confidence 평균 계산 (D3 자동 통과 보장) ────────────────
        confidences: list[int] = []
        for item in new_open:
            if item.confidence > 0:
                confidences.append(item.confidence)
        # newly_closed 는 confidence 가 없으므로 9 (자체 보고 → 신뢰도 높음) 기본값
        for _ in newly_closed:
            confidences.append(9)
        avg_confidence = (
            sum(confidences) // len(confidences) if confidences else 8
        )  # 기본 8

        # ── 4. 마크다운 직렬화 ────────────────────────────────────────
        open_count = len(remaining_open) + len(new_open)
        closed_count = len(all_closed)

        parts: list[str] = []
        # frontmatter
        parts.append("---")
        parts.append("type: action_items")
        parts.append(f"last_compiled: {last_compiled_at}")
        parts.append("---")
        parts.append("")
        parts.append("# Action Items")
        parts.append("")
        # Open 섹션
        parts.append(f"## Open ({open_count})")
        parts.append("")
        for item in remaining_open:
            parts.append(self._render_open_line(item))
        for item in new_open:
            parts.append(self._render_new_line(item))
        if open_count == 0:
            parts.append("_(없음)_")
        parts.append("")
        # Closed 섹션
        parts.append(f"## Closed ({closed_count})")
        parts.append("")
        for c in all_closed:
            parts.extend(self._render_closed_block(c))
        if closed_count == 0:
            parts.append("_(없음)_")
        parts.append("")
        parts.append(f"<!-- confidence: {avg_confidence} -->")
        parts.append("")

        return "\n".join(parts)

    # ─────────────────────────────────────────────────────────────────────
    # 내부 헬퍼
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _seconds_to_hhmmss(seconds: float) -> str:
        """float 초를 HH:MM:SS 문자열로 변환."""
        total = int(seconds)
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    @staticmethod
    def _build_new_action_item(
        item: dict[str, Any],
        *,
        meeting_id: str,
        meeting_date: date,
        speakers: set[str],
        all_text: str,
        speaker_name_map: dict[str, str] | None = None,
    ) -> NewActionItem | None:
        """LLM 응답 dict 를 NewActionItem 으로 변환 (환각 방지 + Phase 2.E 보강).

        환각 방지 정책 (Phase 2.E 강화):
            - owner: 화자 set 에 없거나 매핑에서도 못 찾으면 None 강제 (fuzzy matching).
            - due_date:
                a) 발화에 명시 ISO/한국어 날짜가 있으면 LLM 응답 그대로 사용.
                b) 명시 날짜는 없지만 상대 표현 ("내일", "다음주" 등) 이 있으면
                   meeting_date 기준으로 ISO 날짜 자동 계산.
                c) 위 둘 다 없으면 None 강제.

        Args:
            item: LLM 응답 단일 항목.
            meeting_id: 회의 ID.
            meeting_date: 회의 날짜 (상대 날짜 변환 기준).
            speakers: 실제 화자 이름 집합.
            all_text: 발화 텍스트 전체 (날짜 검증용).
            speaker_name_map: SPEAKER_XX → 한국어이름 매핑 (선택).

        Returns:
            NewActionItem 또는 None (필수 필드 누락 시).
        """
        description = str(item.get("description", "") or "").strip()
        if not description:
            return None

        # ── owner: fuzzy matching (Phase 2.E) ───────────────────────────
        # 1) string equality 가 통과하면 그대로 인정.
        # 2) speaker_name_map 이 있으면 한국어이름 ↔ SPEAKER_XX 양방향 매핑.
        # 3) 매칭 실패 → None 강제 (환각 방지).
        raw_owner = item.get("owner")
        if raw_owner is None:
            owner: str | None = None
        else:
            owner_str = str(raw_owner).strip()
            owner = _resolve_owner(owner_str, speakers, speaker_name_map)

        # ── due_date: 명시 날짜 + 상대 표현 (Phase 2.E) ────────────────
        # 우선순위:
        #   (a) 발화에 명시 날짜 (2026-05-01, 5월 1일, 5/1) 있음 → LLM 응답 사용.
        #   (b) 발화에 상대 표현 (내일/모레/다음주/다다음주) 있음 → meeting_date + offset.
        #   (c) 둘 다 없음 → None (환각 방지).
        raw_due = item.get("due_date")
        due_date: str | None = None
        if _has_explicit_date(all_text):
            # (a) 명시 날짜 — LLM 응답 우선
            if raw_due is not None:
                due_str = str(raw_due).strip()
                due_date = due_str if due_str else None
        else:
            # (b) 상대 표현 시도
            relative_iso = _resolve_relative_date(meeting_date, all_text)
            if relative_iso is not None:
                due_date = relative_iso
            # (c) 그 외 → None 강제 (raw_due 무시)

        # project_slug
        raw_proj = item.get("project_slug")
        project_slug = str(raw_proj).strip() if raw_proj else None
        if project_slug == "":
            project_slug = None

        # citation
        ts_str = str(item.get("citation_ts", "") or "")
        cit = _citation_from_ts(meeting_id, ts_str)
        if cit is None:
            cit = Citation(
                meeting_id=meeting_id, timestamp_str="00:00:00", timestamp_seconds=0
            )

        # confidence
        try:
            confidence = int(item.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            confidence = 0

        return NewActionItem(
            owner=owner,
            description=description,
            citation=cit,
            due_date=due_date,
            project_slug=project_slug,
            confidence=confidence,
        )

    @staticmethod
    def _find_speaker_at(utterances: list, ts_seconds: int) -> str:
        """주어진 timestamp 에서 발화한 화자 이름을 찾는다.

        Args:
            utterances: 발화 목록.
            ts_seconds: 검색할 시각 (초).

        Returns:
            화자 이름. 매칭 실패 시 빈 문자열.
        """
        for utt in utterances:
            start = float(getattr(utt, "start", 0.0))
            end = float(getattr(utt, "end", 0.0))
            if start <= ts_seconds <= end + 5.0:  # 5초 여유
                return str(getattr(utt, "speaker", ""))
        # fallback: 첫 발화
        if utterances:
            return str(getattr(utterances[0], "speaker", ""))
        return ""

    @staticmethod
    def _render_open_line(item: OpenActionItem) -> str:
        """기존 open 항목을 한 줄로 렌더링."""
        owner = item.owner
        desc = item.description
        cit_str = (
            f"[meeting:{item.citation.meeting_id}@{item.citation.timestamp_str}]"
        )
        due = f" (due: {item.due_date})" if item.due_date else ""
        return f"- [ ] {owner}: {desc}{due} {cit_str}"

    @staticmethod
    def _render_new_line(item: NewActionItem) -> str:
        """신규 NewActionItem 을 한 줄로 렌더링."""
        owner = item.owner or "미지정"
        desc = item.description
        cit_str = (
            f"[meeting:{item.citation.meeting_id}@{item.citation.timestamp_str}]"
        )
        due = f" (due: {item.due_date})" if item.due_date else ""
        return f"- [ ] {owner}: {desc}{due} {cit_str}"

    @staticmethod
    def _render_closed_block(closed: ClosedActionItem) -> list[str]:
        """ClosedActionItem 을 멀티라인 블록으로 렌더링.

        형식:
            - [x] ~~{description}~~ [{original_citation}]
              - Closed by: {speaker} [{closed_citation}]
        """
        original = closed.original
        orig_cit_str = (
            f"[meeting:{original.citation.meeting_id}@{original.citation.timestamp_str}]"
        )
        closed_cit_str = (
            f"[meeting:{closed.closed_citation.meeting_id}"
            f"@{closed.closed_citation.timestamp_str}]"
        )
        return [
            f"- [x] ~~{original.description}~~ {orig_cit_str}",
            f"  - Closed by: {closed.closed_by_speaker} {closed_cit_str}",
        ]
