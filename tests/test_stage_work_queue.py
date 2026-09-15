"""단계 큐의 영속성, 의존성, 선점·취소·명시적 복구 계약 검증."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from core.stage_work_queue import (
    StageWorkConflict,
    StageWorkError,
    StageWorkQueue,
    StageWorkSpec,
    StageWorkStatus,
)


def _queue(tmp_path: Path) -> StageWorkQueue:
    """각 테스트의 임시 DB에만 큐를 초기화한다."""
    queue = StageWorkQueue(tmp_path / "jobs.db")
    queue.initialize()
    return queue


def _spec(
    meeting_id: str = "meeting-1",
    stage: str = "transcribe",
    dependencies: tuple[str, ...] = (),
    generation: str = "generation-1",
    model: str = "whisper",
) -> StageWorkSpec:
    """모델·파일 실행 없이 저장 가능한 작은 단계 의도를 만든다."""
    return StageWorkSpec(
        meeting_id=meeting_id,
        generation=generation,
        stage=stage,
        dependencies=dependencies,
        input_fingerprint=f"fingerprint-{meeting_id}-{generation}",
        model_snapshot={"model": model, "options": {"language": "ko"}},
    )


def test_finite_cohort_is_atomic_idempotent_and_persistent(tmp_path: Path) -> None:
    """반복 접수는 원래 ID를 반환하고 restart 뒤에도 상태·목록을 보존한다."""
    queue = _queue(tmp_path)
    specs = [_spec(), _spec(stage="diarize", dependencies=("transcribe",), model="pyannote")]
    first = queue.enqueue_cohort("batch-1", specs)
    claimed = queue.claim_ready("batch-1", "process-1", "transcribe")[0]
    queue.complete(claimed.id, "process-1", claimed.claim_token)
    restarted = StageWorkQueue(tmp_path / "jobs.db")
    restarted.initialize()
    repeated = restarted.enqueue_cohort("batch-1", specs)
    assert [work.id for work in repeated] == [work.id for work in first]
    assert repeated[0].status == StageWorkStatus.COMPLETED
    assert repeated[0].attempt_count == 1
    assert repeated[1].dependencies == ("transcribe",)
    assert repeated[0].model_snapshot == specs[0].model_snapshot
    with pytest.raises(StageWorkConflict):
        queue.enqueue_cohort("batch-1", [*specs, _spec("meeting-2")])
    with pytest.raises(StageWorkConflict):
        queue.enqueue_cohort("batch-1", [replace(specs[0], input_fingerprint="changed"), specs[1]])
    assert len(queue.list_cohort("batch-1")) == 2


def test_duplicate_stage_rolls_back_entire_new_cohort(tmp_path: Path) -> None:
    """두 번째 항목 충돌도 앞서 삽입한 작업과 묶음 자체를 모두 되돌린다."""
    queue = _queue(tmp_path)
    queue.enqueue_cohort("existing", [_spec()])
    with pytest.raises(StageWorkConflict):
        queue.enqueue_cohort("new", [_spec("meeting-2"), _spec()])
    assert queue.list_cohort("new") == []
    # 실패한 접수 ID는 잔여 manifest 없이 올바른 내용으로 다시 사용할 수 있다.
    accepted = queue.enqueue_cohort("new", [_spec("meeting-2")])
    assert len(accepted) == 1


def test_dependency_readiness_is_scoped_to_meeting_and_generation(tmp_path: Path) -> None:
    """다른 회의·세대의 성공은 현재 작업의 선행 단계를 대신하지 못한다."""
    queue = _queue(tmp_path)
    specs = [
        _spec(),
        _spec(stage="diarize", dependencies=("transcribe",)),
        _spec("meeting-2"),
        _spec("meeting-2", "diarize", ("transcribe",)),
        _spec(generation="generation-2"),
        _spec(stage="diarize", dependencies=("transcribe",), generation="generation-2"),
    ]
    queue.enqueue_cohort("batch", specs)
    assert queue.claim_ready("batch", "process", "diarize", limit=5) == []
    transcribe = queue.claim_ready("batch", "process", "transcribe")[0]
    queue.complete(transcribe.id, "process", transcribe.claim_token)
    ready = queue.claim_ready("batch", "process", "diarize", limit=5)
    assert [(work.meeting_id, work.generation) for work in ready] == [
        ("meeting-1", "generation-1")
    ]


def test_dependency_can_reference_prior_cohort_without_duplicating_stage(tmp_path: Path) -> None:
    """앞선 묶음의 영속 단계도 완료된 뒤에만 새 묶음을 준비 상태로 만든다."""
    queue = _queue(tmp_path)
    queue.enqueue_cohort("first", [_spec()])
    queue.enqueue_cohort("second", [_spec(stage="diarize", dependencies=("transcribe",))])
    assert queue.claim_ready("second", "process", "diarize") == []
    work = queue.claim_ready("first", "process", "transcribe")[0]
    queue.complete(work.id, "process", work.claim_token)
    assert len(queue.claim_ready("second", "process", "diarize")) == 1


def test_claim_filters_stage_model_and_finite_cohort(tmp_path: Path) -> None:
    """stage/model/묶음 필터 및 limit이 선점 범위를 고정한다."""
    queue = _queue(tmp_path)
    queue.enqueue_cohort(
        "batch",
        [
            _spec("one"),
            _spec("two", model="other-model"),
            _spec("three"),
            _spec("four", stage="convert"),
        ],
    )
    queue.enqueue_cohort("next-batch", [_spec("five")])
    ready = queue.claim_ready(
        "batch", "process", "transcribe", limit=1, model_snapshot=_spec().model_snapshot
    )
    assert [work.meeting_id for work in ready] == ["one"]
    second = queue.claim_ready(
        "batch", "process", "transcribe", limit=10, model_snapshot=_spec().model_snapshot
    )
    assert [work.meeting_id for work in second] == ["three"]
    assert queue.list_cohort("next-batch")[0].status == StageWorkStatus.QUEUED


@pytest.mark.parametrize(
    "specs",
    [
        [],
        [_spec(), _spec()],
        [_spec(dependencies=("missing",))],
        [_spec(dependencies=("transcribe",))],
        [_spec(dependencies=("diarize",)), _spec(stage="diarize", dependencies=("transcribe",))],
        [_spec(dependencies=("convert", "convert")), _spec(stage="convert")],
        [replace(_spec(), input_fingerprint="")],
        [replace(_spec(), model_snapshot={"invalid": float("nan")})],
        [replace(_spec(), model_snapshot={"invalid": object()})],
    ],
)
def test_invalid_manifest_is_rejected_without_partial_work(
    tmp_path: Path, specs: list[StageWorkSpec]
) -> None:
    """잘못된 graph와 snapshot은 일부 접수를 남기지 않는다."""
    queue = _queue(tmp_path)
    with pytest.raises(StageWorkError):
        queue.enqueue_cohort("invalid", specs)
    assert queue.list_cohort("invalid") == []


def test_concurrent_consumers_do_not_claim_same_stage(tmp_path: Path) -> None:
    """서로 다른 연결의 동시 선점은 하나의 단계에 소유자 한 명만 배정한다."""
    queue = _queue(tmp_path)
    queue.enqueue_cohort("batch", [_spec(f"meeting-{index}") for index in range(8)])
    barrier = threading.Barrier(2)

    def claim(session: str) -> list[int]:
        """독립 연결 두 개가 동시에 준비된 묶음을 선점한다."""
        independent = StageWorkQueue(tmp_path / "jobs.db")
        barrier.wait(timeout=5)
        return [
            work.id for work in independent.claim_ready("batch", session, "transcribe", limit=5)
        ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ["process-one", "process-two"]))
    assert set(results[0]).isdisjoint(results[1])
    assert len(results[0]) + len(results[1]) == 8
    assert all(work.attempt_count == 1 for work in queue.list_cohort("batch"))


def test_completion_requires_exact_claim_and_is_idempotent(tmp_path: Path) -> None:
    """다른 소유자와 오래된 token은 완료할 수 없고 같은 완료는 중복 기록하지 않는다."""
    queue = _queue(tmp_path)
    queue.enqueue_cohort("batch", [_spec()])
    work = queue.claim_ready("batch", "owner", "transcribe")[0]
    with pytest.raises(StageWorkConflict):
        queue.complete(work.id, "other", work.claim_token)
    with pytest.raises(StageWorkConflict):
        queue.complete(work.id, "owner", "stale-token")
    complete = queue.complete(work.id, "owner", work.claim_token)
    assert queue.complete(work.id, "owner", work.claim_token) == complete
    with pytest.raises(StageWorkConflict):
        queue.fail(work.id, "owner", work.claim_token, "MODEL_ERROR")
    assert complete.attempt_count == 1


def test_failure_blocks_only_its_own_dependents_and_is_not_recovered(tmp_path: Path) -> None:
    """단계 실패는 후속 작업을 막고, 다른 회의는 정상 진행한다."""
    queue = _queue(tmp_path)
    queue.enqueue_cohort(
        "batch",
        [
            _spec(),
            _spec(stage="diarize", dependencies=("transcribe",)),
            _spec("other"),
            _spec("other", "diarize", ("transcribe",)),
        ],
    )
    failed, succeeded = queue.claim_ready("batch", "old", "transcribe", limit=2)
    queue.fail(failed.id, "old", failed.claim_token, "MODEL_TIMEOUT")
    queue.complete(succeeded.id, "old", succeeded.claim_token)
    assert (
        queue.recover_session("old", current_session_id="new", native_ownership_released=True)
        == []
    )
    assert [work.meeting_id for work in queue.claim_ready("batch", "new", "diarize")] == ["other"]
    assert queue.get(failed.id).error_code == "MODEL_TIMEOUT"


def test_old_timestamps_never_steal_claims_and_explicit_recovery_preserves_attempts(
    tmp_path: Path,
) -> None:
    """오래된 시간만으로 탈취하지 않고 실제 종료 확인 후 지정한 이전 세션만 복구한다."""
    queue = _queue(tmp_path)
    queue.enqueue_cohort("batch", [_spec(), _spec("other")])
    old = queue.claim_ready("batch", "old", "transcribe")[0]
    active = queue.claim_ready("batch", "active", "transcribe")[0]
    with sqlite3.connect(str(tmp_path / "jobs.db")) as conn:
        conn.execute("UPDATE stage_work_items SET updated_at='2000-01-01' WHERE id=?", (old.id,))
    assert queue.claim_ready("batch", "new", "transcribe", limit=10) == []
    with pytest.raises(StageWorkConflict):
        queue.recover_session("old", current_session_id="new", native_ownership_released=False)
    with pytest.raises(StageWorkConflict):
        queue.recover_session("old", current_session_id="old", native_ownership_released=True)
    recovered = queue.recover_session(
        "old", current_session_id="new", native_ownership_released=True
    )
    assert [work.id for work in recovered] == [old.id]
    assert recovered[0].attempt_count == 1
    assert recovered[0].status == StageWorkStatus.QUEUED
    assert recovered[0].owner_session_id == recovered[0].claim_token == ""
    assert queue.get(active.id).status == StageWorkStatus.RUNNING
    retried = queue.claim_ready("batch", "new", "transcribe")[0]
    assert retried.attempt_count == 2
    assert retried.claim_token != old.claim_token
    with pytest.raises(StageWorkConflict):
        queue.complete(old.id, "old", old.claim_token)


def test_cancel_intent_survives_restart_and_cannot_be_completed(tmp_path: Path) -> None:
    """실행·대기 취소 의도는 완료 기록과 재시작 복구로 지워지지 않는다."""
    queue = _queue(tmp_path)
    queue.enqueue_cohort(
        "batch",
        [
            _spec(),
            _spec(stage="diarize", dependencies=("transcribe",)),
            _spec(generation="generation-2"),
        ],
    )
    running = queue.claim_ready("batch", "old", "transcribe")[0]
    cancelled = queue.request_cancel("meeting-1", "generation-1")
    assert len(cancelled) == 2
    assert {work.status for work in cancelled} == {StageWorkStatus.CANCEL_REQUESTED}
    with pytest.raises(StageWorkConflict):
        queue.complete(running.id, "old", running.claim_token)
    with pytest.raises(StageWorkConflict):
        queue.fail(running.id, "old", running.claim_token, "MODEL_ERROR")
    restarted = StageWorkQueue(tmp_path / "jobs.db")
    recovered = restarted.recover_session(
        "old", current_session_id="new", native_ownership_released=True
    )
    assert len(recovered) == 1
    assert recovered[0].status == StageWorkStatus.CANCEL_REQUESTED
    assert recovered[0].attempt_count == 1
    assert recovered[0].owner_session_id == ""
    assert [
        work.generation for work in restarted.claim_ready("batch", "new", "transcribe", limit=10)
    ] == ["generation-2"]
    assert restarted.claim_ready("batch", "new", "diarize") == []


def test_cancel_acknowledgement_releases_only_current_owner(tmp_path: Path) -> None:
    """native 종료 확인 뒤 현재 소유자만 취소 작업 소유권을 반납한다."""
    queue = _queue(tmp_path)
    queue.enqueue_cohort("batch", [_spec()])
    running = queue.claim_ready("batch", "owner", "transcribe")[0]
    queue.request_cancel("meeting-1", "generation-1")
    with pytest.raises(StageWorkConflict):
        queue.acknowledge_cancel(running.id, "other", running.claim_token)
    cancelled = queue.acknowledge_cancel(running.id, "owner", running.claim_token)
    assert cancelled.status == StageWorkStatus.CANCEL_REQUESTED
    assert cancelled.owner_session_id == cancelled.claim_token == ""
    assert cancelled.attempt_count == 1
    assert queue.claim_ready("batch", "next", "transcribe") == []


def test_error_codes_exclude_raw_error_text(tmp_path: Path) -> None:
    """저장용 실패 분류 코드에 경로와 원문 메시지를 넣지 못한다."""
    queue = _queue(tmp_path)
    queue.enqueue_cohort("batch", [_spec()])
    running = queue.claim_ready("batch", "owner", "transcribe")[0]
    with pytest.raises(StageWorkError):
        queue.fail(running.id, "owner", running.claim_token, "failed reading /private/audio.wav")
    assert queue.get(running.id).status == StageWorkStatus.RUNNING


def test_initialize_preserves_unrelated_tables(tmp_path: Path) -> None:
    """기존 DB의 jobs와 사용자 데이터는 초기화 대상이 아니다."""
    db = tmp_path / "jobs.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO jobs VALUES (1, 'preserved')")
    queue = StageWorkQueue(db)
    queue.initialize()
    queue.initialize()
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT * FROM jobs").fetchall() == [(1, "preserved")]
