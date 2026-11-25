"""모델을 캐시한 Mac에서 스피커 발화 누락·무음 인사말·마이크 중복을 재현한다."""

from __future__ import annotations

import platform
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from config import AppConfig, PathsConfig
from core.model_manager import ModelLoadManager
from core.pipeline import PipelineManager
from core.recording_channels import RECORDING_TRACK_COMMENT

pytestmark = pytest.mark.native

_SCRIPT = (
    "다음 주 화요일에는 이용자 조사 계획을 정리해 보겠습니다. "
    "먼저 고객에게 필요한 화면과 보고서에 담길 내용을 같이 살펴보면 좋겠습니다. "
    "담당자가 검토한 뒤에 자료를 공유하겠습니다."
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "playback_times"),
    [(_SCRIPT, (1.0, 17.0)), ("맞아요.", (10.05,))],
    ids=["repeated-across-whisper-window", "isolated-short-room-echo"],
)
async def test_only_the_remote_track_transcribes_loudspeaker_speech(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str, playback_times: tuple[float, ...]
) -> None:
    """반복 재생과 고립된 한마디의 반사음이 마이크로 들어와도 상대 트랙에만 전사한다."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        pytest.skip("Apple Silicon Mac용 로컬 음성 검증")
    from huggingface_hub import try_to_load_from_cache

    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path.resolve())))
    weights = try_to_load_from_cache(config.stt.model_name, "weights.safetensors")
    if not isinstance(weights, str) or not Path(weights).is_file():
        pytest.skip("로컬 Whisper 모델 캐시가 필요합니다.")
    voices = subprocess.check_output(["say", "-v", "?"], text=True)
    if not re.search(r"^Yuna\s+ko_KR\b", voices, re.MULTILINE):
        pytest.skip("설치된 한국어 Yuna 음성이 필요합니다.")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    from core.preflight import run_preflight

    if not run_preflight().can_use_mlx:
        pytest.skip("로컬 MLX 추론을 실행할 수 없습니다.")

    spoken = tmp_path / "remote.aiff"
    subprocess.run(["say", "-v", "Yuna", "-r", "175", "-o", str(spoken), script], check=True)
    pcm = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(spoken),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-f",
            "s16le",
            "-",
        ]
    )
    samples = np.frombuffer(pcm, dtype="<i2")
    rate = 16000
    remote = np.zeros(34 * rate, dtype=np.int16)
    for second in playback_times:
        start = round(second * rate)
        remote[start : start + len(samples)] = samples
    if len(playback_times) == 1:
        # 350ms 뒤 스피커 음색이 변한 직접음과 두 번의 반사가 같은 마이크에 들어온다.
        rendered = np.convolve(remote, np.array([1, 2, 3, 5, 3, 2, 1]) / 17)[: len(remote)]
        echo = np.zeros_like(rendered)
        for extra, gain in ((0, 0.62), (256, -0.19), (675, 0.09)):
            delay = round(0.350 * rate) + extra
            echo[delay:] += gain * rendered[:-delay]
        microphone = np.rint(echo).astype(np.int16)
    else:
        microphone = np.zeros_like(remote)
        microphone[640:] = remote[:-640] // 2  # 상대 소리만 40ms 늦게 마이크로 돌아온다.
    config.paths.resolved_audio_input_dir.mkdir()
    source = config.paths.resolved_audio_input_dir / "synthetic.wav"
    with sf.SoundFile(source, "w", rate, 2, subtype="PCM_16") as recording:
        recording.comment = RECORDING_TRACK_COMMENT
        recording.write(np.column_stack((microphone, remote)))

    expected = re.sub(r"[\s.]", "", script)
    pipeline = PipelineManager(config, ModelLoadManager())
    for attempt in range(2):
        checkpoint = config.paths.resolved_checkpoints_dir / f"run-{attempt}" / "transcribe.json"
        checkpoint.parent.mkdir(parents=True)
        result = await pipeline._run_step_transcribe(
            source, checkpoint, stt_provider="local", stt_model=str(Path(weights).parent)
        )
        assert result.separate_mic_track and all(s.speaker is None for s in result.segments)
        intervals = ((0, 34),) if len(playback_times) == 1 else ((0, 16), (16, 34))
        for left, right in intervals:
            actual = "".join(s.text for s in result.segments if left <= s.start < right)
            assert re.sub(r"[\s.,!?]", "", actual) == expected
