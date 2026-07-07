"""Recap unsigned DMG 패키징 스크립트 테스트."""

from __future__ import annotations

import inspect
import json
import subprocess
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


def _is_bash_syntax_check(command: list[str]) -> bool:
    return command[:2] == ["/bin/bash", "-n"]


def _bash_syntax_success(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, "", "")


def test_build_launcher_dmg_runs_hdiutil_create_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """유효한 `.app`를 hdiutil create 명령으로 DMG에 패키징한다."""
    import scripts.build_launcher_dmg as builder

    app_path = _build_app(tmp_path)
    calls: list[list[str]] = []
    run_kwargs: list[dict[str, object]] = []

    def fake_run(command, **kwargs) -> subprocess.CompletedProcess[str]:
        if _is_bash_syntax_check(list(command)):
            return _bash_syntax_success(list(command))
        calls.append(list(command))
        run_kwargs.append(dict(kwargs))
        Path(command[-1]).write_bytes(b"fake dmg")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(builder.shutil, "which", lambda _: "/usr/bin/hdiutil")
    monkeypatch.setattr(builder.subprocess, "run", fake_run)

    result = builder.build_launcher_dmg(
        app_path=app_path,
        output_dir=tmp_path / "release",
        volume_name="Recap Test",
    )

    assert result.dmg_path == (tmp_path / "release" / "Recap Test.dmg").resolve()
    assert result.app_path == app_path.resolve()
    assert result.volume_name == "Recap Test"
    assert calls == [
        [
            "/usr/bin/hdiutil",
            "create",
            "-format",
            "UDZO",
            "-volname",
            "Recap Test",
            "-srcfolder",
            str(app_path.resolve()),
            str(result.dmg_path),
        ]
    ]
    assert tuple(calls[0]) == result.command
    assert result.dmg_path.is_file()
    assert result.dmg_path.stat().st_size > 0
    assert result.returncode == 0
    assert result.validation_summary["local_ready"] is True
    assert result.validation_summary["distribution_ready"] is False
    assert run_kwargs[0]["check"] is False
    assert "shell" not in run_kwargs[0]


def test_build_launcher_dmg_force_adds_overwrite_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """force=True일 때만 hdiutil overwrite flag를 전달한다."""
    import scripts.build_launcher_dmg as builder

    app_path = _build_app(tmp_path)
    output_path = tmp_path / "Recap.dmg"
    output_path.write_text("old dmg\n", encoding="utf-8")

    def fake_run(command, **kwargs) -> subprocess.CompletedProcess[str]:
        if _is_bash_syntax_check(list(command)):
            return _bash_syntax_success(list(command))
        Path(command[-1]).write_bytes(b"new dmg")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(builder.shutil, "which", lambda _: "/usr/bin/hdiutil")
    monkeypatch.setattr(builder.subprocess, "run", fake_run)

    result = builder.build_launcher_dmg(
        app_path=app_path,
        output_path=output_path,
        force=True,
    )

    assert "-ov" in result.command
    assert result.command[-1] == str(output_path.resolve())
    assert output_path.read_bytes() == b"new dmg"


def test_build_launcher_dmg_rejects_existing_output_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """기존 DMG는 force 없이는 덮어쓰지 않는다."""
    import scripts.build_launcher_dmg as builder

    app_path = _build_app(tmp_path)
    output_path = tmp_path / "Recap.dmg"
    output_path.write_text("old dmg\n", encoding="utf-8")

    def fail_run(command, **kwargs):
        if _is_bash_syntax_check(list(command)):
            return _bash_syntax_success(list(command))
        raise AssertionError("hdiutil should not be called")

    monkeypatch.setattr(builder.shutil, "which", lambda _: "/usr/bin/hdiutil")
    monkeypatch.setattr(builder.subprocess, "run", fail_run)

    with pytest.raises(FileExistsError):
        builder.build_launcher_dmg(app_path=app_path, output_path=output_path)


def test_build_launcher_dmg_force_rejects_directory_and_symlink_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """force=True이어도 디렉토리와 symlink는 덮어쓰기 대상이 아니다."""
    import scripts.build_launcher_dmg as builder

    app_path = _build_app(tmp_path)
    output_dir = tmp_path / "Recap.dmg"
    output_dir.mkdir()
    symlink_output = tmp_path / "Linked.dmg"
    symlink_output.symlink_to(tmp_path / "target.dmg")

    def fail_run(command, **kwargs):
        if _is_bash_syntax_check(list(command)):
            return _bash_syntax_success(list(command))
        raise AssertionError("hdiutil should not be called")

    monkeypatch.setattr(builder.shutil, "which", lambda _: "/usr/bin/hdiutil")
    monkeypatch.setattr(builder.subprocess, "run", fail_run)

    with pytest.raises(FileExistsError, match="directory"):
        builder.build_launcher_dmg(app_path=app_path, output_path=output_dir, force=True)

    with pytest.raises(FileExistsError, match="symlink"):
        builder.build_launcher_dmg(app_path=app_path, output_path=symlink_output, force=True)


