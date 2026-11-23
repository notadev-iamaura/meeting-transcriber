"""회의 주제 문단의 연속성과 한국어 요약의 실제 근거를 검증한다."""

from __future__ import annotations

import json

from search.source_answers import SourceCandidate, SourceQuote
from search.topic_passages import (
    Passage,
    _aligned_quote,
    _locate_quote,
    _read_fields,
    _source_summary,
    passage_payload,
    read_passage_brief,
)


def turn(i: int, text: str) -> SourceCandidate:
    return SourceCandidate(i, 0, i, "meeting", SourceQuote(text, "A", i, i + 1), "참석자 1")


def test_discussion_starts_at_topic_and_keeps_its_following_actions() -> None:
    rows = [
        turn(0, "다른 작업은 오늘 오후에 처리하겠습니다."),
        turn(1, "다음 안건은 안내판 대비입니다."),
        turn(2, "고대비 견본과 기존 표지를 비교한 뒤에"),
        turn(3, "도면을 디자인팀에 전달하겠습니다."),
        turn(4, "다음 주 화요일에 설치하겠습니다."),
        turn(15, "시설 단가를 조정하겠습니다."),
    ]
    prompt, groups = passage_payload("안내판 대비", rows, 900)
    assert len(prompt) <= 900 and groups[0].turns == tuple(rows[1:5])
    assert rows[0].quote.text not in prompt and rows[-1].quote.text not in prompt
    assert rows[3].quote.text in prompt and rows[4].quote.text in prompt


def test_vernacular_topic_keeps_later_followup_until_next_agenda() -> None:
    rows = [turn(1, "동부 창고 포장 파손을 논의하겠습니다.")]
    rows += [turn(i, "이 수치를 살펴봤습니다.") for i in range(2, 26)]
    rows[10] = turn(11, "파손율이 높아진 원인을 찾고 있습니다.")
    rows[20] = turn(21, "이후에도 파손이 이어졌습니다.")
    rows += [
        turn(26, "그래서 이번 주 안에 조사를 끝내려고 합니다."),
        turn(27, "그리고 이제 저희가 채용하는 게 주요한 과제인데요."),
        turn(28, "내일 공고를 올리겠습니다."),
    ]
    prompt, _ = passage_payload("동부 창고 포장 파손이랑 재발", rows, 5000)
    assert rows[-3].quote.text in prompt and rows[-1].quote.text not in prompt


def test_reader_uses_concise_supported_korean_and_exact_time() -> None:
    a = turn(7, "네, 회의용 안내판을 입구에 설치하겠습니다.")
    b = turn(8, "금요일은 업체 희망일이고, 실제 설치는 다음 주입니다.")
    _, groups = passage_payload("안내판", [a, b], 800)
    raw = json.dumps(
        {
            "topic": {"ids": [7], "quote": "안내판을 입구에"},
            "decision": None,
            "action": {
                "text": "안내판을 입구에 설치하겠습니다.",
                "ids": [7],
                "quote": "입구에 설치하겠습니다.",
            },
            "schedule": {
                "text": "금요일에 설치합니다.",
                "ids": [8],
                "quote": "실제 설치는 다음 주입니다.",
            },
        },
        ensure_ascii=False,
    )
    result = read_passage_brief("안내판", raw, groups)
    assert result is not None and result.facts["action"].text == "안내판을 입구에 설치하겠습니다."
    if "schedule" in result.facts:
        assert result.facts["schedule"].text == b.quote.text


def test_reader_cannot_join_quoted_reference_from_another_passage() -> None:
    a = turn(1, "안내판의 대비를 논의했습니다.")
    b = turn(28, "재고는 한 달 후에 평가하려고 합니다.")
    groups = [Passage((a,), 3), Passage((b,), 0)]
    result = read_passage_brief(
        "안내판",
        json.dumps(
            {
                "topic": {"ids": [1]},
                "action": {"ids": [1, 28], "quote": "재고"},
                "schedule": {"ids": [28], "quote": b.quote.text},
            },
            ensure_ascii=False,
        ),
        groups,
    )
    assert result and "action" not in result.facts and "schedule" not in result.facts
    assert read_passage_brief("안내판", '{"topic":null}', groups) is None


