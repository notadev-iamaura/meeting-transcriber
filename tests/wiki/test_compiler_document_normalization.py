"""모델의 전체 문서 code fence가 검토 메타데이터를 숨기던 회귀를 검증한다."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import WikiConfig
from core.wiki.compiler import (
    WikiCompilerV2,
    _normalize_generated_document,
    _prepare_pending_content,
)
from core.wiki.extractors.project import ExtractedProject, ProjectExtractor
from core.wiki.guard import GuardVerdict
from core.wiki.llm_client import WikiLLMError
from core.wiki.store import WikiStore, _parse_frontmatter

PROJECT = """---
type: project
slug: sample
status: in-progress
owner: 담당자
last_updated: 2026-11-04
---
# 예시 프로젝트

## 현재 상태
검토 중 [meeting:sample@00:00:20]

<!-- confidence: 7 -->
"""


def _project() -> ExtractedProject:
    """기존 원장 필드를 보존해야 하는 최소 프로젝트 후보를 만든다."""
    return ExtractedProject(
        name="예시 프로젝트",
        slug="sample",
        status="in-progress",
        owner=None,
        started=None,
        target=None,
        description="검토 중",
        confidence=9,
    )


@pytest.mark.parametrize("language", ["markdown", "md", "", "MARKDOWN"])
def test_전체_문서_wrapper만_제거하고_메타데이터와_본문을_보존한다(language: str) -> None:
    """실제 모델처럼 문서를 감싸도 frontmatter·인용·confidence가 그대로 드러난다."""
    wrapped = f"\n```{language}\n{PROJECT}```\n\n"
    assert _normalize_generated_document(wrapped) == PROJECT
    metadata, body = _parse_frontmatter(_normalize_generated_document(wrapped))
    assert metadata == _parse_frontmatter(PROJECT)[0]
    assert body == _parse_frontmatter(PROJECT)[1]


def test_본문의_정상_코드블록과_원래_문서는_그대로_유지한다() -> None:
    """더 긴 바깥 fence 안의 코드와 인용 예시는 내용 변경 없이 보존한다."""
    document = PROJECT + '\n```python\nresult = "[meeting:sample@00:00:20]"\n```\n'
    assert _normalize_generated_document(document) == document
    assert _normalize_generated_document(f"````markdown\n{document}````\n") == document
    tilde_document = PROJECT + "\n~~~python\nvalue = 1\n~~~\n"
    assert _normalize_generated_document(f"```md\n{tilde_document}```\n") == tilde_document


def test_CRLF_문서의_본문과_개행을_보존한다() -> None:
    """Windows 형태 응답도 문서 내부 개행을 재작성하지 않는다."""
    document = PROJECT.replace("\n", "\r\n")
    assert _normalize_generated_document(f"```md\r\n{document}```\r\n") == document


@pytest.mark.parametrize(
    "wrapped",
    [
        f"```markdown\n{PROJECT}",
        f"```markdown\n{PROJECT}```\n뒤에 추가한 문장",
        f"설명 문장\n```markdown\n{PROJECT}```\n",
        f"```markdown\n{PROJECT}```\n```markdown\n{PROJECT}```\n",
        f"```\n{PROJECT}```\n```\n{PROJECT}```\n",
        f"```python\n{PROJECT}```\n",
        f"~~~markdown\n{PROJECT}~~~\n",
        "```markdown\n---\n닫히지 않은 frontmatter\n```\n",
        f"```markdown\n{PROJECT}\n```python\nvalue = 1\n```\n```\n",
    ],
)
def test_부분_여러문서_모호한_wrapper는_보수적으로_거부한다(wrapped: str) -> None:
    """내용을 추측해서 합치거나 첫 블록만 게시하지 않는다."""
    with pytest.raises(ValueError, match="^malformed_document$"):
        _normalize_generated_document(wrapped)


@pytest.mark.parametrize("status", ["in-progress", "blocked", "shipped", "cancelled"])
def test_pending의_게시상태와_프로젝트_후보상태를_구분해_보존한다(status: str) -> None:
    """검토 후보를 pending으로 격리하면서 원래 제안된 프로젝트 상태도 남긴다."""
    document = PROJECT.replace("status: in-progress", f"status: {status}")
    result = _prepare_pending_content(document, "low_confidence")
    metadata, body = _parse_frontmatter(result)
    assert metadata["type"] == "project"
    assert metadata["status"] == "pending"
    assert metadata["proposed_status"] == status
    assert metadata["review_reason"] == "low_confidence"
    assert metadata["owner"] == "담당자"
    assert body == _parse_frontmatter(document)[1]
    assert _prepare_pending_content(result, "low_confidence") == result


def test_결정후보에_프로젝트_상태필드를_추가하지_않는다() -> None:
    """프로젝트 상태 보존 범위를 결정 문서로 넓히지 않는다."""
    metadata, _ = _parse_frontmatter(
        _prepare_pending_content(
            PROJECT.replace("type: project", "type: decision"), "unsupported_claim"
        )
    )
    assert "proposed_status" not in metadata


def _compiler(tmp_path: Path, content: str) -> tuple[WikiCompilerV2, WikiStore, MagicMock]:
    """실제 저장·분류 경로를 사용하고 추출·모델 호출만 대체한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    guard = MagicMock()
    guard.verify = AsyncMock(return_value=GuardVerdict(False, "low_confidence", 7))
    decision = MagicMock(
        extract=AsyncMock(return_value=[object()]),
        render_pages=AsyncMock(return_value=[("projects/sample.md", content)]),
    )
    action = MagicMock(
        extract_new=AsyncMock(return_value=[]),
        detect_closed=AsyncMock(return_value=[]),
        render_unified_page=AsyncMock(side_effect=WikiLLMError("테스트에서는 액션 렌더 생략")),
    )
    person = MagicMock(extract_speakers=AsyncMock(return_value=[]))
    project = MagicMock(
        extract_projects=AsyncMock(return_value=[]),
        detect_status_transitions=AsyncMock(return_value={}),
    )
    compiler = WikiCompilerV2(
        config=WikiConfig(root=store.root),
        store=store,
        llm=MagicMock(calls=[]),
        guard=guard,
        decision_extractor=decision,
        action_item_extractor=action,
        person_extractor=person,
        project_extractor=project,
    )
    return compiler, store, guard


