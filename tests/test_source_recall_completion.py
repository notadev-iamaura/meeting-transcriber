"""원인·완료 질문의 조치 구간을 별도 검색하되 상태를 추정하지 않는 경계를 검사한다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from config import AppConfig, PathsConfig
from search.source_recall import supplement_results
from tests.test_source_bound_chat import _chunk, _index, _utterance, _write


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    """운영 원문과 분리된 로컬 시험 경로를 만든다."""
    return AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))


def _options(**changes: Any) -> dict[str, Any]:
    """기존 필터·최대 문맥 수를 명시한다."""
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


@pytest.mark.parametrize("action", ["조치", "액션", "해결", "완화", "중재"])
async def test_causal_completion_recalls_action_word_without_claiming_done(
    config: AppConfig, action: str
) -> None:
    """조치 단어의 후보를 보충하되 미래·부정 표현까지 원문 그대로 보존한다."""
    u = [
        _utterance("SPEAKER_00", "RCA 원인 분석은 별도 자료입니다.", 10),
        _utterance("SPEAKER_07", f"{action} 작업은 아직 시작하지 않았고 내일 할 예정입니다.", 90),
    ]
    _write(config, u)
    rows = [_chunk(0, [u[0]]), _chunk(9, [u[1]])]
    _index(config, rows)
    output = await supplement_results(
        config, "원인 분석은 완료됐고 앞으로 할 일은 무엇인가요?", rows[:1], **_options()
    )
    assert output[0] is rows[0] and len(output) == 2
    assert output[1].text == rows[1].text and output[1].source == "fts" and output[1].score == 0.0
    assert (output[1].speakers, output[1].start_time, output[1].end_time) == (
        rows[1].speakers,
        rows[1].start_time,
        rows[1].end_time,
    )


@pytest.mark.parametrize(
    "query", ["검토는 완료됐나요?", "RCA 원인 문장을 알려 주세요.", "앞으로 일정은 무엇인가요?"]
)
async def test_action_axis_requires_both_causal_and_completion_intent(
    config: AppConfig, query: str
) -> None:
    """일반 완료 질문이나 원인 단독 질문에 조치 단어만으로 새 구간을 넣지 않는다."""
    u = [
        _utterance("SPEAKER_00", "RCA 자료입니다.", 10),
        _utterance("SPEAKER_07", "액션을 수행했습니다.", 90),
    ]
    _write(config, u)
    rows = [_chunk(0, [u[0]]), _chunk(9, [u[1]])]
    _index(config, rows)
    assert await supplement_results(config, query, rows[:1], **_options()) == rows[:1]


async def test_full_context_reserves_action_and_keeps_anchor_order_scores(
    config: AppConfig,
) -> None:
    """미래 RCA 후보로15개가 차 있어도 조치축 하나를 확보하고 기존 객체·점수를 보존한다."""
    u = [
        _utterance("SPEAKER_00", f"RCA 원인 분석 자료 {i}는 앞으로 준비합니다.", i * 3)
        for i in range(15)
    ]
    u.append(_utterance("SPEAKER_07", "액션을 수행했고 상황을 중재했습니다.", 100))
    _write(config, u)
    rows = [_chunk(i * 4, [item]) for i, item in enumerate(u)]
    _index(config, rows)
    before = [r.to_dict() for r in rows[:15]]
    output = await supplement_results(
        config, "원인 분석과 재발 방지 업무는 어디까지 끝났나요?", rows[:15], **_options()
    )
    assert len(output) == 15 and output[0] is rows[0]
    assert rows[-1].chunk_id in {r.chunk_id for r in output}
    retained = [r for r in output if r.chunk_id in {v.chunk_id for v in rows[:15]}]
    assert retained == [r for r in rows[:15] if r.chunk_id in {v.chunk_id for v in retained}]
    assert all(r is rows[rows.index(r)] for r in retained)
    assert [r.to_dict() for r in rows[:15]] == before


@pytest.mark.parametrize(
    "window,budget,expect_next", [(0, 15, False), (1, 15, True), (1, 2, False)]
)
async def test_new_action_anchor_preserves_only_bounded_indexed_neighbors(
    config: AppConfig, window: int, budget: int, expect_next: bool
) -> None:
    """조치 경계의 뒤 발언을 기존 인접 창과 전체 예산 안에서만 보충한다."""
    u = [
        _utterance("SPEAKER_00", "RCA 원인 분석 자료입니다.", 10),
        _utterance("SPEAKER_07", "액션을 수행했습니다.", 90),
        _utterance("SPEAKER_00", "그 내용을 하나 더 짚고 넘어가겠습니다.", 94),
    ]
    _write(config, u)
    rows = [_chunk(i, [item]) for i, item in zip([0, 8, 9], u, strict=True)]
    _index(config, rows)
    output = await supplement_results(
        config,
        "원인 분석은 완료됐나요?",
        rows[:1],
        **_options(neighbor_window=window, max_chunks=budget),
    )
    assert len(output) <= budget and output[0] is rows[0]
    assert (rows[2].chunk_id in {r.chunk_id for r in output}) is expect_next


async def test_new_action_neighbors_keep_explicit_speaker_and_date_filters(
    config: AppConfig,
) -> None:
    """조치의 새 이웃에도 원래 회의·화자·날짜 필터를 적용한다."""
    u = [
        _utterance("SPEAKER_00", "RCA 원인 분석 자료입니다.", 10),
        _utterance("SPEAKER_00", "액션을 수행했습니다.", 90),
        _utterance("SPEAKER_07", "그 내용을 하나 더 짚고 넘어가겠습니다.", 94),
    ]
    _write(config, u)
    rows = [_chunk(i, [item]) for i, item in zip([0, 8, 9], u, strict=True)]
    wrong_date = _chunk(7, [dict(u[2], speaker="SPEAKER_00")])
    wrong_date.date = "2026-01-02"
    other = _chunk(9, [dict(u[2], speaker="SPEAKER_00")], mid="other")
    _index(config, [*rows, wrong_date, other])
    output = await supplement_results(
        config,
        "원인 분석은 완료됐나요?",
        rows[:1],
        **_options(neighbor_window=1, speaker_filter="SPEAKER_00"),
    )
    assert {r.chunk_id for r in output} == {r.chunk_id for r in rows[:2]}


async def test_action_axis_respects_meeting_date_and_speaker_filters(config: AppConfig) -> None:
    """다른 회의·날짜·화자의 조치가 같은 질문의 후보로 들어오지 않는다."""
    u = [
        _utterance("SPEAKER_00", "RCA 자료입니다.", 10),
        _utterance("SPEAKER_07", "액션을 수행했습니다.", 90),
    ]
    _write(config, u)
    anchor = _chunk(0, [u[0]])
    wrong_actor = _chunk(9, [u[1]])
    wrong_date = _chunk(10, [dict(u[1], speaker="SPEAKER_00")])
    wrong_date.date = "2026-01-02"
    other = _chunk(11, [dict(u[1], speaker="SPEAKER_00")], mid="other")
    _index(config, [anchor, wrong_actor, wrong_date, other])
    assert await supplement_results(
        config, "원인 분석은 완료됐나요?", [anchor], **_options(speaker_filter="SPEAKER_00")
    ) == [anchor]


async def test_action_axis_requires_current_receipt_and_matching_embed(config: AppConfig) -> None:
    """편집본의 현재 영수증과 embed가 맞아야 조치 후보를 보충한다."""
    u = [
        _utterance("SPEAKER_00", "RCA 자료입니다.", 10),
        _utterance("SPEAKER_07", "액션을 수행했습니다.", 90),
    ]
    path = _write(config, u, revision="edit-current")
    rows = [_chunk(0, [u[0]]), _chunk(9, [u[1]])]
    _index(config, rows)
    (path.parent / "search_receipt.json").write_text(
        json.dumps({"revision": "edit-current", "state": "current"})
    )
    (path.parent / "embed.json").write_text(json.dumps({"chunks": [r.to_dict() for r in rows]}))
    query = "원인 분석은 완료됐나요?"
    assert len(await supplement_results(config, query, rows[:1], **_options())) == 2
    (path.parent / "embed.json").write_text(json.dumps({"chunks": [rows[0].to_dict()]}))
    assert await supplement_results(config, query, rows[:1], **_options()) == rows[:1]
    (path.parent / "search_receipt.json").write_text(
        json.dumps({"revision": "older", "state": "current"})
    )
    assert await supplement_results(config, query, rows[:1], **_options()) == rows[:1]


async def test_unsafe_source_and_one_chunk_budget_do_not_admit_action(
    config: AppConfig, tmp_path: Path
) -> None:
    """상한1과 symlink 원문을 통해 조치 후보가 경계를 우회하지 않는다."""
    u = [
        _utterance("SPEAKER_00", "RCA 자료입니다.", 10),
        _utterance("SPEAKER_07", "액션을 수행했습니다.", 90),
    ]
    path = _write(config, u)
    rows = [_chunk(0, [u[0]]), _chunk(9, [u[1]])]
    _index(config, rows)
    query = "원인 분석은 이미 끝났나요?"
    assert await supplement_results(config, query, rows[:1], **_options(max_chunks=1)) == rows[:1]
    moved = tmp_path / "moved.json"
    path.rename(moved)
    path.symlink_to(moved)
    assert await supplement_results(config, query, rows[:1], **_options()) == rows[:1]
