"""
서멀 매니저 테스트 모듈 (Thermal Manager Test Module)

목적: ThermalManager의 배치 쿨다운, 온도 모니터링, 상태 관리 기능을 검증한다.
주요 테스트:
    - 배치 카운터 기반 쿨다운 동작 검증
    - CPU 온도 기반 스로틀/정지 동작 검증
    - 쿨다운 후 카운터 리셋 검증
    - 콜백 알림 동작 검증
    - 온도 읽기 실패 시 폴백 동작 검증
    - 상태 조회 검증
의존성: pytest, asyncio, unittest.mock
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import ThermalConfig
from core.thermal_manager import (
    ThermalManager,
    ThermalState,
    ThermalStatus,
)

# ThermalConfig 최소 cooldown_seconds는 30 (pydantic 검증)
_MIN_COOLDOWN = 30


# === 테스트용 설정 팩토리 ===


def _make_config(
    batch_size: int = 2,
    cooldown_seconds: int = _MIN_COOLDOWN,
    cpu_temp_throttle_celsius: int = 85,
    cpu_temp_halt_celsius: int = 95,
) -> ThermalConfig:
    """테스트용 ThermalConfig를 생성한다.

    Args:
        batch_size: 배치 한도
        cooldown_seconds: 쿨다운 시간 (초, 최소 30)
        cpu_temp_throttle_celsius: 속도 조절 온도
        cpu_temp_halt_celsius: 강제 정지 온도

    Returns:
        ThermalConfig 인스턴스
    """
    return ThermalConfig(
        batch_size=batch_size,
        cooldown_seconds=cooldown_seconds,
        cpu_temp_throttle_celsius=cpu_temp_throttle_celsius,
        cpu_temp_halt_celsius=cpu_temp_halt_celsius,
    )


# asyncio.sleep 모킹 데코레이터 (실제 대기 방지)
_SLEEP_PATCH = "core.thermal_manager.asyncio.sleep"


# === 초기화 테스트 ===


class TestThermalManagerInit:
    """ThermalManager 초기화 관련 테스트."""

    def test_init_with_thermal_config(self) -> None:
        """ThermalConfig로 직접 초기화할 수 있다."""
        config = _make_config(batch_size=3, cooldown_seconds=60)
        manager = ThermalManager(config)

        assert manager.batch_count == 0
        assert manager.total_jobs_processed == 0
        assert manager.is_cooling is False

    def test_init_with_app_config(self) -> None:
        """AppConfig로 초기화하면 thermal 설정을 추출한다."""
        from config import AppConfig

        app_config = AppConfig(thermal=_make_config(batch_size=5))
        manager = ThermalManager(app_config)

        status = manager.get_status()
        assert status.batch_limit == 5

    def test_initial_status(self) -> None:
        """초기 상태는 NORMAL이다."""
        manager = ThermalManager(_make_config())
        status = manager.get_status()

        assert status.state == ThermalState.NORMAL
        assert status.batch_count == 0
        assert status.total_jobs_processed == 0
        assert status.cooldown_remaining_seconds == 0.0


# === 배치 카운터 테스트 ===


class TestBatchCounter:
    """배치 카운터 및 쿨다운 관련 테스트."""

    @pytest.mark.asyncio
    async def test_batch_count_increases_on_job_complete(self) -> None:
        """작업 완료 시 배치 카운터가 증가한다."""
        manager = ThermalManager(_make_config(batch_size=5))

        with patch(_SLEEP_PATCH, new_callable=AsyncMock):
            await manager.notify_job_completed()
            assert manager.batch_count == 1
            assert manager.total_jobs_processed == 1

            await manager.notify_job_completed()
            assert manager.batch_count == 2
            assert manager.total_jobs_processed == 2

    @pytest.mark.asyncio
    async def test_cooldown_triggers_at_batch_limit(self) -> None:
        """배치 한도 도달 시 쿨다운이 시작되고, 완료 후 카운터가 리셋된다."""
        config = _make_config(batch_size=2)
        manager = ThermalManager(config)

        with patch(_SLEEP_PATCH, new_callable=AsyncMock) as mock_sleep:
            # 첫 번째 작업 완료 - 쿨다운 없음
            await manager.notify_job_completed()
            assert manager.batch_count == 1
            assert manager.is_cooling is False

            # 두 번째 작업 완료 - 쿨다운 발생 후 리셋
            await manager.notify_job_completed()
            assert manager.batch_count == 0  # 쿨다운 후 리셋
            assert manager.is_cooling is False  # 쿨다운 완료
            # asyncio.sleep이 쿨다운 시간으로 호출됨
            mock_sleep.assert_called_with(_MIN_COOLDOWN)

    @pytest.mark.asyncio
    async def test_total_jobs_accumulates_across_batches(self) -> None:
        """총 작업 수는 배치 리셋과 관계없이 누적된다."""
        config = _make_config(batch_size=2)
        manager = ThermalManager(config)

        with patch(_SLEEP_PATCH, new_callable=AsyncMock):
            # 첫 번째 배치 (2건 → 쿨다운)
            await manager.notify_job_completed()
            await manager.notify_job_completed()
            assert manager.total_jobs_processed == 2

            # 두 번째 배치 (1건)
            await manager.notify_job_completed()
            assert manager.total_jobs_processed == 3
            assert manager.batch_count == 1  # 리셋 후 1건

    @pytest.mark.asyncio
    async def test_wait_if_needed_no_cooldown(self) -> None:
        """쿨다운 중이 아닐 때 wait_if_needed는 즉시 반환한다."""
        manager = ThermalManager(_make_config(batch_size=5))

        with patch(_SLEEP_PATCH, new_callable=AsyncMock) as mock_sleep:
            await manager.wait_if_needed()
            # 쿨다운 중이 아니므로 sleep 호출 없음
            mock_sleep.assert_not_called()


# === 온도 모니터링 테스트 ===


class TestTemperatureMonitoring:
    """CPU 온도 모니터링 관련 테스트."""

    def test_default_temp_reader_returns_none(self) -> None:
        """기본 온도 읽기는 None을 반환한다 (Apple Silicon 제약)."""
        manager = ThermalManager(_make_config())
        status = manager.get_status()
        assert status.cpu_temp_celsius is None

    def test_state_normal_when_temp_unavailable(self) -> None:
        """온도 읽기 불가 시 NORMAL 상태이다."""
        manager = ThermalManager(_make_config())
        status = manager.get_status()
        assert status.state == ThermalState.NORMAL

    def test_state_throttled_when_temp_above_throttle(self) -> None:
        """온도가 스로틀 임계값 이상이면 THROTTLED 상태이다."""
        config = _make_config(cpu_temp_throttle_celsius=85, cpu_temp_halt_celsius=95)
        manager = ThermalManager(config)

        # 온도 읽기 모킹: 87°C
        with patch.object(manager, "_try_read_temperature", return_value=87.0):
            manager._temp_reader_available = None  # 재시도 허용
            status = manager.get_status()
            assert status.state == ThermalState.THROTTLED
            assert status.cpu_temp_celsius == 87.0

    def test_state_halted_when_temp_above_halt(self) -> None:
        """온도가 정지 임계값 이상이면 HALTED 상태이다."""
        config = _make_config(cpu_temp_throttle_celsius=85, cpu_temp_halt_celsius=95)
        manager = ThermalManager(config)

        # 온도 읽기 모킹: 97°C
        with patch.object(manager, "_try_read_temperature", return_value=97.0):
            manager._temp_reader_available = None
            status = manager.get_status()
            assert status.state == ThermalState.HALTED
            assert status.cpu_temp_celsius == 97.0

    def test_state_normal_when_temp_below_throttle(self) -> None:
        """온도가 스로틀 임계값 미만이면 NORMAL 상태이다."""
        config = _make_config(cpu_temp_throttle_celsius=85)
        manager = ThermalManager(config)

        with patch.object(manager, "_try_read_temperature", return_value=70.0):
            manager._temp_reader_available = None
            status = manager.get_status()
            assert status.state == ThermalState.NORMAL

    @pytest.mark.asyncio
    async def test_throttle_adds_delay(self) -> None:
        """온도가 스로틀 구간이면 추가 대기가 발생한다."""
        config = _make_config(
            cpu_temp_throttle_celsius=85,
            cpu_temp_halt_celsius=95,
            cooldown_seconds=90,  # 1/3 = 30초 추가 대기
        )
        manager = ThermalManager(config)

        # 온도 읽기 모킹: 88°C (스로틀 구간)
        with patch.object(manager, "_try_read_temperature", return_value=88.0):
            manager._temp_reader_available = None
            with patch(_SLEEP_PATCH, new_callable=AsyncMock) as mock_sleep:
                await manager._wait_for_safe_temperature()
                # 쿨다운 시간의 1/3 대기
                mock_sleep.assert_called_once_with(30)

    @pytest.mark.asyncio
    async def test_halt_waits_until_temp_drops(self) -> None:
        """온도가 정지 구간이면 안전 온도까지 대기한다."""
        config = _make_config(
            cpu_temp_throttle_celsius=85,
            cpu_temp_halt_celsius=95,
        )
        manager = ThermalManager(config)

        # 처음에 96°C, 그 다음에 80°C로 떨어짐
        temp_sequence = [96.0, 80.0]
        call_count = 0

        def mock_read_temp() -> float:
            nonlocal call_count
            idx = min(call_count, len(temp_sequence) - 1)
            result = temp_sequence[idx]
            call_count += 1
            return result

        with patch.object(manager, "_try_read_temperature", side_effect=mock_read_temp):
            manager._temp_reader_available = None
            with patch(_SLEEP_PATCH, new_callable=AsyncMock):
                await manager._wait_for_safe_temperature()
                # 온도 읽기가 최소 2번 호출됨 (최초 + 루프 내)
                assert call_count >= 2

    @pytest.mark.asyncio
    async def test_no_delay_when_temp_unavailable(self) -> None:
        """온도 읽기 불가 시 추가 대기 없이 즉시 반환한다."""
        manager = ThermalManager(_make_config())

        # _try_read_temperature가 None을 반환 → 대기 없음
        with patch(_SLEEP_PATCH, new_callable=AsyncMock) as mock_sleep:
            await manager._wait_for_safe_temperature()
            mock_sleep.assert_not_called()

    def test_temp_reader_disabled_after_first_failure(self) -> None:
        """첫 번째 온도 읽기 실패 후 재시도하지 않는다."""
        manager = ThermalManager(_make_config())

        # 첫 번째 호출: None 반환 → _temp_reader_available = False
        temp1 = manager._read_cpu_temperature()
        assert temp1 is None
        assert manager._temp_reader_available is False

        # 두 번째 호출: _try_read_temperature가 호출되지 않음
        with patch.object(manager, "_try_read_temperature") as mock_try:
            temp2 = manager._read_cpu_temperature()
            assert temp2 is None
            mock_try.assert_not_called()

    def test_temp_reader_enabled_on_success(self) -> None:
        """온도 읽기 성공 시 _temp_reader_available이 True로 설정된다."""
        manager = ThermalManager(_make_config())

        with patch.object(manager, "_try_read_temperature", return_value=65.0):
            manager._temp_reader_available = None  # 미결정 상태
            temp = manager._read_cpu_temperature()
            assert temp == 65.0
            assert manager._temp_reader_available is True


# === 콜백 테스트 ===


class TestCallbacks:
    """콜백 알림 관련 테스트."""

    def test_register_callback(self) -> None:
        """콜백을 등록할 수 있다."""
        manager = ThermalManager(_make_config())
        callback = MagicMock()

        manager.register_callback(callback)
        assert callback in manager._callbacks

    def test_unregister_callback(self) -> None:
        """등록된 콜백을 제거할 수 있다."""
        manager = ThermalManager(_make_config())
        callback = MagicMock()

        manager.register_callback(callback)
        manager.unregister_callback(callback)
        assert callback not in manager._callbacks

    def test_unregister_nonexistent_callback_no_error(self) -> None:
        """등록되지 않은 콜백 제거 시 에러가 발생하지 않는다."""
        manager = ThermalManager(_make_config())
        callback = MagicMock()

        # 등록하지 않은 콜백 제거 시도 - 에러 없이 경고만
        manager.unregister_callback(callback)

    @pytest.mark.asyncio
    async def test_callback_called_on_cooldown_start_and_end(self) -> None:
        """쿨다운 시작과 완료 시 콜백이 각각 호출된다."""
        config = _make_config(batch_size=1)
        manager = ThermalManager(config)
        callback = MagicMock()
        manager.register_callback(callback)

        with patch(_SLEEP_PATCH, new_callable=AsyncMock):
            await manager.notify_job_completed()

        # 쿨다운 시작과 완료 시 각각 1번씩 호출
        assert callback.call_count == 2

    @pytest.mark.asyncio
    async def test_callback_receives_thermal_status(self) -> None:
        """콜백에 ThermalStatus가 전달된다."""
        config = _make_config(batch_size=1)
        manager = ThermalManager(config)
        received_statuses: list[ThermalStatus] = []

        def track_status(status: ThermalStatus) -> None:
            received_statuses.append(status)

        manager.register_callback(track_status)

        with patch(_SLEEP_PATCH, new_callable=AsyncMock):
            await manager.notify_job_completed()

        assert len(received_statuses) == 2
        # 첫 번째: 쿨다운 시작 (COOLING)
        assert received_statuses[0].state == ThermalState.COOLING
        # 두 번째: 쿨다운 완료 (NORMAL)
        assert received_statuses[1].state == ThermalState.NORMAL

    @pytest.mark.asyncio
    async def test_callback_error_does_not_break_operation(self) -> None:
        """콜백에서 에러가 발생해도 동작이 중단되지 않는다."""
        config = _make_config(batch_size=1)
        manager = ThermalManager(config)

        # 에러를 발생시키는 콜백
        bad_callback = MagicMock(side_effect=RuntimeError("콜백 에러"))
        good_callback = MagicMock()

        manager.register_callback(bad_callback)
        manager.register_callback(good_callback)

        with patch(_SLEEP_PATCH, new_callable=AsyncMock):
            # 에러에도 불구하고 정상 완료
            await manager.notify_job_completed()

        bad_callback.assert_called()
        good_callback.assert_called()


# === 상태 조회 테스트 ===


class TestGetStatus:
    """상태 조회 관련 테스트."""

    def test_status_reflects_batch_count(self) -> None:
        """상태에 현재 배치 카운트가 반영된다."""
        manager = ThermalManager(_make_config(batch_size=5))
        manager._batch_count = 3
        manager._total_jobs_processed = 10

        status = manager.get_status()
        assert status.batch_count == 3
        assert status.batch_limit == 5
        assert status.total_jobs_processed == 10

    def test_status_shows_cooldown_remaining(self) -> None:
        """쿨다운 중 남은 시간이 표시된다."""
        config = _make_config(cooldown_seconds=60)
        manager = ThermalManager(config)
        manager._cooling = True
        manager._cooldown_start_time = time.monotonic() - 20  # 20초 경과

        status = manager.get_status()
        assert status.state == ThermalState.COOLING
        # 60 - 20 = 약 40초 남음 (타이밍 허용 범위)
        assert 35.0 <= status.cooldown_remaining_seconds <= 41.0


# === 리셋 테스트 ===


class TestReset:
    """서멀 매니저 리셋 관련 테스트."""

    @pytest.mark.asyncio
    async def test_reset_clears_all_state(self) -> None:
        """reset 호출 시 모든 상태가 초기화된다."""
        config = _make_config(batch_size=3)
        manager = ThermalManager(config)

        with patch(_SLEEP_PATCH, new_callable=AsyncMock):
            # 상태 변경
            await manager.notify_job_completed()
            await manager.notify_job_completed()
            assert manager.batch_count == 2
            assert manager.total_jobs_processed == 2

        # 리셋
        manager.reset()

        assert manager.batch_count == 0
        assert manager.total_jobs_processed == 0
        assert manager.is_cooling is False


# === 통합 시나리오 테스트 ===


class TestIntegrationScenarios:
    """실제 사용 시나리오를 재현하는 통합 테스트."""

    @pytest.mark.asyncio
    async def test_typical_workflow_batch_2_cooldown(self) -> None:
        """일반적인 워크플로우: 2건 처리 → 쿨다운 → 2건 처리."""
        config = _make_config(batch_size=2)
        manager = ThermalManager(config)

        with patch(_SLEEP_PATCH, new_callable=AsyncMock):
            # 첫 번째 배치
            await manager.notify_job_started()
            await manager.notify_job_completed()
            assert manager.batch_count == 1

            await manager.notify_job_started()
            await manager.notify_job_completed()
            # 배치 한도 도달 → 쿨다운 → 리셋
            assert manager.batch_count == 0

            # 두 번째 배치
            await manager.wait_if_needed()
            await manager.notify_job_started()
            await manager.notify_job_completed()
            assert manager.batch_count == 1
            assert manager.total_jobs_processed == 3

    @pytest.mark.asyncio
    async def test_temp_throttle_before_job_start(self) -> None:
        """작업 시작 전 온도 확인으로 스로틀이 동작한다."""
        config = _make_config(
            cpu_temp_throttle_celsius=85,
            cpu_temp_halt_celsius=95,
            cooldown_seconds=90,
        )
        manager = ThermalManager(config)

        # 온도가 87°C (스로틀 구간)
        with patch.object(manager, "_try_read_temperature", return_value=87.0):
            manager._temp_reader_available = None
            with patch(_SLEEP_PATCH, new_callable=AsyncMock) as mock_sleep:
                await manager.notify_job_started()
                # 쿨다운 시간의 1/3 = 30초 대기
                mock_sleep.assert_called_once_with(30)

    @pytest.mark.asyncio
    async def test_emergency_halt_recovery(self) -> None:
        """긴급 정지 후 온도 하강 시 복구된다."""
        config = _make_config(
            cpu_temp_throttle_celsius=85,
            cpu_temp_halt_celsius=95,
        )
        manager = ThermalManager(config)

        # 온도 시퀀스: 98°C → 90°C → 80°C
        temps = [98.0, 90.0, 80.0]
        call_idx = 0

        def mock_temp() -> float:
            nonlocal call_idx
            idx = min(call_idx, len(temps) - 1)
            result = temps[idx]
            call_idx += 1
            return result

        with patch.object(manager, "_try_read_temperature", side_effect=mock_temp):
            manager._temp_reader_available = None
            with patch(_SLEEP_PATCH, new_callable=AsyncMock):
                await manager.wait_if_needed()
                # 정상적으로 완료 (80°C < 85°C)

    @pytest.mark.asyncio
    async def test_max_wait_timeout_on_persistent_high_temp(self) -> None:
        """지속적으로 높은 온도에서 최대 대기 시간 초과 시 작업을 재개한다."""
        config = _make_config(
            cpu_temp_throttle_celsius=85,
            cpu_temp_halt_celsius=95,
            cooldown_seconds=30,  # 최대 대기 = 30 * 3 = 90초
        )
        manager = ThermalManager(config)

        # 온도가 계속 96°C로 유지
        with patch.object(manager, "_try_read_temperature", return_value=96.0):
            manager._temp_reader_available = None
            with patch(_SLEEP_PATCH, new_callable=AsyncMock):
                # 최대 대기 시간 후 반환 (무한 대기 방지)
                await manager._wait_for_safe_temperature()

    @pytest.mark.asyncio
    async def test_concurrent_wait_if_needed(self) -> None:
        """여러 태스크가 동시에 wait_if_needed를 호출해도 안전하다."""
        config = _make_config(batch_size=2)
        manager = ThermalManager(config)

        with patch(_SLEEP_PATCH, new_callable=AsyncMock):
            # 여러 코루틴이 동시에 대기
            results = await asyncio.gather(
                manager.wait_if_needed(),
                manager.wait_if_needed(),
                return_exceptions=True,
            )

            # 에러 없이 완료
            assert all(r is None for r in results)

    @pytest.mark.asyncio
    async def test_temp_reading_failure_mid_halt(self) -> None:
        """긴급 정지 중 온도 읽기가 실패하면 기본 쿨다운 시간만큼 대기 후 반환한다."""
        config = _make_config(
            cpu_temp_throttle_celsius=85,
            cpu_temp_halt_celsius=95,
        )
        manager = ThermalManager(config)

        # 처음 96°C → 이후 읽기 실패 (None)
        call_idx = 0

        def mock_temp():
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return 96.0
            return None

        with patch.object(manager, "_try_read_temperature", side_effect=mock_temp):
            manager._temp_reader_available = None
            with patch(_SLEEP_PATCH, new_callable=AsyncMock) as mock_sleep:
                await manager._wait_for_safe_temperature()
                # sleep이 호출됨 (긴급 정지 대기 + 폴백 대기)
                assert mock_sleep.call_count >= 1
