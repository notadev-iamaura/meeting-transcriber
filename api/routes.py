"""
API 라우터 모듈 (API Router Module)

목적: FastAPI 라우터로 REST API 엔드포인트를 정의한다.
주요 기능:
    - /api/status: 시스템 상태 및 작업 큐 현황 조회
    - /api/meetings: 전체 회의 목록 조회
    - /api/meetings/{meeting_id}: 특정 회의 상세 조회
    - /api/search: 하이브리드 검색 (벡터 + FTS5)
    - /api/chat: RAG 기반 AI Chat
    - /api/settings: 시스템 설정 조회/수정 (GET/PUT)
    - pydantic 요청/응답 스키마 정의
의존성: fastapi, pydantic, pyyaml, search/hybrid_search, search/chat, core/job_queue
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.dependencies import (
    get_job_queue as _get_job_queue,
)
from api.dependencies import (
    get_pipeline_manager as _get_pipeline_manager,
)

logger = logging.getLogger(__name__)


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """백그라운드 태스크의 미처리 예외를 로깅한다.

    asyncio.Task.add_done_callback()에 등록하여 사용한다.
    태스크가 예외로 종료된 경우 logger.error로 기록하고,
    CancelledError는 정상 취소이므로 무시한다.

    Args:
        task: 완료된 asyncio.Task 객체
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            f"백그라운드 태스크 실패: {task.get_name()}: {exc}",
            exc_info=exc,
        )


# === API 라우터 ===

router = APIRouter(prefix="/api", tags=["api"])


# === 요청/응답 Pydantic 스키마 ===


# 시스템/대시보드 API 는 api.routers.system 으로 분리한다.
# 아래 re-export 는 기존 `api.routes.StatusResponse` 같은 접근을 보존한다.
from api.routers import system as _system_router  # noqa: E402

DashboardStatsResponse = _system_router.DashboardStatsResponse
OpenFolderResponse = _system_router.OpenFolderResponse
StatusResponse = _system_router.StatusResponse
SystemResourcesResponse = _system_router.SystemResourcesResponse
_ACTIVE_JOB_STATUSES = _system_router._ACTIVE_JOB_STATUSES
_PENDING_JOB_STATUSES = _system_router._PENDING_JOB_STATUSES
_UNTRANSCRIBED_JOB_STATUSES = _system_router._UNTRANSCRIBED_JOB_STATUSES
shutil = _system_router.shutil
subprocess = _system_router.subprocess
sys = _system_router.sys
get_dashboard_stats = _system_router.get_dashboard_stats
get_status = _system_router.get_status
get_system_resources = _system_router.get_system_resources
open_audio_folder = _system_router.open_audio_folder

router.include_router(_system_router.router)


# 업로드 API 는 api.routers.uploads 로 분리한다.
# 아래 re-export 는 기존 업로드 helper import/patch 경로를 보존한다.
from api.routers import uploads as _uploads_router  # noqa: E402

UploadResponse = _uploads_router.UploadResponse
_FILENAME_FORBIDDEN_PATTERN = _uploads_router._FILENAME_FORBIDDEN_PATTERN
_UPLOAD_MAX_BYTES = _uploads_router._UPLOAD_MAX_BYTES
_resolve_unique_upload_path = _uploads_router._resolve_unique_upload_path
_sanitize_upload_filename = _uploads_router._sanitize_upload_filename
upload_audio = _uploads_router.upload_audio

router.include_router(_uploads_router.router)


# 녹음 API 는 api.routers.recording 으로 분리한다.
# 아래 re-export 는 기존 `api.routes.RecordingStatusResponse` 같은 접근을 보존한다.
from api.routers import recording as _recording_router  # noqa: E402

AudioDeviceItem = _recording_router.AudioDeviceItem
RecordingStartRequest = _recording_router.RecordingStartRequest
RecordingStatusResponse = _recording_router.RecordingStatusResponse
get_recording_devices = _recording_router.get_recording_devices
get_recording_status = _recording_router.get_recording_status
start_recording = _recording_router.start_recording
stop_recording = _recording_router.stop_recording

