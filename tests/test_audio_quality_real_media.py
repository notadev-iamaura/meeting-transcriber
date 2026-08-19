"""ffmpeg으로 생성한 실제 미디어의 30초 경계 회귀 테스트."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from core.audio_quality import AudioQualityStatus, validate_audio_quality

SOURCE_SAMPLE_RATES = (8_000, 12_000, 16_000, 24_000, 48_000)
FORMAT_MATRIX = (
    ("wav", "pcm_s16le", ()),
    ("mp3", "libmp3lame", ("-b:a", "64k")),
    ("m4a", "aac", ("-b:a", "64k", "-movflags", "+faststart")),
    ("flac", "flac", ()),
    ("ogg", "vorbis", ("-strict", "-2", "-ac", "2", "-b:a", "64k")),
    ("webm", "libopus", ("-b:a", "48k")),
)


def _require_media_tools(*encoders: str) -> tuple[str, str]:
    """ffmpeg/ffprobe와 필요 인코더가 없으면 환경 의존 테스트를 건너뛴다."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("실제 미디어 테스트에 ffmpeg과 ffprobe가 필요합니다")

    if encoders:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        for encoder in encoders:
            pattern = rf"(?m)^\s*[A-Z.]{{6}}\s+{re.escape(encoder)}\s"
            if re.search(pattern, result.stdout) is None:
                pytest.skip(f"ffmpeg {encoder} 인코더가 없습니다")

    return ffmpeg, ffprobe


def _generate_tone(
    ffmpeg: str,
    output_path: Path,
    *,
    duration: str,
    sample_rate: int = 16_000,
    codec: str,
    extra_args: tuple[str, ...] = (),
) -> None:
    """정확한 source duration의 실제 인코딩 테스트 파일을 생성한다."""
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            (f"sine=frequency=1000:sample_rate={sample_rate}:duration={duration}"),
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            codec,
            *extra_args,
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


def _generate_multi_audio_webm(ffmpeg: str, output_path: Path) -> None:
    """30초 mono stream 0과 1초 stereo stream 1을 가진 WebM을 생성한다."""
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=30.000",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=1.000",
            "-map",
            "0:a:0",
            "-map",
            "1:a:0",
            "-c:a",
            "libopus",
            "-ac:a:0",
            "1",
            "-ac:a:1",
            "2",
            "-disposition:a:0",
            "0",
            "-disposition:a:1",
            "default",
            "-b:a",
            "48k",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


def _assert_media_failure(result: object) -> None:
    """미디어 결함은 인프라 장애와 달리 안전하게 격리 가능해야 한다."""
    from core.audio_quality import AudioFailureKind

    assert result.failure_kind is AudioFailureKind.MEDIA_INVALID
    assert result.quarantine_safe is True


@pytest.fixture(scope="module")
def webm_opus_matrix(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[int, str], Path]:
    """5개 source rate의 29.999/30.000/30.001초 WebM/Opus를 생성한다."""
    ffmpeg, _ = _require_media_tools("libopus")
    output_dir = tmp_path_factory.mktemp("audio-quality-webm")
    matrix: dict[tuple[int, str], Path] = {}

    for sample_rate in SOURCE_SAMPLE_RATES:
        for duration in ("29.999", "30.000", "30.001"):
            output_path = output_dir / f"source-{sample_rate}-{duration}.webm"
            _generate_tone(
                ffmpeg,
                output_path,
                duration=duration,
                sample_rate=sample_rate,
                codec="libopus",
                extra_args=("-b:a", "48k"),
            )
            matrix[(sample_rate, duration)] = output_path

    return matrix


