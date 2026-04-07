"""
회의록 생성기 모듈 (Meeting Summarizer Module)

목적: LLM 백엔드(Ollama/MLX)를 통해 EXAONE 3.5 LLM으로 보정된 전사문을
      구조화된 마크다운 회의록으로 변환한다.
주요 기능:
    - 전사문 → 마크다운 회의록 자동 생성 (주요 안건, 결정 사항, 액션 아이템)
    - 긴 전사문 자동 분할 요약 (컨텍스트 윈도우 초과 시)
    - ModelLoadManager를 통한 LLM 사용 관리 (뮤텍스)
    - 요약 실패 시 원본 전사문 기반 폴백 회의록 생성 (graceful degradation)
    - JSON 체크포인트 저장/복원 지원
    - 비동기(async) 인터페이스 지원
의존성: config 모듈, core/model_manager 모듈, core/llm_backend 모듈, steps/corrector 모듈
"""

from __future__ import annotations

import asyncio
import json
import logging
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from config import AppConfig, get_config
from core.llm_backend import (
    LLMBackend,
    LLMConnectionError,
    LLMGenerationError,
    create_backend,
)
from core.model_manager import ModelLoadManager, get_model_manager
from core.user_settings import build_summarizer_system_prompt
from steps.corrector import CorrectedResult, CorrectedUtterance

logger = logging.getLogger(__name__)

# 폴백 시스템 프롬프트 (사용자 설정 저장소 로드 실패 시에만 사용)
# 정상 경로에서는 core.user_settings.build_summarizer_system_prompt() 반환값을 사용한다.
_FALLBACK_SUMMARIZER_PROMPT = """당신은 한국어 회의록 작성 전문가입니다.
회의 전사문을 분석하여 구조화된 마크다운 형식의 회의록을 작성합니다.

다음 형식으로 출력하세요:

## 회의 개요
- 참석자: (화자 목록)
- 주요 주제 한 줄 요약

## 주요 안건
1. 안건 제목
   - 세부 내용

## 결정 사항
- 결정된 내용을 항목별로 정리

## 액션 아이템
- [ ] 담당자: 할 일 내용

## 기타 논의
- 위 항목에 포함되지 않는 중요 논의 사항

규칙:
1. 전사문의 내용만 기반으로 작성하세요. 추측하지 마세요.
2. 결정 사항이 없으면 해당 섹션에 "없음"이라고 적으세요.
3. 액션 아이템이 없으면 해당 섹션에 "없음"이라고 적으세요.
4. 화자 이름을 그대로 사용하세요 (SPEAKER_00 등).
5. 간결하고 명확하게 작성하세요.
6. 마크다운 형식을 정확히 지켜주세요."""

# 청크별 부분 요약(중간 단계) 시스템 프롬프트.
# 이 프롬프트는 "긴 회의를 잘게 쪼개 핵심만 추출"하는 내부 구현 상세이며
# 사용자에게 노출되지 않는다. 사용자는 최종 출력 형식만 제어한다 (_load_system_prompt).
# 최종 머지 단계는 사용자의 요약 프롬프트를 사용하므로, 청크 경로와 단일 경로가
# 동일한 출력 형식·스타일로 수렴한다.
_CHUNK_SUMMARY_PROMPT = """당신은 한국어 회의록 작성 전문가입니다.
회의 전사문의 일부를 분석하여 핵심 내용을 요약합니다.

다음 정보를 추출하세요:
- 논의된 주제
- 결정된 사항
- 액션 아이템 (담당자 + 할 일)
- 기타 중요 내용

간결한 불릿 포인트로 작성하세요. 전사문에 없는 내용은 추측하지 마세요."""

# PERF: 토큰 추정 상수 개선 — 한국어/영어 혼합 텍스트 고려
# 한국어: 1토큰 ≈ 1.5글자, ASCII(영어): 1토큰 ≈ 4글자
_CHARS_PER_TOKEN_KOREAN = 1.5
_CHARS_PER_TOKEN_ASCII = 4.0
# 시스템 프롬프트 + 응답 여유분 토큰
_RESERVED_TOKENS = 2000


# === 에러 계층 ===


class SummaryError(Exception):
    """요약 처리 중 발생하는 에러의 기본 클래스."""


