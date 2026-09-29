"""로컬 런타임 안전 점검 유틸리티.

HF 오프라인 모드, pyannote 캐시 상태, 자동처리용 보수 설정처럼
파이프라인 시작 전 확인해야 하는 환경 조합을 모은다.

캐시 완전성 판정은 huggingface_hub import 없이 파일시스템만 본다.
setup_readiness 등 패키지 import를 피해야 하는 경로에서도 안전하게 쓸 수 있다.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_TRUE_VALUES = {"1", "true", "yes", "on"}
_HF_OFFLINE_FLAGS = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
_PYANNOTE_SEGMENTATION_REPO = "pyannote/segmentation-3.0"
_WEIGHT_SUFFIXES = (".bin", ".safetensors", ".ckpt", ".pt", ".pth", ".npz")
_MODEL_PATH_RE = re.compile(
    r"(?:\$model/|['\"]|\s)([A-Za-z0-9_./-]+\.(?:bin|safetensors|ckpt|pt|pth|npz))"
)
# community-1 config uses bare component dirs: $model/segmentation|embedding|plda
_MODEL_DIR_RE = re.compile(r"\$model/([A-Za-z0-9_.-]+)(?![A-Za-z0-9_./-])")
_REPO_REF_RE = re.compile(r"(?:^|[\s'\"])((?:pyannote)/[A-Za-z0-9_.-]+)(?:[\s'\"]|$)")


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


def hf_hub_cache_root(environ: Mapping[str, str] | None = None) -> Path:
    """HuggingFace hub 캐시 루트 경로를 반환한다."""
    env = environ if environ is not None else os.environ
    explicit = env.get("HF_HUB_CACHE")
    if explicit:
        return Path(explicit).expanduser()
    hf_home = env.get("HF_HOME")
    if hf_home:
        return Path(hf_home).expanduser() / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def hf_repo_cache_dir(repo_id: str, environ: Mapping[str, str] | None = None) -> Path:
    """repo_id에 대응하는 HF hub 캐시 디렉터리 경로를 반환한다."""
    return hf_hub_cache_root(environ) / f"models--{repo_id.replace('/', '--')}"


def resolve_hf_snapshot_dir(
    repo_id: str,
    *,
    revision: str = "main",
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """refs/<revision>이 가리키는 snapshot 디렉터리를 반환한다.

    snapshot이 없거나 revision ref가 없으면 None.
    symlink로 캐시 루트 밖을 가리키면 fail-closed로 None.
    """
    repo_dir = hf_repo_cache_dir(repo_id, environ)
    ref_path = repo_dir / "refs" / revision
    if not ref_path.is_file():
        return None
    try:
        rev = ref_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not rev or "/" in rev or "\\" in rev or rev in {".", ".."}:
        return None
    snapshot = (repo_dir / "snapshots" / rev).resolve()
    hub_root = hf_hub_cache_root(environ).resolve()
    try:
        snapshot.relative_to(hub_root)
    except ValueError:
        return None
    if not snapshot.is_dir():
        return None
    return snapshot


def _is_safe_cached_file(path: Path, hub_root: Path) -> bool:
    """캐시 파일이 hub 루트 아래에서 실제 non-empty 파일인지 확인한다.

    중간 디렉터리 symlink로 hub 밖을 가리키는 경우도 resolve 후 거부한다.
    """
    try:
        resolved = path.resolve()
        resolved.relative_to(hub_root)
        return resolved.is_file() and resolved.stat().st_size > 0
    except (OSError, ValueError):
        return False


def _is_safe_cached_component_dir(path: Path, hub_root: Path) -> bool:
    """$model/<component> 디렉터리 안에 non-empty 가중치가 있는지 확인한다."""
    try:
        resolved = path.resolve()
        resolved.relative_to(hub_root)
        if not resolved.is_dir():
            return False
        for child in resolved.rglob("*"):
            if child.suffix.lower() not in _WEIGHT_SUFFIXES:
                continue
            if _is_safe_cached_file(child, hub_root):
                return True
        return False
    except (OSError, ValueError):
        return False


def _snapshot_weight_files(snapshot: Path, hub_root: Path) -> list[Path]:
    """snapshot 아래에서 유효한 가중치 파일 목록을 반환한다."""
    weights: list[Path] = []
    try:
        for path in snapshot.rglob("*"):
            if not path.is_file() and not path.is_symlink():
                continue
            if path.suffix.lower() not in _WEIGHT_SUFFIXES:
                continue
            if _is_safe_cached_file(path, hub_root):
                weights.append(path)
    except OSError:
        return []
    return weights


def _referenced_relative_files(config_text: str) -> list[str]:
    """config.yaml 텍스트에서 참조하는 상대 파일 경로를 추출한다."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _MODEL_PATH_RE.finditer(config_text):
        rel = match.group(1).removeprefix("./")
        if rel.startswith("http") or rel in seen:
            continue
        if ".." in Path(rel).parts:
            continue
        seen.add(rel)
        found.append(rel)
    return found


