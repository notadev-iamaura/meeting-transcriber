"""마이크 전사 누락의 영속 상태·정상 접수·마이크만 복구하는 경계를 검증한다."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI

from api.routers.meeting_detail import router
from config import AppConfig, PathsConfig
from core.audio_quality import AudioFailureKind
from core.io_utils import atomic_write_json_pinned
from core.job_queue import AsyncJobQueue, InvalidTransitionError, JobQueue, JobStatus
from core.mic_transcription import mic_transcription_status, uncovered_mic_speech, write_mic_status
from core.model_manager import ModelLoadManager
from core.orchestrator import JobProcessor
from core.pipeline import PipelineError, PipelineManager, PipelineState
from core.recording_channels import RECORDING_TRACK_COMMENT, PreparedRecordingTrack
from core.search_revision import read_source
from steps.corrector import CorrectedResult, CorrectedUtterance, correction_source_sha256
from steps.transcriber import (
    EmptyTranscriptError,
    LowVolumeAudioError,
    TranscriptResult,
    TranscriptSegment,
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """실제 WAV·큐·API·원문 저장을 쓰고 추론과 재색인 모델만 대체한다."""
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    mid = "meeting_mic_retry"
    audio = tmp_path / "recording.wav"
    samples = np.zeros((32 * 16000, 2), dtype=np.float32)
    samples[16000:64000, 0] = 0.03 * np.sin(np.arange(48000) * 0.1)
    samples[16000:64000, 1] = 0.02 * np.sin(np.arange(48000) * 0.07)
    with sf.SoundFile(audio, "w", samplerate=16000, channels=2, subtype="PCM_16") as output:
        output.comment = RECORDING_TRACK_COMMENT
        output.write(samples)
    cp = config.paths.resolved_checkpoints_dir / mid
    cp.mkdir(parents=True)
    source = cp / "correct.json"
    original = {
        "utterances": [
            {
                "speaker": "SPEAKER_07",
                "text": "사용자가 수정한 상대 발화",
                "original_text": "원래 상대 발화",
                "start": 2.0,
                "end": 3.0,
                "was_corrected": True,
            },
            {
                "speaker": "SPEAKER_04",
                "text": "다른 상대 발화",
                "original_text": "다른 상대 발화",
                "start": 7.0,
                "end": 9.0,
                "was_corrected": False,
            },
        ],
        "audio_path": str(audio),
        "num_speakers": 2,
        "meeting_date": "2031-01-15",
        "search_revision": "user-edit",
        "correction_failures": [
            {"index": 1, "reason": "backend_failure", "source_sha256": "preserved"}
        ],
        "failed_indices": [1],
        "total_failed": 1,
    }
    original["correction_failures"][0]["source_sha256"] = correction_source_sha256(
        CorrectedUtterance(**original["utterances"][1])
    )
    atomic_write_json_pinned(source, original)
    write_mic_status(cp / "mic_transcription.json", "empty", True, model=config.stt.model_name)
    PipelineState(meeting_id=mid, audio_path=str(audio), status="completed").save(
        cp / "pipeline_state.json"
    )
    queue = JobQueue(tmp_path / "jobs.db")
    queue.initialize()
    job_id = queue.add_job(
        mid, str(audio), "completed", stt_provider="local", stt_model=config.stt.model_name
    )
    pipeline = PipelineManager(config, model_manager=ModelLoadManager())
    pipeline._validate_audio_duration = AsyncMock()
    pipeline._transcribe_wav = AsyncMock(
        return_value=TranscriptResult(
            [TranscriptSegment(text="복구된 내 발화", start=1, end=4)],
            "복구된 내 발화",
            "ko",
            str(audio),
            model=config.stt.model_name,
        )
    )

    def prepare(path: Path, *_args: Any) -> PreparedRecordingTrack:
        """실제 모델 없이 감지된 구간을 고정한다."""
        return PreparedRecordingTrack(path, (1.0, 4.0), -24.0)

    monkeypatch.setattr("core.mic_transcription.prepare_recording_track", prepare)
    monkeypatch.setattr("core.pipeline.prepare_recording_track", prepare)
    reindex = AsyncMock(return_value={"chunks": 1})
    monkeypatch.setattr("core.reindex_recovery._reindex_meeting_artifacts_locked", reindex)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.config = config
    app.state.job_queue = AsyncJobQueue(queue)
    app.state.pipeline_manager = pipeline
    app.state.meeting_mutation_coordinator = pipeline.meeting_mutation_coordinator
    app.state.chat_engine = MagicMock()
    thermal = AsyncMock()
    worker = JobProcessor(app.state.job_queue, pipeline, thermal, AsyncMock())
    app.state.job_processor = worker
    yield SimpleNamespace(
        config=config,
        mid=mid,
        audio=audio,
        cp=cp,
        source=source,
        original=original,
        queue=queue,
        job_id=job_id,
        pipeline=pipeline,
        reindex=reindex,
        app=app,
        worker=worker,
    )
    queue.close()


async def request(env: Any, method: str, suffix: str) -> httpx.Response:
    """공개 ASGI 라우터와 같은 의존성 경계를 호출한다."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://test"
    ) as client:
        return await client.request(method, f"/api/meetings/{env.mid}/{suffix}")


