"""
RAG 기반 AI Chat 엔진 모듈 (RAG-based AI Chat Engine Module)

목적: 하이브리드 검색 결과를 컨텍스트로 활용하여 EXAONE LLM에
      회의 내용 기반 질의응답을 수행한다.
주요 기능:
    - 하이브리드 검색(벡터 + FTS5) → 상위 5청크 컨텍스트 구성
    - LLM 백엔드(Ollama/MLX)를 통한 RAG 응답 생성
    - 대화 이력 슬라이딩 윈도우 (최근 3쌍 유지)
    - 참조 출처(회의 ID, 화자, 시간) 표시
    - 스트리밍 응답 지원 (stream=True)
    - LLM 실패 시 검색 결과만 반환 (graceful degradation)
    - 비동기(async) 인터페이스 지원
의존성: config 모듈, search/hybrid_search 모듈, core/model_manager 모듈, core/llm_backend 모듈
"""

from __future__ import annotations

import asyncio
import logging
import queue
import unicodedata
from collections.abc import AsyncGenerator
from dataclasses import asdict, dataclass
from typing import Any

from config import AppConfig, ChatConfig, get_config
from core.llm_backend import (
    LLMBackend,
    LLMConnectionError,
    LLMGenerationError,
    create_backend,
)
from core.model_manager import ModelLoadManager, get_model_manager
from core.user_settings import build_chat_system_prompt
from search.hybrid_search import (
    HybridSearchEngine,
    SearchResponse,
    SearchResult,
)

logger = logging.getLogger(__name__)


# === 에러 계층 ===


class ChatError(Exception):
    """Chat 처리 중 발생하는 에러의 기본 클래스."""


class EmptyQueryError(ChatError):
    """질문이 비어있을 때 발생한다."""


# === 데이터 클래스 ===


@dataclass
class ChatMessage:
    """단일 대화 메시지를 나타내는 데이터 클래스.

    Attributes:
        role: 메시지 역할 ("user" 또는 "assistant")
        content: 메시지 내용
    """

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        """딕셔너리로 변환한다 (JSON 직렬화용).

        Returns:
            메시지 딕셔너리
        """
        return {"role": self.role, "content": self.content}


@dataclass
class ChatReference:
    """응답의 참조 출처를 나타내는 데이터 클래스.

    Attributes:
        chunk_id: 청크 고유 식별자
        meeting_id: 회의 식별자
        date: 회의 날짜
        speakers: 화자 목록
        start_time: 시작 시간 (초)
        end_time: 종료 시간 (초)
        text_preview: 청크 텍스트 미리보기 (첫 100자)
        score: 검색 관련도 점수
    """

    chunk_id: str
    meeting_id: str
    date: str
    speakers: list[str]
    start_time: float
    end_time: float
    text_preview: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화용).

        Returns:
            참조 출처 딕셔너리
        """
        return asdict(self)


@dataclass
class ChatResponse:
    """AI Chat 응답을 담는 데이터 클래스.

    Attributes:
        answer: LLM이 생성한 답변 텍스트
        references: 참조 출처 목록 (검색에 사용된 청크들)
        query: 원본 질문
        has_context: 검색 컨텍스트가 있었는지 여부
        llm_used: LLM 응답이 성공했는지 여부
        error_message: 에러 발생 시 메시지 (없으면 None)
    """

    answer: str
    references: list[ChatReference]
    query: str
    has_context: bool = True
    llm_used: bool = True
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화용).

        Returns:
            Chat 응답 딕셔너리
        """
        return {
            "answer": self.answer,
            "references": [r.to_dict() for r in self.references],
            "query": self.query,
            "has_context": self.has_context,
            "llm_used": self.llm_used,
            "error_message": self.error_message,
        }


# === 대화 세션 관리 ===


