"""
텍스트 후처리 모듈 (Text Postprocessor Module)

목적: Whisper 전사 결과의 텍스트를 정리하여 품질을 향상시킨다.
주요 기능:
    - 연속 공백 정규화 (연속 공백 → 단일 공백)
    - 앞뒤 공백/줄바꿈 정리
    - NFC 유니코드 정규화 (한글 자모 조합형 통일)
의존성: config 모듈
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

logger = logging.getLogger(__name__)


def postprocess_text(text: str) -> str:
    """텍스트를 후처리하여 정리한다.

    Args:
        text: 정리할 텍스트

    Returns:
        후처리된 텍스트
    """
    if not text:
        return text

    # 1. NFC 유니코드 정규화 (한글 자모 조합형 통일)
    result = unicodedata.normalize("NFC", text)

    # 2. 줄바꿈/탭 → 공백 변환
    result = result.replace("\n", " ").replace("\r", " ").replace("\t", " ")

    # 3. 연속 공백 → 단일 공백
    result = re.sub(r"\s{2,}", " ", result)

    # 4. 앞뒤 공백 제거
    result = result.strip()

    return result


def postprocess_segments(
    segments: list[Any],
    config: Any,
) -> list[Any]:
    """전사 세그먼트의 텍스트를 후처리한다.

    config의 text_postprocessing 설정에 따라 세그먼트 텍스트를 정리한다.
    텍스트가 비어있게 되는 세그먼트는 제거한다.

    Args:
        segments: TranscriptSegment 리스트
        config: AppConfig 인스턴스 (text_postprocessing 설정 포함)

    Returns:
        후처리된 세그먼트 리스트
    """
    pp_config = getattr(config, "text_postprocessing", None)
    if pp_config is None or not pp_config.enabled:
        return segments

    processed: list[Any] = []
    removed_count = 0

    for seg in segments:
        original_text = getattr(seg, "text", "")
        cleaned_text = postprocess_text(original_text)

        if not cleaned_text:
            removed_count += 1
            continue

        # 텍스트가 변경된 경우에만 업데이트
        if cleaned_text != original_text:
            seg.text = cleaned_text

        processed.append(seg)

    if removed_count > 0:
        logger.info(f"텍스트 후처리: {removed_count}개 빈 세그먼트 제거")

    return processed