router.include_router(_recording_router.router)


# 회의 상세 API 는 api.routers.meeting_detail 로 분리한다.
# 아래 re-export 는 기존 `api.routes.MeetingItem`/`TranscriptResponse` 같은 접근을 보존한다.
from api.routers import meeting_detail as _meeting_detail_router  # noqa: E402

MeetingItem = _meeting_detail_router.MeetingItem
TranscriptReplaceRequest = _meeting_detail_router.TranscriptReplaceRequest
TranscriptReplaceResponse = _meeting_detail_router.TranscriptReplaceResponse
TranscriptResponse = _meeting_detail_router.TranscriptResponse
TranscriptUpdateRequest = _meeting_detail_router.TranscriptUpdateRequest
TranscriptUtteranceItem = _meeting_detail_router.TranscriptUtteranceItem
TranscriptUtterancePatch = _meeting_detail_router.TranscriptUtterancePatch
SummaryResponse = _meeting_detail_router.SummaryResponse
SummaryUpdateRequest = _meeting_detail_router.SummaryUpdateRequest
MeetingPatchRequest = _meeting_detail_router.MeetingPatchRequest
_AUDIO_MIME_BY_EXT = _meeting_detail_router._AUDIO_MIME_BY_EXT
_PLAYABLE_AUDIO_EXTS = _meeting_detail_router._PLAYABLE_AUDIO_EXTS
_atomic_write_json = _meeting_detail_router._atomic_write_json
_atomic_write_text = _meeting_detail_router._atomic_write_text
_find_meeting_audio_path = _meeting_detail_router._find_meeting_audio_path
_find_transcript_file = _meeting_detail_router._find_transcript_file
_parse_range_header = _meeting_detail_router._parse_range_header
cancel_meeting = _meeting_detail_router.cancel_meeting
delete_meeting = _meeting_detail_router.delete_meeting
get_meeting = _meeting_detail_router.get_meeting
get_meeting_audio = _meeting_detail_router.get_meeting_audio
get_pipeline_state = _meeting_detail_router.get_pipeline_state
get_summary = _meeting_detail_router.get_summary
get_transcript = _meeting_detail_router.get_transcript
patch_meeting = _meeting_detail_router.patch_meeting
re_transcribe_meeting = _meeting_detail_router.re_transcribe_meeting
replace_transcript_pattern = _meeting_detail_router.replace_transcript_pattern
retry_meeting = _meeting_detail_router.retry_meeting
summarize_meeting = _meeting_detail_router.summarize_meeting
transcribe_meeting = _meeting_detail_router.transcribe_meeting
update_summary = _meeting_detail_router.update_summary
update_transcript = _meeting_detail_router.update_transcript

router.include_router(_meeting_detail_router.router)


class MeetingsResponse(BaseModel):
    """회의 목록 응답 스키마.

    Attributes:
        meetings: 회의 목록
        total: 전체 회의 수
    """

    meetings: list[MeetingItem] = Field(default_factory=list)
    total: int = 0


# === 헬퍼 함수 ===


# meeting_id 유효성 검증 정규식 (path traversal 방지)
_MEETING_ID_PATTERN = re.compile(r"^[\w\-\.]+$")


def _validate_meeting_id(meeting_id: str) -> None:
    """meeting_id 형식을 검증한다 (path traversal 방지).

    Args:
        meeting_id: 검증할 회의 ID

    Raises:
        HTTPException: 유효하지 않은 형식일 때 (400)
    """
    if not _MEETING_ID_PATTERN.match(meeting_id):
        raise HTTPException(
            status_code=400,
            detail=f"유효하지 않은 회의 ID 형식입니다: {meeting_id}",
        )


# === 엔드포인트 ===


