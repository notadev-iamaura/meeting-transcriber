"""DecisionExtractor.render_pages 병렬화 + 페이지 갱신 상한 8개 TDD 테스트

목적: Phase 2.E Reviewer 위임 사항 검증.
    1. render_pages 가 asyncio.gather 로 병렬 실행 — 직렬 시간 대비 단축
    2. confidence 내림차순 정렬 후 상위 8개로 제한 (decisions[:8])
    3. 9건 이상 입력 시 정확히 8건만 처리

검증 전략:
    - LLM mock 의 generate() 가 인위적 sleep 을 가져 직렬 vs 병렬 시간 차이 측정
    - 호출 인자 (call_count, last_titles) 로 어느 decision 이 처리되었는지 확인
"""

from __future__ import annotations

import asyncio
import time
from datetime import date
from typing import Any

import pytest

from core.wiki.extractors.decision import DecisionExtractor, ExtractedDecision

# ─────────────────────────────────────────────────────────────────────────
# Mock 헬퍼
# ─────────────────────────────────────────────────────────────────────────


class _SlowMockLLM:
    """generate() 마다 일정 시간 sleep 후 응답 반환 — 병렬화 검증용."""

    def __init__(self, sleep_seconds: float = 0.05) -> None:
        """sleep 시간을 설정한다."""
        self.sleep_seconds = sleep_seconds
        self.calls: list[dict[str, Any]] = []
        self._call_lock = asyncio.Lock()

    @property
    def model_name(self) -> str:
        """식별자."""
        return "mock-slow-llm"

    async def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        """sleep 후 마크다운 페이지 본문 반환."""
        # 호출 시점 기록
        async with self._call_lock:
            self.calls.append(
                {
                    "user_prompt": user_prompt,
                    "started_at": time.monotonic(),
                }
            )
        await asyncio.sleep(self.sleep_seconds)
        # 마크다운 페이지 본문 — _build_render_prompt 에서 user_prompt 에 title 이 들어감
        # decision title 을 user_prompt 에서 찾아 반환에 포함
        title_marker = "제목: "
        title = ""
        if title_marker in user_prompt:
            title = user_prompt.split(title_marker)[1].split("\n")[0].strip()
        return (
            "---\n"
            "type: decision\n"
            "date: 2026-04-28\n"
            "meeting_id: abc12345\n"
            "status: confirmed\n"
            "participants: []\n"
            "projects: []\n"
            "confidence: 9\n"
            "created_at: 2026-04-28T10:00:00+09:00\n"
            "updated_at: 2026-04-28T10:00:00+09:00\n"
            "---\n\n"
            f"# {title}\n\n"
            "## 결정 내용\n결정 [meeting:abc12345@00:23:45].\n\n"
            "## 배경\n배경 [meeting:abc12345@00:18:30].\n\n"
            "## 후속 액션\n- [ ] 액션 [meeting:abc12345@00:25:12]\n\n"
            "## 참고 회의\n- [abc12345](../../../app/viewer/abc12345)\n\n"
            "<!-- confidence: 9 -->"
        )


class _MockStore:
    """read_page 가 항상 KeyError 던지는 빈 저장소."""

    def read_page(self, rel_path: str) -> str:
        """없는 페이지로 가정."""
        raise KeyError("page_not_found")


def _build_decisions(count: int, base_confidence: int = 5) -> list[ExtractedDecision]:
    """count 개의 ExtractedDecision 을 만든다.

    confidence 는 base_confidence ~ base_confidence + count - 1 까지 단조증가 — 정렬 검증용.
    """
    return [
        ExtractedDecision(
            title=f"결정사항 {i:02d}",
            slug=f"decision-{i:02d}",
            decision_text=f"결정 본문 {i} [meeting:abc12345@00:23:45].",
            background=f"배경 {i} [meeting:abc12345@00:18:30].",
            confidence=base_confidence + i,
        )
        for i in range(count)
    ]


