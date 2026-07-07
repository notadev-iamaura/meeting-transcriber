"""최초 설정 마법사용 readiness 점검 테스트."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from config import AppConfig, DiarizationConfig, PathsConfig, RecordingConfig, STTConfig
from security.setup_readiness import (
    ReadinessAction,
    ReadinessCheck,
    SetupReadinessReport,
    check_ffmpeg,
    check_hf_token_configured,
    check_python_runtime,
    check_stt_model,
    collect_setup_readiness,
)


def _make_config(
    base_dir: Path,
    *,
    token: str | None = "hf_test_secret",
    prefer_system_audio: bool = True,
    multi_track: bool = False,
    stt_model: str = "mlx-community/whisper-large-v3-turbo",
) -> AppConfig:
    """테스트용 AppConfig를 생성한다."""
    return AppConfig(
        paths=PathsConfig(base_dir=str(base_dir)),
        diarization=DiarizationConfig(huggingface_token=token),
        recording=RecordingConfig(
            enabled=True,
            prefer_system_audio=prefer_system_audio,
            multi_track=multi_track,
        ),
        stt=STTConfig(model_name=stt_model),
    )


def _prepare_base_dir(path: Path, mode: int = 0o700) -> None:
    """테스트용 데이터 디렉토리를 생성하고 권한을 맞춘다."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)


def _fake_device_process(output: str) -> subprocess.CompletedProcess[str]:
    """ffmpeg 장치 조회용 CompletedProcess를 생성한다."""
    return subprocess.CompletedProcess(
        args=["ffmpeg"],
        returncode=1,
        stdout="",
        stderr=output,
    )


def _checks_by_id(report: SetupReadinessReport) -> dict[str, str]:
    """보고서의 check status를 ID별 dict로 반환한다."""
    return {check.id: check.status for check in report.checks}


def _actions_by_id(check: ReadinessCheck) -> dict[str, ReadinessAction]:
    """check의 안내 액션을 ID별 dict로 반환한다."""
    return {action.id: action for action in check.actions}


def _patch_ready_stt(monkeypatch: pytest.MonkeyPatch) -> None:
    """활성 STT 모델을 ready로 판정하도록 패치한다."""
    from core.stt_model_status import ModelStatus

    monkeypatch.setattr(
        "security.setup_readiness.get_model_status",
        lambda *_args, **_kwargs: ModelStatus.READY,
        raising=False,
    )


class _FakeLauncherSpec:
    """Python runtime readiness 테스트용 launcher spec."""

    def __init__(self, *, source: str, python_executable: Path) -> None:
        self.python_source = source
        self.python_executable = python_executable

    def to_dict(self) -> dict[str, object]:
        """ui.launcher.LauncherSpec runtime subset을 반환한다."""
        is_file = self.python_executable.is_file()
        return {
            "runtime": {
                "python_source": self.python_source,
                "python_executable": str(self.python_executable),
                "candidates": [
                    {
                        "id": self.python_source,
                        "path": str(self.python_executable),
                        "exists": self.python_executable.exists(),
                        "is_file": is_file,
                        "is_executable": bool(
                            is_file and os.access(self.python_executable, os.X_OK)
                        ),
                        "selected": True,
                    }
                ],
            }
        }


def _patch_python_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    source: str = "managed_venv",
    exists: bool = True,
    python_path: Path | None = None,
) -> Path:
    """Python runtime readiness가 deterministic fake launcher spec을 사용하게 한다."""
    selected_path = python_path or tmp_path / "runtime" / "bin" / "python"
    if exists and python_path is None:
        selected_path.parent.mkdir(parents=True, exist_ok=True)
        selected_path.write_text("#!/bin/sh\n", encoding="utf-8")
        selected_path.chmod(0o755)

    monkeypatch.setattr(
        "security.setup_readiness.build_launcher_spec",
        lambda: _FakeLauncherSpec(source=source, python_executable=selected_path),
    )
    return selected_path


