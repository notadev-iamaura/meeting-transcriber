"""
설치 스크립트 테스트 모듈

목적: scripts/install.sh의 동작을 검증한다.
주요 테스트:
  - 스크립트 파일 존재 및 실행 권한
  - bash 문법 검증
  - --help 옵션 동작
  - --check 옵션 동작
  - 알 수 없는 옵션 에러 처리
  - 함수별 단위 검증 (source 모드)
  - 디렉토리 생성 및 보안 설정
  - 멱등성 (여러 번 실행 시 안전)
  - requirements.txt 생성
의존성: subprocess, pathlib
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# 스크립트 경로
_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "install.sh"


def _create_mock_env(
    tmp_path: Path,
    *,
    create_brew: bool = True,
    create_python: bool = True,
    create_ffmpeg: bool = True,
    create_ollama: bool = True,
    create_venv: bool = False,
    create_data_dir: bool = False,
) -> dict[str, str]:
    """격리된 테스트 환경을 구성하는 헬퍼 함수.

    실제 시스템에 영향 없이 install.sh의 개별 함수를
    테스트할 수 있도록 가짜 바이너리와 디렉토리를 구성한다.

    Args:
        tmp_path: pytest가 제공하는 임시 디렉토리
        create_brew: 가짜 brew 바이너리 생성 여부
        create_python: 가짜 python3.11 바이너리 생성 여부
        create_ffmpeg: 가짜 ffmpeg 바이너리 생성 여부
        create_ollama: 가짜 ollama 바이너리 생성 여부
        create_venv: 가짜 venv 디렉토리 생성 여부
        create_data_dir: 가짜 데이터 디렉토리 생성 여부

    Returns:
        테스트 환경 정보 딕셔너리
    """
    # 가짜 HOME 디렉토리
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    # 가짜 mock bin 디렉토리
    mock_bin = tmp_path / "mock_bin"
    mock_bin.mkdir()

    # 가짜 brew
    if create_brew:
        brew_bin = mock_bin / "brew"
        brew_bin.write_text(
            "#!/bin/bash\n"
            'if [[ "$1" == "--version" ]]; then\n'
            '  echo "Homebrew 4.2.0"\n'
            'elif [[ "$1" == "install" ]]; then\n'
            '  echo "가짜 brew install: $2"\n'
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        brew_bin.chmod(0o755)

    # 가짜 python3.11
    if create_python:
        py_bin = mock_bin / "python3.11"
        py_bin.write_text(
            "#!/bin/bash\n"
            'if [[ "$1" == "--version" ]]; then\n'
            '  echo "Python 3.11.7"\n'
            'elif [[ "$1" == "-m" && "$2" == "venv" ]]; then\n'
            "  # 가짜 venv 생성\n"
            '  mkdir -p "$3/bin"\n'
            '  cp "$0" "$3/bin/python"\n'
            "  cat > \"$3/bin/pip\" << 'PIPEOF'\n"
            "#!/bin/bash\n"
            'echo "가짜 pip: $@"\n'
            "exit 0\n"
            "PIPEOF\n"
            '  chmod +x "$3/bin/pip"\n'
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        py_bin.chmod(0o755)

        # python3도 생성 (check_python_version에서 python3도 확인)
        py3_bin = mock_bin / "python3"
        py3_bin.write_text(
            "#!/bin/bash\n"
            'if [[ "$1" == "--version" ]]; then\n'
            '  echo "Python 3.11.7"\n'
            'elif [[ "$1" == "-m" && "$2" == "venv" ]]; then\n'
            '  mkdir -p "$3/bin"\n'
            '  cp "$0" "$3/bin/python"\n'
            "  cat > \"$3/bin/pip\" << 'PIPEOF'\n"
            "#!/bin/bash\n"
            'echo "가짜 pip: $@"\n'
            "exit 0\n"
            "PIPEOF\n"
            '  chmod +x "$3/bin/pip"\n'
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        py3_bin.chmod(0o755)

    # 가짜 ffmpeg
    if create_ffmpeg:
        ffmpeg_bin = mock_bin / "ffmpeg"
        ffmpeg_bin.write_text(
            "#!/bin/bash\n"
            'if [[ "$1" == "-version" ]]; then\n'
            '  echo "ffmpeg version 6.1"\n'
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        ffmpeg_bin.chmod(0o755)

    # 가짜 ollama
    if create_ollama:
        ollama_bin = mock_bin / "ollama"
        ollama_bin.write_text(
            "#!/bin/bash\n"
            'if [[ "$1" == "--version" ]]; then\n'
            '  echo "ollama version 0.1.29"\n'
            'elif [[ "$1" == "list" ]]; then\n'
            '  echo "exaone3.5:7.8b-instruct-q4_K_M  abc123  4.5 GB"\n'
            'elif [[ "$1" == "pull" ]]; then\n'
            '  echo "가짜 ollama pull: $2"\n'
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        ollama_bin.chmod(0o755)

    # 가짜 sw_vers (macOS 버전)
    sw_vers_bin = mock_bin / "sw_vers"
    sw_vers_bin.write_text(
        '#!/bin/bash\necho "15.2"\n',
        encoding="utf-8",
    )
    sw_vers_bin.chmod(0o755)

    # 가짜 df (디스크 여유 확인)
    df_bin = mock_bin / "df"
    df_bin.write_text(
        "#!/bin/bash\n"
        'echo "Filesystem   1G-blocks  Used  Available"\n'
        'echo "/dev/disk1s1 500        200   300"\n',
        encoding="utf-8",
    )
    df_bin.chmod(0o755)

    # 가짜 curl (Ollama 서버 확인)
    curl_bin = mock_bin / "curl"
    curl_bin.write_text(
        '#!/bin/bash\necho "{}"\nexit 0\n',
        encoding="utf-8",
    )
    curl_bin.chmod(0o755)

    # 가짜 stat (macOS 형식)
    stat_bin = mock_bin / "stat"
    stat_bin.write_text(
        '#!/bin/bash\necho "700"\n',
        encoding="utf-8",
    )
    stat_bin.chmod(0o755)

    # 가짜 venv (선택적)
    venv_dir = fake_home / ".meeting-transcriber-venv"
    if create_venv:
        (venv_dir / "bin").mkdir(parents=True)
        python_bin = venv_dir / "bin" / "python"
        python_bin.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        python_bin.chmod(0o755)
        pip_bin = venv_dir / "bin" / "pip"
        pip_bin.write_text(
            '#!/bin/bash\necho "가짜 pip: $@"\nexit 0\n',
            encoding="utf-8",
        )
        pip_bin.chmod(0o755)

    # 가짜 데이터 디렉토리 (선택적)
    data_dir = fake_home / ".meeting-transcriber"
    if create_data_dir:
        data_dir.mkdir(parents=True)
        data_dir.chmod(0o700)

    # 가짜 프로젝트 디렉토리
    project_dir = tmp_path / "project"
    scripts_dir = project_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (project_dir / "main.py").touch()

    # 스크립트 복사
    test_script = scripts_dir / "install.sh"
    test_script.write_text(
        _SCRIPT_PATH.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    test_script.chmod(0o755)

    return {
        "home": str(fake_home),
        "mock_bin": str(mock_bin),
        "venv_dir": str(venv_dir),
        "data_dir": str(data_dir),
        "project_dir": str(project_dir),
        "script": str(test_script),
    }


def _run_script(
    mock_env: dict[str, str],
    args: list[str] | None = None,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """가짜 바이너리 환경에서 스크립트를 실행하는 헬퍼.

    Args:
        mock_env: _create_mock_env()의 반환값
        args: 스크립트에 전달할 인자
        extra_env: 추가 환경변수

    Returns:
        subprocess.CompletedProcess 인스턴스
    """
    env = {
        "HOME": mock_env["home"],
        "PATH": f"{mock_env['mock_bin']}:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "ko_KR.UTF-8",
    }
    if extra_env:
        env.update(extra_env)

    cmd = ["bash", mock_env["script"]]
    if args:
        cmd.extend(args)

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def _source_and_call(
    mock_env: dict[str, str],
    function_call: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """스크립트를 source한 후 특정 함수를 호출하는 헬퍼.

    Args:
        mock_env: _create_mock_env()의 반환값
        function_call: 호출할 함수 (예: "check_homebrew")
        extra_env: 추가 환경변수

    Returns:
        subprocess.CompletedProcess 인스턴스
    """
    env = {
        "HOME": mock_env["home"],
        "PATH": f"{mock_env['mock_bin']}:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "ko_KR.UTF-8",
    }
    if extra_env:
        env.update(extra_env)

    # source 후 함수 호출
    bash_cmd = f'source "{mock_env["script"]}" && {function_call}'

    return subprocess.run(
        ["bash", "-c", bash_cmd],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


class TestScriptExists:
    """스크립트 파일 존재 및 실행 권한 검증."""

    def test_스크립트_파일_존재(self) -> None:
        """install.sh 파일이 존재하는지 확인한다."""
        assert _SCRIPT_PATH.exists(), f"스크립트 파일 없음: {_SCRIPT_PATH}"

    def test_스크립트_실행_권한(self) -> None:
        """install.sh에 실행 권한이 있는지 확인한다."""
        assert os.access(_SCRIPT_PATH, os.X_OK), "스크립트에 실행 권한 없음"

    def test_스크립트_셸_해석기(self) -> None:
        """셸 스크립트 첫 줄이 #!/bin/bash인지 확인한다."""
        first_line = _SCRIPT_PATH.read_text(encoding="utf-8").split("\n")[0]
        assert first_line == "#!/bin/bash", f"예상: #!/bin/bash, 실제: {first_line}"


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

    def test_source_모드_지원(self) -> None:
        """source 시 main이 호출되지 않는 가드가 있는지 확인한다."""
        content = _SCRIPT_PATH.read_text(encoding="utf-8")
        assert "BASH_SOURCE[0]" in content
        assert '"${0}"' in content


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
        assert "--check" in result.stdout
        assert "설치" in result.stdout

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
        assert "--check" in result.stdout

    def test_help_설치항목_목록(self) -> None:
        """도움말에 설치 항목 목록이 포함되어 있는지 확인한다."""
        result = subprocess.run(
            ["bash", str(_SCRIPT_PATH), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert "Homebrew" in result.stdout
        assert "Python" in result.stdout
        assert "ffmpeg" in result.stdout
        assert "Ollama" in result.stdout
        assert "EXAONE" in result.stdout


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


class TestCheckMacos:
    """macOS 확인 함수 검증."""

    def test_darwin_에서_성공(self, tmp_path: Path) -> None:
        """macOS(Darwin)에서 check_macos가 성공하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path)
        # uname -s가 "Darwin"을 반환하므로 성공해야 함
        result = _source_and_call(mock_env, "check_macos")
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "macOS 확인 완료" in combined


class TestCheckHomebrew:
    """Homebrew 확인 함수 검증."""

    def test_brew_있을_때_성공(self, tmp_path: Path) -> None:
        """brew가 PATH에 있으면 성공하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_brew=True)
        result = _source_and_call(mock_env, "check_homebrew")
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "Homebrew" in combined

    def test_brew_없을_때_실패(self, tmp_path: Path) -> None:
        """brew가 없으면 실패하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_brew=False)
        result = _source_and_call(mock_env, "check_homebrew")
        assert result.returncode != 0
        assert "Homebrew" in result.stderr


class TestCheckPythonVersion:
    """Python 버전 확인 함수 검증."""

    def test_python3_11_있을_때_성공(self, tmp_path: Path) -> None:
        """python3.11이 PATH에 있으면 성공하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_python=True)
        result = _source_and_call(mock_env, "check_python_version")
        assert result.returncode == 0
        assert "python3" in result.stdout

    def test_python_없을_때_실패(self, tmp_path: Path) -> None:
        """python3.11+이 없으면 실패하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_python=False)
        result = _source_and_call(mock_env, "check_python_version")
        assert result.returncode != 0


class TestCheckFfmpeg:
    """ffmpeg 확인 함수 검증."""

    def test_ffmpeg_있을_때_성공(self, tmp_path: Path) -> None:
        """ffmpeg가 PATH에 있으면 성공하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_ffmpeg=True)
        result = _source_and_call(mock_env, "check_ffmpeg")
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "ffmpeg" in combined

    def test_ffmpeg_없을_때_실패(self, tmp_path: Path) -> None:
        """ffmpeg가 없으면 실패하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_ffmpeg=False)
        result = _source_and_call(mock_env, "check_ffmpeg")
        assert result.returncode != 0


class TestCheckOllama:
    """Ollama 확인 함수 검증."""

    def test_ollama_있을_때_성공(self, tmp_path: Path) -> None:
        """ollama가 PATH에 있으면 성공하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_ollama=True)
        result = _source_and_call(mock_env, "check_ollama")
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "Ollama" in combined

    def test_ollama_없을_때_실패(self, tmp_path: Path) -> None:
        """ollama가 없으면 실패하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_ollama=False)
        result = _source_and_call(mock_env, "check_ollama")
        assert result.returncode != 0
        assert "Ollama" in result.stderr


class TestCheckExaoneModel:
    """EXAONE 모델 존재 확인 검증."""

    def test_모델_있을_때_성공(self, tmp_path: Path) -> None:
        """ollama list에서 exaone3.5가 보이면 성공하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_ollama=True)
        result = _source_and_call(mock_env, "check_exaone_model")
        assert result.returncode == 0

    def test_모델_없을_때_실패(self, tmp_path: Path) -> None:
        """ollama list에서 모델이 없으면 실패하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_ollama=True)
        # ollama가 빈 목록을 반환하도록 수정
        ollama_bin = Path(mock_env["mock_bin"]) / "ollama"
        ollama_bin.write_text(
            '#!/bin/bash\nif [[ "$1" == "list" ]]; then\n  echo "NAME  ID  SIZE"\nfi\nexit 0\n',
            encoding="utf-8",
        )
        ollama_bin.chmod(0o755)
        result = _source_and_call(mock_env, "check_exaone_model")
        assert result.returncode != 0


class TestCreateVenv:
    """가상환경 생성 함수 검증."""

    def test_새_venv_생성(self, tmp_path: Path) -> None:
        """새로운 가상환경을 생성하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_venv=False, create_python=True)
        result = _source_and_call(
            mock_env,
            'create_venv "python3.11"',
        )
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "가상환경" in combined

    def test_기존_venv_건너뛰기(self, tmp_path: Path) -> None:
        """이미 venv가 있으면 건너뛰는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_venv=True)
        result = _source_and_call(
            mock_env,
            'create_venv "python3.11"',
        )
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "이미 존재" in combined


class TestSetupDirectories:
    """디렉토리 생성 및 보안 설정 검증."""

    def test_디렉토리_구조_생성(self, tmp_path: Path) -> None:
        """필요한 디렉토리가 모두 생성되는지 확인한다."""
        mock_env = _create_mock_env(tmp_path)
        result = _source_and_call(mock_env, "setup_directories")
        assert result.returncode == 0

        data_dir = Path(mock_env["data_dir"])
        assert data_dir.exists()
        assert (data_dir / "audio_input").exists()
        assert (data_dir / "outputs").exists()
        assert (data_dir / "checkpoints").exists()
        assert (data_dir / "chroma_db").exists()
        assert (data_dir / "logs").exists()

    def test_chmod_700_적용(self, tmp_path: Path) -> None:
        """데이터 디렉토리에 chmod 700이 적용되는지 확인한다."""
        mock_env = _create_mock_env(tmp_path)
        _source_and_call(mock_env, "setup_directories")

        data_dir = Path(mock_env["data_dir"])
        mode = data_dir.stat().st_mode & 0o777
        assert mode == 0o700, f"예상 권한: 700, 실제: {oct(mode)}"

    def test_metadata_never_index_생성(self, tmp_path: Path) -> None:
        """Spotlight 제외 파일이 생성되는지 확인한다."""
        mock_env = _create_mock_env(tmp_path)
        _source_and_call(mock_env, "setup_directories")

        never_index = Path(mock_env["data_dir"]) / ".metadata_never_index"
        assert never_index.exists()

    def test_gitignore_생성(self, tmp_path: Path) -> None:
        """.gitignore가 생성되는지 확인한다."""
        mock_env = _create_mock_env(tmp_path)
        _source_and_call(mock_env, "setup_directories")

        gitignore = Path(mock_env["data_dir"]) / ".gitignore"
        assert gitignore.exists()
        assert gitignore.read_text(encoding="utf-8").strip() == "*"

    def test_멱등성_디렉토리(self, tmp_path: Path) -> None:
        """두 번 실행해도 에러 없이 동작하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path)
        # 첫 번째 실행
        result1 = _source_and_call(mock_env, "setup_directories")
        assert result1.returncode == 0

        # 두 번째 실행
        result2 = _source_and_call(mock_env, "setup_directories")
        assert result2.returncode == 0


