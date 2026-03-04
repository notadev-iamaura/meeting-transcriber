"""
EXAONE 전사문 보정기 테스트 모듈 (Corrector Test Module)

목적: steps/corrector.py의 Corrector 클래스와 관련 유틸리티 함수를 검증한다.
주요 테스트:
    - 정상 보정 (오타 → 보정된 텍스트)
    - 배치 처리 (10개 미만, 정확히 10개, 10개 초과)
    - 보정 불필요 시 원본 유지
    - Ollama 연결 실패 / 타임아웃 / 파싱 오류 처리
    - 배치 실패 시 원본 텍스트 보존 (graceful degradation)
    - 체크포인트 저장/복원 라운드트립
    - 한국어 유니코드 NFC 정규화
    - ModelLoadManager 뮤텍스 연동
    - 빈 입력 처리
의존성: pytest, pytest-asyncio
"""

import json
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from steps.corrector import (
    CorrectedResult,
    CorrectedUtterance,
    CorrectionError,
    Corrector,
    EmptyInputError,
    OllamaConnectionError,
    OllamaTimeoutError,
    _build_correction_prompt,
    _parse_correction_response,
)
from steps.merger import MergedResult, MergedUtterance

pytestmark = pytest.mark.asyncio


# === 헬퍼 함수 ===


def _make_merged_result(
    utterances: list[tuple[str, str, float, float]],
    audio_path: str = "/tmp/test.wav",
) -> MergedResult:
    """테스트용 MergedResult를 생성한다.

    Args:
        utterances: (text, speaker, start, end) 튜플 리스트
        audio_path: 오디오 파일 경로

    Returns:
        MergedResult 인스턴스
    """
    merged_utterances = [
        MergedUtterance(text=text, speaker=speaker, start=start, end=end)
        for text, speaker, start, end in utterances
    ]
    unique_speakers = set(u[1] for u in utterances)
    return MergedResult(
        utterances=merged_utterances,
        num_speakers=len(unique_speakers),
        audio_path=audio_path,
    )


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


def _make_mock_manager() -> MagicMock:
    """ModelLoadManager의 acquire() 모킹을 생성한다.

    acquire()가 Ollama 클라이언트 설정 딕셔너리를 반환하도록 한다.

    Returns:
        모킹된 ModelLoadManager
    """
    manager = MagicMock()
    client_config = {
        "host": "http://127.0.0.1:11434",
        "model": "exaone3.5:7.8b-instruct-q4_K_M",
        "temperature": 0.3,
        "num_ctx": 8192,
        "timeout": 120,
    }

    # acquire()가 async context manager를 반환하도록 설정
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client_config)
    ctx.__aexit__ = AsyncMock(return_value=False)
    manager.acquire.return_value = ctx

    return manager


# === _build_correction_prompt 테스트 ===


class Test프롬프트생성:
    """_build_correction_prompt 함수의 단위 테스트."""

    def test_단일_발화(self) -> None:
        """발화 1개로 프롬프트를 생성한다."""
        utterances = [
            MergedUtterance(
                text="안녕하세요", speaker="S0", start=0.0, end=5.0
            ),
        ]
        prompt = _build_correction_prompt(utterances)
        assert prompt == "[1] 안녕하세요"

    def test_다중_발화(self) -> None:
        """발화 여러 개로 프롬프트를 생성한다."""
        utterances = [
            MergedUtterance(text="첫 번째", speaker="S0", start=0.0, end=5.0),
            MergedUtterance(text="두 번째", speaker="S1", start=5.0, end=10.0),
            MergedUtterance(text="세 번째", speaker="S0", start=10.0, end=15.0),
        ]
        prompt = _build_correction_prompt(utterances)
        lines = prompt.split("\n")
        assert len(lines) == 3
        assert lines[0] == "[1] 첫 번째"
        assert lines[1] == "[2] 두 번째"
        assert lines[2] == "[3] 세 번째"


# === _parse_correction_response 테스트 ===


