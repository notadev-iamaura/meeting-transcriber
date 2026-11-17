"""원문 번호 참조가 인용·화자·종료·예산 검수를 우회하지 않는지 검증한다."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from typing import Any

import pytest

from config import WikiGroundingConfig
from core.wiki.citation_verifier import UtterancesCitationVerifier
from core.wiki.extractors.action_item import ActionItemExtractor, ClosedActionItem, OpenActionItem
from core.wiki.grounding import LocalClaimGrounder, _with_quote_actor, resolve_grounding_witness
from core.wiki.guard import InMemoryCitationVerifier
from core.wiki.models import Citation

_MARKER = "[meeting:witness_sample@00:00:10]"


def _page(claim: str = "검토 중이다.") -> str:
    """사용자 자료 없이 단일 근거 문서를 만든다."""
    return f"---\ntype: action_items\n---\n{claim} {_MARKER}\n"


def _source() -> UtterancesCitationVerifier:
    """같은 초의 요청자와 자기 수행 약속자를 구별할 수 있는 원문을 만든다."""
    return UtterancesCitationVerifier(
        {
            "witness_sample": [
                dict(start=10.1, end=10.4, text="검토해 주세요.", speaker="SPEAKER_00"),
                dict(start=10.5, end=11, text="제가 검토하겠습니다.", speaker="SPEAKER_01"),
            ]
        }
    )


class Judge:
    """행 생성 규칙만 주입하며 실제 모델은 호출하지 않는 대역."""

    model_name = "synthetic-witness-judge"

    def __init__(self, row: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        """지원되는 번호 응답 또는 명시된 잘못된 응답을 준비한다."""
        self.row = row or (
            lambda claim: dict(
                id=claim["id"], verdict="supported", evidence_index=0, segment_index=1
            )
        )
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> str:
        """입력을 보관하고 id별 합성 구조 응답을 반환한다."""
        self.calls.append(kwargs)
        return json.dumps(
            {
                "results": [
                    self.row(claim) for claim in json.loads(kwargs["user_prompt"])["claims"]
                ]
            },
            ensure_ascii=False,
        )


async def test_번호로선택한_단일실제원문과_수행화자로_검수한다() -> None:
    """문구를 모델이 재생성하지 않고 서버가 실제 원문에서 가져온다."""
    llm = Judge()
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(
        _page("- [ ] SPEAKER_01: 검토하기로 했다."), _source()
    )
    assert result.passed
    claim = json.loads(llm.calls[0]["user_prompt"])["claims"][0]
    assert claim["required_quote_actor"] == "SPEAKER_01"
    assert [segment["quote_actor_matches"] for segment in claim["evidence"][0]["segments"]] == [
        False,
        True,
    ]
    assert len(claim["evidence"][0]["segments"]) == 2
    assert "모든 연결 evidence의 범위·조건 검수 의무는 그대로" in llm.calls[0]["system_prompt"]


async def test_여러인용중_두번째실제약속을_선택하고_첫범위근거도_보존한다() -> None:
    """evidence index와 segment index를 혼동하거나 범위 설명을 삭제하지 않는다."""
    other = "[meeting:witness_sample@00:00:20]"
    source = UtterancesCitationVerifier(
        {
            "witness_sample": [
                dict(start=10, end=11, text="보고서의 확인 범위입니다.", speaker="SPEAKER_00"),
                dict(start=20, end=21, text="제가 보고서를 확인하겠습니다.", speaker="SPEAKER_01"),
            ]
        }
    )
    llm = Judge(lambda c: dict(id=c["id"], verdict="supported", evidence_index=1, segment_index=0))
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(
        _page("- SPEAKER_01: 보고서를 확인하기로 했다.").replace(_MARKER, f"{_MARKER} {other}"),
        source,
    )
    assert result.passed
    claim = json.loads(llm.calls[0]["user_prompt"])["claims"][0]
    assert [item["citation"] for item in claim["evidence"]] == [_MARKER, other]
    witness = resolve_grounding_witness(
        dict(id=0, verdict="supported", evidence_index=1, segment_index=0), claim["evidence"]
    )
    assert witness.quote == "제가 보고서를 확인하겠습니다."
    assert [item["citation"] for item in witness.evidence] == [other]


async def test_다른claim의_전역순번으로_현재claim근거를_참조할수없다() -> None:
    """두 번째 claim도 자신의 한 개짜리 evidence 배열에서만 선택해야 한다."""
    other = "[meeting:witness_sample@00:00:20]"
    source = UtterancesCitationVerifier(
        {
            "witness_sample": [
                dict(start=10, end=11, text="제가 첫 검토를 하겠습니다.", speaker="SPEAKER_00"),
                dict(start=20, end=21, text="제가 다음 검토를 하겠습니다.", speaker="SPEAKER_01"),
            ]
        }
    )
    page = f"---\ntype: action_items\n---\n- SPEAKER_00: 첫 검토를 한다. {_MARKER}\n- SPEAKER_01: 다음 검토를 한다. {other}\n"
    llm = Judge(
        lambda c: dict(id=c["id"], verdict="supported", evidence_index=c["id"], segment_index=0)
    )
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(page, source)
    assert result.diagnostic == "invalid_evidence_reference"


async def test_표시번호는_현재claim순서에맞고_모델은_그번호를_그대로복사한다() -> None:
    """같은 cached source가 다른 위치에 있어도 claim별 evidence 번호를 다시 붙인다."""
    other = "[meeting:witness_sample@00:00:20]"
    source = UtterancesCitationVerifier(
        {
            "witness_sample": [
                dict(start=10, end=11, text="보고서의 확인 범위입니다.", speaker="SPEAKER_00"),
                dict(start=20.1, end=20.4, text="보고서를 확인해 주세요.", speaker="SPEAKER_00"),
                dict(
                    start=20.5, end=21, text="제가 보고서를 확인하겠습니다.", speaker="SPEAKER_01"
                ),
            ]
        }
    )

    def copy_displayed_reference(claim: dict[str, Any]) -> dict[str, Any]:
        """초수나 화자 번호를 계산하지 않고 표시된 두 번호만 복사한다."""
        item = next(
            item
            for item in claim["evidence"]
            if any(segment["quote_actor_matches"] for segment in item["segments"])
        )
        segment = next(s for s in item["segments"] if s["quote_actor_matches"])
        return dict(
            id=claim["id"],
            verdict="supported",
            evidence_index=item["evidence_index"],
            segment_index=segment["segment_index"],
        )

    page = (
        "---\ntype: action_items\n---\n"
        f"- SPEAKER_01: 보고서를 확인한다. {_MARKER} {other}\n"
        f"- SPEAKER_01: 보고서를 확인한다. {other} {_MARKER}\n"
    )
    llm = Judge(copy_displayed_reference)
    assert (await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(page, source)).passed
    claims = json.loads(llm.calls[0]["user_prompt"])["claims"]
    assert [[e["evidence_index"] for e in c["evidence"]] for c in claims] == [[0, 1], [0, 1]]
    assert [
        next(e for e in c["evidence"] if e["citation"] == other)["evidence_index"] for c in claims
    ] == [1, 0]
    assert [
        [
            s["segment_index"]
            for s in next(e for e in c["evidence"] if e["citation"] == other)["segments"]
        ]
        for c in claims
    ] == [[0, 1], [0, 1]]
    assert "초수·speaker_id의 화자 번호" in llm.calls[0]["system_prompt"]


def test_표시번호와화자hint를_재배치해도_cache원문을_변경하지_않는다() -> None:
    """source에 남은 번호를 신뢰하지 않고 새 복사본에만 실제 위치를 표시한다."""
    item = dict(
        text="원문\n다른 원문",
        evidence_index=99,
        segments=[
            dict(text="원문", speaker_id="SPEAKER_00", segment_index=77),
            dict(text="다른 원문", speaker_id="SPEAKER_01", segment_index=88),
        ],
    )
    before = deepcopy(item)
    first = _with_quote_actor(item, "SPEAKER_00", 0)
    second = _with_quote_actor(item, "SPEAKER_01", 1)
    assert item == before
    assert [first["evidence_index"], second["evidence_index"]] == [0, 1]
    assert [[s["segment_index"] for s in e["segments"]] for e in (first, second)] == [
        [0, 1],
        [0, 1],
    ]
    assert [[s["quote_actor_matches"] for s in e["segments"]] for e in (first, second)] == [
        [True, False],
        [False, True],
    ]
    first["segments"][0]["text"] = "복사본만 수정"
    assert item == before and second["segments"][0]["text"] == "원문"


def test_표시필드가_잘못돼도_참조검사는_실제배열위치를_계속사용한다() -> None:
    """표시 번호를 lookup key로 신뢰해 범위 밖 참조를 허용하지 않는다."""
    evidence = [
        dict(evidence_index=99, text="원문", segments=[dict(segment_index=88, text="원문")])
    ]
    with pytest.raises(ValueError, match="^invalid_evidence_reference$"):
        resolve_grounding_witness(
            dict(id=0, verdict="supported", evidence_index=99, segment_index=88), evidence
        )
    witness = resolve_grounding_witness(
        dict(id=0, verdict="supported", evidence_index=0, segment_index=0), evidence
    )
    assert witness.quote == "원문"


@pytest.mark.parametrize(
    "evidence_index,segment_index",
    [
        (True, 0),
        (False, 0),
        (0, True),
        (0, False),
        ("0", 0),
        (0, "1"),
        (0.0, 0),
        (0, 1.0),
        (-1, 0),
        (1, 0),
        (0, -1),
        (0, 2),
        (None, 0),
        (0, None),
        ([], 0),
        (0, {}),
        (10**30, 0),
    ],
)
async def test_잘못된번호는_암묵변환하거나_다른근거로_대체하지_않는다(
    evidence_index: Any, segment_index: Any
) -> None:
    """bool·문자열·실수·음수·범위 밖 index는 검증 불가다."""
    llm = Judge(
        lambda c: dict(
            id=c["id"],
            verdict="supported",
            evidence_index=evidence_index,
            segment_index=segment_index,
        )
    )
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(), _source())
    assert result.reason == "grounding_unavailable"
    assert result.diagnostic == "invalid_evidence_reference"


@pytest.mark.parametrize(
    "extra",
    [
        dict(quote="제가 검토하겠습니다.", evidence_index=0, segment_index=1),
        dict(evidence_index=0),
        dict(segment_index=1),
        dict(evidence_index=0, segment_index=1, extra=True),
    ],
)
async def test_번호형과_구형의_혼합이나_불완전한형식은_거부한다(extra: dict[str, Any]) -> None:
    """유효한 quote나 index 하나를 골라 잘못된 구조를 구제하지 않는다."""
    llm = Judge(lambda c: dict(id=c["id"], verdict="supported", **extra))
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(), _source())
    assert result.diagnostic == "invalid_result"


@pytest.mark.parametrize(
    "verdict,diagnostic",
    [("insufficient", "insufficient_evidence"), ("contradicted", "contradicted_evidence")],
)
async def test_음성판정은_null번호를_허용하고_기존unsupported로_처리한다(
    verdict: str, diagnostic: str
) -> None:
    """불충분·반대 판정에 원문 번호를 억지로 요구하지 않는다."""
    llm = Judge(
        lambda c: dict(id=c["id"], verdict=verdict, evidence_index=None, segment_index=None)
    )
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(), _source())
    assert result.reason == "unsupported_claim" and result.diagnostic == diagnostic


@pytest.mark.parametrize(
    "verdict,diagnostic",
    [("insufficient", "insufficient_evidence"), ("contradicted", "contradicted_evidence")],
)
async def test_정확한두필드_음성판정은_근거를요구하지않고_의미보류한다(
    verdict: str, diagnostic: str
) -> None:
    """실제 모델의 올바른 negative를 원문 번호 누락 오류로 오분류하지 않는다."""
    llm = Judge(lambda c: dict(id=c["id"], verdict=verdict))
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(), _source())
    assert result.reason == "unsupported_claim" and result.diagnostic == diagnostic


@pytest.mark.parametrize(
    "row,diagnostic",
    [
        (dict(id=0, verdict="supported"), "invalid_result"),
        (dict(id=0, verdict="insufficient", unknown=True), "invalid_result"),
        (dict(id=True, verdict="contradicted"), "invalid_id"),
        (dict(id=0, verdict="unknown"), "invalid_verdict"),
    ],
)
async def test_두필드지원이나_추가필드나_잘못된id판정은_여전히거부한다(
    row: dict[str, Any], diagnostic: str
) -> None:
    """negative 허용을 supported의 근거 생략이나 모호한 응답 허용으로 넓히지 않는다."""
    llm = Judge(lambda _c: row)
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(), _source())
    assert result.reason == "grounding_unavailable" and result.diagnostic == diagnostic


async def test_두필드보류형도_이미나온id를_재사용할수없다() -> None:
    """서로 다른 claim의 판정 수를 중복 id로 채우지 못하게 기존 검사를 유지한다."""
    llm = Judge(
        lambda c: (
            dict(id=0, verdict="supported", evidence_index=0, segment_index=1)
            if c["id"] == 0
            else dict(id=0, verdict="insufficient")
        )
    )
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(
        _page() + f"다시 검토 중이다. {_MARKER}\n", _source()
    )
    assert result.reason == "grounding_unavailable" and result.diagnostic == "invalid_id"


@pytest.mark.parametrize(
    "quote,passed,diagnostic",
    [
        ("제가 검토하겠습니다.", True, None),
        ("만들어진 인용입니다.", False, "quote_not_in_evidence"),
        (None, False, "missing_quote"),
        (12, False, "missing_quote"),
    ],
)
async def test_기존quote형응답의_정확인용_계약을_바꾸지_않는다(
    quote: Any, passed: bool, diagnostic: str | None
) -> None:
    """기존 정상·위조·누락 quote는 동일한 기준으로 판정한다."""
    llm = Judge(lambda c: dict(id=c["id"], verdict="supported", quote=quote))
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_page(), _source())
    assert result.passed is passed and result.diagnostic == diagnostic


async def test_구조없는_기존text근거는_segment0만_가리킬수있다() -> None:
    """legacy EvidenceSource에 가상의 화자나 여러 발화를 만들지 않는다."""
    source = InMemoryCitationVerifier({("witness_sample", 10): "예산은 미정입니다."})
    good = Judge(
        lambda c: dict(id=c["id"], verdict="supported", evidence_index=0, segment_index=0)
    )
    assert (
        await LocalClaimGrounder(good, WikiGroundingConfig()).verify(
            _page("예산은 미정이다."), source
        )
    ).passed
    legacy_item = json.loads(good.calls[0]["user_prompt"])["claims"][0]["evidence"][0]
    assert legacy_item["evidence_index"] == 0 and legacy_item["segment_index"] == 0
    assert "segments" not in legacy_item
    bad = Judge(lambda c: dict(id=c["id"], verdict="supported", evidence_index=0, segment_index=1))
    assert (
        await LocalClaimGrounder(bad, WikiGroundingConfig()).verify(
            _page("예산은 미정이다."), source
        )
    ).diagnostic == "invalid_evidence_reference"


def test_선택원문은_공백과개행을포함해_그대로복사하고_원래배열은_변경하지_않는다() -> None:
    """출력 quote를 생성·정규화·절단하지 않고 선택한 원문을 가져온다."""
    text = '  원문 "표현"을 유지합니다.\n' + "가" * 300 + "  "
    evidence = [
        dict(
            citation=_MARKER,
            meeting_id="witness_sample",
            text=text,
            segments=[dict(start=10.1, end=11.0, speaker_id="SPEAKER_01", text=text)],
        )
    ]
    before = deepcopy(evidence)
    witness = resolve_grounding_witness(
        dict(id=0, verdict="supported", evidence_index=0, segment_index=0), evidence
    )
    assert (
        witness.quote == text and witness.evidence[0]["segments"][0] == evidence[0]["segments"][0]
    )
    witness.evidence[0]["segments"][0]["text"] = "복사본만 수정"
    assert evidence == before


async def test_잘못선택한화자를_다른source의_동일문장으로_구제하지_않는다() -> None:
    """같은 원문이 두 화자에게 있어도 선택한 source의 실제 화자로 검수한다."""
    text = "제가 검토하겠습니다."
    source = UtterancesCitationVerifier(
        {
            "witness_sample": [
                dict(start=10.1, end=10.4, speaker="SPEAKER_00", text=text),
                dict(start=10.5, end=11, speaker="SPEAKER_01", text=text),
            ]
        }
    )
    wrong = Judge(
        lambda c: dict(id=c["id"], verdict="supported", evidence_index=0, segment_index=0)
    )
    page = _page("- [ ] SPEAKER_01: 검토하기로 했다.")
    result = await LocalClaimGrounder(wrong, WikiGroundingConfig()).verify(page, source)
    assert result.diagnostic == "quote_actor_mismatch"
    assert (await LocalClaimGrounder(Judge(), WikiGroundingConfig()).verify(page, source)).passed


async def test_다른화자의_명시배정은_일치hint가false여도_실제배정문구로_검수한다() -> None:
    """boolean 힌트가 의미 판정을 대체하지 않으며 기존 명시 배정 계약을 유지한다."""
    text = "SPEAKER_01이 검토하기로 했습니다."
    source = UtterancesCitationVerifier(
        {"witness_sample": [dict(start=10, end=11, speaker="SPEAKER_00", text=text)]}
    )
    llm = Judge(lambda c: dict(id=c["id"], verdict="supported", evidence_index=0, segment_index=0))
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(
        _page("- SPEAKER_01: 검토하기로 했다."), source
    )
    assert result.passed
    assert (
        json.loads(llm.calls[0]["user_prompt"])["claims"][0]["evidence"][0]["segments"][0][
            "quote_actor_matches"
        ]
        is False
    )


@pytest.mark.parametrize("batch_size", [1, 6])
async def test_같은근거cache를_공유해도_claim별화자hint가_서로오염되지_않는다(
    batch_size: int,
) -> None:
    """원래 source와 다른 claim의 boolean을 변경하거나 소유자를 추측하지 않는다."""
    raw = dict(
        meeting_id="witness_sample",
        text="제가 검토하겠습니다.",
        segments=[
            dict(start=10.1, end=10.4, speaker_id="SPEAKER_00", text="제가 검토하겠습니다."),
            dict(start=10.5, end=11.0, speaker_id="SPEAKER_01", text="제가 검토하겠습니다."),
        ],
    )
    before = deepcopy(raw)

    class SharedSource:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch_evidence(self, *_args: Any) -> dict[str, Any]:
            self.calls += 1
            return raw

        async def fetch_utterance(self, *_args: Any) -> str:
            raise AssertionError("구조 근거를 text로 대체하지 않는다")

    llm = Judge(
        lambda c: dict(
            id=c["id"],
            verdict="supported",
            evidence_index=0,
            segment_index=int(c["required_quote_actor"][-1]),
        )
    )
    source = SharedSource()
    page = f"---\ntype: action_items\n---\n- [ ] SPEAKER_00: 검토하기로 했다. {_MARKER}\n- [ ] SPEAKER_01: 검토하기로 했다. {_MARKER}\n"
    result = await LocalClaimGrounder(llm, WikiGroundingConfig(batch_size=batch_size)).verify(
        page, source
    )
    assert result.passed and source.calls == 1 and raw == before
    claims = [c for call in llm.calls for c in json.loads(call["user_prompt"])["claims"]]
    assert [c["required_quote_actor"] for c in claims] == ["SPEAKER_00", "SPEAKER_01"]
    assert [[s["quote_actor_matches"] for s in c["evidence"][0]["segments"]] for c in claims] == [
        [True, False],
        [False, True],
    ]


async def test_담당자가없는_주장은_raw언급만으로_소유자를_추정하지_않는다() -> None:
    llm = Judge()
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(
        _page("검토 중이다."), _source()
    )
    assert result.passed
    claim = json.loads(llm.calls[0]["user_prompt"])["claims"][0]
    assert "required_quote_actor" not in claim
    assert all("quote_actor_matches" not in s for s in claim["evidence"][0]["segments"])


async def test_화자hint의_추가크기도_기존근거예산에_포함한다() -> None:
    candidates = []
    for length in range(1, 150):
        text = "가" * length
        item = dict(
            meeting_id="witness_sample",
            text=text,
            segments=[dict(speaker_id="SPEAKER_00", start=10.0, end=11.0, text=text)],
        )
        annotated = deepcopy(item)
        annotated["segments"][0]["quote_actor_matches"] = True
        if (
            len(json.dumps(item, ensure_ascii=False))
            <= 256
            < len(json.dumps(annotated, ensure_ascii=False))
        ):
            candidates.append(text)
    assert candidates
    source = UtterancesCitationVerifier(
        {"witness_sample": [dict(start=10, end=11, speaker="SPEAKER_00", text=candidates[0])]}
    )
    llm = Judge()
    result = await LocalClaimGrounder(llm, WikiGroundingConfig(max_item_chars=256)).verify(
        _page("- SPEAKER_00: 검토한다."), source
    )
    assert result.diagnostic == "evidence_limit" and not llm.calls


def _closure_page() -> str:
    original = OpenActionItem(
        item_id="witness-action",
        owner="SPEAKER_00",
        description="보고서를 공유한다.",
        from_meeting_id="assignment",
        from_date="2026-01-01",
        citation=Citation("assignment", "00:00:10", 10),
    )
    closed = ClosedActionItem(
        original=original,
        closed_by_speaker="SPEAKER_01",
        closed_at_meeting_id="witness_sample",
        closed_citation=Citation("witness_sample", "00:00:10", 10),
    )
    return "---\ntype: action_items\n---\n" + "\n".join(
        ActionItemExtractor._render_closed_block(closed)
    )


@pytest.mark.parametrize("wrong_reporter", [False, True])
async def test_번호형종료근거도_실제reporter와_맞아야한다(wrong_reporter: bool) -> None:
    text = "보고서 공유를 완료했습니다."
    source = UtterancesCitationVerifier(
        {
            "assignment": [
                dict(start=10, end=11, speaker="SPEAKER_00", text="보고서를 공유하겠습니다.")
            ],
            "witness_sample": [
                dict(start=10.1, end=10.4, speaker="SPEAKER_00", text=text),
                dict(start=10.5, end=11, speaker="SPEAKER_01", text=text),
            ],
        }
    )
    llm = Judge(
        lambda c: dict(
            id=c["id"],
            verdict="supported",
            evidence_index=0,
            segment_index=(0 if wrong_reporter else 1) if "closure_context" in c else 0,
        )
    )
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_closure_page(), source)
    assert result.passed is not wrong_reporter
    if wrong_reporter:
        assert result.diagnostic == "closure_reporter_mismatch"
    claims = json.loads(llm.calls[0]["user_prompt"])["claims"]
    closure = next(c for c in claims if "closure_context" in c)
    assert closure["required_quote_actor"] == "SPEAKER_01"
    assert [s["quote_actor_matches"] for s in closure["evidence"][0]["segments"]] == [False, True]


async def test_번호를썼다고_맞장구를_완료근거로_허용하지_않는다() -> None:
    source = UtterancesCitationVerifier(
        {
            "assignment": [
                dict(start=10, end=11, speaker="SPEAKER_00", text="보고서를 공유하겠습니다.")
            ],
            "witness_sample": [
                dict(start=10, end=11, speaker="SPEAKER_01", text="네 알겠습니다.")
            ],
        }
    )
    llm = Judge(lambda c: dict(id=c["id"], verdict="supported", evidence_index=0, segment_index=0))
    result = await LocalClaimGrounder(llm, WikiGroundingConfig()).verify(_closure_page(), source)
    assert result.diagnostic == "closure_ack_only"
