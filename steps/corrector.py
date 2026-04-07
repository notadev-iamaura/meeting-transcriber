"""
EXAONE 전사문 보정기 모듈 (Transcript Corrector Module)

목적: LLM 백엔드(Ollama/MLX)를 통해 EXAONE 3.5 LLM으로 STT 전사문의 오타/문법을 보정한다.
주요 기능:
    - 배치 처리 (발화 N개씩 묶어서 보정, 기본 10개)
    - ModelLoadManager를 통한 LLM 사용 관리 (뮤텍스)
    - 의미 변경 없이 오타/문법만 수정
    - 보정 실패 시 원본 텍스트 유지 (graceful degradation)
    - JSON 체크포인트 저장/복원 지원
    - 비동기(async) 인터페이스 지원
의존성: config 모듈, core/model_manager 모듈, core/llm_backend 모듈
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass
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
from core.user_settings import build_corrector_snapshot
from steps.merger import MergedResult, MergedUtterance

logger = logging.getLogger(__name__)

# 폴백 시스템 프롬프트 (사용자 설정 저장소 로드 실패 시에만 사용)
# 정상 경로에서는 core.user_settings.build_corrector_snapshot()이 반환하는
# 최종 프롬프트(사용자 편집본 + 용어집 주입)를 사용한다.
_FALLBACK_SYSTEM_PROMPT = """당신은 한국어 회의 전사문 보정 전문가입니다.
음성인식(STT) 결과에서 발생하는 오타, 문법 오류, 단어 오인식을 보정합니다.

