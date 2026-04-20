"""
STT + 화자분리 병합기 테스트 모듈 (Merger Test Module)

목적: steps/merger.py의 Merger 클래스와 관련 유틸리티 함수를 검증한다.
주요 테스트:
    - 정상 병합 (완전 포함, 부분 겹침)
    - 다중 화자 겹침 시 최대 겹침 화자 선택
    - 화자 매칭 실패 시 UNKNOWN 할당
    - 빈 입력 처리
    - 체크포인트 라운드트립 (저장/복원)
    - 시간순 정렬 보장
    - 한국어 텍스트 보존
    - 에러 처리
의존성: pytest, pytest-asyncio
"""

import pytest

from steps.diarizer import DiarizationResult, DiarizationSegment
from steps.merger import (
    UNKNOWN_SPEAKER,
    EmptySegmentsError,
    MergedResult,
    MergedUtterance,
    Merger,
    _calculate_overlap,
    _find_best_speaker,
)
from steps.transcriber import TranscriptResult, TranscriptSegment

# === 헬퍼 함수 ===


def _make_transcript(
    segments: list[tuple[str, float, float]],
    audio_path: str = "/tmp/test.wav",
) -> TranscriptResult:
    """테스트용 TranscriptResult를 생성한다.

    Args:
        segments: (text, start, end) 튜플 리스트
        audio_path: 오디오 파일 경로

    Returns:
        TranscriptResult 인스턴스
    """
    stt_segments = [
        TranscriptSegment(text=text, start=start, end=end) for text, start, end in segments
    ]
    full_text = " ".join(seg[0] for seg in segments)
    return TranscriptResult(
        segments=stt_segments,
        full_text=full_text,
        language="ko",
        audio_path=audio_path,
    )


def _make_diarization(
    segments: list[tuple[str, float, float]],
    audio_path: str = "/tmp/test.wav",
) -> DiarizationResult:
    """테스트용 DiarizationResult를 생성한다.

    Args:
        segments: (speaker, start, end) 튜플 리스트
        audio_path: 오디오 파일 경로

    Returns:
        DiarizationResult 인스턴스
    """
    dia_segments = [
        DiarizationSegment(speaker=speaker, start=start, end=end)
        for speaker, start, end in segments
    ]
    unique_speakers = set(seg[0] for seg in segments)
    return DiarizationResult(
        segments=dia_segments,
        num_speakers=len(unique_speakers),
        audio_path=audio_path,
    )


# === _calculate_overlap 테스트 ===


class Test겹침계산:
    """_calculate_overlap 함수의 단위 테스트."""

    def test_완전_겹침(self) -> None:
        """STT 구간이 화자 구간 안에 완전히 포함될 때."""
        overlap = _calculate_overlap(5.0, 10.0, 3.0, 15.0)
        assert overlap == pytest.approx(5.0)

    def test_부분_겹침_왼쪽(self) -> None:
        """STT 구간이 화자 구간 왼쪽에서 겹칠 때."""
        overlap = _calculate_overlap(3.0, 8.0, 5.0, 15.0)
        assert overlap == pytest.approx(3.0)

    def test_부분_겹침_오른쪽(self) -> None:
        """STT 구간이 화자 구간 오른쪽에서 겹칠 때."""
        overlap = _calculate_overlap(10.0, 18.0, 5.0, 15.0)
        assert overlap == pytest.approx(5.0)

    def test_겹침_없음_앞(self) -> None:
        """화자 구간이 STT 구간보다 앞에 있을 때."""
        overlap = _calculate_overlap(10.0, 15.0, 1.0, 5.0)
        assert overlap == pytest.approx(0.0)

    def test_겹침_없음_뒤(self) -> None:
        """화자 구간이 STT 구간보다 뒤에 있을 때."""
        overlap = _calculate_overlap(1.0, 5.0, 10.0, 15.0)
        assert overlap == pytest.approx(0.0)

    def test_정확히_접촉(self) -> None:
        """두 구간이 경계에서 정확히 접촉할 때 (겹침 0)."""
        overlap = _calculate_overlap(5.0, 10.0, 10.0, 15.0)
        assert overlap == pytest.approx(0.0)

    def test_동일_구간(self) -> None:
        """두 구간이 완전히 동일할 때."""
        overlap = _calculate_overlap(5.0, 10.0, 5.0, 10.0)
        assert overlap == pytest.approx(5.0)

    def test_화자구간이_STT안에_포함(self) -> None:
        """화자 구간이 STT 구간 안에 완전히 포함될 때."""
        overlap = _calculate_overlap(0.0, 20.0, 5.0, 10.0)
        assert overlap == pytest.approx(5.0)


