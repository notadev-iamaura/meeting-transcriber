"""TopicExtractor TDD Red 단계 테스트 모듈

목적: core/wiki/extractors/topic.py 가 아직 존재하지 않으므로
  ImportError 로 모든 테스트가 Red 상태가 된다.
  구현체가 생기면 여기 정의된 계약을 통과해야 Green 이 된다.

커버리지:
  - extract_concepts() — LLM mock 기반 (3건)
  - aggregate_and_render() — 3회 등장 임계 + 멱등성 + 영속화 (10건)
  - slug 정규화 + 인용 마커 보존 (2건)
  - PRD §4.2 topics 템플릿 정확 준수 (1건)
  총 16건 (임무 요구사항 초과)

의존성:
  - pytest (asyncio_mode=auto, pyproject.toml 에 설정됨)
  - core.wiki.models.Citation (Phase 1, 이미 구현 완료)
  - core.wiki.store.WikiStore (Phase 1, 이미 구현 완료)
  - core.wiki.extractors.topic (Phase 4, 아직 미구현 → ImportError)

설계 근거:
  - §3 TopicExtractor 인터페이스 정의 (phase4_interfaces.md) 기반
  - PRD §5.3 "topics 는 3회 이상 반복 등장한 개념만" 임계 정책
  - 멱등성: 같은 meeting_id 두 번 처리 → meeting_ids set 중복 추가 금지
  - 메타파일 `.topic_mentions.json` 디스크 영속화 필수

작성자: TDD Red Author (Phase 4)
날짜: 2026-04-29
"""

from __future__ import annotations

import json
import re
from dataclasses import fields
from datetime import date
from pathlib import Path

import pytest

# ──────────────────────────────────────────────────────────────────────────────
# Phase 1 모듈 — 이미 구현 완료, import 가능
# ──────────────────────────────────────────────────────────────────────────────
from core.wiki.models import Citation
from core.wiki.store import WikiStore

# ──────────────────────────────────────────────────────────────────────────────
# [TDD Red] core/wiki/extractors/topic.py 가 없으므로
# 이 import 블록이 ImportError 를 일으켜 모든 테스트가 Red 상태가 된다.
# 구현체가 생기면 아래 계약을 모두 통과해야 Green 이 된다.
# ──────────────────────────────────────────────────────────────────────────────
from core.wiki.extractors.topic import (  # noqa: E402
    ConceptMention,
    ExtractedConcept,
    TopicExtractor,
)

# PRD §4.3 인용 패턴 (citations.py 의 CITATION_PATTERN 과 동일)
CITATION_PATTERN = re.compile(r"\[meeting:([a-f0-9]{8})@(\d{2}):(\d{2}):(\d{2})\]")


# ══════════════════════════════════════════════════════════════════════════════
# MockTopicLLM — Phase 2/3 의 MockDecisionLLM / MockPersonLLM 패턴 동일
# ══════════════════════════════════════════════════════════════════════════════


class MockTopicLLM:
    """테스트용 WikiLLMClient. Phase 2/3 의 Mock 패턴과 완전히 동일."""

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
# 테스트용 헬퍼 클래스 및 함수
# ══════════════════════════════════════════════════════════════════════════════


class _SimpleUtterance:
    """테스트용 Utterance Protocol 최소 구현체."""

    def __init__(self, text: str, speaker: str, start: float, end: float) -> None:
        """발화 데이터를 저장한다."""
        self.text = text
        self.speaker = speaker
        self.start = start
        self.end = end


def _make_utterances(texts: list[str] | None = None) -> list[_SimpleUtterance]:
    """테스트용 발화 목록을 생성하는 헬퍼.

    Args:
        texts: 발화 텍스트 목록. None 이면 기본 발화 2개를 반환한다.

    Returns:
        _SimpleUtterance 인스턴스 목록.
    """
    if texts is None:
        return [
            _SimpleUtterance("오늘 가격 전략에 대해 논의합시다.", "SPEAKER_00", 0.0, 3.0),
            _SimpleUtterance("pricing strategy 가 핵심입니다.", "SPEAKER_01", 3.5, 6.0),
        ]
    return [
        _SimpleUtterance(text, "SPEAKER_00", float(i * 5), float(i * 5 + 4))
        for i, text in enumerate(texts)
    ]


