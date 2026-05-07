"""검색과 RAG 채팅 API 라우터."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.dependencies import get_chat_engine as _get_chat_engine
from api.dependencies import get_search_engine as _get_search_engine

logger = logging.getLogger(__name__)

router = APIRouter()


class SearchRequest(BaseModel):
    """검색 요청 스키마.

    Attributes:
        query: 검색 쿼리 문자열
        date_filter: 날짜 필터 (선택, 예: "2026-03-04")
        speaker_filter: 화자 필터 (선택, 예: "SPEAKER_00")
        meeting_id_filter: 회의 ID 필터 (선택)
        top_k: 반환할 최대 결과 수 (선택)
    """

    query: str = Field(..., min_length=1, description="검색 쿼리")
    date_filter: str | None = None
    speaker_filter: str | None = None
    meeting_id_filter: str | None = None
    top_k: int | None = Field(None, ge=1, le=20)


class SearchResultItem(BaseModel):
    """검색 결과 아이템 스키마.

    Attributes:
        chunk_id: 청크 고유 식별자
        text: 청크 텍스트
        score: RRF 결합 점수
        meeting_id: 회의 식별자
        date: 회의 날짜
        speakers: 화자 목록
        start_time: 시작 시간 (초)
        end_time: 종료 시간 (초)
        chunk_index: 청크 순서 인덱스
        source: 검색 소스 ("vector", "fts", "both")
    """

    chunk_id: str
    text: str
    score: float
    meeting_id: str
    date: str
    speakers: list[str] = Field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    chunk_index: int = 0
    source: str = "both"


class SearchResponse(BaseModel):
    """검색 응답 스키마.

    Attributes:
        results: 검색 결과 목록
        query: 원본 검색 쿼리
        total_found: 검색된 결과 수
        vector_count: 벡터 검색 결과 수
        fts_count: FTS 검색 결과 수
        filters_applied: 적용된 필터 정보
    """

    results: list[SearchResultItem] = Field(default_factory=list)
    query: str
    total_found: int = 0
    vector_count: int = 0
    fts_count: int = 0
    filters_applied: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    """Chat 요청 스키마.

    Attributes:
        query: 사용자 질문
        session_id: 대화 세션 ID (선택)
        meeting_id_filter: 특정 회의로 검색 범위 제한 (선택)
        date_filter: 특정 날짜로 검색 범위 제한 (선택)
        speaker_filter: 특정 화자로 검색 범위 제한 (선택)
    """

    query: str = Field(..., min_length=1, description="사용자 질문")
    session_id: str | None = None
    meeting_id_filter: str | None = None
    date_filter: str | None = None
    speaker_filter: str | None = None


class ChatReferenceItem(BaseModel):
    """Chat 참조 출처 스키마.

    Attributes:
        chunk_id: 청크 고유 식별자
        meeting_id: 회의 식별자
        date: 회의 날짜
        speakers: 화자 목록
        start_time: 시작 시간 (초)
        end_time: 종료 시간 (초)
        text_preview: 청크 텍스트 미리보기
        score: 검색 관련도 점수
    """

    chunk_id: str
    meeting_id: str
    date: str
    speakers: list[str] = Field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    text_preview: str = ""
    score: float = 0.0


class ChatResponse(BaseModel):
    """Chat 응답 스키마.

    Phase 5 추가: 라우터 활성 시 source_type/router_verdict 가 채워진다.
    기존 호출자는 두 필드를 무시해도 동작이 변하지 않는다 (Optional).

    Attributes:
        answer: LLM이 생성한 답변
        references: 참조 출처 목록
        query: 원본 질문
        has_context: 검색 컨텍스트 존재 여부
        llm_used: LLM 응답 성공 여부
        error_message: 에러 메시지 (선택)
        source_type: "rag" | "wiki" | "both" | None.
            None — config.wiki.router_enabled=False 일 때 (회귀 0 보장).
            "rag" — 라우터가 RAG 결정 또는 LLM 폴백 fallback.
            "wiki" — 위키 페이지 합성 답변.
            "both" — RAG + 위키 병렬 답변.
        router_verdict: 라우터 결정 메타데이터 (활성 시).
            decision/confidence/reason/matched_signals/used_llm 키 포함.
        wiki_sources: WIKI/BOTH 분기에서 인용된 위키 페이지 목록 (Optional).
    """

    answer: str
    references: list[ChatReferenceItem] = Field(default_factory=list)
    query: str
    has_context: bool = True
    llm_used: bool = True
    error_message: str | None = None
    source_type: str | None = None
    router_verdict: dict[str, Any] | None = None
    wiki_sources: list[dict[str, Any]] | None = None


@router.post("/search", response_model=SearchResponse)
async def search(request: Request, body: SearchRequest) -> SearchResponse:
    """하이브리드 검색을 수행한다.

    벡터 검색(ChromaDB)과 키워드 검색(FTS5)을 RRF로 결합하여
    관련 회의 내용을 검색한다.

    Args:
        request: FastAPI Request 객체
        body: SearchRequest 검색 요청

    Returns:
        SearchResponse: 검색 결과

    Raises:
        HTTPException: 빈 쿼리(400), 엔진 미초기화(503), 서버 에러(500)
    """
    search_engine = _get_search_engine(request)

    try:
        from search.hybrid_search import EmptyQueryError, ModelLoadError

        result = await search_engine.search(
            query=body.query,
            date_filter=body.date_filter,
            speaker_filter=body.speaker_filter,
            meeting_id_filter=body.meeting_id_filter,
            top_k=body.top_k,
        )

        # SearchResult → SearchResultItem 변환
        items = [
            SearchResultItem(
                chunk_id=r.chunk_id,
                text=r.text,
                score=r.score,
                meeting_id=r.meeting_id,
                date=r.date,
                speakers=r.speakers,
                start_time=r.start_time,
                end_time=r.end_time,
                chunk_index=r.chunk_index,
                source=r.source,
            )
            for r in result.results
        ]

        return SearchResponse(
            results=items,
            query=result.query,
            total_found=result.total_found,
            vector_count=result.vector_count,
            fts_count=result.fts_count,
            filters_applied=result.filters_applied,
        )

    except EmptyQueryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ModelLoadError as e:
        logger.error(f"검색 모델 로드 실패: {e}")
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"검색 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"검색 중 오류가 발생했습니다: {e}",
        ) from e


class _ChatEngineAdapter:
    """ChatEngine.chat() 시그니처를 HybridChatService 가 기대하는 respond() 인터페이스로 변환하는 얇은 어댑터.

    HybridChatService 는 chat_service.respond(query, **kwargs) 를 호출하지만,
    기존 search.chat.ChatEngine 은 chat(query, session_id=..., ...) 시그니처를
    가진다. 이 어댑터가 둘을 연결한다.

    설계 원칙:
        - search/chat.py 무변경 (Phase 5.D 격리 보장).
        - kwargs 를 그대로 ChatEngine.chat() 에 전달 (session_id 등).
        - 반환값은 ChatEngine.ChatResponse 그대로 — HybridChatService 가 그대로 보존.
    """

    def __init__(self, chat_engine: Any) -> None:
        """ChatEngine 인스턴스를 감싼다.

        Args:
            chat_engine: search.chat.ChatEngine 인스턴스.
        """
        self._chat_engine = chat_engine

    async def respond(self, query: str, **kwargs: Any) -> Any:
        """HybridChatService 호환 시그니처.

        Args:
            query: 사용자 질문.
            **kwargs: session_id / meeting_id_filter / date_filter / speaker_filter
                를 chat_engine.chat() 에 그대로 전달.

        Returns:
            ChatEngine.chat() 반환값 (search.chat.ChatResponse).
        """
        return await self._chat_engine.chat(query=query, **kwargs)


def _build_chat_references(
    references: list[Any],
) -> list[ChatReferenceItem]:
    """search.chat.ChatReference → ChatReferenceItem 변환.

    Args:
        references: ChatEngine 응답의 references 리스트.

    Returns:
        Pydantic 변환된 ChatReferenceItem 리스트.
    """
    return [
        ChatReferenceItem(
            chunk_id=r.chunk_id,
            meeting_id=r.meeting_id,
            date=r.date,
            speakers=r.speakers,
            start_time=r.start_time,
            end_time=r.end_time,
            text_preview=r.text_preview,
            score=r.score,
        )
        for r in references
    ]


def _serialize_router_verdict(verdict: Any) -> dict[str, Any]:
    """RouterVerdict → JSON 직렬화 가능한 dict.

    Args:
        verdict: core.wiki.router.RouterVerdict 인스턴스.

    Returns:
        decision/confidence/reason/matched_signals/used_llm 키 dict.
    """
    return {
        "decision": str(verdict.decision),
        "confidence": int(verdict.confidence),
        "reason": str(verdict.reason),
        "matched_signals": list(verdict.matched_signals),
        "used_llm": bool(verdict.used_llm),
    }


def _serialize_wiki_sources(sources: list[Any]) -> list[dict[str, Any]]:
    """WikiAnswerSource 리스트 → JSON 직렬화 가능한 dict 리스트.

    Args:
        sources: HybridChatResponse.wiki_sources 리스트.

    Returns:
        page_path/page_type/title/snippet/citations 키 dict 리스트.
    """
    return [
        {
            "page_path": s.page_path,
            "page_type": s.page_type,
            "title": s.title,
            "snippet": s.snippet,
            "citations": list(s.citations),
        }
        for s in sources
    ]


def _build_hybrid_chat_service(request: Request, chat_engine: Any) -> Any:
    """라우터 활성 분기에서 HybridChatService 인스턴스를 생성한다.

    Phase 5.D — 라우터 + WikiStore + (옵션) MlxWikiClient 를 lazy 로 묶어
    HybridChatService 를 만든다. 매 요청 생성이지만 라우터 자체는 stateless 라
    추가 메모리 비용은 무시할 수 있다.

    Args:
        request: FastAPI Request 객체 (app.state.config / model_manager 접근).
        chat_engine: 기존 ChatEngine 인스턴스 (어댑터로 감쌈).

    Returns:
        HybridChatService 인스턴스.
    """
    # 지연 import — 라우터 비활성 시 import 비용 0
    from core.wiki.chat_integration import HybridChatService  # noqa: PLC0415
    from core.wiki.router import QueryRouter  # noqa: PLC0415

    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="서버 설정이 초기화되지 않았습니다.",
        )

    # WikiStore — wiki 활성 시에만 생성
    wiki_store: Any = None
    if config.wiki.enabled:
        try:
            from core.wiki.store import WikiStore  # noqa: PLC0415

            wiki_root = config.wiki.resolved_root
            wiki_store = WikiStore(root=wiki_root)
        except Exception as exc:  # noqa: BLE001 — WikiStore 실패 시 RAG 폴백 가능
            logger.warning("WikiStore 초기화 실패, WIKI 분기는 RAG 폴백됨: %s", exc)
            wiki_store = None

    # LLM 폴백 — router_llm_fallback=True 면 MlxWikiClient 시도
    wiki_llm: Any = None
    if config.wiki.router_llm_fallback:
        try:
            from core.wiki.llm_client import MlxWikiClient  # noqa: PLC0415

            model_manager = getattr(request.app.state, "model_manager", None)
            if model_manager is not None:
                wiki_llm = MlxWikiClient(config=config, model_manager=model_manager)
        except Exception as exc:  # noqa: BLE001 — LLM 폴백은 옵션
            logger.warning("MlxWikiClient 초기화 실패, LLM 폴백 비활성: %s", exc)
            wiki_llm = None

    router_obj = QueryRouter(
        llm=wiki_llm,
        enable_llm_fallback=config.wiki.router_llm_fallback and wiki_llm is not None,
    )

    chat_adapter = _ChatEngineAdapter(chat_engine)

    return HybridChatService(
        chat_service=chat_adapter,
        router=router_obj,
        wiki_store=wiki_store,
        wiki_llm=wiki_llm,
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    """RAG 기반 AI Chat을 수행한다 (Phase 5: 옵션 라우터 통합).

    동작 분기 (config.wiki.router_enabled):
        - False (default): 기존 ChatEngine.chat() 100% 위임 — 회귀 0건 보장.
        - True: HybridChatService 통과 — 라우터가 RAG/WIKI/BOTH 결정 후 실행.

    응답 호환성:
        ChatResponse 의 source_type/router_verdict/wiki_sources 는 모두 Optional.
        라우터 비활성 시 None — 기존 UI 가 무시해도 동작이 변하지 않는다.

    Args:
        request: FastAPI Request 객체.
        body: ChatRequest 채팅 요청.

    Returns:
        ChatResponse: AI 답변 + 참조 출처 (+ 라우터 활성 시 추가 메타).

    Raises:
        HTTPException: 빈 질문(400), 엔진 미초기화(503), 서버 에러(500).
    """
    chat_engine = _get_chat_engine(request)
    config = getattr(request.app.state, "config", None)

    # 라우터 활성 여부 결정 — config 없거나 router_enabled=False 면 기존 경로
    router_enabled: bool = bool(
        config is not None and getattr(getattr(config, "wiki", None), "router_enabled", False)
    )

    try:
        from search.chat import EmptyQueryError as ChatEmptyQueryError

        # ─── 라우터 비활성 (default) — 기존 동작 100% 보존 ──────────────────
        if not router_enabled:
            result = await chat_engine.chat(
                query=body.query,
                session_id=body.session_id,
                meeting_id_filter=body.meeting_id_filter,
                date_filter=body.date_filter,
                speaker_filter=body.speaker_filter,
            )
            return ChatResponse(
                answer=result.answer,
                references=_build_chat_references(result.references),
                query=result.query,
                has_context=result.has_context,
                llm_used=result.llm_used,
                error_message=result.error_message,
                # 회귀 보장: 라우터 비활성 시 새 필드는 None 유지
                source_type=None,
                router_verdict=None,
                wiki_sources=None,
            )

        # ─── 라우터 활성 — HybridChatService 분기 ───────────────────────────
        hybrid = _build_hybrid_chat_service(request, chat_engine)
        hybrid_result = await hybrid.respond(
            query=body.query,
            session_id=body.session_id,
            meeting_id_filter=body.meeting_id_filter,
            date_filter=body.date_filter,
            speaker_filter=body.speaker_filter,
        )

        # 응답 합성 — source_type 별 분기
        rag_response = hybrid_result.rag_response
        verdict_dict = _serialize_router_verdict(hybrid_result.router_verdict)

        if hybrid_result.source_type == "wiki":
            # WIKI 답변만 — 기존 references 는 빈 리스트
            return ChatResponse(
                answer=hybrid_result.wiki_answer or "",
                references=[],
                query=body.query,
                has_context=bool(hybrid_result.wiki_sources),
                llm_used=True,
                error_message=hybrid_result.error_message,
                source_type="wiki",
                router_verdict=verdict_dict,
                wiki_sources=_serialize_wiki_sources(hybrid_result.wiki_sources),
            )

        if hybrid_result.source_type == "both":
            # BOTH — RAG answer + wiki answer 병합 (UI 가 둘 다 표시)
            rag_answer = rag_response.answer if rag_response is not None else ""
            wiki_answer = hybrid_result.wiki_answer or ""
            combined = rag_answer
            if wiki_answer:
                combined = (
                    f"{rag_answer}\n\n---\n\n## 위키 누적 답변\n\n{wiki_answer}"
                    if rag_answer
                    else wiki_answer
                )
            refs = (
                _build_chat_references(rag_response.references) if rag_response is not None else []
            )
            return ChatResponse(
                answer=combined,
                references=refs,
                query=body.query,
                has_context=bool(refs) or bool(hybrid_result.wiki_sources),
                llm_used=rag_response.llm_used if rag_response is not None else True,
                error_message=hybrid_result.error_message,
                source_type="both",
                router_verdict=verdict_dict,
                wiki_sources=_serialize_wiki_sources(hybrid_result.wiki_sources),
            )

        # source_type == "rag" — 기존 RAG 응답에 라우터 메타만 추가
        if rag_response is None:
            # graceful — chat_service 가 None 반환했을 때 (실제로는 거의 없음)
            return ChatResponse(
                answer="",
                references=[],
                query=body.query,
                has_context=False,
                llm_used=False,
                error_message=hybrid_result.error_message or "rag_response_missing",
                source_type="rag",
                router_verdict=verdict_dict,
                wiki_sources=None,
            )

        return ChatResponse(
            answer=rag_response.answer,
            references=_build_chat_references(rag_response.references),
            query=rag_response.query,
            has_context=rag_response.has_context,
            llm_used=rag_response.llm_used,
            error_message=rag_response.error_message or hybrid_result.error_message,
            source_type="rag",
            router_verdict=verdict_dict,
            wiki_sources=None,
        )

    except ChatEmptyQueryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"Chat 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Chat 중 오류가 발생했습니다: {e}",
        ) from e