@pytest.mark.parametrize("track", ["mic", "system", "mix"])
async def test_original_recording_clip_pcm_and_ranges(env: Any, track: str) -> None:
    """원본의 같은 2초에 실제로 겹친 두 신호를 각 채널·혼합과 샘플 단위로 대조한다."""
    assert (await request(env, "GET", "mic-transcription")).json()["split_playback_available"]
    url = f"/api/meetings/{env.mid}/audio/clip?track={track}&start=1.5&end=3.5"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://test"
    ) as client:
        response = await client.get(url)
        assert response.status_code == 200 and "no-store" in response.headers["cache-control"]
        assert len(response.content) == 44 + 32000 * 2
        pcm, rate = sf.read(io.BytesIO(response.content), dtype="int16")
        original, _ = sf.read(env.audio, start=24000, frames=32000, dtype="int16")
        expected = (
            (original[:, 0].astype("int32") + original[:, 1]) // 2
            if track == "mix"
            else original[:, 0 if track == "mic" else 1]
        )
        assert rate == 16000 and np.array_equal(pcm, expected)
        for first, last in ((0, 1), (41, 55), (123, 3002)):
            partial = await client.get(url, headers={"Range": f"bytes={first}-{last}"})
            assert partial.status_code == 206
            assert partial.content == response.content[first : last + 1]


async def test_recording_clip_rejects_outside_or_expired_audio(env: Any) -> None:
    """음성 끝과 만료 상태를 확인하며 컷오프 뒤 캐시된 구간을 반환하지 않는다."""
    path = "audio/clip?track=mic&start=40&end=41"
    assert (await request(env, "GET", path)).status_code == 416
    assert (await request(env, "GET", "audio/clip?track=mic&start=2&end=1")).status_code == 416
    assert env.queue.claim_audio_retention(env.queue.get_job(env.job_id), "2031-03-15", "[]")
    assert (await request(env, "GET", "audio/clip?track=mic&start=1&end=2")).status_code == 410
    assert not (await request(env, "GET", "mic-transcription")).json()["split_playback_available"]


async def test_recording_clip_requires_verified_original(env: Any) -> None:
    """일반 스테레오나 심볼릭 링크를 채널별 원본이라고 표시하지 않는다."""
    path = "audio/clip?track=system&start=1&end=2"
    original = env.audio.with_name("original.wav")
    env.audio.rename(original)
    env.audio.symlink_to(original)
    assert (await request(env, "GET", path)).status_code == 400
    assert not (await request(env, "GET", "mic-transcription")).json()["split_playback_available"]
    env.audio.unlink()
    sf.write(env.audio, np.zeros((24000, 2)), 8000, subtype="PCM_16")
    assert (await request(env, "GET", path)).status_code == 409
    assert not (await request(env, "GET", "mic-transcription")).json()["split_playback_available"]


