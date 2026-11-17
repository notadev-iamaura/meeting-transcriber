"""화자 원문 ID와 회의별 표시명의 전사·검색·채팅 연결을 검증한다."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from api.routers import meeting_detail, search_chat
from config import AppConfig, PathsConfig
from core.speaker_labels import build_speaker_labels, read_speaker_labels
from search.chat import _build_context_text, _build_references
from search.hybrid_search import HybridSearchEngine, SearchResult

# 내용·일시와 무관한 합성 발화. 비연속 ID와 UNKNOWN의 첫 등장 위치를 재현한다.
ORDERS = [
    [
        "UNKNOWN",
        "SPEAKER_04",
        "SPEAKER_01",
        "SPEAKER_08",
        "SPEAKER_02",
        "SPEAKER_05",
        "SPEAKER_06",
        "SPEAKER_07",
        "SPEAKER_03",
        "SPEAKER_11",
        "SPEAKER_10",
    ],
    ["SPEAKER_02", "SPEAKER_04", "SPEAKER_01", "SPEAKER_03", "UNKNOWN"],
]


def _utterances(order: list[str]) -> list[dict[str, object]]:
    """화자 외에는 업무 내용이 없는 결정적인 발화 목록을 만든다."""
    return [
        {"speaker": speaker, "text": f"합성 발화 {i}", "start": float(i), "end": i + 0.5}
        for i, speaker in enumerate(order)
    ]


def _source(config: AppConfig, mid: str, order: list[str], *, output: bool = False) -> Path:
    """격리된 설정 아래 합성 원문만 기록한다."""
    root = config.paths.resolved_outputs_dir if output else config.paths.resolved_checkpoints_dir
    path = root / mid / ("corrected.json" if output else "correct.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"utterances": _utterances(order)}), encoding="utf-8")
    return path


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    """실사용 저장소를 읽지 않는 설정을 만든다."""
    return AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))


@pytest.mark.parametrize(
    ("order", "expected"), list(zip(ORDERS, ["참석자 9", "참석자 4"], strict=True))
)
def test_first_appearance_is_meeting_scoped_and_preserves_input(
    order: list[str], expected: str
) -> None:
    """같은 raw S3의 표시명은 각 회의 전체 순서로 정하고 입력은 그대로 둔다."""
    utterances = _utterances(order + ["SPEAKER_03", "수동 이름"])
    before = copy.deepcopy(utterances)
    labels = build_speaker_labels(utterances)
    assert labels["SPEAKER_03"] == expected
    assert labels["UNKNOWN"] == "참석자 ?"
    assert labels["수동 이름"] == "수동 이름"
    assert utterances == before


def test_object_labels_invalid_entries_and_duplicate_order() -> None:
    """객체·수동 이름을 지원하고 중복이나 빈 값이 번호를 이동시키지 않는다."""
    assert build_speaker_labels(
        [
            SimpleNamespace(speaker="UNKNOWN"),
            {},
            {"speaker": None},
            {"speaker": " "},
            {"speaker": "SPEAKER_10"},
            {"speaker": "SPEAKER_10"},
            {"speaker": "윤 담당"},
            SimpleNamespace(speaker="SPEAKER_03"),
        ]
    ) == {
        "UNKNOWN": "참석자 ?",
        "SPEAKER_10": "참석자 2",
        "윤 담당": "윤 담당",
        "SPEAKER_03": "참석자 4",
    }


def test_read_priority_and_missing_or_unsafe_source_do_not_guess(config: AppConfig) -> None:
    """Viewer 우선순위를 지키고 누락·위험 경로에서는 숫자를 추측하지 않는다."""
    _source(config, "meeting", ORDERS[0])
    _source(config, "meeting", ORDERS[1], output=True)
    assert read_speaker_labels(config, ["meeting"])["meeting"]["SPEAKER_03"] == "참석자 4"
    assert read_speaker_labels(config, ["missing", "../outside"]) == {
        "missing": {},
        "../outside": {},
    }
    target = _source(config, "unsafe", ORDERS[0])
    output = config.paths.resolved_outputs_dir / "unsafe" / "corrected.json"
    output.parent.mkdir(parents=True)
    output.symlink_to(target)
    assert read_speaker_labels(config, ["unsafe"]) == {"unsafe": {}}


def test_each_meeting_is_read_once_per_label_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """청크 수가 늘어나도 같은 요청에서 전체 원문은 회의별 한 번만 읽는다."""
    reader = MagicMock(return_value=(Path("unused"), {"utterances": _utterances(ORDERS[0])}))
    monkeypatch.setattr("core.search_revision.read_source", reader)
    labels = read_speaker_labels(object(), ["a", "a", "b", "a"])
    assert reader.call_count == 2
    assert labels["a"]["SPEAKER_03"] == labels["b"]["SPEAKER_03"] == "참석자 9"


@pytest.mark.asyncio
async def test_transcript_aliases_follow_full_utterances_not_advisory_speakers(
    config: AppConfig,
) -> None:
    """실제 전사 응답은 UNKNOWN을 뺀 목록과 별개로 전체 원문의 표시 맵을 제공한다."""
    source = _source(config, "meeting", ORDERS[0])
    before = source.read_bytes()
    app = FastAPI()
    app.state.config = config
    result = await meeting_detail.get_transcript(Request({"type": "http", "app": app}), "meeting")
    assert result.speaker_labels["SPEAKER_03"] == "참석자 9"
    assert result.speakers.index("SPEAKER_03") == 7  # UNKNOWN 제외 목록의 순서와 구분
    assert result.utterances[8].speaker == "SPEAKER_03"
    assert source.read_bytes() == before


@pytest.mark.asyncio
async def test_search_prompt_references_and_api_share_map_without_rewriting_ids(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """부분 청크만 검색해도 전체 회의 번호를 쓰고 검색·채팅 API는 raw ID를 보존한다."""
    source = _source(config, "meeting", ORDERS[0])
    before = source.read_bytes()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=object())
    context.__aexit__ = AsyncMock(return_value=False)
    manager = MagicMock()
    manager.acquire.return_value = context
    engine = HybridSearchEngine(config, manager)
    monkeypatch.setattr(engine, "_embed_query", lambda *_: [0.0])
    monkeypatch.setattr(engine, "_get_chroma_collection", lambda: object())
    monkeypatch.setattr(engine, "_get_fts_connection", lambda: None)
    raw = {
        "chunk_id": "chunk",
        "meeting_id": "meeting",
        "text": "[SPEAKER_03] 합성 응답",
        "speakers": "SPEAKER_03",
        "date": "",
        "start_time": 8.0,
        "end_time": 8.5,
    }
    monkeypatch.setattr("search.hybrid_search._search_vector", lambda *_args, **_kwargs: [raw])
    monkeypatch.setattr("search.hybrid_search._search_fts", lambda *_args, **_kwargs: [])
    response = await engine.search("합성", speaker_filter="SPEAKER_03")
    item = response.results[0]
    assert item.speaker_labels["SPEAKER_03"] == "참석자 9"
    assert item.speakers == ["SPEAKER_03"]
    assert item.text == raw["text"]
    assert response.filters_applied["speaker"] == "SPEAKER_03"
    prompt = _build_context_text(response.results)
    assert "[SPEAKER_03] 합성 응답" in prompt
    assert "SPEAKER_03 = 참석자 9" in prompt
    assert "회의 meeting 안에서만 유효" in prompt
    assert "다른 회의의 같은 번호와 동일 인물로 연결하지 마세요" in prompt
    refs = _build_references(response.results)
    assert refs[0].speakers == ["SPEAKER_03"]
    assert refs[0].speaker_labels == item.speaker_labels
    assert search_chat._build_chat_references(refs)[0].speaker_labels == item.speaker_labels
    fake = SimpleNamespace(search=AsyncMock(return_value=response))
    monkeypatch.setattr(search_chat, "_get_search_engine", lambda _: fake)
    api_result = await search_chat.search(MagicMock(), search_chat.SearchRequest(query="합성"))
    assert api_result.results[0].speaker_labels == item.speaker_labels
    assert api_result.results[0].speakers == ["SPEAKER_03"]
    assert source.read_bytes() == before


def test_missing_map_does_not_invent_prompt_alias() -> None:
    """전체 원문이 없을 때 청크 화자 번호만으로 다른 표시명을 발명하지 않는다."""
    result = SearchResult("c", "[SPEAKER_03] 합성", 0.1, "unknown", "", ["SPEAKER_03"], 0, 1)
    assert "SPEAKER_03" in _build_context_text([result])
    assert "참석자 4" not in _build_context_text([result])
    assert _build_references([result])[0].speaker_labels == {}
