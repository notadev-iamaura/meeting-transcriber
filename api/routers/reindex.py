"""RAG 검색 인덱스 백필 API 라우터."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.dependencies import get_config as _get_config
from api.dependencies import get_job_queue as _get_job_queue
from api.dependencies import get_pipeline_manager as _get_pipeline_manager

logger = logging.getLogger(__name__)

router = APIRouter()

_MEETING_ID_PATTERN = re.compile(r"^[\w\-\.]+$")


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """백그라운드 태스크의 미처리 예외를 로깅한다."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            f"백그라운드 태스크 실패: {task.get_name()}: {exc}",
            exc_info=exc,
        )


def _validate_meeting_id(meeting_id: str) -> None:
    """meeting_id 형식을 검증한다 (path traversal 방지)."""
    if not _MEETING_ID_PATTERN.match(meeting_id):
        raise HTTPException(
            status_code=400,
            detail=f"유효하지 않은 회의 ID 형식입니다: {meeting_id}",
        )


# ====================================================================
# RAG 검색 인덱스 백필 API
# ====================================================================
#
# 배경:
#   PIPELINE_STEPS 에 chunk/embed 단계가 추가되기 전에 완료된 회의들은
#   ChromaDB / FTS5 인덱스가 없어 /api/chat 이 컨텍스트 없이 답변한다.
#   이 API 들은 그런 기존 회의를 백필하기 위한 진입점.
#
# 정책:
#   - reindex-all 은 글로벌 락(asyncio.Lock + busy 플래그) 으로 단일 동시 실행 강제
#   - 회의별 처리는 순차 (ChromaDB / SQLite 동시 쓰기 충돌 회피, 메모리 보호)
#   - 진행 상황은 WebSocket reindex_progress 이벤트로 broadcast


class ReindexStatusResponse(BaseModel):
    """인덱싱 상태 조회 응답."""

    total: int = Field(description="completed 상태 회의 총 개수")
    indexed: int = Field(description="ChromaDB 에 청크가 1개 이상 있는 회의 수")
    missing: int = Field(description="청크가 없는 (백필 필요) 회의 수")
    missing_meeting_ids: list[str] = Field(
        default_factory=list,
        description="청크가 없는 회의 ID 목록 (UI 가 개별 백필 버튼으로 사용)",
    )


class ReindexResponse(BaseModel):
    """단일 회의 재색인 응답."""

    meeting_id: str
    chunks: int = Field(description="생성된 청크 수")
    chroma_stored: bool
    fts_stored: bool


class ReindexAllResponse(BaseModel):
    """일괄 백필 시작 응답."""

    status: str = Field(description="started 또는 running")
    total: int = Field(description="대상 회의 수")
    meeting_ids: list[str] = Field(default_factory=list)


def _get_chroma_collection_for_status(config: Any) -> Any:
    """ChromaDB 컬렉션을 조회용으로 가져온다 (status 집계 전용).

    HybridSearchEngine 의 컬렉션 캐시와 별도로 짧게 사용하고 닫는다
    (status API 가 빈번히 호출되어도 안전).

    Args:
        config: AppConfig

    Returns:
        ChromaDB 컬렉션 또는 None (디렉토리 없거나 컬렉션 없음)
    """
    chroma_dir = config.paths.resolved_chroma_db_dir
    if not chroma_dir.exists():
        return None
    try:
        import chromadb

        from steps.embedder import _CHROMA_COLLECTION_NAME

        client = chromadb.PersistentClient(path=str(chroma_dir))
        return client.get_collection(name=_CHROMA_COLLECTION_NAME)
    except Exception as e:
        logger.debug(f"ChromaDB 컬렉션 조회 실패 (status 집계, 무시): {e}")
        return None


def _count_chunks_for_meeting(collection: Any, meeting_id: str) -> int:
    """ChromaDB 컬렉션에서 특정 meeting_id 의 청크 수를 반환한다.

    Args:
        collection: ChromaDB 컬렉션 (None 이면 0)
        meeting_id: 회의 식별자

    Returns:
        청크 수 (0 이상)
    """
    if collection is None:
        return 0
    try:
        result = collection.get(where={"meeting_id": meeting_id})
        return len(result.get("ids", []))
    except Exception as e:
        logger.debug(f"청크 카운트 실패 (무시): meeting_id={meeting_id}, {e}")
        return 0


