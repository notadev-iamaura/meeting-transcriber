"""
STT 벤치마크 스크립트 단위 테스트

벤치마크 스크립트의 핵심 로직을 검증한다:
- 텍스트 정규화 (normalize_korean)
- CER/WER 계산
- 프로바이더 인터페이스
- 결과 리포트 생성
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import jiwer
import pytest

# 프로젝트 루트를 PYTHONPATH에 추가
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.benchmark_stt import (
    BenchmarkMetrics,
    LocalSTTProvider,
    OpenAISTTProvider,
    OpenRouterSTTProvider,
    TranscriptionResult,
    normalize_korean,
    run_benchmark,
    save_results,
)


# ============================================================
# 텍스트 정규화 테스트
# ============================================================


class TestNormalizeKorean:
    """한국어 텍스트 정규화 함수 테스트"""

    def test_nfc_normalization(self) -> None:
        """NFD → NFC 정규화가 올바르게 작동한다."""
        # NFD (분리형): ㅎ+ㅏ+ㄴ+ㄱ+ㅜ+ㄱ
        nfd_text = "\u1112\u1161\u11ab\u1100\u116e\u11a8"
        result = normalize_korean(nfd_text)
        assert "한국" in result

    def test_punctuation_removal(self) -> None:
        """구두점이 제거된다."""
        text = "안녕하세요, 반갑습니다! 잘 부탁드립니다."
        result = normalize_korean(text)
        assert "," not in result
        assert "!" not in result
        assert "." not in result
        assert "안녕하세요" in result

    def test_whitespace_normalization(self) -> None:
        """연속 공백이 단일 공백으로 변환된다."""
        text = "안녕   하세요    반갑습니다"
        result = normalize_korean(text)
        assert "  " not in result
        assert result == "안녕 하세요 반갑습니다"

    def test_lowercase_english(self) -> None:
        """영문이 소문자로 변환된다."""
        text = "Hello World 안녕"
        result = normalize_korean(text)
        assert "hello" in result
        assert "Hello" not in result

    def test_empty_string(self) -> None:
        """빈 문자열은 빈 문자열을 반환한다."""
        assert normalize_korean("") == ""
        assert normalize_korean("   ") == ""

    def test_numbers_preserved(self) -> None:
        """숫자는 보존된다."""
        text = "2024년 3월 10일"
        result = normalize_korean(text)
        assert "2024" in result
        assert "3" in result
        assert "10" in result


# ============================================================
# CER/WER 계산 테스트
# ============================================================


class TestMetricsCalculation:
    """CER/WER 지표 계산 검증"""

    def test_perfect_match_cer(self) -> None:
        """동일한 텍스트의 CER은 0이다."""
        ref = normalize_korean("안녕하세요 반갑습니다")
        hyp = normalize_korean("안녕하세요 반갑습니다")
        assert jiwer.cer(ref, hyp) == pytest.approx(0.0)

    def test_perfect_match_wer(self) -> None:
        """동일한 텍스트의 WER은 0이다."""
        ref = normalize_korean("안녕하세요 반갑습니다")
        hyp = normalize_korean("안녕하세요 반갑습니다")
        assert jiwer.wer(ref, hyp) == pytest.approx(0.0)

    def test_one_char_error_cer(self) -> None:
        """한 글자 오류의 CER이 올바르게 계산된다."""
        ref = "안녕하세요"  # 5글자
        hyp = "안녕하세오"  # 1글자 다름
        cer = jiwer.cer(ref, hyp)
        # CER = 대체 1 / 전체 5 = 0.2
        assert 0.1 < cer < 0.3

    def test_completely_wrong(self) -> None:
        """완전히 다른 텍스트의 CER은 높다."""
        ref = "안녕하세요"
        hyp = "감사합니다"
        cer = jiwer.cer(ref, hyp)
        assert cer > 0.5

    def test_normalization_helps_cer(self) -> None:
        """정규화가 구두점 차이로 인한 CER을 낮춘다."""
        ref_raw = "안녕하세요, 반갑습니다!"
        hyp_raw = "안녕하세요 반갑습니다"
        cer_raw = jiwer.cer(ref_raw, hyp_raw)

        ref_norm = normalize_korean(ref_raw)
        hyp_norm = normalize_korean(hyp_raw)
        cer_norm = jiwer.cer(ref_norm, hyp_norm)

        # 정규화 후 CER이 더 낮아야 함 (구두점 차이 제거)
        assert cer_norm <= cer_raw


# ============================================================
# 데이터 클래스 테스트
# ============================================================


class TestDataClasses:
    """데이터 클래스 기본 기능 테스트"""

    def test_transcription_result_creation(self) -> None:
        """TranscriptionResult 생성이 올바르게 작동한다."""
        result = TranscriptionResult(
            sample_id="test_001",
            reference="안녕하세요",
            hypothesis="안녕하세요",
            audio_duration=3.5,
            processing_time=1.2,
        )
        assert result.sample_id == "test_001"
        assert result.audio_duration == 3.5

    def test_benchmark_metrics_creation(self) -> None:
        """BenchmarkMetrics 생성이 올바르게 작동한다."""
        metrics = BenchmarkMetrics(
            provider="테스트",
            model="test-model",
            total_samples=10,
            failed_samples=1,
            cer=0.06,
            wer=0.12,
            avg_processing_time=2.0,
            total_audio_duration=50.0,
            rtf=0.4,
        )
        assert metrics.cer == 0.06
        assert metrics.failed_samples == 1
        assert metrics.results == []


# ============================================================
# 프로바이더 테스트
# ============================================================


class TestLocalSTTProvider:
    """로컬 STT 프로바이더 단위 테스트"""

    def test_init(self) -> None:
        """LocalSTTProvider 초기화가 올바르다."""
        provider = LocalSTTProvider(
            model_name="test-model",
            language="ko",
        )
        assert provider.model_name == "test-model"
        assert provider.language == "ko"
        assert provider._whisper is None

    @patch.dict(sys.modules, {"mlx_whisper": MagicMock()})
    def test_load_model(self) -> None:
        """mlx-whisper 모듈 로드가 작동한다."""
        provider = LocalSTTProvider(model_name="test-model")
        provider._load_model()
        assert provider._whisper is not None

    @patch.dict(sys.modules, {"mlx_whisper": MagicMock()})
    def test_transcribe_calls_whisper(self, tmp_path: Path) -> None:
        """transcribe가 mlx_whisper.transcribe를 호출한다."""
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 1024)

        mock_whisper = MagicMock()
        mock_whisper.transcribe.return_value = {"text": "안녕하세요"}

        provider = LocalSTTProvider(model_name="test-model")
        provider._whisper = mock_whisper

        result = provider.transcribe(audio_file)
        assert result == "안녕하세요"
        mock_whisper.transcribe.assert_called_once()

    @patch.dict(sys.modules, {"mlx_whisper": MagicMock()})
    def test_transcribe_beam_fallback(self, tmp_path: Path) -> None:
        """beam search 실패 시 greedy decoding으로 폴백한다."""
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 1024)

        mock_whisper = MagicMock()
        # 첫 번째 호출: beam search NotImplementedError
        # 두 번째 호출: greedy decoding 성공
        mock_whisper.transcribe.side_effect = [
            NotImplementedError("beam search not supported"),
            {"text": "폴백 결과"},
        ]

        provider = LocalSTTProvider(model_name="test-model")
        provider._whisper = mock_whisper

        result = provider.transcribe(audio_file)
        assert result == "폴백 결과"
        assert mock_whisper.transcribe.call_count == 2


class TestOpenAISTTProvider:
    """OpenAI STT 프로바이더 단위 테스트"""

    def test_init(self) -> None:
        """OpenAISTTProvider 초기화가 올바르다."""
        provider = OpenAISTTProvider(api_key="test-key", model="whisper-1")
        assert provider.model == "whisper-1"
        assert provider.base_url == "https://api.openai.com/v1"
        provider.close()

    @patch("httpx.Client.post")
    def test_transcribe_calls_api(self, mock_post: MagicMock, tmp_path: Path) -> None:
        """transcribe가 OpenAI API를 올바르게 호출한다."""
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 1024)

        mock_response = MagicMock()
        mock_response.json.return_value = {"text": "API 전사 결과"}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        provider = OpenAISTTProvider(api_key="test-key")
        result = provider.transcribe(audio_file)

        assert result == "API 전사 결과"
        mock_post.assert_called_once()
        # 호출 URL 검증
        call_args = mock_post.call_args
        assert "/audio/transcriptions" in call_args[0][0]
        provider.close()


class TestOpenRouterSTTProvider:
    """OpenRouter STT 프로바이더 단위 테스트"""

    def test_init(self) -> None:
        """OpenRouterSTTProvider 초기화가 올바르다."""
        provider = OpenRouterSTTProvider(api_key="test-key")
        assert provider.base_url == "https://openrouter.ai/api/v1"
        provider.close()

    @patch("httpx.Client.post")
    def test_transcribe_sends_base64_audio(
        self, mock_post: MagicMock, tmp_path: Path,
    ) -> None:
        """transcribe가 base64 인코딩된 오디오를 전송한다."""
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "OpenRouter 전사 결과"}}],
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        provider = OpenRouterSTTProvider(api_key="test-key")
        result = provider.transcribe(audio_file)

        assert result == "OpenRouter 전사 결과"
        # chat/completions 엔드포인트 확인
        call_args = mock_post.call_args
        assert "/chat/completions" in call_args[0][0]
        # base64 인코딩 확인
        body = call_args[1]["json"]
        audio_content = body["messages"][0]["content"][0]
        assert audio_content["type"] == "input_audio"
        provider.close()


# ============================================================
# 벤치마크 실행 테스트
# ============================================================


class TestRunBenchmark:
    """run_benchmark 함수 테스트"""

    def test_successful_benchmark(self) -> None:
        """정상적인 벤치마크 실행이 올바른 결과를 반환한다."""
        mock_provider = MagicMock()
        mock_provider.transcribe.return_value = "안녕하세요"

        samples = [
            {
                "id": "test_001",
                "audio_path": Path("/fake/audio.wav"),
                "reference": "안녕하세요",
                "duration": 2.0,
            },
            {
                "id": "test_002",
                "audio_path": Path("/fake/audio2.wav"),
                "reference": "반갑습니다",
                "duration": 1.5,
            },
        ]

        metrics = run_benchmark(mock_provider, "테스트", "test-model", samples)

        assert metrics.total_samples == 2
        assert metrics.failed_samples == 0
        assert metrics.provider == "테스트"
        # 첫 번째 샘플은 완벽 일치, 두 번째는 불일치
        assert metrics.cer < 1.0  # 전체가 틀리진 않음

    def test_failed_samples_counted(self) -> None:
        """실패한 샘플이 올바르게 카운트된다."""
        mock_provider = MagicMock()
        mock_provider.transcribe.side_effect = [
            "성공 결과",
            RuntimeError("API 오류"),
        ]

        samples = [
            {"id": "s1", "audio_path": Path("/a.wav"), "reference": "성공 결과", "duration": 1.0},
            {"id": "s2", "audio_path": Path("/b.wav"), "reference": "실패", "duration": 1.0},
        ]

        metrics = run_benchmark(mock_provider, "테스트", "model", samples)

        assert metrics.total_samples == 1
        assert metrics.failed_samples == 1

    def test_all_failed_returns_cer_1(self) -> None:
        """모든 샘플 실패 시 CER=1.0을 반환한다."""
        mock_provider = MagicMock()
        mock_provider.transcribe.side_effect = RuntimeError("전부 실패")

        samples = [
            {"id": "s1", "audio_path": Path("/a.wav"), "reference": "테스트", "duration": 1.0},
        ]

        metrics = run_benchmark(mock_provider, "테스트", "model", samples)

        assert metrics.total_samples == 0
        assert metrics.cer == 1.0


# ============================================================
# 결과 저장 테스트
# ============================================================


class TestSaveResults:
    """결과 JSON 저장 테스트"""

    def test_save_results_creates_json(self, tmp_path: Path) -> None:
        """save_results가 유효한 JSON 파일을 생성한다."""
        metrics = BenchmarkMetrics(
            provider="테스트",
            model="test-model",
            total_samples=1,
            failed_samples=0,
            cer=0.05,
            wer=0.10,
            avg_processing_time=1.0,
            total_audio_duration=3.0,
            rtf=0.33,
            results=[
                TranscriptionResult(
                    sample_id="s1",
                    reference="안녕하세요",
                    hypothesis="안녕하세요",
                    audio_duration=3.0,
                    processing_time=1.0,
                ),
            ],
        )

        output_path = tmp_path / "results.json"
        save_results([metrics], output_path)

        assert output_path.exists()
        data = json.loads(output_path.read_text(encoding="utf-8"))
        assert "benchmark_info" in data
        assert len(data["results"]) == 1
        assert data["results"][0]["metrics"]["cer"] == 0.05

    def test_save_results_korean_text(self, tmp_path: Path) -> None:
        """한국어 텍스트가 유니코드 이스케이프 없이 저장된다."""
        metrics = BenchmarkMetrics(
            provider="로컬",
            model="test",
            total_samples=1,
            failed_samples=0,
            cer=0.0,
            wer=0.0,
            avg_processing_time=0.5,
            total_audio_duration=1.0,
            rtf=0.5,
            results=[
                TranscriptionResult(
                    sample_id="s1",
                    reference="한국어 테스트",
                    hypothesis="한국어 테스트",
                    audio_duration=1.0,
                    processing_time=0.5,
                ),
            ],
        )

        output_path = tmp_path / "results.json"
        save_results([metrics], output_path)

        raw_text = output_path.read_text(encoding="utf-8")
        # ensure_ascii=False 확인
        assert "한국어" in raw_text
        assert "\\u" not in raw_text
