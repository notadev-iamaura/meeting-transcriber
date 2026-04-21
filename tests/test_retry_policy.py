"""재시도 정책 예외 계층 테스트."""

from __future__ import annotations

from core.retry_policy import (
    NonRetryableError,
    RetryableError,
    TranscriptionTimeoutError,
    should_retry,
)


def test_retryable_error는_재시도_허용():
    err = RetryableError("일시적 오류")
    assert should_retry(err, attempt=1, max_attempts=3) is True


def test_nonretryable_error는_재시도_거부():
    err = NonRetryableError("구조적 오류")
    assert should_retry(err, attempt=1, max_attempts=3) is False


def test_transcription_timeout은_nonretryable():
    """타임아웃은 기본적으로 NonRetryableError의 하위 클래스다."""
    err = TranscriptionTimeoutError("전사 타임아웃 1800초")
    assert isinstance(err, NonRetryableError)
    assert should_retry(err, attempt=1, max_attempts=3) is False


def test_마지막_시도에서는_retryable이어도_재시도_거부():
    """attempt >= max_attempts면 더 이상 재시도 없음."""
    err = RetryableError("일시적 오류")
    assert should_retry(err, attempt=3, max_attempts=3) is False


def test_일반_exception은_retryable로_취급():
    """명시되지 않은 예외는 보수적으로 재시도 허용 (기존 동작 호환)."""
    err = ValueError("알 수 없는 오류")
    assert should_retry(err, attempt=1, max_attempts=3) is True
