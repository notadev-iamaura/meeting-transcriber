"""native diagnostic gate용 preflight smoke 테스트."""

from __future__ import annotations

import pytest

from core.preflight import PreflightResult, reset_preflight_cache, run_preflight

pytestmark = pytest.mark.native


def test_native_preflight_smoke_returns_consistent_result() -> None:
    """실제 환경 preflight가 프로세스 abort 없이 일관된 결과를 반환한다.

    이 테스트는 본 프로세스에서 `mlx.core`를 직접 import하지 않는다.
    `run_preflight()` 내부의 subprocess 검증 경로만 사용해 native/Metal 환경
    문제를 diagnostic gate에서 탐지한다.
    """
    reset_preflight_cache()

    result = run_preflight(force=True)

    assert isinstance(result, PreflightResult)
    assert result.can_use_chromadb is result.python_compatible
    assert result.can_use_mlx is (
        result.is_apple_silicon and result.metal_available and result.mlx_importable
    )
    assert isinstance(result.warnings, tuple)