class ChatSession:
    """대화 이력을 관리하는 세션 클래스.

    슬라이딩 윈도우 방식으로 최근 N쌍의 대화만 유지한다.
    user 질문과 assistant 답변이 하나의 쌍을 구성한다.

    Args:
        max_pairs: 유지할 최대 대화 쌍 수 (기본 3)
    """

    def __init__(self, max_pairs: int = 3) -> None:
        """ChatSession을 초기화한다.

        Args:
            max_pairs: 유지할 최대 대화 쌍 수
        """
        self._max_pairs = max_pairs
        self._history: list[ChatMessage] = []

    @property
    def history(self) -> list[ChatMessage]:
        """현재 대화 이력을 반환한다."""
        return list(self._history)

    @property
    def pair_count(self) -> int:
        """현재 유지 중인 대화 쌍 수."""
        return len(self._history) // 2

    def add_exchange(self, user_query: str, assistant_answer: str) -> None:
        """사용자 질문과 AI 답변 쌍을 이력에 추가한다.

        최대 쌍 수를 초과하면 가장 오래된 쌍부터 제거한다.

        Args:
            user_query: 사용자 질문
            assistant_answer: AI 답변
        """
        self._history.append(ChatMessage(role="user", content=user_query))
        self._history.append(ChatMessage(role="assistant", content=assistant_answer))

        # 슬라이딩 윈도우: 최대 쌍 수 초과 시 가장 오래된 쌍 제거
        max_messages = self._max_pairs * 2
        if len(self._history) > max_messages:
            self._history = self._history[-max_messages:]

    def clear(self) -> None:
        """대화 이력을 초기화한다."""
        self._history.clear()

    def to_ollama_messages(self) -> list[dict[str, str]]:
        """대화 이력을 Ollama API 형식으로 변환한다.

        Returns:
            Ollama messages 형식의 딕셔너리 목록
        """
        return [msg.to_dict() for msg in self._history]


# === 컨텍스트 구성 ===


