"""닫힌 원래 업무·종료 사유·실제 보고자를 연결해 근거를 검수하는 회귀."""

from __future__ import annotations

import json
from typing import Any

import pytest

from config import WikiGroundingConfig
from core.wiki.citation_verifier import UtterancesCitationVerifier
from core.wiki.extractors.action_item import (
    ActionItemExtractor,
    ClosedActionItem,
    OpenActionItem,
)
from core.wiki.grounding import LocalClaimGrounder
from core.wiki.models import Citation

_ASSIGNMENT = "[meeting:assignment_sample@00:00:10]"
_REPORT = "[meeting:completion_sample@00:00:20]"


def _page(*, reporter: str = "SPEAKER_01", reason: str = "completed") -> str:
    """실제 렌더러와 identity 메타데이터를 사용한 합성 닫힌 항목을 만든다."""
    original = OpenActionItem(
        item_id="synthetic-action",
        owner="SPEAKER_00",
        description="보고서를 공유한다.",
        from_meeting_id="assignment_sample",
        citation=Citation("assignment_sample", "00:00:10", 10),
        from_date="2026-01-01",
    )
    closed = ClosedActionItem(
        original=original,
        closed_citation=Citation("completion_sample", "00:00:20", 20),
        closed_by_speaker=reporter,
        closed_at_meeting_id="completion_sample",
        closed_reason=reason,
    )
    block = "\n".join(ActionItemExtractor._render_closed_block(closed))
    return f"---\ntype: action_items\n---\n# Action Items\n## Closed (1)\n{block}\n"


def _page_with_supporting_assignment() -> str:
    """원래 할 일의 보조 인용도 보존하는 실제 렌더러 문서를 만든다."""
    original = OpenActionItem(
        item_id="synthetic-supporting",
        owner="SPEAKER_00",
        description="보고서를 공유한다.",
        from_meeting_id="assignment_sample",
        from_date="2026-01-01",
        citation=Citation("assignment_sample", "00:00:10", 10),
        supporting_citations=(Citation("assignment_sample", "00:00:12", 12),),
    )
    closed = ClosedActionItem(
        original=original,
        closed_citation=Citation("completion_sample", "00:00:20", 20),
        closed_by_speaker="SPEAKER_01",
        closed_at_meeting_id="completion_sample",
    )
    return "---\ntype: action_items\n---\n" + "\n".join(
        ActionItemExtractor._render_closed_block(closed)
    )


def _source(text: str, *, speaker: str | None = "SPEAKER_01") -> UtterancesCitationVerifier:
    """원래 수행 약속과 다른 회의의 종료 근거를 분리해 제공한다."""
    return UtterancesCitationVerifier(
        {
            "assignment_sample": [
                dict(start=10, end=11, text="제가 보고서를 공유하겠습니다.", speaker="SPEAKER_00")
            ],
            "completion_sample": [dict(start=20, end=21, text=text, speaker=speaker)],
        }
    )


class ClosureJudge:
    """본문은 지지하고 종료 주장에만 지정 판정을 적용하는 로컬 모델 대역."""

    model_name = "synthetic-closure-judge"

    def __init__(self, *, verdict: str = "supported", quote: str | None = None) -> None:
        """실제 모델 없이 종료 판정과 quote를 지정한다."""
        self.verdict = verdict
        self.quote = quote
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> str:
        """원래 업무 성공이 종료 주장의 근거를 대신하지 못하게 구별한다."""
        self.calls.append(kwargs)
        claims = json.loads(kwargs["user_prompt"])["claims"]
        return json.dumps(
            {
                "results": [
                    {
                        "id": claim["id"],
                        "verdict": self.verdict if "closure_context" in claim else "supported",
                        "quote": self.quote
                        if "closure_context" in claim and self.quote is not None
                        else claim["evidence"][0]["segments"][0]["text"],
                    }
                    for claim in claims
                ]
            },
            ensure_ascii=False,
        )