# === _find_best_speaker 테스트 ===


class Test최적화자찾기:
    """_find_best_speaker 함수의 단위 테스트."""

    def test_단일_화자_완전_겹침(self) -> None:
        """화자 구간이 STT 구간을 완전 포함할 때."""
        stt_seg = TranscriptSegment(text="안녕하세요", start=5.0, end=10.0)
        dia_segments = [
            DiarizationSegment(speaker="SPEAKER_00", start=0.0, end=20.0),
        ]
        assert _find_best_speaker(stt_seg, dia_segments) == "SPEAKER_00"

    def test_다중_화자_최대_겹침_선택(self) -> None:
        """여러 화자가 겹칠 때 최대 겹침 화자를 선택한다."""
        stt_seg = TranscriptSegment(text="테스트", start=5.0, end=15.0)
        dia_segments = [
            # 3초 겹침 (5~8)
            DiarizationSegment(speaker="SPEAKER_00", start=0.0, end=8.0),
            # 7초 겹침 (8~15)
            DiarizationSegment(speaker="SPEAKER_01", start=8.0, end=20.0),
        ]
        assert _find_best_speaker(stt_seg, dia_segments) == "SPEAKER_01"

    def test_화자_없음(self) -> None:
        """화자 세그먼트 리스트가 비어있을 때 UNKNOWN 반환."""
        stt_seg = TranscriptSegment(text="안녕", start=5.0, end=10.0)
        assert _find_best_speaker(stt_seg, []) == UNKNOWN_SPEAKER

    def test_겹침_없는_화자(self) -> None:
        """STT 구간과 겹치는 화자가 없을 때 UNKNOWN 반환."""
        stt_seg = TranscriptSegment(text="안녕", start=50.0, end=55.0)
        dia_segments = [
            DiarizationSegment(speaker="SPEAKER_00", start=0.0, end=10.0),
            DiarizationSegment(speaker="SPEAKER_01", start=10.0, end=20.0),
        ]
        assert _find_best_speaker(stt_seg, dia_segments) == UNKNOWN_SPEAKER

    def test_동률_시_먼저_나온_화자(self) -> None:
        """겹침이 동일할 때 먼저 나온 화자를 유지한다 (> 조건으로 인해)."""
        stt_seg = TranscriptSegment(text="테스트", start=5.0, end=15.0)
        dia_segments = [
            # 5초 겹침 (5~10)
            DiarizationSegment(speaker="SPEAKER_00", start=0.0, end=10.0),
            # 5초 겹침 (10~15)
            DiarizationSegment(speaker="SPEAKER_01", start=10.0, end=20.0),
        ]
        # 동률이면 먼저 매칭된 SPEAKER_00 유지 (strictly greater로 교체하지 않음)
        assert _find_best_speaker(stt_seg, dia_segments) == "SPEAKER_00"

    def test_세_화자_중간_선택(self) -> None:
        """세 화자 중 중간 화자의 겹침이 가장 클 때."""
        stt_seg = TranscriptSegment(text="회의", start=10.0, end=20.0)
        dia_segments = [
            # 2초 겹침 (10~12)
            DiarizationSegment(speaker="SPEAKER_00", start=5.0, end=12.0),
            # 6초 겹침 (12~18)
            DiarizationSegment(speaker="SPEAKER_01", start=12.0, end=18.0),
            # 2초 겹침 (18~20)
            DiarizationSegment(speaker="SPEAKER_02", start=18.0, end=25.0),
        ]
        assert _find_best_speaker(stt_seg, dia_segments) == "SPEAKER_01"


