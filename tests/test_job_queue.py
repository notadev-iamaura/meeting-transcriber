"""
SQLite 작업 큐 테스트 모듈 (Job Queue Test Module)

목적: core/job_queue.py의 JobQueue, AsyncJobQueue 클래스를 테스트한다.
주요 검증:
    - DB 초기화 (WAL 모드, 테이블 생성, 인덱스)
    - 작업 등록 (add_job) 및 조회 (get_job)
    - 상태 머신 기반 전이 (update_status, queue_job)
    - 재시도 로직 (retry_job, retry_all_failed)
    - 에러 처리 (중복 등록, 유효하지 않은 전이, 존재하지 않는 작업)
    - 비동기 래퍼 (AsyncJobQueue)
    - 한국어 데이터 처리
    - 정리 (cleanup_completed)
의존성: pytest, pytest-asyncio
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.job_queue import (
    VALID_TRANSITIONS,
    AsyncJobQueue,
    AudioAdmissionHold,
    InvalidTransitionError,
    JobNotFoundError,
    JobQueue,
    JobQueueError,
    JobStatus,
    MaxRetriesExceededError,
    cleanup_retranscribe_staging,
    parse_audio_admission_hold,
    parse_retranscribe_claim,
    retranscribe_staging_paths,
    rollback_retranscribe_staging,
)

# === 픽스처 ===


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """테스트용 DB 경로를 반환한다."""
    return tmp_path / "test_jobs.db"


@pytest.fixture
def queue(db_path: Path) -> JobQueue:
    """초기화된 JobQueue 인스턴스를 반환한다."""
    q = JobQueue(db_path, max_retries=3)
    q.initialize()
    yield q
    q.close()


@pytest.fixture
def async_queue(queue: JobQueue) -> AsyncJobQueue:
    """AsyncJobQueue 인스턴스를 반환한다."""
    return AsyncJobQueue(queue)


# === DB 초기화 테스트 ===


class TestJobQueueInitialize:
    """DB 초기화 관련 테스트."""

    def test_initialize_creates_db_file(self, db_path: Path) -> None:
        """initialize()가 DB 파일을 생성하는지 확인한다."""
        q = JobQueue(db_path)
        q.initialize()
        assert db_path.exists()
        q.close()

    def test_initialize_creates_parent_directory(self, tmp_path: Path) -> None:
        """부모 디렉토리가 없어도 자동 생성하는지 확인한다."""
        deep_path = tmp_path / "a" / "b" / "c" / "jobs.db"
        q = JobQueue(deep_path)
        q.initialize()
        assert deep_path.exists()
        q.close()

    def test_initialize_sets_wal_mode(self, queue: JobQueue) -> None:
        """WAL 모드가 설정되었는지 확인한다."""
        conn = queue._ensure_connection()
        result = conn.execute("PRAGMA journal_mode").fetchone()
        assert result[0] == "wal"

    def test_busy_timeout_설정(self, queue: JobQueue) -> None:
        """busy_timeout PRAGMA가 설정되어 있는지 확인한다."""
        result = queue._ensure_connection().execute("PRAGMA busy_timeout").fetchone()
        assert result[0] == 5000

    def test_initialize_creates_jobs_table(self, queue: JobQueue) -> None:
        """jobs 테이블이 생성되었는지 확인한다."""
        conn = queue._ensure_connection()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        assert tables is not None

    def test_initialize_creates_status_index(self, queue: JobQueue) -> None:
        """status 인덱스가 생성되었는지 확인한다."""
        conn = queue._ensure_connection()
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_jobs_status'"
        ).fetchone()
        assert indexes is not None

    def test_initialize_idempotent(self, db_path: Path) -> None:
        """initialize()가 멱등하게 동작하는지 확인한다."""
        q = JobQueue(db_path)
        q.initialize()
        # 다시 초기화해도 에러 없어야 함
        q.initialize()
        q.close()

    def test_db_path_property(self, queue: JobQueue, db_path: Path) -> None:
        """db_path 속성이 올바른 경로를 반환하는지 확인한다."""
        assert queue.db_path == db_path


# === 작업 등록 테스트 ===


class TestAddJob:
    """작업 등록 관련 테스트."""

    def test_add_job_returns_id(self, queue: JobQueue) -> None:
        """add_job()이 양의 정수 ID를 반환하는지 확인한다."""
        job_id = queue.add_job("meeting_001", "/path/to/audio.m4a")
        assert isinstance(job_id, int)
        assert job_id > 0

    def test_add_job_default_status_queued(self, queue: JobQueue) -> None:
        """새 작업의 기본 상태가 queued인지 확인한다."""
        job_id = queue.add_job("meeting_001", "/path/to/audio.m4a")
        job = queue.get_job(job_id)
        assert job.status == JobStatus.QUEUED.value

    def test_add_job_stores_audio_path(self, queue: JobQueue) -> None:
        """오디오 경로가 정확히 저장되는지 확인한다."""
        path = "/path/to/한국어회의.m4a"
        job_id = queue.add_job("meeting_001", path)
        job = queue.get_job(job_id)
        assert job.audio_path == path

    def test_add_job_sets_timestamps(self, queue: JobQueue) -> None:
        """created_at, updated_at이 설정되는지 확인한다."""
        job_id = queue.add_job("meeting_001", "/path/audio.m4a")
        job = queue.get_job(job_id)
        assert job.created_at != ""
        assert job.updated_at != ""

    def test_add_job_initial_retry_count_zero(self, queue: JobQueue) -> None:
        """초기 retry_count가 0인지 확인한다."""
        job_id = queue.add_job("meeting_001", "/path/audio.m4a")
        job = queue.get_job(job_id)
        assert job.retry_count == 0

    def test_add_job_max_retries_from_config(self, db_path: Path) -> None:
        """max_retries가 설정값으로 저장되는지 확인한다."""
        q = JobQueue(db_path, max_retries=5)
        q.initialize()
        job_id = q.add_job("meeting_001", "/path/audio.m4a")
        job = q.get_job(job_id)
        assert job.max_retries == 5
        q.close()

    def test_add_job_duplicate_meeting_id_raises(self, queue: JobQueue) -> None:
        """중복 meeting_id 등록 시 에러가 발생하는지 확인한다."""
        queue.add_job("meeting_001", "/path/audio.m4a")
        with pytest.raises(JobQueueError, match="중복"):
            queue.add_job("meeting_001", "/path/other.m4a")

    def test_add_multiple_jobs(self, queue: JobQueue) -> None:
        """여러 작업을 등록하면 각각 고유 ID를 받는지 확인한다."""
        id1 = queue.add_job("meeting_001", "/path/a.m4a")
        id2 = queue.add_job("meeting_002", "/path/b.m4a")
        id3 = queue.add_job("meeting_003", "/path/c.m4a")
        assert len({id1, id2, id3}) == 3

    def test_add_job_korean_meeting_id(self, queue: JobQueue) -> None:
        """한국어 meeting_id가 정상 저장/조회되는지 확인한다."""
        job_id = queue.add_job("회의_2026년3월", "/path/한글파일.m4a")
        job = queue.get_job(job_id)
        assert job.meeting_id == "회의_2026년3월"
        assert job.audio_path == "/path/한글파일.m4a"


# === 작업 조회 테스트 ===


class TestGetJob:
    """작업 조회 관련 테스트."""

    def test_get_job_returns_correct_data(self, queue: JobQueue) -> None:
        """get_job()이 정확한 데이터를 반환하는지 확인한다."""
        job_id = queue.add_job("meeting_001", "/path/audio.m4a")
        job = queue.get_job(job_id)
        assert job.id == job_id
        assert job.meeting_id == "meeting_001"
        assert job.audio_path == "/path/audio.m4a"

    def test_get_job_not_found_raises(self, queue: JobQueue) -> None:
        """존재하지 않는 작업 조회 시 JobNotFoundError를 발생시킨다."""
        with pytest.raises(JobNotFoundError) as exc_info:
            queue.get_job(9999)
        assert exc_info.value.job_id == 9999

    def test_get_job_by_meeting_id(self, queue: JobQueue) -> None:
        """meeting_id로 조회할 수 있는지 확인한다."""
        queue.add_job("meeting_001", "/path/audio.m4a")
        job = queue.get_job_by_meeting_id("meeting_001")
        assert job is not None
        assert job.meeting_id == "meeting_001"

    def test_get_job_by_meeting_id_not_found(self, queue: JobQueue) -> None:
        """존재하지 않는 meeting_id 조회 시 None을 반환하는지 확인한다."""
        result = queue.get_job_by_meeting_id("nonexistent")
        assert result is None


# === 상태별 조회 테스트 ===


class TestGetJobsByStatus:
    """상태별 조회 관련 테스트."""

    def test_get_pending_jobs_empty(self, queue: JobQueue) -> None:
        """대기 작업이 없으면 빈 리스트를 반환하는지 확인한다."""
        assert queue.get_pending_jobs() == []

    def test_get_pending_jobs(self, queue: JobQueue) -> None:
        """queued 상태의 작업만 반환하는지 확인한다."""
        queue.add_job("m1", "/path/a.m4a")
        queue.add_job("m2", "/path/b.m4a")
        job_id3 = queue.add_job("m3", "/path/c.m4a")

        # m3를 recording으로 전이
        queue.update_status(job_id3, JobStatus.RECORDING)

        pending = queue.get_pending_jobs()
        assert len(pending) == 2
        meeting_ids = {j.meeting_id for j in pending}
        assert meeting_ids == {"m1", "m2"}

    def test_get_pending_jobs_orders_by_queue_entry_time(
        self,
        queue: JobQueue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """recorded 회의는 전사 시작을 누른 순서대로 대기열에서 조회된다."""
        older_recording_id = queue.add_job(
            "older_recording",
            "/path/older.m4a",
            initial_status=JobStatus.RECORDED.value,
        )
        newer_recording_id = queue.add_job(
            "newer_recording",
            "/path/newer.m4a",
            initial_status=JobStatus.RECORDED.value,
        )

        monkeypatch.setattr(queue, "_now_iso", lambda: "2026-01-01T10:00:01")
        queue.update_status(newer_recording_id, JobStatus.QUEUED)
        monkeypatch.setattr(queue, "_now_iso", lambda: "2026-01-01T10:00:02")
        queue.update_status(older_recording_id, JobStatus.QUEUED)

        pending = queue.get_pending_jobs()
        assert [j.meeting_id for j in pending] == ["newer_recording", "older_recording"]

    def test_get_jobs_by_status(self, queue: JobQueue) -> None:
        """특정 상태의 작업만 반환하는지 확인한다."""
        id1 = queue.add_job("m1", "/path/a.m4a")
        queue.add_job("m2", "/path/b.m4a")

        queue.update_status(id1, JobStatus.RECORDING)

        recording = queue.get_jobs_by_status(JobStatus.RECORDING)
        assert len(recording) == 1
        assert recording[0].meeting_id == "m1"

    def test_get_all_jobs(self, queue: JobQueue) -> None:
        """전체 작업을 반환하는지 확인한다."""
        queue.add_job("m1", "/path/a.m4a")
        queue.add_job("m2", "/path/b.m4a")
        queue.add_job("m3", "/path/c.m4a")

        all_jobs = queue.get_all_jobs()
        assert len(all_jobs) == 3

    def test_count_by_status(self, queue: JobQueue) -> None:
        """상태별 집계가 정확한지 확인한다."""
        id1 = queue.add_job("m1", "/path/a.m4a")
        queue.add_job("m2", "/path/b.m4a")
        id3 = queue.add_job("m3", "/path/c.m4a")

        queue.update_status(id1, JobStatus.RECORDING)
        queue.update_status(id3, JobStatus.FAILED, error_message="오류")

        counts = queue.count_by_status()
        assert counts.get("queued", 0) == 1
        assert counts.get("recording", 0) == 1
        assert counts.get("failed", 0) == 1


# === 상태 전이 테스트 ===


class TestUpdateStatus:
    """상태 전이 관련 테스트."""

    def test_valid_transition_queued_to_recording(self, queue: JobQueue) -> None:
        """queued → recording 전이가 성공하는지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")
        job = queue.update_status(job_id, JobStatus.RECORDING)
        assert job.status == JobStatus.RECORDING.value

    def test_valid_transition_queued_to_transcribing(self, queue: JobQueue) -> None:
        """queued → transcribing 전이가 성공하는지 확인한다 (녹음 단계 건너뛰기)."""
        job_id = queue.add_job("m1", "/path/audio.m4a")
        job = queue.update_status(job_id, JobStatus.TRANSCRIBING)
        assert job.status == JobStatus.TRANSCRIBING.value

    def test_full_pipeline_transition(self, queue: JobQueue) -> None:
        """전체 파이프라인 상태 전이가 성공하는지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")

        # queued → recording → transcribing → diarizing → merging → embedding → completed
        transitions = [
            JobStatus.RECORDING,
            JobStatus.TRANSCRIBING,
            JobStatus.DIARIZING,
            JobStatus.MERGING,
            JobStatus.EMBEDDING,
            JobStatus.COMPLETED,
        ]
        for status in transitions:
            job = queue.update_status(job_id, status)
            assert job.status == status.value

    def test_invalid_transition_raises(self, queue: JobQueue) -> None:
        """유효하지 않은 전이 시 InvalidTransitionError가 발생하는지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")
        with pytest.raises(InvalidTransitionError) as exc_info:
            queue.update_status(job_id, JobStatus.MERGING)  # queued → merging 불가

        assert exc_info.value.job_id == job_id
        assert exc_info.value.current_status == "queued"
        assert exc_info.value.target_status == "merging"

    def test_completed_no_transition(self, queue: JobQueue) -> None:
        """completed 상태에서는 어떤 전이도 불가한지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")

        # completed까지 이동
        for status in [
            JobStatus.RECORDING,
            JobStatus.TRANSCRIBING,
            JobStatus.DIARIZING,
            JobStatus.MERGING,
            JobStatus.EMBEDDING,
            JobStatus.COMPLETED,
        ]:
            queue.update_status(job_id, status)

        # completed에서 어떤 전이도 불가
        with pytest.raises(InvalidTransitionError):
            queue.update_status(job_id, JobStatus.QUEUED)

    def test_any_to_failed(self, queue: JobQueue) -> None:
        """어떤 상태에서든 failed로 전이 가능한지 확인한다."""
        # 각 상태에서 failed 전이 테스트
        test_states = [
            JobStatus.QUEUED,
            JobStatus.RECORDING,
            JobStatus.TRANSCRIBING,
            JobStatus.DIARIZING,
            JobStatus.MERGING,
            JobStatus.EMBEDDING,
        ]

        for i, initial_status in enumerate(test_states):
            job_id = queue.add_job(f"m_{i}", f"/path/{i}.m4a")

            # initial_status까지 이동
            if initial_status != JobStatus.QUEUED:
                transitions_to = []
                for status in [
                    JobStatus.RECORDING,
                    JobStatus.TRANSCRIBING,
                    JobStatus.DIARIZING,
                    JobStatus.MERGING,
                    JobStatus.EMBEDDING,
                ]:
                    transitions_to.append(status)
                    if status == initial_status:
                        break
                for status in transitions_to:
                    queue.update_status(job_id, status)

            job = queue.update_status(job_id, JobStatus.FAILED, error_message="테스트 에러")
            assert job.status == JobStatus.FAILED.value

    def test_failed_stores_error_message(self, queue: JobQueue) -> None:
        """failed 전이 시 에러 메시지가 저장되는지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")
        error_msg = "Ollama 연결 실패: Connection refused"
        job = queue.update_status(job_id, JobStatus.FAILED, error_message=error_msg)
        assert job.error_message == error_msg

    def test_update_status_clears_error_on_non_failed(
        self,
        queue: JobQueue,
    ) -> None:
        """failed가 아닌 전이 시 에러 메시지가 초기화되는지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")

        # failed로 전이 (에러 메시지 설정)
        queue.update_status(job_id, JobStatus.FAILED, error_message="에러")

        # queued로 재시도 (에러 메시지 초기화 확인)
        queue.retry_job(job_id)
        job = queue.get_job(job_id)
        assert job.error_message == ""

    def test_update_status_not_found(self, queue: JobQueue) -> None:
        """존재하지 않는 작업 상태 변경 시 에러가 발생하는지 확인한다."""
        with pytest.raises(JobNotFoundError):
            queue.update_status(9999, JobStatus.RECORDING)

    def test_update_status_updates_timestamp(self, queue: JobQueue) -> None:
        """상태 변경 시 updated_at이 갱신되는지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")
        job_before = queue.get_job(job_id)

        import time

        time.sleep(0.01)  # 타임스탬프 차이 보장

        queue.update_status(job_id, JobStatus.RECORDING)
        job_after = queue.get_job(job_id)

        assert job_after.updated_at >= job_before.updated_at


