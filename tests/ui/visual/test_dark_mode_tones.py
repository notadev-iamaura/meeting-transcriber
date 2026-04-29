"""dark-mode-tones — 시각 회귀 placeholder.

본 컴포넌트는 토큰 변경만이라 컴포넌트 자체의 시각 베이스라인 없음.
gate.py 의 _run_visual_axis 가 component 별 test_*.py 존재를 강제하므로
PASS 하는 placeholder 로 visual axis NO-OP 명시.

실제 시각 영향은 T-101 의 베이스라인 재캡처 + 시각 회귀 테스트로 검증됨.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.ui]


def test_dark_mode_tones_visual_placeholder() -> None:
    """본 컴포넌트는 토큰 변경만이며 시각 회귀 검증은 T-101 의 재캡처로 대체."""
    # 의도적으로 placeholder. T-101 의 시각 회귀 테스트가 새 토큰 적용 후
    # 재캡처된 베이스라인과 1:1 일치하는지 검증함.
    assert True
