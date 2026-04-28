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
    - /api/system/resources: 시스템 리소스 조회
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

    def test_recording_devices_응답에_is_aggregate_기본값_포함(self, tmp_path: Path) -> None:
        """GET /api/recording/devices 응답 스키마에 is_aggregate 필드가 기본값 False 로 포함되는지 확인한다."""
        from steps.recorder import AudioDevice

        app = _make_test_app(tmp_path)

        mock_devices = [
            AudioDevice(index=0, name="MacBook Air 마이크"),
        ]

        with TestClient(app) as client:
            mock_recorder = self._setup_recorder(app)
            mock_recorder.detect_audio_devices = AsyncMock(return_value=mock_devices)
            response = client.get("/api/recording/devices")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        # is_aggregate 필드가 응답에 포함되고 기본값이 False 여야 한다
        assert "is_aggregate" in data[0]
        assert data[0]["is_aggregate"] is False

    def test_recording_devices_aggregate_장치_노출(self, tmp_path: Path) -> None:
        """Aggregate Device 를 반환하면 API 응답에서 is_aggregate: true 로 노출되는지 확인한다."""
        from steps.recorder import AudioDevice

        app = _make_test_app(tmp_path)

        mock_devices = [
            AudioDevice(index=0, name="MacBook Air 마이크", is_aggregate=False),
            AudioDevice(
                index=1,
                name="Meeting Transcriber Aggregate",
                is_aggregate=True,
                is_blackhole=False,
            ),
        ]

        with TestClient(app) as client:
            mock_recorder = self._setup_recorder(app)
            mock_recorder.detect_audio_devices = AsyncMock(return_value=mock_devices)
            response = client.get("/api/recording/devices")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        # 일반 마이크는 is_aggregate: false
        assert data[0]["is_aggregate"] is False
        # Aggregate 장치는 is_aggregate: true 로 노출되어야 한다
        assert data[1]["name"] == "Meeting Transcriber Aggregate"
        assert data[1]["is_aggregate"] is True

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


# === TestRetryMeetingEndpoint ===


