"""기존 회의의 안건 기록을 앱 시작 후 복구할 때 원문 편집·삭제와 충돌하지 않는다."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.server import _backfill_agenda_records
from config import AppConfig, PathsConfig
from core.job_queue import AsyncJobQueue
from core.meeting_mutation import MeetingMutationCoordinator


@pytest.mark.asyncio
async def test_backfill_rechecks_status_and_continues_after_unreadable_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """완료 상태를 다시 확인하고, 원문 하나가 손상돼도 나머지 회의를 처리한다."""
    coordinator = MeetingMutationCoordinator()
    queue = MagicMock(spec=AsyncJobQueue)
    config = AppConfig()
    jobs = [
        SimpleNamespace(meeting_id=mid, status=status)
        for mid, status in [
            ("recording", "recorded"),
            ("deleted", "completed"),
            ("retrying", "completed"),
            ("damaged", "completed"),
            ("good", "completed"),
        ]
    ]
    queue.get_all_jobs = AsyncMock(return_value=jobs)
    queue.queue.get_job_by_meeting_id.side_effect = [
        None,
        SimpleNamespace(status="running"),
        jobs[3],
        jobs[4],
    ]
    writes = []

    def refresh(app_config: AppConfig, mid: str) -> int:
        assert app_config is config and coordinator.locked(mid)
        writes.append(mid)
        if mid == "damaged":
            raise ValueError("손상된 전사문")
        return 1

    monkeypatch.setattr("core.agenda_records.refresh_agenda_records", refresh)
    await _backfill_agenda_records(config, queue, coordinator)
    assert writes == ["damaged", "good"]
    assert not coordinator.locked("good")


def test_minutes_fallback_never_revives_a_deadline_from_before_an_edit(tmp_path: Path) -> None:
    """안건 캐시 유무에 관계없이 수정 전 회의록 기한을 새 전사의 답으로 재사용하지 않는다."""
    from core.agenda_records import refresh_agenda_records
    from search.topic_minutes import topic_minutes

    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    mid = "deadline"
    source_dir = config.paths.resolved_checkpoints_dir / mid
    output_dir = config.paths.resolved_outputs_dir / mid
    source_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    report = output_dir / "meeting_minutes.md"
    report.write_text("## 결정 사항\n- 온실 도면을 월요일에 공유하기로 함.", encoding="utf-8")
    turn = {"speaker": "A", "start": 1.0, "end": 2.0, "text": "온실 도면은 월요일에 공유합니다."}
    source = source_dir / "correct.json"
    source.write_text(json.dumps({"utterances": [turn]}), encoding="utf-8")
    refresh_agenda_records(config, mid)
    assert topic_minutes(config, mid, "온실 도면")

    turn["text"] = "온실 도면 공유를 취소했습니다."
    source.write_text(
        json.dumps({"utterances": [turn], "search_revision": "new"}), encoding="utf-8"
    )
    assert topic_minutes(config, mid, "온실 도면") is None
    refresh_agenda_records(config, mid)
    assert topic_minutes(config, mid, "온실 도면") is None

    report.write_text("## 결정 사항\n- 온실 도면 공유를 취소함.", encoding="utf-8")
    refresh_agenda_records(config, mid, minutes_edited=True)
    current = topic_minutes(config, mid, "온실 도면")
    assert current and "취소" in current.facts["decision"][0].text
