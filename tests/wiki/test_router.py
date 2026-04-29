"""QueryRouter TDD Red 단계 테스트 모듈 (Phase 5)

목적: core/wiki/router.py 의 QueryRouter.classify() 인터페이스를
  TDD Red 단계로 검증한다. core/wiki/router.py 가 아직 존재하지 않으므로
  모든 테스트는 ImportError 로 실패해야 한다.

테스트 범주:
    1. WIKI 강시그널 휴리스틱 — time_range_decisions, person_recent,
       project_status, aggregated_open (4건)
    2. RAG 강시그널 휴리스틱 — single_meeting_scope, exact_quote,
       utterance_search (3건)
    3. BOTH 시그널 — time_range_with_quote 겹침 (1건)
    4. LLM 폴백 — 고신뢰 채택, 저신뢰 RAG fallback (2건)

총 10건

의존성:
    - pytest, pytest-asyncio (asyncio_mode=auto, pyproject.toml 설정)
    - core.wiki.router (Phase 5, 아직 미구현 → ImportError Red)

작성자: TDD Red Author
날짜: 2026-04-29
"""

from __future__ import annotations

# ─── Phase 5 대상 모듈 (아직 미구현 → ImportError Red) ─────────────────────
from core.wiki.router import (  # type: ignore[import]  # noqa: E402
    QueryRouter,
    RouteDecision,
)

# ─────────────────────────────────────────────────────────────────────────────
# Mock — LLM 폴백 전용 (search/chat.py import 금지)
# ─────────────────────────────────────────────────────────────────────────────


class MockRouterLLM:
    """라우터의 LLM 폴백 mock.

    미리 설정된 JSON 응답 문자열을 반환해 LLM 호출을 시뮬레이션한다.
    호출 횟수를 기록해 used_llm 검증에 사용한다.
    """

    def __init__(self, response_json: str) -> None:
        """고정 응답 문자열을 설정한다.

        Args:
            response_json: LLM 이 반환할 JSON 형식 문자열.
        """
        self._response = response_json
        self.call_count = 0

    async def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        **kwargs,
    ) -> str:
        """고정 응답을 반환하고 호출 횟수를 증가시킨다.

        Args:
            system_prompt: 시스템 프롬프트 (사용하지 않음).
            user_prompt: 사용자 프롬프트 (사용하지 않음).
            **kwargs: 추가 파라미터 무시.

        Returns:
            초기화 시 설정된 고정 응답 문자열.
        """
        self.call_count += 1
        return self._response

    @property
    def model_name(self) -> str:
        """모델명 (테스트용 고정값).

        Returns:
            mock 모델명 문자열.
        """
        return "mock-gemma"


# ─────────────────────────────────────────────────────────────────────────────
# 1. WIKI 강시그널 휴리스틱 (4건)
# ─────────────────────────────────────────────────────────────────────────────


