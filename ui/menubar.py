"""
rumps 메뉴바 앱 모듈 (macOS Menu Bar Application Module)

목적: macOS 메뉴바에 회의 전사 시스템 상태를 표시하고
      웹 UI 접근, 상태 모니터링, 기본 제어를 제공한다.
주요 기능:
    - 상태 아이콘 표시 (대기/녹음/처리중/오류/미연결)
    - 대기열 현황 메뉴 표시
    - 웹 UI 열기 (webbrowser.open)
    - HTTP 폴링(3초)으로 FastAPI 서버 상태 조회
    - 데이터 폴더 열기, 서버 정보 표시
의존성: rumps, config 모듈
"""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import rumps

from config import AppConfig, get_config

logger = logging.getLogger(__name__)


# === 상태 정의 ===


class AppStatus(Enum):
    """메뉴바 앱 상태를 정의하는 열거형.

    Attributes:
        IDLE: 대기 중 (서버 정상, 진행 중인 작업 없음)
        RECORDING: 녹음 중
        PROCESSING: 처리 중 (전사/분리/병합/임베딩)
        ERROR: 오류 발생
        DISCONNECTED: 서버 연결 안됨
    """

    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"
    ERROR = "error"
    DISCONNECTED = "disconnected"


# 상태별 메뉴바 표시 텍스트
STATUS_DISPLAY: dict[AppStatus, str] = {
    AppStatus.IDLE: "🎙️ 대기",
    AppStatus.RECORDING: "🔴 녹음",
    AppStatus.PROCESSING: "⚙️ 처리",
    AppStatus.ERROR: "⚠️ 오류",
    AppStatus.DISCONNECTED: "❌ 미연결",
}

# 폴링 간격 (초)
POLL_INTERVAL_SECONDS = 3

# HTTP 요청 타임아웃 (초)
HTTP_TIMEOUT_SECONDS = 5

# 대기열 상태명 → 한국어 매핑
_QUEUE_STATUS_LABELS: dict[str, str] = {
    "queued": "대기",
    "recording": "녹음",
    "transcribing": "전사",
    "diarizing": "화자분리",
    "merging": "병합",
    "embedding": "임베딩",
    "completed": "완료",
    "failed": "실패",
}


# === 데이터 클래스 ===


@dataclass
class StatusInfo:
    """서버 상태 정보를 담는 데이터 클래스.

    Attributes:
        status: 앱 상태
        active_jobs: 진행 중인 작업 수
        total_jobs: 전체 작업 수
        queue_summary: 상태별 작업 수
        uptime_seconds: 서버 가동 시간 (초)
    """

    status: AppStatus
    active_jobs: int = 0
    total_jobs: int = 0
    queue_summary: dict[str, int] = field(default_factory=dict)
    uptime_seconds: float = 0.0


# === 순수 함수 (비즈니스 로직, 테스트 가능) ===


def build_api_url(config: AppConfig, path: str) -> str:
    """config에서 API 엔드포인트 URL을 구성한다.

    Args:
        config: 앱 설정
        path: API 경로 (예: "/api/status")

    Returns:
        완전한 URL 문자열 (예: "http://127.0.0.1:8765/api/status")
    """
    host = config.server.host
    port = config.server.port
    return f"http://{host}:{port}{path}"


def determine_status(status_data: dict[str, Any]) -> AppStatus:
    """서버 응답 데이터에서 앱 상태를 결정한다.

    우선순위: 녹음 > 처리 중 > 오류 > 대기

    Args:
        status_data: /api/status 응답 JSON 딕셔너리

    Returns:
        결정된 AppStatus
    """
    queue_summary = status_data.get("queue_summary", {})
    active_jobs = status_data.get("active_jobs", 0)

    # 녹음 상태 최우선
    if queue_summary.get("recording", 0) > 0:
        return AppStatus.RECORDING

    # 처리 중 (전사/분리/병합/임베딩 등)
    if active_jobs > 0:
        return AppStatus.PROCESSING

    # 실패 작업 존재
    if queue_summary.get("failed", 0) > 0:
        return AppStatus.ERROR

    return AppStatus.IDLE


