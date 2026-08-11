"""
경량 런처 계약 모듈.

목적: 향후 macOS `.app` 런처가 실행 전에 사용할 read-only preflight와
      서버 실행 명령을 한 곳에서 생성한다. 이 모듈은 설치, 권한 변경,
      디렉토리 생성, 모델 다운로드를 수행하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

LauncherStatus = Literal["pass", "fail"]
PythonSource = Literal["explicit", "project_venv", "managed_venv", "current_interpreter"]
LAUNCHER_PYTHON_SOURCE_ENV = "MT_LAUNCHER_PYTHON_SOURCE"
LAUNCHER_PYTHON_EXECUTABLE_ENV = "MT_LAUNCHER_PYTHON_EXECUTABLE"
LAUNCHER_PROJECT_DIR_ENV = "MT_LAUNCHER_PROJECT_DIR"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class LauncherPythonCandidate:
    """런처가 고려한 Python 실행 파일 후보."""

    id: PythonSource
    path: Path
    selected: bool = False

    @property
    def exists(self) -> bool:
        """후보 경로가 존재하는지 반환한다."""
        return self.path.exists()

    @property
    def is_file(self) -> bool:
        """후보 경로가 파일인지 반환한다."""
        return self.path.is_file()

    @property
    def is_executable(self) -> bool:
        """후보 경로가 실행 가능한 파일인지 반환한다."""
        return self.is_file and os.access(self.path, os.X_OK)

    def to_dict(self) -> dict[str, object]:
        """JSON 응답용 dict로 변환한다."""
        return {
            "id": self.id,
            "path": str(self.path),
            "exists": self.exists,
            "is_file": self.is_file,
            "is_executable": self.is_executable,
            "selected": self.selected,
        }


@dataclass(frozen=True)
class LauncherCheck:
    """런처 preflight의 단일 점검 항목."""

    id: str
    status: LauncherStatus
    message: str
    details: dict[str, str | int | bool]

    @property
    def ready(self) -> bool:
        """점검 항목이 통과했는지 반환한다."""
        return self.status == "pass"

    def to_dict(self) -> dict[str, object]:
        """JSON 응답용 dict로 변환한다."""
        return {
            "id": self.id,
            "status": self.status,
            "ready": self.ready,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class LauncherSpec:
    """런처가 서버를 시작하고 UI를 열기 위해 필요한 불변 설정."""

    project_dir: Path
    python_executable: Path
    main_py: Path
    host: str
    port: int
    log_file: Path
    python_source: PythonSource
    python_candidates: tuple[LauncherPythonCandidate, ...]

    @property
    def app_url(self) -> str:
        """일반 앱 UI URL."""
        return f"http://{_format_url_host(self.host)}:{self.port}/app"

    @property
    def setup_url(self) -> str:
        """최초 설정 준비 상태 UI URL."""
        return f"http://{_format_url_host(self.host)}:{self.port}/app/setup"

    def build_server_argv(self) -> list[str]:
        """헤드리스 서버 실행 argv를 생성한다.

        런처는 별도 창을 열 수 있으므로 서버는 메뉴바 없이 실행한다.
        """
        return [
            str(self.python_executable),
            str(self.main_py),
            "--no-menubar",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--log-file",
            str(self.log_file),
        ]

    def build_environment_overrides(self) -> dict[str, str]:
        """서버 실행 시 런처가 더할 비밀 없는 환경변수 override를 반환한다.

        사용자의 전체 환경은 런처 구현체가 프로세스 실행 시점에 병합할 수 있다.
        이 JSON 계약은 토큰이나 `.env` 값을 직렬화하지 않도록 안전한 override만
        노출한다.
        """
        path_parts = [
            str(self.python_executable.parent),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        return {
            "PATH": ":".join(dict.fromkeys(path_parts)),
            "LANG": "ko_KR.UTF-8",
            "PYTHONUNBUFFERED": "1",
            LAUNCHER_PYTHON_SOURCE_ENV: self.python_source,
            LAUNCHER_PYTHON_EXECUTABLE_ENV: str(self.python_executable),
            LAUNCHER_PROJECT_DIR_ENV: str(self.project_dir),
        }

    def to_dict(self) -> dict[str, object]:
        """JSON 응답용 dict로 변환한다."""
        return {
            "project_dir": str(self.project_dir),
            "python_executable": str(self.python_executable),
            "main_py": str(self.main_py),
            "host": self.host,
            "port": self.port,
            "log_file": str(self.log_file),
            "cwd": str(self.project_dir),
            "command": self.build_server_argv(),
            "command_display": shlex.join(self.build_server_argv()),
            "environment_overrides": self.build_environment_overrides(),
            "app_url": self.app_url,
            "setup_url": self.setup_url,
            "runtime": {
                "python_source": self.python_source,
                "python_executable": str(self.python_executable),
                "candidates": [candidate.to_dict() for candidate in self.python_candidates],
            },
        }


@dataclass(frozen=True)
class LauncherPreflightReport:
    """런처 실행 전 read-only preflight 결과."""

    status: LauncherStatus
    ready: bool
    spec: LauncherSpec
    checks: tuple[LauncherCheck, ...]

    def to_dict(self) -> dict[str, object]:
        """JSON 응답용 dict로 변환한다."""
        return {
            "status": self.status,
            "ready": self.ready,
            "launcher": self.spec.to_dict(),
            "checks": [check.to_dict() for check in self.checks],
        }


def default_project_dir() -> Path:
    """소스 checkout 기준 프로젝트 루트를 반환한다."""
    return Path(__file__).resolve().parents[1]


def _normalize_path(path: Path | str) -> Path:
    """사용자 입력 경로를 절대 경로로 정규화한다."""
    return Path(path).expanduser().resolve(strict=False)


def _normalize_executable_path(path: Path | str) -> Path:
    """실행 파일 경로를 symlink 보존 절대 경로로 정규화한다."""
    expanded = Path(path).expanduser()
    if expanded.is_absolute():
        return expanded
    return Path.cwd() / expanded


def _normalize_host(host: str) -> str:
    """런처 host 입력을 일관된 비교/출력 형태로 정규화한다."""
    return host.strip().lower()


def _format_url_host(host: str) -> str:
    """URL에 사용할 host 문자열을 반환한다."""
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return f"[{host}]"
    return host


def _default_python_executable(project_dir: Path) -> Path:
    """런처용 Python 후보를 결정한다.

    우선순위는 프로젝트 `.venv`, 사용자 전용 설치 venv, 현재 인터프리터 순서다.
    """
    selected, _source, _candidates = _select_default_python(project_dir)
    return selected


def _default_python_candidates(project_dir: Path) -> tuple[LauncherPythonCandidate, ...]:
    return (
        LauncherPythonCandidate(
            id="project_venv",
            path=project_dir / ".venv" / "bin" / "python",
        ),
        LauncherPythonCandidate(
            id="managed_venv",
            path=Path.home() / ".meeting-transcriber-venv" / "bin" / "python",
        ),
        LauncherPythonCandidate(
            id="current_interpreter",
            path=_normalize_executable_path(sys.executable),
        ),
    )


def _select_default_python(
    project_dir: Path,
) -> tuple[Path, PythonSource, tuple[LauncherPythonCandidate, ...]]:
    candidates = _default_python_candidates(project_dir)
    selected = next((candidate for candidate in candidates if candidate.is_file), candidates[-1])
    selected_candidates = tuple(
        LauncherPythonCandidate(
            id=candidate.id,
            path=candidate.path,
            selected=candidate.id == selected.id,
        )
        for candidate in candidates
    )
    return selected.path, selected.id, selected_candidates


def _explicit_python_candidates(
    *,
    project_dir: Path,
    python_executable: Path,
) -> tuple[LauncherPythonCandidate, ...]:
    return (
        LauncherPythonCandidate(
            id="explicit",
            path=python_executable,
            selected=True,
        ),
        *_default_python_candidates(project_dir),
    )


def build_launcher_spec(
    *,
    project_dir: Path | str | None = None,
    python_executable: Path | str | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    log_file: Path | str | None = None,
) -> LauncherSpec:
    """런처 실행 설정을 생성한다.

    Args:
        project_dir: Recap 프로젝트 루트. None이면 현재 소스 checkout을 사용.
        python_executable: 서버 실행 Python. None이면 로컬 후보를 자동 선택.
        host: FastAPI 바인딩 호스트.
        port: FastAPI 포트.
        log_file: 앱 로그 파일. None이면 사용자 데이터 디렉토리 로그를 사용.

    Returns:
        LauncherSpec 인스턴스.
    """
    resolved_project_dir = _normalize_path(project_dir or default_project_dir())
    if python_executable is not None:
        resolved_python = _normalize_executable_path(python_executable)
        python_source: PythonSource = "explicit"
        python_candidates = _explicit_python_candidates(
            project_dir=resolved_project_dir,
            python_executable=resolved_python,
        )
    else:
        resolved_python, python_source, python_candidates = _select_default_python(
            resolved_project_dir
        )
    resolved_log_file = (
        _normalize_path(log_file)
        if log_file is not None
        else _normalize_path(Path.home() / ".meeting-transcriber" / "logs" / "launcher-app.log")
    )
    return LauncherSpec(
        project_dir=resolved_project_dir,
        python_executable=resolved_python,
        main_py=resolved_project_dir / "main.py",
        host=_normalize_host(host),
        port=port,
        log_file=resolved_log_file,
        python_source=python_source,
        python_candidates=python_candidates,
    )


def collect_launcher_preflight(spec: LauncherSpec) -> LauncherPreflightReport:
    """런처 실행 가능 여부를 read-only로 점검한다.

    파일 존재/실행 권한과 host/port 형식만 확인하며, 시스템 설정이나 파일 시스템을 수정하지 않는다.
    """
    checks = (
        _check_project_dir(spec.project_dir),
        _check_main_py(spec.main_py),
        _check_python(spec.python_executable),
        _check_server_binding(spec.host, spec.port),
    )
    ready = all(check.ready for check in checks)
    return LauncherPreflightReport(
        status="pass" if ready else "fail",
        ready=ready,
        spec=spec,
        checks=checks,
    )


def _check_project_dir(project_dir: Path) -> LauncherCheck:
    if project_dir.is_dir():
        return LauncherCheck(
            id="project_dir",
            status="pass",
            message="프로젝트 디렉토리를 찾았습니다.",
            details={"path": str(project_dir)},
        )
    return LauncherCheck(
        id="project_dir",
        status="fail",
        message="프로젝트 디렉토리를 찾을 수 없습니다.",
        details={"path": str(project_dir)},
    )


def _check_main_py(main_py: Path) -> LauncherCheck:
    if main_py.is_file():
        return LauncherCheck(
            id="main_py",
            status="pass",
            message="main.py 진입점을 찾았습니다.",
            details={"path": str(main_py)},
        )
    return LauncherCheck(
        id="main_py",
        status="fail",
        message="main.py 진입점을 찾을 수 없습니다.",
        details={"path": str(main_py)},
    )


def _check_python(python_executable: Path) -> LauncherCheck:
    is_file = python_executable.is_file()
    is_executable = bool(is_file and os.access(python_executable, os.X_OK))
    details: dict[str, str | int | bool] = {
        "path": str(python_executable),
        "is_file": is_file,
        "is_executable": is_executable,
    }

    if is_executable:
        return LauncherCheck(
            id="python_executable",
            status="pass",
            message="Python 실행 파일을 찾았습니다.",
            details=details,
        )
    if is_file:
        return LauncherCheck(
            id="python_executable",
            status="fail",
            message="Python 실행 파일에 실행 권한이 없습니다.",
            details=details,
        )
    return LauncherCheck(
        id="python_executable",
        status="fail",
        message="Python 실행 파일을 찾을 수 없습니다.",
        details=details,
    )


def _check_server_binding(host: str, port: int) -> LauncherCheck:
    valid = host in _LOOPBACK_HOSTS and 1 <= port <= 65535
    if valid:
        return LauncherCheck(
            id="server_binding",
            status="pass",
            message="서버 바인딩 설정이 유효합니다.",
            details={"host": host, "port": port},
        )
    return LauncherCheck(
        id="server_binding",
        status="fail",
        message="서버 host는 loopback이어야 하며 port는 TCP 범위 안에 있어야 합니다.",
        details={"host": host, "port": port},
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """런처 preflight CLI 인자를 파싱한다."""
    parser = argparse.ArgumentParser(description="Recap 경량 런처 preflight")
    parser.add_argument("--project-dir", type=Path, default=None, help="Recap 프로젝트 루트")
    parser.add_argument("--python", type=Path, default=None, help="서버 실행 Python 경로")
    parser.add_argument("--host", default="127.0.0.1", help="FastAPI 바인딩 호스트")
    parser.add_argument("--port", type=int, default=8765, help="FastAPI 포트")
    parser.add_argument("--log-file", type=Path, default=None, help="앱 로그 파일 경로")
    parser.add_argument(
        "--print-command",
        action="store_true",
        help="JSON 대신 서버 실행 명령만 출력",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint.

    Returns:
        0 if preflight is ready, otherwise 1.
    """
    args = _parse_args(argv)
    spec = build_launcher_spec(
        project_dir=args.project_dir,
        python_executable=args.python,
        host=args.host,
        port=args.port,
        log_file=args.log_file,
    )
    report = collect_launcher_preflight(spec)
    if args.print_command:
        sys.stdout.write(shlex.join(spec.build_server_argv()) + "\n")
    else:
        sys.stdout.write(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n")
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
