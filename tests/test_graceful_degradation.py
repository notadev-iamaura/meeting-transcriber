"""
Graceful Degradation 테스트 모듈

목적: core/pipeline.py의 ResourceGuard, ResourceStatus,
     PipelineManager의 Graceful Degradation 로직을 검증한다.
주요 테스트:
    - ResourceStatus 속성 검증
    - ResourceGuard 디스크/메모리 체크
    - ResourceGuard 체크 실패 시 안전 처리
    - ResourceGuard 경고 콜백 호출
    - PipelineManager 디스크 부족 시 시작 거부
    - PipelineManager 메모리 부족 시 LLM 단계 스킵
    - PipelineState degraded/skipped_steps/warnings 필드
    - 정상 리소스에서 파이프라인 정상 실행 확인
의존성: pytest, pytest-asyncio, unittest.mock
"""

import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.pipeline import (
    _LLM_STEPS,
    PipelineError,
    PipelineManager,
    PipelineState,
    ResourceGuard,
    ResourceStatus,
)

pytestmark = pytest.mark.asyncio


# === 픽스처 ===


@pytest.fixture
def mock_config(tmp_path: Path) -> MagicMock:
    """테스트용 AppConfig 모킹 객체를 생성한다."""
    config = MagicMock()
    config.pipeline.checkpoint_enabled = True
    config.pipeline.retry_max_count = 2
    config.pipeline.peak_ram_limit_gb = 9.5
    config.pipeline.min_disk_free_gb = 2.0
    config.pipeline.min_memory_free_gb = 2.0
    config.paths.resolved_outputs_dir = tmp_path / "outputs"
    config.paths.resolved_checkpoints_dir = tmp_path / "checkpoints"
    config.paths.resolved_base_dir = tmp_path
    return config


@pytest.fixture
def mock_model_manager() -> MagicMock:
    """테스트용 ModelLoadManager 모킹 객체를 생성한다."""
    return MagicMock()


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    """테스트용 임시 오디오 파일을 생성한다."""
    audio = tmp_path / "test_meeting.m4a"
    audio.write_bytes(b"fake audio content for testing")
    return audio


# === ResourceStatus 테스트 ===


class TestResourceStatus:
    """ResourceStatus 데이터클래스 테스트."""

    def test_all_ok_true(self) -> None:
        """디스크와 메모리 모두 충분하면 all_ok=True."""
        status = ResourceStatus(
            disk_ok=True,
            disk_free_gb=10.0,
            memory_ok=True,
            memory_free_gb=8.0,
        )
        assert status.all_ok is True
        assert status.llm_available is True

    def test_all_ok_false_disk(self) -> None:
        """디스크 부족이면 all_ok=False."""
        status = ResourceStatus(
            disk_ok=False,
            disk_free_gb=1.0,
            memory_ok=True,
            memory_free_gb=8.0,
        )
        assert status.all_ok is False
        assert status.llm_available is True

    def test_all_ok_false_memory(self) -> None:
        """메모리 부족이면 all_ok=False, llm_available=False."""
        status = ResourceStatus(
            disk_ok=True,
            disk_free_gb=10.0,
            memory_ok=False,
            memory_free_gb=1.0,
        )
        assert status.all_ok is False
        assert status.llm_available is False

    def test_both_insufficient(self) -> None:
        """디스크+메모리 모두 부족."""
        status = ResourceStatus(
            disk_ok=False,
            disk_free_gb=0.5,
            memory_ok=False,
            memory_free_gb=0.5,
        )
        assert status.all_ok is False
        assert status.llm_available is False


# === ResourceGuard 테스트 ===


