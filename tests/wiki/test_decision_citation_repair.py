"""인용만 제한적으로 복구하고 실패한 결정 원문을 공개본과 분리해 보존한다."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import WikiConfig, WikiGroundingConfig
from core.wiki.compiler import WikiCompilerV2
from core.wiki.extractors.decision import DecisionExtractor, _select_repair_evidence
from core.wiki.guard import GuardVerdict
from core.wiki.llm_client import WikiLLMError
from core.wiki.store import WikiStore

M1 = "[meeting:sample@00:00:20]"
M2 = "[meeting:sample@00:00:41]"
ZERO = "[meeting:sample@00:00:00]"
SOURCE = [
    {"speaker": "S1", "start": 20.0, "end": 25.0, "text": "예산이 확정되면 시작할까요?"},
    {"speaker": "S2", "start": 41.0, "end": 45.0, "text": "네, 확정될 때만 시작하겠습니다."},
]


def _decision(**changes: Any) -> dict[str, Any]:
    """조건이 유지돼야 하는 가상 결정 후보를 만든다."""
    return {
        "title": "Unresolved sample",
        "decision_text": "예산이 확정되면 시작한다.",
        "background": "",
        "follow_ups": [],
        "confidence": 9,
        **changes,
    }


async def _extract(
    items: list[dict[str, Any]],
    repair: Any = "[]",
    *,
    source: list[dict[str, Any]] | None = None,
    config: WikiGroundingConfig | None = None,
) -> tuple[Any, list[Any]]:
    """실제 모델 대신 응답 시퀀스를 주고 추출·복구를 함께 실행한다."""
    llm = MagicMock(
        generate=AsyncMock(side_effect=[json.dumps(items, ensure_ascii=False), repair])
    )
    results = await DecisionExtractor(llm, repair_config=config).extract(
        meeting_id="sample",
        meeting_date=date(2026, 11, 4),
        summary="인용 복구는 이 요약을 변경하거나 근거로 사용하지 않는다.",
        utterances=source if source is not None else SOURCE,
    )
    return llm, results


@pytest.mark.asyncio
async def test_정상_복수인용은_본문_그대로_복구호출없이_유지한다() -> None:
    """제공된 질문·답변 마커가 모두 있으면 추가 추론이나 첫 발화 치환을 하지 않는다."""
    text = f"예산이 확정되면 시작한다. {M1} {M2}"
    llm, result = await _extract([_decision(decision_text=text)])
    assert llm.generate.await_count == 1
    assert result[0].decision_text == text
    assert not result[0].citation_unresolved


@pytest.mark.asyncio
async def test_누락된_여러후보와_필드를_한번만_복구하고_원문을_재작성하지_않는다() -> None:
    """모델은 마커 매핑만 반환하며 조건·배경·다른 후보의 원문은 그대로 유지한다."""
    first = _decision(background="확정 전에는 실행하지 않는다.")
    second = _decision(title="Second sample", decision_text="확정 여부를 확인한다.")
    repair = json.dumps(
        [
            {"id": 0, "citations": [M1, M2]},
            {"id": 1, "citations": [M2]},
            {"id": 2, "citations": [M1]},
        ]
    )
    llm, result = await _extract([first, second], repair)
    assert llm.generate.await_count == 2
    assert result[0].decision_text == f"{first['decision_text']} {M1} {M2}"
    assert result[0].background == f"{first['background']} {M2}"
    assert result[1].decision_text == f"{second['decision_text']} {M1}"
    assert all(not decision.citation_unresolved for decision in result)
    request = json.loads(llm.generate.await_args.kwargs["user_prompt"])
    assert len(request["claims"]) == 3
    assert set(request) == {"utterances", "claims"}


@pytest.mark.asyncio
async def test_다른필드나_후속액션의_인용을_본문과_배경에_상속하지_않는다() -> None:
    """후속 액션의 인용 하나가 있어도 누락된 두 주장 필드는 미해결로 남는다."""
    item = _decision(
        background="조건을 먼저 확인해야 한다.",
        follow_ups=[
            {"owner": "S2", "description": "확정 여부를 확인한다.", "citation_ts": "00:00:41"}
        ],
    )
    llm, result = await _extract([item])
    assert llm.generate.await_count == 2
    assert result[0].decision_text == item["decision_text"]
    assert result[0].background == item["background"]
    assert set(result[0].unresolved_citation_fields) == {"decision_text", "background"}


@pytest.mark.asyncio
async def test_후속액션의_누락도_드롭없이_원문을_보존한다() -> None:
    """timestamp가 없는 후속 설명도 임의 인용이나 삭제 없이 미해결로 보존한다."""
    item = _decision(
        decision_text=f"조건부로 진행한다. {M1}",
        follow_ups=[{"owner": "S2", "description": "상태를 확인한다."}],
    )
    _, result = await _extract([item])
    assert result[0].follow_ups[0].description == "상태를 확인한다."
    assert result[0].follow_ups[0].citation is None
    assert result[0].unresolved_citation_fields == ("follow_ups.0",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker",
    ["[meeting:other@00:00:20]", "[meeting:sample@00:00:21]", ZERO, "[meeting:sample@00:99:00]"],
)
async def test_실제_입력에_없는_마커는_복구결과로_채택하지_않는다(marker: str) -> None:
    """다른 회의·구간 내부 추정시각·기본 0초·잘못된 시각은 모두 거부한다."""
    item = _decision()
    _, result = await _extract([item], json.dumps([{"id": 0, "citations": [marker]}]))
    assert result[0].decision_text == item["decision_text"]
    assert result[0].citation_unresolved
    assert not result[0].citations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [{"id": 0, "citations": [M1], "text": "조건 없이 시작한다."}],
        [{"id": 0, "citations": [M1]}, {"id": 0, "citations": [M2]}],
        [{"id": True, "citations": [M1]}],
        [{"id": 99, "citations": [M1]}],
    ],
)
async def test_본문변경이나_모호한_주장매핑_응답은_전체_적용하지_않는다(rows: Any) -> None:
    """형식이 잘못되거나 본문을 바꾸려는 응답을 부분 성공처럼 반영하지 않는다."""
    item = _decision()
    _, result = await _extract([item], json.dumps(rows))
    assert result[0].decision_text == item["decision_text"]
    assert result[0].citation_unresolved


@pytest.mark.asyncio
async def test_실제_0초_마커는_보존하되_없는_0초를_첫발화로_바꾸지_않는다() -> None:
    """0초 인용은 실제 제공된 경우만 정상이며 가짜 0초는 미해결 원문으로 남는다."""
    item = _decision(decision_text=f"조건부로 시작한다. {ZERO}")
    llm, result = await _extract([item], source=[{**SOURCE[0], "start": 0.0}])
    assert llm.generate.await_count == 1 and not result[0].citation_unresolved
    _, invalid = await _extract([item])
    assert invalid[0].decision_text == item["decision_text"]
    assert M1 not in invalid[0].decision_text
    assert invalid[0].citation_unresolved


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["input", "item", "claims"])
async def test_복구_예산을_넘으면_추가호출없이_원문을_미해결로_보존한다(budget: str) -> None:
    """검수와 같은 입력·항목·주장 수 예산을 사용하며 원문을 잘라 근거처럼 쓰지 않는다."""
    config = WikiGroundingConfig(max_input_chars=1000)
    source = SOURCE
    item = _decision()
    if budget == "input":
        source = [{**SOURCE[0], "text": "근거" * 600}]
    elif budget == "item":
        config.max_item_chars = 100
        source = [{**SOURCE[0], "text": "근거" * 60}]
    else:
        config.max_claims = 1
        item["background"] = "추가 조건을 확인한다."
    llm, result = await _extract([item], config=config, source=source)
    assert llm.generate.await_count == 1
    assert result[0].decision_text == item["decision_text"]
    assert result[0].citation_unresolved


def _long_source() -> list[dict[str, Any]]:
    """서로 다른 두 안건과 인접 응답이 긴 무관 발화 사이에 있는 가상 회의를 만든다."""
    source = [
        {
            "speaker": "S1",
            "start": float(index * 10),
            "end": float(index * 10 + 5),
            "text": ("가나다라마바사 아자차카타파하 " * 20).strip(),
        }
        for index in range(16)
    ]
    for index, text in {
        3: "한글맞춤법 검토를 요청할까요?",
        4: "네, 그렇게 해주세요.",
        11: "세금계산서의 정산항목을 구분하겠습니다.",
        12: "네, 동의합니다.",
    }.items():
        source[index]["text"] = text
    return source


@pytest.mark.asyncio
async def test_긴회의는_주장별_관련발화와_인접턴을_예산안에서_그대로_선정한다() -> None:
    """입력을 자르지 않고 두 주제 모두의 근거와 바로 뒤 응답을 고르게 포함한다."""
    source = _long_source()
    first = _decision(decision_text="한글맞춤법 검토를 요청한다.")
    second = _decision(title="Second sample", decision_text="세금계산서의 정산항목을 구분한다.")
    marker_a = "[meeting:sample@00:00:30]"
    marker_b = "[meeting:sample@00:01:50]"
    config = WikiGroundingConfig(max_input_chars=1600)
    repair = json.dumps([{"id": 0, "citations": [marker_a]}, {"id": 1, "citations": [marker_b]}])
    llm, result = await _extract([first, second], repair, source=source, config=config)
    assert llm.generate.await_count == 2
    raw_prompt = llm.generate.await_args.kwargs["user_prompt"]
    assert len(raw_prompt) <= config.max_input_chars
    chosen = json.loads(raw_prompt)["utterances"]
    expected = [
        f"[meeting:sample@00:00:30] S1: {source[3]['text']}",
        f"[meeting:sample@00:00:40] S1: {source[4]['text']}",
        f"[meeting:sample@00:01:50] S1: {source[11]['text']}",
        f"[meeting:sample@00:02:00] S1: {source[12]['text']}",
    ]
    assert all(line in chosen for line in expected)
    assert [chosen.index(line) for line in expected] == sorted(
        chosen.index(line) for line in expected
    )
    original = {
        f"[meeting:sample@{index * 10 // 3600:02d}:{index * 10 % 3600 // 60:02d}:{index * 10 % 60:02d}] S1: {row['text']}"
        for index, row in enumerate(source)
    }
    assert set(chosen).issubset(original)
    assert all(not item.citation_unresolved for item in result)
    assert result[0].decision_text == f"{first['decision_text']} {marker_a}"
    assert result[1].decision_text == f"{second['decision_text']} {marker_b}"


@pytest.mark.asyncio
async def test_전체입력에는_있어도_선정된_복구입력에_없는_인용은_거부한다() -> None:
    """선정 범위 밖의 첫 발화를 모델이 제안해도 실제 복구 근거로 채택하지 않는다."""
    item = _decision(decision_text="한글맞춤법 검토를 요청한다.")
    llm, result = await _extract(
        [item],
        json.dumps([{"id": 0, "citations": [ZERO]}]),
        source=_long_source(),
        config=WikiGroundingConfig(max_input_chars=1600),
    )
    assert llm.generate.await_count == 2
    assert ZERO not in "\n".join(
        json.loads(llm.generate.await_args.kwargs["user_prompt"])["utterances"]
    )
    assert result[0].citation_unresolved and result[0].decision_text == item["decision_text"]


def test_예산안의_전체발화는_그대로_쓰고_초과한_발화하나를_잘라_넣지_않는다() -> None:
    """짧은 입력은 손대지 않으며 항목 예산을 넘는 턴은 조각으로 만들지 않는다."""
    config = WikiGroundingConfig(max_input_chars=1000, max_item_chars=100)
    claims = [{"id": 0, "text": "한글맞춤법 검토를 요청한다."}]
    short = [f"{M1} S1: 한글맞춤법 검토를 요청한다."]
    assert _select_repair_evidence(short, claims, config) is short
    oversized = f"{M2} S2: " + "한글맞춤법" * 100
    chosen = _select_repair_evidence([oversized, *short], claims, config)
    assert oversized not in chosen and chosen == short


@pytest.mark.asyncio
async def test_복구_timeout은_원문을_보존하고_취소는_전파한다() -> None:
    """시간 제한 실패를 복구 성공으로 바꾸지 않고 명시 취소를 삼키지 않는다."""
    started = asyncio.Event()
    calls = 0

    async def generate(**_kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        if calls % 2:
            return json.dumps([_decision()])
        started.set()
        await asyncio.Event().wait()
        return "[]"

    llm = MagicMock(generate=generate)
    extractor = DecisionExtractor(llm, repair_config=WikiGroundingConfig(timeout_seconds=0.01))
    result = await extractor.extract(
        meeting_id="sample", meeting_date=date.today(), summary="", utterances=SOURCE
    )
    assert result[0].citation_unresolved and calls == 2
    started.clear()
    task = asyncio.create_task(
        extractor.extract(
            meeting_id="sample", meeting_date=date.today(), summary="", utterances=SOURCE
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _compiler(store: WikiStore, decision: Any, guard: Any) -> WikiCompilerV2:
    """실제 결정 추출·저장만 연결하고 다른 추출기와 모델을 대체한다."""
    action = MagicMock(
        extract_new=AsyncMock(return_value=[]),
        detect_closed=AsyncMock(return_value=[]),
        render_unified_page=AsyncMock(side_effect=WikiLLMError("테스트에서는 액션 렌더 생략")),
    )
    return WikiCompilerV2(
        config=WikiConfig(root=store.root),
        store=store,
        llm=MagicMock(calls=[]),
        guard=guard,
        decision_extractor=decision,
        action_item_extractor=action,
        person_extractor=MagicMock(extract_speakers=AsyncMock(return_value=[])),
        project_extractor=MagicMock(
            extract_projects=AsyncMock(return_value=[]),
            detect_status_transitions=AsyncMock(return_value={}),
        ),
    )


@pytest.mark.asyncio
async def test_복구불가_결정은_D1전에_pending으로_보존하고_기존공개본은_유지한다(
    tmp_path: Path,
) -> None:
    """미인용 원문 삭제·조작 인용·canonical 덮어쓰기 없이 전용 사유로 격리한다."""
    store = WikiStore(tmp_path)
    path = Path("decisions/2026-11-04-unresolved-sample.md")
    previous = "---\ntype: decision\nstatus: decided\n---\n# 이전 공개본\n"
    store.write_page(path, previous)
    llm = MagicMock(generate=AsyncMock(side_effect=[json.dumps([_decision()]), "[]"]))
    guard = MagicMock(verify=AsyncMock(return_value=GuardVerdict(True, "passed", 9)))
    result = await _compiler(store, DecisionExtractor(llm), guard).compile_meeting(
        meeting_id="sample",
        meeting_date=date(2026, 11, 4),
        summary="",
        utterances=SOURCE,
    )
    assert result.pages_pending == [str(path)]
    guard.verify.assert_not_awaited()
    assert store.read_raw_page(path) == previous
    pending = store.read_page(Path("pending") / path)
    assert pending.frontmatter["status"] == "pending"
    assert pending.frontmatter["review_reason"] == "citation_unresolved"
    assert _decision()["decision_text"] in pending.content
    assert not pending.citations


@pytest.mark.asyncio
@pytest.mark.parametrize("genuine_zero", [False, True])
async def test_compiler는_없는_0초를_변환하지_않고_실제_0초만_guard에_넘긴다(
    tmp_path: Path, genuine_zero: bool
) -> None:
    """D2 전의 시간 치환을 제거하며 진짜 0초의 정상 검수 경로는 유지한다."""
    content = f"---\ntype: decision\nstatus: decided\n---\n조건부로 진행한다. {ZERO}\n<!-- confidence: 9 -->"
    decision = MagicMock(
        extract=AsyncMock(return_value=[object()]),
        render_pages=AsyncMock(return_value=[("decisions/sample.md", content)]),
    )
    store = WikiStore(tmp_path)
    guard = MagicMock(verify=AsyncMock(return_value=GuardVerdict(True, "passed", 9)))
    result = await _compiler(store, decision, guard).compile_meeting(
        meeting_id="sample",
        meeting_date=date(2026, 11, 4),
        summary="",
        utterances=[{**SOURCE[0], "start": 0.0}] if genuine_zero else SOURCE,
    )
    if genuine_zero:
        assert result.pages_created == ["decisions/sample.md"]
        assert guard.verify.await_args.kwargs["new_content"] == content
    else:
        guard.verify.assert_not_awaited()
        pending = store.read_page(Path("pending/decisions/sample.md"))
        assert ZERO in pending.content and M1 not in pending.content
        assert pending.frontmatter["review_reason"] == "citation_unresolved"
