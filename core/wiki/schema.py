"""Wiki 페이지 템플릿 및 스키마 정의 모듈

목적: PRD §4.2 의 페이지 템플릿(decisions/people/projects/topics/action_items/index)
과 §4.3 의 인용 형식 표준 + LLM 시스템 프롬프트(`CLAUDE.md`) 를 마크다운으로
렌더링한다. 모든 템플릿은 frontmatter + 본문 형식을 지키며, 인용 마커는
WikiCompiler 가 LLM 호출로 채우므로 템플릿 단계에서는 placeholder 만 둔다.

주요 기능:
    - generate_schema_md(): wiki/CLAUDE.md 의 전체 텍스트 (LLM 시스템 프롬프트)
    - render_decision_template(...): decisions/YYYY-MM-DD-{slug}.md 신규 페이지
    - render_person_template(...): people/{name}.md
    - render_project_template(...): projects/{slug}.md
    - render_topic_template(...): topics/{concept}.md
    - render_action_items_template(...): action_items.md (단일 파일)
    - render_index_md(pages_metadata): index.md 자동 갱신용

의존성: 표준 라이브러리(datetime, logging) + core.wiki.models. 외부 템플릿 엔진
사용 안 함 — 의존성 0 원칙.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from core.wiki.models import PageType

logger = logging.getLogger(__name__)


# 허용된 프로젝트 status 값 — PRD §4.2 projects frontmatter 에서 4개만 허용
_VALID_PROJECT_STATUSES: frozenset[str] = frozenset(
    {"in-progress", "blocked", "shipped", "cancelled"}
)


def generate_schema_md() -> str:
    """`wiki/CLAUDE.md` 의 전체 텍스트를 생성한다.

    이 파일은 WikiCompiler 가 EXAONE 에 매번 시스템 프롬프트로 주입하는 정전(canon).
    PRD §4.3 인용 형식 표준 + 페이지 템플릿 요약 + LLM 행동 규칙을 포함한다.

    Returns:
        UTF-8 마크다운 문자열.
    """
    # 의도적으로 raw triple-quoted string 사용 — f-string 으로 인한 중괄호 이스케이프 회피
    return r"""# Wiki 작성 규칙 (LLM 시스템 프롬프트)

이 문서는 WikiCompiler 가 매 LLM 호출 시 시스템 프롬프트로 주입하는 정전(canon)
입니다. 모든 사실 진술은 출처 인용을 가지며, 한국어 고유명사는 외국어 병기 없이
원문 그대로 작성합니다.

## 1. 인용 형식 표준

모든 사실 진술은 다음 형식의 인용 마커를 최소 1개 이상 가져야 합니다.

    [meeting:{id}@HH:MM:SS]

- `id`: 8자리 소문자 hex (예: `abc12345`)
- `HH:MM:SS`: 발화 시점의 시·분·초 (각 자릿수 2자리 고정)

검증 정규식:

    \[meeting:([a-f0-9]{8})@(\d{2}):(\d{2}):(\d{2})\]

