"""오디오 품질 검증 모듈 테스트."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from core.audio_quality import (
    AudioQualityResult,
    AudioQualityStatus,
    validate_audio_quality,
)


def test_정상_오디오는_accept_반환():
    """정상 볼륨(-25dB) 오디오는 ACCEPT 반환한다."""
    fake_path = Path("/tmp/normal.wav")
    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=-25.0),
        patch("core.audio_quality._measure_duration_seconds", return_value=900.0),
    ):
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)

    assert result.status == AudioQualityStatus.ACCEPT
    assert result.mean_volume_db == -25.0
    assert result.duration_seconds == 900.0
    assert result.reason == ""


def test_저볼륨_오디오는_reject_반환():
    """−45dB 오디오는 LOW_VOLUME 사유로 REJECT 반환한다."""
    fake_path = Path("/tmp/quiet.wav")
    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=-45.0),
        patch("core.audio_quality._measure_duration_seconds", return_value=1200.0),
    ):
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)

    assert result.status == AudioQualityStatus.REJECT
    assert "저볼륨" in result.reason or "볼륨" in result.reason
    assert "-45" in result.reason


def test_너무_짧은_오디오는_reject_반환():
    """3초 오디오는 TOO_SHORT 사유로 REJECT 반환한다."""
    fake_path = Path("/tmp/short.wav")
    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=-25.0),
        patch("core.audio_quality._measure_duration_seconds", return_value=3.0),
    ):
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)

    assert result.status == AudioQualityStatus.REJECT
    assert "짧" in result.reason


def test_ffmpeg_실행_실패시_error_반환():
    """ffmpeg 호출이 실패하면 ERROR 상태 반환 (REJECT 아님, 판단 보류)."""
    fake_path = Path("/tmp/corrupt.wav")
    with patch("core.audio_quality._measure_mean_volume_db", side_effect=RuntimeError("ffmpeg failed")):
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)

    assert result.status == AudioQualityStatus.ERROR
    assert "ffmpeg" in result.reason.lower() or "측정" in result.reason


def test_경계값_정확히_mean_db와_같으면_accept():
    """mean_volume이 임계값과 정확히 같으면 통과 (>= 의미론)."""
    fake_path = Path("/tmp/edge.wav")
    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=-40.0),
        patch("core.audio_quality._measure_duration_seconds", return_value=600.0),
    ):
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)

    assert result.status == AudioQualityStatus.ACCEPT
