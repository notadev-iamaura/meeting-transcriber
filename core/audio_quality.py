"""
오디오 품질 검증 모듈

목적: 파이프라인 진입 전 오디오 파일의 볼륨·길이를 검사하여
     저품질 파일이 STT 디코더 루프/크래시를 유발하는 것을 차단한다.

근거: docs/BENCHMARK.md, 실측 크래시 파일 mean_volume=-48.6dB (정상은 -20~-30dB).
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from pathlib import Path
from threading import Lock
from typing import TypeAlias

logger = logging.getLogger(__name__)

_FFMPEG_MEDIA_DIAGNOSTIC_MARKERS = (
    "error while decoding stream",
    "invalid data found when processing input",
    "header missing",
)
_SECURITY_DIAGNOSTIC_MARKERS = (
    "permission denied",
    "operation not permitted",
)
_SOURCE_BUSY_DIAGNOSTIC_MARKERS = (
    "device or resource busy",
    "resource temporarily unavailable",
    "no such file or directory",
)
_NORMALIZED_SAMPLE_RATE = 16_000
_DEFAULT_DECODE_TIMEOUT_BASE_SECONDS = 60.0
_DEFAULT_DECODE_TIMEOUT_FACTOR = 0.25
_DEFAULT_DECODE_TIMEOUT_CAP_SECONDS = 900.0
_AUDIO_QUALITY_ALGORITHM_VERSION = "s16-16khz-effective-duration-v2"
_ACCEPT_CACHE_MAX_ENTRIES = 128

AudioFileIdentity: TypeAlias = tuple[int, int, int, int, int]
_AcceptCacheKey: TypeAlias = tuple[
    int,
    int,
    int,
    int,
    int,
    float,
    float,
    str,
]


# Phase 1 Cleanup P2: 상태별 검증 횟수 카운터 (관찰성).
# ffmpeg 부재, 손상 파일 등으로 ERROR 가 빈발하면 외부 모니터가 감지할 수 있도록
# 단순 카운터를 노출한다. `get_validation_stats()` 로 조회, `reset_validation_stats()` 로 리셋.
_STATS_LOCK = Lock()
_STATS: dict[str, int] = {"accept": 0, "reject": 0, "error": 0}
_ACCEPT_CACHE_LOCK = Lock()
_ACCEPT_CACHE: OrderedDict[_AcceptCacheKey, AudioQualityResult] = OrderedDict()


class AudioQualityStatus(str, Enum):  # noqa: UP042 — str 상속 유지 (기존 직렬화 호환)
    """오디오 품질 검증 결과 상태."""

    ACCEPT = "accept"
    REJECT = "reject"
    ERROR = "error"  # 측정 실패 (failure_kind에 따라 원본 보존/격리 결정)


class AudioFailureKind(str, Enum):  # noqa: UP042 — JSON 직렬화 호환
    """오디오 gate 실패의 소유권과 후속 처리를 결정하는 타입."""

    MEDIA_INVALID = "media_invalid"
    SOURCE_BUSY = "source_busy"
    INFRA_UNAVAILABLE = "infra_unavailable"
    SECURITY_BLOCKED = "security_blocked"


@dataclass(frozen=True)
class AudioQualityResult:
    """오디오 품질 검증 결과."""

    status: AudioQualityStatus
    mean_volume_db: float | None
    duration_seconds: float | None
    reason: str = ""
    failure_kind: AudioFailureKind | None = None

    @property
    def quarantine_safe(self) -> bool:
        """파일 자체의 결함이 확인되어 격리해도 되는지 반환한다."""
        return self.failure_kind is AudioFailureKind.MEDIA_INVALID


class _MeasuredMeanVolume(float):
    """float 호환을 유지하며 full-decode 메타데이터를 담는 내부 타입."""

    decoded_duration_seconds: float
    media_diagnostic: str | None

    def __new__(
        cls,
        mean_volume_db: float,
        *,
        decoded_duration_seconds: float,
        media_diagnostic: str | None,
    ) -> _MeasuredMeanVolume:
        instance = super().__new__(cls, mean_volume_db)
        instance.decoded_duration_seconds = decoded_duration_seconds
        instance.media_diagnostic = media_diagnostic
        return instance


class AudioMeasurementError(RuntimeError):
    """ffmpeg/ffprobe 측정 실패 예외."""

    def __init__(
        self,
        message: str,
        *,
        failure_kind: AudioFailureKind = AudioFailureKind.INFRA_UNAVAILABLE,
        decoded_duration_seconds: float | None = None,
        decode_fallback_allowed: bool = False,
    ) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind
        self.decoded_duration_seconds = decoded_duration_seconds
        self.decode_fallback_allowed = decode_fallback_allowed


def validate_audio_quality(
    audio_path: Path,
    *,
    min_mean_db: float,
    min_duration_s: float,
    expected_identity: AudioFileIdentity | None = None,
    decode_timeout_base_seconds: float = _DEFAULT_DECODE_TIMEOUT_BASE_SECONDS,
    decode_timeout_factor: float = _DEFAULT_DECODE_TIMEOUT_FACTOR,
    decode_timeout_cap_seconds: float = _DEFAULT_DECODE_TIMEOUT_CAP_SECONDS,
) -> AudioQualityResult:
    """오디오 파일의 품질을 검증한다.

    Args:
        audio_path: 검증할 오디오 파일 경로
        min_mean_db: 허용 최소 mean_volume (예: -40.0)
        min_duration_s: 허용 최소 재생 시간 (예: 30.0)
        expected_identity: no-follow 검증이 확정한 source fingerprint.
            있을 때만 ACCEPT 결과를 process-local cache에서 재사용한다.
        decode_timeout_base_seconds: full-decode 최소 timeout.
        decode_timeout_factor: ffprobe duration에 곱할 timeout 계수.
        decode_timeout_cap_seconds: full-decode timeout 상한. duration hint가
            없을 때는 이 값을 사용한다.

    Returns:
        검증 결과. status가 ERROR면 측정 자체가 실패한 경우이며,
        호출자는 전사 큐에 넣지 않고 ``quarantine_safe``를 확인해야 한다.
    """
    cache_key = _accept_cache_key(
        expected_identity,
        min_mean_db=min_mean_db,
        min_duration_s=min_duration_s,
    )
    if cache_key is not None:
        cached_result = _get_cached_accept(cache_key)
        if cached_result is not None:
            _increment_stats("accept")
            return cached_result

    probe_duration_s: float | None = None
    probe_error: AudioMeasurementError | None = None
    try:
        probe_duration_s = _measure_duration_seconds(audio_path)
    except AudioMeasurementError as e:
        # AAC encoder padding은 29.999초 source를 decoded 30.016초로
        # 늘릴 수 있다. probe 길이 없이 decoded-only ACCEPT하면
        # strict cutoff를 우회하므로 full decode가 정상이어도 ACCEPT하지
        # 않는다. 다만 decoded-short/저볼륨은 안전한 REJECT 근거가 된다.
        if e.failure_kind in {
            AudioFailureKind.SECURITY_BLOCKED,
            AudioFailureKind.SOURCE_BUSY,
        }:
            return _measurement_error_result(audio_path, e)
        probe_error = e
        logger.info(f"ffprobe duration을 확정할 수 없어 fail-closed 디코딩: {audio_path} ({e})")

    try:
        measured_mean_db = _measure_mean_volume_db(
            audio_path,
            min_decoded_duration_s=min_duration_s,
            probe_duration_s=probe_duration_s,
            decode_timeout_base_seconds=decode_timeout_base_seconds,
            decode_timeout_factor=decode_timeout_factor,
            decode_timeout_cap_seconds=decode_timeout_cap_seconds,
        )
    except AudioMeasurementError as e:
        if (
            e.failure_kind is AudioFailureKind.MEDIA_INVALID
            and e.decoded_duration_seconds is not None
            and e.decoded_duration_seconds < min_duration_s
        ):
            return _policy_reject_result(
                duration_seconds=e.decoded_duration_seconds,
                min_duration_s=min_duration_s,
            )
        return _measurement_error_result(
            audio_path,
            e,
            duration_seconds=(
                e.decoded_duration_seconds
                if e.decoded_duration_seconds is not None
                else probe_duration_s
            ),
        )

    mean_db = float(measured_mean_db)
    # ffmpeg sample count는 잘린 파일을 찾고, 성공한 ffprobe duration은
    # AAC encoder padding이 29.999초 source를 30초 이상으로 늘리는 경우를
    # 보정한다. 둘 다 있으면 더 보수적인 값을 유효 길이로 쓴다.
    decoded_duration_s: float | None = getattr(
        measured_mean_db,
        "decoded_duration_seconds",
        None,
    )
    if decoded_duration_s is None:
        duration_s = probe_duration_s
    elif probe_duration_s is None:
        duration_s = decoded_duration_s
    else:
        duration_s = min(decoded_duration_s, probe_duration_s)
    media_diagnostic = getattr(measured_mean_db, "media_diagnostic", None)

    if duration_s is None:
        return _measurement_error_result(
            audio_path,
            AudioMeasurementError(
                "full decode 결과에 재생 시간 정보가 없습니다",
                failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
            ),
        )

    if duration_s < min_duration_s:
        return _policy_reject_result(
            duration_seconds=duration_s,
            min_duration_s=min_duration_s,
        )

    if media_diagnostic is not None:
        return _measurement_error_result(
            audio_path,
            AudioMeasurementError(
                f"ffmpeg 디코딩 경고 감지: {media_diagnostic}",
                failure_kind=AudioFailureKind.MEDIA_INVALID,
                decoded_duration_seconds=duration_s,
            ),
            duration_seconds=duration_s,
        )

    if mean_db < min_mean_db:
        _increment_stats("reject")
        return AudioQualityResult(
            status=AudioQualityStatus.REJECT,
            mean_volume_db=mean_db,
            duration_seconds=duration_s,
            reason=f"저볼륨: mean={mean_db:.1f}dB < {min_mean_db:.1f}dB",
            failure_kind=AudioFailureKind.MEDIA_INVALID,
        )

    if probe_error is not None:
        # full decode 성공과 ffprobe MEDIA_INVALID가 충돌하면 파일 결함이
        # 확정된 것이 아니므로 격리하지 않고 인프라/호환성 보류로 남긴다.
        if probe_error.failure_kind is AudioFailureKind.MEDIA_INVALID:
            probe_error = AudioMeasurementError(
                f"ffprobe는 미디어를 읽지 못했으나 ffmpeg decode는 성공: {probe_error}",
                failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
                decoded_duration_seconds=duration_s,
            )
        return _measurement_error_result(
            audio_path,
            probe_error,
            duration_seconds=duration_s,
        )

    accepted_result = AudioQualityResult(
        status=AudioQualityStatus.ACCEPT,
        mean_volume_db=mean_db,
        duration_seconds=duration_s,
    )
    if cache_key is not None:
        _store_cached_accept(cache_key, accepted_result)
    _increment_stats("accept")
    return accepted_result


def _policy_reject_result(
    *,
    duration_seconds: float,
    min_duration_s: float,
) -> AudioQualityResult:
    """실제 디코딩 길이가 정책 임계 미만인 미디어를 거부한다."""
    _increment_stats("reject")
    return AudioQualityResult(
        status=AudioQualityStatus.REJECT,
        mean_volume_db=None,
        duration_seconds=duration_seconds,
        reason=f"너무 짧음: {duration_seconds:.3f}s < {min_duration_s:.3f}s",
        failure_kind=AudioFailureKind.MEDIA_INVALID,
    )


def _measurement_error_result(
    audio_path: Path,
    error: AudioMeasurementError,
    *,
    duration_seconds: float | None = None,
) -> AudioQualityResult:
    """오디오 측정 실패를 원인 타입이 보존된 ERROR 결과로 변환한다."""
    logger.warning(f"오디오 품질 측정 실패: {audio_path} ({error})")
    _increment_stats("error")
    return AudioQualityResult(
        status=AudioQualityStatus.ERROR,
        mean_volume_db=None,
        duration_seconds=duration_seconds,
        reason=f"측정 실패: {error}",
        failure_kind=error.failure_kind,
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


def reset_audio_quality_cache() -> None:
    """process-local ACCEPT cache를 비운다 (주로 테스트용)."""
    with _ACCEPT_CACHE_LOCK:
        _ACCEPT_CACHE.clear()


def _accept_cache_key(
    expected_identity: AudioFileIdentity | None,
    *,
    min_mean_db: float,
    min_duration_s: float,
) -> _AcceptCacheKey | None:
    """secure identity가 제공된 ACCEPT 호출의 cache key를 만든다."""
    if expected_identity is None:
        return None
    dev, ino, size, mtime_ns, ctime_ns = expected_identity
    return (
        dev,
        ino,
        size,
        mtime_ns,
        ctime_ns,
        float(min_mean_db),
        float(min_duration_s),
        _AUDIO_QUALITY_ALGORITHM_VERSION,
    )


def _get_cached_accept(cache_key: _AcceptCacheKey) -> AudioQualityResult | None:
    """LRU에서 ACCEPT 결과를 읽고 최근 사용 위치로 옮긴다."""
    with _ACCEPT_CACHE_LOCK:
        cached_result = _ACCEPT_CACHE.get(cache_key)
        if cached_result is not None:
            _ACCEPT_CACHE.move_to_end(cache_key)
        return cached_result


def _store_cached_accept(
    cache_key: _AcceptCacheKey,
    result: AudioQualityResult,
) -> None:
    """ACCEPT 결과를 유한 LRU에 저장한다."""
    if result.status is not AudioQualityStatus.ACCEPT:
        return
    with _ACCEPT_CACHE_LOCK:
        _ACCEPT_CACHE[cache_key] = result
        _ACCEPT_CACHE.move_to_end(cache_key)
        while len(_ACCEPT_CACHE) > _ACCEPT_CACHE_MAX_ENTRIES:
            _ACCEPT_CACHE.popitem(last=False)


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


def _measure_mean_volume_db(
    audio_path: Path,
    *,
    min_decoded_duration_s: float | None = None,
    probe_duration_s: float | None = None,
    decode_timeout_base_seconds: float = _DEFAULT_DECODE_TIMEOUT_BASE_SECONDS,
    decode_timeout_factor: float = _DEFAULT_DECODE_TIMEOUT_FACTOR,
    decode_timeout_cap_seconds: float = _DEFAULT_DECODE_TIMEOUT_CAP_SECONDS,
) -> float:
    """ffmpeg full decode로 16 kHz mono 샘플 수와 mean volume을 측정한다.

    반환값은 float 호환 내부 타입이며, 검증 경로에서는 마지막
    ``n_samples / 16000``을 실제 길이의 단일 authority로 사용한다.

    Raises:
        AudioMeasurementError: ffmpeg 미설치 또는 파싱 실패
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AudioMeasurementError("ffmpeg 실행 파일을 찾을 수 없습니다")

    decode_timeout_seconds = _compute_decode_timeout_seconds(
        probe_duration_s,
        base_seconds=decode_timeout_base_seconds,
        factor=decode_timeout_factor,
        cap_seconds=decode_timeout_cap_seconds,
    )

    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-nostats",
                "-i",
                str(audio_path),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-af",
                ("aformat=sample_fmts=s16:sample_rates=16000:channel_layouts=mono,volumedetect"),
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=decode_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise AudioMeasurementError(
            f"ffmpeg 타임아웃: {audio_path}",
            failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
        ) from e
    except OSError as e:
        raise AudioMeasurementError(
            f"ffmpeg 실행 실패: {e}",
            failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
        ) from e

    output = result.stderr or ""  # volumedetect는 stderr에 출력
    media_diagnostic = _find_diagnostic_marker(
        output,
        _FFMPEG_MEDIA_DIAGNOSTIC_MARKERS,
    )
    sample_count = _parse_ffmpeg_sample_count(output)
    decoded_duration = sample_count / _NORMALIZED_SAMPLE_RATE if sample_count is not None else None

    if min_decoded_duration_s is not None and decoded_duration is not None:
        if decoded_duration < min_decoded_duration_s:
            short_failure_kind = _classify_process_failure(
                output,
                default=(
                    AudioFailureKind.MEDIA_INVALID
                    if result.returncode == 0
                    else AudioFailureKind.INFRA_UNAVAILABLE
                ),
            )
            if short_failure_kind is AudioFailureKind.MEDIA_INVALID:
                raise AudioMeasurementError(
                    f"실제 디코딩 길이가 너무 짧음: "
                    f"{decoded_duration:.3f}s < {min_decoded_duration_s:.3f}s",
                    failure_kind=AudioFailureKind.MEDIA_INVALID,
                    decoded_duration_seconds=decoded_duration,
                )

    if sample_count is None:
        failure_kind = (
            AudioFailureKind.MEDIA_INVALID
            if media_diagnostic is not None
            else _classify_process_failure(
                output,
                default=AudioFailureKind.INFRA_UNAVAILABLE,
            )
        )
        detail = (
            f"ffmpeg 디코딩 경고 감지: {media_diagnostic}"
            if media_diagnostic is not None
            else f"n_samples 파싱 실패: {output[-500:]}"
        )
        if result.returncode != 0:
            detail = f"ffmpeg 실패 (returncode={result.returncode}): {detail}"
        raise AudioMeasurementError(detail, failure_kind=failure_kind)
    assert decoded_duration is not None

    # Phase 1 Cleanup (M2): 완전 무음 파일의 "-inf dB" 매칭 추가.
    # ffmpeg 는 무음일 때 `mean_volume: -inf dB` 를 출력하는데 기존 정규식은
    # 숫자만 허용해 파싱 실패 → ERROR 로 흘러갔다. -inf 를 명시적으로 인식하여
    # REJECT 경로(Python 의 -inf < threshold 비교)로 자연스럽게 보낸다.
    mean_matches = re.findall(
        r"mean_volume:\s*(-?\d+\.?\d*|-inf)\s*dB",
        output,
    )
    if not mean_matches:
        failure_kind = (
            AudioFailureKind.MEDIA_INVALID
            if media_diagnostic is not None
            else _classify_process_failure(
                output,
                default=AudioFailureKind.INFRA_UNAVAILABLE,
            )
        )
        detail = f"mean_volume 파싱 실패: {output[-500:]}"
        if result.returncode != 0:
            detail = f"ffmpeg 실패 (returncode={result.returncode}): {detail}"
        raise AudioMeasurementError(
            detail,
            failure_kind=failure_kind,
            decoded_duration_seconds=decoded_duration,
        )

    if result.returncode != 0 or media_diagnostic is not None:
        failure_kind = _classify_process_failure(
            output,
            default=(
                AudioFailureKind.MEDIA_INVALID
                if media_diagnostic is not None
                else AudioFailureKind.INFRA_UNAVAILABLE
            ),
        )
        detail = (
            f"ffmpeg 디코딩 경고 감지: {media_diagnostic}"
            if media_diagnostic is not None
            else f"ffmpeg 실패 (returncode={result.returncode}): {output[-500:]}"
        )
        raise AudioMeasurementError(
            detail,
            failure_kind=failure_kind,
            decoded_duration_seconds=decoded_duration,
        )

    # -inf 문자열을 float('-inf') 로 변환 (Python 의 비교 연산과 호환)
    raw_value = mean_matches[-1]
    mean_volume_db = float("-inf") if raw_value == "-inf" else float(raw_value)
    return _MeasuredMeanVolume(
        mean_volume_db,
        decoded_duration_seconds=decoded_duration,
        media_diagnostic=None,
    )


