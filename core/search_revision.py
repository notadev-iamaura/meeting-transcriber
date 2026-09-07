"""전사 편집 버전과 검색 반영 영수증을 비교하는 읽기 전용 계약."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from core.io_utils import read_text_no_follow


def checkpoint_dir(config: Any, meeting_id: str) -> Path:
    """회의 체크포인트의 lexical 경로를 반환한다."""
    from core.reindex_recovery import _configured_storage_root, _validate_meeting_id

    _validate_meeting_id(meeting_id)
    return (
        _configured_storage_root(config, "checkpoints_dir", config.paths.resolved_checkpoints_dir)
        / meeting_id
    )


def read_source(config: Any, meeting_id: str) -> tuple[Path, dict[str, Any]]:
    """뷰어와 동일한 우선순위로 안전한 전사 원문을 읽는다."""
    from core.reindex_recovery import _configured_storage_root

    cp = checkpoint_dir(config, meeting_id)
    output = _configured_storage_root(config, "outputs_dir", config.paths.resolved_outputs_dir)
    for path in (output / meeting_id / "corrected.json", cp / "correct.json", cp / "merge.json"):
        try:
            data = json.loads(read_text_no_follow(path))
        except FileNotFoundError:
            continue
        if not isinstance(data, dict):
            raise ValueError("전사 원문 형식이 올바르지 않습니다")
        return path, data
    raise FileNotFoundError(meeting_id)


def revision_status(config: Any, meeting_id: str) -> str:
    """편집본의 검색 반영 상태를 반환한다. 기존 데이터는 legacy로 구분한다."""
    try:
        _, source = read_source(config, meeting_id)
        revision = source.get("search_revision")
        if revision is None:
            return "legacy"
        if not isinstance(revision, str) or not revision:
            return "unavailable"
        try:
            receipt = json.loads(
                read_text_no_follow(checkpoint_dir(config, meeting_id) / "search_receipt.json")
            )
        except FileNotFoundError:
            return "pending"
        if not isinstance(receipt, dict):
            return "unavailable"
        if receipt.get("revision") != revision:
            return "pending"
        state = receipt.get("state", "current")
        return state if state in {"running", "failed", "current"} else "unavailable"
    except FileNotFoundError:
        return "legacy"
    except (OSError, ValueError):
        return "unavailable"


def source_was_edited(config: Any, meeting_id: str) -> bool:
    """편집 이전 Wiki 근거 사용을 보수적으로 차단할지 판단한다."""
    try:
        _, source = read_source(config, meeting_id)
        return "search_revision" in source
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True


def filter_current_results(config: Any, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """편집 중/실패한 인덱스와 질의 도중 교체된 옛 청크를 제외한다."""
    states: dict[str, tuple[str, dict[str, Any]]] = {}
    accepted: list[dict[str, Any]] = []
    for result in results:
        mid = result["meeting_id"]
        if mid not in states:
            status = revision_status(config, mid)
            chunks: dict[str, Any] = {}
            if status == "current":
                try:
                    data = json.loads(
                        read_text_no_follow(checkpoint_dir(config, mid) / "embed.json")
                    )
                    chunks = {c["chunk_id"]: c for c in data["chunks"]}
                except (OSError, ValueError, KeyError, TypeError):
                    status = "unavailable"
            states[mid] = status, chunks
        status, chunks = states[mid]
        if status == "legacy":
            accepted.append(result)
        elif status == "current":
            chunk = chunks.get(result["chunk_id"], {})
            if (
                chunk.get("text") == result["text"]
                and chunk.get("date") == result["date"]
                and ",".join(chunk.get("speakers", [])) == result.get("speakers", "")
            ):
                accepted.append(result)
    return accepted


def meeting_date(config: Any, meeting_id: str, source: dict[str, Any]) -> str:
    """원문에 보존된 날짜 또는 검증한 입력 시각을 사용하고 불명은 빈 값으로 둔다."""
    try:
        saved = json.loads(
            read_text_no_follow(checkpoint_dir(config, meeting_id) / "meeting_date.json")
        )
    except FileNotFoundError:
        saved = None
    if saved is not None:
        if not isinstance(saved, dict) or not isinstance(saved.get("date"), str):
            raise ValueError("보존된 회의 날짜 기록이 잘못되었습니다")
        value = saved["date"]
        if value:
            datetime.strptime(value, "%Y-%m-%d")
        return str(value)
    if "meeting_date" in source:
        value = source["meeting_date"]
        if value == "":
            return ""
        if not isinstance(value, str):
            raise ValueError("보존된 회의 날짜 형식이 잘못되었습니다")
        datetime.strptime(value, "%Y-%m-%d")
        return value
    match = re.search(r"(\d{4})(\d{2})(\d{2})_\d{6}", meeting_id)
    if match:
        value = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        datetime.strptime(value, "%Y-%m-%d")
        return value
    raw = source.get("audio_path")
    if isinstance(raw, str) and raw:
        from core.quarantine import _open_directory_tree_no_follow

        path = Path(raw).expanduser().absolute()
        base = Path(config.paths.base_dir).expanduser().absolute()
        if path.is_relative_to(base) and ".." not in path.parts:
            try:
                parent = _open_directory_tree_no_follow(path.parent, create=False)
                try:
                    entry = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISREG(entry.st_mode):
                        return datetime.fromtimestamp(entry.st_mtime).strftime("%Y-%m-%d")
                finally:
                    os.close(parent)
            except FileNotFoundError:
                pass
    # 원본을 확인할 수 없다면 등록일/재색인일을 회의일로 추측하지 않는다.
    return ""


def preserve_meeting_date(config: Any, meeting_id: str, source: dict[str, Any]) -> str:
    """회의 lease 안에서 처음 결정한 날짜를 이후 전사·재색인에 고정한다."""
    from core.io_utils import publish_text_no_replace

    value = meeting_date(config, meeting_id, source)
    target = checkpoint_dir(config, meeting_id) / "meeting_date.json"
    try:
        publish_text_no_replace(
            target,
            json.dumps(
                {
                    "date": value,
                    "source": "preserved"
                    if "meeting_date" in source
                    else "filename"
                    if re.search(r"\d{8}_\d{6}", meeting_id)
                    else "audio_mtime"
                    if value
                    else "unknown",
                }
            ),
        )
    except FileExistsError:
        return meeting_date(config, meeting_id, source)
    return value


def index_health(config: Any, collection: Any, meeting_id: str) -> str:
    """양쪽 인덱스의 청크 집합과 편집 버전을 대조한다."""
    status = revision_status(config, meeting_id)
    if status not in {"legacy", "current"}:
        return status
    if collection is None:
        return "missing"
    try:
        vector_ids = set(collection.get(where={"meeting_id": meeting_id})["ids"])
        if not vector_ids:
            return "missing"
        db_path = config.paths.resolved_meetings_db
        if not db_path.is_file() or db_path.is_symlink():
            return "incomplete"
        with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT chunk_id FROM chunks_fts WHERE meeting_id = ?", (meeting_id,)
            ).fetchall()
        fts_ids = {row[0] for row in rows}
        if vector_ids != fts_ids:
            return "incomplete"
        expected = json.loads(
            read_text_no_follow(checkpoint_dir(config, meeting_id) / "embed.json")
        )
        expected_ids = {c["chunk_id"] for c in expected["chunks"]}
        return "current" if vector_ids == expected_ids else "incomplete"
    except FileNotFoundError:
        return "unverified"
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return "unavailable"
