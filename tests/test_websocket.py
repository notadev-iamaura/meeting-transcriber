"""
WebSocket 이벤트 시스템 테스트 (WebSocket Event System Tests)

목적: api/websocket.py의 ConnectionManager, WebSocketEvent,
      WebSocket 엔드포인트를 종합적으로 검증한다.
주요 테스트:
    - WebSocketEvent 직렬화/생성
    - ConnectionManager 연결/해제, 브로드캐스트
    - 하트비트 태스크 시작/중지
    - 최대 연결 수 초과 거부
    - /ws/events 엔드포인트 통합 테스트
의존성: pytest, fastapi[testclient], api/websocket 모듈, api/server 모듈
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from api.websocket import (
    ConnectionManager,
    EventType,
    WebSocketEvent,
    ws_router,
)

# === WebSocketEvent 테스트 ===


class TestWebSocketEvent:
    """WebSocketEvent 데이터 클래스 테스트."""

    def test_기본_생성(self) -> None:
        """이벤트 생성 시 타임스탬프가 자동 설정된다."""
        before = time.time()
        event = WebSocketEvent(
            event_type=EventType.HEARTBEAT.value,
            data={"key": "value"},
        )
        after = time.time()

        assert event.event_type == "heartbeat"
        assert event.data == {"key": "value"}
        assert before <= event.timestamp <= after

    def test_커스텀_타임스탬프(self) -> None:
        """명시적 타임스탬프가 유지된다."""
        event = WebSocketEvent(
            event_type=EventType.JOB_ADDED.value,
            data={},
            timestamp=1234567890.0,
        )
        assert event.timestamp == 1234567890.0

    def test_json_직렬화(self) -> None:
        """to_json()이 올바른 JSON을 반환한다."""
        event = WebSocketEvent(
            event_type=EventType.PIPELINE_STATUS.value,
            data={"meeting_id": "test_001", "status": "transcribing"},
            timestamp=1000.0,
        )
        result = json.loads(event.to_json())

        assert result["event_type"] == "pipeline_status"
        assert result["data"]["meeting_id"] == "test_001"
        assert result["data"]["status"] == "transcribing"
        assert result["timestamp"] == 1000.0

    def test_한국어_json_직렬화(self) -> None:
        """한국어 텍스트가 ensure_ascii=False로 올바르게 직렬화된다."""
        event = WebSocketEvent(
            event_type=EventType.SYSTEM_STATUS.value,
            data={"message": "처리 완료"},
        )
        json_str = event.to_json()

        # 한국어가 이스케이프되지 않고 그대로 포함
        assert "처리 완료" in json_str
        assert "\\u" not in json_str  # 유니코드 이스케이프 없음

    def test_빈_데이터(self) -> None:
        """빈 data 딕셔너리도 정상 동작한다."""
        event = WebSocketEvent(event_type=EventType.HEARTBEAT.value)
        result = json.loads(event.to_json())

        assert result["data"] == {}


# === ConnectionManager 테스트 ===


class TestConnectionManager:
    """ConnectionManager 연결 관리 테스트."""

    def test_초기화(self) -> None:
        """기본값으로 올바르게 초기화된다."""
        manager = ConnectionManager()
        assert manager.connection_count == 0
        assert manager.max_connections == 10
        assert manager.heartbeat_interval == 30

    def test_커스텀_초기화(self) -> None:
        """커스텀 값으로 초기화된다."""
        manager = ConnectionManager(
            max_connections=5,
            heartbeat_interval=15,
        )
        assert manager.max_connections == 5
        assert manager.heartbeat_interval == 15

    @pytest.mark.asyncio
    async def test_연결_수락(self) -> None:
        """WebSocket 연결을 수락하고 카운트가 증가한다."""
        manager = ConnectionManager()
        ws = AsyncMock()

        result = await manager.connect(ws)

        assert result is True
        assert manager.connection_count == 1
        ws.accept.assert_called_once()

    @pytest.mark.asyncio
    async def test_연결_해제(self) -> None:
        """연결 해제 후 카운트가 감소한다."""
        manager = ConnectionManager()
        ws = AsyncMock()

        await manager.connect(ws)
        assert manager.connection_count == 1

        manager.disconnect(ws)
        assert manager.connection_count == 0

    @pytest.mark.asyncio
    async def test_중복_해제_안전(self) -> None:
        """이미 해제된 연결을 다시 해제해도 에러가 없다."""
        manager = ConnectionManager()
        ws = AsyncMock()

        await manager.connect(ws)
        manager.disconnect(ws)
        manager.disconnect(ws)  # 두 번째 해제 — 에러 없음
        assert manager.connection_count == 0

    @pytest.mark.asyncio
    async def test_최대_연결_초과_거부(self) -> None:
        """최대 연결 수를 초과하면 연결을 거부한다."""
        manager = ConnectionManager(max_connections=2)

        # 2개 연결 성공
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        assert await manager.connect(ws1) is True
        assert await manager.connect(ws2) is True
        assert manager.connection_count == 2

        # 3번째 연결 거부
        ws3 = AsyncMock()
        result = await manager.connect(ws3)

        assert result is False
        assert manager.connection_count == 2  # 변하지 않음
        ws3.accept.assert_called_once()
        ws3.send_text.assert_called_once()
        ws3.close.assert_called_once()

        # 거부 메시지 확인
        sent_msg = json.loads(ws3.send_text.call_args[0][0])
        assert sent_msg["event_type"] == "connection_rejected"
        assert "최대 연결 수 초과" in sent_msg["data"]["reason"]


class TestConnectionManagerBroadcast:
    """ConnectionManager 브로드캐스트 테스트."""

    @pytest.mark.asyncio
    async def test_단일_연결_브로드캐스트(self) -> None:
        """단일 연결에 이벤트가 전달된다."""
        manager = ConnectionManager()
        ws = AsyncMock()
        await manager.connect(ws)

        event = WebSocketEvent(
            event_type=EventType.JOB_ADDED.value,
            data={"meeting_id": "m001"},
        )
        count = await manager.broadcast_event(event)

        assert count == 1
        ws.send_text.assert_called()

        # 전송된 메시지 검증
        sent = json.loads(ws.send_text.call_args[0][0])
        assert sent["event_type"] == "job_added"
        assert sent["data"]["meeting_id"] == "m001"

    @pytest.mark.asyncio
    async def test_다중_연결_브로드캐스트(self) -> None:
        """여러 연결 모두에 이벤트가 전달된다."""
        manager = ConnectionManager()
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        ws3 = AsyncMock()

        await manager.connect(ws1)
        await manager.connect(ws2)
        await manager.connect(ws3)

        event = WebSocketEvent(
            event_type=EventType.PIPELINE_STATUS.value,
            data={"status": "running"},
        )
        count = await manager.broadcast_event(event)

        assert count == 3
        for ws in [ws1, ws2, ws3]:
            ws.send_text.assert_called()

    @pytest.mark.asyncio
    async def test_빈_연결_브로드캐스트(self) -> None:
        """연결이 없으면 0을 반환한다."""
        manager = ConnectionManager()
        event = WebSocketEvent(
            event_type=EventType.HEARTBEAT.value,
        )
        count = await manager.broadcast_event(event)
        assert count == 0

    @pytest.mark.asyncio
    async def test_전송_실패_연결_자동_제거(self) -> None:
        """전송 실패한 연결은 자동으로 제거된다."""
        manager = ConnectionManager()

        ws_ok = AsyncMock()
        ws_fail = AsyncMock()
        ws_fail.send_text.side_effect = RuntimeError("연결 끊김")

        await manager.connect(ws_ok)
        await manager.connect(ws_fail)
        assert manager.connection_count == 2

        event = WebSocketEvent(
            event_type=EventType.SYSTEM_STATUS.value,
            data={"test": True},
        )
        count = await manager.broadcast_event(event)

        assert count == 1  # 1개 성공
        assert manager.connection_count == 1  # 실패한 연결 제거됨

    @pytest.mark.asyncio
    async def test_broadcast_json_헬퍼(self) -> None:
        """broadcast_json이 딕셔너리를 이벤트로 감싸서 전송한다."""
        manager = ConnectionManager()
        ws = AsyncMock()
        await manager.connect(ws)

        count = await manager.broadcast_json(
            {
                "event_type": "job_completed",
                "meeting_id": "m002",
                "status": "completed",
            }
        )

        assert count == 1
        sent = json.loads(ws.send_text.call_args[0][0])
        assert sent["event_type"] == "job_completed"
        assert sent["data"]["meeting_id"] == "m002"


class TestConnectionManagerHeartbeat:
    """ConnectionManager 하트비트 테스트."""

    @pytest.mark.asyncio
    async def test_하트비트_시작_중지(self) -> None:
        """하트비트 태스크를 시작하고 중지할 수 있다."""
        manager = ConnectionManager(heartbeat_interval=1)

        await manager.start_heartbeat()
        assert manager._heartbeat_task is not None
        assert not manager._heartbeat_task.done()

        await manager.stop_heartbeat()
        assert manager._heartbeat_task is None

    @pytest.mark.asyncio
    async def test_하트비트_중복_시작_무시(self) -> None:
        """이미 실행 중인 하트비트를 다시 시작하면 무시한다."""
        manager = ConnectionManager(heartbeat_interval=1)

        await manager.start_heartbeat()
        first_task = manager._heartbeat_task

        await manager.start_heartbeat()  # 두 번째 시작 — 무시
        assert manager._heartbeat_task is first_task

        await manager.stop_heartbeat()

    @pytest.mark.asyncio
    async def test_하트비트_전송(self) -> None:
        """하트비트가 연결된 클라이언트에 전송된다."""
        manager = ConnectionManager(heartbeat_interval=0.1)
        ws = AsyncMock()
        await manager.connect(ws)

        await manager.start_heartbeat()

        # 하트비트 전송 대기
        await asyncio.sleep(0.25)

        await manager.stop_heartbeat()

        # 하트비트 메시지 수신 확인
        assert ws.send_text.call_count >= 1
        # accept() 호출 포함하여 send_text 호출 확인
        sent_calls = ws.send_text.call_args_list
        heartbeat_found = False
        for call in sent_calls:
            msg = json.loads(call[0][0])
            if msg["event_type"] == "heartbeat":
                heartbeat_found = True
                assert "active_connections" in msg["data"]
                break
        assert heartbeat_found, "하트비트 메시지가 전송되지 않았습니다"

    @pytest.mark.asyncio
    async def test_close_all(self) -> None:
        """close_all이 모든 연결과 하트비트를 정리한다."""
        manager = ConnectionManager(heartbeat_interval=1)
        ws1 = AsyncMock()
        ws2 = AsyncMock()

        await manager.connect(ws1)
        await manager.connect(ws2)
        await manager.start_heartbeat()

        assert manager.connection_count == 2

        await manager.close_all()

        assert manager.connection_count == 0
        assert manager._heartbeat_task is None
        ws1.close.assert_called_once()
        ws2.close.assert_called_once()


# === WebSocket 엔드포인트 통합 테스트 ===


class TestWebSocketEndpoint:
    """FastAPI WebSocket 엔드포인트 통합 테스트."""

    def _create_test_app(self) -> FastAPI:
        """테스트용 최소 FastAPI 앱을 생성한다."""
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(ws_router)

        # ConnectionManager를 app.state에 추가
        manager = ConnectionManager(max_connections=3)
        app.state.ws_manager = manager

        return app

    def test_websocket_연결_성공(self) -> None:
        """WebSocket 연결이 성공하고 환영 메시지를 수신한다."""
        from starlette.testclient import TestClient

        app = self._create_test_app()
        client = TestClient(app)

        with client.websocket_connect("/ws/events") as ws:
            # 환영 메시지 수신
            data = ws.receive_json()
            assert data["event_type"] == "system_status"
            assert "WebSocket 연결 성공" in data["data"]["message"]
            assert data["data"]["active_connections"] == 1

    def test_websocket_manager_미초기화(self) -> None:
        """ws_manager가 없으면 거부 메시지를 받는다."""
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        app = FastAPI()
        app.include_router(ws_router)
        # ws_manager를 설정하지 않음

        client = TestClient(app)

        with client.websocket_connect("/ws/events") as ws:
            data = ws.receive_json()
            assert data["event_type"] == "connection_rejected"
            assert "초기화" in data["data"]["reason"]

    def test_websocket_최대_연결_초과(self) -> None:
        """최대 연결 수 초과 시 거부 메시지를 받는다."""
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        app = FastAPI()
        app.include_router(ws_router)
        app.state.ws_manager = ConnectionManager(max_connections=1)

        client = TestClient(app)

        # 첫 번째 연결 성공
        with client.websocket_connect("/ws/events") as ws1:
            data1 = ws1.receive_json()
            assert data1["event_type"] == "system_status"

            # 두 번째 연결 — 거부
            with client.websocket_connect("/ws/events") as ws2:
                data2 = ws2.receive_json()
                assert data2["event_type"] == "connection_rejected"


class TestWebSocketServerIntegration:
    """server.py와의 통합 테스트."""

    def test_server_lifespan에_ws_manager_등록(self) -> None:
        """create_app()으로 생성된 앱에 ws_manager가 등록된다."""
        from starlette.testclient import TestClient

        with (
            patch("api.server.JobQueue"),
            patch("api.server.AsyncJobQueue") as mock_async_queue,
            patch("search.hybrid_search.HybridSearchEngine"),
            patch("search.chat.ChatEngine"),
        ):
            mock_async_queue_inst = AsyncMock()
            mock_async_queue.return_value = mock_async_queue_inst

            from api.server import create_app

            app = create_app()

            with TestClient(app) as _client:
                # ws_manager가 app.state에 존재하는지 확인
                assert hasattr(app.state, "ws_manager")
                assert isinstance(
                    app.state.ws_manager,
                    ConnectionManager,
                )

    def test_server_websocket_엔드포인트_동작(self) -> None:
        """create_app()으로 생성된 앱에서 WebSocket이 동작한다."""
        from starlette.testclient import TestClient

        with (
            patch("api.server.JobQueue"),
            patch("api.server.AsyncJobQueue") as mock_async_queue,
            patch("search.hybrid_search.HybridSearchEngine"),
            patch("search.chat.ChatEngine"),
        ):
            mock_async_queue_inst = AsyncMock()
            mock_async_queue.return_value = mock_async_queue_inst

            from api.server import create_app

            app = create_app()

            with (
                TestClient(app) as client,
                client.websocket_connect("/ws/events") as ws,
            ):
                data = ws.receive_json()
                assert data["event_type"] == "system_status"
                assert data["data"]["active_connections"] == 1


class TestEventType:
    """EventType 열거형 테스트."""

    def test_모든_이벤트_타입_존재(self) -> None:
        """필수 이벤트 타입이 모두 정의되어 있다."""
        expected_types = {
            "heartbeat",
            "pipeline_status",
            "job_added",
            "job_completed",
            "job_failed",
            "system_status",
            "connection_rejected",
            "recording_started",
            "recording_stopped",
            "recording_error",
            "recording_duration",
            "step_progress",
            "reindex_progress",
        }
        actual_types = {e.value for e in EventType}
        assert expected_types == actual_types

    def test_이벤트_타입_문자열_비교(self) -> None:
        """EventType은 문자열과 직접 비교할 수 있다."""
        assert EventType.HEARTBEAT == "heartbeat"
        assert EventType.PIPELINE_STATUS == "pipeline_status"

    def test_녹음_이벤트_타입_존재(self) -> None:
        """녹음 관련 이벤트 타입 4개가 정의되어 있다."""
        assert EventType.RECORDING_STARTED == "recording_started"
        assert EventType.RECORDING_STOPPED == "recording_stopped"
        assert EventType.RECORDING_ERROR == "recording_error"
        assert EventType.RECORDING_DURATION == "recording_duration"
