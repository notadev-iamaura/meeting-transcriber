"""
오디오 녹음 모듈 (Audio Recorder Module)

목적: ffmpeg를 사용하여 macOS에서 오디오를 녹음한다.
주요 기능:
    - ffmpeg + AVFoundation 기반 마이크 녹음
    - BlackHole 가상 오디오 디바이스 자동 감지 (시스템 오디오 캡처)
    - Zoom 감지 연동 자동 녹음 시작/정지
    - 녹음 파일을 recordings_temp → audio_input 이동 (FolderWatcher 연동)
    - 최대 녹음 시간 가드 (max_duration_seconds)
    - 비동기(async) 인터페이스
    - WebSocket 이벤트 브로드캐스트
의존성: asyncio, config 모듈
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional, Union

from config import AppConfig, get_config

logger = logging.getLogger(__name__)


# === 에러 계층 ===


class RecorderError(Exception):
    """녹음 처리 중 발생하는 에러의 기본 클래스."""


class AlreadyRecordingError(RecorderError):
    """이미 녹음 중일 때 녹음 시작 시도 시 발생한다."""


class FFmpegRecordError(RecorderError):
    """ffmpeg 녹음 프로세스 관련 에러가 발생할 때."""


class AudioDeviceError(RecorderError):
    """오디오 장치를 찾거나 사용할 수 없을 때 발생한다."""


# === 상태 및 결과 ===


class RecordingState(str, Enum):
    """녹음 상태를 정의하는 열거형.

    Attributes:
        IDLE: 대기 중
        RECORDING: 녹음 중
        STOPPING: 정지 중 (ffmpeg 종료 대기)
    """

    IDLE = "idle"
    RECORDING = "recording"
    STOPPING = "stopping"


@dataclass
class RecordingResult:
    """녹음 완료 결과를 담는 데이터 클래스.

    Attributes:
        file_path: 최종 저장된 파일 경로
        duration_seconds: 녹음 길이 (초)
        audio_device: 사용된 오디오 장치명
        started_at: 녹음 시작 시각 (ISO 형식)
        ended_at: 녹음 종료 시각 (ISO 형식)
        file_size_bytes: 파일 크기 (바이트)
    """

    file_path: Path
    duration_seconds: float
    audio_device: str
    started_at: str
    ended_at: str
    file_size_bytes: int


@dataclass
class AudioDevice:
    """오디오 장치 정보를 담는 데이터 클래스.

    Attributes:
        index: ffmpeg AVFoundation 장치 인덱스
        name: 장치 이름
        is_blackhole: BlackHole 가상 장치 여부
    """

    index: int
    name: str
    is_blackhole: bool = False

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다."""
        return {
            "index": self.index,
            "name": self.name,
            "is_blackhole": self.is_blackhole,
        }


# 콜백 타입 정의
SyncCallback = Callable[[RecordingResult], None]
AsyncCallback = Callable[[RecordingResult], Coroutine[Any, Any, None]]


