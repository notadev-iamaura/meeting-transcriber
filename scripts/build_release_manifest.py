#!/usr/bin/env python3
"""
Recap unsigned local release manifest 생성기.

목적: 이미 생성된 `.app`과 `.dmg` 산출물을 read-only로 식별하고
      무결성/ready 상태를 JSON으로 기록한다. 앱 실행, DMG mount,
      서명, 공증, 설치, 네트워크 작업은 수행하지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.validate_launcher_app import LauncherAppValidationReport, validate_launcher_app

MANIFEST_KIND = "recap.unsigned-local-release-manifest"
MANIFEST_VERSION = 1
_FORBIDDEN_SECRET_MARKERS = ("HUGGINGFACE_TOKEN", "HF_TOKEN", "hf_")
_HF_TOKEN_VALUE_RE = re.compile(r"(?<![A-Za-z0-9_])hf_[A-Za-z0-9][A-Za-z0-9_-]{7,}")


@dataclass(frozen=True)
class ArtifactDigest:
    """산출물 무결성 정보."""

    path: Path
    artifact_type: str
    byte_size: int
    sha256: str
    file_count: int | None = None

    def to_dict(self) -> dict[str, object]:
        """JSON 출력용 dict로 변환한다."""
        payload: dict[str, object] = {
            "type": self.artifact_type,
            "path": str(self.path),
            "byte_size": self.byte_size,
            "sha256": self.sha256,
        }
        if self.file_count is not None:
            payload["file_count"] = self.file_count
        return payload


@dataclass(frozen=True)
class ReleaseManifest:
    """Unsigned local release manifest."""

    generated_at: str
    app: ArtifactDigest
    dmg: ArtifactDigest
    validation_summary: dict[str, object]

    @property
    def local_ready(self) -> bool:
        """로컬 실행 후보로 볼 수 있는지 반환한다."""
        return bool(self.validation_summary["local_ready"])

    @property
    def distribution_ready(self) -> bool:
        """서명/공증 포함 배포 후보인지 반환한다."""
        return bool(self.validation_summary["distribution_ready"])

    def to_dict(self) -> dict[str, object]:
        """JSON 출력용 dict로 변환한다."""
        return _redact_json_payload(
            {
                "kind": MANIFEST_KIND,
                "manifest_version": MANIFEST_VERSION,
                "generated_at": self.generated_at,
                "local_ready": self.local_ready,
                "distribution_ready": self.distribution_ready,
                "artifacts": {
                    "app": self.app.to_dict(),
                    "dmg": self.dmg.to_dict(),
                },
                "validation": self.validation_summary,
            }
        )


def build_release_manifest(
    *,
    app_path: Path | str,
    dmg_path: Path | str,
    generated_at: str | None = None,
) -> ReleaseManifest:
    """Unsigned local release manifest를 생성한다.

    Args:
        app_path: 검증할 `.app` bundle 경로.
        dmg_path: 검증할 `.dmg` 파일 경로.
        generated_at: 테스트용 timestamp override.

    Returns:
        ReleaseManifest.

    Raises:
        ValueError: `.app` 또는 `.dmg` 계약이 유효하지 않을 때.
    """
    resolved_app_path = Path(app_path).expanduser().resolve(strict=False)
    resolved_dmg_path = _absolute_path(Path(dmg_path).expanduser())

    validation = validate_launcher_app(resolved_app_path, check_codesign=True)
    if not validation.local_ready:
        failed = ", ".join(check.id for check in validation.checks if not check.ok)
        raise ValueError(f"app bundle validation failed: {failed}")

    _validate_dmg_artifact(resolved_dmg_path)
    resolved_dmg_path = resolved_dmg_path.resolve(strict=False)
    timestamp = generated_at or datetime.now(UTC).replace(microsecond=0).isoformat()

    return ReleaseManifest(
        generated_at=timestamp,
        app=_digest_app_bundle(resolved_app_path),
        dmg=_digest_file(resolved_dmg_path, artifact_type="dmg"),
        validation_summary=_summarize_validation(validation),
    )


def _validate_dmg_artifact(dmg_path: Path) -> None:
    if dmg_path.suffix != ".dmg":
        raise ValueError("dmg_path must end with .dmg")
    if dmg_path.is_symlink():
        raise ValueError(f"DMG artifact must not be a symlink: {dmg_path}")
    if dmg_path.is_dir():
        raise ValueError(f"DMG artifact must not be a directory: {dmg_path}")
    if not dmg_path.is_file():
        raise ValueError(f"DMG artifact does not exist: {dmg_path}")
    if dmg_path.stat().st_size <= 0:
        raise ValueError(f"DMG artifact is empty: {dmg_path}")


def _absolute_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    return Path.cwd() / path


def _digest_file(path: Path, *, artifact_type: str) -> ArtifactDigest:
    digest = hashlib.sha256()
    total_size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
            total_size += len(chunk)
    return ArtifactDigest(
        path=path,
        artifact_type=artifact_type,
        byte_size=total_size,
        sha256=digest.hexdigest(),
    )


def _digest_app_bundle(app_path: Path) -> ArtifactDigest:
    digest = hashlib.sha256()
    total_size = 0
    file_count = 0
    for path in _iter_bundle_files(app_path):
        relative_path = path.relative_to(app_path).as_posix()
        stat_result = path.stat()
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat_result.st_mode & 0o7777).encode("ascii"))
        digest.update(b"\0")
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
                total_size += len(chunk)
        digest.update(b"\0")
        file_count += 1
    return ArtifactDigest(
        path=app_path,
        artifact_type="app",
        byte_size=total_size,
        sha256=digest.hexdigest(),
        file_count=file_count,
    )


def _iter_bundle_files(app_path: Path) -> list[Path]:
    return sorted(path for path in app_path.rglob("*") if path.is_file() and not path.is_symlink())


def _summarize_validation(validation: LauncherAppValidationReport) -> dict[str, object]:
    codesign = next((check for check in validation.checks if check.id == "codesign"), None)
    return {
        "status": validation.status,
        "local_ready": validation.local_ready,
        "distribution_ready": validation.distribution_ready,
        "codesign": (
            {
                "status": codesign.status,
                "ok": codesign.ok,
                "message": codesign.message,
                "details": dict(codesign.details),
            }
            if codesign is not None
            else None
        ),
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
    parser = argparse.ArgumentParser(description="Recap unsigned local release manifest 생성")
    parser.add_argument("--app-path", type=Path, required=True, help="검증할 .app 경로")
    parser.add_argument("--dmg-path", type=Path, required=True, help="검증할 .dmg 경로")
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = _parse_args(argv)
    try:
        manifest = build_release_manifest(
            app_path=args.app_path,
            dmg_path=args.dmg_path,
        )
    except ValueError as exc:
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

    payload = {"success": True, "manifest": manifest.to_dict()}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(_redact_json_payload(payload), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