@pytest.mark.parametrize("failure", ["empty", "low_volume"])
async def test_missing_mic_persists_while_remote_succeeds(env: Any, failure: str) -> None:
    """감지된 마이크만 STT에서 빠져도 회의 성공과 별개로 누락을 기록한다."""
    exc = (
        EmptyTranscriptError("empty")
        if failure == "empty"
        else LowVolumeAudioError("quiet", failure_kind=AudioFailureKind.MEDIA_INVALID)
    )
    remote = TranscriptResult(
        [TranscriptSegment("상대 발화", 1, 4)], "상대 발화", "ko", str(env.audio)
    )
    events: list[dict[str, Any]] = []

    async def transcribe(path: Path, **_kwargs: Any) -> TranscriptResult:
        """상대 트랙이 시작되기 전에 마이크 실패가 전달됐는지 확인한다."""
        if path.name == "channel-0.wav":
            raise exc
        assert events and events[0]["status"] == failure
        return remote

    async def on_mic_status(event: dict[str, Any]) -> None:
        """실제 통지 순서와 payload를 보존한다."""
        events.append(event)

    env.pipeline._transcribe_wav.side_effect = transcribe
    result = await env.pipeline._transcribe_recording_tracks(
        env.audio,
        env.pipeline._validate_input(env.audio),
        selected_model=env.config.stt.model_name,
        should_cancel=None,
        mic_status_path=env.cp / "mic_transcription.json",
        on_mic_status=on_mic_status,
    )
    assert [s.text for s in result.segments] == ["상대 발화"]
    state = (await request(env, "GET", "mic-transcription")).json()
    assert state["status"] == failure and state["speech_detected"] and state["retry_available"]
    assert events[0]["event"] == "mic_transcription_warning"