class TestResourceGuardDisk:
    """ResourceGuard 디스크 체크 테스트."""

    @patch("core.pipeline.shutil.disk_usage")
    def test_disk_sufficient(
        self,
        mock_usage: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """디스크 여유 충분 시 (True, free_gb) 반환."""
        # 10GB 여유
        mock_usage.return_value = MagicMock(free=10 * 1024**3)
        guard = ResourceGuard(mock_config)
        ok, free = guard.check_disk()
        assert ok is True
        assert free == 10.0

    @patch("core.pipeline.shutil.disk_usage")
    def test_disk_insufficient(
        self,
        mock_usage: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """디스크 여유 부족 시 (False, free_gb) 반환."""
        # 1GB 여유 (임계값 2GB 미만)
        mock_usage.return_value = MagicMock(free=1 * 1024**3)
        guard = ResourceGuard(mock_config)
        ok, free = guard.check_disk()
        assert ok is False
        assert free == 1.0

    @patch("core.pipeline.shutil.disk_usage")
    def test_disk_check_oserror_returns_ok(
        self,
        mock_usage: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """디스크 체크 OSError 시 안전하게 (True, 0.0) 반환."""
        mock_usage.side_effect = OSError("디스크 접근 불가")
        guard = ResourceGuard(mock_config)
        ok, free = guard.check_disk()
        assert ok is True
        assert free == 0.0

    @patch("core.pipeline.shutil.disk_usage")
    def test_disk_nonexistent_base_dir_fallback(
        self,
        mock_usage: MagicMock,
        mock_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        """base_dir가 없으면 상위 디렉토리로 폴백."""
        # 존재하지 않는 경로 설정
        mock_config.paths.resolved_base_dir = tmp_path / "nonexistent" / "deep" / "path"
        mock_usage.return_value = MagicMock(free=5 * 1024**3)
        guard = ResourceGuard(mock_config)
        ok, free = guard.check_disk()
        assert ok is True
        # 폴백 경로(tmp_path)로 체크했는지 확인
        mock_usage.assert_called_once_with(str(tmp_path))


class TestResourceGuardMemory:
    """ResourceGuard 메모리 체크 테스트."""

    @patch("core.pipeline.psutil.virtual_memory")
    def test_memory_sufficient(
        self,
        mock_mem: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """메모리 충분 시 (True, available_gb) 반환."""
        mock_mem.return_value = MagicMock(available=8 * 1024**3)
        guard = ResourceGuard(mock_config)
        ok, available = guard.check_memory()
        assert ok is True
        assert available == 8.0

    @patch("core.pipeline.psutil.virtual_memory")
    def test_memory_insufficient(
        self,
        mock_mem: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """메모리 부족 시 (False, available_gb) 반환."""
        mock_mem.return_value = MagicMock(available=1 * 1024**3)
        guard = ResourceGuard(mock_config)
        ok, available = guard.check_memory()
        assert ok is False
        assert available == 1.0

    @patch("core.pipeline.psutil.virtual_memory")
    def test_memory_check_error_returns_ok(
        self,
        mock_mem: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """메모리 체크 예외 시 안전하게 (True, 0.0) 반환."""
        mock_mem.side_effect = OSError("psutil 오류")
        guard = ResourceGuard(mock_config)
        ok, available = guard.check_memory()
        assert ok is True
        assert available == 0.0


class TestResourceGuardCheckAll:
    """ResourceGuard.check_all() 통합 테스트."""

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    def test_check_all_ok(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """모든 리소스 충분 시 ResourceStatus.all_ok=True."""
        mock_disk.return_value = MagicMock(free=10 * 1024**3)
        mock_mem.return_value = MagicMock(available=8 * 1024**3)
        guard = ResourceGuard(mock_config)
        status = guard.check_all()
        assert status.all_ok is True
        assert status.disk_ok is True
        assert status.memory_ok is True

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    def test_check_all_disk_low(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """디스크만 부족 시 disk_ok=False."""
        mock_disk.return_value = MagicMock(free=1 * 1024**3)
        mock_mem.return_value = MagicMock(available=8 * 1024**3)
        guard = ResourceGuard(mock_config)
        status = guard.check_all()
        assert status.disk_ok is False
        assert status.memory_ok is True

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    def test_check_all_memory_low(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """메모리만 부족 시 memory_ok=False."""
        mock_disk.return_value = MagicMock(free=10 * 1024**3)
        mock_mem.return_value = MagicMock(available=1 * 1024**3)
        guard = ResourceGuard(mock_config)
        status = guard.check_all()
        assert status.disk_ok is True
        assert status.memory_ok is False


class TestResourceGuardCallback:
    """ResourceGuard 경고 콜백 테스트."""

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    def test_disk_warning_callback(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """디스크 부족 시 콜백이 disk_low 수준으로 호출된다."""
        mock_disk.return_value = MagicMock(free=1 * 1024**3)
        mock_mem.return_value = MagicMock(available=8 * 1024**3)
        callback = MagicMock()
        guard = ResourceGuard(mock_config, on_warning=callback)
        guard.check_all()
        callback.assert_called_once()
        args = callback.call_args[0]
        assert "디스크" in args[0]
        assert args[1] == "disk_low"

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    def test_memory_warning_callback(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """메모리 부족 시 콜백이 memory_low 수준으로 호출된다."""
        mock_disk.return_value = MagicMock(free=10 * 1024**3)
        mock_mem.return_value = MagicMock(available=1 * 1024**3)
        callback = MagicMock()
        guard = ResourceGuard(mock_config, on_warning=callback)
        guard.check_all()
        callback.assert_called_once()
        args = callback.call_args[0]
        assert "메모리" in args[0]
        assert args[1] == "memory_low"

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    def test_both_warning_callbacks(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """디스크+메모리 모두 부족 시 콜백이 2회 호출된다."""
        mock_disk.return_value = MagicMock(free=1 * 1024**3)
        mock_mem.return_value = MagicMock(available=1 * 1024**3)
        callback = MagicMock()
        guard = ResourceGuard(mock_config, on_warning=callback)
        guard.check_all()
        assert callback.call_count == 2

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    def test_no_callback_when_ok(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        """리소스 충분 시 콜백이 호출되지 않는다."""
        mock_disk.return_value = MagicMock(free=10 * 1024**3)
        mock_mem.return_value = MagicMock(available=8 * 1024**3)
        callback = MagicMock()
        guard = ResourceGuard(mock_config, on_warning=callback)
        guard.check_all()
        callback.assert_not_called()


class TestResourceGuardLLMSteps:
    """ResourceGuard.is_llm_step() 테스트."""

    def test_correct_is_llm_step(self, mock_config: MagicMock) -> None:
        guard = ResourceGuard(mock_config)
        assert guard.is_llm_step("correct") is True

    def test_summarize_is_llm_step(self, mock_config: MagicMock) -> None:
        guard = ResourceGuard(mock_config)
        assert guard.is_llm_step("summarize") is True

    def test_convert_is_not_llm_step(self, mock_config: MagicMock) -> None:
        guard = ResourceGuard(mock_config)
        assert guard.is_llm_step("convert") is False

    def test_transcribe_is_not_llm_step(self, mock_config: MagicMock) -> None:
        guard = ResourceGuard(mock_config)
        assert guard.is_llm_step("transcribe") is False

    def test_merge_is_not_llm_step(self, mock_config: MagicMock) -> None:
        guard = ResourceGuard(mock_config)
        assert guard.is_llm_step("merge") is False


class TestLLMStepsConstant:
    """_LLM_STEPS 상수 테스트."""

    def test_llm_steps_contains_correct_and_summarize(self) -> None:
        assert "correct" in _LLM_STEPS
        assert "summarize" in _LLM_STEPS
        assert len(_LLM_STEPS) == 2


# === PipelineState 확장 필드 테스트 ===


class TestPipelineStateDegradedFields:
    """PipelineState의 degraded 관련 필드 테스트."""

    def test_default_values(self) -> None:
        """기본값: degraded=False, skipped_steps=[], warnings=[]."""
        state = PipelineState(
            meeting_id="test_001",
            audio_path="/tmp/test.m4a",
        )
        assert state.degraded is False
        assert state.skipped_steps == []
        assert state.warnings == []

    def test_degraded_serialization(self, tmp_path: Path) -> None:
        """degraded 필드가 JSON 직렬화/역직렬화에 보존된다."""
        state = PipelineState(
            meeting_id="test_002",
            audio_path="/tmp/test.m4a",
            degraded=True,
            skipped_steps=["correct", "summarize"],
            warnings=["메모리 부족으로 LLM 단계 건너뜀"],
        )
        state_path = tmp_path / "state.json"
        state.save(state_path)

        restored = PipelineState.from_file(state_path)
        assert restored.degraded is True
        assert restored.skipped_steps == ["correct", "summarize"]
        assert len(restored.warnings) == 1
        assert "메모리 부족" in restored.warnings[0]

    def test_to_dict_includes_degraded(self) -> None:
        """to_dict()에 degraded 관련 필드가 포함된다."""
        state = PipelineState(
            meeting_id="test_003",
            audio_path="/tmp/test.m4a",
            degraded=True,
        )
        d = state.to_dict()
        assert "degraded" in d
        assert "skipped_steps" in d
        assert "warnings" in d


# === PipelineManager Graceful Degradation 통합 테스트 ===


class TestPipelineManagerDiskInsufficient:
    """디스크 부족 시 파이프라인 시작 거부 테스트."""

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    async def test_disk_insufficient_raises_pipeline_error(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """디스크 부족 시 PipelineError가 발생한다."""
        # 디스크 1GB (임계값 2GB 미만)
        mock_disk.return_value = MagicMock(free=1 * 1024**3)
        mock_mem.return_value = MagicMock(available=8 * 1024**3)

        pipeline = PipelineManager(
            config=mock_config,
            model_manager=mock_model_manager,
        )
        with pytest.raises(PipelineError, match="디스크 여유 공간 부족"):
            await pipeline.run(audio_file)

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    async def test_disk_insufficient_state_is_failed(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
        tmp_path: Path,
    ) -> None:
        """디스크 부족 시 state.status='failed'로 저장된다."""
        mock_disk.return_value = MagicMock(free=1 * 1024**3)
        mock_mem.return_value = MagicMock(available=8 * 1024**3)

        pipeline = PipelineManager(
            config=mock_config,
            model_manager=mock_model_manager,
        )
        with contextlib.suppress(PipelineError):
            await pipeline.run(audio_file, meeting_id="disk_test")

        # 상태 파일 확인
        state_path = tmp_path / "checkpoints" / "disk_test" / "pipeline_state.json"
        assert state_path.exists()
        state = PipelineState.from_file(state_path)
        assert state.status == "failed"
        assert "디스크" in state.error_message
        assert len(state.warnings) > 0

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    async def test_disk_insufficient_callback_called(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """디스크 부족 시 경고 콜백이 호출된다."""
        mock_disk.return_value = MagicMock(free=1 * 1024**3)
        mock_mem.return_value = MagicMock(available=8 * 1024**3)
        callback = MagicMock()

        pipeline = PipelineManager(
            config=mock_config,
            model_manager=mock_model_manager,
            on_resource_warning=callback,
        )
        with contextlib.suppress(PipelineError):
            await pipeline.run(audio_file)

        # disk_low 콜백이 호출되었는지 확인
        assert callback.call_count >= 1
        # 첫 번째 호출의 두 번째 인자가 "disk_low"
        found_disk_low = any(c[0][1] == "disk_low" for c in callback.call_args_list)
        assert found_disk_low


class TestPipelineManagerMemoryInsufficient:
    """메모리 부족 시 LLM 단계 스킵 테스트."""

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    async def test_memory_insufficient_skips_correct_and_summarize(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """메모리 부족 시 correct/summarize를 건너뛴다."""
        mock_disk.return_value = MagicMock(free=10 * 1024**3)
        # 메모리 1GB (임계값 2GB 미만)
        mock_mem.return_value = MagicMock(available=1 * 1024**3)

        # 비-LLM 단계 모킹
        with (
            patch.object(
                PipelineManager,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=audio_file,
            ),
            patch.object(
                PipelineManager,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_correct",
                new_callable=AsyncMock,
            ) as mock_correct,
            patch.object(
                PipelineManager,
                "_run_step_summarize",
                new_callable=AsyncMock,
            ) as mock_summarize,
        ):
            pipeline = PipelineManager(
                config=mock_config,
                model_manager=mock_model_manager,
            )
            state = await pipeline.run(audio_file, meeting_id="mem_test")

            # LLM 단계가 실행되지 않았는지 확인
            mock_correct.assert_not_called()
            mock_summarize.assert_not_called()

            # 상태 확인
            assert state.status == "completed"
            assert state.degraded is True
            assert "correct" in state.skipped_steps
            assert "summarize" in state.skipped_steps
            assert len(state.warnings) > 0

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    async def test_memory_insufficient_completes_non_llm_steps(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """메모리 부족이라도 비-LLM 단계는 정상 실행된다."""
        mock_disk.return_value = MagicMock(free=10 * 1024**3)
        mock_mem.return_value = MagicMock(available=1 * 1024**3)

        with (
            patch.object(
                PipelineManager,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=audio_file,
            ) as mock_convert,
            patch.object(
                PipelineManager,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ) as mock_transcribe,
            patch.object(
                PipelineManager,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ) as mock_diarize,
            patch.object(
                PipelineManager,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ) as mock_merge,
            patch.object(
                PipelineManager,
                "_run_step_correct",
                new_callable=AsyncMock,
            ),
            patch.object(
                PipelineManager,
                "_run_step_summarize",
                new_callable=AsyncMock,
            ),
        ):
            pipeline = PipelineManager(
                config=mock_config,
                model_manager=mock_model_manager,
            )
            state = await pipeline.run(audio_file, meeting_id="mem_ok_test")

            # 비-LLM 단계가 실행되었는지 확인
            mock_convert.assert_called_once()
            mock_transcribe.assert_called_once()
            mock_diarize.assert_called_once()
            mock_merge.assert_called_once()

            # 모든 6단계가 completed_steps에 포함
            assert len(state.completed_steps) == 6

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    async def test_correct_skip_passes_merged_as_corrected(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """correct 스킵 시 merged_result가 corrected_result로 패스스루된다."""
        mock_disk.return_value = MagicMock(free=10 * 1024**3)
        mock_mem.return_value = MagicMock(available=1 * 1024**3)

        merged_mock = MagicMock(name="merged_result")

        with (
            patch.object(
                PipelineManager,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=audio_file,
            ),
            patch.object(
                PipelineManager,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=merged_mock,
            ),
            patch.object(
                PipelineManager,
                "_run_step_correct",
                new_callable=AsyncMock,
            ),
            patch.object(
                PipelineManager,
                "_run_step_summarize",
                new_callable=AsyncMock,
            ),
        ):
            pipeline = PipelineManager(
                config=mock_config,
                model_manager=mock_model_manager,
            )
            state = await pipeline.run(
                audio_file,
                meeting_id="passthrough_test",
            )

            # correct이 스킵되었는지 확인
            assert "correct" in state.skipped_steps
            # summarize도 스킵 (메모리 부족)
            assert "summarize" in state.skipped_steps

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    async def test_memory_warning_callback_called(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """메모리 부족 시 경고 콜백이 호출된다."""
        mock_disk.return_value = MagicMock(free=10 * 1024**3)
        mock_mem.return_value = MagicMock(available=1 * 1024**3)
        callback = MagicMock()

        with (
            patch.object(
                PipelineManager,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=audio_file,
            ),
            patch.object(
                PipelineManager,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_correct",
                new_callable=AsyncMock,
            ),
            patch.object(
                PipelineManager,
                "_run_step_summarize",
                new_callable=AsyncMock,
            ),
        ):
            pipeline = PipelineManager(
                config=mock_config,
                model_manager=mock_model_manager,
                on_resource_warning=callback,
            )
            await pipeline.run(audio_file, meeting_id="cb_test")

            # memory_low 콜백 호출 확인
            found_memory_low = any(c[0][1] == "memory_low" for c in callback.call_args_list)
            assert found_memory_low


class TestPipelineManagerNormalResources:
    """리소스 충분 시 정상 실행 테스트."""

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    async def test_normal_resources_runs_all_steps(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """리소스 충분 시 모든 단계가 정상 실행된다."""
        mock_disk.return_value = MagicMock(free=10 * 1024**3)
        mock_mem.return_value = MagicMock(available=8 * 1024**3)

        with (
            patch.object(
                PipelineManager,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=audio_file,
            ) as mock_convert,
            patch.object(
                PipelineManager,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ) as mock_transcribe,
            patch.object(
                PipelineManager,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ) as mock_diarize,
            patch.object(
                PipelineManager,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ) as mock_merge,
            patch.object(
                PipelineManager,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ) as mock_correct,
            patch.object(
                PipelineManager,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ) as mock_summarize,
        ):
            pipeline = PipelineManager(
                config=mock_config,
                model_manager=mock_model_manager,
            )
            state = await pipeline.run(audio_file, meeting_id="normal_test")

            # 모든 단계 실행 확인
            mock_convert.assert_called_once()
            mock_transcribe.assert_called_once()
            mock_diarize.assert_called_once()
            mock_merge.assert_called_once()
            mock_correct.assert_called_once()
            mock_summarize.assert_called_once()

            # 정상 완료 상태
            assert state.status == "completed"
            assert state.degraded is False
            assert state.skipped_steps == []
            assert len(state.completed_steps) == 6

    @patch("core.pipeline.psutil.virtual_memory")
    @patch("core.pipeline.shutil.disk_usage")
    async def test_normal_resources_no_callback(
        self,
        mock_disk: MagicMock,
        mock_mem: MagicMock,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """리소스 충분 시 경고 콜백이 호출되지 않는다."""
        mock_disk.return_value = MagicMock(free=10 * 1024**3)
        mock_mem.return_value = MagicMock(available=8 * 1024**3)
        callback = MagicMock()

        with (
            patch.object(
                PipelineManager,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=audio_file,
            ),
            patch.object(
                PipelineManager,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
        ):
            pipeline = PipelineManager(
                config=mock_config,
                model_manager=mock_model_manager,
                on_resource_warning=callback,
            )
            await pipeline.run(audio_file, meeting_id="no_cb_test")

            callback.assert_not_called()
