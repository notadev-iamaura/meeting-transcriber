"""DecisionExtractor TDD Red 단계 테스트 모듈

목적: core/wiki/extractors/decision.py 가 아직 존재하지 않으므로
  ImportError 로 모든 테스트가 Red 상태가 된다.
  구현체가 생기면 여기 정의된 계약을 통과해야 Green 이 된다.

커버리지:
  - DecisionExtractor.extract() — LLM mock 기반 5건
  - ExtractedDecision dataclass — 2건
  - render_pages() — PRD §4.2 템플릿 정확 준수 5건
  - 한국어 고유명사 병기 금지 후처리 — 1건
  총 13건

의존성:
  - pytest (asyncio_mode=auto, pyproject.toml 에 설정됨)
  - core.wiki.models.Citation, PageType (Phase 1, 이미 구현 완료)
  - core.wiki.extractors.decision (Phase 2, 아직 미구현 → ImportError)

작성자: TDD Red Author
날짜: 2026-04-28
"""

from __future__ import annotations

import re
from dataclasses import fields
from datetime import date

import pytest

# ──────────────────────────────────────────────────────────────────────────────
# [TDD Red] core/wiki/extractors/decision.py 가 없으므로
# 이 import 블록이 ImportError 를 일으켜 모든 테스트가 Red 상태가 된다.
# ──────────────────────────────────────────────────────────────────────────────
from core.wiki.extractors.decision import (  # noqa: E402
    ActionItemRef,
    DecisionExtractor,
    ExtractedDecision,
)
from core.wiki.models import Citation  # Phase 1 — 이미 존재

# PRD §4.2 results 검증에 사용할 인용 패턴 (citations.py 의 CITATION_PATTERN 과 동일)
CITATION_PATTERN = re.compile(r"\[meeting:([a-f0-9]{8})@(\d{2}):(\d{2}):(\d{2})\]")

# 한국어 고유명사 뒤에 영어/중국어 병기가 붙는 패턴 (예: "배미령(Baimilong)")
_FOREIGN_GLOSS_PATTERN = re.compile(r"([\uAC00-\uD7A3]+)\([A-Za-z\u4E00-\u9FFF]+\)")


# ══════════════════════════════════════════════════════════════════════════════
# 테스트용 Mock 및 Fixture
# ══════════════════════════════════════════════════════════════════════════════


class MockDecisionLLM:
    """테스트용 WikiLLMClient 구현. 미리 설정된 응답을 순서대로 반환한다."""

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
        return "mock-exaone"


class _SimpleUtterance:
    """테스트용 Utterance Protocol 최소 구현체."""

    def __init__(self, text: str, speaker: str, start: float, end: float) -> None:
        """발화 데이터를 저장한다."""
        self.text = text
        self.speaker = speaker
        self.start = start
        self.end = end


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


def _make_utterances(
    decisions: list[tuple[str, str, float, float]] | None = None,
) -> list[_SimpleUtterance]:
    """테스트용 발화 목록을 생성하는 헬퍼 fixture.

    Args:
        decisions: (text, speaker, start, end) 튜플 목록.
            None 이면 기본 잡담 발화 2개를 반환한다.

    Returns:
        _SimpleUtterance 인스턴스 목록.
    """
    if decisions is None:
        return [
            _SimpleUtterance("오늘 날씨가 좋네요.", "SPEAKER_00", 0.0, 3.0),
            _SimpleUtterance("네, 산책하기 좋겠어요.", "SPEAKER_01", 3.5, 6.0),
        ]
    return [
        _SimpleUtterance(text, speaker, start, end)
        for text, speaker, start, end in decisions
    ]


def _make_decision_utterances() -> list[_SimpleUtterance]:
    """출시일 결정 흐름을 담은 테스트용 발화 목록을 반환한다."""
    return _make_utterances(
        decisions=[
            ("QA 가 5일 더 필요하다고 합니다.", "SPEAKER_01", 1110.0, 1115.0),
            ("그러면 5월 1일 출시로 가시죠.", "SPEAKER_00", 1425.0, 1430.0),
            ("동의합니다.", "SPEAKER_01", 1432.0, 1434.0),
        ]
    )


