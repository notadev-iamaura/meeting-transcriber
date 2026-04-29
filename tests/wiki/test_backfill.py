"""백필 스크립트 테스트 모듈 (Phase 4.E, TDD Red 단계)

목적: scripts/backfill_wiki.py 의 backfill() 함수를 검증한다.
회의 1건 실패 격리, 진행 콜백, cancel 신호, dry_run, 날짜 필터 등.

테스트 시나리오 (총 7건+):
    1. 빈 범위 — 처리 회의 0건, 정상 종료
    2. 여러 회의 성공 — 5건 mock → 모두 성공
    3. 1건 실패, 다음 진행 — 3번째 실패 → errors=[1] + 4번째 정상
    4. cancel_event 작동 — 중간 cancel → 현재 회의 끝나고 종료
    5. dry_run 모드 — 실제 컴파일 안 함, succeeded=0
    6. progress_callback 호출 — 매 회의마다
    7. duration_seconds 기록 — 실제 경과 시간

mock 전략:
    - WikiCompilerV2.compile_meeting → AsyncMock 으로 대체
    - JobQueue → in-memory 가짜 객체
    - 체크포인트 파일 → tmp_path 에 직접 생성

의존성: pytest, pytest-asyncio, scripts.backfill_wiki
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig, PathsConfig, ServerConfig, WikiConfig

# ─── 헬퍼 ───────────────────────────────────────────────────────────────


def _make_test_config(tmp_path: Path) -> AppConfig:
    """백필 테스트용 AppConfig 를 생성한다.

    wiki.enabled=True + wiki.dry_run=False 로 실 컴파일 경로를 활성화하되,
    실제 LLM 호출은 mock 으로 대체한다.

    Args:
        tmp_path: pytest tmp_path fixture.

    Returns:
        백필 경로용 AppConfig.
    """
    return AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
        wiki=WikiConfig(
            enabled=True,
            root=tmp_path / "wiki",
            dry_run=False,
        ),
    )


def _seed_meeting_outputs(
    config: AppConfig,
    meeting_id: str,
    *,
    with_summary: bool = True,
    with_corrected: bool = True,
) -> None:
    """회의 outputs / checkpoints 디렉토리에 가짜 데이터 시드.

    백필이 회의별 utterances + summary 를 로드하는 경로를 시뮬레이션한다.

    Args:
        config: AppConfig.
        meeting_id: 회의 식별자.
        with_summary: outputs/{id}/summary.md 생성 여부.
        with_corrected: outputs/{id}/corrected.json 생성 여부.
    """
    outputs_dir = config.paths.resolved_outputs_dir / meeting_id
    outputs_dir.mkdir(parents=True, exist_ok=True)

    if with_summary:
        (outputs_dir / "summary.md").write_text(
            "# 요약\n\n샘플 회의 요약입니다.\n", encoding="utf-8"
        )

    if with_corrected:
        utterances = [
            {
                "speaker": "SPEAKER_00",
                "text": f"샘플 발화 {meeting_id}",
                "start": 0.0,
                "end": 5.0,
            }
        ]
        (outputs_dir / "corrected.json").write_text(
            json.dumps({"utterances": utterances}, ensure_ascii=False),
            encoding="utf-8",
        )


def _make_fake_job(meeting_id: str, status: str = "completed") -> Any:
    """JobQueue.Job 의 mock 인스턴스를 만든다."""
    job = MagicMock()
    job.meeting_id = meeting_id
    job.status = status
    job.created_at = "2026-04-29T10:00:00"
    return job


# ─── 시나리오 1: 빈 범위 ────────────────────────────────────────────────


class TestBackfillEmpty:
    """빈 회의 목록 시나리오."""

    @pytest.mark.asyncio
    async def test_backfill_빈_목록_정상_종료(self, tmp_path: Path) -> None:
        """JobQueue 에 회의가 0건이면 succeeded=0 으로 정상 종료."""
        from scripts.backfill_wiki import backfill

        config = _make_test_config(tmp_path)
        fake_queue = MagicMock()
        fake_queue.get_all_jobs = MagicMock(return_value=[])

        result = await backfill(
            config=config,
            job_queue=fake_queue,
        )

        assert result.total == 0
        assert result.succeeded == 0
        assert result.failed == 0
        assert result.errors == []
        assert result.duration_seconds >= 0.0


# ─── 시나리오 2: 여러 회의 성공 ─────────────────────────────────────────


class TestBackfillSuccess:
    """전체 회의 성공 시나리오."""

    @pytest.mark.asyncio
    async def test_backfill_5건_모두_성공(self, tmp_path: Path) -> None:
        """5개 회의가 mock 컴파일러로 모두 성공해야 한다."""
        from scripts.backfill_wiki import backfill

        config = _make_test_config(tmp_path)
        meeting_ids = [f"abc{i:05d}" for i in range(5)]
        for mid in meeting_ids:
            _seed_meeting_outputs(config, mid)

        fake_queue = MagicMock()
        fake_queue.get_all_jobs = MagicMock(
            return_value=[_make_fake_job(mid) for mid in meeting_ids]
        )

        # WikiCompilerV2 가 항상 성공하는 mock
        async def fake_compile(**kwargs: Any) -> Any:
            return MagicMock(
                meeting_id=kwargs["meeting_id"],
                pages_created=["x.md"],
                pages_updated=[],
                pages_pending=[],
                pages_rejected=[],
            )

        with patch(
            "scripts.backfill_wiki._compile_single_meeting",
            new=AsyncMock(side_effect=fake_compile),
        ):
            result = await backfill(
                config=config,
                job_queue=fake_queue,
            )

        assert result.total == 5
        assert result.succeeded == 5
        assert result.failed == 0
        assert result.errors == []


# ─── 시나리오 3: 1건 실패, 다음 진행 ───────────────────────────────────


class TestBackfillFailureIsolated:
    """회의 1건 실패해도 다음 회의가 정상 진행되는 격리 정책."""

    @pytest.mark.asyncio
    async def test_backfill_3번째_실패_4번째_진행(
        self, tmp_path: Path
    ) -> None:
        """3번째 회의가 예외를 던져도 4번째가 정상 처리되어야 한다."""
        from scripts.backfill_wiki import backfill

        config = _make_test_config(tmp_path)
        meeting_ids = [f"abc{i:05d}" for i in range(5)]
        for mid in meeting_ids:
            _seed_meeting_outputs(config, mid)

        fake_queue = MagicMock()
        fake_queue.get_all_jobs = MagicMock(
            return_value=[_make_fake_job(mid) for mid in meeting_ids]
        )

        # 3번째(index=2) 회의에서 의도적 예외
        call_log: list[str] = []

        async def fake_compile(**kwargs: Any) -> Any:
            mid = kwargs["meeting_id"]
            call_log.append(mid)
            if mid == meeting_ids[2]:
                raise RuntimeError("의도된 컴파일 실패 — 격리 검증")
            return MagicMock(
                meeting_id=mid,
                pages_created=[],
                pages_updated=[],
                pages_pending=[],
                pages_rejected=[],
            )

        with patch(
            "scripts.backfill_wiki._compile_single_meeting",
            new=AsyncMock(side_effect=fake_compile),
        ):
            result = await backfill(
                config=config,
                job_queue=fake_queue,
            )

        # 5건 모두 시도되었지만 3번째만 실패.
        assert result.total == 5
        assert result.succeeded == 4
        assert result.failed == 1
        assert len(result.errors) == 1
        assert result.errors[0].meeting_id == meeting_ids[2]
        # 4번째, 5번째도 정상 호출되었는지 확인.
        assert meeting_ids[3] in call_log
        assert meeting_ids[4] in call_log


# ─── 시나리오 4: cancel_event 작동 ──────────────────────────────────────


class TestBackfillCancel:
    """cancel_event 가 set 되면 현재 회의 끝낸 후 즉시 종료."""

    @pytest.mark.asyncio
    async def test_backfill_cancel_중간_종료(self, tmp_path: Path) -> None:
        """3번째 회의 처리 중 cancel_event.set() → 4·5번째는 처리되지 않아야."""
        from scripts.backfill_wiki import backfill

        config = _make_test_config(tmp_path)
        meeting_ids = [f"abc{i:05d}" for i in range(5)]
        for mid in meeting_ids:
            _seed_meeting_outputs(config, mid)

        fake_queue = MagicMock()
        fake_queue.get_all_jobs = MagicMock(
            return_value=[_make_fake_job(mid) for mid in meeting_ids]
        )

        cancel_event = asyncio.Event()
        processed: list[str] = []

        async def fake_compile(**kwargs: Any) -> Any:
            mid = kwargs["meeting_id"]
            processed.append(mid)
            # 2번째 회의 직후 취소 신호
            if mid == meeting_ids[1]:
                cancel_event.set()
            return MagicMock(
                meeting_id=mid,
                pages_created=[],
                pages_updated=[],
                pages_pending=[],
                pages_rejected=[],
            )

        with patch(
            "scripts.backfill_wiki._compile_single_meeting",
            new=AsyncMock(side_effect=fake_compile),
        ):
            result = await backfill(
                config=config,
                job_queue=fake_queue,
                cancel_event=cancel_event,
            )

        # 첫 두 건은 성공해야 함. 3번째 이후는 cancel 로 skip.
        assert meeting_ids[0] in processed
        assert meeting_ids[1] in processed
        # 3번째 이후는 처리되지 않았어야 함.
        assert meeting_ids[2] not in processed
        assert meeting_ids[3] not in processed
        assert result.succeeded == 2


# ─── 시나리오 5: dry_run 모드 ───────────────────────────────────────────


class TestBackfillDryRun:
    """dry_run=True 시 실제 컴파일러를 호출하지 않는다."""

    @pytest.mark.asyncio
    async def test_backfill_dry_run_컴파일_없음(self, tmp_path: Path) -> None:
        """dry_run=True → succeeded=0, total>0 (목록만 시뮬레이션)."""
        from scripts.backfill_wiki import backfill

        config = _make_test_config(tmp_path)
        meeting_ids = [f"abc{i:05d}" for i in range(3)]
        for mid in meeting_ids:
            _seed_meeting_outputs(config, mid)

        fake_queue = MagicMock()
        fake_queue.get_all_jobs = MagicMock(
            return_value=[_make_fake_job(mid) for mid in meeting_ids]
        )

        compile_mock = AsyncMock()
        with patch(
            "scripts.backfill_wiki._compile_single_meeting",
            new=compile_mock,
        ):
            result = await backfill(
                config=config,
                job_queue=fake_queue,
                dry_run=True,
            )

        # dry_run 은 컴파일 함수를 호출하지 않아야 한다.
        compile_mock.assert_not_called()
        assert result.total == 3
        assert result.succeeded == 0
        # dry_run 결과는 errors 없이 종료.
        assert result.failed == 0


# ─── 시나리오 6: progress_callback ──────────────────────────────────────


class TestBackfillProgressCallback:
    """progress_callback 이 매 회의마다 호출되는지 검증."""

    @pytest.mark.asyncio
    async def test_backfill_progress_콜백_호출(self, tmp_path: Path) -> None:
        """3건 회의 처리 시 progress_callback 이 정확히 3번 호출된다."""
        from scripts.backfill_wiki import backfill

        config = _make_test_config(tmp_path)
        meeting_ids = [f"abc{i:05d}" for i in range(3)]
        for mid in meeting_ids:
            _seed_meeting_outputs(config, mid)

        fake_queue = MagicMock()
        fake_queue.get_all_jobs = MagicMock(
            return_value=[_make_fake_job(mid) for mid in meeting_ids]
        )

        progress_log: list[tuple[int, int, str]] = []

        def progress_cb(processed: int, total: int, current: str) -> None:
            progress_log.append((processed, total, current))

        async def fake_compile(**kwargs: Any) -> Any:
            return MagicMock(
                meeting_id=kwargs["meeting_id"],
                pages_created=[],
                pages_updated=[],
                pages_pending=[],
                pages_rejected=[],
            )

        with patch(
            "scripts.backfill_wiki._compile_single_meeting",
            new=AsyncMock(side_effect=fake_compile),
        ):
            await backfill(
                config=config,
                job_queue=fake_queue,
                progress_callback=progress_cb,
            )

        assert len(progress_log) == 3
        # 처리 카운트가 1, 2, 3 으로 증가해야 한다.
        assert [p[0] for p in progress_log] == [1, 2, 3]
        assert all(p[1] == 3 for p in progress_log)
        # current_meeting_id 가 순서대로 전달.
        assert [p[2] for p in progress_log] == meeting_ids


# ─── 시나리오 7: duration_seconds ───────────────────────────────────────


class TestBackfillDuration:
    """BackfillResult.duration_seconds 가 실제 경과 시간을 기록한다."""

    @pytest.mark.asyncio
    async def test_backfill_경과시간_기록(self, tmp_path: Path) -> None:
        """duration_seconds >= 0 이며 재현 가능해야 한다."""
        from scripts.backfill_wiki import backfill

        config = _make_test_config(tmp_path)
        fake_queue = MagicMock()
        fake_queue.get_all_jobs = MagicMock(return_value=[])

        result = await backfill(config=config, job_queue=fake_queue)

        assert result.duration_seconds >= 0.0
        assert isinstance(result.duration_seconds, float)


# ─── 시나리오 8: 날짜 필터 ──────────────────────────────────────────────


class TestBackfillDateFilter:
    """since/until 날짜 필터가 회의 목록을 정확히 필터링한다."""

    @pytest.mark.asyncio
    async def test_backfill_since_until_필터(self, tmp_path: Path) -> None:
        """since=2026-04-15 ~ until=2026-04-20 범위 회의만 처리."""
        from scripts.backfill_wiki import backfill

        config = _make_test_config(tmp_path)

        # 5개 회의 — 날짜는 created_at 으로 결정
        jobs = []
        for i, day in enumerate([10, 14, 16, 19, 25]):
            job = MagicMock()
            job.meeting_id = f"abc{i:05d}"
            job.status = "completed"
            job.created_at = f"2026-04-{day:02d}T10:00:00"
            jobs.append(job)
            _seed_meeting_outputs(config, job.meeting_id)

        fake_queue = MagicMock()
        fake_queue.get_all_jobs = MagicMock(return_value=jobs)

        processed: list[str] = []

        async def fake_compile(**kwargs: Any) -> Any:
            processed.append(kwargs["meeting_id"])
            return MagicMock(
                meeting_id=kwargs["meeting_id"],
                pages_created=[],
                pages_updated=[],
                pages_pending=[],
                pages_rejected=[],
            )

        with patch(
            "scripts.backfill_wiki._compile_single_meeting",
            new=AsyncMock(side_effect=fake_compile),
        ):
            result = await backfill(
                config=config,
                job_queue=fake_queue,
                since=date(2026, 4, 15),
                until=date(2026, 4, 20),
            )

        # 2번째(14일) 제외, 3·4번째(16, 19일) 만 처리.
        assert result.total == 2
        assert "abc00002" in processed
        assert "abc00003" in processed
        assert "abc00000" not in processed
        assert "abc00001" not in processed
        assert "abc00004" not in processed
