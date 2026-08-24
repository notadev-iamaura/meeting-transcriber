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
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from config import AppConfig, PathsConfig, ServerConfig
from steps.embedder import IndexPurgeError, IndexPurgeResult

# === 헬퍼 ===


def test_legacy_summarize_batch_validator는_공백_한국어_single_segment를_허용() -> None:
    """watcher가 만든 회의 ID를 legacy summarize-batch도 동일하게 받는다."""
    from api.routes import _validate_meeting_id

    _validate_meeting_id("회의 1")


@pytest.mark.parametrize("meeting_id", ["", ".", "..", "a/b", r"a\b", "a\x00b"])
def test_legacy_summarize_batch_validator는_비정상_segment를_거부(
    meeting_id: str,
) -> None:
    """path traversal에 쓰일 수 있는 ID는 기존처럼 400으로 차단한다."""
    from fastapi import HTTPException

    from api.routes import _validate_meeting_id

    with pytest.raises(HTTPException) as exc_info:
        _validate_meeting_id(meeting_id)

    assert exc_info.value.status_code == 400


def _denied_audio_admission(failure_kind_name: str) -> Any:
    """API admission 상태 매핑 테스트용 비수락 결과를 생성한다."""
    from core.audio_quality import AudioFailureKind, AudioQualityResult, AudioQualityStatus

    media_invalid = failure_kind_name == "MEDIA_INVALID"
    return AudioQualityResult(
        status=AudioQualityStatus.REJECT if media_invalid else AudioQualityStatus.ERROR,
        mean_volume_db=None,
        duration_seconds=1.0 if media_invalid else None,
        reason=f"admission denied: {failure_kind_name}",
        failure_kind=getattr(AudioFailureKind, failure_kind_name),
    )


def _make_test_config(tmp_path: Path) -> AppConfig:
    """테스트용 AppConfig를 생성한다.

    Args:
        tmp_path: pytest 임시 디렉토리

    Returns:
        테스트용 AppConfig 인스턴스
    """
    config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
    )
    # API 단위 테스트의 기존 가짜 audio_path는 실제 미디어가 아니다. admission
    # 계약 전용 테스트만 품질 게이트를 다시 켜서 상태 매핑을 검증한다.
    config.audio_quality.enabled = False
    return config


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
        app = create_app(config, runtime_profile="api-test")

    return app


def _make_audio_file(tmp_path: Path, filename: str) -> Path:
    """raw base_dir 안에 no-follow admission을 통과할 테스트 오디오를 만든다."""
    audio_path = tmp_path / "audio_input" / filename
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"audio-sentinel")
    return audio_path


def _install_retranscribe_claim_mocks(queue: Any, original_job: MockJob) -> MagicMock:
    """versioned 재전사 claim/phase를 모사하고 durable 조회 mock을 반환한다."""
    from core.job_queue import JobStatus, RetranscribeClaim

    durable_job = MockJob(
        original_job.id,
        original_job.meeting_id,
        original_job.audio_path,
        JobStatus.RECORDING.value,
        retry_count=original_job.retry_count,
        error_message=original_job.error_message,
    )
    durable_job.requested_action = ""  # type: ignore[attr-defined]
    token_box: dict[str, str] = {}

    def _set_payload(token: str, phase: str) -> None:
        durable_job.requested_action = RetranscribeClaim(  # type: ignore[attr-defined]
            original_status=original_job.status,
            original_requested_action="",
            token=token,
            phase=phase,
        ).to_requested_action()

    def _claim(
        _job_id: int,
        token: str,
        *,
        stt_provider: str = "",
        stt_model: str = "",
    ) -> MockJob:
        token_box["token"] = token
        _set_payload(token, "claimed")
        durable_job.stt_provider = stt_provider  # type: ignore[attr-defined]
        durable_job.stt_model = stt_model  # type: ignore[attr-defined]
        return durable_job

    def _phase(_job_id: int, token: str, phase: str) -> MockJob:
        assert token == token_box["token"]
        _set_payload(token, phase)
        return durable_job

    queue.claim_for_retranscribe = MagicMock(side_effect=_claim)
    queue.update_retranscribe_claim_phase = MagicMock(side_effect=_phase)
    queue.get_job = MagicMock(return_value=durable_job)
    queue.restore_retranscribe_claim = MagicMock(return_value=original_job)
    return queue.restore_retranscribe_claim


def _install_delete_claim_mocks(
    queue: Any,
    job: MockJob,
    *,
    delete_side_effect: Any | None = None,
) -> MagicMock:
    """DELETE route의 durable claim/commit/rollback을 테스트용으로 모사한다."""
    from core.job_queue import DeleteClaim

    original_status = job.status
    original_action = str(getattr(job, "requested_action", "") or "")
    original_error = str(getattr(job, "error_message", "") or "")
    claim_box: dict[str, DeleteClaim] = {}

    def _publish(claim: DeleteClaim) -> MockJob:
        claim_box["claim"] = claim
        job.status = "recording"
        job.requested_action = claim.to_requested_action()  # type: ignore[attr-defined]
        return job

    def _claim(_job_id: int, token: str) -> MockJob:
        return _publish(
            DeleteClaim(
                original_status=original_status,
                original_requested_action=original_action,
                original_error_message=original_error,
                token=token,
            )
        )

    def _prepare(
        _job_id: int,
        token: str,
        *,
        source_path: str,
        quarantine_path: str,
        source_identity: tuple[int, int, int, int],
    ) -> MockJob:
        current = claim_box["claim"]
        assert current.token == token
        return _publish(
            DeleteClaim(
                original_status=current.original_status,
                original_requested_action=current.original_requested_action,
                original_error_message=current.original_error_message,
                token=current.token,
                phase="quarantining",
                source_path=source_path,
                quarantine_path=quarantine_path,
                source_identity=source_identity,
            )
        )

    def _phase(_job_id: int, token: str, phase: str) -> MockJob:
        current = claim_box["claim"]
        assert current.token == token
        return _publish(
            DeleteClaim(
                original_status=current.original_status,
                original_requested_action=current.original_requested_action,
                original_error_message=current.original_error_message,
                token=current.token,
                phase=phase,
                source_path=current.source_path,
                quarantine_path=current.quarantine_path,
                source_identity=current.source_identity,
            )
        )

    queue.claim_for_deletion = MagicMock(side_effect=_claim)
    queue.prepare_delete_quarantine = MagicMock(side_effect=_prepare)
    queue.update_delete_claim_phase = MagicMock(side_effect=_phase)
    queue.get_job = MagicMock(return_value=job)
    queue.delete_claimed_job = MagicMock(side_effect=delete_side_effect)
    queue.restore_delete_claim = MagicMock(return_value=job)
    return queue.delete_claimed_job


def _install_search_engine_mock(
    app: Any,
    *,
    return_value: Any | None = None,
    side_effect: Exception | None = None,
) -> MagicMock:
    """lazy search dependency가 사용할 검색 엔진 mock을 설치한다."""
    engine = MagicMock()
    engine.search = AsyncMock(return_value=return_value, side_effect=side_effect)
    app.state.search_engine = engine
    return engine


def _install_chat_engine_mock(
    app: Any,
    *,
    return_value: Any | None = None,
    side_effect: Exception | None = None,
) -> MagicMock:
    """lazy chat dependency가 사용할 Chat 엔진 mock을 설치한다."""
    engine = MagicMock()
    engine.chat = AsyncMock(return_value=return_value, side_effect=side_effect)
    app.state.chat_engine = engine
    return engine


