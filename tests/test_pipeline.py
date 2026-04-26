"""
파이프라인 매니저 테스트 모듈 (Pipeline Manager Test Module)

목적: core/pipeline.py의 PipelineManager 클래스와 관련 유틸리티를 검증한다.
주요 테스트:
    - 전체 파이프라인 정상 실행
    - 체크포인트 기반 재개 (실패 → 재개)
    - 단계별 실패 처리 및 재시도
    - 입력 검증 (파일 없음, 빈 파일)
    - PipelineState 저장/복원
    - PipelineStep 열거형
    - StepResult 데이터 클래스
    - 체크포인트 비활성화 모드
    - 이미 완료된 파이프라인 재실행
의존성: pytest, pytest-asyncio
"""

import json
import os
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.pipeline import (
    PIPELINE_STEPS,
    InvalidInputError,
    PipelineError,
    PipelineManager,
    PipelineState,
    PipelineStep,
    PipelineStepError,
    StepResult,
)

# === 픽스처 ===


@pytest.fixture
def mock_config(tmp_path: Path) -> MagicMock:
    """테스트용 AppConfig 모킹 객체를 생성한다."""
    config = MagicMock()

    # pipeline 설정
    config.pipeline.checkpoint_enabled = True
    config.pipeline.retry_max_count = 2
    config.pipeline.peak_ram_limit_gb = 9.5
    config.pipeline.min_disk_free_gb = 1.0
    config.pipeline.min_memory_free_gb = 2.0
    config.pipeline.skip_llm_steps = False
    # 안정성 개선 기본값: 테스트는 충분히 여유를 둠
    config.pipeline.correct_timeout_seconds = 1800
    config.pipeline.summarize_timeout_seconds = 600
    config.pipeline.llm_lock_acquire_timeout_seconds = 3600
    config.pipeline.llm_recommended_memory_gb = 6.5

    # paths 설정
    config.paths.resolved_outputs_dir = tmp_path / "outputs"
    config.paths.resolved_checkpoints_dir = tmp_path / "checkpoints"

    # audio 설정
    config.audio.sample_rate = 16000
    config.audio.channels = 1
    config.audio.format = "wav"
    config.audio.supported_input_formats = ["wav", "mp3", "m4a"]

    # stt 설정
    config.stt.model_name = "whisper-medium-ko-zeroth"
    config.stt.language = "ko"
    config.stt.beam_size = 5

    # diarization 설정
    config.diarization.model_name = "pyannote/speaker-diarization-3.1"
    config.diarization.device = "cpu"
    config.diarization.min_speakers = 1
    config.diarization.max_speakers = 10
    config.diarization.huggingface_token = "test-token"

    # llm 설정
    config.llm.model_name = "exaone3.5:7.8b-instruct-q4_K_M"
    config.llm.host = "http://127.0.0.1:11434"
    config.llm.temperature = 0.3
    config.llm.max_context_tokens = 8192
    config.llm.correction_batch_size = 10
    config.llm.request_timeout_seconds = 120

    return config


@pytest.fixture
def mock_model_manager() -> MagicMock:
    """테스트용 ModelLoadManager 모킹 객체를 생성한다."""
    return MagicMock()


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    """테스트용 오디오 파일을 생성한다."""
    audio = tmp_path / "test_meeting.m4a"
    audio.write_bytes(b"fake audio content for testing")
    return audio


@pytest.fixture
def pipeline(
    mock_config: MagicMock,
    mock_model_manager: MagicMock,
) -> PipelineManager:
    """테스트용 PipelineManager 인스턴스를 생성한다."""
    return PipelineManager(mock_config, mock_model_manager)


# === 헬퍼 ===


def _make_mock_transcript() -> MagicMock:
    """테스트용 TranscriptResult Mock을 생성한다."""
    result = MagicMock()
    result.segments = [MagicMock(text="안녕하세요", start=0.0, end=2.0)]
    result.full_text = "안녕하세요"
    result.save_checkpoint = MagicMock()
    return result


def _make_mock_diarization() -> MagicMock:
    """테스트용 DiarizationResult Mock을 생성한다."""
    result = MagicMock()
    result.segments = [
        MagicMock(speaker="SPEAKER_00", start=0.0, end=2.0),
    ]
    result.num_speakers = 1
    result.save_checkpoint = MagicMock()
    return result


def _make_mock_merged() -> MagicMock:
    """테스트용 MergedResult Mock을 생성한다."""
    result = MagicMock()
    result.utterances = [
        MagicMock(
            text="안녕하세요",
            speaker="SPEAKER_00",
            start=0.0,
            end=2.0,
        ),
    ]
    result.num_speakers = 1
    result.save_checkpoint = MagicMock()
    return result


def _make_mock_corrected() -> MagicMock:
    """테스트용 CorrectedResult Mock을 생성한다."""
    result = MagicMock()
    result.utterances = [
        MagicMock(
            text="안녕하세요",
            speaker="SPEAKER_00",
            start=0.0,
            end=2.0,
            was_corrected=False,
        ),
    ]
    result.num_speakers = 1
    result.speakers = ["SPEAKER_00"]
    result.audio_path = "/tmp/test.wav"
    result.save_checkpoint = MagicMock()
    return result


def _make_mock_summary() -> MagicMock:
    """테스트용 SummaryResult Mock을 생성한다."""
    result = MagicMock()
    result.markdown = "## 회의록\n- 테스트 회의"
    result.save_checkpoint = MagicMock()
    result.save_markdown = MagicMock()
    return result


# === PipelineStep 열거형 테스트 ===


class TestPipelineStep:
    """PipelineStep 열거형 테스트."""

    def test_step_values(self) -> None:
        """각 단계의 문자열 값이 올바른지 확인한다."""
        assert PipelineStep.CONVERT == "convert"
        assert PipelineStep.TRANSCRIBE == "transcribe"
        assert PipelineStep.DIARIZE == "diarize"
        assert PipelineStep.MERGE == "merge"
        assert PipelineStep.CORRECT == "correct"
        assert PipelineStep.SUMMARIZE == "summarize"

    def test_step_order(self) -> None:
        """PIPELINE_STEPS 순서가 올바른지 확인한다."""
        expected = [
            PipelineStep.CONVERT,
            PipelineStep.TRANSCRIBE,
            PipelineStep.DIARIZE,
            PipelineStep.MERGE,
            PipelineStep.CORRECT,
            PipelineStep.SUMMARIZE,
        ]
        assert expected == PIPELINE_STEPS

    def test_step_count(self) -> None:
        """파이프라인 단계 수가 6개인지 확인한다."""
        assert len(PIPELINE_STEPS) == 6


# === StepResult 테스트 ===


class TestStepResult:
    """StepResult 데이터 클래스 테스트."""

    def test_to_dict(self) -> None:
        """StepResult를 딕셔너리로 변환할 수 있는지 확인한다."""
        result = StepResult(
            step="convert",
            success=True,
            elapsed_seconds=5.2,
        )
        d = result.to_dict()
        assert d["step"] == "convert"
        assert d["success"] is True
        assert d["elapsed_seconds"] == 5.2
        assert d["error_message"] == ""

    def test_failed_step_result(self) -> None:
        """실패한 단계 결과에 에러 메시지가 포함되는지 확인한다."""
        result = StepResult(
            step="transcribe",
            success=False,
            error_message="모델 로드 실패",
        )
        assert result.success is False
        assert "모델 로드 실패" in result.error_message


# === PipelineState 테스트 ===


class TestPipelineState:
    """PipelineState 데이터 클래스 테스트."""

    def test_default_values(self) -> None:
        """기본값이 올바르게 설정되는지 확인한다."""
        state = PipelineState(
            meeting_id="test_meeting",
            audio_path="/tmp/test.m4a",
        )
        assert state.status == "pending"
        assert state.completed_steps == []
        assert state.created_at != ""
        assert state.updated_at != ""

    def test_save_and_load(self, tmp_path: Path) -> None:
        """상태 저장/복원 라운드트립을 확인한다."""
        state = PipelineState(
            meeting_id="test_123",
            audio_path="/tmp/test.m4a",
            status="running",
            completed_steps=["convert", "transcribe"],
            wav_path="/tmp/test_16k.wav",
        )

        state_path = tmp_path / "state.json"
        state.save(state_path)

        # 파일 존재 확인
        assert state_path.exists()

        # 복원
        loaded = PipelineState.from_file(state_path)
        assert loaded.meeting_id == "test_123"
        assert loaded.status == "running"
        assert loaded.completed_steps == ["convert", "transcribe"]
        assert loaded.wav_path == "/tmp/test_16k.wav"

    def test_to_dict(self) -> None:
        """딕셔너리 변환이 올바른지 확인한다."""
        state = PipelineState(
            meeting_id="test",
            audio_path="/tmp/test.wav",
        )
        d = state.to_dict()
        assert "meeting_id" in d
        assert "audio_path" in d
        assert "status" in d
        assert "completed_steps" in d

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        """저장 시 부모 디렉토리를 자동 생성하는지 확인한다."""
        state = PipelineState(
            meeting_id="test",
            audio_path="/tmp/test.wav",
        )
        nested_path = tmp_path / "a" / "b" / "state.json"
        state.save(nested_path)
        assert nested_path.exists()

    def test_korean_text_preservation(self, tmp_path: Path) -> None:
        """한국어 텍스트가 저장/복원 시 보존되는지 확인한다."""
        state = PipelineState(
            meeting_id="회의_테스트",
            audio_path="/tmp/회의녹음.m4a",
            error_message="한국어 에러 메시지",
        )
        state_path = tmp_path / "state.json"
        state.save(state_path)

        loaded = PipelineState.from_file(state_path)
        assert loaded.meeting_id == "회의_테스트"
        assert loaded.error_message == "한국어 에러 메시지"

    def test_atomic_save_no_tmp_file_remains(self, tmp_path: Path) -> None:
        """원자적 저장 후 임시 파일(.tmp)이 남아있지 않은지 확인한다."""
        state = PipelineState(
            meeting_id="atomic_test",
            audio_path="/tmp/test.m4a",
        )
        state_path = tmp_path / "state.json"
        state.save(state_path)

        # 저장 완료 후 .tmp 파일이 남아있으면 안 됨
        tmp_file = state_path.with_suffix(".tmp")
        assert not tmp_file.exists(), ".tmp 파일이 정리되지 않았습니다"
        assert state_path.exists()

    def test_atomic_save_preserves_original_on_write_failure(
        self,
        tmp_path: Path,
    ) -> None:
        """쓰기 실패 시 기존 파일이 보존되는지 확인한다.

        json.dump가 실패하면 원본 파일은 손상되지 않아야 한다.
        """
        state = PipelineState(
            meeting_id="original",
            audio_path="/tmp/test.m4a",
            status="running",
        )
        state_path = tmp_path / "state.json"

        # 먼저 정상적으로 저장
        state.save(state_path)
        original_content = state_path.read_text(encoding="utf-8")

        # json.dump가 실패하도록 모킹
        with (
            patch("core.pipeline.json.dump", side_effect=OSError("쓰기 실패")),
            pytest.raises(IOError, match="쓰기 실패"),
        ):
            state.save(state_path)

        # 원본 파일이 그대로 보존되어야 함
        assert state_path.exists(), "원본 파일이 삭제되었습니다"
        preserved_content = state_path.read_text(encoding="utf-8")
        assert preserved_content == original_content, "원본 파일 내용이 변경되었습니다"

        # 임시 파일이 남아있지 않아야 함
        tmp_file = state_path.with_suffix(".tmp")
        assert not tmp_file.exists(), "실패 후 .tmp 파일이 정리되지 않았습니다"

    def test_atomic_save_uses_fsync(self, tmp_path: Path) -> None:
        """저장 시 fsync가 호출되는지 확인한다 (디스크 플러시 보장)."""
        state = PipelineState(
            meeting_id="fsync_test",
            audio_path="/tmp/test.m4a",
        )
        state_path = tmp_path / "state.json"

        with patch("core.pipeline.os.fsync") as mock_fsync:
            state.save(state_path)
            # fsync가 최소 1회 호출되어야 함
            assert mock_fsync.called, "os.fsync가 호출되지 않았습니다"

    def test_atomic_save_uses_os_replace(self, tmp_path: Path) -> None:
        """저장 시 os.replace로 원자적 교체가 수행되는지 확인한다."""
        state = PipelineState(
            meeting_id="replace_test",
            audio_path="/tmp/test.m4a",
        )
        state_path = tmp_path / "state.json"

        with patch("core.pipeline.os.replace", wraps=os.replace) as mock_replace:
            state.save(state_path)
            # os.replace가 호출되어야 함
            assert mock_replace.called, "os.replace가 호출되지 않았습니다"
            # 인자 확인: (임시 파일 경로, 최종 파일 경로)
            call_args = mock_replace.call_args[0]
            assert call_args[0].endswith(".tmp"), "os.replace의 소스가 .tmp 파일이 아닙니다"
            assert call_args[1] == str(state_path), "os.replace의 대상이 올바르지 않습니다"


