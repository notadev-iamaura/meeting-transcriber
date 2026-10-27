"""Recap 전용 창과 로컬 서버의 수명을 관리하는 macOS 앱 진입점."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request

from ui.launcher import LauncherSpec
from ui.native_window import NativeWindowConfig, build_subprocess_args

logger = logging.getLogger(__name__)


class DesktopLaunchError(RuntimeError):
    """로컬 서버 또는 전용 창을 시작하지 못했을 때 발생한다."""


def is_server_healthy(spec: LauncherSpec) -> bool:
    """프록시와 리다이렉트 없이 지정된 로컬 서버의 준비 상태를 확인한다."""
    health_url = spec.app_url.removesuffix("/app") + "/api/health"

    class LocalHealthHandler(urllib.request.HTTPRedirectHandler):
        """준비 상태 조회가 다른 주소로 이동하는 것을 차단한다."""

        def redirect_request(
            self,
            req: urllib.request.Request,
            fp: object,
            code: int,
            msg: str,
            headers: object,
            newurl: str,
        ) -> None:
            """리다이렉트를 거부한다."""
            return None

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), LocalHealthHandler(),
    )
    try:
        with opener.open(health_url, timeout=1) as response:
            payload = json.loads(response.read(4096))
            return response.status == 200 and payload.get("status") == "ok"
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return False


def _stop_owned_process(process: subprocess.Popen, *, label: str) -> None:
    """이 앱이 만든 프로세스만 정상 종료하고 종료 완료를 기다린다."""
    if process.poll() is not None:
        return
    logger.info(f"{label} 종료 요청")
    try:
        process.send_signal(signal.SIGINT)
    except ProcessLookupError:
        return
    # 녹음 파일 정리와 파이프라인 체크포인트 저장을 끝낼 때까지 강제 종료하지 않는다.
    process.wait()


def run_desktop_app(
    spec: LauncherSpec,
    *,
    window: NativeWindowConfig,
    startup_timeout_seconds: float,
) -> int:
    """서버에 연결해 전용 창을 열고 직접 만든 서버만 창 종료 후 정리한다."""
    env = os.environ.copy()
    env.update(spec.build_environment_overrides())
    server: subprocess.Popen | None = None
    native_window: subprocess.Popen | None = None
    spec.log_file.parent.mkdir(parents=True, exist_ok=True)
    with spec.log_file.open("a", encoding="utf-8") as log_handle:
        try:
            if not is_server_healthy(spec):
                server = subprocess.Popen(
                    spec.build_server_argv(),
                    cwd=str(spec.project_dir),
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                )
                deadline = time.monotonic() + startup_timeout_seconds
                while True:
                    if server.poll() is not None:
                        raise DesktopLaunchError("로컬 서버가 시작 중 종료되었습니다.")
                    if is_server_healthy(spec):
                        break
                    if time.monotonic() >= deadline:
                        raise DesktopLaunchError("로컬 서버의 시작 대기 시간을 초과했습니다.")
                    time.sleep(0.1)

            args = build_subprocess_args(window)
            args[0] = str(spec.python_executable)
            native_window = subprocess.Popen(
                args,
                cwd=str(spec.project_dir),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
            # Cocoa의 Cmd+Q가 Python finally를 생략해도 부모 프로세스가 서버를 정리한다.
            return_code = native_window.wait()
            if return_code != 0:
                raise DesktopLaunchError("Recap 전용 창을 열지 못했습니다.")
            return 0
        finally:
            if native_window is not None:
                _stop_owned_process(native_window, label="Recap 창")
            if server is not None:
                _stop_owned_process(server, label="로컬 서버")


def launch_desktop_app(spec: LauncherSpec, *, app_name: str) -> int:
    """설정을 읽어 전용 앱을 시작하고 실패하면 macOS 알림으로 안내한다."""
    from config import load_config
    from yaml import YAMLError

    spec.log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(spec.log_file), level=logging.INFO)
    try:
        # 서버와 같은 기본 경로 및 MT_CONFIG_PATH 선택 규칙을 사용한다.
        config = load_config()
        window = NativeWindowConfig(
            url=spec.app_url,
            title=app_name,
            width=config.window.width,
            height=config.window.height,
            min_width=config.window.min_width,
            min_height=config.window.min_height,
        )
        return run_desktop_app(
            spec,
            window=window,
            startup_timeout_seconds=config.server.startup_timeout_seconds,
        )
    except (DesktopLaunchError, ImportError, OSError, ValueError, YAMLError) as error:
        logger.error(f"전용 앱 실행 실패: {type(error).__name__}")
        from AppKit import NSAlert

        alert = NSAlert.alloc().init()
        alert.setMessageText_(f"{app_name}을 실행하지 못했습니다")
        alert.setInformativeText_(
            f"로컬 서버 또는 전용 창을 시작하지 못했습니다. 로그를 확인해 주세요.\n{spec.log_file}"
        )
        alert.addButtonWithTitle_("확인")
        alert.runModal()
        return 1
