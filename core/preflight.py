"""
사전 검증 모듈 (Preflight Check Module)

목적: MLX Metal 런타임 및 시스템 환경의 사전 검증을 수행한다.
주요 기능:
    - Apple Silicon(arm64) 여부 확인
    - Metal GPU 가용성 확인 (별도 프로세스에서 안전하게 검증)
    - 검증 결과 캐싱 (한 번만 실행)
    - Python 버전 호환성 확인
의존성: subprocess, platform (표준 라이브러리만 사용)
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreflightResult:
    """사전 검증 결과.

    Attributes:
        is_apple_silicon: Apple Silicon(arm64) CPU 여부
        metal_available: Metal GPU 가용 여부
        python_compatible: Python 버전 호환 여부 (3.11 <= ver < 3.13)
        mlx_importable: mlx 패키지 import 가능 여부
        warnings: 경고 메시지 목록
    """

    is_apple_silicon: bool
    metal_available: bool
    python_compatible: bool
    mlx_importable: bool
    warnings: tuple[str, ...]

    @property
    def can_use_mlx(self) -> bool:
        """MLX 사용이 안전한지 여부."""
        return self.is_apple_silicon and self.metal_available and self.mlx_importable

    @property
    def can_use_chromadb(self) -> bool:
        """ChromaDB 사용이 안전한지 여부."""
        return self.python_compatible


# 모듈 수준 캐시 (한 번만 실행)
_cached_result: PreflightResult | None = None


def _check_apple_silicon() -> bool:
    """Apple Silicon(arm64) CPU인지 확인한다."""
    machine = platform.machine().lower()
    is_arm = machine in ("arm64", "aarch64")
    if not is_arm:
        logger.warning(f"Apple Silicon이 아닙니다: {platform.machine()} — MLX 사용 불가")
    return is_arm


def _check_python_version() -> tuple[bool, list[str]]:
    """Python 버전이 호환 범위(3.11~3.12)인지 확인한다."""
    ver = sys.version_info
    warnings: list[str] = []

    if ver >= (3, 13):
        warnings.append(
            f"Python {ver.major}.{ver.minor}은 지원하지 않습니다. "
            "chromadb Rust 바인딩과 호환되지 않아 SIGSEGV가 발생할 수 있습니다. "
            "Python 3.11 또는 3.12를 사용하세요."
        )
        return False, warnings

    if ver < (3, 11):
        warnings.append(
            f"Python {ver.major}.{ver.minor}은 지원하지 않습니다. 3.11 이상이 필요합니다."
        )
        return False, warnings

    return True, warnings


def _check_metal_availability() -> bool:
    """Metal GPU가 사용 가능한지 별도 프로세스에서 안전하게 검증한다.

    MLX의 Metal 초기화가 실패하면 C++ abort()를 호출하여
    프로세스 전체가 종료되므로, 별도 서브프로세스에서 검증한다.

    CI 환경(GitHub Actions 등)에서는 macOS runner가 Apple Silicon 이라도
    가상화 계층 때문에 `mlx.core` 본 프로세스 import 시점에 SIGABRT 가
    발생하는 사례가 있다 (subprocess preflight 는 통과하지만 본 프로세스
    cleanup path 의 `import mlx.core` 가 abort). CI 에서는 mlx 백엔드를
    사용할 일이 없으므로 (단위 테스트는 모두 mock 으로 동작) 일찍 False
    반환하여 SIGABRT 를 원천 차단한다.

    Returns:
        Metal GPU 사용 가능 여부
    """
    if os.environ.get("CI", "").lower() in ("true", "1"):
        logger.info("CI 환경 감지 — Metal/MLX 검증 스킵 (mlx import abort 방지)")
        return False

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import mlx.core as mx; assert mx.metal.is_available(), 'Metal not available'",
            ],
            capture_output=True,
            timeout=15,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            logger.warning(f"Metal GPU 검증 실패 (subprocess): {stderr[:200]}")
            return False
        return True
    except FileNotFoundError:
        logger.warning("Python 실행파일을 찾을 수 없습니다")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("Metal GPU 검증 타임아웃 (15초)")
        return False
    except Exception as e:
        logger.warning(f"Metal GPU 검증 중 예외: {e}")
        return False


def _check_mlx_importable() -> bool:
    """mlx 패키지가 import 가능한지 확인한다.

    실제 import는 하지 않고 별도 프로세스에서 검증하여
    SIGABRT를 방지한다.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import mlx"],
            capture_output=True,
            timeout=10,
            text=True,
        )
        if result.returncode != 0:
            logger.info("mlx 패키지 미설치 — MLX 백엔드 비활성화")
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        logger.warning(f"mlx import 검증 실패: {e}")
        return False


def run_preflight(*, force: bool = False) -> PreflightResult:
    """사전 검증을 실행하고 결과를 반환한다.

    첫 호출 시 검증을 수행하고, 이후에는 캐시된 결과를 반환한다.
    force=True로 호출하면 캐시를 무시하고 재검증한다.

    Args:
        force: True면 캐시 무시하고 재검증

    Returns:
        PreflightResult 검증 결과
    """
    global _cached_result
    if _cached_result is not None and not force:
        return _cached_result

    logger.info("사전 검증(preflight) 시작...")

    all_warnings: list[str] = []

    # 1. Apple Silicon 확인
    is_apple_silicon = _check_apple_silicon()
    if not is_apple_silicon:
        all_warnings.append(
            "Apple Silicon(M1/M2/M3/M4)이 필요합니다. "
            "Intel Mac에서는 MLX 기반 STT가 동작하지 않습니다."
        )

    # 2. Python 버전 확인
    python_ok, py_warnings = _check_python_version()
    all_warnings.extend(py_warnings)

    # 3. MLX import 가능 여부 (Apple Silicon일 때만 의미 있음)
    mlx_importable = False
    metal_available = False

    if is_apple_silicon:
        mlx_importable = _check_mlx_importable()

        if mlx_importable:
            # 4. Metal GPU 가용성 (별도 프로세스)
            metal_available = _check_metal_availability()
            if not metal_available:
                all_warnings.append(
                    "Metal GPU를 사용할 수 없습니다. "
                    "SSH/headless 환경이거나 macOS가 오래된 버전일 수 있습니다."
                )
        else:
            all_warnings.append(
                "mlx 패키지가 설치되지 않았습니다. 'pip install mlx mlx-whisper'로 설치하세요."
            )

    result = PreflightResult(
        is_apple_silicon=is_apple_silicon,
        metal_available=metal_available,
        python_compatible=python_ok,
        mlx_importable=mlx_importable,
        warnings=tuple(all_warnings),
    )

    # 결과 로깅
    logger.info(
        f"사전 검증 완료: "
        f"Apple Silicon={result.is_apple_silicon}, "
        f"Metal={result.metal_available}, "
        f"Python={result.python_compatible}, "
        f"MLX={result.mlx_importable}"
    )
    for warning in result.warnings:
        logger.warning(f"⚠️  {warning}")

    _cached_result = result
    return result


def reset_preflight_cache() -> None:
    """캐시를 초기화한다. 테스트 용도로만 사용."""
    global _cached_result
    _cached_result = None
