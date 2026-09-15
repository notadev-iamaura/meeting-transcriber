"""유한 회의 묶음의 단계 의도와 실행 소유권을 보존하는 SQLite 큐.

모델 실행과 산출물 게시는 호출자가 담당한다. 이 모듈은 원본 경로나 자격 증명을
필요로 하지 않으며, model_snapshot에는 모델 식별자와 비밀 없는 실행 옵션만 넣는다.
시간 경과로 실행 소유권을 빼앗지 않는다. 이전 세션 복구 전에는 호출자가 실제
native worker 종료를 확인해야 한다.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4


class StageWorkError(RuntimeError):
    """단계 큐의 저장 또는 입력 계약 위반."""


class StageWorkConflict(StageWorkError):
    """접수 내용 또는 실행 소유권이 현재 상태와 충돌함."""


class StageWorkStatus(StrEnum):
    """영속 단계 상태. 취소 의도는 별도 재접수 없이 실행하지 않는다."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"


@dataclass(frozen=True)
class StageWorkSpec:
    """한 회의 generation의 단계와 같은 generation 내 선행 단계 이름."""

    meeting_id: str
    generation: str
    stage: str
    dependencies: tuple[str, ...] = ()
    input_fingerprint: str = ""
    model_snapshot: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageWork:
    """호출자가 산출물 검증과 실행을 이어갈 수 있는 영속 작업 스냅샷."""

    id: int
    cohort_id: str
    meeting_id: str
    generation: str
    stage: str
    dependencies: tuple[str, ...]
    input_fingerprint: str
    model_snapshot: dict[str, Any]
    status: StageWorkStatus
    attempt_count: int
    owner_session_id: str
    claim_token: str
    error_code: str
    created_at: str
    updated_at: str


