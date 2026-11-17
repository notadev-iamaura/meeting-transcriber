"""혼합 화자와 여러 회의의 검색 범위를 실제 격리 원문·색인으로 검증한다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from config import AppConfig, PathsConfig
from search.hybrid_search import SearchResult
from search.source_answers import build_source_candidates, candidate_context_members
from search.source_query import SourceScope, cross_meeting_comparison, topic_context_results
from search.source_recall import supplement_results
from tests.test_source_bound_chat import (
    _chunk,
    _engine,
    _index,
    _model_candidates,
    _utterance,
    _write,
)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    """모델과 사용자 데이터 없이 사용할 기본 제품 설정을 만든다."""
    return AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))


def _select_visible(**kwargs: Any) -> str:
    """모델 입력에 들어온 실제 번호만 선택해 범위 누출을 그대로 드러낸다."""
    payload = json.loads(kwargs["messages"][-1]["content"])
    rows = payload.get("evidence_units")
    if rows is None:
        rows = _model_candidates(payload)
    return json.dumps({"ids": [row["id"] for row in rows][: payload["max_items"]]})


def _meeting(config: AppConfig, mid: str, day: str, texts: list[str]) -> list[SearchResult]:
    """회의별 현재 원문·검색 부모·보존 날짜를 일치시킨다."""
    utterances = [_utterance("SPEAKER_00", text, 10 + i * 4) for i, text in enumerate(texts)]
    checkpoint = config.paths.resolved_checkpoints_dir / mid
    checkpoint.mkdir(parents=True)
    (checkpoint / "correct.json").write_text(
        json.dumps({"meeting_date": day, "utterances": utterances}, ensure_ascii=False)
    )
    rows = [_chunk(i, [item], mid=mid) for i, item in enumerate(utterances)]
    for row in rows:
        row.date = day
    return rows


def _pair(config: AppConfig) -> tuple[SearchResult, SearchResult]:
    """검색 문자 일치가 높은 이전 결정과 짧은 최신 변경을 함께 만든다."""
    old = _meeting(
        config,
        "meeting_20310115_091000",
        "2031-01-15",
        ["배포 일정은 지난 회의에서 월요일로 확정했습니다."],
    )[0]
    new = _meeting(
        config,
        "meeting_20310116_091000",
        "2031-01-16",
        ["금요일로 변경했습니다."],
    )[0]
    _index(config, [old, new])
    return old, new


@pytest.mark.parametrize("speaker", ["SPEAKER_00", "나"])
@pytest.mark.parametrize(
    ("focus_calls", "query"),
    [
        (1, "배포 일정은 언제 확정됐나요?"),
        (1, "배포 일정과 결과 보고, 후속 계획을 정리해 주세요."),
        (4, "배포 일정과 결과 보고, 후속 계획을 정리해 주세요."),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_speaker_filter_reaches_mixed_parent_neighbor_and_all_selectors(
    config: AppConfig, speaker: str, focus_calls: int, query: str, stream: bool
) -> None:
    """단일·보완·초점 선택과 스트림에서 다른 화자의 발화·응답을 후보에 넣지 않는다."""
    config.chat.source_focus_max_calls = focus_calls
    utterances = [
        _utterance(speaker, "배포 일정은 월요일로 확정했습니다.", 10),
        _utterance("SPEAKER_07", "디자인 일정은 화요일로 확정했습니다.", 12),
        _utterance("SPEAKER_07", "그럼 다른 게시판으로 보고해 주세요.", 14),
        _utterance(speaker, "배포 결과는 다음 회의에서 보고하겠습니다.", 16),
        _utterance("SPEAKER_07", "네 알겠습니다.", 18),
    ]
    _write(config, utterances)
    seed, neighbor = _chunk(0, utterances[:2]), _chunk(1, utterances[2:])
    _index(config, [seed, neighbor])
    engine, backend = _engine(config, [seed], _select_visible)
    if stream:
        events = [event async for event in engine.stream_chat(query, speaker_filter=speaker)]
        references = next(event["data"] for event in events if event["type"] == "references")
        answer = next(event["data"]["answer"] for event in events if event["type"] == "done")
        assert references and all(ref["quote"]["speaker"] == speaker for ref in references)
    else:
        response = await engine.chat(query, speaker_filter=speaker)
        assert response.references
        assert all(ref.quote and ref.quote.speaker == speaker for ref in response.references)
        answer = response.answer
    assert backend.chat.call_count >= 1
    prompts = "\n".join(
        call.kwargs["messages"][-1]["content"] for call in backend.chat.call_args_list
    )
    assert utterances[3]["text"] in prompts  # 실제 인접 청크의 대상 화자까지 도달한다.
    for other in (utterances[1], utterances[2], utterances[4]):
        assert other["text"] not in prompts
        assert other["text"] not in answer
    assert "월요일" in answer


async def test_invalid_selection_fallback_keeps_speaker_scope(config: AppConfig) -> None:
    """번호 선택 실패의 코드 폴백도 필터 밖 화자를 첫 원문으로 채택하지 않는다."""
    utterances = [
        _utterance("SPEAKER_07", "배포 일정은 화요일입니다.", 10),
        _utterance("SPEAKER_00", "배포 일정은 월요일입니다.", 12),
    ]
    _write(config, utterances)
    engine, _ = _engine(config, [_chunk(0, utterances)], '{"ids":[999]}')
    response = await engine.chat("배포 일정은 언제인가요?", speaker_filter="SPEAKER_00")
    assert response.references and "월요일" in response.answer
    assert "화요일" not in response.answer
    assert all(ref.quote and ref.quote.speaker == "SPEAKER_00" for ref in response.references)


def test_filtered_candidates_do_not_connect_through_excluded_turn(config: AppConfig) -> None:
    """제외한 화자의 질문·응답을 건너뛰어 서로 다른 발화를 새 문맥으로 연결하지 않는다."""
    utterances = [
        _utterance("SPEAKER_00", "배포 일정이 확정됐나요?", 10),
        _utterance("SPEAKER_07", "월요일에 배포합니다.", 12),
        _utterance("SPEAKER_00", "네 알겠습니다.", 14),
    ]
    _write(config, utterances)
    candidates = build_source_candidates(
        config, [_chunk(0, utterances)], speaker_filter="SPEAKER_00"
    )
    assert {candidate.source_index for candidate in candidates} == {0, 2}
    assert candidate_context_members(candidates)[0] == (0,)
    assert all(candidate.quote.speaker == "SPEAKER_00" for candidate in candidates)


@pytest.mark.parametrize(
    "query",
    [
        "배포 일정이 지난 회의와 이번 회의에서 어떻게 달라졌나요?",
        "배포 일정이 이전과 최근 회의에서 어떻게 달라졌나요?",
        "여러 회의 간 배포 일정 차이를 알려 주세요.",
    ],
)
async def test_comparison_preserves_both_meetings_before_and_after_selection(
    config: AppConfig, query: str
) -> None:
    """낮은 문자 일치의 최신 변경도 이전 결정과 함께 모델 입력·최종 인용에 남는다."""
    old, new = _pair(config)
    engine, backend = _engine(config, [old, new], _select_visible)
    response = await engine.chat(query)
    prompts = "\n".join(
        call.kwargs["messages"][-1]["content"] for call in backend.chat.call_args_list
    )
    assert "월요일" in prompts and "금요일" in prompts
    assert "월요일" in response.answer and "금요일" in response.answer
    assert {ref.meeting_id for ref in response.references} == {old.meeting_id, new.meeting_id}
    assert {ref.date for ref in response.references} == {old.date, new.date}


async def test_comparison_single_meeting_selection_is_explicitly_incomplete(
    config: AppConfig,
) -> None:
    """모델이 한 회의만 고르면 답을 확정했다고 표시하지 않는다."""
    old, new = _pair(config)
    engine, backend = _engine(config, [old, new], '{"ids":[0]}')
    response = await engine.chat("배포 일정이 지난 회의와 이번 회의에서 어떻게 달라졌나요?")
    assert backend.chat.call_count == 1
    assert len({ref.meeting_id for ref in response.references}) == 1
    assert "질문에 답할 근거를 확정하지 못했습니다" in response.answer


@pytest.mark.parametrize("scope_kind", ["meeting", "date", "allowed"])
def test_comparison_never_broadens_explicit_scope(config: AppConfig, scope_kind: str) -> None:
    """비교 문구보다 명시 회의·날짜·허용 목록이 우선하며 오염된 검색 응답도 걸러낸다."""
    old, new = _pair(config)
    scope = {
        "meeting": SourceScope(new.meeting_id, None),
        "date": SourceScope(None, new.date),
        "allowed": SourceScope(None, None, frozenset([new.meeting_id])),
    }[scope_kind]
    assert topic_context_results(
        "지난 회의와 이번 회의의 배포 일정을 비교해 주세요.",
        [old, new],
        [old, new],
        scope,
        15,
        config=config,
    ) == [new]


def test_comparison_reserves_each_meeting_under_chunk_cap(config: AppConfig) -> None:
    """앞 회의 청크가 많아도 두 번째 회의는 남기며 두 회의를 담을 수 없으면 차단한다."""
    old = _meeting(config, "old", "2031-01-15", [f"배포 일정 설명 {i}" for i in range(4)])
    new = _meeting(config, "new", "2031-01-16", ["금요일로 변경했습니다."])[0]
    query = "지난 회의와 이번 회의의 배포 일정을 비교해 주세요."
    scope = SourceScope(None, None)
    assert topic_context_results(query, [*old, new], [], scope, 2) == [old[0], new]
    assert topic_context_results(query, [*old, new], [], scope, 1) == []


async def test_comparison_supplements_each_meeting_without_eviction(config: AppConfig) -> None:
    """이전 회의의 조치 보충이 최신 회의의 유일한 근거를 밀어내지 않는다."""
    old = _meeting(
        config, "old", "2031-01-15", ["원인 분석은 검토 중입니다.", "임시 조치는 완료했습니다."]
    )
    new = _meeting(
        config, "new", "2031-01-16", ["원인 분석은 진행 중입니다.", "영구 조치를 완료했습니다."]
    )
    _index(config, [*old, *new])
    output = await supplement_results(
        config,
        "지난 회의와 이번 회의의 원인 분석과 완료된 조치를 비교해 주세요.",
        [old[0], new[0]],
        meeting_id_filter=None,
        date_filter=None,
        speaker_filter=None,
        max_chunks=4,
        neighbor_window=1,
    )
    assert output[:2] == [old[0], new[0]]
    assert {row.chunk_id for row in output} == {row.chunk_id for row in [*old, *new]}


@pytest.mark.parametrize(
    "field,value",
    [
        ("meeting_id_filter", "missing"),
        ("date_filter", "2031-01-02"),
        ("speaker_filter", "SPEAKER_07"),
    ],
)
async def test_index_failure_still_enforces_all_explicit_scopes(
    config: AppConfig, field: str, value: str
) -> None:
    """색인이 없거나 상한에 도달해 보충을 생략해도 기본 검색에 잘못 섞인 행을 반환하지 않는다."""
    row = _chunk(0, [_utterance("SPEAKER_00", "일정은 월요일입니다.", 10)])
    args = dict(meeting_id_filter=None, date_filter=None, speaker_filter=None)
    args[field] = value
    assert (
        await supplement_results(
            config,
            "원인 분석의 타임라인은 무엇인가요?",
            [row],
            **args,
            max_chunks=1,
            neighbor_window=0,
        )
        == []
    )


@pytest.mark.parametrize(
    "query",
    [
        "'회의 간 비교' 프로젝트의 최신 상태는 무엇인가요?",
        "두 회의실의 변경 사항을 알려 주세요.",
        "최신 회의에서 배포 일정은 어떻게 변경됐나요?",
    ],
)
def test_comparison_detection_excludes_quoted_topics_and_single_latest(query: str) -> None:
    """인용된 주제명·회의실·단일 최신 회의 요청은 기존 최신 선택 규칙을 유지한다."""
    assert not cross_meeting_comparison(query)