def parse_status_response(response_body: bytes) -> StatusInfo:
    """서버 응답 바이트를 StatusInfo로 파싱한다.

    Args:
        response_body: HTTP 응답 본문 (bytes)

    Returns:
        파싱된 StatusInfo

    Raises:
        json.JSONDecodeError: JSON 파싱 실패 시
        KeyError: 필수 필드 누락 시
    """
    data = json.loads(response_body.decode("utf-8"))

    app_status = determine_status(data)

    return StatusInfo(
        status=app_status,
        active_jobs=data.get("active_jobs", 0),
        total_jobs=data.get("total_jobs", 0),
        queue_summary=data.get("queue_summary", {}),
        uptime_seconds=data.get("uptime_seconds", 0.0),
    )


def format_queue_summary(queue_summary: dict[str, int]) -> list[str]:
    """대기열 요약을 메뉴 표시용 문자열 리스트로 변환한다.

    Args:
        queue_summary: 상태별 작업 수 딕셔너리

    Returns:
        표시용 문자열 리스트 (예: ["  대기: 2건", "  처리 중: 1건"])
    """
    lines: list[str] = []
    for status_key, count in queue_summary.items():
        if count > 0:
            label = _QUEUE_STATUS_LABELS.get(status_key, status_key)
            lines.append(f"  {label}: {count}건")

    if not lines:
        lines.append("  작업 없음")

    return lines


def get_status_title(status: AppStatus) -> str:
    """AppStatus에 해당하는 메뉴바 타이틀을 반환한다.

    Args:
        status: 현재 앱 상태

    Returns:
        메뉴바에 표시할 문자열
    """
    return STATUS_DISPLAY.get(status, "❓ 알 수 없음")


# === HTTP 폴링 ===


