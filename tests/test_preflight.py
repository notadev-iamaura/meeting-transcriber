"""
사전 검증(preflight) 모듈 테스트.

core/preflight.py의 시스템 환경 검증 로직을 테스트한다.
- Apple Silicon 검출
- Python 버전 호환성 검사
- Metal GPU 가용성 검사 (subprocess mock)
- MLX import 가용성 검사 (subprocess mock)
- 결과 캐싱 동작
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core.preflight import (
    PreflightResult,
    _check_apple_silicon,
    _check_metal_availability,
    _check_mlx_importable,
    _check_python_version,
    reset_preflight_cache,
    run_preflight,
)

# === PreflightResult 테스트 ===


class TestPreflightResult:
    """PreflightResult dataclass 속성 테스트."""

    def test_can_use_mlx_모든_조건_충족(self) -> None:
        result = PreflightResult(
            is_apple_silicon=True,
            metal_available=True,
            python_compatible=True,
            mlx_importable=True,
            warnings=(),
        )
        assert result.can_use_mlx is True

    def test_can_use_mlx_metal_불가(self) -> None:
        result = PreflightResult(
            is_apple_silicon=True,
            metal_available=False,
            python_compatible=True,
            mlx_importable=True,
            warnings=(),
        )
        assert result.can_use_mlx is False

    def test_can_use_mlx_intel_mac(self) -> None:
        result = PreflightResult(
            is_apple_silicon=False,
            metal_available=False,
            python_compatible=True,
            mlx_importable=False,
            warnings=(),
        )
        assert result.can_use_mlx is False

    def test_can_use_mlx_mlx_미설치(self) -> None:
        result = PreflightResult(
            is_apple_silicon=True,
            metal_available=True,
            python_compatible=True,
            mlx_importable=False,
            warnings=(),
        )
        assert result.can_use_mlx is False

    def test_can_use_chromadb_호환(self) -> None:
        result = PreflightResult(
            is_apple_silicon=True,
            metal_available=True,
            python_compatible=True,
            mlx_importable=True,
            warnings=(),
        )
        assert result.can_use_chromadb is True

    def test_can_use_chromadb_비호환(self) -> None:
        result = PreflightResult(
            is_apple_silicon=True,
            metal_available=True,
            python_compatible=False,
            mlx_importable=True,
            warnings=("Python 3.13 미지원",),
        )
        assert result.can_use_chromadb is False


# === 개별 검증 함수 테스트 ===


class TestCheckAppleSilicon:
    """_check_apple_silicon() 테스트."""

    @patch("core.preflight.platform.machine", return_value="arm64")
    def test_arm64_반환(self, _mock: MagicMock) -> None:
        assert _check_apple_silicon() is True

    @patch("core.preflight.platform.machine", return_value="x86_64")
    def test_intel_반환(self, _mock: MagicMock) -> None:
        assert _check_apple_silicon() is False


class _FakeVersionInfo:
    """sys.version_info를 흉내내는 가짜 객체."""

    def __init__(self, major: int, minor: int, micro: int = 0) -> None:
        self.major = major
        self.minor = minor
        self.micro = micro
        self._tuple = (major, minor, micro)

    def __ge__(self, other: object) -> bool:
        if isinstance(other, tuple):
            return self._tuple >= other
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, tuple):
            return self._tuple < other
        return NotImplemented


class TestCheckPythonVersion:
    """_check_python_version() 테스트."""

    @patch("core.preflight.sys")
    def test_python_312_호환(self, mock_sys: MagicMock) -> None:
        mock_sys.version_info = _FakeVersionInfo(3, 12, 8)
        ok, warnings = _check_python_version()
        assert ok is True
        assert warnings == []

    @patch("core.preflight.sys")
    def test_python_311_호환(self, mock_sys: MagicMock) -> None:
        mock_sys.version_info = _FakeVersionInfo(3, 11, 0)
        ok, warnings = _check_python_version()
        assert ok is True
        assert warnings == []

    @patch("core.preflight.sys")
    def test_python_313_비호환(self, mock_sys: MagicMock) -> None:
        mock_sys.version_info = _FakeVersionInfo(3, 13, 0)
        ok, warnings = _check_python_version()
        assert ok is False
        assert len(warnings) == 1
        assert "3.13" in warnings[0]

    @patch("core.preflight.sys")
    def test_python_310_비호환(self, mock_sys: MagicMock) -> None:
        mock_sys.version_info = _FakeVersionInfo(3, 10, 0)
        ok, warnings = _check_python_version()
        assert ok is False
        assert len(warnings) == 1


class TestCheckMetalAvailability:
    """_check_metal_availability() subprocess 검증 테스트.

    `_check_metal_availability` 는 CI 환경변수를 만나면 곧바로 False 를
    반환하는 SIGABRT 가드(GitHub macOS runner 의 mlx import abort 회피)를
    가지고 있다. 본 클래스의 테스트들은 subprocess mock 결과를 검증하므로
    autouse fixture 로 CI 변수를 제거하여 가드 분기를 우회한다.
    """

    @pytest.fixture(autouse=True)
    def _disable_ci_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """모든 테스트 시작 시 CI 환경변수를 제거 (가드 우회)."""
        monkeypatch.delenv("CI", raising=False)

    @patch("core.preflight.subprocess.run")
    def test_metal_가용(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        assert _check_metal_availability() is True

    @patch("core.preflight.subprocess.run")
    def test_metal_불가(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stderr="Metal not available")
        assert _check_metal_availability() is False

    @patch("core.preflight.subprocess.run")
    def test_metal_타임아웃(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=15)
        assert _check_metal_availability() is False

    @patch("core.preflight.subprocess.run")
    def test_metal_파일없음(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError("python not found")
        assert _check_metal_availability() is False

    def test_CI_환경변수면_subprocess_호출_없이_False(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CI=true 면 subprocess 호출 자체를 회피하고 False 반환 (SIGABRT 방지)."""
        monkeypatch.setenv("CI", "true")
        with patch("core.preflight.subprocess.run") as mock_run:
            assert _check_metal_availability() is False
            mock_run.assert_not_called()


