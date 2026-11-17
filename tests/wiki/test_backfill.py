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
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig, PathsConfig, ServerConfig, WikiConfig


@pytest.fixture(autouse=True)
def _no_real_thermal_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """백필 단위 테스트에서 실제 온도 probe와 3분 쿨다운을 실행하지 않는다."""
    fake = MagicMock()
    fake.wait_if_needed = AsyncMock()
    fake.notify_job_started = AsyncMock()
    fake.notify_job_completed = AsyncMock()
    monkeypatch.setattr("core.thermal_manager.ThermalManager", lambda _config: fake)


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
    meeting_date: str = "2026-04-29",
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
    checkpoint = config.paths.resolved_checkpoints_dir / meeting_id
    checkpoint.mkdir(parents=True, exist_ok=True)
    (checkpoint / "meeting_date.json").write_text(json.dumps({"date": meeting_date}))

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

    @pytest.mark.asyncio
    async def test_compile_single_meeting은_model_manager를_주입한다(
        self,
        tmp_path: Path,
    ) -> None:
        """백필 단일 컴파일 경로가 model_manager=None 으로 V2 를 만들지 않아야 한다."""
        from scripts.backfill_wiki import _compile_single_meeting

        config = _make_test_config(tmp_path)
        meeting_id = "abc12345"
        manager = object()
        seen: dict[str, Any] = {}

        class _FakeCompiler:
            async def compile_meeting(self, **kwargs: Any) -> Any:
                return MagicMock(
                    meeting_id=kwargs["meeting_id"],
                    pages_created=["x.md"],
                    pages_updated=[],
                    pages_pending=[],
                    pages_rejected=[],
                    llm_call_count=1,
                )

        def fake_create_wiki_compiler_v2(**kwargs: Any) -> _FakeCompiler:
            seen.update(kwargs)
            return _FakeCompiler()

        with (
            patch("core.model_manager.get_model_manager", return_value=manager),
            patch(
                "steps.wiki_compiler._create_wiki_compiler_v2",
                side_effect=fake_create_wiki_compiler_v2,
            ),
        ):
            await _compile_single_meeting(
                config=config,
                meeting_id=meeting_id,
                meeting_date=date(2026, 4, 29),
                summary="# 요약",
                utterances=[],
            )

        assert seen["model_manager"] is manager

    @pytest.mark.asyncio
    async def test_backfill_empty_compile은_성공으로_카운트하지_않는다(
        self,
        tmp_path: Path,
    ) -> None:
        """LLM 호출 후 페이지 0건이면 silent success 대신 empty_compile 실패로 보고한다."""
        from scripts.backfill_wiki import backfill

        config = _make_test_config(tmp_path)
        meeting_id = "abc12345"
        _seed_meeting_outputs(config, meeting_id)

        fake_queue = MagicMock()
        fake_queue.get_all_jobs = MagicMock(return_value=[_make_fake_job(meeting_id)])

        async def fake_compile(**kwargs: Any) -> Any:
            return MagicMock(
                meeting_id=kwargs["meeting_id"],
                pages_created=[],
                pages_updated=[],
                pages_pending=[],
                pages_rejected=[],
                llm_call_count=5,
            )

        with patch(
            "scripts.backfill_wiki._compile_single_meeting",
            new=AsyncMock(side_effect=fake_compile),
        ):
            result = await backfill(config=config, job_queue=fake_queue)

        assert result.succeeded == 0
        assert result.failed == 1
        assert len(result.errors) == 1
        assert result.errors[0].meeting_id == meeting_id
        assert result.errors[0].error_type == "empty_compile"


# ─── 시나리오 3: 1건 실패, 다음 진행 ───────────────────────────────────


