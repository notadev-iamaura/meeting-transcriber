"""CLI 라우팅 — 본 stub 은 Task 8 에서 argparse 기반 본 구현으로 교체된다.

현재는 `python -m harness` 호출이 ModuleNotFoundError 로 깨지지 않게 하는 placeholder.
"""
from __future__ import annotations

from harness import __version__


def main() -> None:
    """Task 8 까지의 임시 진입점 — 버전만 출력."""
    print(f"harness {__version__} — CLI not implemented yet (arrives in Task 8).")
