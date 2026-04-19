"""
네이티브 창 모듈 (Native Window Module)

목적: PyWebView를 사용하여 네이티브 macOS 창에서 웹 UI를 표시한다.
주요 기능:
    - NativeWindowConfig: 창 설정을 담는 불변 데이터클래스
    - build_window_config: 서버 정보로부터 창 설정 생성
    - build_subprocess_args: 서브프로세스 실행 인자 구성
    - launch_native_window: 서브프로세스로 네이티브 창 실행
    - run_webview_window: webview 창 생성 및 표시 (서브프로세스 엔트리)
의존성: subprocess, sys, argparse, webview (pywebview)
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NativeWindowConfig:
    """네이티브 창 실행에 필요한 불변 설정.

    Attributes:
        url: 표시할 웹 페이지 URL
        title: 창 제목
        width: 초기 창 너비 (px)
        height: 초기 창 높이 (px)
        min_width: 최소 창 너비 (px)
        min_height: 최소 창 높이 (px)
    """

    url: str
    title: str = "Recap"
    width: int = 1200
    height: int = 800
    min_width: int = 800
    min_height: int = 600


def build_window_config(
    host: str,
    port: int,
    title: str,
    width: int,
    height: int,
    min_width: int,
    min_height: int,
) -> NativeWindowConfig:
    """서버 정보와 창 설정으로부터 NativeWindowConfig를 생성한다.

    Args:
        host: 서버 호스트 주소
        port: 서버 포트 번호
        title: 창 제목
        width: 초기 창 너비
        height: 초기 창 높이
        min_width: 최소 창 너비
        min_height: 최소 창 높이

    Returns:
        NativeWindowConfig 인스턴스
    """
    url = f"http://{host}:{port}/app"
    return NativeWindowConfig(
        url=url,
        title=title,
        width=width,
        height=height,
        min_width=min_width,
        min_height=min_height,
    )


def build_subprocess_args(config: NativeWindowConfig) -> list[str]:
    """NativeWindowConfig로부터 서브프로세스 실행 인자 목록을 구성한다.

    Args:
        config: 네이티브 창 설정

    Returns:
        subprocess.Popen에 전달할 인자 리스트
    """
    return [
        sys.executable,
        "-m",
        "ui.native_window",
        "--url",
        config.url,
        "--title",
        config.title,
        "--width",
        str(config.width),
        "--height",
        str(config.height),
        "--min-width",
        str(config.min_width),
        "--min-height",
        str(config.min_height),
    ]


def launch_native_window(config: NativeWindowConfig) -> subprocess.Popen:
    """서브프로세스로 네이티브 창을 실행한다.

    rumps 메인 스레드와 충돌하지 않도록 별도 프로세스에서 pywebview를 실행한다.

    Args:
        config: 네이티브 창 설정

    Returns:
        실행된 서브프로세스의 Popen 인스턴스

    Raises:
        OSError: 서브프로세스 실행 실패 시
    """
    args = build_subprocess_args(config)
    logger.info(f"네이티브 창 서브프로세스 실행: {config.url}")
    return subprocess.Popen(args)


def run_webview_window(
    url: str,
    title: str,
    width: int,
    height: int,
    min_width: int,
    min_height: int,
) -> None:
    """pywebview 창을 생성하고 표시한다.

    서브프로세스의 엔트리포인트로 사용된다.
    이 함수는 창이 닫힐 때까지 블로킹된다.

    Args:
        url: 표시할 웹 페이지 URL
        title: 창 제목
        width: 창 너비
        height: 창 높이
        min_width: 최소 너비
        min_height: 최소 높이
    """
    import webview

    webview.create_window(
        title,
        url,
        width=width,
        height=height,
        min_size=(min_width, min_height),
    )
    webview.start()


def _parse_args() -> argparse.Namespace:
    """명령줄 인자를 파싱한다.

    Returns:
        파싱된 인자 네임스페이스
    """
    parser = argparse.ArgumentParser(description="네이티브 창으로 웹 UI 표시")
    parser.add_argument("--url", required=True, help="표시할 URL")
    parser.add_argument("--title", default="Recap", help="창 제목")
    parser.add_argument("--width", type=int, default=1200, help="창 너비")
    parser.add_argument("--height", type=int, default=800, help="창 높이")
    parser.add_argument("--min-width", type=int, default=800, help="최소 너비")
    parser.add_argument("--min-height", type=int, default=600, help="최소 높이")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_webview_window(
        url=args.url,
        title=args.title,
        width=args.width,
        height=args.height,
        min_width=args.min_width,
        min_height=args.min_height,
    )
