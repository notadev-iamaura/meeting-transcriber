"""ProjectExtractor — 회의에서 프로젝트 발견 + projects/{slug}.md 페이지 갱신.

핵심 차별점:
    1. status enum 검증 — schema._VALID_PROJECT_STATUSES 4종 외 ValueError.
    2. slug 정책 — 영문 lowercase + hyphen, 한글 보존, path traversal 거부.
    3. 타임라인 누적 — 기존 "## 진행 타임라인" 보존 + 이번 회의 항목만 추가.
    4. status 전환은 별도 메서드 (`detect_status_transitions`) — 보수적 검증을
       위해 extract 와 분리. confidence < 8 또는 모호한 표현은 무시.

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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from core.wiki.extractors.action_item import NewActionItem, OpenActionItem
from core.wiki.extractors.decision import ExtractedDecision, _strip_paren_latin
from core.wiki.generated_document import normalize_generated_document
from core.wiki.llm_client import WikiLLMClient, sanitize_utterance_text
from core.wiki.models import Citation
from core.wiki.person_identity import is_named_person_identity
from core.wiki.store import WikiStoreError

logger = logging.getLogger(__name__)


# 회의당 갱신 상한 — decisions / persons 와 동일.
_MAX_PROJECTS_PER_MEETING: int = 8

# status 전환을 자동 적용할 최소 confidence (모호 전환 무시).
_STATUS_TRANSITION_MIN_CONFIDENCE: int = 8

# 허용된 status — schema._VALID_PROJECT_STATUSES 와 동기화.
_VALID_STATUSES: frozenset[str] = frozenset({"in-progress", "blocked", "shipped", "cancelled"})


# ─────────────────────────────────────────────────────────────────────────
# 2.1 Utterance Protocol — Phase 2 와 동일
# ─────────────────────────────────────────────────────────────────────────


class Utterance(Protocol):
    """corrector 단계의 발화 표현. 직접 import 회피용 Protocol."""

    speaker: str
    text: str
    start: float
    end: float


# ─────────────────────────────────────────────────────────────────────────
# 2.2 데이터 모델
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TimelineEntry:
    """프로젝트 진행 타임라인의 단일 항목.

    Attributes:
        entry_date: ISO 날짜 문자열.
        description: 한 줄 설명.
        citation: 발화 인용.
    """

    entry_date: str
    description: str
    citation: Citation


@dataclass
class ExtractedProject:
    """LLM 추출 결과 — 1차 가공 단계.

    Attributes:
        name: 사람이 부르는 이름.
        slug: filename-safe.
        status: _VALID_STATUSES 4종 중 1.
        owner: 담당자. None 허용.
        started: 프로젝트 시작일.
        target: 목표일.
        description: 한 줄 요약.
        timeline_entry: 이번 회의에서 추가될 타임라인 1건.
        unresolved_issues: 미해결 이슈 (description, citation) 목록.
        participants: 회의에서 이 프로젝트를 언급한 화자 이름 목록.
        citations: 본 프로젝트의 모든 인용 평탄화.
        confidence: LLM 자체 평가 0~10.
    """

    name: str
    slug: str
    status: str
    owner: str | None
    started: date | None
    target: date | None
    description: str
    timeline_entry: TimelineEntry | None = None
    unresolved_issues: list[tuple[str, Citation]] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    confidence: int = 0


@dataclass(frozen=True)
class ExistingProject:
    """기존 projects/{slug}.md 의 frontmatter + 본문에서 파싱한 정보.

    Attributes:
        rel_path: 상대 경로.
        slug: frontmatter 의 slug.
        name: 본문 첫 H1 의 표시 이름.
        status: 현재 status.
        owner: 현재 owner.
        started, target, last_updated: ISO 날짜 문자열.
        existing_timeline: 본문 "## 진행 타임라인" 섹션의 항목 목록 (line raw).
        existing_issues: 본문 "## 미해결 이슈" 섹션의 항목 목록 (line raw).
        seen_meeting_ids: 본문에 등장한 모든 meeting_id 집합.
        raw_content: 전체 raw text.
    """

    rel_path: Path
    slug: str
    name: str
    status: str
    owner: str | None
    started: str | None
    target: str | None
    last_updated: str | None
    existing_timeline: tuple[str, ...]
    existing_issues: tuple[str, ...]
    seen_meeting_ids: frozenset[str]
    raw_content: str
    current_status_lines: tuple[str, ...] = ()


class StatusTransitions(dict[str, str]):
    """기존 dict 계약을 유지하며 각 전환을 뒷받침하는 인용을 함께 전달한다."""

    def __init__(self) -> None:
        """전환 목록과 검증된 인용 목록을 초기화한다."""
        super().__init__()
        self.citations: dict[str, Citation] = {}


# ─────────────────────────────────────────────────────────────────────────
# 2.3 헬퍼
# ─────────────────────────────────────────────────────────────────────────


# 인용 마커 패턴.
_CITATION_PATTERN: re.Pattern[str] = re.compile(
    r"\[meeting:([A-Za-z0-9_]+)@(\d{2}):(\d{2}):(\d{2})\]"
)


def _utterance_value(utterance: Any, key: str, default: Any) -> Any:
    """객체·체크포인트 dict 양쪽 발화의 필드를 읽는다."""
    return (
        utterance.get(key, default)
        if isinstance(utterance, Mapping)
        else getattr(utterance, key, default)
    )


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
    if h < 0 or not 0 <= m < 60 or not 0 <= s < 60:
        return None
    return Citation(
        meeting_id=meeting_id,
        timestamp_str=f"{h:02d}:{m:02d}:{s:02d}",
        timestamp_seconds=h * 3600 + m * 60 + s,
    )


def _extract_citations_from_text(text: str) -> list[Citation]:
    """텍스트에서 인용 마커를 모두 추출하여 Citation 리스트로 반환한다."""
    results: list[Citation] = []
    if not text:
        return results
    for match in _CITATION_PATTERN.finditer(text):
        mid = match.group(1)
        hh, mm, ss = match.group(2), match.group(3), match.group(4)
        results.append(
            Citation(
                meeting_id=mid,
                timestamp_str=f"{hh}:{mm}:{ss}",
                timestamp_seconds=int(hh) * 3600 + int(mm) * 60 + int(ss),
            )
        )
    return results


def _extract_json_array(text: str) -> list[Any] | None:
    """LLM 응답에서 JSON 배열을 robust 하게 파싱한다.

    동작:
        1. 전체 텍스트로 json.loads 시도.
        2. 실패 시 첫 `[` 와 마지막 `]` 사이를 추출하여 재시도.

    Returns:
        파싱된 리스트 또는 None.
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


