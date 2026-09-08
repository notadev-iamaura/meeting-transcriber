"""
회의 일괄 처리 API 라우터.

목적: `POST /api/meetings/batch` 엔드포인트와 해당 엔드포인트 전용
스키마·헬퍼를 api.routes 모놀리스에서 분리한다.
"""

from __future__ import annotations

import asyncio
import logging
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.dependencies import (
    get_job_queue as _get_job_queue,
)
from api.dependencies import (
    get_pipeline_manager as _get_pipeline_manager,
)
from core.audio_quality import (
    AudioFailureKind,
    AudioQualityStatus,
    validate_audio_quality,
)
from core.job_queue import JobQueueError, lexical_root_no_symlinks
from steps.transcriber import (
    AudioAdmissionError,
    EmptyAudioError,
    inspect_audio_path_no_symlinks,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_IN_PROGRESS_STATUSES: frozenset[str] = frozenset(
    {
        "queued",
        "recording",
        "transcribing",
        "diarizing",
        "merging",
        "embedding",
    }
)
_AUDIO_FAILURE_HTTP_STATUS = {
    AudioFailureKind.MEDIA_INVALID: 422,
    AudioFailureKind.SOURCE_BUSY: 409,
    AudioFailureKind.INFRA_UNAVAILABLE: 503,
    AudioFailureKind.SECURITY_BLOCKED: 400,
}


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """백그라운드 태스크의 미처리 예외를 로깅한다.

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


def _validate_meeting_id(meeting_id: str) -> None:
    """meeting_id 형식을 검증한다 (path traversal 방지).

    Args:
        meeting_id: 검증할 회의 ID

    Raises:
        HTTPException: 유효하지 않은 형식일 때 (400)
    """
    if (
        not meeting_id
        or meeting_id in {".", ".."}
        or "/" in meeting_id
        or "\\" in meeting_id
        or "\x00" in meeting_id
    ):
        raise HTTPException(
            status_code=400,
            detail=f"유효하지 않은 회의 ID 형식입니다: {meeting_id}",
        )


def _get_sync_job_queue(queue: Any) -> Any | None:
    """AsyncJobQueue 또는 테스트 double 에서 동기 JobQueue 핸들을 반환한다."""
    return getattr(queue, "_queue", None) or getattr(queue, "queue", None)


def _configured_lexical_path(config: Any, child_attribute: str | None = None) -> Path:
    """raw base/child 설정을 resolve하지 않고 no-follow 검증한다."""
    try:
        base = lexical_root_no_symlinks(Path(config.paths.base_dir))
        if child_attribute is None:
            return base
        configured_child = Path(str(getattr(config.paths, child_attribute))).expanduser()
        raw_path = configured_child if configured_child.is_absolute() else base / configured_child
        child = lexical_root_no_symlinks(raw_path)
    except JobQueueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"SECURITY_BLOCKED: 안전하지 않은 설정 경로입니다: {exc}",
        ) from exc
    if not child.is_relative_to(base):
        raise HTTPException(
            status_code=400,
            detail="SECURITY_BLOCKED: 설정 경로가 base_dir 밖에 있습니다.",
        )
    return child


def _configured_lexical_base(config: Any) -> Path:
    """raw base_dir를 no-follow 검증한다."""
    return _configured_lexical_path(config)


def _batch_artifact_path(root: Path, meeting_id: str, filename: str) -> Path:
    """batch 분류 artifact의 intermediate/final symlink를 거부한다."""
    candidate = root / meeting_id / filename
    current = root
    for index, component in enumerate((meeting_id, filename)):
        current /= component
        try:
            entry_stat = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"INFRA_UNAVAILABLE: batch 산출물 상태 확인 실패: {exc}",
            ) from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            raise HTTPException(
                status_code=400,
                detail=f"SECURITY_BLOCKED: batch 산출물 symlink는 허용되지 않습니다: {current}",
            )
        if index == 0 and not stat.S_ISDIR(entry_stat.st_mode):
            raise HTTPException(
                status_code=400,
                detail=f"SECURITY_BLOCKED: 회의 산출물 경로가 디렉터리가 아닙니다: {current}",
            )
    return candidate


def _is_regular_batch_artifact(root: Path, meeting_id: str, filename: str) -> bool:
    """artifact가 no-follow 일반 파일인지 반환한다."""
    path = _batch_artifact_path(root, meeting_id, filename)
    try:
        entry_stat = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"INFRA_UNAVAILABLE: batch 산출물 상태 확인 실패: {exc}",
        ) from exc
    if not stat.S_ISREG(entry_stat.st_mode):
        raise HTTPException(
            status_code=400,
            detail=f"SECURITY_BLOCKED: batch 산출물이 일반 파일이 아닙니다: {path}",
        )
    return True


def _require_audio_in_base(config: Any, audio_path: Path) -> Path:
    """audio_path를 lexical base 내부로 제한한다."""
    raw = audio_path.expanduser()
    if "\x00" in str(raw) or ".." in raw.parts:
        raise HTTPException(
            status_code=400,
            detail="SECURITY_BLOCKED: 안전하지 않은 오디오 경로입니다.",
        )
    candidate = raw.absolute()
    base = _configured_lexical_base(config)
    if not candidate.is_relative_to(base):
        raise HTTPException(
            status_code=400,
            detail="SECURITY_BLOCKED: 오디오 파일이 설정된 base_dir 밖에 있습니다.",
        )
    return candidate


async def _require_audio_quality_accept(config: Any, audio_path: Path) -> None:
    """활성화된 공통 오디오 gate를 실행하고 비수락 사유를 HTTP로 매핑한다."""
    audio_path = _require_audio_in_base(config, audio_path)
    try:
        before_identity = await asyncio.to_thread(inspect_audio_path_no_symlinks, audio_path)
    except AudioAdmissionError as exc:
        raise HTTPException(
            status_code=_AUDIO_FAILURE_HTTP_STATUS[exc.failure_kind],
            detail=f"{exc.failure_kind.name}: {exc}",
        ) from exc
    except EmptyAudioError as exc:
        raise HTTPException(status_code=422, detail=f"MEDIA_INVALID: {exc}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=f"SOURCE_BUSY: {exc}") from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"INFRA_UNAVAILABLE: {exc}") from exc

    quality_config = getattr(config, "audio_quality", None)
    if quality_config is None or getattr(quality_config, "enabled", False) is not True:
        return

    try:
        result = await asyncio.to_thread(
            validate_audio_quality,
            audio_path,
            min_mean_db=quality_config.min_mean_volume_db,
            min_duration_s=quality_config.min_duration_seconds,
            expected_identity=before_identity,
            decode_timeout_base_seconds=quality_config.decode_timeout_base_seconds,
            decode_timeout_factor=quality_config.decode_timeout_factor,
            decode_timeout_cap_seconds=quality_config.decode_timeout_cap_seconds,
        )
    except Exception as exc:
        try:
            after_identity = await asyncio.to_thread(
                inspect_audio_path_no_symlinks,
                audio_path,
            )
        except AudioAdmissionError as identity_exc:
            raise HTTPException(
                status_code=_AUDIO_FAILURE_HTTP_STATUS[identity_exc.failure_kind],
                detail=f"{identity_exc.failure_kind.name}: {identity_exc}",
            ) from exc
        except (EmptyAudioError, FileNotFoundError) as identity_exc:
            raise HTTPException(
                status_code=409,
                detail=f"SOURCE_BUSY: 검증 중 오디오 파일이 변경되었습니다: {identity_exc}",
            ) from exc
        except OSError as identity_exc:
            raise HTTPException(
                status_code=503,
                detail=f"INFRA_UNAVAILABLE: {identity_exc}",
            ) from exc
        if after_identity != before_identity:
            raise HTTPException(
                status_code=409,
                detail="SOURCE_BUSY: 품질 검증 중 오디오 파일 identity가 변경되었습니다.",
            ) from exc
        raise HTTPException(
            status_code=503,
            detail=f"INFRA_UNAVAILABLE: 오디오 품질 검증 실행 실패: {exc}",
        ) from exc

    try:
        after_identity = await asyncio.to_thread(
            inspect_audio_path_no_symlinks,
            audio_path,
        )
    except AudioAdmissionError as exc:
        raise HTTPException(
            status_code=_AUDIO_FAILURE_HTTP_STATUS[exc.failure_kind],
            detail=f"{exc.failure_kind.name}: {exc}",
        ) from exc
    except (EmptyAudioError, FileNotFoundError) as exc:
        raise HTTPException(
            status_code=409,
            detail=f"SOURCE_BUSY: 검증 중 오디오 파일이 변경되었습니다: {exc}",
        ) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"INFRA_UNAVAILABLE: {exc}") from exc
    if after_identity != before_identity:
        raise HTTPException(
            status_code=409,
            detail="SOURCE_BUSY: 검증 중 오디오 파일 identity가 변경되었습니다.",
        )
    if result.status is AudioQualityStatus.ACCEPT:
        return

    failure_kind = result.failure_kind or AudioFailureKind.INFRA_UNAVAILABLE
    raise HTTPException(
        status_code=_AUDIO_FAILURE_HTTP_STATUS[failure_kind],
        detail=f"{failure_kind.name}: {result.reason or '오디오 품질 검증 비수락'}",
    )


class BatchActionRequest(BaseModel):
    """일괄 처리 요청 스키마.

    Attributes:
        action: 수행할 작업 종류 — "transcribe" | "summarize" | "full"
        scope: 대상 회의 수집 정책 — "all" | "recent" | "selected"
        hours: scope="recent" 일 때의 시간 윈도우 (1~720)
        meeting_ids: scope="selected" 일 때의 명시 회의 ID 목록.
            최대 500 개로 제한 (Phase 6 보안 감사 Medium-01: DoS 차단).
    """

    action: Literal["transcribe", "summarize", "full"]
    scope: Literal["all", "recent", "selected"]
    hours: int = Field(default=24, ge=1, le=720)
    # 보안 Medium-01 (Phase 6): 비정상적으로 큰 배열로 fs I/O / 정규식 매칭이
    # 폭주하는 것을 차단한다. 500 은 운영 환경의 단일 일괄 처리 상한선.
    meeting_ids: list[str] = Field(default_factory=list, max_length=500)
    request_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9-]{1,64}$")


class BatchActionResponse(BaseModel):
    """일괄 처리 응답 스키마.

    Attributes:
        status: "ok" 또는 "no_targets"
        message: 사람이 읽을 수 있는 결과 메시지
        action: 요청한 action 값 (echo)
        scope: 요청한 scope 값 (echo)
        matched: 후보로 식별된 회의 수 (필터 적용 전)
        queued: 실제 백그라운드 처리 대상으로 확정된 회의 수
        skipped: matched - queued (분류 불일치, audio 부재, 권한 등)
        meeting_ids: 실제 처리 대상 회의 ID 목록
    """

    status: Literal["ok", "no_targets"]
    message: str
    action: str
    scope: str
    matched: int
    queued: int
    skipped: int
    meeting_ids: list[str]
    request_id: str = ""
    candidates: list[dict[str, Any]] = Field(default_factory=list)


class BatchPreviewResponse(BaseModel):
    """일괄 처리 미리보기 응답 스키마.

    실제 파이프라인 실행 없이 `POST /api/meetings/batch` 와 동일한 대상 산정
    규칙으로 matched / queued / skipped 를 계산한다.
    """

    status: Literal["ok", "no_targets"]
    message: str
    action: str
    scope: str
    matched: int
    queued: int
    skipped: int
    meeting_ids: list[str]
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    time_basis: str = "앱 등록 시각 (created_at)"


@dataclass(frozen=True)
class PreparedBatch:
    """일괄 처리 대상 산정 결과."""

    matched: int
    skipped: int
    items: list[tuple[str, str, Path | None]]
    candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def queued(self) -> int:
        """실제 실행 가능한 회의 수."""
        return len(self.items)

    @property
    def meeting_ids(self) -> list[str]:
        """실제 실행 가능한 회의 ID 목록."""
        return [mid for (mid, _cls, _ap) in self.items]


def _has_merge_checkpoint(checkpoints_dir: Path, meeting_id: str) -> bool:
    """merge.json 체크포인트 존재 여부를 반환한다.

    Args:
        checkpoints_dir: 체크포인트 루트 디렉토리
        meeting_id: 회의 ID

    Returns:
        merge.json 이 있으면 True
    """
    return _is_regular_batch_artifact(checkpoints_dir, meeting_id, "merge.json")


def _has_summary_output(outputs_dir: Path, meeting_id: str) -> bool:
    """요약 결과물(summary.md 또는 meeting_minutes.md) 존재 여부를 반환한다.

    레거시 회의는 meeting_minutes.md, 신규 회의는 summary.md 를 사용한다.
    둘 중 하나라도 있으면 요약 완료로 간주한다.

    Args:
        outputs_dir: 출력 루트 디렉토리
        meeting_id: 회의 ID

    Returns:
        둘 중 하나라도 있으면 True
    """
    return _is_regular_batch_artifact(
        outputs_dir,
        meeting_id,
        "summary.md",
    ) or _is_regular_batch_artifact(
        outputs_dir,
        meeting_id,
        "meeting_minutes.md",
    )


def _classify_meeting_for_batch(
    checkpoints_dir: Path,
    outputs_dir: Path,
    meeting_id: str,
) -> Literal["transcribe", "summarize", "done"]:
    """회의의 현재 진행 단계를 분류한다.

    분류 규칙:
        - merge 체크포인트 없음 → "transcribe" (전사부터 필요)
        - merge 있음 + summary 없음 → "summarize" (LLM 단계만 필요)
        - merge + summary 모두 있음 → "done" (처리 불필요)

    Args:
        checkpoints_dir: 체크포인트 루트
        outputs_dir: 출력 루트
        meeting_id: 회의 ID

    Returns:
        분류 결과 문자열
    """
    if not _has_merge_checkpoint(checkpoints_dir, meeting_id):
        return "transcribe"
    if _has_summary_output(outputs_dir, meeting_id):
        return "done"
    return "summarize"


def _is_meeting_eligible(
    action: str,
    classification: str,
) -> bool:
    """주어진 action 에 대해 분류 결과가 적합한지 판단한다.

    매핑:
        - action="transcribe" → classification == "transcribe" 만 허용
        - action="summarize"  → classification == "summarize" 만 허용
        - action="full"       → classification ∈ {"transcribe", "summarize"}

    Args:
        action: 요청 action
        classification: _classify_meeting_for_batch 결과

    Returns:
        적합하면 True
    """
    if action == "transcribe":
        return classification == "transcribe"
    if action == "summarize":
        return classification == "summarize"
    if action == "full":
        return classification in ("transcribe", "summarize")
    return False


async def _get_job_for_batch(queue: Any, meeting_id: str) -> Any | None:
    """일괄 처리용 JobQueue row 를 조회한다.

    Args:
        queue: AsyncJobQueue 인스턴스
        meeting_id: 회의 ID

    Returns:
        Job row. 조회 불가 또는 미존재 시 None.
    """
    sync_queue = _get_sync_job_queue(queue)
    if sync_queue is None:
        logger.warning(f"일괄 처리: JobQueue 핸들을 얻을 수 없음 ({meeting_id})")
        return None
    try:
        return await asyncio.to_thread(sync_queue.get_job_by_meeting_id, meeting_id)
    except Exception as exc:
        logger.warning(f"일괄 처리: Job 조회 실패 — {meeting_id}: {exc}")
        return None


def _is_job_status_safe_for_batch(job: Any | None, classification: str) -> bool:
    """현재 작업 상태 기준으로 일괄 처리 대상에 포함해도 안전한지 판단한다.

    전사는 아직 큐에 들어가지 않은 recorded 작업만 허용한다. 요약은 merge
    체크포인트만 있으면 실행 가능하므로 레거시 데이터처럼 JobQueue row 가 없는
    경우도 허용하되, 명시적으로 진행 중이거나 failed 인 row 는 제외한다.
    """
    if job is None:
        return classification == "summarize"

    status = str(getattr(job, "status", "") or "")
    if status in _IN_PROGRESS_STATUSES:
        return False
    if status == "failed":
        return False
    if classification == "transcribe":
        return status == "recorded"
    if classification == "summarize":
        return status in {"completed", "recorded", ""}
    return False


async def _resolve_audio_path(
    queue: Any,
    meeting_id: str,
    base_dir_resolved: Path,
) -> Path | None:
    """JobQueue 에서 audio_path 를 조회해 검증한다.

    api.routes 의 기존 private re-export 호환을 유지하기 위한 wrapper 이다.
    신규 일괄 처리 경로는 이미 조회한 Job row 를 재사용하는
    _resolve_audio_path_from_job() 를 직접 호출한다.
    """
    job = await _get_job_for_batch(queue, meeting_id)
    if job is None:
        return None
    return _resolve_audio_path_from_job(job, meeting_id, base_dir_resolved)


def _resolve_audio_path_from_job(
    job: Any,
    meeting_id: str,
    base_dir_lexical: Path,
) -> Path | None:
    """Job row 의 audio_path 를 base_dir 내부 실재 파일로 검증한다.

    lexical 경로를 보존해 이후 no-follow 검사가 원래 direntry를 검사하게 한다.
    먼저 resolve하면 base 내부 symlink가 target 일반 파일로 바뀌어 보안 gate를
    우회할 수 있다.
    """
    if not getattr(job, "audio_path", None):
        return None

    try:
        raw_candidate = Path(job.audio_path).expanduser()
        if "\x00" in str(raw_candidate) or ".." in raw_candidate.parts:
            raise HTTPException(
                status_code=400,
                detail="SECURITY_BLOCKED: 안전하지 않은 오디오 경로입니다.",
            )
        candidate = raw_candidate.absolute()
    except HTTPException:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning(f"일괄 처리: audio_path 정규화 실패 ({meeting_id}): {exc}")
        raise HTTPException(
            status_code=400,
            detail=f"SECURITY_BLOCKED: 안전하지 않은 오디오 경로입니다: {exc}",
        ) from exc

    try:
        if not candidate.is_relative_to(base_dir_lexical):
            raise HTTPException(
                status_code=400,
                detail="SECURITY_BLOCKED: 오디오 파일이 설정된 base_dir 밖에 있습니다.",
            )
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="SECURITY_BLOCKED: 오디오 경로와 base_dir가 호환되지 않습니다.",
        ) from None

    return candidate


def _collect_candidate_ids_sync(
    scope: str,
    meeting_ids: list[str],
    all_jobs: list[Any],
    hours: int,
    checkpoints_dir: Path,
) -> list[str]:
    """scope 정책에 따라 후보 회의 ID 목록을 수집한다 (동기 함수).

    asyncio.to_thread 로 호출되어 이벤트 루프 블로킹을 방지한다 (Phase 6 perf C-1).

    수집 정책:
        - selected: 입력 meeting_ids 를 dedupe 만 적용해 그대로 사용
        - recent:   all_jobs 의 created_at 을 파싱하여 cutoff 기준 윈도우 필터
        - all:      DB Job ID와 checkpoints_dir의 폴더 ID를 합쳐 수집

    중복 제거 (Phase 3 Major #1): 같은 회의가 두 번 처리되어 LLM 토큰을 낭비하거나
    summary.md 가 덮어써지는 사고를 방지하기 위해 list(dict.fromkeys(...)) 로
    순서를 보존한 채 중복을 제거한다.

    Args:
        scope: "all" | "recent" | "selected"
        meeting_ids: scope="selected" 일 때 사용할 ID 목록
        all_jobs: scope="recent" 또는 "all" 일 때 사용할 Job 목록
        hours: scope="recent" 의 시간 윈도우
        checkpoints_dir: scope="all" 일 때 스캔할 디렉토리

    Returns:
        중복 제거된 회의 ID 목록 (순서 보존)
    """
    from datetime import datetime, timedelta

    candidate_ids: list[str] = []

    if scope == "selected":
        candidate_ids = list(meeting_ids)
    elif scope == "recent":
        cutoff = datetime.now() - timedelta(hours=hours)
        for job in all_jobs:
            mid = getattr(job, "meeting_id", None)
            created_at = getattr(job, "created_at", None)
            if not mid or not created_at:
                continue
            try:
                created_dt = datetime.fromisoformat(str(created_at))
            except (ValueError, TypeError):
                # 파싱 실패는 명시적으로 로깅하고 건너뛴다
                logger.warning(f"일괄 처리: created_at 파싱 실패 — 건너뜀 ({mid}: {created_at!r})")
                continue
            if created_dt >= cutoff:
                candidate_ids.append(mid)
    elif scope == "all":
        candidate_ids.extend(job.meeting_id for job in all_jobs if job.meeting_id)
        if checkpoints_dir.is_dir():
            for cp_dir in sorted(checkpoints_dir.iterdir()):
                try:
                    cp_stat = cp_dir.lstat()
                except OSError as exc:
                    logger.warning(
                        "일괄 처리: checkpoint entry 상태 확인 실패 (%s): %s", cp_dir, exc
                    )
                    continue
                if stat.S_ISDIR(cp_stat.st_mode):
                    candidate_ids.append(cp_dir.name)

    # Phase 3 Major #1: 순서 보존 dedupe
    return list(dict.fromkeys(candidate_ids))


def _classify_eligibility_sync(
    candidate_ids: list[str],
    action: str,
    scope: str,
    checkpoints_dir: Path,
    outputs_dir: Path,
) -> list[tuple[str, str]]:
    """후보 ID 목록을 분류하고 적합한 회의만 (id, classification) 페어로 반환한다.

    동기 함수로 asyncio.to_thread 를 통해 호출되어 이벤트 루프를 막지 않는다
    (Phase 6 perf C-1).

    scope != "selected" 인 경우, 디스크에서 가져온 ID 도 path traversal 방어를
    위해 _validate_meeting_id 로 재검증한다. 검증 실패 ID 는 silently skip
    (HTTPException 던지지 않음 — 디스크 자료는 사용자 입력이 아니므로).

    Args:
        candidate_ids: 사전 수집된 회의 ID 목록
        action: 요청 action
        scope: 요청 scope (selected 인지 검사용)
        checkpoints_dir: 체크포인트 루트
        outputs_dir: 출력 루트

    Returns:
        (meeting_id, classification) 페어 목록 — eligibility 통과한 회의만
    """
    pairs: list[tuple[str, str]] = []
    for mid in candidate_ids:
        # selected 는 엔드포인트에서 미리 _validate_meeting_id 로 검증됨.
        # selected 가 아닌 경우 (recent / all) 는 디스크 자료라 재검증 후 skip.
        if scope != "selected":
            try:
                _validate_meeting_id(mid)
            except HTTPException:
                logger.warning(f"일괄 처리: 디스크에서 발견된 비정상 meeting_id 건너뜀: {mid!r}")
                continue

        classification = _classify_meeting_for_batch(checkpoints_dir, outputs_dir, mid)
        if _is_meeting_eligible(action, classification):
            pairs.append((mid, classification))

    return pairs


async def _prepare_batch(
    request: Request,
    body: BatchActionRequest,
    *,
    review: bool = False,
) -> PreparedBatch:
    """일괄 처리 대상 목록을 실행 없이 산정한다.

    `preview` 와 실제 `batch_action` 이 같은 계산 경로를 쓰게 하여,
    확인 다이얼로그에 표시한 queued/skipped 수와 실제 시작 응답이 어긋나지
    않도록 한다.
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="서버 설정이 초기화되지 않았습니다.",
        )
    queue = _get_job_queue(request)

    checkpoints_dir = _configured_lexical_path(config, "checkpoints_dir")
    outputs_dir = _configured_lexical_path(config, "outputs_dir")
    # raw base_dir를 resolve하기 전에 no-follow로 검사한다. symlink base가 외부
    # target을 정상 경로처럼 보이게 하는 것을 차단한다.
    base_dir_lexical = _configured_lexical_base(config)

    if body.scope == "selected":
        for mid in body.meeting_ids:
            _validate_meeting_id(mid)
        all_jobs: list[Any] = []
    else:  # "recent" 또는 "all": 체크포인트가 없는 DB 녹음도 후보에 포함
        all_jobs = await queue.get_all_jobs()

    candidate_ids = await asyncio.to_thread(
        _collect_candidate_ids_sync,
        body.scope,
        body.meeting_ids,
        all_jobs,
        body.hours,
        checkpoints_dir,
    )
    matched = len(candidate_ids)

    from api.routers.meeting_detail import _read_pipeline_state_for_response
    from core.meeting_progress import effective_meeting_status, meeting_progress

    candidates: list[dict[str, Any]] = []
    final_items: list[tuple[str, str, Path | None]] = []
    for mid in candidate_ids:
        if body.scope != "selected":
            try:
                _validate_meeting_id(mid)
            except HTTPException:
                candidates.append(
                    dict(
                        meeting_id=mid,
                        title=mid,
                        eligible=False,
                        blocked=False,
                        reason="유효하지 않은 회의 ID",
                    )
                )
                continue
        job = await _get_job_for_batch(queue, mid)
        row: dict[str, Any] = {
            "meeting_id": mid,
            "title": getattr(job, "title", "") or mid,
            "created_at": getattr(job, "created_at", ""),
            "status": getattr(job, "status", "") or "unknown",
            "eligible": False,
            "reason": "",
            "blocked": False,
        }
        candidates.append(row)
        try:
            _validate_meeting_id(mid)
            state = await asyncio.to_thread(_read_pipeline_state_for_response, config, mid)
            row["status"] = effective_meeting_status(row["status"], state)
            row.update(meeting_progress(row["status"], state))
            classification = await asyncio.to_thread(
                _classify_meeting_for_batch, checkpoints_dir, outputs_dir, mid
            )
            if not _is_meeting_eligible(body.action, classification):
                row["reason"] = {
                    "done": "전사·요약이 이미 있습니다",
                    "summarize": "전사 완료 — 교정·요약 작업을 선택하세요",
                    "transcribe": "전사가 먼저 필요합니다",
                }[classification]
                continue
            if row["status"] in {"failed", "embedding"} or not _is_job_status_safe_for_batch(
                job, classification
            ):
                row["reason"] = (
                    "실패한 단계 재시도는 회의 상세에서 실행하세요"
                    if row["status"] == "failed"
                    else "대기·진행 중이거나 현재 상태에서 실행할 수 없습니다"
                )
                continue
            audio_path = None
            if classification == "transcribe":
                audio_path = _resolve_audio_path_from_job(job, mid, base_dir_lexical)
                if audio_path is None:
                    row["reason"] = "등록된 원본 오디오가 없습니다"
                    continue
                await _require_audio_quality_accept(config, audio_path)
            row["eligible"] = True
            final_items.append((mid, classification, audio_path))
        except HTTPException as exc:
            row["blocked"] = True
            row["reason"] = str(exc.detail)
            if not review:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail=f"{mid}: {exc.detail} — 전체 요청이 접수되지 않았습니다.",
                ) from exc

    queued = len(final_items)
    return PreparedBatch(
        matched=matched,
        skipped=matched - queued,
        items=final_items,
        candidates=candidates,
    )


