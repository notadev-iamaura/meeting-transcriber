"""
일괄 처리 API 테스트 모듈 (Bulk Action API Test Module)

목적: api/routes.py 의 `POST /api/meetings/batch` 엔드포인트(Phase 3)를 검증한다.
주요 테스트:
    - 입력 검증: action / scope / hours 범위 (422)
    - 대상 필터링: action 별 merge·summary 상태에 따른 자동 분류
    - scope 정책: all / recent (hours 윈도우) / selected (명시 ID)
    - 응답 형식: matched / queued / skipped 카운트 + status="no_targets"
    - 백그라운드 실행: pipeline.run / pipeline.run_llm_steps mock 호출 검증
    - 보안: path traversal 차단

의존성: pytest, fastapi (TestClient), unittest.mock
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from config import AppConfig, PathsConfig, ServerConfig

# === 헬퍼 ===


def _make_test_config(tmp_path: Path) -> AppConfig:
    """테스트용 AppConfig 를 생성한다.

    Args:
        tmp_path: pytest 임시 디렉토리

    Returns:
        AppConfig 인스턴스
    """
    return AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
    )


def _make_test_app(tmp_path: Path) -> Any:
    """테스트용 FastAPI 앱을 생성한다.

    HybridSearchEngine / ChatEngine 초기화를 패치하여 외부 의존성을 제거한다.

    Args:
        tmp_path: pytest 임시 디렉토리

    Returns:
        FastAPI 앱 인스턴스
    """
    from api.server import create_app

    config = _make_test_config(tmp_path)

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
    created_at: str = "2026-04-01T10:00:00"
    updated_at: str = "2026-04-01T10:30:00"
    title: str = ""


def _make_meeting_dirs(
    tmp_path: Path,
    meeting_id: str,
    *,
    has_merge: bool,
    has_summary: bool = False,
    summary_filename: str = "summary.md",
) -> None:
    """테스트용 회의 폴더 구조를 생성한다.

    체크포인트와 출력 파일의 존재 여부를 제어해 액션 분류를 테스트한다.

    Args:
        tmp_path: AppConfig.paths.base_dir
        meeting_id: 회의 ID
        has_merge: True 면 checkpoints/{id}/merge.json 생성
        has_summary: True 면 outputs/{id}/{summary_filename} 생성
        summary_filename: "summary.md" 또는 "meeting_minutes.md"
    """
    cp_dir = tmp_path / "checkpoints" / meeting_id
    cp_dir.mkdir(parents=True, exist_ok=True)
    if has_merge:
        (cp_dir / "merge.json").write_text(
            json.dumps({"utterances": [], "num_speakers": 1}),
            encoding="utf-8",
        )

    if has_summary:
        out_dir = tmp_path / "outputs" / meeting_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / summary_filename).write_text("# 회의록\n", encoding="utf-8")


def _setup_pipeline_mock(app: Any) -> MagicMock:
    """pipeline_manager 와 running_tasks 를 mock 으로 교체한다.

    백그라운드 태스크가 실제 파이프라인을 호출하지 않도록 차단하고,
    pipeline.run / pipeline.run_llm_steps 호출 여부를 검증할 수 있게 한다.

    Args:
        app: FastAPI 앱

    Returns:
        모킹된 pipeline_manager
    """
    mock_pipeline = MagicMock()
    mock_pipeline.run = AsyncMock()
    mock_pipeline.run_llm_steps = AsyncMock()
    app.state.pipeline_manager = mock_pipeline
    app.state.running_tasks = set()
    return mock_pipeline


async def _wait_for_background_tasks(app: Any, timeout: float = 5.0) -> None:
    """app.state.running_tasks 에 등록된 모든 백그라운드 태스크의 완료를 대기한다.

    Args:
        app: FastAPI 앱
        timeout: 최대 대기 시간 (초)
    """
    tasks = list(getattr(app.state, "running_tasks", set()))
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)


def _drain_background_tasks(client: TestClient, app: Any) -> None:
    """TestClient 의 이벤트 루프에서 백그라운드 태스크 완료를 대기한다.

    TestClient 는 startup/shutdown 사이에 portal(스레드)을 통해 async 코드를 실행한다.
    `client.portal.call(...)` 로 동일 루프에서 await 한다.
    """
    portal = getattr(client, "portal", None)
    if portal is None:
        return
    portal.call(_wait_for_background_tasks, app)


# ============================================================================
# 입력 검증 테스트
# ============================================================================


class TestBatchInputValidation:
    """POST /api/meetings/batch 입력 검증 (Pydantic 422)."""

    def test_batch_action_invalid_returns_422(self, tmp_path: Path) -> None:
        """알 수 없는 action 값은 422 Unprocessable Entity 를 반환한다."""
        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            _setup_pipeline_mock(app)
            response = client.post(
                "/api/meetings/batch",
                json={"action": "invalid", "scope": "all"},
            )
        assert response.status_code == 422

    def test_batch_scope_invalid_returns_422(self, tmp_path: Path) -> None:
        """알 수 없는 scope 값은 422 를 반환한다."""
        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            _setup_pipeline_mock(app)
            response = client.post(
                "/api/meetings/batch",
                json={"action": "summarize", "scope": "invalid"},
            )
        assert response.status_code == 422

    def test_batch_hours_out_of_range(self, tmp_path: Path) -> None:
        """hours 가 1~720 범위를 벗어나면 422 를 반환한다."""
        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            _setup_pipeline_mock(app)

            # hours=0 — 하한 미달
            r1 = client.post(
                "/api/meetings/batch",
                json={"action": "summarize", "scope": "recent", "hours": 0},
            )
            assert r1.status_code == 422

            # hours=721 — 상한 초과
            r2 = client.post(
                "/api/meetings/batch",
                json={"action": "summarize", "scope": "recent", "hours": 721},
            )
            assert r2.status_code == 422

    def test_batch_missing_required_fields_returns_422(self, tmp_path: Path) -> None:
        """action / scope 미지정 시 422."""
        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            _setup_pipeline_mock(app)
            r = client.post("/api/meetings/batch", json={})
        assert r.status_code == 422

    def test_batch_meeting_ids_too_long_returns_422(self, tmp_path: Path) -> None:
        """meeting_ids 길이가 max_length(500)를 초과하면 422 (DoS 방지).

        Phase 6 보안 감사 Medium-01 수정: 비정상적으로 큰 배열로 fs I/O
        폭주 / 정규식 매칭 폭주를 차단한다.
        """
        app = _make_test_app(tmp_path)
        # 501개: 정상 ID 형식이라도 길이만으로 거부되어야 함.
        too_many = [f"m_{i}" for i in range(501)]
        with TestClient(app) as client:
            _setup_pipeline_mock(app)
            r = client.post(
                "/api/meetings/batch",
                json={
                    "action": "summarize",
                    "scope": "selected",
                    "meeting_ids": too_many,
                },
            )
        assert r.status_code == 422


# ============================================================================
# 대상 필터링 테스트 (action 별 자동 분류)
# ============================================================================


class TestBatchActionFilter:
    """action 값에 따른 회의 자동 필터링 검증."""

    def test_batch_transcribe_all_filters_merge_done(self, tmp_path: Path) -> None:
        """action=transcribe + scope=all: merge.json 있는 회의는 제외된다."""
        app = _make_test_app(tmp_path)

        # 두 개 회의: 하나는 merge 완료, 하나는 미완료
        _make_meeting_dirs(tmp_path, "meeting_with_merge", has_merge=True)
        _make_meeting_dirs(tmp_path, "meeting_without_merge", has_merge=False)

        with TestClient(app) as client:
            mock_pipeline = _setup_pipeline_mock(app)
            # transcribe 액션은 audio_path 가 필요 — JobQueue mock 설정
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=MockJob(
                    1,
                    "meeting_without_merge",
                    str(tmp_path / "fake.wav"),
                ),
            )
            # audio_path 가 실제로 존재해야 pipeline.run 이 호출된다
            (tmp_path / "fake.wav").write_bytes(b"\x00" * 16)

            response = client.post(
                "/api/meetings/batch",
                json={"action": "transcribe", "scope": "all"},
            )
            _drain_background_tasks(client, app)

        assert response.status_code == 200
        data = response.json()
        # merge 미완료 회의만 큐잉
        assert data["queued"] == 1
        assert data["meeting_ids"] == ["meeting_without_merge"]
        # merge 완료 회의는 skipped
        assert "meeting_with_merge" not in data["meeting_ids"]
        # mock_pipeline.run 이 호출되었음
        assert mock_pipeline.run.called

    def test_batch_summarize_all_filters_summary_done(self, tmp_path: Path) -> None:
        """action=summarize + scope=all: summary.md 있는 회의는 제외된다."""
        app = _make_test_app(tmp_path)

        # merge.json 모두 있고, 한 쪽만 summary.md 보유
        _make_meeting_dirs(tmp_path, "m_done", has_merge=True, has_summary=True)
        _make_meeting_dirs(tmp_path, "m_pending", has_merge=True, has_summary=False)

        with TestClient(app) as client:
            mock_pipeline = _setup_pipeline_mock(app)
            response = client.post(
                "/api/meetings/batch",
                json={"action": "summarize", "scope": "all"},
            )
            _drain_background_tasks(client, app)

        assert response.status_code == 200
        data = response.json()
        assert data["queued"] == 1
        assert data["meeting_ids"] == ["m_pending"]
        # summarize 액션은 run_llm_steps 호출
        mock_pipeline.run_llm_steps.assert_called_once_with("m_pending")

    def test_batch_summarize_recognizes_meeting_minutes_md(self, tmp_path: Path) -> None:
        """summary.md 가 아닌 meeting_minutes.md 도 요약 완료로 인정한다."""
        app = _make_test_app(tmp_path)

        _make_meeting_dirs(
            tmp_path,
            "m_legacy",
            has_merge=True,
            has_summary=True,
            summary_filename="meeting_minutes.md",
        )

        with TestClient(app) as client:
            _setup_pipeline_mock(app)
            response = client.post(
                "/api/meetings/batch",
                json={"action": "summarize", "scope": "all"},
            )

        data = response.json()
        # meeting_minutes.md 가 있으므로 요약 대상에서 제외
        assert data["queued"] == 0
        assert data["status"] == "no_targets"

    def test_batch_full_all_includes_both_states(self, tmp_path: Path) -> None:
        """action=full + scope=all: merge 없음 + (merge 있고 summary 없음) 합집합."""
        app = _make_test_app(tmp_path)

        _make_meeting_dirs(tmp_path, "m_no_merge", has_merge=False)
        _make_meeting_dirs(tmp_path, "m_no_summary", has_merge=True, has_summary=False)
        _make_meeting_dirs(tmp_path, "m_done", has_merge=True, has_summary=True)

        with TestClient(app) as client:
            _setup_pipeline_mock(app)
            # transcribe 분기에서 호출되는 JobQueue mock
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=MockJob(
                    1,
                    "m_no_merge",
                    str(tmp_path / "fake.wav"),
                ),
            )
            (tmp_path / "fake.wav").write_bytes(b"\x00" * 16)

            response = client.post(
                "/api/meetings/batch",
                json={"action": "full", "scope": "all"},
            )
            _drain_background_tasks(client, app)

        data = response.json()
        # full = m_no_merge ∪ m_no_summary, m_done 은 skip
        assert data["queued"] == 2
        assert set(data["meeting_ids"]) == {"m_no_merge", "m_no_summary"}
        assert data["matched"] == 3
        assert data["skipped"] == 1

    def test_batch_full_skips_already_summarized(self, tmp_path: Path) -> None:
        """action=full: merge + summary 둘 다 있는 회의는 skip."""
        app = _make_test_app(tmp_path)

        _make_meeting_dirs(tmp_path, "m_done", has_merge=True, has_summary=True)

        with TestClient(app) as client:
            _setup_pipeline_mock(app)
            response = client.post(
                "/api/meetings/batch",
                json={"action": "full", "scope": "all"},
            )

        data = response.json()
        assert data["status"] == "no_targets"
        assert data["queued"] == 0
        assert data["matched"] == 1
        assert data["skipped"] == 1


# ============================================================================
# scope 정책 테스트
# ============================================================================


class TestBatchScope:
    """scope 별 후보 회의 수집 정책 검증."""

    def test_batch_scope_recent_filters_by_hours(self, tmp_path: Path) -> None:
        """scope=recent + hours=24: 25시간 전 회의는 제외된다."""
        app = _make_test_app(tmp_path)

        # 시간이 다른 두 Job
        recent_iso = (datetime.now() - timedelta(hours=1)).isoformat()
        old_iso = (datetime.now() - timedelta(hours=25)).isoformat()
        jobs = [
            MockJob(1, "m_recent", "/audio/a.wav", created_at=recent_iso),
            MockJob(2, "m_old", "/audio/b.wav", created_at=old_iso),
        ]

        # 두 회의 모두 summarize 대상으로 적합 (merge 있고 summary 없음)
        _make_meeting_dirs(tmp_path, "m_recent", has_merge=True, has_summary=False)
        _make_meeting_dirs(tmp_path, "m_old", has_merge=True, has_summary=False)

        with TestClient(app) as client:
            _setup_pipeline_mock(app)
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=jobs)

            response = client.post(
                "/api/meetings/batch",
                json={"action": "summarize", "scope": "recent", "hours": 24},
            )

        data = response.json()
        assert data["queued"] == 1
        assert data["meeting_ids"] == ["m_recent"]

    def test_batch_scope_recent_default_hours_is_24(self, tmp_path: Path) -> None:
        """scope=recent + hours 미지정 시 기본값 24 가 적용된다."""
        app = _make_test_app(tmp_path)

        recent_iso = (datetime.now() - timedelta(hours=1)).isoformat()
        old_iso = (datetime.now() - timedelta(hours=48)).isoformat()
        jobs = [
            MockJob(1, "m_in_24h", "/audio/a.wav", created_at=recent_iso),
            MockJob(2, "m_out_24h", "/audio/b.wav", created_at=old_iso),
        ]

        _make_meeting_dirs(tmp_path, "m_in_24h", has_merge=True, has_summary=False)
        _make_meeting_dirs(tmp_path, "m_out_24h", has_merge=True, has_summary=False)

        with TestClient(app) as client:
            _setup_pipeline_mock(app)
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=jobs)

            # hours 필드 없이 요청
            response = client.post(
                "/api/meetings/batch",
                json={"action": "summarize", "scope": "recent"},
            )

        data = response.json()
        # 기본 24h 윈도우 → m_out_24h(48시간 전) 제외
        assert data["queued"] == 1
        assert data["meeting_ids"] == ["m_in_24h"]

    def test_batch_scope_selected_uses_provided_ids(self, tmp_path: Path) -> None:
        """scope=selected: meeting_ids 명시 시 그것만 처리.

        부적합한 항목(이미 요약된 회의)은 skipped 카운트로 처리.
        """
        app = _make_test_app(tmp_path)

        # 두 회의: 하나는 요약 대상, 하나는 이미 완료
        _make_meeting_dirs(tmp_path, "m_pending", has_merge=True, has_summary=False)
        _make_meeting_dirs(tmp_path, "m_done", has_merge=True, has_summary=True)
        # 디스크에는 다른 회의도 있다 (selected 라서 무시되어야 함)
        _make_meeting_dirs(tmp_path, "m_other", has_merge=True, has_summary=False)

        with TestClient(app) as client:
            _setup_pipeline_mock(app)
            response = client.post(
                "/api/meetings/batch",
                json={
                    "action": "summarize",
                    "scope": "selected",
                    "meeting_ids": ["m_pending", "m_done"],
                },
            )

        data = response.json()
        assert data["matched"] == 2  # m_pending + m_done
        assert data["queued"] == 1
        assert data["skipped"] == 1
        assert data["meeting_ids"] == ["m_pending"]
        # m_other 는 selected 가 아니므로 영향 없음
        assert "m_other" not in data["meeting_ids"]


# ============================================================================
# 응답 형식 테스트
# ============================================================================


class TestBatchResponseShape:
    """matched / queued / skipped 카운트 + status 응답 검증."""

    def test_batch_no_targets_returns_no_targets_status(self, tmp_path: Path) -> None:
        """후보가 없거나 모두 부적합하면 status='no_targets', queued=0."""
        app = _make_test_app(tmp_path)

        # 빈 체크포인트 디렉토리
        (tmp_path / "checkpoints").mkdir(exist_ok=True)

        with TestClient(app) as client:
            _setup_pipeline_mock(app)
            response = client.post(
                "/api/meetings/batch",
                json={"action": "summarize", "scope": "all"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "no_targets"
        assert data["queued"] == 0
        assert data["meeting_ids"] == []

    def test_batch_returns_correct_counts(self, tmp_path: Path) -> None:
        """matched / queued / skipped 카운트가 일관되어야 한다."""
        app = _make_test_app(tmp_path)

        # 5개 회의: 2개는 요약 대상, 3개는 이미 완료
        for i in range(2):
            _make_meeting_dirs(tmp_path, f"m_pending_{i}", has_merge=True, has_summary=False)
        for i in range(3):
            _make_meeting_dirs(tmp_path, f"m_done_{i}", has_merge=True, has_summary=True)

        with TestClient(app) as client:
            _setup_pipeline_mock(app)
            response = client.post(
                "/api/meetings/batch",
                json={"action": "summarize", "scope": "all"},
            )

        data = response.json()
        assert data["matched"] == 5
        assert data["queued"] == 2
        assert data["skipped"] == 3
        # 항등식: matched == queued + skipped
        assert data["matched"] == data["queued"] + data["skipped"]
        assert data["status"] == "ok"
        # action / scope 가 echo 됨
        assert data["action"] == "summarize"
        assert data["scope"] == "all"

    def test_batch_invalid_meeting_id_rejected(self, tmp_path: Path) -> None:
        """경로 traversal 시도(meeting_ids 에 ../) 는 400 으로 차단된다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            _setup_pipeline_mock(app)
            response = client.post(
                "/api/meetings/batch",
                json={
                    "action": "summarize",
                    "scope": "selected",
                    "meeting_ids": ["../../etc/passwd"],
                },
            )

        # _validate_meeting_id 가 HTTPException(400) 를 발생시킴
        assert response.status_code == 400
        assert "유효하지 않은" in response.json()["detail"]

    def test_batch_response_schema_fields(self, tmp_path: Path) -> None:
        """응답 스키마의 필수 필드가 모두 포함되어 있어야 한다."""
        app = _make_test_app(tmp_path)
        _make_meeting_dirs(tmp_path, "m1", has_merge=True, has_summary=False)

        with TestClient(app) as client:
            _setup_pipeline_mock(app)
            response = client.post(
                "/api/meetings/batch",
                json={"action": "summarize", "scope": "all"},
            )

        data = response.json()
        for field in (
            "status",
            "message",
            "action",
            "scope",
            "matched",
            "queued",
            "skipped",
            "meeting_ids",
        ):
            assert field in data, f"응답에 {field!r} 필드가 누락됨: {data!r}"