class TestWikiHeuristicSignals:
    """WIKI 강시그널 4종이 올바르게 WIKI 결정을 반환하는지 검증한다."""

    async def test_시간_범위_결정사항_질의는_wiki로_라우팅된다(self) -> None:
        """WIKI: '지난주 결정사항 정리해줘' → WIKI, confidence ≥ 7, time_range_decisions 시그널.

        §2.1 time_range_decisions: 시간 범위 키워드 + 결정/액션 키워드 동시 등장 시
        WIKI 강시그널이 발동해야 한다.

        Arrange: 휴리스틱 전용 라우터 (LLM 폴백 비활성)
        Act:     '지난주 결정사항 정리해줘' 분류
        Assert:  decision=WIKI, confidence ≥ 7, matched_signals 에 time_range_decisions
        """
        # Arrange
        router = QueryRouter(llm=None, enable_llm_fallback=False)

        # Act
        verdict = await router.classify("지난주 결정사항 정리해줘")

        # Assert
        assert verdict.decision == RouteDecision.WIKI, (
            f"'지난주 결정사항' 은 WIKI 여야 하나 {verdict.decision!r} 반환됨"
        )
        assert verdict.confidence >= 7, (
            f"WIKI 강시그널 confidence 는 ≥7 이어야 하나 {verdict.confidence}"
        )
        assert "time_range_decisions" in verdict.matched_signals, (
            f"matched_signals 에 'time_range_decisions' 없음: {verdict.matched_signals}"
        )

    async def test_인물_최근_결정_질의는_wiki로_라우팅된다(self) -> None:
        """WIKI: '철수가 최근 결정한 것' → WIKI, person_recent 시그널.

        §2.1 person_recent: [인물](가|이|의) + (최근|지난) + 결정/담당/진행 조합.

        Arrange: 휴리스틱 전용 라우터
        Act:     '철수가 최근 결정한 것' 분류
        Assert:  decision=WIKI, matched_signals 에 person_recent
        """
        # Arrange
        router = QueryRouter(llm=None, enable_llm_fallback=False)

        # Act
        verdict = await router.classify("철수가 최근 결정한 것")

        # Assert
        assert verdict.decision == RouteDecision.WIKI, (
            f"'철수가 최근 결정한 것' 은 WIKI 여야 하나 {verdict.decision!r}"
        )
        assert "person_recent" in verdict.matched_signals, (
            f"matched_signals 에 'person_recent' 없음: {verdict.matched_signals}"
        )

    async def test_프로젝트_진행상황_질의는_wiki로_라우팅된다(self) -> None:
        """WIKI: '프로젝트 X 진행 상황' → WIKI, project_status 시그널.

        §2.1 project_status: '프로젝트 [이름] 진행/상황/status' 패턴.

        Arrange: 휴리스틱 전용 라우터
        Act:     '프로젝트 X 진행 상황' 분류
        Assert:  decision=WIKI, matched_signals 에 project_status
        """
        # Arrange
        router = QueryRouter(llm=None, enable_llm_fallback=False)

        # Act
        verdict = await router.classify("프로젝트 X 진행 상황")

        # Assert
        assert verdict.decision == RouteDecision.WIKI, (
            f"'프로젝트 X 진행 상황' 은 WIKI 여야 하나 {verdict.decision!r}"
        )
        assert "project_status" in verdict.matched_signals, (
            f"matched_signals 에 'project_status' 없음: {verdict.matched_signals}"
        )

    async def test_오픈된_액션아이템_질의는_wiki로_라우팅된다(self) -> None:
        """WIKI: '오픈된 액션아이템 모두 보여줘' → WIKI, aggregated_open 시그널.

        §2.1 aggregated_open: '오픈된/미해결/미완료' + '액션/이슈/todo' 조합.

        Arrange: 휴리스틱 전용 라우터
        Act:     '오픈된 액션아이템 모두 보여줘' 분류
        Assert:  decision=WIKI, matched_signals 에 aggregated_open
        """
        # Arrange
        router = QueryRouter(llm=None, enable_llm_fallback=False)

        # Act
        verdict = await router.classify("오픈된 액션아이템 모두 보여줘")

        # Assert
        assert verdict.decision == RouteDecision.WIKI, (
            f"'오픈된 액션아이템' 은 WIKI 여야 하나 {verdict.decision!r}"
        )
        assert "aggregated_open" in verdict.matched_signals, (
            f"matched_signals 에 'aggregated_open' 없음: {verdict.matched_signals}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. RAG 강시그널 휴리스틱 (3건)
# ─────────────────────────────────────────────────────────────────────────────


class TestRagHeuristicSignals:
    """RAG 강시그널 3종이 올바르게 RAG 결정을 반환하는지 검증한다."""

    async def test_이번_회의_요약_질의는_rag로_라우팅된다(self) -> None:
        """RAG: '이번 회의 뭐 얘기했어?' → RAG, confidence ≥ 7, single_meeting_scope 시그널.

        §2.2 single_meeting_scope: 단일 회의 범위(이번/오늘/어제) + 요약/정리 키워드.
        시간 범위 키워드('최근/지난/3개월') 가 없어야 매칭.

        Arrange: 휴리스틱 전용 라우터
        Act:     '이번 회의 뭐 얘기했어?' 분류
        Assert:  decision=RAG, confidence ≥ 7, matched_signals 에 single_meeting_scope
        """
        # Arrange
        router = QueryRouter(llm=None, enable_llm_fallback=False)

        # Act
        verdict = await router.classify("이번 회의 뭐 얘기했어?")

        # Assert
        assert verdict.decision == RouteDecision.RAG, (
            f"'이번 회의 뭐 얘기했어' 는 RAG 여야 하나 {verdict.decision!r}"
        )
        assert verdict.confidence >= 7, (
            f"RAG 강시그널 confidence 는 ≥7 이어야 하나 {verdict.confidence}"
        )
        assert "single_meeting_scope" in verdict.matched_signals, (
            f"matched_signals 에 'single_meeting_scope' 없음: {verdict.matched_signals}"
        )

    async def test_정확한_발언_인용_질의는_rag로_라우팅된다(self) -> None:
        """RAG: '정확히 누가 X 라고 말했어?' → RAG, exact_quote 시그널.

        §2.2 exact_quote: '정확히/exactly/원본/verbatim' + '누가/뭐라고/말' 조합.

        Arrange: 휴리스틱 전용 라우터
        Act:     '정확히 누가 출시 연기라고 말했어?' 분류
        Assert:  decision=RAG, matched_signals 에 exact_quote
        """
        # Arrange
        router = QueryRouter(llm=None, enable_llm_fallback=False)

        # Act
        verdict = await router.classify("정확히 누가 출시 연기라고 말했어?")

        # Assert
        assert verdict.decision == RouteDecision.RAG, (
            f"'정확히 누가 ... 말했어' 는 RAG 여야 하나 {verdict.decision!r}"
        )
        assert "exact_quote" in verdict.matched_signals, (
            f"matched_signals 에 'exact_quote' 없음: {verdict.matched_signals}"
        )

    async def test_단어_검색_질의는_rag로_라우팅된다(self) -> None:
        """RAG: 'Zoom 언급한 회의' → RAG, utterance_search 시그널.

        §2.2 utterance_search: '[단어] 언급/나온/등장 + 회의/미팅' 패턴.

        Arrange: 휴리스틱 전용 라우터
        Act:     'Zoom 언급한 회의' 분류
        Assert:  decision=RAG, matched_signals 에 utterance_search
        """
        # Arrange
        router = QueryRouter(llm=None, enable_llm_fallback=False)

        # Act
        verdict = await router.classify("Zoom 언급한 회의")

        # Assert
        assert verdict.decision == RouteDecision.RAG, (
            f"'Zoom 언급한 회의' 는 RAG 여야 하나 {verdict.decision!r}"
        )
        assert "utterance_search" in verdict.matched_signals, (
            f"matched_signals 에 'utterance_search' 없음: {verdict.matched_signals}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. BOTH 시그널 (1건)
# ─────────────────────────────────────────────────────────────────────────────


class TestBothSignals:
    """WIKI + RAG 시그널 동시 매칭 시 BOTH 가 반환되는지 검증한다."""

    async def test_시간범위와_정확인용_동시_매칭은_both로_라우팅된다(self) -> None:
        """BOTH: '지난주 회의에서 누가 X 라고 말했어' → BOTH.

        §2.3 time_range_with_quote: WIKI time_range_decisions AND RAG exact_quote
        동시 매칭 → BOTH 결정 (confidence=8).

        §2.4 매칭 알고리즘: wiki + rag 동시 매칭 → BOTH 로 격상.

        Arrange: 휴리스틱 전용 라우터
        Act:     '지난주 회의에서 정확히 누가 출시 결정이라고 말했어?' 분류
        Assert:  decision=BOTH
        """
        # Arrange
        router = QueryRouter(llm=None, enable_llm_fallback=False)

        # Act
        verdict = await router.classify("지난주 회의에서 정확히 누가 출시 결정이라고 말했어?")

        # Assert
        assert verdict.decision == RouteDecision.BOTH, (
            f"'지난주 회의에서 정확히 누가 ... 말했어' 는 BOTH 여야 하나 {verdict.decision!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. LLM 폴백 시나리오 (2건)
# ─────────────────────────────────────────────────────────────────────────────


class TestLlmFallback:
    """휴리스틱 매칭 0건일 때 LLM 폴백 동작을 검증한다."""

    async def test_모호한_질의에_llm이_고신뢰_rag_반환하면_rag로_결정된다(self) -> None:
        """모호한 질의 + LLM confidence=8 + decision='rag' → RAG 채택, used_llm=True.

        §1.4 QueryRouter 분류 정책 5~6: 매칭 0건 + enable_llm_fallback=True →
        LLM 호출. confidence ≥ 7 이면 LLM 결정 채택.

        Arrange: 모호한 질의("회의 어땠어?"), MockRouterLLM이 confidence=8+rag 반환
        Act:     router.classify() 호출
        Assert:  decision=RAG, used_llm=True (LLM 결과 채택)
        """
        # Arrange — LLM 이 confidence=8, decision="rag" 를 JSON 으로 반환
        mock_llm = MockRouterLLM(
            response_json='{"decision": "rag", "confidence": 8, "reason": "단일 회의 감상 질의"}'
        )
        router = QueryRouter(
            llm=mock_llm,
            enable_llm_fallback=True,
            confidence_threshold=7,
        )

        # Act
        verdict = await router.classify("회의 어땠어?")

        # Assert
        assert verdict.decision == RouteDecision.RAG, (
            f"LLM confidence=8 + decision=rag 이면 RAG 여야 하나 {verdict.decision!r}"
        )
        assert verdict.used_llm is True, "LLM 폴백이 호출됐으면 used_llm=True 여야 함"
        assert mock_llm.call_count >= 1, (
            f"MockRouterLLM.generate() 가 호출되지 않음 (call_count={mock_llm.call_count})"
        )

    async def test_모호한_질의에_llm이_저신뢰_반환하면_rag_fallback된다(self) -> None:
        """모호한 질의 + LLM confidence=5 → 임계(7) 미만 → 보수적 RAG fallback.

        §1.4 분류 정책 6: LLM confidence < threshold(7) → RAG fallback (보수적).
        RouterVerdict.confidence=5 로 '확신도 낮음' 신호 포함.

        Arrange: MockRouterLLM이 confidence=5 반환
        Act:     router.classify() 호출
        Assert:  decision=RAG, confidence=5 (보수적 fallback 값)
        """
        # Arrange — LLM 이 confidence=5 (임계 7 미만)
        mock_llm = MockRouterLLM(
            response_json='{"decision": "wiki", "confidence": 5, "reason": "불명확"}'
        )
        router = QueryRouter(
            llm=mock_llm,
            enable_llm_fallback=True,
            confidence_threshold=7,
        )

        # Act
        verdict = await router.classify("회의 관련해서 뭔가 알고 싶어")

        # Assert — confidence < 7 이므로 LLM 결정 무시, RAG fallback
        assert verdict.decision == RouteDecision.RAG, (
            f"LLM confidence=5 < 7 이면 RAG fallback 이어야 하나 {verdict.decision!r}"
        )
        assert verdict.confidence == 5, (
            f"보수적 fallback confidence 는 5 여야 하나 {verdict.confidence}"
        )
