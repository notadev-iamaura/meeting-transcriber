"""합성 음성 검증 스크립트의 저장소 격리와 엄격한 완료 판정을 검증한다."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.verify_bulk_pipeline import artifacts_valid, completed_cohort, prepare_workspace


def test_workspace_preserves_source_and_creates_independent_copies(tmp_path: Path) -> None:
    """기존 합성 원본을 바꾸지 않고 다른 inode의 독립 복사본만 만든다."""
    audio = tmp_path / "synthetic.wav"
    audio.write_bytes(b"synthetic placeholder")
    copies, digest = prepare_workspace(audio, tmp_path / "new-output")
    assert len(digest) == 64
    assert len({p.stat().st_ino for p in [audio, *copies]}) == 3
    assert all(p.read_bytes() == audio.read_bytes() for p in copies)
    with pytest.raises(FileExistsError):
        prepare_workspace(audio, tmp_path / "new-output")
    assert audio.read_bytes() == b"synthetic placeholder"


def test_symlink_source_is_rejected(tmp_path: Path) -> None:
    """원본 파일의 symlink를 허용하지 않는다."""
    audio = tmp_path / "original.wav"
    audio.write_bytes(b"synthetic")
    link = tmp_path / "link.wav"
    link.symlink_to(audio)
    with pytest.raises(ValueError, match="symlink"):
        prepare_workspace(link, tmp_path / "output")


def test_db_completion_before_final_stage_commit_is_not_success() -> None:
    """DB 작업 완료만으로 쿨다운 뒤 단계 큐의 게시 완료를 미리 선언하지 않는다."""
    snapshot = {
        "jobs": [{"status": "completed"}, {"status": "completed"}],
        "stages": [{"status": "completed", "cohort_id": "one"} for _ in range(8)],
    }
    assert completed_cohort(snapshot)
    snapshot["stages"][-1]["status"] = "running"
    assert not completed_cohort(snapshot)
    snapshot["stages"][-1].update(status="completed", cohort_id="other")
    assert not completed_cohort(snapshot)


@pytest.mark.parametrize(
    "changed",
    [
        {"transcript_segments": 0},
        {"correction_failed": 1},
        {"correction_failed": None},
        {"summary_chars": 0},
        {"summary_fallback_marker": True},
        {"pipeline_status": "paused"},
    ],
)
def test_completed_db_does_not_hide_invalid_model_artifacts(changed: dict[str, object]) -> None:
    """빈 전사·교정 실패·폴백 회의록은 DB 상태와 무관하게 성공으로 세지 않는다."""
    valid = {
        "pipeline_status": "completed",
        "transcript_segments": 12,
        "correction_failed": 0,
        "summary_chars": 250,
        "summary_fallback_marker": False,
    }
    assert artifacts_valid([valid, valid])
    assert not artifacts_valid([valid, {**valid, **changed}])
