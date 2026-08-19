"""오디오 품질 검증 모듈 테스트."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core.audio_quality import (
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
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=30.0)

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
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=30.0)

    assert result.status == AudioQualityStatus.REJECT
    assert "저볼륨" in result.reason or "볼륨" in result.reason
    assert "-45" in result.reason


def test_30초_미만_오디오는_full_decode_후_reject_반환():
    """ffprobe 값은 진단용으로만 쓰고 full decode 후 29초를 거부한다."""
    fake_path = Path("/tmp/short.wav")
    with (
        patch(
            "core.audio_quality._measure_mean_volume_db",
            return_value=-25.0,
        ) as mean_mock,
        patch("core.audio_quality._measure_duration_seconds", return_value=29.0),
    ):
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=30.0)

    assert result.status == AudioQualityStatus.REJECT
    assert "짧" in result.reason
    assert result.duration_seconds == 29.0
    assert result.mean_volume_db is None
    mean_mock.assert_called_once_with(
        fake_path,
        min_decoded_duration_s=30.0,
        probe_duration_s=29.0,
        decode_timeout_base_seconds=60.0,
        decode_timeout_factor=0.25,
        decode_timeout_cap_seconds=900.0,
    )


def test_성공한_full_decode의_0샘플은_볼륨_파싱보다_먼저_reject():
    """닫힌 0초 컨테이너는 mean volume이 없어도 media-invalid로 거부한다."""
    from core.audio_quality import AudioFailureKind, AudioMeasurementError

    fake_path = Path("/tmp/empty-container.wav")
    with (
        patch("core.audio_quality._measure_duration_seconds", return_value=0.0),
        patch(
            "core.audio_quality._measure_mean_volume_db",
            side_effect=AudioMeasurementError(
                "실제 디코딩 길이가 너무 짧음",
                failure_kind=AudioFailureKind.MEDIA_INVALID,
                decoded_duration_seconds=0.0,
            ),
        ) as mean_mock,
    ):
        result = validate_audio_quality(
            fake_path,
            min_mean_db=-40.0,
            min_duration_s=30.0,
        )

    assert result.status is AudioQualityStatus.REJECT
    assert result.duration_seconds == 0.0
    assert result.quarantine_safe is True
    mean_mock.assert_called_once_with(
        fake_path,
        min_decoded_duration_s=30.0,
        probe_duration_s=0.0,
        decode_timeout_base_seconds=60.0,
        decode_timeout_factor=0.25,
        decode_timeout_cap_seconds=900.0,
    )


def test_ffmpeg_실행_실패시_error_반환():
    """볼륨 측정 AudioMeasurementError 발생 시 ERROR 상태를 반환한다."""
    from core.audio_quality import AudioMeasurementError

    fake_path = Path("/tmp/corrupt.wav")
    with (
        patch("core.audio_quality._measure_duration_seconds", return_value=60.0),
        patch(
            "core.audio_quality._measure_mean_volume_db",
            side_effect=AudioMeasurementError("ffmpeg failed"),
        ),
    ):
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=30.0)

    assert result.status == AudioQualityStatus.ERROR
    assert result.duration_seconds == 60.0
    assert "ffmpeg" in result.reason.lower() or "측정" in result.reason


@pytest.mark.parametrize("probe_kind", ["INFRA_UNAVAILABLE", "MEDIA_INVALID"])
def test_ffprobe_실패는_strict_cutoff를_위해_fail_closed(probe_kind: str):
    """probe 없이는 codec padding을 배제할 수 없으므로 ACCEPT하지 않는다."""
    from core.audio_quality import (
        AudioFailureKind,
        AudioMeasurementError,
        _MeasuredMeanVolume,
    )

    fake_path = Path("/tmp/corrupt.wav")
    with (
        patch(
            "core.audio_quality._measure_duration_seconds",
            side_effect=AudioMeasurementError(
                "ffprobe failed",
                failure_kind=getattr(AudioFailureKind, probe_kind),
            ),
        ),
        patch(
            "core.audio_quality._measure_mean_volume_db",
            return_value=_MeasuredMeanVolume(
                -25.0,
                decoded_duration_seconds=30.0,
                media_diagnostic=None,
            ),
        ) as mean_mock,
    ):
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=30.0)

    assert result.status == AudioQualityStatus.ERROR
    assert result.failure_kind is AudioFailureKind.INFRA_UNAVAILABLE
    mean_mock.assert_called_once()


def test_정확히_30초_오디오는_accept_가능():
    """최소 길이와 같은 30초 오디오는 볼륨 조건을 만족하면 통과한다."""
    fake_path = Path("/tmp/edge-duration.wav")
    with (
        patch("core.audio_quality._measure_duration_seconds", return_value=30.0),
        patch("core.audio_quality._measure_mean_volume_db", return_value=-25.0),
    ):
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=30.0)

    assert result.status == AudioQualityStatus.ACCEPT


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
    with (
        patch("core.audio_quality._measure_duration_seconds", return_value=60.0),
        patch(
            "core.audio_quality._measure_mean_volume_db",
            side_effect=RuntimeError("unexpected bug"),
        ),
    ):
        with pytest.raises(RuntimeError, match="unexpected bug"):
            validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=30.0)


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


def test_헤더보다_실제_디코딩_길이가_짧으면_error():
    """헤더는 길지만 실제 디코딩이 30초 미만인 잘린 파일을 거부한다."""
    from core.audio_quality import AudioMeasurementError, _measure_mean_volume_db

    fake_stderr = """
    Stream #0:0: Audio: pcm_s16le, 44100 Hz, mono
    [Parsed_volumedetect_0] n_samples: 24800
    [Parsed_volumedetect_0] mean_volume: -21.1 dB
    size=N/A time=00:00:01.55 bitrate=N/A speed=200x
    """

    class FakeResult:
        stderr = fake_stderr
        returncode = 0

    with (
        patch("core.audio_quality.shutil.which", return_value="/opt/homebrew/bin/ffmpeg"),
        patch("core.audio_quality.subprocess.run", return_value=FakeResult()),
        pytest.raises(AudioMeasurementError, match="실제 디코딩 길이가 너무 짧음"),
    ):
        _measure_mean_volume_db(
            Path("/tmp/truncated.mp3"),
            min_decoded_duration_s=30.0,
        )


def test_ffmpeg_길이_불일치_경고는_full_decode_성공시_accept():
    """container warning은 30초 분량의 decoded sample이 있으면 정보성이다."""
    from core.audio_quality import _measure_mean_volume_db

    fake_stderr = """
    filesize and duration do not match (growing file?)
    [Parsed_volumedetect_0] n_samples: 480000
    [Parsed_volumedetect_0] mean_volume: -21.1 dB
    size=N/A time=00:00:35.00 bitrate=N/A speed=200x
    """

    class FakeResult:
        stderr = fake_stderr
        returncode = 0

    with (
        patch("core.audio_quality.shutil.which", return_value="/opt/homebrew/bin/ffmpeg"),
        patch("core.audio_quality.subprocess.run", return_value=FakeResult()),
    ):
        measured = _measure_mean_volume_db(
            Path("/tmp/truncated.mp3"),
            min_decoded_duration_s=30.0,
        )

    assert measured == pytest.approx(-21.1)
    assert measured.decoded_duration_seconds == 30.0


def test_ffmpeg_길이_불일치_경고와_decoded_short는_reject():
    """warning 자체가 아니라 16 kHz sample count 미달을 거부 근거로 삼는다."""
    fake_stderr = """
    filesize and duration do not match (growing file?)
    [Parsed_volumedetect_0] n_samples: 479984
    [Parsed_volumedetect_0] mean_volume: -21.1 dB
    """

    class FakeResult:
        stderr = fake_stderr
        returncode = 0

    with (
        patch("core.audio_quality._measure_duration_seconds", return_value=35.0),
        patch("core.audio_quality.shutil.which", return_value="/usr/bin/ffmpeg"),
        patch("core.audio_quality.subprocess.run", return_value=FakeResult()),
    ):
        result = validate_audio_quality(
            Path("/tmp/truncated.mp3"),
            min_mean_db=-40.0,
            min_duration_s=30.0,
        )

    assert result.status is AudioQualityStatus.REJECT
    assert result.duration_seconds == pytest.approx(29.999)


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
    with (
        patch("core.audio_quality._measure_duration_seconds", return_value=900.0),
        patch(
            "core.audio_quality._measure_mean_volume_db",
            side_effect=AudioMeasurementError("no ffmpeg"),
        ),
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
        _increment_stats,
        get_validation_stats,
        reset_validation_stats,
    )

    _increment_stats("accept")
    _increment_stats("reject")
    _increment_stats("error")
    reset_validation_stats()

    assert get_validation_stats() == {"accept": 0, "reject": 0, "error": 0}


# === 30초 공통 gate fail-closed 계약 (RED) ===


def _assert_failure_contract(
    result: object,
    *,
    expected_kind: str,
    quarantine_safe: bool,
) -> None:
    """측정 실패가 타입과 격리 가능 여부를 별도로 노출하는지 검증한다."""
    from core.audio_quality import AudioFailureKind

    assert result.failure_kind is getattr(AudioFailureKind, expected_kind)
    assert result.quarantine_safe is quarantine_safe


@pytest.mark.parametrize("missing_tool", ["ffprobe", "ffmpeg"])
def test_오디오_측정_도구_부재는_infra_unavailable(missing_tool: str):
    """ffprobe/ffmpeg 미설치는 미디어 결함이 아니므로 파일을 격리하지 않는다."""
    fake_path = Path("/tmp/valid.wav")

    if missing_tool == "ffprobe":
        context = (
            patch(
                "core.audio_quality.shutil.which",
                side_effect=lambda name: None if name == "ffprobe" else f"/usr/bin/{name}",
            ),
        )
    else:
        context = (
            patch("core.audio_quality._measure_duration_seconds", return_value=30.0),
            patch(
                "core.audio_quality.shutil.which",
                side_effect=lambda name: None if name == "ffmpeg" else f"/usr/bin/{name}",
            ),
        )

    with context[0]:
        if len(context) == 1:
            result = validate_audio_quality(
                fake_path,
                min_mean_db=-40.0,
                min_duration_s=30.0,
            )
        else:
            with context[1]:
                result = validate_audio_quality(
                    fake_path,
                    min_mean_db=-40.0,
                    min_duration_s=30.0,
                )

    assert result.status == AudioQualityStatus.ERROR
    _assert_failure_contract(
        result,
        expected_kind="INFRA_UNAVAILABLE",
        quarantine_safe=False,
    )


@pytest.mark.parametrize("timeout_tool", ["ffprobe", "ffmpeg"])
def test_오디오_측정_타임아웃은_infra_unavailable(timeout_tool: str):
    """ffprobe/ffmpeg timeout은 일시적 인프라 장애이며 격리 근거가 아니다."""
    fake_path = Path("/tmp/valid.wav")
    timeout = subprocess.TimeoutExpired(cmd=timeout_tool, timeout=1)

    with patch("core.audio_quality.shutil.which", return_value=f"/usr/bin/{timeout_tool}"):
        if timeout_tool == "ffprobe":
            with patch("core.audio_quality.subprocess.run", side_effect=timeout):
                result = validate_audio_quality(
                    fake_path,
                    min_mean_db=-40.0,
                    min_duration_s=30.0,
                )
        else:
            with (
                patch(
                    "core.audio_quality._measure_duration_seconds",
                    return_value=30.0,
                ),
                patch("core.audio_quality.subprocess.run", side_effect=timeout),
            ):
                result = validate_audio_quality(
                    fake_path,
                    min_mean_db=-40.0,
                    min_duration_s=30.0,
                )

    assert result.status == AudioQualityStatus.ERROR
    _assert_failure_contract(
        result,
        expected_kind="INFRA_UNAVAILABLE",
        quarantine_safe=False,
    )


@pytest.mark.parametrize("spawn_tool", ["ffprobe", "ffmpeg"])
def test_오디오_측정_프로세스_spawn_실패는_infra_unavailable(spawn_tool: str):
    """도구 실행 자체의 OSError는 파일 결함이 아니므로 격리하지 않는다."""
    fake_path = Path("/tmp/valid.wav")

    with patch("core.audio_quality.shutil.which", return_value=f"/usr/bin/{spawn_tool}"):
        if spawn_tool == "ffprobe":
            with patch(
                "core.audio_quality.subprocess.run",
                side_effect=OSError("spawn failed"),
            ):
                result = validate_audio_quality(
                    fake_path,
                    min_mean_db=-40.0,
                    min_duration_s=30.0,
                )
        else:
            with (
                patch(
                    "core.audio_quality._measure_duration_seconds",
                    return_value=30.0,
                ),
                patch(
                    "core.audio_quality.subprocess.run",
                    side_effect=OSError("spawn failed"),
                ),
            ):
                result = validate_audio_quality(
                    fake_path,
                    min_mean_db=-40.0,
                    min_duration_s=30.0,
                )

    assert result.status == AudioQualityStatus.ERROR
    _assert_failure_contract(
        result,
        expected_kind="INFRA_UNAVAILABLE",
        quarantine_safe=False,
    )


def test_ffprobe_unknown_duration_파싱은_infra_unavailable():
    """ffprobe가 성공했어도 알 수 없는 duration은 미디어 폐기 근거가 아니다."""

    class FakeResult:
        stdout = '{"streams": [{}], "format": {"duration": "N/A"}}'
        stderr = ""
        returncode = 0

    with (
        patch("core.audio_quality.shutil.which", return_value="/usr/bin/ffprobe"),
        patch("core.audio_quality.subprocess.run", return_value=FakeResult()),
    ):
        result = validate_audio_quality(
            Path("/tmp/valid.wav"),
            min_mean_db=-40.0,
            min_duration_s=30.0,
        )

    assert result.status == AudioQualityStatus.ERROR
    _assert_failure_contract(
        result,
        expected_kind="INFRA_UNAVAILABLE",
        quarantine_safe=False,
    )


def test_ffmpeg_unknown_n_samples_파싱은_infra_unavailable():
    """progress/mean만 있고 n_samples가 없으면 추측하지 않고 INFRA로 보류한다."""

    class FakeResult:
        stderr = """
        [Parsed_volumedetect_1 @ 0x1] mean_volume: -21.1 dB
        size=N/A time=00:00:30.00 bitrate=N/A speed=200x
        """
        returncode = 0

    with (
        patch("core.audio_quality._measure_duration_seconds", return_value=30.0),
        patch("core.audio_quality.shutil.which", return_value="/usr/bin/ffmpeg"),
        patch("core.audio_quality.subprocess.run", return_value=FakeResult()),
    ):
        result = validate_audio_quality(
            Path("/tmp/valid.wav"),
            min_mean_db=-40.0,
            min_duration_s=30.0,
        )

    assert result.status == AudioQualityStatus.ERROR
    _assert_failure_contract(
        result,
        expected_kind="INFRA_UNAVAILABLE",
        quarantine_safe=False,
    )


def test_ffmpeg_io_error의_partial_short_decode는_infra로_원본_보존():
    """I/O 장애 중 일부 샘플만 읽힌 경우 짧은 미디어로 오판하지 않는다."""

    class FakeResult:
        stderr = """
        Input/output error
        [Parsed_volumedetect_1 @ 0x1] n_samples: 16000
        [Parsed_volumedetect_1 @ 0x1] mean_volume: -21.1 dB
        """
        returncode = 1

    with (
        patch("core.audio_quality._measure_duration_seconds", return_value=35.0),
        patch("core.audio_quality.shutil.which", return_value="/usr/bin/ffmpeg"),
        patch("core.audio_quality.subprocess.run", return_value=FakeResult()),
    ):
        result = validate_audio_quality(
            Path("/tmp/io-failure.wav"),
            min_mean_db=-40.0,
            min_duration_s=30.0,
        )

    assert result.status is AudioQualityStatus.ERROR
    _assert_failure_contract(
        result,
        expected_kind="INFRA_UNAVAILABLE",
        quarantine_safe=False,
    )


@pytest.mark.parametrize(
    ("diagnostic", "expected_kind"),
    [
        ("/tmp/input.wav: Device or resource busy", "SOURCE_BUSY"),
        ("/tmp/input.wav: No such file or directory", "SOURCE_BUSY"),
        ("/tmp/input.wav: Permission denied", "SECURITY_BLOCKED"),
    ],
)
def test_ffprobe_소유권_진단은_미디어_격리로_변질되지_않음(
    diagnostic: str,
    expected_kind: str,
):
    """잠긴 source와 권한 차단은 MEDIA_INVALID가 아닌 별도 소유권이다."""
    failure = subprocess.CalledProcessError(
        returncode=1,
        cmd="ffprobe",
        stderr=diagnostic,
    )
    with (
        patch("core.audio_quality.shutil.which", return_value="/usr/bin/ffprobe"),
        patch("core.audio_quality.subprocess.run", side_effect=failure),
    ):
        result = validate_audio_quality(
            Path("/tmp/input.wav"),
            min_mean_db=-40.0,
            min_duration_s=30.0,
        )

    assert result.status == AudioQualityStatus.ERROR
    _assert_failure_contract(
        result,
        expected_kind=expected_kind,
        quarantine_safe=False,
    )


def test_ffprobe_unknown_called_process_error는_infra_unavailable():
    """알려진 media 진단이 없는 ffprobe nonzero를 파일 결함으로 추측하지 않는다."""
    from core.audio_quality import _MeasuredMeanVolume

    failure = subprocess.CalledProcessError(
        returncode=70,
        cmd="ffprobe",
        stderr="ffprobe internal worker exited unexpectedly",
    )
    with (
        patch("core.audio_quality.shutil.which", return_value="/usr/bin/ffprobe"),
        patch("core.audio_quality.subprocess.run", side_effect=failure),
        patch(
            "core.audio_quality._measure_mean_volume_db",
            return_value=_MeasuredMeanVolume(
                -20.0,
                decoded_duration_seconds=30.0,
                media_diagnostic=None,
            ),
        ) as decode,
    ):
        result = validate_audio_quality(
            Path("/tmp/input.wav"),
            min_mean_db=-40.0,
            min_duration_s=30.0,
        )

    assert result.status == AudioQualityStatus.ERROR
    _assert_failure_contract(
        result,
        expected_kind="INFRA_UNAVAILABLE",
        quarantine_safe=False,
    )
    decode.assert_called_once()


def test_정책_reject는_media_invalid이고_격리_가능():
    """30초 미만 정책 거부는 안전하게 격리할 수 있는 미디어 결과다."""
    with (
        patch("core.audio_quality._measure_duration_seconds", return_value=29.0),
        patch("core.audio_quality._measure_mean_volume_db", return_value=-25.0),
    ):
        result = validate_audio_quality(
            Path("/tmp/short.wav"),
            min_mean_db=-40.0,
            min_duration_s=30.0,
        )

    assert result.status == AudioQualityStatus.REJECT
    _assert_failure_contract(
        result,
        expected_kind="MEDIA_INVALID",
        quarantine_safe=True,
    )


def test_ffmpeg_progress_time은_길이_판정에_사용하지_않음():
    """WebM preroll로 progress time이 30초 미만이어도 480000 samples면 통과한다."""
    fake_stderr = """
    [Parsed_volumedetect_1 @ 0x1] n_samples: 480000
    [Parsed_volumedetect_1 @ 0x1] mean_volume: -21.1 dB
    size=N/A time=00:00:29.98 bitrate=N/A speed=200x
    """

    class FakeResult:
        stderr = fake_stderr
        returncode = 0

    with (
        patch("core.audio_quality._measure_duration_seconds", return_value=30.008),
        patch("core.audio_quality.shutil.which", return_value="/usr/bin/ffmpeg"),
        patch("core.audio_quality.subprocess.run", return_value=FakeResult()),
    ):
        result = validate_audio_quality(
            Path("/tmp/exact-30.webm"),
            min_mean_db=-40.0,
            min_duration_s=30.0,
        )

    assert result.status == AudioQualityStatus.ACCEPT
    assert result.duration_seconds == pytest.approx(30.0)
    assert result.failure_kind is None
    assert result.quarantine_safe is False


def test_ffmpeg_full_decode는_첫_번째_오디오_스트림만_선택():
    """다중 audio input에서 자동 선택 규칙 대신 stream 0:a:0을 고정한다."""
    from core.audio_quality import _measure_mean_volume_db

    class FakeResult:
        stderr = """
        [Parsed_volumedetect_1 @ 0x1] n_samples: 480000
        [Parsed_volumedetect_1 @ 0x1] mean_volume: -21.1 dB
        """
        returncode = 0

    with (
        patch("core.audio_quality.shutil.which", return_value="/usr/bin/ffmpeg"),
        patch(
            "core.audio_quality.subprocess.run",
            return_value=FakeResult(),
        ) as run_mock,
    ):
        _measure_mean_volume_db(Path("/tmp/multi-stream.webm"))

    argv = run_mock.call_args.args[0]
    map_index = argv.index("-map")
    assert argv[map_index : map_index + 2] == ["-map", "0:a:0"]
    assert "-vn" in argv
    assert "-sn" in argv


def test_probe가_decoded보다_짧으면_보수적으로_reject():
    """encoder padding을 통과시키지 않도록 성공한 probe/decode 중 짧은 값을 쓴다."""
    fake_stderr = """
    [Parsed_volumedetect_1 @ 0x1] n_samples: 479984
    [Parsed_volumedetect_1 @ 0x1] n_samples: 480000
    [Parsed_volumedetect_1 @ 0x1] mean_volume: -21.1 dB
    size=N/A time=00:00:29.97 bitrate=N/A speed=200x
    """

    class FakeResult:
        stderr = fake_stderr
        returncode = 0

    with (
        patch("core.audio_quality._measure_duration_seconds", return_value=29.0),
        patch("core.audio_quality.shutil.which", return_value="/usr/bin/ffmpeg"),
        patch("core.audio_quality.subprocess.run", return_value=FakeResult()),
    ):
        result = validate_audio_quality(
            Path("/tmp/header-underreports.webm"),
            min_mean_db=-40.0,
            min_duration_s=30.0,
        )

    assert result.status == AudioQualityStatus.REJECT
    assert result.duration_seconds == pytest.approx(29.0)
    _assert_failure_contract(
        result,
        expected_kind="MEDIA_INVALID",
        quarantine_safe=True,
    )


def test_n_samples_479984는_29_999초로_정확히_reject():
    """16 kHz의 479984 samples는 반올림 없이 29.999초로 판정한다."""
    fake_stderr = """
    [Parsed_volumedetect_1 @ 0x1] n_samples: 479984
    [Parsed_volumedetect_1 @ 0x1] mean_volume: -21.1 dB
    size=N/A time=00:00:30.00 bitrate=N/A speed=200x
    """

    class FakeResult:
        stderr = fake_stderr
        returncode = 0

    with (
        patch("core.audio_quality._measure_duration_seconds", return_value=35.0),
        patch("core.audio_quality.shutil.which", return_value="/usr/bin/ffmpeg"),
        patch("core.audio_quality.subprocess.run", return_value=FakeResult()),
    ):
        result = validate_audio_quality(
            Path("/tmp/header-overreports.webm"),
            min_mean_db=-40.0,
            min_duration_s=30.0,
        )

    assert result.status == AudioQualityStatus.REJECT
    assert result.duration_seconds == pytest.approx(479984 / 16000)
    _assert_failure_contract(
        result,
        expected_kind="MEDIA_INVALID",
        quarantine_safe=True,
    )


def test_ffprobe_security_busy는_full_decode_전에_차단():
    """source ownership/security 진단만 ffprobe 단계에서 즉시 차단한다."""
    from core.audio_quality import AudioFailureKind, AudioMeasurementError

    for kind in (AudioFailureKind.SECURITY_BLOCKED, AudioFailureKind.SOURCE_BUSY):
        with (
            patch(
                "core.audio_quality._measure_duration_seconds",
                side_effect=AudioMeasurementError("blocked", failure_kind=kind),
            ),
            patch("core.audio_quality._measure_mean_volume_db") as decode,
        ):
            result = validate_audio_quality(
                Path("/tmp/input.wav"),
                min_mean_db=-40.0,
                min_duration_s=30.0,
            )

        assert result.status is AudioQualityStatus.ERROR
        assert result.failure_kind is kind
        decode.assert_not_called()


@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        (None, 900.0),
        (0.0, 60.0),
        (120.0, 90.0),
        (10_000.0, 900.0),
    ],
)
def test_full_decode_timeout은_probe_hint와_config로_계산(
    hint: float | None,
    expected: float,
):
    """duration을 모르면 cap, 알면 base+duration*factor를 cap 범위로 쓴다."""
    from core.audio_quality import _compute_decode_timeout_seconds

    assert (
        _compute_decode_timeout_seconds(
            hint,
            base_seconds=60.0,
            factor=0.25,
            cap_seconds=900.0,
        )
        == expected
    )


def test_full_decode는_계산된_timeout을_subprocess에_전달():
    """ffmpeg subprocess에 동적 timeout budget이 실제로 적용된다."""
    from core.audio_quality import _measure_mean_volume_db

    class FakeResult:
        stderr = """
        [Parsed_volumedetect_0] n_samples: 480000
        [Parsed_volumedetect_0] mean_volume: -21.1 dB
        """
        returncode = 0

    with (
        patch("core.audio_quality.shutil.which", return_value="/usr/bin/ffmpeg"),
        patch("core.audio_quality.subprocess.run", return_value=FakeResult()) as run,
    ):
        _measure_mean_volume_db(
            Path("/tmp/input.wav"),
            probe_duration_s=120.0,
            decode_timeout_base_seconds=60.0,
            decode_timeout_factor=0.25,
            decode_timeout_cap_seconds=900.0,
        )

    assert run.call_args.kwargs["timeout"] == 90.0


def _identity(path: Path) -> tuple[int, int, int, int, int]:
    """테스트 파일의 secure admission identity를 만든다."""
    stat_result = path.stat()
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def test_ACCEPT_cache는_secure_identity와_정책이_같을_때만_재사용(
    tmp_path: Path,
):
    """process-local cache는 ACCEPT만 identity+임계값 키로 재사용한다."""
    from core.audio_quality import _MeasuredMeanVolume, reset_audio_quality_cache

    audio_path = tmp_path / "same.wav"
    audio_path.write_bytes(b"audio")
    expected_identity = _identity(audio_path)
    reset_audio_quality_cache()

    with (
        patch("core.audio_quality._measure_duration_seconds", return_value=30.0),
        patch(
            "core.audio_quality._measure_mean_volume_db",
            return_value=_MeasuredMeanVolume(
                -20.0,
                decoded_duration_seconds=30.0,
                media_diagnostic=None,
            ),
        ) as decode,
    ):
        first = validate_audio_quality(
            audio_path,
            min_mean_db=-40.0,
            min_duration_s=30.0,
            expected_identity=expected_identity,
        )
        second = validate_audio_quality(
            audio_path,
            min_mean_db=-40.0,
            min_duration_s=30.0,
            expected_identity=expected_identity,
        )
        changed_policy = validate_audio_quality(
            audio_path,
            min_mean_db=-39.0,
            min_duration_s=30.0,
            expected_identity=expected_identity,
        )

    assert first.status is second.status is changed_policy.status is AudioQualityStatus.ACCEPT
    assert decode.call_count == 2
    reset_audio_quality_cache()


def test_REJECT와_identity_없는_ACCEPT는_cache하지_않음(tmp_path: Path):
    """비수락 결과와 secure identity 없는 호출은 항상 full decode한다."""
    from core.audio_quality import _MeasuredMeanVolume, reset_audio_quality_cache

    audio_path = tmp_path / "quiet.wav"
    audio_path.write_bytes(b"audio")
    expected_identity = _identity(audio_path)
    reset_audio_quality_cache()

    with (
        patch("core.audio_quality._measure_duration_seconds", return_value=30.0),
        patch(
            "core.audio_quality._measure_mean_volume_db",
            return_value=_MeasuredMeanVolume(
                -50.0,
                decoded_duration_seconds=30.0,
                media_diagnostic=None,
            ),
        ) as decode,
    ):
        for _ in range(2):
            result = validate_audio_quality(
                audio_path,
                min_mean_db=-40.0,
                min_duration_s=30.0,
                expected_identity=expected_identity,
            )
            assert result.status is AudioQualityStatus.REJECT
        for _ in range(2):
            validate_audio_quality(
                audio_path,
                min_mean_db=-40.0,
                min_duration_s=30.0,
            )

    assert decode.call_count == 4
    reset_audio_quality_cache()