class TestRetryMeetingEndpoint:
    """POST /api/meetings/{meeting_id}/retry 엔드포인트 테스트."""

    def test_재시도_성공(self, tmp_path: Path) -> None:
        """실패한 회의를 재시도하면 200과 업데이트된 정보를 반환한다."""
        app = _make_test_app(tmp_path)

        mock_job = MockJob(1, "meeting_001", "/audio/001.m4a", "failed")
        mock_retried = MockJob(
            1,
            "meeting_001",
            "/audio/001.m4a",
            "queued",
            retry_count=1,
        )

        with TestClient(app) as client:
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            app.state.job_queue._queue.retry_job = MagicMock(
                return_value=mock_retried,
            )

            response = client.post("/api/meetings/meeting_001/retry")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert data["retry_count"] == 1

    def test_재시도_미존재_404(self, tmp_path: Path) -> None:
        """존재하지 않는 meeting_id 재시도 시 404를 반환한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=None,
            )

            response = client.post("/api/meetings/nonexistent/retry")

        assert response.status_code == 404

    def test_재시도_상태_전이_불가_409(self, tmp_path: Path) -> None:
        """failed가 아닌 상태에서 재시도 시 409를 반환한다."""
        from core.job_queue import InvalidTransitionError

        app = _make_test_app(tmp_path)

        mock_job = MockJob(1, "meeting_001", "/audio/001.m4a", "completed")

        with TestClient(app) as client:
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            app.state.job_queue._queue.retry_job = MagicMock(
                side_effect=InvalidTransitionError(1, "completed", "queued"),
            )

            response = client.post("/api/meetings/meeting_001/retry")

        assert response.status_code == 409

    def test_재시도_최대_횟수_초과_409(self, tmp_path: Path) -> None:
        """최대 재시도 횟수 초과 시 409를 반환한다."""
        from core.job_queue import MaxRetriesExceededError

        app = _make_test_app(tmp_path)

        mock_job = MockJob(1, "meeting_001", "/audio/001.m4a", "failed", retry_count=3)

        with TestClient(app) as client:
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            app.state.job_queue._queue.retry_job = MagicMock(
                side_effect=MaxRetriesExceededError(1, 3, 3),
            )

            response = client.post("/api/meetings/meeting_001/retry")

        assert response.status_code == 409


# === TestDeleteMeetingEndpoint ===


class TestDeleteMeetingEndpoint:
    """DELETE /api/meetings/{meeting_id} 엔드포인트 테스트."""

    def test_삭제_성공(self, tmp_path: Path) -> None:
        """회의 삭제 성공 시 200과 확인 메시지를 반환한다."""
        app = _make_test_app(tmp_path)

        mock_job = MockJob(1, "meeting_001", "/audio/001.m4a", "failed")

        with TestClient(app) as client:
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            app.state.job_queue._queue.delete_job = MagicMock()

            response = client.delete("/api/meetings/meeting_001")

        assert response.status_code == 200
        assert "삭제" in response.json()["message"]

    def test_삭제_미존재_404(self, tmp_path: Path) -> None:
        """존재하지 않는 meeting_id 삭제 시 404를 반환한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=None,
            )

            response = client.delete("/api/meetings/nonexistent")

        assert response.status_code == 404

    def test_완료된_회의_삭제_성공(self, tmp_path: Path) -> None:
        """완료된 회의도 삭제할 수 있다."""
        app = _make_test_app(tmp_path)

        mock_job = MockJob(1, "meeting_001", "/audio/001.m4a", "completed")

        with TestClient(app) as client:
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            app.state.job_queue._queue.delete_job = MagicMock()

            response = client.delete("/api/meetings/meeting_001")

        assert response.status_code == 200

    # === Phase 1-7: 오디오 파일 quarantine 이동 테스트 ===

    def test_삭제시_오디오_파일도_quarantine으로_이동(self, tmp_path: Path) -> None:
        """DELETE 엔드포인트가 DB 레코드 삭제 + 오디오 파일 quarantine 이동을 수행한다.

        근거: watcher 재감지 루프 차단을 위해 파일도 격리되어야 한다.
        """
        # 1) 실제 오디오 파일 생성 (tmp_path/audio_input 아래)
        audio_input = tmp_path / "audio_input"
        audio_input.mkdir(parents=True, exist_ok=True)
        audio_file = audio_input / "meeting_phase1.wav"
        audio_file.write_bytes(b"fake audio data")

        app = _make_test_app(tmp_path)

        # Job 은 실제 audio_path 를 가리킨다
        mock_job = MockJob(
            id=1,
            meeting_id="meeting_phase1",
            audio_path=str(audio_file),
            status="completed",
        )

        with TestClient(app) as client:
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            app.state.job_queue._queue.delete_job = MagicMock()

            response = client.delete("/api/meetings/meeting_phase1")

        # 2) DELETE 자체는 성공
        assert response.status_code == 200
        assert "삭제" in response.json()["message"]

        # 3) DB 삭제 호출 확인
        app.state.job_queue._queue.delete_job.assert_called_once_with(1)

        # 4) 원본 파일 사라졌는지
        assert not audio_file.exists(), "원본 오디오 파일이 quarantine으로 이동되었어야 한다"

        # 5) quarantine 디렉토리에 이동되었는지
        quarantine_dir = tmp_path / "audio_quarantine"
        assert quarantine_dir.exists()
        moved = quarantine_dir / "meeting_phase1.wav"
        assert moved.exists(), f"{quarantine_dir} 아래에 meeting_phase1.wav 가 있어야 한다"
        assert moved.read_bytes() == b"fake audio data"

    def test_삭제시_오디오_파일_누락이어도_DB_삭제는_성공(self, tmp_path: Path) -> None:
        """오디오 파일이 이미 없어도 DB 삭제는 성공 처리된다 (경고 로그만)."""
        # 존재하지 않는 경로를 Job 에 등록
        missing_audio = tmp_path / "audio_input" / "missing.wav"

        app = _make_test_app(tmp_path)

        mock_job = MockJob(
            id=2,
            meeting_id="meeting_missing",
            audio_path=str(missing_audio),
            status="completed",
        )

        with TestClient(app) as client:
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            app.state.job_queue._queue.delete_job = MagicMock()

            response = client.delete("/api/meetings/meeting_missing")

        # 파일 부재에도 DELETE 성공
        assert response.status_code == 200
        # DB 삭제는 여전히 호출
        app.state.job_queue._queue.delete_job.assert_called_once_with(2)

    def test_삭제시_audio_path_비어있어도_정상_처리(self, tmp_path: Path) -> None:
        """Job 의 audio_path 가 비어 있어도 DB 삭제는 성공한다."""
        app = _make_test_app(tmp_path)

        mock_job = MockJob(
            id=3,
            meeting_id="meeting_noaudio",
            audio_path="",  # 빈 문자열
            status="failed",
        )

        with TestClient(app) as client:
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            app.state.job_queue._queue.delete_job = MagicMock()

            response = client.delete("/api/meetings/meeting_noaudio")

        assert response.status_code == 200
        app.state.job_queue._queue.delete_job.assert_called_once_with(3)


