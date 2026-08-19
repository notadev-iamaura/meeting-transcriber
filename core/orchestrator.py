"""
오케스트레이터 모듈 (Orchestrator Module)

목적: 작업 큐를 폴링하여 파이프라인을 순차 실행하는 조율 레이어.
주요 기능:
    - 작업 큐에서 대기 중인 작업을 주기적으로 폴링
    - 파이프라인 실행 및 작업 상태 업데이트
    - 서멀 매니저와 연동한 쿨다운 관리
    - WebSocket을 통한 실시간 이벤트 브로드캐스트
의존성: core.job_queue, core.pipeline, core.thermal_manager, api.websocket
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from core.io_utils import atomic_write_json
from core.job_queue import (
    JobStatus,
    RetranscribeClaim,
    cleanup_retranscribe_staging,
    parse_audio_rejection_claim,
    parse_retranscribe_claim,
    rollback_retranscribe_staging,
)
from core.perf_stats import PerfStats
from core.pipeline import InvalidInputError, PipelineManager

logger = logging.getLogger(__name__)


class JobProcessor:
    """작업 큐를 폴링하여 파이프라인을 실행하는 프로세서.

    일정 간격으로 작업 큐를 확인하고, 대기 중인 작업이 있으면
    파이프라인을 실행하여 처리한다. 서멀 매니저로 과열을 방지하고,
    WebSocket으로 상태 변화를 실시간 전달한다.

    Args:
        job_queue: 비동기 작업 큐
        pipeline: 파이프라인 매니저
        thermal_manager: 서멀 매니저
        ws_manager: WebSocket 연결 매니저 (선택)
        poll_interval: 폴링 주기 (초, 기본값: 5.0)
    """

    def __init__(
        self,
        job_queue: Any,
        pipeline: Any,
        thermal_manager: Any,
        ws_manager: Any | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        """JobProcessor를 초기화한다.

        Args:
            job_queue: 비동기 작업 큐 (AsyncJobQueue)
            pipeline: 파이프라인 매니저 (PipelineManager)
            thermal_manager: 서멀 매니저 (ThermalManager)
            ws_manager: WebSocket 연결 매니저 (ConnectionManager, 선택)
            poll_interval: 폴링 주기 (초)
        """
        self._job_queue = job_queue
        self._pipeline = pipeline
        self._thermal_manager = thermal_manager
        self._ws_manager = ws_manager
        self._poll_interval = poll_interval
        self._running = False
        self._task: asyncio.Task[None] | None = None

        # 사용자 취소 요청 집합. API 가 meeting_id 를 추가하면
        # _process_job 의 on_step_start 콜백이 단계 경계에서 감지하여
        # asyncio.CancelledError 를 발생시키고 작업을 recorded 로 되돌린다.
        self._cancellation_requests: set[str] = set()

        # 단계별 성능 통계 (EMA) — ETA 예측 및 이상 탐지용
        self._perf_stats: PerfStats | None
        try:
            self._perf_stats = PerfStats.load()
        except Exception as e:
            logger.warning(f"perf_stats 초기화 실패 (예측 비활성화): {e}")
            self._perf_stats = None

        logger.info(f"JobProcessor 초기화: poll_interval={poll_interval}초")

    def request_cancellation(self, meeting_id: str) -> None:
        """진행 중인 회의에 대해 취소 요청을 등록한다.

        다음 파이프라인 단계 경계(`on_step_start`)에서 감지되어
        `asyncio.CancelledError` 가 발생하고, `_process_job` 에서
        잡아 작업을 `recorded` 상태로 되돌린다.

        주의: 이 메서드는 즉시 작업을 중단시키지 않는다. 현재 실행 중인
        단계(예: 전사)가 끝난 뒤 다음 단계 시작 직전에 취소된다.

        Args:
            meeting_id: 취소할 회의 ID
        """
        self._cancellation_requests.add(meeting_id)
        logger.info(f"취소 요청 등록: meeting_id={meeting_id}")

    def is_cancellation_requested(self, meeting_id: str) -> bool:
        """해당 회의에 대해 취소 요청이 있는지 확인한다."""
        return meeting_id in self._cancellation_requests

    @property
    def is_running(self) -> bool:
        """프로세서 실행 중 여부를 반환한다."""
        return self._running

    async def start(self) -> None:
        """작업 루프를 시작한다.

        이미 실행 중이면 무시한다.
        시작 전에 진행 중 상태에 남아 있는 orphaned 작업들을 queued로 복구한다.
        백그라운드 태스크로 _run_loop를 실행한다.
        """
        if self._running:
            logger.warning("JobProcessor가 이미 실행 중입니다.")
            return

        await self._recover_orphaned_jobs()
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("JobProcessor 시작")

    async def _recover_orphaned_jobs(self) -> None:
        """앱 비정상 종료로 진행 중 상태에 남은 작업을 queued로 복구한다.

        앱이 죽으면 transcribing/diarizing/merging/embedding 등 진행 상태 작업이
        rollback 되지 않아 영구 stuck 된다. 시작 시 이런 orphaned 작업을
        force_set_status로 queued로 되돌려 재처리를 가능하게 한다.
        개별 작업 복구가 실패해도 다른 작업의 복구는 계속 진행한다.
        """
        # 진행 중으로 간주되는 상태들 (JobStatus 가 str 을 상속하므로 값 비교 가능)
        in_progress_statuses = {
            JobStatus.TRANSCRIBING.value,
            JobStatus.DIARIZING.value,
            JobStatus.MERGING.value,
            JobStatus.EMBEDDING.value,
        }
        try:
            all_jobs = await self._job_queue.get_all_jobs()
        except Exception as e:
            logger.error(f"orphaned 작업 조회 실패: {e}")
            return

        # watcher가 복구할 audio-rejection claim은 recording 상태를 durable
        # transaction lock으로 사용한다. watcher가 비활성/실패한 startup에서도
        # generic orphan 복구가 이 marker를 queued/recorded로 풀지 않도록 명시한다.
        audio_rejection_claim_ids: set[int] = set()
        for job in all_jobs:
            if job.status != JobStatus.RECORDING.value:
                continue
            audio_rejection_claim = parse_audio_rejection_claim(
                str(getattr(job, "requested_action", ""))
            )
            if audio_rejection_claim is None:
                continue
            audio_rejection_claim_ids.add(job.id)
            logger.warning(
                "중단된 audio rejection claim 보존: job_id=%s, meeting_id=%s, token=%s",
                job.id,
                job.meeting_id,
                audio_rejection_claim.token,
            )

        # 재전사 claim은 일반 recording과 달리 원래 completed/failed 상태와
        # staging 위치를 payload에 보존한다. 파일을 먼저 원복하고 token CAS로
        # DB를 되돌려야 crash 시 산출물/상태가 함께 유실되지 않는다.
        retranscribe_claims: list[tuple[Any, RetranscribeClaim]] = []
        for job in all_jobs:
            if job.status != JobStatus.RECORDING.value:
                continue
            claim = parse_retranscribe_claim(str(getattr(job, "requested_action", "")))
            if claim is not None:
                retranscribe_claims.append((job, claim))

        for job, claim in retranscribe_claims:
            try:
                config = getattr(self._pipeline, "_config", None)
                if config is None:
                    raise RuntimeError("파이프라인 설정을 찾을 수 없습니다")
                checkpoints_root = self._configured_storage_root(
                    config,
                    "checkpoints_dir",
                    config.paths.resolved_checkpoints_dir,
                )
                outputs_root = self._configured_storage_root(
                    config,
                    "outputs_dir",
                    config.paths.resolved_outputs_dir,
                )
                if claim.phase == "committing":
                    await asyncio.to_thread(
                        cleanup_retranscribe_staging,
                        checkpoints_root,
                        outputs_root,
                        job.meeting_id,
                        claim.token,
                    )
                    await asyncio.to_thread(
                        self._job_queue.queue.reset_for_retranscribe,
                        job.id,
                        claim.token,
                    )
                    logger.warning(
                        "중단된 재전사 commit 완료: meeting_id=%s, token=%s",
                        job.meeting_id,
                        claim.token,
                    )
                    continue
                await asyncio.to_thread(
                    rollback_retranscribe_staging,
                    checkpoints_root,
                    outputs_root,
                    job.meeting_id,
                    claim.token,
                )
                if claim.phase == "purging":
                    await asyncio.to_thread(
                        self._write_retranscribe_recovery_marker,
                        checkpoints_root,
                        job.meeting_id,
                        claim,
                    )
                await asyncio.to_thread(
                    self._job_queue.queue.restore_retranscribe_claim,
                    job.id,
                    claim.token,
                )
                logger.warning(
                    "중단된 재전사 claim 복구: meeting_id=%s, phase=%s, status=%s",
                    job.meeting_id,
                    claim.phase,
                    claim.original_status,
                )
            except Exception as e:
                # 파일 원복/marker가 완결되지 않았다면 recording claim을 그대로
                # 두어 다음 startup에서 다시 복구한다. queued로 덮어쓰지 않는다.
                logger.error(
                    "중단된 재전사 claim 복구 실패: job_id=%s, meeting_id=%s, phase=%s, error=%s",
                    job.id,
                    job.meeting_id,
                    claim.phase,
                    e,
                )

        orphaned = [
            job
            for job in all_jobs
            if job.status in in_progress_statuses and job.id not in audio_rejection_claim_ids
        ]
        if not orphaned:
            return

        recovered = 0
        for job in orphaned:
            try:
                # VALID_TRANSITIONS 에 역방향 전이가 없으므로 force_set_status 사용
                await asyncio.to_thread(
                    self._job_queue.queue.force_set_status,
                    job.id,
                    JobStatus.QUEUED,
                    error_message="",
                )
                logger.warning(f"orphaned 작업 복구: {job.meeting_id} ({job.status} → queued)")
                recovered += 1
            except Exception as e:
                logger.error(
                    f"orphaned 작업 복구 실패: job_id={job.id}, "
                    f"meeting_id={job.meeting_id}, status={job.status}, error={e}"
                )
        logger.info(f"orphaned 작업 복구 완료: {recovered}/{len(orphaned)}건")

    @staticmethod
    def _configured_storage_root(config: Any, field_name: str, fallback: Path) -> Path:
        """resolve()가 숨긴 base symlink를 보존한 lexical storage root를 반환한다."""
        raw_base = getattr(config.paths, "base_dir", None)
        raw_child = getattr(config.paths, field_name, None)
        if isinstance(raw_base, (str, Path)) and isinstance(raw_child, (str, Path)):
            lexical_base = Path(raw_base).expanduser().absolute()
            child = Path(raw_child).expanduser()
            if child == Path(".") or ".." in child.parts or "\x00" in str(child):
                raise ValueError(
                    f"{field_name}은 base_dir 하위 상대경로여야 합니다: {raw_child!r}"
                )
            candidate = (
                child.absolute() if child.is_absolute() else (lexical_base / child).absolute()
            )
            try:
                relative = candidate.relative_to(lexical_base)
            except ValueError as exc:
                raise ValueError(f"{field_name}이 base_dir 밖을 가리킵니다: {candidate}") from exc
            if not relative.parts:
                raise ValueError(f"{field_name}은 base_dir 하위 경로여야 합니다")
            return candidate
        return Path(fallback).expanduser().absolute()

    def _write_retranscribe_recovery_marker(
        self,
        checkpoints_root: Path,
        meeting_id: str,
        claim: RetranscribeClaim,
    ) -> None:
        """purge 진입 후 crash한 회의에 재색인 필요 marker를 원자 기록한다."""
        PipelineManager._validate_meeting_id(meeting_id)
        lexical_root = PipelineManager._validate_storage_directory(
            checkpoints_root,
            label="retranscribe recovery checkpoint root",
        )
        meeting_dir = lexical_root / meeting_id
        if meeting_dir.parent != lexical_root:
            raise InvalidInputError(f"유효하지 않은 회의 ID입니다: {meeting_id!r}")
        PipelineManager._validate_storage_directory(
            meeting_dir,
            label="retranscribe recovery meeting",
        )
        marker_path = PipelineManager._validate_storage_artifact(
            meeting_dir / "reindex_required.json",
            label="retranscribe recovery marker",
        )
        atomic_write_json(
            marker_path,
            {
                "meeting_id": meeting_id,
                "reason": "재전사 index purge 도중 앱이 종료되어 재색인이 필요합니다.",
                "claim_token": claim.token,
                "claim_phase": claim.phase,
                "created_at": datetime.now().isoformat(),
                "recommended_action": f"POST /api/meetings/{meeting_id}/reindex",
            },
            backup=False,
        )

    async def stop(self) -> None:
        """작업 루프를 중지한다.

        실행 중이 아니면 무시한다.
        백그라운드 태스크를 취소하고 완료를 대기한다.
        """
        if not self._running:
            return

        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("JobProcessor 중지")

    async def _run_loop(self) -> None:
        """작업 큐를 주기적으로 폴링하는 메인 루프.

        _running이 False가 되거나 태스크가 취소되면 종료한다.
        각 사이클: 작업 조회 → 작업 처리 → 대기 반복.
        """
        logger.info("작업 루프 시작")
        try:
            while self._running:
                try:
                    job = await self._get_next_job()
                    if job is not None:
                        await self._process_job(job)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"작업 루프 사이클 에러: {e}")

                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            logger.info("작업 루프 취소됨")
            raise

    async def _get_next_job(self) -> Any | None:
        """큐에서 다음 대기 작업을 가져온다.

        Returns:
            대기 중인 첫 번째 Job 또는 None
        """
        try:
            pending = await self._job_queue.get_pending_jobs()
            if pending:
                return pending[0]
            return None
        except Exception as e:
            logger.error(f"작업 큐 조회 실패: {e}")
            return None

    async def _update_job_status_safe(
        self,
        job_id: int,
        status: str,
        error_message: str = "",
    ) -> None:
        """작업 상태를 안전하게 업데이트한다.

        업데이트 실패 시 예외를 전파하지 않고 로그만 남긴다.

        Args:
            job_id: 작업 ID
            status: 새 상태 문자열
            error_message: 에러 메시지 (기본값: "")
        """
        try:
            job_status = JobStatus(status) if isinstance(status, str) else status
            await self._job_queue.update_status(
                job_id,
                job_status,
                error_message=error_message,
            )
        except Exception as e:
            logger.error(f"작업 상태 업데이트 실패: job_id={job_id}, status={status}, error={e}")

    async def _mark_job_completed_after_pipeline(
        self,
        job_id: int,
        meeting_id: str,
    ) -> bool:
        """파이프라인 완료 후 작업 큐 상태를 completed 로 확정한다.

        일반 상태 전이(update_status)가 실패하면 pipeline_state 와 DB 상태가 갈라질 수
        있으므로, 완료 처리에서는 복구 전용 강제 업데이트를 한 번 더 시도한다.
        """
        try:
            await self._job_queue.update_status(job_id, JobStatus.COMPLETED)
            return True
        except Exception as exc:
            logger.error(
                "작업 완료 상태 업데이트 실패: job_id=%s, meeting_id=%s, error=%s",
                job_id,
                meeting_id,
                exc,
            )

        raw_queue = getattr(self._job_queue, "queue", None)
        force_set_status = getattr(raw_queue, "force_set_status", None)
        if raw_queue is None or not callable(force_set_status):
            logger.error(
                "작업 완료 상태 복구 불가: force_set_status 없음 job_id=%s, meeting_id=%s",
                job_id,
                meeting_id,
            )
            return False

        try:
            await asyncio.to_thread(
                force_set_status,
                job_id,
                JobStatus.COMPLETED,
                "",
            )
            logger.warning(
                "작업 완료 상태 강제 복구: job_id=%s, meeting_id=%s",
                job_id,
                meeting_id,
            )
            return True
        except Exception as exc:
            logger.error(
                "작업 완료 상태 강제 복구 실패: job_id=%s, meeting_id=%s, error=%s",
                job_id,
                meeting_id,
                exc,
            )
            return False

    def _resolve_step_model_id(self, step: str) -> str:
        """단계에 해당하는 활성 모델 ID를 반환한다.

        - transcribe: STT 모델명 (HF repo ID 의 마지막 segment 또는 원본)
        - correct / summarize: LLM 모델명
        - 나머지 단계: "default"

        perf_stats 의 by_model 기본값 키와 일치하도록 단순화한다.
        """
        try:
            config = getattr(self._pipeline, "_config", None)
            if config is None:
                return "default"

            if step == "transcribe":
                stt_name = getattr(getattr(config, "stt", None), "model_name", "") or ""
                # HF repo ID 에서 슬러그 추출: "youngouk/seastar-medium-ko-4bit-mlx" → 마지막 segment
                # 단, perf_baseline.json 의 by_model 키와 매칭되도록 간단한 변환 사용
                if "seastar" in stt_name:
                    return "seastar-medium-4bit"
                if "ghost613" in stt_name:
                    return "ghost613-turbo-4bit"
                if "komixv2" in stt_name or "komix" in stt_name:
                    return "komixv2"
                return stt_name.split("/")[-1] if stt_name else "default"

            if step in ("correct", "summarize"):
                llm_cfg = getattr(config, "llm", None)
                if llm_cfg is None:
                    return "default"
                backend = getattr(llm_cfg, "backend", "mlx")
                if backend == "mlx":
                    return getattr(llm_cfg, "mlx_model_name", "default") or "default"
                return getattr(llm_cfg, "model_name", "default") or "default"
        except Exception:
            pass
        return "default"

    async def _broadcast_event(self, event_type: str, data: dict[str, Any]) -> None:
        """WebSocket으로 이벤트를 브로드캐스트한다.

        ws_manager가 없거나 전송 실패 시 예외를 전파하지 않는다.

        Args:
            event_type: 이벤트 타입 문자열
            data: 이벤트 데이터 딕셔너리
        """
        if self._ws_manager is None:
            return

        try:
            from api.websocket import WebSocketEvent

            event = WebSocketEvent(event_type=event_type, data=data)
            await self._ws_manager.broadcast_event(event)
        except Exception as e:
            logger.warning(f"이벤트 브로드캐스트 실패: {event_type}, error={e}")

    async def _process_job(self, job: Any) -> None:
        """단일 작업을 처리한다.

        서멀 대기 → 상태 업데이트 → 파이프라인 실행 → 결과 처리 순서로 진행한다.
        파이프라인 실행 중 on_step_start 콜백으로 단계별 상태를 업데이트한다.

        Args:
            job: 처리할 Job 객체
        """
        from pathlib import Path

        job_id = job.id
        meeting_id = job.meeting_id
        audio_path = job.audio_path
        requested_action = str(getattr(job, "requested_action", "") or "")
        skip_llm_steps_override: bool | None
        if requested_action == "transcribe":
            skip_llm_steps_override = True
        elif requested_action == "full":
            skip_llm_steps_override = False
        else:
            skip_llm_steps_override = None

        logger.info(f"작업 처리 시작: job_id={job_id}, meeting_id={meeting_id}")

        # 서멀 대기
        await self._thermal_manager.wait_if_needed()
        await self._thermal_manager.notify_job_started()

        # 초기 상태 업데이트 (transcribing)
        await self._update_job_status_safe(job_id, "transcribing")

        # 파이프라인 단계별 상태 업데이트 콜백
        async def on_step_start(step_name: str) -> None:
            """파이프라인 단계 시작 시 호출되는 콜백.

            사용자 취소 요청이 있으면 단계 경계에서 CancelledError 를 발생시켜
            파이프라인을 중단시킨다.

            Args:
                step_name: 단계 이름

            Raises:
                asyncio.CancelledError: 사용자가 이 회의에 대해 취소를 요청한 경우
            """
            if meeting_id in self._cancellation_requests:
                logger.info(
                    f"취소 감지: meeting_id={meeting_id}, step={step_name} → CancelledError 발생"
                )
                raise asyncio.CancelledError(f"사용자 취소: {meeting_id}")

            mapped_status = STEP_TO_STATUS.get(step_name)
            if mapped_status:
                await self._update_job_status_safe(job_id, mapped_status)
                await self._broadcast_event(
                    "pipeline_status",
                    {"job_id": job_id, "step": step_name, "status": mapped_status},
                )

        async def on_step_progress(evt: dict[str, Any]) -> None:
            """단계 시작/완료 시 ETA 예측과 EMA 업데이트를 수행하고 브로드캐스트한다.

            `evt` 는 pipeline.run() 이 전달하는 dict:
              - phase: "start" | "complete"
              - step: 단계명
              - input_size: 입력 크기 (단계별 단위)
              - elapsed: (complete 시) 실제 소요 시간
            """
            if self._perf_stats is None:
                return
            try:
                phase = evt.get("phase", "")
                step = evt.get("step", "")
                input_size = float(evt.get("input_size") or 0.0)
                model_id = self._resolve_step_model_id(step)

                payload: dict[str, Any] = {
                    "job_id": job_id,
                    "meeting_id": meeting_id,
                    "step": step,
                    "phase": phase,
                    "input_size": input_size,
                    "model_id": model_id,
                }

                if phase == "start":
                    eta = self._perf_stats.predict(step, model_id=model_id, input_size=input_size)
                    payload["eta_seconds"] = eta
                    payload["anomaly"] = "normal"
                elif phase == "complete":
                    elapsed = float(evt.get("elapsed") or 0.0)
                    # EMA 업데이트
                    self._perf_stats.update(
                        step,
                        model_id=model_id,
                        input_size=input_size,
                        elapsed=elapsed,
                    )
                    self._perf_stats.save()
                    # 완료 시점의 이상 탐지 (사후 기록용)
                    eta = self._perf_stats.predict(step, model_id=model_id, input_size=input_size)
                    payload["eta_seconds"] = eta
                    payload["elapsed_seconds"] = elapsed
                    payload["anomaly"] = self._perf_stats.classify_anomaly(
                        elapsed=elapsed, eta=eta
                    )

                await self._broadcast_event("step_progress", payload)
            except Exception as e:
                logger.debug(f"step_progress 처리 실패 (무시): {e}")

        try:
            # 파이프라인 실행: batch 가 명시한 실행 의도는 우선하고,
            # 빈 값이면 pipeline.run 내부에서 config.pipeline.skip_llm_steps 를 사용한다.
            await self._pipeline.run(
                Path(audio_path),
                meeting_id=meeting_id,
                on_step_start=on_step_start,
                on_step_progress=on_step_progress,
                skip_llm_steps=skip_llm_steps_override,
            )

            # 완료 상태 업데이트. 실패 시 복구 경로를 통해 pipeline_state 와 DB 상태를 맞춘다.
            status_recovered = await self._mark_job_completed_after_pipeline(job_id, meeting_id)
            await self._thermal_manager.notify_job_completed()
            if not status_recovered:
                await self._broadcast_event(
                    "job_state_inconsistent",
                    {
                        "job_id": job_id,
                        "meeting_id": meeting_id,
                        "pipeline_status": "completed",
                        "job_status": "unknown",
                    },
                )
            await self._broadcast_event(
                "job_completed",
                {"job_id": job_id, "meeting_id": meeting_id, "status": "completed"},
            )

            logger.info(f"작업 처리 완료: job_id={job_id}")

        except asyncio.CancelledError:
            is_user_cancel = meeting_id in self._cancellation_requests
            self._cancellation_requests.discard(meeting_id)

            if is_user_cancel:
                try:
                    # JobStatus 전이 규칙 우회: 직접 강제 업데이트
                    await asyncio.to_thread(
                        self._job_queue.queue.force_set_status,
                        job_id,
                        JobStatus.RECORDED,
                        "사용자가 취소함",
                    )
                except Exception as exc:
                    logger.error(f"취소 후 상태 복귀 실패: job_id={job_id}, error={exc}")
                await self._thermal_manager.notify_job_completed()
                await self._broadcast_event(
                    "job_cancelled",
                    {"job_id": job_id, "meeting_id": meeting_id, "status": "recorded"},
                )
                logger.info(f"작업 취소 완료: job_id={job_id}, meeting_id={meeting_id}")
                # 사용자 취소는 작업 루프를 계속 동작시켜야 하므로 재전파하지 않는다.
                return

            try:
                await asyncio.to_thread(
                    self._job_queue.queue.force_set_status,
                    job_id,
                    JobStatus.QUEUED,
                    "앱 종료로 작업이 중단되어 재시도 대기 중입니다.",
                )
            except Exception as exc:
                logger.error(f"종료 중 작업 재대기 실패: job_id={job_id}, error={exc}")
            await self._thermal_manager.notify_job_completed()
            await self._broadcast_event(
                "job_interrupted",
                {"job_id": job_id, "meeting_id": meeting_id, "status": "queued"},
            )
            logger.info(f"작업 종료 중단 처리: job_id={job_id}, meeting_id={meeting_id}")
            raise

        except InvalidInputError as e:
            try:
                await asyncio.to_thread(
                    self._job_queue.queue.force_set_status,
                    job_id,
                    JobStatus.RECORDED,
                    "",
                )
            except Exception as status_exc:
                logger.error(
                    f"입력 품질 보류 후 상태 복귀 실패: job_id={job_id}, error={status_exc}"
                )
            await self._thermal_manager.notify_job_completed()
            logger.info(
                f"입력 품질 검증 비수락으로 작업 보류: "
                f"job_id={job_id}, meeting_id={meeting_id}, reason={e}"
            )

        except Exception as e:
            # 실패 상태 업데이트
            error_msg = str(e)
            await self._update_job_status_safe(
                job_id,
                "failed",
                error_message=error_msg,
            )
            await self._thermal_manager.notify_job_completed()
            await self._broadcast_event(
                "job_failed",
                {"job_id": job_id, "meeting_id": meeting_id, "error": error_msg},
            )

            logger.error(f"작업 처리 실패: job_id={job_id}, error={e}")


# === 파이프라인 단계 → 작업 상태 매핑 ===


STEP_TO_STATUS: dict[str, str] = {
    "convert": "transcribing",
    "transcribe": "transcribing",
    "diarize": "diarizing",
    "merge": "merging",
    "correct": "embedding",
    "summarize": "embedding",
}