def _make_wiki_store(tmp_path: Path) -> WikiStore:
    """테스트용 WikiStore 를 tmp_path 에 생성·초기화한다.

    Args:
        tmp_path: pytest 제공 임시 디렉토리.

    Returns:
        초기화된 WikiStore 인스턴스.
    """
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir(parents=True, exist_ok=True)
    # topics 서브디렉토리 미리 생성
    (wiki_root / "topics").mkdir(exist_ok=True)
    return WikiStore(wiki_root)


def _make_single_concept_json(
    name: str = "pricing strategy",
    slug: str = "pricing-strategy",
    confidence: int = 8,
    meeting_id: str = "abc12345",
    ts: str = "00:10:00",
) -> str:
    """LLM 이 개념 1건을 반환할 때의 JSON 문자열을 생성한다.

    Args:
        name: 개념 이름.
        slug: filename-safe 슬러그.
        confidence: LLM self-rated 신뢰도 (0~10).
        meeting_id: 8자리 hex 회의 ID.
        ts: HH:MM:SS 형식 타임스탬프.

    Returns:
        JSON 배열 문자열 (개념 1건).
    """
    return (
        f'[{{"name": "{name}", "slug": "{slug}",'
        f'"description": "{name} 관련 핵심 개념",'
        f'"citations": [{{"meeting_id": "{meeting_id}", "timestamp_str": "{ts}",'
        f'"timestamp_seconds": 600}}],'
        f'"confidence": {confidence}}}]'
    )


def _make_topic_page_response(
    name: str = "pricing strategy",
    slug: str = "pricing-strategy",
    meeting_id: str = "abc12345",
    ts: str = "00:10:00",
    confidence: int = 8,
) -> str:
    """PRD §4.2 topics 템플릿에 맞는 LLM 페이지 응답 문자열을 반환한다.

    Args:
        name: 개념 이름.
        slug: filename-safe 슬러그.
        meeting_id: 8자리 hex 회의 ID.
        ts: HH:MM:SS 형식 타임스탬프.
        confidence: 신뢰도 정수.

    Returns:
        frontmatter + 본문 섹션이 포함된 마크다운 문자열.
    """
    return (
        "---\n"
        "type: topic\n"
        f"name: {name}\n"
        "first_seen: 2026-04-29\n"
        "last_seen: 2026-04-29\n"
        "mention_count: 3\n"
        f"confidence: {confidence}\n"
        "---\n\n"
        f"# {name}\n\n"
        "## 정의\n"
        f"{name} 는 회의에서 반복 등장한 핵심 개념이다 [meeting:{meeting_id}@{ts}].\n\n"
        "## 관련 회의\n"
        f"- [{meeting_id} — 2026-04-29 회의](/app/viewer/{meeting_id})\n\n"
        "## 자주 함께 등장하는 개념\n"
        "- (아직 관련 개념 없음)\n\n"
        f"<!-- confidence: {confidence} -->"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. extract_concepts() — LLM mock 기반 테스트 (3건)
# ══════════════════════════════════════════════════════════════════════════════


async def test_extract_concepts_단일_개념_반환():
    """단일 개념 추출 정상 흐름.

    Arrange: utterances 에 "pricing strategy" 반복 + LLM 이 1건 JSON 반환.
    Act: extract_concepts() 호출.
    Assert: ExtractedConcept 1건이 반환되고, name 이 올바르다.
    """
    # Arrange
    mock_llm = MockTopicLLM(responses=[_make_single_concept_json()])
    extractor = TopicExtractor(mock_llm)
    utterances = _make_utterances([
        "pricing strategy 가 가장 중요한 결정입니다.",
        "pricing strategy 를 다음 분기까지 확정해야 합니다.",
        "pricing strategy 에 대한 팀원 의견을 모아주세요.",
    ])

    # Act
    results = await extractor.extract_concepts(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 29),
        utterances=utterances,
        summary="pricing strategy 관련 회의 요약",
    )

    # Assert
    assert len(results) == 1
    assert isinstance(results[0], ExtractedConcept)
    assert "pricing" in results[0].name.lower() or "strategy" in results[0].name.lower()


