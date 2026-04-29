"""tests/ui — 공용 Playwright fixture.

테스트 서버는 별도로 띄우지 않고 file:// URL 로 정적 페이지 직접 로드.
이렇게 하면 본 데모는 FastAPI 의존성 없이 동작.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def demo_swatch_url() -> str:
    """`ui/web/_demo/swatch.html` 의 file:// URL."""
    p = PROJECT_ROOT / "ui" / "web" / "_demo" / "swatch.html"
    return p.as_uri()
