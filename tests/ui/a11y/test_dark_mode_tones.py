"""dark-mode-tones — design.md §2.2 토큰의 WCAG 색대비 자동 검증.

마크업 변경 없는 순수 토큰 단위 테스트. ui/web/style.css 의 :root 와
@media (prefers-color-scheme: dark) 또는 [data-theme="dark"] 블록에서
토큰 값 추출 후 W3C 2.x relative luminance 공식으로 직접 계산.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.ui]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
STYLE_CSS = PROJECT_ROOT / "ui" / "web" / "style.css"


def _relative_luminance(hex_color: str) -> float:
    """W3C WCAG 2.x sRGB relative luminance."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def chan(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _contrast(c1: str, c2: str) -> float:
    """WCAG 2.x contrast ratio."""
    l1, l2 = sorted(
        [_relative_luminance(c1), _relative_luminance(c2)],
        reverse=True,
    )
    return (l1 + 0.05) / (l2 + 0.05)


def _read_root_token(token_name: str) -> str:
    """style.css 의 :root 블록에서 토큰 값을 추출."""
    text = STYLE_CSS.read_text()
    root_pattern = re.compile(r":root\s*\{([^}]*)\}", re.DOTALL)
    match = root_pattern.search(text)
    assert match, ":root block not found in style.css"
    block = match.group(1)
    token_pattern = re.compile(re.escape(token_name) + r":\s*([^;]+);")
    token_match = token_pattern.search(block)
    assert token_match, f"token {token_name} not found in :root"
    return token_match.group(1).strip()


def _read_dark_token(token_name: str) -> str:
    """style.css 의 다크 모드 블록에서 토큰 값을 추출.

    @media (prefers-color-scheme: dark) 또는 [data-theme="dark"] 또는
    [data-theme='dark'] 블록 모두 시도.
    """
    text = STYLE_CSS.read_text()
    # 다크 블록 candidates — 안쪽 :root 또는 직접 selector
    patterns = [
        # @media (prefers-color-scheme: dark) { :root { ... } }
        re.compile(
            r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{[^}]*?:root\s*\{([^}]+)\}",
            re.DOTALL,
        ),
        # [data-theme="dark"] { ... }
        re.compile(
            r"\[data-theme=[\"']dark[\"']\]\s*\{([^}]+)\}",
            re.DOTALL,
        ),
    ]
    for pattern in patterns:
        for match in pattern.finditer(text):
            block = match.group(1)
            token_pattern = re.compile(re.escape(token_name) + r":\s*([^;]+);")
            token_match = token_pattern.search(block)
            if token_match:
                return token_match.group(1).strip()
    raise AssertionError(f"dark mode token {token_name} not found")


# === 라이트 모드 ===


def test_light_text_secondary_meets_aa() -> None:
    """라이트 --text-secondary on --bg-canvas (#FFFFFF) >= 4.5:1 (WCAG AA)."""
    fg = _read_root_token("--text-secondary")
    bg = _read_root_token("--bg-canvas")
    ratio = _contrast(fg, bg)
    assert ratio >= 4.5, f"--text-secondary {fg} on {bg} = {ratio:.2f}:1 (AA 4.5:1 미달)"


def test_light_text_muted_meets_ui_3_1() -> None:
    """라이트 --text-muted on --bg-canvas >= 3:1 (WCAG 1.4.11 UI 비텍스트)."""
    fg = _read_root_token("--text-muted")
    bg = _read_root_token("--bg-canvas")
    ratio = _contrast(fg, bg)
    assert ratio >= 3.0, f"--text-muted {fg} on {bg} = {ratio:.2f}:1 (UI 3:1 미달)"


def test_light_accent_text_meets_aa() -> None:
    """라이트 --accent-text on --bg-canvas >= 4.5:1 (본문 텍스트 용)."""
    fg = _read_root_token("--accent-text")
    bg = _read_root_token("--bg-canvas")
    ratio = _contrast(fg, bg)
    assert ratio >= 4.5, f"--accent-text {fg} on {bg} = {ratio:.2f}:1 (AA 4.5:1 미달)"


# === 다크 모드 ===


def test_dark_text_muted_meets_ui_3_1() -> None:
    """다크 --text-muted on dark --bg-canvas (#1C1C1E) >= 3:1."""
    fg = _read_dark_token("--text-muted")
    bg = _read_dark_token("--bg-canvas")
    ratio = _contrast(fg, bg)
    assert ratio >= 3.0, f"다크 --text-muted {fg} on {bg} = {ratio:.2f}:1 (UI 3:1 미달)"


def test_dark_accent_text_meets_aa() -> None:
    """다크 --accent-text on dark --bg-canvas >= 4.5:1."""
    fg = _read_dark_token("--accent-text")
    bg = _read_dark_token("--bg-canvas")
    ratio = _contrast(fg, bg)
    assert ratio >= 4.5, f"다크 --accent-text {fg} on {bg} = {ratio:.2f}:1 (AA 4.5:1 미달)"