def _identifier(value: str) -> str:
    """식별자·fingerprint의 빈 값과 NUL을 차단한다."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise StageWorkError("식별자와 fingerprint는 비어 있지 않은 문자열이어야 합니다")
    return value


def _json(value: Any) -> str:
    """동일한 JSON 입력을 비교할 수 있도록 안정적으로 직렬화한다."""
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise StageWorkError("단계 snapshot은 유효한 JSON 값이어야 합니다") from exc


def _spec_payload(spec: StageWorkSpec) -> dict[str, Any]:
    """입력을 검증하고 외부 mutable 객체와 분리된 접수 내용을 만든다."""
    meeting_id = _identifier(spec.meeting_id)
    generation = _identifier(spec.generation)
    stage = _identifier(spec.stage)
    dependencies = sorted(_identifier(dependency) for dependency in spec.dependencies)
    if len(dependencies) != len(set(dependencies)) or stage in dependencies:
        raise StageWorkError("선행 단계는 중복되거나 자기 자신일 수 없습니다")
    if not isinstance(spec.model_snapshot, Mapping):
        raise StageWorkError("모델 snapshot은 JSON 객체여야 합니다")
    snapshot = json.loads(_json(dict(spec.model_snapshot)))
    return {
        "meeting_id": meeting_id,
        "generation": generation,
        "stage": stage,
        "dependencies": dependencies,
        "input_fingerprint": _identifier(spec.input_fingerprint),
        "model_snapshot": snapshot,
    }


def _record(row: sqlite3.Row) -> StageWork:
    """SQLite row를 연결과 분리된 불변 기록으로 반환한다."""
    return StageWork(
        id=row["id"],
        cohort_id=row["cohort_id"],
        meeting_id=row["meeting_id"],
        generation=row["generation"],
        stage=row["stage"],
        dependencies=tuple(json.loads(row["dependencies_json"])),
        input_fingerprint=row["input_fingerprint"],
        model_snapshot=json.loads(row["model_snapshot_json"]),
        status=StageWorkStatus(row["status"]),
        attempt_count=row["attempt_count"],
        owner_session_id=row["owner_session_id"],
        claim_token=row["claim_token"],
        error_code=row["error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class StageWorkQueue:
    """주어진 DB의 독립 테이블에서 단계 실행 의도를 저장한다.

    생성자는 경로만 보관하며 initialize()가 스키마를 만든다. DB 부모 디렉토리는
    이미 존재해야 한다. 호출별 connection으로 스레드 간 연결 공유를 피한다.
    """

    def __init__(self, db_path: Path) -> None:
        """사용할 SQLite 경로를 보관한다."""
        self._db_path = Path(db_path)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """짧은 transaction을 열고 성공 시 commit, 실패 시 rollback한다."""
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except sqlite3.Error as exc:
            if conn is not None:
                conn.rollback()
            raise StageWorkError("단계 큐 SQLite 작업에 실패했습니다") from exc
        finally:
            if conn is not None:
                conn.close()

    def initialize(self) -> None:
        """기존 jobs 테이블을 변경하지 않고 단계 큐 스키마를 생성한다."""
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        with self._connection(write=True) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS stage_work_cohorts (
                    cohort_id TEXT PRIMARY KEY,
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS stage_work_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cohort_id TEXT NOT NULL REFERENCES stage_work_cohorts(cohort_id),
                    meeting_id TEXT NOT NULL,
                    generation TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    dependencies_json TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL,
                    model_snapshot_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN ('queued', 'running', 'completed', 'failed',
                                         'cancel_requested')),
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                    owner_session_id TEXT NOT NULL DEFAULT '',
                    claim_token TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(meeting_id, generation, stage)
                )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_stage_work_ready
                   ON stage_work_items(cohort_id, status, stage, id)"""
            )

    def enqueue_cohort(self, cohort_id: str, specs: Sequence[StageWorkSpec]) -> list[StageWork]:
        """유한 목록 전체를 원자 접수한다. 같은 ID는 같은 목록으로만 재접수한다.

        입력 순서도 묶음의 일부다. 다른 묶음이 같은 회의/generation/단계를 소유하면
        충돌하며, 선행 단계는 이 접수 목록 또는 이미 저장된 작업에 존재해야 한다.
        """
        _identifier(cohort_id)
        payloads = [_spec_payload(spec) for spec in specs]
        keys = [(item["meeting_id"], item["generation"], item["stage"]) for item in payloads]
        if not payloads or len(keys) != len(set(keys)):
            raise StageWorkError("접수 목록은 비어 있지 않아야 하며 단계가 중복될 수 없습니다")
        manifest = _json(payloads)
        now = datetime.now(UTC).isoformat()
        with self._connection(write=True) as conn:
            existing = conn.execute(
                "SELECT manifest_json FROM stage_work_cohorts WHERE cohort_id=?", (cohort_id,)
            ).fetchone()
            if existing is not None:
                if existing["manifest_json"] != manifest:
                    raise StageWorkConflict("이미 접수한 묶음의 내용을 변경할 수 없습니다")
                return self._list_cohort(conn, cohort_id)
            self._validate_dependencies(conn, payloads)
            conn.execute(
                "INSERT INTO stage_work_cohorts VALUES (?, ?, ?)", (cohort_id, manifest, now)
            )
            for item in payloads:
                duplicate = conn.execute(
                    """SELECT id FROM stage_work_items
                       WHERE meeting_id=? AND generation=? AND stage=?""",
                    (item["meeting_id"], item["generation"], item["stage"]),
                ).fetchone()
                if duplicate is not None:
                    raise StageWorkConflict("다른 묶음에 이미 접수된 단계입니다")
                conn.execute(
                    """INSERT INTO stage_work_items
                       (cohort_id, meeting_id, generation, stage, dependencies_json,
                        input_fingerprint, model_snapshot_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        cohort_id,
                        item["meeting_id"],
                        item["generation"],
                        item["stage"],
                        _json(item["dependencies"]),
                        item["input_fingerprint"],
                        _json(item["model_snapshot"]),
                        now,
                        now,
                    ),
                )
            return self._list_cohort(conn, cohort_id)

    @staticmethod
    def _validate_dependencies(conn: sqlite3.Connection, payloads: list[dict[str, Any]]) -> None:
        """존재하지 않는 선행 단계와 같은 접수 안의 순환 의존을 거부한다."""
        dependencies = {
            (item["meeting_id"], item["generation"], item["stage"]): {
                (item["meeting_id"], item["generation"], dependency)
                for dependency in item["dependencies"]
            }
            for item in payloads
        }
        for prerequisites in dependencies.values():
            for prerequisite in prerequisites:
                if prerequisite in dependencies:
                    continue
                if (
                    conn.execute(
                        """SELECT id FROM stage_work_items
                       WHERE meeting_id=? AND generation=? AND stage=?""",
                        prerequisite,
                    ).fetchone()
                    is None
                ):
                    raise StageWorkError("선행 단계가 같은 회의 generation에 존재하지 않습니다")
        remaining = {
            key: prerequisites.intersection(dependencies)
            for key, prerequisites in dependencies.items()
        }
        while remaining:
            ready = {key for key, prerequisites in remaining.items() if not prerequisites}
            if not ready:
                raise StageWorkError("단계 의존성에 순환이 있습니다")
            remaining = {
                key: prerequisites - ready
                for key, prerequisites in remaining.items()
                if key not in ready
            }

    @staticmethod
    def _list_cohort(conn: sqlite3.Connection, cohort_id: str) -> list[StageWork]:
        """열린 transaction 안에서 접수 순서대로 작업을 읽는다."""
        return [
            _record(row)
            for row in conn.execute(
                "SELECT * FROM stage_work_items WHERE cohort_id=? ORDER BY id", (cohort_id,)
            ).fetchall()
        ]

    def list_cohort(self, cohort_id: str) -> list[StageWork]:
        """묶음의 현재 상태를 접수 순서로 반환한다."""
        with self._connection() as conn:
            return self._list_cohort(conn, _identifier(cohort_id))

    def get(self, work_id: int) -> StageWork:
        """ID에 대응하는 단계 작업을 반환한다."""
        with self._connection() as conn:
            return self._get(conn, work_id)

    @staticmethod
    def _get(conn: sqlite3.Connection, work_id: int) -> StageWork:
        """열린 transaction에서 작업을 찾고 누락을 명확히 반환한다."""
        row = conn.execute("SELECT * FROM stage_work_items WHERE id=?", (work_id,)).fetchone()
        if row is None:
            raise StageWorkError("단계 작업을 찾을 수 없습니다")
        return _record(row)

    def claim_ready(
        self,
        cohort_id: str,
        session_id: str,
        stage: str,
        *,
        limit: int = 1,
        model_snapshot: Mapping[str, Any] | None = None,
    ) -> list[StageWork]:
        """해당 유한 묶음의 준비된 단계만 원자 선점한다. 실행 중 작업은 건드리지 않는다."""
        _identifier(cohort_id)
        _identifier(session_id)
        _identifier(stage)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise StageWorkError("선점 개수는 양의 정수여야 합니다")
        snapshot = None if model_snapshot is None else _json(dict(model_snapshot))
        with self._connection(write=True) as conn:
            rows = conn.execute(
                """SELECT work.* FROM stage_work_items AS work
                   WHERE cohort_id=? AND status='queued' AND stage=?
                     AND (? IS NULL OR model_snapshot_json=?)
                     AND NOT EXISTS (
                       SELECT 1 FROM json_each(work.dependencies_json) AS dependency
                       WHERE NOT EXISTS (
                         SELECT 1 FROM stage_work_items AS prerequisite
                         WHERE prerequisite.meeting_id=work.meeting_id
                           AND prerequisite.generation=work.generation
                           AND prerequisite.stage=dependency.value
                           AND prerequisite.status='completed'
                       )
                     ) ORDER BY work.id LIMIT ?""",
                (cohort_id, stage, snapshot, snapshot, limit),
            ).fetchall()
            result = []
            for row in rows:
                conn.execute(
                    """UPDATE stage_work_items SET status='running', attempt_count=attempt_count+1,
                       owner_session_id=?, claim_token=?, error_code='', updated_at=?
                       WHERE id=? AND status='queued'""",
                    (session_id, str(uuid4()), datetime.now(UTC).isoformat(), row["id"]),
                )
                result.append(self._get(conn, row["id"]))
            return result

    def complete(self, work_id: int, session_id: str, claim_token: str) -> StageWork:
        """산출물 게시를 검증한 현재 실행 소유자만 완료를 기록한다."""
        return self._finish(work_id, session_id, claim_token, StageWorkStatus.COMPLETED, "")

    def fail(self, work_id: int, session_id: str, claim_token: str, error_code: str) -> StageWork:
        """원문 오류·경로 대신 짧은 분류 코드로 실패를 기록한다."""
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", error_code) is None:
            raise StageWorkError("실패 코드는 영문 대문자·숫자·밑줄만 사용할 수 있습니다")
        return self._finish(work_id, session_id, claim_token, StageWorkStatus.FAILED, error_code)

    def _finish(
        self,
        work_id: int,
        session_id: str,
        claim_token: str,
        status: StageWorkStatus,
        error_code: str,
    ) -> StageWork:
        """소유권과 상태를 같은 transaction에서 확인하며 중복 완료는 멱등 처리한다."""
        _identifier(session_id)
        _identifier(claim_token)
        with self._connection(write=True) as conn:
            work = self._get(conn, work_id)
            if work.owner_session_id != session_id or work.claim_token != claim_token:
                raise StageWorkConflict("단계 실행 소유권이 변경되었습니다")
            if work.status == status and work.error_code == error_code:
                return work
            if work.status != StageWorkStatus.RUNNING:
                raise StageWorkConflict("실행 중인 단계만 완료 또는 실패로 기록할 수 있습니다")
            conn.execute(
                "UPDATE stage_work_items SET status=?, error_code=?, updated_at=? WHERE id=?",
                (status.value, error_code, datetime.now(UTC).isoformat(), work_id),
            )
            return self._get(conn, work_id)

    def request_cancel(self, meeting_id: str, generation: str) -> list[StageWork]:
        """해당 generation의 대기·실행 작업에 취소 의도를 영속 기록한다."""
        _identifier(meeting_id)
        _identifier(generation)
        with self._connection(write=True) as conn:
            conn.execute(
                """UPDATE stage_work_items SET status='cancel_requested', updated_at=?
                   WHERE meeting_id=? AND generation=? AND status IN ('queued', 'running')""",
                (datetime.now(UTC).isoformat(), meeting_id, generation),
            )
            return [
                _record(row)
                for row in conn.execute(
                    """SELECT * FROM stage_work_items WHERE meeting_id=? AND generation=?
                   AND status='cancel_requested' ORDER BY id""",
                    (meeting_id, generation),
                ).fetchall()
            ]

    def acknowledge_cancel(self, work_id: int, session_id: str, claim_token: str) -> StageWork:
        """현재 소유자가 실제 실행 종료 후 취소 작업의 소유권만 해제한다."""
        _identifier(session_id)
        _identifier(claim_token)
        with self._connection(write=True) as conn:
            work = self._get(conn, work_id)
            if (
                work.status != StageWorkStatus.CANCEL_REQUESTED
                or work.owner_session_id != session_id
                or work.claim_token != claim_token
            ):
                raise StageWorkConflict("취소 작업의 실행 소유권이 일치하지 않습니다")
            conn.execute(
                """UPDATE stage_work_items SET owner_session_id='', claim_token='', updated_at=?
                   WHERE id=?""",
                (datetime.now(UTC).isoformat(), work_id),
            )
            return self._get(conn, work_id)

    def recover_session(
        self,
        old_session_id: str,
        *,
        current_session_id: str,
        native_ownership_released: bool,
    ) -> list[StageWork]:
        """실제로 종료된 이전 세션만 복구한다. 취소 의도와 시도 횟수는 보존한다.

        native_ownership_released는 외부 실행기의 확인이며 시간 기반 lease가 아니다.
        이 메서드를 호출하는 현재 프로세스의 작업은 복구 대상으로 삼지 않는다.
        """
        _identifier(old_session_id)
        _identifier(current_session_id)
        if old_session_id == current_session_id or native_ownership_released is not True:
            raise StageWorkConflict("실제 실행 종료를 확인한 이전 세션만 복구할 수 있습니다")
        with self._connection(write=True) as conn:
            rows = conn.execute(
                """SELECT id FROM stage_work_items WHERE owner_session_id=?
                   AND status IN ('running', 'cancel_requested') ORDER BY id""",
                (old_session_id,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """UPDATE stage_work_items SET
                       status=CASE WHEN status='running' THEN 'queued' ELSE status END,
                       owner_session_id='', claim_token='', updated_at=? WHERE id=?""",
                    (datetime.now(UTC).isoformat(), row["id"]),
                )
            return [self._get(conn, row["id"]) for row in rows]
