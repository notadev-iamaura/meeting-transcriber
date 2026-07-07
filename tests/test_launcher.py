"""경량 런처 계약 모듈 테스트."""

from __future__ import annotations

import inspect
import json
from pathlib import Path


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    """테스트용 프로젝트 루트와 Python 실행 파일을 만든다."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "main.py").write_text("# test entrypoint\n", encoding="utf-8")

    python = tmp_path / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    return project_dir, python


def _make_python(path: Path) -> Path:
    """테스트용 실행 가능 Python 후보를 만든다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_non_executable_python(path: Path) -> Path:
    """테스트용 실행 권한 없는 Python 후보를 만든다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o644)
    return path


def _make_python_symlink(path: Path, target: Path) -> Path:
    """테스트용 Python symlink 후보를 만든다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    return path


def test_build_launcher_spec_generates_headless_server_command(tmp_path: Path) -> None:
    """런처는 메뉴바 없이 서버를 띄우는 명령을 생성한다."""
    from ui.launcher import build_launcher_spec

    project_dir, python = _make_project(tmp_path)
    log_file = tmp_path / "logs" / "launcher.log"

    spec = build_launcher_spec(
        project_dir=project_dir,
        python_executable=python,
        host="127.0.0.1",
        port=9876,
        log_file=log_file,
    )

    assert spec.app_url == "http://127.0.0.1:9876/app"
    assert spec.setup_url == "http://127.0.0.1:9876/app/setup"
    assert spec.build_server_argv() == [
        str(python),
        str(project_dir / "main.py"),
        "--no-menubar",
        "--host",
        "127.0.0.1",
        "--port",
        "9876",
        "--log-file",
        str(log_file),
    ]
    assert spec.build_environment_overrides() == {
        "PATH": f"{python.parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "ko_KR.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "MT_LAUNCHER_PYTHON_SOURCE": "explicit",
        "MT_LAUNCHER_PYTHON_EXECUTABLE": str(python),
        "MT_LAUNCHER_PROJECT_DIR": str(project_dir),
    }
    runtime = spec.to_dict()["runtime"]
    assert runtime["python_source"] == "explicit"
    assert runtime["python_executable"] == str(python)
    assert runtime["candidates"][0] == {
        "id": "explicit",
        "path": str(python),
        "exists": True,
        "is_file": True,
        "is_executable": True,
        "selected": True,
    }


def test_build_launcher_spec_preserves_explicit_python_symlink(tmp_path: Path) -> None:
    """명시적 Python 경로는 venv symlink를 보존해 실행한다."""
    from ui.launcher import LAUNCHER_PYTHON_EXECUTABLE_ENV, build_launcher_spec

    project_dir, _python = _make_project(tmp_path)
    base_python = _make_python(tmp_path / "base" / "python")
    venv_python = _make_python_symlink(project_dir / ".venv" / "bin" / "python", base_python)

    spec = build_launcher_spec(project_dir=project_dir, python_executable=venv_python)
    runtime = spec.to_dict()["runtime"]

    assert spec.python_executable == venv_python
    assert spec.build_server_argv()[0] == str(venv_python)
    assert spec.build_environment_overrides()[LAUNCHER_PYTHON_EXECUTABLE_ENV] == str(venv_python)
    assert runtime["python_source"] == "explicit"
    assert runtime["python_executable"] == str(venv_python)
    assert runtime["candidates"][0]["path"] == str(venv_python)
    assert runtime["candidates"][0]["is_executable"] is True


def test_build_launcher_spec_records_project_venv_runtime_priority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """기본 Python 후보는 project venv를 managed/current보다 우선한다."""
    import ui.launcher as launcher

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "main.py").write_text("# test entrypoint\n", encoding="utf-8")
    project_python = _make_python(project_dir / ".venv" / "bin" / "python")
    fake_home = tmp_path / "home"
    managed_python = _make_python(fake_home / ".meeting-transcriber-venv" / "bin" / "python")
    current_python = _make_python(tmp_path / "current" / "python")
    monkeypatch.setattr(launcher.Path, "home", lambda: fake_home)
    monkeypatch.setattr(launcher.sys, "executable", str(current_python))

    spec = launcher.build_launcher_spec(project_dir=project_dir)
    runtime = spec.to_dict()["runtime"]
    candidates = {candidate["id"]: candidate for candidate in runtime["candidates"]}

    assert spec.python_executable == project_python.resolve()
    assert runtime["python_source"] == "project_venv"
    assert candidates["project_venv"]["selected"] is True
    assert candidates["managed_venv"]["path"] == str(managed_python.resolve())
    assert candidates["managed_venv"]["selected"] is False
    assert candidates["managed_venv"]["is_executable"] is True
    assert candidates["current_interpreter"]["path"] == str(current_python.resolve())
    assert candidates["current_interpreter"]["selected"] is False