@router.get("/meetings", response_model=MeetingsResponse)
async def get_meetings(
    request: Request,
    offset: int = 0,
    limit: int = 50,
) -> MeetingsResponse:
    """회의 목록을 반환한다.

    PERF: 페이지네이션을 지원하여 대량 데이터 시 응답 속도를 개선한다.
    최신순으로 정렬된 회의(작업) 목록을 offset/limit으로 페이징한다.

    Args:
        request: FastAPI Request 객체
        offset: 건너뛸 항목 수 (기본 0)
        limit: 반환할 최대 항목 수 (기본 50, 최대 200)

    Returns:
        MeetingsResponse: 회의 목록 (페이징 적용)
    """
    queue = _get_job_queue(request)

    # limit 상한 제한
    limit = min(limit, 200)

    try:
        all_jobs = await queue.get_all_jobs()
        total = len(all_jobs)

        # PERF: 메모리에서 슬라이싱으로 페이지네이션 적용
        # (SQLite 쿼리에 LIMIT/OFFSET 추가 시 JobQueue 인터페이스 변경 필요)
        paged_jobs = all_jobs[offset : offset + limit]

        meetings = [
            MeetingItem(
                id=job.id,
                meeting_id=job.meeting_id,
                audio_path=job.audio_path,
                status=job.status,
                retry_count=job.retry_count,
                error_message=job.error_message,
                created_at=job.created_at,
                updated_at=job.updated_at,
                title=getattr(job, "title", "") or "",
            )
            for job in paged_jobs
        ]

        return MeetingsResponse(
            meetings=meetings,
            total=total,
        )
    except Exception as e:
        logger.exception(f"회의 목록 조회 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"회의 목록 조회 중 오류가 발생했습니다: {e}",
        ) from e


# 검색/채팅 API 는 api.routers.search_chat 으로 분리한다.
# 아래 re-export 는 기존 `api.routes.SearchResponse`/`ChatResponse` 같은 접근을 보존한다.
from api.routers import search_chat as _search_chat_router  # noqa: E402

ChatReferenceItem = _search_chat_router.ChatReferenceItem
ChatRequest = _search_chat_router.ChatRequest
ChatResponse = _search_chat_router.ChatResponse
SearchRequest = _search_chat_router.SearchRequest
SearchResponse = _search_chat_router.SearchResponse
SearchResultItem = _search_chat_router.SearchResultItem
_ChatEngineAdapter = _search_chat_router._ChatEngineAdapter
_build_chat_references = _search_chat_router._build_chat_references
_build_hybrid_chat_service = _search_chat_router._build_hybrid_chat_service
_serialize_router_verdict = _search_chat_router._serialize_router_verdict
_serialize_wiki_sources = _search_chat_router._serialize_wiki_sources
chat = _search_chat_router.chat
search = _search_chat_router.search

router.include_router(_search_chat_router.router)


class SummarizeBatchRequest(BaseModel):
    """일괄 요약 요청 모델."""

    meeting_ids: list[str] = Field(
        default_factory=list,
        description="요약할 회의 ID 목록. 빈 리스트이면 요약이 없는 전체 회의 대상.",
    )


