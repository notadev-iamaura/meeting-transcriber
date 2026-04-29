"""PersonExtractor — 회의 발화에서 인물 정보를 추출하고 people/{name}.md 페이지를
누적 갱신한다. PRD §4.2 의 people 템플릿을 정확히 준수한다.

핵심 차별점 (decisions 와의 차이):
    1. 누적성 — 동일 인물이 여러 회의에 등장하면 last_seen / meetings_count 가
       단조 증가한다.
    2. derived 섹션 — "최근 결정", "담당 프로젝트", "미해결 액션아이템" 은
       Phase 2 결과를 입력으로 받아 LLM 호출 없이 자동 생성한다. LLM 은 role
       추론 + "자주 언급하는 주제" 두 섹션만 담당한다.
    3. 슬러그 정책 — name_normalized 는 한글 그대로 사용 (filename-safe).
       공백·슬래시·`..` 만 제거한다. URL 인코딩은 server 측 책임.

의존성:
    - core.wiki.llm_client.WikiLLMClient, sanitize_utterance_text
    - core.wiki.models.Citation
    - core.wiki.extractors.decision.ExtractedDecision (derived 섹션 입력)
    - core.wiki.extractors.action_item.{NewActionItem, OpenActionItem}
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from core.wiki.extractors.action_item import NewActionItem, OpenActionItem
from core.wiki.extractors.decision import ExtractedDecision, _strip_paren_latin
from core.wiki.llm_client import WikiLLMClient, sanitize_utterance_text
from core.wiki.models import Citation

logger = logging.getLogger(__name__)


# 회의당 갱신 상한 — decisions 와 동일 (PRD R3).
_MAX_PERSONS_PER_MEETING: int = 8

# people 페이지의 derived 섹션에 노출되는 최근 결정/액션 개수 상한.
_MAX_RECENT_DECISIONS_IN_PAGE: int = 5
_MAX_OPEN_ACTIONS_IN_PAGE: int = 10


# ─────────────────────────────────────────────────────────────────────────
# 1.1 Utterance Protocol — Phase 2 와 동일한 duck-typing 계약 명시
# ─────────────────────────────────────────────────────────────────────────


class Utterance(Protocol):
    """corrector 단계의 발화 표현. 직접 import 회피용 Protocol."""

    speaker: str
    text: str
    start: float
    end: float


# ─────────────────────────────────────────────────────────────────────────
# 1.2 데이터 모델
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TopicMention:
    """인물이 자주 언급한 주제 1건.

    Attributes:
        topic: 한 줄 요약 (예: "pricing-strategy").
        citation: 대표 발언 인용 (LLM 이 가장 명확한 1건 선택).
    """

    topic: str
    citation: Citation


@dataclass
class ExtractedPerson:
    """LLM 추출 결과 — 1차 가공 단계.

    Attributes:
        name: 한국어 이름 (또는 영문). 페이지 제목으로 사용.
        name_normalized: 파일명 안전 식별자. 한글 그대로 사용 가능.
        role: PM/Eng Lead/Designer 등. 추론 실패 시 None.
        first_seen_meeting_id: 첫 등장 회의 ID.
        first_seen_date: ISO 날짜 문자열.
        last_seen_meeting_id: 가장 최근 회의 ID.
        last_seen_date: ISO 날짜 문자열.
        topic_mentions: "자주 언급하는 주제" 섹션에 들어갈 항목들.
        citations: name 추론 근거가 된 발화 인용.
        confidence: LLM 자체 평가 0~10.
    """

    name: str
    name_normalized: str
    role: str | None
    first_seen_meeting_id: str
    first_seen_date: str
    last_seen_meeting_id: str
    last_seen_date: str
    topic_mentions: list[TopicMention] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    confidence: int = 0


@dataclass(frozen=True)
class ExistingPersonState:
    """기존 people/{name}.md 의 frontmatter + 본문에서 파싱한 정보.

    Attributes:
        rel_path: people/{name_normalized}.md 상대 경로.
        name: frontmatter 의 name.
        role: frontmatter 의 role (없으면 None).
        first_seen_date: frontmatter 의 first_seen.
        last_seen_date: frontmatter 의 last_seen.
        meetings_count: frontmatter 의 meetings_count.
        seen_meeting_ids: 본문에 등장한 모든 [meeting:id@HH:MM:SS] 의 id 집합.
        existing_topics: 기존 "자주 언급하는 주제" 섹션의 주제 문자열 set.
        raw_content: 페이지 전체 raw text.
    """

    rel_path: Path
    name: str
    role: str | None
    first_seen_date: str
    last_seen_date: str
    meetings_count: int
    seen_meeting_ids: frozenset[str]
    existing_topics: frozenset[str]
    raw_content: str


# ─────────────────────────────────────────────────────────────────────────
# 1.3 헬퍼 (decision.py 의 동일 helper 와 정책 일치)
# ─────────────────────────────────────────────────────────────────────────


# 인용 마커 패턴 — citations.CITATION_PATTERN 과 동일.
_CITATION_PATTERN: re.Pattern[str] = re.compile(
    r"\[meeting:([a-f0-9]{8})@(\d{2}):(\d{2}):(\d{2})\]"
)

# person slug 에 허용되는 문자 — 한글/영숫자/하이픈/언더스코어.
_PERSON_SLUG_ALLOWED: re.Pattern[str] = re.compile(
    r"^[\uAC00-\uD7A3A-Za-z0-9\-_]+$"
)


def _citation_from_ts(meeting_id: str, ts_str: str) -> Citation | None:
    """HH:MM:SS 형태의 timestamp 를 Citation 으로 변환한다.

    Args:
        meeting_id: 8자리 hex.
        ts_str: "HH:MM:SS" 문자열.

    Returns:
        Citation 인스턴스. 형식 불량 시 None.
    """
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
    """LLM 응답에서 JSON 배열을 robust 하게 파싱한다.

    동작:
        1. 전체 텍스트로 json.loads 시도.
        2. 실패 시 첫 `[` 와 마지막 `]` 사이를 추출하여 재시도.

    Args:
        text: LLM 원시 응답.

    Returns:
        파싱된 리스트 또는 None (실패 시).
    """
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


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """LLM 응답에서 JSON 객체를 robust 하게 파싱한다.

    동작:
        1. 전체 텍스트로 json.loads 시도.
        2. 실패 시 첫 `{` 와 마지막 `}` 사이를 추출하여 재시도.

    Args:
        text: LLM 원시 응답.

    Returns:
        파싱된 dict 또는 None.
    """
    if not text:
        return None
    text = text.strip()
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    snippet = text[start : end + 1]
    try:
        result = json.loads(snippet)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────
# 1.4 시스템 프롬프트
# ─────────────────────────────────────────────────────────────────────────


_EXTRACT_SYSTEM_PROMPT = """\
당신은 회의록에서 인물(people) 정보를 추출하는 분석가입니다.
주의: 결정/액션은 추출하지 마세요 (별도 모듈 책임).

