"""
오디오 변환 모듈 (Audio Converter Module)

목적: 다양한 오디오 포맷(mp3, m4a, flac, ogg, webm 등)을
     Whisper STT에 필요한 16kHz 모노 WAV로 변환한다.
주요 기능: ffmpeg 기반 오디오 변환, ffprobe 기반 포맷 감지, 불필요 변환 스킵
의존성: ffmpeg (시스템 바이너리), config 모듈
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import AppConfig

logger = logging.getLogger(__name__)


@dataclass
class AudioInfo:
    """오디오 파일의 메타정보를 담는 데이터 클래스.

    Attributes:
        sample_rate: 샘플레이트 (Hz)
        channels: 채널 수 (1=모노, 2=스테레오)
        codec: 오디오 코덱명 (예: pcm_s16le, aac)
        duration: 재생 시간 (초)
    """
    sample_rate: int
    channels: int
    codec: str
    duration: float


class AudioConvertError(Exception):
    """오디오 변환 중 발생하는 에러의 기본 클래스."""


class FFmpegNotFoundError(AudioConvertError):
    """ffmpeg 또는 ffprobe 바이너리를 찾을 수 없을 때 발생한다."""


class UnsupportedFormatError(AudioConvertError):
    """지원하지 않는 오디오 포맷일 때 발생한다."""


class ConversionFailedError(AudioConvertError):
    """ffmpeg 변환 프로세스가 실패했을 때 발생한다."""


class AudioConverter:
    """ffmpeg를 사용하여 오디오를 16kHz 모노 WAV로 변환하는 클래스.

    Args:
        config: 애플리케이션 설정 인스턴스

    사용 예시:
        converter = AudioConverter(config)
        output_path = converter.convert(Path("input.mp3"), Path("output_dir"))
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._target_sample_rate = config.audio.sample_rate
        self._target_channels = config.audio.channels
        self._supported_formats = set(config.audio.supported_input_formats)

    def _check_ffmpeg_installed(self) -> None:
        """ffmpeg와 ffprobe가 시스템에 설치되어 있는지 확인한다.

        Raises:
            FFmpegNotFoundError: ffmpeg 또는 ffprobe를 찾을 수 없을 때
        """
        for binary in ("ffmpeg", "ffprobe"):
            if shutil.which(binary) is None:
                raise FFmpegNotFoundError(
                    f"{binary}가 설치되어 있지 않습니다. "
                    f"'brew install ffmpeg'으로 설치하세요."
                )

    def _validate_input(self, input_path: Path) -> None:
        """입력 파일의 존재 여부와 포맷을 검증한다.

        Args:
            input_path: 입력 오디오 파일 경로

        Raises:
            FileNotFoundError: 파일이 존재하지 않을 때
            UnsupportedFormatError: 지원하지 않는 포맷일 때
        """
        if not input_path.exists():
            raise FileNotFoundError(f"입력 파일을 찾을 수 없습니다: {input_path}")

        if not input_path.is_file():
            raise FileNotFoundError(f"입력 경로가 파일이 아닙니다: {input_path}")

        # 확장자로 포맷 확인 (소문자 변환, 점 제거)
        ext = input_path.suffix.lower().lstrip(".")
        if ext not in self._supported_formats:
            raise UnsupportedFormatError(
                f"지원하지 않는 오디오 포맷입니다: .{ext} "
                f"(지원 포맷: {', '.join(sorted(self._supported_formats))})"
            )

    def probe(self, input_path: Path) -> Optional[AudioInfo]:
        """ffprobe로 오디오 파일의 메타정보를 조회한다.

        Args:
            input_path: 분석할 오디오 파일 경로

        Returns:
            AudioInfo 객체. ffprobe 실패 시 None 반환.
        """
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-select_streams", "a:0",
            str(input_path),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"ffprobe 타임아웃: {input_path}")
            return None
        except FileNotFoundError:
            logger.warning("ffprobe 바이너리를 찾을 수 없습니다")
            return None

        if result.returncode != 0:
            logger.warning(f"ffprobe 실패 (코드 {result.returncode}): {result.stderr}")
            return None

        try:
            data = json.loads(result.stdout)
            streams = data.get("streams", [])
            if not streams:
                logger.warning(f"오디오 스트림을 찾을 수 없습니다: {input_path}")
                return None

            stream = streams[0]
            return AudioInfo(
                sample_rate=int(stream.get("sample_rate", 0)),
                channels=int(stream.get("channels", 0)),
                codec=stream.get("codec_name", "unknown"),
                duration=float(stream.get("duration", 0.0)),
            )
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning(f"ffprobe 출력 파싱 실패: {e}")
            return None

    def _is_already_target_format(self, info: AudioInfo) -> bool:
        """이미 목표 포맷(16kHz 모노 PCM WAV)인지 확인한다.

        Args:
            info: ffprobe로 조회한 오디오 메타정보

        Returns:
            변환이 불필요하면 True
        """
        return (
            info.sample_rate == self._target_sample_rate
            and info.channels == self._target_channels
            and info.codec in ("pcm_s16le", "pcm_s16be")
        )

    def convert(
        self,
        input_path: Path,
        output_dir: Path,
        output_filename: Optional[str] = None,
    ) -> Path:
        """오디오 파일을 16kHz 모노 WAV로 변환한다.

        이미 목표 포맷이면 변환을 건너뛰고 원본 경로를 반환한다.

        Args:
            input_path: 입력 오디오 파일 경로
            output_dir: 변환 결과를 저장할 디렉토리
            output_filename: 출력 파일명 (None이면 원본명_16k.wav)

        Returns:
            변환된 WAV 파일 경로 (Path)

        Raises:
            FFmpegNotFoundError: ffmpeg가 설치되지 않았을 때
            FileNotFoundError: 입력 파일이 없을 때
            UnsupportedFormatError: 지원하지 않는 포맷일 때
            ConversionFailedError: ffmpeg 변환 실패 시
        """
        # 사전 검증
        self._check_ffmpeg_installed()
        self._validate_input(input_path)

        # PERF: WAV 파일일 때만 ffprobe로 포맷 확인 (변환 스킵 판단용)
        # 비-WAV 포맷(mp3, m4a 등)은 항상 변환이 필요하므로 ffprobe 생략
        ext = input_path.suffix.lower().lstrip(".")
        info = None
        if ext == "wav":
            info = self.probe(input_path)
            if info is not None:
                logger.info(
                    f"입력 오디오 정보: {info.codec}, "
                    f"{info.sample_rate}Hz, {info.channels}ch, "
                    f"{info.duration:.1f}초"
                )
                # 이미 목표 포맷이면 변환 스킵
                if self._is_already_target_format(info):
                    logger.info(f"이미 목표 포맷입니다. 변환을 건너뜁니다: {input_path}")
                    return input_path
        else:
            logger.info(f"비-WAV 포맷 ({ext}), ffprobe 건너뛰고 바로 변환")

        # 출력 경로 결정
        output_dir.mkdir(parents=True, exist_ok=True)
        if output_filename is None:
            output_filename = f"{input_path.stem}_16k.wav"
        output_path = output_dir / output_filename

        # ffmpeg 변환 명령 구성
        cmd = [
            "ffmpeg",
            "-y",                          # 덮어쓰기 허용
            "-i", str(input_path),         # 입력 파일
            "-vn",                         # 비디오 스트림 제거
            "-acodec", "pcm_s16le",        # 16비트 PCM 리틀엔디안
            "-ar", str(self._target_sample_rate),  # 샘플레이트
            "-ac", str(self._target_channels),     # 채널 수
            str(output_path),
        ]

        logger.info(f"오디오 변환 시작: {input_path.name} → {output_path.name}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10분 타임아웃 (긴 녹음 파일 대비)
            )
        except subprocess.TimeoutExpired as e:
            # 타임아웃 시 불완전한 출력 파일 정리
            if output_path.exists():
                try:
                    output_path.unlink()
                    logger.info(f"타임아웃으로 인한 불완전 출력 파일 삭제: {output_path}")
                except OSError as cleanup_err:
                    logger.warning(f"불완전 출력 파일 삭제 실패: {cleanup_err}")
            raise ConversionFailedError(
                f"오디오 변환 타임아웃 (600초 초과): {input_path}"
            ) from e

        if result.returncode != 0:
            # 실패 시 부분 생성된 파일 정리
            if output_path.exists():
                output_path.unlink()
            raise ConversionFailedError(
                f"ffmpeg 변환 실패 (코드 {result.returncode}): {result.stderr.strip()}"
            )

        # 출력 파일 생성 확인
        if not output_path.exists():
            raise ConversionFailedError(
                f"ffmpeg가 성공했으나 출력 파일이 생성되지 않았습니다: {output_path}"
            )

        # 출력 파일 크기가 0이면 실패로 간주
        output_size = output_path.stat().st_size
        if output_size == 0:
            output_path.unlink()
            raise ConversionFailedError(
                f"변환된 파일 크기가 0입니다: {output_path}"
            )

        # 출력 파일이 너무 작으면 (44바이트 = WAV 헤더만 있는 빈 파일) 경고
        if output_size <= 44:
            output_path.unlink()
            raise ConversionFailedError(
                f"변환된 파일이 WAV 헤더만 포함합니다 ({output_size} bytes): {output_path}"
            )

        # ffprobe로 변환 결과 무결성 검증 (best-effort — 실패해도 계속 진행)
        try:
            output_info = self.probe(output_path)
            if output_info is not None and output_info.duration <= 0.0:
                logger.warning(
                    f"변환된 파일의 duration이 0입니다. 파일이 손상되었을 수 있습니다: {output_path}"
                )
        except Exception as probe_err:
            logger.debug(f"변환 후 ffprobe 검증 실패 (무시): {probe_err}")

        logger.info(
            f"오디오 변환 완료: {output_path} "
            f"({output_size / 1024:.1f}KB)"
        )
        return output_path

    async def convert_async(
        self,
        input_path: Path,
        output_dir: Path,
        output_filename: Optional[str] = None,
    ) -> Path:
        """오디오 변환의 비동기 래퍼.

        asyncio 파이프라인에서 사용할 수 있도록
        convert()를 별도 스레드에서 실행한다.

        Args:
            input_path: 입력 오디오 파일 경로
            output_dir: 변환 결과를 저장할 디렉토리
            output_filename: 출력 파일명 (None이면 원본명_16k.wav)

        Returns:
            변환된 WAV 파일 경로 (Path)
        """
        return await asyncio.to_thread(
            self.convert, input_path, output_dir, output_filename
        )