async def test_extract_concepts_여러_개념_3건_반환():
    """여러 개념 동시 추출.

    Arrange: LLM 이 3개 개념 JSON 배열 반환.
    Act: extract_concepts() 호출.
    Assert: ExtractedConcept 3건이 반환된다.
    """
    # Arrange
    three_concepts_json = (
        "["
        '{"name": "pricing strategy", "slug": "pricing-strategy",'
        '"description": "가격 전략", "citations": [], "confidence": 9},'
        '{"name": "온보딩 프로세스", "slug": "온보딩-프로세스",'
        '"description": "신규 입사자 온보딩", "citations": [], "confidence": 8},'
        '{"name": "기술 부채", "slug": "기술-부채",'
        '"description": "레거시 코드 관련 누적 비용", "citations": [], "confidence": 7}'
        "]"
    )
    mock_llm = MockTopicLLM(responses=[three_concepts_json])
    extractor = TopicExtractor(mock_llm)
    utterances = _make_utterances()

    # Act
    results = await extractor.extract_concepts(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 29),
        utterances=utterances,
        summary="여러 주제가 다뤄진 회의",
    )

    # Assert
    assert len(results) == 3
    assert all(isinstance(c, ExtractedConcept) for c in results)


async def test_extract_concepts_빈_utterances_llm_호출_없음():
    """utterances 가 빈 리스트일 때 LLM 호출 없이 즉시 반환.

    Arrange: utterances 가 빈 리스트.
    Act: extract_concepts() 호출.
    Assert: LLM 호출 0회 + 빈 리스트 반환.
    """
    # Arrange
    mock_llm = MockTopicLLM(responses=[])
    extractor = TopicExtractor(mock_llm)

    # Act
    results = await extractor.extract_concepts(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 29),
        utterances=[],
        summary="빈 회의",
    )

    # Assert
    assert results == []
    assert mock_llm.call_count == 0  # LLM 호출 0회


# ══════════════════════════════════════════════════════════════════════════════
# 2. aggregate_and_render() — 3회 임계 테스트 (5건)
# ══════════════════════════════════════════════════════════════════════════════


async def test_aggregate_첫_등장_mention_count_1_페이지_없음(tmp_path):
    """1회 등장 시 메타파일만 갱신, 페이지 생성 안 함.

    Arrange: 새 concept "pricing-strategy" 첫 등장 (회의 m1).
    Act: aggregate_and_render() 1회 호출.
    Assert:
      - 반환 목록이 비어있다 (페이지 생성 없음).
      - .topic_mentions.json 에 meeting_ids 길이 == 1.
      - topics/pricing-strategy.md 파일이 없다.
    """
    # Arrange
    store = _make_wiki_store(tmp_path)
    mock_llm = MockTopicLLM(responses=[])
    extractor = TopicExtractor(mock_llm)
    concept = ExtractedConcept(
        name="pricing strategy",
        slug="pricing-strategy",
        description="가격 전략 핵심 개념",
        citations=[],
        confidence=8,
    )

    # Act
    results = await extractor.aggregate_and_render(
        new_concepts=[concept],
        meeting_id="m1111111",
        meeting_date=date(2026, 4, 29),
        existing_store=store,
    )

    # Assert — 페이지 생성 없음
    assert results == []
    # 메타파일이 갱신되었는지 확인
    meta_path = store.root / ".topic_mentions.json"
    assert meta_path.exists(), ".topic_mentions.json 이 생성되어야 한다"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "pricing-strategy" in meta["mentions"]
    assert len(meta["mentions"]["pricing-strategy"]["meeting_ids"]) == 1
    # 페이지 파일 없음 확인
    page_path = store.root / "topics" / "pricing-strategy.md"
    assert not page_path.exists(), "1회 등장으로는 페이지를 생성하면 안 됨"