class TestGenerateRequirements:
    """requirements.txt 생성 검증."""

    def test_requirements_파일_생성(self, tmp_path: Path) -> None:
        """requirements.txt가 생성되는지 확인한다."""
        mock_env = _create_mock_env(tmp_path)
        result = _source_and_call(mock_env, "generate_requirements")
        assert result.returncode == 0

        req_file = Path(mock_env["project_dir"]) / "requirements.txt"
        assert req_file.exists()

    def test_requirements_pyproject_참조_스텁(self, tmp_path: Path) -> None:
        """requirements.txt가 pyproject.toml 참조 스텁으로 생성되는지 확인한다."""
        mock_env = _create_mock_env(tmp_path)
        _source_and_call(mock_env, "generate_requirements")

        req_file = Path(mock_env["project_dir"]) / "requirements.txt"
        content = req_file.read_text(encoding="utf-8")

        # pyproject.toml을 SSOT로 참조하는 스텁 확인
        assert "-e ." in content
        assert "SSOT" in content
        assert "pyproject.toml" in content

    def test_기존_파일_보존(self, tmp_path: Path) -> None:
        """이미 requirements.txt가 있으면 덮어쓰지 않는지 확인한다."""
        mock_env = _create_mock_env(tmp_path)

        # 기존 파일 생성
        req_file = Path(mock_env["project_dir"]) / "requirements.txt"
        req_file.write_text("기존_내용\n", encoding="utf-8")

        _source_and_call(mock_env, "generate_requirements")

        # 기존 내용이 보존되어야 함
        content = req_file.read_text(encoding="utf-8")
        assert "기존_내용" in content