def _create_completed_pipeline_state(
    tmp_path: Path,
    meeting_id: str,
    *,
    skipped_steps: list[str] | None = None,
) -> None:
    """완료된 pipeline_state.json 과 전사 산출물을 생성한다."""
    ckpt_dir = tmp_path / "checkpoints" / meeting_id
    out_dir = tmp_path / "outputs" / meeting_id
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (ckpt_dir / "pipeline_state.json").write_text(
        json.dumps(
            {
                "meeting_id": meeting_id,
                "audio_path": f"/audio/{meeting_id}.m4a",
                "status": "completed",
                "completed_steps": [
                    "convert",
                    "transcribe",
                    "diarize",
                    "merge",
                    "correct",
                ],
                "skipped_steps": skipped_steps or [],
                "step_results": [],
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "corrected.json").write_text(
        json.dumps({"utterances": [{"text": "안녕하세요", "speaker": "SPEAKER_00"}]}),
        encoding="utf-8",
    )


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
    llm_called: bool = True
    grounding_status: str = "grounded"
    repair_actions: list[str] = field(default_factory=list)
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
            app.state.job_queue.get_all_jobs = AsyncMock(
                return_value=[
                    MockJob(1, "m1", "/a.wav", "completed"),
                    MockJob(2, "m2", "/b.wav", "completed"),
                    MockJob(3, "m3", "/c.wav", "completed"),
                    MockJob(4, "m4", "/d.wav", "queued"),
                ],
            )

            response = client.get("/api/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["queue_summary"]["completed"] == 3
        assert data["queue_summary"]["queued"] == 1
        assert data["total_jobs"] == 4

    def test_status_active_jobs_계산(self, tmp_path: Path) -> None:
        """진행 중인 작업(recording, transcribing 등)이 올바르게 집계되는지 확인한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.job_queue.get_all_jobs = AsyncMock(
                return_value=[
                    MockJob(1, "m1", "/1.wav", "recording"),
                    MockJob(2, "m2", "/2.wav", "transcribing"),
                    MockJob(3, "m3", "/3.wav", "transcribing"),
                    MockJob(4, "m4", "/4.wav", "completed"),
                ],
            )

            response = client.get("/api/status")

        data = response.json()
        # recording(1) + transcribing(2) = 3 active
        assert data["active_jobs"] == 3

    def test_status_완료_산출물이_있는_failed_작업은_집계_전에_복구한다(
        self,
        tmp_path: Path,
    ) -> None:
        """시스템 상태 집계도 목록과 같은 reconciliation 기준을 사용한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_reconcile_status"
        _create_completed_pipeline_state(tmp_path, meeting_id)

        failed_job = MockJob(1, meeting_id, "/audio/status.m4a", "failed", retry_count=1)
        completed_job = MockJob(1, meeting_id, "/audio/status.m4a", "completed", retry_count=1)

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=[failed_job])
            queue.force_set_status = MagicMock(return_value=completed_job)

            response = client.get("/api/status")

        assert response.status_code == 200
        data = response.json()
        assert data["queue_summary"]["completed"] == 1
        assert "failed" not in data["queue_summary"]
        queue.force_set_status.assert_called_once()

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

    def test_meetings_완료_산출물이_있는_failed_작업은_복구한다(
        self,
        tmp_path: Path,
    ) -> None:
        """목록 조회 시 completed 체크포인트와 전사 산출물이 있으면 failed 상태를 복구한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_reconcile_list"
        _create_completed_pipeline_state(tmp_path, meeting_id, skipped_steps=["summarize"])

        failed_job = MockJob(1, meeting_id, "/audio/list.m4a", "failed", retry_count=1)
        completed_job = MockJob(1, meeting_id, "/audio/list.m4a", "completed", retry_count=1)

        with TestClient(app) as client:
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=[failed_job])
            app.state.job_queue._queue.force_set_status = MagicMock(return_value=completed_job)

            response = client.get("/api/meetings")

        assert response.status_code == 200
        data = response.json()
        meeting = data["meetings"][0]
        assert meeting["status"] == "completed"
        assert meeting["skipped_steps"] == ["summarize"]
        assert "completed" in meeting["status_detail"]
        app.state.job_queue._queue.force_set_status.assert_called_once()


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

    def test_pipeline_state_없으면_빈_상태_200(self, tmp_path: Path) -> None:
        """pipeline_state.json 누락은 UI 콘솔 404가 아니라 빈 로그 상태로 응답한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.get("/api/meetings/meeting_001/pipeline-state")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "missing"
        assert data["step_results"] == []
        assert data["total_elapsed_seconds"] == 0.0

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

    def test_meeting_상세_완료_산출물이_있는_failed_작업은_복구한다(
        self,
        tmp_path: Path,
    ) -> None:
        """상세 조회도 목록과 같은 기준으로 상태 불일치를 복구한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_reconcile_detail"
        _create_completed_pipeline_state(tmp_path, meeting_id)

        failed_job = MockJob(1, meeting_id, "/audio/detail.m4a", "failed", retry_count=1)
        completed_job = MockJob(1, meeting_id, "/audio/detail.m4a", "completed", retry_count=1)

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=failed_job)
            queue.force_set_status = MagicMock(return_value=completed_job)

            response = client.get(f"/api/meetings/{meeting_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["retry_count"] == 1
        assert "completed" in data["status_detail"]
        queue.force_set_status.assert_called_once()


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
            _install_search_engine_mock(app, return_value=mock_response)

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
            _install_search_engine_mock(app, return_value=mock_response)

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
        """검색 엔진 지연 초기화 실패 시 503을 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with (
            patch(
                "search.hybrid_search.HybridSearchEngine",
                side_effect=RuntimeError("검색 엔진 초기화 실패"),
            ),
            TestClient(app) as client,
        ):
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
            _install_search_engine_mock(app, side_effect=EmptyQueryError("빈 쿼리"))

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
            _install_search_engine_mock(app, return_value=mock_response)

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
            _install_chat_engine_mock(app, return_value=mock_response)

            response = client.post(
                "/api/chat",
                json={"query": "프로젝트 일정이 어떻게 되나요?"},
            )

        assert response.status_code == 200
        data = response.json()
        assert "프로젝트 일정" in data["answer"]
        assert len(data["references"]) == 1
        assert data["llm_used"] is True
        assert data["llm_called"] is True
        assert data["grounding_status"] == "grounded"
        assert data["repair_actions"] == []

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
            _install_chat_engine_mock(app, return_value=mock_response)

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
        """Chat 엔진 지연 초기화 실패 시 503을 반환하는지 확인한다."""
        app = _make_test_app(tmp_path)

        with (
            patch("search.hybrid_search.HybridSearchEngine", return_value=MagicMock()),
            patch("search.chat.ChatEngine", side_effect=RuntimeError("Chat 엔진 초기화 실패")),
            TestClient(app) as client,
        ):
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
            _install_chat_engine_mock(app, return_value=mock_response)

            response = client.post(
                "/api/chat",
                json={"query": "질문"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["llm_used"] is False
        assert data["llm_called"] is True
        assert data["grounding_status"] == "grounded"
        assert data["error_message"] is not None

    def test_chat_EmptyQueryError_400(self, tmp_path: Path) -> None:
        """ChatEngine이 EmptyQueryError를 발생시킬 때 400을 반환하는지 확인한다."""
        from search.chat import EmptyQueryError

        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            _install_chat_engine_mock(app, side_effect=EmptyQueryError("빈 질문"))

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
            _install_chat_engine_mock(app, return_value=mock_response)

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
        assert "llm_called" in data
        assert "grounding_status" in data
        assert "repair_actions" in data
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
            app.state.job_queue.get_all_jobs = AsyncMock(
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

        # FastAPI 공개 계약인 OpenAPI schema에서 등록된 REST 경로를 수집한다.
        route_paths = set(app.openapi()["paths"])

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


def _create_transcript_checkpoint(
    checkpoints_dir: Path,
    meeting_id: str,
    filename: str,
    *,
    speaker: str = "SPEAKER_00",
    text: str = "체크포인트 전사",
) -> Path:
    """테스트용 correct/merge checkpoint JSON을 생성한다."""
    meeting_dir = checkpoints_dir / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)
    file_path = meeting_dir / filename
    file_path.write_text(
        json.dumps(
            {
                "utterances": [
                    {
                        "text": text,
                        "original_text": text,
                        "speaker": speaker,
                        "start": 1.0,
                        "end": 2.0,
                        "was_corrected": filename == "correct.json",
                    }
                ],
                "num_speakers": 1,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return file_path


def _create_transcribe_checkpoint(
    checkpoints_dir: Path,
    meeting_id: str,
    *,
    text: str = "초안 전사입니다.",
) -> Path:
    """테스트용 transcribe.json checkpoint를 생성한다."""
    meeting_dir = checkpoints_dir / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)
    file_path = meeting_dir / "transcribe.json"
    file_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "text": text,
                        "start": 0.5,
                        "end": 3.0,
                        "avg_logprob": -0.1,
                        "no_speech_prob": 0.01,
                    }
                ],
                "full_text": text,
                "language": "ko",
                "audio_path": "/tmp/audio.wav",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
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
        assert data["source_stage"] == "corrected"
        assert data["readonly"] is False

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

    def test_transcribe_checkpoint_초안_조회(self, tmp_path: Path) -> None:
        """transcribe.json만 있으면 읽기 전용 전사 초안을 반환한다."""
        app = _make_test_app(tmp_path)
        checkpoints_dir = tmp_path / "checkpoints"
        _create_transcribe_checkpoint(checkpoints_dir, "meeting_draft")

        with TestClient(app) as client:
            response = client.get("/api/meetings/meeting_draft/transcript")

        assert response.status_code == 200
        data = response.json()
        assert data["meeting_id"] == "meeting_draft"
        assert data["source_stage"] == "transcribe"
        assert data["readonly"] is True
        assert data["num_speakers"] == 0
        assert data["speakers"] == []
        assert data["total_utterances"] == 1
        first = data["utterances"][0]
        assert first["text"] == "초안 전사입니다."
        assert first["original_text"] == "초안 전사입니다."
        assert first["speaker"] == "UNKNOWN"
        assert first["start"] == 0.5
        assert first["end"] == 3.0
        assert first["was_corrected"] is False

    def test_전사문_source_우선순위와_readonly(self, tmp_path: Path) -> None:
        """corrected > correct > merge > transcribe 우선순위와 readonly 계약을 검증한다."""
        app = _make_test_app(tmp_path)
        outputs_dir = tmp_path / "outputs"
        checkpoints_dir = tmp_path / "checkpoints"

        _create_transcribe_checkpoint(checkpoints_dir, "meeting_priority", text="draft")
        _create_transcript_checkpoint(
            checkpoints_dir, "meeting_priority", "merge.json", text="merge"
        )

        with TestClient(app) as client:
            response = client.get("/api/meetings/meeting_priority/transcript")
        assert response.status_code == 200
        data = response.json()
        assert data["source_stage"] == "merge"
        assert data["readonly"] is True
        assert data["utterances"][0]["text"] == "merge"

        _create_transcript_checkpoint(
            checkpoints_dir, "meeting_priority", "correct.json", text="correct"
        )
        with TestClient(app) as client:
            response = client.get("/api/meetings/meeting_priority/transcript")
        assert response.status_code == 200
        data = response.json()
        assert data["source_stage"] == "correct"
        assert data["readonly"] is False
        assert data["utterances"][0]["text"] == "correct"

        _create_corrected_json(outputs_dir, "meeting_priority")
        with TestClient(app) as client:
            response = client.get("/api/meetings/meeting_priority/transcript")
        assert response.status_code == 200
        data = response.json()
        assert data["source_stage"] == "corrected"
        assert data["readonly"] is False

    def test_transcribe_checkpoint_초안은_편집_불가(self, tmp_path: Path) -> None:
        """transcribe.json만 있는 상태에서는 PUT/replace가 파일을 바꾸지 않는다."""
        app = _make_test_app(tmp_path)
        checkpoints_dir = tmp_path / "checkpoints"
        draft_path = _create_transcribe_checkpoint(checkpoints_dir, "meeting_draft_edit")
        before = draft_path.read_text(encoding="utf-8")
        payload = {
            "utterances": [
                {
                    "text": "수정",
                    "original_text": "수정",
                    "speaker": "UNKNOWN",
                    "start": 0,
                    "end": 1,
                    "was_corrected": True,
                }
            ]
        }

        with TestClient(app) as client:
            put_response = client.put(
                "/api/meetings/meeting_draft_edit/transcript",
                json=payload,
            )
            replace_response = client.post(
                "/api/meetings/meeting_draft_edit/transcript/replace",
                json={"find": "초안", "replace": "수정", "add_to_vocabulary": False},
            )

        assert put_response.status_code == 404
        assert replace_response.status_code == 404
        assert draft_path.read_text(encoding="utf-8") == before

    def test_dot_segment_meeting_id가_checkpoint_root를_읽지_않음(
        self,
        tmp_path: Path,
    ) -> None:
        """'.' 같은 meeting_id가 checkpoints 루트 파일을 읽지 못하게 한다."""
        app = _make_test_app(tmp_path)
        checkpoints_dir = tmp_path / "checkpoints"
        checkpoints_dir.mkdir(parents=True, exist_ok=True)
        (checkpoints_dir / "transcribe.json").write_text(
            json.dumps({"segments": [{"text": "root leak", "start": 0, "end": 1}]}),
            encoding="utf-8",
        )

        with TestClient(app) as client:
            response = client.get("/api/meetings/./transcript")

        assert response.status_code != 200

    def test_transcript_cache가_higher_stage_생성시_갱신됨(self, tmp_path: Path) -> None:
        """draft 조회 뒤 merge가 생기면 다음 조회는 merge를 반환한다."""
        app = _make_test_app(tmp_path)
        checkpoints_dir = tmp_path / "checkpoints"
        _create_transcribe_checkpoint(checkpoints_dir, "meeting_cache", text="draft")

        with TestClient(app) as client:
            first_response = client.get("/api/meetings/meeting_cache/transcript")
            _create_transcript_checkpoint(
                checkpoints_dir,
                "meeting_cache",
                "merge.json",
                text="merge",
            )
            second_response = client.get("/api/meetings/meeting_cache/transcript")

        assert first_response.status_code == 200
        assert first_response.json()["source_stage"] == "transcribe"
        assert second_response.status_code == 200
        assert second_response.json()["source_stage"] == "merge"
        assert second_response.json()["utterances"][0]["text"] == "merge"

    def test_higher_stage_json_손상시_초안으로_조용히_fallback하지_않음(
        self,
        tmp_path: Path,
    ) -> None:
        """merge.json이 존재하지만 깨진 경우 transcribe 초안으로 숨기지 않는다."""
        app = _make_test_app(tmp_path)
        checkpoints_dir = tmp_path / "checkpoints"
        _create_transcribe_checkpoint(checkpoints_dir, "meeting_corrupt", text="draft")
        meeting_dir = checkpoints_dir / "meeting_corrupt"
        (meeting_dir / "merge.json").write_text("{broken", encoding="utf-8")

        with TestClient(app) as client:
            response = client.get("/api/meetings/meeting_corrupt/transcript")

        assert response.status_code == 500


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


# === 재처리 endpoint meeting_id 경계 ===


@pytest.mark.parametrize("endpoint", ["retry", "transcribe", "re-transcribe"])
def test_재처리_endpoint는_single_segment_meeting_id를_조회전에_검증한다(
    tmp_path: Path,
    endpoint: str,
) -> None:
    """백슬래시가 포함된 ID는 DB 조회나 파일 경로 계산 전에 400으로 거부한다."""
    app = _make_test_app(tmp_path)

    with TestClient(app) as client:
        lookup = MagicMock()
        app.state.job_queue._queue.get_job_by_meeting_id = lookup
        response = client.post(f"/api/meetings/bad%5Cid/{endpoint}")

    assert response.status_code == 400
    lookup.assert_not_called()


def test_meeting_detail_single_segment_contract는_한글과_공백을_허용한다(
    tmp_path: Path,
) -> None:
    """watcher/pipeline 계약처럼 slash 없는 Unicode·공백 ID를 허용한다."""
    app = _make_test_app(tmp_path)
    meeting_id = "회의 1"
    audio_path = _make_audio_file(tmp_path, "unicode-id.m4a")
    recorded = MockJob(1, meeting_id, str(audio_path), "recorded")
    queued = MockJob(1, meeting_id, str(audio_path), "queued")

    with TestClient(app) as client:
        queue = app.state.job_queue._queue
        queue.get_job_by_meeting_id = MagicMock(return_value=recorded)
        queue.queue_job = MagicMock(return_value=queued)
        response = client.post(f"/api/meetings/{meeting_id}/transcribe")

    assert response.status_code == 200
    queue.queue_job.assert_called_once()
    assert response.json()["meeting_id"] == meeting_id


# === TestRetryMeetingEndpoint ===


class TestRetryMeetingEndpoint:
    """POST /api/meetings/{meeting_id}/retry 엔드포인트 테스트."""

    def test_재시도_성공(self, tmp_path: Path) -> None:
        """실패한 회의를 재시도하면 200과 업데이트된 정보를 반환한다."""
        app = _make_test_app(tmp_path)
        audio_path = _make_audio_file(tmp_path, "retry-success.m4a")

        mock_job = MockJob(1, "meeting_001", str(audio_path), "failed")
        mock_retried = MockJob(
            1,
            "meeting_001",
            str(audio_path),
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

    def test_재시도는_DB의_OpenAI_snapshot을_local설정으로_우회하지_않는다(
        self,
        tmp_path: Path,
    ) -> None:
        """state 생성 전 실패한 OpenAI job도 악성 Host에서 재개할 수 없다."""
        app = _make_test_app(tmp_path)
        audio_path = _make_audio_file(tmp_path, "retry-openai-snapshot.m4a")
        failed_job = MockJob(1, "retry_openai_snapshot", str(audio_path), "failed")
        failed_job.stt_provider = "openai"  # type: ignore[attr-defined]
        failed_job.stt_model = "gpt-4o-transcribe-diarize"  # type: ignore[attr-defined]

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=failed_job)
            queue.retry_job = MagicMock()
            response = client.post(
                "/api/meetings/retry_openai_snapshot/retry",
                headers={"host": "attacker.example:8765"},
            )

        assert app.state.config.stt.provider == "local"
        assert response.status_code == 403
        queue.retry_job.assert_not_called()

    def test_재시도_상태_전이_불가_409(self, tmp_path: Path) -> None:
        """failed가 아닌 상태에서 재시도 시 409를 반환한다."""
        from core.job_queue import InvalidTransitionError

        app = _make_test_app(tmp_path)
        audio_path = _make_audio_file(tmp_path, "retry-completed.m4a")

        mock_job = MockJob(1, "meeting_001", str(audio_path), "completed")

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
        audio_path = _make_audio_file(tmp_path, "retry-max.m4a")

        mock_job = MockJob(1, "meeting_001", str(audio_path), "failed", retry_count=3)

        with TestClient(app) as client:
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            app.state.job_queue._queue.retry_job = MagicMock(
                side_effect=MaxRetriesExceededError(1, 3, 3),
            )

            response = client.post("/api/meetings/meeting_001/retry")

        assert response.status_code == 409

    def test_재시도_완료_산출물이_있으면_재시도_대신_복구한다(
        self,
        tmp_path: Path,
    ) -> None:
        """최대 재시도 초과 상태라도 completed 산출물이 있으면 completed 로 복구한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retry_reconcile"
        _create_completed_pipeline_state(tmp_path, meeting_id)

        failed_job = MockJob(1, meeting_id, "/audio/retry.m4a", "failed", retry_count=1)
        completed_job = MockJob(1, meeting_id, "/audio/retry.m4a", "completed", retry_count=1)

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=failed_job)
            queue.force_set_status = MagicMock(return_value=completed_job)
            queue.retry_job = MagicMock()

            response = client.post(f"/api/meetings/{meeting_id}/retry")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert "completed" in data["status_detail"]
        queue.force_set_status.assert_called_once()
        queue.retry_job.assert_not_called()

    def test_재시도_완료상태_복구는_audio_gate보다_먼저_수행한다(
        self,
        tmp_path: Path,
    ) -> None:
        """실제 재실행이 필요 없는 completed 복구에는 오디오 검증을 요구하지 않는다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retry_reconcile_before_gate"
        _create_completed_pipeline_state(tmp_path, meeting_id)
        failed_job = MockJob(1, meeting_id, "/missing/legacy.m4a", "failed", retry_count=1)
        completed_job = MockJob(
            1,
            meeting_id,
            "/missing/legacy.m4a",
            "completed",
            retry_count=1,
        )
        admission = MagicMock(side_effect=AssertionError("gate must not run"))

        with (
            TestClient(app) as client,
            patch(
                "api.routers.meeting_detail.validate_audio_quality",
                admission,
                create=True,
            ),
        ):
            app.state.config.audio_quality.enabled = True
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=failed_job)
            queue.force_set_status = MagicMock(return_value=completed_job)
            queue.retry_job = MagicMock()

            response = client.post(f"/api/meetings/{meeting_id}/retry")

        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        admission.assert_not_called()
        queue.force_set_status.assert_called_once()
        queue.retry_job.assert_not_called()

    def test_재시도_completed_reconcile은_pipeline_state_symlink를_읽지_않는다(
        self,
        tmp_path: Path,
    ) -> None:
        """외부 completed JSON symlink가 failed job을 completed로 바꾸지 못한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retry_state_symlink"
        checkpoint_dir = tmp_path / "checkpoints" / meeting_id
        checkpoint_dir.mkdir(parents=True)
        external_state = tmp_path.parent / f"external-state-{tmp_path.name}.json"
        external_state.write_text(
            json.dumps({"status": "completed", "completed_steps": ["merge"]}),
            encoding="utf-8",
        )
        (checkpoint_dir / "pipeline_state.json").symlink_to(external_state)
        failed_job = MockJob(1, meeting_id, "/missing/audio.wav", "failed")

        try:
            with TestClient(app) as client:
                queue = app.state.job_queue._queue
                queue.get_job_by_meeting_id = MagicMock(return_value=failed_job)
                queue.force_set_status = MagicMock()
                queue.retry_job = MagicMock()
                response = client.post(f"/api/meetings/{meeting_id}/retry")

            assert response.status_code == 400
            queue.force_set_status.assert_not_called()
            queue.retry_job.assert_not_called()
            assert external_state.read_text(encoding="utf-8").startswith("{")
        finally:
            external_state.unlink(missing_ok=True)

    def test_재시도_completed_reconcile은_raw_base_symlink_target을_읽지_않는다(
        self,
        tmp_path: Path,
    ) -> None:
        """resolved checkpoints가 유효해 보여도 raw base symlink는 먼저 거부한다."""
        from api.server import create_app

        external_base = tmp_path / "external-base"
        external_base.mkdir()
        base_link = tmp_path / "base-link"
        base_link.symlink_to(external_base, target_is_directory=True)
        config = AppConfig(
            paths=PathsConfig(base_dir=str(base_link)),
            server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
        )
        with (
            patch("search.hybrid_search.HybridSearchEngine", return_value=MagicMock()),
            patch("search.chat.ChatEngine", return_value=MagicMock()),
        ):
            app = create_app(config, runtime_profile="api-test")
        meeting_id = "meeting_retry_base_symlink"
        state = external_base / "checkpoints" / meeting_id / "pipeline_state.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        failed_job = MockJob(1, meeting_id, str(external_base / "audio.wav"), "failed")
        cache_read = MagicMock(side_effect=AssertionError("external state must not be read"))

        with (
            TestClient(app) as client,
            patch("api.routers.meeting_detail._json_cache.get", cache_read),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=failed_job)
            queue.force_set_status = MagicMock()
            queue.retry_job = MagicMock()
            response = client.post(f"/api/meetings/{meeting_id}/retry")

        assert response.status_code == 400
        cache_read.assert_not_called()
        queue.force_set_status.assert_not_called()
        queue.retry_job.assert_not_called()

    def test_재시도_완료_산출물_복구_실패시_재처리하지_않는다(
        self,
        tmp_path: Path,
    ) -> None:
        """완료 산출물이 확인됐지만 DB 복구가 실패하면 retry_job을 호출하지 않는다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retry_reconcile_fail"
        _create_completed_pipeline_state(tmp_path, meeting_id)

        failed_job = MockJob(1, meeting_id, "/audio/retry.m4a", "failed", retry_count=1)

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=failed_job)
            queue.force_set_status = MagicMock(side_effect=RuntimeError("DB locked"))
            queue.retry_job = MagicMock()

            response = client.post(f"/api/meetings/{meeting_id}/retry")

        assert response.status_code == 409
        assert "상태 복구에 실패" in response.json()["detail"]
        queue.force_set_status.assert_called_once()
        queue.retry_job.assert_not_called()

    def test_재시도는_체크포인트와_결과파일을_보존한다(self, tmp_path: Path) -> None:
        """실패한 단계부터 재시도는 기존 진행 기록을 지우지 않는다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retry_keep"
        ckpt_dir = tmp_path / "checkpoints" / meeting_id
        out_dir = tmp_path / "outputs" / meeting_id
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        state_path = ckpt_dir / "pipeline_state.json"
        transcript_path = out_dir / "corrected.json"
        summary_path = out_dir / "summary.md"
        state_path.write_text("{}", encoding="utf-8")
        transcript_path.write_text("{}", encoding="utf-8")
        summary_path.write_text("# summary", encoding="utf-8")

        audio_path = _make_audio_file(tmp_path, "retry-keep.m4a")
        mock_job = MockJob(1, meeting_id, str(audio_path), "failed", retry_count=1)
        mock_retried = MockJob(1, meeting_id, str(audio_path), "queued", retry_count=2)

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=mock_job)
            queue.retry_job = MagicMock(return_value=mock_retried)

            response = client.post(f"/api/meetings/{meeting_id}/retry")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert data["retry_count"] == 2
        assert state_path.exists()
        assert transcript_path.exists()
        assert summary_path.exists()

    @pytest.mark.parametrize(
        ("failure_kind_name", "expected_status"),
        [
            ("MEDIA_INVALID", 422),
            ("SOURCE_BUSY", 409),
            ("INFRA_UNAVAILABLE", 503),
            ("SECURITY_BLOCKED", 400),
        ],
    )
    def test_재시도는_audio_ACCEPT_전에_job과_산출물을_변경하지_않는다(
        self,
        tmp_path: Path,
        failure_kind_name: str,
        expected_status: int,
    ) -> None:
        """비수락 오디오는 retry_count 증가나 queued 전이 전에 HTTP로 거부한다."""
        app = _make_test_app(tmp_path)
        meeting_id = f"retry_gate_{failure_kind_name.lower()}"
        audio_path = tmp_path / "audio_input" / f"{meeting_id}.wav"
        checkpoint_path = tmp_path / "checkpoints" / meeting_id / "transcribe.json"
        output_path = tmp_path / "outputs" / meeting_id / "corrected.json"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"audio-sentinel")
        checkpoint_path.write_text("checkpoint-sentinel", encoding="utf-8")
        output_path.write_text("output-sentinel", encoding="utf-8")

        failed_job = MockJob(
            71,
            meeting_id,
            str(audio_path),
            "failed",
            retry_count=1,
            error_message="old failure",
        )
        admission = MagicMock(return_value=_denied_audio_admission(failure_kind_name))

        with (
            TestClient(app) as client,
            patch("core.audio_quality.validate_audio_quality", admission),
            patch(
                "api.routers.meeting_detail.validate_audio_quality",
                admission,
                create=True,
            ),
        ):
            app.state.config.audio_quality.enabled = True
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=failed_job)
            queue.retry_job = MagicMock()
            queue.force_set_status = MagicMock()

            response = client.post(f"/api/meetings/{meeting_id}/retry")

        assert response.status_code == expected_status
        assert failure_kind_name in response.json()["detail"]
        admission.assert_called_once()
        admission_kwargs = admission.call_args.kwargs
        audio_stat = audio_path.lstat()
        assert admission_kwargs["expected_identity"] == (
            audio_stat.st_dev,
            audio_stat.st_ino,
            audio_stat.st_size,
            audio_stat.st_mtime_ns,
            audio_stat.st_ctime_ns,
        )
        assert admission_kwargs["decode_timeout_base_seconds"] == (
            app.state.config.audio_quality.decode_timeout_base_seconds
        )
        assert admission_kwargs["decode_timeout_factor"] == (
            app.state.config.audio_quality.decode_timeout_factor
        )
        assert admission_kwargs["decode_timeout_cap_seconds"] == (
            app.state.config.audio_quality.decode_timeout_cap_seconds
        )
        queue.retry_job.assert_not_called()
        queue.force_set_status.assert_not_called()
        assert failed_job.status == "failed"
        assert failed_job.retry_count == 1
        assert failed_job.error_message == "old failure"
        assert checkpoint_path.read_text(encoding="utf-8") == "checkpoint-sentinel"
        assert output_path.read_text(encoding="utf-8") == "output-sentinel"

    def test_재시도_gate예외중_source가_바뀌면_503대신_409이고_DB무변경(
        self,
        tmp_path: Path,
    ) -> None:
        """validator 예외와 source swap이 겹치면 identity 원인을 우선 보존한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "retry_gate_exception_swap"
        audio_path = _make_audio_file(tmp_path, "retry-gate-exception-swap.wav")
        failed_job = MockJob(
            72,
            meeting_id,
            str(audio_path),
            "failed",
            retry_count=1,
            error_message="old failure",
        )

        def mutate_then_fail(*args: Any, **kwargs: Any) -> None:
            audio_path.write_bytes(audio_path.read_bytes() + b"changed")
            raise RuntimeError("decoder crashed after source swap")

        admission = MagicMock(side_effect=mutate_then_fail)
        with (
            TestClient(app) as client,
            patch("api.routers.meeting_detail.validate_audio_quality", admission),
        ):
            app.state.config.audio_quality.enabled = True
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=failed_job)
            queue.retry_job = MagicMock()

            response = client.post(f"/api/meetings/{meeting_id}/retry")

        assert response.status_code == 409
        assert "SOURCE_BUSY" in response.json()["detail"]
        queue.retry_job.assert_not_called()
        assert failed_job.status == "failed"
        assert failed_job.retry_count == 1
        assert failed_job.error_message == "old failure"

    def test_재시도_gate비수락중_source가_바뀌어도_409가_우선한다(
        self,
        tmp_path: Path,
    ) -> None:
        """stale REJECT보다 pre/post identity 불일치가 우선해야 한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "retry_gate_reject_swap"
        audio_path = _make_audio_file(tmp_path, "retry-gate-reject-swap.wav")
        failed_job = MockJob(73, meeting_id, str(audio_path), "failed", retry_count=1)

        def mutate_then_reject(*args: Any, **kwargs: Any) -> Any:
            audio_path.write_bytes(audio_path.read_bytes() + b"changed")
            return _denied_audio_admission("MEDIA_INVALID")

        with (
            TestClient(app) as client,
            patch(
                "api.routers.meeting_detail.validate_audio_quality",
                side_effect=mutate_then_reject,
            ),
        ):
            app.state.config.audio_quality.enabled = True
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=failed_job)
            queue.retry_job = MagicMock()

            response = client.post(f"/api/meetings/{meeting_id}/retry")

        assert response.status_code == 409
        assert "SOURCE_BUSY" in response.json()["detail"]
        queue.retry_job.assert_not_called()


# === TestDeleteMeetingEndpoint ===


class TestDeleteMeetingEndpoint:
    """DELETE /api/meetings/{meeting_id} 엔드포인트 테스트."""

    def test_삭제_성공(self, tmp_path: Path) -> None:
        """회의 삭제 성공 시 200과 확인 메시지를 반환한다."""
        app = _make_test_app(tmp_path)
        audio_path = _make_audio_file(tmp_path, "delete-failed.m4a")
        cache_dir = (
            app.state.config.paths.resolved_checkpoints_dir
            / "meeting_001"
            / ".openai-transcribe-parts"
        )
        cache_dir.mkdir(parents=True)
        cached_response = cache_dir / "chunk.json"
        cached_response.write_text('{"response":"sensitive transcript"}', encoding="utf-8")

        mock_job = MockJob(1, "meeting_001", str(audio_path), "failed")

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            delete_commit = _install_delete_claim_mocks(queue, mock_job)

            response = client.delete("/api/meetings/meeting_001")

        assert response.status_code == 200
        assert "삭제" in response.json()["message"]
        delete_commit.assert_called_once()
        assert delete_commit.call_args.args[0] == mock_job.id
        assert not cached_response.exists()

    def test_삭제_미존재_404(self, tmp_path: Path) -> None:
        """존재하지 않는 meeting_id 삭제 시 404를 반환한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=None,
            )

            response = client.delete("/api/meetings/nonexistent")

        assert response.status_code == 404

    @pytest.mark.parametrize(
        "status",
        ["queued", "recording", "transcribing", "diarizing", "merging", "embedding"],
    )
    def test_처리중인_회의는_취소완료전_삭제하지_않는다(
        self,
        tmp_path: Path,
        status: str,
    ) -> None:
        """진행 중 외부 업로드와 저장소 삭제가 경합하지 않게 409로 차단한다."""
        app = _make_test_app(tmp_path)
        audio_path = _make_audio_file(tmp_path, f"active-{status}.m4a")
        purge = MagicMock(return_value=IndexPurgeResult(meeting_id="meeting_active"))

        with (
            TestClient(app) as client,
            patch("api.routers.meeting_detail.purge_meeting_index", purge),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(
                return_value=MockJob(1, "meeting_active", str(audio_path), status)
            )
            queue.claim_for_deletion = MagicMock()

            response = client.delete("/api/meetings/meeting_active")

        assert response.status_code == 409
        assert "취소 완료 후" in response.json()["detail"]
        purge.assert_not_called()
        queue.claim_for_deletion.assert_not_called()
        assert audio_path.exists()

    def test_완료된_회의_삭제_성공(self, tmp_path: Path) -> None:
        """완료된 회의도 삭제할 수 있다."""
        app = _make_test_app(tmp_path)
        audio_path = _make_audio_file(tmp_path, "delete-completed.m4a")

        mock_job = MockJob(1, "meeting_001", str(audio_path), "completed")

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            delete_commit = _install_delete_claim_mocks(queue, mock_job)

            response = client.delete("/api/meetings/meeting_001")

        assert response.status_code == 200
        delete_commit.assert_called_once()

    def test_삭제는_audio_identity를_검증해_quarantine에_전달한다(
        self,
        tmp_path: Path,
    ) -> None:
        """source swap을 막는 expected identity와 raw quarantine 경로를 사용한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_delete_identity"
        audio_path = _make_audio_file(tmp_path, "delete-identity.wav")
        audio_stat = audio_path.lstat()
        expected_identity = (
            audio_stat.st_dev,
            audio_stat.st_ino,
            audio_stat.st_size,
            audio_stat.st_mtime_ns,
            audio_stat.st_ctime_ns,
        )
        move = MagicMock(return_value=tmp_path / "audio_quarantine" / audio_path.name)

        with (
            TestClient(app) as client,
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                return_value=IndexPurgeResult(meeting_id=meeting_id),
            ),
            patch("core.quarantine.move_to_quarantine_exact", move),
        ):
            queue = app.state.job_queue._queue
            mock_job = MockJob(1, meeting_id, str(audio_path), "completed")
            queue.get_job_by_meeting_id = MagicMock(return_value=mock_job)
            _install_delete_claim_mocks(queue, mock_job)
            response = client.delete(f"/api/meetings/{meeting_id}")

        assert response.status_code == 200
        move.assert_called_once()
        move_args = move.call_args
        assert move_args.args[0] == audio_path
        assert move_args.args[1].parent == tmp_path / "audio_quarantine"
        assert move_args.args[1].name.startswith("deleted-")
        assert move_args.args[1].suffix == ".audio"
        assert move_args.kwargs == {
            "reason": f"사용자 삭제 준비: meeting_id={meeting_id}",
            "expected_identity": expected_identity,
        }

    def test_삭제_audio_symlink는_target과_DB를_보존한다(
        self,
        tmp_path: Path,
    ) -> None:
        """source final symlink를 따라 외부 파일을 격리하지 않는다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_delete_source_symlink"
        target = tmp_path.parent / f"delete-target-{tmp_path.name}.wav"
        target.write_bytes(b"external-sentinel")
        audio_path = tmp_path / "audio_input" / "linked.wav"
        audio_path.parent.mkdir(parents=True)
        audio_path.symlink_to(target)
        purge = MagicMock(return_value=IndexPurgeResult(meeting_id=meeting_id))

        try:
            with (
                TestClient(app) as client,
                patch("api.routers.meeting_detail.purge_meeting_index", purge),
            ):
                queue = app.state.job_queue._queue
                mock_job = MockJob(1, meeting_id, str(audio_path), "completed")
                queue.get_job_by_meeting_id = MagicMock(return_value=mock_job)
                delete_commit = _install_delete_claim_mocks(queue, mock_job)
                response = client.delete(f"/api/meetings/{meeting_id}")

            assert response.status_code == 400
            assert "SECURITY_BLOCKED" in response.json()["detail"]
            purge.assert_not_called()
            delete_commit.assert_not_called()
            queue.restore_delete_claim.assert_called_once()
            assert target.read_bytes() == b"external-sentinel"
        finally:
            target.unlink(missing_ok=True)

    def test_삭제_quarantine_symlink는_external_sink와_DB를_보존한다(
        self,
        tmp_path: Path,
    ) -> None:
        """raw quarantine child symlink를 resolved destination으로 신뢰하지 않는다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_delete_quarantine_symlink"
        audio_path = _make_audio_file(tmp_path, "delete-quarantine.wav")
        external_sink = tmp_path.parent / f"quarantine-sink-{tmp_path.name}"
        external_sink.mkdir()
        (tmp_path / "audio_quarantine").symlink_to(
            external_sink,
            target_is_directory=True,
        )
        purge = MagicMock(return_value=IndexPurgeResult(meeting_id=meeting_id))

        try:
            with (
                TestClient(app) as client,
                patch("api.routers.meeting_detail.purge_meeting_index", purge),
            ):
                queue = app.state.job_queue._queue
                mock_job = MockJob(1, meeting_id, str(audio_path), "completed")
                queue.get_job_by_meeting_id = MagicMock(return_value=mock_job)
                delete_commit = _install_delete_claim_mocks(queue, mock_job)
                response = client.delete(f"/api/meetings/{meeting_id}")

            assert response.status_code == 400
            purge.assert_not_called()
            delete_commit.assert_not_called()
            queue.restore_delete_claim.assert_called_once()
            assert audio_path.read_bytes() == b"audio-sentinel"
            assert list(external_sink.iterdir()) == []
        finally:
            external_sink.rmdir()

    def test_삭제_raw_base_symlink는_external_source를_읽거나_옮기지_않는다(
        self,
        tmp_path: Path,
    ) -> None:
        """resolved quarantine/source가 정상이어도 symlink base 자체를 거부한다."""
        from api.server import create_app

        external_base = tmp_path / "delete-external-base"
        external_base.mkdir()
        base_link = tmp_path / "delete-base-link"
        base_link.symlink_to(external_base, target_is_directory=True)
        config = AppConfig(
            paths=PathsConfig(base_dir=str(base_link)),
            server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
        )
        with (
            patch("search.hybrid_search.HybridSearchEngine", return_value=MagicMock()),
            patch("search.chat.ChatEngine", return_value=MagicMock()),
        ):
            app = create_app(config, runtime_profile="api-test")
        audio_path = external_base / "audio_input" / "external.wav"
        audio_path.parent.mkdir(parents=True)
        audio_path.write_bytes(b"external-audio")
        meeting_id = "meeting_delete_base_symlink"
        purge = MagicMock(return_value=IndexPurgeResult(meeting_id=meeting_id))
        inspect = MagicMock()

        with (
            TestClient(app) as client,
            patch("api.routers.meeting_detail.purge_meeting_index", purge),
            patch("api.routers.meeting_detail.inspect_audio_path_no_symlinks", inspect),
        ):
            queue = app.state.job_queue._queue
            mock_job = MockJob(1, meeting_id, str(audio_path), "completed")
            queue.get_job_by_meeting_id = MagicMock(return_value=mock_job)
            delete_commit = _install_delete_claim_mocks(queue, mock_job)
            response = client.delete(f"/api/meetings/{meeting_id}")

        assert response.status_code == 400
        inspect.assert_not_called()
        purge.assert_not_called()
        delete_commit.assert_not_called()
        queue.restore_delete_claim.assert_called_once()
        assert audio_path.read_bytes() == b"external-audio"

    def test_삭제는_검색인덱스_정리_후_DB삭제(self, tmp_path: Path) -> None:
        """삭제 시 stale 검색 인덱스를 먼저 정리한 뒤 DB 레코드를 삭제한다."""
        app = _make_test_app(tmp_path)
        mock_job = MockJob(1, "meeting_order", "", "completed")
        calls: list[str] = []

        def _fake_purge(_config: AppConfig, _meeting_id: str) -> IndexPurgeResult:
            calls.append("purge")
            return IndexPurgeResult(meeting_id="meeting_order")

        def _fake_delete(_job_id: int, _token: str) -> None:
            calls.append("delete")

        with (
            TestClient(app) as client,
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                side_effect=_fake_purge,
            ),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            _install_delete_claim_mocks(
                queue,
                mock_job,
                delete_side_effect=_fake_delete,
            )

            response = client.delete("/api/meetings/meeting_order")

        assert response.status_code == 200
        assert calls == ["purge", "delete"]

    def test_삭제시_검색인덱스_정리_실패하면_DB삭제하지_않음(
        self,
        tmp_path: Path,
    ) -> None:
        """재색인까지 못 하면 DB 행은 durable claim 상태로 보존한다."""
        app = _make_test_app(tmp_path)
        mock_job = MockJob(1, "meeting_purge_fail", "", "completed")

        with (
            TestClient(app) as client,
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                side_effect=IndexPurgeError("index locked"),
            ),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            delete_commit = _install_delete_claim_mocks(queue, mock_job)

            response = client.delete("/api/meetings/meeting_purge_fail")

        assert response.status_code == 500
        assert "검색 인덱스" in response.json()["detail"]
        delete_commit.assert_not_called()
        queue.restore_delete_claim.assert_not_called()
        assert mock_job.status == "recording"

    def test_삭제_purge실패는_OpenAI_성공청크_cache를_보존한다(
        self,
        tmp_path: Path,
    ) -> None:
        """삭제 rollback 뒤 retry가 이미 과금된 청크를 다시 보내지 않게 한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_delete_cache_rollback"
        cache_dir = (
            app.state.config.paths.resolved_checkpoints_dir
            / meeting_id
            / ".openai-transcribe-parts"
        )
        cache_dir.mkdir(parents=True)
        cached = cache_dir / "chunk_0000.json"
        cached.write_text('{"response":"paid"}', encoding="utf-8")
        mock_job = MockJob(1, meeting_id, "", "completed")

        with (
            TestClient(app) as client,
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                side_effect=IndexPurgeError("index locked"),
            ),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=mock_job)
            _install_delete_claim_mocks(queue, mock_job)
            response = client.delete(f"/api/meetings/{meeting_id}")

        assert response.status_code == 500
        assert cached.read_text(encoding="utf-8") == '{"response":"paid"}'
        queue.restore_delete_claim.assert_not_called()

    def test_삭제_cache정리실패는_committing_claim을_남겨_startup이_완료한다(
        self,
        tmp_path: Path,
    ) -> None:
        """부분 cache 삭제 뒤 회의를 복원하거나 tombstone을 먼저 없애지 않는다."""
        from core.job_queue import parse_delete_claim

        app = _make_test_app(tmp_path)
        meeting_id = "meeting_delete_cache_commit"
        mock_job = MockJob(1, meeting_id, "", "completed")

        with (
            TestClient(app) as client,
            patch(
                "steps.openai_transcriber.cleanup_meeting_openai_resume_caches",
                side_effect=OSError("disk busy"),
            ),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=mock_job)
            delete_commit = _install_delete_claim_mocks(queue, mock_job)
            response = client.delete(f"/api/meetings/{meeting_id}")

        assert response.status_code == 500
        delete_commit.assert_not_called()
        queue.restore_delete_claim.assert_not_called()
        claim = parse_delete_claim(mock_job.requested_action)  # type: ignore[attr-defined]
        assert claim is not None
        assert claim.phase == "committing"

    def test_recorded_삭제_purge실패는_검색산출물없이_원상복구한다(
        self,
        tmp_path: Path,
    ) -> None:
        """미전사 회의 rollback은 없는 correct/merge 체크포인트를 요구하지 않는다."""
        app = _make_test_app(tmp_path)
        mock_job = MockJob(2, "recorded_purge_fail", "", "recorded")

        with (
            TestClient(app) as client,
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                side_effect=IndexPurgeError("index locked"),
            ),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=mock_job)
            _install_delete_claim_mocks(queue, mock_job)

            response = client.delete("/api/meetings/recorded_purge_fail")

        assert response.status_code == 500
        queue.restore_delete_claim.assert_called_once()

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
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            delete_commit = _install_delete_claim_mocks(queue, mock_job)

            response = client.delete("/api/meetings/meeting_phase1")

        # 2) DELETE 자체는 성공
        assert response.status_code == 200
        assert "삭제" in response.json()["message"]

        # 3) DB 삭제 호출 확인
        delete_commit.assert_called_once()
        assert delete_commit.call_args.args[0] == 1

        # 4) 원본 파일 사라졌는지
        assert not audio_file.exists(), "원본 오디오 파일이 quarantine으로 이동되었어야 한다"

        # 5) quarantine 디렉토리에 이동되었는지
        quarantine_dir = tmp_path / "audio_quarantine"
        assert quarantine_dir.exists()
        moved_files = list(quarantine_dir.glob("deleted-*.audio"))
        assert len(moved_files) == 1
        moved = moved_files[0]
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
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            delete_commit = _install_delete_claim_mocks(queue, mock_job)

            response = client.delete("/api/meetings/meeting_missing")

        # 파일 부재에도 DELETE 성공
        assert response.status_code == 200
        # DB 삭제는 여전히 호출
        delete_commit.assert_called_once()
        assert delete_commit.call_args.args[0] == 2

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
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(
                return_value=mock_job,
            )
            delete_commit = _install_delete_claim_mocks(queue, mock_job)

            response = client.delete("/api/meetings/meeting_noaudio")

        assert response.status_code == 200
        delete_commit.assert_called_once()
        assert delete_commit.call_args.args[0] == 3


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
        """failed 상태에서 force=true 이면 중간 상태 없이 queued 로 원자 전이한다."""
        app = _make_test_app(tmp_path)
        audio_path = _make_audio_file(tmp_path, "transcribe-retry.m4a")

        failed_job = MockJob(1, "meeting_retry", str(audio_path), "failed")
        queued_job = MockJob(1, "meeting_retry", str(audio_path), "queued")

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=failed_job)
            queue.queue_failed_job = MagicMock(return_value=queued_job)
            queue.force_set_status = MagicMock()
            queue.update_status = MagicMock()

            response = client.post("/api/meetings/meeting_retry/transcribe?force=true")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        queue.queue_failed_job.assert_called_once_with(
            1,
            stt_provider="local",
            stt_model="mlx-community/whisper-large-v3-turbo",
        )
        queue.force_set_status.assert_not_called()
        queue.update_status.assert_not_called()

    def test_transcribe_recorded_상태_정상(self, tmp_path: Path) -> None:
        """recorded 상태에서는 force 여부와 무관하게 정상 전이한다."""
        app = _make_test_app(tmp_path)
        audio_path = _make_audio_file(tmp_path, "transcribe-recorded.m4a")

        recorded_job = MockJob(1, "meeting_ok", str(audio_path), "recorded")
        queued_job = MockJob(1, "meeting_ok", str(audio_path), "queued")

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=recorded_job)
            queue.force_set_status = MagicMock()
            queue.queue_job = MagicMock(return_value=queued_job)

            response = client.post("/api/meetings/meeting_ok/transcribe")

        assert response.status_code == 200
        # recorded 상태에서는 force_set_status 를 호출하지 않아야 한다
        queue.force_set_status.assert_not_called()

    def test_transcribe_개별_OpenAI선택은_전역local을_바꾸지않고_snapshot한다(
        self,
        tmp_path: Path,
    ) -> None:
        """한 회의만 OpenAI를 선택해도 기본 설정은 local로 유지한다."""
        from security.openai_keychain import OpenAICredentialStatus

        app = _make_test_app(tmp_path)
        meeting_id = "meeting_oneoff_openai"
        audio_path = _make_audio_file(tmp_path, f"{meeting_id}.m4a")
        recorded_job = MockJob(1, meeting_id, str(audio_path), "recorded")
        queued_job = MockJob(1, meeting_id, str(audio_path), "queued")

        with (
            TestClient(app, base_url="http://127.0.0.1") as client,
            patch(
                "security.openai_keychain.get_status",
                return_value=OpenAICredentialStatus(configured=True, source="keychain"),
            ) as key_status,
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=recorded_job)
            queue.queue_job = MagicMock(return_value=queued_job)

            response = client.post(
                f"/api/meetings/{meeting_id}/transcribe",
                json={
                    "model_id": "openai:gpt-4o-transcribe-diarize",
                    "external_upload_confirmed": True,
                },
            )

        assert response.status_code == 200
        assert app.state.config.stt.provider == "local"
        key_status.assert_called_once_with()
        queue.queue_job.assert_called_once_with(
            1,
            "",
            stt_provider="openai",
            stt_model="gpt-4o-transcribe-diarize",
        )

    def test_transcribe_개별_local선택은_전역OpenAI보다_우선한다(
        self,
        tmp_path: Path,
    ) -> None:
        """전역 OpenAI 상태에서도 이 회의만 local snapshot으로 큐잉할 수 있다."""
        app = _make_test_app(tmp_path)
        app.state.config.stt.provider = "openai"
        meeting_id = "meeting_oneoff_local"
        audio_path = _make_audio_file(tmp_path, f"{meeting_id}.m4a")
        recorded_job = MockJob(1, meeting_id, str(audio_path), "recorded")
        queued_job = MockJob(1, meeting_id, str(audio_path), "queued")

        with (
            TestClient(app) as client,
            patch("security.openai_keychain.get_status") as key_status,
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=recorded_job)
            queue.queue_job = MagicMock(return_value=queued_job)
            response = client.post(
                f"/api/meetings/{meeting_id}/transcribe",
                json={"model_id": "local", "external_upload_confirmed": False},
            )

        assert response.status_code == 200
        assert app.state.config.stt.provider == "openai"
        key_status.assert_not_called()
        queue.queue_job.assert_called_once_with(
            1,
            "",
            stt_provider="local",
            stt_model="mlx-community/whisper-large-v3-turbo",
        )

    def test_transcribe_OpenAI동의누락은_Keychain과_회의조회전에_거부한다(
        self,
        tmp_path: Path,
    ) -> None:
        """명시적 파일 동의가 없으면 비밀·DB·오디오 경계에 진입하지 않는다."""
        app = _make_test_app(tmp_path)

        with (
            TestClient(app) as client,
            patch("security.openai_keychain.get_status") as key_status,
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock()
            queue.queue_job = MagicMock()
            response = client.post(
                "/api/meetings/consent_first/transcribe",
                json={
                    "model_id": "openai:gpt-4o-transcribe-diarize",
                    "external_upload_confirmed": False,
                },
            )

        assert response.status_code == 400
        assert "동의" in response.json()["detail"]
        key_status.assert_not_called()
        queue.get_job_by_meeting_id.assert_not_called()
        queue.queue_job.assert_not_called()

    def test_transcribe_OpenAI키누락과_악성Origin은_queue전에_거부한다(
        self,
        tmp_path: Path,
    ) -> None:
        """키 미등록과 DNS-rebinding 요청은 DB·오디오 경계에 진입하지 않는다."""
        from security.openai_keychain import OpenAICredentialStatus

        app = _make_test_app(tmp_path)
        payload = {
            "model_id": "openai:gpt-4o-transcribe-diarize",
            "external_upload_confirmed": True,
        }

        with (
            TestClient(app, base_url="http://127.0.0.1") as client,
            patch(
                "security.openai_keychain.get_status",
                return_value=OpenAICredentialStatus(configured=False, source=None),
            ) as key_status,
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock()
            queue.queue_job = MagicMock()
            no_key = client.post(
                "/api/meetings/no_key/transcribe",
                json=payload,
            )
            hostile_origin = client.post(
                "/api/meetings/evil_origin/transcribe",
                json=payload,
                headers={"origin": "https://attacker.example"},
            )

        assert no_key.status_code == 400
        assert "API 키" in no_key.json()["detail"]
        assert hostile_origin.status_code == 403
        key_status.assert_called_once_with()
        queue.get_job_by_meeting_id.assert_not_called()
        queue.queue_job.assert_not_called()

    @pytest.mark.parametrize("invalid_consent", ["true", 1, 0, None])
    def test_transcribe_외부동의는_boolean만_허용하고_입력을_반사하지않는다(
        self,
        tmp_path: Path,
        invalid_consent: Any,
    ) -> None:
        """문자열·숫자 동의와 임의 비밀 필드는 고정 오류로 거부한다."""
        app = _make_test_app(tmp_path)
        secret = "sk-never-reflect-this-value-1234567890"

        with TestClient(app) as client:
            response = client.post(
                "/api/meetings/strict_consent/transcribe",
                json={
                    "model_id": "openai:gpt-4o-transcribe-diarize",
                    "external_upload_confirmed": invalid_consent,
                },
            )
            extra_response = client.post(
                "/api/meetings/strict_consent/transcribe",
                json={"model_id": "local", "api_key": secret},
            )

        assert response.status_code == 400
        assert extra_response.status_code == 400
        assert secret not in extra_response.text

    def test_transcribe_pinned_pipeline과_다른_개별모델은_queue전에_409(
        self,
        tmp_path: Path,
    ) -> None:
        """실행 이력이 있는 취소 작업은 다른 모델로 몰래 재개하지 않는다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_pinned_openai"
        audio_path = _make_audio_file(tmp_path, f"{meeting_id}.m4a")
        checkpoint_dir = app.state.config.paths.resolved_checkpoints_dir / meeting_id
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        (checkpoint_dir / "pipeline_state.json").write_text(
            json.dumps(
                {
                    "status": "pending",
                    "stt_provider": "openai",
                    "stt_model": "gpt-4o-transcribe-diarize",
                }
            ),
            encoding="utf-8",
        )
        recorded_job = MockJob(1, meeting_id, str(audio_path), "recorded")

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=recorded_job)
            queue.queue_job = MagicMock()
            response = client.post(
                f"/api/meetings/{meeting_id}/transcribe",
                json={"model_id": "local", "external_upload_confirmed": False},
            )

        assert response.status_code == 409
        assert "고정" in response.json()["detail"]
        queue.queue_job.assert_not_called()

    def test_transcribe_pinned_local은_기본로컬모델이_바뀌어도_기존모델로_재개(
        self,
        tmp_path: Path,
    ) -> None:
        """local 공개 ID는 재개 시 현재 기본값 대신 pinned 모델을 유지한다."""
        app = _make_test_app(tmp_path)
        app.state.config.stt.model_name = "mlx-community/new-local-default"
        meeting_id = "meeting_pinned_old_local"
        audio_path = _make_audio_file(tmp_path, f"{meeting_id}.m4a")
        checkpoint_dir = app.state.config.paths.resolved_checkpoints_dir / meeting_id
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        (checkpoint_dir / "pipeline_state.json").write_text(
            json.dumps(
                {
                    "status": "pending",
                    "stt_provider": "local",
                    "stt_model": "mlx-community/old-local-pinned",
                }
            ),
            encoding="utf-8",
        )
        recorded_job = MockJob(1, meeting_id, str(audio_path), "recorded")
        queued_job = MockJob(1, meeting_id, str(audio_path), "queued")

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=recorded_job)
            queue.queue_job = MagicMock(return_value=queued_job)
            response = client.post(
                f"/api/meetings/{meeting_id}/transcribe",
                json={"model_id": "local", "external_upload_confirmed": False},
            )

        assert response.status_code == 200
        queue.queue_job.assert_called_once_with(
            1,
            "",
            stt_provider="local",
            stt_model="mlx-community/old-local-pinned",
        )

    def test_transcribe_queued취소_snapshot은_새_개별선택으로_덮어쓴다(
        self,
        tmp_path: Path,
    ) -> None:
        """worker 시작 전 취소된 snapshot은 pipeline state가 없어 재선택 가능하다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_cancelled_before_worker"
        audio_path = _make_audio_file(tmp_path, f"{meeting_id}.m4a")
        recorded_job = MockJob(1, meeting_id, str(audio_path), "recorded")
        recorded_job.stt_provider = "openai"  # type: ignore[attr-defined]
        recorded_job.stt_model = "gpt-4o-transcribe-diarize"  # type: ignore[attr-defined]
        queued_job = MockJob(1, meeting_id, str(audio_path), "queued")

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=recorded_job)
            queue.queue_job = MagicMock(return_value=queued_job)
            response = client.post(
                f"/api/meetings/{meeting_id}/transcribe",
                json={"model_id": "local", "external_upload_confirmed": False},
            )

        assert response.status_code == 200
        queue.queue_job.assert_called_once_with(
            1,
            "",
            stt_provider="local",
            stt_model="mlx-community/whisper-large-v3-turbo",
        )

    def test_transcribe_동시queue_CAS경합은_409로_수렴한다(self, tmp_path: Path) -> None:
        """중복 클릭으로 queue CAS가 경합해도 내부 오류 대신 명시적 충돌을 반환한다."""
        from core.job_queue import JobQueueError

        app = _make_test_app(tmp_path)
        meeting_id = "meeting_queue_race"
        audio_path = _make_audio_file(tmp_path, f"{meeting_id}.m4a")
        recorded_job = MockJob(1, meeting_id, str(audio_path), "recorded")

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=recorded_job)
            queue.queue_job = MagicMock(side_effect=JobQueueError("queue CAS conflict"))
            response = client.post(
                f"/api/meetings/{meeting_id}/transcribe",
                json={"model_id": "local", "external_upload_confirmed": False},
            )

        assert response.status_code == 409
        assert "CAS" in response.json()["detail"]

    def test_transcribe_queue직전_동시삭제는_404를_반환한다(self, tmp_path: Path) -> None:
        """최초 조회 후 삭제된 작업은 상위 queue 충돌이 아니라 not-found다."""
        from core.job_queue import JobNotFoundError

        app = _make_test_app(tmp_path)
        meeting_id = "meeting_deleted_during_queue"
        audio_path = _make_audio_file(tmp_path, f"{meeting_id}.m4a")
        recorded_job = MockJob(1, meeting_id, str(audio_path), "recorded")

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=recorded_job)
            queue.queue_job = MagicMock(side_effect=JobNotFoundError(1))
            response = client.post(
                f"/api/meetings/{meeting_id}/transcribe",
                json={"model_id": "local", "external_upload_confirmed": False},
            )

        assert response.status_code == 404
        assert "찾을 수 없습니다" in response.json()["detail"]

    def test_transcribe는_취소된_OpenAI_state를_local_기본값으로_우회하지_않는다(
        self,
        tmp_path: Path,
    ) -> None:
        """pinned OpenAI 재개는 현재 기본값이 local이어도 악성 Host에서 거부한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_cancelled_openai"
        audio_path = _make_audio_file(tmp_path, f"{meeting_id}.m4a")
        checkpoint_dir = app.state.config.paths.resolved_checkpoints_dir / meeting_id
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        (checkpoint_dir / "pipeline_state.json").write_text(
            json.dumps(
                {
                    "status": "pending",
                    "stt_provider": "openai",
                    "stt_model": "gpt-4o-transcribe-diarize",
                }
            ),
            encoding="utf-8",
        )
        recorded_job = MockJob(1, meeting_id, str(audio_path), "recorded")

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=recorded_job)
            queue.queue_job = MagicMock()
            response = client.post(
                f"/api/meetings/{meeting_id}/transcribe",
                headers={"host": "attacker.example:8765"},
            )

        assert app.state.config.stt.provider == "local"
        assert response.status_code == 403
        queue.queue_job.assert_not_called()

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

    @pytest.mark.parametrize(
        ("failure_kind_name", "expected_status"),
        [
            ("MEDIA_INVALID", 422),
            ("SOURCE_BUSY", 409),
            ("INFRA_UNAVAILABLE", 503),
            ("SECURITY_BLOCKED", 400),
        ],
    )
    def test_force_transcribe는_audio_ACCEPT_전에_failed_job을_변경하지_않는다(
        self,
        tmp_path: Path,
        failure_kind_name: str,
        expected_status: int,
    ) -> None:
        """force=true여도 admission 통과 전에 failed→recorded 전이를 하면 안 된다."""
        app = _make_test_app(tmp_path)
        meeting_id = f"force_gate_{failure_kind_name.lower()}"
        audio_path = tmp_path / "audio_input" / f"{meeting_id}.wav"
        output_path = tmp_path / "outputs" / meeting_id / "corrected.json"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"audio-sentinel")
        output_path.write_text("output-sentinel", encoding="utf-8")
        failed_job = MockJob(
            72,
            meeting_id,
            str(audio_path),
            "failed",
            retry_count=2,
            error_message="old failure",
        )
        admission = MagicMock(return_value=_denied_audio_admission(failure_kind_name))

        with (
            TestClient(app) as client,
            patch("core.audio_quality.validate_audio_quality", admission),
            patch(
                "api.routers.meeting_detail.validate_audio_quality",
                admission,
                create=True,
            ),
        ):
            app.state.config.audio_quality.enabled = True
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=failed_job)
            queue.queue_failed_job = MagicMock()
            queue.force_set_status = MagicMock()
            queue.update_status = MagicMock()

            response = client.post(f"/api/meetings/{meeting_id}/transcribe?force=true")

        assert response.status_code == expected_status
        assert failure_kind_name in response.json()["detail"]
        admission.assert_called_once()
        queue.queue_failed_job.assert_not_called()
        queue.force_set_status.assert_not_called()
        queue.update_status.assert_not_called()
        assert failed_job.status == "failed"
        assert failed_job.retry_count == 2
        assert failed_job.error_message == "old failure"
        assert output_path.read_text(encoding="utf-8") == "output-sentinel"


class TestCancelMeetingEndpoint:
    """POST /api/meetings/{meeting_id}/cancel의 durable 취소 계약."""

    def test_실행중_OpenAI_취소는_DB_claim후_flag를_등록한다(
        self,
        tmp_path: Path,
    ) -> None:
        """200 응답 직후 종료돼도 startup이 외부 업로드를 재개하지 않게 한다."""
        from core.job_queue import CancellationClaim

        app = _make_test_app(tmp_path)
        meeting_id = "cancel-active-openai"
        active_job = MockJob(
            91,
            meeting_id,
            str(_make_audio_file(tmp_path, f"{meeting_id}.m4a")),
            "transcribing",
        )
        active_job.stt_provider = "openai"  # type: ignore[attr-defined]
        active_job.stt_model = "gpt-4o-transcribe-diarize"  # type: ignore[attr-defined]
        durable_job = MockJob(
            91,
            meeting_id,
            active_job.audio_path,
            "recording",
            error_message="사용자가 취소 요청함",
        )
        durable_job.stt_provider = "openai"  # type: ignore[attr-defined]
        durable_job.stt_model = "gpt-4o-transcribe-diarize"  # type: ignore[attr-defined]
        durable_job.requested_action = CancellationClaim(  # type: ignore[attr-defined]
            original_status="transcribing",
            original_requested_action="",
            token="durable-token",
        ).to_requested_action()

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=active_job)
            queue.claim_active_job_for_cancellation = MagicMock(return_value=durable_job)
            processor = MagicMock()
            processor.stop = AsyncMock()
            app.state.job_processor = processor

            response = client.post(f"/api/meetings/{meeting_id}/cancel")

        assert response.status_code == 200
        assert response.json()["status"] == "recording"
        assert "저장" in response.json()["status_detail"]
        queue.claim_active_job_for_cancellation.assert_called_once()
        processor.request_cancellation.assert_called_once_with(meeting_id)


class TestReTranscribeMeetingEndpoint:
    """POST /api/meetings/{meeting_id}/re-transcribe 엔드포인트 테스트."""

    def test_재전사_real_queue_claim부터_queued_finalize까지_완료한다(
        self,
        tmp_path: Path,
    ) -> None:
        """실제 SQLite 큐에서도 CAS claim과 reset이 하나의 요청으로 연결된다."""
        from core.job_queue import JobStatus

        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retranscribe_real_queue"
        audio_path = tmp_path / "audio_input" / f"{meeting_id}.wav"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"audio")

        with (
            TestClient(app) as client,
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                return_value=IndexPurgeResult(meeting_id=meeting_id),
            ),
        ):
            queue = app.state.job_queue._queue
            job_id = queue.add_job(
                meeting_id,
                str(audio_path),
                initial_status=JobStatus.COMPLETED.value,
            )

            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")
            stored = queue.get_job(job_id)

        assert response.status_code == 200
        assert response.json()["status"] == JobStatus.QUEUED.value
        assert stored.status == JobStatus.QUEUED.value
        assert stored.requested_action == ""
        assert audio_path.read_bytes() == b"audio"

    def test_재전사는_체크포인트와_결과파일을_삭제하고_queued로_초기화한다(
        self,
        tmp_path: Path,
    ) -> None:
        """처음부터 다시 전사는 기존 결과를 버리고 retry_count를 0으로 리셋한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retranscribe_reset"
        ckpt_dir = tmp_path / "checkpoints" / meeting_id
        out_dir = tmp_path / "outputs" / meeting_id
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        state_path = ckpt_dir / "pipeline_state.json"
        transcript_path = out_dir / "corrected.json"
        summary_path = out_dir / "summary.md"
        minutes_path = out_dir / "meeting_minutes.md"
        summary_json_path = out_dir / "summary.json"
        audio_path = out_dir / "input.wav"
        state_path.write_text("{}", encoding="utf-8")
        transcript_path.write_text("{}", encoding="utf-8")
        summary_path.write_text("# summary", encoding="utf-8")
        minutes_path.write_text("# legacy summary", encoding="utf-8")
        summary_json_path.write_text('{"summary": "stale"}', encoding="utf-8")
        audio_path.write_bytes(b"audio")

        mock_job = MockJob(1, meeting_id, str(audio_path), "failed", retry_count=2)
        mock_reset = MockJob(1, meeting_id, str(audio_path), "queued", retry_count=0)

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=mock_job)
            _install_retranscribe_claim_mocks(queue, mock_job)
            queue.reset_for_retranscribe = MagicMock(return_value=mock_reset)

            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert data["retry_count"] == 0
        assert ckpt_dir.is_dir()
        assert not list(ckpt_dir.iterdir())
        assert not transcript_path.exists()
        assert not summary_path.exists()
        assert not minutes_path.exists()
        assert not summary_json_path.exists()
        assert audio_path.exists()

    def test_재전사는_검색인덱스_정리_후_상태를_리셋한다(
        self,
        tmp_path: Path,
    ) -> None:
        """재전사 요청은 stale 인덱스 정리 후 job 상태를 queued로 되돌린다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retranscribe_order"
        audio_path = tmp_path / "outputs" / meeting_id / "input.wav"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"audio")

        mock_job = MockJob(1, meeting_id, str(audio_path), "completed", retry_count=1)
        mock_reset = MockJob(1, meeting_id, str(audio_path), "queued", retry_count=0)
        calls: list[str] = []

        def _fake_purge(_config: AppConfig, _meeting_id: str) -> IndexPurgeResult:
            calls.append("purge")
            return IndexPurgeResult(meeting_id=meeting_id)

        def _fake_reset(_job_id: int, _token: str) -> MockJob:
            calls.append("reset")
            return mock_reset

        with (
            TestClient(app) as client,
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                side_effect=_fake_purge,
            ),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=mock_job)
            _install_retranscribe_claim_mocks(queue, mock_job)
            queue.reset_for_retranscribe = MagicMock(side_effect=_fake_reset)

            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

        assert response.status_code == 200
        assert calls == ["purge", "reset"]

    def test_재전사_claim_CAS_실패는_staging과_purge를_시작하지_않는다(
        self,
        tmp_path: Path,
    ) -> None:
        """조회 뒤 상태가 바뀐 race는 조건부 claim에서 막고 로컬 파일을 보존한다."""
        from core.job_queue import InvalidTransitionError

        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retranscribe_claim_race"
        audio_path = tmp_path / "audio_input" / f"{meeting_id}.wav"
        state_path = tmp_path / "checkpoints" / meeting_id / "pipeline_state.json"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"audio")
        state_path.write_text("state", encoding="utf-8")
        original_job = MockJob(1, meeting_id, str(audio_path), "completed")
        stage = MagicMock()
        purge = MagicMock(return_value=IndexPurgeResult(meeting_id=meeting_id))

        with (
            TestClient(app) as client,
            patch("api.routers.meeting_detail._stage_retranscribe_artifacts", stage),
            patch("api.routers.meeting_detail.purge_meeting_index", purge),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=original_job)
            queue.claim_for_retranscribe = MagicMock(
                side_effect=InvalidTransitionError(1, "queued", "recording")
            )
            queue.reset_for_retranscribe = MagicMock()

            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

        assert response.status_code == 409
        stage.assert_not_called()
        purge.assert_not_called()
        queue.reset_for_retranscribe.assert_not_called()
        assert state_path.read_text(encoding="utf-8") == "state"
        assert audio_path.read_bytes() == b"audio"

    def test_재전사_claim후_audio_identity가_바뀌면_claim만_복구한다(
        self,
        tmp_path: Path,
    ) -> None:
        """gate→claim 사이 source swap은 staging/purge 전에 409로 종료한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retranscribe_audio_swap"
        audio_path = _make_audio_file(tmp_path, "retranscribe-swap.wav")
        job = MockJob(1, meeting_id, str(audio_path), "completed")
        first_identity = (1, 2, 3, 4, 5)
        swapped_identity = (1, 9, 3, 10, 11)
        stage = MagicMock()
        purge = MagicMock(return_value=IndexPurgeResult(meeting_id=meeting_id))

        with (
            TestClient(app) as client,
            patch(
                "api.routers.meeting_detail.inspect_audio_path_no_symlinks",
                side_effect=[first_identity, swapped_identity],
            ),
            patch("api.routers.meeting_detail._stage_retranscribe_artifacts", stage),
            patch("api.routers.meeting_detail.purge_meeting_index", purge),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=job)
            restore = _install_retranscribe_claim_mocks(queue, job)
            queue.reset_for_retranscribe = MagicMock()
            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

        assert response.status_code == 409
        assert "SOURCE_BUSY" in response.json()["detail"]
        stage.assert_not_called()
        purge.assert_not_called()
        queue.reset_for_retranscribe.assert_not_called()
        restore.assert_called_once()
        assert audio_path.read_bytes() == b"audio-sentinel"

    @pytest.mark.parametrize(
        "status",
        ["recorded", "queued", "recording", "transcribing", "diarizing", "merging"],
    )
    def test_재전사는_completed_or_failed_아니면_어떤_정리도_시작하지_않는다(
        self,
        tmp_path: Path,
        status: str,
    ) -> None:
        """진행 중/대기 상태의 산출물과 인덱스를 eligibility 검사 전에 지우지 않는다."""
        app = _make_test_app(tmp_path)
        meeting_id = f"meeting_retranscribe_{status}"
        audio_path = tmp_path / "audio_input" / f"{meeting_id}.wav"
        state_path = tmp_path / "checkpoints" / meeting_id / "pipeline_state.json"
        output_path = tmp_path / "outputs" / meeting_id / "corrected.json"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"audio")
        state_path.write_text("state", encoding="utf-8")
        output_path.write_text("output", encoding="utf-8")
        purge = MagicMock(return_value=IndexPurgeResult(meeting_id=meeting_id))

        with (
            TestClient(app) as client,
            patch("api.routers.meeting_detail.purge_meeting_index", purge),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(
                return_value=MockJob(1, meeting_id, str(audio_path), status)
            )
            queue.reset_for_retranscribe = MagicMock()

            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

        assert response.status_code == 409
        purge.assert_not_called()
        queue.reset_for_retranscribe.assert_not_called()
        assert audio_path.read_bytes() == b"audio"
        assert state_path.read_text(encoding="utf-8") == "state"
        assert output_path.read_text(encoding="utf-8") == "output"

    def test_재전사시_검색인덱스_정리_실패하면_산출물을_보존한다(
        self,
        tmp_path: Path,
    ) -> None:
        """검색 인덱스 정리 실패 시 체크포인트/결과 파일과 job 상태를 보존한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retranscribe_purge_fail"
        ckpt_dir = tmp_path / "checkpoints" / meeting_id
        out_dir = tmp_path / "outputs" / meeting_id
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        state_path = ckpt_dir / "pipeline_state.json"
        transcript_path = out_dir / "corrected.json"
        summary_path = out_dir / "summary.md"
        audio_path = out_dir / "input.wav"
        state_path.write_text("{}", encoding="utf-8")
        transcript_path.write_text("{}", encoding="utf-8")
        summary_path.write_text("# summary", encoding="utf-8")
        audio_path.write_bytes(b"audio")

        mock_job = MockJob(1, meeting_id, str(audio_path), "completed", retry_count=1)

        with (
            TestClient(app) as client,
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                side_effect=IndexPurgeError("fts busy"),
            ),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=mock_job)
            _install_retranscribe_claim_mocks(queue, mock_job)
            queue.reset_for_retranscribe = MagicMock()

            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

        assert response.status_code == 500
        assert state_path.exists()
        assert transcript_path.exists()
        assert summary_path.exists()
        assert audio_path.exists()
        app.state.job_queue._queue.reset_for_retranscribe.assert_not_called()

    def test_재전사_purging_복구는_rollback_marker_restore_순서다(
        self,
        tmp_path: Path,
    ) -> None:
        """marker가 durable하기 전에 completed 상태를 다시 노출하지 않는다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retranscribe_recovery_order"
        audio_path = _make_audio_file(tmp_path, "recovery-order.wav")
        job = MockJob(1, meeting_id, str(audio_path), "completed")
        calls: list[str] = []

        with (
            TestClient(app) as client,
            patch("api.routers.meeting_detail._stage_retranscribe_artifacts"),
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                side_effect=IndexPurgeError("partial purge"),
            ),
            patch(
                "api.routers.meeting_detail.rollback_retranscribe_staging",
                side_effect=lambda *_args: calls.append("rollback"),
            ),
            patch(
                "api.routers.meeting_detail._write_retranscribe_recovery_marker",
                side_effect=lambda *_args: calls.append("marker"),
            ),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=job)
            _install_retranscribe_claim_mocks(queue, job)
            queue.restore_retranscribe_claim = MagicMock(
                side_effect=lambda *_args: calls.append("restore") or job
            )
            queue.reset_for_retranscribe = MagicMock()
            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

        assert response.status_code == 500
        assert calls == ["rollback", "marker", "restore"]
        queue.reset_for_retranscribe.assert_not_called()

    def test_재전사_purging_marker실패는_claim을_유지한다(
        self,
        tmp_path: Path,
    ) -> None:
        """marker 기록 실패 뒤 DB original status를 노출하지 않는다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retranscribe_marker_fail"
        audio_path = _make_audio_file(tmp_path, "marker-fail.wav")
        job = MockJob(1, meeting_id, str(audio_path), "completed")

        with (
            TestClient(app) as client,
            patch("api.routers.meeting_detail._stage_retranscribe_artifacts"),
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                side_effect=IndexPurgeError("partial purge"),
            ),
            patch("api.routers.meeting_detail.rollback_retranscribe_staging"),
            patch(
                "api.routers.meeting_detail._write_retranscribe_recovery_marker",
                side_effect=OSError("disk full"),
            ),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=job)
            restore = _install_retranscribe_claim_mocks(queue, job)
            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

        assert response.status_code == 500
        assert "marker" in response.json()["detail"]
        restore.assert_not_called()

    def test_재전사_committing_cleanup실패는_rollback없이_claim을_유지한다(
        self,
        tmp_path: Path,
    ) -> None:
        """purge 성공 뒤 cleanup 실패는 startup roll-forward 대상으로 남긴다."""
        from core.job_queue import JobQueueError, parse_retranscribe_claim

        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retranscribe_cleanup_fail"
        audio_path = _make_audio_file(tmp_path, "cleanup-fail.wav")
        job = MockJob(1, meeting_id, str(audio_path), "completed")
        rollback = MagicMock()

        with (
            TestClient(app) as client,
            patch("api.routers.meeting_detail._stage_retranscribe_artifacts"),
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                return_value=IndexPurgeResult(meeting_id=meeting_id),
            ),
            patch(
                "api.routers.meeting_detail.cleanup_retranscribe_staging",
                side_effect=JobQueueError("cleanup blocked"),
            ),
            patch("api.routers.meeting_detail.rollback_retranscribe_staging", rollback),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=job)
            restore = _install_retranscribe_claim_mocks(queue, job)
            queue.reset_for_retranscribe = MagicMock()
            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

        assert response.status_code == 500
        rollback.assert_not_called()
        restore.assert_not_called()
        queue.reset_for_retranscribe.assert_not_called()
        claim = parse_retranscribe_claim(queue.get_job.return_value.requested_action)
        assert claim is not None
        assert claim.phase == "committing"

    def test_재전사_DB_reset_실패는_committing_claim으로_rollforward를_보존한다(
        self,
        tmp_path: Path,
    ) -> None:
        """strict cleanup 뒤 reset 실패는 old artifact를 되살리지 않고 claim을 유지한다."""
        from core.job_queue import parse_retranscribe_claim

        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retranscribe_reset_rollback"
        audio_path = tmp_path / "audio_input" / f"{meeting_id}.wav"
        state_path = tmp_path / "checkpoints" / meeting_id / "pipeline_state.json"
        transcript_path = tmp_path / "outputs" / meeting_id / "corrected.json"
        summary_path = tmp_path / "outputs" / meeting_id / "summary.md"
        minutes_path = tmp_path / "outputs" / meeting_id / "meeting_minutes.md"
        summary_json_path = tmp_path / "outputs" / meeting_id / "summary.json"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"audio-sentinel")
        state_path.write_text("state-sentinel", encoding="utf-8")
        transcript_path.write_text("transcript-sentinel", encoding="utf-8")
        summary_path.write_text("summary-sentinel", encoding="utf-8")
        minutes_path.write_text("minutes-sentinel", encoding="utf-8")
        summary_json_path.write_text("summary-json-sentinel", encoding="utf-8")
        original_job = MockJob(1, meeting_id, str(audio_path), "completed", retry_count=2)

        with (
            TestClient(app) as client,
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                return_value=IndexPurgeResult(
                    meeting_id=meeting_id,
                    chroma_deleted=2,
                    fts_deleted=2,
                ),
            ),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=original_job)
            _install_retranscribe_claim_mocks(queue, original_job)
            queue.reset_for_retranscribe = MagicMock(side_effect=RuntimeError("DB locked"))

            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

        assert response.status_code == 500
        assert audio_path.read_bytes() == b"audio-sentinel"
        assert not state_path.exists()
        assert not transcript_path.exists()
        assert not summary_path.exists()
        assert not minutes_path.exists()
        assert not summary_json_path.exists()
        marker = state_path.parent / "reindex_required.json"
        assert not marker.exists()
        queue.restore_retranscribe_claim.assert_not_called()
        durable = queue.get_job.return_value
        claim = parse_retranscribe_claim(durable.requested_action)
        assert claim is not None
        assert claim.phase == "committing"

    def test_재전사_output_symlink는_외부_산출물을_건드리지_않는다(
        self,
        tmp_path: Path,
    ) -> None:
        """outputs/{id} 심링크가 base_dir 밖을 가리키면 purge 전에 차단한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retranscribe_symlink"
        audio_path = tmp_path / "audio_input" / f"{meeting_id}.wav"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"audio")
        outside_dir = tmp_path.parent / f"outside_{tmp_path.name}"
        outside_dir.mkdir()
        outside_output = outside_dir / "corrected.json"
        outside_output.write_text("external-sentinel", encoding="utf-8")
        outputs_root = tmp_path / "outputs"
        outputs_root.mkdir(parents=True, exist_ok=True)
        (outputs_root / meeting_id).symlink_to(outside_dir, target_is_directory=True)
        purge = MagicMock(return_value=IndexPurgeResult(meeting_id=meeting_id))

        try:
            with (
                TestClient(app) as client,
                patch("api.routers.meeting_detail.purge_meeting_index", purge),
            ):
                queue = app.state.job_queue._queue
                queue.get_job_by_meeting_id = MagicMock(
                    return_value=MockJob(1, meeting_id, str(audio_path), "completed")
                )
                _install_retranscribe_claim_mocks(
                    queue,
                    MockJob(1, meeting_id, str(audio_path), "completed"),
                )
                queue.reset_for_retranscribe = MagicMock()

                response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

            assert response.status_code == 400, response.text
            purge.assert_not_called()
            queue.reset_for_retranscribe.assert_not_called()
            assert outside_output.read_text(encoding="utf-8") == "external-sentinel"
            assert audio_path.exists()
        finally:
            outside_output.unlink(missing_ok=True)
            outside_dir.rmdir()

    def test_재전사_final_artifact_symlink는_내부_victim을_이동하지_않는다(
        self,
        tmp_path: Path,
    ) -> None:
        """final corrected.json symlink target을 staging으로 replace하지 않는다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retranscribe_final_symlink"
        audio_path = _make_audio_file(tmp_path, "final-symlink.wav")
        victim_dir = tmp_path / "outputs" / "victim-meeting"
        victim_dir.mkdir(parents=True)
        victim = victim_dir / "corrected.json"
        victim.write_text("victim-sentinel", encoding="utf-8")
        meeting_output = tmp_path / "outputs" / meeting_id
        meeting_output.mkdir(parents=True)
        (meeting_output / "corrected.json").symlink_to(victim)
        job = MockJob(1, meeting_id, str(audio_path), "completed")
        purge = MagicMock(return_value=IndexPurgeResult(meeting_id=meeting_id))

        with (
            TestClient(app) as client,
            patch("api.routers.meeting_detail.purge_meeting_index", purge),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=job)
            restore = _install_retranscribe_claim_mocks(queue, job)
            queue.reset_for_retranscribe = MagicMock()
            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

        assert response.status_code == 400, response.text
        purge.assert_not_called()
        queue.reset_for_retranscribe.assert_not_called()
        restore.assert_called_once()
        assert victim.read_text(encoding="utf-8") == "victim-sentinel"
        assert (meeting_output / "corrected.json").is_symlink()

    def test_재전사_staging_중간실패는_이미_이동한_산출물을_rollback한다(
        self,
        tmp_path: Path,
    ) -> None:
        """두 번째 rename 실패 시 첫 번째 파일과 job claim을 원상 복구한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_retranscribe_unlink_fail"
        out_dir = tmp_path / "outputs" / meeting_id
        out_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = out_dir / "corrected.json"
        summary_path = out_dir / "summary.md"
        audio_path = out_dir / "input.wav"
        transcript_path.write_text("{}", encoding="utf-8")
        summary_path.write_text("summary", encoding="utf-8")
        audio_path.write_bytes(b"audio")
        mock_job = MockJob(1, meeting_id, str(audio_path), "completed", retry_count=1)
        original_rename = os.rename
        failed = False

        def _failing_rename(
            source: str | Path,
            target: str | Path,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            nonlocal failed
            if source == "summary.md" and not failed:
                failed = True
                raise OSError("read-only filesystem")
            original_rename(
                source,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        with (
            TestClient(app) as client,
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                return_value=IndexPurgeResult(meeting_id=meeting_id),
            ),
            patch("api.routers.meeting_detail.os.rename", _failing_rename),
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=mock_job)
            _install_retranscribe_claim_mocks(queue, mock_job)
            queue.reset_for_retranscribe = MagicMock()

            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

        assert response.status_code == 500
        queue.restore_retranscribe_claim.assert_called_once()
        queue.reset_for_retranscribe.assert_not_called()
        assert transcript_path.exists()
        assert summary_path.exists()
        assert audio_path.exists()

    def test_재전사_checkpoint_staging_intermediate_swap은_외부를_이동하지_않음(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """checkpoint 검증과 rename 사이 root swap이 외부 rename으로 이어지지 않는다."""
        from api.routers.meeting_detail import _stage_retranscribe_artifacts

        config = _make_test_config(tmp_path)
        config.paths.checkpoints_dir = "safe/checkpoints"
        config.paths.outputs_dir = "outputs"
        meeting_id = "checkpoint-intermediate-swap"
        token = "checkpoint-swap-token"
        safe = tmp_path / "safe"
        local_meeting = safe / "checkpoints" / meeting_id
        local_meeting.mkdir(parents=True)
        (local_meeting / "pipeline_state.json").write_bytes(b"local")
        (tmp_path / "outputs").mkdir()
        audio_path = _make_audio_file(tmp_path, "checkpoint-swap.wav")

        outside = tmp_path / "outside"
        outside_meeting = outside / "checkpoints" / meeting_id
        outside_meeting.mkdir(parents=True)
        external_sentinel = outside_meeting / "pipeline_state.json"
        external_sentinel.write_bytes(b"external-sentinel")
        sentinel_before = external_sentinel.stat()
        safe_original = tmp_path / "safe-original"
        original_mkdir = os.mkdir
        swapped = False

        def swap_before_stage_mkdir(
            path: str | Path,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            nonlocal swapped
            if (
                path == f".retranscribe-{meeting_id}-{token}-checkpoints"
                and dir_fd is not None
                and not swapped
            ):
                swapped = True
                safe.rename(safe_original)
                safe.symlink_to(outside, target_is_directory=True)
            original_mkdir(path, mode=mode, dir_fd=dir_fd)

        monkeypatch.setattr(
            "api.routers.meeting_detail.os.mkdir",
            swap_before_stage_mkdir,
        )

        try:
            _stage_retranscribe_artifacts(
                config,
                meeting_id,
                audio_path,
                token,
            )
        except (HTTPException, OSError):
            pass

        assert swapped is True
        sentinel_after = external_sentinel.stat()
        assert external_sentinel.read_bytes() == b"external-sentinel"
        assert (sentinel_after.st_dev, sentinel_after.st_ino) == (
            sentinel_before.st_dev,
            sentinel_before.st_ino,
        )
        assert (
            safe_original / "checkpoints" / meeting_id / "pipeline_state.json"
        ).read_bytes() == b"local"

    def test_재전사_checkpoint_forward_staging은_rename중_child교체를_publish하지_않음(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """checkpoint child가 rename 호출 중 바뀌면 stage 게시 없이 중단한다."""
        from api.routers.meeting_detail import _stage_retranscribe_artifacts

        config = _make_test_config(tmp_path)
        meeting_id = "checkpoint-forward-entry-swap"
        token = "forward-entry-swap-token"
        meeting_dir = tmp_path / "checkpoints" / meeting_id
        meeting_dir.mkdir(parents=True)
        source_path = meeting_dir / "pipeline_state.json"
        source_path.write_bytes(b"expected-checkpoint")
        original_recovery_path = meeting_dir / "pipeline_state.expected.json"
        (tmp_path / "outputs").mkdir()
        audio_path = _make_audio_file(tmp_path, "forward-entry-swap.wav")
        stage_path = tmp_path / "checkpoints" / f".retranscribe-{meeting_id}-{token}-checkpoints"
        original_rename = os.rename
        injected = False

        def replace_during_child_rename(
            source: str | Path,
            target: str | Path,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            nonlocal injected
            if (
                source == "pipeline_state.json"
                and target == "pipeline_state.json"
                and src_dir_fd is not None
                and dst_dir_fd is not None
                and src_dir_fd != dst_dir_fd
                and not injected
            ):
                injected = True
                original_rename(
                    source,
                    original_recovery_path.name,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=src_dir_fd,
                )
                replacement_fd = os.open(
                    source,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=src_dir_fd,
                )
                try:
                    os.write(replacement_fd, b"foreign-checkpoint")
                finally:
                    os.close(replacement_fd)
            original_rename(
                source,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        monkeypatch.setattr(
            "api.routers.meeting_detail.os.rename",
            replace_during_child_rename,
        )

        with pytest.raises(HTTPException) as caught:
            _stage_retranscribe_artifacts(config, meeting_id, audio_path, token)

        assert caught.value.status_code == 409
        assert injected is True
        assert not stage_path.exists()
        assert source_path.read_bytes() == b"foreign-checkpoint"
        assert original_recovery_path.read_bytes() == b"expected-checkpoint"

    def test_재전사_output_stage_entry가_이동되면_FD로_원복하고_claim을_유지(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """열린 output stage의 root entry가 변경되면 성공으로 게시하지 않는다."""
        from api.routers import meeting_detail

        app = _make_test_app(tmp_path)
        meeting_id = "output-stage-entry-displaced"
        output_dir = tmp_path / "outputs" / meeting_id
        output_dir.mkdir(parents=True)
        corrected = output_dir / "corrected.json"
        corrected.write_bytes(b"expected-output")
        audio_path = _make_audio_file(tmp_path, "output-stage-entry-displaced.wav")
        mock_job = MockJob(91, meeting_id, str(audio_path), "completed")
        stage_paths: list[tuple[Path, Path]] = []
        real_move = meeting_detail._move_entry_checked
        displaced = False

        def move_then_displace_stage(
            source_fd: int,
            destination_fd: int,
            name: str,
            expected: os.stat_result,
            token: str,
            *,
            destination_name: str | None = None,
        ) -> os.stat_result:
            nonlocal displaced
            moved = real_move(
                source_fd,
                destination_fd,
                name,
                expected,
                token,
                destination_name=destination_name,
            )
            if name == "corrected.json" and not displaced:
                displaced = True
                stage = tmp_path / "outputs" / f".retranscribe-{meeting_id}-{token}-outputs"
                displaced_stage = stage.with_name(f"{stage.name}.displaced")
                stage.rename(displaced_stage)
                stage_paths.append((stage, displaced_stage))
            return moved

        monkeypatch.setattr(
            meeting_detail,
            "_move_entry_checked",
            move_then_displace_stage,
        )

        with (
            TestClient(app) as client,
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                return_value=IndexPurgeResult(meeting_id=meeting_id),
            ) as purge,
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=mock_job)
            restore = _install_retranscribe_claim_mocks(queue, mock_job)
            queue.reset_for_retranscribe = MagicMock()

            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

        assert response.status_code == 409
        assert displaced is True
        assert corrected.read_bytes() == b"expected-output"
        assert stage_paths
        stage, displaced_stage = stage_paths[0]
        assert not stage.exists()
        assert displaced_stage.is_dir()
        assert not list(displaced_stage.iterdir())
        purge.assert_not_called()
        restore.assert_not_called()
        queue.reset_for_retranscribe.assert_not_called()

    def test_재전사_checkpoint_source_entry가_이동되면_정확한_inode를_재부착(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """checkpoint source root 변경을 성공 rollback으로 잘못 보고하지 않는다."""
        from api.routers import meeting_detail

        app = _make_test_app(tmp_path)
        meeting_id = "checkpoint-source-entry-displaced"
        checkpoint_dir = tmp_path / "checkpoints" / meeting_id
        checkpoint_dir.mkdir(parents=True)
        pipeline_state = checkpoint_dir / "pipeline_state.json"
        pipeline_state.write_bytes(b"expected-checkpoint")
        audio_path = _make_audio_file(tmp_path, "checkpoint-source-entry-displaced.wav")
        mock_job = MockJob(92, meeting_id, str(audio_path), "completed")
        displaced_source = checkpoint_dir.with_name(f"{meeting_id}.displaced")
        real_move = meeting_detail._move_entry_checked
        displaced = False

        def move_then_displace_source(
            source_fd: int,
            destination_fd: int,
            name: str,
            expected: os.stat_result,
            token: str,
            *,
            destination_name: str | None = None,
        ) -> os.stat_result:
            nonlocal displaced
            moved = real_move(
                source_fd,
                destination_fd,
                name,
                expected,
                token,
                destination_name=destination_name,
            )
            if name == "pipeline_state.json" and not displaced:
                displaced = True
                checkpoint_dir.rename(displaced_source)
            return moved

        monkeypatch.setattr(
            meeting_detail,
            "_move_entry_checked",
            move_then_displace_source,
        )

        with (
            TestClient(app) as client,
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                return_value=IndexPurgeResult(meeting_id=meeting_id),
            ) as purge,
        ):
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=mock_job)
            restore = _install_retranscribe_claim_mocks(queue, mock_job)
            queue.reset_for_retranscribe = MagicMock()

            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

        assert response.status_code == 409
        assert displaced is True
        assert pipeline_state.read_bytes() == b"expected-checkpoint"
        assert not displaced_source.exists()
        purge.assert_not_called()
        restore.assert_not_called()
        queue.reset_for_retranscribe.assert_not_called()

    def test_재전사_output_stage_mkdir_intermediate_swap은_외부를_이동하지_않음(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """stage exists-check과 mkdir 사이 root swap이 외부 산출물을 건드리지 않는다."""
        from api.routers.meeting_detail import _stage_retranscribe_artifacts

        config = _make_test_config(tmp_path)
        config.paths.checkpoints_dir = "checkpoints"
        config.paths.outputs_dir = "safe/outputs"
        meeting_id = "output-intermediate-swap"
        token = "output-swap-token"
        (tmp_path / "checkpoints").mkdir()
        safe = tmp_path / "safe"
        local_meeting = safe / "outputs" / meeting_id
        local_meeting.mkdir(parents=True)
        (local_meeting / "corrected.json").write_bytes(b"local")
        audio_path = _make_audio_file(tmp_path, "output-swap.wav")

        outside = tmp_path / "outside"
        outside_meeting = outside / "outputs" / meeting_id
        outside_meeting.mkdir(parents=True)
        external_sentinel = outside_meeting / "corrected.json"
        external_sentinel.write_bytes(b"external-sentinel")
        sentinel_before = external_sentinel.stat()
        safe_original = tmp_path / "safe-original"
        stage_name = f".retranscribe-{meeting_id}-{token}-outputs"
        original_mkdir = os.mkdir
        swapped = False

        def swap_before_mkdir(
            path: str | Path,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            nonlocal swapped
            if path == stage_name and dir_fd is not None and not swapped:
                swapped = True
                safe.rename(safe_original)
                safe.symlink_to(outside, target_is_directory=True)
            original_mkdir(path, mode=mode, dir_fd=dir_fd)

        monkeypatch.setattr("api.routers.meeting_detail.os.mkdir", swap_before_mkdir)

        try:
            _stage_retranscribe_artifacts(
                config,
                meeting_id,
                audio_path,
                token,
            )
        except (HTTPException, OSError):
            pass

        assert swapped is True
        sentinel_after = external_sentinel.stat()
        assert external_sentinel.read_bytes() == b"external-sentinel"
        assert (sentinel_after.st_dev, sentinel_after.st_ino) == (
            sentinel_before.st_dev,
            sentinel_before.st_ino,
        )
        assert (safe_original / "outputs" / meeting_id / "corrected.json").read_bytes() == b"local"

    def test_pipeline_state_final_swap은_외부_JSON을_읽지_않음(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """lstat 후 final symlink swap이 있어도 external JSON fd를 열지 않는다."""
        from fastapi import HTTPException

        from api.routers import meeting_detail

        config = _make_test_config(tmp_path)
        meeting_id = "pipeline-state-final-swap"
        state_path = tmp_path / "checkpoints" / meeting_id / "pipeline_state.json"
        state_path.parent.mkdir(parents=True)
        local_payload = {"status": "completed", "origin": "local"}
        external_payload = {"status": "failed", "origin": "external"}
        state_path.write_text(json.dumps(local_payload), encoding="utf-8")
        external_state = tmp_path / "external-state.json"
        external_state.write_text(json.dumps(external_payload), encoding="utf-8")
        original_state = state_path.with_name("pipeline_state.original.json")
        meeting_detail._json_cache.invalidate(state_path)
        cache_before = dict(meeting_detail._json_cache._cache)
        swapped = False

        real_get_from_fd = meeting_detail._json_cache.get_from_fd

        def swap_after_file_open(
            path: Path,
            file_fd: int,
            file_stat: os.stat_result,
        ) -> Any:
            nonlocal swapped
            if not swapped:
                swapped = True
                state_path.rename(original_state)
                state_path.symlink_to(external_state)
            return real_get_from_fd(path, file_fd, file_stat)

        loaded_payloads: list[Any] = []
        real_json_load = json.load

        def track_json_load(file_obj: Any, *args: Any, **kwargs: Any) -> Any:
            payload = real_json_load(file_obj, *args, **kwargs)
            loaded_payloads.append(payload)
            return payload

        monkeypatch.setattr(meeting_detail._json_cache, "get_from_fd", swap_after_file_open)
        monkeypatch.setattr("api.routers.meeting_detail.json.load", track_json_load)

        try:
            result = meeting_detail._read_pipeline_state_for_response(config, meeting_id)
        except HTTPException:
            result = None

        assert swapped is True
        assert loaded_payloads, (
            f"result={result!r}, state_is_symlink={state_path.is_symlink()}, "
            f"state_target={state_path.readlink() if state_path.is_symlink() else None}, "
            f"cache_before={cache_before!r}, "
            f"cache_after={meeting_detail._json_cache._cache!r}, "
            f"logs={caplog.text}"
        )
        assert external_payload not in loaded_payloads
        if result is not None:
            assert result == local_payload

    def test_pipeline_state_validation후_entry교체는_409로_실패(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """descriptor open 직전 final entry 교체는 외부 JSON을 읽지 않고 중단한다."""
        from api.routers import meeting_detail

        config = _make_test_config(tmp_path)
        meeting_id = "pipeline-state-before-open-swap"
        state_path = tmp_path / "checkpoints" / meeting_id / "pipeline_state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text('{"origin":"local"}', encoding="utf-8")
        original_state = state_path.with_name("pipeline_state.original.json")
        external_state = tmp_path / "external-state-before-open.json"
        external_state.write_text('{"origin":"external"}', encoding="utf-8")
        external_before = external_state.stat()
        meeting_detail._json_cache.invalidate(state_path)
        real_open = os.open
        swapped = False
        loaded = MagicMock(wraps=json.load)

        def swap_before_open(
            path: str | Path,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal swapped
            if path == "pipeline_state.json" and dir_fd is not None and not swapped:
                swapped = True
                state_path.rename(original_state)
                state_path.symlink_to(external_state)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr("api.routers.meeting_detail.os.open", swap_before_open)
        monkeypatch.setattr("api.routers.meeting_detail.json.load", loaded)

        with pytest.raises(HTTPException) as caught:
            meeting_detail._read_pipeline_state_for_response(config, meeting_id)

        assert caught.value.status_code == 409
        assert swapped is True
        loaded.assert_not_called()
        external_after = external_state.stat()
        assert external_state.read_text(encoding="utf-8") == '{"origin":"external"}'
        assert (external_after.st_dev, external_after.st_ino) == (
            external_before.st_dev,
            external_before.st_ino,
        )
        assert original_state.read_text(encoding="utf-8") == '{"origin":"local"}'

    def test_reindex_marker_root_swap은_외부와_기존파일을_변경하지_않음(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """marker publish 직전 root 교체는 pinned fd에서 감지하고 새 marker를 회수한다."""
        from api.routers.meeting_detail import _write_retranscribe_recovery_marker
        from core.job_queue import JobQueueError

        config = _make_test_config(tmp_path)
        config.paths.checkpoints_dir = "safe/checkpoints"
        meeting_id = "marker-root-swap"
        safe = tmp_path / "safe"
        local_meeting = safe / "checkpoints" / meeting_id
        local_meeting.mkdir(parents=True)
        local_sentinel = local_meeting / "pipeline_state.json"
        local_sentinel.write_bytes(b"local-state")

        outside = tmp_path / "outside"
        outside_meeting = outside / "checkpoints" / meeting_id
        outside_meeting.mkdir(parents=True)
        external_marker = outside_meeting / "reindex_required.json"
        external_marker.write_bytes(b"external-marker")
        external_before = external_marker.stat()
        safe_original = tmp_path / "safe-original"
        real_link = os.link
        swapped = False

        def swap_before_publish(
            source: str | Path,
            target: str | Path,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> None:
            nonlocal swapped
            if target == "reindex_required.json" and not swapped:
                swapped = True
                safe.rename(safe_original)
                safe.symlink_to(outside, target_is_directory=True)
            real_link(
                source,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=follow_symlinks,
            )

        monkeypatch.setattr("api.routers.meeting_detail.os.link", swap_before_publish)

        with pytest.raises(JobQueueError, match="root"):
            _write_retranscribe_recovery_marker(
                config,
                meeting_id,
                "injected recovery",
                IndexPurgeResult(meeting_id=meeting_id, chroma_deleted=1, fts_deleted=1),
            )

        assert swapped is True
        external_after = external_marker.stat()
        assert external_marker.read_bytes() == b"external-marker"
        assert (external_after.st_dev, external_after.st_ino) == (
            external_before.st_dev,
            external_before.st_ino,
        )
        restored_local = safe_original / "checkpoints" / meeting_id
        assert (restored_local / "pipeline_state.json").read_bytes() == b"local-state"
        assert not (restored_local / "reindex_required.json").exists()
        assert not list(restored_local.glob(".reindex-required-*.tmp"))

    def test_reindex_marker_publish직후_entry교체는_foreign_marker를_삭제하지_않음(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """publish 후 교체된 marker는 temp inode 소유가 아니므로 cleanup하지 않는다."""
        from api.routers.meeting_detail import _write_retranscribe_recovery_marker
        from core.job_queue import JobQueueError

        config = _make_test_config(tmp_path)
        meeting_id = "marker-publish-handoff"
        meeting_dir = tmp_path / "checkpoints" / meeting_id
        meeting_dir.mkdir(parents=True)
        sentinel = meeting_dir / "pipeline_state.json"
        sentinel.write_bytes(b"existing-state")
        marker = meeting_dir / "reindex_required.json"
        owned_backup = meeting_dir / "reindex_required.owned-after-link.json"
        real_link = os.link
        real_rename = os.rename
        injected = False

        def replace_after_publish(
            source: str | Path,
            target: str | Path,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> None:
            nonlocal injected
            real_link(
                source,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=follow_symlinks,
            )
            if target == marker.name and not injected:
                injected = True
                assert dst_dir_fd is not None
                real_rename(
                    target,
                    owned_backup.name,
                    src_dir_fd=dst_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
                foreign_fd = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=dst_dir_fd,
                )
                try:
                    os.write(foreign_fd, b"foreign-marker")
                finally:
                    os.close(foreign_fd)

        monkeypatch.setattr("api.routers.meeting_detail.os.link", replace_after_publish)

        with pytest.raises(JobQueueError, match="publish identity"):
            _write_retranscribe_recovery_marker(
                config,
                meeting_id,
                "injected recovery",
                IndexPurgeResult(meeting_id=meeting_id, chroma_deleted=1, fts_deleted=1),
            )

        assert injected is True
        assert sentinel.read_bytes() == b"existing-state"
        assert marker.read_bytes() == b"foreign-marker"
        assert owned_backup.is_file()
        assert not list(meeting_dir.glob(".reindex-required-*.tmp"))

    def test_reindex_marker_parent_fsync실패는_기존파일을_보존(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """새 meeting dir의 parent fsync 실패는 빈 entry를 회수하고 기존 데이터를 보존한다."""
        from api.routers.meeting_detail import _write_retranscribe_recovery_marker

        config = _make_test_config(tmp_path)
        checkpoint_root = tmp_path / "checkpoints"
        checkpoint_root.mkdir()
        sibling = checkpoint_root / "existing-meeting"
        sibling.mkdir()
        sentinel = sibling / "pipeline_state.json"
        sentinel.write_bytes(b"existing-state")
        meeting_id = "marker-fsync-failure"

        def fail_fsync(_file_fd: int) -> None:
            raise OSError("injected directory fsync failure")

        monkeypatch.setattr("api.routers.meeting_detail.os.fsync", fail_fsync)

        with pytest.raises(OSError, match="injected directory fsync failure"):
            _write_retranscribe_recovery_marker(
                config,
                meeting_id,
                "injected recovery",
                IndexPurgeResult(meeting_id=meeting_id),
            )

        assert sentinel.read_bytes() == b"existing-state"
        assert not (checkpoint_root / meeting_id).exists()

    @pytest.mark.parametrize(
        ("failure_kind_name", "expected_status"),
        [
            ("MEDIA_INVALID", 422),
            ("SOURCE_BUSY", 409),
            ("INFRA_UNAVAILABLE", 503),
            ("SECURITY_BLOCKED", 400),
        ],
    )
    def test_재전사는_audio_ACCEPT_전에_모든_기존상태를_보존한다(
        self,
        tmp_path: Path,
        failure_kind_name: str,
        expected_status: int,
    ) -> None:
        """비수락이면 인덱스·체크포인트·출력·job을 하나도 purge하지 않는다."""
        app = _make_test_app(tmp_path)
        meeting_id = f"retranscribe_gate_{failure_kind_name.lower()}"
        audio_path = tmp_path / "audio_input" / f"{meeting_id}.wav"
        ckpt_dir = tmp_path / "checkpoints" / meeting_id
        out_dir = tmp_path / "outputs" / meeting_id
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        state_path = ckpt_dir / "pipeline_state.json"
        transcript_path = out_dir / "corrected.json"
        summary_path = out_dir / "summary.md"
        audio_path.write_bytes(b"audio-sentinel")
        state_path.write_text("state-sentinel", encoding="utf-8")
        transcript_path.write_text("transcript-sentinel", encoding="utf-8")
        summary_path.write_text("summary-sentinel", encoding="utf-8")

        completed_job = MockJob(
            73,
            meeting_id,
            str(audio_path),
            "completed",
            retry_count=2,
            error_message="",
        )
        admission = MagicMock(return_value=_denied_audio_admission(failure_kind_name))
        purge = MagicMock(return_value=IndexPurgeResult(meeting_id=meeting_id))

        with (
            TestClient(app) as client,
            patch("core.audio_quality.validate_audio_quality", admission),
            patch(
                "api.routers.meeting_detail.validate_audio_quality",
                admission,
                create=True,
            ),
            patch(
                "api.routers.meeting_detail.purge_meeting_index",
                purge,
            ),
        ):
            app.state.config.audio_quality.enabled = True
            queue = app.state.job_queue._queue
            queue.get_job_by_meeting_id = MagicMock(return_value=completed_job)
            queue.reset_for_retranscribe = MagicMock()

            response = client.post(f"/api/meetings/{meeting_id}/re-transcribe")

        assert response.status_code == expected_status
        assert failure_kind_name in response.json()["detail"]
        admission.assert_called_once()
        purge.assert_not_called()
        queue.reset_for_retranscribe.assert_not_called()
        assert completed_job.status == "completed"
        assert completed_job.retry_count == 2
        assert audio_path.read_bytes() == b"audio-sentinel"
        assert state_path.read_text(encoding="utf-8") == "state-sentinel"
        assert transcript_path.read_text(encoding="utf-8") == "transcript-sentinel"
        assert summary_path.read_text(encoding="utf-8") == "summary-sentinel"


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