def _parse_iso_date(value: Any) -> date | None:
    """문자열을 ISO date 로 파싱. 실패 시 None."""
    if not value or value in ("null", "None"):
        return None
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────
# 2.4 시스템 프롬프트
# ─────────────────────────────────────────────────────────────────────────


_EXTRACT_SYSTEM_PROMPT = """\
당신은 회의록에서 프로젝트 정보만 추출하는 분석가입니다.
출력은 JSON 배열, 각 항목 키:
- name: 한국어 또는 영문 이름
- slug: filename-safe (영문은 hyphen-case, 한글은 그대로)
- status: "in-progress" | "blocked" | "shipped" | "cancelled" 중 하나
- owner: 담당자 이름 (없으면 null)
- started: 시작일 "YYYY-MM-DD" (발화에 명시된 경우만)
- target: 목표일 "YYYY-MM-DD"
- description: 한 줄 요약 (인용 마커 [meeting:{제공된 회의 ID}@HH:MM:SS] 1개 이상 포함)
- timeline_entry: {description, citation_ts} 또는 null
- unresolved_issues: [{description, citation_ts}, ...]
- participants: 화자 이름 배열
- confidence: 0~10 정수

규칙:
1. status 는 4종 외 사용 금지. 모호하면 "in-progress" 기본.
2. 신규 프로젝트는 회의에서 명확히 시작/언급된 경우만.
3. 한국어 고유명사 외국어 병기 금지.
4. 프로젝트가 없으면 빈 배열 [].
5. citation_ts 와 인용 마커의 HH:MM:SS 는 반드시 발화 목록에 있는 실제 시각을 사용.
6. 00:00:00 은 발화 목록에 실제로 [00:00:00] 줄이 있을 때만 사용하고, 추정 기본값으로 쓰지 말 것.
"""


_STATUS_TRANSITION_SYSTEM_PROMPT = """\
당신은 기존 프로젝트의 status 전환 신호를 보수적으로 감지합니다.

입력으로 기존 프로젝트 목록(slug + 현재 status) 과 회의 발화가 주어집니다.
출력 JSON 배열:
- slug: 기존 프로젝트 slug
- new_status: "in-progress" | "blocked" | "shipped" | "cancelled"
- reason_citation_ts: "HH:MM:SS"
- confidence: 0~10 정수

규칙:
1. "출시했습니다", "출시 완료" → shipped (confidence ≥ 8)
2. "막혔어요", "블로커" → blocked
3. "취소합시다" → cancelled
4. "진행 중", "잘 되고 있어요" → 전환 신호 아님, 출력 제외
5. confidence < 8 모호 신호는 출력 제외.
6. 매핑 실패 시 빈 배열 [].
7. reason_citation_ts 는 반드시 발화 목록에 있는 실제 시각을 사용.
8. 00:00:00 은 발화 목록에 실제로 [00:00:00] 줄이 있을 때만 사용하고, 추정 기본값으로 쓰지 말 것.
"""


_RENDER_SYSTEM_PROMPT = """\
당신은 프로젝트 진행 페이지를 작성/갱신합니다.

페이지 형식 (PRD §4.2 projects 템플릿):
---
type: project
slug: <slug>
status: in-progress | blocked | shipped | cancelled
owner: <이름>
started: YYYY-MM-DD
target: YYYY-MM-DD
last_updated: YYYY-MM-DD
---

# {title} ({slug})

## 현재 상태
**{status}** — 한 줄 요약 [meeting:{제공된 회의 ID}@HH:MM:SS].

## 최근 결정사항
- ...

## 진행 타임라인
- YYYY-MM-DD: ... [meeting:{제공된 회의 ID}@HH:MM:SS]

## 미해결 이슈
- ... [meeting:{제공된 회의 ID}@HH:MM:SS]

## 참여자
- 이름 (역할), ...

<!-- confidence: N -->

규칙:
1. 모든 사실 진술에 인용 마커 부착.
2. 인용 마커의 meeting_id 는 입력에 제공된 회의 ID 문자열을 절대 변경하지 말고 그대로 사용.
3. "## 참여자" 섹션은 메타데이터이므로 인용 마커가 없어도 된다.
4. 00:00:00 은 발화 목록에 실제로 [00:00:00] 줄이 있을 때만 사용하고, 추정 기본값으로 쓰지 말 것.
5. 한국어 고유명사 외국어 병기 금지.
6. 기존 페이지가 있으면 created_at + 기존 타임라인 항목 보존, 새 항목만 추가.
7. status 전환 시 옛 status 는 ~~취소선~~ + 인용 유지.
8. 마지막 줄에 confidence 마커 필수.
9. owner/started/target은 추출 정보 또는 기존 페이지에 제공된 값만 사용하고, 미제공 필드는
   생략한다. 회의 날짜를 착수일·목표일로 추정하거나 익명 화자 라벨을 담당자 이름으로 쓰지 않는다.
10. 질문·제안·조건부 발언을 합의나 확정 사실로 바꾸지 않는다. 질문에 대한 명시적 확답이
    있어야 확정으로 쓰고, 질문과 확답이 모두 근거이면 양쪽의 정확한 인용 마커를 함께 복사한다.
"""