@router.get("/reindex/status", response_model=ReindexStatusResponse)
async def get_index_status(request: Request) -> ReindexStatusResponse:
    """모든 완료 회의의 RAG 인덱싱 상태를 집계한다.

    각 회의에 대해 ChromaDB 컬렉션의 청크 수를 조회하여
    인덱싱된/누락된 회의를 분류한다.

    Returns:
        ReindexStatusResponse: total/indexed/missing 카운트 + 누락 목록
    """
    queue = _get_job_queue(request)
    config = _get_config(request)

    try:
        all_jobs = await queue.get_all_jobs()
    except Exception as e:
        logger.exception(f"회의 목록 조회 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e

    completed_jobs = [j for j in all_jobs if getattr(j, "status", "") == "completed"]
    total = len(completed_jobs)

    if total == 0:
        return ReindexStatusResponse(total=0, indexed=0, missing=0, missing_meeting_ids=[])

    collection = _get_chroma_collection_for_status(config)

    indexed = 0
    missing_ids: list[str] = []
    for job in completed_jobs:
        mid = job.meeting_id
        chunk_count = _count_chunks_for_meeting(collection, mid)
        if chunk_count > 0:
            indexed += 1
        else:
            missing_ids.append(mid)

    return ReindexStatusResponse(
        total=total,
        indexed=indexed,
        missing=len(missing_ids),
        missing_meeting_ids=missing_ids,
    )


async def _reindex_meeting(
    config: Any,
    model_manager: Any,
    meeting_id: str,
) -> dict[str, Any]:
    """단일 회의의 chunk + embed 를 실행한다.

    correct.json 우선 → merge.json 폴백 (구버전 호환).
    파이프라인 chunk/embed 단계와 동일한 로직을 호출한다.

    Args:
        config: AppConfig
        model_manager: ModelLoadManager (e5 임베딩 모델 라이프사이클 관리)
        meeting_id: 회의 식별자

    Returns:
        {"chunks": N, "chroma_stored": bool, "fts_stored": bool}

    Raises:
        FileNotFoundError: correct.json / merge.json 둘 다 없을 때
    """
    from steps.chunker import Chunker
    from steps.corrector import CorrectedResult
    from steps.embedder import Embedder
    from steps.merger import MergedResult

    checkpoints_dir = config.paths.resolved_checkpoints_dir
    correct_cp = checkpoints_dir / meeting_id / "correct.json"
    merge_cp = checkpoints_dir / meeting_id / "merge.json"

    # 입력: correct.json 우선, 없으면 merge.json (LLM 보정 없는 raw 발화)
    if correct_cp.exists():
        corrected = CorrectedResult.from_checkpoint(correct_cp)
    elif merge_cp.exists():
        merged = MergedResult.from_checkpoint(merge_cp)
        # MergedResult → CorrectedResult 변환 (was_corrected=False 로 패스스루)
        from steps.corrector import CorrectedUtterance

        utterances = [
            CorrectedUtterance(
                text=u.text,
                original_text=u.text,
                speaker=u.speaker,
                start=u.start,
                end=u.end,
                was_corrected=False,
            )
            for u in merged.utterances
        ]
        corrected = CorrectedResult(
            utterances=utterances,
            audio_path=getattr(merged, "audio_path", ""),
            num_speakers=getattr(merged, "num_speakers", 0),
            speakers=list(getattr(merged, "speakers", []) or []),
            total_corrected=0,
        )
    else:
        raise FileNotFoundError(f"correct.json / merge.json 체크포인트가 없습니다: {meeting_id}")

    # 회의 날짜 도출 (meeting_id 패턴 → mtime → 오늘)
    import re
    from datetime import datetime

    match = re.search(r"(\d{4})(\d{2})(\d{2})_\d{6}", meeting_id)
    if match:
        date_str = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")

    # 1) Chunker
    chunker = Chunker(config)
    chunked = await chunker.chunk(corrected, meeting_id, date_str)

    # 청크 체크포인트 저장 (재개 가능성)
    chunk_cp = checkpoints_dir / meeting_id / "chunk.json"
    chunk_cp.parent.mkdir(parents=True, exist_ok=True)
    chunked.save_checkpoint(chunk_cp)

    # 2) Embedder (fail-loud — ChromaDB / FTS5 둘 다 성공해야 함)
    embedder = Embedder(config, model_manager)
    embedded = await embedder.embed(chunked)

    embed_cp = checkpoints_dir / meeting_id / "embed.json"
    embedded.save_checkpoint(embed_cp)

    return {
        "chunks": embedded.total_chunks,
        "chroma_stored": embedded.chroma_stored,
        "fts_stored": embedded.fts_stored,
    }


@router.post("/meetings/{meeting_id}/reindex", response_model=ReindexResponse)
async def reindex_meeting(request: Request, meeting_id: str) -> ReindexResponse:
    """단일 회의의 RAG 인덱스를 재생성한다 (백필).

    correct.json 또는 merge.json 체크포인트에서 chunk → embed 를 실행한다.
    오디오 재처리 없이 LLM/STT 결과를 재사용하므로 빠르게 복구 가능.

    Returns:
        ReindexResponse

    Raises:
        HTTPException: 404 (회의 없음), 422 (체크포인트 없음), 500 (실행 실패)
    """
    _validate_meeting_id(meeting_id)
    queue = _get_job_queue(request)
    config = _get_config(request)

    job = await asyncio.to_thread(
        queue.queue.get_job_by_meeting_id,
        meeting_id,
    )
    if job is None:
        raise HTTPException(status_code=404, detail=f"회의를 찾을 수 없습니다: {meeting_id}")

    # 체크포인트 존재 여부 사전 점검
    checkpoints_dir = config.paths.resolved_checkpoints_dir
    correct_cp = checkpoints_dir / meeting_id / "correct.json"
    merge_cp = checkpoints_dir / meeting_id / "merge.json"
    if not correct_cp.exists() and not merge_cp.exists():
        raise HTTPException(
            status_code=422,
            detail=(
                f"체크포인트가 없어 재색인할 수 없습니다: {meeting_id} "
                "(correct.json/merge.json 모두 부재)"
            ),
        )

    pipeline = _get_pipeline_manager(request)
    model_manager = pipeline._model_manager

    ws_manager = getattr(request.app.state, "ws_manager", None)

    # WebSocket: 시작 이벤트
    if ws_manager is not None:
        try:
            from api.websocket import EventType, WebSocketEvent

            await ws_manager.broadcast_event(
                WebSocketEvent(
                    type=EventType.REINDEX_PROGRESS,
                    data={"meeting_id": meeting_id, "phase": "start"},
                )
            )
        except Exception as e:
            logger.debug(f"reindex 시작 이벤트 broadcast 실패 (무시): {e}")

    try:
        result = await _reindex_meeting(config, model_manager, meeting_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"재색인 실패: meeting_id={meeting_id}")
        if ws_manager is not None:
            try:
                from api.websocket import EventType, WebSocketEvent

                await ws_manager.broadcast_event(
                    WebSocketEvent(
                        type=EventType.REINDEX_PROGRESS,
                        data={
                            "meeting_id": meeting_id,
                            "phase": "failed",
                            "error_message": str(e),
                        },
                    )
                )
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(e)) from e

    # WebSocket: 완료 이벤트
    if ws_manager is not None:
        try:
            from api.websocket import EventType, WebSocketEvent

            await ws_manager.broadcast_event(
                WebSocketEvent(
                    type=EventType.REINDEX_PROGRESS,
                    data={
                        "meeting_id": meeting_id,
                        "phase": "complete",
                        "chunks": result["chunks"],
                    },
                )
            )
        except Exception as e:
            logger.debug(f"reindex 완료 이벤트 broadcast 실패 (무시): {e}")

    return ReindexResponse(
        meeting_id=meeting_id,
        chunks=result["chunks"],
        chroma_stored=result["chroma_stored"],
        fts_stored=result["fts_stored"],
    )


