"""액션 저장 왕복의 명시적 identity와 과거 완료 이력의 보수적 재처리를 검증한다."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest

from core.wiki.compiler import WikiCompilerV2
from core.wiki.digest import parse_open_actions
from core.wiki.extractors.action_item import (
    ActionItemExtractor,
    ClosedActionItem,
    NewActionItem,
    OpenActionItem,
    parse_action_identity_comments,
)
from core.wiki.llm_client import MockWikiLLMClient
from core.wiki.models import Citation
from core.wiki.store import WikiStore


def _original() -> OpenActionItem:
    """완료되기 전의 명시적 담당·기한·프로젝트를 만든다."""
    return OpenActionItem(
        "original",
        "SPEAKER_01",
        "가상 검토 자료 전달",
        "origin",
        "2026-01-01",
        Citation("origin", "00:00:10", 10),
        "review",
        "2026-01-03",
    )


def _closed(item: OpenActionItem) -> ClosedActionItem:
    """종료 화자와 근거는 원래 업무의 식별정보와 별개로 보존한다."""
    return ClosedActionItem(
        item, "SPEAKER_02", "closed", Citation("closed", "00:00:20", 20), "cancelled"
    )


async def _parse(
    tmp_path: Path, content: str
) -> tuple[list[OpenActionItem], list[ClosedActionItem]]:
    """운영 자료 없이 실제 디스크 파서를 통과시킨다."""
    store = WikiStore(tmp_path.resolve() / "wiki")
    store.write_page(Path("action_items.md"), content)
    compiler = object.__new__(WikiCompilerV2)
    compiler._store = store
    return await compiler._parse_existing_action_items()


async def _render(
    opened: list[OpenActionItem],
    closed: list[ClosedActionItem],
    new: list[NewActionItem] | None = None,
) -> str:
    """모델 호출이 없는 실제 렌더러를 사용한다."""
    llm = MockWikiLLMClient()
    result = await ActionItemExtractor(llm).render_unified_page(
        existing_open=opened,
        existing_closed=closed,
        new_open=new or [],
        newly_closed=[],
        last_compiled_at="2026-01-04",
    )
    assert not llm.calls
    return result


async def test_closed_identity_survives_disk_and_prevents_reopening(tmp_path: Path) -> None:
    """종료 후 원래 회의를 다시 처리해도 같은 액션을 새로 열지 않는다."""
    item = _original()
    closed = _closed(item)
    page = await _render([], [closed])
    opened, saved = await _parse(tmp_path, page)
    assert opened == [] and saved == [closed]
    replay = NewActionItem(
        item.owner, item.description, item.citation, item.due_date, item.project_slug, 9
    )
    again = await _render(opened, saved, [replay])
    assert "## Open (0)" in again and "## Closed (1)" in again
    assert await _parse(tmp_path, again) == ([], [closed])


async def test_open_identity_and_digest_preserve_fields_without_comment_leak(
    tmp_path: Path,
) -> None:
    """Open의 프로젝트도 저장하고 현황 설명에 구현 주석을 노출하지 않는다."""
    original = _original()
    page = await _render([original], [])
    opened, closed = await _parse(tmp_path, page)
    assert opened == [original] and closed == []
    actions = parse_open_actions(page)
    assert actions[0].description == original.description
    assert "action-identity" not in actions[0].raw_line
    clean, identities = parse_action_identity_comments(page)
    assert "action-identity" not in clean and len(identities) == 1


@pytest.mark.parametrize(
    "field,value", [("owner", "SPEAKER_03"), ("due_date", "2026-01-05"), ("project_slug", "other")]
)
async def test_explicit_distinct_identity_is_not_suppressed(
    tmp_path: Path, field: str, value: str
) -> None:
    """기한·프로젝트·담당이 명시적으로 다른 업무는 완료 이력과 합치지 않는다."""
    item = _original()
    _, saved = await _parse(tmp_path, await _render([], [_closed(item)]))
    new = NewActionItem(
        item.owner, item.description, item.citation, item.due_date, item.project_slug, 9
    )
    changed = (
        replace(new, owner=value)
        if field == "owner"
        else replace(new, due_date=value)
        if field == "due_date"
        else replace(new, project_slug=value)
    )
    result = await _render([], saved, [changed])
    assert "## Open (1)" in result and "## Closed (1)" in result


async def test_legacy_closed_only_holds_new_candidate_without_deleting_open(
    tmp_path: Path,
) -> None:
    """과거 완료의 누락 identity를 추측하지 않고 기존 Open은 유지한다."""
    item = _original()
    legacy, _ = parse_action_identity_comments(await _render([], [_closed(item)]))
    _, saved = await _parse(tmp_path, legacy)
    assert saved[0].original.owner == "미지정" and not saved[0].original.identity_known
    replay = NewActionItem(
        item.owner, item.description, item.citation, item.due_date, item.project_slug, 9
    )
    result = await _render([item], saved, [replay])
    opened, parsed_closed = await _parse(tmp_path, result)
    assert opened == [item] and len(parsed_closed) == 1
    assert "## Open (0)" in await _render([], saved, [replay])


@pytest.mark.parametrize("tamper", ["checksum", "visible", "duplicate", "orphan", "injection"])
async def test_invalid_identity_preserves_original_page(tmp_path: Path, tamper: str) -> None:
    """훼손·중복·작업 불일치 주석을 과거 무정보 형식으로 조용히 처리하지 않는다."""
    page = await _render([_original()], [])
    lines = page.splitlines(keepends=True)
    index = next(i for i, line in enumerate(lines) if line.startswith("<!-- action-identity"))
    if tamper == "checksum":
        payload = json.loads(base64.b64decode(lines[index].split(": ", 1)[1].split(" -->")[0]))
        payload["owner"] = "SPEAKER_03"
        lines[index] = (
            "<!-- action-identity-v1: "
            + base64.b64encode(json.dumps(payload).encode()).decode()
            + " -->\n"
        )
    elif tamper == "visible":
        lines[index + 1] = lines[index + 1].replace("SPEAKER_01", "SPEAKER_03")
    elif tamper == "duplicate":
        lines.insert(index, lines[index])
    elif tamper == "orphan":
        lines.insert(index + 1, "\n")
    else:
        lines[index] = lines[index].replace(" -->", " -->injected-->")
    content = "".join(lines)
    with pytest.raises(ValueError, match="action_identity"):
        await _parse(tmp_path, content)
    assert (tmp_path.resolve() / "wiki/action_items.md").read_text() == content


@pytest.mark.parametrize(
    "kind", ["known_open", "new_open", "known_closed", "legacy_open", "legacy_closed"]
)
async def test_supporting_citations_survive_open_and_closed_roundtrip(
    tmp_path: Path, kind: str
) -> None:
    """주 인용의 identity를 유지하면서 질문·답변 보강 근거를 모두 보존한다."""
    extra = (Citation("request", "00:00:09", 9), Citation("answer", "00:00:12", 12))
    item = replace(_original(), supporting_citations=extra)
    if kind == "new_open":
        new = NewActionItem(
            item.owner, item.description, item.citation, item.due_date, item.project_slug, 9, extra
        )
        page = await _render([], [], [new])
    elif kind.endswith("closed"):
        page = await _render([], [_closed(item)])
    else:
        page = await _render([item], [])
    if kind.startswith("legacy"):
        page, _ = parse_action_identity_comments(page)
    opened, closed = await _parse(tmp_path, page)
    saved = closed[0].original if closed else opened[0]
    assert saved.citation == item.citation and saved.supporting_citations == extra
    again = await _render(opened, closed)
    reopened, reclosed = await _parse(tmp_path, again)
    assert reopened == opened and reclosed == closed


async def test_supporting_evidence_does_not_change_replay_identity(tmp_path: Path) -> None:
    """보강 인용을 추가한 재추출도 같은 완료 업무를 다시 열지 않는다."""
    item = _original()
    _, closed = await _parse(tmp_path, await _render([], [_closed(item)]))
    new = NewActionItem(
        item.owner,
        item.description,
        item.citation,
        item.due_date,
        item.project_slug,
        9,
        (Citation("request", "00:00:09", 9),),
    )
    result = await _render([], closed, [new])
    assert "## Open (0)" in result and "## Closed (1)" in result
