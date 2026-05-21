"""백필 API 엔드포인트 테스트 모듈 (Phase 4.E, TDD Red 단계)

목적: api/routes.py 에 추가되는 3개의 Wiki 백필 엔드포인트
      (POST /api/wiki/backfill, GET /api/wiki/backfill/{job_id},
       POST /api/wiki/backfill/{job_id}/cancel) 을 검증한다.

테스트 시나리오 (총 4건+):
    1. POST /wiki/backfill — 200 응답 + job_id
    2. GET /wiki/backfill/{job_id} — 진행 상태 조회
    3. GET /wiki/backfill/invalid_id — 404
    4. POST /wiki/backfill/{job_id}/cancel — 취소 처리

mock 전략:
    - scripts.backfill_wiki.backfill 을 AsyncMock 으로 교체.
    - JobQueue.get_all_jobs 빈 리스트 mock.
    - app.state.search_engine / chat_engine MagicMock.

의존성: pytest, fastapi.TestClient, AppConfig, WikiConfig
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from config import AppConfig, PathsConfig, ServerConfig, WikiConfig

# ─── 헬퍼 ───────────────────────────────────────────────────────────────


def _make_test_config(
    tmp_path: Path,
    *,
    wiki_enabled: bool = True,
) -> AppConfig:
    """라우트 테스트용 AppConfig.

    Args:
        tmp_path: pytest tmp_path fixture.
        wiki_enabled: WikiConfig.enabled.

    Returns:
        AppConfig 인스턴스.
    """
    return AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
        wiki=WikiConfig(
            enabled=wiki_enabled,
            root=tmp_path / "wiki",
            dry_run=False,
        ),
    )


def _make_test_app(config: AppConfig) -> Any:
    """테스트용 FastAPI 앱 생성. 외부 의존성은 mocking."""
    from api.server import create_app

    with (
        patch(
            "search.hybrid_search.HybridSearchEngine",
            return_value=MagicMock(),
        ),
        patch(
            "search.chat.ChatEngine",
            return_value=MagicMock(),
        ),
    ):
        app = create_app(config)
    return app


# ─── 시나리오 1: POST /wiki/backfill 시작 ──────────────────────────────


class TestStartBackfill:
    """POST /api/wiki/backfill — 백필 작업 시작."""

    def test_post_backfill_200_응답_job_id(self, tmp_path: Path) -> None:
        """정상 호출 시 202 (또는 200) + job_id 반환."""
        config = _make_test_config(tmp_path)
        app = _make_test_app(config)

        # backfill 자체는 호출되지 않게 mock — 테스트는 등록까지만 검증.
        async def fake_backfill(**kwargs: Any) -> Any:
            from scripts.backfill_wiki import BackfillResult

            return BackfillResult(
                total=0,
                succeeded=0,
                skipped=0,
                failed=0,
            )

        with patch(
            "scripts.backfill_wiki.backfill",
            new=AsyncMock(side_effect=fake_backfill),
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/api/wiki/backfill",
                    json={"dry_run": True},
                )

        assert response.status_code in (200, 202)
        data = response.json()
        assert "job_id" in data

    def test_post_backfill_dry_run_파라미터_전달(self, tmp_path: Path) -> None:
        """dry_run=true 가 응답에 반영되거나 정상 처리되어야 한다."""
        config = _make_test_config(tmp_path)
        app = _make_test_app(config)

        async def fake_backfill(**kwargs: Any) -> Any:
            from scripts.backfill_wiki import BackfillResult

            return BackfillResult(total=0, succeeded=0, skipped=0, failed=0)

        with patch(
            "scripts.backfill_wiki.backfill",
            new=AsyncMock(side_effect=fake_backfill),
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/api/wiki/backfill",
                    json={"dry_run": True, "since": "2026-04-01"},
                )

        assert response.status_code in (200, 202)


# ─── 시나리오 2: GET /wiki/backfill/{job_id} ───────────────────────────


class TestGetBackfillStatus:
    """GET /api/wiki/backfill/{job_id} — 진행 상태 조회."""

    def test_get_status_정상_조회(self, tmp_path: Path) -> None:
        """방금 등록한 job_id 로 조회 시 status 필드 포함 응답."""
        config = _make_test_config(tmp_path)
        app = _make_test_app(config)

        # backfill 이 즉시 완료되도록 한다.
        async def fake_backfill(**kwargs: Any) -> Any:
            from scripts.backfill_wiki import BackfillResult

            return BackfillResult(total=0, succeeded=0, skipped=0, failed=0)

        with patch(
            "scripts.backfill_wiki.backfill",
            new=AsyncMock(side_effect=fake_backfill),
        ):
            with TestClient(app) as client:
                start_resp = client.post(
                    "/api/wiki/backfill",
                    json={"dry_run": True},
                )
                assert start_resp.status_code in (200, 202)
                job_id = start_resp.json()["job_id"]

                # 백그라운드 작업이 끝나도록 잠시 대기.
                # TestClient 는 동기 호출이므로 약간 시도 (최대 5번).
                import time

                for _ in range(10):
                    status_resp = client.get(f"/api/wiki/backfill/{job_id}")
                    if status_resp.status_code == 200:
                        data = status_resp.json()
                        if data["status"] in ("completed", "failed", "cancelled"):
                            break
                    time.sleep(0.05)

                assert status_resp.status_code == 200
                data = status_resp.json()
                assert data["job_id"] == job_id
                assert "status" in data
                assert "processed" in data
                assert "total" in data


# ─── 시나리오 3: GET /wiki/backfill/{invalid_id} → 404 ─────────────────


class TestGetBackfillStatusNotFound:
    """존재하지 않는 job_id 조회 시 404 반환."""

    def test_get_status_없는_id_404(self, tmp_path: Path) -> None:
        """등록되지 않은 job_id 로 조회 시 404."""
        config = _make_test_config(tmp_path)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.get("/api/wiki/backfill/non-existent-id-99999")

        assert response.status_code == 404


# ─── 시나리오 4: POST /wiki/backfill/{job_id}/cancel ───────────────────


class TestCancelBackfill:
    """POST /api/wiki/backfill/{job_id}/cancel — 취소 신호 전송."""

    def test_cancel_정상_취소(self, tmp_path: Path) -> None:
        """실행 중 작업 cancel 호출 시 200 + cancelled 처리."""
        config = _make_test_config(tmp_path)
        app = _make_test_app(config)

        # 실제 backfill 은 잠시 동안 실행되도록 mock.
        async def slow_backfill(**kwargs: Any) -> Any:
            from scripts.backfill_wiki import BackfillResult

            cancel_event = kwargs.get("cancel_event")
            # cancel 신호를 기다린다 (최대 1초).
            for _ in range(20):
                if cancel_event is not None and cancel_event.is_set():
                    break
                await asyncio.sleep(0.05)
            return BackfillResult(total=0, succeeded=0, skipped=0, failed=0)

        with patch(
            "scripts.backfill_wiki.backfill",
            new=AsyncMock(side_effect=slow_backfill),
        ):
            with TestClient(app) as client:
                start_resp = client.post(
                    "/api/wiki/backfill",
                    json={"dry_run": False},
                )
                assert start_resp.status_code in (200, 202)
                job_id = start_resp.json()["job_id"]

                cancel_resp = client.post(f"/api/wiki/backfill/{job_id}/cancel")

        assert cancel_resp.status_code == 200

    def test_cancel_없는_id_404(self, tmp_path: Path) -> None:
        """등록되지 않은 job_id 취소 시도 → 404."""
        config = _make_test_config(tmp_path)
        app = _make_test_app(config)

        with TestClient(app) as client:
            response = client.post("/api/wiki/backfill/non-existent-id-99999/cancel")

        assert response.status_code == 404


class TestDurableBackfillRoutes:
    """Durable Wiki backfill 상태 복구/resume/retry 엔드포인트."""

    def test_get_status는_sqlite_durable_job을_복구한다(self, tmp_path: Path) -> None:
        """DW-F02: 메모리에 없는 running job은 interrupted로 조회된다."""
        config = _make_test_config(tmp_path)
        app = _make_test_app(config)
        from core.wiki.backfill_state import WikiBackfillStateStore

        state_store = WikiBackfillStateStore(config.wiki.resolved_root)
        state_store.create_job(
            job_id="durable-job",
            started_at="2026-05-21T10:00:00",
            request={"since": "2026-05-01", "dry_run": False},
        )
        state_store.update_progress(
            job_id="durable-job",
            processed=2,
            total=5,
            current_meeting_id="1234abcd",
        )

        with TestClient(app) as client:
            response = client.get("/api/wiki/backfill/durable-job")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "interrupted"
        assert data["processed"] == 2
        assert data["total"] == 5
        assert data["current_meeting_id"] == "1234abcd"

    def test_retry_failed는_실패_meeting_ids만_새_job으로_전달한다(
        self,
        tmp_path: Path,
    ) -> None:
        """DW-F03: retry-failed는 durable errors의 meeting_id만 backfill에 전달한다."""
        config = _make_test_config(tmp_path)
        app = _make_test_app(config)
        from core.wiki.backfill_state import WikiBackfillStateStore
        from scripts.backfill_wiki import BackfillError, BackfillResult

        state_store = WikiBackfillStateStore(config.wiki.resolved_root)
        state_store.create_job(job_id="failed-job", started_at="2026-05-21T10:00:00", request={})
        state_store.complete_job(
            job_id="failed-job",
            status="failed",
            finished_at="2026-05-21T10:02:00",
            result=BackfillResult(
                total=3,
                succeeded=1,
                skipped=0,
                failed=2,
                errors=[
                    BackfillError("deadbeef", "summary_missing", "요약 없음"),
                    BackfillError("feedcafe", "utterances_missing", "발화 없음"),
                ],
            ),
        )
        captured: dict[str, Any] = {}

        async def fake_backfill(**kwargs: Any) -> Any:
            captured["meeting_ids"] = kwargs.get("meeting_ids")
            return BackfillResult(total=2, succeeded=2, skipped=0, failed=0)

        with patch("scripts.backfill_wiki.backfill", new=AsyncMock(side_effect=fake_backfill)):
            with TestClient(app) as client:
                response = client.post("/api/wiki/backfill/failed-job/retry-failed")

        assert response.status_code == 202
        assert captured["meeting_ids"] == ["deadbeef", "feedcafe"]

    def test_resume은_기존_request_조건으로_새_job을_시작한다(self, tmp_path: Path) -> None:
        """DW-F04: resume은 durable request의 since/until/meeting_ids/dry_run을 재사용한다."""
        config = _make_test_config(tmp_path)
        app = _make_test_app(config)
        from core.wiki.backfill_state import WikiBackfillStateStore
        from scripts.backfill_wiki import BackfillResult

        state_store = WikiBackfillStateStore(config.wiki.resolved_root)
        state_store.create_job(
            job_id="interrupted-job",
            started_at="2026-05-21T10:00:00",
            request={
                "since": "2026-05-01",
                "until": "2026-05-20",
                "meeting_ids": ["1234abcd"],
                "dry_run": True,
            },
        )
        captured: dict[str, Any] = {}

        async def fake_backfill(**kwargs: Any) -> Any:
            captured["since"] = kwargs.get("since")
            captured["until"] = kwargs.get("until")
            captured["meeting_ids"] = kwargs.get("meeting_ids")
            captured["dry_run"] = kwargs.get("dry_run")
            return BackfillResult(total=1, succeeded=0, skipped=0, failed=0)

        with patch("scripts.backfill_wiki.backfill", new=AsyncMock(side_effect=fake_backfill)):
            with TestClient(app) as client:
                response = client.post("/api/wiki/backfill/interrupted-job/resume")

        assert response.status_code == 202
        assert captured["meeting_ids"] == ["1234abcd"]
        assert captured["dry_run"] is True
        assert str(captured["since"]) == "2026-05-01"
        assert str(captured["until"]) == "2026-05-20"