class Test응답파싱:
    """_parse_correction_response 함수의 단위 테스트."""

    def test_정상_파싱(self) -> None:
        """정상 응답을 파싱한다."""
        response = "[1] 보정된 텍스트\n[2] 두 번째 보정"
        result = _parse_correction_response(response, 2)
        assert result == {1: "보정된 텍스트", 2: "두 번째 보정"}

    def test_빈_응답(self) -> None:
        """빈 응답은 빈 딕셔너리를 반환한다."""
        result = _parse_correction_response("", 3)
        assert result == {}

    def test_범위_초과_번호_무시(self) -> None:
        """배치 크기를 초과하는 번호는 무시한다."""
        response = "[1] 텍스트1\n[2] 텍스트2\n[99] 잘못된번호"
        result = _parse_correction_response(response, 2)
        assert result == {1: "텍스트1", 2: "텍스트2"}

    def test_0번_무시(self) -> None:
        """0번 번호는 무시한다 (1부터 시작)."""
        response = "[0] 잘못된\n[1] 정상"
        result = _parse_correction_response(response, 1)
        assert result == {1: "정상"}

    def test_포맷_불일치_줄_무시(self) -> None:
        """[번호] 형식이 아닌 줄은 무시한다."""
        response = "잘못된 형식\n[1] 정상 텍스트\n또 잘못된 줄"
        result = _parse_correction_response(response, 1)
        assert result == {1: "정상 텍스트"}

    def test_빈_텍스트_무시(self) -> None:
        """텍스트가 비어있는 줄은 무시한다."""
        response = "[1] \n[2] 정상"
        result = _parse_correction_response(response, 2)
        assert result == {2: "정상"}


# === CorrectedUtterance 테스트 ===


class TestCorrectedUtterance:
    """CorrectedUtterance 데이터 클래스의 단위 테스트."""

    def test_생성(self) -> None:
        """기본 생성 및 필드 접근."""
        u = CorrectedUtterance(
            text="보정됨",
            original_text="보정안됨",
            speaker="SPEAKER_00",
            start=0.0,
            end=5.0,
            was_corrected=True,
        )
        assert u.text == "보정됨"
        assert u.original_text == "보정안됨"
        assert u.speaker == "SPEAKER_00"
        assert u.was_corrected is True

    def test_duration(self) -> None:
        """duration 프로퍼티 계산."""
        u = CorrectedUtterance(
            text="t", original_text="t",
            speaker="S0", start=3.0, end=8.0,
        )
        assert u.duration == pytest.approx(5.0)

    def test_to_dict(self) -> None:
        """딕셔너리 변환."""
        u = CorrectedUtterance(
            text="보정", original_text="원본",
            speaker="S0", start=0.0, end=3.0,
            was_corrected=True,
        )
        d = u.to_dict()
        assert d["text"] == "보정"
        assert d["original_text"] == "원본"
        assert d["was_corrected"] is True


# === CorrectedResult 테스트 ===