async def test_no_speech_is_not_a_missing_transcript_warning(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """음성이 감지되지 않은 마이크는 누락 확정이나 재시도 대상으로 표시하지 않는다."""
    monkeypatch.setattr(
        "core.pipeline.prepare_recording_track",
        lambda path, *_args: (
            None if path.name == "channel-0.wav" else PreparedRecordingTrack(path, (1, 4), -24)
        ),
    )
    await env.pipeline._transcribe_recording_tracks(
        env.audio,
        env.pipeline._validate_input(env.audio),
        selected_model=env.config.stt.model_name,
        should_cancel=None,
        mic_status_path=env.cp / "mic_transcription.json",
    )
    state = (await request(env, "GET", "mic-transcription")).json()
    assert state["status"] == "no_speech" and not state["retry_available"]


async def test_actual_api_and_worker_retry_only_mic_preserving_source_and_metadata(
    env: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """정상 큐에서 마이크만 추론하고 상대 편집·실패 인덱스·원본·요약을 보존한다."""
    audio_before = env.audio.read_bytes()
    for name in ("transcribe.json", "merge.json", "summary.json"):
        (env.cp / name).write_text("original-checkpoint")
    response = await request(env, "POST", "mic-transcription/retry")
    assert response.status_code == 202
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 409
    assert (await request(env, "GET", "mic-transcription")).json()["status"] == "retry_queued"
    env.pipeline.run = AsyncMock(side_effect=AssertionError("전체 재전사 금지"))
    assert await env.worker._process_job(env.queue.get_job(env.job_id))
    env.pipeline.run.assert_not_awaited()
    env.pipeline._transcribe_wav.assert_awaited_once()
    assert env.pipeline._transcribe_wav.await_args.args[0].name == "channel-0.wav"
    _, result = read_source(env.config, env.mid)
    assert [u for u in result["utterances"] if u["speaker"] != "나"] == env.original["utterances"]
    assert result["correction_failures"][0]["index"] == 2 and result["failed_indices"] == [2]
    assert (
        result["correction_failures"][0]["source_sha256"]
        == env.original["correction_failures"][0]["source_sha256"]
    )
    assert CorrectedResult.from_checkpoint(env.source).failure_tracking_complete
    assert result["search_revision"] != "user-edit" and result["meeting_date"] == "2031-01-15"
    assert env.audio.read_bytes() == audio_before
    assert all(
        (env.cp / name).read_text() == "original-checkpoint"
        for name in ("transcribe.json", "merge.json", "summary.json")
    )
    assert env.queue.get_job(env.job_id).status == "completed"
    assert "작업 완료 상태 업데이트 실패" not in caplog.text
    state = (await request(env, "GET", "transcript")).json()
    assert state["mic_transcription"]["status"] == "complete" and state["summary_needs_review"]


async def test_mic_recovery_refreshes_agenda_without_rebinding_old_minutes(env: Any) -> None:
    """추가된 내 발화를 재시작 없이 찾고, 재전사 전에 작성한 회의록은 분리한다."""
    from core.agenda_records import (
        agenda_for_topic,
        minutes_match_transcript,
        refresh_agenda_records,
    )

    minutes = "## 결정 사항\n- 기존 계획은 금요일에 공유하기로 했습니다."
    output = env.config.paths.resolved_outputs_dir / env.mid
    output.mkdir(parents=True)
    (output / "meeting_minutes.md").write_text(minutes, encoding="utf-8")
    refresh_agenda_records(env.config, env.mid, published_markdown=minutes)
    assert agenda_for_topic(env.config, env.mid, "상대 발화") is not None
    assert minutes_match_transcript(env.config, env.mid, minutes)

    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 202
    assert await env.worker._process_job(env.queue.get_job(env.job_id))

    recovered = agenda_for_topic(env.config, env.mid, "복구된", anchor_index=0)
    assert recovered is not None and recovered.title == "복구된 내 발화"
    assert agenda_for_topic(env.config, env.mid, "상대 발화") is not None
    assert not minutes_match_transcript(env.config, env.mid, minutes)


async def test_stt_failure_keeps_current_source_and_can_retry(env: Any) -> None:
    """실제 재시도 실패는 원문을 교체하지 않으며 다음 요청을 허용한다."""
    before = env.source.read_bytes()
    env.pipeline._transcribe_wav.side_effect = EmptyTranscriptError("still empty")
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 202
    assert not await env.worker._process_job(env.queue.get_job(env.job_id))
    assert env.source.read_bytes() == before
    state = (await request(env, "GET", "mic-transcription")).json()
    assert state["status"] == "retry_failed" and state["retry_available"]
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 202


async def test_reindex_failure_resumes_without_duplicate_mic_or_second_stt(env: Any) -> None:
    """게시 뒤 실패/프로세스 교체에서도 같은 마이크를 두 번 추가하지 않는다."""
    env.reindex.side_effect = [RuntimeError("index unavailable"), {"chunks": 1}]
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 202
    assert not await env.worker._process_job(env.queue.get_job(env.job_id))
    published = env.source.read_bytes()
    assert (await request(env, "GET", "mic-transcription")).json()["retry_available"]
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 202
    assert await env.worker._process_job(env.queue.get_job(env.job_id))
    assert env.source.read_bytes() == published
    # 색인 재개 완료 직후 DB 완료 전 종료되어도 완료 저널의 새 요청을 재사용한다.
    await env.pipeline.retry_mic(env.audio, env.mid, stt_model=env.config.stt.model_name)
    env.pipeline._transcribe_wav.assert_awaited_once()
    assert (
        len([u for u in read_source(env.config, env.mid)[1]["utterances"] if u["speaker"] == "나"])
        == 1
    )


async def test_only_mic_all_empty_original_can_recover_from_failed_without_transcript(
    env: Any,
) -> None:
    """상대방 무음·내 전사 실패로 원문이 없어도 공개 API로 마이크를 복구한다."""
    env.source.unlink()
    env.queue.force_set_status(env.job_id, JobStatus.FAILED, "all empty")
    PipelineState(meeting_id=env.mid, audio_path=str(env.audio), status="failed").save(
        env.cp / "pipeline_state.json"
    )
    assert (await request(env, "GET", "mic-transcription")).json()["retry_available"]
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 202
    assert await env.worker._process_job(env.queue.get_job(env.job_id))
    transcript = (await request(env, "GET", "transcript")).json()
    assert len(transcript["utterances"]) == 1 and transcript["speakers"] == ["나"]
    assert json.loads((env.cp / "pipeline_state.json").read_text())["status"] == "completed"
    recovered = json.loads((env.cp / "pipeline_state.json").read_text())
    assert set(recovered["completed_steps"]) == {"transcribe", "chunk", "embed"}
    assert {"convert", "diarize", "merge", "correct", "summarize"} <= set(
        recovered["skipped_steps"]
    )


async def test_user_added_self_is_preserved_and_untagged_audio_is_blocked(env: Any) -> None:
    """사용자가 추가한 '나'는 겹친 결과로 덮지 않고 출처 미확인 녹음은 차단한다."""
    data = dict(env.original)
    data["utterances"] = [dict(env.original["utterances"][0], speaker="나")]
    atomic_write_json_pinned(env.source, data)
    before = env.source.read_bytes()
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 202
    assert await env.worker._process_job(env.queue.get_job(env.job_id))
    assert env.source.read_bytes() == before
    state = (await request(env, "GET", "mic-transcription")).json()
    assert state["no_new_speech"] and state["added_utterances"] == 0
    env.reindex.assert_not_awaited()
    atomic_write_json_pinned(env.source, env.original)
    sf.write(env.audio, np.zeros((32 * 16000, 2)), 16000, subtype="PCM_16")
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 409
    env.pipeline._transcribe_wav.assert_awaited_once()


async def test_same_meeting_mutation_lease_blocks_retry_processing(env: Any) -> None:
    """편집·삭제와 같은 회의의 잠금을 공유하며 모델을 먼저 실행하지 않는다."""
    async with env.pipeline.meeting_mutation_coordinator.lease(env.mid):
        pending = asyncio.create_task(
            env.pipeline.retry_mic(env.audio, env.mid, stt_model=env.config.stt.model_name)
        )
        await asyncio.sleep(0)
        env.pipeline._transcribe_wav.assert_not_awaited()
    await pending


def test_short_status_transition_applies_only_to_mic_retry(env: Any) -> None:
    """전체 전사의 단계 전이는 넓히지 않고 마이크 작업의 실제 단축 단계만 허용한다."""
    env.queue.queue_mic_retry(env.job_id, stt_model=env.config.stt.model_name)
    env.queue.claim_queued_job_for_processing(env.job_id)
    env.queue.update_status(env.job_id, JobStatus.EMBEDDING)
    env.queue.update_status(env.job_id, JobStatus.COMPLETED)
    other = env.queue.add_job("normal", str(env.audio))
    env.queue.claim_queued_job_for_processing(other)
    with pytest.raises(InvalidTransitionError):
        env.queue.update_status(other, JobStatus.EMBEDDING)


async def test_publish_conflict_preserves_edit_and_next_request_rebases_cached_mic(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """게시 직전의 원문 경합을 차단하고 재요청에서는 최신 편집에만 마이크를 더한다."""
    real_write = atomic_write_json_pinned
    conflicted = False

    def write(path: Path, data: dict[str, Any], **kwargs: Any) -> None:
        """저널 저장 직후 외부 편집 경합을 한 번만 재현한다."""
        nonlocal conflicted
        real_write(path, data, **kwargs)
        if path.name == "mic_retry.json" and data["phase"] == "prepared" and not conflicted:
            conflicted = True
            revised = json.loads(env.source.read_text())
            revised["utterances"][0]["text"] = "게시 직전에 한 사용자 편집"
            real_write(env.source, revised)

    monkeypatch.setattr("core.mic_transcription.atomic_write_json_pinned", write)
    with pytest.raises(PipelineError, match="원문이 변경"):
        await env.pipeline.retry_mic(env.audio, env.mid, stt_model=env.config.stt.model_name)
    assert not any(u["speaker"] == "나" for u in read_source(env.config, env.mid)[1]["utterances"])
    await env.pipeline.retry_mic(env.audio, env.mid, stt_model=env.config.stt.model_name)
    rows = read_source(env.config, env.mid)[1]["utterances"]
    assert rows[1]["text"] == "게시 직전에 한 사용자 편집"
    env.pipeline._transcribe_wav.assert_awaited_once()


async def test_queue_cancel_and_shutdown_retry_keep_mic_intent(env: Any) -> None:
    """대기 취소 후 다시 접수하거나 앱 종료로 재대기해도 전체 전사로 바꾸지 않는다."""
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 202
    assert (await request(env, "POST", "cancel")).status_code == 200
    assert (await request(env, "GET", "mic-transcription")).json()["retry_available"]
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 202
    env.pipeline._transcribe_wav.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await env.worker._process_job(env.queue.get_job(env.job_id))
    interrupted = env.queue.get_job(env.job_id)
    assert interrupted.status == "queued" and interrupted.requested_action == "retry_mic"
    env.pipeline._transcribe_wav.side_effect = None
    assert await env.worker._process_job(interrupted)


async def test_status_survives_process_reload_and_corrupt_or_symlink_state_is_blocked(
    env: Any, tmp_path: Path
) -> None:
    """영속 상태를 새 조회가 읽으며 손상·symlink는 성공 상태로 숨기지 않는다."""
    assert (
        mic_transcription_status(env.config, env.mid, env.queue.get_job(env.job_id))["status"]
        == "empty"
    )
    path = env.cp / "mic_transcription.json"
    path.write_text("invalid-json")
    assert (await request(env, "GET", "mic-transcription")).status_code == 409
    target = tmp_path / "outside.json"
    target.write_text('{"status":"empty"}')
    path.unlink()
    path.symlink_to(target)
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 409
    assert target.read_text() == '{"status":"empty"}'


async def test_completed_journal_replay_is_idempotent(env: Any) -> None:
    """게시·색인 완료 후 DB 완료 전 종료된 작업은 추론 없이 완료를 회복한다."""
    await env.pipeline.retry_mic(env.audio, env.mid, stt_model=env.config.stt.model_name)
    before = env.source.read_bytes()
    await env.pipeline.retry_mic(env.audio, env.mid, stt_model=env.config.stt.model_name)
    assert env.source.read_bytes() == before
    env.pipeline._transcribe_wav.assert_awaited_once()
    env.reindex.assert_awaited_once()


async def test_failed_retry_is_not_reconciled_by_old_completed_pipeline(env: Any) -> None:
    """기존 전체 처리 완료 기록으로 마이크 재시도 실패를 성공으로 오인하지 않는다."""
    from api.routers.meeting_detail import reconcile_job_state_for_response

    env.queue.queue_mic_retry(env.job_id, stt_model=env.config.stt.model_name)
    env.queue.force_set_status(env.job_id, JobStatus.FAILED, "mic failed")
    job, _, reason = await reconcile_job_state_for_response(
        env.queue, env.config, env.queue.get_job(env.job_id)
    )
    assert job.status == "failed" and reason == ""


async def test_legacy_split_recording_is_unknown_without_invented_warning(env: Any) -> None:
    """과거 처리 기록이 없다는 사실과 마이크 발화 누락을 구분한다."""
    (env.cp / "mic_transcription.json").unlink()
    atomic_write_json_pinned(
        env.cp / "transcribe.json", {"separate_mic_track": True, "segments": []}
    )
    state = (await request(env, "GET", "mic-transcription")).json()
    assert (
        state["status"] == "unknown" and not state["speech_detected"] and state["retry_available"]
    )
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 202
    assert await env.worker._process_job(env.queue.get_job(env.job_id))


async def test_startup_recovers_inflight_mic_job_as_same_intent(env: Any) -> None:
    """프로세스 종료 후 남은 마이크 실행을 같은 의도로 복구한다."""
    env.queue.queue_mic_retry(env.job_id, stt_model=env.config.stt.model_name)
    env.queue.claim_queued_job_for_processing(env.job_id)
    await env.worker._recover_orphaned_jobs()
    recovered = env.queue.get_job(env.job_id)
    assert recovered.status == "queued" and recovered.requested_action == "retry_mic"


async def test_one_of_four_missing_is_partial_before_remote_stt(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4개 중 1개만 빠져도 상대 전사 전에 경고하고 음성 없는 중간 공백은 제외한다."""
    speech = ((1, 3), (10, 13), (20, 23), (29, 31))
    monkeypatch.setattr(
        "core.pipeline.prepare_recording_track",
        lambda path, *_args: PreparedRecordingTrack(path, (1, 31), -24, speech),
    )
    events: list[dict[str, Any]] = []

    async def transcribe(path: Path, **_kwargs: Any) -> TranscriptResult:
        """준비된 실제 음성과 반환 전사 사이의 한 발화 누락을 재현한다."""
        if path.name == "channel-0.wav":
            return TranscriptResult(
                [
                    TranscriptSegment(str(index), start, end)
                    for index, (start, end) in enumerate(speech)
                    if index != 2
                ],
                "내 발화 3개",
                "ko",
                str(path),
            )
        assert events[0]["status"] == "partial"
        return TranscriptResult([TranscriptSegment("상대 발화", 20, 23)], "상대", "ko", str(path))

    async def notify(event: dict[str, Any]) -> None:
        """처리 중 즉시 경고를 보존한다."""
        events.append(event)

    env.pipeline._transcribe_wav.side_effect = transcribe
    result = await env.pipeline._transcribe_recording_tracks(
        env.audio,
        env.pipeline._validate_input(env.audio),
        selected_model=env.config.stt.model_name,
        should_cancel=None,
        mic_status_path=env.cp / "mic_transcription.json",
        on_mic_status=notify,
    )
    assert len([s for s in result.segments if s.speaker == "나"]) == 3
    state = (await request(env, "GET", "mic-transcription")).json()
    assert state["status"] == "partial" and state["uncovered_intervals"] == [[20.0, 23.0]]


def test_coverage_uses_only_verified_speech_and_tolerates_timestamp_edges(env: Any) -> None:
    """5초 미만 무음 병합·짧은 응답·경계 오차를 발화 누락으로 경고하지 않는다."""
    settings = env.config.recording.split_track_processing
    assert uncovered_mic_speech([], [], settings) == []
    assert uncovered_mic_speech(((1, 2), (6, 6.4)), [{"start": 1.3, "end": 1.7}], settings) == []
    assert (
        uncovered_mic_speech(
            ((1, 2), (5, 6)), [{"start": 1, "end": 2}, {"start": 5, "end": 6}], settings
        )
        == []
    )


async def test_partial_retry_preserves_all_existing_rows_then_explicit_repeat_is_noop(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """기존 자기3개/상대 편집을 보존해 새1개만 추가하며 재요청은 원문·색인 무변경이다."""
    own = [
        {
            "speaker": "나",
            "start": left,
            "end": right,
            "text": f"직접 수정 {left}",
            "original_text": "보존",
            "was_corrected": True,
        }
        for left, right in ((1, 3), (10, 13), (29, 31))
    ]
    source = dict(env.original, utterances=env.original["utterances"] + own)
    atomic_write_json_pinned(env.source, source)
    write_mic_status(env.cp / "mic_transcription.json", "partial", True)
    speech = ((1, 3), (10, 13), (20, 23), (29, 31))
    monkeypatch.setattr(
        "core.mic_transcription.prepare_recording_track",
        lambda path, *_args: PreparedRecordingTrack(path, (1, 31), -24, speech),
    )
    env.pipeline._transcribe_wav.return_value = TranscriptResult(
        [TranscriptSegment(f"재인식 {left}", left, right) for left, right in speech],
        "전체",
        "ko",
        str(env.audio),
    )
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 202
    assert await env.worker._process_job(env.queue.get_job(env.job_id))
    rows = read_source(env.config, env.mid)[1]["utterances"]
    assert all(row in rows for row in source["utterances"])
    assert len(rows) == len(source["utterances"]) + 1
    state = (await request(env, "GET", "mic-transcription")).json()
    assert (
        state["status"] == "complete"
        and state["added_utterances"] == 1
        and state["retry_available"]
    )
    before = env.source.read_bytes()
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 202
    assert await env.worker._process_job(env.queue.get_job(env.job_id))
    assert env.source.read_bytes() == before
    assert env.pipeline._transcribe_wav.await_count == 2
    env.reindex.assert_awaited_once()
    state = (await request(env, "GET", "mic-transcription")).json()
    assert (
        state["status"] == "complete" and state["no_new_speech"] and state["added_utterances"] == 0
    )


async def test_overlap_changed_segmentation_is_never_used_to_replace_existing_self(
    env: Any,
) -> None:
    """새 STT가 기존 발화와 누락 구간을 묶어도 편집된 자기 발화를 지우지 않는다."""
    original = dict(env.original["utterances"][0], speaker="나", start=1, end=3)
    atomic_write_json_pinned(env.source, dict(env.original, utterances=[original]))
    before = env.source.read_bytes()
    env.pipeline._transcribe_wav.return_value.segments = [TranscriptSegment("합쳐진 전사", 1, 8)]
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 202
    assert await env.worker._process_job(env.queue.get_job(env.job_id))
    assert env.source.read_bytes() == before
    env.reindex.assert_not_awaited()


def test_generic_failed_retry_preserves_mic_action(env: Any) -> None:
    """일반 개별·일괄 재시도도 기존 마이크 전용 의도를 지우지 않는다."""
    env.queue.queue_mic_retry(env.job_id, stt_model=env.config.stt.model_name)
    env.queue.force_set_status(env.job_id, JobStatus.FAILED)
    assert env.queue.retry_job(env.job_id).requested_action == "retry_mic"
    env.queue.force_set_status(env.job_id, JobStatus.FAILED)
    assert env.job_id in env.queue.retry_all_failed()
    assert env.queue.get_job(env.job_id).requested_action == "retry_mic"


@pytest.mark.parametrize("saved_status", [None, "empty"])
async def test_audio_admission_failure_cannot_hide_existing_transcript(
    env: Any, saved_status: str | None
) -> None:
    """원본 경로가 symlink면 마이크 재시도만 막고 정상 전사문 조회는 보존한다."""
    if saved_status is None:
        (env.cp / "mic_transcription.json").unlink()
    target = env.audio.with_name("original.wav")
    env.audio.rename(target)
    env.audio.symlink_to(target)
    before = target.read_bytes()
    state = await request(env, "GET", "mic-transcription")
    assert state.status_code == 200 and not state.json()["retry_available"]
    response = await request(env, "GET", "transcript")
    assert response.status_code == 200
    assert [row["text"] for row in response.json()["utterances"]] == [
        row["text"] for row in env.original["utterances"]
    ]
    assert (await request(env, "POST", "mic-transcription/retry")).status_code == 409
    assert target.read_bytes() == before
    env.pipeline._transcribe_wav.assert_not_awaited()


@pytest.mark.parametrize("mic_action", [True, False])
async def test_generic_retry_keeps_mic_local_snapshot_and_normal_cloud_gate(
    env: Any, monkeypatch: pytest.MonkeyPatch, mic_action: bool
) -> None:
    """마이크 전용은 현재 로컬 snapshot을 쓰고 일반 재시도의 OpenAI gate는 유지한다."""
    if mic_action:
        env.queue.queue_mic_retry(env.job_id, stt_model=env.config.stt.model_name)
    env.queue.force_set_status(env.job_id, JobStatus.FAILED)
    PipelineState(
        meeting_id=env.mid,
        audio_path=str(env.audio),
        status="failed",
        stt_provider="openai",
        stt_model="gpt-4o-transcribe-diarize",
    ).save(env.cp / "pipeline_state.json")
    admission = AsyncMock()
    monkeypatch.setattr("api.routers.meeting_detail._require_audio_quality_accept", admission)
    response = await request(env, "POST", "retry")
    if mic_action:
        assert response.status_code == 200
        job = env.queue.get_job(env.job_id)
        assert job.requested_action == "retry_mic" and job.stt_provider == "local"
        assert job.stt_model == env.config.stt.model_name and job.status == "queued"
        admission.assert_awaited_once()
    else:
        assert response.status_code == 403
        assert env.queue.get_job(env.job_id).status == "failed"
        admission.assert_not_awaited()
