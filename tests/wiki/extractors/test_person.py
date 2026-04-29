"""PersonExtractor TDD Red 단계 테스트 모듈

목적: core/wiki/extractors/person.py 가 아직 존재하지 않으므로
  ImportError 로 모든 테스트가 Red 상태가 된다.
  구현체가 생기면 여기 정의된 계약을 통과해야 Green 이 된다.

커버리지:
  - PersonExtractor.extract_speakers() — LLM mock 기반 (4건)
  - PersonExtractor.render_or_update_pages() — 누적성 핵심 (5건)
  - _normalize_person_slug() — slug 정규화 (1건)
  - 인용 마커 보존 — CITATION_PATTERN (1건)
  총 11건 (임무 요구사항 초과)

의존성:
  - pytest (asyncio_mode=auto, pyproject.toml 에 설정됨)
  - core.wiki.models.Citation (Phase 1, 이미 구현 완료)
  - core.wiki.extractors.decision.ExtractedDecision (Phase 2, 이미 구현)
  - core.wiki.extractors.action_item.NewActionItem, OpenActionItem (Phase 2, 이미 구현)
  - core.wiki.extractors.person (Phase 3, 아직 미구현 → ImportError)

작성자: TDD Red Author (Phase 3)
날짜: 2026-04-29
"""

from __future__ import annotations

import re
from dataclasses import fields
from datetime import date
from pathlib import Path
from typing import Any

import pytest

# ──────────────────────────────────────────────────────────────────────────────
# Phase 2 모듈 — 이미 구현 완료, import 가능
# ──────────────────────────────────────────────────────────────────────────────
from core.wiki.extractors.decision import ExtractedDecision
from core.wiki.extractors.action_item import NewActionItem, OpenActionItem
from core.wiki.models import Citation  # Phase 1 — 이미 존재

# ──────────────────────────────────────────────────────────────────────────────
# [TDD Red] core/wiki/extractors/person.py 가 없으므로
# 이 import 블록이 ImportError 를 일으켜 모든 테스트가 Red 상태가 된다.
# ──────────────────────────────────────────────────────────────────────────────
from core.wiki.extractors.person import (  # noqa: E402
    ExtractedPerson,
    PersonExtractor,
    TopicMention,
    ExistingPersonState,
)

# PRD §4.2 인용 패턴 (citations.py 의 CITATION_PATTERN 과 동일)
CITATION_PATTERN = re.compile(r"\[meeting:([a-f0-9]{8})@(\d{2}):(\d{2}):(\d{2})\]")

# 한국어 고유명사 뒤에 외국어 병기가 붙는 패턴 (예: "철수(Chulsoo)")
_FOREIGN_GLOSS_PATTERN = re.compile(r"([\uAC00-\uD7A3]+)\([A-Za-z\u4E00-\u9FFF\u3041-\u30FF]+\)")


# ══════════════════════════════════════════════════════════════════════════════
# MockPersonLLM — Phase 2 의 MockDecisionLLM 패턴 동일
# ══════════════════════════════════════════════════════════════════════════════


class MockPersonLLM:
    """테스트용 WikiLLMClient. Phase 2 의 MockDecisionLLM 패턴 동일."""

    def __init__(self, responses: list[str]) -> None:
        """응답 시퀀스를 FIFO 큐로 저장한다.

        Args:
            responses: 호출 순서대로 반환할 응답 문자열 목록.
        """
        self._responses: list[str] = list(responses)
        self.call_count: int = 0
        self.last_prompt: str | None = None

    async def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        """다음 응답을 pop 해 반환한다. 응답이 비면 기본값 "[]" 반환.

        Args:
            system_prompt: 위키 스키마 + 역할 지시.
            user_prompt: 회의 컨텍스트 발화 목록.
            max_tokens: 응답 상한.
            temperature: 결정성 설정값.

        Returns:
            미리 설정된 응답 문자열.
        """
        self.last_prompt = user_prompt
        self.call_count += 1
        if not self._responses:
            return "[]"
        return self._responses.pop(0)

    @property
    def model_name(self) -> str:
        """테스트용 모델 식별자."""
        return "mock-gemma"


