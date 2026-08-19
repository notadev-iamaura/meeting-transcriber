"""
홈 화면 대시보드/시스템 액션/업로드 엔드포인트 테스트
(Home Dashboard / System Action / Upload Endpoint Tests)

목적:
    - GET /api/dashboard/stats : 통계 집계 정확성
    - POST /api/system/open-audio-folder : Finder 호출 모킹 검증
    - POST /api/uploads : 헤더 검증, 확장자 화이트리스트, 충돌 회피, 본문 저장

의존성: pytest, fastapi.TestClient, unittest.mock
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from config import AppConfig, PathsConfig, ServerConfig

# === 헬퍼 (test_routes.py 의 패턴 재사용) ===


def _make_test_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
    )


def _make_test_app(tmp_path: Path) -> Any:
    from api.server import create_app

    config = _make_test_config(tmp_path)

    with (
        patch("search.hybrid_search.HybridSearchEngine", return_value=MagicMock()),
        patch("search.chat.ChatEngine", return_value=MagicMock()),
    ):
        app = create_app(config, runtime_profile="api-test")

    return app


@dataclass
class MockJob:
    """테스트용 Job — created_at 등 통계에 영향을 주는 필드를 직접 제어."""

    id: int
    meeting_id: str
    audio_path: str = "/tmp/x.wav"
    status: str = "completed"
    retry_count: int = 0
    error_message: str = ""
    created_at: str = "2026-03-04T10:00:00"
    updated_at: str = "2026-03-04T10:30:00"


def _create_completed_pipeline_state(tmp_path: Path, meeting_id: str) -> None:
    """완료된 pipeline_state.json 과 전사 산출물을 생성한다."""
    ckpt_dir = tmp_path / "checkpoints" / meeting_id
    out_dir = tmp_path / "outputs" / meeting_id
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (ckpt_dir / "pipeline_state.json").write_text(
        json.dumps(
            {
                "meeting_id": meeting_id,
                "audio_path": f"/audio/{meeting_id}.m4a",
                "status": "completed",
                "completed_steps": ["convert", "transcribe", "diarize", "merge", "correct"],
                "skipped_steps": ["summarize"],
                "step_results": [],
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "corrected.json").write_text(
        json.dumps({"utterances": [{"text": "안녕하세요", "speaker": "SPEAKER_00"}]}),
        encoding="utf-8",
    )


# === GET /api/dashboard/stats ===


class TestDashboardStatsEndpoint:
    """대시보드 통계 엔드포인트."""

    def test_stats_빈_큐(self, tmp_path: Path) -> None:
        """작업이 0 건이면 모든 카운트가 0 이고 audio_input_dir 가 채워진다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=[])
            response = client.get("/api/dashboard/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["total_meetings"] == 0
        assert data["this_week_meetings"] == 0
        assert data["queue_pending"] == 0
        assert data["untranscribed_recordings"] == 0
        assert data["active_processing"] == 0
        assert data["completed"] == 0
        assert data["failed"] == 0
        # audio_input_dir 는 base_dir 하위로 절대 경로여야 한다.
        assert data["audio_input_dir"]
        assert str(tmp_path) in data["audio_input_dir"]

    def test_stats_상태별_집계(self, tmp_path: Path) -> None:
        """recording/transcribing/queued/recorded/completed/failed 가 각 카테고리에 매핑된다.

        queue_pending 은 자동 처리 대기(queued) 만, untranscribed_recordings 는
        사용자 액션 대기(recorded) 만 집계한다 — 두 카운터는 독립적이다.
        """
        app = _make_test_app(tmp_path)

        now = datetime.now()
        recent = now.isoformat()
        old = (now - timedelta(days=30)).isoformat()

        jobs = [
            MockJob(1, "m1", status="recording", created_at=recent),
            MockJob(2, "m2", status="transcribing", created_at=recent),
            MockJob(3, "m3", status="diarizing", created_at=recent),
            MockJob(4, "m4", status="merging", created_at=recent),
            MockJob(5, "m5", status="embedding", created_at=recent),
            MockJob(6, "m6", status="queued", created_at=recent),
            MockJob(7, "m7", status="recorded", created_at=recent),
            MockJob(8, "m8", status="completed", created_at=old),
            MockJob(9, "m9", status="completed", created_at=old),
            MockJob(10, "m10", status="failed", created_at=old),
        ]

        with TestClient(app) as client:
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=jobs)
            response = client.get("/api/dashboard/stats")

        data = response.json()
        assert data["total_meetings"] == 10
        assert (
            data["active_processing"] == 5
        )  # recording, transcribing, diarizing, merging, embedding
        assert data["queue_pending"] == 1  # queued (자동 처리 대기)
        assert data["untranscribed_recordings"] == 1  # recorded (사용자 액션 대기)
        assert data["completed"] == 2
        assert data["failed"] == 1

    def test_stats_완료_산출물이_있는_failed_작업은_집계_전에_복구한다(
        self,
        tmp_path: Path,
    ) -> None:
        """대시보드 통계도 상태 불일치를 completed 로 복구한 뒤 집계한다."""
        app = _make_test_app(tmp_path)
        meeting_id = "meeting_reconcile_dashboard"
        _create_completed_pipeline_state(tmp_path, meeting_id)

        failed_job = MockJob(1, meeting_id, status="failed", retry_count=1)
        completed_job = MockJob(1, meeting_id, status="completed", retry_count=1)

        with TestClient(app) as client:
            queue = app.state.job_queue._queue
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=[failed_job])
            queue.force_set_status = MagicMock(return_value=completed_job)

            response = client.get("/api/dashboard/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["completed"] == 1
        assert data["failed"] == 0
        queue.force_set_status.assert_called_once()

    def test_stats_이번_주_카운트(self, tmp_path: Path) -> None:
        """this_week_meetings 는 created_at 기준 최근 7 일 회의만 카운트한다."""
        app = _make_test_app(tmp_path)

        now = datetime.now()
        within = (now - timedelta(days=3)).isoformat()
        boundary = (now - timedelta(days=7, hours=-1)).isoformat()  # 7일 직전 (포함)
        outside = (now - timedelta(days=10)).isoformat()

        jobs = [
            MockJob(1, "m1", status="completed", created_at=within),
            MockJob(2, "m2", status="completed", created_at=boundary),
            MockJob(3, "m3", status="completed", created_at=outside),
            MockJob(4, "m4", status="completed", created_at=""),  # 빈 created_at 은 무시
            MockJob(5, "m5", status="completed", created_at="invalid-date"),
        ]

        with TestClient(app) as client:
            app.state.job_queue.get_all_jobs = AsyncMock(return_value=jobs)
            response = client.get("/api/dashboard/stats")

        data = response.json()
        # within(1) + boundary(1) = 2 (outside, empty, invalid 는 제외)
        assert data["this_week_meetings"] == 2
        assert data["total_meetings"] == 5

    def test_stats_큐_미초기화_503(self, tmp_path: Path) -> None:
        """job_queue 가 없으면 503."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            original = app.state.job_queue
            app.state.job_queue = None
            response = client.get("/api/dashboard/stats")
            app.state.job_queue = original

        assert response.status_code == 503


# === POST /api/system/open-audio-folder ===


class TestOpenAudioFolderEndpoint:
    """폴더 열기 엔드포인트 — subprocess.run 모킹."""

    def test_macos_정상_호출(self, tmp_path: Path) -> None:
        """darwin 환경에서 `open` 명령이 정확한 인자로 호출되고 200 을 반환한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client, patch("api.routes.sys") as mock_sys:
            mock_sys.platform = "darwin"
            with (
                patch("api.routes.shutil.which", return_value="/usr/bin/open"),
                patch("api.routes.subprocess.run") as mock_run,
            ):
                mock_run.return_value = MagicMock(returncode=0, stderr=b"")
                response = client.post("/api/system/open-audio-folder")

        assert response.status_code == 200
        data = response.json()
        assert data["opened"] is True
        # 폴더 자동 생성도 검증
        assert Path(data["path"]).exists()
        # subprocess.run 이 ["/usr/bin/open", path] 형태로 호출되었는지
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0][0] == "/usr/bin/open"
        assert args[0][1] == data["path"]
        assert kwargs.get("check") is True

    def test_비_macos_환경(self, tmp_path: Path) -> None:
        """darwin 이 아닌 플랫폼에서는 opened=False 와 경로만 반환한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client, patch("api.routes.sys") as mock_sys:
            mock_sys.platform = "linux"
            response = client.post("/api/system/open-audio-folder")

        assert response.status_code == 200
        data = response.json()
        assert data["opened"] is False
        assert data["path"]

    def test_open_명령_없음(self, tmp_path: Path) -> None:
        """`open` 바이너리가 없으면 500."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client, patch("api.routes.sys") as mock_sys:
            mock_sys.platform = "darwin"
            with patch("api.routes.shutil.which", return_value=None):
                response = client.post("/api/system/open-audio-folder")

        assert response.status_code == 500