async def _recheck_transcribe_items(
    queue: Any,
    config: Any,
    items: list[tuple[str, str, Path | None]],
) -> list[tuple[str, int, Any]]:
    """모든 전사 항목을 queue mutation 전에 한 번 더 검증한다.

    한 항목의 admission이 HTTP 오류를 내면 아직 어떤 job도 queued가 아니므로
    batch 전체가 무변경 상태로 종료된다.
    """
    validated: list[tuple[str, int, Any]] = []
    for meeting_id, classification, expected_audio_path in items:
        if classification != "transcribe" or expected_audio_path is None:
            continue
        job = await _get_job_for_batch(queue, meeting_id)
        if not _is_job_status_safe_for_batch(job, "transcribe"):
            logger.info(
                "일괄 처리 큐잉 건너뜀: 현재 작업 상태 부적합 (%s: %s)",
                meeting_id,
                getattr(job, "status", None) if job is not None else None,
            )
            continue
        assert job is not None
        current_audio_path = _resolve_audio_path_from_job(
            job,
            meeting_id,
            _configured_lexical_base(config),
        )
        if current_audio_path is None or current_audio_path != expected_audio_path:
            logger.warning("일괄 처리 큐잉 보류: audio_path 변경/소실 (%s)", meeting_id)
            continue
        try:
            await _require_audio_quality_accept(config, current_audio_path)
        except HTTPException as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=f"{meeting_id}: {exc.detail} — 전체 요청이 접수되지 않았습니다.",
            ) from exc
        validated.append((meeting_id, int(job.id), job))
    return validated


