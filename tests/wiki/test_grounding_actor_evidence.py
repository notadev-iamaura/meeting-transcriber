"""인용 근거의 실제 화자·시간·단일 발화 경계를 보존하는 회귀 테스트."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from config import WikiGroundingConfig
from core.wiki.citation_verifier import CheckpointCitationVerifier, UtterancesCitationVerifier
from core.wiki.grounding import LocalClaimGrounder

_MARKER = "[meeting:actor_sample@00:00:10]"


def _page(claim: str = "검토 중이다.") -> str:
    """실제 사용자 자료가 없는 단일 인용 주장을 만든다."""
    return f"---\ntype: action_items\n---\n{claim} {_MARKER}\n"


class ActorJudge:
    """구조 정보와 판정 계약만 검증하며 의미 정확도를 보증하지 않는 모델 대역."""

    model_name = "test-local"

    def __init__(self, *, verdict: str = "supported", quote: str | None = None) -> None:
        """입력과 고정 판정을 메모리에 보관한다."""
        self.calls: list[dict[str, Any]] = []
        self.verdict = verdict
        self.quote = quote

    async def generate(self, **kwargs: Any) -> str:
        """검수에 제공된 실제 첫 segment를 인용하거나 지정한 위조 인용을 반환한다."""
        self.calls.append(kwargs)
        claims = json.loads(kwargs["user_prompt"])["claims"]
        return json.dumps(
            {
                "results": [
                    {
                        "id": claim["id"],
                        "verdict": self.verdict,
                        "quote": self.quote
                        if self.quote is not None
                        else claim["evidence"][0]["segments"][0]["text"],
                    }
                    for claim in claims
                ]
            },
            ensure_ascii=False,
        )


async def test_동일초의_같은문장도_서로다른_화자와_시간경계를_보존한다() -> None:
    """legacy text는 중복 제거를 유지하고 구조 근거는 각 발화 정보를 잃지 않는다."""
    source = UtterancesCitationVerifier(
        {
            "actor_sample": [
                dict(start=10.1, end=10.4, text="네.", speaker="SPEAKER_00"),
                dict(start=10.6, end=11.0, text="네.", speaker="SPEAKER_01"),
            ]
        }
    )
    evidence = await source.fetch_evidence("actor_sample", 10)
    assert evidence == {
        "meeting_id": "actor_sample",
        "text": "네.",
        "segments": [
            dict(start=10.1, end=10.4, text="네.", speaker_id="SPEAKER_00"),
            dict(start=10.6, end=11.0, text="네.", speaker_id="SPEAKER_01"),
        ],
    }
    assert await source.fetch_utterance("actor_sample", 10) == "네."


async def test_화자번호는_회의별로_구분하고_인접답변을_자동추가하지_않는다() -> None:
    """같은 번호를 전역 인물로 묶거나 지정하지 않은 주변 답변을 근거에 넣지 않는다."""
    source = UtterancesCitationVerifier(
        {
            "actor_sample": [
                dict(start=10, end=11, text="검토하셨나요?", speaker="SPEAKER_00"),
                dict(start=12, end=13, text="완료했습니다.", speaker="SPEAKER_01"),
            ],
            "other_meeting": [dict(start=10, end=11, text="다른 회의 발언", speaker="SPEAKER_00")],
        }
    )
    one = await source.fetch_evidence("actor_sample", 10)
    other = await source.fetch_evidence("other_meeting", 10)
    assert one["meeting_id"] != other["meeting_id"]
    assert one["segments"][0]["speaker_id"] == other["segments"][0]["speaker_id"]
    assert len(one["segments"]) == 1 and one["text"] == "검토하셨나요?"


async def test_과거_checkpoint도_원래_화자를_유지하고_누락된_화자는_추정하지_않는다(
    tmp_path: Path,
) -> None:
    """역사 원장 파싱 과정이 speaker를 버리거나 임의 라벨로 채우지 않는다."""
    directory = tmp_path / "actor_sample"
    directory.mkdir()
    (directory / "correct.json").write_text(
        json.dumps(
            {
                "utterances": [
                    dict(start=10, end=11, text="확인하겠습니다.", speaker="SPEAKER_03"),
                    dict(start=20, end=21, text="예산은 미정입니다."),
                ]
            }
        )
    )
    source = CheckpointCitationVerifier(checkpoints_dir=tmp_path)
    assert (await source.fetch_evidence("actor_sample", 10))["segments"][0][
        "speaker_id"
    ] == "SPEAKER_03"
    assert (await source.fetch_evidence("actor_sample", 20))["segments"][0]["speaker_id"] is None


@pytest.mark.parametrize(
    "source_text,claim,verdict,expected",
    [
        (
            "제가 해소할 일이 있으면 알려주시면 지원하겠습니다.",
            "- [ ] SPEAKER_00: 검토와 공유를 수행하기로 확정했다.",
            "insufficient",
            False,
        ),
        (
            "제가 해당 업무를 검토하고 공유하겠습니다.",
            "- [ ] SPEAKER_00: 해당 업무를 검토하고 공유하기로 했다.",
            "supported",
            True,
        ),
        (
            "제가 해소할 일이 있으면 알려주시면 지원하겠습니다.",
            "SPEAKER_00이 필요한 일이 생기면 지원하겠다고 제안했다.",
            "supported",
            True,
        ),
    ],
)
async def test_조건부지원_요청대상_자기수행약속을_구분할_구조를_모델에_전달한다(
    source_text: str, claim: str, verdict: str, expected: bool
) -> None:
    """조건어 전역 차단 없이 모델이 역할과 주장 강도를 별도 판정할 수 있어야 한다."""
    source = UtterancesCitationVerifier(
        {"actor_sample": [dict(start=10, end=12, text=source_text, speaker="SPEAKER_00")]}
    )
    llm = ActorJudge(verdict=verdict)
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(claim), source)
    assert result.passed is expected
    call = llm.calls[0]
    evidence = json.loads(call["user_prompt"])["claims"][0]["evidence"][0]
    assert evidence["meeting_id"] == "actor_sample"
    expected_segment: dict[str, Any] = dict(
        speaker_id="SPEAKER_00", start=10.0, end=12.0, text=source_text, segment_index=0
    )
    if claim.startswith("- [ ] SPEAKER_00:"):
        expected_segment["quote_actor_matches"] = True
    assert evidence["segments"] == [expected_segment]
    assert "발화자가 업무 담당자라는 뜻은 아닙니다" in call["system_prompt"]
    assert "조건부 지원으로 쓰는 주장은 지지될 수 있습니다" in call["system_prompt"]


async def test_다른_화자의_발화를_합친_quote는_원문인용으로_통과하지_않는다() -> None:
    """동일 초의 결합 text에 존재하더라도 실제 단일 segment에 없는 인용은 거부한다."""
    source = UtterancesCitationVerifier(
        {
            "actor_sample": [
                dict(start=10.1, end=10.4, text="검토하셨나요?", speaker="SPEAKER_00"),
                dict(start=10.6, end=11, text="완료했습니다.", speaker="SPEAKER_01"),
            ]
        }
    )
    llm = ActorJudge(quote="검토하셨나요? 완료했습니다.")
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(), source)
    assert (
        result.reason == "grounding_unavailable" and result.diagnostic == "quote_not_in_evidence"
    )
    positive = await LocalClaimGrounder(
        ActorJudge(quote="완료했습니다."), WikiGroundingConfig()
    ).verify(_page(), source)
    assert positive.passed


def _evidence() -> dict[str, Any]:
    """구조 검증 실패를 위한 정상 기준 근거를 만든다."""
    return dict(
        meeting_id="actor_sample",
        text="검토 중입니다.",
        segments=[dict(speaker_id="SPEAKER_00", start=10.0, end=11.0, text="검토 중입니다.")],
    )


@pytest.mark.parametrize(
    "field,value",
    [("meeting_id", "other_meeting"), ("segments", []), ("text", "원장과 다른 텍스트")],
)
async def test_잘못된_구조근거를_기존_text조회로_조용히_대체하지_않는다(
    field: str, value: Any
) -> None:
    """회의 범위·발화 누락·원문 불일치는 검증 불가로 보류한다."""
    raw = _evidence()
    raw[field] = value

    class BrokenSource:
        async def fetch_evidence(self, *_args: Any) -> dict[str, Any]:
            """잘못된 구조를 반환한다."""
            return raw

        async def fetch_utterance(self, *_args: Any) -> str:
            """구조 오류 뒤 text fallback을 시도하면 테스트를 실패시킨다."""
            raise AssertionError("구조 오류를 text로 대체하면 안 됩니다")

    llm = ActorJudge()
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(), BrokenSource())
    assert result.reason == "grounding_unavailable" and result.diagnostic == "invalid_response"
    assert not llm.calls


@pytest.mark.parametrize(
    "field,value", [("start", True), ("end", float("nan")), ("speaker_id", 7)]
)
async def test_구조근거의_화자와_시간_타입을_검증한다(field: str, value: Any) -> None:
    """비정상 원장 필드를 화자·시각 정보로 모델에 보내지 않는다."""
    raw = deepcopy(_evidence())
    raw["segments"][0][field] = value

    class Source:
        async def fetch_evidence(self, *_args: Any) -> dict[str, Any]:
            """잘못된 단일 발화 구조를 반환한다."""
            return raw

        async def fetch_utterance(self, *_args: Any) -> str:
            """레거시 메서드는 호출하지 않는다."""
            raise AssertionError

    llm = ActorJudge()
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(), Source())
    assert not result.passed and not llm.calls


async def test_추가된_화자구조도_기존_근거_크기예산에_포함한다() -> None:
    """원문이 짧아도 구조를 무제한으로 붙이거나 잘라서 통과시키지 않는다."""
    source = UtterancesCitationVerifier(
        {"actor_sample": [dict(start=10, end=11, text="검토 중입니다.", speaker="A" * 400)]}
    )
    llm = ActorJudge()
    result = await LocalClaimGrounder(llm, WikiGroundingConfig(max_item_chars=256)).verify(
        _page(), source
    )
    assert result.reason == "grounding_limit" and result.diagnostic == "evidence_limit"
    assert not llm.calls


async def test_구조근거_조회중_취소를_게시성공으로_바꾸지_않는다() -> None:
    """원장 조회 대기도 기존 취소 전파 계약을 유지한다."""
    entered = asyncio.Event()

    class Source:
        async def fetch_evidence(self, *_args: Any) -> None:
            """실제 모델 없이 구조 조회를 대기시킨다."""
            entered.set()
            await asyncio.Event().wait()

        async def fetch_utterance(self, *_args: Any) -> str:
            """레거시 메서드는 호출하지 않는다."""
            raise AssertionError

    task = asyncio.create_task(
        LocalClaimGrounder(ActorJudge(), WikiGroundingConfig()).verify(_page(), Source())
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.native
async def test_실제_캐시모델이_조건부지원과_명시적자기수행약속을_구분한다() -> None:
    """오프라인 캐시 모델로 합성 세 사례를 검사하며 실제 회의는 사용하지 않는다.

    실제 처리 서버와 함께 실행하지 않는다. HF 캐시가 없으면 건너뛰고 repo ID
    대신 이미 존재하는 snapshot 경로만 로드한다. 응답 진단도 합성 사례에 한정한다.
    """
    import os

    if os.environ.get("HF_HUB_OFFLINE") != "1" or os.environ.get("TRANSFORMERS_OFFLINE") != "1":
        pytest.skip("HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1의 명시적 오프라인 실행이 필요합니다.")

    from huggingface_hub import try_to_load_from_cache

    from config import AppConfig
    from core.model_manager import ModelLoadManager
    from core.wiki.llm_client import MlxWikiClient

    config = AppConfig()
    cached_config = try_to_load_from_cache(config.wiki.compiler_model, "config.json")
    if not isinstance(cached_config, str):
        pytest.skip("로컬 Wiki 모델 config 캐시가 없어 다운로드 없이 평가를 건너뜁니다.")
    config.wiki.compiler_model = str(Path(cached_config).parent)
    manager = ModelLoadManager()
    delegate = MlxWikiClient(config, manager)

    class ObservedLLM:
        """합성 사례의 실제 모델 응답만 실패 진단에 남긴다."""

        model_name = "cached-local-wiki-actor"

        def __init__(self) -> None:
            """사례별 합성 출력 수집기를 준비한다."""
            self.outputs: list[str] = []

        async def generate(self, **kwargs: Any) -> str:
            """실제 호출을 위임하고 사용자 자료 없는 응답을 수집한다."""
            raw = await delegate.generate(**kwargs)
            self.outputs.append(raw)
            return raw

    cases = [
        (
            "조건부지원제안자의_검토공유확정둔갑",
            "해당 절차를 살펴봐 주세요. 제가 확인하거나 해소할 일이 있으면 알려 주시면 지원하겠습니다.",
            "- [ ] SPEAKER_00: 해당 절차를 검토하고 진행 상황을 공유하기로 확정했다.",
            False,
        ),
        (
            "명시적자기검토와_공유약속",
            "해당 절차는 제가 직접 검토하고 진행 상황을 공유하겠습니다.",
            "- [ ] SPEAKER_00: 해당 절차를 검토하고 진행 상황을 공유하기로 했다.",
            True,
        ),
        (
            "응답연결조각의_복합상세주장_둔갑",
            "네, 그 답변을 받았습니다.",
            "구현 난이도가 낮다고 확인했으며 추가 검토 요청을 접수하고 개발팀이 일정을 검토하기로 했다.",
            False,
        ),
    ]
    failures: list[dict[str, Any]] = []
    try:
        for name, source_text, claim, expected in cases:
            observed = ObservedLLM()
            verifier = UtterancesCitationVerifier(
                {
                    "actor_sample": [
                        dict(start=10.0, end=12.0, speaker="SPEAKER_00", text=source_text),
                        dict(start=13.0, end=14.0, speaker="SPEAKER_01", text="네."),
                    ]
                }
            )
            result = await LocalClaimGrounder(observed, config.wiki.grounding).verify(
                _page(claim), verifier
            )
            # 보류 사례도 JSON/모델 오류가 아닌 의미 검수 결과로 구분됐는지 확인한다.
            semantic_match = result.passed is expected and (
                expected or result.reason == "unsupported_claim"
            )
            if not semantic_match or not observed.outputs:
                failures.append(
                    {
                        "case": name,
                        "expected_pass": expected,
                        "reason": result.reason,
                        "diagnostic": result.diagnostic,
                        "model_outputs": observed.outputs,
                    }
                )
    finally:
        await manager.unload_model()
    assert not failures, json.dumps(failures, ensure_ascii=False)
