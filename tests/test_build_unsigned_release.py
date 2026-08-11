"""Recap unsigned local release 조립기 테스트."""

from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import sys
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


def _make_bundle_project(tmp_path: Path) -> tuple[Path, Path]:
    """번들 소스 테스트용 프로젝트 구조를 만든다."""
    project_dir, python = _make_project(tmp_path)
    for filename in ("config.py", "pyproject.toml", "requirements.txt"):
        (project_dir / filename).write_text(f"# {filename}\n", encoding="utf-8")
    (project_dir / "config.yaml").write_text(
        "diarization:\n  huggingface_token: hf_secretvalue\n  block_hf_offline_cache_miss: true\n",
        encoding="utf-8",
    )
    for dirname in ("api", "core", "steps", "search", "security", "ui"):
        package_dir = project_dir / dirname
        package_dir.mkdir()
        (package_dir / "__init__.py").write_text("# package\n", encoding="utf-8")
    web_dir = project_dir / "ui" / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    return project_dir, python


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


def test_build_unsigned_release_creates_app_dmg_and_manifest_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """기존 safe builder들을 순서대로 호출해 세 산출물을 만든다."""
    import scripts.build_unsigned_release as builder

    project_dir, python = _make_project(tmp_path)
    calls: list[str] = []
    original_build_app = builder.build_launcher_app
    original_build_dmg = builder.build_launcher_dmg
    original_build_manifest = builder.build_release_manifest

    def spy_build_app(**kwargs):
        calls.append("app")
        return original_build_app(**kwargs)

    def spy_build_dmg(**kwargs):
        calls.append("dmg")
        return original_build_dmg(**kwargs)

    def spy_build_manifest(**kwargs):
        calls.append("manifest")
        return original_build_manifest(**kwargs)

    def fake_run(command, **kwargs) -> subprocess.CompletedProcess[str]:
        Path(command[-1]).write_bytes(b"fake dmg")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(builder, "build_launcher_app", spy_build_app)
    monkeypatch.setattr(builder, "build_launcher_dmg", spy_build_dmg)
    monkeypatch.setattr(builder, "build_release_manifest", spy_build_manifest)
    monkeypatch.setattr("scripts.build_launcher_dmg.shutil.which", lambda _: "/usr/bin/hdiutil")
    monkeypatch.setattr("scripts.build_launcher_dmg.subprocess.run", fake_run)

    result = builder.build_unsigned_release(
        output_dir=tmp_path / "dist",
        project_dir=project_dir,
        python_executable=python,
        app_name="Recap Test",
        bundle_id="com.recap.test",
        version="1.2.3",
    )
    payload = result.to_dict()
    manifest_payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert calls == ["app", "dmg", "manifest"]
    assert result.app.app_path == (tmp_path / "dist" / "Recap Test.app").resolve()
    assert result.dmg.dmg_path == (tmp_path / "dist" / "Recap Test.dmg").resolve()
    assert (
        result.manifest_path == (tmp_path / "dist" / "Recap Test.release-manifest.json").resolve()
    )
    assert result.app.app_path.is_dir()
    assert result.dmg.dmg_path.is_file()
    assert result.manifest_path.is_file()
    assert payload["release_type"] == "unsigned_local"
    assert payload["local_ready"] is True
    assert payload["distribution_ready"] is False
    assert manifest_payload["release_type"] == "unsigned_local"
    assert manifest_payload["manifest"]["local_ready"] is True
    assert manifest_payload["manifest"]["distribution_ready"] is False