# ══════════════════════════════════════════════════════════════════════════════
# 테스트 픽스처 헬퍼 함수
# ══════════════════════════════════════════════════════════════════════════════


class _FakeUtterance:
    """Utterance Protocol 을 만족하는 최소 테스트 픽스처.

    corrector 모듈에 의존하지 않고 Protocol duck-typing 으로 사용한다.
    """

    def __init__(self, text: str, speaker: str, start: float, end: float) -> None:
        """발화 데이터를 저장한다."""
        self.text = text
        self.speaker = speaker
        self.start = start
        self.end = end


def mock_utterances(speakers: list[str]) -> list[_FakeUtterance]:
    """화자 목록으로 최소 발화 목록을 생성하는 헬퍼 픽스처.

    각 화자마다 기본 발화 1건씩 생성한다.

    Args:
        speakers: 화자 식별자 목록 (예: ["철수", "SPEAKER_00"]).

    Returns:
        _FakeUtterance 인스턴스 목록.
    """
    utterances = []
    for i, speaker in enumerate(speakers):
        utterances.append(
            _FakeUtterance(
                text=f"{speaker}의 발화 내용 {i+1}",
                speaker=speaker,
                start=float(i * 30),
                end=float(i * 30 + 20),
            )
        )
    return utterances


def _make_citation(
    meeting_id: str = "abc12345",
    ts_str: str = "00:10:00",
    ts_seconds: int = 600,
) -> Citation:
    """테스트용 Citation 인스턴스를 생성한다."""
    return Citation(
        meeting_id=meeting_id,
        timestamp_str=ts_str,
        timestamp_seconds=ts_seconds,
    )


def _make_extracted_decision(
    title: str = "출시일 확정",
    slug: str = "launch-date",
    participants: list[str] | None = None,
    meeting_id: str = "abc12345",
) -> ExtractedDecision:
    """테스트용 ExtractedDecision 인스턴스를 생성한다.

    Args:
        title: 결정 제목.
        slug: filename-safe 식별자.
        participants: 참여 화자 목록. None 이면 ["철수"] 기본값.
        meeting_id: 회의 ID.

    Returns:
        ExtractedDecision 인스턴스.
    """
    if participants is None:
        participants = ["철수"]
    return ExtractedDecision(
        title=title,
        slug=slug,
        decision_text=f"결정 내용 [meeting:{meeting_id}@00:10:00].",
        background=f"배경 설명 [meeting:{meeting_id}@00:08:00].",
        participants=participants,
        projects=["new-onboarding"],
        confidence=8,
    )


def _make_new_action_item(
    owner: str = "철수",
    description: str = "캘린더 갱신",
    meeting_id: str = "abc12345",
) -> NewActionItem:
    """테스트용 NewActionItem 인스턴스를 생성한다."""
    return NewActionItem(
        owner=owner,
        description=description,
        due_date="2026-05-01",
        citation=_make_citation(meeting_id=meeting_id),
        confidence=8,
    )


def _make_open_action_item(
    owner: str = "철수",
    description: str = "이전 회의 캘린더 갱신",
    from_meeting_id: str = "prev1234",
) -> OpenActionItem:
    """테스트용 OpenActionItem 인스턴스를 생성한다."""
    return OpenActionItem(
        item_id="item001",
        owner=owner,
        description=description,
        from_meeting_id=from_meeting_id,
        from_date="2026-04-15",
        citation=_make_citation(meeting_id=from_meeting_id, ts_str="01:00:00", ts_seconds=3600),
    )


