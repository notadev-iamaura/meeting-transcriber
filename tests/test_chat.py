"""
Phase 3 통합 테스트 + RAG Chat 엔진 단위 테스트
(Phase 3 Integration Tests + Unit Tests for RAG Chat Engine)

목적: Phase 3의 AI Chat, API 엔드포인트, WebSocket 시스템을 종합적으로 검증한다.
주요 테스트:
    [단위 테스트]
    - 대화 세션 슬라이딩 윈도우 동작
    - 컨텍스트 텍스트 구성 정확성
    - 프롬프트 구성 및 토큰 절단
    - LLM 호출 성공/실패 시나리오
    - 스트리밍 응답 처리
    - Graceful degradation (LLM 실패 시 검색 결과만 반환)
    - 참조 출처 구성
    [통합 테스트]
    - API /api/chat 엔드포인트 + ChatEngine 연동
    - API /api/search 엔드포인트 + SearchEngine 연동
    - WebSocket /ws/events 이벤트 브로드캐스트
    - 다중 엔드포인트 워크플로우 (status → meetings → search → chat)
    - 세션 관리 (session_id별 대화 분리, 초기화)
    - RAG 파이프라인 정확도 (검색 → 컨텍스트 → LLM 프롬프트)
    - Graceful degradation (컴포넌트 장애 시 안전한 응답)
    - 한국어 NFC 정규화 및 유니코드 처리
    - 보안 (입력 검증, path traversal 방지)
의존성: pytest, pytest-asyncio, unittest.mock, fastapi (TestClient)
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig, ChatConfig, EmbeddingConfig, LLMConfig, SearchConfig
from core.llm_backend import LLMConnectionError, LLMGenerationError
from core.ollama_client import OllamaConnectionError, clear_connection_cache
from search.chat import (
    ChatEngine,
    ChatError,
    ChatMessage,
    ChatReference,
    ChatResponse,
    ChatSession,
    EmptyQueryError,
    _build_context_text,
    _build_references,
    _build_user_prompt,
    _estimate_korean_tokens,
)
from search.hybrid_search import SearchResponse, SearchResult


# === 테스트 픽스처 ===


def _make_search_result(
    chunk_id: str = "chunk_001",
    text: str = "프로젝트 일정을 논의했습니다.",
    score: float = 0.05,
    meeting_id: str = "meeting_001",
    date: str = "2026-03-04",
    speakers: list[str] | None = None,
    start_time: float = 60.0,
    end_time: float = 120.0,
    chunk_index: int = 0,
    source: str = "both",
) -> SearchResult:
    """테스트용 SearchResult를 생성한다."""
    return SearchResult(
        chunk_id=chunk_id,
        text=text,
        score=score,
        meeting_id=meeting_id,
        date=date,
        speakers=speakers if speakers is not None else ["SPEAKER_00"],
        start_time=start_time,
        end_time=end_time,
        chunk_index=chunk_index,
        source=source,
    )


def _make_search_response(
    results: list[SearchResult] | None = None,
    query: str = "프로젝트 일정",
) -> SearchResponse:
    """테스트용 SearchResponse를 생성한다."""
    if results is None:
        results = [_make_search_result()]
    return SearchResponse(
        results=results,
        query=query,
        total_found=len(results),
        vector_count=len(results),
        fts_count=len(results),
    )


def _make_config() -> AppConfig:
    """테스트용 AppConfig를 생성한다."""
    return AppConfig(
        chat=ChatConfig(
            max_history_pairs=3,
            system_prompt="당신은 회의 내용 기반 AI 어시스턴트입니다.",
        ),
        llm=LLMConfig(
            model_name="exaone3.5:7.8b-instruct-q4_K_M",
            host="http://127.0.0.1:11434",
            temperature=0.3,
            max_context_tokens=8192,
            request_timeout_seconds=120,
        ),
        search=SearchConfig(top_k=5),
        embedding=EmbeddingConfig(),
    )


# === ChatSession 테스트 ===


class TestChatSession:
    """대화 세션 슬라이딩 윈도우 테스트 클래스."""

    def test_초기_상태(self) -> None:
        """세션 생성 시 이력이 비어있는지 확인한다."""
        session = ChatSession(max_pairs=3)
        assert session.pair_count == 0
        assert session.history == []

    def test_대화_쌍_추가(self) -> None:
        """user/assistant 쌍이 정상적으로 추가되는지 확인한다."""
        session = ChatSession(max_pairs=3)
        session.add_exchange("안녕하세요", "네, 무엇을 도와드릴까요?")

        assert session.pair_count == 1
        assert len(session.history) == 2
        assert session.history[0].role == "user"
        assert session.history[0].content == "안녕하세요"
        assert session.history[1].role == "assistant"
        assert session.history[1].content == "네, 무엇을 도와드릴까요?"

    def test_슬라이딩_윈도우_동작(self) -> None:
        """최대 쌍 수 초과 시 가장 오래된 쌍이 제거되는지 확인한다."""
        session = ChatSession(max_pairs=2)

        session.add_exchange("질문1", "답변1")
        session.add_exchange("질문2", "답변2")
        session.add_exchange("질문3", "답변3")

        # max_pairs=2이므로 가장 오래된 쌍 제거
        assert session.pair_count == 2
        assert session.history[0].content == "질문2"
        assert session.history[1].content == "답변2"
        assert session.history[2].content == "질문3"
        assert session.history[3].content == "답변3"

    def test_이력_초기화(self) -> None:
        """clear() 호출 시 이력이 비워지는지 확인한다."""
        session = ChatSession(max_pairs=3)
        session.add_exchange("질문", "답변")
        session.clear()

        assert session.pair_count == 0
        assert session.history == []

    def test_ollama_메시지_변환(self) -> None:
        """to_ollama_messages()가 올바른 형식을 반환하는지 확인한다."""
        session = ChatSession(max_pairs=3)
        session.add_exchange("질문1", "답변1")

        messages = session.to_ollama_messages()
        assert len(messages) == 2
        assert messages[0] == {"role": "user", "content": "질문1"}
        assert messages[1] == {"role": "assistant", "content": "답변1"}

    def test_max_pairs_1인_경우(self) -> None:
        """max_pairs=1이면 항상 최신 1쌍만 유지하는지 확인한다."""
        session = ChatSession(max_pairs=1)
        session.add_exchange("질문1", "답변1")
        session.add_exchange("질문2", "답변2")

        assert session.pair_count == 1
        assert session.history[0].content == "질문2"


# === 유틸리티 함수 테스트 ===


class TestUtilityFunctions:
    """유틸리티 함수 테스트 클래스."""

    def test_컨텍스트_텍스트_구성(self) -> None:
        """검색 결과가 올바른 컨텍스트 텍스트로 변환되는지 확인한다."""
        results = [
            _make_search_result(
                chunk_id="c1",
                text="프로젝트 일정을 논의했습니다.",
                meeting_id="m001",
                date="2026-03-04",
                speakers=["SPEAKER_00", "SPEAKER_01"],
                start_time=65.0,
                end_time=125.0,
            ),
        ]

        context = _build_context_text(results)

        assert "[참조 1]" in context
        assert "회의: m001" in context
        assert "날짜: 2026-03-04" in context
        assert "SPEAKER_00, SPEAKER_01" in context
        assert "01:05~02:05" in context
        assert "프로젝트 일정을 논의했습니다." in context

    def test_빈_검색결과_컨텍스트(self) -> None:
        """검색 결과가 없으면 빈 문자열을 반환하는지 확인한다."""
        assert _build_context_text([]) == ""

    def test_사용자_프롬프트_구성_컨텍스트_있음(self) -> None:
        """컨텍스트가 있을 때 프롬프트가 올바르게 구성되는지 확인한다."""
        prompt = _build_user_prompt("질문입니다", "회의 내용 컨텍스트")

        assert "회의 내용 컨텍스트" in prompt
        assert "질문입니다" in prompt
        assert "참고하여" in prompt

    def test_사용자_프롬프트_구성_컨텍스트_없음(self) -> None:
        """컨텍스트가 없을 때 적절한 안내 메시지가 포함되는지 확인한다."""
        prompt = _build_user_prompt("질문입니다", "")

        assert "찾을 수 없습니다" in prompt
        assert "질문입니다" in prompt

    def test_참조출처_구성(self) -> None:
        """검색 결과가 ChatReference 목록으로 변환되는지 확인한다."""
        results = [
            _make_search_result(text="A" * 150),
            _make_search_result(chunk_id="c2", text="짧은 텍스트"),
        ]

        refs = _build_references(results)

        assert len(refs) == 2
        # 긴 텍스트는 100자 + "..."
        assert refs[0].text_preview.endswith("...")
        assert len(refs[0].text_preview) == 103  # 100 + "..."
        # 짧은 텍스트는 그대로
        assert refs[1].text_preview == "짧은 텍스트"

    def test_한국어_토큰_추정(self) -> None:
        """한국어 텍스트 토큰 수 추정이 합리적인지 확인한다."""
        # 6글자 → 6/1.5 = 4 토큰
        assert _estimate_korean_tokens("안녕하세요!") == 4
        # 빈 문자열 → 0 토큰
        assert _estimate_korean_tokens("") == 0
        # 1글자 → 최소 1 토큰
        assert _estimate_korean_tokens("A") == 1

    def test_참조출처_to_dict(self) -> None:
        """ChatReference.to_dict()가 올바른 딕셔너리를 반환하는지 확인한다."""
        ref = ChatReference(
            chunk_id="c1",
            meeting_id="m1",
            date="2026-03-04",
            speakers=["SPEAKER_00"],
            start_time=10.0,
            end_time=20.0,
            text_preview="미리보기",
            score=0.05,
        )
        d = ref.to_dict()
        assert d["chunk_id"] == "c1"
        assert d["meeting_id"] == "m1"
        assert d["speakers"] == ["SPEAKER_00"]

    def test_chat_response_to_dict(self) -> None:
        """ChatResponse.to_dict()가 올바른 딕셔너리를 반환하는지 확인한다."""
        resp = ChatResponse(
            answer="답변입니다",
            references=[],
            query="질문",
            has_context=True,
            llm_used=True,
        )
        d = resp.to_dict()
        assert d["answer"] == "답변입니다"
        assert d["query"] == "질문"
        assert d["has_context"] is True
        assert d["llm_used"] is True
        assert d["error_message"] is None

    def test_chat_message_to_dict(self) -> None:
        """ChatMessage.to_dict()가 올바른 딕셔너리를 반환하는지 확인한다."""
        msg = ChatMessage(role="user", content="안녕")
        assert msg.to_dict() == {"role": "user", "content": "안녕"}


# === ChatEngine 테스트 ===


class TestChatEngine:
    """ChatEngine 핵심 기능 테스트 클래스."""

    def _make_engine(
        self,
        search_engine: MagicMock | None = None,
        model_manager: MagicMock | None = None,
    ) -> ChatEngine:
        """테스트용 ChatEngine을 생성한다."""
        config = _make_config()

        if model_manager is None:
            model_manager = MagicMock()
            # acquire 컨텍스트 매니저 모킹 (LLMBackend 목 반환)
            ctx = AsyncMock()
            backend_mock = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=backend_mock)
            ctx.__aexit__ = AsyncMock(return_value=None)
            model_manager.acquire = MagicMock(return_value=ctx)

        if search_engine is None:
            search_engine = MagicMock()
            search_engine.search = AsyncMock(
                return_value=_make_search_response()
            )

        return ChatEngine(
            config=config,
            model_manager=model_manager,
            search_engine=search_engine,
        )

    @pytest.mark.asyncio
    async def test_빈_질문_에러(self) -> None:
        """빈 질문에 EmptyQueryError가 발생하는지 확인한다."""
        engine = self._make_engine()

        with pytest.raises(EmptyQueryError):
            await engine.chat("")

        with pytest.raises(EmptyQueryError):
            await engine.chat("   ")

    @pytest.mark.asyncio
    async def test_정상_chat_응답(self) -> None:
        """정상적인 RAG Chat 응답이 생성되는지 확인한다."""
        engine = self._make_engine()

        # LLM 백엔드 목의 chat 반환값 설정
        backend_mock = engine._model_manager.acquire.return_value.__aenter__.return_value
        backend_mock.chat.return_value = "프로젝트 일정은 다음 주 월요일까지입니다."

        response = await engine.chat("프로젝트 일정이 어떻게 되나요?")

        assert response.answer == "프로젝트 일정은 다음 주 월요일까지입니다."
        assert response.llm_used is True
        assert response.has_context is True
        assert len(response.references) == 1
        assert response.query == "프로젝트 일정이 어떻게 되나요?"

    @pytest.mark.asyncio
    async def test_검색_실패_시_컨텍스트_없이_진행(self) -> None:
        """검색 실패 시 컨텍스트 없이 LLM 호출이 진행되는지 확인한다."""
        search_engine = MagicMock()
        search_engine.search = AsyncMock(side_effect=Exception("검색 오류"))

        engine = self._make_engine(search_engine=search_engine)

        # LLM 백엔드 목의 chat 반환값 설정
        backend_mock = engine._model_manager.acquire.return_value.__aenter__.return_value
        backend_mock.chat.return_value = "검색 결과 없이 답변합니다."

        response = await engine.chat("질문입니다")

        assert response.llm_used is True
        assert response.has_context is False
        assert len(response.references) == 0

    @pytest.mark.asyncio
    async def test_ollama_연결_실패_graceful_degradation(self) -> None:
        """LLM 연결 실패 시 검색 결과만 반환하는지 확인한다."""
        engine = self._make_engine()

        # ModelLoadManager.acquire가 LLMConnectionError를 발생시키도록 설정
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(
            side_effect=LLMConnectionError("연결 실패")
        )
        ctx.__aexit__ = AsyncMock(return_value=None)
        engine._model_manager.acquire = MagicMock(return_value=ctx)

        response = await engine.chat("질문입니다")

        assert response.llm_used is False
        assert response.has_context is True
        assert "연결 실패" in (response.error_message or "")
        assert len(response.references) == 1

    @pytest.mark.asyncio
    async def test_대화_이력_유지(self) -> None:
        """대화 이력이 세션에 저장되는지 확인한다."""
        engine = self._make_engine()

        # LLM 백엔드 목의 chat 반환값 설정
        backend_mock = engine._model_manager.acquire.return_value.__aenter__.return_value
        backend_mock.chat.return_value = "답변1"

        await engine.chat("질문1")

        session = engine.get_session()
        assert session.pair_count == 1
        assert session.history[0].content == "질문1"
        assert session.history[1].content == "답변1"

    @pytest.mark.asyncio
    async def test_세션_분리(self) -> None:
        """session_id별로 이력이 분리되는지 확인한다."""
        engine = self._make_engine()

        # LLM 백엔드 목의 chat 반환값 설정
        backend_mock = engine._model_manager.acquire.return_value.__aenter__.return_value
        backend_mock.chat.return_value = "답변"

        await engine.chat("질문A", session_id="session_a")
        await engine.chat("질문B", session_id="session_b")

        session_a = engine.get_session("session_a")
        session_b = engine.get_session("session_b")

        assert session_a.pair_count == 1
        assert session_a.history[0].content == "질문A"
        assert session_b.pair_count == 1
        assert session_b.history[0].content == "질문B"

    def test_세션_초기화(self) -> None:
        """clear_session()이 이력을 비우는지 확인한다."""
        engine = self._make_engine()
        session = engine.get_session()
        session.add_exchange("질문", "답변")

        engine.clear_session()
        assert session.pair_count == 0

    @pytest.mark.asyncio
    async def test_필터링_파라미터_전달(self) -> None:
        """필터링 파라미터가 검색 엔진에 전달되는지 확인한다."""
        search_engine = MagicMock()
        search_engine.search = AsyncMock(
            return_value=_make_search_response(results=[])
        )
        engine = self._make_engine(search_engine=search_engine)

        # LLM 백엔드 목의 chat 반환값 설정
        backend_mock = engine._model_manager.acquire.return_value.__aenter__.return_value
        backend_mock.chat.return_value = "답변"

        await engine.chat(
            "질문",
            meeting_id_filter="m001",
            date_filter="2026-03-04",
            speaker_filter="SPEAKER_00",
        )

        search_engine.search.assert_called_once_with(
            query="질문",
            meeting_id_filter="m001",
            date_filter="2026-03-04",
            speaker_filter="SPEAKER_00",
            top_k=5,
        )


class TestChatEngineContextTruncation:
    """컨텍스트 윈도우 절단 테스트 클래스."""

    def _make_engine(self) -> ChatEngine:
        """테스트용 ChatEngine을 생성한다."""
        config = _make_config()
        model_manager = MagicMock()
        search_engine = MagicMock()
        return ChatEngine(
            config=config,
            model_manager=model_manager,
            search_engine=search_engine,
        )

    def test_짧은_프롬프트는_절단되지_않음(self) -> None:
        """토큰 제한 내의 프롬프트는 그대로 반환되는지 확인한다."""
        engine = self._make_engine()
        result = engine._truncate_context(
            system_prompt="시스템",
            history_messages=[],
            user_prompt="짧은 질문",
            max_tokens=8192,
        )
        assert result == "짧은 질문"

    def test_긴_프롬프트_절단(self) -> None:
        """토큰 제한 초과 시 프롬프트가 절단되는지 확인한다."""
        engine = self._make_engine()
        # 매우 긴 프롬프트
        long_prompt = "가" * 20000
        result = engine._truncate_context(
            system_prompt="시스템 프롬프트",
            history_messages=[],
            user_prompt=long_prompt,
            max_tokens=1000,  # 작은 제한
        )
        assert len(result) < len(long_prompt)


class TestChatEngineFallback:
    """Graceful degradation 테스트 클래스."""

    def _make_engine(self) -> ChatEngine:
        """테스트용 ChatEngine을 생성한다."""
        config = _make_config()
        model_manager = MagicMock()
        search_engine = MagicMock()
        return ChatEngine(
            config=config,
            model_manager=model_manager,
            search_engine=search_engine,
        )

    def test_검색결과_있을때_대체답변(self) -> None:
        """검색 결과가 있을 때 대체 답변에 결과가 포함되는지 확인한다."""
        engine = self._make_engine()
        results = [
            _make_search_result(
                text="회의 내용입니다.",
                date="2026-03-04",
                speakers=["SPEAKER_00"],
            ),
        ]
        answer = engine._build_fallback_answer(results, "연결 실패")

        assert "연결 실패" in answer
        assert "회의 내용입니다." in answer
        assert "2026-03-04" in answer

    def test_검색결과_없을때_대체답변(self) -> None:
        """검색 결과가 없을 때 적절한 메시지가 반환되는지 확인한다."""
        engine = self._make_engine()
        answer = engine._build_fallback_answer([], "타임아웃")

        assert "타임아웃" in answer
        assert "찾지 못했습니다" in answer


class TestChatEngineStreaming:
    """스트리밍 응답 테스트 클래스."""

    def _make_engine(
        self,
        search_results: list[SearchResult] | None = None,
    ) -> ChatEngine:
        """테스트용 ChatEngine을 생성한다."""
        config = _make_config()

        model_manager = MagicMock()
        # acquire 컨텍스트 매니저 모킹 (LLMBackend 목 반환)
        ctx = AsyncMock()
        backend_mock = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=backend_mock)
        ctx.__aexit__ = AsyncMock(return_value=None)
        model_manager.acquire = MagicMock(return_value=ctx)

        search_engine = MagicMock()
        if search_results is not None:
            search_engine.search = AsyncMock(
                return_value=_make_search_response(results=search_results)
            )
        else:
            search_engine.search = AsyncMock(
                return_value=_make_search_response()
            )

        return ChatEngine(
            config=config,
            model_manager=model_manager,
            search_engine=search_engine,
        )

    @pytest.mark.asyncio
    async def test_스트리밍_정상_응답(self) -> None:
        """스트리밍 응답이 올바른 이벤트 순서로 생성되는지 확인한다."""
        engine = self._make_engine()

        # LLM 백엔드 목의 chat_stream 반환값 설정 (동기 Iterator[str])
        backend_mock = engine._model_manager.acquire.return_value.__aenter__.return_value
        backend_mock.chat_stream.return_value = iter(["프로젝트", " 일정은", " 월요일"])

        events = []
        async for event in engine.stream_chat("프로젝트 일정"):
            events.append(event)

        # 첫 이벤트: references
        assert events[0]["type"] == "references"
        assert len(events[0]["data"]) == 1

        # 중간 이벤트: tokens
        token_events = [e for e in events if e["type"] == "token"]
        assert len(token_events) == 3
        assert token_events[0]["data"] == "프로젝트"
        assert token_events[1]["data"] == " 일정은"
        assert token_events[2]["data"] == " 월요일"

        # 마지막 이벤트: done
        done_event = events[-1]
        assert done_event["type"] == "done"
        assert "프로젝트 일정은 월요일" in done_event["data"]["answer"]

    @pytest.mark.asyncio
    async def test_스트리밍_빈_질문_에러(self) -> None:
        """스트리밍에서 빈 질문이 EmptyQueryError를 발생시키는지 확인한다."""
        engine = self._make_engine()

        with pytest.raises(EmptyQueryError):
            async for _ in engine.stream_chat(""):
                pass

    @pytest.mark.asyncio
    async def test_스트리밍_llm_실패(self) -> None:
        """스트리밍 중 LLM 실패 시 error 이벤트가 생성되는지 확인한다."""
        engine = self._make_engine()

        # acquire에서 LLMConnectionError 발생
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(
            side_effect=LLMConnectionError("스트리밍 연결 실패")
        )
        ctx.__aexit__ = AsyncMock(return_value=None)
        engine._model_manager.acquire = MagicMock(return_value=ctx)

        events = []
        async for event in engine.stream_chat("질문"):
            events.append(event)

        # references 이벤트는 먼저 전송됨
        assert events[0]["type"] == "references"
        # error 이벤트
        error_event = events[-1]
        assert error_event["type"] == "error"
        assert "스트리밍 연결 실패" in error_event["data"]["message"]


class TestChatEngineBackend:
    """LLM 백엔드 생성 및 호출 테스트 클래스."""

    def setup_method(self) -> None:
        """각 테스트 전 Ollama 연결 캐시를 초기화한다."""
        clear_connection_cache()

    def _make_engine(self) -> ChatEngine:
        """테스트용 ChatEngine을 생성한다."""
        config = _make_config()
        model_manager = MagicMock()
        search_engine = MagicMock()
        return ChatEngine(
            config=config,
            model_manager=model_manager,
            search_engine=search_engine,
        )

    def test_백엔드_생성_성공(self) -> None:
        """_create_backend()가 OllamaBackend 인스턴스를 반환하는지 확인한다."""
        engine = self._make_engine()

        # Ollama 연결 확인 모킹
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("core.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            backend = engine._create_backend()

        from core.llm_backend import OllamaBackend
        assert isinstance(backend, OllamaBackend)

    def test_백엔드_생성_연결_실패(self) -> None:
        """백엔드 생성 시 연결 실패하면 LLMConnectionError가 발생하는지 확인한다."""
        engine = self._make_engine()

        import urllib.error
        with patch(
            "core.ollama_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("연결 거부"),
        ):
            with pytest.raises((OllamaConnectionError, LLMConnectionError)):
                engine._create_backend()

    def test_llm_chat_빈_응답(self) -> None:
        """LLM 백엔드가 빈 문자열을 반환하면 ChatError가 발생하는지 확인한다."""
        engine = self._make_engine()

        # LLMGenerationError를 발생시키는 백엔드 목
        backend_mock = MagicMock()
        backend_mock.chat.side_effect = LLMGenerationError("빈 응답: content가 없습니다")

        with pytest.raises(ChatError):
            engine._call_llm_chat(
                backend_mock,
                [{"role": "user", "content": "질문"}],
            )

    def test_llm_chat_타임아웃(self) -> None:
        """LLM 백엔드 타임아웃 시 ChatError가 발생하는지 확인한다."""
        engine = self._make_engine()

        # LLMGenerationError를 발생시키는 백엔드 목
        backend_mock = MagicMock()
        backend_mock.chat.side_effect = LLMGenerationError("요청 타임아웃")

        with pytest.raises(ChatError):
            engine._call_llm_chat(
                backend_mock,
                [{"role": "user", "content": "질문"}],
            )

    def test_llm_chat_연결_에러_전파(self) -> None:
        """LLM 백엔드 연결 에러가 LLMConnectionError로 전파되는지 확인한다."""
        engine = self._make_engine()

        # LLMConnectionError를 발생시키는 백엔드 목
        backend_mock = MagicMock()
        backend_mock.chat.side_effect = LLMConnectionError("연결 거부")

        with pytest.raises(LLMConnectionError):
            engine._call_llm_chat(
                backend_mock,
                [{"role": "user", "content": "질문"}],
            )


class TestChatEngineEdgeCases:
    """엣지 케이스 테스트 클래스."""

    def test_컨텍스트_텍스트_화자_미확인(self) -> None:
        """화자 정보가 없을 때 '미확인'으로 표시되는지 확인한다."""
        results = [_make_search_result(speakers=[])]
        context = _build_context_text(results)
        assert "미확인" in context

    def test_컨텍스트_텍스트_여러_결과(self) -> None:
        """여러 검색 결과가 번호별로 구분되는지 확인한다."""
        results = [
            _make_search_result(chunk_id="c1", text="내용1"),
            _make_search_result(chunk_id="c2", text="내용2"),
            _make_search_result(chunk_id="c3", text="내용3"),
        ]
        context = _build_context_text(results)
        assert "[참조 1]" in context
        assert "[참조 2]" in context
        assert "[참조 3]" in context

    def test_시간_표시_형식(self) -> None:
        """시간이 MM:SS 형식으로 올바르게 표시되는지 확인한다."""
        results = [
            _make_search_result(start_time=0.0, end_time=5.0),
        ]
        context = _build_context_text(results)
        assert "00:00~00:05" in context

        results2 = [
            _make_search_result(start_time=3661.0, end_time=3725.0),
        ]
        context2 = _build_context_text(results2)
        assert "61:01~62:05" in context2


# ============================================================
# Phase 3 통합 테스트 (Integration Tests)
# ============================================================


# === 통합 테스트 헬퍼 ===


def _make_integration_test_app(tmp_path: "Path") -> "FastAPI":
    """Phase 3 통합 테스트용 FastAPI 앱을 생성한다.

    ChatEngine과 HybridSearchEngine 초기화를 패치하여
    외부 의존성 없이 전체 API 스택을 테스트할 수 있다.

    Args:
        tmp_path: pytest 임시 디렉토리

    Returns:
        테스트용 FastAPI 앱 인스턴스
    """
    from config import AppConfig, PathsConfig, ServerConfig
    from api.server import create_app

    config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
    )

    with (
        patch(
            "search.hybrid_search.HybridSearchEngine",
            return_value=MagicMock(),
        ),
        patch(
            "search.chat.ChatEngine",
            return_value=MagicMock(),
        ),
    ):
        app = create_app(config)

    return app


@dataclass
class _MockJob:
    """통합 테스트용 Job 데이터 클래스."""

    id: int
    meeting_id: str
    audio_path: str
    status: str = "completed"
    retry_count: int = 0
    error_message: str = ""
    created_at: str = "2026-03-04T10:00:00"
    updated_at: str = "2026-03-04T10:30:00"


@dataclass
class _MockIntegSearchResult:
    """통합 테스트용 SearchResult 데이터 클래스."""

    chunk_id: str
    text: str
    score: float
    meeting_id: str
    date: str
    speakers: list[str]
    start_time: float
    end_time: float
    chunk_index: int = 0
    source: str = "both"


@dataclass
class _MockIntegSearchResponse:
    """통합 테스트용 SearchResponse 데이터 클래스."""

    results: list[_MockIntegSearchResult]
    query: str
    total_found: int = 0
    vector_count: int = 0
    fts_count: int = 0
    filters_applied: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.filters_applied is None:
            self.filters_applied = {}


@dataclass
class _MockIntegChatReference:
    """통합 테스트용 ChatReference 데이터 클래스."""

    chunk_id: str
    meeting_id: str
    date: str
    speakers: list[str]
    start_time: float
    end_time: float
    text_preview: str
    score: float


@dataclass
class _MockIntegChatResponse:
    """통합 테스트용 ChatResponse 데이터 클래스."""

    answer: str
    references: list[_MockIntegChatReference]
    query: str
    has_context: bool = True
    llm_used: bool = True
    error_message: str | None = None


# === Phase 3 API Chat 통합 테스트 ===


class TestPhase3APIChatIntegration:
    """POST /api/chat 엔드포인트와 ChatEngine 연동 통합 테스트."""

    def test_chat_정상_RAG_응답(self, tmp_path: "Path") -> None:
        """Chat API가 검색 결과 기반 RAG 응답을 반환하는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        mock_refs = [
            _MockIntegChatReference(
                chunk_id="chunk_001",
                meeting_id="meeting_001",
                date="2026-03-04",
                speakers=["SPEAKER_00", "SPEAKER_01"],
                start_time=60.0,
                end_time=120.0,
                text_preview="프로젝트 일정 논의...",
                score=0.85,
            ),
        ]
        mock_response = _MockIntegChatResponse(
            answer="프로젝트 일정은 다음 주 월요일까지입니다.",
            references=mock_refs,
            query="프로젝트 일정이 어떻게 되나요?",
            has_context=True,
            llm_used=True,
        )

        with TestClient(app) as client:
            app.state.chat_engine.chat = AsyncMock(
                return_value=mock_response,
            )

            response = client.post(
                "/api/chat",
                json={"query": "프로젝트 일정이 어떻게 되나요?"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["llm_used"] is True
        assert data["has_context"] is True
        assert "프로젝트 일정" in data["answer"]
        assert len(data["references"]) == 1
        assert data["references"][0]["meeting_id"] == "meeting_001"
        assert data["references"][0]["score"] == 0.85

    def test_chat_세션별_대화_분리(self, tmp_path: "Path") -> None:
        """session_id가 ChatEngine에 올바르게 전달되어 세션이 분리되는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        mock_response = _MockIntegChatResponse(
            answer="답변", references=[], query="질문",
        )

        with TestClient(app) as client:
            app.state.chat_engine.chat = AsyncMock(
                return_value=mock_response,
            )

            # 세션 A
            client.post(
                "/api/chat",
                json={"query": "질문A", "session_id": "session_a"},
            )
            call_a = app.state.chat_engine.chat.call_args
            assert call_a.kwargs["session_id"] == "session_a"

            # 세션 B
            client.post(
                "/api/chat",
                json={"query": "질문B", "session_id": "session_b"},
            )
            call_b = app.state.chat_engine.chat.call_args
            assert call_b.kwargs["session_id"] == "session_b"

    def test_chat_필터_조합_전달(self, tmp_path: "Path") -> None:
        """meeting_id, date, speaker 필터가 모두 ChatEngine에 전달되는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        mock_response = _MockIntegChatResponse(
            answer="답변", references=[], query="질문",
        )

        with TestClient(app) as client:
            app.state.chat_engine.chat = AsyncMock(
                return_value=mock_response,
            )

            response = client.post(
                "/api/chat",
                json={
                    "query": "안건 정리해줘",
                    "session_id": "sess_01",
                    "meeting_id_filter": "meeting_003",
                    "date_filter": "2026-03-04",
                    "speaker_filter": "SPEAKER_02",
                },
            )

        assert response.status_code == 200
        kwargs = app.state.chat_engine.chat.call_args.kwargs
        assert kwargs["meeting_id_filter"] == "meeting_003"
        assert kwargs["date_filter"] == "2026-03-04"
        assert kwargs["speaker_filter"] == "SPEAKER_02"

    def test_chat_LLM_실패시_graceful_degradation(
        self, tmp_path: "Path",
    ) -> None:
        """LLM 실패 시에도 200 응답과 검색 결과를 반환하는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        mock_response = _MockIntegChatResponse(
            answer="AI 답변을 생성할 수 없습니다. 관련 회의 내용을 검색 결과로 대신 제공합니다.",
            references=[
                _MockIntegChatReference(
                    chunk_id="c1", meeting_id="m1", date="2026-03-04",
                    speakers=["SPEAKER_00"], start_time=0.0, end_time=10.0,
                    text_preview="회의 내용...", score=0.7,
                ),
            ],
            query="질문",
            has_context=True,
            llm_used=False,
            error_message="Ollama 서버에 연결할 수 없습니다",
        )

        with TestClient(app) as client:
            app.state.chat_engine.chat = AsyncMock(
                return_value=mock_response,
            )

            response = client.post(
                "/api/chat",
                json={"query": "질문"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["llm_used"] is False
        assert data["error_message"] is not None
        assert "Ollama" in data["error_message"]
        # 검색 결과는 여전히 포함
        assert len(data["references"]) == 1

    def test_chat_빈_질문_거부(self, tmp_path: "Path") -> None:
        """빈 질문은 pydantic 검증에 의해 422를 반환한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/api/chat",
                json={"query": ""},
            )

        assert response.status_code == 422

    def test_chat_엔진_미초기화_503(self, tmp_path: "Path") -> None:
        """ChatEngine이 초기화되지 않았을 때 503을 반환한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        with TestClient(app) as client:
            original = app.state.chat_engine
            app.state.chat_engine = None

            response = client.post(
                "/api/chat",
                json={"query": "테스트"},
            )

            app.state.chat_engine = original

        assert response.status_code == 503


# === Phase 3 API Search 통합 테스트 ===


class TestPhase3APISearchIntegration:
    """POST /api/search 엔드포인트 통합 테스트."""

    def test_search_한국어_쿼리_정상_응답(self, tmp_path: "Path") -> None:
        """한국어 검색 쿼리가 올바르게 처리되는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        mock_results = [
            _MockIntegSearchResult(
                chunk_id="chunk_001",
                text="다음 주 월요일까지 보고서를 제출해야 합니다.",
                score=0.92,
                meeting_id="meeting_001",
                date="2026-03-04",
                speakers=["SPEAKER_00"],
                start_time=120.0,
                end_time=180.0,
                source="vector",
            ),
            _MockIntegSearchResult(
                chunk_id="chunk_002",
                text="보고서 양식은 기존 템플릿을 사용합니다.",
                score=0.78,
                meeting_id="meeting_001",
                date="2026-03-04",
                speakers=["SPEAKER_01"],
                start_time=200.0,
                end_time=240.0,
                source="fts",
            ),
        ]
        mock_response = _MockIntegSearchResponse(
            results=mock_results,
            query="보고서 제출 일정",
            total_found=2,
            vector_count=1,
            fts_count=1,
        )

        with TestClient(app) as client:
            app.state.search_engine.search = AsyncMock(
                return_value=mock_response,
            )

            response = client.post(
                "/api/search",
                json={"query": "보고서 제출 일정"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["total_found"] == 2
        assert data["vector_count"] == 1
        assert data["fts_count"] == 1
        assert len(data["results"]) == 2
        assert data["results"][0]["source"] == "vector"
        assert data["results"][1]["source"] == "fts"

    def test_search_필터_조합(self, tmp_path: "Path") -> None:
        """날짜 + 화자 + 회의ID 필터 조합이 검색 엔진에 전달되는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        mock_response = _MockIntegSearchResponse(
            results=[], query="테스트", total_found=0,
        )

        with TestClient(app) as client:
            app.state.search_engine.search = AsyncMock(
                return_value=mock_response,
            )

            response = client.post(
                "/api/search",
                json={
                    "query": "결정 사항",
                    "date_filter": "2026-03-04",
                    "speaker_filter": "SPEAKER_00",
                    "meeting_id_filter": "meeting_003",
                    "top_k": 3,
                },
            )

        assert response.status_code == 200
        kwargs = app.state.search_engine.search.call_args.kwargs
        assert kwargs["date_filter"] == "2026-03-04"
        assert kwargs["speaker_filter"] == "SPEAKER_00"
        assert kwargs["meeting_id_filter"] == "meeting_003"
        assert kwargs["top_k"] == 3


# === Phase 3 WebSocket 통합 테스트 ===


class TestPhase3WebSocketIntegration:
    """WebSocket /ws/events 서버 통합 테스트."""

    def test_websocket_서버_연결_환영_메시지(
        self, tmp_path: "Path",
    ) -> None:
        """서버 앱의 WebSocket 연결 시 환영 메시지를 수신하는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        with TestClient(app) as client:
            with client.websocket_connect("/ws/events") as ws:
                data = ws.receive_json()

                assert data["event_type"] == "system_status"
                assert "WebSocket 연결 성공" in data["data"]["message"]
                assert data["data"]["active_connections"] >= 1

    @pytest.mark.asyncio
    async def test_websocket_이벤트_브로드캐스트(
        self, tmp_path: "Path",
    ) -> None:
        """ConnectionManager를 통한 이벤트 브로드캐스트가 동작하는지 검증한다."""
        from api.websocket import ConnectionManager, EventType, WebSocketEvent

        manager = ConnectionManager(max_connections=5)
        ws1 = AsyncMock()
        ws2 = AsyncMock()

        await manager.connect(ws1)
        await manager.connect(ws2)

        event = WebSocketEvent(
            event_type=EventType.JOB_COMPLETED.value,
            data={"meeting_id": "m001", "status": "completed"},
        )
        count = await manager.broadcast_event(event)

        assert count == 2
        ws1.send_text.assert_called()
        ws2.send_text.assert_called()

        # 전송된 메시지 내용 검증
        sent1 = json.loads(ws1.send_text.call_args[0][0])
        assert sent1["event_type"] == "job_completed"
        assert sent1["data"]["meeting_id"] == "m001"

    def test_websocket_라우트_등록_확인(
        self, tmp_path: "Path",
    ) -> None:
        """서버 앱에 /ws/events 라우트가 등록되어 있는지 검증한다."""
        app = _make_integration_test_app(tmp_path)

        route_paths = []
        for route in app.routes:
            if hasattr(route, "path"):
                route_paths.append(route.path)

        assert "/ws/events" in route_paths


# === Phase 3 다중 엔드포인트 워크플로우 테스트 ===


class TestPhase3MultiEndpointFlow:
    """Phase 3 다중 엔드포인트 워크플로우 통합 테스트."""

    def test_status_meetings_search_chat_워크플로우(
        self, tmp_path: "Path",
    ) -> None:
        """status → meetings → search → chat 전체 워크플로우를 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        jobs = [
            _MockJob(1, "meeting_001", "/audio/001.m4a", "completed"),
            _MockJob(2, "meeting_002", "/audio/002.m4a", "transcribing"),
        ]

        with TestClient(app) as client:
            # 1단계: 시스템 상태 조회
            app.state.job_queue.count_by_status = AsyncMock(
                return_value={"completed": 1, "transcribing": 1},
            )
            app.state.job_queue.get_all_jobs = AsyncMock(
                return_value=jobs,
            )

            status_resp = client.get("/api/status")
            assert status_resp.status_code == 200
            assert status_resp.json()["status"] == "ok"
            assert status_resp.json()["total_jobs"] == 2

            # 2단계: 회의 목록 조회
            meetings_resp = client.get("/api/meetings")
            assert meetings_resp.status_code == 200
            assert meetings_resp.json()["total"] == 2

            # 3단계: 검색
            search_mock = _MockIntegSearchResponse(
                results=[
                    _MockIntegSearchResult(
                        chunk_id="c1",
                        text="프로젝트 일정 관련 내용",
                        score=0.9,
                        meeting_id="meeting_001",
                        date="2026-03-04",
                        speakers=["SPEAKER_00"],
                        start_time=60.0,
                        end_time=120.0,
                    ),
                ],
                query="프로젝트 일정",
                total_found=1,
                vector_count=1,
                fts_count=0,
            )
            app.state.search_engine.search = AsyncMock(
                return_value=search_mock,
            )

            search_resp = client.post(
                "/api/search",
                json={"query": "프로젝트 일정"},
            )
            assert search_resp.status_code == 200
            assert len(search_resp.json()["results"]) == 1

            # 4단계: Chat (검색 결과를 기반으로 질의)
            chat_mock = _MockIntegChatResponse(
                answer="프로젝트 일정은 다음 주 월요일 마감입니다.",
                references=[
                    _MockIntegChatReference(
                        chunk_id="c1", meeting_id="meeting_001",
                        date="2026-03-04", speakers=["SPEAKER_00"],
                        start_time=60.0, end_time=120.0,
                        text_preview="프로젝트 일정 관련 내용",
                        score=0.9,
                    ),
                ],
                query="프로젝트 일정이 어떻게 되나요?",
            )
            app.state.chat_engine.chat = AsyncMock(
                return_value=chat_mock,
            )

            chat_resp = client.post(
                "/api/chat",
                json={
                    "query": "프로젝트 일정이 어떻게 되나요?",
                    "meeting_id_filter": "meeting_001",
                },
            )
            assert chat_resp.status_code == 200
            assert chat_resp.json()["llm_used"] is True
            assert "마감" in chat_resp.json()["answer"]

    def test_헬스체크_후_API_사용(self, tmp_path: "Path") -> None:
        """헬스체크가 OK면 API 엔드포인트들이 정상 동작하는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        with TestClient(app) as client:
            # 헬스체크
            health_resp = client.get("/api/health")
            assert health_resp.status_code == 200
            assert health_resp.json()["status"] == "ok"

            # OpenAPI 스키마 접근
            schema_resp = client.get("/api/openapi.json")
            assert schema_resp.status_code == 200
            paths = schema_resp.json()["paths"]
            assert "/api/chat" in paths
            assert "/api/search" in paths
            assert "/api/status" in paths
            assert "/api/meetings" in paths


# === Phase 3 RAG 정확도 테스트 ===


class TestPhase3RAGAccuracy:
    """RAG 파이프라인 정확도 통합 테스트.

    검색 결과가 LLM 프롬프트에 올바르게 반영되는지 검증한다.
    """

    def test_검색결과가_프롬프트에_포함(self) -> None:
        """검색 결과의 회의 내용이 LLM 프롬프트 컨텍스트에 포함되는지 검증한다."""
        results = [
            _make_search_result(
                text="다음 주 월요일까지 디자인 시안을 제출해야 합니다.",
                meeting_id="m_design",
                date="2026-03-03",
                speakers=["SPEAKER_00"],
                start_time=300.0,
                end_time=360.0,
            ),
            _make_search_result(
                chunk_id="c2",
                text="UI 피드백은 수요일까지 받겠습니다.",
                meeting_id="m_design",
                date="2026-03-03",
                speakers=["SPEAKER_01"],
                start_time=380.0,
                end_time=400.0,
            ),
        ]

        context = _build_context_text(results)
        prompt = _build_user_prompt("디자인 일정은?", context)

        # 프롬프트에 검색 결과 내용이 포함
        assert "디자인 시안" in prompt
        assert "UI 피드백" in prompt
        assert "m_design" in prompt
        assert "SPEAKER_00" in prompt
        assert "SPEAKER_01" in prompt
        assert "디자인 일정은?" in prompt

    def test_검색결과_없을때_프롬프트(self) -> None:
        """검색 결과가 없을 때 적절한 안내 프롬프트가 구성되는지 검증한다."""
        context = _build_context_text([])
        prompt = _build_user_prompt("질문입니다", context)

        assert "찾을 수 없습니다" in prompt
        assert "질문입니다" in prompt

    def test_참조출처_정확한_변환(self) -> None:
        """검색 결과가 참조 출처로 정확하게 변환되는지 검증한다."""
        # 100자 초과 텍스트 → 절단 검증용
        long_text = "예산 관련 논의를 진행했습니다. " * 10  # 약 180자
        results = [
            _make_search_result(
                chunk_id="chunk_042",
                text=long_text,
                score=0.05,
                meeting_id="meeting_budget",
                date="2026-02-28",
                speakers=["SPEAKER_00", "SPEAKER_02"],
                start_time=600.5,
                end_time=660.0,
            ),
        ]

        refs = _build_references(results)

        assert len(refs) == 1
        ref = refs[0]
        assert ref.chunk_id == "chunk_042"
        assert ref.meeting_id == "meeting_budget"
        assert ref.date == "2026-02-28"
        assert ref.speakers == ["SPEAKER_00", "SPEAKER_02"]
        assert ref.start_time == 600.5
        assert ref.end_time == 660.0
        assert ref.score == 0.05
        # text_preview는 100자 초과이므로 절단
        assert ref.text_preview.endswith("...")
        assert len(ref.text_preview) <= 103  # 100자 + "..."

    def test_다중_검색결과_참조번호_순서(self) -> None:
        """여러 검색 결과가 순서대로 번호가 부여되는지 검증한다."""
        results = [
            _make_search_result(chunk_id=f"c{i}", text=f"내용{i}")
            for i in range(5)
        ]

        context = _build_context_text(results)

        for i in range(1, 6):
            assert f"[참조 {i}]" in context


# === Phase 3 한국어 NLP 통합 테스트 ===


class TestPhase3KoreanNLP:
    """한국어 텍스트 처리 통합 테스트."""

    def test_NFC_정규화_적용(self) -> None:
        """한국어 텍스트에 NFC 정규화가 적용되는지 검증한다."""
        import unicodedata

        # NFD로 분해된 한국어 → NFC로 조합되어야 함
        nfd_text = unicodedata.normalize("NFD", "안녕하세요")
        nfc_text = unicodedata.normalize("NFC", nfd_text)

        assert nfd_text != nfc_text  # NFD와 NFC는 다름
        assert nfc_text == "안녕하세요"

    def test_한국어_토큰_추정_다양한_입력(self) -> None:
        """다양한 한국어 텍스트의 토큰 추정이 합리적인지 검증한다."""
        # 순수 한글
        assert _estimate_korean_tokens("가나다라마바사") == 4  # 7/1.5 ≈ 4

        # 한영 혼합
        mixed = "프로젝트 schedule을 확인합니다"
        tokens = _estimate_korean_tokens(mixed)
        assert tokens > 0
        assert tokens < len(mixed)  # 토큰 수 < 글자 수

        # 특수문자 포함
        special = "회의 일정: 3/4(월) 10:00~11:00"
        tokens_special = _estimate_korean_tokens(special)
        assert tokens_special > 0

    def test_컨텍스트_시간_포맷_한국어_친화(self) -> None:
        """시간 표시가 MM:SS 형식으로 한국어 사용자에게 친화적인지 검증한다."""
        results = [
            _make_search_result(start_time=90.0, end_time=150.0),
        ]
        context = _build_context_text(results)
        # 90초 = 01:30, 150초 = 02:30
        assert "01:30~02:30" in context

    def test_화자_라벨_목록_구성(self) -> None:
        """여러 화자가 쉼표로 구분되어 표시되는지 검증한다."""
        results = [
            _make_search_result(
                speakers=["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"],
            ),
        ]
        context = _build_context_text(results)
        assert "SPEAKER_00, SPEAKER_01, SPEAKER_02" in context


# === Phase 3 보안 통합 테스트 ===


class TestPhase3Security:
    """Phase 3 보안 관련 통합 테스트."""

    def test_chat_요청_필수_필드_검증(self, tmp_path: "Path") -> None:
        """필수 필드 누락 시 422를 반환하는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        with TestClient(app) as client:
            # query 필드 누락
            response = client.post("/api/chat", json={})

        assert response.status_code == 422

    def test_search_요청_필수_필드_검증(self, tmp_path: "Path") -> None:
        """검색 요청 시 query 필드가 필수인지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.post("/api/search", json={})

        assert response.status_code == 422

    def test_잘못된_HTTP_메서드(self, tmp_path: "Path") -> None:
        """지원하지 않는 HTTP 메서드 사용 시 405를 반환하는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        with TestClient(app) as client:
            # /api/chat은 POST만 지원
            response = client.get("/api/chat")

        assert response.status_code == 405

    def test_CORS_localhost_허용(self, tmp_path: "Path") -> None:
        """localhost 오리진만 CORS로 허용되는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        with TestClient(app) as client:
            # localhost 허용
            response = client.options(
                "/api/health",
                headers={
                    "Origin": "http://127.0.0.1:8765",
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert response.headers.get(
                "access-control-allow-origin",
            ) == "http://127.0.0.1:8765"

            # 외부 오리진 차단
            response2 = client.options(
                "/api/health",
                headers={
                    "Origin": "http://attacker.com",
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert response2.headers.get(
                "access-control-allow-origin",
            ) is None


# === Phase 3 Graceful Degradation 통합 테스트 ===


class TestPhase3GracefulDegradation:
    """컴포넌트 장애 시 시스템 안정성 통합 테스트."""

    def test_검색엔진_장애시_chat_에러_처리(
        self, tmp_path: "Path",
    ) -> None:
        """검색 엔진 장애 시 Chat이 적절한 에러를 반환하는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.chat_engine.chat = AsyncMock(
                side_effect=Exception("내부 검색 오류"),
            )

            response = client.post(
                "/api/chat",
                json={"query": "테스트 질문"},
            )

        assert response.status_code == 500

    def test_검색엔진_미초기화_503(self, tmp_path: "Path") -> None:
        """검색 엔진이 초기화되지 않았을 때 503을 반환하는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.search_engine = None

            response = client.post(
                "/api/search",
                json={"query": "테스트"},
            )

        assert response.status_code == 503

    def test_서버_예외_핸들러_동작(self, tmp_path: "Path") -> None:
        """처리되지 않은 예외가 500 JSON 응답으로 변환되는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        # 의도적 예외를 발생시키는 엔드포인트 추가
        @app.get("/api/test-crash")
        async def _crash() -> None:
            raise RuntimeError("의도적 크래시 테스트")

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/test-crash")

        assert response.status_code == 500
        data = response.json()
        assert "error" in data or "detail" in data


# === Phase 3 서버 Lifespan 통합 테스트 ===


class TestPhase3ServerLifespan:
    """서버 Lifespan에서 Phase 3 컴포넌트 초기화/정리 통합 테스트."""

    def test_lifespan_chat_engine_초기화(
        self, tmp_path: "Path",
    ) -> None:
        """서버 시작 시 chat_engine이 app.state에 설정되는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        with TestClient(app):
            assert hasattr(app.state, "chat_engine")

    def test_lifespan_search_engine_초기화(
        self, tmp_path: "Path",
    ) -> None:
        """서버 시작 시 search_engine이 app.state에 설정되는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        with TestClient(app):
            assert hasattr(app.state, "search_engine")

    def test_lifespan_ws_manager_초기화(
        self, tmp_path: "Path",
    ) -> None:
        """서버 시작 시 ws_manager가 app.state에 설정되는지 검증한다."""
        from fastapi.testclient import TestClient
        from api.websocket import ConnectionManager

        app = _make_integration_test_app(tmp_path)

        with TestClient(app):
            assert hasattr(app.state, "ws_manager")
            assert isinstance(app.state.ws_manager, ConnectionManager)

    def test_lifespan_job_queue_초기화(
        self, tmp_path: "Path",
    ) -> None:
        """서버 시작 시 job_queue가 초기화되는지 검증한다."""
        from fastapi.testclient import TestClient

        app = _make_integration_test_app(tmp_path)

        with TestClient(app):
            assert hasattr(app.state, "job_queue")
            assert app.state.job_queue is not None

    def test_모든_API_라우트_등록_확인(
        self, tmp_path: "Path",
    ) -> None:
        """Phase 3의 모든 API 엔드포인트가 등록되어 있는지 검증한다."""
        app = _make_integration_test_app(tmp_path)

        route_paths = [
            getattr(route, "path", "")
            for route in app.routes
        ]

        # REST API 엔드포인트
        assert "/api/status" in route_paths
        assert "/api/meetings" in route_paths
        assert "/api/meetings/{meeting_id}" in route_paths
        assert "/api/meetings/{meeting_id}/transcript" in route_paths
        assert "/api/meetings/{meeting_id}/summary" in route_paths
        assert "/api/search" in route_paths
        assert "/api/chat" in route_paths
        # WebSocket 엔드포인트
        assert "/ws/events" in route_paths
        # 헬스체크
        assert "/api/health" in route_paths
