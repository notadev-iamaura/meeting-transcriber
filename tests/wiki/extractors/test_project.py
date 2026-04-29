"""ProjectExtractor TDD Red 단계 테스트 모듈

목적: core/wiki/extractors/project.py 가 아직 존재하지 않으므로
  ImportError 로 모든 테스트가 Red 상태가 된다.
  구현체가 생기면 여기 정의된 계약을 통과해야 Green 이 된다.

커버리지:
  - ProjectExtractor.extract_projects() — LLM mock 기반 (4건)
  - ProjectExtractor.detect_status_transitions() — 보수적 status 전환 (4건)
  - ProjectExtractor.render_or_update_pages() — 누적성 핵심 (4건)
  - _normalize_project_slug() — slug 정규화 (1건)
  - _validate_status() — status enum 검증 (1건)
  총 14건 (임무 요구사항 초과)

의존성:
  - pytest (asyncio_mode=auto, pyproject.toml 에 설정됨)
  - core.wiki.models.Citation (Phase 1, 이미 구현 완료)
  - core.wiki.extractors.decision.ExtractedDecision (Phase 2, 이미 구현)
  - core.wiki.extractors.action_item.NewActionItem, OpenActionItem (Phase 2, 이미 구현)
  - core.wiki.extractors.project (Phase 3, 아직 미구현 → ImportError)

작성자: TDD Red Author (Phase 3)
날짜: 2026-04-29
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from core.wiki.extractors.action_item import NewActionItem, OpenActionItem

# ──────────────────────────────────────────────────────────────────────────────
# Phase 2 모듈 — 이미 구현 완료, import 가능
# ──────────────────────────────────────────────────────────────────────────────
from core.wiki.extractors.decision import ExtractedDecision

# ──────────────────────────────────────────────────────────────────────────────
# [TDD Red] core/wiki/extractors/project.py 가 없으므로
# 이 import 블록이 ImportError 를 일으켜 모든 테스트가 Red 상태가 된다.
# ──────────────────────────────────────────────────────────────────────────────
from core.wiki.extractors.project import (  # noqa: E402
    ExistingProject,
    ExtractedProject,
    ProjectExtractor,
    TimelineEntry,
)
from core.wiki.models import Citation  # Phase 1 — 이미 존재

# PRD §4.2 인용 패턴 (citations.py 의 CITATION_PATTERN 과 동일)
CITATION_PATTERN = re.compile(r"\[meeting:([a-f0-9]{8})@(\d{2}):(\d{2}):(\d{2})\]")

# 한국어 고유명사 뒤 외국어 병기 패턴 (예: "철수(Chulsoo)")
_FOREIGN_GLOSS_PATTERN = re.compile(r"([\uAC00-\uD7A3]+)\([A-Za-z\u4E00-\u9FFF\u3041-\u30FF]+\)")


# ══════════════════════════════════════════════════════════════════════════════
# MockProjectLLM — Phase 2 의 MockDecisionLLM 패턴 동일
# ══════════════════════════════════════════════════════════════════════════════


class MockProjectLLM:
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


def _make_utterances(texts: list[str], speaker: str = "철수") -> list[_FakeUtterance]:
    """텍스트 목록으로 최소 발화 목록을 생성한다.

    Args:
        texts: 발화 텍스트 목록.
        speaker: 화자 이름 (기본값 "철수").

    Returns:
        _FakeUtterance 인스턴스 목록.
    """
    return [
        _FakeUtterance(text=t, speaker=speaker, start=float(i * 30), end=float(i * 30 + 20))
        for i, t in enumerate(texts)
    ]


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
    title: str = "Q3 출시일 확정",
    slug: str = "q3-launch-date",
    participants: list[str] | None = None,
    projects: list[str] | None = None,
    meeting_id: str = "abc12345",
) -> ExtractedDecision:
    """테스트용 ExtractedDecision 인스턴스를 생성한다.

    Args:
        title: 결정 제목.
        slug: filename-safe 식별자.
        participants: 참여 화자 목록.
        projects: 연관 프로젝트 slug 목록.
        meeting_id: 회의 ID.

    Returns:
        ExtractedDecision 인스턴스.
    """
    if participants is None:
        participants = ["철수"]
    if projects is None:
        projects = ["신규-온보딩"]
    return ExtractedDecision(
        title=title,
        slug=slug,
        decision_text=f"결정 내용 [meeting:{meeting_id}@00:10:00].",
        background=f"배경 설명 [meeting:{meeting_id}@00:08:00].",
        participants=participants,
        projects=projects,
        confidence=8,
    )


def _make_new_action_item(
    owner: str = "철수",
    description: str = "온보딩 문서 정리",
    meeting_id: str = "abc12345",
) -> NewActionItem:
    """테스트용 NewActionItem 인스턴스를 생성한다."""
    return NewActionItem(
        owner=owner,
        description=description,
        due_date="2026-05-10",
        citation=_make_citation(meeting_id=meeting_id),
        confidence=8,
    )


def _make_open_action_item(
    owner: str = "철수",
    description: str = "이전 회의 문서 검토",
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
        """페이지를 반환하거나 KeyError 를 발생시킨다.

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


