"""
회의록 생성기 테스트 모듈 (Summarizer Test Module)

목적: steps/summarizer.py의 Summarizer 클래스와 관련 유틸리티 함수를 검증한다.
주요 테스트:
    - 정상 단일 요약 생성
    - 긴 전사문 분할 요약 (청킹)
    - Ollama 연결 실패 / 타임아웃 → 폴백 회의록 생성
    - 빈 입력 처리 (EmptySummaryInputError)
    - 체크포인트 저장/복원 라운드트립
    - 마크다운 파일 저장
    - SummaryResult 데이터 클래스 동작
    - _estimate_tokens, _format_transcript, _split_utterances 유틸리티
    - _build_fallback_markdown 폴백 생성
    - ModelLoadManager 뮤텍스 연동
    - 한국어 유니코드 NFC 정규화
의존성: pytest, pytest-asyncio
"""

import json
import urllib.error
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.llm_backend import LLMConnectionError, LLMGenerationError
from core.ollama_client import OllamaConnectionError, OllamaTimeoutError, clear_connection_cache
from steps.corrector import CorrectedResult, CorrectedUtterance
from steps.summarizer import (
    EmptySummaryInputError,
    Summarizer,
    SummaryError,
    SummaryResult,
    _build_fallback_markdown,
    _estimate_tokens,
    _format_transcript,
    _split_utterances,
)

pytestmark = pytest.mark.asyncio


# === 헬퍼 함수 ===


def _make_corrected_result(
    utterances: list[tuple[str, str, str, float, float]],
    audio_path: str = "/tmp/test.wav",
) -> CorrectedResult:
    """테스트용 CorrectedResult를 생성한다.

    Args:
        utterances: (text, original_text, speaker, start, end) 튜플 리스트
        audio_path: 오디오 파일 경로

    Returns:
        CorrectedResult 인스턴스
    """
    corrected_utterances = [
        CorrectedUtterance(
            text=text,
            original_text=original_text,
            speaker=speaker,
            start=start,
            end=end,
            was_corrected=(text != original_text),
        )
        for text, original_text, speaker, start, end in utterances
    ]
    unique_speakers = set(u[2] for u in utterances)
    return CorrectedResult(
        utterances=corrected_utterances,
        num_speakers=len(unique_speakers),
        audio_path=audio_path,
    )


def _make_simple_corrected_result() -> CorrectedResult:
    """간단한 테스트용 CorrectedResult를 생성한다."""
    return _make_corrected_result([
        ("안녕하세요, 오늘 회의를 시작하겠습니다.", "안녕하세요, 오늘 회의를 시작하겠습니다.", "SPEAKER_00", 0.0, 3.0),
        ("네, 준비됐습니다.", "네, 준비됐습니다.", "SPEAKER_01", 3.5, 5.0),
        ("첫 번째 안건은 프로젝트 일정입니다.", "첫 번째 안건은 프로젝트 일정입니다.", "SPEAKER_00", 5.5, 8.0),
        ("다음 주 금요일까지 완료해야 합니다.", "다음 주 금요일까지 완료해야 합니다.", "SPEAKER_01", 8.5, 11.0),
        ("알겠습니다. 그러면 그렇게 결정하겠습니다.", "알겠습니다. 그러면 그렇게 결정하겠습니다.", "SPEAKER_00", 11.5, 14.0),
    ])


def _make_ollama_response(content: str) -> bytes:
    """Ollama API 응답 JSON을 생성한다.

    Args:
        content: LLM 응답 텍스트

    Returns:
        JSON 인코딩된 바이트열
    """
    response = {
        "model": "exaone3.5:7.8b-instruct-q4_K_M",
        "message": {
            "role": "assistant",
            "content": content,
        },
        "done": True,
    }
    return json.dumps(response).encode("utf-8")


def _make_mock_urlopen(response_bytes: bytes) -> MagicMock:
    """urllib.request.urlopen의 모킹 응답을 생성한다.

    Args:
        response_bytes: 응답 바이트열

    Returns:
        컨텍스트 매니저 프로토콜을 지원하는 MagicMock
    """
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read.return_value = response_bytes
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