@router.post("/meetings/batch/preview", response_model=BatchPreviewResponse)
async def batch_action_preview(
    request: Request,
    body: BatchActionRequest,
) -> BatchPreviewResponse:
    """일괄 처리 대상을 미리 계산한다.

    백그라운드 파이프라인을 시작하지 않는다. 홈 확인 다이얼로그에서 사용자가
    실제 규모를 확인한 뒤 명시적으로 [시작]을 누르게 하기 위한 엔드포인트다.
    """
    prepared = await _prepare_batch(request, body)
    if prepared.queued == 0:
        return BatchPreviewResponse(
            status="no_targets",
            message="일괄 처리 대상 회의가 없습니다.",
            action=body.action,
            scope=body.scope,
            matched=prepared.matched,
            queued=0,
            skipped=prepared.skipped,
            meeting_ids=[],
            candidates=prepared.candidates,
        )

    return BatchPreviewResponse(
        status="ok",
        message=f"일괄 처리 대상 {prepared.queued}건을 찾았습니다.",
        action=body.action,
        scope=body.scope,
        matched=prepared.matched,
        queued=prepared.queued,
        skipped=prepared.skipped,
        meeting_ids=prepared.meeting_ids,
        candidates=prepared.candidates,
    )


@router.post("/meetings/batch/review", response_model=BatchPreviewResponse)
async def review_batch(request: Request, body: BatchActionRequest) -> BatchPreviewResponse:
    """파일별 검증 오류를 목록에 표시해 실행 전에 선택을 바꿀 수 있게 한다."""
    prepared = await _prepare_batch(request, body, review=True)
    return BatchPreviewResponse(
        status="ok" if prepared.queued else "no_targets",
        message="대상 확인 완료",
        action=body.action,
        scope=body.scope,
        matched=prepared.matched,
        queued=prepared.queued,
        skipped=prepared.skipped,
        meeting_ids=prepared.meeting_ids,
        candidates=prepared.candidates,
    )


