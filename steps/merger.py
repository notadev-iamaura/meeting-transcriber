"""
STT + 화자분리 병합기 모듈 (Segment-Speaker Merger Module)

목적: STT 전사 세그먼트와 화자분리 세그먼트를 시간 기준으로 병합하여
     각 발화에 화자를 할당한 최종 utterance를 생성한다.
주요 기능:
    - STT 세그먼트와 화자 세그먼트의 시간 겹침 기반 병합
    - 겹침 구간이 가장 큰 화자 할당 (최대 겹침 전략)
    - 매칭 실패 시 "UNKNOWN" 화자 할당
    - JSON 체크포인트 저장/복원 지원
    - 비동기(async) 인터페이스 지원
의존성: steps/transcriber.py (TranscriptResult), steps/diarizer.py (DiarizationResult)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from steps.transcriber import TranscriptResult, TranscriptSegment
from steps.diarizer import DiarizationResult, DiarizationSegment

logger = logging.getLogger(__name__)

# 화자를 할당할 수 없을 때 사용하는 기본 라벨
UNKNOWN_SPEAKER = "UNKNOWN"


@dataclass
class MergedUtterance:
    """병합된 단일 발화를 나타내는 데이터 클래스.

    STT 세그먼트에 화자 정보가 결합된 최종 발화 단위.

    Attributes:
        text: 전사된 텍스트
        speaker: 할당된 화자 라벨 (예: "SPEAKER_00")
        start: 발화 시작 시간 (초)
        end: 발화 종료 시간 (초)
    """

    text: str
    speaker: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        """발화 구간의 길이 (초)."""
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화용).

        Returns:
            발화 데이터 딕셔너리
        """
        return asdict(self)


@dataclass
class MergedResult:
    """전체 병합 결과를 담는 데이터 클래스.

    Attributes:
        utterances: 병합된 발화 목록 (시간순 정렬)
        num_speakers: 감지된 화자 수
        audio_path: 원본 오디오 파일 경로 문자열
        unknown_count: 화자 매칭 실패 발화 수
    """

    utterances: list[MergedUtterance]
    num_speakers: int
    audio_path: str
    unknown_count: int = 0

    @property
    def total_duration(self) -> float:
        """전체 오디오 길이 추정치 (마지막 발화 종료 시간)."""
        if not self.utterances:
            return 0.0
        return max(u.end for u in self.utterances)

    @property
    def speakers(self) -> list[str]:
        """감지된 화자 라벨 목록 (UNKNOWN 제외, 중복 제거, 정렬)."""
        return sorted(
            set(
                u.speaker for u in self.utterances
                if u.speaker != UNKNOWN_SPEAKER
            )
        )

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화/체크포인트 저장용).

        Returns:
            전체 병합 결과 딕셔너리
        """
        return {
            "utterances": [u.to_dict() for u in self.utterances],
            "num_speakers": self.num_speakers,
            "audio_path": self.audio_path,
            "unknown_count": self.unknown_count,
        }

    def save_checkpoint(self, output_path: Path) -> None:
        """병합 결과를 JSON 파일로 저장한다 (체크포인트).

        Args:
            output_path: 저장할 JSON 파일 경로
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info(f"병합 체크포인트 저장: {output_path}")

    @classmethod
    def from_checkpoint(cls, checkpoint_path: Path) -> MergedResult:
        """체크포인트 JSON 파일에서 병합 결과를 복원한다.

        Args:
            checkpoint_path: 체크포인트 JSON 파일 경로

        Returns:
            복원된 MergedResult 인스턴스

        Raises:
            FileNotFoundError: 체크포인트 파일이 없을 때
            json.JSONDecodeError: JSON 파싱 실패 시
        """
        with open(checkpoint_path, encoding="utf-8") as f:
            data = json.load(f)

        utterances = [
            MergedUtterance(**u) for u in data.get("utterances", [])
        ]

        return cls(
            utterances=utterances,
            num_speakers=data.get("num_speakers", 0),
            audio_path=data.get("audio_path", ""),
            unknown_count=data.get("unknown_count", 0),
        )