def test_build_launcher_dmg_rejects_output_inside_app_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DMG 산출물을 source `.app` 내부에 만들지 않는다."""
    import scripts.build_launcher_dmg as builder

    app_path = _build_app(tmp_path)

    def fail_run(command, **kwargs):
        if _is_bash_syntax_check(list(command)):
            return _bash_syntax_success(list(command))
        raise AssertionError("hdiutil should not be called")

    monkeypatch.setattr(builder.shutil, "which", lambda _: "/usr/bin/hdiutil")
    monkeypatch.setattr(builder.subprocess, "run", fail_run)

    with pytest.raises(ValueError, match="inside the .app"):
        builder.build_launcher_dmg(
            app_path=app_path,
            output_path=app_path / "Contents" / "Resources" / "Recap.dmg",
            force=True,
        )


def test_build_launcher_dmg_rejects_output_inside_app_via_symlink_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """symlink된 `.app` 경유 출력도 source bundle 내부로 판정한다."""
    import scripts.build_launcher_dmg as builder

    app_path = _build_app(tmp_path)
    linked_app = tmp_path / "Link.app"
    linked_app.symlink_to(app_path, target_is_directory=True)

    def fail_run(command, **kwargs):
        if _is_bash_syntax_check(list(command)):
            return _bash_syntax_success(list(command))
        raise AssertionError("hdiutil should not be called")

    monkeypatch.setattr(builder.shutil, "which", lambda _: "/usr/bin/hdiutil")
    monkeypatch.setattr(builder.subprocess, "run", fail_run)

    with pytest.raises(ValueError, match="inside the .app"):
        builder.build_launcher_dmg(
            app_path=linked_app,
            output_path=linked_app / "Contents" / "Resources" / "Recap.dmg",
            force=True,
        )


def test_build_launcher_dmg_rejects_invalid_app_path(tmp_path: Path) -> None:
    """`.app` 디렉토리가 아닌 대상은 패키징하지 않는다."""
    from scripts.build_launcher_dmg import build_launcher_dmg

    with pytest.raises(ValueError, match="existing .app"):
        build_launcher_dmg(app_path=tmp_path)


def test_build_launcher_dmg_rejects_app_that_fails_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """app bundle 구조 검증 실패 시 hdiutil을 호출하지 않는다."""
    import scripts.build_launcher_dmg as builder

    app_path = _build_app(tmp_path)
    (app_path / "Contents" / "Info.plist").unlink()

    def fail_run(command, **kwargs):
        if _is_bash_syntax_check(list(command)):
            return _bash_syntax_success(list(command))
        raise AssertionError("hdiutil should not be called")

    monkeypatch.setattr(builder.shutil, "which", lambda _: "/usr/bin/hdiutil")
    monkeypatch.setattr(builder.subprocess, "run", fail_run)

    with pytest.raises(ValueError, match="app bundle validation failed"):
        builder.build_launcher_dmg(app_path=app_path)


def test_build_launcher_dmg_reports_missing_hdiutil(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hdiutil이 없으면 명확히 실패한다."""
    import scripts.build_launcher_dmg as builder

    app_path = _build_app(tmp_path)
    monkeypatch.setattr(builder.shutil, "which", lambda _: None)

    with pytest.raises(FileNotFoundError, match="hdiutil"):
        builder.build_launcher_dmg(app_path=app_path)