def _make_runtime_python(path: Path) -> Path:
    """테스트용 런처 handoff Python 파일을 만든다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_non_executable_runtime_python(path: Path) -> Path:
    """테스트용 실행 권한 없는 런처 handoff Python 파일을 만든다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o644)
    return path


def _patch_coreaudio_aggregate_names(
    monkeypatch: pytest.MonkeyPatch,
    names: set[str] | None = None,
) -> None:
    """CoreAudio Aggregate 조회가 실제 subprocess를 실행하지 않도록 패치한다."""
    monkeypatch.setattr(
        "security.setup_readiness.get_aggregate_device_names",
        lambda *_args, **_kwargs: names or set(),
    )


def test_collect_setup_readiness_ready_when_required_items_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """필수 항목이 준비되면 ready=true를 반환한다."""
    base_dir = tmp_path / "meeting-data"
    _prepare_base_dir(base_dir)
    config = _make_config(base_dir)
    device_output = """
    [AVFoundation indev @ 0x0] AVFoundation video devices:
    [AVFoundation indev @ 0x0] AVFoundation audio devices:
    [AVFoundation indev @ 0x0] [0] MacBook Air Microphone
    [AVFoundation indev @ 0x0] [1] BlackHole 2ch
    [AVFoundation indev @ 0x0] [2] Meeting Transcriber Aggregate
    """

    monkeypatch.setattr("security.setup_readiness.sys.platform", "darwin")
    monkeypatch.setattr("security.setup_readiness.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "security.setup_readiness.subprocess.run",
        lambda *_args, **_kwargs: _fake_device_process(device_output),
    )
    _patch_coreaudio_aggregate_names(monkeypatch)
    _patch_ready_stt(monkeypatch)
    _patch_python_runtime(monkeypatch, tmp_path)

    report = collect_setup_readiness(config)

    assert report.status == "pass"
    assert report.configured is True
    assert report.ready is True
    assert report.capabilities.recording_usable is True
    assert report.capabilities.full_meeting_capture_ready is True
    assert report.capabilities.has_blackhole is True
    assert report.capabilities.has_aggregate is True
    assert _checks_by_id(report) == {
        "base_dir": "pass",
        "python_runtime": "pass",
        "ffmpeg": "pass",
        "hf_token_env": "pass",
        "audio_devices": "pass",
        "stt_model": "pass",
    }


def test_python_runtime_ready_for_managed_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """관리형 venv 후보가 파일이면 Python runtime check는 통과한다."""
    selected_path = _patch_python_runtime(monkeypatch, tmp_path, source="managed_venv")

    check = check_python_runtime()

    assert check.status == "pass"
    assert check.ready is True
    assert check.details["advisory"] is True
    assert check.details["runtime_scope"] == "server_reconstructed"
    assert check.details["python_source"] == "managed_venv"
    assert check.details["python_executable"] == str(selected_path)
    assert check.details["selected_is_file"] is True
    assert check.details["selected_is_executable"] is True
    assert check.details["candidates"][0]["selected"] is True
    assert check.details["candidates"][0]["is_executable"] is True
    assert check.actions == ()


def test_python_runtime_current_interpreter_is_advisory_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """현재 인터프리터 fallback은 실행 가능하지만 재현성 경고로 표시한다."""
    _patch_python_runtime(
        monkeypatch,
        tmp_path,
        source="current_interpreter",
        python_path=Path(sys.executable),
    )

    check = check_python_runtime()

    assert check.status == "warn"
    assert check.ready is True
    assert check.details["selected_is_file"] is True
    assert check.details["selected_is_executable"] is True
    assert check.details["selected_matches_running_python"] is True
    actions = _actions_by_id(check)
    assert actions["check_system_python"].kind == "command"
    assert actions["check_system_python"].value == "python3 --version"
    assert actions["prepare_project_venv"].kind == "command"
    assert "cd <프로젝트_디렉토리>" in actions["prepare_project_venv"].value
    assert 'python -m pip install -e ".[dev]"' in actions["prepare_project_venv"].value
    assert actions["prepare_managed_venv"].kind == "command"
    assert "<관리형_venv>" in actions["prepare_managed_venv"].value


