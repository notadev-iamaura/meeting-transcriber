"""겹쳐 말한 마이크 성분과 재생 누설을 시간축에서 구별하는 회귀 검사."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from config import SplitTrackProcessingConfig
from core.echo_suppression import suppress_recording_echo
from core.recording_channels import (
    MIC_CHANNEL,
    RECORDING_TRACK_COMMENT,
    SYSTEM_CHANNEL,
    extract_recording_tracks,
)
from steps.transcriber import open_audio_path_no_symlinks

RATE = 16000
HISTORY = 4480


def _bandlimited(seed: int, size: int) -> np.ndarray:
    """서로 독립적인 광대역 테스트 신호를 만든다."""
    rng = np.random.default_rng(seed)
    return np.convolve(rng.normal(0, 0.05, size), [0.3, 0.6, 0.3], mode="same").astype(np.float32)


@pytest.mark.parametrize("voice_gain", [1.0, 0.10], ids=["same-level", "quiet-near-voice"])
def test_echo_cancellation_keeps_independent_near_voice_during_double_talk(
    voice_gain: float,
) -> None:
    """독립된 마이크 발화가 크거나 작아도 겹침 구간에서 실제 파형을 보존한다."""
    far = _bandlimited(23, RATE * 8)
    local = _bandlimited(901, len(far)) * voice_gain
    overlap = slice(3 * RATE, 5 * RATE)
    local[: overlap.start] = 0
    local[overlap.stop :] = 0
    reference = np.pad(far, (HISTORY, 0))
    delay = 1313
    echo = (
        reference[HISTORY - delay :][: len(far)] * 0.58
        + reference[HISTORY - delay - 390 :][: len(far)] * -0.20
    )
    cleaned, applied = suppress_recording_echo(local + echo, reference, RATE)
    assert applied
    expected = local[overlap]
    actual = cleaned[overlap]
    voice_gain_after = float(np.dot(actual, expected) / np.dot(expected, expected))
    error_in_overlap = np.linalg.norm(actual - expected) / np.linalg.norm(echo[overlap])
    assert voice_gain_after == pytest.approx(1.0, abs=0.02)
    assert error_in_overlap < 0.06


def test_no_echo_path_keeps_simultaneous_independent_inputs_bitwise_unchanged() -> None:
    """재생이 켜졌다는 이유로 내 목소리를 약화시키지 않는다."""
    far = _bandlimited(31, RATE * 5)
    mic = _bandlimited(32, len(far))
    result, applied = suppress_recording_echo(mic, np.pad(far, (HISTORY, 0)), RATE)
    assert not applied
    np.testing.assert_array_equal(result, mic)


def test_weak_shared_component_without_clear_replay_windows_keeps_original() -> None:
    """일부 상관이 있어도 독립 검증에서 재생 파형이 주성분으로 확인되어야 한다."""
    far = _bandlimited(73, RATE * 6)
    mic = _bandlimited(91, len(far)) + 0.45 * np.roll(far, 400)
    result, applied = suppress_recording_echo(mic, np.pad(far, (HISTORY, 0)), RATE)
    assert not applied
    np.testing.assert_array_equal(result, mic)


def test_ambiguous_periodic_reference_is_not_used_to_erase_the_microphone() -> None:
    """같은 패턴이 가능한 지연이 여럿이면 추정값을 적용하지 않는다."""
    signal = (0.12 * np.sin(np.arange(RATE * 5) * (2 * np.pi * 400 / RATE))).astype(np.float32)
    result, applied = suppress_recording_echo(signal, np.pad(signal, (HISTORY, 0)), RATE)
    assert not applied
    np.testing.assert_array_equal(result, signal)


@pytest.mark.parametrize("vary_volume", [False, True], ids=["fixed", "changing-volume"])
def test_filtered_room_echo_leaves_quiet_near_speech_intact_in_both_extraction_paths(
    tmp_path: Path, vary_volume: bool
) -> None:
    """스피커 음색·반사·동시 발화가 있는 분리 WAV를 첫 전사와 마이크 재시도로 읽는다."""
    far = _bandlimited(1, 12 * RATE)
    seconds = np.arange(len(far)) / RATE
    far *= 0.05 + 0.95 * np.abs(np.sin(seconds * 2 * np.pi * 1.3)) ** 1.5
    kernel = np.array([1, 2, 3, 4, 4, 3, 2, 1]) / 20
    room = np.zeros(2000)
    for start, gain in [(415, 0.54), (861, 0.25), (1527, -0.15)]:
        room[start : start + len(kernel)] = gain * kernel
    tail = np.arange(len(room) - 430)
    room[430:] += np.random.default_rng(774).normal(0, 0.004, len(tail)) * np.exp(-tail / 550)
    echo = np.convolve(far, room)[: len(far)]
    if vary_volume:
        echo *= 0.8 + 0.45 * np.cos(seconds * 0.39)
    near = _bandlimited(2, len(far)) * 0.12
    overlap = (seconds >= 4) & (seconds < 7)
    near[~overlap] = 0
    # 내 목소리에 독립적인 큰 소음까지 동시에 들어온다.
    noise = (seconds >= 5.5) & (seconds < 6)
    near[noise] += _bandlimited(3, int(noise.sum())) * 2
    source = tmp_path / "meeting.wav"
    with sf.SoundFile(source, "w", channels=2, samplerate=RATE, subtype="PCM_16") as output:
        output.comment = RECORDING_TRACK_COMMENT
        output.write(np.column_stack((near + echo, far)))
    original = source.read_bytes()
    raw, _ = sf.read(source, dtype="int16")
    with open_audio_path_no_symlinks(source) as (_, identity):
        cases = [("full", (MIC_CHANNEL, SYSTEM_CHANNEL)), ("retry", (MIC_CHANNEL,))]
        for name, channels in cases:
            work = tmp_path / name
            work.mkdir()
            paths = extract_recording_tracks(
                source, work, channels, identity, echo_settings=SplitTrackProcessingConfig()
            )
            if SYSTEM_CHANNEL in paths:
                played, _ = sf.read(paths[SYSTEM_CHANNEL], dtype="int16")
                np.testing.assert_array_equal(played, raw[:, SYSTEM_CHANNEL])
            cleaned, _ = sf.read(paths[MIC_CHANNEL], dtype="float32")
            far_only = ~overlap & (seconds > 0.1) & (seconds < 11.9)
            assert np.linalg.norm(cleaned[far_only]) < 0.04 * np.linalg.norm(echo[far_only])
            for segment in [overlap & ~noise, noise]:
                expected = near[segment]
                recovered = np.dot(cleaned[segment], expected) / np.dot(expected, expected)
                assert recovered == pytest.approx(1.0, abs=0.02)
                error = np.linalg.norm(cleaned[segment] - near[segment])
                # 잡음 우세 구간에는 차감을 보류할 수 있으므로 근거리 원 파형의 오차를 측정한다.
                assert error < 0.08 * np.linalg.norm(near[segment])
    assert source.read_bytes() == original
