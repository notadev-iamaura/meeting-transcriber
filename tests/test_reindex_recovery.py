"""삭제 rollback에서 검색 인덱스를 복구하는 산출물 폴백 계약."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig, PathsConfig
from core.meeting_mutation import MeetingMutationCoordinator
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
    chunked.to_dict.return_value = {"chunks": [], "meeting_id": meeting_id}
    embedded = MagicMock(total_chunks=1, chroma_stored=1, fts_stored=1)
    embedded.to_dict.return_value = {"chunks": [], "meeting_id": meeting_id}
    coordinator = MeetingMutationCoordinator()

    with (
        patch("steps.chunker.Chunker.chunk", new=AsyncMock(return_value=chunked)) as chunk,
        patch("steps.embedder.Embedder.embed", new=AsyncMock(return_value=embedded)) as embed,
    ):
        result = await reindex_meeting_artifacts(
            config,
            object(),
            meeting_id,
            meeting_mutation_coordinator=coordinator,
        )

    chunk.assert_awaited_once()
    corrected_arg = chunk.await_args.args[0]
    assert corrected_arg.utterances[0].text == "복구할 회의입니다."
    embed.assert_awaited_once_with(chunked)
    assert result == {"chunks": 1, "chroma_stored": 1, "fts_stored": 1}


@pytest.mark.asyncio
async def test_checkpoint_회의_dir_symlink는_외부_chunk_embed를_쓰지_않는다(
    tmp_path: Path,
) -> None:
    """정적 checkpoint directory symlink를 모델/index 실행 전에 차단한다."""
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path / "base")))
    meeting_id = "meeting_checkpoint_link"
    corrected_output = config.paths.resolved_outputs_dir / meeting_id / "corrected.json"
    CorrectedResult(
        utterances=[
            CorrectedUtterance(
                text="안전한 회의",
                original_text="안전한 회의",
                speaker="SPEAKER_00",
                start=0.0,
                end=1.0,
            )
        ],
        num_speakers=1,
        audio_path="",
    ).save_checkpoint(corrected_output)
    checkpoints_root = config.paths.resolved_checkpoints_dir
    checkpoints_root.mkdir(parents=True)
    external = tmp_path / "external-checkpoints"
    external.mkdir()
    external_chunk = external / "chunk.json"
    external_embed = external / "embed.json"
    external_chunk.write_text("chunk-sentinel", encoding="utf-8")
    external_embed.write_text("embed-sentinel", encoding="utf-8")
    (checkpoints_root / meeting_id).symlink_to(external, target_is_directory=True)
    coordinator = MeetingMutationCoordinator()

    with (
        patch("steps.chunker.Chunker.chunk", new_callable=AsyncMock) as chunk,
        patch("steps.embedder.Embedder.embed", new_callable=AsyncMock) as embed,
    ):
        with pytest.raises(OSError):
            await reindex_meeting_artifacts(
                config,
                object(),
                meeting_id,
                meeting_mutation_coordinator=coordinator,
            )

    chunk.assert_not_awaited()
    embed.assert_not_awaited()
    assert external_chunk.read_text(encoding="utf-8") == "chunk-sentinel"
    assert external_embed.read_text(encoding="utf-8") == "embed-sentinel"


@pytest.mark.asyncio
async def test_output_회의_dir_symlink는_외부_corrected를_읽지_않는다(
    tmp_path: Path,
) -> None:
    """output meeting directory symlink source도 no-follow로 거부한다."""
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path / "base")))
    meeting_id = "meeting_output_link"
    outputs_root = config.paths.resolved_outputs_dir
    outputs_root.mkdir(parents=True)
    external = tmp_path / "external-output"
    external.mkdir()
    victim = external / "corrected.json"
    victim.write_text("external-sentinel", encoding="utf-8")
    (outputs_root / meeting_id).symlink_to(external, target_is_directory=True)
    coordinator = MeetingMutationCoordinator()

    with (
        patch("steps.chunker.Chunker.chunk", new_callable=AsyncMock) as chunk,
        patch("steps.embedder.Embedder.embed", new_callable=AsyncMock) as embed,
    ):
        with pytest.raises(OSError):
            await reindex_meeting_artifacts(
                config,
                object(),
                meeting_id,
                meeting_mutation_coordinator=coordinator,
            )

    chunk.assert_not_awaited()
    embed.assert_not_awaited()
    assert victim.read_text(encoding="utf-8") == "external-sentinel"


@pytest.mark.asyncio
async def test_reindex_전체가_회의_mutation_lease를_유지한다(tmp_path: Path) -> None:
    """source read부터 checkpoint 게시까지 재전사와 겹치지 않는다."""
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    meeting_id = "meeting_reindex_lease"
    output = config.paths.resolved_outputs_dir / meeting_id / "corrected.json"
    CorrectedResult(
        utterances=[
            CorrectedUtterance(
                text="직렬화할 회의",
                original_text="직렬화할 회의",
                speaker="SPEAKER_00",
                start=0.0,
                end=1.0,
            )
        ],
        num_speakers=1,
        audio_path="",
    ).save_checkpoint(output)
    coordinator = MeetingMutationCoordinator()
    chunk_entered = asyncio.Event()
    release_chunk = asyncio.Event()
    competing_entered = asyncio.Event()
    chunked = MagicMock()
    chunked.to_dict.return_value = {"chunks": [], "meeting_id": meeting_id}
    embedded = MagicMock(total_chunks=0, chroma_stored=True, fts_stored=True)
    embedded.to_dict.return_value = {"chunks": [], "meeting_id": meeting_id}

    async def _delayed_chunk(*_args: object, **_kwargs: object) -> MagicMock:
        chunk_entered.set()
        await release_chunk.wait()
        return chunked

    async def _competing_mutation() -> None:
        async with coordinator.lease(meeting_id):
            competing_entered.set()

    with (
        patch("steps.chunker.Chunker.chunk", new=AsyncMock(side_effect=_delayed_chunk)),
        patch("steps.embedder.Embedder.embed", new=AsyncMock(return_value=embedded)),
    ):
        reindex_task = asyncio.create_task(
            reindex_meeting_artifacts(
                config,
                object(),
                meeting_id,
                meeting_mutation_coordinator=coordinator,
            )
        )
        await chunk_entered.wait()
        competing_task = asyncio.create_task(_competing_mutation())
        await asyncio.sleep(0)
        assert competing_entered.is_set() is False
        release_chunk.set()
        await asyncio.gather(reindex_task, competing_task)

    assert competing_entered.is_set() is True
