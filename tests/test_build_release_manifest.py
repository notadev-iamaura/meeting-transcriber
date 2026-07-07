"""Recap unsigned local release manifest 테스트."""

from __future__ import annotations

import hashlib
import inspect
import json
import plistlib
from pathlib import Path

import pytest


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    """테스트용 Recap 프로젝트와 Python 실행 파일을 만든다."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "main.py").write_text("# test entrypoint\n", encoding="utf-8")

    python = tmp_path / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    return project_dir, python


def _build_app(tmp_path: Path) -> Path:
    """테스트용 unsigned `.app` 번들을 생성한다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_project(tmp_path)
    result = build_launcher_app(
        output_dir=tmp_path / "dist",
        project_dir=project_dir,
        python_executable=python,
        app_name="Recap Test",
        bundle_id="com.recap.test",
        version="1.2.3",
    )
    return result.app_path


def _write_dmg(path: Path, content: bytes = b"fake dmg\n") -> Path:
    """테스트용 non-empty DMG 파일을 쓴다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    """파일 내용과 mtime 스냅샷을 만든다."""
    snapshot = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        stat_result = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshot[str(path.relative_to(root))] = (
            stat_result.st_mode,
            stat_result.st_mtime_ns,
            digest,
        )
    return snapshot


def test_build_release_manifest_accepts_unsigned_local_ready_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """unsigned local-ready `.app`와 non-empty `.dmg`의 manifest를 생성한다."""
    import scripts.build_release_manifest as builder

    app_path = _build_app(tmp_path)
    dmg_path = _write_dmg(tmp_path / "dist" / "Recap Test.dmg", b"release artifact")
    original_validate = builder.validate_launcher_app
    calls: list[bool] = []

    def spy_validate_launcher_app(app_path_arg, *, check_codesign: bool = True):
        calls.append(check_codesign)
        return original_validate(app_path_arg, check_codesign=check_codesign)

    monkeypatch.setattr(builder, "validate_launcher_app", spy_validate_launcher_app)

    manifest = builder.build_release_manifest(
        app_path=app_path,
        dmg_path=dmg_path,
        generated_at="2026-07-07T00:00:00+00:00",
    )
    payload = manifest.to_dict()

    assert calls == [True]
    assert payload["kind"] == "recap.unsigned-local-release-manifest"
    assert payload["manifest_version"] == 1
    assert payload["generated_at"] == "2026-07-07T00:00:00+00:00"
    assert payload["local_ready"] is True
    assert payload["distribution_ready"] is False
    assert payload["validation"]["local_ready"] is True
    assert payload["validation"]["distribution_ready"] is False
    assert payload["validation"]["codesign"]["status"] in {"warn", "pass"}
    assert payload["artifacts"]["app"]["type"] == "app"
    assert payload["artifacts"]["app"]["path"] == str(app_path.resolve())
    assert payload["artifacts"]["app"]["byte_size"] > 0
    assert payload["artifacts"]["app"]["file_count"] >= 3
    assert len(payload["artifacts"]["app"]["sha256"]) == 64
    assert payload["artifacts"]["dmg"] == {
        "type": "dmg",
        "path": str(dmg_path.resolve()),
        "byte_size": len(b"release artifact"),
        "sha256": _file_sha256(dmg_path),
    }


def test_build_release_manifest_rejects_invalid_app_before_dmg_validation(
    tmp_path: Path,
) -> None:
    """invalid `.app`은 manifest 생성 전에 명확히 실패한다."""
    from scripts.build_release_manifest import build_release_manifest

    app_path = _build_app(tmp_path)
    dmg_path = tmp_path / "dist" / "Linked.dmg"
    dmg_path.symlink_to(tmp_path / "target.dmg")
    (app_path / "Contents" / "Info.plist").unlink()

    with pytest.raises(ValueError, match="app bundle validation failed"):
        build_release_manifest(app_path=app_path, dmg_path=dmg_path)


def test_build_release_manifest_rejects_invalid_executable_syntax(
    tmp_path: Path,
) -> None:
    """손상된 launcher executable은 release manifest local_ready를 통과하지 못한다."""
    from scripts.build_release_manifest import build_release_manifest

    app_path = _build_app(tmp_path)
    dmg_path = _write_dmg(tmp_path / "dist" / "Recap Test.dmg")
    executable_path = app_path / "Contents" / "MacOS" / "Recap Test"
    with executable_path.open("a", encoding="utf-8") as fh:
        fh.write("\nif [[\n")

    with pytest.raises(ValueError, match="executable_syntax"):
        build_release_manifest(app_path=app_path, dmg_path=dmg_path)


def test_build_release_manifest_rejects_invalid_dmg_artifacts(tmp_path: Path) -> None:
    """DMG는 존재하는 일반 non-empty 파일이어야 한다."""
    from scripts.build_release_manifest import build_release_manifest

    app_path = _build_app(tmp_path)
    missing = tmp_path / "missing.dmg"
    empty = tmp_path / "empty.dmg"
    empty.write_bytes(b"")
    directory = tmp_path / "directory.dmg"
    directory.mkdir()
    symlink = tmp_path / "linked.dmg"
    symlink.symlink_to(tmp_path / "target.dmg")
    wrong_suffix = tmp_path / "Recap.zip"
    wrong_suffix.write_bytes(b"zip")

    cases = [
        (missing, "does not exist"),
        (empty, "empty"),
        (directory, "directory"),
        (symlink, "symlink"),
        (wrong_suffix, "must end with .dmg"),
    ]
    for dmg_path, message in cases:
        with pytest.raises(ValueError, match=message):
            build_release_manifest(app_path=app_path, dmg_path=dmg_path)


def test_build_release_manifest_hashes_are_deterministic_and_change_on_mutation(
    tmp_path: Path,
) -> None:
    """파일 순서가 고정되어 있고 산출물 변경은 hash에 반영된다."""
    from scripts.build_release_manifest import build_release_manifest

    app_path = _build_app(tmp_path)
    dmg_path = _write_dmg(tmp_path / "dist" / "Recap Test.dmg", b"first dmg")

    first = build_release_manifest(
        app_path=app_path,
        dmg_path=dmg_path,
        generated_at="2026-07-07T00:00:00+00:00",
    ).to_dict()
    second = build_release_manifest(
        app_path=app_path,
        dmg_path=dmg_path,
        generated_at="2026-07-07T00:00:00+00:00",
    ).to_dict()

    assert second["artifacts"] == first["artifacts"]

    dmg_path.write_bytes(b"second dmg")
    changed_dmg = build_release_manifest(
        app_path=app_path,
        dmg_path=dmg_path,
        generated_at="2026-07-07T00:00:00+00:00",
    ).to_dict()
    assert changed_dmg["artifacts"]["dmg"]["sha256"] != first["artifacts"]["dmg"]["sha256"]

    with (app_path / "Contents" / "Info.plist").open("rb") as fh:
        plist = plistlib.load(fh)
    plist["CFBundleVersion"] = "1.2.4"
    with (app_path / "Contents" / "Info.plist").open("wb") as fh:
        plistlib.dump(plist, fh)
    changed_app = build_release_manifest(
        app_path=app_path,
        dmg_path=dmg_path,
        generated_at="2026-07-07T00:00:00+00:00",
    ).to_dict()
    assert changed_app["artifacts"]["app"]["sha256"] != first["artifacts"]["app"]["sha256"]


def test_build_release_manifest_does_not_mutate_app_or_dmg(tmp_path: Path) -> None:
    """manifest 생성은 입력 app/dmg 파일을 읽기만 한다."""
    from scripts.build_release_manifest import build_release_manifest

    app_path = _build_app(tmp_path)
    dmg_path = _write_dmg(tmp_path / "dist" / "Recap Test.dmg", b"fake dmg")
    app_before = _tree_snapshot(app_path)
    dmg_before = (
        dmg_path.stat().st_mode,
        dmg_path.stat().st_mtime_ns,
        _file_sha256(dmg_path),
    )

    manifest = build_release_manifest(app_path=app_path, dmg_path=dmg_path)

    assert manifest.local_ready is True
    assert _tree_snapshot(app_path) == app_before
    assert (
        dmg_path.stat().st_mode,
        dmg_path.stat().st_mtime_ns,
        _file_sha256(dmg_path),
    ) == dmg_before


def test_build_release_manifest_cli_outputs_json(tmp_path: Path, capsys) -> None:
    """CLI는 manifest를 JSON으로 출력한다."""
    from scripts.build_release_manifest import main

    app_path = _build_app(tmp_path)
    dmg_path = _write_dmg(tmp_path / "dist" / "Recap Test.dmg")

    exit_code = main(
        [
            "--app-path",
            str(app_path),
            "--dmg-path",
            str(dmg_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["manifest"]["local_ready"] is True
    assert payload["manifest"]["distribution_ready"] is False
    assert payload["manifest"]["artifacts"]["dmg"]["path"] == str(dmg_path.resolve())


def test_build_release_manifest_cli_json_redacts_secret_values(
    tmp_path: Path,
    capsys,
) -> None:
    """성공 JSON은 path의 token marker를 redaction한다."""
    from scripts.build_release_manifest import main

    app_path = _build_app(tmp_path)
    secret_root = tmp_path / "HF_TOKEN_release"
    secret_root.mkdir()
    dmg_path = _write_dmg(secret_root / "hf_secretvalue.dmg")

    exit_code = main(
        [
            "--app-path",
            str(app_path),
            "--dmg-path",
            str(dmg_path),
            "--json",
        ]
    )
    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["manifest"]["artifacts"]["app"]["path"] == str(app_path.resolve())
    assert payload["manifest"]["artifacts"]["dmg"]["path"] == "<redacted>"
    assert "HUGGINGFACE_TOKEN" not in payload_text
    assert "HF_TOKEN" not in payload_text
    assert "hf_secretvalue" not in payload_text


def test_build_release_manifest_cli_json_redacts_secret_path_on_failure(
    tmp_path: Path,
    capsys,
) -> None:
    """실패 JSON도 secret marker가 포함된 path를 노출하지 않는다."""
    from scripts.build_release_manifest import main

    app_path = _build_app(tmp_path)
    secret_root = tmp_path / "HF_TOKEN_workspace"
    secret_root.mkdir()
    exit_code = main(
        [
            "--app-path",
            str(app_path),
            "--dmg-path",
            str(secret_root / "Missing.dmg"),
            "--json",
        ]
    )
    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["error"]["message"] == "<redacted>"
    assert "HUGGINGFACE_TOKEN" not in payload_text
    assert "HF_TOKEN" not in payload_text


def test_build_release_manifest_module_has_no_launch_signing_or_network_apis() -> None:
    """manifest builder는 실행/마운트/서명/공증/네트워크 API를 사용하지 않는다."""
    import scripts.build_release_manifest as builder

    source = inspect.getsource(builder)

    assert "subprocess" not in source
    assert "Popen" not in source
    assert "webbrowser" not in source
    assert "urllib" not in source
    assert "requests" not in source
    assert "hdiutil" not in source
    assert "attach" not in source
    assert "notarytool" not in source
    assert "stapler" not in source
    assert "pip install" not in source
    assert "brew install" not in source
    assert "shell=True" not in source
