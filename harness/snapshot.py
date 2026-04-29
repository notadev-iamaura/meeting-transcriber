"""시각 회귀 베이스라인 + 픽셀 diff 비교.

베이스라인 PNG 캡처는 Playwright (`page.screenshot()`) 이 담당하며,
픽셀 비교는 본 모듈의 `pixel_diff_ratio()` / `assert_visual_match()` 가
Pillow + numpy 로 직접 수행한다.

(Playwright Python sync API 는 Node 의 `expect(page).to_have_screenshot()`
같은 시각 회귀 어설션을 미지원하므로 본 헬퍼가 필수.)

스펙 참조: docs/superpowers/specs/2026-04-28-ui-ux-overhaul-design.md §4.5, §5.3
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image

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
        raise ValueError(f"variant must be one of {SUPPORTED_VARIANTS}, got {variant!r}")
    return BASELINES_ROOT / f"{component}-{variant}.png"


def _sha256_of(path: Path) -> str:
    """파일의 SHA-256 hex 다이제스트."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
        raise ValueError(f"variant must be one of {SUPPORTED_VARIANTS}, got {variant!r}")
    if not path.exists():
        raise FileNotFoundError(f"baseline image not found: {path}")
    sha = _sha256_of(path)
    conn.execute(
        "INSERT INTO artifacts (ticket_id, kind, path, sha256, author_agent, created_at) "
        "VALUES (?, 'visual_baseline', ?, ?, 'designer', ?)",
        (ticket_id, str(path), sha, _now()),
    )
    conn.commit()


def pixel_diff_ratio(actual_path: Path, expected_path: Path) -> float:
    """두 PNG 사이의 픽셀 차이 비율 (0.0 = 동일, 1.0 = 모두 다름).

    Args:
        actual_path: 현재 캡처 PNG
        expected_path: 베이스라인 PNG

    Returns:
        다른 픽셀 수 / 전체 픽셀 수. 두 이미지 크기가 다르면 1.0 (비교 불가).
    """
    a = np.array(Image.open(actual_path).convert("RGB"))
    e = np.array(Image.open(expected_path).convert("RGB"))
    if a.shape != e.shape:
        return 1.0
    # 픽셀별로 RGB 채널 중 하나라도 다르면 차이 픽셀로 카운트
    differing = np.any(a != e, axis=-1).sum()
    total = a.shape[0] * a.shape[1]
    return float(differing) / float(total)


def assert_visual_match(
    actual_path: Path,
    baseline_path: Path,
    *,
    max_diff_pixel_ratio: float = 0.001,
) -> None:
    """현재 캡처를 베이스라인과 비교 — 첫 실행 시 베이스라인 자동 생성.

    Args:
        actual_path: 방금 캡처한 PNG
        baseline_path: 비교 대상 베이스라인 (없으면 actual_path 를 복사)
        max_diff_pixel_ratio: 허용 diff 비율 (기본 0.1%)

    Raises:
        AssertionError: 베이스라인 존재 시 diff > 임계 인 경우
    """
    actual_path = Path(actual_path)
    baseline_path = Path(baseline_path)
    if not baseline_path.exists():
        # 첫 실행: 베이스라인을 캡처로부터 생성하고 통과
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_bytes(actual_path.read_bytes())
        return
    ratio = pixel_diff_ratio(actual_path, baseline_path)
    if ratio > max_diff_pixel_ratio:
        raise AssertionError(
            f"visual diff {ratio:.4%} exceeds threshold {max_diff_pixel_ratio:.4%} "
            f"(actual={actual_path}, baseline={baseline_path})"
        )