# === PipelineManager 입력 검증 테스트 ===


class TestPipelineManagerValidation:
    """PipelineManager 입력 검증 테스트."""

    @pytest.mark.asyncio
    async def test_nonexistent_file(
        self,
        pipeline: PipelineManager,
        tmp_path: Path,
    ) -> None:
        """존재하지 않는 파일에 대해 InvalidInputError를 발생시키는지 확인한다."""
        fake_path = tmp_path / "nonexistent.wav"
        with pytest.raises(InvalidInputError, match="찾을 수 없습니다"):
            await pipeline.run(fake_path)

    @pytest.mark.asyncio
    async def test_empty_file(
        self,
        pipeline: PipelineManager,
        tmp_path: Path,
    ) -> None:
        """빈 파일에 대해 InvalidInputError를 발생시키는지 확인한다."""
        empty = tmp_path / "empty.wav"
        empty.touch()
        with pytest.raises(InvalidInputError, match="비어있습니다"):
            await pipeline.run(empty)

    @pytest.mark.asyncio
    async def test_directory_input(
        self,
        pipeline: PipelineManager,
        tmp_path: Path,
    ) -> None:
        """디렉토리를 입력하면 InvalidInputError를 발생시키는지 확인한다."""
        dir_path = tmp_path / "some_dir"
        dir_path.mkdir()
        with pytest.raises(InvalidInputError, match="파일이 아닙니다"):
            await pipeline.run(dir_path)


# === PipelineManager 초기화 테스트 ===


class TestPipelineManagerInit:
    """PipelineManager 초기화 테스트."""

    def test_init_with_config(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
    ) -> None:
        """설정값이 올바르게 적용되는지 확인한다."""
        pm = PipelineManager(mock_config, mock_model_manager)
        assert pm._checkpoint_enabled is True
        assert pm._retry_max == 2

    def test_meeting_id_generation(
        self,
        pipeline: PipelineManager,
        tmp_path: Path,
    ) -> None:
        """회의 ID가 날짜 + 파일명 기반으로 생성되는지 확인한다."""
        audio = tmp_path / "test_meeting.wav"
        mid = pipeline._generate_meeting_id(audio)
        assert "test_meeting" in mid
        # 타임스탬프 형식 검증 (YYYYMMDD_HHMMSS)
        assert len(mid.split("_")) >= 3


# === PipelineManager 전체 실행 테스트 ===


class TestPipelineManagerRun:
    """PipelineManager 전체 파이프라인 실행 테스트."""

    @pytest.mark.asyncio
    async def test_full_pipeline_success(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """전체 파이프라인이 정상적으로 완료되는지 확인한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        mock_transcript = _make_mock_transcript()
        mock_diarization = _make_mock_diarization()
        mock_merged = _make_mock_merged()
        mock_corrected = _make_mock_corrected()
        mock_summary = _make_mock_summary()

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=mock_transcript,
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=mock_diarization,
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=mock_merged,
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=mock_corrected,
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=mock_summary,
            ),
        ):
            state = await pipeline.run(audio_file, meeting_id="test_run")

        assert state.status == "completed"
        assert len(state.completed_steps) == 6
        assert state.completed_steps == [
            "convert",
            "transcribe",
            "diarize",
            "merge",
            "correct",
            "summarize",
        ]

    @pytest.mark.asyncio
    async def test_step_failure_with_retry(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """단계 실패 시 재시도 후 최종 실패하면 PipelineStepError를 발생시키는지 확인한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                side_effect=RuntimeError("모델 로드 실패"),
            ),
        ):
            with pytest.raises(PipelineStepError) as exc_info:
                await pipeline.run(audio_file, meeting_id="test_fail")

            assert exc_info.value.step == "transcribe"
            assert "재시도" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_step_failure_then_retry_success(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """첫 번째 시도 실패 후 재시도 시 성공하는 경우를 확인한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        mock_transcript = _make_mock_transcript()
        mock_diarization = _make_mock_diarization()
        mock_merged = _make_mock_merged()
        mock_corrected = _make_mock_corrected()
        mock_summary = _make_mock_summary()

        # transcribe: 첫 번째 실패 → 두 번째 성공
        transcribe_mock = AsyncMock(
            side_effect=[
                RuntimeError("일시적 오류"),
                mock_transcript,
            ]
        )

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                transcribe_mock,
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=mock_diarization,
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=mock_merged,
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=mock_corrected,
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=mock_summary,
            ),
        ):
            state = await pipeline.run(audio_file, meeting_id="test_retry")

        assert state.status == "completed"
        assert transcribe_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_state_saved_on_failure(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """실패 시 파이프라인 상태가 저장되는지 확인한다."""
        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                side_effect=RuntimeError("변환 실패"),
            ),
            pytest.raises(PipelineStepError),
        ):
            await pipeline.run(
                audio_file,
                meeting_id="test_save_fail",
            )

        state_path = pipeline._get_state_path("test_save_fail")
        assert state_path.exists()

        state = PipelineState.from_file(state_path)
        assert state.status == "failed"
        assert "변환 실패" in state.error_message

    @pytest.mark.asyncio
    async def test_custom_meeting_id(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """사용자 지정 meeting_id가 적용되는지 확인한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        mock_transcript = _make_mock_transcript()
        mock_diarization = _make_mock_diarization()
        mock_merged = _make_mock_merged()
        mock_corrected = _make_mock_corrected()
        mock_summary = _make_mock_summary()

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=mock_transcript,
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=mock_diarization,
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=mock_merged,
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=mock_corrected,
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=mock_summary,
            ),
        ):
            state = await pipeline.run(
                audio_file,
                meeting_id="custom_id_001",
            )

        assert state.meeting_id == "custom_id_001"


# === 체크포인트 재개 테스트 ===