def _build_single_decision_json(
    meeting_id: str = "abc12345",
    ts: str = "00:23:45",
) -> str:
    """LLM 이 결정사항 1건을 반환할 때의 JSON 문자열을 생성한다.

    Args:
        meeting_id: 8자리 hex 회의 ID.
        ts: HH:MM:SS 형식 타임스탬프.

    Returns:
        JSON 배열 문자열 (결정사항 1건).
    """
    return (
        f'[{{"title": "신규 온보딩 출시일을 5월 1일로 확정",'
        f'"decision_text": "2026-05-01 출시 합의 [meeting:{meeting_id}@{ts}]",'
        f'"background": "QA 5일 추가 필요 [meeting:{meeting_id}@00:18:30]",'
        f'"follow_ups": [{{"owner": "SPEAKER_00", "description": "캘린더 갱신",'
        f'"citation_ts": "00:25:12"}}],'
        f'"participants": ["SPEAKER_00", "SPEAKER_01"],'
        f'"projects": ["new-onboarding"],'
        f'"confidence": 9}}]'
    )


def _build_rendered_page(
    meeting_id: str = "abc12345",
    ts: str = "00:23:45",
    confidence: int = 9,
) -> str:
    """PRD §4.2 decisions 템플릿에 맞는 렌더링된 페이지 본문을 반환한다.

    Args:
        meeting_id: 8자리 hex 회의 ID.
        ts: HH:MM:SS 형식 타임스탬프.
        confidence: 신뢰도 정수 (0~10).

    Returns:
        frontmatter + 섹션 4개 + confidence 마커가 포함된 마크다운 문자열.
    """
    return (
        "---\n"
        "type: decision\n"
        "date: 2026-04-28\n"
        f"meeting_id: {meeting_id}\n"
        "status: confirmed\n"
        "participants: [SPEAKER_00, SPEAKER_01]\n"
        "projects: [new-onboarding]\n"
        f"confidence: {confidence}\n"
        "created_at: 2026-04-28T10:00:00+09:00\n"
        "updated_at: 2026-04-28T10:00:00+09:00\n"
        "---\n\n"
        "# 신규 온보딩 출시일을 5월 1일로 확정\n\n"
        "## 결정 내용\n"
        f"2026-05-01 출시 합의 [meeting:{meeting_id}@{ts}].\n\n"
        "## 배경\n"
        f"QA 5일 추가 필요 [meeting:{meeting_id}@00:18:30].\n\n"
        "## 후속 액션\n"
        f"- [ ] SPEAKER_00: 캘린더 갱신 [meeting:{meeting_id}@00:25:12]\n\n"
        "## 참고 회의\n"
        f"- [{meeting_id} — 2026-04-28 미팅](../../../app/viewer/{meeting_id})\n\n"
        f"<!-- confidence: {confidence} -->"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. DecisionExtractor.extract() — LLM mock 기반 테스트 (5건)
# ══════════════════════════════════════════════════════════════════════════════


async def test_extract_명확한_결정_1건_반환():
    """Arrange: 출시일 결정 발화 + LLM 이 결정사항 1건 JSON 반환.
    Act: extract() 호출.
    Assert: ExtractedDecision 1건이 반환되고 title 이 포함된다.
    """
    # Arrange
    mock_llm = MockDecisionLLM(responses=[_build_single_decision_json()])
    extractor = DecisionExtractor(llm=mock_llm)
    utterances = _make_decision_utterances()

    # Act
    results = await extractor.extract(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 28),
        summary="출시일 회의 요약",
        utterances=utterances,
    )

    # Assert
    assert len(results) == 1
    assert isinstance(results[0], ExtractedDecision)
    assert "5월" in results[0].title or "출시" in results[0].title