# ─────────────────────────────────────────────────────────────────────────
# 2.5 추출기
# ─────────────────────────────────────────────────────────────────────────


class ProjectExtractor:
    """회의에서 프로젝트 발견 + projects/{slug}.md 페이지 갱신."""

    def __init__(self, llm: WikiLLMClient) -> None:
        """LLM 추상화 1개만 받는다.

        Args:
            llm: WikiLLMClient (실구현 또는 mock).
        """
        self._llm: WikiLLMClient = llm

    @classmethod
    def load_existing_projects(
        cls,
        store: Any,
        projects: list[ExtractedProject],
        *,
        meeting_date: date,
    ) -> list[ExistingProject]:
        """현재 회의에 언급된 프로젝트의 실제 이전 상태만 안전하게 읽는다."""
        existing: list[ExistingProject] = []
        seen: set[str] = set()
        for project in projects:
            if project.slug in seen:
                continue
            seen.add(project.slug)
            path = Path("projects") / f"{project.slug}.md"
            try:
                raw = cls._read_existing(store, str(path))
                state = cls._parse_existing_project_state(raw, path) if raw else None
            except (OSError, WikiStoreError, ValueError) as exc:
                logger.warning("프로젝트 기존 상태 읽기 실패: path=%s, error=%r", path, exc)
                continue
            if state is None:
                continue
            previous_date = _parse_iso_date((state.last_updated or "").split("T", 1)[0])
            if previous_date is not None and previous_date <= meeting_date:
                existing.append(state)
        return existing

    async def extract_projects(
        self,
        *,
        meeting_id: str,
        meeting_date: date,
        utterances: list,
        summary: str,
    ) -> list[ExtractedProject]:
        """LLM 1회 호출로 프로젝트 언급 추출. 신규/기존 모두.

        Args:
            meeting_id: 회의 ID.
            meeting_date: 회의 날짜.
            utterances: 5단계 corrector 결과.
            summary: 8단계 요약.

        Returns:
            ExtractedProject 리스트. 빈 회의면 빈 리스트.
        """
        if not utterances:
            return []

        # utterances 직렬화
        lines: list[str] = []
        for utt in utterances:
            text = sanitize_utterance_text(_utterance_value(utt, "text", ""))
            speaker = _utterance_value(utt, "speaker", "UNKNOWN")
            start = _utterance_value(utt, "start", 0.0)
            ts_str = self._seconds_to_hhmmss(float(start))
            lines.append(f"[{ts_str}] {speaker}: {text}")

        user_prompt = (
            f"회의 ID: {meeting_id}\n"
            f"회의 날짜: {meeting_date.isoformat()}\n\n"
            f"## 8단계 요약\n{summary}\n\n"
            f"## 발화 목록\n" + "\n".join(lines) + "\n\n"
            "위 컨텍스트에서 프로젝트를 JSON 배열로 추출하세요."
        )

        try:
            raw = await self._llm.generate(
                system_prompt=_EXTRACT_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ProjectExtractor.extract_projects 1차 호출 실패: %r", exc)
            return []

        parsed = _extract_json_array(raw)

        # 1회 재시도
        if parsed is None:
            logger.warning("ProjectExtractor: 1차 JSON 파싱 실패, 1회 재시도")
            try:
                raw = await self._llm.generate(
                    system_prompt=_EXTRACT_SYSTEM_PROMPT,
                    user_prompt=user_prompt + "\n\n반드시 JSON 배열만 출력하세요.",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("ProjectExtractor 재시도 실패: %r", exc)
                return []
            parsed = _extract_json_array(raw)

        if parsed is None:
            return []

        results: list[ExtractedProject] = []
        slug_to_idx: dict[str, int] = {}

        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                project = self._build_extracted_project(item, meeting_id=meeting_id)
            except ValueError as exc:
                logger.warning("ProjectExtractor: 항목 검증 실패 — skip: %r", exc)
                continue
            except Exception as exc:  # noqa: BLE001 — 기타 항목 변환 실패는 skip
                logger.warning("ProjectExtractor: 항목 변환 실패 — skip: %r", exc)
                continue
            if project is None:
                continue

            # 동일 slug 중복 시 confidence 높은 쪽 채택
            if project.slug in slug_to_idx:
                idx = slug_to_idx[project.slug]
                if project.confidence > results[idx].confidence:
                    results[idx] = project
            else:
                slug_to_idx[project.slug] = len(results)
                results.append(project)

        return results

    async def detect_status_transitions(
        self,
        *,
        existing_projects: list[ExistingProject],
        meeting_id: str,
        utterances: list,
    ) -> dict[str, str]:
        """기존 프로젝트의 status 전환을 보수적으로 감지 (LLM 1회).

        Args:
            existing_projects: 기존 프로젝트 목록.
            meeting_id: 회의 ID.
            utterances: 발화 목록.

        Returns:
            {slug: new_status} dict. 변경 없으면 빈 dict.
        """
        # 단락 — existing_projects 비어있으면 LLM 호출 없이 즉시 반환
        if not existing_projects:
            return {}

        if not utterances:
            return {}

        # existing 직렬화
        existing_lines: list[str] = []
        for proj in existing_projects:
            existing_lines.append(f"- slug={proj.slug}, status={proj.status}")

        utt_lines: list[str] = []
        for utt in utterances:
            text = sanitize_utterance_text(_utterance_value(utt, "text", ""))
            speaker = _utterance_value(utt, "speaker", "UNKNOWN")
            start = _utterance_value(utt, "start", 0.0)
            ts_str = self._seconds_to_hhmmss(float(start))
            utt_lines.append(f"[{ts_str}] {speaker}: {text}")

        user_prompt = (
            f"회의 ID: {meeting_id}\n\n"
            f"## 기존 프로젝트 목록\n" + "\n".join(existing_lines) + "\n\n"
            "## 발화 목록\n" + "\n".join(utt_lines) + "\n\n"
            "위 발화에서 status 전환 신호를 JSON 배열로 알려주세요."
        )

        try:
            raw = await self._llm.generate(
                system_prompt=_STATUS_TRANSITION_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ProjectExtractor.detect_status_transitions 실패: %r", exc)
            return {}

        parsed = _extract_json_array(raw)
        if parsed is None:
            return {}

        existing_slug_set = {p.slug for p in existing_projects}
        transitions = StatusTransitions()

        for item in parsed:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug", "") or "").strip()
            new_status = str(item.get("new_status", "") or "").strip()
            try:
                confidence = int(item.get("confidence", 0) or 0)
            except (TypeError, ValueError):
                continue

            if not slug or slug not in existing_slug_set:
                continue
            # confidence 임계값 검증
            if confidence < _STATUS_TRANSITION_MIN_CONFIDENCE:
                continue
            # status 화이트리스트 검증
            if new_status not in _VALID_STATUSES:
                logger.warning(
                    "ProjectExtractor: 잘못된 new_status — skip: slug=%s, new_status=%s",
                    slug,
                    new_status,
                )
                continue
            citation = _citation_from_ts(meeting_id, str(item.get("reason_citation_ts", "")))
            if citation is None:
                continue
            has_source = False
            for utterance in utterances:
                try:
                    start = int(float(_utterance_value(utterance, "start", None)))
                    end = int(float(_utterance_value(utterance, "end", None)))
                except (TypeError, ValueError, OverflowError):
                    continue
                if (
                    start <= citation.timestamp_seconds <= end
                    and str(_utterance_value(utterance, "text", "")).strip()
                ):
                    has_source = True
                    break
            if not has_source:
                logger.warning("프로젝트 상태 전환 인용 부재 — 보존: slug=%s", slug)
                continue
            transitions[slug] = new_status
            transitions.citations[slug] = citation

        return transitions

    async def render_or_update_pages(
        self,
        *,
        projects: list[ExtractedProject],
        status_transitions: dict[str, str],
        meeting_id: str,
        meeting_date: date,
        existing_store: Any,
        meeting_decisions: list[ExtractedDecision],
        meeting_new_actions: list[NewActionItem],
        existing_open_actions: list[OpenActionItem],
    ) -> list[tuple[str, str, int]]:
        """기존 페이지가 있으면 갱신, 없으면 신규 생성.

        Args:
            projects: extract_projects() 결과.
            status_transitions: detect_status_transitions() 결과.
            meeting_id: 회의 ID.
            meeting_date: 회의 날짜.
            existing_store: WikiStore (read_page 만 사용).
            meeting_decisions: 동일 회의의 결정사항.
            meeting_new_actions: 동일 회의의 신규 액션.
            existing_open_actions: 기존 미완료 액션.

        Returns:
            [(rel_path, content, confidence), ...]. confidence 내림차순,
            최대 _MAX_PROJECTS_PER_MEETING 건.
        """
        if not projects:
            return []

        # confidence 내림차순 정렬 + 상위 N건 cap
        sorted_projects = sorted(projects, key=lambda p: p.confidence, reverse=True)
        capped = sorted_projects[:_MAX_PROJECTS_PER_MEETING]
        if len(projects) > _MAX_PROJECTS_PER_MEETING:
            logger.info(
                "ProjectExtractor.render_or_update_pages: %d건 중 상위 %d건만 처리 (R3 상한)",
                len(projects),
                _MAX_PROJECTS_PER_MEETING,
            )

        async def _render_one(
            project: ExtractedProject,
        ) -> tuple[str, str, int] | None:
            """단일 project → (rel_path, content, confidence)."""
            rel_path = f"projects/{project.slug}.md"

            # 기존 페이지 확인
            existing_state: ExistingProject | None = None
            try:
                existing_raw = self._read_existing(existing_store, rel_path)
            except (OSError, WikiStoreError, ValueError) as exc:
                logger.warning("프로젝트 원장 읽기 실패 — 보존: path=%s, error=%r", rel_path, exc)
                return None
            if existing_raw:
                existing_state = self._parse_existing_project_state(existing_raw, Path(rel_path))
                if existing_state is None:
                    logger.warning("기존 프로젝트 메타데이터 불명 — 보존: path=%s", rel_path)
                    return None
                previous_date = _parse_iso_date(
                    (existing_state.last_updated or "").split("T", 1)[0]
                )
                if previous_date is None or previous_date > meeting_date:
                    logger.info("과거·날짜불명 프로젝트 갱신 보류: path=%s", rel_path)
                    return None

            # 기존 상태는 별도 전환 검사를 통과한 경우만 바꾼다.
            fallback_status = existing_state.status if existing_state else project.status
            citations = getattr(status_transitions, "citations", {})
            transition_citation = citations.get(project.slug)
            if not isinstance(transition_citation, Citation):
                transition_citation = None
            final_status = (
                status_transitions.get(project.slug, fallback_status)
                if transition_citation is not None
                else fallback_status
            )
            try:
                self._validate_status(final_status)
            except ValueError as exc:
                logger.warning(
                    "ProjectExtractor: invalid final_status — skip: slug=%s, %r",
                    project.slug,
                    exc,
                )
                return None

            # LLM 호출 — 페이지 본문 생성
            try:
                raw_response = await self._llm.generate(
                    system_prompt=_RENDER_SYSTEM_PROMPT,
                    user_prompt=self._build_render_prompt(
                        project=project,
                        final_status=final_status,
                        meeting_id=meeting_id,
                        meeting_date=meeting_date,
                        existing=existing_state,
                        meeting_decisions=meeting_decisions,
                        meeting_new_actions=meeting_new_actions,
                        existing_open_actions=existing_open_actions,
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ProjectExtractor LLM 호출 실패 — skip: path=%s, %r",
                    rel_path,
                    exc,
                )
                return None

            # 전체 문서 wrapper를 먼저 벗겨 아래 상태·날짜·기존 원장 보존을 적용한다.
            try:
                normalized_response = normalize_generated_document(raw_response)
            except ValueError:
                logger.warning(
                    "프로젝트 문서 wrapper 거부 — 기존 원장 보존: reason=malformed_document"
                )
                return None

            # 후처리 — 한국어 고유명사 영문 병기 제거
            content = _strip_paren_latin(normalized_response)

            # 검증된 final_status 강제 주입 — LLM 이 임의 status 를 쓰는 환각 차단 (R9)
            content = self._inject_frontmatter_field(content, "status", final_status)
            content = self._inject_frontmatter_field(
                content, "last_updated", meeting_date.isoformat()
            )

            # 렌더 모델의 추정을 버리고 구조화된 입력·검증된 기존 값만 반영한다.
            owner = project.owner if is_named_person_identity(project.owner) else None
            if owner is None and existing_state is not None:
                owner = existing_state.owner
            started = existing_state.started if existing_state is not None else None
            if started is None and project.started is not None:
                started = project.started.isoformat()
            target = project.target.isoformat() if project.target is not None else None
            if target is None and existing_state is not None:
                target = existing_state.target
            for key, value in (("owner", owner), ("started", started), ("target", target)):
                content = self._set_optional_frontmatter_field(content, key, value)

            # 프롬프트만으로는 누적 원장이 유실될 수 있으므로 프로그램으로 보존한다.
            if existing_state is not None:
                if transition_citation is not None:
                    label = {
                        "in-progress": "진행 중",
                        "blocked": "진행 차단",
                        "shipped": "출시 완료",
                        "cancelled": "취소",
                    }[final_status]
                    content = self._replace_section(
                        content,
                        "현재 상태",
                        [
                            f"프로젝트 상태: {label} ({final_status}). "
                            f"[meeting:{transition_citation.meeting_id}@{transition_citation.timestamp_str}]"
                        ],
                    )
                elif existing_state.current_status_lines:
                    content = self._replace_section(
                        content, "현재 상태", list(existing_state.current_status_lines)
                    )
                prior_metadata, _ = self._parse_frontmatter(existing_raw or "")
                if prior_metadata.get("created_at"):
                    content = self._inject_frontmatter_field(
                        content, "created_at", str(prior_metadata["created_at"])
                    )
                entry = project.timeline_entry
                if entry is not None and not entry.entry_date:
                    entry = TimelineEntry(
                        meeting_date.isoformat(), entry.description, entry.citation
                    )
                content = self._replace_section(
                    content,
                    "진행 타임라인",
                    self._merge_timeline(existing_state.existing_timeline, entry),
                )
                issues = list(existing_state.existing_issues)
                for description, citation in project.unresolved_issues:
                    line = (
                        f"- {description} [meeting:{citation.meeting_id}@{citation.timestamp_str}]"
                    )
                    if line not in issues:
                        issues.append(line)
                content = self._replace_section(content, "미해결 이슈", issues)

            return (rel_path, content, project.confidence)

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
    def _validate_status(status: str) -> None:
        """status 가 _VALID_STATUSES 에 속하는지 검증.

        Raises:
            ValueError: 4종 외 또는 빈 문자열.
        """
        if not status or status not in _VALID_STATUSES:
            raise ValueError(f"invalid status: {status!r}. 허용: {sorted(_VALID_STATUSES)}")

    @staticmethod
    def _normalize_project_slug(raw: str) -> str:
        """프로젝트 이름 → filename-safe slug.

        정책:
            - 영문/숫자: 소문자화 + 공백 → 하이픈.
            - 한글: 그대로 보존, 공백 → 언더스코어.
            - 혼합: 영문 부분만 lowercase, 한글 부분 보존.
            - `..` / 슬래시 / NUL → ValueError.

        예시:
            "New Onboarding"  → "new-onboarding"
            "신규 온보딩"      → "신규_온보딩"
            "Q3 Launch"       → "q3-launch"
            "../etc/pwd"      → ValueError
        """
        if not raw or not raw.strip():
            raise ValueError("project slug 가 빈 문자열입니다")
        s = raw.strip()
        # path traversal 방어
        if ".." in s or "/" in s or "\\" in s or "\x00" in s:
            raise ValueError(f"path traversal 시도가 감지되었습니다: {raw!r}")

        # 한글 포함 여부 확인
        has_korean = bool(re.search(r"[\uAC00-\uD7A3]", s))

        if has_korean:
            # 한글 슬러그 — 영문 부분도 lowercase, 공백 → 언더스코어
            s = s.lower()
            s = re.sub(r"\s+", "_", s)
            # 허용 문자 — 한글/영숫자/하이픈/언더스코어
            if not re.match(r"^[\uAC00-\uD7A3a-z0-9\-_]+$", s):
                raise ValueError(f"허용되지 않는 문자가 포함된 slug 입니다: {raw!r}")
        else:
            # 영문 슬러그 — lowercase + 공백 → 하이픈
            s = s.lower()
            s = re.sub(r"\s+", "-", s)
            # 연속 하이픈 정리
            s = re.sub(r"-+", "-", s).strip("-_")
            if not s:
                raise ValueError(f"빈 slug 결과: {raw!r}")
            if not re.match(r"^[a-z0-9\-_]+$", s):
                raise ValueError(f"허용되지 않는 문자가 포함된 slug 입니다: {raw!r}")

        # 슬러그 길이 상한 — 파일시스템 안전 + ReDoS 방지
        if len(s) > 64:
            raise ValueError(f"project slug 가 64자 초과입니다 ({len(s)}자): {s[:32]!r}...")

        return s

    @staticmethod
    def _read_existing(store: Any, rel_path: str) -> str | None:
        """existing_store.read_page 결과를 안전하게 반환.

        실제 파일은 frontmatter를 포함한 원문으로 읽으며, 부재와 읽기 실패를 구분한다.
        """
        try:
            raw_reader = getattr(store, "read_raw_page", None)
            if callable(raw_reader):
                raw = raw_reader(Path(rel_path))
                if isinstance(raw, str):
                    return raw
            result = store.read_page(Path(rel_path))
        except KeyError:
            return None
        except WikiStoreError as exc:
            if exc.reason == "page_not_found":
                return None
            raise
        if isinstance(result, str):
            return result
        if hasattr(result, "content"):
            content = getattr(result, "content", None)
            metadata = getattr(result, "frontmatter", None)
            if isinstance(content, str) and isinstance(metadata, dict) and metadata:
                fields = [
                    f"{key}: {json.dumps(value, ensure_ascii=False, default=str)}"
                    for key, value in metadata.items()
                ]
                return "---\n" + "\n".join(fields) + "\n---\n\n" + content
            return content if isinstance(content, str) else None
        return None

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
        """저장소와 같은 스키마로 원문 메타데이터를 읽는다."""
        from core.wiki.store import _parse_frontmatter

        metadata, body = _parse_frontmatter(content)
        return metadata, body

    @staticmethod
    def _replace_section(content: str, title: str, lines: list[str]) -> str:
        """다른 섹션을 보존하면서 누적 섹션을 원장 라인으로 대체한다."""
        block = f"## {title}\n\n" + "\n".join(lines) + "\n"
        pattern = re.compile(
            rf"^##\s+{re.escape(title)}\s*\n.*?(?=^##\s|^<!--\s*confidence:|\Z)",
            re.M | re.S,
        )
        match = pattern.search(content)
        if match:
            return content[: match.start()] + block + "\n" + content[match.end() :]
        confidence = re.search(r"^<!--\s*confidence:", content, re.M)
        insert_at = confidence.start() if confidence else len(content)
        return content[:insert_at].rstrip() + "\n\n" + block + "\n" + content[insert_at:]

    @staticmethod
    def _parse_existing_project_state(page_content: str, rel_path: Path) -> ExistingProject | None:
        """WikiStore.read_page() 결과를 ExistingProject 로 lift.

        Args:
            page_content: 페이지 raw text.
            rel_path: 상대 경로.

        Returns:
            ExistingProject 또는 None.
        """
        try:
            from core.wiki.store import _parse_frontmatter  # noqa: PLC0415
        except ImportError:
            return None

        try:
            fm, body = _parse_frontmatter(page_content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ExistingProject 파싱 실패: %r", exc)
            return None

        slug = str(fm.get("slug", "") or "")
        status = str(fm.get("status", "") or "")
        if not slug or slug != rel_path.stem or status not in _VALID_STATUSES:
            return None
        owner_raw = fm.get("owner")
        owner: str | None = str(owner_raw).strip() if is_named_person_identity(owner_raw) else None
        started_date = _parse_iso_date(fm.get("started"))
        target_date = _parse_iso_date(fm.get("target"))
        started = started_date.isoformat() if started_date is not None else None
        target = target_date.isoformat() if target_date is not None else None
        last_updated = str(fm.get("last_updated", "") or "") or None

        # 본문 첫 H1 — "# 표시이름 (slug)"
        name = slug
        h1_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        if h1_match:
            h1_text = h1_match.group(1).strip()
            # "(slug)" 제거 시도
            paren_match = re.match(r"^(.+?)\s*\([^)]+\)\s*$", h1_text)
            if paren_match:
                name = paren_match.group(1).strip()
            else:
                name = h1_text

        # "## 진행 타임라인" 섹션 파싱
        timeline_lines: list[str] = []
        timeline_section_re = re.compile(
            r"##\s*진행\s*타임라인\s*\n(.*?)(?=\n##\s|\n<!--\s*confidence:|\Z)", re.DOTALL
        )
        m = timeline_section_re.search(body)
        if m:
            for line in m.group(1).splitlines():
                if line.strip():
                    timeline_lines.append(line.rstrip())

        # "## 미해결 이슈" 섹션 파싱
        issue_lines: list[str] = []
        issue_section_re = re.compile(
            r"##\s*미해결\s*이슈\s*\n(.*?)(?=\n##\s|\n<!--\s*confidence:|\Z)", re.DOTALL
        )
        m2 = issue_section_re.search(body)
        if m2:
            for line in m2.group(1).splitlines():
                if line.strip():
                    issue_lines.append(line.rstrip())

        # 본문 등장 meeting_id 집합
        seen_ids: set[str] = set()
        for match in _CITATION_PATTERN.finditer(body):
            seen_ids.add(match.group(1))

        current_status = re.search(
            r"##\s*현재\s*상태\s*\n(.*?)(?=\n##\s|\n<!--\s*confidence:|\Z)",
            body,
            re.DOTALL,
        )

        return ExistingProject(
            rel_path=rel_path,
            slug=slug,
            name=name,
            status=status,
            owner=owner,
            started=started,
            target=target,
            last_updated=last_updated,
            existing_timeline=tuple(timeline_lines),
            existing_issues=tuple(issue_lines),
            seen_meeting_ids=frozenset(seen_ids),
            raw_content=page_content,
            current_status_lines=tuple(
                line.rstrip() for line in current_status.group(1).splitlines() if line.strip()
            )
            if current_status
            else (),
        )

    def _build_extracted_project(
        self, item: dict[str, Any], *, meeting_id: str
    ) -> ExtractedProject | None:
        """LLM 응답 dict 를 ExtractedProject 로 변환.

        Args:
            item: LLM JSON 배열 단일 항목.
            meeting_id: 회의 ID.

        Returns:
            ExtractedProject 또는 None.
        """
        name = str(item.get("name", "") or "").strip()
        if not name:
            return None

        raw_slug = str(item.get("slug", "") or "").strip()
        if not raw_slug:
            raw_slug = name

        # slug 정규화 — 실패 시 skip
        try:
            slug = self._normalize_project_slug(raw_slug)
        except ValueError as exc:
            logger.warning("ProjectExtractor: slug 정규화 실패 — skip: %r", exc)
            return None

        status = str(item.get("status", "") or "").strip()
        # status 화이트리스트 검증 — 4종 외 항목 skip
        if status not in _VALID_STATUSES:
            logger.warning(
                "ProjectExtractor: 잘못된 status — skip: slug=%s, status=%s",
                slug,
                status,
            )
            return None

        owner_raw = item.get("owner")
        owner: str | None = None
        if is_named_person_identity(owner_raw):
            owner_str = str(owner_raw).strip()
            owner = owner_str

        started = _parse_iso_date(item.get("started"))
        target = _parse_iso_date(item.get("target"))

        description = str(item.get("description", "") or "").strip()

        # timeline_entry
        timeline_entry: TimelineEntry | None = None
        te_raw = item.get("timeline_entry")
        if isinstance(te_raw, dict):
            te_desc = str(te_raw.get("description", "") or "").strip()
            te_ts = str(te_raw.get("citation_ts", "") or "")
            te_cit = _citation_from_ts(meeting_id, te_ts)
            if te_desc and te_cit is not None:
                # entry_date — 명시값 없으면 회의 날짜에서 가져오므로 호출자가 채움
                te_date = str(te_raw.get("entry_date", "") or "")
                if not te_date:
                    te_date = ""  # render 단계에서 meeting_date 로 채움
                timeline_entry = TimelineEntry(
                    entry_date=te_date,
                    description=te_desc,
                    citation=te_cit,
                )

        # unresolved_issues
        unresolved: list[tuple[str, Citation]] = []
        for ui in item.get("unresolved_issues") or []:
            if not isinstance(ui, dict):
                continue
            ui_desc = str(ui.get("description", "") or "").strip()
            ui_ts = str(ui.get("citation_ts", "") or "")
            ui_cit = _citation_from_ts(meeting_id, ui_ts)
            if ui_desc and ui_cit is not None:
                unresolved.append((ui_desc, ui_cit))

        participants_raw = item.get("participants") or []
        participants = [str(p) for p in participants_raw if p]

        # citations 평탄화
        flat_citations = _extract_citations_from_text(description)

        try:
            confidence = int(item.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            confidence = 0

        return ExtractedProject(
            name=name,
            slug=slug,
            status=status,
            owner=owner,
            started=started,
            target=target,
            description=description,
            timeline_entry=timeline_entry,
            unresolved_issues=unresolved,
            participants=participants,
            citations=flat_citations,
            confidence=confidence,
        )

    @staticmethod
    def _build_render_prompt(
        *,
        project: ExtractedProject,
        final_status: str,
        meeting_id: str,
        meeting_date: date,
        existing: ExistingProject | None,
        meeting_decisions: list[ExtractedDecision],
        meeting_new_actions: list[NewActionItem],
        existing_open_actions: list[OpenActionItem],
    ) -> str:
        """render_or_update_pages() 의 user_prompt 를 조립한다."""
        parts: list[str] = []
        parts.append(f"파일 경로: projects/{project.slug}.md")
        parts.append(f"회의 ID: {meeting_id}")
        parts.append(f"회의 날짜: {meeting_date.isoformat()}")
        parts.append("")
        parts.append("## 추출된 프로젝트")
        parts.append(f"name: {project.name}")
        parts.append(f"slug: {project.slug}")
        parts.append(f"status (최종): {final_status}")
        if is_named_person_identity(project.owner):
            parts.append(f"owner: {project.owner}")
        if project.started:
            parts.append(f"started: {project.started.isoformat()}")
        if project.target:
            parts.append(f"target: {project.target.isoformat()}")
        parts.append(f"description: {project.description}")
        parts.append(f"participants: {project.participants}")
        parts.append(f"신뢰도: {project.confidence}")
        parts.append("")

        # derived "최근 결정사항" — slug 가 일치하는 decisions
        related_decisions = [d for d in meeting_decisions if project.slug in (d.projects or [])]
        if related_decisions:
            parts.append("## 이번 회의의 관련 결정 (최근 결정사항 섹션 입력)")
            for d in related_decisions:
                parts.append(f"- {d.title} ({d.slug})")
            parts.append("")

        # timeline_entry
        if project.timeline_entry is not None:
            te = project.timeline_entry
            te_date = te.entry_date or meeting_date.isoformat()
            parts.append("## 신규 타임라인 항목")
            parts.append(
                f"- {te_date}: {te.description} "
                f"[meeting:{te.citation.meeting_id}@{te.citation.timestamp_str}]"
            )
            parts.append("")

        # unresolved_issues
        if project.unresolved_issues:
            parts.append("## 신규 미해결 이슈")
            for desc, cit in project.unresolved_issues:
                parts.append(f"- {desc} [meeting:{cit.meeting_id}@{cit.timestamp_str}]")
            parts.append("")

        # 기존 페이지
        if existing is not None:
            parts.append("## 기존 페이지 (created_at + 기존 타임라인 보존)")
            parts.append(existing.raw_content)
            parts.append("")

        parts.append(
            "위 정보를 바탕으로 PRD §4.2 projects 템플릿에 맞춰 마크다운 페이지를 작성하세요."
        )
        return "\n".join(parts)

    @staticmethod
    def _set_optional_frontmatter_field(content: str, key: str, value: str | None) -> str:
        """frontmatter 안의 추정값·중복 필드를 제거하고 확인된 값만 한 번 기록한다."""
        lines = content.splitlines(keepends=True)
        if not lines or lines[0].rstrip("\r\n") != "---":
            return content
        end_idx = next(
            (idx for idx in range(1, len(lines)) if lines[idx].rstrip("\r\n") == "---"),
            None,
        )
        if end_idx is None:
            return content
        fields = [line for line in lines[1:end_idx] if line.partition(":")[0].strip() != key]
        if value is not None:
            newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
            fields.append(f"{key}: {value}{newline}")
        return "".join([lines[0], *fields, *lines[end_idx:]])

    @staticmethod
    def _inject_frontmatter_field(content: str, key: str, value: str) -> str:
        """frontmatter 의 특정 key 를 강제 주입.

        이미 있으면 교체, 없으면 frontmatter 닫기 직전에 추가.
        """
        # 기존 라인 교체
        new_content, count = re.subn(
            rf"^{re.escape(key)}:\s*.+$",
            f"{key}: {value}",
            content,
            count=1,
            flags=re.MULTILINE,
        )
        if count > 0:
            return new_content

        # frontmatter 닫기(`---`) 직전에 추가
        lines = content.splitlines(keepends=True)
        in_fm = False
        for idx, line in enumerate(lines):
            if line.strip() == "---":
                if not in_fm:
                    in_fm = True
                    continue
                # 닫는 ---
                lines.insert(idx, f"{key}: {value}\n")
                return "".join(lines)
        return content

    @staticmethod
    def _merge_timeline(
        existing: tuple[str, ...],
        new_entry: TimelineEntry | None,
    ) -> list[str]:
        """기존 타임라인 + 신규 항목 병합 (날짜 오름차순, 중복 citation 제거).

        Args:
            existing: 기존 타임라인 라인 튜플.
            new_entry: 신규 항목.

        Returns:
            병합된 라인 리스트.
        """
        merged: list[str] = list(existing)
        if new_entry is not None:
            cit_str = (
                f"[meeting:{new_entry.citation.meeting_id}@{new_entry.citation.timestamp_str}]"
            )
            new_line = f"- {new_entry.entry_date}: {new_entry.description} {cit_str}"
            # 중복 검사 — 두 가지 기준:
            # 1) 동일 citation 마커 (meeting_id@timestamp) 가 이미 있으면 skip (원본 기준).
            # 2) 날짜 + 설명 접두사가 동일한 라인이 있으면 skip (citation 없이 직접 작성된 경우).
            desc_prefix = f"- {new_entry.entry_date}: {new_entry.description}"
            already = any(cit_str in line or line.startswith(desc_prefix) for line in merged)
            if not already:
                merged.append(new_line)
        return merged
