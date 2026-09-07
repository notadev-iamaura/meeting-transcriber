"""의존성 없이 실제 WebSocket 구현의 Origin 경계를 격리 확인한다.

FastAPI 타입/데코레이터만 대체한다. HTTP 서버/브라우저 E2E 검증은 아니다.
실제 오디오, 사용자 데이터, 네트워크를 사용하지 않는다.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import types
from pathlib import Path

ROOT = Path("/Users/youngouksong/projects/meeting-transcriber")


class RouterStub:
    def websocket(self, path):
        return lambda fn: fn


class DisconnectStub(Exception):
    pass


source = ast.parse((ROOT / "api/websocket.py").read_text())
source.body = [
    node
    for node in source.body
    if not (isinstance(node, ast.ImportFrom) and node.module == "fastapi")
]
module = types.ModuleType("mt_ws_audit")
sys.modules[module.__name__] = module
module.__dict__.update(APIRouter=RouterStub, WebSocket=object, WebSocketDisconnect=DisconnectStub)
exec(compile(source, str(ROOT / "api/websocket.py"), "exec"), module.__dict__)


class SocketStub:
    def __init__(self, manager):
        self.headers = {"origin": "https://untrusted.example", "host": "127.0.0.1:8765"}
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(ws_manager=manager))
        self.accepted = False
        self.messages = []
        self.manager = manager

    async def accept(self):
        self.accepted = True

    async def send_text(self, text):
        self.messages.append(json.loads(text))

    async def close(self, **kwargs):
        pass

    async def receive_text(self):
        await self.manager.broadcast_event(
            module.WebSocketEvent(
                event_type="recording_stopped",
                data={"meeting_id": "synthetic", "file_path": "/synthetic/private.wav"},
            )
        )
        raise DisconnectStub()


async def main():
    socket = SocketStub(module.ConnectionManager())
    await module.websocket_events(socket)
    assert socket.accepted
    assert any(m["data"].get("file_path") == "/synthetic/private.wav" for m in socket.messages)
    print(
        json.dumps(
            {
                "hostile_origin_accepted": socket.accepted,
                "events_received": [m["event_type"] for m in socket.messages],
                "synthetic_path_received": True,
                "scope": "actual module with FastAPI types/decorator stubbed; no browser/server",
            },
            ensure_ascii=False,
        )
    )


asyncio.run(main())
