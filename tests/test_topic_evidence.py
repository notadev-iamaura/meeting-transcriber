"""전체 회의에서 고른 주제별 원문과 검색 색인의 경계를 검증한다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import AppConfig, PathsConfig
from search.hybrid_search import SearchResult
from search.topic_evidence import any_indexed_topic_term, topic_evidence
from tests.test_source_bound_chat import _chunk, _index, _utterance


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    """운영 회의와 분리된 실제 FTS와 원문 저장소를 사용한다."""
    return AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))


def _meeting(config: AppConfig, meeting_id: str, *lines: str) -> list[SearchResult]:
    """발화마다 다른 검색 구간을 만들어 선택된 구간 밖 후속 발언도 검증한다."""
    utterances = [
        _utterance(f"SPEAKER_0{i % 2}", line, 10 * (i + 1)) for i, line in enumerate(lines)
    ]
    source = config.paths.resolved_checkpoints_dir / meeting_id / "correct.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"utterances": utterances}, ensure_ascii=False), encoding="utf-8")
    parents = [_chunk(i, [row], mid=meeting_id) for i, row in enumerate(utterances)]
    _index(config, parents)
    return parents


def test_lexical_zero_is_distinct_from_unavailable_index(config: AppConfig) -> None:
    assert any_indexed_topic_term(config, "온실 식물") is None
    _meeting(config, "meeting_garden", "온실 차광 원단을 점검합니다.")
    assert any_indexed_topic_term(config, "온실 식물") is True
    assert any_indexed_topic_term(config, "도서관 대여증") is False


def test_rare_topic_recovers_followup_and_two_meetings_with_exact_parent_bounds(
    config: AppConfig,
) -> None:
    """검색 첫 결과에 빠진 회의·후속 발언을 살리고 각 발화의 실제 부모를 보존한다."""
    topic = "청람 밀봉검사"
    first = _meeting(
        config,
        "alpha",
        "청람 밀봉검사는 시제품에 전수 적용하기로 했습니다.",
        "제가 금요일까지 검증표를 보내겠습니다.",
    )
    second = _meeting(config, "beta", "청람 밀봉검사의 결과는 다음 주에 공유하겠습니다.")
    distractor = _meeting(config, "other", "식당 점심 메뉴는 금요일에 확정합니다.")
    groups = topic_evidence(config, topic, distractor + first[:1], 6000)
    returned = {parents[0].meeting_id: candidates for parents, candidates in groups}
    assert set(returned) == {first[0].meeting_id, second[0].meeting_id}
    assert {c.quote.text for c in returned["alpha"]} == {
        "청람 밀봉검사는 시제품에 전수 적용하기로 했습니다.",
        "제가 금요일까지 검증표를 보내겠습니다.",
    }
    for parents, candidates in groups:
        assert len({p.meeting_id for p in parents}) == 1
        for candidate in candidates:
            parent = parents[candidate.parent_index]
            assert parent.meeting_id == candidate.meeting_id
            assert candidate.quote.speaker in parent.speakers
            assert parent.start_time <= candidate.quote.start_time <= candidate.quote.end_time
            assert candidate.quote.end_time <= parent.end_time
            assert candidate.quote.text in parent.text


def test_stale_pre_edit_hit_cannot_supply_original_quote(config: AppConfig) -> None:
    """최신 색인 접수가 없는 편집 전 발화를 주제 근거나 후속 일정에 쓰지 않는다."""
    old = _meeting(config, "stale", "은하 건조기를 월요일에 교체하겠습니다.")
    path = config.paths.resolved_checkpoints_dir / "stale" / "correct.json"
    payload = {
        "search_revision": "수정-후",
        "utterances": [_utterance("SPEAKER_00", "교체를 취소했습니다.", 10)],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert not topic_evidence(config, "은하 건조기", old, 4000)


def test_overlong_source_is_never_published_as_an_invented_fragment(config: AppConfig) -> None:
    """작은 입력 예산에 들어가지 않는 긴 발화를 자르거나 예산을 넘겨 선택하지 않는다."""
    long_line = "오로라 윤활 " + '기계 가운데의 "회전축"을 검사합니다. ' * 180
    rows = _meeting(config, "budget", long_line, "오로라 윤활 점검을 완료했습니다.")
    groups = topic_evidence(config, "오로라 윤활", rows, 350)
    assert len(groups) == 1
    _, candidates = groups[0]
    assert [c.quote.text for c in candidates] == ["오로라 윤활 점검을 완료했습니다."]
    payload = [[c.id, c.display_speaker, c.quote.text] for c in candidates]
    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) <= 350


def test_matching_words_unnecessary_for_semantic_candidate(config: AppConfig) -> None:
    """표현이 전혀 다른 벡터 적중도 실제 발화 후보로 유지해 모델이 관련성을 판정한다."""
    rows = _meeting(config, "semantic", "앞으로 불량 부품을 한 곳에 따로 모으기로 했습니다.")
    rows[0].source = "vector"
    groups = topic_evidence(config, "하자품 격리", rows, 4000)
    assert len(groups) == 1
    parents, candidates = groups[0]
    assert candidates[0].quote.text == "앞으로 불량 부품을 한 곳에 따로 모으기로 했습니다."
    assert parents[candidates[0].parent_index].chunk_id == rows[0].chunk_id


def test_incidental_word_match_does_not_displace_top_semantic_meeting(config: AppConfig) -> None:
    """단어 하나만 겹친 다른 회의가 첫 의미 검색의 동의어 회의를 밀어내지 않는다."""
    semantic = _meeting(config, "parts", "불량 부품은 창고 한쪽에 따로 모아 두기로 했습니다.")
    semantic[0].source = "vector"
    _meeting(config, "medical", "감염자 격리 절차를 다음 주에 점검합니다.")
    groups = topic_evidence(config, "하자품 격리", semantic, 4000)
    by_meeting = {parents[0].meeting_id: turns for parents, turns in groups}
    assert by_meeting.keys() == {"parts", "medical"}
    assert (
        by_meeting["parts"][0].quote.text == "불량 부품은 창고 한쪽에 따로 모아 두기로 했습니다."
    )


def test_distinctive_agenda_limits_shared_generic_words(config: AppConfig) -> None:
    """여러 회의의 공통 표현만으로는 특정 안건의 결정을 다른 회의에 배정하지 않는다."""
    first = _meeting(config, "clinic", "창고 반품 은빛 검사에 새 장비를 쓰기로 했습니다.")
    _meeting(config, "shop", "창고 반품 창고 반품 재고를 다시 정리했습니다.")
    _meeting(config, "outlet", "창고 반품 공지를 공유했습니다.")
    groups = topic_evidence(config, "창고 반품 은빛", first, 4000)
    assert [parents[0].meeting_id for parents, _ in groups] == ["clinic"]


def test_consecutive_words_identify_a_named_process(config: AppConfig) -> None:
    """같은 네 단어가 별개로 등장해도 연속된 업무명과 일치한 회의를 우선한다."""
    first = _meeting(config, "logistics", "반품 접수 사유 코드 입력 방식이 바뀌었습니다.")
    _meeting(config, "misc", "코드 검증과 사유 작성, 접수 기준, 반품 물량을 확인했습니다.")
    groups = topic_evidence(config, "반품 접수 사유 코드", first, 4000)
    assert [parents[0].meeting_id for parents, _ in groups] == ["logistics"]
