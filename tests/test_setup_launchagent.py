"""
LaunchAgent 설정 스크립트 테스트 모듈

목적: scripts/setup_launchagent.sh의 동작을 검증한다.
주요 테스트:
  - plist XML 생성 및 문법 검증
  - 경로 치환 정확성
  - --unload 옵션 동작
  - --status 옵션 동작
  - --help 옵션 동작
  - 멱등성 (여러 번 실행 시 안전)
  - 사전 조건 검증 (venv 없을 때 에러)
의존성: subprocess, pathlib, plistlib
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

# 스크립트 경로
_SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "setup_launchagent.sh"
)


def _create_mock_env(tmp_path: Path, *, create_venv: bool = True) -> dict[str, str]:
    """격리된 테스트 환경을 구성하는 헬퍼 함수.

    실제 HOME과 LaunchAgents 디렉토리를 건드리지 않도록
    임시 디렉토리를 사용한다.

    Args:
        tmp_path: pytest가 제공하는 임시 디렉토리
        create_venv: venv 디렉토리를 생성할지 여부

    Returns:
        테스트 환경 정보 딕셔너리
    """
    # 가짜 HOME 디렉토리 구조 생성
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    # 가짜 venv (선택적)
    venv_dir = fake_home / ".meeting-transcriber-venv"
    python_bin = venv_dir / "bin" / "python"
    if create_venv:
        python_bin.parent.mkdir(parents=True)
        python_bin.touch()
        python_bin.chmod(0o755)

    # 가짜 LaunchAgents 디렉토리
    launch_agents = fake_home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)

    # 가짜 프로젝트 디렉토리 (스크립트를 scripts/ 하위에 복사)
    project_dir = tmp_path / "project"
    scripts_dir = project_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (project_dir / "main.py").touch()

    test_script = scripts_dir / "setup_launchagent.sh"
    test_script.write_text(
        _SCRIPT_PATH.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    test_script.chmod(0o755)

    return {
        "home": str(fake_home),
        "venv_dir": str(venv_dir),
        "python_bin": str(python_bin),
        "project_dir": str(project_dir),
        "script": str(test_script),
        "plist_path": str(launch_agents / "com.meeting-transcriber.plist"),
    }


def _run_with_mock_launchctl(
    mock_env: dict[str, str],
    args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """가짜 launchctl/plutil을 PATH에 넣고 스크립트를 실행한다.

    source가 아닌 직접 실행으로, $0과 BASH_SOURCE[0]이
    올바르게 스크립트를 가리킨다.

    Args:
        mock_env: _create_mock_env()의 반환값
        args: 스크립트에 전달할 인자 (예: ["--unload"])

    Returns:
        subprocess.CompletedProcess 인스턴스
    """
    # 가짜 launchctl, plutil 바이너리를 담을 임시 bin 디렉토리
    mock_bin = Path(mock_env["project_dir"]) / "_mock_bin"
    mock_bin.mkdir(exist_ok=True)

    # 가짜 launchctl (항상 성공, list는 실패=미등록)
    mock_launchctl = mock_bin / "launchctl"
    mock_launchctl.write_text(
        '#!/bin/bash\n'
        '# 가짜 launchctl — 테스트용\n'
        'if [[ "$1" == "list" ]]; then exit 1; fi\n'
        'exit 0\n',
        encoding="utf-8",
    )
    mock_launchctl.chmod(0o755)

    # 가짜 plutil (항상 lint 통과)
    mock_plutil = mock_bin / "plutil"
    mock_plutil.write_text(
        '#!/bin/bash\nexit 0\n',
        encoding="utf-8",
    )
    mock_plutil.chmod(0o755)

    # PATH 앞에 mock_bin을 추가하여 가짜 바이너리 우선 사용
    # VIRTUAL_ENV를 제거하여 실제 활성화된 venv가 테스트에 영향을 주지 않도록 함
    filtered_env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env = {
        **filtered_env,
        "HOME": mock_env["home"],
        "PATH": f"{mock_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
    }

    cmd = ["bash", mock_env["script"]]
    if args:
        cmd.extend(args)

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )


def _generate_plist(mock_env: dict[str, str]) -> Path:
    """plist를 생성하고 경로를 반환하는 헬퍼.

    Args:
        mock_env: _create_mock_env()의 반환값

    Returns:
        생성된 plist 파일 경로

    Raises:
        AssertionError: plist 생성 실패 시
    """
    result = _run_with_mock_launchctl(mock_env)
    assert result.returncode == 0, (
        f"plist 생성 실패 (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    plist_path = Path(mock_env["plist_path"])
    assert plist_path.exists(), f"plist 파일 미생성: {plist_path}"
    return plist_path


class TestScriptExists:
    """스크립트 파일 존재 및 실행 권한 검증."""

    def test_스크립트_파일_존재(self) -> None:
        """setup_launchagent.sh 파일이 존재하는지 확인한다."""
        assert _SCRIPT_PATH.exists(), f"스크립트 파일 없음: {_SCRIPT_PATH}"

    def test_스크립트_실행_권한(self) -> None:
        """setup_launchagent.sh에 실행 권한이 있는지 확인한다."""
        assert os.access(_SCRIPT_PATH, os.X_OK), "스크립트에 실행 권한 없음"

    def test_스크립트_셸_해석기(self) -> None:
        """셸 스크립트 첫 줄이 #!/bin/bash인지 확인한다."""
        first_line = _SCRIPT_PATH.read_text(encoding="utf-8").split("\n")[0]
        assert first_line == "#!/bin/bash", f"예상: #!/bin/bash, 실제: {first_line}"


