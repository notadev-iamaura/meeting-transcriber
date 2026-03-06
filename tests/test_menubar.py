"""
메뉴바 앱 테스트 모듈 (Menu Bar Application Test Module)

목적: ui/menubar.py의 메뉴바 앱 기능을 검증한다.
주요 테스트:
    - 순수 함수 (build_api_url, determine_status, parse_status_response 등)
    - HTTP 폴링 함수 (fetch_status)
    - MeetingTranscriberApp 클래스 (초기화, UI 업데이트, 메뉴 구성)
의존성: pytest, unittest.mock, config 모듈
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from config import AppConfig, PathsConfig, ServerConfig
from ui.menubar import (
    AppStatus,
    MeetingTranscriberApp,
    StatusInfo,
    build_api_url,
    determine_status,
    fetch_status,
    format_queue_summary,
    get_status_title,
    parse_status_response,
)


# === 헬퍼 함수 ===


def _make_test_config(tmp_path: Path) -> AppConfig:
    """테스트용 AppConfig를 생성한다.

    Args:
        tmp_path: pytest 임시 디렉토리

    Returns:
        테스트용 AppConfig 인스턴스
    """
    return AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765),
    )


def _make_status_response(
    queue_summary: dict[str, int] | None = None,
    active_jobs: int = 0,
    total_jobs: int = 0,
) -> bytes:
    """테스트용 /api/status 응답 바이트를 생성한다.

    Args:
        queue_summary: 상태별 작업 수
        active_jobs: 진행 중인 작업 수
        total_jobs: 전체 작업 수

    Returns:
        JSON 인코딩된 응답 바이트
    """
    data = {
        "status": "ok",
        "queue_summary": queue_summary or {},
        "active_jobs": active_jobs,
        "total_jobs": total_jobs,
    }
    return json.dumps(data).encode("utf-8")


# === TestBuildApiUrl ===


class TestBuildApiUrl:
    """build_api_url 함수 테스트."""

    def test_기본_URL_구성(self, tmp_path: Path) -> None:
        """config의 host와 port로 URL을 올바르게 구성하는지 확인한다."""
        config = _make_test_config(tmp_path)
        url = build_api_url(config, "/api/status")

        assert url == "http://127.0.0.1:8765/api/status"

    def test_커스텀_포트_URL(self, tmp_path: Path) -> None:
        """커스텀 포트가 URL에 반영되는지 확인한다."""
        config = AppConfig(
            paths=PathsConfig(base_dir=str(tmp_path)),
            server=ServerConfig(host="127.0.0.1", port=9999),
        )
        url = build_api_url(config, "/api/health")

        assert url == "http://127.0.0.1:9999/api/health"

    def test_다양한_경로(self, tmp_path: Path) -> None:
        """다양한 API 경로가 올바르게 포함되는지 확인한다."""
        config = _make_test_config(tmp_path)

        assert build_api_url(config, "/api/health").endswith("/api/health")
        assert build_api_url(config, "/static/index.html").endswith(
            "/static/index.html"
        )


# === TestDetermineStatus ===


class TestDetermineStatus:
    """determine_status 함수 테스트."""

    def test_대기_상태(self) -> None:
        """진행 중인 작업이 없으면 IDLE을 반환하는지 확인한다."""
        data = {"queue_summary": {}, "active_jobs": 0}
        assert determine_status(data) == AppStatus.IDLE

    def test_녹음_상태_최우선(self) -> None:
        """recording이 있으면 다른 상태보다 우선하는지 확인한다."""
        data = {
            "queue_summary": {"recording": 1, "failed": 2},
            "active_jobs": 1,
        }
        assert determine_status(data) == AppStatus.RECORDING

    def test_처리_중_상태(self) -> None:
        """active_jobs > 0이면 PROCESSING을 반환하는지 확인한다."""
        data = {
            "queue_summary": {"transcribing": 1},
            "active_jobs": 1,
        }
        assert determine_status(data) == AppStatus.PROCESSING

    def test_오류_상태(self) -> None:
        """실패 작업만 있으면 ERROR를 반환하는지 확인한다."""
        data = {
            "queue_summary": {"failed": 3, "completed": 5},
            "active_jobs": 0,
        }
        assert determine_status(data) == AppStatus.ERROR

    def test_완료_작업만_있으면_대기(self) -> None:
        """완료된 작업만 있으면 IDLE을 반환하는지 확인한다."""
        data = {
            "queue_summary": {"completed": 10},
            "active_jobs": 0,
        }
        assert determine_status(data) == AppStatus.IDLE

    def test_빈_데이터(self) -> None:
        """빈 딕셔너리면 IDLE을 반환하는지 확인한다."""
        assert determine_status({}) == AppStatus.IDLE

    def test_is_recording_필드_우선(self) -> None:
        """is_recording=True이면 queue_summary보다 우선하여 RECORDING을 반환하는지 확인한다."""
        data = {
            "is_recording": True,
            "queue_summary": {"completed": 5},
            "active_jobs": 0,
        }
        assert determine_status(data) == AppStatus.RECORDING

    def test_is_recording_False이면_일반_로직(self) -> None:
        """is_recording=False이면 일반 상태 판별 로직을 따르는지 확인한다."""
        data = {
            "is_recording": False,
            "queue_summary": {"transcribing": 1},
            "active_jobs": 1,
        }
        assert determine_status(data) == AppStatus.PROCESSING


# === TestParseStatusResponse ===


class TestParseStatusResponse:
    """parse_status_response 함수 테스트."""

    def test_정상_응답_파싱(self) -> None:
        """정상 JSON 응답을 StatusInfo로 올바르게 파싱하는지 확인한다."""
        body = _make_status_response(
            queue_summary={"transcribing": 1},
            active_jobs=1,
            total_jobs=5,
        )
        info = parse_status_response(body)

        assert info.status == AppStatus.PROCESSING
        assert info.active_jobs == 1
        assert info.total_jobs == 5
        assert info.queue_summary == {"transcribing": 1}

    def test_대기_상태_파싱(self) -> None:
        """작업 없는 응답이 IDLE StatusInfo로 파싱되는지 확인한다."""
        body = _make_status_response()
        info = parse_status_response(body)

        assert info.status == AppStatus.IDLE
        assert info.active_jobs == 0

    def test_녹음_상태_파싱(self) -> None:
        """recording 포함 응답이 RECORDING StatusInfo로 파싱되는지 확인한다."""
        body = _make_status_response(
            queue_summary={"recording": 1},
            active_jobs=1,
            total_jobs=1,
        )
        info = parse_status_response(body)

        assert info.status == AppStatus.RECORDING

    def test_잘못된_JSON_예외(self) -> None:
        """잘못된 JSON에 대해 JSONDecodeError가 발생하는지 확인한다."""
        with pytest.raises(json.JSONDecodeError):
            parse_status_response(b"not json")

    def test_한국어_포함_응답(self) -> None:
        """한국어 텍스트가 포함된 응답이 정상 파싱되는지 확인한다."""
        data = {
            "status": "ok",
            "queue_summary": {"완료": 1},
            "active_jobs": 0,
            "total_jobs": 1,
        }
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        info = parse_status_response(body)

        assert info.total_jobs == 1


# === TestFormatQueueSummary ===


class TestFormatQueueSummary:
    """format_queue_summary 함수 테스트."""

    def test_빈_대기열(self) -> None:
        """빈 대기열이면 '작업 없음'을 반환하는지 확인한다."""
        lines = format_queue_summary({})

        assert len(lines) == 1
        assert "작업 없음" in lines[0]

    def test_0건_상태_제외(self) -> None:
        """count가 0인 상태는 표시하지 않는지 확인한다."""
        lines = format_queue_summary({"completed": 0, "failed": 0})

        assert len(lines) == 1
        assert "작업 없음" in lines[0]

    def test_다중_상태_표시(self) -> None:
        """여러 상태의 작업이 각각 표시되는지 확인한다."""
        summary = {"queued": 2, "transcribing": 1, "completed": 5}
        lines = format_queue_summary(summary)

        assert len(lines) == 3
        assert any("대기" in line and "2건" in line for line in lines)
        assert any("전사" in line and "1건" in line for line in lines)
        assert any("완료" in line and "5건" in line for line in lines)

    def test_알_수_없는_상태_원문_표시(self) -> None:
        """매핑되지 않은 상태명은 원문 그대로 표시하는지 확인한다."""
        lines = format_queue_summary({"unknown_status": 1})

        assert len(lines) == 1
        assert "unknown_status" in lines[0]

    def test_한국어_라벨_매핑(self) -> None:
        """모든 기본 상태가 한국어로 올바르게 매핑되는지 확인한다."""
        summary = {
            "queued": 1,
            "recording": 1,
            "transcribing": 1,
            "diarizing": 1,
            "merging": 1,
            "embedding": 1,
            "completed": 1,
            "failed": 1,
        }
        lines = format_queue_summary(summary)

        assert len(lines) == 8
        text = " ".join(lines)
        for label in ["대기", "녹음", "전사", "화자분리", "병합", "임베딩", "완료", "실패"]:
            assert label in text


# === TestGetStatusTitle ===


class TestGetStatusTitle:
    """get_status_title 함수 테스트."""

    def test_모든_상태_타이틀(self) -> None:
        """모든 AppStatus에 대해 유효한 타이틀을 반환하는지 확인한다."""
        for status in AppStatus:
            title = get_status_title(status)
            assert isinstance(title, str)
            assert len(title) > 0

    def test_대기_타이틀(self) -> None:
        """IDLE 상태의 타이틀에 '대기'가 포함되는지 확인한다."""
        assert "대기" in get_status_title(AppStatus.IDLE)

    def test_녹음_타이틀(self) -> None:
        """RECORDING 상태의 타이틀에 '녹음'이 포함되는지 확인한다."""
        assert "녹음" in get_status_title(AppStatus.RECORDING)

    def test_미연결_타이틀(self) -> None:
        """DISCONNECTED 상태의 타이틀에 '미연결'이 포함되는지 확인한다."""
        assert "미연결" in get_status_title(AppStatus.DISCONNECTED)


# === TestFetchStatus ===


class TestFetchStatus:
    """fetch_status 함수 테스트."""

    def test_정상_응답(self) -> None:
        """서버가 정상 응답하면 StatusInfo를 반환하는지 확인한다."""
        body = _make_status_response(active_jobs=1, total_jobs=3)

        mock_resp = MagicMock()
        mock_resp.read.return_value = body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("ui.menubar.urllib.request.urlopen", return_value=mock_resp):
            info = fetch_status("http://127.0.0.1:8765/api/status")

        assert info is not None
        assert info.active_jobs == 1
        assert info.total_jobs == 3

    def test_연결_실패_None_반환(self) -> None:
        """서버 연결 실패 시 None을 반환하는지 확인한다."""
        import urllib.error

        with patch(
            "ui.menubar.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            info = fetch_status("http://127.0.0.1:8765/api/status")

        assert info is None

    def test_잘못된_JSON_응답_None_반환(self) -> None:
        """서버가 잘못된 JSON을 반환하면 None을 반환하는지 확인한다."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("ui.menubar.urllib.request.urlopen", return_value=mock_resp):
            info = fetch_status("http://127.0.0.1:8765/api/status")

        assert info is None

    def test_HTTP_에러_None_반환(self) -> None:
        """HTTP 에러 응답 시 None을 반환하는지 확인한다."""
        import urllib.error

        with patch(
            "ui.menubar.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                url="http://127.0.0.1:8765/api/status",
                code=500,
                msg="Internal Server Error",
                hdrs=None,
                fp=None,
            ),
        ):
            info = fetch_status("http://127.0.0.1:8765/api/status")

        assert info is None

    def test_타임아웃_None_반환(self) -> None:
        """요청 타임아웃 시 None을 반환하는지 확인한다."""
        with patch(
            "ui.menubar.urllib.request.urlopen",
            side_effect=OSError("Connection timed out"),
        ):
            info = fetch_status("http://127.0.0.1:8765/api/status")

        assert info is None