def _create_summarizer() -> Summarizer:
    """테스트용 Summarizer 인스턴스를 생성한다 (__init__ 우회).

    __new__ 패턴으로 config/model_manager 싱글턴 의존성 없이 생성한다.
    """
    instance = Summarizer.__new__(Summarizer)
    instance._config = MagicMock()
    instance._manager = MagicMock()
    instance._max_context = 8192
    instance._max_input_tokens = 8192 - 2000  # 6192
    return instance


_SAMPLE_MARKDOWN = """## 회의 개요
- 참석자: SPEAKER_00, SPEAKER_01
- 프로젝트 일정 논의 회의

## 주요 안건
1. 프로젝트 일정
   - 다음 주 금요일까지 완료 필요

## 결정 사항
- 다음 주 금요일까지 프로젝트 완료

## 액션 아이템
- [ ] SPEAKER_01: 프로젝트 마감 준비

## 기타 논의
- 없음"""


# === _estimate_tokens 테스트 ===


class TestEstimateTokens:
    """토큰 추정 함수 테스트."""

    def test_빈_문자열(self) -> None:
        """빈 문자열은 0 토큰으로 추정한다."""
        assert _estimate_tokens("") == 0

    def test_한국어_텍스트(self) -> None:
        """한국어 텍스트의 토큰 수를 추정한다 (1.5글자/토큰)."""
        # "안녕하세요" = 5글자 → 5/1.5 ≈ 3 토큰
        result = _estimate_tokens("안녕하세요")
        assert result == 3

    def test_긴_텍스트(self) -> None:
        """긴 텍스트의 토큰 수를 추정한다."""
        # 150글자 → 150/1.5 = 100 토큰
        text = "가" * 150
        assert _estimate_tokens(text) == 100

    def test_영어_혼합_텍스트(self) -> None:
        """영어가 포함된 텍스트도 동일 방식으로 추정한다."""
        result = _estimate_tokens("Hello World")
        assert result > 0

    def test_최소_1토큰(self) -> None:
        """1글자도 최소 1 토큰으로 추정한다."""
        assert _estimate_tokens("가") == 1


# === _format_transcript 테스트 ===


class TestFormatTranscript:
    """전사문 포맷팅 함수 테스트."""

    def test_기본_포맷팅(self) -> None:
        """발화 목록을 [화자] 텍스트 형식으로 변환한다."""
        utterances = [
            CorrectedUtterance(
                text="안녕하세요",
                original_text="안녕하세요",
                speaker="SPEAKER_00",
                start=0.0,
                end=2.0,
            ),
            CorrectedUtterance(
                text="반갑습니다",
                original_text="반갑습니다",
                speaker="SPEAKER_01",
                start=2.5,
                end=4.0,
            ),
        ]

        result = _format_transcript(utterances)
        assert "[SPEAKER_00] 안녕하세요" in result
        assert "[SPEAKER_01] 반갑습니다" in result

    def test_빈_목록(self) -> None:
        """빈 발화 목록은 빈 문자열을 반환한다."""
        assert _format_transcript([]) == ""

    def test_줄바꿈_구분(self) -> None:
        """각 발화는 줄바꿈으로 구분된다."""
        utterances = [
            CorrectedUtterance(
                text="첫째",
                original_text="첫째",
                speaker="A",
                start=0.0,
                end=1.0,
            ),
            CorrectedUtterance(
                text="둘째",
                original_text="둘째",
                speaker="B",
                start=1.0,
                end=2.0,
            ),
        ]

        result = _format_transcript(utterances)
        lines = result.split("\n")
        assert len(lines) == 2


# === _build_fallback_markdown 테스트 ===


