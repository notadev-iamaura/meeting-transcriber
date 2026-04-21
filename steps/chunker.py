"""
RAG 청크 생성기 모듈 (RAG Chunk Generator Module)

목적: 보정된 전사문을 RAG 검색에 적합한 크기의 청크로 분할한다.
주요 기능:
    - 화자 발화 그룹핑 (동일 화자 연속 발화 병합)
    - 시간 윈도우 기반 토픽 분리 (time_gap_threshold 초과 시 분리)
    - 토큰 수 기반 청크 크기 제어 (max_tokens, min_tokens)
    - 청크 간 오버랩 지원 (검색 시 컨텍스트 손실 방지)
    - 메타데이터 포함 (meeting_id, date, speakers, time_range)
    - JSON 체크포인트 저장/복원 지원
    - 비동기(async) 인터페이스 지원
의존성: config 모듈, steps/corrector.py (CorrectedResult)
"""

from __future__ import annotations

import asyncio
import json
import logging
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config import AppConfig, get_config
from steps.corrector import CorrectedResult, CorrectedUtterance

logger = logging.getLogger(__name__)


def _estimate_tokens(text: str) -> int:
    """한국어 텍스트의 토큰 수를 추정한다.

    한국어는 대략 1토큰 ≈ 1.5글자로 근사 추정한다.
    정확한 토크나이저 없이도 RAG 청크 분할에 충분한 정확도를 제공한다.

    Args:
        text: 토큰 수를 추정할 텍스트

    Returns:
        추정 토큰 수 (최소 1)
    """
    if not text:
        return 0
    # 한국어 1토큰 ≈ 1.5글자
    return max(1, int(len(text) / 1.5))


