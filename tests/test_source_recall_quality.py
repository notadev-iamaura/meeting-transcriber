"""원시 fence 회귀와 내용 주제·명시 회의 범위·불완결 발화의 일반 경계를 검증한다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from config import AppConfig, PathsConfig
from search.hybrid_search import SearchResponse
from search.source_answers import build_source_candidates, rank_source_candidates, select_ids
from search.source_query import (
    SourceScope,
    focused_source_query,
    resolve_source_scope,
    topic_context_results,
)
from tests.test_source_bound_chat import _chunk, _engine, _index, _utterance, _write


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    """모든 원문과 색인을 격리한 설정을 만든다."""
    return AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))


def _meeting(
    config: AppConfig, mid: str, *, day: str = "2031-01-15", revision: str | None = None
) -> Any:
    """실제 ID·보존 날짜·색인 날짜가 일치하는 합성 회의를 기록한다."""
    u = [_utterance("SPEAKER_07", "베타 확장본은 검토 후 배포하겠습니다.", 10)]
    cp = config.paths.resolved_checkpoints_dir / mid
    cp.mkdir(parents=True)
    payload = {"utterances": u, "meeting_date": day}
    if revision:
        payload["search_revision"] = revision
    (cp / "correct.json").write_text(json.dumps(payload))
    row = _chunk(0, u, mid=mid)
    row.date = day
    return row


@pytest.mark.parametrize(
    "raw",
    [
        '```json\n{\n"ids": [\n3]\n}\n```',
        '```\n{"ids":[3]}\n```',
        ' \n```json\r\n{"ids":[3]}\r\n```\n',
    ],
)
def test_actual_single_whole_fence_keeps_strict_existing_id_binding(
    config: AppConfig, raw: str
) -> None:
    """실제 관측한 전체 JSON fence는 벗기되 실제 후보 ID 판정은 동일하게 유지한다."""
    u = [_utterance("SPEAKER_07", f"합성 문장 {i}입니다.", i * 2 + 10) for i in range(4)]
    _write(config, u)
    candidates = build_source_candidates(config, [_chunk(0, u)])
    assert select_ids(raw, candidates, 4, 1000) == [3]


@pytest.mark.parametrize(
    "body",
    [
        '{"ids":[99]}',
        '{"ids":[true]}',
        '{"ids":[null]}',
        '{"ids":[0,0]}',
        '{"ids":[0],"answer":"새 주장"}',
        '{"ids":[],"ids":[0]}',
        '{"ids":[0]',
        '{"ids":[0]}\n{"ids":[0]}',
    ],
)
def test_fence_does_not_allow_bad_ids_prose_duplicate_keys_or_truncation(
    config: AppConfig, body: str
) -> None:
    """fence 안에서도 bool·중복·없는 ID·새 설명·부분 JSON은 계속 거부한다."""
    u = [_utterance("SPEAKER_07", "합성 문장입니다.", 10)]
    _write(config, u)
    candidates = build_source_candidates(config, [_chunk(0, u)])
    assert select_ids(f"```json\n{body}\n```", candidates, 2, 1000) is None
    assert select_ids('설명\n```json\n{"ids":[0]}\n```', candidates, 2, 1000) is None


def test_content_topic_precedes_generic_future_facet_and_keeps_base_metadata(
    config: AppConfig,
) -> None:
    """받아보고 같은 일반 표현이 실제 주제·활성 단계보다 먼저 오르지 않는다."""
    u = [
        _utterance("SPEAKER_00", "데이터를 받아보고 생각하시죠.", 10),
        _utterance(
            "SPEAKER_07", "베타 확장본 열람을 마치고 활성 단계는 다음 회의에 하겠습니다.", 20
        ),
    ]
    _write(config, u)
    rows = [_chunk(i, [v]) for i, v in enumerate(u)]
    candidates = build_source_candidates(config, rows)
    assert (
        rank_source_candidates(
            "베타 확장본 열람·활성 일정과 공유 내용을 정리해 주세요.", candidates
        )[0].source_index
        == 1
    )
    focus = focused_source_query(
        "현재 베타 확장본 열람·활성 일정과 안내 대상을 정리하고 이미 한 일과 앞으로 할 일을 구분해 주세요.",
        15,
    )
    assert "베타" in focus and "확장본" in focus and "열람" in focus
    assert "현재" not in focus and "구분" not in focus
    assert focused_source_query("현재 확정된 결정은 무엇인가요?", 15) == ""
    assert focused_source_query("'첫 주제'와 '둘째 주제'의 결정을 비교해 주세요.", 15) == ""
    other = _chunk(3, [u[0]], mid="other")
    combined = topic_context_results(
        "베타 확장본 열람", [other, rows[1]], [rows[1]], SourceScope(None, None), 5
    )
    assert combined == [rows[1]] and combined[0] is rows[1]
    assert rows[1].score == 0.1 and other.meeting_id == "other"


@pytest.mark.parametrize("suffix", ["", "_zoom_0123456789abcdef0123456789abcdef"])
def test_natural_meeting_scope_requires_unique_current_indexed_identity(
    config: AppConfig, suffix: str
) -> None:
    """자연어 날짜·시각은 실제 보존 날짜와 색인 ID가 유일할 때만 필터가 된다."""
    mid = f"meeting_20310115_094510{suffix}"
    row = _meeting(config, mid)
    _index(config, [row])
    query = "2031년 1월 15일 09:45 회의 기준 베타 확장본은 어떤 상태인가요?"
    scope = resolve_source_scope(config, query, None, None)
    assert not scope.blocked and scope.meeting_id == mid and scope.date_filter == "2031-01-15"
    assert "2031" not in focused_source_query(query, 15)
    second = _meeting(config, "meeting_20310115_094520")
    _index(config, [second])
    assert resolve_source_scope(config, query, None, None).blocked
    forced = resolve_source_scope(config, "2032년 2월 30일 11:11 회의 기준", mid, "2031-01-15")
    assert forced.meeting_id == mid and forced.date_filter == "2031-01-15" and not forced.blocked
    assert resolve_source_scope(config, "2031년 2월 30일 09:45 회의", None, None).blocked
    assert (
        resolve_source_scope(config, "2031년 1월 15일까지 보고할 일정", None, None).meeting_id
        is None
    )
    multiple = "2031년 1월 15일 09:45 회의와 2031년 1월 16일 10:00 회의 비교"
    assert resolve_source_scope(config, multiple, None, None).blocked
    assert focused_source_query(multiple, 15) == ""


@pytest.mark.parametrize("kind", ["stale", "wrong_date", "missing"])
def test_stale_missing_or_conflicting_source_is_not_a_natural_scope(
    config: AppConfig, kind: str
) -> None:
    mid = "meeting_20310115_094510"
    row = _meeting(config, mid, revision="edited" if kind == "stale" else None)
    _index(config, [row])
    path = config.paths.resolved_checkpoints_dir / mid / "correct.json"
    if kind == "wrong_date":
        body = json.loads(path.read_text())
        body["meeting_date"] = "2031-01-16"
        path.write_text(json.dumps(body))
    if kind == "missing":
        path.unlink()
    assert resolve_source_scope(config, "2031년 1월 15일 09:45 회의의 상태", None, None).blocked


@pytest.mark.parametrize("other_actor", [False, True])
async def test_same_actor_unfinished_clause_uses_next_own_parent_quote_only(
    config: AppConfig, other_actor: bool
) -> None:
    """연결 어미 뒤 연속 같은 화자만 자기 parent·시각 인용으로 보충하며 다른 주체는 합치지 않는다."""
    u = [
        _utterance("SPEAKER_07", "결과를 표시해야 하는데", 10),
        _utterance(
            "SPEAKER_02" if other_actor else "SPEAKER_07", "서비스가 잠시 작동하지 않았습니다.", 12
        ),
    ]
    _write(config, u)
    rows = [_chunk(i, [v]) for i, v in enumerate(u)]
    engine, backend = _engine(config, rows, '```json\n{"ids":[0]}\n```')
    answer = await engine.chat("원문 한 문장을 그대로 인용해 주세요.", meeting_id_filter="sample")
    assert backend.chat.call_count == 1 and answer.llm_used
    assert len(answer.references) == (1 if other_actor else 2)
    assert answer.references[0].quote.text == u[0]["text"]
    if not other_actor:
        assert answer.references[1].chunk_id == rows[1].chunk_id
        assert answer.references[1].quote.start_time == 12
    u[0]["text"] = "결과 표시 방식은 확정했습니다."
    _write(config, u)
    terminal, _ = _engine(config, [_chunk(i, [v]) for i, v in enumerate(u)], '{"ids":[0]}')
    assert (
        len(
            (
                await terminal.chat(
                    "원문 한 문장을 그대로 인용해 주세요.", meeting_id_filter="sample"
                )
            ).references
        )
        == 1
    )


async def test_focused_search_is_one_additional_call_and_error_does_not_broaden_scope(
    config: AppConfig,
) -> None:
    """주제 검색은 바뀐 문자열로 최대 한 번이고 오류나 명시 필터를 우회하지 않는다."""
    u = [_utterance("SPEAKER_07", "베타 확장본 검토 후 배포하겠습니다.", 10)]
    _write(config, u)
    row = _chunk(0, u)
    engine, backend = _engine(config, [row], '{"ids":[0]}')
    question = "'베타 확장본'의 현재 확정된 결정은 무엇인가요?"
    result = await engine.chat(
        question, meeting_id_filter="sample", date_filter=row.date, speaker_filter="SPEAKER_07"
    )
    calls = engine._search_engine.search.call_args_list
    assert len(calls) == 2 and calls[0].kwargs["query"] != calls[1].kwargs["query"]
    assert all(
        c.kwargs["meeting_id_filter"] == "sample"
        and c.kwargs["date_filter"] == row.date
        and c.kwargs["speaker_filter"] == "SPEAKER_07"
        for c in calls
    )
    assert result.references and backend.chat.call_count == 1
    engine._search_engine.search.side_effect = [
        SearchResponse([row], question, 1),
        SearchResponse([row], question, 1, source_errors={"vector": "unavailable"}),
    ]
    failed = await engine.chat(question, meeting_id_filter="sample")
    assert not failed.references and not failed.llm_called


@pytest.mark.parametrize(
    "mid", ["meeting_20310115_0945", "meeting_20310115_0945junk", "meeting_20310115_094599"]
)
def test_natural_clock_does_not_accept_prefix_only_suffix_junk_or_invalid_seconds(
    config: AppConfig, mid: str
) -> None:
    """시각의 prefix만 닮은 ID가 실제 같은 분 회의로 가장하지 못한다."""
    row = _meeting(config, mid)
    _index(config, [row])
    assert resolve_source_scope(config, "2031년 1월 15일 09:45 회의의 상태", None, None).blocked
