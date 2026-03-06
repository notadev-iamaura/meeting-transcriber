"""
모델 로드 매니저 (Model Load Manager)

목적: 한 번에 하나의 대형 모델만 메모리에 적재하도록 제어하는 뮤텍스 기반 매니저.
주요 기능:
    - asyncio.Lock 기반 동시 로드 방지
    - 이전 모델 자동 언로드 (gc.collect + Metal 캐시 정리)
    - async with 컨텍스트 매니저 패턴 지원
    - psutil 기반 메모리 사용량 모니터링
의존성: asyncio, gc, psutil, config 모듈
"""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional, TypeVar, Union

import psutil

from config import get_config

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 모델 로더 타입: 동기 또는 비동기 함수
ModelLoader = Union[Callable[[], T], Callable[[], Coroutine[Any, Any, T]]]


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

    def __init__(self) -> None:
        """ModelLoadManager 초기화."""
        self._lock = asyncio.Lock()
        self._current: Optional[ModelInfo] = None
        self._config = get_config()
        logger.info("ModelLoadManager 초기화 완료")

    @property
    def current_model_name(self) -> Optional[str]:
        """현재 로드된 모델의 이름. 없으면 None.
        
        Returns:
            모델명 또는 None
        """
        return self._current.name if self._current else None

    @property
    def current_model(self) -> Optional[Any]:
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
        return process.memory_info().rss / (1024 * 1024)

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
        """
        try:
            import mlx.core as mx  # type: ignore[import-untyped]
            mx.metal.clear_cache()
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

        # 모델 참조 제거
        self._current.instance = None
        self._current = None

        # 가비지 컬렉션 수행
        gc.collect()

        # Apple Silicon Metal 캐시 정리
        self._clear_gpu_cache()

        mem_after_unload = self._get_memory_usage_mb()
        freed_mb = mem_before_unload - mem_after_unload

        logger.info(
            f"모델 언로드 완료: {model_name} | "
            f"해제된 메모리: {freed_mb:.1f}MB | "
            f"현재 메모리: {mem_after_unload:.1f}MB"
        )

    def _check_memory_limit(self) -> None:
        """현재 메모리 사용량이 peak_ram_limit_gb를 초과하는지 확인한다.

        초과 시 경고 로그를 남긴다 (강제 중단하지는 않음).
        """
        current_gb = self._get_memory_usage_gb()
        limit_gb = self._config.pipeline.peak_ram_limit_gb

        if current_gb > limit_gb:
            logger.warning(
                f"메모리 사용량 경고: {current_gb:.2f}GB / "
                f"제한: {limit_gb:.1f}GB"
            )

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
        async with self._lock:
            # 같은 모델이 이미 로드되어 있으면 재사용
            if self._current is not None and self._current.name == name:
                logger.info(f"모델 이미 로드됨, 재사용: {name}")
                return self._current.instance

            # 기존 모델 언로드
            await self._unload_current()

            # 새 모델 로드
            mem_before = self._get_memory_usage_mb()
            logger.info(
                f"모델 로드 시작: {name} | "
                f"현재 메모리: {mem_before:.1f}MB"
            )

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

        Lock을 획득한 후 언로드하여 동시 접근을 방지한다.
        로드된 모델이 없으면 아무 동작도 하지 않는다.
        """
        async with self._lock:
            await self._unload_current()

    def acquire(
        self,
        name: str,
        loader: ModelLoader,
    ) -> "_ModelContext":
        """컨텍스트 매니저로 모델을 로드하고, 블록 종료 시 자동 언로드한다.

        사용 예시:
            async with manager.acquire("whisper", load_fn) as model:
                result = model.transcribe(audio)
            # 블록 종료 시 자동 언로드

        Args:
            name: 모델 식별 이름
            loader: 모델 로드 함수 (동기 또는 비동기)

        Returns:
            비동기 컨텍스트 매니저 (_ModelContext)
        """
        return _ModelContext(self, name, loader)

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
        }
        if self._current is not None:
            status["model_memory_delta_mb"] = round(
                self._current.memory_delta_mb, 1
            )
            status["model_loaded_at"] = self._current.loaded_at
        return status


class _ModelContext:
    """ModelLoadManager.acquire()에서 반환되는 비동기 컨텍스트 매니저.

    __aenter__에서 모델을 로드하고, __aexit__에서 자동 언로드한다.
    예외가 발생해도 반드시 언로드를 수행한다.
    """

    def __init__(
        self,
        manager: ModelLoadManager,
        name: str,
        loader: ModelLoader,
    ) -> None:
        self._manager = manager
        self._name = name
        self._loader = loader

    async def __aenter__(self) -> Any:
        """모델을 로드하고 인스턴스를 반환한다."""
        return await self._manager.load_model(self._name, self._loader)

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """모델을 언로드한다. 예외 발생 여부와 무관하게 반드시 수행."""
        await self._manager.unload_model()


# 모듈 수준 싱글턴 인스턴스 (threading.Lock으로 경합 조건 방지)
_manager_instance: Optional[ModelLoadManager] = None
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
