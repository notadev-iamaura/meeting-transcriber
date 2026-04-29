"""ActionItemExtractor Phase 2.E 신규 기능 TDD 테스트.

검증 범위:
    - 작업 3: owner fuzzy matching (SPEAKER_00 ↔ "철수" 매핑)
    - 작업 4: due_date 상대 표현 ("내일/다음주" → ISO 날짜)

기존 정책 (string equality, 환각 방지) 회귀하지 않도록 함께 검증.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest

from core.wiki.extractors.action_item import (
    ActionItemExtractor,
    NewActionItem,
)
from core.wiki.models import Citation


FAKE_MEETING_ID = "abc12345"
FAKE_MEETING_DATE = date(2026, 4, 28)


# ─────────────────────────────────────────────────────────────────────────
# Mock LLM
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class _MockResponse:
    """단일 응답."""

    body: str
    raise_error: str | None = None


class _MockLLM:
    """ActionItem fuzzy matching/relative date 검증용 mock."""

    def __init__(self, responses: list[_MockResponse]) -> None:
        """응답 시퀀스를 저장."""
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    @property
    def model_name(self) -> str:
        """식별자."""
        return "mock-phase2e"

    async def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        """다음 응답 반환."""
        self.calls.append(
            {"system_prompt": system_prompt, "user_prompt": user_prompt}
        )
        assert self._responses, "mock responses 소진"
        resp = self._responses.pop(0)
        if resp.raise_error:
            raise RuntimeError(resp.raise_error)
        return resp.body


@dataclass
class _FakeUtterance:
    """Utterance Protocol 호환 fixture."""

    text: str
    speaker: str
    start: float
    end: float


def _make_utterances(specs: list[tuple[str, str, float]]) -> list[_FakeUtterance]:
    """(text, speaker, start) → _FakeUtterance 리스트."""
    return [
        _FakeUtterance(text=t, speaker=s, start=st, end=st + 5.0)
        for t, s, st in specs
    ]


# ─────────────────────────────────────────────────────────────────────────
# 1. owner fuzzy matching — speaker_name_map 사용
# ─────────────────────────────────────────────────────────────────────────


class TestOwnerFuzzyMatching:
    """SPEAKER_XX 라벨과 한국어 이름 매핑 — corrector 가 매핑을 제공하는 시나리오."""

    @pytest.mark.asyncio
    async def test_owner_korean_name_mapped_via_speaker_name_map(self) -> None:
        """utterances 가 SPEAKER_00 라벨, LLM 이 "철수" 추출, 매핑이 SPEAKER_00→철수.

        매핑이 제공되면 owner="철수" 로 보존되어야 한다 (None 강제하지 않음).
        """
        llm_json = (
            '[{"owner": "철수", "description": "캘린더 갱신",'
            ' "due_date": null, "project_slug": null,'
            ' "citation_ts": "00:25:12", "confidence": 9}]'
        )
        llm = _MockLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("캘린더 갱신할게요", "SPEAKER_00", 1512.0),
        ])
        # corrector 가 제공한 매핑 (실제 이름)
        speaker_name_map = {"SPEAKER_00": "철수", "SPEAKER_01": "영희"}

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
            speaker_name_map=speaker_name_map,
        )

        assert len(result) == 1
        assert result[0].owner == "철수", (
            "매핑된 한국어 이름이 보존되어야 합니다 (fuzzy matching)."
        )

    @pytest.mark.asyncio
    async def test_owner_speaker_label_mapped_to_korean_name(self) -> None:
        """LLM 이 "SPEAKER_00" 라벨을 그대로 추출하면 매핑된 "철수" 로 정규화."""
        llm_json = (
            '[{"owner": "SPEAKER_00", "description": "API 문서 정리",'
            ' "due_date": null, "project_slug": null,'
            ' "citation_ts": "00:30:00", "confidence": 8}]'
        )
        llm = _MockLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("API 문서 정리하겠습니다", "SPEAKER_00", 1800.0),
        ])
        speaker_name_map = {"SPEAKER_00": "철수"}

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
            speaker_name_map=speaker_name_map,
        )

        assert len(result) == 1
        # 매핑된 이름으로 정규화
        assert result[0].owner == "철수", (
            f"SPEAKER_00 가 '철수' 로 정규화되어야 함: '{result[0].owner}'"
        )

    @pytest.mark.asyncio
    async def test_owner_without_map_falls_back_to_string_equality(self) -> None:
        """speaker_name_map 미제공 시 기존 동작 (string equality + None 강제) 회귀 보장."""
        llm_json = (
            '[{"owner": "철수", "description": "캘린더 갱신",'
            ' "due_date": null, "project_slug": null,'
            ' "citation_ts": "00:25:12", "confidence": 9}]'
        )
        llm = _MockLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        # SPEAKER_00 라벨만 있고 매핑 없음 → "철수" 가 화자 set 에 없음
        utterances = _make_utterances([
            ("캘린더 갱신할게요", "SPEAKER_00", 1512.0),
        ])

        # speaker_name_map 인자 미제공 (Phase 1 호환)
        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
        )

        assert len(result) == 1
        # 기존 정책: 화자 set 에 없으므로 None 강제
        assert result[0].owner is None, (
            "speaker_name_map 미제공 시 string equality 폴백 동작 회귀."
        )

    @pytest.mark.asyncio
    async def test_owner_not_in_map_or_speakers_forced_to_none(self) -> None:
        """매핑이 있어도 hallucinated owner ("박민수") 는 None 강제 (환각 방지)."""
        llm_json = (
            '[{"owner": "박민수", "description": "보고서 작성",'
            ' "due_date": null, "project_slug": null,'
            ' "citation_ts": "00:05:00", "confidence": 6}]'
        )
        llm = _MockLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("보고서 작성해야 합니다", "SPEAKER_00", 300.0),
        ])
        speaker_name_map = {"SPEAKER_00": "철수"}

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
            speaker_name_map=speaker_name_map,
        )

        assert len(result) == 1
        # 박민수는 매핑에도, 라벨에도 없음 → None
        assert result[0].owner is None


# ─────────────────────────────────────────────────────────────────────────
# 2. due_date 상대 표현 — meeting_date 기준 ISO 변환
# ─────────────────────────────────────────────────────────────────────────


class TestRelativeDateResolver:
    """발화에 "내일/모레/다음주" 가 있을 때 meeting_date 기준 ISO 날짜 변환."""

    @pytest.mark.asyncio
    async def test_relative_date_tomorrow_korean(self) -> None:
        """'내일까지' → meeting_date + 1.

        meeting_date=2026-04-28, 발화 "내일까지" → due_date="2026-04-29".
        """
        llm_json = (
            '[{"owner": "철수", "description": "캘린더 갱신",'
            ' "due_date": null, "project_slug": null,'
            ' "citation_ts": "00:25:12", "confidence": 9}]'
        )
        llm = _MockLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("내일까지 캘린더 갱신할게요", "철수", 1512.0),
        ])

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
        )

        assert len(result) == 1
        assert result[0].due_date == "2026-04-29", (
            f"'내일까지' → 2026-04-29 로 변환되어야 함: '{result[0].due_date}'"
        )

    @pytest.mark.asyncio
    async def test_relative_date_tomorrow_english(self) -> None:
        """'tomorrow' (영문) → meeting_date + 1."""
        llm_json = (
            '[{"owner": "철수", "description": "doc update",'
            ' "due_date": null, "project_slug": null,'
            ' "citation_ts": "00:25:12", "confidence": 9}]'
        )
        llm = _MockLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("I will update the doc by tomorrow", "철수", 1512.0),
        ])

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
        )

        assert len(result) == 1
        assert result[0].due_date == "2026-04-29"

    @pytest.mark.asyncio
    async def test_relative_date_day_after_tomorrow_korean(self) -> None:
        """'모레' → meeting_date + 2 (2026-04-30)."""
        llm_json = (
            '[{"owner": "철수", "description": "리뷰",'
            ' "due_date": null, "project_slug": null,'
            ' "citation_ts": "00:25:12", "confidence": 9}]'
        )
        llm = _MockLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("모레 리뷰 부탁드립니다", "철수", 1512.0),
        ])

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
        )

        assert len(result) == 1
        assert result[0].due_date == "2026-04-30"

    @pytest.mark.asyncio
    async def test_relative_date_next_week_korean(self) -> None:
        """'다음주' → meeting_date + 7 (2026-05-05)."""
        llm_json = (
            '[{"owner": "철수", "description": "보고서",'
            ' "due_date": null, "project_slug": null,'
            ' "citation_ts": "00:25:12", "confidence": 9}]'
        )
        llm = _MockLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("다음주에 보고서 제출하겠습니다", "철수", 1512.0),
        ])

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
        )

        assert len(result) == 1
        assert result[0].due_date == "2026-05-05"

    @pytest.mark.asyncio
    async def test_relative_date_two_weeks_korean(self) -> None:
        """'다다음주' → meeting_date + 14 (2026-05-12)."""
        llm_json = (
            '[{"owner": "철수", "description": "기획안",'
            ' "due_date": null, "project_slug": null,'
            ' "citation_ts": "00:25:12", "confidence": 9}]'
        )
        llm = _MockLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("다다음주에 기획안 마무리할게요", "철수", 1512.0),
        ])

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
        )

        assert len(result) == 1
        assert result[0].due_date == "2026-05-12"

    @pytest.mark.asyncio
    async def test_explicit_iso_date_takes_priority_over_relative(self) -> None:
        """발화에 ISO 날짜가 명시되면 LLM 응답을 우선 (상대 표현 변환 안 함)."""
        llm_json = (
            '[{"owner": "철수", "description": "캘린더 갱신",'
            ' "due_date": "2026-05-15", "project_slug": null,'
            ' "citation_ts": "00:25:12", "confidence": 9}]'
        )
        llm = _MockLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("2026-05-15까지 캘린더 갱신할게요", "철수", 1512.0),
        ])

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
        )

        assert len(result) == 1
        # ISO 날짜가 명시되어 있으므로 LLM 응답값 그대로 사용
        assert result[0].due_date == "2026-05-15"

    @pytest.mark.asyncio
    async def test_unsupported_complex_relative_returns_none(self) -> None:
        """'이번주 금요일' / '다음 월요일' 같은 복잡한 표현은 미지원 → None."""
        llm_json = (
            '[{"owner": "철수", "description": "보고",'
            ' "due_date": null, "project_slug": null,'
            ' "citation_ts": "00:25:12", "confidence": 9}]'
        )
        llm = _MockLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("이번주 금요일까지 보고할게요", "철수", 1512.0),
        ])

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
        )

        assert len(result) == 1
        # 미지원 → 환각 방지 정책으로 None
        assert result[0].due_date is None

    @pytest.mark.asyncio
    async def test_no_date_mention_returns_none(self) -> None:
        """발화에 날짜 언급이 전혀 없으면 None (환각 방지 회귀 보장)."""
        llm_json = (
            '[{"owner": "철수", "description": "캘린더 갱신",'
            ' "due_date": "2026-05-15", "project_slug": null,'
            ' "citation_ts": "00:25:12", "confidence": 9}]'
        )
        llm = _MockLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("캘린더 갱신할게요", "철수", 1512.0),
        ])

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
        )

        assert len(result) == 1
        # 발화에 날짜 표현 0건 → None 강제
        assert result[0].due_date is None