class MockWikiStore:
    """테스트용 WikiStore 최소 구현체. 미리 설정된 페이지를 반환한다."""

    def __init__(self, existing_pages: dict[str, str] | None = None) -> None:
        """기존 페이지 맵을 저장한다.

        Args:
            existing_pages: {rel_path: content} 형태. None 이면 빈 딕셔너리.
        """
        self._pages: dict[str, str] = existing_pages or {}

    def read_page(self, rel_path: str) -> str:
        """페이지를 반환하거나 page_not_found 예외를 발생시킨다.

        Args:
            rel_path: wiki 루트 기준 상대 경로.

        Returns:
            저장된 페이지 본문.

        Raises:
            KeyError: 페이지가 없을 때 (page_not_found 시뮬레이션).
        """
        if rel_path not in self._pages:
            raise KeyError("page_not_found")
        return self._pages[rel_path]


def _build_single_person_json(
    name: str = "철수",
    role: str = "PM",
    meeting_id: str = "abc12345",
    ts: str = "00:10:00",
    confidence: int = 9,
) -> str:
    """LLM 이 인물 1건을 반환할 때의 JSON 문자열을 생성한다.

    Args:
        name: 인물 이름.
        role: 역할 (PM, Eng Lead 등).
        meeting_id: 8자리 hex 회의 ID.
        ts: HH:MM:SS 형식 타임스탬프.
        confidence: 신뢰도 0~10.

    Returns:
        JSON 배열 문자열 (인물 1건).
    """
    return (
        f'[{{"name": "{name}", "role": "{role}",'
        f'"topic_mentions": [{{"topic": "pricing-strategy", "citation_ts": "{ts}"}}],'
        f'"citation_ts": "{ts}",'
        f'"confidence": {confidence}}}]'
    )


def _build_existing_person_page(
    name: str = "철수",
    role: str = "PM",
    first_seen: str = "2026-04-01",
    last_seen: str = "2026-04-22",
    meetings_count: int = 3,
    seen_meeting_id: str = "prev1234",
) -> str:
    """기존 people/{name}.md 페이지 본문을 생성한다.

    PRD §4.2 people 템플릿을 준수하는 마크다운 문자열을 반환한다.

    Args:
        name: 인물 이름.
        role: frontmatter 역할.
        first_seen: 첫 등장 날짜.
        last_seen: 마지막 등장 날짜.
        meetings_count: 회의 참석 횟수.
        seen_meeting_id: 본문에 등장하는 기존 회의 ID.

    Returns:
        frontmatter + 4섹션이 포함된 마크다운 문자열.
    """
    return (
        "---\n"
        "type: person\n"
        f"name: {name}\n"
        f"role: {role}\n"
        f"first_seen: {first_seen}\n"
        f"last_seen: {last_seen}\n"
        f"meetings_count: {meetings_count}\n"
        "---\n\n"
        f"# {name} ({role})\n\n"
        "## 최근 결정 (latest 5)\n"
        f"- 2026-04-22: API v2 마이그레이션 보류 [meeting:{seen_meeting_id}@00:10:00]\n\n"
        "## 담당 프로젝트\n"
        "- [new-onboarding](../projects/new-onboarding.md)\n\n"
        "## 자주 언급하는 주제\n"
        f"- pricing-strategy [meeting:{seen_meeting_id}@00:30:11]\n\n"
        "## 미해결 액션아이템\n"
        f"- [ ] 캘린더 갱신 (from 2026-04-15) [meeting:{seen_meeting_id}@01:00:00]\n"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. PersonExtractor.extract_speakers() — LLM mock 기반 (4건)
# ══════════════════════════════════════════════════════════════════════════════


async def test_extract_speakers_단일_화자_1건_반환():
    """단일 화자 발화에서 ExtractedPerson 1건을 추출한다.

    Arrange: 화자 1명("철수")의 발화 + LLM 이 ExtractedPerson 1건 JSON 반환.
    Act: extract_speakers() 호출.
    Assert: ExtractedPerson 1건이 반환되고 name=="철수".
    """
    # Arrange
    mock_llm = MockPersonLLM(responses=[_build_single_person_json(name="철수")])
    extractor = PersonExtractor(llm=mock_llm)
    utterances = mock_utterances(speakers=["철수"])

    # Act
    results = await extractor.extract_speakers(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 29),
        utterances=utterances,
        speaker_name_map=None,
    )

    # Assert
    assert len(results) == 1
    assert isinstance(results[0], ExtractedPerson)
    assert results[0].name == "철수"


