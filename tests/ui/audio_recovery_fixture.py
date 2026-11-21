"""실제 정적 UI에 합성 API만 제공하는 오디오 복구 검증 서버."""

from __future__ import annotations

import json
import mimetypes
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

WEB = Path(__file__).resolve().parents[2] / "ui" / "web"
MEETING_ID = "synthetic_audio_recovery"
FIXTURE_CONTROLS = """
<aside id="fixtureControls" style="position:fixed;bottom:0;left:0;right:0;z-index:9000;
background:#fff;color:#111;padding:8px;font:12px sans-serif;display:flex;gap:6px;flex-wrap:wrap"
aria-label="합성 검증 도구">
<span>합성 검증 전용</span>
<button data-fixture="mic-warning">마이크 누락 알림</button>
<button data-fixture="mic-ready">마이크 재시도 준비</button>
<button data-fixture="mic-partial">일부 누락 준비</button>
<button data-fixture="mic-no-new">추가 발화 없음</button>
<button data-fixture="mic-legacy">과거 분리 녹음</button>
<button data-fixture="mic-running">마이크 처리</button>
<button data-fixture="mic-complete">마이크 완료</button>
<button data-fixture="mic-failed">마이크 실패</button>
<button data-fixture="mic-poll-complete">이벤트 없이 완료</button>
<button data-fixture="request-failed">요청 오류 준비</button>
<button data-fixture="request-ready">요청 정상 준비</button>
<button data-fixture="record-start">합성 녹음 시작</button>
<button data-fixture="record-short">짧은 녹음 준비</button>
<button data-fixture="record-register">회의 등록</button>
<button data-fixture="record-queue">전사 대기</button>
<button data-fixture="record-transcribe">전사 처리</button>
<button data-fixture="record-complete">전사 완료</button>
<button data-fixture="record-auto-stop">자동 녹음 종료</button>
<button data-fixture="record-failed">저장 오류</button>
<button data-fixture="replay-failure">같은 오류 다시 알림</button>
<button data-fixture="record-stop-failed">정지 오류 준비</button>
<button data-fixture="late-duration">지연 음량 이벤트</button>
<button data-fixture="old-failure">이전 녹음 오류</button>
</aside>
<script>
document.querySelector('#fixtureControls').addEventListener('click', async event => {
 const button=event.target.closest('[data-fixture]');if(!button)return;
 const response=await fetch('/__fixture/'+button.dataset.fixture,{method:'POST'});
 const data=await response.json();
 for(const item of data.events||[]) document.dispatchEvent(new CustomEvent('ws:'+item.type,{detail:item.data}));
});
</script>
"""
FAKE_SOCKET = """<script>
window.WebSocket=class {
 static CONNECTING=0;static OPEN=1;
 constructor(){this.readyState=1;setTimeout(()=>this.onopen&&this.onopen({}),0);}
 close(){this.readyState=3;}
};
</script>"""


