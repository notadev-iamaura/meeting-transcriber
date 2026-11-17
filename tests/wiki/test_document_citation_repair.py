"""누락 인용 복구가 주장과 기존 출처를 덮어쓰지 않는지 검사한다."""

from unittest.mock import AsyncMock

import pytest

from config import WikiGroundingConfig
from core.wiki.citation_repair import repair_document_citations, source_lines

MARKER = "[meeting:meeting_A@00:00:12]"
LINES = [f"{MARKER} SPEAKER_01: 조건이 확인되면 결과를 공유하겠습니다."]
DOCUMENT = "---\ntype: project\ncitation_unresolved: true\n---\n# 프로젝트\n조건이 확인되면 결과를 공유하기로 했다.\n"


@pytest.mark.asyncio
async def test_only_appends_existing_marker_and_preserves_condition() -> None:
    llm = AsyncMock()
    llm.generate.return_value = '[{"id":0,"citations":["' + MARKER + '"]}]'
    result = await repair_document_citations(DOCUMENT, LINES, llm, WikiGroundingConfig())
    assert result.repaired == 1 and result.unresolved == 0
    assert result.content == DOCUMENT.replace(
        "citation_unresolved: true", "citation_unresolved: false"
    ).replace("공유하기로 했다.\n", f"공유하기로 했다. {MARKER}\n")
    assert llm.generate.await_count == 1


@pytest.mark.parametrize(
    "response",
    [
        '[{"id":0,"citations":["[meeting:unknown@00:00:00]"]}]',
        '[{"id":0,"citations":[]},{"id":0,"citations":[]}]',
        '[{"id":false,"citations":[]}]',
        '[{"id":0,"id":1,"citations":[]}]',
        '[{"id":0,"citations":[],"rewritten":"확정"}]',
    ],
)
@pytest.mark.asyncio
async def test_invalid_response_preserves_every_original_byte(response: str) -> None:
    llm = AsyncMock()
    llm.generate.return_value = response
    result = await repair_document_citations(DOCUMENT, LINES, llm, WikiGroundingConfig())
    assert result.content == DOCUMENT and result.repaired == 0


@pytest.mark.asyncio
async def test_existing_citations_never_borrow_another_field_or_call_model() -> None:
    content = DOCUMENT.replace("공유하기로 했다.\n", f"공유하기로 했다. {MARKER}\n")
    llm = AsyncMock()
    result = await repair_document_citations(content, LINES, llm, WikiGroundingConfig())
    assert result.content == content
    llm.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_repair_keeps_unresolved_flag_and_second_claim_unchanged() -> None:
    content = DOCUMENT + "기한은 별도로 확정한다.\n"
    llm = AsyncMock()
    llm.generate.return_value = '[{"id":0,"citations":["' + MARKER + '"]}]'
    result = await repair_document_citations(content, LINES, llm, WikiGroundingConfig())
    assert result.unresolved == 1 and "citation_unresolved: true" in result.content
    assert result.content.endswith("기한은 별도로 확정한다.\n")


@pytest.mark.asyncio
async def test_budget_and_empty_evidence_do_not_call_model() -> None:
    llm = AsyncMock()
    for evidence in ([], LINES):
        result = await repair_document_citations(
            DOCUMENT + "추가 주장이다.\n", evidence, llm, WikiGroundingConfig(max_claims=1)
        )
        assert result.repaired == 0
    llm.generate.assert_not_awaited()


def test_source_markers_use_actual_start_and_do_not_fabricate_zero() -> None:
    lines = source_lines(
        "meeting_A",
        [
            {"start": 12.9, "speaker": "SPEAKER_01", "text": "공유하겠습니다."},
            {"start": True, "text": "안 됨"},
            {"start": -1, "text": "안 됨"},
        ],
    )
    assert lines == [f"{MARKER} SPEAKER_01: 공유하겠습니다."]


@pytest.mark.asyncio
async def test_non_marker_evidence_cannot_resolve_claim_and_trailing_spaces_survive() -> None:
    llm = AsyncMock()
    result = await repair_document_citations(
        DOCUMENT, ["not-a-marker SPEAKER_01: 원문"], llm, WikiGroundingConfig()
    )
    assert result.content == DOCUMENT and result.repaired == 0
    llm.generate.assert_not_awaited()
    content = DOCUMENT.replace("공유하기로 했다.\n", "공유하기로 했다.  \r\n")
    llm.generate.return_value = '[{"id":0,"citations":["' + MARKER + '"]}]'
    result = await repair_document_citations(content, LINES, llm, WikiGroundingConfig())
    assert f"공유하기로 했다.   {MARKER}\r\n" in result.content


@pytest.mark.asyncio
async def test_disabled_direct_function_and_unknown_source_never_call_model() -> None:
    assert source_lines("meeting_A", [object(), None, 3]) == []
    llm = AsyncMock()
    result = await repair_document_citations(
        DOCUMENT, LINES, llm, WikiGroundingConfig(enabled=False)
    )
    assert result.content == DOCUMENT and result.unresolved == 1 and result.reason == "disabled"
    llm.generate.assert_not_awaited()