# === MergedUtterance 테스트 ===


class TestMergedUtterance:
    """MergedUtterance 데이터 클래스의 단위 테스트."""

    def test_생성(self) -> None:
        """기본 생성 및 필드 접근."""
        u = MergedUtterance(text="안녕하세요", speaker="SPEAKER_00", start=0.0, end=5.0)
        assert u.text == "안녕하세요"
        assert u.speaker == "SPEAKER_00"
        assert u.start == 0.0
        assert u.end == 5.0

    def test_duration(self) -> None:
        """duration 프로퍼티 계산."""
        u = MergedUtterance(text="테스트", speaker="SPEAKER_00", start=3.0, end=8.0)
        assert u.duration == pytest.approx(5.0)

    def test_to_dict(self) -> None:
        """딕셔너리 변환."""
        u = MergedUtterance(text="한국어", speaker="SPEAKER_01", start=1.0, end=3.5)
        d = u.to_dict()
        assert d == {
            "text": "한국어",
            "speaker": "SPEAKER_01",
            "start": 1.0,
            "end": 3.5,
        }


# === MergedResult 테스트 ===


class TestMergedResult:
    """MergedResult 데이터 클래스의 단위 테스트."""

    def test_total_duration(self) -> None:
        """total_duration 프로퍼티 계산."""
        result = MergedResult(
            utterances=[
                MergedUtterance(text="a", speaker="S0", start=0.0, end=5.0),
                MergedUtterance(text="b", speaker="S1", start=5.0, end=12.0),
            ],
            num_speakers=2,
            audio_path="/tmp/test.wav",
        )
        assert result.total_duration == pytest.approx(12.0)

    def test_total_duration_빈_결과(self) -> None:
        """빈 utterance 리스트에서 total_duration은 0."""
        result = MergedResult(utterances=[], num_speakers=0, audio_path="/tmp/test.wav")
        assert result.total_duration == pytest.approx(0.0)

    def test_speakers_UNKNOWN_제외(self) -> None:
        """speakers 프로퍼티는 UNKNOWN을 제외한다."""
        result = MergedResult(
            utterances=[
                MergedUtterance(text="a", speaker="SPEAKER_00", start=0, end=5),
                MergedUtterance(text="b", speaker=UNKNOWN_SPEAKER, start=5, end=10),
                MergedUtterance(text="c", speaker="SPEAKER_01", start=10, end=15),
            ],
            num_speakers=2,
            audio_path="/tmp/test.wav",
            unknown_count=1,
        )
        assert result.speakers == ["SPEAKER_00", "SPEAKER_01"]

    def test_to_dict(self) -> None:
        """딕셔너리 변환."""
        result = MergedResult(
            utterances=[
                MergedUtterance(text="안녕", speaker="S0", start=0.0, end=3.0),
            ],
            num_speakers=1,
            audio_path="/tmp/test.wav",
            unknown_count=0,
        )
        d = result.to_dict()
        assert d["num_speakers"] == 1
        assert d["audio_path"] == "/tmp/test.wav"
        assert d["unknown_count"] == 0
        assert len(d["utterances"]) == 1
        assert d["utterances"][0]["text"] == "안녕"

    def test_체크포인트_라운드트립(self, tmp_path) -> None:
        """체크포인트 저장/복원 라운드트립."""
        original = MergedResult(
            utterances=[
                MergedUtterance(
                    text="안녕하세요",
                    speaker="SPEAKER_00",
                    start=0.0,
                    end=5.0,
                ),
                MergedUtterance(
                    text="반갑습니다",
                    speaker="SPEAKER_01",
                    start=5.0,
                    end=10.0,
                ),
                MergedUtterance(
                    text="회의 시작하겠습니다",
                    speaker=UNKNOWN_SPEAKER,
                    start=10.0,
                    end=15.0,
                ),
            ],
            num_speakers=2,
            audio_path="/tmp/test.wav",
            unknown_count=1,
        )

        checkpoint_path = tmp_path / "sub" / "merged.json"
        original.save_checkpoint(checkpoint_path)

        # 파일이 생성되었는지 확인
        assert checkpoint_path.exists()

        # 복원
        restored = MergedResult.from_checkpoint(checkpoint_path)

        assert len(restored.utterances) == 3
        assert restored.num_speakers == 2
        assert restored.audio_path == "/tmp/test.wav"
        assert restored.unknown_count == 1

        # 텍스트 보존 확인
        assert restored.utterances[0].text == "안녕하세요"
        assert restored.utterances[0].speaker == "SPEAKER_00"
        assert restored.utterances[1].text == "반갑습니다"
        assert restored.utterances[2].speaker == UNKNOWN_SPEAKER

    def test_체크포인트_한국어_보존(self, tmp_path) -> None:
        """체크포인트 JSON에서 한국어가 이스케이프 없이 보존되는지 확인."""
        original = MergedResult(
            utterances=[
                MergedUtterance(
                    text="한국어 텍스트 테스트",
                    speaker="S0",
                    start=0.0,
                    end=5.0,
                ),
            ],
            num_speakers=1,
            audio_path="/tmp/test.wav",
        )

        checkpoint_path = tmp_path / "korean.json"
        original.save_checkpoint(checkpoint_path)

        # 파일 내용에 유니코드 이스케이프가 없는지 확인
        raw_content = checkpoint_path.read_text(encoding="utf-8")
        assert "한국어 텍스트 테스트" in raw_content
        assert "\\u" not in raw_content

    def test_체크포인트_파일_없음(self, tmp_path) -> None:
        """존재하지 않는 체크포인트 파일에서 복원 시 에러."""
        with pytest.raises(FileNotFoundError):
            MergedResult.from_checkpoint(tmp_path / "not_exist.json")


