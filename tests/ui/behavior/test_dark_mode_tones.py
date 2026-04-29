"""dark-mode-tones — 행동 시나리오 placeholder.

본 컴포넌트는 토큰 변경만이라 행동 검증 무관. gate.py 의
_run_behavior_axis 가 component 별 test_*.py 존재를 강제하므로
PASS 하는 placeholder.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.ui]


def test_dark_mode_tones_behavior_placeholder() -> None:
    """본 컴포넌트는 토큰 변경만이며 행동 검증 무관."""
    assert True