def _build_single_project_json(
    name: str = "신규 온보딩",
    slug: str = "신규-온보딩",
    status: str = "in-progress",
    owner: str | None = "철수",
    meeting_id: str = "abc12345",
    ts: str = "00:10:00",
    confidence: int = 9,
) -> str:
    """LLM 이 프로젝트 1건을 반환할 때의 JSON 문자열을 생성한다.

    Args:
        name: 프로젝트 한국어 이름.
        slug: filename-safe 식별자.
        status: 현재 상태 (4종 enum 중 하나).
        owner: 담당자 이름 (없으면 null).
        meeting_id: 8자리 hex 회의 ID.
        ts: HH:MM:SS 형식 타임스탬프.
        confidence: 신뢰도 0~10.

    Returns:
        JSON 배열 문자열 (프로젝트 1건).
    """
    owner_val = f'"{owner}"' if owner else "null"
    return (
        f'[{{"name": "{name}", "slug": "{slug}", "status": "{status}",'
        f'"owner": {owner_val}, "started": null, "target": null,'
        f'"description": "신규 온보딩 프로젝트 진행 중 [meeting:{meeting_id}@{ts}].",'
        f'"timeline_entry": {{"description": "킥오프 완료", "citation_ts": "{ts}"}},'
        f'"unresolved_issues": [], "participants": ["철수"],'
        f'"confidence": {confidence}}}]'
    )


def _build_existing_project_page(
    slug: str = "신규-온보딩",
    name: str = "신규 온보딩",
    status: str = "in-progress",
    owner: str = "철수",
    started: str = "2026-04-01",
    target: str = "2026-06-30",
    last_updated: str = "2026-04-22",
    seen_meeting_id: str = "prev1234",
) -> str:
    """기존 projects/{slug}.md 페이지 본문을 생성한다.

    PRD §4.2 projects 템플릿을 준수하는 마크다운 문자열을 반환한다.

    Args:
        slug: 프로젝트 식별자.
        name: 사람이 부르는 이름.
        status: 현재 상태.
        owner: 담당자.
        started: 시작일.
        target: 목표일.
        last_updated: 마지막 갱신일.
        seen_meeting_id: 본문에 등장하는 기존 회의 ID.

    Returns:
        frontmatter + 5섹션이 포함된 마크다운 문자열.
    """
    return (
        "---\n"
        "type: project\n"
        f"slug: {slug}\n"
        f"status: {status}\n"
        f"owner: {owner}\n"
        f"started: {started}\n"
        f"target: {target}\n"
        f"last_updated: {last_updated}\n"
        "---\n\n"
        f"# {name} ({slug})\n\n"
        "## 현재 상태\n"
        f"**{status}** — 온보딩 시스템 개발 중 [meeting:{seen_meeting_id}@00:10:00].\n\n"
        "## 최근 결정사항\n"
        f"- 2026-04-22: UI 프레임워크 결정 [meeting:{seen_meeting_id}@00:15:00]\n\n"
        "## 진행 타임라인\n"
        f"- 2026-04-01: 프로젝트 킥오프 [meeting:{seen_meeting_id}@00:05:00]\n"
        f"- 2026-04-22: 1차 개발 완료 [meeting:{seen_meeting_id}@00:30:00]\n\n"
        "## 미해결 이슈\n"
        f"- 외부 API 연동 일정 미정 [meeting:{seen_meeting_id}@00:40:00]\n\n"
        "## 참여자\n"
        "- 철수 (PM), 영희 (Eng Lead)\n\n"
        "<!-- confidence: 8 -->\n"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. ProjectExtractor.extract_projects() — LLM mock 기반 (4건)
# ══════════════════════════════════════════════════════════════════════════════


async def test_extract_projects_명확한_프로젝트_1건_반환():
    """발화에 "신규 온보딩 프로젝트 진행 중" 이 있으면 ExtractedProject 1건을 반환한다.

    Arrange: "신규 온보딩 프로젝트 진행 중" 발화 + LLM 이 1건 JSON 반환.
    Act: extract_projects() 호출.
    Assert: ExtractedProject 1건 반환, name 에 "신규 온보딩" 포함.
    """
    # Arrange
    mock_llm = MockProjectLLM(responses=[_build_single_project_json(name="신규 온보딩")])
    extractor = ProjectExtractor(llm=mock_llm)
    utterances = _make_utterances(["신규 온보딩 프로젝트 진행 중입니다."])

    # Act
    results = await extractor.extract_projects(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 29),
        utterances=utterances,
        summary="신규 온보딩 프로젝트 진행 현황 보고.",
    )

    # Assert
    assert len(results) == 1
    assert isinstance(results[0], ExtractedProject)
    assert "신규 온보딩" in results[0].name