class TestBuildFallbackMarkdown:
    """폴백 회의록 생성 함수 테스트."""

    def test_기본_폴백(self) -> None:
        """폴백 회의록에 참석자와 원본 전사문이 포함된다."""
        utterances = [
            CorrectedUtterance(
                text="안녕하세요",
                original_text="안녕하세요",
                speaker="SPEAKER_00",
                start=0.0,
                end=2.0,
            ),
        ]
        speakers = ["SPEAKER_00"]

        result = _build_fallback_markdown(utterances, speakers)
        assert "## 회의 개요" in result
        assert "SPEAKER_00" in result
        assert "AI 요약 실패" in result
        assert "안녕하세요" in result

    def test_다수_화자(self) -> None:
        """다수 화자의 이름이 모두 포함된다."""
        utterances = [
            CorrectedUtterance(
                text="첫째", original_text="첫째",
                speaker="A", start=0.0, end=1.0,
            ),
            CorrectedUtterance(
                text="둘째", original_text="둘째",
                speaker="B", start=1.0, end=2.0,
            ),
        ]
        result = _build_fallback_markdown(utterances, ["A", "B"])
        assert "A" in result
        assert "B" in result


# === _split_utterances 테스트 ===


class TestSplitUtterances:
    """발화 분할 함수 테스트."""

    def test_분할_불필요(self) -> None:
        """토큰 제한 내 발화는 분할하지 않는다."""
        utterances = [
            CorrectedUtterance(
                text="짧은 텍스트",
                original_text="짧은 텍스트",
                speaker="A",
                start=0.0,
                end=1.0,
            ),
        ]
        chunks = _split_utterances(utterances, max_tokens=1000)
        assert len(chunks) == 1
        assert len(chunks[0]) == 1

    def test_분할_필요(self) -> None:
        """토큰 제한 초과 시 여러 청크로 분할한다."""
        # 각 발화를 충분히 길게 만들어서 분할 유도
        utterances = [
            CorrectedUtterance(
                text="가" * 100,  # 약 67 토큰
                original_text="가" * 100,
                speaker="A",
                start=float(i),
                end=float(i + 1),
            )
            for i in range(10)
        ]

        # 토큰 제한을 작게 설정하여 분할 유도
        chunks = _split_utterances(utterances, max_tokens=100)
        assert len(chunks) > 1
        # 모든 발화가 보존되는지 확인
        total = sum(len(chunk) for chunk in chunks)
        assert total == 10

    def test_빈_목록(self) -> None:
        """빈 발화 목록은 빈 청크 목록을 반환한다."""
        chunks = _split_utterances([], max_tokens=1000)
        assert len(chunks) == 0

    def test_단일_긴_발화(self) -> None:
        """하나의 발화가 매우 길어도 하나의 청크에 포함된다."""
        utterances = [
            CorrectedUtterance(
                text="가" * 3000,  # 매우 긴 발화
                original_text="가" * 3000,
                speaker="A",
                start=0.0,
                end=1.0,
            ),
        ]
        chunks = _split_utterances(utterances, max_tokens=100)
        assert len(chunks) == 1
        assert len(chunks[0]) == 1

    def test_정확한_경계(self) -> None:
        """토큰 경계에서 올바르게 분할된다."""
        utterances = [
            CorrectedUtterance(
                text=f"발화{i}",
                original_text=f"발화{i}",
                speaker="A",
                start=float(i),
                end=float(i + 1),
            )
            for i in range(5)
        ]
        # PERF: 개선된 토큰 추정(한국어/ASCII 분리)에서 각 발화는 약 2~3토큰
        # max_tokens=5이면 2~3개씩 분할됨
        chunks = _split_utterances(utterances, max_tokens=5)
        assert len(chunks) >= 2


# === SummaryResult 테스트 ===


