"""검색 인덱스 재생성과 crash-recovery marker 소비 로직."""

from __future__ import annotations

import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from core.quarantine import QuarantineError, _open_directory_tree_no_follow


async def reindex_meeting_artifacts(
    config: Any,
    model_manager: Any,
    meeting_id: str,
) -> dict[str, Any]:
    """correct/merge 체크포인트에서 chunk와 두 검색 인덱스를 재생성한다."""
    from steps.chunker import Chunker
    from steps.corrector import CorrectedResult, CorrectedUtterance
    from steps.embedder import Embedder
    from steps.merger import MergedResult

    outputs_dir = config.paths.resolved_outputs_dir
    checkpoints_dir = config.paths.resolved_checkpoints_dir
    corrected_output = outputs_dir / meeting_id / "corrected.json"
    correct_cp = checkpoints_dir / meeting_id / "correct.json"
    merge_cp = checkpoints_dir / meeting_id / "merge.json"
    if corrected_output.exists():
        corrected = CorrectedResult.from_checkpoint(corrected_output)
    elif correct_cp.exists():
        corrected = CorrectedResult.from_checkpoint(correct_cp)
    elif merge_cp.exists():
        merged = MergedResult.from_checkpoint(merge_cp)
        corrected = CorrectedResult(
            utterances=[
                CorrectedUtterance(
                    text=utterance.text,
                    original_text=utterance.text,
                    speaker=utterance.speaker,
                    start=utterance.start,
                    end=utterance.end,
                    was_corrected=False,
                )
                for utterance in merged.utterances
            ],
            audio_path=getattr(merged, "audio_path", ""),
            num_speakers=getattr(merged, "num_speakers", 0),
            total_corrected=0,
        )
    else:
        raise FileNotFoundError(
            f"corrected.json / correct.json / merge.json 산출물이 없습니다: {meeting_id}"
        )

    match = re.search(r"(\d{4})(\d{2})(\d{2})_\d{6}", meeting_id)
    date_str = (
        f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        if match
        else datetime.now().strftime("%Y-%m-%d")
    )
    chunked = await Chunker(config).chunk(corrected, meeting_id, date_str)
    meeting_dir = checkpoints_dir / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)
    chunked.save_checkpoint(meeting_dir / "chunk.json")
    embedded = await Embedder(config, model_manager).embed(chunked)
    embedded.save_checkpoint(meeting_dir / "embed.json")
    return {
        "chunks": embedded.total_chunks,
        "chroma_stored": embedded.chroma_stored,
        "fts_stored": embedded.fts_stored,
    }


def consume_reindex_required_marker(config: Any, meeting_id: str) -> None:
    """성공한 재색인 뒤 해당 회의의 recovery marker를 no-follow로 제거한다."""
    if (
        not meeting_id
        or meeting_id in {".", ".."}
        or "/" in meeting_id
        or "\\" in meeting_id
        or "\x00" in meeting_id
    ):
        raise ValueError("유효하지 않은 회의 ID입니다")
    meeting_dir = Path(config.paths.resolved_checkpoints_dir) / meeting_id
    try:
        directory_fd = _open_directory_tree_no_follow(meeting_dir, create=False)
    except FileNotFoundError:
        return
    except (OSError, QuarantineError) as exc:
        raise ValueError("재색인 marker 경로가 안전하지 않습니다") from exc
    try:
        try:
            entry = os.stat(
                "reindex_required.json",
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if not stat.S_ISREG(entry.st_mode):
            raise ValueError("재색인 marker가 안전한 일반 파일이 아닙니다")
        current = os.stat(
            "reindex_required.json",
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != (entry.st_dev, entry.st_ino):
            raise ValueError("재색인 marker가 처리 중 교체되었습니다")
        os.unlink("reindex_required.json", dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
