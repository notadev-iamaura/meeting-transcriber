"""Zoom 오디오 활동 감지 테스트."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from steps.zoom_activity import ZoomActivityError, ZoomActivityResult, ZoomAudioActivityChecker


@pytest.mark.asyncio
async def test_process_backend_uses_pgrep() -> None:
    """process backend는 기존 pgrep 기반 감지를 사용한다."""
    checker = ZoomAudioActivityChecker(process_name="CptHost", prefer_coreaudio=False)
    mock_proc = AsyncMock()
    mock_proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        result = await checker.check()

    assert result == ZoomActivityResult(active=True, source="pgrep")
    mock_exec.assert_called_once()
    assert mock_exec.call_args.args[:3] == ("pgrep", "-f", "CptHost")


@pytest.mark.asyncio
async def test_coreaudio_backend_parses_helper_json(tmp_path) -> None:
    """CoreAudio helper JSON의 active 값을 감지 결과로 반환한다."""
    source = tmp_path / "zoom_audio_activity.swift"
    binary = tmp_path / "zoom_audio_activity"
    source.write_text("// helper", encoding="utf-8")
    binary.write_text("#!/bin/sh\n", encoding="utf-8")

    checker = ZoomAudioActivityChecker(
        process_name="CptHost",
        helper_source=source,
        helper_binary=binary,
    )
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(
        return_value=(
            b'{"ok":true,"active":true,"processes":[{"pid":123,"input":true}]}',
            b"",
        )
    )

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await checker.check()

    assert result == ZoomActivityResult(active=True, source="coreaudio", process_count=1)


@pytest.mark.asyncio
async def test_coreaudio_failure_falls_back_to_process() -> None:
    """CoreAudio 확인 실패 시 pgrep 폴백 결과를 반환한다."""
    checker = ZoomAudioActivityChecker(process_name="CptHost")

    with (
        patch.object(
            checker,
            "_check_coreaudio",
            AsyncMock(side_effect=ZoomActivityError("boom")),
        ),
        patch.object(
            checker,
            "_check_process_fallback",
            AsyncMock(return_value=ZoomActivityResult(active=True, source="pgrep")),
        ),
    ):
        result = await checker.check()

    assert result == ZoomActivityResult(active=True, source="pgrep")