class AudioRecoveryFixture:
    """녹음·전사 API 상태를 메모리에서만 전환한다."""

    def __init__(self, mic_status: str = "empty") -> None:
        self.retry_posts = 0
        self.mic_gets = 0
        self.retry_error = False
        self.stop_error = False
        self.record_number = 0
        self.short = False
        self.capture_status: str | None = None
        self.stop_posts = 0
        self.mic = self.mic_state(mic_status)
        self.recording: dict[str, Any] = {"is_recording": False, "state": "idle"}
        self.utterances = [
            {"speaker": "SPEAKER_00", "text": "원래 상대방 발화", "start": 1, "end": 4}
        ]
        self.url = ""

    def mic_state(self, status: str) -> dict[str, Any]:
        """제품 계약과 같은 필드를 반환한다."""
        return {
            "meeting_id": MEETING_ID,
            "status": status,
            "speech_detected": True,
            "retry_available": status
            in {"empty", "low_volume", "partial", "retry_failed", "complete", "unknown"},
            "added_utterances": None,
            "no_new_speech": False,
            "uncovered_intervals": [[20, 23], [51, 55]] if status == "partial" else [],
            "message": {
                "empty": "마이크에서 음성을 감지했지만 전사된 텍스트가 없습니다.",
                "low_volume": "마이크 음량이 작아 전사를 완료하지 못했습니다.",
                "partial": "마이크 음성 구간 중 전사문이 없는 부분이 있습니다. 마이크만 다시 시도해 주세요.",
                "retry_failed": "마이크 재전사에 실패했습니다. 기존 발화는 보존했습니다.",
                "complete": "마이크 발화를 추가했습니다. 요약 내용을 다시 확인해 주세요.",
            }.get(status, ""),
        }

    def failure(self) -> dict[str, Any]:
        """경로 대신 파일명만 가진 합성 저장 오류를 만든다."""
        return {
            "meeting_id": f"capture_{self.record_number}",
            "code": "capture_failed",
            "error": "저장 공간이 부족해 녹음이 중단됐습니다.",
            "occurred_at": "2026-11-18T12:00:00+09:00",
            "partial_files_preserved": True,
            "partial_file_names": ["synthetic.mic.partial.wav", "<b>system</b>.partial.wav"],
        }

    def control(self, name: str) -> dict[str, Any]:
        """테스트 버튼으로 상태와 합성 이벤트를 함께 바꾼다."""
        events: list[dict[str, Any]] = []

        def event(kind: str, data: dict[str, Any]) -> None:
            events.append({"type": kind, "data": data})

        if name in {"mic-partial", "mic-legacy", "mic-no-new"}:
            status = {"mic-partial": "partial", "mic-legacy": "unknown", "mic-no-new": "complete"}[
                name
            ]
            self.mic = self.mic_state(status)
            if name == "mic-no-new":
                self.mic.update(
                    no_new_speech=True,
                    added_utterances=0,
                    message="새로 추가할 마이크 발화가 없습니다. 기존 발화와 편집을 그대로 보존했습니다.",
                )
            event("job_completed", {"meeting_id": MEETING_ID})
        elif name in {"mic-warning", "mic-ready"}:
            self.mic = self.mic_state("empty")
            self.mic["retry_available"] = name == "mic-ready"
            event(
                "mic_transcription_warning" if name == "mic-warning" else "job_completed", self.mic
            )
        elif name in {"mic-running", "mic-complete", "mic-failed", "mic-poll-complete"}:
            status = {
                "mic-running": "retrying",
                "mic-complete": "complete",
                "mic-failed": "retry_failed",
                "mic-poll-complete": "complete",
            }[name]
            self.mic = self.mic_state(status)
            if status == "complete" and not any(
                u["text"] == "복구된 내 발화" for u in self.utterances
            ):
                self.mic["added_utterances"] = 1
                self.utterances.append(
                    {"speaker": "나", "text": "복구된 내 발화", "start": 2, "end": 3}
                )
            if name != "mic-poll-complete":
                event(
                    "job_completed" if status == "complete" else "pipeline_status",
                    {"meeting_id": MEETING_ID},
                )
        elif name == "request-failed":
            self.retry_error = True
        elif name == "request-ready":
            self.retry_error = False
        elif name == "record-start":
            self.record_number += 1
            self.stop_error = False
            self.short = False
            self.capture_status = None
            self.recording = {
                "is_recording": True,
                "state": "recording",
                "meeting_id": f"capture_{self.record_number}",
                "duration_seconds": 37,
                "audio_levels": {"mic_dbfs": -18, "system_dbfs": -12},
            }
            event("recording_started", self.recording)
        elif name == "record-short":
            self.short = True
        elif name in {"record-register", "record-queue", "record-transcribe", "record-complete"}:
            self.capture_status = {
                "record-register": "recorded",
                "record-queue": "queued",
                "record-transcribe": "transcribing",
                "record-complete": "completed",
            }[name]
            event("job_added", {"meeting_id": f"capture_{self.record_number}"})
        elif name == "record-auto-stop":
            event("recording_stopped", self.finish())
        elif name in {"record-failed", "record-stop-failed"}:
            self.stop_error = True
            self.recording = {"is_recording": False, "state": "idle", "last_error": self.failure()}
            if name == "record-failed":
                event("recording_error", self.failure())
        elif name == "late-duration":
            event(
                "recording_duration",
                {"meeting_id": f"capture_{self.record_number}", "duration_seconds": 999},
            )
        elif name == "replay-failure":
            event("recording_error", dict(reversed(self.failure().items())))
        elif name == "old-failure":
            event("recording_error", {**self.failure(), "meeting_id": "capture_0"})
            event("recording_stopped", {"meeting_id": "capture_0"})
        return {"events": events}

    def finish(self) -> dict[str, Any]:
        """같은 회기의 HTTP·WebSocket·다음 상태 조회에 같은 종료 결과를 제공한다."""
        mid = f"capture_{self.record_number}"
        result = {
            "meeting_id": mid,
            "ended_at": f"2026-11-18T12:01:{self.record_number:02}",
            "files": [] if self.short else [f"{mid}.wav"],
            "expected_files": 1,
            "saved": not self.short,
            "discarded": self.short,
            "reason": "최소 녹음 시간(30초)보다 짧습니다." if self.short else "",
        }
        self.recording = {"is_recording": False, "state": "idle", "last_stop": result}
        return result

    def api(
        self, path: str, method: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        """실제 앱·모델·오디오 파일을 호출하지 않는 합성 응답을 만든다."""
        if path.startswith("/__fixture/"):
            return 200, self.control(path.rsplit("/", 1)[1])
        if path.endswith("/mic-transcription/retry"):
            self.retry_posts += 1
            if self.retry_error:
                return 503, {"detail": "합성 작업 등록 오류"}
            self.mic = self.mic_state("retry_queued")
            return 202, {"meeting_id": MEETING_ID, "job_id": 1, "status": "queued"}
        if path.endswith("/mic-transcription"):
            self.mic_gets += 1
            return 200, self.mic
        if path == "/api/recording/status":
            return 200, self.recording
        if path == "/api/recording/error/acknowledge":
            failure = self.recording.get("last_error") or {}
            matched = payload == {k: failure.get(k) for k in ("meeting_id", "occurred_at")}
            if matched:
                failure["acknowledged"] = True
            return 200, {"acknowledged": matched}
        if path == "/api/recording/stop":
            self.stop_posts += 1
            if self.stop_error:
                return 500, {"detail": "녹음 저장 오류"}
            return 200, {"status": "ok", **self.finish()}
        if path == f"/api/meetings/capture_{self.record_number}":
            if self.capture_status is None:
                return 404, {"detail": "회의 미등록"}
            return 200, {
                "meeting_id": f"capture_{self.record_number}",
                "status": self.capture_status,
            }
        if path.endswith("/transcript"):
            return 200, {
                "meeting_id": MEETING_ID,
                "utterances": self.utterances,
                "speakers": [u["speaker"] for u in self.utterances],
                "source_stage": "corrected",
                "mic_transcription": self.mic,
            }
        meeting = {
            "meeting_id": MEETING_ID,
            "title": "합성 오디오 복구 검증",
            "status": "completed",
            "audio_path": "synthetic.wav",
            "created_at": "2026-11-18T10:00:00",
            "duration_seconds": 60,
            "has_transcript": True,
            "has_summary": False,
        }
        if path == "/api/meetings":
            return 200, {"meetings": [meeting], "total": 1}
        if path == f"/api/meetings/{MEETING_ID}":
            return 200, meeting
        if path.endswith("/summary"):
            return 404, {"detail": "합성 요약 없음"}
        if path.endswith("/pipeline-state"):
            return 200, {"status": "completed", "completed_steps": [], "step_results": []}
        return 200, {}


@contextmanager
def audio_recovery_server(mic_status: str = "empty") -> Iterator[AudioRecoveryFixture]:
    """빈 임시 포트에서 합성 서버를 열고 반드시 닫는다."""
    fixture = AudioRecoveryFixture(mic_status)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            """합성 요청 로그를 출력하지 않는다."""

        def do_GET(self) -> None:
            """합성 API 또는 실제 정적 UI를 제공한다."""
            route = urlsplit(self.path).path
            if route.startswith(("/api/", "/__fixture/")):
                payload = None
                if route == "/api/recording/error/acknowledge":
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                status, data = fixture.api(route, self.command, payload)
                body, content_type = json.dumps(data).encode(), "application/json"
            elif route.startswith("/static/"):
                target = (WEB / route.removeprefix("/static/")).resolve()
                if not target.is_relative_to(WEB) or not target.is_file():
                    self.send_error(404)
                    return
                status, body = 200, target.read_bytes()
                content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            elif route.startswith("/app"):
                content = (WEB / "index.html").read_text()
                content = content.replace("<head>", "<head>" + FAKE_SOCKET)
                content = content.replace("</body>", FIXTURE_CONTROLS + "</body>")
                status, body, content_type = 200, content.encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fixture.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fixture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