# ============================================================================
# 백그라운드 실행 테스트
# ============================================================================


class TestBatchBackgroundExecution:
    """백그라운드 태스크 생성 및 파이프라인 메서드 호출 검증."""

    def test_batch_creates_background_task(self, tmp_path: Path) -> None:
        """큐잉 성공 시 running_tasks 에 태스크가 등록되고 메서드가 호출된다."""
        app = _make_test_app(tmp_path)
        _make_meeting_dirs(tmp_path, "m1", has_merge=True, has_summary=False)

        with TestClient(app) as client:
            mock_pipeline = _setup_pipeline_mock(app)
            response = client.post(
                "/api/meetings/batch",
                json={"action": "summarize", "scope": "all"},
            )
            _drain_background_tasks(client, app)

        assert response.status_code == 200
        # run_llm_steps 가 정확히 한 번 호출됨
        mock_pipeline.run_llm_steps.assert_called_once_with("m1")
        # pipeline.run 은 호출되지 않아야 함 (transcribe 분기 아님)
        assert not mock_pipeline.run.called

    def test_batch_full_routes_to_correct_pipeline_method(self, tmp_path: Path) -> None:
        """action=full: 분류에 따라 회의별로 다른 메서드를 호출한다.

        - merge 없는 회의 → pipeline.run(skip_llm_steps=True)
        - merge 있고 summary 없는 회의 → pipeline.run_llm_steps()
        """
        app = _make_test_app(tmp_path)

        _make_meeting_dirs(tmp_path, "m_no_merge", has_merge=False)
        _make_meeting_dirs(tmp_path, "m_no_summary", has_merge=True, has_summary=False)

        with TestClient(app) as client:
            mock_pipeline = _setup_pipeline_mock(app)
            # m_no_merge 의 audio_path 를 JobQueue mock 으로 제공
            audio = tmp_path / "input.wav"
            audio.write_bytes(b"\x00" * 16)
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=MockJob(1, "m_no_merge", str(audio)),
            )

            response = client.post(
                "/api/meetings/batch",
                json={"action": "full", "scope": "all"},
            )
            _drain_background_tasks(client, app)

        assert response.status_code == 200

        # m_no_merge → pipeline.run 호출 (skip_llm_steps=True)
        assert mock_pipeline.run.called
        run_call = mock_pipeline.run.call_args
        # 첫 번째 위치 인자가 audio_path
        assert run_call.args[0] == audio
        # meeting_id, skip_llm_steps 키워드 인자
        assert run_call.kwargs.get("meeting_id") == "m_no_merge"
        assert run_call.kwargs.get("skip_llm_steps") is True

        # m_no_summary → run_llm_steps 호출
        mock_pipeline.run_llm_steps.assert_called_once_with("m_no_summary")

    def test_batch_pipeline_not_initialized_returns_503(self, tmp_path: Path) -> None:
        """pipeline_manager 가 None 이면 503 을 반환한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.pipeline_manager = None
            response = client.post(
                "/api/meetings/batch",
                json={"action": "summarize", "scope": "all"},
            )

        assert response.status_code == 503
        assert "파이프라인" in response.json()["detail"]


# ============================================================================
# 동시성/통합 시나리오
# ============================================================================


class TestBatchIntegration:
    """여러 시나리오의 조합 검증."""

    def test_batch_transcribe_skips_when_audio_missing(self, tmp_path: Path) -> None:
        """transcribe 분기: audio_path 가 누락되면 사전 검증에서 skipped 로 분류된다.

        Phase 3 리뷰의 Major #2 수정: 응답의 queued 카운트는 실제 실행 가능한
        수와 정확히 일치해야 한다. audio_path 부재 회의는 큐잉 직전에 사전 제외.
        """
        app = _make_test_app(tmp_path)

        # merge 없음 → transcribe 후보지만 audio_path 없음
        _make_meeting_dirs(tmp_path, "m_no_audio", has_merge=False)

        with TestClient(app) as client:
            mock_pipeline = _setup_pipeline_mock(app)
            # JobQueue 가 회의를 모름 → audio_path 없음
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(return_value=None)

            response = client.post(
                "/api/meetings/batch",
                json={"action": "transcribe", "scope": "all"},
            )
            _drain_background_tasks(client, app)

        # 응답: matched=1 (후보), queued=0 (사전 제외), skipped=1, status="no_targets"
        assert response.status_code == 200
        data = response.json()
        assert data["matched"] == 1
        assert data["queued"] == 0
        assert data["skipped"] == 1
        assert data["status"] == "no_targets"
        # 백그라운드 태스크 자체가 만들어지지 않으므로 pipeline.run 미호출
        assert not mock_pipeline.run.called

    def test_batch_selected_dedupes_duplicate_meeting_ids(self, tmp_path: Path) -> None:
        """selected scope: 동일 meeting_id 가 중복 전송되면 한 번만 큐잉된다.

        Phase 3 리뷰의 Major #1 수정: LLM 토큰 낭비·summary.md 덮어쓰기를 방지.
        """
        app = _make_test_app(tmp_path)

        # 요약 대상으로 분류되도록 merge 만 있고 summary 없음.
        _make_meeting_dirs(tmp_path, "m_dup", has_merge=True, has_summary=False)

        with TestClient(app) as client:
            mock_pipeline = _setup_pipeline_mock(app)

            response = client.post(
                "/api/meetings/batch",
                json={
                    "action": "summarize",
                    "scope": "selected",
                    "meeting_ids": ["m_dup", "m_dup", "m_dup"],
                },
            )
            _drain_background_tasks(client, app)

        assert response.status_code == 200
        data = response.json()
        assert data["matched"] == 1, "중복 ID 는 후보 단계에서 1건으로 통합되어야 함"
        assert data["queued"] == 1
        assert data["meeting_ids"] == ["m_dup"]
        # pipeline.run_llm_steps 도 정확히 1회 호출
        assert mock_pipeline.run_llm_steps.call_count == 1
        mock_pipeline.run_llm_steps.assert_called_once_with("m_dup")

    def test_batch_full_action_audio_missing_counted_as_skipped(self, tmp_path: Path) -> None:
        """full 액션: audio 없는 transcribe 후보는 skipped 로 카운트된다.

        Phase 3 리뷰의 Major #2 보완: full 액션도 transcribe 분류 항목에
        대해 동일하게 사전 audio 검증을 통과해야 queued 에 포함된다.
        """
        app = _make_test_app(tmp_path)

        # 두 회의: 하나는 audio 없음(transcribe 후보, 제외 예정), 하나는 요약 후보(통과 예정)
        _make_meeting_dirs(tmp_path, "m_no_audio", has_merge=False)
        _make_meeting_dirs(tmp_path, "m_need_summary", has_merge=True, has_summary=False)

        with TestClient(app) as client:
            _setup_pipeline_mock(app)
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(return_value=None)

            response = client.post(
                "/api/meetings/batch",
                json={"action": "full", "scope": "all"},
            )
            _drain_background_tasks(client, app)

        assert response.status_code == 200
        data = response.json()
        assert data["matched"] == 2
        assert data["queued"] == 1, "audio 없는 transcribe 후보는 빠져야 함"
        assert data["skipped"] == 1
        assert data["meeting_ids"] == ["m_need_summary"]

    def test_batch_summarize_propagates_continues_after_failure(self, tmp_path: Path) -> None:
        """한 회의 실패가 다음 회의 처리를 막지 않아야 한다."""
        app = _make_test_app(tmp_path)

        _make_meeting_dirs(tmp_path, "m_will_fail", has_merge=True, has_summary=False)
        _make_meeting_dirs(tmp_path, "m_will_pass", has_merge=True, has_summary=False)

        with TestClient(app) as client:
            mock_pipeline = _setup_pipeline_mock(app)

            # m_will_fail 만 예외 발생, m_will_pass 는 정상 통과
            async def _maybe_fail(mid: str) -> None:
                if mid == "m_will_fail":
                    raise RuntimeError("의도된 실패")

            mock_pipeline.run_llm_steps = AsyncMock(side_effect=_maybe_fail)

            response = client.post(
                "/api/meetings/batch",
                json={"action": "summarize", "scope": "all"},
            )
            _drain_background_tasks(client, app)

        assert response.status_code == 200
        # 두 회의 모두 호출되어야 함 — 첫 실패가 두 번째 호출을 막지 않는다
        assert mock_pipeline.run_llm_steps.call_count == 2
        called_ids = sorted(call.args[0] for call in mock_pipeline.run_llm_steps.call_args_list)
        assert called_ids == ["m_will_fail", "m_will_pass"]

    def test_batch_audio_path_outside_base_dir_rejected(self, tmp_path: Path) -> None:
        """audio_path 가 base_dir 외부를 가리키면 사전 제외된다.

        Phase 6 보안 감사 Medium-02 수정: SQLite 직접 편집/심링크 공격으로
        임의 파일 경로가 들어와도 파이프라인에 도달하지 못한다.
        """
        app = _make_test_app(tmp_path)
        _make_meeting_dirs(tmp_path, "m_evil_path", has_merge=False)

        # base_dir 외부 (tmp_path 가 아닌 곳) 의 실재 파일 — /etc/hosts 같은 시스템 파일 사용.
        # 단순화를 위해 tmp_path 의 부모 (= /tmp 또는 /private/var/folders/...) 에 파일을 만든다.
        outside = tmp_path.parent / f"outside_{tmp_path.name}.wav"
        outside.write_bytes(b"fake audio")

        with TestClient(app) as client:
            mock_pipeline = _setup_pipeline_mock(app)
            app.state.job_queue._queue.get_job_by_meeting_id = MagicMock(
                return_value=MockJob(1, "m_evil_path", str(outside))
            )

            try:
                response = client.post(
                    "/api/meetings/batch",
                    json={"action": "transcribe", "scope": "all"},
                )
                _drain_background_tasks(client, app)
            finally:
                outside.unlink(missing_ok=True)

        # base_dir 외부라 사전 제외 → queued=0, status="no_targets"
        assert response.status_code == 200
        data = response.json()
        assert data["matched"] == 1
        assert data["queued"] == 0
        assert data["skipped"] == 1
        assert not mock_pipeline.run.called
