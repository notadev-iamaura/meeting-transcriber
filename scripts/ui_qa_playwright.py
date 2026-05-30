#!/usr/bin/env python3
"""로컬 웹 UI를 Playwright로 점검하고 스크린샷 아티팩트를 남긴다."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "ui-qa"


def _wait_for_server(base_url: str, timeout: float) -> None:
    """서버가 /api/status에 응답할 때까지 대기한다."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/status", timeout=2) as response:
                # 2xx 성공 응답이면 준비 완료로 간주 (202/204 등도 포함)
                if 200 <= response.status < 300:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
        # 성공 분기/예외 분기 모두 동일하게 폴링 간격을 둔다 (busy-loop 방지)
        time.sleep(0.5)
    raise RuntimeError(f"{base_url} 서버가 {timeout:.0f}초 안에 응답하지 않았습니다: {last_error}")


def _run_probe(base_url: str, output_dir: Path, headed: bool) -> dict[str, Any]:
    """핵심 SPA 경로를 열고 콘솔 에러와 스크린샷을 수집한다."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("playwright가 설치되어 있어야 합니다: pip install -e '.[dev]'") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    routes = [
        ("home", "/app"),
        ("settings", "/app/settings"),
        ("chat", "/app/chat"),
    ]
    viewports = [
        ("desktop", {"width": 1440, "height": 1000}),
        ("mobile", {"width": 390, "height": 844}),
    ]
    report: dict[str, Any] = {
        "base_url": base_url,
        "routes": [],
        "console_errors": [],
        "page_errors": [],
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        try:
            for viewport_name, viewport in viewports:
                context = browser.new_context(viewport=viewport)
                page = context.new_page()
                page.on("console", lambda msg: report["console_errors"].append(msg.text) if msg.type == "error" else None)
                page.on("pageerror", lambda exc: report["page_errors"].append(str(exc)))

                for route_name, route_path in routes:
                    url = f"{base_url}{route_path}"
                    page.goto(url, wait_until="domcontentloaded")
                    # SPA 라우터가 #content 를 비운 뒤 뷰를 렌더링하므로(ui/web/spa.js),
                    # 정적 마크업으로 항상 존재하는 #app/body 가 아니라 "#content 의 자식"을
                    # 기다려야 실제 하이드레이션 완료를 보장한다. 빈 렌더링 회귀를 잡는 핵심 게이트.
                    page.wait_for_selector("#content > *", timeout=10_000)
                    screenshot = output_dir / f"{viewport_name}-{route_name}.png"
                    page.screenshot(path=str(screenshot), full_page=True)
                    report["routes"].append(
                        {
                            "viewport": viewport_name,
                            "route": route_path,
                            "title": page.title(),
                            "screenshot": str(screenshot),
                        }
                    )
                context.close()
        finally:
            browser.close()

    return report


def parse_args() -> argparse.Namespace:
    """CLI 인자를 파싱한다."""
    parser = argparse.ArgumentParser(
        description=(
            "이미 실행 중인 meeting-transcriber 서버를 대상으로 핵심 UI 경로를 "
            "Playwright로 열고 스크린샷/콘솔 에러 리포트를 생성합니다."
        )
    )
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="대상 서버 URL")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--headed", action="store_true", help="브라우저 창을 표시한다")
    return parser.parse_args()


def main() -> int:
    """CLI 엔트리포인트."""
    args = parse_args()
    base_url = args.url.rstrip("/")
    _wait_for_server(base_url, args.timeout)
    report = _run_probe(base_url, args.output_dir, args.headed)
    report_path = args.output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["console_errors"] or report["page_errors"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
