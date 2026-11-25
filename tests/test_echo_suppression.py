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
    prepare_recording_track,
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


@pytest.mark.parametrize("overlap_gain", [0.0, 0.09], ids=["echo-only", "quiet-double-talk"])
def test_isolated_short_reply_loses_only_the_replayed_component(overlap_gain: float) -> None:
    """0.6초짜리 상대 발화 한 번도 시간차와 반사를 검증해 실제 마이크 성분만 남긴다."""
    far = np.zeros(RATE * 6, dtype=np.float32)
    start = int(2.36 * RATE)
    size = int(0.60 * RATE)
    far[start : start + size] = _bandlimited(117, size)
    reference = np.pad(far, (HISTORY, 0))
    echo = 0.47 * np.pad(far, (1280, 0))[: len(far)]
    echo -= 0.12 * np.pad(far, (1513, 0))[: len(far)]
    near = np.zeros_like(far)
    overlap = slice(start + 2500, start + 6300)
    near[overlap] = overlap_gain * _bandlimited(118, overlap.stop - overlap.start)
    near[RATE : RATE + size] = 0.3 * _bandlimited(119, size)
    cleaned, applied = suppress_recording_echo(near + echo, reference, RATE)
    assert applied
    assert np.linalg.norm(cleaned - near) < 0.04 * np.linalg.norm(echo)
    np.testing.assert_allclose(cleaned[RATE : RATE + size], near[RATE : RATE + size], atol=1e-9)
    if overlap_gain:
        gain = np.dot(cleaned[overlap], near[overlap]) / np.dot(near[overlap], near[overlap])
        assert gain == pytest.approx(1.0, abs=0.025)


