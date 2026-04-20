"""
칩셋 자동 감지 모듈 (Chipset Auto-Detection Module)

목적: macOS Apple Silicon 칩셋과 RAM을 감지하여 최적 설정 프로파일을 반환한다.
주요 기능:
    - platform.machine()으로 arm64 (Apple Silicon) 감지
    - sysctl로 구체적 칩 이름(M1/M2/M3/M4) 파싱
    - psutil로 시스템 RAM 크기 감지
    - 칩셋+RAM 조합에 따른 최적 batch_size 프로파일 반환
의존성: platform, subprocess, psutil
"""

from __future__ import annotations

import logging
import platform
import re
import subprocess
from dataclasses import dataclass

import psutil

logger = logging.getLogger(__name__)


@dataclass
class ChipsetInfo:
    """감지된 칩셋 정보."""

    is_apple_silicon: bool
    chip_name: str | None  # "M1", "M2", "M3", "M4" 등
    ram_gb: int


@dataclass
class OptimalProfile:
    """칩셋+RAM 기반 최적 설정 프로파일."""

    batch_size: int


# 세대별 RAM 임계값 기반 batch_size 매핑
_GENERATION_BATCH_MAP: dict[str, dict[int, int]] = {
    "M1": {8: 8, 16: 12},
    "M2": {8: 8, 16: 12},
    "M3": {8: 8, 16: 16, 24: 16},
    "M4": {8: 8, 16: 16, 24: 16, 32: 24},
}
_DEFAULT_BATCH_SIZE = 16


class ChipsetDetector:
    """macOS Apple Silicon 칩셋 감지기.

    platform.machine() + sysctl + psutil을 사용하여
    시스템의 칩셋과 RAM을 감지하고 최적 설정을 반환한다.
    """

    def detect(self) -> ChipsetInfo:
        """시스템 칩셋과 RAM 정보를 감지한다.

        Returns:
            ChipsetInfo: 감지된 칩셋 정보
        """
        arch = platform.machine()
        is_apple_silicon = arch == "arm64"
        chip_name = None
        ram_gb = self._detect_ram_gb()

        if is_apple_silicon:
            chip_name = self._detect_chip_name()

        info = ChipsetInfo(
            is_apple_silicon=is_apple_silicon,
            chip_name=chip_name,
            ram_gb=ram_gb,
        )
        logger.info(f"칩셋 감지 완료: {info}")
        return info

    def get_optimal_profile(self) -> OptimalProfile:
        """현재 시스템에 최적화된 설정 프로파일을 반환한다.

        Returns:
            OptimalProfile: 최적 batch_size 등 설정값
        """
        info = self.detect()
        batch_size = self._compute_batch_size(info)
        profile = OptimalProfile(batch_size=batch_size)
        logger.info(f"최적 프로파일: {profile}")
        return profile

    @staticmethod
    def _detect_chip_name() -> str | None:
        """sysctl로 Apple Silicon 칩 이름을 감지한다.

        Returns:
            "M1", "M2", "M3", "M4" 등 또는 None
        """
        try:
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                brand = result.stdout.strip()
                match = re.search(r"Apple (M\d+)", brand)
                if match:
                    return match.group(1)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning(f"sysctl 칩셋 감지 실패: {e}")
        return None

    @staticmethod
    def _detect_ram_gb() -> int:
        """psutil로 시스템 총 RAM(GB)을 감지한다.

        Returns:
            시스템 RAM 크기 (GB 단위, 반올림)
        """
        total_bytes = psutil.virtual_memory().total
        return round(total_bytes / (1024**3))

    @staticmethod
    def _compute_batch_size(info: ChipsetInfo) -> int:
        """칩셋+RAM 조합에 따른 최적 batch_size를 계산한다.

        Args:
            info: 감지된 칩셋 정보

        Returns:
            최적 batch_size 값
        """
        if not info.is_apple_silicon or info.chip_name is None:
            return _DEFAULT_BATCH_SIZE

        ram_thresholds = _GENERATION_BATCH_MAP.get(info.chip_name)
        if ram_thresholds is None:
            return _DEFAULT_BATCH_SIZE

        best_batch_size = _DEFAULT_BATCH_SIZE
        for ram_threshold, bs in sorted(ram_thresholds.items()):
            if info.ram_gb >= ram_threshold:
                best_batch_size = bs
        return best_batch_size
