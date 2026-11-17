"""빈 결정 필드와 원문 화자 목록을 사실·동의로 확대하지 않는 회귀 검사."""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.wiki.citations import enforce_citations, is_factual_statement
from core.wiki.decision_record import DecisionRecord
from core.wiki.extractors.decision import DecisionExtractor
from core.wiki.store import WikiStore, WikiStoreError

MEETING_ID = "abc12345"
MARKER = f"[meeting:{MEETING_ID}@00:00:01]"


def _reply(participants: object, *, count: int = 1) -> str:
    """화자 목록을 믿을 수 없는 모델 응답 대역을 만든다."""
    return json.dumps(
        [
            {
                "title": f"가상 기준 검토 {i}",
                "slug": f"source-metadata-{i}",
                "decision_text": f"가상 기준을 검토하기로 했다. {MARKER}",
                "background": "",
                "follow_ups": [],
                "participants": participants,
                "projects": [],
                "confidence": 9,
            }
            for i in range(count)
        ],
        ensure_ascii=False,
    )


def _record(*, background: str = "", follow_ups: list[str] | None = None) -> DecisionRecord:
    """필드 부재와 명시된 주장을 구별할 테스트 기록을 만든다."""
    return DecisionRecord(
        id="decision-source-metadata",
        title="가상 기준 검토",
        status="decided",
        decision_date=date(2026, 1, 1),
        decision_text=f"가상 기준을 검토하기로 했다. {MARKER}",
        background=background,
        follow_ups=follow_ups or [],
        source_meetings=[MEETING_ID],
        citations=[MARKER],
        confidence=9,
    )


def test_empty_context_renders_unknown_without_claiming_absence() -> None:
    """미추출 배경·후속은 부재 주장 대신 미확인으로 남고 가짜 인용이 필요 없다."""
    record = _record()

    rendered = record.to_markdown()
    checked, rejected = enforce_citations(rendered, MEETING_ID)

    assert "## 배경\n미확인\n" in rendered
    assert "## 후속 액션\n- 미확인\n" in rendered
    assert checked == rendered and rejected == []
    assert record.background == "" and record.follow_ups == []
    assert record.citation_unresolved is False


def test_explicit_followup_and_cited_absence_are_preserved() -> None:
    """실제 적힌 배경·후속·인용은 빈 필드 표시로 바꾸지 않는다."""
    background = f"추가 배경은 없다고 말했다. {MARKER}"
    follow_up = f"검토 담당: 다음 검토에서 조건을 확인한다. {MARKER}"
    record = _record(background=background, follow_ups=[follow_up])

    rendered = record.to_markdown()

    assert f"## 배경\n{background}\n" in rendered
    assert f"## 후속 액션\n- {follow_up}\n" in rendered
    assert "미확인" not in rendered
    assert record.background == background and record.follow_ups == [follow_up]


@pytest.mark.parametrize("line", ["미확인", "- 미확인", "* 미확인", "미확인.", "_(미확인)_"])
def test_exact_unknown_placeholder_is_exempt(line: str) -> None:
    """placeholder만 있는 줄은 실제 내용을 주장하지 않으므로 인용을 요구하지 않는다."""
    assert is_factual_statement(line) is False
    assert enforce_citations(line, MEETING_ID) == (line, [])


@pytest.mark.parametrize(
    "line",
    [
        "후속 업무 없음",
        "- 후속업무 없음",
        "담당자 미확인",
        "- 담당자미확인",
        "미확인: 후속 업무",
        "미확인 상태로 진행한다.",
    ],
)
def test_unknown_or_absence_inside_a_claim_still_requires_citation(line: str) -> None:
    """부재·담당자 상태 등의 실제 문장은 단어가 같아도 인용 면제를 받지 않는다."""
    assert is_factual_statement(line) is True
    checked, rejected = enforce_citations(f"미확인\n{line}", MEETING_ID)
    assert checked == "미확인\n"
    assert rejected == [line]


