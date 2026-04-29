"""
ActionItemExtractor TDD Red 단계 테스트 모듈

목적: core/wiki/extractors/action_item.py 의 ActionItemExtractor,
  NewActionItem, OpenActionItem, ClosedActionItem 인터페이스를 TDD Red 단계로
  검증한다. 구현 파일이 아직 존재하지 않으므로 모든 테스트는 ImportError 로 실패한다.

작성 범위:
  - extract_new(): 신규 액션아이템 추출 (LLM 1회 호출, 4건)
  - detect_closed(): 기존 open 항목의 완료 감지 (LLM 1회 호출, 4건)
  - render_unified_page(): LLM 비호출 결정적 렌더링 (4건+)
  - 환각 방지 정책: due_date 추론 금지, owner 화자 검증 (2건)
  - id 생성 정책: 동일 description 이라도 회의 다르면 다른 id (1건)

의존성: pytest, pytest-asyncio (stdlib 외 금지, mock LLM 내부 정의)
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest

# ──────────────────────────────────────────────────────────────────────────────
# [TDD Red] core/wiki/extractors/action_item.py 가 아직 없으므로
# 이 import 가 ImportError 를 일으켜야 한다. (모든 테스트 Red 의 원인)
# ──────────────────────────────────────────────────────────────────────────────
from core.wiki.extractors.action_item import (  # noqa: E402
    ActionItemExtractor,
    ClosedActionItem,
    NewActionItem,
    OpenActionItem,
)

# Phase 1 모델은 구현되어 있으므로 import 가능 (테스트 픽스처 구성에 사용)
from core.wiki.models import Citation  # noqa: E402

# ──────────────────────────────────────────────────────────────────────────────
# 공통 상수
# ──────────────────────────────────────────────────────────────────────────────
FAKE_MEETING_ID = "abc12345"
FAKE_MEETING_DATE = date(2026, 4, 28)
FAKE_TS_STR = "00:25:12"
FAKE_TS_SECONDS = 25 * 60 + 12  # 1512


# ──────────────────────────────────────────────────────────────────────────────
# MockActionLLM — test_decision.py 의 MockDecisionLLM 과 이름 충돌 방지를 위해
# MockActionLLM 으로 별도 정의한다.
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _MockResponse:
    """MockActionLLM 이 시퀀스로 반환할 단일 응답.

    Attributes:
        body: 응답 본문 문자열.
        raise_error: 설정 시 RuntimeError 발생 (WikiLLMError 시뮬레이션 용도).
    """

    body: str
    raise_error: str | None = None


class MockActionLLM:
    """ActionItemExtractor 테스트 전용 mock LLM.

    호출 순서대로 미리 셋팅된 응답을 반환하며, 호출 내역을 calls 에 기록한다.
    WikiLLMClient Protocol 을 만족하도록 generate() 와 model_name 을 구현한다.
    """

    def __init__(self, responses: list[_MockResponse] | None = None) -> None:
        """초기 응답 시퀀스를 주입한다.

        Args:
            responses: 호출 순서대로 반환할 응답 시퀀스. None 이면 빈 리스트.
        """
        self._responses: list[_MockResponse] = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    @property
    def model_name(self) -> str:
        """고정값 "mock-action-llm" 반환 (로깅 식별용)."""
        return "mock-action-llm"

    async def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        """다음 응답을 pop 하여 반환한다. responses 가 비면 AssertionError."""
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        assert self._responses, "MockActionLLM: 응답 시퀀스가 소진되었습니다."
        resp = self._responses.pop(0)
        if resp.raise_error:
            # WikiLLMError 를 직접 import 할 수 없으므로 RuntimeError 로 시뮬레이션
            raise RuntimeError(f"mock_wiki_llm_error:{resp.raise_error}")
        return resp.body


# ──────────────────────────────────────────────────────────────────────────────
# 픽스처
# ──────────────────────────────────────────────────────────────────────────────

def _make_citation(
    meeting_id: str = FAKE_MEETING_ID,
    ts_str: str = FAKE_TS_STR,
    ts_seconds: int = FAKE_TS_SECONDS,
) -> Citation:
    """테스트용 Citation 인스턴스를 생성한다."""
    return Citation(
        meeting_id=meeting_id,
        timestamp_str=ts_str,
        timestamp_seconds=ts_seconds,
    )


@dataclass
class _FakeUtterance:
    """Utterance Protocol 을 만족하는 최소 테스트 픽스처.

    corrector 모듈에 의존하지 않고 Protocol duck-typing 으로 사용한다.
    """

    text: str
    speaker: str
    start: float
    end: float


def _make_utterances(specs: list[tuple[str, str, float]]) -> list[_FakeUtterance]:
    """(text, speaker, start_seconds) 튜플 목록을 _FakeUtterance 리스트로 변환한다."""
    return [
        _FakeUtterance(text=t, speaker=s, start=st, end=st + 5.0)
        for t, s, st in specs
    ]


def _make_open_item(
    item_id: str = "item001",
    owner: str = "철수",
    description: str = "MVP 데모 자료 작성",
    from_meeting_id: str = "prev1234",
    from_date: str = "2026-04-15",
) -> OpenActionItem:
    """테스트용 OpenActionItem 을 생성한다."""
    return OpenActionItem(
        item_id=item_id,
        owner=owner,
        description=description,
        from_meeting_id=from_meeting_id,
        from_date=from_date,
        citation=_make_citation(meeting_id=from_meeting_id, ts_str="01:00:00", ts_seconds=3600),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 1. extract_new() — 신규 액션아이템 추출 (4건)
# ──────────────────────────────────────────────────────────────────────────────

class TestExtractNew:
    """ActionItemExtractor.extract_new() 의 동작을 검증한다."""

    @pytest.mark.asyncio
    async def test_extract_new_clear_action_with_owner_and_due_date(self):
        """명확한 액션: '철수가 5월 1일까지 캘린더 갱신할게요' → NewActionItem(owner='철수', due_date=2026-05-01).

        LLM 이 owner, due_date, description 을 올바르게 파싱한 JSON 을 반환하면
        NewActionItem 으로 변환되어야 한다.
        """
        llm_json = (
            '[{"owner": "철수", "description": "캘린더 갱신",'
            ' "due_date": "2026-05-01", "project_slug": null,'
            ' "citation_ts": "00:25:12", "confidence": 9}]'
        )
        llm = MockActionLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("철수가 5월 1일까지 캘린더 갱신할게요", "철수", 1512.0),
        ])

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
        )

        assert len(result) == 1
        item = result[0]
        assert isinstance(item, NewActionItem)
        assert item.owner == "철수"
        assert item.description == "캘린더 갱신"
        assert item.due_date == "2026-05-01"
        assert isinstance(item.citation, Citation)
        assert item.citation.meeting_id == FAKE_MEETING_ID

    @pytest.mark.asyncio
    async def test_extract_new_owner_none_when_not_specified(self):
        """owner 미지정: '캘린더 갱신해야 합니다' → NewActionItem(owner=None 또는 화자 추론).

        발화에 명시적 owner 가 없을 때 LLM 이 null 을 반환하면 owner=None 이어야 한다.
        발화자 추론이 적용된다면 owner 는 speakers set 에 포함된 이름이어야 한다.
        """
        llm_json = (
            '[{"owner": null, "description": "캘린더 갱신",'
            ' "due_date": null, "project_slug": null,'
            ' "citation_ts": "00:01:30", "confidence": 7}]'
        )
        llm = MockActionLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("캘린더 갱신해야 합니다", "영희", 90.0),
        ])

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
        )

        assert len(result) == 1
        item = result[0]
        # owner 는 None 이거나, 추론 시 화자 목록({"영희"}) 내의 이름이어야 한다
        if item.owner is not None:
            speakers = {u.speaker for u in utterances}
            assert item.owner in speakers, (
                f"owner 추론 결과 '{item.owner}' 가 실제 화자 목록 {speakers} 에 없습니다."
            )

    @pytest.mark.asyncio
    async def test_extract_new_due_date_none_when_not_mentioned(self):
        """due_date 미지정: '캘린더 갱신할게요' → due_date=None.

        발화에 날짜 표현이 없으면 LLM 이 추론하더라도 due_date 는 None 이어야 한다
        (환각 방지 정책 §D4.2: due_date 추론 금지).
        """
        llm_json = (
            '[{"owner": "철수", "description": "캘린더 갱신",'
            ' "due_date": null, "project_slug": null,'
            ' "citation_ts": "00:25:12", "confidence": 8}]'
        )
        llm = MockActionLLM(responses=[_MockResponse(body=llm_json)])
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
        assert result[0].due_date is None

    @pytest.mark.asyncio
    async def test_extract_new_multiple_actions_from_one_meeting(self):
        """여러 액션: 한 회의에서 3개의 액션 → NewActionItem 3건.

        LLM 이 JSON 배열 3건을 반환하면 결과도 3건이어야 한다.
        """
        llm_json = (
            '[{"owner": "철수", "description": "캘린더 갱신",'
            ' "due_date": "2026-05-01", "project_slug": null,'
            ' "citation_ts": "00:25:12", "confidence": 9},'
            ' {"owner": "영희", "description": "마케팅팀에 일정 공유",'
            ' "due_date": null, "project_slug": "new-onboarding",'
            ' "citation_ts": "00:25:50", "confidence": 8},'
            ' {"owner": "민준", "description": "API 문서 업데이트",'
            ' "due_date": "2026-05-10", "project_slug": "api-v2",'
            ' "citation_ts": "00:30:00", "confidence": 7}]'
        )
        llm = MockActionLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("캘린더 갱신할게요", "철수", 1512.0),
            ("마케팅팀에 일정 공유할게요", "영희", 1550.0),
            ("API 문서 업데이트하겠습니다", "민준", 1800.0),
        ])

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
        )

        assert len(result) == 3
        owners = {item.owner for item in result}
        assert owners == {"철수", "영희", "민준"}
        # 모든 항목이 NewActionItem 타입인지 확인
        for item in result:
            assert isinstance(item, NewActionItem)


# ──────────────────────────────────────────────────────────────────────────────
# 2. detect_closed() — 기존 액션의 완료 감지 (4건)
# ──────────────────────────────────────────────────────────────────────────────

class TestDetectClosed:
    """ActionItemExtractor.detect_closed() 의 동작을 검증한다."""

    @pytest.mark.asyncio
    async def test_detect_closed_explicit_completion(self):
        """명시적 완료: existing_open 에 'MVP 데모 자료 작성', utterances 에 '완료했습니다' → ClosedActionItem 1건."""
        existing_open = [_make_open_item(
            item_id="a1",
            description="MVP 데모 자료 작성",
            owner="영희",
        )]
        llm_json = (
            '[{"item_index": 0, "closed_reason": "completed",'
            ' "closed_citation_ts": "00:00:30", "confidence": 9}]'
        )
        llm = MockActionLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("MVP 데모 자료 완료했습니다", "영희", 30.0),
        ])

        result = await extractor.detect_closed(
            existing_open=existing_open,
            meeting_id=FAKE_MEETING_ID,
            utterances=utterances,
        )

        assert len(result) == 1
        closed = result[0]
        assert isinstance(closed, ClosedActionItem)
        assert closed.original.item_id == "a1"
        assert closed.closed_reason == "completed"
        assert closed.closed_at_meeting_id == FAKE_MEETING_ID

    @pytest.mark.asyncio
    async def test_detect_closed_returns_empty_when_no_completion_mentioned(self):
        """완료 언급 없음: utterances 에 완료 신호 없으면 빈 리스트 반환."""
        existing_open = [_make_open_item(item_id="a2", description="API 문서 업데이트")]
        llm_json = "[]"
        llm = MockActionLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("오늘 회의 시작하겠습니다", "철수", 10.0),
            ("다음 주 계획 논의합시다", "영희", 30.0),
        ])

        result = await extractor.detect_closed(
            existing_open=existing_open,
            meeting_id=FAKE_MEETING_ID,
            utterances=utterances,
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_detect_closed_partial_progress_not_detected(self):
        """부분 진행: '70% 작성했습니다' 같은 부분 완료 발화 → 완료로 감지하지 않음.

        조건: 명확한 완료 언어('완료', '됐습니다', '마쳤습니다' 등)만 완료로 인정.
        """
        existing_open = [_make_open_item(item_id="a3", description="데모 자료 작성")]
        # LLM 이 부분 완료를 완료로 판단하지 않아 빈 배열 반환
        llm_json = "[]"
        llm = MockActionLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("데모 자료 70% 작성했습니다", "영희", 300.0),
        ])

        result = await extractor.detect_closed(
            existing_open=existing_open,
            meeting_id=FAKE_MEETING_ID,
            utterances=utterances,
        )

        # 부분 진행은 완료로 감지하지 않는다
        assert result == []

    @pytest.mark.asyncio
    async def test_detect_closed_ambiguous_statement_low_confidence_not_detected(self):
        """모호한 완료: '그건 잘 됐어요' 같은 모호 발화 → mock LLM confidence 낮으면 미감지.

        detect_closed 는 명확한 완료 언어에만 반응해야 한다. LLM 이 빈 배열을 반환하면
        모호한 발화로 인한 오탐을 방지한다.
        """
        existing_open = [_make_open_item(item_id="a4", description="일정 조율")]
        # 모호한 발화로 LLM 이 confidence 낮게 판단 → 빈 배열 반환
        llm_json = "[]"
        llm = MockActionLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([
            ("그건 잘 됐어요", "철수", 120.0),
        ])

        result = await extractor.detect_closed(
            existing_open=existing_open,
            meeting_id=FAKE_MEETING_ID,
            utterances=utterances,
        )

        assert result == []


# ──────────────────────────────────────────────────────────────────────────────
# 3. render_unified_page() — LLM 호출 0회 결정적 (4건+)
# ──────────────────────────────────────────────────────────────────────────────

class TestRenderUnifiedPage:
    """ActionItemExtractor.render_unified_page() 의 결정적 동작을 검증한다.

    render_unified_page 는 LLM 을 호출하지 않는다. MockActionLLM 에 응답을 주지
    않아도 정상 작동해야 한다 (결정적 함수 검증).
    """

    def _make_extractor(self) -> ActionItemExtractor:
        """LLM 응답 없는 MockActionLLM 으로 추출기를 만든다 (render 는 LLM 불필요)."""
        return ActionItemExtractor(llm=MockActionLLM(responses=[]))

    def _make_new_open_item(
        self,
        owner: str = "철수",
        description: str = "캘린더 갱신",
        due_date: str | None = "2026-05-01",
        meeting_id: str = FAKE_MEETING_ID,
    ) -> NewActionItem:
        """테스트용 NewActionItem 을 생성한다."""
        return NewActionItem(
            owner=owner,
            description=description,
            due_date=due_date,
            citation=_make_citation(meeting_id=meeting_id),
            confidence=8,
        )

    def _make_closed_item(
        self,
        open_item: OpenActionItem | None = None,
        closed_by: str = "영희",
        closed_meeting: str = FAKE_MEETING_ID,
        from_date: str = "2026-04-22",
    ) -> ClosedActionItem:
        """테스트용 ClosedActionItem 을 생성한다."""
        base = open_item or _make_open_item(
            item_id="c001",
            description="MVP 데모 자료 작성",
            owner="영희",
            from_date=from_date,
        )
        return ClosedActionItem(
            original=base,
            closed_by_speaker=closed_by,
            closed_at_meeting_id=closed_meeting,
            closed_citation=_make_citation(meeting_id=closed_meeting),
            closed_reason="completed",
        )

    @pytest.mark.asyncio
    async def test_render_prd_format_open_and_closed_headers(self):
        """PRD §4.2 형식 준수: '## Open (N)' + '## Closed (M)' 헤더, owner 별 그룹화.

        출력 마크다운에 '## Open (N)' 과 '## Closed (M)' 헤더가 포함되어야 하며
        N, M 은 실제 항목 수와 일치해야 한다.
        """
        extractor = self._make_extractor()
        new_open = [self._make_new_open_item(owner="철수")]
        newly_closed = [self._make_closed_item()]
        existing_open: list[OpenActionItem] = []
        existing_closed: list[ClosedActionItem] = []

        result = await extractor.render_unified_page(
            new_open=new_open,
            newly_closed=newly_closed,
            existing_open=existing_open,
            existing_closed=existing_closed,
            last_compiled_at="2026-04-28T14:00:00+09:00",
        )

        # Open 헤더: new_open 1건 (existing_open=[] 이므로 합계 1)
        assert re.search(r"##\s+Open\s+\(1\)", result), (
            f"'## Open (1)' 헤더를 찾을 수 없습니다.\n출력:\n{result}"
        )
        # Closed 헤더: newly_closed 1건 + existing_closed 0건 = 1
        assert re.search(r"##\s+Closed\s+\(1\)", result), (
            f"'## Closed (1)' 헤더를 찾을 수 없습니다.\n출력:\n{result}"
        )

    @pytest.mark.asyncio
    async def test_render_empty_input_still_outputs_headers(self):
        """빈 입력: open=[], closed=[] → '## Open (0)' + '## Closed (0)' 헤더 항상 출력.

        항목이 하나도 없어도 두 헤더는 반드시 존재해야 한다.
        """
        extractor = self._make_extractor()

        result = await extractor.render_unified_page(
            new_open=[],
            newly_closed=[],
            existing_open=[],
            existing_closed=[],
            last_compiled_at="2026-04-28T00:00:00+09:00",
        )

        assert re.search(r"##\s+Open\s+\(0\)", result), (
            f"'## Open (0)' 헤더를 찾을 수 없습니다.\n출력:\n{result}"
        )
        assert re.search(r"##\s+Closed\s+\(0\)", result), (
            f"'## Closed (0)' 헤더를 찾을 수 없습니다.\n출력:\n{result}"
        )

    @pytest.mark.asyncio
    async def test_render_frontmatter_type_and_last_compiled(self):
        """frontmatter: type=action_items, last_compiled ISO 8601 포함.

        PRD §4.2 템플릿에 따라 YAML frontmatter 에 type 과 last_compiled 가 있어야 한다.
        """
        extractor = self._make_extractor()
        last_compiled = "2026-04-28T14:00:00+09:00"

        result = await extractor.render_unified_page(
            new_open=[],
            newly_closed=[],
            existing_open=[],
            existing_closed=[],
            last_compiled_at=last_compiled,
        )

        # frontmatter 블록 존재 확인
        assert result.startswith("---"), "frontmatter 가 '---' 로 시작해야 합니다."
        assert "type: action_items" in result, (
            "frontmatter 에 'type: action_items' 가 없습니다."
        )
        assert last_compiled in result, (
            f"frontmatter 에 last_compiled '{last_compiled}' 가 없습니다."
        )

    @pytest.mark.asyncio
    async def test_render_citation_marker_preserved(self):
        """인용 마커 보존: NewActionItem.citation 의 [meeting:id@HH:MM:SS] 가 결과 마크다운에 출력.

        D1 방어를 위해 모든 사실 줄에는 인용이 포함되어야 한다.
        """
        extractor = self._make_extractor()
        citation = Citation(
            meeting_id="aabbccdd",
            timestamp_str="00:25:12",
            timestamp_seconds=1512,
        )
        new_open = [NewActionItem(
            owner="철수",
            description="캘린더 갱신",
            due_date="2026-05-01",
            citation=citation,
            confidence=8,
        )]

        result = await extractor.render_unified_page(
            new_open=new_open,
            newly_closed=[],
            existing_open=[],
            existing_closed=[],
            last_compiled_at="2026-04-28T14:00:00+09:00",
        )

        expected_marker = "[meeting:aabbccdd@00:25:12]"
        assert expected_marker in result, (
            f"인용 마커 '{expected_marker}' 가 출력에서 누락되었습니다.\n출력:\n{result}"
        )

    @pytest.mark.asyncio
    async def test_render_newly_closed_moves_to_closed_section_with_strikethrough(self):
        """closed 변환: newly_closed 항목은 Closed 섹션으로 이동, ~~취소선~~, 'Closed by' 인용 추가.

        PRD §4.2 Closed 섹션 형식:
          - [x] ~~{description}~~ [{citation}]
          - Closed by: {speaker} [{closed_citation}]
        """
        extractor = self._make_extractor()
        open_item = _make_open_item(
            item_id="c001",
            owner="영희",
            description="MVP 데모 자료 작성",
            from_date="2026-04-15",
        )
        closed_item = ClosedActionItem(
            original=open_item,
            closed_by_speaker="영희",
            closed_at_meeting_id=FAKE_MEETING_ID,
            closed_citation=Citation(
                meeting_id=FAKE_MEETING_ID,
                timestamp_str="00:00:30",
                timestamp_seconds=30,
            ),
            closed_reason="completed",
        )

        result = await extractor.render_unified_page(
            new_open=[],
            newly_closed=[closed_item],
            existing_open=[open_item],  # open 에 있던 항목이 제거되어야 함
            existing_closed=[],
            last_compiled_at="2026-04-28T14:00:00+09:00",
        )

        # Open 섹션에서 해당 항목이 제거되어야 함
        assert re.search(r"##\s+Open\s+\(0\)", result), (
            "newly_closed 항목이 Open 섹션에서 제거되지 않았습니다."
        )
        # 취소선 형식
        assert "~~MVP 데모 자료 작성~~" in result, (
            "Closed 섹션에 취소선(~~...~~) 형식이 없습니다."
        )
        # Closed by 인용
        assert "Closed by" in result, (
            "Closed 섹션에 'Closed by' 표기가 없습니다."
        )
        # Closed 인용 마커
        assert f"[meeting:{FAKE_MEETING_ID}@00:00:30]" in result, (
            "Closed 인용 마커가 출력에서 누락되었습니다."
        )


# ──────────────────────────────────────────────────────────────────────────────
# 4. 환각 방지 정책 (2건)
# ──────────────────────────────────────────────────────────────────────────────

class TestHallucinationPrevention:
    """ActionItemExtractor 의 환각 방지 정책을 검증한다."""

    @pytest.mark.asyncio
    async def test_due_date_forced_none_even_if_llm_infers(self):
        """due_date 추론 금지: 발화에 날짜 미언급 시 LLM 이 추론해도 due_date=None 강제.

        LLM 이 '다음 주쯤' 같은 표현을 보고 날짜를 추론해 반환해도,
        발화에 명시적 날짜가 없으면 구현이 due_date=None 으로 강제해야 한다.
        이 테스트는 LLM 이 추론 날짜를 반환하는 시나리오를 시뮬레이션하여
        최종 결과가 None 인지 검증한다.
        """
        # LLM 이 추론한 날짜를 반환하는 시나리오
        llm_json = (
            '[{"owner": "철수", "description": "캘린더 갱신",'
            ' "due_date": "2026-05-05", "project_slug": null,'
            ' "citation_ts": "00:10:00", "confidence": 5}]'
        )
        llm = MockActionLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        # 발화에 명시적 날짜 없음 — '다음 주' 같은 모호한 표현도 없음
        utterances = _make_utterances([
            ("캘린더 갱신할게요", "철수", 600.0),
        ])

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
        )

        # 구현은 발화를 분석해 명시적 날짜가 없으면 due_date=None 으로 강제해야 한다.
        # 만약 구현이 LLM 응답을 그대로 사용한다면 이 테스트는 실패 → 환각 감지.
        # (구현 의도: utterances 내 날짜 패턴 확인 후 LLM 응답 오버라이드)
        assert len(result) == 1
        # due_date 는 utterances 에서 추출된 값이어야 하며, LLM 추론 값이면 안 됨
        # 발화에 날짜가 없으므로 None 이 정답
        assert result[0].due_date is None, (
            f"due_date 추론 금지 정책 위반: 발화에 날짜 미언급 시 None 이어야 하지만 "
            f"'{result[0].due_date}' 가 반환되었습니다."
        )

    @pytest.mark.asyncio
    async def test_owner_not_in_speakers_set_forced_to_none(self):
        """owner 추론 검증: LLM 이 실제 화자 목록에 없는 owner 반환 시 None 으로 대체.

        LLM 이 hallucinate 한 owner 이름이 실제 utterances 의 화자 목록에 없으면
        owner=None 으로 강제해야 한다.
        """
        # LLM 이 존재하지 않는 화자 "박민수" 를 owner 로 반환
        llm_json = (
            '[{"owner": "박민수", "description": "보고서 작성",'
            ' "due_date": null, "project_slug": null,'
            ' "citation_ts": "00:05:00", "confidence": 6}]'
        )
        llm = MockActionLLM(responses=[_MockResponse(body=llm_json)])
        extractor = ActionItemExtractor(llm=llm)
        # 실제 화자는 "철수"와 "영희" 뿐
        utterances = _make_utterances([
            ("보고서 작성해야 해요", "철수", 300.0),
            ("맞아요", "영희", 310.0),
        ])

        result = await extractor.extract_new(
            meeting_id=FAKE_MEETING_ID,
            meeting_date=FAKE_MEETING_DATE,
            utterances=utterances,
        )

        assert len(result) == 1
        # "박민수" 는 실제 화자 {"철수", "영희"} 에 없으므로 None 으로 강제
        assert result[0].owner is None, (
            f"owner 검증 실패: '박민수' 는 실제 화자 목록에 없으므로 None 이어야 하지만 "
            f"'{result[0].owner}' 가 반환되었습니다."
        )


# ──────────────────────────────────────────────────────────────────────────────
# 5. id 생성 정책: 동일 description 이라도 다른 회의면 다른 id
# ──────────────────────────────────────────────────────────────────────────────

class TestIdGeneration:
    """OpenActionItem.item_id 생성 정책을 검증한다.

    인터페이스 정의에 따르면 item_id 는
    SHA-1(owner + description + from_meeting_id) 8자리 이다.
    같은 owner + description 이라도 from_meeting_id 가 다르면 다른 id 가 되어야 한다.
    """

    def test_same_description_different_meeting_produces_different_ids(self):
        """동일 description 이라도 다른 회의 ID면 item_id 가 달라야 한다.

        item_id 생성 시 from_meeting_id 를 포함하므로 같은 담당자·설명이어도
        회의가 다르면 반드시 다른 id 가 생성되어야 한다.
        """
        common_owner = "철수"
        common_description = "캘린더 갱신"
        meeting_a = "aaaaaaaa"
        meeting_b = "bbbbbbbb"

        item_a = OpenActionItem(
            item_id=_compute_item_id(common_owner, common_description, meeting_a),
            owner=common_owner,
            description=common_description,
            from_meeting_id=meeting_a,
            from_date="2026-04-20",
            citation=_make_citation(meeting_id=meeting_a),
        )
        item_b = OpenActionItem(
            item_id=_compute_item_id(common_owner, common_description, meeting_b),
            owner=common_owner,
            description=common_description,
            from_meeting_id=meeting_b,
            from_date="2026-04-28",
            citation=_make_citation(meeting_id=meeting_b),
        )

        assert item_a.item_id != item_b.item_id, (
            "동일 owner+description 이라도 from_meeting_id 가 다르면 item_id 가 달라야 합니다. "
            f"item_a.item_id={item_a.item_id!r}, item_b.item_id={item_b.item_id!r}"
        )

    def test_same_description_same_meeting_produces_same_id(self):
        """동일 owner + description + meeting_id 조합은 항상 동일한 id 를 생성한다.

        결정적 id 생성 — 같은 입력은 항상 같은 결과를 반환해야 한다.
        """
        owner = "영희"
        description = "API 문서 정리"
        meeting_id = "cccccccc"

        id1 = _compute_item_id(owner, description, meeting_id)
        id2 = _compute_item_id(owner, description, meeting_id)

        assert id1 == id2, "같은 입력에 대해 id 가 달라지면 안 됩니다."
        # id 는 8자리 hex
        assert len(id1) == 8, f"item_id 는 8자리여야 하지만 {len(id1)}자리입니다."
        assert all(c in "0123456789abcdef" for c in id1), (
            f"item_id '{id1}' 가 hex 문자만 포함하지 않습니다."
        )


def _compute_item_id(owner: str, description: str, from_meeting_id: str) -> str:
    """테스트용 item_id 계산 함수.

    인터페이스 정의(SHA-1(owner + description + from_meeting_id) 8자리)를
    테스트에서 재현하여 OpenActionItem 생성 시 정확한 id 를 주입한다.

    Args:
        owner: 담당자 이름.
        description: 작업 설명.
        from_meeting_id: 최초 등장 회의 ID.

    Returns:
        SHA-1 해시의 앞 8자리 소문자 hex 문자열.
    """
    raw = f"{owner}{description}{from_meeting_id}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


# ──────────────────────────────────────────────────────────────────────────────
# 6. detect_closed() 단락(short-circuit) — existing_open=[] 시 LLM 호출 없음
# ──────────────────────────────────────────────────────────────────────────────

class TestDetectClosedShortCircuit:
    """detect_closed 의 성능 최적화: existing_open=[] 이면 LLM 호출 없이 즉시 반환."""

    @pytest.mark.asyncio
    async def test_detect_closed_short_circuits_when_existing_open_is_empty(self):
        """existing_open=[] → LLM 호출 0회 + 빈 리스트 반환.

        인터페이스 정의에 따르면 existing_open 이 빈 리스트면
        LLM 호출 없이 즉시 빈 리스트를 반환해야 한다(성능 최적화).
        """
        # responses 가 있어도 호출되면 안 됨
        llm = MockActionLLM(responses=[_MockResponse(body="should-not-be-called")])
        extractor = ActionItemExtractor(llm=llm)
        utterances = _make_utterances([("완료했습니다", "철수", 60.0)])

        result = await extractor.detect_closed(
            existing_open=[],
            meeting_id=FAKE_MEETING_ID,
            utterances=utterances,
        )

        assert result == [], "existing_open=[] 이면 빈 리스트를 반환해야 합니다."
        assert len(llm.calls) == 0, (
            f"existing_open=[] 일 때 LLM 호출이 발생했습니다 ({len(llm.calls)}회)."
        )


# ──────────────────────────────────────────────────────────────────────────────
# 7. render_unified_page() — confidence 마커 (D3 자동 통과)
# ──────────────────────────────────────────────────────────────────────────────

class TestRenderConfidenceMarker:
    """render_unified_page 출력에 D3 자동 통과용 confidence 마커가 포함되는지 검증한다."""

    @pytest.mark.asyncio
    async def test_render_includes_confidence_marker(self):
        """출력 마지막에 '<!-- confidence: N -->' 마커 존재 (D3 자동 통과 보장).

        render_unified_page 는 LLM 을 호출하지 않고 결정적으로 마커를 삽입해야 한다.
        N 은 new_open + newly_closed 의 confidence 평균이다.
        """
        extractor = ActionItemExtractor(llm=MockActionLLM(responses=[]))
        new_open = [NewActionItem(
            owner="철수",
            description="캘린더 갱신",
            citation=_make_citation(),
            confidence=8,
        )]

        result = await extractor.render_unified_page(
            new_open=new_open,
            newly_closed=[],
            existing_open=[],
            existing_closed=[],
            last_compiled_at="2026-04-28T14:00:00+09:00",
        )

        assert re.search(r"<!--\s*confidence:\s*\d+\s*-->", result), (
            f"'<!-- confidence: N -->' 마커가 출력에서 누락되었습니다.\n출력:\n{result}"
        )
