"""STT 모델 상태 확인 모듈

목적: 디스크 또는 HuggingFace 캐시를 기반으로 STT 모델의 다운로드 여부를
확인하고, 실제 디스크 사용량을 계산한다.

주요 기능:
    - ModelStatus: 모델 런타임 상태 Enum
    - get_model_status(spec): spec 기반으로 현재 상태 판정
    - get_actual_size_mb(path): 실제 디스크 사용량 (MB)

의존성: 표준 라이브러리만 사용 (enum, pathlib).
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from pathlib import Path

from .stt_model_registry import STTModelSpec, get_manual_import_dir

logger = logging.getLogger(__name__)


class ModelStatus(StrEnum):
    """STT 모델의 런타임 상태.

    str 혼합 Enum이므로 JSON 직렬화 시 문자열 값이 사용된다.
    """

    NOT_DOWNLOADED = "not_downloaded"
    DOWNLOADING = "downloading"
    READY = "ready"
    ERROR = "error"


def _is_hf_repo_id(model_path: str) -> bool:
    """'/' 는 포함하되 로컬 경로 형태가 아니면 HF repo ID로 간주한다."""
    if "/" not in model_path:
        return False
    # 로컬 경로(절대 경로 또는 ~ 로 시작)는 제외
    return not model_path.startswith(("/", "~", "./", "../"))


def _get_hf_hub_cache_root() -> Path:
    """HuggingFace Hub 캐시 루트 디렉토리를 반환한다.

    huggingface_hub 의 표준 환경변수 우선순위를 따른다. 테스트와 운영 환경에서
    임의 HOME 패치 없이 캐시 위치를 명시할 수 있도록 별도 헬퍼로 분리한다.
    """
    hf_hub_cache = os.environ.get("HF_HUB_CACHE")
    if hf_hub_cache:
        return Path(hf_hub_cache).expanduser()

    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home).expanduser() / "hub"

    return Path.home() / ".cache" / "huggingface" / "hub"


def _get_hf_repo_cache_path(repo_id: str) -> Path:
    """HF repo ID에 대응하는 로컬 캐시 디렉토리를 반환한다."""
    cache_name = "models--" + repo_id.replace("/", "--")
    return _get_hf_hub_cache_root() / cache_name


def _get_manual_import_path(spec: STTModelSpec, base_dir: str | Path | None = None) -> Path:
    """수동 임포트 경로를 반환한다.

    테스트에서 기존 1-인자 함수로 monkeypatch 한 경우도 받아들여 하위 호환을
    유지한다.
    """
    try:
        return Path(get_manual_import_dir(spec, base_dir=str(base_dir) if base_dir else None))
    except TypeError:
        return Path(get_manual_import_dir(spec))


def _is_valid_snapshot_dir(path: Path) -> bool:
    """mlx-whisper 로딩에 필요한 최소 파일이 있는 snapshot인지 확인한다."""
    if not path.is_dir():
        return False
    if not (path / "config.json").is_file():
        return False
    try:
        return any(path.glob("*.safetensors"))
    except OSError as exc:
        logger.warning("HF snapshot 스캔 실패 (%s): %s", path, exc)
        return False


def _safe_snapshot_candidate(snapshots_dir: Path, ref_value: str) -> Path | None:
    """refs/main 값으로부터 snapshots 하위 후보 경로를 안전하게 만든다."""
    revision = ref_value.strip()
    if not revision:
        return None

    try:
        snapshots_root = snapshots_dir.resolve()
        candidate = (snapshots_dir / revision).resolve()
        candidate.relative_to(snapshots_root)
    except (OSError, ValueError):
        logger.warning("유효하지 않은 HF snapshot ref 무시: %s", ref_value)
        return None

    return candidate


def resolve_hf_cached_snapshot(repo_id: str) -> str | None:
    """HF repo ID를 로컬 cached snapshot 절대경로로 해석한다.

    `HF_HUB_OFFLINE=1` 환경에서 `snapshot_download()` 또는 mlx-whisper 내부
    경로 해석이 refs/main 개행/누락 문제로 실패해도, 이미 캐시에 완전한 모델이
    있으면 해당 snapshot 경로를 직접 반환한다.

    확인 순서:
        1. refs/main 의 revision을 strip() 후 snapshots/{revision} 검증
        2. refs/main 이 없거나 손상되었으면 유효한 최신 snapshot으로 fallback
        3. 캐시가 없거나 불완전하면 None

    Args:
        repo_id: HuggingFace repo ID (예: owner/model)

    Returns:
        유효한 snapshot 절대경로 문자열 또는 None.
    """
    if not _is_hf_repo_id(repo_id):
        return None

    cache_path = _get_hf_repo_cache_path(repo_id)
    snapshots_dir = cache_path / "snapshots"
    if not snapshots_dir.is_dir():
        return None

    refs_main = cache_path / "refs" / "main"
    if refs_main.is_file():
        try:
            candidate = _safe_snapshot_candidate(snapshots_dir, refs_main.read_text().strip())
        except OSError as exc:
            logger.warning("HF refs/main 읽기 실패 (%s): %s", refs_main, exc)
            candidate = None
        if candidate is not None and _is_valid_snapshot_dir(candidate):
            resolved = str(candidate)
            logger.debug("HF cached snapshot 해석: %s -> %s", repo_id, resolved)
            return resolved

    try:
        candidates = [path for path in snapshots_dir.iterdir() if _is_valid_snapshot_dir(path)]
    except OSError as exc:
        logger.warning("HF snapshots 디렉토리 스캔 실패 (%s): %s", snapshots_dir, exc)
        return None

    if not candidates:
        return None

    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    resolved = str(latest.resolve())
    logger.debug("HF cached snapshot fallback 해석: %s -> %s", repo_id, resolved)
    return resolved


def _check_hf_cache(repo_id: str) -> bool:
    """HF 캐시에 해당 repo의 완전한 snapshot이 존재하는지 확인한다."""
    return resolve_hf_cached_snapshot(repo_id) is not None


def get_effective_hf_model_path(model_path: str) -> str:
    """HF repo ID이면 로컬 cached snapshot을 우선 반환한다.

    캐시가 없거나 불완전하면 원래 값을 그대로 반환하여 기존 온라인 다운로드
    동작을 유지한다.
    """
    cached = resolve_hf_cached_snapshot(model_path)
    return cached if cached is not None else model_path


def _check_manual_import(spec: STTModelSpec, base_dir: str | Path | None = None) -> bool:
    """수동 임포트 디렉토리에 유효한 모델이 존재하는지 확인한다.

    사용자가 HF CDN 대신 브라우저로 직접 다운로드한 파일을
    `~/.meeting-transcriber/stt_models/{id}-manual/` 에 복사해 두었을 때
    해당 경로를 READY 로 판정한다.

    Returns:
        True: weights.safetensors + config.json 모두 존재
        False: 디렉토리 없음 또는 필수 파일 누락
    """
    manual_dir = _get_manual_import_path(spec, base_dir=base_dir)
    if not manual_dir.exists():
        return False
    weights = manual_dir / "weights.safetensors"
    config = manual_dir / "config.json"
    return weights.exists() and config.exists()


def get_effective_model_path(spec: STTModelSpec, base_dir: str | Path | None = None) -> str:
    """활성화·전사 시 실제로 사용할 모델 경로를 반환한다.

    우선순위:
        1. 수동 임포트 디렉토리가 유효하면 그 경로
        2. HF repo ID가 로컬 캐시에 있으면 cached snapshot 절대경로
        3. 아니면 spec.model_path (HF repo ID 또는 로컬 양자화 경로)

    mlx-whisper의 `path_or_hf_repo=` 인자는 로컬 경로와 HF repo ID를
    모두 받아들이므로 둘 다 이 한 값을 사용한다.

    Args:
        spec: STTModelSpec 메타데이터.

    Returns:
        모델 로드에 사용할 경로 문자열.
    """
    if _check_manual_import(spec, base_dir=base_dir):
        return str(_get_manual_import_path(spec, base_dir=base_dir))
    return get_effective_hf_model_path(spec.model_path)


def get_model_status(spec: STTModelSpec, base_dir: str | Path | None = None) -> ModelStatus:
    """모델의 다운로드 상태를 확인한다.

    확인 순서:
        1. 수동 임포트 디렉토리 (`{id}-manual/`) — 브라우저로 직접 받은 경우
        2. HF repo ID 이면 완전한 HF cached snapshot 확인
        3. (하위 호환) 로컬 경로 이면 weights.safetensors + config.json 확인

    모든 지원 모델은 HF repo ID 를 사용하지만, 하위 호환과 방어적 프로그래밍을
    위해 로컬 경로 체크도 유지한다.

    Args:
        spec: STTModelSpec 메타데이터.

    Returns:
        ModelStatus.READY: 사용 가능 상태
        ModelStatus.NOT_DOWNLOADED: 그 외 (손상된 상태 포함)

    Note:
        DOWNLOADING/ERROR 상태는 본 함수가 판정하지 않는다.
        다운로더(STTModelDownloader)의 in-memory 상태와 결합해 상위 계층에서 계산한다.
    """
    # 1. 수동 임포트 우선 (사용자가 브라우저로 직접 받아 배치한 경우)
    if _check_manual_import(spec, base_dir=base_dir):
        return ModelStatus.READY

    # 2. HF repo ID 형태면 HF 캐시 확인
    if _is_hf_repo_id(spec.model_path):
        if _check_hf_cache(spec.model_path):
            return ModelStatus.READY
        return ModelStatus.NOT_DOWNLOADED

    # 3. (하위 호환) 로컬 경로 — 현재 레지스트리의 모든 모델은 이 경로를 타지 않는다.
    path = Path(spec.model_path).expanduser()
    if not path.exists():
        return ModelStatus.NOT_DOWNLOADED

    weights = path / "weights.safetensors"
    config = path / "config.json"
    if not weights.exists() or not config.exists():
        logger.debug(
            "모델 무결성 검사 실패 (%s): weights=%s, config=%s",
            spec.id,
            weights.exists(),
            config.exists(),
        )
        return ModelStatus.NOT_DOWNLOADED

    return ModelStatus.READY


def get_actual_size_mb(model_path: str) -> float:
    """모델 경로의 실제 디스크 사용량을 MB 단위로 반환한다.

    Args:
        model_path: 파일 또는 디렉토리 경로 (tilde 확장 지원).

    Returns:
        MB 단위 크기. 경로가 없으면 0.0.
    """
    # HF repo ID 형식(owner/name)은 로컬 경로가 아니라 HuggingFace 캐시를 조회한다.
    if _is_hf_repo_id(model_path):
        cache_dir = _get_hf_repo_cache_path(model_path)
        if not cache_dir.exists():
            return 0.0
        try:
            total = sum(
                f.stat().st_size
                for f in cache_dir.rglob("*")
                if f.is_file() and not f.is_symlink()
            )
        except OSError as exc:
            logger.warning("HF 캐시 크기 계산 실패 (%s): %s", cache_dir, exc)
            return 0.0
        return round(total / (1024**2), 1)

    path = Path(model_path).expanduser()
    if not path.exists():
        return 0.0
    if path.is_file():
        return round(path.stat().st_size / (1024**2), 1)
    try:
        # 심볼릭 링크는 중복 계산을 피하려 제외
        total = sum(
            f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink()
        )
    except OSError as exc:
        logger.warning("디스크 크기 계산 실패 (%s): %s", path, exc)
        return 0.0
    return round(total / (1024**2), 1)
