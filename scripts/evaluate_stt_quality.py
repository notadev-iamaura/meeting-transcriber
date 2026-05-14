#!/usr/bin/env python3
"""전사 결과와 reference 발화 구간을 비교해 STT 품질 메트릭을 계산한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.stt_quality_metrics import (  # noqa: E402
    TimeInterval,
    calculate_temporal_coverage,
    calculate_text_error_rates,
)


def _load_json(path: Path) -> Any:
    """JSON 파일을 로드한다."""
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_records(data: Any) -> list[dict[str, Any]]:
    """지원되는 JSON 구조에서 start/end record 목록을 추출한다."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise ValueError("JSON 루트는 list 또는 object여야 합니다.")

    for key in ("speech_intervals", "reference_intervals", "segments", "utterances"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    raise ValueError(
        "speech_intervals/reference_intervals/segments/utterances 키를 찾을 수 없습니다."
    )


def _extract_intervals(data: Any) -> list[TimeInterval]:
    """지원되는 JSON 구조에서 시간 구간을 추출한다."""
    return [TimeInterval.from_mapping(record) for record in _extract_records(data)]


def _extract_text(data: Any) -> str:
    """전사 JSON에서 텍스트를 추출한다."""
    if isinstance(data, dict) and isinstance(data.get("full_text"), str):
        return data["full_text"]
    texts = []
    for record in _extract_records(data):
        text = record.get("text")
        if isinstance(text, str):
            texts.append(text)
    return " ".join(texts)


def parse_args() -> argparse.Namespace:
    """CLI 인자를 파싱한다."""
    parser = argparse.ArgumentParser(
        description="reference 발화 구간과 transcript JSON으로 STT 누락/환각 메트릭을 계산합니다."
    )
    parser.add_argument(
        "--reference-intervals",
        type=Path,
        required=True,
        help="reference 발화 구간 JSON. list 또는 speech_intervals/reference_intervals 키 지원.",
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        required=True,
        help="transcriber segments 또는 merged utterances JSON.",
    )
    parser.add_argument(
        "--reference-text",
        type=Path,
        help="선택: CER/WER 계산용 reference 텍스트 파일.",
    )
    parser.add_argument(
        "--hypothesis-text",
        type=Path,
        help="선택: CER/WER 계산용 hypothesis 텍스트 파일. 없으면 transcript JSON에서 추출.",
    )
    parser.add_argument("--output", type=Path, help="선택: 결과 JSON 저장 경로.")
    return parser.parse_args()


def main() -> None:
    """CLI 엔트리포인트."""
    args = parse_args()
    reference_data = _load_json(args.reference_intervals)
    transcript_data = _load_json(args.transcript)

    temporal = calculate_temporal_coverage(
        _extract_intervals(reference_data),
        _extract_intervals(transcript_data),
    )
    result: dict[str, Any] = {"temporal": temporal.to_dict()}

    if args.reference_text is not None:
        reference_text = args.reference_text.read_text(encoding="utf-8")
        hypothesis_text = (
            args.hypothesis_text.read_text(encoding="utf-8")
            if args.hypothesis_text is not None
            else _extract_text(transcript_data)
        )
        result["text"] = calculate_text_error_rates(reference_text, hypothesis_text).to_dict()

    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    sys.stdout.write(encoded + "\n")


if __name__ == "__main__":
    main()