@pytest.mark.asyncio
@pytest.mark.parametrize("llm_participants", [["가상 실명", "SPEAKER_01"], None, 42, "가상 실명"])
async def test_every_decision_uses_complete_source_roster_without_mutation(
    llm_participants: object,
) -> None:
    """모델의 누락·가짜 이름·잘못된 자료형과 무관하게 실제 입력 화자 전체를 쓴다."""
    utterances = [
        {"text": "가상 기준을 검토하자.", "speaker": "SPEAKER_01", "start": 1, "end": 2},
        SimpleNamespace(text="확인했습니다.", speaker="SPEAKER_08", start=3, end=4),
        {"text": "조건을 확인한다.", "speaker": "UNKNOWN", "start": 5, "end": 6},
        SimpleNamespace(text="검토합니다.", speaker="김가람", start=7, end=8),
        {"text": "다음 항목입니다.", "speaker": "SPEAKER_01", "start": 9, "end": 10},
    ]
    before = copy.deepcopy(utterances)
    llm = SimpleNamespace(generate=AsyncMock(return_value=_reply(llm_participants, count=2)))

    decisions = await DecisionExtractor(llm).extract(
        meeting_id=MEETING_ID, meeting_date=date(2026, 1, 1), summary="", utterances=utterances
    )

    expected = ["SPEAKER_01", "SPEAKER_08", "UNKNOWN", "김가람"]
    assert len(decisions) == 2
    assert [decision.participants for decision in decisions] == [expected, expected]
    assert decisions[0].participants is not decisions[1].participants
    assert utterances == before
    llm.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_empty_or_non_label_speakers_do_not_invent_identities() -> None:
    """빈 화자 필드를 프롬프트가 UNKNOWN으로 보이게 해도 실제 목록에 신원을 만들지 않는다."""
    utterances = [
        {"text": "검토하자.", "speaker": "민수", "start": 1, "end": 2},
        {"text": "조건입니다.", "speaker": "", "start": 3, "end": 4},
        SimpleNamespace(text="조건입니다.", speaker=None, start=5, end=6),
        {"text": "조건입니다.", "speaker": "  ", "start": 7, "end": 8},
        {"text": "조건입니다.", "start": 9, "end": 10},
        {"text": "조건입니다.", "speaker": 42, "start": 11, "end": 12},
    ]
    before = copy.deepcopy(utterances)
    llm = SimpleNamespace(generate=AsyncMock(return_value=_reply(["UNKNOWN", "새 이름"])))

    decisions = await DecisionExtractor(llm).extract(
        meeting_id=MEETING_ID, meeting_date=date(2026, 1, 1), summary="", utterances=utterances
    )

    assert decisions[0].participants == ["민수"]
    assert utterances == before


@pytest.mark.asyncio
async def test_roster_is_all_input_labels_not_only_cited_or_serialized_turns() -> None:
    """화자 목록은 특정 결정을 지지한 사람의 목록이 아니라 입력 원문의 라벨 집합이다."""
    utterances = [
        {"text": "검토하자.", "speaker": "SPEAKER_01", "start": 1, "end": 2},
        {"text": "", "speaker": "SPEAKER_08", "start": None, "end": None},
        SimpleNamespace(text="", speaker="UNKNOWN", start=3, end=4),
    ]
    before = copy.deepcopy(utterances)
    llm = SimpleNamespace(generate=AsyncMock(return_value=_reply(["SPEAKER_01"])))

    decisions = await DecisionExtractor(llm).extract(
        meeting_id=MEETING_ID, meeting_date=date(2026, 1, 1), summary="", utterances=utterances
    )

    assert decisions[0].participants == ["SPEAKER_01", "SPEAKER_08", "UNKNOWN"]
    assert utterances == before


@pytest.mark.asyncio
@pytest.mark.parametrize("protection", ["human_edited", "human_reviewed"])
async def test_new_roster_and_unknown_fields_do_not_overwrite_human_document(
    tmp_path: Path, protection: str
) -> None:
    """새 생성 후보를 만들더라도 기존 사람 편집·확인 문서의 본문과 metadata를 보존한다."""
    root = tmp_path / "wiki"
    store = WikiStore(root)
    store.init_repo()
    path = Path("decisions/2026-01-01-source-metadata-0.md")
    original = (
        f"---\ntype: decision\nid: human-record\n{protection}: true\n"
        "participants: [사람이 기록한 참석자]\ncreated_at: 2026-01-01T09:00:00\n---\n"
        "## 배경\n사용자가 기록한 배경\n## 후속 액션\n- 사용자가 기록한 작업\n"
    )
    store.write_page(path, original)
    before = (root / path).read_bytes()
    utterances = [{"text": "검토하자.", "speaker": "SPEAKER_08", "start": 1, "end": 2}]
    source_before = copy.deepcopy(utterances)
    llm = SimpleNamespace(generate=AsyncMock(return_value=_reply(["가상 실명"])))
    extractor = DecisionExtractor(llm)
    decisions = await extractor.extract(
        meeting_id=MEETING_ID, meeting_date=date(2026, 1, 1), summary="", utterances=utterances
    )

    pages = await extractor.render_pages(
        decisions=decisions,
        meeting_id=MEETING_ID,
        meeting_date=date(2026, 1, 1),
        existing_store=store,
    )

    assert len(pages) == 1
    assert "participants: [SPEAKER_08]" in pages[0][1]
    assert "## 배경\n미확인\n" in pages[0][1]
    assert "## 후속 액션\n- 미확인\n" in pages[0][1]
    with pytest.raises(WikiStoreError, match="human_protected"):
        store.write_page(Path(pages[0][0]), pages[0][1])
    assert (root / path).read_bytes() == before
    assert utterances == source_before
