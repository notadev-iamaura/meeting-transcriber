"""누적 Wiki의 현재 상태·과거 원장·검토 후보 보존 회귀 테스트."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.wiki.compiler import WikiCompilerV2
from core.wiki.digest import collect_project_status, collect_recent_decisions
from core.wiki.extractors.person import ExtractedPerson, PersonExtractor
from core.wiki.extractors.project import ExtractedProject, ProjectExtractor, TimelineEntry
from core.wiki.guard import GuardVerdict
from core.wiki.models import Citation
from core.wiki.store import WikiStore, WikiStoreError


def _project_md(*, updated: str = "2026-06-01", status: str = "blocked") -> str:
    """모델이 누락하기 쉬운 기존 원장 필드를 포함한다."""
    return f"""---
type: project
slug: apollo
status: {status}
owner: 민수
started: 2026-01-01
target: 2026-12-01
created_at: 2026-01-01T10:00:00
last_updated: {updated}
---

# Apollo

## 현재 상태
이전 상태 [meeting:old@00:01:00]

## 진행 타임라인
- 2026-06-01: 장애 확인 [meeting:old@00:01:00]

## 미해결 이슈
- 승인 대기 [meeting:old@00:01:00]

<!-- confidence: 9 -->
"""


def _project() -> ExtractedProject:
    """신규 발화가 기존 status를 묵시적으로 바꾸지 못하게 하는 입력."""
    return ExtractedProject(
        name="Apollo",
        slug="apollo",
        status="in-progress",
        owner=None,
        started=None,
        target=None,
        description="진행 보고 [meeting:new@00:01:00]",
        timeline_entry=TimelineEntry("", "계획 검토", Citation("new", "00:01:00", 60)),
        confidence=9,
    )


def test_검토와_대체된_결정이_현재_확정_현황을_가로채지_않는다(tmp_path: Path) -> None:
    """원본을 삭제하지 않고 현재 확정 결정만 다이제스트로 선별한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    for number, status in enumerate(("decided", "pending", "rejected", "superseded", "proposed")):
        store.write_page(
            Path(f"decisions/{number}.md"),
            f"""---
type: decision
title: {status}
status: {status}
decision_date: 2026-06-0{number + 1}
project: Apollo
---
결정 [meeting:old@00:01:00]
""",
        )
    original = {path: store.read_page(path).content for path in store.all_pages()}
    assert [
        item.title
        for item in collect_recent_decisions(
            store, now=date(2026, 6, 8), recent_days=30, max_recent=50
        )
    ] == ["decided"]
    assert collect_project_status(store)[0].last_title == "decided"
    assert {path: store.read_page(path).content for path in store.all_pages()} == original


def test_대체_대상이_명시된_결정은_현재_현황에서_제외한다(tmp_path: Path) -> None:
    """status가 과거 스키마 confirmed여도 superseded_by가 있으면 과거 기록이다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("decisions/old.md"),
        """---
type: decision
title: 과거 결정
status: confirmed
superseded_by: decision-new
decision_date: 2026-06-01
project: Apollo
---
과거 결정 [meeting:old@00:01:00]
""",
    )
    assert collect_project_status(store) == []


def test_실제_저장소에서_기존_프로젝트_메타데이터를_잃지_않고_읽는다(tmp_path: Path) -> None:
    """body-only WikiPage.content가 기존 status를 사라지게 하던 회귀를 막는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("projects/apollo.md"), _project_md())
    existing = ProjectExtractor.load_existing_projects(
        store, [_project()], meeting_date=date(2026, 6, 2)
    )
    assert len(existing) == 1
    assert existing[0].status == "blocked"
    assert existing[0].owner == "민수"
    assert "slug: apollo" in existing[0].raw_content


