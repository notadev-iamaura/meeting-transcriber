"""
앱 진입점 모듈 (Application Entry Point Module)

목적: rumps 메뉴바 앱(메인 스레드)과 FastAPI 서버(데몬 스레드)를 통합하여
      단일 프로세스로 실행한다.
주요 기능:
    - 커맨드라인 인자 파싱 (argparse)
    - 로깅 설정 (콘솔 + 선택적 파일 핸들러)
    - 데이터 디렉토리 초기화 (보안 설정 포함)
    - FastAPI 서버 데몬 스레드 실행
    - rumps 메뉴바 앱 메인 스레드 실행
    - --no-menubar 헤드리스 모드 지원
    - Graceful shutdown 처리
의존성: config, api/server, ui/menubar, security/secure_dir
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import uvicorn

from config import AppConfig, load_config, reset_config

logger = logging.getLogger(__name__)

# 로그 포맷
_LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 로그 파일 크기 제한
_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10MB
_LOG_BACKUP_COUNT = 5


# === CLI 인자 파싱 ===


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """커맨드라인 인자를 파싱한다.

    Args:
        argv: 인자 리스트. None이면 sys.argv[1:]을 사용.

    Returns:
        파싱된 인자 네임스페이스
    """
    parser = argparse.ArgumentParser(
        description="한국어 로컬 AI 회의 전사 시스템",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "사용 예시:\n"
            "  python main.py                    # 메뉴바 + 웹 서버 실행\n"
            "  python main.py --no-menubar       # 웹 서버만 실행 (헤드리스)\n"
            "  python main.py --port 9000        # 포트 변경\n"
            "  python main.py --log-level debug  # 디버그 로깅\n"
        ),
    )

    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="설정 파일 경로 (기본: config.yaml)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="서버 호스트 (기본: config.yaml의 server.host)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="서버 포트 (기본: config.yaml의 server.port)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=["debug", "info", "warning", "error", "critical"],
        default=None,
        help="로그 레벨 (기본: config.yaml의 server.log_level)",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="로그 파일 경로 (기본: 콘솔만 출력)",
    )
    parser.add_argument(
        "--no-menubar",
        action="store_true",
        default=False,
        help="메뉴바 없이 서버만 실행 (헤드리스 모드)",
    )

    return parser.parse_args(argv)


# === 로깅 설정 ===


def setup_logging(
    level: str = "info",
    log_file: Path | None = None,
) -> None:
    """로깅을 설정한다.

    콘솔 핸들러는 항상 추가. log_file 지정 시 RotatingFileHandler 추가.

    Args:
        level: 로그 레벨 ("debug", "info", "warning", "error", "critical")
        log_file: 로그 파일 경로. None이면 콘솔만 출력.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # 기존 핸들러 제거 (중복 방지)
    root_logger.handlers.clear()

    # 콘솔 핸들러
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT))
    root_logger.addHandler(console_handler)

    # 파일 핸들러 (선택적)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            str(log_file),
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT))
        root_logger.addHandler(file_handler)

    # uvicorn 로거 레벨 동기화
    for uvicorn_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(uvicorn_logger_name)
        uv_logger.setLevel(numeric_level)


# === 설정 로드 ===


def load_config_with_overrides(
    config_path: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    log_level: str | None = None,
) -> AppConfig:
    """설정 파일을 로드하고 CLI 인자로 오버라이드한다.

    Args:
        config_path: 설정 파일 경로
        host: 서버 호스트 오버라이드
        port: 서버 포트 오버라이드
        log_level: 로그 레벨 오버라이드

    Returns:
        CLI 인자가 반영된 AppConfig 인스턴스
    """
    # 싱글턴 캐시 초기화 (CLI에서 매번 새로 로드)
    reset_config()
    config = load_config(config_path)

    # CLI 인자로 오버라이드
    if host is not None:
        config.server.host = host
    if port is not None:
        config.server.port = port
    if log_level is not None:
        config.server.log_level = log_level

    return config


# === 데이터 디렉토리 초기화 ===


def ensure_data_directories(config: AppConfig) -> None:
    """데이터 디렉토리를 생성하고 보안 설정을 적용한다.

    security/secure_dir.py의 SecureDirManager를 사용하여
    base_dir 및 하위 디렉토리를 생성하고 권한/Spotlight 제외 등을 적용.

    Args:
        config: 앱 설정
    """
    try:
        from security.secure_dir import SecureDirManager

        manager = SecureDirManager(config)
        dirs = manager.ensure_secure_dirs()
        logger.info(f"데이터 디렉토리 초기화 완료: {len(dirs)}개")
    except (OSError, PermissionError, ImportError) as e:
        logger.warning(f"데이터 디렉토리 보안 설정 실패 (기본 생성으로 대체): {e}")
        # 보안 설정 실패 시 최소한 디렉토리만 생성
        _ensure_minimal_dirs(config)


def _ensure_minimal_dirs(config: AppConfig) -> None:
    """보안 설정 없이 최소 디렉토리만 생성한다.

    SecureDirManager 실패 시 폴백 용도.

    Args:
        config: 앱 설정
    """
    dirs = [
        config.paths.resolved_base_dir,
        config.paths.resolved_audio_input_dir,
        config.paths.resolved_outputs_dir,
        config.paths.resolved_checkpoints_dir,
        config.paths.resolved_chroma_db_dir,
        config.paths.resolved_recordings_temp_dir,
    ]
    for dir_path in dirs:
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"디렉토리 생성 실패: {dir_path} — {e}")