@router.get("/batch-requests")
async def batch_history(request: Request) -> dict[str, Any]:
    """최근 접수 내역과 회의별 실행 이벤트를 다시 조회한다."""
    queue = _get_sync_job_queue(_get_job_queue(request))
    if queue is None:
        raise HTTPException(status_code=503, detail="접수 기록 저장소가 준비되지 않았습니다.")
    receipts = await asyncio.to_thread(queue.get_batch_receipts)
    # 후처리는 현재 프로세스의 task다. 재시작 후 이를 계속 실행 중이라고 표시하지 않는다.
    session = getattr(request.app.state, "batch_session", "")
    for receipt in receipts:
        if receipt.get("server_session") and receipt["server_session"] != session:
            for mid in receipt.get("background_ids", []):
                events = [e for e in receipt["events"] if e["meeting_id"] == mid]
                if not events or events[-1].get("status") not in {"completed", "failed"}:
                    receipt["events"].append(
                        dict(
                            meeting_id=mid,
                            status="interrupted",
                            status_label="앱 재시작으로 후처리 중단 · 다시 접수 필요",
                        )
                    )
    return {"requests": receipts}


def _receipt(
    body: BatchActionRequest, prepared: PreparedBatch, queued_ids: list[str], message: str = ""
) -> dict[str, Any]:
    """실제 접수 ID와 후보별 제외 결과를 고정한다."""
    rows = []
    for candidate in prepared.candidates:
        row = dict(candidate)
        row["admission"] = "queued" if row["meeting_id"] in queued_ids else "skipped"
        if row["admission"] == "skipped" and not row["reason"]:
            row["reason"] = message or "접수 직전 상태가 바뀌어 등록하지 못했습니다"
        rows.append(row)
    return dict(
        request_id=body.request_id,
        status="ok" if queued_ids else "no_targets",
        message=message or f"{len(queued_ids)}건 대기열 등록",
        action=body.action,
        scope=body.scope,
        matched=prepared.matched,
        queued=len(queued_ids),
        skipped=prepared.matched - len(queued_ids),
        meeting_ids=queued_ids,
        candidates=rows,
        requested_meeting_ids=body.meeting_ids,
        hours=body.hours,
    )


