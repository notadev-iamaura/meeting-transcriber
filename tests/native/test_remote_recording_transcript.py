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
async def test_remote_speech_repeated_across_whisper_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 세 문장을 두 번 재생하면 두 전사 모두 여섯 문장을 상대 발화로 보존한다."""
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
    subprocess.run(["say", "-v", "Yuna", "-r", "175", "-o", str(spoken), _SCRIPT], check=True)
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
    for second in (1, 17):
        remote[second * rate : second * rate + len(samples)] = samples
    microphone = np.zeros_like(remote)
    microphone[640:] = remote[:-640] // 2  # 상대 소리만 40ms 늦게 마이크로 돌아온다.
    config.paths.resolved_audio_input_dir.mkdir()
    source = config.paths.resolved_audio_input_dir / "synthetic.wav"
    with sf.SoundFile(source, "w", rate, 2, subtype="PCM_16") as recording:
        recording.comment = RECORDING_TRACK_COMMENT
        recording.write(np.column_stack((microphone, remote)))

    expected = re.sub(r"[\s.]", "", _SCRIPT)
    pipeline = PipelineManager(config, ModelLoadManager())
    for attempt in range(2):
        checkpoint = config.paths.resolved_checkpoints_dir / f"run-{attempt}" / "transcribe.json"
        checkpoint.parent.mkdir(parents=True)
        result = await pipeline._run_step_transcribe(
            source, checkpoint, stt_provider="local", stt_model=str(Path(weights).parent)
        )
        assert result.separate_mic_track and all(s.speaker is None for s in result.segments)
        for left, right in ((0, 16), (16, 34)):
            actual = "".join(s.text for s in result.segments if left <= s.start < right)
            assert re.sub(r"[\s.,!?]", "", actual) == expected