@pytest.mark.asyncio
async def test_과거_회의는_현재_프로젝트를_되돌리거나_모델을_호출하지_않는다(
    tmp_path: Path,
) -> None:
    """역순 백필은 이후 회의에서 갱신한 상태를 보존한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("projects/apollo.md"), _project_md(updated="2026-06-10"))
    llm = SimpleNamespace(generate=AsyncMock())
    extractor = ProjectExtractor(llm)
    result = await extractor.render_or_update_pages(
        projects=[_project()],
        status_transitions={"apollo": "shipped"},
        meeting_id="new",
        meeting_date=date(2026, 6, 2),
        existing_store=store,
        meeting_decisions=[],
        meeting_new_actions=[],
        existing_open_actions=[],
    )
    assert result == []
    llm.generate.assert_not_awaited()
    assert store.read_page(Path("projects/apollo.md")).frontmatter["status"] == "blocked"


@pytest.mark.asyncio
async def test_전환_근거없는_status와_모델이_누락한_누적_정보를_보존한다(tmp_path: Path) -> None:
    """명시적 전환 없이 status를 바꾸지 않고 과거 이슈·타임라인을 복원한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("projects/apollo.md"), _project_md())
    llm = SimpleNamespace(
        generate=AsyncMock(
            return_value="""---
type: project
slug: apollo
status: shipped
last_updated: 1900-01-01
---
# Apollo

## 진행 타임라인
- 모델이 과거를 누락했다 [meeting:new@00:01:00]

## 미해결 이슈
없음

<!-- confidence: 9 -->
"""
        )
    )
    pages = await ProjectExtractor(llm).render_or_update_pages(
        projects=[_project()],
        status_transitions={},
        meeting_id="new",
        meeting_date=date(2026, 6, 2),
        existing_store=store,
        meeting_decisions=[],
        meeting_new_actions=[],
        existing_open_actions=[],
    )
    assert len(pages) == 1
    content = pages[0][1]
    assert "status: blocked" in content
    assert "last_updated: 2026-06-02" in content
    assert "owner: 민수" in content
    assert "target: 2026-12-01" in content
    assert "created_at: 2026-01-01T10:00:00" in content
    assert "장애 확인 [meeting:old@00:01:00]" in content
    assert "승인 대기 [meeting:old@00:01:00]" in content
    assert "2026-06-02: 계획 검토 [meeting:new@00:01:00]" in content
    assert "<!-- confidence: 9 -->" in content


@pytest.mark.asyncio
@pytest.mark.parametrize("citation_ts", [None, "00:10:00", "00:99:00"])
async def test_상태_전환의_근거시각이_없거나_잘못되면_기존상태를_유지한다(
    tmp_path: Path, citation_ts: str | None
) -> None:
    """모델 자기 점수가 높아도 실제 발화를 가리키지 못하는 전환은 적용하지 않는다."""
    import json

    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("projects/apollo.md"), _project_md())
    previous = ProjectExtractor.load_existing_projects(
        store, [_project()], meeting_date=date(2026, 6, 2)
    )
    llm = SimpleNamespace(
        generate=AsyncMock(
            return_value=json.dumps(
                [
                    {
                        "slug": "apollo",
                        "new_status": "shipped",
                        "reason_citation_ts": citation_ts,
                        "confidence": 10,
                    }
                ]
            )
        )
    )
    transitions = await ProjectExtractor(llm).detect_status_transitions(
        existing_projects=previous,
        meeting_id="new",
        utterances=[{"text": "진행 중입니다.", "start": 60, "end": 70}],
    )
    assert transitions == {}


