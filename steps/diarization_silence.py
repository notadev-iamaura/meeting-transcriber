"""
화자분리용 긴 무음 압축 유틸리티.

STT 입력은 원본 타임라인을 유지하고, pyannote 화자분리에만 긴 무음이 압축된
WAV 사본을 선택적으로 사용한다. 압축된 타임라인에서 나온 세그먼트는 다시 원본
시간으로 되돌려 merge 단계의 시간 계약을 보존한다.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from steps.diarizer import DiarizationResult, DiarizationSegment

logger = logging.getLogger(__name__)

_SILENCE_START_PATTERN = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END_PATTERN = re.compile(r"silence_end:\s*([0-9.]+)")


@dataclass(frozen=True)
class SilenceSpan:
    """감지된 무음 구간."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        """무음 길이(초)를 반환한다."""
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class TimelineRange:
    """원본 타임라인과 압축 타임라인의 대응 구간."""

    original_start: float
    original_end: float
    compressed_start: float
    compressed_end: float

    @property
    def duration(self) -> float:
        """구간 길이(초)를 반환한다."""
        return max(0.0, self.original_end - self.original_start)


@dataclass(frozen=True)
class SilenceCompressionPlan:
    """긴 무음 압축 계획과 적용 결과."""

    input_path: Path
    output_path: Path | None
    original_duration: float
    compressed_duration: float
    saved_seconds: float
    saved_ratio: float
    silences: list[SilenceSpan]
    timeline: list[TimelineRange]
    applied: bool
    reason: str

    @property
    def audio_path(self) -> Path:
        """화자분리에 사용할 오디오 경로를 반환한다."""
        if self.applied and self.output_path is not None:
            return self.output_path
        return self.input_path


class SilenceCompressionError(Exception):
    """긴 무음 압축 준비 중 발생하는 에러."""


def probe_audio_duration(audio_path: Path) -> float:
    """ffprobe로 오디오 길이를 초 단위로 조회한다."""
    if shutil.which("ffprobe") is None:
        raise SilenceCompressionError("ffprobe를 찾을 수 없습니다.")

    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise SilenceCompressionError(completed.stderr.strip() or "ffprobe 실행 실패")

    try:
        return float(completed.stdout.strip())
    except ValueError as e:
        raise SilenceCompressionError("ffprobe duration 파싱 실패") from e


def detect_long_silences(
    audio_path: Path,
    *,
    min_duration_seconds: float,
    threshold_db: float,
) -> list[SilenceSpan]:
    """ffmpeg silencedetect로 긴 무음 구간을 감지한다."""
    if shutil.which("ffmpeg") is None:
        raise SilenceCompressionError("ffmpeg를 찾을 수 없습니다.")

    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(audio_path),
            "-af",
            f"silencedetect=n={threshold_db}dB:d={min_duration_seconds}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise SilenceCompressionError(completed.stderr.strip() or "ffmpeg silencedetect 실패")

    silences: list[SilenceSpan] = []
    current_start: float | None = None
    for line in completed.stderr.splitlines():
        if start_match := _SILENCE_START_PATTERN.search(line):
            current_start = float(start_match.group(1))
            continue
        if end_match := _SILENCE_END_PATTERN.search(line):
            silence_end = float(end_match.group(1))
            if current_start is not None and silence_end > current_start:
                silences.append(SilenceSpan(current_start, silence_end))
            current_start = None

    return silences


def build_compressed_timeline(
    *,
    original_duration: float,
    silences: list[SilenceSpan],
    keep_seconds: float,
) -> list[TimelineRange]:
    """긴 무음 중간부를 제거한 타임라인 대응표를 만든다."""
    removal_ranges: list[tuple[float, float]] = []
    for silence in sorted(silences, key=lambda item: item.start):
        remove_start = max(0.0, silence.start + keep_seconds)
        remove_end = min(original_duration, silence.end - keep_seconds)
        if remove_end > remove_start:
            removal_ranges.append((remove_start, remove_end))

    kept_ranges: list[tuple[float, float]] = []
    cursor = 0.0
    for remove_start, remove_end in removal_ranges:
        if remove_start > cursor:
            kept_ranges.append((cursor, remove_start))
        cursor = max(cursor, remove_end)
    if cursor < original_duration:
        kept_ranges.append((cursor, original_duration))

    timeline: list[TimelineRange] = []
    compressed_cursor = 0.0
    for original_start, original_end in kept_ranges:
        duration = original_end - original_start
        if duration <= 0:
            continue
        timeline.append(
            TimelineRange(
                original_start=round(original_start, 3),
                original_end=round(original_end, 3),
                compressed_start=round(compressed_cursor, 3),
                compressed_end=round(compressed_cursor + duration, 3),
            )
        )
        compressed_cursor += duration

    return timeline


def _plan_without_compression(
    *,
    audio_path: Path,
    duration: float,
    silences: list[SilenceSpan],
    reason: str,
) -> SilenceCompressionPlan:
    return SilenceCompressionPlan(
        input_path=audio_path,
        output_path=None,
        original_duration=duration,
        compressed_duration=duration,
        saved_seconds=0.0,
        saved_ratio=0.0,
        silences=silences,
        timeline=[
            TimelineRange(
                original_start=0.0,
                original_end=round(duration, 3),
                compressed_start=0.0,
                compressed_end=round(duration, 3),
            )
        ]
        if duration > 0
        else [],
        applied=False,
        reason=reason,
    )


def build_compression_plan(
    *,
    audio_path: Path,
    output_path: Path,
    original_duration: float,
    silences: list[SilenceSpan],
    keep_seconds: float,
    min_saved_seconds: float,
    min_saved_ratio: float,
) -> SilenceCompressionPlan:
    """감지된 무음을 기준으로 실제 적용 여부를 결정한다."""
    if original_duration <= 0:
        return _plan_without_compression(
            audio_path=audio_path,
            duration=original_duration,
            silences=silences,
            reason="invalid_duration",
        )
    if not silences:
        return _plan_without_compression(
            audio_path=audio_path,
            duration=original_duration,
            silences=silences,
            reason="no_long_silence",
        )

    timeline = build_compressed_timeline(
        original_duration=original_duration,
        silences=silences,
        keep_seconds=keep_seconds,
    )
    compressed_duration = sum(item.duration for item in timeline)
    saved_seconds = max(0.0, original_duration - compressed_duration)
    saved_ratio = saved_seconds / original_duration

    if saved_seconds < min_saved_seconds:
        reason = "saved_seconds_below_threshold"
        output: Path | None = None
        applied = False
    elif saved_ratio < min_saved_ratio:
        reason = "saved_ratio_below_threshold"
        output = None
        applied = False
    elif not timeline:
        reason = "empty_timeline"
        output = None
        applied = False
    else:
        reason = "applied"
        output = output_path
        applied = True

    return SilenceCompressionPlan(
        input_path=audio_path,
        output_path=output,
        original_duration=round(original_duration, 3),
        compressed_duration=round(compressed_duration, 3),
        saved_seconds=round(saved_seconds, 3),
        saved_ratio=round(saved_ratio, 4),
        silences=silences,
        timeline=timeline,
        applied=applied,
        reason=reason,
    )


def write_compressed_audio(
    *,
    input_path: Path,
    output_path: Path,
    timeline: list[TimelineRange],
) -> None:
    """타임라인 대응표에 따라 압축된 WAV 사본을 생성한다."""
    if not timeline:
        raise SilenceCompressionError("압축할 타임라인이 비어 있습니다.")
    if shutil.which("ffmpeg") is None:
        raise SilenceCompressionError("ffmpeg를 찾을 수 없습니다.")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    filter_parts: list[str] = []
    labels: list[str] = []
    for idx, segment in enumerate(timeline):
        label = f"a{idx}"
        labels.append(f"[{label}]")
        filter_parts.append(
            f"[0:a]atrim=start={segment.original_start:.3f}:"
            f"end={segment.original_end:.3f},asetpts=PTS-STARTPTS[{label}]"
        )

    if len(labels) == 1:
        filter_complex = f"{filter_parts[0]};{labels[0]}anull[out]"
    else:
        filter_complex = ";".join(filter_parts)
        filter_complex = f"{filter_complex};{''.join(labels)}concat=n={len(labels)}:v=0:a=1[out]"

    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        raise SilenceCompressionError(completed.stderr.strip() or "ffmpeg 압축 실패")


def prepare_diarization_audio(
    *,
    audio_path: Path,
    output_path: Path,
    diarization_config: Any,
) -> SilenceCompressionPlan:
    """설정에 따라 화자분리용 긴 무음 압축 사본을 준비한다."""
    enabled = bool(getattr(diarization_config, "silence_compression_enabled", False))
    duration = probe_audio_duration(audio_path)
    if not enabled:
        return _plan_without_compression(
            audio_path=audio_path,
            duration=duration,
            silences=[],
            reason="disabled",
        )

    min_duration = float(
        getattr(diarization_config, "silence_compression_min_duration_seconds", 10.0)
    )
    threshold_db = float(getattr(diarization_config, "silence_compression_threshold_db", -40.0))
    silences = detect_long_silences(
        audio_path,
        min_duration_seconds=min_duration,
        threshold_db=threshold_db,
    )
    plan = build_compression_plan(
        audio_path=audio_path,
        output_path=output_path,
        original_duration=duration,
        silences=silences,
        keep_seconds=float(getattr(diarization_config, "silence_compression_keep_seconds", 0.75)),
        min_saved_seconds=float(
            getattr(diarization_config, "silence_compression_min_saved_seconds", 20.0)
        ),
        min_saved_ratio=float(
            getattr(diarization_config, "silence_compression_min_saved_ratio", 0.03)
        ),
    )

    if not plan.applied:
        logger.info(
            "화자분리 긴 무음 압축 미적용: reason=%s, silences=%d, saved=%.1fs",
            plan.reason,
            len(plan.silences),
            plan.saved_seconds,
        )
        return plan

    write_compressed_audio(
        input_path=audio_path,
        output_path=output_path,
        timeline=plan.timeline,
    )
    logger.info(
        "화자분리 긴 무음 압축 적용: %.1fs → %.1fs (절약 %.1fs, %.1f%%)",
        plan.original_duration,
        plan.compressed_duration,
        plan.saved_seconds,
        plan.saved_ratio * 100,
    )
    return plan


def remap_diarization_result(
    result: DiarizationResult,
    plan: SilenceCompressionPlan,
    *,
    original_audio_path: Path,
) -> DiarizationResult:
    """압축 타임라인 기준 화자분리 결과를 원본 타임라인으로 되돌린다."""
    if not plan.applied:
        return result

    remapped: list[DiarizationSegment] = []
    for segment in result.segments:
        for timeline in plan.timeline:
            overlap_start = max(segment.start, timeline.compressed_start)
            overlap_end = min(segment.end, timeline.compressed_end)
            if overlap_end <= overlap_start:
                continue

            original_start = timeline.original_start + (overlap_start - timeline.compressed_start)
            original_end = timeline.original_start + (overlap_end - timeline.compressed_start)
            if original_end <= original_start:
                continue
            remapped.append(
                DiarizationSegment(
                    speaker=segment.speaker,
                    start=round(original_start, 3),
                    end=round(original_end, 3),
                )
            )

    remapped.sort(key=lambda item: item.start)
    return DiarizationResult(
        segments=remapped,
        num_speakers=len({segment.speaker for segment in remapped}),
        audio_path=str(original_audio_path),
        model_name=result.model_name,
        output_mode=result.output_mode,
    )
