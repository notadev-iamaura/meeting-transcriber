"""
백필 API 엔드포인트 테스트 (Reindex API Endpoint Tests)

목적:
    GET /api/reindex/status : 회의별 청크 카운트 집계
    POST /api/meetings/{id}/reindex : 단일 회의 재색인 (correct.json → chunker → embedder)
    POST /api/reindex/all : 백그라운드 일괄 백필 + WebSocket 진행 이벤트

배경:
    PIPELINE_STEPS 에 chunk/embed 단계가 추가되기 전에 완료된 회의들은
    ChromaDB / FTS5 인덱스가 없어 /api/chat 이 컨텍스트 없는 답변을 반환한다.
    이 API 들은 그런 기존 회의를 백필하기 위한 진입점.

의존성: pytest, fastapi.TestClient, unittest.mock
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from config import AppConfig, PathsConfig, ServerConfig


def _make_test_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
    )


def _make_test_app(tmp_path: Path) -> Any:
    from api.server import create_app

    config = _make_test_config(tmp_path)
    with (
        patch("search.hybrid_search.HybridSearchEngine", return_value=MagicMock()),
        patch("search.chat.ChatEngine", return_value=MagicMock()),
    ):
        app = create_app(config)
    return app


@dataclass
class _MockJob:
    """테스트용 Job."""

    id: int
    meeting_id: str
    audio_path: str = "/tmp/x.wav"
    status: str = "completed"
    retry_count: int = 0
    error_message: str = ""
    created_at: str = "2026-04-29T10:00:00"
    updated_at: str = "2026-04-29T10:30:00"


def _make_correct_checkpoint(checkpoints_dir: Path, meeting_id: str) -> Path:
    """테스트용 correct.json 체크포인트를 만든다.

    실제 CorrectedResult 스키마와 호환되는 최소 형태.
    """
    cp_dir = checkpoints_dir / meeting_id
    cp_dir.mkdir(parents=True, exist_ok=True)
    cp = cp_dir / "correct.json"
    cp.write_text(
        json.dumps(
            {
                "utterances": [
                    {
                        "speaker": "SPEAKER_00",
                        "start": 0.0,
                        "end": 2.0,
                        "text": "안녕하세요",
                        "original_text": "안녕하세요",
                        "was_corrected": False,
                    },
                ],
                "audio_path": "/tmp/x.wav",
                "num_speakers": 1,
                "speakers": ["SPEAKER_00"],
                "total_corrected": 0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return cp


def _create_completed_pipeline_state(tmp_path: Path, meeting_id: str) -> None:
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
                "completed_steps": ["convert", "transcribe", "diarize", "merge", "correct"],
                "skipped_steps": ["summarize"],
                "step_results": [],
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "corrected.json").write_text(
        json.dumps({"utterances": [{"text": "안녕하세요", "speaker": "SPEAKER_00"}]}),
        encoding="utf-8",
    )


# === GET /api/reindex/status ===


class TestIndexStatusEndpoint:
    """회의별 인덱싱 상태 조회 엔드포인트."""

    def test_index_status_빈_큐(self, tmp_path: Path) -> None:
        """회의가 0건이면 total=0, indexed=0, missing=0 반환."""
        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=[])
            response = client.get("/api/reindex/status")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["indexed"] == 0
        assert data["missing"] == 0
        assert data["missing_meeting_ids"] == []

    def test_index_status_모든_회의_인덱싱됨(self, tmp_path: Path) -> None:
        """ChromaDB 에 모든 회의의 청크가 있으면 missing=0."""
        app = _make_test_app(tmp_path)
        jobs = [
            _MockJob(id=1, meeting_id="m1", status="completed"),
            _MockJob(id=2, meeting_id="m2", status="completed"),
        ]
        # ChromaDB 카운트 mock: 두 회의 모두 청크 보유
        mock_collection = MagicMock()
        mock_collection.get = MagicMock(
            side_effect=lambda where=None, **_: {
                "ids": ["c1", "c2", "c3"] if where else [],
            }
        )
        with TestClient(app) as client:
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=jobs)
            with patch(
                "api.routers.reindex._get_chroma_collection_for_status",
                return_value=mock_collection,
            ):
                response = client.get("/api/reindex/status")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert data["indexed"] == 2
        assert data["missing"] == 0
        assert data["missing_meeting_ids"] == []

    def test_index_status_일부_누락(self, tmp_path: Path) -> None:
        """일부 회의가 ChromaDB 에 청크 0개면 missing 에 포함."""
        app = _make_test_app(tmp_path)
        jobs = [
            _MockJob(id=1, meeting_id="m1", status="completed"),
            _MockJob(id=2, meeting_id="m2_missing", status="completed"),
            _MockJob(id=3, meeting_id="m3", status="completed"),
        ]
        # m2_missing 만 ids 비어있음
        mock_collection = MagicMock()

        def _get_side_effect(where: dict | None = None, **_: Any) -> dict:
            if where and where.get("meeting_id") == "m2_missing":
                return {"ids": []}
            return {"ids": ["c1", "c2"]}

        mock_collection.get = MagicMock(side_effect=_get_side_effect)

        with TestClient(app) as client:
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=jobs)
            with patch(
                "api.routers.reindex._get_chroma_collection_for_status",
                return_value=mock_collection,
            ):
                response = client.get("/api/reindex/status")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert data["indexed"] == 2
        assert data["missing"] == 1
        assert "m2_missing" in data["missing_meeting_ids"]

    def test_index_status_완료_산출물이_있는_failed_작업은_복구_후_대상에_포함한다(
        self,
        tmp_path: Path,
    ) -> None:
        """reindex 상태도 completed 산출물이 있는 failed 작업을 completed 로 복구한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_reconcile_reindex"
        _create_completed_pipeline_state(tmp_path, meeting_id)

        failed_job = _MockJob(id=1, meeting_id=meeting_id, status="failed", retry_count=1)
        completed_job = _MockJob(id=1, meeting_id=meeting_id, status="completed", retry_count=1)
        mock_collection = MagicMock()
        mock_collection.get = MagicMock(return_value={"ids": []})

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=[failed_job])
            queue.force_set_status = MagicMock(return_value=completed_job)
            with patch(
                "api.routers.reindex._get_chroma_collection_for_status",
                return_value=mock_collection,
            ):
                response = client.get("/api/reindex/status")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["missing"] == 1
        assert data["missing_meeting_ids"] == [meeting_id]
        queue.force_set_status.assert_called_once()


