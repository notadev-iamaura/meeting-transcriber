"""경량 Recap `.app` 번들 생성기 테스트."""

from __future__ import annotations

import json
import plistlib
import shlex
import stat
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


def _make_python_symlink(path: Path, target: Path) -> Path:
    """테스트용 Python symlink 후보를 만든다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    return path


def _make_bundle_project(tmp_path: Path) -> tuple[Path, Path]:
    """번들 소스 테스트용 프로젝트 구조를 만든다."""
    project_dir, python = _make_project(tmp_path)
    for filename in ("config.py", "pyproject.toml", "requirements.txt"):
        (project_dir / filename).write_text(f"# {filename}\n", encoding="utf-8")
    (project_dir / "config.yaml").write_text(
        "# HUGGINGFACE_TOKEN must stay local\n"
        "diarization:\n"
        "  huggingface_token: hf_secretvalue\n"
        "  block_hf_offline_cache_miss: true\n",
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


def test_build_launcher_app_creates_valid_bundle(tmp_path: Path) -> None:
    """Info.plist, 실행 파일, metadata를 포함한 `.app` 구조를 생성한다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_project(tmp_path)
    output_dir = tmp_path / "dist"

    result = build_launcher_app(
        output_dir=output_dir,
        project_dir=project_dir,
        python_executable=python,
        app_name="Recap Test",
        bundle_id="com.recap.test",
        version="1.2.3",
    )

    assert result.app_path == output_dir / "Recap Test.app"
    assert result.info_plist_path == result.app_path / "Contents" / "Info.plist"
    assert result.executable_path == result.app_path / "Contents" / "MacOS" / "Recap Test"
    assert result.metadata_path == (
        result.app_path / "Contents" / "Resources" / "launcher-metadata.json"
    )

    with result.info_plist_path.open("rb") as fh:
        plist = plistlib.load(fh)
    assert plist["CFBundlePackageType"] == "APPL"
    assert plist["CFBundleExecutable"] == "Recap Test"
    assert plist["CFBundleIdentifier"] == "com.recap.test"
    assert plist["CFBundleShortVersionString"] == "1.2.3"

    mode = result.executable_path.stat().st_mode
    assert mode & stat.S_IXUSR

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["app_name"] == "Recap Test"
    assert metadata["launcher"]["project_dir"] == str(project_dir.resolve())
    assert metadata["launcher"]["setup_url"] == "http://127.0.0.1:8765/app/setup"
    assert metadata["launcher"]["environment_overrides"]["MT_LAUNCHER_PYTHON_SOURCE"] == "explicit"
    assert metadata["launcher"]["environment_overrides"]["MT_LAUNCHER_PYTHON_EXECUTABLE"] == str(
        python.resolve()
    )
    assert metadata["launcher"]["environment_overrides"]["MT_LAUNCHER_PROJECT_DIR"] == str(
        project_dir.resolve()
    )
    assert metadata["launcher"]["runtime"]["python_source"] == "explicit"
    assert metadata["launcher"]["runtime"]["python_executable"] == str(python.resolve())
    assert metadata["launcher"]["runtime"]["candidates"][0]["id"] == "explicit"
    assert metadata["launcher"]["runtime"]["candidates"][0]["selected"] is True