@router.post("/meetings/summarize-batch")
async def summarize_batch(
    request: Request,
    body: SummarizeBatchRequest | None = None,
) -> dict[str, Any]:
    """일괄 요약 생성: 여러 회의의 LLM 후처리를 순차 실행한다.

    meeting_ids를 지정하면 해당 회의만, 빈 리스트이면
    merge 체크포인트가 있고 summary가 없는 모든 회의를 대상으로 한다.
    메모리 부족 방지를 위해 백그라운드에서 순차(하나씩) 실행된다.

    Args:
        request: FastAPI Request 객체
        body: 요약할 회의 ID 목록 (선택)

    Returns:
        요약 시작 확인 메시지 및 대상 회의 목록
    """

    pipeline = _get_pipeline_manager(request)
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")

    checkpoints_dir = config.paths.resolved_checkpoints_dir
    outputs_dir = config.paths.resolved_outputs_dir

    meeting_ids = body.meeting_ids if body and body.meeting_ids else []

    if not meeting_ids:
        # merge 체크포인트가 있고 summary가 없는 회의 자동 탐색
        for cp_dir in sorted(checkpoints_dir.iterdir()):
            if not cp_dir.is_dir():
                continue
            mid = cp_dir.name
            merge_cp = cp_dir / "merge.json"
            summary_md = outputs_dir / mid / "summary.md"
            if merge_cp.is_file() and not summary_md.is_file():
                meeting_ids.append(mid)

    if not meeting_ids:
        return {
            "status": "ok",
            "message": "요약 대상 회의가 없습니다.",
            "meeting_ids": [],
            "total": 0,
        }

    # 유효성 검증: merge 체크포인트 존재 여부
    valid_ids: list[str] = []
    for mid in meeting_ids:
        _validate_meeting_id(mid)
        merge_cp = checkpoints_dir / mid / "merge.json"
        if merge_cp.is_file():
            valid_ids.append(mid)
        else:
            logger.warning(f"일괄 요약 건너뜀: merge 체크포인트 없음 ({mid})")

    if not valid_ids:
        return {
            "status": "ok",
            "message": "유효한 요약 대상이 없습니다.",
            "meeting_ids": [],
            "total": 0,
        }

    async def _run_batch(ids: list[str]) -> None:
        """백그라운드에서 순차적으로 LLM 단계를 실행한다."""
        for mid in ids:
            try:
                logger.info(f"일괄 요약 실행: {mid}")
                await pipeline.run_llm_steps(mid)
                logger.info(f"일괄 요약 완료: {mid}")
            except Exception:
                logger.exception(f"일괄 요약 실패: {mid}")

    task = asyncio.create_task(
        _run_batch(valid_ids),
        name="summarize-batch",
    )
    task.add_done_callback(_log_task_exception)
    running_tasks = getattr(request.app.state, "running_tasks", None)
    if running_tasks is not None:
        running_tasks.add(task)
        task.add_done_callback(running_tasks.discard)

    logger.info(f"일괄 요약 시작: {len(valid_ids)}건")

    return {
        "status": "ok",
        "message": f"일괄 요약 생성을 시작합니다 ({len(valid_ids)}건).",
        "meeting_ids": valid_ids,
        "total": len(valid_ids),
    }


# === 통합 일괄 처리 (Bulk Actions) ===
#
# POST /api/meetings/batch 는 api.routers.meetings_batch 로 분리한다.
# 아래 import 는 기존 `api.routes.BatchActionResponse` 같은 접근을 보존한다.

from api.routers import meetings_batch as _meetings_batch  # noqa: E402

BatchActionRequest = _meetings_batch.BatchActionRequest
BatchActionResponse = _meetings_batch.BatchActionResponse
_classify_eligibility_sync = _meetings_batch._classify_eligibility_sync
_classify_meeting_for_batch = _meetings_batch._classify_meeting_for_batch
_collect_candidate_ids_sync = _meetings_batch._collect_candidate_ids_sync
_has_merge_checkpoint = _meetings_batch._has_merge_checkpoint
_has_summary_output = _meetings_batch._has_summary_output
_is_meeting_eligible = _meetings_batch._is_meeting_eligible
_resolve_audio_path = _meetings_batch._resolve_audio_path
batch_action = _meetings_batch.batch_action

router.include_router(_meetings_batch.router)


# === 설정 관리 API ===

# 시스템 설정 API 는 api.routers.settings 로 분리한다.
# 아래 re-export 는 기존 `api.routes.SettingsResponse` 같은 접근을 보존한다.
from api.routers import settings as _settings_router  # noqa: E402

SettingsResponse = _settings_router.SettingsResponse
SettingsUpdateRequest = _settings_router.SettingsUpdateRequest
SettingsUpdateResponse = _settings_router.SettingsUpdateResponse
_ALLOWED_MLX_MODELS = _settings_router._ALLOWED_MLX_MODELS
_STT_LANGUAGE_PATTERN = _settings_router._STT_LANGUAGE_PATTERN
_AVAILABLE_MODELS = _settings_router._AVAILABLE_MODELS
_get_config_path = _settings_router._get_config_path
_replace_yaml_value = _settings_router._replace_yaml_value
get_settings = _settings_router.get_settings
update_settings = _settings_router.update_settings

router.include_router(_settings_router.router)