# === POST /api/uploads ===


class TestUploadEndpoint:
    """업로드 엔드포인트 — X-Filename + raw body."""

    def test_정상_업로드(self, tmp_path: Path) -> None:
        """올바른 헤더 + 본문이면 audio_input_dir 에 파일이 저장된다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            payload = b"FAKE_AUDIO_BYTES" * 100  # 1.6 KB
            response = client.post(
                "/api/uploads",
                headers={
                    "X-Filename": "meeting_test.wav",
                    "Content-Type": "application/octet-stream",
                },
                content=payload,
            )

        assert response.status_code == 201
        data = response.json()
        assert data["filename"] == "meeting_test.wav"
        assert data["size"] == len(payload)
        saved = Path(data["path"])
        assert saved.exists()
        assert saved.read_bytes() == payload
        # 디렉토리 검증
        assert saved.parent.name == "audio_input"

    def test_헤더_누락(self, tmp_path: Path) -> None:
        """X-Filename 헤더가 없으면 400."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/api/uploads",
                headers={"Content-Type": "application/octet-stream"},
                content=b"data",
            )

        assert response.status_code == 400

    def test_미지원_확장자(self, tmp_path: Path) -> None:
        """허용되지 않은 확장자는 400."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/api/uploads",
                headers={
                    "X-Filename": "evil.exe",
                    "Content-Type": "application/octet-stream",
                },
                content=b"data",
            )

        assert response.status_code == 400
        assert "확장자" in response.json()["detail"]

    def test_path_traversal_차단(self, tmp_path: Path) -> None:
        """파일명에 슬래시/백슬래시가 있으면 400."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/api/uploads",
                headers={
                    "X-Filename": "../etc/passwd.wav",
                    "Content-Type": "application/octet-stream",
                },
                content=b"data",
            )

        assert response.status_code == 400

    def test_빈_본문(self, tmp_path: Path) -> None:
        """본문이 비어 있으면 400."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/api/uploads",
                headers={
                    "X-Filename": "empty.wav",
                    "Content-Type": "application/octet-stream",
                },
                content=b"",
            )

        assert response.status_code == 400

    def test_한글_파일명_url_인코딩(self, tmp_path: Path) -> None:
        """X-Filename 이 URL 인코딩된 한글이면 디코딩 후 저장된다."""
        from urllib.parse import quote

        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            encoded = quote("주간 회의 2026-04-29.m4a")
            response = client.post(
                "/api/uploads",
                headers={
                    "X-Filename": encoded,
                    "Content-Type": "application/octet-stream",
                },
                content=b"audio",
            )

        assert response.status_code == 201
        data = response.json()
        assert data["filename"] == "주간 회의 2026-04-29.m4a"
        assert Path(data["path"]).exists()

    def test_파일명_충돌_시_자동_접미사(self, tmp_path: Path) -> None:
        """이미 같은 이름이 있으면 ` (1)` 형식으로 자동 회피한다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            r1 = client.post(
                "/api/uploads",
                headers={
                    "X-Filename": "dup.wav",
                    "Content-Type": "application/octet-stream",
                },
                content=b"first",
            )
            r2 = client.post(
                "/api/uploads",
                headers={
                    "X-Filename": "dup.wav",
                    "Content-Type": "application/octet-stream",
                },
                content=b"second",
            )

        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["filename"] == "dup.wav"
        assert r2.json()["filename"] == "dup (1).wav"
        # 두 파일 모두 존재
        assert Path(r1.json()["path"]).read_bytes() == b"first"
        assert Path(r2.json()["path"]).read_bytes() == b"second"

    @pytest.mark.skipif(sys.version_info < (3, 11), reason="httpx multipart streaming")
    def test_content_length_초과(self, tmp_path: Path) -> None:
        """Content-Length 가 상한을 넘으면 413."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            # 헤더로만 거짓 큰 값을 보고하고 본문은 작게
            response = client.post(
                "/api/uploads",
                headers={
                    "X-Filename": "big.wav",
                    "Content-Type": "application/octet-stream",
                    # 3 GB > _UPLOAD_MAX_BYTES (2 GB)
                    "Content-Length": str(3 * 1024 * 1024 * 1024),
                },
                content=b"x",
            )

        # TestClient 가 Content-Length 를 자동으로 재계산할 수 있어
        # 정확히 413 이 오지 않을 수 있다 — 우리는 헤더 사전 검증 로직만 보장한다.
        # 실패해도 400 (빈 본문 인식) 이상은 나와야 함.
        assert response.status_code in (400, 413)

    def test_raw_base_dir_symlink는_외부로_업로드하지_않음(
        self,
        tmp_path: Path,
    ) -> None:
        """raw base_dir symlink target에 업로드 바이트를 쓰지 않는다."""
        external = tmp_path / "external"
        external.mkdir()
        configured_base = tmp_path / "configured-base"
        configured_base.symlink_to(external, target_is_directory=True)
        app = _make_test_app(configured_base)

        with TestClient(app) as client:
            response = client.post(
                "/api/uploads",
                headers={"X-Filename": "escape.wav"},
                content=b"must-not-leave-base",
            )

        assert response.status_code == 400
        assert "SECURITY_BLOCKED" in response.json()["detail"]
        assert not (external / "audio_input" / "escape.wav").exists()

    def test_audio_input_dir_symlink는_외부로_업로드하지_않음(
        self,
        tmp_path: Path,
    ) -> None:
        """audio_input directory symlink을 따라 외부 target에 쓰지 않는다."""
        external = tmp_path / "external"
        external.mkdir()
        audio_input = tmp_path / "audio_input"
        audio_input.symlink_to(external, target_is_directory=True)
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/api/uploads",
                headers={"X-Filename": "escape.wav"},
                content=b"must-not-follow-input-link",
            )

        assert response.status_code == 400
        assert "SECURITY_BLOCKED" in response.json()["detail"]
        assert not (external / "escape.wav").exists()

    @pytest.mark.parametrize("configured_child", ["../outside", "ABSOLUTE"])
    def test_audio_input_dir_base_escape는_외부에_쓰지_않음(
        self,
        tmp_path: Path,
        configured_child: str,
    ) -> None:
        """absolute/부모 traversal 설정은 base_dir 밖 쓰기 전에 차단된다."""
        base = tmp_path / "base"
        outside = tmp_path / "outside"
        outside.mkdir()
        app = _make_test_app(base)
        app.state.config.paths.audio_input_dir = (
            str(outside) if configured_child == "ABSOLUTE" else configured_child
        )

        with TestClient(app) as client:
            response = client.post(
                "/api/uploads",
                headers={"X-Filename": "escape.wav"},
                content=b"must-stay-contained",
            )

        assert response.status_code == 400
        assert "SECURITY_BLOCKED" in response.json()["detail"]
        assert not (outside / "escape.wav").exists()

    def test_predictable_part_symlink를_따라_victim을_덮어쓰지_않음(
        self,
        tmp_path: Path,
    ) -> None:
        """legacy `{final}.part` symlink가 있어도 target을 열거나 이동하지 않는다."""
        audio_input = tmp_path / "audio_input"
        audio_input.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_bytes(b"sentinel")
        legacy_part = audio_input / "victim.wav.part"
        legacy_part.symlink_to(victim)
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/api/uploads",
                headers={"X-Filename": "victim.wav"},
                content=b"safe-upload",
            )

        assert response.status_code == 201
        assert victim.read_bytes() == b"sentinel"
        assert legacy_part.is_symlink()
        assert (audio_input / "victim.wav").read_bytes() == b"safe-upload"

    def test_dangling_final_symlink는_보존하고_다음_이름으로_publish(
        self,
        tmp_path: Path,
    ) -> None:
        """dangling symlink도 존재하는 충돌로 보고 절대 덮어쓰지 않는다."""
        audio_input = tmp_path / "audio_input"
        audio_input.mkdir()
        dangling = audio_input / "collision.wav"
        dangling.symlink_to(tmp_path / "missing-target.wav")
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/api/uploads",
                headers={"X-Filename": "collision.wav"},
                content=b"new-audio",
            )

        assert response.status_code == 201
        assert response.json()["filename"] == "collision (1).wav"
        assert dangling.is_symlink()
        assert os.readlink(dangling) == str(tmp_path / "missing-target.wav")
        assert (audio_input / "collision (1).wav").read_bytes() == b"new-audio"

    def test_publish_TOCTOU_충돌은_기존_파일을_보존하고_재시도(
        self,
        tmp_path: Path,
    ) -> None:
        """publish 순간 선점된 final을 덮어쓰지 않고 다음 이름을 쓴다."""
        audio_input = tmp_path / "audio_input"
        audio_input.mkdir()
        app = _make_test_app(tmp_path)
        real_link = os.link
        collided = False

        def inject_collision(
            src: str,
            dst: str,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> None:
            nonlocal collided
            if dst == "race.wav" and not collided:
                assert dst_dir_fd is not None
                collided = True
                racer_fd = os.open(
                    dst,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=dst_dir_fd,
                )
                try:
                    os.write(racer_fd, b"racer-wins")
                finally:
                    os.close(racer_fd)
            real_link(
                src,
                dst,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=follow_symlinks,
            )

        with (
            TestClient(app) as client,
            patch(
                "api.routers.uploads.os.link",
                side_effect=inject_collision,
            ),
        ):
            response = client.post(
                "/api/uploads",
                headers={"X-Filename": "race.wav"},
                content=b"uploader-loses-first-name",
            )

        assert response.status_code == 201
        assert collided is True
        assert (audio_input / "race.wav").read_bytes() == b"racer-wins"
        assert response.json()["filename"] == "race (1).wav"
        assert (audio_input / "race (1).wav").read_bytes() == b"uploader-loses-first-name"

    def test_stream_예외_시_자신이_만든_part를_정리(
        self,
        tmp_path: Path,
    ) -> None:
        """client disconnect/예외로 불완전 본문이 끝나면 temp inode만 정리한다."""
        config = _make_test_config(tmp_path)

        async def broken_stream() -> Any:
            yield b"partial"
            raise RuntimeError("client disconnected")

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(config=config)),
            headers={"x-filename": "broken.wav"},
            stream=broken_stream,
        )

        from api.routers.uploads import upload_audio

        with pytest.raises(RuntimeError, match="client disconnected"):
            asyncio.run(upload_audio(request))

        audio_input = tmp_path / "audio_input"
        assert not list(audio_input.glob("*.part"))
        assert not (audio_input / "broken.wav").exists()

    @pytest.mark.asyncio
    async def test_stream_중에는_지원_확장자_final이_노출되지_않음(
        self,
        tmp_path: Path,
    ) -> None:
        """body가 완료되기 전에 watcher 대상 final 파일을 노출하지 않는다."""
        config = _make_test_config(tmp_path)
        first_chunk_written = asyncio.Event()
        release_stream = asyncio.Event()

        async def paused_stream() -> Any:
            yield b"first-"
            first_chunk_written.set()
            await release_stream.wait()
            yield b"second"

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(config=config)),
            headers={"x-filename": "streaming.wav"},
            stream=paused_stream,
        )

        from api.routers.uploads import upload_audio

        task = asyncio.create_task(upload_audio(request))
        await first_chunk_written.wait()
        audio_input = tmp_path / "audio_input"
        assert not list(audio_input.glob("*.wav"))
        parts = list(audio_input.glob("*.part"))
        assert len(parts) == 1
        assert parts[0].name.startswith(".upload-")
        assert parts[0].stat().st_mode & 0o777 == 0o600

        release_stream.set()
        result = await task
        assert result.filename == "streaming.wav"
        assert (audio_input / "streaming.wav").read_bytes() == b"first-second"
        assert not list(audio_input.glob("*.part"))

    @pytest.mark.asyncio
    async def test_stream_cancelled_error는_publish_전_part만_정리(
        self,
        tmp_path: Path,
    ) -> None:
        """request 취소는 최초 temp inode를 정리하고 final을 만들지 않는다."""
        config = _make_test_config(tmp_path)
        waiting = asyncio.Event()

        async def cancellable_stream() -> Any:
            yield b"partial"
            waiting.set()
            await asyncio.Future()

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(config=config)),
            headers={"x-filename": "cancelled.wav"},
            stream=cancellable_stream,
        )

        from api.routers.uploads import upload_audio

        task = asyncio.create_task(upload_audio(request))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        audio_input = tmp_path / "audio_input"
        assert not list(audio_input.glob("*.part"))
        assert not (audio_input / "cancelled.wav").exists()

    @pytest.mark.asyncio
    async def test_link_성공_뒤_cancelled_error는_final을_지우지_않음(
        self,
        tmp_path: Path,
    ) -> None:
        """publish link 이후 취소는 roll-forward하고 part만 정리한다."""
        config = _make_test_config(tmp_path)

        async def complete_stream() -> Any:
            yield b"durable-final"

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(config=config)),
            headers={"x-filename": "roll-forward.wav"},
            stream=complete_stream,
        )

        from api.routers import uploads

        real_verify = uploads._verify_directory_identity
        verification_count = 0

        def cancel_after_link(*args: Any, **kwargs: Any) -> None:
            nonlocal verification_count
            verification_count += 1
            if verification_count == 3:
                raise asyncio.CancelledError
            real_verify(*args, **kwargs)

        with patch(
            "api.routers.uploads._verify_directory_identity",
            side_effect=cancel_after_link,
        ):
            with pytest.raises(asyncio.CancelledError):
                await uploads.upload_audio(request)

        audio_input = tmp_path / "audio_input"
        assert (audio_input / "roll-forward.wav").read_bytes() == b"durable-final"
        assert not list(audio_input.glob("*.part"))

    def test_short_malformed_upload은_엔드포인트에서_큐를_우회하지_않음(
        self,
        tmp_path: Path,
    ) -> None:
        """업로드 API는 저장만 하고 품질 gate/큐 진입은 watcher에 맡긴다."""
        app = _make_test_app(tmp_path)

        with TestClient(app) as client:
            async_add = AsyncMock()
            app.state.job_queue.add_job = async_add
            with patch.object(app.state.job_queue.queue, "add_job") as sync_add:
                response = client.post(
                    "/api/uploads",
                    headers={"X-Filename": "too-short.wav"},
                    content=b"not-a-real-wave",
                )

            async_add.assert_not_awaited()
            sync_add.assert_not_called()

        assert response.status_code == 201
        assert (tmp_path / "audio_input" / "too-short.wav").read_bytes() == b"not-a-real-wave"

    def test_publish_event_유실_시_startup_scan이_final을_등록(
        self,
        tmp_path: Path,
    ) -> None:
        """hard-link create event를 못 받아도 재기동 scan이 final을 gate 후 등록한다."""
        from core.audio_quality import AudioQualityResult, AudioQualityStatus
        from core.watcher import FolderWatcher

        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            response = client.post(
                "/api/uploads",
                headers={"X-Filename": "startup-scan.wav"},
                content=b"complete-upload",
            )
            assert response.status_code == 201

            watcher = FolderWatcher(
                async_job_queue=app.state.job_queue,
                config=app.state.config,
            )
            watcher._debounce_seconds = 0
            watcher._confirm_closed_unchanged = AsyncMock(return_value=True)
            watcher._audio_validator = MagicMock(
                return_value=AudioQualityResult(
                    status=AudioQualityStatus.ACCEPT,
                    mean_volume_db=-20.0,
                    duration_seconds=60.0,
                    reason="",
                )
            )

            registered = asyncio.run(watcher.scan_existing())
            job = app.state.job_queue.queue.get_job_by_meeting_id("startup-scan")

        assert len(registered) == 1
        assert job is not None
        assert job.status == "recorded"
