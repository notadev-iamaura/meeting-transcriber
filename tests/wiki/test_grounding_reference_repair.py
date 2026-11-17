"""잘못된 근거 번호만 한 번 선택하며 판정·정상 행·기존 검수를 보존하는 회귀."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from config import WikiGroundingConfig
from core.wiki.citation_verifier import UtterancesCitationVerifier
from core.wiki.grounding import LocalClaimGrounder

_MARKER = "[meeting:repair_sample@00:00:10]"


def _page(lines: int = 1) -> str:
    """원문과 분리된 합성 수행 약속만 검수한다."""
    return "---\ntype: action_items\n---\n" + "\n".join(
        f"- SPEAKER_01: 검토하기로 했다. {_MARKER}" for _ in range(lines)
    )


def _source() -> UtterancesCitationVerifier:
    """질문과 실제 수행 약속이 서로 다른 화자인 원문을 만든다."""
    return UtterancesCitationVerifier(
        {
            "repair_sample": [
                dict(start=10.1, end=10.4, text="검토해 주세요.", speaker="SPEAKER_00"),
                dict(start=10.5, end=11, text="제가 검토하겠습니다.", speaker="SPEAKER_01"),
            ]
        }
    )


class Judge:
    """정상 판정과 번호 수리 응답을 구분해 원래 요청을 모두 보존한다."""

    model_name = "synthetic-reference-repair"

    def __init__(
        self, repair: Any = None, original: Callable[[int], dict[str, Any]] | None = None
    ) -> None:
        self.repair = repair
        self.original = original or (
            lambda index: dict(id=index, verdict="supported", evidence_index=0, segment_index=99)
        )
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> str:
        """수리 요청은 valid_pairs가 있을 때만 별도의 응답으로 처리한다."""
        self.calls.append(deepcopy(kwargs))
        claims = json.loads(kwargs["user_prompt"])["claims"]
        if "valid_pairs" not in claims[0]:
            return json.dumps({"results": [self.original(c["id"]) for c in claims]})
        if isinstance(self.repair, BaseException):
            raise self.repair
        if self.repair == "wait":
            await asyncio.Event().wait()
        response = self.repair
        if response is None:
            response = {
                "references": [dict(id=c["id"], evidence_index=0, segment_index=1) for c in claims]
            }
        return response if isinstance(response, str) else json.dumps(response)


async def test_reference_repair_preserves_verdict_ids_valid_rows_and_all_evidence() -> None:
    """번호가 잘못된 행만 수리 입력에 포함하고 정상 행과 원문은 그대로 둔다."""

    def original(index: int) -> dict[str, Any]:
        return dict(
            id=index, verdict="supported", evidence_index=0, segment_index=99 if index == 0 else 1
        )

    llm = Judge(original=original)
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(2), _source())
    assert result.passed and len(llm.calls) == 2
    normal = json.loads(llm.calls[0]["user_prompt"])["claims"]
    repair = json.loads(llm.calls[1]["user_prompt"])["claims"]
    assert [c["id"] for c in normal] == [0, 1]
    assert [c["id"] for c in repair] == [0]
    assert repair[0]["evidence"] == normal[0]["evidence"]
    assert repair[0]["valid_pairs"] == [
        dict(evidence_index=0, segment_index=0),
        dict(evidence_index=0, segment_index=1),
    ]
    assert "판정·원문을 새로 생성하거나 수정하지" in llm.calls[1]["system_prompt"]
    assert llm.calls[1]["max_tokens"] == WikiGroundingConfig().max_output_tokens
    assert llm.calls[1]["temperature"] == 0


@pytest.mark.parametrize(
    "reference",
    [
        {"id": 0, "evidence_index": 1, "segment_index": 0},
        {"id": 0, "evidence_index": 0, "segment_index": 9},
        {"id": 0, "evidence_index": True, "segment_index": 1},
        {"id": 0, "evidence_index": 0, "segment_index": False},
        {"id": True, "evidence_index": 0, "segment_index": 1},
        {"id": 99, "evidence_index": 0, "segment_index": 1},
        {"id": "0", "evidence_index": 0, "segment_index": 1},
        {"id": 0, "evidence_index": None, "segment_index": None},
        {"id": 0, "evidence_index": 0.0, "segment_index": 1},
        {"id": 0, "evidence_index": 0, "segment_index": 1, "verdict": "supported"},
        {"id": 0, "evidence_index": 0, "segment_index": 1, "quote": "새로 쓴 문구"},
    ],
)
async def test_repair_rejects_unknown_or_unrelated_pair_bool_null_and_extra_fields(
    reference: dict[str, Any],
) -> None:
    """다른 근거나 판정·문구 추가는 정확한 세 필드 번호 수리로 인정하지 않는다."""
    llm = Judge({"references": [reference]})
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(), _source())
    assert not result.passed and result.diagnostic == "invalid_evidence_reference"
    assert len(llm.calls) == 2


@pytest.mark.parametrize(
    "repair",
    [
        {"references": []},
        {"references": [{"id": 0, "evidence_index": 0, "segment_index": 1}] * 2},
        {
            "references": [{"id": 0, "evidence_index": 0, "segment_index": 1}],
            "verdict": "supported",
        },
        '{"references":[],"references":[]}',
        "not json",
    ],
)
async def test_repair_is_strict_and_has_no_retry_for_malformed_or_incomplete_response(
    repair: Any,
) -> None:
    """누락·중복·다른 형식은 실패하며 세 번째 호출로 구제하지 않는다."""
    llm = Judge(repair)
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(), _source())
    assert not result.passed and len(llm.calls) == 2


async def test_duplicate_ids_cannot_replace_an_omitted_repair_target() -> None:
    """행 수가 같아도 같은 id를 두 번 쓰거나 대상 id를 생략하면 차단한다."""
    llm = Judge({"references": [dict(id=0, evidence_index=0, segment_index=1)] * 2})
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(2), _source())
    assert not result.passed and len(llm.calls) == 2


@pytest.mark.parametrize("decline", ["contradicted", "insufficient"])
@pytest.mark.parametrize("decline_first", [True, False])
async def test_any_original_decline_prevents_repair_even_when_bad_pointer_is_first(
    decline: str, decline_first: bool
) -> None:
    """원 batch 전체의 부정 판정을 먼저 검사해 잘못된 번호가 앞에 있어도 재시도하지 않는다."""

    def original(index: int) -> dict[str, Any]:
        if (index == 0) == decline_first:
            return dict(id=index, verdict=decline)
        return dict(id=index, verdict="supported", evidence_index=0, segment_index=99)

    llm = Judge(original=original)
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(2), _source())
    assert result.reason == "unsupported_claim" and len(llm.calls) == 1


async def test_repaired_reference_still_cannot_use_request_as_owners_commitment() -> None:
    """유효 번호여도 다른 화자의 요청을 수행 약속으로 쓰는 경우 기존 검수가 막는다."""
    llm = Judge({"references": [dict(id=0, evidence_index=0, segment_index=0)]})
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(), _source())
    assert result.reason == "unsupported_claim"
    assert result.diagnostic in {"quote_request_as_commitment", "quote_actor_mismatch"}


async def test_reference_repair_budget_is_per_document_not_per_batch() -> None:
    """다음 batch에도 잘못된 번호가 나오면 두 번째 수리 호출 없이 보류한다."""
    llm = Judge()
    result = await LocalClaimGrounder(llm, WikiGroundingConfig(batch_size=1)).verify(
        _page(2), _source()
    )
    assert not result.passed and result.diagnostic == "invalid_evidence_reference"
    assert len(llm.calls) == 3  # 정상 batch1 + 수리1 + 정상 batch2


async def test_reference_repair_can_be_disabled() -> None:
    """0 설정은 기존의 즉시 보류 경로를 유지한다."""
    llm = Judge()
    result = await LocalClaimGrounder(llm, WikiGroundingConfig(max_reference_repairs=0)).verify(
        _page(), _source()
    )
    assert not result.passed and len(llm.calls) == 1


async def test_other_valid_row_semantic_or_quote_failure_is_not_retried() -> None:
    """같은 batch의 다른 실패를 번호 수리 호출로 바꾸지 않는다."""
    llm = Judge(
        original=lambda i: (
            dict(id=i, verdict="supported", evidence_index=0, segment_index=99)
            if i == 0
            else dict(id=i, verdict="supported", quote="없는 문장")
        )
    )
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(2), _source())
    assert result.diagnostic == "quote_not_in_evidence" and len(llm.calls) == 1


async def test_repair_cancellation_propagates_without_another_call() -> None:
    """취소는 기술 실패로 삼켜 다시 실행하지 않는다."""
    llm = Judge(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(), _source())
    assert len(llm.calls) == 2


async def test_repair_shares_the_original_document_timeout() -> None:
    """수리 호출에 새 시간 예산을 주지 않고 문서의 기존 timeout으로 중단한다."""
    llm = Judge("wait")
    result = await LocalClaimGrounder(llm, WikiGroundingConfig(timeout_seconds=0.02)).verify(
        _page(), _source()
    )
    assert result.diagnostic == "timeout" and len(llm.calls) == 2


async def test_repaired_reference_still_runs_actual_action_closure_checks() -> None:
    """번호만 복구돼도 미래 약속을 완료 보고로 바꾸는 종료 주장은 그대로 보류한다."""
    from core.wiki.extractors.action_item import (
        ActionItemExtractor,
        ClosedActionItem,
        OpenActionItem,
    )
    from core.wiki.models import Citation

    original = OpenActionItem(
        item_id="repair-closure",
        owner="SPEAKER_00",
        description="보고서를 공유한다.",
        from_meeting_id="assignment_sample",
        citation=Citation("assignment_sample", "00:00:10", 10),
        from_date="2026-01-01",
    )
    closed = ClosedActionItem(
        original=original,
        closed_citation=Citation("completion_sample", "00:00:20", 20),
        closed_by_speaker="SPEAKER_01",
        closed_at_meeting_id="completion_sample",
        closed_reason="completed",
    )
    page = "---\ntype: action_items\n---\n" + "\n".join(
        ActionItemExtractor._render_closed_block(closed)
    )
    source = UtterancesCitationVerifier(
        {
            "assignment_sample": [
                dict(start=10, end=11, text="제가 보고서를 공유하겠습니다.", speaker="SPEAKER_00")
            ],
            "completion_sample": [
                dict(start=20, end=21, text="제가 보고서를 공유하겠습니다.", speaker="SPEAKER_01")
            ],
        }
    )

    class ClosureJudge(Judge):
        async def generate(self, **kwargs: Any) -> str:
            self.calls.append(deepcopy(kwargs))
            claims = json.loads(kwargs["user_prompt"])["claims"]
            if "valid_pairs" in claims[0]:
                return json.dumps(
                    {
                        "references": [
                            dict(id=c["id"], evidence_index=0, segment_index=0) for c in claims
                        ]
                    }
                )
            return json.dumps(
                {
                    "results": [
                        dict(
                            id=c["id"],
                            verdict="supported",
                            evidence_index=0,
                            segment_index=99 if "closure_context" in c else 0,
                        )
                        for c in claims
                    ]
                }
            )

    llm = ClosureJudge()
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(page, source)
    assert not result.passed and result.diagnostic == "closure_future_promise"
    assert len(llm.calls) == 2


async def test_repair_request_keeps_original_input_limit() -> None:
    """번호 선택지가 더해져 기존 입력 예산을 넘으면 수리 모델 호출 전에 보류한다."""
    source = UtterancesCitationVerifier(
        {
            "repair_sample": [
                dict(
                    start=10.1,
                    end=10.4,
                    text="검토해 주세요." + " 배경" * 160,
                    speaker="SPEAKER_00",
                ),
                dict(
                    start=10.5,
                    end=11,
                    text="제가 검토하겠습니다." + " 배경" * 160,
                    speaker="SPEAKER_01",
                ),
            ]
        }
    )
    probe = Judge()
    assert (await LocalClaimGrounder(probe, WikiGroundingConfig()).verify(_page(), source)).passed
    original_size = len(probe.calls[0]["user_prompt"])
    assert len(probe.calls[1]["user_prompt"]) > original_size >= 1000
    llm = Judge()
    result = await LocalClaimGrounder(
        llm, WikiGroundingConfig(max_input_chars=original_size)
    ).verify(_page(), source)
    assert result.reason == "grounding_limit" and result.diagnostic == "request_limit"
    assert len(llm.calls) == 1


async def test_repair_response_keeps_original_output_bound() -> None:
    """원 응답과 동일하게 과도한 수리 응답도 그대로 거부한다."""
    llm = Judge("x" * (WikiGroundingConfig().max_input_chars + 1))
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(), _source())
    assert result.diagnostic == "response_limit" and len(llm.calls) == 2


@pytest.mark.parametrize("value", [-1, 2, True, "1"])
def test_repair_budget_config_only_accepts_zero_or_one(value: Any) -> None:
    """문서당 최대 한 번이라는 설정 계약을 넓히지 않는다."""
    with pytest.raises(ValidationError):
        WikiGroundingConfig(max_reference_repairs=value)
