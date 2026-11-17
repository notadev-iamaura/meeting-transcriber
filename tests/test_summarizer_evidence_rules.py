"""요약 예방 규칙의 전 경로 전달과 실측 생성 상한 재시도 경계를 검증한다."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.llm_backend import LLMGenerationError
from steps.corrector import CorrectedUtterance
from steps.summarizer import (
    _CHUNK_SUMMARY_PROMPT,
    _FALLBACK_SUMMARIZER_PROMPT,
    _SUMMARY_EVIDENCE_RULES,
    Summarizer,
    SummaryError,
)

CUSTOM = "# 사용자 지정 서식\n요약은 두 항목으로 작성하고 원문의 표현을 유지하세요.  "


def _summarizer(monkeypatch: pytest.MonkeyPatch) -> Summarizer:
    """사용자 저장소·모델 매니저에 접근하지 않는 요약기를 만든다."""
    summarizer = Summarizer.__new__(Summarizer)
    summarizer._config = SimpleNamespace(llm=SimpleNamespace(summarize_max_tokens=1200))
    monkeypatch.setattr("steps.summarizer.build_summarizer_system_prompt", lambda: CUSTOM)
    return summarizer


def _utterance(text: str, speaker: str) -> CorrectedUtterance:
    """그대로 보존해야 하는 짧은 가상 발화를 만든다."""
    return CorrectedUtterance(text, text, speaker, 0, 2)


def test_single_keeps_user_format_and_adds_general_evidence_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """사용자 프롬프트·전사문·화자 집합을 바꾸지 않고 예방 지시만 덧붙인다."""
    summarizer = _summarizer(monkeypatch)
    backend = SimpleNamespace(chat=MagicMock(return_value="# 사용자 지정 서식\n가상 요약"))
    transcript = "[SPEAKER_01] 조건이 확인되면 진행할 수 있습니다.\n[UNKNOWN] 단위는 불명확합니다."
    result = summarizer._summarize_single(backend, transcript, ["SPEAKER_01", "UNKNOWN"])
    messages = backend.chat.call_args.kwargs["messages"]
    assert messages[0]["content"].startswith(CUSTOM)
    assert messages[0]["content"].count(_SUMMARY_EVIDENCE_RULES) == 1
    assert transcript in messages[1]["content"]
    assert "참석자: SPEAKER_01, UNKNOWN" in messages[1]["content"]
    for rule in (
        "화자 집합 밖",
        "UNKNOWN",
        "호명된 사람",
        "요청자",
        "지연 가능성",
        "정정된 기준",
        "분모",
        "지역별",
        "측정 방법별",
        "영문 풀네임",
    ):
        assert rule in messages[0]["content"]
    assert result == "# 사용자 지정 서식\n가상 요약"
    assert backend.chat.call_count == 1


def test_chunk_and_merge_share_rules_without_replacing_user_final_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """부분 요약 두 개와 최종 통합의 실제 호출에 같은 근거 지시가 한 번씩 들어간다."""
    summarizer = _summarizer(monkeypatch)
    backend = SimpleNamespace(
        chat=MagicMock(side_effect=["부분 하나", "부분 둘", "사용자 최종 서식"])
    )
    chunks = [
        [_utterance("조건부 작업입니다.", "SPEAKER_01")],
        [_utterance("수치는 불명확합니다.", "UNKNOWN")],
    ]
    result = summarizer._summarize_chunked(backend, chunks, ["SPEAKER_01", "UNKNOWN"])
    calls = backend.chat.call_args_list
    assert len(calls) == 3
    assert calls[0].kwargs["messages"][0]["content"].startswith(_CHUNK_SUMMARY_PROMPT)
    assert calls[-1].kwargs["messages"][0]["content"].startswith(CUSTOM)
    assert all(
        call.kwargs["messages"][0]["content"].count(_SUMMARY_EVIDENCE_RULES) == 1 for call in calls
    )
    assert "부분 하나" in calls[-1].kwargs["messages"][1]["content"]
    assert "부분 둘" in calls[-1].kwargs["messages"][1]["content"]
    assert result == "사용자 최종 서식"


def test_prompt_store_failure_keeps_existing_fallback_format_with_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """프롬프트 저장소 오류 경로에서도 기존 서식을 교체하거나 지시를 빠뜨리지 않는다."""
    summarizer = _summarizer(monkeypatch)
    monkeypatch.setattr(
        "steps.summarizer.build_summarizer_system_prompt",
        MagicMock(side_effect=OSError("fixture")),
    )
    prompt = summarizer._load_system_prompt()
    assert prompt.startswith(_FALLBACK_SUMMARIZER_PROMPT)
    assert prompt.count(_SUMMARY_EVIDENCE_RULES) == 1
    backend = SimpleNamespace(chat=MagicMock(return_value="폴백 프롬프트로 작성한 요약"))
    summarizer._summarize_single(backend, "[UNKNOWN] 원문", ["UNKNOWN"])
    assert backend.chat.call_args.kwargs["messages"][0]["content"] == prompt


def test_failed_chunk_keeps_original_context_and_merge_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """부분 실패의 원문을 통합 입력에 보존하고 근거 규칙을 계속 적용한다."""
    summarizer = _summarizer(monkeypatch)
    backend = SimpleNamespace(
        chat=MagicMock(side_effect=["정상 부분", LLMGenerationError("fixture"), "최종 요약"])
    )
    chunks = [
        [_utterance("정상 원문", "SPEAKER_01")],
        [_utterance("원문의 조건은 그대로입니다.", "UNKNOWN")],
    ]
    result = summarizer._summarize_chunked(backend, chunks, ["SPEAKER_01", "UNKNOWN"])
    merge = backend.chat.call_args_list[-1].kwargs["messages"]
    assert "(요약 실패 — 원본)" in merge[1]["content"]
    assert "[UNKNOWN] 원문의 조건은 그대로입니다." in merge[1]["content"]
    assert merge[0]["content"].startswith(CUSTOM)
    assert _SUMMARY_EVIDENCE_RULES in merge[0]["content"]
    assert result == "최종 요약"


@pytest.mark.parametrize(
    "metrics",
    [
        None,
        {},
        [],
        {"generation_tokens": None},
        {"generation_tokens": "1200"},
        {"generation_tokens": True},
        {"generation_tokens": float("nan")},
        {"generation_tokens": 1199},
    ],
)
def test_no_measured_cap_does_not_retry_or_guess_from_sentence_ending(
    monkeypatch: pytest.MonkeyPatch, metrics: object
) -> None:
    """미측정·정상 실측은 문장 끝 모양과 무관하게 기존 한 번 호출을 유지한다."""
    summarizer = _summarizer(monkeypatch)
    backend = SimpleNamespace(
        chat=MagicMock(return_value="문장 중간처럼 보이는 시"),
        get_generation_metrics=MagicMock(return_value=metrics),
    )
    assert summarizer._call_llm(backend, CUSTOM, "원문") == "문장 중간처럼 보이는 시"
    assert backend.chat.call_count == 1


@pytest.mark.parametrize("failure", [RuntimeError, ValueError, TypeError, AttributeError, OSError])
def test_metrics_failure_is_unmeasured_and_does_not_add_generation(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    """선택적인 통계 조회 실패를 상한 도달로 만들어 재요청하지 않는다."""
    summarizer = _summarizer(monkeypatch)
    backend = SimpleNamespace(
        chat=MagicMock(return_value="완결."),
        get_generation_metrics=MagicMock(side_effect=failure("fixture")),
    )
    assert summarizer._call_llm(backend, CUSTOM, "원문") == "완결."
    assert backend.chat.call_count == 1


def test_measured_cap_retries_once_with_same_limit_source_and_user_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """출력 상한 실측 때만 원문·형식을 유지한 간결한 요청을 같은 상한으로 재시도한다."""
    summarizer = _summarizer(monkeypatch)
    backend = SimpleNamespace(
        chat=MagicMock(side_effect=["잘린 요약", "완결된 요약."]),
        get_generation_metrics=MagicMock(
            side_effect=[{"generation_tokens": 1200}, {"generation_tokens": 800}]
        ),
    )
    original = "참석자: UNKNOWN\n원문에는 조건이 포함돼 있습니다."
    assert summarizer._call_llm(backend, CUSTOM, original) == "완결된 요약."
    first, second = backend.chat.call_args_list
    assert first.kwargs["max_tokens"] == second.kwargs["max_tokens"] == 1200
    assert first.kwargs["messages"][0] == second.kwargs["messages"][0]
    assert second.kwargs["messages"][0]["content"].startswith(CUSTOM)
    assert second.kwargs["messages"][1]["content"].startswith(original)
    assert "모든 문장을 끝까지 완결" in second.kwargs["messages"][1]["content"]
    assert backend.chat.call_count == 2


def test_second_measured_cap_is_explicit_failure_without_third_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """짧게 재시도해도 실측 상한에 도달하면 잘린 내용을 정상 요약으로 반환하지 않는다."""
    summarizer = _summarizer(monkeypatch)
    backend = SimpleNamespace(
        chat=MagicMock(return_value="잘린 내용"),
        get_generation_metrics=MagicMock(
            side_effect=[{"generation_tokens": 1201}, {"generation_tokens": 1200}]
        ),
    )
    with pytest.raises(SummaryError, match="재시도 후에도 생성 토큰 상한"):
        summarizer._call_llm(backend, CUSTOM, "원문")
    assert backend.chat.call_count == 2


def test_capped_chunk_uses_explicit_original_fallback_not_partial_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """부분 요약의 반복 상한도 기존 실패 폴백으로 원문을 보존한다."""
    summarizer = _summarizer(monkeypatch)
    backend = SimpleNamespace(
        chat=MagicMock(return_value="잘린 부분"),
        get_generation_metrics=MagicMock(return_value={"generation_tokens": 1200}),
    )
    result = summarizer._summarize_chunked(
        backend, [[_utterance("원본 발화입니다.", "UNKNOWN")]], ["UNKNOWN"]
    )
    assert backend.chat.call_count == 2
    assert "AI 요약 실패" in result and "원본 발화입니다." in result
    assert "잘린 부분" not in result


@pytest.mark.parametrize("failure", ["measured_cap", "generation_error"])
def test_merge_failure_shows_all_original_speakers_not_unverified_partial_claims(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """부분 요약의 부정·담당자·전달 대상 오류가 통합 실패 결과로 노출되지 않는다."""
    summarizer = _summarizer(monkeypatch)
    utterances = [
        CorrectedUtterance(
            "초안은 삭제되지 않았습니다.", "초안은 삭제되지 않았습니다.", "SPEAKER_01", 12, 15
        ),
        CorrectedUtterance(
            "제가 표지를 점검하겠습니다.", "제가 표지를 점검하겠습니다.", "SPEAKER_02", 25, 28
        ),
        CorrectedUtterance(
            "자료는 SPEAKER_02에게 전달하겠습니다.",
            "자료는 SPEAKER_02에게 전달하겠습니다.",
            "SPEAKER_03",
            35,
            39,
        ),
    ]
    chunks = [[utterance] for utterance in utterances]
    speakers = ["SPEAKER_01", "SPEAKER_02", "SPEAKER_03"]
    originals = [(u.text, u.speaker, u.start, u.end) for u in utterances]
    incorrect_partials = [
        "초안을 삭제했다.",
        "SPEAKER_03이 표지 점검을 완료했다.",
        "SPEAKER_03이 자기 자신에게 자료를 전달했다.",
    ]
    metrics = [{"generation_tokens": 100} for _ in chunks]
    if failure == "measured_cap":
        responses = [*incorrect_partials, "미완성 통합 요약", "다시 미완성 통합 요약"]
        metrics.extend([{"generation_tokens": 1200}, {"generation_tokens": 1200}])
    else:
        responses = [*incorrect_partials, LLMGenerationError("fixture")]
    backend = SimpleNamespace(
        chat=MagicMock(side_effect=responses),
        get_generation_metrics=MagicMock(side_effect=metrics),
    )
    result = summarizer._summarize_chunked(backend, chunks, speakers)
    assert "AI 요약 실패 — 원본 전사문을 첨부합니다" in result
    assert f"참석자: {', '.join(speakers)}" in result
    assert [line for line in result.splitlines() if line.startswith("- **[")] == [
        f"- **[{speaker}]** {text}" for text, speaker, _start, _end in originals
    ]
    assert all(incorrect not in result for incorrect in incorrect_partials)
    assert "미완성 통합 요약" not in result and "### 파트" not in result
    assert [(u.text, u.speaker, u.start, u.end) for u in utterances] == originals
    assert backend.chat.call_count == (5 if failure == "measured_cap" else 4)