# === POST /api/meetings/{id}/reindex ===


class TestReindexSingleEndpoint:
    """단일 회의 재색인 엔드포인트."""

    def test_reindex_체크포인트_없음_422(self, tmp_path: Path) -> None:
        """correct.json / merge.json 체크포인트 둘 다 없으면 422 반환."""
        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            app.state.job_queue.queue.get_job_by_meeting_id = MagicMock(
                return_value=_MockJob(id=1, meeting_id="m_no_cp"),
            )
            response = client.post("/api/meetings/m_no_cp/reindex")
        assert response.status_code == 422
        assert "체크포인트" in response.json()["detail"]

    def test_reindex_회의_없음_404(self, tmp_path: Path) -> None:
        """존재하지 않는 meeting_id 는 404."""
        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            app.state.job_queue.queue.get_job_by_meeting_id = MagicMock(
                return_value=None,
            )
            response = client.post("/api/meetings/no_such/reindex")
        assert response.status_code == 404

    def test_reindex_정상_경로(self, tmp_path: Path) -> None:
        """correct.json 이 있으면 chunker → embedder 실행 후 200."""
        app = _make_test_app(tmp_path)
        config = app.state.config
        meeting_id = "m_ok"
        _make_correct_checkpoint(config.paths.resolved_checkpoints_dir, meeting_id)

        # Chunker / Embedder 결과 mock
        mock_chunked = MagicMock()
        mock_chunked.chunks = [MagicMock()]
        mock_chunked.total_utterances = 1
        mock_chunked.num_speakers = 1

        mock_embedded = MagicMock()
        mock_embedded.total_chunks = 1
        mock_embedded.chroma_stored = True
        mock_embedded.fts_stored = True

        with TestClient(app) as client:
            app.state.job_queue.queue.get_job_by_meeting_id = MagicMock(
                return_value=_MockJob(id=1, meeting_id=meeting_id),
            )
            with (
                patch(
                    "api.routers.reindex._reindex_meeting",
                    new_callable=AsyncMock,
                    return_value={"chunks": 1, "chroma_stored": True, "fts_stored": True},
                ),
            ):
                response = client.post(f"/api/meetings/{meeting_id}/reindex")

        assert response.status_code == 200
        data = response.json()
        assert data["meeting_id"] == meeting_id
        assert data["chunks"] >= 0
        assert data["chroma_stored"] is True
        assert data["fts_stored"] is True