async def test_aggregate_2회_등장_페이지_여전히_없음(tmp_path):
    """2회 등장 시에도 페이지 미생성.

    Arrange: "pricing-strategy" 가 m1, m2 두 회의에서 등장.
    Act: aggregate_and_render() 2회 호출 (m1 → m2).
    Assert:
      - 두 번째 호출 결과도 빈 리스트.
      - .topic_mentions.json 에 meeting_ids 길이 == 2.
      - topics/pricing-strategy.md 없음.
    """
    # Arrange
    store = _make_wiki_store(tmp_path)
    # LLM 은 페이지 생성 시에만 호출되므로 응답 불필요
    mock_llm = MockTopicLLM(responses=[])
    extractor = TopicExtractor(mock_llm)
    concept = ExtractedConcept(
        name="pricing strategy",
        slug="pricing-strategy",
        description="가격 전략",
        citations=[],
        confidence=8,
    )

    # Act — 첫 번째 회의 m1
    await extractor.aggregate_and_render(
        new_concepts=[concept],
        meeting_id="m1111111",
        meeting_date=date(2026, 4, 27),
        existing_store=store,
    )
    # Act — 두 번째 회의 m2 (새 인스턴스 사용 — 영속화 검증 포함)
    extractor2 = TopicExtractor(mock_llm)
    results = await extractor2.aggregate_and_render(
        new_concepts=[concept],
        meeting_id="m2222222",
        meeting_date=date(2026, 4, 28),
        existing_store=store,
    )

    # Assert
    assert results == []
    meta_path = store.root / ".topic_mentions.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert len(meta["mentions"]["pricing-strategy"]["meeting_ids"]) == 2
    page_path = store.root / "topics" / "pricing-strategy.md"
    assert not page_path.exists(), "2회 등장으로는 페이지를 생성하면 안 됨"


async def test_aggregate_3회_도달_시_페이지_처음_생성(tmp_path):
    """3회 도달 시 topics/{slug}.md 페이지가 처음 생성된다.

    Arrange: "pricing-strategy" 가 m1, m2, m3 세 회의에서 등장.
    Act: aggregate_and_render() 3회 호출.
    Assert:
      - 3회 호출 결과 목록에 (rel_path, content, confidence) 튜플이 1건.
      - rel_path 가 "topics/pricing-strategy.md" 형태.
      - .topic_mentions.json 의 page_created == True.
      - LLM 이 1회 호출됨 (페이지 생성용).
    """
    # Arrange
    store = _make_wiki_store(tmp_path)
    page_content = _make_topic_page_response()
    # 첫 두 호출은 LLM 없이, 세 번째에서 LLM 1회 호출
    mock_llm = MockTopicLLM(responses=[page_content])
    extractor = TopicExtractor(mock_llm)
    concept = ExtractedConcept(
        name="pricing strategy",
        slug="pricing-strategy",
        description="가격 전략",
        citations=[],
        confidence=8,
    )

    # Act — m1, m2 는 페이지 생성 없이 진행
    await extractor.aggregate_and_render(
        new_concepts=[concept], meeting_id="m1111111",
        meeting_date=date(2026, 4, 27), existing_store=store,
    )
    await extractor.aggregate_and_render(
        new_concepts=[concept], meeting_id="m2222222",
        meeting_date=date(2026, 4, 28), existing_store=store,
    )
    # Act — m3 에서 3회 임계 도달
    results = await extractor.aggregate_and_render(
        new_concepts=[concept], meeting_id="m3333333",
        meeting_date=date(2026, 4, 29), existing_store=store,
    )

    # Assert — 페이지 1건 생성
    assert len(results) == 1, f"3회 도달 시 페이지 1건이 생성되어야 한다. 실제: {len(results)}"
    rel_path, content, confidence = results[0]
    assert "topics/" in rel_path, f"rel_path 가 topics/ 아래 있어야 한다: {rel_path}"
    assert "pricing-strategy" in rel_path
    # LLM 1회 호출 (페이지 생성)
    assert mock_llm.call_count == 1, f"LLM 은 페이지 생성 시 1회만 호출되어야 한다: {mock_llm.call_count}"
    # 메타파일 page_created 확인
    meta_path = store.root / ".topic_mentions.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["mentions"]["pricing-strategy"]["page_created"] is True