# === Merger 클래스 테스트 ===


class TestMerger정상병합:
    """Merger 클래스의 정상 병합 테스트."""

    @pytest.mark.asyncio
    async def test_완전_포함_병합(self) -> None:
        """STT 세그먼트가 화자 구간에 완전 포함될 때."""
        transcript = _make_transcript(
            [
                ("안녕하세요", 0.0, 5.0),
                ("오늘 회의를 시작하겠습니다", 5.0, 10.0),
            ]
        )
        diarization = _make_diarization(
            [
                ("SPEAKER_00", 0.0, 10.0),
            ]
        )

        merger = Merger()
        result = await merger.merge(transcript, diarization)

        assert len(result.utterances) == 2
        assert result.num_speakers == 1
        assert result.unknown_count == 0
        assert result.utterances[0].speaker == "SPEAKER_00"
        assert result.utterances[1].speaker == "SPEAKER_00"

    @pytest.mark.asyncio
    async def test_두_화자_교대(self) -> None:
        """두 화자가 교대로 발화하는 정상 케이스."""
        transcript = _make_transcript(
            [
                ("안녕하세요", 0.0, 5.0),
                ("네 안녕하세요", 6.0, 10.0),
                ("오늘 안건은", 11.0, 15.0),
                ("네 알겠습니다", 16.0, 20.0),
            ]
        )
        diarization = _make_diarization(
            [
                ("SPEAKER_00", 0.0, 5.5),
                ("SPEAKER_01", 5.5, 10.5),
                ("SPEAKER_00", 10.5, 15.5),
                ("SPEAKER_01", 15.5, 20.5),
            ]
        )

        merger = Merger()
        result = await merger.merge(transcript, diarization)

        assert len(result.utterances) == 4
        assert result.num_speakers == 2
        assert result.unknown_count == 0

        assert result.utterances[0].speaker == "SPEAKER_00"
        assert result.utterances[0].text == "안녕하세요"
        assert result.utterances[1].speaker == "SPEAKER_01"
        assert result.utterances[2].speaker == "SPEAKER_00"
        assert result.utterances[3].speaker == "SPEAKER_01"

    @pytest.mark.asyncio
    async def test_다중_화자_겹침(self) -> None:
        """STT 세그먼트가 여러 화자 구간에 걸칠 때 최대 겹침 화자 선택."""
        transcript = _make_transcript(
            [
                # 이 세그먼트는 SPEAKER_00과 3초, SPEAKER_01과 7초 겹침
                ("테스트 발화", 5.0, 15.0),
            ]
        )
        diarization = _make_diarization(
            [
                ("SPEAKER_00", 0.0, 8.0),  # 3초 겹침 (5~8)
                ("SPEAKER_01", 8.0, 20.0),  # 7초 겹침 (8~15)
            ]
        )

        merger = Merger()
        result = await merger.merge(transcript, diarization)

        assert result.utterances[0].speaker == "SPEAKER_01"

    @pytest.mark.asyncio
    async def test_세_화자_회의(self) -> None:
        """세 명의 화자가 참여하는 회의."""
        transcript = _make_transcript(
            [
                ("첫 번째 발화", 0.0, 5.0),
                ("두 번째 발화", 6.0, 10.0),
                ("세 번째 발화", 11.0, 15.0),
            ]
        )
        diarization = _make_diarization(
            [
                ("SPEAKER_00", 0.0, 5.5),
                ("SPEAKER_01", 5.5, 10.5),
                ("SPEAKER_02", 10.5, 15.5),
            ]
        )

        merger = Merger()
        result = await merger.merge(transcript, diarization)

        assert result.num_speakers == 3
        assert result.utterances[0].speaker == "SPEAKER_00"
        assert result.utterances[1].speaker == "SPEAKER_01"
        assert result.utterances[2].speaker == "SPEAKER_02"

    @pytest.mark.asyncio
    async def test_audio_path_보존(self) -> None:
        """결과의 audio_path가 TranscriptResult에서 가져와지는지 확인."""
        transcript = _make_transcript(
            [("테스트", 0.0, 5.0)],
            audio_path="/custom/path/audio.wav",
        )
        diarization = _make_diarization(
            [
                ("SPEAKER_00", 0.0, 10.0),
            ]
        )

        merger = Merger()
        result = await merger.merge(transcript, diarization)
        assert result.audio_path == "/custom/path/audio.wav"