async def test_extract_결정_없는_회의_빈_리스트_반환():
    """Arrange: 잡담만 있는 발화 + LLM 이 빈 배열 JSON 반환.
    Act: extract() 호출.
    Assert: 빈 리스트가 반환된다.
    """
    # Arrange
    mock_llm = MockDecisionLLM(responses=["[]"])
    extractor = DecisionExtractor(llm=mock_llm)
    utterances = _make_utterances()  # 기본 잡담 발화

    # Act
    results = await extractor.extract(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 28),
        summary="가벼운 근황 대화",
        utterances=utterances,
    )

    # Assert
    assert results == []


async def test_extract_여러_결정_3건_동시_반환():
    """Arrange: 결정 3개가 있는 회의 + LLM 이 3건 JSON 반환.
    Act: extract() 호출.
    Assert: ExtractedDecision 3건이 반환된다.
    """
    # Arrange
    three_decisions_json = (
        '['
        '{"title": "출시일 확정", "decision_text": "5/1 출시 [meeting:abc12345@00:10:00]",'
        '"background": "일정 논의 [meeting:abc12345@00:08:00]",'
        '"follow_ups": [], "participants": ["SPEAKER_00"], "projects": [], "confidence": 8},'
        '{"title": "예산 승인", "decision_text": "1억 예산 승인 [meeting:abc12345@00:20:00]",'
        '"background": "비용 보고 [meeting:abc12345@00:18:00]",'
        '"follow_ups": [], "participants": ["SPEAKER_01"], "projects": [], "confidence": 7},'
        '{"title": "팀장 변경", "decision_text": "영희 팀장 선임 [meeting:abc12345@00:30:00]",'
        '"background": "인사 논의 [meeting:abc12345@00:28:00]",'
        '"follow_ups": [], "participants": ["SPEAKER_00", "SPEAKER_01"], "projects": [], "confidence": 9}'
        ']'
    )
    mock_llm = MockDecisionLLM(responses=[three_decisions_json])
    extractor = DecisionExtractor(llm=mock_llm)
    utterances = _make_decision_utterances()

    # Act
    results = await extractor.extract(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 28),
        summary="다건 결정 회의 요약",
        utterances=utterances,
    )

    # Assert
    assert len(results) == 3
    assert all(isinstance(d, ExtractedDecision) for d in results)


async def test_extract_잘못된_json_응답_빈_리스트_반환():
    """Arrange: LLM 이 첫 번째로 깨진 JSON, 두 번째로 다시 깨진 JSON 반환 (1회 재시도 후 포기).
    Act: extract() 호출.
    Assert: 빈 리스트가 반환되고 LLM 은 최대 2회 호출된다 (원본 + 재시도).
    """
    # Arrange
    broken_json = "이건 JSON 이 아닙니다. {broken"
    # 재시도도 실패하는 시나리오
    mock_llm = MockDecisionLLM(responses=[broken_json, broken_json])
    extractor = DecisionExtractor(llm=mock_llm)
    utterances = _make_decision_utterances()

    # Act
    results = await extractor.extract(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 28),
        summary="테스트 요약",
        utterances=utterances,
    )

    # Assert — 보수적 정책: 파싱 실패 시 빈 리스트
    assert results == []
    # LLM 은 최대 2회 호출 (원본 1회 + 재시도 1회)
    assert mock_llm.call_count <= 2


async def test_extract_빈_utterances_llm_호출_없이_즉시_반환():
    """Arrange: utterances 가 빈 리스트.
    Act: extract() 호출.
    Assert: LLM 호출 없이 즉시 빈 리스트가 반환된다.
    """
    # Arrange
    mock_llm = MockDecisionLLM(responses=[])
    extractor = DecisionExtractor(llm=mock_llm)

    # Act
    results = await extractor.extract(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 28),
        summary="빈 회의",
        utterances=[],
    )

    # Assert
    assert results == []
    assert mock_llm.call_count == 0  # LLM 호출 0회


# ══════════════════════════════════════════════════════════════════════════════
# 2. ExtractedDecision dataclass 테스트 (2건)
# ══════════════════════════════════════════════════════════════════════════════


