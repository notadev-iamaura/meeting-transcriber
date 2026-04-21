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
from threading import Lock

logger = logging.getLogger(__name__)


# Phase 1 Cleanup P2: 상태별 검증 횟수 카운터 (관찰성).
# ffmpeg 부재, 손상 파일 등으로 ERROR 가 빈발하면 외부 모니터가 감지할 수 있도록
# 단순 카운터를 노출한다. `get_validation_stats()` 로 조회, `reset_validation_stats()` 로 리셋.
_STATS_LOCK = Lock()
_STATS: dict[str, int] = {"accept": 0, "reject": 0, "error": 0}


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
    except AudioMeasurementError as e:
        # Phase 1 Cleanup (I2): except 범위를 AudioMeasurementError 만으로 좁혀
        # 예상치 못한 RuntimeError/버그가 ERROR 상태로 은폐되지 않도록 fail-fast.
        logger.warning(f"오디오 품질 측정 실패: {audio_path} ({e})")
        _increment_stats("error")
        return AudioQualityResult(
            status=AudioQualityStatus.ERROR,
            mean_volume_db=None,
            duration_seconds=None,
            reason=f"측정 실패: {e}",
        )

    if duration_s < min_duration_s:
        _increment_stats("reject")
        return AudioQualityResult(
            status=AudioQualityStatus.REJECT,
            mean_volume_db=mean_db,
            duration_seconds=duration_s,
            reason=f"너무 짧음: {duration_s:.1f}s < {min_duration_s:.1f}s",
        )

    if mean_db < min_mean_db:
        _increment_stats("reject")
        return AudioQualityResult(
            status=AudioQualityStatus.REJECT,
            mean_volume_db=mean_db,
            duration_seconds=duration_s,
            reason=f"저볼륨: mean={mean_db:.1f}dB < {min_mean_db:.1f}dB",
        )

    _increment_stats("accept")
    return AudioQualityResult(
        status=AudioQualityStatus.ACCEPT,
        mean_volume_db=mean_db,
        duration_seconds=duration_s,
    )


def _increment_stats(status_key: str) -> None:
    """스레드 안전하게 상태별 카운터를 증가시킨다."""
    with _STATS_LOCK:
        _STATS[status_key] = _STATS.get(status_key, 0) + 1


def get_validation_stats() -> dict[str, int]:
    """현재까지의 검증 결과 카운터 스냅샷을 반환한다.

    Phase 1 Cleanup P2: 외부 관찰(API 엔드포인트, 주기적 로깅 등)에서
    ffmpeg 부재/오디오 파이프라인 이상을 조기 감지하기 위한 관찰성 헬퍼.

    Returns:
        {"accept": int, "reject": int, "error": int} 형태의 복사본
    """
    with _STATS_LOCK:
        return dict(_STATS)


def reset_validation_stats() -> None:
    """카운터를 0 으로 초기화한다 (주로 테스트 용)."""
    with _STATS_LOCK:
        for k in _STATS:
            _STATS[k] = 0


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

    # Phase 1 Cleanup (M2): 완전 무음 파일의 "-inf dB" 매칭 추가.
    # ffmpeg 는 무음일 때 `mean_volume: -inf dB` 를 출력하는데 기존 정규식은
    # 숫자만 허용해 파싱 실패 → ERROR 로 흘러갔다. -inf 를 명시적으로 인식하여
    # REJECT 경로(Python 의 -inf < threshold 비교)로 자연스럽게 보낸다.
    match = re.search(r"mean_volume:\s*(-?\d+\.?\d*|-inf)\s*dB", output)
    if match is None:
        # Phase 1 Cleanup (I3): output 앞 200자가 아니라 끝 500자로 변경.
        # volumedetect 결과는 stderr 끝쪽에 출력되므로 끝부분이 진단에 유용.
        # Phase 1 Cleanup (I4): returncode != 0 인 경우 진짜 ffmpeg 에러와
        # 단순 파싱 실패를 구분.
        if result.returncode != 0:
            raise AudioMeasurementError(
                f"ffmpeg 실패 (returncode={result.returncode}): {output[-500:]}"
            )
        raise AudioMeasurementError(f"mean_volume 파싱 실패: {output[-500:]}")

    # -inf 문자열을 float('-inf') 로 변환 (Python 의 비교 연산과 호환)
    raw_value = match.group(1)
    if raw_value == "-inf":
        return float("-inf")
    return float(raw_value)


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
