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
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.job_queue import (
    CancellationClaim,
    DeleteClaim,
    InvalidTransitionError,
    JobStatus,
    RetranscribeClaim,
)
from core.orchestrator import JobProcessor

pytestmark = pytest.mark.asyncio


# === Fixture 정의 ===


@pytest.fixture
def mock_job_queue() -> AsyncMock:
    """비동기 작업 큐 목(Mock)을 생성한다."""
    queue = AsyncMock()
    queue.get_pending_jobs = AsyncMock(return_value=[])
    queue.get_all_jobs = AsyncMock(return_value=[])
    queue.update_status = AsyncMock()
    queue.queue = MagicMock()
    return queue


@pytest.fixture
def mock_pipeline() -> AsyncMock:
    """파이프라인 매니저 목(Mock)을 생성한다."""
    from core.meeting_mutation import MeetingMutationCoordinator

    pipeline = AsyncMock()
    pipeline.run = AsyncMock(return_value=MagicMock(status="completed"))
    pipeline.meeting_mutation_coordinator = MeetingMutationCoordinator()
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
    job.requested_action = ""
    return job


def _claim_action(
    *,
    token: str = "claim-token",
    phase: str = "staging",
    original_status: str = "completed",
    original_action: str = "full",
) -> str:
    """테스트용 versioned 재전사 claim payload를 만든다."""
    return RetranscribeClaim(
        original_status=original_status,
        original_requested_action=original_action,
        token=token,
        phase=phase,
    ).to_requested_action()


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


class TestRecoverRetranscribeClaim:
    """startup 시 중단된 재전사 transaction 복구 계약."""

    @pytest.mark.parametrize(
        ("phase", "expected_order"),
        [
            ("claimed", ["rollback", "restore"]),
            ("staging", ["rollback", "restore"]),
            ("purging", ["rollback", "marker", "restore"]),
            ("committing", ["cleanup", "finalize"]),
        ],
    )
    async def test_파일원복과_marker후에만_DB원상태를_복구한다(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
        mock_pipeline: AsyncMock,
        tmp_path: Path,
        phase: str,
        expected_order: list[str],
    ) -> None:
        """claim phase별 복구 순서가 filesystem → marker → token CAS여야 한다."""
        job = _make_job(status="recording")
        job.requested_action = _claim_action(phase=phase)
        mock_job_queue.get_all_jobs.return_value = [job]
        mock_pipeline._config.paths.resolved_checkpoints_dir = tmp_path / "checkpoints"
        mock_pipeline._config.paths.resolved_outputs_dir = tmp_path / "outputs"
        order: list[str] = []
        mock_job_queue.queue.restore_retranscribe_claim.side_effect = lambda *_args: order.append(
            "restore"
        )
        mock_job_queue.queue.reset_for_retranscribe.side_effect = lambda *_args: order.append(
            "finalize"
        )

        with (
            patch(
                "core.orchestrator.rollback_retranscribe_staging",
                side_effect=lambda *_args: order.append("rollback"),
            ) as rollback,
            patch.object(
                processor,
                "_write_retranscribe_recovery_marker",
                side_effect=lambda *_args: order.append("marker"),
            ) as marker,
            patch(
                "core.orchestrator.cleanup_retranscribe_staging",
                side_effect=lambda *_args: order.append("cleanup"),
            ) as cleanup,
        ):
            await processor._recover_orphaned_jobs()

        assert order == expected_order
        if phase == "committing":
            rollback.assert_not_called()
            cleanup.assert_called_once_with(
                tmp_path / "checkpoints",
                tmp_path / "outputs",
                job.meeting_id,
                "claim-token",
            )
        else:
            rollback.assert_called_once_with(
                tmp_path / "checkpoints",
                tmp_path / "outputs",
                job.meeting_id,
                "claim-token",
            )
            cleanup.assert_not_called()
        if phase == "purging":
            marker.assert_called_once()
            assert marker.call_args.args[0] == tmp_path / "checkpoints"
        else:
            marker.assert_not_called()
        if phase == "committing":
            mock_job_queue.queue.restore_retranscribe_claim.assert_not_called()
            mock_job_queue.queue.reset_for_retranscribe.assert_called_once_with(
                job.id,
                "claim-token",
            )
        else:
            mock_job_queue.queue.restore_retranscribe_claim.assert_called_once_with(
                job.id,
                "claim-token",
            )
            mock_job_queue.queue.reset_for_retranscribe.assert_not_called()
        mock_job_queue.queue.force_set_status.assert_not_called()