class TestCheckOption:
    """--check 옵션 동작 검증."""

    def test_check_출력(self, tmp_path: Path) -> None:
        """--check 옵션이 설치 상태를 출력하는지 확인한다."""
        mock_env = _create_mock_env(
            tmp_path,
            create_venv=True,
            create_data_dir=True,
        )
        result = _run_script(mock_env, ["--check"])
        combined = result.stdout + result.stderr
        assert "설치 상태 확인" in combined

    def test_check_brew_상태(self, tmp_path: Path) -> None:
        """--check가 Homebrew 상태를 표시하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_brew=True)
        result = _run_script(mock_env, ["--check"])
        combined = result.stdout + result.stderr
        assert "Homebrew" in combined

    def test_check_python_상태(self, tmp_path: Path) -> None:
        """--check가 Python 상태를 표시하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_python=True)
        result = _run_script(mock_env, ["--check"])
        combined = result.stdout + result.stderr
        assert "Python" in combined

    def test_check_ffmpeg_상태(self, tmp_path: Path) -> None:
        """--check가 ffmpeg 상태를 표시하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path, create_ffmpeg=True)
        result = _run_script(mock_env, ["--check"])
        combined = result.stdout + result.stderr
        assert "ffmpeg" in combined


class TestCheckDiskSpace:
    """디스크 여유 공간 확인 함수 검증."""

    def test_충분한_여유_시_성공(self, tmp_path: Path) -> None:
        """디스크 여유가 충분하면 성공하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path)
        result = _source_and_call(mock_env, "check_disk_space")
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "디스크 여유 공간" in combined

    def test_여유_부족_시_실패(self, tmp_path: Path) -> None:
        """디스크 여유가 부족하면 실패하는지 확인한다."""
        mock_env = _create_mock_env(tmp_path)
        # df가 5GB만 반환하도록 수정
        df_bin = Path(mock_env["mock_bin"]) / "df"
        df_bin.write_text(
            "#!/bin/bash\n"
            'echo "Filesystem   1G-blocks  Used  Available"\n'
            'echo "/dev/disk1s1 500        495   5"\n',
            encoding="utf-8",
        )
        df_bin.chmod(0o755)
        result = _source_and_call(mock_env, "check_disk_space")
        assert result.returncode != 0
        assert "디스크 여유 공간 부족" in result.stderr