def fetch_status(
    api_url: str,
    timeout: int = HTTP_TIMEOUT_SECONDS,
) -> Optional[StatusInfo]:
    """FastAPI 서버에서 상태를 가져온다.

    Args:
        api_url: /api/status 전체 URL
        timeout: HTTP 요청 타임아웃 (초)

    Returns:
        StatusInfo 또는 연결 실패 시 None
    """
    try:
        req = urllib.request.Request(api_url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return parse_status_response(body)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        logger.debug(f"서버 상태 조회 실패: {e}")
        return None
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(f"서버 응답 파싱 실패: {e}")
        return None


# === rumps 메뉴바 앱 ===


class MeetingTranscriberApp(rumps.App):
    """macOS 메뉴바 회의 전사 앱.

    rumps.App을 상속하여 메뉴바에서 시스템 상태를 표시하고
    웹 UI 접근 및 기본 제어 기능을 제공한다.

    Attributes:
        config: 앱 설정 (AppConfig)
        _current_status: 현재 앱 상태
        _status_url: 상태 조회 API URL
        _web_url: 웹 UI URL
        _menu_status: 상태 표시 메뉴 아이템
        _menu_queue_info: 대기열 정보 메뉴 아이템
    """

    def __init__(self, config: Optional[AppConfig] = None) -> None:
        """메뉴바 앱을 초기화한다.

        Args:
            config: 앱 설정. None이면 config.yaml에서 로드.
        """
        self.config = config or get_config()
        self._current_status = AppStatus.DISCONNECTED

        # API/웹 URL 구성
        self._status_url = build_api_url(self.config, "/api/status")
        self._web_url = build_api_url(self.config, "/static/index.html")

        # 초기 타이틀 설정
        initial_title = get_status_title(self._current_status)

        super().__init__(
            name="회의 전사",
            title=initial_title,
            quit_button=None,  # 커스텀 종료 버튼 사용
        )

        # 메뉴 구성
        self._setup_menu()

        logger.info(
            f"메뉴바 앱 초기화 — "
            f"상태 URL: {self._status_url}, "
            f"웹 URL: {self._web_url}"
        )

    def _setup_menu(self) -> None:
        """메뉴 아이템을 구성한다."""
        # 상태 표시 (정보용, 콜백 없음)
        self._menu_status = rumps.MenuItem(
            "상태: 서버 연결 확인 중...",
        )

        # 대기열 정보 (정보용)
        self._menu_queue_header = rumps.MenuItem("📋 대기열")
        self._menu_queue_info = rumps.MenuItem("  정보 로딩 중...")

        # 메뉴 구성
        self.menu = [
            self._menu_status,
            None,  # 구분선
            self._menu_queue_header,
            self._menu_queue_info,
            None,  # 구분선
            rumps.MenuItem(
                "🌐 웹 UI 열기",
                callback=self._on_open_web_ui,
            ),
            None,  # 구분선
            rumps.MenuItem(
                "📁 데이터 폴더 열기",
                callback=self._on_open_data_dir,
            ),
            rumps.MenuItem(
                "ℹ️ 서버 정보",
                callback=self._on_show_info,
            ),
            None,  # 구분선
            rumps.MenuItem("종료", callback=self._on_quit),
        ]

    @rumps.timer(POLL_INTERVAL_SECONDS)
    def _poll_status(self, _sender: Any) -> None:
        """3초 주기로 FastAPI 서버 상태를 폴링한다.

        Args:
            _sender: rumps 타이머 콜백 발신자 (사용 안함)
        """
        try:
            status_info = fetch_status(self._status_url)

            if status_info is None:
                self._update_ui(StatusInfo(status=AppStatus.DISCONNECTED))
            else:
                self._update_ui(status_info)

        except Exception as e:
            # 타이머 콜백 예외가 앱을 죽이지 않도록 격리
            logger.error(
                f"상태 폴링 중 예상치 못한 오류: {e}",
                exc_info=True,
            )
            self._update_ui(StatusInfo(status=AppStatus.ERROR))

    def _update_ui(self, info: StatusInfo) -> None:
        """UI를 상태 정보에 맞게 업데이트한다.

        Args:
            info: 서버 상태 정보
        """
        # 상태 변경 시에만 타이틀 업데이트 (불필요한 렌더링 방지)
        if info.status != self._current_status:
            self._current_status = info.status
            self.title = get_status_title(info.status)
            logger.info(f"메뉴바 상태 변경: {info.status.value}")

        # 상태 메뉴 텍스트 업데이트
        if info.status == AppStatus.DISCONNECTED:
            self._menu_status.title = "상태: 서버 연결 안됨"
        else:
            status_text = STATUS_DISPLAY.get(info.status, "알 수 없음")
            self._menu_status.title = (
                f"상태: {status_text} "
                f"(작업 {info.active_jobs}/{info.total_jobs})"
            )

        # 대기열 메뉴 업데이트
        lines = format_queue_summary(info.queue_summary)
        self._menu_queue_info.title = " | ".join(lines)

    def _on_open_web_ui(self, _sender: Any) -> None:
        """웹 UI를 기본 브라우저로 연다.

        Args:
            _sender: 메뉴 아이템 콜백 발신자 (사용 안함)
        """
        logger.info(f"웹 UI 열기: {self._web_url}")
        webbrowser.open(self._web_url)

    def _on_open_data_dir(self, _sender: Any) -> None:
        """데이터 디렉토리를 Finder로 연다.

        Args:
            _sender: 메뉴 아이템 콜백 발신자 (사용 안함)
        """
        data_dir = self.config.paths.resolved_base_dir
        logger.info(f"데이터 폴더 열기: {data_dir}")

        try:
            subprocess.Popen(["open", str(data_dir)])
        except OSError as e:
            logger.error(f"데이터 폴더 열기 실패: {e}")
            rumps.alert(
                title="오류",
                message=f"데이터 폴더를 열 수 없습니다: {e}",
            )

    def _on_show_info(self, _sender: Any) -> None:
        """서버 정보를 알림으로 표시한다.

        Args:
            _sender: 메뉴 아이템 콜백 발신자 (사용 안함)
        """
        host = self.config.server.host
        port = self.config.server.port

        rumps.alert(
            title="회의 전사 시스템",
            message=(
                f"서버: http://{host}:{port}\n"
                f"데이터: {self.config.paths.resolved_base_dir}\n"
                f"상태: {self._current_status.value}"
            ),
        )

    def _on_quit(self, _sender: Any) -> None:
        """앱을 종료한다.

        Args:
            _sender: 메뉴 아이템 콜백 발신자 (사용 안함)
        """
        logger.info("메뉴바 앱 종료 요청")
        rumps.quit_application()


# === 진입점 함수 ===


def run_menubar(config: Optional[AppConfig] = None) -> None:
    """메뉴바 앱을 실행한다.

    이 함수는 메인 스레드에서 호출되어야 한다.
    rumps.App.run()은 macOS 이벤트 루프를 실행하므로
    이 호출은 앱 종료 시까지 블록된다.

    Args:
        config: 앱 설정. None이면 config.yaml에서 로드.
    """
    app = MeetingTranscriberApp(config=config)
    app.run()


# 직접 실행 시 (개발 테스트용)
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    run_menubar()