def _compute_decode_timeout_seconds(
    probe_duration_s: float | None,
    *,
    base_seconds: float,
    factor: float,
    cap_seconds: float,
) -> float:
    """ffprobe hint와 설정에서 full-decode timeout을 계산한다."""
    if not math.isfinite(base_seconds) or base_seconds <= 0:
        raise ValueError("decode timeout base_seconds는 0보다 큰 유한값이어야 합니다")
    if not math.isfinite(factor) or factor < 0:
        raise ValueError("decode timeout factor는 0 이상 유한값이어야 합니다")
    if not math.isfinite(cap_seconds) or cap_seconds < base_seconds:
        raise ValueError("decode timeout cap_seconds는 base_seconds 이상이어야 합니다")

    if probe_duration_s is None:
        return cap_seconds
    if not math.isfinite(probe_duration_s) or probe_duration_s < 0:
        return cap_seconds
    return min(
        cap_seconds,
        max(base_seconds, base_seconds + probe_duration_s * factor),
    )


def _parse_ffmpeg_sample_count(output: str) -> int | None:
    """volumedetect의 마지막 ``n_samples``를 읽는다."""
    matches = re.findall(r"\bn_samples:\s*(\d+)\b", output)
    if not matches:
        return None
    return int(matches[-1])


def _find_diagnostic_marker(
    output: str,
    markers: tuple[str, ...],
    *,
    allow_path_suffix: bool = False,
) -> str | None:
    """ffmpeg 진단 line payload에서만 알려진 marker를 찾는다.

    ``Input #..., from '/path/header missing.mp3'`` 같은 파일명/메타데이터를
    진단으로 오판하지 않도록 정상 decode 출력에서는 line 시작만 인정한다.
    실패 프로세스를 분류할 때만 ``path: marker`` 형태의 정확한 suffix도 허용한다.
    """
    for raw_line in output.splitlines():
        has_leading_whitespace = bool(raw_line[:1].isspace())
        line = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", raw_line.strip().lower())
        for marker in markers:
            exact_suffixes = (f": {marker}", f": {marker}.")
            if line.startswith(marker) or (
                allow_path_suffix and not has_leading_whitespace and line.endswith(exact_suffixes)
            ):
                return marker
    return None