def test_python_runtime_uses_valid_launcher_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """런처가 전달한 비밀 없는 runtime handoff가 있으면 이를 우선 표시한다."""
    reconstructed_path = _patch_python_runtime(monkeypatch, tmp_path, source="managed_venv")
    handoff_python = _make_runtime_python(tmp_path / "handoff" / "bin" / "python")
    handoff_project = tmp_path / "handoff-project"
    handoff_project.mkdir()
    monkeypatch.setenv("MT_LAUNCHER_PYTHON_SOURCE", "explicit")
    monkeypatch.setenv("MT_LAUNCHER_PYTHON_EXECUTABLE", str(handoff_python))
    monkeypatch.setenv("MT_LAUNCHER_PROJECT_DIR", str(handoff_project))

    check = check_python_runtime()

    assert check.status == "pass"
    assert check.ready is True
    assert check.details["runtime_scope"] == "launcher_handoff"
    assert check.details["python_source"] == "explicit"
    assert check.details["python_executable"] == str(handoff_python.resolve())
    assert check.details["handoff_python_source"] == "explicit"
    assert check.details["handoff_project_dir"] == str(handoff_project.resolve())
    assert check.details["candidates"][0]["origin"] == "launcher_handoff"
    assert check.details["candidates"][0]["is_executable"] is True
    assert check.details["reconstructed_candidates"][0]["path"] == str(reconstructed_path)


def test_python_runtime_invalid_launcher_handoff_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """허용되지 않은 handoff source는 서버 재구성 진단으로 fallback한다."""
    reconstructed_path = _patch_python_runtime(monkeypatch, tmp_path, source="managed_venv")
    handoff_python = _make_runtime_python(tmp_path / "handoff" / "bin" / "python")
    monkeypatch.setenv("MT_LAUNCHER_PYTHON_SOURCE", "unexpected")
    monkeypatch.setenv("MT_LAUNCHER_PYTHON_EXECUTABLE", str(handoff_python))

    check = check_python_runtime()

    assert check.status == "pass"
    assert check.details["runtime_scope"] == "server_reconstructed"
    assert check.details["python_source"] == "managed_venv"
    assert check.details["python_executable"] == str(reconstructed_path)
    assert "handoff_python_source" not in check.details


def test_python_runtime_partial_launcher_handoff_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """부분 handoff env는 launcher_handoff로 해석하지 않는다."""
    reconstructed_path = _patch_python_runtime(monkeypatch, tmp_path, source="managed_venv")
    monkeypatch.setenv("MT_LAUNCHER_PYTHON_SOURCE", "explicit")
    monkeypatch.delenv("MT_LAUNCHER_PYTHON_EXECUTABLE", raising=False)

    check = check_python_runtime()

    assert check.status == "pass"
    assert check.details["runtime_scope"] == "server_reconstructed"
    assert check.details["python_executable"] == str(reconstructed_path)