class TestScriptContents:
    """스크립트 내용 검증."""

    def test_한국어_메시지(self) -> None:
        """스크립트 출력이 한국어로 되어 있는지 확인한다."""
        content = _SCRIPT_PATH.read_text(encoding="utf-8")
        assert "설치" in content
        assert "완료" in content
        assert "오류" in content

    def test_exaone_모델명_포함(self) -> None:
        """EXAONE 모델명이 스크립트에 포함되어 있는지 확인한다."""
        content = _SCRIPT_PATH.read_text(encoding="utf-8")
        assert "exaone3.5:7.8b-instruct-q4_K_M" in content

    def test_venv_경로_포함(self) -> None:
        """venv 경로가 스크립트에 포함되어 있는지 확인한다."""
        content = _SCRIPT_PATH.read_text(encoding="utf-8")
        assert ".meeting-transcriber-venv" in content

    def test_data_dir_경로_포함(self) -> None:
        """데이터 디렉토리 경로가 스크립트에 포함되어 있는지 확인한다."""
        content = _SCRIPT_PATH.read_text(encoding="utf-8")
        assert ".meeting-transcriber" in content

    def test_ollama_host_localhost(self) -> None:
        """Ollama 호스트가 localhost인지 확인한다."""
        content = _SCRIPT_PATH.read_text(encoding="utf-8")
        assert "127.0.0.1:11434" in content

    def test_huggingface_토큰_안내(self) -> None:
        """HuggingFace 토큰 설정 안내가 포함되어 있는지 확인한다."""
        content = _SCRIPT_PATH.read_text(encoding="utf-8")
        assert "HUGGINGFACE_TOKEN" in content

    def test_launchagent_안내(self) -> None:
        """LaunchAgent 설정 안내가 포함되어 있는지 확인한다."""
        content = _SCRIPT_PATH.read_text(encoding="utf-8")
        assert "setup_launchagent.sh" in content

    def test_print_summary_8단계_안내(self) -> None:
        """설치 완료 후 다음 단계 안내가 포함되어 있는지 확인한다."""
        content = _SCRIPT_PATH.read_text(encoding="utf-8")
        assert "가상환경 활성화" in content
        assert "python main.py" in content


