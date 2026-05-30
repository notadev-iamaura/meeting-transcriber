"""오프라인 품질 eval 골든 케이스.

실제 STT/LLM 모델을 호출하지 않고, 품질 회귀를 감지할 수 있는 계약을 검증한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.stt_quality_metrics import (
    TimeInterval,
    calculate_temporal_coverage,
    calculate_text_error_rates,
)
from search.chat import _build_context_text, _build_user_prompt
from search.hybrid_search import SearchResult
from steps.corrector import CorrectedUtterance
from steps.hallucination_filter import filter_hallucinations
from steps.summarizer import _build_fallback_markdown

CASES_PATH = Path(__file__).resolve().parent.parent / "evals" / "quality_golden_cases.json"


@dataclass
class _MockSegment:
    """환각 필터 eval용 세그먼트."""

    text: str
    start: float = 0.0
    end: float = 1.0
    avg_logprob: float = -0.5
    no_speech_prob: float = 0.1
    compression_ratio: float = 1.0


@dataclass
class _MockFilterConfig:
    """환각 필터 eval용 설정."""

    enabled: bool = True
    compression_ratio_threshold: float = 2.4
    logprob_threshold: float = -1.0
    no_speech_threshold: float = 0.6
    repetition_threshold: int = 3


def _load_cases() -> dict[str, Any]:
    """골든 케이스 JSON을 로드한다."""
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _intervals(raw: list[list[float]]) -> list[TimeInterval]:
    """JSON 구간 배열을 TimeInterval 목록으로 변환한다."""
    return [TimeInterval(start=start, end=end) for start, end in raw]


@pytest.mark.parametrize("case", _load_cases()["stt_text_cases"], ids=lambda c: c["id"])
def test_stt_text_quality_golden_cases(case: dict[str, Any]) -> None:
    """STT 텍스트 메트릭의 기본 정규화와 고유명사 보존 계약을 검증한다."""
    metrics = calculate_text_error_rates(case["reference"], case["hypothesis"])

    assert metrics.cer <= case["max_cer"]
    assert metrics.wer <= case["max_wer"]


@pytest.mark.parametrize("case", _load_cases()["temporal_cases"], ids=lambda c: c["id"])
def test_temporal_coverage_golden_cases(case: dict[str, Any]) -> None:
    """VAD/세그먼트 시간 품질의 누락 및 환각 예산을 검증한다."""
    metrics = calculate_temporal_coverage(
        _intervals(case["reference_intervals"]),
        _intervals(case["transcript_intervals"]),
    )

    assert metrics.coverage_rate >= case["min_coverage_rate"]
    assert metrics.hallucination_time_rate <= case["max_hallucination_time_rate"]


@pytest.mark.parametrize("case", _load_cases()["hallucination_cases"], ids=lambda c: c["id"])
def test_hallucination_filter_golden_cases(case: dict[str, Any]) -> None:
    """무음 반복 환각 세그먼트가 제거되는지 검증한다."""
    segments = [_MockSegment(**segment) for segment in case["segments"]]
    config = MagicMock()
    config.hallucination_filter = _MockFilterConfig()

    _, removed = filter_hallucinations(segments, config)

    assert len(removed) == case["expected_removed"]


@pytest.mark.parametrize("case", _load_cases()["summary_contract_cases"], ids=lambda c: c["id"])
def test_summary_contract_golden_cases(case: dict[str, Any]) -> None:
    """LLM 실패 폴백 회의록이 원문과 화자 라벨을 보존하는지 검증한다."""
    utterances = [
        CorrectedUtterance(
            text=item["text"],
            original_text=item["text"],
            speaker=item["speaker"],
            start=float(index),
            end=float(index + 1),
            was_corrected=False,
        )
        for index, item in enumerate(case["utterances"])
    ]

    markdown = _build_fallback_markdown(utterances, case["speakers"])

    for expected in case["must_contain"]:
        assert expected in markdown


@pytest.mark.parametrize("case", _load_cases()["rag_prompt_cases"], ids=lambda c: c["id"])
def test_rag_prompt_contract_golden_cases(case: dict[str, Any]) -> None:
    """RAG 프롬프트가 출처 메타데이터와 검색 내용을 유지하는지 검증한다."""
    result = SearchResult(**case["result"])
    context = _build_context_text([result])
    prompt = _build_user_prompt(case["query"], context)

    for expected in case["must_contain"]:
        assert expected in prompt