@pytest.mark.parametrize(
    "reason,text",
    [
        ("completed", "보고서 공유를 완료했습니다."),
        ("cancelled", "보고서 공유 업무를 취소했습니다."),
        ("superseded", "보고서 공유 업무는 대시보드 배포로 대체했습니다."),
    ],
)
async def test_종료사유와_구체업무를_종료인용만으로_별도검수한다(reason: str, text: str) -> None:
    """원래 assignment의 인용을 종료 claim 근거로 가져오지 않는다."""
    llm = ClosureJudge()
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(
        _page(reason=reason), _source(text)
    )
    assert result.passed
    claims = json.loads(llm.calls[0]["user_prompt"])["claims"]
    closure = next(claim for claim in claims if "closure_context" in claim)
    assert closure["closure_context"] == {
        "original_scope": "보고서를 공유한다.",
        "original_owner": "SPEAKER_00",
        "original_assignment_citations": [_ASSIGNMENT],
        "reason": reason,
        "reporter": "SPEAKER_01",
    }
    assert [item["citation"] for item in closure["evidence"]] == [_REPORT]
    assert "같은 임시 번호라도 같은" in llm.calls[0]["system_prompt"]
    assert "action-identity-v1" not in llm.calls[0]["user_prompt"]


@pytest.mark.parametrize("text", ["네 알겠습니다.", "네.", "알겠습니다. 감사합니다."])
async def test_모델이_거짓supported라도_맞장구만으로_업무완료를_통과시키지_않는다(
    text: str,
) -> None:
    """요청의 수락과 완료 보고를 분리한다."""
    result = await LocalClaimGrounder(ClosureJudge(), WikiGroundingConfig()).verify(
        _page(), _source(text)
    )
    assert result.reason == "unsupported_claim" and result.diagnostic == "closure_ack_only"


async def test_다른사람의_완료발화를_보고자번호로_기록하면_거부한다() -> None:
    """공유 완료 사실이 있더라도 실제 보고자의 임시키를 잘못 붙일 수 없다."""
    result = await LocalClaimGrounder(ClosureJudge(), WikiGroundingConfig()).verify(
        _page(reporter="SPEAKER_00"), _source("보고서 공유를 완료했습니다.")
    )
    assert result.reason == "unsupported_claim"
    assert result.diagnostic == "closure_reporter_mismatch"


async def test_이전발화와_구간이겹쳐도_실제floor보고자와_대조한다() -> None:
    """구간 첫 화자가 아니라 citation verifier가 선택한 종료 발화를 검수한다."""
    source = _source("보고서 공유를 완료했습니다.")
    source = UtterancesCitationVerifier(
        {
            **source._utterances_by_meeting,
            "completion_sample": [
                dict(start=19, end=20.5, text="진행 상황이 어떤가요?", speaker="SPEAKER_00"),
                dict(start=20.2, end=21, text="보고서 공유를 완료했습니다.", speaker="SPEAKER_01"),
            ],
        }
    )
    result = await LocalClaimGrounder(ClosureJudge(), WikiGroundingConfig()).verify(
        _page(), source
    )
    assert result.passed


@pytest.mark.parametrize(
    "text,diagnostic",
    [
        ("보고서를 공유하겠습니다.", "closure_future_promise"),
        ("보고서 공유는 진행 중입니다.", "closure_in_progress"),
        ("보고서를 공유해 주세요.", "closure_request_as_completion"),
        ("보고서 공유를 완료했나요?", "closure_request_as_completion"),
    ],
)
async def test_원래할일의_약속이나진행이나질문을_완료로_바꾸지_않는다(
    text: str, diagnostic: str
) -> None:
    """모델 대역이 의도적으로 오판해도 명백히 다른 단계의 quote는 보류한다."""
    result = await LocalClaimGrounder(ClosureJudge(), WikiGroundingConfig()).verify(
        _page(), _source(text)
    )
    assert result.reason == "unsupported_claim" and result.diagnostic == diagnostic


async def test_완료보고뒤_추가작업진행중이_있어도_완료된업무는_허용한다() -> None:
    """진행 중이라는 표현을 문서 전체 금지 규칙으로 사용하지 않는다."""
    result = await LocalClaimGrounder(ClosureJudge(), WikiGroundingConfig()).verify(
        _page(), _source("보고서 공유를 완료했습니다. 부록은 확인 중입니다.")
    )
    assert result.passed


async def test_종료보고자_원문화자가_없으면_추정하지_않는다() -> None:
    """원래 담당자나 다른 회의의 번호로 보고자 정보를 채우지 않는다."""
    result = await LocalClaimGrounder(ClosureJudge(), WikiGroundingConfig()).verify(
        _page(), _source("보고서 공유를 완료했습니다.", speaker=None)
    )
    assert result.reason == "grounding_unavailable"
    assert result.diagnostic == "closure_reporter_unresolved"