def test_build_launcher_dmg_reports_hdiutil_failure_without_secret_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hdiutil 실패 메시지는 stdout/stderr의 비밀값을 포함하지 않는다."""
    import scripts.build_launcher_dmg as builder

    app_path = _build_app(tmp_path)

    def fake_run(command, **kwargs) -> subprocess.CompletedProcess[str]:
        if _is_bash_syntax_check(list(command)):
            return _bash_syntax_success(list(command))
        return subprocess.CompletedProcess(
            command,
            1,
            "HUGGINGFACE_TOKEN=hf_secretvalue",
            "HF_TOKEN=hf_secretvalue",
        )

    monkeypatch.setattr(builder.shutil, "which", lambda _: "/usr/bin/hdiutil")
    monkeypatch.setattr(builder.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc_info:
        builder.build_launcher_dmg(app_path=app_path, output_dir=tmp_path / "release")

    message = str(exc_info.value)
    assert "exit code 1" in message
    assert "HUGGINGFACE_TOKEN" not in message
    assert "HF_TOKEN" not in message
    assert "hf_secretvalue" not in message


def test_build_launcher_dmg_rejects_missing_or_empty_hdiutil_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hdiutil 0 반환 후에도 실제 DMG 파일을 검증한다."""
    import scripts.build_launcher_dmg as builder

    app_path = _build_app(tmp_path)

    def fake_run_missing(command, **kwargs) -> subprocess.CompletedProcess[str]:
        if _is_bash_syntax_check(list(command)):
            return _bash_syntax_success(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(builder.shutil, "which", lambda _: "/usr/bin/hdiutil")
    monkeypatch.setattr(builder.subprocess, "run", fake_run_missing)

    with pytest.raises(RuntimeError, match="not created"):
        builder.build_launcher_dmg(
            app_path=app_path,
            output_path=tmp_path / "missing-output.dmg",
        )

    def fake_run_empty(command, **kwargs) -> subprocess.CompletedProcess[str]:
        if _is_bash_syntax_check(list(command)):
            return _bash_syntax_success(list(command))
        Path(command[-1]).write_bytes(b"")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(builder.subprocess, "run", fake_run_empty)

    with pytest.raises(RuntimeError, match="empty"):
        builder.build_launcher_dmg(
            app_path=app_path,
            output_path=tmp_path / "empty-output.dmg",
        )


def test_build_launcher_dmg_rejects_bad_output_contract(tmp_path: Path) -> None:
    """output_path/output_dir 동시 지정과 비-dmg 확장자는 거부한다."""
    from scripts.build_launcher_dmg import build_launcher_dmg

    app_path = _build_app(tmp_path)

    with pytest.raises(ValueError, match="mutually exclusive"):
        build_launcher_dmg(
            app_path=app_path,
            output_path=tmp_path / "Recap.dmg",
            output_dir=tmp_path / "release",
            hdiutil_path="/usr/bin/hdiutil",
        )

    with pytest.raises(ValueError, match="must end with .dmg"):
        build_launcher_dmg(
            app_path=app_path,
            output_path=tmp_path / "Recap.zip",
            hdiutil_path="/usr/bin/hdiutil",
        )


def test_build_launcher_dmg_cli_outputs_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    """CLI는 생성 결과를 JSON으로 출력할 수 있다."""
    import scripts.build_launcher_dmg as builder

    app_path = _build_app(tmp_path)

    def fake_run(command, **kwargs) -> subprocess.CompletedProcess[str]:
        if _is_bash_syntax_check(list(command)):
            return _bash_syntax_success(list(command))
        Path(command[-1]).write_bytes(b"fake dmg")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(builder.shutil, "which", lambda _: "/usr/bin/hdiutil")
    monkeypatch.setattr(builder.subprocess, "run", fake_run)

    exit_code = builder.main(
        [
            "--app-path",
            str(app_path),
            "--output-dir",
            str(tmp_path / "release"),
            "--volume-name",
            "Recap Test",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["dmg_path"].endswith("Recap Test.dmg")
    assert payload["volume_name"] == "Recap Test"
    assert payload["success"] is True
    assert payload["returncode"] == 0
    assert payload["command"][1] == "create"
    assert payload["validation"]["local_ready"] is True


def test_build_launcher_dmg_cli_json_redacts_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    """CLI JSON은 path, volume, command의 토큰 marker/value를 redaction한다."""
    import scripts.build_launcher_dmg as builder

    app_path = _build_app(tmp_path)
    secret_output_dir = tmp_path / "HF_TOKEN_release"

    def fake_run(command, **kwargs) -> subprocess.CompletedProcess[str]:
        if _is_bash_syntax_check(list(command)):
            return _bash_syntax_success(list(command))
        Path(command[-1]).write_bytes(b"fake dmg")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(builder.shutil, "which", lambda _: "/usr/bin/hdiutil")
    monkeypatch.setattr(builder.subprocess, "run", fake_run)

    exit_code = builder.main(
        [
            "--app-path",
            str(app_path),
            "--output-dir",
            str(secret_output_dir),
            "--volume-name",
            "hf_secretvalue",
            "--json",
        ]
    )
    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["dmg_path"] == "<redacted>"
    assert payload["volume_name"] == "<redacted>"
    assert "HUGGINGFACE_TOKEN" not in payload_text
    assert "HF_TOKEN" not in payload_text
    assert "hf_secretvalue" not in payload_text


def test_build_launcher_dmg_cli_json_redacts_secret_path_on_failure(
    tmp_path: Path,
    capsys,
) -> None:
    """실패 JSON도 secret marker가 포함된 path를 노출하지 않는다."""
    import scripts.build_launcher_dmg as builder

    secret_path = tmp_path / "HF_TOKEN_workspace" / "Missing.app"

    exit_code = builder.main(["--app-path", str(secret_path), "--json"])
    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["error"]["message"] == "<redacted>"
    assert "HUGGINGFACE_TOKEN" not in payload_text
    assert "HF_TOKEN" not in payload_text


def test_build_launcher_dmg_module_has_no_launch_signing_or_network_apis() -> None:
    """DMG builder는 실행/서명/공증/네트워크 API를 사용하지 않는다."""
    import scripts.build_launcher_dmg as builder

    source = inspect.getsource(builder)

    assert "webbrowser" not in source
    assert "urllib" not in source
    assert "requests" not in source
    assert "open " not in source
    assert "attach" not in source
    assert '"codesign"' not in source
    assert "'codesign'" not in source
    assert "notarytool" not in source
    assert "stapler" not in source
    assert "pip install" not in source
    assert "brew install" not in source
    assert "shell=True" not in source
