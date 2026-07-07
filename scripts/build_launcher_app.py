#!/usr/bin/env python3
"""
경량 Recap 런처 `.app` 번들 생성기.

목적: 개발/검증용 unsigned macOS app bundle을 output 디렉토리에 생성한다.
      이 스크립트는 앱을 실행하지 않고, 설치/권한 변경/네트워크/모델 작업을 하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ui.launcher import LauncherSpec, build_launcher_spec, collect_launcher_preflight

DEFAULT_APP_NAME = "Recap"
DEFAULT_BUNDLE_ID = "com.recap.local-launcher"
DEFAULT_VERSION = "0.1.0"
BUNDLED_PROJECT_RELATIVE_PATH = "Contents/Resources/project"
_BUNDLED_SOURCE_DIRS = ("api", "core", "steps", "search", "security", "ui")
_BUNDLED_SOURCE_FILES = (
    "main.py",
    "config.py",
    "config.yaml",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "LICENSE",
)
_EXCLUDED_SOURCE_NAMES = frozenset(
    {
        ".DS_Store",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "benchmark_runs",
        "build",
        "dist",
        "htmlcov",
        "meeting_transcriber.egg-info",
        "node_modules",
        "output",
        "state",
        "web-dist",
    }
)
_EXCLUDED_SOURCE_SUFFIXES = frozenset(
    {
        ".aac",
        ".aiff",
        ".bin",
        ".chroma",
        ".db",
        ".flac",
        ".gguf",
        ".m4a",
        ".mlmodel",
        ".mp3",
        ".mp4",
        ".npy",
        ".npz",
        ".onnx",
        ".pt",
        ".pyc",
        ".pyo",
        ".safetensors",
        ".sqlite",
        ".sqlite3",
        ".wav",
    }
)
_HF_TOKEN_VALUE_RE = re.compile(r"(?<![A-Za-z0-9_])hf_[A-Za-z0-9][A-Za-z0-9_-]{7,}")


@dataclass(frozen=True)
class LauncherAppBuildResult:
    """생성된 `.app` 번들 정보."""

    app_path: Path
    executable_path: Path
    info_plist_path: Path
    metadata_path: Path
    bundled_project_path: Path | None = None
    bundled_file_count: int = 0

    def to_dict(self) -> dict[str, str | int | None]:
        """JSON 출력용 dict로 변환한다."""
        return {
            "app_path": str(self.app_path),
            "executable_path": str(self.executable_path),
            "info_plist_path": str(self.info_plist_path),
            "metadata_path": str(self.metadata_path),
            "bundled_project_path": (
                str(self.bundled_project_path) if self.bundled_project_path else None
            ),
            "bundled_file_count": self.bundled_file_count,
        }


def build_launcher_app(
    *,
    output_dir: Path | str,
    project_dir: Path | str | None = None,
    python_executable: Path | str | None = None,
    app_name: str = DEFAULT_APP_NAME,
    bundle_id: str = DEFAULT_BUNDLE_ID,
    version: str = DEFAULT_VERSION,
    host: str = "127.0.0.1",
    port: int = 8765,
    force: bool = False,
    bundle_source: bool = False,
) -> LauncherAppBuildResult:
    """Unsigned local `.app` 번들을 생성한다.

    Args:
        output_dir: `.app` 번들을 둘 디렉토리.
        project_dir: Recap 프로젝트 루트.
        python_executable: 서버 실행 Python. None이면 `ui.launcher` 기본 선택.
        app_name: Finder에 표시할 앱 이름.
        bundle_id: CFBundleIdentifier.
        version: CFBundleShortVersionString/CFBundleVersion.
        host: 로컬 서버 host. loopback만 허용.
        port: 로컬 서버 port.
        force: 기존 app bundle을 교체할지 여부.
        bundle_source: 런타임 소스 스냅샷을 `.app` 안에 포함할지 여부.

    Returns:
        생성된 app bundle 경로 정보.

    Raises:
        ValueError: app 이름이나 preflight가 유효하지 않을 때.
        FileExistsError: 대상 app bundle이 이미 있고 force=False일 때.
    """
    if "/" in app_name or app_name.endswith(".app") or not app_name.strip():
        raise ValueError(
            "app_name must be a non-empty bundle display name without slashes or .app"
        )

    spec = build_launcher_spec(
        project_dir=project_dir,
        python_executable=python_executable,
        host=host,
        port=port,
    )
    report = collect_launcher_preflight(spec)
    if not report.ready:
        failed = ", ".join(check.id for check in report.checks if not check.ready)
        raise ValueError(f"launcher preflight failed: {failed}")

    expanded_output_dir = Path(output_dir).expanduser()
    if expanded_output_dir.is_symlink():
        raise ValueError(f"output_dir must not be a symlink: {expanded_output_dir}")
    if expanded_output_dir.exists() and not expanded_output_dir.is_dir():
        raise ValueError(f"output_dir must be a directory: {expanded_output_dir}")

    resolved_output_dir = expanded_output_dir.resolve(strict=False)
    app_path = resolved_output_dir / f"{app_name}.app"
    _validate_app_output_target(app_path=app_path, output_dir=resolved_output_dir, force=force)
    resolved_output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=f".{app_name}.build-",
        dir=resolved_output_dir,
    ) as staging_root:
        staging_app_path = Path(staging_root) / f"{app_name}.app"
        result = _write_launcher_app_bundle(
            actual_app_path=staging_app_path,
            public_app_path=app_path,
            spec=spec,
            app_name=app_name,
            bundle_id=bundle_id,
            version=version,
            host=host,
            port=port,
            bundle_source=bundle_source,
        )
        _install_staged_app(staging_app_path=staging_app_path, app_path=app_path)

    return result


def _write_launcher_app_bundle(
    *,
    actual_app_path: Path,
    public_app_path: Path,
    spec: LauncherSpec,
    app_name: str,
    bundle_id: str,
    version: str,
    host: str,
    port: int,
    bundle_source: bool,
) -> LauncherAppBuildResult:
    """staging `.app` 내용을 쓰고 최종 public 경로 기준 result를 반환한다."""
    contents_dir = actual_app_path / "Contents"
    macos_dir = contents_dir / "MacOS"
    resources_dir = contents_dir / "Resources"
    macos_dir.mkdir(parents=True)
    resources_dir.mkdir(parents=True)

    executable_path = macos_dir / app_name
    info_plist_path = contents_dir / "Info.plist"
    metadata_path = resources_dir / "launcher-metadata.json"
    public_contents_dir = public_app_path / "Contents"
    public_executable_path = public_contents_dir / "MacOS" / app_name
    public_info_plist_path = public_contents_dir / "Info.plist"
    public_metadata_path = public_contents_dir / "Resources" / "launcher-metadata.json"
    actual_bundled_project_path = resources_dir / "project" if bundle_source else None
    public_bundled_project_path = (
        public_contents_dir / "Resources" / "project" if bundle_source else None
    )
    bundled_file_count = 0
    if actual_bundled_project_path is not None and public_bundled_project_path is not None:
        bundled_file_count = _copy_project_source(
            source_dir=spec.project_dir,
            target_dir=actual_bundled_project_path,
        )
        runtime_preflight_spec = build_launcher_spec(
            project_dir=actual_bundled_project_path,
            python_executable=spec.python_executable,
            host=host,
            port=port,
            log_file=spec.log_file,
        )
        runtime_report = collect_launcher_preflight(runtime_preflight_spec)
        if not runtime_report.ready:
            failed = ", ".join(check.id for check in runtime_report.checks if not check.ready)
            raise ValueError(f"bundled launcher preflight failed: {failed}")
        runtime_spec = build_launcher_spec(
            project_dir=public_bundled_project_path,
            python_executable=spec.python_executable,
            host=host,
            port=port,
            log_file=spec.log_file,
        )
    else:
        runtime_spec = spec

    _write_info_plist(
        path=info_plist_path,
        app_name=app_name,
        bundle_id=bundle_id,
        version=version,
    )
    _write_launcher_executable(
        path=executable_path,
        app_name=app_name,
        spec=runtime_spec,
        bundled_project=bundle_source,
    )
    _validate_launcher_executable_syntax(executable_path)
    _write_metadata(
        path=metadata_path,
        app_name=app_name,
        bundle_id=bundle_id,
        version=version,
        spec=runtime_spec,
        bundled_project=bundle_source,
        bundled_file_count=bundled_file_count,
    )

    return LauncherAppBuildResult(
        app_path=public_app_path,
        executable_path=public_executable_path,
        info_plist_path=public_info_plist_path,
        metadata_path=public_metadata_path,
        bundled_project_path=public_bundled_project_path,
        bundled_file_count=bundled_file_count,
    )


def _install_staged_app(*, staging_app_path: Path, app_path: Path) -> None:
    """검증을 마친 staging bundle을 최종 app 경로로 이동한다."""
    backup_root: Path | None = None
    backup_app_path: Path | None = None
    if app_path.exists():
        backup_root = Path(
            tempfile.mkdtemp(prefix=f".{app_path.stem}.backup-", dir=app_path.parent)
        )
        backup_app_path = backup_root / app_path.name
        app_path.rename(backup_app_path)
    try:
        staging_app_path.rename(app_path)
    except OSError:
        if backup_app_path is not None and backup_app_path.exists() and not app_path.exists():
            backup_app_path.rename(app_path)
        if backup_root is not None:
            try:
                backup_root.rmdir()
            except OSError:
                pass
        raise
    if backup_root is not None:
        shutil.rmtree(backup_root)


def _validate_app_output_target(*, app_path: Path, output_dir: Path, force: bool) -> None:
    """`.app` 출력 target이 output_dir 안의 안전한 교체 대상인지 확인한다."""
    resolved_output_dir = output_dir.resolve(strict=False)
    if app_path.is_symlink():
        raise FileExistsError(f"app bundle output must not be a symlink: {app_path}")
    if not _is_relative_to(app_path.resolve(strict=False), resolved_output_dir):
        raise ValueError(f"app bundle output must stay inside output_dir: {app_path}")
    if not app_path.exists():
        return
    if not force:
        raise FileExistsError(f"app bundle already exists: {app_path}")
    if not app_path.is_dir():
        raise FileExistsError(f"app bundle output must be an app directory: {app_path}")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _copy_project_source(*, source_dir: Path, target_dir: Path) -> int:
    """런타임에 필요한 프로젝트 소스 스냅샷을 복사한다."""
    copied_files = 0
    target_dir.mkdir(parents=True)
    for name in _BUNDLED_SOURCE_FILES:
        source_path = source_dir / name
        if source_path.is_file() and _is_allowed_source_path(source_path):
            destination = target_dir / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            _copy_source_file(source_path, destination)
            copied_files += 1

    for name in _BUNDLED_SOURCE_DIRS:
        source_path = source_dir / name
        if source_path.is_dir() and _is_allowed_source_path(source_path):
            copied_files += _copy_tree_filtered(source_path, target_dir / name)

    return copied_files


def _copy_tree_filtered(source_dir: Path, target_dir: Path) -> int:
    copied_files = 0
    for source_path in source_dir.rglob("*"):
        if not _is_allowed_source_path(source_path):
            continue
        relative_path = source_path.relative_to(source_dir)
        destination = target_dir / relative_path
        if source_path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_source_file(source_path, destination)
        copied_files += 1
    return copied_files


def _copy_source_file(source_path: Path, destination: Path) -> None:
    if source_path.name == "config.yaml":
        sanitized = _sanitize_bundled_config(source_path.read_text(encoding="utf-8"))
        destination.write_text(sanitized, encoding="utf-8")
        shutil.copystat(source_path, destination)
        return
    shutil.copy2(source_path, destination)


def _sanitize_bundled_config(content: str) -> str:
    """번들 config에서 로컬 HuggingFace 토큰 값을 제거한다."""
    sanitized_lines: list[str] = []
    for line in content.splitlines():
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if stripped.startswith("huggingface_token:"):
            sanitized_lines.append(f"{indent}huggingface_token: null")
            continue
        if "HUGGINGFACE_TOKEN" in line or "HF_TOKEN" in line or _HF_TOKEN_VALUE_RE.search(line):
            continue
        sanitized_lines.append(line)
    return "\n".join(sanitized_lines) + "\n"


def _is_allowed_source_path(path: Path) -> bool:
    """로컬 상태, 캐시, 비밀 파일을 소스 스냅샷에서 제외한다."""
    if path.is_symlink():
        return False
    if path.name.startswith(".env"):
        return False
    if path.name in _EXCLUDED_SOURCE_NAMES:
        return False
    if path.suffix in _EXCLUDED_SOURCE_SUFFIXES:
        return False
    return not any(
        part in _EXCLUDED_SOURCE_NAMES or part.startswith(".env") for part in path.parts
    )


def _validate_launcher_executable_syntax(path: Path) -> None:
    """생성된 런처 shell wrapper가 bash 구문으로 유효한지 확인한다."""
    try:
        result = subprocess.run(
            ["/bin/bash", "-n", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"launcher executable syntax validation could not run: {type(exc).__name__}"
        ) from exc
    if result.returncode == 0:
        return

    detail = (result.stderr or result.stdout or "").strip().splitlines()
    message = detail[0] if detail else f"bash -n exited with {result.returncode}"
    if (
        "HUGGINGFACE_TOKEN" in message
        or "HF_TOKEN" in message
        or _HF_TOKEN_VALUE_RE.search(message)
    ):
        message = "<redacted>"
    raise RuntimeError(f"launcher executable failed bash syntax validation: {message}")


def _write_info_plist(
    *,
    path: Path,
    app_name: str,
    bundle_id: str,
    version: str,
) -> None:
    """Info.plist를 생성한다."""
    payload = {
        "CFBundleDevelopmentRegion": "ko",
        "CFBundleDisplayName": app_name,
        "CFBundleExecutable": app_name,
        "CFBundleIdentifier": bundle_id,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": app_name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
    }
    with path.open("wb") as fh:
        plistlib.dump(payload, fh, sort_keys=True)


def _write_launcher_executable(
    *,
    path: Path,
    app_name: str,
    spec: LauncherSpec,
    bundled_project: bool,
) -> None:
    """`.app/Contents/MacOS` 실행 파일을 생성한다."""
    project_dir = "''" if bundled_project else shlex.quote(str(spec.project_dir))
    python_executable = shlex.quote(str(spec.python_executable))
    host = shlex.quote(spec.host)
    port = str(spec.port)
    log_file = shlex.quote(str(spec.log_file))
    app_label = shlex.quote(app_name)
    bundled_project_probe = (
        """
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLED_PROJECT_DIR="$(cd "${SCRIPT_DIR}/../Resources/project" 2>/dev/null && pwd || true)"
if [[ -n "${BUNDLED_PROJECT_DIR}" && -f "${BUNDLED_PROJECT_DIR}/main.py" ]]; then
  PROJECT_DIR="${BUNDLED_PROJECT_DIR}"
