"""
WebSocket 이벤트 시스템 모듈 (WebSocket Event System Module)

목적: WebSocket을 통해 파이프라인 상태 변화를 클라이언트에 실시간 브로드캐스트한다.
주요 기능:
    - /ws/events WebSocket 엔드포인트
    - ConnectionManager로 연결 관리 (connect/disconnect)
    - 이벤트 브로드캐스트 (JSON 형식)
    - 하트비트 (30초 간격)
    - 최대 동시 연결 수 제한 (로컬 시스템 보호)
의존성: fastapi (WebSocket), asyncio
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

# === 이벤트 타입 정의 ===

# WebSocket 라우터
ws_router = APIRouter()

# 기본 설정 상수
_DEFAULT_MAX_CONNECTIONS = 10
_DEFAULT_HEARTBEAT_INTERVAL = 30  # 초


class EventType(str, Enum):
    """WebSocket 이벤트 타입을 정의하는 열거형.

    Attributes:
        HEARTBEAT: 하트비트 (연결 유지 확인)
        PIPELINE_STATUS: 파이프라인 상태 변화
        JOB_ADDED: 새 작업 등록
        JOB_COMPLETED: 작업 완료
        JOB_FAILED: 작업 실패
        SYSTEM_STATUS: 시스템 전체 상태 변화
        CONNECTION_REJECTED: 연결 거부 (최대 연결 초과)
    """

    HEARTBEAT = "heartbeat"
    PIPELINE_STATUS = "pipeline_status"
    JOB_ADDED = "job_added"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    SYSTEM_STATUS = "system_status"
    CONNECTION_REJECTED = "connection_rejected"
    RECORDING_STARTED = "recording_started"
    RECORDING_STOPPED = "recording_stopped"
    RECORDING_ERROR = "recording_error"
    RECORDING_DURATION = "recording_duration"


@dataclass
class WebSocketEvent:
    """WebSocket으로 전송되는 이벤트 데이터 클래스.

    Attributes:
        event_type: 이벤트 타입
        data: 이벤트 관련 데이터
        timestamp: 이벤트 발생 시각 (Unix timestamp)
    """

    event_type: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        """타임스탬프 자동 설정."""
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_json(self) -> str:
        """JSON 문자열로 직렬화한다.

        한국어 텍스트를 위해 ensure_ascii=False를 사용한다.
        PERF: asdict() 대신 수동 딕셔너리로 변환하여 재귀 복사 오버헤드를 제거한다.

        Returns:
            JSON 문자열
        """
        return json.dumps(
            {
                "event_type": self.event_type,
                "data": self.data,
                "timestamp": self.timestamp,
            },
            ensure_ascii=False,
        )


# === 연결 관리자 ===


class ConnectionManager:
    """WebSocket 연결을 중앙에서 관리하는 클래스.

    동시 연결 수를 제한하고, 모든 연결에 이벤트를 브로드캐스트한다.
    개별 연결 전송 실패 시 해당 연결만 안전하게 제거한다.

    Args:
        max_connections: 최대 동시 연결 수 (기본값: 10)
        heartbeat_interval: 하트비트 전송 간격 (초, 기본값: 30)

    사용 예시:
        manager = ConnectionManager()
        await manager.connect(websocket)
        await manager.broadcast_event(event)
        manager.disconnect(websocket)
    """

    def __init__(
        self,
        max_connections: int = _DEFAULT_MAX_CONNECTIONS,
        heartbeat_interval: int = _DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        """ConnectionManager를 초기화한다.

        Args:
            max_connections: 최대 동시 연결 수
            heartbeat_interval: 하트비트 전송 간격 (초)
        """
        self._connections: set[WebSocket] = set()
        self._max_connections = max_connections
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_task: Optional[asyncio.Task[None]] = None

        logger.info(
            f"ConnectionManager 초기화: "
            f"max_connections={max_connections}, "
            f"heartbeat_interval={heartbeat_interval}초"
        )

    @property
    def connection_count(self) -> int:
        """현재 활성 연결 수를 반환한다."""
        return len(self._connections)

    @property
    def max_connections(self) -> int:
        """최대 연결 수를 반환한다."""
        return self._max_connections

    @property
    def heartbeat_interval(self) -> int:
        """하트비트 간격(초)을 반환한다."""
        return self._heartbeat_interval

    async def connect(self, websocket: WebSocket) -> bool:
        """WebSocket 연결을 수락하고 관리 목록에 추가한다.

        최대 연결 수를 초과하면 거부 메시지를 보내고 연결을 닫는다.

        Args:
            websocket: 연결할 WebSocket 인스턴스

        Returns:
            연결 성공 여부 (True: 연결됨, False: 거부됨)
        """
        # 최대 연결 수 확인
        if len(self._connections) >= self._max_connections:
            await websocket.accept()
            # 거부 메시지 전송 후 연결 종료
            reject_event = WebSocketEvent(
                event_type=EventType.CONNECTION_REJECTED.value,
                data={
                    "reason": "최대 연결 수 초과",
                    "max_connections": self._max_connections,
                    "current_connections": len(self._connections),
                },
            )
            await websocket.send_text(reject_event.to_json())
            await websocket.close(
                code=1013,  # Try Again Later
                reason="최대 연결 수 초과",
            )
            logger.warning(
                f"WebSocket 연결 거부: 최대 연결 수 초과 "
                f"({len(self._connections)}/{self._max_connections})"
            )
            return False

        await websocket.accept()
        self._connections.add(websocket)

        logger.info(
            f"WebSocket 연결 수락: "
            f"활성 연결 {len(self._connections)}/{self._max_connections}"
        )
        return True

    def disconnect(self, websocket: WebSocket) -> None:
        """WebSocket 연결을 관리 목록에서 제거한다.

        Args:
            websocket: 제거할 WebSocket 인스턴스
        """
        self._connections.discard(websocket)
        logger.info(
            f"WebSocket 연결 해제: "
            f"활성 연결 {len(self._connections)}/{self._max_connections}"
        )

    async def broadcast_event(self, event: WebSocketEvent) -> int:
        """모든 연결된 클라이언트에 이벤트를 브로드캐스트한다.

        전송 실패한 연결은 자동으로 제거한다.

        Args:
            event: 브로드캐스트할 이벤트

        Returns:
            성공적으로 전송된 연결 수
        """
        if not self._connections:
            return 0

        message = event.to_json()
        failed: list[WebSocket] = []
        success_count = 0

        # 각 연결에 개별 전송 (하나의 실패가 다른 연결에 영향 없음)
        send_tasks = []
        connections_list = list(self._connections)

        for ws in connections_list:
            send_tasks.append(self._safe_send(ws, message, failed))

        results = await asyncio.gather(*send_tasks, return_exceptions=True)

        # 성공 카운트 집계
        for result in results:
            if result is True:
                success_count += 1

        # 실패한 연결 정리
        for ws in failed:
            self._connections.discard(ws)
            logger.debug("전송 실패한 WebSocket 연결 제거")

        if failed:
            logger.warning(
                f"브로드캐스트 부분 실패: "
                f"성공 {success_count}, 실패 {len(failed)}"
            )

        return success_count

    async def _safe_send(
        self,
        websocket: WebSocket,
        message: str,
        failed: list[WebSocket],
    ) -> bool:
        """WebSocket에 메시지를 안전하게 전송한다.

        전송 실패 시 failed 목록에 추가한다.

        Args:
            websocket: 전송 대상 WebSocket
            message: 전송할 JSON 문자열
            failed: 실패한 연결을 수집할 리스트

        Returns:
            전송 성공 여부
        """
        try:
            await websocket.send_text(message)
            return True
        except Exception:
            failed.append(websocket)
            return False

    async def broadcast_json(self, data: dict[str, Any]) -> int:
        """딕셔너리를 WebSocketEvent로 감싸서 브로드캐스트한다.

        Args:
            data: 브로드캐스트할 데이터 딕셔너리
                  (event_type 키 필수)

        Returns:
            성공적으로 전송된 연결 수
        """
        event_type = data.pop("event_type", EventType.SYSTEM_STATUS.value)
        event = WebSocketEvent(event_type=event_type, data=data)
        return await self.broadcast_event(event)

    async def start_heartbeat(self) -> None:
        """하트비트 태스크를 시작한다.

        이미 실행 중이면 무시한다.
        """
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            logger.debug("하트비트 태스크가 이미 실행 중")
            return

        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name="ws-heartbeat",
        )
        logger.info(
            f"하트비트 태스크 시작: 간격 {self._heartbeat_interval}초"
        )

    async def stop_heartbeat(self) -> None:
        """하트비트 태스크를 중지한다."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
            logger.info("하트비트 태스크 중지")

    async def _heartbeat_loop(self) -> None:
        """주기적으로 하트비트 이벤트를 브로드캐스트하는 루프.

        CancelledError 발생 시 정상 종료한다.
        """
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)

                if self._connections:
                    heartbeat = WebSocketEvent(
                        event_type=EventType.HEARTBEAT.value,
                        data={
                            "active_connections": len(self._connections),
                        },
                    )
                    await self.broadcast_event(heartbeat)
                    logger.debug(
                        f"하트비트 전송: 활성 연결 {len(self._connections)}"
                    )

        except asyncio.CancelledError:
            logger.debug("하트비트 루프 취소됨")
            raise

    async def close_all(self) -> None:
        """모든 연결을 닫고 하트비트를 중지한다.

        개별 연결 종료 실패 시에도 다른 연결 정리를 계속하며,
        실패한 연결을 로그에 남긴다 (STAB: 예외 무시 방지).
        """
        await self.stop_heartbeat()

        close_errors = 0
        for ws in list(self._connections):
            try:
                await ws.close(code=1001, reason="서버 종료")
            except (ConnectionResetError, RuntimeError) as e:
                # 이미 끊어진 연결 또는 런타임 에러 — 일반적인 경우
                logger.debug(f"WebSocket 연결 종료 중 무시 가능한 에러: {e}")
                close_errors += 1
            except Exception as e:
                # 예상치 못한 에러 — 경고 로깅
                logger.warning(
                    f"WebSocket 연결 종료 중 예외: "
                    f"{type(e).__name__}: {e}"
                )
                close_errors += 1

        self._connections.clear()

        if close_errors > 0:
            logger.info(
                f"모든 WebSocket 연결 종료 완료 "
                f"(종료 에러 {close_errors}건)"
            )
        else:
            logger.info("모든 WebSocket 연결 종료 완료")