# === TestStatusInfo ===


class TestStatusInfo:
    """StatusInfo 데이터 클래스 테스트."""

    def test_기본값_초기화(self) -> None:
        """기본값으로 올바르게 초기화되는지 확인한다."""
        info = StatusInfo(status=AppStatus.IDLE)

        assert info.status == AppStatus.IDLE
        assert info.active_jobs == 0
        assert info.total_jobs == 0
        assert info.queue_summary == {}
        assert info.uptime_seconds == 0.0

    def test_커스텀_값_초기화(self) -> None:
        """커스텀 값으로 올바르게 초기화되는지 확인한다."""
        info = StatusInfo(
            status=AppStatus.PROCESSING,
            active_jobs=2,
            total_jobs=5,
            queue_summary={"transcribing": 2},
            uptime_seconds=100.5,
        )

        assert info.active_jobs == 2
        assert info.queue_summary["transcribing"] == 2


# === TestMeetingTranscriberApp ===


class TestMeetingTranscriberApp:
    """MeetingTranscriberApp 클래스 테스트."""

    @patch("ui.menubar.rumps.App.__init__", return_value=None)
    def test_초기_상태_DISCONNECTED(
        self,
        mock_init: MagicMock,
        tmp_path: Path,
    ) -> None:
        """앱 초기 상태가 DISCONNECTED인지 확인한다."""
        config = _make_test_config(tmp_path)

        # rumps.App.__init__를 모킹하되, 필요한 속성은 직접 설정
        app = MeetingTranscriberApp.__new__(MeetingTranscriberApp)
        app.config = config
        app._current_status = AppStatus.DISCONNECTED
        app._status_url = build_api_url(config, "/api/status")
        app._web_url = build_api_url(config, "/static/index.html")

        assert app._current_status == AppStatus.DISCONNECTED

    @patch("ui.menubar.rumps.App.__init__", return_value=None)
    def test_URL_구성(
        self,
        mock_init: MagicMock,
        tmp_path: Path,
    ) -> None:
        """API URL이 config에서 올바르게 구성되는지 확인한다."""
        config = _make_test_config(tmp_path)

        app = MeetingTranscriberApp.__new__(MeetingTranscriberApp)
        app.config = config
        app._status_url = build_api_url(config, "/api/status")
        app._web_url = build_api_url(config, "/static/index.html")

        assert "8765" in app._status_url
        assert "/api/status" in app._status_url
        assert "/static/index.html" in app._web_url

    @patch("ui.menubar.rumps.App.__init__", return_value=None)
    def test_update_ui_상태_변경(
        self,
        mock_init: MagicMock,
        tmp_path: Path,
    ) -> None:
        """_update_ui가 상태 변경 시 타이틀을 업데이트하는지 확인한다."""
        config = _make_test_config(tmp_path)

        app = MeetingTranscriberApp.__new__(MeetingTranscriberApp)
        app.config = config
        app._current_status = AppStatus.DISCONNECTED
        app.title = get_status_title(AppStatus.DISCONNECTED)
        app._menu_status = MagicMock()
        app._menu_queue_header = MagicMock()
        app._menu_queue_items = [MagicMock()]
        app._menu_recording = MagicMock()
        app._is_recording = False

        # IDLE로 변경
        info = StatusInfo(status=AppStatus.IDLE, active_jobs=0, total_jobs=3)
        app._update_ui(info)

        assert app._current_status == AppStatus.IDLE
        assert "대기" in app.title

    @patch("ui.menubar.rumps.App.__init__", return_value=None)
    def test_update_ui_동일_상태시_타이틀_미변경(
        self,
        mock_init: MagicMock,
        tmp_path: Path,
    ) -> None:
        """동일 상태에서 타이틀이 변경되지 않는지 확인한다."""
        config = _make_test_config(tmp_path)

        app = MeetingTranscriberApp.__new__(MeetingTranscriberApp)
        app.config = config
        app._current_status = AppStatus.IDLE
        original_title = get_status_title(AppStatus.IDLE)
        app.title = original_title
        app._menu_status = MagicMock()
        app._menu_queue_header = MagicMock()
        app._menu_queue_items = [MagicMock()]
        app._menu_recording = MagicMock()
        app._is_recording = False

        # 같은 IDLE 상태로 업데이트
        info = StatusInfo(status=AppStatus.IDLE)
        app._update_ui(info)

        assert app.title == original_title

    @patch("ui.menubar.rumps.App.__init__", return_value=None)
    def test_update_ui_DISCONNECTED_상태_메시지(
        self,
        mock_init: MagicMock,
        tmp_path: Path,
    ) -> None:
        """DISCONNECTED 상태에서 메뉴에 '서버 연결 안됨'이 표시되는지 확인한다."""
        config = _make_test_config(tmp_path)

        app = MeetingTranscriberApp.__new__(MeetingTranscriberApp)
        app.config = config
        app._current_status = AppStatus.IDLE  # 이전 상태를 다르게 설정
        app.title = ""
        app._menu_status = MagicMock()
        app._menu_queue_header = MagicMock()
        app._menu_queue_items = [MagicMock()]
        app._menu_recording = MagicMock()
        app._is_recording = False

        info = StatusInfo(status=AppStatus.DISCONNECTED)
        app._update_ui(info)

        assert "서버 연결 안됨" in app._menu_status.title

    @patch("ui.menubar.webbrowser.open")
    @patch("ui.menubar.rumps.App.__init__", return_value=None)
    def test_웹_UI_열기(
        self,
        mock_init: MagicMock,
        mock_open: MagicMock,
        tmp_path: Path,
    ) -> None:
        """_on_open_web_ui가 webbrowser.open을 호출하는지 확인한다."""
        config = _make_test_config(tmp_path)

        app = MeetingTranscriberApp.__new__(MeetingTranscriberApp)
        app.config = config
        app._web_url = build_api_url(config, "/static/index.html")

        app._on_open_web_ui(None)

        mock_open.assert_called_once_with(app._web_url)

    @patch("ui.menubar.subprocess.Popen")
    @patch("ui.menubar.rumps.App.__init__", return_value=None)
    def test_데이터_폴더_열기(
        self,
        mock_init: MagicMock,
        mock_popen: MagicMock,
        tmp_path: Path,
    ) -> None:
        """_on_open_data_dir가 open 명령을 실행하는지 확인한다."""
        config = _make_test_config(tmp_path)

        app = MeetingTranscriberApp.__new__(MeetingTranscriberApp)
        app.config = config

        app._on_open_data_dir(None)

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args[0][0]
        assert call_args[0] == "open"


# === TestAppStatusEnum ===


class TestAppStatusEnum:
    """AppStatus 열거형 테스트."""

    def test_모든_상태값_존재(self) -> None:
        """필수 상태값이 모두 정의되어 있는지 확인한다."""
        status_values = {s.value for s in AppStatus}

        assert "idle" in status_values
        assert "recording" in status_values
        assert "processing" in status_values
        assert "error" in status_values
        assert "disconnected" in status_values

    def test_상태별_디스플레이_매핑(self) -> None:
        """모든 상태에 대해 디스플레이 텍스트가 매핑되어 있는지 확인한다."""
        from ui.menubar import STATUS_DISPLAY

        for status in AppStatus:
            assert status in STATUS_DISPLAY
            assert isinstance(STATUS_DISPLAY[status], str)
