"""시각 회귀 베이스라인 경로 + 아티팩트 등록.

베이스라인 PNG 캡처 자체는 Playwright (`expect(page).to_have_screenshot()`)
가 담당하며, Wave 1+ 의 tests/ui/visual/test_*.py 에서 호출된다.
본 모듈은 경로 규칙·아티팩트 등록만 책임진다.

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §4.5, §5.3
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# 지원 변종: 라이트 모드 / 다크 모드 / 모바일 (768px 이하).
SUPPORTED_VARIANTS: tuple[str, ...] = ("light", "dark", "mobile")

# 베이스라인 PNG 가 저장될 루트.
BASELINES_ROOT = Path("tests/ui/visual/baselines")


def baseline_path(component: str, variant: str) -> Path:
    """베이스라인 PNG 의 표준 경로를 반환한다.

    Args:
        component: 컴포넌트 식별자 (예: "empty-state")
        variant: light / dark / mobile 중 하나

    Returns:
        tests/ui/visual/baselines/{component}-{variant}.png
    """
    if variant not in SUPPORTED_VARIANTS:
        raise ValueError(
            f"variant must be one of {SUPPORTED_VARIANTS}, got {variant!r}"
        )
    return BASELINES_ROOT / f"{component}-{variant}.png"


def _sha256_of(path: Path) -> str:
    """파일의 SHA-256 hex 다이제스트."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def register_baseline(
    conn: sqlite3.Connection,
    *,
    ticket_id: str,
    path: Path,
    variant: str,
) -> None:
    """기존 PNG 파일을 artifacts 테이블에 visual_baseline 으로 등록한다.

    Designer 에이전트가 Playwright 로 PNG 를 저장한 뒤 이 함수를 호출한다.
    """
    if variant not in SUPPORTED_VARIANTS:
        raise ValueError(
            f"variant must be one of {SUPPORTED_VARIANTS}, got {variant!r}"
        )
    if not path.exists():
        raise FileNotFoundError(f"baseline image not found: {path}")
    sha = _sha256_of(path)
    conn.execute(
        "INSERT INTO artifacts (ticket_id, kind, path, sha256, author_agent, created_at) "
        "VALUES (?, 'visual_baseline', ?, ?, 'designer', ?)",
        (ticket_id, str(path), sha, _now()),
    )
    conn.commit()