def test_build_launcher_app_preserves_explicit_python_symlink_metadata(
    tmp_path: Path,
) -> None:
    """명시적 venv Python symlink는 metadata와 executable에 그대로 남긴다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, _python = _make_project(tmp_path)
    base_python = tmp_path / "base" / "python"
    base_python.parent.mkdir()
    base_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    base_python.chmod(0o755)
    venv_python = _make_python_symlink(project_dir / ".venv" / "bin" / "python", base_python)

    result = build_launcher_app(
        output_dir=tmp_path / "dist",
        project_dir=project_dir,
        python_executable=venv_python,
    )
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    executable = result.executable_path.read_text(encoding="utf-8")
    launcher = metadata["launcher"]

    assert launcher["python_executable"] == str(venv_python)
    assert launcher["command"][0] == str(venv_python)
    assert launcher["environment_overrides"]["MT_LAUNCHER_PYTHON_EXECUTABLE"] == str(venv_python)
    assert launcher["runtime"]["python_executable"] == str(venv_python)
    assert launcher["runtime"]["candidates"][0]["path"] == str(venv_python)
    assert f"PYTHON_BIN={shlex.quote(str(venv_python))}" in executable
    assert str(base_python.resolve()) not in executable


def test_build_launcher_app_generated_executable_uses_launcher_contract(
    tmp_path: Path,
) -> None:
    """생성된 executable은 런타임에 ui.launcher 계약을 사용한다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_project(tmp_path)

    result = build_launcher_app(
        output_dir=tmp_path / "dist",
        project_dir=project_dir,
        python_executable=python,
    )
    executable = result.executable_path.read_text(encoding="utf-8")

    assert "from ui.launcher import build_launcher_spec, collect_launcher_preflight" in executable
    assert "open_setup_if_healthy()" in executable
    assert executable.index("if open_setup_if_healthy():") < executable.index("subprocess.Popen")
    assert "log_path = Path(spec.log_file)" in executable
    assert "log_path.parent.mkdir(parents=True, exist_ok=True)" in executable
    assert 'with log_path.open("a", encoding="utf-8") as log_handle:' in executable
    assert "stdout=log_handle" in executable
    assert "stderr=subprocess.STDOUT" in executable
    assert executable.index("if open_setup_if_healthy():") < executable.index("log_path.open")
    assert executable.index("log_path.open") < executable.index("subprocess.Popen")
    assert 'PYTHON_LAUNCH=("${PYTHON_BIN}")' in executable
    assert "/usr/bin/arch -arm64 /usr/bin/true" in executable
    assert 'PYTHON_LAUNCH=(/usr/bin/arch -arm64 "${PYTHON_BIN}")' in executable
    assert 'exec "${PYTHON_LAUNCH[@]}" -' in executable
    assert '"--no-menubar"' not in executable
    assert "brew install" not in executable
    assert "pip install" not in executable
    assert "launchctl" not in executable
    assert "setup_audio.sh" not in executable
    assert "HUGGINGFACE_TOKEN" not in executable
    assert "HF_TOKEN" not in executable


