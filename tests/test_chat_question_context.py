"""확인 질문만 인용한 답변에 실제 인접 응답을 보존하는 회귀."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from config import AppConfig, ChatConfig
from search.chat import ChatEngine, _question_only_answer_context
from search.hybrid_search import SearchResponse, SearchResult

QUESTION = "항목 정리는 누가 하기로 한 건가요?"
ANSWER = f'"{QUESTION}" [참조 1]'
QUERY = "최근 'Transfer Auto' 관련 결정은 무엇인가요?"


def _result(text: str) -> SearchResult:
    """인용에 들어간 실제 청크와 화자 경계를 만든다."""
    return SearchResult(
        "c1", text, 0.1, "sample", "2026-11-05", ["SPEAKER_01", "SPEAKER_02"], 10, 30
    )


def test_질문만_인용하면_같은청크의_직후응답을_원문그대로_붙인다() -> None:
    """영문 검색어의 문자 일치를 강요하지 않고 실제 대화의 이어진 발언을 보여 준다."""
    result = _result(
        f"[SPEAKER_01] 오토 트랜스퍼 관련입니다. {QUESTION}\n[SPEAKER_02] 자료 정리는 제가 맡겠습니다."
    )
    answer = _question_only_answer_context(QUERY, ANSWER, [result])
    assert answer.startswith(ANSWER)
    assert "이 질문만으로 결정·담당자·완료를 확정할 수 없습니다" in answer
    assert '"자료 정리는 제가 맡겠습니다." [참조 1]' in answer
    assert "SPEAKER_02" in answer
    assert "전체 업무나 정책이 확정·완료" in answer


@pytest.mark.parametrize(
    "following", ["", "\n[SPEAKER_02] 네.", "\n[SPEAKER_02] 무슨 자료인가요?"]
)
def test_응답이없거나_맞장구_질문이면_확정이라고_판단하지_않는다(following: str) -> None:
    """확인할 응답 없이 생성한 가짜 결론으로 빈틈을 메우지 않는다."""
    result = _result(f"[SPEAKER_01] {QUESTION}{following}")
    answer = _question_only_answer_context(QUERY, ANSWER, [result])
    assert answer.startswith(ANSWER)
    assert "확정할 수 없습니다" in answer
    assert "바로 이어진 발언" not in answer


def test_다른검색결과의_비슷한발언을_해당질문의_응답으로_상속하지_않는다() -> None:
    """다른 회의·청크에서 찾은 답변을 인접 발언처럼 붙이지 않는다."""
    first = _result(f"[SPEAKER_01] {QUESTION}")
    other = _result("[SPEAKER_02] 자료 정리는 제가 맡겠습니다.")
    other.meeting_id = "different_meeting"
    answer = _question_only_answer_context(QUERY, ANSWER, [first, other])
    assert "자료 정리는 제가 맡겠습니다" not in answer
    assert "이어진 응답은 확인하지 못했습니다" in answer


@pytest.mark.parametrize(
    "query,answer",
    [
        ("결정에 대해 어떤 질문을 했는지 인용해 주세요", ANSWER),
        ("결정에 관한 질문 자체를 찾아 주세요", ANSWER),
        (QUERY, '"자료 정리는 제가 맡겠습니다." [참조 1]'),
        (QUERY, ANSWER + "\n담당자는 원문에서 확인할 수 없습니다."),
    ],
)
def test_질문자체를_찾는요청과_이미설명한답변은_그대로_둔다(query: str, answer: str) -> None:
    """명확한 사용자 인용 의도와 충분한 기존 답변에 불필요한 문구를 붙이지 않는다."""
    result = _result(f"[SPEAKER_01] {QUESTION}\n[SPEAKER_02] 자료 정리는 제가 맡겠습니다.")
    assert _question_only_answer_context(query, answer, [result]) == answer


def test_서로다른화자의_말을_한인용으로_합쳐서_구제하지_않는다() -> None:
    """모델이 화자 경계를 넘겨 만든 인용에 뒤 발언을 임의 연결하지 않는다."""
    result = _result(
        "[SPEAKER_01] 정리는 누가\n[SPEAKER_02] 하기로 한 건가요?\n[SPEAKER_03] 제가 합니다."
    )
    answer = '"정리는 누가 하기로 한 건가요?" [참조 1]'
    completed = _question_only_answer_context(QUERY, answer, [result])
    assert completed.startswith(answer)
    assert "동일 화자 발언으로 확인하지 못해" in completed
    assert "제가 합니다" not in completed


def _engine(answer: str) -> ChatEngine:
    """실제 모델·외부 I/O 없이 동기·스트림 경로가 같은 후처리를 쓰는지 검사한다."""
    result = _result(f"[SPEAKER_01] {QUESTION}\n[SPEAKER_02] 자료 정리는 제가 맡겠습니다.")
    search = MagicMock()
    search.search = AsyncMock(
        return_value=SearchResponse(results=[result], query=QUERY, total_found=1)
    )
    backend = MagicMock()
    backend.chat.return_value = answer
    backend.chat_stream.return_value = iter([answer[:10], answer[10:]])
    context = AsyncMock()
    context.__aenter__.return_value = backend
    manager = MagicMock()
    manager.acquire.return_value = context
    engine = ChatEngine(
        config=AppConfig(chat=ChatConfig(source_bound_answers=False)),
        search_engine=search,
        model_manager=manager,
    )
    engine._get_system_prompt = lambda: "가상 테스트 프롬프트"  # type: ignore[method-assign]
    return engine


async def test_동기답변과_세션이력에_확인질문_문맥을_보존한다() -> None:
    """후처리한 응답과 다음 질문이 읽는 이력이 서로 달라지지 않는다."""
    engine = _engine(ANSWER)
    response = await engine.chat(QUERY)
    assert "자료 정리는 제가 맡겠습니다" in response.answer
    assert engine.get_session().history[-1].content == response.answer
    assert len(response.references) == 1


async def test_스트림은_기존토큰을_수정하지않고_보충문맥만_추가한다() -> None:
    """token 전체 연결·최종 done·세션 이력이 같은 본문을 보존한다."""
    engine = _engine(ANSWER)
    events = [event async for event in engine.stream_chat(QUERY)]
    tokens = [event["data"] for event in events if event["type"] == "token"]
    final = next(event["data"]["answer"] for event in events if event["type"] == "done")
    assert tokens[0] == ANSWER[:10]
    assert tokens[1] == ANSWER[10:]
    assert "".join(tokens) == final
    assert "자료 정리는 제가 맡겠습니다" in final
    assert engine.get_session().history[-1].content == final


async def test_보충토큰_전송중_원문수정은_옛_이력을_저장하지_않는다() -> None:
    """추가 yield 지점에서도 기존 전사문 편집 무효화 경계를 유지한다."""
    engine = _engine(ANSWER)
    events = []
    async for event in engine.stream_chat(QUERY):
        events.append(event)
        if event["type"] == "token" and "바로 이어진 발언" in event["data"]:
            engine.invalidate_transcript_history()
    assert not any(event["type"] == "done" for event in events)
    assert events[-1]["type"] == "error"
    assert engine.get_session().pair_count == 0