class MergeError(Exception):
    """병합 처리 중 발생하는 에러의 기본 클래스."""


class EmptySegmentsError(MergeError):
    """STT 전사 세그먼트가 비어있을 때 발생한다."""


def _calculate_overlap(
    seg_start: float,
    seg_end: float,
    dia_start: float,
    dia_end: float,
) -> float:
    """두 시간 구간의 겹침 길이를 계산한다.

    Args:
        seg_start: 첫 번째 구간 시작 시간
        seg_end: 첫 번째 구간 종료 시간
        dia_start: 두 번째 구간 시작 시간
        dia_end: 두 번째 구간 종료 시간

    Returns:
        겹침 길이 (초). 겹치지 않으면 0.0
    """
    overlap_start = max(seg_start, dia_start)
    overlap_end = min(seg_end, dia_end)
    return max(0.0, overlap_end - overlap_start)


def _find_best_speaker(
    transcript_seg: TranscriptSegment,
    diarization_segments: list[DiarizationSegment],
) -> str:
    """STT 세그먼트에 가장 적합한 화자를 찾는다.

    각 화자 세그먼트와의 시간 겹침을 계산하여,
    겹침이 가장 큰 화자를 반환한다.

    Args:
        transcript_seg: STT 전사 세그먼트
        diarization_segments: 화자분리 세그먼트 목록

    Returns:
        최대 겹침 화자 라벨. 매칭 실패 시 "UNKNOWN"
    """
    best_speaker = UNKNOWN_SPEAKER
    max_overlap = 0.0

    for dia_seg in diarization_segments:
        # 빠른 스킵: 화자 세그먼트가 STT 세그먼트보다 뒤에 시작하면 탐색 종료 가능
        # (정렬된 상태에서 최적화)
        if dia_seg.start > transcript_seg.end:
            break

        # 화자 세그먼트가 STT 세그먼트보다 먼저 끝나면 건너뜀
        if dia_seg.end < transcript_seg.start:
            continue

        overlap = _calculate_overlap(
            transcript_seg.start,
            transcript_seg.end,
            dia_seg.start,
            dia_seg.end,
        )

        if overlap > max_overlap:
            max_overlap = overlap
            best_speaker = dia_seg.speaker

    return best_speaker


