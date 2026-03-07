"""
시스템 헬스체크 모듈 테스트 (System Health Check Tests)

목적: security/health_check.py의 모든 기능을 검증한다.
주요 테스트:
  - Ollama 서버 접근 확인 (성공/실패/예외)
  - EXAONE 모델 존재 확인 (정확 일치/부분 일치/미존재/서버 미응답)
  - ffmpeg 설치 확인 (존재/미존재)
  - Python 패키지 import 확인 (성공/실패)
  - 디스크 여유 공간 확인 (충분/부족/오류)
  - 데이터 디렉토리 접근 확인 (존재/미존재/쓰기불가)
  - 전체 실행(run) 통합 테스트
  - 비동기 래퍼(run_async) 테스트
  - 편의 함수(run_health_check) 테스트
  - HealthReport 프로퍼티 검증
의존성: pytest, config 모듈
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from config import AppConfig, LLMConfig, PathsConfig
from security.health_check import (
    _REQUIRED_PACKAGES,
    CheckResult,
    CheckStatus,
    HealthChecker,
    HealthReport,
    _is_writable,
    run_health_check,
)

# === 픽스처 (Fixtures) ===


@pytest.fixture
def base_dir(tmp_path: Path) -> Path:
    """테스트용 기본 데이터 디렉토리 경로."""
    d = tmp_path / "meeting-data"
    d.mkdir()
    return d


@pytest.fixture
def config(base_dir: Path) -> AppConfig:
    """테스트용 AppConfig 인스턴스."""
    return AppConfig(
        paths=PathsConfig(base_dir=str(base_dir)),
        llm=LLMConfig(
            host="http://127.0.0.1:11434",
            model_name="exaone3.5:7.8b-instruct-q4_K_M",
        ),
    )


@pytest.fixture
def checker(config: AppConfig) -> HealthChecker:
    """테스트용 HealthChecker 인스턴스."""
    return HealthChecker(config)


# === TestCheckOllamaServer ===


class TestCheckOllamaServer:
    """Ollama 서버 접근 체크 테스트."""

    def test_pass_when_server_responds(self, checker: HealthChecker) -> None:
        """Ollama 서버가 응답하면 PASS를 반환한다."""
        with patch("security.health_check.urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__ = MagicMock()
            mock_open.return_value.__exit__ = MagicMock(return_value=False)
            result = checker.check_ollama_server()

        assert result.status == CheckStatus.PASS
        assert result.name == "ollama_server"
        assert "정상" in result.message

    def test_fail_when_connection_refused(self, checker: HealthChecker) -> None:
        """Ollama 서버에 연결할 수 없으면 FAIL을 반환한다."""
        import urllib.error

        with patch(
            "security.health_check.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = checker.check_ollama_server()

        assert result.status == CheckStatus.FAIL
        assert "연결할 수 없습니다" in result.message
        assert "ollama serve" in result.guide

    def test_fail_when_unexpected_error(self, checker: HealthChecker) -> None:
        """예상치 못한 오류 발생 시 FAIL을 반환한다."""
        with patch(
            "security.health_check.urllib.request.urlopen",
            side_effect=RuntimeError("unexpected"),
        ):
            result = checker.check_ollama_server()

        assert result.status == CheckStatus.FAIL
        assert "오류" in result.message


# === TestCheckExaoneModel ===


class TestCheckExaoneModel:
    """EXAONE 모델 존재 체크 테스트."""

    def _mock_ollama_tags(self, models: list[str]) -> MagicMock:
        """Ollama /api/tags 응답을 모킹한다."""
        data = {"models": [{"name": m} for m in models]}
        resp = MagicMock()
        resp.read.return_value = json.dumps(data).encode("utf-8")
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_pass_when_exact_match(self, checker: HealthChecker) -> None:
        """정확한 모델명이 존재하면 PASS를 반환한다."""
        model_name = checker._config.llm.model_name
        resp = self._mock_ollama_tags([model_name, "llama2:7b"])

        with patch("security.health_check.urllib.request.urlopen", return_value=resp):
            result = checker.check_exaone_model()

        assert result.status == CheckStatus.PASS
        assert "확인 완료" in result.message

    def test_warn_when_partial_match(self, checker: HealthChecker) -> None:
        """기본 이름만 일치하면 WARN을 반환한다."""
        resp = self._mock_ollama_tags(["exaone3.5:7.8b-instruct-q8_0"])

        with patch("security.health_check.urllib.request.urlopen", return_value=resp):
            result = checker.check_exaone_model()

        assert result.status == CheckStatus.WARN
        assert "유사 모델" in result.message

    def test_fail_when_no_match(self, checker: HealthChecker) -> None:
        """모델이 전혀 없으면 FAIL을 반환한다."""
        resp = self._mock_ollama_tags(["llama2:7b"])

        with patch("security.health_check.urllib.request.urlopen", return_value=resp):
            result = checker.check_exaone_model()

        assert result.status == CheckStatus.FAIL
        assert "설치되지 않았습니다" in result.message
        assert "ollama pull" in result.guide

    def test_warn_when_server_unreachable(self, checker: HealthChecker) -> None:
        """Ollama 서버 미응답 시 WARN을 반환한다."""
        import urllib.error

        with patch(
            "security.health_check.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            result = checker.check_exaone_model()

        assert result.status == CheckStatus.WARN
        assert "미응답" in result.message

    def test_fail_when_unexpected_error(self, checker: HealthChecker) -> None:
        """예상치 못한 오류 시 FAIL을 반환한다."""
        with patch(
            "security.health_check.urllib.request.urlopen",
            side_effect=ValueError("bad data"),
        ):
            result = checker.check_exaone_model()

        assert result.status == CheckStatus.FAIL

    def test_fail_when_empty_model_list(self, checker: HealthChecker) -> None:
        """모델 목록이 비어있으면 FAIL을 반환한다."""
        resp = self._mock_ollama_tags([])

        with patch("security.health_check.urllib.request.urlopen", return_value=resp):
            result = checker.check_exaone_model()

        assert result.status == CheckStatus.FAIL


# === TestCheckFfmpeg ===


class TestCheckFfmpeg:
    """ffmpeg 설치 체크 테스트."""

    def test_pass_when_installed(self, checker: HealthChecker) -> None:
        """ffmpeg이 설치되어 있으면 PASS를 반환한다."""
        with patch("security.health_check.shutil.which", return_value="/usr/bin/ffmpeg"):
            result = checker.check_ffmpeg()

        assert result.status == CheckStatus.PASS
        assert "ffmpeg" in result.message

    def test_fail_when_not_installed(self, checker: HealthChecker) -> None:
        """ffmpeg이 설치되지 않았으면 FAIL을 반환한다."""
        with patch("security.health_check.shutil.which", return_value=None):
            result = checker.check_ffmpeg()

        assert result.status == CheckStatus.FAIL
        assert "설치되지 않았습니다" in result.message
        assert "brew install ffmpeg" in result.guide


# === TestCheckPythonPackages ===


class TestCheckPythonPackages:
    """Python 패키지 import 체크 테스트."""

    def test_pass_when_all_installed(self, checker: HealthChecker) -> None:
        """모든 패키지가 설치되어 있으면 전부 PASS를 반환한다."""
        with patch("security.health_check.importlib.import_module"):
            results = checker.check_python_packages()

        assert len(results) == len(_REQUIRED_PACKAGES)
        assert all(r.status == CheckStatus.PASS for r in results)

    def test_fail_for_missing_package(self, checker: HealthChecker) -> None:
        """누락된 패키지는 FAIL을 반환한다."""

        def mock_import(name: str) -> None:
            if name == "mlx_whisper":
                raise ImportError("No module named 'mlx_whisper'")

        with patch(
            "security.health_check.importlib.import_module",
            side_effect=mock_import,
        ):
            results = checker.check_python_packages()

        fail_results = [r for r in results if r.status == CheckStatus.FAIL]
        assert len(fail_results) == 1
        assert "mlx-whisper" in fail_results[0].message

    def test_multiple_failures(self, checker: HealthChecker) -> None:
        """여러 패키지가 누락되면 각각 FAIL을 반환한다."""

        def mock_import(name: str) -> None:
            if name in ("mlx_whisper", "chromadb"):
                raise ImportError(f"No module named '{name}'")

        with patch(
            "security.health_check.importlib.import_module",
            side_effect=mock_import,
        ):
            results = checker.check_python_packages()

        fail_results = [r for r in results if r.status == CheckStatus.FAIL]
        assert len(fail_results) == 2


# === TestCheckDiskSpace ===


class TestCheckDiskSpace:
    """디스크 여유 공간 체크 테스트."""

    def test_pass_when_sufficient(self, checker: HealthChecker) -> None:
        """여유 공간이 충분하면 PASS를 반환한다."""
        mock_usage = MagicMock()
        mock_usage.free = int(10 * 1024**3)  # 10GB

        with patch("security.health_check.shutil.disk_usage", return_value=mock_usage):
            result = checker.check_disk_space()

        assert result.status == CheckStatus.PASS
        assert "10.0GB" in result.message

    def test_warn_when_low(self, checker: HealthChecker) -> None:
        """여유 공간이 부족하면 WARN을 반환한다."""
        mock_usage = MagicMock()
        mock_usage.free = int(1 * 1024**3)  # 1GB

        with patch("security.health_check.shutil.disk_usage", return_value=mock_usage):
            result = checker.check_disk_space()

        assert result.status == CheckStatus.WARN
        assert "부족" in result.message

    def test_warn_when_error(self, checker: HealthChecker) -> None:
        """디스크 사용량 확인 실패 시 WARN을 반환한다."""
        with patch(
            "security.health_check.shutil.disk_usage",
            side_effect=OSError("Permission denied"),
        ):
            result = checker.check_disk_space()

        assert result.status == CheckStatus.WARN
        assert "실패" in result.message

    def test_nonexistent_base_dir_fallback(self, tmp_path: Path) -> None:
        """base_dir가 존재하지 않으면 상위 경로로 폴백한다."""
        config = AppConfig(
            paths=PathsConfig(base_dir=str(tmp_path / "not" / "exist")),
        )
        checker = HealthChecker(config)

        mock_usage = MagicMock()
        mock_usage.free = int(5 * 1024**3)

        with patch("security.health_check.shutil.disk_usage", return_value=mock_usage):
            result = checker.check_disk_space()

        assert result.status == CheckStatus.PASS


# === TestCheckDataDirectories ===


class TestCheckDataDirectories:
    """데이터 디렉토리 접근 체크 테스트."""

    def test_pass_when_all_exist_and_writable(
        self, base_dir: Path, checker: HealthChecker
    ) -> None:
        """모든 디렉토리가 존재하고 쓰기 가능하면 PASS를 반환한다."""
        # base_dir 하위 디렉토리 생성
        (base_dir / "outputs").mkdir()
        (base_dir / "chroma_db").mkdir()

        result = checker.check_data_directories()

        assert result.status == CheckStatus.PASS
        assert "접근 가능" in result.message

    def test_warn_when_dirs_missing(self, tmp_path: Path) -> None:
        """디렉토리가 존재하지 않으면 WARN을 반환한다."""
        config = AppConfig(
            paths=PathsConfig(base_dir=str(tmp_path / "nonexistent")),
        )
        checker = HealthChecker(config)

        result = checker.check_data_directories()

        assert result.status == CheckStatus.WARN
        assert "미존재" in result.message

    def test_fail_when_not_writable(self, base_dir: Path, checker: HealthChecker) -> None:
        """디렉토리에 쓰기 불가하면 FAIL을 반환한다."""
        (base_dir / "outputs").mkdir()
        (base_dir / "chroma_db").mkdir()

        with patch(
            "security.health_check._is_writable",
            return_value=False,
        ):
            result = checker.check_data_directories()

        assert result.status == CheckStatus.FAIL
        assert "쓰기 불가" in result.message


# === TestIsWritable ===


class TestIsWritable:
    """_is_writable 유틸리티 함수 테스트."""

    def test_writable_dir(self, tmp_path: Path) -> None:
        """쓰기 가능한 디렉토리에서 True를 반환한다."""
        assert _is_writable(tmp_path) is True

    def test_not_writable_dir(self, tmp_path: Path) -> None:
        """touch 실패 시 False를 반환한다."""
        with patch.object(Path, "touch", side_effect=OSError("Permission denied")):
            assert _is_writable(tmp_path) is False


# === TestHealthReport ===


class TestHealthReport:
    """HealthReport 데이터클래스 프로퍼티 테스트."""

    def test_all_passed_true(self) -> None:
        """모든 항목이 PASS이면 all_passed가 True이다."""
        report = HealthReport(
            results=[
                CheckResult(name="a", status=CheckStatus.PASS, message="ok"),
                CheckResult(name="b", status=CheckStatus.PASS, message="ok"),
            ]
        )
        assert report.all_passed is True
        assert report.fail_count == 0
        assert report.warn_count == 0

    def test_all_passed_false_with_fail(self) -> None:
        """FAIL 항목이 있으면 all_passed가 False이다."""
        report = HealthReport(
            results=[
                CheckResult(name="a", status=CheckStatus.PASS, message="ok"),
                CheckResult(name="b", status=CheckStatus.FAIL, message="fail"),
            ]
        )
        assert report.all_passed is False
        assert report.fail_count == 1

    def test_all_passed_false_with_warn(self) -> None:
        """WARN 항목이 있으면 all_passed가 False이다."""
        report = HealthReport(
            results=[
                CheckResult(name="a", status=CheckStatus.PASS, message="ok"),
                CheckResult(name="b", status=CheckStatus.WARN, message="warn"),
            ]
        )
        assert report.all_passed is False
        assert report.warn_count == 1

    def test_empty_report(self) -> None:
        """빈 결과는 all_passed가 True이다."""
        report = HealthReport()
        assert report.all_passed is True
        assert report.fail_count == 0
        assert report.warn_count == 0

    def test_mixed_statuses(self) -> None:
        """혼합된 상태에서 카운트가 정확하다."""
        report = HealthReport(
            results=[
                CheckResult(name="a", status=CheckStatus.PASS, message="ok"),
                CheckResult(name="b", status=CheckStatus.FAIL, message="fail"),
                CheckResult(name="c", status=CheckStatus.WARN, message="warn"),
                CheckResult(name="d", status=CheckStatus.FAIL, message="fail2"),
            ]
        )
        assert report.fail_count == 2
        assert report.warn_count == 1


# === TestCheckStatus ===


class TestCheckStatus:
    """CheckStatus 열거형 테스트."""

    def test_values(self) -> None:
        """열거형 값이 올바른지 확인한다."""
        assert CheckStatus.PASS.value == "pass"
        assert CheckStatus.FAIL.value == "fail"
        assert CheckStatus.WARN.value == "warn"


# === TestRun ===


class TestRun:
    """전체 실행(run) 통합 테스트."""

    def test_run_collects_all_results(self, checker: HealthChecker) -> None:
        """run()이 모든 체크 결과를 수집한다."""
        with (
            patch.object(
                checker,
                "check_ollama_server",
                return_value=CheckResult(
                    name="ollama_server",
                    status=CheckStatus.PASS,
                    message="ok",
                ),
            ),
            patch.object(
                checker,
                "check_exaone_model",
                return_value=CheckResult(
                    name="exaone_model",
                    status=CheckStatus.PASS,
                    message="ok",
                ),
            ),
            patch.object(
                checker,
                "check_ffmpeg",
                return_value=CheckResult(
                    name="ffmpeg",
                    status=CheckStatus.PASS,
                    message="ok",
                ),
            ),
            patch.object(
                checker,
                "check_python_packages",
                return_value=[
                    CheckResult(name="pkg_a", status=CheckStatus.PASS, message="ok"),
                ],
            ),
            patch.object(
                checker,
                "check_disk_space",
                return_value=CheckResult(
                    name="disk_space",
                    status=CheckStatus.PASS,
                    message="ok",
                ),
            ),
            patch.object(
                checker,
                "check_data_directories",
                return_value=CheckResult(
                    name="data_directories",
                    status=CheckStatus.PASS,
                    message="ok",
                ),
            ),
        ):
            report = checker.run()

        assert report.all_passed is True
        # ollama + exaone + ffmpeg + 1 pkg + disk + dirs = 6
        assert len(report.results) == 6

    def test_run_continues_after_check_exception(self, checker: HealthChecker) -> None:
        """개별 체크에서 예외가 발생해도 나머지 체크는 계속된다."""
        with (
            patch.object(
                checker,
                "check_ollama_server",
                side_effect=RuntimeError("boom"),
            ),
            patch.object(
                checker,
                "check_exaone_model",
                return_value=CheckResult(
                    name="exaone_model",
                    status=CheckStatus.PASS,
                    message="ok",
                ),
            ),
            patch.object(
                checker,
                "check_ffmpeg",
                return_value=CheckResult(
                    name="ffmpeg",
                    status=CheckStatus.PASS,
                    message="ok",
                ),
            ),
            patch.object(checker, "check_python_packages", return_value=[]),
            patch.object(
                checker,
                "check_disk_space",
                return_value=CheckResult(
                    name="disk_space",
                    status=CheckStatus.PASS,
                    message="ok",
                ),
            ),
            patch.object(
                checker,
                "check_data_directories",
                return_value=CheckResult(
                    name="data_directories",
                    status=CheckStatus.PASS,
                    message="ok",
                ),
            ),
        ):
            report = checker.run()

        # 예외 발생한 체크도 결과에 포함 (FAIL)
        failed = [r for r in report.results if r.status == CheckStatus.FAIL]
        assert len(failed) == 1
        assert "예외 발생" in failed[0].message

    def test_run_handles_list_results(self, checker: HealthChecker) -> None:
        """check_python_packages가 리스트를 반환하면 results에 확장된다."""
        pkg_results = [
            CheckResult(name="pkg_a", status=CheckStatus.PASS, message="ok"),
            CheckResult(name="pkg_b", status=CheckStatus.FAIL, message="fail"),
        ]

        with (
            patch.object(
                checker,
                "check_ollama_server",
                return_value=CheckResult(
                    name="ollama_server",
                    status=CheckStatus.PASS,
                    message="ok",
                ),
            ),
            patch.object(
                checker,
                "check_exaone_model",
                return_value=CheckResult(
                    name="exaone_model",
                    status=CheckStatus.PASS,
                    message="ok",
                ),
            ),
            patch.object(
                checker,
                "check_ffmpeg",
                return_value=CheckResult(
                    name="ffmpeg",
                    status=CheckStatus.PASS,
                    message="ok",
                ),
            ),
            patch.object(checker, "check_python_packages", return_value=pkg_results),
            patch.object(
                checker,
                "check_disk_space",
                return_value=CheckResult(
                    name="disk_space",
                    status=CheckStatus.PASS,
                    message="ok",
                ),
            ),
            patch.object(
                checker,
                "check_data_directories",
                return_value=CheckResult(
                    name="data_directories",
                    status=CheckStatus.PASS,
                    message="ok",
                ),
            ),
        ):
            report = checker.run()

        # ollama + exaone + ffmpeg + 2 pkgs + disk + dirs = 7
        assert len(report.results) == 7


# === TestRunAsync ===


class TestRunAsync:
    """비동기 래퍼 테스트."""

    def test_run_async(self, checker: HealthChecker) -> None:
        """run_async()가 run()과 동일한 결과를 반환한다."""
        mock_report = HealthReport(
            results=[
                CheckResult(name="test", status=CheckStatus.PASS, message="ok"),
            ]
        )

        with patch.object(checker, "run", return_value=mock_report):
            result = asyncio.get_event_loop().run_until_complete(checker.run_async())

        assert result.all_passed is True
        assert len(result.results) == 1


# === TestRunHealthCheck ===


class TestRunHealthCheck:
    """편의 함수 run_health_check 테스트."""

    def test_with_config(self, config: AppConfig) -> None:
        """config를 전달하면 해당 설정으로 체크한다."""
        mock_report = HealthReport(
            results=[
                CheckResult(name="test", status=CheckStatus.PASS, message="ok"),
            ]
        )

        with patch.object(HealthChecker, "run", return_value=mock_report):
            report = run_health_check(config)

        assert report.all_passed is True

    def test_without_config(self) -> None:
        """config가 None이면 싱글턴에서 가져온다."""
        mock_report = HealthReport(
            results=[
                CheckResult(name="test", status=CheckStatus.PASS, message="ok"),
            ]
        )

        with (
            patch("config.get_config") as mock_get_config,
            patch.object(HealthChecker, "run", return_value=mock_report),
        ):
            mock_get_config.return_value = AppConfig()
            report = run_health_check()

        mock_get_config.assert_called_once()
        assert report.all_passed is True


# === TestLogReport ===


class TestLogReport:
    """_log_report 로깅 테스트."""

    def test_log_all_passed(self, checker: HealthChecker) -> None:
        """모든 항목 통과 시 INFO 로그를 남긴다."""
        report = HealthReport(
            results=[
                CheckResult(name="a", status=CheckStatus.PASS, message="ok"),
            ]
        )

        with patch("security.health_check.logger") as mock_logger:
            checker._log_report(report)

        mock_logger.info.assert_called_once()
        assert "모든 항목 통과" in mock_logger.info.call_args[0][0]

    def test_log_failures(self, checker: HealthChecker) -> None:
        """실패 항목이 있으면 WARNING 로그를 남긴다."""
        report = HealthReport(
            results=[
                CheckResult(
                    name="a",
                    status=CheckStatus.FAIL,
                    message="실패 메시지",
                    guide="해결 가이드",
                ),
            ]
        )

        with patch("security.health_check.logger") as mock_logger:
            checker._log_report(report)

        # 첫 번째 warning: 요약, 두 번째: 상세
        assert mock_logger.warning.call_count >= 2
