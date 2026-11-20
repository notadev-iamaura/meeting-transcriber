"""분리 녹음부터 같은 회의의 전사·화자분리까지 검증한다."""

from __future__ import annotations

import asyncio
import shutil
import struct
import sys
import time
import wave
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest

from config import (
    AppConfig,
    DiarizationConfig,
    PathsConfig,
    RecordingConfig,
    SplitTrackProcessingConfig,
)
from core.audio_quality import AudioQualityStatus, validate_audio_quality
from core.job_queue import AsyncJobQueue, JobQueue, JobStatus
from core.model_manager import ModelLoadManager
from core.pipeline import PipelineManager, PipelineStep, ResourceStatus
from core.recording_channels import (
    RECORDING_TRACK_COMMENT,
    has_separate_mic_track,
    prepare_recording_track,
)
from core.watcher import FolderWatcher, _OpenWriterState
from core.whisper_backend import _transcribe_recording_clips
from steps.audio_converter import AudioConverter
from steps.diarizer import DiarizationResult, DiarizationSegment, Diarizer
from steps.merger import MergedResult
from steps.recorder import AudioDevice, AudioDeviceError, AudioRecorder
from steps.transcriber import (
    Transcriber,
    TranscriptResult,
    TranscriptSegment,
    inspect_audio_path_no_symlinks,
)
from steps.zoom_activity import ZoomAudioActivityChecker

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="실제 WAV 변환에 ffmpeg와 ffprobe가 필요합니다.",
)


