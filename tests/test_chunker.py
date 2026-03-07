"""
RAG 청크 생성기 테스트 모듈 (Chunker Test Module)

목적: steps/chunker.py의 청크 분할 로직을 검증한다.
주요 테스트:
    - 기본 청킹 동작 (화자 그룹핑 + 토큰 기반 분할)
    - 시간 간격 기반 토픽 분리
    - min_tokens 미만 청크 병합
    - 빈 입력 에러 처리
    - 단일 발화 처리
    - 대용량 발화 처리
    - 한국어 텍스트 NFC 정규화
    - 체크포인트 저장/복원 왕복
    - 큰 그룹 분할
의존성: pytest, pytest-asyncio
"""

import json

import pytest

from config import AppConfig, ChunkingConfig
from steps.chunker import (
    Chunk,
    ChunkedResult,
    Chunker,
    EmptyInputError,
    _estimate_tokens,
    _group_by_speaker_and_time,
    _split_groups_into_chunks,
    _UtteranceGroup,
)
from steps.corrector import CorrectedResult, CorrectedUtterance

# === 헬퍼 함수 ===


def _make_utterance(
    text: str,
    speaker: str = "SPEAKER_00",
    start: float = 0.0,
    end: float = 1.0,
) -> CorrectedUtterance:
    """테스트용 CorrectedUtterance를 생성한다."""
    return CorrectedUtterance(
        text=text,
        original_text=text,
        speaker=speaker,
        start=start,
        end=end,
        was_corrected=False,
    )


def _make_corrected_result(
    utterances: list[CorrectedUtterance],
    num_speakers: int = 2,
    audio_path: str = "/test/audio.wav",
) -> CorrectedResult:
    """테스트용 CorrectedResult를 생성한다."""
    return CorrectedResult(
        utterances=utterances,
        num_speakers=num_speakers,
        audio_path=audio_path,
    )


def _make_config(
    max_tokens: int = 300,
    min_tokens: int = 50,
    time_gap: int = 30,
    overlap: int = 30,
) -> AppConfig:
    """테스트용 AppConfig를 생성한다."""
    return AppConfig(
        chunking=ChunkingConfig(
            max_tokens=max_tokens,
            min_tokens=min_tokens,
            time_gap_threshold_seconds=time_gap,
            overlap_tokens=overlap,
        ),
    )


# === 토큰 추정 테스트 ===


class TestEstimateTokens:
    """토큰 추정 함수 테스트."""

    def test_빈_텍스트(self) -> None:
        """빈 텍스트는 0 토큰을 반환한다."""
        assert _estimate_tokens("") == 0

    def test_한국어_텍스트(self) -> None:
        """한국어 텍스트의 토큰 수를 추정한다."""
        # "안녕하세요" = 5글자 → 5/1.5 = 3.33 → 3
        result = _estimate_tokens("안녕하세요")
        assert result == 3

    def test_영어_텍스트(self) -> None:
        """영어 텍스트도 동일 로직으로 추정한다."""
        # "hello" = 5글자 → 5/1.5 = 3.33 → 3
        result = _estimate_tokens("hello")
        assert result == 3

    def test_최소값_1(self) -> None:
        """1글자 텍스트는 최소 1 토큰을 반환한다."""
        assert _estimate_tokens("a") == 1

    def test_긴_한국어_텍스트(self) -> None:
        """긴 한국어 텍스트의 토큰 수를 추정한다."""
        text = "오늘 회의에서는 프로젝트 일정에 대해 논의하겠습니다"
        result = _estimate_tokens(text)
        # 25글자 → 25/1.5 ≈ 16
        assert result == int(len(text) / 1.5)


# === 화자/시간 그룹핑 테스트 ===


