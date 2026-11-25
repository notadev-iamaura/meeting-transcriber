"""같은 시간축의 재생 신호로 마이크에 돌아온 소리를 추정한다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

# 지연 후보는 주파수 응답이 달라진 스피커 신호까지 찾는다. 실제 차감은 뒤의
# 독립 검증에서 필터가 마이크 파형을 재현했을 때만 허용한다.
_MIN_CORRELATION = 0.30
_MIN_WINDOWS = 4  # 경로 추정과 독립 검증에 각각 두 구간 이상 필요
_MIN_EXPLAINED_POWER = 0.97  # 파형에 독립적인 음성이 섞이면 경로와 이득을 고정한다.


@dataclass(frozen=True)
class _Window:
    """지연과 극성이 명확한 재생 누설 후보."""

    start: int
    delay: int
    sign: float


@dataclass(frozen=True)
class EchoPath:
    """독립 구간에서 검증된 재생 전달 필터와 그때의 기준 지연."""

    taps: NDArray[np.float64]
    start_delay: int
    anchor_delay: int


def _fft_size(size: int) -> int:
    return 1 << (size - 1).bit_length()


def _match_windows(
    mic: NDArray[np.float32],
    reference: NDArray[np.float32],
    rate: int,
    width: int,
    starts: list[int] | None = None,
) -> list[_Window]:
    """서로 다른 시간의 일관된 지연 후보를 찾는다. 단일 톤의 모호한 상관은 제외한다."""
    history = len(reference) - len(mic)
    size = _fft_size(2 * width + history)
    exclusion = max(1, rate * 4 // 1000)
    windows = []
    for start in starts if starts is not None else range(0, len(mic) - width + 1, width):
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
    return windows


def _delay_groups(windows: list[_Window], rate: int) -> list[list[_Window]]:
    """재생 중 경로가 바뀐 시점의 지연 후보도 별도로 검증한다."""
    tolerance = max(1, rate // 500)
    groups: list[list[_Window]] = []
    while windows and len(groups) < 3:
        group = max(
            (
                [
                    w
                    for w in windows
                    if w.sign == anchor.sign and abs(w.delay - anchor.delay) <= tolerance
                ]
                for anchor in windows
            ),
            key=len,
        )
        groups.append(group)
        selected = set(group)
        windows = [w for w in windows if w not in selected]
    return groups


def _short_windows(
    mic: NDArray[np.float32],
    reference: NDArray[np.float32],
    rate: int,
    anchors: list[_Window],
    width: int,
) -> list[_Window]:
    """반 초 창에서 찾은 지연을 같은 위치 주변의 독립된 짧은 창에서 다시 확인한다."""
    starts: set[int] = set()
    for anchor in anchors:
        # 창 경계에 걸친 음절을 포함한다. 겹치는 근거를 두 번 세지 않도록 같은 격자를 쓴다.
        first = max(0, anchor.start // width - 1) * width
        stop = min(len(mic) - width + 1, anchor.start + rate // 2 + width)
        starts.update(range(first, stop, width))
    candidates = _match_windows(mic, reference, rate, width, sorted(starts))
    anchor = anchors[0]
    return [
        w
        for w in candidates
        if w.sign == anchor.sign and abs(w.delay - anchor.delay) <= max(1, rate // 500)
    ]


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


def _fit_sparse_paths(
    reference: NDArray[np.float32],
    captured: NDArray[np.float64],
    train: list[_Window],
    width: int,
    history: int,
    direct_gain: float,
    rate: int,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
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
    coefficients: NDArray[np.float64] = np.empty(0, dtype=np.float64)
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
        coefficients = cast(
            NDArray[np.float64],
            np.linalg.solve(gram + np.eye(len(positions)) * ridge, design.T @ target),
        )
        residual = (target - design @ coefficients).reshape(captured.shape)
    taps = np.zeros(history + 1, dtype=np.float64)
    grouped = np.zeros_like(taps)
    if positions and np.sum(np.abs(coefficients)) <= 3.2:
        taps[history - np.array(positions)] = coefficients
        # 스피커 응답 한 갈래가 여러 샘플에 걸치면 한 점만 추정한 필터에 말소리 잔향이
        # 남는다. 뚜렷한 갈래의 이웃만 같이 맞춰 독립 잡음·겹말을 학습하는 자유도를 줄인다.
        grouped = _fit_reflection_clusters(played, captured, residual, positions, rate)
    return taps, grouped


def _fit_reflection_clusters(
    played: NDArray[np.float64],
    captured: NDArray[np.float64],
    residual: NDArray[np.float64],
    positions: list[int],
    rate: int,
) -> NDArray[np.float64]:
    """훈련 잔차가 낮은 창에서 인접 반사 계수를 함께 추정한다."""
    width = captured.shape[1]
    history = played.shape[1] - width
    # 각 경로의 스피커 착색은 같은 시간 폭을 가진다. 짧은 발화라도 인접 탭을 줄이지 않는다.
    radius = max(1, rate // 4000)
    offsets = np.unique(
        np.clip(np.array(positions)[:, None] + np.arange(-radius, radius + 1), 0, history)
    )
    errors = np.sum(residual**2, axis=1) / np.maximum(np.sum(captured**2, axis=1), 1e-12)
    order = np.argsort(errors)
    count = max(2, np.count_nonzero(errors <= max(0.02, 4 * errors[order[0]])))
    gram = np.zeros((len(offsets), len(offsets)))
    rhs = np.zeros(len(offsets))
    for i in order[:count]:
        design = np.stack([played[i, p : p + width] for p in offsets], axis=1)
        gram += design.T @ design
        rhs += design.T @ captured[i]
    ridge = 1e-5 * max(float(np.trace(gram)) / len(offsets), 1e-9)
    solution = np.linalg.solve(gram + np.eye(len(offsets)) * ridge, rhs)
    taps = np.zeros(history + 1, dtype=np.float64)
    if np.sum(np.abs(solution)) <= 3.2:
        taps[history - offsets] = solution
    return taps


def _track_replay_gain(
    mic: NDArray[np.float32],
    prediction: NDArray[np.float64],
    rate: int,
    training: list[_Window] | None = None,
) -> NDArray[np.float64]:
    """예측 신호가 마이크 에너지 대부분을 설명하는 구간을 바탕으로 출력 음량을 추적한다."""
    frame = max(1, rate // 8)
    starts = np.arange(0, len(mic), frame)
    energy = np.add.reduceat(prediction**2, starts)
    captured = np.add.reduceat(mic.astype(np.float64) ** 2, starts)
    cross = np.add.reduceat(mic * prediction, starts)
    gain = cross / np.maximum(1e-15, energy)
    # 예측한 성분에만 계수를 적용하며 독립된 겹말·잡음에서 계수를 다시 맞추지 않는다.
    valid = (
        (cross**2 >= _MIN_EXPLAINED_POWER * captured * energy)
        & (energy > frame * 1e-9)
        & (gain >= 0.25)
        & (gain <= 4.0)
    )
    if training is not None:
        # 검증 파형은 이득 보간의 기준점으로도 쓰지 않는다.
        valid &= np.logical_or.reduce(
            [(starts >= w.start) & (starts + frame <= w.start + rate // 2) for w in training]
        )
    if not np.any(valid):
        return prediction
    return prediction * np.interp(np.arange(len(mic)), starts[valid] + frame / 2, gain[valid])


def _validated_path(
    mic: NDArray[np.float32],
    reference: NDArray[np.float32],
    rate: int,
    windows: list[_Window],
    previous: list[EchoPath],
    width: int,
) -> EchoPath | None:
    """같은 지연의 후보 중 일부에서 추정하고 나머지에서 전달 경로를 검증한다."""
    history = len(reference) - len(mic)
    train, holdout = windows[::2], windows[1::2]
    delay = int(np.median([window.delay for window in train]))
    offset = history - delay
    played = np.stack(
        [reference[w.start + offset : w.start + offset + width] for w in train]
    ).astype(np.float64)
    captured = np.stack([mic[w.start : w.start + width] for w in train]).astype(np.float64)
    gain = float(np.sum(played * captured) / max(1e-12, np.sum(played**2)))
    if not 0.01 <= abs(gain) <= 2.5:
        return None

    # 직접 성분 외에 방의 짧은 반사도 후보에 넣되, 검증 구간에서 더 나은 경우만 쓴다.
    window = np.hanning(width)
    size = _fft_size(width)
    x = np.fft.rfft(played * window, n=size)
    y = np.fft.rfft(captured * window, n=size)
    power = np.sum(np.abs(x) ** 2, axis=0)
    transfer = np.sum(np.conj(x) * y, axis=0) / (power + max(1e-12, float(power.mean()) * 0.002))
    # 가장 뚜렷한 상관점이 늦은 반사음일 수 있어 그 앞의 직접음도 함께 추정한다.
    earlier = min(delay, rate // 10, width // 4)
    later = min(history - delay, rate // 5, width // 2)

    def room_taps(spectrum: NDArray[np.complex128]) -> NDArray[np.float64]:
        """방의 짧은 선행·후행 반사만 전사용 필터에 포함한다."""
        response = np.fft.irfft(spectrum, n=size)
        return np.concatenate((response[size - earlier :], response[: later + 1]))

    reflected = room_taps(transfer)
    sparse, grouped = _fit_sparse_paths(reference, captured, train, width, history, gain, rate)
    candidates = [
        (np.array([gain]), delay),
        (reflected, delay - earlier),
        (sparse, 0),
        (grouped, 0),
    ]

    # 초기 필터가 설명하는 훈련 구간만 사용해 창 경계에서 어긋난 반사음을 보완한다.
    # 겹말을 포함한 구간과 검증 구간은 추가 학습에서 제외한다.
    for _ in range(8):
        coarse = _predict(reference, reflected, delay - earlier, history, len(mic))
        fitted = np.stack([coarse[w.start : w.start + width] for w in train])
        cross = np.sum(captured * fitted, axis=1)
        fit_power = np.sum(fitted**2, axis=1)
        ratio = cross / np.maximum(1e-12, fit_power)
        trusted = (
            (cross**2 >= _MIN_EXPLAINED_POWER * np.sum(captured**2, axis=1) * fit_power)
            & (ratio >= 0.25)
            & (ratio <= 4.0)
        )
        if np.count_nonzero(trusted) < 2:
            break
        # 잔차가 작은 훈련 구간을 우선해 아주 작은 겹말도 필터에 섞이지 않게 한다.
        variance = np.sum((captured - ratio[:, None] * fitted) ** 2, axis=1) / np.maximum(
            fit_power, 1e-12
        )
        limit = np.median(variance[trusted])
        if np.count_nonzero(trusted & (variance <= limit)) >= 2:
            trusted &= variance <= limit
        residual = captured[trusted] / ratio[trusted, None] - fitted[trusted]
        residual_spectrum = np.fft.rfft(residual * window, n=size)
        basis = x[trusted]
        power = np.sum(np.abs(basis) ** 2, axis=0)
        reflected = reflected + room_taps(
            np.sum(np.conj(basis) * residual_spectrum, axis=0)
            / (power + max(1e-12, power.mean() * 0.002))
        )
    candidates.append((reflected, delay - earlier))
    # 동일한 방에서 기기 지연만 바뀐 경우, 이전 필터의 시간 위치를 다시 맞춰 검증한다.
    for path in previous:
        shift = path.start_delay + delay - path.anchor_delay
        # 짧아진 경로에서 0초보다 앞에 놓인 필터 여백은 물리적으로 도착할 수 없다.
        taps = path.taps[max(0, -shift) :]
        shift = max(0, shift)
        if len(taps) and shift <= history:
            estimate = _predict(reference, taps, shift, history, len(mic))
            candidates.append(
                (taps, _align_replayed_path(estimate, captured, train, shift, history, rate))
            )
    best_path = None
    best_error = float("inf")
    short = width < rate // 2
    for taps, start_delay in candidates:
        estimate = _predict(reference, taps, start_delay, history, len(mic))
        # 짧은 후보는 고정 이득으로만 검증한다. 125ms 이득 추정 창은 100ms 훈련 창을 넘어간다.
        validation = estimate if short else _track_replay_gain(mic, estimate, rate, training=train)
        errors = []
        for span in holdout:
            raw = mic[span.start : span.start + width]
            residual = raw - validation[span.start : span.start + width]
            errors.append(float(np.dot(residual, residual) / max(1e-12, np.dot(raw, raw))))
        # 겹말 한 구간에 우연히 더 맞는 복잡한 필터가 단순한 직접음을 밀어내지 않게 한다.
        error = float(np.median(errors))
        # 짧은 근거로 추정할 때는 모든 검증 창에서 재생이 마이크 에너지의 대부분을 설명해야 한다.
        if (
            error < best_error * 0.98
            and np.mean(errors) < (0.06 if short else 0.52)
            and max(errors) <= (0.12 if short else 0.80)
        ):
            best_path, best_error = EchoPath(taps, start_delay, delay), error
    return best_path


def _align_replayed_path(
    estimate: NDArray[np.float64],
    captured: NDArray[np.float64],
    train: list[_Window],
    shift: int,
    history: int,
    rate: int,
) -> int:
    """상관 최대점의 미세한 흔들림을 훈련 구간의 차분 파형으로 샘플 단위 보정한다."""
    margin = max(1, rate // 4000)
    width = captured.shape[1]
    target = np.diff(captured[:, margin:-margin], axis=1)
    best_shift = shift
    best_score = 0.0
    for delta in range(max(-margin, -shift), min(margin, history - shift) + 1):
        probe = np.stack(
            [estimate[w.start + margin - delta : w.start + width - margin - delta] for w in train]
        )
        gradient = np.diff(probe, axis=1)
        score = np.sum(target * gradient) / np.sqrt(max(1e-15, float(np.sum(gradient**2))))
        if score > best_score:
            best_score = score
            best_shift = shift + delta
    return best_shift


def suppress_recording_echo(
    mic: NDArray[np.float32],
    reference: NDArray[np.float32],
    rate: int,
    previous: list[EchoPath] | None = None,
) -> tuple[NDArray[np.float32], bool]:
    """검증된 재생 필터를 시간별로 선택하고 독립된 마이크 발화는 그대로 유지한다."""
    coarse_width = rate // 2
    if coarse_width < 4 or len(mic) < coarse_width or not np.any(reference):
        return mic, False
    groups = _delay_groups(_match_windows(mic, reference, rate, coarse_width), rate)
    history = len(reference) - len(mic)
    paths: list[EchoPath] = []
    for windows in groups:
        width = coarse_width
        if len(windows) < _MIN_WINDOWS:
            width = rate // 10
            if width < 4:
                continue
            windows = _short_windows(mic, reference, rate, windows, width)
            if len(windows) < _MIN_WINDOWS:
                continue
        path = _validated_path(mic, reference, rate, windows, paths + (previous or []), width)
        if path is not None:
            paths.append(path)
    if not paths:
        return mic, False
    if previous is not None:
        previous[:] = paths

    frame = max(1, rate // 50)
    boundaries = np.arange(0, len(mic), frame)
    samples = np.arange(len(mic))
    centers = np.minimum(boundaries + frame / 2, len(mic) - 1)
    frame_lengths = np.diff(boundaries, append=len(mic))
    pcm_floor_power = frame_lengths / 32768**2
    cleaned = mic.astype(np.float64)
    mic_power = np.add.reduceat(cleaned**2, boundaries)
    quiet_mic = mic_power <= pcm_floor_power
    errors = [mic_power]
    predictions = []
    for path in paths:
        estimate = _predict(reference, path.taps, path.start_delay, history, len(mic))
        estimate = _track_replay_gain(mic, estimate, rate)
        errors.append(np.add.reduceat((mic - estimate) ** 2, boundaries))
        predictions.append(estimate)
    chosen = np.argmin(errors, axis=0)
    chosen[quiet_mic] = 0
    for index, estimate in enumerate(predictions, 1):
        selected = chosen == index
        prediction_power = np.add.reduceat(estimate**2, boundaries)
        quiet = (chosen == 0) & (quiet_mic | (prediction_power <= pcm_floor_power))
        weight = np.interp(samples, centers, selected.astype(np.float64))
        # 무음과 맞닿은 선택 프레임 안에서만 페이드를 생략해 첫·끝 음절의 누설을 줄인다.
        # 무음 프레임의 보간점을 바꾸면 그 너머의 마이크 무음에 재생 신호가 생길 수 있다.
        for start in boundaries[1:][quiet[:-1] & selected[1:]]:
            weight[start : start + (frame + 1) // 2] = 1.0
        for stop in boundaries[1:][selected[:-1] & quiet[1:]]:
            weight[stop - frame // 2 : stop] = 1.0
        cleaned -= estimate * weight
    # 재생음이 계속돼도 마이크가 PCM 바닥값인 프레임의 원 샘플을 보존한다.
    np.copyto(cleaned, mic, where=np.repeat(quiet_mic, frame_lengths))
    return cleaned.astype(np.float32), True