async def test_native_recording_failure_does_not_announce_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """장치 입력이 오기 전에 캡처가 종료되면 녹음 중 상태를 만들지 않는다."""
    recorder = AudioRecorder(
        AppConfig(
            paths=PathsConfig(base_dir=str(tmp_path)),
            recording=RecordingConfig(zoom_process_tap=False),
        )
    )
    device = AudioDevice(index=2, name="Test Aggregate", is_aggregate=True)
    monkeypatch.setattr(recorder, "_select_audio_device", AsyncMock(return_value=device))
    monkeypatch.setattr(
        ZoomAudioActivityChecker, "aggregate_mic_channel", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(
        ZoomAudioActivityChecker,
        "split_recording_command",
        lambda *_args: [sys.executable, "-c", "raise SystemExit(2)"],
    )
    with pytest.raises(AudioDeviceError, match="입력을 받기 전에 종료"):
        await recorder.start_recording(meeting_id="capture_not_started")
    status = recorder.get_status()
    assert not status["is_recording"] and status["audio_levels"] is None
    assert recorder._process is None


def test_disjoint_recording_clips_decode_only_requested_samples(tmp_path: Path) -> None:
    """긴 무음을 모델에 넘기지 않고 두 번째 발화도 원래 시각에 놓는다."""
    path = tmp_path / "two-utterances.wav"
    audio = np.zeros(16000 * 95, dtype="<i2")
    audio[16000 : 2 * 16000] = 6000
    audio[92 * 16000 : 93 * 16000] = -9000
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(audio.tobytes())

    spans = []

    def fake_whisper(samples: np.ndarray, **_options: Any) -> dict[str, Any]:
        spans.append((len(samples), float(samples[500])))
        return {
            "text": "한마디",
            "segments": [{"start": 0.0, "end": 0.9, "words": [{"start": 0.10, "end": 0.8}]}],
        }

    result = _transcribe_recording_clips(fake_whisper, str(path), [1, 2, 92, 93])
    assert spans == [(16000, 6000 / 32768), (16000, -9000 / 32768)]
    assert result["text"] == "한마디 한마디"
    assert [segment["start"] for segment in result["segments"]] == [1.0, 92.0]
    assert result["segments"][1]["words"] == [{"start": 92.1, "end": 92.8}]


def _write_pcm(wav_path: Path, values: tuple[int, ...], *, seconds: int) -> None:
    """값이 서로 다른 입력 채널을 생성한다."""
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(len(values))
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(struct.pack(f"<{len(values)}h", *values) * (16000 * seconds))


def _read_pcm_sample(wav_path: Path, *, channels: int, seconds: int) -> tuple[int, ...]:
    """채널 수와 길이를 확인하고 비교용 PCM 샘플을 읽는다."""
    with wave.open(str(wav_path), "rb") as wav:
        assert wav.getnchannels() == channels
        assert wav.getnframes() == 16000 * seconds
        return struct.unpack(f"<{channels}h", wav.readframes(1))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mic_channel", "mic_signal", "system_signal"),
    [(2, True, True), (0, True, False), (0, False, True)],
    ids=["overlapping-speakers", "only-microphone", "only-system"],
)
async def test_recording_flows_into_one_meeting_and_keeps_speaker_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mic_channel: int,
    mic_signal: bool,
    system_signal: bool,
) -> None:
    """기기와 모델만 대체하고 녹음 파일·큐·단계 재개·화자 결과를 실제로 검증한다."""
    config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        diarization=DiarizationConfig(engine="community-1", min_speakers=2),
        recording=RecordingConfig(
            split_track_processing=SplitTrackProcessingConfig(
                native_capture=False, speech_verification=False
            )
        ),
    )
    device = AudioDevice(index=2, name="Test Aggregate", is_aggregate=True)
    recorder = AudioRecorder(config)
    monkeypatch.setattr(recorder, "_select_audio_device", AsyncMock(return_value=device))
    monkeypatch.setattr(
        ZoomAudioActivityChecker, "aggregate_mic_channel", AsyncMock(return_value=mic_channel)
    )
    mic = 8000 if mic_signal else 0
    loopback = (2000, 6000) if system_signal else (0, 0)
    input_wav = tmp_path / "synthetic-device.wav"
    values = (mic, *loopback) if mic_channel == 0 else (*loopback, mic)
    _write_pcm(input_wav, values, seconds=34)
    create_process = asyncio.create_subprocess_exec
    capture_ready = tmp_path / "fixture-ready"

    async def record_from_fixture(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        """실제 FFmpeg 필터를 실행하고 유한 입력 처리 후 캡처처럼 q를 기다린다."""
        assert args[2:6] == ("-f", "avfoundation", "-i", ":2")
        return await create_process(
            sys.executable,
            "-c",
            "import pathlib, subprocess, sys; "
            "subprocess.run(sys.argv[2:], stdin=subprocess.DEVNULL, check=True); "
            "pathlib.Path(sys.argv[1]).touch(); sys.stdin.buffer.read(1)",
            str(capture_ready),
            *args[:2],
            "-i",
            str(input_wav),
            *args[6:],
            **kwargs,
        )

    with monkeypatch.context() as capture:
        capture.setattr(asyncio, "create_subprocess_exec", record_from_fixture)
        await recorder.start_recording(meeting_id="meeting_split_sources")
        assert recorder._process is not None
        async with asyncio.timeout(5):
            while not capture_ready.exists() or recorder.get_status()["audio_levels"] is None:
                await asyncio.sleep(0.01)
        assert recorder._process.returncode is None
        levels = recorder.get_status()["audio_levels"]
        assert levels["mic_dbfs"] == pytest.approx(-12.2 if mic_signal else -90.0, abs=0.2)
        assert levels["system_dbfs"] == pytest.approx(-18.3 if system_signal else -90.0, abs=0.2)
        recorder._start_time = time.time() - 34
        recording = await recorder.stop_recording()

    assert recording is not None
    assert list(config.paths.resolved_audio_input_dir.iterdir()) == [recording.file_path]
    assert has_separate_mic_track(recording.file_path)
    expected_system_sample = 4000 if system_signal else 0
    assert _read_pcm_sample(recording.file_path, channels=2, seconds=34) == (
        mic,
        expected_system_sample,
    )

    queue = AsyncJobQueue(JobQueue(db_path=tmp_path / "pipeline.db"))
    await queue.initialize()
    watcher = FolderWatcher(queue, config=config)
    monkeypatch.setattr(
        watcher, "_probe_writable_open", AsyncMock(return_value=_OpenWriterState.CLEAR)
    )
    await watcher.recover_unregistered_files()
    [job] = queue.queue.get_jobs_by_status(JobStatus.RECORDED)
    assert job.meeting_id == "meeting_split_sources"
    assert Path(job.audio_path) == recording.file_path

    stt_inputs: list[int] = []
    diarizer_inputs: list[int] = []

    async def transcribe(
        self: Transcriber, audio_path: Path, **kwargs: object
    ) -> TranscriptResult:
        (sample,) = _read_pcm_sample(audio_path, channels=1, seconds=34)
        stt_inputs.append(sample)
        assert sample in {8000, 4000}
        prefix = "마이크" if sample == 8000 else "상대방"
        return TranscriptResult(
            segments=[
                TranscriptSegment(text=f"{prefix} 첫 발언", start=2.0, end=4.0),
                TranscriptSegment(text=f"{prefix} 둘째 발언", start=6.0, end=7.0),
            ],
            full_text=prefix,
            language="ko",
            audio_path=str(audio_path),
            model=config.stt.model_name,
        )

    async def diarize(self: Diarizer, audio_path: Path, **kwargs: object) -> DiarizationResult:
        (sample,) = _read_pcm_sample(audio_path, channels=1, seconds=34)
        diarizer_inputs.append(sample)
        assert sample == 4000
        assert self._config.diarization.min_speakers == 1
        return DiarizationResult(
            segments=[DiarizationSegment(speaker="SPEAKER_00", start=2.0, end=4.0)],
            num_speakers=1,
            audio_path=str(audio_path),
        )

    monkeypatch.setattr(Transcriber, "transcribe", transcribe)
    monkeypatch.setattr(Diarizer, "diarize", diarize)
    pipeline = PipelineManager(config, model_manager=ModelLoadManager())
    monkeypatch.setattr(
        pipeline._resource_guard, "check_all", lambda: ResourceStatus(True, 20, True, 10)
    )

    partial = await pipeline.run(
        recording.file_path, meeting_id=job.meeting_id, stop_after=PipelineStep.TRANSCRIBE
    )
    assert partial.status == "paused"
    assert diarizer_inputs == []
    assert has_separate_mic_track(Path(partial.wav_path))
    checkpoints = config.paths.resolved_checkpoints_dir / job.meeting_id
    draft = TranscriptResult.from_checkpoint(checkpoints / "transcribe.json")
    assert draft.separate_mic_track
    assert {seg.speaker for seg in draft.segments} == (
        ({"나"} if mic_signal else set()) | ({None} if system_signal else set())
    )

    await pipeline.run(
        recording.file_path, meeting_id=job.meeting_id, stop_after=PipelineStep.MERGE
    )
    assert stt_inputs == ([8000] if mic_signal else []) + ([4000] if system_signal else [])
    assert diarizer_inputs == ([4000] if system_signal else [])
    assert config.diarization.min_speakers == 2
    merged = MergedResult.from_checkpoint(checkpoints / "merge.json")
    expected = {}
    if mic_signal:
        expected.update({"마이크 첫 발언": "나", "마이크 둘째 발언": "나"})
    if system_signal:
        expected.update({"상대방 첫 발언": "SPEAKER_00", "상대방 둘째 발언": "UNKNOWN"})
    assert {utterance.text: utterance.speaker for utterance in merged.utterances} == expected
    assert merged.num_speakers == int(mic_signal) + int(system_signal)
    assert not list(Path(partial.wav_path).parent.glob(".recap-tracks-*"))

    # 선택한 OpenAI와 다른 모델 비교 경로에 쓰이는 기본 변환은 기존 단일 오디오 계약이다.
    mono = AudioConverter(config).convert(recording.file_path, tmp_path / "mono")
    _read_pcm_sample(mono, channels=1, seconds=34)
    assert not has_separate_mic_track(mono)


def test_imported_stereo_stays_an_ordinary_meeting(tmp_path: Path) -> None:
    """일반 스테레오 파일에 임의로 마이크·상대방 의미를 붙이지 않는다."""
    source = tmp_path / "uploaded.wav"
    _write_pcm(source, (8000, 4000), seconds=2)
    converter = AudioConverter(AppConfig())
    converted = converter.convert(source, tmp_path / "output", preserve_recording_tracks=True)
    _read_pcm_sample(converted, channels=1, seconds=2)
    assert not has_separate_mic_track(converted)


def test_sparse_quiet_split_recording_is_admitted_without_lowering_length_gate(
    tmp_path: Path,
) -> None:
    """전체 평균이 낮아도 길이가 충분한 분리 WAV의 짧은 유효음을 보존한다."""
    import numpy as np
    import soundfile as sf

    def create(name: str, seconds: int, *, source_mark: bool) -> Path:
        path = tmp_path / name
        signal = np.zeros((16000 * seconds, 2), dtype=np.float32)
        signal[16000:28000, 0] = 0.008 * np.sin(2 * np.pi * 300 * np.arange(12000) / 16000)
        with sf.SoundFile(path, mode="w", samplerate=16000, channels=2, subtype="PCM_16") as audio:
            if source_mark:
                audio.comment = RECORDING_TRACK_COMMENT
            audio.write(signal)
        return path

    def status(path: Path) -> AudioQualityStatus:
        result = validate_audio_quality(
            path,
            min_mean_db=-40,
            min_duration_s=30,
            expected_identity=inspect_audio_path_no_symlinks(path),
        )
        return result.status

    assert status(create("long-split.wav", 80, source_mark=True)) is AudioQualityStatus.ACCEPT
    assert status(create("short-split.wav", 29, source_mark=True)) is AudioQualityStatus.REJECT
    assert (
        status(create("ordinary-stereo.wav", 80, source_mark=False)) is AudioQualityStatus.REJECT
    )


def test_sparse_mic_gets_normalized_with_original_timestamps_and_silence_skipped(
    tmp_path: Path,
) -> None:
    """뒤늦게 나온 작은 발화의 시간과 길이를 보존하고 앞선 무음을 전사하지 않는다."""
    import numpy as np
    import soundfile as sf

    path = tmp_path / "microphone.wav"
    signal = np.zeros(90 * 16000, dtype=np.float32)
    signal[80 * 16000 : 81 * 16000] = 0.003 * np.sin(2 * np.pi * 300 * np.arange(16000) / 16000)
    sf.write(path, signal, 16000)
    track = prepare_recording_track(
        path, SplitTrackProcessingConfig(speech_verification=False), -40
    )
    assert track is not None and track.path != path
    assert track.clip_timestamps == pytest.approx((79.55, 81.45), abs=0.025)
    normalized, rate = sf.read(track.path)
    assert rate == 16000 and len(normalized) == len(signal)
    assert not normalized[: 79 * rate].any()
    assert max(normalized) > 0.06


@pytest.mark.parametrize("noise_samples", [1, 160, 3200])
def test_loud_transient_does_not_discard_quiet_speech(
    tmp_path: Path, noise_samples: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """큰 순간 잡음 때문에 다른 시각의 작은 발화 전체를 버리거나 낮추지 않는다."""
    import soundfile as sf

    signal = np.zeros(32 * 16000, dtype=np.float32)
    voice = 0.003 * np.sin(2 * np.pi * 300 * np.arange(3 * 16000) / 16000)
    signal[10 * 16000 : 13 * 16000] = voice
    signal[7 * 16000 : 7 * 16000 + noise_samples] = 0.99
    path = tmp_path / "quiet-and-click.wav"
    sf.write(path, signal, 16000, subtype="PCM_16")
    original = path.read_bytes()
    monkeypatch.setattr(
        "core.recording_channels._detect_recording_speech", lambda *_args: [(9.5, 13.5)]
    )
    track = prepare_recording_track(path, SplitTrackProcessingConfig(), -40)
    assert track is not None
    samples, _ = sf.read(track.path)
    assert np.sqrt(np.mean(samples[10 * 16000 : 13 * 16000] ** 2)) > 0.04
    assert np.max(np.abs(samples)) <= 0.981
    assert track.clip_timestamps == (9.5, 13.5)
    assert path.read_bytes() == original


def test_speech_free_noise_is_not_sent_to_whisper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """에너지가 충분한 입력도 발화가 확인되지 않으면 전사하지 않는다."""
    import soundfile as sf

    noise = np.random.default_rng(77531).normal(0, 0.003, 32 * 16000)
    path = tmp_path / "fan.wav"
    sf.write(path, noise, 16000)
    seen = []

    def detect(path: Path, threshold: float) -> list[tuple[float, float]]:
        samples, _ = sf.read(path)
        seen.append(float(np.sqrt(np.mean(samples**2))))
        assert threshold == 0.20
        return []

    monkeypatch.setattr("core.recording_channels._detect_recording_speech", detect)
    assert prepare_recording_track(path, SplitTrackProcessingConfig(), -40) is None
    assert len(seen) == 1 and seen[0] > 0.04


def test_confirmed_very_quiet_speech_is_not_rejected_by_whole_track_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """발화가 확인되면 증폭 제한에 걸려도 평균 음량만으로 채널을 버리지 않는다."""
    import soundfile as sf

    path = tmp_path / "very-quiet.wav"
    sf.write(path, np.full(32 * 16000, 0.001, dtype=np.float32), 16000)
    monkeypatch.setattr(
        "core.recording_channels._detect_recording_speech", lambda *_args: [(4.0, 4.4)]
    )
    track = prepare_recording_track(path, SplitTrackProcessingConfig(max_gain_db=0), -40)
    assert track is not None and track.active_rms_dbfs < -40
    assert track.clip_timestamps == (4.0, 4.4)


def test_speech_detector_failure_is_not_silently_treated_as_empty_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """로컬 발화 검사가 고장 나면 입력을 무음으로 위장하지 않고 작업을 실패시킨다."""
    import soundfile as sf

    path = tmp_path / "voice.wav"
    sf.write(path, np.full(32 * 16000, 0.03, dtype=np.float32), 16000)

    def fail(*_args: object) -> list[tuple[float, float]]:
        raise RuntimeError("speech verification unavailable")

    monkeypatch.setattr("core.recording_channels._detect_recording_speech", fail)
    with pytest.raises(RuntimeError, match="speech verification unavailable"):
        prepare_recording_track(path, SplitTrackProcessingConfig(), -40)


def test_speech_detection_unions_overlapping_blocks_without_reordering_or_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """중첩 블록이 앞선 발화를 다시 찾더라도 전사 범위를 역전·중복하지 않는다."""
    from types import SimpleNamespace

    import soundfile as sf

    from core.recording_channels import _detect_recording_speech

    rate = 16000
    path = tmp_path / "block-boundaries.wav"
    samples = np.arange(rate * 50, dtype=np.float32) / (rate * 50) * 0.5
    sf.write(path, samples, rate, subtype="PCM_16")
    local_regions = [
        [(21.5, 22.8), (23.3, 24.0)],
        [(0.0, 0.2), (1.4, 2.5), (21.5, 24.0)],
        [(0.0, 0.2), (4.0, 5.0)],
    ]
    observed: list[tuple[int, float]] = []
    model = object()

    def timestamps(audio: Any, selected_model: object, **kwargs: Any) -> list[dict[str, int]]:
        assert selected_model is model
        assert kwargs["sampling_rate"] == rate
        assert kwargs["min_speech_duration_ms"] == 64
        assert kwargs["speech_pad_ms"] == 450
        ranges = local_regions[len(observed)]
        observed.append((len(audio), float(audio[0])))
        return [
            {"start": round(left * rate), "end": round(right * rate)} for left, right in ranges
        ]

    monkeypatch.setitem(
        sys.modules,
        "silero_vad",
        SimpleNamespace(load_silero_vad=lambda: model, get_speech_timestamps=timestamps),
    )
    regions = _detect_recording_speech(path, 0.2)

    assert [length for length, _ in observed] == [rate * 24, rate * 24, rate * 6]
    assert [first for _, first in observed] == pytest.approx([0.0, 0.22, 0.44], abs=1 / 32768)
    assert regions == [(21.5, 22.8), (23.3, 24.5), (43.5, 46.0), (48.0, 49.0)]
    assert all(
        previous[1] < following[0]
        for previous, following in zip(regions, regions[1:], strict=False)
    )


def test_very_quiet_solo_recording_reaches_speech_verification_with_legacy_gates_preserved(
    tmp_path: Path,
) -> None:
    """상대방이 조용해도 작은 본인 입력을 허용하고 일반·짧은·무음 파일은 거른다."""
    import soundfile as sf

    rate = 16000

    def status(name: str, seconds: int, *, marked: bool, audible: bool) -> AudioQualityStatus:
        samples = np.zeros((seconds * rate, 2), dtype=np.float32)
        if audible:
            samples[rate : 3 * rate, 0] = 0.001 * np.sin(
                2 * np.pi * 300 * np.arange(2 * rate) / rate
            )
        path = tmp_path / name
        with sf.SoundFile(path, "w", samplerate=rate, channels=2, subtype="PCM_16") as audio:
            if marked:
                audio.comment = RECORDING_TRACK_COMMENT
            audio.write(samples)
        return validate_audio_quality(
            path,
            min_mean_db=-40,
            min_duration_s=30,
            expected_identity=inspect_audio_path_no_symlinks(path),
        ).status

    assert status("quiet-solo.wav", 32, marked=True, audible=True) is AudioQualityStatus.ACCEPT
    assert status("uploaded.wav", 32, marked=False, audible=True) is AudioQualityStatus.REJECT
    assert status("short.wav", 29, marked=True, audible=True) is AudioQualityStatus.REJECT
    assert status("silent.wav", 32, marked=True, audible=False) is AudioQualityStatus.REJECT


@pytest.mark.asyncio
async def test_verified_quiet_track_reaches_transcriber_when_gain_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """발화가 확인된 작은 입력은 증폭이 꺼져 있어도 전체 평균 음량으로 거절하지 않는다."""
    import soundfile as sf

    from core.recording_channels import PreparedRecordingTrack

    config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path / "data")),
        recording=RecordingConfig(
            split_track_processing=SplitTrackProcessingConfig(max_gain_db=0)
        ),
    )
    path = tmp_path / "verified-quiet.wav"
    samples = np.zeros(32 * 16000, dtype=np.float32)
    samples[2 * 16000 : 4 * 16000] = 0.001 * np.sin(2 * np.pi * 300 * np.arange(2 * 16000) / 16000)
    sf.write(path, samples, 16000, subtype="PCM_16")
    track = PreparedRecordingTrack(path, (2.0, 4.0), -60.0)
    invoked = []

    async def transcribe(
        self: Transcriber, audio_path: Path, **kwargs: object
    ) -> TranscriptResult:
        assert self._config.audio_quality.min_mean_volume_db == -96.0
        assert self._config.audio_quality.min_duration_seconds == 30.0
        assert kwargs["vad_clip_timestamps"] == [2.0, 4.0]
        assert kwargs["prepared_recording_track"] is True
        invoked.append(audio_path)
        return TranscriptResult(
            segments=[TranscriptSegment(text="안내 메일은 제가 보내겠습니다.", start=2, end=4)],
            full_text="안내 메일은 제가 보내겠습니다.",
            language="ko",
            audio_path=str(audio_path),
            model=config.stt.model_name,
        )

    monkeypatch.setattr(Transcriber, "transcribe", transcribe)
    pipeline = PipelineManager(config, model_manager=ModelLoadManager())
    result = await pipeline._transcribe_wav(
        path,
        selected_provider="local",
        selected_model=config.stt.model_name,
        prepared_track=track,
    )

    assert invoked == [path]
    assert result.segments[0].text == "안내 메일은 제가 보내겠습니다."
    assert config.audio_quality.min_mean_volume_db == -40.0


