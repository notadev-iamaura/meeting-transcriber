"""앱이 분리해 녹음한 WAV의 채널 계약 및 임시 전사용 트랙 생성."""

from __future__ import annotations

import logging
import math
import os
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray
    from soundfile import SoundFile

    from config import SplitTrackProcessingConfig
    from steps.transcriber import AudioFileIdentity

logger = logging.getLogger(__name__)

RECORDING_TRACK_COMMENT = "recap-microphone-system-v1"
_ECHO_REDUCED_MIC_COMMENT = "recap-echo-reduced-microphone-v1"
SELF_SPEAKER = "나"
MIC_CHANNEL = 0
SYSTEM_CHANNEL = 1


@dataclass(frozen=True)
class PreparedRecordingTrack:
    """원래 시간축을 유지하는 전사용 WAV와 처리할 구간."""

    path: Path
    clip_timestamps: tuple[float, ...]
    active_rms_dbfs: float
    speech_intervals: tuple[tuple[float, float], ...] = ()


def matches_track_layout(source: SoundFile) -> bool:
    """앱 표시가 있고 저장 포맷도 일치하는 파일만 두 트랙으로 해석한다."""
    return (
        source.format in {"WAV", "WAVEX"}
        and source.channels == 2
        and source.subtype == "PCM_16"
        and source.comment == RECORDING_TRACK_COMMENT
    )


def has_separate_mic_track(wav_path: Path) -> bool:
    """녹음 파일 표시를 no-follow로 읽고 일반 오디오와 구분한다."""
    import soundfile as sf

    from steps.transcriber import open_audio_path_no_symlinks

    with open_audio_path_no_symlinks(wav_path) as (source_fd, _):
        try:
            with sf.SoundFile(source_fd, closefd=False) as source:
                return matches_track_layout(source)
        except sf.LibsndfileError:
            return False


def has_sustained_recording_signal(
    wav_path: Path, minimum_rms: float, expected_identity: AudioFileIdentity | None = None
) -> bool | None:
    """출처가 확인된 WAV에서 어느 한 채널에 지속 신호가 있는지 확인한다."""
    import numpy as np
    import soundfile as sf

    from core.audio_quality import AudioFailureKind
    from steps.transcriber import AudioAdmissionError, open_audio_path_no_symlinks

    with open_audio_path_no_symlinks(wav_path) as (source_fd, identity):
        if expected_identity is not None and identity != expected_identity:
            raise AudioAdmissionError(
                "음량 검사 전에 분리 녹음 파일이 변경되었습니다.",
                failure_kind=AudioFailureKind.SOURCE_BUSY,
            )
        try:
            with sf.SoundFile(source_fd, closefd=False) as audio:
                if not matches_track_layout(audio):
                    return None
                window = max(1, round(audio.samplerate * 0.10))
                found = False
                for block in audio.blocks(blocksize=window * 256, dtype="float32", always_2d=True):
                    complete = len(block) // window * window
                    powers = np.square(block[:complete], dtype=np.float64)
                    rms_squared = powers.reshape(-1, window, 2).mean(axis=1)
                    if np.any(rms_squared > minimum_rms * minimum_rms):
                        found = True
                        break
        except sf.LibsndfileError:
            return None  # 지원 여부와 전체 디코딩 판정은 공통 FFmpeg 경로가 담당한다.
        stat = os.fstat(source_fd)
        if identity != (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        ):
            raise AudioAdmissionError(
                "음량 검사 중 분리 녹음 파일이 변경되었습니다.",
                failure_kind=AudioFailureKind.SOURCE_BUSY,
            )
        return found


