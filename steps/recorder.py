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
import contextlib
import logging
import shutil
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Union

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


class RecordingState(StrEnum):
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
    file_paths: dict[str, Path] | None = None  # 멀티트랙: {"system": Path, "mic": Path}
    is_multitrack: bool = False


@dataclass
class AudioDevice:
    """오디오 장치 정보를 담는 데이터 클래스.

    Attributes:
        index: ffmpeg AVFoundation 장치 인덱스
        name: 장치 이름
        is_blackhole: BlackHole 가상 장치 여부
        is_virtual: 가상 오디오 장치 여부 (ZoomAudioDevice, SoundFlower 등)
        is_aggregate: macOS Aggregate Device 여부 (본인 마이크 + BlackHole 통합용)
    """

    index: int
    name: str
    is_blackhole: bool = False
    is_virtual: bool = False
    is_aggregate: bool = False

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다."""
        return {
            "index": self.index,
            "name": self.name,
            "is_blackhole": self.is_blackhole,
            "is_virtual": self.is_virtual,
            "is_aggregate": self.is_aggregate,
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
        config: AppConfig | None = None,
        ws_manager: Any | None = None,
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
        self._multi_track = self._recording_config.multi_track
        self._silence_threshold = self._recording_config.silence_threshold_rms

        # 경로 설정
        self._temp_dir = self._config.paths.resolved_recordings_temp_dir
        self._audio_input_dir = self._config.paths.resolved_audio_input_dir

        # 상태 (싱글트랙)
        self._state = RecordingState.IDLE
        self._process: asyncio.subprocess.Process | None = None
        self._current_file: Path | None = None
        self._start_time: float | None = None
        self._current_device: AudioDevice | None = None
        self._meeting_id: str | None = None
        self._max_duration_task: asyncio.Task[None] | None = None
        self._duration_broadcast_task: asyncio.Task[None] | None = None

        # 멀티트랙 상태
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._current_files: dict[str, Path] = {}
        self._current_devices: dict[str, AudioDevice] = {}

        # 콜백
        self._sync_callbacks: list[SyncCallback] = []
        self._async_callbacks: list[AsyncCallback] = []

        logger.info(
            f"AudioRecorder 초기화: "
            f"sample_rate={self._sample_rate}, "
            f"channels={self._channels}, "
            f"max_duration={self._max_duration}초, "
            f"multi_track={self._multi_track}"
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
        self,
        callback: Union[SyncCallback, AsyncCallback],
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
                "ffmpeg",
                "-f",
                "avfoundation",
                "-list_devices",
                "true",
                "-i",
                "",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
        except FileNotFoundError as e:
            raise AudioDeviceError(
                "ffmpeg가 설치되어 있지 않습니다. 'brew install ffmpeg'로 설치하세요."
            ) from e
        except OSError as e:
            raise AudioDeviceError(f"ffmpeg 실행 실패: {e}") from e

        return self._parse_device_list(stderr.decode("utf-8", errors="replace"))

    def _check_audio_energy(self, file_path: Path) -> bool:
        """녹음 파일의 RMS 에너지를 검사하여 무음 여부를 판정한다.

        soundfile로 WAV 파일을 읽고 RMS(Root Mean Square)를 계산한다.
        RMS가 silence_threshold_rms 미만이면 무음으로 판정한다.

        Args:
            file_path: 검사할 WAV 파일 경로

        Returns:
            True: 유효한 오디오 (무음 아님)
            False: 무음 파일
        """
        try:
            import numpy as np
            import soundfile as sf
        except ImportError:
            logger.warning(
                "soundfile/numpy 미설치 — 무음 감지를 건너뜁니다. "
                "'pip install soundfile numpy'로 설치하세요."
            )
            return True  # 라이브러리 없으면 통과시킨다

        try:
            data, _ = sf.read(file_path, dtype="float32")
        except Exception as e:
            logger.warning(f"오디오 파일 읽기 실패 — 무음 감지를 건너뜁니다: {e}")
            return True  # 파일 읽기 실패 시 통과시킨다

        if data.size == 0:
            logger.warning(f"오디오 데이터가 비어있습니다: {file_path}")
            return False

        rms = float(np.sqrt(np.mean(data**2)))
        logger.info(f"오디오 RMS 에너지: {rms:.6f} (임계값: {self._silence_threshold})")

        if rms < self._silence_threshold:
            logger.warning(
                f"무음 파일 감지: RMS={rms:.6f} < 임계값={self._silence_threshold}. "
                f"오디오 장치를 확인하세요."
            )
            return False

        return True

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

                        name_lower = name.lower()
                        is_blackhole = "blackhole" in name_lower
                        # Aggregate Device: 본인 마이크 + BlackHole 합성 장치.
                        # 본인 목소리 포함 녹음의 핵심 수단이므로 virtual 에서 제외하고
                        # 별도 플래그로 분리한다.
                        is_aggregate = "aggregate" in name_lower
                        # 가상 장치 감지 (BlackHole·Aggregate 는 별도 플래그로 처리)
                        virtual_keywords = [
                            "zoom",
                            "virtual",
                            "soundflower",
                            "loopback",
                        ]
                        is_virtual = not is_aggregate and any(
                            kw in name_lower for kw in virtual_keywords
                        )
                        devices.append(
                            AudioDevice(
                                index=idx,
                                name=name,
                                is_blackhole=is_blackhole,
                                is_virtual=is_virtual,
                                is_aggregate=is_aggregate,
                            )
                        )
                except (ValueError, IndexError):
                    continue

        logger.info(f"오디오 장치 감지: {len(devices)}개")
        for dev in devices:
            # 장치 타입 레이블 생성
            label = ""
            if dev.is_aggregate:
                label = " (Aggregate)"
            elif dev.is_blackhole:
                label = " (BlackHole)"
            elif dev.is_virtual:
                label = " (가상 장치)"
            logger.info(f"  [{dev.index}] {dev.name}{label}")

        return devices

    async def _select_audio_device(self) -> AudioDevice:
        """녹음에 사용할 오디오 장치를 선택한다.

        선택 우선순위:
            0단계: config.preferred_device_name 명시 지정 (정확 매칭 → 부분 매칭)
            1단계: Aggregate Device (본인 마이크 + BlackHole 통합, prefer_system_audio 시)
            2단계: BlackHole (Aggregate 없을 때, prefer_system_audio 시)
            3단계: 가상·Aggregate·BlackHole 을 제외한 실제 장치 목록 필터링
            4단계: 마이크 키워드 매칭 (microphone, built-in 등)
            5단계: 실제 장치 중 첫 번째 선택
            6단계: 가상 장치만 있는 경우 경고 후 폴백

        Returns:
            선택된 오디오 장치

        Raises:
            AudioDeviceError: 사용 가능한 오디오 장치가 없을 때
        """
        devices = await self.detect_audio_devices()

        if not devices:
            raise AudioDeviceError("사용 가능한 오디오 입력 장치가 없습니다.")

        # 0단계: 명시적 장치명 지정 — 정확 매칭 우선, 없으면 부분 매칭
        preferred = getattr(self._recording_config, "preferred_device_name", "") or ""
        if preferred:
            pref_lower = preferred.lower()
            for dev in devices:
                if dev.name.lower() == pref_lower:
                    logger.info(f"명시 지정 장치 선택 (정확): [{dev.index}] {dev.name}")
                    return dev
            for dev in devices:
                if pref_lower in dev.name.lower():
                    logger.info(f"명시 지정 장치 선택 (부분): [{dev.index}] {dev.name}")
                    return dev
            logger.warning(f"preferred_device_name='{preferred}' 장치 미발견 → 자동 선택으로 폴백")

        if self._recording_config.prefer_system_audio:
            # 1단계: Aggregate Device 우선 — 본인 목소리 + 시스템 오디오 통합
            for dev in devices:
                if dev.is_aggregate:
                    logger.info(
                        f"Aggregate 장치 선택 (본인 + 시스템 오디오 통합): "
                        f"[{dev.index}] {dev.name}"
                    )
                    return dev

            # 2단계: BlackHole (Aggregate 없을 때 폴백)
            for dev in devices:
                if dev.is_blackhole:
                    logger.info(f"시스템 오디오 장치 선택: [{dev.index}] {dev.name}")
                    return dev

        # 3단계: 가상·Aggregate·BlackHole 제외한 실제 장치 목록
        real_devices = [
            d for d in devices if not d.is_virtual and not d.is_blackhole and not d.is_aggregate
        ]

        # 4단계: 마이크 키워드 매칭
        mic_keywords = ["microphone", "마이크", "built-in", "internal", "macbook"]
        for dev in real_devices:
            if any(kw in dev.name.lower() for kw in mic_keywords):
                logger.info(f"마이크 장치 선택: [{dev.index}] {dev.name}")
                return dev

        # 5단계: 가상 장치 제외 후 첫 번째
        if real_devices:
            selected = real_devices[0]
            logger.info(f"기본 오디오 장치 선택: [{selected.index}] {selected.name}")
            return selected

        # 6단계: 최후 폴백 (가상 장치만 있는 경우)
        selected = devices[0]
        logger.warning(
            f"실제 마이크를 찾을 수 없어 가상 장치를 사용합니다: "
            f"[{selected.index}] {selected.name}"
        )
        return selected

    async def start_recording(
        self,
        meeting_id: str | None = None,
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
            raise AlreadyRecordingError(f"이미 녹음 중입니다. 현재 상태: {self._state.value}")

        if not self._recording_config.enabled:
            logger.warning("녹음 기능이 비활성화되어 있습니다 (recording.enabled=false)")
            return

        # 녹음 파일 경로 설정
        if meeting_id is None:
            meeting_id = datetime.now().strftime("meeting_%Y%m%d_%H%M%S")
        self._meeting_id = meeting_id
        self._temp_dir.mkdir(parents=True, exist_ok=True)

        # 멀티트랙 vs 싱글트랙 분기
        if self._multi_track:
            await self._start_multitrack_recording(meeting_id)
        else:
            await self._start_singletrack_recording(meeting_id)

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
        device_name = (
            self._current_device.name
            if self._current_device
            else ", ".join(d.name for d in self._current_devices.values())
        )
        await self._broadcast_event(
            "recording_started",
            {
                "meeting_id": meeting_id,
                "device": device_name,
                "is_multitrack": bool(self._processes),
            },
        )

        pids = (
            {k: p.pid for k, p in self._processes.items()}
            if self._processes
            else {"single": self._process.pid if self._process else None}
        )
        logger.info(f"녹음 시작 완료: PIDs={pids}")

    async def _start_singletrack_recording(self, meeting_id: str) -> None:
        """싱글트랙 녹음을 시작한다 (기존 로직).

        Args:
            meeting_id: 회의 식별자
        """
        device = await self._select_audio_device()
        self._current_device = device

        output_file = self._temp_dir / f"{meeting_id}.wav"
        self._current_file = output_file

        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "avfoundation",
            "-i",
            f":{device.index}",
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(self._sample_rate),
            "-ac",
            str(self._channels),
            str(output_file),
        ]

        logger.info(
            f"싱글트랙 녹음 시작: meeting_id={meeting_id}, "
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
                "ffmpeg가 설치되어 있지 않습니다. 'brew install ffmpeg'로 설치하세요."
            ) from e
        except OSError as e:
            raise FFmpegRecordError(f"ffmpeg 프로세스 시작 실패: {e}") from e

    async def _select_devices_multitrack(self) -> dict[str, AudioDevice]:
        """멀티트랙 녹음용 장치를 선택한다.

        BlackHole(시스템 오디오) + 마이크를 동시에 반환한다.
        BlackHole이 없으면 마이크만 반환 (싱글트랙 폴백).

        Returns:
            {"system": BlackHole장치, "mic": 마이크장치} 또는 {"mic": 마이크장치}
        """
        devices = await self.detect_audio_devices()
        if not devices:
            raise AudioDeviceError("사용 가능한 오디오 입력 장치가 없습니다.")

        result: dict[str, AudioDevice] = {}
        blackhole = None
        mic = None

        for dev in devices:
            if dev.is_blackhole and blackhole is None:
                blackhole = dev
            elif not dev.is_blackhole and mic is None:
                mic = dev

        if blackhole is not None and mic is not None:
            result["system"] = blackhole
            result["mic"] = mic
            logger.info(
                f"멀티트랙 장치 선택: system=[{blackhole.index}] {blackhole.name}, "
                f"mic=[{mic.index}] {mic.name}"
            )
        elif mic is not None:
            result["mic"] = mic
            logger.warning(f"BlackHole 미감지, 마이크만 사용: [{mic.index}] {mic.name}")
        elif blackhole is not None:
            result["system"] = blackhole
            logger.warning(
                f"마이크 미감지, BlackHole만 사용: [{blackhole.index}] {blackhole.name}"
            )
        else:
            raise AudioDeviceError("사용 가능한 오디오 입력 장치가 없습니다.")

        return result

    async def _start_multitrack_recording(self, meeting_id: str) -> None:
        """멀티트랙 녹음을 시작한다 (BlackHole + 마이크 동시).

        Args:
            meeting_id: 회의 식별자

        Raises:
            FFmpegRecordError: ffmpeg 프로세스 시작 실패 시
        """
        selected = await self._select_devices_multitrack()
        self._current_devices = selected

        # BlackHole 하나만이면 싱글트랙으로 폴백
        if len(selected) == 1:
            track_name = next(iter(selected))
            device = selected[track_name]
            logger.info(f"멀티트랙 장치 1개만 감지, 싱글트랙으로 폴백: {track_name}")
            self._current_device = device
            output_file = self._temp_dir / f"{meeting_id}.wav"
            self._current_file = output_file
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "avfoundation",
                "-i",
                f":{device.index}",
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(self._sample_rate),
                "-ac",
                str(self._channels),
                str(output_file),
            ]
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as e:
                raise FFmpegRecordError("ffmpeg가 설치되어 있지 않습니다.") from e
            except OSError as e:
                raise FFmpegRecordError(f"ffmpeg 프로세스 시작 실패: {e}") from e
            return

        # 2개 이상 → 각 트랙별 ffmpeg 프로세스 시작
        for track_name, device in selected.items():
            suffix = "_system" if track_name == "system" else "_mic"
            output_file = self._temp_dir / f"{meeting_id}{suffix}.wav"
            self._current_files[track_name] = output_file

            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "avfoundation",
                "-i",
                f":{device.index}",
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(self._sample_rate),
                "-ac",
                str(self._channels),
                str(output_file),
            ]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._processes[track_name] = proc
                logger.info(
                    f"멀티트랙 녹음 시작: {track_name}=[{device.index}] {device.name}, "
                    f"PID={proc.pid}, 출력={output_file}"
                )
            except (FileNotFoundError, OSError) as e:
                # 이미 시작된 프로세스 정리
                await self._cleanup_multitrack_processes()
                raise FFmpegRecordError(
                    f"멀티트랙 ffmpeg 프로세스 시작 실패 ({track_name}): {e}"
                ) from e

    def _build_multitrack_paths(self, meeting_id: str) -> dict[str, Path]:
        """멀티트랙 녹음 파일 경로를 생성한다.

        Args:
            meeting_id: 회의 식별자

        Returns:
            {"system": Path, "mic": Path} 형식의 경로 딕셔너리
        """
        return {
            "system": self._temp_dir / f"{meeting_id}_system.wav",
            "mic": self._temp_dir / f"{meeting_id}_mic.wav",
        }

    async def stop_recording(
        self,
        *,
        _from_guard: bool = False,
    ) -> RecordingResult | None:
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
            with contextlib.suppress(asyncio.CancelledError):
                await self._max_duration_task
        self._max_duration_task = None

        # 브로드캐스트 태스크 취소
        if self._duration_broadcast_task is not None:
            self._duration_broadcast_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._duration_broadcast_task
            self._duration_broadcast_task = None

        # 멀티트랙 vs 싱글트랙 종료 분기
        if self._processes:
            result = await self._terminate_multitrack()
        else:
            result = await self._terminate_ffmpeg()

        self._state = RecordingState.IDLE

        if result is not None:
            # 콜백 호출
            await self._fire_callbacks(result)

            # WebSocket 이벤트 브로드캐스트
            await self._broadcast_event(
                "recording_stopped",
                {
                    "meeting_id": self._meeting_id,
                    "duration_seconds": result.duration_seconds,
                    "file_path": str(result.file_path),
                    "audio_device": result.audio_device,
                    "is_multitrack": result.is_multitrack,
                },
            )
        else:
            await self._broadcast_event(
                "recording_stopped",
                {
                    "meeting_id": self._meeting_id,
                    "discarded": True,
                    "reason": "최소 녹음 시간 미달",
                },
            )

        # 상태 초기화
        self._process = None
        self._current_file = None
        self._start_time = None
        self._current_device = None
        self._meeting_id = None
        self._processes.clear()
        self._current_files.clear()
        self._current_devices.clear()

        return result

    async def _terminate_ffmpeg(self) -> RecordingResult | None:
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
        except TimeoutError:
            # 3단계: SIGTERM 강제 종료
            logger.warning(
                f"ffmpeg graceful 종료 타임아웃 ({self._graceful_timeout}초). SIGTERM 전송"
            )
            try:
                self._process.terminate()
                await asyncio.wait_for(
                    self._process.wait(),
                    timeout=5,
                )
            except TimeoutError:
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
            raise FFmpegRecordError(f"녹음 파일이 생성되지 않았습니다: {self._current_file}")

        file_size = self._current_file.stat().st_size
        if file_size == 0:
            logger.warning(f"녹음 파일이 비어있습니다: {self._current_file}")
            self._current_file.unlink()
            return None

        # 무음 감지: RMS 에너지가 임계값 미만이면 무음 파일로 판정
        if not self._check_audio_energy(self._current_file):
            logger.warning(
                f"무음 녹음 파일 파기: {self._current_file} "
                f"(장치: {self._current_device.name if self._current_device else '알 수 없음'})"
            )
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

    async def _terminate_one_ffmpeg(
        self,
        proc: asyncio.subprocess.Process,
        label: str,
    ) -> None:
        """하나의 ffmpeg 프로세스를 graceful하게 종료한다.

        Args:
            proc: 종료할 프로세스
            label: 로그용 트랙 라벨 (예: "system", "mic")
        """
        # stdin 'q' 전송
        try:
            if proc.stdin is not None:
                proc.stdin.write(b"q")
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.debug(f"ffmpeg stdin 전송 실패 ({label}, 이미 종료됨)")

        # graceful 타임아웃 대기
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._graceful_timeout)
            logger.info(f"ffmpeg 정상 종료 ({label})")
        except TimeoutError:
            logger.warning(f"ffmpeg graceful 타임아웃 ({label}). SIGTERM 전송")
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                logger.error(f"ffmpeg SIGTERM 타임아웃 ({label}). SIGKILL 전송")
                proc.kill()
                await proc.wait()

    async def _terminate_multitrack(self) -> RecordingResult | None:
        """멀티트랙 ffmpeg 프로세스를 모두 종료하고 결과를 반환한다.

        Returns:
            녹음 결과 또는 최소 시간 미달 시 None
        """
        duration = self.current_duration
        ended_at = datetime.now().isoformat()
        started_at = ""
        if self._start_time is not None:
            started_at = datetime.fromtimestamp(self._start_time).isoformat()

        logger.info(
            f"멀티트랙 ffmpeg 종료 시도: {len(self._processes)}개 프로세스, 녹음 시간={duration:.1f}초"
        )

        # 모든 프로세스 종료
        for track_name, proc in self._processes.items():
            await self._terminate_one_ffmpeg(proc, track_name)

        # 최소 시간 미달 체크
        if duration < self._min_duration:
            logger.info(
                f"녹음 시간 {duration:.1f}초 < 최소 {self._min_duration}초. 멀티트랙 파일 파기"
            )
            for file_path in self._current_files.values():
                if file_path.exists():
                    file_path.unlink()
            return None

        # 파일 유효성 확인 및 audio_input으로 이동
        self._audio_input_dir.mkdir(parents=True, exist_ok=True)
        moved_files: dict[str, Path] = {}
        total_size = 0

        for track_name, file_path in self._current_files.items():
            if not file_path.exists():
                logger.warning(f"멀티트랙 파일 미생성: {track_name}={file_path}")
                continue

            file_size = file_path.stat().st_size
            if file_size == 0:
                logger.warning(f"멀티트랙 파일 비어있음: {track_name}={file_path}")
                file_path.unlink()
                continue

            dest = self._audio_input_dir / file_path.name
            shutil.move(str(file_path), str(dest))
            moved_files[track_name] = dest
            total_size += file_size
            logger.info(f"멀티트랙 파일 이동: {file_path} → {dest}")

        if not moved_files:
            logger.warning("유효한 멀티트랙 파일이 없습니다.")
            return None

        # 첫 번째 파일을 file_path로 (하위 호환)
        first_file = next(iter(moved_files.values()))
        device_names = ", ".join(d.name for d in self._current_devices.values())

        return RecordingResult(
            file_path=first_file,
            duration_seconds=round(duration, 1),
            audio_device=device_names,
            started_at=started_at,
            ended_at=ended_at,
            file_size_bytes=total_size,
            file_paths=moved_files,
            is_multitrack=True,
        )

    async def _cleanup_multitrack_processes(self) -> None:
        """시작 실패 시 이미 시작된 멀티트랙 프로세스를 정리한다."""
        for track_name, proc in list(self._processes.items()):
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (TimeoutError, ProcessLookupError, OSError):
                try:
                    proc.kill()
                    await proc.wait()
                except (ProcessLookupError, OSError):
                    pass
            logger.info(f"멀티트랙 프로세스 정리: {track_name}")
        self._processes.clear()
        self._current_files.clear()

    async def _max_duration_guard(self) -> None:
        """최대 녹음 시간 초과 시 자동 정지한다."""
        try:
            await asyncio.sleep(self._max_duration)
            logger.warning(f"최대 녹음 시간 초과 ({self._max_duration}초). 자동 정지")
            await self.stop_recording(_from_guard=True)
        except asyncio.CancelledError:
            pass

    async def _duration_broadcast_loop(self) -> None:
        """10초 간격으로 녹음 경과 시간을 브로드캐스트한다."""
        try:
            while True:
                await asyncio.sleep(10)
                if self._state == RecordingState.RECORDING:
                    await self._broadcast_event(
                        "recording_duration",
                        {
                            "meeting_id": self._meeting_id,
                            "duration_seconds": round(self.current_duration, 1),
                        },
                    )
        except asyncio.CancelledError:
            pass

    async def _broadcast_event(
        self,
        event_type: str,
        data: dict[str, Any],
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
            except TimeoutError:
                logger.warning(f"고아 ffmpeg SIGTERM 타임아웃, SIGKILL 전송: PID={pid}")
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
                self._current_device.is_blackhole if self._current_device else False
            ),
        }