# ─────────────────────────────────────────────────────────────────────────
# 1. 병렬화 — N건 동시 처리
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_pages_parallel_execution_significantly_faster() -> None:
    """3건 decision 을 0.1s sleep mock 으로 처리 시 병렬은 ~0.1s, 직렬은 ~0.3s.

    asyncio.gather 로 병렬화되어 있으면 총 시간이 0.2s 미만이어야 한다.
    """
    sleep_per_call = 0.1
    decisions = _build_decisions(count=3)
    mock_llm = _SlowMockLLM(sleep_seconds=sleep_per_call)
    extractor = DecisionExtractor(llm=mock_llm)
    store = _MockStore()

    start = time.monotonic()
    pages = await extractor.render_pages(
        decisions=decisions,
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 28),
        existing_store=store,
    )
    elapsed = time.monotonic() - start

    assert len(pages) == 3
    assert len(mock_llm.calls) == 3
    # 병렬화 시 직렬(0.3s) 대비 절반 미만이어야 함
    assert elapsed < sleep_per_call * 2.5, (
        f"render_pages 가 병렬화되지 않았습니다. "
        f"3건 0.1s sleep 시 ~{sleep_per_call:.1f}s 예상이지만 {elapsed:.2f}s 소요."
    )


# ─────────────────────────────────────────────────────────────────────────
# 2. 페이지 갱신 상한 8개
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_pages_caps_at_8_when_more_decisions_provided() -> None:
    """10건 decision 입력 → 8건만 처리 (PRD R3 리스크 대응).

    confidence 내림차순 정렬 후 상위 8개만 처리한다.
    """
    decisions = _build_decisions(count=10, base_confidence=0)
    # confidence: 0,1,2,3,4,5,6,7,8,9
    mock_llm = _SlowMockLLM(sleep_seconds=0.0)
    extractor = DecisionExtractor(llm=mock_llm)
    store = _MockStore()

    pages = await extractor.render_pages(
        decisions=decisions,
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 28),
        existing_store=store,
    )

    # 정확히 8건만 페이지가 생성되어야 함
    assert len(pages) == 8, f"상한 8개 위반: {len(pages)}건 생성됨"
    # LLM 도 8회만 호출
    assert len(mock_llm.calls) == 8, (
        f"LLM 호출이 8회로 제한되어야 함: {len(mock_llm.calls)}회 호출"
    )


@pytest.mark.asyncio
async def test_render_pages_selects_top_8_by_confidence_desc() -> None:
    """10건 입력 시 confidence 가 높은 상위 8건이 선택된다.

    confidence: 0,1,2,3,4,5,6,7,8,9 → 선택: 9,8,7,6,5,4,3,2 (상위 8)
    제외: 0, 1
    """
    decisions = _build_decisions(count=10, base_confidence=0)
    mock_llm = _SlowMockLLM(sleep_seconds=0.0)
    extractor = DecisionExtractor(llm=mock_llm)
    store = _MockStore()

    pages = await extractor.render_pages(
        decisions=decisions,
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 28),
        existing_store=store,
    )

    # rel_path 에서 slug 추출 → 어느 decision 이 들어갔는지 확인
    slugs_used: set[str] = set()
    for rel_path, _ in pages:
        # decisions/2026-04-28-decision-XX.md
        slug = rel_path.replace("decisions/2026-04-28-", "").replace(".md", "")
        slugs_used.add(slug)

    # confidence 0,1 인 decision-00, decision-01 은 제외되어야 함
    assert "decision-00" not in slugs_used, "최저 confidence(0) 항목이 선택됨"
    assert "decision-01" not in slugs_used, "두번째 최저 confidence(1) 항목이 선택됨"
    # 상위 confidence(9, 8) 인 decision-09, decision-08 은 반드시 포함
    assert "decision-09" in slugs_used, "최고 confidence 항목 누락"
    assert "decision-08" in slugs_used, "두번째 최고 confidence 항목 누락"


@pytest.mark.asyncio
async def test_render_pages_below_8_decisions_processes_all() -> None:
    """8건 미만 입력 시 모두 처리 (slice 영향 없음)."""
    decisions = _build_decisions(count=5, base_confidence=3)
    mock_llm = _SlowMockLLM(sleep_seconds=0.0)
    extractor = DecisionExtractor(llm=mock_llm)
    store = _MockStore()

    pages = await extractor.render_pages(
        decisions=decisions,
        meeting_id="abc12345",
        meeting_date=date(2026, 4, 28),
        existing_store=store,
    )

    assert len(pages) == 5, f"5건 입력 시 5건 모두 처리되어야 함: {len(pages)}건"
    assert len(mock_llm.calls) == 5
