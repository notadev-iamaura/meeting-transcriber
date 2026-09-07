"""실제 저장소와 ASGI 경계에서 편집본 검색 일관성을 검증한다."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.websocket import ConnectionManager, ws_router
from config import AppConfig, PathsConfig
from core.io_utils import atomic_write_json_pinned
from core.meeting_mutation import MeetingMutationCoordinator
from core.search_revision import (
    filter_current_results,
    index_health,
    meeting_date,
    revision_status,
)
from search.hybrid_search import _search_fts, _search_vector
from steps.embedder import EmbeddedChunk, _ensure_fts_table, _store_chunks_fts


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    """실사용 데이터와 분리된 설정을 반환한다."""
    return AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))


def write_source(config: AppConfig, revision: str = "v1") -> None:
    """원문과 버전을 하나의 원자적 파일로 게시한다."""
    atomic_write_json_pinned(
        config.paths.resolved_outputs_dir / "meeting" / "corrected.json",
        {"search_revision": revision, "utterances": [{"text": "200만원"}]},
    )


def test_pending_failed_and_changed_query_results_are_excluded(config: AppConfig) -> None:
    """반영 전, 실패, 옛 질의 결과는 답변 근거에서 제외된다."""
    old = {
        "meeting_id": "meeting",
        "chunk_id": "c1",
        "text": "100만원",
        "date": "",
        "speakers": "S1",
    }
    new = {**old, "text": "200만원"}
    write_source(config)
    assert revision_status(config, "meeting") == "pending"
    assert filter_current_results(config, [old]) == []
    cp = config.paths.resolved_checkpoints_dir / "meeting"
    atomic_write_json_pinned(cp / "search_receipt.json", {"revision": "v1", "state": "failed"})
    assert revision_status(config, "meeting") == "failed"
    assert filter_current_results(config, [old]) == []
    atomic_write_json_pinned(cp / "embed.json", {"chunks": [{**new, "speakers": ["S1"]}]})
    atomic_write_json_pinned(cp / "search_receipt.json", {"revision": "v1"})
    assert filter_current_results(config, [old, new]) == [new]
    write_source(config, "v2")
    assert filter_current_results(config, [new]) == []


def test_source_date_is_preserved_and_unknown_is_not_today(config: AppConfig) -> None:
    """일반 파일명은 원본 시각을 사용하고 확인 불가능하면 미확정으로 둔다."""
    audio = Path(config.paths.base_dir) / "audio.wav"
    audio.write_bytes(b"fixture")
    os.utime(audio, (1609459200, 1609459200))
    date = meeting_date(config, "meeting", {"audio_path": str(audio)})
    assert date == "2021-01-01"
    audio.unlink()
    assert meeting_date(config, "meeting", {"meeting_date": date}) == date
    assert meeting_date(config, "meeting", {}) == ""


def test_pipeline_date_survives_source_timestamp_change(config: AppConfig) -> None:
    """최초 인덱싱 날짜는 원본 mtime 변경·삭제·상충하는 원문 날짜에도 유지된다."""
    from core.pipeline import PipelineManager
    from core.search_revision import preserve_meeting_date

    audio = Path(config.paths.base_dir) / "sample.wav"
    audio.write_bytes(b"fixture")
    os.utime(audio, (1609459200, 1609459200))
    manager = object.__new__(PipelineManager)
    manager._config = config
    assert manager._derive_meeting_date("meeting", audio) == "2021-01-01"
    os.utime(audio, (1704067200, 1704067200))
    assert manager._derive_meeting_date("meeting", audio) == "2021-01-01"
    audio.unlink()
    assert preserve_meeting_date(config, "meeting", {"meeting_date": "2024-01-01"}) == "2021-01-01"


def test_running_receipt_is_not_a_success(config: AppConfig) -> None:
    """실행 중 또는 손상된 영수증을 완료로 해석하지 않는다."""
    write_source(config)
    receipt = config.paths.resolved_checkpoints_dir / "meeting" / "search_receipt.json"
    atomic_write_json_pinned(receipt, {"revision": "v1", "state": "running"})
    assert revision_status(config, "meeting") == "running"
    atomic_write_json_pinned(receipt, {"revision": "v1", "state": "invalid"})
    assert revision_status(config, "meeting") == "unavailable"


def test_storage_failure_is_distinct_from_no_matches(config: AppConfig) -> None:
    """실제 FTS 스키마 누락과 벡터 오류는 정상 빈 검색과 구분된다."""
    from search.hybrid_search import SearchError

    with pytest.raises(SearchError, match="키워드 검색"):
        _search_fts("예산", config.paths.resolved_meetings_db, 5, strict=True)
    broken = MagicMock()
    broken.count.side_effect = RuntimeError("storage unavailable")
    with pytest.raises(SearchError, match="벡터 검색"):
        _search_vector([1.0], broken, 5, strict=True)


@pytest.mark.asyncio
async def test_running_job_is_recovered_after_restart(config: AppConfig) -> None:
    """실행 중 종료한 작업을 재시작 시 복구 대상으로 재등록한다."""
    from api.routers.reindex import recover_edited_indexes

    write_source(config)
    atomic_write_json_pinned(
        config.paths.resolved_checkpoints_dir / "meeting" / "search_receipt.json",
        {"revision": "v1", "state": "running"},
    )
    app = FastAPI()
    app.state.config = config
    app.state.job_queue = SimpleNamespace(
        get_all_jobs=AsyncMock(
            return_value=[SimpleNamespace(meeting_id="meeting", status="completed")]
        )
    )
    with patch("api.routers.reindex.schedule_edited_reindex", new_callable=AsyncMock) as schedule:
        await recover_edited_indexes(app)
    schedule.assert_awaited_once_with(app, "meeting")


@pytest.mark.asyncio
async def test_partial_search_failure_keeps_good_results_and_reports_source(
    config: AppConfig,
) -> None:
    """한 저장소 장애는 정상 결과를 없애지 않고 출처별 오류로 전달한다."""
    from search.hybrid_search import HybridSearchEngine, SearchError

    manager = MagicMock()
    manager.acquire.return_value = AsyncMock()
    engine = HybridSearchEngine(config=config, model_manager=manager)
    row = {
        "chunk_id": "c",
        "meeting_id": "meeting",
        "text": "예산",
        "date": "",
        "speakers": "S1",
        "start_time": 0,
        "end_time": 1,
        "chunk_index": 0,
    }
    with (
        patch.object(engine, "_embed_query", return_value=[1.0]),
        patch.object(engine, "_get_chroma_collection", return_value=None),
        patch.object(engine, "_get_fts_connection", return_value=None),
        patch("search.hybrid_search._search_vector", side_effect=SearchError("벡터 검색 실패")),
        patch("search.hybrid_search._search_fts", return_value=[row]),
    ):
        result = await engine.search("예산")
    assert len(result.results) == 1
    assert result.to_dict()["source_errors"] == {"vector": "벡터 검색 실패"}


@pytest.mark.parametrize(
    "origin,host",
    [
        (None, "127.0.0.1:8765"),
        ("null", "127.0.0.1:8765"),
        ("https://evil.example", "127.0.0.1:8765"),
        ("http://127.0.0.1:9999", "127.0.0.1:8765"),
        ("http://evil.example:8765", "evil.example:8765"),
        ("http://127.0.0.1:8765", "localhost:8765"),
    ],
)
def test_websocket_rejection_has_no_events(origin: str | None, host: str) -> None:
    """신뢰 경계 밖 연결은 accept와 이벤트 전송 이전에 거부된다."""
    app = FastAPI()
    app.include_router(ws_router)
    app.state.ws_manager = ConnectionManager()
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("ws://127.0.0.1:8765/ws/events", headers=headers):
            pytest.fail("신뢰할 수 없는 연결이 승인됨")
    assert exc.value.code == 1008
    assert app.state.ws_manager.connection_count == 0


def test_real_chroma_and_fts_exact_speaker_and_partial_index(
    config: AppConfig, tmp_path: Path
) -> None:
    """실제 Chroma와 FTS에서 CSV 화자를 정확히 찾고 한쪽 누락을 탐지한다."""
    import chromadb

    client = chromadb.PersistentClient(path=str(tmp_path / "chroma-test"))
    collection = client.get_or_create_collection("revision-test", embedding_function=None)
    speakers = ["SPEAKER_01", "SPEAKER_010", "SPEAKER_00,SPEAKER_01"]
    chunks = [
        EmbeddedChunk(
            chunk_id=f"c{i}",
            text="예산 200만원",
            embedding=[1.0, float(i + 1)],
            meeting_id="meeting",
            date="2021-01-01",
            speakers=s.split(","),
            start_time=0,
            end_time=1,
            chunk_index=i,
        )
        for i, s in enumerate(speakers)
    ]
    collection.upsert(
        ids=[c.chunk_id for c in chunks],
        embeddings=[c.embedding for c in chunks],
        documents=[c.text for c in chunks],
        metadatas=[
            {"meeting_id": "meeting", "date": c.date, "speakers": speakers[i]}
            for i, c in enumerate(chunks)
        ],
    )
    db = config.paths.resolved_meetings_db
    _ensure_fts_table(db)
    _store_chunks_fts(chunks, db, "meeting")
    for speaker, ids in [
        ("SPEAKER_01", {"c0", "c2"}),
        ("SPEAKER_010", {"c1"}),
        ("SPEAKER_09", set()),
    ]:
        vector = _search_vector(
            [1.0, 1.0],
            collection,
            10,
            speaker_filter=speaker,
            meeting_id_filter="meeting",
            date_filter="2021-01-01",
        )
        fts = _search_fts("예산", db, 10, speaker_filter=speaker, meeting_id_filter="meeting")
        assert {r["chunk_id"] for r in vector} == ids
        assert {r["chunk_id"] for r in fts} == ids
    cp = config.paths.resolved_checkpoints_dir / "meeting"
    atomic_write_json_pinned(
        cp / "embed.json", {"chunks": [{"chunk_id": c.chunk_id} for c in chunks]}
    )
    assert index_health(config, collection, "meeting") == "current"
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM chunks_fts WHERE chunk_id='c1'")
    assert index_health(config, collection, "meeting") == "incomplete"
    collection.delete(ids=["c1"])
    assert index_health(config, collection, "meeting") == "incomplete"


@pytest.mark.asyncio
async def test_restart_and_consecutive_edit_recover_latest_source(config: AppConfig) -> None:
    """편집 종료 후 재시작과 연속 편집은 FIFO에서 최신 버전 하나로 수렴한다."""
    from api.routers.reindex import recover_edited_indexes, schedule_edited_reindex

    write_source(config)
    coordinator = MeetingMutationCoordinator()
    job = SimpleNamespace(meeting_id="meeting", status="completed")
    app = FastAPI()
    app.state.config = config
    app.state.pipeline_manager = SimpleNamespace(_model_manager=object())
    app.state.meeting_mutation_coordinator = coordinator
    app.state.job_queue = SimpleNamespace(
        queue=SimpleNamespace(get_job_by_meeting_id=lambda mid: job),
        get_all_jobs=AsyncMock(return_value=[job]),
    )
    app.state.running_tasks = set()

    async def reindex(*args: object) -> None:
        """최신 원문 버전으로 성공 영수증을 게시한다."""
        from core.search_revision import read_source

        _, source = read_source(config, "meeting")
        atomic_write_json_pinned(
            config.paths.resolved_checkpoints_dir / "meeting" / "search_receipt.json",
            {"revision": source["search_revision"]},
        )

    with patch("api.routers.reindex._reindex_meeting", side_effect=reindex) as worker:
        async with coordinator.lease("meeting"):
            await recover_edited_indexes(app)
            write_source(config, "v2")
            await schedule_edited_reindex(app, "meeting")
            assert worker.call_count == 0
        await asyncio.gather(*list(app.state.running_tasks))
        assert worker.call_count == 1
    assert revision_status(config, "meeting") == "current"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters",
    [
        {"meeting_id_filter": "m"},
        {"date_filter": "2021-01-01"},
        {"speaker_filter": "S1"},
        {"meeting_id_filter": "m", "speaker_filter": "S1"},
    ],
)
async def test_scoped_wiki_questions_delegate_without_router(
    filters: dict[str, str], tmp_path: Path
) -> None:
    """범위 지정 질문은 Wiki 라우터가 선택될 수 있어도 RAG 필터를 보존한다."""
    from core.wiki.chat_integration import HybridChatService
    from core.wiki.store import WikiStore

    rag = SimpleNamespace(respond=AsyncMock(return_value={"answer": "범위 안 답변"}))
    wiki_router = MagicMock()
    service = HybridChatService(
        chat_service=rag, router=wiki_router, wiki_store=WikiStore(tmp_path / "wiki")
    )
    response = await service.respond("결정 내용", **filters)
    assert response.source_type == "rag"
    rag.respond.assert_awaited_once_with("결정 내용", **filters)
    wiki_router.classify.assert_not_called()