class TestPipelineResume:
    """파이프라인 재개 기능 테스트."""

    @pytest.mark.asyncio
    async def test_resume_from_failed_step(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """실패한 단계부터 재개할 수 있는지 확인한다."""
        meeting_id = "test_resume"
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        # 먼저 convert 까지 성공 상태를 저장
        state = PipelineState(
            meeting_id=meeting_id,
            audio_path=str(audio_file),
            status="failed",
            completed_steps=["convert"],
            wav_path=str(wav_path),
            output_dir=str(pipeline._get_output_dir(meeting_id)),
        )
        state_path = pipeline._get_state_path(meeting_id)
        state.save(state_path)

        # 출력 디렉토리 생성
        pipeline._get_output_dir(meeting_id).mkdir(
            parents=True,
            exist_ok=True,
        )

        mock_transcript = _make_mock_transcript()
        mock_diarization = _make_mock_diarization()
        mock_merged = _make_mock_merged()
        mock_corrected = _make_mock_corrected()
        mock_summary = _make_mock_summary()

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
            ) as convert_mock,
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=mock_transcript,
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=mock_diarization,
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=mock_merged,
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=mock_corrected,
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=mock_summary,
            ),
        ):
            result = await pipeline.resume(meeting_id)

        assert result.status == "completed"
        # convert는 이미 완료되었으므로 다시 호출되지 않아야 함
        convert_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_nonexistent_meeting(
        self,
        pipeline: PipelineManager,
    ) -> None:
        """존재하지 않는 meeting_id로 재개 시 PipelineError를 발생시키는지 확인한다."""
        with pytest.raises(PipelineError, match="찾을 수 없습니다"):
            await pipeline.resume("nonexistent_meeting")

    @pytest.mark.asyncio
    async def test_resume_all_completed(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """모든 단계가 완료된 파이프라인은 즉시 완료 상태를 반환하는지 확인한다."""
        meeting_id = "test_all_done"

        state = PipelineState(
            meeting_id=meeting_id,
            audio_path=str(audio_file),
            status="completed",
            completed_steps=[
                "convert",
                "transcribe",
                "diarize",
                "merge",
                "correct",
                "summarize",
            ],
            wav_path=str(audio_file),
            output_dir=str(pipeline._get_output_dir(meeting_id)),
        )
        state_path = pipeline._get_state_path(meeting_id)
        state.save(state_path)

        # 출력 디렉토리 생성
        pipeline._get_output_dir(meeting_id).mkdir(
            parents=True,
            exist_ok=True,
        )

        result = await pipeline.resume(meeting_id)
        assert result.status == "completed"


# === 체크포인트 비활성화 테스트 ===


class TestCheckpointDisabled:
    """체크포인트 비활성화 모드 테스트."""

    @pytest.mark.asyncio
    async def test_no_checkpoint_saving(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """체크포인트 비활성화 시 체크포인트 파일이 생성되지 않는지 확인한다."""
        mock_config.pipeline.checkpoint_enabled = False
        pm = PipelineManager(mock_config, mock_model_manager)

        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        mock_transcript = _make_mock_transcript()
        mock_diarization = _make_mock_diarization()
        mock_merged = _make_mock_merged()
        mock_corrected = _make_mock_corrected()
        mock_summary = _make_mock_summary()

        with (
            patch.object(
                pm,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pm,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=mock_transcript,
            ),
            patch.object(
                pm,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=mock_diarization,
            ),
            patch.object(
                pm,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=mock_merged,
            ),
            patch.object(
                pm,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=mock_corrected,
            ),
            patch.object(
                pm,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=mock_summary,
            ),
        ):
            state = await pm.run(audio_file, meeting_id="no_cp")

        assert state.status == "completed"


# === _find_resume_step 테스트 ===


class TestFindResumeStep:
    """_find_resume_step 내부 메서드 테스트."""

    def test_empty_completed(
        self,
        pipeline: PipelineManager,
    ) -> None:
        """완료된 단계가 없으면 인덱스 0을 반환하는지 확인한다."""
        state = PipelineState(
            meeting_id="test",
            audio_path="/tmp/test.wav",
            completed_steps=[],
        )
        assert pipeline._find_resume_step(state) == 0

    def test_one_step_completed(
        self,
        pipeline: PipelineManager,
    ) -> None:
        """convert만 완료되었으면 인덱스 1(transcribe)을 반환하는지 확인한다."""
        state = PipelineState(
            meeting_id="test",
            audio_path="/tmp/test.wav",
            completed_steps=["convert"],
        )
        assert pipeline._find_resume_step(state) == 1

    def test_three_steps_completed(
        self,
        pipeline: PipelineManager,
    ) -> None:
        """3단계 완료 시 인덱스 3(merge)을 반환하는지 확인한다."""
        state = PipelineState(
            meeting_id="test",
            audio_path="/tmp/test.wav",
            completed_steps=["convert", "transcribe", "diarize"],
        )
        assert pipeline._find_resume_step(state) == 3

    def test_all_completed(
        self,
        pipeline: PipelineManager,
    ) -> None:
        """모든 단계 완료 시 None을 반환하는지 확인한다."""
        state = PipelineState(
            meeting_id="test",
            audio_path="/tmp/test.wav",
            completed_steps=[
                "convert",
                "transcribe",
                "diarize",
                "merge",
                "correct",
                "summarize",
            ],
        )
        assert pipeline._find_resume_step(state) is None


# === get_status 테스트 ===


class TestGetStatus:
    """get_status 메서드 테스트."""

    def test_get_existing_status(
        self,
        pipeline: PipelineManager,
    ) -> None:
        """기존 상태 파일이 있을 때 정상 조회되는지 확인한다."""
        meeting_id = "test_status"
        state = PipelineState(
            meeting_id=meeting_id,
            audio_path="/tmp/test.wav",
            status="running",
        )
        state_path = pipeline._get_state_path(meeting_id)
        state.save(state_path)

        result = pipeline.get_status(meeting_id)
        assert result is not None
        assert result.status == "running"
        assert result.meeting_id == meeting_id

    def test_get_nonexistent_status(
        self,
        pipeline: PipelineManager,
    ) -> None:
        """상태 파일이 없으면 None을 반환하는지 확인한다."""
        result = pipeline.get_status("nonexistent")
        assert result is None

    def test_get_corrupted_status(
        self,
        pipeline: PipelineManager,
        tmp_path: Path,
        mock_config: MagicMock,
    ) -> None:
        """손상된 상태 파일에 대해 None을 반환하는지 확인한다."""
        meeting_id = "corrupted"
        state_path = pipeline._get_state_path(meeting_id)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{ invalid json", encoding="utf-8")

        result = pipeline.get_status(meeting_id)
        assert result is None


# === 에러 계층 테스트 ===


class TestErrorHierarchy:
    """에러 계층 테스트."""

    def test_pipeline_error_base(self) -> None:
        """PipelineError가 Exception의 하위 클래스인지 확인한다."""
        assert issubclass(PipelineError, Exception)

    def test_pipeline_step_error(self) -> None:
        """PipelineStepError에 step 속성이 있는지 확인한다."""
        err = PipelineStepError("convert", "변환 실패")
        assert err.step == "convert"
        assert "convert" in str(err)
        assert "변환 실패" in str(err)

    def test_invalid_input_error(self) -> None:
        """InvalidInputError가 PipelineError의 하위 클래스인지 확인한다."""
        assert issubclass(InvalidInputError, PipelineError)


# === 경로 생성 테스트 ===


class TestPathGeneration:
    """경로 생성 메서드 테스트."""

    def test_checkpoint_path(
        self,
        pipeline: PipelineManager,
    ) -> None:
        """체크포인트 파일 경로가 올바르게 생성되는지 확인한다."""
        path = pipeline._get_checkpoint_path(
            "meeting_001",
            PipelineStep.TRANSCRIBE,
        )
        assert "meeting_001" in str(path)
        assert "transcribe.json" in str(path)

    def test_state_path(
        self,
        pipeline: PipelineManager,
    ) -> None:
        """상태 파일 경로가 올바르게 생성되는지 확인한다."""
        path = pipeline._get_state_path("meeting_001")
        assert "meeting_001" in str(path)
        assert "pipeline_state.json" in str(path)

    def test_output_dir(
        self,
        pipeline: PipelineManager,
    ) -> None:
        """출력 디렉토리 경로가 올바르게 생성되는지 확인한다."""
        path = pipeline._get_output_dir("meeting_001")
        assert "meeting_001" in str(path)
        assert "outputs" in str(path)


# === 단계별 실행 단위 테스트 ===


class TestIndividualSteps:
    """개별 파이프라인 단계의 단위 테스트."""

    @pytest.mark.asyncio
    async def test_run_step_convert(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
        tmp_path: Path,
    ) -> None:
        """변환 단계가 AudioConverter를 호출하는지 확인한다."""
        output_dir = tmp_path / "output"
        expected_wav = output_dir / "test_16k.wav"

        mock_converter = MagicMock()
        mock_converter.convert_async = AsyncMock(return_value=expected_wav)

        with patch(
            "steps.audio_converter.AudioConverter",
            return_value=mock_converter,
        ):
            result = await pipeline._run_step_convert(
                audio_file,
                output_dir,
            )

        assert result == expected_wav
        mock_converter.convert_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_step_transcribe_with_checkpoint(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
        tmp_path: Path,
    ) -> None:
        """체크포인트가 있으면 전사를 건너뛰는지 확인한다."""
        checkpoint_path = tmp_path / "transcribe.json"

        mock_result = _make_mock_transcript()

        with patch("steps.transcriber.TranscriptResult") as MockTranscript:
            MockTranscript.from_checkpoint.return_value = mock_result
            # 체크포인트 파일 생성
            checkpoint_path.write_text(
                '{"segments": [], "full_text": "", "language": "ko", "audio_path": ""}',
                encoding="utf-8",
            )

            result = await pipeline._run_step_transcribe(
                audio_file,
                checkpoint_path,
            )

        assert result == mock_result

    @pytest.mark.asyncio
    async def test_run_step_summarize_saves_markdown(
        self,
        pipeline: PipelineManager,
        tmp_path: Path,
    ) -> None:
        """요약 단계가 마크다운 파일도 저장하는지 확인한다."""
        mock_corrected = _make_mock_corrected()
        mock_summary = _make_mock_summary()
        checkpoint_path = tmp_path / "summarize.json"
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        mock_summarizer = MagicMock()
        mock_summarizer.summarize = AsyncMock(return_value=mock_summary)

        with patch(
            "steps.summarizer.Summarizer",
            return_value=mock_summarizer,
        ):
            result = await pipeline._run_step_summarize(
                mock_corrected,
                checkpoint_path,
                output_dir,
            )

        assert result == mock_summary
        # save_checkpoint과 save_markdown 모두 호출되어야 함
        mock_summary.save_checkpoint.assert_called_once()
        mock_summary.save_markdown.assert_called_once()


# === 상태 전이 테스트 ===


class TestStateTransitions:
    """파이프라인 상태 전이 테스트."""

    @pytest.mark.asyncio
    async def test_pending_to_running(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """파이프라인 시작 시 pending → running 전이를 확인한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        states_captured: list[str] = []

        original_save = PipelineState.save

        def capture_save(self_state: PipelineState, path: Path) -> None:
            states_captured.append(self_state.status)
            original_save(self_state, path)

        mock_transcript = _make_mock_transcript()
        mock_diarization = _make_mock_diarization()
        mock_merged = _make_mock_merged()
        mock_corrected = _make_mock_corrected()
        mock_summary = _make_mock_summary()

        with (
            patch.object(PipelineState, "save", capture_save),
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=mock_transcript,
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=mock_diarization,
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=mock_merged,
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=mock_corrected,
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=mock_summary,
            ),
        ):
            await pipeline.run(audio_file, meeting_id="test_transition")

        # 첫 save는 "running" 상태여야 함
        assert states_captured[0] == "running"
        # 마지막 save는 "completed" 상태여야 함
        assert states_captured[-1] == "completed"

    @pytest.mark.asyncio
    async def test_running_to_failed(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """실패 시 running → failed 전이를 확인한다."""
        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                side_effect=RuntimeError("변환 실패"),
            ),
            pytest.raises(PipelineStepError),
        ):
            await pipeline.run(
                audio_file,
                meeting_id="test_to_failed",
            )

        state = pipeline.get_status("test_to_failed")
        assert state is not None
        assert state.status == "failed"


# ===================================================================
# Phase 1 통합 테스트 (P1-11)
# ===================================================================
# 외부 의존성(ffmpeg, mlx-whisper, pyannote, Ollama)만 모킹하고,
# 실제 모듈 인터페이스 간 데이터 흐름을 검증한다.
# ===================================================================


# === E2E 통합 테스트 헬퍼 ===


def _make_real_transcript_result(audio_path: str = "/tmp/test.wav"):
    """실제 TranscriptResult 인스턴스를 생성한다 (모킹 아닌 실제 dataclass)."""
    from steps.transcriber import TranscriptResult, TranscriptSegment

    segments = [
        TranscriptSegment(
            text="안녕하세요 오늘 회의를 시작하겠습니다",
            start=0.0,
            end=3.5,
            avg_logprob=-0.3,
            no_speech_prob=0.01,
        ),
        TranscriptSegment(
            text="네 좋습니다 진행해주세요",
            start=3.8,
            end=5.5,
            avg_logprob=-0.25,
            no_speech_prob=0.02,
        ),
        TranscriptSegment(
            text="첫 번째 안건은 프로젝트 일정입니다",
            start=6.0,
            end=9.0,
            avg_logprob=-0.2,
            no_speech_prob=0.01,
        ),
    ]
    return TranscriptResult(
        segments=segments,
        full_text="안녕하세요 오늘 회의를 시작하겠습니다 "
        "네 좋습니다 진행해주세요 "
        "첫 번째 안건은 프로젝트 일정입니다",
        language="ko",
        audio_path=audio_path,
    )


def _make_real_diarization_result(audio_path: str = "/tmp/test.wav"):
    """실제 DiarizationResult 인스턴스를 생성한다."""
    from steps.diarizer import DiarizationResult, DiarizationSegment

    segments = [
        DiarizationSegment(speaker="SPEAKER_00", start=0.0, end=3.5),
        DiarizationSegment(speaker="SPEAKER_01", start=3.8, end=5.5),
        DiarizationSegment(speaker="SPEAKER_00", start=6.0, end=9.0),
    ]
    return DiarizationResult(
        segments=segments,
        num_speakers=2,
        audio_path=audio_path,
    )


def _make_real_merged_result(audio_path: str = "/tmp/test.wav"):
    """실제 MergedResult 인스턴스를 생성한다."""
    from steps.merger import MergedResult, MergedUtterance

    utterances = [
        MergedUtterance(
            text="안녕하세요 오늘 회의를 시작하겠습니다",
            speaker="SPEAKER_00",
            start=0.0,
            end=3.5,
        ),
        MergedUtterance(
            text="네 좋습니다 진행해주세요",
            speaker="SPEAKER_01",
            start=3.8,
            end=5.5,
        ),
        MergedUtterance(
            text="첫 번째 안건은 프로젝트 일정입니다",
            speaker="SPEAKER_00",
            start=6.0,
            end=9.0,
        ),
    ]
    return MergedResult(
        utterances=utterances,
        num_speakers=2,
        audio_path=audio_path,
        unknown_count=0,
    )


def _make_real_corrected_result(audio_path: str = "/tmp/test.wav"):
    """실제 CorrectedResult 인스턴스를 생성한다."""
    from steps.corrector import CorrectedResult, CorrectedUtterance

    utterances = [
        CorrectedUtterance(
            text="안녕하세요, 오늘 회의를 시작하겠습니다.",
            original_text="안녕하세요 오늘 회의를 시작하겠습니다",
            speaker="SPEAKER_00",
            start=0.0,
            end=3.5,
            was_corrected=True,
        ),
        CorrectedUtterance(
            text="네, 좋습니다. 진행해 주세요.",
            original_text="네 좋습니다 진행해주세요",
            speaker="SPEAKER_01",
            start=3.8,
            end=5.5,
            was_corrected=True,
        ),
        CorrectedUtterance(
            text="첫 번째 안건은 프로젝트 일정입니다.",
            original_text="첫 번째 안건은 프로젝트 일정입니다",
            speaker="SPEAKER_00",
            start=6.0,
            end=9.0,
            was_corrected=True,
        ),
    ]
    return CorrectedResult(
        utterances=utterances,
        num_speakers=2,
        audio_path=audio_path,
        total_corrected=3,
        total_failed=0,
    )


def _make_real_summary_result(audio_path: str = "/tmp/test.wav"):
    """실제 SummaryResult 인스턴스를 생성한다."""
    from steps.summarizer import SummaryResult

    return SummaryResult(
        markdown=(
            "# 회의록\n\n"
            "## 참석자\n- SPEAKER_00\n- SPEAKER_01\n\n"
            "## 주요 안건\n- 프로젝트 일정 논의\n\n"
            "## 결정 사항\n- 없음\n\n"
            "## 액션 아이템\n- 없음\n"
        ),
        audio_path=audio_path,
        num_speakers=2,
        speakers=["SPEAKER_00", "SPEAKER_01"],
        num_utterances=3,
        was_chunked=False,
        chunk_count=1,
    )


# === E2E 전체 파이프라인 통합 테스트 ===


class TestE2EFullPipeline:
    """Phase 1 전체 파이프라인 E2E 통합 테스트.

    외부 의존성(ffmpeg, mlx-whisper, pyannote, Ollama)만 모킹하고,
    실제 데이터 클래스를 사용하여 단계 간 데이터 흐름을 검증한다.
    """

    @pytest.mark.asyncio
    async def test_e2e_full_pipeline_with_real_data_classes(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """실제 데이터 클래스를 사용한 전체 파이프라인 E2E 테스트.

        각 단계의 실제 Result 인스턴스가 올바르게 생성되고
        다음 단계로 전달되는지 검증한다.
        """
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        real_transcript = _make_real_transcript_result(str(wav_path))
        real_diarization = _make_real_diarization_result(str(wav_path))
        real_merged = _make_real_merged_result(str(wav_path))
        real_corrected = _make_real_corrected_result(str(wav_path))
        real_summary = _make_real_summary_result(str(wav_path))

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=real_transcript,
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=real_diarization,
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=real_merged,
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=real_corrected,
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=real_summary,
            ),
        ):
            state = await pipeline.run(audio_file, meeting_id="e2e_full")

        assert state.status == "completed"
        assert len(state.completed_steps) == 6
        assert state.step_results[-1]["success"] is True

    @pytest.mark.asyncio
    async def test_e2e_checkpoint_roundtrip(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
        tmp_path: Path,
    ) -> None:
        """모든 단계의 체크포인트가 실제로 저장/복원 가능한지 검증한다.

        실제 데이터 클래스를 JSON으로 저장한 뒤 from_checkpoint로
        복원하여 라운드트립이 정상인지 확인한다.
        """
        from steps.corrector import CorrectedResult
        from steps.diarizer import DiarizationResult
        from steps.merger import MergedResult
        from steps.summarizer import SummaryResult
        from steps.transcriber import TranscriptResult

        checkpoint_dir = tmp_path / "checkpoints"
        checkpoint_dir.mkdir()

        # 각 데이터 클래스의 체크포인트 저장/복원
        transcript = _make_real_transcript_result()
        tp = checkpoint_dir / "transcribe.json"
        transcript.save_checkpoint(tp)
        restored_t = TranscriptResult.from_checkpoint(tp)
        assert len(restored_t.segments) == 3
        assert restored_t.segments[0].text == "안녕하세요 오늘 회의를 시작하겠습니다"
        assert restored_t.language == "ko"

        diarization = _make_real_diarization_result()
        dp = checkpoint_dir / "diarize.json"
        diarization.save_checkpoint(dp)
        restored_d = DiarizationResult.from_checkpoint(dp)
        assert len(restored_d.segments) == 3
        assert restored_d.num_speakers == 2

        merged = _make_real_merged_result()
        mp = checkpoint_dir / "merge.json"
        merged.save_checkpoint(mp)
        restored_m = MergedResult.from_checkpoint(mp)
        assert len(restored_m.utterances) == 3
        assert restored_m.utterances[0].speaker == "SPEAKER_00"
        assert restored_m.unknown_count == 0

        corrected = _make_real_corrected_result()
        cp = checkpoint_dir / "correct.json"
        corrected.save_checkpoint(cp)
        restored_c = CorrectedResult.from_checkpoint(cp)
        assert len(restored_c.utterances) == 3
        assert restored_c.total_corrected == 3
        assert restored_c.utterances[0].was_corrected is True

        summary = _make_real_summary_result()
        sp = checkpoint_dir / "summarize.json"
        summary.save_checkpoint(sp)
        restored_s = SummaryResult.from_checkpoint(sp)
        assert "회의록" in restored_s.markdown
        assert restored_s.num_speakers == 2
        assert restored_s.speakers == ["SPEAKER_00", "SPEAKER_01"]

    @pytest.mark.asyncio
    async def test_e2e_step_results_recorded(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """각 단계의 실행 결과(소요 시간, 성공 여부)가 기록되는지 확인한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=_make_real_transcript_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=_make_real_diarization_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_real_merged_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_real_corrected_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_real_summary_result(),
            ),
        ):
            state = await pipeline.run(
                audio_file,
                meeting_id="e2e_results",
            )

        # 6개 단계 모두 결과가 기록되어야 함
        assert len(state.step_results) == 6
        for i, step_name in enumerate(
            [
                "convert",
                "transcribe",
                "diarize",
                "merge",
                "correct",
                "summarize",
            ]
        ):
            assert state.step_results[i]["step"] == step_name
            assert state.step_results[i]["success"] is True
            assert state.step_results[i]["elapsed_seconds"] >= 0


# === E2E 체크포인트 기반 재개 통합 테스트 ===


class TestE2ECheckpointResume:
    """체크포인트 기반 파이프라인 재개 E2E 통합 테스트."""

    @pytest.mark.asyncio
    async def test_e2e_resume_from_merge_step(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """merge 단계에서 실패 후 재개할 때 이전 3단계를 건너뛰는지 확인한다."""
        meeting_id = "e2e_resume_merge"
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        output_dir = pipeline._get_output_dir(meeting_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        # transcribe/diarize 체크포인트 생성 (재개 시 복원용)
        transcript = _make_real_transcript_result(str(wav_path))
        transcript.save_checkpoint(
            pipeline._get_checkpoint_path(
                meeting_id,
                PipelineStep.TRANSCRIBE,
            ),
        )
        diarization = _make_real_diarization_result(str(wav_path))
        diarization.save_checkpoint(
            pipeline._get_checkpoint_path(
                meeting_id,
                PipelineStep.DIARIZE,
            ),
        )

        # convert/transcribe/diarize 완료된 상태로 저장
        state = PipelineState(
            meeting_id=meeting_id,
            audio_path=str(audio_file),
            status="failed",
            completed_steps=["convert", "transcribe", "diarize"],
            wav_path=str(wav_path),
            output_dir=str(output_dir),
            error_message="병합 실패",
        )
        state.save(pipeline._get_state_path(meeting_id))

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
            ) as convert_mock,
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
            ) as transcribe_mock,
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
            ) as diarize_mock,
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_real_merged_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_real_corrected_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_real_summary_result(),
            ),
        ):
            result = await pipeline.resume(meeting_id)

        # 이미 완료된 단계는 다시 실행하지 않아야 함
        convert_mock.assert_not_called()
        transcribe_mock.assert_not_called()
        diarize_mock.assert_not_called()

        assert result.status == "completed"
        assert len(result.completed_steps) == 6

    @pytest.mark.asyncio
    async def test_e2e_failed_state_persists(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """파이프라인 실패 시 상태가 올바르게 저장되고 재개 가능한지 확인한다."""
        meeting_id = "e2e_fail_persist"
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        # 1단계: convert 성공 → transcribe에서 실패
        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                side_effect=RuntimeError("STT 모델 로드 실패"),
            ),
            pytest.raises(PipelineStepError),
        ):
            await pipeline.run(audio_file, meeting_id=meeting_id)

        # 실패 상태 확인
        failed_state = pipeline.get_status(meeting_id)
        assert failed_state is not None
        assert failed_state.status == "failed"
        assert "convert" in failed_state.completed_steps
        assert "transcribe" not in failed_state.completed_steps
        assert failed_state.wav_path != ""

        # 2단계: 재개하여 나머지 단계 완료
        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
            ) as convert_mock,
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=_make_real_transcript_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=_make_real_diarization_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_real_merged_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_real_corrected_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_real_summary_result(),
            ),
        ):
            result = await pipeline.resume(meeting_id)

        # convert는 이미 완료되었으므로 건너뜀
        convert_mock.assert_not_called()
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_e2e_state_file_integrity(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """파이프라인 완료 후 상태 파일의 무결성을 검증한다."""
        meeting_id = "e2e_integrity"
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=_make_real_transcript_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=_make_real_diarization_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_real_merged_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_real_corrected_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_real_summary_result(),
            ),
        ):
            await pipeline.run(audio_file, meeting_id=meeting_id)

        # 상태 파일 직접 읽어서 JSON 무결성 검증
        state_path = pipeline._get_state_path(meeting_id)
        assert state_path.exists()

        with open(state_path, encoding="utf-8") as f:
            raw_data = json.load(f)

        assert raw_data["meeting_id"] == meeting_id
        assert raw_data["status"] == "completed"
        assert len(raw_data["completed_steps"]) == 6
        assert raw_data["wav_path"] != ""
        assert raw_data["created_at"] != ""
        assert raw_data["updated_at"] != ""
        assert raw_data["error_message"] == ""


