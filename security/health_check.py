"""
시스템 헬스체크 모듈 (System Health Check Module)

목적: 앱 시작 시 필수 의존성(Ollama, ffmpeg, Python 패키지, EXAONE 모델,
      디스크 여유, 데이터 디렉토리)을 점검하여 누락 시 안내 메시지를 표시한다.
주요 기능:
  - Ollama 서버 접근 가능 여부 확인
  - EXAONE LLM 모델 다운로드 여부 확인
  - ffmpeg 설치 여부 확인
  - 핵심 Python 패키지 import 가능 여부 확인
  - 디스크 여유 공간 확인
  - 데이터 디렉토리 접근 가능 여부 확인
의존성: config 모듈 (AppConfig)
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from config import AppConfig

logger = logging.getLogger(__name__)


class CheckStatus(Enum):
    """헬스체크 항목의 상태를 나타내는 열거형."""

    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"


@dataclass
class CheckResult:
    """개별 헬스체크 항목의 결과를 담는 데이터클래스.

    Attributes:
        name: 체크 항목 이름 (예: "ollama_server")
        status: 체크 결과 상태
        message: 사용자에게 표시할 한국어 메시지
        guide: 실패 시 해결 방법 안내 (선택)
    """

    name: str
    status: CheckStatus
    message: str
    guide: str = ""


@dataclass
class HealthReport:
    """전체 헬스체크 결과를 담는 데이터클래스.

    Attributes:
        results: 개별 체크 결과 목록
        all_passed: 모든 항목이 PASS인지 여부
        fail_count: 실패 항목 수
        warn_count: 경고 항목 수
    """

    results: list[CheckResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        """모든 체크 항목이 통과했는지 반환한다."""
        return all(r.status == CheckStatus.PASS for r in self.results)

    @property
    def fail_count(self) -> int:
        """실패한 체크 항목 수를 반환한다."""
        return sum(1 for r in self.results if r.status == CheckStatus.FAIL)

    @property
    def warn_count(self) -> int:
        """경고 체크 항목 수를 반환한다."""
        return sum(1 for r in self.results if r.status == CheckStatus.WARN)


# Ollama API 요청 기본 타임아웃 (초)
_OLLAMA_TIMEOUT_SECONDS = 5

# 디스크 최소 여유 공간 (GB)
_MIN_DISK_FREE_GB = 2.0

# 필수 Python 패키지 목록 (import 이름, 표시 이름)
_REQUIRED_PACKAGES: list[tuple[str, str]] = [
    ("mlx_whisper", "mlx-whisper"),
    ("pyannote.audio", "pyannote-audio"),
    ("chromadb", "chromadb"),
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("yaml", "pyyaml"),
    ("pydantic", "pydantic"),
    ("rumps", "rumps"),
]


class HealthChecker:
    """시스템 헬스체크를 수행하는 클래스.

    앱 시작 시 필수 의존성을 점검하고 결과를 보고한다.
    각 체크 항목은 독립적으로 실행되어 하나의 실패가 다른 체크에
    영향을 주지 않는다.

    Args:
        config: 애플리케이션 설정 인스턴스

    사용 예시:
        config = load_config()
        checker = HealthChecker(config)
        report = checker.run()
        if not report.all_passed:
            for r in report.results:
                if r.status != CheckStatus.PASS:
                    print(f"[{r.status.value}] {r.message}")
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def run(self) -> HealthReport:
        """모든 헬스체크 항목을 실행하고 보고서를 반환한다.

        각 체크 메서드를 순차적으로 호출한다.
        개별 체크에서 예외가 발생해도 나머지 체크는 계속 진행한다.

        Returns:
            전체 헬스체크 결과가 담긴 HealthReport
        """
        report = HealthReport()

        # 체크 메서드 목록 (순서대로 실행)
        checks = [
            self.check_ollama_server,
            self.check_exaone_model,
            self.check_ffmpeg,
            self.check_python_packages,
            self.check_disk_space,
            self.check_data_directories,
        ]

        for check_fn in checks:
            try:
                result = check_fn()
                # 단일 결과 또는 결과 리스트 처리
                if isinstance(result, list):
                    report.results.extend(result)
                else:
                    report.results.append(result)
            except Exception as e:
                # 체크 함수 자체에서 예외 발생 시 FAIL로 기록
                fn_name = getattr(check_fn, "__name__", str(check_fn))
                report.results.append(CheckResult(
                    name=fn_name,
                    status=CheckStatus.FAIL,
                    message=f"체크 실행 중 예외 발생: {e}",
                ))
                logger.error(
                    f"헬스체크 '{fn_name}' 실행 중 오류: {e}",
                    exc_info=True,
                )

        # 결과 로깅
        self._log_report(report)

        return report

    async def run_async(self) -> HealthReport:
        """run()의 비동기 래퍼.

        이벤트 루프 블로킹을 방지하기 위해 별도 스레드에서 실행한다.

        Returns:
            전체 헬스체크 결과가 담긴 HealthReport
        """
        return await asyncio.to_thread(self.run)

    # === 개별 체크 메서드 ===

    def check_ollama_server(self) -> CheckResult:
        """Ollama 서버 접근 가능 여부를 확인한다.

        config.yaml의 llm.host에 HTTP 요청을 보내 응답을 확인한다.

        Returns:
            Ollama 서버 체크 결과
        """
        host = self._config.llm.host
        url = host.rstrip("/")

        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=_OLLAMA_TIMEOUT_SECONDS):
                pass
            return CheckResult(
                name="ollama_server",
                status=CheckStatus.PASS,
                message=f"Ollama 서버 정상 ({host})",
            )
        except urllib.error.URLError as e:
            return CheckResult(
                name="ollama_server",
                status=CheckStatus.FAIL,
                message=f"Ollama 서버에 연결할 수 없습니다 ({host})",
                guide="Ollama를 설치하고 실행하세요: brew install ollama && ollama serve",
            )
        except Exception as e:
            return CheckResult(
                name="ollama_server",
                status=CheckStatus.FAIL,
                message=f"Ollama 서버 확인 중 오류: {e}",
                guide="Ollama를 설치하고 실행하세요: brew install ollama && ollama serve",
            )

    def check_exaone_model(self) -> CheckResult:
        """EXAONE 모델이 Ollama에 다운로드되어 있는지 확인한다.

        Ollama의 /api/tags 엔드포인트를 호출하여 모델 목록을 조회한다.

        Returns:
            EXAONE 모델 체크 결과
        """
        import json

        host = self._config.llm.host.rstrip("/")
        model_name = self._config.llm.model_name
        url = f"{host}/api/tags"

        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=_OLLAMA_TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            # 모델 목록에서 검색
            models = data.get("models", [])
            model_names = [m.get("name", "") for m in models]

            if model_name in model_names:
                return CheckResult(
                    name="exaone_model",
                    status=CheckStatus.PASS,
                    message=f"EXAONE 모델 확인 완료 ({model_name})",
                )

            # 정확히 일치하지 않으면 부분 일치 확인
            # (태그 없이 기본 이름만 있는 경우 대비)
            base_name = model_name.split(":")[0]
            partial_matches = [n for n in model_names if n.startswith(base_name)]
            if partial_matches:
                return CheckResult(
                    name="exaone_model",
                    status=CheckStatus.WARN,
                    message=(
                        f"EXAONE 모델 '{model_name}'은 없지만 "
                        f"유사 모델 발견: {', '.join(partial_matches)}"
                    ),
                    guide=f"정확한 모델을 다운로드하세요: ollama pull {model_name}",
                )

            return CheckResult(
                name="exaone_model",
                status=CheckStatus.FAIL,
                message=f"EXAONE 모델이 설치되지 않았습니다 ({model_name})",
                guide=f"모델을 다운로드하세요: ollama pull {model_name}",
            )

        except urllib.error.URLError:
            # Ollama 서버가 꺼져 있으면 모델 확인 불가
            return CheckResult(
                name="exaone_model",
                status=CheckStatus.WARN,
                message="Ollama 서버 미응답으로 EXAONE 모델 확인 불가",
                guide="먼저 Ollama 서버를 시작하세요: ollama serve",
            )
        except Exception as e:
            return CheckResult(
                name="exaone_model",
                status=CheckStatus.FAIL,
                message=f"EXAONE 모델 확인 중 오류: {e}",
                guide=f"모델을 다운로드하세요: ollama pull {model_name}",
            )

    def check_ffmpeg(self) -> CheckResult:
        """ffmpeg이 시스템 PATH에 존재하는지 확인한다.

        shutil.which()로 PATH 검색하므로 subprocess 오버헤드가 없다.

        Returns:
            ffmpeg 설치 체크 결과
        """
        ffmpeg_path = shutil.which("ffmpeg")

        if ffmpeg_path is not None:
            return CheckResult(
                name="ffmpeg",
                status=CheckStatus.PASS,
                message=f"ffmpeg 설치 확인 ({ffmpeg_path})",
            )

        return CheckResult(
            name="ffmpeg",
            status=CheckStatus.FAIL,
            message="ffmpeg이 설치되지 않았습니다",
            guide="ffmpeg을 설치하세요: brew install ffmpeg",
        )

    def check_python_packages(self) -> list[CheckResult]:
        """핵심 Python 패키지의 import 가능 여부를 확인한다.

        각 패키지를 importlib.import_module()로 시도한다.

        Returns:
            각 패키지별 체크 결과 리스트
        """
        results: list[CheckResult] = []

        for import_name, display_name in _REQUIRED_PACKAGES:
            try:
                importlib.import_module(import_name)
                results.append(CheckResult(
                    name=f"package_{import_name}",
                    status=CheckStatus.PASS,
                    message=f"패키지 '{display_name}' 설치 확인",
                ))
            except ImportError:
                results.append(CheckResult(
                    name=f"package_{import_name}",
                    status=CheckStatus.FAIL,
                    message=f"패키지 '{display_name}'이(가) 설치되지 않았습니다",
                    guide=f"패키지를 설치하세요: pip install {display_name}",
                ))

        return results

    def check_disk_space(self) -> CheckResult:
        """데이터 디렉토리 파티션의 디스크 여유 공간을 확인한다.

        base_dir가 존재하면 해당 경로, 없으면 홈 디렉토리를 기준으로
        shutil.disk_usage()를 호출한다.

        Returns:
            디스크 여유 공간 체크 결과
        """
        base_dir = self._config.paths.resolved_base_dir

        # base_dir가 아직 생성되지 않았을 수 있으므로, 존재하는 상위 경로를 찾는다
        check_path = base_dir
        while not check_path.exists() and check_path.parent != check_path:
            check_path = check_path.parent

        if not check_path.exists():
            check_path = Path.home()

        try:
            usage = shutil.disk_usage(str(check_path))
            free_gb = usage.free / (1024 ** 3)

            if free_gb >= _MIN_DISK_FREE_GB:
                return CheckResult(
                    name="disk_space",
                    status=CheckStatus.PASS,
                    message=f"디스크 여유 공간: {free_gb:.1f}GB",
                )

            return CheckResult(
                name="disk_space",
                status=CheckStatus.WARN,
                message=f"디스크 여유 공간 부족: {free_gb:.1f}GB (권장: {_MIN_DISK_FREE_GB}GB 이상)",
                guide="불필요한 파일을 정리하여 디스크 공간을 확보하세요",
            )
        except OSError as e:
            return CheckResult(
                name="disk_space",
                status=CheckStatus.WARN,
                message=f"디스크 여유 공간 확인 실패: {e}",
            )

    def check_data_directories(self) -> CheckResult:
        """데이터 디렉토리의 존재 및 접근 가능 여부를 확인한다.

        base_dir 및 핵심 하위 디렉토리가 존재하고 쓰기 가능한지 확인한다.
        디렉토리가 없으면 경고만 표시한다 (시작 시 자동 생성되므로).

        Returns:
            데이터 디렉토리 체크 결과
        """
        paths = self._config.paths
        dirs_to_check = {
            "base_dir": paths.resolved_base_dir,
            "outputs": paths.resolved_outputs_dir,
            "chroma_db": paths.resolved_chroma_db_dir,
        }

        missing: list[str] = []
        not_writable: list[str] = []

        for label, dir_path in dirs_to_check.items():
            if not dir_path.exists():
                missing.append(label)
            elif not _is_writable(dir_path):
                not_writable.append(label)

        if not missing and not not_writable:
            return CheckResult(
                name="data_directories",
                status=CheckStatus.PASS,
                message="데이터 디렉토리 접근 가능",
            )

        if not_writable:
            return CheckResult(
                name="data_directories",
                status=CheckStatus.FAIL,
                message=f"데이터 디렉토리 쓰기 불가: {', '.join(not_writable)}",
                guide="디렉토리 권한을 확인하세요: chmod 700 <경로>",
            )

        # 디렉토리 미존재는 경고 (시작 시 자동 생성)
        return CheckResult(
            name="data_directories",
            status=CheckStatus.WARN,
            message=f"데이터 디렉토리 미존재 (시작 시 자동 생성): {', '.join(missing)}",
        )

    # === 내부 유틸리티 ===

    def _log_report(self, report: HealthReport) -> None:
        """헬스체크 결과를 로그에 기록한다.

        Args:
            report: 헬스체크 결과 보고서
        """
        if report.all_passed:
            logger.info(
                f"헬스체크 완료: 모든 항목 통과 ({len(report.results)}개)"
            )
            return

        logger.warning(
            f"헬스체크 완료: "
            f"{report.fail_count}개 실패, {report.warn_count}개 경고 "
            f"(전체 {len(report.results)}개)"
        )

        for result in report.results:
            if result.status == CheckStatus.FAIL:
                msg = f"  [실패] {result.message}"
                if result.guide:
                    msg += f"\n         → {result.guide}"
                logger.warning(msg)
            elif result.status == CheckStatus.WARN:
                msg = f"  [경고] {result.message}"
                if result.guide:
                    msg += f"\n         → {result.guide}"
                logger.warning(msg)

    def get_failure_summary(self, report: HealthReport) -> Optional[str]:
        """헬스체크 실패 시 알림에 사용할 요약 메시지를 생성한다.

        모든 항목이 통과하면 None을 반환한다.
        실패/경고가 있으면 간결한 요약 문자열을 반환한다.
        메뉴바 알림, WebSocket 알림 등에서 활용할 수 있다.

        Args:
            report: 헬스체크 결과 보고서

        Returns:
            실패 요약 문자열 또는 None (모두 통과 시)
        """
        if report.all_passed:
            return None

        issues: list[str] = []
        for result in report.results:
            if result.status == CheckStatus.FAIL:
                issues.append(f"[실패] {result.name}: {result.message}")
            elif result.status == CheckStatus.WARN:
                issues.append(f"[경고] {result.name}: {result.message}")

        return (
            f"헬스체크 문제 발견 ({report.fail_count}개 실패, "
            f"{report.warn_count}개 경고):\n" + "\n".join(issues)
        )


def _is_writable(dir_path: Path) -> bool:
    """디렉토리에 쓰기가 가능한지 확인한다.

    임시 파일 생성/삭제를 시도하여 실제 쓰기 가능 여부를 판단한다.

    Args:
        dir_path: 확인할 디렉토리 경로

    Returns:
        쓰기 가능하면 True
    """
    test_file = dir_path / ".health_check_write_test"
    try:
        test_file.touch()
        test_file.unlink()
        return True
    except OSError:
        return False


def run_health_check(config: Optional[AppConfig] = None) -> HealthReport:
    """시스템 헬스체크의 편의 함수.

    HealthChecker 인스턴스를 생성하고 run()을 호출한다.

    Args:
        config: 애플리케이션 설정. None이면 싱글턴에서 가져온다.

    Returns:
        전체 헬스체크 결과가 담긴 HealthReport
    """
    if config is None:
        from config import get_config
        config = get_config()

    checker = HealthChecker(config)
    return checker.run()