def _classify_process_failure(
    output: str,
    *,
    default: AudioFailureKind,
) -> AudioFailureKind:
    """프로세스 stderr의 구조화된 진단으로 실패 타입을 분류한다."""
    if (
        _find_diagnostic_marker(
            output,
            _SECURITY_DIAGNOSTIC_MARKERS,
            allow_path_suffix=True,
        )
        is not None
    ):
        return AudioFailureKind.SECURITY_BLOCKED
    if (
        _find_diagnostic_marker(
            output,
            _SOURCE_BUSY_DIAGNOSTIC_MARKERS,
            allow_path_suffix=True,
        )
        is not None
    ):
        return AudioFailureKind.SOURCE_BUSY
    if (
        _find_diagnostic_marker(
            output,
            _FFMPEG_MEDIA_DIAGNOSTIC_MARKERS,
            allow_path_suffix=True,
        )
        is not None
    ):
        return AudioFailureKind.MEDIA_INVALID
    return default


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
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=time_base,duration_ts,duration:format=duration",
                "-of",
                "json",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except subprocess.TimeoutExpired as e:
        raise AudioMeasurementError(
            f"ffprobe 타임아웃: {audio_path}",
            failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
        ) from e
    except subprocess.CalledProcessError as e:
        output = e.stderr or ""
        raise AudioMeasurementError(
            f"ffprobe 실패: {output}",
            failure_kind=_classify_process_failure(
                output,
                default=AudioFailureKind.INFRA_UNAVAILABLE,
            ),
        ) from e
    except OSError as e:
        raise AudioMeasurementError(
            f"ffprobe 실행 실패: {e}",
            failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
        ) from e

    try:
        payload = json.loads(result.stdout)
        duration_seconds = _parse_probe_duration_seconds(payload)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as e:
        raise AudioMeasurementError(
            f"duration 파싱 실패: {result.stdout!r}",
            failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
        ) from e

    return duration_seconds