def test_single_matching_piece_does_not_authorize_a_short_echo_filter() -> None:
    """단 한 조각만 재생과 같으면 뒤의 독립 마이크 파형은 그대로 보존한다."""
    size = RATE * 4
    far = np.zeros(size, dtype=np.float32)
    reply = slice(RATE, RATE + int(0.6 * RATE))
    far[reply] = _bandlimited(531, reply.stop - reply.start)
    mic = np.zeros_like(far)
    mic[reply] = _bandlimited(532, reply.stop - reply.start)
    shared = slice(RATE, RATE + RATE // 6)
    mic[shared] = 1.8 * far[shared]
    result, applied = suppress_recording_echo(mic, np.pad(far, (HISTORY, 0)), RATE)
    assert not applied
    np.testing.assert_array_equal(result, mic)


def test_silent_boundary_does_not_create_echo_in_an_unselected_frame() -> None:
    """누설이 멈춘 프레임을 건너뛰고 재생음이 다시 돌아올 때 무음을 보존한다."""
    frame = RATE // 50
    delay = RATE // 10
    predicted = np.zeros(RATE * 8, dtype=np.float32)
    training = slice(RATE + frame // 2, 5 * RATE + frame // 2)
    predicted[training] = _bandlimited(742, training.stop - training.start)
    silent = slice(6 * RATE, 6 * RATE + frame)
    predicted[silent] = 0.3 * _bandlimited(743, frame)
    resumed = slice(silent.stop + frame, silent.stop + 3 * frame)
    predicted[resumed] = _bandlimited(744, resumed.stop - resumed.start)
    mic = 0.62 * predicted
    mic[silent] = 0
    reference = np.pad(predicted[delay:], (HISTORY, delay))

    cleaned, applied = suppress_recording_echo(mic, reference, RATE)

    assert applied
    np.testing.assert_allclose(cleaned[silent], 0, atol=1e-9)
    assert np.linalg.norm(cleaned[resumed]) < 0.001 * np.linalg.norm(mic[resumed])


@pytest.mark.parametrize("rate", [8000, 16000])
def test_muted_microphone_stays_at_pcm_floor_while_playback_continues(
    tmp_path: Path, rate: int
) -> None:
    """재생이 이어져도 마이크 무음과 PCM 1단계 신호를 블록 경계 양쪽에서 보존한다."""
    size = 49 * rate + 13
    frame = rate // 50
    delay = round(rate * 0.086)
    far = _bandlimited(500 + rate, size)
    mic = 0.58 * np.pad(far, (delay, 0))[:size]
    mute_frames = (370, 1199, 1200, 1261, 2399, 2400, 2450)
    muted = np.zeros(size, dtype=bool)
    for index in mute_frames:
        muted[index * frame : (index + 1) * frame] = True
    mic[muted] = 0
    mic[1261 * frame : 1262 * frame] = -1 / 32768
    source = tmp_path / "microphone-mute.wav"
    with sf.SoundFile(source, "w", rate, 2, subtype="PCM_16") as recording:
        recording.comment = RECORDING_TRACK_COMMENT
        recording.write(np.column_stack((mic, far)))
    original = source.read_bytes()
    raw, _ = sf.read(source, dtype="int16")

    with open_audio_path_no_symlinks(source) as (_, identity):
        tracks = extract_recording_tracks(
            source,
            tmp_path,
            (MIC_CHANNEL, SYSTEM_CHANNEL),
            identity,
            echo_settings=SplitTrackProcessingConfig(),
        )

    cleaned, _ = sf.read(tracks[MIC_CHANNEL], dtype="int16")
    remote, _ = sf.read(tracks[SYSTEM_CHANNEL], dtype="int16")
    np.testing.assert_array_equal(cleaned[muted], raw[muted, MIC_CHANNEL])
    for index in mute_frames[:-1]:
        around = slice((index - 1) * frame, (index + 2) * frame)
        assert np.max(np.abs(cleaned[around])) <= 2
    np.testing.assert_array_equal(remote, raw[:, SYSTEM_CHANNEL])
    assert source.read_bytes() == original


@pytest.mark.parametrize("delay_ms", [55, 350])
def test_reflected_short_syllables_do_not_enter_microphone_stt(
    tmp_path: Path, delay_ms: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """단독 짧은 유성음이 반사되어도 정규화·음성 인식 단계로 마이크 사본을 넘기지 않는다."""
    size = round(0.60 * RATE)
    t = np.arange(size) / RATE
    phase = 2 * np.pi * np.cumsum(168 + 42 * np.sin(t * 6.2 + np.cos(t * 11))) / RATE
    voice = sum(np.sin(i * phase + 0.1 * i) / i for i in range(1, 37))
    # 음량이 거의 끊겼다가 다시 시작되는 짧은 음절을 포함한다.
    envelope = np.interp(
        t,
        [0, 0.02, 0.10, 0.14, 0.16, 0.18, 0.24, 0.29, 0.38, 0.46, 0.56, 0.60],
        [0, 1, 1, 0.58, 0.009, 0.01, 0.95, 0.65, 0.42, 0.55, 0.15, 0],
    )
    far = np.zeros(6 * RATE)
    start = round(2.05 * RATE)
    far[start : start + size] = voice * envelope * (0.65 / np.max(np.abs(voice * envelope)))
    coloration = np.array([1, 2, 3, 5, 3, 2, 1]) / 17
    delay = round(delay_ms * RATE / 1000)
    room = np.zeros(delay + 675 + len(coloration))
    for offset, gain in ((0, 0.62), (256, -0.19), (675, 0.09)):
        room[delay + offset : delay + offset + len(coloration)] = gain * coloration
    echo = np.convolve(far, room)[: len(far)]
    source = tmp_path / "short-speech.wav"
    with sf.SoundFile(source, "w", RATE, channels=2, subtype="PCM_16") as f:
        f.comment = RECORDING_TRACK_COMMENT
        f.write(np.column_stack((echo, far)))
    settings = SplitTrackProcessingConfig()
    with open_audio_path_no_symlinks(source) as (_, identity):
        tracks = extract_recording_tracks(
            source, tmp_path, (MIC_CHANNEL, SYSTEM_CHANNEL), identity, echo_settings=settings
        )
    # 음성 감지기가 두 트랙을 모두 음성으로 판정하더라도 원음에 근거해 에코만 걸러야 한다.
    monkeypatch.setattr(
        "core.recording_channels._detect_recording_speech", lambda *_: [(2.0, 3.5)]
    )
    residual, _ = sf.read(tracks[MIC_CHANNEL])
    assert np.linalg.norm(residual) < 0.003 * np.linalg.norm(echo)
    assert prepare_recording_track(tracks[MIC_CHANNEL], settings, -40) is None
    assert prepare_recording_track(tracks[SYSTEM_CHANNEL], settings, -40) is not None


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


@pytest.mark.parametrize(
    ("switch_time", "shorter_delay", "shift_seconds"),
    [
        (9.37, False, 0.0734),
        (23.87, False, 0.0734),
        (24.06, False, 0.0734),
        (22.15, True, 0.0734),
        (23.87, False, 0.350),
    ],
)
def test_quiet_double_talk_survives_a_render_delay_change(
    tmp_path: Path, switch_time: float, shorter_delay: bool, shift_seconds: float
) -> None:
    """재생 경로가 블록 안·경계 양쪽에서 바뀌어도 작은 발화와 출력 시간을 보존한다."""
    duration = 31 * RATE
    far = _bandlimited(772, duration)
    far *= (0.6 + 0.4 * np.sin(np.arange(duration) * 7 / RATE)).astype(np.float32)
    room = np.zeros(850)
    room[217:224] = np.array([1, 3, 4, 5, 4, 3, 1]) / 34
    room[812:816] = [0.07, 0.12, 0.11, 0.03]
    original_echo = np.convolve(far, room)[:duration]
    pivot = round(switch_time * RATE)
    shift = round(shift_seconds * RATE)
    delayed = np.pad(original_echo, (shift, 0))[:duration]
    before, after = (delayed, original_echo) if shorter_delay else (original_echo, delayed)
    echo = np.concatenate((before[:pivot], after[pivot:]))
    near = np.zeros(duration)
    overlap = slice(pivot - RATE, pivot + 3 * RATE)
    near[overlap] = 0.047 * _bandlimited(827, 4 * RATE)
    source = tmp_path / "switch.wav"
    with sf.SoundFile(source, "w", channels=2, samplerate=RATE, subtype="PCM_16") as f:
        f.comment = RECORDING_TRACK_COMMENT
        f.write(np.column_stack((echo + near, far)))
    original = source.read_bytes()
    with open_audio_path_no_symlinks(source) as (_, identity):
        tracks = extract_recording_tracks(
            source,
            tmp_path,
            (MIC_CHANNEL, SYSTEM_CHANNEL),
            identity,
            echo_settings=SplitTrackProcessingConfig(),
        )
    cleaned, _ = sf.read(tracks[MIC_CHANNEL])
    played, _ = sf.read(tracks[SYSTEM_CHANNEL], dtype="int16")
    raw, _ = sf.read(source, dtype="int16")
    np.testing.assert_array_equal(played, raw[:, SYSTEM_CHANNEL])
    assert source.read_bytes() == original
    # 경계 바로 바깥의 2초는 새 지연 추정이 적용됐음을 원 파형과 비교한다.
    steady = slice(pivot + RATE // 20, pivot + 2 * RATE)
    assert np.linalg.norm(cleaned[steady] - near[steady]) < 0.20 * np.linalg.norm(near[steady])
    gain = np.dot(cleaned[overlap], near[overlap]) / np.dot(near[overlap], near[overlap])
    assert gain == pytest.approx(1.0, abs=0.025)
    far_only = slice(RATE, min(5 * RATE, overlap.start))
    assert np.linalg.norm(cleaned[far_only]) < 0.002 * np.linalg.norm(echo[far_only])


def test_post_gain_vad_cannot_restore_below_floor_replay_between_two_quiet_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """정규화 후 VAD가 전 구간을 선택해도 마이크 잔향만 있는 시간은 다시 전사하지 않는다."""
    far = _bandlimited(398, 10 * RATE)
    echo = np.pad(far, (511, 0))[: len(far)] * 0.71
    mic = echo.copy()
    for start in (2, 5):
        mic[start * RATE : (start + 1) * RATE] += 0.07 * _bandlimited(start, RATE)
    source = tmp_path / "two-turns.wav"
    with sf.SoundFile(source, "w", channels=2, samplerate=RATE, subtype="PCM_16") as f:
        f.comment = RECORDING_TRACK_COMMENT
        f.write(np.column_stack((mic, far)))
    settings = SplitTrackProcessingConfig()
    with open_audio_path_no_symlinks(source) as (_, identity):
        tracks = extract_recording_tracks(
            source, tmp_path, (MIC_CHANNEL, SYSTEM_CHANNEL), identity, echo_settings=settings
        )
    monkeypatch.setattr(
        "core.recording_channels._detect_recording_speech", lambda *_: [(0.0, 10.0)]
    )
    local = prepare_recording_track(tracks[MIC_CHANNEL], settings, -40)
    remote = prepare_recording_track(tracks[SYSTEM_CHANNEL], settings, -40)
    assert local is not None and remote is not None
    assert local.clip_timestamps == pytest.approx((1.55, 3.45, 4.55, 6.45), abs=0.03)
    assert remote.clip_timestamps == (0.0, 10.0)


def test_reflected_speech_drops_below_colored_noise_after_silent_delay_change() -> None:
    """약한 배경 소음을 보존하면서 소음과 비슷하게 남던 상대 발화 잔향을 줄인다."""
    size = 24 * RATE
    t = np.arange(size) / RATE
    rng = np.random.default_rng(458)

    def voiced(seed: int, pitch: int) -> np.ndarray:
        """음높이·음절 에너지·숨소리가 변하는 광대역 유성음을 만든다."""
        phase = 2 * np.pi * np.cumsum(pitch + 16 * np.sin(t * 2.4) + 8 * np.sin(t * 5.12)) / RATE
        harmonics = sum(np.sin(phase * i + 0.1 * i) / i**0.9 for i in range(1, 23)) * 0.09
        breath = np.convolve(
            np.random.default_rng(seed).normal(0, 0.014, size),
            [0.15, 0.3, 0.45, 0.3, 0.15],
            "same",
        )
        syllables = 0.10 + 0.90 * (0.5 + 0.5 * np.sin(t * 14.4 + np.sin(t * 3))) ** 1.5
        return ((harmonics + breath) * syllables).astype(np.float32)

    far = voiced(31, 183)
    far[(t < 1) | ((t >= 7) & (t < 10)) | (t >= 23)] = 0
    near = 0.14 * voiced(8, 132)
    overlap = (t >= 10) & (t < 17.8)
    near[~overlap] = 0
    noise = np.convolve(rng.standard_normal(size), 0.76 ** np.arange(44), "same")
    noise *= np.linalg.norm(near[overlap]) / np.linalg.norm(noise[overlap]) * 10 ** (-14 / 20)
    reflected = np.zeros(size)
    for offset, gain in ((0, 0.49), (361, 0.14), (819, -0.09)):
        impulse = np.array([0.14, 0.36, 0.36, 0.14]) * gain
        reflected[offset:] += np.convolve(far, impulse)[: size - offset]
    switch = 9 * RATE
    echo = np.concatenate(
        (np.pad(reflected, (432, 0))[:switch], np.pad(reflected, (1904, 0))[switch:size])
    )
    cleaned, applied = suppress_recording_echo(
        (near + noise + echo).astype(np.float32),
        np.pad(far, (HISTORY, 0)),
        RATE,
    )
    assert applied
    error = cleaned - near - noise
    only_remote = (t >= 1) & (t < 7)
    assert np.linalg.norm(error[only_remote]) < 0.15 * np.linalg.norm(noise[only_remote])
    assert np.linalg.norm(error[overlap]) < 0.18 * np.linalg.norm(near[overlap])
