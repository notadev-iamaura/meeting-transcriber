"""모델이 거짓 지원 판정을 해도 질문 승격과 임시 화자 오귀속을 막는 회귀."""

from __future__ import annotations

import json
from typing import Any

import pytest

from config import WikiGroundingConfig
from core.wiki.citation_verifier import UtterancesCitationVerifier
from core.wiki.grounding import LocalClaimGrounder

_QUESTION = "[meeting:role_sample@00:00:10]"
_ANSWER = "[meeting:role_sample@00:00:12]"


class AlwaysSupported:
    """잘못된 supported 판정과 실제 원문 quote를 의도적으로 반환한다."""

    model_name = "synthetic-role-judge"

    def __init__(self, quote: str) -> None:
        """사용자 자료가 없는 합성 quote를 보관한다."""
        self.quote = quote
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> str:
        """모든 입력 주장을 무조건 지원된다고 답한다."""
        self.calls.append(kwargs)
        claims = json.loads(kwargs["user_prompt"])["claims"]
        return json.dumps(
            {
                "results": [
                    {"id": claim["id"], "verdict": "supported", "quote": self.quote}
                    for claim in claims
                ]
            },
            ensure_ascii=False,
        )


def _source(
    question: str = "자료를 공유해 주실 수 있나요?",
    answer: str = "네. 제가 자료를 공유하겠습니다.",
    *,
    answer_speaker: str | None = "SPEAKER_01",
) -> UtterancesCitationVerifier:
    """서로 다른 요청자와 수행 약속자의 실제 발화 경계를 만든다."""
    return UtterancesCitationVerifier(
        {
            "role_sample": [
                dict(start=10, end=11, text=question, speaker="SPEAKER_00"),
                dict(start=12, end=13, text=answer, speaker=answer_speaker),
            ]
        }
    )


def _page(claim: str) -> str:
    """가짜 수치나 실제 이름이 없는 단일 주장 문서를 만든다."""
    return f"---\ntype: decision\nstatus: decided\n---\n{claim}\n"


async def test_요청자의_담당번호로_다른사람의_자기약속을_기록하면_거부한다() -> None:
    """확답은 있어도 그 화자와 다른 요청자를 수행 담당자로 붙일 수 없다."""
    quote = "네. 제가 자료를 공유하겠습니다."
    llm = AlwaysSupported(quote)
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(
        _page(f"- SPEAKER_00: 자료를 공유하기로 했다. {_ANSWER}"), _source()
    )
    assert result.reason == "unsupported_claim" and result.diagnostic == "quote_actor_mismatch"
    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    "quote",
    [
        "자료를 공유해 주실 수 있나요?",
        "자료를 공유해주세요.",
        "자료를 공유해 주세요.",
        "자료 공유 부탁드립니다.",
        "자료 공유 부탁하겠습니다.",
        "자료를 공유해 주시면 감사하겠습니다.",
    ],
)
async def test_요청_quote를_합의확정으로_승격한_주장은_거부한다(quote: str) -> None:
    """바로 다음 확답이 있어도 인용되지 않은 답변을 임의 보강하지 않는다."""
    result = await LocalClaimGrounder(AlwaysSupported(quote), WikiGroundingConfig()).verify(
        _page(f"자료를 공유하기로 했다. {_QUESTION}"), _source(question=quote)
    )
    assert result.reason == "unsupported_claim"
    assert result.diagnostic == "quote_request_as_commitment"


@pytest.mark.parametrize(
    "claim",
    [
        "자료 공유를 요청했다.",
        "- SPEAKER_00: 자료 공유를 요청했다.",
        "- SPEAKER_00: 자료 공유 요청",
    ],
)
async def test_요청을_요청으로_서술한_주장은_허용한다(claim: str) -> None:
    """질문 문법을 문서 전체 금지 규칙으로 사용하지 않는다."""
    quote = "자료를 공유해 주실 수 있나요?"
    result = await LocalClaimGrounder(AlwaysSupported(quote), WikiGroundingConfig()).verify(
        _page(f"{claim} {_QUESTION}"), _source()
    )
    assert result.passed