async def _execute_batch_action(
    request: Request,
    body: BatchActionRequest,
) -> BatchActionResponse:
    """전사·요약·full 통합 일괄 처리 엔드포인트.

    동작 흐름:
        1. config / pipeline / queue 로딩 (없으면 503)
        2. base_dir 절대 경로를 1회 resolve (Phase 6 perf M-1)
        3. scope=selected 면 _validate_meeting_id 로 사전 검증
           scope=recent/all 면 queue.get_all_jobs() 로 Job 목록 미리 조회
        4. 후보 ID 수집 — asyncio.to_thread (Phase 6 perf C-1)
        5. matched = len(candidate_ids)
        6. 분류·eligibility 검사 — asyncio.to_thread
        7. transcribe 분류 항목은 audio_path 사전 검증 후 JobProcessor 큐에 등록
        8. queued == 0 이면 status="no_targets" 응답
        9. summarize 분류 항목만 백그라운드 task 로 순차 실행

    Args:
        request: FastAPI Request 객체 (app.state 접근용)
        body: 일괄 처리 요청 스키마 (Pydantic 검증 통과)

    Returns:
        BatchActionResponse — matched / queued / skipped 카운트와 ID 목록

    Raises:
        HTTPException: 파이프라인/설정/큐 미초기화(503), meeting_ids 형식 오류(400)
    """
    # === 1. 의존성 로딩 ===
    pipeline = _get_pipeline_manager(request)
    queue = _get_job_queue(request)
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")
    prepared = await _prepare_batch(request, body)
    matched = prepared.matched
    skipped = prepared.skipped

    background_items = [item for item in prepared.items if item[1] != "transcribe"]
    if not getattr(request.app.state, "batch_session", None):
        request.app.state.batch_session = str(uuid4())
    server_session = request.app.state.batch_session
    transcribe_items = [item for item in prepared.items if item[1] == "transcribe"]

    # admission 전에 state → DB snapshot → config 순으로 각 회의의 실제
    # 선택을 한 번만 캡는다. 하나라도 OpenAI면 파일을 읽기 전에
    # Host/Origin 경계를 적용하고 같은 snapshot을 DB transaction에 저장한다.
    from api.routers.meeting_detail import _read_pipeline_state_for_response
    from api.routers.transcription_models import require_loopback_server
    from core.transcription_models import selection_from_state_or_config

    preflight_selections: dict[tuple[str, int], tuple[str, str]] = {}
    for meeting_id, classification, _audio_path in transcribe_items:
        if classification != "transcribe":
            continue
        candidate_job = await _get_job_for_batch(queue, meeting_id)
        if not _is_job_status_safe_for_batch(candidate_job, "transcribe"):
            continue
        assert candidate_job is not None
        try:
            state = _read_pipeline_state_for_response(config, meeting_id)
            selection = selection_from_state_or_config(config, state, job=candidate_job)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        preflight_selections[(meeting_id, int(candidate_job.id))] = (
            selection.provider,
            selection.model,
        )
        if selection.external_upload:
            require_loopback_server(config, request)

    # 모든 2차 admission을 끝낸 뒤에만 단일 DB transaction으로 큐잉한다.
    # item N의 gate/CAS 실패가 item 1..N-1을 부분 queued로 남기지 않는다.
    validated_transcribe = await _recheck_transcribe_items(queue, config, transcribe_items)
    skipped += len(transcribe_items) - len(validated_transcribe)
    queued_transcribe_ids: list[str] = []
    if validated_transcribe:
        from core.job_queue import JobQueueError

        sync_queue = _get_sync_job_queue(queue)
        if sync_queue is None:
            skipped += len(validated_transcribe)
        else:
            try:
                stt_selections: dict[int, tuple[str, str]] = {}
                for mid, job_id, job in validated_transcribe:
                    captured = preflight_selections.get((mid, job_id))
                    if captured is None:
                        raise JobQueueError("일괄 전사 선택 snapshot을 찾을 수 없습니다")
                    state = _read_pipeline_state_for_response(config, mid)
                    current = selection_from_state_or_config(config, state, job=job)
                    if captured != (current.provider, current.model):
                        raise JobQueueError("일괄 전사 선택이 admission 중 변경되었습니다")
                    stt_selections[job_id] = captured
                await asyncio.to_thread(
                    sync_queue.queue_jobs_atomically,
                    [job_id for _mid, job_id, _job in validated_transcribe],
                    body.action,
                    stt_selections=stt_selections,
                    receipt=dict(
                        _receipt(
                            body,
                            prepared,
                            [mid for mid, _jid, _job in validated_transcribe]
                            + [item[0] for item in background_items],
                        ),
                        server_session=server_session,
                        background_ids=[item[0] for item in background_items],
                    ),
                )
                queued_transcribe_ids = [mid for mid, _job_id, _job in validated_transcribe]
            except (JobQueueError, ValueError) as exc:
                logger.warning("일괄 처리 원자 큐잉 실패 — 전체 rollback: %s", exc)
                raise HTTPException(
                    status_code=409,
                    detail="접수 직전 작업 상태가 변경되어 전체 요청을 등록하지 못했습니다. 대상을 다시 확인하세요.",
                ) from exc

    queued_ids = queued_transcribe_ids + [mid for mid, _classification, _path in background_items]

    queued = len(queued_ids)
    sync_queue = _get_sync_job_queue(queue)
    receipt = dict(
        _receipt(body, prepared, queued_ids),
        server_session=server_session,
        background_ids=[item[0] for item in background_items],
    )
    if sync_queue is None:
        raise HTTPException(status_code=503, detail="접수 기록 저장소가 준비되지 않았습니다.")
    if not queued_transcribe_ids:
        await asyncio.to_thread(sync_queue.save_batch_receipt, receipt)

    # === 6. 후보 0 건이면 즉시 종료 ===
    if queued == 0:
        return BatchActionResponse(
            status="no_targets",
            message="일괄 처리 대상 회의가 없습니다.",
            action=body.action,
            scope=body.scope,
            matched=matched,
            queued=0,
            skipped=skipped,
            meeting_ids=[],
            request_id=body.request_id or "",
            candidates=receipt["candidates"],
        )

    # === 7. 백그라운드 task ===
    async def _run_batch(
        items: list[tuple[str, str, Path | None]],
        action: str,
    ) -> None:
        """회의별로 분류에 맞는 파이프라인 메서드를 순차 호출한다.

        한 회의 실패는 logger.exception 으로 기록 후 다음 회의 진행.

        Args:
            items: (meeting_id, classification, audio_path) 튜플 목록
            action: 요청 action (로그용)
        """
        for mid, classification, _audio_path in items:
            try:
                if classification == "transcribe":
                    # 전사 항목은 위에서 JobProcessor 큐에 넣었으므로 직접 실행하지 않는다.
                    continue
                elif classification == "summarize":

                    async def record_step(step: str, meeting_id: str = mid) -> None:
                        """후처리의 실제 단계 시작을 접수 기록에 남긴다."""
                        from core.meeting_progress import meeting_progress

                        state = _read_pipeline_state_for_response(config, meeting_id)
                        state = dict(state or {}, current_step=step)
                        await asyncio.to_thread(
                            sync_queue.record_batch_event,
                            meeting_id,
                            dict(
                                status="running", step=step, **meeting_progress("running", state)
                            ),
                            body.request_id,
                        )

                    await asyncio.to_thread(
                        sync_queue.record_batch_event,
                        mid,
                        {"status": "running", "step": "correct"},
                        body.request_id,
                    )
                    logger.info(f"일괄 처리[{action}] 요약 시작: {mid}")
                    await pipeline.run_llm_steps(mid, on_step_start=record_step)
                    await asyncio.to_thread(
                        sync_queue.record_batch_event,
                        mid,
                        {"status": "completed"},
                        body.request_id,
                    )
                    logger.info(f"일괄 처리[{action}] 요약 완료: {mid}")
                else:
                    logger.warning(f"일괄 처리: 알 수 없는 분류 '{classification}' 건너뜀 ({mid})")
            except Exception as exc:
                # 한 건 실패가 나머지 회의를 막지 않는다
                logger.exception(f"일괄 처리[{action}] 회의 실패: {mid}")
                from core.meeting_progress import meeting_progress

                state = _read_pipeline_state_for_response(config, mid)
                await asyncio.to_thread(
                    sync_queue.record_batch_event,
                    mid,
                    dict(
                        status="failed",
                        error_message=str(exc),
                        **meeting_progress("failed", state),
                    ),
                    body.request_id,
                )

    if background_items:
        task = asyncio.create_task(
            _run_batch(background_items, body.action),
            name=f"batch-action-{body.action}",
        )
        running_tasks = getattr(request.app.state, "running_tasks", None)
        if running_tasks is not None:
            running_tasks.add(task)
            task.add_done_callback(_log_task_exception)
            task.add_done_callback(running_tasks.discard)
        else:
            task.add_done_callback(_log_task_exception)

    logger.info(
        f"일괄 처리 시작: action={body.action}, scope={body.scope}, "
        f"matched={matched}, queued={queued}, skipped={skipped}"
    )

    return BatchActionResponse(
        status="ok",
        message=f"{queued}건 대기열 등록",
        action=body.action,
        scope=body.scope,
        matched=matched,
        queued=queued,
        skipped=skipped,
        meeting_ids=queued_ids,
        request_id=body.request_id or "",
        candidates=receipt["candidates"],
    )