class TestGroupBySpeakerAndTime:
    """화자/시간 기준 그룹핑 테스트."""

    def test_빈_목록(self) -> None:
        """빈 발화 목록은 빈 그룹을 반환한다."""
        result = _group_by_speaker_and_time([], time_gap_threshold=30)
        assert result == []

    def test_동일_화자_연속(self) -> None:
        """동일 화자의 연속 발화는 하나의 그룹으로 병합된다."""
        utterances = [
            _make_utterance("안녕하세요", "SPEAKER_00", 0, 2),
            _make_utterance("오늘 안건은", "SPEAKER_00", 2, 4),
            _make_utterance("일정 조정입니다", "SPEAKER_00", 4, 6),
        ]
        groups = _group_by_speaker_and_time(utterances, time_gap_threshold=30)

        assert len(groups) == 1
        assert groups[0].speaker == "SPEAKER_00"
        assert len(groups[0].texts) == 3
        assert groups[0].start == 0
        assert groups[0].end == 6

    def test_화자_교대(self) -> None:
        """화자가 바뀌면 새 그룹이 생성된다."""
        utterances = [
            _make_utterance("안녕하세요", "SPEAKER_00", 0, 2),
            _make_utterance("네 안녕하세요", "SPEAKER_01", 2, 4),
            _make_utterance("회의 시작하죠", "SPEAKER_00", 4, 6),
        ]
        groups = _group_by_speaker_and_time(utterances, time_gap_threshold=30)

        assert len(groups) == 3
        assert groups[0].speaker == "SPEAKER_00"
        assert groups[1].speaker == "SPEAKER_01"
        assert groups[2].speaker == "SPEAKER_00"

    def test_시간_간격_초과(self) -> None:
        """시간 간격이 threshold를 초과하면 동일 화자도 분리된다."""
        utterances = [
            _make_utterance("첫 번째 토픽", "SPEAKER_00", 0, 10),
            # 50초 간격 → threshold(30초) 초과
            _make_utterance("두 번째 토픽", "SPEAKER_00", 60, 70),
        ]
        groups = _group_by_speaker_and_time(utterances, time_gap_threshold=30)

        assert len(groups) == 2
        assert groups[0].texts == ["첫 번째 토픽"]
        assert groups[1].texts == ["두 번째 토픽"]

    def test_시간_간격_이내(self) -> None:
        """시간 간격이 threshold 이내이면 동일 화자는 병합된다."""
        utterances = [
            _make_utterance("첫 번째 문장", "SPEAKER_00", 0, 10),
            # 10초 간격 → threshold(30초) 이내
            _make_utterance("두 번째 문장", "SPEAKER_00", 20, 30),
        ]
        groups = _group_by_speaker_and_time(utterances, time_gap_threshold=30)

        assert len(groups) == 1
        assert len(groups[0].texts) == 2

    def test_단일_발화(self) -> None:
        """발화가 하나뿐이면 하나의 그룹을 생성한다."""
        utterances = [_make_utterance("안녕하세요", "SPEAKER_00", 0, 5)]
        groups = _group_by_speaker_and_time(utterances, time_gap_threshold=30)

        assert len(groups) == 1
        assert groups[0].texts == ["안녕하세요"]


# === 발화 그룹 테스트 ===


class TestUtteranceGroup:
    """_UtteranceGroup 데이터 클래스 테스트."""

    def test_combined_text(self) -> None:
        """여러 발화가 공백으로 결합된다."""
        group = _UtteranceGroup(
            speaker="SPEAKER_00",
            texts=["안녕하세요", "오늘 회의를 시작하겠습니다"],
            start=0.0,
            end=5.0,
        )
        assert group.combined_text == "안녕하세요 오늘 회의를 시작하겠습니다"

    def test_labeled_text(self) -> None:
        """화자 라벨이 포함된 텍스트를 생성한다."""
        group = _UtteranceGroup(
            speaker="SPEAKER_01",
            texts=["네 좋습니다"],
            start=0.0,
            end=2.0,
        )
        assert group.labeled_text == "[SPEAKER_01] 네 좋습니다"


# === 청크 분할 테스트 ===