def test_build_launcher_spec_records_managed_venv_runtime_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """project venv가 없으면 관리형 사용자 venv를 선택한다."""
    import ui.launcher as launcher

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "main.py").write_text("# test entrypoint\n", encoding="utf-8")
    fake_home = tmp_path / "home"
    managed_python = _make_python(fake_home / ".meeting-transcriber-venv" / "bin" / "python")
    current_python = _make_python(tmp_path / "current" / "python")
    monkeypatch.setattr(launcher.Path, "home", lambda: fake_home)
    monkeypatch.setattr(launcher.sys, "executable", str(current_python))

    spec = launcher.build_launcher_spec(project_dir=project_dir)
    runtime = spec.to_dict()["runtime"]
    candidates = {candidate["id"]: candidate for candidate in runtime["candidates"]}

    assert spec.python_executable == managed_python.resolve()
    assert runtime["python_source"] == "managed_venv"
    assert candidates["project_venv"]["exists"] is False
    assert candidates["project_venv"]["is_file"] is False
    assert candidates["project_venv"]["is_executable"] is False
    assert candidates["managed_venv"]["selected"] is True


def test_build_launcher_spec_records_current_interpreter_runtime_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """venv 후보가 없으면 현재 인터프리터 fallback을 기록한다."""
    import ui.launcher as launcher

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "main.py").write_text("# test entrypoint\n", encoding="utf-8")
    fake_home = tmp_path / "home"
    current_python = _make_python(tmp_path / "current" / "python")
    monkeypatch.setattr(launcher.Path, "home", lambda: fake_home)
    monkeypatch.setattr(launcher.sys, "executable", str(current_python))

    spec = launcher.build_launcher_spec(project_dir=project_dir)
    runtime = spec.to_dict()["runtime"]
    candidates = {candidate["id"]: candidate for candidate in runtime["candidates"]}

    assert spec.python_executable == current_python.resolve()
    assert runtime["python_source"] == "current_interpreter"
    assert candidates["current_interpreter"]["selected"] is True
    assert candidates["current_interpreter"]["is_file"] is True
    assert candidates["current_interpreter"]["is_executable"] is True


def test_build_launcher_spec_does_not_skip_non_executable_project_venv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """project venv가 파일이면 실행 권한이 없어도 선택 후보로 보고한다."""
    import ui.launcher as launcher

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "main.py").write_text("# test entrypoint\n", encoding="utf-8")
    project_python = _make_non_executable_python(project_dir / ".venv" / "bin" / "python")
    fake_home = tmp_path / "home"
    managed_python = _make_python(fake_home / ".meeting-transcriber-venv" / "bin" / "python")
    monkeypatch.setattr(launcher.Path, "home", lambda: fake_home)

    spec = launcher.build_launcher_spec(project_dir=project_dir)
    runtime = spec.to_dict()["runtime"]
    candidates = {candidate["id"]: candidate for candidate in runtime["candidates"]}

    assert spec.python_executable == project_python.resolve()
    assert runtime["python_source"] == "project_venv"
    assert candidates["project_venv"]["selected"] is True
    assert candidates["project_venv"]["is_file"] is True
    assert candidates["project_venv"]["is_executable"] is False
    assert candidates["managed_venv"]["path"] == str(managed_python.resolve())
    assert candidates["managed_venv"]["selected"] is False


def test_collect_launcher_preflight_ready_without_creating_directories(tmp_path: Path) -> None:
    """preflight는 read-only 판정만 수행하고 로그 디렉토리를 만들지 않는다."""
    from ui.launcher import build_launcher_spec, collect_launcher_preflight

    project_dir, python = _make_project(tmp_path)
    log_file = tmp_path / "missing" / "launcher.log"

    report = collect_launcher_preflight(
        build_launcher_spec(
            project_dir=project_dir,
            python_executable=python,
            log_file=log_file,
        )
    )

    assert report.ready is True
    assert report.status == "pass"
    assert {check.id: check.status for check in report.checks} == {
        "project_dir": "pass",
        "main_py": "pass",
        "python_executable": "pass",
        "server_binding": "pass",
    }
    python_check = next(check for check in report.checks if check.id == "python_executable")
    assert python_check.details["is_executable"] is True
    assert not log_file.parent.exists()


def test_collect_launcher_preflight_reports_missing_entrypoint(tmp_path: Path) -> None:
    """main.py가 없으면 런처 준비 상태가 fail이다."""
    from ui.launcher import build_launcher_spec, collect_launcher_preflight

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    report = collect_launcher_preflight(
        build_launcher_spec(project_dir=project_dir, python_executable=python)
    )

    assert report.ready is False
    assert report.status == "fail"
    assert {check.id: check.status for check in report.checks}["main_py"] == "fail"


def test_collect_launcher_preflight_rejects_non_executable_python(tmp_path: Path) -> None:
    """Python 후보가 파일이어도 실행 권한이 없으면 fail이다."""
    from ui.launcher import build_launcher_spec, collect_launcher_preflight

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "main.py").write_text("# test entrypoint\n", encoding="utf-8")
    python = _make_non_executable_python(tmp_path / "python")

    report = collect_launcher_preflight(
        build_launcher_spec(project_dir=project_dir, python_executable=python)
    )
    python_check = next(check for check in report.checks if check.id == "python_executable")

    assert report.ready is False
    assert report.status == "fail"
    assert python_check.status == "fail"
    assert python_check.details["is_file"] is True
    assert python_check.details["is_executable"] is False


