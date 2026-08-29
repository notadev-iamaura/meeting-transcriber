"""
모델 로드 매니저 (Model Load Manager)

목적: 한 번에 하나의 대형 모델만 메모리에 적재하도록 제어하는 뮤텍스 기반 매니저.
주요 기능:
    - asyncio.Lock 기반 동시 로드 및 추론 컨텍스트 직렬화
    - 이전 모델 자동 언로드 (gc.collect + Metal 캐시 정리)
    - async with 컨텍스트 매니저 패턴 지원
    - psutil 기반 메모리 사용량 모니터링
의존성: asyncio, gc, psutil, config 모듈
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, TypeVar, Union, cast

import psutil

from config import get_config
from core.preflight import run_preflight

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ``await_native_inference()`` 는 현재 acquire 컨텍스트에 worker를 등록해야 한다.
# ContextVar는 asyncio.wait_for()가 만드는 자식 task에도 복사되므로, timeout 경로에서도
# 원래 ModelLoadManager의 lease를 잃지 않는다.
_active_model_context: ContextVar[Any] = ContextVar(
    "active_model_context",
    default=None,
)

# 모델 로더 타입: 동기 또는 비동기 함수
ModelLoader = Union[Callable[[], T], Callable[[], Coroutine[Any, Any, T]]]
MlxCoreImporter = Callable[[], Any]
PreflightRunner = Callable[[], Any]

_DISABLE_GPU_CACHE_CLEANUP_ENV = "MT_DISABLE_GPU_CACHE_CLEANUP"
_NATIVE_WORKER_POLL_SECONDS = 0.05


class NativeCleanupPendingError(RuntimeError):
    """종료되지 않은 native worker 때문에 새 모델 사용을 시작할 수 없는 오류."""


async def await_native_inference(
    func: Callable[..., T],
    /,
    *args: Any,
    **kwargs: Any,
) -> T:
    """현재 모델 lease 아래에서 동기 native inference를 실행한다.

    취소/timeout은 즉시 호출자에게 전파한다. 다만 ``asyncio.to_thread`` worker는
    취소되지 않으므로, acquire 컨텍스트가 worker 종료 전 cleanup/unload 하지 않도록
    현재 ``_ModelContext``에 worker lease를 등록한다. 컨텍스트 종료 시 worker가
    남아 있으면 ModelLoadManager가 background finalizer로 model-admission lock과
    모델 참조를 유지한다.

    ``acquire()`` 밖 호출은 모델 lifecycle을 보유하지 않으므로 기존 ``to_thread``
    동작으로 폴백한다. 프로덕션 모델 호출은 모두 acquire 내부에서 해야 한다.
    """
    context = _active_model_context.get()
    if context is None:
        return await asyncio.to_thread(func, *args, **kwargs)
    return cast(T, await context._await_native_inference(func, *args, **kwargs))


def _env_flag_enabled(name: str) -> bool:
    """환경변수 플래그가 활성화 값인지 확인한다."""
    value = os.environ.get(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _import_mlx_core() -> Any:
    """이미 로드된 MLX core 모듈을 반환한다.

    cleanup 경계에서 `import mlx.core` 를 새로 실행하면 환경에 따라 C++ abort 로
    프로세스가 종료될 수 있다. 모델 로드 과정에서 이미 들어온 모듈만 사용한다.
    """
    mx = sys.modules.get("mlx.core")
    if mx is None:
        raise ImportError("mlx.core is not loaded")
    return mx


@dataclass
class ModelInfo:
    """현재 로드된 모델의 메타데이터.

    Attributes:
        name: 모델 식별 이름 (예: "whisper", "pyannote", "exaone", "e5")
        instance: 로드된 모델 객체 참조
        loaded_at: 로드 시각 (Unix timestamp)
        memory_before_mb: 로드 전 프로세스 메모리 (MB)
        memory_after_mb: 로드 후 프로세스 메모리 (MB)
    """

    name: str
    instance: Any
    loaded_at: float = field(default_factory=time.time)
    memory_before_mb: float = 0.0
    memory_after_mb: float = 0.0

    @property
    def memory_delta_mb(self) -> float:
        """모델 로드로 인한 메모리 증가량 (MB)

        Returns:
            메모리 증가량 (MB)
        """
        return self.memory_after_mb - self.memory_before_mb


@dataclass
class _NativeInferenceWorker:
    """실제 native thread의 종료를 추적하는 worker lease.

    ``asyncio.to_thread()``의 Task가 취소되어도 native thread는 계속 실행될 수 있다.
    따라서 Task 완료 여부만으로 모델을 언로드하면 use-after-unload가 발생할 수 있다.
    ``finished``는 함수 본문이 실제로 반환하거나 예외로 끝난 뒤에만 설정된다.
    """

    task: asyncio.Task[Any]
    finished: threading.Event


@dataclass
class _DeferredContextCleanup:
    """timeout/cancel 뒤에도 유지해야 하는 모델 컨텍스트 lease."""

    model_name: str
    workers: tuple[_NativeInferenceWorker, ...]
    unload_after: bool
    started_at: float = field(default_factory=time.time)
    task: asyncio.Task[None] | None = None


class ModelLoadManager:
    """대형 모델의 메모리 라이프사이클을 관리하는 뮤텍스 기반 매니저.

    한 번에 하나의 대형 모델만 메모리에 적재되도록 asyncio.Lock으로 제어한다.
    새 모델 로드 요청 시 기존 모델을 먼저 언로드하고, gc.collect() 및
    Apple Silicon Metal 캐시 정리를 수행한 뒤 새 모델을 로드한다.

    사용 예시:
        manager = ModelLoadManager()

        # 컨텍스트 매니저 패턴 (권장)
        async with manager.acquire("whisper", load_whisper_fn) as model:
            result = model.transcribe(audio)

        # 수동 로드/언로드
        model = await manager.load_model("whisper", load_whisper_fn)
        # ... 사용 ...
        await manager.unload_model()
    """

    def __init__(
        self,
        *,
        gpu_cache_cleanup_enabled: bool | None = None,
        mlx_core_importer: MlxCoreImporter | None = None,
        preflight_runner: PreflightRunner | None = None,
    ) -> None:
        """ModelLoadManager 초기화.

        Args:
            gpu_cache_cleanup_enabled: Metal GPU 캐시 정리 활성화 여부.
                None이면 기본 활성화이며, MT_DISABLE_GPU_CACHE_CLEANUP 환경변수로
                테스트 환경에서만 비활성화할 수 있다.
            mlx_core_importer: mlx.core 지연 import 함수. 테스트에서 mock 주입용.
            preflight_runner: MLX 사용 가능성 검사 함수. 테스트에서 mock 주입용.
        """
        self._lock = asyncio.Lock()
        self._context_lock = asyncio.Lock()
        self._current: ModelInfo | None = None
        self._deferred_context_cleanup: _DeferredContextCleanup | None = None
        self._config = get_config()
        self._gpu_cache_cleanup_enabled = (
            not _env_flag_enabled(_DISABLE_GPU_CACHE_CLEANUP_ENV)
            if gpu_cache_cleanup_enabled is None
            else gpu_cache_cleanup_enabled
        )
        self._mlx_core_importer = mlx_core_importer or _import_mlx_core
        self._preflight_runner = preflight_runner or run_preflight
        logger.info("ModelLoadManager 초기화 완료")

    @property
    def current_model_name(self) -> str | None:
        """현재 로드된 모델의 이름. 없으면 None.

        Returns:
            모델명 또는 None
        """
        return self._current.name if self._current else None

    @property
    def current_model(self) -> Any | None:
        """현재 로드된 모델 인스턴스. 없으면 None.

        Returns:
            모델 인스턴스 또는 None
        """
        return self._current.instance if self._current else None

    @property
    def is_model_loaded(self) -> bool:
        """모델이 로드되어 있는지 여부.

        Returns:
            모델 로드 여부
        """
        return self._current is not None

    def _get_memory_usage_mb(self) -> float:
        """현재 프로세스의 RSS 메모리 사용량을 MB 단위로 반환한다.

        Returns:
            메모리 사용량 (MB)
        """
        process: psutil.Process = psutil.Process()
        return float(process.memory_info().rss / (1024 * 1024))

    def _get_memory_usage_gb(self) -> float:
        """현재 프로세스의 RSS 메모리 사용량을 GB 단위로 반환한다.

        Returns:
            메모리 사용량 (GB)
        """
        return self._get_memory_usage_mb() / 1024

    def _clear_gpu_cache(self) -> None:
        """Apple Silicon Metal GPU 캐시를 정리한다.

        mlx 라이브러리가 설치된 경우에만 동작하며,
        설치되지 않은 환경에서는 조용히 건너뛴다.

        사전 검증(preflight)에서 Metal 불가로 판정된 경우
        import 자체를 시도하지 않아 SIGABRT를 방지한다.
        """
        if not self._gpu_cache_cleanup_enabled:
            logger.debug("Metal GPU 캐시 정리 비활성화 — 건너뜀")
            return

        # SIGABRT 방지: preflight에서 Metal 사용 불가 판정 시 스킵
        preflight = self._preflight_runner()
        if not preflight.can_use_mlx:
            logger.debug("MLX 사용 불가 — Metal 캐시 정리 건너뜀")
            return

        try:
            mx = self._mlx_core_importer()
            clear_cache = getattr(getattr(mx, "metal", None), "clear_cache", None)
            if not callable(clear_cache):
                logger.debug("mlx.core.metal.clear_cache 없음 — Metal 캐시 정리 건너뜀")
                return
            clear_cache()
            logger.debug("Metal GPU 캐시 정리 완료")
        except ImportError:
            logger.debug("mlx 미설치 — Metal 캐시 정리 건너뜀")
        except Exception as e:
            logger.warning(f"Metal 캐시 정리 중 오류 (무시): {e}")

    async def _unload_current(self) -> None:
        """현재 로드된 모델을 언로드하고 메모리를 해제한다.

        수행 순서:
            1. 모델 참조 제거
            2. gc.collect() 호출
            3. Metal GPU 캐시 정리 (가능한 경우)
            4. 메모리 변화 로깅
        """
        if self._current is None:
            return

        model_name = self._current.name
        mem_before_unload = self._get_memory_usage_mb()

        logger.info(f"모델 언로드 시작: {model_name}")

        # 백엔드별 정리 (MLX: 모델 해제 + Metal 캐시, Ollama: no-op)
        if self._current.instance is not None and hasattr(self._current.instance, "cleanup"):
            try:
                self._current.instance.cleanup()
            except Exception as cleanup_err:
                logger.warning(f"cleanup() 실행 중 오류 (무시): {cleanup_err}")

        # 모델 참조 제거
        self._current.instance = None
        self._current = None

        # 가비지 컬렉션 수행 (실패해도 언로드 자체는 완료된 것으로 처리)
        try:
            gc.collect()
        except Exception as gc_err:
            logger.warning(f"gc.collect() 실행 중 오류 (무시): {gc_err}")

        # Apple Silicon Metal 캐시 정리
        self._clear_gpu_cache()

        try:
            mem_after_unload = self._get_memory_usage_mb()
            freed_mb = mem_before_unload - mem_after_unload
            logger.info(
                f"모델 언로드 완료: {model_name} | "
                f"해제된 메모리: {freed_mb:.1f}MB | "
                f"현재 메모리: {mem_after_unload:.1f}MB"
            )
        except Exception as mem_err:
            # 메모리 측정 실패 시에도 언로드 자체는 정상 완료
            logger.info(f"모델 언로드 완료: {model_name} | 메모리 측정 실패: {mem_err}")

    def _check_memory_limit(self) -> None:
        """현재 메모리 사용량이 peak_ram_limit_gb를 초과하는지 확인한다.

        초과 시 경고 로그를 남긴다 (강제 중단하지는 않음).
        """
        current_gb = self._get_memory_usage_gb()
        limit_gb = self._config.pipeline.peak_ram_limit_gb

        if current_gb > limit_gb:
            logger.warning(f"메모리 사용량 경고: {current_gb:.2f}GB / 제한: {limit_gb:.1f}GB")

    async def load_model(
        self,
        name: str,
        loader: ModelLoader,
    ) -> Any:
        """모델을 로드한다. 이미 로드된 모델이 있으면 먼저 언로드한다.

        같은 이름의 모델이 이미 로드되어 있으면 기존 인스턴스를 반환한다.
        다른 모델이 로드되어 있으면 언로드 후 새 모델을 로드한다.
        동시 호출 시 asyncio.Lock으로 순차 처리한다.

        Args:
            name: 모델 식별 이름 (예: "whisper", "pyannote", "exaone", "e5")
            loader: 모델을 로드하는 함수 (동기 또는 비동기)

        Returns:
            로드된 모델 인스턴스

        Raises:
            Exception: 모델 로드 중 발생한 모든 예외 (Lock은 안전하게 해제됨)
        """
        await self._acquire_context_lock()
        try:
            return await self._load_model_with_state_lock(name, loader)
        finally:
            self._context_lock.release()

    async def _load_model_with_state_lock(
        self,
        name: str,
        loader: ModelLoader,
    ) -> Any:
        """context lock을 이미 보유한 상태에서 모델을 로드한다.

        직접 load_model() 호출은 active acquire() 컨텍스트가 끝날 때까지 기다린다.
        반면 _ModelContext.__aenter__는 이미 context lock을 보유하므로 이 helper를
        호출해 deadlock 없이 내부 상태만 `_lock`으로 보호한다.
        """
        async with self._lock:
            # 같은 모델이 이미 로드되어 있으면 재사용
            if self._current is not None and self._current.name == name:
                logger.info(f"모델 이미 로드됨, 재사용: {name}")
                return self._current.instance

            # 기존 모델 언로드
            await self._unload_current()

            # 새 모델 로드
            mem_before = self._get_memory_usage_mb()
            logger.info(f"모델 로드 시작: {name} | 현재 메모리: {mem_before:.1f}MB")

            try:
                # 로더가 비동기 함수인지 확인
                result = loader()
                if asyncio.iscoroutine(result):
                    instance = await result
                else:
                    instance = result
            except Exception:
                logger.exception(f"모델 로드 실패: {name}")
                raise

            mem_after = self._get_memory_usage_mb()

            self._current = ModelInfo(
                name=name,
                instance=instance,
                loaded_at=time.time(),
                memory_before_mb=mem_before,
                memory_after_mb=mem_after,
            )

            logger.info(
                f"모델 로드 완료: {name} | "
                f"메모리 증가: {self._current.memory_delta_mb:.1f}MB | "
                f"현재 메모리: {mem_after:.1f}MB"
            )

            # 메모리 제한 확인
            self._check_memory_limit()

            return instance

    async def unload_model(self) -> None:
        """현재 로드된 모델을 명시적으로 언로드한다.

        acquire() 컨텍스트가 모델을 사용 중이면 해당 컨텍스트가 끝날 때까지
        기다린 뒤 언로드한다. 직접 unload 호출이 active MLX inference 중인
        backend를 cleanup하지 못하게 하기 위함이다.
        로드된 모델이 없으면 아무 동작도 하지 않는다.
        """
        if self._deferred_context_cleanup is not None:
            logger.warning("deferred native cleanup이 모델 언로드를 이미 소유하고 있습니다.")
            return
        try:
            await self._acquire_context_lock()
        except NativeCleanupPendingError:
            # 대기 시작 뒤 active context가 deferred lease로 전환된 경쟁도
            # finalizer가 언로드를 소유하므로 cleanup 호출은 안전한 no-op이다.
            logger.warning(
                "대기 중 deferred native cleanup으로 전환되어 모델 언로드를 위임합니다."
            )
            return
        try:
            await self._unload_model_locked()
        finally:
            self._context_lock.release()

    async def unload_if_current(self, name: str) -> bool:
        """현재 로드된 모델 이름이 일치할 때만 언로드한다.

        acquire() 컨텍스트가 모델을 사용 중이면 해당 컨텍스트가 끝날 때까지
        기다린다. 다른 모델이 이미 로드되어 있으면 아무 동작도 하지 않는다.

        Args:
            name: 조건부 언로드 대상 모델 이름

        Returns:
            실제로 언로드했으면 True, 대상 모델이 아니면 False
        """
        if self._deferred_context_cleanup is not None:
            logger.warning(
                "deferred native cleanup이 조건부 모델 언로드를 이미 소유: model=%s",
                name,
            )
            return False
        try:
            await self._acquire_context_lock()
        except NativeCleanupPendingError:
            logger.warning(
                "대기 중 deferred native cleanup으로 전환되어 조건부 언로드를 위임: model=%s",
                name,
            )
            return False
        try:
            return await self._unload_if_current_locked(name)
        finally:
            self._context_lock.release()

    async def _unload_model_locked(self) -> None:
        """context lock을 이미 보유한 상태에서 현재 모델을 언로드한다.

        load/unload 내부 상태 변경은 기존 `_lock`으로 보호한다.
        이 helper는 `_ModelContext.__aexit__`처럼 `_context_lock`을 이미 가진
        경로에서 deadlock 없이 언로드하기 위해 사용한다.
        """
        async with self._lock:
            await self._unload_current()

    async def _unload_if_current_locked(self, name: str) -> bool:
        """context lock을 이미 보유한 상태에서 이름이 일치할 때만 언로드한다."""
        async with self._lock:
            if self._current is None or self._current.name != name:
                return False
            await self._unload_current()
            return True

    async def _unload_model_from_context(self) -> None:
        """acquire() 컨텍스트 종료 시 현재 모델을 언로드한다."""
        await self._unload_model_locked()

    def _defer_context_cleanup(
        self,
        *,
        model_name: str,
        workers: tuple[_NativeInferenceWorker, ...],
        unload_after: bool,
    ) -> None:
        """남은 native worker가 끝날 때까지 context lease를 background로 이전한다.

        이 시점에도 ``_context_lock``은 기존 acquire 컨텍스트가 보유한다. finalizer가
        worker의 실제 thread 종료와 필요한 unload를 확인한 뒤에만 lock을 반납한다.
        """
        if not workers:
            raise ValueError("deferred cleanup에는 최소 한 개의 native worker가 필요합니다.")
        if self._deferred_context_cleanup is not None:
            raise RuntimeError("이미 deferred native cleanup이 진행 중입니다.")

        cleanup = _DeferredContextCleanup(
            model_name=model_name,
            workers=workers,
            unload_after=unload_after,
        )
        self._deferred_context_cleanup = cleanup
        self._start_deferred_context_cleanup(cleanup)
        logger.warning(
            "native inference가 아직 종료되지 않아 모델 lease를 background로 이전: "
            f"model={model_name}, workers={len(workers)}, unload_after={unload_after}"
        )

    def _raise_if_native_cleanup_pending(self) -> None:
        """deferred native cleanup 중인 새 모델 요청을 즉시 거부한다.

        실제 worker가 끝나기 전에 admission lock이나 모델을 해제할 수는 없다.
        대신 후속 작업이 lock에서 무기한 대기하지 않도록 typed 오류로 fail-fast한다.
        worker 종료 후 finalizer가 상태를 비우면 다음 요청은 정상적으로 재개된다.
        """
        cleanup = self._deferred_context_cleanup
        if cleanup is None:
            return
        pending_workers = sum(not worker.finished.is_set() for worker in cleanup.workers)
        raise NativeCleanupPendingError(
            "이전 native inference 정리가 끝나지 않아 새 모델 작업을 시작할 수 없습니다: "
            f"model={cleanup.model_name}, pending_workers={pending_workers}"
        )

    async def _acquire_context_lock(self) -> None:
        """native cleanup 시작을 감지하며 model-admission lock을 획득한다.

        이미 lock을 기다리던 요청도 다른 컨텍스트의 timeout으로 deferred cleanup이
        시작되면 다음 짧은 polling 경계에서 typed 오류로 깨어난다. ``wait_for``가
        timeout될 때 asyncio.Lock waiter도 함께 취소되므로 고아 waiter를 남기지 않는다.
        """
        while True:
            self._raise_if_native_cleanup_pending()
            try:
                await asyncio.wait_for(
                    self._context_lock.acquire(),
                    timeout=_NATIVE_WORKER_POLL_SECONDS,
                )
            except TimeoutError:
                continue

            try:
                self._raise_if_native_cleanup_pending()
            except BaseException:
                self._context_lock.release()
                raise
            return

    def _start_deferred_context_cleanup(self, cleanup: _DeferredContextCleanup) -> None:
        """cancel-safe deferred finalizer를 시작하거나 재시작한다."""
        try:
            task = asyncio.create_task(
                self._finalize_deferred_context_cleanup(cleanup),
                name=f"model-native-cleanup:{cleanup.model_name}",
            )
        except RuntimeError:
            # 이벤트 루프가 이미 종료 중이면 lock을 풀어 use-after-unload를 만들지 않는다.
            # 프로세스 종료 이외의 상황이면 호출자가 상태 API/로그에서 pending을 확인할 수 있다.
            logger.exception(
                "deferred native cleanup finalizer를 시작하지 못했습니다. "
                "안전을 위해 모델 admission lock을 계속 유지합니다."
            )
            raise

        cleanup.task = task
        task.add_done_callback(
            lambda completed_task: self._recover_deferred_context_cleanup(cleanup, completed_task)
        )

    def _recover_deferred_context_cleanup(
        self,
        cleanup: _DeferredContextCleanup,
        completed_task: asyncio.Task[None],
    ) -> None:
        """시작 전 취소·예기치 않은 finalizer 종료 시 안전하게 finalizer를 재시작한다."""
        if self._deferred_context_cleanup is not cleanup:
            return

        if completed_task.cancelled():
            reason = "취소됨"
        else:
            error = completed_task.exception()
            reason = f"오류: {error}" if error is not None else "완료 신호 없이 종료됨"

        logger.error(
            "deferred native cleanup finalizer가 %s. "
            "모델 admission lock을 유지한 채 재시작합니다: model=%s",
            reason,
            cleanup.model_name,
        )
        try:
            self._start_deferred_context_cleanup(cleanup)
        except RuntimeError:
            # _start...가 이미 안전한 보존 상태와 상세 로그를 남긴다.
            return

    async def _wait_for_deferred_native_workers(
        self,
        cleanup: _DeferredContextCleanup,
    ) -> None:
        """native 함수 본문이 실제로 끝날 때까지 finalizer 취소를 흡수하며 대기한다."""
        while any(not worker.finished.is_set() for worker in cleanup.workers):
            try:
                await asyncio.sleep(_NATIVE_WORKER_POLL_SECONDS)
            except asyncio.CancelledError:
                logger.warning(
                    "deferred native cleanup finalizer 취소 요청을 보류: "
                    f"model={cleanup.model_name}"
                )

        for worker in cleanup.workers:
            await self._drain_deferred_native_worker(worker, cleanup.model_name)

    async def _drain_deferred_native_worker(
        self,
        worker: _NativeInferenceWorker,
        model_name: str,
    ) -> None:
        """종료된 native worker Task의 결과를 회수한다.

        ``finished``가 먼저 설정되므로, 외부에서 Task 자체가 취소되었더라도 native
        함수는 이미 모델 참조를 사용하지 않는 상태다.
        """
        while not worker.task.done():
            try:
                await asyncio.shield(worker.task)
            except asyncio.CancelledError:
                if worker.task.cancelled():
                    logger.warning(
                        "종료된 native worker Task가 취소됨: model=%s",
                        model_name,
                    )
                    return
                logger.warning(
                    "deferred native worker 결과 회수 중 취소 요청을 보류: model=%s",
                    model_name,
                )
            except BaseException as worker_error:
                logger.warning(
                    "취소 후 native worker가 오류로 종료됨: model=%s, error=%s",
                    model_name,
                    worker_error,
                )
                return

        if worker.task.cancelled():
            logger.warning("종료된 native worker Task가 취소됨: model=%s", model_name)
            return

        error = worker.task.exception()
        if error is not None:
            logger.warning(
                "취소 후 native worker가 오류로 종료됨: model=%s, error=%s",
                model_name,
                error,
            )

    async def _await_deferred_unload(self, cleanup: _DeferredContextCleanup) -> None:
        """finalizer 취소 요청과 분리된 unload를 끝까지 수행한다."""
        unload_task = asyncio.create_task(
            self._unload_model_from_context(),
            name=f"model-native-unload:{cleanup.model_name}",
        )
        while True:
            try:
                await asyncio.shield(unload_task)
                return
            except asyncio.CancelledError:
                if unload_task.cancelled():
                    raise RuntimeError("deferred 모델 언로드 Task가 취소되었습니다.") from None
                logger.warning(
                    "deferred native cleanup 중 unload 취소 요청을 보류: model=%s",
                    cleanup.model_name,
                )

    async def _finalize_deferred_context_cleanup(
        self,
        cleanup: _DeferredContextCleanup,
    ) -> None:
        """worker 종료 뒤 모델 cleanup과 admission lock 반납을 순서대로 수행한다."""
        await self._wait_for_deferred_native_workers(cleanup)
        if cleanup.unload_after:
            await self._await_deferred_unload(cleanup)
        self._complete_deferred_context_cleanup(cleanup)

    def _complete_deferred_context_cleanup(self, cleanup: _DeferredContextCleanup) -> None:
        """안전한 종료가 확인된 deferred lease를 완료 처리하고 lock을 반납한다."""
        if self._deferred_context_cleanup is not cleanup:
            return

        self._deferred_context_cleanup = None
        self._context_lock.release()
        logger.info(
            "deferred native cleanup 완료: model=%s, workers=%d",
            cleanup.model_name,
            len(cleanup.workers),
        )

    def acquire(
        self,
        name: str,
        loader: ModelLoader,
        *,
        keep_loaded: bool = False,
    ) -> _ModelContext:
        """컨텍스트 매니저로 모델을 로드하고, 블록 종료 시 자동 언로드한다.

        사용 예시:
            async with manager.acquire("whisper", load_fn) as model:
                result = model.transcribe(audio)
            # 블록 종료 시 자동 언로드

        컨텍스트 블록 전체를 직렬화하여 같은 MLX backend 인스턴스에 여러
        스레드/태스크가 동시에 generate() 또는 transcribe()를 호출하지 못하게 한다.

        PERF-001: keep_loaded=True 시 블록 종료 후에도 모델을 언로드하지 않는다.
        연속으로 동일 모델을 사용하는 단계(corrector → summarizer)에서
        불필요한 해제/재로드를 방지한다.

        Args:
            name: 모델 식별 이름
            loader: 모델 로드 함수 (동기 또는 비동기)
            keep_loaded: True면 블록 종료 후에도 모델 유지 (기본 False)

        Returns:
            비동기 컨텍스트 매니저 (_ModelContext)
        """
        return _ModelContext(self, name, loader, keep_loaded=keep_loaded)

    def get_status(self) -> dict[str, Any]:
        """현재 모델 매니저의 상태 정보를 딕셔너리로 반환한다.

        Returns:
            모델명, 메모리 사용량, 로드 시간 등을 포함한 상태 딕셔너리
        """
        status: dict[str, Any] = {
            "is_model_loaded": self.is_model_loaded,
            "current_model_name": self.current_model_name,
            "memory_usage_mb": round(self._get_memory_usage_mb(), 1),
            "memory_usage_gb": round(self._get_memory_usage_gb(), 3),
            "peak_ram_limit_gb": self._config.pipeline.peak_ram_limit_gb,
            "native_cleanup_pending": self._deferred_context_cleanup is not None,
        }
        if self._current is not None:
            status["model_memory_delta_mb"] = round(self._current.memory_delta_mb, 1)
            status["model_loaded_at"] = self._current.loaded_at
        if self._deferred_context_cleanup is not None:
            cleanup = self._deferred_context_cleanup
            status["native_cleanup_model_name"] = cleanup.model_name
            status["native_cleanup_pending_workers"] = sum(
                not worker.finished.is_set() for worker in cleanup.workers
            )
            status["native_cleanup_started_at"] = cleanup.started_at
        return status


class _ModelContext:
    """ModelLoadManager.acquire()에서 반환되는 비동기 컨텍스트 매니저.

    __aenter__에서 모델을 로드하고, __aexit__에서 자동 언로드한다.
    예외가 발생해도 반드시 언로드를 수행한다.

    PERF-001: keep_loaded=True 시 블록 종료 후에도 모델을 유지한다.
    """

    def __init__(
        self,
        manager: ModelLoadManager,
        name: str,
        loader: ModelLoader,
        *,
        keep_loaded: bool = False,
    ) -> None:
        self._manager = manager
        self._name = name
        self._loader = loader
        self._keep_loaded = keep_loaded
        self._context_token: Token[Any] | None = None
        self._native_workers: list[_NativeInferenceWorker] = []
        self._closing = False
        self._native_worker_cancellation_pending = False

    async def __aenter__(self) -> Any:
        """모델을 로드하고 인스턴스를 반환한다."""
        await self._manager._acquire_context_lock()
        try:
            model = await self._manager._load_model_with_state_lock(self._name, self._loader)
            self._context_token = _active_model_context.set(self)
            return model
        except BaseException:
            # ``asyncio.CancelledError`` 는 Exception 계층 밖에 있으므로,
            # 비동기 loader 대기 중 취소되면 context lock도 반드시 반납해야 한다.
            # 그렇지 않으면 이후 모든 load/acquire가 영구 대기한다.
            self._manager._context_lock.release()
            raise

    @staticmethod
    def _observe_native_worker(task: asyncio.Task[Any]) -> None:
        """완료된 worker 예외를 회수해 취소 뒤 경고 누락을 막는다."""
        if task.cancelled():
            return
        task.exception()

    async def _await_native_inference(
        self,
        func: Callable[..., T],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """native worker를 현재 context lease에 등록하고 취소는 즉시 전파한다."""
        if self._closing or self._native_worker_cancellation_pending:
            raise RuntimeError(
                "종료 중이거나 취소된 native worker가 남은 모델 컨텍스트에서는 "
                "native inference를 시작할 수 없습니다."
            )

        finished = threading.Event()

        def run_worker() -> T:
            try:
                return func(*args, **kwargs)
            finally:
                # asyncio Task 취소와 무관하게 실제 native 함수 본문 종료를 표시한다.
                finished.set()

        task = asyncio.create_task(
            asyncio.to_thread(run_worker),
            name=f"native-inference:{self._name}",
        )
        task.add_done_callback(self._observe_native_worker)
        worker = _NativeInferenceWorker(task=task, finished=finished)
        self._native_workers.append(worker)

        # shield가 없으면 wait_for()/상위 task의 취소가 Task만 취소하고 실제 native
        # thread는 계속 실행한다. 그 상태에서 context cleanup이 모델을 해제하면 위험하다.
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # 호출자가 취소를 잡아 같은 context에서 다른 native 호출을 시작하면, 아직
            # 실행 중인 이전 worker와 모델을 동시 사용하게 된다. context exit 전에는
            # 이를 fail-closed로 막고 deferred finalizer가 lease를 넘겨받게 한다.
            if not finished.is_set():
                self._native_worker_cancellation_pending = True
            raise

    def _take_pending_native_workers(self) -> tuple[_NativeInferenceWorker, ...]:
        """context 종료 시 아직 실제 native 함수가 실행 중인 worker를 분리한다."""
        self._closing = True
        pending = tuple(worker for worker in self._native_workers if not worker.finished.is_set())
        self._native_workers.clear()
        return pending

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """모델을 언로드한다. keep_loaded=True면 유지, 예외 시에는 항상 언로드."""
        release_context_lock = True
        try:
            pending_workers = self._take_pending_native_workers()
            if pending_workers:
                # timeout/cancel은 호출자에게 즉시 전파한다. 대신 모델과 admission lock의
                # 소유권을 finalizer로 옮겨 actual native thread 종료 전 cleanup을 막는다.
                unload_after = not (self._keep_loaded and exc_type is None)
                # finalizer 생성이 실패해도 lock을 먼저 반납하면 native worker가 모델을
                # 계속 쓰는 중 use-after-unload가 된다. 그 드문 경우에는 안전하게 lock을
                # 보존하고 오류를 전파해 운영자가 pending 상태/로그를 확인하게 한다.
                release_context_lock = False
                self._manager._defer_context_cleanup(
                    model_name=self._name,
                    workers=pending_workers,
                    unload_after=unload_after,
                )
                return

            # PERF-001: keep_loaded=True이고 예외가 없으면 모델 유지
            if self._keep_loaded and exc_type is None:
                logger.debug(f"모델 유지 (keep_loaded=True): {self._name}")
                return
            await self._manager._unload_model_from_context()
        finally:
            if self._context_token is not None:
                _active_model_context.reset(self._context_token)
                self._context_token = None
            if release_context_lock:
                self._manager._context_lock.release()


# 모듈 수준 싱글턴 인스턴스 (threading.Lock으로 경합 조건 방지)
_manager_instance: ModelLoadManager | None = None
_manager_lock = threading.Lock()


def get_model_manager() -> ModelLoadManager:
    """싱글턴 패턴으로 ModelLoadManager 인스턴스를 반환한다.

    threading.Lock으로 동시 호출 시 경합 조건을 방지한다.
    (STAB: 싱글턴 경합 조건 수정)

    첫 호출 시 인스턴스를 생성하고, 이후에는 캐시된 인스턴스를 반환한다.

    Returns:
        ModelLoadManager 싱글턴 인스턴스
    """
    global _manager_instance
    if _manager_instance is None:
        with _manager_lock:
            # 더블 체크 패턴: 락 획득 후 재확인
            if _manager_instance is None:
                _manager_instance = ModelLoadManager()
    return _manager_instance


def reset_model_manager() -> None:
    """싱글턴 인스턴스를 초기화한다. 테스트 용도로만 사용."""
    global _manager_instance
    with _manager_lock:
        _manager_instance = None