def test_python_runtime_failure_is_advisory_for_top_level_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python runtime check 실패는 표시하되 기존 ready/configured 판정을 바꾸지 않는다."""
    base_dir = tmp_path / "meeting-data"
    _prepare_base_dir(base_dir)
    config = _make_config(base_dir, prefer_system_audio=False)

    monkeypatch.setattr("security.setup_readiness.sys.platform", "darwin")
    monkeypatch.setattr("security.setup_readiness.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "security.setup_readiness.subprocess.run",
        lambda *_args, **_kwargs: _fake_device_process("[0] MacBook Air Microphone"),
    )
    _patch_coreaudio_aggregate_names(monkeypatch)
    _patch_ready_stt(monkeypatch)
    missing_python = _patch_python_runtime(monkeypatch, tmp_path, exists=False)

    report = collect_setup_readiness(config)
    runtime = next(check for check in report.checks if check.id == "python_runtime")

    assert report.configured is True
    assert report.ready is True
    assert runtime.status == "fail"
    assert runtime.ready is False
    assert runtime.details["python_executable"] == str(missing_python)
    assert runtime.details["selected_is_file"] is False
    assert runtime.details["selected_is_executable"] is False
    actions = _actions_by_id(runtime)
    assert actions["prepare_project_venv"].kind == "command"
    assert "<프로젝트_디렉토리>" in actions["prepare_project_venv"].value
    assert actions["prepare_managed_venv"].kind == "command"
    assert "<관리형_venv>" in actions["prepare_managed_venv"].value


def test_missing_launcher_handoff_python_is_advisory_for_top_level_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """존재하지 않는 handoff Python은 check만 실패하고 전체 ready는 기존 기준을 유지한다."""
    base_dir = tmp_path / "meeting-data"
    _prepare_base_dir(base_dir)
    config = _make_config(base_dir, prefer_system_audio=False)
    missing_handoff_python = tmp_path / "missing" / "bin" / "python"

    monkeypatch.setenv("MT_LAUNCHER_PYTHON_SOURCE", "explicit")
    monkeypatch.setenv("MT_LAUNCHER_PYTHON_EXECUTABLE", str(missing_handoff_python))
    monkeypatch.setattr("security.setup_readiness.sys.platform", "darwin")
    monkeypatch.setattr("security.setup_readiness.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "security.setup_readiness.subprocess.run",
        lambda *_args, **_kwargs: _fake_device_process("[0] MacBook Air Microphone"),
    )
    _patch_coreaudio_aggregate_names(monkeypatch)
    _patch_ready_stt(monkeypatch)
    _patch_python_runtime(monkeypatch, tmp_path, source="managed_venv")

    report = collect_setup_readiness(config)
    runtime = next(check for check in report.checks if check.id == "python_runtime")

    assert report.configured is True
    assert report.ready is True
    assert runtime.status == "fail"
    assert runtime.ready is False
    assert runtime.details["runtime_scope"] == "launcher_handoff"
    assert runtime.details["python_executable"] == str(missing_handoff_python.resolve())
    assert runtime.details["selected_is_file"] is False
    assert runtime.details["selected_is_executable"] is False


def test_python_runtime_non_executable_candidate_fails_advisory_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python 후보가 파일이어도 실행 권한이 없으면 advisory check는 fail이다."""
    selected_path = _make_non_executable_runtime_python(tmp_path / "runtime" / "bin" / "python")
    _patch_python_runtime(
        monkeypatch,
        tmp_path,
        source="managed_venv",
        python_path=selected_path,
    )

    check = check_python_runtime()
    actions = _actions_by_id(check)

    assert check.status == "fail"
    assert check.ready is False
    assert check.details["python_executable"] == str(selected_path)
    assert check.details["selected_is_file"] is True
    assert check.details["selected_is_executable"] is False
    assert check.details["candidates"][0]["is_file"] is True
    assert check.details["candidates"][0]["is_executable"] is False
    assert actions["prepare_project_venv"].kind == "command"
    assert actions["prepare_managed_venv"].kind == "command"


def test_python_runtime_non_executable_launcher_handoff_fails_advisory_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """런처 handoff Python이 실행 불가 파일이면 launcher_handoff로 fail 표시한다."""
    _patch_python_runtime(monkeypatch, tmp_path, source="managed_venv")
    handoff_python = _make_non_executable_runtime_python(tmp_path / "handoff" / "bin" / "python")
    monkeypatch.setenv("MT_LAUNCHER_PYTHON_SOURCE", "explicit")
    monkeypatch.setenv("MT_LAUNCHER_PYTHON_EXECUTABLE", str(handoff_python))

    check = check_python_runtime()

    assert check.status == "fail"
    assert check.ready is False
    assert check.details["runtime_scope"] == "launcher_handoff"
    assert check.details["python_executable"] == str(handoff_python.resolve())
    assert check.details["selected_is_file"] is True
    assert check.details["selected_is_executable"] is False
    assert check.details["candidates"][0]["is_executable"] is False


