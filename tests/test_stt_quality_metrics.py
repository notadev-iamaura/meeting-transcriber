"""STT 품질 메트릭 테스트."""

from __future__ import annotations

import pytest

from core.stt_quality_metrics import (
    TimeInterval,
    calculate_temporal_coverage,
    calculate_text_error_rates,
    merge_intervals,
    overlap_duration,
)


def test_merge_intervals_merges_overlap_and_touching_ranges() -> None:
    """겹치거나 맞닿은 구간은 하나로 병합한다."""
    intervals = [
        TimeInterval(5.0, 8.0),
        TimeInterval(0.0, 2.0),
        TimeInterval(2.0, 4.0),
        TimeInterval(7.0, 9.0),
    ]

    assert merge_intervals(intervals) == [
        TimeInterval(0.0, 4.0),
        TimeInterval(5.0, 9.0),
    ]


def test_overlap_duration_uses_merged_intervals() -> None:
    """겹침 시간은 양쪽 구간 병합 후 계산한다."""
    left = [TimeInterval(0.0, 5.0), TimeInterval(4.0, 10.0)]
    right = [TimeInterval(2.0, 3.0), TimeInterval(8.0, 12.0)]

    assert overlap_duration(left, right) == pytest.approx(3.0)


def test_calculate_temporal_coverage_reports_omission_and_hallucination_time() -> None:
    """reference 대비 누락률과 추가 전사 시간을 계산한다."""
    reference = [TimeInterval(0.0, 10.0), TimeInterval(20.0, 30.0)]
    transcript = [TimeInterval(2.0, 8.0), TimeInterval(18.0, 24.0), TimeInterval(40.0, 45.0)]

    metrics = calculate_temporal_coverage(reference, transcript)

    assert metrics.reference_speech_seconds == pytest.approx(20.0)
    assert metrics.transcript_seconds == pytest.approx(17.0)
    assert metrics.covered_reference_seconds == pytest.approx(10.0)
    assert metrics.missing_reference_seconds == pytest.approx(10.0)
    assert metrics.extra_transcript_seconds == pytest.approx(7.0)
    assert metrics.coverage_rate == pytest.approx(0.5)
    assert metrics.omission_rate == pytest.approx(0.5)
    assert metrics.hallucination_time_rate == pytest.approx(7.0 / 17.0)


def test_text_error_rates_normalize_korean_punctuation() -> None:
    """구두점/공백 차이는 정규화해 CER/WER에 반영하지 않는다."""
    metrics = calculate_text_error_rates(
        "안녕하세요, 회의 시작합니다.", "안녕하세요 회의 시작합니다"
    )

    assert metrics.cer == pytest.approx(0.0)
    assert metrics.wer == pytest.approx(0.0)


def test_text_error_rates_count_character_substitution() -> None:
    """한 글자 치환은 CER에 반영된다."""
    metrics = calculate_text_error_rates("가나다라마", "가나다바마")

    assert metrics.cer == pytest.approx(0.2)
    assert metrics.wer == pytest.approx(1.0)