class TestBackfillFailureIsolated:
    """회의 1건 실패해도 다음 회의가 정상 진행되는 격리 정책."""

    @pytest.mark.asyncio
    async def test_backfill_3번째_실패_4번째_진행(self, tmp_path: Path) -> None:
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
                pages_created=["decisions/test.md"],
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
                pages_created=["decisions/test.md"],
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
        """첫 추론 전 대상 수를 알리고 회의가 끝날 때마다 진행을 갱신한다."""
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
            assert progress_log[0] == (0, 3, meeting_ids[0])
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

        assert len(progress_log) == 4
        assert [p[0] for p in progress_log] == [0, 1, 2, 3]
        assert all(p[1] == 3 for p in progress_log)
        # current_meeting_id 가 순서대로 전달.
        assert [p[2] for p in progress_log] == [meeting_ids[0], *meeting_ids]


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
            _seed_meeting_outputs(config, job.meeting_id, meeting_date=f"2026-04-{day:02d}")

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


# ─── 시나리오 9: utterances JSON 정규화 ────────────────────────────────


class TestLoadUtterancesNormalization:
    """백필 로더가 JSON dict 를 라이브 경로와 같은 객체 형태로 정규화하는지 검증."""

    def test_load_utterances_corrected_json을_corrected_utterance로_변환(
        self,
        tmp_path: Path,
    ) -> None:
        """outputs/{id}/corrected.json 의 dict 발화를 CorrectedUtterance 로 반환한다."""
        from scripts.backfill_wiki import _load_utterances
        from steps.corrector import CorrectedUtterance

        config = _make_test_config(tmp_path)
        meeting_id = "abc12345"
        _seed_meeting_outputs(config, meeting_id)

        utterances = _load_utterances(config, meeting_id)

        assert utterances is not None
        assert isinstance(utterances[0], CorrectedUtterance)
        assert utterances[0].start == 0.0
        assert utterances[0].end == 5.0
        assert utterances[0].original_text == utterances[0].text

    def test_load_utterances_merge_json_폴백도_corrected_utterance로_변환(
        self,
        tmp_path: Path,
    ) -> None:
        """보정 결과가 없고 merge 체크포인트만 있어도 verifier 호환 객체를 반환한다."""
        from scripts.backfill_wiki import _load_utterances
        from steps.corrector import CorrectedUtterance

        config = _make_test_config(tmp_path)
        meeting_id = "abc12345"
        checkpoint_dir = config.paths.resolved_checkpoints_dir / meeting_id
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        (checkpoint_dir / "merge.json").write_text(
            json.dumps(
                {
                    "utterances": [
                        {
                            "speaker": "SPEAKER_01",
                            "text": "merge 폴백 발화",
                            "start": 10.0,
                            "end": 12.5,
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        utterances = _load_utterances(config, meeting_id)

        assert utterances is not None
        assert isinstance(utterances[0], CorrectedUtterance)
        assert utterances[0].text == "merge 폴백 발화"
        assert utterances[0].original_text == "merge 폴백 발화"
        assert utterances[0].speaker == "SPEAKER_01"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "published,pending,expected", [(0, 0, "empty"), (0, 1, "pending"), (1, 1, "published")]
)
async def test_백필은_공개_검토_빈결과를_구분한다(
    tmp_path: Path, published: int, pending: int, expected: str
) -> None:
    """모델 호출 0회나 검토 후보만 있는 결과도 공개 성공으로 부풀리지 않는다."""
    from scripts.backfill_wiki import backfill

    config = _make_test_config(tmp_path)
    _seed_meeting_outputs(config, "sample")
    queue = SimpleNamespace(get_all_jobs=lambda: [_make_fake_job("sample")])
    compile_mock = AsyncMock(
        return_value=SimpleNamespace(
            pages_created=["decisions/a.md"] * published,
            pages_updated=[],
            pages_pending=["decisions/b.md"] * pending,
            pages_rejected=[],
            llm_call_count=0,
        )
    )
    with patch("scripts.backfill_wiki._compile_single_meeting", compile_mock):
        result = await backfill(config=config, job_queue=queue)
    assert result.succeeded == int(expected == "published")
    assert result.pending == int(expected == "pending")
    assert result.empty == int(expected == "empty")
    assert result.failed == int(expected == "empty")
    assert result.published_pages == published
    assert result.pending_pages == pending


@pytest.mark.asyncio
async def test_목록조회_실패가_빈목록_완료로_숨겨지지_않는다(tmp_path: Path) -> None:
    """DB 오류는 대상 0건의 정상 완료와 구별한다."""
    from scripts.backfill_wiki import backfill

    queue = SimpleNamespace(get_all_jobs=MagicMock(side_effect=OSError("DB unavailable")))
    result = await backfill(config=_make_test_config(tmp_path), job_queue=queue)
    assert result.succeeded == 0 and result.failed == 1
    assert result.errors[0].error_type == "job_list_failed"


@pytest.mark.asyncio
async def test_등록순서가_아닌_보존된_회의일_오름차순으로_처리한다(tmp_path: Path) -> None:
    """오래된 회의부터 현재로 누적하고 생성일은 회의 날짜로 사용하지 않는다."""
    from scripts.backfill_wiki import backfill

    config = _make_test_config(tmp_path)
    for mid, day in (("new", "2026-06-02"), ("old", "2026-05-01")):
        _seed_meeting_outputs(config, mid, meeting_date=day)
    queue = SimpleNamespace(get_all_jobs=lambda: [_make_fake_job("new"), _make_fake_job("old")])
    calls: list[tuple[str, date]] = []

    async def compile_one(**kwargs: Any) -> Any:
        """실행 날짜와 순서만 기록한다."""
        calls.append((kwargs["meeting_id"], kwargs["meeting_date"]))
        return SimpleNamespace(
            pages_created=["decisions/a.md"], pages_updated=[], pages_pending=[], pages_rejected=[]
        )

    with patch(
        "scripts.backfill_wiki._compile_single_meeting", AsyncMock(side_effect=compile_one)
    ):
        result = await backfill(config=config, job_queue=queue)
    assert result.succeeded == 2
    assert calls == [("old", date(2026, 5, 1)), ("new", date(2026, 6, 2))]


@pytest.mark.asyncio
async def test_날짜불명은_오늘날짜로_추측해_공개하지_않는다(tmp_path: Path) -> None:
    """보존 날짜·파일명·검증 가능한 원본 시각이 모두 없으면 명시적으로 거부한다."""
    from scripts.backfill_wiki import backfill

    config = _make_test_config(tmp_path)
    _seed_meeting_outputs(config, "sample")
    (config.paths.resolved_checkpoints_dir / "sample" / "meeting_date.json").unlink()
    queue = SimpleNamespace(get_all_jobs=lambda: [_make_fake_job("sample")])
    compile_mock = AsyncMock()
    with patch("scripts.backfill_wiki._compile_single_meeting", compile_mock):
        result = await backfill(config=config, job_queue=queue)
    assert result.failed == 1 and result.succeeded == 0
    assert result.errors[0].error_type == "meeting_date_unknown"
    compile_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled", "dry_run", "recorded"])
async def test_설정과_회의상태가_실제_추론_진입을_통제한다(tmp_path: Path, mode: str) -> None:
    """Wiki 비활성·미리보기·미완료 회의는 모델을 실행하지 않는다."""
    from scripts.backfill_wiki import backfill

    config = _make_test_config(tmp_path)
    _seed_meeting_outputs(config, "sample")
    if mode == "disabled":
        config.wiki.enabled = False
    if mode == "dry_run":
        config.wiki.dry_run = True
    job = _make_fake_job("sample", status="recorded" if mode == "recorded" else "completed")
    queue = SimpleNamespace(get_all_jobs=lambda: [job])
    compile_mock = AsyncMock()
    with patch("scripts.backfill_wiki._compile_single_meeting", compile_mock):
        result = await backfill(config=config, job_queue=queue)
    assert result.succeeded == 0
    compile_mock.assert_not_awaited()
    assert result.failed == int(mode != "dry_run")


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), -1, True, None])
def test_발화_시각이_불명확하면_일부만_골라_공개하지_않는다(
    tmp_path: Path, bad_value: Any
) -> None:
    """유효한 merge 폴백이 있어도 잘못된 최신 교정본을 몰래 우회하지 않는다."""
    from scripts.backfill_wiki import _load_utterances

    config = _make_test_config(tmp_path)
    _seed_meeting_outputs(config, "sample")
    path = config.paths.resolved_outputs_dir / "sample" / "corrected.json"
    path.write_text(
        json.dumps(
            {"utterances": [{"text": "내용", "speaker": "S1", "start": bad_value, "end": 3.0}]}
        )
    )
    with pytest.raises(ValueError):
        _load_utterances(config, "sample")


def test_백필_원문_또는_요약의_symlink를_따라가지_않는다(tmp_path: Path) -> None:
    """일반 파일 대신 바깥 파일을 가리키면 보존한 채 읽기를 차단한다."""
    from scripts.backfill_wiki import _load_summary, _load_utterances

    config = _make_test_config(tmp_path)
    _seed_meeting_outputs(config, "sample")
    folder = config.paths.resolved_outputs_dir / "sample"
    original = folder / "corrected.json"
    outside = tmp_path / "outside.json"
    original.rename(outside)
    original.symlink_to(outside)
    with pytest.raises(OSError):
        _load_utterances(config, "sample")
    summary = folder / "summary.md"
    outside_summary = tmp_path / "outside.md"
    summary.rename(outside_summary)
    summary.symlink_to(outside_summary)
    with pytest.raises(OSError):
        _load_summary(config, "sample")
    assert outside.exists() and outside_summary.exists()


@pytest.mark.asyncio
async def test_공유_회의lease가_원문조회부터_컴파일까지_유지된다(tmp_path: Path) -> None:
    """API·파이프라인과 같은 coordinator를 쓰며 대기 후 상태를 다시 확인한다."""
    from core.meeting_mutation import MeetingMutationCoordinator
    from scripts.backfill_wiki import backfill

    config = _make_test_config(tmp_path)
    _seed_meeting_outputs(config, "sample")
    job = _make_fake_job("sample")
    coordinator = MeetingMutationCoordinator()
    queue = SimpleNamespace(get_all_jobs=lambda: [job], get_job_by_meeting_id=lambda mid: job)

    async def compile_one(**kwargs: Any) -> Any:
        """현재 task의 lease 소유를 확인한다."""
        assert coordinator.owned_by_current_task(kwargs["meeting_id"])
        return SimpleNamespace(
            pages_created=["decisions/a.md"], pages_updated=[], pages_pending=[], pages_rejected=[]
        )

    with patch(
        "scripts.backfill_wiki._compile_single_meeting", AsyncMock(side_effect=compile_one)
    ):
        result = await backfill(
            config=config, job_queue=queue, meeting_mutation_coordinator=coordinator
        )
    assert result.succeeded == 1
    assert not coordinator.locked("sample")


@pytest.mark.asyncio
async def test_컴파일_실패에도_해당_Wiki모델을_언로드한다(tmp_path: Path) -> None:
    """부분 페이지 생성이나 예외 이후 다음 회의에 모델이 남지 않게 한다."""
    from core.wiki.llm_client import WIKI_LLM_MODEL_NAME
    from scripts.backfill_wiki import _compile_single_meeting

    manager = SimpleNamespace(unload_if_current=AsyncMock())
    compiler = SimpleNamespace(compile_meeting=AsyncMock(side_effect=RuntimeError("failed")))
    with (
        patch("core.model_manager.get_model_manager", return_value=manager),
        patch("steps.wiki_compiler._create_wiki_compiler_v2", return_value=compiler),
        pytest.raises(RuntimeError),
    ):
        await _compile_single_meeting(
            config=_make_test_config(tmp_path),
            meeting_id="sample",
            meeting_date=date(2026, 4, 29),
            summary="요약",
            utterances=[],
        )
    manager.unload_if_current.assert_awaited_once_with(WIKI_LLM_MODEL_NAME)