def test_setup_readiness_python_runtime_never_exposes_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runtime details를 추가해도 토큰 값은 readiness 보고서에 섞이지 않는다."""
    secret = "hf_secret_must_not_escape_runtime"
    base_dir = tmp_path / "meeting-data"
    _prepare_base_dir(base_dir)
    config = _make_config(base_dir, token=secret, prefer_system_audio=False)

    monkeypatch.setenv("HUGGINGFACE_TOKEN", secret)
    monkeypatch.setattr("security.setup_readiness.sys.platform", "darwin")
    monkeypatch.setattr("security.setup_readiness.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "security.setup_readiness.subprocess.run",
        lambda *_args, **_kwargs: _fake_device_process("[0] MacBook Air Microphone"),
    )
    _patch_coreaudio_aggregate_names(monkeypatch)
    _patch_ready_stt(monkeypatch)
    _patch_python_runtime(monkeypatch, tmp_path)

    report = collect_setup_readiness(config)

    assert secret not in str(report)


def test_hf_token_check_never_exposes_secret(tmp_path: Path) -> None:
    """토큰 설정 여부만 반환하고 토큰 값은 노출하지 않는다."""
    secret = "hf_should_not_be_serialized"
    config = _make_config(tmp_path / "meeting-data", token=secret)

    check = check_hf_token_configured(config)

    assert check.status == "pass"
    assert secret not in str(check)
    assert check.details["configured"] is True
    assert check.details["config_token_configured"] is True


def test_hf_token_env_names_are_reported_without_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """환경변수 이름은 알려주되 값은 응답에 포함하지 않는다."""
    secret = "hf_env_secret_value"
    monkeypatch.setenv("HF_TOKEN", secret)
    config = _make_config(tmp_path / "meeting-data", token=None)

    check = check_hf_token_configured(config)

    assert check.status == "pass"
    assert check.details["environment_variables_present"] == ["HF_TOKEN"]
    assert secret not in str(check)


def test_missing_token_blocks_configuration(tmp_path: Path) -> None:
    """화자분리 토큰이 없으면 configured=false의 원인이 된다."""
    config = _make_config(tmp_path / "meeting-data", token=None)

    check = check_hf_token_configured(config)
    actions = _actions_by_id(check)

    assert check.status == "fail"
    assert check.ready is False
    assert "HUGGINGFACE_TOKEN" in check.action_hint
    assert actions["open_pyannote_diarization_terms"].kind == "external_link"
    assert actions["open_pyannote_diarization_terms"].value.startswith(
        "https://huggingface.co/pyannote/"
    )
    assert actions["open_hf_token_settings"].value == "https://huggingface.co/settings/tokens"
    assert actions["export_hf_token_placeholder"].kind == "command"
    assert "hf_xxxxx" in actions["export_hf_token_placeholder"].value
    assert "hf_test_secret" not in str(check)


def test_missing_ffmpeg_includes_inert_install_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffmpeg 누락 시 설치 명령은 실행이 아니라 표시용 action으로만 반환된다."""
    monkeypatch.setattr("security.setup_readiness.shutil.which", lambda _name: None)

    check = check_ffmpeg()
    actions = _actions_by_id(check)

    assert check.status == "fail"
    assert actions["install_ffmpeg"].kind == "command"
    assert actions["install_ffmpeg"].value == "brew install ffmpeg"