def extract_recording_tracks(
    wav_path: Path,
    output_dir: Path,
    channels: tuple[int, ...],
    expected_identity: AudioFileIdentity,
    *,
    echo_settings: SplitTrackProcessingConfig | None = None,
) -> dict[int, Path]:
    """원래 시간을 유지해 채널을 분리하고, 요청된 마이크 사본에만 누설을 줄인다."""
    import numpy as np
    import soundfile as sf

    from core.audio_quality import AudioFailureKind
    from core.echo_suppression import EchoPath, suppress_recording_echo
    from steps.transcriber import AudioAdmissionError, open_audio_path_no_symlinks

    paths = {channel: output_dir / f"channel-{channel}.wav" for channel in channels}
    audible: set[int] = set()
    with open_audio_path_no_symlinks(wav_path) as (source_fd, identity), ExitStack() as stack:
        if identity != expected_identity:
            raise AudioAdmissionError(
                "품질 검사 후 분리 녹음 파일이 변경되었습니다.",
                failure_kind=AudioFailureKind.SOURCE_BUSY,
            )
        source = stack.enter_context(sf.SoundFile(source_fd, closefd=False))
        if not matches_track_layout(source) or source.frames <= 0:
            raise ValueError("분리 녹음의 WAV 프로필이 유효하지 않습니다.")
        suppress = (
            MIC_CHANNEL in channels
            and echo_settings is not None
            and echo_settings.echo_suppression
        )
        delay = (
            source.samplerate * echo_settings.echo_max_delay_ms // 1000
            if suppress and echo_settings is not None
            else 0
        )
        outputs = {}
        for channel, path in paths.items():
            file = stack.enter_context(path.open("xb"))
            outputs[channel] = stack.enter_context(
                sf.SoundFile(
                    file,
                    mode="w",
                    samplerate=source.samplerate,
                    channels=1,
                    format="WAV",
                    subtype="PCM_16",
                )
            )
        read_frames = 0
        echo_blocks = 0
        previous_paths: list[EchoPath] = []
        block_size = source.samplerate * 24 if suppress else 65536
        for start in range(0, source.frames, block_size):
            stop = min(source.frames, start + block_size)
            if suppress:
                # 경계 직전 기기 지연이 바뀌거나 마지막 블록이 짧아도 양쪽에서
                # 검증 구간을 확보한다. 중앙 구간만 기록해 샘플이 겹치지 않는다.
                context = 3 * source.samplerate
                left = max(0, start - context)
                right = min(source.frames, stop + context)
                ref_start = max(0, left - delay)
                source.seek(ref_start)
                audio = source.read(right - ref_start, dtype="float32", always_2d=True)
                reference = np.pad(audio[:, SYSTEM_CHANNEL], (delay - (left - ref_start), 0))
                cleaned, applied = suppress_recording_echo(
                    audio[left - ref_start :, MIC_CHANNEL],
                    reference,
                    source.samplerate,
                    previous_paths,
                )
                block = audio[start - ref_start : stop - ref_start]
                block[:, MIC_CHANNEL] = cleaned[start - left : stop - left]
                echo_blocks += int(applied)
            else:
                block = source.read(stop - start, dtype="int16", always_2d=True)
            read_frames += len(block)
            for channel, output in outputs.items():
                samples = block[:, channel]
                if channel not in audible and samples.any():
                    audible.add(channel)
                output.write(samples)
        if read_frames != source.frames:
            raise ValueError("분리 녹음의 모든 샘플을 읽을 수 없습니다.")
        if echo_blocks:
            outputs[MIC_CHANNEL].comment = _ECHO_REDUCED_MIC_COMMENT
            logger.info(f"마이크 전사용 사본에서 검증된 스피커 누설 감소: {echo_blocks}개 구간")

    return {channel: path for channel, path in paths.items() if channel in audible}


def _frame_power(samples: NDArray[np.float32], frame_size: int) -> NDArray[np.float64]:
    """분석 프레임별 평균 제곱값을 계산한다. 마지막 짧은 프레임도 반영한다."""
    import numpy as np

    complete = len(samples) // frame_size * frame_size
    squared = np.square(samples, dtype=np.float64)
    powers: NDArray[np.float64] = squared[:complete].reshape(-1, frame_size).mean(axis=1)
    if complete < len(squared):
        powers = np.append(powers, squared[complete:].mean())
    return powers


def _active_ranges(
    active: NDArray[np.bool_], interval_seconds: float, duration: float, min_gap: float
) -> list[tuple[float, float]]:
    """짧은 발화를 보존하고, 긴 무음 사이에 있는 발화 구간을 반환한다."""
    import numpy as np

    edges = np.flatnonzero(np.diff(active, prepend=False, append=False))
    candidates: list[tuple[int, int]] = []
    bridge = math.ceil(0.14 / interval_seconds)
    for start, end in edges.reshape(-1, 2):
        if candidates and start - candidates[-1][1] <= bridge:
            candidates[-1] = (candidates[-1][0], int(end))
        else:
            candidates.append((int(start), int(end)))

    regions: list[tuple[float, float]] = []
    for start, end in candidates:
        if np.count_nonzero(active[start:end]) * interval_seconds < 0.06:
            continue
        left = max(0.0, start * interval_seconds - 0.45)
        right = min(duration, end * interval_seconds + 0.45)
        if regions and left - regions[-1][1] < min_gap:
            regions[-1] = (regions[-1][0], right)
        else:
            regions.append((left, right))
    return regions