async def test_업무범위가_다른종료발화면_본문지원과별개로_보류한다() -> None:
    """의미 판정 모델이 종료 context의 구체 업무를 검수해야 한다."""
    result = await LocalClaimGrounder(
        ClosureJudge(verdict="insufficient"), WikiGroundingConfig()
    ).verify(_page(), _source("다른 안내 메일 전송을 완료했습니다."))
    assert result.reason == "unsupported_claim" and result.diagnostic == "insufficient_evidence"


async def test_종료quote를_다른원래업무인용에서_가져오지_못한다() -> None:
    """원래 약속과 종료 출처를 합쳐 quote 실재성 검사를 우회할 수 없다."""
    result = await LocalClaimGrounder(
        ClosureJudge(quote="제가 보고서를 공유하겠습니다."), WikiGroundingConfig()
    ).verify(_page(), _source("네 알겠습니다."))
    assert result.diagnostic == "quote_not_in_evidence"


async def test_과거종료사유를_추정하거나_잘못된identity를_무시하지_않는다() -> None:
    """legacy·변조 상태는 원문을 수정하지 않고 보수적으로 검수 불가로 남긴다."""
    page = _page()
    llm = ClosureJudge()
    legacy = "\n".join(line for line in page.splitlines() if "action-identity-v1" not in line)
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(
        legacy, _source("보고서 공유를 완료했습니다.")
    )
    assert result.diagnostic == "closure_identity_missing"
    broken = page.replace("action-identity-v1: ", "action-identity-v1: invalid ")
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(
        broken, _source("보고서 공유를 완료했습니다.")
    )
    assert result.diagnostic == "invalid_action_identity" and not llm.calls


async def test_종료보고가_없거나_고립되어_있으면_보류한다() -> None:
    """독립 Closed by 줄의 지원 판정으로 종료 사실을 만들지 않는다."""
    page = _page()
    missing = "\n".join(line for line in page.splitlines() if "Closed by:" not in line)
    result = await LocalClaimGrounder(ClosureJudge(), WikiGroundingConfig()).verify(
        missing, _source("보고서 공유를 완료했습니다.")
    )
    assert result.diagnostic == "closure_source_missing"
    orphan = f"---\ntype: action_items\n---\n  - Closed by: SPEAKER_01 {_REPORT}\n"
    result = await LocalClaimGrounder(ClosureJudge(), WikiGroundingConfig()).verify(
        orphan, _source("보고서 공유를 완료했습니다.")
    )
    assert result.diagnostic == "invalid_action_closure"


async def test_연결검수도_기존전체주장예산을_지킨다() -> None:
    """원래 업무와 reporter 두 줄만 세고 종료 주장을 예산 밖에 추가하지 않는다."""
    llm = ClosureJudge()
    result = await LocalClaimGrounder(llm, WikiGroundingConfig(max_claims=2)).verify(
        _page(), _source("보고서 공유를 완료했습니다.")
    )
    assert result.diagnostic == "claim_limit" and not llm.calls


async def test_원래업무의_여러인용을_보존하되_종료evidence에_섞지_않는다() -> None:
    """요청과 수락을 함께 보존하는 새 원장 형식도 종료 검수와 분리한다."""
    source = _source("보고서 공유를 완료했습니다.")
    source = UtterancesCitationVerifier(
        {
            **source._utterances_by_meeting,
            "assignment_sample": [
                dict(start=10, end=11, text="보고서를 공유해 주세요.", speaker="SPEAKER_02"),
                dict(start=12, end=13, text="제가 보고서를 공유하겠습니다.", speaker="SPEAKER_00"),
            ],
        }
    )
    llm = ClosureJudge()
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(
        _page_with_supporting_assignment(), source
    )
    assert result.passed
    claims = json.loads(llm.calls[0]["user_prompt"])["claims"]
    closure = next(claim for claim in claims if "closure_context" in claim)
    assert len(closure["closure_context"]["original_assignment_citations"]) == 2
    assert [item["citation"] for item in closure["evidence"]] == [_REPORT]


async def test_종료보고는_한개의_종료인용_계약을_지킨다() -> None:
    """원래 할 일의 보조 인용 확장을 종료 보고의 여러 출처 허용으로 넓히지 않는다."""
    page = _page().replace(_REPORT, f"{_REPORT} {_ASSIGNMENT}")
    result = await LocalClaimGrounder(ClosureJudge(), WikiGroundingConfig()).verify(
        page, _source("보고서 공유를 완료했습니다.")
    )
    assert result.diagnostic == "closure_source_missing"