def _build_context_text(results: list[SearchResult]) -> str:
    """검색 결과를 LLM 컨텍스트 텍스트로 구성한다.

    각 검색 결과에 화자, 시간, 날짜 정보를 포함하여
    LLM이 출처를 파악할 수 있도록 한다.

    Args:
        results: 검색 결과 목록

    Returns:
        컨텍스트 텍스트 문자열
    """
    if not results:
        return ""

    context_parts: list[str] = []
    for i, result in enumerate(results, start=1):
        # 화자 정보 구성
        speakers_str = ", ".join(result.speakers) if result.speakers else "미확인"

        # 시간 정보 구성 (분:초 형식)
        start_min = int(result.start_time // 60)
        start_sec = int(result.start_time % 60)
        end_min = int(result.end_time // 60)
        end_sec = int(result.end_time % 60)
        time_str = f"{start_min:02d}:{start_sec:02d}~{end_min:02d}:{end_sec:02d}"

        context_parts.append(
            f"[참조 {i}] 회의: {result.meeting_id} | "
            f"날짜: {result.date} | "
            f"화자: {speakers_str} | "
            f"시간: {time_str}\n"
            f"{result.text}"
        )

    return "\n\n".join(context_parts)


def _build_user_prompt(query: str, context_text: str) -> str:
    """사용자 질문과 검색 컨텍스트를 결합한 프롬프트를 구성한다.

    Args:
        query: 사용자 질문
        context_text: 검색 결과 컨텍스트 텍스트

    Returns:
        LLM에 전달할 사용자 프롬프트
    """
    if context_text:
        return (
            f"다음은 관련 회의 내용입니다:\n\n"
            f"{context_text}\n\n"
            f"위 회의 내용을 참고하여 다음 질문에 답변해주세요:\n"
            f"{query}"
        )
    return (
        f"관련 회의 내용을 찾을 수 없습니다. "
        f"다음 질문에 대해 알고 있는 범위에서 답변해주세요:\n"
        f"{query}"
    )


def _build_references(results: list[SearchResult]) -> list[ChatReference]:
    """검색 결과를 ChatReference 목록으로 변환한다.

    Args:
        results: 검색 결과 목록

    Returns:
        참조 출처 목록
    """
    references: list[ChatReference] = []
    for result in results:
        # 텍스트 미리보기 (최대 100자)
        preview = result.text[:100] + "..." if len(result.text) > 100 else result.text

        references.append(
            ChatReference(
                chunk_id=result.chunk_id,
                meeting_id=result.meeting_id,
                date=result.date,
                speakers=result.speakers,
                start_time=result.start_time,
                end_time=result.end_time,
                text_preview=preview,
                score=result.score,
            )
        )
    return references


def _estimate_korean_tokens(text: str) -> int:
    """한국어 텍스트의 토큰 수를 근사 추정한다.

    한국어 1토큰 ≈ 1.5글자 기준으로 추정한다.
    정확한 토크나이저 없이 stdlib만으로 구현한다.

    Args:
        text: 토큰 수를 추정할 텍스트

    Returns:
        추정 토큰 수
    """
    if not text:
        return 0
    return max(1, int(len(text) / 1.5))


# === 메인 클래스 ===


class ChatEngine:
    """RAG 기반 AI Chat 엔진.

    하이브리드 검색(ChromaDB 벡터 + SQLite FTS5)으로 관련 회의 내용을
    검색한 후, EXAONE 3.5 LLM으로 질문에 대한 답변을 생성한다.

    대화 이력을 슬라이딩 윈도우로 유지하여 맥락 있는 대화를 지원하고,
    각 답변에 참조 출처(회의 ID, 화자, 시간)를 포함한다.

    Args:
        config: 애플리케이션 설정 (None이면 싱글턴 사용)
        model_manager: 모델 로드 매니저 (None이면 싱글턴 사용)
        search_engine: 하이브리드 검색 엔진 (None이면 자동 생성)

    사용 예시:
        engine = ChatEngine()
        response = await engine.chat("프로젝트 일정이 어떻게 되나요?")
        print(response.answer)
        for ref in response.references:
            print(f"  출처: {ref.meeting_id} ({ref.date})")
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        model_manager: ModelLoadManager | None = None,
        search_engine: HybridSearchEngine | None = None,
    ) -> None:
        """ChatEngine을 초기화한다.

        Args:
            config: 애플리케이션 설정 (None이면 get_config() 사용)
            model_manager: 모델 로드 매니저 (None이면 get_model_manager() 사용)
            search_engine: 하이브리드 검색 엔진 (None이면 자동 생성)
        """
        self._config = config or get_config()
        self._model_manager = model_manager or get_model_manager()
        self._search_engine = search_engine or HybridSearchEngine(
            config=self._config,
            model_manager=self._model_manager,
        )

        # Chat 설정 캐시
        self._chat_config: ChatConfig = self._config.chat
        self._max_history_pairs = self._chat_config.max_history_pairs
        # config.yaml의 값은 폴백 용도. 정상 경로에서는 _get_system_prompt()가
        # core.user_settings에서 최신 사용자 편집본을 매 호출마다 로드한다.
        self._system_prompt = self._chat_config.system_prompt

        # LLM 설정 캐시 (컨텍스트 윈도우는 truncation에 사용)
        self._max_context_tokens = self._config.llm.max_context_tokens

        # 검색 설정
        self._top_k = self._config.search.top_k

        # 세션 관리 (세션 ID → ChatSession)
        self._sessions: dict[str, ChatSession] = {}
        self._default_session = ChatSession(
            max_pairs=self._max_history_pairs,
        )

        logger.info(
            f"ChatEngine 초기화: backend={self._config.llm.backend}, "
            f"max_history_pairs={self._max_history_pairs}, "
            f"top_k={self._top_k}"
        )

    def get_session(self, session_id: str | None = None) -> ChatSession:
        """대화 세션을 반환한다.

        session_id가 None이면 기본 세션을 반환한다.
        존재하지 않는 session_id면 새 세션을 생성한다.

        Args:
            session_id: 세션 식별자 (None이면 기본 세션)

        Returns:
            ChatSession 인스턴스
        """
        if session_id is None:
            return self._default_session

        if session_id not in self._sessions:
            self._sessions[session_id] = ChatSession(
                max_pairs=self._max_history_pairs,
            )
        return self._sessions[session_id]

    def clear_session(self, session_id: str | None = None) -> None:
        """대화 세션의 이력을 초기화한다.

        Args:
            session_id: 세션 식별자 (None이면 기본 세션)
        """
        session = self.get_session(session_id)
        session.clear()
        logger.info(f"대화 세션 초기화: session_id={session_id or 'default'}")

    def _create_backend(self) -> LLMBackend:
        """LLM 백엔드를 생성하여 반환한다.

        ModelLoadManager의 loader 함수로 사용된다.
        config.llm.backend에 따라 Ollama 또는 MLX 백엔드를 선택한다.

        Returns:
            LLMBackend 인스턴스

        Raises:
            LLMConnectionError: 백엔드 연결 실패 시
        """
        return create_backend(self._config.llm)

    def _get_system_prompt(self) -> str:
        """사용자가 편집한 채팅 시스템 프롬프트를 로드한다.

        매 chat() 호출마다 실행되지만 core.user_settings의 mtime 캐시 덕에
        파일이 변경되지 않는 한 디스크 I/O는 발생하지 않는다.
        저장소 로드 실패 시 config.yaml의 폴백 값을 사용한다.

        Returns:
            시스템 프롬프트 문자열
        """
        try:
            return build_chat_system_prompt()
        except Exception as e:
            logger.warning(f"채팅 프롬프트 로드 실패, 폴백 사용: {e}")
            return self._system_prompt

    def _call_llm_chat(
        self,
        backend: LLMBackend,
        messages: list[dict[str, str]],
    ) -> str:
        """LLM 백엔드를 호출하여 응답을 반환한다.

        Args:
            backend: LLM 백엔드 인스턴스
            messages: 대화 메시지 목록

        Returns:
            LLM 응답 텍스트

        Raises:
            LLMConnectionError: 연결 실패 시
            LLMGenerationError: 타임아웃 시
            ChatError: 기타 API 오류 시
        """
        try:
            return backend.chat(messages=messages)
        except LLMGenerationError as e:
            raise ChatError(str(e)) from e

    def _truncate_context(
        self,
        system_prompt: str,
        history_messages: list[dict[str, str]],
        user_prompt: str,
        max_tokens: int,
    ) -> str:
        """컨텍스트 윈도우를 초과하지 않도록 사용자 프롬프트를 절단한다.

        PERF: 단순 글자수 자르기 대신 검색 컨텍스트 경계를 존중하여 절단한다.
        참조 블록 단위로 제거하여 불완전한 참조가 LLM에 전달되는 것을 방지한다.

        시스템 프롬프트 + 대화 이력 + 사용자 프롬프트의 총 토큰 수가
        max_tokens를 초과하면 사용자 프롬프트를 줄인다.

        Args:
            system_prompt: 시스템 프롬프트
            history_messages: 대화 이력 메시지 목록
            user_prompt: 사용자 프롬프트 (검색 컨텍스트 포함)
            max_tokens: 최대 토큰 수

        Returns:
            필요 시 절단된 사용자 프롬프트
        """
        # 응답을 위한 여유 토큰 확보 (약 1024 토큰)
        response_reserve = 1024
        available = max_tokens - response_reserve

        # 시스템 프롬프트 + 이력 토큰 추정
        system_tokens = _estimate_korean_tokens(system_prompt)
        history_tokens = sum(
            _estimate_korean_tokens(m.get("content", "")) for m in history_messages
        )

        # 사용자 프롬프트에 할당 가능한 토큰 수
        prompt_budget = available - system_tokens - history_tokens

        if prompt_budget <= 0:
            # 이력이 너무 길면 프롬프트 최소 할당
            prompt_budget = 512
            logger.warning(
                f"대화 이력이 너무 길어 컨텍스트 여유가 부족합니다. "
                f"프롬프트를 {prompt_budget} 토큰으로 제한합니다."
            )

        current_tokens = _estimate_korean_tokens(user_prompt)
        if current_tokens <= prompt_budget:
            return user_prompt

        # PERF: 참조 블록([참조 N]) 경계를 존중하여 뒤에서부터 제거
        # "[참조 N]" 패턴으로 분리하여 블록 단위로 제거한다
        ref_marker = "[참조 "
        if ref_marker in user_prompt:
            # 질문 부분(마지막 "위 회의 내용을..." 이후)은 보존
            question_marker = "위 회의 내용을 참고하여"
            question_idx = user_prompt.rfind(question_marker)

            if question_idx > 0:
                context_part = user_prompt[:question_idx]
                question_part = user_prompt[question_idx:]

                # 참조 블록들을 뒤에서부터 하나씩 제거
                while _estimate_korean_tokens(context_part + question_part) > prompt_budget:
                    # 마지막 "[참조 N]" 블록 찾아서 제거
                    last_ref = context_part.rfind(ref_marker)
                    if last_ref <= 0:
                        break
                    context_part = context_part[:last_ref].rstrip()

                truncated = context_part + "\n\n" + question_part
                logger.info(
                    f"프롬프트 절단 (참조 블록 단위): "
                    f"{current_tokens} → ~{_estimate_korean_tokens(truncated)} 토큰"
                )
                return truncated

        # 참조 블록 패턴이 없는 경우 글자 수 기준 절단 (폴백)
        max_chars = int(prompt_budget * 1.5)
        truncated = user_prompt[:max_chars]

        logger.info(
            f"프롬프트 절단 (글자 수): {current_tokens} → ~{prompt_budget} 토큰 "
            f"({len(user_prompt)} → {max_chars} 글자)"
        )

        return truncated

    async def chat(
        self,
        query: str,
        session_id: str | None = None,
        meeting_id_filter: str | None = None,
        date_filter: str | None = None,
        speaker_filter: str | None = None,
    ) -> ChatResponse:
        """RAG 기반 AI Chat을 수행한다.

        1. 하이브리드 검색으로 관련 회의 내용 검색
        2. 검색 결과를 LLM 컨텍스트로 구성
        3. 대화 이력 + 컨텍스트 + 질문으로 EXAONE LLM 호출
        4. 답변과 참조 출처 반환

        Args:
            query: 사용자 질문
            session_id: 대화 세션 ID (None이면 기본 세션)
            meeting_id_filter: 특정 회의로 검색 범위 제한
            date_filter: 특정 날짜로 검색 범위 제한
            speaker_filter: 특정 화자로 검색 범위 제한

        Returns:
            ChatResponse (답변 + 참조 출처)

        Raises:
            EmptyQueryError: 질문이 비어있을 때
        """
        # 질문 전처리
        query = query.strip()
        if not query:
            raise EmptyQueryError("질문이 비어있습니다.")

        query = unicodedata.normalize("NFC", query)

        logger.info(f"Chat 시작: query='{query}', session={session_id or 'default'}")

        # 1. 하이브리드 검색
        search_results: list[SearchResult] = []
        try:
            search_response: SearchResponse = await self._search_engine.search(
                query=query,
                meeting_id_filter=meeting_id_filter,
                date_filter=date_filter,
                speaker_filter=speaker_filter,
                top_k=self._top_k,
            )
            search_results = search_response.results
            logger.info(f"검색 완료: {len(search_results)}개 결과")
        except Exception as e:
            logger.warning(f"검색 실패, 컨텍스트 없이 진행: {e}")

        # 참조 출처 구성
        references = _build_references(search_results)

        # 2. LLM 프롬프트 구성
        context_text = _build_context_text(search_results)
        user_prompt = _build_user_prompt(query, context_text)

        # 대화 세션 가져오기
        session = self.get_session(session_id)
        history_messages = session.to_ollama_messages()

        # 사용자 편집본 프롬프트 로드 (요청 단위 캐싱)
        system_prompt = self._get_system_prompt()

        # 컨텍스트 윈도우 절단
        user_prompt = self._truncate_context(
            system_prompt=system_prompt,
            history_messages=history_messages,
            user_prompt=user_prompt,
            max_tokens=self._max_context_tokens,
        )

        # Ollama messages 구성
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]
        messages.extend(history_messages)
        messages.append({"role": "user", "content": user_prompt})

        # 3. LLM 호출
        try:
            async with self._model_manager.acquire("exaone", self._create_backend) as backend:
                answer = await asyncio.to_thread(self._call_llm_chat, backend, messages)

            # NFC 정규화 적용
            answer = unicodedata.normalize("NFC", answer.strip())

            # 대화 이력에 추가
            session.add_exchange(query, answer)

            logger.info(
                f"Chat 완료: query='{query}', "
                f"answer_length={len(answer)}, "
                f"references={len(references)}"
            )

            return ChatResponse(
                answer=answer,
                references=references,
                query=query,
                has_context=bool(search_results),
                llm_used=True,
            )

        except (LLMConnectionError, LLMGenerationError) as e:
            # LLM 실패 시 검색 결과만 반환 (graceful degradation)
            logger.warning(f"LLM 호출 실패, 검색 결과만 반환: {e}")

            fallback_answer = self._build_fallback_answer(search_results, str(e))

            return ChatResponse(
                answer=fallback_answer,
                references=references,
                query=query,
                has_context=bool(search_results),
                llm_used=False,
                error_message=str(e),
            )
        except ChatError as e:
            logger.warning(f"Chat 처리 실패: {e}")
            return ChatResponse(
                answer=f"죄송합니다. 답변 생성 중 오류가 발생했습니다: {e}",
                references=references,
                query=query,
                has_context=bool(search_results),
                llm_used=False,
                error_message=str(e),
            )

    async def stream_chat(
        self,
        query: str,
        session_id: str | None = None,
        meeting_id_filter: str | None = None,
        date_filter: str | None = None,
        speaker_filter: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """스트리밍 방식으로 RAG Chat 응답을 생성한다.

        검색 결과와 참조 출처를 먼저 전송한 후,
        LLM 응답을 토큰 단위로 스트리밍한다.

        Args:
            query: 사용자 질문
            session_id: 대화 세션 ID (None이면 기본 세션)
            meeting_id_filter: 특정 회의로 검색 범위 제한
            date_filter: 특정 날짜로 검색 범위 제한
            speaker_filter: 특정 화자로 검색 범위 제한

        Yields:
            스트리밍 이벤트 딕셔너리:
            - {"type": "references", "data": [...]}
            - {"type": "token", "data": "토큰 문자열"}
            - {"type": "done", "data": {"answer": "전체 답변"}}
            - {"type": "error", "data": {"message": "에러 메시지"}}

        Raises:
            EmptyQueryError: 질문이 비어있을 때
        """
        # 질문 전처리
        query = query.strip()
        if not query:
            raise EmptyQueryError("질문이 비어있습니다.")

        query = unicodedata.normalize("NFC", query)

        logger.info(f"스트리밍 Chat 시작: query='{query}'")

        # 1. 하이브리드 검색
        search_results: list[SearchResult] = []
        try:
            search_response = await self._search_engine.search(
                query=query,
                meeting_id_filter=meeting_id_filter,
                date_filter=date_filter,
                speaker_filter=speaker_filter,
                top_k=self._top_k,
            )
            search_results = search_response.results
        except Exception as e:
            logger.warning(f"검색 실패: {e}")

        # 참조 출처 먼저 전송
        references = _build_references(search_results)
        yield {
            "type": "references",
            "data": [r.to_dict() for r in references],
        }

        # 2. LLM 프롬프트 구성
        context_text = _build_context_text(search_results)
        user_prompt = _build_user_prompt(query, context_text)

        session = self.get_session(session_id)
        history_messages = session.to_ollama_messages()

        # 사용자 편집본 프롬프트 로드 (요청 단위 캐싱)
        system_prompt = self._get_system_prompt()

        user_prompt = self._truncate_context(
            system_prompt=system_prompt,
            history_messages=history_messages,
            user_prompt=user_prompt,
            max_tokens=self._max_context_tokens,
        )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]
        messages.extend(history_messages)
        messages.append({"role": "user", "content": user_prompt})

        # 3. LLM 스트리밍 호출 (Queue 브릿지를 통한 실시간 스트리밍)
        try:
            async with self._model_manager.acquire("exaone", self._create_backend) as backend:
                # 동기 스트리밍 스레드 ↔ 비동기 제너레이터 브릿지용 큐
                token_queue: queue.Queue[str | None | Exception] = queue.Queue()

                def _stream_worker() -> None:
                    """별도 스레드에서 LLM 스트리밍 호출을 실행한다."""
                    try:
                        for token in backend.chat_stream(
                            messages=messages,
                        ):
                            token_queue.put(token)
                    except Exception as e:
                        # 에러도 큐로 전달하여 비동기 측에서 처리
                        token_queue.put(e)
                    finally:
                        # 종료 신호 (None sentinel)
                        token_queue.put(None)

                # 별도 스레드에서 스트리밍 시작
                loop = asyncio.get_event_loop()
                stream_task = loop.run_in_executor(None, _stream_worker)

                # 큐에서 토큰을 비동기로 꺼내면서 즉시 yield
                full_answer_parts: list[str] = []
                timeout_seconds = self._config.llm.request_timeout_seconds
                while True:
                    try:
                        item = await asyncio.to_thread(
                            token_queue.get,
                            timeout=timeout_seconds,
                        )
                    except Exception:
                        # 큐 타임아웃 등 예외 시 루프 종료
                        break

                    if item is None:
                        # 스트리밍 완료 신호
                        break
                    if isinstance(item, Exception):
                        # 스트리밍 스레드에서 발생한 에러 전파
                        raise item

                    full_answer_parts.append(item)
                    yield {"type": "token", "data": item}

                # 스레드 완료 대기
                await stream_task

            full_answer = "".join(full_answer_parts)
            full_answer = unicodedata.normalize("NFC", full_answer.strip())

            # 대화 이력에 추가
            session.add_exchange(query, full_answer)

            yield {
                "type": "done",
                "data": {"answer": full_answer},
            }

        except (LLMConnectionError, LLMGenerationError, ChatError) as e:
            logger.warning(f"스트리밍 LLM 호출 실패: {e}")
            yield {
                "type": "error",
                "data": {"message": str(e)},
            }

    def _build_fallback_answer(
        self,
        search_results: list[SearchResult],
        error_message: str,
    ) -> str:
        """LLM 실패 시 검색 결과로 대체 답변을 구성한다.

        Args:
            search_results: 검색 결과 목록
            error_message: 에러 메시지

        Returns:
            대체 답변 텍스트
        """
        if not search_results:
            return (
                f"AI 답변을 생성할 수 없습니다 ({error_message}). "
                f"관련 회의 내용도 찾지 못했습니다."
            )

        parts = [
            f"AI 답변을 생성할 수 없습니다 ({error_message}). "
            f"관련 회의 내용을 검색 결과로 대신 제공합니다:\n"
        ]
        for i, result in enumerate(search_results, start=1):
            speakers_str = ", ".join(result.speakers) if result.speakers else "미확인"
            parts.append(
                f"\n[{i}] {result.date} | {speakers_str}\n"
                f"{result.text[:200]}{'...' if len(result.text) > 200 else ''}"
            )

        return "\n".join(parts)