def test_missing_audio_devices_block_when_system_audio_is_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시스템 오디오 선호 구성에서는 loopback 장치 누락이 ready=false다."""
    base_dir = tmp_path / "meeting-data"
    _prepare_base_dir(base_dir)
    config = _make_config(base_dir, prefer_system_audio=True)

    monkeypatch.setattr("security.setup_readiness.sys.platform", "darwin")
    monkeypatch.setattr("security.setup_readiness.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "security.setup_readiness.subprocess.run",
        lambda *_args, **_kwargs: _fake_device_process("[0] MacBook Air Microphone"),
    )
    _patch_coreaudio_aggregate_names(monkeypatch)
    _patch_ready_stt(monkeypatch)

    report = collect_setup_readiness(config)
    audio = next(check for check in report.checks if check.id == "audio_devices")

    assert report.ready is False
    assert audio.status == "fail"
    assert audio.ready is False
    assert audio.details["has_blackhole"] is False
    assert audio.details["has_aggregate"] is False


def test_blackhole_without_aggregate_is_warn_and_not_full_capture_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BlackHole만 있으면 시스템 오디오는 가능하지만 전체 회의 캡처는 미완이다."""
    base_dir = tmp_path / "meeting-data"
    _prepare_base_dir(base_dir)
    config = _make_config(base_dir, prefer_system_audio=True)

    monkeypatch.setattr("security.setup_readiness.sys.platform", "darwin")
    monkeypatch.setattr("security.setup_readiness.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "security.setup_readiness.subprocess.run",
        lambda *_args, **_kwargs: _fake_device_process("[1] BlackHole 2ch"),
    )
    _patch_coreaudio_aggregate_names(monkeypatch)
    _patch_ready_stt(monkeypatch)

    report = collect_setup_readiness(config)
    audio = next(check for check in report.checks if check.id == "audio_devices")

    assert report.ready is False
    assert audio.status == "warn"
    assert audio.ready is False
    assert audio.details["has_blackhole"] is True
    assert audio.details["has_aggregate"] is False
    assert report.capabilities.full_meeting_capture_ready is False
    actions = _actions_by_id(audio)
    assert actions["check_audio_setup"].value == "bash scripts/setup_audio.sh --check"
    assert actions["create_aggregate_device"].kind == "command"
    assert actions["create_aggregate_device"].value == "bash scripts/setup_audio.sh"


def test_audio_devices_are_optional_when_system_audio_is_not_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """마이크 전용 구성에서는 loopback 장치 누락이 전체 준비 상태를 막지 않는다."""
    base_dir = tmp_path / "meeting-data"
    _prepare_base_dir(base_dir)
    config = _make_config(base_dir, prefer_system_audio=False)

    monkeypatch.setattr("security.setup_readiness.sys.platform", "darwin")
    monkeypatch.setattr("security.setup_readiness.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "security.setup_readiness.subprocess.run",
        lambda *_args, **_kwargs: _fake_device_process("[0] MacBook Air Microphone"),
    )
    _patch_coreaudio_aggregate_names(monkeypatch)
    _patch_ready_stt(monkeypatch)

    report = collect_setup_readiness(config)
    audio = next(check for check in report.checks if check.id == "audio_devices")

    assert report.ready is True
    assert audio.status == "pass"
    assert audio.ready is True
    assert report.capabilities.full_meeting_capture_ready is True


