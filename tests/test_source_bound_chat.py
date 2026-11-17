"""실제 원문 번호 선택·인용 결합·문맥 보충의 제품 경계를 합성 자료로 검증한다."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from api.routers.search_chat import _build_chat_references
from config import AppConfig, ChatConfig, PathsConfig
from core.chat_scope import ChatSourceScope
from search.chat import ChatEngine
from search.hybrid_search import SearchResponse, SearchResult
from search.source_answers import (
    bound_source_answer,
    build_source_candidates,
    candidate_context_costs,
    candidate_context_members,
    coverage_payload,
    facet_refinement_payload,
    followup_user_hint,
    literal_eligible_candidates,
    publish_uptake_context,
    question_items,
    render_source_answer,
    requested_items,
    select_facet_plan,
    select_ids,
    selected_with_replies,
    selector_payload,
    source_bound_question,
    uptake_context_members,
)
from search.source_recall import supplement_results
from steps.embedder import _ensure_fts_table


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    """기존 두 단계 선택 계약을 별도로 검증하는 합성 설정을 만든다."""
    result = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    result.chat.source_focus_max_calls = 1
    return result


def _write(
    config: AppConfig, utterances: list[dict[str, Any]], *, revision: str | None = None
) -> Path:
    """원문 업무 데이터 대신 합성 대화만 안전한 시험 경로에 둔다."""
    cp = config.paths.resolved_checkpoints_dir / "sample"
    cp.mkdir(parents=True, exist_ok=True)
    path = cp / "correct.json"
    payload: dict[str, Any] = {"utterances": utterances}
    if revision:
        payload["search_revision"] = revision
    path.write_text(json.dumps(payload, ensure_ascii=False))
    return path


def _utterance(speaker: str, text: str, at: int) -> dict[str, Any]:
    """서로 다른 실제 시각을 가진 발화를 만든다."""
    return dict(speaker=speaker, text=text, start=float(at), end=float(at + 1))


def _chunk(index: int, utterances: list[dict[str, Any]], *, mid: str = "sample") -> SearchResult:
    """각 parent의 raw 본문·화자·시간을 함께 보존한다."""
    return SearchResult(
        f"{mid}_chunk_{index}",
        "\n".join(f"[{u['speaker']}] {u['text']}" for u in utterances),
        0.1,
        mid,
        "2026-01-01",
        list(dict.fromkeys(u["speaker"] for u in utterances)),
        utterances[0]["start"],
        utterances[-1]["end"],
        index,
    )


def _engine(
    config: AppConfig, results: list[SearchResult], answer: Any, *, facet_fixture: bool = True
) -> tuple[ChatEngine, MagicMock]:
    """백엔드는 번호 JSON만 돌려주며 실제 inference나 외부 API를 실행하지 않는다."""
    backend = MagicMock()

    def fixture_reply(**kwargs: Any) -> str:
        raw = answer(**kwargs) if callable(answer) else answer
        # 기존 경계 회귀의 정수 선택 대역만 새 질문항목 스키마로 표현한다.
        # 형식·필드·타입 오류는 그대로 두며, 새 엄격 스키마 회귀는 이 변환을 끈다.
        if (
            facet_fixture
            and isinstance(raw, str)
            and re.fullmatch(r'\{\s*"ids"\s*:\s*\[\s*(?:\d+\s*(?:,\s*\d+\s*)*)?\]\s*\}', raw)
        ):
            try:
                payload = json.loads(kwargs["messages"][-1]["content"])
            except (ValueError, TypeError):
                return raw
            if isinstance(payload, dict) and payload.get("question_items"):
                return json.dumps(
                    {"facets": {key: json.loads(raw)["ids"] for key in payload["question_items"]}}
                )
        return raw

    backend.chat.side_effect = fixture_reply
    context = AsyncMock()
    context.__aenter__.return_value = backend
    context.__aexit__.return_value = False
    manager = MagicMock()
    manager.acquire.return_value = context
    search = MagicMock()
    search.search = AsyncMock(return_value=SearchResponse(results, "합성 질문", len(results)))
    engine = ChatEngine(config, manager, search)
    engine._get_system_prompt = lambda: "사용자 지정 말투·형식"  # type: ignore[method-assign]
    return engine, backend


def _model_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """대역의 선택만 각 bundle 별칭의 자기 원문으로 연결하며 실제 출력 검증은 바꾸지 않는다."""
    if "source_turns" not in payload:
        return payload["candidates"]
    costs = {i: b["cost"] for b in payload["candidates"] for i in b["selectable_ids"]}
    return [
        {"id": t["id"], "text": t["text"], "cost": costs.get(t["id"], 1)}
        for t in payload["source_turns"]
        if "id" in t
    ]


def _uptake_dialogue() -> list[dict[str, Any]]:
    """실제 사례의 인명·주제 대신 새 담당자와 게시판을 쓰는 연속 대화를 만든다."""
    return [
        _utterance("S1", "새 담당자에게도 안내하나요?", 10),
        _utterance("S1", "운영 담당자 말씀인가요?", 12),
        _utterance("S2", "아 그분은 게시판으로.", 14),
        _utterance("S1", "그럼 게시판으로 안내해 주시죠.", 16),
        _utterance("S2", "네 알겠습니다.", 18),
    ]


def test_uptake_is_added_only_after_best25_selection_and_must_fit_full_publish_budget(
    config: AppConfig,
) -> None:
    """같은 질문자의 이어받기 요청은 실제 QA·ACK 전체를 하나의 비용과 표시로 보존한다."""
    u = _uptake_dialogue()
    _write(config, u)
    full = build_source_candidates(config, [_chunk(0, u)])
    units = uptake_context_members(full)
    assert units == {3: (0, 1, 2, 3, 4)}
    assert candidate_context_costs(full)[3] == 2
    query = "공유 대상·채널, 후속 안내를 정리해 주세요."
    items = question_items(query)
    contexts = candidate_context_members(full)
    payload, packed = selector_payload(query, full, 8, 5000, items=items, contexts=contexts)
    assert all(c.id in {v.id for v in packed} for c in full if not c.context_only)
    shown = json.loads(payload)["source_turns"]
    assert [t["text"] for t in shown] == [v["text"] for v in u]
    assert all(t.get("id") != 4 for t in shown)
    raw = json.dumps({"facets": {item.key: [3] for item in items}})
    assert select_facet_plan(raw, packed, items, full, 2, 5000) is not None
    base = selected_with_replies([3], packed, 8, context_candidates=full)
    assert [c.id for c in base] == [3, 4]
    kept, limited, atomic = publish_uptake_context(base, [3], full, 4, 5000, enabled=True)
    assert kept == [] and limited and not atomic
    kept, limited, atomic = publish_uptake_context(base, [3], full, 5, 5000, enabled=True)
    assert [c.id for c in kept] == list(range(5)) and not limited and atomic == units
    assert publish_uptake_context(base, [], full, 5, 5000, enabled=True) == (base, False, {})
    assert publish_uptake_context(
        base, [3], full, 5, 5000, enabled=True, include_replies=False
    ) == (base, False, {})
    unselected_request = selected_with_replies([1], full, 8)
    assert 3 in {c.id for c in unselected_request}
    assert publish_uptake_context(unselected_request, [1], full, 8, 5000, enabled=True) == (
        unselected_request,
        False,
        {},
    )


@pytest.mark.parametrize(
    "change",
    [
        "no_prefix",
        "no_overlap",
        "same_reply_actor",
        "other_requester",
        "unknown",
        "gap",
        "other_meeting",
        "generic_only",
        "topic",
        "other_topic",
        "separate",
        "negation",
        "fully_specified_reply",
        "question_transition",
        "negative_reply",
        "ambiguous_reply_piece",
    ],
)
def test_uptake_does_not_join_unproved_actor_scope_topic_or_generic_overlap(
    config: AppConfig, change: str
) -> None:
    """연속성·원질문자·내용어·긍정적인 이어받기 증거가 없으면 새 문맥을 붙이지 않는다."""
    from dataclasses import replace

    u = _uptake_dialogue()
    if change == "no_prefix":
        u[3]["text"] = "게시판으로 안내해 주시죠."
    if change == "no_overlap":
        u[3]["text"] = "그럼 다른 저장소로 안내해 주시죠."
    if change == "same_reply_actor":
        u[2]["speaker"] = "S1"
    if change == "other_requester":
        u[3]["speaker"] = "S3"
    if change == "unknown":
        for index in (0, 1, 3):
            u[index]["speaker"] = "UNKNOWN"
    if change == "generic_only":
        u[2]["text"], u[3]["text"] = "아 그분 확인 요청.", "그럼 그분 확인 요청해 주세요."
    if change == "topic":
        u[3]["text"] = "그럼 다음 주제는 게시판으로 안내해 주세요."
    if change == "other_topic":
        u[3]["text"] = "그럼 다른 안건은 게시판으로 안내해 주세요."
    if change == "separate":
        u[3]["text"] = "그럼 별개 안건은 게시판으로 안내해 주세요."
    if change == "negation":
        u[3]["text"] = "그럼 그건 아니고 게시판으로 안내해 주세요."
    if change == "fully_specified_reply":
        u[2]["text"] = "네, 게시판으로 보내 드리겠습니다."
    if change == "question_transition":
        u[0]["text"], u[1]["text"] = (
            "이전 보고서는 어디로 보내나요?",
            "다음 안건으로, 관리자에게 게시판으로 보내나요?",
        )
    if change == "negative_reply":
        u[2]["text"] = "아 그분은 게시판으로 안 됩니다."
    _write(config, u)
    full = build_source_candidates(config, [_chunk(0, u)])
    if change == "gap":
        full[3] = replace(full[3], source_index=7)
    if change == "other_meeting":
        full[2] = replace(full[2], meeting_id="different")
    if change == "ambiguous_reply_piece":
        full.append(replace(full[2], id=99))
    assert uptake_context_members(full) == {}


@pytest.mark.parametrize("reply", ["아 그분은 게시판으로.", "거기 게시판으로."])
def test_future_week_is_not_a_topic_transition(config: AppConfig, reply: str) -> None:
    """다음 주라는 일정 수식은 실제 다음 안건·주제 전환과 구분한다."""
    u = _uptake_dialogue()
    u[2]["text"] = reply
    u[3]["text"] = "그러면 다음 주에 게시판으로 안내해 주세요."
    _write(config, u)
    full = build_source_candidates(config, [_chunk(0, u)])
    assert uptake_context_members(full) == {3: (0, 1, 2, 3, 4)}


async def test_literal_with_share_and_confirm_words_does_not_enable_uptake(
    config: AppConfig,
) -> None:
    """literal의 reply_context가 참이어도 새 compound QA join은 활성화하지 않는다."""
    u = _uptake_dialogue()
    _write(config, u)
    engine, backend = _engine(config, [_chunk(0, u)], '{"ids":[3]}')
    result = await engine.chat("공유하기로 확정된 원문 한 문장을 그대로 인용해 주세요.")
    assert backend.chat.call_count == 1
    assert [r.quote.text for r in result.references if r.quote] == [u[3]["text"], u[4]["text"]]


def test_uptake_survives_prebound_then_is_whole_or_absent_after_escaped_render_cap(
    config: AppConfig,
) -> None:
    """새 unit은 pre-bound에서 일부만 추가하지 않고 실제 escape 길이에서도 전체만 남긴다."""
    u = _uptake_dialogue()
    u[0]["text"] = "<script>[참조 99]* " * 20 + u[0]["text"]
    u.append(_utterance("S3", "독립 검토는 내일 합니다.", 20))
    _write(config, u)
    full = build_source_candidates(config, [_chunk(0, u)])
    # ACK는 마지막 normal ID 뒤에 붙으므로 실제 source_index로 번호를 찾는다.
    request = next(c.id for c in full if c.source_index == 3)
    other = next(c.id for c in full if c.source_index == 5)
    selected = selected_with_replies([request, other], full, 8, max_chars=50)
    kept, limited, atomic = publish_uptake_context(
        selected, [request, other], full, 8, 650, enabled=True
    )
    kept, limited = bound_source_answer(kept, 650, limited=limited, atomic_units=atomic)
    assert limited and [c.id for c in kept] == [other]
    answer = render_source_answer(kept, limited=limited)
    assert len(answer) <= 650 and "[참조 1]" in answer and "[참조 2]" not in answer
    assert kept[0].quote == next(c.quote for c in full if c.id == other)


@pytest.mark.parametrize("mutation_at", [1, 2])
async def test_uptake_previous_question_edit_aborts_without_history(
    config: AppConfig, mutation_at: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """모델에 보이지 않은 추가 Q도 initial pool에 결합해 새 문장과 옛 선택을 섞지 않는다."""
    u = _uptake_dialogue()
    path = _write(config, u)
    calls = 0
    original_pack = selector_payload

    def pack_request_only(
        query: str, candidates: list[Any], max_items: int, max_chars: int, **kwargs: Any
    ) -> Any:
        visible = [c for c in candidates if c.source_index in {3, 4}]
        return original_pack(query, visible, max_items, max_chars, **kwargs)

    monkeypatch.setattr("search.chat.selector_payload", pack_request_only)

    def choose(**kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        payload = json.loads(kwargs["messages"][-1]["content"])
        assert all(t["text"] != u[0]["text"] for t in payload["source_turns"])
        if calls == mutation_at:
            changed = [dict(row) for row in u]
            changed[0]["text"] = "다른 대상에 대한 질문인가요?"
            path.write_text(json.dumps({"utterances": changed}))
        return '{"ids":[3]}'

    engine, backend = _engine(config, [_chunk(0, u)], choose)
    result = await engine.chat(
        "공유 대상·채널, 후속 안내를 정리해 주세요.", session_id="uptake-edit"
    )
    assert backend.chat.call_count == 2
    assert (
        not result.references
        and not result.llm_used
        and not engine.get_session("uptake-edit").history
    )


def test_shared_active_uptake_groups_are_whole_or_excluded(config: AppConfig) -> None:
    """같은 원문을 공유하는 활성 요청 둘은 함께 추가하거나 함께 제외한다."""
    u = [
        _utterance("S1", "운영 담당자도 안내하나요?", 10),
        _utterance("S2", "아 그분은 게시판으로.", 12),
        _utterance("S1", "그럼 게시판으로 안내해 주시죠?", 14),
        _utterance("S2", "아 그쪽도 게시판으로.", 16),
        _utterance("S1", "그럼 게시판으로 안내해 주세요.", 18),
        _utterance("S2", "네 알겠습니다.", 20),
    ]
    _write(config, u)
    full = build_source_candidates(config, [_chunk(0, u)])
    base = selected_with_replies([2, 4], full, 8)
    assert {c.source_index for c in base} == {2, 3, 4, 5}
    kept, limited, atomic = publish_uptake_context(base, [2, 4], full, 5, 5000, enabled=True)
    assert kept == [] and limited and not atomic
    kept, limited, atomic = publish_uptake_context(base, [2, 4], full, 6, 5000, enabled=True)
    assert [c.source_index for c in kept] == list(range(6)) and not limited
    assert set(atomic) == {2, 4} and atomic[2] == atomic[4]


async def test_context_residue_after_publish_rejection_is_not_model_success(
    config: AppConfig,
) -> None:
    """직접 고른 요청이 제외되고 이전 문맥만 남으면 ids가 있어도 생성 성공으로 세지 않는다."""
    u = _uptake_dialogue()
    u[3]["text"] = "그럼 게시판으로 안내해 주시죠?"
    u.append(_utterance("S3", "다른 자료는 내일 받습니다.", 20))
    _write(config, u)
    config.chat.source_answer_max_items = 5
    engine, backend = _engine(config, [_chunk(0, u)], '{"ids":[3]}')
    result = await engine.chat("공유 대상·채널, 후속 안내를 정리해 주세요.")
    assert backend.chat.call_count == 2 and result.has_context and not result.llm_used
    assert [r.quote.text for r in result.references if r.quote] == [u[5]["text"]]
    assert "근거를 확정하지 못했습니다" in result.answer and "[참조 1]" in result.answer


def test_condition_scope_retains_literal_qualifier_without_independent_answer_slot() -> None:
    """문두 조건은 실제 의문 요구를 수식하고 OR나 명사형 공지 요구를 지우지 않는다."""
    query = "작업 경로가 모호할 때는 어디에 어떻게 제보하고 누가 확인하나요?"
    items = question_items(query)
    assert {i.check for i in items} == {"어디", "어떻게", "누가", "확인"}
    assert all(
        i.focus_text == query and query[i.focus_start : i.focus_end] == query for i in items
    )
    assert {
        i.check for i in question_items("조건이 모호한 경우에는 담당과 보고 방법을 알려 주세요.")
    } == {"담당", "보고", "방법"}
    assert {i.check for i in question_items("공지 대상·채널을 정리해 주세요.")} == {"대상", "채널"}
    assert {i.check for i in question_items("작업 경로가 왜 모호한가요?")} == {"경로", "모호"}
    alternative = "캡처 아니면 번호를 어디에 어떻게 제보하나요?"
    assert all(i.focus_text == alternative for i in question_items(alternative))
    quoted = "'경로가 모호할 때'라는 제목의 대상·채널을 정리해 주세요."
    assert {i.check for i in question_items(quoted)} == {"대상", "채널"}


def test_evidence_bundles_deduplicate_identical_closure_and_show_ack_as_context_only(
    config: AppConfig,
) -> None:
    """같은 실제 종속절은 한 번 표시하고 ACK는 자기 화자의 문맥으로만 모델에 보인다."""
    u = [
        _utterance("S1", "관리 화면은 수정하지 말고 목록에만 표시하시고", 10),
        _utterance("S1", "캡처 또는 항목 번호를 보내주세요.", 12),
        _utterance("S2", "운영 담당자에게 제보하시죠.", 14),
        _utterance("S3", "네 알겠습니다.", 16),
    ]
    _write(config, u)
    full = build_source_candidates(config, [_chunk(0, u)])
    items = question_items("대상·채널, 제보 방법을 정리해 주세요.")
    payload, packed = selector_payload(
        "대상·채널, 제보 방법을 정리해 주세요.", full, 8, 5000, items=items
    )
    data = json.loads(payload)
    turns = {t.get("source_key", f"s{t.get('id')}"): t for t in data["source_turns"]}
    paired = next(b for b in data["candidates"] if b["selectable_ids"] == [0, 1])
    assert paired["cost"] == 2 and [turns[key]["text"] for key in paired["turn_keys"]] == [
        x["text"] for x in u[:2]
    ]
    assert len([b for b in data["candidates"] if b["selectable_ids"] == [0, 1]]) == 1
    ack = next(c for c in full if c.context_only)
    request = next(b for b in data["candidates"] if 2 in b["selectable_ids"])
    assert turns[request["turn_keys"][-1]] == {
        "source_key": f"s{ack.id}",
        "speaker": "S3",
        "text": u[-1]["text"],
    }
    assert ack.id not in {i for b in data["candidates"] for i in b["selectable_ids"]}
    assert all(t.get("id") != ack.id for t in data["source_turns"])
    assert [t["text"] for t in data["source_turns"]] == [v["text"] for v in u]
    assert [t["id"] for t in data["source_turns"] if "id" in t] == [0, 1, 2]
    for selected_id in (0, 1):
        plan = select_facet_plan(
            json.dumps({"facets": {i.key: [selected_id] for i in items}}),
            packed,
            items,
            full,
            8,
            5000,
        )
        assert plan is not None and plan.ids == [selected_id]
    assert (
        select_facet_plan(
            json.dumps({"facets": {i.key: [ack.id] for i in items}}), packed, items, full, 8, 5000
        )
        is None
    )
    assert (
        select_facet_plan(
            json.dumps({"facets": {i.key: ["s0"] for i in items}}), packed, items, full, 8, 5000
        )
        is None
    )


def test_overlapping_or_equal_text_different_source_bundles_stay_distinct(
    config: AppConfig,
) -> None:
    """일부 원문이 겹치거나 글자가 같아도 다른 실제 화자·회의·parent 문맥을 합치지 않는다."""
    from dataclasses import replace

    u = [
        _utterance("S1", "운영 담당자가 받나요?", 10),
        _utterance("S1", "신규 담당자 말인가요?", 12),
        _utterance("S2", "그분에게 전달하겠습니다.", 14),
        _utterance("S2", "다음 회의에 안내하겠습니다.", 16),
    ]
    _write(config, u)
    values = build_source_candidates(config, [_chunk(0, u)])
    extra = replace(values[0], id=10, meeting_id="other", parent_index=1)
    full = [*values, extra]
    query = "공유 대상·방법, 일정은 무엇인가요?"
    payload, packed = selector_payload(query, full, 8, 6000, items=question_items(query))
    groups = json.loads(payload)["candidates"]
    b0 = next(b for b in groups if 0 in b["selectable_ids"])
    b1 = next(b for b in groups if 1 in b["selectable_ids"])
    assert b0 != b1 and set(b0["turn_keys"]) & set(b1["turn_keys"])
    data = json.loads(payload)
    assert len({t.get("source_key", f"s{t.get('id')}") for t in data["source_turns"]}) == len(
        data["source_turns"]
    )
    assert 10 in {t["id"] for t in data["source_turns"] if "id" in t}
    assert all(10 not in b["selectable_ids"] for b in groups)
    assert {c.id for c in packed} == {c.id for c in full}


def test_singleton_source_ids_keep_choices_without_repeating_one_turn_bundles(
    config: AppConfig,
) -> None:
    """자기 문장만 드는 비용행은 생략해도 모든 packed 번호를 직접 읽고 선택할 수 있다."""
    u = [
        _utterance(f"S{i}", f"항목 {i}의 확인은 다음 회의에 합니다.", 10 + i * 2) for i in range(8)
    ]
    _write(config, u)
    full = build_source_candidates(config, [_chunk(0, u)])
    query = "공유 대상·채널, 확인 일정을 알려 주세요."
    items = question_items(query)
    payload, packed = selector_payload(query, full, 8, 3000, items=items, reserve_coverage=True)
    data = json.loads(payload)
    assert data["candidates"] == []
    assert (
        {t["id"] for t in data["source_turns"]} == {c.id for c in full} == {c.id for c in packed}
    )
    raw = json.dumps({"facets": {i.key: [full[-1].id] for i in items}})
    plan = select_facet_plan(raw, packed, items, full, 8, 3000)
    assert plan is not None
    extra, fits = facet_refinement_payload(query, packed, items, plan, {}, full, 8, 3000)
    assert (
        fits and len(extra) <= 3000 and json.loads(extra)["source_turns"] == data["source_turns"]
    )


@pytest.mark.parametrize("mutation_at", [1, 2])
async def test_displayed_but_unselected_ack_change_rejects_bundle_answer_and_history(
    config: AppConfig, mutation_at: int
) -> None:
    """선택 불가 ACK도 입력 원문이므로 첫·보조 선택 중 편집되면 과거 묶음을 저장하지 않는다."""
    u = [
        _utterance("S1", "검토는 다음 주에 합니다.", 10),
        _utterance("S2", "운영 채널로 제보하시죠.", 12),
        _utterance("S3", "네 알겠습니다.", 14),
    ]
    path = _write(config, u)
    calls = 0

    def choose(**kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        payload = json.loads(kwargs["messages"][-1]["content"])
        assert any(t["speaker"] == "S3" for t in payload["source_turns"])
        if calls == mutation_at:
            changed = [dict(row) for row in u]
            changed[2]["speaker"] = "S9"
            path.write_text(json.dumps({"utterances": changed}))
        return '{"ids":[0]}'

    engine, backend = _engine(config, [_chunk(0, u)], choose)
    response = await engine.chat("검토 일정, 공지 채널을 알려 주세요.", session_id="ack-edit")
    assert backend.chat.call_count == mutation_at
    assert (
        not response.references
        and not response.llm_used
        and not engine.get_session("ack-edit").history
    )


def test_question_facets_keep_exact_spans_and_separate_recipient_channel() -> None:
    """결과·성과를 과로 잘라내지 않고 따옴표 안 쉼표와 대상·채널의 별도 요구를 보존한다."""
    query = "결과·성과를 검토하고, '실험, 비교'의 대상·채널과 일정, 이미 한 일과 앞으로 할 일을 구분해 주세요."
    items = question_items(query)
    assert all(query[item.start : item.end] == item.text for item in items)
    assert len({item.key for item in items}) == len(items)
    first = [i for i in items if i.clause_key == "q0"]
    assert len(first) == 1 and first[0].text.startswith("결과·성과")
    target = next(i for i in items if i.check == "대상")
    channel = next(i for i in items if i.check == "채널")
    assert target.key != channel.key and target.clause_key == channel.clause_key
    assert "'실험, 비교'" in target.text


@pytest.mark.parametrize(
    "bad",
    [
        "missing",
        "extra",
        "bool",
        "null",
        "duplicate",
        "unknown",
        "legacy",
        "prose",
        "duplicate_key",
    ],
)
def test_facet_plan_strict_keys_ids_and_no_new_claims(config: AppConfig, bad: str) -> None:
    """모든 제공 항목과 실제 packed 번호만 허용하고 새 설명·상태·부분 JSON을 거부한다."""
    u = [
        _utterance("S1", "결과를 다음 회의에 공유하겠습니다.", 10),
        _utterance("S2", "다른 자료는 검토 중입니다.", 20),
    ]
    _write(config, u)
    values = build_source_candidates(config, [_chunk(0, u)])
    items = question_items("대상·채널, 공유 일정은 무엇인가요?")
    facets = {item.key: [0] for item in items}
    raw = json.dumps({"facets": facets})
    if bad == "missing":
        facets.pop(items[-1].key)
        raw = json.dumps({"facets": facets})
    if bad == "extra":
        facets["invented"] = [0]
        raw = json.dumps({"facets": facets})
    if bad in {"bool", "null", "duplicate", "unknown"}:
        facets[items[0].key] = {
            "bool": [True],
            "null": [None],
            "duplicate": [0, 0],
            "unknown": [99],
        }[bad]
        raw = json.dumps({"facets": facets})
    if bad == "legacy":
        raw = '{"ids":[0]}'
    if bad == "prose":
        raw = json.dumps({"facets": facets, "answer": "다른 담당자가 이미 완료했습니다."})
    if bad == "duplicate_key":
        raw = '{"facets":' + json.dumps(facets) + ',"facets":' + json.dumps(facets) + "}"
    assert select_facet_plan(raw, values, items, values, 8, 5000) is None


def test_local_focus_separates_events_without_splitting_nouns_or_quoted_subjects() -> None:
    """같은 대상 축도 실제 사건별 원문 초점으로 구분하고 결과·성과와 인용 주제를 보존한다."""
    query = "자료 검토 일정과 전달 공지 대상·채널을 정리하고, 이미 한 일과 앞으로 할 일을 구분해 주세요."
    items = question_items(query)
    target = next(i for i in items if i.check == "대상")
    channel = next(i for i in items if i.check == "채널")
    assert target.focus_text == channel.focus_text == "전달 공지 대상·채널"
    assert target.focus_key == channel.focus_key and "자료 검토" not in target.focus_text
    assert next(i for i in items if i.check == "일정").focus_text == "자료 검토 일정"
    assert all(query[i.focus_start : i.focus_end] == i.focus_text for i in items)
    assert (
        next(i for i in items if i.check == "이미").focus_key
        != next(i for i in items if i.check == "앞으로").focus_key
    )
    q = "'향후 완료 대상, 채널'의 결과·성과를 확인하고, 검토 대상과 전달 대상·채널은 누구에게 가나요?"
    values = question_items(q)
    assert not any(i.check in {"향후", "완료"} for i in values)
    targets = [i for i in values if i.check == "대상"]
    assert [i.focus_text for i in targets] == ["검토 대상", "전달 대상·채널은 누구에게 가나요?"]
    assert "결과·성과" in values[0].text


def test_soft_backward_background_is_not_required_but_forward_units_still_fit_eight(
    config: AppConfig,
) -> None:
    """역방향 고요 배경을 빼 필수8개를 실현하면서 명시 선택의 정방향 연결은 보존한다."""
    u = [
        _utterance("S1", "별도 일괄 처리는 예정이고요.", 10),
        _utterance("S1", "수정본은 임시 저장이 될 수 있는데", 12),
        _utterance("S1", "최종 검토 후 게시할 거기 때문에", 14),
        _utterance("S1", "편하게 작업해 주세요.", 16),
        _utterance("S1", "관리 화면은 수정하지 말고 목록에만 표시하시고", 20),
        _utterance("S1", "캡처 또는 항목 번호를 주시면 확인하겠습니다.", 22),
        _utterance("S1", "게시판에 제보하고 여기 시트에다가", 30),
        _utterance("S1", "변경 내용이나 메모에 적어주시면", 32),
        _utterance("S1", "회의 뒤 제가 확인하겠습니다.", 34),
    ]
    _write(config, u)
    values = build_source_candidates(config, [_chunk(i, [v]) for i, v in enumerate(u)])
    assert [c.source_index for c in selected_with_replies([2], values, len(values))] == [1, 2, 3]
    assert [c.source_index for c in selected_with_replies([0], values, len(values))] == [
        0,
        1,
        2,
        3,
    ]
    ids = [2, 5, 6]
    selected = selected_with_replies(ids, values, len(values))
    assert [c.source_index for c in selected] == list(range(1, 9)) and len(selected) == 8
    items = question_items("게시 상태, 제보 방법과 확인 일정은 무엇인가요?")
    plan = select_facet_plan(
        json.dumps({"facets": {i.key: ids for i in items}}), values, items, values, 8, 10000
    )
    assert plan is not None and plan.ids == ids


async def test_dynamic_output_template_is_reserved_for_primary_and_refinement_and_reuse_ids(
    config: AppConfig,
) -> None:
    """출력 배열 틀은 실제 key를 모두 제공하고 한 혼합시제 발언을 두 요구에 재사용할 수 있다."""
    u = [_utterance("S1", "자료 정리는 이미 마쳤고 최종 안내는 다음 회의에 하겠습니다.", 10)]
    _write(config, u)
    seen = []

    def choose(**kwargs: Any) -> str:
        p = json.loads(kwargs["messages"][-1]["content"])
        seen.append(p)
        assert list(p)[-1] == "response_template"
        assert p["response_template"] == {"facets": {key: [] for key in p["question_items"]}}
        assert all(isinstance(v, list) for v in p["response_template"]["facets"].values())
        return json.dumps(
            {"facets": {key: [_model_candidates(p)[0]["id"]] for key in p["question_items"]}}
        )

    engine, backend = _engine(config, [_chunk(0, u)], choose, facet_fixture=False)
    result = await engine.chat("이미 공유한 일과 앞으로 공유할 일을 구분해 주세요.")
    assert backend.chat.call_count == 2 and result.llm_used and len(result.references) == 1
    assert seen[0]["question_focuses"] == seen[1]["question_focuses"]
    assert seen[0]["response_template"] == seen[1]["response_template"]
    assert "이미" in result.references[0].quote.text and "질문 항목별" not in result.answer


def test_copied_input_metadata_remains_invalid_while_output_template_has_strict_arrays(
    config: AppConfig,
) -> None:
    """실제 관측된 입력 메타데이터 복사를 허용하지 않고 출력 틀의 배열만 검증한다."""
    u = [_utterance("S1", "검토 후 회신하겠습니다.", 10)]
    _write(config, u)
    values = build_source_candidates(config, [_chunk(0, u)])
    query = "담당과 공유 방식, 후속 일정은 무엇인가요?"
    items = question_items(query)
    payload, packed = selector_payload(
        query,
        values,
        8,
        4000,
        items=items,
        costs=candidate_context_costs(values),
        reserve_coverage=True,
    )
    p = json.loads(payload)
    copied = json.dumps({"facets": p["question_items"]})
    assert select_facet_plan(copied, packed, items, values, 8, 4000) is None
    assert select_facet_plan(copied[:-1], packed, items, values, 8, 4000) is None
    plan = select_facet_plan(json.dumps(p["response_template"]), packed, items, values, 8, 4000)
    assert plan is not None and plan.ids == []
    secondary, fits = facet_refinement_payload(
        query, packed, items, plan, candidate_context_costs(values), values, 8, 4000
    )
    assert fits and json.loads(secondary)["response_template"] == p["response_template"]


async def test_empty_template_copy_is_not_counted_as_generated_answer(config: AppConfig) -> None:
    """빈 출력 틀은 형식이 유효해도 완전한 근거나 생성 성공으로 세지 않는다."""
    u = [_utterance("S1", "검토 후 안내하겠습니다.", 10)]
    _write(config, u)

    def choose(**kwargs: Any) -> str:
        return json.dumps(json.loads(kwargs["messages"][-1]["content"])["response_template"])

    engine, backend = _engine(config, [_chunk(0, u)], choose, facet_fixture=False)
    result = await engine.chat("담당과 공유 방식, 후속 일정은 무엇인가요?")
    assert backend.chat.call_count == 2 and not result.llm_used
    assert (
        "근거를 확정하지 못했습니다" in result.answer and "인용 예산 때문에" not in result.answer
    )


def test_facet_budget_includes_ack_and_atomic_negative_or_context(config: AppConfig) -> None:
    """모델 선택 수뿐 아니라 짧은 응답·금지·OR를 포함한 실제 인용 비용을 검증한다."""
    u = [
        _utterance("S1", "관리 화면은 수정하지 말고 목록에만 표시하시고", 10),
        _utterance("S1", "캡처 또는 항목 번호를 보내주세요.", 12),
        _utterance("S2", "네 알겠습니다.", 14),
    ]
    _write(config, u)
    full = build_source_candidates(config, [_chunk(0, u)])
    selectable = [c for c in full if not c.context_only]
    items = question_items("어디에 어떻게 제보하고, 누가 확인하나요?")
    raw = json.dumps({"facets": {item.key: [1] for item in items}})
    assert candidate_context_costs(full)[1] == 3
    assert select_facet_plan(raw, selectable, items, full, 2, 5000) is None
    plan = select_facet_plan(raw, selectable, items, full, 3, 5000)
    assert plan is not None and plan.ids == [1]
    assert (
        select_facet_plan(
            json.dumps({"facets": {item.key: [full[-1].id] for item in items}}),
            full,
            items,
            full,
            8,
            5000,
        )
        is None
    )
    selected = selected_with_replies(plan.ids, selectable, 3, context_candidates=full)
    assert [c.quote.text for c in selected] == [x["text"] for x in u]


async def test_facet_refinement_replaces_optional_fact_with_recipient_dialogue_within_budget(
    config: AppConfig,
) -> None:
    """첫 보조 선택을 교체해 대상 질문 두 개·실제 답·요청 ACK를 자기 참조로 보존한다."""
    u = [
        _utterance("S1", "별도 요약은 이미 정리해 두었습니다.", 10),
        _utterance("S2", "새 담당자도 같이 받을 수 있나요?", 20),
        _utterance("S2", "운영 담당자요?", 22),
        _utterance("S1", "아 그분은 게시판으로.", 24),
        _utterance("S2", "그 채널에 안내하시죠.", 26),
        _utterance("S3", "네 알겠습니다.", 28),
    ]
    _write(config, u)
    seen = []
    config.chat.source_answer_max_items = 5

    def choose(**kwargs: Any) -> str:
        p = json.loads(kwargs["messages"][-1]["content"])
        seen.append(p)
        bytext = {c["text"]: c for c in _model_candidates(p)}
        assert bytext[u[3]["text"]]["cost"] == 3 and bytext[u[4]["text"]]["cost"] == 2
        if len(seen) == 1:
            return json.dumps(
                {
                    "facets": {
                        key: [0] if val["check"] == "대상" else [4]
                        for key, val in p["question_items"].items()
                    }
                }
            )
        assert "prior_plan" in p and p["candidates"] == seen[0]["candidates"]
        return json.dumps(
            {
                "facets": {
                    key: [3] if val["check"] == "대상" else [4]
                    for key, val in p["question_items"].items()
                }
            }
        )

    engine, backend = _engine(
        config, [_chunk(i, [v]) for i, v in enumerate(u)], choose, facet_fixture=False
    )
    response = await engine.chat("공유 대상·채널을 알려 주고, 설명해 주세요.", session_id="facets")
    assert backend.chat.call_count == 2 and engine._model_manager.acquire.call_count == 1
    assert response.llm_used and len(response.references) == 5
    assert [r.quote.text for r in response.references if r.quote] == [v["text"] for v in u[1:]]
    assert (
        "별도 요약" not in response.answer and "질문 항목별로 선택한 근거" not in response.answer
    )
    assert engine.get_session("facets").history[-1].content == response.answer
    for call in backend.chat.call_args_list:
        from search.chat import _estimate_korean_tokens

        msgs = call.kwargs["messages"]
        assert (
            _estimate_korean_tokens(msgs[0]["content"])
            + len(msgs[-1]["content"]) / 1.5
            + config.llm.chat_max_tokens
            <= engine._max_context_tokens
        )


@pytest.mark.parametrize("second", ["bad_schema", "unknown", "less_coverage"])
async def test_invalid_or_less_covered_refinement_keeps_first_valid_plan(
    config: AppConfig, second: str
) -> None:
    """두 번째 계획이 잘못되거나 빈 항목만 늘리면 첫 현재 근거를 유지하고 세 번째 호출은 없다."""
    u = [
        _utterance("S1", "자료를 확인하고 회신하겠습니다.", 10),
        _utterance("S2", "안내는 지정 채널로 보냅니다.", 12),
    ]
    _write(config, u)
    count = 0

    def choose(**kwargs: Any) -> str:
        nonlocal count
        count += 1
        p = json.loads(kwargs["messages"][-1]["content"])
        keys = list(p["question_items"])
        if count == 1:
            return json.dumps({"facets": {key: [0] for key in keys}})
        if second == "bad_schema":
            return '{"ids":[1]}'
        return json.dumps({"facets": {key: [99] if second == "unknown" else [] for key in keys}})

    engine, backend = _engine(config, [_chunk(0, u)], choose, facet_fixture=False)
    result = await engine.chat("공유 방식, 안내 채널은 무엇인가요?")
    assert backend.chat.call_count == 2 and result.llm_used
    assert [r.quote.text for r in result.references if r.quote] == [u[0]["text"]]


async def test_wiki_fallback_receipt_is_pre_rendered_and_stream_history_consistent(
    config: AppConfig,
) -> None:
    """실제 위키 실패 기록만 범위 안내로 전달하며 완료·문서 부재로 바꾸지 않는다."""
    from api.routers.search_chat import _ChatEngineAdapter
    from core.wiki.chat_integration import HybridChatService
    from core.wiki.router import RouteDecision, RouterVerdict

    u = [_utterance("S1", "확인 결과는 다음 회의에 공유하겠습니다.", 10)]
    _write(config, u)
    engine, backend = _engine(config, [_chunk(0, u)], '{"ids":[0]}')
    router = MagicMock()
    router.classify = AsyncMock(
        return_value=RouterVerdict(RouteDecision.WIKI, 9, "범위검사", [], False)
    )
    service = HybridChatService(_ChatEngineAdapter(engine), router, None)
    response = await service.respond(
        "공개 문서에서 확인한 원문 한 문장을 그대로 인용해 주세요.", session_id="scope"
    )
    answer = response.rag_response.answer
    assert answer.startswith("이번 검색에서 답변에 사용할 공개 Wiki 근거를 선택하지 못해")
    assert "공개 문서가 없습니다" not in answer and "전체 작업이 미완료" not in answer
    assert engine.get_session("scope").history[-1].content == answer
    query = "공개 문서에서 확인한 원문 한 문장을 그대로 인용해 주세요."
    events = [
        e
        async for e in engine.stream_chat(
            query, session_id="scope-stream", source_scope=ChatSourceScope.WIKI_FALLBACK
        )
    ]
    assert events[-1]["data"]["answer"] == answer
    assert engine.get_session("scope-stream").history[-1].content == answer
    assert backend.chat.call_count == 2
    plain = await engine.chat(query, session_id="plain")
    assert "이번 검색에서 답변에 사용할 공개 Wiki" not in plain.answer


def test_network_word_is_not_an_affirmative_reply_context(config: AppConfig) -> None:
    """네트워크를 짧은 네 응답으로 오인해 앞 질문을 인용 예산에 넣지 않는다."""
    u = [
        _utterance("S1", "다른 자료를 확인하셨나요?", 10),
        _utterance("S2", "네트워크 설정은 검토 후 공유하겠습니다.", 12),
    ]
    _write(config, u)
    values = build_source_candidates(config, [_chunk(0, u)])
    assert candidate_context_costs(values)[1] == 1
    assert [c.source_index for c in selected_with_replies([1], values, 8)] == [1]


@pytest.mark.parametrize(
    "first,following",
    [
        ("조치는 정리했고요.", "그 뒤 추가 점검도 마쳤습니다."),
        ("자료를 보내주시고요.", "저희 쪽 기록은 지금 정리 중입니다."),
    ],
)
def test_polite_connective_with_asr_period_is_atomic_current_same_actor(
    config: AppConfig, first: str, following: str
) -> None:
    """고요 뒤 ASR 마침표는 원문을 바꾸지 않고 실제 같은 화자 다음 절과 묶는다."""
    u = [
        _utterance("S1", first, 10),
        _utterance("S1", following, 12),
        _utterance("S2", "네 알겠습니다.", 14),
    ]
    _write(config, u)
    values = build_source_candidates(config, [_chunk(i, [v]) for i, v in enumerate(u)])
    assert candidate_context_costs(values)[0] == 2
    selected = selected_with_replies([0], values, 8)
    assert [c.quote.text for c in selected] == [first, following]
    payload, packed = selector_payload("조치 현황, 자료 정리는 어떻게 됐나요?", values, 8, 1500)
    assert len(payload) <= 1500 and {0, 1} <= {c.id for c in packed}
    other = [values[0], values[-1]]
    assert [c.id for c in selected_with_replies([0], other, 8)] == [0]
    u[1]["speaker"] = "S2"
    _write(config, u)
    mixed = build_source_candidates(config, [_chunk(i, [v]) for i, v in enumerate(u)])
    assert candidate_context_costs(mixed)[0] == 1
    u[1] = _utterance("S1", "네 알겠습니다.", 12)
    _write(config, u)
    closed = build_source_candidates(config, [_chunk(i, [v]) for i, v in enumerate(u)])
    assert [c.source_index for c in selected_with_replies([0], closed, 8)] == [0]


async def test_polite_connective_changed_current_reply_aborts_source_plan(
    config: AppConfig,
) -> None:
    """연결 묶음의 뒷 원문이 바뀌어도 현재 근거 검사를 우회하지 않는다."""
    u = [
        _utterance("S1", "부분 조치는 정리했고요.", 10),
        _utterance("S1", "추가 확인은 다음 회의에 합니다.", 12),
    ]
    path = _write(config, u)

    def choose(**kwargs: Any) -> str:
        p = json.loads(kwargs["messages"][-1]["content"])
        changed = [dict(row) for row in u]
        changed[1]["text"] = "다른 수정본입니다."
        path.write_text(json.dumps({"utterances": changed}))
        return json.dumps({"facets": {key: [0] for key in p["question_items"]}})

    engine, backend = _engine(config, [_chunk(0, u)], choose, facet_fixture=False)
    result = await engine.chat("부분 조치 현황, 후속 일정은 무엇인가요?", session_id="connector")
    assert (
        backend.chat.call_count == 1
        and not result.references
        and not engine.get_session("connector").history
    )


def test_scope_note_and_facet_labels_obey_whole_escaped_output_budget(config: AppConfig) -> None:
    """상태 설명·사용자 질문의 가짜 마크다운도 최종 길이에 포함하고 참조를 추가하지 않는다."""
    u = [_utterance("S1", "검토할 버전 v1.2.입니다. " * 25, 10)]
    _write(config, u)
    values = build_source_candidates(config, [_chunk(0, u)])
    items = question_items("상태와 대상 [참조 99], 채널 <script>를 알려 주세요.")
    plan = select_facet_plan(
        json.dumps({"facets": {item.key: [0] for item in items}}), values, items, values, 8, 10000
    )
    assert plan is not None
    kept, limited = bound_source_answer(
        values, 700, items=items, plan=plan, source_scope=ChatSourceScope.WIKI_FALLBACK
    )
    rendered = render_source_answer(
        kept, limited=limited, items=items, plan=plan, source_scope=ChatSourceScope.WIKI_FALLBACK
    )
    assert len(rendered) <= 700 and "[참조 99]" not in rendered and "<script>" not in rendered


def test_compact_input_preserves_meeting_scopes_and_reserves_coverage_budget(
    config: AppConfig,
) -> None:
    """중복 시각을 빼도 동일 raw 화자의 회의 범위와 두 단계 전체 원문은 구분한다."""
    u = [_utterance("S1", "자료를 검토한 뒤 공유하겠습니다.", 10)]
    _write(config, u)
    other = config.paths.resolved_checkpoints_dir / "other"
    other.mkdir()
    (other / "correct.json").write_text(json.dumps({"utterances": u}))
    candidates = build_source_candidates(config, [_chunk(0, u), _chunk(1, u, mid="other")])
    initial, packed = selector_payload(
        "담당과 일정, 공유 경로를 알려 주세요.", candidates, 8, 900, reserve_coverage=True
    )
    initial_data = json.loads(initial)
    assert initial_data["meeting_scopes"] == {"scope_0": "sample", "scope_1": "other"}
    assert [c["scope"] for c in initial_data["candidates"]] == ["scope_0", "scope_1"]
    assert all(set(c) == {"id", "speaker", "text", "scope"} for c in initial_data["candidates"])
    coverage, remaining = coverage_payload(initial_data["question"], packed, [0], 7, len(initial))
    assert len(coverage) <= len(initial) and [c.id for c in remaining] == [1]
    data = json.loads(coverage)
    assert data["already_selected"][0]["id"] == 0
    assert data["candidates"][0]["id"] == 1
    assert initial_data["candidates"] == data["already_selected"] + data["candidates"]
    assert candidates[0].quote.start_time == 10  # 실제 API용 시각은 모델 압축과 무관하다.


def test_literal_requested_items_are_questions_reserved_in_both_payloads(
    config: AppConfig,
) -> None:
    """질문의 항목만 추출하고 이전 질문·후보의 단어를 사실이나 새 항목으로 보태지 않는다."""
    query = "일정과 대상·채널, 이미 한 일과 앞으로 할 일 및 미완료 조건을 구분해 주세요."
    assert requested_items(query) == ["일정", "대상", "채널", "이미", "앞으로", "미완료", "조건"]
    assert requested_items("숲과 바다를 설명해 주세요.") == []
    assert requested_items("대상과 대상, 채널을 알려 주세요.") == ["대상", "채널"]
    u = [_utterance("S1", "검토와 게시, 원인은 별도 자료입니다.", 10)]
    _write(config, u)
    values = build_source_candidates(config, [_chunk(0, u)])
    initial, packed = selector_payload(
        query,
        values,
        8,
        900,
        reserve_coverage=True,
        previous_user_question="회신 담당은 누구인가요?",
    )
    secondary, _ = coverage_payload(
        query, packed, [0], 7, len(initial), previous_user_question="회신 담당은 누구인가요?"
    )
    assert (
        json.loads(initial)["requested_items"]
        == json.loads(secondary)["requested_items"]
        == requested_items(query)
    )
    assert (
        "담당" not in json.loads(initial)["requested_items"]
        and "원인" not in json.loads(initial)["requested_items"]
    )
    assert len(secondary) <= len(initial)


def test_only_model_id_is_numeric_while_internal_source_index_is_preserved(
    config: AppConfig,
) -> None:
    """원문 순서 번호는 내부 인접 검증에 남기고 선택 입력에서는 다른 숫자와 혼동하지 않는다."""
    u = [_utterance("S1", "네.", i * 2) for i in range(20)] + [
        _utterance("S1", "다음 회의에 확인하겠습니다.", 50)
    ]
    _write(config, u)
    values = build_source_candidates(config, [_chunk(0, u)])
    assert (
        len([c for c in values if not c.context_only]) == 1
        and values[0].source_index == 20
        and values[0].id == 0
    )
    payload, packed = selector_payload("확인은 누가 하기로 했나요?", values, 8, 1000)
    row = json.loads(payload)["candidates"][0]
    assert {k for k, v in row.items() if isinstance(v, (int, float))} == {"id"}
    assert "source_index" not in row and values[0].selection_data()["source_index"] == 20
    assert select_ids('{"ids":[20]}', packed, 8, 1000) is None
    assert select_ids('{"ids":[0]}', packed, 8, 1000) == [0]


@pytest.mark.parametrize("first", ["{}", "```json\n{\n}\n```", '{"ids":[true]}', '{"ids":[999]}'])
async def test_invalid_primary_uses_one_strict_format_reselection_same_lease(
    config: AppConfig,
    first: str,
) -> None:
    """필수 필드·타입·번호 오류는 파서를 완화하지 않고 같은 후보로 한 번만 재선택한다."""
    u = [
        _utterance("S1", "검토는 아직 진행 중입니다.", 10),
        _utterance("S2", "다음 회의에 결과를 회신하겠습니다.", 12),
    ]
    _write(config, u)
    count = 0

    def choose(**kwargs: Any) -> str:
        nonlocal count
        count += 1
        data = json.loads(kwargs["messages"][-1]["content"])
        assert data["requested_items"] == ["검토", "상태", "회신", "일정"]
        if count == 1:
            return first
        assert "prior_plan" not in data
        assert "필수 형식" in kwargs["messages"][0]["content"]
        assert all("source_index" not in row for row in _model_candidates(data))
        return json.dumps(
            {"ids": [next(c["id"] for c in _model_candidates(data) if c["text"] == u[1]["text"])]}
        )

    engine, backend = _engine(config, [_chunk(0, u)], choose)
    response = await engine.chat("검토 상태, 회신 일정은 무엇인가요?", session_id="format")
    assert backend.chat.call_count == 2 and engine._model_manager.acquire.call_count == 1
    assert response.llm_used and response.references[0].quote.text == u[1]["text"]
    assert engine.get_session("format").history[-1].content == response.answer


@pytest.mark.parametrize("second", ["{}", '{"ids":[999]}', '{"ids":[0,0]}', '{"ids":[]}'])
async def test_invalid_or_empty_format_reselection_does_not_get_third_call(
    config: AppConfig, second: str
) -> None:
    """추가 선택의 실패·빈 목록도 새 사실이나 세 번째 생성으로 바꾸지 않는다."""
    u = [_utterance("S1", "아직 검토 중입니다.", 10)]
    _write(config, u)
    replies = iter(["{}", second])
    engine, backend = _engine(config, [_chunk(0, u)], lambda **_: next(replies))
    response = await engine.chat("검토 상태, 공유 일정을 알려 주세요.")
    assert backend.chat.call_count == 2 and not response.llm_used
    assert response.references[0].quote.text == u[0]["text"]
    assert "근거를 확정하지 못했습니다" in response.answer


@pytest.mark.parametrize("literal", [False, True])
async def test_disabled_auxiliary_or_literal_invalid_primary_stays_one_call(
    config: AppConfig, literal: bool
) -> None:
    """설정0과 원문 인용 요청에는 형식 재선택도 실행하지 않는다."""
    u = [_utterance("S1", "검토는 앞으로 하겠습니다.", 10)]
    _write(config, u)
    if not literal:
        config.chat.source_coverage_passes = 0
    engine, backend = _engine(config, [_chunk(0, u)], "{}")
    query = (
        "원문을 그대로 인용하고, 추측하지 마세요."
        if literal
        else "검토 상태, 공유 일정을 알려 주세요."
    )
    response = await engine.chat(query)
    assert backend.chat.call_count == 1 and not response.llm_used


async def test_dedicated_selector_and_free_chat_system_remain_separate(config: AppConfig) -> None:
    """선택 전용 지시는 자유생성 설정과 섞지 않고 일반 창작은 기존 설정을 사용한다."""
    u = [_utterance("S1", "검토 후 회신하겠습니다.", 10)]
    _write(config, u)
    engine, backend = _engine(config, [_chunk(0, u)], '{"ids":[0]}')
    custom = "자유 대화에서는 지정한 문체로 긴 설명을 써 주세요."
    free_calls = []

    def free_system() -> str:
        free_calls.append(True)
        return custom

    engine._get_system_prompt = free_system

    def choose(**kwargs: Any) -> str:
        system = kwargs["messages"][0]["content"]
        if "발언 번호 선택기" in system:
            assert custom not in system and "공개·게시 여부를 판정하는 작업이 아닙니다" in system
            return '{"ids":[0]}'
        assert system == custom
        return "사용자 문체의 창작 결과입니다."

    backend.chat.side_effect = choose
    assert (await engine.chat("누가 회신하기로 했나요?")).llm_used
    assert free_calls == []
    creative = await engine.chat("상상 소설을 써 주세요.")
    assert creative.answer == "사용자 문체의 창작 결과입니다." and free_calls == [True]


async def test_format_reselection_stream_and_sync_share_validated_answer(
    config: AppConfig,
) -> None:
    """형식 재선택도 JSON을 스트림에 내보내지 않고 같은 인용·최종 답변·이력을 남긴다."""
    u = [_utterance("S1", "검토는 내일 하겠습니다.", 10)]
    _write(config, u)
    replies = iter(["{}", '{"ids":[0]}', "{}", '{"ids":[0]}'])
    engine, backend = _engine(config, [_chunk(0, u)], lambda **_: next(replies))
    query = "검토 상태, 후속 일정을 알려 주세요."
    sync = await engine.chat(query, session_id="format-sync")
    events = [event async for event in engine.stream_chat(query, session_id="format-stream")]
    assert backend.chat.call_count == 4 and engine._model_manager.acquire.call_count == 2
    assert events[-1]["type"] == "done" and events[-1]["data"]["answer"] == sync.answer
    assert "".join(e["data"] for e in events if e["type"] == "token") == sync.answer
    assert engine.get_session("format-stream").history[-1].content == sync.answer


def test_ack_is_only_immediate_request_context_never_a_selectable_fact(config: AppConfig) -> None:
    """요청 직후 실제 짧은 응답만 자기 화자·시각으로 보존하고 합의·완료로 해석하지 않는다."""
    u = [
        _utterance("S1", "이 채널에 안내하시죠.", 10),
        _utterance("S2", "네, 알겠습니다.", 12),
        _utterance("S3", "다른 작업은 검토 중입니다.", 14),
        _utterance("S4", "네.", 16),
    ]
    _write(config, u)
    full = build_source_candidates(config, [_chunk(i, [v]) for i, v in enumerate(u)])
    assert [c.source_index for c in full if not c.context_only] == [0, 2]
    assert [c.id for c in full if not c.context_only] == [0, 1]
    assert len({c.id for c in full}) == 4
    payload, packed = selector_payload(
        "안내 대상과 채널, 후속 일정을 알려 주세요.", full, 8, 2000, reserve_coverage=True
    )
    assert all(not c.context_only for c in packed)
    ack = next(c for c in full if c.source_index == 1)
    assert ack.id not in {c["id"] for c in json.loads(payload)["candidates"]}
    assert select_ids(json.dumps({"ids": [ack.id]}), full, 8, 1000) is None
    assert selected_with_replies([ack.id], full, 8) == []
    output = selected_with_replies([0], packed, 8, context_candidates=full, include_replies=False)
    assert [c.source_index for c in output] == [0, 1]
    assert output[1].context_only and output[1].is_context and output[1].quote.speaker == "S2"
    assert output[1].quote.start_time == 12 and output[1].parent_index == 1
    assert [c.id for c in selected_with_replies([1], packed, 8, context_candidates=full)] == [1]
    assert len(selected_with_replies([0], packed, 1, context_candidates=full)) == 1
    cap = len(render_source_answer(output[:1], limited=True))
    kept, limited = bound_source_answer(output, cap)
    assert [c.id for c in kept] == [0] and limited
    text = render_source_answer(output)
    assert "연결 문맥" in text and "합의됨" not in text and "완료됨" not in text
    limited_pool = build_source_candidates(config, [_chunk(0, [u[0]])])
    assert len(selected_with_replies([0], limited_pool, 8)) == 1


async def test_ack_only_search_does_not_call_model_or_publish_standalone_fact(
    config: AppConfig,
) -> None:
    """맞장구만 검색된 결과는 선택·폴백 사실로 내보내지 않는다."""
    u = [_utterance("S2", "네, 알겠습니다.", 10)]
    _write(config, u)
    full = build_source_candidates(config, [_chunk(0, u)])
    assert len(full) == 1 and full[0].context_only
    assert selector_payload("결정은 무엇인가요?", full, 8, 1000)[1] == []
    assert coverage_payload("결정은 무엇인가요?", full, [], 8, 1000)[1] == []
    engine, backend = _engine(config, [_chunk(0, u)], '{"ids":[0]}')
    response = await engine.chat("결정과 담당, 완료 상태는 무엇인가요?")
    assert backend.chat.call_count == 0 and not response.references and not response.llm_used


@pytest.mark.parametrize("change_ack", [False, True])
async def test_ack_keeps_own_reference_and_source_change_aborts_answer(
    config: AppConfig, change_ack: bool
) -> None:
    """본문과 다른 parent의 짧은 응답도 현재 원문 확인 뒤 자기 참조로만 보낸다."""
    u = [_utterance("S1", "안내해 주세요.", 10), _utterance("S2", "네.", 12)]
    path = _write(config, u)

    def choose(**kwargs: Any) -> str:
        payload = json.loads(kwargs["messages"][-1]["content"])
        assert [c["text"] for c in payload["candidates"]] == [u[0]["text"]]
        if change_ack:
            changed = [dict(row) for row in u]
            changed[1]["text"] = "다른 문구로 수정했습니다."
            path.write_text(json.dumps({"utterances": changed}))
        return '{"ids":[0]}'

    parents = [_chunk(i, [v]) for i, v in enumerate(u)]
    engine, backend = _engine(config, parents, choose)
    response = await engine.chat("안내 일정은 무엇인가요?", session_id="ack")
    assert backend.chat.call_count == 1
    if change_ack:
        assert not response.references and not engine.get_session("ack").history
    else:
        assert [r.chunk_id for r in response.references] == [p.chunk_id for p in parents]
        assert [(r.quote.speaker, r.quote.start_time) for r in response.references if r.quote] == [
            ("S1", 10),
            ("S2", 12),
        ]


def test_literal_provenance_accepts_query_neighbors_and_exact_topic_zero_score(
    config: AppConfig,
) -> None:
    """점수0 전체를 거부하지 않고 실제 검색 인접·명시 주제만 좁게 허용한다."""
    u = [
        _utterance("S1", "해당 작업은 아직 진행 중입니다.", 10),
        _utterance("S2", "확인 결과는 다음 회의에 전달하겠습니다.", 12),
        _utterance("S3", "라임 프로젝트는 검토 후 다시 답하겠습니다.", 30),
        _utterance("S4", "다른 권한은 내일 결정하는 걸로 할까요?", 60),
    ]
    _write(config, u)
    rows = [_chunk(i, [v]) for i, v in zip((3, 4, 15, 40), u, strict=True)]
    for row in rows[1:]:
        row.score = 0.0
        row.source = "fts"
    full = build_source_candidates(config, rows)
    literal = "'라임 프로젝트' 관련 원문 한 문장을 그대로 인용해 주세요."
    output = literal_eligible_candidates(literal, full, rows, rows[:1], 1)
    assert [c.source_index for c in output] == [0, 1, 2]
    assert [c.id for c in output] == [c.id for c in full[:3]]
    assert [c.parent_index for c in output] == [0, 1, 2]
    assert output[2].quote.text == u[2]["text"] and rows[2].score == 0
    without_neighbors = literal_eligible_candidates(literal, full, rows, rows[:1], 0)
    assert [c.source_index for c in without_neighbors] == [0, 2]
    # 결과 배열의 이웃이나 다른 회의/날짜를 청크 이웃으로 오인하지 않는다.
    rows[1].meeting_id = "other"
    assert 1 not in {
        c.parent_index for c in literal_eligible_candidates(literal, full, rows, rows[:1], 1)
    }
    rows[1].meeting_id = "sample"
    rows[1].date = "2026-02-01"
    assert 1 not in {
        c.parent_index for c in literal_eligible_candidates(literal, full, rows, rows[:1], 1)
    }
    # 일반 복합 질문의 원인·타임라인 보충 점수0는 그대로 둔다.
    compound = "원인 분석 상태, 앞으로 확인할 작업은 무엇인가요?"
    assert literal_eligible_candidates(compound, full, rows, [], 0) is full


def test_literal_latin_topic_is_a_whole_term_in_zero_score_fts(config: AppConfig) -> None:
    """Latin 주제를 다른 단어 일부로 오인하지 않고 한국어 조사와 실제 FTS0는 허용한다."""
    u = [
        _utterance("S1", "capitol 자료는 내일 검토합니다.", 10),
        _utterance("S2", "API 점검은 다음 회의에 합니다.", 20),
        _utterance("S3", "api를 확인한 뒤 회신합니다.", 30),
    ]
    _write(config, u)
    rows = [_chunk(i, [v]) for i, v in zip((10, 20, 30), u, strict=True)]
    for row in rows:
        row.score = 0
        row.source = "fts"
    full = build_source_candidates(config, rows)
    query = "'api' 원문 한 문장을 그대로 인용해 주세요."
    assert [c.source_index for c in literal_eligible_candidates(query, full, rows, [], 1)] == [
        1,
        2,
    ]
    rows[1].source = "both"
    assert [c.source_index for c in literal_eligible_candidates(query, full, rows, [], 1)] == [2]
    rows[2].score = False
    assert literal_eligible_candidates(query, full, rows, [], 1) == []


async def test_literal_model_and_fallback_cannot_select_distant_generic_supplement(
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """유효한 번호를 반환해도 먼 일반 보충 구간은 선택 목록·폴백 양쪽에서 제외한다."""
    u = [
        _utterance("S1", "해당 작업은 아직 확인 중입니다.", 10),
        _utterance("S2", "결과는 다음 회의에 회신하겠습니다.", 12),
        _utterance("S3", "다른 권한을 내일 결정하는 걸로 할까요?", 60),
        _utterance("S4", "네, 알겠습니다.", 62),
    ]
    _write(config, u)
    rows = [_chunk(i, [v]) for i, v in zip((3, 4, 40, 40), u, strict=True)]
    for row in rows[1:]:
        row.score = 0.0
        row.source = "fts"
    # 서로 다른 parent의 chunk_id도 유일하게 유지한다.
    rows[3].chunk_id = "sample_chunk_41"
    rows[3].chunk_index = 41
    monkeypatch.setattr("search.source_recall.supplement_results", AsyncMock(return_value=rows))

    def choose(**kwargs: Any) -> str:
        data = json.loads(kwargs["messages"][-1]["content"])
        assert {c["text"] for c in data["candidates"]} == {u[0]["text"], u[1]["text"]}
        return '{"ids":[2]}'  # full pool에 있었어도 실제 selectable packed에 없는 번호다.

    engine, backend = _engine(config, rows[:1], choose)
    result = await engine.chat("'라임 프로젝트' 관련 한 문장을 원문 그대로 인용해 주세요.")
    assert backend.chat.call_count == 1 and not result.llm_used
    assert result.references and {r.chunk_id for r in result.references} <= {
        rows[0].chunk_id,
        rows[1].chunk_id,
    }
    assert all(
        r.quote.text not in {u[2]["text"], u[3]["text"]} for r in result.references if r.quote
    )
    assert "다른 권한" not in result.answer


async def test_literal_without_provenance_does_not_generate_or_fallback_to_noise(
    config: AppConfig,
) -> None:
    """직접 검색·이웃·명시 주제 근거가 없으면 일반 점수0 문장을 관련 답변으로 내보내지 않는다."""
    u = [_utterance("S1", "다른 권한은 내일 결정합니다.", 10)]
    _write(config, u)
    row = _chunk(40, u)
    row.score = 0
    row.source = "fts"
    engine, backend = _engine(config, [row], '{"ids":[0]}')
    result = await engine.chat("'라임 프로젝트' 원문 한 문장을 그대로 인용해 주세요.")
    assert backend.chat.call_count == 0 and not result.references and not result.llm_used
    assert "다른 권한" not in result.answer


async def test_literal_neighbor_context_keeps_own_parent_ack_and_current_identity(
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """실제 검색 anchor의 인접 요청·짧은 응답은 허용하되 먼 문맥으로 확장하지 않는다."""
    u = [
        _utterance("S1", "안내해 주세요.", 10),
        _utterance("S2", "네.", 12),
        _utterance("S1", "다른 절차를 확인해 주시고", 60),
        _utterance("S1", "별도 업무를 끝냈습니다.", 62),
    ]
    _write(config, u)
    rows = [_chunk(i, [v]) for i, v in zip((3, 4, 40, 41), u, strict=True)]
    for row in rows[1:]:
        row.score = 0
        row.source = "fts"
    monkeypatch.setattr("search.source_recall.supplement_results", AsyncMock(return_value=rows))
    engine, backend = _engine(config, rows[:1], '{"ids":[0]}')
    result = await engine.chat("안내 일정의 원문 한 문장을 그대로 인용해 주세요.")
    assert backend.chat.call_count == 1
    assert [r.chunk_id for r in result.references] == [rows[0].chunk_id, rows[1].chunk_id]
    assert [(r.quote.speaker, r.quote.start_time) for r in result.references if r.quote] == [
        ("S1", 10),
        ("S2", 12),
    ]


def test_question_reply_unit_is_atomic_and_full_current_context_cannot_be_model_id(
    config: AppConfig,
) -> None:
    """입력은 질문만 남기지 않고, 입력 밖 실제 응답은 코드 문맥으로만 추가한다."""
    u = [
        _utterance("S1", "공유는 어떤 방식으로 할 건가요?", 10),
        _utterance("S2", "확인한 결과를 다음 회의에 전달하겠습니다.", 12),
        _utterance("S2", "다만 아직 확인하지 못한 조건은 별도로 남기겠습니다.", 14),
        _utterance("S1", "별도 주제에 관한 자료입니다." * 60, 20),
    ]
    _write(config, u)
    full = build_source_candidates(config, [_chunk(i, [v]) for i, v in enumerate(u)])
    payload, packed = selector_payload("누가 어떻게 공유하나요?", full, 8, 700)
    assert len(payload) <= 700
    assert {0, 1, 2}.issubset({c.id for c in packed})
    output = selected_with_replies([0], full[:1], 8, context_candidates=full, max_chars=2000)
    assert [c.source_index for c in output] == [0, 1, 2]
    assert [c.parent_index for c in output] == [0, 1, 2]
    assert output[1].is_context and output[2].is_context
    assert select_ids('{"ids":[1]}', full[:1], 8, 1000) is None


def test_previous_unfinished_restriction_and_following_fragment_keep_own_sources(
    config: AppConfig,
) -> None:
    """같은 화자의 미완결 제한·필드·약속은 살리고 완결된 앞 주장은 붙이지 않는다."""
    u = [
        _utterance("S1", "별도 검토는 이미 끝났습니다.", 1),
        _utterance("S1", "관리 화면은 수정하지 말고 시트에만 표시해주시고", 10),
        _utterance("S1", "메시지로 보내면 제가 확인하겠습니다.", 12),
        _utterance("S1", "기록은 여기 시트에다가", 14),
        _utterance("S1", "메모 칸에 넣어주시면", 16),
        _utterance("S1", "끝난 뒤 제가 다시 확인하겠습니다.", 18),
        _utterance("S2", "다른 업무도 끝냈습니다.", 20),
    ]
    _write(config, u)
    full = build_source_candidates(config, [_chunk(i, [v]) for i, v in enumerate(u)])
    chosen = selected_with_replies([2, 3], [full[2], full[3]], 8, context_candidates=full)
    assert [c.source_index for c in chosen] == [1, 2, 3, 4, 5]
    assert [c.quote.text for c in chosen] == [v["text"] for v in u[1:6]]
    assert [c.quote.start_time for c in chosen] == [10, 12, 14, 16, 18]
    tiny = selected_with_replies([3], [full[3]], 8, context_candidates=full, max_chars=1)
    assert [c.id for c in tiny] == [3]
    full[4] = full[6]  # 실제 연속 원문이 없으면 건너뛰어 이어 붙이지 않는다.
    assert [c.id for c in selected_with_replies([3], [full[3]], 8, context_candidates=full)] == [3]


async def test_coverage_adds_missing_facets_before_context_with_one_lease(
    config: AppConfig,
) -> None:
    """기존 선택을 유지하면서 다른 질문 항목만 한 번 추가하고 자기 인용을 보존한다."""
    u = [
        _utterance("S1", "수정본은 임시 저장해 두었습니다.", 10),
        _utterance("S2", "게시 전 최종 검토는 다음 회의에 하겠습니다.", 12),
        _utterance("S3", "공지는 담당 채널로 보내겠습니다.", 14),
        _utterance("S4", "관련 없는 작업은 완료됐습니다.", 16),
    ]
    _write(config, u)
    seen = []

    def selector(**kwargs: Any) -> str:
        payload = json.loads(kwargs["messages"][-1]["content"])
        seen.append(payload)
        rows = _model_candidates(payload)
        if len(seen) == 1:
            return json.dumps({"ids": [next(c["id"] for c in rows if c["text"] == u[0]["text"])]})
        first_id = next(c["id"] for c in rows if c["text"] == u[0]["text"])
        assert all(ids == [first_id] for ids in payload["prior_plan"].values())
        assert u[0]["text"] in {c["text"] for c in rows}
        return json.dumps(
            {
                "ids": [
                    first_id,
                    *[c["id"] for c in rows if c["text"] in (u[1]["text"], u[2]["text"])],
                ]
            }
        )

    engine, backend = _engine(config, [_chunk(i, [v]) for i, v in enumerate(u)], selector)
    config.chat.source_answer_max_items = 3
    response = await engine.chat(
        "저장과 게시 상태, 공지 채널을 구분해 주세요.", session_id="coverage"
    )
    assert backend.chat.call_count == 2 and engine._model_manager.acquire.call_count == 1
    assert [r.quote.text for r in response.references if r.quote] == [v["text"] for v in u[:3]]
    assert [r.quote.speaker for r in response.references if r.quote] == ["S1", "S2", "S3"]
    assert engine.get_session("coverage").history[-1].content == response.answer
    for messages in [c.kwargs["messages"] for c in backend.chat.call_args_list]:
        from search.chat import _estimate_korean_tokens

        assert (
            _estimate_korean_tokens(messages[0]["content"])
            + len(messages[-1]["content"]) / 1.5
            + config.llm.chat_max_tokens
            <= engine._max_context_tokens
        )
    # 전체 후보는 한 번만 담고 첫 계획 메타데이터를 예약한 동일 입력 상한 안에 넣는다.
    assert seen[1]["candidates"] == seen[0]["candidates"]


@pytest.mark.parametrize(
    "bad", ['{"ids":[99]}', '{"ids":[true]}', '{"ids":[1,1]}', '설명 {"ids":[1]}']
)
async def test_invalid_coverage_retains_primary_without_third_generation(
    config: AppConfig,
    bad: str,
) -> None:
    """두 번째 형식·없는 번호 오류로 첫 근거를 버리거나 부분 JSON을 수용하지 않는다."""
    u = [
        _utterance("S1", "검토는 앞으로 하겠습니다.", 10),
        _utterance("S2", "공지는 내일 합니다.", 12),
    ]
    _write(config, u)
    replies = iter(['{"ids":[0]}', bad])
    engine, backend = _engine(
        config, [_chunk(i, [v]) for i, v in enumerate(u)], lambda **_: next(replies)
    )
    response = await engine.chat("검토 현황, 공지 일정은 무엇인가요?")
    assert backend.chat.call_count == 2 and response.llm_used
    assert [r.quote.text for r in response.references if r.quote] == [u[0]["text"]]


async def test_coverage_cannot_select_full_but_unpacked_source_id(
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """현재 원문에 실제 존재해도 모델이 보지 않은 번호는 두 번째 선택에 허용하지 않는다."""
    u = [
        _utterance("S1", "검토는 내일 하겠습니다.", 10),
        _utterance("S2", "공지는 아직 준비 중입니다.", 12),
        _utterance("S3", "다른 자료를 공유하겠습니다.", 14),
    ]
    _write(config, u)
    replies = iter(['{"ids":[0]}', '{"ids":[2]}'])

    def limited_pack(
        query: str, values: list[Any], max_items: int, chars: int, **kwargs: Any
    ) -> Any:
        visible = [c for c in values if c.source_index < 2]
        return selector_payload(query, visible, max_items, chars, **kwargs)

    monkeypatch.setattr("search.chat.selector_payload", limited_pack)
    engine, backend = _engine(
        config, [_chunk(i, [v]) for i, v in enumerate(u)], lambda **_: next(replies)
    )
    response = await engine.chat("검토 상태, 공지 일정은 무엇인가요?")
    assert backend.chat.call_count == 2
    assert [r.quote.text for r in response.references if r.quote] == [u[0]["text"]]


async def test_refined_plan_can_replace_primary_question_with_direct_answers(
    config: AppConfig,
) -> None:
    """전체 계획 보완이 첫 질문을 교체해 직접 답의 자리를 확보한다."""
    u = [
        _utterance("S1", "검토는 완료됐나요?", 10),
        _utterance("S2", "아직 확인 중입니다.", 12),
        _utterance("S1", "네, 조건은 더 확인해 주세요.", 14),
        _utterance("S3", "게시 전 최종 검토를 하겠습니다.", 16),
        _utterance("S4", "공지 채널로 전달하겠습니다.", 18),
    ]
    _write(config, u)
    replies = iter(['{"ids":[0]}', '{"ids":[3,4]}'])
    config.chat.source_answer_max_items = 3
    engine, backend = _engine(
        config, [_chunk(i, [v]) for i, v in enumerate(u)], lambda **_: next(replies)
    )
    response = await engine.chat("검토 상태, 게시 담당과 공지 경로를 알려 주세요.")
    assert backend.chat.call_count == 2
    assert [r.quote.text for r in response.references if r.quote] == [u[i]["text"] for i in (3, 4)]


@pytest.mark.parametrize(
    "mutation", ["epoch", "source", "other_source", "during_other_source", "cancel"]
)
@pytest.mark.parametrize("first_invalid", [False, True])
async def test_coverage_never_publishes_or_stores_after_source_change_or_cancel(
    config: AppConfig,
    mutation: str,
    first_invalid: bool,
) -> None:
    """추가 선택 전후에도 편집 epoch·현재 원문·취소·lease 종료 경계를 유지한다."""
    u = [
        _utterance("S1", "검토 후 공유하겠습니다.", 10),
        _utterance("S2", "공지는 내일 합니다.", 12),
        _utterance("S3", "아직 다른 조건은 확인하지 못했습니다.", 14),
    ]
    path = _write(config, u)
    count = 0
    engine: ChatEngine

    def selector(**_: Any) -> str:
        nonlocal count
        count += 1
        if count == 1 and mutation == "source":
            path.write_text(json.dumps({"utterances": []}))
        if count == 1 and mutation == "other_source":
            changed = [dict(row) for row in u]
            changed[1]["text"] = "선택하지 않은 다른 원문을 수정했습니다."
            path.write_text(json.dumps({"utterances": changed}))
        if count == 2:
            if mutation == "during_other_source":
                changed = [dict(row) for row in u]
                changed[2]["text"] = "선택하지 않은 세 번째 문구를 수정했습니다."
                path.write_text(json.dumps({"utterances": changed}))
            if mutation == "epoch":
                engine._transcript_epoch += 1
            if mutation == "cancel":
                raise asyncio.CancelledError()
        return ("{}" if first_invalid else '{"ids":[0]}') if count == 1 else '{"ids":[1]}'

    engine, backend = _engine(config, [_chunk(i, [v]) for i, v in enumerate(u)], selector)
    if mutation == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await engine.chat("검토 상태, 공지 일정은 무엇인가요?", session_id="changed")
    else:
        response = await engine.chat("검토 상태, 공지 일정은 무엇인가요?", session_id="changed")
        assert not response.references and not response.llm_used
    assert backend.chat.call_count == (1 if mutation in {"source", "other_source"} else 2)
    assert not engine.get_session("changed").history
    engine._model_manager.acquire.return_value.__aexit__.assert_awaited_once()


@pytest.mark.parametrize(
    "query",
    [
        "결정은 무엇인가요? 한 문장을 그대로 인용하고, 추측하지 마세요.",
        "원문을 그대로 인용하고, 추측하지 마세요.",
    ],
)
async def test_literal_question_uses_one_selection_even_with_multiple_candidates(
    config: AppConfig,
    query: str,
) -> None:
    """단일 원문 인용은 복합 지시가 있어도 추가 선택 모델을 호출하지 않는다."""
    u = [
        _utterance("S1", "상태는 아직 확인 중입니다.", 10),
        _utterance("S2", "내일 확인하겠습니다.", 12),
    ]
    _write(config, u)

    def choose(**kwargs: Any) -> str:
        payload = json.loads(kwargs["messages"][-1]["content"])
        assert payload["requested_items"] == requested_items(query)
        assert "already_selected" not in payload
        return '{"ids":[1]}'

    engine, backend = _engine(config, [_chunk(0, u)], choose)
    response = await engine.chat(query)
    assert backend.chat.call_count == 1 and len(response.references) == 1


@pytest.mark.parametrize("invalid", [-1, 2, True, "1"])
def test_coverage_setting_is_an_exact_zero_or_one_limit(invalid: Any) -> None:
    """설정은 bool·문자열이나 추가 재시도 횟수로 묵시 확장하지 않는다."""
    assert ChatConfig().source_coverage_passes == 1
    with pytest.raises(ValidationError):
        ChatConfig(source_coverage_passes=invalid)


async def test_coverage_can_be_disabled_without_changing_source_answer(config: AppConfig) -> None:
    """추가 선택을 꺼도 기존 첫 선택의 실제 원문과 단일 호출을 유지한다."""
    u = [
        _utterance("S1", "검토는 내일 하겠습니다.", 10),
        _utterance("S2", "공지는 준비 중입니다.", 12),
    ]
    _write(config, u)
    config.chat.source_coverage_passes = 0
    engine, backend = _engine(config, [_chunk(0, u)], '{"ids":[0]}')
    response = await engine.chat("검토 현황, 공지 일정을 알려 주세요.")
    assert backend.chat.call_count == 1
    assert [r.quote.text for r in response.references if r.quote] == [u[0]["text"]]


def test_whole_render_budget_accounts_for_escaped_primary_and_drops_context_first(
    config: AppConfig,
) -> None:
    """표시 이스케이프와 안내문까지 재어 문장 절단·부유 참조 없이 상한을 지킨다."""
    u = [
        _utterance("S1", "서비스 버전 v1.2. " * 35, 10),
        _utterance("S2", "별도 검토는 내일 진행합니다.", 12),
        _utterance("S2", "추가 문맥도 현재 원문입니다." * 8, 14),
    ]
    _write(config, u)
    candidates = build_source_candidates(config, [_chunk(0, u)])
    from dataclasses import replace

    selected = [candidates[0], candidates[1], replace(candidates[2], is_context=True)]
    kept, limited = bound_source_answer(selected, 700)
    assert limited and len(render_source_answer(kept, limited=limited)) <= 700
    assert candidates[2] not in kept
    assert all(c.quote.text in [v["text"] for v in u] for c in kept)
    only, limited = bound_source_answer(candidates[:1], 700)
    assert only == [] and limited  # 실제 escaped primary가 상한을 넘으면 통째로 제외한다.
    shorter = [candidates[1], replace(candidates[2], is_context=True)]
    kept, limited = bound_source_answer(
        shorter, len(render_source_answer(candidates[1:2], limited=True))
    )
    assert [c.id for c in kept] == [1] and limited


def test_model_selected_bundle_member_is_primary_under_render_pressure(config: AppConfig) -> None:
    """패킹에서 문맥으로 따라왔어도 모델이 직접 선택한 발언은 선택 우선순위를 갖는다."""
    from dataclasses import replace

    u = [
        _utterance("S1", "어떤 경로로 공유할 건가요?", 10),
        _utterance("S2", "제가 확인 후 회신하겠습니다.", 12),
        _utterance("S2", "추가 조건도 검토 중입니다." * 8, 14),
    ]
    _write(config, u)
    candidates = build_source_candidates(config, [_chunk(0, u)])
    # 같은 packed 원문을 두 번째 선택이 직접 고른 경우를 재현한다.
    packed = [candidates[0], replace(candidates[1], is_context=True)]
    selected = selected_with_replies([1, 0], packed, 8, context_candidates=candidates)
    assert not next(c for c in selected if c.id == 1).is_context
    budget = len(render_source_answer([candidates[0], candidates[1]], limited=True))
    kept, limited = bound_source_answer(selected, budget)
    assert [c.id for c in kept] == [0, 1] and limited


@pytest.mark.parametrize(
    "query",
    [
        "누가 결과를 공유하기로 했나요?",
        "원인 분석과 후속 조치는 완료된 건가요?",
        "어떤 결정과 기한이 확정됐나요?",
        "원문 한 문장을 그대로 인용해 주세요.",
        "번역기 장애의 원인 분석은 완료됐나요?",
    ],
)
def test_generic_factual_and_literal_intents_use_source_path(query: str) -> None:
    """특정 이름·회의·주제 없이 일반적인 사실 요청으로 경로를 고른다."""
    assert source_bound_question(query)


@pytest.mark.parametrize(
    "query", ["안녕하세요", "공유 안내문을 상상해서 써 주세요", "회의 내용을 번역해 주세요"]
)
def test_creative_or_general_conversation_stays_on_normal_path(query: str) -> None:
    """일반 대화·창작·번역에 번호 선택 형식을 강제하지 않는다."""
    assert not source_bound_question(query)


@pytest.mark.parametrize(
    "raw",
    [
        '{"ids":[true]}',
        '{"ids":[null]}',
        '{"ids":[0.0]}',
        '{"ids":["0"]}',
        '{"ids":[99]}',
        '{"ids":[0,0]}',
        '{"ids":[0],"answer":"추측"}',
        '{"ids":[0],"ids":[]}',
        'prefix {"ids":[0]}',
        '{"ids":[0],"reference":5}',
    ],
)
def test_selector_cannot_add_prose_citations_duplicates_or_unknown_ids(
    config: AppConfig, raw: str
) -> None:
    """선택기는 원문 ID 외의 주체·수신인·참조 번호를 생성할 권한이 없다."""
    u = [_utterance("SPEAKER_07", "검토 후 보내겠습니다.", 10)]
    _write(config, u)
    candidates = build_source_candidates(config, [_chunk(0, u)])
    assert select_ids(raw, candidates, 3, 1000) is None


async def test_selection_owns_its_parent_and_real_actor_not_mentioned_recipient(
    config: AppConfig,
) -> None:
    """ref1과 ref5의 유사 문장을 섞거나 호명된 사람을 실제 화자로 바꾸지 않는다."""
    u = [
        _utterance("SPEAKER_00", "민 담당님, 결과 공유 방식은 어떻게 하나요?", 10),
        _utterance("SPEAKER_07", "검토한 뒤 이번 결과를 회신하겠습니다.", 12),
        _utterance("SPEAKER_02", "다음 자료는 아직 준비 중입니다.", 20),
        _utterance("SPEAKER_03", "공유 주기는 정하지 않았습니다.", 30),
        _utterance("SPEAKER_04", "다른 사례는 별도로 확인합니다.", 40),
    ]
    path = _write(config, u)
    results = [_chunk(i, [u[j]]) for i, j in enumerate([0, 2, 3, 4, 1])]
    before = path.read_bytes()

    def selector(**kwargs: Any) -> str:
        assert "사용자 지정 말투·형식" not in kwargs["messages"][0]["content"]
        payload = json.loads(kwargs["messages"][-1]["content"])
        chosen = next(c for c in payload["candidates"] if c["text"] == u[1]["text"])
        return json.dumps({"ids": [chosen["id"]]})

    engine, backend = _engine(config, results, selector)
    response = await engine.chat("누가 어떤 방식으로 공유하기로 했나요?", session_id="source")
    assert backend.chat.call_count == 1
    assert len(response.references) == 1
    ref = response.references[0]
    assert ref.chunk_id == results[4].chunk_id
    assert (ref.start_time, ref.end_time, ref.speakers, ref.text_preview) == (
        results[4].start_time,
        results[4].end_time,
        results[4].speakers,
        results[4].text,
    )
    assert ref.quote and asdict(ref.quote) == dict(
        text=u[1]["text"], speaker="SPEAKER_07", start_time=12.0, end_time=13.0
    )
    assert ref.speaker_labels["SPEAKER_07"] == "참석자 2"
    assert "참석자 2" in response.answer and "민 담당님" not in response.answer
    assert re.findall(r"\[참조 (\d+)\]", response.answer) == ["1"]
    assert _build_chat_references(response.references)[0].quote is not None
    assert engine.get_session("source").history[-1].content == response.answer
    assert path.read_bytes() == before


async def test_question_reply_and_two_quotes_same_parent_keep_precise_times(
    config: AppConfig,
) -> None:
    """질문만 선택돼도 같은 원문에서 확인된 직후 응답을 별도 정확 인용으로 보존한다."""
    u = [
        _utterance("SPEAKER_00", "정리는 완료된 건가요?", 10),
        _utterance("SPEAKER_07", "정리는 마쳤지만 검토와 공유는 내일 하겠습니다.", 12),
    ]
    _write(config, u)
    parent = _chunk(0, u)
    engine, backend = _engine(config, [parent], '{"ids":[0]}')
    response = await engine.chat("무엇이 완료됐고 후속 공유는 언제인가요?")
    assert backend.chat.call_count == 1
    assert [r.chunk_id for r in response.references] == [parent.chunk_id, parent.chunk_id]
    assert [r.quote.start_time for r in response.references if r.quote] == [10.0, 12.0]
    assert all(r.start_time == 10 and r.end_time == 13 for r in response.references)
    assert "내일 하겠습니다" in response.answer
    assert re.findall(r"\[참조 (\d+)\]", response.answer) == ["1", "2"]


async def test_invalid_selector_falls_back_to_literal_current_source_only(
    config: AppConfig,
) -> None:
    """그럴듯한 추가 주체·수신인·완료 주장을 그대로 출력하지 않는다."""
    u = [_utterance("SPEAKER_07", "검토 후 보내겠습니다.", 10)]
    _write(config, u)
    engine, backend = _engine(
        config, [_chunk(0, u)], '{"ids":[0],"answer":"민 담당이 수신인에게 이미 전송 완료"}'
    )
    response = await engine.chat("누가 공유를 완료했나요?")
    assert not response.llm_used and response.llm_called
    assert backend.chat.call_count == 1
    assert response.references[0].quote.text == u[0]["text"]
    assert "수신인" not in response.answer and "이미 전송 완료" not in response.answer
    assert "근거를 확정하지 못했습니다" in response.answer


async def test_source_missing_or_parent_time_speaker_mismatch_never_calls_model(
    config: AppConfig,
) -> None:
    """청크 문구만 같아도 현재 원문·시각·화자가 맞지 않으면 후보로 만들지 않는다."""
    u = [_utterance("SPEAKER_07", "검토하겠습니다.", 10)]
    parent = _chunk(0, u)
    engine, backend = _engine(config, [parent], '{"ids":[0]}')
    assert not (await engine.chat("누가 검토하기로 했나요?")).llm_called
    _write(config, u)
    parent.start_time = 11
    assert not (await engine.chat("누가 검토하기로 했나요?")).llm_called
    parent.start_time = 10
    parent.text = "[SPEAKER_00] 검토하겠습니다."
    assert not (await engine.chat("누가 검토하기로 했나요?")).llm_called
    assert backend.chat.call_count == 0


async def test_stream_sync_parity_and_epoch_change_does_not_store_old_answer(
    config: AppConfig,
) -> None:
    """검증한 같은 답변을 보내며 yield 사이 원문 수정 뒤에는 옛 이력을 저장하지 않는다."""
    u = [_utterance("SPEAKER_00", "검토 후 공유하겠습니다.", 10)]
    _write(config, u)
    engine, backend = _engine(config, [_chunk(0, u)], '{"ids":[0]}')
    query = "공유는 어떻게 하기로 했나요?"
    sync = await engine.chat(query, session_id="sync")
    events = [e async for e in engine.stream_chat(query, session_id="stream")]
    assert "".join(e["data"] for e in events if e["type"] == "token") == sync.answer
    assert next(e["data"]["answer"] for e in events if e["type"] == "done") == sync.answer
    assert engine.get_session("stream").history[-1].content == sync.answer
    assert backend.chat.call_count == 2
    events = []
    async for event in engine.stream_chat(query, session_id="changed"):
        events.append(event)
        if event["type"] == "references":
            engine.invalidate_transcript_history()
    assert events[-1]["type"] == "error"
    assert not any(e["type"] in {"token", "done"} for e in events)
    assert engine.get_session("changed").pair_count == 0


async def test_source_mutation_during_selection_invalidates_quote(config: AppConfig) -> None:
    """epoch 외부의 파일 교체도 실제 원문 문구를 재대조해 예전 인용을 차단한다."""
    u = [_utterance("SPEAKER_00", "검토 후 공유하겠습니다.", 10)]
    path = _write(config, u)

    def mutate(**_: Any) -> str:
        path.write_text(
            json.dumps({"utterances": [_utterance("SPEAKER_00", "공유를 취소했습니다.", 10)]})
        )
        return '{"ids":[0]}'

    engine, _ = _engine(config, [_chunk(0, u)], mutate)
    response = await engine.chat("공유는 어떻게 하기로 했나요?")
    assert not response.references and not response.has_context
    assert "원문 근거를 확인하지 못해" in response.answer
    assert engine.get_session().pair_count == 0


def _index(config: AppConfig, results: list[SearchResult]) -> None:
    """색인 읽기 경계만 검증할 실제 격리 FTS를 만든다."""
    path = config.paths.resolved_meetings_db
    _ensure_fts_table(path)
    with sqlite3.connect(path) as conn:
        for r in results:
            conn.execute(
                "INSERT INTO chunks_fts(chunk_id,text,meeting_id,date,speakers,start_time,end_time,chunk_index) VALUES(?,?,?,?,?,?,?,?)",
                (
                    r.chunk_id,
                    r.text,
                    r.meeting_id,
                    r.date,
                    ",".join(r.speakers),
                    r.start_time,
                    r.end_time,
                    r.chunk_index,
                ),
            )


async def test_recall_adds_same_meeting_reply_and_generic_cause_followup_facets(
    config: AppConfig,
) -> None:
    """기본 순위를 유지하면서 멀리 있는 RCA·후속 일정도 현재 실제 색인에서 보충한다."""
    u = [
        _utterance("SPEAKER_00", "장애는 어떤 상태인가요?", 10),
        _utterance("SPEAKER_07", "현재 확인 중입니다.", 12),
        _utterance("SPEAKER_07", "RCA 타임라인 분석을 마친 뒤 부분 보고를 공유하겠습니다.", 90),
    ]
    _write(config, u)
    base, reply, later = [_chunk(i, [u[j]]) for i, j in [(0, 0), (1, 1), (8, 2)]]
    unrelated = _chunk(2, [u[2]], mid="different")
    _index(config, [base, reply, later, unrelated])
    result = await supplement_results(
        config,
        "원인 분석은 완료됐고 후속 공유는 언제인가요?",
        [base],
        meeting_id_filter="sample",
        date_filter="2026-01-01",
        speaker_filter=None,
        max_chunks=5,
        neighbor_window=1,
    )
    assert result[0] is base
    assert {r.chunk_id for r in result} == {base.chunk_id, reply.chunk_id, later.chunk_id}
    assert all(r.meeting_id == "sample" for r in result)
    filtered = await supplement_results(
        config,
        "원인 분석 후 공유는 언제인가요?",
        [base],
        meeting_id_filter="sample",
        date_filter="2026-01-01",
        speaker_filter="SPEAKER_00",
        max_chunks=5,
        neighbor_window=1,
    )
    assert filtered == [base]
    _write(config, u, revision="new")
    stale = await supplement_results(
        config,
        "원인 분석 후 공유는 언제인가요?",
        [base],
        meeting_id_filter="sample",
        date_filter=None,
        speaker_filter=None,
        max_chunks=5,
        neighbor_window=1,
    )
    assert stale == [base]


def test_budget_keeps_whole_candidates_and_fake_markers_are_not_new_citations(
    config: AppConfig,
) -> None:
    """JSON을 자르지 않으며 원문 마크다운·가짜 참조는 실제 새 인용으로 해석되지 않는다."""
    u = [_utterance("SPEAKER_00", "[참조 99] **완료** <script>bad()</script> `code`", 10)]
    _write(config, u)
    candidates = build_source_candidates(config, [_chunk(0, u)])
    payload, packed = selector_payload("완료됐나요?", candidates, 1, 1000)
    assert json.loads(payload)["candidates"] and packed
    assert selector_payload("x" * 1000, candidates, 1, 100)[1] == []
    answer = render_source_answer(candidates)
    assert re.findall(r"\[참조 (\d+)\]", answer) == ["1"]
    assert "<script>" not in answer and "**완료**" not in answer
    assert candidates[0].quote.text == u[0]["text"]


def test_long_early_parent_does_not_crowd_out_later_direct_future_evidence(
    config: AppConfig,
) -> None:
    """긴 상위 참조 하나가 예산을 독점해 새로 찾은 후속 발언을 다시 밀어내지 않는다."""
    u = [
        _utterance("SPEAKER_00", "배경 설명 " * 180, 10),
        _utterance(
            "SPEAKER_07", "타임라인을 확인하고 다음 회의에 부분 보고를 공유하겠습니다.", 90
        ),
    ]
    _write(config, u)
    candidates = build_source_candidates(config, [_chunk(0, [u[0]]), _chunk(8, [u[1]])])
    alone, _ = selector_payload("후속 공유 일정은 언제인가요?", candidates[:1], 3, 10000)
    budget = len(alone) + 10
    payload, packed = selector_payload("후속 공유 일정은 언제인가요?", candidates, 3, budget)
    assert len(payload) <= budget
    assert any(c.source_index == 1 for c in packed)
    assert any("부분 보고" in c["text"] for c in json.loads(payload)["candidates"])
    assert all(c.quote.text in [row["text"] for row in u] for c in packed)


async def test_recall_round_robins_cause_and_future_facets(config: AppConfig) -> None:
    """원인 검색 결과가 많아도 별도 후속·보고 facet을 위한 자리를 남긴다."""
    u = [_utterance("SPEAKER_00", "장애 상태가 궁금합니다.", 1)]
    u.extend(_utterance("SPEAKER_07", f"RCA 원인 분석 자료 {i}", i * 10 + 10) for i in range(6))
    u.append(_utterance("SPEAKER_07", "부분 보고를 다음 회의에 공유하겠습니다.", 100))
    _write(config, u)
    rows = [_chunk(i * 4, [row]) for i, row in enumerate(u)]
    _index(config, rows)
    output = await supplement_results(
        config,
        "원인 분석과 후속 공유는 언제인가요?",
        rows[:1],
        meeting_id_filter="sample",
        date_filter=None,
        speaker_filter=None,
        max_chunks=3,
        neighbor_window=0,
    )
    assert len(output) == 3 and output[0] is rows[0]
    assert any("RCA" in row.text for row in output[1:])
    assert any("부분 보고" in row.text for row in output[1:])


async def test_recall_rejects_symlink_parent_and_replaced_database_inode(
    config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """final 파일뿐 아니라 부모 symlink와 읽는 도중의 DB inode 교체도 차단한다."""
    import search.source_recall as recall

    u = [
        _utterance("SPEAKER_00", "공유 여부는 확인 중입니다.", 10),
        _utterance("SPEAKER_07", "다음 회의에 공유하겠습니다.", 12),
    ]
    _write(config, u)
    rows = [_chunk(i, [item]) for i, item in enumerate(u)]
    _index(config, rows)
    args = dict(
        meeting_id_filter="sample",
        date_filter=None,
        speaker_filter=None,
        max_chunks=4,
        neighbor_window=1,
    )
    alias = config.paths.resolved_base_dir / "alias"
    alias.symlink_to(config.paths.resolved_base_dir, target_is_directory=True)
    config.paths.meetings_db = "alias/meetings.db"
    assert await supplement_results(config, "공유는 언제인가요?", rows[:1], **args) == rows[:1]
    config.paths.meetings_db = "meetings.db"
    original = recall._assert_index_stable
    calls = 0

    def replace(path: Path, parent_fd: int, file_fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            path.rename(tmp_path / "preserved-index.db")
            path.write_bytes(b"changed index inode")
        original(path, parent_fd, file_fd)

    monkeypatch.setattr(recall, "_assert_index_stable", replace)
    assert await supplement_results(config, "공유는 언제인가요?", rows[:1], **args) == rows[:1]
    assert calls == 3


async def test_cancellation_during_selector_releases_lease_without_history(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """선택을 기다리다 취소하면 추가 모델 호출·답변·세션 저장을 하지 않는다."""
    u = [_utterance("SPEAKER_00", "검토 후 공유하겠습니다.", 10)]
    _write(config, u)
    engine, _ = _engine(config, [_chunk(0, u)], '{"ids":[0]}')
    started = asyncio.Event()

    async def wait(*_args: Any, **_kwargs: Any) -> str:
        started.set()
        await asyncio.Event().wait()
        return ""

    monkeypatch.setattr("search.chat.await_native_inference", wait)
    task = asyncio.create_task(engine.chat("공유는 언제인가요?"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine._model_manager.acquire.call_count == 1
    assert engine._model_manager.acquire.return_value.__aexit__.await_count == 1
    assert engine.get_session().pair_count == 0


async def test_edited_current_receipt_and_embed_accept_neighbor_but_stale_or_unsafe_source_blocks(
    config: AppConfig,
) -> None:
    """편집 후 현재 영수증·embed가 맞으면 수락하고 옛 버전·위험한 상위 원문은 차단한다."""
    old = [_utterance("SPEAKER_00", "과거 원문은 별도 보존합니다.", 10)]
    old_path = _write(config, old, revision="old")
    current = [
        _utterance("SPEAKER_00", "후속 공유는 어떻게 하나요?", 10),
        _utterance("SPEAKER_07", "검토 후 다음 회의에 공유하겠습니다.", 12),
    ]
    output = config.paths.resolved_outputs_dir / "sample" / "corrected.json"
    output.parent.mkdir(parents=True)
    output.write_text(json.dumps({"utterances": current, "search_revision": "current"}))
    rows = [_chunk(i, [u]) for i, u in enumerate(current)]
    _index(config, rows)
    cp = config.paths.resolved_checkpoints_dir / "sample"
    embed = cp / "embed.json"
    embedded = {"chunks": [r.to_dict() for r in rows]}
    embed.write_text(json.dumps(embedded))
    receipt = cp / "search_receipt.json"
    receipt.write_text(json.dumps({"revision": "current", "state": "current"}))
    kwargs = dict(
        meeting_id_filter="sample",
        date_filter=None,
        speaker_filter=None,
        max_chunks=4,
        neighbor_window=1,
    )
    accepted = await supplement_results(
        config, "후속 공유는 어떻게 하기로 했나요?", rows[:1], **kwargs
    )
    assert [r.chunk_id for r in accepted] == [r.chunk_id for r in rows]
    candidates = build_source_candidates(config, accepted)
    assert {c.quote.text for c in candidates} == {u["text"] for u in current}
    assert all("과거 원문" not in c.quote.text for c in candidates)
    embedded["chunks"][1]["text"] = "과거 색인 내용"
    embed.write_text(json.dumps(embedded))
    assert (
        await supplement_results(config, "후속 공유는 언제인가요?", rows[:1], **kwargs) == rows[:1]
    )
    embed.write_text(json.dumps({"chunks": [r.to_dict() for r in rows]}))
    receipt.write_text(json.dumps({"revision": "old", "state": "current"}))
    assert (
        await supplement_results(config, "후속 공유는 언제인가요?", rows[:1], **kwargs) == rows[:1]
    )
    receipt.write_text(json.dumps({"revision": "current", "state": "current"}))
    output.unlink()
    output.symlink_to(old_path)
    assert (
        await supplement_results(config, "후속 공유는 언제인가요?", rows[:1], **kwargs) == rows[:1]
    )
    assert build_source_candidates(config, rows) == []


@pytest.mark.parametrize("reverse_selection", [False, True])
async def test_filler_first_parent_keeps_later_question_reply_and_followup_in_budget(
    config: AppConfig, reverse_selection: bool
) -> None:
    """맞장구가 먼저 있어도 후반 질문·응답·추가 조건을 원문 순서로 각각 인용한다."""
    u = [
        _utterance("UNKNOWN", "네.", 1),
        _utterance("SPEAKER_04", "알겠습니다.", 2),
        _utterance("SPEAKER_01", "공유는 언제 하기로 한 건가요?", 10),
        _utterance("SPEAKER_07", "정리는 마쳤지만 검토와 부분 보고는 다음 회의에 하겠습니다.", 12),
        _utterance("SPEAKER_07", "다만 확인하지 못한 조건은 아직 미정입니다.", 14),
    ]
    _write(config, u)
    parent = _chunk(0, u)
    candidates = build_source_candidates(config, [parent])
    assert [c.source_index for c in candidates if not c.context_only] == [2, 3, 4]
    packed_payload, packed = selector_payload(
        "후속 공유와 미정인 조건은 무엇인가요?", candidates, 3, 10000
    )
    budget = len(packed_payload)
    again, limited = selector_payload(
        "후속 공유와 미정인 조건은 무엇인가요?", candidates, 3, budget
    )
    assert len(again) <= budget and [c.source_index for c in limited] == [2, 3, 4]

    def select(**kwargs: Any) -> str:
        values = json.loads(kwargs["messages"][-1]["content"])["candidates"]
        question = next(c["id"] for c in values if c["text"] == u[2]["text"])
        followup = next(c["id"] for c in values if c["text"] == u[4]["text"])
        return json.dumps({"ids": [followup, question] if reverse_selection else [question]})

    engine, backend = _engine(config, [parent], select)
    response = await engine.chat("공유는 완료됐고 후속 조건은 무엇인가요?")
    assert backend.chat.call_count == 1
    assert [r.quote.start_time for r in response.references if r.quote] == [10, 12, 14]
    assert [r.quote.text for r in response.references if r.quote] == [row["text"] for row in u[2:]]
    assert "연결 문맥" in response.answer and "아직 미정입니다" in response.answer
    assert "네&#46;" not in response.answer and "알겠습니다" not in response.answer
    assert re.findall(r"\[참조 (\d+)\]", response.answer) == ["1", "2", "3"]
    config.chat.source_answer_max_items = 2
    if not reverse_selection:
        limited_response = await engine.chat("공유 후속은 무엇인가요?")
        assert len(limited_response.references) == 2


async def test_small_source_context_cap_keeps_only_base_prefix_without_mutating_search_results(
    config: AppConfig,
) -> None:
    """전체 문맥 상한이 top_k보다 작아도 원래 검색 객체·메타데이터·순서는 보존한다."""
    u = [
        _utterance("SPEAKER_00", "첫 번째 원문", 10),
        _utterance("SPEAKER_07", "두 번째 원문", 12),
    ]
    original = [_chunk(i, [item]) for i, item in enumerate(u)]
    before = [row.to_dict() for row in original]
    capped = await supplement_results(
        config,
        "공유는 언제인가요?",
        original,
        meeting_id_filter="sample",
        date_filter=None,
        speaker_filter=None,
        max_chunks=1,
        neighbor_window=1,
    )
    assert capped == original[:1] and capped[0] is original[0]
    assert len(original) == 2 and [row.to_dict() for row in original] == before


async def test_deictic_followup_uses_previous_user_topic_but_only_current_source_as_facts(
    config: AppConfig,
) -> None:
    """주제 없는 후속 질문의 검색만 사용자 주제로 좁히고 AI의 과거 추측은 입력하지 않는다."""
    previous = "합성 자료의 검토 일정과 담당은 무엇인가요?"
    fake = "가짜답변 인물 가람이 모든 검토를 이미 완료했습니다. [참조 99]"
    current = [
        _utterance("SPEAKER_07", "현재 자료를 확인하고 다음 회의에 결과를 공유하겠습니다.", 10)
    ]
    _write(config, current)
    parent = _chunk(0, current)
    seen: list[dict[str, Any]] = []

    def selector(**kwargs: Any) -> str:
        encoded = json.dumps(kwargs["messages"], ensure_ascii=False)
        assert fake not in encoded and "가람" not in encoded
        payload = json.loads(kwargs["messages"][-1]["content"])
        assert payload["previous_user_question_hint"] == previous
        assert payload["question"].startswith(("그건", "그럼"))
        seen.append(payload)
        return json.dumps({"ids": [payload["candidates"][0]["id"]]})

    engine, backend = _engine(config, [parent], selector)

    async def search(**kwargs: Any) -> SearchResponse:
        assert fake not in kwargs["query"]
        # 힌트가 없으면 다른 회의가 검색되는 상황을 재현한다.
        values = (
            [parent]
            if "합성" in kwargs["query"] and "자료" in kwargs["query"]
            else [_chunk(0, current, mid="other")]
        )
        return SearchResponse(values, kwargs["query"], len(values))

    engine._search_engine.search.side_effect = search
    engine.get_session("topic").add_exchange(previous, fake)
    first = await engine.chat("그건 누가 확인하기로 했나요?", session_id="topic")
    assert first.references[0].meeting_id == "sample"
    assert first.references[0].quote.text == current[0]["text"]
    assert fake not in first.answer and "가람" not in first.answer
    events = [e async for e in engine.stream_chat("그럼 언제 공유하나요?", session_id="topic")]
    assert events[-1]["type"] == "done" and "다음 회의" in events[-1]["data"]["answer"]
    assert backend.chat.call_count == 2 and len(seen) == 2


async def test_explicit_new_topic_or_scope_does_not_inherit_history_and_old_ai_cannot_be_cited(
    config: AppConfig,
) -> None:
    """새 주제·필터를 보존하고 이전 AI의 번호·주장으로 현재 후보를 늘리지 않는다."""
    previous, fake = "옛 합성 프로젝트 담당자는 누구인가요?", "과거가짜 완료 보고 [참조 99]"
    current = [_utterance("SPEAKER_07", "새 자료는 아직 검토 전입니다.", 10)]
    _write(config, current)

    def selector(**kwargs: Any) -> str:
        payload = json.loads(kwargs["messages"][-1]["content"])
        assert "previous_user_question_hint" not in payload
        assert previous not in json.dumps(kwargs["messages"], ensure_ascii=False)
        assert fake not in json.dumps(kwargs["messages"], ensure_ascii=False)
        return '{"ids":[99]}'  # 옛 답변의 번호는 현재 근거가 될 수 없다.

    engine, backend = _engine(config, [_chunk(0, current)], selector)
    engine.get_session("topic").add_exchange(previous, fake)
    explicit = "그럼 '새 자료'는 누가 확인하나요?"
    first = await engine.chat(explicit, session_id="topic")
    assert engine._search_engine.search.call_args_list[0].kwargs["query"] == explicit
    assert first.references[0].quote.text == current[0]["text"]
    second = await engine.chat(
        "그건 누가 확인하나요?", session_id="topic", meeting_id_filter="sample"
    )
    assert engine._search_engine.search.call_args.kwargs["query"] == "그건 누가 확인하나요?"
    assert second.references[0].quote.text == current[0]["text"]
    assert backend.chat.call_count == 2 and fake not in second.answer
    history = engine.get_session("topic").history
    assert (
        followup_user_hint(
            "그럼 확인팀은 누가 담당하나요?", history, explicit_scope=False, max_chars=1000
        )
        is None
    )
    assert (
        followup_user_hint(
            "그럼 새로운예산은 누가 확인하나요?", history, explicit_scope=False, max_chars=1000
        )
        is None
    )
    assert (
        followup_user_hint("그건 누가 확인하나요?", history, explicit_scope=False, max_chars=1)
        is None
    )
