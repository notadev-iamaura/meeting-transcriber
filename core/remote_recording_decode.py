"""상대 출력의 확인된 발화 구간을 로컬 Whisper의 시간축에 맞춰 전사한다."""

from __future__ import annotations

import math
import subprocess
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import soundfile as sf
from numpy.typing import NDArray

_WHISPER_RATE = 16000
_WHISPER_WINDOW_SECONDS = 30


def _packets(
    source: sf.SoundFile, first: int, last: int
) -> Iterator[tuple[NDArray[np.float32], int, int, int]]:
    """긴 연속 발화는 조용한 경계에서 나누고 양옆 문맥과 각 청크가 맡을 범위를 준다."""
    rate = source.samplerate
    limit = _WHISPER_WINDOW_SECONDS * rate
    margin = rate // 2
    cursor = first
    while cursor < last:
        begin = max(first, cursor - margin)
        source.seek(begin)
        requested = min(limit, last - begin)
        data = source.read(requested, dtype="float32")
        if len(data) != requested:
            raise OSError("상대 트랙의 모든 PCM 샘플을 읽지 못했습니다.")
        end = begin + len(data)
        if end < last:
            # 최대 30초이고 양끝 문맥을 제외한 마지막 청크도 충분한 길이가 되도록
            # 배분한다. 목표 직전 두 초 안의 가장 조용한 20ms 지점에서 경계를 고른다.
            remaining = last - cursor
            count = math.ceil(remaining / (limit - 2 * margin))
            goal = cursor - begin + remaining // count
            frame = max(1, rate // 50)
            frames = min(2 * rate // frame, goal // frame)
            left = goal - frames * frame
            energy = np.square(data[left:goal].reshape(frames, frame)).mean(axis=1)
            quiet = frames - 1 - int(np.argmin(energy[::-1]))
            end = begin + left + quiet * frame + frame // 2
            data = data[: end - begin + margin]
        yield data, begin, cursor, end
        cursor = end


def _whisper_samples(samples: NDArray[np.float32], rate: int) -> NDArray[np.float32]:
    """파일 입력과 동일하게 FFmpeg로 16kHz를 맞춘다. 기본 16kHz 입력은 그대로 쓴다."""
    if rate == _WHISPER_RATE:
        return samples
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "f32le",
            "-ar",
            str(rate),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-f",
            "f32le",
            "-ar",
            str(_WHISPER_RATE),
            "pipe:1",
        ],
        input=samples.astype("<f4", copy=False).tobytes(),
        capture_output=True,
        check=True,
    )
    return np.frombuffer(result.stdout, dtype="<f4")


def _aligned_segment(
    segment: dict[str, Any],
    samples: NDArray[np.float32],
    rate: int,
    begin: int,
    left: int,
    right: int,
) -> dict[str, Any] | None:
    """중첩 문맥은 한 번만 채택하고 디지털 무음에만 정렬된 텍스트를 제외한다."""
    words = segment.get("words") or []
    kept = []
    for unit in words or [segment]:
        start = round(float(unit["start"]) * rate)
        end = round(float(unit["end"]) * rate)
        center = begin + (start + end) // 2
        # 상대 출력은 디지털 PCM이다. 정렬 오차의 앞뒤 여유까지 0인 경우에만
        # 음향 근거가 없다고 판단하므로 실제 작은 말이나 실제 인사말을 보존한다.
        tolerance = rate // 5
        lo = max(0, min(len(samples), start - tolerance))
        hi = max(0, min(len(samples), end + tolerance))
        if left <= center < right and np.any(samples[lo:hi]):
            kept.append(
                {
                    **unit,
                    "start": round((begin + max(0, min(start, len(samples)))) / rate, 4),
                    "end": round((begin + max(0, min(end, len(samples)))) / rate, 4),
                }
            )
    if not kept:
        return None
    if not words:
        return kept[0]
    return {
        **segment,
        "words": kept,
        "start": kept[0]["start"],
        "end": kept[-1]["end"],
        "text": segment["text"] if len(words) == len(kept) else "".join(w["word"] for w in kept),
    }


def transcribe_remote_recording(
    transcribe: Callable[..., dict[str, Any]],
    audio: str,
    clip_timestamps: list[float],
    **kwargs: Any,
) -> dict[str, Any]:
    """실제 상대 PCM만 고정 온도로 디코딩하고 발화 시각을 회의 원본 기준으로 복원한다."""
    segments = []
    language = kwargs.get("language", "ko")
    with sf.SoundFile(audio) as source:
        rate = source.samplerate
        if source.channels != 1:
            raise ValueError("상대 전사에는 분리한 단일 채널이 필요합니다.")
        for left, right in zip(clip_timestamps[::2], clip_timestamps[1::2], strict=True):
            first, last = max(0, round(left * rate)), min(source.frames, round(right * rate))
            for samples, begin, owned_start, owned_end in _packets(source, first, last):
                if not np.any(samples[owned_start - begin : owned_end - begin]):
                    continue
                result = transcribe(_whisper_samples(samples, rate), temperature=0.0, **kwargs)
                language = result.get("language", language)
                for segment in result.get("segments", []):
                    aligned = _aligned_segment(
                        segment, samples, rate, begin, owned_start, owned_end
                    )
                    if aligned is not None:
                        segments.append(aligned)
    return {
        "text": " ".join(segment["text"].strip() for segment in segments),
        "segments": segments,
        "language": language,
    }