각 발화 화자에 대해 다음 정보를 JSON 배열로 출력합니다:
- name: 화자 이름 (speaker_name_map 으로 정규화된 한국어 이름 우선)
- role: 추론 가능하면 PM/Eng Lead/Designer 등 (불확실하면 null)
- topic_mentions: [{topic, citation_ts}, ...] — 명확한 주제 (3개 이하). 잡담 제외.
- citation_ts: 화자 동일성 근거 발언 1건의 "HH:MM:SS"
- confidence: 0~10 정수

규칙:
1. SPEAKER_XX 화자라도 speaker_name_map 에 매핑이 있으면 한국어 이름 사용.
2. 매핑 없고 한국어 이름 추론도 불확실하면 추출하지 않음.
3. 한국어 고유명사 외국어 병기 금지.
4. 식별 불가하면 빈 배열 [].
"""


_ROLE_TOPIC_SYSTEM_PROMPT = """\
당신은 기존 인물 페이지에 누적될 추가 정보를 식별합니다.

입력:
- existing_role: 기존 frontmatter 의 role (있으면)
- existing_topics: 기존 "자주 언급하는 주제" 목록
- new_utterances: 이번 회의의 해당 화자 발화 모음

출력 JSON 객체:
{
  "role_update": null | "PM" | "Eng Lead" | ...,
  "new_topics": [{"topic": "...", "citation_ts": "HH:MM:SS"}, ...]
}