fi
"""
        if bundled_project
        else ""
    )
    script = f"""#!/bin/bash
set -euo pipefail

PROJECT_DIR={project_dir}
PYTHON_BIN={python_executable}
SERVER_HOST={host}
SERVER_PORT={port}
LOG_FILE={log_file}
APP_NAME={app_label}
{bundled_project_probe}

if [[ -z "${{PROJECT_DIR}}" ]]; then
  if command -v osascript >/dev/null 2>&1; then
    osascript -e 'display alert "Recap 실행 준비가 필요합니다" message "앱 번들 안의 프로젝트 소스를 찾을 수 없습니다."'
  fi
  exit 1
fi

if [[ ! -x "${{PYTHON_BIN}}" ]]; then
  if [[ -x "${{PROJECT_DIR}}/.venv/bin/python" ]]; then
    PYTHON_BIN="${{PROJECT_DIR}}/.venv/bin/python"
  elif [[ -x "${{HOME}}/.meeting-transcriber-venv/bin/python" ]]; then
    PYTHON_BIN="${{HOME}}/.meeting-transcriber-venv/bin/python"
  else
    if command -v osascript >/dev/null 2>&1; then
      osascript -e 'display alert "Recap 실행 준비가 필요합니다" message "Python 가상환경을 찾을 수 없습니다. 먼저 프로젝트 셋업을 완료해 주세요."'
    fi
    exit 1
  fi