class AudioRecorder:
    """ffmpeg AVFoundation 기반 오디오 녹음기.

    macOS의 AVFoundation을 통해 마이크 또는 BlackHole 가상 장치로
    오디오를 녹음한다. 녹음 파일은 recordings_temp에 임시 저장 후,
    완료 시 audio_input으로 이동하여 FolderWatcher가 감지하도록 한다.

    Args:
        config: 애플리케이션 설정 인스턴스 (None이면 싱글턴 사용)
        ws_manager: WebSocket ConnectionManager (이벤트 브로드캐스트용)

    사용 예시:
        recorder = AudioRecorder(config)
        result = await recorder.start_recording()
        # ... 녹음 진행 ...
        result = await recorder.stop_recording()
    """

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        ws_manager: Optional[Any] = None,
    ) -> None:
        """AudioRecorder를 초기화한다.

        Args:
            config: 애플리케이션 설정 (None이면 get_config() 사용)
            ws_manager: WebSocket 매니저 (None이면 이벤트 미전송)
        """
        self._config = config or get_config()
        self._ws_manager = ws_manager

        # 녹음 설정 캐시
        self._recording_config = self._config.recording
        self._sample_rate = self._recording_config.sample_rate
        self._channels = self._recording_config.channels
        self._max_duration = self._recording_config.max_duration_seconds
        self._min_duration = self._recording_config.min_duration_seconds
        self._graceful_timeout = self._recording_config.ffmpeg_graceful_timeout_seconds

        # 경로 설정
        self._temp_dir = self._config.paths.resolved_recordings_temp_dir
        self._audio_input_dir = self._config.paths.resolved_audio_input_dir

        # 상태
        self._state = RecordingState.IDLE
        self._process: Optional[asyncio.subprocess.Process] = None
        self._current_file: Optional[Path] = None
        self._start_time: Optional[float] = None
        self._current_device: Optional[AudioDevice] = None
        self._meeting_id: Optional[str] = None
        self._max_duration_task: Optional[asyncio.Task[None]] = None
        self._duration_broadcast_task: Optional[asyncio.Task[None]] = None

        # 콜백
        self._sync_callbacks: list[SyncCallback] = []
        self._async_callbacks: list[AsyncCallback] = []

        logger.info(
            f"AudioRecorder 초기화: "
            f"sample_rate={self._sample_rate}, "
            f"channels={self._channels}, "
            f"max_duration={self._max_duration}초"
        )

    @property
    def state(self) -> RecordingState:
        """현재 녹음 상태를 반환한다."""
        return self._state

    @property
    def is_recording(self) -> bool:
        """녹음 중인지 여부를 반환한다."""
        return self._state == RecordingState.RECORDING

    @property
    def current_duration(self) -> float:
        """현재 녹음 경과 시간(초)을 반환한다."""
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    @property
    def current_device_name(self) -> str:
        """현재 사용 중인 오디오 장치명을 반환한다."""
        if self._current_device is None:
            return ""
        return self._current_device.name

    def on_recording_complete(
        self, callback: Union[SyncCallback, AsyncCallback],
    ) -> None:
        """녹음 완료 콜백을 등록한다.

        Args:
            callback: 녹음 완료 시 호출할 콜백 함수
        """
        if asyncio.iscoroutinefunction(callback):
            self._async_callbacks.append(callback)
        else:
            self._sync_callbacks.append(callback)

    async def detect_audio_devices(self) -> list[AudioDevice]:
        """사용 가능한 오디오 입력 장치 목록을 반환한다.

        ffmpeg -f avfoundation -list_devices true -i "" 명령의
        stderr 출력을 파싱하여 오디오 장치를 추출한다.

        Returns:
            오디오 장치 목록

        Raises:
            AudioDeviceError: ffmpeg 실행 실패 시
        """
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-f", "avfoundation",
                "-list_devices", "true", "-i", "",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
        except FileNotFoundError as e:
            raise AudioDeviceError(
                "ffmpeg가 설치되어 있지 않습니다. "
                "'brew install ffmpeg'로 설치하세요."
            ) from e
        except OSError as e:
            raise AudioDeviceError(
                f"ffmpeg 실행 실패: {e}"
            ) from e

        return self._parse_device_list(stderr.decode("utf-8", errors="replace"))

    def _parse_device_list(self, stderr_output: str) -> list[AudioDevice]:
        """ffmpeg stderr 출력에서 오디오 장치 목록을 파싱한다.

        Args:
            stderr_output: ffmpeg의 stderr 출력 문자열

        Returns:
            파싱된 오디오 장치 목록
        """
        devices: list[AudioDevice] = []
        in_audio_section = False

        for line in stderr_output.splitlines():
            # AVFoundation 오디오 장치 섹션 시작
            if "AVFoundation audio devices:" in line:
                in_audio_section = True
                continue

            # 비디오 섹션이 나오면 오디오 섹션 종료
            if in_audio_section and "AVFoundation video devices:" in line:
                break

            if not in_audio_section:
                continue

            # 장치 라인 파싱: "[AVFoundation ...] [0] Device Name"
            # 형식: [숫자] 장치이름
            if "] [" in line:
                try:
                    # 마지막 [숫자] 부분 추출
                    bracket_parts = line.split("] [")
                    if len(bracket_parts) >= 2:
                        index_and_name = bracket_parts[-1]
                        # "0] Device Name" 에서 인덱스와 이름 분리
                        idx_str, name = index_and_name.split("] ", 1)
                        idx = int(idx_str.strip())
                        name = name.strip()

                        is_blackhole = "blackhole" in name.lower()
                        devices.append(AudioDevice(
                            index=idx,
                            name=name,
                            is_blackhole=is_blackhole,
                        ))
                except (ValueError, IndexError):
                    continue

        logger.info(f"오디오 장치 감지: {len(devices)}개")
        for dev in devices:
            logger.info(
                f"  [{dev.index}] {dev.name}"
                f"{' (BlackHole)' if dev.is_blackhole else ''}"
            )

        return devices

    async def _select_audio_device(self) -> AudioDevice:
        """녹음에 사용할 오디오 장치를 선택한다.

        prefer_system_audio가 True이고 BlackHole이 설치되어 있으면
        BlackHole을 사용한다. 그렇지 않으면 기본 마이크를 사용한다.

        Returns:
            선택된 오디오 장치

        Raises:
            AudioDeviceError: 사용 가능한 오디오 장치가 없을 때
        """
        devices = await self.detect_audio_devices()

        if not devices:
            raise AudioDeviceError(
                "사용 가능한 오디오 입력 장치가 없습니다."
            )

        # BlackHole 우선 선택 (설정에서 시스템 오디오 우선일 때)
        if self._recording_config.prefer_system_audio:
            for dev in devices:
                if dev.is_blackhole:
                    logger.info(
                        f"BlackHole 장치 선택: [{dev.index}] {dev.name} "
                        f"(시스템 오디오 캡처)"
                    )
                    return dev

        # 기본 마이크 (첫 번째 장치)
        selected = devices[0]
        logger.info(
            f"기본 마이크 선택: [{selected.index}] {selected.name}"
        )
        return selected

    async def start_recording(
        self, meeting_id: Optional[str] = None,
    ) -> None:
        """녹음을 시작한다.

        ffmpeg subprocess를 시작하여 오디오를 recordings_temp에 저장한다.

        Args:
            meeting_id: 회의 식별자 (None이면 타임스탬프로 자동 생성)

        Raises:
            AlreadyRecordingError: 이미 녹음 중일 때
            AudioDeviceError: 오디오 장치를 사용할 수 없을 때
            FFmpegRecordError: ffmpeg 프로세스 시작 실패 시
        """
        if self._state != RecordingState.IDLE:
            raise AlreadyRecordingError(
                f"이미 녹음 중입니다. 현재 상태: {self._state.value}"
            )

        if not self._recording_config.enabled:
            logger.warning("녹음 기능이 비활성화되어 있습니다 (recording.enabled=false)")
            return

        # 오디오 장치 선택
        device = await self._select_audio_device()
        self._current_device = device

        # 녹음 파일 경로 설정
        if meeting_id is None:
            meeting_id = datetime.now().strftime("meeting_%Y%m%d_%H%M%S")
        self._meeting_id = meeting_id

        self._temp_dir.mkdir(parents=True, exist_ok=True)
        output_file = self._temp_dir / f"{meeting_id}.wav"
        self._current_file = output_file

        # ffmpeg 녹음 명령 구성
        cmd = [
            "ffmpeg", "-y",
            "-f", "avfoundation",
            "-i", f":{device.index}",
            "-acodec", "pcm_s16le",
            "-ar", str(self._sample_rate),
            "-ac", str(self._channels),
            str(output_file),
        ]

        logger.info(
            f"녹음 시작: meeting_id={meeting_id}, "
            f"장치=[{device.index}] {device.name}, "
            f"출력={output_file}"
        )

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise FFmpegRecordError(
                "ffmpeg가 설치되어 있지 않습니다. "
                "'brew install ffmpeg'로 설치하세요."
            ) from e
        except OSError as e:
            raise FFmpegRecordError(
                f"ffmpeg 프로세스 시작 실패: {e}"
            ) from e

        self._state = RecordingState.RECORDING
        self._start_time = time.time()

        # 최대 녹음 시간 가드 시작
        self._max_duration_task = asyncio.create_task(
            self._max_duration_guard(),
            name="recording-max-duration",
        )

        # 녹음 시간 브로드캐스트 시작
        self._duration_broadcast_task = asyncio.create_task(
            self._duration_broadcast_loop(),
            name="recording-duration-broadcast",
        )

        # WebSocket 이벤트 브로드캐스트
        await self._broadcast_event("recording_started", {
            "meeting_id": meeting_id,
            "device": device.name,
            "is_system_audio": device.is_blackhole,
        })

        logger.info(f"녹음 시작 완료: PID={self._process.pid}")

    async def stop_recording(
        self, *, _from_guard: bool = False,
    ) -> Optional[RecordingResult]:
        """녹음을 정지하고 파일을 audio_input으로 이동한다.

        ffmpeg에 'q' 명령을 보내 정상 종료한다.
        타임아웃 시 SIGTERM으로 강제 종료한다.

        Args:
            _from_guard: _max_duration_guard에서 호출 시 True (내부 전용)

        Returns:
            녹음 결과 (RecordingResult) 또는 최소 시간 미달 시 None

        Raises:
            FFmpegRecordError: ffmpeg 종료 처리 실패 시
        """
        if self._state != RecordingState.RECORDING:
            logger.warning(f"녹음 중이 아닙니다. 현재 상태: {self._state.value}")
            return None

        self._state = RecordingState.STOPPING

        # 가드 태스크 취소 (_from_guard=True이면 자기 자신이므로 건너뜀)
        if not _from_guard and self._max_duration_task is not None:
            self._max_duration_task.cancel()
            try:
                await self._max_duration_task
            except asyncio.CancelledError:
                pass
        self._max_duration_task = None

        # 브로드캐스트 태스크 취소
        if self._duration_broadcast_task is not None:
            self._duration_broadcast_task.cancel()
            try:
                await self._duration_broadcast_task
            except asyncio.CancelledError:
                pass
            self._duration_broadcast_task = None

        # ffmpeg 정상 종료 시도 (stdin에 'q' 전송)
        result = await self._terminate_ffmpeg()

        self._state = RecordingState.IDLE

        if result is not None:
            # 콜백 호출
            await self._fire_callbacks(result)

            # WebSocket 이벤트 브로드캐스트
            await self._broadcast_event("recording_stopped", {
                "meeting_id": self._meeting_id,
                "duration_seconds": result.duration_seconds,
                "file_path": str(result.file_path),
                "audio_device": result.audio_device,
            })
        else:
            await self._broadcast_event("recording_stopped", {
                "meeting_id": self._meeting_id,
                "discarded": True,
                "reason": "최소 녹음 시간 미달",
            })

        # 상태 초기화
        self._process = None
        self._current_file = None
        self._start_time = None
        self._current_device = None
        self._meeting_id = None

        return result

    async def _terminate_ffmpeg(self) -> Optional[RecordingResult]:
        """ffmpeg 프로세스를 종료하고 녹음 결과를 반환한다.

        Returns:
            녹음 결과 또는 최소 시간 미달 시 None

        Raises:
            FFmpegRecordError: 프로세스 종료 실패 시
        """
        if self._process is None or self._current_file is None:
            return None

        duration = self.current_duration
        ended_at = datetime.now().isoformat()
        started_at = ""
        if self._start_time is not None:
            started_at = datetime.fromtimestamp(self._start_time).isoformat()

        logger.info(f"ffmpeg 종료 시도: 녹음 시간={duration:.1f}초")

        # 1단계: stdin에 'q' 전송 (graceful 종료)
        try:
            if self._process.stdin is not None:
                self._process.stdin.write(b"q")
                await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.debug("ffmpeg stdin 전송 실패 (이미 종료됨)")

        # 2단계: graceful 타임아웃 대기
        try:
            await asyncio.wait_for(
                self._process.wait(),
                timeout=self._graceful_timeout,
            )
            logger.info("ffmpeg 정상 종료 완료")
        except asyncio.TimeoutError:
            # 3단계: SIGTERM 강제 종료
            logger.warning(
                f"ffmpeg graceful 종료 타임아웃 "
                f"({self._graceful_timeout}초). SIGTERM 전송"
            )
            try:
                self._process.terminate()
                await asyncio.wait_for(
                    self._process.wait(), timeout=5,
                )
            except asyncio.TimeoutError:
                logger.error("ffmpeg SIGTERM 타임아웃. SIGKILL 전송")
                self._process.kill()
                await self._process.wait()

        # 최소 시간 미달 체크
        if duration < self._min_duration:
            logger.info(
                f"녹음 시간 {duration:.1f}초 < 최소 {self._min_duration}초. "
                f"파일 파기: {self._current_file}"
            )
            if self._current_file.exists():
                self._current_file.unlink()
            return None

        # 파일 유효성 확인
        if not self._current_file.exists():
            raise FFmpegRecordError(
                f"녹음 파일이 생성되지 않았습니다: {self._current_file}"
            )

        file_size = self._current_file.stat().st_size
        if file_size == 0:
            logger.warning(f"녹음 파일이 비어있습니다: {self._current_file}")
            self._current_file.unlink()
            return None

        # audio_input으로 파일 이동
        self._audio_input_dir.mkdir(parents=True, exist_ok=True)
        dest_file = self._audio_input_dir / self._current_file.name

        shutil.move(str(self._current_file), str(dest_file))
        logger.info(f"녹음 파일 이동: {self._current_file} → {dest_file}")

        return RecordingResult(
            file_path=dest_file,
            duration_seconds=round(duration, 1),
            audio_device=self._current_device.name if self._current_device else "",
            started_at=started_at,
            ended_at=ended_at,
            file_size_bytes=file_size,
        )

    async def _max_duration_guard(self) -> None:
        """최대 녹음 시간 초과 시 자동 정지한다."""
        try:
            await asyncio.sleep(self._max_duration)
            logger.warning(
                f"최대 녹음 시간 초과 ({self._max_duration}초). 자동 정지"
            )
            await self.stop_recording(_from_guard=True)
        except asyncio.CancelledError:
            pass

    async def _duration_broadcast_loop(self) -> None:
        """10초 간격으로 녹음 경과 시간을 브로드캐스트한다."""
        try:
            while True:
                await asyncio.sleep(10)
                if self._state == RecordingState.RECORDING:
                    await self._broadcast_event("recording_duration", {
                        "meeting_id": self._meeting_id,
                        "duration_seconds": round(self.current_duration, 1),
                    })
        except asyncio.CancelledError:
            pass

    async def _broadcast_event(
        self, event_type: str, data: dict[str, Any],
    ) -> None:
        """WebSocket 이벤트를 브로드캐스트한다.

        Args:
            event_type: 이벤트 타입 문자열
            data: 이벤트 데이터
        """
        if self._ws_manager is None:
            return

        try:
            from api.websocket import WebSocketEvent
            event = WebSocketEvent(event_type=event_type, data=data)
            await self._ws_manager.broadcast_event(event)
        except Exception as e:
            logger.debug(f"WebSocket 브로드캐스트 실패: {e}")

    async def _fire_callbacks(self, result: RecordingResult) -> None:
        """등록된 콜백을 호출한다.

        Args:
            result: 녹음 결과
        """
        for cb in self._sync_callbacks:
            try:
                cb(result)
            except Exception as e:
                logger.error(f"동기 콜백 실행 실패: {e}")

        for cb in self._async_callbacks:
            try:
                await cb(result)
            except Exception as e:
                logger.error(f"비동기 콜백 실행 실패: {e}")

    async def cleanup(self) -> None:
        """녹음 중이면 정지하고 리소스를 정리한다.

        ffmpeg 프로세스가 고아로 남는 것을 방지하기 위해
        모든 상태에서 프로세스 종료를 확인한다 (STAB: 고아 프로세스 방지).
        """
        if self._state == RecordingState.RECORDING:
            logger.info("AudioRecorder 정리: 녹음 정지 중...")
            try:
                await self.stop_recording()
            except Exception as e:
                logger.error(f"녹음 정지 중 에러 발생: {e}")

        # 어떤 상태든 ffmpeg 프로세스가 남아있으면 강제 종료
        await self._kill_orphan_process()

    async def _kill_orphan_process(self) -> None:
        """고아 ffmpeg 프로세스가 남아있으면 강제 종료한다.

        cleanup() 또는 비정상 종료 후 호출되어
        ffmpeg 프로세스가 백그라운드에서 계속 실행되는 것을 방지한다.
        (STAB: ffmpeg 고아 프로세스 방지)
        """
        if self._process is None:
            return

        # 이미 종료된 프로세스인지 확인
        if self._process.returncode is not None:
            logger.debug("ffmpeg 프로세스 이미 종료됨")
            self._process = None
            return

        pid = self._process.pid
        logger.warning(f"고아 ffmpeg 프로세스 감지: PID={pid}, 강제 종료 시도")

        try:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
                logger.info(f"고아 ffmpeg 프로세스 SIGTERM 종료: PID={pid}")
            except asyncio.TimeoutError:
                logger.warning(
                    f"고아 ffmpeg SIGTERM 타임아웃, SIGKILL 전송: PID={pid}"
                )
                self._process.kill()
                await self._process.wait()
                logger.info(f"고아 ffmpeg 프로세스 SIGKILL 종료: PID={pid}")
        except ProcessLookupError:
            logger.debug(f"ffmpeg 프로세스가 이미 사라짐: PID={pid}")
        except OSError as e:
            logger.error(f"고아 ffmpeg 프로세스 종료 실패: PID={pid}, 에러={e}")
        finally:
            self._process = None

    def get_status(self) -> dict[str, Any]:
        """현재 녹음 상태를 딕셔너리로 반환한다.

        Returns:
            녹음 상태 정보 딕셔너리
        """
        return {
            "state": self._state.value,
            "is_recording": self.is_recording,
            "duration_seconds": round(self.current_duration, 1),
            "meeting_id": self._meeting_id,
            "device": self._current_device.name if self._current_device else None,
            "is_system_audio": (
                self._current_device.is_blackhole
                if self._current_device else False
            ),
        }