규칙:
1. role 은 정말 명확할 때만 변경. 모호하면 null.
2. new_topics 는 existing_topics 에 없는 것만.
3. 모든 topic 에는 정확한 citation_ts 필수.
"""


# ─────────────────────────────────────────────────────────────────────────
# 1.5 추출기
# ─────────────────────────────────────────────────────────────────────────


class PersonExtractor:
    """회의 발화에서 인물 정보 추출 + people/{name}.md 페이지 갱신.

    Threading: 단일 코루틴 가정. 페이지별 LLM 호출은 asyncio.gather 로 병렬.
    """

    def __init__(self, llm: WikiLLMClient) -> None:
        """LLM 추상화 1개만 받는다.

        Args:
            llm: WikiLLMClient (실구현 또는 mock).
        """
        self._llm: WikiLLMClient = llm

    async def extract_speakers(
        self,
        *,
        meeting_id: str,
        meeting_date: date,
        utterances: list,
        speaker_name_map: dict[str, str] | None = None,
    ) -> list[ExtractedPerson]:
        """발화 + 화자 매핑 → 인물별 정보 추출 (LLM 1회).

        Args:
            meeting_id: 회의 ID.
            meeting_date: 회의 날짜 (first_seen / last_seen 갱신용).
            utterances: 5단계 corrector 결과.
            speaker_name_map: corrector 가 제공한 {SPEAKER_XX: 한국어이름}.

        Returns:
            ExtractedPerson 리스트. 식별 실패 시 빈 리스트.
        """
        # 빈 utterances → LLM 호출 없이 즉시 빈 리스트
        if not utterances:
            return []

        # 환각 방지용 화자 집합 — action_item.py 의 _resolve_owner 와 동일 패턴
        raw_speakers: set[str] = {getattr(u, "speaker", "") for u in utterances}
        # speaker_name_map 의 값(한국어이름)도 허용 집합에 포함
        allowed_names: set[str] = set(raw_speakers)
        if speaker_name_map:
            allowed_names.update(speaker_name_map.values())

        # utterances 직렬화
        lines: list[str] = []
        for utt in utterances:
            text = sanitize_utterance_text(getattr(utt, "text", ""))
            speaker = getattr(utt, "speaker", "UNKNOWN")
            start = getattr(utt, "start", 0.0)
            ts_str = self._seconds_to_hhmmss(float(start))
            lines.append(f"[{ts_str}] {speaker}: {text}")

        # speaker_name_map 컨텍스트 직렬화
        map_text = ""
        if speaker_name_map:
            pairs = ", ".join(f"{k}={v}" for k, v in speaker_name_map.items())
            map_text = f"## 화자 이름 매핑\n{pairs}\n\n"

        user_prompt = (
            f"회의 ID: {meeting_id}\n"
            f"회의 날짜: {meeting_date.isoformat()}\n\n"
            f"{map_text}"
            f"## 발화 목록\n" + "\n".join(lines) + "\n\n"
            f"위 컨텍스트에서 인물 정보를 JSON 배열로 추출하세요."
        )

        try:
            raw = await self._llm.generate(
                system_prompt=_EXTRACT_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("PersonExtractor.extract_speakers 1차 호출 실패: %r", exc)
            return []

        parsed = _extract_json_array(raw)

        # 2차 재시도 — 파싱 실패 시
        if parsed is None:
            logger.warning("PersonExtractor: 1차 JSON 파싱 실패, 1회 재시도")
            try:
                raw = await self._llm.generate(
                    system_prompt=_EXTRACT_SYSTEM_PROMPT,
                    user_prompt=user_prompt + "\n\n반드시 JSON 배열만 출력하세요.",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("PersonExtractor 재시도 실패: %r", exc)
                return []
            parsed = _extract_json_array(raw)

        if parsed is None:
            return []

        results: list[ExtractedPerson] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                person = self._build_extracted_person(
                    item,
                    meeting_id=meeting_id,
                    meeting_date=meeting_date,
                    speaker_name_map=speaker_name_map,
                    allowed_names=allowed_names,
                )
            except Exception as exc:  # noqa: BLE001 — 항목별 실패는 skip
                logger.warning("PersonExtractor: 항목 변환 실패 — skip: %r", exc)
                continue
            if person is not None:
                results.append(person)
        return results

    async def render_or_update_pages(
        self,
        *,
        persons: list[ExtractedPerson],
        meeting_id: str,
        meeting_date: date,
        existing_store: Any,
        meeting_decisions: list[ExtractedDecision],
        meeting_new_actions: list[NewActionItem],
        existing_open_actions: list[OpenActionItem],
    ) -> list[tuple[str, str, int]]:
        """기존 페이지가 있으면 갱신, 없으면 신규 생성.

        Phase 2 의 decisions / action_items 추출 결과를 받아 derived 섹션을
        자동으로 채운다. LLM 은 role 추론 + 자주 언급 주제 추출만 담당.

        Args:
            persons: extract_speakers() 결과.
            meeting_id: 인용 검증 + meetings_count 증가 결정용.
            meeting_date: last_seen 업데이트 기준.
            existing_store: WikiStore (read_page 만 사용).
            meeting_decisions: 동일 회의의 DecisionExtractor.extract() 결과.
            meeting_new_actions: 동일 회의의 ActionItemExtractor.extract_new() 결과.
            existing_open_actions: 기존 action_items.md 의 Open 목록.

        Returns:
            [(rel_path, new_content, confidence), ...]. confidence 내림차순,
            최대 _MAX_PERSONS_PER_MEETING 건.
        """
        if not persons:
            return []

        # confidence 내림차순 정렬 + 상위 N건 cap
        sorted_persons = sorted(persons, key=lambda p: p.confidence, reverse=True)
        capped = sorted_persons[:_MAX_PERSONS_PER_MEETING]
        if len(persons) > _MAX_PERSONS_PER_MEETING:
            logger.info(
                "PersonExtractor.render_or_update_pages: %d건 중 상위 %d건만 처리 (R3 상한)",
                len(persons),
                _MAX_PERSONS_PER_MEETING,
            )

        async def _render_one(
            person: ExtractedPerson,
        ) -> tuple[str, str, int] | None:
            """단일 person → (rel_path, content, confidence)."""
            rel_path = f"people/{person.name_normalized}.md"

            # 기존 페이지 확인
            existing_state: ExistingPersonState | None = None
            existing_raw = self._read_existing(existing_store, rel_path)
            if existing_raw:
                existing_state = self._parse_existing_person_state(
                    existing_raw, Path(rel_path)
                )

            # LLM 호출 — role/topic 보강
            try:
                raw_response = await self._llm.generate(
                    system_prompt=_ROLE_TOPIC_SYSTEM_PROMPT,
                    user_prompt=self._build_role_topic_prompt(
                        person=person, existing=existing_state
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — 페이지별 실패는 skip
                logger.warning(
                    "PersonExtractor LLM 호출 실패 — skip: path=%s, %r",
                    rel_path,
                    exc,
                )
                return None

            llm_obj = _extract_json_object(raw_response) or {}
            role_update = llm_obj.get("role_update")
            if role_update is not None and not isinstance(role_update, str):
                role_update = None
            # frontmatter 인젝션 방어 — role 에 개행/콜론이 있으면 제거
            # LLM 이 "admin: true\nrole: PM" 같은 값을 삽입하는 시도 차단
            if isinstance(role_update, str):
                role_update = role_update.replace("\n", " ").replace(":", "").strip()
                if not role_update:
                    role_update = None
            new_topics_raw = llm_obj.get("new_topics") or []

            # 최종 role 결정 — 기존 role 보존이 기본, role_update 가 있고 의미있으면 갱신
            if existing_state is not None:
                final_role = existing_state.role
                if (
                    role_update
                    and role_update.strip()
                    and role_update.strip() != (final_role or "")
                ):
                    final_role = role_update.strip()
                final_first_seen = existing_state.first_seen_date
                final_meetings_count = existing_state.meetings_count
                if meeting_id not in existing_state.seen_meeting_ids:
                    final_meetings_count += 1
                existing_topics = existing_state.existing_topics
            else:
                final_role = (
                    role_update.strip()
                    if isinstance(role_update, str) and role_update.strip()
                    else person.role
                )
                final_first_seen = person.first_seen_date
                final_meetings_count = 1
                existing_topics = frozenset()

            # topic 병합 — LLM 의 new_topics + person.topic_mentions, 기존 topic 과 중복 제거
            merged_topics: list[tuple[str, Citation | None]] = []
            seen_topic_strs: set[str] = set(existing_topics)

            for tm in person.topic_mentions:
                if tm.topic and tm.topic not in seen_topic_strs:
                    merged_topics.append((tm.topic, tm.citation))
                    seen_topic_strs.add(tm.topic)

            for nt in new_topics_raw:
                if not isinstance(nt, dict):
                    continue
                topic_str = str(nt.get("topic", "") or "").strip()
                if not topic_str or topic_str in seen_topic_strs:
                    continue
                ts_str = str(nt.get("citation_ts", "") or "")
                cit = _citation_from_ts(meeting_id, ts_str)
                merged_topics.append((topic_str, cit))
                seen_topic_strs.add(topic_str)

            # derived 섹션 — LLM 비호출
            recent_decisions_lines = self._build_derived_recent_decisions(
                person_name=person.name,
                meeting_decisions=meeting_decisions,
                meeting_id=meeting_id,
                meeting_date=meeting_date,
                existing_raw=existing_raw,
            )
            project_lines = self._build_derived_projects(
                person_name=person.name,
                meeting_decisions=meeting_decisions,
                meeting_new_actions=meeting_new_actions,
            )
            open_action_lines = self._build_derived_open_actions(
                person_name=person.name,
                new_actions=meeting_new_actions,
                existing_open_actions=existing_open_actions,
                meeting_id=meeting_id,
            )

            # 페이지 본문 합성
            content = self._compose_person_page(
                name=person.name,
                name_normalized=person.name_normalized,
                role=final_role,
                first_seen_date=final_first_seen,
                last_seen_date=meeting_date.isoformat(),
                meetings_count=final_meetings_count,
                merged_topics=merged_topics,
                recent_decisions_lines=recent_decisions_lines,
                project_lines=project_lines,
                open_action_lines=open_action_lines,
                confidence=person.confidence,
                meeting_id=meeting_id,
            )
            content = _strip_paren_latin(content)

            return (rel_path, content, person.confidence)

        rendered = await asyncio.gather(*[_render_one(p) for p in capped])
        results: list[tuple[str, str, int]] = [r for r in rendered if r is not None]
        return results

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
    def _normalize_person_slug(raw: str) -> str:
        """인물 이름 → filename-safe 식별자.

        한글 보존 정책: 한글/영숫자/하이픈/언더스코어만 허용. 공백 → 언더스코어.
        `..` / 슬래시 / NUL 거부 (path traversal 방어).

        예시:
            "철수"        → "철수"
            "John Doe"    → "John_Doe"
            "박 PM"       → "박_PM"
            "../etc/pwd"  → ValueError
        """
        if not raw or not raw.strip():
            raise ValueError("person slug 가 빈 문자열입니다")
        s = raw.strip()
        # path traversal 방어 — 슬래시 / `..` / NUL 거부
        if ".." in s or "/" in s or "\\" in s or "\x00" in s:
            raise ValueError(
                f"path traversal 시도가 감지되었습니다: {raw!r}"
            )
        # 공백은 언더스코어로 치환
        s = re.sub(r"\s+", "_", s)
        # 허용 문자만 검증
        if not _PERSON_SLUG_ALLOWED.match(s):
            raise ValueError(
                f"허용되지 않는 문자가 포함된 person slug 입니다: {raw!r}"
            )
        # 슬러그 길이 상한 — 파일시스템 안전 + ReDoS 방지
        if len(s) > 64:
            raise ValueError(
                f"person slug 가 64자 초과입니다 ({len(s)}자): {s[:32]!r}..."
            )
        return s

    @staticmethod
    def _parse_existing_person_state(
        page_content: str, rel_path: Path
    ) -> ExistingPersonState | None:
        """기존 페이지 본문을 ExistingPersonState 로 lift.

        store.py 의 _parse_frontmatter 를 lazy import 로 사용.

        Args:
            page_content: 페이지 raw text (frontmatter 포함).
            rel_path: 상대 경로.

        Returns:
            ExistingPersonState 인스턴스 또는 None (파싱 실패 시).
        """
        try:
            from core.wiki.store import _parse_frontmatter  # noqa: PLC0415
        except ImportError:
            return None

        try:
            fm, body = _parse_frontmatter(page_content)
        except Exception as exc:  # noqa: BLE001 — frontmatter 파싱 실패는 보수적 폴백
            logger.warning("ExistingPersonState 파싱 실패: %r", exc)
            return None

        name = str(fm.get("name", "") or "")
        role_raw = fm.get("role")
        role: str | None = (
            str(role_raw) if role_raw is not None and str(role_raw).strip() else None
        )
        first_seen = str(fm.get("first_seen", "") or "")
        last_seen = str(fm.get("last_seen", "") or "")
        try:
            meetings_count = int(fm.get("meetings_count", 0) or 0)
        except (TypeError, ValueError):
            meetings_count = 0

        # 본문에서 등장한 meeting_id 집합
        seen_ids: set[str] = set()
        for match in _CITATION_PATTERN.finditer(body):
            seen_ids.add(match.group(1))

        # "## 자주 언급하는 주제" 섹션 파싱
        existing_topics: set[str] = set()
        topic_section_re = re.compile(
            r"##\s*자주\s*언급하는\s*주제\s*\n(.*?)(?=\n##\s|\Z)",
            re.DOTALL,
        )
        m = topic_section_re.search(body)
        if m:
            section_text = m.group(1)
            for line in section_text.splitlines():
                line = line.strip()
                if not line.startswith("-"):
                    continue
                # "- topic [meeting:...]" → "topic" 추출
                content = line.lstrip("-").strip()
                # 인용 마커 제거
                content_no_cite = _CITATION_PATTERN.sub("", content).strip()
                if content_no_cite:
                    existing_topics.add(content_no_cite)

        return ExistingPersonState(
            rel_path=rel_path,
            name=name,
            role=role,
            first_seen_date=first_seen,
            last_seen_date=last_seen,
            meetings_count=meetings_count,
            seen_meeting_ids=frozenset(seen_ids),
            existing_topics=frozenset(existing_topics),
            raw_content=page_content,
        )

    @staticmethod
    def _read_existing(store: Any, rel_path: str) -> str | None:
        """existing_store.read_page 를 호출하여 기존 페이지 본문 반환.

        store 가 KeyError / WikiStoreError / 기타 예외를 raise 하면 None 반환.
        """
        try:
            result = store.read_page(rel_path)
        except Exception as exc:  # noqa: BLE001 — 기존 페이지 없음 = None
            logger.debug("기존 페이지 없음: %s (%r)", rel_path, exc)
            return None
        if isinstance(result, str):
            return result
        if hasattr(result, "content"):
            return getattr(result, "content", None)
        return None

    def _build_extracted_person(
        self,
        item: dict[str, Any],
        *,
        meeting_id: str,
        meeting_date: date,
        speaker_name_map: dict[str, str] | None,
        allowed_names: set[str] | None = None,
    ) -> ExtractedPerson | None:
        """LLM 응답 dict 를 ExtractedPerson 으로 변환.

        Args:
            item: LLM JSON 배열 단일 항목.
            meeting_id: 회의 ID (Citation 변환 시 사용).
            meeting_date: 회의 날짜.
            speaker_name_map: SPEAKER_XX → 한국어이름 매핑.
            allowed_names: utterances 에서 추출한 허용 화자 이름 집합.
                None 이면 환각 검증 비활성화 (하위 호환).

        Returns:
            ExtractedPerson 인스턴스 또는 None (필수 필드 누락 또는 환각 감지 시).
        """
        raw_name = str(item.get("name", "") or "").strip()
        if not raw_name:
            return None

        # speaker_name_map 적용 — SPEAKER_XX 가 매핑에 있으면 한국어 이름으로 정규화
        if speaker_name_map and raw_name in speaker_name_map:
            name = speaker_name_map[raw_name]
        else:
            name = raw_name

        # 환각 방지 — name 이 허용된 화자 집합에 없으면 skip (R1/R9 게이트)
        # action_item.py 의 _resolve_owner 와 동일 정책 적용
        if allowed_names is not None and name not in allowed_names:
            logger.warning(
                "PersonExtractor: 허용되지 않은 인물명 환각 감지 — skip: name=%r, "
                "allowed=%r",
                name,
                allowed_names,
            )
            return None

        # name_normalized — slug 정규화 실패 시 skip
        try:
            name_normalized = self._normalize_person_slug(name)
        except ValueError as exc:
            logger.warning("PersonExtractor: slug 정규화 실패 — skip: %r", exc)
            return None

        role_raw = item.get("role")
        role: str | None = None
        if role_raw is not None:
            role_str = str(role_raw).strip()
            role = role_str if role_str and role_str.lower() != "null" else None

        # topic_mentions
        topic_mentions: list[TopicMention] = []
        for tm in item.get("topic_mentions") or []:
            if not isinstance(tm, dict):
                continue
            topic = str(tm.get("topic", "") or "").strip()
            ts_str = str(tm.get("citation_ts", "") or "")
            cit = _citation_from_ts(meeting_id, ts_str)
            if topic and cit is not None:
                topic_mentions.append(TopicMention(topic=topic, citation=cit))

        # 화자 동일성 근거 인용
        ts_str = str(item.get("citation_ts", "") or "")
        primary_cit = _citation_from_ts(meeting_id, ts_str)
        citations: list[Citation] = []
        if primary_cit is not None:
            citations.append(primary_cit)

        try:
            confidence = int(item.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            confidence = 0

        meeting_date_str = meeting_date.isoformat()

        return ExtractedPerson(
            name=name,
            name_normalized=name_normalized,
            role=role,
            first_seen_meeting_id=meeting_id,
            first_seen_date=meeting_date_str,
            last_seen_meeting_id=meeting_id,
            last_seen_date=meeting_date_str,
            topic_mentions=topic_mentions,
            citations=citations,
            confidence=confidence,
        )

    @staticmethod
    def _build_role_topic_prompt(
        *,
        person: ExtractedPerson,
        existing: ExistingPersonState | None,
    ) -> str:
        """role/topic 보강용 user prompt 조립."""
        parts: list[str] = []
        parts.append(f"인물: {person.name}")
        parts.append(f"확정된 role 추론값: {person.role or '(없음)'}")
        if existing is not None:
            parts.append(f"existing_role: {existing.role or '(없음)'}")
            existing_topics_str = (
                ", ".join(sorted(existing.existing_topics))
                if existing.existing_topics
                else "(없음)"
            )
            parts.append(f"existing_topics: {existing_topics_str}")
        else:
            parts.append("existing_role: (신규 인물)")
            parts.append("existing_topics: (없음)")
        parts.append("")
        if person.topic_mentions:
            parts.append("## 이번 회의 주제 후보")
            for tm in person.topic_mentions:
                parts.append(
                    f"- {tm.topic} [meeting:{tm.citation.meeting_id}@{tm.citation.timestamp_str}]"
                )
        parts.append("")
        parts.append(
            "위 정보를 바탕으로 role_update 와 new_topics 를 JSON 객체로 출력하세요."
        )
        return "\n".join(parts)

    @staticmethod
    def _build_derived_recent_decisions(
        *,
        person_name: str,
        meeting_decisions: list[ExtractedDecision],
        meeting_id: str,
        meeting_date: date,
        existing_raw: str | None,
    ) -> list[str]:
        """"최근 결정" 섹션의 마크다운 라인 리스트.

        meeting_decisions 중 person_name 이 participants 에 포함된 항목만 필터.
        기존 페이지에 같은 결정이 이미 있으면 중복 추가하지 않는다.

        Args:
            person_name: 인물 이름.
            meeting_decisions: 동일 회의의 결정사항 목록.
            meeting_id: 회의 ID.
            meeting_date: 회의 날짜.
            existing_raw: 기존 페이지 raw text (중복 검사용).

        Returns:
            마크다운 라인 리스트.
        """
        date_str = meeting_date.isoformat()
        lines: list[str] = []
        for decision in meeting_decisions:
            if person_name not in decision.participants:
                continue
            # 인용 마커 — 결정의 첫 citation 또는 회의 시작 시각
            if decision.citations:
                cit = decision.citations[0]
                cit_str = f"[meeting:{cit.meeting_id}@{cit.timestamp_str}]"
            else:
                cit_str = f"[meeting:{meeting_id}@00:00:00]"
            line = f"- {date_str}: {decision.title} {cit_str}"
            # 중복 방지 — 같은 slug 가 기존 페이지에 이미 있으면 skip
            if existing_raw and decision.slug in existing_raw:
                continue
            lines.append(line)
            if len(lines) >= _MAX_RECENT_DECISIONS_IN_PAGE:
                break
        return lines

    @staticmethod
    def _build_derived_projects(
        *,
        person_name: str,
        meeting_decisions: list[ExtractedDecision],
        meeting_new_actions: list[NewActionItem],
    ) -> list[str]:
        """"담당 프로젝트" 섹션의 마크다운 라인 리스트.

        meeting_decisions + meeting_new_actions 의 project_slug 합집합.

        Args:
            person_name: 인물 이름.
            meeting_decisions: 결정사항 목록.
            meeting_new_actions: 신규 액션아이템 목록.

        Returns:
            마크다운 라인 리스트.
        """
        slugs: set[str] = set()
        # decisions 중 participant 인 항목의 projects
        for decision in meeting_decisions:
            if person_name not in decision.participants:
                continue
            for slug in decision.projects:
                if slug:
                    slugs.add(slug)
        # new_actions 중 owner 인 항목의 project_slug
        for action in meeting_new_actions:
            if action.owner != person_name:
                continue
            if action.project_slug:
                slugs.add(action.project_slug)

        lines: list[str] = []
        for slug in sorted(slugs):
            lines.append(f"- [{slug}](../projects/{slug}.md)")
        return lines

    @staticmethod
    def _build_derived_open_actions(
        *,
        person_name: str,
        new_actions: list[NewActionItem],
        existing_open_actions: list[OpenActionItem],
        meeting_id: str,
    ) -> list[str]:
        """"미해결 액션아이템" 섹션의 마크다운 라인 리스트.

        Args:
            person_name: 인물 이름.
            new_actions: 신규 액션 (이번 회의).
            existing_open_actions: 기존 미완료 액션.
            meeting_id: 회의 ID.

        Returns:
            마크다운 라인 리스트.
        """
        lines: list[str] = []
        seen_descs: set[str] = set()

        # 신규 액션 (이번 회의에서 발견)
        for action in new_actions:
            if action.owner != person_name:
                continue
            if action.description in seen_descs:
                continue
            seen_descs.add(action.description)
            cit = action.citation
            cit_str = f"[meeting:{cit.meeting_id}@{cit.timestamp_str}]"
            due = f" (due: {action.due_date})" if action.due_date else ""
            lines.append(f"- [ ] {action.description}{due} {cit_str}")
            if len(lines) >= _MAX_OPEN_ACTIONS_IN_PAGE:
                return lines

        # 기존 미완료 액션
        for action in existing_open_actions:
            if action.owner != person_name:
                continue
            if action.description in seen_descs:
                continue
            seen_descs.add(action.description)
            cit = action.citation
            cit_str = f"[meeting:{cit.meeting_id}@{cit.timestamp_str}]"
            due = f" (due: {action.due_date})" if action.due_date else ""
            lines.append(
                f"- [ ] {action.description} (from {action.from_date}){due} {cit_str}"
            )
            if len(lines) >= _MAX_OPEN_ACTIONS_IN_PAGE:
                break

        return lines

    @staticmethod
    def _compose_person_page(
        *,
        name: str,
        name_normalized: str,
        role: str | None,
        first_seen_date: str,
        last_seen_date: str,
        meetings_count: int,
        merged_topics: list[tuple[str, Citation | None]],
        recent_decisions_lines: list[str],
        project_lines: list[str],
        open_action_lines: list[str],
        confidence: int,
        meeting_id: str,
    ) -> str:
        """PRD §4.2 people 템플릿에 맞춰 페이지 본문을 합성한다.

        Args:
            name: 표시 이름.
            name_normalized: 파일명 안전 식별자.
            role: 역할 또는 None.
            first_seen_date: ISO 날짜.
            last_seen_date: ISO 날짜.
            meetings_count: 누적 회의 수.
            merged_topics: 자주 언급 주제 (topic, citation).
            recent_decisions_lines: derived 최근 결정 라인.
            project_lines: derived 담당 프로젝트 라인.
            open_action_lines: derived 미해결 액션 라인.
            confidence: D3 임계 비교용.
            meeting_id: 인용 fallback 용.

        Returns:
            마크다운 문자열.
        """
        # frontmatter
        fm_lines: list[str] = ["---", "type: person", f"name: {name}"]
        if role is not None:
            fm_lines.append(f"role: {role}")
        fm_lines.append(f"first_seen: {first_seen_date}")
        fm_lines.append(f"last_seen: {last_seen_date}")
        fm_lines.append(f"meetings_count: {meetings_count}")
        fm_lines.append("---")

        body_parts: list[str] = []
        body_parts.append("")
        # 제목 — 역할 표기 포함
        title_suffix = f" ({role})" if role else ""
        body_parts.append(f"# {name}{title_suffix}")
        body_parts.append("")

        # ## 최근 결정
        body_parts.append("## 최근 결정 (latest 5)")
        body_parts.append("")
        if recent_decisions_lines:
            body_parts.extend(recent_decisions_lines)
        else:
            body_parts.append("_(없음)_")
        body_parts.append("")

        # ## 담당 프로젝트
        body_parts.append("## 담당 프로젝트")
        body_parts.append("")
        if project_lines:
            body_parts.extend(project_lines)
        else:
            body_parts.append("_(없음)_")
        body_parts.append("")

        # ## 자주 언급하는 주제
        body_parts.append("## 자주 언급하는 주제")
        body_parts.append("")
        if merged_topics:
            for topic, cit in merged_topics:
                if cit is not None:
                    cit_str = f"[meeting:{cit.meeting_id}@{cit.timestamp_str}]"
                else:
                    cit_str = f"[meeting:{meeting_id}@00:00:00]"
                body_parts.append(f"- {topic} {cit_str}")
        else:
            body_parts.append("_(없음)_")
        body_parts.append("")

        # ## 미해결 액션아이템
        body_parts.append("## 미해결 액션아이템")
        body_parts.append("")
        if open_action_lines:
            body_parts.extend(open_action_lines)
        else:
            body_parts.append("_(없음)_")
        body_parts.append("")

        # confidence 마커
        body_parts.append(f"<!-- confidence: {confidence} -->")
        body_parts.append("")

        return "\n".join(fm_lines) + "\n" + "\n".join(body_parts)