async def test_extract_speakers_여러_화자_3건_반환():
    """발화에 3명의 화자가 있으면 ExtractedPerson 3건을 반환한다.

    Arrange: 화자 3명("철수", "영희", "민준") 발화 + LLM 이 3건 JSON 반환.
    Act: extract_speakers() 호출.
    Assert: ExtractedPerson 3건이 반환된다.
    """
    # Arrange
    three_persons_json = (
        '[{"name": "철수", "role": "PM", "topic_mentions": [], "citation_ts": "00:05:00", "confidence": 9},'
        ' {"name": "영희", "role": "Eng Lead", "topic_mentions": [], "citation_ts": "00:10:00", "confidence": 8},'
        ' {"name": "민준", "role": null, "topic_mentions": [], "citation_ts": "00:15:00", "confidence": 7}]'
    )
    mock_llm = MockPersonLLM(responses=[three_persons_json])
    extractor = PersonExtractor(llm=mock_llm)
    utterances = mock_utterances(speakers=["철수", "영희", "민준"])

    # Act
    results = await extractor.extract_speakers(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 29),
        utterances=utterances,
        speaker_name_map=None,
    )

    # Assert
    assert len(results) == 3
    assert all(isinstance(p, ExtractedPerson) for p in results)
    names = {p.name for p in results}
    assert names == {"철수", "영희", "민준"}


async def test_extract_speakers_speaker_name_map_적용_정규화():
    """speaker_name_map 으로 SPEAKER_00 → "철수" 로 이름이 정규화된다.

    Arrange: utterances 의 speaker="SPEAKER_00" + map={"SPEAKER_00": "철수"}.
             LLM 은 name="철수" 로 정규화된 JSON 반환.
    Act: extract_speakers() 호출.
    Assert: 반환된 ExtractedPerson 의 name=="철수".
    """
    # Arrange
    mock_llm = MockPersonLLM(
        responses=[_build_single_person_json(name="철수", ts="00:05:00")]
    )
    extractor = PersonExtractor(llm=mock_llm)
    utterances = mock_utterances(speakers=["SPEAKER_00"])

    # Act
    results = await extractor.extract_speakers(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 29),
        utterances=utterances,
        speaker_name_map={"SPEAKER_00": "철수"},
    )

    # Assert
    assert len(results) == 1
    assert results[0].name == "철수"
    # name_normalized 은 한글 그대로 보존되어야 한다
    assert results[0].name_normalized == "철수"