class TestHelpOption:
    """--help 옵션 동작 검증."""

    def test_help_출력(self) -> None:
        """--help 옵션이 도움말을 출력하는지 확인한다."""
        result = subprocess.run(
            ["bash", str(_SCRIPT_PATH), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "--unload" in result.stdout
        assert "--status" in result.stdout

    def test_h_단축_옵션(self) -> None:
        """-h 옵션이 도움말을 출력하는지 확인한다."""
        result = subprocess.run(
            ["bash", str(_SCRIPT_PATH), "-h"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "사용법" in result.stdout

    def test_help_서브커맨드(self) -> None:
        """help 서브커맨드가 도움말을 출력하는지 확인한다."""
        result = subprocess.run(
            ["bash", str(_SCRIPT_PATH), "help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "--unload" in result.stdout


class TestUnknownOption:
    """알 수 없는 옵션 처리 검증."""

    def test_알_수_없는_옵션_에러(self) -> None:
        """알 수 없는 옵션이 에러를 반환하는지 확인한다."""
        result = subprocess.run(
            ["bash", str(_SCRIPT_PATH), "--invalid-option"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0
        assert "알 수 없는 옵션" in result.stderr


class TestPlistGeneration:
    """plist XML 생성 로직 검증.

    가짜 launchctl/plutil을 PATH에 넣어
    실제 시스템에 영향 없이 plist 생성을 검증한다.
    """

    @pytest.fixture()
    def mock_env(self, tmp_path: Path) -> dict[str, str]:
        """격리된 테스트 환경을 구성한다."""
        return _create_mock_env(tmp_path)

    def test_plist_파일_생성(self, mock_env: dict[str, str]) -> None:
        """plist 파일이 생성되는지 확인한다."""
        plist_path = _generate_plist(mock_env)
        assert plist_path.exists()

    def test_plist_XML_유효(self, mock_env: dict[str, str]) -> None:
        """생성된 plist가 유효한 XML인지 확인한다."""
        plist_path = _generate_plist(mock_env)
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        assert isinstance(data, dict), "plist 루트가 dict이 아님"

    def test_plist_Label_정확(self, mock_env: dict[str, str]) -> None:
        """plist의 Label이 정확한지 확인한다."""
        plist_path = _generate_plist(mock_env)
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        assert data["Label"] == "com.meeting-transcriber"

    def test_plist_RunAtLoad_활성(self, mock_env: dict[str, str]) -> None:
        """RunAtLoad가 true인지 확인한다."""
        plist_path = _generate_plist(mock_env)
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        assert data["RunAtLoad"] is True

    def test_plist_KeepAlive_비활성(self, mock_env: dict[str, str]) -> None:
        """KeepAlive가 false인지 확인한다 (안전)."""
        plist_path = _generate_plist(mock_env)
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        assert data["KeepAlive"] is False

    def test_plist_ProgramArguments_경로(self, mock_env: dict[str, str]) -> None:
        """ProgramArguments에 올바른 Python, main.py 경로가 포함되는지 확인한다."""
        plist_path = _generate_plist(mock_env)
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        args = data["ProgramArguments"]
        assert args[0] == mock_env["python_bin"]
        assert args[1].endswith("main.py")

    def test_plist_WorkingDirectory(self, mock_env: dict[str, str]) -> None:
        """WorkingDirectory가 프로젝트 디렉토리인지 확인한다."""
        plist_path = _generate_plist(mock_env)
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        assert data["WorkingDirectory"] == mock_env["project_dir"]

    def test_plist_ProcessType_Background(self, mock_env: dict[str, str]) -> None:
        """ProcessType이 Background인지 확인한다."""
        plist_path = _generate_plist(mock_env)
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        assert data["ProcessType"] == "Background"

    def test_plist_LowPriorityBackgroundIO(self, mock_env: dict[str, str]) -> None:
        """LowPriorityBackgroundIO가 true인지 확인한다."""
        plist_path = _generate_plist(mock_env)
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        assert data["LowPriorityBackgroundIO"] is True

    def test_plist_로그_경로(self, mock_env: dict[str, str]) -> None:
        """StandardOutPath, StandardErrorPath가 설정되는지 확인한다."""
        plist_path = _generate_plist(mock_env)
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        assert "StandardOutPath" in data
        assert "StandardErrorPath" in data
        assert data["StandardOutPath"].endswith(".log")
        assert data["StandardErrorPath"].endswith(".log")

    def test_plist_환경변수_PATH_포함(self, mock_env: dict[str, str]) -> None:
        """환경변수에 PATH가 설정되는지 확인한다."""
        plist_path = _generate_plist(mock_env)
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        env_vars = data["EnvironmentVariables"]
        assert "PATH" in env_vars
        # venv bin 경로가 PATH에 포함되어야 함
        assert ".meeting-transcriber-venv/bin" in env_vars["PATH"]

    def test_plist_환경변수_LANG_한국어(self, mock_env: dict[str, str]) -> None:
        """LANG이 ko_KR.UTF-8로 설정되는지 확인한다."""
        plist_path = _generate_plist(mock_env)
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        env_vars = data["EnvironmentVariables"]
        assert env_vars.get("LANG") == "ko_KR.UTF-8"

    def test_plist_환경변수_PYTHONUNBUFFERED(self, mock_env: dict[str, str]) -> None:
        """PYTHONUNBUFFERED=1이 설정되는지 확인한다."""
        plist_path = _generate_plist(mock_env)
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        env_vars = data["EnvironmentVariables"]
        assert env_vars.get("PYTHONUNBUFFERED") == "1"

    def test_plist_파일_권한_644(self, mock_env: dict[str, str]) -> None:
        """plist 파일 권한이 644인지 확인한다."""
        plist_path = _generate_plist(mock_env)
        mode = plist_path.stat().st_mode & 0o777
        assert mode == 0o644, f"예상 권한: 644, 실제: {oct(mode)}"

    def test_plist_log_file_인자_포함(self, mock_env: dict[str, str]) -> None:
        """ProgramArguments에 --log-file 인자가 포함되는지 확인한다."""
        plist_path = _generate_plist(mock_env)
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        args = data["ProgramArguments"]
        assert "--log-file" in args


class TestVenvValidation:
    """venv 사전 조건 검증 테스트."""

    def test_venv_없을_때_에러(self, tmp_path: Path) -> None:
        """venv 디렉토리가 없으면 에러를 반환하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_venv=False)

        result = _run_with_mock_launchctl(mock_env)
        assert result.returncode != 0
        assert "가상환경" in result.stderr


class TestUnloadOption:
    """--unload 옵션 동작 검증."""

    def test_plist_없을_때_unload_안전(self, tmp_path: Path) -> None:
        """plist 파일이 없을 때 --unload가 안전하게 종료되는지 확인한다."""
        mock_env = _create_mock_env(tmp_path)

        result = _run_with_mock_launchctl(mock_env, args=["--unload"])
        assert result.returncode == 0
        # _warn은 stderr로 출력
        combined = result.stdout + result.stderr
        assert "존재하지 않습니다" in combined


class TestStatusOption:
    """--status 옵션 동작 검증."""

    def test_plist_없을_때_status(self, tmp_path: Path) -> None:
        """plist 파일이 없을 때 --status가 안전하게 출력되는지 확인한다."""
        mock_env = _create_mock_env(tmp_path)

        result = _run_with_mock_launchctl(mock_env, args=["--status"])
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "상태 확인" in combined


class TestScriptSyntax:
    """셸 스크립트 문법 검증."""

    def test_bash_문법_검증(self) -> None:
        """bash -n으로 문법 오류가 없는지 확인한다."""
        result = subprocess.run(
            ["bash", "-n", str(_SCRIPT_PATH)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"문법 오류: {result.stderr}"

    def test_set_euo_pipefail_포함(self) -> None:
        """set -euo pipefail이 포함되어 있는지 확인한다."""
        content = _SCRIPT_PATH.read_text(encoding="utf-8")
        assert "set -euo pipefail" in content

    def test_BASH_SOURCE_사용(self) -> None:
        """BASH_SOURCE[0]을 사용하여 경로를 감지하는지 확인한다."""
        content = _SCRIPT_PATH.read_text(encoding="utf-8")
        assert "BASH_SOURCE[0]" in content


class TestIdempotency:
    """멱등성 검증 — 여러 번 실행해도 동일한 결과."""

    def test_plist_재생성_멱등(self, tmp_path: Path) -> None:
        """plist를 두 번 생성해도 내용이 동일한지 확인한다."""
        mock_env = _create_mock_env(tmp_path)

        # 첫 번째 실행
        result1 = _run_with_mock_launchctl(mock_env)
        assert result1.returncode == 0, f"첫 번째 실행 실패: {result1.stderr}"
        plist_path = Path(mock_env["plist_path"])
        first_content = plist_path.read_text(encoding="utf-8")

        # 두 번째 실행
        result2 = _run_with_mock_launchctl(mock_env)
        assert result2.returncode == 0, f"두 번째 실행 실패: {result2.stderr}"
        second_content = plist_path.read_text(encoding="utf-8")

        assert first_content == second_content, "두 번 실행 결과가 다름 (멱등성 위반)"
