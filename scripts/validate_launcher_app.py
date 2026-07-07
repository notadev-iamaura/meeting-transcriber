#!/usr/bin/env python3
"""
Recap 런처 `.app` 번들 검증기.

목적: unsigned local `.app` prototype의 구조와 배포 readiness를 read-only로 검사한다.
      앱 실행, 서명, 공증, 설치, 네트워크, 모델 작업은 수행하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ui.launcher import (
    LAUNCHER_PROJECT_DIR_ENV,
    LAUNCHER_PYTHON_EXECUTABLE_ENV,
    LAUNCHER_PYTHON_SOURCE_ENV,
)

ValidationStatus = Literal["pass", "warn", "fail"]
_FORBIDDEN_SECRET_MARKERS = ("HUGGINGFACE_TOKEN", "HF_TOKEN", "hf_")
_HF_TOKEN_VALUE_RE = re.compile(r"(?<![A-Za-z0-9_])hf_[A-Za-z0-9][A-Za-z0-9_-]{7,}")
_BUNDLED_SOURCE_REQUIRED_FILES = ("main.py", "config.py", "config.yaml", "pyproject.toml")
_BUNDLED_SOURCE_REQUIRED_DIRS = ("api", "core", "steps", "search", "security", "ui")
_BUNDLED_SOURCE_EXCLUDED_NAMES = frozenset(
    {
        ".git",
        ".codex",
        ".claude",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".superpowers",
        ".venv",
        "__pycache__",
        "benchmark_runs",
        "build",
        "design_handoff_recap_rebrand",
        "dist",
        "docs",
        "evals",
        "goals",
        "harness",
        "htmlcov",
        "meeting_transcriber.egg-info",
        "node_modules",
        "output",
        "state",
        "tests",
        "web-dist",
    }
)
_BUNDLED_SOURCE_EXCLUDED_SUFFIXES = frozenset(
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
_LAUNCHER_REQUIRED_STRING_FIELDS = (
    "project_dir",
    "python_executable",
    "main_py",
    "host",
    "log_file",
    "cwd",
    "command_display",
    "app_url",
    "setup_url",
)
_VALID_LAUNCHER_PYTHON_SOURCES = frozenset(
    {"explicit", "project_venv", "managed_venv", "current_interpreter"}
)
_LAUNCHER_REQUIRED_ENV_FIELDS = (
    "PATH",
    "LANG",
    "PYTHONUNBUFFERED",
    LAUNCHER_PYTHON_SOURCE_ENV,
    LAUNCHER_PYTHON_EXECUTABLE_ENV,
    LAUNCHER_PROJECT_DIR_ENV,
)


@dataclass(frozen=True)
class ValidationCheck:
    """`.app` 번들 검증 항목."""

    id: str
    status: ValidationStatus
    message: str
    details: dict[str, str | int | bool]

    @property
    def ok(self) -> bool:
        """구조 검증 실패 여부를 반환한다."""
        return self.status != "fail"

    def to_dict(self) -> dict[str, object]:
        """JSON 출력용 dict로 변환한다."""
        return {
            "id": self.id,
            "status": self.status,
            "ok": self.ok,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class LauncherAppValidationReport:
    """런처 `.app` 검증 결과."""

    app_path: Path
    status: Literal["pass", "fail"]
    local_ready: bool
    distribution_ready: bool
    checks: tuple[ValidationCheck, ...]

    def to_dict(self) -> dict[str, object]:
        """JSON 출력용 dict로 변환한다."""
        return {
            "app_path": str(self.app_path),
            "status": self.status,
            "local_ready": self.local_ready,
            "distribution_ready": self.distribution_ready,
            "checks": [check.to_dict() for check in self.checks],
        }


def validate_launcher_app(
    app_path: Path | str,
    *,
    check_codesign: bool = True,
) -> LauncherAppValidationReport:
    """런처 `.app` 구조와 배포 readiness를 read-only로 검증한다.

    Args:
        app_path: 검사할 `.app` 경로.
        check_codesign: `codesign --verify` read-only 검사를 수행할지 여부.

    Returns:
        LauncherAppValidationReport.
    """
    resolved_app_path = Path(app_path).expanduser().resolve(strict=False)
    plist_data = _load_info_plist(resolved_app_path)
    executable_name = _plist_string(plist_data, "CFBundleExecutable")

    checks = [
        _check_app_directory(resolved_app_path),
        _check_info_plist(resolved_app_path, plist_data),
        _check_plist_contract(plist_data),
        _check_executable(resolved_app_path, executable_name),
        _check_executable_syntax(resolved_app_path, executable_name),
        _check_metadata(resolved_app_path),
        _check_source_bundle(resolved_app_path),
        _check_secret_hygiene(resolved_app_path, executable_name),
    ]
    if check_codesign:
        checks.append(_check_codesign(resolved_app_path))

    checks_tuple = tuple(checks)
    local_ready = all(check.ok for check in checks_tuple if check.id != "codesign")
    distribution_ready = local_ready and any(
        check.id == "codesign" and check.status == "pass" for check in checks_tuple
    )
    status: Literal["pass", "fail"] = "pass" if local_ready else "fail"
    return LauncherAppValidationReport(
        app_path=resolved_app_path,
        status=status,
        local_ready=local_ready,
        distribution_ready=distribution_ready,
        checks=checks_tuple,
    )


def _load_info_plist(app_path: Path) -> dict[str, object] | None:
    plist_path = app_path / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as fh:
            data = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _plist_string(plist_data: dict[str, object] | None, key: str) -> str:
    if not plist_data:
        return ""
    value = plist_data.get(key)
    return value if isinstance(value, str) else ""


def _has_forbidden_marker(value: str) -> bool:
    return any(marker in value for marker in _FORBIDDEN_SECRET_MARKERS)


def _safe_detail(value: str) -> str:
    return "<redacted>" if _has_forbidden_marker(value) else value


def _safe_error_message(value: str, *, max_length: int = 240) -> str:
    first_line = value.strip().splitlines()[0] if value.strip() else ""
    if _has_forbidden_marker(first_line) or _HF_TOKEN_VALUE_RE.search(first_line):
        return "<redacted>"
    if len(first_line) <= max_length:
        return first_line
    return f"{first_line[:max_length]}..."


def _is_valid_bundle_executable_name(name: str) -> bool:
    if not name:
        return False
    candidate = Path(name)
    return (
        not candidate.is_absolute()
        and len(candidate.parts) == 1
        and name not in {".", ".."}
        and candidate.name == name
    )


def _check_app_directory(app_path: Path) -> ValidationCheck:
    if app_path.is_dir() and app_path.name.endswith(".app"):
        return ValidationCheck(
            id="app_directory",
            status="pass",
            message="`.app` 디렉토리를 찾았습니다.",
            details={"path": _safe_detail(str(app_path))},
        )
    return ValidationCheck(
        id="app_directory",
        status="fail",
        message="검사 대상이 `.app` 디렉토리가 아닙니다.",
        details={"path": _safe_detail(str(app_path))},
    )


def _check_info_plist(
    app_path: Path,
    plist_data: dict[str, object] | None,
) -> ValidationCheck:
    plist_path = app_path / "Contents" / "Info.plist"
    if plist_data is not None:
        return ValidationCheck(
            id="info_plist",
            status="pass",
            message="Info.plist를 읽었습니다.",
            details={"path": _safe_detail(str(plist_path))},
        )
    return ValidationCheck(
        id="info_plist",
        status="fail",
        message="Info.plist가 없거나 plist 형식이 아닙니다.",
        details={"path": _safe_detail(str(plist_path))},
    )


def _check_plist_contract(plist_data: dict[str, object] | None) -> ValidationCheck:
    if not plist_data:
        return ValidationCheck(
            id="plist_contract",
            status="fail",
            message="Info.plist 계약을 확인할 수 없습니다.",
            details={},
        )

    package_type = plist_data.get("CFBundlePackageType")
    executable = _plist_string(plist_data, "CFBundleExecutable")
    executable_name_valid = _is_valid_bundle_executable_name(executable)
    bundle_id = _plist_string(plist_data, "CFBundleIdentifier")
    short_version = _plist_string(plist_data, "CFBundleShortVersionString")
    build_version = _plist_string(plist_data, "CFBundleVersion")
    valid = (
        package_type == "APPL"
        and bool(executable)
        and executable_name_valid
        and bool(bundle_id)
        and bool(short_version)
        and bool(build_version)
    )
    if valid:
        return ValidationCheck(
            id="plist_contract",
            status="pass",
            message="Info.plist 앱 번들 계약이 유효합니다.",
            details={
                "bundle_id": _safe_detail(bundle_id),
                "executable": _safe_detail(executable),
                "executable_name_valid": executable_name_valid,
                "package_type": str(package_type),
                "short_version": _safe_detail(short_version),
                "build_version": _safe_detail(build_version),
            },
        )
    return ValidationCheck(
        id="plist_contract",
        status="fail",
        message="Info.plist 필수 앱 번들 필드가 유효하지 않습니다.",
        details={
            "bundle_id": _safe_detail(bundle_id),
            "executable": _safe_detail(executable),
            "executable_name_valid": executable_name_valid,
            "package_type": str(package_type),
            "short_version": _safe_detail(short_version),
            "build_version": _safe_detail(build_version),
        },
    )


def _check_executable(app_path: Path, executable_name: str) -> ValidationCheck:
    if not _is_valid_bundle_executable_name(executable_name):
        return ValidationCheck(
            id="executable",
            status="fail",
            message="CFBundleExecutable 이름이 앱 번들 파일명으로 유효하지 않습니다.",
            details={"executable_name_valid": False},
        )

    executable_path = app_path / "Contents" / "MacOS" / executable_name
    exists = executable_path.is_file()
    executable = bool(exists and executable_path.stat().st_mode & stat.S_IXUSR)
    if exists and executable:
        return ValidationCheck(
            id="executable",
            status="pass",
            message="CFBundleExecutable 파일과 실행 권한을 확인했습니다.",
            details={"path": _safe_detail(str(executable_path)), "executable": True},
        )
    return ValidationCheck(
        id="executable",
        status="fail",
        message="CFBundleExecutable 파일이 없거나 실행 권한이 없습니다.",
        details={
            "path": _safe_detail(str(executable_path)),
            "exists": exists,
            "executable": executable,
        },
    )


def _check_executable_syntax(app_path: Path, executable_name: str) -> ValidationCheck:
    if not _is_valid_bundle_executable_name(executable_name):
        return ValidationCheck(
            id="executable_syntax",
            status="fail",
            message="CFBundleExecutable 이름이 앱 번들 파일명으로 유효하지 않습니다.",
            details={"executable_name_valid": False},
        )

    executable_path = app_path / "Contents" / "MacOS" / executable_name
    if not executable_path.is_file():
        return ValidationCheck(
            id="executable_syntax",
            status="fail",
            message="CFBundleExecutable 구문을 확인할 수 없습니다.",
            details={
                "path": _safe_detail(str(executable_path)),
                "exists": False,
            },
        )

    try:
        result = subprocess.run(
            ["/bin/bash", "-n", str(executable_path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return ValidationCheck(
            id="executable_syntax",
            status="fail",
            message="CFBundleExecutable 구문 검증 시간이 초과되었습니다.",
            details={
                "path": _safe_detail(str(executable_path)),
                "error_type": type(exc).__name__,
            },
        )
    except OSError as exc:
        return ValidationCheck(
            id="executable_syntax",
            status="fail",
            message="CFBundleExecutable 구문 검증을 실행하지 못했습니다.",
            details={
                "path": _safe_detail(str(executable_path)),
                "error_type": type(exc).__name__,
            },
        )

    if result.returncode == 0:
        return ValidationCheck(
            id="executable_syntax",
            status="pass",
            message="CFBundleExecutable bash 구문을 확인했습니다.",
            details={
                "path": _safe_detail(str(executable_path)),
                "returncode": result.returncode,
            },
        )

    return ValidationCheck(
        id="executable_syntax",
        status="fail",
        message="CFBundleExecutable bash 구문이 유효하지 않습니다.",
        details={
            "path": _safe_detail(str(executable_path)),
            "returncode": result.returncode,
            "message": _safe_error_message(result.stderr or result.stdout),
        },
    )


def _check_metadata(app_path: Path) -> ValidationCheck:
    metadata_path = app_path / "Contents" / "Resources" / "launcher-metadata.json"
    payload = _load_metadata(metadata_path)
    if payload is None:
        return ValidationCheck(
            id="metadata",
            status="fail",
            message="launcher metadata가 없거나 JSON 형식이 아닙니다.",
            details={"path": _safe_detail(str(metadata_path))},
        )

    launcher = payload.get("launcher") if isinstance(payload, dict) else None
    missing_fields, invalid_fields = _validate_launcher_metadata(launcher)
    if (
        isinstance(payload, dict)
        and isinstance(launcher, dict)
        and not missing_fields
        and not invalid_fields
    ):
        return ValidationCheck(
            id="metadata",
            status="pass",
            message="launcher metadata JSON을 확인했습니다.",
            details={
                "path": _safe_detail(str(metadata_path)),
                "launcher_contract": True,
            },
        )
    return ValidationCheck(
        id="metadata",
        status="fail",
        message="launcher metadata 계약이 유효하지 않습니다.",
        details={
            "path": _safe_detail(str(metadata_path)),
            "missing_fields": ", ".join(missing_fields),
            "invalid_fields": ", ".join(invalid_fields),
        },
    )


def _load_metadata(metadata_path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _validate_launcher_metadata(launcher: object) -> tuple[list[str], list[str]]:
    if not isinstance(launcher, dict):
        return ["launcher"], []

    missing_fields: list[str] = []
    invalid_fields: list[str] = []
    for field in _LAUNCHER_REQUIRED_STRING_FIELDS:
        value = launcher.get(field)
        if value is None:
            missing_fields.append(field)
        elif not isinstance(value, str) or not value:
            invalid_fields.append(field)

    port = launcher.get("port")
    if port is None:
        missing_fields.append("port")
    elif not isinstance(port, int) or not 0 < port <= 65535:
        invalid_fields.append("port")

    command = launcher.get("command")
    if command is None:
        missing_fields.append("command")
    elif (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        invalid_fields.append("command")

    environment_overrides = launcher.get("environment_overrides")
    if environment_overrides is None:
        missing_fields.append("environment_overrides")
    elif not isinstance(environment_overrides, dict):
        invalid_fields.append("environment_overrides")
    else:
        for field in _LAUNCHER_REQUIRED_ENV_FIELDS:
            value = environment_overrides.get(field)
            if value is None:
                missing_fields.append(f"environment_overrides.{field}")
            elif not isinstance(value, str) or not value:
                invalid_fields.append(f"environment_overrides.{field}")
        invalid_fields.extend(
            _invalid_launcher_environment_fields(
                launcher=launcher,
                environment_overrides=environment_overrides,
            )
        )

    runtime = launcher.get("runtime")
    if runtime is None:
        missing_fields.append("runtime")
    elif not isinstance(runtime, dict):
        invalid_fields.append("runtime")
    else:
        invalid_fields.extend(_invalid_launcher_runtime_fields(launcher, runtime))

    app_url = launcher.get("app_url")
    setup_url = launcher.get("setup_url")
    if isinstance(app_url, str) and not app_url.endswith("/app"):
        invalid_fields.append("app_url")
    if isinstance(setup_url, str) and not setup_url.endswith("/app/setup"):
        invalid_fields.append("setup_url")

    return missing_fields, invalid_fields


def _invalid_launcher_environment_fields(
    *,
    launcher: dict[str, object],
    environment_overrides: dict[object, object],
) -> list[str]:
    """metadata environment_overrides handoff coherence 오류 필드를 반환한다."""
    invalid_fields: list[str] = []
    python_source = environment_overrides.get(LAUNCHER_PYTHON_SOURCE_ENV)
    python_executable = environment_overrides.get(LAUNCHER_PYTHON_EXECUTABLE_ENV)
    project_dir = environment_overrides.get(LAUNCHER_PROJECT_DIR_ENV)
    runtime = launcher.get("runtime")

    if isinstance(python_source, str) and python_source not in _VALID_LAUNCHER_PYTHON_SOURCES:
        invalid_fields.append(f"environment_overrides.{LAUNCHER_PYTHON_SOURCE_ENV}")
    if (
        isinstance(python_executable, str)
        and isinstance(launcher.get("python_executable"), str)
        and python_executable != launcher["python_executable"]
    ):
        invalid_fields.append(f"environment_overrides.{LAUNCHER_PYTHON_EXECUTABLE_ENV}")
    if (
        isinstance(project_dir, str)
        and isinstance(launcher.get("project_dir"), str)
        and project_dir != launcher["project_dir"]
    ):
        invalid_fields.append(f"environment_overrides.{LAUNCHER_PROJECT_DIR_ENV}")
    if (
        isinstance(runtime, dict)
        and isinstance(python_source, str)
        and isinstance(runtime.get("python_source"), str)
        and python_source != runtime["python_source"]
    ):
        invalid_fields.append(f"environment_overrides.{LAUNCHER_PYTHON_SOURCE_ENV}")
    return invalid_fields


def _invalid_launcher_runtime_fields(
    launcher: dict[str, object],
    runtime: dict[object, object],
) -> list[str]:
    """metadata runtime coherence 오류 필드를 반환한다."""
    invalid_fields: list[str] = []
    python_source = runtime.get("python_source")
    python_executable = runtime.get("python_executable")
    candidates = runtime.get("candidates")
    if not isinstance(python_source, str) or python_source not in _VALID_LAUNCHER_PYTHON_SOURCES:
        invalid_fields.append("runtime.python_source")
    if (
        not isinstance(python_executable, str)
        or not python_executable
        or (
            isinstance(launcher.get("python_executable"), str)
            and python_executable != launcher["python_executable"]
        )
    ):
        invalid_fields.append("runtime.python_executable")
    if (
        not isinstance(candidates, list)
        or not candidates
        or not _valid_runtime_candidates(
            candidates,
            python_source=python_source,
            python_executable=python_executable,
        )
    ):
        invalid_fields.append("runtime.candidates")
    return invalid_fields


def _valid_runtime_candidates(
    candidates: list[object],
    *,
    python_source: object,
    python_executable: object,
) -> bool:
    """serialized runtime 후보 목록의 shape/coherence를 검증한다."""
    selected_candidates: list[dict[object, object]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            return False
        candidate_id = candidate.get("id")
        path = candidate.get("path")
        if not isinstance(candidate_id, str) or candidate_id not in _VALID_LAUNCHER_PYTHON_SOURCES:
            return False
        if not isinstance(path, str) or not path:
            return False
        for field in ("exists", "is_file", "is_executable", "selected"):
            if not isinstance(candidate.get(field), bool):
                return False
        if candidate["is_executable"] and not candidate["is_file"]:
            return False
        if candidate["selected"]:
            selected_candidates.append(candidate)

    if len(selected_candidates) != 1:
        return False

    selected = selected_candidates[0]
    return selected["id"] == python_source and selected["path"] == python_executable


def _check_source_bundle(app_path: Path) -> ValidationCheck:
    metadata_path = app_path / "Contents" / "Resources" / "launcher-metadata.json"
    payload = _load_metadata(metadata_path)
    source_bundle = payload.get("source_bundle") if payload else None
    if not isinstance(source_bundle, dict) or source_bundle.get("enabled") is not True:
        return ValidationCheck(
            id="source_bundle",
            status="pass",
            message="번들 소스 스냅샷이 비활성화되어 있습니다.",
            details={"enabled": False},
        )

    relative_path = source_bundle.get("relative_path")
    if not isinstance(relative_path, str) or not relative_path:
        return ValidationCheck(
            id="source_bundle",
            status="fail",
            message="번들 소스 metadata의 relative_path가 유효하지 않습니다.",
            details={"enabled": True, "relative_path_valid": False},
        )
    relative_project_path = Path(relative_path)
    if relative_project_path.is_absolute() or ".." in relative_project_path.parts:
        return ValidationCheck(
            id="source_bundle",
            status="fail",
            message="번들 소스 relative_path가 앱 번들 밖을 가리킵니다.",
            details={"enabled": True, "relative_path_valid": False},
        )

    project_dir = app_path / relative_project_path
    missing_items = _missing_bundled_source_items(project_dir)
    excluded_items = _find_excluded_bundled_source_items(project_dir)
    secret_items = _find_bundled_config_secret_items(project_dir)
    file_count = source_bundle.get("file_count")
    invalid_count = not isinstance(file_count, int) or file_count <= 0
    if not missing_items and not excluded_items and not secret_items and not invalid_count:
        return ValidationCheck(
            id="source_bundle",
            status="pass",
            message="번들 소스 스냅샷 계약을 확인했습니다.",
            details={
                "enabled": True,
                "relative_path": relative_path,
                "file_count": file_count,
            },
        )

    invalid_fields = []
    if invalid_count:
        invalid_fields.append("file_count")
    return ValidationCheck(
        id="source_bundle",
        status="fail",
        message="번들 소스 스냅샷 계약이 유효하지 않습니다.",
        details={
            "enabled": True,
            "missing_items": ", ".join(missing_items),
            "excluded_items": ", ".join(excluded_items),
            "secret_items": ", ".join(secret_items),
            "invalid_fields": ", ".join(invalid_fields),
        },
    )


def _missing_bundled_source_items(project_dir: Path) -> list[str]:
    missing_items: list[str] = []
    for name in _BUNDLED_SOURCE_REQUIRED_FILES:
        if not (project_dir / name).is_file():
            missing_items.append(name)
    for name in _BUNDLED_SOURCE_REQUIRED_DIRS:
        if not (project_dir / name).is_dir():
            missing_items.append(name)
    return missing_items


def _find_excluded_bundled_source_items(project_dir: Path) -> list[str]:
    if not project_dir.exists():
        return []
    excluded_items: list[str] = []
    resolved_project_dir = project_dir.resolve(strict=False)
    for path in project_dir.rglob("*"):
        relative_parts = path.relative_to(project_dir).parts
        excluded_by_name = path.name.startswith(".env") or any(
            part in _BUNDLED_SOURCE_EXCLUDED_NAMES or part.startswith(".env")
            for part in relative_parts
        )
        excluded_by_suffix = path.suffix in _BUNDLED_SOURCE_EXCLUDED_SUFFIXES
        symlink_escape = False
        if path.is_symlink():
            resolved_path = path.resolve(strict=False)
            symlink_escape = not _is_relative_to(resolved_path, resolved_project_dir)
        if excluded_by_name or excluded_by_suffix or symlink_escape:
            excluded_items.append(str(path.relative_to(project_dir)))
            if len(excluded_items) >= 10:
                break
    return excluded_items


def _find_bundled_config_secret_items(project_dir: Path) -> list[str]:
    config_path = project_dir / "config.yaml"
    try:
        content = config_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    if (
        "HUGGINGFACE_TOKEN" in content
        or "HF_TOKEN" in content
        or _HF_TOKEN_VALUE_RE.search(content)
    ):
        return ["config.yaml"]
    return []


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _check_secret_hygiene(app_path: Path, executable_name: str) -> ValidationCheck:
    plist_path = app_path / "Contents" / "Info.plist"
    metadata_path = app_path / "Contents" / "Resources" / "launcher-metadata.json"
    paths: tuple[tuple[str, Path], ...] = (
        ("info_plist", plist_path),
        ("metadata", metadata_path),
    )
    if _is_valid_bundle_executable_name(executable_name):
        paths = (
            *paths,
            ("executable", app_path / "Contents" / "MacOS" / executable_name),
        )
    affected_files: list[str] = []
    marker_count = 0
    for file_id, path in paths:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        matched = [marker for marker in _FORBIDDEN_SECRET_MARKERS if marker in content]
        if matched:
            affected_files.append(file_id)
            marker_count += len(matched)

    if not affected_files:
        return ValidationCheck(
            id="secret_hygiene",
            status="pass",
            message="metadata와 executable에서 토큰 마커를 찾지 못했습니다.",
            details={"forbidden_markers_found": False},
        )
    return ValidationCheck(
        id="secret_hygiene",
        status="fail",
        message="metadata 또는 executable에 금지된 민감정보 마커가 포함되어 있습니다.",
        details={
            "forbidden_markers_found": True,
            "affected_files": ", ".join(affected_files),
            "marker_count": marker_count,
        },
    )


def _check_codesign(app_path: Path) -> ValidationCheck:
    codesign = shutil.which("codesign")
    if not codesign:
        return ValidationCheck(
            id="codesign",
            status="warn",
            message="codesign 도구를 찾을 수 없어 서명 상태를 확인하지 못했습니다.",
            details={"available": False},
        )

    try:
        result = subprocess.run(
            [codesign, "--verify", "--deep", "--strict", str(app_path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ValidationCheck(
            id="codesign",
            status="warn",
            message="codesign 검증을 완료하지 못했습니다.",
            details={"error_type": type(exc).__name__},
        )
    if result.returncode == 0:
        return ValidationCheck(
            id="codesign",
            status="pass",
            message="codesign 검증을 통과했습니다.",
            details={"returncode": result.returncode},
        )
    return ValidationCheck(
        id="codesign",
        status="warn",
        message="앱 번들이 배포용 서명 검증을 통과하지 못했습니다.",
        details={"returncode": result.returncode},
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI 인자를 파싱한다."""
    parser = argparse.ArgumentParser(description="Recap launcher .app 검증")
    parser.add_argument("app_path", type=Path, help="검증할 .app 경로")
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    parser.add_argument("--skip-codesign", action="store_true", help="codesign 검사를 건너뜀")
    parser.add_argument(
        "--strict-distribution",
        action="store_true",
        help="distribution_ready=false면 non-zero exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = _parse_args(argv)
    report = validate_launcher_app(
        args.app_path,
        check_codesign=not args.skip_codesign,
    )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"{report.app_path}: status={report.status} "
            f"local_ready={report.local_ready} distribution_ready={report.distribution_ready}"
        )

    if report.status == "fail":
        return 1
    if args.strict_distribution and not report.distribution_ready:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
