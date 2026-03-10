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

        logger.info(f"JobProcessor 초기화: poll_interval={poll_interval}초")

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

            Args:
                step_name: 단계 이름
            """
            mapped_status = STEP_TO_STATUS.get(step_name)
            if mapped_status:
                await self._update_job_status_safe(job_id, mapped_status)
                await self._broadcast_event(
                    "pipeline_status",
                    {"job_id": job_id, "step": step_name, "status": mapped_status},
                )

        try:
            # 파이프라인 실행 (LLM 단계는 온디맨드로 실행하므로 스킵)
            result = await self._pipeline.run(
                Path(audio_path),
                meeting_id=meeting_id,
                on_step_start=on_step_start,
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