class TestCheckMlxImportable:
    """_check_mlx_importable() subprocess 검증 테스트."""

    @patch("core.preflight.subprocess.run")
    def test_mlx_설치됨(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        assert _check_mlx_importable() is True

    @patch("core.preflight.subprocess.run")
    def test_mlx_미설치(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        assert _check_mlx_importable() is False

    @patch("core.preflight.subprocess.run")
    def test_mlx_타임아웃(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=10)
        assert _check_mlx_importable() is False


# === 통합 테스트: run_preflight ===


class TestRunPreflight:
    """run_preflight() 통합 테스트."""

    def setup_method(self) -> None:
        reset_preflight_cache()

    def teardown_method(self) -> None:
        reset_preflight_cache()

    @patch("core.preflight._check_mlx_importable", return_value=True)
    @patch("core.preflight._check_metal_availability", return_value=True)
    @patch("core.preflight._check_python_version", return_value=(True, []))
    @patch("core.preflight._check_apple_silicon", return_value=True)
    def test_모든_검증_통과(
        self,
        _m_as: MagicMock,
        _m_pv: MagicMock,
        _m_metal: MagicMock,
        _m_mlx: MagicMock,
    ) -> None:
        result = run_preflight()
        assert result.can_use_mlx is True
        assert result.can_use_chromadb is True
        assert result.warnings == ()

    @patch("core.preflight._check_apple_silicon", return_value=False)
    @patch("core.preflight._check_python_version", return_value=(True, []))
    def test_intel_mac(
        self,
        _m_pv: MagicMock,
        _m_as: MagicMock,
    ) -> None:
        result = run_preflight()
        assert result.can_use_mlx is False
        assert result.is_apple_silicon is False
        assert len(result.warnings) > 0

    @patch("core.preflight._check_mlx_importable", return_value=True)
    @patch("core.preflight._check_metal_availability", return_value=True)
    @patch("core.preflight._check_python_version", return_value=(True, []))
    @patch("core.preflight._check_apple_silicon", return_value=True)
    def test_캐싱_동작(
        self,
        m_as: MagicMock,
        _m_pv: MagicMock,
        _m_metal: MagicMock,
        _m_mlx: MagicMock,
    ) -> None:
        result1 = run_preflight()
        result2 = run_preflight()
        # 동일 인스턴스 반환 (캐시)
        assert result1 is result2
        # _check_apple_silicon은 1번만 호출
        m_as.assert_called_once()

    @patch("core.preflight._check_mlx_importable", return_value=True)
    @patch("core.preflight._check_metal_availability", return_value=True)
    @patch("core.preflight._check_python_version", return_value=(True, []))
    @patch("core.preflight._check_apple_silicon", return_value=True)
    def test_강제_재검증(
        self,
        m_as: MagicMock,
        _m_pv: MagicMock,
        _m_metal: MagicMock,
        _m_mlx: MagicMock,
    ) -> None:
        run_preflight()
        run_preflight(force=True)
        assert m_as.call_count == 2

    @patch("core.preflight._check_apple_silicon", return_value=True)
    @patch("core.preflight._check_python_version", return_value=(False, ["Python 3.13 미지원"]))
    @patch("core.preflight._check_mlx_importable", return_value=True)
    @patch("core.preflight._check_metal_availability", return_value=True)
    def test_python_비호환(
        self,
        _m_metal: MagicMock,
        _m_mlx: MagicMock,
        _m_pv: MagicMock,
        _m_as: MagicMock,
    ) -> None:
        result = run_preflight()
        assert result.can_use_chromadb is False
        assert "Python 3.13" in result.warnings[0]
