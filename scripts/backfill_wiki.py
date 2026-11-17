"""기존 회의 일괄 위키화 스크립트 (LLM Wiki Phase 4.E)

목적: WikiCompiler 도입 이전 또는 wiki.enabled=False 였던 시기의 회의들을
일괄 컴파일하여 위키 페이지를 생성한다. 회의별 트랜잭션 — 1건 실패해도
다음 회의는 정상 진행 (PRD §11 R5 완화).

사용법:
    # CLI — 전체 회의 백필
    python -m scripts.backfill_wiki

    # CLI — 날짜 범위
    python -m scripts.backfill_wiki --since 2026-01-01 --until 2026-04-29

    # CLI — dry-run (대상 목록만 출력)
    python -m scripts.backfill_wiki --dry-run

    # API 호출 (동기 import)
    from scripts.backfill_wiki import backfill, BackfillResult
    result: BackfillResult = await backfill(
        config=config,
        job_queue=async_queue.queue,
        since=date(2026, 4, 1),
        until=date(2026, 4, 29),
    )

회의 메타데이터 소스:
    - 1차: ``core.job_queue.JobQueue.get_all_jobs()`` — meeting_id, 상태.
    - 날짜: 보존된 meeting_date/파일명/검증한 원본 시각. 등록일·오늘 날짜로 추측하지 않음.
    - 2차 (각 회의 내부): ``outputs/{id}/corrected.json`` (utterances) +
      ``outputs/{id}/summary.md`` (요약).
    - 두 파일 중 하나라도 없으면 BackfillError 로 누적되며 다음 회의 진행.

의존성:
    - core.wiki.compiler.WikiCompilerV2 (steps.wiki_compiler 의 _create_wiki_compiler_v2)
    - core.job_queue.JobQueue (회의 목록 조회)
    - config.AppConfig
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
import time
import traceback as tb_mod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, cast

logger = logging.getLogger(__name__)


# ─── 결과 데이터 클래스 ─────────────────────────────────────────────────


@dataclass(frozen=True)
class BackfillError:
    """백필 중 발생한 회의별 실패 1건.

    Attributes:
        meeting_id: 실패한 회의 ID.
        error_type: 안정적 코드 — "summary_missing" | "utterances_missing" |
            "wiki_compile_failed" | "unknown".
        message: 사람이 읽는 메시지 (한국어).
        traceback: stack trace (디버그용).
    """

    meeting_id: str
    error_type: str
    message: str
    traceback: str | None = None


@dataclass
class BackfillResult:
    """백필 전체 결과.

    Attributes:
        total: 처리 대상 회의 수.
        succeeded: 성공한 회의 수 (페이지 생성 또는 갱신 발생).
        skipped: 이미 처리된 회의 수 (멱등 추적).
        failed: 실패한 회의 수.
        pending: 공개 페이지 없이 검토 후보만 남긴 회의 수. succeeded/failed와 중복하지 않는다.
        empty: 공개·검토 페이지가 모두 없는 회의 수(failed의 부분집합).
        published_pages: 공개 경로에서 생성·갱신한 페이지 수.
        pending_pages: 검토 후보 수.
        rejected_pages: 검증에서 거부한 후보 수.
        errors: BackfillError 리스트.
        duration_seconds: 전체 경과 시간.
        compiled_at: ISO8601 시작 시각.
    """

    total: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    pending: int = 0
    empty: int = 0
    published_pages: int = 0
    pending_pages: int = 0
    rejected_pages: int = 0
    errors: list[BackfillError] = field(default_factory=list)
    duration_seconds: float = 0.0
    compiled_at: str = ""


# ─── 회의 데이터 로딩 헬퍼 ──────────────────────────────────────────────


def _coerce_utterance_dict(raw: Any) -> Any | None:
    """JSON utterance dict 를 CorrectedUtterance 호환 객체로 정규화한다."""
    if not isinstance(raw, dict):
        return None

    from steps.corrector import CorrectedUtterance

    text = raw.get("text")
    speaker = raw.get("speaker")
    if (
        not isinstance(text, str)
        or not text.strip()
        or not isinstance(speaker, str)
        or not speaker.strip()
    ):
        return None
    if any(isinstance(raw.get(key), bool) or raw.get(key) is None for key in ("start", "end")):
        return None
    try:
        start = float(raw["start"])
        end = float(raw["end"])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        return None

    return CorrectedUtterance(
        text=text,
        original_text=str(raw.get("original_text") or text),
        speaker=speaker,
        start=start,
        end=end,
        was_corrected=bool(raw.get("was_corrected", False)),
    )


def _load_utterances(config: Any, meeting_id: str) -> list[Any] | None:
    """회의의 보정된 utterances 를 로드한다.

    폴백 순서:
        1. outputs/{id}/corrected.json (LLM 보정 완료)
        2. checkpoints/{id}/correct.json (보정 체크포인트)
        3. checkpoints/{id}/merge.json (병합 결과, 미보정)

    Args:
        config: AppConfig.
        meeting_id: 회의 식별자.

    Returns:
        CorrectedUtterance 호환 객체의 리스트. 파일이 없거나 읽기 실패 시 None.
    """
    from core.search_revision import read_source

    try:
        _, data = read_source(config, meeting_id)
    except FileNotFoundError:
        return None
    utterances = data.get("utterances")
    if not isinstance(utterances, list) or not utterances:
        raise ValueError("보존된 전사 원문에 유효한 발화 목록이 없습니다.")
    normalized = [_coerce_utterance_dict(raw) for raw in utterances]
    if any(utterance is None for utterance in normalized):
        raise ValueError("전사 원문의 텍스트·화자·유한한 시작/종료 시각을 확인할 수 없습니다.")
    return cast(list[Any], normalized)


def _load_summary(config: Any, meeting_id: str) -> str | None:
    """회의의 요약 마크다운을 로드한다.

    폴백 순서:
        1. outputs/{id}/summary.md
        2. outputs/{id}/meeting_minutes.md
        3. outputs/{id}/summary.json 의 markdown 필드
        4. checkpoints/{id}/summarize.json 의 markdown 필드

    Args:
        config: AppConfig.
        meeting_id: 회의 식별자.

    Returns:
        요약 마크다운 텍스트. 없으면 None.
    """
    from core.io_utils import read_text_no_follow
    from core.reindex_recovery import _configured_storage_root, _validate_meeting_id

    _validate_meeting_id(meeting_id)
    outputs_dir = _configured_storage_root(
        config, "outputs_dir", config.paths.resolved_outputs_dir
    )
    checkpoints_dir = _configured_storage_root(
        config, "checkpoints_dir", config.paths.resolved_checkpoints_dir
    )

    md_candidates = [
        outputs_dir / meeting_id / "summary.md",
        outputs_dir / meeting_id / "meeting_minutes.md",
    ]
    for path in md_candidates:
        try:
            text = read_text_no_follow(path)
        except FileNotFoundError:
            continue
        if not text.strip():
            raise ValueError("보존된 요약 문서가 비어 있습니다.")
        return str(text)

    json_candidates = [
        outputs_dir / meeting_id / "summary.json",
        checkpoints_dir / meeting_id / "summarize.json",
    ]
    for path in json_candidates:
        try:
            data = json.loads(read_text_no_follow(path))
        except FileNotFoundError:
            continue
        if not isinstance(data, dict):
            raise ValueError("보존된 요약 체크포인트 형식이 잘못되었습니다.")
        markdown = data.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("보존된 요약 체크포인트에 Markdown 내용이 없습니다.")
        return markdown
    return None


# ─── 회의 목록 필터링 ──────────────────────────────────────────────────


def _filter_jobs(
    jobs: list[Any],
    *,
    meeting_ids: list[str] | None,
) -> list[Any]:
    """명시적 ID 범위만 고르고 날짜 필터는 보존된 날짜 조회 후 적용한다.

    Args:
        jobs: JobQueue.get_all_jobs() 결과.
        meeting_ids: 명시적 회의 ID 목록.

    Returns:
        필터링된 job 리스트.
    """
    if meeting_ids is not None:
        target = set(meeting_ids)
        return [j for j in jobs if j.meeting_id in target]

    return list(jobs)


def _compile_result_attr(compile_result: Any, name: str, default: Any) -> Any:
    """CompileResult 또는 테스트 mock 에서 명시된 속성만 안전하게 읽는다."""
    try:
        attrs = vars(compile_result)
    except TypeError:
        attrs = {}
    if name in attrs:
        return attrs[name]

    value = getattr(compile_result, name, default)
    if value.__class__.__module__ == "unittest.mock":
        return default
    return value


# ─── 단일 회의 컴파일 (mock 가능) ──────────────────────────────────────


async def _compile_single_meeting(
    *,
    config: Any,
    meeting_id: str,
    meeting_date: date,
    summary: str,
    utterances: list[Any],
) -> Any:
    """단일 회의를 WikiCompilerV2 로 컴파일한다.

    이 함수는 ``scripts.backfill_wiki._compile_single_meeting`` 으로 monkeypatch
    가능한 진입점이다. 테스트는 이 함수를 mock 하여 LLM 호출을 회피한다.

    Args:
        config: AppConfig.
        meeting_id: 실제 회의 ID 또는 하위 호환 8자리 hex.
        meeting_date: 회의 날짜.
        summary: 8단계 요약 마크다운.
        utterances: 5단계 보정 발화 리스트.

    Returns:
        WikiCompilerV2.compile_meeting() 결과 (CompileResult).

    Raises:
        Exception: 컴파일 실패 — 호출자가 BackfillError 로 변환.
    """
    # Lazy import — 백필 비활성 환경에서 import 비용 0.
    from core.model_manager import get_model_manager
    from core.wiki.store import WikiStore
    from steps.wiki_compiler import _create_wiki_compiler_v2

    wiki_root = config.wiki.resolved_root
    store = WikiStore(wiki_root)
    store.init_repo()

    manager = get_model_manager()
    v2 = _create_wiki_compiler_v2(
        config=config,
        store=store,
        model_manager=manager,
        utterances=utterances,
        meeting_id=meeting_id,
    )
    try:
        return await v2.compile_meeting(
            meeting_id=meeting_id,
            meeting_date=meeting_date,
            summary=summary,
            utterances=utterances,
        )
    finally:
        from core.wiki.llm_client import WIKI_LLM_MODEL_NAME

        unload = getattr(manager, "unload_if_current", None)
        if callable(unload):
            await unload(WIKI_LLM_MODEL_NAME)


def _meeting_date_for_job(config: Any, job: Any) -> date | None:
    """등록일을 추측하지 않고 보존된 회의 날짜만 읽는다."""
    from core.search_revision import meeting_date as source_meeting_date
    from core.search_revision import read_source

    meeting_id = getattr(job, "meeting_id", "")
    try:
        _, source = read_source(config, meeting_id)
    except FileNotFoundError:
        source = {}
    audio_path = getattr(job, "audio_path", None)
    if isinstance(audio_path, str) and audio_path:
        source.setdefault("audio_path", audio_path)
    value = source_meeting_date(config, meeting_id, source)
    return date.fromisoformat(value) if value else None


def _compile_page_count(compile_result: Any, *fields: str) -> int:
    """명시된 산출물 목록의 개수만 읽는다."""
    return sum(len(_compile_result_attr(compile_result, field, []) or []) for field in fields)


class BackfillAdmissionError(ValueError):
    """실행 전 상태·원문 검증 실패를 안정적인 오류 코드로 전달한다."""

    def __init__(self, code: str, message: str) -> None:
        """거부 사유를 저장한다."""
        super().__init__(message)
        self.code = code


async def _run_backfill_job(
    *,
    config: Any,
    job_queue: Any,
    job: Any,
    expected_date: date,
    coordinator: Any,
    thermal_manager: Any,
    cancel_event: asyncio.Event | None,
) -> Any:
    """회의 lease 안에서 최신 상태·안전한 원문을 읽고 Wiki만 갱신한다."""
    meeting_id = job.meeting_id
    async with coordinator.lease(meeting_id):
        refresh = getattr(job_queue, "get_job_by_meeting_id", None)
        if callable(refresh) and refresh.__class__.__module__ != "unittest.mock":
            job = await asyncio.to_thread(refresh, meeting_id)
            if job is None:
                raise BackfillAdmissionError(
                    "meeting_missing", "회의가 삭제되어 백필을 중단했습니다."
                )
        if str(getattr(job, "status", "")) != "completed":
            raise BackfillAdmissionError(
                "meeting_not_completed", "전사·요약이 완료된 회의만 Wiki를 재검증할 수 있습니다."
            )
        actual_date = await asyncio.to_thread(_meeting_date_for_job, config, job)
        if actual_date != expected_date:
            raise BackfillAdmissionError(
                "meeting_date_changed", "계획 이후 회의 날짜가 변경되어 재검증을 보류했습니다."
            )
        utterances = await asyncio.to_thread(_load_utterances, config, meeting_id)
        if utterances is None:
            raise BackfillAdmissionError(
                "utterances_missing", "교정·병합 원문이 없어 Wiki를 생성할 수 없습니다."
            )
        summary = await asyncio.to_thread(_load_summary, config, meeting_id)
        if not summary:
            raise BackfillAdmissionError(
                "summary_missing", "요약 문서가 없어 Wiki를 생성할 수 없습니다."
            )
        if cancel_event is not None and cancel_event.is_set():
            raise BackfillAdmissionError("cancelled", "실행 전 취소되었습니다.")
        await thermal_manager.wait_if_needed()
        if cancel_event is not None and cancel_event.is_set():
            raise BackfillAdmissionError("cancelled", "대기 중 취소되었습니다.")
        await thermal_manager.notify_job_started()
        try:
            return await _compile_single_meeting(
                config=config,
                meeting_id=meeting_id,
                meeting_date=actual_date,
                summary=summary,
                utterances=utterances,
            )
        finally:
            await thermal_manager.notify_job_completed()


# ─── 메인 백필 함수 ────────────────────────────────────────────────────


async def backfill(
    *,
    config: Any,
    job_queue: Any,
    since: date | None = None,
    until: date | None = None,
    meeting_ids: list[str] | None = None,
    dry_run: bool = False,
    progress_callback: Callable[[int, int, str], None] | None = None,
    cancel_event: asyncio.Event | None = None,
    meeting_mutation_coordinator: Any | None = None,
    thermal_manager: Any | None = None,
) -> BackfillResult:
    """기존 회의를 일괄 위키화한다.

    회의별 트랜잭션:
        - 1건 실패는 isolated — 다음 회의 진행.
        - 모든 실패는 BackfillResult.errors 에 누적.
        - cancel_event 가 set 되면 진행 중 회의 완료 후 즉시 중단.

    필터링:
        - meeting_ids 지정 시 since/until 무시.
        - since/until 둘 다 None: 전체 회의.

    Args:
        config: AppConfig.
        job_queue: ``core.job_queue.JobQueue`` 인스턴스 (동기). 메서드:
            - get_all_jobs() -> list[Job] (meeting_id, created_at 등 속성).
        since: 시작일 (포함).
        until: 종료일 (포함).
        meeting_ids: 명시적 회의 목록.
        dry_run: True 면 컴파일 함수를 호출하지 않고 대상 목록만 결정.
        progress_callback: (processed, total, current_meeting_id) 콜백.
        cancel_event: 취소 신호.

    Returns:
        BackfillResult — 절대 raise 하지 않음.
    """
    start_ts = time.time()
    started_iso = datetime.now().isoformat()
    result = BackfillResult(compiled_at=started_iso)
    dry_run = dry_run or bool(getattr(config.wiki, "dry_run", True))
    if not dry_run and not bool(getattr(config.wiki, "enabled", False)):
        result.failed = 1
        result.errors.append(
            BackfillError("", "wiki_disabled", "Wiki 설정을 활성화한 후 실행해 주세요.")
        )
        result.duration_seconds = time.time() - start_ts
        return result

    # ── 1. 회의 목록 조회 (동기 메서드 → 스레드로) ────────────────
    try:
        all_jobs = await asyncio.to_thread(job_queue.get_all_jobs)
    except Exception as exc:  # noqa: BLE001 — 실패해도 빈 결과 반환.
        logger.error("백필: get_all_jobs 실패 — %r", exc)
        result.failed = 1
        result.errors.append(BackfillError("", "job_list_failed", "회의 목록을 읽을 수 없습니다."))
        result.duration_seconds = time.time() - start_ts
        return result

    # ── 2. 필터링 ───────────────────────────────────────────────────
    target_jobs = _filter_jobs(
        all_jobs,
        meeting_ids=meeting_ids,
    )
    dates: dict[str, date | None] = {}
    errors_by_id: dict[str, str] = {}
    filtered_jobs: list[Any] = []
    for job in target_jobs:
        mid = str(getattr(job, "meeting_id", ""))
        try:
            job_date = await asyncio.to_thread(_meeting_date_for_job, config, job)
        except (OSError, ValueError, TypeError) as exc:
            job_date = None
            errors_by_id[mid] = str(exc)
        dates[mid] = job_date
        if meeting_ids is None and job_date is not None:
            if since is not None and job_date < since:
                continue
            if until is not None and job_date > until:
                continue
        filtered_jobs.append(job)
    target_jobs = sorted(
        filtered_jobs,
        key=lambda job: (
            dates[str(getattr(job, "meeting_id", ""))] or date.max,
            str(getattr(job, "meeting_id", "")),
        ),
    )
    result.total = len(target_jobs)

    # dry_run 은 컴파일 호출 없이 즉시 종료.
    if dry_run:
        logger.info(
            "백필 dry_run: 대상 회의 %d건 (since=%s, until=%s)",
            result.total,
            since,
            until,
        )
        result.duration_seconds = time.time() - start_ts
        return result

    from core.meeting_mutation import MeetingMutationCoordinator
    from core.thermal_manager import ThermalManager

    coordinator = meeting_mutation_coordinator or MeetingMutationCoordinator()
    thermal_manager = thermal_manager or ThermalManager(config)

    # 첫 회의의 긴 추론·쿨다운 전부터 확정된 대상 수를 화면에 알린다.
    if target_jobs and progress_callback is not None:
        try:
            progress_callback(0, result.total, target_jobs[0].meeting_id)
        except Exception as cb_exc:  # noqa: BLE001 — 콜백 실패 격리.
            logger.warning("progress_callback 실패: %r", cb_exc)

    # ── 3. 회의별 순차 처리 ──────────────────────────────────────────
    for idx, job in enumerate(target_jobs, start=1):
        # 취소 신호 — 다음 회의는 처리하지 않음.
        if cancel_event is not None and cancel_event.is_set():
            logger.info("백필 취소 — 처리 중단 (processed=%d)", idx - 1)
            break

        meeting_id = getattr(job, "meeting_id", "")
        if not meeting_id:
            result.failed += 1
            result.errors.append(
                BackfillError(
                    meeting_id="(empty)",
                    error_type="invalid_job",
                    message="job.meeting_id 가 비어있음",
                )
            )
            continue

        # ── 3a. source read부터 최종 Wiki 게시까지 같은 회의 lease ─────
        try:
            meeting_date = dates.get(meeting_id)
            if meeting_date is None:
                raise BackfillAdmissionError(
                    "meeting_date_unknown",
                    errors_by_id.get(meeting_id, "회의 날짜를 확인할 수 없습니다."),
                )
            compile_result = await _run_backfill_job(
                config=config,
                job_queue=job_queue,
                job=job,
                expected_date=meeting_date,
                coordinator=coordinator,
                thermal_manager=thermal_manager,
                cancel_event=cancel_event,
            )
            published = _compile_page_count(compile_result, "pages_created", "pages_updated")
            pending = _compile_page_count(compile_result, "pages_pending")
            rejected = _compile_page_count(compile_result, "pages_rejected")
            result.published_pages += published
            result.pending_pages += pending
            result.rejected_pages += rejected
            if published:
                result.succeeded += 1
            elif pending:
                result.pending += 1
                result.errors.append(
                    BackfillError(
                        meeting_id,
                        "review_required",
                        "공개 가능한 페이지가 없어 검토 후보만 보존했습니다.",
                    )
                )
            else:
                result.failed += 1
                result.empty += 1
                result.errors.append(
                    BackfillError(
                        meeting_id,
                        "empty_compile",
                        "공개 페이지와 검토 후보가 모두 생성되지 않았습니다.",
                    )
                )
        except BackfillAdmissionError as exc:
            if exc.code == "cancelled":
                break
            result.failed += 1
            result.errors.append(BackfillError(meeting_id, exc.code, str(exc)))
        except Exception as exc:  # noqa: BLE001 — 격리 정책.
            result.failed += 1
            result.errors.append(
                BackfillError(
                    meeting_id=meeting_id,
                    error_type="wiki_compile_failed",
                    message=f"위키 컴파일 실패: {exc}",
                    traceback=tb_mod.format_exc(),
                )
            )
            logger.warning(
                "백필 실패 (격리 — 다음 진행): meeting=%s, err=%r",
                meeting_id,
                exc,
            )

        # ── 3c. 진행 콜백 ────────────────────────────────────────────
        if progress_callback is not None:
            try:
                progress_callback(idx, result.total, meeting_id)
            except Exception as cb_exc:  # noqa: BLE001 — 콜백 실패 격리.
                logger.warning("progress_callback 실패: %r", cb_exc)

    result.duration_seconds = time.time() - start_ts
    return result


# ─── CLI 진입점 ────────────────────────────────────────────────────────


def _parse_iso_date(value: str) -> date:
    """argparse 용 ISO 날짜 파서."""
    try:
        return datetime.fromisoformat(value).date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"날짜 형식 오류 (YYYY-MM-DD 사용): {value}") from exc


def main() -> int:
    """CLI 진입점.

    Returns:
        프로세스 종료 코드 (성공 0, 실패 시 errors 개수 반환 — 0~255 클램프).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="기존 회의를 일괄 위키화한다 (LLM Wiki Phase 4.E)."
    )
    parser.add_argument(
        "--since",
        type=_parse_iso_date,
        default=None,
        help="시작일 (포함, YYYY-MM-DD).",
    )
    parser.add_argument(
        "--until",
        type=_parse_iso_date,
        default=None,
        help="종료일 (포함, YYYY-MM-DD).",
    )
    parser.add_argument(
        "--meeting-id",
        action="append",
        default=None,
        help="명시적 회의 ID (반복 지정 가능).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 컴파일 없이 대상 회의 목록만 출력.",
    )
    args = parser.parse_args()

    # AppConfig 로드 (지연 import — 모듈 import 부담 0).
    from config import load_config
    from core.job_queue import JobQueue

    config = load_config()

    # JobQueue 초기화.
    db_path = config.paths.resolved_pipeline_db
    job_queue = JobQueue(db_path, max_retries=config.pipeline.retry_max_count)
    job_queue.initialize()

    # 진행 표시 — 단순 stdout.
    def _progress(processed: int, total: int, current: str) -> None:
        sys.stdout.write(f"\r백필 진행: {processed}/{total} — {current}")
        sys.stdout.flush()

    try:
        result = asyncio.run(
            backfill(
                config=config,
                job_queue=job_queue,
                since=args.since,
                until=args.until,
                meeting_ids=args.meeting_id,
                dry_run=args.dry_run,
                progress_callback=_progress,
            )
        )
    finally:
        job_queue.close()

    # 결과 출력.
    sys.stdout.write("\n")
    print(
        f"\n=== 백필 결과 ===\n"
        f"총 대상: {result.total}건\n"
        f"성공: {result.succeeded}건\n"
        f"검토 후보만 생성: {result.pending}건\n"
        f"공개 페이지: {result.published_pages}건 · 검토 후보: {result.pending_pages}건 · 거부 후보: {result.rejected_pages}건\n"
        f"건너뜀: {result.skipped}건\n"
        f"실패: {result.failed}건\n"
        f"  그중 공개·검토 결과 없음: {result.empty}건\n"
        f"경과: {result.duration_seconds:.1f}초\n"
    )

    if result.errors:
        print("\n--- 실패 회의 ---")
        for err in result.errors[:20]:
            print(f"  - {err.meeting_id}: [{err.error_type}] {err.message}")
        if len(result.errors) > 20:
            print(f"  ... 외 {len(result.errors) - 20}건")

    return min(result.failed, 255)


if __name__ == "__main__":
    raise SystemExit(main())
