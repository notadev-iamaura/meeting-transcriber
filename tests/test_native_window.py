"""
네이티브 창 모듈 테스트 (Native Window Module Tests)

목적: ui/native_window.py의 PyWebView 네이티브 창 기능을 검증한다.
주요 테스트:
    - NativeWindowConfig 데이터클래스 기본값 및 불변성
    - build_window_config URL 생성
    - build_subprocess_args 인자 구성
    - launch_native_window 서브프로세스 실행
    - run_webview_window webview 호출
    - WindowConfig Pydantic 모델 (config.py)
의존성: pytest, unittest.mock
"""

from __future__ import annotations

import runpy
import subprocess
import sys
import warnings
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, patch

import pytest

# === TestNativeWindowConfig ===


class TestNativeWindowConfig:
    """NativeWindowConfig 데이터클래스 테스트."""

    def test_기본값(self) -> None:
        """기본값이 올바르게 설정되는지 확인한다."""
        from ui.native_window import NativeWindowConfig

        config = NativeWindowConfig(url="http://localhost:8765/app")

        assert config.url == "http://localhost:8765/app"
        assert config.title == "Recap"
        assert config.width == 1200
        assert config.height == 800
        assert config.min_width == 800
        assert config.min_height == 600

    def test_커스텀_값(self) -> None:
        """모든 필드에 커스텀 값이 반영되는지 확인한다."""
        from ui.native_window import NativeWindowConfig

        config = NativeWindowConfig(
            url="http://127.0.0.1:9999/app",
            title="테스트 앱",
            width=1400,
            height=900,
            min_width=600,
            min_height=400,
        )

        assert config.url == "http://127.0.0.1:9999/app"
        assert config.title == "테스트 앱"
        assert config.width == 1400
        assert config.height == 900
        assert config.min_width == 600
        assert config.min_height == 400

    def test_불변_객체(self) -> None:
        """frozen=True로 속성 변경이 불가능한지 확인한다."""
        from ui.native_window import NativeWindowConfig

        config = NativeWindowConfig(url="http://localhost:8765/app")

        with pytest.raises(FrozenInstanceError):
            config.url = "http://other:1234/app"  # type: ignore[misc]


# === TestBuildWindowConfig ===


class TestBuildWindowConfig:
    """build_window_config 함수 테스트."""

    def test_URL_생성(self) -> None:
        """host와 port로 올바른 URL이 생성되는지 확인한다."""
        from ui.native_window import build_window_config

        config = build_window_config(
            host="127.0.0.1",
            port=8765,
            title="Recap",
            width=1200,
            height=800,
            min_width=800,
            min_height=600,
        )

        assert config.url == "http://127.0.0.1:8765/app"

    def test_커스텀_포트(self) -> None:
        """커스텀 포트가 URL에 반영되는지 확인한다."""
        from ui.native_window import build_window_config

        config = build_window_config(
            host="127.0.0.1",
            port=9999,
            title="테스트",
            width=1200,
            height=800,
            min_width=800,
            min_height=600,
        )

        assert config.url == "http://127.0.0.1:9999/app"

    def test_커스텀_크기(self) -> None:
        """커스텀 크기가 config에 반영되는지 확인한다."""
        from ui.native_window import build_window_config

        config = build_window_config(
            host="127.0.0.1",
            port=8765,
            title="큰 창",
            width=1600,
            height=1000,
            min_width=1024,
            min_height=768,
        )

        assert config.width == 1600
        assert config.height == 1000
        assert config.min_width == 1024
        assert config.min_height == 768
        assert config.title == "큰 창"


# === TestBuildSubprocessArgs ===


class TestBuildSubprocessArgs:
    """build_subprocess_args 함수 테스트."""

    def test_인자_구조(self) -> None:
        """서브프로세스 인자가 올바른 구조를 갖는지 확인한다."""
        from ui.native_window import NativeWindowConfig, build_subprocess_args

        config = NativeWindowConfig(url="http://localhost:8765/app")
        args = build_subprocess_args(config)

        assert args[0] == sys.executable
        assert args[1] == "-m"
        assert args[2] == "ui.native_window"

    def test_모든_파라미터_포함(self) -> None:
        """모든 파라미터가 인자 목록에 포함되는지 확인한다."""
        from ui.native_window import NativeWindowConfig, build_subprocess_args

        config = NativeWindowConfig(
            url="http://localhost:8765/app",
            title="테스트",
            width=1400,
            height=900,
            min_width=600,
            min_height=400,
        )
        args = build_subprocess_args(config)
        joined = " ".join(args)

        assert "--url" in joined
        assert "http://localhost:8765/app" in joined
        assert "--title" in joined
        assert "테스트" in joined
        assert "--width" in joined
        assert "1400" in joined
        assert "--height" in joined
        assert "900" in joined
        assert "--min-width" in joined
        assert "600" in joined
        assert "--min-height" in joined
        assert "400" in joined


# === TestLaunchNativeWindow ===