def _referenced_model_dirs(config_text: str) -> list[str]:
    """config.yaml의 $model/<component> 디렉터리 참조를 추출한다.

    파일 경로($model/foo/bar.bin)는 제외하고, community-1처럼
    segmentation/embedding/plda 디렉터리만 남긴다.
    """
    file_refs = set(_referenced_relative_files(config_text))
    found: list[str] = []
    seen: set[str] = set()
    for match in _MODEL_DIR_RE.finditer(config_text):
        rel = match.group(1)
        if rel in seen or ".." in Path(rel).parts:
            continue
        # 이미 파일 참조로 잡힌 경로/접두사는 건너뛴다.
        if any(fr == rel or fr.startswith(rel + "/") for fr in file_refs):
            continue
        # 확장자가 가중치면 파일로 취급(디렉터리 아님)
        if Path(rel).suffix.lower() in _WEIGHT_SUFFIXES:
            continue
        seen.add(rel)
        found.append(rel)
    return found


def _referenced_external_repos(config_text: str, self_repo: str) -> list[str]:
    """config.yaml에서 참조하는 외부 pyannote repo id 목록을 반환한다."""
    found: list[str] = []
    seen: set[str] = {self_repo}
    for match in _REPO_REF_RE.finditer(config_text):
        repo_id = match.group(1)
        if repo_id in seen:
            continue
        seen.add(repo_id)
        found.append(repo_id)
    return found


def missing_pyannote_repo_cache_files(
    repo_id: str,
    environ: Mapping[str, str] | None = None,
    *,
    _visited: set[str] | None = None,
) -> list[str]:
    """단일 HF repo snapshot이 불완전하면 누락 라벨 목록을 반환한다."""
    visited = _visited if _visited is not None else set()
    if repo_id in visited:
        return []
    visited.add(repo_id)

    hub_root = hf_hub_cache_root(environ).resolve()
    snapshot = resolve_hf_snapshot_dir(repo_id, environ=environ)
    if snapshot is None:
        return [f"{repo_id}:snapshot"]

    config_path = snapshot / "config.yaml"
    if not _is_safe_cached_file(config_path, hub_root):
        return [f"{repo_id}:config.yaml"]

    missing: list[str] = []
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except OSError:
        return [f"{repo_id}:config.yaml"]

    file_refs = _referenced_relative_files(config_text)
    dir_refs = _referenced_model_dirs(config_text)

    for rel in file_refs:
        candidate = snapshot / rel
        if not _is_safe_cached_file(candidate, hub_root):
            missing.append(f"{repo_id}:{rel}")

    for rel in dir_refs:
        candidate = snapshot / rel
        if not _is_safe_cached_component_dir(candidate, hub_root):
            missing.append(f"{repo_id}:{rel}")

    # config가 구성요소를 가리키지 않는 단순 snapshot만 catch-all 가중치 요구.
    # community-1처럼 $model/<dir> 참조가 있으면 디렉터리 검사로 충분하다.
    if not file_refs and not dir_refs:
        weights = _snapshot_weight_files(snapshot, hub_root)
        if not weights:
            missing.append(f"{repo_id}:weights")

    for external in _referenced_external_repos(config_text, repo_id):
        missing.extend(missing_pyannote_repo_cache_files(external, environ, _visited=visited))

    return missing


def pyannote_required_cache_files(model_name: str) -> list[tuple[str, str]]:
    """pyannote 오프라인 실행 전에 있어야 하는 최소 HF 캐시 파일 목록.

    하위 호환용. 완전성 판정은 ``missing_pyannote_cache_files`` /
    ``pyannote_cache_complete``를 사용한다.
    """
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
    # Prefer filesystem snapshot resolution (no optional import).
    snapshot = resolve_hf_snapshot_dir(repo_id)
    if snapshot is not None:
        hub_root = hf_hub_cache_root().resolve()
        return _is_safe_cached_file(snapshot / filename, hub_root)

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
    """pyannote 오프라인 실행에 필요한 캐시 파일 중 누락 목록을 반환한다.

    하위 호환을 위해 유지하며, 내부적으로 가중치까지 포함한 완전성 검사를 쓴다.
    """
    return missing_pyannote_cache_files(model_name)


def missing_pyannote_cache_files(
    model_name: str,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """pyannote 모델 캐시가 불완전하면 누락 라벨 목록을 반환한다.

    config.yaml뿐 아니라 가중치(및 config가 참조하는 파일/외부 repo)까지
    확인한다. 판단이 모호하면 누락으로 처리한다(fail-closed).
    """
    if not model_name or not model_name.strip():
        return ["pyannote:model_name"]

    missing = missing_pyannote_repo_cache_files(model_name, environ)

    # 3.x speaker-diarization 계열은 전통적으로 segmentation-3.0에 의존한다.
    # community-1처럼 단일 repo에 모두 담긴 경우 config 참조로 이미 잡히지만,
    # 3.1 계열은 외부 의존이 config에 안 드러날 수 있어 최소 검사를 유지한다.
    if model_name.startswith("pyannote/speaker-diarization-3"):
        seg_missing = missing_pyannote_repo_cache_files(_PYANNOTE_SEGMENTATION_REPO, environ)
        for item in seg_missing:
            if item not in missing:
                missing.append(item)

    return missing


def pyannote_cache_complete(
    model_name: str,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """로컬 HF 캐시에 pyannote 모델(가중치 포함)이 완전하면 True."""
    return not missing_pyannote_cache_files(model_name, environ)


def pyannote_offline_cache_issue(
    model_name: str,
    environ: Mapping[str, str] | None = None,
) -> RuntimeSafetyIssue | None:
    """HF offline 상태에서 pyannote 캐시가 불완전하면 차단 사유를 반환한다."""
    if not hf_offline_enabled(environ):
        return None

    missing = missing_pyannote_cache_files(model_name, environ)
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
