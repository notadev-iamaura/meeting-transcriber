#!/usr/bin/env python3
"""앱 콜드 스타트 시간을 측정하는 로컬 하네스.

`main.py --no-menubar`를 별도 프로세스로 실행하고 `/api/health`가 200을
반환할 때까지 걸린 시간을 측정한다. 기본값은 임시 데이터 디렉토리와 임시
포트를 사용해 사용자 데이터 및 실행 중인 기본 서버와 충돌하지 않도록 한다.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class StartupMeasurement:
    """스타트업 측정 결과."""

    ok: bool
    within_budget: bool
    health_seconds: float | None
    app_seconds: float | None
    ready_seconds: float | None
    endpoint_seconds: dict[str, float]
    elapsed_seconds: float
    max_seconds: float | None
    timeout_seconds: float
    host: str
    port: int
    base_dir: str
    kept_base_dir: bool
    command: list[str]
    returncode: int | None
    error: str
    stdout_tail: str
    stderr_tail: str

    def to_json(self) -> str:
        """측정 결과를 JSON 문자열로 직렬화한다."""
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def _tail_text(text: str, limit: int = 4000) -> str:
    """긴 프로세스 출력을 뒤쪽 일부만 보존한다."""
    if len(text) <= limit:
        return text
    return text[-limit:]


def _find_free_port(host: str) -> int:
    """로컬에서 사용 가능한 임시 포트를 찾는다."""
    with socket.socket() as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _build_command(
    python: Path,
    host: str,
    port: int,
    log_level: str,
) -> list[str]:
    """측정할 앱 실행 명령을 구성한다."""
    return [
        str(python),
        "main.py",
        "--no-menubar",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        log_level,
    ]


def _resolve_python_for_subprocess(python: Path) -> Path:
    """venv symlink를 보존하면서 subprocess용 Python 경로를 만든다."""
    expanded = python.expanduser()
    if expanded.is_absolute():
        return expanded
    return ROOT / expanded


def _wait_for_endpoints(
    process: subprocess.Popen[str],
    urls: dict[str, str],
    started_at: float,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> tuple[bool, float | None, dict[str, float], str]:
    """모든 대상 URL이 성공하거나 프로세스가 종료될 때까지 대기한다."""
    deadline = started_at + timeout_seconds
    last_error = ""
    completed: dict[str, float] = {}
    while time.perf_counter() < deadline:
        if process.poll() is not None:
            return (
                False,
                None,
                completed,
                f"프로세스가 조기 종료됨: returncode={process.returncode}",
            )
        for name, url in urls.items():
            if name in completed:
                continue
            try:
                with urllib.request.urlopen(url, timeout=min(0.5, poll_interval_seconds)) as res:
                    if res.status == 200:
                        completed[name] = round(time.perf_counter() - started_at, 3)
                    else:
                        last_error = f"{name}: HTTP {res.status}"
            except (OSError, urllib.error.URLError, TimeoutError) as exc:
                last_error = f"{name}: {type(exc).__name__}: {exc}"
        if len(completed) == len(urls):
            return True, round(max(completed.values()), 3), completed, ""
        time.sleep(poll_interval_seconds)

    pending = ", ".join(sorted(set(urls) - set(completed)))
    return (
        False,
        None,
        completed,
        f"엔드포인트 대기 타임아웃 ({timeout_seconds:.1f}초, pending={pending}): {last_error}",
    )


def _terminate_process(process: subprocess.Popen[str]) -> tuple[str, str, int | None]:
    """측정용 앱 프로세스를 종료하고 출력 tail을 반환한다."""
    if process.poll() is None:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
    else:
        stdout, stderr = process.communicate(timeout=2)

    return _tail_text(stdout or ""), _tail_text(stderr or ""), process.returncode


def measure_startup(args: argparse.Namespace) -> StartupMeasurement:
    """CLI 인자 기준으로 앱 콜드 스타트를 측정한다."""
    host = args.host
    port = args.port if args.port is not None else _find_free_port(host)
    python = _resolve_python_for_subprocess(args.python)
    base_dir = (
        args.base_dir.expanduser().resolve()
        if args.base_dir
        else Path(tempfile.mkdtemp(prefix="mt-startup-"))
    )
    created_temp_base_dir = args.base_dir is None
    command = _build_command(python=python, host=host, port=port, log_level=args.log_level)
    health_url = f"http://{host}:{port}/api/health"
    app_url = f"http://{host}:{port}/app"
    env = os.environ.copy()
    env["MT_BASE_DIR"] = str(base_dir)
    env["PYTHONUNBUFFERED"] = "1"

    started_at = time.perf_counter()
    process: subprocess.Popen[str] | None = None
    ok = False
    health_seconds: float | None = None
    app_seconds: float | None = None
    ready_seconds: float | None = None
    endpoint_seconds: dict[str, float] = {}
    error = ""
    stdout_tail = ""
    stderr_tail = ""
    returncode: int | None = None

    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ok, ready_seconds, endpoint_seconds, error = _wait_for_endpoints(
            process=process,
            urls={"health": health_url, "app": app_url},
            started_at=started_at,
            timeout_seconds=args.timeout,
            poll_interval_seconds=args.poll_interval,
        )
        health_seconds = endpoint_seconds.get("health")
        app_seconds = endpoint_seconds.get("app")
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        elapsed_seconds = round(time.perf_counter() - started_at, 3)
        if process is not None:
            stdout_tail, stderr_tail, returncode = _terminate_process(process)
        if created_temp_base_dir and not args.keep_base_dir:
            shutil.rmtree(base_dir, ignore_errors=True)

    max_seconds = None if args.no_threshold else args.max_seconds
    within_budget = bool(ok and (max_seconds is None or (ready_seconds or 0.0) <= max_seconds))
    kept_base_dir = bool(args.keep_base_dir or not created_temp_base_dir)
    return StartupMeasurement(
        ok=ok,
        within_budget=within_budget,
        health_seconds=health_seconds,
        app_seconds=app_seconds,
        ready_seconds=ready_seconds,
        endpoint_seconds=endpoint_seconds,
        elapsed_seconds=elapsed_seconds,
        max_seconds=max_seconds,
        timeout_seconds=args.timeout,
        host=host,
        port=port,
        base_dir=str(base_dir),
        kept_base_dir=kept_base_dir,
        command=command,
        returncode=returncode,
        error=error,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
    )


def build_parser() -> argparse.ArgumentParser:
    """CLI 파서를 생성한다."""
    parser = argparse.ArgumentParser(
        description="Recap 앱 콜드 스타트 시간을 측정합니다.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="측정용 서버 host")
    parser.add_argument("--port", type=int, default=None, help="측정용 서버 port")
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="측정 프로세스를 실행할 Python 인터프리터",
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="측정에 사용할 데이터 디렉토리. 생략하면 임시 디렉토리를 사용합니다.",
    )
    parser.add_argument(
        "--keep-base-dir",
        action="store_true",
        help="임시 데이터 디렉토리를 삭제하지 않고 보존합니다.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="헬스체크 성공을 기다릴 최대 시간(초)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.05,
        help="헬스체크 폴링 간격(초)",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=3.0,
        help="허용할 최대 콜드 스타트 시간(초)",
    )
    parser.add_argument(
        "--no-threshold",
        action="store_true",
        help="측정만 수행하고 max-seconds 초과를 실패로 처리하지 않습니다.",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error", "critical"],
        default="warning",
        help="측정 대상 앱 로그 레벨",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 엔트리포인트."""
    args = build_parser().parse_args(argv)
    measurement = measure_startup(args)
    print(measurement.to_json())

    if not measurement.ok:
        return 1
    if not measurement.within_budget:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
