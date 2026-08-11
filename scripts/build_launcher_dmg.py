#!/usr/bin/env python3
"""
Recap 런처 `.app`을 unsigned local DMG로 패키징한다.

목적: 이미 생성된 unsigned `.app`을 배포 후보 산출물 형태로 감싼다.
      앱 실행, 서명, 공증, 설치, 네트워크, 의존성 설치는 수행하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.validate_launcher_app import LauncherAppValidationReport, validate_launcher_app

DEFAULT_VOLUME_NAME = "Recap"
_FORBIDDEN_SECRET_MARKERS = ("HUGGINGFACE_TOKEN", "HF_TOKEN", "hf_")
_HF_TOKEN_VALUE_RE = re.compile(r"(?<![A-Za-z0-9_])hf_[A-Za-z0-9][A-Za-z0-9_-]{7,}")


@dataclass(frozen=True)
class LauncherDmgBuildResult:
    """생성된 DMG 산출물 정보."""

    dmg_path: Path
    app_path: Path
    volume_name: str
    command: tuple[str, ...]
    returncode: int
    validation_summary: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        """JSON 출력용 dict로 변환한다."""
        return _redact_json_payload(
            {
                "success": True,
                "dmg_path": str(self.dmg_path),
                "app_path": str(self.app_path),
                "volume_name": self.volume_name,
                "returncode": self.returncode,
                "validation": self.validation_summary,
                "command": list(self.command),
            }
        )


def build_launcher_dmg(
    *,
    app_path: Path | str,
    output_path: Path | str | None = None,
    output_dir: Path | str | None = None,
    volume_name: str = DEFAULT_VOLUME_NAME,
    force: bool = False,
    hdiutil_path: Path | str | None = None,
) -> LauncherDmgBuildResult:
    """Unsigned local DMG를 생성한다.

    Args:
        app_path: 패키징할 `.app` 번들 경로.
        output_path: 생성할 `.dmg` 경로. None이면 output_dir/app 이름으로 결정.
        output_dir: output_path가 없을 때 사용할 디렉토리.
        volume_name: 마운트 시 표시될 볼륨 이름.
        force: 기존 DMG를 덮어쓸지 여부.
        hdiutil_path: 테스트 또는 명시 실행용 hdiutil 경로.

    Returns:
        LauncherDmgBuildResult.

    Raises:
        FileNotFoundError: hdiutil 또는 app bundle이 없을 때.
        FileExistsError: output DMG가 이미 있고 force=False일 때.
        ValueError: app bundle 검증이나 입력 계약이 실패했을 때.
        RuntimeError: hdiutil 실행이 실패했을 때.
    """
    resolved_app_path = Path(app_path).expanduser().resolve(strict=False)
    if not resolved_app_path.is_dir() or resolved_app_path.suffix != ".app":
        raise ValueError(f"app_path must be an existing .app directory: {resolved_app_path}")

    if not volume_name.strip() or "/" in volume_name:
        raise ValueError("volume_name must be non-empty and must not contain slashes")

    validation = validate_launcher_app(resolved_app_path, check_codesign=False)
    if not validation.local_ready:
        failed = ", ".join(check.id for check in validation.checks if not check.ok)
        raise ValueError(f"app bundle validation failed: {failed}")

    hdiutil = str(hdiutil_path) if hdiutil_path is not None else shutil.which("hdiutil")
    if not hdiutil:
        raise FileNotFoundError("hdiutil not found")

    resolved_output_path = _resolve_output_path(
        app_path=resolved_app_path,
        output_path=output_path,
        output_dir=output_dir,
    )
    _validate_output_target(
        app_path=resolved_app_path,
        dmg_path=resolved_output_path,
        force=force,
    )
    resolved_output_path = resolved_output_path.resolve(strict=False)
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)

    command = _build_hdiutil_command(
        hdiutil=hdiutil,
        app_path=resolved_app_path,
        dmg_path=resolved_output_path,
        volume_name=volume_name.strip(),
        force=force,
    )
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"hdiutil failed with exit code {result.returncode}")
    _validate_created_dmg(resolved_output_path)

    return LauncherDmgBuildResult(
        dmg_path=resolved_output_path,
        app_path=resolved_app_path,
        volume_name=volume_name.strip(),
        command=tuple(command),
        returncode=result.returncode,
        validation_summary=_summarize_validation(validation),
    )


def _resolve_output_path(
    *,
    app_path: Path,
    output_path: Path | str | None,
    output_dir: Path | str | None,
) -> Path:
    if output_path is not None and output_dir is not None:
        raise ValueError("output_path and output_dir are mutually exclusive")
    if output_path is not None:
        resolved = _absolute_path(Path(output_path).expanduser())
    else:
        base_dir = (
            _absolute_path(Path(output_dir).expanduser())
            if output_dir is not None
            else app_path.parent
        )
        resolved = base_dir / f"{app_path.stem}.dmg"
    if resolved.suffix != ".dmg":
        raise ValueError("output DMG path must end with .dmg")
    return resolved


def _absolute_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return Path.cwd() / path


def _validate_output_target(*, app_path: Path, dmg_path: Path, force: bool) -> None:
    if dmg_path.is_symlink():
        raise FileExistsError(f"DMG output path must not be a symlink: {dmg_path}")
    if _is_relative_to(dmg_path.resolve(strict=False), app_path):
        raise ValueError("output DMG path must not be inside the .app bundle")
    if dmg_path.is_dir():
        raise FileExistsError(f"DMG output path must not be a directory: {dmg_path}")
    if dmg_path.exists() and not dmg_path.is_file():
        raise FileExistsError(f"DMG output path must be a regular file: {dmg_path}")
    if dmg_path.exists() and not force:
        raise FileExistsError(f"DMG already exists: {dmg_path}")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _build_hdiutil_command(
    *,
    hdiutil: str,
    app_path: Path,
    dmg_path: Path,
    volume_name: str,
    force: bool,
) -> list[str]:
    command = [
        hdiutil,
        "create",
        "-format",
        "UDZO",
        "-volname",
        volume_name,
        "-srcfolder",
        str(app_path),
    ]
    if force:
        command.append("-ov")
    command.append(str(dmg_path))
    return command


def _validate_created_dmg(dmg_path: Path) -> None:
    if dmg_path.is_symlink() or not dmg_path.is_file():
        raise RuntimeError("hdiutil completed but DMG output was not created")
    if dmg_path.stat().st_size <= 0:
        raise RuntimeError("hdiutil completed but DMG output is empty")


def _summarize_validation(validation: LauncherAppValidationReport) -> dict[str, object]:
    return {
        "status": validation.status,
        "local_ready": validation.local_ready,
        "distribution_ready": validation.distribution_ready,
        "checks": [
            {"id": check.id, "status": check.status, "ok": check.ok} for check in validation.checks
        ],
    }


def _redact_secret(value: str) -> str:
    if any(marker in value for marker in _FORBIDDEN_SECRET_MARKERS):
        return "<redacted>"
    if _HF_TOKEN_VALUE_RE.search(value):
        return "<redacted>"
    return value


def _redact_json_payload(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_secret(value)
    if isinstance(value, list):
        return [_redact_json_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_json_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_json_payload(item) for key, item in value.items()}
    return value


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI 인자를 파싱한다."""
    parser = argparse.ArgumentParser(description="Recap unsigned launcher DMG 생성")
    parser.add_argument("--app-path", type=Path, required=True, help="패키징할 .app 경로")
    parser.add_argument("--output-path", type=Path, default=None, help="생성할 .dmg 경로")
    parser.add_argument("--output-dir", type=Path, default=None, help="생성할 .dmg 디렉토리")
    parser.add_argument("--volume-name", default=DEFAULT_VOLUME_NAME, help="DMG 볼륨 이름")
    parser.add_argument("--force", action="store_true", help="기존 DMG 덮어쓰기")
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = _parse_args(argv)
    try:
        result = build_launcher_dmg(
            app_path=args.app_path,
            output_path=args.output_path,
            output_dir=args.output_dir,
            volume_name=args.volume_name,
            force=args.force,
        )
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError) as exc:
        if args.json:
            payload = _redact_json_payload(
                {
                    "success": False,
                    "error": {
                        "type": exc.__class__.__name__,
                        "message": str(exc),
                    },
                }
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1
        raise
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(result.dmg_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
