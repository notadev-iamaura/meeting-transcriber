"""전체 회의에서 주제를 찾는 채팅의 범위·근거·실패 경로 회귀."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import search_chat
from config import AppConfig
from core.chat_scope import meeting_lookup_topic
from search.chat import ChatEngine, ChatResponse
from search.hybrid_search import SearchResponse, SearchResult
from search.source_answers import SourceCandidate, SourceQuote
from search.topic_brief import parse_topic_brief
from search.topic_passages import passage_payload, read_passage_brief


@pytest.fixture(autouse=True)
def available_test_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.pipeline.ResourceGuard.check_llm_capacity", lambda self: (True, 12, None)
    )


def test_minutes_fast_path_keeps_two_directly_supported_meetings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from search.chat import _minutes_fast_context
    from search.topic_minutes import MinutesExcerpt, TopicMinutes

    groups = []
    for mid in ("one", "two"):
        parent = SearchResult(mid, "", 0.1, mid, "2026-01-01", ["A"], 0, 10, 0, "fts")
        groups.append(([parent], [_turn(1, "도서관 출입증 재발급을 논의합니다.", mid)]))
    notes = {
        ps[0].meeting_id: TopicMinutes(
            ps[0].meeting_id,
            "digest",
            {
                "decision": (
                    MinutesExcerpt(
                        "도서관 출입증 재발급을 승인함.", "도서관 출입증 재발급을 승인함.", "결정"
                    ),
                )
            },
        )
        for ps, _ in groups
    }
    monkeypatch.setattr("search.topic_evidence.topic_evidence", lambda *args: groups)
    monkeypatch.setattr("search.chat.topic_minutes", lambda cfg, mid, topic: notes.get(mid))
    monkeypatch.setattr(
        "search.chat.build_source_candidates",
        lambda cfg, ps: next(ts for p, ts in groups if p == ps),
    )
    found = _minutes_fast_context(AppConfig(), "도서관 출입증 재발급", 5500)
    assert found and not found[2] and set(found[1]) == {"one", "two"}
    del notes["two"]
    assert _minutes_fast_context(AppConfig(), "도서관 출입증 재발급", 5500) is None
    partial = _minutes_fast_context(AppConfig(), "도서관 출입증 재발급", 5500, allow_partial=True)
    assert partial and partial[2] and len(partial[0]) == 2


def test_minutes_link_successive_source_terms_within_one_agenda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from search.chat import _minutes_fast_context
    from search.topic_brief import MeetingBrief
    from search.topic_minutes import MinutesExcerpt, TopicMinutes
    from search.topic_passages import Passage

    lines = [
        _turn(0, "단열재 이야기를 했는데요."),
        _turn(1, "추후 공유하겠습니다."),
        _turn(2, "견적서를 받은 뒤"),
        _turn(3, "비교표를 정리하기로 했습니다."),
    ]
    parent = SearchResult("a", "", 0.2, lines[0].meeting_id, "2026-01-01", ["A"], 0, 10, 0, "fts")
    topic = "단열재 견적서 비교표 산정"
    fact = MinutesExcerpt(
        "단열재 견적서 비교표를 작성하기로 함.", "단열재 견적서 비교표를 작성하기로 함.", "액션"
    )
    monkeypatch.setattr("search.topic_evidence.topic_evidence", lambda *args: [([parent], lines)])
    monkeypatch.setattr(
        "search.chat.passage_payload", lambda *args: ("", [Passage(tuple(lines), 3)])
    )
    monkeypatch.setattr(
        "search.chat.read_passage_brief", lambda *args: MeetingBrief(lines[0], {}, False)
    )
    monkeypatch.setattr(
        "search.chat.topic_minutes",
        lambda *args: TopicMinutes(parent.meeting_id, "digest", {"action": (fact,)}),
    )
    monkeypatch.setattr("search.chat.build_source_candidates", lambda *args: lines)
    answer = _minutes_fast_context(AppConfig(), topic, 7000, allow_partial=True)
    assert answer and not answer[2]
    lines[1] = _turn(1, "다음 안건은 행사입니다.")
    assert _minutes_fast_context(AppConfig(), topic, 7000, allow_partial=True) is None


@pytest.mark.asyncio
async def test_low_memory_does_not_claim_absence_or_start_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.pipeline.ResourceGuard.check_llm_capacity",
        lambda self: (True, 2, "메모리 권장량 미달"),
    )
    monkeypatch.setattr("search.topic_evidence.topic_evidence", lambda *args: [])
    search = MagicMock()
    manager = MagicMock()
    engine = ChatEngine(config=AppConfig(), model_manager=manager, search_engine=search)
    response = await engine.chat("도서관 출입증 재발급", session_id="memory")
    assert response.grounding_status == "search_error" and not response.llm_called
    assert "메모리" in response.answer and not response.has_context
    search.search.assert_not_called()
    manager.acquire.assert_not_called()


def test_minutes_reference_keeps_document_provenance_and_exact_excerpt() -> None:
    from search.chat import ChatReference, _minutes_reference

    base = ChatReference("chunk", "meeting-one", "2026-01-05", ["A"], 2.5, 4.5, "발언", 0.1)
    sentence = "교정본은 담당자가 최종 검수합니다."
    digest = "f" * 64
    minutes = _minutes_reference(base, sentence, digest=digest)
    transcript, document = search_chat._build_chat_references([base, minutes])
    assert transcript.source_kind == "transcript" and transcript.start_time == 2.5
    assert document.source_kind == "minutes" and document.meeting_id == base.meeting_id
    assert document.source_excerpt == document.text_preview == sentence
    assert document.source_digest == digest and document.quote is None
    assert document.speakers == [] and document.start_time is None and document.end_time is None
    assert document.model_dump() == minutes.to_dict() and document.chunk_id != base.chunk_id


@pytest.mark.parametrize(
    ("query", "topic"),
    [
        ("안내판 위치 변경", "안내판 위치 변경"),
        ("마스킹 작업을 논의한 회의를 찾아서 핵심만 짧게 요약해줘.", "마스킹 작업"),
        ("어느 회의에서 안내판 위치를 얘기했어?", "안내판 위치"),
        ("안내판 위치 관련 회의랑 결정사항, 후속 작업, 일정까지 알려줘", "안내판 위치"),
        ("주제: 배송 포장", "배송 포장"),
        ("그 회의에서 무엇을 결정했어?", None),
        ("프로젝트 일정", None),
        ("오늘 회의 요약해줘", None),
        ("예산이 얼마였지?", None),
        ("2026년 1월 5일 회의에서 안내판을 찾아줘", None),
        ("위키에서 안내판 논의한 회의 찾아줘", None),
    ],
)
def test_topic_lookup_intent(query: str, topic: str | None) -> None:
    assert meeting_lookup_topic(query) == topic


def _turn(idx: int, text: str, meeting: str = "meeting_20260105_130000") -> SourceCandidate:
    return SourceCandidate(idx, 0, idx, meeting, SourceQuote(text, "A", idx, idx + 1), "참석자 1")


def test_selector_keeps_exact_status_and_date_and_rejects_invalid_ids() -> None:
    turns = [
        _turn(14, "안내판을 창가에 설치하기로 했습니다."),
        _turn(15, "금요일은 요청이고, 실제 발송은 다음 주에 합니다."),
        _turn(16, "다른 회의 원문", "other"),
    ]
    raw = json.dumps(
        {
            "anchor": 14,
            "decision": [True],
            "action": [16],
            "schedule": [15],
            "text": "금요일에 발송합니다.",
        },
        ensure_ascii=False,
    )
    brief = parse_topic_brief("안내판", raw, turns)
    assert brief is not None and list(brief.facts) == ["schedule"]
    assert brief.facts["schedule"].text == turns[1].quote.text
    assert brief.facts["schedule"].evidence == (turns[1],)
    assert parse_topic_brief("안내판", '{"anchor":null}', turns) is None


def test_malformed_selection_falls_back_only_to_direct_grounding() -> None:
    turns = [
        _turn(7, "안내판은 내일 배송하기로 했습니다."),
        _turn(8, "상자 포장은 화요일에 합니다."),
    ]
    brief = parse_topic_brief("안내판", "no json", turns)
    assert brief is not None and brief.used_model is False
    assert brief.facts["decision"].evidence == brief.facts["schedule"].evidence == (turns[0],)
    assert parse_topic_brief("지우개", "no json", turns) is None


def test_missing_schedule_or_decision_is_not_inferred() -> None:
    turns = [_turn(0, "안내판을 먼저 바꿀까요?"), _turn(1, "안내판에 그림이 있습니다.")]
    brief = parse_topic_brief("안내판", '{"anchor":1,"decision":[0],"schedule":[1]}', turns)
    assert brief is not None and not brief.facts


def test_empty_facets_recover_subject_promise_and_conditional_estimate() -> None:
    turns = [
        _turn(20, "전시장 안내판은 고대비 인쇄가 필요할 것 같습니다."),
        _turn(21, "네, 제가 인쇄 파일을 보내드리겠습니다."),
        _turn(22, "수정된 도면도 전달드리겠습니다."),
        _turn(24, "다른 업체 직원은 금요일에 방문합니다."),
        _turn(25, "이 작업은 얼마나 걸릴까요?"),
        _turn(27, "도면을 주시면 3일 정도 걸릴 것 같습니다."),
    ]
    brief = parse_topic_brief(
        "안내판 작업", '{"anchor":24,"action":null,"decision":[],"schedule":[]}', turns
    )
    assert brief is not None and brief.anchor == turns[0] and brief.used_model is False
    assert brief.facts["action"].evidence == tuple(turns[1:3])
    assert brief.facts["decision"].text.startswith("확정 여부를 확인할 수 없는 논의")
    assert brief.facts["schedule"].evidence == (turns[-1],)


def test_fallback_does_not_attach_other_subject_or_new_agenda_date() -> None:
    turns = [
        _turn(0, "전시장 표지 교체에 대해 말씀드립니다."),
        _turn(1, "안내 방송은 내일 합니다."),
        _turn(2, "다음 안건은 서류입니다. 금요일에 전달하겠습니다."),
    ]
    brief = parse_topic_brief("전시장 표지", "not JSON", turns)
    assert brief is not None and not brief.facts


def test_compact_selector_accepts_only_visible_number_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    turns = [_turn(31, "회의용 조명을 설치하기로 했습니다."), _turn(32, "내일 설치하겠습니다.")]
    with caplog.at_level("INFO", logger="search.topic_brief"):
        brief = parse_topic_brief(
            "조명",
            '{"anchor":"31","decision":31,"action":{"ids":["32"]},"schedule":[32],"body":"비공개 발언"}',
            turns,
        )
    assert brief and brief.used_model and len(brief.facts) == 3
    assert "32" in caplog.text and "group=" in caplog.text
    assert all(word not in caplog.text for word in ("비공개 발언", "조명", "meeting_"))


def test_payload_budget_includes_escaped_quote_and_only_visible_ids() -> None:
    turns = [_turn(3, '안내판의 "새 문구"를 봤습니다.'), _turn(9, "둘째 " * 50)]
    payload, passages = passage_payload("안내판", turns, 100)
    assert len(payload) <= 100
    assert passages[0].turns == tuple(turns[:1])
    assert turns[0].quote.text in payload


@pytest.mark.parametrize(
    "report",
    [
        "안내판은 오늘 담당 업체와 만나서 논의했는데요.",
        "안내판은 어제 저녁 기준 24시간 동안 본 수치입니다.",
        "안내판은 지난 금요일에 우리 부서에서 논의했던 것 같습니다.",
        "안내판 손상이 확인되면 오류가 납니다.",
    ],
)
def test_historical_reports_do_not_fill_schedule_even_when_model_picks_them(report: str) -> None:
    turns = [_turn(0, report)]
    brief = parse_topic_brief("안내판", '{"anchor":0,"schedule":[0]}', turns)
    assert brief is not None and "schedule" not in brief.facts


def test_promised_reply_and_unfinished_action_keep_their_object() -> None:
    turns = [
        _turn(7, "안내판의 규격 자료를 보내주실 수 있나요?"),
        _turn(8, "네, 제가 정리해서 보내겠습니다."),
    ]
    brief = parse_topic_brief("안내판", '{"anchor":7,"action":[8]}', turns)
    assert brief is not None and brief.facts["action"].evidence == tuple(turns)
    turns = [
        _turn(7, "안내판 도면을 제작사에 전달 드리고"),
        _turn(8, "실물과 다른 폭이 있으면 제작 전에 치수를 맞추겠습니다."),
    ]
    brief = parse_topic_brief("안내판", '{"anchor":7,"action":[7]}', turns)
    assert brief is not None and brief.facts["action"].evidence == tuple(turns)


def test_refinement_selects_only_visible_grounded_passages() -> None:
    turns = [
        _turn(20, "안내판은 현재 크기를 그대로 유지하죠."),
        _turn(21, "안내판 견본은 금요일까지 공유하겠습니다."),
    ]
    brief = parse_topic_brief("안내판", '{"anchor":20}', turns)
    assert brief is not None
    payload, passages = passage_payload("안내판", turns, 600)
    assert len(payload) <= 600
    checked = read_passage_brief(
        "안내판",
        '{"decision":{"ids":[20]},"action":{"ids":[21]},"schedule":{"ids":[999]}}',
        passages,
    )
    assert checked is not None
    assert checked.facts["decision"].evidence == (turns[0],)
    assert checked.facts["action"].evidence == (turns[1],)
    assert checked.facts["schedule"] == brief.facts["schedule"]


@pytest.mark.asyncio
@pytest.mark.parametrize("edited", [False, True])
async def test_actual_chat_uses_topic_across_meetings_and_rechecks_source(
    monkeypatch: pytest.MonkeyPatch,
    edited: bool,
) -> None:
    turns = [_turn(10, "안내판은 창가에 두기로 했습니다."), _turn(11, "내일 인쇄하겠습니다.")]
    parent = SearchResult(
        "chunk", "", 0.1, turns[0].meeting_id, "2026-01-05", ["A"], 0, 30, 0, "both"
    )
    search = MagicMock()
    search.search = AsyncMock(return_value=SearchResponse([parent], "안내판"))
    manager = MagicMock()
    backend = MagicMock()
    backend.chat.return_value = '{"anchor":10,"decision":[10],"action":[11],"schedule":[11]}'
    manager.acquire.return_value.__aenter__.return_value = backend
    engine = ChatEngine(config=AppConfig(), model_manager=manager, search_engine=search)
    monkeypatch.setattr("search.topic_evidence.topic_evidence", lambda *args: [([parent], turns)])
    monkeypatch.setattr(
        "search.chat.build_source_candidates", lambda *args: turns[:1] if edited else turns
    )
    result = await engine.chat("안내판을 논의한 회의 찾아줘", session_id="new")
    assert search.search.call_args.kwargs["query"] == "안내판"
    assert "meeting_id_filter" not in search.search.call_args.kwargs
    if edited:
        assert result.grounding_status == "search_error" and not result.references
        assert engine.get_session("new").pair_count == 0
    else:
        assert result.grounding_status == "grounded" and result.topic_brief
        assert [ref.quote for ref in result.references] == [t.quote for t in turns]
        assert all(
            label in result.answer
            for label in ("13:00", "결정사항", "후속 작업", "일정", "[참조 2]")
        )
        assert engine.get_session("new").pair_count == 1
        events = [event async for event in engine.stream_chat("안내판", session_id="stream")]
        assert events[0]["data"][1]["quote"]["start_time"] == 11
        assert (
            events[-1]["type"] == "done" and events[-1]["data"]["grounding_status"] == "grounded"
        )
        assert engine.get_session("stream").pair_count == 1


@pytest.mark.parametrize("router_enabled", [True, False])
def test_topic_uses_transcripts_with_both_wiki_settings(
    router_enabled: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(search_chat.router, prefix="/api")
    app.state.config = SimpleNamespace(wiki=SimpleNamespace(router_enabled=router_enabled))
    engine = MagicMock()
    engine.chat = AsyncMock(return_value=ChatResponse("회의 요약", [], "책상 재배치"))
    monkeypatch.setattr(search_chat, "_get_chat_engine", lambda request: engine)
    hybrid = MagicMock(side_effect=AssertionError("주제 탐색은 원문이 필요합니다."))
    monkeypatch.setattr(search_chat, "_build_hybrid_chat_service", hybrid)
    response = TestClient(app).post("/api/chat", json={"query": "책상 재배치"})
    assert response.status_code == 200
    assert response.json()["source_type"] == ("rag" if router_enabled else None)
    assert engine.chat.call_args.kwargs["meeting_id_filter"] is None
    hybrid.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("max_calls", [1, 2])
async def test_one_bounded_read_for_missing_topic_facts(
    monkeypatch: pytest.MonkeyPatch, max_calls: int
) -> None:
    """예산이 허용될 때만 1위 회의의 빈 항목을 재확인하고 최초 근거를 보존한다."""
    from search.topic_brief import MeetingBrief, TopicFact

    turns = [_turn(0, "안내판은 입구가 기준입니다."), _turn(1, "내일 시안을 전달하겠습니다.")]
    parent = SearchResult(
        "chunk", "", 0.2, turns[0].meeting_id, "2026-01-05", ["A"], 0, 30, 0, "both"
    )
    search = MagicMock()
    search.search = AsyncMock(return_value=SearchResponse([parent], "안내판"))
    manager, backend = MagicMock(), MagicMock()
    manager.acquire.return_value.__aenter__.return_value = backend
    backend.chat.side_effect = ["first", "second"]
    config = AppConfig()
    config.chat.source_focus_max_calls = max_calls
    engine = ChatEngine(config=config, model_manager=manager, search_engine=search)
    monkeypatch.setattr("search.topic_evidence.topic_evidence", lambda *args: [([parent], turns)])
    monkeypatch.setattr("search.chat.build_source_candidates", lambda *args: turns)

    def reader(topic: str, raw: str, passages: object, *, secondary: bool = False) -> MeetingBrief:
        fact = TopicFact(turns[0].quote.text, (turns[0],))
        if raw == "first":
            return MeetingBrief(turns[0], {"decision": fact}, True)
        extra = TopicFact(turns[1].quote.text, (turns[1],))
        return MeetingBrief(turns[1], {"decision": extra, "schedule": extra}, True)

    monkeypatch.setattr("search.chat.read_passage_brief", reader)
    result = await engine.chat("안내판", session_id="missing")
    assert result.topic_brief and backend.chat.call_count == max_calls
    assert result.references[0].quote == turns[0].quote
    assert len(result.references) == max_calls
    assert "결정사항 · 안내판은 입구가 기준입니다" in result.answer


@pytest.mark.asyncio
async def test_complete_current_minutes_and_transcript_anchor_need_no_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """회의록 세 항목과 원문 주제 발언을 모두 확인하면 모델 없이 각 출처에 연결한다."""
    from search.topic_minutes import MinutesExcerpt, TopicMinutes

    row = _turn(10, "안내판은 공장 입구가 기준입니다.")
    parent = SearchResult("parent", "", 0.2, row.meeting_id, "2026-01-05", ["A"], 0, 30, 0, "both")
    fields = {
        "decision": "안내판을 교체하기로 함.",
        "action": "이 대리가 안을 전달.",
        "schedule": "금요일 납품 예정.",
    }
    facts = {k: (MinutesExcerpt(t, f"- {t}", "회의록"),) for k, t in fields.items()}
    notes = TopicMinutes(row.meeting_id, "a" * 64, facts)
    monkeypatch.setattr("search.chat.topic_minutes", lambda *args: notes)
    monkeypatch.setattr("search.topic_evidence.topic_evidence", lambda *args: [([parent], [row])])
    monkeypatch.setattr("search.chat.build_source_candidates", lambda *args: [row])
    search, manager = MagicMock(), MagicMock()
    search.search = AsyncMock(return_value=SearchResponse([parent], "안내판"))
    engine = ChatEngine(config=AppConfig(), model_manager=manager, search_engine=search)
    result = await engine.chat("안내판", session_id="mixed-sources")
    assert not result.llm_called and result.topic_brief
    manager.acquire.assert_not_called()
    assert result.references[0].quote == row.quote
    assert [r.source_excerpt for r in result.references[1:]] == [f"- {s}" for s in fields.values()]
    assert all(
        r.start_time is None and r.source_digest == notes.digest for r in result.references[1:]
    )
    assert all(s[:-1] in result.answer for s in fields.values())