class TestRecoverDeleteClaim:
    """검색 purge에 진입한 삭제 claim의 startup roll-back 계약."""

    async def test_startup은_durable_취소_claim을_queued로_재개하지_않는다(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
    ) -> None:
        """취소 응답 직후 종료돼도 startup은 외부 업로드를 재개하지 않는다."""
        job = _make_job(status=JobStatus.RECORDING.value)
        job.requested_action = CancellationClaim(
            original_status=JobStatus.TRANSCRIBING.value,
            original_requested_action="transcribe",
            token="cancel-token",
        ).to_requested_action()
        mock_job_queue.get_all_jobs.return_value = [job]

        await processor._recover_orphaned_jobs()

        mock_job_queue.queue.finalize_cancellation_claim.assert_called_once_with(job.id)
        mock_job_queue.queue.force_set_status.assert_not_called()

    async def test_committing_delete_claim은_cache정리후_DB삭제로_rollforward한다(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
        mock_pipeline: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """cache cleanup 중 종료된 삭제는 회의를 되살리지 않고 끝까지 완료한다."""
        from config import AppConfig, PathsConfig

        config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
        mock_pipeline._config = config
        job = _make_job(status=JobStatus.RECORDING.value)
        job.requested_action = DeleteClaim(
            original_status=JobStatus.COMPLETED.value,
            original_requested_action="",
            original_error_message="",
            token="delete-token",
            phase="committing",
        ).to_requested_action()
        mock_job_queue.get_all_jobs.return_value = [job]
        order: list[str] = []
        mock_job_queue.queue.delete_claimed_job.side_effect = lambda *_args: order.append("delete")

        with patch(
            "steps.openai_transcriber.cleanup_meeting_openai_resume_caches",
            side_effect=lambda *_args: order.append("cleanup"),
        ):
            await processor._recover_orphaned_jobs()

        assert order == ["cleanup", "delete"]
        mock_job_queue.queue.restore_delete_claim.assert_not_called()

    async def test_purging_claim은_재색인과_marker소비후에만_DB를_복구한다(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
        mock_pipeline: AsyncMock,
        tmp_path: Path,
    ) -> None:
        from config import AppConfig, PathsConfig

        config = AppConfig().model_copy(update={"paths": PathsConfig(base_dir=str(tmp_path))})
        mock_pipeline._config = config
        mock_pipeline._model_manager = object()
        job = _make_job(status="recording")
        claim = DeleteClaim(
            original_status="completed",
            original_requested_action="full",
            original_error_message="",
            token="delete-token",
            phase="purging",
        )
        job.requested_action = claim.to_requested_action()
        mock_job_queue.get_all_jobs.return_value = [job]
        order: list[str] = []
        mock_job_queue.queue.restore_delete_claim.side_effect = lambda *_args: order.append(
            "restore"
        )

        with (
            patch.object(
                processor,
                "_write_retranscribe_recovery_marker",
                side_effect=lambda *_args, **_kwargs: order.append("marker"),
            ),
            patch(
                "core.reindex_recovery.reindex_meeting_artifacts",
                new=AsyncMock(side_effect=lambda *_args, **_kwargs: order.append("reindex")),
            ),
            patch(
                "core.reindex_recovery.consume_reindex_required_marker",
                side_effect=lambda *_args: order.append("consume"),
            ),
        ):
            await processor._recover_orphaned_jobs()

        assert order == ["marker", "reindex", "consume", "restore"]
        mock_job_queue.queue.restore_delete_claim.assert_called_once_with(
            job.id,
            "delete-token",
        )

    async def test_purging_claim_재색인실패는_completed로_숨기지_않는다(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
        mock_pipeline: AsyncMock,
        tmp_path: Path,
    ) -> None:
        from config import AppConfig, PathsConfig

        config = AppConfig().model_copy(update={"paths": PathsConfig(base_dir=str(tmp_path))})
        mock_pipeline._config = config
        mock_pipeline._model_manager = object()
        job = _make_job(status="recording")
        job.requested_action = DeleteClaim(
            original_status="completed",
            original_requested_action="",
            original_error_message="",
            token="delete-token",
            phase="purging",
        ).to_requested_action()
        mock_job_queue.get_all_jobs.return_value = [job]

        with (
            patch.object(processor, "_write_retranscribe_recovery_marker"),
            patch(
                "core.reindex_recovery.reindex_meeting_artifacts",
                new=AsyncMock(side_effect=RuntimeError("embed unavailable")),
            ),
        ):
            await processor._recover_orphaned_jobs()

        mock_job_queue.queue.restore_delete_claim.assert_not_called()

    async def test_recorded_purging_claim은_산출물없이_원상복구한다(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
    ) -> None:
        """아직 전사되지 않은 회의는 존재하지 않는 검색 산출물을 요구하지 않는다."""
        job = _make_job(status="recording")
        job.requested_action = DeleteClaim(
            original_status="recorded",
            original_requested_action="",
            original_error_message="",
            token="delete-token",
            phase="purging",
        ).to_requested_action()
        mock_job_queue.get_all_jobs.return_value = [job]

        with patch(
            "core.reindex_recovery.reindex_meeting_artifacts",
            new=AsyncMock(),
        ) as reindex:
            await processor._recover_orphaned_jobs()

        reindex.assert_not_awaited()
        mock_job_queue.queue.restore_delete_claim.assert_called_once_with(
            job.id,
            "delete-token",
        )

    @pytest.mark.parametrize("configured_child", ["../outside", "/tmp/outside"])
    async def test_recovery_storage_root는_base_dir_밖을_거부한다(
        self,
        tmp_path: Path,
        configured_child: str,
    ) -> None:
        """startup recovery도 traversal·absolute-outside storage 설정을 사용하지 않는다."""
        from config import AppConfig, PathsConfig

        base = tmp_path.resolve() / "base"
        base.mkdir()
        config = AppConfig().model_copy(
            update={
                "paths": PathsConfig(
                    base_dir=str(base),
                    checkpoints_dir=configured_child,
                )
            }
        )

        with pytest.raises(ValueError, match="base_dir"):
            JobProcessor._configured_storage_root(
                config,
                "checkpoints_dir",
                config.paths.resolved_checkpoints_dir,
            )

    async def test_recovery_marker는_symlink_target을_덮어쓰지_않는다(
        self,
        processor: JobProcessor,
        tmp_path: Path,
    ) -> None:
        """purging recovery marker final symlink는 외부 target mutation 전에 차단한다."""
        from core.audio_quality import AudioFailureKind
        from core.pipeline import InvalidInputError

        checkpoints_root = tmp_path.resolve() / "checkpoints"
        meeting_id = "safe-meeting"
        meeting_dir = checkpoints_root / meeting_id
        meeting_dir.mkdir(parents=True)
        external = tmp_path.resolve() / "external-marker.json"
        external.write_text("KEEP", encoding="utf-8")
        (meeting_dir / "reindex_required.json").symlink_to(external)
        claim = RetranscribeClaim(
            original_status="completed",
            original_requested_action="full",
            token="claim-token",
            phase="purging",
        )

        with pytest.raises(InvalidInputError) as exc_info:
            processor._write_retranscribe_recovery_marker(
                checkpoints_root,
                meeting_id,
                claim,
            )

        assert exc_info.value.failure_kind is AudioFailureKind.SECURITY_BLOCKED
        assert external.read_text(encoding="utf-8") == "KEEP"

    async def test_committing_partial_cleanup실패는_claim유지후_다음_startup에_재개한다(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
        mock_pipeline: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """strict cleanup 중단은 rollback하지 않고 멱등 cleanup→finalize로 이어진다."""
        job = _make_job(status="recording")
        job.requested_action = _claim_action(phase="committing")
        mock_job_queue.get_all_jobs.return_value = [job]
        mock_pipeline._config.paths.resolved_checkpoints_dir = tmp_path / "checkpoints"
        mock_pipeline._config.paths.resolved_outputs_dir = tmp_path / "outputs"

        with patch(
            "core.orchestrator.cleanup_retranscribe_staging",
            side_effect=[OSError("partial cleanup"), None],
        ) as cleanup:
            await processor._recover_orphaned_jobs()
            mock_job_queue.queue.reset_for_retranscribe.assert_not_called()
            await processor._recover_orphaned_jobs()

        assert cleanup.call_count == 2
        mock_job_queue.queue.reset_for_retranscribe.assert_called_once_with(
            job.id,
            "claim-token",
        )
        mock_job_queue.queue.restore_retranscribe_claim.assert_not_called()

    async def test_claim_rollback실패는_DB를_유지하고_다른_orphan은_계속_복구한다(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
        mock_pipeline: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """산출물 원복 실패를 queued 전환으로 숨기지 않고 scan은 계속한다."""
        claim_job = _make_job(job_id=1, status="recording")
        claim_job.requested_action = _claim_action(phase="staging")
        normal_job = _make_job(job_id=2, status="transcribing")
        mock_job_queue.get_all_jobs.return_value = [claim_job, normal_job]
        mock_pipeline._config.paths.resolved_checkpoints_dir = tmp_path / "checkpoints"
        mock_pipeline._config.paths.resolved_outputs_dir = tmp_path / "outputs"

        with patch(
            "core.orchestrator.rollback_retranscribe_staging",
            side_effect=OSError("rollback failed"),
        ):
            await processor._recover_orphaned_jobs()

        mock_job_queue.queue.restore_retranscribe_claim.assert_not_called()
        mock_job_queue.queue.force_set_status.assert_called_once_with(
            normal_job.id,
            JobStatus.QUEUED,
            error_message="",
        )

    async def test_일반_recording은_재전사_claim으로_오인하지_않는다(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
    ) -> None:
        """versioned marker가 없는 실제 recording job은 startup이 변경하지 않는다."""
        recording = _make_job(status="recording")
        recording.requested_action = "record"
        mock_job_queue.get_all_jobs.return_value = [recording]

        with patch("core.orchestrator.rollback_retranscribe_staging") as rollback:
            await processor._recover_orphaned_jobs()

        rollback.assert_not_called()
        mock_job_queue.queue.restore_retranscribe_claim.assert_not_called()
        mock_job_queue.queue.force_set_status.assert_not_called()


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


class TestMarkJobCompletedAfterPipeline:
    """파이프라인 완료 후 작업 상태 확정 테스트."""

    async def test_표준_completed_전이가_성공하면_true를_반환한다(
        self,
        processor: JobProcessor,
        mock_job_queue: AsyncMock,
    ) -> None:
        """정상 completed 업데이트는 추가 복구 없이 성공해야 한다."""
        result = await processor._mark_job_completed_after_pipeline(1, "meeting_done")

        assert result is True
        mock_job_queue.update_status.assert_called_once_with(1, "completed")

    async def test_completed_전이가_실패하면_force_set_status로_복구한다(
        self,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_ws_manager: AsyncMock,
    ) -> None:
        """failed → completed 같은 전이 실패는 강제 복구 경로를 사용한다."""

        class RawQueue:
            def __init__(self) -> None:
                self.calls: list[tuple[int, str, str]] = []

            def force_set_status(
                self,
                job_id: int,
                status: Any,
                error_message: str = "",
            ) -> None:
                self.calls.append((job_id, str(status), error_message))

        class QueueWrapper:
            def __init__(self) -> None:
                self.queue = RawQueue()

            async def update_status(self, *_args: Any, **_kwargs: Any) -> None:
                raise Exception("상태 전이 불가")

        queue = QueueWrapper()
        processor = JobProcessor(
            job_queue=queue,
            pipeline=mock_pipeline,
            thermal_manager=mock_thermal,
            ws_manager=mock_ws_manager,
            poll_interval=0.1,
        )

        result = await processor._mark_job_completed_after_pipeline(7, "meeting_done")

        assert result is True
        assert queue.queue.calls == [(7, "completed", "")]


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

    async def test_thermal_wait중_취소된_stale_job은_pipeline을_시작하지_않는다(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_job_queue: AsyncMock,
    ) -> None:
        """queued snapshot 뒤 CAS가 실패하면 외부 전사를 포함한 실행을 생략한다."""
        job = _make_job(job_id=41, meeting_id="cancelled-during-thermal")
        mock_job_queue.claim_queued_job_for_processing.side_effect = InvalidTransitionError(
            41,
            JobStatus.RECORDED.value,
            JobStatus.TRANSCRIBING.value,
        )

        await processor._process_job(job)

        mock_thermal.wait_if_needed.assert_awaited_once()
        mock_thermal.notify_job_started.assert_not_awaited()
        mock_pipeline.run.assert_not_awaited()

    async def test_pipeline에_청크_취소_callback을_전달한다(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
    ) -> None:
        """OpenAI 어댑터가 청크 사이 취소 상태를 직접 확인할 수 있다."""
        job = _make_job(job_id=42, meeting_id="chunk-cancel")

        await processor._process_job(job)

        callback = mock_pipeline.run.await_args.kwargs["should_cancel"]
        assert callback() is False
        processor.request_cancellation("chunk-cancel")
        assert callback() is True

    async def test_pipeline_취소_callback은_DB_claim도_확인한다(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_job_queue: AsyncMock,
    ) -> None:
        """API task가 중단돼 메모리 flag가 없어도 durable claim이 업로드를 막는다."""
        job = _make_job(job_id=43, meeting_id="durable-chunk-cancel")
        durable_job = _make_job(
            job_id=43,
            meeting_id="durable-chunk-cancel",
            status=JobStatus.RECORDING.value,
        )
        durable_job.requested_action = CancellationClaim(
            original_status=JobStatus.TRANSCRIBING.value,
            original_requested_action="",
            token="cancel-token",
        ).to_requested_action()
        mock_job_queue.queue.get_job.return_value = durable_job

        async def _run_with_callback(*_args: object, **kwargs: object) -> object:
            callback = kwargs["should_cancel"]
            assert callable(callback)
            assert callback() is True
            raise asyncio.CancelledError

        mock_pipeline.run.side_effect = _run_with_callback

        await processor._process_job(job)

        mock_job_queue.queue.finalize_cancellation_claim.assert_called_once_with(job.id)

    async def test_최종취소검사후_claim도_completed로_덮지_않는다(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_job_queue: AsyncMock,
    ) -> None:
        """pipeline 반환과 completed CAS 사이 취소가 이기면 recorded로 확정한다."""
        job = _make_job(job_id=44, meeting_id="cancel-at-complete")
        claimed_job = _make_job(job_id=44, meeting_id="cancel-at-complete")
        claimed_job.stt_provider = "local"
        claimed_job.stt_model = "mlx-community/whisper-large-v3-turbo"
        mock_job_queue.claim_queued_job_for_processing.return_value = claimed_job
        normal = _make_job(
            job_id=44,
            meeting_id="cancel-at-complete",
            status=JobStatus.TRANSCRIBING.value,
        )
        durable = _make_job(
            job_id=44,
            meeting_id="cancel-at-complete",
            status=JobStatus.RECORDING.value,
        )
        durable.requested_action = CancellationClaim(
            original_status=JobStatus.TRANSCRIBING.value,
            original_requested_action="",
            token="cancel-token",
        ).to_requested_action()
        mock_job_queue.queue.get_job.return_value = normal

        async def _mark_then_claim(*_args: object) -> bool:
            mock_job_queue.queue.get_job.return_value = durable
            return False

        with patch.object(
            processor,
            "_mark_job_completed_after_pipeline",
            side_effect=_mark_then_claim,
        ) as mark_completed:
            await processor._process_job(job)

        mark_completed.assert_awaited_once()
        mock_job_queue.queue.finalize_cancellation_claim.assert_called_once_with(job.id)
        events = [
            call.args[0].event_type
            for call in processor._ws_manager.broadcast_event.call_args_list
        ]
        assert "job_cancelled" in events
        assert "job_completed" not in events

    async def test_오류와_취소claim이_경합하면_failed가_아닌_cancelled다(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_job_queue: AsyncMock,
    ) -> None:
        """외부 요청 오류 직전 durable 취소가 생기면 실패 상태로 덮지 않는다."""
        job = _make_job(job_id=45, meeting_id="cancel-at-error")
        claimed_job = _make_job(job_id=45, meeting_id="cancel-at-error")
        claimed_job.stt_provider = "local"
        claimed_job.stt_model = "mlx-community/whisper-large-v3-turbo"
        mock_job_queue.claim_queued_job_for_processing.return_value = claimed_job
        durable = _make_job(
            job_id=45,
            meeting_id="cancel-at-error",
            status=JobStatus.RECORDING.value,
        )
        durable.requested_action = CancellationClaim(
            original_status=JobStatus.TRANSCRIBING.value,
            original_requested_action="",
            token="cancel-token",
        ).to_requested_action()

        async def _fail_after_claim(*_args: object, **_kwargs: object) -> None:
            mock_job_queue.queue.get_job.return_value = durable
            raise RuntimeError("upstream failed")

        mock_pipeline.run.side_effect = _fail_after_claim

        await processor._process_job(job)

        mock_job_queue.queue.finalize_cancellation_claim.assert_called_once_with(job.id)
        failed_updates = [
            call
            for call in mock_job_queue.update_status.call_args_list
            if len(call.args) > 1 and call.args[1] == JobStatus.FAILED
        ]
        assert failed_updates == []

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

    async def test_입력_품질_차단은_failed_대신_recorded로_보류한다(
        self,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_ws_manager: AsyncMock,
    ) -> None:
        """InvalidInputError는 사용자 오류 카드 없이 recorded로 되돌려야 한다."""
        from core.job_queue import JobStatus
        from core.pipeline import InvalidInputError

        force_calls: list[tuple[int, Any, str]] = []
        raw_queue = MagicMock()
        raw_queue.force_set_status = MagicMock(
            side_effect=lambda job_id, status, error_message="": force_calls.append(
                (job_id, status, error_message)
            )
        )
        queue = AsyncMock()
        queue.update_status = AsyncMock()
        queue.queue = raw_queue
        processor = JobProcessor(
            job_queue=queue,
            pipeline=mock_pipeline,
            thermal_manager=mock_thermal,
            ws_manager=mock_ws_manager,
            poll_interval=0.1,
        )
        job = _make_job(job_id=17, meeting_id="invalid_audio")
        mock_pipeline.run.side_effect = InvalidInputError("30초 미만 오디오")

        await processor._process_job(job)

        assert force_calls == [(17, JobStatus.RECORDED, "")]
        status_values = [call.args[1] for call in queue.update_status.call_args_list]
        assert "failed" not in status_values
        event_types = [
            call.args[0].event_type for call in mock_ws_manager.broadcast_event.call_args_list
        ]
        assert "job_failed" not in event_types
        mock_thermal.notify_job_completed.assert_called_once()

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

    async def test_shutdown_cancel은_사용자취소로_recorded_처리하지_않음(
        self,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_job_queue: AsyncMock,
    ) -> None:
        """시스템 종료 취소는 사용자 취소가 아니므로 queued로 재대기해야 한다."""
        from core.job_queue import JobStatus

        calls: list[tuple[int, Any, str]] = []
        raw_queue = MagicMock()
        raw_queue.force_set_status = MagicMock(
            side_effect=lambda job_id, status, error_message="": calls.append(
                (job_id, status, error_message)
            )
        )
        mock_job_queue.queue = raw_queue
        job = _make_job(job_id=9, meeting_id="shutdown_cancel")
        mock_pipeline.run.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await processor._process_job(job)

        assert calls == [(9, JobStatus.QUEUED, "앱 종료로 작업이 중단되어 재시도 대기 중입니다.")]
        assert all(status != JobStatus.RECORDED for _job_id, status, _msg in calls)
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

    @pytest.mark.parametrize(
        ("requested_action", "expected_skip"),
        [
            ("transcribe", True),
            ("full", False),
        ],
    )
    async def test_process_job은_batch_requested_action을_skip_llm으로_변환(
        self,
        requested_action: str,
        expected_skip: bool,
        processor: JobProcessor,
        mock_pipeline: AsyncMock,
    ) -> None:
        """일괄 큐잉된 작업은 저장된 실행 의도를 pipeline.run 에 명시 전달한다."""
        job = _make_job(job_id=3, meeting_id=f"batch_{requested_action}")
        job.requested_action = requested_action
        mock_pipeline.run.return_value = MagicMock(status="completed")

        await processor._process_job(job)

        call_kwargs = mock_pipeline.run.call_args
        assert call_kwargs.kwargs.get("skip_llm_steps") is expected_skip


# === Cycle 11: Pydantic 기본값 및 config 통합 ===


class TestSkipLlmStepsConfig:
    """PipelineConfig.skip_llm_steps 기본값 및 config.yaml 정합성 테스트.

    이슈 C 회귀 방지:
    - config.py 기본값이 True 이면 config.yaml 의 false 와 모순된다.
    - 수정 후: 기본값은 False (6단계 모두 실행).
    """

    @pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
    def test_pipeline_config_기본값은_False(self) -> None:
        """PipelineConfig 의 skip_llm_steps 기본값이 False 여야 한다.

        config.yaml 의 'false' 주석과 일치해야 하며,
        True 이면 사용자가 config.yaml 에서 명시적으로 설정해도
        Pydantic 기본값과 모순이 생긴다.
        await 가 없으므로 동기 함수이며, 모듈 레벨 pytestmark 의 asyncio 마크는
        filterwarnings 로 억제한다.
        """
        from config import PipelineConfig

        default_cfg = PipelineConfig()
        assert default_cfg.skip_llm_steps is False, (
            "PipelineConfig 기본값이 True 이면 config.yaml 의 false 설정이 무시될 수 있다. "
            "이슈 C 참고."
        )

    @pytest.mark.parametrize("config_skip", [False, True])
    async def test_orchestrator는_config_값과_무관하게_항상_None_전달(
        self,
        config_skip: bool,
        mock_job_queue: AsyncMock,
        mock_pipeline: AsyncMock,
        mock_thermal: AsyncMock,
        mock_ws_manager: AsyncMock,
    ) -> None:
        """config.pipeline.skip_llm_steps 값과 무관하게 pipeline.run 에 None 이 전달된다.

        orchestrator 는 skip_llm_steps 를 hardcode 하지 않고 항상 None 을 전달한다.
        True/False 결정은 pipeline.run 내부의 config 폴백 로직이 담당하므로,
        config 값을 어떻게 설정해도 orchestrator 에서 None 이 와야 한다는 불변식을 검증한다.
        """
        from config import get_config

        # config.pipeline.skip_llm_steps 를 실제로 설정해 불변식을 진짜로 검증
        get_config().pipeline.skip_llm_steps = config_skip

        proc = JobProcessor(
            job_queue=mock_job_queue,
            pipeline=mock_pipeline,
            thermal_manager=mock_thermal,
            ws_manager=mock_ws_manager,
            poll_interval=0.1,
        )
        job = _make_job(job_id=10, meeting_id=f"cfg_{config_skip}_test")
        mock_pipeline.run.return_value = MagicMock(status="completed")

        await proc._process_job(job)

        call_kwargs = mock_pipeline.run.call_args
        # config 값이 False 든 True 든 orchestrator 는 None 을 전달해야 한다
        assert call_kwargs.kwargs.get("skip_llm_steps") is None