class TestCorrectedResult:
    """CorrectedResult 데이터 클래스의 단위 테스트."""

    def test_total_duration(self) -> None:
        """total_duration 프로퍼티 계산."""
        result = CorrectedResult(
            utterances=[
                CorrectedUtterance(
                    text="a", original_text="a",
                    speaker="S0", start=0.0, end=5.0,
                ),
                CorrectedUtterance(
                    text="b", original_text="b",
                    speaker="S1", start=5.0, end=12.0,
                ),
            ],
            num_speakers=2,
            audio_path="/tmp/test.wav",
        )
        assert result.total_duration == pytest.approx(12.0)

    def test_total_duration_빈결과(self) -> None:
        """빈 utterance 리스트에서 total_duration은 0."""
        result = CorrectedResult(
            utterances=[], num_speakers=0, audio_path="/tmp/t.wav",
        )
        assert result.total_duration == pytest.approx(0.0)

    def test_speakers(self) -> None:
        """speakers 프로퍼티 (중복 제거, 정렬)."""
        result = CorrectedResult(
            utterances=[
                CorrectedUtterance(
                    text="a", original_text="a",
                    speaker="S1", start=0, end=5,
                ),
                CorrectedUtterance(
                    text="b", original_text="b",
                    speaker="S0", start=5, end=10,
                ),
                CorrectedUtterance(
                    text="c", original_text="c",
                    speaker="S1", start=10, end=15,
                ),
            ],
            num_speakers=2,
            audio_path="/tmp/t.wav",
        )
        assert result.speakers == ["S0", "S1"]

    def test_correction_rate(self) -> None:
        """correction_rate 계산."""
        result = CorrectedResult(
            utterances=[
                CorrectedUtterance(
                    text="a", original_text="a",
                    speaker="S0", start=0, end=5,
                ),
            ] * 4,
            num_speakers=1,
            audio_path="/tmp/t.wav",
            total_corrected=2,
        )
        assert result.correction_rate == pytest.approx(0.5)

    def test_correction_rate_빈결과(self) -> None:
        """빈 결과에서 correction_rate는 0."""
        result = CorrectedResult(
            utterances=[], num_speakers=0, audio_path="/tmp/t.wav",
        )
        assert result.correction_rate == pytest.approx(0.0)

    def test_체크포인트_라운드트립(self, tmp_path) -> None:
        """체크포인트 저장/복원 라운드트립."""
        original = CorrectedResult(
            utterances=[
                CorrectedUtterance(
                    text="보정된 텍스트", original_text="보정안된 텍스트",
                    speaker="SPEAKER_00", start=0.0, end=5.0,
                    was_corrected=True,
                ),
                CorrectedUtterance(
                    text="원본 유지", original_text="원본 유지",
                    speaker="SPEAKER_01", start=5.0, end=10.0,
                    was_corrected=False,
                ),
            ],
            num_speakers=2,
            audio_path="/tmp/test.wav",
            total_corrected=1,
            total_failed=0,
        )

        checkpoint_path = tmp_path / "sub" / "corrected.json"
        original.save_checkpoint(checkpoint_path)

        assert checkpoint_path.exists()

        restored = CorrectedResult.from_checkpoint(checkpoint_path)

        assert len(restored.utterances) == 2
        assert restored.num_speakers == 2
        assert restored.audio_path == "/tmp/test.wav"
        assert restored.total_corrected == 1
        assert restored.total_failed == 0
        assert restored.utterances[0].text == "보정된 텍스트"
        assert restored.utterances[0].original_text == "보정안된 텍스트"
        assert restored.utterances[0].was_corrected is True
        assert restored.utterances[1].was_corrected is False

    def test_체크포인트_한국어_보존(self, tmp_path) -> None:
        """체크포인트 JSON에서 한국어가 이스케이프 없이 보존되는지 확인."""
        original = CorrectedResult(
            utterances=[
                CorrectedUtterance(
                    text="한국어 보정 테스트", original_text="한국어 보정 테스트",
                    speaker="S0", start=0.0, end=5.0,
                ),
            ],
            num_speakers=1,
            audio_path="/tmp/test.wav",
        )

        checkpoint_path = tmp_path / "korean.json"
        original.save_checkpoint(checkpoint_path)

        raw_content = checkpoint_path.read_text(encoding="utf-8")
        assert "한국어 보정 테스트" in raw_content
        assert "\\u" not in raw_content

    def test_체크포인트_파일_없음(self, tmp_path) -> None:
        """존재하지 않는 체크포인트 파일에서 복원 시 에러."""
        with pytest.raises(FileNotFoundError):
            CorrectedResult.from_checkpoint(tmp_path / "not_exist.json")


# === Corrector 정상 보정 테스트 ===