class TestMergerUNKNOWN처리:
    """화자 매칭 실패 시 UNKNOWN 할당 테스트."""

    @pytest.mark.asyncio
    async def test_빈_화자_세그먼트(self) -> None:
        """화자분리 세그먼트가 비어있으면 모든 발화에 UNKNOWN 할당."""
        transcript = _make_transcript(
            [
                ("안녕하세요", 0.0, 5.0),
                ("반갑습니다", 5.0, 10.0),
            ]
        )
        diarization = _make_diarization([])

        merger = Merger()
        result = await merger.merge(transcript, diarization)

        assert len(result.utterances) == 2
        assert result.num_speakers == 0
        assert result.unknown_count == 2
        assert all(u.speaker == UNKNOWN_SPEAKER for u in result.utterances)

    @pytest.mark.asyncio
    async def test_부분_매칭(self) -> None:
        """일부 발화만 화자와 매칭되는 케이스."""
        transcript = _make_transcript(
            [
                ("첫 번째", 0.0, 5.0),  # 화자 구간 존재
                ("두 번째", 50.0, 55.0),  # 화자 구간 없음
            ]
        )
        diarization = _make_diarization(
            [
                ("SPEAKER_00", 0.0, 10.0),
            ]
        )

        merger = Merger()
        result = await merger.merge(transcript, diarization)

        assert result.utterances[0].speaker == "SPEAKER_00"
        assert result.utterances[1].speaker == UNKNOWN_SPEAKER
        assert result.unknown_count == 1