# === FastAPI 서버 스레드 ===


class _ServerThread(threading.Thread):
    """FastAPI 서버를 데몬 스레드에서 실행하며 예외를 저장하는 스레드.

    서버 스레드에서 발생한 예외를 메인 스레드에서 확인할 수 있도록
    exception 속성에 저장한다.
    """

    def __init__(self, server: uvicorn.Server) -> None:
        super().__init__(name="fastapi-server", daemon=True)
        self._server = server
        self.exception: Exception | None = None

    def run(self) -> None:
        """서버를 실행하고 예외 발생 시 저장한다."""
        try:
            self._server.run()
        except Exception as e:  # noqa: BLE001 — 스레드 최상위 catch-all
            self.exception = e
            logger.error(f"FastAPI 서버 실행 중 오류: {e}", exc_info=True)


def start_server_thread(config: AppConfig) -> _ServerThread:
    """FastAPI 서버를 데몬 스레드에서 시작한다.

    uvicorn.Server를 직접 사용하여 비메인 스레드에서도
    안전하게 서버를 실행한다.

    Args:
        config: 앱 설정

    Returns:
        시작된 서버 데몬 스레드 (_ServerThread)
    """
    import uvicorn

    from api.server import create_app

    app = create_app(config)

    uv_config = uvicorn.Config(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level=config.server.log_level,
    )
    server = uvicorn.Server(uv_config)

    thread = _ServerThread(server)
    thread.start()

    logger.info(f"FastAPI 서버 스레드 시작 — http://{config.server.host}:{config.server.port}")

    return thread


# === 시그널 핸들러 ===


def _setup_signal_handlers() -> None:
    """SIGTERM 시그널 핸들러를 설정한다.

    SIGTERM 수신 시 rumps 앱을 종료하여 graceful shutdown을 수행.
    SIGINT는 rumps의 NSApplication 이벤트 루프가 처리한다.
    """

    def _handle_sigterm(signum: int, frame: object) -> None:
        """SIGTERM 수신 시 앱을 종료한다."""
        logger.info(f"시그널 {signum} 수신 — 종료 시작")
        try:
            import rumps

            rumps.quit_application()
        except (ImportError, RuntimeError):
            sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)


# === 메인 함수 ===


def main(argv: list[str] | None = None) -> None:
    """애플리케이션 메인 함수.

    실행 흐름:
        1. CLI 인자 파싱
        2. 로깅 설정
        3. 설정 로드 (CLI 오버라이드 적용)
        4. 데이터 디렉토리 초기화
        5. FastAPI 서버 시작
        6. rumps 메뉴바 실행 (또는 --no-menubar 시 서버만 실행)

    Args:
        argv: 커맨드라인 인자. None이면 sys.argv[1:] 사용.
    """
    # 1. CLI 인자 파싱
    args = parse_args(argv)

    # 2. 로깅 설정
    initial_log_level = args.log_level or "info"
    setup_logging(level=initial_log_level, log_file=args.log_file)

    logger.info("=== 한국어 로컬 AI 회의 전사 시스템 시작 ===")

    # 3. 설정 로드
    try:
        config = load_config_with_overrides(
            config_path=args.config,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
        )
    except (OSError, ValueError) as e:
        logger.critical(f"설정 로드 실패: {e}", exc_info=True)
        sys.exit(1)

    # 로그 레벨 재설정 (config 기반, CLI 미지정 시)
    final_log_level = args.log_level or config.server.log_level
    if final_log_level != initial_log_level:
        setup_logging(level=final_log_level, log_file=args.log_file)

    logger.info(
        f"설정 로드 완료 — "
        f"host={config.server.host}, port={config.server.port}, "
        f"log_level={final_log_level}"
    )

    # 4. 데이터 디렉토리 초기화
    ensure_data_directories(config)

    # 4-1. 사용자 설정 초기화 (프롬프트/용어집 JSON 파일 생성)
    try:
        from core.user_settings import init_user_settings

        init_user_settings()
    except Exception as e:
        logger.warning(f"사용자 설정 초기화 실패 (진행 계속): {e}")

    # 시그널 핸들러 등록 (모든 모드에서 SIGTERM 처리)
    _setup_signal_handlers()

    # 5-6. 실행 모드 분기
    if args.no_menubar:
        # 헤드리스 모드: 서버를 메인 스레드에서 직접 실행
        logger.info("헤드리스 모드 — 메뉴바 없이 서버만 실행")
        from api.server import run_server

        try:
            run_server(config)
        except KeyboardInterrupt:
            logger.info("키보드 인터럽트 수신 — 종료")
    else:
        # 메뉴바 모드: 서버 데몬 스레드 + rumps 메인 스레드
        server_thread = start_server_thread(config)

        # 서버 스레드가 즉시 실패했는지 짧은 대기 후 확인
        server_thread.join(timeout=1.0)
        if not server_thread.is_alive() and server_thread.exception is not None:
            logger.critical(f"FastAPI 서버 시작 실패: {server_thread.exception}")
            sys.exit(1)

        logger.info("메뉴바 모드 — rumps 앱 시작")
        try:
            from ui.menubar import run_menubar

            run_menubar(config)
        except KeyboardInterrupt:
            logger.info("키보드 인터럽트 수신 — 종료")

    logger.info("=== 시스템 종료 완료 ===")


if __name__ == "__main__":
    main()
