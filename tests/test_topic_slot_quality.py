"""전체 회의 채팅의 사용자 표시란별로 원문, 안건 범위, 제안 상태를 평가한다."""

from __future__ import annotations

import html
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from config import AppConfig, PathsConfig
from core.agenda_records import refresh_agenda_records
from search.chat import ChatEngine
from tests.test_topic_evidence import _meeting


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("topic", "utterances", "expected", "distractor"),
    [
        (
            "천체망원경 시연",
            [
                "천체망원경 시연 때문에 제작사에서 연락이 왔어요. 오늘 4시에 만나자고 하셨어요.",
                "다음 안건은 매점입니다. 매점 재고는 월요일까지 공유하겠습니다.",
            ],
            {"일정": [(r"오늘\s*4시", 0), (r"제안|만나자고", 0)]},
            "매점",
        ),
        (
            "온실 차광막 교체",
            [
                "온실 차광막 교체는 서쪽부터 하기로 했습니다.",
                "시설팀이 온실 차광막 도면을 금요일까지 전달하기로 했습니다.",
                "다음 안건은 도서 기증입니다. 도서 기증 명단은 화요일에 공개하겠습니다.",
            ],
            {
                "결정사항": [(r"서쪽", 0)],
                "후속 작업": [(r"시설팀", 1), (r"도면", 1)],
                "일정": [(r"금요일", 1)],
            },
            "도서 기증",
        ),
    ],
)
async def test_each_visible_slot_cites_the_correct_agenda(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    topic: str,
    utterances: list[str],
    expected: dict[str, list[tuple[str, int]]],
    distractor: str,
) -> None:
    """다른 표시란·링크 미리보기에서 발견한 답은 해당 항목의 점수에 포함하지 않는다."""
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    meeting_id = "meeting_20260105_130000"
    _meeting(config, meeting_id, *utterances)
    _meeting(config, "meeting_20260106_130000", f"{distractor}의 다음 회의는 수요일입니다.")
    monkeypatch.setattr(
        "core.pipeline.ResourceGuard.check_llm_capacity",
        lambda self: (False, 2, "테스트용 메모리 제한"),
    )
    model = MagicMock()
    engine = ChatEngine(config=config, model_manager=model, search_engine=MagicMock())
    response = await engine.chat(topic, session_id="independent")
    assert response.topic_brief and response.has_context
    assert {ref.meeting_id for ref in response.references} == {meeting_id}
    model.acquire.assert_not_called()
    answer = html.unescape(response.answer)
    slots = dict(re.findall(r"^• (결정사항|후속 작업|일정) · (.+)$", answer, re.MULTILINE))
    assert set(slots) == {"결정사항", "후속 작업", "일정"}
    assert distractor not in answer
    for name, checks in expected.items():
        field = slots[name]
        markers = [int(n) for n in re.findall(r"\[참조 (\d+)\]", field)]
        assert markers and all(1 <= n <= len(response.references) for n in markers), field
        quotes = [response.references[n - 1].quote for n in markers]
        for pattern, source in checks:
            assert re.search(pattern, field), (name, field)
            assert any(
                quote is not None
                and quote.text == utterances[source]
                and quote.start_time == 10 * (source + 1)
                for quote in quotes
            ), (name, field)


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", ["암석 표본 대여", "암석 표본 대여에 대한 EMT팀 미팅"])
async def test_stored_agenda_answers_a_clear_topic_even_when_the_minutes_omit_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, topic: str
) -> None:
    """자원이 충분해도 동일 안건에 명시된 합의·기한은 모델 대기 없이 원문에서 찾는다."""
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    mid = "meeting_20260201_100000"
    _meeting(
        config,
        mid,
        "EMT 팀에서 암석 표본 대여는 사전 예약으로 진행하기로 했습니다.",
        "암석 표본 대여 예약표를 금요일에 공유하겠습니다.",
    )
    output = config.paths.resolved_outputs_dir / mid
    output.mkdir(parents=True)
    (output / "meeting_minutes.md").write_text("## 주요 안건\n1. 박물관 운영", encoding="utf-8")
    refresh_agenda_records(config, mid)
    monkeypatch.setattr(
        "core.pipeline.ResourceGuard.check_llm_capacity", lambda self: (True, 12, None)
    )
    model = MagicMock()
    search = MagicMock()
    engine = ChatEngine(config=config, model_manager=model, search_engine=search)
    response = await engine.chat(topic, session_id="stored")
    assert response.topic_brief and not response.llm_called
    assert {ref.meeting_id for ref in response.references} == {mid}
    schedule = next(line for line in response.answer.splitlines() if line.startswith("• 일정 ·"))
    assert "금요일" in schedule and "[참조 " in schedule
    model.acquire.assert_not_called()
    search.search.assert_not_called()


def test_team_spacing_preserves_acronym_boundary() -> None:
    """다른 영문 팀 이름에 같은 문자열이 들어가도 원래 팀과 연결하지 않는다."""
    from search.topic_minutes import _present

    assert _present("r3팀", "r3 팀에서 받았습니다.")
    assert not _present("r3팀", "ar3 팀에서 받았습니다.")
    assert not _present("r3팀", "r3 센터에서 받았습니다.")
