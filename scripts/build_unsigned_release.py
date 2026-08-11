#!/usr/bin/env python3
"""
Recap unsigned local release 산출물 조립기.

목적: 기존 safe builder들을 순서대로 호출해 `.app`, `.dmg`, manifest를
      지정 output 디렉토리 안에 만든다. 앱 실행, DMG mount/open, 서명,
      공증, 설치, 네트워크 작업은 수행하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_launcher_app import (
    DEFAULT_APP_NAME,
    DEFAULT_BUNDLE_ID,
    DEFAULT_VERSION,
    LauncherAppBuildResult,
    build_launcher_app,
)
from scripts.build_launcher_dmg import (
    DEFAULT_VOLUME_NAME,
    LauncherDmgBuildResult,
    build_launcher_dmg,
)
from scripts.build_release_manifest import ReleaseManifest, build_release_manifest

RELEASE_TYPE = "unsigned_local"
_FORBIDDEN_SECRET_MARKERS = ("HUGGINGFACE_TOKEN", "HF_TOKEN", "hf_")
_HF_TOKEN_VALUE_RE = re.compile(r"(?<![A-Za-z0-9_])hf_[A-Za-z0-9][A-Za-z0-9_-]{7,}")


@dataclass(frozen=True)
class UnsignedReleaseBuildResult:
    """생성된 unsigned local release 산출물 정보."""

    output_dir: Path
    app: LauncherAppBuildResult
    dmg: LauncherDmgBuildResult
    manifest_path: Path
    manifest: ReleaseManifest

    def to_dict(self) -> dict[str, object]:
        """JSON 출력용 dict로 변환한다."""
        return _redact_json_payload(
            {
                "success": True,
                "release_type": RELEASE_TYPE,
                "output_dir": str(self.output_dir),
                "app_path": str(self.app.app_path),
                "dmg_path": str(self.dmg.dmg_path),
                "manifest_path": str(self.manifest_path),
                "local_ready": self.manifest.local_ready,
                "distribution_ready": self.manifest.distribution_ready,
                "artifacts": {
                    "app": self.app.to_dict(),
                    "dmg": self.dmg.to_dict(),
                    "manifest": self.manifest.to_dict(),
                },
            }
        )


def build_unsigned_release(
    *,
    output_dir: Path | str,
    project_dir: Path | str | None = None,
    python_executable: Path | str | None = None,
    app_name: str = DEFAULT_APP_NAME,
    bundle_id: str = DEFAULT_BUNDLE_ID,
    version: str = DEFAULT_VERSION,
    host: str = "127.0.0.1",
    port: int = 8765,
    volume_name: str = DEFAULT_VOLUME_NAME,
    force: bool = False,
    bundle_source: bool = False,
) -> UnsignedReleaseBuildResult:
    """Unsigned local release 산출물을 조립한다.

    Args:
        output_dir: 산출물을 둘 디렉토리.
        project_dir: Recap 프로젝트 루트.
        python_executable: 서버 실행 Python.
        app_name: Finder에 표시할 앱 이름.
        bundle_id: CFBundleIdentifier.
        version: 앱 버전.
        host: 로컬 서버 host.
        port: 로컬 서버 port.
        volume_name: DMG 볼륨 이름.
        force: 기존 산출물을 교체할지 여부.
        bundle_source: 런타임 프로젝트 소스 스냅샷을 `.app`에 포함할지 여부.

    Returns:
        UnsignedReleaseBuildResult.
    """
    expanded_output_dir = Path(output_dir).expanduser()
    if expanded_output_dir.is_symlink():
        raise ValueError(f"output_dir must not be a symlink: {expanded_output_dir}")
    if expanded_output_dir.exists() and not expanded_output_dir.is_dir():
        raise ValueError(f"output_dir must be a directory: {expanded_output_dir}")
    resolved_output_dir = expanded_output_dir.resolve(strict=False)
    targets = _resolve_targets(resolved_output_dir, app_name)
    _validate_output_targets(targets=targets, output_dir=resolved_output_dir, force=force)

    app = build_launcher_app(
        output_dir=resolved_output_dir,
        project_dir=project_dir,
        python_executable=python_executable,
        app_name=app_name,
        bundle_id=bundle_id,
        version=version,
        host=host,
        port=port,
        force=force,
        bundle_source=bundle_source,
    )
    dmg = build_launcher_dmg(
        app_path=app.app_path,
        output_path=targets["dmg"],
        volume_name=volume_name,
        force=force,
    )
    manifest = build_release_manifest(app_path=app.app_path, dmg_path=dmg.dmg_path)
    _write_manifest_atomically(
        manifest=manifest,
        manifest_path=targets["manifest"],
        output_dir=resolved_output_dir,
        force=force,
    )
    return UnsignedReleaseBuildResult(
        output_dir=resolved_output_dir,
        app=app,
        dmg=dmg,
        manifest_path=targets["manifest"],
        manifest=manifest,
    )


def _resolve_targets(output_dir: Path, app_name: str) -> dict[str, Path]:
    if "/" in app_name or app_name.endswith(".app") or not app_name.strip():
        raise ValueError(
            "app_name must be a non-empty bundle display name without slashes or .app"
        )
    return {
        "app": output_dir / f"{app_name}.app",
        "dmg": output_dir / f"{app_name}.dmg",
        "manifest": output_dir / f"{app_name}.release-manifest.json",
    }


def _validate_output_targets(
    *,
    targets: dict[str, Path],
    output_dir: Path,
    force: bool,
) -> None:
    resolved_output_dir = output_dir.resolve(strict=False)
    for name, target in targets.items():
        if target.is_symlink():
            raise FileExistsError(f"{name} output must not be a symlink: {target}")
        if not _is_relative_to(target.resolve(strict=False), resolved_output_dir):
            raise ValueError(f"{name} output must stay inside output_dir: {target}")
        if target.exists():
            if not force:
                raise FileExistsError(f"{name} output already exists: {target}")
            if name == "app":
                if not target.is_dir():
                    raise FileExistsError(f"{name} output must be an app directory: {target}")
            elif not target.is_file():
                raise FileExistsError(f"{name} output must be a regular file: {target}")


def _write_manifest_atomically(
    *,
    manifest: ReleaseManifest,
    manifest_path: Path,
    output_dir: Path,
    force: bool,
) -> None:
    _validate_manifest_target(manifest_path=manifest_path, output_dir=output_dir, force=force)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = manifest_path.with_name(f".{manifest_path.name}.tmp")
    if temp_path.exists() or temp_path.is_symlink():
        raise FileExistsError(f"temporary manifest path already exists: {temp_path}")
    payload = {
        "release_type": RELEASE_TYPE,
        "manifest": manifest.to_dict(),
    }
    temp_path.write_text(
        json.dumps(_redact_json_payload(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(manifest_path)


def _validate_manifest_target(*, manifest_path: Path, output_dir: Path, force: bool) -> None:
    resolved_output_dir = output_dir.resolve(strict=False)
    if not _is_relative_to(manifest_path.resolve(strict=False), resolved_output_dir):
        raise ValueError(f"manifest output must stay inside output_dir: {manifest_path}")
    if manifest_path.is_symlink():
        raise FileExistsError(f"manifest output must not be a symlink: {manifest_path}")
    if manifest_path.is_dir():
        raise FileExistsError(f"manifest output must not be a directory: {manifest_path}")
    if manifest_path.exists() and not manifest_path.is_file():
        raise FileExistsError(f"manifest output must be a regular file: {manifest_path}")
    if manifest_path.exists() and not force:
        raise FileExistsError(f"manifest output already exists: {manifest_path}")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


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
    parser = argparse.ArgumentParser(description="Recap unsigned local release 생성")
    parser.add_argument("--output-dir", type=Path, default=Path("dist"), help="출력 디렉토리")
    parser.add_argument("--project-dir", type=Path, default=None, help="Recap 프로젝트 루트")
    parser.add_argument("--python", type=Path, default=None, help="서버 실행 Python 경로")
    parser.add_argument("--app-name", default=DEFAULT_APP_NAME, help="앱 이름")
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID, help="CFBundleIdentifier")
    parser.add_argument("--version", default=DEFAULT_VERSION, help="앱 버전")
    parser.add_argument("--host", default="127.0.0.1", help="로컬 서버 host")
    parser.add_argument("--port", type=int, default=8765, help="로컬 서버 port")
    parser.add_argument("--volume-name", default=DEFAULT_VOLUME_NAME, help="DMG 볼륨 이름")
    parser.add_argument("--force", action="store_true", help="기존 산출물 교체")
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
    try:
        result = build_unsigned_release(
            output_dir=args.output_dir,
            project_dir=args.project_dir,
            python_executable=args.python,
            app_name=args.app_name,
            bundle_id=args.bundle_id,
            version=args.version,
            host=args.host,
            port=args.port,
            volume_name=args.volume_name,
            force=args.force,
            bundle_source=args.bundle_source,
        )
    except (FileExistsError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        if args.json:
            payload = _redact_json_payload(
                {
                    "success": False,
                    "release_type": RELEASE_TYPE,
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
        print(result.manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
