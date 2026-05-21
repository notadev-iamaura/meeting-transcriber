"""Decision Wiki canonical record model.

LLM 이 추출한 결정 후보를 저장 가능한 Markdown decision page 로 정규화한다.
이 모듈은 디스크 I/O 나 LLM 호출을 하지 않고, schema 안정성과 dedupe 가능한
식별자 생성을 담당한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

_STATUS_VALUES = {"proposed", "decided", "superseded", "rejected", "pending"}
_CITATION_PATTERN = re.compile(r"\[meeting:([a-f0-9]{8})@(\d{2}):(\d{2}):(\d{2})\]")


def _yaml_inline_list(values: list[str]) -> str:
    """간단한 frontmatter inline list 문자열을 만든다."""
    if not values:
        return "[]"
    return "[" + ", ".join(_escape_scalar(v) for v in values) + "]"


def _escape_scalar(value: str) -> str:
    """frontmatter scalar 로 안전하게 넣을 수 있도록 콤마/대괄호를 정리한다."""
    cleaned = str(value).replace("\n", " ").strip()
    cleaned = cleaned.replace("[", "(").replace("]", ")")
    return cleaned


def _unique_sorted(values: list[str]) -> list[str]:
    """빈 값 제거 + stable unique + 정렬."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = str(value).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return sorted(result)


def extract_citation_markers(text: str) -> list[str]:
    """본문에서 citation marker 문자열을 순서대로 추출한다."""
    if not text:
        return []
    return [match.group(0) for match in _CITATION_PATTERN.finditer(text)]