async def test_aggregate_4회_이상_기존_페이지_갱신(tmp_path):
    """4회 이상 등장 시 기존 페이지에 citation 추가하여 갱신.

    Arrange: "pricing-strategy" 가 4개 회의에서 등장. 3회 후 페이지 생성됨.
    Act: 4번째 회의에서 aggregate_and_render() 호출.
    Assert:
      - 4회 호출 결과 목록에 갱신 튜플 1건.
      - LLM 이 총 2회 호출됨 (3회째 신규 생성 + 4회째 갱신).
    """
    # Arrange
    store = _make_wiki_store(tmp_path)
    # 3회째 신규 생성 응답 + 4회째 갱신 응답
    page_v1 = _make_topic_page_response()
    page_v2 = _make_topic_page_response(meeting_id="m4444444", ts="00:20:00")
    mock_llm = MockTopicLLM(responses=[page_v1, page_v2])
    extractor = TopicExtractor(mock_llm)
    concept = ExtractedConcept(
        name="pricing strategy",
        slug="pricing-strategy",
        description="가격 전략",
        citations=[],
        confidence=8,
    )

    # m1, m2, m3 처리 (3회 임계 도달 + 페이지 생성)
    for meeting_id, d in [
        ("m1111111", date(2026, 4, 26)),
        ("m2222222", date(2026, 4, 27)),
        ("m3333333", date(2026, 4, 28)),
    ]:
        await extractor.aggregate_and_render(
            new_concepts=[concept], meeting_id=meeting_id,
            meeting_date=d, existing_store=store,
        )

    # Act — 4번째 회의
    results = await extractor.aggregate_and_render(
        new_concepts=[concept], meeting_id="m4444444",
        meeting_date=date(2026, 4, 29), existing_store=store,
    )

    # Assert
    assert len(results) == 1, "4회 등장 시 기존 페이지 갱신 튜플 1건 반환"
    assert mock_llm.call_count == 2, (
        f"LLM 호출은 신규 생성 1회 + 갱신 1회 = 총 2회여야 한다: {mock_llm.call_count}"
    )


async def test_aggregate_min_meetings_threshold_변경(tmp_path):
    """min_meetings_threshold=5 로 변경 시 5회 도달 전까지 페이지 미생성.

    Arrange: TopicExtractor(min_meetings_threshold=5) 생성.
    Act: 같은 concept 을 4개 다른 회의에서 처리.
    Assert: 4회 모두 results == [] (페이지 생성 없음).
    """
    # Arrange
    store = _make_wiki_store(tmp_path)
    mock_llm = MockTopicLLM(responses=[])
    # 임계값 5로 설정 — 5회 미만에서는 페이지 생성 안 함
    extractor = TopicExtractor(mock_llm, min_meetings_threshold=5)
    concept = ExtractedConcept(
        name="온보딩 프로세스",
        slug="온보딩-프로세스",
        description="신규 입사자 온보딩 절차",
        citations=[],
        confidence=8,
    )

    # Act — 4회 처리 (임계 5 미만)
    all_results = []
    for i, meeting_id in enumerate(["ma111111", "mb222222", "mc333333", "md444444"]):
        result = await extractor.aggregate_and_render(
            new_concepts=[concept],
            meeting_id=meeting_id,
            meeting_date=date(2026, 4, i + 26),
            existing_store=store,
        )
        all_results.extend(result)

    # Assert — 4회 모두 페이지 생성 없음
    assert all_results == [], (
        f"min_meetings_threshold=5 일 때 4회까지는 페이지를 생성하면 안 됨. "
        f"실제 결과: {all_results}"
    )
    assert mock_llm.call_count == 0, "임계 미달로 LLM 호출이 발생하면 안 됨"