async def _start_reindex_all(app: Any, missing_ids: list[str]) -> None:
    """일괄 백필 백그라운드 작업을 시작한다 (글로벌 락 보유 가정).

    호출자가 app.state.reindex_lock_busy = True 를 설정한 뒤 호출해야 한다.
    이 함수는 작업이 끝날 때 busy 플래그를 해제한다.

    Args:
        app: FastAPI app (state.config / model_manager 사용)
        missing_ids: 백필 대상 meeting_id 목록
    """
    config = app.state.config
    pipeline = getattr(app.state, "pipeline_manager", None)
    if pipeline is None:
        logger.error("reindex-all: PipelineManager 미초기화 — 작업 중단")
        app.state.reindex_lock_busy = False
        return
    model_manager = pipeline._model_manager
    ws_manager = getattr(app.state, "ws_manager", None)

    async def _broadcast(data: dict) -> None:
        if ws_manager is None:
            return
        try:
            from api.websocket import EventType, WebSocketEvent

            await ws_manager.broadcast_event(
                WebSocketEvent(type=EventType.REINDEX_PROGRESS, data=data),
            )
        except Exception as e:
            logger.debug(f"reindex broadcast 실패 (무시): {e}")

    total = len(missing_ids)
    await _broadcast({"phase": "all_started", "total": total, "processed": 0})

    processed = 0
    failed: list[str] = []
    try:
        for mid in missing_ids:
            try:
                await _broadcast(
                    {"phase": "start", "meeting_id": mid, "processed": processed, "total": total}
                )
                result = await _reindex_meeting(config, model_manager, mid)
                processed += 1
                await _broadcast(
                    {
                        "phase": "complete",
                        "meeting_id": mid,
                        "chunks": result["chunks"],
                        "processed": processed,
                        "total": total,
                    }
                )
            except Exception as e:
                logger.exception(f"reindex-all 개별 회의 실패: {mid}")
                failed.append(mid)
                await _broadcast(
                    {
                        "phase": "failed",
                        "meeting_id": mid,
                        "error_message": str(e),
                        "processed": processed,
                        "total": total,
                    }
                )
        await _broadcast(
            {
                "phase": "all_complete",
                "total": total,
                "processed": processed,
                "failed_meeting_ids": failed,
            }
        )
    finally:
        app.state.reindex_lock_busy = False
        logger.info(f"reindex-all 종료: 전체 {total}, 성공 {processed}, 실패 {len(failed)}")


