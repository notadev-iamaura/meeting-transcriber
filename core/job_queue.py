"""
SQLite 기반 작업 큐 모듈 (Job Queue Module)

목적: 회의 전사 파이프라인 작업을 SQLite 테이블로 관리한다.
주요 기능:
    - 작업 등록 (add_job)
    - 상태 머신 기반 상태 전이 (update_status)
    - 재시도 로직 (retry_count, max_retries)
    - WAL 모드로 읽기/쓰기 동시성 확보
    - asyncio.to_thread로 이벤트 루프 블로킹 방지
의존성: sqlite3 (stdlib), config 모듈
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


# === 작업 상태 정의 ===


class JobStatus(str, Enum):
    """작업 큐의 상태를 정의하는 열거형.

    상태 전이 규칙:
        recorded → queued (수동 전사 요청 시)
        queued → recording → transcribing → diarizing → merging → embedding → completed
        어떤 상태에서든 → failed 전이 가능
        failed → queued (재시도 시)
    """

    RECORDED = "recorded"
    QUEUED = "queued"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    DIARIZING = "diarizing"
    MERGING = "merging"
    EMBEDDING = "embedding"
    COMPLETED = "completed"
    FAILED = "failed"


# 유효한 상태 전이 맵 (현재 상태 → 전이 가능한 상태 집합)
VALID_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.RECORDED: {JobStatus.QUEUED, JobStatus.FAILED},  # 수동 전사 요청 시
    JobStatus.QUEUED: {JobStatus.RECORDING, JobStatus.TRANSCRIBING, JobStatus.FAILED},
    JobStatus.RECORDING: {JobStatus.TRANSCRIBING, JobStatus.FAILED},
    JobStatus.TRANSCRIBING: {JobStatus.DIARIZING, JobStatus.FAILED},
    JobStatus.DIARIZING: {JobStatus.MERGING, JobStatus.FAILED},
    JobStatus.MERGING: {JobStatus.EMBEDDING, JobStatus.COMPLETED, JobStatus.FAILED},  # skip_llm_steps 시 merging→completed 직행
    JobStatus.EMBEDDING: {JobStatus.COMPLETED, JobStatus.FAILED},
    JobStatus.COMPLETED: set(),  # 완료 후 전이 불가
    JobStatus.FAILED: {JobStatus.QUEUED},  # 재시도만 가능
}


# === 데이터 클래스 ===


@dataclass
class Job:
    """작업 큐의 단일 작업을 나타내는 데이터 클래스.

    Attributes:
        id: 작업 고유 식별자 (자동 증가)
        meeting_id: 회의 고유 식별자
        audio_path: 오디오 파일 절대 경로
        status: 현재 작업 상태
        retry_count: 현재 재시도 횟수
        max_retries: 최대 재시도 횟수
        error_message: 마지막 에러 메시지
        created_at: 작업 생성 시각 (ISO 형식)
        updated_at: 마지막 업데이트 시각 (ISO 형식)
    """

    id: int
    meeting_id: str
    audio_path: str
    status: str = JobStatus.QUEUED.value
    retry_count: int = 0
    max_retries: int = 3
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""
    # 사용자 정의 제목 (빈 문자열이면 프론트엔드가 meeting_id 기반 타임스탬프 폴백 사용)
    title: str = ""


# === 에러 계층 ===


class JobQueueError(Exception):
    """작업 큐 관련 에러의 기본 클래스."""


class InvalidTransitionError(JobQueueError):
    """유효하지 않은 상태 전이 시도 시 발생한다.

    Attributes:
        job_id: 대상 작업 ID
        current_status: 현재 상태
        target_status: 시도한 상태
    """

    def __init__(
        self,
        job_id: int,
        current_status: str,
        target_status: str,
    ) -> None:
        self.job_id = job_id
        self.current_status = current_status
        self.target_status = target_status
        super().__init__(f"작업 {job_id}: 상태 전이 불가 ({current_status} → {target_status})")


class JobNotFoundError(JobQueueError):
    """존재하지 않는 작업을 조회할 때 발생한다.

    Attributes:
        job_id: 조회 시도한 작업 ID
    """

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        super().__init__(f"작업을 찾을 수 없습니다: {job_id}")


class MaxRetriesExceededError(JobQueueError):
    """최대 재시도 횟수를 초과했을 때 발생한다.

    Attributes:
        job_id: 대상 작업 ID
        retry_count: 현재 재시도 횟수
        max_retries: 최대 재시도 횟수
    """

    def __init__(
        self,
        job_id: int,
        retry_count: int,
        max_retries: int,
    ) -> None:
        self.job_id = job_id
        self.retry_count = retry_count
        self.max_retries = max_retries
        super().__init__(f"작업 {job_id}: 최대 재시도 횟수 초과 ({retry_count}/{max_retries})")


# === 메인 클래스 ===


class JobQueue:
    """SQLite 기반 작업 큐 매니저.

    회의 전사 파이프라인의 작업을 SQLite 테이블로 관리한다.
    WAL 모드를 사용하여 읽기/쓰기 동시성을 확보하고,
    상태 머신으로 유효한 상태 전이만 허용한다.

    Args:
        db_path: SQLite 데이터베이스 파일 경로
        max_retries: 최대 재시도 횟수 (기본값: 3)

    사용 예시:
        queue = JobQueue(Path("jobs.db"))
        queue.initialize()
        job_id = queue.add_job("meeting_001", "/path/to/audio.m4a")
        queue.update_status(job_id, JobStatus.RECORDING)
    """

    # 테이블 생성 SQL
    # 주의: title 은 마이그레이션을 통해 추가되므로 여기의 CREATE 문에도 포함.
    # 기존 DB 는 initialize() 의 _ensure_schema_migrations() 에서 ALTER TABLE 로 추가된다.
    _CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id TEXT NOT NULL UNIQUE,
        audio_path TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        retry_count INTEGER NOT NULL DEFAULT 0,
        max_retries INTEGER NOT NULL DEFAULT 3,
        error_message TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT ''
    )
    """

    _CREATE_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status)
    """

    def __init__(
        self,
        db_path: Path,
        max_retries: int = 3,
    ) -> None:
        """JobQueue를 초기화한다.

        Args:
            db_path: SQLite DB 파일 경로
            max_retries: 최대 재시도 횟수
        """
        self._db_path = db_path
        self._max_retries = max_retries
        self._conn: sqlite3.Connection | None = None
        # 쓰기 직렬화 락 — 동시 쓰기로 인한 "database is locked" 방지
        self._write_lock = threading.Lock()

        logger.info(f"JobQueue 초기화: db_path={db_path}, max_retries={max_retries}")

    @property
    def db_path(self) -> Path:
        """데이터베이스 파일 경로를 반환한다."""
        return self._db_path

    def initialize(self) -> None:
        """데이터베이스를 초기화한다.

        DB 파일과 테이블을 생성하고 WAL 모드를 설정한다.
        이미 존재하는 DB에 대해서는 멱등하게 동작한다.

        Raises:
            JobQueueError: DB 초기화 실패 시
        """
        try:
            # 부모 디렉토리 생성
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row

            # WAL 모드 설정 (읽기/쓰기 동시성 향상)
            self._conn.execute("PRAGMA journal_mode=WAL")

            # 동시 쓰기 충돌 시 5초간 재시도 (STAB-011)
            self._conn.execute("PRAGMA busy_timeout=5000")

            # 외래 키 제약 활성화
            self._conn.execute("PRAGMA foreign_keys=ON")

            # 테이블 + 인덱스 생성 (쓰기 직렬화)
            with self._write_lock:
                self._conn.execute(self._CREATE_TABLE_SQL)
                self._conn.execute(self._CREATE_INDEX_SQL)
                self._ensure_schema_migrations()
                self._conn.commit()

            logger.info(f"JobQueue DB 초기화 완료: {self._db_path}")

        except sqlite3.Error as e:
            raise JobQueueError(f"DB 초기화 실패: {e}") from e

    def close(self) -> None:
        """데이터베이스 연결을 종료한다."""
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("JobQueue DB 연결 종료")

    def _ensure_connection(self) -> sqlite3.Connection:
        """DB 연결이 활성 상태인지 확인하고 반환한다.

        Returns:
            활성 sqlite3.Connection

        Raises:
            JobQueueError: 연결이 초기화되지 않았을 때
        """
        if self._conn is None:
            raise JobQueueError("DB 연결이 초기화되지 않았습니다. initialize()를 먼저 호출하세요.")
        return self._conn

    @staticmethod
    def _now_iso() -> str:
        """현재 시각을 ISO 형식 문자열로 반환한다."""
        return datetime.now().isoformat()

    def _ensure_schema_migrations(self) -> None:
        """기존 DB 스키마를 최신 형태로 마이그레이션한다.

        SQLite는 DROP COLUMN 지원이 제한적이므로, 새 컬럼 추가는 ALTER TABLE ADD COLUMN
        방식으로만 수행한다. PRAGMA table_info 로 현재 컬럼을 확인하고 누락분만 추가한다.

        마이그레이션 목록:
            - v1: title TEXT NOT NULL DEFAULT '' (사용자 정의 회의 제목)
        """
        conn = self._ensure_connection()
        cursor = conn.execute("PRAGMA table_info(jobs)")
        existing_columns = {row["name"] for row in cursor.fetchall()}

        if "title" not in existing_columns:
            logger.info("JobQueue 마이그레이션: jobs.title 컬럼 추가")
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN title TEXT NOT NULL DEFAULT ''"
            )

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        """sqlite3.Row를 Job 데이터 클래스로 변환한다.

        Args:
            row: SQLite 조회 결과 행

        Returns:
            Job 인스턴스
        """
        # 마이그레이션 전 DB 를 읽을 가능성에 대비해 title 은 방어적으로 조회
        try:
            title = row["title"]
        except (KeyError, IndexError):
            title = ""

        return Job(
            id=row["id"],
            meeting_id=row["meeting_id"],
            audio_path=row["audio_path"],
            status=row["status"],
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            title=title or "",
        )

    def add_job(
        self,
        meeting_id: str,
        audio_path: str,
        initial_status: str = JobStatus.QUEUED.value,
    ) -> int:
        """새 작업을 큐에 등록한다.

        Args:
            meeting_id: 회의 고유 식별자
            audio_path: 오디오 파일 경로
            initial_status: 초기 상태 (기본값: "queued", "recorded"로 설정 시 전사 대기)

        Returns:
            생성된 작업 ID

        Raises:
            JobQueueError: 중복 meeting_id 또는 DB 오류 시
        """
        conn = self._ensure_connection()
        now = self._now_iso()

        try:
            # 쓰기 직렬화 (STAB-017)
            with self._write_lock:
                cursor = conn.execute(
                    """
                    INSERT INTO jobs
                        (meeting_id, audio_path, status, retry_count,
                         max_retries, error_message, created_at, updated_at)
                    VALUES (?, ?, ?, 0, ?, '', ?, ?)
                    """,
                    (
                        meeting_id,
                        audio_path,
                        initial_status,
                        self._max_retries,
                        now,
                        now,
                    ),
                )
                conn.commit()
                job_id = cursor.lastrowid

            logger.info(f"작업 등록: id={job_id}, meeting_id={meeting_id}, status={initial_status}, audio={audio_path}")
            return job_id

        except sqlite3.IntegrityError as e:
            raise JobQueueError(f"작업 등록 실패 (중복 meeting_id?): {meeting_id} — {e}") from e

    def get_job(self, job_id: int) -> Job:
        """작업 ID로 작업을 조회한다.

        Args:
            job_id: 작업 ID

        Returns:
            Job 인스턴스

        Raises:
            JobNotFoundError: 작업이 존재하지 않을 때
        """
        conn = self._ensure_connection()
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

        if row is None:
            raise JobNotFoundError(job_id)

        return self._row_to_job(row)

    def get_job_by_meeting_id(self, meeting_id: str) -> Job | None:
        """meeting_id로 작업을 조회한다.

        Args:
            meeting_id: 회의 고유 식별자

        Returns:
            Job 인스턴스. 없으면 None.
        """
        conn = self._ensure_connection()
        row = conn.execute("SELECT * FROM jobs WHERE meeting_id = ?", (meeting_id,)).fetchone()

        if row is None:
            return None

        return self._row_to_job(row)

    def get_jobs_by_status(self, status: JobStatus) -> list[Job]:
        """특정 상태의 작업 목록을 조회한다.

        Args:
            status: 필터링할 작업 상태

        Returns:
            해당 상태의 Job 리스트 (created_at 오름차순)
        """
        conn = self._ensure_connection()
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC",
            (status.value,),
        ).fetchall()

        return [self._row_to_job(row) for row in rows]

    def get_pending_jobs(self) -> list[Job]:
        """대기 중(queued) 작업 목록을 조회한다.

        Returns:
            queued 상태의 Job 리스트 (created_at 오름차순)
        """
        return self.get_jobs_by_status(JobStatus.QUEUED)

    def get_all_jobs(self) -> list[Job]:
        """모든 작업을 조회한다.

        Returns:
            전체 Job 리스트 (created_at 내림차순, 최신순)
        """
        conn = self._ensure_connection()
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()

        return [self._row_to_job(row) for row in rows]

    def update_title(self, meeting_id: str, title: str) -> Job:
        """회의의 사용자 정의 제목을 업데이트한다.

        빈 문자열("")을 저장하면 기본값 표시(프론트엔드가 meeting_id 기반
        타임스탬프를 사용)로 돌아간다. 이 메서드는 상태 전이 규칙과 무관하다.

        Args:
            meeting_id: 회의 식별자
            title: 새 제목 (최대 200자, 앞뒤 공백 제거됨)

        Returns:
            업데이트된 Job 인스턴스

        Raises:
            JobNotFoundError: meeting_id 로 작업을 찾을 수 없을 때
            JobQueueError: title 이 너무 길거나 DB 오류 시
        """
        conn = self._ensure_connection()

        # 정제 + 검증
        cleaned = (title or "").strip()
        if len(cleaned) > 200:
            raise JobQueueError(
                f"제목이 너무 깁니다 ({len(cleaned)}자, 최대 200자)"
            )

        # 대상 작업 조회
        job = self.get_job_by_meeting_id(meeting_id)
        if job is None:
            raise JobNotFoundError(0)  # meeting_id 전용 에러 타입이 없으므로 0 사용

        now = self._now_iso()

        with self._write_lock:
            conn.execute(
                """
                UPDATE jobs
                SET title = ?, updated_at = ?
                WHERE meeting_id = ?
                """,
                (cleaned, now, meeting_id),
            )
            conn.commit()

        logger.info(
            "제목 업데이트: meeting_id=%s, title=%r", meeting_id, cleaned
        )
        return self.get_job_by_meeting_id(meeting_id)  # type: ignore[return-value]

    def update_status(
        self,
        job_id: int,
        new_status: JobStatus,
        error_message: str = "",
    ) -> Job:
        """작업 상태를 변경한다.

        상태 머신 규칙에 따라 유효한 전이만 허용한다.
        failed 상태로 전이 시 에러 메시지를 기록한다.

        Args:
            job_id: 대상 작업 ID
            new_status: 전이할 상태
            error_message: 에러 메시지 (failed 전이 시)

        Returns:
            업데이트된 Job 인스턴스

        Raises:
            JobNotFoundError: 작업이 없을 때
            InvalidTransitionError: 유효하지 않은 전이 시
        """
        conn = self._ensure_connection()

        # 현재 작업 조회
        job = self.get_job(job_id)
        current_status = JobStatus(job.status)

        # 상태 전이 검증
        valid_targets = VALID_TRANSITIONS.get(current_status, set())
        if new_status not in valid_targets:
            raise InvalidTransitionError(
                job_id,
                current_status.value,
                new_status.value,
            )

        now = self._now_iso()

        # 쓰기 직렬화 (STAB-017)
        with self._write_lock:
            # failed 전이 시 에러 메시지 기록
            if new_status == JobStatus.FAILED:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, error_message = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_status.value, error_message, now, job_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, error_message = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (new_status.value, now, job_id),
                )

            conn.commit()

        logger.info(f"작업 상태 변경: id={job_id}, {current_status.value} → {new_status.value}")

        return self.get_job(job_id)

    def retry_job(self, job_id: int) -> Job:
        """실패한 작업을 재시도한다.

        retry_count를 증가시키고 상태를 queued로 되돌린다.
        max_retries 초과 시 MaxRetriesExceededError를 발생시킨다.

        Args:
            job_id: 재시도할 작업 ID

        Returns:
            업데이트된 Job 인스턴스

        Raises:
            JobNotFoundError: 작업이 없을 때
            InvalidTransitionError: failed 상태가 아닐 때
            MaxRetriesExceededError: 최대 재시도 초과 시
        """
        conn = self._ensure_connection()

        job = self.get_job(job_id)

        # failed 상태만 재시도 가능
        if job.status != JobStatus.FAILED.value:
            raise InvalidTransitionError(
                job_id,
                job.status,
                JobStatus.QUEUED.value,
            )

        # 최대 재시도 초과 확인
        if job.retry_count >= job.max_retries:
            raise MaxRetriesExceededError(
                job_id,
                job.retry_count,
                job.max_retries,
            )

        now = self._now_iso()
        new_retry_count = job.retry_count + 1

        # 쓰기 직렬화 (STAB-017)
        with self._write_lock:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, retry_count = ?, error_message = '', updated_at = ?
                WHERE id = ?
                """,
                (JobStatus.QUEUED.value, new_retry_count, now, job_id),
            )
            conn.commit()

        logger.info(f"작업 재시도: id={job_id}, retry_count={new_retry_count}/{job.max_retries}")

        return self.get_job(job_id)

    def reset_for_retranscribe(self, job_id: int) -> Job:
        """완료/실패한 작업을 재전사 대상으로 초기화한다.

        표준 상태 전이 규칙(VALID_TRANSITIONS)을 우회하여
        completed/failed 상태의 작업을 강제로 queued 로 되돌린다.
        retry_count 와 error_message 도 리셋한다.

        주의: 이 메서드는 체크포인트/출력 파일을 삭제하지 않는다.
        호출자가 파일 정리 책임을 가진다 (api/routes.py::re_transcribe_meeting).

        Args:
            job_id: 재전사할 작업 ID

        Returns:
            업데이트된 Job 인스턴스

        Raises:
            JobNotFoundError: 작업이 없을 때
            InvalidTransitionError: completed/failed 가 아닐 때
        """
        conn = self._ensure_connection()
        job = self.get_job(job_id)

        allowed = {JobStatus.COMPLETED.value, JobStatus.FAILED.value}
        if job.status not in allowed:
            raise InvalidTransitionError(
                job_id,
                job.status,
                JobStatus.QUEUED.value,
            )

        now = self._now_iso()
        with self._write_lock:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, retry_count = 0, error_message = '', updated_at = ?
                WHERE id = ?
                """,
                (JobStatus.QUEUED.value, now, job_id),
            )
            conn.commit()

        logger.info(f"재전사 초기화: id={job_id} ({job.status} → queued)")
        return self.get_job(job_id)

    def retry_all_failed(self) -> list[int]:
        """재시도 가능한 모든 실패 작업을 재시도한다.

        max_retries를 초과하지 않은 failed 작업만 재시도한다.
        PERF: 단일 SQL 배치 UPDATE로 N+1 쿼리 패턴을 제거한다.

        Returns:
            재시도된 작업 ID 리스트
        """
        conn = self._ensure_connection()
        now = self._now_iso()

        # PERF: 단일 쿼리로 재시도 가능한 작업 조회 + 일괄 UPDATE
        with self._write_lock:
            # 재시도 가능한 작업 ID를 한 번에 조회
            rows = conn.execute(
                """
                SELECT id FROM jobs
                WHERE status = ? AND retry_count < max_retries
                """,
                (JobStatus.FAILED.value,),
            ).fetchall()

            retried_ids = [row["id"] for row in rows]

            if retried_ids:
                # 단일 UPDATE로 일괄 상태 변경
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        retry_count = retry_count + 1,
                        error_message = '',
                        updated_at = ?
                    WHERE status = ? AND retry_count < max_retries
                    """,
                    (JobStatus.QUEUED.value, now, JobStatus.FAILED.value),
                )
                conn.commit()

        if retried_ids:
            logger.info(f"일괄 재시도 완료: {len(retried_ids)}건 — ids={retried_ids}")

        return retried_ids

    def count_by_status(self) -> dict[str, int]:
        """상태별 작업 수를 집계한다.

        Returns:
            상태 문자열 → 작업 수 딕셔너리
        """
        conn = self._ensure_connection()
        rows = conn.execute("SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status").fetchall()

        result: dict[str, int] = {}
        for row in rows:
            result[row["status"]] = row["cnt"]

        return result

    def delete_job(self, job_id: int) -> None:
        """작업을 삭제한다.

        Args:
            job_id: 삭제할 작업 ID

        Raises:
            JobNotFoundError: 작업이 없을 때
        """
        conn = self._ensure_connection()

        # 존재 확인
        self.get_job(job_id)

        # 쓰기 직렬화 (STAB-017)
        with self._write_lock:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()

        logger.info(f"작업 삭제: id={job_id}")

    def cleanup_completed(self, before_days: int = 30) -> int:
        """오래된 완료 작업을 정리한다.

        Args:
            before_days: 이 일수보다 오래된 completed 작업 삭제

        Returns:
            삭제된 작업 수
        """
        conn = self._ensure_connection()

        # PERF: 상단 import 사용, cutoff 계산 간소화
        cutoff_str = (datetime.now() - timedelta(days=before_days)).isoformat()

        # 쓰기 직렬화 (STAB-017)
        with self._write_lock:
            cursor = conn.execute(
                """
                DELETE FROM jobs
                WHERE status = ? AND updated_at < ?
                """,
                (JobStatus.COMPLETED.value, cutoff_str),
            )
            conn.commit()

        deleted = cursor.rowcount
        if deleted > 0:
            logger.info(f"완료 작업 정리: {deleted}건 삭제 (기준: {before_days}일 이전)")

        return deleted