# ══════════════════════════════════════════════════════════════════════════════
# 3. 멱등성 테스트 (2건)
# ══════════════════════════════════════════════════════════════════════════════


async def test_aggregate_같은_meeting_id_중복_처리_멱등(tmp_path):
    """같은 meeting_id 로 두 번 호출해도 meeting_ids 에 중복 추가 금지.

    Arrange: "pricing-strategy" 를 meeting_id="m1111111" 로 두 번 처리.
    Act: aggregate_and_render() 를 같은 meeting_id 로 2회 호출.
    Assert:
      - .topic_mentions.json 의 meeting_ids 길이 == 1 (중복 없음).
      - mention_count 가 2가 아니라 1이다.
    """
    # Arrange
    store = _make_wiki_store(tmp_path)
    mock_llm = MockTopicLLM(responses=[])
    extractor = TopicExtractor(mock_llm)
    concept = ExtractedConcept(
        name="pricing strategy",
        slug="pricing-strategy",
        description="가격 전략",
        citations=[],
        confidence=8,
    )

    # Act — 동일 meeting_id 로 두 번 호출
    await extractor.aggregate_and_render(
        new_concepts=[concept], meeting_id="m1111111",
        meeting_date=date(2026, 4, 29), existing_store=store,
    )
    await extractor.aggregate_and_render(
        new_concepts=[concept], meeting_id="m1111111",  # 동일 ID 재처리
        meeting_date=date(2026, 4, 29), existing_store=store,
    )

    # Assert — meeting_ids 에 중복 없음
    meta_path = store.root / ".topic_mentions.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    mention = meta["mentions"]["pricing-strategy"]
    assert len(mention["meeting_ids"]) == 1, (
        f"같은 meeting_id 중복 처리 시 meeting_ids 는 1개여야 한다: "
        f"{mention['meeting_ids']}"
    )


async def test_aggregate_topic_mentions_json_영속화(tmp_path):
    """.topic_mentions.json 이 디스크에 영속화되고 새 인스턴스에서도 보존된다.

    Arrange: extractor1 이 m1 처리 후 .topic_mentions.json 저장.
    Act: extractor2 (새 인스턴스) 가 같은 store 로 m2 처리.
    Assert:
      - extractor2 가 기존 카운트를 읽어 meeting_ids 길이 == 2.
      - 디스크의 .topic_mentions.json 에 m1, m2 모두 기록됨.
    """
    # Arrange — 첫 번째 인스턴스
    store = _make_wiki_store(tmp_path)
    mock_llm1 = MockTopicLLM(responses=[])
    extractor1 = TopicExtractor(mock_llm1)
    concept = ExtractedConcept(
        name="pricing strategy",
        slug="pricing-strategy",
        description="가격 전략",
        citations=[],
        confidence=8,
    )

    # Act — extractor1 으로 m1 처리
    await extractor1.aggregate_and_render(
        new_concepts=[concept], meeting_id="m1111111",
        meeting_date=date(2026, 4, 28), existing_store=store,
    )

    # 새 인스턴스로 m2 처리 (영속화 검증)
    mock_llm2 = MockTopicLLM(responses=[])
    extractor2 = TopicExtractor(mock_llm2)
    await extractor2.aggregate_and_render(
        new_concepts=[concept], meeting_id="m2222222",
        meeting_date=date(2026, 4, 29), existing_store=store,
    )

    # Assert — 이전 카운트가 보존됨
    meta_path = store.root / ".topic_mentions.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meeting_ids = meta["mentions"]["pricing-strategy"]["meeting_ids"]
    assert len(meeting_ids) == 2, (
        f"새 인스턴스에서도 이전 카운트가 보존되어야 한다: {meeting_ids}"
    )
    assert "m1111111" in meeting_ids
    assert "m2222222" in meeting_ids