def test_summary_refuses_new_numbers_missing_condition_and_ambiguous_status() -> None:
    for original, claim in [
        ("자료를 주시면 3일 정도 걸릴 것 같습니다.", "3일 안에 완료합니다."),
        ("금요일은 요청이고 실제 발송일은 다음 주입니다.", "금요일에 발송합니다."),
        ("회의 일정은 6일 오전입니다.", "회의 일정은 8일 오전입니다."),
        ("출시를 확정하지 않았습니다.", "출시를 확정했습니다."),
    ]:
        assert _source_summary(claim, (turn(0, original),)) == original


def test_secondary_subtask_with_cancelled_agreement_is_not_a_decision() -> None:
    rows = [
        turn(0, "환승 교육 기준을 정리합니다."),
        turn(1, "원본 로그를 조회하기로 했는데 담당자가 안 오셨네요."),
        turn(2, "환승 교육 기준으로 표본부터 확인할게요."),
    ]
    groups = [Passage(tuple(rows), 3)]
    result = read_passage_brief(
        "환승 교육 기준",
        json.dumps({"topic": {"ids": [0]}, "decision": {"ids": [1]}, "schedule": {"ids": [1]}}),
        groups,
    )
    assert result and all(rows[1] not in fact.evidence for fact in result.facts.values())


def test_valid_ids_recover_only_real_quote_and_multiple_supported_actions() -> None:
    rows = [turn(0, "조명 견본을 전달하겠습니다."), turn(1, "조명 위치를 비교하겠습니다.")]
    groups = [Passage(tuple(rows), 3)]
    raw = json.dumps(
        {
            "topic": {"ids": [0]},
            "action": [
                {"ids": [0], "text": "하자 처리를 완료했습니다.", "quote": "없는 문장"},
                {"ids": [1], "text": "조명 위치를 비교하겠습니다.", "quote": "위치를 비교"},
            ],
        },
        ensure_ascii=False,
    )
    result = read_passage_brief("조명", raw, groups)
    assert result and result.facts["action"].evidence == tuple(rows)
    assert "하자" not in result.facts["action"].text


def test_multi_turn_claim_can_shorten_polite_language_without_losing_scope() -> None:
    rows = (
        turn(0, "저희가 회의용 안내판을 입구에 설치하고"),
        turn(1, "제가 안내판 도면을 비교하겠습니다."),
    )
    short = "안내판을 입구에 설치하고 안내판 도면을 비교하겠습니다."
    assert _source_summary(short, rows) == short
    assert "창가" not in _source_summary("안내판을 창가에 설치하고 도면을 비교하겠습니다.", rows)


def test_anaphoric_promise_and_target_date_survive_a_continuous_discussion() -> None:
    rows = [
        turn(0, "전시장 조도 측정 계획입니다."),
        turn(1, "지난번에 입구에서 한번 쟀습니다."),
        turn(2, "수치를 다시 살펴봐 주세요."),
        turn(3, "네, 알겠습니다."),
        turn(4, "제가 조사해서 전달하겠습니다."),
        turn(5, "언제쯤 공유해 주실 수 있을까요?"),
        turn(6, "다음 주 수요일까지 공유하겠습니다."),
    ]
    data = json.dumps({"topic": {"ids": [0]}, "action": {"ids": [4]}, "schedule": {"ids": [6]}})
    result = read_passage_brief("전시장 조도", data, [Passage(tuple(rows), 3)])
    assert result and rows[4] in result.facts["action"].evidence
    assert rows[6] in result.facts["schedule"].evidence
    rows[2] = turn(2, "창고 임대료는 별도로 살펴봐 주세요.")
    changed = read_passage_brief("전시장 조도", data, [Passage(tuple(rows), 3)])
    assert changed and not any(
        rows[4] in f.evidence or rows[6] in f.evidence for f in changed.facts.values()
    )


def test_korean_lines_map_to_verified_transcript_and_old_json_still_works() -> None:
    rows = (
        turn(1, "행사장 대피 안내판입니다."),
        turn(2, "행사장 대피 안내판 도면을"),
        turn(3, "소방팀에 전달하겠습니다."),
    )
    groups = [Passage(rows, 3)]
    quote = "행사장 대피 안내판 도면을 소방팀에 전달하겠습니다."
    for raw in (
        f'주제: "{rows[0].quote.text}"\n결정: 없음\n후속 작업: (78) "{quote}"\n일정: 없음',
        json.dumps(
            {
                "topic": {"ids": [1]},
                "action": {
                    "ids": [1],
                    "quote": quote,
                    "text": "이름 없는 외부 업체에 보냈습니다.",
                },
            }
        ),
    ):
        result = read_passage_brief("행사장 대피 안내판", raw, groups)
        assert result and result.facts["action"].evidence == rows[1:]
        assert "외부 업체" not in result.facts["action"].text