# 사용자 편집 가능 프롬프트/용어집 API 는 api.routers.user_settings 로 분리한다.
# 아래 re-export 는 기존 `api.routes.PromptsResponse` 같은 접근을 보존한다.
from api.routers import user_settings as _user_settings_router  # noqa: E402

PromptEntryPayload = _user_settings_router.PromptEntryPayload
PromptsPayload = _user_settings_router.PromptsPayload
PromptsResponse = _user_settings_router.PromptsResponse
PromptsUpdateRequest = _user_settings_router.PromptsUpdateRequest
VocabularyAddRequest = _user_settings_router.VocabularyAddRequest
VocabularyResponse = _user_settings_router.VocabularyResponse
VocabularyTermPayload = _user_settings_router.VocabularyTermPayload
VocabularyUpdateRequest = _user_settings_router.VocabularyUpdateRequest
_map_user_settings_error = _user_settings_router._map_user_settings_error
_prompts_to_payload = _user_settings_router._prompts_to_payload
_term_to_payload = _user_settings_router._term_to_payload
_user_settings = _user_settings_router._user_settings
add_vocabulary_term_endpoint = _user_settings_router.add_vocabulary_term_endpoint
delete_vocabulary_term_endpoint = _user_settings_router.delete_vocabulary_term_endpoint
get_prompts = _user_settings_router.get_prompts
get_vocabulary = _user_settings_router.get_vocabulary
reset_prompts = _user_settings_router.reset_prompts
reset_vocabulary_endpoint = _user_settings_router.reset_vocabulary_endpoint
update_prompts = _user_settings_router.update_prompts
update_vocabulary_term_endpoint = _user_settings_router.update_vocabulary_term_endpoint

router.include_router(_user_settings_router.router)


# ============================================================
# STT 모델 선택기 API (Phase 4)는 api.routers.stt_models 로 분리한다.
# 아래 re-export 는 기존 `api.routes.STTModelInfo` 같은 테스트/외부 접근을 보존한다.
from api.routers import stt_models as _stt_models  # noqa: E402

STTImportRequest = _stt_models.STTImportRequest
STTImportResponse = _stt_models.STTImportResponse
STTManualDownloadFile = _stt_models.STTManualDownloadFile
STTManualDownloadInfo = _stt_models.STTManualDownloadInfo
STTModelInfo = _stt_models.STTModelInfo
STTModelsResponse = _stt_models.STTModelsResponse
_is_active_stt_model = _stt_models._is_active_stt_model
activate_stt_model = _stt_models.activate_stt_model
download_stt_model = _stt_models.download_stt_model
download_stt_model_direct = _stt_models.download_stt_model_direct
get_stt_download_status = _stt_models.get_stt_download_status
get_stt_manual_download_info = _stt_models.get_stt_manual_download_info
import_stt_manual = _stt_models.import_stt_manual
list_stt_models = _stt_models.list_stt_models

router.include_router(_stt_models.router)


# A/B 테스트 API 는 api.routers.ab_tests 로 분리한다.
# 아래 re-export 는 기존 `api.routes.ABTestLLMRequest` 같은 접근을 보존한다.
from api.routers import ab_tests as _ab_tests_router  # noqa: E402

ABTestLLMRequest = _ab_tests_router.ABTestLLMRequest
ABTestSTTRequest = _ab_tests_router.ABTestSTTRequest
ABTestStartedResponse = _ab_tests_router.ABTestStartedResponse
LlmScopePayload = _ab_tests_router.LlmScopePayload
ModelSpecPayload = _ab_tests_router.ModelSpecPayload
_LLM_PRESETS = _ab_tests_router._LLM_PRESETS
_check_hf_cache_exists = _ab_tests_router._check_hf_cache_exists
_make_ab_broadcaster = _ab_tests_router._make_ab_broadcaster
_validate_test_id = _ab_tests_router._validate_test_id
_validate_variant = _ab_tests_router._validate_variant
cancel_ab_test = _ab_tests_router.cancel_ab_test
delete_ab_test = _ab_tests_router.delete_ab_test
get_ab_test = _ab_tests_router.get_ab_test
get_ab_test_summary = _ab_tests_router.get_ab_test_summary
list_ab_tests = _ab_tests_router.list_ab_tests
list_available_llm_models = _ab_tests_router.list_available_llm_models
start_llm_ab_test = _ab_tests_router.start_llm_ab_test
start_stt_ab_test = _ab_tests_router.start_stt_ab_test