# === 비동기 래퍼 ===


class AsyncJobQueue:
    """JobQueue의 비동기 래퍼.

    asyncio.to_thread를 사용하여 SQLite 블로킹 호출을
    이벤트 루프에서 분리한다.

    Args:
        job_queue: 래핑할 JobQueue 인스턴스

    사용 예시:
        queue = JobQueue(Path("jobs.db"))
        async_queue = AsyncJobQueue(queue)
        await async_queue.initialize()
        job_id = await async_queue.add_job("meeting_001", "/path/audio.m4a")
    """

    def __init__(self, job_queue: JobQueue) -> None:
        """AsyncJobQueue를 초기화한다.

        Args:
            job_queue: 동기 JobQueue 인스턴스
        """
        self._queue = job_queue

    @property
    def queue(self) -> JobQueue:
        """내부 동기 JobQueue를 반환한다."""
        return self._queue

    async def initialize(self) -> None:
        """비동기로 DB를 초기화한다."""
        import asyncio

        await asyncio.to_thread(self._queue.initialize)

    async def close(self) -> None:
        """비동기로 DB 연결을 종료한다."""
        import asyncio

        await asyncio.to_thread(self._queue.close)

    async def add_job(
        self,
        meeting_id: str,
        audio_path: str,
        initial_status: str = JobStatus.QUEUED.value,
    ) -> int:
        """비동기로 새 작업을 등록한다.

        Args:
            meeting_id: 회의 고유 식별자
            audio_path: 오디오 파일 경로
            initial_status: 초기 상태 (기본값: "queued", "recorded"로 설정 시 전사 대기)

        Returns:
            생성된 작업 ID
        """
        import asyncio

        return await asyncio.to_thread(
            self._queue.add_job,
            meeting_id,
            audio_path,
            initial_status,
        )

    async def get_job(self, job_id: int) -> Job:
        """비동기로 작업을 조회한다.

        Args:
            job_id: 작업 ID

        Returns:
            Job 인스턴스
        """
        import asyncio

        return await asyncio.to_thread(self._queue.get_job, job_id)

    async def get_pending_jobs(self) -> list[Job]:
        """비동기로 대기 중 작업 목록을 조회한다.

        Returns:
            queued 상태의 Job 리스트
        """
        import asyncio

        return await asyncio.to_thread(self._queue.get_pending_jobs)

    async def update_status(
        self,
        job_id: int,
        new_status: JobStatus,
        error_message: str = "",
    ) -> Job:
        """비동기로 작업 상태를 변경한다.

        Args:
            job_id: 대상 작업 ID
            new_status: 전이할 상태
            error_message: 에러 메시지

        Returns:
            업데이트된 Job 인스턴스
        """
        import asyncio

        return await asyncio.to_thread(
            self._queue.update_status,
            job_id,
            new_status,
            error_message,
        )

    async def retry_job(self, job_id: int) -> Job:
        """비동기로 실패 작업을 재시도한다.

        Args:
            job_id: 재시도할 작업 ID

        Returns:
            업데이트된 Job 인스턴스
        """
        import asyncio

        return await asyncio.to_thread(self._queue.retry_job, job_id)

    async def retry_all_failed(self) -> list[int]:
        """비동기로 모든 실패 작업을 재시도한다.

        Returns:
            재시도된 작업 ID 리스트
        """
        import asyncio

        return await asyncio.to_thread(self._queue.retry_all_failed)

    async def count_by_status(self) -> dict[str, int]:
        """비동기로 상태별 작업 수를 집계한다.

        Returns:
            상태 문자열 → 작업 수 딕셔너리
        """
        import asyncio

        return await asyncio.to_thread(self._queue.count_by_status)

    async def get_all_jobs(self) -> list[Job]:
        """비동기로 전체 작업을 조회한다.

        Returns:
            전체 Job 리스트
        """
        import asyncio

        return await asyncio.to_thread(self._queue.get_all_jobs)

    async def delete_job(self, job_id: int) -> None:
        """비동기로 작업을 삭제한다.

        Args:
            job_id: 삭제할 작업 ID
        """
        import asyncio

        await asyncio.to_thread(self._queue.delete_job, job_id)

    async def cleanup_completed(self, before_days: int = 30) -> int:
        """비동기로 오래된 완료 작업을 정리한다.

        Args:
            before_days: 기준 일수

        Returns:
            삭제된 작업 수
        """
        import asyncio

        return await asyncio.to_thread(
            self._queue.cleanup_completed,
            before_days,
        )