async def test_extract_speakers_빈_utterances_llm_호출_없이_빈_리스트():
    """utterances 가 빈 리스트이면 LLM 호출 없이 즉시 빈 리스트를 반환한다.

    Arrange: utterances=[] + 응답이 있는 mock LLM.
    Act: extract_speakers() 호출.
    Assert: LLM 호출 0회 + 빈 리스트 반환.
    """
    # Arrange
    mock_llm = MockPersonLLM(responses=["should-not-be-called"])
    extractor = PersonExtractor(llm=mock_llm)

    # Act
    results = await extractor.extract_speakers(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 29),
        utterances=[],
        speaker_name_map=None,
    )

    # Assert
    assert results == []
    assert mock_llm.call_count == 0, (
        f"utterances=[] 일 때 LLM 이 호출되었습니다 ({mock_llm.call_count}회). "
        "빈 utterances 는 LLM 호출 없이 즉시 빈 리스트를 반환해야 합니다."
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2. PersonExtractor.render_or_update_pages() — 누적성 핵심 (5건)
# ══════════════════════════════════════════════════════════════════════════════


async def test_render_or_update_pages_신규_인물_페이지_생성():
    """existing_store 에 페이지 없으면 신규 페이지를 생성한다.

    Arrange: existing_store 에 people/철수.md 없음.
             LLM 은 role/topic 보강 응답 반환.
    Act: render_or_update_pages() 호출.
    Assert:
      - 반환 튜플의 rel_path 가 "people/철수.md" 이어야 한다.
      - frontmatter 에 meetings_count=1 이 있어야 한다.
      - frontmatter 에 first_seen == last_seen == meeting_id 날짜이어야 한다.
    """
    # Arrange
    meeting_id = "abc12345"
    meeting_date = date(2026, 4, 29)

    # LLM 은 role_update + new_topics JSON 반환 (render 시 role/topic 보강용)
    role_topic_json = '{"role_update": "PM", "new_topics": [{"topic": "pricing-strategy", "citation_ts": "00:10:00"}]}'
    mock_llm = MockPersonLLM(responses=[role_topic_json])
    extractor = PersonExtractor(llm=mock_llm)

    person = ExtractedPerson(
        name="철수",
        name_normalized="철수",
        role="PM",
        first_seen_meeting_id=meeting_id,
        first_seen_date=str(meeting_date),
        last_seen_meeting_id=meeting_id,
        last_seen_date=str(meeting_date),
        topic_mentions=[],
        citations=[_make_citation(meeting_id=meeting_id)],
        confidence=9,
    )

    store = MockWikiStore()  # 빈 store — 기존 페이지 없음

    # Act
    pages = await extractor.render_or_update_pages(
        persons=[person],
        meeting_id=meeting_id,
        meeting_date=meeting_date,
        existing_store=store,
        meeting_decisions=[],
        meeting_new_actions=[],
        existing_open_actions=[],
    )

    # Assert
    assert len(pages) >= 1
    rel_path, content, confidence = pages[0]
    assert "people/철수.md" == rel_path or rel_path.endswith("철수.md"), (
        f"신규 인물의 rel_path 가 'people/철수.md' 여야 합니다. 실제: {rel_path}"
    )
    assert "meetings_count: 1" in content, (
        f"신규 인물의 meetings_count 는 1 이어야 합니다.\n내용:\n{content}"
    )
    # first_seen / last_seen 이 meeting_date 를 포함해야 한다
    assert str(meeting_date) in content, (
        f"신규 인물의 first_seen/last_seen 에 {meeting_date} 이 없습니다."
    )


async def test_render_or_update_pages_기존_인물_meetings_count_증가():
    """existing_store 에 people/철수.md 존재 시 meetings_count 를 1 증가시킨다.

    Arrange: existing_store 에 meetings_count=3 인 people/철수.md 존재.
             LLM 은 role/topic 보강 응답 반환.
    Act: render_or_update_pages() 호출.
    Assert:
      - meetings_count 가 4 로 증가해야 한다.
      - last_seen 이 현재 meeting_date 로 갱신되어야 한다.
      - first_seen 은 기존 값(2026-04-01)이 보존되어야 한다.
    """
    # Arrange
    meeting_id = "abc12345"
    meeting_date = date(2026, 4, 29)
    original_first_seen = "2026-04-01"

    role_topic_json = '{"role_update": null, "new_topics": []}'
    mock_llm = MockPersonLLM(responses=[role_topic_json])
    extractor = PersonExtractor(llm=mock_llm)

    person = ExtractedPerson(
        name="철수",
        name_normalized="철수",
        role="PM",
        first_seen_meeting_id="prev1234",
        first_seen_date=original_first_seen,
        last_seen_meeting_id=meeting_id,
        last_seen_date=str(meeting_date),
        topic_mentions=[],
        citations=[_make_citation(meeting_id=meeting_id)],
        confidence=8,
    )

    # 기존 페이지: meetings_count=3, first_seen=2026-04-01
    existing_content = _build_existing_person_page(
        name="철수",
        role="PM",
        first_seen=original_first_seen,
        last_seen="2026-04-22",
        meetings_count=3,
        seen_meeting_id="prev1234",
    )
    store = MockWikiStore(existing_pages={"people/철수.md": existing_content})

    # Act
    pages = await extractor.render_or_update_pages(
        persons=[person],
        meeting_id=meeting_id,
        meeting_date=meeting_date,
        existing_store=store,
        meeting_decisions=[],
        meeting_new_actions=[],
        existing_open_actions=[],
    )

    # Assert
    assert len(pages) >= 1
    _, content, _ = pages[0]
    assert "meetings_count: 4" in content, (
        f"기존 meetings_count=3 에서 +1 해 4 가 되어야 합니다.\n내용:\n{content}"
    )
    assert str(meeting_date) in content, (
        f"last_seen 이 현재 meeting_date({meeting_date}) 로 갱신되지 않았습니다."
    )
    assert original_first_seen in content, (
        f"first_seen({original_first_seen}) 이 갱신 후 사라지면 안 됩니다. "
        "기존 first_seen 은 보존되어야 합니다."
    )


async def test_render_or_update_pages_derived_최근_결정_섹션_자동_채워짐():
    """meeting_decisions 에서 해당 인물이 participants 인 항목이 '## 최근 결정' 섹션에 삽입된다.

    Arrange: meeting_decisions 에 participants=["철수"] 인 ExtractedDecision 1건.
             LLM 은 role/topic 보강 응답 반환.
    Act: render_or_update_pages() 호출.
    Assert: "## 최근 결정" 섹션이 존재하고 결정 제목이 포함되어야 한다.
            이 섹션은 LLM 호출 없이(derived) 자동 생성이므로 call_count 와 무관.
    """
    # Arrange
    meeting_id = "abc12345"
    meeting_date = date(2026, 4, 29)

    role_topic_json = '{"role_update": null, "new_topics": []}'
    mock_llm = MockPersonLLM(responses=[role_topic_json])
    extractor = PersonExtractor(llm=mock_llm)

    person = ExtractedPerson(
        name="철수",
        name_normalized="철수",
        role="PM",
        first_seen_meeting_id=meeting_id,
        first_seen_date=str(meeting_date),
        last_seen_meeting_id=meeting_id,
        last_seen_date=str(meeting_date),
        topic_mentions=[],
        citations=[_make_citation(meeting_id=meeting_id)],
        confidence=8,
    )

    # 철수가 participants 에 포함된 결정사항
    decision = _make_extracted_decision(
        title="출시일 5월 1일 확정",
        slug="launch-date",
        participants=["철수", "영희"],
        meeting_id=meeting_id,
    )

    store = MockWikiStore()

    # Act
    pages = await extractor.render_or_update_pages(
        persons=[person],
        meeting_id=meeting_id,
        meeting_date=meeting_date,
        existing_store=store,
        meeting_decisions=[decision],
        meeting_new_actions=[],
        existing_open_actions=[],
    )

    # Assert
    assert len(pages) >= 1
    _, content, _ = pages[0]
    assert "## 최근 결정" in content, (
        f"'## 최근 결정' 섹션이 없습니다.\n내용:\n{content}"
    )
    assert "출시일 5월 1일 확정" in content or "launch-date" in content, (
        f"결정 항목이 '## 최근 결정' 섹션에 포함되지 않았습니다.\n내용:\n{content}"
    )


async def test_render_or_update_pages_derived_미해결_액션_섹션_자동_채워짐():
    """meeting_new_actions 에서 owner=="철수" 인 항목이 '## 미해결 액션아이템' 섹션에 삽입된다.

    Arrange: meeting_new_actions 에 owner="철수" 인 NewActionItem 1건.
             existing_open_actions 에 owner="철수" 인 OpenActionItem 1건.
             LLM 은 role/topic 보강 응답 반환.
    Act: render_or_update_pages() 호출.
    Assert: "## 미해결 액션아이템" 섹션이 존재하고 각 액션 설명이 포함되어야 한다.
            derived 섹션이므로 LLM 호출 없이 자동 생성.
    """
    # Arrange
    meeting_id = "abc12345"
    meeting_date = date(2026, 4, 29)

    role_topic_json = '{"role_update": null, "new_topics": []}'
    mock_llm = MockPersonLLM(responses=[role_topic_json])
    extractor = PersonExtractor(llm=mock_llm)

    person = ExtractedPerson(
        name="철수",
        name_normalized="철수",
        role="PM",
        first_seen_meeting_id=meeting_id,
        first_seen_date=str(meeting_date),
        last_seen_meeting_id=meeting_id,
        last_seen_date=str(meeting_date),
        topic_mentions=[],
        citations=[_make_citation(meeting_id=meeting_id)],
        confidence=8,
    )

    # 신규 액션 (이번 회의에서 발생)
    new_action = _make_new_action_item(
        owner="철수",
        description="캘린더 갱신",
        meeting_id=meeting_id,
    )
    # 기존 미완료 액션
    open_action = _make_open_action_item(
        owner="철수",
        description="이전 회의 보고서 작성",
        from_meeting_id="prev1234",
    )

    store = MockWikiStore()

    # Act
    pages = await extractor.render_or_update_pages(
        persons=[person],
        meeting_id=meeting_id,
        meeting_date=meeting_date,
        existing_store=store,
        meeting_decisions=[],
        meeting_new_actions=[new_action],
        existing_open_actions=[open_action],
    )

    # Assert
    assert len(pages) >= 1
    _, content, _ = pages[0]
    assert "## 미해결 액션아이템" in content, (
        f"'## 미해결 액션아이템' 섹션이 없습니다.\n내용:\n{content}"
    )
    assert "캘린더 갱신" in content, (
        f"신규 액션 '캘린더 갱신' 이 미해결 액션아이템 섹션에 없습니다.\n내용:\n{content}"
    )


async def test_render_or_update_pages_prd_템플릿_4섹션_헤더_포함():
    """PRD §4.2 people 템플릿의 4개 섹션 헤더가 모두 출력 페이지에 포함된다.

    PRD 필수 섹션:
      - ## 최근 결정 (latest 5)
      - ## 담당 프로젝트
      - ## 자주 언급하는 주제
      - ## 미해결 액션아이템

    Arrange: 신규 인물 + LLM role/topic 응답.
    Act: render_or_update_pages() 호출.
    Assert: frontmatter type=person 및 4섹션 헤더 모두 포함.
    """
    # Arrange
    meeting_id = "abc12345"
    meeting_date = date(2026, 4, 29)

    role_topic_json = '{"role_update": "PM", "new_topics": [{"topic": "일정관리", "citation_ts": "00:05:00"}]}'
    mock_llm = MockPersonLLM(responses=[role_topic_json])
    extractor = PersonExtractor(llm=mock_llm)

    person = ExtractedPerson(
        name="영희",
        name_normalized="영희",
        role=None,
        first_seen_meeting_id=meeting_id,
        first_seen_date=str(meeting_date),
        last_seen_meeting_id=meeting_id,
        last_seen_date=str(meeting_date),
        topic_mentions=[],
        citations=[_make_citation(meeting_id=meeting_id)],
        confidence=7,
    )

    store = MockWikiStore()

    # Act
    pages = await extractor.render_or_update_pages(
        persons=[person],
        meeting_id=meeting_id,
        meeting_date=meeting_date,
        existing_store=store,
        meeting_decisions=[],
        meeting_new_actions=[],
        existing_open_actions=[],
    )

    # Assert
    assert len(pages) >= 1
    _, content, _ = pages[0]

    # frontmatter type=person 확인
    fm_match = re.search(r"^---\n(.*?)\n---", content, re.DOTALL)
    assert fm_match, f"frontmatter 블록(--- ... ---) 이 없습니다.\n내용:\n{content}"
    fm_text = fm_match.group(1)
    assert "type: person" in fm_text, (
        f"frontmatter 에 'type: person' 이 없습니다.\nfrontmatter:\n{fm_text}"
    )

    # PRD §4.2 4섹션 헤더 모두 존재 확인
    for section in (
        "## 최근 결정",
        "## 담당 프로젝트",
        "## 자주 언급하는 주제",
        "## 미해결 액션아이템",
    ):
        assert section in content, (
            f"PRD §4.2 필수 섹션 '{section}' 이 출력에 없습니다.\n내용:\n{content}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 3. 환각 방지 + 한국어 정규화 (2건+)
# ══════════════════════════════════════════════════════════════════════════════


def test_normalize_person_slug_정규화_정책():
    """_normalize_person_slug 의 slug 정책을 검증한다.

    인터페이스 정의 §1.4 _normalize_person_slug 정책:
      - 한글 그대로 보존: "철수" → "철수"
      - 영문은 공백 → 언더스코어: "John Smith" → "John_Smith"
      - 한영 혼합: "박 PM" → "박_PM"
      - path traversal 거부: "../etc/pwd" → ValueError

    Arrange: 다양한 입력 문자열.
    Act: _normalize_person_slug() 호출.
    Assert: 각 정책이 정확히 적용된다.
    """
    from core.wiki.extractors.person import PersonExtractor

    # 한글 그대로 보존
    assert PersonExtractor._normalize_person_slug("철수") == "철수", (
        "한글 이름은 그대로 보존되어야 합니다."
    )

    # 영문 공백 → 언더스코어
    result_john = PersonExtractor._normalize_person_slug("John Smith")
    assert result_john == "John_Smith", (
        f"'John Smith' → 'John_Smith' 이어야 하지만 '{result_john}' 입니다."
    )

    # path traversal 거부 → ValueError
    with pytest.raises(ValueError, match=r"\.\.|/|path"):
        PersonExtractor._normalize_person_slug("../etc/pwd")


async def test_render_or_update_pages_인용_마커_포함():
    """render_or_update_pages 출력의 사실 문장에 인용 마커가 포함된다.

    PRD §4.2 "모든 사실 진술에 인용 마커 [meeting:id@HH:MM:SS] 강제" 정책 검증.

    Arrange: 신규 인물 + citation 이 있는 ExtractedPerson + LLM 응답.
    Act: render_or_update_pages() 호출.
    Assert: CITATION_PATTERN 에 매칭되는 인용 마커가 1개 이상 있어야 한다.
    """
    # Arrange
    meeting_id = "abc12345"
    meeting_date = date(2026, 4, 29)

    # LLM 이 인용이 포함된 페이지 응답을 반환하도록 설정
    role_topic_json = (
        f'{{"role_update": "PM", "new_topics": [{{'
        f'"topic": "pricing-strategy", "citation_ts": "00:10:00"}}]}}'
    )
    mock_llm = MockPersonLLM(responses=[role_topic_json])
    extractor = PersonExtractor(llm=mock_llm)

    person = ExtractedPerson(
        name="철수",
        name_normalized="철수",
        role="PM",
        first_seen_meeting_id=meeting_id,
        first_seen_date=str(meeting_date),
        last_seen_meeting_id=meeting_id,
        last_seen_date=str(meeting_date),
        topic_mentions=[
            TopicMention(
                topic="pricing-strategy",
                citation=_make_citation(meeting_id=meeting_id, ts_str="00:10:00", ts_seconds=600),
            )
        ],
        citations=[_make_citation(meeting_id=meeting_id)],
        confidence=9,
    )

    store = MockWikiStore()

    # Act
    pages = await extractor.render_or_update_pages(
        persons=[person],
        meeting_id=meeting_id,
        meeting_date=meeting_date,
        existing_store=store,
        meeting_decisions=[],
        meeting_new_actions=[],
        existing_open_actions=[],
    )

    # Assert
    assert len(pages) >= 1
    _, content, _ = pages[0]
    all_citations = CITATION_PATTERN.findall(content)
    assert len(all_citations) >= 1, (
        f"출력 페이지에 인용 마커 [meeting:id@HH:MM:SS] 가 없습니다. "
        f"PRD §4.2 인용 강제 정책 위반.\n내용:\n{content}"
    )