class TestIdempotency:
    """멱등성 검증 — 여러 번 실행해도 안전."""

    def test_디렉토리_재생성_멱등(self, tmp_path: Path) -> None:
        """디렉토리 설정을 두 번 실행해도 동일한 결과인지 확인한다."""
        mock_env = _create_mock_env(tmp_path)

        # 첫 번째 실행
        result1 = _source_and_call(mock_env, "setup_directories")
        assert result1.returncode == 0

        data_dir = Path(mock_env["data_dir"])
        first_mode = data_dir.stat().st_mode & 0o777

        # 두 번째 실행
        result2 = _source_and_call(mock_env, "setup_directories")
        assert result2.returncode == 0

        second_mode = data_dir.stat().st_mode & 0o777
        assert first_mode == second_mode

    def test_requirements_재생성_시_기존_보존(self, tmp_path: Path) -> None:
        """requirements.txt가 이미 있으면 덮어쓰지 않는지 확인한다."""
        mock_env = _create_mock_env(tmp_path)
        req_file = Path(mock_env["project_dir"]) / "requirements.txt"
        req_file.write_text("custom_package==1.0\n", encoding="utf-8")

        _source_and_call(mock_env, "generate_requirements")

        content = req_file.read_text(encoding="utf-8")
        assert "custom_package" in content