class Merger:
    """STT 전사 결과와 화자분리 결과를 병합하는 클래스.

    각 전사 세그먼트에 대해 시간 겹침이 가장 큰 화자를 할당하여
    (text, speaker, start, end) 형태의 발화 목록을 생성한다.

    사용 예시:
        merger = Merger()
        result = await merger.merge(transcript_result, diarization_result)
        for u in result.utterances:
            print(f"[{u.speaker}] {u.start:.1f}~{u.end:.1f}: {u.text}")
    """

    def _validate_inputs(
        self,
        transcript: TranscriptResult,
        diarization: DiarizationResult,
    ) -> None:
        """입력 데이터의 유효성을 검증한다.

        Args:
            transcript: STT 전사 결과
            diarization: 화자분리 결과

        Raises:
            EmptySegmentsError: STT 세그먼트가 비어있을 때
        """
        if not transcript.segments:
            raise EmptySegmentsError(
                "STT 전사 세그먼트가 비어있습니다. "
                "병합할 텍스트가 없습니다."
            )

        if not diarization.segments:
            logger.warning(
                "화자분리 세그먼트가 비어있습니다. "
                "모든 발화에 UNKNOWN 화자가 할당됩니다."
            )

    def _check_time_alignment(
        self,
        transcript: TranscriptResult,
        diarization: DiarizationResult,
    ) -> None:
        """두 결과의 시간 범위 일치 여부를 점검한다.

        시간 범위가 크게 다르면 경고를 로깅한다.
        (처리는 계속 진행)

        Args:
            transcript: STT 전사 결과
            diarization: 화자분리 결과
        """
        if not transcript.segments or not diarization.segments:
            return

        stt_end = transcript.segments[-1].end
        dia_end = max(seg.end for seg in diarization.segments)

        # 시간 차이가 10% 이상이면 경고
        if stt_end > 0 and abs(stt_end - dia_end) / stt_end > 0.1:
            logger.warning(
                f"STT와 화자분리의 시간 범위 불일치: "
                f"STT 종료={stt_end:.1f}초, 화자분리 종료={dia_end:.1f}초 "
                f"(차이: {abs(stt_end - dia_end):.1f}초)"
            )

    def _merge_segments(
        self,
        transcript: TranscriptResult,
        diarization: DiarizationResult,
    ) -> list[MergedUtterance]:
        """전사 세그먼트와 화자 세그먼트를 병합한다.

        각 전사 세그먼트에 대해 시간 겹침이 가장 큰 화자를 할당한다.

        Args:
            transcript: STT 전사 결과
            diarization: 화자분리 결과

        Returns:
            병합된 발화 목록 (시간순 정렬)
        """
        # 화자 세그먼트를 시간순으로 정렬 (이미 정렬되어 있어야 하지만 안전 장치)
        sorted_dia_segments = sorted(
            diarization.segments, key=lambda s: s.start
        )

        # STT 세그먼트를 시간순으로 정렬
        sorted_stt_segments = sorted(
            transcript.segments, key=lambda s: s.start
        )

        utterances: list[MergedUtterance] = []

        for stt_seg in sorted_stt_segments:
            speaker = _find_best_speaker(stt_seg, sorted_dia_segments)

            utterances.append(
                MergedUtterance(
                    text=stt_seg.text,
                    speaker=speaker,
                    start=stt_seg.start,
                    end=stt_seg.end,
                )
            )

        return utterances

    async def merge(
        self,
        transcript: TranscriptResult,
        diarization: DiarizationResult,
    ) -> MergedResult:
        """STT 전사 결과와 화자분리 결과를 병합한다.

        각 전사 세그먼트에 대해 시간 겹침이 가장 큰 화자를 할당한다.
        모델 로드가 필요 없으므로 비교적 빠르게 완료된다.
        병합 작업은 별도 스레드에서 실행하여 이벤트 루프를 블로킹하지 않는다.

        Args:
            transcript: STT 전사 결과
            diarization: 화자분리 결과

        Returns:
            병합 결과 (MergedResult)

        Raises:
            EmptySegmentsError: STT 세그먼트가 비어있을 때
            MergeError: 병합 처리 중 오류 발생 시
        """
        self._validate_inputs(transcript, diarization)
        self._check_time_alignment(transcript, diarization)

        logger.info(
            f"병합 시작: STT 세그먼트={len(transcript.segments)}개, "
            f"화자 세그먼트={len(diarization.segments)}개"
        )

        try:
            # 별도 스레드에서 병합 실행 (큰 데이터에서 이벤트 루프 블로킹 방지)
            utterances = await asyncio.to_thread(
                self._merge_segments, transcript, diarization
            )
        except EmptySegmentsError:
            raise
        except Exception as e:
            raise MergeError(
                f"병합 처리 중 오류 발생: {e}"
            ) from e

        # UNKNOWN 화자 수 집계
        unknown_count = sum(
            1 for u in utterances if u.speaker == UNKNOWN_SPEAKER
        )

        if unknown_count > 0:
            logger.warning(
                f"화자 매칭 실패 발화: {unknown_count}개 / "
                f"전체 {len(utterances)}개 "
                f"({unknown_count / len(utterances) * 100:.1f}%)"
            )

        # 고유 화자 수 (UNKNOWN 제외)
        unique_speakers = set(
            u.speaker for u in utterances
            if u.speaker != UNKNOWN_SPEAKER
        )
        num_speakers = len(unique_speakers)

        result = MergedResult(
            utterances=utterances,
            num_speakers=num_speakers,
            audio_path=transcript.audio_path,
            unknown_count=unknown_count,
        )

        logger.info(
            f"병합 완료: 발화 {len(utterances)}개, "
            f"화자 {num_speakers}명, "
            f"UNKNOWN {unknown_count}개, "
            f"전체 길이: {result.total_duration:.1f}초"
        )

        return result