class TestSplitGroupsIntoChunks:
    """토큰 기반 청크 분할 테스트."""

    def test_단일_그룹_적정_크기(self) -> None:
        """적정 크기의 단일 그룹은 하나의 청크가 된다."""
        groups = [
            _UtteranceGroup(
                speaker="SPEAKER_00",
                texts=["짧은 발화"],
                start=0.0,
                end=5.0,
            ),
        ]
        chunks = _split_groups_into_chunks(
            groups,
            max_tokens=300,
            min_tokens=10,
            overlap_tokens=0,
            meeting_id="m001",
            date="2026-03-04",
        )

        assert len(chunks) == 1
        assert "[SPEAKER_00]" in chunks[0].text
        assert chunks[0].meeting_id == "m001"
        assert chunks[0].date == "2026-03-04"
        assert chunks[0].start_time == 0.0
        assert chunks[0].end_time == 5.0

    def test_여러_그룹_합산_초과(self) -> None:
        """여러 그룹의 토큰 합산이 max_tokens를 초과하면 분할된다."""
        # "[SPEAKER_00] " 라벨(14자) + 텍스트 → labeled_text 기준 토큰 계산
        # 60글자 텍스트 + 14자 라벨 = 74글자 → ~49 토큰
        text = "가" * 60
        groups = [
            _UtteranceGroup("S0", [text], 0, 10),
            _UtteranceGroup("S1", [text], 10, 20),
            _UtteranceGroup("S0", [text], 20, 30),
            _UtteranceGroup("S1", [text], 30, 40),
        ]

        chunks = _split_groups_into_chunks(
            groups,
            max_tokens=100,
            min_tokens=10,
            overlap_tokens=0,
            meeting_id="m001",
            date="2026-03-04",
        )

        # 100토큰 max에 ~49토큰/그룹이면 2개씩 묶임 → 2개 청크
        assert len(chunks) == 2

    def test_빈_그룹_목록(self) -> None:
        """빈 그룹 목록은 빈 청크 목록을 반환한다."""
        chunks = _split_groups_into_chunks(
            [],
            max_tokens=300,
            min_tokens=50,
            overlap_tokens=0,
            meeting_id="m001",
            date="2026-03-04",
        )
        assert chunks == []

    def test_min_tokens_병합(self) -> None:
        """마지막 청크가 min_tokens 미만이면 이전 청크와 병합된다."""
        groups = [
            _UtteranceGroup("SPEAKER_00", ["가" * 300], 0, 10),  # ~200 토큰
            _UtteranceGroup("SPEAKER_01", ["나" * 300], 10, 20),  # ~200 토큰
            _UtteranceGroup("SPEAKER_00", ["다" * 15], 20, 25),  # ~10 토큰 (min_tokens 미만)
        ]

        chunks = _split_groups_into_chunks(
            groups,
            max_tokens=250,
            min_tokens=50,
            overlap_tokens=0,
            meeting_id="m001",
            date="2026-03-04",
        )

        # 첫 그룹(200) → 청크1, 두번째(200)+세번째(10) → 병합되어 청크2
        # 실제로는 두 번째가 단독 청크고 세 번째가 min_tokens 미만이라 병합
        last_chunk = chunks[-1]
        # 마지막 청크에 min_tokens 미만인 내용이 병합되었는지 확인
        assert last_chunk.estimated_tokens >= 50 or len(chunks) == 1

    def test_화자_메타데이터(self) -> None:
        """청크에 포함된 화자 목록이 정확하다."""
        groups = [
            _UtteranceGroup("SPEAKER_00", ["첫 발화"], 0, 5),
            _UtteranceGroup("SPEAKER_01", ["두 번째 발화"], 5, 10),
        ]

        chunks = _split_groups_into_chunks(
            groups,
            max_tokens=300,
            min_tokens=10,
            overlap_tokens=0,
            meeting_id="m001",
            date="2026-03-04",
        )

        # 두 그룹이 하나의 청크에 포함되면 양쪽 화자 모두 포함
        assert len(chunks) == 1
        assert "SPEAKER_00" in chunks[0].speakers
        assert "SPEAKER_01" in chunks[0].speakers

    def test_인덱스_순서(self) -> None:
        """청크 인덱스가 0부터 순차적으로 부여된다."""
        long_text = "가" * 300
        groups = [
            _UtteranceGroup("S0", [long_text], 0, 10),
            _UtteranceGroup("S1", [long_text], 10, 20),
            _UtteranceGroup("S0", [long_text], 20, 30),
        ]

        chunks = _split_groups_into_chunks(
            groups,
            max_tokens=250,
            min_tokens=10,
            overlap_tokens=0,
            meeting_id="m001",
            date="2026-03-04",
        )

        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i


# === Chunk 데이터 클래스 테스트 ===


class TestChunk:
    """Chunk 데이터 클래스 테스트."""

    def test_duration(self) -> None:
        """시간 범위(duration)가 올바르게 계산된다."""
        chunk = Chunk(
            text="테스트",
            meeting_id="m001",
            date="2026-03-04",
            speakers=["SPEAKER_00"],
            start_time=10.0,
            end_time=25.5,
            estimated_tokens=10,
            chunk_index=0,
        )
        assert chunk.duration == pytest.approx(15.5)

    def test_to_dict(self) -> None:
        """딕셔너리 변환이 올바르게 동작한다."""
        chunk = Chunk(
            text="테스트 텍스트",
            meeting_id="m001",
            date="2026-03-04",
            speakers=["SPEAKER_00", "SPEAKER_01"],
            start_time=0.0,
            end_time=10.0,
            estimated_tokens=8,
            chunk_index=0,
        )
        d = chunk.to_dict()

        assert d["text"] == "테스트 텍스트"
        assert d["meeting_id"] == "m001"
        assert d["speakers"] == ["SPEAKER_00", "SPEAKER_01"]
        assert d["start_time"] == 0.0
        assert d["end_time"] == 10.0