class TestCorrector정상보정:
    """Corrector 클래스의 정상 보정 테스트."""

    async def test_단일_발화_보정(self) -> None:
        """발화 1개를 보정한다."""
        merged = _make_merged_result([
            ("오늘 화의를 시작하겠습니다", "SPEAKER_00", 0.0, 5.0),
        ])

        corrected_response = _make_ollama_response(
            "[1] 오늘 회의를 시작하겠습니다"
        )
        tags_response = json.dumps({"models": []}).encode("utf-8")

        manager = _make_mock_manager()

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_mock_urlopen(corrected_response)

            corrector = Corrector.__new__(Corrector)
            corrector._config = MagicMock()
            corrector._config.llm.model_name = "exaone3.5:7.8b-instruct-q4_K_M"
            corrector._config.llm.host = "http://127.0.0.1:11434"
            corrector._config.llm.temperature = 0.3
            corrector._config.llm.max_context_tokens = 8192
            corrector._config.llm.correction_batch_size = 10
            corrector._config.llm.request_timeout_seconds = 120
            corrector._manager = manager
            corrector._model_name = "exaone3.5:7.8b-instruct-q4_K_M"
            corrector._host = "http://127.0.0.1:11434"
            corrector._temperature = 0.3
            corrector._max_context = 8192
            corrector._batch_size = 10
            corrector._timeout = 120

            result = await corrector.correct(merged)

        assert len(result.utterances) == 1
        assert result.utterances[0].text == "오늘 회의를 시작하겠습니다"
        assert result.utterances[0].original_text == "오늘 화의를 시작하겠습니다"
        assert result.utterances[0].was_corrected is True
        assert result.total_corrected == 1

    async def test_다중_발화_보정(self) -> None:
        """여러 발화를 보정한다."""
        merged = _make_merged_result([
            ("화의 안건을 말슴드리겠습니다", "SPEAKER_00", 0.0, 5.0),
            ("네 알겠슴니다", "SPEAKER_01", 5.0, 10.0),
            ("다음 안건으로 넘어가죠", "SPEAKER_00", 10.0, 15.0),
        ])

        corrected_response = _make_ollama_response(
            "[1] 회의 안건을 말씀드리겠습니다\n"
            "[2] 네 알겠습니다\n"
            "[3] 다음 안건으로 넘어가죠"
        )

        manager = _make_mock_manager()

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_mock_urlopen(corrected_response)

            corrector = Corrector.__new__(Corrector)
            corrector._manager = manager
            corrector._batch_size = 10
            corrector._timeout = 120

            result = await corrector.correct(merged)

        assert len(result.utterances) == 3
        assert result.utterances[0].text == "회의 안건을 말씀드리겠습니다"
        assert result.utterances[0].was_corrected is True
        assert result.utterances[1].text == "네 알겠습니다"
        assert result.utterances[1].was_corrected is True
        # 세 번째는 원본과 동일 → was_corrected=False
        assert result.utterances[2].text == "다음 안건으로 넘어가죠"
        assert result.utterances[2].was_corrected is False

    async def test_보정_불필요_시_원본_유지(self) -> None:
        """보정이 필요 없는 텍스트는 원본 그대로 유지한다."""
        merged = _make_merged_result([
            ("정확한 문장입니다", "SPEAKER_00", 0.0, 5.0),
        ])

        corrected_response = _make_ollama_response(
            "[1] 정확한 문장입니다"
        )

        manager = _make_mock_manager()

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_mock_urlopen(corrected_response)

            corrector = Corrector.__new__(Corrector)
            corrector._manager = manager
            corrector._batch_size = 10
            corrector._timeout = 120

            result = await corrector.correct(merged)

        assert result.utterances[0].was_corrected is False
        assert result.total_corrected == 0


# === 배치 처리 테스트 ===


