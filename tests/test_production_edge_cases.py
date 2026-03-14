"""
프로덕션 엣지케이스 테스트 (Production Edge Case Tests)

목적: QA 위원회가 식별한 프로덕션 환경 엣지케이스를 검증한다.
테스트 범위:
    - VAD 0개 구간 시 파이프라인 전체 오디오 폴백 (#39)
    - 장시간 오디오 메모리 안전성 (#40)
    - 환각 필터 + 텍스트 후처리 파이프라인 통합 (#41)
의존성: pytest, pytest-asyncio, unittest.mock
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


# === Mock 데이터 클래스 ===


@dataclass
class MockSegment:
    """테스트용 전사 세그먼트."""

    text: str
    start: float = 0.0
    end: float = 1.0
    avg_logprob: float = -0.3
    no_speech_prob: float = 0.05
    compression_ratio: float = 1.2


@dataclass
class MockFilterConfig:
    """테스트용 환각 필터 설정."""

    enabled: bool = True
    compression_ratio_threshold: float = 2.4
    logprob_threshold: float = -1.0
    no_speech_threshold: float = 0.6
    repetition_threshold: int = 3


@dataclass
class MockPostprocessConfig:
    """테스트용 텍스트 후처리 설정."""

    enabled: bool = True


# === Task #39: VAD 0개 구간 엣지케이스 ===


class TestVAD_Zero_Segments_Pipeline:
    """VAD가 0개 음성 구간을 반환할 때 파이프라인 동작 검증."""

    def _make_pipeline(self, vad_enabled: bool = True) -> MagicMock:
        """VAD 설정이 포함된 파이프라인 Mock을 생성한다."""
        from core.pipeline import PipelineManager

        config = MagicMock()
        config.pipeline.checkpoint_enabled = False
        config.pipeline.retry_max_count = 1
        config.pipeline.peak_ram_limit_gb = 9.5
        config.vad.enabled = vad_enabled
        config.vad.threshold = 0.5
        config.vad.min_speech_duration_ms = 250
        config.vad.min_silence_duration_ms = 100
        config.vad.speech_pad_ms = 30
        # 환각 필터/후처리 비활성
        config.hallucination_filter.enabled = False
        config.text_postprocessing.enabled = False

        manager = MagicMock()
        pipeline = PipelineManager(config, manager)
        return pipeline

    async def test_VAD_None_반환시_전체_오디오_전사(self, tmp_path: Path) -> None:
        """VAD가 None을 반환하면 vad_clip_timestamps=None으로 전사한다."""
        pipeline = self._make_pipeline(vad_enabled=True)
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"\x00" * 1024)
        checkpoint_path = tmp_path / "transcribe.json"

        mock_transcript = MagicMock()
        mock_transcript.segments = [MagicMock(text="안녕하세요", start=0.0, end=2.0)]
        mock_transcript.full_text = "안녕하세요"
        mock_transcript.save_checkpoint = MagicMock()

        mock_vad = MagicMock()
        mock_vad.detect = AsyncMock(return_value=None)

        mock_transcriber = MagicMock()
        mock_transcriber.transcribe = AsyncMock(return_value=mock_transcript)

        with (
            patch("steps.vad_detector.VoiceActivityDetector", return_value=mock_vad),
            patch("steps.transcriber.Transcriber", return_value=mock_transcriber),
        ):
            result = await pipeline._run_step_transcribe(wav_path, checkpoint_path)

        # VAD가 None → transcribe에 vad_clip_timestamps=None 전달
        call_kwargs = mock_transcriber.transcribe.call_args
        assert call_kwargs is not None
        # vad_clip_timestamps가 None이어야 함 (전체 오디오 처리)
        vad_ts = call_kwargs.kwargs.get(
            "vad_clip_timestamps",
            call_kwargs.args[1] if len(call_kwargs.args) > 1 else None,
        )
        assert vad_ts is None
        assert result == mock_transcript

    async def test_VAD_예외시_전체_오디오_폴백(self, tmp_path: Path) -> None:
        """VAD에서 예외 발생 시 전체 오디오로 폴백하여 전사를 계속한다."""
        pipeline = self._make_pipeline(vad_enabled=True)
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"\x00" * 1024)
        checkpoint_path = tmp_path / "transcribe.json"

        mock_transcript = MagicMock()
        mock_transcript.segments = [MagicMock(text="정상 전사", start=0.0, end=3.0)]
        mock_transcript.full_text = "정상 전사"
        mock_transcript.save_checkpoint = MagicMock()

        mock_vad = MagicMock()
        mock_vad.detect = AsyncMock(side_effect=RuntimeError("VAD 모델 로드 실패"))

        mock_transcriber = MagicMock()
        mock_transcriber.transcribe = AsyncMock(return_value=mock_transcript)

        with (
            patch("steps.vad_detector.VoiceActivityDetector", return_value=mock_vad),
            patch("steps.transcriber.Transcriber", return_value=mock_transcriber),
        ):
            result = await pipeline._run_step_transcribe(wav_path, checkpoint_path)

        # 예외에도 불구하고 전사가 실행됨
        mock_transcriber.transcribe.assert_called_once()
        assert result.full_text == "정상 전사"

    async def test_VAD_비활성시_바로_전사(self, tmp_path: Path) -> None:
        """VAD가 비활성화되어 있으면 VAD를 건너뛰고 전사한다."""
        pipeline = self._make_pipeline(vad_enabled=False)
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"\x00" * 1024)
        checkpoint_path = tmp_path / "transcribe.json"

        mock_transcript = MagicMock()
        mock_transcript.segments = [MagicMock(text="전체 오디오 전사")]
        mock_transcript.full_text = "전체 오디오 전사"
        mock_transcript.save_checkpoint = MagicMock()

        mock_transcriber = MagicMock()
        mock_transcriber.transcribe = AsyncMock(return_value=mock_transcript)

        with patch(
            "steps.transcriber.Transcriber", return_value=mock_transcriber
        ) as mock_cls:
            result = await pipeline._run_step_transcribe(wav_path, checkpoint_path)

        mock_transcriber.transcribe.assert_called_once()
        assert result.full_text == "전체 오디오 전사"

    async def test_VAD_config_없을때_전사_진행(self, tmp_path: Path) -> None:
        """config에 vad 속성이 없을 때도 전사가 진행된다."""
        from core.pipeline import PipelineManager

        # MagicMock에서 vad만 None으로 설정 (getattr 시 None 반환)
        config = MagicMock()
        config.pipeline.checkpoint_enabled = False
        config.pipeline.retry_max_count = 1
        config.pipeline.peak_ram_limit_gb = 9.5
        # vad를 None으로 → getattr(config, "vad", None)이 None 반환
        config.vad = None
        config.hallucination_filter.enabled = False
        config.text_postprocessing.enabled = False

        manager = MagicMock()
        pipeline = PipelineManager(config, manager)

        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"\x00" * 1024)
        checkpoint_path = tmp_path / "transcribe.json"

        mock_transcript = MagicMock()
        mock_transcript.segments = [MagicMock(text="정상")]
        mock_transcript.full_text = "정상"
        mock_transcript.save_checkpoint = MagicMock()

        mock_transcriber = MagicMock()
        mock_transcriber.transcribe = AsyncMock(return_value=mock_transcript)

        with patch(
            "steps.transcriber.Transcriber", return_value=mock_transcriber
        ):
            result = await pipeline._run_step_transcribe(wav_path, checkpoint_path)

        assert result.full_text == "정상"


# === Task #40: 장시간 오디오 메모리 안전성 ===


class TestLongAudioMemorySafety:
    """장시간 오디오 처리 시 메모리 안전성 검증."""

    def _make_guard(self, tmp_path: Path) -> "ResourceGuard":
        """테스트용 ResourceGuard를 생성한다."""
        from core.pipeline import ResourceGuard

        config = MagicMock()
        config.pipeline.min_disk_free_gb = 1.0
        config.pipeline.min_memory_free_gb = 2.0
        config.paths.resolved_base_dir = tmp_path

        return ResourceGuard(config)

    def test_리소스_모니터_디스크_부족_감지(self, tmp_path: Path) -> None:
        """디스크 여유 공간이 부족하면 리소스 체크가 실패한다."""
        guard = self._make_guard(tmp_path)

        # 디스크 여유 0.5GB (1.0GB 미만)
        with patch("shutil.disk_usage") as mock_disk:
            mock_disk.return_value = MagicMock(free=int(0.5 * 1024**3))
            ok, free_gb = guard.check_disk()
            assert ok is False

    def test_리소스_모니터_메모리_부족_감지(self, tmp_path: Path) -> None:
        """가용 메모리가 부족하면 리소스 체크가 실패한다."""
        guard = self._make_guard(tmp_path)

        # 가용 메모리 1GB (2GB 미만)
        with patch("psutil.virtual_memory") as mock_mem:
            mock_mem.return_value = MagicMock(available=int(1 * 1024**3))
            ok, available_gb = guard.check_memory()
            assert ok is False

    def test_리소스_모니터_정상_상태(self, tmp_path: Path) -> None:
        """디스크와 메모리가 모두 충분하면 all_ok=True이다."""
        guard = self._make_guard(tmp_path)

        with (
            patch("shutil.disk_usage") as mock_disk,
            patch("psutil.virtual_memory") as mock_mem,
        ):
            mock_disk.return_value = MagicMock(free=int(10 * 1024**3))
            mock_mem.return_value = MagicMock(available=int(4 * 1024**3))
            status = guard.check_all()
            assert status.all_ok is True
            assert status.disk_ok is True
            assert status.memory_ok is True

    def test_peak_ram_limit_config_설정값(self) -> None:
        """peak_ram_limit_gb 설정이 파이프라인에 올바르게 전달된다."""
        from core.pipeline import PipelineManager

        config = MagicMock()
        config.pipeline.checkpoint_enabled = False
        config.pipeline.retry_max_count = 1
        config.pipeline.peak_ram_limit_gb = 9.5
        config.pipeline.min_disk_free_gb = 1.0
        config.pipeline.min_memory_free_gb = 2.0
        config.pipeline.skip_llm_steps = False

        manager = MagicMock()
        pipeline = PipelineManager(config, manager)

        assert pipeline._config.pipeline.peak_ram_limit_gb == 9.5

    def test_대용량_세그먼트_리스트_필터링(self) -> None:
        """수천 개 세그먼트의 환각 필터링이 정상 동작한다."""
        from steps.hallucination_filter import filter_hallucinations

        # 4시간 분량 ~ 약 1440개 세그먼트 (10초 간격)
        # 숫자 반복이 없도록 한글 키워드를 번갈아 사용
        keywords = ["회의", "보고", "논의", "검토", "결정", "분석", "계획", "진행"]
        segments = [
            MockSegment(
                text=f"{keywords[i % len(keywords)]} 안건 진행 중입니다",
                start=i * 10.0,
                end=(i + 1) * 10.0,
                avg_logprob=-0.3,
                no_speech_prob=0.05,
                compression_ratio=1.2,
            )
            for i in range(1440)
        ]
        # 몇 개의 환각 세그먼트 삽입
        segments[100] = MockSegment(
            text="무음 구간", start=1000.0, end=1010.0,
            no_speech_prob=0.9,
        )
        segments[500] = MockSegment(
            text="확확확확확확확", start=5000.0, end=5010.0,
        )
        segments[1000] = MockSegment(
            text="저신뢰 전사", start=10000.0, end=10010.0,
            avg_logprob=-2.0,
        )

        config = MagicMock()
        config.hallucination_filter = MockFilterConfig()

        filtered, removed = filter_hallucinations(segments, config)

        # 3개 환각 제거, 나머지 유지
        assert len(removed) == 3
        assert len(filtered) == 1437


# === Task #41: 환각 필터 + 텍스트 후처리 파이프라인 통합 ===


class TestHallucinationFilterPostprocessIntegration:
    """환각 필터링 → 텍스트 후처리 순차 실행 통합 검증."""

    def test_필터링_후_후처리_순차_적용(self) -> None:
        """환각 필터링 후 텍스트 후처리가 순차적으로 적용된다."""
        from steps.hallucination_filter import filter_hallucinations
        from steps.text_postprocessor import postprocess_segments

        segments = [
            MockSegment(text="정상  발화입니다", start=0.0, end=2.0),
            MockSegment(
                text="환각 환각 환각 환각",
                start=2.0, end=4.0,
            ),
            MockSegment(text="두번째\n발화", start=4.0, end=6.0),
        ]

        config = MagicMock()
        config.hallucination_filter = MockFilterConfig()
        config.text_postprocessing = MockPostprocessConfig()

        # 1단계: 환각 필터링
        filtered, removed = filter_hallucinations(segments, config)
        assert len(removed) == 1  # "환각 환각 환각 환각" 제거
        assert len(filtered) == 2

        # 2단계: 텍스트 후처리
        processed = postprocess_segments(filtered, config)
        assert len(processed) == 2
        # 연속 공백 정규화: "정상  발화입니다" → "정상 발화입니다"
        assert processed[0].text == "정상 발화입니다"
        # 줄바꿈 → 공백: "두번째\n발화" → "두번째 발화"
        assert processed[1].text == "두번째 발화"

    def test_모든_세그먼트_환각시_후처리_빈_리스트(self) -> None:
        """모든 세그먼트가 환각이면 후처리에 빈 리스트가 전달된다."""
        from steps.hallucination_filter import filter_hallucinations
        from steps.text_postprocessor import postprocess_segments

        segments = [
            MockSegment(
                text="무음", start=0.0, end=2.0,
                no_speech_prob=0.9,
            ),
            MockSegment(
                text="저신뢰", start=2.0, end=4.0,
                avg_logprob=-2.0,
            ),
        ]

        config = MagicMock()
        config.hallucination_filter = MockFilterConfig()
        config.text_postprocessing = MockPostprocessConfig()

        filtered, removed = filter_hallucinations(segments, config)
        assert len(filtered) == 0
        assert len(removed) == 2

        processed = postprocess_segments(filtered, config)
        assert len(processed) == 0

    def test_필터_비활성시_후처리만_적용(self) -> None:
        """환각 필터 비활성 시 후처리만 적용된다."""
        from steps.hallucination_filter import filter_hallucinations
        from steps.text_postprocessor import postprocess_segments

        segments = [
            MockSegment(
                text="  공백  많은  텍스트  ", start=0.0, end=2.0,
                no_speech_prob=0.9,  # 필터 비활성이므로 제거 안됨
            ),
        ]

        config = MagicMock()
        config.hallucination_filter = MockFilterConfig(enabled=False)
        config.text_postprocessing = MockPostprocessConfig()

        filtered, removed = filter_hallucinations(segments, config)
        assert len(filtered) == 1  # 필터 비활성 → 원본 유지
        assert len(removed) == 0

        processed = postprocess_segments(filtered, config)
        assert len(processed) == 1
        assert processed[0].text == "공백 많은 텍스트"

    def test_후처리에서_빈_텍스트_세그먼트_제거(self) -> None:
        """후처리 후 텍스트가 비어지는 세그먼트는 제거된다."""
        from steps.text_postprocessor import postprocess_segments

        # 환각 필터 단계를 거친 후의 세그먼트라고 가정 (필터 통과한 것들)
        segments = [
            MockSegment(text="정상 발화", start=0.0, end=2.0),
            MockSegment(text="   ", start=2.0, end=4.0),  # 공백만
            MockSegment(text="\n\t\r", start=4.0, end=6.0),  # 제어문자만
        ]

        config = MagicMock()
        config.text_postprocessing = MockPostprocessConfig()

        # 후처리만 직접 실행하여 빈 텍스트 제거 검증
        processed = postprocess_segments(segments, config)
        assert len(processed) == 1
        assert processed[0].text == "정상 발화"

    async def test_파이프라인_전사_단계_통합흐름(self, tmp_path: Path) -> None:
        """파이프라인의 _run_step_transcribe에서 필터+후처리가 순차 실행된다."""
        from core.pipeline import PipelineManager

        config = MagicMock()
        config.pipeline.checkpoint_enabled = False
        config.pipeline.retry_max_count = 1
        config.pipeline.peak_ram_limit_gb = 9.5
        config.vad.enabled = False  # VAD 비활성으로 단순화
        config.hallucination_filter = MockFilterConfig()
        config.text_postprocessing = MockPostprocessConfig()

        manager = MagicMock()
        pipeline = PipelineManager(config, manager)

        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"\x00" * 1024)
        checkpoint_path = tmp_path / "transcribe.json"

        # 전사 결과에 환각+공백 포함 세그먼트
        mock_segments = [
            MockSegment(text="정상  발화입니다", start=0.0, end=2.0),
            MockSegment(
                text="감사 감사 감사 감사",
                start=2.0, end=4.0,
            ),
            MockSegment(text="  ", start=4.0, end=6.0),
        ]

        mock_transcript = MagicMock()
        mock_transcript.segments = list(mock_segments)
        mock_transcript.full_text = "정상  발화입니다 감사 감사 감사 감사   "
        mock_transcript.save_checkpoint = MagicMock()

        mock_transcriber = MagicMock()
        mock_transcriber.transcribe = AsyncMock(return_value=mock_transcript)

        with patch(
            "steps.transcriber.Transcriber", return_value=mock_transcriber
        ):
            result = await pipeline._run_step_transcribe(wav_path, checkpoint_path)

        # 환각 제거("감사 감사 감사 감사") + 후처리(공백 정규화 + 빈 세그먼트 제거)
        # result.segments가 업데이트됨
        remaining_texts = [seg.text for seg in result.segments]
        # "감사 감사 감사 감사"는 환각 필터에서 제거
        assert "감사 감사 감사 감사" not in remaining_texts
        # "  " (공백만)은 후처리에서 제거
        # "정상  발화입니다"는 후처리에서 "정상 발화입니다"로 정규화
        assert "정상 발화입니다" in remaining_texts

    def test_NFC_유니코드_정규화_통합(self) -> None:
        """한글 자모 조합형이 NFC 정규화된다."""
        from steps.hallucination_filter import filter_hallucinations
        from steps.text_postprocessor import postprocess_segments

        # NFD 형식의 한글 (조합형 자모)
        import unicodedata

        nfd_text = unicodedata.normalize("NFD", "안녕하세요")

        segments = [
            MockSegment(text=nfd_text, start=0.0, end=2.0),
        ]

        config = MagicMock()
        config.hallucination_filter = MockFilterConfig()
        config.text_postprocessing = MockPostprocessConfig()

        filtered, _ = filter_hallucinations(segments, config)
        processed = postprocess_segments(filtered, config)

        assert len(processed) == 1
        # NFC 정규화 적용 확인
        assert processed[0].text == unicodedata.normalize("NFC", "안녕하세요")
        assert processed[0].text == "안녕하세요"
