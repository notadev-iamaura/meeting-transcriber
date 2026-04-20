"""
칩셋 자동 감지 모듈 테스트 (Chipset Auto-Detection Tests)

목적: core/chipset_detector.py의 ChipsetDetector 전체 기능 단위 테스트.
주요 테스트:
    - Apple Silicon / Intel 감지
    - 칩셋+RAM 조합별 최적 batch_size
    - sysctl 실패 시 폴백
    - RAM 크기 감지
의존성: pytest, unittest.mock
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core.chipset_detector import (
    _DEFAULT_BATCH_SIZE,
    ChipsetDetector,
    ChipsetInfo,
    OptimalProfile,
)


class TestChipsetDetection:
    """칩셋 감지 테스트."""

    @patch("core.chipset_detector.subprocess.run")
    @patch("core.chipset_detector.platform.machine", return_value="arm64")
    @patch("core.chipset_detector.psutil.virtual_memory")
    def test_apple_silicon_감지_arm64(self, mock_mem, mock_machine, mock_run):
        """arm64 아키텍처에서 Apple Silicon을 감지한다."""
        mock_mem.return_value = MagicMock(total=16 * 1024**3)
        mock_run.return_value = MagicMock(stdout="Apple M4", returncode=0)

        detector = ChipsetDetector()
        info = detector.detect()

        assert info.is_apple_silicon is True
        assert info.chip_name == "M4"
        assert info.ram_gb == 16

    @patch("core.chipset_detector.platform.machine", return_value="x86_64")
    @patch("core.chipset_detector.psutil.virtual_memory")
    def test_intel_mac_감지(self, mock_mem, mock_machine):
        """x86_64 아키텍처에서 Intel Mac으로 감지한다."""
        mock_mem.return_value = MagicMock(total=16 * 1024**3)

        detector = ChipsetDetector()
        info = detector.detect()

        assert info.is_apple_silicon is False
        assert info.chip_name is None

    @patch("core.chipset_detector.subprocess.run")
    @patch("core.chipset_detector.platform.machine", return_value="arm64")
    @patch("core.chipset_detector.psutil.virtual_memory")
    def test_sysctl_실패시_폴백(self, mock_mem, mock_machine, mock_run):
        """sysctl 호출 실패 시 chip_name은 None이지만 Apple Silicon은 감지한다."""
        mock_mem.return_value = MagicMock(total=16 * 1024**3)
        mock_run.return_value = MagicMock(stdout="", returncode=1)

        detector = ChipsetDetector()
        info = detector.detect()

        assert info.is_apple_silicon is True
        assert info.chip_name is None

    @patch("core.chipset_detector.subprocess.run")
    @patch("core.chipset_detector.platform.machine", return_value="arm64")
    @patch("core.chipset_detector.psutil.virtual_memory")
    def test_sysctl_타임아웃_폴백(self, mock_mem, mock_machine, mock_run):
        """sysctl 타임아웃 시 chip_name은 None이다."""
        mock_mem.return_value = MagicMock(total=16 * 1024**3)
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="sysctl", timeout=5)

        detector = ChipsetDetector()
        info = detector.detect()

        assert info.is_apple_silicon is True
        assert info.chip_name is None

    @patch("core.chipset_detector.psutil.virtual_memory")
    def test_RAM_크기_감지(self, mock_mem):
        """psutil을 통해 시스템 RAM 크기를 감지한다."""
        mock_mem.return_value = MagicMock(total=16 * 1024**3)

        ram_gb = ChipsetDetector._detect_ram_gb()
        assert ram_gb == 16

    @patch("core.chipset_detector.psutil.virtual_memory")
    def test_RAM_24GB_감지(self, mock_mem):
        """24GB RAM을 올바르게 감지한다."""
        mock_mem.return_value = MagicMock(total=24 * 1024**3)

        ram_gb = ChipsetDetector._detect_ram_gb()
        assert ram_gb == 24


class TestOptimalProfile:
    """칩셋+RAM 기반 최적 설정 프로파일 테스트."""

    @pytest.mark.parametrize(
        "chip_name, ram_gb, expected_batch_size",
        [
            ("M1", 8, 8),
            ("M1", 16, 12),
            ("M2", 8, 8),
            ("M2", 16, 12),
            ("M3", 8, 8),
            ("M3", 16, 16),
            ("M3", 24, 16),
            ("M4", 16, 16),
            ("M4", 24, 16),
            ("M4", 32, 24),
        ],
    )
    def test_칩셋_RAM_조합별_batch_size(self, chip_name, ram_gb, expected_batch_size):
        """칩셋+RAM 조합에 따른 최적 batch_size를 반환한다."""
        info = ChipsetInfo(is_apple_silicon=True, chip_name=chip_name, ram_gb=ram_gb)
        result = ChipsetDetector._compute_batch_size(info)
        assert result == expected_batch_size

    def test_intel_mac_기본값(self):
        """Intel Mac에서는 기본값 batch_size를 반환한다."""
        info = ChipsetInfo(is_apple_silicon=False, chip_name=None, ram_gb=16)
        result = ChipsetDetector._compute_batch_size(info)
        assert result == _DEFAULT_BATCH_SIZE

    def test_알_수_없는_칩셋_기본값(self):
        """알 수 없는 칩셋(M5 등)에서는 기본값을 반환한다."""
        info = ChipsetInfo(is_apple_silicon=True, chip_name="M5", ram_gb=32)
        result = ChipsetDetector._compute_batch_size(info)
        assert result == _DEFAULT_BATCH_SIZE

    def test_chip_name_None_기본값(self):
        """chip_name이 None이면 기본값을 반환한다."""
        info = ChipsetInfo(is_apple_silicon=True, chip_name=None, ram_gb=16)
        result = ChipsetDetector._compute_batch_size(info)
        assert result == _DEFAULT_BATCH_SIZE

    @patch("core.chipset_detector.subprocess.run")
    @patch("core.chipset_detector.platform.machine", return_value="arm64")
    @patch("core.chipset_detector.psutil.virtual_memory")
    def test_get_optimal_profile_M4_16GB(self, mock_mem, mock_machine, mock_run):
        """M4 16GB 시스템에서 최적 프로파일을 반환한다."""
        mock_mem.return_value = MagicMock(total=16 * 1024**3)
        mock_run.return_value = MagicMock(stdout="Apple M4", returncode=0)

        detector = ChipsetDetector()
        profile = detector.get_optimal_profile()

        assert isinstance(profile, OptimalProfile)
        assert profile.batch_size == 16


class TestChipNameParsing:
    """칩 이름 파싱 테스트."""

    @patch("core.chipset_detector.subprocess.run")
    def test_M4_Pro_파싱(self, mock_run):
        """'Apple M4 Pro'에서 'M4'를 추출한다."""
        mock_run.return_value = MagicMock(stdout="Apple M4 Pro", returncode=0)
        assert ChipsetDetector._detect_chip_name() == "M4"

    @patch("core.chipset_detector.subprocess.run")
    def test_M3_Max_파싱(self, mock_run):
        """'Apple M3 Max'에서 'M3'을 추출한다."""
        mock_run.return_value = MagicMock(stdout="Apple M3 Max", returncode=0)
        assert ChipsetDetector._detect_chip_name() == "M3"

    @patch("core.chipset_detector.subprocess.run")
    def test_M1_파싱(self, mock_run):
        """'Apple M1'에서 'M1'을 추출한다."""
        mock_run.return_value = MagicMock(stdout="Apple M1", returncode=0)
        assert ChipsetDetector._detect_chip_name() == "M1"