fi

PYTHON_LAUNCH=("${{PYTHON_BIN}}")
if [[ -x /usr/bin/arch ]] && /usr/bin/arch -arm64 /usr/bin/true >/dev/null 2>&1; then
  PYTHON_LAUNCH=(/usr/bin/arch -arm64 "${{PYTHON_BIN}}")
fi

exec "${{PYTHON_LAUNCH[@]}}" - "${{PROJECT_DIR}}" "${{PYTHON_BIN}}" "${{SERVER_HOST}}" "${{SERVER_PORT}}" "${{LOG_FILE}}" "${{APP_NAME}}" <<'PY'
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

project_dir, python_bin, server_host, server_port, log_file, app_name = sys.argv[1:7]
sys.path.insert(0, project_dir)

from ui.launcher import build_launcher_spec, collect_launcher_preflight

spec = build_launcher_spec(
    project_dir=project_dir,
    python_executable=python_bin,
    host=server_host,
    port=int(server_port),
    log_file=log_file,
)
report = collect_launcher_preflight(spec)
if not report.ready:
    failed = ", ".join(check.id for check in report.checks if not check.ready)
    print(f"{{app_name}} launcher preflight failed: {{failed}}", file=sys.stderr)
    raise SystemExit(1)

base_url = spec.app_url.rsplit("/app", 1)[0]
health_url = base_url + "/api/health"