def prepare_recording_track(
    path: Path, settings: SplitTrackProcessingConfig, minimum_dbfs: float
) -> PreparedRecordingTrack | None:
    """발화가 있는 순간을 기준으로 음량을 보정하고 무음 건너뛰기 범위를 만든다."""
    import numpy as np
    import soundfile as sf

    windows = []
    with sf.SoundFile(path) as audio:
        echo_reduced = audio.comment == _ECHO_REDUCED_MIC_COMMENT
        rate = audio.samplerate
        duration = audio.frames / rate
        frame = max(1, round(rate * 0.020))
        for samples in audio.blocks(blocksize=frame * 1024, dtype="float32"):
            windows.append(_frame_power(samples, frame))
    if not windows:
        return None
    powers = np.concatenate(windows)
    # 소프트한 말까지 살리기 위해 바닥값을 낮게 유지한다. 녹음에 확인 가능한
    # 배경음이 있을 때만 그보다 약간 높은 신호를 후보로 본다.
    floor = 10 ** (settings.speech_floor_dbfs / 10)
    noise, upper = np.quantile(powers, [0.1, 0.95])
    threshold = max(floor, noise * 2.5) if upper >= noise * 4 else floor
    active = powers >= threshold
    regions = _active_ranges(active, frame / rate, duration, settings.silence_gap_seconds)
    if not regions:
        return None
    # 순간적인 클릭이나 짧은 큰 잡음이 전체 발화의 증폭량을 결정하지 않는다.
    # 활성 프레임의 중앙값을 사용하고, 포화 제한은 해당 샘플에만 적용한다.
    active_dbfs = 10 * math.log10(float(np.median(powers[active])))
    gain_db = min(settings.max_gain_db, max(-26.0, minimum_dbfs + 3) - active_dbfs)
    gain_db = max(0.0, gain_db)
    prepared_dbfs = active_dbfs + gain_db
    if gain_db >= 0.1:
        normalized = path.with_name(f"{path.stem}-speech.wav")
        with (
            sf.SoundFile(path) as audio,
            normalized.open("xb") as destination,
            sf.SoundFile(
                destination, mode="w", samplerate=rate, channels=1, format="WAV", subtype="PCM_16"
            ) as output,
        ):
            gain = 10 ** (gain_db / 20)
            for samples in audio.blocks(blocksize=65536, dtype="float32"):
                output.write(np.clip(samples * gain, -0.98, 0.98))
        path = normalized
    verified_speech: tuple[tuple[float, float], ...] = ()
    if settings.speech_verification:
        # 에너지만으로는 선풍기·키보드·순간 잡음과 말소리를 구별할 수 없다.
        # 작은 목소리를 먼저 보정한 사본에서 검사하고, 원본은 변경하지 않는다.
        speech = _detect_recording_speech(path, settings.speech_threshold)
        if echo_reduced:
            # 잔향이 증폭 뒤 말소리처럼 들려도 실제 마이크 사본에서 발화 최저 음량을
            # 넘는 부분에만 STT를 허용한다. 조용한 말의 시작·끝을 위한 여백은 보존한다.
            audible = _active_ranges(powers >= floor, frame / rate, duration, 0.0)
            speech = _supported_speech(speech, audible)
        if not speech:
            return None
        verified_speech = tuple(speech)
        regions = []
        for left, right in speech:
            if regions and left - regions[-1][1] < settings.silence_gap_seconds:
                regions[-1] = (regions[-1][0], max(regions[-1][1], right))
            else:
                regions.append((left, right))
        if echo_reduced:
            # 앞뒤 문장을 묶더라도 두 문장 사이에 제외된 에코만 있는 구간을 다시 열지 않는다.
            regions = _supported_speech(regions, audible)
    times = tuple(time for region in regions for time in region)
    included = sum(end - start for start, end in regions)
    logger.info(
        f"분리 음성 전처리: {included:.2f}/{duration:.2f}초 전사, "
        f"활성 음량 {active_dbfs:.1f}dBFS, 보정 {gain_db:.1f}dB"
    )
    return PreparedRecordingTrack(path, times, prepared_dbfs, verified_speech)


def _supported_speech(
    detected: list[tuple[float, float]],
    audible: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """정렬된 두 구간 목록에서 원음과 음성 감지 양쪽이 지지하는 시간대를 반환한다."""
    supported = []
    i = j = 0
    while i < len(detected) and j < len(audible):
        left = max(detected[i][0], audible[j][0])
        right = min(detected[i][1], audible[j][1])
        if left < right:
            supported.append((left, right))
        if detected[i][1] < audible[j][1]:
            i += 1
        else:
            j += 1
    return supported


def _detect_recording_speech(path: Path, threshold: float) -> list[tuple[float, float]]:
    """패키지에 포함된 CPU 모델로 발화를 확인하며 긴 파일도 일정한 메모리로 읽는다."""
    import soundfile as sf
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad

    model = load_silero_vad()  # 패키지 내부 JIT 가중치만 사용하며 다운로드하지 않는다.
    regions: list[tuple[float, float]] = []
    with sf.SoundFile(path) as audio:
        rate = audio.samplerate
        if rate not in (8000, 16000) or audio.channels != 1:
            raise ValueError("분리 음성 검사는 8/16kHz mono WAV가 필요합니다.")
        # 겹쳐 읽어 블록 경계에서 시작한 짧은 발화도 양쪽 문맥으로 검사한다.
        block_size, overlap = rate * 24, rate * 2
        offset = 0
        while offset < audio.frames:
            audio.seek(offset)
            samples = audio.read(block_size, dtype="float32")
            with torch.inference_mode():
                speech = get_speech_timestamps(
                    torch.from_numpy(samples),
                    model,
                    sampling_rate=rate,
                    threshold=threshold,
                    min_speech_duration_ms=64,
                    min_silence_duration_ms=300,
                    speech_pad_ms=450,
                )
            for interval in speech:
                left = (offset + interval["start"]) / rate
                right = (offset + interval["end"]) / rate
                regions.append((left, right))
            if offset + len(samples) >= audio.frames:
                break
            offset += block_size - overlap
    merged: list[tuple[float, float]] = []
    for left, right in sorted(regions):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged
