"""
main.py 단위 테스트 모듈 (Main Entry Point Unit Tests)

목적: 앱 진입점(main.py)의 CLI 파싱, 로깅 설정, 설정 로드,
      데이터 디렉토리 초기화, 서버 스레드 시작, 메인 함수를 검증한다.
의존성: pytest, unittest.mock
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 테스트 대상 모듈은 sys.path에 meeting-transcriber가 포함된 상태에서 import
from main import (
    _ensure_minimal_dirs,
    _setup_signal_handlers,
    ensure_data_directories,
    load_config_with_overrides,
    main,
    parse_args,
    setup_logging,
    start_server_thread,
)


# === TestParseArgs ===


class TestParseArgs:
    """커맨드라인 인자 파싱 테스트."""

    def test_기본값(self) -> None:
        """인자 없이 호출 시 기본값을 반환한다."""
        args = parse_args([])

        assert args.config is None
        assert args.host is None
        assert args.port is None
        assert args.log_level is None
        assert args.log_file is None
        assert args.no_menubar is False

    def test_모든_인자_지정(self, tmp_path: Path) -> None:
        """모든 인자를 지정하면 올바르게 파싱한다."""
        config_path = tmp_path / "test.yaml"
        log_path = tmp_path / "test.log"

        args = parse_args([
            "--config", str(config_path),
            "--host", "0.0.0.0",
            "--port", "9000",
            "--log-level", "debug",
            "--log-file", str(log_path),
            "--no-menubar",
        ])

        assert args.config == config_path
        assert args.host == "0.0.0.0"
        assert args.port == 9000
        assert args.log_level == "debug"
        assert args.log_file == log_path
        assert args.no_menubar is True

    def test_포트_정수_변환(self) -> None:
        """--port 값은 정수로 변환된다."""
        args = parse_args(["--port", "8080"])
        assert isinstance(args.port, int)
        assert args.port == 8080

    def test_잘못된_로그레벨(self) -> None:
        """허용되지 않은 로그 레벨은 에러를 발생시킨다."""
        with pytest.raises(SystemExit):
            parse_args(["--log-level", "verbose"])

    def test_no_menubar_플래그(self) -> None:
        """--no-menubar 플래그 동작을 확인한다."""
        args_with = parse_args(["--no-menubar"])
        args_without = parse_args([])

        assert args_with.no_menubar is True
        assert args_without.no_menubar is False


# === TestSetupLogging ===


class TestSetupLogging:
    """로깅 설정 테스트."""

    def teardown_method(self) -> None:
        """각 테스트 후 루트 로거 핸들러를 정리한다."""
        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(logging.WARNING)

    def test_기본_콘솔_핸들러(self) -> None:
        """기본 설정 시 콘솔 핸들러만 추가된다."""
        setup_logging(level="info")

        root = logging.getLogger()
        assert root.level == logging.INFO
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], logging.StreamHandler)

    def test_디버그_레벨(self) -> None:
        """debug 레벨 설정을 확인한다."""
        setup_logging(level="debug")

        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_파일_핸들러_추가(self, tmp_path: Path) -> None:
        """log_file 지정 시 파일 핸들러가 추가된다."""
        log_file = tmp_path / "logs" / "app.log"
        setup_logging(level="info", log_file=log_file)

        root = logging.getLogger()
        # 콘솔(1) + 파일(1) = 2개 핸들러
        assert len(root.handlers) == 2
        # 로그 디렉토리 자동 생성 확인
        assert log_file.parent.exists()

    def test_중복_호출_시_핸들러_초기화(self) -> None:
        """setup_logging을 재호출하면 기존 핸들러를 제거하고 새로 추가한다."""
        setup_logging(level="info")
        setup_logging(level="debug")

        root = logging.getLogger()
        # 핸들러 중복 없이 1개만
        assert len(root.handlers) == 1
        assert root.level == logging.DEBUG

    def test_uvicorn_로거_레벨_동기화(self) -> None:
        """uvicorn 관련 로거의 레벨도 동기화된다."""
        setup_logging(level="warning")

        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            uv_logger = logging.getLogger(name)
            assert uv_logger.level == logging.WARNING


# === TestLoadConfigWithOverrides ===


class TestLoadConfigWithOverrides:
    """설정 로드 및 CLI 오버라이드 테스트."""

    def test_기본_설정_로드(self) -> None:
        """오버라이드 없이 기본 설정을 로드한다."""
        config = load_config_with_overrides()

        assert config.server.host == "127.0.0.1"
        assert config.server.port == 8765

    def test_호스트_오버라이드(self) -> None:
        """host 인자로 서버 호스트를 변경한다."""
        config = load_config_with_overrides(host="0.0.0.0")
        assert config.server.host == "0.0.0.0"

    def test_포트_오버라이드(self) -> None:
        """port 인자로 서버 포트를 변경한다."""
        config = load_config_with_overrides(port=9000)
        assert config.server.port == 9000

    def test_로그레벨_오버라이드(self) -> None:
        """log_level 인자로 로그 레벨을 변경한다."""
        config = load_config_with_overrides(log_level="debug")
        assert config.server.log_level == "debug"

    def test_복합_오버라이드(self) -> None:
        """여러 인자를 동시에 오버라이드한다."""
        config = load_config_with_overrides(
            host="0.0.0.0",
            port=9000,
            log_level="warning",
        )

        assert config.server.host == "0.0.0.0"
        assert config.server.port == 9000
        assert config.server.log_level == "warning"

    def test_None_인자는_오버라이드_안함(self) -> None:
        """None 값은 기존 설정을 유지한다."""
        config = load_config_with_overrides(
            host=None,
            port=None,
            log_level=None,
        )

        assert config.server.host == "127.0.0.1"
        assert config.server.port == 8765
        assert config.server.log_level == "info"


# === TestEnsureDataDirectories ===


class TestEnsureDataDirectories:
    """데이터 디렉토리 초기화 테스트."""

    def test_SecureDirManager_호출(self) -> None:
        """SecureDirManager.ensure_secure_dirs()를 호출한다."""
        config = load_config_with_overrides()

        # 소스 모듈의 SecureDirManager를 패칭
        with patch(
            "security.secure_dir.SecureDirManager", autospec=True,
        ) as mock_cls:
            mock_manager = mock_cls.return_value
            mock_manager.ensure_secure_dirs.return_value = [Path("/a"), Path("/b")]

            ensure_data_directories(config)

            mock_cls.assert_called_once_with(config)
            mock_manager.ensure_secure_dirs.assert_called_once()

    def test_SecureDirManager_실패시_폴백(self, tmp_path: Path) -> None:
        """SecureDirManager가 실패하면 최소 디렉토리만 생성한다."""
        config = load_config_with_overrides()
        config.paths.base_dir = str(tmp_path / "data")

        # SecureDirManager 초기화 시 예외 발생 시뮬레이션
        with patch(
            "security.secure_dir.SecureDirManager",
            side_effect=RuntimeError("보안 설정 실패"),
        ):
            ensure_data_directories(config)

        # 폴백으로 최소 디렉토리가 생성되었는지 확인
        assert config.paths.resolved_base_dir.exists()


class TestEnsureMinimalDirs:
    """최소 디렉토리 생성 폴백 테스트."""

    def test_디렉토리_생성(self, tmp_path: Path) -> None:
        """base_dir 및 하위 디렉토리를 생성한다."""
        config = load_config_with_overrides()
        config.paths.base_dir = str(tmp_path / "data")

        _ensure_minimal_dirs(config)

        assert config.paths.resolved_base_dir.exists()
        assert config.paths.resolved_audio_input_dir.exists()
        assert config.paths.resolved_outputs_dir.exists()
        assert config.paths.resolved_checkpoints_dir.exists()
        assert config.paths.resolved_chroma_db_dir.exists()

    def test_이미_존재하는_디렉토리(self, tmp_path: Path) -> None:
        """이미 존재하는 디렉토리에도 에러 없이 동작한다."""
        config = load_config_with_overrides()
        config.paths.base_dir = str(tmp_path / "data")

        # 디렉토리 미리 생성
        config.paths.resolved_base_dir.mkdir(parents=True)

        _ensure_minimal_dirs(config)

        assert config.paths.resolved_base_dir.exists()


# === TestStartServerThread ===


class TestStartServerThread:
    """FastAPI 서버 데몬 스레드 시작 테스트."""

    @patch("api.server.create_app")
    @patch("uvicorn.Server")
    @patch("uvicorn.Config")
    def test_데몬_스레드_시작(
        self,
        mock_uv_config: MagicMock,
        mock_uv_server: MagicMock,
        mock_create_app: MagicMock,
    ) -> None:
        """서버가 데몬 스레드로 시작된다."""
        config = load_config_with_overrides()
        mock_server_instance = MagicMock()
        mock_uv_server.return_value = mock_server_instance

        thread = start_server_thread(config)

        assert thread.daemon is True
        assert thread.name == "fastapi-server"

        # mock된 server.run()이 즉시 반환하므로 스레드 종료 대기
        thread.join(timeout=2)

        # server.run()이 호출되었는지 확인
        mock_server_instance.run.assert_called_once()

    @patch("api.server.create_app")
    @patch("uvicorn.Server")
    @patch("uvicorn.Config")
    def test_uvicorn_Config_인자(
        self,
        mock_uv_config: MagicMock,
        mock_uv_server: MagicMock,
        mock_create_app: MagicMock,
    ) -> None:
        """uvicorn.Config에 올바른 인자가 전달된다."""
        config = load_config_with_overrides(
            host="0.0.0.0", port=9000, log_level="debug",
        )
        mock_app = MagicMock()
        mock_create_app.return_value = mock_app
        mock_uv_server.return_value = MagicMock()

        thread = start_server_thread(config)
        thread.join(timeout=2)

        mock_uv_config.assert_called_once_with(
            mock_app,
            host="0.0.0.0",
            port=9000,
            log_level="debug",
        )


# === TestSetupSignalHandlers ===


class TestSetupSignalHandlers:
    """시그널 핸들러 테스트."""

    def test_SIGTERM_핸들러_등록(self) -> None:
        """SIGTERM에 핸들러가 등록된다."""
        # 기존 핸들러 저장
        original = signal.getsignal(signal.SIGTERM)

        try:
            _setup_signal_handlers()

            current = signal.getsignal(signal.SIGTERM)
            assert current != signal.SIG_DFL
            assert callable(current)
        finally:
            # 원래 핸들러 복원
            signal.signal(signal.SIGTERM, original)

    def test_SIGTERM_핸들러_rumps_종료_호출(self) -> None:
        """SIGTERM 핸들러가 rumps.quit_application()을 호출한다."""
        original = signal.getsignal(signal.SIGTERM)

        try:
            _setup_signal_handlers()
            handler = signal.getsignal(signal.SIGTERM)

            with patch("rumps.quit_application") as mock_quit:
                handler(signal.SIGTERM, None)
                mock_quit.assert_called_once()
        finally:
            signal.signal(signal.SIGTERM, original)


# === TestMain ===


class TestMain:
    """메인 함수 통합 테스트.

    main() 내부에서 함수 레벨 import를 사용하므로,
    소스 모듈 경로로 패칭한다.
    """

    @patch("ui.menubar.run_menubar")
    @patch("main.start_server_thread")
    @patch("main._setup_signal_handlers")
    @patch("main.ensure_data_directories")
    def test_메뉴바_모드(
        self,
        mock_ensure_dirs: MagicMock,
        mock_signal: MagicMock,
        mock_server_thread: MagicMock,
        mock_run_menubar: MagicMock,
    ) -> None:
        """기본 실행 시 서버 스레드 + rumps 메뉴바를 실행한다."""
        mock_server_thread.return_value = MagicMock(spec=threading.Thread)

        main([])

        mock_ensure_dirs.assert_called_once()
        mock_server_thread.assert_called_once()
        mock_signal.assert_called_once()
        mock_run_menubar.assert_called_once()

    @patch("api.server.run_server")
    @patch("main.ensure_data_directories")
    def test_헤드리스_모드(
        self,
        mock_ensure_dirs: MagicMock,
        mock_run_server: MagicMock,
    ) -> None:
        """--no-menubar 시 서버만 실행한다."""
        main(["--no-menubar"])

        mock_ensure_dirs.assert_called_once()
        mock_run_server.assert_called_once()

    @patch("ui.menubar.run_menubar")
    @patch("main.start_server_thread")
    @patch("main._setup_signal_handlers")
    @patch("main.ensure_data_directories")
    def test_CLI_오버라이드_적용(
        self,
        mock_ensure_dirs: MagicMock,
        mock_signal: MagicMock,
        mock_server_thread: MagicMock,
        mock_run_menubar: MagicMock,
    ) -> None:
        """CLI 인자가 config에 반영되어 전달된다."""
        mock_server_thread.return_value = MagicMock(spec=threading.Thread)

        main(["--port", "9000", "--host", "0.0.0.0"])

        # start_server_thread에 전달된 config 확인
        call_config = mock_server_thread.call_args[0][0]
        assert call_config.server.port == 9000
        assert call_config.server.host == "0.0.0.0"

    @patch("main.load_config_with_overrides")
    @patch("main.setup_logging")
    def test_설정_로드_실패시_종료(
        self,
        mock_logging: MagicMock,
        mock_load: MagicMock,
    ) -> None:
        """설정 로드 실패 시 sys.exit(1)을 호출한다."""
        mock_load.side_effect = ValueError("설정 파일 손상")

        with pytest.raises(SystemExit) as exc_info:
            main([])

        assert exc_info.value.code == 1

    @patch("ui.menubar.run_menubar")
    @patch("main.start_server_thread")
    @patch("main._setup_signal_handlers")
    @patch("main.ensure_data_directories")
    def test_KeyboardInterrupt_처리(
        self,
        mock_ensure_dirs: MagicMock,
        mock_signal: MagicMock,
        mock_server_thread: MagicMock,
        mock_run_menubar: MagicMock,
    ) -> None:
        """KeyboardInterrupt 발생 시 정상 종료한다."""
        mock_server_thread.return_value = MagicMock(spec=threading.Thread)
        mock_run_menubar.side_effect = KeyboardInterrupt()

        # 예외 없이 정상 종료
        main([])

        mock_run_menubar.assert_called_once()

    @patch("api.server.run_server")
    @patch("main.ensure_data_directories")
    def test_헤드리스_KeyboardInterrupt(
        self,
        mock_ensure_dirs: MagicMock,
        mock_run_server: MagicMock,
    ) -> None:
        """헤드리스 모드에서 KeyboardInterrupt 발생 시 정상 종료한다."""
        mock_run_server.side_effect = KeyboardInterrupt()

        # 예외 없이 정상 종료
        main(["--no-menubar"])