def open_setup_if_healthy() -> bool:
    try:
        with urllib.request.urlopen(health_url, timeout=1) as response:
            if response.status == 200:
                subprocess.run(["open", spec.setup_url], check=False)
                return True
    except (urllib.error.URLError, TimeoutError):
        return False
    return False

if open_setup_if_healthy():
    raise SystemExit(0)

env = os.environ.copy()
env.update(spec.build_environment_overrides())
log_path = Path(spec.log_file)
log_path.parent.mkdir(parents=True, exist_ok=True)
with log_path.open("a", encoding="utf-8") as log_handle:
    server = subprocess.Popen(
        spec.build_server_argv(),
        cwd=str(spec.project_dir),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if open_setup_if_healthy():
            raise SystemExit(server.wait())

        return_code = server.poll()
        if return_code is not None:
            raise SystemExit(return_code)
        time.sleep(0.5)

    server.terminate()
raise SystemExit(1)
PY
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def _write_metadata(
    *,
    path: Path,
    app_name: str,
    bundle_id: str,
    version: str,
    spec: LauncherSpec,
    bundled_project: bool,
    bundled_file_count: int,
) -> None:
    """런처 번들 메타데이터를 JSON으로 저장한다."""
    payload = {
        "app_name": app_name,
        "bundle_id": bundle_id,
        "version": version,
        "launcher": spec.to_dict(),
        "source_bundle": {
            "enabled": bundled_project,
            "relative_path": BUNDLED_PROJECT_RELATIVE_PATH if bundled_project else "",
            "file_count": bundled_file_count,
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI 인자를 파싱한다."""
    parser = argparse.ArgumentParser(description="Recap unsigned launcher .app 생성")
    parser.add_argument("--output-dir", type=Path, default=Path("dist"), help="출력 디렉토리")
    parser.add_argument("--project-dir", type=Path, default=None, help="Recap 프로젝트 루트")
    parser.add_argument("--python", type=Path, default=None, help="서버 실행 Python 경로")
    parser.add_argument("--app-name", default=DEFAULT_APP_NAME, help="앱 이름")
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID, help="CFBundleIdentifier")
    parser.add_argument("--version", default=DEFAULT_VERSION, help="앱 버전")
    parser.add_argument("--host", default="127.0.0.1", help="로컬 서버 host")
    parser.add_argument("--port", type=int, default=8765, help="로컬 서버 port")
    parser.add_argument("--force", action="store_true", help="기존 app bundle 교체")
    parser.add_argument(
        "--bundle-source",
        action="store_true",
        help="런타임 프로젝트 소스 스냅샷을 .app에 포함",
    )
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = _parse_args(argv)
    result = build_launcher_app(
        output_dir=args.output_dir,
        project_dir=args.project_dir,
        python_executable=args.python,
        app_name=args.app_name,
        bundle_id=args.bundle_id,
        version=args.version,
        host=args.host,
        port=args.port,
        force=args.force,
        bundle_source=args.bundle_source,
    )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(result.app_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