@pytest.mark.asyncio
async def test_프로젝트_부분인용복구가_남긴_주장은_삭제하지_않고_보류한다(
    tmp_path: Path, monkeypatch
) -> None:
    """명시 플래그가 없어도 미복구 본문을 D1 삭제 전에 원형으로 보존한다."""
    from core.wiki.citation_repair import CitationRepairResult

    content = PROJECT + "\n아직 원문을 확인하지 못한 추가 주장입니다.\n"
    compiler, store, guard = _compiler(tmp_path, content)
    monkeypatch.setattr(
        "core.wiki.citation_repair.repair_document_citations",
        AsyncMock(return_value=CitationRepairResult(content, 0, 1, "no_support")),
    )
    result = await compiler.compile_meeting(
        meeting_id="sample", meeting_date=date(2026, 11, 5), summary="", utterances=[]
    )
    guard.verify.assert_not_awaited()
    assert result.pages_pending == ["projects/sample.md"]
    assert (
        "아직 원문을 확인하지 못한 추가 주장입니다."
        in store.read_page(Path("pending/projects/sample.md")).content
    )
    assert not result.pages_created


@pytest.mark.asyncio
async def test_guard_전에_정규화해_pending의_필수메타데이터를_기록한다(tmp_path: Path) -> None:
    """모델 code fence 때문에 pending 후보의 상태·이유가 사라지던 실제 경로를 막는다."""
    compiler, store, guard = _compiler(tmp_path, f"```markdown\n{PROJECT}```\n")
    result = await compiler.compile_meeting(
        meeting_id="sample", meeting_date=date(2026, 11, 4), summary="", utterances=[]
    )
    assert guard.verify.await_args.kwargs["new_content"] == PROJECT
    assert result.pages_pending == ["projects/sample.md"]
    assert not result.pages_created and not result.pages_rejected
    page = store.read_page(Path("pending/projects/sample.md"))
    assert page.frontmatter["status"] == "pending"
    assert page.frontmatter["proposed_status"] == "in-progress"
    assert page.frontmatter["review_reason"] == "low_confidence"
    assert "<!-- confidence: 7 -->" in page.content


