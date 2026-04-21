"""
오디오 품질 검증 모듈

목적: 파이프라인 진입 전 오디오 파일의 볼륨·길이를 검사하여
     저품질 파일이 STT 디코더 루프/크래시를 유발하는 것을 차단한다.

근거: docs/BENCHMARK.md, 실측 크래시 파일 mean_volume=-48.6dB (정상은 -20~-30dB).
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioQualityStatus(str, Enum):
    """오디오 품질 검증 결과 상태."""

    ACCEPT = "accept"
    REJECT = "reject"
    ERROR = "error"  # 측정 실패 (판단 보류)


@dataclass(frozen=True)
class AudioQualityResult:
    """오디오 품질 검증 결과."""

    status: AudioQualityStatus
    mean_volume_db: float | None
    duration_seconds: float | None
    reason: str = ""


class AudioMeasurementError(RuntimeError):
    """ffmpeg/ffprobe 측정 실패 예외."""


def validate_audio_quality(
    audio_path: Path,
    *,
    min_mean_db: float,
    min_duration_s: float,
) -> AudioQualityResult:
    """오디오 파일의 품질을 검증한다.

    Args:
        audio_path: 검증할 오디오 파일 경로
        min_mean_db: 허용 최소 mean_volume (예: -40.0)
        min_duration_s: 허용 최소 재생 시간 (예: 5.0)

    Returns:
        검증 결과. status가 ERROR면 측정 자체가 실패한 경우이며,
        호출자는 보수적으로 ACCEPT 처리하거나 별도 로깅 후 진행할 수 있다.
    """
    try:
        mean_db = _measure_mean_volume_db(audio_path)
        duration_s = _measure_duration_seconds(audio_path)
    except (AudioMeasurementError, RuntimeError, FileNotFoundError) as e:
        logger.warning(f"오디오 품질 측정 실패: {audio_path} ({e})")
        return AudioQualityResult(
            status=AudioQualityStatus.ERROR,
            mean_volume_db=None,
            duration_seconds=None,
            reason=f"측정 실패: {e}",
        )

    if duration_s < min_duration_s:
        return AudioQualityResult(
            status=AudioQualityStatus.REJECT,
            mean_volume_db=mean_db,
            duration_seconds=duration_s,
            reason=f"너무 짧음: {duration_s:.1f}s < {min_duration_s:.1f}s",
        )

    if mean_db < min_mean_db:
        return AudioQualityResult(
            status=AudioQualityStatus.REJECT,
            mean_volume_db=mean_db,
            duration_seconds=duration_s,
            reason=f"저볼륨: mean={mean_db:.1f}dB < {min_mean_db:.1f}dB",
        )

    return AudioQualityResult(
        status=AudioQualityStatus.ACCEPT,
        mean_volume_db=mean_db,
        duration_seconds=duration_s,
    )


def measure_audio_duration(audio_path: Path) -> float:
    """오디오 파일의 재생 시간(초)을 측정한다.

    동적 타임아웃 계산 등 다른 모듈에서 duration만 필요할 때 사용하는
    공개 헬퍼. 내부적으로 ffprobe 를 호출한다.

    Args:
        audio_path: 측정 대상 오디오 파일 경로

    Returns:
        재생 시간 (초)

    Raises:
        AudioMeasurementError: ffprobe 미설치 또는 측정 실패
    """
    return _measure_duration_seconds(audio_path)


def _measure_mean_volume_db(audio_path: Path) -> float:
    """ffmpeg volumedetect 필터로 mean_volume을 측정한다.

    Raises:
        AudioMeasurementError: ffmpeg 미설치 또는 파싱 실패
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AudioMeasurementError("ffmpeg 실행 파일을 찾을 수 없습니다")

    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-nostats",
                "-i",
                str(audio_path),
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise AudioMeasurementError(f"ffmpeg 타임아웃: {audio_path}") from e

    output = result.stderr  # volumedetect는 stderr에 출력
    match = re.search(r"mean_volume:\s*(-?\d+\.?\d*)\s*dB", output)
    if match is None:
        raise AudioMeasurementError(f"mean_volume 파싱 실패: {output[:200]}")
    return float(match.group(1))


def _measure_duration_seconds(audio_path: Path) -> float:
    """ffprobe로 오디오 duration을 측정한다.

    Raises:
        AudioMeasurementError: ffprobe 미설치 또는 파싱 실패
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise AudioMeasurementError("ffprobe 실행 파일을 찾을 수 없습니다")

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except subprocess.TimeoutExpired as e:
        raise AudioMeasurementError(f"ffprobe 타임아웃: {audio_path}") from e
    except subprocess.CalledProcessError as e:
        raise AudioMeasurementError(f"ffprobe 실패: {e.stderr}") from e

    try:
        return float(result.stdout.strip())
    except ValueError as e:
        raise AudioMeasurementError(f"duration 파싱 실패: {result.stdout!r}") from e
