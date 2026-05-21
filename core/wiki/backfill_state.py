"""Durable Wiki backfill job state.

백필 작업의 진행 상태를 SQLite 에 저장해 서버 재시작 후에도 사용자가 실패/취소/
완료 상태를 확인하고 실패 건만 재시도할 수 있게 한다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DurableBackfillJob:
    """SQLite 에 저장된 Wiki backfill job snapshot."""

    job_id: str
    status: str
    request: dict[str, Any] = field(default_factory=dict)
    processed: int = 0
    total: int = 0
    current_meeting_id: str | None = None
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None


class WikiBackfillStateStore:
    """Wiki backfill job 상태를 SQLite 에 저장하는 작은 저장소."""

    def __init__(self, wiki_root: Path) -> None:
        """wiki_root 하위 .index/wiki_backfill_jobs.db 를 사용한다."""
        self._db_path = wiki_root / ".index" / "wiki_backfill_jobs.db"

    @property
    def db_path(self) -> Path:
        """SQLite DB 경로."""
        return self._db_path

    def create_job(
        self,
        *,
        job_id: str,
        started_at: str,
        request: dict[str, Any],
    ) -> None:
        """새 백필 job 을 running 상태로 저장한다."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                INSERT OR REPLACE INTO wiki_backfill_jobs
                    (job_id, status, request_json, processed, total,
                     current_meeting_id, succeeded, skipped, failed, errors_json,
                     started_at, finished_at, duration_seconds)
                VALUES (?, 'running', ?, 0, 0, NULL, 0, 0, 0, '[]', ?, NULL, NULL)
                """,
                (job_id, json.dumps(request, ensure_ascii=False), started_at),
            )
            conn.commit()

    def update_progress(
        self,
        *,
        job_id: str,
        processed: int,
        total: int,
        current_meeting_id: str,
    ) -> None:
        """진행률 스냅샷을 갱신한다."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                UPDATE wiki_backfill_jobs
                SET processed = ?, total = ?, current_meeting_id = ?
                WHERE job_id = ?
                """,
                (processed, total, current_meeting_id, job_id),
            )
            conn.commit()

    def complete_job(
        self,
        *,
        job_id: str,
        status: str,
        finished_at: str,
        result: Any,
    ) -> None:
        """BackfillResult 호환 객체를 terminal 상태로 저장한다."""
        errors = []
        for err in getattr(result, "errors", []) or []:
            errors.append(
                {
                    "meeting_id": str(getattr(err, "meeting_id", "")),
                    "error_type": str(getattr(err, "error_type", "unknown")),
                    "message": str(getattr(err, "message", "")),
                }
            )
        with self._connect() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                UPDATE wiki_backfill_jobs
                SET status = ?, processed = ?, total = ?, current_meeting_id = NULL,
                    succeeded = ?, skipped = ?, failed = ?, errors_json = ?,
                    finished_at = ?, duration_seconds = ?
                WHERE job_id = ?
                """,
                (
                    status,
                    int(getattr(result, "succeeded", 0))
                    + int(getattr(result, "skipped", 0))
                    + int(getattr(result, "failed", 0)),
                    int(getattr(result, "total", 0)),
                    int(getattr(result, "succeeded", 0)),
                    int(getattr(result, "skipped", 0)),
                    int(getattr(result, "failed", 0)),
                    json.dumps(errors, ensure_ascii=False),
                    finished_at,
                    getattr(result, "duration_seconds", None),
                    job_id,
                ),
            )
            conn.commit()

    def fail_job(self, *, job_id: str, finished_at: str, message: str) -> None:
        """백그라운드 예외 등 job-level 실패를 저장한다."""
        error = [{"meeting_id": "", "error_type": "job_failed", "message": message}]
        with self._connect() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                UPDATE wiki_backfill_jobs
                SET status = 'failed', finished_at = ?, failed = failed + 1,
                    errors_json = ?
                WHERE job_id = ?
                """,
                (finished_at, json.dumps(error, ensure_ascii=False), job_id),
            )
            conn.commit()

    def cancel_job(self, *, job_id: str, finished_at: str | None = None) -> None:
        """취소 요청을 durable 상태에 반영한다."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            conn.execute(
                """
                UPDATE wiki_backfill_jobs
                SET status = 'cancelled', finished_at = COALESCE(?, finished_at)
                WHERE job_id = ?
                """,
                (finished_at, job_id),
            )
            conn.commit()

    def get_job(self, job_id: str) -> DurableBackfillJob | None:
        """job_id 로 상태를 조회한다."""
        with self._connect() as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM wiki_backfill_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def failed_meeting_ids(self, job_id: str) -> list[str]:
        """해당 job 의 실패 meeting id 목록을 반환한다."""
        job = self.get_job(job_id)
        if job is None:
            return []
        ids: list[str] = []
        for err in job.errors:
            meeting_id = str(err.get("meeting_id") or "").strip()
            if meeting_id and meeting_id not in ids:
                ids.append(meeting_id)
        return ids

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wiki_backfill_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL,
                processed INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                current_meeting_id TEXT,
                succeeded INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                errors_json TEXT NOT NULL DEFAULT '[]',
                started_at TEXT,
                finished_at TEXT,
                duration_seconds REAL
            )
            """
        )
        conn.commit()

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> DurableBackfillJob:
        request = json.loads(row["request_json"] or "{}")
        errors = json.loads(row["errors_json"] or "[]")
        status = str(row["status"])
        if status == "running" and row["finished_at"] is None:
            # 서버 재시작 후 메모리 task 가 없는 running job 은 복구 가능한 중단 상태로 노출.
            status = "interrupted"
        return DurableBackfillJob(
            job_id=str(row["job_id"]),
            status=status,
            request=request if isinstance(request, dict) else {},
            processed=int(row["processed"] or 0),
            total=int(row["total"] or 0),
            current_meeting_id=row["current_meeting_id"],
            succeeded=int(row["succeeded"] or 0),
            skipped=int(row["skipped"] or 0),
            failed=int(row["failed"] or 0),
            errors=errors if isinstance(errors, list) else [],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            duration_seconds=row["duration_seconds"],
        )
