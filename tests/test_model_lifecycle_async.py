"""동기 모델 lifecycle의 서버 응답성과 취소 후 슬롯 보존을 검증한다."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from core.model_manager import ModelLoadManager, NativeCleanupPendingError

pytestmark = pytest.mark.asyncio


@pytest.fixture
def manager() -> Iterator[ModelLoadManager]:
    """실제 설정·GPU를 사용하지 않는 매니저를 만든다."""
    config = SimpleNamespace(pipeline=SimpleNamespace(peak_ram_limit_gb=9.5))
    with patch("core.model_manager.get_config", return_value=config):
        yield ModelLoadManager(gpu_cache_cleanup_enabled=False)


async def _wait_started(event: threading.Event) -> None:
    """실제 동기 worker의 시작을 제한된 시간 안에 기다린다."""
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 2), timeout=3)


async def _wait_clean(manager: ModelLoadManager) -> None:
    """실제 worker와 finalizer의 정리 완료를 기다린다."""
    async with asyncio.timeout(3):
        while manager.get_status()["native_cleanup_pending"] or manager.get_status().get(
            "residency_cleanup_pending", False
        ):
            await asyncio.sleep(0.01)


async def _assert_admission_blocked(manager: ModelLoadManager) -> None:
    """정리 완료 전에 다음 모델 로더가 호출되지 않는지 확인한다."""
    with pytest.raises(NativeCleanupPendingError):
        async with manager.acquire("replacement", lambda: pytest.fail("조기 모델 로드")):
            pytest.fail("조기 모델 사용")


async def test_synchronous_load_keeps_event_loop_responsive(manager: ModelLoadManager) -> None:
    """느린 동기 로드 중에도 이벤트 루프가 실행되고 모델 사용은 기다린다."""
    started, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    loader_threads: list[int] = []
    model = object()

    def load() -> object:
        loader_threads.append(threading.get_ident())
        started.set()
        assert release.wait(2)
        return model

    task = asyncio.create_task(manager.load_model("model", load))
    try:
        await _wait_started(started)
        await asyncio.sleep(0)
        assert not task.done()
        assert loader_threads != [loop_thread]
    finally:
        release.set()
    assert await task is model
    await manager.unload_model()


@pytest.mark.parametrize("use_context", [False, True])
async def test_cancelled_sync_load_cleans_orphan_before_releasing_admission(
    manager: ModelLoadManager, use_context: bool
) -> None:
    """취소된 로드가 반환한 모델은 사용하지 않고 정리한 뒤 슬롯을 반납한다."""
    started, release = threading.Event(), threading.Event()
    events: list[str] = []

    class Model:
        def cleanup(self) -> None:
            """로드 완료 뒤에만 정리한다."""
            assert events == ["loaded"]
            events.append("cleaned")

    def load() -> Model:
        started.set()
        assert release.wait(2)
        events.append("loaded")
        return Model()

    async def run() -> None:
        if use_context:
            async with manager.acquire("model", load):
                pytest.fail("취소된 로드의 모델을 사용하면 안 됩니다")
        else:
            await manager.load_model("model", load)

    task = asyncio.create_task(run())
    try:
        await _wait_started(started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.3)
        assert events == []
        assert manager.get_status()["native_cleanup_operation"] == "load"
        await _assert_admission_blocked(manager)
        await manager.unload_model()
        assert manager.get_status()["native_cleanup_pending"]
    finally:
        release.set()
        await _wait_clean(manager)
    assert events == ["loaded", "cleaned"]
    assert manager.current_model is None
    async with manager.acquire("replacement", object):
        pass


async def test_cancelled_sync_load_failure_releases_admission(manager: ModelLoadManager) -> None:
    """취소 후 로더가 실패해도 예외를 회수하고 다음 요청이 실행된다."""
    started, release = threading.Event(), threading.Event()

    def load() -> None:
        started.set()
        assert release.wait(2)
        raise ValueError("모델 로드 실패")

    task = asyncio.create_task(manager.load_model("model", load))
    try:
        await _wait_started(started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _assert_admission_blocked(manager)
    finally:
        release.set()
        await _wait_clean(manager)
    assert not manager.is_model_loaded
    async with manager.acquire("replacement", object):
        pass


async def test_lifecycle_submission_survives_unscheduled_thread_task_cancellation(
    manager: ModelLoadManager,
) -> None:
    """to_thread Task가 실행 전 취소돼도 미실행 worker lease가 영구히 남지 않는다."""
    create_task = asyncio.create_task
    model = object()
    cancelled = False

    def cancel_thread_task(coroutine: Any, **kwargs: Any) -> asyncio.Task[Any]:
        task = create_task(coroutine, **kwargs)
        if getattr(getattr(coroutine, "cr_code", None), "co_name", None) == "to_thread":
            task.cancel()
        return task

    with patch("core.model_manager.asyncio.create_task", side_effect=cancel_thread_task):
        try:
            loaded = await manager.load_model("model", lambda: model)
        except asyncio.CancelledError:
            cancelled = True
            # 과거 구현의 무한 finalizer를 테스트 teardown 전에 해제한다.
            pending = manager._deferred_context_cleanup
            if pending is not None:
                for worker in pending.workers:
                    worker.finished.set()
    if cancelled:
        await _wait_clean(manager)
        pytest.fail("실행되지 않은 to_thread Task의 취소가 lifecycle을 잃었습니다")
    assert loaded is model
    await manager.unload_model()


@pytest.mark.parametrize("operation", ["unload", "context_exit", "replace"])
async def test_cleanup_is_responsive_and_cancellation_keeps_admission(
    manager: ModelLoadManager, operation: str
) -> None:
    """모든 정리 진입점에서 취소 응답 후에도 실제 cleanup 완료까지 기다린다."""
    started, release = threading.Event(), threading.Event()
    events: list[str] = []

    class Model:
        def cleanup(self) -> None:
            """정리를 외부 신호까지 멈춰 이벤트 루프의 응답성을 검증한다."""
            started.set()
            assert release.wait(2)
            events.append("cleaned")

    async def run() -> None:
        if operation == "context_exit":
            async with manager.acquire("model", Model):
                pass
        else:
            await manager.load_model("model", Model)
            if operation == "replace":
                await manager.load_model("replacement", lambda: events.append("replacement"))
            else:
                await manager.unload_model()

    task = asyncio.create_task(run())
    try:
        await _wait_started(started)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.3)
        assert manager.get_status()["native_cleanup_operation"] == "unload"
        assert manager.current_model_name == "model"
        assert events == []
        await _assert_admission_blocked(manager)
    finally:
        release.set()
        await _wait_clean(manager)
    assert events == ["cleaned"]
    assert not manager.is_model_loaded


async def test_cancelled_load_finalizer_can_be_cancelled_without_losing_lease(
    manager: ModelLoadManager,
) -> None:
    """로드 finalizer 자체의 취소도 고아 모델을 남기거나 슬롯을 먼저 풀지 않는다."""
    started, release = threading.Event(), threading.Event()
    cleaned = threading.Event()

    class Model:
        def cleanup(self) -> None:
            """최종 정리 여부를 기록한다."""
            cleaned.set()

    def load() -> Model:
        started.set()
        assert release.wait(2)
        return Model()

    task = asyncio.create_task(manager.load_model("model", load))
    try:
        await _wait_started(started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pending = manager._deferred_context_cleanup
        assert pending is not None and pending.task is not None
        pending.task.cancel()
        pending.task.cancel()
        await asyncio.sleep(0)
        await _assert_admission_blocked(manager)
    finally:
        release.set()
        await _wait_clean(manager)
    assert cleaned.is_set()
    assert not manager.is_model_loaded


async def test_async_loader_and_coroutine_factory_run_on_event_loop(
    manager: ModelLoadManager,
) -> None:
    """비동기 로더와 coroutine을 반환하는 동기 factory 호환성을 유지한다."""
    loop_thread = threading.get_ident()

    async def load() -> object:
        assert threading.get_ident() == loop_thread
        await asyncio.sleep(0)
        return object()

    for loader in (load, lambda: load()):
        async with manager.acquire("async", loader) as model:
            assert model is not None


async def test_lifecycle_status_counts_successful_loads_and_reuse(
    manager: ModelLoadManager,
) -> None:
    """측정값은 성공한 로드·재사용·정리 사실을 따로 기록한다."""
    initial = manager.get_status()
    assert initial["model_load_count"] == 0
    assert initial["last_load_seconds"] is None
    assert initial["last_unload_seconds"] is None
    first = await manager.load_model("model", object)
    assert await manager.load_model("model", object) is first
    await manager.unload_model()
    status: dict[str, Any] = manager.get_status()
    assert status["model_load_count"] == 1
    assert status["model_reuse_count"] == 1
    assert status["last_load_seconds"] >= 0
    assert status["last_unload_seconds"] >= 0
    assert status["last_context_wait_seconds"] >= 0


async def test_residency_reuses_only_within_scope_without_holding_admission(
    manager: ModelLoadManager,
) -> None:
    """유한 scope에서는 재사용하고 scope 사이에는 모델 슬롯을 점유하지 않는다."""
    cleaned: list[object] = []

    class Model:
        def cleanup(self) -> None:
            """정리된 실제 인스턴스를 기록한다."""
            cleaned.append(self)

    async with manager.residency_scope("model", reuse_key="config-a"):
        async with manager.acquire("model", Model) as first:
            pass
        assert not manager._context_lock.locked()
        assert manager.current_model is first
        async with manager.acquire("model", Model) as second:
            assert second is first
        assert cleaned == []
    assert cleaned == [first]
    assert not manager.is_model_loaded
    assert manager.get_status()["model_load_count"] == 1


async def test_same_name_different_reuse_key_loads_new_instance(manager: ModelLoadManager) -> None:
    """동일 이름이어도 설정 식별자가 다르면 이전 모델을 재사용하지 않는다."""
    first = await manager.load_model("model", object, reuse_key="config-a")
    second = await manager.load_model("model", object, reuse_key="config-b")
    assert second is not first
    assert manager.get_status()["model_load_count"] == 2
    await manager.unload_model()


async def test_residency_exit_keeps_other_tasks_replacement_instance(
    manager: ModelLoadManager,
) -> None:
    """다른 task가 동일 이름의 모델을 교체하면 scope 종료가 그 모델을 내리지 않는다."""
    async with manager.residency_scope("model", reuse_key="scope-config"):
        async with manager.acquire("model", object) as resident:
            pass
        replacement = await asyncio.create_task(
            manager.load_model("model", object, reuse_key="other-config")
        )
        assert replacement is not resident
    assert manager.current_model is replacement
    await manager.unload_model()


async def test_residency_cancellation_between_acquires_unloads_model(
    manager: ModelLoadManager,
) -> None:
    """모델을 유지하던 scope가 취소되면 보유한 모델을 정리한다."""
    ready = asyncio.Event()

    async def run() -> None:
        async with manager.residency_scope("model", reuse_key="config"):
            async with manager.acquire("model", object):
                pass
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await ready.wait()
    assert manager.is_model_loaded
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not manager.is_model_loaded


async def test_scope_exit_requests_unload_for_pending_native_worker(
    manager: ModelLoadManager,
) -> None:
    """컨텍스트가 취소를 처리했더라도 scope 종료 후 모델을 영구 보유하지 않는다."""
    from core.model_manager import await_native_inference

    started, release = threading.Event(), threading.Event()

    def inference() -> None:
        started.set()
        assert release.wait(2)

    try:
        async with manager.residency_scope("model", reuse_key="config"):
            async with manager.acquire("model", object):
                work = asyncio.create_task(await_native_inference(inference))
                await _wait_started(started)
                work.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await work
            pending = manager._deferred_context_cleanup
            assert pending is not None and not pending.unload_after
        assert pending.unload_after
        await _assert_admission_blocked(manager)
    finally:
        release.set()
        await _wait_clean(manager)
    assert not manager.is_model_loaded


async def test_child_task_does_not_retain_model_after_inherited_scope_ends(
    manager: ModelLoadManager,
) -> None:
    """ContextVar를 상속한 지연 child task도 종료된 scope의 보유 정책을 적용하지 않는다."""
    release = asyncio.Event()

    async def child() -> None:
        await release.wait()
        async with manager.acquire("model", object):
            pass

    async with manager.residency_scope("model", reuse_key="config"):
        task = asyncio.create_task(child())
    release.set()
    await task
    assert not manager.is_model_loaded


async def test_child_loading_when_scope_ends_cleans_model_on_context_exit(
    manager: ModelLoadManager,
) -> None:
    """scope 종료 당시 아직 로드 중인 child도 나중에 모델을 남기지 않는다."""
    started, release = threading.Event(), threading.Event()

    def load() -> object:
        started.set()
        assert release.wait(2)
        return object()

    async def child() -> None:
        async with manager.acquire("model", load):
            pass

    try:
        async with manager.residency_scope("model", reuse_key="config"):
            task = asyncio.create_task(child())
            await _wait_started(started)
    finally:
        release.set()
    await task
    assert not manager.is_model_loaded


async def test_cancelling_scope_exit_preserves_cleanup_queued_behind_active_acquire(
    manager: ModelLoadManager,
) -> None:
    """scope 종료 대기를 취소해도 사용 중인 같은 모델의 정리 요청은 남는다."""
    held, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    holders: list[asyncio.Task[None]] = []

    async def hold_model() -> None:
        async with manager.acquire("model", object, keep_loaded=True, reuse_key="config"):
            held.set()
            await release.wait()

    async def scoped() -> None:
        async with manager.residency_scope("model", reuse_key="config"):
            async with manager.acquire("model", object):
                pass
            holders.append(asyncio.create_task(hold_model()))
            await held.wait()
            closing.set()

    task = asyncio.create_task(scoped())
    try:
        await asyncio.wait_for(closing.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.is_model_loaded
        assert manager.get_status()["residency_cleanup_pending"]
    finally:
        release.set()
        await asyncio.gather(*holders)
        await _wait_clean(manager)
    assert not manager.is_model_loaded