규칙:
1. 의미를 절대 변경하지 마세요. 오타와 문법만 수정합니다.
2. 고유명사가 잘못 인식된 경우 문맥에 맞게 수정합니다.
3. 조사(은/는, 이/가, 을/를 등)가 누락되거나 잘못된 경우 수정합니다.
4. 원래 의도된 문장과 다른 단어로 인식된 경우 문맥 기반으로 수정합니다.
5. 보정이 필요 없는 문장은 그대로 유지합니다.
6. 반드시 입력과 동일한 번호와 포맷([번호] 텍스트)으로 출력하세요.
7. 설명이나 부가 텍스트 없이 보정 결과만 출력하세요.
8. 아라비아 숫자(예: 30, 250, 2026)는 절대 한글 숫자로 변환하지 마세요. 숫자 표기를 그대로 유지하세요."""

# 발화 번호 파싱 정규식
_LINE_PATTERN = re.compile(r"\[(\d+)\]\s*(.*)")


@dataclass
class CorrectedUtterance:
    """보정된 단일 발화를 나타내는 데이터 클래스.

    Attributes:
        text: 보정된 텍스트
        original_text: 원본 텍스트 (보정 전)
        speaker: 화자 라벨
        start: 발화 시작 시간 (초)
        end: 발화 종료 시간 (초)
        was_corrected: 보정이 적용되었는지 여부
    """

    text: str
    original_text: str
    speaker: str
    start: float
    end: float
    was_corrected: bool = False

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
class CorrectedResult:
    """전체 보정 결과를 담는 데이터 클래스.

    Attributes:
        utterances: 보정된 발화 목록
        num_speakers: 화자 수
        audio_path: 원본 오디오 파일 경로
        total_corrected: 보정된 발화 수
        total_failed: 보정 실패(원본 유지) 발화 수
    """

    utterances: list[CorrectedUtterance]
    num_speakers: int
    audio_path: str
    total_corrected: int = 0
    total_failed: int = 0

    @property
    def total_duration(self) -> float:
        """전체 오디오 길이 추정치."""
        if not self.utterances:
            return 0.0
        return max(u.end for u in self.utterances)

    @property
    def speakers(self) -> list[str]:
        """감지된 화자 라벨 목록 (중복 제거, 정렬)."""
        return sorted(set(u.speaker for u in self.utterances))

    @property
    def correction_rate(self) -> float:
        """보정 비율 (0.0 ~ 1.0)."""
        if not self.utterances:
            return 0.0
        return self.total_corrected / len(self.utterances)

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화/체크포인트 저장용).

        Returns:
            전체 보정 결과 딕셔너리
        """
        return {
            "utterances": [u.to_dict() for u in self.utterances],
            "num_speakers": self.num_speakers,
            "audio_path": self.audio_path,
            "total_corrected": self.total_corrected,
            "total_failed": self.total_failed,
        }

    def save_checkpoint(self, output_path: Path) -> None:
        """보정 결과를 JSON 파일로 저장한다 (체크포인트).

        Args:
            output_path: 저장할 JSON 파일 경로
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info(f"보정 체크포인트 저장: {output_path}")

    @classmethod
    def from_checkpoint(cls, checkpoint_path: Path) -> CorrectedResult:
        """체크포인트 JSON 파일에서 보정 결과를 복원한다.

        Args:
            checkpoint_path: 체크포인트 JSON 파일 경로

        Returns:
            복원된 CorrectedResult 인스턴스

        Raises:
            FileNotFoundError: 체크포인트 파일이 없을 때
            json.JSONDecodeError: JSON 파싱 실패 시
        """
        with open(checkpoint_path, encoding="utf-8") as f:
            data = json.load(f)

        utterances = [CorrectedUtterance(**u) for u in data.get("utterances", [])]

        return cls(
            utterances=utterances,
            num_speakers=data.get("num_speakers", 0),
            audio_path=data.get("audio_path", ""),
            total_corrected=data.get("total_corrected", 0),
            total_failed=data.get("total_failed", 0),
        )


# === 에러 계층 ===


class CorrectionError(Exception):
    """보정 처리 중 발생하는 에러의 기본 클래스."""


class EmptyInputError(CorrectionError):
    """보정할 발화가 비어있을 때 발생한다."""


# === 유틸리티 함수 ===


def _build_correction_prompt(utterances: list[MergedUtterance]) -> str:
    """보정할 발화 목록을 프롬프트 텍스트로 변환한다.

    각 발화를 [번호] 형식으로 번호를 매겨 LLM이 입출력을 매핑하도록 한다.

    Args:
        utterances: 보정할 발화 목록

    Returns:
        번호가 매겨진 발화 텍스트
    """
    lines: list[str] = []
    for i, u in enumerate(utterances, 1):
        lines.append(f"[{i}] {u.text}")
    return "\n".join(lines)


def _parse_correction_response(
    response_text: str,
    batch_size: int,
) -> dict[int, str]:
    """LLM 응답에서 보정된 텍스트를 파싱한다.

    [번호] 텍스트 형식의 응답을 파싱하여 번호-텍스트 매핑을 반환한다.

    Args:
        response_text: LLM의 응답 텍스트
        batch_size: 원본 배치 크기 (유효 범위 검증용)

    Returns:
        {번호: 보정된 텍스트} 딕셔너리
    """
    corrections: dict[int, str] = {}

    for line in response_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        match = _LINE_PATTERN.match(line)
        if match:
            idx = int(match.group(1))
            text = match.group(2).strip()
            # 유효한 범위인지 확인
            if 1 <= idx <= batch_size and text:
                corrections[idx] = text

    return corrections


# === 메인 클래스 ===


class Corrector:
    """EXAONE 3.5 기반 전사문 보정기.

    Ollama API를 통해 EXAONE LLM으로 STT 결과의 오타/문법을 보정한다.
    ModelLoadManager를 통해 다른 모델과의 동시 로드를 방지한다.
    보정 실패 시 원본 텍스트를 유지하여 데이터 손실을 방지한다.

    Args:
        config: 애플리케이션 설정 인스턴스 (None이면 싱글턴 사용)
        model_manager: 모델 로드 매니저 (None이면 싱글턴 사용)

    사용 예시:
        corrector = Corrector(config, model_manager)
        result = await corrector.correct(merged_result)
        for u in result.utterances:
            print(f"[{u.speaker}] {u.text}")
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        model_manager: ModelLoadManager | None = None,
    ) -> None:
        """Corrector를 초기화한다.

        Args:
            config: 애플리케이션 설정 (None이면 get_config() 사용)
            model_manager: 모델 매니저 (None이면 get_model_manager() 사용)
        """
        self._config = config or get_config()
        self._manager = model_manager or get_model_manager()

        # LLM 설정 캐시
        self._batch_size = self._config.llm.correction_batch_size

        logger.info(
            f"Corrector 초기화: backend={self._config.llm.backend}, batch_size={self._batch_size}"
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

    # 배치 보정 최대 재시도 횟수
    _MAX_BATCH_RETRIES: int = 2

    def _correct_batch(
        self,
        backend: LLMBackend,
        batch: list[MergedUtterance],
        system_prompt: str,
    ) -> tuple[list[CorrectedUtterance], int, int]:
        """발화 배치를 보정한다.

        배치 내 발화들을 하나의 프롬프트로 묶어서 LLM에 보정을 요청한다.
        LLM 호출 실패 시 최대 _MAX_BATCH_RETRIES회 재시도한다.
        보정 실패 시 원본 텍스트를 유지한다.

        Args:
            backend: LLM 백엔드 인스턴스
            batch: 보정할 발화 배치
            system_prompt: 이 회의에 사용할 시스템 프롬프트 (잡 단위 스냅샷)

        Returns:
            (보정된 발화 목록, 보정된 수, 실패한 수) 튜플
        """
        prompt = _build_correction_prompt(batch)
        corrections: dict[int, str] = {}
        _last_error: Exception | None = None

        # 재시도 로직: LLMGenerationError 발생 시 재시도
        for attempt in range(1, self._MAX_BATCH_RETRIES + 2):
            try:
                response_text = backend.chat(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                )
                corrections = _parse_correction_response(response_text, len(batch))

                # 파싱 결과가 원본 배치의 절반 미만이면 파싱 실패로 간주하고 재시도
                if len(corrections) < len(batch) // 2 and attempt <= self._MAX_BATCH_RETRIES:
                    logger.warning(
                        f"배치 파싱 결과 부족 ({len(corrections)}/{len(batch)}), "
                        f"재시도 {attempt}/{self._MAX_BATCH_RETRIES}"
                    )
                    continue

                # 성공 — 루프 탈출
                break

            except LLMConnectionError as conn_err:
                # 연결 에러는 재시도 의미 없음 (서버 자체 불가)
                logger.warning(f"배치 보정 연결 실패, 원본 유지: {conn_err}")
                corrections = {}
                break
            except (LLMGenerationError, CorrectionError) as e:
                _last_error = e
                if attempt <= self._MAX_BATCH_RETRIES:
                    logger.warning(
                        f"배치 보정 실패 (시도 {attempt}/{self._MAX_BATCH_RETRIES + 1}): {e}"
                    )
                else:
                    logger.warning(f"배치 보정 최종 실패, 원본 유지: {e}")
                    corrections = {}

        results: list[CorrectedUtterance] = []
        corrected_count = 0
        failed_count = 0

        for i, utterance in enumerate(batch):
            idx = i + 1
            corrected_text = corrections.get(idx)

            if corrected_text:
                # NFC 정규화 적용
                corrected_text = unicodedata.normalize("NFC", corrected_text.strip())
                original_normalized = unicodedata.normalize("NFC", utterance.text.strip())
                was_corrected = corrected_text != original_normalized

                if was_corrected:
                    corrected_count += 1
            else:
                # 보정 결과 없음 → 원본 유지
                corrected_text = utterance.text
                was_corrected = False
                # LLM에서 번호 매핑이 누락된 경우 실패로 카운트
                if not corrections:
                    # 전체 배치가 실패한 경우
                    failed_count += 1
                else:
                    # 개별 발화가 파싱에서 누락된 경우
                    failed_count += 1

            results.append(
                CorrectedUtterance(
                    text=corrected_text,
                    original_text=utterance.text,
                    speaker=utterance.speaker,
                    start=utterance.start,
                    end=utterance.end,
                    was_corrected=was_corrected,
                )
            )

        return results, corrected_count, failed_count

    async def correct(
        self,
        merged: MergedResult,
    ) -> CorrectedResult:
        """병합된 전사 결과를 보정한다.

        발화를 배치 크기(기본 10개)씩 묶어서 EXAONE LLM으로 보정한다.
        ModelLoadManager를 통해 다른 모델과 동시 사용을 방지한다.

        Args:
            merged: 병합된 전사 결과

        Returns:
            보정된 결과 (CorrectedResult)

        Raises:
            EmptyInputError: 보정할 발화가 없을 때
            LLMConnectionError: LLM 백엔드 연결 실패 시
            CorrectionError: 보정 처리 중 오류 발생 시
        """
        if not merged.utterances:
            raise EmptyInputError("보정할 발화가 비어있습니다.")

        logger.info(f"보정 시작: 발화 {len(merged.utterances)}개, 배치 크기 {self._batch_size}")

        # 잡 단위 프롬프트 스냅샷: 회의 처리 시작 시점에 1회 빌드하여
        # 처리 도중 사용자가 설정을 수정해도 진행 중인 회의는 일관성을 유지한다.
        # 저장소 로드 실패 시 폴백 상수 사용 (graceful degradation).
        try:
            snapshot = build_corrector_snapshot()
            system_prompt = snapshot.system_prompt
            logger.info(
                f"보정 스냅샷 빌드 완료: 프롬프트 {len(system_prompt)}자, "
                f"활성 용어 {snapshot.vocab_term_count}개"
            )
        except Exception as e:
            logger.warning(f"프롬프트 스냅샷 빌드 실패, 폴백 사용: {e}")
            system_prompt = _FALLBACK_SYSTEM_PROMPT

        # 배치 분할
        batches: list[list[MergedUtterance]] = []
        for i in range(0, len(merged.utterances), self._batch_size):
            batches.append(merged.utterances[i : i + self._batch_size])

        logger.info(f"배치 수: {len(batches)}")

        all_corrected: list[CorrectedUtterance] = []
        total_corrected = 0
        total_failed = 0

        try:
            # PERF-001: 다음 단계(summarizer)에서도 exaone을 사용하므로
            # keep_loaded=True로 모델을 유지하여 불필요한 해제/재로드 방지
            async with self._manager.acquire(
                "exaone", self._create_backend, keep_loaded=True
            ) as backend:
                for batch_idx, batch in enumerate(batches):
                    logger.info(
                        f"배치 {batch_idx + 1}/{len(batches)} 보정 중 ({len(batch)}개 발화)"
                    )

                    # 별도 스레드에서 실행 (동기 호출이 블로킹이므로)
                    corrected, batch_corrected, batch_failed = await asyncio.to_thread(
                        self._correct_batch, backend, batch, system_prompt
                    )

                    total_corrected += batch_corrected
                    total_failed += batch_failed
                    all_corrected.extend(corrected)

        except (LLMConnectionError, LLMGenerationError):
            raise
        except CorrectionError:
            raise
        except Exception as e:
            raise CorrectionError(f"보정 처리 중 오류 발생: {e}") from e

        result = CorrectedResult(
            utterances=all_corrected,
            num_speakers=merged.num_speakers,
            audio_path=merged.audio_path,
            total_corrected=total_corrected,
            total_failed=total_failed,
        )

        logger.info(
            f"보정 완료: 전체 {len(all_corrected)}개, "
            f"보정됨 {total_corrected}개, "
            f"실패 {total_failed}개, "
            f"보정율 {result.correction_rate:.1%}"
        )

        return result