class TestQueueJob:
    """queue_job() 전용 큐잉 의도 저장 테스트."""

    def test_queue_job_sets_requested_action(self, queue: JobQueue) -> None:
        """recorded 작업을 queued 로 바꾸며 실행 의도를 저장한다."""
        job_id = queue.add_job(
            "m_batch_full",
            "/path/audio.m4a",
            initial_status=JobStatus.RECORDED.value,
        )

        job = queue.queue_job(job_id, requested_action="full")

        assert job.status == JobStatus.QUEUED.value
        assert job.requested_action == "full"
        assert job.error_message == ""

    def test_queue_job_rejects_invalid_requested_action(self, queue: JobQueue) -> None:
        """정의되지 않은 action 값은 저장하지 않는다."""
        job_id = queue.add_job(
            "m_invalid_action",
            "/path/audio.m4a",
            initial_status=JobStatus.RECORDED.value,
        )

        with pytest.raises(JobQueueError):
            queue.queue_job(job_id, requested_action="summarize")

    def test_queue_failed_job은_중간_recorded_상태없이_원자적으로_queued로_전이한다(
        self,
        queue: JobQueue,
    ) -> None:
        """강제 재전사는 retry_count를 보존하고 실패 메타데이터를 한 번에 지운다."""
        job_id = queue.add_job(
            "m_force_retry",
            "/path/audio.m4a",
            initial_status=JobStatus.RECORDED.value,
        )
        queue.queue_job(job_id, requested_action="full")
        queue.update_status(job_id, JobStatus.FAILED, error_message="old failure")
        conn = queue._ensure_connection()
        conn.execute("UPDATE jobs SET retry_count = 2 WHERE id = ?", (job_id,))
        conn.commit()

        job = queue.queue_failed_job(job_id)

        assert job.status == JobStatus.QUEUED.value
        assert job.retry_count == 2
        assert job.error_message == ""
        assert job.requested_action == ""

    def test_queue_failed_job은_failed가_아니면_상태를_변경하지_않는다(
        self,
        queue: JobQueue,
    ) -> None:
        """조건부 UPDATE는 경합으로 상태가 바뀐 작업을 다시 queued로 덮지 않는다."""
        job_id = queue.add_job(
            "m_force_retry_race",
            "/path/audio.m4a",
            initial_status=JobStatus.RECORDED.value,
        )

        with pytest.raises(InvalidTransitionError):
            queue.queue_failed_job(job_id)

        assert queue.get_job(job_id).status == JobStatus.RECORDED.value

    def test_retranscribe_claim은_완료상태를_예약하고_일반큐잉을_차단한다(
        self,
        queue: JobQueue,
    ) -> None:
        """재전사 예약과 queued finalize 사이에는 일반 큐 요청이 선점할 수 없다."""
        job_id = queue.add_job(
            "m_retranscribe_claim",
            "/path/audio.m4a",
            initial_status=JobStatus.COMPLETED.value,
        )

        token = "claim-token-1"
        claimed = queue.claim_for_retranscribe(job_id, token)

        assert claimed.status == JobStatus.RECORDING.value
        claim = parse_retranscribe_claim(claimed.requested_action)
        assert claim is not None
        assert claim.original_status == JobStatus.COMPLETED.value
        assert claim.token == token
        assert claim.phase == "claimed"
        with pytest.raises(InvalidTransitionError):
            queue.queue_job(job_id)

        queue.update_retranscribe_claim_phase(job_id, token, "staging")
        queue.update_retranscribe_claim_phase(job_id, token, "purging")
        queue.update_retranscribe_claim_phase(job_id, token, "committing")
        finalized = queue.reset_for_retranscribe(job_id, token)
        assert finalized.status == JobStatus.QUEUED.value
        assert finalized.retry_count == 0
        assert finalized.requested_action == ""

    def test_retranscribe_claim_실패복구는_기존_failed_metadata를_보존한다(
        self,
        queue: JobQueue,
    ) -> None:
        """staging/purge 실패 시 상태·오류·실행 의도가 claim 이전 값으로 돌아간다."""
        job_id = queue.add_job("m_retranscribe_restore", "/path/audio.m4a")
        queue.update_status(job_id, JobStatus.FAILED, error_message="old failure")
        conn = queue._ensure_connection()
        conn.execute(
            "UPDATE jobs SET retry_count = 2, requested_action = 'full' WHERE id = ?",
            (job_id,),
        )
        conn.commit()

        token = "restore-token"
        queue.claim_for_retranscribe(job_id, token)
        queue.update_retranscribe_claim_phase(job_id, token, "staging")
        restored = queue.restore_retranscribe_claim(job_id, token)

        assert restored.status == JobStatus.FAILED.value
        assert restored.retry_count == 2
        assert restored.error_message == "old failure"
        assert restored.requested_action == "full"

    def test_retranscribe_claim_동시요청은_하나만_예약한다(
        self,
        queue: JobQueue,
    ) -> None:
        """동일 job의 동시 재전사 요청은 조건부 UPDATE 한 건만 성공한다."""
        job_id = queue.add_job(
            "m_retranscribe_concurrent_claim",
            "/path/audio.m4a",
            initial_status=JobStatus.COMPLETED.value,
        )

        def _claim(index: int) -> str:
            try:
                return queue.claim_for_retranscribe(job_id, f"token-{index}").status
            except InvalidTransitionError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(_claim, range(2)))

        assert sorted(results) == ["conflict", JobStatus.RECORDING.value]

    def test_retranscribe_finalize는_committing_claim만_허용한다(
        self,
        queue: JobQueue,
    ) -> None:
        """completed 직접 reset과 purging 조기 finalize를 모두 거부한다."""
        job_id = queue.add_job(
            "m_retranscribe_finalize_gate",
            "/path/audio.m4a",
            initial_status=JobStatus.COMPLETED.value,
        )
        with pytest.raises(InvalidTransitionError):
            queue.reset_for_retranscribe(job_id, "token")

        token = "finalize-token"
        queue.claim_for_retranscribe(job_id, token)
        queue.update_retranscribe_claim_phase(job_id, token, "staging")
        queue.update_retranscribe_claim_phase(job_id, token, "purging")
        with pytest.raises(JobQueueError, match="phase"):
            queue.reset_for_retranscribe(job_id, token)
        assert queue.get_job(job_id).status == JobStatus.RECORDING.value

    def test_retranscribe_claim은_파싱불가_token을_저장하지_않는다(
        self,
        queue: JobQueue,
    ) -> None:
        """payload parser와 불일치하는 token으로 고착 상태를 만들 수 없다."""
        job_id = queue.add_job(
            "m_retranscribe_invalid_token",
            "/path/audio.m4a",
            initial_status=JobStatus.COMPLETED.value,
        )

        with pytest.raises(JobQueueError, match="token"):
            queue.claim_for_retranscribe(job_id, "bad token\x00")

        assert queue.get_job(job_id).status == JobStatus.COMPLETED.value

    def test_queue_jobs_atomically는_하나가_부적합하면_전체를_rollback한다(
        self,
        queue: JobQueue,
    ) -> None:
        """두 번째 CAS 실패가 첫 작업만 queued로 남기지 않는다."""
        first = queue.add_job("m_atomic_first", "/path/a.wav", initial_status="recorded")
        second = queue.add_job("m_atomic_second", "/path/b.wav", initial_status="recorded")
        queue.queue_job(second, "transcribe")

        with pytest.raises(JobQueueError, match="CAS"):
            queue.queue_jobs_atomically([first, second], "transcribe")

        assert queue.get_job(first).status == JobStatus.RECORDED.value
        assert queue.get_job(second).status == JobStatus.QUEUED.value

    def test_queue_jobs_atomically는_SQL실패도_rollback해_connection을_재사용한다(
        self,
        queue: JobQueue,
    ) -> None:
        """SQLite 예외가 나도 열린 transaction이나 부분 상태를 남기지 않는다."""
        first = queue.add_job("m_atomic_sql_first", "/path/a.wav", initial_status="recorded")
        second = queue.add_job("m_atomic_sql_second", "/path/b.wav", initial_status="recorded")
        conn = queue._ensure_connection()
        conn.execute(
            f"""
            CREATE TRIGGER reject_second_batch_update
            BEFORE UPDATE OF status ON jobs
            WHEN OLD.id = {second} AND NEW.status = 'queued'
            BEGIN
                SELECT RAISE(ABORT, 'forced batch failure');
            END
            """
        )
        conn.commit()

        try:
            with pytest.raises(JobQueueError, match="SQLite"):
                queue.queue_jobs_atomically([first, second], "transcribe")

            assert conn.in_transaction is False
            assert queue.get_job(first).status == JobStatus.RECORDED.value
            assert queue.get_job(second).status == JobStatus.RECORDED.value
        finally:
            conn.rollback()
            conn.execute("DROP TRIGGER IF EXISTS reject_second_batch_update")
            conn.commit()

    def test_queue_jobs_atomically는_모든_recorded를_한번에_queued로_바꾼다(
        self,
        queue: JobQueue,
    ) -> None:
        """전체 CAS 성공 시 동일 action을 저장한다."""
        first = queue.add_job("m_atomic_ok_a", "/path/a.wav", initial_status="recorded")
        second = queue.add_job("m_atomic_ok_b", "/path/b.wav", initial_status="recorded")

        queued = queue.queue_jobs_atomically([first, second], "full")

        assert [job.status for job in queued] == ["queued", "queued"]
        assert [job.requested_action for job in queued] == ["full", "full"]

    def test_audio_admission_hold는_reopen후_queued_action을_복원한다(
        self,
        db_path: Path,
    ) -> None:
        """프로세스 중단 뒤에도 DB payload만으로 queued 의도를 복구한다."""
        first_queue = JobQueue(db_path)
        first_queue.initialize()
        job_id = first_queue.add_job(
            "m_hold_reopen",
            "/path/audio.wav",
            initial_status=JobStatus.RECORDED.value,
        )
        first_queue.queue_job(job_id, "full")
        held = first_queue.hold_job_for_audio_admission(job_id, "hold-token-1")
        parsed = parse_audio_admission_hold(held.requested_action)
        assert parsed == AudioAdmissionHold(
            original_status=JobStatus.QUEUED.value,
            original_requested_action="full",
            token="hold-token-1",
        )
        first_queue.close()

        reopened = JobQueue(db_path)
        reopened.initialize()
        finalized = reopened.finalize_audio_admission_hold(job_id, "hold-token-1")
        assert finalized.status == JobStatus.QUEUED.value
        assert finalized.requested_action == "full"
        reopened.close()

    def test_audio_admission_hold_failed_origin은_recorded로_finalize한다(
        self,
        queue: JobQueue,
    ) -> None:
        """failed legacy 입력 ACCEPT 후 stale 오류/action을 노출하지 않는다."""
        job_id = queue.add_job("m_hold_failed", "/path/audio.wav")
        queue.update_status(job_id, JobStatus.FAILED, "stale failure")
        conn = queue._ensure_connection()
        conn.execute("UPDATE jobs SET requested_action = 'full' WHERE id = ?", (job_id,))
        conn.commit()

        held = queue.hold_job_for_audio_admission(job_id, "hold-failed")
        assert held.status == JobStatus.RECORDED.value
        assert held.error_message == ""
        finalized = queue.finalize_audio_admission_hold(job_id, "hold-failed")

        assert finalized.status == JobStatus.RECORDED.value
        assert finalized.requested_action == ""
        assert finalized.error_message == ""

    def test_audio_admission_hold_stale_token은_상태를_바꾸지_않는다(
        self,
        queue: JobQueue,
    ) -> None:
        """늦게 끝난 validator는 다른 hold를 finalize할 수 없다."""
        job_id = queue.add_job("m_hold_stale", "/path/audio.wav")
        held = queue.hold_job_for_audio_admission(job_id, "current-token")

        with pytest.raises(JobQueueError, match="token"):
            queue.finalize_audio_admission_hold(job_id, "stale-token")

        unchanged = queue.get_job(job_id)
        assert unchanged.status == JobStatus.RECORDED.value
        assert unchanged.requested_action == held.requested_action

    @pytest.mark.parametrize("token", ["", "bad token", "bad\x00token", "/bad"])
    def test_audio_admission_hold는_파싱불가_token을_거부한다(
        self,
        queue: JobQueue,
        token: str,
    ) -> None:
        """저장 직후 parser가 읽지 못하는 hold payload를 만들지 않는다."""
        job_id = queue.add_job(f"m_hold_invalid_{len(token)}", "/path/audio.wav")

        with pytest.raises(JobQueueError):
            queue.hold_job_for_audio_admission(job_id, token)

        assert queue.get_job(job_id).status == JobStatus.QUEUED.value

    def test_retranscribe_staging_root_symlink는_외부를_이동하지_않는다(
        self,
        tmp_path: Path,
    ) -> None:
        """rollback helper가 root symlink target의 staging을 절대 복구하지 않는다."""
        outside = tmp_path / "outside"
        outside.mkdir()
        meeting_id = "회의 1"
        token = "root-token"
        staged = outside / f".retranscribe-{meeting_id}-{token}-checkpoints"
        staged.mkdir()
        sentinel = staged / "pipeline_state.json"
        sentinel.write_text("outside", encoding="utf-8")
        linked_root = tmp_path / "linked-checkpoints"
        linked_root.symlink_to(outside, target_is_directory=True)
        outputs_root = tmp_path / "outputs"
        outputs_root.mkdir()

        with pytest.raises(JobQueueError, match="심볼릭 링크"):
            rollback_retranscribe_staging(
                linked_root,
                outputs_root,
                meeting_id,
                token,
            )

        assert sentinel.read_text(encoding="utf-8") == "outside"
        assert not (outside / meeting_id).exists()

    def test_retranscribe_cleanup은_멱등하고_unsafe_stage를_거부한다(
        self,
        tmp_path: Path,
    ) -> None:
        """committing recovery cleanup은 반복 가능하며 stage symlink를 따르지 않는다."""
        checkpoints_root = tmp_path / "checkpoints"
        outputs_root = tmp_path / "outputs"
        checkpoints_root.mkdir()
        outputs_root.mkdir()
        checkpoint_stage, output_stage = retranscribe_staging_paths(
            checkpoints_root,
            outputs_root,
            "회의 1",
            "cleanup-token",
        )
        checkpoint_stage.mkdir()
        output_stage.mkdir()

        cleanup_retranscribe_staging(
            checkpoints_root,
            outputs_root,
            "회의 1",
            "cleanup-token",
        )
        cleanup_retranscribe_staging(
            checkpoints_root,
            outputs_root,
            "회의 1",
            "cleanup-token",
        )
        assert not checkpoint_stage.exists()
        assert not output_stage.exists()

        outside = tmp_path / "outside-cleanup"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("keep", encoding="utf-8")
        checkpoint_stage.symlink_to(outside, target_is_directory=True)
        with pytest.raises(JobQueueError, match="안전한 디렉터리"):
            cleanup_retranscribe_staging(
                checkpoints_root,
                outputs_root,
                "회의 1",
                "cleanup-token",
            )
        assert sentinel.read_text(encoding="utf-8") == "keep"

    def test_retranscribe_cleanup_intermediate_swap은_외부_stage를_삭제하지_않음(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """root 검사 직후 intermediate가 symlink로 교체돼도 외부를 안 지운다."""
        base = tmp_path / "base"
        safe = base / "safe"
        checkpoints_root = safe / "checkpoints"
        checkpoints_root.mkdir(parents=True)
        outputs_root = tmp_path / "outputs"
        outputs_root.mkdir()

        meeting_id = "cleanup-swap"
        token = "cleanup-swap-token"
        stage_name = f".retranscribe-{meeting_id}-{token}-checkpoints"
        outside = tmp_path / "outside"
        outside_stage = outside / "checkpoints" / stage_name
        outside_stage.mkdir(parents=True)
        sentinel = outside_stage / "pipeline_state.json"
        sentinel.write_bytes(b"external-sentinel")
        sentinel_before = sentinel.stat()
        safe_original = base / "safe-original"
        original_lstat = Path.lstat
        swapped = False

        def swap_after_validation(path: Path) -> object:
            nonlocal swapped
            entry_stat = original_lstat(path)
            if path == safe and not swapped:
                swapped = True
                safe.rename(safe_original)
                safe.symlink_to(outside, target_is_directory=True)
            return entry_stat

        monkeypatch.setattr(Path, "lstat", swap_after_validation)

        with pytest.raises(JobQueueError):
            cleanup_retranscribe_staging(
                checkpoints_root,
                outputs_root,
                meeting_id,
                token,
            )

        sentinel_after = sentinel.stat()
        assert sentinel.read_bytes() == b"external-sentinel"
        assert (sentinel_after.st_dev, sentinel_after.st_ino) == (
            sentinel_before.st_dev,
            sentinel_before.st_ino,
        )

    def test_retranscribe_rollback_intermediate_swap은_외부_artifact를_이동하지_않음(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """rollback의 두 번째 root 검사 직후 swap이 외부 rename으로 이어지지 않는다."""
        base = tmp_path / "base"
        safe = base / "safe"
        checkpoints_root = safe / "checkpoints"
        checkpoints_root.mkdir(parents=True)
        outputs_root = tmp_path / "outputs"
        outputs_root.mkdir()

        meeting_id = "rollback-swap"
        token = "rollback-swap-token"
        stage_name = f".retranscribe-{meeting_id}-{token}-checkpoints"
        outside = tmp_path / "outside"
        outside_stage = outside / "checkpoints" / stage_name
        outside_stage.mkdir(parents=True)
        sentinel = outside_stage / "pipeline_state.json"
        sentinel.write_bytes(b"external-sentinel")
        sentinel_before = sentinel.stat()
        safe_original = base / "safe-original"
        original_lstat = Path.lstat
        safe_validation_count = 0

        def swap_on_second_validation(path: Path) -> object:
            nonlocal safe_validation_count
            entry_stat = original_lstat(path)
            if path == safe:
                safe_validation_count += 1
                if safe_validation_count == 2:
                    safe.rename(safe_original)
                    safe.symlink_to(outside, target_is_directory=True)
            return entry_stat

        monkeypatch.setattr(Path, "lstat", swap_on_second_validation)

        try:
            rollback_retranscribe_staging(
                checkpoints_root,
                outputs_root,
                meeting_id,
                token,
            )
        except JobQueueError:
            pass

        sentinel_after = sentinel.stat()
        assert sentinel.read_bytes() == b"external-sentinel"
        assert (sentinel_after.st_dev, sentinel_after.st_ino) == (
            sentinel_before.st_dev,
            sentinel_before.st_ino,
        )
        assert not (outside / "checkpoints" / meeting_id).exists()

    def test_retranscribe_output_rollback은_rename직전_entry교체를_publish하지_않음(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """검증 뒤 교체된 output entry는 원본 위치에 유효 산출물로 게시하지 않는다."""
        checkpoints_root = tmp_path / "checkpoints"
        outputs_root = tmp_path / "outputs"
        checkpoints_root.mkdir()
        outputs_root.mkdir()
        meeting_id = "output-entry-swap"
        token = "output-entry-token"
        _checkpoint_stage, output_stage = retranscribe_staging_paths(
            checkpoints_root,
            outputs_root,
            meeting_id,
            token,
        )
        output_stage.mkdir()
        staged_file = output_stage / "corrected.json"
        staged_file.write_bytes(b"expected-output")
        (outputs_root / meeting_id).mkdir()
        real_rename = os.rename
        swapped = False

        def swap_inside_rename(
            source: str | Path,
            target: str | Path,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            nonlocal swapped
            if source == "corrected.json" and target == "corrected.json" and not swapped:
                swapped = True
                assert src_dir_fd is not None
                real_rename(
                    source,
                    "corrected.expected",
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=src_dir_fd,
                )
                replacement_fd = os.open(
                    "corrected.json",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=src_dir_fd,
                )
                try:
                    os.write(replacement_fd, b"foreign-replacement")
                finally:
                    os.close(replacement_fd)
            real_rename(
                source,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        monkeypatch.setattr("core.job_queue.os.rename", swap_inside_rename)

        with pytest.raises(JobQueueError):
            rollback_retranscribe_staging(
                checkpoints_root,
                outputs_root,
                meeting_id,
                token,
            )

        assert swapped is True
        assert not (outputs_root / meeting_id / "corrected.json").exists()
        preserved = sorted(path.read_bytes() for path in output_stage.iterdir())
        assert preserved == [b"expected-output", b"foreign-replacement"]

    def test_retranscribe_checkpoint_rollback은_whole_stage_entry를_rename하지_않음(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """checkpoint original이 없어도 stage 이름 자체를 meeting 이름으로 게시하지 않는다."""
        checkpoints_root = tmp_path / "checkpoints"
        outputs_root = tmp_path / "outputs"
        checkpoints_root.mkdir()
        outputs_root.mkdir()
        meeting_id = "checkpoint-whole-stage-swap"
        token = "checkpoint-whole-stage-token"
        checkpoint_stage, _output_stage = retranscribe_staging_paths(
            checkpoints_root,
            outputs_root,
            meeting_id,
            token,
        )
        checkpoint_stage.mkdir()
        (checkpoint_stage / "pipeline_state.json").write_bytes(b"expected-checkpoint")
        real_rename = os.rename
        whole_stage_rename_calls = 0

        def observe_whole_stage_rename(
            source: str | Path,
            target: str | Path,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            nonlocal whole_stage_rename_calls
            if source == checkpoint_stage.name and target == meeting_id:
                whole_stage_rename_calls += 1
            real_rename(
                source,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        monkeypatch.setattr("core.job_queue.os.rename", observe_whole_stage_rename)

        rollback_retranscribe_staging(
            checkpoints_root,
            outputs_root,
            meeting_id,
            token,
        )

        assert whole_stage_rename_calls == 0
        assert (checkpoints_root / meeting_id / "pipeline_state.json").read_bytes() == (
            b"expected-checkpoint"
        )

    def test_retranscribe_checkpoint_rollback은_rename직전_child교체를_publish하지_않음(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """검증 뒤 교체된 checkpoint child는 canonical meeting dir에 게시하지 않는다."""
        checkpoints_root = tmp_path / "checkpoints"
        outputs_root = tmp_path / "outputs"
        checkpoints_root.mkdir()
        outputs_root.mkdir()
        meeting_id = "checkpoint-child-swap"
        token = "checkpoint-child-token"
        checkpoint_stage, _output_stage = retranscribe_staging_paths(
            checkpoints_root,
            outputs_root,
            meeting_id,
            token,
        )
        checkpoint_stage.mkdir()
        (checkpoint_stage / "pipeline_state.json").write_bytes(b"expected-checkpoint")
        (checkpoints_root / meeting_id).mkdir()
        real_rename = os.rename
        swapped = False

        def swap_inside_rename(
            source: str | Path,
            target: str | Path,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            nonlocal swapped
            if source == "pipeline_state.json" and target == source and not swapped:
                swapped = True
                assert src_dir_fd is not None
                real_rename(
                    source,
                    "pipeline_state.expected.json",
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=src_dir_fd,
                )
                replacement_fd = os.open(
                    source,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=src_dir_fd,
                )
                try:
                    os.write(replacement_fd, b"foreign-checkpoint")
                finally:
                    os.close(replacement_fd)
            real_rename(
                source,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        monkeypatch.setattr("core.job_queue.os.rename", swap_inside_rename)

        with pytest.raises(JobQueueError):
            rollback_retranscribe_staging(
                checkpoints_root,
                outputs_root,
                meeting_id,
                token,
            )

        assert swapped is True
        assert not (checkpoints_root / meeting_id / "pipeline_state.json").exists()
        preserved = sorted(path.read_bytes() for path in checkpoint_stage.iterdir())
        assert preserved == [b"expected-checkpoint", b"foreign-checkpoint"]

    def test_queue_job_uses_pending_order_timestamp(
        self,
        queue: JobQueue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """queue_job 으로 들어간 일괄 작업도 큐 진입 순서를 따른다."""
        first = queue.add_job("m_first", "/path/first.m4a", initial_status="recorded")
        second = queue.add_job("m_second", "/path/second.m4a", initial_status="recorded")

        monkeypatch.setattr(queue, "_now_iso", lambda: "2026-01-01T10:00:01")
        queue.queue_job(second, requested_action="transcribe")
        monkeypatch.setattr(queue, "_now_iso", lambda: "2026-01-01T10:00:02")
        queue.queue_job(first, requested_action="full")

        pending = queue.get_pending_jobs()
        assert [(j.meeting_id, j.requested_action) for j in pending] == [
            ("m_second", "transcribe"),
            ("m_first", "full"),
        ]


# === 재시도 테스트 ===


class TestRetryJob:
    """재시도 관련 테스트."""

    def test_retry_job_increments_count(self, queue: JobQueue) -> None:
        """retry_job()이 retry_count를 증가시키는지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")
        queue.update_status(job_id, JobStatus.FAILED, error_message="에러")

        job = queue.retry_job(job_id)
        assert job.retry_count == 1
        assert job.status == JobStatus.QUEUED.value

    def test_retry_job_clears_error(self, queue: JobQueue) -> None:
        """retry_job()이 에러 메시지를 초기화하는지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")
        queue.update_status(job_id, JobStatus.FAILED, error_message="에러")

        job = queue.retry_job(job_id)
        assert job.error_message == ""

    def test_retry_job_max_exceeded(self, queue: JobQueue) -> None:
        """max_retries 초과 시 MaxRetriesExceededError가 발생하는지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")

        # 3번 재시도 (max_retries=3)
        for i in range(3):
            queue.update_status(job_id, JobStatus.FAILED, error_message=f"에러 {i}")
            queue.retry_job(job_id)

        # 4번째는 실패해야 함
        queue.update_status(job_id, JobStatus.FAILED, error_message="에러 3")
        with pytest.raises(MaxRetriesExceededError) as exc_info:
            queue.retry_job(job_id)

        assert exc_info.value.job_id == job_id
        assert exc_info.value.retry_count == 3
        assert exc_info.value.max_retries == 3

    def test_retry_non_failed_raises(self, queue: JobQueue) -> None:
        """failed가 아닌 작업 재시도 시 에러가 발생하는지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")
        with pytest.raises(InvalidTransitionError):
            queue.retry_job(job_id)

    def test_retry_all_failed(self, queue: JobQueue) -> None:
        """retry_all_failed()가 재시도 가능한 작업만 재시도하는지 확인한다."""
        id1 = queue.add_job("m1", "/path/a.m4a")
        id2 = queue.add_job("m2", "/path/b.m4a")
        _id3 = queue.add_job("m3", "/path/c.m4a")

        # m1, m2를 failed로
        queue.update_status(id1, JobStatus.FAILED, error_message="에러1")
        queue.update_status(id2, JobStatus.FAILED, error_message="에러2")
        # m3는 queued 유지

        retried = queue.retry_all_failed()
        assert set(retried) == {id1, id2}

        # 모두 queued로 복귀
        for jid in [id1, id2]:
            job = queue.get_job(jid)
            assert job.status == JobStatus.QUEUED.value

    def test_retry_all_failed_skips_exhausted(self, queue: JobQueue) -> None:
        """max_retries 초과 작업은 건너뛰는지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")

        # 3번 재시도하여 소진
        for i in range(3):
            queue.update_status(job_id, JobStatus.FAILED, error_message=f"에러{i}")
            queue.retry_job(job_id)

        # 다시 실패
        queue.update_status(job_id, JobStatus.FAILED, error_message="최종 에러")

        retried = queue.retry_all_failed()
        assert retried == []  # 재시도 불가

    def test_retry_all_failed_empty_queue(self, queue: JobQueue) -> None:
        """실패 작업이 없으면 빈 리스트를 반환하는지 확인한다."""
        retried = queue.retry_all_failed()
        assert retried == []


# === 삭제 및 정리 테스트 ===


class TestDeleteAndCleanup:
    """작업 삭제 및 정리 관련 테스트."""

    def test_delete_job(self, queue: JobQueue) -> None:
        """작업 삭제가 정상 동작하는지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")
        queue.delete_job(job_id)

        with pytest.raises(JobNotFoundError):
            queue.get_job(job_id)

    def test_delete_job_not_found(self, queue: JobQueue) -> None:
        """존재하지 않는 작업 삭제 시 에러가 발생하는지 확인한다."""
        with pytest.raises(JobNotFoundError):
            queue.delete_job(9999)

    def test_cleanup_completed(self, queue: JobQueue) -> None:
        """오래된 완료 작업이 정리되는지 확인한다."""
        # 작업 추가 후 완료 처리
        job_id = queue.add_job("m1", "/path/audio.m4a")
        for status in [
            JobStatus.RECORDING,
            JobStatus.TRANSCRIBING,
            JobStatus.DIARIZING,
            JobStatus.MERGING,
            JobStatus.EMBEDDING,
            JobStatus.COMPLETED,
        ]:
            queue.update_status(job_id, status)

        # before_days=0 → 모든 완료 작업 삭제
        deleted = queue.cleanup_completed(before_days=0)
        assert deleted == 1

    def test_cleanup_completed_preserves_recent(self, queue: JobQueue) -> None:
        """최근 완료 작업은 보존되는지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")
        for status in [
            JobStatus.RECORDING,
            JobStatus.TRANSCRIBING,
            JobStatus.DIARIZING,
            JobStatus.MERGING,
            JobStatus.EMBEDDING,
            JobStatus.COMPLETED,
        ]:
            queue.update_status(job_id, status)

        # before_days=30 → 최근 작업은 보존
        deleted = queue.cleanup_completed(before_days=30)
        assert deleted == 0


# === 상태 머신 유효성 테스트 ===


class TestStateMachine:
    """상태 머신 유효성 테스트."""

    def test_all_statuses_have_transitions(self) -> None:
        """모든 상태에 대한 전이 규칙이 정의되어 있는지 확인한다."""
        for status in JobStatus:
            assert status in VALID_TRANSITIONS

    def test_completed_has_no_transitions(self) -> None:
        """completed 상태에서 전이 불가인지 확인한다."""
        assert VALID_TRANSITIONS[JobStatus.COMPLETED] == set()

    def test_failed_can_only_retry(self) -> None:
        """failed 상태에서 queued로만 전이 가능한지 확인한다."""
        assert VALID_TRANSITIONS[JobStatus.FAILED] == {JobStatus.QUEUED}

    def test_all_non_terminal_can_fail(self) -> None:
        """completed를 제외한 모든 상태에서 failed 전이가 가능한지 확인한다."""
        non_terminal = [s for s in JobStatus if s not in (JobStatus.COMPLETED, JobStatus.FAILED)]
        for status in non_terminal:
            assert JobStatus.FAILED in VALID_TRANSITIONS[status], (
                f"{status.value}에서 failed 전이 불가"
            )


# === 연결 관리 테스트 ===


class TestConnectionManagement:
    """DB 연결 관리 테스트."""

    def test_ensure_connection_without_init_raises(self, db_path: Path) -> None:
        """initialize() 없이 연결 확인 시 에러가 발생하는지 확인한다."""
        q = JobQueue(db_path)
        with pytest.raises(JobQueueError, match="초기화"):
            q.add_job("m1", "/path/audio.m4a")

    def test_close_and_reopen(self, db_path: Path) -> None:
        """close 후 재초기화가 가능한지 확인한다."""
        q = JobQueue(db_path)
        q.initialize()
        job_id = q.add_job("m1", "/path/audio.m4a")
        q.close()

        # 재초기화 후 데이터 보존 확인
        q2 = JobQueue(db_path)
        q2.initialize()
        job = q2.get_job(job_id)
        assert job.meeting_id == "m1"
        q2.close()


# === 비동기 래퍼 테스트 ===


class TestAsyncJobQueue:
    """AsyncJobQueue 비동기 래퍼 테스트."""

    pytestmark = pytest.mark.asyncio

    async def test_async_add_and_get_job(
        self,
        async_queue: AsyncJobQueue,
    ) -> None:
        """비동기로 작업 등록 및 조회가 되는지 확인한다."""
        job_id = await async_queue.add_job("m1", "/path/audio.m4a")
        job = await async_queue.get_job(job_id)
        assert job.meeting_id == "m1"
        assert job.status == JobStatus.QUEUED.value

    async def test_async_update_status(
        self,
        async_queue: AsyncJobQueue,
    ) -> None:
        """비동기로 상태 변경이 되는지 확인한다."""
        job_id = await async_queue.add_job("m1", "/path/audio.m4a")
        job = await async_queue.update_status(job_id, JobStatus.RECORDING)
        assert job.status == JobStatus.RECORDING.value

    async def test_async_queue_job(
        self,
        async_queue: AsyncJobQueue,
    ) -> None:
        """비동기로 queue_job 실행 의도를 저장할 수 있다."""
        job_id = await async_queue.add_job(
            "m_batch",
            "/path/audio.m4a",
            initial_status=JobStatus.RECORDED.value,
        )

        job = await async_queue.queue_job(job_id, requested_action="transcribe")

        assert job.status == JobStatus.QUEUED.value
        assert job.requested_action == "transcribe"

    async def test_async_get_pending_jobs(
        self,
        async_queue: AsyncJobQueue,
    ) -> None:
        """비동기로 대기 작업 조회가 되는지 확인한다."""
        await async_queue.add_job("m1", "/path/a.m4a")
        await async_queue.add_job("m2", "/path/b.m4a")

        pending = await async_queue.get_pending_jobs()
        assert len(pending) == 2

    async def test_async_retry_job(
        self,
        async_queue: AsyncJobQueue,
    ) -> None:
        """비동기로 재시도가 되는지 확인한다."""
        job_id = await async_queue.add_job("m1", "/path/audio.m4a")
        await async_queue.update_status(job_id, JobStatus.FAILED, error_message="에러")

        job = await async_queue.retry_job(job_id)
        assert job.status == JobStatus.QUEUED.value
        assert job.retry_count == 1

    async def test_async_retry_all_failed(
        self,
        async_queue: AsyncJobQueue,
    ) -> None:
        """비동기로 일괄 재시도가 되는지 확인한다."""
        id1 = await async_queue.add_job("m1", "/path/a.m4a")
        id2 = await async_queue.add_job("m2", "/path/b.m4a")

        await async_queue.update_status(id1, JobStatus.FAILED, error_message="에러1")
        await async_queue.update_status(id2, JobStatus.FAILED, error_message="에러2")

        retried = await async_queue.retry_all_failed()
        assert set(retried) == {id1, id2}

    async def test_async_count_by_status(
        self,
        async_queue: AsyncJobQueue,
    ) -> None:
        """비동기로 상태별 집계가 되는지 확인한다."""
        id1 = await async_queue.add_job("m1", "/path/a.m4a")
        await async_queue.add_job("m2", "/path/b.m4a")
        await async_queue.update_status(id1, JobStatus.RECORDING)

        counts = await async_queue.count_by_status()
        assert counts.get("queued", 0) == 1
        assert counts.get("recording", 0) == 1

    async def test_async_get_all_jobs(
        self,
        async_queue: AsyncJobQueue,
    ) -> None:
        """비동기로 전체 작업 조회가 되는지 확인한다."""
        await async_queue.add_job("m1", "/path/a.m4a")
        await async_queue.add_job("m2", "/path/b.m4a")

        all_jobs = await async_queue.get_all_jobs()
        assert len(all_jobs) == 2

    async def test_async_delete_job(
        self,
        async_queue: AsyncJobQueue,
    ) -> None:
        """비동기로 작업 삭제가 되는지 확인한다."""
        job_id = await async_queue.add_job("m1", "/path/audio.m4a")
        await async_queue.delete_job(job_id)

        with pytest.raises(JobNotFoundError):
            await async_queue.get_job(job_id)

    async def test_async_cleanup_completed(
        self,
        async_queue: AsyncJobQueue,
    ) -> None:
        """비동기로 완료 작업 정리가 되는지 확인한다."""
        job_id = await async_queue.add_job("m1", "/path/audio.m4a")

        for status in [
            JobStatus.RECORDING,
            JobStatus.TRANSCRIBING,
            JobStatus.DIARIZING,
            JobStatus.MERGING,
            JobStatus.EMBEDDING,
            JobStatus.COMPLETED,
        ]:
            await async_queue.update_status(job_id, status)

        deleted = await async_queue.cleanup_completed(before_days=0)
        assert deleted == 1

    async def test_async_queue_property(
        self,
        async_queue: AsyncJobQueue,
    ) -> None:
        """queue 속성이 내부 JobQueue를 반환하는지 확인한다."""
        assert isinstance(async_queue.queue, JobQueue)


# === 한국어 데이터 통합 테스트 ===


class TestKoreanDataIntegration:
    """한국어 데이터 처리 통합 테스트."""

    def test_korean_error_message(self, queue: JobQueue) -> None:
        """한국어 에러 메시지가 정상 저장/조회되는지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")
        error_msg = "Ollama 서버 연결 실패: 타임아웃 120초 초과"
        queue.update_status(job_id, JobStatus.FAILED, error_message=error_msg)

        job = queue.get_job(job_id)
        assert job.error_message == error_msg

    def test_korean_path_with_spaces(self, queue: JobQueue) -> None:
        """공백과 한국어가 포함된 경로가 정상 처리되는지 확인한다."""
        path = "/Users/홍길동/문서/회의 녹음/2026년 3월.m4a"
        job_id = queue.add_job("m1", path)
        job = queue.get_job(job_id)
        assert job.audio_path == path


# === 엣지 케이스 테스트 ===


class TestEdgeCases:
    """엣지 케이스 테스트."""

    def test_concurrent_add_different_meetings(self, queue: JobQueue) -> None:
        """다수의 회의를 동시에 등록해도 문제 없는지 확인한다."""
        ids = []
        for i in range(20):
            job_id = queue.add_job(f"meeting_{i:03d}", f"/path/{i}.m4a")
            ids.append(job_id)
        assert len(set(ids)) == 20

    def test_empty_audio_path(self, queue: JobQueue) -> None:
        """빈 오디오 경로도 등록 가능한지 확인한다 (검증은 파이프라인에서)."""
        job_id = queue.add_job("m1", "")
        job = queue.get_job(job_id)
        assert job.audio_path == ""

    def test_rapid_status_transitions(self, queue: JobQueue) -> None:
        """빠른 연속 상태 전이가 정상 동작하는지 확인한다."""
        job_id = queue.add_job("m1", "/path/audio.m4a")
        statuses = [
            JobStatus.RECORDING,
            JobStatus.TRANSCRIBING,
            JobStatus.DIARIZING,
            JobStatus.MERGING,
            JobStatus.EMBEDDING,
            JobStatus.COMPLETED,
        ]
        for s in statuses:
            queue.update_status(job_id, s)

        job = queue.get_job(job_id)
        assert job.status == JobStatus.COMPLETED.value

    def test_custom_max_retries(self, db_path: Path) -> None:
        """커스텀 max_retries가 적용되는지 확인한다."""
        q = JobQueue(db_path, max_retries=1)
        q.initialize()

        job_id = q.add_job("m1", "/path/audio.m4a")
        q.update_status(job_id, JobStatus.FAILED, error_message="에러")
        q.retry_job(job_id)

        # 1번 재시도 후 다시 실패
        q.update_status(job_id, JobStatus.FAILED, error_message="에러2")
        with pytest.raises(MaxRetriesExceededError):
            q.retry_job(job_id)

        q.close()