class TestCorrector배치처리:
    """배치 분할 및 처리 테스트."""

    async def test_배치_크기_미만(self) -> None:
        """발화가 배치 크기(10)보다 적을 때 단일 배치로 처리."""
        utterances = [
            (f"발화 {i}", "S0", i * 5.0, (i + 1) * 5.0)
            for i in range(3)
        ]
        merged = _make_merged_result(utterances)

        response = "[1] 발화 0\n[2] 발화 1\n[3] 발화 2"
        corrected_response = _make_ollama_response(response)

        manager = _make_mock_manager()

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_mock_urlopen(corrected_response)

            corrector = Corrector.__new__(Corrector)
            corrector._manager = manager
            corrector._batch_size = 10
            corrector._timeout = 120

            result = await corrector.correct(merged)

        assert len(result.utterances) == 3

    async def test_배치_크기_초과_분할(self) -> None:
        """발화가 배치 크기를 초과하면 여러 배치로 분할한다."""
        utterances = [
            (f"발화 {i}", "S0", i * 5.0, (i + 1) * 5.0)
            for i in range(15)
        ]
        merged = _make_merged_result(utterances)

        # 배치1 응답 (10개)
        batch1_lines = "\n".join(f"[{i+1}] 발화 {i}" for i in range(10))
        batch1_response = _make_ollama_response(batch1_lines)

        # 배치2 응답 (5개)
        batch2_lines = "\n".join(f"[{i+1}] 발화 {i+10}" for i in range(5))
        batch2_response = _make_ollama_response(batch2_lines)

        manager = _make_mock_manager()

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [
                _make_mock_urlopen(batch1_response),
                _make_mock_urlopen(batch2_response),
            ]

            corrector = Corrector.__new__(Corrector)
            corrector._manager = manager
            corrector._batch_size = 10
            corrector._timeout = 120

            result = await corrector.correct(merged)

        assert len(result.utterances) == 15

    async def test_정확히_배치_크기(self) -> None:
        """발화 수가 정확히 배치 크기와 같을 때."""
        utterances = [
            (f"발화 {i}", "S0", i * 5.0, (i + 1) * 5.0)
            for i in range(10)
        ]
        merged = _make_merged_result(utterances)

        lines = "\n".join(f"[{i+1}] 발화 {i}" for i in range(10))
        corrected_response = _make_ollama_response(lines)

        manager = _make_mock_manager()

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_mock_urlopen(corrected_response)

            corrector = Corrector.__new__(Corrector)
            corrector._manager = manager
            corrector._batch_size = 10
            corrector._timeout = 120

            result = await corrector.correct(merged)

        assert len(result.utterances) == 10


# === 에러 처리 테스트 ===