# === E2E 한국어 텍스트 보존 테스트 ===


class TestE2EKoreanTextPreservation:
    """한국어 텍스트가 파이프라인 전체를 통과한 뒤에도 보존되는지 검증한다."""

    @pytest.mark.asyncio
    async def test_e2e_korean_nfc_roundtrip(
        self,
        tmp_path: Path,
    ) -> None:
        """한국어 텍스트의 NFC 정규화 및 체크포인트 라운드트립을 검증한다."""
        import unicodedata

        from steps.corrector import CorrectedResult, CorrectedUtterance
        from steps.merger import MergedResult, MergedUtterance
        from steps.transcriber import TranscriptResult, TranscriptSegment

        # NFD 형식의 한국어 텍스트 (조합형)
        _nfd_text = unicodedata.normalize("NFD", "안녕하세요")
        # NFC 형식 (완성형)
        nfc_text = unicodedata.normalize("NFC", "안녕하세요")

        # TranscriptResult에 NFC 텍스트 저장 → 체크포인트 → 복원
        transcript = TranscriptResult(
            segments=[
                TranscriptSegment(text=nfc_text, start=0.0, end=2.0),
            ],
            full_text=nfc_text,
            language="ko",
            audio_path="/tmp/test.wav",
        )
        tp = tmp_path / "t.json"
        transcript.save_checkpoint(tp)
        restored = TranscriptResult.from_checkpoint(tp)
        assert restored.segments[0].text == nfc_text
        assert unicodedata.is_normalized("NFC", restored.segments[0].text)

        # MergedResult 라운드트립
        merged = MergedResult(
            utterances=[
                MergedUtterance(
                    text=nfc_text,
                    speaker="화자_01",
                    start=0.0,
                    end=2.0,
                ),
            ],
            num_speakers=1,
            audio_path="/tmp/test.wav",
        )
        mp = tmp_path / "m.json"
        merged.save_checkpoint(mp)
        restored_m = MergedResult.from_checkpoint(mp)
        assert restored_m.utterances[0].text == nfc_text
        assert restored_m.utterances[0].speaker == "화자_01"

        # CorrectedResult 라운드트립
        corrected = CorrectedResult(
            utterances=[
                CorrectedUtterance(
                    text=nfc_text + ".",
                    original_text=nfc_text,
                    speaker="화자_01",
                    start=0.0,
                    end=2.0,
                    was_corrected=True,
                ),
            ],
            num_speakers=1,
            audio_path="/tmp/test.wav",
            total_corrected=1,
        )
        cp = tmp_path / "c.json"
        corrected.save_checkpoint(cp)
        restored_c = CorrectedResult.from_checkpoint(cp)
        assert restored_c.utterances[0].original_text == nfc_text

    @pytest.mark.asyncio
    async def test_e2e_korean_meeting_id_pipeline(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """한국어 문자가 포함된 상태에서도 파이프라인이 정상 동작하는지 확인한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        korean_text = "한국어 전사 테스트 발화입니다"
        from steps.summarizer import SummaryResult

        summary = SummaryResult(
            markdown=f"# 회의록\n\n{korean_text}",
            audio_path=str(wav_path),
            num_speakers=1,
            speakers=["SPEAKER_00"],
            num_utterances=1,
        )

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=_make_real_transcript_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=_make_real_diarization_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_real_merged_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_real_corrected_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=summary,
            ),
        ):
            state = await pipeline.run(
                audio_file,
                meeting_id="e2e_korean_test",
            )

        assert state.status == "completed"
        # 상태 파일 복원 후 한국어 보존 확인
        loaded = PipelineState.from_file(
            pipeline._get_state_path("e2e_korean_test"),
        )
        assert loaded.meeting_id == "e2e_korean_test"


# === E2E 데이터 흐름 통합 테스트 ===


class TestE2EDataFlowIntegration:
    """실제 Merger 로직을 사용하여 데이터 흐름 연동을 검증한다."""

    @pytest.mark.asyncio
    async def test_merger_with_real_dataclasses(self) -> None:
        """실제 TranscriptResult + DiarizationResult → Merger → MergedResult.

        Merger는 외부 의존성이 없으므로 모킹 없이 실제 로직으로 검증한다.
        """
        from steps.merger import Merger

        transcript = _make_real_transcript_result()
        diarization = _make_real_diarization_result()

        merger = Merger()
        result = await merger.merge(transcript, diarization)

        # 병합 결과 검증
        assert len(result.utterances) == 3
        assert result.num_speakers == 2
        assert result.utterances[0].speaker == "SPEAKER_00"
        assert result.utterances[1].speaker == "SPEAKER_01"
        assert result.utterances[2].speaker == "SPEAKER_00"

        # 텍스트 보존 확인
        assert result.utterances[0].text == "안녕하세요 오늘 회의를 시작하겠습니다"
        assert result.utterances[1].text == "네 좋습니다 진행해주세요"

    @pytest.mark.asyncio
    async def test_merger_output_checkpoint_to_corrector_input(
        self,
        tmp_path: Path,
    ) -> None:
        """Merger 출력 → 체크포인트 저장 → 복원 → Corrector 입력 형식 검증."""
        from steps.merger import MergedResult

        merged = _make_real_merged_result()
        cp_path = tmp_path / "merge.json"
        merged.save_checkpoint(cp_path)

        # 체크포인트에서 복원
        restored = MergedResult.from_checkpoint(cp_path)

        # Corrector가 기대하는 인터페이스 검증
        for u in restored.utterances:
            assert hasattr(u, "text")
            assert hasattr(u, "speaker")
            assert hasattr(u, "start")
            assert hasattr(u, "end")
            assert isinstance(u.text, str)
            assert isinstance(u.speaker, str)

    @pytest.mark.asyncio
    async def test_summary_result_saves_markdown_file(
        self,
        tmp_path: Path,
    ) -> None:
        """SummaryResult가 마크다운 파일을 올바르게 저장하는지 확인한다."""
        summary = _make_real_summary_result()
        md_path = tmp_path / "meeting_minutes.md"
        summary.save_markdown(md_path)

        assert md_path.exists()
        content = md_path.read_text(encoding="utf-8")
        assert "회의록" in content
        assert "SPEAKER_00" in content
        assert "프로젝트 일정" in content


# === E2E 보안 디렉토리 연동 테스트 ===


class TestE2ESecurityIntegration:
    """보안 모듈(secure_dir)과 파이프라인의 연동 테스트."""

    @pytest.mark.asyncio
    async def test_secure_dir_manager_setup(
        self,
        tmp_path: Path,
    ) -> None:
        """SecureDirManager가 출력 디렉토리를 올바르게 보호하는지 확인한다."""
        from security.secure_dir import SecureDirManager

        mock_config = MagicMock()
        mock_config.paths.resolved_base_dir = tmp_path / "data"
        mock_config.paths.resolved_audio_input_dir = tmp_path / "data" / "audio"
        mock_config.paths.resolved_outputs_dir = tmp_path / "data" / "outputs"
        mock_config.paths.resolved_checkpoints_dir = tmp_path / "data" / "checkpoints"
        mock_config.paths.resolved_chroma_db_dir = tmp_path / "data" / "chroma_db"
        # SecureDirManager.__init__이 접근하는 실제 속성명 사용
        mock_config.security.data_dir_permissions = 0o700
        mock_config.security.exclude_from_spotlight = True
        mock_config.security.exclude_from_timemachine = False

        manager = SecureDirManager(mock_config)

        # chmod + subprocess.run 모두 모킹하여 OS 의존성 제거
        with (
            patch("subprocess.run"),
            patch("pathlib.Path.chmod"),
        ):
            created = manager.ensure_secure_dirs()

        # 디렉토리 생성 확인
        assert (tmp_path / "data").exists()
        assert (tmp_path / "data" / "outputs").exists()
        assert (tmp_path / "data" / "checkpoints").exists()
        assert len(created) > 0

    @pytest.mark.asyncio
    async def test_pipeline_output_dir_creation(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """파이프라인이 출력 디렉토리를 자동 생성하는지 확인한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        meeting_id = "e2e_dir_test"

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=_make_real_transcript_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=_make_real_diarization_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_real_merged_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_real_corrected_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_real_summary_result(),
            ),
        ):
            _state = await pipeline.run(audio_file, meeting_id=meeting_id)

        # 출력/체크포인트 디렉토리 자동 생성 확인
        output_dir = pipeline._get_output_dir(meeting_id)
        assert output_dir.exists()


