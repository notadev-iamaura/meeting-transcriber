"""
파이프라인 chunk/embed 단계 통합 테스트 모듈
(Pipeline chunk/embed integration test module)

목적:
    core/pipeline.py 의 메인 파이프라인이 SUMMARIZE 이후 CHUNK / EMBED 단계를
    실행하고 RAG 검색 인덱스(ChromaDB + SQLite FTS5)를 생성하는지 검증한다.

배경:
    이전까지 PIPELINE_STEPS 에는 [CONVERT, TRANSCRIBE, DIARIZE, MERGE,
    CORRECT, SUMMARIZE] 6단계만 있었고 chunker/embedder 모듈은 import 되지
    않았다. 결과적으로 회의는 completed 상태였지만 RAG 인덱스가 비어 있어
    /api/chat 이 항상 컨텍스트 없는 답변을 반환했다.

테스트 전략:
    - test_full_pipeline_includes_chunk_and_embed: 8단계 모두 completed_steps
      에 들어가는지 검증
    - test_pipeline_step_enum_has_chunk_embed: PipelineStep enum 정의 검증
    - test_pipeline_steps_order: PIPELINE_STEPS 순서가
      [..., SUMMARIZE, CHUNK, EMBED] 인지 검증
    - test_chunk_embed_called_after_summarize: 단계 호출 순서 검증
    - test_chunk_failure_does_not_block_summary: chunk/embed 가 SUMMARIZE
      이후이므로 회의록은 이미 생성된 상태여야 함
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.pipeline import (
    PIPELINE_STEPS,
    PipelineManager,
    PipelineStep,
    PipelineStepError,
)


# === 픽스처 — test_pipeline.py 의 패턴 따름 ===


@pytest.fixture
def mock_config(tmp_path: Path) -> MagicMock:
    """테스트용 AppConfig 모킹 객체를 생성한다."""
    config = MagicMock()
    config.pipeline.checkpoint_enabled = True
    config.pipeline.retry_max_count = 2
    config.pipeline.peak_ram_limit_gb = 9.5
    config.pipeline.min_disk_free_gb = 1.0
    config.pipeline.min_memory_free_gb = 2.0
    config.pipeline.skip_llm_steps = False
    config.pipeline.correct_timeout_seconds = 1800
    config.pipeline.summarize_timeout_seconds = 600
    config.pipeline.llm_lock_acquire_timeout_seconds = 3600
    config.pipeline.llm_recommended_memory_gb = 6.5

    config.paths.resolved_outputs_dir = tmp_path / "outputs"
    config.paths.resolved_checkpoints_dir = tmp_path / "checkpoints"

    config.audio.sample_rate = 16000
    config.audio.channels = 1
    config.audio.format = "wav"
    config.audio.supported_input_formats = ["wav", "mp3", "m4a"]

    config.stt.model_name = "whisper-medium-ko-zeroth"
    config.stt.language = "ko"
    config.stt.beam_size = 5

    config.diarization.model_name = "pyannote/speaker-diarization-3.1"
    config.diarization.device = "cpu"
    config.diarization.min_speakers = 1
    config.diarization.max_speakers = 10
    config.diarization.huggingface_token = "test-token"

    config.llm.model_name = "exaone3.5:7.8b-instruct-q4_K_M"
    config.llm.host = "http://127.0.0.1:11434"
    config.llm.temperature = 0.3
    config.llm.max_context_tokens = 8192
    config.llm.correction_batch_size = 10
    config.llm.request_timeout_seconds = 120

    return config


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    """테스트용 오디오 파일을 생성한다."""
    audio = tmp_path / "test_meeting.m4a"
    audio.write_bytes(b"fake audio content for testing")
    return audio


@pytest.fixture
def pipeline(mock_config: MagicMock) -> PipelineManager:
    """테스트용 PipelineManager 인스턴스를 생성한다."""
    return PipelineManager(mock_config, MagicMock())


# === Mock 헬퍼 ===


def _make_mock_transcript() -> MagicMock:
    result = MagicMock()
    result.segments = [MagicMock(text="안녕하세요", start=0.0, end=2.0)]
    result.full_text = "안녕하세요"
    result.save_checkpoint = MagicMock()
    return result


def _make_mock_diarization() -> MagicMock:
    result = MagicMock()
    result.segments = [MagicMock(speaker="SPEAKER_00", start=0.0, end=2.0)]
    result.num_speakers = 1
    result.save_checkpoint = MagicMock()
    return result


def _make_mock_merged() -> MagicMock:
    result = MagicMock()
    result.utterances = [
        MagicMock(text="안녕하세요", speaker="SPEAKER_00", start=0.0, end=2.0),
    ]
    result.num_speakers = 1
    result.save_checkpoint = MagicMock()
    return result


def _make_mock_corrected() -> MagicMock:
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
    result = MagicMock()
    result.markdown = "## 회의록\n- 테스트 회의"
    result.save_checkpoint = MagicMock()
    result.save_markdown = MagicMock()
    return result


def _make_mock_chunked() -> MagicMock:
    """ChunkedResult Mock — chunker.chunk() 의 반환."""
    result = MagicMock()
    result.chunks = [
        MagicMock(
            chunk_id="test_meeting_chunk_0001",
            text="안녕하세요",
            speakers=["SPEAKER_00"],
            start_time=0.0,
            end_time=2.0,
            chunk_index=0,
        ),
    ]
    result.meeting_id = "test_chunk_embed"
    result.date = "2026-04-29"
    result.total_utterances = 1
    result.num_speakers = 1
    result.audio_path = "/tmp/test.wav"
    result.save_checkpoint = MagicMock()
    return result


def _make_mock_embedded() -> MagicMock:
    """EmbeddedResult Mock — embedder.embed() 의 반환."""
    result = MagicMock()
    result.chunks = [
        MagicMock(
            chunk_id="test_meeting_chunk_0001",
            embedding=[0.1] * 384,
            text="안녕하세요",
            meeting_id="test_chunk_embed",
            date="2026-04-29",
            speakers=["SPEAKER_00"],
            start_time=0.0,
            end_time=2.0,
            chunk_index=0,
        ),
    ]
    result.meeting_id = "test_chunk_embed"
    result.date = "2026-04-29"
    result.total_chunks = 1
    result.embedding_dimension = 384
    result.chroma_stored = True
    result.fts_stored = True
    result.save_checkpoint = MagicMock()
    return result


# === Phase 1 RED: PipelineStep 열거형 ===


class TestPipelineStepHasChunkEmbed:
    """PipelineStep 열거형이 CHUNK / EMBED 를 포함하는지 검증."""

    def test_chunk_step_exists(self) -> None:
        """PipelineStep.CHUNK 가 정의되어 있어야 한다."""
        assert hasattr(PipelineStep, "CHUNK"), (
            "PipelineStep enum 에 CHUNK 가 없습니다. "
            "RAG 검색용 청크 분할 단계를 추가해야 합니다."
        )
        assert PipelineStep.CHUNK.value == "chunk"

    def test_embed_step_exists(self) -> None:
        """PipelineStep.EMBED 가 정의되어 있어야 한다."""
        assert hasattr(PipelineStep, "EMBED"), (
            "PipelineStep enum 에 EMBED 가 없습니다. "
            "RAG 검색용 임베딩 단계를 추가해야 합니다."
        )
        assert PipelineStep.EMBED.value == "embed"


class TestPipelineStepsOrder:
    """PIPELINE_STEPS 가 8단계로 확장되었는지 + 순서 검증."""

    def test_pipeline_steps_has_8_steps(self) -> None:
        """PIPELINE_STEPS 는 8단계여야 한다."""
        assert len(PIPELINE_STEPS) == 8, (
            f"PIPELINE_STEPS 가 {len(PIPELINE_STEPS)} 단계입니다. "
            "CHUNK + EMBED 추가 후 8단계여야 합니다."
        )

    def test_chunk_after_summarize(self) -> None:
        """CHUNK 는 SUMMARIZE 직후여야 한다 (회의록 생성을 차단하지 않기 위해)."""
        summarize_idx = PIPELINE_STEPS.index(PipelineStep.SUMMARIZE)
        chunk_idx = PIPELINE_STEPS.index(PipelineStep.CHUNK)
        assert chunk_idx == summarize_idx + 1, (
            f"CHUNK 는 SUMMARIZE 다음이어야 합니다. "
            f"summarize={summarize_idx}, chunk={chunk_idx}"
        )

    def test_embed_after_chunk(self) -> None:
        """EMBED 는 CHUNK 직후여야 한다."""
        chunk_idx = PIPELINE_STEPS.index(PipelineStep.CHUNK)
        embed_idx = PIPELINE_STEPS.index(PipelineStep.EMBED)
        assert embed_idx == chunk_idx + 1, (
            f"EMBED 는 CHUNK 다음이어야 합니다. "
            f"chunk={chunk_idx}, embed={embed_idx}"
        )

    def test_embed_is_last(self) -> None:
        """EMBED 가 마지막 단계여야 한다."""
        assert PIPELINE_STEPS[-1] == PipelineStep.EMBED


# === Phase 1 RED: 전체 파이프라인 통합 ===


class TestFullPipelineWithChunkEmbed:
    """pipeline.run() 이 chunk/embed 단계를 포함해 8단계 모두 실행하는지 검증."""

    @pytest.mark.asyncio
    async def test_full_pipeline_includes_chunk_and_embed(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """전체 파이프라인 실행 시 chunk/embed 가 completed_steps 에 포함되어야 한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        with (
            patch.object(
                pipeline, "_run_step_convert", new_callable=AsyncMock,
                return_value=wav_path,
            ),
            patch.object(
                pipeline, "_run_step_transcribe", new_callable=AsyncMock,
                return_value=_make_mock_transcript(),
            ),
            patch.object(
                pipeline, "_run_step_diarize", new_callable=AsyncMock,
                return_value=_make_mock_diarization(),
            ),
            patch.object(
                pipeline, "_run_step_merge", new_callable=AsyncMock,
                return_value=_make_mock_merged(),
            ),
            patch.object(
                pipeline, "_run_step_correct", new_callable=AsyncMock,
                return_value=_make_mock_corrected(),
            ),
            patch.object(
                pipeline, "_run_step_summarize", new_callable=AsyncMock,
                return_value=_make_mock_summary(),
            ),
            patch.object(
                pipeline, "_run_step_chunk", new_callable=AsyncMock,
                return_value=_make_mock_chunked(),
            ),
            patch.object(
                pipeline, "_run_step_embed", new_callable=AsyncMock,
                return_value=_make_mock_embedded(),
            ),
        ):
            state = await pipeline.run(audio_file, meeting_id="test_chunk_embed")

        assert state.status == "completed"
        assert state.completed_steps == [
            "convert",
            "transcribe",
            "diarize",
            "merge",
            "correct",
            "summarize",
            "chunk",
            "embed",
        ]

    @pytest.mark.asyncio
    async def test_chunk_called_with_corrected_result(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """_run_step_chunk 는 corrected_result 를 입력으로 받아야 한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        mock_corrected = _make_mock_corrected()
        chunk_mock = AsyncMock(return_value=_make_mock_chunked())

        with (
            patch.object(pipeline, "_run_step_convert", new_callable=AsyncMock,
                         return_value=wav_path),
            patch.object(pipeline, "_run_step_transcribe", new_callable=AsyncMock,
                         return_value=_make_mock_transcript()),
            patch.object(pipeline, "_run_step_diarize", new_callable=AsyncMock,
                         return_value=_make_mock_diarization()),
            patch.object(pipeline, "_run_step_merge", new_callable=AsyncMock,
                         return_value=_make_mock_merged()),
            patch.object(pipeline, "_run_step_correct", new_callable=AsyncMock,
                         return_value=mock_corrected),
            patch.object(pipeline, "_run_step_summarize", new_callable=AsyncMock,
                         return_value=_make_mock_summary()),
            patch.object(pipeline, "_run_step_chunk", chunk_mock),
            patch.object(pipeline, "_run_step_embed", new_callable=AsyncMock,
                         return_value=_make_mock_embedded()),
        ):
            await pipeline.run(audio_file, meeting_id="test_chunk_input")

        assert chunk_mock.called, "_run_step_chunk 가 호출되지 않았습니다."
        # 첫 번째 인자는 corrected_result 여야 함
        call_args = chunk_mock.call_args
        assert call_args.args[0] is mock_corrected or call_args.kwargs.get("corrected") is mock_corrected, (
            f"_run_step_chunk 의 첫 인자가 corrected_result 가 아닙니다. args={call_args}"
        )

    @pytest.mark.asyncio
    async def test_embed_called_with_chunked_result(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """_run_step_embed 는 chunked_result 를 입력으로 받아야 한다."""
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        mock_chunked = _make_mock_chunked()
        embed_mock = AsyncMock(return_value=_make_mock_embedded())

        with (
            patch.object(pipeline, "_run_step_convert", new_callable=AsyncMock,
                         return_value=wav_path),
            patch.object(pipeline, "_run_step_transcribe", new_callable=AsyncMock,
                         return_value=_make_mock_transcript()),
            patch.object(pipeline, "_run_step_diarize", new_callable=AsyncMock,
                         return_value=_make_mock_diarization()),
            patch.object(pipeline, "_run_step_merge", new_callable=AsyncMock,
                         return_value=_make_mock_merged()),
            patch.object(pipeline, "_run_step_correct", new_callable=AsyncMock,
                         return_value=_make_mock_corrected()),
            patch.object(pipeline, "_run_step_summarize", new_callable=AsyncMock,
                         return_value=_make_mock_summary()),
            patch.object(pipeline, "_run_step_chunk", new_callable=AsyncMock,
                         return_value=mock_chunked),
            patch.object(pipeline, "_run_step_embed", embed_mock),
        ):
            await pipeline.run(audio_file, meeting_id="test_embed_input")

        assert embed_mock.called, "_run_step_embed 가 호출되지 않았습니다."
        call_args = embed_mock.call_args
        assert call_args.args[0] is mock_chunked or call_args.kwargs.get("chunked") is mock_chunked, (
            f"_run_step_embed 의 첫 인자가 chunked_result 가 아닙니다. args={call_args}"
        )


class TestChunkEmbedFailureIsolation:
    """CHUNK / EMBED 단계 실패가 회의록 생성에는 영향이 없어야 함."""

    @pytest.mark.asyncio
    async def test_chunk_failure_after_summarize_completed(
        self,
        pipeline: PipelineManager,
        audio_file: Path,
    ) -> None:
        """CHUNK 실패 시 SUMMARIZE 까지는 완료 상태여야 한다.

        검색 인덱싱 실패가 회의록 생성을 차단해서는 안 된다는 정책 검증.
        """
        wav_path = audio_file.parent / "test_16k.wav"
        wav_path.write_bytes(b"fake wav content")

        with (
            patch.object(pipeline, "_run_step_convert", new_callable=AsyncMock,
                         return_value=wav_path),
            patch.object(pipeline, "_run_step_transcribe", new_callable=AsyncMock,
                         return_value=_make_mock_transcript()),
            patch.object(pipeline, "_run_step_diarize", new_callable=AsyncMock,
                         return_value=_make_mock_diarization()),
            patch.object(pipeline, "_run_step_merge", new_callable=AsyncMock,
                         return_value=_make_mock_merged()),
            patch.object(pipeline, "_run_step_correct", new_callable=AsyncMock,
                         return_value=_make_mock_corrected()),
            patch.object(pipeline, "_run_step_summarize", new_callable=AsyncMock,
                         return_value=_make_mock_summary()),
            patch.object(pipeline, "_run_step_chunk", new_callable=AsyncMock,
                         side_effect=RuntimeError("청크 분할 실패")),
        ):
            with pytest.raises(PipelineStepError) as exc_info:
                await pipeline.run(audio_file, meeting_id="test_chunk_fail")

            assert exc_info.value.step == "chunk"

        # SUMMARIZE 까지는 완료 체크포인트가 남아있어야 함 → 재개 가능
        # (state 는 raise 직전까지 저장되므로 summarize 가 completed_steps 에 포함)
        state_path = (
            pipeline._checkpoints_dir / "test_chunk_fail" / "pipeline_state.json"
        )
        assert state_path.exists(), "파이프라인 상태 파일이 저장되지 않았습니다."

        from core.pipeline import PipelineState
        state = PipelineState.from_file(state_path)
        assert "summarize" in state.completed_steps, (
            "CHUNK 실패 시에도 SUMMARIZE 는 완료되어 있어야 합니다."
        )