def test_build_launcher_app_generated_executable_preserves_runtime_spec_scalars(
    tmp_path: Path,
) -> None:
    """생성 executable은 metadata와 같은 host/port/log 파일을 런타임 spec에 전달한다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_project(tmp_path)

    result = build_launcher_app(
        output_dir=tmp_path / "dist",
        project_dir=project_dir,
        python_executable=python,
        host="localhost",
        port=9876,
    )
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    executable = result.executable_path.read_text(encoding="utf-8")
    launcher = metadata["launcher"]

    assert launcher["host"] == "localhost"
    assert launcher["port"] == 9876
    assert launcher["setup_url"] == "http://localhost:9876/app/setup"
    assert launcher["command"][4] == "localhost"
    assert launcher["command"][6] == "9876"
    assert f"SERVER_HOST={shlex.quote(launcher['host'])}" in executable
    assert f"SERVER_PORT={launcher['port']}" in executable
    assert f"LOG_FILE={shlex.quote(launcher['log_file'])}" in executable
    assert "host=server_host" in executable
    assert "port=int(server_port)" in executable
    assert "log_file=log_file" in executable


def test_build_launcher_app_generated_executable_passes_bash_syntax(
    tmp_path: Path,
) -> None:
    """생성 executable은 실제 bash syntax check를 통과한다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_project(tmp_path)

    result = build_launcher_app(
        output_dir=tmp_path / "dist",
        project_dir=project_dir,
        python_executable=python,
    )
    syntax = subprocess.run(
        ["/bin/bash", "-n", str(result.executable_path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert syntax.returncode == 0, syntax.stderr


def test_build_launcher_app_rejects_existing_bundle_without_force(tmp_path: Path) -> None:
    """기존 app bundle은 --force 없이는 덮어쓰지 않는다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_project(tmp_path)
    output_dir = tmp_path / "dist"

    build_launcher_app(
        output_dir=output_dir,
        project_dir=project_dir,
        python_executable=python,
    )

    with pytest.raises(FileExistsError):
        build_launcher_app(
            output_dir=output_dir,
            project_dir=project_dir,
            python_executable=python,
        )


def test_build_launcher_app_force_replaces_existing_bundle(tmp_path: Path) -> None:
    """force=True일 때 대상 app bundle만 교체한다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_project(tmp_path)
    output_dir = tmp_path / "dist"

    first = build_launcher_app(
        output_dir=output_dir,
        project_dir=project_dir,
        python_executable=python,
    )
    marker = first.app_path / "Contents" / "Resources" / "old.txt"
    marker.write_text("old\n", encoding="utf-8")

    second = build_launcher_app(
        output_dir=output_dir,
        project_dir=project_dir,
        python_executable=python,
        force=True,
    )

    assert second.app_path == first.app_path
    assert not marker.exists()
    assert second.metadata_path.exists()


def test_build_launcher_app_cleans_staged_bundle_on_syntax_validation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """생성 executable syntax 검증 실패 시 partial app bundle을 남기지 않는다."""
    import scripts.build_launcher_app as builder

    project_dir, python = _make_project(tmp_path)
    output_dir = tmp_path / "dist"

    def fail_syntax_validation(path: Path) -> None:
        raise RuntimeError("synthetic syntax failure")

    monkeypatch.setattr(
        builder,
        "_validate_launcher_executable_syntax",
        fail_syntax_validation,
    )

    with pytest.raises(RuntimeError, match="synthetic syntax failure"):
        builder.build_launcher_app(
            output_dir=output_dir,
            project_dir=project_dir,
            python_executable=python,
        )

    assert not output_dir.joinpath("Recap.app").exists()
    assert not any(output_dir.iterdir())


def test_build_launcher_app_force_preserves_existing_bundle_on_staged_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """force=True이어도 staging 검증 실패는 기존 app bundle을 보존한다."""
    import scripts.build_launcher_app as builder

    project_dir, python = _make_project(tmp_path)
    output_dir = tmp_path / "dist"
    original = builder.build_launcher_app(
        output_dir=output_dir,
        project_dir=project_dir,
        python_executable=python,
    )
    marker = original.app_path / "Contents" / "Resources" / "old.txt"
    marker.write_text("old bundle\n", encoding="utf-8")

    def fail_syntax_validation(path: Path) -> None:
        raise RuntimeError("synthetic syntax failure")

    monkeypatch.setattr(
        builder,
        "_validate_launcher_executable_syntax",
        fail_syntax_validation,
    )

    with pytest.raises(RuntimeError, match="synthetic syntax failure"):
        builder.build_launcher_app(
            output_dir=output_dir,
            project_dir=project_dir,
            python_executable=python,
            force=True,
        )

    assert marker.read_text(encoding="utf-8") == "old bundle\n"
    assert original.metadata_path.exists()


def test_build_launcher_app_rejects_output_dir_symlink_escape(tmp_path: Path) -> None:
    """output_dir symlink를 따라 다른 위치에 app bundle을 만들지 않는다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    output_link = tmp_path / "dist-link"
    output_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="output_dir must not be a symlink"):
        build_launcher_app(
            output_dir=output_link,
            project_dir=project_dir,
            python_executable=python,
            force=True,
        )

    assert not outside.joinpath("Recap.app").exists()


def test_build_launcher_app_rejects_output_dir_regular_file(tmp_path: Path) -> None:
    """output_dir 자체가 파일이면 명확히 거부한다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_project(tmp_path)
    output_file = tmp_path / "dist"
    output_file.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(ValueError, match="output_dir must be a directory"):
        build_launcher_app(
            output_dir=output_file,
            project_dir=project_dir,
            python_executable=python,
            force=True,
        )


def test_build_launcher_app_force_rejects_symlink_and_bad_target_types(
    tmp_path: Path,
) -> None:
    """force=True이어도 symlink와 non-directory app target은 덮어쓰지 않는다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_project(tmp_path)
    output_dir = tmp_path / "dist"
    output_dir.mkdir()

    symlink_app = output_dir / "Recap.app"
    symlink_app.symlink_to(tmp_path / "Real.app", target_is_directory=True)
    with pytest.raises(FileExistsError, match="symlink"):
        build_launcher_app(
            output_dir=output_dir,
            project_dir=project_dir,
            python_executable=python,
            force=True,
        )
    symlink_app.unlink()

    bad_app = output_dir / "Recap.app"
    bad_app.write_text("not a bundle\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="app directory"):
        build_launcher_app(
            output_dir=output_dir,
            project_dir=project_dir,
            python_executable=python,
            force=True,
        )
    assert bad_app.read_text(encoding="utf-8") == "not a bundle\n"


def test_build_launcher_app_rejects_path_like_app_name(tmp_path: Path) -> None:
    """app_name으로 output-dir 밖을 빠져나갈 수 없다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_project(tmp_path)

    with pytest.raises(ValueError):
        build_launcher_app(
            output_dir=tmp_path / "dist",
            project_dir=project_dir,
            python_executable=python,
            app_name="../Recap",
        )


def test_build_launcher_app_rejects_invalid_host_without_writing_bundle(
    tmp_path: Path,
) -> None:
    """ui.launcher preflight 실패 시 bundle을 쓰지 않는다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_project(tmp_path)
    output_dir = tmp_path / "dist"

    with pytest.raises(ValueError, match="server_binding"):
        build_launcher_app(
            output_dir=output_dir,
            project_dir=project_dir,
            python_executable=python,
            host="0.0.0.0",
        )

    assert not output_dir.exists()


def test_build_launcher_app_does_not_mutate_project_dir(tmp_path: Path) -> None:
    """빌드 산출물은 output_dir에만 생성한다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_project(tmp_path)
    before = sorted(path.relative_to(project_dir) for path in project_dir.rglob("*"))

    build_launcher_app(
        output_dir=tmp_path / "dist",
        project_dir=project_dir,
        python_executable=python,
    )

    after = sorted(path.relative_to(project_dir) for path in project_dir.rglob("*"))
    assert after == before


def test_build_launcher_app_metadata_does_not_expose_secret_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metadata와 executable은 토큰 값이나 토큰 환경변수 이름을 노출하지 않는다."""
    from scripts.build_launcher_app import build_launcher_app

    monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_secret")
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    project_dir, python = _make_project(tmp_path)

    result = build_launcher_app(
        output_dir=tmp_path / "dist",
        project_dir=project_dir,
        python_executable=python,
    )
    combined = (
        result.metadata_path.read_text(encoding="utf-8")
        + "\n"
        + result.executable_path.read_text(encoding="utf-8")
    )

    assert "hf_secret" not in combined
    assert "HUGGINGFACE_TOKEN" not in combined
    assert "HF_TOKEN" not in combined


def test_build_launcher_app_cli_outputs_json(tmp_path: Path, capsys) -> None:
    """CLI는 생성된 bundle 경로를 JSON으로 출력할 수 있다."""
    from scripts.build_launcher_app import main

    project_dir, python = _make_project(tmp_path)

    exit_code = main(
        [
            "--output-dir",
            str(tmp_path / "dist"),
            "--project-dir",
            str(project_dir),
            "--python",
            str(python),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["app_path"].endswith("Recap.app")
    assert Path(payload["info_plist_path"]).exists()


def test_build_launcher_app_bundles_project_source_snapshot(tmp_path: Path) -> None:
    """명시 옵션으로 런타임 프로젝트 소스 스냅샷을 app bundle에 포함한다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_bundle_project(tmp_path)
    output_dir = project_dir / "dist"

    result = build_launcher_app(
        output_dir=output_dir,
        project_dir=project_dir,
        python_executable=python,
        host="localhost",
        port=9876,
        bundle_source=True,
    )
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    executable = result.executable_path.read_text(encoding="utf-8")

    assert result.bundled_project_path == result.app_path / "Contents" / "Resources" / "project"
    assert result.bundled_file_count > 0
    assert (result.bundled_project_path / "main.py").is_file()
    assert (result.bundled_project_path / "pyproject.toml").is_file()
    assert (result.bundled_project_path / "ui" / "web" / "index.html").is_file()
    bundled_config = (result.bundled_project_path / "config.yaml").read_text(encoding="utf-8")
    assert "huggingface_token: null" in bundled_config
    assert "block_hf_offline_cache_miss" in bundled_config
    assert "HUGGINGFACE_TOKEN" not in bundled_config
    assert "hf_secretvalue" not in bundled_config
    assert metadata["source_bundle"] == {
        "enabled": True,
        "relative_path": "Contents/Resources/project",
        "file_count": result.bundled_file_count,
    }
    assert metadata["launcher"]["project_dir"] == str(result.bundled_project_path.resolve())
    assert metadata["launcher"]["cwd"] == str(result.bundled_project_path.resolve())
    assert metadata["launcher"]["host"] == "localhost"
    assert metadata["launcher"]["port"] == 9876
    assert metadata["launcher"]["setup_url"] == "http://localhost:9876/app/setup"
    assert metadata["launcher"]["command"][1] == str(
        result.bundled_project_path.resolve() / "main.py"
    )
    assert metadata["launcher"]["command"][4] == "localhost"
    assert metadata["launcher"]["command"][6] == "9876"
    assert "BUNDLED_PROJECT_DIR" in executable
    assert "../Resources/project" in executable
    assert "SERVER_HOST=localhost" in executable
    assert "SERVER_PORT=9876" in executable
    assert f"LOG_FILE={shlex.quote(metadata['launcher']['log_file'])}" in executable
    assert str(project_dir.resolve()) not in executable


def test_build_launcher_app_bundle_source_excludes_local_state_and_secret_files(
    tmp_path: Path,
) -> None:
    """번들 소스는 allowlist 기반이며 로컬 상태와 비밀 파일을 복사하지 않는다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_bundle_project(tmp_path)
    (project_dir / ".env").write_text("HUGGINGFACE_TOKEN=hf_secret\n", encoding="utf-8")
    for dirname in (
        ".git",
        ".venv",
        "__pycache__",
        "benchmark_runs",
        "output",
        "state",
        "ui/web-dist",
    ):
        excluded_dir = project_dir / dirname
        excluded_dir.mkdir()
        (excluded_dir / "marker.txt").write_text("do not copy\n", encoding="utf-8")
    scripts_extra = project_dir / "scripts" / "benchmark_stt.py"
    scripts_extra.parent.mkdir()
    scripts_extra.write_text("# benchmark should not be bundled\n", encoding="utf-8")
    model_artifact = project_dir / "core" / "weights.safetensors"
    model_artifact.write_text("not a real model\n", encoding="utf-8")

    result = build_launcher_app(
        output_dir=project_dir / "dist",
        project_dir=project_dir,
        python_executable=python,
        bundle_source=True,
    )
    bundled = result.bundled_project_path
    assert bundled is not None
    bundled_files = {str(path.relative_to(bundled)) for path in bundled.rglob("*")}
    combined = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in bundled.rglob("*")
        if path.is_file()
    )

    assert ".env" not in bundled_files
    assert ".git/marker.txt" not in bundled_files
    assert ".venv/marker.txt" not in bundled_files
    assert "__pycache__/marker.txt" not in bundled_files
    assert "benchmark_runs/marker.txt" not in bundled_files
    assert "output/marker.txt" not in bundled_files
    assert "state/marker.txt" not in bundled_files
    assert "ui/web-dist/marker.txt" not in bundled_files
    assert "dist/Recap.app" not in "\n".join(bundled_files)
    assert "scripts/install.sh" not in bundled_files
    assert "scripts/benchmark_stt.py" not in bundled_files
    assert "core/weights.safetensors" not in bundled_files
    assert "hf_secret" not in combined


def test_build_launcher_app_bundle_source_skips_symlinks(tmp_path: Path) -> None:
    """소스 스냅샷은 symlink를 복사하지 않아 bundle 밖 escape를 막는다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_bundle_project(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (project_dir / "core" / "outside-link").symlink_to(outside)

    result = build_launcher_app(
        output_dir=tmp_path / "dist",
        project_dir=project_dir,
        python_executable=python,
        bundle_source=True,
    )

    assert result.bundled_project_path is not None
    assert not (result.bundled_project_path / "core" / "outside-link").exists()


def test_build_launcher_app_bundle_source_does_not_mutate_project_dir(tmp_path: Path) -> None:
    """소스 번들링도 산출물 외 프로젝트 파일을 수정하지 않는다."""
    from scripts.build_launcher_app import build_launcher_app

    project_dir, python = _make_bundle_project(tmp_path)
    before = sorted(path.relative_to(project_dir) for path in project_dir.rglob("*"))

    build_launcher_app(
        output_dir=tmp_path / "dist",
        project_dir=project_dir,
        python_executable=python,
        bundle_source=True,
    )

    after = sorted(path.relative_to(project_dir) for path in project_dir.rglob("*"))
    assert after == before


def test_build_launcher_app_cli_can_bundle_source(tmp_path: Path, capsys) -> None:
    """CLI의 --bundle-source 옵션은 bundle 경로와 파일 수를 JSON에 포함한다."""
    from scripts.build_launcher_app import main

    project_dir, python = _make_bundle_project(tmp_path)

    exit_code = main(
        [
            "--output-dir",
            str(tmp_path / "dist"),
            "--project-dir",
            str(project_dir),
            "--python",
            str(python),
            "--bundle-source",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["bundled_project_path"].endswith("Recap.app/Contents/Resources/project")
    assert payload["bundled_file_count"] > 0