async def test_질문과확답을_함께인용하고_실제답변자를_담당자로쓰면_허용한다() -> None:
    """복수 인용의 질문을 보존하면서 약속 quote와 실제 담당 화자를 대조한다."""
    result = await LocalClaimGrounder(
        AlwaysSupported("네. 제가 자료를 공유하겠습니다."), WikiGroundingConfig()
    ).verify(_page(f"- [ ] SPEAKER_01: 자료를 공유하기로 했다. {_QUESTION} {_ANSWER}"), _source())
    assert result.passed


async def test_이전요청의_구간과겹쳐도_floor일치_약속답변을_정상통과시킨다() -> None:
    """포함구간 첫 발화로 잘못 대조해 올바른 답변 인용을 요청으로 오판하지 않는다."""
    quote = "네. 자료를 전달드리겠습니다."
    source = UtterancesCitationVerifier(
        {
            "role_sample": [
                dict(start=10, end=12.5, text="자료를 전달해 주세요.", speaker="SPEAKER_00"),
                dict(start=12.2, end=13, text=quote, speaker="SPEAKER_01"),
            ]
        }
    )
    evidence = await source.fetch_evidence("role_sample", 12)
    assert evidence is not None and evidence["segments"][0]["speaker_id"] == "SPEAKER_01"
    result = await LocalClaimGrounder(AlwaysSupported(quote), WikiGroundingConfig()).verify(
        _page(f"자료를 전달하기로 했다. {_ANSWER}"), source
    )
    assert result.passed


@pytest.mark.parametrize("owner,expected", [("SPEAKER_00", False), ("SPEAKER_01", True)])
async def test_같은초_혼합화자도_quote에_맞는_실제화자로_판정한다(
    owner: str, expected: bool
) -> None:
    """두 화자의 발화를 합친 text의 존재만으로 담당자를 바꾸지 못한다."""
    quote = "제가 자료를 공유하겠습니다."
    source = UtterancesCitationVerifier(
        {
            "role_sample": [
                dict(
                    start=10.1,
                    end=10.4,
                    text="자료를 공유해 주실 수 있나요?",
                    speaker="SPEAKER_00",
                ),
                dict(start=10.5, end=11, text=quote, speaker="SPEAKER_01"),
            ]
        }
    )
    result = await LocalClaimGrounder(AlwaysSupported(quote), WikiGroundingConfig()).verify(
        _page(f"- SPEAKER_00: 자료를 공유하기로 했다. {_QUESTION}".replace("SPEAKER_00", owner)),
        source,
    )
    assert result.passed is expected
    if not expected:
        assert result.diagnostic == "quote_actor_mismatch"


async def test_짧은_quote가_질문과약속에_모두있어도_질문자를_담당자로_추정하지_않는다() -> None:
    """동일 문자열 중 질문 발화를 제외하고 남은 약속 화자로 역할을 확인한다."""
    result = await LocalClaimGrounder(
        AlwaysSupported("자료를 공유"), WikiGroundingConfig()
    ).verify(_page(f"- SPEAKER_00: 자료를 공유하기로 했다. {_QUESTION} {_ANSWER}"), _source())
    assert result.diagnostic == "quote_actor_mismatch"


async def test_화자정보없는_raw담당자는_추측하지_않고_보류한다() -> None:
    """원문의 화자 누락은 요청자나 이름 유추로 채우지 않는다."""
    result = await LocalClaimGrounder(
        AlwaysSupported("네. 제가 자료를 공유하겠습니다."), WikiGroundingConfig()
    ).verify(
        _page(f"- SPEAKER_01: 자료를 공유하기로 했다. {_ANSWER}"), _source(answer_speaker=None)
    )
    assert result.reason == "grounding_unavailable"
    assert result.diagnostic == "quote_actor_unresolved"