class TestCorrector에러처리:
    """에러 발생 시 처리 테스트."""

    async def test_빈_입력(self) -> None:
        """빈 MergedResult로 보정 시 EmptyInputError 발생."""
        merged = MergedResult(
            utterances=[], num_speakers=0, audio_path="/tmp/t.wav",
        )

        manager = _make_mock_manager()

        corrector = Corrector.__new__(Corrector)
        corrector._manager = manager
        corrector._batch_size = 10
        corrector._timeout = 120

        with pytest.raises(EmptyInputError):
            await corrector.correct(merged)

    async def test_Ollama_연결_실패(self) -> None:
        """Ollama 서버 연결 실패 시 OllamaConnectionError 발생."""
        merged = _make_merged_result([
            ("테스트", "S0", 0.0, 5.0),
        ])

        # acquire에서 OllamaConnectionError 발생하도록 설정
        manager = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(
            side_effect=OllamaConnectionError("연결 실패")
        )
        ctx.__aexit__ = AsyncMock(return_value=False)
        manager.acquire.return_value = ctx

        corrector = Corrector.__new__(Corrector)
        corrector._manager = manager
        corrector._batch_size = 10
        corrector._timeout = 120

        with pytest.raises(OllamaConnectionError):
            await corrector.correct(merged)

    async def test_배치_실패_시_원본_유지(self) -> None:
        """LLM 호출 실패 시 원본 텍스트를 유지한다."""
        merged = _make_merged_result([
            ("오타있는 텍스트", "S0", 0.0, 5.0),
            ("또다른 텍스트", "S1", 5.0, 10.0),
        ])

        manager = _make_mock_manager()

        import urllib.error
        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("connection refused")

            corrector = Corrector.__new__(Corrector)
            corrector._manager = manager
            corrector._batch_size = 10
            corrector._timeout = 120

            result = await corrector.correct(merged)

        # 모든 발화가 원본 텍스트로 유지됨
        assert result.utterances[0].text == "오타있는 텍스트"
        assert result.utterances[0].was_corrected is False
        assert result.utterances[1].text == "또다른 텍스트"
        assert result.utterances[1].was_corrected is False
        assert result.total_corrected == 0
        assert result.total_failed == 2

    async def test_Ollama_타임아웃(self) -> None:
        """Ollama 타임아웃 발생 시 원본 유지 (배치 단위 graceful degradation)."""
        merged = _make_merged_result([
            ("텍스트", "S0", 0.0, 5.0),
        ])

        manager = _make_mock_manager()

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = TimeoutError("timed out")

            corrector = Corrector.__new__(Corrector)
            corrector._manager = manager
            corrector._batch_size = 10
            corrector._timeout = 120

            result = await corrector.correct(merged)

        # 타임아웃 시 원본 유지
        assert result.utterances[0].text == "텍스트"
        assert result.utterances[0].was_corrected is False

    async def test_잘못된_응답_포맷(self) -> None:
        """LLM이 잘못된 포맷으로 응답할 때 원본 유지."""
        merged = _make_merged_result([
            ("테스트", "S0", 0.0, 5.0),
        ])

        # 포맷이 맞지 않는 응답
        bad_response = _make_ollama_response(
            "보정된 텍스트입니다 (번호 없음)"
        )

        manager = _make_mock_manager()

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_mock_urlopen(bad_response)

            corrector = Corrector.__new__(Corrector)
            corrector._manager = manager
            corrector._batch_size = 10
            corrector._timeout = 120

            result = await corrector.correct(merged)

        # 파싱 실패 → 원본 유지
        assert result.utterances[0].text == "테스트"
        assert result.utterances[0].was_corrected is False

    async def test_빈_content_응답(self) -> None:
        """Ollama 응답에 content가 비어있을 때 원본 유지."""
        merged = _make_merged_result([
            ("테스트", "S0", 0.0, 5.0),
        ])

        # content가 빈 응답
        empty_response = json.dumps({
            "model": "test",
            "message": {"role": "assistant", "content": ""},
            "done": True,
        }).encode("utf-8")

        manager = _make_mock_manager()

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_mock_urlopen(empty_response)

            corrector = Corrector.__new__(Corrector)
            corrector._manager = manager
            corrector._batch_size = 10
            corrector._timeout = 120

            result = await corrector.correct(merged)

        # 빈 응답 → 원본 유지
        assert result.utterances[0].text == "테스트"
        assert result.utterances[0].was_corrected is False


# === 한국어 처리 테스트 ===


class TestCorrector한국어처리:
    """한국어 텍스트 처리 관련 테스트."""

    async def test_한국어_NFC_정규화(self) -> None:
        """보정 후 한국어 텍스트가 NFC 정규화되는지 확인."""
        import unicodedata

        # NFD 형식의 한국어 (자모 분리형)
        nfd_text = unicodedata.normalize("NFD", "한국어")
        nfc_text = unicodedata.normalize("NFC", "한국어")

        merged = _make_merged_result([
            (nfd_text, "S0", 0.0, 5.0),
        ])

        # LLM 응답도 NFD 형식
        corrected_response = _make_ollama_response(
            f"[1] {unicodedata.normalize('NFD', '한국어 보정')}"
        )

        manager = _make_mock_manager()

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_mock_urlopen(corrected_response)

            corrector = Corrector.__new__(Corrector)
            corrector._manager = manager
            corrector._batch_size = 10
            corrector._timeout = 120

            result = await corrector.correct(merged)

        # NFC 정규화 확인
        assert result.utterances[0].text == unicodedata.normalize(
            "NFC", "한국어 보정"
        )

    async def test_한국어_조사_보정(self) -> None:
        """한국어 조사 오류 보정 시나리오."""
        merged = _make_merged_result([
            ("프로젝트를 진행 상황을 말씀해 주세요", "S0", 0.0, 5.0),
        ])

        corrected_response = _make_ollama_response(
            "[1] 프로젝트 진행 상황을 말씀해 주세요"
        )

        manager = _make_mock_manager()

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_mock_urlopen(corrected_response)

            corrector = Corrector.__new__(Corrector)
            corrector._manager = manager
            corrector._batch_size = 10
            corrector._timeout = 120

            result = await corrector.correct(merged)

        assert result.utterances[0].text == "프로젝트 진행 상황을 말씀해 주세요"
        assert result.utterances[0].was_corrected is True

    async def test_특수문자_포함_보정(self) -> None:
        """특수문자가 포함된 텍스트의 보정."""
        merged = _make_merged_result([
            ("매출이 120프로 증가했습니다!", "S0", 0.0, 5.0),
        ])

        corrected_response = _make_ollama_response(
            "[1] 매출이 120% 증가했습니다!"
        )

        manager = _make_mock_manager()

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_mock_urlopen(corrected_response)

            corrector = Corrector.__new__(Corrector)
            corrector._manager = manager
            corrector._batch_size = 10
            corrector._timeout = 120

            result = await corrector.correct(merged)

        assert result.utterances[0].text == "매출이 120% 증가했습니다!"
        assert result.utterances[0].was_corrected is True


