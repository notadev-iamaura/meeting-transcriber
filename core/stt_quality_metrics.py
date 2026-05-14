"""STT 품질 평가용 순수 메트릭.

모델 실행이나 오디오 디코딩 없이 reference 발화 구간과 전사 구간을 비교해
누락률, 커버리지, 환각성 시간 비율, CER/WER를 계산한다.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TimeInterval:
    """초 단위 시간 구간."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        """구간 길이를 초 단위로 반환한다."""
        return max(0.0, self.end - self.start)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> TimeInterval:
        """start/end 키를 가진 mapping에서 시간 구간을 만든다."""
        return cls(start=float(data["start"]), end=float(data["end"]))


@dataclass(frozen=True)
class TemporalCoverageMetrics:
    """reference 발화 구간 대비 전사 시간 커버리지 지표."""

    reference_speech_seconds: float
    transcript_seconds: float
    covered_reference_seconds: float
    missing_reference_seconds: float
    extra_transcript_seconds: float
    coverage_rate: float
    omission_rate: float
    hallucination_time_rate: float

    def to_dict(self) -> dict[str, float]:
        """JSON 직렬화 가능한 dict로 변환한다."""
        return {
            "reference_speech_seconds": round(self.reference_speech_seconds, 3),
            "transcript_seconds": round(self.transcript_seconds, 3),
            "covered_reference_seconds": round(self.covered_reference_seconds, 3),
            "missing_reference_seconds": round(self.missing_reference_seconds, 3),
            "extra_transcript_seconds": round(self.extra_transcript_seconds, 3),
            "coverage_rate": round(self.coverage_rate, 6),
            "omission_rate": round(self.omission_rate, 6),
            "hallucination_time_rate": round(self.hallucination_time_rate, 6),
        }


@dataclass(frozen=True)
class TextErrorMetrics:
    """reference text 대비 hypothesis text 오류율."""

    cer: float
    wer: float
    reference_chars: int
    hypothesis_chars: int
    reference_words: int
    hypothesis_words: int

    def to_dict(self) -> dict[str, float | int]:
        """JSON 직렬화 가능한 dict로 변환한다."""
        return {
            "cer": round(self.cer, 6),
            "wer": round(self.wer, 6),
            "reference_chars": self.reference_chars,
            "hypothesis_chars": self.hypothesis_chars,
            "reference_words": self.reference_words,
            "hypothesis_words": self.hypothesis_words,
        }


def merge_intervals(intervals: Iterable[TimeInterval]) -> list[TimeInterval]:
    """겹치거나 맞닿은 시간 구간을 병합한다."""
    ordered = sorted((i for i in intervals if i.end > i.start), key=lambda i: (i.start, i.end))
    if not ordered:
        return []

    merged = [ordered[0]]
    for current in ordered[1:]:
        previous = merged[-1]
        if current.start <= previous.end:
            merged[-1] = TimeInterval(previous.start, max(previous.end, current.end))
        else:
            merged.append(current)
    return merged


def total_duration(intervals: Iterable[TimeInterval]) -> float:
    """병합된 구간들의 총 길이를 반환한다."""
    return sum(interval.duration for interval in merge_intervals(intervals))


def overlap_duration(
    left_intervals: Iterable[TimeInterval],
    right_intervals: Iterable[TimeInterval],
) -> float:
    """두 구간 집합의 총 겹침 시간을 계산한다."""
    left = merge_intervals(left_intervals)
    right = merge_intervals(right_intervals)
    i = 0
    j = 0
    overlap = 0.0

    while i < len(left) and j < len(right):
        a = left[i]
        b = right[j]
        overlap += max(0.0, min(a.end, b.end) - max(a.start, b.start))
        if a.end < b.end:
            i += 1
        else:
            j += 1

    return overlap


def calculate_temporal_coverage(
    reference_intervals: Iterable[TimeInterval],
    transcript_intervals: Iterable[TimeInterval],
) -> TemporalCoverageMetrics:
    """reference 발화 구간과 전사 구간의 시간 기반 품질 지표를 계산한다."""
    references = merge_intervals(reference_intervals)
    transcripts = merge_intervals(transcript_intervals)

    reference_seconds = total_duration(references)
    transcript_seconds = total_duration(transcripts)
    covered_seconds = overlap_duration(references, transcripts)
    missing_seconds = max(0.0, reference_seconds - covered_seconds)
    extra_seconds = max(0.0, transcript_seconds - covered_seconds)

    coverage_rate = covered_seconds / reference_seconds if reference_seconds > 0 else 1.0
    omission_rate = missing_seconds / reference_seconds if reference_seconds > 0 else 0.0
    hallucination_time_rate = extra_seconds / transcript_seconds if transcript_seconds > 0 else 0.0

    return TemporalCoverageMetrics(
        reference_speech_seconds=reference_seconds,
        transcript_seconds=transcript_seconds,
        covered_reference_seconds=covered_seconds,
        missing_reference_seconds=missing_seconds,
        extra_transcript_seconds=extra_seconds,
        coverage_rate=coverage_rate,
        omission_rate=omission_rate,
        hallucination_time_rate=hallucination_time_rate,
    )


def normalize_text_for_error_rate(text: str) -> str:
    """CER/WER 계산 전 한국어 텍스트를 보수적으로 정규화한다."""
    normalized = unicodedata.normalize("NFC", text).lower()
    normalized = re.sub(r"[^\w\s가-힣]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _levenshtein_distance(left: list[str], right: list[str]) -> int:
    """두 token sequence의 Levenshtein distance를 계산한다."""
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for i, left_token in enumerate(left, start=1):
        current = [i]
        for j, right_token in enumerate(right, start=1):
            cost = 0 if left_token == right_token else 1
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + cost,
                )
            )
        previous = current
    return previous[-1]


def calculate_text_error_rates(reference_text: str, hypothesis_text: str) -> TextErrorMetrics:
    """정규화된 reference/hypothesis 텍스트의 CER/WER를 계산한다."""
    ref = normalize_text_for_error_rate(reference_text)
    hyp = normalize_text_for_error_rate(hypothesis_text)

    ref_chars = list(ref.replace(" ", ""))
    hyp_chars = list(hyp.replace(" ", ""))
    ref_words = ref.split() if ref else []
    hyp_words = hyp.split() if hyp else []

    cer = (
        _levenshtein_distance(ref_chars, hyp_chars) / len(ref_chars)
        if ref_chars
        else (0.0 if not hyp_chars else 1.0)
    )
    wer = (
        _levenshtein_distance(ref_words, hyp_words) / len(ref_words)
        if ref_words
        else (0.0 if not hyp_words else 1.0)
    )

    return TextErrorMetrics(
        cer=cer,
        wer=wer,
        reference_chars=len(ref_chars),
        hypothesis_chars=len(hyp_chars),
        reference_words=len(ref_words),
        hypothesis_words=len(hyp_words),
    )
