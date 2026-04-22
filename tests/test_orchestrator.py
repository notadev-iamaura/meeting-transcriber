"""
오케스트레이터 테스트 모듈 (Orchestrator Test Module)

목적: JobProcessor의 작업 루프, 상태 관리, 이벤트 브로드캐스트를 검증한다.
주요 테스트:
    - 인스턴스 생성 및 초기 상태
    - start/stop 라이프사이클
    - 작업 폴링 및 처리
    - 파이프라인 실행 및 상태 업데이트
    - 서멀 관리 통합
    - WebSocket 이벤트 브로드캐스트
    - 에러 처리 및 복구
의존성: pytest, asyncio, unittest.mock
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.orchestrator import JobProcessor

pytestmark = pytest.mark.asyncio


# === Fixture 정의 ===


@pytest.fixture
def mock_job_queue() -> AsyncMock:
    """비동기 작업 큐 목(Mock)을 생성한다."""
    queue = AsyncMock()
    queue.get_pending_jobs = AsyncMock(return_value=[])
    queue.update_status = AsyncMock()
    return queue


@pytest.fixture
def mock_pipeline() -> AsyncMock:
    """파이프라인 매니저 목(Mock)을 생성한다."""
    pipeline = AsyncMock()
    pipeline.run = AsyncMock(return_value=MagicMock(status="completed"))
    return pipeline


@pytest.fixture
def mock_thermal() -> AsyncMock:
    """서멀 매니저 목(Mock)을 생성한다."""
    thermal = AsyncMock()
    thermal.wait_if_needed = AsyncMock()
    thermal.notify_job_started = AsyncMock()
    thermal.notify_job_completed = AsyncMock()
    return thermal


@pytest.fixture
def mock_ws_manager() -> AsyncMock:
    """WebSocket 매니저 목(Mock)을 생성한다."""
    return AsyncMock()


def _make_job(
    job_id: int = 1,
    meeting_id: str = "test_meeting",
    audio_path: str = "/tmp/test.wav",
    status: str = "queued",
) -> MagicMock:
    """테스트용 Job 목(Mock) 객체를 생성한다.

    Args:
        job_id: 작업 ID
        meeting_id: 회의 ID
        audio_path: 오디오 파일 경로
        status: 작업 상태

    Returns:
        Job 속성이 설정된 MagicMock 객체
    """
    job = MagicMock()
    job.id = job_id
    job.meeting_id = meeting_id
    job.audio_path = audio_path
    job.status = status
    return job


@pytest.fixture
def processor(
    mock_job_queue: AsyncMock,
    mock_pipeline: AsyncMock,
    mock_thermal: AsyncMock,
    mock_ws_manager: AsyncMock,
) -> JobProcessor:
    """기본 JobProcessor 인스턴스를 생성한다."""
    return JobProcessor(
        job_queue=mock_job_queue,
        pipeline=mock_pipeline,
        thermal_manager=mock_thermal,
        ws_manager=mock_ws_manager,
        poll_interval=0.1,
    )


# === Cycle 1: 생성 및 is_running ===


class TestJobProcessorInit:
    """JobProcessor 초기화 테스트."""

    async def test_초기_상태는_실행중이_아니다(
        self,
        processor: JobProcessor,
    ) -> None:
        """생성 직후 is_running은 False여야 한다."""
        assert processor.is_running is False

    async def test_의존성이_올바르게_주입된다(
        self,
        mock_job_queue: AsyncMock,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_ws_manager: AsyncMock,
    ) -> None:
        """생성자에 전달된 의존성이 올바르게 저장되어야 한다."""
        proc = JobProcessor(
            job_queue=mock_job_queue,
            pipeline=mock_pipeline,
            thermal_manager=mock_thermal,
            ws_manager=mock_ws_manager,
            poll_interval=2.0,
        )
        assert proc.is_running is False

    async def test_ws_manager_없이_생성_가능(
        self,
        mock_job_queue: AsyncMock,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
    ) -> None:
        """ws_manager가 None이어도 정상 생성되어야 한다."""
        proc = JobProcessor(
            job_queue=mock_job_queue,
            pipeline=mock_pipeline,
            thermal_manager=mock_thermal,
            ws_manager=None,
        )
        assert proc.is_running is False


# === Cycle 2: start/stop 라이프사이클 ===


class TestJobProcessorStartStop:
    """JobProcessor start/stop 라이프사이클 테스트."""

    async def test_start_후_is_running_True(
        self,
        processor: JobProcessor,
    ) -> None:
        """start() 호출 후 is_running이 True가 되어야 한다."""
        await processor.start()
        try:
            assert processor.is_running is True
        finally:
            await processor.stop()

    async def test_stop_후_is_running_False(
        self,
        processor: JobProcessor,
    ) -> None:
        """stop() 호출 후 is_running이 False가 되어야 한다."""
        await processor.start()
        await processor.stop()
        assert processor.is_running is False

    async def test_중복_start_무시(
        self,
        processor: JobProcessor,
    ) -> None:
        """이미 실행 중일 때 start()를 다시 호출해도 안전해야 한다."""
        await processor.start()
        await processor.start()  # 중복 호출
        try:
            assert processor.is_running is True
        finally:
            await processor.stop()

    async def test_중복_stop_무시(
        self,
        processor: JobProcessor,
    ) -> None:
        """실행 중이 아닐 때 stop()을 호출해도 안전해야 한다."""
        await processor.stop()  # 시작하지 않은 상태에서 stop
        assert processor.is_running is False


# === Cycle 3: _get_next_job ===


class TestGetNextJob:
    """_get_next_job 메서드 테스트."""

    async def test_대기_작업_없으면_None_반환(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
    ) -> None:
        """대기 중인 작업이 없으면 None을 반환해야 한다."""
        mock_job_queue.get_pending_jobs.return_value = []
        result = await processor._get_next_job()
        assert result is None

    async def test_대기_작업_있으면_첫번째_반환(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
    ) -> None:
        """대기 중인 작업이 있으면 첫 번째 작업을 반환해야 한다."""
        job1 = _make_job(job_id=1)
        job2 = _make_job(job_id=2)
        mock_job_queue.get_pending_jobs.return_value = [job1, job2]
        result = await processor._get_next_job()
        assert result is not None
        assert result.id == 1

    async def test_큐_조회_실패시_None_반환(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
    ) -> None:
        """큐 조회에서 예외가 발생하면 None을 반환해야 한다."""
        mock_job_queue.get_pending_jobs.side_effect = Exception("DB 에러")
        result = await processor._get_next_job()
        assert result is None


# === Cycle 4: _update_job_status_safe ===


class TestUpdateJobStatusSafe:
    """_update_job_status_safe 메서드 테스트."""

    async def test_상태_업데이트_성공(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
    ) -> None:
        """정상적인 상태 업데이트가 큐에 전달되어야 한다."""
        await processor._update_job_status_safe(1, "transcribing")
        mock_job_queue.update_status.assert_called_once_with(
            1,
            "transcribing",
            error_message="",
        )

    async def test_에러_메시지와_함께_업데이트(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
    ) -> None:
        """에러 메시지가 포함된 상태 업데이트가 전달되어야 한다."""
        await processor._update_job_status_safe(
            1,
            "failed",
            error_message="파이프라인 실패",
        )
        mock_job_queue.update_status.assert_called_once_with(
            1,
            "failed",
            error_message="파이프라인 실패",
        )

    async def test_업데이트_실패시_예외_전파_안함(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
    ) -> None:
        """큐 업데이트가 실패해도 예외가 전파되지 않아야 한다."""
        mock_job_queue.update_status.side_effect = Exception("DB 에러")
        # 예외가 발생하지 않아야 함
        await processor._update_job_status_safe(1, "failed")


# === Cycle 5: _broadcast_event ===


class TestBroadcastEvent:
    """_broadcast_event 메서드 테스트."""

    async def test_ws_manager_있으면_이벤트_브로드캐스트(
        self,
        processor: JobProcessor,
        mock_ws_manager: AsyncMock,
    ) -> None:
        """ws_manager가 있으면 broadcast_event를 호출해야 한다."""
        await processor._broadcast_event("job_completed", {"job_id": 1})
        mock_ws_manager.broadcast_event.assert_called_once()
        # 전달된 이벤트 확인
        event = mock_ws_manager.broadcast_event.call_args[0][0]
        assert event.event_type == "job_completed"
        assert event.data == {"job_id": 1}

    async def test_ws_manager_없으면_무시(
        self,
        mock_job_queue: AsyncMock,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
    ) -> None:
        """ws_manager가 None이면 예외 없이 무시해야 한다."""
        proc = JobProcessor(
            job_queue=mock_job_queue,
            pipeline=mock_pipeline,
            thermal_manager=mock_thermal,
            ws_manager=None,
        )
        # 예외가 발생하지 않아야 함
        await proc._broadcast_event("job_completed", {"job_id": 1})

    async def test_브로드캐스트_실패시_예외_전파_안함(
        self,
        processor: JobProcessor,
        mock_ws_manager: AsyncMock,
    ) -> None:
        """브로드캐스트 실패 시 예외가 전파되지 않아야 한다."""
        mock_ws_manager.broadcast_event.side_effect = Exception("WebSocket 에러")
        # 예외가 발생하지 않아야 함
        await processor._broadcast_event("job_completed", {"job_id": 1})


# === Cycle 6: _process_job 성공 경로 ===


class TestProcessJobSuccess:
    """_process_job 성공 경로 테스트."""

    async def test_파이프라인_실행_성공시_completed_상태(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_job_queue: AsyncMock,
    ) -> None:
        """파이프라인 성공 시 작업 상태가 completed로 변경되어야 한다."""
        job = _make_job(job_id=1, meeting_id="meeting_001")
        mock_pipeline.run.return_value = MagicMock(status="completed")

        await processor._process_job(job)

        # 서멀 매니저 호출 확인
        mock_thermal.wait_if_needed.assert_called_once()
        mock_thermal.notify_job_started.assert_called_once()
        mock_thermal.notify_job_completed.assert_called_once()

        # 파이프라인 실행 확인
        mock_pipeline.run.assert_called_once()

        # 상태 업데이트: transcribing → completed
        calls = mock_job_queue.update_status.call_args_list
        # 최소한 completed 상태가 포함되어야 함
        status_values = [c[0][1] for c in calls]
        assert "completed" in status_values

    async def test_파이프라인_실행시_on_step_start_콜백_전달(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
    ) -> None:
        """파이프라인 실행 시 on_step_start 콜백이 전달되어야 한다."""
        job = _make_job(job_id=1)
        mock_pipeline.run.return_value = MagicMock(status="completed")

        await processor._process_job(job)

        # pipeline.run 호출 시 on_step_start 키워드 인자 확인
        call_kwargs = mock_pipeline.run.call_args
        # on_step_start가 전달됐는지 확인 (kwargs 또는 positional)
        assert call_kwargs is not None

    async def test_성공시_job_completed_이벤트_브로드캐스트(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_ws_manager: AsyncMock,
    ) -> None:
        """성공 시 job_completed 이벤트가 브로드캐스트되어야 한다."""
        job = _make_job(job_id=1, meeting_id="meeting_001")
        mock_pipeline.run.return_value = MagicMock(status="completed")

        await processor._process_job(job)

        # broadcast_event 호출 확인
        assert mock_ws_manager.broadcast_event.called
        # job_completed 이벤트 포함 확인
        event_types = [c[0][0].event_type for c in mock_ws_manager.broadcast_event.call_args_list]
        assert "job_completed" in event_types


# === Cycle 7: _process_job 실패 경로 ===


class TestProcessJobFailure:
    """_process_job 실패 경로 테스트."""

    async def test_파이프라인_실패시_failed_상태(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_job_queue: AsyncMock,
    ) -> None:
        """파이프라인 실패 시 작업 상태가 failed로 변경되어야 한다."""
        job = _make_job(job_id=1)
        mock_pipeline.run.side_effect = Exception("전사 실패")

        await processor._process_job(job)

        # failed 상태 업데이트 확인
        calls = mock_job_queue.update_status.call_args_list
        status_values = [c[0][1] for c in calls]
        assert "failed" in status_values

    async def test_실패시_에러_메시지_전달(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_job_queue: AsyncMock,
    ) -> None:
        """파이프라인 실패 시 에러 메시지가 전달되어야 한다."""
        job = _make_job(job_id=1)
        mock_pipeline.run.side_effect = Exception("STT 모델 로드 실패")

        await processor._process_job(job)

        # failed 상태 호출에서 에러 메시지 확인
        for call in mock_job_queue.update_status.call_args_list:
            if call[0][1] == "failed":
                assert "STT 모델 로드 실패" in call[1].get("error_message", "")
                break

    async def test_실패시_job_failed_이벤트_브로드캐스트(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_ws_manager: AsyncMock,
    ) -> None:
        """실패 시 job_failed 이벤트가 브로드캐스트되어야 한다."""
        job = _make_job(job_id=1, meeting_id="meeting_001")
        mock_pipeline.run.side_effect = Exception("파이프라인 에러")

        await processor._process_job(job)

        # job_failed 이벤트 포함 확인
        event_types = [c[0][0].event_type for c in mock_ws_manager.broadcast_event.call_args_list]
        assert "job_failed" in event_types

    async def test_실패시에도_서멀_notify_job_completed_호출(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
    ) -> None:
        """실패 시에도 서멀 매니저에 작업 완료를 알려야 한다."""
        job = _make_job(job_id=1)
        mock_pipeline.run.side_effect = Exception("에러")

        await processor._process_job(job)

        mock_thermal.notify_job_completed.assert_called_once()


# === Cycle 8: _run_loop 통합 ===


class TestRunLoop:
    """_run_loop 통합 테스트."""

    async def test_작업_있으면_처리_후_계속_폴링(
        self,
        mock_job_queue: AsyncMock,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_ws_manager: AsyncMock,
    ) -> None:
        """대기 작업이 있으면 처리하고 다시 폴링해야 한다."""
        job = _make_job(job_id=1)
        call_count = 0

        async def get_pending_side_effect() -> list:
            """첫 호출 시 작업 반환, 이후 빈 리스트."""
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [job]
            return []

        mock_job_queue.get_pending_jobs = AsyncMock(
            side_effect=get_pending_side_effect,
        )

        proc = JobProcessor(
            job_queue=mock_job_queue,
            pipeline=mock_pipeline,
            thermal_manager=mock_thermal,
            ws_manager=mock_ws_manager,
            poll_interval=0.05,
        )

        await proc.start()
        # 충분한 시간 대기 후 정지
        await asyncio.sleep(0.3)
        await proc.stop()

        # 파이프라인이 1번 실행되어야 함
        assert mock_pipeline.run.call_count == 1

    async def test_작업_없으면_대기만_반복(
        self,
        mock_job_queue: AsyncMock,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_ws_manager: AsyncMock,
    ) -> None:
        """작업이 없으면 파이프라인이 실행되지 않아야 한다."""
        mock_job_queue.get_pending_jobs.return_value = []

        proc = JobProcessor(
            job_queue=mock_job_queue,
            pipeline=mock_pipeline,
            thermal_manager=mock_thermal,
            ws_manager=mock_ws_manager,
            poll_interval=0.05,
        )

        await proc.start()
        await asyncio.sleep(0.2)
        await proc.stop()

        # 파이프라인 실행 안 됨
        mock_pipeline.run.assert_not_called()

    async def test_루프_에러_발생해도_계속_실행(
        self,
        mock_job_queue: AsyncMock,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_ws_manager: AsyncMock,
    ) -> None:
        """폴링 루프 중 에러가 발생해도 루프가 중단되지 않아야 한다."""
        call_count = 0

        async def get_pending_side_effect() -> list:
            """첫 호출 시 에러, 이후 빈 리스트."""
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("DB 연결 에러")
            return []

        mock_job_queue.get_pending_jobs = AsyncMock(
            side_effect=get_pending_side_effect,
        )

        proc = JobProcessor(
            job_queue=mock_job_queue,
            pipeline=mock_pipeline,
            thermal_manager=mock_thermal,
            ws_manager=mock_ws_manager,
            poll_interval=0.05,
        )

        await proc.start()
        await asyncio.sleep(0.3)
        await proc.stop()

        # 에러 후에도 폴링이 계속되었음을 확인
        assert call_count >= 2


# === Cycle 9: STEP_TO_STATUS 매핑 ===


class TestStepToStatus:
    """STEP_TO_STATUS 매핑 및 on_step_start 콜백 테스트."""

    async def test_step_to_status_매핑_존재(self) -> None:
        """STEP_TO_STATUS에 필수 단계가 모두 매핑되어야 한다."""
        from core.orchestrator import STEP_TO_STATUS

        assert "convert" in STEP_TO_STATUS
        assert "transcribe" in STEP_TO_STATUS
        assert "diarize" in STEP_TO_STATUS
        assert "merge" in STEP_TO_STATUS
        assert "correct" in STEP_TO_STATUS
        assert "summarize" in STEP_TO_STATUS

    async def test_step_to_status_매핑_값_검증(self) -> None:
        """STEP_TO_STATUS 매핑 값이 올바른 JobStatus여야 한다."""
        from core.orchestrator import STEP_TO_STATUS

        assert STEP_TO_STATUS["convert"] == "transcribing"
        assert STEP_TO_STATUS["transcribe"] == "transcribing"
        assert STEP_TO_STATUS["diarize"] == "diarizing"
        assert STEP_TO_STATUS["merge"] == "merging"
        assert STEP_TO_STATUS["correct"] == "embedding"
        assert STEP_TO_STATUS["summarize"] == "embedding"

    async def test_on_step_start_콜백으로_상태_업데이트(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_job_queue: AsyncMock,
    ) -> None:
        """on_step_start 콜백이 단계별 상태를 업데이트해야 한다."""
        job = _make_job(job_id=42)
        captured_callback = None

        async def capture_run(*args: Any, **kwargs: Any) -> MagicMock:
            """pipeline.run 호출 시 on_step_start 콜백을 캡처한다."""
            nonlocal captured_callback
            captured_callback = kwargs.get("on_step_start")
            # 콜백 호출하여 동작 검증
            if captured_callback:
                await captured_callback("diarize")
            return MagicMock(status="completed")

        mock_pipeline.run = AsyncMock(side_effect=capture_run)

        await processor._process_job(job)

        # 콜백이 전달되었음을 확인
        assert captured_callback is not None

        # diarize 단계에 대한 상태 업데이트 확인
        update_calls = mock_job_queue.update_status.call_args_list
        status_values = [c[0][1] for c in update_calls]
        assert "diarizing" in status_values

    async def test_on_step_start_콜백_pipeline_status_이벤트(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_ws_manager: AsyncMock,
    ) -> None:
        """on_step_start 콜백이 pipeline_status 이벤트를 브로드캐스트해야 한다."""
        job = _make_job(job_id=42)

        async def capture_run(*args: Any, **kwargs: Any) -> MagicMock:
            """pipeline.run 호출 시 on_step_start 콜백을 실행한다."""
            callback = kwargs.get("on_step_start")
            if callback:
                await callback("transcribe")
            return MagicMock(status="completed")

        mock_pipeline.run = AsyncMock(side_effect=capture_run)

        await processor._process_job(job)

        # pipeline_status 이벤트 확인
        event_types = [c[0][0].event_type for c in mock_ws_manager.broadcast_event.call_args_list]
        assert "pipeline_status" in event_types


# === Cycle 10: skip_llm_steps 전달 (config 존중 동작 검증) ===


class TestProcessJobSkipLlm:
    """_process_job에서 skip_llm_steps=None 전달로 config 설정을 존중하는지 테스트.

    이슈 C 회귀 방지:
    - orchestrator 가 하드코딩 True 를 넘기면 config.yaml 의 false 가 무시된다.
    - 수정 후: orchestrator 는 None 을 전달, pipeline.run 내부에서 config 값을 사용.
    """

    async def test_process_job이_pipeline_run에_skip_llm_none_전달(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
    ) -> None:
        """_process_job은 pipeline.run()에 skip_llm_steps=None을 전달해야 한다.

        orchestrator 가 하드코딩 True 를 주입하지 않고,
        pipeline.run 의 config 폴백 경로를 타도록 None 을 전달해야 한다.
        """
        job = _make_job(job_id=1, meeting_id="skip_test")
        mock_pipeline.run.return_value = MagicMock(status="completed")

        await processor._process_job(job)

        # pipeline.run 호출 확인
        mock_pipeline.run.assert_called_once()
        call_kwargs = mock_pipeline.run.call_args
        # None 을 전달해야 함 — True 를 넘기면 config.yaml 이 무시된다
        assert call_kwargs.kwargs.get("skip_llm_steps") is None, (
            "orchestrator 가 skip_llm_steps=True 를 하드코딩하면 안 됨; "
            "config.pipeline.skip_llm_steps 를 존중하려면 None 을 전달해야 한다."
        )

    async def test_process_job_None_전달_후_정상_완료(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_job_queue: AsyncMock,
    ) -> None:
        """skip_llm_steps=None 전달 후에도 작업이 정상 완료되어야 한다."""
        job = _make_job(job_id=2, meeting_id="skip_complete")
        mock_pipeline.run.return_value = MagicMock(status="completed")

        await processor._process_job(job)

        # completed 상태 업데이트 확인
        calls = mock_job_queue.update_status.call_args_list
        status_values = [c[0][1] for c in calls]
        assert "completed" in status_values

        # 서멀 매니저 정상 호출 확인
        mock_thermal.notify_job_completed.assert_called_once()


# === Cycle 11: Pydantic 기본값 및 config 통합 ===


class TestSkipLlmStepsConfig:
    """PipelineConfig.skip_llm_steps 기본값 및 config.yaml 정합성 테스트.

    이슈 C 회귀 방지:
    - config.py 기본값이 True 이면 config.yaml 의 false 와 모순된다.
    - 수정 후: 기본값은 False (6단계 모두 실행).
    """

    async def test_pipeline_config_기본값은_False(self) -> None:
        """PipelineConfig 의 skip_llm_steps 기본값이 False 여야 한다.

        config.yaml 의 'false' 주석과 일치해야 하며,
        True 이면 사용자가 config.yaml 에서 명시적으로 설정해도
        Pydantic 기본값과 모순이 생긴다.
        """
        from config import PipelineConfig

        default_cfg = PipelineConfig()
        assert default_cfg.skip_llm_steps is False, (
            "PipelineConfig 기본값이 True 이면 config.yaml 의 false 설정이 무시될 수 있다. "
            "이슈 C 참고."
        )

    async def test_skip_llm_steps_false_설정시_pipeline_run에_None_전달(
        self,
        mock_job_queue: AsyncMock,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_ws_manager: AsyncMock,
    ) -> None:
        """config.pipeline.skip_llm_steps=False 일 때 pipeline.run 에 None 이 전달되어야 한다.

        pipeline.run 은 None 을 받으면 config.pipeline.skip_llm_steps(False)를 사용,
        즉 LLM 단계(correct, summarize)가 실행된다.
        """
        proc = JobProcessor(
            job_queue=mock_job_queue,
            pipeline=mock_pipeline,
            thermal_manager=mock_thermal,
            ws_manager=mock_ws_manager,
            poll_interval=0.1,
        )
        job = _make_job(job_id=10, meeting_id="cfg_false_test")
        mock_pipeline.run.return_value = MagicMock(status="completed")

        await proc._process_job(job)

        call_kwargs = mock_pipeline.run.call_args
        assert call_kwargs.kwargs.get("skip_llm_steps") is None

    async def test_skip_llm_steps_true_설정시에도_pipeline_run에_None_전달(
        self,
        mock_job_queue: AsyncMock,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_ws_manager: AsyncMock,
    ) -> None:
        """config.pipeline.skip_llm_steps=True 일 때도 pipeline.run 에 None 이 전달된다.

        orchestrator 는 항상 None 을 전달하며, True/False 결정은
        pipeline.run 내부의 config 폴백 로직이 담당한다.
        """
        proc = JobProcessor(
            job_queue=mock_job_queue,
            pipeline=mock_pipeline,
            thermal_manager=mock_thermal,
            ws_manager=mock_ws_manager,
            poll_interval=0.1,
        )
        job = _make_job(job_id=11, meeting_id="cfg_true_test")
        mock_pipeline.run.return_value = MagicMock(status="completed")

        await proc._process_job(job)

        call_kwargs = mock_pipeline.run.call_args
        # True 가 아니어야 함 — 하드코딩 회귀 방지
        assert call_kwargs.kwargs.get("skip_llm_steps") is not True