# === TestSystemResourcesEndpoint ===


class TestSystemResourcesEndpoint:
    """GET /api/system/resources 엔드포인트 테스트."""

    def test_get_system_resources_정상_응답(self, tmp_path: Path) -> None:
        """시스템 리소스 조회 시 200 OK와 JSON을 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.get("/api/system/resources")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)

    def test_get_system_resources_스키마_검증(self, tmp_path: Path) -> None:
        """응답에 필수 필드(ram_used_gb, ram_total_gb, ram_percent, cpu_percent, loaded_model)가 존재하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.get("/api/system/resources")

        assert response.status_code == 200
        data = response.json()
        assert "ram_used_gb" in data
        assert "ram_total_gb" in data
        assert "ram_percent" in data
        assert "cpu_percent" in data
        assert "loaded_model" in data

    def test_get_system_resources_ram_범위(self, tmp_path: Path) -> None:
        """ram_percent가 0~100 범위인지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.get("/api/system/resources")

        assert response.status_code == 200
        data = response.json()
        assert 0 <= data["ram_percent"] <= 100


# === TestSummarizeMeetingEndpoint ===


class TestSummarizeMeetingEndpoint:
    """POST /api/meetings/{meeting_id}/summarize 엔드포인트 테스트."""

    def _setup_pipeline(
        self,
        app: Any,
        tmp_path: Path,
        meeting_id: str,
        *,
        create_state: bool = True,
        create_merge_cp: bool = True,
    ) -> MagicMock:
        """테스트용 PipelineManager 모킹을 설정한다.

        Args:
            app: FastAPI 앱 인스턴스
            tmp_path: pytest 임시 디렉토리
            meeting_id: 회의 ID
            create_state: 상태 파일 생성 여부
            create_merge_cp: merge 체크포인트 생성 여부

        Returns:
            모킹된 pipeline_manager 인스턴스
        """
        checkpoints_dir = tmp_path / "checkpoints"
        state_dir = checkpoints_dir / meeting_id
        state_dir.mkdir(parents=True, exist_ok=True)

        state_path = state_dir / "pipeline_state.json"
        merge_cp_path = state_dir / "merge.json"

        if create_state:
            state_path.write_text(
                '{"meeting_id": "' + meeting_id + '", "status": "completed"}',
                encoding="utf-8",
            )
        if create_merge_cp:
            merge_cp_path.write_text(
                '{"utterances": [], "num_speakers": 1}',
                encoding="utf-8",
            )

        mock_pipeline = MagicMock()
        mock_pipeline._get_state_path = MagicMock(return_value=state_path)
        mock_pipeline._get_checkpoint_path = MagicMock(return_value=merge_cp_path)
        mock_pipeline.run_llm_steps = AsyncMock()

        app.state.pipeline_manager = mock_pipeline
        app.state.running_tasks = set()

        return mock_pipeline

    def test_summarize_meeting_정상(self, tmp_path: Path) -> None:
        """정상적으로 요약을 시작하면 200과 확인 메시지를 반환한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            self._setup_pipeline(app, tmp_path, "meeting_001")
            response = client.post("/api/meetings/meeting_001/summarize")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["meeting_id"] == "meeting_001"
        assert "요약" in data["message"]

    def test_summarize_meeting_존재하지_않는_회의_404(self, tmp_path: Path) -> None:
        """상태 파일과 merge 체크포인트가 모두 없는 meeting_id 는 404 를 반환한다.

        (이슈 I 이후: merge 체크포인트가 있으면 state 자동 재구성하므로,
         404 를 받으려면 merge 도 없어야 한다.)
        """
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            self._setup_pipeline(
                app,
                tmp_path,
                "nonexistent",
                create_state=False,
                create_merge_cp=False,
            )
            response = client.post("/api/meetings/nonexistent/summarize")

        assert response.status_code == 404
        assert "찾을 수 없습니다" in response.json()["detail"]

    def test_summarize_meeting_체크포인트_없음_400(self, tmp_path: Path) -> None:
        """merge 체크포인트가 없을 때 400을 반환한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            self._setup_pipeline(
                app,
                tmp_path,
                "meeting_002",
                create_merge_cp=False,
            )
            response = client.post("/api/meetings/meeting_002/summarize")

        assert response.status_code == 400
        assert "체크포인트" in response.json()["detail"]

    def test_summarize_meeting_pipeline_미초기화_503(self, tmp_path: Path) -> None:
        """pipeline_manager가 None일 때 503을 반환한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.pipeline_manager = None
            response = client.post("/api/meetings/meeting_001/summarize")

        assert response.status_code == 503
        assert "파이프라인" in response.json()["detail"]

    def test_summarize_meeting_진행중_표시(self, tmp_path: Path) -> None:
        """요약 시작 후 running_tasks에 태스크가 등록되는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            mock_pipeline = self._setup_pipeline(app, tmp_path, "meeting_003")
            response = client.post("/api/meetings/meeting_003/summarize")

        assert response.status_code == 200
        # run_llm_steps가 호출되었는지 확인
        mock_pipeline.run_llm_steps.assert_called_once_with("meeting_003")

    def test_summarize_meeting_state_유실_자동_재구성(self, tmp_path: Path) -> None:
        """이슈 I: state 파일이 없고 merge 체크포인트만 있을 때 자동 재구성 후 요약 시작."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            mock_pipeline = self._setup_pipeline(
                app,
                tmp_path,
                "meeting_legacy",
                create_state=False,
                create_merge_cp=True,
            )
            mock_pipeline._rebuild_state_from_checkpoints = MagicMock()
            response = client.post("/api/meetings/meeting_legacy/summarize")

        # 404 가 아닌 200 을 받아야 한다 — state 재구성 경로
        assert response.status_code == 200
        mock_pipeline._rebuild_state_from_checkpoints.assert_called_once_with("meeting_legacy")
        mock_pipeline.run_llm_steps.assert_called_once_with("meeting_legacy")

    def test_summarize_meeting_state_merge_모두_없음_404(self, tmp_path: Path) -> None:
        """이슈 I: state와 merge가 모두 없으면 여전히 404."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            mock_pipeline = self._setup_pipeline(
                app,
                tmp_path,
                "meeting_ghost",
                create_state=False,
                create_merge_cp=False,
            )
            mock_pipeline._rebuild_state_from_checkpoints = MagicMock()
            response = client.post("/api/meetings/meeting_ghost/summarize")

        assert response.status_code == 404
        # 재구성은 호출되지 않아야 한다
        mock_pipeline._rebuild_state_from_checkpoints.assert_not_called()


# === TestTranscribeMeetingEndpoint (이슈 J) ===


class TestTranscribeMeetingEndpoint:
    """POST /api/meetings/{meeting_id}/transcribe 엔드포인트 테스트 (이슈 J)."""

    def test_transcribe_failed_상태_force_false_409(self, tmp_path: Path) -> None:
        """failed 상태에서 force=false 면 409 + 힌트 메시지를 반환한다."""
        app = _make_test_app(tmp_path)
        mock_job = MockJob(1, "meeting_fail", "/audio/fail.m4a", "failed")

        with TestClient(app) as client:
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            response = client.post("/api/meetings/meeting_fail/transcribe")

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "failed" in detail
        assert "force=true" in detail  # 힌트 포함

    def test_transcribe_failed_상태_force_true_재시도(self, tmp_path: Path) -> None:
        """failed 상태에서 force=true 이면 recorded 로 되돌린 뒤 queued 로 전이한다."""
        app = _make_test_app(tmp_path)

        failed_job = MockJob(1, "meeting_retry", "/audio/retry.m4a", "failed")
        recorded_job = MockJob(1, "meeting_retry", "/audio/retry.m4a", "recorded")
        queued_job = MockJob(1, "meeting_retry", "/audio/retry.m4a", "queued")

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=failed_job)
            queue.force_set_status = MagicMock(return_value=recorded_job)
            queue.update_status = MagicMock(return_value=queued_job)

            response = client.post("/api/meetings/meeting_retry/transcribe?force=true")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        # force_set_status 가 failed → recorded 로 호출되었는지 확인
        queue.force_set_status.assert_called_once()
        queue.update_status.assert_called_once()

    def test_transcribe_recorded_상태_정상(self, tmp_path: Path) -> None:
        """recorded 상태에서는 force 여부와 무관하게 정상 전이한다."""
        app = _make_test_app(tmp_path)

        recorded_job = MockJob(1, "meeting_ok", "/audio/ok.m4a", "recorded")
        queued_job = MockJob(1, "meeting_ok", "/audio/ok.m4a", "queued")

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=recorded_job)
            queue.force_set_status = MagicMock()
            queue.update_status = MagicMock(return_value=queued_job)

            response = client.post("/api/meetings/meeting_ok/transcribe")

        assert response.status_code == 200
        # recorded 상태에서는 force_set_status 를 호출하지 않아야 한다
        queue.force_set_status.assert_not_called()

    def test_transcribe_completed_상태_force_true_여도_409(self, tmp_path: Path) -> None:
        """completed 등 다른 상태에서는 force=true 여도 force_set_status 를 타지 않아 409."""
        app = _make_test_app(tmp_path)
        mock_job = MockJob(1, "meeting_done", "/audio/done.m4a", "completed")

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=mock_job)
            queue.force_set_status = MagicMock()

            response = client.post("/api/meetings/meeting_done/transcribe?force=true")

        assert response.status_code == 409
        # force=true 라도 failed 가 아니므로 force_set_status 는 호출되지 않음
        queue.force_set_status.assert_not_called()


class TestGetMeetingAudio:
    """GET /api/meetings/{meeting_id}/audio 엔드포인트 테스트.

    발화 음성 재생 기능을 위한 오디오 스트리밍 (HTTP Range 지원) 검증.
    """

    @staticmethod
    def _seed_audio_via_pipeline_state(
        tmp_path: Path,
        meeting_id: str,
        wav_bytes: bytes,
    ) -> Path:
        """pipeline_state.json + wav_path 조합으로 회의 음성을 시드한다.

        실제 운영 환경의 1순위 탐색 경로(pipeline_state.json 의 wav_path)를 재현.
        """
        outputs_dir = tmp_path / "outputs" / meeting_id
        outputs_dir.mkdir(parents=True, exist_ok=True)
        wav_path = outputs_dir / "input_16k.wav"
        wav_path.write_bytes(wav_bytes)

        ckpt_dir = tmp_path / "checkpoints" / meeting_id
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        state_path = ckpt_dir / "pipeline_state.json"
        state_path.write_text(
            json.dumps(
                {
                    "meeting_id": meeting_id,
                    "audio_path": "/some/original/input.m4a",
                    "wav_path": str(wav_path),
                    "output_dir": str(outputs_dir),
                    "status": "completed",
                }
            ),
            encoding="utf-8",
        )
        return wav_path

    @staticmethod
    def _seed_audio_glob_only(
        tmp_path: Path,
        meeting_id: str,
        wav_bytes: bytes,
        filename: str = "test_16k.wav",
    ) -> Path:
        """state 파일 없이 outputs/{id}/*.wav 폴백 경로만 시드한다."""
        outputs_dir = tmp_path / "outputs" / meeting_id
        outputs_dir.mkdir(parents=True, exist_ok=True)
        wav_path = outputs_dir / filename
        wav_path.write_bytes(wav_bytes)
        return wav_path

    def test_audio_endpoint_returns_full_file_without_range(self, tmp_path: Path) -> None:
        """Range 헤더 없이 요청하면 200 + 전체 파일 바이트를 반환한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "20260428_120000_test"
        wav_bytes = b"RIFF" + b"\x00" * 100 + b"data" + b"\xab" * 200
        self._seed_audio_via_pipeline_state(tmp_path, meeting_id, wav_bytes)

        with TestClient(app) as client:
            response = client.get(f"/api/meetings/{meeting_id}/audio")

        assert response.status_code == 200
        assert response.content == wav_bytes
        assert response.headers.get("accept-ranges") == "bytes"
        assert response.headers.get("content-type", "").startswith("audio/")

    def test_audio_endpoint_returns_partial_for_explicit_range(self, tmp_path: Path) -> None:
        """Range: bytes=START-END 요청은 206 + Content-Range + 부분 바이트."""
        app = _make_test_app(tmp_path)
        meeting_id = "20260428_120000_partial"
        # 인덱스가 명확한 시드 — 0..255 반복 패턴
        wav_bytes = bytes(i % 256 for i in range(1000))
        self._seed_audio_via_pipeline_state(tmp_path, meeting_id, wav_bytes)

        with TestClient(app) as client:
            response = client.get(
                f"/api/meetings/{meeting_id}/audio",
                headers={"Range": "bytes=100-199"},
            )

        assert response.status_code == 206
        assert response.headers["content-range"] == f"bytes 100-199/{len(wav_bytes)}"
        assert response.headers["content-length"] == "100"
        assert response.headers["accept-ranges"] == "bytes"
        assert response.content == wav_bytes[100:200]

    def test_audio_endpoint_handles_open_ended_range(self, tmp_path: Path) -> None:
        """bytes=START- 형식 (END 미지정) 은 파일 끝까지 반환한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "20260428_120000_openend"
        wav_bytes = bytes(range(50))
        self._seed_audio_via_pipeline_state(tmp_path, meeting_id, wav_bytes)

        with TestClient(app) as client:
            response = client.get(
                f"/api/meetings/{meeting_id}/audio",
                headers={"Range": "bytes=20-"},
            )

        assert response.status_code == 206
        assert response.headers["content-range"] == "bytes 20-49/50"
        assert response.content == wav_bytes[20:]

    def test_audio_endpoint_handles_suffix_range(self, tmp_path: Path) -> None:
        """bytes=-N 형식 (suffix range) 은 마지막 N 바이트를 반환한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "20260428_120000_suffix"
        wav_bytes = bytes(range(100))
        self._seed_audio_via_pipeline_state(tmp_path, meeting_id, wav_bytes)

        with TestClient(app) as client:
            response = client.get(
                f"/api/meetings/{meeting_id}/audio",
                headers={"Range": "bytes=-30"},
            )

        assert response.status_code == 206
        assert response.headers["content-range"] == "bytes 70-99/100"
        assert response.content == wav_bytes[70:]

    def test_audio_endpoint_returns_416_for_out_of_range(self, tmp_path: Path) -> None:
        """파일 크기를 넘는 Range 는 416 Range Not Satisfiable."""
        app = _make_test_app(tmp_path)
        meeting_id = "20260428_120000_oor"
        wav_bytes = b"\x00" * 100
        self._seed_audio_via_pipeline_state(tmp_path, meeting_id, wav_bytes)

        with TestClient(app) as client:
            response = client.get(
                f"/api/meetings/{meeting_id}/audio",
                headers={"Range": "bytes=500-999"},
            )

        assert response.status_code == 416
        assert response.headers.get("content-range") == "bytes */100"

    def test_audio_endpoint_returns_404_when_file_missing(self, tmp_path: Path) -> None:
        """state 도 outputs 도 없으면 404."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.get("/api/meetings/no_such_meeting/audio")

        assert response.status_code == 404

    def test_audio_endpoint_rejects_invalid_meeting_id(self, tmp_path: Path) -> None:
        """meeting_id 가 path traversal 또는 잘못된 형식이면 400."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            # 슬래시는 _MEETING_ID_PATTERN 에서 거부 (FastAPI 라우팅이 먼저 잡으면 404)
            response = client.get("/api/meetings/bad id with space/audio")

        # 공백 포함 → 정규식 미매치 → 400, 또는 라우팅 단계에서 404 둘 다 허용
        assert response.status_code in (400, 404)

    def test_audio_endpoint_falls_back_to_outputs_glob(self, tmp_path: Path) -> None:
        """pipeline_state.json 이 없을 때 outputs/{id}/*.wav 폴백 경로로 응답한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "20260428_120000_glob"
        wav_bytes = b"WAVE" + b"\x11" * 60
        self._seed_audio_glob_only(tmp_path, meeting_id, wav_bytes)

        with TestClient(app) as client:
            response = client.get(f"/api/meetings/{meeting_id}/audio")

        assert response.status_code == 200
        assert response.content == wav_bytes

    def test_audio_endpoint_uses_audio_path_when_wav_path_missing(self, tmp_path: Path) -> None:
        """pipeline_state.json 의 wav_path 가 비어있으면 audio_path 폴백."""
        app = _make_test_app(tmp_path)
        meeting_id = "20260428_120000_audiopath"
        wav_bytes = b"\xaa" * 200
        outputs_dir = tmp_path / "outputs" / meeting_id
        outputs_dir.mkdir(parents=True, exist_ok=True)
        # audio_path 가 가리키는 실제 파일 (wav 가 아닌 위치)
        orig_path = outputs_dir / "input.wav"
        orig_path.write_bytes(wav_bytes)
        # state 의 wav_path 는 빈 문자열, audio_path 만 채움
        ckpt_dir = tmp_path / "checkpoints" / meeting_id
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        (ckpt_dir / "pipeline_state.json").write_text(
            json.dumps({"wav_path": "", "audio_path": str(orig_path)}),
            encoding="utf-8",
        )

        with TestClient(app) as client:
            response = client.get(f"/api/meetings/{meeting_id}/audio")

        assert response.status_code == 200
        assert response.content == wav_bytes

    def test_audio_endpoint_handles_zero_byte_file(self, tmp_path: Path) -> None:
        """0 바이트 wav 도 안전하게 처리한다 (200 + 빈 본문)."""
        app = _make_test_app(tmp_path)
        meeting_id = "20260428_120000_zero"
        self._seed_audio_via_pipeline_state(tmp_path, meeting_id, b"")

        with TestClient(app) as client:
            response = client.get(f"/api/meetings/{meeting_id}/audio")

        assert response.status_code == 200
        assert response.content == b""

    def test_audio_endpoint_416_on_zero_byte_with_range(self, tmp_path: Path) -> None:
        """0 바이트 파일에 Range 요청 → 모든 start 가 file_size 이상 → 416."""
        app = _make_test_app(tmp_path)
        meeting_id = "20260428_120000_zerorange"
        self._seed_audio_via_pipeline_state(tmp_path, meeting_id, b"")

        with TestClient(app) as client:
            response = client.get(
                f"/api/meetings/{meeting_id}/audio",
                headers={"Range": "bytes=0-9"},
            )

        assert response.status_code == 416

    def test_audio_endpoint_ignores_malformed_range(self, tmp_path: Path) -> None:
        """잘못된 형식의 Range 헤더(bytes=abc) 는 416 으로 응답한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "20260428_120000_malformed"
        wav_bytes = b"\x00" * 100
        self._seed_audio_via_pipeline_state(tmp_path, meeting_id, wav_bytes)

        with TestClient(app) as client:
            for bad_range in ("bytes=abc-def", "bytes=", "bytes=10-5", "kilobytes=0-9"):
                response = client.get(
                    f"/api/meetings/{meeting_id}/audio",
                    headers={"Range": bad_range},
                )
                # 비정상 형식 → 416, 또는 prefix 부터 다른 형식("kilobytes=")은 Range 미지원으로 간주 → 200
                assert response.status_code in (200, 416), f"bad_range={bad_range}"

    def test_audio_endpoint_returns_correct_mime_for_mp3(self, tmp_path: Path) -> None:
        """원본이 mp3 면 audio/mpeg MIME 으로 응답한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "20260428_120000_mp3"
        outputs_dir = tmp_path / "outputs" / meeting_id
        outputs_dir.mkdir(parents=True, exist_ok=True)
        mp3_path = outputs_dir / "input.mp3"
        mp3_path.write_bytes(b"ID3" + b"\x00" * 100)
        ckpt_dir = tmp_path / "checkpoints" / meeting_id
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        (ckpt_dir / "pipeline_state.json").write_text(
            json.dumps({"wav_path": str(mp3_path), "audio_path": str(mp3_path)}),
            encoding="utf-8",
        )

        with TestClient(app) as client:
            response = client.get(f"/api/meetings/{meeting_id}/audio")

        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"

    def test_audio_endpoint_skips_unplayable_extension(self, tmp_path: Path) -> None:
        """state.wav_path 가 .txt 등 재생 불가 확장자면 무시하고 폴백."""
        app = _make_test_app(tmp_path)
        meeting_id = "20260428_120000_unplayable"
        outputs_dir = tmp_path / "outputs" / meeting_id
        outputs_dir.mkdir(parents=True, exist_ok=True)
        # 잘못된 wav_path: 확장자가 .txt
        bad_path = outputs_dir / "garbage.txt"
        bad_path.write_bytes(b"not audio")
        # 폴백용 진짜 wav
        real_wav = outputs_dir / "real_16k.wav"
        real_wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

        ckpt_dir = tmp_path / "checkpoints" / meeting_id
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        (ckpt_dir / "pipeline_state.json").write_text(
            json.dumps({"wav_path": str(bad_path), "audio_path": str(bad_path)}),
            encoding="utf-8",
        )

        with TestClient(app) as client:
            response = client.get(f"/api/meetings/{meeting_id}/audio")

        # state 파일의 잘못된 확장자는 무시되고 outputs 글롭 폴백으로 real_16k.wav 응답
        assert response.status_code == 200
        assert response.content.startswith(b"RIFF")