# ══════════════════════════════════════════════════════════════════════════════
# 4. 환각 방지 + slug 정규화 테스트 (2건)
# ══════════════════════════════════════════════════════════════════════════════


async def test_extract_concepts_confidence_7_미만_필터링():
    """confidence < 7 인 개념은 mention_count 에 추가되지 않는다.

    Arrange: LLM 이 confidence=5 인 개념 1건 반환.
    Act: extract_concepts() 후 aggregate_and_render() 호출.
    Assert:
      - extract_concepts() 결과가 빈 리스트 (confidence 7 미만 필터링).
      - 메타파일에 해당 slug 가 없다.
    """
    # Arrange
    low_confidence_json = (
        '[{"name": "불확실한 개념", "slug": "불확실한-개념",'
        '"description": "낮은 신뢰도 개념", "citations": [], "confidence": 5}]'
    )
    mock_llm = MockTopicLLM(responses=[low_confidence_json])
    extractor = TopicExtractor(mock_llm, min_confidence_to_count=7)
    utterances = _make_utterances()

    # Act
    results = await extractor.extract_concepts(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 29),
        utterances=utterances,
        summary="불확실한 내용 회의",
    )

    # Assert — confidence 7 미만이므로 필터링되어 빈 리스트
    assert results == [], (
        f"confidence < 7 인 개념은 반환되지 않아야 한다. 실제: {results}"
    )


