"""삭제 rollback에서 검색 인덱스를 복구하는 산출물 폴백 계약."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig, PathsConfig
from core.reindex_recovery import reindex_meeting_artifacts
from steps.corrector import CorrectedResult, CorrectedUtterance


@pytest.mark.asyncio
async def test_output_corrected만_남은_completed회의도_재색인한다(
    tmp_path: Path,
) -> None:
    """viewer가 읽는 최종 output을 delete rollback도 동일하게 사용한다."""
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    meeting_id = "meeting_20260822-120000"
    output = config.paths.resolved_outputs_dir / meeting_id / "corrected.json"
    CorrectedResult(
        utterances=[
            CorrectedUtterance(
                text="복구할 회의입니다.",
                original_text="복구할 회의입니다.",
                speaker="SPEAKER_00",
                start=0.0,
                end=1.0,
            )
        ],
        num_speakers=1,
        audio_path="",
    ).save_checkpoint(output)
    chunked = MagicMock()
    embedded = MagicMock(total_chunks=1, chroma_stored=1, fts_stored=1)

    with (
        patch("steps.chunker.Chunker.chunk", new=AsyncMock(return_value=chunked)) as chunk,
        patch("steps.embedder.Embedder.embed", new=AsyncMock(return_value=embedded)) as embed,
    ):
        result = await reindex_meeting_artifacts(config, object(), meeting_id)

    chunk.assert_awaited_once()
    corrected_arg = chunk.await_args.args[0]
    assert corrected_arg.utterances[0].text == "복구할 회의입니다."
    embed.assert_awaited_once_with(chunked)
    assert result == {"chunks": 1, "chroma_stored": 1, "fts_stored": 1}