@pytest.mark.asyncio
async def test_short_quiet_mic_signal_survives_recording_stop_with_original_pcm(
    tmp_path: Path,
) -> None:
    """100ms 평균이 낮은 짧은 마이크 입력도 녹음 정지에서 지우지 않는다."""
    import soundfile as sf

    from steps.recorder import RecordingState

    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    recorder = AudioRecorder(config)
    recorder._temp_dir.mkdir(parents=True, exist_ok=True)
    source = recorder._temp_dir / "quiet-short-utterance.wav"
    rate = 16000
    samples = np.zeros((32 * rate, 2), dtype=np.float32)
    # 20ms 프레임은 -67dBFS보다 크지만 100ms 경계를 걸치면 그보다 작아진다.
    start, count = round(0.98 * rate), round(0.08 * rate)
    samples[start : start + count, 0] = 0.0007 * np.sin(2 * np.pi * 300 * np.arange(count) / rate)
    with sf.SoundFile(source, "w", samplerate=rate, channels=2, subtype="PCM_16") as audio:
        audio.comment = RECORDING_TRACK_COMMENT
        audio.write(samples)
    original = source.read_bytes()
    assert recorder._check_audio_energy(source)

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.stdin.buffer.read(1)",
        stdin=asyncio.subprocess.PIPE,
    )
    recorder._state = RecordingState.RECORDING
    recorder._start_time = time.time() - 32
    recorder._meeting_id = "quiet-short-utterance"
    recorder._current_file = source
    recorder._current_device = AudioDevice(index=0, name="QA Aggregate", is_aggregate=True)
    recorder._process = proc
    completed = AsyncMock()
    recorder.on_recording_complete(completed)
    try:
        result = await recorder.stop_recording()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    assert result is not None
    assert result.file_path == tmp_path / "audio_input" / source.name
    assert result.file_path.read_bytes() == original
    assert has_separate_mic_track(result.file_path)
    completed.assert_awaited_once_with(result)
    assert recorder.state == RecordingState.IDLE
