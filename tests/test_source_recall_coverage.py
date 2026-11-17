"""고정 질문·실제 회의 없이 의도별 문맥과 최신의 날짜·원문 경계를 검증한다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from config import AppConfig, PathsConfig
from search.source_query import SourceScope, topic_context_results
from search.source_recall import supplement_results
from tests.test_source_bound_chat import _chunk, _index, _utterance, _write


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    """운영 데이터와 분리된 원문·색인 경로를 만든다."""
    return AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))


def _args(**changes: Any) -> dict[str, Any]:
    """명시 회의·날짜·화자·문맥 예산을 고정한 기본 인수를 만든다."""
    return (
        dict(
            meeting_id_filter="sample",
            date_filter="2026-01-01",
            speaker_filter=None,
            max_chunks=15,
            neighbor_window=0,
        )
        | changes
    )


async def test_full_context_reserves_distinct_requested_intent_axes_without_reranking(
    config: AppConfig,
) -> None:
    """15개가 차 있어도 원인·시간순서·요청·후속을 확보하고 상위 객체·점수는 보존한다."""
    u = [_utterance("SPEAKER_00", f"검증 배경 {i}", i * 3) for i in range(15)]
    u.extend(
        _utterance("SPEAKER_07", text, 100 + i * 3)
        for i, text in enumerate(
            [
                "원인 분석은 검토 중입니다.",
                "타임라인은 발생 시각 순서로 작성합니다.",
                "별도 검증을 해달라고 부탁했습니다.",
                "검증 결과는 다음 회의에 보고하겠습니다.",
            ]
        )
    )
    _write(config, u)
    rows = [_chunk(i * 4, [item]) for i, item in enumerate(u)]
    _index(config, rows)
    base = rows[:15]
    before = [row.to_dict() for row in base]
    result = await supplement_results(
        config, "장애 원인 분석 요청과 타임라인, 후속 보고는 무엇인가요?", base, **_args()
    )
    assert len(result) == 15
    assert result[:11] == base[:11]
    assert all(result[i] is base[i] for i in range(11))
    assert {r.chunk_id for r in rows[15:]} <= {r.chunk_id for r in result}
    assert [r.to_dict() for r in base] == before
    assert all(row.score == 0.0 for row in result if row not in base)


async def test_intent_axis_never_borrows_foreign_date_speaker_or_meeting(
    config: AppConfig,
) -> None:
    """새 축을 채우기 위해 구조화된 필터를 우회하지 않는다."""
    u = [
        _utterance("SPEAKER_00", "배경입니다.", 1),
        _utterance("SPEAKER_00", "다른 배경입니다.", 2),
        _utterance("SPEAKER_07", "타임라인 순서를 정리하겠습니다.", 90),
    ]
    _write(config, u)
    base = [_chunk(i, [u[i]]) for i in range(2)]
    wrong_speaker = _chunk(10, [u[2]])
    wrong_date = _chunk(11, [dict(u[2], speaker="SPEAKER_00")])
    wrong_date.date = "2026-01-02"
    other = _chunk(12, [dict(u[2], speaker="SPEAKER_00")], mid="different")
    _index(config, [*base, wrong_speaker, wrong_date, other])
    output = await supplement_results(
        config,
        "타임라인 순서를 알려 주세요.",
        base,
        **_args(max_chunks=2, speaker_filter="SPEAKER_00"),
    )
    assert output == base


async def test_pending_source_does_not_replace_full_context_with_new_axis(
    config: AppConfig,
) -> None:
    """새 편집본의 색인 영수증이 없으면 의도별 보충도 막는다."""
    u = [
        _utterance("SPEAKER_00", "배경입니다.", 1),
        _utterance("SPEAKER_00", "다른 배경입니다.", 2),
        _utterance("SPEAKER_07", "타임라인 순서입니다.", 90),
    ]
    _write(config, u, revision="pending-edit")
    rows = [_chunk(i * 4, [item]) for i, item in enumerate(u)]
    _index(config, rows)
    assert (
        await supplement_results(
            config, "타임라인 순서를 알려 주세요.", rows[:2], **_args(max_chunks=2)
        )
        == rows[:2]
    )


async def test_intent_index_failure_still_enforces_context_cap(config: AppConfig) -> None:
    """보충 색인을 읽지 못해도 이미 큰 입력 전체를 상한 밖으로 반환하지 않는다."""
    rows = [_chunk(i, [_utterance("SPEAKER_00", "배경입니다.", i * 3)]) for i in range(5)]
    assert (
        await supplement_results(
            config, "원인 분석의 타임라인은 무엇인가요?", rows, **_args(max_chunks=2)
        )
        == rows[:2]
    )
    assert (
        await supplement_results(
            config,
            "원인 분석의 타임라인은 무엇인가요?",
            rows,
            **_args(max_chunks=2, meeting_id_filter="missing"),
        )
        == []
    )


def _meeting(config: AppConfig, mid: str, day: str, text: str) -> Any:
    """본문·보존 날짜·검색 메타데이터가 같은 합성 회의를 만든다."""
    u = [_utterance("SPEAKER_07", text, 10)]
    cp = config.paths.resolved_checkpoints_dir / mid
    cp.mkdir(parents=True)
    (cp / "correct.json").write_text(json.dumps({"meeting_date": day, "utterances": u}))
    row = _chunk(0, u, mid=mid)
    row.date = day
    return row


def _dated_pair(config: AppConfig) -> tuple[Any, Any]:
    """기존 검색의 오래된 높은 문자 일치와 새 관련 회의를 함께 만든다."""
    old = _meeting(
        config,
        "meeting_20310115_091000",
        "2031-01-15",
        "베타 확장본의 검토 일정과 공유 방식은 검토 중입니다.",
    )
    newer = _meeting(
        config,
        "meeting_20310116_091000",
        "2031-01-16",
        "베타 확장본은 내일 시작할 예정입니다.",
    )
    return old, newer


@pytest.mark.parametrize("intent", ["최근", "최신"])
def test_explicit_latest_uses_current_related_metadata_date_not_literal_coverage(
    config: AppConfig, intent: str
) -> None:
    """더 새롭지만 무관한 회의는 제외하고 관련 후보의 보존 날짜를 우선한다."""
    old, newer = _dated_pair(config)
    unrelated = _meeting(
        config, "meeting_20310117_091000", "2031-01-17", "휴가 공지를 전달했습니다."
    )
    unrelated.source = "fts"
    base = [unrelated, newer, old]
    before = [r.to_dict() for r in base]
    result = topic_context_results(
        f"{intent} 베타 확장본의 검토 일정과 공유 방식은 무엇인가요?",
        base,
        [old],
        SourceScope(None, None),
        15,
        config=config,
    )
    assert result == [newer] and result[0] is newer
    assert [r.to_dict() for r in base] == before


def test_ordinary_or_explicit_scope_does_not_adopt_latest_policy(config: AppConfig) -> None:
    """일반 질의와 API가 명시한 범위는 최신 회의로 바뀌지 않는다."""
    old, newer = _dated_pair(config)
    normal = "베타 확장본의 검토 일정과 공유 방식은 무엇인가요?"
    assert topic_context_results(
        normal, [newer, old], [old], SourceScope(None, None), 15, config=config
    ) == [old]
    assert topic_context_results(
        f"최근 {normal}",
        [newer, old],
        [old],
        SourceScope(old.meeting_id, old.date, frozenset([old.meeting_id])),
        15,
        config=config,
    ) == [old]


@pytest.mark.parametrize(
    "invalid", ["missing", "pending", "date_conflict", "invalid_date", "symlink"]
)
def test_latest_excludes_unsafe_stale_missing_or_conflicting_source(
    config: AppConfig, invalid: str, tmp_path: Path
) -> None:
    """현재 원문과 날짜를 검증하지 못한 새 회의가 이전 유효 후보를 밀어내지 않는다."""
    old, newer = _dated_pair(config)
    path = config.paths.resolved_checkpoints_dir / newer.meeting_id / "correct.json"
    body = json.loads(path.read_text())
    if invalid == "missing":
        path.unlink()
    elif invalid == "pending":
        path.write_text(json.dumps(body | {"search_revision": "new-edit"}))
    elif invalid == "date_conflict":
        (path.parent / "meeting_date.json").write_text(json.dumps({"date": "2031-01-17"}))
    elif invalid == "invalid_date":
        newer.date = "2031-99-99"
    else:
        target = tmp_path / "elsewhere.json"
        path.rename(target)
        path.symlink_to(target)
    assert topic_context_results(
        "최근 베타 확장본의 상태는?",
        [newer, old],
        [old],
        SourceScope(None, None),
        15,
        config=config,
    ) == [old]


def test_latest_latin_topic_does_not_match_word_substring(config: AppConfig) -> None:
    """영문 주제 api를 capitol 내부 글자에서 찾았다고 판단하지 않는다."""
    old = _meeting(config, "meeting_20310115_091000", "2031-01-15", "API 검토 결과입니다.")
    unrelated = _meeting(config, "meeting_20310116_091000", "2031-01-16", "capitol 공지입니다.")
    unrelated.source = "fts"
    assert topic_context_results(
        "최신 'API' 결과는?", [unrelated, old], [old], SourceScope(None, None), 15, config=config
    ) == [old]


def test_latest_semantic_hit_may_use_another_language_without_topic_alias(
    config: AppConfig,
) -> None:
    """이미 검색된 다른 언어의 현재 의미 후보를 문자 일치 부족만으로 버리지 않는다."""
    old = _meeting(config, "meeting_20310115_091000", "2031-01-15", "Solar Review 검토입니다.")
    newer = _meeting(
        config, "meeting_20310116_091000", "2031-01-16", "태양 관측 검토는 진행 중입니다."
    )
    newer.source = "vector"
    _meeting(config, "meeting_20310117_091000", "2031-01-17", "무관한 휴가 공지입니다.")
    result = topic_context_results(
        "최근 'Solar Review' 상태는?",
        [newer, old],
        [old],
        SourceScope(None, None),
        15,
        config=config,
    )
    assert result == [newer] and result[0] is newer
