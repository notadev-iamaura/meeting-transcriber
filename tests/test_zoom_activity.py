"""Zoom 오디오 활동 감지 테스트."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, patch

import pytest

from steps.zoom_activity import (
    ZoomActivityError,
    ZoomActivityResult,
    ZoomAudioActivityChecker,
    _read_helper_output,
)


async def test_cancelled_topic_lookup_reaps_helper() -> None:
    """수동 녹음 정지 시 아직 응답하지 않은 조회 프로세스도 회수한다."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(10)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    task = asyncio.create_task(_read_helper_output(process, 3))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None


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