def test_ffmpeg_missing_blocks_and_does_not_probe_devices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffmpeg이 없으면 장치 조회 subprocess를 실행하지 않는다."""
    base_dir = tmp_path / "meeting-data"
    _prepare_base_dir(base_dir)
    config = _make_config(base_dir)
    run_mock = MagicMock()

    monkeypatch.setattr("security.setup_readiness.sys.platform", "darwin")
    monkeypatch.setattr("security.setup_readiness.shutil.which", lambda _name: None)
    monkeypatch.setattr("security.setup_readiness.subprocess.run", run_mock)
    _patch_coreaudio_aggregate_names(monkeypatch)
    _patch_ready_stt(monkeypatch)

    report = collect_setup_readiness(config)

    assert report.configured is False
    assert report.ready is False
    assert _checks_by_id(report)["ffmpeg"] == "fail"
    assert _checks_by_id(report)["audio_devices"] == "fail"
    run_mock.assert_not_called()


def test_device_probe_timeout_is_unknown_not_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """장치 조회 timeout은 500 대신 unknown check로 표현한다."""
    base_dir = tmp_path / "meeting-data"
    _prepare_base_dir(base_dir)
    config = _make_config(base_dir)

    def _raise_timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=3)

    monkeypatch.setattr("security.setup_readiness.sys.platform", "darwin")
    monkeypatch.setattr("security.setup_readiness.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr("security.setup_readiness.subprocess.run", _raise_timeout)
    _patch_coreaudio_aggregate_names(monkeypatch)
    _patch_ready_stt(monkeypatch)

    report = collect_setup_readiness(config)
    audio = next(check for check in report.checks if check.id == "audio_devices")

    assert report.ready is False
    assert audio.status == "unknown"
    assert audio.ready is False
    assert audio.details["probe_status"] == "error"


def test_wrong_base_dir_mode_is_warning_not_configuration_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """권장 권한과 다르면 경고하지만 configured 자체는 막지 않는다."""
    base_dir = tmp_path / "meeting-data"
    _prepare_base_dir(base_dir, mode=0o755)
    config = _make_config(base_dir, prefer_system_audio=False)

    monkeypatch.setattr("security.setup_readiness.sys.platform", "darwin")
    monkeypatch.setattr("security.setup_readiness.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "security.setup_readiness.subprocess.run",
        lambda *_args, **_kwargs: _fake_device_process("[0] MacBook Air Microphone"),
    )
    _patch_coreaudio_aggregate_names(monkeypatch)
    _patch_ready_stt(monkeypatch)

    report = collect_setup_readiness(config)
    base_dir_check = next(check for check in report.checks if check.id == "base_dir")

    assert report.configured is True
    assert report.ready is True
    assert base_dir_check.status == "warn"
    assert base_dir_check.details["actual_mode"] == "0o755"
    actions = _actions_by_id(base_dir_check)
    assert actions["fix_base_dir_permissions"].value == "chmod 0o700 <데이터_디렉토리>"
    assert str(base_dir) not in actions["fix_base_dir_permissions"].value


def test_setup_readiness_has_no_setup_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """readiness 조회는 디렉토리 보정, 설치, 네트워크 호출을 수행하지 않는다."""
    base_dir = tmp_path / "meeting-data"
    _prepare_base_dir(base_dir)
    config = _make_config(base_dir, prefer_system_audio=False)

    monkeypatch.setattr("security.setup_readiness.sys.platform", "darwin")
    monkeypatch.setattr("security.setup_readiness.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "security.setup_readiness.subprocess.run",
        lambda args, **_kwargs: (
            _fake_device_process("[0] MacBook Air Microphone")
            if "ffmpeg" in str(args[0])
            else pytest.fail(f"unexpected subprocess: {args}")
        ),
    )
    _patch_coreaudio_aggregate_names(monkeypatch)
    _patch_ready_stt(monkeypatch)
    _patch_python_runtime(
        monkeypatch,
        tmp_path,
        source="current_interpreter",
        python_path=Path(sys.executable),
    )

    with (
        patch("pathlib.Path.mkdir") as mkdir_mock,
        patch("pathlib.Path.chmod") as chmod_mock,
        patch("pathlib.Path.touch") as touch_mock,
        patch("security.secure_dir.SecureDirManager.ensure_secure_dirs") as ensure_mock,
        patch("urllib.request.urlopen") as urlopen_mock,
    ):
        report = collect_setup_readiness(config)

    assert report.configured is True
    runtime = next(check for check in report.checks if check.id == "python_runtime")
    assert runtime.status == "warn"
    assert _actions_by_id(runtime)["prepare_project_venv"].kind == "command"
    mkdir_mock.assert_not_called()
    chmod_mock.assert_not_called()
    touch_mock.assert_not_called()
    ensure_mock.assert_not_called()
    urlopen_mock.assert_not_called()


def test_multi_track_missing_blackhole_does_not_report_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """multi_track=True라도 BlackHole이 없으면 top-level ready=false다."""
    base_dir = tmp_path / "meeting-data"
    _prepare_base_dir(base_dir)
    config = _make_config(base_dir, prefer_system_audio=True, multi_track=True)

    monkeypatch.setattr("security.setup_readiness.sys.platform", "darwin")
    monkeypatch.setattr("security.setup_readiness.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "security.setup_readiness.subprocess.run",
        lambda *_args, **_kwargs: _fake_device_process("[0] MacBook Air Microphone"),
    )
    _patch_coreaudio_aggregate_names(monkeypatch)
    _patch_ready_stt(monkeypatch)

    report = collect_setup_readiness(config)

    assert report.ready is False
    assert report.capabilities.full_meeting_capture_ready is False
    assert _checks_by_id(report)["audio_devices"] == "fail"


def test_aggregate_detected_by_coreaudio_name_without_keyword(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """이름에 aggregate가 없어도 CoreAudio가 Aggregate로 판정한 장치를 반영한다."""
    base_dir = tmp_path / "meeting-data"
    _prepare_base_dir(base_dir)
    config = _make_config(base_dir)
    device_output = """
    [AVFoundation indev @ 0x0] [0] MacBook Air Microphone
    [AVFoundation indev @ 0x0] [1] BlackHole 2ch
    [AVFoundation indev @ 0x0] [2] 통합 오디오
    """

    monkeypatch.setattr("security.setup_readiness.sys.platform", "darwin")
    monkeypatch.setattr("security.setup_readiness.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "security.setup_readiness.subprocess.run",
        lambda *_args, **_kwargs: _fake_device_process(device_output),
    )
    _patch_coreaudio_aggregate_names(monkeypatch, {"통합 오디오"})
    _patch_ready_stt(monkeypatch)

    report = collect_setup_readiness(config)
    audio = next(check for check in report.checks if check.id == "audio_devices")

    assert report.ready is True
    assert audio.details["has_aggregate"] is True
    assert audio.details["coreaudio_aggregate_names"] == ["통합 오디오"]


def test_stt_model_ready_when_active_model_is_manual_import_path(tmp_path: Path) -> None:
    """수동 import로 활성화된 local effective path도 registered model로 판정한다."""
    from core.stt_model_registry import get_default, get_manual_import_dir

    base_dir = tmp_path / "meeting-data"
    _prepare_base_dir(base_dir)
    spec = get_default()
    manual_dir = Path(get_manual_import_dir(spec, base_dir=base_dir))
    manual_dir.mkdir(parents=True)
    (manual_dir / "config.json").write_text("{}", encoding="utf-8")
    (manual_dir / "weights.safetensors").write_bytes(b"weights")
    config = _make_config(base_dir, stt_model=str(manual_dir))

    check = check_stt_model(config)

    assert check.status == "pass"
    assert check.ready is True
    assert check.details["registered"] is True
    assert check.details["active_model_id"] == spec.id


def test_stt_model_not_ready_links_to_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """활성 STT 모델이 없으면 설정 화면 route action을 반환한다."""
    from core.stt_model_status import ModelStatus

    monkeypatch.setattr(
        "security.setup_readiness.get_model_status",
        lambda *_args, **_kwargs: ModelStatus.NOT_DOWNLOADED,
        raising=False,
    )
    config = _make_config(tmp_path / "meeting-data")

    check = check_stt_model(config)
    actions = _actions_by_id(check)

    assert check.status == "warn"
    assert check.ready is False
    assert actions["open_stt_settings"].kind == "route"
    assert actions["open_stt_settings"].value == "/app/settings"