# === _create_ollama_client 테스트 ===


class TestOllamaClient:
    """Ollama 연결 관련 테스트."""

    def test_연결_성공(self) -> None:
        """Ollama 서버 연결 성공 시 설정 딕셔너리 반환."""
        tags_response = json.dumps({"models": []}).encode("utf-8")
        mock_resp = _make_mock_urlopen(tags_response)

        corrector = Corrector.__new__(Corrector)
        corrector._host = "http://127.0.0.1:11434"
        corrector._model_name = "exaone3.5:7.8b-instruct-q4_K_M"
        corrector._temperature = 0.3
        corrector._max_context = 8192
        corrector._timeout = 120

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = mock_resp

            client_config = corrector._create_ollama_client()

        assert client_config["host"] == "http://127.0.0.1:11434"
        assert client_config["model"] == "exaone3.5:7.8b-instruct-q4_K_M"
        assert client_config["temperature"] == 0.3

    def test_연결_실패(self) -> None:
        """Ollama 서버 연결 실패 시 OllamaConnectionError 발생."""
        import urllib.error

        corrector = Corrector.__new__(Corrector)
        corrector._host = "http://127.0.0.1:99999"
        corrector._model_name = "test"
        corrector._temperature = 0.3
        corrector._max_context = 8192
        corrector._timeout = 120

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError(
                "Connection refused"
            )

            with pytest.raises(OllamaConnectionError):
                corrector._create_ollama_client()


# === _call_ollama 테스트 ===