router.include_router(_ab_tests_router.router)


# LLM Wiki API 는 api.routers.wiki 로 분리한다.
# 아래 re-export 는 기존 `api.routes.WikiPagesResponse` 같은 접근을 보존한다.
from api.routers import wiki as _wiki_router  # noqa: E402

WikiBackfillErrorItem = _wiki_router.WikiBackfillErrorItem
WikiBackfillRequest = _wiki_router.WikiBackfillRequest
WikiBackfillStartedResponse = _wiki_router.WikiBackfillStartedResponse
WikiBackfillStatusResponse = _wiki_router.WikiBackfillStatusResponse
WikiCitationItem = _wiki_router.WikiCitationItem
WikiHealthResponse = _wiki_router.WikiHealthResponse
WikiPageDetail = _wiki_router.WikiPageDetail
WikiPageItem = _wiki_router.WikiPageItem
WikiPagesResponse = _wiki_router.WikiPagesResponse
WikiSearchResponse = _wiki_router.WikiSearchResponse
WikiSearchResult = _wiki_router.WikiSearchResult
_WIKI_PAGE_TYPE_TO_DIRNAME = _wiki_router._WIKI_PAGE_TYPE_TO_DIRNAME
_WIKI_SEARCH_DEFAULT_LIMIT = _wiki_router._WIKI_SEARCH_DEFAULT_LIMIT
_WIKI_SEARCH_MAX_LIMIT = _wiki_router._WIKI_SEARCH_MAX_LIMIT
_WIKI_SEARCH_SNIPPET_AFTER = _wiki_router._WIKI_SEARCH_SNIPPET_AFTER
_WIKI_SEARCH_SNIPPET_BEFORE = _wiki_router._WIKI_SEARCH_SNIPPET_BEFORE
_extract_title_from_markdown = _wiki_router._extract_title_from_markdown
_get_raw_job_queue = _wiki_router._get_raw_job_queue
_make_search_snippet = _wiki_router._make_search_snippet
_resolve_wiki_root = _wiki_router._resolve_wiki_root
_wiki_backfill_jobs = _wiki_router._wiki_backfill_jobs
_wiki_backfill_lock = _wiki_router._wiki_backfill_lock
cancel_wiki_backfill = _wiki_router.cancel_wiki_backfill
get_wiki_backfill_status = _wiki_router.get_wiki_backfill_status
get_wiki_health = _wiki_router.get_wiki_health
get_wiki_page_detail = _wiki_router.get_wiki_page_detail
list_wiki_pages = _wiki_router.list_wiki_pages
search_wiki = _wiki_router.search_wiki
start_wiki_backfill = _wiki_router.start_wiki_backfill

router.include_router(_wiki_router.router)


# RAG 검색 인덱스 백필 API 는 api.routers.reindex 로 분리한다.
# 아래 re-export 는 기존 `api.routes.ReindexResponse` 같은 접근을 보존한다.
from api.routers import reindex as _reindex_router  # noqa: E402

ReindexAllResponse = _reindex_router.ReindexAllResponse
ReindexResponse = _reindex_router.ReindexResponse
ReindexStatusResponse = _reindex_router.ReindexStatusResponse
_count_chunks_for_meeting = _reindex_router._count_chunks_for_meeting
_get_chroma_collection_for_status = _reindex_router._get_chroma_collection_for_status
_reindex_meeting = _reindex_router._reindex_meeting
_start_reindex_all = _reindex_router._start_reindex_all
get_index_status = _reindex_router.get_index_status
reindex_all = _reindex_router.reindex_all
reindex_meeting = _reindex_router.reindex_meeting

router.include_router(_reindex_router.router)
