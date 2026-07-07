"""스타트업 측정 스크립트 단위 테스트."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from scripts.measure_startup import (
    ROOT,
    StartupMeasurement,
    _build_command,
    _resolve_python_for_subprocess,
    _tail_text,
    build_parser,
    main,
    measure_startup,
)


class FakeProcess:
    """measure_startup 테스트용 subprocess 대역."""

    returncode: int | None = None

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        """프로세스 실행 상태를 반환한다."""
        return self.returncode

    def terminate(self) -> None:
        """프로세스 종료 요청을 기록한다."""
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        """프로세스 강제 종료 요청을 기록한다."""
        self.killed = True
        self.returncode = -9

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        """프로세스 출력 대역을 반환한다."""
        return "stdout", "stderr"


def test_build_command_uses_main_no_menubar() -> None:
    """측정 명령은 실제 headless 앱 경로를 사용한다."""
    command = _build_command(
        python=Path("/tmp/python"),
        host="127.0.0.1",
        port=9000,
        log_level="warning",
    )

    assert command == [
        "/tmp/python",
        "main.py",
        "--no-menubar",
        "--host",
        "127.0.0.1",
        "--port",
        "9000",
        "--log-level",
        "warning",
    ]


def test_resolve_python_preserves_relative_venv_symlink() -> None:
    """상대 Python 경로는 symlink resolve 없이 프로젝트 루트 기준으로 보존한다."""
    assert _resolve_python_for_subprocess(Path(".venv/bin/python")) == ROOT / ".venv/bin/python"


def test_tail_text_keeps_short_text_and_truncates_long_text() -> None:
    """프로세스 출력 tail 보존 규칙을 검증한다."""
    assert _tail_text("abc", limit=10) == "abc"
    assert _tail_text("0123456789", limit=4) == "6789"


def test_parser_defaults_measure_three_second_budget() -> None:
    """기본 CLI 값은 B-0 3초 목표를 검사한다."""
    args = build_parser().parse_args([])

    assert args.max_seconds == 3.0
    assert args.no_threshold is False
    assert args.timeout == 30.0


def test_measure_startup_uses_temp_base_dir_and_cleans_up(tmp_path: Path) -> None:
    """기본 측정은 임시 데이터 디렉토리를 만들고 종료 시 삭제한다."""
    fake_process = FakeProcess()
    created_base_dir = tmp_path / "startup"

    args = argparse.Namespace(
        host="127.0.0.1",
        port=54321,
        python=Path("/tmp/python"),
        base_dir=None,
        keep_base_dir=False,
        timeout=30.0,
        poll_interval=0.05,
        no_threshold=False,
        max_seconds=3.0,
        log_level="warning",
    )

    with (
        patch("scripts.measure_startup.tempfile.mkdtemp", return_value=str(created_base_dir)),
        patch("scripts.measure_startup.shutil.rmtree") as mock_rmtree,
        patch("scripts.measure_startup.subprocess.Popen", return_value=fake_process) as mock_popen,
        patch(
            "scripts.measure_startup._wait_for_endpoints",
            return_value=(True, 2.25, {"health": 2.0, "app": 2.25}, ""),
        ),
    ):
        measurement = measure_startup(args)

    assert measurement.ok is True
    assert measurement.within_budget is True
    assert measurement.health_seconds == 2.0
    assert measurement.app_seconds == 2.25
    assert measurement.ready_seconds == 2.25
    assert measurement.endpoint_seconds == {"health": 2.0, "app": 2.25}
    assert measurement.base_dir == str(created_base_dir)
    assert measurement.kept_base_dir is False
    assert fake_process.terminated is True
    mock_rmtree.assert_called_once_with(created_base_dir, ignore_errors=True)
    popen_kwargs = mock_popen.call_args.kwargs
    assert popen_kwargs["env"]["MT_BASE_DIR"] == str(created_base_dir)
    assert popen_kwargs["env"]["PYTHONUNBUFFERED"] == "1"


def test_measure_startup_reports_budget_failure(tmp_path: Path) -> None:
    """헬스체크가 성공해도 max_seconds 초과는 별도 실패로 표시한다."""
    fake_process = FakeProcess()
    args = argparse.Namespace(
        host="127.0.0.1",
        port=54322,
        python=Path("/tmp/python"),
        base_dir=tmp_path,
        keep_base_dir=False,
        timeout=30.0,
        poll_interval=0.05,
        no_threshold=False,
        max_seconds=3.0,
        log_level="warning",
    )

    with (
        patch("scripts.measure_startup.subprocess.Popen", return_value=fake_process),
        patch(
            "scripts.measure_startup._wait_for_endpoints",
            return_value=(True, 3.5, {"health": 2.8, "app": 3.5}, ""),
        ),
    ):
        measurement = measure_startup(args)

    assert measurement.ok is True
    assert measurement.within_budget is False
    assert measurement.kept_base_dir is True


def test_main_exit_codes() -> None:
    """CLI 종료 코드는 성공, 측정 실패, 예산 초과를 구분한다."""
    success = StartupMeasurement(
        ok=True,
        within_budget=True,
        health_seconds=1.0,
        app_seconds=1.0,
        ready_seconds=1.0,
        endpoint_seconds={"health": 1.0, "app": 1.0},
        elapsed_seconds=1.0,
        max_seconds=3.0,
        timeout_seconds=30.0,
        host="127.0.0.1",
        port=1,
        base_dir="/tmp/a",
        kept_base_dir=False,
        command=[],
        returncode=-15,
        error="",
        stdout_tail="",
        stderr_tail="",
    )
    failed = StartupMeasurement(
        ok=False,
        within_budget=False,
        health_seconds=None,
        app_seconds=None,
        ready_seconds=None,
        endpoint_seconds={},
        elapsed_seconds=30.0,
        max_seconds=3.0,
        timeout_seconds=30.0,
        host="127.0.0.1",
        port=1,
        base_dir="/tmp/a",
        kept_base_dir=False,
        command=[],
        returncode=1,
        error="timeout",
        stdout_tail="",
        stderr_tail="",
    )
    slow = StartupMeasurement(
        ok=True,
        within_budget=False,
        health_seconds=4.0,
        app_seconds=4.0,
        ready_seconds=4.0,
        endpoint_seconds={"health": 4.0, "app": 4.0},
        elapsed_seconds=4.0,
        max_seconds=3.0,
        timeout_seconds=30.0,
        host="127.0.0.1",
        port=1,
        base_dir="/tmp/a",
        kept_base_dir=False,
        command=[],
        returncode=-15,
        error="",
        stdout_tail="",
        stderr_tail="",
    )

    with patch("scripts.measure_startup.measure_startup", return_value=success):
        assert main([]) == 0
    with patch("scripts.measure_startup.measure_startup", return_value=failed):
        assert main([]) == 1
    with patch("scripts.measure_startup.measure_startup", return_value=slow):
        assert main([]) == 2