async def test_다른화자라도_quote가_raw담당자를_명시배정하면_허용한다() -> None:
    """원문 안에 담당자 배정이 실제로 있으면 발화자와 담당자를 구별할 수 있다."""
    quote = "SPEAKER_01이 자료를 공유하기로 했습니다."
    result = await LocalClaimGrounder(AlwaysSupported(quote), WikiGroundingConfig()).verify(
        _page(f"- SPEAKER_01: 자료를 공유하기로 했다. {_ANSWER}"),
        _source(answer=quote, answer_speaker="SPEAKER_00"),
    )
    assert result.passed


async def test_수치없는_단순상태보고는_자기약속을_요구하지_않는다() -> None:
    """단순 현황의 사실 검수는 기존 모델 의미 판정을 유지한다."""
    quote = "현재 검토 단계입니다."
    result = await LocalClaimGrounder(AlwaysSupported(quote), WikiGroundingConfig()).verify(
        _page(f"현재 검토 단계이다. {_ANSWER}"), _source(answer=quote)
    )
    assert result.passed


async def test_결정제목도_본문과_별도주장으로_검수하고_결정내용_인용을_우선한다() -> None:
    """현황 설명이 맞아도 제목의 새로운 확정까지 자동으로 지지하지 않는다."""

    class TitleJudge(AlwaysSupported):
        async def generate(self, **kwargs: Any) -> str:
            """현재 상태 본문은 지원하고 새 확정 제목은 불충분으로 답한다."""
            self.calls.append(kwargs)
            claims = json.loads(kwargs["user_prompt"])["claims"]
            return json.dumps(
                {
                    "results": [
                        {
                            "id": claim["id"],
                            "verdict": "insufficient"
                            if claim["claim"].startswith("결정 제목:")
                            else "supported",
                            "quote": self.quote,
                        }
                        for claim in claims
                    ]
                },
                ensure_ascii=False,
            )

    llm = TitleJudge("현재 검토 단계입니다.")
    page = (
        "---\ntype: decision\nstatus: decided\ntitle: 진행 방식 최종 확정\n---\n"
        f"## 결정 내용\n현재 검토 단계이다. {_ANSWER}\n"
        f"## 근거\n{_QUESTION}\n"
    )
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(
        page, _source(answer=llm.quote)
    )
    assert result.diagnostic == "insufficient_evidence"
    claims = json.loads(llm.calls[0]["user_prompt"])["claims"]
    assert len(claims) == 2
    assert claims[-1]["claim"] == "결정 제목: 진행 방식 최종 확정"
    assert [item["citation"] for item in claims[-1]["evidence"]] == [_ANSWER]


async def test_제목에쓴_가짜인용은_근거로_조회하지_않는다() -> None:
    """제목은 검수 대상이며 제목에 들어간 마커를 근거 집합에 추가하지 않는다."""
    llm = AlwaysSupported("현재 검토 단계입니다.")
    page = (
        "---\ntype: decision\n"
        "title: 현재 상태 [meeting:unprovided@00:00:00]\n---\n"
        f"현재 검토 단계이다. {_ANSWER}\n"
    )
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(
        page, _source(answer=llm.quote)
    )
    assert result.passed
    claims = json.loads(llm.calls[0]["user_prompt"])["claims"]
    assert [item["citation"] for item in claims[-1]["evidence"]] == [_ANSWER]


async def test_제목추가도_기존_claim예산을_초과하면_보류한다() -> None:
    """제목 검수에 별도 무제한 예산을 부여하지 않는다."""
    llm = AlwaysSupported("현재 검토 단계입니다.")
    page = f"---\ntype: decision\ntitle: 상태 확인\n---\n현재 검토 단계이다. {_ANSWER}\n"
    result = await LocalClaimGrounder(llm, WikiGroundingConfig(max_claims=1)).verify(
        page, _source(answer=llm.quote)
    )
    assert result.diagnostic == "claim_limit"
    assert not llm.calls


async def test_제목이있어도_본문주장없음_계약을_완화하지_않는다() -> None:
    """제목만으로 원래 no_claims인 문서를 게시하지 않는다."""
    llm = AlwaysSupported("현재 검토 단계입니다.")
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(
        f"---\ntype: decision\ntitle: 최종 확정\n---\n{_ANSWER}\n", _source()
    )
    assert result.diagnostic == "no_claims"
    assert not llm.calls