class TestSummaryResult:
    """SummaryResult 데이터 클래스 테스트."""

    def test_기본_생성(self) -> None:
        """기본 필드로 인스턴스를 생성한다."""
        result = SummaryResult(
            markdown="# 회의록",
            audio_path="/tmp/test.wav",
            num_speakers=2,
            speakers=["A", "B"],
            num_utterances=10,
        )
        assert result.markdown == "# 회의록"
        assert result.num_speakers == 2
        assert result.speakers == ["A", "B"]
        assert result.num_utterances == 10
        assert result.was_chunked is False
        assert result.chunk_count == 1
        assert result.created_at != ""

    def test_created_at_자동_설정(self) -> None:
        """created_at이 자동으로 설정된다."""
        result = SummaryResult(
            markdown="# 회의록",
            audio_path="/tmp/test.wav",
            num_speakers=1,
            speakers=["A"],
            num_utterances=5,
        )
        assert result.created_at != ""

    def test_created_at_유지(self) -> None:
        """명시적으로 지정한 created_at은 유지된다."""
        result = SummaryResult(
            markdown="# 회의록",
            audio_path="/tmp/test.wav",
            num_speakers=1,
            speakers=["A"],
            num_utterances=5,
            created_at="2026-01-01T00:00:00",
        )
        assert result.created_at == "2026-01-01T00:00:00"

    def test_to_dict(self) -> None:
        """딕셔너리 변환이 모든 필드를 포함한다."""
        result = SummaryResult(
            markdown="# 회의록",
            audio_path="/tmp/test.wav",
            num_speakers=2,
            speakers=["A", "B"],
            num_utterances=10,
            was_chunked=True,
            chunk_count=3,
        )
        d = result.to_dict()
        assert d["markdown"] == "# 회의록"
        assert d["num_speakers"] == 2
        assert d["speakers"] == ["A", "B"]
        assert d["was_chunked"] is True
        assert d["chunk_count"] == 3

    def test_분할_요약_필드(self) -> None:
        """분할 요약 정보가 올바르게 기록된다."""
        result = SummaryResult(
            markdown="# 회의록",
            audio_path="/tmp/test.wav",
            num_speakers=3,
            speakers=["A", "B", "C"],
            num_utterances=100,
            was_chunked=True,
            chunk_count=5,
        )
        assert result.was_chunked is True
        assert result.chunk_count == 5

    def test_체크포인트_저장_복원(self, tmp_path: pytest.TempPathFactory) -> None:
        """체크포인트 저장 후 복원하면 동일한 데이터를 얻는다."""
        original = SummaryResult(
            markdown="## 회의 개요\n- 참석자: A, B\n\n## 주요 안건\n1. 테스트",
            audio_path="/tmp/test.wav",
            num_speakers=2,
            speakers=["A", "B"],
            num_utterances=10,
            created_at="2026-03-04T10:00:00",
            was_chunked=True,
            chunk_count=2,
        )

        checkpoint_path = tmp_path / "summary_checkpoint.json"
        original.save_checkpoint(checkpoint_path)

        restored = SummaryResult.from_checkpoint(checkpoint_path)
        assert restored.markdown == original.markdown
        assert restored.audio_path == original.audio_path
        assert restored.num_speakers == original.num_speakers
        assert restored.speakers == original.speakers
        assert restored.num_utterances == original.num_utterances
        assert restored.created_at == original.created_at
        assert restored.was_chunked == original.was_chunked
        assert restored.chunk_count == original.chunk_count

    def test_체크포인트_한국어_보존(self, tmp_path: pytest.TempPathFactory) -> None:
        """체크포인트 저장/복원 시 한국어가 깨지지 않는다."""
        original = SummaryResult(
            markdown="## 회의 개요\n- 한국어 테스트: 가나다라마바사",
            audio_path="/tmp/한국어경로.wav",
            num_speakers=1,
            speakers=["화자_00"],
            num_utterances=1,
        )

        checkpoint_path = tmp_path / "kr_checkpoint.json"
        original.save_checkpoint(checkpoint_path)

        # JSON 파일 내용 확인 (ensure_ascii=False 검증)
        with open(checkpoint_path, encoding="utf-8") as f:
            raw = f.read()
        assert "가나다라마바사" in raw
        assert "화자_00" in raw

        restored = SummaryResult.from_checkpoint(checkpoint_path)
        assert restored.markdown == original.markdown

    def test_마크다운_파일_저장(self, tmp_path: pytest.TempPathFactory) -> None:
        """마크다운 파일을 올바르게 저장한다."""
        result = SummaryResult(
            markdown="## 회의 개요\n- 테스트 회의록",
            audio_path="/tmp/test.wav",
            num_speakers=1,
            speakers=["A"],
            num_utterances=1,
        )

        md_path = tmp_path / "meeting_minutes.md"
        result.save_markdown(md_path)

        with open(md_path, encoding="utf-8") as f:
            content = f.read()
        assert content == "## 회의 개요\n- 테스트 회의록"

    def test_중첩_디렉토리_자동_생성(self, tmp_path: pytest.TempPathFactory) -> None:
        """저장 시 부모 디렉토리가 자동 생성된다."""
        result = SummaryResult(
            markdown="# 테스트",
            audio_path="/tmp/test.wav",
            num_speakers=1,
            speakers=["A"],
            num_utterances=1,
        )

        nested_path = tmp_path / "a" / "b" / "c" / "summary.json"
        result.save_checkpoint(nested_path)
        assert nested_path.exists()