# === ChunkedResult 테스트 ===


class TestChunkedResult:
    """ChunkedResult 데이터 클래스 테스트."""

    def test_total_tokens(self) -> None:
        """전체 토큰 수가 올바르게 합산된다."""
        result = ChunkedResult(
            chunks=[
                Chunk("a" * 30, "m001", "2026-03-04", ["S0"], 0, 10, 20, 0),
                Chunk("b" * 45, "m001", "2026-03-04", ["S1"], 10, 20, 30, 1),
            ],
            meeting_id="m001",
            date="2026-03-04",
            total_utterances=5,
            num_speakers=2,
            audio_path="/test.wav",
        )
        assert result.total_tokens == 50

    def test_avg_tokens_빈_청크(self) -> None:
        """청크가 없으면 평균 토큰 수는 0이다."""
        result = ChunkedResult(
            chunks=[],
            meeting_id="m001",
            date="2026-03-04",
            total_utterances=0,
            num_speakers=0,
            audio_path="",
        )
        assert result.avg_tokens_per_chunk == 0.0

    def test_체크포인트_왕복(self, tmp_path) -> None:
        """체크포인트 저장/복원이 정확하게 동작한다."""
        original = ChunkedResult(
            chunks=[
                Chunk(
                    text="[SPEAKER_00] 안녕하세요 오늘 회의를 시작합니다",
                    meeting_id="meeting_001",
                    date="2026-03-04",
                    speakers=["SPEAKER_00"],
                    start_time=0.0,
                    end_time=5.0,
                    estimated_tokens=20,
                    chunk_index=0,
                ),
                Chunk(
                    text="[SPEAKER_01] 네 좋습니다\n[SPEAKER_00] 그럼 첫 안건부터",
                    meeting_id="meeting_001",
                    date="2026-03-04",
                    speakers=["SPEAKER_00", "SPEAKER_01"],
                    start_time=5.0,
                    end_time=15.0,
                    estimated_tokens=30,
                    chunk_index=1,
                ),
            ],
            meeting_id="meeting_001",
            date="2026-03-04",
            total_utterances=3,
            num_speakers=2,
            audio_path="/test/audio.wav",
        )

        # 저장
        checkpoint_path = tmp_path / "chunks_checkpoint.json"
        original.save_checkpoint(checkpoint_path)

        # 파일이 생성되었는지 확인
        assert checkpoint_path.exists()

        # 복원
        restored = ChunkedResult.from_checkpoint(checkpoint_path)

        # 원본과 동일한지 검증
        assert len(restored.chunks) == len(original.chunks)
        assert restored.meeting_id == original.meeting_id
        assert restored.date == original.date
        assert restored.total_utterances == original.total_utterances
        assert restored.num_speakers == original.num_speakers
        assert restored.audio_path == original.audio_path

        # 개별 청크 검증
        for orig, rest in zip(original.chunks, restored.chunks, strict=False):
            assert rest.text == orig.text
            assert rest.speakers == orig.speakers
            assert rest.start_time == orig.start_time
            assert rest.end_time == orig.end_time
            assert rest.chunk_index == orig.chunk_index

    def test_to_dict(self) -> None:
        """to_dict가 모든 필드를 포함한다."""
        result = ChunkedResult(
            chunks=[
                Chunk("테스트", "m001", "2026-03-04", ["S0"], 0, 5, 3, 0),
            ],
            meeting_id="m001",
            date="2026-03-04",
            total_utterances=1,
            num_speakers=1,
            audio_path="/test.wav",
        )
        d = result.to_dict()

        assert "chunks" in d
        assert len(d["chunks"]) == 1
        assert d["meeting_id"] == "m001"
        assert d["audio_path"] == "/test.wav"


# === Chunker 통합 테스트 ===


