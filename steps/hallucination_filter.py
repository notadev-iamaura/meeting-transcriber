"""
환각 필터링 모듈 (Hallucination Filter Module)

목적: Whisper 전사 결과에서 환각(hallucination) 세그먼트를 감지하고 제거한다.
주요 기능:
    - compression_ratio 기반 환각 감지 (비정상적으로 반복적인 텍스트)
    - avg_logprob 기반 저신뢰도 세그먼트 경고
    - no_speech_prob 기반 무음 세그먼트 제거
    - 반복 패턴 감지 (동일 문자열 연속 반복)
의존성: config 모듈, steps/transcriber (TranscriptSegment, TranscriptResult)
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def detect_repetition(text: str, threshold: int = 3) -> bool:
    """텍스트에서 반복 패턴을 감지한다.

    동일한 단어나 구(phrase)가 threshold회 이상 연속 반복되면
    환각으로 판정한다.

    Args:
        text: 검사할 텍스트
        threshold: 반복 횟수 임계값 (기본 3)

    Returns:
        반복 패턴이 감지되면 True
    """
    if not text or len(text) < 2:
        return False

    # 1~20자 길이의 임의 패턴이 threshold회 이상 연속 반복되는지 검사
    # (.{1,20})\1{threshold-1,} 형태로 텍스트 내 어디서든 반복 감지
    for pattern_len in range(1, min(21, len(text) // threshold + 1)):
        regex = f"(.{{{pattern_len}}})" + f"\\1{{{threshold - 1},}}"
        if re.search(regex, text):
            return True

    # 공백 기준 단어 반복 검사
    words = text.split()
    if len(words) >= threshold:
        for i in range(len(words) - threshold + 1):
            window = words[i : i + threshold]
            if len(set(window)) == 1:
                return True

    return False


def filter_hallucinations(
    segments: list[Any],
    config: Any,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """전사 세그먼트에서 환각을 필터링한다.

    config의 hallucination_filter 설정에 따라 세그먼트를 검사하고,
    환각으로 판정된 세그먼트를 제거한다.

    Args:
        segments: TranscriptSegment 리스트
        config: AppConfig 인스턴스 (hallucination_filter 설정 포함)

    Returns:
        (필터링된 세그먼트 리스트, 제거된 세그먼트 정보 리스트) 튜플
    """
    filter_config = getattr(config, "hallucination_filter", None)
    if filter_config is None or not filter_config.enabled:
        return segments, []

    filtered: list[Any] = []
    removed: list[dict[str, Any]] = []

    for seg in segments:
        removal_reason = _check_segment(seg, filter_config)
        if removal_reason:
            removed.append({
                "text": getattr(seg, "text", ""),
                "start": getattr(seg, "start", 0.0),
                "end": getattr(seg, "end", 0.0),
                "reason": removal_reason,
            })
            logger.warning(
                f"환각 세그먼트 제거: [{getattr(seg, 'start', 0.0):.1f}"
                f"~{getattr(seg, 'end', 0.0):.1f}s] "
                f"사유={removal_reason}, "
                f"텍스트=\"{getattr(seg, 'text', '')[:50]}\""
            )
        else:
            filtered.append(seg)

    # 크로스 세그먼트 반복 제거: 연속으로 동일 텍스트가 N회 이상 등장하면 환각
    filtered, cross_removed = _remove_cross_segment_repetitions(
        filtered, filter_config.repetition_threshold
    )
    removed.extend(cross_removed)

    if removed:
        logger.info(
            f"환각 필터링 완료: {len(removed)}개 제거, "
            f"{len(filtered)}개 유지 (전체 {len(segments)}개)"
        )

    return filtered, removed


def _remove_cross_segment_repetitions(
    segments: list[Any],
    threshold: int,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """연속으로 동일한 텍스트가 반복되는 세그먼트를 제거한다.

    Whisper가 침묵 구간에서 동일 문장을 수십 번 반복 생성하는 환각 패턴을 잡는다.
    연속 N개 이상 동일 텍스트(정규화 기준)가 등장하면 해당 연속 구간 전체를 제거한다.

    Args:
        segments: 세그먼트 리스트
        threshold: 연속 반복 임계값 (이 수 이상이면 제거)

    Returns:
        (정리된 세그먼트 리스트, 제거된 세그먼트 정보 리스트) 튜플
    """
    if not segments or threshold < 2:
        return segments, []

    def _normalize(text: str) -> str:
        return " ".join(text.strip().split())

    result: list[Any] = []
    removed: list[dict[str, Any]] = []
    i = 0

    while i < len(segments):
        current_text = _normalize(getattr(segments[i], "text", ""))
        run_end = i + 1
        while run_end < len(segments) and _normalize(getattr(segments[run_end], "text", "")) == current_text:
            run_end += 1

        run_length = run_end - i
        if run_length >= threshold:
            # 연속 반복 구간 전체 제거
            for seg in segments[i:run_end]:
                removed.append({
                    "text": getattr(seg, "text", ""),
                    "start": getattr(seg, "start", 0.0),
                    "end": getattr(seg, "end", 0.0),
                    "reason": f"cross_segment_repetition(run={run_length}>=threshold={threshold})",
                })
            logger.warning(
                f"크로스 세그먼트 반복 제거: {run_length}회 연속 동일 텍스트 "
                f"[{getattr(segments[i], 'start', 0.0):.1f}"
                f"~{getattr(segments[run_end-1], 'end', 0.0):.1f}s] "
                f"\"{current_text[:50]}\""
            )
        else:
            result.extend(segments[i:run_end])

        i = run_end

    return result, removed


def _check_segment(seg: Any, filter_config: Any) -> str | None:
    """단일 세그먼트의 환각 여부를 검사한다.

    Args:
        seg: TranscriptSegment 인스턴스
        filter_config: HallucinationFilterConfig 인스턴스

    Returns:
        환각 사유 문자열. 정상이면 None.
    """
    # 1. no_speech_prob 검사: 무음 확률이 높으면 제거
    no_speech_prob = getattr(seg, "no_speech_prob", 0.0)
    if no_speech_prob > filter_config.no_speech_threshold:
        return f"no_speech_prob={no_speech_prob:.3f}>{filter_config.no_speech_threshold}"

    # 2. avg_logprob 검사: 신뢰도가 매우 낮으면 제거
    avg_logprob = getattr(seg, "avg_logprob", 0.0)
    if avg_logprob < filter_config.logprob_threshold:
        return f"avg_logprob={avg_logprob:.3f}<{filter_config.logprob_threshold}"

    # 3. compression_ratio 검사: 비정상적 반복 텍스트 감지
    compression_ratio = getattr(seg, "compression_ratio", 0.0)
    if compression_ratio > filter_config.compression_ratio_threshold:
        return (
            f"compression_ratio={compression_ratio:.2f}"
            f">{filter_config.compression_ratio_threshold}"
        )

    # 4. 반복 패턴 검사
    text = getattr(seg, "text", "")
    if detect_repetition(text, filter_config.repetition_threshold):
        return f"repetition_detected(threshold={filter_config.repetition_threshold})"

    return None
