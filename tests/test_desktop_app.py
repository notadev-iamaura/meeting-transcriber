"""전용 앱 창의 연결·실패·종료 시 서버 소유권을 검증한다."""

from __future__ import annotations

import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ui.desktop_app import DesktopLaunchError, run_desktop_app
from ui.launcher import LauncherSpec, build_launcher_spec
from ui.native_window import NativeWindowConfig


@pytest.fixture
def spec(tmp_path: Path) -> LauncherSpec:
    """실제 사용자 데이터와 분리된 런처 계약을 만든다."""
    return build_launcher_spec(project_dir=tmp_path, log_file=tmp_path / "app.log")


def _process(*, running: bool = False, exit_code: int = 0) -> MagicMock:
    """종료 여부와 대기 결과를 제어하는 프로세스를 만든다."""
    process = MagicMock()
    process.poll.return_value = None if running else exit_code
    process.wait.return_value = exit_code
    return process


def _run(spec: LauncherSpec) -> int:
    """테스트용 창 설정으로 앱을 실행한다."""
    return run_desktop_app(
        spec,
        window=NativeWindowConfig(url=spec.app_url),
        startup_timeout_seconds=1,
    )


def test_reuses_existing_server_and_leaves_it_running(spec: LauncherSpec) -> None:
    """기존 서버에 붙은 창을 닫아도 서버를 종료하지 않는다."""
    window = _process()
    with (
        patch("ui.desktop_app.is_server_healthy", return_value=True),
        patch("ui.desktop_app.subprocess.Popen", return_value=window) as spawn,
    ):
        assert _run(spec) == 0
    spawn.assert_called_once()
    args = spawn.call_args.args[0]
    assert args[:3] == [str(spec.python_executable), "-m", "ui.native_window"]
    assert spec.app_url in args
    window.send_signal.assert_not_called()


def test_closing_window_gracefully_stops_owned_server(spec: LauncherSpec) -> None:
    """앱이 서버를 시작했으면 창 종료 후 SIGINT와 wait로 정리한다."""
    server, window = _process(running=True), _process()
    with (
        patch("ui.desktop_app.is_server_healthy", side_effect=[False, True]),
        patch("ui.desktop_app.subprocess.Popen", side_effect=[server, window]) as spawn,
    ):
        assert _run(spec) == 0
    assert spawn.call_args_list[0].args[0] == spec.build_server_argv()
    assert spawn.call_args_list[0].kwargs["cwd"] == str(spec.project_dir)
    server.send_signal.assert_called_once_with(signal.SIGINT)
    server.wait.assert_called_once()
    server.kill.assert_not_called()


@pytest.mark.parametrize("failure", [OSError("window unavailable"), None])
def test_window_failure_cleans_up_owned_server(
    spec: LauncherSpec, failure: OSError | None,
) -> None:
    """창 생성 실패와 비정상 종료 모두 직접 시작한 서버를 정리한다."""
    server = _process(running=True)
    second = failure if failure else _process(exit_code=1)
    with (
        patch("ui.desktop_app.is_server_healthy", side_effect=[False, True]),
        patch("ui.desktop_app.subprocess.Popen", side_effect=[server, second]),
        pytest.raises((DesktopLaunchError, OSError)),
    ):
        _run(spec)
    server.send_signal.assert_called_once_with(signal.SIGINT)
    server.wait.assert_called_once()


def test_window_failure_does_not_stop_existing_server(spec: LauncherSpec) -> None:
    """기존 서버에 붙을 때 창이 실패해도 서버에는 시그널을 보내지 않는다."""
    window = _process(exit_code=1)
    with (
        patch("ui.desktop_app.is_server_healthy", return_value=True),
        patch("ui.desktop_app.subprocess.Popen", return_value=window) as spawn,
        pytest.raises(DesktopLaunchError),
    ):
        _run(spec)
    spawn.assert_called_once()
    window.send_signal.assert_not_called()


def test_server_timeout_cleans_up_without_opening_window(spec: LauncherSpec) -> None:
    """서버가 준비되지 않으면 전용 창을 열지 않고 직접 만든 서버만 정리한다."""
    server = _process(running=True)
    with (
        patch("ui.desktop_app.is_server_healthy", return_value=False),
        patch("ui.desktop_app.subprocess.Popen", return_value=server) as spawn,
        patch("ui.desktop_app.time.monotonic", side_effect=[0, 2]),
        pytest.raises(DesktopLaunchError, match="대기 시간"),
    ):
        _run(spec)
    spawn.assert_called_once()
    server.send_signal.assert_called_once_with(signal.SIGINT)


def test_server_early_exit_does_not_open_window(spec: LauncherSpec) -> None:
    """서버가 먼저 죽으면 창을 열거나 이미 종료된 프로세스에 시그널을 보내지 않는다."""
    server = _process(exit_code=1)
    with (
        patch("ui.desktop_app.is_server_healthy", return_value=False),
        patch("ui.desktop_app.subprocess.Popen", return_value=server) as spawn,
        pytest.raises(DesktopLaunchError, match="시작 중 종료"),
    ):
        _run(spec)
    spawn.assert_called_once()
    server.send_signal.assert_not_called()


def test_interruption_cleans_up_both_owned_processes(spec: LauncherSpec) -> None:
    """창 대기를 중단해도 남겨진 자식 창과 서버를 정리한다."""
    server, window = _process(running=True), _process(running=True)
    window.wait.side_effect = [KeyboardInterrupt, 0]
    with (
        patch("ui.desktop_app.is_server_healthy", side_effect=[False, True]),
        patch("ui.desktop_app.subprocess.Popen", side_effect=[server, window]),
        pytest.raises(KeyboardInterrupt),
    ):
        _run(spec)
    window.send_signal.assert_called_once_with(signal.SIGINT)
    server.send_signal.assert_called_once_with(signal.SIGINT)
