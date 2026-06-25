"""로컬 런타임 안전 점검 유틸리티.

HF 오프라인 모드, pyannote 캐시 상태, 자동처리용 보수 설정처럼
파이프라인 시작 전 확인해야 하는 환경 조합을 모은다.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_TRUE_VALUES = {"1", "true", "yes", "on"}
_HF_OFFLINE_FLAGS = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
_PYANNOTE_SEGMENTATION_REPO = "pyannote/segmentation-3.0"


@dataclass(frozen=True)
class RuntimeSafetyIssue:
    """런타임 안전 점검에서 발견한 차단 사유."""

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        """API 응답에 포함 가능한 dict로 변환한다."""
        return {"code": self.code, "error": self.message}


def env_flag_enabled(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """환경변수 플래그가 활성값인지 반환한다."""
    env = environ if environ is not None else os.environ
    value = env.get(name)
    return value is not None and value.strip().lower() in _TRUE_VALUES


def hf_offline_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """HuggingFace 오프라인 모드가 켜져 있는지 반환한다."""
    return any(env_flag_enabled(name, environ) for name in _HF_OFFLINE_FLAGS)


def pyannote_required_cache_files(model_name: str) -> list[tuple[str, str]]:
    """pyannote 오프라인 실행 전에 있어야 하는 최소 HF 캐시 파일 목록."""
    required = [(model_name, "config.yaml")]
    if model_name.startswith("pyannote/speaker-diarization"):
        required.append((_PYANNOTE_SEGMENTATION_REPO, "config.yaml"))
    return required


def _cached_hf_file_exists(repo_id: str, filename: str) -> bool | None:
    """HF 캐시에 파일이 있는지 확인한다.

    Returns:
        True: 캐시 파일 존재
        False: 캐시 파일 없음
        None: huggingface_hub 캐시 검사 API를 사용할 수 없음
    """
    try:
        from huggingface_hub import try_to_load_from_cache  # type: ignore[import-untyped]
    except Exception:
        return None

    try:
        cached = try_to_load_from_cache(repo_id, filename)
    except Exception:
        return False

    return isinstance(cached, str) and Path(cached).is_file()


def missing_pyannote_offline_cache_files(model_name: str) -> list[str]:
    """pyannote 오프라인 실행에 필요한 캐시 파일 중 누락 목록을 반환한다."""
    missing: list[str] = []
    for repo_id, filename in pyannote_required_cache_files(model_name):
        exists = _cached_hf_file_exists(repo_id, filename)
        if exists is True:
            continue
        if exists is None:
            missing.append(f"{repo_id}:{filename} (cache inspector unavailable)")
        else:
            missing.append(f"{repo_id}:{filename}")
    return missing


def pyannote_offline_cache_issue(
    model_name: str,
    environ: Mapping[str, str] | None = None,
) -> RuntimeSafetyIssue | None:
    """HF offline 상태에서 pyannote 캐시가 불완전하면 차단 사유를 반환한다."""
    if not hf_offline_enabled(environ):
        return None

    missing = missing_pyannote_offline_cache_files(model_name)
    if not missing:
        return None

    missing_text = ", ".join(missing)
    return RuntimeSafetyIssue(
        code="hf_offline_pyannote_cache_incomplete",
        message=(
            "HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE 모드가 켜져 있지만 "
            f"pyannote 오프라인 캐시가 불완전합니다: {missing_text}. "
            "오프라인 모드를 끄고 모델을 한 번 정상 다운로드하거나, "
            "캐시가 완전한 상태에서 다시 실행하세요."
        ),
    )


def auto_processing_safety_issues(
    config: object,
    *,
    action: str,
    environ: Mapping[str, str] | None = None,
) -> list[RuntimeSafetyIssue]:
    """자동 처리 실행 전 차단해야 할 위험 조합을 반환한다."""
    auto = getattr(config, "auto_processing", None)
    if auto is not None and not bool(getattr(auto, "safety_checks_enabled", True)):
        return []

    issues: list[RuntimeSafetyIssue] = []
    uses_transcribe_path = action in {"transcribe", "full"}

    if uses_transcribe_path:
        diar = getattr(config, "diarization", None)
        model_name = str(getattr(diar, "model_name", "")) if diar is not None else ""
        if bool(getattr(auto, "block_hf_offline_cache_miss", True)):
            issue = pyannote_offline_cache_issue(model_name, environ)
            if issue is not None:
                issues.append(issue)

        thermal = getattr(config, "thermal", None)
        batch_size = int(getattr(thermal, "batch_size", 2)) if thermal is not None else 2
        cooldown = int(getattr(thermal, "cooldown_seconds", 180)) if thermal is not None else 180
        max_batch = int(getattr(auto, "max_thermal_batch_size", 2))
        min_cooldown = int(getattr(auto, "min_thermal_cooldown_seconds", 180))
        if batch_size > max_batch or cooldown < min_cooldown:
            issues.append(
                RuntimeSafetyIssue(
                    code="auto_processing_aggressive_thermal",
                    message=(
                        "자동처리에서 전사 경로를 실행하기에는 thermal 설정이 공격적입니다: "
                        f"batch_size={batch_size}, cooldown_seconds={cooldown}. "
                        f"자동처리 안전 기준은 batch_size<={max_batch}, "
                        f"cooldown_seconds>={min_cooldown}입니다."
                    ),
                )
            )

    return issues