# === SummaryError 계층 테스트 ===


class TestSummaryErrors:
    """에러 계층 테스트."""

    def test_SummaryError_기본(self) -> None:
        """SummaryError가 Exception의 하위 클래스이다."""
        assert issubclass(SummaryError, Exception)

    def test_EmptySummaryInputError_상속(self) -> None:
        """EmptySummaryInputError가 SummaryError의 하위 클래스이다."""
        assert issubclass(EmptySummaryInputError, SummaryError)

    def test_에러_메시지(self) -> None:
        """에러에 메시지가 올바르게 전달된다."""
        error = SummaryError("테스트 에러")
        assert str(error) == "테스트 에러"


# === Summarizer 클래스 테스트 ===


class TestSummarizer:
    """Summarizer 클래스 테스트."""

    def setup_method(self) -> None:
        """각 테스트 전 Ollama 연결 캐시를 초기화한다."""
        clear_connection_cache()

    def test_초기화(self) -> None:
        """__new__ 패턴으로 초기화된 인스턴스의 속성을 확인한다."""
        summarizer = _create_summarizer()
        assert summarizer._max_context == 8192
        assert summarizer._max_input_tokens == 6192

    def test_create_backend_성공(self) -> None:
        """LLM 백엔드 생성 성공 시 OllamaBackend 인스턴스를 반환한다."""
        summarizer = _create_summarizer()

        # config.llm 설정
        summarizer._config.llm.backend = "ollama"
        summarizer._config.llm.host = "http://127.0.0.1:11434"
        summarizer._config.llm.model_name = "exaone3.5:7.8b-instruct-q4_K_M"
        summarizer._config.llm.temperature = 0.3
        summarizer._config.llm.max_context_tokens = 8192
        summarizer._config.llm.request_timeout_seconds = 120

        mock_resp = _make_mock_urlopen(b'{"models": []}')
        with patch("core.ollama_client.urllib.request.urlopen", return_value=mock_resp):
            backend = summarizer._create_backend()

        from core.llm_backend import OllamaBackend
        assert isinstance(backend, OllamaBackend)

    def test_create_backend_연결_실패(self) -> None:
        """LLM 백엔드 생성 시 연결 실패하면 LLMConnectionError를 발생한다."""
        summarizer = _create_summarizer()

        # config.llm 설정
        summarizer._config.llm.backend = "ollama"
        summarizer._config.llm.host = "http://127.0.0.1:11434"
        summarizer._config.llm.model_name = "exaone3.5:7.8b-instruct-q4_K_M"
        summarizer._config.llm.temperature = 0.3
        summarizer._config.llm.max_context_tokens = 8192
        summarizer._config.llm.request_timeout_seconds = 120

        with patch(
            "core.ollama_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            with pytest.raises(LLMConnectionError):
                summarizer._create_backend()

    def test_call_llm_성공(self) -> None:
        """LLM 백엔드 호출이 성공하면 응답 텍스트를 반환한다."""
        summarizer = _create_summarizer()
        backend = MagicMock()
        backend.chat.return_value = "## 회의 개요\n- 테스트"

        result = summarizer._call_llm(
            backend, "시스템 프롬프트", "사용자 프롬프트"
        )

        assert "회의 개요" in result
        backend.chat.assert_called_once()

    def test_call_llm_타임아웃(self) -> None:
        """LLM 백엔드 타임아웃 시 SummaryError를 발생한다."""
        summarizer = _create_summarizer()
        backend = MagicMock()
        backend.chat.side_effect = LLMGenerationError("timed out")

        with pytest.raises(SummaryError):
            summarizer._call_llm(
                backend, "시스템", "사용자"
            )

    def test_call_llm_연결_실패(self) -> None:
        """LLM 백엔드 연결 실패 시 LLMConnectionError를 전파한다."""
        summarizer = _create_summarizer()
        backend = MagicMock()
        backend.chat.side_effect = LLMConnectionError("Connection refused")

        with pytest.raises(LLMConnectionError):
            summarizer._call_llm(
                backend, "시스템", "사용자"
            )

    def test_call_llm_응답_파싱_실패(self) -> None:
        """LLM 백엔드 응답 파싱 실패 시 SummaryError를 발생한다."""
        summarizer = _create_summarizer()
        backend = MagicMock()
        # OllamaResponseError는 LLMGenerationError의 하위 클래스
        from core.ollama_client import OllamaResponseError
        backend.chat.side_effect = OllamaResponseError("JSON 파싱 실패")

        with pytest.raises(SummaryError, match="JSON 파싱"):
            summarizer._call_llm(
                backend, "시스템", "사용자"
            )

    def test_call_llm_빈_content(self) -> None:
        """LLM 백엔드가 빈 응답을 반환하면 빈 문자열을 반환한다."""
        summarizer = _create_summarizer()
        backend = MagicMock()
        backend.chat.return_value = ""

        result = summarizer._call_llm(
            backend, "시스템", "사용자"
        )
        assert result == ""

    def test_call_llm_NFC_정규화(self) -> None:
        """LLM 응답에 NFC 정규화가 적용된다."""
        summarizer = _create_summarizer()
        backend = MagicMock()

        # NFD 형태의 한국어 (ㅎㅏㄴ ㄱㅜㄱ)
        import unicodedata
        nfd_text = unicodedata.normalize("NFD", "한국어 회의록")
        backend.chat.return_value = nfd_text

        result = summarizer._call_llm(
            backend, "시스템", "사용자"
        )

        # 결과가 NFC로 정규화되어야 함
        assert result == unicodedata.normalize("NFC", nfd_text)

    def test_call_llm_LLMGenerationError(self) -> None:
        """LLMGenerationError 발생 시 SummaryError를 발생한다."""
        summarizer = _create_summarizer()
        backend = MagicMock()
        backend.chat.side_effect = LLMGenerationError("timed out")

        with pytest.raises(SummaryError):
            summarizer._call_llm(
                backend, "시스템", "사용자"
            )


# === Summarizer.summarize() 비동기 테스트 ===


class TestSummarizeSingle:
    """단일 요약 (전체 전사문이 컨텍스트 내) 테스트."""

    async def test_단일_요약_성공(self) -> None:
        """짧은 전사문은 단일 호출로 요약된다."""
        summarizer = _create_summarizer()
        corrected = _make_simple_corrected_result()

        # ModelLoadManager 모킹 — acquire 컨텍스트에서 backend Mock 반환
        ctx = MagicMock()
        backend = MagicMock()
        backend.chat.return_value = _SAMPLE_MARKDOWN
        ctx.__aenter__ = AsyncMock(return_value=backend)
        ctx.__aexit__ = AsyncMock(return_value=False)
        summarizer._manager.acquire = MagicMock(return_value=ctx)

        result = await summarizer.summarize(corrected)

        assert isinstance(result, SummaryResult)
        assert "회의 개요" in result.markdown
        assert result.was_chunked is False
        assert result.chunk_count == 1
        assert result.num_speakers == 2
        assert result.num_utterances == 5
        assert "SPEAKER_00" in result.speakers
        assert "SPEAKER_01" in result.speakers

    async def test_빈_발화_에러(self) -> None:
        """빈 발화 목록은 EmptySummaryInputError를 발생한다."""
        summarizer = _create_summarizer()
        corrected = CorrectedResult(
            utterances=[],
            num_speakers=0,
            audio_path="/tmp/empty.wav",
        )

        with pytest.raises(EmptySummaryInputError):
            await summarizer.summarize(corrected)

    async def test_요약_실패_폴백(self) -> None:
        """요약 실패 시 폴백 회의록이 생성된다."""
        summarizer = _create_summarizer()
        corrected = _make_simple_corrected_result()

        # ModelLoadManager 모킹
        ctx = MagicMock()
        backend = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=backend)
        ctx.__aexit__ = AsyncMock(return_value=False)
        summarizer._manager.acquire = MagicMock(return_value=ctx)

        # SummaryError 발생 (_call_llm 메서드를 직접 모킹)
        with patch.object(
            summarizer, "_call_llm",
            side_effect=SummaryError("테스트 실패"),
        ):
            result = await summarizer.summarize(corrected)

        assert "AI 요약 실패" in result.markdown
        assert "안녕하세요" in result.markdown

    async def test_LLM_연결_실패_전파(self) -> None:
        """LLMConnectionError는 폴백 없이 전파된다."""
        summarizer = _create_summarizer()
        corrected = _make_simple_corrected_result()

        # ModelLoadManager가 LLMConnectionError를 발생
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(
            side_effect=LLMConnectionError("연결 실패")
        )
        ctx.__aexit__ = AsyncMock(return_value=False)
        summarizer._manager.acquire = MagicMock(return_value=ctx)

        with pytest.raises(LLMConnectionError):
            await summarizer.summarize(corrected)

    async def test_LLM_타임아웃_전파(self) -> None:
        """LLMGenerationError는 폴백 없이 전파된다."""
        summarizer = _create_summarizer()
        corrected = _make_simple_corrected_result()

        # ModelLoadManager가 LLMGenerationError를 발생
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(
            side_effect=LLMGenerationError("타임아웃")
        )
        ctx.__aexit__ = AsyncMock(return_value=False)
        summarizer._manager.acquire = MagicMock(return_value=ctx)

        with pytest.raises(LLMGenerationError):
            await summarizer.summarize(corrected)

    async def test_예상치_못한_오류_폴백(self) -> None:
        """예상치 못한 오류 발생 시 폴백 회의록을 생성한다."""
        summarizer = _create_summarizer()
        corrected = _make_simple_corrected_result()

        # ModelLoadManager 모킹
        ctx = MagicMock()
        backend = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=backend)
        ctx.__aexit__ = AsyncMock(return_value=False)
        summarizer._manager.acquire = MagicMock(return_value=ctx)

        # RuntimeError 발생 (예상치 못한 오류 — _call_llm 메서드를 직접 모킹)
        with patch.object(
            summarizer, "_call_llm",
            side_effect=RuntimeError("unexpected"),
        ):
            result = await summarizer.summarize(corrected)

        assert "AI 요약 실패" in result.markdown

    async def test_ModelLoadManager_acquire_호출(self) -> None:
        """ModelLoadManager.acquire가 'exaone'으로 호출된다."""
        summarizer = _create_summarizer()
        corrected = _make_simple_corrected_result()

        ctx = MagicMock()
        backend = MagicMock()
        backend.chat.return_value = _SAMPLE_MARKDOWN
        ctx.__aenter__ = AsyncMock(return_value=backend)
        ctx.__aexit__ = AsyncMock(return_value=False)
        summarizer._manager.acquire = MagicMock(return_value=ctx)

        await summarizer.summarize(corrected)

        # acquire가 "exaone"으로 호출되었는지 확인
        summarizer._manager.acquire.assert_called_once()
        call_args = summarizer._manager.acquire.call_args
        assert call_args[0][0] == "exaone"