class TestLaunchNativeWindow:
    """launch_native_window 함수 테스트."""

    def test_서브프로세스_실행(self) -> None:
        """subprocess.Popen이 호출되는지 확인한다."""
        from ui.native_window import NativeWindowConfig, launch_native_window

        config = NativeWindowConfig(url="http://localhost:8765/app")

        with patch("ui.native_window.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            launch_native_window(config)

            mock_popen.assert_called_once()

    def test_프로세스_반환(self) -> None:
        """Popen 인스턴스가 반환되는지 확인한다."""
        from ui.native_window import NativeWindowConfig, launch_native_window

        config = NativeWindowConfig(url="http://localhost:8765/app")
        mock_process = MagicMock(spec=subprocess.Popen)

        with patch("ui.native_window.subprocess.Popen", return_value=mock_process):
            result = launch_native_window(config)

        assert result is mock_process

    def test_실패시_예외_전파(self) -> None:
        """서브프로세스 실행 실패 시 OSError가 전파되는지 확인한다."""
        from ui.native_window import NativeWindowConfig, launch_native_window

        config = NativeWindowConfig(url="http://localhost:8765/app")

        with (
            patch(
                "ui.native_window.subprocess.Popen",
                side_effect=OSError("실행 불가"),
            ),
            pytest.raises(OSError, match="실행 불가"),
        ):
            launch_native_window(config)


# === TestRunWebviewWindow ===


class TestRunWebviewWindow:
    """run_webview_window 함수 테스트."""

    def test_webview_호출(self) -> None:
        """webview.create_window과 webview.start가 호출되는지 확인한다."""
        mock_webview = MagicMock()
        mock_window = MagicMock()
        mock_webview.create_window.return_value = mock_window

        with patch.dict("sys.modules", {"webview": mock_webview}):
            # 모듈 캐시를 우회하기 위해 함수를 직접 임포트
            from importlib import reload

            import ui.native_window

            reload(ui.native_window)

            ui.native_window.run_webview_window(
                url="http://localhost:8765/app",
                title="테스트",
                width=1200,
                height=800,
                min_width=800,
                min_height=600,
            )

            mock_webview.create_window.assert_called_once_with(
                "테스트",
                "http://localhost:8765/app",
                width=1200,
                height=800,
                min_size=(800, 600),
            )
            mock_webview.start.assert_called_once()


# === TestWindowConfigModel ===


class TestWindowConfigModel:
    """config.py의 WindowConfig Pydantic 모델 테스트."""

    def test_기본값(self) -> None:
        """WindowConfig 기본값이 올바른지 확인한다."""
        from config import WindowConfig

        config = WindowConfig()

        assert config.title == "Recap"
        assert config.width == 1200
        assert config.height == 800
        assert config.min_width == 800
        assert config.min_height == 600
        assert config.use_native is True

    def test_AppConfig_통합(self) -> None:
        """AppConfig에서 window 필드에 접근 가능한지 확인한다."""
        from config import AppConfig

        app_config = AppConfig()

        assert hasattr(app_config, "window")
        assert app_config.window.title == "Recap"
        assert app_config.window.use_native is True


# === TestBoundaryValues ===


class TestBoundaryValues:
    """NativeWindowConfig 경계값 테스트.

    frozen 데이터클래스는 값 검증을 수행하지 않으므로,
    경계값이 그대로 허용됨을 문서화한다.
    """

    def test_width_0(self) -> None:
        """width=0이 검증 없이 허용되는지 확인한다."""
        from ui.native_window import NativeWindowConfig

        config = NativeWindowConfig(url="http://test:8080/app", width=0)

        assert config.width == 0

    def test_height_음수(self) -> None:
        """height=-1이 검증 없이 허용되는지 확인한다."""
        from ui.native_window import NativeWindowConfig

        config = NativeWindowConfig(url="http://test:8080/app", height=-1)

        assert config.height == -1

    def test_min_width_초과_width(self) -> None:
        """min_width > width인 경우도 검증 없이 허용되는지 확인한다."""
        from ui.native_window import NativeWindowConfig

        config = NativeWindowConfig(
            url="http://test:8080/app",
            min_width=1000,
            width=800,
        )

        assert config.min_width == 1000
        assert config.width == 800

    def test_빈_URL(self) -> None:
        """빈 문자열 URL이 검증 없이 허용되는지 확인한다."""
        from ui.native_window import NativeWindowConfig

        config = NativeWindowConfig(url="")

        assert config.url == ""

    def test_빈_title(self) -> None:
        """빈 문자열 title이 검증 없이 허용되는지 확인한다."""
        from ui.native_window import NativeWindowConfig

        config = NativeWindowConfig(url="http://test", title="")

        assert config.title == ""


# === TestMainBlock ===


class TestMainBlock:
    """__main__ 블록 및 _parse_args 함수 테스트."""

    def test_parse_args_기본값(self) -> None:
        """_parse_args가 필수 인자(url)를 파싱하고 기본값을 설정하는지 확인한다."""
        from ui.native_window import _parse_args

        with patch("sys.argv", ["native_window", "--url", "http://test:8080/app"]):
            args = _parse_args()

        assert args.url == "http://test:8080/app"
        assert args.title == "Recap"
        assert args.width == 1200
        assert args.height == 800
        assert args.min_width == 800
        assert args.min_height == 600

    def test_parse_args_커스텀_인자(self) -> None:
        """_parse_args가 커스텀 인자를 올바르게 파싱하는지 확인한다."""
        from ui.native_window import _parse_args

        with patch(
            "sys.argv",
            [
                "native_window",
                "--url",
                "http://localhost:9999/app",
                "--title",
                "커스텀 제목",
                "--width",
                "1600",
                "--height",
                "1000",
                "--min-width",
                "1024",
                "--min-height",
                "768",
            ],
        ):
            args = _parse_args()

        assert args.url == "http://localhost:9999/app"
        assert args.title == "커스텀 제목"
        assert args.width == 1600
        assert args.height == 1000
        assert args.min_width == 1024
        assert args.min_height == 768

    def test_main_모듈_실행(self) -> None:
        """__main__ 블록이 인자를 파싱하고 run_webview_window를 호출하는지 확인한다."""
        mock_webview = MagicMock()

        test_argv = [
            "native_window",
            "--url",
            "http://test:8080/app",
            "--title",
            "테스트",
            "--width",
            "1200",
            "--height",
            "800",
            "--min-width",
            "800",
            "--min-height",
            "600",
        ]

        with (
            patch("sys.argv", test_argv),
            patch.dict("sys.modules", {"webview": mock_webview}),
            warnings.catch_warnings(),
        ):
            warnings.filterwarnings(
                "ignore",
                message="'ui.native_window' found in sys.modules",
                category=RuntimeWarning,
            )
            # runpy로 __main__ 블록 실행 (새 네임스페이스에서 실행됨)
            runpy.run_module("ui.native_window", run_name="__main__")

            # webview.create_window과 webview.start가 호출되었는지 확인
            mock_webview.create_window.assert_called_once_with(
                "테스트",
                "http://test:8080/app",
                width=1200,
                height=800,
                min_size=(800, 600),
            )
            mock_webview.start.assert_called_once()