class TestMerger에러처리:
    """Merger 에러 처리 테스트."""

    @pytest.mark.asyncio
    async def test_빈_STT_세그먼트(self) -> None:
        """STT 세그먼트가 비어있으면 EmptySegmentsError 발생."""
        transcript = TranscriptResult(
            segments=[], full_text="", language="ko", audio_path="/tmp/t.wav"
        )
        diarization = _make_diarization(
            [
                ("SPEAKER_00", 0.0, 10.0),
            ]
        )

        merger = Merger()
        with pytest.raises(EmptySegmentsError):
            await merger.merge(transcript, diarization)


class TestMerger시간정렬:
    """시간순 정렬 관련 테스트."""

    @pytest.mark.asyncio
    async def test_역순_입력_정렬(self) -> None:
        """시간순이 아닌 입력도 결과는 시간순으로 정렬."""
        # 역순으로 세그먼트 생성
        transcript = TranscriptResult(
            segments=[
                TranscriptSegment(text="세 번째", start=10.0, end=15.0),
                TranscriptSegment(text="첫 번째", start=0.0, end=5.0),
                TranscriptSegment(text="두 번째", start=5.0, end=10.0),
            ],
            full_text="세 번째 첫 번째 두 번째",
            language="ko",
            audio_path="/tmp/test.wav",
        )
        diarization = _make_diarization(
            [
                ("SPEAKER_00", 0.0, 20.0),
            ]
        )

        merger = Merger()
        result = await merger.merge(transcript, diarization)

        # 시간순 정렬 확인
        assert result.utterances[0].text == "첫 번째"
        assert result.utterances[1].text == "두 번째"
        assert result.utterances[2].text == "세 번째"
        assert result.utterances[0].start < result.utterances[1].start
        assert result.utterances[1].start < result.utterances[2].start


class TestMerger한국어텍스트:
    """한국어 텍스트 관련 테스트."""

    @pytest.mark.asyncio
    async def test_한국어_텍스트_보존(self) -> None:
        """병합 후 한국어 텍스트가 원본 그대로 보존되는지 확인."""
        korean_texts = [
            "오늘 회의 안건은 세 가지입니다",
            "첫 번째는 프로젝트 일정 검토입니다",
            "네, 알겠습니다. 다음 안건으로 넘어가겠습니다",
        ]

        transcript = _make_transcript(
            [
                (korean_texts[0], 0.0, 5.0),
                (korean_texts[1], 5.0, 10.0),
                (korean_texts[2], 10.0, 15.0),
            ]
        )
        diarization = _make_diarization(
            [
                ("SPEAKER_00", 0.0, 5.5),
                ("SPEAKER_01", 5.5, 10.5),
                ("SPEAKER_00", 10.5, 15.5),
            ]
        )

        merger = Merger()
        result = await merger.merge(transcript, diarization)

        for i, text in enumerate(korean_texts):
            assert result.utterances[i].text == text

    @pytest.mark.asyncio
    async def test_특수문자_포함_텍스트(self) -> None:
        """특수문자가 포함된 텍스트 보존."""
        transcript = _make_transcript(
            [
                ("매출이 120% 증가했습니다!", 0.0, 5.0),
                ("Q4 실적은 어떤가요?", 5.0, 10.0),
            ]
        )
        diarization = _make_diarization(
            [
                ("SPEAKER_00", 0.0, 10.0),
            ]
        )

        merger = Merger()
        result = await merger.merge(transcript, diarization)

        assert result.utterances[0].text == "매출이 120% 증가했습니다!"
        assert result.utterances[1].text == "Q4 실적은 어떤가요?"


