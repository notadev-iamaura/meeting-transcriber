"""검색 인덱스 재생성과 crash-recovery marker 소비 로직."""

from __future__ import annotations

import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from core.io_utils import (
    atomic_write_json_pinned,
    ensure_directory_no_follow,
    read_text_no_follow,
)
from core.meeting_mutation import MeetingMutationCoordinator
from core.quarantine import QuarantineError, _open_directory_tree_no_follow


def _validate_meeting_id(meeting_id: str) -> None:
    """재색인 경로에 사용할 안전한 단일 segment 회의 ID를 검증한다."""
    if (
        not isinstance(meeting_id, str)
        or not meeting_id
        or meeting_id in {".", ".."}
        or "\x00" in meeting_id
        or "\\" in meeting_id
        or Path(meeting_id).name != meeting_id
    ):
        raise ValueError(f"유효하지 않은 회의 ID입니다: {meeting_id!r}")


def _configured_storage_root(config: Any, field_name: str, fallback: Path) -> Path:
    """resolve()가 숨길 수 있는 storage symlink를 보존한 lexical root를 만든다."""
    raw_base = getattr(config.paths, "base_dir", None)
    raw_child = getattr(config.paths, field_name, None)
    if isinstance(raw_base, (str, Path)) and isinstance(raw_child, (str, Path)):
        lexical_base = Path(raw_base).expanduser().absolute()
        child = Path(raw_child).expanduser()
        if child == Path(".") or ".." in child.parts or "\x00" in str(child):
            raise ValueError(f"{field_name}은 base_dir 하위 상대경로여야 합니다")
        candidate = child.absolute() if child.is_absolute() else (lexical_base / child).absolute()
        try:
            relative = candidate.relative_to(lexical_base)
        except ValueError as exc:
            raise ValueError(f"{field_name}이 base_dir 밖을 가리킵니다: {candidate}") from exc
        if not relative.parts:
            raise ValueError(f"{field_name}은 base_dir 하위 경로여야 합니다")
        return candidate
    return Path(fallback).expanduser().absolute()


def has_reindex_source_artifact(config: Any, meeting_id: str) -> bool:
    """재색인 가능한 source가 안전한 일반 파일로 존재하는지 no-follow 확인한다."""
    _validate_meeting_id(meeting_id)
    outputs_dir = _configured_storage_root(
        config,
        "outputs_dir",
        config.paths.resolved_outputs_dir,
    )
    checkpoints_dir = _configured_storage_root(
        config,
        "checkpoints_dir",
        config.paths.resolved_checkpoints_dir,
    )
    candidates = (
        outputs_dir / meeting_id / "corrected.json",
        checkpoints_dir / meeting_id / "correct.json",
        checkpoints_dir / meeting_id / "merge.json",
    )
    for candidate in candidates:
        try:
            read_text_no_follow(candidate)
        except FileNotFoundError:
            continue
        return True
    return False


async def reindex_meeting_artifacts(
    config: Any,
    model_manager: Any,
    meeting_id: str,
    *,
    meeting_mutation_coordinator: MeetingMutationCoordinator,
) -> dict[str, Any]:
    """회의별 lease 안에서 chunk 체크포인트와 두 검색 인덱스를 재생성한다."""
    if not isinstance(meeting_mutation_coordinator, MeetingMutationCoordinator):
        raise TypeError("회의 mutation coordinator가 필요합니다")
    _validate_meeting_id(meeting_id)
    async with meeting_mutation_coordinator.lease(meeting_id):
        return await _reindex_meeting_artifacts_locked(config, model_manager, meeting_id)


async def _reindex_meeting_artifacts_locked(
    config: Any,
    model_manager: Any,
    meeting_id: str,
) -> dict[str, Any]:
    """호출자가 회의별 lease를 보유한 상태에서 재색인 본문을 실행한다."""
    from steps.chunker import Chunker
    from steps.corrector import CorrectedResult, CorrectedUtterance
    from steps.embedder import Embedder
    from steps.merger import MergedResult

    outputs_dir = _configured_storage_root(
        config,
        "outputs_dir",
        config.paths.resolved_outputs_dir,
    )
    checkpoints_dir = _configured_storage_root(
        config,
        "checkpoints_dir",
        config.paths.resolved_checkpoints_dir,
    )
    meeting_dir = checkpoints_dir / meeting_id
    # source를 읽거나 모델/인덱스를 실행하기 전에 전체 lexical dir chain을
    # no-follow로 고정 검증한다. 정적 meeting-dir symlink도 여기서 차단된다.
    ensure_directory_no_follow(meeting_dir)

    corrected_output = outputs_dir / meeting_id / "corrected.json"
    correct_cp = meeting_dir / "correct.json"
    merge_cp = meeting_dir / "merge.json"
    try:
        corrected = CorrectedResult.from_checkpoint(corrected_output)
    except FileNotFoundError:
        try:
            corrected = CorrectedResult.from_checkpoint(correct_cp)
        except FileNotFoundError:
            try:
                merged = MergedResult.from_checkpoint(merge_cp)
            except FileNotFoundError:
                raise FileNotFoundError(
                    f"corrected.json / correct.json / merge.json 산출물이 없습니다: {meeting_id}"
                ) from None
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

    match = re.search(r"(\d{4})(\d{2})(\d{2})_\d{6}", meeting_id)
    date_str = (
        f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        if match
        else datetime.now().strftime("%Y-%m-%d")
    )
    chunked = await Chunker(config).chunk(corrected, meeting_id, date_str)
    atomic_write_json_pinned(
        meeting_dir / "chunk.json",
        chunked.to_dict(),
        backup=False,
    )
    embedded = await Embedder(config, model_manager).embed(chunked)
    atomic_write_json_pinned(
        meeting_dir / "embed.json",
        embedded.to_dict(),
        backup=False,
    )
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