def test_extracted_decision_dataclass_필드_존재():
    """Arrange: ExtractedDecision 클래스 정의.
    Act: dataclasses.fields() 로 필드 목록 조회.
    Assert: PRD §3.1 에 명시된 9개 필드가 모두 존재한다.
    """
    # Arrange + Act
    field_names = {f.name for f in fields(ExtractedDecision)}

    # Assert — 인터페이스 정의 §3.1 의 필수 필드
    required_fields = {
        "title",
        "slug",
        "decision_text",
        "background",
        "follow_ups",
        "participants",
        "projects",
        "citations",
        "confidence",
    }
    assert required_fields.issubset(field_names), (
        f"누락된 필드: {required_fields - field_names}"
    )


async def test_extracted_decision_slug_filename_safe_문자열():
    """Arrange: 한국어 제목을 가진 결정사항 추출 요청.
    Act: extract() 후 slug 필드 확인.
    Assert: slug 가 filename-safe 문자만 포함한다 (영문 소문자, 숫자, 하이픈 또는 날짜 타임스탬프 기반).

    설계 근거: PRD §3.1 "slug 자동 생성 (한국어 → 영문 슬러그 + 날짜 prefix 는 render_pages 에서)"
    """
    # Arrange
    mock_llm = MockDecisionLLM(responses=[_build_single_decision_json()])
    extractor = DecisionExtractor(llm=mock_llm)
    utterances = _make_decision_utterances()

    # Act
    results = await extractor.extract(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 28),
        summary="출시일 회의",
        utterances=utterances,
    )

    # Assert — slug 는 반드시 비어있지 않아야 하고 filename-safe
    assert len(results) >= 1
    slug = results[0].slug
    assert slug, "slug 가 비어있어서는 안 됨"
    # filename-safe: 영문 소문자, 숫자, 하이픈, 밑줄만 허용 (경로 구분자 금지)
    assert re.match(r"^[a-z0-9\-_]+$", slug), (
        f"slug '{slug}' 이 filename-safe 하지 않음 (한국어나 공백 포함 금지)"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 3. render_pages() — PRD §4.2 템플릿 정확 준수 테스트 (5건)
# ══════════════════════════════════════════════════════════════════════════════


async def test_render_pages_frontmatter_6필드_포함():
    """Arrange: 결정사항 1건 + render_pages 용 LLM mock 응답 설정.
    Act: render_pages() 호출.
    Assert: 출력 페이지의 frontmatter 에 PRD §4.2 의 필수 6필드가 모두 포함된다.

    필수 frontmatter 필드: type, date, meeting_id, status, participants, projects.
    """
    # Arrange
    meeting_id = "abc12345"
    decision = ExtractedDecision(
        title="신규 온보딩 출시일을 5월 1일로 확정",
        slug="launch-date",
        decision_text=f"2026-05-01 출시 합의 [meeting:{meeting_id}@00:23:45].",
        background=f"QA 5일 추가 필요 [meeting:{meeting_id}@00:18:30].",
        participants=["SPEAKER_00", "SPEAKER_01"],
        projects=["new-onboarding"],
        confidence=9,
    )
    rendered_content = _build_rendered_page(meeting_id=meeting_id)
    mock_llm = MockDecisionLLM(responses=[rendered_content])
    extractor = DecisionExtractor(llm=mock_llm)
    store = MockWikiStore()

    # Act
    pages = await extractor.render_pages(
        decisions=[decision],
        meeting_id=meeting_id,
        meeting_date=date(2026, 4, 28),
        existing_store=store,
    )

    # Assert
    assert len(pages) == 1
    _, content = pages[0]
    # frontmatter 블록 추출 (--- 사이)
    fm_match = re.search(r"^---\n(.*?)\n---", content, re.DOTALL)
    assert fm_match, "frontmatter 블록(--- ... ---) 이 없음"
    fm_text = fm_match.group(1)
    for field_key in ("type:", "date:", "meeting_id:", "status:", "participants:", "projects:"):
        assert field_key in fm_text, f"frontmatter 에 '{field_key}' 필드가 없음"


async def test_render_pages_본문_4섹션_포함():
    """Arrange: 결정사항 1건 + 렌더링된 페이지 반환.
    Act: render_pages() 호출.
    Assert: 출력 페이지에 PRD §4.2 필수 4섹션이 모두 있다.

    필수 섹션: "## 결정 내용", "## 배경", "## 후속 액션", "## 참고 회의".
    """
    # Arrange
    meeting_id = "abc12345"
    decision = ExtractedDecision(
        title="신규 온보딩 출시일을 5월 1일로 확정",
        slug="launch-date",
        decision_text=f"출시 합의 [meeting:{meeting_id}@00:23:45].",
        background=f"QA 논의 [meeting:{meeting_id}@00:18:30].",
        confidence=9,
    )
    rendered_content = _build_rendered_page(meeting_id=meeting_id)
    mock_llm = MockDecisionLLM(responses=[rendered_content])
    extractor = DecisionExtractor(llm=mock_llm)
    store = MockWikiStore()

    # Act
    pages = await extractor.render_pages(
        decisions=[decision],
        meeting_id=meeting_id,
        meeting_date=date(2026, 4, 28),
        existing_store=store,
    )

    # Assert
    assert len(pages) == 1
    _, content = pages[0]
    for section in ("## 결정 내용", "## 배경", "## 후속 액션", "## 참고 회의"):
        assert section in content, f"필수 섹션 '{section}' 이 출력에 없음"


async def test_render_pages_인용_마커_포함():
    """Arrange: 결정 텍스트에 인용이 있는 ExtractedDecision + 렌더링 mock.
    Act: render_pages() 호출.
    Assert: 출력 페이지의 "## 결정 내용" 본문에 인용 마커 `[meeting:id@HH:MM:SS]` 가 있다.

    설계 근거: PRD §4.2 "모든 사실 진술에 인용 강제" + D1 인용 강제 통과 조건.
    """
    # Arrange
    meeting_id = "abc12345"
    decision = ExtractedDecision(
        title="신규 온보딩 출시일을 5월 1일로 확정",
        slug="launch-date",
        decision_text=f"2026-05-01 출시 합의 [meeting:{meeting_id}@00:23:45].",
        background=f"QA 논의 [meeting:{meeting_id}@00:18:30].",
        confidence=9,
    )
    rendered_content = _build_rendered_page(meeting_id=meeting_id)
    mock_llm = MockDecisionLLM(responses=[rendered_content])
    extractor = DecisionExtractor(llm=mock_llm)
    store = MockWikiStore()

    # Act
    pages = await extractor.render_pages(
        decisions=[decision],
        meeting_id=meeting_id,
        meeting_date=date(2026, 4, 28),
        existing_store=store,
    )

    # Assert
    assert len(pages) == 1
    _, content = pages[0]
    all_citations = CITATION_PATTERN.findall(content)
    assert len(all_citations) >= 1, (
        f"출력 페이지에 인용 마커 [meeting:id@HH:MM:SS] 가 없음. "
        f"발견된 인용: {all_citations}"
    )


async def test_render_pages_기존_페이지_존재_시_supersede_정책():
    """Arrange: same slug 의 페이지가 existing_store 에 이미 존재.
    Act: render_pages() 호출.
    Assert: 기존 페이지를 완전히 삭제하지 않고 "supersede" 정책을 적용한다.

    supersede 정책 검증: 기존 페이지의 created_at 이 신규 content 에도 보존되어야 한다.
    (또는 status=superseded 가 갱신된 내용에 반영되거나, 기존 내용이 취소선으로 보존.)

    설계 근거: 인터페이스 정의 §3.2 "기존 페이지가 있으면 frontmatter 보존" 정책.
    """
    # Arrange
    meeting_id = "abc12345"
    existing_rel_path = f"decisions/2026-04-28-launch-date.md"
    original_created_at = "2026-04-20T09:00:00+09:00"
    existing_content = (
        "---\n"
        "type: decision\n"
        "date: 2026-04-20\n"
        f"meeting_id: prev9999\n"
        "status: confirmed\n"
        "participants: [SPEAKER_00]\n"
        "projects: [new-onboarding]\n"
        "confidence: 7\n"
        f"created_at: {original_created_at}\n"
        "updated_at: 2026-04-20T09:00:00+09:00\n"
        "---\n\n"
        "# 신규 온보딩 출시일을 5월 1일로 확정\n\n"
        "## 결정 내용\n"
        "이전 결정 내용 [meeting:prev9999@00:10:00].\n\n"
        "## 배경\n이전 배경 [meeting:prev9999@00:08:00].\n\n"
        "## 후속 액션\n- [ ] 이전 액션 [meeting:prev9999@00:12:00]\n\n"
        "## 참고 회의\n- [prev9999](../../../app/viewer/prev9999)\n\n"
        "<!-- confidence: 7 -->"
    )
    # LLM 은 기존 created_at 을 보존한 갱신 페이지를 반환한다고 가정
    updated_content = (
        "---\n"
        "type: decision\n"
        "date: 2026-04-28\n"
        f"meeting_id: {meeting_id}\n"
        "status: confirmed\n"
        "participants: [SPEAKER_00, SPEAKER_01]\n"
        "projects: [new-onboarding]\n"
        "confidence: 9\n"
        f"created_at: {original_created_at}\n"  # 기존 created_at 보존
        "updated_at: 2026-04-28T10:00:00+09:00\n"
        "---\n\n"
        "# 신규 온보딩 출시일을 5월 1일로 확정\n\n"
        "## 결정 내용\n"
        f"2026-05-01 출시 합의 [meeting:{meeting_id}@00:23:45].\n\n"
        "## 배경\n이전 배경 보존 + 신규 배경 [meeting:{meeting_id}@00:18:30].\n\n"
        "## 후속 액션\n"
        f"- [ ] 캘린더 갱신 [meeting:{meeting_id}@00:25:12]\n\n"
        "## 참고 회의\n"
        f"- [{meeting_id}](../../../app/viewer/{meeting_id})\n\n"
        f"<!-- confidence: 9 -->"
    )
    decision = ExtractedDecision(
        title="신규 온보딩 출시일을 5월 1일로 확정",
        slug="launch-date",
        decision_text=f"출시 합의 [meeting:{meeting_id}@00:23:45].",
        background=f"QA 논의 [meeting:{meeting_id}@00:18:30].",
        confidence=9,
    )
    mock_llm = MockDecisionLLM(responses=[updated_content])
    extractor = DecisionExtractor(llm=mock_llm)
    store = MockWikiStore(existing_pages={existing_rel_path: existing_content})

    # Act
    pages = await extractor.render_pages(
        decisions=[decision],
        meeting_id=meeting_id,
        meeting_date=date(2026, 4, 28),
        existing_store=store,
    )

    # Assert — supersede 정책: 기존 created_at 이 신규 content 에도 보존
    assert len(pages) == 1
    _, content = pages[0]
    assert original_created_at in content, (
        f"기존 created_at({original_created_at}) 이 갱신 후 content 에서 사라졌음. "
        "supersede 정책 위반 (기존 frontmatter 보존 필요)"
    )


async def test_render_pages_confidence_마커_포함():
    """Arrange: confidence=9 인 결정사항 1건.
    Act: render_pages() 호출.
    Assert: 출력 페이지 마지막 줄에 `<!-- confidence: N -->` 형식의 마커가 있다.

    설계 근거: PRD §5.4 + GuardVerdict.confidence 추출을 위한 D3 의무.
    """
    # Arrange
    meeting_id = "abc12345"
    confidence = 9
    decision = ExtractedDecision(
        title="신규 온보딩 출시일을 5월 1일로 확정",
        slug="launch-date",
        decision_text=f"출시 합의 [meeting:{meeting_id}@00:23:45].",
        background=f"QA 논의 [meeting:{meeting_id}@00:18:30].",
        confidence=confidence,
    )
    rendered_content = _build_rendered_page(meeting_id=meeting_id, confidence=confidence)
    mock_llm = MockDecisionLLM(responses=[rendered_content])
    extractor = DecisionExtractor(llm=mock_llm)
    store = MockWikiStore()

    # Act
    pages = await extractor.render_pages(
        decisions=[decision],
        meeting_id=meeting_id,
        meeting_date=date(2026, 4, 28),
        existing_store=store,
    )

    # Assert
    assert len(pages) == 1
    _, content = pages[0]
    confidence_pattern = re.compile(r"<!--\s*confidence:\s*\d{1,2}\s*-->")
    assert confidence_pattern.search(content), (
        f"출력 페이지에 '<!-- confidence: N -->' 마커가 없음. "
        f"D3 검증을 위해 필수."
    )


# ══════════════════════════════════════════════════════════════════════════════
# 4. 한국어 고유명사 병기 금지 후처리 테스트 (1건+)
# ══════════════════════════════════════════════════════════════════════════════


async def test_render_pages_한국어_고유명사_외국어_병기_제거():
    """Arrange: LLM 이 "배미령(Baimilong)" 처럼 한국어에 외국어 병기를 포함해 반환.
    Act: render_pages() 호출.
    Assert: 최종 페이지에는 "배미령" 만 남고 "(Baimilong)" 은 제거된다.

    설계 근거: CLAUDE.md "Gemma 계열 주의사항" + PRD §5.4 한국어 고유명사 보호.
    EXAONE 사용으로 대부분 방지되지만, mock 응답에 병기가 있는 경우 후처리 보장.
    """
    # Arrange
    meeting_id = "abc12345"
    # LLM 이 고유명사 병기를 포함한 페이지를 반환하는 시나리오
    content_with_gloss = (
        "---\n"
        "type: decision\n"
        "date: 2026-04-28\n"
        f"meeting_id: {meeting_id}\n"
        "status: confirmed\n"
        "participants: [배미령]\n"
        "projects: [new-onboarding]\n"
        "confidence: 8\n"
        "created_at: 2026-04-28T10:00:00+09:00\n"
        "updated_at: 2026-04-28T10:00:00+09:00\n"
        "---\n\n"
        "# 출시 결정\n\n"
        "## 결정 내용\n"
        f"배미령(Baimilong) 이 5/1 출시를 확정했다 [meeting:{meeting_id}@00:23:45].\n\n"
        "## 배경\n"
        f"배미령(Baimilong) 이 일정을 제안했다 [meeting:{meeting_id}@00:18:30].\n\n"
        "## 후속 액션\n"
        f"- [ ] 배미령(Baimilong): 캘린더 갱신 [meeting:{meeting_id}@00:25:12]\n\n"
        "## 참고 회의\n"
        f"- [{meeting_id}](../../../app/viewer/{meeting_id})\n\n"
        "<!-- confidence: 8 -->"
    )
    decision = ExtractedDecision(
        title="출시 결정",
        slug="launch-decision",
        decision_text=f"배미령(Baimilong) 이 5/1 출시를 확정 [meeting:{meeting_id}@00:23:45].",
        background=f"배미령(Baimilong) 제안 [meeting:{meeting_id}@00:18:30].",
        confidence=8,
    )
    mock_llm = MockDecisionLLM(responses=[content_with_gloss])
    extractor = DecisionExtractor(llm=mock_llm)
    store = MockWikiStore()

    # Act
    pages = await extractor.render_pages(
        decisions=[decision],
        meeting_id=meeting_id,
        meeting_date=date(2026, 4, 28),
        existing_store=store,
    )

    # Assert — 외국어 병기 "(Baimilong)" 이 최종 출력에서 제거되어야 함
    assert len(pages) == 1
    _, content = pages[0]
    # 병기 패턴이 없어야 함
    glosses_found = _FOREIGN_GLOSS_PATTERN.findall(content)
    assert not glosses_found, (
        f"한국어 고유명사 외국어 병기가 최종 페이지에 남아 있음: {glosses_found}. "
        "후처리에서 제거되어야 함."
    )
    # 순수 한국어 이름은 남아 있어야 함
    assert "배미령" in content, "후처리 후 한국어 이름 '배미령' 이 사라지면 안 됨"


# ══════════════════════════════════════════════════════════════════════════════
# 4-2. Phase 2.E — Gemma 4 다국어 병기 패턴 강화 테스트
# ══════════════════════════════════════════════════════════════════════════════


def test_strip_paren_latin_한자_병기_제거():
    """Gemma 가 자주 만드는 한자 병기를 차단한다.

    예: "배미령(裵美玲)" → "배미령"

    배경: Gemma 4 는 다국어 모델이라 한국어 인명 옆에 한자(중국어 한자) 를
    병기하는 경향이 있다. PRD §5.4 한국어 고유명사 보호 정책에 따라 제거.
    """
    from core.wiki.extractors.decision import _strip_paren_latin

    text = "배미령(裵美玲) 이 안건을 발표했다."
    result = _strip_paren_latin(text)
    assert "裵美玲" not in result, "한자 병기가 제거되지 않았다"
    assert "배미령" in result, "한국어 이름이 사라졌다"


def test_strip_paren_latin_가타카나_병기_제거():
    """일본어 가타카나 병기도 차단한다.

    예: "배미령(ベミリョン)" → "배미령"
    """
    from core.wiki.extractors.decision import _strip_paren_latin

    text = "배미령(ベミリョン) 이 결정했다."
    result = _strip_paren_latin(text)
    assert "ベミリョン" not in result, "가타카나 병기가 제거되지 않았다"
    assert "배미령" in result


def test_strip_paren_latin_히라가나_병기_제거():
    """일본어 히라가나 병기도 차단한다.

    예: "배미령(ばいみりょん)" → "배미령"
    """
    from core.wiki.extractors.decision import _strip_paren_latin

    text = "배미령(ばいみりょん) 가 의견을 냈다."
    result = _strip_paren_latin(text)
    assert "ばいみりょん" not in result, "히라가나 병기가 제거되지 않았다"
    assert "배미령" in result


def test_strip_paren_latin_한국어_별칭은_보존():
    """한국어 변형(별칭) 은 정당한 표기로 보존되어야 한다.

    예: "배미령(백미령)" 은 별칭 표기이므로 그대로 둔다.
    Gemma 의 외국어 병기와 사용자가 직접 작성한 별칭을 구분하는 것이 중요.
    """
    from core.wiki.extractors.decision import _strip_paren_latin

    text = "배미령(백미령) 이 발표했다."
    result = _strip_paren_latin(text)
    assert "(백미령)" in result, "한국어 별칭이 잘못 제거되었다"


def test_strip_paren_latin_다단어_영어_병기_제거():
    """다단어 영어 병기(공백/하이픈 포함) 도 차단한다.

    예: "배미령(Bea Mi-ryeong)" → "배미령"
    """
    from core.wiki.extractors.decision import _strip_paren_latin

    text = "배미령(Bea Mi-ryeong) 이 검토했다."
    result = _strip_paren_latin(text)
    assert "Bea" not in result and "Mi-ryeong" not in result, (
        f"다단어 영어 병기가 제거되지 않았다: {result}"
    )
    assert "배미령" in result


def test_strip_paren_latin_공백있는_병기_제거():
    """한국어와 괄호 사이 공백이 있어도 병기를 차단한다.

    예: "배미령 (Baimilong)" → "배미령"
    """
    from core.wiki.extractors.decision import _strip_paren_latin

    text = "배미령 (Baimilong) 이 발표했다."
    result = _strip_paren_latin(text)
    assert "Baimilong" not in result
    assert "배미령" in result


def test_strip_paren_latin_영영_병기는_보존():
    """영어 약어의 영어 풀이(영영 병기) 는 정당한 설명이므로 보존한다.

    예: "API(Application Programming Interface)" 는 그대로 둔다.
    한국어 앞에 있을 때만 외국어 병기로 간주.
    """
    from core.wiki.extractors.decision import _strip_paren_latin

    text = "API(Application Programming Interface) 를 사용했다."
    result = _strip_paren_latin(text)
    assert "(Application Programming Interface)" in result, (
        f"영영 병기가 잘못 제거되었다: {result}"
    )
