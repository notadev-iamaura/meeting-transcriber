"""자동 전사/요약 일괄 처리 서비스.

설정된 시간대에 최근 회의 중 전사 또는 요약이 누락된 항목을 찾아 기존
PipelineManager 로 순차 처리한다. API 라우터와 스케줄러가 같은 실행 규칙을
공유할 수 있게 core 계층에 둔다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

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

    meeting_id: str
    classification: MeetingClassification
    audio_path: Path | None = None


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
    failed: int = 0
    meeting_ids: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


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
    ) -> list[AutoProcessingItem]:
        """자동 처리 대상 목록을 산정한다."""
        all_jobs = await self._job_queue.get_all_jobs()
        cutoff = datetime.now() - timedelta(hours=recent_hours)
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
            if created_dt < cutoff:
                continue

            classification = classify_meeting(checkpoints_dir, outputs_dir, meeting_id)
            if not is_action_eligible(action, classification):
                continue
            status = str(getattr(job, "status", "") or "")
            if not is_job_safe_to_auto_process(status, classification):
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
                    logger.warning("자동 처리: 오디오 경로 검증 실패 — 건너뜀 (%s)", meeting_id)
                    continue

            items.append(
                AutoProcessingItem(
                    meeting_id=meeting_id,
                    classification=classification,
                    audio_path=audio_path,
                )
            )

        return items

    async def run(
        self,
        *,
        action: AutoProcessingAction,
        recent_hours: int,
    ) -> AutoProcessingResult:
        """자동 처리를 1회 실행한다.

        한 회의가 실패해도 나머지 회의는 계속 처리한다.
        """
        items = await self.prepare(action=action, recent_hours=recent_hours)
        result = AutoProcessingResult(
            action=action,
            recent_hours=recent_hours,
            matched=len(items),
            queued=len(items),
            meeting_ids=[item.meeting_id for item in items],
        )

        for item in items:
            try:
                if item.classification == "transcribe":
                    if item.audio_path is None:
                        result.skipped += 1
                        continue
                    logger.info("자동 처리[%s] 전사 시작: %s", action, item.meeting_id)
                    await self._pipeline.run(
                        item.audio_path,
                        meeting_id=item.meeting_id,
                        skip_llm_steps=(action == "transcribe"),
                    )
                    result.transcribed += 1
                    if action == "full":
                        result.summarized += 1
                    logger.info("자동 처리[%s] 전사 완료: %s", action, item.meeting_id)
                elif item.classification == "summarize":
                    logger.info("자동 처리[%s] 요약 시작: %s", action, item.meeting_id)
                    await self._pipeline.run_llm_steps(item.meeting_id)
                    result.summarized += 1
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


def is_job_safe_to_auto_process(status: str, classification: str) -> bool:
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