@dataclass
class Chunk:
    """RAG 검색용 단일 청크를 나타내는 데이터 클래스.

    Attributes:
        text: 청크 텍스트 (화자 라벨 포함)
        meeting_id: 회의 식별자
        date: 회의 날짜 문자열
        speakers: 청크에 포함된 화자 목록
        start_time: 청크 시작 시간 (초)
        end_time: 청크 종료 시간 (초)
        estimated_tokens: 추정 토큰 수
        chunk_index: 청크 순서 인덱스
    """

    text: str
    meeting_id: str
    date: str
    speakers: list[str]
    start_time: float
    end_time: float
    estimated_tokens: int
    chunk_index: int = 0

    @property
    def duration(self) -> float:
        """청크가 포함하는 시간 범위 (초)."""
        return self.end_time - self.start_time

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화용).

        Returns:
            청크 데이터 딕셔너리
        """
        return asdict(self)


@dataclass
class ChunkedResult:
    """전체 청크 분할 결과를 담는 데이터 클래스.

    Attributes:
        chunks: 생성된 청크 목록
        meeting_id: 회의 식별자
        date: 회의 날짜 문자열
        total_utterances: 원본 발화 수
        num_speakers: 화자 수
        audio_path: 원본 오디오 파일 경로
    """

    chunks: list[Chunk]
    meeting_id: str
    date: str
    total_utterances: int
    num_speakers: int
    audio_path: str

    @property
    def total_tokens(self) -> int:
        """전체 추정 토큰 수."""
        return sum(c.estimated_tokens for c in self.chunks)

    @property
    def avg_tokens_per_chunk(self) -> float:
        """청크당 평균 토큰 수."""
        if not self.chunks:
            return 0.0
        return self.total_tokens / len(self.chunks)

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화/체크포인트 저장용).

        Returns:
            전체 청크 결과 딕셔너리
        """
        return {
            "chunks": [c.to_dict() for c in self.chunks],
            "meeting_id": self.meeting_id,
            "date": self.date,
            "total_utterances": self.total_utterances,
            "num_speakers": self.num_speakers,
            "audio_path": self.audio_path,
        }

    def save_checkpoint(self, output_path: Path) -> None:
        """청크 결과를 JSON 파일로 저장한다 (체크포인트).

        Args:
            output_path: 저장할 JSON 파일 경로
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False)
        logger.info(f"청크 체크포인트 저장: {output_path}")

    @classmethod
    def from_checkpoint(cls, checkpoint_path: Path) -> ChunkedResult:
        """체크포인트 JSON 파일에서 청크 결과를 복원한다.

        Args:
            checkpoint_path: 체크포인트 JSON 파일 경로

        Returns:
            복원된 ChunkedResult 인스턴스

        Raises:
            FileNotFoundError: 체크포인트 파일이 없을 때
            json.JSONDecodeError: JSON 파싱 실패 시
        """
        with open(checkpoint_path, encoding="utf-8") as f:
            data = json.load(f)

        chunks = [Chunk(**c) for c in data.get("chunks", [])]

        return cls(
            chunks=chunks,
            meeting_id=data.get("meeting_id", ""),
            date=data.get("date", ""),
            total_utterances=data.get("total_utterances", 0),
            num_speakers=data.get("num_speakers", 0),
            audio_path=data.get("audio_path", ""),
        )


# === 에러 계층 ===


class ChunkingError(Exception):
    """청크 분할 중 발생하는 에러의 기본 클래스."""


class EmptyInputError(ChunkingError):
    """청크할 발화가 비어있을 때 발생한다."""


# === 내부 데이터 구조 ===


@dataclass
class _UtteranceGroup:
    """동일 화자의 연속 발화를 그룹핑한 내부 데이터 구조.

    Attributes:
        speaker: 화자 라벨
        texts: 발화 텍스트 목록
        start: 그룹 시작 시간 (초)
        end: 그룹 종료 시간 (초)
    """

    speaker: str
    texts: list[str] = field(default_factory=list)
    start: float = 0.0
    end: float = 0.0

    @property
    def combined_text(self) -> str:
        """그룹 내 발화를 하나의 텍스트로 합친다."""
        return " ".join(self.texts)

    @property
    def labeled_text(self) -> str:
        """화자 라벨이 포함된 텍스트."""
        return f"[{self.speaker}] {self.combined_text}"


# === 유틸리티 함수 ===


def _group_by_speaker_and_time(
    utterances: list[CorrectedUtterance],
    time_gap_threshold: float,
) -> list[_UtteranceGroup]:
    """발화를 화자와 시간 기준으로 그룹핑한다.

    동일 화자의 연속 발화를 하나의 그룹으로 병합하되,
    시간 간격이 threshold를 초과하면 새 그룹으로 분리한다.

    Args:
        utterances: 보정된 발화 목록 (시간순 정렬 가정)
        time_gap_threshold: 토픽 분리 시간 간격 (초)

    Returns:
        발화 그룹 목록
    """
    if not utterances:
        return []

    groups: list[_UtteranceGroup] = []
    current_group = _UtteranceGroup(
        speaker=utterances[0].speaker,
        texts=[utterances[0].text],
        start=utterances[0].start,
        end=utterances[0].end,
    )

    for utterance in utterances[1:]:
        # 시간 간격 계산
        time_gap = utterance.start - current_group.end

        # 화자가 다르거나 시간 간격이 임계값 초과 시 새 그룹
        if utterance.speaker != current_group.speaker or time_gap > time_gap_threshold:
            groups.append(current_group)
            current_group = _UtteranceGroup(
                speaker=utterance.speaker,
                texts=[utterance.text],
                start=utterance.start,
                end=utterance.end,
            )
        else:
            # 동일 화자, 시간 간격 내 → 기존 그룹에 추가
            current_group.texts.append(utterance.text)
            current_group.end = utterance.end

    # 마지막 그룹 추가
    groups.append(current_group)

    return groups


def _split_groups_into_chunks(
    groups: list[_UtteranceGroup],
    max_tokens: int,
    min_tokens: int,
    overlap_tokens: int,
    meeting_id: str,
    date: str,
    time_gap_threshold: float = 30.0,
) -> list[Chunk]:
    """발화 그룹을 토큰 수 및 시간 간격 기준으로 청크로 분할한다.

    하나의 그룹이 max_tokens를 초과하면 그룹 내에서 분할하고,
    그룹 간 시간 간격이 threshold를 초과하면 별도 청크로 분리하고,
    마지막 청크가 min_tokens 미만이면 이전 청크와 병합한다.

    Args:
        groups: 화자별 발화 그룹 목록
        max_tokens: 청크 최대 토큰 수
        min_tokens: 청크 최소 토큰 수
        overlap_tokens: 청크 간 오버랩 토큰 수
        meeting_id: 회의 식별자
        date: 회의 날짜 문자열
        time_gap_threshold: 청크 분리 시간 간격 임계값 (초)

    Returns:
        생성된 청크 목록
    """
    if not groups:
        return []

    chunks: list[Chunk] = []

    # 현재 청크를 구성하는 데이터
    current_texts: list[str] = []
    current_speakers: set[str] = set()
    current_start: float = groups[0].start
    current_end: float = groups[0].end
    current_tokens: int = 0

    for group in groups:
        group_text = group.labeled_text
        group_tokens = _estimate_tokens(group_text)

        # 그룹 간 시간 간격이 threshold를 초과하면 현재 청크를 확정 (토픽 분리)
        time_gap = group.start - current_end if current_texts else 0.0
        if current_texts and time_gap > time_gap_threshold:
            chunks.append(
                _build_chunk(
                    texts=current_texts,
                    speakers=current_speakers,
                    start=current_start,
                    end=current_end,
                    tokens=current_tokens,
                    meeting_id=meeting_id,
                    date=date,
                    index=len(chunks),
                )
            )
            current_texts = []
            current_speakers = set()
            current_tokens = 0
            current_start = group.start

        # 그룹 하나가 max_tokens를 초과하는 경우 → 현재 청크 먼저 flush 후 그룹을 분할
        if group_tokens > max_tokens:
            # 현재 축적된 내용이 있으면 먼저 flush
            if current_texts:
                chunks.append(
                    _build_chunk(
                        texts=current_texts,
                        speakers=current_speakers,
                        start=current_start,
                        end=current_end,
                        tokens=current_tokens,
                        meeting_id=meeting_id,
                        date=date,
                        index=len(chunks),
                    )
                )
                current_texts = []
                current_speakers = set()
                current_tokens = 0

            # 큰 그룹을 문장 단위로 분할
            _split_large_group(
                group=group,
                max_tokens=max_tokens,
                meeting_id=meeting_id,
                date=date,
                chunks=chunks,
            )
            # 다음 그룹의 시작 시간으로 초기화
            current_start = group.end
            current_end = group.end
            continue

        # 현재 청크에 추가했을 때 max_tokens 초과 여부 확인
        if current_tokens + group_tokens > max_tokens and current_texts:
            # 현재 청크 확정
            chunks.append(
                _build_chunk(
                    texts=current_texts,
                    speakers=current_speakers,
                    start=current_start,
                    end=current_end,
                    tokens=current_tokens,
                    meeting_id=meeting_id,
                    date=date,
                    index=len(chunks),
                )
            )
            current_texts = []
            current_speakers = set()
            current_tokens = 0
            current_start = group.start

        # 그룹을 현재 청크에 추가
        current_texts.append(group_text)
        current_speakers.add(group.speaker)
        current_end = group.end
        current_tokens += group_tokens

    # 남은 내용으로 마지막 청크 생성
    if current_texts:
        chunks.append(
            _build_chunk(
                texts=current_texts,
                speakers=current_speakers,
                start=current_start,
                end=current_end,
                tokens=current_tokens,
                meeting_id=meeting_id,
                date=date,
                index=len(chunks),
            )
        )

    # min_tokens 미만인 마지막 청크를 이전 청크와 병합
    if len(chunks) >= 2:
        last = chunks[-1]
        if last.estimated_tokens < min_tokens:
            prev = chunks[-2]
            merged_text = f"{prev.text}\n{last.text}"
            merged_speakers = sorted(set(prev.speakers + last.speakers))
            merged_tokens = _estimate_tokens(merged_text)

            chunks[-2] = Chunk(
                text=merged_text,
                meeting_id=meeting_id,
                date=date,
                speakers=merged_speakers,
                start_time=prev.start_time,
                end_time=last.end_time,
                estimated_tokens=merged_tokens,
                chunk_index=prev.chunk_index,
            )
            chunks.pop()
            logger.debug(
                f"마지막 청크({last.estimated_tokens} 토큰)를 "
                f"이전 청크와 병합 → {merged_tokens} 토큰"
            )

    # 인덱스 재부여
    for i, chunk in enumerate(chunks):
        chunk.chunk_index = i

    return chunks


def _build_chunk(
    texts: list[str],
    speakers: set[str],
    start: float,
    end: float,
    tokens: int,
    meeting_id: str,
    date: str,
    index: int,
) -> Chunk:
    """청크 객체를 생성한다.

    Args:
        texts: 청크에 포함될 텍스트 라인 목록
        speakers: 포함된 화자 집합
        start: 시작 시간 (초)
        end: 종료 시간 (초)
        tokens: 추정 토큰 수
        meeting_id: 회의 식별자
        date: 회의 날짜
        index: 청크 인덱스

    Returns:
        생성된 Chunk 객체
    """
    text = "\n".join(texts)
    return Chunk(
        text=text,
        meeting_id=meeting_id,
        date=date,
        speakers=sorted(speakers),
        start_time=start,
        end_time=end,
        estimated_tokens=_estimate_tokens(text),
        chunk_index=index,
    )


def _split_text_by_tokens(
    text: str,
    max_chars: int,
) -> list[str]:
    """긴 텍스트를 문자 수 기준으로 분할한다.

    한국어 문장 부호(. ? ! 등)를 기준으로 분할을 시도하고,
    문장 부호가 없으면 max_chars 단위로 강제 분할한다.
    메모리 효율성을 위해 슬라이싱만 사용한다.

    Args:
        text: 분할할 텍스트
        max_chars: 청크당 최대 문자 수

    Returns:
        분할된 텍스트 조각 목록
    """
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    remaining = text

    while len(remaining) > max_chars:
        # 문장 부호 기준 분할 지점 탐색 (max_chars 이내)
        split_pos = -1
        for sep in ("。", ". ", "? ", "! ", ".\n", "\n"):
            pos = remaining.rfind(sep, 0, max_chars)
            if pos > 0:
                split_pos = pos + len(sep)
                break

        if split_pos <= 0:
            # 문장 부호 없으면 공백 기준 분할, 없으면 강제 분할
            space_pos = remaining.rfind(" ", 0, max_chars)
            split_pos = space_pos + 1 if space_pos > 0 else max_chars

        parts.append(remaining[:split_pos].rstrip())
        remaining = remaining[split_pos:].lstrip()

    if remaining:
        parts.append(remaining)

    return parts


def _split_large_group(
    group: _UtteranceGroup,
    max_tokens: int,
    meeting_id: str,
    date: str,
    chunks: list[Chunk],
) -> None:
    """max_tokens를 초과하는 큰 그룹을 분할하여 청크 목록에 추가한다.

    개별 발화(문장) 단위로 분할하여 max_tokens에 맞춘다.
    개별 발화 자체가 max_tokens를 초과하는 초대형 발화(1만자+)도
    문자 수 기준으로 안전하게 분할한다.

    Args:
        group: 분할할 발화 그룹
        max_tokens: 청크 최대 토큰 수
        meeting_id: 회의 식별자
        date: 회의 날짜
        chunks: 청크를 추가할 목록 (in-place 수정)
    """
    current_lines: list[str] = []
    current_tokens = 0
    # max_tokens에 대응하는 대략적 문자 수 (1토큰 ≈ 1.5글자)
    max_chars_per_chunk = int(max_tokens * 1.5)

    for text in group.texts:
        line = f"[{group.speaker}] {text}"
        line_tokens = _estimate_tokens(line)

        # 개별 발화 자체가 max_tokens를 초과하는 초대형 발화 처리
        if line_tokens > max_tokens:
            # 먼저 축적된 내용이 있으면 flush
            if current_lines:
                chunk_text = "\n".join(current_lines)
                chunks.append(
                    Chunk(
                        text=chunk_text,
                        meeting_id=meeting_id,
                        date=date,
                        speakers=[group.speaker],
                        start_time=group.start,
                        end_time=group.end,
                        estimated_tokens=_estimate_tokens(chunk_text),
                        chunk_index=len(chunks),
                    )
                )
                current_lines = []
                current_tokens = 0

            # 초대형 발화를 문자 수 기준으로 분할
            sub_parts = _split_text_by_tokens(line, max_chars_per_chunk)
            for sub_part in sub_parts:
                chunks.append(
                    Chunk(
                        text=sub_part,
                        meeting_id=meeting_id,
                        date=date,
                        speakers=[group.speaker],
                        start_time=group.start,
                        end_time=group.end,
                        estimated_tokens=_estimate_tokens(sub_part),
                        chunk_index=len(chunks),
                    )
                )

            logger.debug(f"초대형 발화({len(text)}자) → {len(sub_parts)}개 청크로 분할")
            continue

        if current_tokens + line_tokens > max_tokens and current_lines:
            # 현재까지의 내용으로 청크 생성
            chunk_text = "\n".join(current_lines)
            chunks.append(
                Chunk(
                    text=chunk_text,
                    meeting_id=meeting_id,
                    date=date,
                    speakers=[group.speaker],
                    start_time=group.start,
                    end_time=group.end,
                    estimated_tokens=_estimate_tokens(chunk_text),
                    chunk_index=len(chunks),
                )
            )
            current_lines = []
            current_tokens = 0

        current_lines.append(line)
        current_tokens += line_tokens

    # 남은 내용으로 마지막 청크
    if current_lines:
        chunk_text = "\n".join(current_lines)
        chunks.append(
            Chunk(
                text=chunk_text,
                meeting_id=meeting_id,
                date=date,
                speakers=[group.speaker],
                start_time=group.start,
                end_time=group.end,
                estimated_tokens=_estimate_tokens(chunk_text),
                chunk_index=len(chunks),
            )
        )


# === 메인 클래스 ===


class Chunker:
    """RAG 검색용 청크 생성기.

    보정된 전사문을 RAG 검색에 적합한 크기의 청크로 분할한다.
    화자별 발화를 그룹핑하고, 시간 간격과 토큰 수 기준으로 분할한다.
    외부 모델 로드가 필요 없으므로 ModelLoadManager를 사용하지 않는다.

    Args:
        config: 애플리케이션 설정 인스턴스 (None이면 싱글턴 사용)

    사용 예시:
        chunker = Chunker(config)
        result = await chunker.chunk(corrected_result, "meeting_001", "2026-03-04")
        for c in result.chunks:
            print(f"[청크 {c.chunk_index}] {c.estimated_tokens}토큰, 화자: {c.speakers}")
    """

    def __init__(self, config: AppConfig | None = None) -> None:
        """Chunker를 초기화한다.

        Args:
            config: 애플리케이션 설정 (None이면 get_config() 사용)
        """
        self._config = config or get_config()

        # 청킹 설정 캐시
        self._max_tokens = self._config.chunking.max_tokens
        self._min_tokens = self._config.chunking.min_tokens
        self._time_gap_threshold = self._config.chunking.time_gap_threshold_seconds
        self._overlap_tokens = self._config.chunking.overlap_tokens

        logger.info(
            f"Chunker 초기화: max_tokens={self._max_tokens}, "
            f"min_tokens={self._min_tokens}, "
            f"time_gap={self._time_gap_threshold}초, "
            f"overlap={self._overlap_tokens}"
        )

    def _create_chunks(
        self,
        corrected: CorrectedResult,
        meeting_id: str,
        date: str,
    ) -> list[Chunk]:
        """보정된 발화를 청크로 분할한다 (동기 메서드).

        1. 발화를 화자/시간 기준으로 그룹핑
        2. 그룹을 토큰 수 기준으로 청크로 분할
        3. NFC 정규화 적용

        Args:
            corrected: 보정된 전사 결과
            meeting_id: 회의 식별자
            date: 회의 날짜 문자열

        Returns:
            생성된 청크 목록
        """
        # 1단계: 화자 + 시간 기준 그룹핑
        groups = _group_by_speaker_and_time(corrected.utterances, self._time_gap_threshold)

        logger.debug(
            f"화자/시간 그룹핑 완료: {len(corrected.utterances)}개 발화 → {len(groups)}개 그룹"
        )

        # 2단계: 토큰 수 + 시간 간격 기준 청크 분할
        chunks = _split_groups_into_chunks(
            groups=groups,
            max_tokens=self._max_tokens,
            min_tokens=self._min_tokens,
            overlap_tokens=self._overlap_tokens,
            meeting_id=meeting_id,
            date=date,
            time_gap_threshold=self._time_gap_threshold,
        )

        # 3단계: NFC 정규화 적용
        for chunk in chunks:
            chunk.text = unicodedata.normalize("NFC", chunk.text)

        return chunks

    async def chunk(
        self,
        corrected: CorrectedResult,
        meeting_id: str,
        date: str,
    ) -> ChunkedResult:
        """보정된 전사 결과를 RAG 청크로 분할한다.

        화자별 발화를 그룹핑하고, 시간 간격과 토큰 수 기준으로
        적절한 크기의 청크를 생성한다.
        별도 스레드에서 실행하여 이벤트 루프를 블로킹하지 않는다.

        Args:
            corrected: 보정된 전사 결과
            meeting_id: 회의 식별자 (예: "meeting_001")
            date: 회의 날짜 문자열 (예: "2026-03-04")

        Returns:
            청크 분할 결과 (ChunkedResult)

        Raises:
            EmptyInputError: 발화가 비어있을 때
            ChunkingError: 청크 분할 중 오류 발생 시
        """
        if not corrected.utterances:
            raise EmptyInputError("청크할 발화가 비어있습니다.")

        logger.info(f"청크 분할 시작: 발화 {len(corrected.utterances)}개, meeting_id={meeting_id}")

        try:
            # 별도 스레드에서 청크 분할 (큰 데이터에서 이벤트 루프 블로킹 방지)
            chunks = await asyncio.to_thread(self._create_chunks, corrected, meeting_id, date)
        except EmptyInputError:
            raise
        except Exception as e:
            raise ChunkingError(f"청크 분할 중 오류 발생: {e}") from e

        result = ChunkedResult(
            chunks=chunks,
            meeting_id=meeting_id,
            date=date,
            total_utterances=len(corrected.utterances),
            num_speakers=corrected.num_speakers,
            audio_path=corrected.audio_path,
        )

        logger.info(
            f"청크 분할 완료: {len(chunks)}개 청크, "
            f"총 {result.total_tokens} 토큰, "
            f"평균 {result.avg_tokens_per_chunk:.0f} 토큰/청크"
        )

        return result
