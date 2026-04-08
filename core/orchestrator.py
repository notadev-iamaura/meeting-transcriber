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
import logging
from typing import Any

from core.job_queue import JobStatus

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
        try:
            from core.perf_stats import PerfStats

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
        백그라운드 태스크로 _run_loop를 실행한다.
        """
        if self._running:
            logger.warning("JobProcessor가 이미 실행 중입니다.")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("JobProcessor 시작")

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
            try:
                await self._task
            except asyncio.CancelledError:
                pass
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
                job_id, job_status, error_message=error_message,
            )
        except Exception as e:
            logger.error(f"작업 상태 업데이트 실패: job_id={job_id}, status={status}, error={e}")

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
                    eta = self._perf_stats.predict(
                        step, model_id=model_id, input_size=input_size
                    )
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
                    eta = self._perf_stats.predict(
                        step, model_id=model_id, input_size=input_size
                    )
                    payload["eta_seconds"] = eta
                    payload["elapsed_seconds"] = elapsed
                    payload["anomaly"] = self._perf_stats.classify_anomaly(
                        elapsed=elapsed, eta=eta
                    )

                await self._broadcast_event("step_progress", payload)
            except Exception as e:
                logger.debug(f"step_progress 처리 실패 (무시): {e}")

        try:
            # 파이프라인 실행 (LLM 단계는 온디맨드로 실행하므로 스킵)
            result = await self._pipeline.run(
                Path(audio_path),
                meeting_id=meeting_id,
                on_step_start=on_step_start,
                on_step_progress=on_step_progress,
                skip_llm_steps=True,
            )

            # 완료 상태 업데이트
            await self._update_job_status_safe(job_id, "completed")
            await self._thermal_manager.notify_job_completed()
            await self._broadcast_event(
                "job_completed",
                {"job_id": job_id, "meeting_id": meeting_id, "status": "completed"},
            )

            logger.info(f"작업 처리 완료: job_id={job_id}")

        except asyncio.CancelledError:
            # 사용자 취소: recorded 상태로 되돌리고 취소 요청 set 에서 제거
            self._cancellation_requests.discard(meeting_id)
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
            # CancelledError 를 다시 raise 하지 않음 — 작업 루프는 계속 동작해야 함

        except Exception as e:
            # 실패 상태 업데이트
            error_msg = str(e)
            await self._update_job_status_safe(
                job_id, "failed", error_message=error_msg,
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