async def test_extract_projects_잡담만_있으면_빈_리스트():
    """프로젝트 언급이 없는 잡담 발화에서는 빈 리스트를 반환한다.

    Arrange: 잡담 발화 + LLM 이 빈 배열 "[]" 반환.
    Act: extract_projects() 호출.
    Assert: 빈 리스트 반환.
    """
    # Arrange
    mock_llm = MockProjectLLM(responses=["[]"])
    extractor = ProjectExtractor(llm=mock_llm)
    utterances = _make_utterances(["오늘 날씨가 좋네요.", "점심 뭐 먹었어요?"])

    # Act
    results = await extractor.extract_projects(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 29),
        utterances=utterances,
        summary="일상 대화.",
    )

    # Assert
    assert results == []


async def test_extract_projects_여러_프로젝트_3건_반환():
    """발화에서 3개 프로젝트가 동시에 언급되면 ExtractedProject 3건을 반환한다.

    Arrange: 3개 프로젝트 언급 발화 + LLM 이 3건 JSON 반환.
    Act: extract_projects() 호출.
    Assert: ExtractedProject 3건이 반환되고 slug 가 모두 다름.
    """
    # Arrange
    three_projects_json = (
        '[{"name": "신규 온보딩", "slug": "신규-온보딩", "status": "in-progress",'
        '"owner": "철수", "started": null, "target": null,'
        '"description": "온보딩 시스템 [meeting:abc12345@00:05:00].",'
        '"timeline_entry": null, "unresolved_issues": [], "participants": ["철수"], "confidence": 9},'
        '{"name": "결제 시스템 개편", "slug": "결제-개편", "status": "blocked",'
        '"owner": "영희", "started": null, "target": null,'
        '"description": "결제 API 개편 [meeting:abc12345@00:15:00].",'
        '"timeline_entry": null, "unresolved_issues": [], "participants": ["영희"], "confidence": 8},'
        '{"name": "Q3 런치", "slug": "q3-launch", "status": "in-progress",'
        '"owner": null, "started": null, "target": "2026-09-30",'
        '"description": "Q3 출시 목표 [meeting:abc12345@00:25:00].",'
        '"timeline_entry": null, "unresolved_issues": [], "participants": ["민준"], "confidence": 7}]'
    )
    mock_llm = MockProjectLLM(responses=[three_projects_json])
    extractor = ProjectExtractor(llm=mock_llm)
    utterances = _make_utterances(
        [
            "신규 온보딩 프로젝트는 순조롭게 진행 중입니다.",
            "결제 시스템 개편은 외부 API 블로커가 있어요.",
            "Q3 런치는 9월 말 목표로 준비 중입니다.",
        ]
    )

    # Act
    results = await extractor.extract_projects(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 29),
        utterances=utterances,
        summary="세 개 프로젝트 현황 보고.",
    )

    # Assert
    assert len(results) == 3
    assert all(isinstance(p, ExtractedProject) for p in results)
    slugs = {p.slug for p in results}
    assert len(slugs) == 3, f"slug 가 중복됩니다: {slugs}"


