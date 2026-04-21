"""
재시도 정책 예외 계층

목적: Phase 1에서 타임아웃 후 재시도가 MLX Metal 크래시를 유발하는 문제를 차단.
     재시도 가능한 오류(RetryableError)와 구조적 오류(NonRetryableError)를
     분리하여 후자는 즉시 실패 처리한다.

근거: 2026-04-21 meeting_20260420_100536.wav 크래시 분석 — 타임아웃 후
     재시도 과정에서 MLX Metal 상태가 오염된 채 모델 재로드되어 SIGSEGV 발생.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class RetryableError(Exception):
    """재시도로 복구 가능한 일시적 오류."""


class NonRetryableError(Exception):
    """구조적·결정적 오류로 재시도가 무의미하거나 위험한 경우.

    예: 전사 타임아웃(디코더 루프), 모델 로드 실패(메모리 부족),
        입력 파일 형식 오류 등.
    """


class TranscriptionTimeoutError(NonRetryableError):
    """전사 단계 타임아웃. MLX Metal 상태 오염 방지를 위해 재시도 금지."""


def should_retry(
    error: BaseException,
    *,
    attempt: int,
    max_attempts: int,
) -> bool:
    """해당 예외에 대해 재시도를 수행해야 하는지 판정한다.

    Args:
        error: 발생한 예외
        attempt: 현재 시도 번호 (1부터)
        max_attempts: 최대 시도 횟수

    Returns:
        재시도 가능 여부. NonRetryableError 계열은 항상 False.
    """
    if isinstance(error, NonRetryableError):
        logger.info(f"NonRetryableError 감지 — 재시도 중단: {type(error).__name__}: {error}")
        return False
    return not attempt >= max_attempts
