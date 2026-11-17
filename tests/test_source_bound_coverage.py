"""복합 질문의 독립 초점 선택과 서버의 전역 근거 예산을 검증한다."""

from __future__ import annotations

import asyncio
import html
import json
from dataclasses import replace
from pathlib import Path

import pytest

from config import AppConfig, PathsConfig
from search.chat import _estimate_korean_tokens
from search.source_answers import (
    FOCUSED_SELECTOR_PROMPT,
    VERIFICATION_SELECTOR_PROMPT,
    QuestionFocus,
    SourceCandidate,
    SourceQuote,
    _card_topic_action_hints,
    _implicit_state_subjects,
    _postposed_reason_pair,
    _priority_fragment,
    allocate_focus_ids,
    bound_source_answer,
    build_source_candidates,
    focus_selector_prompt,
    focused_selector_payload,
    publish_uptake_context,
    question_focuses,
    question_items,
    rank_focus_candidates,
    render_source_answer,
    select_ids,
    selected_with_replies,
    speech_act_candidates,
)
from tests.test_source_bound_chat import _chunk, _engine, _utterance, _write


def payload_turns(payload: dict) -> list[dict]:
    """독립 카드의 실제 발언을 검사할 때만 한 목록으로 펼친다."""
    return [turn for unit in payload["evidence_units"] for turn in unit["turns"]]


def candidate(
    index: int, text: str, speaker: str = "A", *, context_only: bool = False
) -> SourceCandidate:
    """실제 회의와 무관한 연속 시각의 원문 후보를 만든다."""
    return SourceCandidate(
        index,
        0,
        index,
        "synthetic",
        SourceQuote(text, speaker, float(index), float(index + 1)),
        speaker,
        context_only=context_only,
    )


def test_focus_tasks_keep_every_requested_focus_under_call_limit() -> None:
    query = "일정과 대상, 채널과 담당, 과거 조치와 향후 약속, 조건과 방법을 각각 설명해 주세요."
    items = question_items(query)
    tasks = question_focuses(items, 4)
    assert len(tasks) == 4
    for item in items:
        assert (item.focus_text or item.text) in " / ".join(task.text for task in tasks)
        if item.check:
            assert any(item.check in task.checks for task in tasks)
    assert question_focuses(items, 0) == []


def test_payload_keeps_full_query_and_actual_qa_context_without_selectable_ack() -> None:
    pool = [
        candidate(0, "관리자분도 태그 가능한가요?", "B"),
        candidate(1, "그분은 메신저로.", "A"),
        candidate(2, "그럼 메신저로 공지해 주세요.", "B"),
        candidate(3, "네 알겠습니다.", "A", context_only=True),
    ]
    query = "검토 일정과 공지 대상·채널을 설명해 주세요."
    raw, packed, shown = focused_selector_payload(
        query, QuestionFocus("공지 대상·채널", ("대상", "채널")), list(reversed(pool)), 8, 10000
    )
    payload = json.loads(raw)
    assert payload["question"] == query
    assert payload["focus"] == "공지 대상·채널"
    turns = next(unit["turns"] for unit in payload["evidence_units"] if unit["id"] == 2)
    assert [turn["text"] for turn in turns] == [c.quote.text for c in pool]
    assert set(c.id for c in shown) == {0, 1, 2, 3}
    assert "id" not in turns[-1]
    assert select_ids('{"ids":[3]}', packed, 8, 1000) is None
    assert "question_items" not in payload
    assert "response_template" not in payload
    selectable = {unit["id"] for unit in payload["evidence_units"]}
    assert selectable == {candidate.id for candidate in packed}
    assert 3 not in selectable
    assert "priority_ids" not in payload and "source_turns" not in payload


def test_payload_does_not_cut_source_text_to_fit() -> None:
    pool = [candidate(0, "긴 원문" * 200), candidate(1, "짧은 독립 근거입니다.")]
    raw, packed, shown = focused_selector_payload(
        "내용과 담당을 알려 주세요.", QuestionFocus("담당", ("담당",)), pool, 8, 300
    )
    assert len(raw) <= 300
    assert [c.id for c in packed] == [1]
    assert [c.id for c in shown] == [1]
    assert payload_turns(json.loads(raw))[0]["text"] == pool[1].quote.text
    assert select_ids('{"ids":[0]}', packed, 8, 1000) is None


def test_allocator_prefers_first_evidence_from_each_focus_before_extra_background() -> None:
    pool = [candidate(i, f"독립 근거 {i}입니다.") for i in range(8)]
    ids, limited = allocate_focus_ids([[0, 1, 2, 3, 4], [5], [6], [7]], pool, pool, 4, 10000)
    assert ids == [0, 5, 6, 7]
    assert limited


def test_allocator_reserves_whole_deictic_qa_unit_and_drops_it_atomically() -> None:
    pool = [
        candidate(0, "검토는 내일 오전에 합니다."),
        candidate(1, "관리자분도 태그 가능한가요?", "B"),
        candidate(2, "그분은 메신저로.", "A"),
        candidate(3, "그럼 메신저로 공지해 주세요.", "B"),
        candidate(4, "네 알겠습니다.", "A", context_only=True),
    ]
    ids, limited = allocate_focus_ids([[0], [3]], pool, pool, 5, 10000)
    assert ids == [0, 3] and not limited
    ids, limited = allocate_focus_ids([[0], [3]], pool, pool, 4, 10000)
    assert ids == [0] and limited


def test_allocator_uses_actual_escaped_render_length_and_marks_empty_focus() -> None:
    pool = [candidate(0, "短"), candidate(1, "[x]" * 50)]
    first = selected_with_replies([0], pool, len(pool), context_candidates=pool)
    cap = len(render_source_answer(first, limited=True))
    ids, limited = allocate_focus_ids([[0], [1], []], pool, pool, 8, cap)
    assert ids == [0] and limited
    assert len(render_source_answer(first, limited=True)) <= cap


@pytest.mark.parametrize("invalid", [True, "0", 99, -1])
def test_allocator_never_admits_non_candidate_or_non_integer_ids(invalid: object) -> None:
    pool = [candidate(0, "확인된 원문입니다.")]
    ids, limited = allocate_focus_ids([[invalid]], pool, pool, 8, 10000)  # type: ignore[list-item]
    assert ids == [] and limited


def test_focused_chat_uses_one_lease_and_covers_four_independent_requests(tmp_path: Path) -> None:
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    config.chat.source_focus_max_calls = 4
    utterances = [
        _utterance("A", "리뷰는 내일 오전에 진행합니다.", 0),
        _utterance("A", "이미 담당자에게 변경 내용을 알렸습니다.", 10),
        _utterance("A", "관리자에게 메신저로 공지해 주세요.", 20),
        _utterance("A", "앞으로 공지 결과를 회신하겠습니다.", 30),
    ]
    _write(config, utterances)

    def answer(**kwargs: object) -> str:
        messages = kwargs["messages"]  # type: ignore[index]
        payload = json.loads(messages[-1]["content"])  # type: ignore[index]
        focus = payload["focus"]
        token = (
            "내일"
            if "일정" in focus
            else "관리자"
            if "대상" in focus
            else "이미"
            if "이미" in focus
            else "앞으로"
        )
        selected = [
            turn["id"] for turn in payload_turns(payload) if "id" in turn and token in turn["text"]
        ]
        return json.dumps({"ids": selected})

    engine, backend = _engine(config, [_chunk(0, utterances)], answer)
    response = asyncio.run(
        engine.chat(
            "리뷰 일정과 공지 대상·채널, 이미 한 일과 앞으로 할 일을 구분해 주세요.",
            meeting_id_filter="sample",
        )
    )
    assert backend.chat.call_count == 4
    assert engine._model_manager.acquire.call_count == 1
    assert all(text["text"] in html.unescape(response.answer) for text in utterances)
    assert response.llm_used and response.llm_called
    assert len(response.references) == 4


def test_focused_empty_selections_do_not_publish_arbitrary_first_source(tmp_path: Path) -> None:
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    config.chat.source_focus_max_calls = 4
    utterances = [_utterance("A", "무관한 배경입니다.", 0)]
    _write(config, utterances)
    engine, backend = _engine(config, [_chunk(0, utterances)], '{"ids":[]}')
    response = asyncio.run(
        engine.chat("일정과 담당을 각각 알려 주세요.", meeting_id_filter="sample")
    )
    assert backend.chat.call_count <= 4
    assert response.llm_called and not response.llm_used
    assert not response.references
    assert "무관한 배경" not in response.answer


