"""모델 실행 없이 벤치마크 집계·해제·출력 보존 경계를 검증한다."""

from __future__ import annotations

import pytest

from scripts.benchmark_bulk_models import measure_lifecycle, text_fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle,expected_cleanups", [("retained", 1), ("reload", 3)])
async def test_lifecycle_includes_cleanup_and_total_wall(
    monkeypatch: pytest.MonkeyPatch, lifecycle: str, expected_cleanups: int
) -> None:
    """전체 벽시계는 실제 추론과 해제를 포함하고 retained만 반복 간 해제를 생략한다."""
    now = 0.0
    cleanups = 0
    monkeypatch.setattr("scripts.benchmark_bulk_models.time.perf_counter", lambda: now)

    async def operation(index: int) -> dict[str, int]:
        nonlocal now
        now += 10
        return {"value": index}

    async def cleanup() -> None:
        nonlocal now, cleanups
        cleanups += 1
        now += 2

    result = await measure_lifecycle(2, lifecycle, operation, cleanup)
    assert cleanups == expected_cleanups
    assert result["total_wall_seconds"] == 20 + 2 * expected_cleanups
    assert result["cleanup_seconds"] == 2 * expected_cleanups
    assert [r["operation_seconds"] for r in result["runs"]] == [10, 10]


@pytest.mark.asyncio
async def test_failure_still_unloads_model() -> None:
    """생성 실패를 성공 기록으로 바꾸지 않으며 모델은 반드시 해제한다."""
    cleanups = []

    async def operation(index: int) -> dict[str, int]:
        raise RuntimeError("synthetic failure")

    async def cleanup() -> None:
        cleanups.append(True)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        await measure_lifecycle(2, "retained", operation, cleanup)
    assert cleanups == [True]


def test_report_fingerprint_does_not_include_text() -> None:
    """결과 동일성은 확인할 수 있고 입력·출력 내용은 보고서에 남지 않는다."""
    result = text_fingerprint("고정 합성 입력")
    assert result == text_fingerprint("고정 합성 입력")
    assert result != text_fingerprint("다른 합성 입력")
    assert set(result) == {"chars", "sha256"}