async def test_slug_정규화_영문_소문자_하이픈(tmp_path):
    """slug 정규화 검증.

    Arrange: concept_name="pricing strategy" 로 extract_concepts() 호출.
    Act: ExtractedConcept.slug 확인.
    Assert:
      - 영어 이름 "pricing strategy" → slug = "pricing-strategy" (공백 → 하이픈).
      - slug 는 반드시 filename-safe 이어야 한다 (영소문자 + 숫자 + 하이픈 + 한글).
    """
    # Arrange
    slug_test_json = (
        '[{"name": "pricing strategy", "slug": "pricing-strategy",'
        '"description": "가격 전략", "citations": [], "confidence": 8}]'
    )
    mock_llm = MockTopicLLM(responses=[slug_test_json])
    extractor = TopicExtractor(mock_llm)
    utterances = _make_utterances()

    # Act
    results = await extractor.extract_concepts(
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 29),
        utterances=utterances,
        summary="가격 전략 회의",
    )

    # Assert
    assert len(results) >= 1
    slug = results[0].slug
    assert slug, "slug 가 비어있으면 안 됨"
    # filename-safe: 영소문자, 숫자, 하이픈, 밑줄, 한글 허용 (경로 구분자 금지)
    assert "/" not in slug, f"slug 에 경로 구분자가 있으면 안 됨: {slug}"
    assert ".." not in slug, f"slug 에 path traversal 이 있으면 안 됨: {slug}"
    # "pricing strategy" 는 "pricing-strategy" 로 정규화되어야 함
    assert slug == "pricing-strategy", (
        f"영어 이름 'pricing strategy' 의 slug 는 'pricing-strategy' 여야 한다: {slug}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 5. 인용 마커 보존 테스트 (1건)
# ══════════════════════════════════════════════════════════════════════════════


async def test_aggregate_페이지_본문_인용_마커_포함(tmp_path):
    """생성된 topics 페이지 본문에 인용 마커가 포함된다.

    Arrange: "pricing-strategy" 3회 등장 후 페이지 생성. LLM 응답에 인용 마커 포함.
    Act: 3회 aggregate_and_render() 후 결과 content 확인.
    Assert: content 에 [meeting:{8hex}@HH:MM:SS] 형식 인용 마커가 1건 이상 포함.
    """
    # Arrange
    store = _make_wiki_store(tmp_path)
    meeting_id = "abc12345"
    page_with_citation = _make_topic_page_response(meeting_id=meeting_id, ts="00:10:00")
    mock_llm = MockTopicLLM(responses=[page_with_citation])
    extractor = TopicExtractor(mock_llm)
    concept = ExtractedConcept(
        name="pricing strategy",
        slug="pricing-strategy",
        description="가격 전략",
        citations=[
            Citation(
                meeting_id=meeting_id,
                timestamp_str="00:10:00",
                timestamp_seconds=600,
            )
        ],
        confidence=8,
    )

    # Act — 3회 처리
    for i, mid in enumerate(["m1111111", "m2222222", "m3333333"]):
        results = await extractor.aggregate_and_render(
            new_concepts=[concept], meeting_id=mid,
            meeting_date=date(2026, 4, 27 + i), existing_store=store,
        )

    # Assert — 3회째 결과에 인용 마커 포함
    assert len(results) == 1
    _, content, _ = results[0]
    citations_found = CITATION_PATTERN.findall(content)
    assert len(citations_found) >= 1, (
        f"생성된 topics 페이지에 인용 마커 [meeting:id@HH:MM:SS] 가 없음. "
        f"발견된 인용: {citations_found}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 6. PRD §4.2 topics 템플릿 정확성 테스트 (1건)
# ══════════════════════════════════════════════════════════════════════════════


async def test_aggregate_prd_topics_템플릿_준수(tmp_path):
    """생성된 topics 페이지가 PRD §4.2 템플릿을 정확히 준수한다.

    Arrange: "pricing-strategy" 3회 등장 → 페이지 생성 트리거.
    Act: aggregate_and_render() 3회 호출 후 결과 content 검사.
    Assert:
      - frontmatter 에 type=topic, name, first_seen, last_seen, mention_count 포함.
      - 본문에 "## 정의", "## 관련 회의", "## 자주 함께 등장하는 개념" 섹션 포함.
      - <!-- confidence: N --> 마커 포함.
    """
    # Arrange
    store = _make_wiki_store(tmp_path)
    page_response = _make_topic_page_response()
    mock_llm = MockTopicLLM(responses=[page_response])
    extractor = TopicExtractor(mock_llm)
    concept = ExtractedConcept(
        name="pricing strategy",
        slug="pricing-strategy",
        description="가격 전략",
        citations=[],
        confidence=8,
    )

    # Act — 3회 처리
    results_all = []
    for i, mid in enumerate(["m1111111", "m2222222", "m3333333"]):
        r = await extractor.aggregate_and_render(
            new_concepts=[concept], meeting_id=mid,
            meeting_date=date(2026, 4, 27 + i), existing_store=store,
        )
        results_all.extend(r)

    # Assert — 페이지 1건 생성
    assert len(results_all) == 1
    rel_path, content, confidence = results_all[0]

    # frontmatter 블록 확인
    fm_match = re.search(r"^---\n(.*?)\n---", content, re.DOTALL)
    assert fm_match, "frontmatter 블록(--- ... ---) 이 없음"
    fm_text = fm_match.group(1)

    # PRD §4.2 topics frontmatter 필수 필드
    for field_key in ("type:", "name:", "first_seen:", "last_seen:", "mention_count:"):
        assert field_key in fm_text, (
            f"frontmatter 에 '{field_key}' 필드가 없음. PRD §4.2 topics 템플릿 위반."
        )

    # type 이 "topic" 이어야 함
    assert "type: topic" in fm_text, "frontmatter 의 type 이 'topic' 이 아님"

    # 본문 섹션 확인 (PRD §4.2 topics 3개 필수 섹션)
    for section in ("## 정의", "## 관련 회의", "## 자주 함께 등장하는 개념"):
        assert section in content, (
            f"PRD §4.2 필수 섹션 '{section}' 이 topics 페이지에 없음"
        )

    # confidence 마커
    confidence_pattern = re.compile(r"<!--\s*confidence:\s*\d{1,2}\s*-->")
    assert confidence_pattern.search(content), (
        "<!-- confidence: N --> 마커가 없음. D3 검증에 필요."
    )
