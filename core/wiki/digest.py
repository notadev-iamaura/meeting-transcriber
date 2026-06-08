"""C2 — 위키 현황 다이제스트(집계, LLM 0).

위키에 쌓인 결정/액션을 **모델 로드 0**(LLM·임베딩 미사용)으로 순수 집계해
"지금 내 미해결 액션 / 최근 결정 / 프로젝트별 현재 상태" 를 작은 현황판으로 만든다.
디스크 원장(frontmatter + 본문 인용)만 읽으며, 모든 줄은 원본 인용을 그대로 보존한다
(불변식 #1 인용 무결성·#4 코어 모델 로드 0).

집계만 수행한다 — 본문/인용을 생성·변형하지 않고 원문에서 선별·재배치할 뿐이다.
깨진 페이지 1건은 경고 후 skip 하여 전체 집계를 막지 않는다(graceful, #6).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.wiki.models import Citation, PageType

if TYPE_CHECKING:
    from config import WikiDigestConfig
    from core.wiki.store import WikiStore

logger = logging.getLogger(__name__)

# action_items.md 의 `- [ ] {owner}: {desc}{due} {cit}` 라인(_render_open_line 포맷).
_OPEN_SECTION_RE = re.compile(r"^##\s+Open\b")
_SECTION_RE = re.compile(r"^##\s+")
_OPEN_ITEM_RE = re.compile(r"^-\s*\[\s*\]\s*(?P<rest>.+?)\s*$")
# 인용 마커(store._CITATION_PATTERN 과 동일 형식, 전체 브래킷 캡처).
_CITATION_RE = re.compile(r"\[meeting:[A-Za-z0-9_]+@\d{2}:\d{2}:\d{2}\]")
# due 표기: " (due: 2026-05-30)"
_DUE_RE = re.compile(r"\(due:\s*(?P<due>[^)]+?)\s*\)")

_UNASSIGNED = "미지정"


@dataclass(frozen=True)
class OpenAction:
    """미해결(open) 액션 한 건. 원본 인용/라인을 모두 보존한다."""

    owner: str
    description: str
    citations: list[str]  # 라인의 모든 "[meeting:id@HH:MM:SS]"(없으면 빈 리스트)
    due_date: str | None
    raw_line: str  # 파싱 손실 방지를 위한 원본(누락 0 보증)


@dataclass(frozen=True)
class RecentDecision:
    """최근 결정 한 건. 본문 인용을 모두 보존한다."""

    page_path: str
    title: str
    decision_date: str
    status: str
    project: str | None
    citations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProjectStatus:
    """프로젝트별 현재 상태(가장 최근 결정 1건)."""

    project: str
    last_title: str
    last_date: str
    status: str
    page_path: str


@dataclass(frozen=True)
class WikiDigest:
    """현황 다이제스트 집계 결과."""

    open_actions_by_owner: dict[str, list[OpenAction]]
    recent_decisions: list[RecentDecision]
    project_status: list[ProjectStatus]
    total_open_actions: int
    generated_for: str  # 생성 기준일(ISO date)


def _citation_str(c: Citation) -> str:
    """Citation 을 `[meeting:id@HH:MM:SS]` 원문 형식으로 직렬화한다."""
    return f"[meeting:{c.meeting_id}@{c.timestamp_str}]"


def parse_open_actions(action_items_content: str) -> list[OpenAction]:
    """action_items.md 본문의 `## Open` 섹션을 OpenAction 목록으로 파싱한다.

    `## Open` 헤더부터 다음 `##` 섹션 전까지의 모든 `- [ ]` 항목을 집계한다.
    한 항목이 여러 물리 라인에 걸쳐 있어도(LLM 이 개행 포함 description 반환 시)
    다음 `- [ ]`/`##`/빈 줄 전까지를 **하나의 논리 항목으로 병합**해 인용·설명을
    잃지 않는다(인용 무결성·누락 0). owner 구분(`:`)이나 인용이 없는 비정형 항목도
    드롭하지 않고 owner="미지정"/citations=[] 로 보존한다.

    Args:
        action_items_content: action_items.md 전체 본문.

    Returns:
        등장 순서의 OpenAction 리스트(없으면 빈 리스트).
    """
    actions: list[OpenAction] = []
    in_open = False
    buffer: list[str] = []  # 현재 논리 항목을 이루는 물리 라인들

    def _flush() -> None:
        if buffer:
            actions.append(_parse_open_item(list(buffer)))
            buffer.clear()

    for raw in action_items_content.splitlines():
        line = raw.rstrip()
        if _SECTION_RE.match(line):
            _flush()
            in_open = bool(_OPEN_SECTION_RE.match(line))
            continue
        if not in_open:
            continue
        if not line.strip():
            _flush()  # 빈 줄 = 항목 경계
            continue
        if _OPEN_ITEM_RE.match(line):
            _flush()  # 새 `- [ ]` 항목 시작
            buffer.append(line)
        elif buffer:
            buffer.append(line)  # 직전 항목의 연속(continuation) 라인
        # buffer 가 비었고 항목도 아니면 placeholder("_(없음)_") 등 → 무시
    _flush()
    return actions


def _parse_open_item(lines: list[str]) -> OpenAction:
    """논리 항목(1+ 물리 라인)에서 owner·desc·due·citations 을 추출한다(드롭 없음)."""
    raw_line = "\n".join(s.strip() for s in lines).strip()
    head_match = _OPEN_ITEM_RE.match(lines[0])
    head = head_match.group("rest") if head_match else lines[0].strip()
    merged = " ".join([head.strip(), *(s.strip() for s in lines[1:])]).strip()

    # 라인의 모든 인용을 보존(다중 인용 손실 방지 — 불변식 #1).
    citations = _CITATION_RE.findall(merged)

    body = _CITATION_RE.sub("", merged)
    # due 가 여러 번 등장하면 인용 직전(끝)에 가까운 마지막 매치를 마감일로 본다.
    due_matches = _DUE_RE.findall(body)
    due_date = due_matches[-1].strip() if due_matches else None
    body = _DUE_RE.sub("", body).strip()

    # "owner: desc" 분해(콜론 없으면 전체를 desc, owner=미지정 — 누락 0).
    if ":" in body:
        owner, _, desc = body.partition(":")
        owner = owner.strip() or _UNASSIGNED
        description = desc.strip()
    else:
        owner = _UNASSIGNED
        description = body
    return OpenAction(
        owner=owner,
        description=description,
        citations=citations,
        due_date=due_date,
        raw_line=raw_line,
    )


def _iter_decision_pages(store: WikiStore) -> list[Any]:
    """decisions/ 하위 결정 페이지를 읽어 반환한다(깨진 페이지는 skip)."""
    pages: list[Any] = []
    for rel_path in store.all_pages():
        if rel_path.parts and rel_path.parts[0] != "decisions":
            continue
        try:
            page = store.read_page(rel_path)
        except Exception as exc:  # noqa: BLE001 — 깨진 1건이 전체 집계를 막지 않게 skip
            logger.warning("다이제스트: 결정 페이지 읽기 skip %s (%s)", rel_path, exc)
            continue
        if page.page_type is PageType.DECISION:
            pages.append(page)
    return pages


def _fm_str(frontmatter: dict[str, Any], *keys: str) -> str:
    """frontmatter 에서 keys 중 처음으로 값이 있는 것을 문자열로 반환(없으면 "")."""
    for key in keys:
        val = frontmatter.get(key)
        if val not in (None, ""):
            return str(val).strip()
    return ""


def _projects_of(frontmatter: dict[str, Any]) -> list[str]:
    """frontmatter 의 project/projects 를 문자열 리스트로 정규화(다중값 허용)."""
    # search_index._string_list 와 동일 의미(리스트/콤마/단일 scalar 처리).
    raw = frontmatter.get("project")
    if raw in (None, ""):
        raw = frontmatter.get("projects")
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        return [str(v).strip() for v in raw if str(v).strip()]
    text = str(raw).strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [i.strip() for i in inner.split(",") if i.strip()]
    if "," in text:
        return [i.strip() for i in text.split(",") if i.strip()]
    return [text] if text else []


def collect_recent_decisions(
    store: WikiStore, *, now: date, recent_days: int, max_recent: int
) -> list[RecentDecision]:
    """now 기준 recent_days 윈도 안의 결정을 날짜 내림차순으로 모은다(상한 max_recent).

    decision_date 가 비거나 ISO 파싱 불가면 최근 목록에서 제외한다(graceful).
    각 결정의 본문 인용은 그대로 보존한다.
    """
    cutoff = now - timedelta(days=max(1, recent_days))
    recent: list[RecentDecision] = []
    for page in _iter_decision_pages(store):
        fm = page.frontmatter or {}
        date_str = _fm_str(fm, "decision_date", "date")
        parsed = _parse_iso_date(date_str)
        if parsed is None or parsed < cutoff or parsed > now:
            continue
        projects = _projects_of(fm)
        recent.append(
            RecentDecision(
                page_path=str(page.path),
                title=_fm_str(fm, "title") or "(제목 없음)",
                decision_date=date_str,
                status=_fm_str(fm, "status"),
                project=projects[0] if projects else None,
                citations=[_citation_str(c) for c in page.citations],
            )
        )
    recent.sort(key=lambda d: (d.decision_date, d.page_path), reverse=True)
    return recent[: max(1, max_recent)]


def collect_project_status(store: WikiStore) -> list[ProjectStatus]:
    """프로젝트별로 가장 최근(decision_date 최댓값) 결정을 현재 상태로 집계한다.

    project frontmatter 가 없는 결정은 제외한다. 결정이 여러 프로젝트에 속하면
    각 프로젝트 모두에 계상한다. 반환은 프로젝트명 오름차순.
    """
    latest: dict[str, ProjectStatus] = {}
    for page in _iter_decision_pages(store):
        fm = page.frontmatter or {}
        date_str = _fm_str(fm, "decision_date", "date")
        title = _fm_str(fm, "title") or "(제목 없음)"
        status = _fm_str(fm, "status")
        page_path = str(page.path)
        for project in _projects_of(fm):
            current = latest.get(project)
            # (파싱날짜, page_path) 안정 비교 — 문자열 날짜 비교는 불량 날짜("없음" 등)가
            # 한글>숫자로 '최신'을 가로채고, 동일 날짜 tie 는 rglob 순서에 의존해
            # 비결정적이 된다. 파싱 날짜 우선 + page_path 2차 키로 결정성 확보.
            new_key = (_date_key(date_str), page_path)
            if current is None or new_key > (_date_key(current.last_date), current.page_path):
                latest[project] = ProjectStatus(
                    project=project,
                    last_title=title,
                    last_date=date_str,
                    status=status,
                    page_path=page_path,
                )
    return [latest[p] for p in sorted(latest)]


def build_digest(store: WikiStore, *, digest_config: WikiDigestConfig, now: date) -> WikiDigest:
    """미해결 액션·최근 결정·프로젝트 상태를 집계한 WikiDigest 를 만든다(LLM 0).

    Args:
        store: 위키 저장소.
        digest_config: `wiki.digest` 설정(윈도/상한).
        now: 최근성 기준일.

    Returns:
        집계 결과. 액션/결정이 없어도 빈 다이제스트를 graceful 하게 반환.
    """
    open_actions = _read_open_actions(store)
    by_owner: dict[str, list[OpenAction]] = {}
    for action in open_actions:
        by_owner.setdefault(action.owner, []).append(action)
    # owner당 표시 상한 적용(총 카운트는 전체를 보존 — 누락 사실을 숨기지 않음).
    capped = {owner: items[: digest_config.max_per_owner] for owner, items in by_owner.items()}

    recent = collect_recent_decisions(
        store,
        now=now,
        recent_days=digest_config.recent_days,
        max_recent=digest_config.max_recent,
    )
    projects = collect_project_status(store)

    return WikiDigest(
        open_actions_by_owner=capped,
        recent_decisions=recent,
        project_status=projects,
        total_open_actions=len(open_actions),
        generated_for=now.isoformat(),
    )


def _read_open_actions(store: WikiStore) -> list[OpenAction]:
    """action_items.md 를 읽어 미해결 액션을 파싱한다(없으면 빈 리스트)."""
    try:
        page = store.read_page(Path("action_items.md"))
    except Exception as exc:  # noqa: BLE001 — 파일 없음/읽기 실패는 빈 목록(graceful)
        logger.debug("다이제스트: action_items.md 없음/읽기 실패 (%s)", exc)
        return []
    return parse_open_actions(page.content)


def render_digest_markdown(digest: WikiDigest) -> str:
    """WikiDigest 를 `digest.md` 마크다운으로 렌더한다(인용 그대로 노출).

    frontmatter `type: digest` + 3 섹션(미해결 액션·최근 결정·프로젝트 현황).
    """
    parts: list[str] = [
        "---",
        "type: digest",
        f"generated_for: {digest.generated_for}",
        "---",
        "",
        "# 현황 다이제스트",
        "",
        f"_생성 기준일: {digest.generated_for} · 집계(LLM 미사용)_",
        "",
    ]

    # ── 미해결 액션 (owner별) ─────────────────────────────────────────
    parts.append(f"## 미해결 액션 ({digest.total_open_actions})")
    parts.append("")
    if not digest.open_actions_by_owner:
        parts.append("_(없음)_")
        parts.append("")
    else:
        for owner in sorted(digest.open_actions_by_owner):
            items = digest.open_actions_by_owner[owner]
            parts.append(f"### {owner} ({len(items)})")
            for action in items:
                due = f" (due: {action.due_date})" if action.due_date else ""
                cit = (" " + " ".join(action.citations)) if action.citations else ""
                parts.append(f"- {action.description}{due}{cit}")
            parts.append("")

    # ── 최근 결정 ─────────────────────────────────────────────────────
    parts.append(f"## 최근 결정 ({len(digest.recent_decisions)})")
    parts.append("")
    if not digest.recent_decisions:
        parts.append("_(없음)_")
        parts.append("")
    else:
        for dec in digest.recent_decisions:
            proj = f" · {dec.project}" if dec.project else ""
            status = f" · {dec.status}" if dec.status else ""
            cits = (" " + " ".join(dec.citations)) if dec.citations else ""
            parts.append(
                f"- [{_escape_link_text(dec.title)}]({dec.page_path}) "
                f"({dec.decision_date}{proj}{status}){cits}"
            )
        parts.append("")

    # ── 프로젝트별 현황 ───────────────────────────────────────────────
    parts.append(f"## 프로젝트별 현황 ({len(digest.project_status)})")
    parts.append("")
    if not digest.project_status:
        parts.append("_(없음)_")
        parts.append("")
    else:
        for ps in digest.project_status:
            status = f" — {ps.status}" if ps.status else ""
            parts.append(
                f"- **{ps.project}**: [{_escape_link_text(ps.last_title)}]({ps.page_path}) "
                f"({ps.last_date}){status}"
            )
        parts.append("")

    return "\n".join(parts) + "\n"


def _escape_link_text(text: str) -> str:
    """마크다운 링크 텍스트(`[...]`)용 대괄호 이스케이프 — 제목의 `[`/`]` 가 링크를
    깨거나 C3 deep link 파싱을 망가뜨리지 않게 한다(인용 텍스트는 영향 없음)."""
    return text.replace("[", "\\[").replace("]", "\\]")


def _parse_iso_date(value: str) -> date | None:
    """'YYYY-MM-DD' 또는 ISO datetime 문자열을 date 로 파싱(불가 시 None)."""
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _date_key(value: str) -> date:
    """정렬/비교용 날짜 키. 파싱 불가/빈 값은 date.min(가장 과거)으로 강등한다."""
    return _parse_iso_date(value) or date.min