@pytest.fixture(scope="module")
def supported_format_boundary_matrix(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[tuple[str, str], Path]:
    """지원 포맷별 29.9/30.0/30.1초 경계 파일을 생성한다."""
    encoders = tuple(encoder for _, encoder, _ in FORMAT_MATRIX)
    ffmpeg, _ = _require_media_tools(*encoders)
    output_dir = tmp_path_factory.mktemp("audio-quality-format-boundary")
    matrix: dict[tuple[str, str], Path] = {}

    for suffix, encoder, extra_args in FORMAT_MATRIX:
        for duration in ("29.999", "30.000", "30.100"):
            output_path = output_dir / f"boundary-{duration}.{suffix}"
            _generate_tone(
                ffmpeg,
                output_path,
                duration=duration,
                codec=encoder,
                extra_args=extra_args,
            )
            matrix[(suffix, duration)] = output_path

    return matrix


@pytest.mark.parametrize(("suffix", "encoder", "extra_args"), FORMAT_MATRIX)
@pytest.mark.parametrize(
    ("duration", "expected_status"),
    [
        ("29.999", AudioQualityStatus.REJECT),
        ("30.000", AudioQualityStatus.ACCEPT),
        ("30.100", AudioQualityStatus.ACCEPT),
    ],
)
def test_지원_포맷별_30초_경계(
    supported_format_boundary_matrix: dict[tuple[str, str], Path],
    suffix: str,
    encoder: str,
    extra_args: tuple[str, ...],
    duration: str,
    expected_status: AudioQualityStatus,
):
    """지원 포맷 모두 30초 미만만 거부하고 정확히 30초부터 통과시킨다."""
    del encoder, extra_args
    result = validate_audio_quality(
        supported_format_boundary_matrix[(suffix, duration)],
        min_mean_db=-40.0,
        min_duration_s=30.0,
    )

    # Vorbis granule은 16 kHz source 29.999초와 30.000초를 둘 다
    # 480000 samples로 표현한다. 저장된 파일의 effective duration은
    # 둘 다 30.000초이므로 원본 source 문자열만으로는 구분할 수 없다.
    effective_expected = (
        AudioQualityStatus.ACCEPT if suffix == "ogg" and duration == "29.999" else expected_status
    )
    assert result.status is effective_expected
    if effective_expected is AudioQualityStatus.REJECT:
        _assert_media_failure(result)
    else:
        assert result.failure_kind is None
        assert result.quarantine_safe is False


@pytest.mark.parametrize("sample_rate", SOURCE_SAMPLE_RATES)
def test_webm_opus_source_rate별_정확히_30초는_accept(
    webm_opus_matrix: dict[tuple[int, str], Path],
    sample_rate: int,
):
    """8/12/16/24/48 kHz source의 정확히 30초 Opus를 모두 통과시킨다."""
    result = validate_audio_quality(
        webm_opus_matrix[(sample_rate, "30.000")],
        min_mean_db=-40.0,
        min_duration_s=30.0,
    )

    assert result.status == AudioQualityStatus.ACCEPT
    assert result.duration_seconds == pytest.approx(30.0)
    assert result.failure_kind is None
    assert result.quarantine_safe is False


@pytest.mark.parametrize("sample_rate", SOURCE_SAMPLE_RATES)
def test_webm_opus_source_rate별_29_999초는_reject(
    webm_opus_matrix: dict[tuple[int, str], Path],
    sample_rate: int,
):
    """container padding을 제외한 29.999초 Opus는 모두 거부한다."""
    result = validate_audio_quality(
        webm_opus_matrix[(sample_rate, "29.999")],
        min_mean_db=-40.0,
        min_duration_s=30.0,
    )

    assert result.status == AudioQualityStatus.REJECT
    assert result.duration_seconds == pytest.approx(29.999)
    _assert_media_failure(result)


@pytest.mark.parametrize("sample_rate", SOURCE_SAMPLE_RATES)
def test_webm_opus_source_rate별_30_001초는_accept(
    webm_opus_matrix: dict[tuple[int, str], Path],
    sample_rate: int,
):
    """30초 경계 바로 위의 Opus를 source sample rate와 무관하게 통과시킨다."""
    result = validate_audio_quality(
        webm_opus_matrix[(sample_rate, "30.001")],
        min_mean_db=-40.0,
        min_duration_s=30.0,
    )

    assert result.status == AudioQualityStatus.ACCEPT
    assert result.duration_seconds == pytest.approx(30.001)
    assert result.failure_kind is None


def test_다중_오디오_스트림은_0_a_0의_길이로_검증(tmp_path: Path):
    """1초 stereo stream이 자동 선택되어도 30초 stream 0:a:0을 기준으로 한다."""
    ffmpeg, _ = _require_media_tools("libopus")
    audio_path = tmp_path / "multi-audio.webm"
    _generate_multi_audio_webm(ffmpeg, audio_path)

    result = validate_audio_quality(
        audio_path,
        min_mean_db=-40.0,
        min_duration_s=30.0,
    )

    assert result.status == AudioQualityStatus.ACCEPT
    assert result.duration_seconds == pytest.approx(30.0)


def test_다중_오디오_스트림_변환도_품질_gate와_같은_0_a_0을_사용(tmp_path: Path):
    """converter가 default 1초 stream 대신 gate가 승인한 30초 stream 0을 변환한다."""
    from config import AppConfig
    from steps.audio_converter import AudioConverter

    ffmpeg, _ = _require_media_tools("libopus")
    audio_path = tmp_path / "multi-audio-convert.webm"
    _generate_multi_audio_webm(ffmpeg, audio_path)

    converted_path = AudioConverter(AppConfig()).convert(audio_path, tmp_path / "converted")
    result = validate_audio_quality(
        converted_path,
        min_mean_db=-40.0,
        min_duration_s=30.0,
    )

    assert result.status is AudioQualityStatus.ACCEPT
    assert result.duration_seconds == pytest.approx(30.0)


@pytest.mark.parametrize(
    "filename",
    ["header missing.mp3", "x: header missing.mp3"],
)
def test_정상_파일명_header_missing_mp3는_accept(tmp_path: Path, filename: str):
    """파일명의 `header missing`을 ffmpeg 진단 문구로 오판하지 않는다."""
    ffmpeg, _ = _require_media_tools("libmp3lame")
    audio_path = tmp_path / filename
    _generate_tone(
        ffmpeg,
        audio_path,
        duration="1.000",
        codec="libmp3lame",
        extra_args=("-b:a", "64k"),
    )

    result = validate_audio_quality(
        audio_path,
        min_mean_db=-40.0,
        min_duration_s=0.5,
    )

    assert result.status == AudioQualityStatus.ACCEPT
    assert result.failure_kind is None
    assert result.quarantine_safe is False


def test_정상_메타데이터_header_missing은_진단으로_오판하지_않음(tmp_path: Path):
    """metadata 값이 진단 marker와 같아도 정상 미디어를 격리하지 않는다."""
    ffmpeg, _ = _require_media_tools("libmp3lame")
    audio_path = tmp_path / "metadata-marker.mp3"
    _generate_tone(
        ffmpeg,
        audio_path,
        duration="1.000",
        codec="libmp3lame",
        extra_args=("-b:a", "64k", "-metadata", "comment=header missing"),
    )

    result = validate_audio_quality(
        audio_path,
        min_mean_db=-40.0,
        min_duration_s=0.5,
    )

    assert result.status is AudioQualityStatus.ACCEPT
    assert result.failure_kind is None


def test_실제_비미디어_데이터는_media_invalid로_분류(tmp_path: Path):
    """ffprobe가 열 수 없는 실제 bytes는 격리 가능한 MEDIA_INVALID다."""
    _require_media_tools()
    invalid_path = tmp_path / "invalid.mp3"
    invalid_path.write_bytes(b"not-an-audio-stream\x00" * 128)

    result = validate_audio_quality(
        invalid_path,
        min_mean_db=-40.0,
        min_duration_s=30.0,
    )

    assert result.status == AudioQualityStatus.ERROR
    _assert_media_failure(result)


def test_닫힌_0초_wav는_media_invalid_reject(tmp_path: Path):
    """ffprobe duration이 N/A여도 full decode 0 samples로 격리 가능하게 거부한다."""
    ffmpeg, _ = _require_media_tools("pcm_s16le")
    audio_path = tmp_path / "zero-duration.wav"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            "0",
            "-c:a",
            "pcm_s16le",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )

    result = validate_audio_quality(
        audio_path,
        min_mean_db=-40.0,
        min_duration_s=30.0,
    )

    assert result.status is AudioQualityStatus.REJECT
    assert result.duration_seconds == 0.0
    _assert_media_failure(result)