면제 대상 (인용 불필요):
- 마크다운 제목 (`#`, `##`, ...)
- YAML frontmatter (`---` 사이)
- 페이지 간 상대 링크 (`[../people/철수.md]`)
- HTML 주석 (`<!-- confidence: 9 -->`)
- 코드블록 (` ``` `)
- 표 구분자 (`|---|---|`)

## 2. 페이지 종류별 frontmatter 스키마

### decisions
    type: decision
    meeting_id: <8 hex>
    date: YYYY-MM-DD
    status: confirmed | superseded
    participants: [이름, ...]
    projects: [slug, ...]

### people
    type: person
    name: <이름>
    role: <역할>
    first_seen: YYYY-MM-DD
    last_seen: YYYY-MM-DD
    meetings_count: <int>

### projects
    type: project
    slug: <slug>
    status: in-progress | blocked | shipped | cancelled
    owner: <이름>
    started: YYYY-MM-DD
    target: YYYY-MM-DD
    last_updated: YYYY-MM-DD

### topics
    type: topic
    concept: <식별자>
    mention_count: <int>

### action_items
    type: action_items
    last_compiled: <ISO8601>

## 3. LLM 작성 규칙

1. **인용 강제**: 사실을 주장하는 모든 줄은 위 인용 마커를 포함해야 합니다.
   인용 없이 작성된 사실 문장은 D1 후처리에서 자동 제거됩니다.

2. **한국어 고유명사 외국어 병기 금지**: `배미령(Baimilong)`, `김철수(Kim Cheol-su)`
   같은 영어/중국어 병기를 절대 추가하지 마십시오. 한국어 고유명사는 한글
   원문 그대로 작성하며, 영문 표기가 필요한 외래어 또는 영문 약어(API, PM 등)
   는 그대로 둡니다.

3. **confidence 마커**: 페이지 마지막에 `<!-- confidence: <0~10> -->` 형식의
   HTML 주석으로 신뢰도를 표기합니다. 누락되거나 정수가 아니면 D3 검증
   실패로 페이지가 거부됩니다.

4. **모순 처리 정책**: 기존 결정이 후속 회의에서 뒤집혔을 때는 원문을 삭제하지
   말고 `~~취소선~~` 으로 마크하되 인용은 유지합니다. 새 결정은 별도 줄로
   추가하고 자체 인용을 가집니다.

## 4. 출력 형식

- UTF-8 마크다운
- frontmatter 는 파일 최상단(`---` 사이)에 배치
- 본문은 `## ` 헤더로 섹션 구분
- 파일 끝에 빈 줄 1개

위 규칙을 따르지 않으면 5중 방어(D1~D5) 중 하나에서 거부되며, 페이지가 위키에
반영되지 않습니다.
"""


def _format_yaml_list(values: list[str] | None) -> str:
    """YAML 리스트 표기를 인라인 형식으로 직렬화한다.

    Args:
        values: 문자열 리스트. None 또는 빈 리스트면 `[]` 반환.

    Returns:
        `[a, b, c]` 형태의 문자열.
    """
    if not values:
        return "[]"
    return "[" + ", ".join(values) + "]"


def render_decision_template(
    *,
    meeting_id: str,
    date: str,
    title: str,
    participants: list[str] | None = None,
    projects: list[str] | None = None,
    confidence: int = 0,
    created_at: datetime | None = None,
) -> str:
    """`decisions/YYYY-MM-DD-{slug}.md` 새 페이지의 초기 텍스트를 렌더링한다.

    PRD §4.2 의 decisions 템플릿을 1:1 재현. frontmatter 의 status 는 신규 작성
    시 항상 `confirmed`.

    Args:
        meeting_id: 8자리 hex.
        date: ISO 날짜 문자열.
        title: 결정 제목.
        participants: 참여 화자 이름 목록. None 또는 빈 리스트 허용.
        projects: 관련 프로젝트 slug 목록. None 또는 빈 리스트 허용.
        confidence: 0~10 정수 (D3 임계 비교용).
        created_at: 페이지 생성 시각. None 이면 datetime.now() 사용.

    Returns:
        frontmatter + 본문 placeholder 가 포함된 마크다운 문자열.
    """
    # 사용 변수에서 created_at 참조 — 향후 frontmatter 확장 시 활용
    _ = created_at or datetime.now()

    participants_yaml = _format_yaml_list(participants)
    projects_yaml = _format_yaml_list(projects)

    return (
        f"---\n"
        f"type: decision\n"
        f"meeting_id: {meeting_id}\n"
        f"date: {date}\n"
        f"status: confirmed\n"
        f"participants: {participants_yaml}\n"
        f"projects: {projects_yaml}\n"
        f"---\n"
        f"\n"
        f"# {title}\n"
        f"\n"
        f"## 결정 내용\n"
        f"\n"
        f"## 배경\n"
        f"\n"
        f"## 후속 액션\n"
        f"\n"
        f"## 참고 회의\n"
        f"\n"
        f"<!-- confidence: {confidence} -->\n"
    )


def render_person_template(
    *,
    name: str,
    role: str | None = None,
    first_seen: str | None = None,
    last_seen: str | None = None,
    meetings_count: int = 0,
) -> str:
    """`people/{name}.md` 새 페이지의 초기 텍스트를 렌더링한다.

    PRD §4.2 의 people 템플릿을 1:1 재현.

    Args:
        name: 화자 이름.
        role: 역할. None 이면 frontmatter 에서 생략.
        first_seen, last_seen: ISO 날짜 문자열.
        meetings_count: 누적 등장 회의 수.

    Returns:
        frontmatter + 본문 placeholder.
    """
    # frontmatter 행을 줄 리스트로 누적 — None 인 키는 생략
    fm_lines: list[str] = ["---", "type: person", f"name: {name}"]
    if role is not None:
        fm_lines.append(f"role: {role}")
    if first_seen is not None:
        fm_lines.append(f"first_seen: {first_seen}")
    if last_seen is not None:
        fm_lines.append(f"last_seen: {last_seen}")
    fm_lines.append(f"meetings_count: {meetings_count}")
    fm_lines.append("---")

    frontmatter = "\n".join(fm_lines)

    return (
        f"{frontmatter}\n"
        f"\n"
        f"# {name}\n"
        f"\n"
        f"## 최근 결정\n"
        f"\n"
        f"## 담당 프로젝트\n"
        f"\n"
        f"## 자주 언급하는 주제\n"
        f"\n"
        f"## 미해결 액션아이템\n"
    )


def render_project_template(
    *,
    slug: str,
    title: str | None = None,
    status: str = "in-progress",
    owner: str | None = None,
    started: str | None = None,
    target: str | None = None,
    last_updated: str | None = None,
) -> str:
    """`projects/{slug}.md` 새 페이지의 초기 텍스트를 렌더링한다.

    PRD §4.2 의 projects 템플릿을 1:1 재현.

    Args:
        slug: URL safe 식별자.
        title: 사람이 읽는 제목. None 이면 slug 그대로 사용.
        status: "in-progress" | "blocked" | "shipped" | "cancelled" 중 하나.
        owner: 담당자 이름. None 허용.
        started, target: ISO 날짜 문자열.
        last_updated: ISO 날짜 문자열.

    Returns:
        frontmatter + 본문 placeholder.

    Raises:
        ValueError: status 가 허용 4종 외의 값일 때.
    """
    # status 화이트리스트 검증 — 오탈자/대소문자 불일치도 즉시 거부
    if status not in _VALID_PROJECT_STATUSES:
        raise ValueError(
            f"잘못된 status 값: '{status}'. "
            f"허용: {sorted(_VALID_PROJECT_STATUSES)}"
        )

    display_title = title if title is not None else slug

    # frontmatter 누적 — None 키 생략
    fm_lines: list[str] = [
        "---",
        "type: project",
        f"slug: {slug}",
        f"status: {status}",
    ]
    if owner is not None:
        fm_lines.append(f"owner: {owner}")
    if started is not None:
        fm_lines.append(f"started: {started}")
    if target is not None:
        fm_lines.append(f"target: {target}")
    if last_updated is not None:
        fm_lines.append(f"last_updated: {last_updated}")
    fm_lines.append("---")

    frontmatter = "\n".join(fm_lines)

    return (
        f"{frontmatter}\n"
        f"\n"
        f"# {display_title}\n"
        f"\n"
        f"## 현재 상태\n"
        f"\n"
        f"## 최근 결정\n"
        f"\n"
        f"## 진행 타임라인\n"
        f"\n"
        f"## 미해결 이슈\n"
        f"\n"
        f"## 참여자\n"
    )


def render_topic_template(
    *,
    concept: str,
    mention_count: int = 0,
) -> str:
    """`topics/{concept}.md` 새 페이지의 초기 텍스트를 렌더링한다.

    Args:
        concept: 주제 식별자.
        mention_count: 등장 회의 수.

    Returns:
        frontmatter + 본문 placeholder.

    Raises:
        ValueError: concept 이 빈 문자열 또는 공백만 있는 경우.
    """
    # 입력 검증 — concept 은 파일명으로 사용되므로 빈 값 금지
    if not concept or not concept.strip():
        raise ValueError("concept 은 빈 문자열이거나 공백만 있어선 안 됩니다")

    return (
        f"---\n"
        f"type: topic\n"
        f"concept: {concept}\n"
        f"mention_count: {mention_count}\n"
        f"---\n"
        f"\n"
        f"# {concept}\n"
        f"\n"
        f"## 요약\n"
        f"\n"
        f"## 등장 회의\n"
        f"\n"
        f"## 관련 결정\n"
    )


def render_action_items_template(
    *,
    last_compiled: datetime | None = None,
) -> str:
    """`action_items.md` 단일 파일의 초기 텍스트를 렌더링한다.

    PRD §4.2 의 action_items 템플릿을 1:1 재현.

    Args:
        last_compiled: 마지막 컴파일 시각. None 이면 datetime.now() 사용.

    Returns:
        frontmatter + "## Open (0)" + "## Closed (0)" placeholder.
    """
    compiled_at = last_compiled or datetime.now()
    iso_str = compiled_at.isoformat()

    return (
        f"---\n"
        f"type: action_items\n"
        f"last_compiled: {iso_str}\n"
        f"---\n"
        f"\n"
        f"# Action Items\n"
        f"\n"
        f"## Open (0)\n"
        f"\n"
        f"## Closed (0)\n"
    )


def render_index_md(pages_metadata: dict[PageType, list[dict[str, Any]]]) -> str:
    """`index.md` 카탈로그를 자동 생성한다.

    PRD §4.2 의 index.md 템플릿을 1:1 재현. 카테고리(decisions/people/projects/
    topics/action_items)별로 그룹화되며 각 카테고리 내부는 `last_updated`
    내림차순 정렬.

    Args:
        pages_metadata: PageType 별 페이지 메타데이터 딕셔너리.
            각 항목은 {"path": str, "type": PageType | str, "title": str,
            "last_updated": str (ISO date)} 를 포함.

    Returns:
        마크다운 문자열.
    """
    # PRD §4.2 카테고리 순서 (Decisions → People → Projects → Topics → Action Items)
    category_order: list[tuple[PageType, str]] = [
        (PageType.DECISION, "Decisions"),
        (PageType.PERSON, "People"),
        (PageType.PROJECT, "Projects"),
        (PageType.TOPIC, "Topics"),
        (PageType.ACTION_ITEMS, "Action Items"),
    ]

    parts: list[str] = ["# Index", ""]

    for page_type, label in category_order:
        # 해당 카테고리 페이지가 없으면 헤더는 출력하되 비어있는 상태로 표시
        items = pages_metadata.get(page_type, [])
        parts.append(f"## {label}")
        parts.append("")

        if not items:
            parts.append("_(없음)_")
            parts.append("")
            continue

        # last_updated 내림차순 정렬 — 빈 문자열은 가장 뒤로
        sorted_items = sorted(
            items,
            key=lambda x: x.get("last_updated", ""),
            reverse=True,
        )

        for item in sorted_items:
            title = item.get("title", "(제목 없음)")
            path = item.get("path", "")
            last_updated = item.get("last_updated", "")
            # 추가 메타 — meetings_count, open/closed 등 카테고리별 표시
            meta_suffix = ""
            if page_type == PageType.PERSON and "meetings_count" in item:
                meta_suffix = f" — {item['meetings_count']}회"
            elif page_type == PageType.ACTION_ITEMS:
                # open/closed 카운트가 있으면 부가 표기
                open_count = item.get("open")
                closed_count = item.get("closed")
                if open_count is not None or closed_count is not None:
                    meta_suffix = (
                        f" — Open {open_count or 0} / Closed {closed_count or 0}"
                    )

            parts.append(f"- [{title}]({path}) ({last_updated}){meta_suffix}")

        parts.append("")

    return "\n".join(parts) + "\n"
