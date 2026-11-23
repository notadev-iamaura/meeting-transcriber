"""회의 안건의 영속 근거·편집 최신성·구간 경계를 검증한다."""

import json
from pathlib import Path

import pytest

from config import AppConfig, PathsConfig, WikiConfig
from core.agenda_records import (
    agenda_for_topic,
    minutes_match_transcript,
    refresh_agenda_records,
)

_MEETING = "meeting_20260601_101020"
_TRANSCRIPT = [
    "물류 창고 비상등 점검 요청이 왔습니다. 오늘 5시에 만나자고 하셨어요.",
    "운영팀이 점검하고 결과를 공유하기로 했습니다.",
    "승인이 되면 제가 보고서를 보내드릴게요.",
    "다음 안건은 비밀번호 재설정입니다. 보안팀이 수요일까지 공지하기로 했습니다.",
    "비밀번호 재설정은 미확정입니다.",
]
_MINUTES = """## 결정 사항
- 물류 창고 비상등 점검은 운영팀이 점검하고 결과를 공유하기로 했다.
- 비밀번호 재설정은 보안팀이 수요일까지 공지하기로 했다.
"""


def _source(config: AppConfig, texts: list[str], revision: str = "") -> Path:
    directory = config.paths.resolved_checkpoints_dir / _MEETING
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "utterances": [
            {"speaker": "S1", "text": text, "start": i * 10.0, "end": i * 10.0 + 9}
            for i, text in enumerate(texts)
        ]
    }
    if revision:
        data["search_revision"] = revision
    path = directory / "correct.json"
    path.write_text(json.dumps(data, ensure_ascii=False))
    return path


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        wiki=WikiConfig(enabled=True, root=tmp_path / "custom"),
    )
    _source(cfg, _TRANSCRIPT)
    directory = cfg.paths.resolved_outputs_dir / _MEETING
    directory.mkdir(parents=True)
    (directory / "summary.md").write_text(_MINUTES)
    return cfg


def test_scoped_fields_reconstruct_from_sources_and_reuse_index(config: AppConfig) -> None:
    assert refresh_agenda_records(config, _MEETING) == 2
    (path,) = config.wiki.root.glob(".index/agenda_records/*.json")
    original = path.stat().st_mtime_ns
    cached = path.read_text()
    assert "비상등" not in cached and "운영팀" not in cached and _MEETING not in cached
    assert '"condition"' in cached and '"owner"' in cached and '"time"' in cached
    assert refresh_agenda_records(config, _MEETING) == 2
    assert path.stat().st_mtime_ns == original
    agenda = agenda_for_topic(config, _MEETING, "창고 비상등", anchor_index=0)
    assert agenda is not None and agenda.turn_indices == (0, 1, 2)
    assert all("비밀번호" not in f.text for f in agenda.facts)
    scheduled = next(f for f in agenda.facts if f.source_index == 0 and f.kind == "schedule")
    assert scheduled.status == "제안" and scheduled.time_text == "오늘 5시"
    action = next(f for f in agenda.facts if f.source_index == 1 and f.kind == "action")
    assert action.owner == "운영팀"
    conditional = next(f for f in agenda.facts if f.source_index == 2 and f.kind == "action")
    assert conditional.condition_text == "승인이 되면" and conditional.owner == "S1"
    assert any(f.minutes_excerpt and f.minutes_excerpt in _MINUTES for f in agenda.facts)
    assert agenda_for_topic(config, _MEETING, "비밀번호", anchor_index=1) is None


@pytest.mark.parametrize("revision", ["edited", ""])
def test_edit_invalidates_query_and_old_minutes_until_rebound(
    config: AppConfig, revision: str
) -> None:
    refresh_agenda_records(config, _MEETING)
    assert minutes_match_transcript(config, _MEETING, _MINUTES)
    changed = _TRANSCRIPT.copy()
    changed[0] = "창고 비상등 점검 취소되었습니다."
    _source(config, changed, revision)
    assert not minutes_match_transcript(config, _MEETING, _MINUTES)
    assert agenda_for_topic(config, _MEETING, "비상등") is None
    refresh_agenda_records(config, _MEETING)
    result = agenda_for_topic(config, _MEETING, "비상등")
    assert result and not result.minutes_bound and not result.minutes_excerpts
    assert not any(f.text == _TRANSCRIPT[0] for f in result.facts)
    revised = _MINUTES.replace("점검하고 결과를 공유하기로 했다", "점검 취소를 공지하기로 했다")
    (config.paths.resolved_outputs_dir / _MEETING / "summary.md").write_text(revised)
    assert agenda_for_topic(config, _MEETING, "비상등") is None
    refresh_agenda_records(config, _MEETING, minutes_edited=True)
    assert minutes_match_transcript(config, _MEETING, revised)
    assert not minutes_match_transcript(config, _MEETING, _MINUTES)


def test_unbound_legacy_custom_base_and_symlink(config: AppConfig, tmp_path: Path) -> None:
    local = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    refresh_agenda_records(local, _MEETING)
    path = local.paths.resolved_checkpoints_dir / _MEETING / ".wiki-agenda.json"
    assert not (tmp_path / "wiki").exists()
    # 현재 전사가 사라지면 캐시만으로는 복원할 수 없다.
    (_source(local, _TRANSCRIPT)).unlink()
    assert agenda_for_topic(local, _MEETING, "창고") is None
    assert not minutes_match_transcript(local, _MEETING, _MINUTES)
    assert path.is_file()
    _source(config, _TRANSCRIPT)
    config.wiki.root.symlink_to(tmp_path / "wiki", target_is_directory=True)
    with pytest.raises(OSError):
        refresh_agenda_records(config, _MEETING)
    assert agenda_for_topic(config, _MEETING, "창고") is None