@pytest.mark.parametrize("mutation", ["epoch", "source", "cancel"])
async def test_focused_calls_recheck_unselected_source_and_cancel_without_history(
    tmp_path: Path, mutation: str
) -> None:
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    utterances = [
        _utterance("A", "검토 일정은 내일입니다.", 0),
        _utterance("B", "공지 대상은 관리자입니다.", 10),
        _utterance("C", "향후 추가 공지를 하겠습니다.", 20),
    ]
    path = _write(config, utterances)
    calls = 0

    def answer(**_: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            if mutation == "epoch":
                engine._transcript_epoch += 1
            elif mutation == "source":
                changed = [dict(row) for row in utterances]
                changed[2]["text"] = "선택하지 않은 원문을 수정했습니다."
                path.write_text(json.dumps({"utterances": changed}, ensure_ascii=False))
            else:
                raise asyncio.CancelledError()
        return '{"ids":[0]}'

    engine, backend = _engine(config, [_chunk(0, utterances)], answer)
    if mutation == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await engine.chat(
                "검토 일정, 공지 대상, 향후 계획을 알려 주세요.", session_id="changed"
            )
    else:
        response = await engine.chat(
            "검토 일정, 공지 대상, 향후 계획을 알려 주세요.", session_id="changed"
        )
        assert not response.references and not response.llm_used
    assert backend.chat.call_count == 2
    assert not engine.get_session("changed").history
    engine._model_manager.acquire.return_value.__aexit__.assert_awaited_once()


def test_focused_parser_cannot_use_id_only_shown_in_another_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    utterances = [
        _utterance("A", "일정은 내일입니다.", 0),
        _utterance("B", "담당은 운영팀입니다.", 10),
    ]
    _write(config, utterances)
    count = 0

    def scoped_payload(
        query: str,
        focus: QuestionFocus,
        pool: list[SourceCandidate],
        max_items: int,
        max_chars: int,
        **_: object,
    ) -> tuple[str, list[SourceCandidate], list[SourceCandidate]]:
        nonlocal count
        chosen = [pool[count]]
        count += 1
        return (
            json.dumps(
                {"focus": focus.text, "source_turns": [c.selection_data() for c in chosen]}
            ),
            chosen,
            chosen,
        )

    monkeypatch.setattr("search.chat.focused_selector_payload", scoped_payload)
    engine, backend = _engine(config, [_chunk(0, utterances)], '{"ids":[1]}')
    response = asyncio.run(engine.chat("일정, 담당을 알려 주세요."))
    assert backend.chat.call_count == 2
    assert [reference.quote.text for reference in response.references if reference.quote] == [
        utterances[1]["text"]
    ]
    assert "일정은 내일" not in response.answer
    assert "확정하지 못했습니다" in response.answer


@pytest.mark.parametrize("max_calls,coverage,expected_calls", [(1, 1, 2), (4, 0, 1)])
def test_focus_opt_out_preserves_legacy_selection_protocol(
    tmp_path: Path, max_calls: int, coverage: int, expected_calls: int
) -> None:
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    config.chat.source_focus_max_calls = max_calls
    config.chat.source_coverage_passes = coverage
    utterances = [_utterance("A", "일정은 내일이며 담당은 운영팀입니다.", 0)]
    _write(config, utterances)
    engine, backend = _engine(config, [_chunk(0, utterances)], '{"ids":[0]}')
    response = asyncio.run(engine.chat("일정, 담당을 알려 주세요."))
    assert response.references
    assert backend.chat.call_count == expected_calls
    for call in backend.chat.call_args_list:
        payload = json.loads(call.kwargs["messages"][-1]["content"])
        assert "focus" not in payload and "question_items" in payload


@pytest.mark.parametrize("max_calls", [1, 4])
async def test_speaker_order_change_outside_shown_chunk_rejects_stale_labels(
    tmp_path: Path, max_calls: int
) -> None:
    """미표시 앞 발화의 화자 변경으로 표시명이 재배치되면 원문이 같아도 게시하지 않는다."""
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    config.chat.source_focus_max_calls = max_calls
    utterances = [
        _utterance("SPEAKER_09", "무관한 시작 인사입니다.", 0),
        _utterance("SPEAKER_03", "검토 일정은 내일입니다.", 10),
        _utterance("SPEAKER_01", "공지 대상은 관리자입니다.", 20),
    ]
    path = _write(config, utterances)
    results = [_chunk(0, utterances[1:])]
    before = build_source_candidates(config, results)
    selected_before = next(c for c in before if c.quote.speaker == "SPEAKER_01")
    calls = 0

    def answer(**_: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            changed = [dict(row) for row in utterances]
            changed[0]["speaker"] = "SPEAKER_01"
            path.write_text(json.dumps({"utterances": changed}, ensure_ascii=False))
        return json.dumps({"ids": [selected_before.id]})

    engine, backend = _engine(config, results, answer)
    response = await engine.chat("검토 일정, 공지 대상을 알려 주세요.", session_id="labels")
    after = build_source_candidates(config, results)
    selected_after = next(c for c in after if c.id == selected_before.id)
    assert selected_before.quote == selected_after.quote
    assert selected_before.source_index == selected_after.source_index
    assert selected_before.display_speaker != selected_after.display_speaker
    assert backend.chat.call_count == 2
    assert not response.references and not response.llm_used
    assert not engine.get_session("labels").history
    engine._model_manager.acquire.return_value.__aexit__.assert_awaited_once()


def test_each_focus_has_distinct_priority_even_when_full_query_is_identical() -> None:
    pool = [candidate(0, "배포는 내일 오전입니다."), candidate(1, "공지는 메신저로 합니다.")]
    query = "배포 일정과 공지 채널을 알려 주세요."
    first, _, _ = focused_selector_payload(
        query, QuestionFocus("배포 일정", ("일정",)), pool, 3, 10000
    )
    second, _, _ = focused_selector_payload(
        query, QuestionFocus("공지 채널", ("채널",)), pool, 3, 10000
    )
    assert json.loads(first)["evidence_units"][0]["id"] == 0
    assert json.loads(second)["evidence_units"][0]["id"] == 1
    # 카드는 검색 순위이며 각 카드 안에서만 실제 순서를 보존한다.
    assert {row["id"] for row in payload_turns(json.loads(first))} == {0, 1}
    assert {row["id"] for row in payload_turns(json.loads(second))} == {0, 1}


def test_cards_merge_only_identical_closures_and_keep_partial_overlap_complete() -> None:
    pool = [
        candidate(0, "어떤 자료를 전달하나요?", "A"),
        candidate(1, "네 요청하신 자료를 전달합니다.", "B"),
        candidate(2, "추가로 기록도 보냅니다.", "B"),
    ]
    raw, packed, shown = focused_selector_payload(
        "전달 자료는?", QuestionFocus("전달 자료", ("공유",)), pool, 3, 10000, max_units=8
    )
    units = json.loads(raw)["evidence_units"]
    question_card = next(unit for unit in units if unit["id"] == 0)
    reply_card = next(unit for unit in units if unit["id"] == 1)
    assert question_card is not reply_card
    assert [turn["text"] for turn in question_card["turns"]] == [c.quote.text for c in pool]
    assert [turn["text"] for turn in reply_card["turns"]] == [c.quote.text for c in pool[:2]]
    assert {c.id for c in packed} == {c.id for c in shown} == {0, 1, 2}
    assert "id" not in reply_card["turns"][0]


def test_card_order_is_focus_rank_while_internal_times_and_scope_remain_actual() -> None:
    pool = [
        candidate(0, "자료는 별도로 기록했습니다.", "B"),
        candidate(1, "검토 자료를 확인하고", "A"),
        candidate(2, "내일 오전에 검토 자료를 공유하겠습니다.", "A"),
    ]
    foreign = replace(candidate(3, "검토 자료는 다음 주에 보냅니다."), meeting_id="another")
    raw, _, _ = focused_selector_payload(
        "검토 자료의 일정은?",
        QuestionFocus("검토 자료 일정", ("일정",)),
        [*pool, foreign],
        3,
        10000,
        max_units=8,
    )
    payload = json.loads(raw)
    joined_card = next(unit for unit in payload["evidence_units"] if unit["id"] == 2)
    assert [turn["text"] for turn in joined_card["turns"]] == [c.quote.text for c in pool[1:]]
    assert [turn["time"] for turn in joined_card["turns"]] == ["1.0–2.0s", "2.0–3.0s"]
    assert set(payload["meeting_scopes"].values()) == {"synthetic", "another"}
    assert payload["meeting_scopes"][joined_card["scope"]] == "synthetic"
    assert "시간순이 아닙니다" in FOCUSED_SELECTOR_PROMPT
    assert "마지막 상태는 카드 순위로 판단하지" in FOCUSED_SELECTOR_PROMPT


def test_focus_card_width_is_bounded_and_unshown_ids_cannot_be_selected() -> None:
    pool = [candidate(index, f"독립 자료 {index}입니다.", f"A{index}") for index in range(40)]
    raw, packed, shown = focused_selector_payload(
        "자료를 알려 주세요.", QuestionFocus("자료", ()), pool, 3, 10000, max_units=2
    )
    assert len(json.loads(raw)["evidence_units"]) == 2
    assert len(packed) == len(shown) == 2
    omitted = next(c.id for c in pool if c.id not in {p.id for p in packed})
    assert select_ids(json.dumps({"ids": [omitted]}), packed, 3, 1000) is None


@pytest.mark.parametrize(
    "boundary",
    ["continuous", "other_actor", "gap", "unknown", "later_topic", "transition", "quoted"],
)
def test_card_topic_hint_inherits_only_actual_preceding_same_actor_subject(boundary: str) -> None:
    focus = QuestionFocus("알파 자료를 어떻게 공유하고 확인하기로 했나요?", ("어떻게", "확인"))
    pool = [
        candidate(0, "알파 자료의 범위는 정리했습니다.", "A"),
        candidate(1, "보완할 내용도 조금 남아 있습니다.", "A"),
        candidate(2, "공유해주시고 제가 확인해보도록 하겠습니다.", "A"),
    ]
    if boundary == "other_actor":
        pool[2] = replace(pool[2], quote=replace(pool[2].quote, speaker="B"))
    elif boundary == "gap":
        pool[2] = replace(pool[2], source_index=5)
    elif boundary == "unknown":
        pool = [replace(c, quote=replace(c.quote, speaker=" UNKNOWN ")) for c in pool]
    elif boundary == "later_topic":
        pool[0], pool[2] = (
            replace(pool[2], id=0, source_index=0),
            replace(pool[0], id=2, source_index=2),
        )
    elif boundary == "transition":
        pool[1] = replace(pool[1], quote=replace(pool[1].quote, text="다음 주제입니다."))
    elif boundary == "quoted":
        focus = QuestionFocus("'알파 자료 공유 확인' 문서 담당은 누구인가요?", ("담당",))
    hints = _card_topic_action_hints(focus, pool, query=focus.text)
    assert (2 in hints) == (boundary == "continuous")
    assert not ({0, 1} & hints)


@pytest.mark.parametrize(
    "focus",
    [
        QuestionFocus("5%·3%·4%가 확정 비율인가요?", ("확정",)),
        QuestionFocus("5%·3%·4% 비율을 공유하는 방법은?", ("방법",)),
        QuestionFocus("알파 공유 방법은?", ("방법",)),
        QuestionFocus("알파 제보 공유기와 확인할당 문서의 담당은?", ("담당",)),
    ],
)
def test_numeric_or_noun_only_focus_does_not_inherit_an_unrelated_shared_verb(
    focus: QuestionFocus,
) -> None:
    pool = [
        candidate(0, "베타 자료가 있습니다."),
        candidate(1, "공유해주시고 확인해보도록 하겠습니다."),
    ]
    assert not _card_topic_action_hints(focus, pool, query=focus.text)


def test_card_topic_hint_does_not_use_quoted_source_title_as_actual_subject() -> None:
    focus = QuestionFocus("알파 공유 방법은?", ("방법",))
    pool = [
        candidate(0, "'알파'는 문서 제목입니다."),
        candidate(1, "공유해주시고 확인해보도록 하겠습니다."),
    ]
    assert not _card_topic_action_hints(focus, pool, query=focus.text)


def test_card_qa_hint_uses_actual_request_actor_not_substantive_reply_actor() -> None:
    focus = QuestionFocus("알파를 어떻게 공유하나요?", ("어떻게",))
    pool = [
        candidate(0, "알파 결과를 말씀드리겠습니다.", "A"),
        candidate(1, "결과를 공유할까요?", "A"),
        candidate(2, "네 공유하고 확인해보도록 하겠습니다.", "B"),
    ]
    assert _card_topic_action_hints(focus, pool, query=focus.text) == {1}
    raw, _, _ = focused_selector_payload(focus.text, focus, pool, 3, 10000, max_units=1)
    card = json.loads(raw)["evidence_units"][0]
    assert [turn["speaker"] for turn in card["turns"]] == ["A", "B"]
    assert {turn["text"] for turn in card["turns"]} == {c.quote.text for c in pool[1:]}
    assert pool[0].quote.text not in {turn["text"] for turn in card["turns"]}


def test_card_action_hint_does_not_turn_generic_materials_into_an_unrelated_topic() -> None:
    query = "압축 변경을 설명해 주세요. 공유할 자료와 예상 기간을 알려 주세요."
    focus = QuestionFocus("공유할 자료와 예상 기간", ("공유", "기간"))
    pool = [
        candidate(0, "자료 형태는 기록했습니다.", "A"),
        candidate(1, "다른 환경의 설계를 공유할까요?", "A"),
        candidate(2, "요청하신 자료를 공유드리겠습니다.", "B"),
        candidate(3, "자료를 받으면 3일 정도 걸릴 것 같습니다.", "B"),
    ]
    assert not _card_topic_action_hints(focus, pool, query=query)
    raw, _, _ = focused_selector_payload(query, focus, pool, 3, 10000)
    first_two = {unit["id"] for unit in json.loads(raw)["evidence_units"][:2]}
    assert 1 not in first_two and 3 in first_two


def test_card_action_hint_preserves_actual_topic_shared_with_the_first_question() -> None:
    query = "알파 절차를 설명해 주세요. 알파 내용이 모호하면 어떻게 제보하고 확인하나요?"
    focus = QuestionFocus("알파 내용이 모호하면 어떻게 제보하고 확인하나요?", ("어떻게", "확인"))
    pool = [
        candidate(0, "알파 기준은 이 자료에 있습니다."),
        candidate(1, "설명과 다른 경우도 있을 것 같습니다."),
        candidate(2, "제보해주시고 제가 확인해보도록 하겠습니다."),
    ]
    assert _card_topic_action_hints(focus, pool, query=query) == {2}
    raw, _, _ = focused_selector_payload(query, focus, pool, 3, 10000)
    assert json.loads(raw)["evidence_units"][0]["id"] == 2


def test_temporal_and_constraint_priority_is_generic_and_does_not_add_unselected_ids() -> None:
    pool = [
        candidate(0, "그거는 화면을 정리하면 됩니다."),
        candidate(1, "담당자에게 공지해 놨습니다."),
        candidate(2, "수정하지 마시고 문서 번호 또는 캡처를 보내주시면 확인하겠습니다."),
    ]
    past = QuestionFocus("이미 한 일", ("이미",))
    method = QuestionFocus("모호한 내용은 어디에 어떻게 제보하나요?", ("어디", "어떻게"))
    assert (
        rank_focus_candidates("공지에서 이미 한 일과 향후 조치를 알려 주세요.", past, pool)[0].id
        == 1
    )
    assert (
        rank_focus_candidates("모호한 내용은 어디에 어떻게 제보하나요?", method, pool)[0].id == 2
    )
    ids, limited = allocate_focus_ids(
        [[0, 2]], pool, pool, 1, 10000, query="제보 방법은?", focuses=[method]
    )
    assert ids == [2] and limited
    ids, _ = allocate_focus_ids(
        [[0]], pool, pool, 1, 10000, query="제보 방법은?", focuses=[method]
    )
    assert ids == [0]


def test_focus_per_call_direct_anchor_limit_is_enforced_by_payload_and_parser(
    tmp_path: Path,
) -> None:
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    config.chat.source_focus_max_items = 3
    utterances = [
        _utterance("A", f"독립적인 요청 항목 {index}입니다.", index * 10) for index in range(4)
    ]
    _write(config, utterances)
    engine, backend = _engine(config, [_chunk(0, utterances)], '{"ids":[0,1,2,3]}')
    response = asyncio.run(engine.chat("일정, 담당을 각각 알려 주세요."))
    assert not response.references and not response.llm_used
    for call in backend.chat.call_args_list:
        payload = json.loads(call.kwargs["messages"][-1]["content"])
        assert payload["max_items"] == 3


@pytest.mark.parametrize(
    "focus",
    [
        QuestionFocus("알파 릴리스 향후 조치", ("향후",)),
        QuestionFocus("알파 릴리스의 모호한 내용을 어떻게 확인하나요?", ("어떻게", "확인")),
    ],
)
def test_unrelated_temporal_or_constraint_cues_do_not_beat_actual_focus_subject(
    focus: QuestionFocus,
) -> None:
    pool = [
        candidate(0, "베타는 내일 수정하지 마시고 파일을 보내주시면 확인하겠습니다."),
        candidate(1, "알파 릴리스 부분을 검토 중입니다."),
    ]
    assert rank_focus_candidates("알파 릴리스의 향후 조치와 확인 방법은?", focus, pool)[0].id == 1


@pytest.mark.parametrize(
    "focus",
    [
        QuestionFocus("앞으로 확인하거나 공유하기로 한 작업", ("앞으로", "확인")),
        QuestionFocus("추후 검토하고 회신하기로 한 일", ("추후", "검토")),
    ],
)
def test_inflected_generic_task_verbs_do_not_suppress_future_evidence(
    focus: QuestionFocus,
) -> None:
    pool = [
        candidate(0, "지난 자료를 확인했고 공유한 적이 있습니다."),
        candidate(1, "시간대별로 비교해 볼게요."),
    ]
    assert rank_focus_candidates("앞으로 확인하거나 공유하기로 한 작업은?", focus, pool)[0].id == 1


def test_generic_verb_normalization_preserves_real_compound_subject_nouns() -> None:
    focus = QuestionFocus("공유기 상태의 향후 조치", ("향후",))
    pool = [
        candidate(0, "모뎀 설정은 내일 확인하도록 하겠습니다."),
        candidate(1, "공유기 부분을 조사 중입니다."),
    ]
    assert rank_focus_candidates("공유기 상태의 향후 조치는?", focus, pool)[0].id == 1


@pytest.mark.parametrize("subject", ["처리한계", "확인할당", "보고서", "확인해상도"])
def test_finite_verb_endings_do_not_erase_compound_topic_nouns(subject: str) -> None:
    focus = QuestionFocus(f"{subject} 상태의 향후 조치", ("향후",))
    pool = [
        candidate(0, "다른 서비스는 내일 확인하도록 하겠습니다."),
        candidate(1, f"{subject} 부분을 조사 중입니다."),
    ]
    assert rank_focus_candidates(f"{subject} 상태의 향후 조치는?", focus, pool)[0].id == 1


def test_korean_sentence_boundary_keeps_decimals_and_quoted_periods() -> None:
    query = (
        "목표는 1.2배이고 '검증. 종료' 문구를 확인합니다. 추후 검증과 종료 범위는 어디까지인가요?"
    )
    items = question_items(query)
    clauses = list(dict.fromkeys(item.text for item in items))
    assert len(clauses) == 2
    assert "1.2" in clauses[0] and "검증. 종료" in clauses[0]
    assert {"추후", "검증", "종료", "범위"}.issubset({item.check for item in items})
    assert "어디까지" in clauses[1]
    assert "어디" not in {item.check for item in items}


def test_korean_identifier_period_is_not_a_sentence_boundary() -> None:
    items = question_items("회사.한국 인증서를 확인합니다. 추후 검증 범위를 알려 주세요.")
    clauses = list(dict.fromkeys(item.text for item in items))
    assert len(clauses) == 2
    assert clauses[0] == "회사.한국 인증서를 확인합니다."


@pytest.mark.parametrize("same_actor", [False, True])
def test_limited_observation_keeps_its_actual_preceding_scope_qualifier(same_actor: bool) -> None:
    pool = [
        candidate(0, "제가 한번 확인한 것 중에", "A"),
        candidate(1, "그래서 현재 상태는 나아진 것으로 보입니다.", "A" if same_actor else "B"),
    ]
    selected = selected_with_replies([1], pool, 8, context_candidates=pool)
    assert [c.id for c in selected] == ([0, 1] if same_actor else [1])
    if same_actor:
        assert selected[0].is_context


def test_identical_closure_card_exposes_only_highest_ranked_actual_source_id() -> None:
    pool = [replace(candidate(0, "자료를 확인하고"), id=100), candidate(1, "오늘 공유하겠습니다.")]
    raw, packed, shown = focused_selector_payload(
        "자료 확인과 공유를 알려 주세요.", QuestionFocus("자료 확인", ("확인",)), pool, 3, 10000
    )
    assert {c.id for c in packed} == {100}
    assert {c.id for c in shown} == {100, 1}
    assert select_ids('{"ids":[100]}', packed, 3, 1000) == [100]
    assert [turn["id"] for turn in payload_turns(json.loads(raw)) if "id" in turn] == [100]
    assert select_ids('{"ids":[1]}', packed, 3, 1000) is None
    assert "ids" not in json.loads(raw)["evidence_units"][0]
    assert len(json.loads(raw)["evidence_units"]) == 1
    exact, exact_packed, _ = focused_selector_payload(
        "자료 확인과 공유를 알려 주세요.", QuestionFocus("자료 확인", ("확인",)), pool, 3, len(raw)
    )
    assert len(exact) == len(raw) and {c.id for c in exact_packed} == {100}
    _, too_small, _ = focused_selector_payload(
        "자료 확인과 공유를 알려 주세요.",
        QuestionFocus("자료 확인", ("확인",)),
        pool,
        3,
        len(raw) - 1,
    )
    assert not too_small


@pytest.mark.parametrize("cap", [200, 237, 238, 275])
def test_context_promotion_never_splits_or_exceeds_actual_json_budget(cap: int) -> None:
    pool = [replace(candidate(0, "자료를 확인하고"), id=100), candidate(1, "오늘 공유하겠습니다.")]
    raw, packed, shown = focused_selector_payload(
        "자료 확인과 공유를 알려 주세요.", QuestionFocus("자료 확인", ("확인",)), pool, 3, cap
    )
    assert len(raw) <= cap
    assert all(
        turn["text"] in {candidate.quote.text for candidate in pool}
        for turn in payload_turns(json.loads(raw))
    )
    assert {c.id for c in packed}.issubset({c.id for c in shown})


def test_focus_payload_preserves_original_completion_predicate() -> None:
    query = "원인 분석과 예방 대책까지 완료된 건지, 추후 확인할 일을 알려 주세요."
    focus = question_focuses(question_items(query), 4)[0]
    assert focus.text == "원인 분석"
    assert "완료된 건지" in focus.scope_question
    raw, _, _ = focused_selector_payload(
        query, focus, [candidate(0, "필요한 조치를 취했습니다.")], 3, 10000
    )
    assert json.loads(raw)["scope_question"] == focus.scope_question


@pytest.mark.parametrize("head", ["이번 보고서에는", "회복 방법에 대한", "검증을 위한"])
def test_predicate_less_postposition_fragment_keeps_whole_same_actor_clause(head: str) -> None:
    pool = [candidate(0, head), candidate(1, "구체적인 상태를 기록하겠습니다.")]
    assert [c.id for c in selected_with_replies([0], pool, 8, context_candidates=pool)] == [0, 1]
    assert [c.id for c in selected_with_replies([1], pool, 8, context_candidates=pool)] == [0, 1]
    ids, limited = allocate_focus_ids([[0]], pool, pool, 1, 10000)
    assert ids == [] and limited


def test_completion_predicate_prioritizes_actual_past_report_over_future_document_heading() -> (
    None
):
    focus = QuestionFocus("원인 분석", ("원인",), "원인 분석과 대책까지 완료된 건지 알려 주세요.")
    pool = [
        candidate(0, "분석 보고서에는"),
        candidate(1, "앞으로 내용을 담겠습니다."),
        candidate(2, "필요한 조치를 취했고요."),
        candidate(3, "상황은 중재되었습니다."),
    ]
    assert (
        rank_focus_candidates("원인 분석과 대책까지 완료된 건지 알려 주세요.", focus, pool)[0].id
        == 2
    )


def test_allocator_closes_all_model_selected_contexts_before_admitting_anchor() -> None:
    pool = [
        candidate(0, "이쪽만인가요?", "A"),
        candidate(1, "네 이쪽에만 존재해서", "B"),
        candidate(2, "이 항목에서 제외해 주세요.", "B"),
    ]
    focus = QuestionFocus("처리 방법", ("방법",))
    ids, limited = allocate_focus_ids([[1, 2]], pool, pool, 2, 10000, focuses=[focus])
    assert ids == [] and limited
    ids, limited = allocate_focus_ids([[1, 2]], pool, pool, 3, 10000, focuses=[focus])
    assert set(ids) == {1, 2} and not limited
    assert [c.id for c in selected_with_replies(ids, pool, 8, context_candidates=pool)] == [
        0,
        1,
        2,
    ]


def test_later_allocation_rounds_delay_fragment_filler_without_rejecting_names() -> None:
    pool = [
        candidate(0, "범위는 일부 확인입니다."),
        candidate(1, "인입서비스도"),
        candidate(2, "추후 다시 확인하겠습니다."),
    ]
    ids, limited = allocate_focus_ids(
        [[0, 1], [0, 2]],
        pool,
        pool,
        2,
        10000,
        focuses=[QuestionFocus("확인 범위", ("확인",)), QuestionFocus("후속", ("후속",))],
    )
    assert ids == [0, 2] and limited
    name = [candidate(0, "홍길동")]
    assert allocate_focus_ids(
        [[0]], name, name, 1, 10000, focuses=[QuestionFocus("담당", ("담당",))]
    )[0] == [0]


@pytest.mark.parametrize("relation", ["reply", "same_actor", "nonconsecutive"])
def test_dialogue_priority_requires_real_other_actor_consecutive_question_context(
    relation: str,
) -> None:
    pool = [
        candidate(0, "결과 공유를 하겠습니다.", "B"),
        candidate(1, "결과는 어떻게 공유하나요?", "A"),
        candidate(
            2 if relation != "nonconsecutive" else 3,
            "네 확인하고 회신할게요.",
            "B" if relation != "same_actor" else "A",
        ),
    ]
    focus = QuestionFocus("결과 공유 방법", ("방법",))
    ranked = rank_focus_candidates("결과 공유 방법은?", focus, pool)
    assert ranked[0].id == (2 if relation == "reply" else 0)


@pytest.mark.parametrize("cap", [3, 4])
@pytest.mark.parametrize("overlap", [False, True])
def test_contextual_model_selected_request_keeps_its_full_atomic_uptake_unit(
    cap: int, overlap: bool
) -> None:
    pool = [
        candidate(0, "관리자분도 메신저로 공지 가능한가요?", "B"),
        candidate(1, "그분은 메신저로.", "A"),
        candidate(2, "그럼 메신저로 공지해 주세요.", "B"),
        candidate(3, "네 알겠습니다.", "A", context_only=True),
    ]
    selections = [[0], [2, 1] if overlap else [2]]
    ids, limited = allocate_focus_ids(selections, pool, pool, cap, 10000)
    selected = selected_with_replies(ids, pool, len(pool), context_candidates=pool)
    published, final_limited, units = publish_uptake_context(
        selected, ids, pool, cap, 10000, enabled=True, limited=limited
    )
    assert 3 not in ids
    if cap == 4:
        assert {0, 2}.issubset(ids)
        assert [c.id for c in published] == [0, 1, 2, 3]
        assert not final_limited
        assert set(units[2]) == {0, 1, 2, 3}
        assert not next(c for c in published if c.id == 2).is_context
    else:
        assert ids == [] and published == [] and final_limited


def test_contextual_model_selected_uptake_request_is_not_stranded_by_character_cap() -> None:
    pool = [
        candidate(0, "관리자분도 메신저로 공지 가능한가요?", "B"),
        candidate(1, "그분은 메신저로.", "A"),
        candidate(2, "그럼 메신저로 공지해 주세요.", "B"),
        candidate(3, "네 알겠습니다.", "A", context_only=True),
    ]
    ids, _ = allocate_focus_ids([[0], [2]], pool, pool, 4, 10000)
    selected = selected_with_replies(ids, pool, len(pool), context_candidates=pool)
    full, _, _ = publish_uptake_context(selected, ids, pool, 4, 10000, enabled=True)
    cap = len(render_source_answer(full, limited=True)) - 1
    limited_ids, limited = allocate_focus_ids([[0], [2]], pool, pool, 4, cap)
    assert limited_ids == [] and limited


def test_failed_connected_unit_preserves_unrelated_admitted_evidence() -> None:
    pool = [
        candidate(0, "관리자분도 메신저로 공지 가능한가요?", "B"),
        candidate(1, "그분은 메신저로.", "A"),
        candidate(2, "그럼 메신저로 공지해 주세요.", "B"),
        candidate(3, "네 알겠습니다.", "A", context_only=True),
        candidate(4, "별도 확인은 내일 합니다.", "C"),
    ]
    ids, limited = allocate_focus_ids([[4], [0], [2]], pool, pool, 4, 10000)
    assert set(ids) == {0, 2} and limited
    assert {c.id for c in selected_with_replies(ids, pool, 4, context_candidates=pool)} == {
        0,
        1,
        2,
        3,
    }
    # 연결 단위가 예산에 맞지 않을 때에는 독립 근거를 보존한다.
    ids, limited = allocate_focus_ids([[4], [0], [2]], pool, pool, 3, 10000)
    assert ids == [4] and limited


def test_closing_scope_prioritizes_actual_closing_report() -> None:
    focus = QuestionFocus(
        "종료 범위", ("종료", "범위"), "이번에 정리한 종료 범위를 설명해 주세요."
    )
    pool = [
        candidate(0, "현재 내용을 추가 검토하고 있습니다."),
        candidate(1, "그때 정리한 내용은 마무리했고 종료했습니다."),
    ]
    assert rank_focus_candidates("이번에 정리한 종료 범위는?", focus, pool)[0].id == 1


def test_verification_scope_can_show_forecast_without_turning_it_into_completion() -> None:
    focus = QuestionFocus("전체 검증까지 끝난 건지도", ("검증", "끝난"))
    pool = [
        candidate(0, "다른 배경 설명입니다."),
        candidate(1, "앞으로 이 문제는 발생하지 않을 것으로 예상됩니다."),
    ]
    assert rank_focus_candidates("전체 검증까지 끝난 건지도 알려 주세요.", focus, pool)[0].id == 1
    report = candidate(2, "전체 검증을 완료한 결과입니다.")
    assert (
        rank_focus_candidates("전체 검증까지 끝난 건지도 알려 주세요.", focus, [*pool, report])[
            0
        ].id
        == 2
    )
    rendered = html.unescape(render_source_answer([pool[1]]))
    assert pool[1].quote.text in rendered
    assert "검증이 완료되었습니다" not in rendered


def test_unrelated_verification_or_closing_cues_do_not_override_actual_subject() -> None:
    focus = QuestionFocus("알파 전체 검증이 끝난 범위", ("검증", "범위"))
    pool = [
        candidate(0, "베타 전체 검증은 종료했고 앞으로 문제가 없을 것으로 예상됩니다."),
        candidate(1, "알파의 현재 상태를 살펴보고 있습니다."),
    ]
    assert rank_focus_candidates("알파 전체 검증이 끝난 범위는?", focus, pool)[0].id == 1


def test_linked_copula_fragments_keep_the_whole_finite_clause_chain() -> None:
    pool = [
        candidate(0, "시각은 임의로 정한 거고"),
        candidate(1, "날짜도 조정할 수 있는 것이고"),
        candidate(2, "최종 시각은 추후 정리하겠습니다."),
    ]
    selected = selected_with_replies([0], pool, 8, context_candidates=pool)
    assert [c.id for c in selected] == [0, 1, 2]
    assert [c.id for c in selected_with_replies([2], pool, 8, context_candidates=pool)] == [
        0,
        1,
        2,
    ]
    ids, limited = allocate_focus_ids([[0]], pool, pool, 2, 10000)
    assert ids == [] and limited


def test_closing_speech_act_keeps_full_context_without_promoting_ineligible_context() -> None:
    focus = QuestionFocus("종료 범위", ("종료", "범위"))
    pool = [
        candidate(0, "일단 닫고, 오해가 있어서"),
        candidate(1, "추가 항목을 다르게 이해했습니다."),
        candidate(2, "현재 상태가 좋아 보입니다."),
    ]
    eligible, fallback = speech_act_candidates("종료 범위를 알려 주세요.", focus, pool)
    assert [c.id for c in eligible] == [0] and not fallback
    raw, packed, shown = focused_selector_payload(
        "종료 범위를 알려 주세요.", focus, pool, 3, 10000
    )
    assert [c.id for c in packed] == [0]
    assert {c.id for c in shown} == {0, 1}
    assert select_ids('{"ids":[1]}', packed, 3, 1000) is None
    assert "id" not in payload_turns(json.loads(raw))[1]


def test_verification_speech_act_distinguishes_future_event_from_observation_and_polite_request() -> (
    None
):
    focus = QuestionFocus("전체 검증까지 끝난 건지", ("검증",))
    pool = [
        candidate(0, "현재 문제는 없어 보입니다."),
        candidate(1, "제외해 주시면 좋을 것 같아요."),
        candidate(2, "네 그럼 될 것 같아요."),
        candidate(3, "앞으로 문제가 발생하지 않을 테니까 이슈는 없을 것 같아요."),
        candidate(4, "아직 전체 검증을 완료하지 못했습니다."),
        candidate(5, "전체 검증이 끝났나요?"),
        candidate(6, "검증해 주세요."),
        candidate(7, "검증해 주시면 좋을 것 같아요."),
    ]
    eligible, fallback = speech_act_candidates("전체 검증까지 끝난 건지 알려 주세요.", focus, pool)
    assert {c.id for c in eligible} == {3, 4} and not fallback


@pytest.mark.parametrize("boundary", ["same_actor", "other_actor", "new_topic"])
def test_closing_followup_requires_same_actor_contiguous_closing_block(boundary: str) -> None:
    focus = QuestionFocus("종료 범위", ("종료",))
    pool = [
        candidate(0, "일단 이 건은 닫겠습니다.", "A"),
        candidate(
            1,
            "다른 안건입니다." if boundary == "new_topic" else "추가 항목은 정리했습니다.",
            "B" if boundary == "other_actor" else "A",
        ),
        candidate(2, "추후 문제가 발생하면 다시 알려 주세요.", "A"),
    ]
    eligible, fallback = speech_act_candidates("종료 범위는?", focus, pool)
    assert not fallback
    assert {c.id for c in eligible} == ({0, 2} if boundary == "same_actor" else {0})


def test_speech_act_subject_guard_and_empty_specific_pool_preserve_broad_limited_fallback() -> (
    None
):
    focus = QuestionFocus("알파 종료 범위", ("종료",))
    unrelated = candidate(0, "베타는 종료했습니다.")
    related = candidate(1, "알파는 마무리했습니다.")
    assert [
        c.id for c in speech_act_candidates("알파 종료 범위는?", focus, [unrelated, related])[0]
    ] == [1]
    available, fallback = speech_act_candidates("알파 종료 범위는?", focus, [unrelated])
    assert available == [unrelated] and fallback
    nominal = [candidate(0, "홍길동")]
    assert speech_act_candidates(
        "담당은 누구인가요?", QuestionFocus("담당", ("담당",)), nominal
    ) == (nominal, False)


@pytest.mark.parametrize("name", ["전체검증", "종료관리"])
def test_project_name_alone_does_not_activate_speech_act_filter(name: str) -> None:
    pool = [candidate(0, "홍길동")]
    focus = QuestionFocus(f"'{name}' 프로젝트 담당은 누구인가요?", ("담당",))
    assert speech_act_candidates(focus.text, focus, pool) == (pool, False)


def test_missing_act_specific_anchor_is_limited_in_actual_chat_render(tmp_path: Path) -> None:
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    utterances = [_utterance("A", "현재 상태는 좋아 보입니다.", 0)]
    _write(config, utterances)
    engine, _ = _engine(config, [_chunk(0, utterances)], '{"ids":[0]}')
    response = asyncio.run(engine.chat("전체 검증이 끝난 건지, 담당을 구분해 알려 주세요."))
    assert response.references
    assert "근거를 확정하지 못했습니다" in response.answer
    assert "검증이 미완료" not in response.answer


def test_speech_act_payload_keeps_char_caps_and_ack_not_selectable() -> None:
    focus = QuestionFocus("종료 범위", ("종료",))
    pool = [candidate(0, "종료해 주세요."), candidate(1, "네 알겠습니다.", context_only=True)]
    raw, packed, shown = focused_selector_payload("종료 범위는?", focus, pool, 3, 10000)
    assert [c.id for c in packed] == [0]
    assert {c.id for c in shown} == {0, 1}
    assert select_ids('{"ids":[1]}', packed, 3, 1000) is None
    large = [candidate(0, "종료하고 " + "전체 원문 " * 200)]
    raw, packed, shown = focused_selector_payload("종료 범위는?", focus, large, 3, 200)
    assert len(raw) <= 200 and not packed and not shown


def test_verification_guidance_is_not_applied_to_general_completion_or_quoted_project_name() -> (
    None
):
    general = QuestionFocus("조치 완료 여부", ("완료",))
    verification = QuestionFocus("전체 검증까지 끝난 건지", ("검증",))
    named = QuestionFocus("'전체검증' 프로젝트 담당은?", ("담당",))
    assert focus_selector_prompt(general) == FOCUSED_SELECTOR_PROMPT
    assert focus_selector_prompt(named) == FOCUSED_SELECTOR_PROMPT
    assert focus_selector_prompt(verification) == VERIFICATION_SELECTOR_PROMPT
    assert "검증이라는 단어의 유무만으로" not in FOCUSED_SELECTOR_PROMPT
    assert "검증이라는 단어의 유무만으로" in VERIFICATION_SELECTOR_PROMPT


def test_merged_closing_and_verification_focus_preserves_both_act_types() -> None:
    focus = QuestionFocus("종료 범위 / 전체 검증까지 끝난 건지", ("종료", "검증"))
    pool = [
        candidate(0, "이 건은 닫겠습니다."),
        candidate(1, "앞으로 문제는 없을 것으로 예상합니다."),
        candidate(2, "아직 전체 검증을 못했습니다."),
        candidate(3, "현재 상태가 좋아 보입니다."),
    ]
    eligible, fallback = speech_act_candidates("종료 범위와 전체 검증 여부는?", focus, pool)
    assert {c.id for c in eligible} == {0, 1, 2} and not fallback
    assert focus_selector_prompt(focus) == VERIFICATION_SELECTOR_PROMPT


@pytest.mark.parametrize("mixed_verification", [False, True])
def test_model_system_and_input_budget_follow_actual_focus_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mixed_verification: bool
) -> None:
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    utterances = [
        _utterance("A", "전체 검증을 완료했습니다.", 0),
        _utterance("B", "내일 추가로 확인하겠습니다.", 10),
    ]
    _write(config, utterances)
    caps = []
    unit_caps = []

    def capture(
        query: str,
        focus: QuestionFocus,
        candidates: list[SourceCandidate],
        max_items: int,
        max_chars: int,
        *,
        previous_user_question: str | None = None,
        max_units: int | None = None,
    ) -> tuple[str, list[SourceCandidate], list[SourceCandidate]]:
        caps.append(max_chars)
        unit_caps.append(max_units)
        return focused_selector_payload(
            query,
            focus,
            candidates,
            max_items,
            max_chars,
            previous_user_question=previous_user_question,
            max_units=max_units,
        )

    monkeypatch.setattr("search.chat.focused_selector_payload", capture)
    engine, backend = _engine(config, [_chunk(0, utterances)], '{"ids":[0]}')
    query = (
        "전체 검증까지 완료된 건지, 앞으로 할 일을 알려 주세요."
        if mixed_verification
        else "원인 분석이 완료된 건지, 앞으로 할 일을 알려 주세요."
    )
    asyncio.run(engine.chat(query))
    systems = [call.kwargs["messages"][0]["content"] for call in backend.chat.call_args_list]
    assert systems.count(VERIFICATION_SELECTOR_PROMPT) == int(mixed_verification)
    assert all(
        system in {FOCUSED_SELECTOR_PROMPT, VERIFICATION_SELECTOR_PROMPT} for system in systems
    )
    expected = int(
        (
            config.llm.max_context_tokens
            - config.llm.chat_max_tokens
            - max(_estimate_korean_tokens(system) for system in systems)
        )
        * 1.5
    )
    assert caps and set(caps) == {expected}
    assert set(unit_caps) == {
        config.chat.source_answer_max_items * config.chat.source_focus_max_items
    }
    assert expected == int(
        (
            config.llm.max_context_tokens
            - config.llm.chat_max_tokens
            - _estimate_korean_tokens(
                VERIFICATION_SELECTOR_PROMPT if mixed_verification else FOCUSED_SELECTOR_PROMPT
            )
        )
        * 1.5
    )


def test_nominalized_coordination_keeps_decision_and_sharing_as_distinct_focuses() -> None:
    query = (
        "업무를 진행할 때 직접 결정하는 것과 비용이 큰 이슈의 공유를 어떻게 구분했나요? "
        "배포 조건과, 기다릴 시간이 구체적으로 정해졌는지도 알려 주세요."
    )
    focuses = question_focuses(question_items(query), 4)
    assert len(focuses) == 4
    assert focuses[0].text.endswith("직접 결정하는 것")
    assert focuses[1].text.startswith("비용이 큰 이슈의 공유")
    assert "어떻게 구분했나요?" in focuses[0].scope_question
    assert focuses[-1].checks == ("시간",)


@pytest.mark.parametrize(
    "query",
    [
        "점유시간 데이터를 누가 확인했나요?",
        "응답 시간 기록을 누가 공유했나요?",
        "'시간이 구체적으로 정해졌는지' 문서의 담당은 누구인가요?",
    ],
)
def test_time_topic_noun_alone_does_not_create_temporal_question_axis(query: str) -> None:
    assert all(item.check not in {"시간", "기간"} for item in question_items(query))


def test_time_focus_prioritizes_actual_conditional_time_over_generic_action_noun() -> None:
    focus = QuestionFocus("설계 검토 시점이 확정됐나요?", ("시점", "확정"))
    pool = [
        candidate(0, "설계 검토를 하자고 했습니다."),
        candidate(1, "설계 검토는 가능하면 오늘도 좋지만"),
        candidate(2, "아니면 내일 오전에 설계 검토를 진행하면 좋겠습니다."),
    ]
    ranked = rank_focus_candidates(focus.text, focus, pool)
    assert ranked[0].id in {1, 2}
    ids, limited = allocate_focus_ids(
        [[0, 2]], pool, pool, 2, 10000, query=focus.text, focuses=[focus]
    )
    assert ids == [2] and limited
    assert [c.id for c in selected_with_replies(ids, pool, 2, context_candidates=pool)] == [1, 2]


def test_allocator_replaces_expensive_first_choice_to_preserve_later_distinct_focus() -> None:
    pool = [
        candidate(0, "일부 조건은 남아 있지만"),
        candidate(1, "첫 번째 요청은 확인했습니다."),
        candidate(2, "첫 번째 요청은 별도로 기록했습니다.", "B"),
        candidate(3, "두 번째 요청도 확인했습니다.", "C"),
        candidate(4, "마지막 일정은 내일입니다.", "D"),
    ]
    ids, limited = allocate_focus_ids([[1, 2], [3], [4]], pool, pool, 3, 10000)
    assert ids == [2, 3, 4] and limited
    assert {c.id for c in selected_with_replies(ids, pool, 3, context_candidates=pool)} == {
        2,
        3,
        4,
    }


def test_allocator_reuses_shared_model_context_and_preserves_escaped_global_cap() -> None:
    pool = [
        candidate(0, "[필수 조건]이 남아 있지만"),
        candidate(1, "검토해서 공유하겠습니다."),
        candidate(2, "별도 일정은 내일입니다.", "B"),
    ]
    all_quotes = selected_with_replies([0, 1, 2], pool, 3, context_candidates=pool)
    cap = len(render_source_answer(all_quotes, limited=True))
    ids, limited = allocate_focus_ids([[0], [1], [2]], pool, pool, 3, cap)
    assert set(ids) == {0, 1, 2} and not limited
    assert len(render_source_answer(all_quotes, limited=True)) <= cap
    ids, limited = allocate_focus_ids([[0], [1], [2]], pool, pool, 3, cap - 1)
    final = selected_with_replies(ids, pool, 3, context_candidates=pool)
    assert limited and len(render_source_answer(final, limited=True)) <= cap - 1


def test_allocator_many_per_focus_choices_have_a_bounded_number_of_context_evaluations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = [candidate(i, f"독립 근거 {i}입니다.", f"A{i}") for i in range(64)]
    selections = [list(range(index * 16, (index + 1) * 16)) for index in range(4)]
    calls = 0
    original = selected_with_replies

    def counted(*args: object, **kwargs: object) -> list[SourceCandidate]:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("search.source_answers.selected_with_replies", counted)
    ids, limited = allocate_focus_ids(selections, pool, pool, 8, 10000)
    assert len(ids) == 8 and limited
    assert all(set(ids) & set(row) for row in selections)
    assert calls <= 8 * len(selections) * sum(map(len, selections)) + len(pool)


def test_physical_close_time_does_not_narrow_delivery_or_followup_evidence() -> None:
    focus = QuestionFocus("닫기 직전의 기록 전달이 항상 보장되는지", ())
    pool = [
        candidate(0, "종료하기 바로 직전에"),
        candidate(1, "이벤트로 정보를 전달할 수 있는데"),
        candidate(2, "항상 전달된다고 확신할 수는 없습니다."),
        candidate(3, "다음에 누락 여부를 확인하겠습니다.", "B"),
    ]
    assert speech_act_candidates(focus.text, focus, pool) == (pool, False)
    raw, packed, shown = focused_selector_payload(focus.text, focus, pool, 3, 10000)
    assert {c.id for c in packed} == {0, 3}
    assert {c.id for c in shown} == {0, 1, 2, 3}
    assert len(raw) <= 10000
    for anchor in (0, 2):
        assert [
            c.id for c in selected_with_replies([anchor], pool, 4, context_candidates=pool)
        ] == [0, 1, 2]
    changed_actor = [pool[0], replace(pool[1], quote=replace(pool[1].quote, speaker="C"))]
    assert len(selected_with_replies([0], changed_actor, 4, context_candidates=changed_actor)) == 1


def test_need_and_method_guidance_is_focus_scoped_and_composes_with_verification() -> None:
    ordinary = QuestionFocus("현재 적용 상태", ("상태",), "현재 상태와 필요한 처리 방식은?")
    need = QuestionFocus("필요한 처리 방식", ("방식",), "현재 상태와 필요한 처리 방식은?")
    both = QuestionFocus("필요한 검증 방식과 완료 여부", ("방식", "검증", "완료"))
    assert focus_selector_prompt(ordinary) == FOCUSED_SELECTOR_PROMPT
    assert focus_selector_prompt(need).startswith(FOCUSED_SELECTOR_PROMPT)
    assert "필요 여부·그 근거" in focus_selector_prompt(need)
    assert focus_selector_prompt(both).startswith(VERIFICATION_SELECTOR_PROMPT)
    assert "필요 여부·그 근거" in focus_selector_prompt(both)
    named = QuestionFocus("'필요한 처리 방식' 프로젝트 담당은 누구인가요?", ("담당",))
    assert focus_selector_prompt(named) == FOCUSED_SELECTOR_PROMPT
    sharing = QuestionFocus(
        "비용이 많이 필요한 이슈의 공유를 어떻게 구분했나요?", ("공유", "어떻게")
    )
    assert focus_selector_prompt(sharing) == FOCUSED_SELECTOR_PROMPT


@pytest.mark.parametrize("boundary", ["same_actor", "other_actor", "unknown", "gap", "topic"])
def test_need_method_pack_priority_links_only_actual_contiguous_same_actor_reason(
    boundary: str,
) -> None:
    focus = QuestionFocus("필요한 압축 방식", ("방식",))
    pool = [
        candidate(0, "압축에 대해 살펴보고 있습니다.", "C"),
        candidate(1, "압축은 필요할 것 같습니다.", "A"),
        candidate(2, "이게 현재 규정이 그렇게 되어 있다 보니까", "A"),
    ]
    if boundary == "other_actor":
        pool[2] = replace(pool[2], quote=replace(pool[2].quote, speaker="B"))
    elif boundary == "unknown":
        pool[1:] = [replace(c, quote=replace(c.quote, speaker="UNKNOWN")) for c in pool[1:]]
    elif boundary == "gap":
        pool[2] = replace(pool[2], source_index=7)
    elif boundary == "topic":
        pool[2] = replace(
            pool[2], quote=replace(pool[2].quote, text="이게 다른 주제의 규정이다 보니까")
        )
    ranking = [c.id for c in rank_focus_candidates(focus.text, focus, pool)]
    assert (ranking.index(2) < ranking.index(0)) == (boundary == "same_actor")
    # 순위는 선택을 만들지 않으며 엄격한 후치 이유절은 실제 연결 문맥으로 남는다.
    assert [c.id for c in selected_with_replies([1], pool, 3, context_candidates=pool)] == (
        [1, 2] if boundary == "same_actor" else [1]
    )
    general = QuestionFocus("압축 현황", ("현황",))
    general_ranking = [c.id for c in rank_focus_candidates(general.text, general, pool)]
    assert general_ranking.index(0) < general_ranking.index(2)


@pytest.mark.parametrize("relative", ["검토자가 작성하신", "담당자가 쓰신", "팀장이 주신"])
def test_finite_relative_verb_keeps_contiguous_actual_predicate_and_atomic_budget(
    relative: str,
) -> None:
    pool = [
        candidate(0, relative),
        candidate(1, "자료를 검토해서"),
        candidate(2, "다음에 공유하겠습니다."),
    ]
    for selected in (0, 2):
        assert [
            c.id for c in selected_with_replies([selected], pool, 3, context_candidates=pool)
        ] == [0, 1, 2]
    ids, limited = allocate_focus_ids([[0]], pool, pool, 2, 10000)
    assert ids == [] and limited
    for actor in ("B", "UNKNOWN", ""):
        changed = [replace(c, quote=replace(c.quote, speaker=actor)) for c in pool]
        if actor == "B":
            changed[1] = pool[1]
        assert len(selected_with_replies([0], changed, 3, context_candidates=changed)) == 1
    gap = [pool[0], replace(pool[1], source_index=4), replace(pool[2], source_index=5)]
    assert len(selected_with_replies([0], gap, 3, context_candidates=gap)) == 1


@pytest.mark.parametrize(
    "name", ["담당자가 김유신", "참석자는 이순신", "팀 이름은 혁신", "담당자가 이하신"]
)
def test_noun_or_name_ending_in_sin_is_not_a_relative_verb(name: str) -> None:
    pool = [candidate(0, name), candidate(1, "다른 이야기를 하겠습니다.")]
    assert [c.id for c in selected_with_replies([0], pool, 3, context_candidates=pool)] == [0]


def test_allocator_keeps_selected_subject_and_time_over_unrelated_introduction() -> None:
    pool = [
        candidate(0, "예를 들어서 설명하면"),
        candidate(1, "틀릴 수도 있지만"),
        candidate(2, "먼저 들어봐 주세요."),
        candidate(3, "문제를 쪼개 주세요.", "B"),
        candidate(4, "비율은 수치 예시가 있고", "B"),
        candidate(5, "담당자가 작성하신", "B"),
        candidate(6, "자료로 나눠서 검토해서", "B"),
        candidate(7, "회의를 하자고 했습니다.", "B"),
        candidate(8, "가능하면 오늘이 좋지만", "B"),
        candidate(9, "아니면 내일 오전에 회의를 하면 좋겠습니다.", "B"),
    ]
    focuses = [
        QuestionFocus("문제를 쪼개는 방법", ("방법",)),
        QuestionFocus("비율이 확정인가요?", ("확정",)),
        QuestionFocus("자료 요청", ("요청",)),
        QuestionFocus("회의 시점", ("시점",)),
    ]
    ids, limited = allocate_focus_ids(
        [[3], [4], [2], [7, 9]], pool, pool, 7, 10000, focuses=focuses
    )
    visible = selected_with_replies(ids, pool, 7, context_candidates=pool)
    assert {c.id for c in visible} == {3, 4, 5, 6, 7, 8, 9}
    assert limited and set(ids).issubset({2, 3, 4, 7, 9})


def test_relative_time_without_topic_word_stays_an_eligible_model_choice() -> None:
    pool = [candidate(0, "설계 회의를 하자는 뜻입니다."), candidate(1, "그 전까지 해 주세요.")]
    focus = QuestionFocus("설계 시점은 언제인가요?", ("시점", "언제"))
    ids, limited = allocate_focus_ids([[0, 1]], pool, pool, 1, 10000, focuses=[focus])
    assert ids == [1] and limited


def test_implicit_state_card_hint_uses_only_shared_actual_question_subject() -> None:
    query = "압축 작업을 설명해 주세요. 현재 적용 상태와 필요한 압축 방식은?"
    focus = QuestionFocus("현재 적용 상태", ("상태",), "현재 적용 상태와 필요한 압축 방식은?")
    assert _implicit_state_subjects(query, focus) == {"압축"}
    pool = [
        candidate(0, "지금 적용 여부는 검토 중입니다."),
        candidate(1, "현재 압축은 하지 않고 있습니다."),
    ]
    assert rank_focus_candidates(query, focus, pool)[0].id == 0
    raw, _, _ = focused_selector_payload(query, focus, pool, 3, 10000)
    assert json.loads(raw)["evidence_units"][0]["id"] == 1


@pytest.mark.parametrize(
    "own",
    ["베타 적용 상태", "적용범위 상태", "적용성 상태", "적용한계 상태", "'베타' 현재 적용 상태"],
)
def test_implicit_state_hint_does_not_replace_own_entity_or_real_compound_noun(own: str) -> None:
    query = "알파 압축을 설명해 주세요. 현재 적용 상태와 필요한 압축 방식은?"
    focus = QuestionFocus(own, ("상태",), "현재 적용 상태와 필요한 압축 방식은?")
    assert not _implicit_state_subjects(query, focus)


@pytest.mark.parametrize(
    ("query", "scope"),
    [
        ("알파 압축을 설명해 주세요.", "현재 적용 상태와 저장 방식은?"),
        ("'압축' 문서를 설명해 주세요.", "현재 적용 상태와 '압축' 방식은?"),
        ("현재 적용 상태는?", "현재 적용 상태는?"),
    ],
)
def test_implicit_state_hint_requires_unquoted_topic_in_both_original_clauses(
    query: str, scope: str
) -> None:
    assert not _implicit_state_subjects(query, QuestionFocus("현재 적용 상태", ("상태",), scope))


def test_verification_limit_evidence_guidance_remains_verification_only() -> None:
    assert "확인 범위를 제한하는 근거도 선택" not in FOCUSED_SELECTOR_PROMPT
    assert "확인 범위를 제한하는 근거도 선택" in VERIFICATION_SELECTOR_PROMPT
    assert (
        focus_selector_prompt(QuestionFocus("원인 분석 완료 여부", ("원인",)))
        == FOCUSED_SELECTOR_PROMPT
    )


@pytest.mark.parametrize("selected", [0, 1])
def test_postposed_causal_fragment_is_mandatory_bidirectional_actual_context(
    selected: int,
) -> None:
    pool = [
        candidate(0, "압축 방식의 변경은 필요할 것 같습니다."),
        candidate(1, "이게 검토 기준이 그렇게 되어 있다 보니까"),
    ]
    assert [c.id for c in selected_with_replies([selected], pool, 2, context_candidates=pool)] == [
        0,
        1,
    ]
    assert selected_with_replies([selected], pool, 1, context_candidates=pool) == []
    ids, limited = allocate_focus_ids([[selected]], pool, pool, 1, 10000)
    assert ids == [] and limited
    ids, limited = allocate_focus_ids([[selected]], pool, pool, 2, 10000)
    assert ids == [selected] and not limited
    assert [
        c.id
        for c in selected_with_replies(
            [selected],
            pool,
            2,
            context_candidates=pool,
            include_replies=False,
        )
    ] == [selected]
    complete = selected_with_replies([selected], pool, 2, context_candidates=pool)
    cap = len(render_source_answer([pool[selected]], limited=True))
    kept, limited = bound_source_answer(complete, cap, limited=True)
    assert kept == [] and limited


def test_explicit_terminal_weekday_time_axis_uses_actual_time_priority() -> None:
    query = "데이터 입력과 검토 회의의 요일·시간, 결정 사항을 알려 주세요."
    focus = question_focuses(question_items(query), 4)[0]
    assert focus.checks == ("요일·시간",)
    pool = [
        candidate(0, "데이터 입력과 검토 회의입니다."),
        candidate(1, "화요일에 오후 3시로 하겠습니다."),
    ]
    assert rank_focus_candidates(query, focus, pool)[0].id == 1
    for text in ("'요일·시간' 메뉴를 확인해 주세요.", "요일·시간 화면에서 정리해 주세요."):
        assert not any("시간" in item.check for item in question_items(text))


@pytest.mark.parametrize("fragment", ["조건이 맞으면 ZX를", "예시 B/C를", "기본값은 R2를"])
def test_allocator_delays_open_alphanumeric_object_without_rejecting_nominal_names(
    fragment: str,
) -> None:
    assert _priority_fragment(fragment)
    assert not _priority_fragment("선택값은 ZX")
    assert not _priority_fragment("ZX를 보여 줍니다.")
    pool = [candidate(0, fragment), candidate(1, "조건이 맞으면 자료를 보여 줍니다.")]
    focus = QuestionFocus("조건에 맞을 때 방식", ("방식",))
    ids, limited = allocate_focus_ids([[0, 1]], pool, pool, 1, 10000, focuses=[focus])
    assert ids == [1] and limited
    ids, limited = allocate_focus_ids([[0]], pool, pool, 1, 10000, focuses=[focus])
    assert ids == [0] and not limited


@pytest.mark.parametrize(
    "variant",
    [
        "other_actor",
        "unknown",
        "gap",
        "transition",
        "question",
        "request",
        "polite_request",
        "proposal",
        "formal_request",
        "indirect_request",
        "formal_wish",
        "complete_reason",
        "no_deictic",
        "ambiguous_source",
    ],
)
def test_postposed_reason_does_not_bind_unrelated_or_independent_utterances(variant: str) -> None:
    pool = [candidate(0, "압축은 필요합니다."), candidate(1, "이게 검토 기준이 그렇다 보니까")]
    if variant == "other_actor":
        pool[1] = replace(pool[1], quote=replace(pool[1].quote, speaker="B"))
    elif variant == "unknown":
        pool = [replace(c, quote=replace(c.quote, speaker=" UNKNOWN ")) for c in pool]
    elif variant == "gap":
        pool[1] = replace(pool[1], source_index=3)
    elif variant == "transition":
        pool[1] = replace(
            pool[1], quote=replace(pool[1].quote, text="이게 다른 안건의 기준이다 보니까")
        )
    elif variant == "question":
        pool[0] = replace(pool[0], quote=replace(pool[0].quote, text="압축은 필요한가요?"))
    elif variant == "request":
        pool[0] = replace(pool[0], quote=replace(pool[0].quote, text="압축해 주세요."))
    elif variant == "polite_request":
        pool[0] = replace(
            pool[0], quote=replace(pool[0].quote, text="압축해 주시면 좋을 것 같습니다.")
        )
    elif variant == "proposal":
        pool[0] = replace(pool[0], quote=replace(pool[0].quote, text="자료를 공유합시다."))
    elif variant == "formal_request":
        pool[0] = replace(
            pool[0], quote=replace(pool[0].quote, text="자료를 보내주시기 바랍니다.")
        )
    elif variant == "indirect_request":
        pool[0] = replace(
            pool[0], quote=replace(pool[0].quote, text="자료 공유를 요청드리고자 합니다.")
        )
    elif variant == "formal_wish":
        pool[0] = replace(pool[0], quote=replace(pool[0].quote, text="자료 공유를 요망합니다."))
    elif variant == "complete_reason":
        pool[1] = replace(
            pool[1], quote=replace(pool[1].quote, text="이게 검토 기준에 맞는 이유입니다.")
        )
    elif variant == "no_deictic":
        pool[1] = replace(pool[1], quote=replace(pool[1].quote, text="검토 기준이 그렇다 보니까"))
    elif variant == "ambiguous_source":
        pool.append(replace(pool[1], id=2))
    selected = selected_with_replies([0], pool, 5, context_candidates=pool)
    if variant != "ambiguous_source":
        assert not _postposed_reason_pair(pool[0], pool[1])
    # 질문·요청의 기존 응답 문맥은 유지하지만 새 이유절 결합으로 추가하지는 않는다.
    if variant in {"question", "request"}:
        assert selected
    else:
        assert [c.id for c in selected] == [0]