@pytest.mark.asyncio
async def test_불완전한_wrapper는_guard나_저장으로_넘기지_않는다(tmp_path: Path) -> None:
    """서식 오류가 grounding 실패처럼 저장되거나 자동 게시되지 않게 한다."""
    compiler, store, guard = _compiler(tmp_path, f"```markdown\n{PROJECT}")
    result = await compiler.compile_meeting(
        meeting_id="sample", meeting_date=date(2026, 11, 4), summary="", utterances=[]
    )
    guard.verify.assert_not_awaited()
    assert result.pages_rejected == [("projects/sample.md", "malformed_document")]
    assert not result.pages_pending and not result.pages_created
    assert not any(path.parts[0] in {"projects", "pending"} for path in store.all_pages())


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["markdown", "md", ""])
async def test_프로젝트_wrapper를_먼저_벗겨_강제상태와_날짜_기존원장을_보존한다(
    tmp_path: Path, language: str
) -> None:
    """모델의 감싼 응답이 기존 상태·생성일·담당자 보존을 건너뛰지 못하게 한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    path = Path("projects/sample.md")
    previous = PROJECT.replace("status: in-progress", "status: blocked").replace(
        "last_updated: 2026-11-04",
        "last_updated: 2026-11-03\nstarted: 2026-01-01\ntarget: 2026-12-31\ncreated_at: 2026-01-01T10:00:00",
    )
    store.write_page(path, previous)
    rendered = (
        PROJECT.replace("status: in-progress", "status: shipped")
        .replace("last_updated: 2026-11-04", "last_updated: 1900-01-01")
        .replace("owner: 담당자\n", "")
    )
    llm = MagicMock(generate=AsyncMock(return_value=f"```{language}\n{rendered}```\n"))
    pages = await ProjectExtractor(llm).render_or_update_pages(
        projects=[_project()],
        status_transitions={},
        meeting_id="sample",
        meeting_date=date(2026, 11, 4),
        existing_store=store,
        meeting_decisions=[],
        meeting_new_actions=[],
        existing_open_actions=[],
    )
    assert len(pages) == 1
    metadata, body = _parse_frontmatter(pages[0][1])
    assert metadata["type"] == "project"
    assert metadata["status"] == "blocked"
    assert metadata["last_updated"] == "2026-11-04"
    assert metadata["started"] == "2026-01-01"
    assert metadata["target"] == "2026-12-31"
    assert metadata["created_at"] == "2026-01-01T10:00:00"
    assert metadata["owner"] == "담당자"
    assert "검토 중 [meeting:sample@00:00:20]" in body
    assert store.read_raw_page(path) == previous


@pytest.mark.asyncio
async def test_불완전한_프로젝트_wrapper는_기존원장을_보존하고_후보를_생략한다(
    tmp_path: Path,
) -> None:
    """형식이 불명확한 모델 응답으로 기존 페이지를 바꾸지 않는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    path = Path("projects/sample.md")
    store.write_page(path, PROJECT)
    llm = MagicMock(generate=AsyncMock(return_value=f"```markdown\n{PROJECT}"))
    pages = await ProjectExtractor(llm).render_or_update_pages(
        projects=[_project()],
        status_transitions={},
        meeting_id="sample",
        meeting_date=date(2026, 11, 4),
        existing_store=store,
        meeting_decisions=[],
        meeting_new_actions=[],
        existing_open_actions=[],
    )
    assert pages == []
    assert store.read_raw_page(path) == PROJECT


@pytest.mark.asyncio
async def test_새로운_프로젝트원장은_과거회의의_fenced후보로_되돌리지_않는다(
    tmp_path: Path,
) -> None:
    """기존 최신 날짜 검사를 유지하고 필요 없는 모델 호출도 시작하지 않는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    path = Path("projects/sample.md")
    previous = PROJECT.replace("last_updated: 2026-11-04", "last_updated: 2026-11-05")
    store.write_page(path, previous)
    llm = MagicMock(generate=AsyncMock(return_value=f"```markdown\n{PROJECT}```\n"))
    pages = await ProjectExtractor(llm).render_or_update_pages(
        projects=[_project()],
        status_transitions={},
        meeting_id="sample",
        meeting_date=date(2026, 11, 4),
        existing_store=store,
        meeting_decisions=[],
        meeting_new_actions=[],
        existing_open_actions=[],
    )
    assert pages == []
    llm.generate.assert_not_awaited()
    assert store.read_raw_page(path) == previous