# === E2E 에러 전파 체인 테스트 ===


class TestE2EErrorPropagation:
    """각 단계에서 발생하는 에러의 전파 경로를 검증한다."""

    @pytest.mark.asyncio
    async def test_convert_error_propagation(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """convert 단계 에러가 PipelineStepError로 래핑되는지 확인한다."""
        from steps.audio_converter import FFmpegNotFoundError

        with patch.object(
            pipeline,
            "_run_step_convert",
            new_callable=AsyncMock,
            side_effect=FFmpegNotFoundError("ffmpeg 없음"),
        ):
            with pytest.raises(PipelineStepError) as exc_info:
                await pipeline.run(
                    audio_file,
                    meeting_id="e2e_err_convert",
                )
            assert exc_info.value.step == "convert"

    @pytest.mark.asyncio
    async def test_transcribe_error_propagation(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """transcribe 단계 에러가 PipelineStepError로 래핑되는지 확인한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        from steps.transcriber import ModelNotAvailableError

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                side_effect=ModelNotAvailableError("whisper 모델 없음"),
            ),
        ):
            with pytest.raises(PipelineStepError) as exc_info:
                await pipeline.run(
                    audio_file,
                    meeting_id="e2e_err_transcribe",
                )
            assert exc_info.value.step == "transcribe"

    @pytest.mark.asyncio
    async def test_diarize_error_propagation(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """diarize 단계 에러가 PipelineStepError로 래핑되는지 확인한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        from steps.diarizer import TokenNotConfiguredError

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=_make_real_transcript_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                side_effect=TokenNotConfiguredError("HF 토큰 없음"),
            ),
        ):
            with pytest.raises(PipelineStepError) as exc_info:
                await pipeline.run(
                    audio_file,
                    meeting_id="e2e_err_diarize",
                )
            assert exc_info.value.step == "diarize"

    @pytest.mark.asyncio
    async def test_correct_error_with_fallback_state(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """correct 단계 실패 시 이전 4단계의 진행 상태가 보존되는지 확인한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        from core.llm_backend import LLMConnectionError

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=_make_real_transcript_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=_make_real_diarization_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_real_merged_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                side_effect=LLMConnectionError("LLM 연결 불가"),
            ),
        ):
            with pytest.raises(PipelineStepError) as exc_info:
                await pipeline.run(
                    audio_file,
                    meeting_id="e2e_err_correct",
                )
            assert exc_info.value.step == "correct"

        # 이전 4단계 완료 상태 보존 확인
        state = pipeline.get_status("e2e_err_correct")
        assert state is not None
        assert state.status == "failed"
        assert "convert" in state.completed_steps
        assert "transcribe" in state.completed_steps
        assert "diarize" in state.completed_steps
        assert "merge" in state.completed_steps
        assert "correct" not in state.completed_steps

    @pytest.mark.asyncio
    async def test_summarize_error_preserves_all_prior(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """summarize 실패 시 이전 5단계의 상태가 모두 보존되는지 확인한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        from core.llm_backend import LLMGenerationError

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=_make_real_transcript_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=_make_real_diarization_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_real_merged_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_real_corrected_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                side_effect=LLMGenerationError("요약 타임아웃"),
            ),
        ):
            with pytest.raises(PipelineStepError) as exc_info:
                await pipeline.run(
                    audio_file,
                    meeting_id="e2e_err_summarize",
                )
            assert exc_info.value.step == "summarize"

        state = pipeline.get_status("e2e_err_summarize")
        assert state is not None
        assert len(state.completed_steps) == 5
        assert "summarize" not in state.completed_steps


# === E2E 다중 실행 및 멱등성 테스트 ===