@dataclass(frozen=True)
class DecisionRecord:
    """Canonical Decision Wiki record.

    Attributes:
        id: stable decision id.
        title: decision title.
        status: proposed/decided/superseded/rejected/pending.
        decision_date: meeting decision date.
        decision_text: accepted decision body.
        background: rationale/background body.
        project: primary project name/slug.
        participants: speakers or people involved.
        owners: action owners.
        confidence: extractor confidence 0..10.
        source_meetings: meeting ids backing this decision.
        citations: raw citation marker strings.
        follow_ups: Markdown-ready follow-up action lines.
        supersedes: prior decision ids.
        superseded_by: later decision id.
        created_at: first record creation timestamp.
        last_updated: latest update timestamp.
    """

    id: str
    title: str
    status: str
    decision_date: date
    decision_text: str
    background: str
    project: str = ""
    participants: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    confidence: int = 0
    source_meetings: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    created_at: str = ""
    last_updated: str = ""

    def __post_init__(self) -> None:
        """상태와 타임스탬프를 보수적으로 정규화한다."""
        status = self.status if self.status in _STATUS_VALUES else "decided"
        now = datetime.now().isoformat(timespec="seconds")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "participants", _unique_sorted(self.participants))
        object.__setattr__(self, "owners", _unique_sorted(self.owners))
        object.__setattr__(self, "source_meetings", _unique_sorted(self.source_meetings))
        object.__setattr__(self, "citations", _unique_sorted(self.citations))
        object.__setattr__(self, "supersedes", _unique_sorted(self.supersedes))
        object.__setattr__(self, "created_at", self.created_at or now)
        object.__setattr__(self, "last_updated", self.last_updated or now)

    @property
    def has_verified_candidate_citation(self) -> bool:
        """검증 후보로 넘길 citation 이 1개 이상 있는지 반환한다."""
        return bool(self.citations)

    def to_markdown(self) -> str:
        """DecisionRecord 를 canonical Markdown page 로 렌더링한다."""
        frontmatter = [
            "---",
            "type: decision",
            f"id: {_escape_scalar(self.id)}",
            f"title: {_escape_scalar(self.title)}",
            f"status: {_escape_scalar(self.status)}",
            f"decision_date: {self.decision_date.isoformat()}",
            # Legacy fields kept for existing readers/tests during migration.
            f"date: {self.decision_date.isoformat()}",
            f"meeting_id: {_escape_scalar(self.source_meetings[0]) if self.source_meetings else ''}",
            f"project: {_escape_scalar(self.project)}",
            f"projects: {_yaml_inline_list([self.project] if self.project else [])}",
            f"participants: {_yaml_inline_list(self.participants)}",
            f"owners: {_yaml_inline_list(self.owners)}",
            f"confidence: {int(self.confidence)}",
            f"source_meetings: {_yaml_inline_list(self.source_meetings)}",
            f"supersedes: {_yaml_inline_list(self.supersedes)}",
            f"superseded_by: {_escape_scalar(self.superseded_by or '')}",
            f"created_at: {self.created_at}",
            f"last_updated: {self.last_updated}",
            f"updated_at: {self.last_updated}",
            "---",
        ]
        follow_ups = self.follow_ups or ["없음"]
        citations = self.citations or []
        citation_lines = [f"- {marker}" for marker in citations] or ["- 없음"]
        source_lines = [
            f"- [{meeting_id}](../../../app/viewer/{meeting_id})"
            for meeting_id in self.source_meetings
        ] or ["- 없음"]

        body = [
            f"# {self.title}",
            "",
            "## 결정 내용",
            self.decision_text or "없음",
            "",
            "## 배경",
            self.background or "없음",
            "",
            "## 후속 액션",
            *[f"- {line}" if not line.startswith("-") else line for line in follow_ups],
            "",
            "## 근거",
            *citation_lines,
            "",
            "## 참고 회의",
            *source_lines,
            "",
            f"<!-- confidence: {int(self.confidence)} -->",
        ]
        return "\n".join(frontmatter + [""] + body)

    @classmethod
    def from_extracted(
        cls,
        *,
        decision: Any,
        meeting_id: str,
        meeting_date: date,
        created_at: str | None = None,
        record_id: str | None = None,
        source_meetings: list[str] | None = None,
    ) -> DecisionRecord:
        """ExtractedDecision 호환 객체에서 DecisionRecord 를 만든다."""
        citations = extract_citation_markers(
            "\n".join(
                [
                    str(getattr(decision, "decision_text", "") or ""),
                    str(getattr(decision, "background", "") or ""),
                    "\n".join(
                        str(getattr(getattr(fu, "citation", ""), "marker", "") or "")
                        for fu in getattr(decision, "follow_ups", []) or []
                    ),
                ]
            )
        )
        follow_ups: list[str] = []
        owners: list[str] = []
        for fu in getattr(decision, "follow_ups", []) or []:
            owner = str(getattr(fu, "owner", "") or "").strip()
            desc = str(getattr(fu, "description", "") or "").strip()
            citation = getattr(fu, "citation", None)
            marker = ""
            if citation is not None:
                marker = (
                    f"[meeting:{getattr(citation, 'meeting_id', meeting_id)}@"
                    f"{getattr(citation, 'timestamp_str', '00:00:00')}]"
                )
                citations.append(marker)
            if owner:
                owners.append(owner)
            if owner and desc:
                follow_ups.append(f"{owner}: {desc} {marker}".strip())

        projects = list(getattr(decision, "projects", []) or [])
        project = str(projects[0]) if projects else ""
        slug = str(getattr(decision, "slug", "") or "decision")
        decision_id = record_id or f"decision-{meeting_date.isoformat()}-{slug}"
        return cls(
            id=decision_id,
            title=str(getattr(decision, "title", "") or "결정사항"),
            status="decided",
            decision_date=meeting_date,
            decision_text=str(getattr(decision, "decision_text", "") or ""),
            background=str(getattr(decision, "background", "") or ""),
            project=project,
            participants=list(getattr(decision, "participants", []) or []),
            owners=owners,
            confidence=int(getattr(decision, "confidence", 0) or 0),
            source_meetings=source_meetings or [meeting_id],
            citations=citations,
            follow_ups=follow_ups,
            created_at=created_at or "",
        )