class TestMerger엣지케이스:
    """엣지 케이스 테스트."""

    @pytest.mark.asyncio
    async def test_단일_세그먼트(self) -> None:
        """세그먼트가 하나일 때."""
        transcript = _make_transcript([("안녕하세요", 0.0, 5.0)])
        diarization = _make_diarization([("SPEAKER_00", 0.0, 5.0)])

        merger = Merger()
        result = await merger.merge(transcript, diarization)

        assert len(result.utterances) == 1
        assert result.utterances[0].speaker == "SPEAKER_00"

    @pytest.mark.asyncio
    async def test_매우_짧은_세그먼트(self) -> None:
        """매우 짧은 시간의 세그먼트 처리."""
        transcript = _make_transcript([("네", 5.0, 5.1)])
        diarization = _make_diarization([("SPEAKER_00", 0.0, 10.0)])

        merger = Merger()
        result = await merger.merge(transcript, diarization)

        assert result.utterances[0].speaker == "SPEAKER_00"
        assert result.utterances[0].duration == pytest.approx(0.1)

    @pytest.mark.asyncio
    async def test_긴_회의_시뮬레이션(self) -> None:
        """1시간 회의 시뮬레이션 (200 세그먼트)."""
        # 200개 STT 세그먼트 (각 18초 간격으로 분포)
        stt_segments = [(f"발화 {i}", i * 18.0, i * 18.0 + 15.0) for i in range(200)]
        transcript = _make_transcript(stt_segments)

        # 50개 화자 세그먼트 (2명이 교대)
        dia_segments = [
            (
                f"SPEAKER_0{i % 2}",
                i * 72.0,
                (i + 1) * 72.0,
            )
            for i in range(50)
        ]
        diarization = _make_diarization(dia_segments)

        merger = Merger()
        result = await merger.merge(transcript, diarization)

        assert len(result.utterances) == 200
        assert result.num_speakers == 2

    @pytest.mark.asyncio
    async def test_speakers_프로퍼티(self) -> None:
        """speakers 프로퍼티가 UNKNOWN 제외하고 정렬된 리스트를 반환."""
        transcript = _make_transcript(
            [
                ("a", 0.0, 5.0),
                ("b", 5.0, 10.0),
                ("c", 50.0, 55.0),  # 매칭 안 됨
            ]
        )
        diarization = _make_diarization(
            [
                ("SPEAKER_01", 0.0, 5.5),
                ("SPEAKER_00", 5.5, 10.5),
            ]
        )

        merger = Merger()
        result = await merger.merge(transcript, diarization)

        assert result.speakers == ["SPEAKER_00", "SPEAKER_01"]
        assert UNKNOWN_SPEAKER not in result.speakers

    @pytest.mark.asyncio
    async def test_체크포인트_라운드트립_전체(self, tmp_path) -> None:
        """병합 → 체크포인트 저장 → 복원 전체 흐름."""
        transcript = _make_transcript(
            [
                ("안녕하세요", 0.0, 5.0),
                ("반갑습니다", 5.0, 10.0),
            ]
        )
        diarization = _make_diarization(
            [
                ("SPEAKER_00", 0.0, 5.5),
                ("SPEAKER_01", 5.5, 10.5),
            ]
        )

        merger = Merger()
        result = await merger.merge(transcript, diarization)

        # 체크포인트 저장
        cp_path = tmp_path / "merged_utterances.json"
        result.save_checkpoint(cp_path)

        # 복원
        restored = MergedResult.from_checkpoint(cp_path)

        assert len(restored.utterances) == len(result.utterances)
        assert restored.num_speakers == result.num_speakers
        assert restored.unknown_count == result.unknown_count

        for orig, rest in zip(result.utterances, restored.utterances, strict=False):
            assert orig.text == rest.text
            assert orig.speaker == rest.speaker
            assert orig.start == pytest.approx(rest.start)
            assert orig.end == pytest.approx(rest.end)