class TestCallOllama:
    """Ollama API 호출 테스트."""

    def test_정상_호출(self) -> None:
        """정상적인 API 호출 및 응답 파싱."""
        response = _make_ollama_response("[1] 보정된 텍스트")
        mock_resp = _make_mock_urlopen(response)

        corrector = Corrector.__new__(Corrector)

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = mock_resp

            client_config = {
                "host": "http://127.0.0.1:11434",
                "model": "test",
                "temperature": 0.3,
                "num_ctx": 8192,
                "timeout": 120,
            }
            result = corrector._call_ollama(client_config, "[1] 원본")

        assert "[1] 보정된 텍스트" in result

    def test_JSON_파싱_실패(self) -> None:
        """JSON 파싱 실패 시 CorrectionError 발생."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        corrector = Corrector.__new__(Corrector)

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = mock_resp

            client_config = {
                "host": "http://127.0.0.1:11434",
                "model": "test",
                "temperature": 0.3,
                "num_ctx": 8192,
                "timeout": 120,
            }

            with pytest.raises(CorrectionError, match="JSON 파싱 실패"):
                corrector._call_ollama(client_config, "[1] 텍스트")

    def test_타임아웃(self) -> None:
        """타임아웃 시 OllamaTimeoutError 발생."""
        import urllib.error

        corrector = Corrector.__new__(Corrector)

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError(
                "urlopen error timed out"
            )

            client_config = {
                "host": "http://127.0.0.1:11434",
                "model": "test",
                "temperature": 0.3,
                "num_ctx": 8192,
                "timeout": 120,
            }

            with pytest.raises(OllamaTimeoutError):
                corrector._call_ollama(client_config, "[1] 텍스트")

    def test_TimeoutError_직접(self) -> None:
        """socket.timeout (TimeoutError) 발생 시 OllamaTimeoutError로 변환."""
        corrector = Corrector.__new__(Corrector)

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = TimeoutError("timed out")

            client_config = {
                "host": "http://127.0.0.1:11434",
                "model": "test",
                "temperature": 0.3,
                "num_ctx": 8192,
                "timeout": 120,
            }

            with pytest.raises(OllamaTimeoutError):
                corrector._call_ollama(client_config, "[1] 텍스트")


# === 에러 계층 테스트 ===


class TestErrorHierarchy:
    """에러 클래스 계층 구조 테스트."""

    def test_OllamaConnectionError_is_CorrectionError(self) -> None:
        """OllamaConnectionError는 CorrectionError의 하위 클래스."""
        assert issubclass(OllamaConnectionError, CorrectionError)

    def test_OllamaTimeoutError_is_CorrectionError(self) -> None:
        """OllamaTimeoutError는 CorrectionError의 하위 클래스."""
        assert issubclass(OllamaTimeoutError, CorrectionError)

    def test_EmptyInputError_is_CorrectionError(self) -> None:
        """EmptyInputError는 CorrectionError의 하위 클래스."""
        assert issubclass(EmptyInputError, CorrectionError)


# === ModelLoadManager 연동 테스트 ===


class TestModelManagerIntegration:
    """ModelLoadManager 연동 테스트."""

    async def test_acquire_호출_확인(self) -> None:
        """correct() 실행 시 ModelLoadManager.acquire()가 호출되는지 확인."""
        merged = _make_merged_result([
            ("테스트", "S0", 0.0, 5.0),
        ])

        corrected_response = _make_ollama_response("[1] 테스트")
        manager = _make_mock_manager()

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_mock_urlopen(corrected_response)

            corrector = Corrector.__new__(Corrector)
            corrector._manager = manager
            corrector._batch_size = 10
            corrector._timeout = 120

            await corrector.correct(merged)

        # acquire가 "exaone" 이름으로 호출되었는지 확인
        manager.acquire.assert_called_once()
        call_args = manager.acquire.call_args
        assert call_args[0][0] == "exaone"

    async def test_화자_정보_보존(self) -> None:
        """보정 후에도 화자 정보가 유지되는지 확인."""
        merged = _make_merged_result([
            ("발화1", "SPEAKER_00", 0.0, 5.0),
            ("발화2", "SPEAKER_01", 5.0, 10.0),
        ])

        corrected_response = _make_ollama_response(
            "[1] 발화1\n[2] 발화2"
        )

        manager = _make_mock_manager()

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_mock_urlopen(corrected_response)

            corrector = Corrector.__new__(Corrector)
            corrector._manager = manager
            corrector._batch_size = 10
            corrector._timeout = 120

            result = await corrector.correct(merged)

        assert result.utterances[0].speaker == "SPEAKER_00"
        assert result.utterances[1].speaker == "SPEAKER_01"
        assert result.num_speakers == 2

    async def test_시간_정보_보존(self) -> None:
        """보정 후에도 시간 정보가 유지되는지 확인."""
        merged = _make_merged_result([
            ("텍스트", "S0", 3.5, 8.2),
        ])

        corrected_response = _make_ollama_response("[1] 텍스트")

        manager = _make_mock_manager()

        with patch("steps.corrector.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_mock_urlopen(corrected_response)

            corrector = Corrector.__new__(Corrector)
            corrector._manager = manager
            corrector._batch_size = 10
            corrector._timeout = 120

            result = await corrector.correct(merged)

        assert result.utterances[0].start == pytest.approx(3.5)
        assert result.utterances[0].end == pytest.approx(8.2)
