"""같은 시간축의 재생 신호로 마이크에 돌아온 소리를 추정한다."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

_MIN_CORRELATION = 0.73
_MIN_WINDOWS = 4  # 경로 추정과 독립 검증에 각각 두 구간 이상 필요


@dataclass(frozen=True)
class _Window:
    """지연과 극성이 명확한 재생 누설 후보."""

    start: int
    delay: int
    sign: float


def _fft_size(size: int) -> int:
    return 1 << (size - 1).bit_length()


def _match_windows(
    mic: NDArray[np.float32], reference: NDArray[np.float32], rate: int, width: int
) -> list[_Window]:
    """서로 다른 시간의 일관된 지연 후보를 찾는다. 단일 톤의 모호한 상관은 제외한다."""
    history = len(reference) - len(mic)
    size = _fft_size(2 * width + history)
    exclusion = max(1, rate * 4 // 1000)
    windows = []
    for start in range(0, len(mic) - width + 1, width):
        # 차분으로 DC와 저주파 단조 신호가 후보를 압도하지 않게 한다.
        captured = np.diff(mic[start : start + width].astype(np.float64))
        played = np.diff(reference[start : start + width + history].astype(np.float64))
        power = float(np.dot(captured, captured))
        if power < len(captured) * 1e-9:
            continue
        prefix = np.concatenate(([0.0], np.cumsum(played * played)))
        played_power = prefix[len(captured) :] - prefix[: history + 1]
        correlation = np.fft.irfft(
            np.fft.rfft(played, n=size) * np.fft.rfft(captured[::-1], n=size), n=size
        )[len(captured) - 1 : len(captured) + history]
        normalized = correlation / np.sqrt(np.maximum(played_power * power, 1e-15))
        absolute = np.abs(normalized)
        peak = int(np.argmax(absolute))
        alternate = max(
            float(np.max(absolute[: max(0, peak - exclusion)], initial=0.0)),
            float(np.max(absolute[peak + exclusion + 1 :], initial=0.0)),
        )
        if absolute[peak] >= _MIN_CORRELATION and absolute[peak] - alternate >= 0.09:
            windows.append(_Window(start, history - peak, float(np.sign(normalized[peak]))))
    if len(windows) < _MIN_WINDOWS:
        return []
    tolerance = max(1, rate // 500)
    return max(
        (
            [
                window
                for window in windows
                if window.sign == anchor.sign and abs(window.delay - anchor.delay) <= tolerance
            ]
            for anchor in windows
        ),
        key=len,
    )


def _predict(
    reference: NDArray[np.float32],
    taps: NDArray[np.float64],
    delay: int,
    history: int,
    samples: int,
) -> NDArray[np.float64]:
    """재생 신호에 검증할 지연과 필터를 적용해 마이크 시간축의 누설을 계산한다."""
    start = history - delay
    if len(taps) == 1:
        return reference[start : start + samples].astype(np.float64) * float(taps[0])
    data = reference[: start + samples]
    size = _fft_size(len(data) + len(taps) - 1)
    convolution = np.fft.irfft(np.fft.rfft(data, n=size) * np.fft.rfft(taps, n=size), n=size)
    return convolution[start : start + samples]


def _fit_sparse_path(
    reference: NDArray[np.float32],
    captured: NDArray[np.float64],
    train: list[_Window],
    width: int,
    history: int,
    direct_gain: float,
) -> NDArray[np.float64]:
    """뚜렷한 직접음과 소수 반사 지연을 고르고 같은 훈련 구간에서 계수를 다시 맞춘다."""
    played = np.stack(
        [reference[span.start : span.start + width + history] for span in train]
    ).astype(np.float64)
    size = _fft_size(2 * width + history)
    spectrum = np.fft.rfft(played, n=size, axis=1)
    prefix = np.pad(np.cumsum(played**2, axis=1), ((0, 0), (1, 0)))
    power = np.sum(prefix[:, width:] - prefix[:, : history + 1], axis=0)
    norm = np.sqrt(np.maximum(power, float(power.max()) * 0.03) + 1e-12)
    positions: list[int] = []
    columns: list[NDArray[np.float64]] = []
    coefficients = np.empty(0, dtype=np.float64)
    target = captured.ravel()
    residual = captured
    for _ in range(10):
        cross = np.fft.irfft(
            np.sum(spectrum * np.fft.rfft(residual[:, ::-1], n=size, axis=1), axis=0),
            n=size,
        )[width - 1 : width + history]
        cross[positions] = 0
        position = int(np.argmax(np.abs(cross) / norm))
        gain = abs(float(cross[position])) / max(float(power[position]), 1e-12)
        if gain < max(0.003, abs(direct_gain) * 0.007):
            break
        positions.append(position)
        columns.append(played[:, position : position + width].ravel())
        design = np.stack(columns, axis=1)
        gram = design.T @ design
        # 매우 비슷한 두 지연을 학습해 마이크 신호까지 설명하려는 해를 억제한다.
        ridge = 1e-5 * max(float(np.trace(gram)) / len(positions), 1e-9)
        coefficients = np.linalg.solve(gram + np.eye(len(positions)) * ridge, design.T @ target)
        residual = (target - design @ coefficients).reshape(captured.shape)
    taps = np.zeros(history + 1, dtype=np.float64)
    if positions and np.sum(np.abs(coefficients)) <= 3.2:
        taps[history - np.array(positions)] = coefficients
    return taps


def suppress_recording_echo(
    mic: NDArray[np.float32], reference: NDArray[np.float32], rate: int
) -> tuple[NDArray[np.float32], bool]:
    """검증된 재생 성분만 빼고, 불확실하면 마이크 샘플을 그대로 반환한다.

    reference 앞부분에는 지연 검색에 필요한 이전 샘플이 있다. 필터는 누설이
    지배적인 후보의 일부에서만 추정하고, 별도 후보에서 검증한 뒤 고정한다.
    재생·마이크가 함께 커지는 순간에 적응하거나 마이크 전체를 감쇠하지 않는다.
    """
    history = len(reference) - len(mic)
    width = rate // 2
    windows = _match_windows(mic, reference, rate, width)
    if len(windows) < _MIN_WINDOWS:
        return mic, False
    train, holdout = windows[::2], windows[1::2]
    delay = int(np.median([window.delay for window in train]))
    offset = history - delay
    played = np.stack(
        [reference[w.start + offset : w.start + offset + width] for w in train]
    ).astype(np.float64)
    captured = np.stack([mic[w.start : w.start + width] for w in train]).astype(np.float64)
    gain = float(np.sum(played * captured) / max(1e-12, np.sum(played**2)))
    if not 0.01 <= abs(gain) <= 2.5 or np.sign(gain) != train[0].sign:
        return mic, False

    # 직접 성분 외에 방의 짧은 반사도 후보에 넣되, 검증 구간에서 더 나은 경우만 쓴다.
    window = np.hanning(width)
    size = _fft_size(width)
    x = np.fft.rfft(played * window, n=size)
    y = np.fft.rfft(captured * window, n=size)
    power = np.sum(np.abs(x) ** 2, axis=0)
    transfer = np.sum(np.conj(x) * y, axis=0) / (power + max(1e-12, float(power.mean()) * 0.002))
    response = np.fft.irfft(transfer, n=size)
    earlier = min(delay, rate // 125)
    later = min(history - delay, rate // 10)
    reflected = np.concatenate((response[size - earlier :], response[: later + 1]))
    sparse = _fit_sparse_path(reference, captured, train, width, history, gain)
    candidates = [(np.array([gain]), delay), (reflected, delay - earlier), (sparse, 0)]
    prediction = None
    best_error = 0.52
    for taps, start_delay in candidates:
        estimate = _predict(reference, taps, start_delay, history, len(mic))
        errors = []
        for span in holdout:
            raw = mic[span.start : span.start + width]
            residual = raw - estimate[span.start : span.start + width]
            errors.append(float(np.dot(residual, residual) / max(1e-12, np.dot(raw, raw))))
        error = float(np.mean(errors))
        if error < best_error and max(errors) <= 0.80:
            prediction, best_error = estimate, error
    if prediction is None:
        return mic, False

    # 지연·출력 변경 등으로 빼는 쪽이 신호를 키우는 구간에서는 원본으로 돌아간다.
    # 비교는 적용 여부에만 사용하며, 겹말에서 필터를 다시 학습하지 않는다.
    frame = max(1, rate // 8)
    boundaries = np.arange(0, len(mic), frame)
    compatible = 2 * np.add.reduceat(mic * prediction, boundaries) >= np.add.reduceat(
        prediction * prediction, boundaries
    )
    samples = np.arange(len(mic))
    envelope = np.interp(samples, boundaries + frame / 2, compatible.astype(np.float64))
    fade = max(1, rate // 40)
    envelope *= np.minimum(1, np.minimum(samples, len(mic) - 1 - samples) / fade)
    return (mic - prediction * envelope).astype(np.float32), True