def _parse_probe_duration_seconds(payload: object) -> float:
    """ffprobe JSON에서 0:a:0 duration을 rational 우선순위로 읽는다."""
    if not isinstance(payload, dict):
        raise ValueError("ffprobe JSON root가 object가 아닙니다")

    stream: dict[object, object] = {}
    streams = payload.get("streams")
    if isinstance(streams, list) and streams and isinstance(streams[0], dict):
        stream = streams[0]

    duration_ts = stream.get("duration_ts")
    time_base = stream.get("time_base")
    scalar_types = (str, int, float)
    if isinstance(duration_ts, scalar_types) and isinstance(time_base, scalar_types):
        try:
            rational_duration = Fraction(str(time_base)) * int(duration_ts)
            duration_seconds = float(rational_duration)
        except (ValueError, ZeroDivisionError):
            duration_seconds = math.nan
        if math.isfinite(duration_seconds) and duration_seconds >= 0:
            return duration_seconds

    candidates: list[object] = [stream.get("duration")]
    format_info = payload.get("format")
    if isinstance(format_info, dict):
        candidates.append(format_info.get("duration"))

    for candidate in candidates:
        if not isinstance(candidate, scalar_types):
            continue
        try:
            duration_seconds = float(candidate)
        except ValueError:
            continue
        if math.isfinite(duration_seconds) and duration_seconds >= 0:
            return duration_seconds

    raise ValueError("ffprobe duration이 없거나 유효하지 않습니다")