@router.post("/meetings/batch", response_model=BatchActionResponse)
async def batch_action(request: Request, body: BatchActionRequest) -> BatchActionResponse:
    """요청 ID로 중복 접수를 막고 성공·거절 내역을 보존한다."""
    body = body.model_copy(update={"request_id": body.request_id or str(uuid4())})
    queue = _get_sync_job_queue(_get_job_queue(request))
    if queue is None:
        raise HTTPException(status_code=503, detail="접수 기록 저장소가 준비되지 않았습니다.")
    existing = await asyncio.to_thread(queue.get_batch_receipts, body.request_id)
    if existing:
        if (
            existing[0]["action"],
            existing[0]["scope"],
            existing[0].get("requested_meeting_ids", []),
            existing[0].get("hours", 24),
        ) != (body.action, body.scope, body.meeting_ids, body.hours):
            raise HTTPException(
                status_code=409,
                detail="이미 사용된 요청 ID입니다. 새 대상 확인 화면에서 접수해 주세요.",
            )
        return BatchActionResponse(**existing[0])
    try:
        return await _execute_batch_action(request, body)
    except HTTPException as exc:
        # 큐 등록 전에 거절된 요청도 추적한다. 이미 커밋된 접수는 덮어쓰지 않는다.
        existing = await asyncio.to_thread(queue.get_batch_receipts, body.request_id)
        if not existing:
            ids = list(dict.fromkeys(body.meeting_ids))
            prepared = PreparedBatch(
                matched=len(ids),
                skipped=len(ids),
                items=[],
                candidates=[
                    dict(meeting_id=mid, title=mid, eligible=False, reason=str(exc.detail))
                    for mid in ids
                ],
            )
            await asyncio.to_thread(
                queue.save_batch_receipt,
                _receipt(body, prepared, [], str(exc.detail)),
            )
        raise
