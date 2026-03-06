"""
오디오 변환기 테스트 모듈 (Audio Converter Test Suite)

목적: AudioConverter의 변환, 검증, 에러 처리를 테스트한다.
주요 기능: ffmpeg 모킹 기반 단위 테스트, 포맷 검증, 엣지 케이스 테스트
의존성: pytest, unittest.mock, config 모듈, steps.audio_converter
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from config import AppConfig, load_config
from steps.audio_converter import (
    AudioConvertError,
    AudioConverter,
    AudioInfo,
    ConversionFailedError,
    FFmpegNotFoundError,
    UnsupportedFormatError,
)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    """테스트용 설정 인스턴스를 생성한다."""
    return AppConfig()


@pytest.fixture
def converter(config: AppConfig) -> AudioConverter:
    """테스트용 AudioConverter 인스턴스를 생성한다."""
    return AudioConverter(config)


@pytest.fixture
def sample_audio(tmp_path: Path) -> Path:
    """테스트용 더미 오디오 파일을 생성한다."""
    audio_file = tmp_path / "test_meeting.mp3"
    audio_file.write_bytes(b"\x00" * 1024)  # 더미 데이터
    return audio_file


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    """테스트용 출력 디렉토리를 생성한다."""
    out = tmp_path / "output"
    out.mkdir()
    return out


class TestAudioConverterInit:
    """AudioConverter 초기화 테스트."""

    def test_설정값이_올바르게_로드된다(self, converter: AudioConverter) -> None:
        """config.yaml의 audio 섹션 설정이 정상 반영되는지 확인한다."""
        assert converter._target_sample_rate == 16000
        assert converter._target_channels == 1
        assert "mp3" in converter._supported_formats
        assert "m4a" in converter._supported_formats
        assert "wav" in converter._supported_formats
        assert "flac" in converter._supported_formats
        assert "ogg" in converter._supported_formats
        assert "webm" in converter._supported_formats


class TestValidateInput:
    """입력 파일 검증 테스트."""

    def test_존재하지_않는_파일은_에러가_발생한다(
        self, converter: AudioConverter
    ) -> None:
        """존재하지 않는 파일 경로에 대해 FileNotFoundError를 발생시킨다."""
        with pytest.raises(FileNotFoundError, match="입력 파일을 찾을 수 없습니다"):
            converter._validate_input(Path("/nonexistent/audio.mp3"))

    def test_디렉토리_경로는_에러가_발생한다(
        self, converter: AudioConverter, tmp_path: Path
    ) -> None:
        """디렉토리 경로를 입력하면 FileNotFoundError를 발생시킨다."""
        with pytest.raises(FileNotFoundError, match="파일이 아닙니다"):
            converter._validate_input(tmp_path)

    def test_지원하지_않는_포맷은_에러가_발생한다(
        self, converter: AudioConverter, tmp_path: Path
    ) -> None:
        """지원하지 않는 확장자에 대해 UnsupportedFormatError를 발생시킨다."""
        unsupported = tmp_path / "test.xyz"
        unsupported.write_bytes(b"\x00")
        with pytest.raises(UnsupportedFormatError, match="지원하지 않는 오디오 포맷"):
            converter._validate_input(unsupported)

    def test_지원하는_포맷은_정상_통과한다(
        self, converter: AudioConverter, tmp_path: Path
    ) -> None:
        """지원하는 모든 포맷에 대해 검증이 통과하는지 확인한다."""
        for fmt in ("wav", "mp3", "m4a", "flac", "ogg", "webm"):
            audio_file = tmp_path / f"test.{fmt}"
            audio_file.write_bytes(b"\x00")
            converter._validate_input(audio_file)  # 에러 없이 통과

    def test_대문자_확장자도_정상_인식한다(
        self, converter: AudioConverter, tmp_path: Path
    ) -> None:
        """확장자가 대문자여도 정상적으로 인식하는지 확인한다."""
        audio_file = tmp_path / "test.MP3"
        audio_file.write_bytes(b"\x00")
        converter._validate_input(audio_file)  # 에러 없이 통과


class TestCheckFfmpeg:
    """ffmpeg 설치 여부 확인 테스트."""

    @patch("shutil.which", return_value=None)
    def test_ffmpeg_미설치_시_에러가_발생한다(
        self, mock_which: MagicMock, converter: AudioConverter
    ) -> None:
        """ffmpeg가 없을 때 FFmpegNotFoundError를 발생시킨다."""
        with pytest.raises(FFmpegNotFoundError, match="ffmpeg가 설치되어 있지 않습니다"):
            converter._check_ffmpeg_installed()

    @patch("shutil.which", side_effect=lambda x: "/usr/bin/ffmpeg" if x == "ffmpeg" else None)
    def test_ffprobe_미설치_시_에러가_발생한다(
        self, mock_which: MagicMock, converter: AudioConverter
    ) -> None:
        """ffprobe가 없을 때 FFmpegNotFoundError를 발생시킨다."""
        with pytest.raises(FFmpegNotFoundError, match="ffprobe가 설치되어 있지 않습니다"):
            converter._check_ffmpeg_installed()

    @patch("shutil.which", return_value="/usr/bin/mock")
    def test_둘_다_설치되면_정상_통과한다(
        self, mock_which: MagicMock, converter: AudioConverter
    ) -> None:
        """ffmpeg, ffprobe 모두 있으면 에러 없이 통과한다."""
        converter._check_ffmpeg_installed()


class TestProbe:
    """ffprobe 오디오 정보 조회 테스트."""

    def _make_probe_output(
        self,
        sample_rate: int = 44100,
        channels: int = 2,
        codec: str = "aac",
        duration: float = 120.5,
    ) -> str:
        """ffprobe JSON 출력을 생성하는 헬퍼 함수."""
        return json.dumps({
            "streams": [{
                "sample_rate": str(sample_rate),
                "channels": channels,
                "codec_name": codec,
                "duration": str(duration),
            }]
        })

    @patch("subprocess.run")
    def test_정상_프로브_결과를_파싱한다(
        self, mock_run: MagicMock, converter: AudioConverter, sample_audio: Path
    ) -> None:
        """ffprobe의 JSON 출력을 AudioInfo로 파싱하는지 확인한다."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=self._make_probe_output(44100, 2, "aac", 120.5),
        )
        info = converter.probe(sample_audio)
        assert info is not None
        assert info.sample_rate == 44100
        assert info.channels == 2
        assert info.codec == "aac"
        assert info.duration == 120.5

    @patch("subprocess.run")
    def test_프로브_실패_시_None을_반환한다(
        self, mock_run: MagicMock, converter: AudioConverter, sample_audio: Path
    ) -> None:
        """ffprobe가 실패하면 None을 반환하는지 확인한다."""
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        info = converter.probe(sample_audio)
        assert info is None

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ffprobe", timeout=30))
    def test_프로브_타임아웃_시_None을_반환한다(
        self, mock_run: MagicMock, converter: AudioConverter, sample_audio: Path
    ) -> None:
        """ffprobe 타임아웃 시 None을 반환하는지 확인한다."""
        info = converter.probe(sample_audio)
        assert info is None

    @patch("subprocess.run")
    def test_오디오_스트림_없으면_None을_반환한다(
        self, mock_run: MagicMock, converter: AudioConverter, sample_audio: Path
    ) -> None:
        """오디오 스트림이 없는 파일에 대해 None을 반환하는지 확인한다."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"streams": []}),
        )
        info = converter.probe(sample_audio)
        assert info is None


class TestIsAlreadyTargetFormat:
    """목표 포맷 확인 로직 테스트."""

    def test_16kHz_모노_PCM이면_True를_반환한다(
        self, converter: AudioConverter
    ) -> None:
        """이미 목표 포맷이면 True를 반환하는지 확인한다."""
        info = AudioInfo(
            sample_rate=16000, channels=1, codec="pcm_s16le", duration=60.0
        )
        assert converter._is_already_target_format(info) is True

    def test_다른_샘플레이트면_False를_반환한다(
        self, converter: AudioConverter
    ) -> None:
        """샘플레이트가 다르면 False를 반환하는지 확인한다."""
        info = AudioInfo(
            sample_rate=44100, channels=1, codec="pcm_s16le", duration=60.0
        )
        assert converter._is_already_target_format(info) is False

    def test_스테레오면_False를_반환한다(
        self, converter: AudioConverter
    ) -> None:
        """스테레오이면 False를 반환하는지 확인한다."""
        info = AudioInfo(
            sample_rate=16000, channels=2, codec="pcm_s16le", duration=60.0
        )
        assert converter._is_already_target_format(info) is False

    def test_다른_코덱이면_False를_반환한다(
        self, converter: AudioConverter
    ) -> None:
        """PCM이 아닌 코덱이면 False를 반환하는지 확인한다."""
        info = AudioInfo(
            sample_rate=16000, channels=1, codec="aac", duration=60.0
        )
        assert converter._is_already_target_format(info) is False

    def test_빅엔디안_PCM도_True를_반환한다(
        self, converter: AudioConverter
    ) -> None:
        """pcm_s16be도 목표 포맷으로 인정하는지 확인한다."""
        info = AudioInfo(
            sample_rate=16000, channels=1, codec="pcm_s16be", duration=60.0
        )
        assert converter._is_already_target_format(info) is True


class TestConvert:
    """오디오 변환 메인 로직 테스트."""

    @patch("shutil.which", return_value="/usr/bin/mock")
    @patch("subprocess.run")
    def test_정상_변환이_성공한다(
        self,
        mock_run: MagicMock,
        mock_which: MagicMock,
        converter: AudioConverter,
        sample_audio: Path,
        output_dir: Path,
    ) -> None:
        """정상적인 변환 플로우가 작동하는지 확인한다."""
        output_path = output_dir / "test_meeting_16k.wav"

        # PERF: mp3 포맷은 입력 ffprobe 건너뛰므로 ffmpeg + 출력 ffprobe 검증만 mock
        convert_result = MagicMock(returncode=0, stderr="")
        # 변환 후 무결성 검증용 ffprobe 호출
        post_probe_result = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "streams": [{
                    "sample_rate": "16000",
                    "channels": 1,
                    "codec_name": "pcm_s16le",
                    "duration": "60.0",
                }]
            }),
        )

        mock_run.side_effect = [convert_result, post_probe_result]

        # 출력 파일 생성 시뮬레이션
        output_path.write_bytes(b"\x00" * 512)

        result = converter.convert(sample_audio, output_dir)
        assert result == output_path
        assert mock_run.call_count == 2  # ffmpeg + 출력 ffprobe 검증

    @patch("shutil.which", return_value="/usr/bin/mock")
    @patch("subprocess.run")
    def test_이미_목표_포맷이면_변환을_건너뛴다(
        self,
        mock_run: MagicMock,
        mock_which: MagicMock,
        converter: AudioConverter,
        tmp_path: Path,
    ) -> None:
        """이미 16kHz 모노 PCM WAV이면 변환하지 않는지 확인한다."""
        wav_file = tmp_path / "already_ok.wav"
        wav_file.write_bytes(b"\x00" * 512)

        # ffprobe: 이미 목표 포맷
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "streams": [{
                    "sample_rate": "16000",
                    "channels": 1,
                    "codec_name": "pcm_s16le",
                    "duration": "60.0",
                }]
            }),
        )

        result = converter.convert(wav_file, tmp_path / "output")
        assert result == wav_file  # 원본 경로 반환
        assert mock_run.call_count == 1  # ffprobe만 호출

    @patch("shutil.which", return_value=None)
    def test_ffmpeg_미설치_시_에러가_발생한다(
        self,
        mock_which: MagicMock,
        converter: AudioConverter,
        sample_audio: Path,
        output_dir: Path,
    ) -> None:
        """ffmpeg가 없으면 FFmpegNotFoundError를 발생시킨다."""
        with pytest.raises(FFmpegNotFoundError):
            converter.convert(sample_audio, output_dir)

    def test_존재하지_않는_파일_변환_시_에러가_발생한다(
        self, converter: AudioConverter, output_dir: Path
    ) -> None:
        """존재하지 않는 파일 변환 시 FileNotFoundError를 발생시킨다."""
        with pytest.raises(FileNotFoundError):
            converter.convert(Path("/nonexistent.mp3"), output_dir)

    @patch("shutil.which", return_value="/usr/bin/mock")
    @patch("subprocess.run")
    def test_ffmpeg_실패_시_ConversionFailedError가_발생한다(
        self,
        mock_run: MagicMock,
        mock_which: MagicMock,
        converter: AudioConverter,
        sample_audio: Path,
        output_dir: Path,
    ) -> None:
        """ffmpeg 변환 실패 시 ConversionFailedError를 발생시킨다."""
        # PERF: mp3 포맷은 ffprobe를 건너뛰므로 ffmpeg 실패만 mock
        convert_result = MagicMock(returncode=1, stderr="변환 오류 발생")
        mock_run.side_effect = [convert_result]

        with pytest.raises(ConversionFailedError, match="ffmpeg 변환 실패"):
            converter.convert(sample_audio, output_dir)

    @patch("shutil.which", return_value="/usr/bin/mock")
    @patch("subprocess.run")
    def test_ffmpeg_타임아웃_시_ConversionFailedError가_발생한다(
        self,
        mock_run: MagicMock,
        mock_which: MagicMock,
        converter: AudioConverter,
        sample_audio: Path,
        output_dir: Path,
    ) -> None:
        """ffmpeg 변환 타임아웃 시 ConversionFailedError를 발생시킨다."""
        # PERF: mp3 포맷은 ffprobe를 건너뛰므로 ffmpeg 타임아웃만 mock
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=600),
        ]

        with pytest.raises(ConversionFailedError, match="타임아웃"):
            converter.convert(sample_audio, output_dir)

    @patch("shutil.which", return_value="/usr/bin/mock")
    @patch("subprocess.run")
    def test_출력_디렉토리가_자동_생성된다(
        self,
        mock_run: MagicMock,
        mock_which: MagicMock,
        converter: AudioConverter,
        sample_audio: Path,
        tmp_path: Path,
    ) -> None:
        """출력 디렉토리가 없으면 자동으로 생성하는지 확인한다."""
        nested_output = tmp_path / "a" / "b" / "c"
        output_path = nested_output / "test_meeting_16k.wav"

        # PERF: mp3 포맷은 입력 ffprobe 건너뛰므로 ffmpeg + 출력 ffprobe 검증 mock
        convert_result = MagicMock(returncode=0, stderr="")
        post_probe_result = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "streams": [{
                    "sample_rate": "16000",
                    "channels": 1,
                    "codec_name": "pcm_s16le",
                    "duration": "60.0",
                }]
            }),
        )
        mock_run.side_effect = [convert_result, post_probe_result]

        # 출력 파일 생성 시뮬레이션
        nested_output.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\x00" * 512)

        result = converter.convert(sample_audio, nested_output)
        assert result == output_path
        assert nested_output.exists()

    @patch("shutil.which", return_value="/usr/bin/mock")
    @patch("subprocess.run")
    def test_커스텀_출력_파일명을_사용할_수_있다(
        self,
        mock_run: MagicMock,
        mock_which: MagicMock,
        converter: AudioConverter,
        sample_audio: Path,
        output_dir: Path,
    ) -> None:
        """output_filename 매개변수로 출력 파일명을 지정할 수 있는지 확인한다."""
        custom_name = "회의_2026_03_04.wav"
        output_path = output_dir / custom_name

        # PERF: 비-WAV 포맷(mp3)은 입력 ffprobe를 건너뛰므로 ffmpeg + 출력 ffprobe 검증 mock
        convert_result = MagicMock(returncode=0, stderr="")
        post_probe_result = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "streams": [{
                    "sample_rate": "16000",
                    "channels": 1,
                    "codec_name": "pcm_s16le",
                    "duration": "60.0",
                }]
            }),
        )
        mock_run.side_effect = [convert_result, post_probe_result]

        # 출력 파일 생성 시뮬레이션
        output_path.write_bytes(b"\x00" * 512)

        result = converter.convert(sample_audio, output_dir, output_filename=custom_name)
        assert result == output_path
        assert result.name == custom_name

    @patch("shutil.which", return_value="/usr/bin/mock")
    @patch("subprocess.run")
    def test_빈_출력_파일은_에러로_처리한다(
        self,
        mock_run: MagicMock,
        mock_which: MagicMock,
        converter: AudioConverter,
        sample_audio: Path,
        output_dir: Path,
    ) -> None:
        """변환 후 출력 파일이 0바이트면 ConversionFailedError를 발생시킨다."""
        output_path = output_dir / "test_meeting_16k.wav"

        # PERF: 비-WAV 포맷(mp3)은 ffprobe를 건너뛰므로 변환 결과만 모킹
        convert_result = MagicMock(returncode=0, stderr="")
        mock_run.side_effect = [convert_result]

        # 빈 파일 생성
        output_path.write_bytes(b"")

        with pytest.raises(ConversionFailedError, match="파일 크기가 0"):
            converter.convert(sample_audio, output_dir)


class TestConvertAsync:
    """비동기 변환 래퍼 테스트."""

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/mock")
    @patch("subprocess.run")
    async def test_비동기_변환이_동작한다(
        self,
        mock_run: MagicMock,
        mock_which: MagicMock,
        converter: AudioConverter,
        sample_audio: Path,
        output_dir: Path,
    ) -> None:
        """convert_async가 정상적으로 동작하는지 확인한다."""
        output_path = output_dir / "test_meeting_16k.wav"

        # PERF: 비-WAV 포맷(mp3)은 입력 ffprobe를 건너뛰므로 ffmpeg + 출력 ffprobe 검증 mock
        convert_result = MagicMock(returncode=0, stderr="")
        post_probe_result = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "streams": [{
                    "sample_rate": "16000",
                    "channels": 1,
                    "codec_name": "pcm_s16le",
                    "duration": "60.0",
                }]
            }),
        )
        mock_run.side_effect = [convert_result, post_probe_result]

        # 출력 파일 생성 시뮬레이션
        output_path.write_bytes(b"\x00" * 512)

        result = await converter.convert_async(sample_audio, output_dir)
        assert result == output_path


class TestErrorHierarchy:
    """에러 클래스 계층 구조 테스트."""

    def test_모든_에러는_AudioConvertError의_하위_클래스이다(self) -> None:
        """커스텀 에러가 올바른 계층 구조를 갖는지 확인한다."""
        assert issubclass(FFmpegNotFoundError, AudioConvertError)
        assert issubclass(UnsupportedFormatError, AudioConvertError)
        assert issubclass(ConversionFailedError, AudioConvertError)

    def test_AudioConvertError는_Exception의_하위_클래스이다(self) -> None:
        """AudioConvertError가 Exception을 상속하는지 확인한다."""
        assert issubclass(AudioConvertError, Exception)
