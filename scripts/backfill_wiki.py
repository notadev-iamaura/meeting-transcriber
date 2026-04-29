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
    - 1차: ``core.job_queue.JobQueue.get_all_jobs()`` — meeting_id, created_at.
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
import sys
import time
import traceback as tb_mod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

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
        errors: BackfillError 리스트.
        duration_seconds: 전체 경과 시간.
        compiled_at: ISO8601 시작 시각.
    """

    total: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[BackfillError] = field(default_factory=list)
    duration_seconds: float = 0.0
    compiled_at: str = ""


# ─── 회의 데이터 로딩 헬퍼 ──────────────────────────────────────────────


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
        utterance dict 의 리스트. 파일이 없거나 읽기 실패 시 None.
    """
    outputs_dir = config.paths.resolved_outputs_dir
    checkpoints_dir = config.paths.resolved_checkpoints_dir

    candidates = [
        outputs_dir / meeting_id / "corrected.json",
        checkpoints_dir / meeting_id / "correct.json",
        checkpoints_dir / meeting_id / "merge.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("백필: utterances 파일 로드 실패 — %s (%r)", path, exc)
            continue
        utterances = data.get("utterances", [])
        if utterances:
            return utterances
    return None


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
    outputs_dir = config.paths.resolved_outputs_dir
    checkpoints_dir = config.paths.resolved_checkpoints_dir

    md_candidates = [
        outputs_dir / meeting_id / "summary.md",
        outputs_dir / meeting_id / "meeting_minutes.md",
    ]
    for path in md_candidates:
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("백필: summary md 읽기 실패 — %s (%r)", path, exc)
                continue

    json_candidates = [
        outputs_dir / meeting_id / "summary.json",
        checkpoints_dir / meeting_id / "summarize.json",
    ]
    for path in json_candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("백필: summary json 읽기 실패 — %s (%r)", path, exc)
            continue
        markdown = data.get("markdown")
        if markdown:
            return markdown
    return None


# ─── 회의 목록 필터링 ──────────────────────────────────────────────────


def _parse_job_date(created_at: str) -> date | None:
    """job.created_at (ISO8601 문자열) 을 date 로 파싱한다.

    Args:
        created_at: ISO 형식 문자열 (예: "2026-04-29T10:00:00").

    Returns:
        파싱 성공 시 date 객체. 실패 시 None.
    """
    if not created_at:
        return None
    try:
        return datetime.fromisoformat(created_at).date()
    except (TypeError, ValueError):
        return None


def _filter_jobs(
    jobs: list[Any],
    *,
    since: date | None,
    until: date | None,
    meeting_ids: list[str] | None,
) -> list[Any]:
    """필터 조건에 맞는 job 만 골라낸다.

    Args:
        jobs: JobQueue.get_all_jobs() 결과.
        since: 시작일 (포함).
        until: 종료일 (포함).
        meeting_ids: 명시적 회의 ID 목록 — 지정 시 since/until 무시.

    Returns:
        필터링된 job 리스트.
    """
    if meeting_ids is not None:
        target = set(meeting_ids)
        return [j for j in jobs if j.meeting_id in target]

    if since is None and until is None:
        return list(jobs)

    filtered: list[Any] = []
    for job in jobs:
        job_date = _parse_job_date(getattr(job, "created_at", ""))
        if job_date is None:
            # created_at 없는 회의는 보수적으로 포함 (사용자가 명시 ID 지정 안 한 경우).
            filtered.append(job)
            continue
        if since is not None and job_date < since:
            continue
        if until is not None and job_date > until:
            continue
        filtered.append(job)
    return filtered


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
        meeting_id: 8자리 hex 또는 일반 회의 식별자.
        meeting_date: 회의 날짜.
        summary: 8단계 요약 마크다운.
        utterances: 5단계 보정 발화 리스트.

    Returns:
        WikiCompilerV2.compile_meeting() 결과 (CompileResult).

    Raises:
        Exception: 컴파일 실패 — 호출자가 BackfillError 로 변환.
    """
    # Lazy import — 백필 비활성 환경에서 import 비용 0.
    from core.wiki.store import WikiStore
    from steps.wiki_compiler import _create_wiki_compiler_v2

    wiki_root = config.wiki.resolved_root
    store = WikiStore(wiki_root)
    store.init_repo()

    v2 = _create_wiki_compiler_v2(
        config=config,
        store=store,
        model_manager=None,  # 백필은 ModelLoadManager 없이 직접 LLM 호출.
        utterances=utterances,
        meeting_id=meeting_id,
    )
    return await v2.compile_meeting(
        meeting_id=meeting_id,
        meeting_date=meeting_date,
        summary=summary,
        utterances=utterances,
    )


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

    # ── 1. 회의 목록 조회 (동기 메서드 → 스레드로) ────────────────
    try:
        all_jobs = await asyncio.to_thread(job_queue.get_all_jobs)
    except Exception as exc:  # noqa: BLE001 — 실패해도 빈 결과 반환.
        logger.error("백필: get_all_jobs 실패 — %r", exc)
        result.duration_seconds = time.time() - start_ts
        return result

    # ── 2. 필터링 ───────────────────────────────────────────────────
    target_jobs = _filter_jobs(
        all_jobs,
        since=since,
        until=until,
        meeting_ids=meeting_ids,
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

        # 회의 날짜 결정 — created_at 우선, 없으면 today.
        job_date = _parse_job_date(getattr(job, "created_at", ""))
        meeting_date = job_date or date.today()

        # ── 3a. 데이터 로드 (utterances + summary) ──────────────────
        try:
            utterances = await asyncio.to_thread(_load_utterances, config, meeting_id)
            summary = await asyncio.to_thread(_load_summary, config, meeting_id)
        except Exception as exc:  # noqa: BLE001
            result.failed += 1
            result.errors.append(
                BackfillError(
                    meeting_id=meeting_id,
                    error_type="data_load_failed",
                    message=f"회의 데이터 로드 실패: {exc}",
                    traceback=tb_mod.format_exc(),
                )
            )
            if progress_callback is not None:
                try:
                    progress_callback(idx, result.total, meeting_id)
                except Exception as cb_exc:  # noqa: BLE001 — 콜백 실패 격리.
                    logger.warning("progress_callback 실패: %r", cb_exc)
            continue

        if utterances is None:
            result.failed += 1
            result.errors.append(
                BackfillError(
                    meeting_id=meeting_id,
                    error_type="utterances_missing",
                    message="utterances 파일을 찾을 수 없음 (corrected/correct/merge 모두 부재)",
                )
            )
            if progress_callback is not None:
                try:
                    progress_callback(idx, result.total, meeting_id)
                except Exception as cb_exc:  # noqa: BLE001
                    logger.warning("progress_callback 실패: %r", cb_exc)
            continue

        if not summary:
            result.failed += 1
            result.errors.append(
                BackfillError(
                    meeting_id=meeting_id,
                    error_type="summary_missing",
                    message="요약 파일을 찾을 수 없음 (summary.md/meeting_minutes.md/summarize.json)",
                )
            )
            if progress_callback is not None:
                try:
                    progress_callback(idx, result.total, meeting_id)
                except Exception as cb_exc:  # noqa: BLE001
                    logger.warning("progress_callback 실패: %r", cb_exc)
            continue

        # ── 3b. 컴파일 호출 (회의별 atomic) ──────────────────────────
        try:
            await _compile_single_meeting(
                config=config,
                meeting_id=meeting_id,
                meeting_date=meeting_date,
                summary=summary,
                utterances=utterances,
            )
            result.succeeded += 1
            logger.info(
                "백필 성공: meeting=%s (%d/%d)",
                meeting_id,
                idx,
                result.total,
            )
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
        f"건너뜀: {result.skipped}건\n"
        f"실패: {result.failed}건\n"
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