# === POST /api/reindex/all ===


class TestReindexAllEndpoint:
    """일괄 백필 엔드포인트."""

    def test_reindex_all_시작(self, tmp_path: Path) -> None:
        """누락 회의가 있으면 백그라운드 작업 시작 + 202 반환."""
        app = _make_test_app(tmp_path)
        jobs = [
            _MockJob(id=1, meeting_id="m1", status="completed"),
            _MockJob(id=2, meeting_id="m2", status="completed"),
        ]
        with TestClient(app) as client:
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=jobs)
            with patch(
                "api.routers.reindex._start_reindex_all", new_callable=AsyncMock
            ) as start_mock:
                response = client.post("/api/reindex/all")

        assert response.status_code == 202
        data = response.json()
        assert data["status"] in ("started", "running")
        assert "total" in data
        # 백그라운드 작업 트리거 호출 검증
        assert start_mock.called

    def test_reindex_all_동시_호출_409(self, tmp_path: Path) -> None:
        """이미 reindex-all 이 실행 중이면 두 번째 호출은 409."""
        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            app.state.reindex_lock_busy = True
            response = client.post("/api/reindex/all")
        assert response.status_code == 409
        assert "이미 진행" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_reindex_all_실제_동시_요청은_하나만_시작(self, tmp_path: Path) -> None:
        """busy check/set이 원자적이라 실제 동시 요청 중 하나만 시작해야 한다."""
        from api.routers import reindex

        config = _make_test_config(tmp_path)
        _make_correct_checkpoint(config.paths.resolved_checkpoints_dir, "m1")
        app = SimpleNamespace(state=SimpleNamespace(running_tasks=set(), config=config))
        request = SimpleNamespace(app=app)
        jobs = [_MockJob(id=1, meeting_id="m1", status="completed")]
        queue = MagicMock()

        async def slow_reconciled_jobs(*_args: Any, **_kwargs: Any) -> list[_MockJob]:
            await asyncio.sleep(0.05)
            return jobs

        with (
            patch("api.routers.reindex._get_job_queue", return_value=queue),
            patch("api.routers.reindex._get_config", return_value=config),
            patch("api.routers.reindex._get_reconciled_jobs", side_effect=slow_reconciled_jobs),
            patch("api.routers.reindex._get_chroma_collection_for_status", return_value=None),
            patch("api.routers.reindex._start_reindex_all", new_callable=AsyncMock),
        ):
            results = await asyncio.gather(
                reindex.reindex_all(request),  # type: ignore[arg-type]
                reindex.reindex_all(request),  # type: ignore[arg-type]
                return_exceptions=True,
            )

        started = [result for result in results if not isinstance(result, Exception)]
        conflicts = [
            result
            for result in results
            if isinstance(result, HTTPException) and result.status_code == 409
        ]
        assert len(started) == 1
        assert len(conflicts) == 1