class EmptySummaryInputError(SummaryError):
    """요약할 전사문이 비어있을 때 발생한다."""


# === 데이터 클래스 ===


@dataclass
class SummaryResult:
    """회의록 요약 결과를 담는 데이터 클래스.

    Attributes:
        markdown: 마크다운 형식의 회의록
        audio_path: 원본 오디오 파일 경로
        num_speakers: 화자 수
        speakers: 화자 라벨 목록
        num_utterances: 전사문 발화 수
        created_at: 생성 시각 (ISO 형식 문자열)
        was_chunked: 분할 요약이 사용되었는지 여부
        chunk_count: 분할 수 (분할 미사용 시 1)
    """

    markdown: str
    audio_path: str
    num_speakers: int
    speakers: list[str]
    num_utterances: int
    created_at: str = ""
    was_chunked: bool = False
    chunk_count: int = 1

    def __post_init__(self) -> None:
        """생성 시각 자동 설정."""
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화/체크포인트 저장용).

        Returns:
            전체 요약 결과 딕셔너리
        """
        return asdict(self)

    def save_checkpoint(self, output_path: Path) -> None:
        """요약 결과를 JSON 파일로 저장한다 (체크포인트).

        Args:
            output_path: 저장할 JSON 파일 경로
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info(f"요약 체크포인트 저장: {output_path}")

    def save_markdown(self, output_path: Path) -> None:
        """마크다운 회의록을 파일로 저장한다.

        Args:
            output_path: 저장할 마크다운 파일 경로
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(self.markdown)
        logger.info(f"마크다운 회의록 저장: {output_path}")

    @classmethod
    def from_checkpoint(cls, checkpoint_path: Path) -> SummaryResult:
        """체크포인트 JSON 파일에서 요약 결과를 복원한다.

        Args:
            checkpoint_path: 체크포인트 JSON 파일 경로

        Returns:
            복원된 SummaryResult 인스턴스

        Raises:
            FileNotFoundError: 체크포인트 파일이 없을 때
            json.JSONDecodeError: JSON 파싱 실패 시
        """
        with open(checkpoint_path, encoding="utf-8") as f:
            data = json.load(f)

        return cls(
            markdown=data.get("markdown", ""),
            audio_path=data.get("audio_path", ""),
            num_speakers=data.get("num_speakers", 0),
            speakers=data.get("speakers", []),
            num_utterances=data.get("num_utterances", 0),
            created_at=data.get("created_at", ""),
            was_chunked=data.get("was_chunked", False),
            chunk_count=data.get("chunk_count", 1),
        )


# === 유틸리티 함수 ===


def _estimate_tokens(text: str) -> int:
    """텍스트의 토큰 수를 추정한다.

    PERF: 한국어/영어 혼합 비율에 따라 동적으로 추정 정확도를 향상시킨다.
    - 한국어(비ASCII) 글자: 1토큰 ≈ 1.5글자
    - 영어(ASCII) 글자: 1토큰 ≈ 4글자
    회의 전사문은 한국어 중심이지만 영어 고유명사가 섞일 수 있다.

    Args:
        text: 토큰 수를 추정할 텍스트

    Returns:
        추정된 토큰 수
    """
    if not text:
        return 0

    # ASCII와 비ASCII 글자 수를 분리하여 각각의 토큰 비율 적용
    ascii_count = sum(1 for c in text if ord(c) < 128)
    non_ascii_count = len(text) - ascii_count

    tokens = non_ascii_count / _CHARS_PER_TOKEN_KOREAN + ascii_count / _CHARS_PER_TOKEN_ASCII
    return max(1, int(tokens))


def _format_transcript(utterances: list[CorrectedUtterance]) -> str:
    """보정된 발화 목록을 전사문 텍스트로 포맷팅한다.

    각 발화를 [화자] 텍스트 형식으로 변환한다.

    Args:
        utterances: 보정된 발화 목록

    Returns:
        포맷팅된 전사문 텍스트
    """
    lines = []
    for u in utterances:
        lines.append(f"[{u.speaker}] {u.text}")
    return "\n".join(lines)


def _build_fallback_markdown(
    utterances: list[CorrectedUtterance],
    speakers: list[str],
) -> str:
    """요약 실패 시 원본 전사문 기반 폴백 회의록을 생성한다.

    LLM 호출이 실패했을 때 최소한의 회의록 형태를 제공한다.

    Args:
        utterances: 보정된 발화 목록
        speakers: 화자 라벨 목록

    Returns:
        폴백 마크다운 회의록
    """
    lines = [
        "## 회의 개요",
        f"- 참석자: {', '.join(speakers)}",
        "- (AI 요약 실패 — 원본 전사문을 첨부합니다)",
        "",
        "## 전사문",
    ]
    for u in utterances:
        lines.append(f"- **[{u.speaker}]** {u.text}")
    return "\n".join(lines)


def _split_utterances(
    utterances: list[CorrectedUtterance],
    max_tokens: int,
) -> list[list[CorrectedUtterance]]:
    """발화 목록을 토큰 제한에 맞게 분할한다.

    각 청크가 max_tokens를 초과하지 않도록 발화를 분할한다.
    하나의 발화가 max_tokens를 초과하면 해당 발화만으로 하나의 청크를 구성한다.

    Args:
        utterances: 분할할 발화 목록
        max_tokens: 청크당 최대 토큰 수

    Returns:
        분할된 발화 청크 목록
    """
    chunks: list[list[CorrectedUtterance]] = []
    current_chunk: list[CorrectedUtterance] = []
    current_tokens = 0

    for u in utterances:
        # "[SPEAKER] text\n" 형태의 토큰 추정
        line = f"[{u.speaker}] {u.text}\n"
        line_tokens = _estimate_tokens(line)

        # 현재 청크에 추가 시 초과하면 새 청크 시작
        if current_chunk and current_tokens + line_tokens > max_tokens:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0

        current_chunk.append(u)
        current_tokens += line_tokens

    # 마지막 청크 추가
    if current_chunk:
        chunks.append(current_chunk)

    return chunks


# === 메인 클래스 ===


class Summarizer:
    """EXAONE 3.5 기반 회의록 생성기.

    Ollama API를 통해 EXAONE LLM으로 보정된 전사문을
    구조화된 마크다운 회의록으로 변환한다.
    ModelLoadManager를 통해 다른 모델과의 동시 로드를 방지한다.
    전사문이 컨텍스트 윈도우를 초과하면 자동으로 분할 요약을 수행한다.

    Args:
        config: 애플리케이션 설정 인스턴스 (None이면 싱글턴 사용)
        model_manager: 모델 로드 매니저 (None이면 싱글턴 사용)

    사용 예시:
        summarizer = Summarizer(config, model_manager)
        result = await summarizer.summarize(corrected_result)
        print(result.markdown)
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        model_manager: ModelLoadManager | None = None,
    ) -> None:
        """Summarizer를 초기화한다.

        Args:
            config: 애플리케이션 설정 (None이면 get_config() 사용)
            model_manager: 모델 매니저 (None이면 get_model_manager() 사용)
        """
        self._config = config or get_config()
        self._manager = model_manager or get_model_manager()

        # LLM 설정 캐시
        self._max_context = self._config.llm.max_context_tokens

        # 입력 토큰 한도 (컨텍스트 윈도우 - 예약 토큰)
        self._max_input_tokens = self._max_context - _RESERVED_TOKENS

        logger.info(
            f"Summarizer 초기화: backend={self._config.llm.backend}, "
            f"max_input_tokens={self._max_input_tokens}"
        )

    def _create_backend(self) -> LLMBackend:
        """LLM 백엔드를 생성하여 반환한다.

        ModelLoadManager의 loader 함수로 사용된다.
        config.llm.backend에 따라 Ollama 또는 MLX 백엔드를 선택한다.

        Returns:
            LLMBackend 인스턴스

        Raises:
            LLMConnectionError: 백엔드 연결 실패 시
        """
        return create_backend(self._config.llm)

    def _call_llm(
        self,
        backend: LLMBackend,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """LLM 백엔드를 호출하여 응답을 반환한다.

        Args:
            backend: LLM 백엔드 인스턴스
            system_prompt: 시스템 프롬프트
            user_prompt: 사용자 프롬프트 (전사문 텍스트)

        Returns:
            LLM 응답 텍스트 (NFC 정규화 적용)

        Raises:
            LLMConnectionError: 연결 실패 시
            LLMGenerationError: 타임아웃 시
            SummaryError: 기타 API 오류 시
        """
        try:
            content = backend.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except LLMGenerationError as e:
            raise SummaryError(str(e)) from e

        return unicodedata.normalize("NFC", content.strip())

    def _load_system_prompt(self) -> str:
        """사용자가 편집한 요약 시스템 프롬프트를 로드한다.

        저장소 로드 실패 시 폴백 상수로 graceful degradation.

        Returns:
            시스템 프롬프트 문자열
        """
        try:
            return build_summarizer_system_prompt()
        except Exception as e:
            logger.warning(f"요약 프롬프트 로드 실패, 폴백 사용: {e}")
            return _FALLBACK_SUMMARIZER_PROMPT

    def _summarize_single(
        self,
        backend: LLMBackend,
        transcript: str,
        speakers: list[str],
    ) -> str:
        """단일 호출로 전사문을 요약한다.

        전사문이 컨텍스트 윈도우에 들어가는 경우 사용한다.

        Args:
            backend: LLM 백엔드 인스턴스
            transcript: 포맷팅된 전사문 텍스트
            speakers: 화자 라벨 목록

        Returns:
            마크다운 회의록 텍스트
        """
        user_prompt = f"참석자: {', '.join(speakers)}\n\n=== 전사문 ===\n{transcript}"
        return self._call_llm(backend, self._load_system_prompt(), user_prompt)

    def _summarize_chunked(
        self,
        backend: LLMBackend,
        chunks: list[list[CorrectedUtterance]],
        speakers: list[str],
    ) -> str:
        """분할 요약 후 통합하여 회의록을 생성한다.

        전사문이 컨텍스트 윈도우를 초과하는 경우 사용한다.
        1단계: 각 청크별 부분 요약 생성
        2단계: 부분 요약들을 통합하여 최종 회의록 생성

        Args:
            backend: LLM 백엔드 인스턴스
            chunks: 분할된 발화 청크 목록
            speakers: 화자 라벨 목록

        Returns:
            통합된 마크다운 회의록 텍스트
        """
        # 1단계: 각 청크별 부분 요약
        partial_summaries: list[str] = []
        for i, chunk in enumerate(chunks):
            logger.info(f"청크 {i + 1}/{len(chunks)} 부분 요약 중 ({len(chunk)}개 발화)")
            transcript = _format_transcript(chunk)
            user_prompt = (
                f"파트 {i + 1}/{len(chunks)}\n"
                f"참석자: {', '.join(speakers)}\n\n"
                f"=== 전사문 ===\n{transcript}"
            )

            try:
                partial = self._call_llm(backend, _CHUNK_SUMMARY_PROMPT, user_prompt)
                partial_summaries.append(f"### 파트 {i + 1}\n{partial}")
            except (LLMConnectionError, LLMGenerationError):
                raise
            except SummaryError as e:
                logger.warning(f"청크 {i + 1} 요약 실패: {e}")
                # 실패한 청크는 원본 텍스트로 대체
                partial_summaries.append(f"### 파트 {i + 1}\n(요약 실패 — 원본)\n{transcript}")

        # 모든 청크가 실패했는지 확인
        all_failed = all("(요약 실패 — 원본)" in s for s in partial_summaries)
        if all_failed:
            logger.warning("모든 청크 요약 실패 — 통합 단계 건너뛰고 폴백 반환")
            # 전체 발화를 평탄화하여 폴백 회의록 생성
            all_utterances: list[CorrectedUtterance] = []
            for chunk in chunks:
                all_utterances.extend(chunk)
            return _build_fallback_markdown(all_utterances, speakers)

        # 2단계: 부분 요약 통합
        # 사용자가 편집한 요약 프롬프트(_load_system_prompt)를 여기서도 사용하여
        # 단일 요약 경로와 동일한 출력 형식·스타일을 유지한다.
        # user_prompt에 "파트별 요약"이라는 명시적 컨텍스트를 제공해 LLM이
        # 입력이 이미 요약된 것임을 인지하도록 한다.
        logger.info("부분 요약 통합 중")
        merged_summaries = "\n\n".join(partial_summaries)
        user_prompt = (
            f"아래는 회의 전사문을 {len(chunks)}개 파트로 나누어 각각 요약한 결과입니다.\n"
            "중복되는 내용은 하나로 통합하고, 지시된 형식에 맞춰 최종 회의록을 작성하세요.\n\n"
            f"참석자: {', '.join(speakers)}\n\n"
            f"=== 파트별 요약 ===\n{merged_summaries}"
        )

        try:
            return self._call_llm(backend, self._load_system_prompt(), user_prompt)
        except (SummaryError, LLMGenerationError) as e:
            # 통합 단계 실패 시 부분 요약들을 이어붙여 폴백 반환
            logger.warning(f"부분 요약 통합 실패, 파트별 요약 병합으로 대체: {e}")
            fallback_lines = [
                "## 회의 개요",
                f"- 참석자: {', '.join(speakers)}",
                "- (AI 통합 요약 실패 — 파트별 요약을 첨부합니다)",
                "",
            ]
            fallback_lines.append(merged_summaries)
            return "\n".join(fallback_lines)

    async def summarize(
        self,
        corrected: CorrectedResult,
    ) -> SummaryResult:
        """보정된 전사 결과를 회의록으로 요약한다.

        전사문 길이에 따라 단일 요약 또는 분할 요약을 자동으로 선택한다.
        ModelLoadManager를 통해 다른 모델과 동시 사용을 방지한다.
        요약 실패 시 원본 전사문 기반 폴백 회의록을 생성한다.

        Args:
            corrected: 보정된 전사 결과

        Returns:
            요약 결과 (SummaryResult)

        Raises:
            EmptySummaryInputError: 요약할 발화가 없을 때
            LLMConnectionError: LLM 백엔드 연결 실패 시
            LLMGenerationError: LLM 생성 실패/타임아웃 시
        """
        if not corrected.utterances:
            raise EmptySummaryInputError("요약할 발화가 비어있습니다.")

        speakers = corrected.speakers
        num_utterances = len(corrected.utterances)

        logger.info(f"요약 시작: 발화 {num_utterances}개, 화자 {len(speakers)}명")

        # 전사문 포맷팅 및 토큰 추정
        full_transcript = _format_transcript(corrected.utterances)
        estimated_tokens = _estimate_tokens(full_transcript)

        logger.info(f"전사문 추정 토큰 수: {estimated_tokens}")

        # 분할 여부 결정
        needs_chunking = estimated_tokens > self._max_input_tokens
        was_chunked = False
        chunk_count = 1

        try:
            async with self._manager.acquire("exaone", self._create_backend) as backend:
                if needs_chunking:
                    # 분할 요약
                    chunks = _split_utterances(corrected.utterances, self._max_input_tokens)
                    chunk_count = len(chunks)
                    was_chunked = True

                    logger.info(f"분할 요약 모드: {chunk_count}개 청크로 분할")

                    markdown = await asyncio.to_thread(
                        self._summarize_chunked,
                        backend,
                        chunks,
                        speakers,
                    )
                else:
                    # 단일 요약
                    logger.info("단일 요약 모드")

                    markdown = await asyncio.to_thread(
                        self._summarize_single,
                        backend,
                        full_transcript,
                        speakers,
                    )

        except (LLMConnectionError, LLMGenerationError):
            raise
        except SummaryError as e:
            # 요약 실패 시 폴백 회의록 생성
            logger.warning(f"요약 실패, 폴백 회의록 생성: {e}")
            markdown = _build_fallback_markdown(corrected.utterances, speakers)
        except Exception as e:
            # 예상치 못한 오류 시에도 폴백 시도
            logger.error(f"요약 중 예상치 못한 오류, 폴백 회의록 생성: {e}")
            markdown = _build_fallback_markdown(corrected.utterances, speakers)

        result = SummaryResult(
            markdown=markdown,
            audio_path=corrected.audio_path,
            num_speakers=corrected.num_speakers,
            speakers=speakers,
            num_utterances=num_utterances,
            was_chunked=was_chunked,
            chunk_count=chunk_count,
        )

        logger.info(
            f"요약 완료: 화자 {len(speakers)}명, "
            f"발화 {num_utterances}개, "
            f"분할={was_chunked} (청크 {chunk_count}개)"
        )

        return result