def test_build_unsigned_release_cli_json_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    """CLI JSON은 주요 산출물 path와 readiness를 노출한다."""
    import scripts.build_unsigned_release as builder

    project_dir, python = _make_project(tmp_path)

    def fake_run(command, **kwargs) -> subprocess.CompletedProcess[str]:
        Path(command[-1]).write_bytes(b"fake dmg")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("scripts.build_launcher_dmg.shutil.which", lambda _: "/usr/bin/hdiutil")
    monkeypatch.setattr("scripts.build_launcher_dmg.subprocess.run", fake_run)

    exit_code = builder.main(
        [
            "--output-dir",
            str(tmp_path / "dist"),
            "--project-dir",
            str(project_dir),
            "--python",
            str(python),
            "--app-name",
            "Recap Test",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["release_type"] == "unsigned_local"
    assert payload["app_path"].endswith("Recap Test.app")
    assert payload["dmg_path"].endswith("Recap Test.dmg")
    assert payload["manifest_path"].endswith("Recap Test.release-manifest.json")
    assert payload["local_ready"] is True
    assert payload["distribution_ready"] is False


def test_build_unsigned_release_direct_script_invocation_matches_readme(
    tmp_path: Path,
) -> None:
    """README에 문서화된 직접 script 실행 형태가 동작한다."""
    project_dir, python = _make_project(tmp_path)
    output_dir = tmp_path / "dist"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_hdiutil = fake_bin / "hdiutil"
    fake_hdiutil.write_text(
        "#!/bin/sh\n"
        'last=""\n'
        'for arg in "$@"; do last="$arg"; done\n'
        "printf 'fake dmg' > \"$last\"\n",
        encoding="utf-8",
    )
    fake_hdiutil.chmod(0o755)
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            sys.executable,
            "scripts/build_unsigned_release.py",
            "--output-dir",
            str(output_dir),
            "--project-dir",
            str(project_dir),
            "--python",
            str(python),
            "--force",
            "--json",
        ],
        cwd=repo_root,
        env={"PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    payload = json.loads(result.stdout)

    assert result.returncode == 0, result.stderr
    assert payload["success"] is True
    assert payload["release_type"] == "unsigned_local"
    assert payload["app_path"].endswith("Recap.app")
    assert payload["dmg_path"].endswith("Recap.dmg")
    assert payload["manifest_path"].endswith("Recap.release-manifest.json")
    assert output_dir.joinpath("Recap.app").is_dir()
    assert output_dir.joinpath("Recap.dmg").read_bytes() == b"fake dmg"
    assert output_dir.joinpath("Recap.release-manifest.json").is_file()


def test_build_unsigned_release_rejects_existing_outputs_without_force(
    tmp_path: Path,
) -> None:
    """기존 app/dmg/manifest는 force 없이는 덮어쓰지 않는다."""
    from scripts.build_unsigned_release import build_unsigned_release

    project_dir, python = _make_project(tmp_path)
    output_dir = tmp_path / "dist"
    output_dir.mkdir()
    cases = [
        output_dir / "Recap.app",
        output_dir / "Recap.dmg",
        output_dir / "Recap.release-manifest.json",
    ]
    for target in cases:
        if target.suffix == ".app":
            target.mkdir()
        else:
            target.write_text("old\n", encoding="utf-8")
        with pytest.raises(FileExistsError):
            build_unsigned_release(
                output_dir=output_dir,
                project_dir=project_dir,
                python_executable=python,
            )
        if target.is_dir():
            target.rmdir()
        else:
            target.unlink()


def test_build_unsigned_release_force_rejects_symlink_and_bad_target_types(
    tmp_path: Path,
) -> None:
    """force=True이어도 symlink와 잘못된 target type은 거부한다."""
    from scripts.build_unsigned_release import build_unsigned_release

    project_dir, python = _make_project(tmp_path)
    output_dir = tmp_path / "dist"
    output_dir.mkdir()

    symlink_app = output_dir / "Recap.app"
    symlink_app.symlink_to(tmp_path / "Real.app", target_is_directory=True)
    with pytest.raises(FileExistsError, match="symlink"):
        build_unsigned_release(
            output_dir=output_dir,
            project_dir=project_dir,
            python_executable=python,
            force=True,
        )
    symlink_app.unlink()

    bad_dmg = output_dir / "Recap.dmg"
    bad_dmg.mkdir()
    with pytest.raises(FileExistsError, match="regular file"):
        build_unsigned_release(
            output_dir=output_dir,
            project_dir=project_dir,
            python_executable=python,
            force=True,
        )
    bad_dmg.rmdir()

    bad_manifest = output_dir / "Recap.release-manifest.json"
    bad_manifest.mkdir()
    with pytest.raises(FileExistsError, match="regular file"):
        build_unsigned_release(
            output_dir=output_dir,
            project_dir=project_dir,
            python_executable=python,
            force=True,
        )


def test_build_unsigned_release_force_overwrites_regular_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """force=True은 output_dir 안의 예상 regular 산출물만 교체한다."""
    from scripts.build_unsigned_release import build_unsigned_release

    project_dir, python = _make_project(tmp_path)
    output_dir = tmp_path / "dist"
    output_dir.mkdir()
    (output_dir / "Recap.dmg").write_text("old dmg\n", encoding="utf-8")
    (output_dir / "Recap.release-manifest.json").write_text("old manifest\n", encoding="utf-8")

    def fake_run(command, **kwargs) -> subprocess.CompletedProcess[str]:
        Path(command[-1]).write_bytes(b"new dmg")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("scripts.build_launcher_dmg.shutil.which", lambda _: "/usr/bin/hdiutil")
    monkeypatch.setattr("scripts.build_launcher_dmg.subprocess.run", fake_run)

    result = build_unsigned_release(
        output_dir=output_dir,
        project_dir=project_dir,
        python_executable=python,
        force=True,
    )

    assert result.dmg.dmg_path.read_bytes() == b"new dmg"
    manifest_payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest_payload["release_type"] == "unsigned_local"


def test_build_unsigned_release_rejects_output_dir_symlink_escape(tmp_path: Path) -> None:
    """output_dir symlink를 통해 다른 위치에 산출물을 만들지 않는다."""
    from scripts.build_unsigned_release import build_unsigned_release

    project_dir, python = _make_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    output_link = tmp_path / "dist-link"
    output_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="output_dir must not be a symlink"):
        build_unsigned_release(
            output_dir=output_link,
            project_dir=project_dir,
            python_executable=python,
            force=True,
        )


def test_build_unsigned_release_rejects_output_dir_regular_file(tmp_path: Path) -> None:
    """output_dir 자체가 파일이면 builder가 명확히 거부한다."""
    from scripts.build_unsigned_release import build_unsigned_release

    project_dir, python = _make_project(tmp_path)
    output_file = tmp_path / "dist"
    output_file.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(ValueError, match="output_dir must be a directory"):
        build_unsigned_release(
            output_dir=output_file,
            project_dir=project_dir,
            python_executable=python,
            force=True,
        )


def test_build_unsigned_release_dmg_failure_does_not_write_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DMG 단계 실패 시 manifest를 쓰지 않는다."""
    from scripts.build_unsigned_release import build_unsigned_release

    project_dir, python = _make_project(tmp_path)
    output_dir = tmp_path / "dist"
    manifest_path = output_dir / "Recap.release-manifest.json"

    monkeypatch.setattr("scripts.build_launcher_dmg.shutil.which", lambda _: None)

    with pytest.raises(FileNotFoundError):
        build_unsigned_release(
            output_dir=output_dir,
            project_dir=project_dir,
            python_executable=python,
        )

    assert not manifest_path.exists()


def test_build_unsigned_release_cli_json_redacts_secret_failure(
    tmp_path: Path,
    capsys,
) -> None:
    """실패 JSON은 secret marker가 포함된 경로를 노출하지 않는다."""
    from scripts.build_unsigned_release import main

    secret_output = tmp_path / "HF_TOKEN_dist"
    secret_output.mkdir()
    (secret_output / "Recap.release-manifest.json").write_text("old\n", encoding="utf-8")

    exit_code = main(["--output-dir", str(secret_output), "--json"])
    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["release_type"] == "unsigned_local"
    assert payload["error"]["message"] == "<redacted>"
    assert "HUGGINGFACE_TOKEN" not in payload_text
    assert "HF_TOKEN" not in payload_text


def test_build_unsigned_release_cli_json_redacts_filesystem_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    """OSError 계열 실패도 --json에서 secret-safe로 보고한다."""
    import scripts.build_unsigned_release as builder

    def fail_build_app(**kwargs):
        raise PermissionError("HF_TOKEN denied")

    monkeypatch.setattr(builder, "build_launcher_app", fail_build_app)

    exit_code = builder.main(
        [
            "--output-dir",
            str(tmp_path / "dist"),
            "--json",
        ]
    )
    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["error"]["type"] == "PermissionError"
    assert payload["error"]["message"] == "<redacted>"
    assert "HF_TOKEN" not in payload_text


def test_build_unsigned_release_bundle_source_does_not_mutate_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--bundle-source도 project source 파일을 수정하지 않는다."""
    from scripts.build_unsigned_release import build_unsigned_release

    project_dir, python = _make_bundle_project(tmp_path)
    before = _tree_snapshot(project_dir)

    def fake_run(command, **kwargs) -> subprocess.CompletedProcess[str]:
        Path(command[-1]).write_bytes(b"fake dmg")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("scripts.build_launcher_dmg.shutil.which", lambda _: "/usr/bin/hdiutil")
    monkeypatch.setattr("scripts.build_launcher_dmg.subprocess.run", fake_run)

    result = build_unsigned_release(
        output_dir=tmp_path / "dist",
        project_dir=project_dir,
        python_executable=python,
        bundle_source=True,
    )

    assert result.manifest.local_ready is True
    assert _tree_snapshot(project_dir) == before


def test_build_unsigned_release_module_has_no_shell_launch_signing_or_network_apis() -> None:
    """통합 builder는 shell/app 실행/서명/공증/네트워크 API를 직접 사용하지 않는다."""
    import scripts.build_unsigned_release as builder

    source = inspect.getsource(builder)

    assert "subprocess" not in source
    assert "Popen" not in source
    assert "webbrowser" not in source
    assert "urllib" not in source
    assert "requests" not in source
    assert "open " not in source
    assert "attach" not in source
    assert "notarytool" not in source
    assert "stapler" not in source
    assert "pip install" not in source
    assert "brew install" not in source
    assert "shell=True" not in source
