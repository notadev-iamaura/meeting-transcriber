"""겹쳐 말한 마이크 성분과 재생 누설을 시간축에서 구별하는 회귀 검사."""

from __future__ import annotations

import numpy as np
import pytest

from core.echo_suppression import suppress_recording_echo

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


def test_ambiguous_periodic_reference_is_not_used_to_erase_the_microphone() -> None:
    """같은 패턴이 가능한 지연이 여럿이면 추정값을 적용하지 않는다."""
    signal = (0.12 * np.sin(np.arange(RATE * 5) * (2 * np.pi * 400 / RATE))).astype(np.float32)
    result, applied = suppress_recording_echo(signal, np.pad(signal, (HISTORY, 0)), RATE)
    assert not applied
    np.testing.assert_array_equal(result, signal)
