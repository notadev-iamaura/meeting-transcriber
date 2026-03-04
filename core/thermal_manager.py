"""
서멀 매니저 모듈 (Thermal Manager Module)

목적: 팬리스 MacBook Air의 과열을 방지하기 위한 서멀 관리 모듈.
     배치 카운터 기반 쿨다운과 CPU 온도 모니터링(best-effort)을 결합하여
     장시간 파이프라인 실행 시 서멀 스로틀링을 예방한다.
주요 기능:
    - 배치 카운터 기반 쿨다운 (2건 처리 후 3분 대기)
    - CPU 온도 모니터링 (macOS 환경에서 best-effort)
    - 85°C 이상: 추가 쿨다운 대기 (속도 조절)
    - 95°C 이상: 온도 하강까지 강제 대기 (긴급 정지)
    - 쿨다운 상태 알림 (콜백 지원)
의존성: config 모듈 (ThermalConfig)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from config import AppConfig, ThermalConfig

logger = logging.getLogger(__name__)


class ThermalState(str, Enum):
    """서멀 상태를 나타내는 열거형.

    Attributes:
        NORMAL: 정상 온도, 작업 진행 가능
        THROTTLED: 속도 조절 구간 (85°C 이상)
        HALTED: 긴급 정지 구간 (95°C 이상)
        COOLING: 배치 쿨다운 진행 중
    """

    NORMAL = "normal"
    THROTTLED = "throttled"
    HALTED = "halted"
    COOLING = "cooling"


@dataclass
class ThermalStatus:
    """현재 서멀 상태 정보를 담는 데이터 클래스.

    Attributes:
        state: 현재 서멀 상태
        cpu_temp_celsius: CPU 온도 (읽기 실패 시 None)
        batch_count: 현재 배치에서 처리한 작업 수
        batch_limit: 배치 한도 (이 값에 도달하면 쿨다운)
        cooldown_remaining_seconds: 남은 쿨다운 시간 (초)
        total_jobs_processed: 총 처리 완료 작업 수
    """

    state: ThermalState = ThermalState.NORMAL
    cpu_temp_celsius: Optional[float] = None
    batch_count: int = 0
    batch_limit: int = 2
    cooldown_remaining_seconds: float = 0.0
    total_jobs_processed: int = 0


# 콜백 타입: 서멀 상태 변경 시 호출되는 함수
ThermalCallback = Callable[[ThermalStatus], None]


class ThermalManager:
    """팬리스 MacBook Air를 위한 서멀 관리자.

    이중 보호 전략을 사용한다:
    1. 배치 카운터 기반 쿨다운: 설정된 건수만큼 처리 후 강제 쿨다운
    2. CPU 온도 모니터링 (best-effort): 온도 읽기 가능 시 추가 보호

    macOS Apple Silicon에서는 관리자 권한 없이 CPU 온도를 직접 읽을 수 없으므로,
    배치 카운터가 주요 보호 메커니즘이고 온도 모니터링은 보조 역할을 한다.

    Args:
        config: AppConfig 인스턴스 또는 ThermalConfig 인스턴스
    """

    def __init__(self, config: AppConfig | ThermalConfig) -> None:
        """서멀 매니저를 초기화한다.

        Args:
            config: 전체 AppConfig 또는 ThermalConfig 단독 전달 가능
        """
        if isinstance(config, AppConfig):
            self._config = config.thermal
        else:
            self._config = config

        # 배치 카운터
        self._batch_count: int = 0
        self._total_jobs_processed: int = 0

        # 쿨다운 상태
        self._cooling: bool = False
        self._cooldown_start_time: Optional[float] = None

        # 콜백 목록
        self._callbacks: list[ThermalCallback] = []

        # 온도 읽기 가능 여부 (첫 시도 후 결정)
        self._temp_reader_available: Optional[bool] = None

        logger.info(
            f"서멀 매니저 초기화 완료. "
            f"배치 한도={self._config.batch_size}건, "
            f"쿨다운={self._config.cooldown_seconds}초, "
            f"스로틀 온도={self._config.cpu_temp_throttle_celsius}°C, "
            f"정지 온도={self._config.cpu_temp_halt_celsius}°C"
        )

    @property
    def batch_count(self) -> int:
        """현재 배치에서 처리한 작업 수."""
        return self._batch_count

    @property
    def total_jobs_processed(self) -> int:
        """총 처리 완료 작업 수."""
        return self._total_jobs_processed

    @property
    def is_cooling(self) -> bool:
        """쿨다운 진행 중 여부."""
        return self._cooling

    def register_callback(self, callback: ThermalCallback) -> None:
        """서멀 상태 변경 시 호출될 콜백을 등록한다.

        Args:
            callback: ThermalStatus를 인자로 받는 콜백 함수
        """
        self._callbacks.append(callback)
        callback_name = getattr(callback, "__name__", repr(callback))
        logger.debug(f"서멀 콜백 등록: {callback_name}")

    def unregister_callback(self, callback: ThermalCallback) -> None:
        """등록된 콜백을 제거한다.

        Args:
            callback: 제거할 콜백 함수
        """
        try:
            self._callbacks.remove(callback)
            callback_name = getattr(callback, "__name__", repr(callback))
            logger.debug(f"서멀 콜백 제거: {callback_name}")
        except ValueError:
            logger.warning("등록되지 않은 콜백을 제거하려 했습니다.")

    def get_status(self) -> ThermalStatus:
        """현재 서멀 상태를 반환한다.

        Returns:
            현재 서멀 상태 정보
        """
        cpu_temp = self._read_cpu_temperature()
        state = self._determine_state(cpu_temp)

        cooldown_remaining = 0.0
        if self._cooling and self._cooldown_start_time is not None:
            elapsed = time.monotonic() - self._cooldown_start_time
            cooldown_remaining = max(0.0, self._config.cooldown_seconds - elapsed)

        return ThermalStatus(
            state=state,
            cpu_temp_celsius=cpu_temp,
            batch_count=self._batch_count,
            batch_limit=self._config.batch_size,
            cooldown_remaining_seconds=cooldown_remaining,
            total_jobs_processed=self._total_jobs_processed,
        )

    def _determine_state(self, cpu_temp: Optional[float]) -> ThermalState:
        """CPU 온도와 쿨다운 상태로 현재 서멀 상태를 결정한다.

        Args:
            cpu_temp: CPU 온도 (None이면 온도 기반 판단 건너뜀)

        Returns:
            결정된 서멀 상태
        """
        if self._cooling:
            return ThermalState.COOLING

        if cpu_temp is not None:
            if cpu_temp >= self._config.cpu_temp_halt_celsius:
                return ThermalState.HALTED
            if cpu_temp >= self._config.cpu_temp_throttle_celsius:
                return ThermalState.THROTTLED

        return ThermalState.NORMAL

    def _read_cpu_temperature(self) -> Optional[float]:
        """CPU 온도를 읽는다 (best-effort).

        macOS Apple Silicon에서는 관리자 권한 없이 CPU 온도를 직접 읽을 수 없다.
        사용 가능한 방법이 있으면 시도하고, 없으면 None을 반환한다.

        Returns:
            CPU 온도 (섭씨) 또는 None (읽기 불가)
        """
        # 이전에 읽기 불가능으로 판정된 경우 재시도하지 않음
        if self._temp_reader_available is False:
            return None

        try:
            temp = self._try_read_temperature()
            if temp is not None:
                self._temp_reader_available = True
                return temp
        except Exception as e:
            logger.debug(f"CPU 온도 읽기 실패: {e}")

        # 첫 시도에서 실패한 경우
        if self._temp_reader_available is None:
            self._temp_reader_available = False
            logger.info(
                "CPU 온도 읽기 불가. 배치 카운터 기반 쿨다운만 사용합니다. "
                "(macOS Apple Silicon에서는 관리자 권한 없이 온도 읽기가 제한됩니다)"
            )

        return None

    def _try_read_temperature(self) -> Optional[float]:
        """실제 CPU 온도 읽기를 시도한다.

        macOS에서 관리자 권한 없이 사용 가능한 방법을 순차적으로 시도한다.
        현재 Apple Silicon에서는 관리자 권한 없이 직접 온도를 읽을 수 없으므로,
        이 메서드는 주로 테스트에서 모킹되어 사용된다.

        Returns:
            CPU 온도 (섭씨) 또는 None
        """
        # macOS Apple Silicon: 관리자 권한 없이 CPU 온도 직접 읽기 불가
        # powermetrics (root 필요), sysctl (Apple Silicon에서 온도 키 없음)
        # 향후 서드파티 도구(osx-cpu-temp 등) 설치 시 여기에 추가 가능
        return None

    def _notify_callbacks(self, status: ThermalStatus) -> None:
        """등록된 콜백들에게 서멀 상태를 알린다.

        Args:
            status: 현재 서멀 상태 정보
        """
        for callback in self._callbacks:
            try:
                callback(status)
            except Exception as e:
                callback_name = getattr(callback, "__name__", repr(callback))
                logger.warning(f"서멀 콜백 실행 중 에러 ({callback_name}): {e}")

    async def notify_job_started(self) -> None:
        """작업 시작을 알린다.

        파이프라인에서 새 작업을 시작하기 전에 호출한다.
        현재 서멀 상태를 확인하고 필요 시 대기한다.
        """
        # 먼저 온도 기반 대기가 필요한지 확인
        await self._wait_for_safe_temperature()

        logger.debug(
            f"작업 시작 알림. 배치 카운트: {self._batch_count}/{self._config.batch_size}"
        )

    async def notify_job_completed(self) -> None:
        """작업 완료를 알린다.

        파이프라인에서 작업 완료 후 호출한다.
        배치 카운터를 증가시키고, 한도 도달 시 쿨다운을 시작한다.
        """
        self._batch_count += 1
        self._total_jobs_processed += 1

        logger.info(
            f"작업 완료. 배치 카운트: {self._batch_count}/{self._config.batch_size}, "
            f"총 처리: {self._total_jobs_processed}건"
        )

        # 배치 한도 도달 시 쿨다운 시작
        if self._batch_count >= self._config.batch_size:
            await self._start_cooldown()

    async def wait_if_needed(self) -> None:
        """다음 작업 시작 전 서멀 상태를 확인하고 필요 시 대기한다.

        이 메서드는 파이프라인에서 다음 작업 시작 전에 호출되어야 한다.
        배치 쿨다운 중이거나 CPU 온도가 높으면 안전해질 때까지 대기한다.
        """
        # 1. 배치 쿨다운 대기
        if self._cooling:
            await self._wait_cooldown()

        # 2. 온도 기반 대기
        await self._wait_for_safe_temperature()

    async def _start_cooldown(self) -> None:
        """배치 쿨다운을 시작한다.

        배치 카운터가 한도에 도달하면 호출된다.
        설정된 시간만큼 대기하고 배치 카운터를 리셋한다.
        """
        self._cooling = True
        self._cooldown_start_time = time.monotonic()

        status = self.get_status()
        self._notify_callbacks(status)

        logger.info(
            f"배치 쿨다운 시작. {self._config.batch_size}건 처리 완료. "
            f"{self._config.cooldown_seconds}초 대기..."
        )

        await asyncio.sleep(self._config.cooldown_seconds)

        # 쿨다운 완료
        self._cooling = False
        self._cooldown_start_time = None
        self._batch_count = 0

        logger.info("배치 쿨다운 완료. 배치 카운터 리셋.")

        status = self.get_status()
        self._notify_callbacks(status)

    async def _wait_cooldown(self) -> None:
        """진행 중인 쿨다운이 완료될 때까지 대기한다."""
        if not self._cooling:
            return

        if self._cooldown_start_time is not None:
            elapsed = time.monotonic() - self._cooldown_start_time
            remaining = max(0.0, self._config.cooldown_seconds - elapsed)

            if remaining > 0:
                logger.info(f"쿨다운 대기 중... 남은 시간: {remaining:.1f}초")
                await asyncio.sleep(remaining)

        # 쿨다운 완료 처리
        self._cooling = False
        self._cooldown_start_time = None
        self._batch_count = 0

        logger.info("쿨다운 대기 완료.")

    async def _wait_for_safe_temperature(self) -> None:
        """CPU 온도가 안전 범위에 들어올 때까지 대기한다.

        온도 읽기가 불가능하면 즉시 반환한다 (배치 카운터에만 의존).
        """
        # 최초 온도 체크
        cpu_temp = self._read_cpu_temperature()
        if cpu_temp is None:
            return

        # 95°C 이상: 긴급 정지 - 스로틀 온도 이하로 떨어질 때까지 대기
        if cpu_temp >= self._config.cpu_temp_halt_celsius:
            logger.warning(
                f"CPU 온도 위험! {cpu_temp:.1f}°C >= {self._config.cpu_temp_halt_celsius}°C. "
                f"긴급 쿨다운 시작."
            )
            status = self.get_status()
            self._notify_callbacks(status)

            await self._wait_until_temp_below(self._config.cpu_temp_throttle_celsius)
            return

        # 85°C 이상: 속도 조절 - 추가 쿨다운 대기
        if cpu_temp >= self._config.cpu_temp_throttle_celsius:
            throttle_wait = self._config.cooldown_seconds // 3  # 쿨다운 시간의 1/3
            logger.warning(
                f"CPU 온도 높음. {cpu_temp:.1f}°C >= {self._config.cpu_temp_throttle_celsius}°C. "
                f"{throttle_wait}초 추가 대기."
            )
            status = self.get_status()
            self._notify_callbacks(status)

            await asyncio.sleep(throttle_wait)

    async def _wait_until_temp_below(self, target_celsius: float) -> None:
        """CPU 온도가 목표 온도 이하로 내려갈 때까지 대기한다.

        10초 간격으로 온도를 확인하며, 최대 쿨다운 시간의 3배까지 대기.
        온도 읽기가 실패하면 기본 쿨다운 시간만큼 대기 후 반환.

        Args:
            target_celsius: 목표 온도 (섭씨)
        """
        check_interval = 10  # 10초마다 온도 확인
        max_wait = self._config.cooldown_seconds * 3  # 최대 대기 시간
        waited = 0.0

        while waited < max_wait:
            await asyncio.sleep(check_interval)
            waited += check_interval

            cpu_temp = self._read_cpu_temperature()

            # 온도 읽기 실패 시 기본 쿨다운 시간만큼 대기
            if cpu_temp is None:
                remaining = max(0.0, self._config.cooldown_seconds - waited)
                if remaining > 0:
                    logger.info(
                        f"온도 읽기 실패. 기본 쿨다운 {remaining:.0f}초 대기."
                    )
                    await asyncio.sleep(remaining)
                return

            logger.info(
                f"긴급 쿨다운 중... CPU 온도: {cpu_temp:.1f}°C "
                f"(목표: {target_celsius}°C 이하, 대기: {waited:.0f}초)"
            )

            if cpu_temp < target_celsius:
                logger.info(
                    f"CPU 온도 안정화. {cpu_temp:.1f}°C < {target_celsius}°C. "
                    f"작업 재개 가능."
                )
                return

        logger.warning(
            f"최대 대기 시간({max_wait}초) 초과. "
            f"현재 온도: {self._read_cpu_temperature()}°C. 작업 재개."
        )

    def reset(self) -> None:
        """서멀 매니저 상태를 초기화한다.

        배치 카운터와 쿨다운 상태를 리셋한다.
        테스트 또는 시스템 재시작 시 사용한다.
        """
        self._batch_count = 0
        self._total_jobs_processed = 0
        self._cooling = False
        self._cooldown_start_time = None
        self._temp_reader_available = None
        logger.info("서멀 매니저 상태 초기화 완료.")
