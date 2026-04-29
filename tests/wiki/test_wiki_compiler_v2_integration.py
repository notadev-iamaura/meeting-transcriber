"""WikiCompiler × WikiCompilerV2 연결 TDD 테스트 (Phase 2.E).

검증 범위 (작업 5):
    1. wiki.dry_run=True (기본): 기존 dry_run 동작 (log.md 한 줄 + git commit)
    2. wiki.dry_run=False: WikiCompilerV2.compile_meeting() 호출
    3. summary/utterances 가 V2 까지 전달되어야 함
    4. PRD §5.5 모델 분리: 9단계 진입 시 EXAONE 으로 acquire (8단계 Gemma unload 후)

mock 전략:
    - WikiCompilerV2.compile_meeting 을 patch 해 호출 인자 검증
    - ExaoneWikiClient 는 _create_exaone_backend 를 mock 해 실제 모델 로드 회피
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import AppConfig, WikiConfig


def _build_app_config(
    *,
    enabled: bool,
    root: Path,
    dry_run: bool,
) -> AppConfig:
    """테스트용 AppConfig 를 만든다."""
    wiki = WikiConfig(enabled=enabled, root=root, dry_run=dry_run)
    return AppConfig(wiki=wiki)


# ─────────────────────────────────────────────────────────────────────────
# 1. dry_run=True 기존 동작 회귀 — 변경 없음
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_true_preserves_existing_behavior(tmp_path: Path) -> None:
    """dry_run=True 일 때 기존 log.md 한 줄 동작 유지.

    Phase 2.E 변경 후에도 dry_run=True 면 V2 가 호출되지 않아야 한다.
    """
    from steps.wiki_compiler import WikiCompiler

    root = tmp_path / "wiki"
    cfg = _build_app_config(enabled=True, root=root, dry_run=True)
    compiler = WikiCompiler(cfg)

    result = await compiler.run(meeting_id="aaa11111")

    assert result["status"] == "dry_run"
    log_text = (root / "log.md").read_text(encoding="utf-8")
    assert "aaa11111" in log_text
    assert "dry_run" in log_text


# ─────────────────────────────────────────────────────────────────────────
# 2. dry_run=False 일 때 WikiCompilerV2.compile_meeting 호출
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_false_invokes_wiki_compiler_v2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry_run=False 일 때 V2.compile_meeting 이 호출되어야 한다.

    summary, utterances 가 그대로 V2 에 전달되는지 인자 캡처로 검증.
    """
    from steps.wiki_compiler import WikiCompiler

    root = tmp_path / "wiki"
    cfg = _build_app_config(enabled=True, root=root, dry_run=False)

    # WikiCompilerV2 인스턴스를 mock 으로 대체
    mock_v2 = MagicMock()
    mock_v2.compile_meeting = AsyncMock(
        return_value=MagicMock(
            meeting_id="bbb22222",
            pages_created=["decisions/2026-04-28-test.md"],
            pages_updated=[],
            pages_pending=[],
            pages_rejected=[],
            commit_sha="abcdef1234567890",
            duration_seconds=1.5,
            llm_call_count=2,
        )
    )

    # _create_wiki_compiler_v2 헬퍼를 mock 으로 교체
    # (구현 측이 어떤 이름의 헬퍼/팩토리를 사용하든 wiki_compiler 모듈 내부 함수로 정의)
    monkeypatch.setattr(
        "steps.wiki_compiler._create_wiki_compiler_v2",
        lambda **kwargs: mock_v2,
    )

    compiler = WikiCompiler(cfg)
    result = await compiler.run(
        meeting_id="bbb22222",
        summary="테스트 요약",
        utterances=[],
    )

    # V2 가 호출되었는지 확인
    assert mock_v2.compile_meeting.await_count == 1, (
        f"WikiCompilerV2.compile_meeting 이 호출되지 않았습니다. "
        f"호출 수: {mock_v2.compile_meeting.await_count}"
    )

    # 호출 인자 검증
    call_kwargs = mock_v2.compile_meeting.await_args.kwargs
    assert call_kwargs["meeting_id"] == "bbb22222"
    assert call_kwargs["summary"] == "테스트 요약"
    assert call_kwargs["utterances"] == []
    assert isinstance(call_kwargs["meeting_date"], date)

    # 결과 status 도 확인
    assert result["status"] == "compiled"
    assert result["meeting_id"] == "bbb22222"


# ─────────────────────────────────────────────────────────────────────────
# 3. dry_run=False 면서 summary 가 None 일 때 graceful fallback
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_false_with_none_summary_falls_back_to_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """summary 가 None 이면 V2 호출이 없고 dry_run 폴백.

    PipelineManager 가 8단계 실패로 summary 가 비어있는 경우의 안전장치.
    """
    from steps.wiki_compiler import WikiCompiler

    root = tmp_path / "wiki"
    cfg = _build_app_config(enabled=True, root=root, dry_run=False)

    mock_v2 = MagicMock()
    mock_v2.compile_meeting = AsyncMock()

    monkeypatch.setattr(
        "steps.wiki_compiler._create_wiki_compiler_v2",
        lambda **kwargs: mock_v2,
    )

    compiler = WikiCompiler(cfg)
    result = await compiler.run(meeting_id="ccc33333", summary=None)

    # V2 미호출 — summary 없으므로 dry_run 폴백
    assert mock_v2.compile_meeting.await_count == 0
    assert result["status"] == "dry_run"


# ─────────────────────────────────────────────────────────────────────────
# 4. V2 가 예외를 던지면 non-fatal 로 PipelineError escalate
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v2_failure_escalates_as_pipeline_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V2.compile_meeting 이 예외 → PipelineError 로 wrap.

    PipelineManager 가 catch 후 non-fatal 로 처리한다.
    """
    from core.pipeline import PipelineError
    from steps.wiki_compiler import WikiCompiler

    root = tmp_path / "wiki"
    cfg = _build_app_config(enabled=True, root=root, dry_run=False)

    mock_v2 = MagicMock()
    mock_v2.compile_meeting = AsyncMock(side_effect=RuntimeError("v2 explosion"))

    monkeypatch.setattr(
        "steps.wiki_compiler._create_wiki_compiler_v2",
        lambda **kwargs: mock_v2,
    )

    compiler = WikiCompiler(cfg)

    with pytest.raises(PipelineError):
        await compiler.run(
            meeting_id="ddd44444",
            summary="요약",
            utterances=[],
        )
