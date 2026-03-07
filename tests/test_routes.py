"""
API 라우터 테스트 모듈 (API Routes Test Module)

목적: api/routes.py의 REST API 엔드포인트를 검증한다.
주요 테스트:
    - /api/status: 시스템 상태 조회
    - /api/meetings: 회의 목록 조회
    - /api/meetings/{meeting_id}: 특정 회의 상세 조회
    - /api/meetings/{meeting_id}/transcript: 전사문 조회
    - /api/meetings/{meeting_id}/summary: 회의록 조회
    - /api/search: 하이브리드 검색
    - /api/chat: RAG 기반 AI Chat
    - 에러 처리 (400, 404, 503, 500)
    - pydantic 스키마 검증
의존성: pytest, fastapi (TestClient), unittest.mock
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from config import AppConfig, PathsConfig, ServerConfig

# === 헬퍼 ===


def _make_test_config(tmp_path: Path) -> AppConfig:
    """테스트용 AppConfig를 생성한다.

    Args:
        tmp_path: pytest 임시 디렉토리

    Returns:
        테스트용 AppConfig 인스턴스
    """
    return AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
    )


def _make_test_app(tmp_path: Path) -> Any:
    """테스트용 FastAPI 앱을 생성한다.

    ChatEngine과 HybridSearchEngine 초기화를 패치하여
    외부 의존성 없이 테스트할 수 있도록 한다.

    Args:
        tmp_path: pytest 임시 디렉토리

    Returns:
        FastAPI 앱 인스턴스
    """
    from api.server import create_app

    config = _make_test_config(tmp_path)

    # lifespan에서 lazy import하므로 원본 모듈을 패치
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
class MockJob:
    """테스트용 Job 데이터 클래스."""

    id: int
    meeting_id: str
    audio_path: str
    status: str = "completed"
    retry_count: int = 0
    error_message: str = ""
    created_at: str = "2026-03-04T10:00:00"
    updated_at: str = "2026-03-04T10:30:00"


@dataclass
class MockSearchResult:
    """테스트용 SearchResult 데이터 클래스."""

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
class MockChatReference:
    """테스트용 ChatReference 데이터 클래스."""

    chunk_id: str
    meeting_id: str
    date: str
    speakers: list[str]
    start_time: float
    end_time: float
    text_preview: str
    score: float


@dataclass
class MockChatResponse:
    """테스트용 ChatResponse 데이터 클래스."""

    answer: str
    references: list[MockChatReference]
    query: str
    has_context: bool = True
    llm_used: bool = True
    error_message: str | None = None


@dataclass
class MockSearchResponse:
    """테스트용 SearchResponse 데이터 클래스."""

    results: list[MockSearchResult]
    query: str
    total_found: int = 0
    vector_count: int = 0
    fts_count: int = 0
    filters_applied: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.filters_applied is None:
            self.filters_applied = {}


# === TestStatusEndpoint ===


class TestStatusEndpoint:
    """GET /api/status 엔드포인트 테스트."""

    def test_status_정상_응답(self, tmp_path: Path) -> None:
        """상태 조회 시 200 OK와 큐 정보를 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            # 모킹: 큐 상태 집계
            app.state.job_queue.count_by_status = AsyncMock(
                return_value={"completed": 3, "queued": 1},
            )
            app.state.job_queue.get_all_jobs = AsyncMock(
                return_value=[MockJob(1, "m1", "/a.wav"), MockJob(2, "m2", "/b.wav")],
            )

            response = client.get("/api/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["queue_summary"]["completed"] == 3
        assert data["total_jobs"] == 2

    def test_status_active_jobs_계산(self, tmp_path: Path) -> None:
        """진행 중인 작업(recording, transcribing 등)이 올바르게 집계되는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.job_queue.count_by_status = AsyncMock(
                return_value={
                    "recording": 1,
                    "transcribing": 2,
                    "completed": 5,
                    "queued": 0,
                },
            )
            app.state.job_queue.get_all_jobs = AsyncMock(
                return_value=[MockJob(i, f"m{i}", f"/{i}.wav") for i in range(8)],
            )

            response = client.get("/api/status")

        data = response.json()
        # recording(1) + transcribing(2) = 3 active
        assert data["active_jobs"] == 3

    def test_status_큐_미초기화_503(self, tmp_path: Path) -> None:
        """job_queue가 없을 때 503을 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            # 원본 큐를 저장하고 None으로 교체 (503 테스트)
            original_queue = app.state.job_queue
            app.state.job_queue = None

            response = client.get("/api/status")

            # shutdown 시 close() 호출을 위해 원본 복원
            app.state.job_queue = original_queue

        assert response.status_code == 503


# === TestMeetingsEndpoint ===


class TestMeetingsEndpoint:
    """GET /api/meetings 엔드포인트 테스트."""

    def test_meetings_전체_목록_조회(self, tmp_path: Path) -> None:
        """전체 회의 목록을 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        jobs = [
            MockJob(1, "meeting_001", "/audio/001.m4a", "completed"),
            MockJob(2, "meeting_002", "/audio/002.m4a", "transcribing"),
        ]

        with TestClient(app) as client:
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=jobs)

            response = client.get("/api/meetings")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert len(data["meetings"]) == 2
        assert data["meetings"][0]["meeting_id"] == "meeting_001"

    def test_meetings_빈_목록(self, tmp_path: Path) -> None:
        """작업이 없을 때 빈 목록을 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=[])

            response = client.get("/api/meetings")

        data = response.json()
        assert data["total"] == 0
        assert data["meetings"] == []

    def test_meetings_응답_스키마_검증(self, tmp_path: Path) -> None:
        """응답이 MeetingsResponse 스키마를 준수하는지 확인한다."""
        app = _make_test_app(tmp_path)

        jobs = [
            MockJob(
                1,
                "m1",
                "/a.wav",
                "completed",
                0,
                "",
                "2026-03-04T10:00:00",
                "2026-03-04T10:30:00",
            ),
        ]

        with TestClient(app) as client:
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=jobs)

            response = client.get("/api/meetings")

        data = response.json()
        meeting = data["meetings"][0]
        assert "id" in meeting
        assert "meeting_id" in meeting
        assert "audio_path" in meeting
        assert "status" in meeting
        assert "created_at" in meeting


