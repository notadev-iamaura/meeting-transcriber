"""회의별 익명 화자 번호를 전역 인물로 누적하거나 연결하지 않는 회귀 테스트."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.wiki.editing import related_pages
from core.wiki.extractors.person import ExtractedPerson, PersonExtractor
from core.wiki.person_identity import is_named_person_identity
from core.wiki.store import WikiStore


def _utterance(speaker: str, text: str = "이번 회의 발언") -> SimpleNamespace:
    """모델과 실제 자료를 사용하지 않는 최소 발화를 만든다."""
    return SimpleNamespace(speaker=speaker, text=text, start=20.0, end=30.0)


def _person(name: str, *, slug: str | None = None, confidence: int = 9) -> ExtractedPerson:
    """렌더 직접 호출의 마지막 방어를 검증할 후보를 만든다."""
    return ExtractedPerson(
        name=name,
        name_normalized=slug if slug is not None else name,
        role=None,
        first_seen_meeting_id="sample",
        first_seen_date="2026-11-04",
        last_seen_meeting_id="sample",
        last_seen_date="2026-11-04",
        confidence=confidence,
    )


def _response(name: str) -> str:
    """구조상 정상인 모델 응답을 만든다."""
    return json.dumps(
        [
            {
                "name": name,
                "role": None,
                "topic_mentions": [],
                "citation_ts": "00:00:20",
                "confidence": 9,
            }
        ]
    )


@pytest.mark.parametrize(
    "label",
    [
        "SPEAKER_00",
        "speaker_12",
        "SPEAKER_XX",
        "Speaker A",
        "UNKNOWN",
        "unknown_speaker",
        "UNKNOWN_SPEAKER_01",
        "SPEAKER_UNKNOWN",
        "화자 1",
        "참석자_2",
        "익명",
        "NONE",
        "",
        None,
        0,
    ],
)
def test_예약된_화자라벨은_인물식별자가_아니다(label: object) -> None:
    """기술 식별자·누락값의 표기 차이를 실명으로 승격하지 않는다."""
    assert not is_named_person_identity(label)


@pytest.mark.parametrize("name", ["철수", "김영희", "John Smith", "박 PM"])
def test_명시된_이름은_기존의_정확일치_대상으로_유지한다(name: str) -> None:
    """이름 추정이나 퍼지 매칭을 추가하지 않는다."""
    assert is_named_person_identity(name)


@pytest.mark.asyncio
async def test_같은_익명번호가_다른회의에_등장해도_인물생성_모델호출을_하지_않는다() -> None:
    """Senko 번호 재사용을 전역 people/SPEAKER_00 원장으로 합치지 않는다."""
    llm = MagicMock(generate=AsyncMock(return_value=_response("SPEAKER_00")))
    extractor = PersonExtractor(llm)
    for meeting in ("first", "second"):
        people = await extractor.extract_speakers(
            meeting_id=meeting,
            meeting_date=date(2026, 11, 4),
            utterances=[_utterance("SPEAKER_00"), _utterance("UNKNOWN")],
        )
        assert people == []
        assert (
            await extractor.render_or_update_pages(
                persons=people,
                meeting_id=meeting,
                meeting_date=date(2026, 11, 4),
                existing_store=MagicMock(),
                meeting_decisions=[],
                meeting_new_actions=[],
                existing_open_actions=[],
            )
            == []
        )
    llm.generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("model_name", ["철수", "SPEAKER_00"])
async def test_현재회의의_명시매핑은_실명으로_정규화하고_익명발언을_섞지_않는다(
    model_name: str,
) -> None:
    """모델이 실명이나 raw key를 반환해도 허용된 현재 매핑으로만 이름을 정한다."""
    llm = MagicMock(generate=AsyncMock(return_value=_response(model_name)))
    people = await PersonExtractor(llm).extract_speakers(
        meeting_id="sample",
        meeting_date=date(2026, 11, 4),
        utterances=[
            _utterance("SPEAKER_00", "NAMED_TEXT"),
            _utterance("UNKNOWN", "UNIDENTIFIED_TEXT"),
        ],
        speaker_name_map={"SPEAKER_00": "철수", "ABSENT_SPEAKER": "다른회의이름"},
    )
    assert len(people) == 1 and people[0].name == people[0].name_normalized == "철수"
    prompt = llm.generate.await_args.kwargs["user_prompt"]
    assert "NAMED_TEXT" in prompt
    assert "UNIDENTIFIED_TEXT" not in prompt
    assert "ABSENT_SPEAKER" not in prompt and "다른회의이름" not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mapping",
    [
        {"SPEAKER_99": "철수"},
        {"SPEAKER_00": "UNKNOWN"},
        {"SPEAKER_00": "SPEAKER_01"},
        {"SPEAKER_00": ""},
    ],
)
async def test_현재없는_화자키나_익명으로_향하는_매핑은_허용하지_않는다(
    mapping: dict[str, str],
) -> None:
    """다른 회의 매핑의 이름을 현재 회의 인물로 허용하지 않는다."""
    llm = MagicMock(generate=AsyncMock(return_value=_response("철수")))
    assert (
        await PersonExtractor(llm).extract_speakers(
            meeting_id="sample",
            meeting_date=date(2026, 11, 4),
            utterances=[_utterance("SPEAKER_00")],
            speaker_name_map=mapping,
        )
        == []
    )
    llm.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_실명화자와_섞여도_다른회의_매핑값이나_모델추측은_허용하지_않는다() -> None:
    """허용 집합을 현재 raw 화자와 유효 매핑으로 제한한다."""
    llm = MagicMock(generate=AsyncMock(return_value=_response("다른회의이름")))
    assert (
        await PersonExtractor(llm).extract_speakers(
            meeting_id="sample",
            meeting_date=date(2026, 11, 4),
            utterances=[_utterance("철수"), _utterance("SPEAKER_00")],
            speaker_name_map={"SPEAKER_99": "다른회의이름"},
        )
        == []
    )


@pytest.mark.parametrize("name", ["SPEAKER_00", "UNKNOWN"])
def test_항목직접변환도_익명화자를_거부한다(name: str) -> None:
    """호환용 allowed_names=None 경로도 익명 전역 정체성의 우회가 되지 않는다."""
    extractor = PersonExtractor(MagicMock())
    assert (
        extractor._build_extracted_person(
            {"name": name, "confidence": 10},
            meeting_id="sample",
            meeting_date=date(2026, 11, 4),
            speaker_name_map=None,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,slug",
    [
        ("SPEAKER_00", "SPEAKER_00"),
        ("UNKNOWN", "UNKNOWN"),
        ("철수", "SPEAKER_00"),
        ("SPEAKER_00", "철수"),
        ("철수", "영희"),
    ],
)
async def test_렌더직접호출도_익명이나_불일치경로의_원장을_읽거나_갱신하지_않는다(
    name: str, slug: str
) -> None:
    """가짜 ExtractedPerson을 주입해도 기존 전역 원장에 병합하지 않는다."""
    llm = MagicMock(generate=AsyncMock())
    store = MagicMock()
    assert (
        await PersonExtractor(llm).render_or_update_pages(
            persons=[_person(name, slug=slug)],
            meeting_id="sample",
            meeting_date=date(2026, 11, 4),
            existing_store=store,
            meeting_decisions=[],
            meeting_new_actions=[],
            existing_open_actions=[],
        )
        == []
    )
    llm.generate.assert_not_awaited()
    store.read_raw_page.assert_not_called()
    store.read_page.assert_not_called()


@pytest.mark.asyncio
async def test_익명_후보가_상한을_차지해_실명_후보를_밀어내지_않는다(tmp_path: Path) -> None:
    """직접 전달된 높은 점수의 익명 후보를 상한 적용 전에 제거한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    llm = MagicMock(generate=AsyncMock(return_value='{"role_update":null,"new_topics":[]}'))
    people = [_person(f"SPEAKER_{number:02d}", confidence=10) for number in range(8)]
    people.append(_person("철수", confidence=9))
    pages = await PersonExtractor(llm).render_or_update_pages(
        persons=people,
        meeting_id="sample",
        meeting_date=date(2026, 11, 4),
        existing_store=store,
        meeting_decisions=[],
        meeting_new_actions=[],
        existing_open_actions=[],
    )
    assert [page[0] for page in pages] == ["people/철수.md"]
    llm.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_같은번호도_회의별_명시실명에_따라_별도원장으로_보존한다(tmp_path: Path) -> None:
    """실명 매핑은 유지하면서 번호만 같은 두 사람의 누적 합병을 막는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    for meeting, name in (("first", "철수"), ("second", "영희"), ("third", "철수")):
        llm = MagicMock(
            generate=AsyncMock(
                side_effect=[_response("SPEAKER_00"), '{"role_update":null,"new_topics":[]}']
            )
        )
        extractor = PersonExtractor(llm)
        people = await extractor.extract_speakers(
            meeting_id=meeting,
            meeting_date=date(2026, 11, 4),
            utterances=[_utterance("SPEAKER_00")],
            speaker_name_map={"SPEAKER_00": name},
        )
        pages = await extractor.render_or_update_pages(
            persons=people,
            meeting_id=meeting,
            meeting_date=date(2026, 11, 4),
            existing_store=store,
            meeting_decisions=[],
            meeting_new_actions=[],
            existing_open_actions=[],
        )
        assert len(pages) == 1
        for path, content, _ in pages:
            store.write_page(Path(path), content)
    assert store.read_page(Path("people/철수.md")).frontmatter["meetings_count"] == 2
    assert store.read_page(Path("people/영희.md")).frontmatter["meetings_count"] == 1
    assert not any(path.stem == "SPEAKER_00" for path in store.all_pages())


def test_관련문서는_익명번호로_다른회의_인물후보를_연결하지_않는다(tmp_path: Path) -> None:
    """역할 필드와 people 정체성 양쪽에서 익명 비교 키를 제거한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    source = Path("decisions/first.md")
    store.write_page(
        source, "---\ntype: decision\nparticipants: [SPEAKER_00, UNKNOWN]\n---\n# 첫 회의\n"
    )
    for path, name in (
        ("people/SPEAKER_00.md", "SPEAKER_00"),
        ("pending/people/UNKNOWN.md", "UNKNOWN"),
    ):
        store.write_page(Path(path), f"---\ntype: person\nname: {name}\n---\n# 인물 후보\n")
    assert related_pages(store, source) == []
    assert related_pages(store, Path("people/SPEAKER_00.md")) == []


def test_실명관계와_명시된_직접링크는_계속_탐색할_수_있다(tmp_path: Path) -> None:
    """자동 익명 추정 연결만 제한하고 실명과 명시 링크의 기존 동작은 보존한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    source = Path("decisions/first.md")
    store.write_page(
        source,
        "---\ntype: decision\nowner: 철수\n---\n# 회의\n[기존 후보](../people/SPEAKER_00.md)\n",
    )
    store.write_page(Path("people/철수.md"), "---\ntype: person\nname: 철수\n---\n# 철수\n")
    store.write_page(
        Path("people/SPEAKER_00.md"), "---\ntype: person\nname: SPEAKER_00\n---\n# 후보\n"
    )
    found = {item["path"]: item["relation"] for item in related_pages(store, source)}
    assert found == {"people/철수.md": "person", "people/SPEAKER_00.md": "link"}