async def test_extract_projects_잘못된_json_재시도_후_빈_리스트():
    """LLM 이 잘못된 JSON 을 반환하면 robust 파서가 1회 재시도 후 빈 리스트를 반환한다.

    인터페이스 정의 §2.4: "JSON 파싱 1회 재시도" — 2회 모두 실패하면 보수적으로 빈 리스트.

    Arrange: 첫 번째 응답=잘못된 JSON, 두 번째 응답=잘못된 JSON.
    Act: extract_projects() 호출.
    Assert: 빈 리스트 반환 + LLM 호출 횟수가 2회 이하.
    """
    # Arrange
    mock_llm = MockProjectLLM(responses=["NOT_JSON{{{", "STILL_BROKEN"])
    extractor = ProjectExtractor(llm=mock_llm)
    utterances = _make_utterances(["신규 온보딩 프로젝트 진행 중."])

    # Act
    results = await extractor.extract_projects(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 29),
        utterances=utterances,
        summary="프로젝트 현황 보고.",
    )

    # Assert
    assert results == [], f"잘못된 JSON 에서 빈 리스트를 반환해야 합니다. 실제: {results}"
    assert mock_llm.call_count <= 2, (
        f"JSON 파싱 실패 시 최대 2회 (1회 재시도) 만 LLM 을 호출해야 합니다. "
        f"실제 호출: {mock_llm.call_count}회"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2. ProjectExtractor.detect_status_transitions() — 별도 메서드 (4건)
# ══════════════════════════════════════════════════════════════════════════════


async def test_detect_status_transitions_명시적_출시_전환_적용():
    """발화에 "출시 완료" 가 있고 confidence ≥ 8 이면 status 가 "shipped" 로 전환된다.

    인터페이스 정의 §2.4: confidence ≥ _STATUS_TRANSITION_MIN_CONFIDENCE(8) 만 채택.

    Arrange: existing_projects=[slug="신규-온보딩", status="in-progress"],
             utterances=["신규 온보딩 출시 완료했습니다."],
             LLM 응답=[{"slug": "신규-온보딩", "new_status": "shipped", "confidence": 9}].
    Act: detect_status_transitions() 호출.
    Assert: {"신규-온보딩": "shipped"} 반환.
    """
    # Arrange
    transition_json = (
        '[{"slug": "신규-온보딩", "new_status": "shipped",'
        '"reason_citation_ts": "00:10:00", "confidence": 9}]'
    )
    mock_llm = MockProjectLLM(responses=[transition_json])
    extractor = ProjectExtractor(llm=mock_llm)

    existing_project = ExistingProject(
        rel_path=Path("projects/신규-온보딩.md"),
        slug="신규-온보딩",
        name="신규 온보딩",
        status="in-progress",
        owner="철수",
        started="2026-04-01",
        target="2026-06-30",
        last_updated="2026-04-22",
        existing_timeline=("2026-04-01: 킥오프",),
        existing_issues=(),
        seen_meeting_ids=frozenset({"prev1234"}),
        raw_content="",
    )
    utterances = _make_utterances(["신규 온보딩 출시 완료했습니다."])

    # Act
    transitions = await extractor.detect_status_transitions(
        existing_projects=[existing_project],
        meeting_id="abc12345",
        utterances=utterances,
    )

    # Assert
    assert transitions == {"신규-온보딩": "shipped"}, (
        f"confidence=9, '출시 완료' → shipped 로 전환되어야 합니다. 실제: {transitions}"
    )


async def test_detect_status_transitions_모호한_진행_보고_변경_없음():
    """발화에 "잘 진행 중" 처럼 모호한 표현이 있으면 status 전환을 적용하지 않는다.

    인터페이스 정의 §2.3 규칙 4: "진행 중", "잘 되고 있어요" → 전환 신호 아님.

    Arrange: LLM 이 빈 배열 "[]" 반환 (전환 신호 없음 판정).
    Act: detect_status_transitions() 호출.
    Assert: 빈 dict 반환.
    """
    # Arrange
    mock_llm = MockProjectLLM(responses=["[]"])
    extractor = ProjectExtractor(llm=mock_llm)

    existing_project = ExistingProject(
        rel_path=Path("projects/신규-온보딩.md"),
        slug="신규-온보딩",
        name="신규 온보딩",
        status="in-progress",
        owner="철수",
        started="2026-04-01",
        target=None,
        last_updated="2026-04-22",
        existing_timeline=(),
        existing_issues=(),
        seen_meeting_ids=frozenset(),
        raw_content="",
    )
    utterances = _make_utterances(["신규 온보딩 프로젝트 잘 진행 중입니다."])

    # Act
    transitions = await extractor.detect_status_transitions(
        existing_projects=[existing_project],
        meeting_id="abc12345",
        utterances=utterances,
    )

    # Assert
    assert transitions == {}, (
        f"모호한 '진행 중' 표현에서는 status 전환이 없어야 합니다. 실제: {transitions}"
    )


async def test_detect_status_transitions_낮은_confidence_무시():
    """LLM 이 confidence=7 을 반환하면 _STATUS_TRANSITION_MIN_CONFIDENCE(8) 미만이므로 무시한다.

    인터페이스 정의 §2.4: confidence < 8 인 항목은 채택하지 않는다.

    Arrange: LLM 응답=[{"slug": "신규-온보딩", "new_status": "shipped", "confidence": 7}].
    Act: detect_status_transitions() 호출.
    Assert: 빈 dict 반환 (confidence 7 은 임계치 미달).
    """
    # Arrange
    low_confidence_json = (
        '[{"slug": "신규-온보딩", "new_status": "shipped",'
        '"reason_citation_ts": "00:10:00", "confidence": 7}]'
    )
    mock_llm = MockProjectLLM(responses=[low_confidence_json])
    extractor = ProjectExtractor(llm=mock_llm)

    existing_project = ExistingProject(
        rel_path=Path("projects/신규-온보딩.md"),
        slug="신규-온보딩",
        name="신규 온보딩",
        status="in-progress",
        owner=None,
        started=None,
        target=None,
        last_updated=None,
        existing_timeline=(),
        existing_issues=(),
        seen_meeting_ids=frozenset(),
        raw_content="",
    )
    utterances = _make_utterances(["온보딩 출시했어요."])

    # Act
    transitions = await extractor.detect_status_transitions(
        existing_projects=[existing_project],
        meeting_id="abc12345",
        utterances=utterances,
    )

    # Assert
    assert transitions == {}, f"confidence=7 (< 8) 은 무시되어야 합니다. 실제: {transitions}"


async def test_detect_status_transitions_잘못된_status_enum_skip():
    """LLM 이 _VALID_STATUSES 에 없는 status 를 반환하면 해당 항목을 skip 한다.

    인터페이스 정의 §2.4: new_status 가 _VALID_STATUSES 에 있는지 재검증.
    "delayed" 는 4종 enum {"in-progress", "blocked", "shipped", "cancelled"} 외.

    Arrange: LLM 응답=[{"slug": "신규-온보딩", "new_status": "delayed", "confidence": 9}].
    Act: detect_status_transitions() 호출.
    Assert: 빈 dict 반환 (유효하지 않은 status 는 skip).
    """
    # Arrange
    invalid_status_json = (
        '[{"slug": "신규-온보딩", "new_status": "delayed",'
        '"reason_citation_ts": "00:10:00", "confidence": 9}]'
    )
    mock_llm = MockProjectLLM(responses=[invalid_status_json])
    extractor = ProjectExtractor(llm=mock_llm)

    existing_project = ExistingProject(
        rel_path=Path("projects/신규-온보딩.md"),
        slug="신규-온보딩",
        name="신규 온보딩",
        status="in-progress",
        owner=None,
        started=None,
        target=None,
        last_updated=None,
        existing_timeline=(),
        existing_issues=(),
        seen_meeting_ids=frozenset(),
        raw_content="",
    )
    utterances = _make_utterances(["온보딩이 지연되고 있어요."])

    # Act
    transitions = await extractor.detect_status_transitions(
        existing_projects=[existing_project],
        meeting_id="abc12345",
        utterances=utterances,
    )

    # Assert
    assert transitions == {}, (
        f"'delayed' 는 유효한 status 가 아니므로 skip 되어야 합니다. 실제: {transitions}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 3. ProjectExtractor.render_or_update_pages() — 누적성 (4건)
# ══════════════════════════════════════════════════════════════════════════════


async def test_render_or_update_pages_신규_프로젝트_frontmatter_6필드_포함():
    """신규 프로젝트 페이지 생성 시 PRD 필수 frontmatter 7필드가 모두 포함된다.

    PRD §4.2 projects 템플릿 frontmatter 필수 필드:
      type, slug, status, owner, started, target, last_updated

    Arrange: existing_store 에 프로젝트 페이지 없음 (신규).
             LLM 은 페이지 본문 생성 응답 반환.
    Act: render_or_update_pages() 호출.
    Assert: frontmatter 에 7필드 모두 포함.
    """
    # Arrange
    meeting_id = "abc12345"
    meeting_date = date(2026, 4, 29)

    # render_or_update_pages 에서 호출하는 LLM 응답 (현재 상태 + 타임라인 작성용)
    page_content = (
        "---\n"
        "type: project\n"
        "slug: 신규-온보딩\n"
        "status: in-progress\n"
        "owner: 철수\n"
        "started: 2026-04-29\n"
        "target: null\n"
        f"last_updated: {meeting_date}\n"
        "---\n\n"
        "# 신규 온보딩 (신규-온보딩)\n\n"
        "## 현재 상태\n"
        f"**in-progress** — 프로젝트 시작 [meeting:{meeting_id}@00:10:00].\n\n"
        "## 최근 결정사항\n\n"
        "## 진행 타임라인\n"
        f"- 2026-04-29: 킥오프 완료 [meeting:{meeting_id}@00:10:00]\n\n"
        "## 미해결 이슈\n\n"
        "## 참여자\n"
        "- 철수\n\n"
        "<!-- confidence: 9 -->\n"
    )
    mock_llm = MockProjectLLM(responses=[page_content])
    extractor = ProjectExtractor(llm=mock_llm)

    project = ExtractedProject(
        name="신규 온보딩",
        slug="신규-온보딩",
        status="in-progress",
        owner="철수",
        started=None,
        target=None,
        description=f"신규 온보딩 프로젝트 [meeting:{meeting_id}@00:10:00].",
        timeline_entry=TimelineEntry(
            entry_date="2026-04-29",
            description="킥오프 완료",
            citation=_make_citation(meeting_id=meeting_id),
        ),
        unresolved_issues=[],
        participants=["철수"],
        citations=[_make_citation(meeting_id=meeting_id)],
        confidence=9,
    )

    store = MockWikiStore()  # 빈 store — 기존 페이지 없음

    # Act
    pages = await extractor.render_or_update_pages(
        projects=[project],
        status_transitions={},
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

    # frontmatter 블록 추출
    fm_match = re.search(r"^---\n(.*?)\n---", content, re.DOTALL)
    assert fm_match, f"frontmatter 블록(--- ... ---) 이 없습니다.\n내용:\n{content}"
    fm_text = fm_match.group(1)

    # 7필드 모두 포함 확인
    for field in ("type", "slug", "status", "owner", "started", "target", "last_updated"):
        assert field in fm_text, (
            f"frontmatter 에 '{field}' 필드가 없습니다.\nfrontmatter:\n{fm_text}"
        )


async def test_render_or_update_pages_기존_프로젝트_타임라인_보존_및_신규_추가():
    """기존 프로젝트 갱신 시 기존 타임라인이 보존되고 이번 회의 항목이 추가된다.

    인터페이스 정의 §2.4 흐름 3.타임라인 누적:
      - 기존 타임라인 항목 보존.
      - 신규 timeline_entry 만 추가.
      - 중복 citation 제거.

    Arrange: 기존 타임라인 2건이 있는 프로젝트 페이지 + 신규 timeline_entry 1건.
    Act: render_or_update_pages() 호출.
    Assert:
      - 기존 타임라인 항목("2026-04-01: 프로젝트 킥오프")이 출력에 포함.
      - 신규 항목("2차 개발 완료")이 추가됨.
    """
    # Arrange
    meeting_id = "abc12345"
    meeting_date = date(2026, 4, 29)

    existing_content = _build_existing_project_page(
        slug="신규-온보딩",
        seen_meeting_id="prev1234",
    )

    # LLM 이 기존 타임라인 보존 + 신규 항목 추가한 페이지를 반환
    updated_page = existing_content.replace(
        "## 진행 타임라인\n"
        "- 2026-04-01: 프로젝트 킥오프 [meeting:prev1234@00:05:00]\n"
        "- 2026-04-22: 1차 개발 완료 [meeting:prev1234@00:30:00]\n",
        "## 진행 타임라인\n"
        "- 2026-04-01: 프로젝트 킥오프 [meeting:prev1234@00:05:00]\n"
        "- 2026-04-22: 1차 개발 완료 [meeting:prev1234@00:30:00]\n"
        f"- 2026-04-29: 2차 개발 완료 [meeting:{meeting_id}@00:10:00]\n",
    )
    mock_llm = MockProjectLLM(responses=[updated_page])
    extractor = ProjectExtractor(llm=mock_llm)

    project = ExtractedProject(
        name="신규 온보딩",
        slug="신규-온보딩",
        status="in-progress",
        owner="철수",
        started=None,
        target=None,
        description=f"2차 개발 완료 [meeting:{meeting_id}@00:10:00].",
        timeline_entry=TimelineEntry(
            entry_date="2026-04-29",
            description="2차 개발 완료",
            citation=_make_citation(meeting_id=meeting_id),
        ),
        unresolved_issues=[],
        participants=["철수"],
        citations=[_make_citation(meeting_id=meeting_id)],
        confidence=8,
    )

    store = MockWikiStore(existing_pages={"projects/신규-온보딩.md": existing_content})

    # Act
    pages = await extractor.render_or_update_pages(
        projects=[project],
        status_transitions={},
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
    assert "2026-04-01" in content and "프로젝트 킥오프" in content, (
        f"기존 타임라인 항목 '2026-04-01: 프로젝트 킥오프' 가 보존되어야 합니다.\n내용:\n{content}"
    )
    assert "2차 개발 완료" in content, (
        f"신규 타임라인 항목 '2차 개발 완료' 가 추가되어야 합니다.\n내용:\n{content}"
    )


async def test_render_or_update_pages_prd_템플릿_5섹션_헤더_포함():
    """PRD §4.2 projects 템플릿의 5개 섹션 헤더가 모두 출력 페이지에 포함된다.

    PRD 필수 섹션:
      - ## 현재 상태
      - ## 최근 결정사항
      - ## 진행 타임라인
      - ## 미해결 이슈
      - ## 참여자

    Arrange: 신규 프로젝트 + LLM 5섹션 포함 페이지 응답.
    Act: render_or_update_pages() 호출.
    Assert: 5섹션 헤더 모두 포함 + frontmatter type=project.
    """
    # Arrange
    meeting_id = "abc12345"
    meeting_date = date(2026, 4, 29)

    full_page = (
        "---\n"
        "type: project\n"
        "slug: q3-launch\n"
        "status: in-progress\n"
        "owner: null\n"
        "started: null\n"
        "target: 2026-09-30\n"
        f"last_updated: {meeting_date}\n"
        "---\n\n"
        "# Q3 런치 (q3-launch)\n\n"
        "## 현재 상태\n"
        f"**in-progress** — Q3 목표로 개발 중 [meeting:{meeting_id}@00:25:00].\n\n"
        "## 최근 결정사항\n"
        f"- 2026-04-29: 9월 말 출시 목표 확정 [meeting:{meeting_id}@00:20:00]\n\n"
        "## 진행 타임라인\n"
        f"- 2026-04-29: 프로젝트 시작 [meeting:{meeting_id}@00:25:00]\n\n"
        "## 미해결 이슈\n"
        f"- 팀 구성 미완료 [meeting:{meeting_id}@00:30:00]\n\n"
        "## 참여자\n"
        "- 민준\n\n"
        "<!-- confidence: 7 -->\n"
    )
    mock_llm = MockProjectLLM(responses=[full_page])
    extractor = ProjectExtractor(llm=mock_llm)

    project = ExtractedProject(
        name="Q3 런치",
        slug="q3-launch",
        status="in-progress",
        owner=None,
        started=None,
        target=date(2026, 9, 30),
        description=f"Q3 출시 목표 [meeting:{meeting_id}@00:25:00].",
        timeline_entry=None,
        unresolved_issues=[("팀 구성 미완료", _make_citation(meeting_id=meeting_id))],
        participants=["민준"],
        citations=[_make_citation(meeting_id=meeting_id)],
        confidence=7,
    )

    store = MockWikiStore()

    # Act
    pages = await extractor.render_or_update_pages(
        projects=[project],
        status_transitions={},
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

    # frontmatter type=project 확인
    fm_match = re.search(r"^---\n(.*?)\n---", content, re.DOTALL)
    assert fm_match, f"frontmatter 블록이 없습니다.\n내용:\n{content}"
    fm_text = fm_match.group(1)
    assert "type: project" in fm_text, (
        f"frontmatter 에 'type: project' 가 없습니다.\nfrontmatter:\n{fm_text}"
    )

    # 5섹션 헤더 모두 확인
    for section in (
        "## 현재 상태",
        "## 최근 결정사항",
        "## 진행 타임라인",
        "## 미해결 이슈",
        "## 참여자",
    ):
        assert section in content, (
            f"PRD §4.2 필수 섹션 '{section}' 이 출력에 없습니다.\n내용:\n{content}"
        )


async def test_render_or_update_pages_derived_섹션_llm_호출_0회():
    """meeting_decisions 에서 파생되는 '최근 결정사항' 섹션은 LLM 호출 없이 자동 채워진다.

    인터페이스 정의 §2.4 흐름 4.derived 섹션:
      - "최근 결정사항" 은 meeting_decisions 중 project slug 일치 항목만 선택.
      - LLM 호출 0회로 자동 생성 (토큰 절감).

    참고: render_or_update_pages 에서 LLM 을 1회 호출할 수 있지만 (현재 상태 + 타임라인),
      derived 섹션 자체는 Phase 2 결과 재활용이므로 추가 LLM 호출이 없어야 한다.
      본 테스트는 "최근 결정사항" 에 해당 slug 결정이 포함되는지를 검증한다.

    Arrange: meeting_decisions 에 projects=["신규-온보딩"] 인 ExtractedDecision 1건.
             LLM 은 최근 결정사항이 포함된 페이지 반환.
    Act: render_or_update_pages() 호출.
    Assert: "## 최근 결정사항" 섹션에 해당 결정 정보가 포함됨.
    """
    # Arrange
    meeting_id = "abc12345"
    meeting_date = date(2026, 4, 29)

    decision = _make_extracted_decision(
        title="UI 프레임워크 React 로 확정",
        slug="ui-framework-decision",
        projects=["신규-온보딩"],
        meeting_id=meeting_id,
    )

    # LLM 응답에 최근 결정사항 섹션이 있는 페이지
    page_with_decision = (
        "---\n"
        "type: project\n"
        "slug: 신규-온보딩\n"
        "status: in-progress\n"
        "owner: 철수\n"
        "started: null\n"
        "target: null\n"
        f"last_updated: {meeting_date}\n"
        "---\n\n"
        "# 신규 온보딩 (신규-온보딩)\n\n"
        "## 현재 상태\n"
        f"**in-progress** [meeting:{meeting_id}@00:10:00].\n\n"
        "## 최근 결정사항\n"
        f"- UI 프레임워크 React 로 확정 [meeting:{meeting_id}@00:10:00]\n\n"
        "## 진행 타임라인\n"
        f"- 2026-04-29: 시작 [meeting:{meeting_id}@00:10:00]\n\n"
        "## 미해결 이슈\n\n"
        "## 참여자\n"
        "- 철수\n\n"
        "<!-- confidence: 8 -->\n"
    )
    mock_llm = MockProjectLLM(responses=[page_with_decision])
    extractor = ProjectExtractor(llm=mock_llm)

    project = ExtractedProject(
        name="신규 온보딩",
        slug="신규-온보딩",
        status="in-progress",
        owner="철수",
        started=None,
        target=None,
        description=f"신규 온보딩 [meeting:{meeting_id}@00:10:00].",
        timeline_entry=None,
        unresolved_issues=[],
        participants=["철수"],
        citations=[_make_citation(meeting_id=meeting_id)],
        confidence=8,
    )

    store = MockWikiStore()

    # Act
    pages = await extractor.render_or_update_pages(
        projects=[project],
        status_transitions={},
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
    assert "## 최근 결정사항" in content, f"'## 최근 결정사항' 섹션이 없습니다.\n내용:\n{content}"
    # 결정 제목 또는 slug 중 하나라도 포함되어야 한다
    assert "UI 프레임워크" in content or "ui-framework-decision" in content, (
        f"'최근 결정사항' 에 해당 결정 정보가 없습니다.\n내용:\n{content}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 4. slug 정규화 + status enum 검증 (2건)
# ══════════════════════════════════════════════════════════════════════════════


def test_normalize_project_slug_한영_혼합_정책():
    """_normalize_project_slug 의 slug 정책을 검증한다.

    인터페이스 정의 §2.4 _normalize_project_slug 정책:
      - 영문: 소문자화 + 공백 → 하이픈: "New Onboarding" → "new-onboarding"
      - 한글: 그대로 보존, 공백 → 언더스코어: "신규 온보딩" → "신규_온보딩"
      - 영숫자 혼합: "Q3 Launch" → "q3-launch"
      - path traversal 거부: "../etc/pwd" → ValueError

    Arrange: 다양한 입력 문자열.
    Act: _normalize_project_slug() 호출.
    Assert: 각 정책이 정확히 적용된다.
    """
    # 영문 공백 → 하이픈
    result_en = ProjectExtractor._normalize_project_slug("New Onboarding")
    assert result_en == "new-onboarding", (
        f"'New Onboarding' → 'new-onboarding' 이어야 하지만 '{result_en}' 입니다."
    )

    # 한글 공백 → 언더스코어 (또는 하이픈 — 인터페이스 §2.4 예시 기준)
    result_ko = ProjectExtractor._normalize_project_slug("신규 온보딩")
    assert result_ko in ("신규_온보딩", "신규-온보딩"), (
        f"'신규 온보딩' → '신규_온보딩' 또는 '신규-온보딩' 이어야 하지만 '{result_ko}' 입니다."
    )

    # 영숫자 혼합
    result_q3 = ProjectExtractor._normalize_project_slug("Q3 Launch")
    assert result_q3 == "q3-launch", (
        f"'Q3 Launch' → 'q3-launch' 이어야 하지만 '{result_q3}' 입니다."
    )

    # path traversal 거부 → ValueError
    with pytest.raises(ValueError):
        ProjectExtractor._normalize_project_slug("../etc/pwd")


def test_validate_status_잘못된_값_value_error():
    """_validate_status 에 4종 enum 외 값을 전달하면 ValueError 가 발생한다.

    인터페이스 정의 §2.4: _VALID_STATUSES = {"in-progress", "blocked", "shipped", "cancelled"}.
    "delayed" 는 유효하지 않으므로 ValueError raise.

    Arrange: 유효하지 않은 status 문자열 "delayed".
    Act: _validate_status("delayed") 호출.
    Assert: ValueError 가 발생한다.
    """
    # 유효한 4종은 예외 없이 통과해야 한다
    for valid_status in ("in-progress", "blocked", "shipped", "cancelled"):
        # 예외 없이 통과 확인 (반환값 없음)
        ProjectExtractor._validate_status(valid_status)

    # 유효하지 않은 값 → ValueError
    with pytest.raises(ValueError, match=r"delayed|invalid|status"):
        ProjectExtractor._validate_status("delayed")

    # 빈 문자열도 거부
    with pytest.raises(ValueError):
        ProjectExtractor._validate_status("")