@pytest.mark.asyncio
async def test_실재하는_상태전환_근거가_본문과_메타데이터에_함께_남는다(tmp_path: Path) -> None:
    """기존 상태 전환은 별도 인용을 보존해 내용 검증 단계에서 다시 검사할 수 있다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("projects/apollo.md"), _project_md())
    previous = ProjectExtractor.load_existing_projects(
        store, [_project()], meeting_date=date(2026, 6, 2)
    )
    llm = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                '[{"slug":"apollo","new_status":"shipped","reason_citation_ts":"00:01:00","confidence":9}]',
                "---\ntype: project\nslug: apollo\nstatus: in-progress\n---\n# Apollo\n\n<!-- confidence: 9 -->\n",
            ]
        )
    )
    extractor = ProjectExtractor(llm)
    transitions = await extractor.detect_status_transitions(
        existing_projects=previous,
        meeting_id="new",
        utterances=[{"text": "Apollo 출시 완료했습니다.", "start": 60, "end": 70}],
    )
    assert transitions == {"apollo": "shipped"}
    result = await extractor.render_or_update_pages(
        projects=[_project()],
        status_transitions=transitions,
        meeting_id="new",
        meeting_date=date(2026, 6, 2),
        existing_store=store,
        meeting_decisions=[],
        meeting_new_actions=[],
        existing_open_actions=[],
    )
    assert "status: shipped" in result[0][1]
    assert "프로젝트 상태: 출시 완료 (shipped). [meeting:new@00:01:00]" in result[0][1]


@pytest.mark.parametrize(
    "reason", ["unsupported_claim", "grounding_unavailable", "grounding_limit"]
)
def test_내용_검증_실패는_공개본_보존후_검토후보로_남는다(tmp_path: Path, reason: str) -> None:
    """오류·한도·불일치에서 기존 원장을 바꾸지 않고 근거 후보를 보존한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    path = Path("decisions/budget.md")
    original = "---\ntype: decision\nstatus: decided\n---\n기존 결정\n"
    candidate = "---\ntype: decision\nstatus: decided\n---\n새 결정\n"
    store.write_page(path, original)
    created: list[str] = []
    updated: list[str] = []
    pending: list[str] = []
    rejected: list[tuple[str, str]] = []
    WikiCompilerV2._dispatch_page_by_verdict(
        rel_path=str(path),
        content=candidate,
        verdict=GuardVerdict(False, reason, 9),
        store=store,
        pages_created=created,
        pages_updated=updated,
        pages_pending=pending,
        pages_rejected=rejected,
    )
    assert not created and not updated and not rejected
    assert pending == [str(path)]
    assert "기존 결정" in store.read_page(path).content
    review = store.read_page(Path("pending") / path)
    assert review.frontmatter["status"] == "pending"
    assert review.frontmatter["review_reason"] == reason


def test_검토후보_저장실패를_성공한_pending으로_표시하지_않는다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """디스크 오류를 성공으로 보고해 후보 유실을 숨기지 않는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()

    def fail_write(*_args: object, **_kwargs: object) -> None:
        """저장 실패를 재현한다."""
        raise WikiStoreError("write_failed")

    monkeypatch.setattr(store, "write_page", fail_write)
    pending: list[str] = []
    rejected: list[tuple[str, str]] = []
    WikiCompilerV2._dispatch_page_by_verdict(
        rel_path="decisions/a.md",
        content="---\ntype: decision\n---\n본문",
        verdict=GuardVerdict(False, "grounding_unavailable", 9),
        store=store,
        pages_created=[],
        pages_updated=[],
        pages_pending=pending,
        pages_rejected=rejected,
    )
    assert pending == []
    assert rejected == [("decisions/a.md", "pending_write_failed")]


def test_사용자_수정본_충돌은_보존하고_새_후보는_별도로_저장한다(tmp_path: Path) -> None:
    """수정 보호 표시를 생성기가 복사해도 사람의 원장을 덮어쓰지 않는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    path = Path("decisions/a.md")
    store.write_page(path, "---\ntype: decision\nhuman_edited: true\n---\n사람 수정\n")
    pending: list[str] = []
    rejected: list[tuple[str, str]] = []
    content = (
        "---\ntype: decision\nstatus: decided\nhuman_reviewed: true\n"
        "human_updated_at: invented\n---\nAI 제안\n"
    )
    for suffix in ("첫 후보", "다른 후보"):
        WikiCompilerV2._dispatch_page_by_verdict(
            rel_path=str(path),
            content=content + suffix,
            verdict=GuardVerdict(True, "passed", 9),
            store=store,
            pages_created=[],
            pages_updated=[],
            pages_pending=pending,
            pages_rejected=rejected,
        )
    assert pending == [str(path), str(path)]
    assert not rejected
    assert "사람 수정" in store.read_page(path).content
    candidates = [p for p in store.all_pages() if p.parts[0] == "pending"]
    assert len(candidates) == 2
    for candidate in candidates:
        metadata = store.read_page(candidate).frontmatter
        assert not any(key.startswith("human_") for key in metadata)
        assert metadata["review_reason"] == "human_protected"


