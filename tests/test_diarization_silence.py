"""
화자분리용 긴 무음 압축 유틸리티 테스트.

실제 ffmpeg 실행 대신 타임라인 계산과 세그먼트 시간 복원 계약을 검증한다.
"""

from pathlib import Path

from steps.diarization_silence import (
    SilenceSpan,
    build_compressed_timeline,
    build_compression_plan,
    remap_diarization_result,
)
from steps.diarizer import DiarizationResult, DiarizationSegment


def test_긴무음_중간부만_제거한_타임라인을_만든다() -> None:
    """무음 양끝은 남기고 중간부만 제거한 대응표를 생성한다."""
    timeline = build_compressed_timeline(
        original_duration=100.0,
        silences=[SilenceSpan(10.0, 40.0)],
        keep_seconds=0.75,
    )

    # 10~40초 무음에서 10.75~39.25초만 제거한다.
    assert len(timeline) == 2
    assert timeline[0].original_start == 0.0
    assert timeline[0].original_end == 10.75
    assert timeline[0].compressed_start == 0.0
    assert timeline[0].compressed_end == 10.75
    assert timeline[1].original_start == 39.25
    assert timeline[1].original_end == 100.0
    assert timeline[1].compressed_start == 10.75
    assert timeline[1].compressed_end == 71.5


def test_절약량이_충분할때만_압축계획을_적용한다() -> None:
    """절약 시간과 비율 임계값을 모두 넘을 때만 압축을 적용한다."""
    plan = build_compression_plan(
        audio_path=Path("/tmp/input.wav"),
        output_path=Path("/tmp/output.wav"),
        original_duration=100.0,
        silences=[SilenceSpan(10.0, 40.0)],
        keep_seconds=0.75,
        min_saved_seconds=20.0,
        min_saved_ratio=0.03,
    )

    assert plan.applied is True
    assert plan.audio_path == Path("/tmp/output.wav")
    assert plan.saved_seconds == 28.5
    assert plan.saved_ratio == 0.285


def test_절약량이_작으면_압축을_건너뛴다() -> None:
    """10초 이상 무음이어도 실제 절약 효과가 작으면 원본을 사용한다."""
    plan = build_compression_plan(
        audio_path=Path("/tmp/input.wav"),
        output_path=Path("/tmp/output.wav"),
        original_duration=500.0,
        silences=[SilenceSpan(100.0, 111.0)],
        keep_seconds=0.75,
        min_saved_seconds=20.0,
        min_saved_ratio=0.03,
    )

    assert plan.applied is False
    assert plan.audio_path == Path("/tmp/input.wav")
    assert plan.reason == "saved_seconds_below_threshold"
    assert plan.saved_seconds == 9.5


def test_압축_경계를_가로지르는_화자분리_세그먼트를_원본시간으로_분할복원한다() -> None:
    """압축된 타임라인의 세그먼트가 원본의 제거 구간을 건너면 안전하게 분할한다."""
    plan = build_compression_plan(
        audio_path=Path("/tmp/input.wav"),
        output_path=Path("/tmp/output.wav"),
        original_duration=100.0,
        silences=[SilenceSpan(10.0, 40.0)],
        keep_seconds=1.0,
        min_saved_seconds=20.0,
        min_saved_ratio=0.03,
    )
    compressed_result = DiarizationResult(
        segments=[
            DiarizationSegment("SPEAKER_00", 10.0, 12.0),
            DiarizationSegment("SPEAKER_01", 13.0, 15.0),
        ],
        num_speakers=2,
        audio_path="/tmp/output.wav",
        model_name="pyannote/speaker-diarization-community-1",
        output_mode="exclusive",
    )

    remapped = remap_diarization_result(
        compressed_result,
        plan,
        original_audio_path=Path("/tmp/input.wav"),
    )

    assert remapped.audio_path == "/tmp/input.wav"
    assert remapped.model_name == "pyannote/speaker-diarization-community-1"
    assert remapped.output_mode == "exclusive"
    assert remapped.segments == [
        DiarizationSegment("SPEAKER_00", 10.0, 11.0),
        DiarizationSegment("SPEAKER_00", 39.0, 40.0),
        DiarizationSegment("SPEAKER_01", 41.0, 43.0),
    ]