# === TestMeetingDetailEndpoint ===


class TestMeetingDetailEndpoint:
    """GET /api/meetings/{meeting_id} 엔드포인트 테스트."""

    def test_meeting_상세_조회_성공(self, tmp_path: Path) -> None:
        """존재하는 meeting_id로 상세 정보를 조회하는지 확인한다."""
        app = _make_test_app(tmp_path)

        mock_job = MockJob(1, "meeting_001", "/audio/001.m4a", "completed")

        with TestClient(app) as client:
            # queue는 읽기 전용 property이므로 _queue의 메서드를 직접 모킹
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )

            response = client.get("/api/meetings/meeting_001")

        assert response.status_code == 200
        data = response.json()
        assert data["meeting_id"] == "meeting_001"
        assert data["status"] == "completed"

    def test_meeting_미존재_404(self, tmp_path: Path) -> None:
        """존재하지 않는 meeting_id 조회 시 404를 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=None,
            )

            response = client.get("/api/meetings/nonexistent")

        assert response.status_code == 404
        assert "찾을 수 없습니다" in response.json()["detail"]

    def test_meeting_상세_응답_필드_검증(self, tmp_path: Path) -> None:
        """상세 조회 응답에 모든 필수 필드가 포함되는지 확인한다."""
        app = _make_test_app(tmp_path)

        mock_job = MockJob(
            1,
            "meeting_001",
            "/audio/001.m4a",
            "failed",
            retry_count=2,
            error_message="OOM",
        )

        with TestClient(app) as client:
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )

            response = client.get("/api/meetings/meeting_001")

        data = response.json()
        assert data["retry_count"] == 2
        assert data["error_message"] == "OOM"


# === TestSearchEndpoint ===


class TestSearchEndpoint:
    """POST /api/search 엔드포인트 테스트."""

    def test_search_정상_응답(self, tmp_path: Path) -> None:
        """검색 요청에 정상 응답을 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        mock_results = [
            MockSearchResult(
                chunk_id="chunk_001",
                text="프로젝트 일정 논의",
                score=0.85,
                meeting_id="meeting_001",
                date="2026-03-04",
                speakers=["SPEAKER_00", "SPEAKER_01"],
                start_time=120.0,
                end_time=180.0,
            ),
        ]
        mock_response = MockSearchResponse(
            results=mock_results,
            query="프로젝트 일정",
            total_found=1,
            vector_count=1,
            fts_count=1,
        )

        with TestClient(app) as client:
            app.state.search_engine.search = AsyncMock(
                return_value=mock_response,
            )

            response = client.post(
                "/api/search",
                json={"query": "프로젝트 일정"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["query"] == "프로젝트 일정"
        assert len(data["results"]) == 1
        assert data["results"][0]["chunk_id"] == "chunk_001"

    def test_search_빈_쿼리_400(self, tmp_path: Path) -> None:
        """빈 쿼리로 검색 시 400을 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/api/search",
                json={"query": ""},
            )

        assert response.status_code == 422  # pydantic min_length=1 검증

    def test_search_필터_전달(self, tmp_path: Path) -> None:
        """날짜/화자/회의ID 필터가 검색 엔진에 전달되는지 확인한다."""
        app = _make_test_app(tmp_path)

        mock_response = MockSearchResponse(
            results=[],
            query="테스트",
            total_found=0,
        )

        with TestClient(app) as client:
            app.state.search_engine.search = AsyncMock(
                return_value=mock_response,
            )

            response = client.post(
                "/api/search",
                json={
                    "query": "테스트",
                    "date_filter": "2026-03-04",
                    "speaker_filter": "SPEAKER_00",
                    "meeting_id_filter": "m001",
                    "top_k": 3,
                },
            )

        assert response.status_code == 200
        # 검색 엔진이 필터와 함께 호출되었는지 확인
        call_kwargs = app.state.search_engine.search.call_args.kwargs
        assert call_kwargs["date_filter"] == "2026-03-04"
        assert call_kwargs["speaker_filter"] == "SPEAKER_00"
        assert call_kwargs["meeting_id_filter"] == "m001"
        assert call_kwargs["top_k"] == 3

    def test_search_엔진_미초기화_503(self, tmp_path: Path) -> None:
        """검색 엔진이 초기화되지 않았을 때 503을 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.search_engine = None

            response = client.post(
                "/api/search",
                json={"query": "테스트"},
            )

        assert response.status_code == 503

    def test_search_EmptyQueryError_400(self, tmp_path: Path) -> None:
        """검색 엔진이 EmptyQueryError를 발생시킬 때 400을 반환하는지 확인한다."""
        from search.hybrid_search import EmptyQueryError

        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.search_engine.search = AsyncMock(
                side_effect=EmptyQueryError("빈 쿼리"),
            )

            response = client.post(
                "/api/search",
                json={"query": "a"},  # min_length=1은 통과
            )

        assert response.status_code == 400

    def test_search_응답_스키마_검증(self, tmp_path: Path) -> None:
        """검색 응답이 SearchResponse 스키마를 준수하는지 확인한다."""
        app = _make_test_app(tmp_path)

        mock_results = [
            MockSearchResult(
                chunk_id="c1",
                text="텍스트",
                score=0.5,
                meeting_id="m1",
                date="2026-03-04",
                speakers=["S0"],
                start_time=0.0,
                end_time=10.0,
                chunk_index=0,
                source="vector",
            ),
        ]
        mock_response = MockSearchResponse(
            results=mock_results,
            query="q",
            total_found=1,
            vector_count=1,
            fts_count=0,
            filters_applied={"date": "2026-03-04"},
        )

        with TestClient(app) as client:
            app.state.search_engine.search = AsyncMock(
                return_value=mock_response,
            )

            response = client.post(
                "/api/search",
                json={"query": "q"},
            )

        data = response.json()
        assert "results" in data
        assert "query" in data
        assert "total_found" in data
        assert "vector_count" in data
        assert "fts_count" in data
        result = data["results"][0]
        assert "chunk_id" in result
        assert "speakers" in result
        assert "source" in result


# === TestChatEndpoint ===


class TestChatEndpoint:
    """POST /api/chat 엔드포인트 테스트."""

    def test_chat_정상_응답(self, tmp_path: Path) -> None:
        """Chat 요청에 정상 응답을 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        mock_refs = [
            MockChatReference(
                chunk_id="c1",
                meeting_id="m1",
                date="2026-03-04",
                speakers=["SPEAKER_00"],
                start_time=60.0,
                end_time=120.0,
                text_preview="프로젝트 일정에 대해...",
                score=0.8,
            ),
        ]
        mock_response = MockChatResponse(
            answer="프로젝트 일정은 다음과 같습니다.",
            references=mock_refs,
            query="프로젝트 일정이 어떻게 되나요?",
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
        assert "프로젝트 일정" in data["answer"]
        assert len(data["references"]) == 1
        assert data["llm_used"] is True

    def test_chat_빈_질문_422(self, tmp_path: Path) -> None:
        """빈 질문으로 Chat 시 422(pydantic 검증)를 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/api/chat",
                json={"query": ""},
            )

        assert response.status_code == 422

    def test_chat_세션_ID_전달(self, tmp_path: Path) -> None:
        """session_id가 ChatEngine에 전달되는지 확인한다."""
        app = _make_test_app(tmp_path)

        mock_response = MockChatResponse(
            answer="답변",
            references=[],
            query="질문",
        )

        with TestClient(app) as client:
            app.state.chat_engine.chat = AsyncMock(
                return_value=mock_response,
            )

            response = client.post(
                "/api/chat",
                json={
                    "query": "질문",
                    "session_id": "session_123",
                    "meeting_id_filter": "m001",
                },
            )

        assert response.status_code == 200
        call_kwargs = app.state.chat_engine.chat.call_args.kwargs
        assert call_kwargs["session_id"] == "session_123"
        assert call_kwargs["meeting_id_filter"] == "m001"

    def test_chat_엔진_미초기화_503(self, tmp_path: Path) -> None:
        """Chat 엔진이 초기화되지 않았을 때 503을 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.chat_engine = None

            response = client.post(
                "/api/chat",
                json={"query": "테스트"},
            )

        assert response.status_code == 503

    def test_chat_LLM_실패시_fallback_응답(self, tmp_path: Path) -> None:
        """LLM 실패 시에도 검색 결과가 포함된 응답을 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        mock_response = MockChatResponse(
            answer="AI 답변을 생성할 수 없습니다.",
            references=[],
            query="질문",
            llm_used=False,
            error_message="Ollama 연결 실패",
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

    def test_chat_EmptyQueryError_400(self, tmp_path: Path) -> None:
        """ChatEngine이 EmptyQueryError를 발생시킬 때 400을 반환하는지 확인한다."""
        from search.chat import EmptyQueryError

        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.chat_engine.chat = AsyncMock(
                side_effect=EmptyQueryError("빈 질문"),
            )

            response = client.post(
                "/api/chat",
                json={"query": "a"},
            )

        assert response.status_code == 400

    def test_chat_응답_스키마_검증(self, tmp_path: Path) -> None:
        """Chat 응답이 ChatResponse 스키마를 준수하는지 확인한다."""
        app = _make_test_app(tmp_path)

        mock_refs = [
            MockChatReference(
                chunk_id="c1",
                meeting_id="m1",
                date="2026-03-04",
                speakers=["S0"],
                start_time=0.0,
                end_time=10.0,
                text_preview="미리보기...",
                score=0.7,
            ),
        ]
        mock_response = MockChatResponse(
            answer="답변 텍스트",
            references=mock_refs,
            query="질문",
            has_context=True,
            llm_used=True,
        )

        with TestClient(app) as client:
            app.state.chat_engine.chat = AsyncMock(
                return_value=mock_response,
            )

            response = client.post(
                "/api/chat",
                json={"query": "질문"},
            )

        data = response.json()
        assert "answer" in data
        assert "references" in data
        assert "query" in data
        assert "has_context" in data
        assert "llm_used" in data
        ref = data["references"][0]
        assert "chunk_id" in ref
        assert "text_preview" in ref
        assert "score" in ref


# === TestErrorHandling ===


class TestErrorHandling:
    """API 에러 처리 통합 테스트."""

    def test_서버_내부_오류_500(self, tmp_path: Path) -> None:
        """예상치 못한 예외 발생 시 500을 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.job_queue.count_by_status = AsyncMock(
                side_effect=RuntimeError("DB 연결 끊김"),
            )

            response = client.get("/api/status")

        assert response.status_code == 500

    def test_잘못된_JSON_요청_422(self, tmp_path: Path) -> None:
        """잘못된 JSON 형식의 요청 시 422를 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            # 필수 필드 누락
            response = client.post(
                "/api/search",
                json={},
            )

        assert response.status_code == 422

    def test_지원하지_않는_HTTP_메서드_405(self, tmp_path: Path) -> None:
        """지원하지 않는 HTTP 메서드 사용 시 405를 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            # GET은 /api/search에서 지원하지 않음
            response = client.get("/api/search")

        assert response.status_code == 405


# === TestRouterIntegration ===


class TestRouterIntegration:
    """라우터 등록 및 통합 테스트."""

    def test_라우터_등록_확인(self, tmp_path: Path) -> None:
        """API 라우터가 앱에 등록되었는지 확인한다."""
        app = _make_test_app(tmp_path)

        # 등록된 라우트 경로 수집
        route_paths = []
        for route in app.routes:
            if hasattr(route, "path"):
                route_paths.append(route.path)

        assert "/api/status" in route_paths
        assert "/api/meetings" in route_paths
        assert "/api/meetings/{meeting_id}" in route_paths
        assert "/api/search" in route_paths
        assert "/api/chat" in route_paths

    def test_헬스체크_여전히_동작(self, tmp_path: Path) -> None:
        """라우터 추가 후에도 /api/health가 정상 동작하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_openapi_스키마에_엔드포인트_포함(self, tmp_path: Path) -> None:
        """OpenAPI 스키마에 모든 API 엔드포인트가 포함되는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.get("/api/openapi.json")

        assert response.status_code == 200
        schema = response.json()
        paths = schema["paths"]
        assert "/api/status" in paths
        assert "/api/meetings" in paths
        assert "/api/search" in paths
        assert "/api/chat" in paths


# === TestTranscriptEndpoint ===


def _create_corrected_json(outputs_dir: Path, meeting_id: str) -> Path:
    """테스트용 corrected.json 파일을 생성한다.

    Args:
        outputs_dir: outputs 디렉토리 경로
        meeting_id: 회의 ID

    Returns:
        생성된 corrected.json 파일 경로
    """
    meeting_dir = outputs_dir / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "utterances": [
            {
                "text": "안녕하세요, 오늘 회의를 시작하겠습니다.",
                "original_text": "안녕하세요 오늘 회의를 시작 하겠습니다",
                "speaker": "SPEAKER_00",
                "start": 0.5,
                "end": 3.2,
                "was_corrected": True,
            },
            {
                "text": "네, 감사합니다.",
                "original_text": "네 감사합니다",
                "speaker": "SPEAKER_01",
                "start": 3.5,
                "end": 5.0,
                "was_corrected": False,
            },
            {
                "text": "첫 번째 안건을 논의하겠습니다.",
                "original_text": "첫번째 안건을 논의 하겠습니다",
                "speaker": "SPEAKER_00",
                "start": 5.5,
                "end": 8.0,
                "was_corrected": True,
            },
        ],
        "num_speakers": 2,
        "audio_path": "/audio/meeting_test.m4a",
        "total_corrected": 2,
        "total_failed": 0,
    }

    file_path = meeting_dir / "corrected.json"
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return file_path


def _create_summary_files(
    outputs_dir: Path,
    meeting_id: str,
) -> tuple[Path, Path]:
    """테스트용 summary.md와 summary.json 파일을 생성한다.

    Args:
        outputs_dir: outputs 디렉토리 경로
        meeting_id: 회의 ID

    Returns:
        (summary.md 경로, summary.json 경로) 튜플
    """
    meeting_dir = outputs_dir / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)

    md_content = """## 회의 개요
- 참석자: SPEAKER_00, SPEAKER_01
- 프로젝트 진행 상황 논의

## 주요 안건
1. 일정 확인
   - 다음 주 마감 예정

## 결정 사항
- 일정 변경 없음

## 액션 아이템
- [ ] SPEAKER_00: 보고서 제출
"""

    meta_data = {
        "markdown": md_content,
        "audio_path": "/audio/meeting_test.m4a",
        "num_speakers": 2,
        "speakers": ["SPEAKER_00", "SPEAKER_01"],
        "num_utterances": 3,
        "created_at": "2026-03-04T14:00:00",
        "was_chunked": False,
        "chunk_count": 1,
    }

    md_path = meeting_dir / "summary.md"
    json_path = meeting_dir / "summary.json"

    md_path.write_text(md_content, encoding="utf-8")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(meta_data, f, ensure_ascii=False, indent=2)

    return md_path, json_path


class TestTranscriptEndpoint:
    """GET /api/meetings/{meeting_id}/transcript 엔드포인트 테스트."""

    def test_전사문_정상_조회(self, tmp_path: Path) -> None:
        """전사문 JSON이 정상적으로 반환되는지 확인한다."""
        app = _make_test_app(tmp_path)
        outputs_dir = tmp_path / "outputs"
        _create_corrected_json(outputs_dir, "meeting_test")

        with TestClient(app) as client:
            response = client.get("/api/meetings/meeting_test/transcript")

        assert response.status_code == 200
        data = response.json()
        assert data["meeting_id"] == "meeting_test"
        assert data["num_speakers"] == 2
        assert data["total_utterances"] == 3
        assert len(data["utterances"]) == 3
        assert len(data["speakers"]) == 2
        assert "SPEAKER_00" in data["speakers"]
        assert "SPEAKER_01" in data["speakers"]

    def test_전사문_발화_필드_검증(self, tmp_path: Path) -> None:
        """전사문 발화 항목의 필드가 올바른지 확인한다."""
        app = _make_test_app(tmp_path)
        outputs_dir = tmp_path / "outputs"
        _create_corrected_json(outputs_dir, "meeting_test")

        with TestClient(app) as client:
            response = client.get("/api/meetings/meeting_test/transcript")

        data = response.json()
        first = data["utterances"][0]
        assert first["text"] == "안녕하세요, 오늘 회의를 시작하겠습니다."
        assert first["original_text"] == "안녕하세요 오늘 회의를 시작 하겠습니다"
        assert first["speaker"] == "SPEAKER_00"
        assert first["start"] == 0.5
        assert first["end"] == 3.2
        assert first["was_corrected"] is True

    def test_전사문_미존재_404(self, tmp_path: Path) -> None:
        """전사문 파일이 없을 때 404를 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.get("/api/meetings/nonexistent/transcript")

        assert response.status_code == 404

    def test_전사문_잘못된_meeting_id_400(self, tmp_path: Path) -> None:
        """path traversal이 포함된 meeting_id일 때 400을 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.get("/api/meetings/../etc/passwd/transcript")

        assert response.status_code in (400, 404, 422)

    def test_전사문_화자_순서_보존(self, tmp_path: Path) -> None:
        """화자 목록이 발화 순서대로 생성되는지 확인한다."""
        app = _make_test_app(tmp_path)
        outputs_dir = tmp_path / "outputs"
        _create_corrected_json(outputs_dir, "meeting_test")

        with TestClient(app) as client:
            response = client.get("/api/meetings/meeting_test/transcript")

        data = response.json()
        # SPEAKER_00이 먼저 등장하므로 첫 번째
        assert data["speakers"][0] == "SPEAKER_00"
        assert data["speakers"][1] == "SPEAKER_01"


# === TestSummaryEndpoint ===


class TestSummaryEndpoint:
    """GET /api/meetings/{meeting_id}/summary 엔드포인트 테스트."""

    def test_회의록_정상_조회(self, tmp_path: Path) -> None:
        """회의록 마크다운과 메타데이터가 정상 반환되는지 확인한다."""
        app = _make_test_app(tmp_path)
        outputs_dir = tmp_path / "outputs"
        _create_summary_files(outputs_dir, "meeting_test")

        with TestClient(app) as client:
            response = client.get("/api/meetings/meeting_test/summary")

        assert response.status_code == 200
        data = response.json()
        assert data["meeting_id"] == "meeting_test"
        assert "## 회의 개요" in data["markdown"]
        assert data["num_speakers"] == 2
        assert "SPEAKER_00" in data["speakers"]
        assert data["num_utterances"] == 3
        assert data["created_at"] == "2026-03-04T14:00:00"

    def test_회의록_md만_있을때(self, tmp_path: Path) -> None:
        """summary.json 없이 summary.md만 있을 때도 동작하는지 확인한다."""
        app = _make_test_app(tmp_path)
        outputs_dir = tmp_path / "outputs"
        meeting_dir = outputs_dir / "md_only_test"
        meeting_dir.mkdir(parents=True, exist_ok=True)

        md_path = meeting_dir / "summary.md"
        md_path.write_text("## 간단 회의록\n- 내용\n", encoding="utf-8")

        with TestClient(app) as client:
            response = client.get("/api/meetings/md_only_test/summary")

        assert response.status_code == 200
        data = response.json()
        assert "## 간단 회의록" in data["markdown"]
        assert data["num_speakers"] == 0  # 메타 없으므로 기본값

    def test_회의록_json만_있을때(self, tmp_path: Path) -> None:
        """summary.md 없이 summary.json만 있을 때 JSON의 markdown 필드를 사용하는지 확인."""
        app = _make_test_app(tmp_path)
        outputs_dir = tmp_path / "outputs"
        meeting_dir = outputs_dir / "json_only_test"
        meeting_dir.mkdir(parents=True, exist_ok=True)

        json_path = meeting_dir / "summary.json"
        meta = {
            "markdown": "## JSON 내 회의록\n- 내용\n",
            "num_speakers": 1,
            "speakers": ["SPEAKER_00"],
            "num_utterances": 5,
            "created_at": "2026-03-04T15:00:00",
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)

        with TestClient(app) as client:
            response = client.get("/api/meetings/json_only_test/summary")

        assert response.status_code == 200
        data = response.json()
        assert "## JSON 내 회의록" in data["markdown"]

    def test_회의록_미존재_404(self, tmp_path: Path) -> None:
        """회의록 파일이 없을 때 404를 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.get("/api/meetings/nonexistent/summary")

        assert response.status_code == 404

    def test_회의록_path_traversal_방지(self, tmp_path: Path) -> None:
        """path traversal 공격을 차단하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.get("/api/meetings/..%2F..%2Fetc%2Fpasswd/summary")

        assert response.status_code in (400, 404, 422)


# === TestRecordingEndpoints ===


class TestRecordingEndpoints:
    """녹음 API 엔드포인트 테스트."""

    def _setup_recorder(self, app: Any, is_recording: bool = False) -> MagicMock:
        """테스트용 AudioRecorder 모킹을 설정한다.

        Args:
            app: FastAPI 앱 인스턴스
            is_recording: 현재 녹음 상태

        Returns:
            모킹된 recorder 인스턴스
        """
        mock_recorder = MagicMock()
        mock_recorder.is_recording = is_recording
        mock_recorder.current_duration = 0.0 if not is_recording else 120.5
        mock_recorder.state = MagicMock()
        mock_recorder.state.value = "idle" if not is_recording else "recording"
        mock_recorder.get_status = MagicMock(
            return_value={
                "state": "idle" if not is_recording else "recording",
                "is_recording": is_recording,
                "duration_seconds": 0.0 if not is_recording else 120.5,
                "audio_device": None,
                "file_path": None,
            }
        )
        mock_recorder.detect_audio_devices = AsyncMock(return_value=[])
        mock_recorder.start_recording = AsyncMock()
        mock_recorder.stop_recording = AsyncMock()
        mock_recorder.cleanup = AsyncMock()
        app.state.recorder = mock_recorder
        return mock_recorder

    def test_recording_status_조회(self, tmp_path: Path) -> None:
        """GET /api/recording/status가 녹음 상태를 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            self._setup_recorder(app)
            response = client.get("/api/recording/status")

        assert response.status_code == 200
        data = response.json()
        assert "state" in data
        assert "is_recording" in data

    def test_recording_status_녹음중(self, tmp_path: Path) -> None:
        """녹음 중 상태가 올바르게 반환되는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            self._setup_recorder(app, is_recording=True)
            response = client.get("/api/recording/status")

        assert response.status_code == 200
        data = response.json()
        assert data["is_recording"] is True

    def test_recording_start_성공(self, tmp_path: Path) -> None:
        """POST /api/recording/start가 녹음을 시작하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            mock_recorder = self._setup_recorder(app)
            response = client.post("/api/recording/start")

        assert response.status_code == 200
        mock_recorder.start_recording.assert_called_once()

    def test_recording_start_이미_녹음중_409(self, tmp_path: Path) -> None:
        """이미 녹음 중일 때 POST /api/recording/start가 409를 반환하는지 확인한다."""
        from steps.recorder import AlreadyRecordingError

        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            mock_recorder = self._setup_recorder(app, is_recording=True)
            mock_recorder.start_recording = AsyncMock(
                side_effect=AlreadyRecordingError("이미 녹음 중"),
            )
            response = client.post("/api/recording/start")

        assert response.status_code == 409

    def test_recording_stop_성공(self, tmp_path: Path) -> None:
        """POST /api/recording/stop이 녹음을 정지하는지 확인한다."""
        from steps.recorder import RecordingResult

        app = _make_test_app(tmp_path)

        mock_result = MagicMock(spec=RecordingResult)
        mock_result.file_path = Path("/tmp/test.wav")
        mock_result.duration_seconds = 60.0
        mock_result.audio_device = "MacBook Air 마이크"
        mock_result.file_size_bytes = 1920000

        with TestClient(app) as client:
            mock_recorder = self._setup_recorder(app, is_recording=True)
            mock_recorder.stop_recording = AsyncMock(return_value=mock_result)
            response = client.post("/api/recording/stop")

        assert response.status_code == 200
        mock_recorder.stop_recording.assert_called_once()

    def test_recording_devices_조회(self, tmp_path: Path) -> None:
        """GET /api/recording/devices가 오디오 장치 목록을 반환하는지 확인한다."""
        from steps.recorder import AudioDevice

        app = _make_test_app(tmp_path)

        mock_devices = [
            AudioDevice(index=0, name="MacBook Air 마이크", is_blackhole=False),
            AudioDevice(index=1, name="BlackHole 2ch", is_blackhole=True),
        ]

        with TestClient(app) as client:
            mock_recorder = self._setup_recorder(app)
            mock_recorder.detect_audio_devices = AsyncMock(
                return_value=mock_devices,
            )
            response = client.get("/api/recording/devices")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[1]["name"] == "BlackHole 2ch"
        assert data[1]["is_blackhole"] is True

    def test_recording_미초기화_503(self, tmp_path: Path) -> None:
        """recorder가 None일 때 503을 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.recorder = None
            response = client.get("/api/recording/status")

        assert response.status_code == 503

    def test_status_응답에_is_recording_포함(self, tmp_path: Path) -> None:
        """GET /api/status 응답에 is_recording 필드가 포함되는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.job_queue.count_by_status = AsyncMock(
                return_value={"completed": 1},
            )
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=[])
            self._setup_recorder(app, is_recording=True)

            response = client.get("/api/status")

        assert response.status_code == 200
        data = response.json()
        assert "is_recording" in data
        assert data["is_recording"] is True
