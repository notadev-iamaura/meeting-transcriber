"""
Zoom 프로세스 감지기 테스트 모듈 (Zoom Detector Test Module)

목적: ZoomDetector의 프로세스 감지, 이벤트 발행, 콜백 호출,
     에러 처리 등 전체 기능을 검증한다.
주요 테스트:
    - 프로세스 감지 (pgrep 실행 결과 해석)
    - 미팅 시작/종료 이벤트 발행
    - 동기/비동기 콜백 호출
    - 중복 이벤트 방지
    - 에러 처리 (pgrep 실패, 타임아웃 등)
    - 감지 루프 시작/중지
    - config 설정 적용
의존성: pytest, pytest-asyncio, steps/zoom_detector.py
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from steps.zoom_detector import (
    AlreadyRunningError,
    ProcessCheckError,
    ZoomDetector,
    ZoomDetectorError,
)



# === 테스트 픽스처 ===


def _make_config() -> MagicMock:
    """테스트용 설정 목 객체를 생성한다."""
    config = MagicMock()
    config.zoom.process_name = "CptHost"
    config.zoom.poll_interval_seconds = 1  # 테스트에서는 빠르게 폴링
    return config


@pytest_asyncio.fixture
async def detector() -> ZoomDetector:
    """테스트용 ZoomDetector 인스턴스를 생성한다."""
    config = _make_config()
    det = ZoomDetector(config=config)
    yield det
    # 테스트 후 정리
    if det.is_running:
        await det.stop()


# === 초기화 테스트 ===


class TestZoomDetectorInit:
    """ZoomDetector 초기화 관련 테스트."""

    def test_기본_초기화(self) -> None:
        """설정에서 process_name과 poll_interval을 올바르게 로드하는지 검증."""
        config = _make_config()
        det = ZoomDetector(config=config)

        assert det._process_name == "CptHost"
        assert det._poll_interval == 1
        assert det.is_meeting_active is False
        assert det.is_running is False

    def test_커스텀_설정_적용(self) -> None:
        """커스텀 프로세스명/폴링 간격이 적용되는지 검증."""
        config = _make_config()
        config.zoom.process_name = "CustomZoom"
        config.zoom.poll_interval_seconds = 10

        det = ZoomDetector(config=config)

        assert det._process_name == "CustomZoom"
        assert det._poll_interval == 10

    def test_이벤트_초기_상태(self) -> None:
        """이벤트가 초기에 설정되지 않은 상태인지 검증."""
        config = _make_config()
        det = ZoomDetector(config=config)

        assert not det.meeting_started_event.is_set()
        assert not det.meeting_ended_event.is_set()


# === 프로세스 확인 테스트 ===


class TestProcessCheck:
    """pgrep 기반 프로세스 확인 관련 테스트."""

    @pytest.mark.asyncio
    async def test_프로세스_존재_시_True(self, detector: ZoomDetector) -> None:
        """pgrep이 returncode 0을 반환하면 True를 반환하는지 검증."""
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await detector._check_zoom_process()

        assert result is True

    @pytest.mark.asyncio
    async def test_프로세스_미존재_시_False(self, detector: ZoomDetector) -> None:
        """pgrep이 returncode 1을 반환하면 False를 반환하는지 검증."""
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await detector._check_zoom_process()

        assert result is False

    @pytest.mark.asyncio
    async def test_pgrep_올바른_인자(self, detector: ZoomDetector) -> None:
        """pgrep이 올바른 인자로 호출되는지 검증."""
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await detector._check_zoom_process()

        mock_exec.assert_called_once_with(
            "pgrep",
            "-f",
            "CptHost",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    @pytest.mark.asyncio
    async def test_pgrep_미설치_시_에러(self, detector: ZoomDetector) -> None:
        """pgrep 명령이 없을 때 ProcessCheckError가 발생하는지 검증."""
        with (
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=FileNotFoundError("pgrep not found"),
            ),
            pytest.raises(ProcessCheckError, match="pgrep 명령을 찾을 수 없습니다"),
        ):
            await detector._check_zoom_process()

    @pytest.mark.asyncio
    async def test_OS_에러_시_에러(self, detector: ZoomDetector) -> None:
        """OSError 발생 시 ProcessCheckError로 래핑되는지 검증."""
        with (
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=OSError("permission denied"),
            ),
            pytest.raises(ProcessCheckError, match="OS 에러"),
        ):
            await detector._check_zoom_process()

    @pytest.mark.asyncio
    async def test_타임아웃_시_이전_상태_유지(self, detector: ZoomDetector) -> None:
        """pgrep 타임아웃 시 이전 상태를 유지하는지 검증."""
        detector._is_meeting_active = True

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(side_effect=TimeoutError())

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await detector._check_zoom_process()

        # 이전 상태(True) 유지
        assert result is True

    @pytest.mark.asyncio
    async def test_타임아웃_시_이전_상태_False_유지(self, detector: ZoomDetector) -> None:
        """이전 상태가 False일 때 타임아웃 시 False를 유지하는지 검증."""
        detector._is_meeting_active = False

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(side_effect=TimeoutError())

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await detector._check_zoom_process()

        assert result is False


# === 상태 변화 처리 테스트 ===


class TestStateChange:
    """미팅 상태 변화 감지 및 이벤트 발행 테스트."""

    @pytest.mark.asyncio
    async def test_미팅_시작_이벤트(self, detector: ZoomDetector) -> None:
        """미팅이 시작되면 started 이벤트가 set 되는지 검증."""
        await detector._handle_state_change(True)

        assert detector.is_meeting_active is True
        assert detector.meeting_started_event.is_set()
        assert not detector.meeting_ended_event.is_set()

    @pytest.mark.asyncio
    async def test_미팅_종료_이벤트(self, detector: ZoomDetector) -> None:
        """미팅이 종료되면 ended 이벤트가 set 되는지 검증."""
        # 먼저 미팅 시작 상태로 전환
        detector._is_meeting_active = True
        detector.meeting_started_event.set()

        await detector._handle_state_change(False)

        assert detector.is_meeting_active is False
        assert not detector.meeting_started_event.is_set()
        assert detector.meeting_ended_event.is_set()

    @pytest.mark.asyncio
    async def test_중복_이벤트_방지_시작(self, detector: ZoomDetector) -> None:
        """이미 시작 상태에서 다시 시작 감지 시 콜백이 호출되지 않는지 검증."""
        callback = MagicMock()
        detector.on_meeting_change(callback)

        # 미팅 시작
        await detector._handle_state_change(True)
        assert callback.call_count == 1

        # 같은 상태로 다시 호출 — 콜백 추가 호출 없어야 함
        await detector._handle_state_change(True)
        assert callback.call_count == 1

    @pytest.mark.asyncio
    async def test_중복_이벤트_방지_종료(self, detector: ZoomDetector) -> None:
        """이미 종료 상태에서 다시 종료 감지 시 콜백이 호출되지 않는지 검증."""
        callback = MagicMock()
        detector.on_meeting_change(callback)

        # 초기 상태 = False, 다시 False
        await detector._handle_state_change(False)
        assert callback.call_count == 0  # 상태 변화 없음

    @pytest.mark.asyncio
    async def test_시작_후_종료_순서(self, detector: ZoomDetector) -> None:
        """미팅 시작 → 종료 순서대로 이벤트가 발행되는지 검증."""
        states: list[bool] = []
        callback = MagicMock(side_effect=lambda x: states.append(x))
        detector.on_meeting_change(callback)

        await detector._handle_state_change(True)
        await detector._handle_state_change(False)

        assert states == [True, False]
        assert callback.call_count == 2


# === 콜백 테스트 ===


class TestCallbacks:
    """동기/비동기 콜백 등록 및 호출 테스트."""

    @pytest.mark.asyncio
    async def test_동기_콜백_호출(self, detector: ZoomDetector) -> None:
        """동기 콜백이 올바르게 호출되는지 검증."""
        callback = MagicMock()
        detector.on_meeting_change(callback)

        await detector._handle_state_change(True)

        callback.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_비동기_콜백_호출(self, detector: ZoomDetector) -> None:
        """비동기 콜백이 올바르게 호출되는지 검증."""
        callback = AsyncMock()
        detector.on_meeting_change(callback)

        await detector._handle_state_change(True)

        callback.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_다중_콜백_호출(self, detector: ZoomDetector) -> None:
        """여러 개의 콜백이 모두 호출되는지 검증."""
        sync_cb = MagicMock()
        async_cb = AsyncMock()
        detector.on_meeting_change(sync_cb)
        detector.on_meeting_change(async_cb)

        await detector._handle_state_change(True)

        sync_cb.assert_called_once_with(True)
        async_cb.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_콜백_에러_격리(self, detector: ZoomDetector) -> None:
        """하나의 콜백 에러가 다른 콜백에 영향 주지 않는지 검증."""
        error_cb = MagicMock(side_effect=RuntimeError("콜백 에러"))
        normal_cb = MagicMock()

        detector.on_meeting_change(error_cb)
        detector.on_meeting_change(normal_cb)

        # 에러 콜백이 있어도 다른 콜백은 정상 호출되어야 함
        await detector._handle_state_change(True)

        error_cb.assert_called_once_with(True)
        normal_cb.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_비동기_콜백_에러_격리(self, detector: ZoomDetector) -> None:
        """비동기 콜백 에러가 다른 콜백에 영향 주지 않는지 검증."""
        error_cb = AsyncMock(side_effect=RuntimeError("비동기 에러"))
        normal_cb = AsyncMock()

        detector.on_meeting_change(error_cb)
        detector.on_meeting_change(normal_cb)

        await detector._handle_state_change(True)

        error_cb.assert_called_once_with(True)
        normal_cb.assert_called_once_with(True)


# === 감지 루프 시작/중지 테스트 ===


class TestStartStop:
    """감지기 시작/중지 관련 테스트."""

    @pytest.mark.asyncio
    async def test_시작_후_실행_상태(self, detector: ZoomDetector) -> None:
        """start() 호출 후 is_running이 True인지 검증."""
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await detector.start()

        assert detector.is_running is True

        await detector.stop()

    @pytest.mark.asyncio
    async def test_중지_후_상태(self, detector: ZoomDetector) -> None:
        """stop() 호출 후 is_running이 False인지 검증."""
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await detector.start()

        await detector.stop()
        assert detector.is_running is False

    @pytest.mark.asyncio
    async def test_이중_시작_방지(self, detector: ZoomDetector) -> None:
        """이미 실행 중일 때 start()가 AlreadyRunningError를 발생시키는지 검증."""
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await detector.start()

        with pytest.raises(AlreadyRunningError, match="이미 실행 중"):
            await detector.start()

        await detector.stop()

    @pytest.mark.asyncio
    async def test_이중_중지_안전(self, detector: ZoomDetector) -> None:
        """이미 중지된 상태에서 stop()이 에러 없이 처리되는지 검증."""
        # 이미 중지 상태 — 에러 없이 반환
        await detector.stop()
        assert detector.is_running is False

    @pytest.mark.asyncio
    async def test_시작_시_초기_상태_확인_미팅_없음(self, detector: ZoomDetector) -> None:
        """시작 시 미팅이 없으면 ended 이벤트가 set 되는지 검증."""
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await detector.start()

        assert detector.is_meeting_active is False
        assert detector.meeting_ended_event.is_set()

        await detector.stop()

    @pytest.mark.asyncio
    async def test_시작_시_초기_상태_확인_미팅_중(self, detector: ZoomDetector) -> None:
        """시작 시 미팅이 진행 중이면 started 이벤트가 set 되는지 검증."""
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await detector.start()

        assert detector.is_meeting_active is True
        assert detector.meeting_started_event.is_set()

        await detector.stop()

    @pytest.mark.asyncio
    async def test_시작_시_이미_미팅_중이면_시작_콜백이_호출된다(
        self, detector: ZoomDetector
    ) -> None:
        """회귀 방지: 앱 시작 시 Zoom 회의가 이미 진행 중이면
        on_meeting_change 콜백이 is_active=True 로 호출되어야 한다.

        버그 시나리오:
            1. Zoom 회의 진행 중 사용자가 앱 실행
            2. ZoomDetector.start() 가 _is_meeting_active=True 만 설정하고
               _notify_callbacks 를 호출하지 않음
            3. _poll_loop 의 _handle_state_change 도 단락 (현재 상태와 일치) →
               콜백 영원히 호출 안 됨
            4. 결과: 자동 녹음이 시작되지 않고, Zoom 종료 시 "녹음 중이 아님" 경고
        """
        callback_calls: list[bool] = []

        def on_change(is_active: bool) -> None:
            callback_calls.append(is_active)

        detector.on_meeting_change(on_change)

        # Zoom 프로세스가 이미 실행 중인 상태 시뮬레이션
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await detector.start()

        # 폴링 첫 사이클이 단락되기 전에 콜백이 발화돼야 한다 (start() 내부에서)
        assert callback_calls == [True], (
            f"start() 시점에 미팅 시작 콜백이 호출되지 않음. 호출 기록: {callback_calls}"
        )
        assert detector.is_meeting_active is True
        assert detector.meeting_started_event.is_set()

        await detector.stop()

    @pytest.mark.asyncio
    async def test_시작_시_async_콜백도_정상_호출된다(
        self, detector: ZoomDetector
    ) -> None:
        """async 콜백 (api/server.py 의 _on_zoom_meeting_change 와 동일 형태) 도
        start() 안에서 정상 await 되는지 검증.

        실 환경에서 등록되는 콜백은 async 이므로 sync 콜백만 검증하면 부족하다.
        _notify_callbacks 의 sync/async 분기 양쪽을 모두 회귀 방지 대상으로 둔다.
        """
        async_calls: list[bool] = []

        async def async_cb(is_active: bool) -> None:
            async_calls.append(is_active)

        detector.on_meeting_change(async_cb)

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await detector.start()

        assert async_calls == [True], (
            f"async 콜백이 발화되지 않음: {async_calls}"
        )

        await detector.stop()

    @pytest.mark.asyncio
    async def test_시작_시_콜백_예외가_start를_막지_않는다(
        self, detector: ZoomDetector
    ) -> None:
        """콜백 안에서 예외가 발생해도 start() 가 정상 완료되고
        polling 태스크가 생성되어야 한다 (실 환경에서 recorder 시작 실패 시나리오).
        """

        async def failing_cb(is_active: bool) -> None:
            raise RuntimeError("recorder.start_recording 시뮬 실패")

        detector.on_meeting_change(failing_cb)

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            # 콜백 예외가 발생해도 start() 자체는 성공해야 함
            await detector.start()

        # 상태와 polling 태스크가 정상 생성됐는지 확인
        assert detector.is_meeting_active is True
        assert detector.is_running is True
        assert detector._poll_task is not None
        assert not detector._poll_task.done()

        await detector.stop()

    @pytest.mark.asyncio
    async def test_시작_시_recorder_와의_통합_플로우(
        self, detector: ZoomDetector
    ) -> None:
        """api/server.py 의 _on_zoom_meeting_change 와 동일한 시그니처의
        async 콜백이 ZoomDetector.start() 에서 한 번 발화되고, recorder mock 의
        start_recording 이 정확히 1회 호출되는지 검증한다.

        이 테스트가 통과하면 lifespan 안에서:
            recorder = AudioRecorder(...)
            zoom_detector.on_meeting_change(_on_zoom_meeting_change)  # async
            await zoom_detector.start()
        호출만으로 자동 녹음이 트리거됨을 보장할 수 있다.
        """
        recorder_mock = AsyncMock()
        recorder_mock.start_recording = AsyncMock()
        recorder_mock.stop_recording = AsyncMock()

        async def _on_zoom_meeting_change(is_active: bool) -> None:
            if is_active:
                await recorder_mock.start_recording()
            else:
                await recorder_mock.stop_recording()

        detector.on_meeting_change(_on_zoom_meeting_change)

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await detector.start()

        # 정확히 1회 호출 (start() 안에서) - polling 첫 사이클은 단락되어야 함
        recorder_mock.start_recording.assert_awaited_once()
        recorder_mock.stop_recording.assert_not_awaited()

        await detector.stop()

    @pytest.mark.asyncio
    async def test_시작_시_미팅_없으면_콜백_호출_안함(
        self, detector: ZoomDetector
    ) -> None:
        """대칭 검증: 초기에 미팅이 없으면 콜백이 호출되지 않는다 (False→False 전이 없음)."""
        callback_calls: list[bool] = []

        def on_change(is_active: bool) -> None:
            callback_calls.append(is_active)

        detector.on_meeting_change(on_change)

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)  # pgrep not found

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await detector.start()

        assert callback_calls == [], (
            f"미팅 없는 초기 상태에서 콜백이 호출되면 안 됨: {callback_calls}"
        )
        assert detector.meeting_ended_event.is_set()

        await detector.stop()

    @pytest.mark.asyncio
    async def test_stop_후_재시작_시_콜백이_다시_호출된다(
        self, detector: ZoomDetector
    ) -> None:
        """회귀 방지: stop() 후 같은 detector 인스턴스로 start() 를 재호출하면
        시작 콜백이 다시 정상 발화되어야 한다.

        잠재 버그:
            stop() 이 _is_meeting_active 를 리셋하지 않으면, 재시작 시점에
            _handle_state_change(True) 가 단락되어 콜백 미호출.
            (사용자 보고 버그와 동일 패턴 — 다른 시나리오)
        """
        calls: list[bool] = []

        async def cb(active: bool) -> None:
            calls.append(active)

        detector.on_meeting_change(cb)

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await detector.start()
            await detector.stop()
            await detector.start()

        # 첫 start() + 재시작 → 콜백이 두 번 호출돼야 함
        assert calls == [True, True], (
            f"stop()→start() 재시작 시 시작 콜백이 발화되지 않음: {calls}"
        )
        # stop 후 상태가 리셋됐는지 확인
        assert detector.is_meeting_active is True  # 재시작 후 다시 True

        await detector.stop()
        # 마지막 stop 후 상태 리셋 확인
        assert detector.is_meeting_active is False
        assert not detector.meeting_started_event.is_set()

    @pytest.mark.asyncio
    async def test_시작_시_프로세스_확인_실패_계속_진행(self, detector: ZoomDetector) -> None:
        """초기 프로세스 확인 실패해도 감지기가 시작되는지 검증."""
        call_count = 0

        async def mock_exec(*args: object, **kwargs: object) -> AsyncMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise FileNotFoundError("pgrep not found")
            mock_p = AsyncMock()
            mock_p.wait = AsyncMock(return_value=1)
            return mock_p

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            # ProcessCheckError 발생하지만 start() 자체는 성공해야 함
            await detector.start()

        assert detector.is_running is True
        await detector.stop()


# === 폴링 루프 테스트 ===


class TestPollLoop:
    """폴링 루프 동작 관련 테스트."""

    @pytest.mark.asyncio
    async def test_폴링으로_미팅_시작_감지(self, detector: ZoomDetector) -> None:
        """폴링 루프가 미팅 시작을 감지하는지 검증."""
        call_count = 0

        async def mock_exec(*args: object, **kwargs: object) -> AsyncMock:
            nonlocal call_count
            call_count += 1
            mock_p = AsyncMock()
            # 첫 번째(초기): 미팅 없음, 두 번째(폴링): 미팅 시작
            if call_count <= 1:
                mock_p.wait = AsyncMock(return_value=1)
            else:
                mock_p.wait = AsyncMock(return_value=0)
            return mock_p

        callback = MagicMock()
        detector.on_meeting_change(callback)
        detector._poll_interval = 0.1  # 빠른 테스트

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            await detector.start()
            # 폴링이 미팅 시작을 감지할 시간을 줌
            await asyncio.sleep(0.3)
            await detector.stop()

        # 콜백이 True(미팅 시작)로 호출되었는지 확인
        callback.assert_called_with(True)

    @pytest.mark.asyncio
    async def test_폴링으로_미팅_종료_감지(self, detector: ZoomDetector) -> None:
        """폴링 루프가 미팅 종료를 감지하는지 검증."""
        call_count = 0

        async def mock_exec(*args: object, **kwargs: object) -> AsyncMock:
            nonlocal call_count
            call_count += 1
            mock_p = AsyncMock()
            # 첫 번째(초기): 미팅 중, 두 번째(폴링): 미팅 종료
            if call_count <= 1:
                mock_p.wait = AsyncMock(return_value=0)
            else:
                mock_p.wait = AsyncMock(return_value=1)
            return mock_p

        states: list[bool] = []
        callback = MagicMock(side_effect=lambda x: states.append(x))
        detector.on_meeting_change(callback)
        detector._poll_interval = 0.1

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            await detector.start()
            await asyncio.sleep(0.3)
            await detector.stop()

        # 종료 이벤트 (False)가 콜백에 전달되었는지 확인
        assert False in states

    @pytest.mark.asyncio
    async def test_프로세스_확인_실패_시_루프_계속(self, detector: ZoomDetector) -> None:
        """pgrep 실패 시에도 폴링 루프가 중단되지 않는지 검증."""
        call_count = 0

        async def mock_exec(*args: object, **kwargs: object) -> AsyncMock:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                # 두 번째 호출에서 에러
                raise OSError("temporary failure")
            mock_p = AsyncMock()
            mock_p.wait = AsyncMock(return_value=1)
            return mock_p

        detector._poll_interval = 0.1

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            await detector.start()
            await asyncio.sleep(0.5)  # 여러 번 폴링
            await detector.stop()

        # 에러 이후에도 계속 폴링했으므로 호출 횟수 > 2
        assert call_count >= 3

    @pytest.mark.asyncio
    async def test_stop_후_폴링_중단(self, detector: ZoomDetector) -> None:
        """stop() 후 폴링이 즉시 중단되는지 검증."""
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)

        detector._poll_interval = 0.1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await detector.start()
            await asyncio.sleep(0.2)
            await detector.stop()

        assert detector._poll_task is None


# === check_once 테스트 ===


class TestCheckOnce:
    """1회성 상태 확인 테스트."""

    @pytest.mark.asyncio
    async def test_미팅_진행_중_확인(self, detector: ZoomDetector) -> None:
        """check_once가 미팅 진행 중이면 True를 반환하는지 검증."""
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await detector.check_once()

        assert result is True
        assert detector.is_meeting_active is True

    @pytest.mark.asyncio
    async def test_미팅_없음_확인(self, detector: ZoomDetector) -> None:
        """check_once가 미팅 없으면 False를 반환하는지 검증."""
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await detector.check_once()

        assert result is False
        assert detector.is_meeting_active is False

    @pytest.mark.asyncio
    async def test_상태_변화_시_이벤트_발행(self, detector: ZoomDetector) -> None:
        """check_once에서 상태가 변하면 이벤트가 발행되는지 검증."""
        callback = MagicMock()
        detector.on_meeting_change(callback)

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await detector.check_once()

        callback.assert_called_once_with(True)
        assert detector.meeting_started_event.is_set()


# === reset_events 테스트 ===


class TestResetEvents:
    """이벤트 초기화 테스트."""

    @pytest.mark.asyncio
    async def test_이벤트_초기화(self, detector: ZoomDetector) -> None:
        """reset_events가 모든 이벤트와 상태를 초기화하는지 검증."""
        # 상태를 설정
        detector._is_meeting_active = True
        detector.meeting_started_event.set()
        detector.meeting_ended_event.set()

        detector.reset_events()

        assert detector.is_meeting_active is False
        assert not detector.meeting_started_event.is_set()
        assert not detector.meeting_ended_event.is_set()


# === 에러 계층 테스트 ===


class TestErrorHierarchy:
    """에러 클래스 계층 구조 테스트."""

    def test_ZoomDetectorError_기본_클래스(self) -> None:
        """ZoomDetectorError가 Exception의 하위 클래스인지 검증."""
        assert issubclass(ZoomDetectorError, Exception)

    def test_ProcessCheckError_상속(self) -> None:
        """ProcessCheckError가 ZoomDetectorError를 상속하는지 검증."""
        assert issubclass(ProcessCheckError, ZoomDetectorError)

    def test_AlreadyRunningError_상속(self) -> None:
        """AlreadyRunningError가 ZoomDetectorError를 상속하는지 검증."""
        assert issubclass(AlreadyRunningError, ZoomDetectorError)

    def test_ProcessCheckError_원본_에러_보존(self) -> None:
        """ProcessCheckError가 원본 에러를 보존하는지 검증."""
        original = OSError("disk error")
        err = ProcessCheckError("프로세스 확인 실패", original_error=original)

        assert err.original_error is original
        assert "프로세스 확인 실패" in str(err)


# === 커스텀 프로세스명 테스트 ===


class TestCustomProcessName:
    """커스텀 프로세스명 설정 테스트."""

    @pytest.mark.asyncio
    async def test_커스텀_프로세스명으로_pgrep_호출(self) -> None:
        """config에서 설정한 프로세스명으로 pgrep이 호출되는지 검증."""
        config = _make_config()
        config.zoom.process_name = "CustomProcess"
        det = ZoomDetector(config=config)

        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await det._check_zoom_process()

        # 커스텀 프로세스명이 전달되었는지 확인
        mock_exec.assert_called_once_with(
            "pgrep",
            "-f",
            "CustomProcess",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )


# === 통합 시나리오 테스트 ===


class TestIntegrationScenarios:
    """미팅 시작 → 진행 → 종료 전체 시나리오 테스트."""

    @pytest.mark.asyncio
    async def test_미팅_전체_라이프사이클(self, detector: ZoomDetector) -> None:
        """미팅 없음 → 시작 → 진행 → 종료 전체 흐름 검증."""
        call_count = 0

        async def mock_exec(*args: object, **kwargs: object) -> AsyncMock:
            nonlocal call_count
            call_count += 1
            mock_p = AsyncMock()
            # 초기: 없음(1), 폴1: 없음(1), 폴2: 시작(0), 폴3: 진행(0), 폴4: 종료(1)
            pattern = [1, 1, 0, 0, 1]
            idx = min(call_count - 1, len(pattern) - 1)
            mock_p.wait = AsyncMock(return_value=pattern[idx])
            return mock_p

        states: list[bool] = []
        callback = MagicMock(side_effect=lambda x: states.append(x))
        detector.on_meeting_change(callback)
        detector._poll_interval = 0.1

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            await detector.start()
            await asyncio.sleep(0.6)
            await detector.stop()

        # 시작(True) → 종료(False) 순서로 콜백 호출
        assert states == [True, False]

    @pytest.mark.asyncio
    async def test_여러_번_미팅_반복(self, detector: ZoomDetector) -> None:
        """미팅이 여러 번 시작/종료되는 시나리오 검증."""
        call_count = 0

        async def mock_exec(*args: object, **kwargs: object) -> AsyncMock:
            nonlocal call_count
            call_count += 1
            mock_p = AsyncMock()
            # 초기:없음, 시작, 종료, 다시시작, 다시종료
            pattern = [1, 0, 1, 0, 1]
            idx = min(call_count - 1, len(pattern) - 1)
            mock_p.wait = AsyncMock(return_value=pattern[idx])
            return mock_p

        states: list[bool] = []
        callback = MagicMock(side_effect=lambda x: states.append(x))
        detector.on_meeting_change(callback)
        detector._poll_interval = 0.1

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            await detector.start()
            await asyncio.sleep(0.6)
            await detector.stop()

        # 시작-종료-시작-종료 패턴
        assert states == [True, False, True, False]