def test_존재하지_않는_파일은_source_busy이고_격리하지_않음(tmp_path: Path):
    """watcher race로 사라진 source를 손상 미디어로 오판하지 않는다."""
    from core.audio_quality import AudioFailureKind

    _require_media_tools()
    missing_path = tmp_path / "missing.wav"

    result = validate_audio_quality(
        missing_path,
        min_mean_db=-40.0,
        min_duration_s=30.0,
    )

    assert result.status == AudioQualityStatus.ERROR
    assert result.failure_kind is AudioFailureKind.SOURCE_BUSY
    assert result.quarantine_safe is False


@pytest.mark.parametrize(
    ("suffix", "encoder", "truncate_bytes", "extra_args"),
    [
        (".mp3", "libmp3lame", 16_000, ("-b:a", "64k")),
        (".m4a", "aac", 18_000, ("-b:a", "64k", "-movflags", "+faststart")),
    ],
)
def test_헤더는_길지만_실제_디코딩이_짧은_파일은_reject(
    tmp_path: Path,
    suffix: str,
    encoder: str,
    truncate_bytes: int,
    extra_args: tuple[str, ...],
):
    """35초 헤더를 보존한 잘린 MP3/M4A를 실제 샘플 길이로 거부한다."""
    ffmpeg, _ = _require_media_tools(encoder)
    original_path = tmp_path / f"original{suffix}"
    truncated_path = tmp_path / f"truncated{suffix}"
    _generate_tone(
        ffmpeg,
        original_path,
        duration="35.000",
        codec=encoder,
        extra_args=extra_args,
    )
    shutil.copyfile(original_path, truncated_path)
    with truncated_path.open("r+b") as audio_file:
        audio_file.truncate(truncate_bytes)

    result = validate_audio_quality(
        truncated_path,
        min_mean_db=-40.0,
        min_duration_s=30.0,
    )

    assert result.status == AudioQualityStatus.REJECT
    assert result.duration_seconds is not None
    assert result.duration_seconds < 30.0
    _assert_media_failure(result)
