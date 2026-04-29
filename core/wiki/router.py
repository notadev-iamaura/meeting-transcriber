"""Wiki vs RAG 라우팅 모듈 (Phase 5)

목적: 사용자 질의를 분석해 WIKI(누적 합성) / RAG(정확 인용) / BOTH(둘 다) 중
어느 응답 채널을 사용할지 결정한다. 보수적 default — 애매하면 RAG.

설계 원칙:
    - 휴리스틱 우선: 명확한 키워드/패턴은 LLM 호출 없이 즉시 결정.
    - LLM 폴백 보수성: confidence < 7 이면 무조건 RAG (PRD §6 D3 와 동일 임계).
    - 결정 추적성: RouterVerdict.matched_signals + reason 으로 운영 데이터 수집.
    - 비활성 default: WikiConfig.router_enabled=False 일 때 호출되지 않음.

의존성:
    - core.wiki.llm_client.WikiLLMClient (LLM 폴백 — 옵션, TYPE_CHECKING)
    - 표준 라이브러리만 (re, dataclasses, enum, json, logging)

**search/* 모듈은 import 금지** (RAG 격리 보장).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.wiki.llm_client import WikiLLMClient

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# 1.1 결정 enum
# ─────────────────────────────────────────────────────────────────────────


class RouteDecision(StrEnum):
    """질의 라우팅 결정.

    WIKI: 위키 페이지만 답변 (누적 합성).
    RAG:  기존 RAG 만 답변 (정확 인용 우선).
    BOTH: 둘 다 표시 (사용자 선택).
    """

    WIKI = "wiki"
    RAG = "rag"
    BOTH = "both"


# ─────────────────────────────────────────────────────────────────────────
# 1.2 라우팅 결과
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RouterVerdict:
    """단일 라우팅 결정의 결과.

    Attributes:
        decision: WIKI / RAG / BOTH 중 하나.
        confidence: 0~10 정수. 휴리스틱 강시그널은 9~10, LLM 폴백 채택은 7~10,
            폴백/모호한 경우 RAG default 는 5 (보수적).
        reason: 한국어 짧은 설명.
        matched_signals: 매칭된 휴리스틱 시그널 ID 목록.
        used_llm: LLM 폴백 호출 여부 (운영 비용 측정용).
    """

    decision: RouteDecision
    confidence: int
    reason: str
    matched_signals: list[str] = field(default_factory=list)
    used_llm: bool = False


# ─────────────────────────────────────────────────────────────────────────
# 1.3 에러
# ─────────────────────────────────────────────────────────────────────────


class RouterError(Exception):
    """라우터 동작 중 escalate 할 실패. fail-safe 경로에서는 raise 안 함."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        """기술적 실패 사유 코드와 상세 메시지를 받는다.

        Args:
            reason: 안정적 코드(snake_case).
            detail: 사람이 읽는 메시지.
        """
        super().__init__(reason)
        self.reason: str = reason
        self.detail: str | None = detail


# ─────────────────────────────────────────────────────────────────────────
# 1.4 휴리스틱 시그널 정의 (PRD §2.1~§2.3 매핑)
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Signal:
    """단일 휴리스틱 시그널의 매칭 패턴 + 메타데이터.

    Attributes:
        signal_id: 시그널 안정적 식별자 (운영 telemetry 키).
        pattern: 컴파일된 정규식. re.IGNORECASE 적용 가정.
        decision: 매칭 시 부여할 RouteDecision.
        confidence: 매칭 시 부여할 confidence (0~10).
        reason: 한국어 reason 메시지.
    """

    signal_id: str
    pattern: re.Pattern[str]
    decision: RouteDecision
    confidence: int
    reason: str


# 시간 범위 키워드 — RAG single_meeting_scope 매칭 배제용.
# (이 키워드가 함께 등장하면 단일 회의가 아닌 누적 질의로 본다.)
_TIME_RANGE_KEYWORDS: re.Pattern[str] = re.compile(
    r"(최근|지난\s*주|지난\s*달|최근\s*\d+\s*(주|개?월|년)|\d+\s*개?월|3개월)",
    re.IGNORECASE,
)


# ─── WIKI 강시그널 (4종, confidence 9) ──────────────────────────────────────

_WIKI_SIGNALS: tuple[_Signal, ...] = (
    # time_range_decisions: 시간 범위 + 결정/액션 동시 등장
    _Signal(
        signal_id="time_range_decisions",
        pattern=re.compile(
            r"(?=.*?(지난|최근|최근에|이번\s*주|지난\s*주|지난\s*달"
            r"|최근\s*\d+\s*(?:주|개?월|년)))"
            r"(?=.*?(결정|액션|결정사항|액션\s*아이템|action\s*item))",
            re.IGNORECASE,
        ),
        decision=RouteDecision.WIKI,
        confidence=9,
        reason="시간 범위 + 결정/액션 키워드 매칭",
    ),
    # person_recent: [인물](가|이|의) + (최근|지난) + 결정/담당/진행/말한/언급
    _Signal(
        signal_id="person_recent",
        pattern=re.compile(
            r"\S+?(?:가|이|의)\s*(?:최근|지난).*?(?:결정|담당|진행|말한|언급)",
            re.IGNORECASE,
        ),
        decision=RouteDecision.WIKI,
        confidence=9,
        reason="인물 + 시간 범위 + 활동 키워드 매칭",
    ),
    # project_status: '프로젝트 [이름] 진행/상황/status' 또는 역순
    _Signal(
        signal_id="project_status",
        pattern=re.compile(
            r"(프로젝트\s*\S+\s*(?:진행|상황|status|상태|어디까지)"
            r"|\S+\s*프로젝트\s*(?:진행|상황|status|상태))",
            re.IGNORECASE,
        ),
        decision=RouteDecision.WIKI,
        confidence=9,
        reason="프로젝트 진행 상황 질의",
    ),
    # aggregated_open: 오픈된/미해결/진행중 + 액션/이슈/todo 또는 액션아이템 + 모두/리스트
    _Signal(
        signal_id="aggregated_open",
        pattern=re.compile(
            r"((?:오픈된?|미해결|미완료|진행\s*중인?|남은)\s*(?:액션|이슈|todo|할\s*일|일정|task)"
            r"|(?:액션\s*아이템|할\s*일)\s*(?:모두|전부|전체|리스트|목록))",
            re.IGNORECASE,
        ),
        decision=RouteDecision.WIKI,
        confidence=9,
        reason="오픈/미해결 통합 질의",
    ),
)


# ─── RAG 강시그널 (3종, confidence 9) ────────────────────────────────────────

_RAG_SIGNALS: tuple[_Signal, ...] = (
    # single_meeting_scope: '이번/오늘/어제' 회의 + '뭐/요약/정리'
    # 단, 시간 범위 키워드(최근/지난/3개월)가 함께 있으면 안 됨.
    _Signal(
        signal_id="single_meeting_scope",
        pattern=re.compile(
            r"(?=.*?(이번|오늘|어제|방금|좀\s*전|아까)\s*(?:회의|미팅|이?\s*세션))"
            r"(?=.*?(뭐|뭘|무엇을|어떤\s*얘기|요약|정리|어땠))",
            re.IGNORECASE,
        ),
        decision=RouteDecision.RAG,
        confidence=9,
        reason="단일 회의 범위 요약/정리 질의",
    ),
    # exact_quote: '정확히/원본/verbatim' + '누가/뭐라고/말'
    # 또는 '[화자] 발언 원본'
    _Signal(
        signal_id="exact_quote",
        pattern=re.compile(
            r"((?:정확히|exactly|원본|원문|verbatim)\s*.*?(?:누가|뭐라고|어떻게|말했|발언|얘기)"
            r"|\S+\s*발언\s*(?:원본|원문)"
            r"|(?:누가)?\s*\"[^\"]+\"\s*(?:라고|이라고)\s*말)",
            re.IGNORECASE,
        ),
        decision=RouteDecision.RAG,
        confidence=9,
        reason="정확한 발언 인용 질의",
    ),
    # utterance_search: '[단어] 언급/나온/등장 + 회의/미팅' 또는 '"단어" 언급'
    _Signal(
        signal_id="utterance_search",
        pattern=re.compile(
            r"(\"[^\"]{2,}\"\s*(?:라는|단어|표현|언급).*?(?:회의|미팅|발화)"
            r"|\S+\s*(?:라는\s*)?(?:단어|표현|용어)?\s*(?:언급|나온|등장)(?:한|된|했)?\s*(?:회의|미팅))",
            re.IGNORECASE,
        ),
        decision=RouteDecision.RAG,
        confidence=9,
        reason="특정 단어/표현 언급 검색",
    ),
)


# ─── BOTH 시그널 (1종, confidence 8) ─────────────────────────────────────────

_BOTH_SIGNALS: tuple[_Signal, ...] = (
    # explicit_both: 사용자가 명시적으로 두 채널을 모두 요청
    # "모두/전체" 단독은 약한 신호 — 두 채널을 가리키는 키워드가 필요
    # (위키, wiki, 둘 다, both 등). "모두 보여줘" 같은 표현은
    # WIKI aggregated_open 시그널이 잡도록 양보한다.
    _Signal(
        signal_id="explicit_both",
        pattern=re.compile(
            r"(둘\s*다|wiki\s*랑|위키\s*랑|위키.*?채팅|채팅.*?위키|both)"
            r"\s*(?:보여|표시|찾아|검색|나열|알려)?",
            re.IGNORECASE,
        ),
        decision=RouteDecision.BOTH,
        confidence=8,
        reason="명시적 BOTH 시그널 매칭",
    ),
)


# ─────────────────────────────────────────────────────────────────────────
# 1.5 핵심 클래스 — QueryRouter
# ─────────────────────────────────────────────────────────────────────────


class QueryRouter:
    """질의 의도 분류기 (휴리스틱 우선 + LLM 폴백).

    분류 정책:
        1. 휴리스틱 매칭 시도 (BOTH > WIKI+RAG 동시 > WIKI > RAG 우선순위).
        2. 매칭 0건 + enable_llm_fallback=True → LLM 한 번 호출.
        3. LLM confidence ≥ threshold → 채택, 그 외 → RAG fallback (보수).
        4. enable_llm_fallback=False 또는 LLM 실패 → RAG fallback.

    Threading: 인스턴스 자체는 stateless 라 동시 classify() 호출 안전.
    LLM 호출은 self._llm 에 위임된다.

    Args:
        llm: WikiLLMClient — LLM 폴백용. None 이면 LLM 폴백 비활성.
        enable_llm_fallback: False 면 휴리스틱만 사용 (테스트/저비용 모드).
        confidence_threshold: LLM 응답 채택 최소 confidence (기본 7).
    """

    def __init__(
        self,
        llm: WikiLLMClient | None = None,
        *,
        enable_llm_fallback: bool = True,
        confidence_threshold: int = 7,
    ) -> None:
        """라우터 인스턴스를 생성한다.

        Args:
            llm: WikiLLMClient 또는 None.
            enable_llm_fallback: LLM 폴백 활성 여부.
            confidence_threshold: LLM 결정 채택 최소 confidence (0~10).
        """
        self._llm = llm
        self._enable_llm_fallback = enable_llm_fallback
        self._confidence_threshold = confidence_threshold

    async def classify(self, query: str) -> RouterVerdict:
        """질의를 분류하여 RouterVerdict 를 반환한다.

        실패 시에도 절대 raise 하지 않고 RAG fallback verdict 를 반환한다.

        Args:
            query: 사용자 입력.

        Returns:
            RouterVerdict (decision/confidence/reason/matched_signals/used_llm).
        """
        # 빈 입력 방어 — 그대로 RAG fallback
        if not query or not query.strip():
            return self._rag_fallback(reason="empty_query")

        # 1. 휴리스틱 매칭 시도
        heuristic_verdict = self._match_heuristics(query)
        if heuristic_verdict is not None:
            logger.debug(
                "휴리스틱 매칭 성공: decision=%s, signals=%s",
                heuristic_verdict.decision,
                heuristic_verdict.matched_signals,
            )
            return heuristic_verdict

        # 2. 휴리스틱 매칭 0건 → LLM 폴백 시도
        if self._enable_llm_fallback and self._llm is not None:
            llm_verdict = await self._llm_classify(query)
            if llm_verdict is not None:
                # confidence 임계 미만 → RAG fallback
                if llm_verdict.confidence < self._confidence_threshold:
                    logger.debug(
                        "LLM confidence=%d < threshold=%d → RAG fallback",
                        llm_verdict.confidence,
                        self._confidence_threshold,
                    )
                    return self._rag_fallback(reason="llm_low_confidence_fallback")
                # 채택
                return llm_verdict

        # 3. 모든 폴백 실패 → 보수적 RAG
        return self._rag_fallback(reason="default_conservative")

    def _match_heuristics(self, query: str) -> RouterVerdict | None:
        """휴리스틱 정규식 매칭. 매칭 0건이면 None.

        매칭 우선순위 (PRD §2.4):
            1. BOTH 시그널 (explicit_both) — 가장 명시적 사용자 의도.
            2. WIKI + RAG 동시 매칭 → BOTH 로 격상 (confidence 7).
            3. WIKI 시그널만 → WIKI.
            4. RAG 시그널만 → RAG.

        Args:
            query: 사용자 입력.

        Returns:
            RouterVerdict 또는 None (매칭 0건).
        """
        matched_wiki = [s for s in _WIKI_SIGNALS if s.pattern.search(query)]
        matched_rag_raw = [s for s in _RAG_SIGNALS if s.pattern.search(query)]
        matched_both = [s for s in _BOTH_SIGNALS if s.pattern.search(query)]

        # single_meeting_scope 는 시간 범위 키워드와 공존하면 매칭 무효화
        # (단일 회의가 아닌 누적 질의로 재해석)
        matched_rag = [
            s
            for s in matched_rag_raw
            if not (s.signal_id == "single_meeting_scope" and _TIME_RANGE_KEYWORDS.search(query))
        ]

        # 1. BOTH 명시 매칭 우선
        if matched_both:
            ids = [s.signal_id for s in matched_both]
            return RouterVerdict(
                decision=RouteDecision.BOTH,
                confidence=8,
                reason="명시적 BOTH 시그널 매칭",
                matched_signals=ids,
                used_llm=False,
            )

        # 2. WIKI + RAG 동시 매칭 → BOTH 격상
        if matched_wiki and matched_rag:
            ids = [s.signal_id for s in matched_wiki] + [s.signal_id for s in matched_rag]
            return RouterVerdict(
                decision=RouteDecision.BOTH,
                confidence=7,
                reason="wiki_and_rag_signals_both_matched",
                matched_signals=ids,
                used_llm=False,
            )

        # 3. WIKI 단독
        if matched_wiki:
            ids = [s.signal_id for s in matched_wiki]
            # 가장 높은 confidence 선택
            best = max(matched_wiki, key=lambda s: s.confidence)
            return RouterVerdict(
                decision=RouteDecision.WIKI,
                confidence=best.confidence,
                reason=best.reason,
                matched_signals=ids,
                used_llm=False,
            )

        # 4. RAG 단독
        if matched_rag:
            ids = [s.signal_id for s in matched_rag]
            best = max(matched_rag, key=lambda s: s.confidence)
            return RouterVerdict(
                decision=RouteDecision.RAG,
                confidence=best.confidence,
                reason=best.reason,
                matched_signals=ids,
                used_llm=False,
            )

        # 5. 매칭 0건
        return None

    async def _llm_classify(self, query: str) -> RouterVerdict | None:
        """LLM 폴백. 실패 시 None (호출자가 RAG fallback 처리).

        LLM 에 JSON 응답을 요청하여 decision/confidence/reason 을 파싱한다.

        Args:
            query: 사용자 입력.

        Returns:
            RouterVerdict 또는 None (LLM 호출 실패 또는 파싱 실패).
        """
        if self._llm is None:
            return None

        system_prompt = (
            "당신은 회의록 채팅 시스템의 라우터다. "
            "사용자 질의를 분석해 다음 셋 중 하나를 결정한다:\n"
            "- 'wiki': 시간 범위에 걸친 누적 질의 (지난주 결정사항 등)\n"
            "- 'rag': 단일 회의 정확 인용 질의 (이번 회의 요약 등)\n"
            "- 'both': 둘 다 필요\n"
            "JSON 형식으로만 응답: "
            '{"decision": "rag|wiki|both", '
            '"confidence": 0-10 정수, "reason": "한국어 짧은 설명"}'
        )
        user_prompt = f"질의: {query}"

        try:
            response = await self._llm.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except Exception as exc:  # noqa: BLE001 — LLM 백엔드 실패는 fail-safe
            logger.warning("LLM 폴백 호출 실패: %s", exc)
            return None

        # JSON 파싱 — 실패 시 None
        try:
            parsed = json.loads(response.strip())
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("LLM 응답 JSON 파싱 실패: %s (응답=%r)", exc, response)
            return None

        if not isinstance(parsed, dict):
            logger.warning("LLM 응답이 dict 아님: %r", parsed)
            return None

        # decision 검증
        decision_str = parsed.get("decision", "").lower()
        try:
            decision = RouteDecision(decision_str)
        except ValueError:
            logger.warning("LLM 응답 decision 무효: %r", decision_str)
            return None

        # confidence 검증
        confidence_raw = parsed.get("confidence", 0)
        try:
            confidence = int(confidence_raw)
        except (TypeError, ValueError):
            logger.warning("LLM 응답 confidence 무효: %r", confidence_raw)
            return None

        confidence = max(0, min(10, confidence))
        reason = str(parsed.get("reason", "llm_fallback"))

        return RouterVerdict(
            decision=decision,
            confidence=confidence,
            reason=reason,
            matched_signals=[],
            used_llm=True,
        )

    @staticmethod
    def _rag_fallback(reason: str = "default_conservative") -> RouterVerdict:
        """모든 fail-safe 경로의 단일 진입점.

        confidence=5 로 명시 — UI 가 "확신도 낮음" 표시할 수 있게.

        Args:
            reason: fallback 사유 (telemetry 키).

        Returns:
            RouteDecision.RAG, confidence=5 인 RouterVerdict.
        """
        return RouterVerdict(
            decision=RouteDecision.RAG,
            confidence=5,
            reason=reason,
            matched_signals=[],
            used_llm=False,
        )
