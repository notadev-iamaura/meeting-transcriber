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
from enum import Enum
from pathlib import Path

from .stt_model_registry import STTModelSpec

logger = logging.getLogger(__name__)


class ModelStatus(str, Enum):
    """STT 모델의 런타임 상태.

    str 혼합 Enum이므로 JSON 직렬화 시 문자열 값이 사용된다.
    """

    NOT_DOWNLOADED = "not_downloaded"
    DOWNLOADING = "downloading"
    QUANTIZING = "quantizing"
    READY = "ready"
    ERROR = "error"


def _is_hf_repo_id(model_path: str) -> bool:
    """'/' 는 포함하되 로컬 경로 형태가 아니면 HF repo ID로 간주한다."""
    if "/" not in model_path:
        return False
    # 로컬 경로(절대 경로 또는 ~ 로 시작)는 제외
    if model_path.startswith(("/", "~", "./", "../")):
        return False
    return True


def _check_hf_cache(repo_id: str) -> bool:
    """HF 캐시 디렉토리에 해당 repo의 safetensors가 존재하는지 확인한다."""
    cache_root = Path.home() / ".cache" / "huggingface" / "hub"
    cache_name = "models--" + repo_id.replace("/", "--")
    cache_path = cache_root / cache_name
    if not cache_path.exists():
        return False
    # snapshots 하위에 safetensors 가중치 파일이 하나라도 있으면 준비됨으로 판단
    try:
        return any(cache_path.rglob("*.safetensors"))
    except OSError as exc:
        logger.warning("HF 캐시 스캔 실패 (%s): %s", cache_path, exc)
        return False


def get_model_status(spec: STTModelSpec) -> ModelStatus:
    """모델의 다운로드 상태를 확인한다.

    Args:
        spec: STTModelSpec 메타데이터.

    Returns:
        ModelStatus.READY: 사용 가능 상태 (로컬 또는 HF 캐시)
        ModelStatus.NOT_DOWNLOADED: 그 외 (손상된 상태 포함)

    Note:
        DOWNLOADING/QUANTIZING/ERROR 상태는 본 함수가 판정하지 않는다.
        다운로더(STTModelDownloader)의 in-memory 상태와 결합해 상위 계층에서 계산한다.
    """
    # HF repo ID 형태면 HF 캐시만 확인
    if _is_hf_repo_id(spec.model_path):
        if _check_hf_cache(spec.model_path):
            return ModelStatus.READY
        return ModelStatus.NOT_DOWNLOADED

    # 로컬 경로 기반 (예: ~/.meeting-transcriber/stt_models/...)
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
        cache_dir = Path.home() / ".cache" / "huggingface" / "hub" / (
            "models--" + model_path.replace("/", "--")
        )
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
        return round(total / (1024 ** 2), 1)

    path = Path(model_path).expanduser()
    if not path.exists():
        return 0.0
    if path.is_file():
        return round(path.stat().st_size / (1024 ** 2), 1)
    try:
        # 심볼릭 링크는 중복 계산을 피하려 제외
        total = sum(
            f.stat().st_size
            for f in path.rglob("*")
            if f.is_file() and not f.is_symlink()
        )
    except OSError as exc:
        logger.warning("디스크 크기 계산 실패 (%s): %s", path, exc)
        return 0.0
    return round(total / (1024 ** 2), 1)