@pytest.mark.asyncio
async def test_인물_누적이_실제_저장소에서_역할_날짜_주제와_참석횟수를_보존한다(
    tmp_path: Path,
) -> None:
    """원문 메타데이터 유실과 주제 인용 삭제, 재처리 참석 횟수 증가를 함께 막는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    path = Path("people/민수.md")
    store.write_page(
        path,
        """---
type: person
name: 민수
role: PM
first_seen: 2026-01-01
last_seen: 2026-06-01
meetings_count: 3
---
# 민수

## 자주 언급하는 주제
- 기존 주제 [meeting:old@00:01:00] [meeting:older@00:00:10]

<!-- confidence: 9 -->
""",
    )
    person = ExtractedPerson(
        name="민수",
        name_normalized="민수",
        role=None,
        first_seen_meeting_id="new",
        first_seen_date="2026-06-02",
        last_seen_meeting_id="new",
        last_seen_date="2026-06-02",
        confidence=9,
    )
    llm = SimpleNamespace(generate=AsyncMock(return_value='{"role_update":null,"new_topics":[]}'))
    extractor = PersonExtractor(llm)
    for _ in range(2):
        result = await extractor.render_or_update_pages(
            persons=[person],
            meeting_id="new",
            meeting_date=date(2026, 6, 2),
            existing_store=store,
            meeting_decisions=[],
            meeting_new_actions=[],
            existing_open_actions=[],
        )
        assert len(result) == 1
        store.write_page(path, result[0][1])
        page = store.read_page(path)
        assert page.frontmatter["role"] == "PM"
        assert page.frontmatter["first_seen"] == "2026-01-01"
        assert page.frontmatter["last_seen"] == "2026-06-02"
        assert page.frontmatter["meetings_count"] == 4
        assert "[meeting:old@00:01:00] [meeting:older@00:00:10]" in page.content


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unreadable", "malformed"])
async def test_읽을수없거나_깨진_인물원장은_신규페이지로_덮어쓰지_않는다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """불명확한 메타데이터와 디스크 읽기 오류를 부재로 오인하지 않는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    path = Path("people/민수.md")
    store.write_page(path, "---\ntype: person\nname: 민수\n---\n이전 원장\n")
    if failure == "unreadable":

        def fail_read(_path: Path) -> str:
            """읽기 오류를 재현한다."""
            raise WikiStoreError("read_failed")

        monkeypatch.setattr(store, "read_raw_page", fail_read)
    llm = SimpleNamespace(generate=AsyncMock())
    person = ExtractedPerson(
        name="민수",
        name_normalized="민수",
        role=None,
        first_seen_meeting_id="new",
        first_seen_date="2026-06-02",
        last_seen_meeting_id="new",
        last_seen_date="2026-06-02",
        confidence=9,
    )
    assert (
        await PersonExtractor(llm).render_or_update_pages(
            persons=[person],
            meeting_id="new",
            meeting_date=date(2026, 6, 2),
            existing_store=store,
            meeting_decisions=[],
            meeting_new_actions=[],
            existing_open_actions=[],
        )
        == []
    )
    llm.generate.assert_not_awaited()