class TestSummarizeChunked:
    """분할 요약 (긴 전사문) 테스트."""

    async def test_분할_요약_자동_감지(self) -> None:
        """컨텍스트 초과 전사문은 자동으로 분할 요약된다."""
        summarizer = _create_summarizer()
        # max_input_tokens를 매우 작게 설정하여 분할 유도
        summarizer._max_input_tokens = 50

        corrected = _make_simple_corrected_result()

        # ModelLoadManager 모킹 — acquire 컨텍스트에서 backend Mock 반환
        ctx = MagicMock()
        backend = MagicMock()
        # 부분 요약 + 통합 요약 응답
        call_count = 0

        def mock_chat_side_effect(*, messages, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 5:
                return f"파트 {call_count} 요약 내용"
            else:
                return _SAMPLE_MARKDOWN

        backend.chat.side_effect = mock_chat_side_effect
        ctx.__aenter__ = AsyncMock(return_value=backend)
        ctx.__aexit__ = AsyncMock(return_value=False)
        summarizer._manager.acquire = MagicMock(return_value=ctx)

        result = await summarizer.summarize(corrected)

        assert result.was_chunked is True
        assert result.chunk_count > 1

    async def test_분할_요약_부분_실패_계속(self) -> None:
        """일부 청크 요약이 실패해도 나머지 청크로 통합 요약한다."""
        summarizer = _create_summarizer()
        summarizer._max_input_tokens = 50

        corrected = _make_simple_corrected_result()

        ctx = MagicMock()
        backend = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=backend)
        ctx.__aexit__ = AsyncMock(return_value=False)
        summarizer._manager.acquire = MagicMock(return_value=ctx)

        # _call_llm를 직접 모킹하여 첫 호출만 실패하도록 설정
        call_count = 0

        def mock_call_llm(backend, system_prompt, user_prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise SummaryError("첫 청크 실패")
            return "요약 내용"

        with patch.object(summarizer, "_call_llm", side_effect=mock_call_llm):
            result = await summarizer.summarize(corrected)

        # 폴백 없이 결과가 생성되어야 함 (부분 실패는 원본으로 대체)
        assert result.was_chunked is True

    async def test_분할_요약_연결_실패_전파(self) -> None:
        """분할 요약 중 연결 실패는 전파된다."""
        summarizer = _create_summarizer()
        summarizer._max_input_tokens = 50

        corrected = _make_simple_corrected_result()

        ctx = MagicMock()
        backend = MagicMock()
        backend.chat.side_effect = LLMConnectionError("Connection refused")
        ctx.__aenter__ = AsyncMock(return_value=backend)
        ctx.__aexit__ = AsyncMock(return_value=False)
        summarizer._manager.acquire = MagicMock(return_value=ctx)

        with pytest.raises(LLMConnectionError):
            await summarizer.summarize(corrected)


class TestSummarizerSingleMethod:
    """Summarizer._summarize_single 메서드 테스트."""

    def test_단일_요약_프롬프트_구성(self) -> None:
        """단일 요약 시 참석자와 전사문이 프롬프트에 포함된다."""
        summarizer = _create_summarizer()
        backend = MagicMock()

        # backend.chat 호출 시 messages 인자를 캡처
        captured_messages = {}

        def capture_chat(*, messages, **kwargs):
            captured_messages["messages"] = messages
            return "## 회의 개요"

        backend.chat.side_effect = capture_chat

        summarizer._summarize_single(
            backend,
            "[SPEAKER_00] 안녕하세요",
            ["SPEAKER_00", "SPEAKER_01"],
        )

        # 프롬프트에 참석자 정보가 포함되어야 함
        user_content = captured_messages["messages"][1]["content"]
        assert "SPEAKER_00" in user_content
        assert "SPEAKER_01" in user_content
        assert "안녕하세요" in user_content


class TestSummarizerChunkedMethod:
    """Summarizer._summarize_chunked 메서드 테스트."""

    def test_분할_요약_2단계_호출(self) -> None:
        """분할 요약은 부분 요약 + 통합 요약 2단계로 호출된다."""
        summarizer = _create_summarizer()
        backend = MagicMock()

        chunks = [
            [CorrectedUtterance(
                text="첫 번째", original_text="첫 번째",
                speaker="A", start=0.0, end=1.0,
            )],
            [CorrectedUtterance(
                text="두 번째", original_text="두 번째",
                speaker="B", start=1.0, end=2.0,
            )],
        ]

        call_count = 0

        def mock_chat_side_effect(*, messages, **kwargs):
            nonlocal call_count
            call_count += 1
            return f"요약 {call_count}"

        backend.chat.side_effect = mock_chat_side_effect

        result = summarizer._summarize_chunked(
            backend, chunks, ["A", "B"]
        )

        # 2개 청크 부분 요약 + 1개 통합 = 3회 호출
        assert call_count == 3
        assert result  # 결과가 비어있지 않아야 함
