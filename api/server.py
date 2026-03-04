"""
FastAPI 백엔드 서버 모듈 (FastAPI Backend Server Module)

목적: FastAPI 애플리케이션을 정의하고 lifespan 이벤트로
      시스템 리소스(DB, 작업 큐, 검색/Chat 엔진)의 초기화와 정리를 관리한다.
주요 기능:
    - FastAPI 앱 팩토리 (create_app)
    - lifespan 컨텍스트 매니저로 startup/shutdown 관리
    - API 라우터 등록 (api/routes.py)
    - 정적 파일 서빙 (ui/web/ 디렉토리)
    - CORS 미들웨어 (localhost만 허용)
    - 헬스체크 엔드포인트 (/api/health)
    - 글로벌 예외 핸들러
    - uvicorn 실행 헬퍼 함수
의존성: fastapi, uvicorn, config 모듈, core/job_queue 모듈,
        search/hybrid_search 모듈, search/chat 모듈
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from config import AppConfig, get_config
from core.job_queue import AsyncJobQueue, JobQueue

logger = logging.getLogger(__name__)

# 프로젝트 루트에서 ui/web/ 경로 계산
_PROJECT_ROOT = Path(__file__).parent.parent
_STATIC_DIR = _PROJECT_ROOT / "ui" / "web"


# === lifespan 컨텍스트 매니저 ===


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI 앱의 생명주기를 관리한다.

    startup 시:
        - 설정 로드
        - JobQueue 초기화 (SQLite WAL 모드)
        - app.state에 공유 리소스 저장

    shutdown 시:
        - JobQueue 연결 종료
        - 리소스 정리

    Args:
        app: FastAPI 애플리케이션 인스턴스

    Yields:
        None (startup 완료 후 앱 실행, 종료 시 cleanup)
    """
    # --- Startup ---
    config: AppConfig = app.state.config
    logger.info("FastAPI 서버 시작 — 리소스 초기화 중...")

    # 작업 큐 초기화
    db_path = config.paths.resolved_pipeline_db
    job_queue = JobQueue(db_path, max_retries=config.pipeline.retry_max_count)
    async_queue = AsyncJobQueue(job_queue)
    await async_queue.initialize()

    # app.state에 공유 리소스 저장 (라우터에서 접근 가능)
    app.state.job_queue = async_queue
    app.state.start_time = time.time()

    # 검색 엔진 및 Chat 엔진 초기화 (lazy: 실패해도 서버 시작은 가능)
    try:
        from search.hybrid_search import HybridSearchEngine
        app.state.search_engine = HybridSearchEngine(config=config)
        logger.info("HybridSearchEngine 초기화 완료")
    except Exception as e:
        app.state.search_engine = None
        logger.warning(f"HybridSearchEngine 초기화 실패 (검색 비활성화): {e}")

    try:
        from search.chat import ChatEngine
        app.state.chat_engine = ChatEngine(config=config)
        logger.info("ChatEngine 초기화 완료")
    except Exception as e:
        app.state.chat_engine = None
        logger.warning(f"ChatEngine 초기화 실패 (Chat 비활성화): {e}")

    # WebSocket ConnectionManager 초기화
    from api.websocket import ConnectionManager
    ws_manager = ConnectionManager()
    app.state.ws_manager = ws_manager
    await ws_manager.start_heartbeat()
    logger.info("WebSocket ConnectionManager 초기화 완료")

    logger.info(
        f"FastAPI 서버 리소스 초기화 완료 — "
        f"DB: {db_path}, 포트: {config.server.port}"
    )

    yield  # 앱 실행

    # --- Shutdown ---
    logger.info("FastAPI 서버 종료 — 리소스 정리 중...")

    # WebSocket 연결 종료
    if hasattr(app.state, "ws_manager"):
        await app.state.ws_manager.close_all()
        logger.info("WebSocket 연결 모두 종료 완료")

    # 작업 큐 종료
    if hasattr(app.state, "job_queue"):
        await app.state.job_queue.close()
        logger.info("JobQueue DB 연결 종료 완료")

    logger.info("FastAPI 서버 리소스 정리 완료")


# === 앱 팩토리 ===


def create_app(config: Optional[AppConfig] = None) -> FastAPI:
    """FastAPI 애플리케이션을 생성하고 설정한다.

    팩토리 패턴으로 테스트와 프로덕션 환경 모두 지원.

    Args:
        config: 앱 설정. None이면 config.yaml에서 로드.

    Returns:
        설정 완료된 FastAPI 인스턴스
    """
    if config is None:
        config = get_config()

    app = FastAPI(
        title="회의 전사 시스템 API",
        description="한국어 로컬 AI 회의 전사 + RAG + AI Chat 시스템",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=_lifespan,
    )

    # config를 app.state에 저장 (lifespan에서 접근)
    app.state.config = config

    # CORS 미들웨어 — localhost만 허용
    _setup_cors(app, config)

    # 글로벌 예외 핸들러
    _setup_exception_handlers(app)

    # 헬스체크 엔드포인트
    _setup_health_endpoint(app)

    # API 라우터 등록
    _setup_routes(app)

    # WebSocket 라우터 등록
    _setup_websocket_routes(app)

    # 정적 파일 서빙 (ui/web/ 디렉토리가 존재할 때만)
    _setup_static_files(app)

    logger.info(
        f"FastAPI 앱 생성 완료 — "
        f"host={config.server.host}, port={config.server.port}"
    )

    return app