# === WebSocket 엔드포인트 ===


@ws_router.websocket("/ws/events")
async def websocket_events(websocket: WebSocket) -> None:
    """파이프라인 이벤트를 실시간으로 스트리밍하는 WebSocket 엔드포인트.

    클라이언트 연결을 수락하고, 연결이 유지되는 동안
    서버에서 발생하는 파이프라인 이벤트를 수신한다.
    클라이언트에서 보낸 메시지는 무시하지만 연결 유지를 위해 수신한다.

    Args:
        websocket: FastAPI WebSocket 인스턴스
    """
    # app.state에서 ConnectionManager 가져오기
    manager: Optional[ConnectionManager] = getattr(
        websocket.app.state, "ws_manager", None,
    )

    if manager is None:
        # ConnectionManager가 없으면 연결 거부
        await websocket.accept()
        error_event = WebSocketEvent(
            event_type=EventType.CONNECTION_REJECTED.value,
            data={"reason": "WebSocket 매니저가 초기화되지 않았습니다."},
        )
        await websocket.send_text(error_event.to_json())
        await websocket.close(code=1011, reason="서버 에러")
        return

    # 연결 시도
    connected = await manager.connect(websocket)
    if not connected:
        return

    try:
        # 연결 성공 알림
        welcome = WebSocketEvent(
            event_type=EventType.SYSTEM_STATUS.value,
            data={
                "message": "WebSocket 연결 성공",
                "active_connections": manager.connection_count,
            },
        )
        await websocket.send_text(welcome.to_json())

        # 클라이언트 메시지 대기 루프 (연결 유지용)
        while True:
            # 클라이언트에서 보내는 메시지를 수신하여 연결을 유지
            # 실제 클라이언트 명령 처리가 필요하면 여기서 확장
            await websocket.receive_text()

    except WebSocketDisconnect:
        logger.info("WebSocket 클라이언트 연결 끊김")
    except Exception as e:
        logger.warning(f"WebSocket 예외 발생: {type(e).__name__}: {e}")
    finally:
        manager.disconnect(websocket)