def test_label_quote_is_unique_and_never_changes_dates_names_amounts_or_polarity() -> None:
    source = "연준님께 3.1만 원은 화요일까지 보내지 않습니다."
    same = [Passage((turn(2, source), turn(3, source)), 3)]
    assert not _locate_quote(source, same)
    groups = [Passage((turn(2, source),), 3)]
    for phrase in ("연주님", "31만", "금요일", "보내겠습니다"):
        changed = source.replace(
            {
                "연주님": "연준님",
                "31만": "3.1만",
                "금요일": "화요일",
                "보내겠습니다": "보내지 않습니다",
            }[phrase],
            phrase,
        )
        assert not _locate_quote(changed, groups)
    assert _read_fields("후속 작업: 없음")[0] == {"action": None}
    assert _read_fields("출력 형식 오류") == ({}, "invalid")


def test_spoken_filler_may_differ_on_long_quote_while_negation_stays_intact() -> None:
    text = "안내판 변경 안내문을 음, 다음 주 수요일까지 담당자에게 공유하겠습니다."
    row = turn(1, text)
    groups = [Passage((row,), 3)]
    assert _locate_quote(text.replace("음, ", ""), groups) == (row,)
    assert not _locate_quote(
        text.replace("음, ", "").replace("공유하겠습니다", "공유하지 않겠습니다"), groups
    )


def test_abbreviated_markdown_quote_finds_unique_consecutive_originals() -> None:
    a = turn(1, "안내판 도면을 어제 접수한 서류와 비교하고")
    b = turn(2, "금요일에 수진님께 다시 한번 전달하겠습니다.")
    quote = "안내판 도면을 어제 접수한 서류와 비교하고 금요일에 수진님께 전달하겠습니다."
    groups = [Passage((a, b), 3)]
    assert _aligned_quote(quote, groups) == (a, b)
    raw = f"### **주제 확인**\n- 안내판 도면을 어제 접수한 서류와 비교하고\n**후속 작업**\n  - “{quote}”\n### 일정: 없음"
    answer = read_passage_brief("안내판", raw, groups)
    assert answer and answer.facts["action"].evidence == (a, b)
    assert "다시 한번" in answer.facts["action"].text
    assert not _aligned_quote(
        quote, [*groups, Passage((turn(15, a.quote.text), turn(16, b.quote.text)), 3)]
    )
    for before, after in (
        ("금요일", "월요일"),
        ("수진님께", "지선님께"),
        ("수진님께", "수진님께서"),
        ("어제 ", ""),
        ("전달하겠습니다", "전달하지 않겠습니다"),
    ):
        assert not _aligned_quote(quote.replace(before, after), groups)


def test_abbreviated_quote_cannot_remove_condition_or_exclusivity() -> None:
    src = "업체 승인이 되면 샘플 3개만 담당자에게 전달하겠습니다."
    groups = [Passage((turn(1, src),), 3)]
    for wrong in (
        "샘플 3개만 담당자에게 전달하겠습니다.",
        "업체 승인이 되면 샘플 3개 담당자에게 전달하겠습니다.",
        "업체 승인이 되면 샘플 2개만 담당자에게 전달하겠습니다.",
    ):
        assert not _aligned_quote(wrong, groups)


def test_later_explicit_correction_keeps_both_sources_in_the_same_discussion() -> None:
    from search.topic_passages import _later_role_correction

    old = turn(10, "운송표의 ZA가 창고이고 ZB가 공장입니다.")
    new = turn(18, "ZA가 공장이고 ZB가 창고입니다.")
    marker = turn(20, "거꾸로 말했네요.")
    assert _later_role_correction(old, [Passage((old, new, marker), 1)]) == new
    assert _later_role_correction(old, [Passage((old, new), 1)]) is None
    assert _later_role_correction(old, [Passage((old,), 1), Passage((new, marker), 1)]) is None