# === CORS 설정 ===


def _setup_cors(app: FastAPI, config: AppConfig) -> None:
    """CORS 미들웨어를 설정한다.

    localhost 접근만 허용하여 외부 요청을 차단한다.

    Args:
        app: FastAPI 인스턴스
        config: 앱 설정
    """
    port = config.server.port
    allowed_origins = [
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        "http://127.0.0.1",
        "http://localhost",
    ]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    logger.info(f"CORS 설정 완료 — 허용 오리진: {allowed_origins}")


# === 예외 핸들러 ===


def _setup_exception_handlers(app: FastAPI) -> None:
    """글로벌 예외 핸들러를 등록한다.

    예상치 못한 예외 발생 시 안전한 JSON 응답을 반환한다.

    Args:
        app: FastAPI 인스턴스
    """

    @app.exception_handler(Exception)
    async def _global_exception_handler(
        request: Request, exc: Exception,
    ) -> JSONResponse:
        """처리되지 않은 예외를 잡아서 500 응답을 반환한다.

        Args:
            request: HTTP 요청 객체
            exc: 발생한 예외

        Returns:
            500 상태의 JSON 에러 응답
        """
        logger.error(
            f"처리되지 않은 예외: {type(exc).__name__}: {exc}",
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "서버 내부 오류가 발생했습니다.",
                "detail": str(exc),
            },
        )


# === 헬스체크 ===


def _setup_health_endpoint(app: FastAPI) -> None:
    """헬스체크 엔드포인트를 등록한다.

    Args:
        app: FastAPI 인스턴스
    """

    @app.get("/api/health")
    async def health_check() -> dict:
        """서버 상태를 반환한다.

        Returns:
            서버 상태 정보 딕셔너리:
                - status: 동작 상태 ("ok")
                - uptime_seconds: 서버 가동 시간 (초)
                - version: API 버전
        """
        uptime = 0.0
        if hasattr(app.state, "start_time"):
            uptime = round(time.time() - app.state.start_time, 1)

        return {
            "status": "ok",
            "uptime_seconds": uptime,
            "version": "1.0.0",
        }


# === API 라우터 ===


def _setup_routes(app: FastAPI) -> None:
    """API 라우터를 등록한다.

    api/routes.py에 정의된 엔드포인트들을 앱에 포함시킨다.

    Args:
        app: FastAPI 인스턴스
    """
    from api.routes import router

    app.include_router(router)
    logger.info("API 라우터 등록 완료")


# === WebSocket 라우터 ===


def _setup_websocket_routes(app: FastAPI) -> None:
    """WebSocket 라우터를 등록한다.

    api/websocket.py에 정의된 WebSocket 엔드포인트를 앱에 포함시킨다.

    Args:
        app: FastAPI 인스턴스
    """
    from api.websocket import ws_router

    app.include_router(ws_router)
    logger.info("WebSocket 라우터 등록 완료")


# === 정적 파일 ===


def _setup_static_files(app: FastAPI) -> None:
    """정적 파일 서빙을 설정한다.

    ui/web/ 디렉토리가 존재하면 / 경로에 마운트한다.
    디렉토리가 없으면 경고 로그만 출력하고 건너뛴다.

    Args:
        app: FastAPI 인스턴스
    """
    if _STATIC_DIR.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=str(_STATIC_DIR)),
            name="static",
        )
        logger.info(f"정적 파일 서빙 설정 완료 — 경로: {_STATIC_DIR}")
    else:
        logger.warning(
            f"정적 파일 디렉토리가 존재하지 않습니다: {_STATIC_DIR}. "
            f"정적 파일 서빙이 비활성화됩니다."
        )


# === uvicorn 실행 헬퍼 ===


def run_server(config: Optional[AppConfig] = None) -> None:
    """uvicorn으로 FastAPI 서버를 실행한다.

    단독 실행 시 사용. main.py에서는 데몬 스레드로 별도 실행.

    Args:
        config: 앱 설정. None이면 config.yaml에서 로드.
    """
    import uvicorn

    if config is None:
        config = get_config()

    app = create_app(config)

    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level=config.server.log_level,
    )


# 직접 실행 시 서버 시작
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    run_server()
