"""교정 실패의 원문 결합 추적과 작은 범위 재시도를 검증한다."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import AppConfig
from core.llm_backend import LLMConnectionError, LLMGenerationError
from steps.corrector import (
    CorrectedResult,
    CorrectedUtterance,
    CorrectionError,
    CorrectionFailure,
    Corrector,
    _find_suspicious_correction_reason,
    correction_source_sha256,
)
from steps.merger import MergedResult, MergedUtterance


def original(text: str, index: int = 0) -> CorrectedUtterance:
    """화자와 시간까지 고정된 가상 원문을 만든다."""
    return CorrectedUtterance(text, text, "SPEAKER_00", index * 5.0, index * 5.0 + 4.0)


def failure(index: int, utterance: CorrectedUtterance) -> CorrectionFailure:
    """원문으로 확인한 가상 실패 기록을 만든다."""
    return CorrectionFailure(index, "missing_number", correction_source_sha256(utterance))


def corrector(backend: MagicMock, *, batch_size: int = 4) -> tuple[Corrector, MagicMock]:
    """실제 모델을 읽지 않는 매니저와 교정기를 만든다."""
    manager = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=backend)
    context.__aexit__ = AsyncMock(return_value=False)
    manager.acquire.return_value = context
    worker = Corrector(
        AppConfig(llm={"correction_mode": "full", "correction_batch_size": batch_size}),
        manager,
    )
    worker._build_system_prompt = MagicMock(return_value="가상 시스템 프롬프트")
    return worker, manager


@pytest.mark.parametrize("prefix", ["네", "예", "그렇습니다", "좋습니다", "감사합니다"])
def test_long_preserved_sentence_is_not_generic_short_rewrite(prefix: str) -> None:
    """인사말로 시작해도 원문 내용을 보존하는 긴 교정문은 접두사만으로 거부하지 않는다."""
    text = f"{prefix} 다음주 배포 일정과 담당자를 함께 검토하겠습니다"
    batch = [MergedUtterance(text, "SPEAKER_00", 0.0, 5.0)]
    candidate = f"{prefix}, 다음 주 배포 일정과 담당자를 함께 검토하겠습니다."
    assert _find_suspicious_correction_reason(batch, idx=1, candidate=candidate) is None


@pytest.mark.parametrize("candidate", ["네", "네, 알겠습니다.", "그 부분에", "감사합니다"])
def test_short_filler_still_cannot_replace_substantive_source(candidate: str) -> None:
    """짧은 정형 응답으로 긴 원문을 지우는 결과는 계속 거부한다."""
    batch = [
        MergedUtterance(
            "배포 담당자와 구체적인 검증 일정을 다음 회의 전에 확인하겠습니다", "S0", 0.0, 5.0
        )
    ]
    assert _find_suspicious_correction_reason(batch, idx=1, candidate=candidate) is not None


def test_final_selected_branch_only_records_failures() -> None:
    """중간 changed-only 거부 기록이 성공한 full 폴백의 실패로 남지 않는다."""
    backend = MagicMock()
    worker, _ = corrector(backend)
    worker._config.llm.correction_mode = "changed_only"
    batch = [
        MergedUtterance(
            f"업무 항목 {i}의 담당자와 검증 일정을 다음 회의에 확인합니다", "S0", i, i + 1
        )
        for i in range(4)
    ]
    backend.chat.side_effect = [
        "[1] 네\n[2] 네",
        "\n".join(f"[{i}] {u.text}" for i, u in enumerate(batch, 1)),
    ]
    details: list[CorrectionFailure] = []
    _, _, failed = worker._correct_batch(backend, batch, "시스템", failure_details=details)
    assert failed == 0 and details == [] and backend.chat.call_count == 2


def test_failure_tracking_roundtrip_and_legacy_unknown(tmp_path: Path) -> None:
    """명시 기록은 복원하고 오래된 실패 집계만으로 위치를 만들지 않는다."""
    u = original("교정하지 않은 원문입니다")
    result = CorrectedResult(
        [u], 1, "source.wav", total_failed=1, correction_failures=[failure(0, u)]
    )
    path = tmp_path / "correct.json"
    result.save_checkpoint(path)
    restored = CorrectedResult.from_checkpoint(path)
    assert restored.failed_indices == [0] and restored.failure_tracking_complete
    legacy = replace(result, correction_failures=[])
    assert legacy.failed_indices == [] and not legacy.failure_tracking_complete


@pytest.mark.asyncio
async def test_retry_changes_only_audited_failures_and_accepts_unchanged_source() -> None:
    """성공 교정과 사용자 수정은 보존하고 정상 원문 유지도 처리 성공으로 센다."""
    backend = MagicMock()
    worker, _ = corrector(backend)
    rows = [
        original("오늘 화의를 검토하겠습니다", 0),
        replace(original("이전 기록", 1), text="사용자가 수정한 기록"),
        replace(original("별도 화의", 2), text="별도 회의", was_corrected=True),
        original("다음 주 담당자를 확인합니다", 3),
    ]
    result = CorrectedResult(rows, 1, "source.wav", total_corrected=1, total_failed=2)
    manifest = [failure(0, rows[0]), failure(3, rows[3])]
    before = [asdict(row) for row in rows]
    backend.chat.return_value = "[1] 오늘 회의를 검토하겠습니다\n[2] 다음 주 담당자를 확인합니다"
    updated = await worker.retry_failed(result, failures=manifest)
    assert backend.chat.call_count == 1
    assert updated.total_failed == 0 and updated.total_corrected == 2
    assert updated.correction_failures == [] and updated.failure_tracking_complete
    assert asdict(updated.utterances[1]) == before[1]
    assert asdict(updated.utterances[2]) == before[2]
    assert updated.utterances[3].was_corrected is False
    assert [asdict(row) for row in result.utterances] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["hash", "text", "speaker", "time", "duplicate", "missing"])
async def test_invalid_retry_manifest_blocks_before_model(tamper: str) -> None:
    """해시·원문·메타데이터·범위가 바뀐 재시도는 모델 적재 전에 차단한다."""
    worker, manager = corrector(MagicMock())
    u = original("원문을 그대로 보존합니다")
    result = CorrectedResult([u], 1, "source.wav", total_failed=1)
    manifest = [failure(0, u)]
    if tamper == "hash":
        manifest[0] = replace(manifest[0], source_sha256="0" * 64)
    elif tamper == "text":
        result.utterances[0] = replace(u, text="사용자 수정")
    elif tamper == "speaker":
        result.utterances[0] = replace(u, speaker="SPEAKER_02")
    elif tamper == "time":
        result.utterances[0] = replace(u, start=1.0)
    elif tamper == "duplicate":
        result.total_failed = 2
        manifest *= 2
    else:
        manifest = []
    with pytest.raises(CorrectionError):
        await worker.retry_failed(result, failures=manifest)
    manager.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_retry_guard_keeps_adjacent_text_copy_rejected() -> None:
    """작은 배치로 재시도해도 원래 배치의 이웃 문장을 가져오지 못한다."""
    backend = MagicMock()
    worker, _ = corrector(backend)
    rows = [
        original("오늘은 배포 일정부터 논의하겠습니다", 0),
        original("다음 주 검증 담당자는 따로 확인하겠습니다", 1),
    ]
    backend.chat.return_value = f"[1] {rows[1].text}"
    result = CorrectedResult(rows, 1, "source.wav", total_failed=1)
    updated = await worker.retry_failed(result, failures=[failure(0, rows[0])])
    assert updated.total_failed == 1 and updated.utterances[0].text == rows[0].text
    assert updated.correction_failures[0].reason.startswith("line_")
    assert backend.chat.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [LLMConnectionError("synthetic"), LLMGenerationError("synthetic")]
)
async def test_retry_backend_failure_preserves_source_without_unbounded_retry(
    error: Exception,
) -> None:
    """백엔드 실패에서도 한 번만 요청하고 원문과 실패 기록을 유지한다."""
    backend = MagicMock()
    worker, _ = corrector(backend)
    row = original("업무 검토 기록을 유지하겠습니다")
    backend.chat.side_effect = error
    result = CorrectedResult([row], 1, "source.wav", total_failed=1)
    updated = await worker.retry_failed(result, failures=[failure(0, row)])
    assert updated.total_failed == 1 and updated.utterances[0] == row
    assert backend.chat.call_count == 1


@pytest.mark.asyncio
async def test_normal_pipeline_retries_only_missing_row_once() -> None:
    """새 회의의 일반 교정도 누락된 발화만 작은 요청으로 한 번 복구한다."""
    backend = MagicMock()
    worker, _ = corrector(backend, batch_size=2)
    rows = [
        MergedUtterance("오늘 회의를 시작하겠습니다", "S0", 0, 4),
        MergedUtterance("다음 주 검증 담당자를 확인합니다", "S0", 5, 9),
    ]
    backend.chat.side_effect = [f"[1] {rows[0].text}", f"[1] {rows[1].text}"]
    result = await worker.correct(MergedResult(rows, 1, "source.wav"))
    assert result.total_failed == 0 and result.failed_indices == []
    assert result.failure_tracking_complete and backend.chat.call_count == 2
    assert rows[0].text not in backend.chat.call_args.kwargs["messages"][1]["content"]
    assert rows[1].text in backend.chat.call_args.kwargs["messages"][1]["content"]


def test_guard_log_does_not_expose_transcript_or_candidate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """거절 사유를 기록하되 원문과 모델 교정 후보는 로그에 넣지 않는다."""
    backend = MagicMock()
    worker, _ = corrector(backend)
    source = "PRIVATE_SOURCE_CONTENT_MUST_NOT_APPEAR_IN_LOGS"
    backend.chat.return_value = "[1] 네"
    worker._correct_batch(backend, [MergedUtterance(source, "S0", 0, 4)], "시스템")
    assert "destructive_shortening" in caplog.text and source not in caplog.text


@pytest.mark.parametrize(
    ("source", "candidate", "reason"),
    [
        ("전체 121건을 확인했습니다", "전체 12건을 확인했습니다", "numeric_changed"),
        ("현재 5%입니다", "현재 5입니다", "numeric_changed"),
        ("온도가 -5도입니다", "온도가 5도입니다", "numeric_changed"),
        ("버전 1.20입니다", "버전 1.2입니다", "numeric_changed"),
        ("큰 차이는 없긴 한데요", "큰 차이는 있긴 한데요", "negation_changed"),
        ("이번에는 하지 않았습니다", "이번에는 했습니다", "negation_changed"),
        ("오늘은 안 하겠습니다", "오늘은 하겠습니다", "negation_changed"),
        ("오늘은 안했어요", "오늘은 했어요", "negation_changed"),
        ("이 상태는 안좋아요", "이 상태는 좋아요", "negation_changed"),
        ("이번 일정은 안 늦습니다", "이번 일정은 늦습니다", "negation_changed"),
        ("문제는 있습니다", "문제는 없습니다", "negation_changed"),
    ],
)
def test_factual_numeric_or_negation_changes_are_held(
    source: str, candidate: str, reason: str
) -> None:
    """숫자나 긍정·부정을 추측해서 바꾸는 교정은 보류한다."""
    batch = [MergedUtterance(source, "S0", 0.0, 5.0)]
    assert _find_suspicious_correction_reason(batch, idx=1, candidate=candidate) == reason


@pytest.mark.parametrize(
    ("source", "candidate"),
    [
        ("총 1,000건 확인했습니다", "총 1000건 확인했습니다."),
        ("현재 120프로 증가했습니다", "현재 120% 증가했습니다."),
        ("온도는 - 5도입니다", "온도는 -5도입니다."),
        ("오늘은 안했어요", "오늘은 안 했어요."),
        ("주요안건은 다음주 일정입니다", "주요 안건은 다음 주 일정입니다."),
        ("안내 내용을 확인했습니다", "안내 내용을 확인했습니다."),
        ("안전 절차를 검토합니다", "안전 절차를 검토합니다."),
    ],
)
def test_safe_spacing_punctuation_and_thousands_format_are_allowed(
    source: str, candidate: str
) -> None:
    """값과 부정을 보존하는 표현 정리는 허용하고 비부정 안건을 오인하지 않는다."""
    batch = [MergedUtterance(source, "S0", 0.0, 5.0)]
    assert _find_suspicious_correction_reason(batch, idx=1, candidate=candidate) is None


@pytest.mark.asyncio
async def test_retry_prompt_and_semantic_hold_preserve_uncertain_source() -> None:
    """의미 검수 보류도 추적하며 불확실한 수치·명칭·조건 보존을 지시한다."""
    backend = MagicMock()
    worker, _ = corrector(backend)
    row = original("PIPs 121건은 확인이 필요할 수도 있습니다")
    backend.chat.return_value = f"[1] {row.text}"
    result = CorrectedResult([row], 1, "source.wav", total_failed=1)
    issue = replace(failure(0, row), reason="semantic_review_hold")
    updated = await worker.retry_failed(result, failures=[issue])
    system = backend.chat.call_args.kwargs["messages"][0]["content"]
    assert all(term in system for term in ("숫자", "고유명사", "부정", "조건", "불확실"))
    assert updated.total_failed == 0 and not updated.utterances[0].was_corrected