def test_collect_launcher_preflight_rejects_invalid_port(tmp_path: Path) -> None:
    """port는 TCP 포트 범위 안에 있어야 한다."""
    from ui.launcher import build_launcher_spec, collect_launcher_preflight

    project_dir, python = _make_project(tmp_path)

    report = collect_launcher_preflight(
        build_launcher_spec(project_dir=project_dir, python_executable=python, port=70000)
    )

    assert report.ready is False
    assert {check.id: check.status for check in report.checks}["server_binding"] == "fail"


def test_collect_launcher_preflight_rejects_non_loopback_host(tmp_path: Path) -> None:
    """경량 런처는 로컬 UI만 열도록 loopback host만 허용한다."""
    from ui.launcher import build_launcher_spec, collect_launcher_preflight

    project_dir, python = _make_project(tmp_path)

    report = collect_launcher_preflight(
        build_launcher_spec(project_dir=project_dir, python_executable=python, host="0.0.0.0")
    )

    assert report.ready is False
    assert {check.id: check.status for check in report.checks}["server_binding"] == "fail"


def test_launcher_ipv6_loopback_url_is_bracketed(tmp_path: Path) -> None:
    """IPv6 loopback URL은 RFC 형식대로 대괄호로 감싼다."""
    from ui.launcher import build_launcher_spec, collect_launcher_preflight

    project_dir, python = _make_project(tmp_path)

    spec = build_launcher_spec(
        project_dir=project_dir,
        python_executable=python,
        host="::1",
        port=9876,
    )
    report = collect_launcher_preflight(spec)

    assert report.ready is True
    assert spec.app_url == "http://[::1]:9876/app"
    assert spec.setup_url == "http://[::1]:9876/app/setup"
    assert spec.build_server_argv()[4] == "::1"


def test_launcher_host_is_normalized_consistently(tmp_path: Path) -> None:
    """공백/대소문자가 섞인 host 입력은 검증과 출력 모두에서 정규화한다."""
    from ui.launcher import build_launcher_spec, collect_launcher_preflight

    project_dir, python = _make_project(tmp_path)

    spec = build_launcher_spec(
        project_dir=project_dir,
        python_executable=python,
        host=" localhost ",
        port=9876,
    )
    report = collect_launcher_preflight(spec)

    assert report.ready is True
    assert spec.host == "localhost"
    assert spec.setup_url == "http://localhost:9876/app/setup"
    assert spec.build_server_argv()[4] == "localhost"


def test_launcher_report_does_not_expose_secret_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """런처 JSON 계약은 토큰 값이나 전체 환경을 노출하지 않는다."""
    from ui.launcher import build_launcher_spec, collect_launcher_preflight

    monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_secret")
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    project_dir, python = _make_project(tmp_path)

    payload = collect_launcher_preflight(
        build_launcher_spec(project_dir=project_dir, python_executable=python)
    ).to_dict()
    serialized = json.dumps(payload, ensure_ascii=False)

    assert "hf_secret" not in serialized
    assert "HUGGINGFACE_TOKEN" not in serialized
    assert "HF_TOKEN" not in serialized
    assert "environment_overrides" in payload["launcher"]


def test_launcher_cli_outputs_json_and_exit_code(tmp_path: Path, capsys) -> None:
    """CLI는 Swift/셸 런처가 소비할 수 있는 JSON 계약을 출력한다."""
    from ui.launcher import main

    project_dir, python = _make_project(tmp_path)

    exit_code = main(
        [
            "--project-dir",
            str(project_dir),
            "--python",
            str(python),
            "--port",
            "9876",
        ]
    )
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert exit_code == 0
    assert payload["status"] == "pass"
    assert payload["ready"] is True
    assert payload["launcher"]["setup_url"] == "http://127.0.0.1:9876/app/setup"
    assert payload["launcher"]["command"][2] == "--no-menubar"
    assert payload["launcher"]["environment_overrides"]["PYTHONUNBUFFERED"] == "1"
    assert payload["launcher"]["environment_overrides"]["MT_LAUNCHER_PYTHON_SOURCE"] == "explicit"


def test_launcher_cli_print_command(tmp_path: Path, capsys) -> None:
    """--print-command는 사람이 확인 가능한 실행 명령을 출력한다."""
    from ui.launcher import main

    project_dir, python = _make_project(tmp_path)

    exit_code = main(
        [
            "--project-dir",
            str(project_dir),
            "--python",
            str(python),
            "--print-command",
        ]
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert str(project_dir / "main.py") in out
    assert "--no-menubar" in out


def test_launcher_module_does_not_start_processes_or_mutate_filesystem() -> None:
    """런처 계약 모듈은 실행/설치 부작용 API를 직접 사용하지 않는다."""
    import ui.launcher

    source = inspect.getsource(ui.launcher)

    assert "subprocess" not in source
    assert "Popen" not in source
    assert ".mkdir(" not in source
    assert ".chmod(" not in source
    assert ".touch(" not in source
    assert ".unlink(" not in source
