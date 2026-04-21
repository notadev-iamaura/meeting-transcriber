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
    """AudioMeasurementError 발생 시 ERROR 상태 반환 (REJECT 아님, 판단 보류)."""
    from core.audio_quality import AudioMeasurementError

    fake_path = Path("/tmp/corrupt.wav")
    with patch(
        "core.audio_quality._measure_mean_volume_db",
        side_effect=AudioMeasurementError("ffmpeg failed"),
    ):
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


# === Phase 1 Cleanup (2026-04-21): 견고성 개선 테스트 ===


def test_예상치_못한_RuntimeError는_전파됨():
    """Phase 1 Cleanup (I2): AudioMeasurementError 외 예외는 fail-fast 전파.

    의도적으로 좁힌 except 로 인해 내부 버그가 ERROR 상태로 은폐되지 않는다.
    """
    fake_path = Path("/tmp/bug.wav")
    with patch(
        "core.audio_quality._measure_mean_volume_db",
        side_effect=RuntimeError("unexpected bug"),
    ):
        with pytest.raises(RuntimeError, match="unexpected bug"):
            validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)


def test_완전_무음_파일은_reject_반환():
    """Phase 1 Cleanup (M2): ffmpeg의 `mean_volume: -inf dB` 출력을 REJECT로 처리.

    _measure_mean_volume_db 가 float('-inf') 반환 시, -inf < -40.0 비교가 True이므로
    자연스럽게 REJECT 경로를 타야 한다.
    """
    fake_path = Path("/tmp/silent.wav")
    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=float("-inf")),
        patch("core.audio_quality._measure_duration_seconds", return_value=600.0),
    ):
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)

    assert result.status == AudioQualityStatus.REJECT
    assert result.mean_volume_db == float("-inf")


def test_mean_volume_정규식이_inf_값을_파싱():
    """Phase 1 Cleanup (M2): 정규식이 `-inf dB` 토큰을 매치하고 float('-inf') 반환."""
    from core.audio_quality import _measure_mean_volume_db

    fake_stderr = """
    [Parsed_volumedetect_0 @ 0x7f] n_samples: 1
    [Parsed_volumedetect_0 @ 0x7f] mean_volume: -inf dB
    [Parsed_volumedetect_0 @ 0x7f] max_volume: -inf dB
    """

    class FakeResult:
        stderr = fake_stderr
        returncode = 0

    with (
        patch("core.audio_quality.shutil.which", return_value="/opt/homebrew/bin/ffmpeg"),
        patch("core.audio_quality.subprocess.run", return_value=FakeResult()),
    ):
        assert _measure_mean_volume_db(Path("/tmp/x.wav")) == float("-inf")


def test_ffmpeg_returncode_nonzero와_파싱_실패_구분():
    """Phase 1 Cleanup (I4): returncode != 0 인 경우 더 명확한 에러 메시지."""
    from core.audio_quality import AudioMeasurementError, _measure_mean_volume_db

    fake_stderr = "File not found: /tmp/missing.wav"  # 파싱 실패 + 실패 종료

    class FakeResult:
        stderr = fake_stderr
        returncode = 1

    with (
        patch("core.audio_quality.shutil.which", return_value="/opt/homebrew/bin/ffmpeg"),
        patch("core.audio_quality.subprocess.run", return_value=FakeResult()),
        pytest.raises(AudioMeasurementError, match=r"returncode=1"),
    ):
        _measure_mean_volume_db(Path("/tmp/missing.wav"))


# === Phase 1 Cleanup P2a: 관찰성 카운터 ===


def test_카운터_초기값은_0():
    from core.audio_quality import get_validation_stats, reset_validation_stats

    reset_validation_stats()
    stats = get_validation_stats()
    assert stats == {"accept": 0, "reject": 0, "error": 0}


def test_ACCEPT_REJECT_ERROR_각각_카운터_증가():
    from core.audio_quality import (
        AudioMeasurementError,
        get_validation_stats,
        reset_validation_stats,
    )

    reset_validation_stats()
    fake_path = Path("/tmp/x.wav")

    # ACCEPT 2회
    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=-25.0),
        patch("core.audio_quality._measure_duration_seconds", return_value=900.0),
    ):
        validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)
        validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)

    # REJECT 1회 (저볼륨)
    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=-50.0),
        patch("core.audio_quality._measure_duration_seconds", return_value=900.0),
    ):
        validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)

    # ERROR 1회
    with patch(
        "core.audio_quality._measure_mean_volume_db",
        side_effect=AudioMeasurementError("no ffmpeg"),
    ):
        validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)

    stats = get_validation_stats()
    assert stats == {"accept": 2, "reject": 1, "error": 1}


def test_get_validation_stats_는_복사본_반환():
    """반환된 dict 를 변경해도 내부 카운터에 영향 없어야 한다."""
    from core.audio_quality import get_validation_stats, reset_validation_stats

    reset_validation_stats()
    snapshot = get_validation_stats()
    snapshot["accept"] = 9999
    assert get_validation_stats()["accept"] == 0


def test_reset_validation_stats_는_모든_키_초기화():
    from core.audio_quality import (
        get_validation_stats,
        reset_validation_stats,
        _increment_stats,
    )

    _increment_stats("accept")
    _increment_stats("reject")
    _increment_stats("error")
    reset_validation_stats()

    assert get_validation_stats() == {"accept": 0, "reject": 0, "error": 0}