@router.post("/reindex/all", response_model=ReindexAllResponse, status_code=202)
async def reindex_all(request: Request) -> ReindexAllResponse:
    """청크 누락 회의 전체를 백그라운드로 백필한다.

    글로벌 단일 동시 실행 강제 (app.state.reindex_lock_busy 플래그).
    이미 진행 중이면 409 반환.

    Returns:
        ReindexAllResponse (status="started", total, meeting_ids)
    """
    app = request.app

    # 동시성 가드
    if getattr(app.state, "reindex_lock_busy", False):
        raise HTTPException(
            status_code=409,
            detail="이미 진행 중인 일괄 백필 작업이 있습니다.",
        )

    queue = _get_job_queue(request)
    config = _get_config(request)

    try:
        all_jobs = await queue.get_all_jobs()
    except Exception as e:
        logger.exception(f"reindex-all: 회의 목록 조회 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e

    completed_jobs = [j for j in all_jobs if getattr(j, "status", "") == "completed"]
    collection = _get_chroma_collection_for_status(config)

    missing_ids: list[str] = []
    for job in completed_jobs:
        mid = job.meeting_id
        # correct/merge 체크포인트가 있어야 백필 가능
        cp_dir = config.paths.resolved_checkpoints_dir / mid
        if not (cp_dir / "correct.json").exists() and not (cp_dir / "merge.json").exists():
            continue
        if _count_chunks_for_meeting(collection, mid) == 0:
            missing_ids.append(mid)

    # 락 설정 + 백그라운드 작업 시작
    app.state.reindex_lock_busy = True
    task = asyncio.create_task(
        _start_reindex_all(app, missing_ids),
        name=f"reindex_all_{len(missing_ids)}",
    )
    task.add_done_callback(_log_task_exception)

    return ReindexAllResponse(
        status="started",
        total=len(missing_ids),
        meeting_ids=missing_ids,
    )
