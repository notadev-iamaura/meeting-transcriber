"""OpenAI 전사 선택과 파이프라인 재개 계약을 검증한다.

외부 네트워크, 실제 STT, pyannote는 모두 mock으로 대체한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers.meeting_detail import _build_meeting_item
from config import AppConfig, PathsConfig
from core.pipeline import PIPELINE_STEPS, InvalidInputError, PipelineManager, PipelineState
from steps.diarizer import DiarizationResult, DiarizationSegment
from steps.transcriber import TranscriptResult, TranscriptSegment


def _config(tmp_path: Path) -> AppConfig:
    """임시 저장소를 사용하는 실제 설정을 만든다."""
    return AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))


def _openai_transcript(audio_path: Path, *, with_speakers: bool = True) -> TranscriptResult:
    """테스트용 OpenAI 전사 결과를 만든다."""
    return TranscriptResult(
        segments=[
            TranscriptSegment(
                text="안녕하세요",
                start=0.0,
                end=1.5,
                speaker="SPEAKER_00" if with_speakers else None,
            ),
            TranscriptSegment(
                text="회의를 시작합니다",
                start=1.5,
                end=3.0,
                speaker="SPEAKER_01" if with_speakers else None,
            ),
        ],
        full_text="안녕하세요 회의를 시작합니다",
        language="ko",
        audio_path=str(audio_path),
        provider="openai",
        model="gpt-4o-transcribe-diarize",
    )


def test_회의_응답은_실제_전사_provider와_model을_노출한다() -> None:
    """뷰어가 처리 위치를 확인할 수 있도록 비밀 없는 provenance만 반환한다."""
    job = SimpleNamespace(
        id=1,
        meeting_id="meeting_001",
        audio_path="/private/meeting.wav",
        status="completed",
        retry_count=0,
        error_message="",
        created_at="",
        updated_at="",
        title="",
    )

    item = _build_meeting_item(
        job,
        pipeline_state={
            "stt_provider": "openai",
            "stt_model": "gpt-4o-transcribe-diarize",
        },
    )

    assert item.stt_provider == "openai"
    assert item.stt_model == "gpt-4o-transcribe-diarize"


def test_pipeline_state_생성전에는_queue_snapshot을_처리위치로_노출한다() -> None:
    """queued 취소·대기 화면도 실제 재개될 OpenAI 선택을 숨기지 않는다."""
    job = SimpleNamespace(
        id=2,
        meeting_id="meeting_queued",
        audio_path="/private/meeting.wav",
        status="recorded",
        retry_count=0,
        error_message="",
        created_at="",
        updated_at="",
        title="",
        stt_provider="openai",
        stt_model="gpt-4o-transcribe-diarize",
    )

    item = _build_meeting_item(job)

    assert item.stt_provider == "openai"
    assert item.stt_model == "gpt-4o-transcribe-diarize"


@pytest.mark.asyncio
async def test_전사_단계는_고정된_OpenAI_선택만_어댑터로_전달한다(
    tmp_path: Path,
) -> None:
    """명시된 provider/model이 로컬 Transcriber 없이 OpenAI 어댑터로 전달된다."""
    config = _config(tmp_path)
    pipeline = PipelineManager(config, MagicMock())
    audio_path = tmp_path / "meeting.wav"
    audio_path.write_bytes(b"mock-wave")
    checkpoint = tmp_path / "transcribe.json"
    expected = _openai_transcript(audio_path)
    openai_instance = MagicMock()
    openai_instance.transcribe = AsyncMock(return_value=expected)

    with (
        patch.object(pipeline, "_validate_input", return_value=(1, 2, 3, 4, 5)),
        patch.object(pipeline, "_validate_audio_duration", new_callable=AsyncMock),
        patch("core.pipeline.measure_audio_duration", return_value=60.0),
        patch(
            "steps.openai_transcriber.OpenAITranscriber",
            return_value=openai_instance,
        ) as openai_cls,
        patch("steps.transcriber.Transcriber") as local_cls,
        patch(
            "steps.hallucination_filter.filter_hallucinations",
            return_value=(expected.segments, 0),
        ),
        patch(
            "steps.text_postprocessor.postprocess_segments",
            return_value=expected.segments,
        ),
    ):
        result = await pipeline._run_step_transcribe(
            audio_path,
            checkpoint,
            stt_provider="openai",
            stt_model="gpt-4o-transcribe-diarize",
        )

    assert result is expected
    local_cls.assert_not_called()
    execution_config = openai_cls.call_args.args[0]
    assert execution_config.stt.provider == "openai"
    assert execution_config.stt.openai_model == "gpt-4o-transcribe-diarize"
    openai_instance.transcribe.assert_awaited_once()
    transcribe_kwargs = openai_instance.transcribe.await_args.kwargs
    assert transcribe_kwargs["timeout_override"] is None
    assert transcribe_kwargs["resume_dir"] == tmp_path / ".openai-transcribe-parts"
    assert transcribe_kwargs["should_cancel"] is None
    assert transcribe_kwargs["expected_audio_identity"] == (1, 2, 3, 4, 5)
    openai_instance.cleanup_resume_cache.assert_called_once_with(
        tmp_path / ".openai-transcribe-parts"
    )


@pytest.mark.asyncio
async def test_OpenAI_화자_세그먼트가_있으면_pyannote를_우회한다(
    tmp_path: Path,
) -> None:
    """provider 화자/시간 구간은 기존 병합 형식으로 변환하고 로컬 모델을 열지 않는다."""
    config = _config(tmp_path)
    pipeline = PipelineManager(config, MagicMock())
    audio_path = tmp_path / "meeting.wav"
    transcript = _openai_transcript(audio_path)
    checkpoint = tmp_path / "diarize.json"

    with patch("steps.diarizer.Diarizer") as diarizer_cls:
        result = await pipeline._run_step_diarize(
            audio_path,
            checkpoint,
            transcript,
        )

    diarizer_cls.assert_not_called()
    assert result.output_mode == "provider"
    assert result.model_name == "openai:gpt-4o-transcribe-diarize"
    assert [segment.speaker for segment in result.segments] == [
        "SPEAKER_00",
        "SPEAKER_01",
    ]


@pytest.mark.asyncio
async def test_OpenAI_provider_병합은_겹말에서도_원래_화자를_보존한다(
    tmp_path: Path,
) -> None:
    """provider segment를 overlap 재추론하지 않아 짧은 겹말 화자를 보존한다."""
    pipeline = PipelineManager(_config(tmp_path), MagicMock())
    audio_path = tmp_path / "meeting.wav"
    transcript = TranscriptResult(
        segments=[
            TranscriptSegment(
                text="긴 발화",
                start=0.0,
                end=10.0,
                speaker="SPEAKER_00",
            ),
            TranscriptSegment(
                text="짧은 겹말",
                start=2.0,
                end=3.0,
                speaker="SPEAKER_01",
            ),
        ],
        full_text="긴 발화 짧은 겹말",
        language="auto",
        audio_path=str(audio_path),
        provider="openai",
        model="gpt-4o-transcribe-diarize",
    )
    diarization = await pipeline._run_step_diarize(
        audio_path,
        tmp_path / "diarize-overlap.json",
        transcript,
    )

    result = await pipeline._run_step_merge(
        transcript,
        diarization,
        tmp_path / "merge-overlap.json",
    )

    assert [utterance.speaker for utterance in result.utterances] == [
        "SPEAKER_00",
        "SPEAKER_01",
    ]
    assert result.num_speakers == 2
    assert result.unknown_count == 0


@pytest.mark.asyncio
async def test_OpenAI_청크에_화자_ID가_없으면_기존_pyannote를_사용한다(
    tmp_path: Path,
) -> None:
    """여러 업로드 청크처럼 화자 ID가 불연속인 결과는 로컬 diarization으로 보완한다."""
    config = _config(tmp_path)
    pipeline = PipelineManager(config, MagicMock())
    audio_path = tmp_path / "meeting.wav"
    transcript = _openai_transcript(audio_path, with_speakers=False)
    checkpoint = tmp_path / "diarize.json"
    expected = DiarizationResult(
        segments=[DiarizationSegment(speaker="SPEAKER_00", start=0.0, end=3.0)],
        num_speakers=1,
        audio_path=str(audio_path),
    )
    diarizer_instance = MagicMock()
    diarizer_instance.diarize = AsyncMock(return_value=expected)

    with (
        patch.object(pipeline, "_validate_input", return_value=(1, 2, 3, 4, 5)),
        patch.object(pipeline, "_validate_audio_duration", new_callable=AsyncMock),
        patch("steps.diarizer.Diarizer", return_value=diarizer_instance) as diarizer_cls,
    ):
        result = await pipeline._run_step_diarize(
            audio_path,
            checkpoint,
            transcript,
        )

    assert result is expected
    diarizer_cls.assert_called_once()
    diarizer_instance.diarize.assert_awaited_once_with(audio_path)


@pytest.mark.asyncio
async def test_과거_완료_상태는_현재_OpenAI_기본값으로_오인하지_않는다(
    tmp_path: Path,
) -> None:
    """provider 필드 도입 전 완료 체크포인트는 legacy local provenance로 이관한다."""
    config = _config(tmp_path)
    config.stt.provider = "openai"
    pipeline = PipelineManager(config, MagicMock())
    meeting_id = "legacy_completed"
    state_path = pipeline._get_state_path(meeting_id)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = PipelineState(
        meeting_id=meeting_id,
        audio_path=str(tmp_path / "missing.wav"),
        status="completed",
        completed_steps=[step.value for step in PIPELINE_STEPS],
    ).to_dict()
    legacy.pop("stt_provider")
    legacy.pop("stt_model")
    state_path.write_text(json.dumps(legacy), encoding="utf-8")

    result = await pipeline.run(tmp_path / "missing.wav", meeting_id=meeting_id)
    restored = PipelineState.from_file(state_path)

    assert result.stt_provider == "local"
    assert result.stt_model == "legacy-local"
    assert restored.stt_provider == "local"
    assert restored.stt_model == "legacy-local"


@pytest.mark.asyncio
async def test_OpenAI_선택은_로컬_전사_체크포인트를_재사용하지_않는다(
    tmp_path: Path,
) -> None:
    """stale 체크포인트가 외부 모델 결과로 잘못 표시되는 것을 차단한다."""
    config = _config(tmp_path)
    pipeline = PipelineManager(config, MagicMock())
    checkpoint = tmp_path / "transcribe.json"
    TranscriptResult(
        segments=[],
        full_text="기존 로컬 결과",
        language="ko",
        audio_path=str(tmp_path / "meeting.wav"),
        provider="local",
        model="mlx-community/whisper-large-v3-turbo",
    ).save_checkpoint(checkpoint)

    with pytest.raises(InvalidInputError, match="provider/model"):
        await pipeline._run_step_transcribe(
            tmp_path / "meeting.wav",
            checkpoint,
            stt_provider="openai",
            stt_model="gpt-4o-transcribe-diarize",
        )


@pytest.mark.asyncio
async def test_로컬_선택도_다른_로컬_모델_체크포인트를_재사용하지_않는다(
    tmp_path: Path,
) -> None:
    """로컬 A/B 모델이 달라져도 stale 결과를 새 모델 provenance로 오인하지 않는다."""
    pipeline = PipelineManager(_config(tmp_path), MagicMock())
    checkpoint = tmp_path / "transcribe.json"
    TranscriptResult(
        segments=[],
        full_text="다른 로컬 모델 결과",
        language="ko",
        audio_path=str(tmp_path / "meeting.wav"),
        provider="local",
        model="youngouk/whisper-medium-komixv2-mlx",
    ).save_checkpoint(checkpoint)

    with pytest.raises(InvalidInputError, match="provider/model"):
        await pipeline._run_step_transcribe(
            tmp_path / "meeting.wav",
            checkpoint,
            stt_provider="local",
            stt_model="mlx-community/whisper-large-v3-turbo",
        )


def test_state_유실_복구는_OpenAI_전사_checkpoint_provenance를_보존한다(
    tmp_path: Path,
) -> None:
    """state 재구축이 신규 OpenAI 회의를 legacy local로 오표기하지 않는다."""
    pipeline = PipelineManager(_config(tmp_path), MagicMock())
    meeting_id = "openai_rebuilt"
    checkpoint = pipeline._get_checkpoint_path(meeting_id, PIPELINE_STEPS[1])
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    _openai_transcript(tmp_path / "meeting.wav").save_checkpoint(checkpoint)

    rebuilt = pipeline._rebuild_state_from_checkpoints(meeting_id)

    assert rebuilt.stt_provider == "openai"
    assert rebuilt.stt_model == "gpt-4o-transcribe-diarize"


@pytest.mark.asyncio
async def test_정상_resume_경로도_state와_다른_전사_checkpoint를_거부한다(
    tmp_path: Path,
) -> None:
    """TRANSCRIBE를 건너뛰는 재개 경로에서도 provenance mismatch를 차단한다."""
    pipeline = PipelineManager(_config(tmp_path), MagicMock())
    meeting_id = "resume_mismatch"
    output_dir = pipeline._get_output_dir(meeting_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    wav_path = output_dir / "meeting_16k.wav"
    wav_path.write_bytes(b"mock-wave")
    state_path = pipeline._get_state_path(meeting_id)
    PipelineState(
        meeting_id=meeting_id,
        audio_path=str(tmp_path / "original.m4a"),
        status="failed",
        completed_steps=["convert", "transcribe"],
        wav_path=str(wav_path),
        output_dir=str(output_dir),
        stt_provider="openai",
        stt_model="gpt-4o-transcribe-diarize",
    ).save(state_path)
    TranscriptResult(
        segments=[],
        full_text="로컬 stale 결과",
        language="ko",
        audio_path=str(wav_path),
        provider="local",
        model="mlx-community/whisper-large-v3-turbo",
    ).save_checkpoint(pipeline._get_checkpoint_path(meeting_id, PIPELINE_STEPS[1]))

    with pytest.raises(InvalidInputError, match="provider/model"):
        await pipeline.run(tmp_path / "original.m4a", meeting_id=meeting_id)