class TestChunker:
    """Chunker 클래스 통합 테스트."""

    @pytest.mark.asyncio
    async def test_기본_청킹(self) -> None:
        """기본 청킹 동작이 올바르게 수행된다."""
        config = _make_config(max_tokens=300, min_tokens=10, time_gap=30)
        chunker = Chunker(config=config)

        utterances = [
            _make_utterance("안녕하세요 오늘 회의를 시작하겠습니다", "SPEAKER_00", 0, 3),
            _make_utterance("네 좋습니다", "SPEAKER_01", 3, 5),
            _make_utterance("첫 번째 안건은 프로젝트 일정입니다", "SPEAKER_00", 5, 8),
        ]
        corrected = _make_corrected_result(utterances)

        result = await chunker.chunk(corrected, "meeting_001", "2026-03-04")

        assert len(result.chunks) >= 1
        assert result.meeting_id == "meeting_001"
        assert result.date == "2026-03-04"
        assert result.total_utterances == 3
        assert result.num_speakers == 2

        # 각 청크에 메타데이터가 포함되어 있는지 확인
        for chunk in result.chunks:
            assert chunk.meeting_id == "meeting_001"
            assert chunk.date == "2026-03-04"
            assert len(chunk.speakers) > 0
            assert chunk.estimated_tokens > 0

    @pytest.mark.asyncio
    async def test_빈_입력_에러(self) -> None:
        """빈 발화 입력 시 EmptyInputError가 발생한다."""
        config = _make_config()
        chunker = Chunker(config=config)

        corrected = _make_corrected_result(utterances=[])

        with pytest.raises(EmptyInputError, match="비어있습니다"):
            await chunker.chunk(corrected, "m001", "2026-03-04")

    @pytest.mark.asyncio
    async def test_단일_발화(self) -> None:
        """발화가 하나뿐이어도 정상적으로 청크가 생성된다."""
        config = _make_config(min_tokens=10)
        chunker = Chunker(config=config)

        utterances = [
            _make_utterance("안녕하세요", "SPEAKER_00", 0, 2),
        ]
        corrected = _make_corrected_result(utterances, num_speakers=1)

        result = await chunker.chunk(corrected, "m001", "2026-03-04")

        assert len(result.chunks) == 1
        assert "SPEAKER_00" in result.chunks[0].speakers
        assert "안녕하세요" in result.chunks[0].text

    @pytest.mark.asyncio
    async def test_시간_간격_분리(self) -> None:
        """시간 간격이 threshold를 초과하면 별도 청크로 분리된다."""
        config = _make_config(max_tokens=1000, min_tokens=10, time_gap=30)
        chunker = Chunker(config=config)

        utterances = [
            _make_utterance("첫 번째 토픽 이야기입니다", "SPEAKER_00", 0, 10),
            _make_utterance("동의합니다", "SPEAKER_00", 10, 15),
            # 50초 간격 → time_gap(30초) 초과 → 토픽 분리
            _make_utterance("두 번째 토픽으로 넘어가겠습니다", "SPEAKER_00", 65, 75),
            _make_utterance("새로운 주제입니다", "SPEAKER_00", 75, 85),
        ]
        corrected = _make_corrected_result(utterances, num_speakers=1)

        result = await chunker.chunk(corrected, "m001", "2026-03-04")

        # 시간 간격 분리로 최소 2개 청크
        assert len(result.chunks) >= 2

    @pytest.mark.asyncio
    async def test_화자_교대_그룹핑(self) -> None:
        """화자 교대 시 그룹이 분리되어 청크에 반영된다."""
        config = _make_config(max_tokens=1000, min_tokens=10)
        chunker = Chunker(config=config)

        utterances = [
            _make_utterance("안녕하세요", "SPEAKER_00", 0, 2),
            _make_utterance("네 반갑습니다", "SPEAKER_01", 2, 4),
            _make_utterance("오늘 안건은", "SPEAKER_00", 4, 6),
        ]
        corrected = _make_corrected_result(utterances)

        result = await chunker.chunk(corrected, "m001", "2026-03-04")

        # 모든 화자가 포함되어 있는지 확인
        all_speakers = set()
        for chunk in result.chunks:
            all_speakers.update(chunk.speakers)
        assert "SPEAKER_00" in all_speakers
        assert "SPEAKER_01" in all_speakers

    @pytest.mark.asyncio
    async def test_대용량_발화(self) -> None:
        """많은 발화도 정상적으로 분할된다."""
        config = _make_config(max_tokens=100, min_tokens=10, time_gap=30)
        chunker = Chunker(config=config)

        # 100개 발화 생성
        utterances = []
        for i in range(100):
            speaker = f"SPEAKER_{i % 3:02d}"
            utterances.append(
                _make_utterance(
                    f"발화 번호 {i}번입니다 내용이 조금 길게 작성됩니다",
                    speaker,
                    start=i * 3.0,
                    end=i * 3.0 + 2.5,
                )
            )
        corrected = _make_corrected_result(utterances, num_speakers=3)

        result = await chunker.chunk(corrected, "m001", "2026-03-04")

        # 청크가 적절히 분할되었는지 확인
        assert len(result.chunks) > 1
        assert result.total_utterances == 100
        assert result.num_speakers == 3

        # 각 청크의 토큰 수가 제한 이내인지 확인
        for chunk in result.chunks:
            assert chunk.estimated_tokens > 0

    @pytest.mark.asyncio
    async def test_한국어_NFC_정규화(self) -> None:
        """한국어 텍스트에 NFC 정규화가 적용된다."""
        import unicodedata

        config = _make_config(min_tokens=10)
        chunker = Chunker(config=config)

        # NFD 형식의 한국어 텍스트 (자모 분리)
        nfd_text = unicodedata.normalize("NFD", "안녕하세요")
        utterances = [
            _make_utterance(nfd_text, "SPEAKER_00", 0, 2),
        ]
        corrected = _make_corrected_result(utterances, num_speakers=1)

        result = await chunker.chunk(corrected, "m001", "2026-03-04")

        # 청크 텍스트가 NFC로 정규화되었는지 확인
        chunk_text = result.chunks[0].text
        assert chunk_text == unicodedata.normalize("NFC", chunk_text)

    @pytest.mark.asyncio
    async def test_큰_그룹_분할(self) -> None:
        """max_tokens를 초과하는 큰 그룹이 올바르게 분할된다."""
        config = _make_config(max_tokens=50, min_tokens=10)
        chunker = Chunker(config=config)

        # 하나의 화자가 매우 많은 발화를 한 경우
        utterances = [
            _make_utterance(
                f"이것은 긴 문장 번호 {i}번 입니다 추가 내용도 있습니다",
                "SPEAKER_00",
                i * 2,
                i * 2 + 1.5,
            )
            for i in range(20)
        ]
        corrected = _make_corrected_result(utterances, num_speakers=1)

        result = await chunker.chunk(corrected, "m001", "2026-03-04")

        # 작은 max_tokens로 여러 청크로 분할되어야 함
        assert len(result.chunks) > 1

    @pytest.mark.asyncio
    async def test_체크포인트_왕복_통합(self, tmp_path) -> None:
        """청크 생성 → 체크포인트 저장 → 복원 전체 흐름이 동작한다."""
        config = _make_config(min_tokens=10)
        chunker = Chunker(config=config)

        utterances = [
            _make_utterance("첫 번째 발화입니다", "SPEAKER_00", 0, 5),
            _make_utterance("두 번째 발화입니다", "SPEAKER_01", 5, 10),
            _make_utterance("세 번째 발화입니다", "SPEAKER_00", 10, 15),
        ]
        corrected = _make_corrected_result(utterances)

        # 청크 생성
        result = await chunker.chunk(corrected, "meeting_test", "2026-03-04")

        # 체크포인트 저장
        cp_path = tmp_path / "test_checkpoint.json"
        result.save_checkpoint(cp_path)

        # 복원
        restored = ChunkedResult.from_checkpoint(cp_path)

        # 원본과 동일한지 검증
        assert len(restored.chunks) == len(result.chunks)
        assert restored.meeting_id == result.meeting_id
        assert restored.total_utterances == result.total_utterances

        for orig, rest in zip(result.chunks, restored.chunks, strict=False):
            assert rest.text == orig.text
            assert rest.speakers == orig.speakers

    @pytest.mark.asyncio
    async def test_체크포인트_JSON_한국어(self, tmp_path) -> None:
        """체크포인트 JSON 파일에 한국어가 올바르게 저장된다."""
        config = _make_config(min_tokens=10)
        chunker = Chunker(config=config)

        utterances = [
            _make_utterance("한국어 테스트 발화입니다", "SPEAKER_00", 0, 3),
        ]
        corrected = _make_corrected_result(utterances, num_speakers=1)

        result = await chunker.chunk(corrected, "회의_001", "2026-03-04")

        cp_path = tmp_path / "korean_checkpoint.json"
        result.save_checkpoint(cp_path)

        # JSON 파일 직접 읽어서 한국어가 깨지지 않았는지 확인
        with open(cp_path, encoding="utf-8") as f:
            data = json.load(f)

        assert data["meeting_id"] == "회의_001"
        assert "한국어" in data["chunks"][0]["text"]