class TestE2EIdempotency:
    """파이프라인 다중 실행 및 멱등성 테스트."""

    @pytest.mark.asyncio
    async def test_completed_pipeline_no_rerun(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """이미 완료된 파이프라인은 재실행 시 즉시 완료를 반환하는지 확인한다."""
        meeting_id = "e2e_no_rerun"
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        # 첫 번째 실행: 모든 단계 완료
        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=_make_real_transcript_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=_make_real_diarization_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_real_merged_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_real_corrected_result(),
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_real_summary_result(),
            ),
        ):
            first = await pipeline.run(audio_file, meeting_id=meeting_id)

        assert first.status == "completed"

        # 두 번째 실행: 모든 단계가 이미 완료 → 즉시 반환
        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
            ) as convert_mock,
        ):
            second = await pipeline.run(audio_file, meeting_id=meeting_id)

        convert_mock.assert_not_called()
        assert second.status == "completed"

    @pytest.mark.asyncio
    async def test_multiple_pipelines_independent(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """서로 다른 meeting_id의 파이프라인이 독립적으로 동작하는지 확인한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        for meeting_id in ["e2e_multi_a", "e2e_multi_b"]:
            with (
                patch.object(
                    pipeline,
                    "_run_step_convert",
                    new_callable=AsyncMock,
                    return_value=wav_path,
                ),
                patch.object(
                    pipeline,
                    "_run_step_transcribe",
                    new_callable=AsyncMock,
                    return_value=_make_real_transcript_result(),
                ),
                patch.object(
                    pipeline,
                    "_run_step_diarize",
                    new_callable=AsyncMock,
                    return_value=_make_real_diarization_result(),
                ),
                patch.object(
                    pipeline,
                    "_run_step_merge",
                    new_callable=AsyncMock,
                    return_value=_make_real_merged_result(),
                ),
                patch.object(
                    pipeline,
                    "_run_step_correct",
                    new_callable=AsyncMock,
                    return_value=_make_real_corrected_result(),
                ),
                patch.object(
                    pipeline,
                    "_run_step_summarize",
                    new_callable=AsyncMock,
                    return_value=_make_real_summary_result(),
                ),
            ):
                state = await pipeline.run(
                    audio_file,
                    meeting_id=meeting_id,
                )
                assert state.status == "completed"

        # 두 상태가 독립적으로 존재
        assert pipeline.get_status("e2e_multi_a") is not None
        assert pipeline.get_status("e2e_multi_b") is not None
        assert (
            pipeline.get_status("e2e_multi_a").meeting_id
            != pipeline.get_status("e2e_multi_b").meeting_id
        )


# === 파이프라인 단계 시작 콜백 테스트 ===


class TestPipelineStepCallback:
    """파이프라인 단계 시작 콜백 테스트."""

    @pytest.mark.asyncio
    async def test_콜백_없으면_기존_동작_유지(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """on_step_start가 None이면 기존과 동일하게 동작한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=_make_mock_transcript(),
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=_make_mock_diarization(),
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_mock_merged(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_mock_corrected(),
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_mock_summary(),
            ),
        ):
            # on_step_start 미전달 — 예외 없이 정상 실행되어야 함
            state = await pipeline.run(audio_file, meeting_id="test_cb_none")

        assert state.status == "completed"
        assert len(state.completed_steps) == 6

    @pytest.mark.asyncio
    async def test_콜백_있으면_단계_시작시_호출(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """on_step_start 콜백이 각 단계 시작 시 호출된다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        callback = AsyncMock()

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=_make_mock_transcript(),
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=_make_mock_diarization(),
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_mock_merged(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_mock_corrected(),
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_mock_summary(),
            ),
        ):
            state = await pipeline.run(
                audio_file,
                meeting_id="test_cb_called",
                on_step_start=callback,
            )

        assert state.status == "completed"
        # 콜백이 6개 단계 모두에서 호출되었는지 확인
        assert callback.call_count == 6
        # 호출 인자가 PipelineStep의 value(문자열)인지 확인
        called_steps = [call.args[0] for call in callback.call_args_list]
        assert called_steps == [
            "convert",
            "transcribe",
            "diarize",
            "merge",
            "correct",
            "summarize",
        ]

    @pytest.mark.asyncio
    async def test_콜백_예외시_파이프라인_중단_안함(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """콜백에서 예외가 발생해도 파이프라인은 계속 진행한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        callback = AsyncMock(side_effect=RuntimeError("콜백 에러"))

        with (
            patch.object(
                pipeline,
                "_run_step_convert",
                new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline,
                "_run_step_transcribe",
                new_callable=AsyncMock,
                return_value=_make_mock_transcript(),
            ),
            patch.object(
                pipeline,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=_make_mock_diarization(),
            ),
            patch.object(
                pipeline,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_mock_merged(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_mock_corrected(),
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_mock_summary(),
            ),
        ):
            # 콜백이 예외를 던져도 파이프라인은 정상 완료되어야 함
            state = await pipeline.run(
                audio_file,
                meeting_id="test_cb_error",
                on_step_start=callback,
            )

        assert state.status == "completed"
        assert len(state.completed_steps) == 6


# === LLM 단계 스킵 테스트 ===


class TestSkipLlmSteps:
    """LLM 단계 스킵 테스트."""

    def test_skip_llm_steps_config_기본값(self) -> None:
        """PipelineConfig의 skip_llm_steps 기본값이 False인지 확인한다.

        이슈 C 수정: config.yaml 의 'false' 설정과 정합성을 맞추기 위해
        Pydantic 기본값을 True → False 로 변경함.
        기본 동작은 6단계 모두 실행 (LLM 교정·요약 포함).
        """
        from config import PipelineConfig

        cfg = PipelineConfig()
        assert cfg.skip_llm_steps is False

    @pytest.mark.asyncio
    async def test_run_skip_llm_steps시_correct_summarize_스킵(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """skip_llm_steps=True일 때 correct, summarize가 스킵되는지 확인한다."""
        mock_config.pipeline.skip_llm_steps = True

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
                return_value=_make_mock_merged(),
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
            pipeline = PipelineManager(mock_config, mock_model_manager)
            state = await pipeline.run(audio_file, meeting_id="skip_test")

            # LLM 단계가 호출되지 않았는지 확인
            mock_correct.assert_not_called()
            mock_summarize.assert_not_called()

            # 스킵된 단계 확인
            assert "correct" in state.skipped_steps
            assert "summarize" in state.skipped_steps

    @pytest.mark.asyncio
    async def test_run_skip_llm_steps시_merged_result_패스스루(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """skip_llm_steps=True일 때 merged_result가 corrected_result로 패스스루되는지 확인한다."""
        mock_config.pipeline.skip_llm_steps = True

        merged_mock = _make_mock_merged()

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
            pipeline = PipelineManager(mock_config, mock_model_manager)
            state = await pipeline.run(audio_file, meeting_id="passthrough_skip")

            # correct 스킵 확인
            assert "correct" in state.skipped_steps
            # 스킵 메시지에 skip_llm_steps 사유가 포함되는지 확인
            skip_warnings = [w for w in state.warnings if "skip_llm_steps" in w]
            assert len(skip_warnings) > 0

    @pytest.mark.asyncio
    async def test_run_skip_llm_steps시_정상_완료(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """LLM 스킵해도 status가 completed인지 확인한다."""
        mock_config.pipeline.skip_llm_steps = True

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
                return_value=_make_mock_merged(),
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
            pipeline = PipelineManager(mock_config, mock_model_manager)
            state = await pipeline.run(audio_file, meeting_id="skip_completed")

            assert state.status == "completed"
            # 모든 6단계가 completed_steps에 포함 (스킵된 단계도 포함)
            assert len(state.completed_steps) == 6

    @pytest.mark.asyncio
    async def test_run_skip_llm_steps_false시_기존_동작(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """skip_llm_steps=False이면 6단계 모두 실행한다."""
        mock_config.pipeline.skip_llm_steps = False

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
                return_value=_make_mock_transcript(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=_make_mock_diarization(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_mock_merged(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_mock_corrected(),
            ) as mock_correct,
            patch.object(
                PipelineManager,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_mock_summary(),
            ) as mock_summarize,
        ):
            pipeline = PipelineManager(mock_config, mock_model_manager)
            state = await pipeline.run(audio_file, meeting_id="no_skip_test")

            # LLM 단계가 실행되었는지 확인
            mock_correct.assert_called_once()
            mock_summarize.assert_called_once()

            assert state.status == "completed"
            assert state.skipped_steps == []
            assert len(state.completed_steps) == 6

    @pytest.mark.asyncio
    async def test_run_파라미터_skip_llm_steps가_config_오버라이드(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """run(skip_llm_steps=True) 파라미터가 config 값을 오버라이드하는지 확인한다."""
        # config는 False지만 파라미터로 True 전달
        mock_config.pipeline.skip_llm_steps = False

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
                return_value=_make_mock_merged(),
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
            pipeline = PipelineManager(mock_config, mock_model_manager)
            state = await pipeline.run(
                audio_file,
                meeting_id="override_test",
                skip_llm_steps=True,
            )

            # 파라미터가 config를 오버라이드 → LLM 스킵
            mock_correct.assert_not_called()
            mock_summarize.assert_not_called()
            assert "correct" in state.skipped_steps
            assert "summarize" in state.skipped_steps


# === 온디맨드 LLM 실행 테스트 ===


class TestRunLlmSteps:
    """온디맨드 LLM 실행 테스트."""

    @pytest.mark.asyncio
    async def test_run_llm_steps_merged_체크포인트_로드(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """merge 체크포인트에서 결과를 로드하는지 확인한다."""
        meeting_id = "llm_test_001"
        checkpoints_dir = tmp_path / "checkpoints"

        # 상태 파일 생성
        state_dir = checkpoints_dir / meeting_id
        state_dir.mkdir(parents=True, exist_ok=True)
        state = PipelineState(
            meeting_id=meeting_id,
            audio_path="/tmp/test.m4a",
            status="completed",
            completed_steps=["convert", "transcribe", "diarize", "merge", "correct", "summarize"],
            skipped_steps=["correct", "summarize"],
            output_dir=str(tmp_path / "outputs" / meeting_id),
        )
        state.save(state_dir / "pipeline_state.json")

        # merge 체크포인트 생성
        merge_cp = state_dir / "merge.json"
        merge_cp.write_text('{"utterances": [], "num_speakers": 1}', encoding="utf-8")

        pipeline = PipelineManager(mock_config, mock_model_manager)

        with (
            patch(
                "steps.merger.MergedResult.from_checkpoint",
                return_value=_make_mock_merged(),
            ) as mock_load,
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_mock_corrected(),
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_mock_summary(),
            ),
        ):
            result = await pipeline.run_llm_steps(meeting_id)

            # merge 체크포인트에서 로드 확인
            mock_load.assert_called_once_with(merge_cp)
            assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_run_llm_steps_correct_summarize_실행(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """correct + summarize가 실행되는지 확인한다."""
        meeting_id = "llm_test_002"
        checkpoints_dir = tmp_path / "checkpoints"

        # 상태 파일 생성
        state_dir = checkpoints_dir / meeting_id
        state_dir.mkdir(parents=True, exist_ok=True)
        state = PipelineState(
            meeting_id=meeting_id,
            audio_path="/tmp/test.m4a",
            status="completed",
            completed_steps=["convert", "transcribe", "diarize", "merge", "correct", "summarize"],
            skipped_steps=["correct", "summarize"],
            output_dir=str(tmp_path / "outputs" / meeting_id),
        )
        state.save(state_dir / "pipeline_state.json")

        # merge 체크포인트 생성
        merge_cp = state_dir / "merge.json"
        merge_cp.write_text('{"utterances": [], "num_speakers": 1}', encoding="utf-8")

        pipeline = PipelineManager(mock_config, mock_model_manager)

        with (
            patch(
                "steps.merger.MergedResult.from_checkpoint",
                return_value=_make_mock_merged(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_mock_corrected(),
            ) as mock_correct,
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_mock_summary(),
            ) as mock_summarize,
        ):
            result = await pipeline.run_llm_steps(meeting_id)

            # correct, summarize 실행 확인
            mock_correct.assert_called_once()
            mock_summarize.assert_called_once()

            # skipped_steps에서 제거 확인
            assert "correct" not in result.skipped_steps
            assert "summarize" not in result.skipped_steps

    @pytest.mark.asyncio
    async def test_run_llm_steps_체크포인트_없으면_에러(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """체크포인트 미존재 시 PipelineError가 발생하는지 확인한다."""
        meeting_id = "llm_test_003"
        checkpoints_dir = tmp_path / "checkpoints"

        # 상태 파일만 생성 (merge 체크포인트 없음)
        state_dir = checkpoints_dir / meeting_id
        state_dir.mkdir(parents=True, exist_ok=True)
        state = PipelineState(
            meeting_id=meeting_id,
            audio_path="/tmp/test.m4a",
            status="completed",
            completed_steps=["convert", "transcribe", "diarize", "merge"],
        )
        state.save(state_dir / "pipeline_state.json")

        pipeline = PipelineManager(mock_config, mock_model_manager)

        with pytest.raises(PipelineError, match="merge 체크포인트"):
            await pipeline.run_llm_steps(meeting_id)

    @pytest.mark.asyncio
    async def test_run_llm_steps_아무것도_없으면_에러(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
    ) -> None:
        """state 파일과 merge 체크포인트 모두 없으면 PipelineError 가 발생한다.

        (이슈 I 이후: merge 체크포인트가 있으면 state 를 자동 재구성하므로,
         merge 까지 없어야 진짜 에러. 메시지는 merge 체크포인트 부재를 안내한다.)
        """
        pipeline = PipelineManager(mock_config, mock_model_manager)

        with pytest.raises(PipelineError, match="merge 체크포인트"):
            await pipeline.run_llm_steps("nonexistent_meeting")


class TestPipelineStateMultitrack:
    """PipelineState 멀티트랙 확장 테스트."""

    def test_기본값_싱글트랙(self) -> None:
        """기존 동작: wav_paths 비어있고 is_multitrack=False."""
        state = PipelineState(meeting_id="test", audio_path="/test.wav")
        assert state.wav_paths == {}
        assert state.is_multitrack is False
        assert state.wav_path == ""

    def test_멀티트랙_필드_설정(self) -> None:
        """멀티트랙 필드가 올바르게 설정된다."""
        state = PipelineState(
            meeting_id="test",
            audio_path="/test.wav",
            wav_paths={"system": "/out/test_system.wav", "mic": "/out/test_mic.wav"},
            is_multitrack=True,
            wav_path="/out/test_merged.wav",
        )
        assert state.is_multitrack is True
        assert len(state.wav_paths) == 2
        assert "system" in state.wav_paths
        assert "mic" in state.wav_paths
        assert state.wav_path == "/out/test_merged.wav"

    def test_체크포인트_직렬화_멀티트랙(self) -> None:
        """멀티트랙 필드가 체크포인트 JSON에 포함된다."""
        state = PipelineState(
            meeting_id="test",
            audio_path="/test.wav",
            wav_paths={"system": "/s.wav", "mic": "/m.wav"},
            is_multitrack=True,
        )
        d = asdict(state)
        assert "wav_paths" in d
        assert "is_multitrack" in d
        assert d["is_multitrack"] is True


# === Phase 1: 동적 타임아웃 및 재시도 정책 ===


def test_compute_dynamic_timeout_짧은_파일은_최소값():
    """짧은 오디오(60초 × 3 = 180s)는 하한(600s)으로 클램핑된다."""
    from core.pipeline import compute_dynamic_timeout

    assert (
        compute_dynamic_timeout(
            duration_seconds=60.0,
            multiplier=3.0,
            min_seconds=600,
            max_seconds=10800,
        )
        == 600
    )


def test_compute_dynamic_timeout_중간_파일():
    """중간 길이(900초 × 3 = 2700s)는 그대로 사용된다."""
    from core.pipeline import compute_dynamic_timeout

    assert (
        compute_dynamic_timeout(
            duration_seconds=900.0,
            multiplier=3.0,
            min_seconds=600,
            max_seconds=10800,
        )
        == 2700
    )


def test_compute_dynamic_timeout_긴_파일은_상한():
    """매우 긴 오디오(10시간 × 3 = 108000s)는 상한(10800s)으로 클램핑된다."""
    from core.pipeline import compute_dynamic_timeout

    assert (
        compute_dynamic_timeout(
            duration_seconds=36000.0,
            multiplier=3.0,
            min_seconds=600,
            max_seconds=10800,
        )
        == 10800
    )


def test_compute_dynamic_timeout_타입은_int():
    """float 입력에서도 반환 타입은 int이며, 절삭(truncation) 방식이다."""
    from core.pipeline import compute_dynamic_timeout

    result = compute_dynamic_timeout(
        duration_seconds=900.5,  # float
        multiplier=3.0,
        min_seconds=600,
        max_seconds=10800,
    )
    assert isinstance(result, int)
    assert result == 2701  # int(900.5 * 3.0) = int(2701.5) = 2701


def test_타임아웃_에러는_재시도_안함():
    """NonRetryableError(TranscriptionTimeoutError) 감지 시 재시도 루프가 break 되어야 한다."""
    from core.retry_policy import TranscriptionTimeoutError, should_retry

    err = TranscriptionTimeoutError("1800초 초과")
    assert should_retry(err, attempt=1, max_attempts=3) is False


# === DEV_REQUEST_2026-04-23 이슈 G 회귀 테스트 ===
# degraded 플래그 로직 분리 + 메모리 임계치 기본값 정합 검증


class TestDegradedFlagLogicSeparation:
    """degraded 플래그가 LLM 단계 스킵 결정과 분리되는지 검증하는 테스트 클래스.

    핵심 변경 내용:
        - state.degraded: 파이프라인 시작 시점 진단 결과 저장용 (UI/API 보고 목적)
        - LLM 단계 스킵 결정: 각 단계 직전 실시간 check_memory() 반환값(mem_ok)만 사용
        - 이전 동작: 초기 degraded=True면 중반 메모리 회복 후에도 LLM 영구 스킵
        - 수정 동작: 실시간 mem_ok=True면 degraded 값과 관계없이 LLM 실행
    """

    def test_케이스D_config_min_memory_free_gb_기본값(self) -> None:
        """케이스 D: PipelineConfig 기본값이 2.0에서 1.5로 완화되었는지 확인한다.

        이슈 G 변경 1: 실측 21건 중 가용 메모리 최대값 1.9GB 기준으로
        min_memory_free_gb를 2.0 → 1.5로 완화.
        """
        from config import PipelineConfig

        cfg = PipelineConfig()
        assert cfg.min_memory_free_gb == 1.5

    @pytest.mark.asyncio
    async def test_케이스A_초기메모리부족_중반회복시_llm_실행(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """케이스 A: 파이프라인 시작 시 메모리 부족(check_all=False)이었다가
        중반에 회복(check_memory=True)되면 LLM 단계가 실행되어야 한다.

        이전 동작(버그): degraded=True → LLM 영구 스킵.
        수정 동작: 실시간 mem_ok=True → LLM 실행, state.degraded=True 는 유지.
        """
        mock_config.pipeline.skip_llm_steps = False

        from core.pipeline import ResourceGuard, ResourceStatus

        # 초기 check_all: memory_ok=False (degraded 발생)
        initial_status = ResourceStatus(
            disk_ok=True,
            disk_free_gb=10.0,
            memory_ok=False,
            memory_free_gb=1.2,
        )
        # 각 단계 직전 check_memory: 회복됨 (True, 2.5GB)
        recovered_memory = (True, 2.5)

        with (
            patch.object(
                ResourceGuard,
                "check_all",
                return_value=initial_status,
            ),
            patch.object(
                ResourceGuard,
                "check_memory",
                return_value=recovered_memory,
            ),
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
                return_value=_make_mock_transcript(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=_make_mock_diarization(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_mock_merged(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_mock_corrected(),
            ) as mock_correct,
            patch.object(
                PipelineManager,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_mock_summary(),
            ) as mock_summarize,
        ):
            pipeline = PipelineManager(mock_config, mock_model_manager)
            state = await pipeline.run(audio_file, meeting_id="degraded_recovery_test")

        # LLM 단계가 스킵되지 않고 실행되어야 한다
        mock_correct.assert_called_once()
        mock_summarize.assert_called_once()
        # correct, summarize 가 skipped_steps 에 없어야 한다
        assert "correct" not in state.skipped_steps
        assert "summarize" not in state.skipped_steps
        # state.degraded 는 초기 진단 결과(True)를 유지한다 (UI 보고용)
        assert state.degraded is True

    @pytest.mark.asyncio
    async def test_케이스B_실시간메모리부족시_llm_스킵(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """케이스 B: 초기 check_all은 OK이지만 단계 직전 check_memory가
        False를 반환하면 LLM 단계가 스킵되어야 한다.
        """
        mock_config.pipeline.skip_llm_steps = False

        from core.pipeline import ResourceGuard, ResourceStatus

        # 초기 check_all: memory_ok=True (degraded 없음)
        initial_status = ResourceStatus(
            disk_ok=True,
            disk_free_gb=10.0,
            memory_ok=True,
            memory_free_gb=2.5,
        )
        # 단계 직전 실시간 메모리: 부족 (False, 0.5GB)
        low_memory = (False, 0.5)

        with (
            patch.object(
                ResourceGuard,
                "check_all",
                return_value=initial_status,
            ),
            patch.object(
                ResourceGuard,
                "check_memory",
                return_value=low_memory,
            ),
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
                return_value=_make_mock_transcript(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=_make_mock_diarization(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_mock_merged(),
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
            pipeline = PipelineManager(mock_config, mock_model_manager)
            state = await pipeline.run(audio_file, meeting_id="realtime_low_mem_test")

        # 실시간 메모리 부족 → LLM 단계 스킵
        mock_correct.assert_not_called()
        mock_summarize.assert_not_called()
        assert "correct" in state.skipped_steps
        assert "summarize" in state.skipped_steps
        # state.degraded 도 True 로 설정된다 (스킵 시 설정)
        assert state.degraded is True

    @pytest.mark.asyncio
    async def test_케이스C_skip_llm_steps_true_우선순위(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        audio_file: Path,
    ) -> None:
        """케이스 C: skip_llm_steps=True 설정 시 메모리 상태에 관계없이 LLM 스킵.

        기존 동작(회귀 없음): skip_llm_steps=True 는 mem_ok 보다 우선한다.
        """
        mock_config.pipeline.skip_llm_steps = True

        from core.pipeline import ResourceGuard, ResourceStatus

        # 메모리 충분한 상태여도 skip_llm_steps 우선
        good_status = ResourceStatus(
            disk_ok=True,
            disk_free_gb=10.0,
            memory_ok=True,
            memory_free_gb=4.0,
        )

        with (
            patch.object(
                ResourceGuard,
                "check_all",
                return_value=good_status,
            ),
            patch.object(
                ResourceGuard,
                "check_memory",
                return_value=(True, 4.0),
            ),
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
                return_value=_make_mock_transcript(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_diarize",
                new_callable=AsyncMock,
                return_value=_make_mock_diarization(),
            ),
            patch.object(
                PipelineManager,
                "_run_step_merge",
                new_callable=AsyncMock,
                return_value=_make_mock_merged(),
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
            pipeline = PipelineManager(mock_config, mock_model_manager)
            state = await pipeline.run(audio_file, meeting_id="skip_llm_priority_test")

        # skip_llm_steps=True 가 우선 → LLM 스킵
        mock_correct.assert_not_called()
        mock_summarize.assert_not_called()
        assert "correct" in state.skipped_steps
        assert "summarize" in state.skipped_steps


# === 이슈 H / I 회귀 테스트 ===


class TestLlmLockSerialization:
    """이슈 H: run_llm_steps 가 _llm_lock 으로 직렬화되는지 검증."""

    @pytest.mark.asyncio
    async def test_run_llm_steps_동시_호출_순차_실행(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """두 회의에 대해 동시에 run_llm_steps 를 호출해도 내부에서 직렬화되어야 한다.

        MLX Metal 커맨드 버퍼 충돌 방지를 위한 프로세스 전역 락이 동작하는지
        검증한다. 두 번째 호출은 첫 번째가 완료될 때까지 대기해야 한다.
        """
        import asyncio

        checkpoints_dir = tmp_path / "checkpoints"
        pipeline = PipelineManager(mock_config, mock_model_manager)

        # 두 회의 상태/체크포인트 파일 준비
        for mid in ("llm_lock_A", "llm_lock_B"):
            state_dir = checkpoints_dir / mid
            state_dir.mkdir(parents=True, exist_ok=True)
            PipelineState(
                meeting_id=mid,
                audio_path="/tmp/t.m4a",
                status="completed",
                completed_steps=["convert", "transcribe", "diarize", "merge"],
                output_dir=str(tmp_path / "outputs" / mid),
            ).save(state_dir / "pipeline_state.json")
            (state_dir / "merge.json").write_text(
                '{"utterances": [], "num_speakers": 1}', encoding="utf-8"
            )

        # 동시 실행 순서 추적용
        active_count = {"value": 0, "peak": 0}

        async def _slow_correct(*_args: object, **_kwargs: object) -> MagicMock:
            active_count["value"] += 1
            active_count["peak"] = max(active_count["peak"], active_count["value"])
            try:
                await asyncio.sleep(0.05)
                return _make_mock_corrected()
            finally:
                active_count["value"] -= 1

        with (
            patch(
                "steps.merger.MergedResult.from_checkpoint",
                return_value=_make_mock_merged(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new=_slow_correct,
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_mock_summary(),
            ),
        ):
            # 두 건을 동시에 실행
            results = await asyncio.gather(
                pipeline.run_llm_steps("llm_lock_A"),
                pipeline.run_llm_steps("llm_lock_B"),
            )

        # 핵심 검증: 동시에 2개가 실행되면 peak 가 2가 되지만, 락이 있으면 1 이어야 한다.
        assert active_count["peak"] == 1, (
            f"LLM 단계가 동시에 실행되었다 (peak={active_count['peak']}) — "
            "_llm_lock 이 동작하지 않음"
        )
        assert all(r.status == "completed" for r in results)

    @pytest.mark.asyncio
    async def test_run_내_LLM_단계와_run_llm_steps_동시_호출_직렬화(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """자동 파이프라인(run) 의 CORRECT 단계와 수동 run_llm_steps 가 겹쳐도 MLX 호출이 직렬화되어야 한다.

        가장 현실적 시나리오: JobProcessor 가 자동 파이프라인을 돌리는 중에
        사용자가 다른 회의의 /summarize 를 눌러 run_llm_steps 를 트리거하면
        두 경로가 모두 _run_step_correct / _run_step_summarize 를 호출한다.
        _llm_lock 이 양쪽 경로에 적용되어야 MLX Metal 크래시가 차단된다.
        """
        import asyncio

        pipeline = PipelineManager(mock_config, mock_model_manager)

        # 카운터로 동시 실행 감지
        mlx_active = {"value": 0, "peak": 0}

        async def _fake_correct(*_args: object, **_kwargs: object) -> MagicMock:
            mlx_active["value"] += 1
            mlx_active["peak"] = max(mlx_active["peak"], mlx_active["value"])
            try:
                await asyncio.sleep(0.05)
                return _make_mock_corrected()
            finally:
                mlx_active["value"] -= 1

        # 수동 run_llm_steps 경로용 체크포인트 준비
        manual_mid = "manual_summary"
        manual_dir = tmp_path / "checkpoints" / manual_mid
        manual_dir.mkdir(parents=True, exist_ok=True)
        PipelineState(
            meeting_id=manual_mid,
            audio_path="/tmp/m.m4a",
            completed_steps=["convert", "transcribe", "diarize", "merge"],
            output_dir=str(tmp_path / "outputs" / manual_mid),
        ).save(manual_dir / "pipeline_state.json")
        (manual_dir / "merge.json").write_text(
            '{"utterances": [], "num_speakers": 1}', encoding="utf-8"
        )

        # 자동 파이프라인의 CORRECT 단계 내부 호출 흉내 (경로 A)
        async def _auto_pipeline_llm_call() -> None:
            async with pipeline._llm_lock:
                await _fake_correct()

        # 수동 run_llm_steps 경로 (경로 B)
        with (
            patch(
                "steps.merger.MergedResult.from_checkpoint",
                return_value=_make_mock_merged(),
            ),
            patch.object(pipeline, "_run_step_correct", new=_fake_correct),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_mock_summary(),
            ),
        ):
            results = await asyncio.gather(
                _auto_pipeline_llm_call(),
                pipeline.run_llm_steps(manual_mid),
            )

        # 두 경로가 같은 락을 공유하므로 peak=1 이어야 한다
        assert mlx_active["peak"] == 1, (
            f"혼합 경로 동시 호출 시 MLX 직렬화 실패 (peak={mlx_active['peak']}). "
            "run() 내부 LLM 단계에도 _llm_lock 을 적용해야 한다."
        )
        assert results[1].status == "completed"


class TestRebuildStateFromCheckpoints:
    """이슈 I: pipeline_state.json 유실 시 재구성 로직."""

    def test_merge_체크포인트만_있을_때_재구성(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """merge 체크포인트가 있으면 state 를 재구성하고 completed_steps 에 merge 가 포함된다."""
        meeting_id = "legacy_001"
        state_dir = tmp_path / "checkpoints" / meeting_id
        state_dir.mkdir(parents=True, exist_ok=True)

        # merge 체크포인트만 존재, state 파일은 없음
        (state_dir / "merge.json").write_text(
            '{"utterances": [], "num_speakers": 1}', encoding="utf-8"
        )
        (state_dir / "transcribe.json").write_text("{}", encoding="utf-8")

        pipeline = PipelineManager(mock_config, mock_model_manager)
        state = pipeline._rebuild_state_from_checkpoints(meeting_id)

        # state 파일이 생성되었고, merge/transcribe 가 completed_steps 에 포함
        assert (state_dir / "pipeline_state.json").exists()
        assert "merge" in state.completed_steps
        assert "transcribe" in state.completed_steps
        assert state.meeting_id == meeting_id

    def test_체크포인트_디렉토리_부재_시_예외(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """체크포인트 디렉토리조차 없으면 PipelineError 를 발생시킨다."""
        pipeline = PipelineManager(mock_config, mock_model_manager)
        with pytest.raises(PipelineError, match="체크포인트 디렉토리"):
            pipeline._rebuild_state_from_checkpoints("never_existed")

    @pytest.mark.asyncio
    async def test_run_llm_steps_state_유실_자동_복구(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """run_llm_steps 가 state 파일 유실에도 merge 체크포인트로 자동 복구 후 실행된다."""
        meeting_id = "legacy_002"
        state_dir = tmp_path / "checkpoints" / meeting_id
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "merge.json").write_text(
            '{"utterances": [], "num_speakers": 1}', encoding="utf-8"
        )

        pipeline = PipelineManager(mock_config, mock_model_manager)

        with (
            patch(
                "steps.merger.MergedResult.from_checkpoint",
                return_value=_make_mock_merged(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_mock_corrected(),
            ),
            patch.object(
                pipeline,
                "_run_step_summarize",
                new_callable=AsyncMock,
                return_value=_make_mock_summary(),
            ),
        ):
            state = await pipeline.run_llm_steps(meeting_id)

        assert state.status == "completed"
        assert (state_dir / "pipeline_state.json").exists()


# === 안정성 개선: 타임아웃 회귀 테스트 ===


class TestLlmTimeouts:
    """correct/summarize 단계 하드 타임아웃 + 락 획득 타임아웃 검증."""

    @pytest.mark.asyncio
    async def test_correct_단계_타임아웃_시_PipelineError(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """correct 단계가 타임아웃 임계 이상 걸리면 PipelineError(타임아웃 메시지)."""
        import asyncio

        mock_config.pipeline.correct_timeout_seconds = 1
        mock_config.pipeline.summarize_timeout_seconds = 60
        mock_config.pipeline.llm_lock_acquire_timeout_seconds = 60

        meeting_id = "timeout_correct"
        state_dir = tmp_path / "checkpoints" / meeting_id
        state_dir.mkdir(parents=True, exist_ok=True)
        PipelineState(
            meeting_id=meeting_id,
            audio_path="/tmp/t.m4a",
            completed_steps=["convert", "transcribe", "diarize", "merge"],
            output_dir=str(tmp_path / "outputs" / meeting_id),
        ).save(state_dir / "pipeline_state.json")
        (state_dir / "merge.json").write_text(
            '{"utterances": [], "num_speakers": 1}', encoding="utf-8"
        )

        async def _slow_correct(*_a: object, **_kw: object) -> None:
            await asyncio.sleep(5)  # 타임아웃 5배 이상

        pipeline = PipelineManager(mock_config, mock_model_manager)
        with (
            patch(
                "steps.merger.MergedResult.from_checkpoint",
                return_value=_make_mock_merged(),
            ),
            patch.object(pipeline, "_run_step_correct", new=_slow_correct),
        ):
            with pytest.raises(PipelineError, match="correct 단계 타임아웃"):
                await pipeline.run_llm_steps(meeting_id)

    @pytest.mark.asyncio
    async def test_summarize_단계_타임아웃_시_PipelineError(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """summarize 단계가 타임아웃 임계 이상 걸리면 PipelineError."""
        import asyncio

        mock_config.pipeline.correct_timeout_seconds = 60
        mock_config.pipeline.summarize_timeout_seconds = 1
        mock_config.pipeline.llm_lock_acquire_timeout_seconds = 60

        meeting_id = "timeout_summarize"
        state_dir = tmp_path / "checkpoints" / meeting_id
        state_dir.mkdir(parents=True, exist_ok=True)
        PipelineState(
            meeting_id=meeting_id,
            audio_path="/tmp/t.m4a",
            completed_steps=["convert", "transcribe", "diarize", "merge"],
            output_dir=str(tmp_path / "outputs" / meeting_id),
        ).save(state_dir / "pipeline_state.json")
        (state_dir / "merge.json").write_text(
            '{"utterances": [], "num_speakers": 1}', encoding="utf-8"
        )

        async def _slow_summarize(*_a: object, **_kw: object) -> None:
            await asyncio.sleep(5)

        pipeline = PipelineManager(mock_config, mock_model_manager)
        with (
            patch(
                "steps.merger.MergedResult.from_checkpoint",
                return_value=_make_mock_merged(),
            ),
            patch.object(
                pipeline,
                "_run_step_correct",
                new_callable=AsyncMock,
                return_value=_make_mock_corrected(),
            ),
            patch.object(pipeline, "_run_step_summarize", new=_slow_summarize),
        ):
            with pytest.raises(PipelineError, match="summarize 단계 타임아웃"):
                await pipeline.run_llm_steps(meeting_id)

    @pytest.mark.asyncio
    async def test_llm_락_획득_타임아웃_시_PipelineError(
        self,
        mock_config: MagicMock,
        mock_model_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """선행 작업이 락을 쥐고 놓지 않을 때 후속 호출은 타임아웃 후 PipelineError."""

        mock_config.pipeline.correct_timeout_seconds = 60
        mock_config.pipeline.summarize_timeout_seconds = 60
        mock_config.pipeline.llm_lock_acquire_timeout_seconds = 1  # 1초 내 획득 실패

        meeting_id = "lock_timeout"
        state_dir = tmp_path / "checkpoints" / meeting_id
        state_dir.mkdir(parents=True, exist_ok=True)
        PipelineState(
            meeting_id=meeting_id,
            audio_path="/tmp/t.m4a",
            completed_steps=["convert", "transcribe", "diarize", "merge"],
            output_dir=str(tmp_path / "outputs" / meeting_id),
        ).save(state_dir / "pipeline_state.json")
        (state_dir / "merge.json").write_text(
            '{"utterances": [], "num_speakers": 1}', encoding="utf-8"
        )

        pipeline = PipelineManager(mock_config, mock_model_manager)

        # 사전에 락을 강제 획득하여 타임아웃 유발
        await pipeline._llm_lock.acquire()
        try:
            with pytest.raises(PipelineError, match="LLM 락 획득 타임아웃"):
                await pipeline.run_llm_steps(meeting_id)
        finally:
            pipeline._llm_lock.release()
