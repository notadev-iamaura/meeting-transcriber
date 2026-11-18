"""자동 전사/요약 일괄 처리 서비스.

설정된 시간대에 최근 회의 중 전사 또는 요약이 누락된 항목을 찾아 기존
PipelineManager 로 순차 처리한다. API 라우터와 스케줄러가 같은 실행 규칙을
공유할 수 있게 core 계층에 둔다.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from core.job_queue import nightly_transcription_exclusion
from core.nightly_window import current_window
from core.runtime_safety import auto_processing_safety_issues

logger = logging.getLogger(__name__)

AutoProcessingAction = Literal["transcribe", "summarize", "full"]
MeetingClassification = Literal["transcribe", "summarize", "done"]

_IN_PROGRESS_STATUSES = {
    "queued",
    "recording",
    "transcribing",
    "diarizing",
    "merging",
    "embedding",
}


@dataclass(frozen=True)
class AutoProcessingItem:
    """자동 처리 대상 회의."""

    job_id: int
    meeting_id: str
    classification: MeetingClassification
    audio_path: Path | None = None
    stt_provider: str = ""
    stt_model: str = ""


@dataclass
class AutoProcessingResult:
    """자동 처리 1회 실행 결과."""

    action: str
    recent_hours: int
    matched: int = 0
    queued: int = 0
    transcribed: int = 0
    summarized: int = 0
    skipped: int = 0
    skipped_by_limit: int = 0
    failed: int = 0
    meeting_ids: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    skipped_reasons: dict[str, int] = field(default_factory=dict)


class AutoProcessingRunner:
    """최근 회의 중 누락된 전사/요약을 순차 처리한다."""

    def __init__(
        self,
        *,
        config: Any,
        job_queue: Any,
        pipeline: Any,
    ) -> None:
        self._config = config
        self._job_queue = job_queue
        self._pipeline = pipeline

    async def prepare(
        self,
        *,
        action: AutoProcessingAction,
        recent_hours: int,
        nightly_window: str = "",
        exclusions: Counter[str] | None = None,
    ) -> list[AutoProcessingItem]:
        """자동 처리 대상 목록을 산정한다."""
        all_jobs = await self._job_queue.get_all_jobs()
        now = datetime.now()
        cutoff = now - timedelta(hours=recent_hours)
        checkpoints_dir = self._config.paths.resolved_checkpoints_dir
        outputs_dir = self._config.paths.resolved_outputs_dir
        base_dir = self._config.paths.resolved_base_dir

        items: list[AutoProcessingItem] = []
        seen: set[str] = set()
        for job in all_jobs:
            meeting_id = getattr(job, "meeting_id", "")
            created_at = getattr(job, "created_at", "")
            if not meeting_id or meeting_id in seen:
                continue
            seen.add(meeting_id)
            try:
                created_dt = datetime.fromisoformat(str(created_at))
            except (TypeError, ValueError):
                logger.warning(
                    "자동 처리: created_at 파싱 실패 — 건너뜀 (%s: %r)",
                    meeting_id,
                    created_at,
                )
                continue
            if created_dt.tzinfo is not None:
                created_dt = created_dt.astimezone().replace(tzinfo=None)
            if not cutoff <= created_dt <= now:
                continue

            classification = classify_meeting(checkpoints_dir, outputs_dir, meeting_id)
            status = str(getattr(job, "status", "") or "")
            error_message = str(getattr(job, "error_message", "") or "")
            if nightly_window and action != "summarize":
                reason = self._nightly_exclusion(job, action, classification, nightly_window)
                if reason is not None:
                    if reason and exclusions is not None:
                        exclusions[reason] += 1
                    continue
                classification = "transcribe"
            elif not is_action_eligible(action, classification) or not is_job_safe_to_auto_process(
                status,
                classification,
                error_message=error_message,
            ):
                logger.info(
                    "자동 처리: 현재 작업 상태 때문에 건너뜀 (%s: %s, %s)",
                    meeting_id,
                    status,
                    classification,
                )
                continue

            audio_path: Path | None = None
            if classification == "transcribe":
                audio_path = await asyncio.to_thread(
                    resolve_job_audio_path,
                    job,
                    base_dir,
                )
                if audio_path is None:
                    if exclusions is not None:
                        exclusions["오디오 경로 확인 필요"] += 1
                    logger.warning("자동 처리: 오디오 경로 검증 실패 — 건너뜀 (%s)", meeting_id)
                    continue
                if nightly_window:
                    reason = await asyncio.to_thread(
                        _nightly_audio_exclusion,
                        Path(job.audio_path),
                        audio_path,
                        self._config,
                        retry_failed=status == "failed",
                    )
                    if reason:
                        logger.info("야간 예약 제외 (%s): %s", meeting_id, reason)
                        if exclusions is not None:
                            exclusions[reason] += 1
                        continue

            items.append(
                AutoProcessingItem(
                    job_id=int(getattr(job, "id", 0) or 0),
                    meeting_id=meeting_id,
                    classification=classification,
                    audio_path=audio_path,
                    stt_provider=str(getattr(job, "stt_provider", "") or ""),
                    stt_model=str(getattr(job, "stt_model", "") or ""),
                )
            )

        return items

    def _nightly_exclusion(
        self,
        job: Any,
        action: str,
        classification: str,
        window: str,
    ) -> str | None:
        """신규 미전사와 이전 야간 작업의 미완료 단계만 허용한다. 대상 밖이면 빈 사유다."""
        mid = job.meeting_id
        retry_existing = (
            action == "full"
            and job.status == "failed"
            and job.requested_action == "nightly_full"
            and job.last_nightly_window
        )
        if classification != "transcribe" and not retry_existing:
            return ""
        reason = nightly_transcription_exclusion(job, window)
        if reason:
            return reason
        paths = self._config.paths
        corrected = (paths.resolved_outputs_dir / mid / "corrected.json").exists() or (
            paths.resolved_checkpoints_dir / mid / "correct.json"
        ).exists()
        summarized = has_summary_output(paths.resolved_outputs_dir, mid)
        if classification == "transcribe":
            return "기존 전사 또는 회의록 보존" if corrected or summarized else None
        # 완료된 앞 단계의 결과만 재사용한다. 출처를 확정할 수 없는 산출물은 보존한다.
        try:
            state = self._pipeline.get_status(mid)
        except (OSError, ValueError, RuntimeError):
            return "이전 처리 기록 확인 필요"
        if not state or state.status != "failed" or state.skipped_steps:
            return "이전 처리 기록 확인 필요"
        completed = state.completed_steps
        if (
            "merge" not in completed
            or (corrected and "correct" not in completed)
            or (summarized and "summarize" not in completed)
        ):
            return "기존 전사 또는 회의록 보존"
        return None

    async def run(
        self,
        *,
        action: AutoProcessingAction,
        recent_hours: int,
        scheduled: bool = False,
    ) -> AutoProcessingResult:
        """자동 처리를 1회 실행한다.

        한 회의가 실패해도 나머지 회의는 계속 처리한다.
        """
        safety_issues = auto_processing_safety_issues(self._config, action=action)
        if safety_issues:
            logger.warning(
                "자동 처리 보류: %s",
                "; ".join(issue.message for issue in safety_issues),
            )
            return AutoProcessingResult(
                action=action,
                recent_hours=recent_hours,
                skipped=len(safety_issues),
                errors=[issue.to_dict() for issue in safety_issues],
            )

        # 예약 full은 최근 미전사 녹음을 처음부터 색인까지 처리한다. 이미 전사를
        # 완료해 사용자가 편집한 회의에는 수동으로 요약을 요청할 수 있다.
        window_key = ""
        excluded: Counter[str] = Counter()
        if scheduled:
            window = current_window(self._config.auto_processing, datetime.now())
            if not window:
                return AutoProcessingResult(action=action, recent_hours=recent_hours)
            window_key = window[0].isoformat()
        prepared_items = await self.prepare(
            action=action,
            recent_hours=recent_hours,
            nightly_window=window_key,
            exclusions=excluded if scheduled else None,
        )
        max_items = int(getattr(self._config.auto_processing, "max_items_per_run", 0))
        items = prepared_items if max_items == 0 else prepared_items[:max_items]
        skipped_by_limit = max(0, len(prepared_items) - len(items))

        if skipped_by_limit > 0:
            logger.info(
                "자동 처리: 1회 처리 상한으로 %d건 보류 (max_items_per_run=%d)",
                skipped_by_limit,
                max_items,
            )

        result = AutoProcessingResult(
            action=action,
            recent_hours=recent_hours,
            matched=len(prepared_items),
            skipped=skipped_by_limit + excluded.total(),
            skipped_by_limit=skipped_by_limit,
            skipped_reasons=dict(excluded),
        )

        for item in items:
            try:
                if item.classification == "transcribe":
                    if item.audio_path is None:
                        result.skipped += 1
                        continue
                    if item.job_id <= 0:
                        raise ValueError("자동 처리 대상의 job_id가 올바르지 않습니다.")
                    from core.transcription_models import selection_from_state_or_config

                    get_status = getattr(type(self._pipeline), "get_status", None)
                    pipeline_state = (
                        get_status(self._pipeline, item.meeting_id)
                        if callable(get_status)
                        else None
                    )
                    selection_config = self._config
                    if scheduled:
                        selection_config = self._config.model_copy(
                            update={
                                "stt": self._config.stt.model_copy(update={"provider": "local"})
                            }
                        )
                    stt_selection = selection_from_state_or_config(
                        selection_config,
                        pipeline_state,
                        job=item,
                    )
                    requested_action = "transcribe" if action == "transcribe" else "full"
                    if scheduled:
                        if stt_selection.provider != "local" or item.stt_provider == "openai":
                            result.skipped += 1
                            reason = "외부 전사 선택"
                            result.skipped_reasons[reason] = (
                                result.skipped_reasons.get(reason, 0) + 1
                            )
                            continue
                        requested_action = f"nightly_{requested_action}"
                    logger.info("자동 처리[%s] 전사 큐 등록: %s", action, item.meeting_id)
                    await self._job_queue.queue_job(
                        item.job_id,
                        requested_action=requested_action,
                        stt_provider=stt_selection.provider,
                        stt_model=stt_selection.model,
                        **({"nightly_window": window_key} if scheduled else {}),
                    )
                    # queue_job 성공은 파이프라인 완료가 아니라 전사 작업을
                    # 수락해 대기열에 등록했다는 뜻이다.
                    result.queued += 1
                    result.meeting_ids.append(item.meeting_id)
                    logger.info("자동 처리[%s] 전사 큐 등록 완료: %s", action, item.meeting_id)
                elif item.classification == "summarize":
                    logger.info("자동 처리[%s] 요약 시작: %s", action, item.meeting_id)
                    await self._pipeline.run_llm_steps(item.meeting_id)
                    result.summarized += 1
                    result.meeting_ids.append(item.meeting_id)
                    logger.info("자동 처리[%s] 요약 완료: %s", action, item.meeting_id)
                else:
                    result.skipped += 1
            except Exception as exc:
                result.failed += 1
                result.errors.append(
                    {
                        "meeting_id": item.meeting_id,
                        "error": str(exc),
                    }
                )
                logger.exception("자동 처리 회의 실패: %s", item.meeting_id)

        return result


def _nightly_audio_exclusion(
    source: Path, resolved_path: Path, config: Any, *, retry_failed: bool
) -> str:
    """격리·심볼릭 링크를 제외하고 실패 건의 원본을 재검증한다."""
    from core.audio_quality import AudioQualityStatus, validate_audio_quality
    from steps.transcriber import (
        AudioAdmissionError,
        EmptyAudioError,
        inspect_audio_path_no_symlinks,
    )

    if resolved_path.is_relative_to(config.paths.resolved_audio_quarantine_dir):
        return "격리된 오디오"
    try:
        identity = inspect_audio_path_no_symlinks(source)
        if retry_failed:
            quality = config.audio_quality
            verdict = validate_audio_quality(
                source,
                min_mean_db=quality.min_mean_volume_db,
                min_duration_s=quality.min_duration_seconds,
                expected_identity=identity,
                decode_timeout_base_seconds=quality.decode_timeout_base_seconds,
                decode_timeout_factor=quality.decode_timeout_factor,
                decode_timeout_cap_seconds=quality.decode_timeout_cap_seconds,
            )
            if inspect_audio_path_no_symlinks(source) != identity:
                return "오디오 변경 중"
            if verdict.status is not AudioQualityStatus.ACCEPT:
                kind = verdict.failure_kind
                return f"오디오 검증 보류 ({kind.value if kind else 'unknown'})"
    except (AudioAdmissionError, EmptyAudioError, OSError):
        return "오디오 경로 확인 필요"
    return ""


def has_summary_output(outputs_dir: Path, meeting_id: str) -> bool:
    """요약 결과 파일 존재 여부를 반환한다."""
    out_dir = outputs_dir / meeting_id
    return (out_dir / "summary.md").is_file() or (out_dir / "meeting_minutes.md").is_file()


def classify_meeting(
    checkpoints_dir: Path,
    outputs_dir: Path,
    meeting_id: str,
) -> MeetingClassification:
    """회의의 누락 상태를 분류한다."""
    if not (checkpoints_dir / meeting_id / "merge.json").is_file():
        return "transcribe"
    if has_summary_output(outputs_dir, meeting_id):
        return "done"
    return "summarize"


def is_action_eligible(action: str, classification: str) -> bool:
    """자동 처리 action 에 해당 분류가 포함되는지 반환한다."""
    if action == "transcribe":
        return classification == "transcribe"
    if action == "summarize":
        return classification == "summarize"
    if action == "full":
        return classification in {"transcribe", "summarize"}
    return False


def is_job_safe_to_auto_process(
    status: str,
    classification: str,
    *,
    error_message: str = "",
) -> bool:
    """현재 작업 상태 기준으로 자동 처리해도 안전한지 반환한다.

    JobProcessor 가 이미 처리 중인 상태는 자동 스케줄러가 건드리지 않는다.
    전사는 아직 큐잉되지 않은 recorded 상태만 자동 시작한다. 요약은 전사/병합이
    끝난 completed/recorded 계열에서만 허용하고 failed 는 자동 반복 실패를
    피하기 위해 제외한다.
    """
    if status in _IN_PROGRESS_STATUSES:
        return False
    if status == "failed":
        return False
    if status == "recorded" and error_message.startswith("사용자가 취소함"):
        return False
    if classification == "transcribe":
        return status == "recorded"
    if classification == "summarize":
        return status in {"completed", "recorded"}
    return False


def resolve_job_audio_path(job: Any, base_dir_resolved: Path) -> Path | None:
    """작업 큐의 audio_path 를 실제 base_dir 내부 파일로 검증한다."""
    raw_path = getattr(job, "audio_path", "")
    if not raw_path:
        return None
    try:
        candidate = Path(raw_path).resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        return None
    try:
        if not candidate.is_relative_to(base_dir_resolved):
            return None
    except ValueError:
        return None
    return candidate
