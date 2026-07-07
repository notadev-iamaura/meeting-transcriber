"""최초 설정 마법사용 read-only 준비 상태 점검."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from config import AppConfig
from core.coreaudio_helper import get_aggregate_device_names
from core.stt_model_registry import STT_MODELS
from core.stt_model_status import (
    ModelStatus,
    get_effective_model_path,
    get_model_status,
)
from ui.launcher import (
    LAUNCHER_PROJECT_DIR_ENV,
    LAUNCHER_PYTHON_EXECUTABLE_ENV,
    LAUNCHER_PYTHON_SOURCE_ENV,
    build_launcher_spec,
)

ReadinessStatus = Literal["pass", "warn", "fail", "unknown"]
ReadinessActionKind = Literal["external_link", "route", "command"]

_AUDIO_DEVICE_TIMEOUT_SECONDS = 3.0
_AGGREGATE_DEVICE_NAME = "Meeting Transcriber Aggregate"
_HF_TOKEN_ENV_NAMES = ("HUGGINGFACE_TOKEN", "HF_TOKEN")
_HF_DIARIZATION_MODEL_URL = "https://huggingface.co/pyannote/speaker-diarization-community-1"
_HF_SEGMENTATION_MODEL_URL = "https://huggingface.co/pyannote/segmentation-3.0"
_HF_TOKEN_SETTINGS_URL = "https://huggingface.co/settings/tokens"
_VALID_LAUNCHER_PYTHON_SOURCES = frozenset(
    {"explicit", "project_venv", "managed_venv", "current_interpreter"}
)


@dataclass(frozen=True)
class ReadinessAction:
    """설정 마법사가 표시만 할 수 있는 다음 단계 안내."""

    id: str
    label: str
    kind: ReadinessActionKind
    value: str
    description: str = ""


@dataclass(frozen=True)
class ReadinessCheck:
    """설정 마법사에서 표시할 단일 준비 상태 항목."""

    id: str
    status: ReadinessStatus
    ready: bool
    message: str
    action_hint: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    actions: tuple[ReadinessAction, ...] = ()


@dataclass(frozen=True)
class SetupCapabilities:
    """첫 실행 마법사가 판단에 사용할 기능별 준비 상태."""

    recording_usable: bool
    full_meeting_capture_ready: bool
    has_blackhole: bool
    has_aggregate: bool
    stt_model_ready: bool

    def to_dict(self) -> dict[str, bool]:
        """JSON 응답에 사용할 dict로 변환한다."""
        return {
            "recording_usable": self.recording_usable,
            "full_meeting_capture_ready": self.full_meeting_capture_ready,
            "has_blackhole": self.has_blackhole,
            "has_aggregate": self.has_aggregate,
            "stt_model_ready": self.stt_model_ready,
        }


@dataclass(frozen=True)
class SetupReadinessReport:
    """설정 마법사용 준비 상태 보고서."""

    status: str
    configured: bool
    ready: bool
    capabilities: SetupCapabilities
    checks: list[ReadinessCheck]


def collect_setup_readiness(config: AppConfig) -> SetupReadinessReport:
    """설정 마법사가 사용할 로컬 준비 상태를 수집한다.

    네트워크 호출, 패키지 import, 설치/권한 변경을 하지 않는 read-only 점검만 수행한다.
    """
    base_dir_check = check_base_dir(config)
    python_runtime_check = check_python_runtime()
    ffmpeg_check = check_ffmpeg()
    hf_token_check = check_hf_token_configured(config)
    audio_check = check_audio_devices(config)
    stt_check = check_stt_model(config)

    checks = [
        base_dir_check,
        python_runtime_check,
        ffmpeg_check,
        hf_token_check,
        audio_check,
        stt_check,
    ]
    configured = all(check.ready for check in (base_dir_check, ffmpeg_check, hf_token_check))
    has_blackhole = bool(audio_check.details.get("has_blackhole", False))
    has_aggregate = bool(audio_check.details.get("has_aggregate", False))
    recording_usable = bool(config.recording.enabled and ffmpeg_check.ready)
    full_meeting_capture_ready = bool(recording_usable and audio_check.ready)
    stt_model_ready = stt_check.ready
    ready = bool(configured and audio_check.ready and stt_model_ready)
    status = "pass" if ready else "fail"

    return SetupReadinessReport(
        status=status,
        configured=configured,
        ready=ready,
        capabilities=SetupCapabilities(
            recording_usable=recording_usable,
            full_meeting_capture_ready=full_meeting_capture_ready,
            has_blackhole=has_blackhole,
            has_aggregate=has_aggregate,
            stt_model_ready=stt_model_ready,
        ),
        checks=checks,
    )


def _base_dir_create_action(expected_mode: int) -> tuple[ReadinessAction, ...]:
    """데이터 디렉토리 생성 안내 액션을 반환한다."""
    return (
        ReadinessAction(
            id="create_base_dir",
            label="디렉토리 생성 예시",
            kind="command",
            value=f"mkdir -p <데이터_디렉토리> && chmod {oct(expected_mode)} <데이터_디렉토리>",
            description="실제 경로를 확인한 뒤 터미널에서 직접 실행하세요.",
        ),
    )


def _base_dir_permission_action(expected_mode: int) -> tuple[ReadinessAction, ...]:
    """데이터 디렉토리 권한 보정 안내 액션을 반환한다."""
    return (
        ReadinessAction(
            id="fix_base_dir_permissions",
            label="권한 보정 예시",
            kind="command",
            value=f"chmod {oct(expected_mode)} <데이터_디렉토리>",
            description="민감한 회의 데이터는 소유자만 접근할 수 있어야 합니다.",
        ),
    )


def _ffmpeg_actions() -> tuple[ReadinessAction, ...]:
    """ffmpeg 설치 안내 액션을 반환한다."""
    return (
        ReadinessAction(
            id="install_ffmpeg",
            label="ffmpeg 설치 명령",
            kind="command",
            value="brew install ffmpeg",
            description="설치 후 readiness 화면을 새로고침하세요.",
        ),
    )


def _hf_token_actions() -> tuple[ReadinessAction, ...]:
    """HuggingFace 토큰 설정 안내 액션을 반환한다."""
    return (
        ReadinessAction(
            id="open_pyannote_diarization_terms",
            label="화자분리 모델 약관 열기",
            kind="external_link",
            value=_HF_DIARIZATION_MODEL_URL,
        ),
        ReadinessAction(
            id="open_pyannote_segmentation_terms",
            label="세그먼트 모델 약관 열기",
            kind="external_link",
            value=_HF_SEGMENTATION_MODEL_URL,
        ),
        ReadinessAction(
            id="open_hf_token_settings",
            label="토큰 발급 페이지 열기",
            kind="external_link",
            value=_HF_TOKEN_SETTINGS_URL,
        ),
        ReadinessAction(
            id="export_hf_token_placeholder",
            label="환경변수 설정 예시",
            kind="command",
            value="export HUGGINGFACE_TOKEN=hf_xxxxx\nexport HF_TOKEN=hf_xxxxx",
            description="실제 토큰 값은 readiness 응답이나 화면에 표시하지 않습니다.",
        ),
    )


def _audio_device_check_actions() -> tuple[ReadinessAction, ...]:
    """오디오 장치 재점검 안내 액션을 반환한다."""
    return (
        ReadinessAction(
            id="check_audio_setup",
            label="오디오 장치 점검 명령",
            kind="command",
            value="bash scripts/setup_audio.sh --check",
            description="BlackHole과 Aggregate Device 상태만 확인합니다.",
        ),
    )


def _aggregate_device_actions() -> tuple[ReadinessAction, ...]:
    """Aggregate Device 생성 안내 액션을 반환한다."""
    return _audio_device_check_actions() + (
        ReadinessAction(
            id="create_aggregate_device",
            label="Aggregate Device 생성 명령",
            kind="command",
            value="bash scripts/setup_audio.sh",
            description="실행 전 현재 오디오 장치 설정을 확인하세요.",
        ),
    )


def _blackhole_actions() -> tuple[ReadinessAction, ...]:
    """BlackHole 설치 안내 액션을 반환한다."""
    return _audio_device_check_actions() + (
        ReadinessAction(
            id="install_blackhole",
            label="BlackHole 설치 명령",
            kind="command",
            value="brew install blackhole-2ch",
            description="설치 후 로그아웃/재로그인이 필요할 수 있습니다.",
        ),
    )


def _stt_settings_action() -> tuple[ReadinessAction, ...]:
    """STT 모델 설정 화면 이동 액션을 반환한다."""
    return (
        ReadinessAction(
            id="open_stt_settings",
            label="설정에서 모델 관리",
            kind="route",
            value="/app/settings",
            description="음성 인식 모델 카드에서 다운로드 또는 수동 가져오기를 진행하세요.",
        ),
    )


def _python_runtime_actions() -> tuple[ReadinessAction, ...]:
    """Python 런타임 확인/venv 준비 안내 액션을 반환한다."""
    return (
        ReadinessAction(
            id="check_system_python",
            label="Python 버전 확인 예시",
            kind="command",
            value="python3 --version",
            description="Python 3.11 이상, 3.13 미만을 권장합니다.",
        ),
        ReadinessAction(
            id="prepare_project_venv",
            label="프로젝트 venv 준비 예시",
            kind="command",
            value=(
                "cd <프로젝트_디렉토리>\n"
                "python3 -m venv .venv\n"
                "source .venv/bin/activate\n"
                'python -m pip install -e ".[dev]"'
            ),
            description="프로젝트 루트에서 직접 실행하세요. 네트워크 오류가 나면 SSL 우회 없이 중단하세요.",
        ),
        ReadinessAction(
            id="prepare_managed_venv",
            label="관리형 venv 준비 예시",
            kind="command",
            value=(
                "cd <프로젝트_디렉토리>\n"
                "python3 -m venv <관리형_venv>\n"
                '<관리형_venv>/bin/python -m pip install -e ".[dev]"'
            ),
            description="경량 런처의 관리형 Python 후보를 사용하려면 경로를 먼저 확인하세요.",
        ),
    )


def check_base_dir(config: AppConfig) -> ReadinessCheck:
    """데이터 디렉토리 존재, 쓰기 가능 여부, 소유자 전용 권한을 확인한다."""
    base_dir = config.paths.resolved_base_dir
    expected_mode = config.security.data_dir_permissions & 0o777
    details: dict[str, Any] = {
        "path": str(base_dir),
        "expected_mode": oct(expected_mode),
    }

    if not base_dir.exists():
        return ReadinessCheck(
            id="base_dir",
            status="fail",
            ready=False,
            message=f"데이터 디렉토리가 아직 없습니다: {base_dir}",
            action_hint="앱을 다시 시작하거나 디렉토리 권한을 확인하세요.",
            details=details | {"exists": False},
            actions=_base_dir_create_action(expected_mode),
        )

    if not base_dir.is_dir():
        return ReadinessCheck(
            id="base_dir",
            status="fail",
            ready=False,
            message=f"데이터 경로가 디렉토리가 아닙니다: {base_dir}",
            action_hint="해당 경로를 비우고 디렉토리로 다시 생성하세요.",
            details=details | {"exists": True, "is_dir": False},
        )

    writable = os.access(base_dir, os.W_OK)
    current_mode = stat.S_IMODE(base_dir.stat().st_mode)
    details = details | {
        "exists": True,
        "is_dir": True,
        "writable": writable,
        "actual_mode": oct(current_mode),
        "permissions_ok": current_mode == expected_mode,
    }

    if not writable:
        return ReadinessCheck(
            id="base_dir",
            status="fail",
            ready=False,
            message=f"데이터 디렉토리에 쓸 수 없습니다: {base_dir}",
            action_hint=f"소유권과 권한을 확인한 뒤 chmod {oct(expected_mode)} <경로>를 적용하세요.",
            details=details,
            actions=_base_dir_permission_action(expected_mode),
        )

    if current_mode != expected_mode:
        return ReadinessCheck(
            id="base_dir",
            status="warn",
            ready=True,
            message=(
                f"데이터 디렉토리 권한이 권장값과 다릅니다 "
                f"({oct(current_mode)} != {oct(expected_mode)})."
            ),
            action_hint=f"민감한 회의 데이터를 보호하려면 chmod {oct(expected_mode)} <경로>를 적용하세요.",
            details=details,
            actions=_base_dir_permission_action(expected_mode),
        )

    return ReadinessCheck(
        id="base_dir",
        status="pass",
        ready=True,
        message="데이터 디렉토리가 준비되었습니다.",
        details=details,
    )


def check_ffmpeg() -> ReadinessCheck:
    """ffmpeg 실행 파일 존재 여부를 확인한다."""
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        return ReadinessCheck(
            id="ffmpeg",
            status="fail",
            ready=False,
            message="ffmpeg이 설치되지 않았습니다.",
            action_hint="Homebrew에서 brew install ffmpeg을 실행한 뒤 앱을 다시 확인하세요.",
            actions=_ffmpeg_actions(),
        )

    return ReadinessCheck(
        id="ffmpeg",
        status="pass",
        ready=True,
        message="ffmpeg을 사용할 수 있습니다.",
        details={"installed": True, "path": ffmpeg_path},
    )


def check_python_runtime() -> ReadinessCheck:
    """서버 기준 런처 Python 후보를 read-only로 진단한다."""
    reconstructed_spec = build_launcher_spec()
    reconstructed_runtime = reconstructed_spec.to_dict().get("runtime", {})
    handoff = _launcher_runtime_handoff()
    if handoff is None:
        selected_path = reconstructed_spec.python_executable
        python_source = reconstructed_spec.python_source
        runtime_scope = "server_reconstructed"
        candidates = reconstructed_runtime.get("candidates", [])
        extra_details: dict[str, Any] = {}
        origin_label = "서버 기준 런처"
    else:
        python_source, selected_path, handoff_details = handoff
        runtime_scope = "launcher_handoff"
        candidates = [_runtime_candidate_details(python_source, selected_path)]
        extra_details = handoff_details | {
            "reconstructed_candidates": reconstructed_runtime.get("candidates", [])
        }
        origin_label = "런처가 전달한"

    selected_is_file = selected_path.is_file()
    selected_is_executable = bool(selected_is_file and os.access(selected_path, os.X_OK))
    running_python = _safe_resolved_path(sys.executable)
    selected_python = _safe_resolved_path(selected_path)
    details = {
        "advisory": True,
        "runtime_scope": runtime_scope,
        "python_source": python_source,
        "python_executable": str(selected_path),
        "running_python": str(running_python),
        "selected_matches_running_python": selected_python == running_python,
        "candidates": candidates,
    } | extra_details

    if not selected_is_file:
        return ReadinessCheck(
            id="python_runtime",
            status="fail",
            ready=False,
            message=f"{origin_label} Python 후보가 파일이 아닙니다: {selected_path}",
            action_hint="런처 metadata, 프로젝트 .venv, 또는 관리형 venv 경로를 확인하세요.",
            details=details | {"selected_is_file": False, "selected_is_executable": False},
            actions=_python_runtime_actions(),
        )

    if not selected_is_executable:
        return ReadinessCheck(
            id="python_runtime",
            status="fail",
            ready=False,
            message=f"{origin_label} Python 후보에 실행 권한이 없습니다: {selected_path}",
            action_hint="선택된 Python 후보의 실행 권한 또는 venv 경로를 확인하세요.",
            details=details | {"selected_is_file": True, "selected_is_executable": False},
            actions=_python_runtime_actions(),
        )

    if python_source == "current_interpreter":
        return ReadinessCheck(
            id="python_runtime",
            status="warn",
            ready=True,
            message="서버가 현재 인터프리터 기준으로 실행 중입니다.",
            action_hint="재실행/배포 경로에서는 프로젝트 .venv 또는 관리형 venv 사용을 권장합니다.",
            details=details | {"selected_is_file": True, "selected_is_executable": True},
            actions=_python_runtime_actions(),
        )

    return ReadinessCheck(
        id="python_runtime",
        status="pass",
        ready=True,
        message=f"{origin_label} Python 후보가 준비되었습니다: {python_source}",
        details=details | {"selected_is_file": True, "selected_is_executable": True},
    )


def check_hf_token_configured(config: AppConfig) -> ReadinessCheck:
    """HuggingFace 토큰 설정 여부를 값 노출 없이 확인한다."""
    env_present = [name for name in _HF_TOKEN_ENV_NAMES if bool(os.environ.get(name))]
    config_token_configured = bool(getattr(config.diarization, "huggingface_token", None))
    configured = bool(env_present or config_token_configured)
    details = {
        "configured": configured,
        "environment_variables": list(_HF_TOKEN_ENV_NAMES),
        "environment_variables_present": env_present,
        "config_token_configured": config_token_configured,
    }

    if configured:
        return ReadinessCheck(
            id="hf_token_env",
            status="pass",
            ready=True,
            message="화자분리용 HuggingFace 토큰이 설정되어 있습니다.",
            details=details,
        )

    return ReadinessCheck(
        id="hf_token_env",
        status="fail",
        ready=False,
        message="화자분리용 HuggingFace 토큰이 설정되지 않았습니다.",
        action_hint=(
            "pyannote 모델 약관에 동의한 뒤 HUGGINGFACE_TOKEN 또는 HF_TOKEN 환경변수를 설정하세요."
        ),
        details=details,
        actions=_hf_token_actions(),
    )


def check_audio_devices(config: AppConfig) -> ReadinessCheck:
    """BlackHole/Aggregate 장치 상태를 read-only로 확인한다."""
    recording = config.recording
    system_audio_required = bool(
        recording.enabled and (recording.prefer_system_audio or recording.multi_track)
    )
    ffmpeg_path = shutil.which("ffmpeg")
    base_details: dict[str, Any] = {
        "recording_enabled": recording.enabled,
        "prefer_system_audio": recording.prefer_system_audio,
        "multi_track": recording.multi_track,
        "selected_mode": _selected_audio_mode(config),
        "has_blackhole": False,
        "has_aggregate": False,
        "probe_status": "not_run",
    }

    if sys.platform != "darwin":
        return ReadinessCheck(
            id="audio_devices",
            status="unknown",
            ready=not system_audio_required,
            message="시스템 오디오 캡처 점검은 macOS에서만 지원됩니다.",
            action_hint="macOS 환경에서 다시 확인하세요.",
            details=base_details | {"platform": sys.platform, "probe_status": "unsupported"},
        )

    if ffmpeg_path is None:
        return ReadinessCheck(
            id="audio_devices",
            status="fail" if system_audio_required else "warn",
            ready=not system_audio_required,
            message="ffmpeg이 없어 오디오 장치 목록을 확인할 수 없습니다.",
            action_hint="먼저 ffmpeg을 설치하세요.",
            details=base_details | {"probe_status": "ffmpeg_missing"},
            actions=_ffmpeg_actions(),
        )

    device_output, error = _load_avfoundation_devices(ffmpeg_path)
    if error:
        return ReadinessCheck(
            id="audio_devices",
            status="unknown",
            ready=not system_audio_required,
            message=f"오디오 장치 목록을 확인할 수 없습니다: {error}",
            action_hint="macOS 오디오 권한과 ffmpeg 설치 상태를 확인하세요.",
            details=base_details | {"probe_status": "error", "error": error},
            actions=_audio_device_check_actions(),
        )

    lower_output = device_output.lower()
    aggregate_names = get_aggregate_device_names(timeout_seconds=_AUDIO_DEVICE_TIMEOUT_SECONDS)
    has_blackhole = "blackhole" in lower_output
    has_aggregate = ("aggregate" in lower_output) or any(
        name.lower() in lower_output for name in aggregate_names
    )
    full_capture_ready = bool(
        not recording.prefer_system_audio or recording.multi_track or has_aggregate
    )
    ready = bool(not system_audio_required or (has_blackhole and full_capture_ready))
    status: ReadinessStatus
    if ready:
        status = "pass"
    elif has_blackhole:
        status = "warn"
    else:
        status = "fail"

    if ready and has_aggregate:
        message = "BlackHole 2ch와 Aggregate Device가 감지되었습니다."
        action_hint = ""
    elif ready:
        message = "시스템 오디오 캡처에 필요한 장치 상태가 충분합니다."
        action_hint = ""
    elif has_blackhole:
        message = "BlackHole은 있지만 Aggregate Device가 감지되지 않았습니다."
        action_hint = (
            "본인 마이크와 시스템 오디오를 함께 녹음하려면 scripts/setup_audio.sh를 실행하세요."
        )
        actions = _aggregate_device_actions()
    else:
        message = "BlackHole 2ch 장치가 감지되지 않았습니다."
        action_hint = "시스템 오디오 녹음이 필요하면 brew install blackhole-2ch 후 재로그인하세요."
        actions = _blackhole_actions()

    if ready:
        actions = ()

    return ReadinessCheck(
        id="audio_devices",
        status=status,
        ready=ready,
        message=message,
        action_hint=action_hint,
        details=base_details
        | {
            "has_blackhole": has_blackhole,
            "has_aggregate": has_aggregate,
            "coreaudio_aggregate_names": sorted(aggregate_names),
            "aggregate_device_name": _AGGREGATE_DEVICE_NAME,
            "probe_status": "pass",
        },
        actions=actions,
    )


def check_stt_model(config: AppConfig) -> ReadinessCheck:
    """활성 STT 모델이 로컬에서 사용 가능한지 확인한다."""
    active_model = config.stt.model_name
    base_dir = config.paths.resolved_base_dir
    active_spec = None
    for spec in STT_MODELS:
        candidates = _registered_model_path_candidates(spec, base_dir)
        if active_model in candidates:
            active_spec = spec
            break

    if active_spec is None:
        local_path = _local_model_path(active_model)
        if local_path is not None:
            local_ready = _is_local_stt_model_ready(local_path)
            return ReadinessCheck(
                id="stt_model",
                status="pass" if local_ready else "warn",
                ready=local_ready,
                message=(
                    f"활성 STT 로컬 모델이 준비되었습니다: {local_path}"
                    if local_ready
                    else f"활성 STT 로컬 모델 파일이 부족합니다: {local_path}"
                ),
                action_hint=(
                    ""
                    if local_ready
                    else "config.json과 safetensors 가중치가 있는 모델 디렉토리를 지정하세요."
                ),
                details={
                    "active_model_path": active_model,
                    "registered": False,
                    "local_path": str(local_path),
                },
                actions=() if local_ready else _stt_settings_action(),
            )

        return ReadinessCheck(
            id="stt_model",
            status="warn",
            ready=False,
            message="활성 STT 모델이 기본 레지스트리 외부 경로입니다.",
            action_hint="설정 화면에서 지원 STT 모델을 다운로드하고 활성화하세요.",
            details={"active_model_path": active_model, "registered": False},
            actions=_stt_settings_action(),
        )

    model_status = get_model_status(active_spec, base_dir=base_dir)
    ready = model_status == ModelStatus.READY
    return ReadinessCheck(
        id="stt_model",
        status="pass" if ready else "warn",
        ready=ready,
        message=(
            f"활성 STT 모델이 준비되었습니다: {active_spec.id}"
            if ready
            else f"활성 STT 모델이 아직 로컬에 없습니다: {active_spec.id}"
        ),
        action_hint=""
        if ready
        else "설정 화면에서 STT 모델을 다운로드하거나 수동 가져오기를 진행하세요.",
        details={
            "active_model_id": active_spec.id,
            "active_model_path": active_model,
            "model_status": model_status.value,
            "registered": True,
        },
        actions=() if ready else _stt_settings_action(),
    )


def _registered_model_path_candidates(spec: Any, base_dir: Any) -> set[str]:
    """등록 모델이 활성 경로로 가질 수 있는 후보 문자열을 반환한다."""
    candidates = {spec.model_path}
    try:
        candidates.add(str(Path(spec.model_path).expanduser()))
    except (OSError, RuntimeError, ValueError):
        pass

    try:
        effective = get_effective_model_path(spec, base_dir=base_dir)
    except Exception:  # noqa: BLE001
        effective = spec.model_path
    candidates.add(effective)
    local_effective = _local_model_path(effective)
    if local_effective is not None:
        candidates.add(str(local_effective))
    return candidates


def _local_model_path(model_path: str) -> Path | None:
    """로컬 경로 문자열이면 확장된 Path를 반환한다."""
    if model_path.startswith(("~", "/", "./", "../")):
        return Path(model_path).expanduser()
    return None


def _launcher_runtime_handoff() -> tuple[str, Path, dict[str, Any]] | None:
    """런처가 서버에 전달한 Python 런타임 진단값을 검증해 반환한다."""
    python_source = os.environ.get(LAUNCHER_PYTHON_SOURCE_ENV, "").strip()
    python_executable = os.environ.get(LAUNCHER_PYTHON_EXECUTABLE_ENV, "").strip()
    if python_source not in _VALID_LAUNCHER_PYTHON_SOURCES or not python_executable:
        return None

    try:
        selected_path = _safe_resolved_path(python_executable)
    except (OSError, RuntimeError, ValueError):
        return None

    details: dict[str, Any] = {
        "handoff_python_source": python_source,
        "handoff_python_executable": str(selected_path),
    }
    handoff_project_dir = os.environ.get(LAUNCHER_PROJECT_DIR_ENV, "").strip()
    if handoff_project_dir:
        try:
            details["handoff_project_dir"] = str(_safe_resolved_path(handoff_project_dir))
        except (OSError, RuntimeError, ValueError):
            details["handoff_project_dir_valid"] = False
    return python_source, selected_path, details


def _runtime_candidate_details(python_source: str, path: Path) -> dict[str, Any]:
    """Python 후보 1개의 표시용 상태를 반환한다."""
    is_file = path.is_file()
    return {
        "id": python_source,
        "path": str(path),
        "exists": path.exists(),
        "is_file": is_file,
        "is_executable": bool(is_file and os.access(path, os.X_OK)),
        "selected": True,
        "origin": "launcher_handoff",
    }


def _safe_resolved_path(path: str | Path) -> Path:
    """비교용 경로를 best-effort로 정규화한다."""
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return Path(path)


def _is_local_stt_model_ready(path: Path) -> bool:
    """로컬 STT 모델 디렉토리의 최소 파일 구성을 확인한다."""
    if not path.is_dir():
        return False
    if not (path / "config.json").is_file():
        return False
    try:
        return any(path.glob("*.safetensors"))
    except OSError:
        return False


def _selected_audio_mode(config: AppConfig) -> str:
    """현재 녹음 설정이 선호하는 오디오 모드를 반환한다."""
    recording = config.recording
    if not recording.enabled:
        return "disabled"
    if recording.multi_track:
        return "multi_track"
    if recording.prefer_system_audio:
        return "aggregate_preferred"
    return "microphone"


def _load_avfoundation_devices(ffmpeg_path: str) -> tuple[str, str]:
    """ffmpeg AVFoundation 장치 목록을 문자열로 반환한다."""
    try:
        completed = subprocess.run(
            [ffmpeg_path, "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True,
            text=True,
            timeout=_AUDIO_DEVICE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            "",
            f"ffmpeg 장치 조회가 {_AUDIO_DEVICE_TIMEOUT_SECONDS:.0f}초 내 끝나지 않았습니다.",
        )
    except OSError as exc:
        return "", str(exc)

    return f"{completed.stdout}\n{completed.stderr}", ""
