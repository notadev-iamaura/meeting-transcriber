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
from difflib import SequenceMatcher
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

_CHANGED_ONLY_SYSTEM_SUFFIX = """

이번 요청은 changed-only 교정 모드입니다. 위 규칙 중 "입력과 동일한 번호와 포맷으로 모두 출력" 규칙은
이번 요청에서만 다음 규칙으로 대체합니다.
1. 명백한 STT 오인식, 띄어쓰기, 조사, 문장부호, 반복어는 적극적으로 자연스럽게 고치세요.
2. 보정이 필요한 줄만 [번호] 보정문 형식으로 출력하세요.
3. 보정이 필요 없는 줄은 출력하지 마세요.
4. 수정할 줄이 없으면 아무 내용도 출력하지 마세요.
5. 문장 순서 변경, 줄 병합/분리, 옆 줄 내용 가져오기, 요약, 의미 추가는 절대 금지입니다.
6. 설명, 요약, 코드블록, "수정 없음" 같은 부가 텍스트를 출력하지 마세요."""

# 발화 번호 파싱 정규식
_LINE_PATTERN = re.compile(r"\[(\d+)\]\s*(.*)")
_GENERIC_SHORT_CORRECTIONS = {
    "그 부분에",
    "있으세요",
    "있습니다",
    "네",
    "네.",
    "예",
    "예.",
    "맞습니다",
    "좋습니다",
    "그렇습니다",
    "감사합니다",
}


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
            json.dump(self.to_dict(), f, ensure_ascii=False)
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


def _normalize_for_similarity(text: str) -> str:
    """유사도 비교용으로 공백과 문장부호를 제거한다."""
    normalized = unicodedata.normalize("NFC", text).casefold()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def _similarity(left: str, right: str) -> float:
    """두 문장의 완화된 문자열 유사도를 반환한다."""
    left_normalized = _normalize_for_similarity(left)
    right_normalized = _normalize_for_similarity(right)
    if not left_normalized and not right_normalized:
        return 1.0
    if not left_normalized or not right_normalized:
        return 0.0
    return SequenceMatcher(None, left_normalized, right_normalized).ratio()


def _is_generic_short_rewrite(candidate: str) -> bool:
    """일반적인 짧은 문구로 긴 원문을 덮은 결과인지 판단한다."""
    stripped = candidate.strip()
    stripped_without_punct = stripped.strip(" .,!?:;~…。！？")
    if stripped in _GENERIC_SHORT_CORRECTIONS:
        return True
    if stripped_without_punct in _GENERIC_SHORT_CORRECTIONS:
        return True
    return any(stripped.startswith(value) for value in _GENERIC_SHORT_CORRECTIONS)


def _find_suspicious_correction_reason(
    batch: list[MergedUtterance],
    *,
    idx: int,
    candidate: str,
) -> str | None:
    """LLM 교정 결과가 원문 계약을 깨는지 보수적으로 판정한다.

    Args:
        batch: 같은 LLM 요청에 들어간 발화 배치
        idx: 1부터 시작하는 발화 번호
        candidate: LLM이 반환한 보정문

    Returns:
        의심 사유. 안전해 보이면 None.
    """
    if not (1 <= idx <= len(batch)):
        return "invalid_index"

    original = batch[idx - 1].text.strip()
    candidate = candidate.strip()
    original_normalized = _normalize_for_similarity(original)
    candidate_normalized = _normalize_for_similarity(candidate)
    original_len = len(original_normalized)
    candidate_len = len(candidate_normalized)

    if not candidate_normalized:
        return "empty_candidate"

    if original_len >= 18 and candidate_len <= 6:
        return "destructive_shortening"
    if original_len >= 18 and candidate_len / max(original_len, 1) < 0.35:
        return "destructive_length_ratio"
    if original_len >= 15 and _is_generic_short_rewrite(candidate):
        return "generic_short_rewrite"
    if original_len >= 8 and candidate_len > original_len * 2.0 and (
        candidate_len - original_len
    ) >= 12:
        return "unexpected_expansion"
    if original_len >= 20 and candidate_len > original_len * 1.6 and (
        candidate_len - original_len
    ) >= 20:
        return "unexpected_expansion"

    own_similarity = _similarity(original, candidate)
    best_other_similarity = 0.0
    best_other_idx = 0
    for other_idx, utterance in enumerate(batch, 1):
        if other_idx == idx:
            continue
        other_normalized = _normalize_for_similarity(utterance.text)
        if len(other_normalized) >= 8:
            longest = SequenceMatcher(
                None,
                candidate_normalized,
                other_normalized,
            ).find_longest_match()
            if longest.size >= max(8, int(len(other_normalized) * 0.75)):
                return f"line_merge_with_{other_idx}"
        other_similarity = _similarity(utterance.text, candidate)
        if other_similarity > best_other_similarity:
            best_other_similarity = other_similarity
            best_other_idx = other_idx

    if best_other_idx and best_other_similarity >= 0.82 and best_other_similarity > (
        own_similarity + 0.2
    ):
        return f"line_shift_to_{best_other_idx}"

    return None


def _with_changed_only_instruction(system_prompt: str) -> str:
    """교정 시스템 프롬프트에 changed-only 출력 규칙을 덧붙인다."""
    return f"{system_prompt.rstrip()}{_CHANGED_ONLY_SYSTEM_SUFFIX}"


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

        # LLM 설정 캐시 — config 값은 "base" 배치 크기로 사용하고,
        # correct() 에서 발화 수에 따라 동적으로 조정한다 (적응형, §2-C).
        self._base_batch_size = self._config.llm.correction_batch_size
        self._batch_size = self._base_batch_size

        logger.info(
            f"Corrector 초기화: backend={self._config.llm.backend}, "
            f"batch_size={self._batch_size} (base)"
        )

    # 발화 수 기반 적응형 배치 크기 임계값.
    # MLXBackend 의 prompt_cache 로 시스템 프롬프트 재사용이 자동화된 이후
    # (§1-A), 큰 배치의 상대 이득이 줄었다. 다만 발화 수가 많으면 여전히
    # 호출 횟수 자체를 줄이는 이득이 있어 중간 배치(10) 로 전환한다.
    _ADAPTIVE_BATCH_THRESHOLD = 20  # 이 발화 수 이상이면 큰 배치 사용
    _ADAPTIVE_LARGE_BATCH = 10  # 큰 배치 크기 (발화 수)

    def _resolve_batch_size(self, total_utterances: int) -> int:
        """발화 수에 따라 적절한 배치 크기를 반환한다.

        - 사용자가 config 에서 명시적으로 batch=5 이외 값을 설정했으면 그 값을
          존중 (적응 비활성화)
        - config=5 (기본 권장값) + 발화 >=20 이면 10 으로 자동 상향
        - 그 외에는 base 사용

        이 로직은 "config 가 기본값(5)일 때만 적응" 이라 사용자 설정이
        우선권을 가진다.
        """
        if self._base_batch_size != 5:
            return self._base_batch_size
        if total_utterances >= self._ADAPTIVE_BATCH_THRESHOLD:
            return self._ADAPTIVE_LARGE_BATCH
        return self._base_batch_size

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

    def _get_llm_config(self) -> Any:
        """현재 인스턴스의 LLM 설정을 반환한다."""
        config = getattr(self, "_config", None)
        return getattr(config, "llm", None)

    def _resolve_correction_mode(self) -> str:
        """설정된 교정 출력 모드를 반환한다."""
        mode = getattr(self._get_llm_config(), "correction_mode", "full")
        if not isinstance(mode, str):
            return "full"
        mode = mode.lower()
        return mode if mode in {"full", "changed_only", "auto"} else "full"

    def _resolve_correction_max_tokens(
        self,
        batch: list[MergedUtterance],
        *,
        mode: str,
    ) -> int | None:
        """교정 배치 크기와 입력 길이에 맞춰 max_tokens를 계산한다."""
        llm_config = self._get_llm_config()
        configured = getattr(llm_config, "correction_max_tokens", None)
        if not isinstance(configured, int):
            return None

        if not self._should_use_adaptive_correction_max_tokens(llm_config):
            return configured

        input_chars = sum(len(utterance.text) for utterance in batch)
        if mode == "changed_only":
            estimated = 96 + len(batch) * 36 + input_chars // 6
        else:
            estimated = 128 + len(batch) * 64 + input_chars // 3
        return max(100, min(configured, estimated))

    def _should_use_adaptive_correction_max_tokens(self, llm_config: Any) -> bool:
        """교정 max_tokens 동적 축소 적용 여부를 반환한다."""
        adaptive = bool(getattr(llm_config, "correction_adaptive_max_tokens", False))
        if not adaptive:
            return False

        fields_set = getattr(llm_config, "model_fields_set", None)
        if not isinstance(fields_set, (set, frozenset)):
            return True

        max_tokens_overridden = "correction_max_tokens" in fields_set
        adaptive_explicit = "correction_adaptive_max_tokens" in fields_set
        return not (max_tokens_overridden and not adaptive_explicit)

    def _chat_for_corrections(
        self,
        backend: LLMBackend,
        batch: list[MergedUtterance],
        system_prompt: str,
        *,
        mode: str,
    ) -> str:
        """지정한 출력 모드로 LLM 교정을 호출한다."""
        prompt = _build_correction_prompt(batch)
        effective_system_prompt = (
            _with_changed_only_instruction(system_prompt)
            if mode == "changed_only"
            else system_prompt
        )
        max_tokens = self._resolve_correction_max_tokens(batch, mode=mode)
        return backend.chat(
            messages=[
                {"role": "system", "content": effective_system_prompt},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
        )

    def _collect_corrections(
        self,
        backend: LLMBackend,
        batch: list[MergedUtterance],
        system_prompt: str,
        *,
        mode: str,
    ) -> tuple[dict[int, str], bool, str, bool]:
        """LLM 응답을 수집하고 파싱한다.

        Returns:
            (번호별 교정문, 배치 호출 실패 여부, 실제 사용 모드, 파싱 실패 여부)
        """
        corrections: dict[int, str] = {}

        for attempt in range(1, self._MAX_BATCH_RETRIES + 2):
            try:
                response_text = self._chat_for_corrections(
                    backend,
                    batch,
                    system_prompt,
                    mode=mode,
                )
                corrections = _parse_correction_response(response_text, len(batch))

                if mode == "full" and len(corrections) < len(batch) // 2:
                    if attempt <= self._MAX_BATCH_RETRIES:
                        logger.warning(
                            f"배치 파싱 결과 부족 ({len(corrections)}/{len(batch)}), "
                            f"재시도 {attempt}/{self._MAX_BATCH_RETRIES}"
                        )
                        continue
                changed_only_parse_failed = bool(
                    mode == "changed_only" and response_text.strip() and not corrections
                )
                if changed_only_parse_failed:
                    if attempt <= self._MAX_BATCH_RETRIES:
                        logger.warning(
                            "changed-only 응답 파싱 실패, 재시도 "
                            f"{attempt}/{self._MAX_BATCH_RETRIES}"
                        )
                        continue
                    return corrections, False, mode, True

                return corrections, False, mode, False

            except LLMConnectionError as conn_err:
                logger.warning(f"배치 보정 연결 실패, 원본 유지: {conn_err}")
                return {}, True, mode, False
            except (LLMGenerationError, CorrectionError) as e:
                if attempt <= self._MAX_BATCH_RETRIES:
                    logger.warning(
                        f"배치 보정 실패 (시도 {attempt}/{self._MAX_BATCH_RETRIES + 1}): {e}"
                    )
                else:
                    logger.warning(f"배치 보정 최종 실패, 원본 유지: {e}")
                    return {}, True, mode, False

        return corrections, False, mode, False

    def _materialize_corrections(
        self,
        batch: list[MergedUtterance],
        corrections: dict[int, str],
        *,
        batch_failed: bool,
        used_mode: str,
    ) -> tuple[list[CorrectedUtterance], int, int, int]:
        """파싱된 LLM 응답을 보정 결과 객체로 변환한다."""
        results: list[CorrectedUtterance] = []
        corrected_count = 0
        failed_count = 0
        rejected_count = 0

        for i, utterance in enumerate(batch):
            idx = i + 1
            corrected_text = corrections.get(idx)

            if corrected_text:
                # NFC 정규화 적용
                corrected_text = unicodedata.normalize("NFC", corrected_text.strip())
                original_normalized = unicodedata.normalize("NFC", utterance.text.strip())
                if corrected_text == original_normalized:
                    was_corrected = False
                else:
                    suspicious_reason = _find_suspicious_correction_reason(
                        batch,
                        idx=idx,
                        candidate=corrected_text,
                    )
                    if suspicious_reason is not None:
                        logger.warning(
                            "의심스러운 LLM 교정 결과 폐기: batch_idx=%d, reason=%s, "
                            "original=%r, candidate=%r",
                            idx,
                            suspicious_reason,
                            utterance.text,
                            corrected_text,
                        )
                        corrected_text = utterance.text
                        was_corrected = False
                        failed_count += 1
                        rejected_count += 1
                    else:
                        was_corrected = True
                        corrected_count += 1
            else:
                # 보정 결과 없음 → 원본 유지
                corrected_text = utterance.text
                was_corrected = False
                # full 모드에서는 번호 누락을 실패로 보지만 changed-only 모드에서는 원문 유지가 정상이다.
                if batch_failed:
                    # 전체 배치가 실패한 경우
                    failed_count += 1
                elif used_mode == "full":
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

        return results, corrected_count, failed_count, rejected_count

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
        requested_mode = self._resolve_correction_mode()
        first_mode = "changed_only" if requested_mode == "auto" else requested_mode
        corrections, batch_failed, used_mode, parse_failed = self._collect_corrections(
            backend,
            batch,
            system_prompt,
            mode=first_mode,
        )

        fallback_enabled = bool(
            getattr(self._get_llm_config(), "correction_changed_only_fallback", True)
        )
        if (
            not batch_failed
            and fallback_enabled
            and first_mode == "changed_only"
            and requested_mode in {"auto", "changed_only"}
            and parse_failed
        ):
            logger.info("changed-only 교정 응답 파싱 실패로 full 모드 재시도")
            corrections, batch_failed, used_mode, _ = self._collect_corrections(
                backend,
                batch,
                system_prompt,
                mode="full",
            )

        results, corrected_count, failed_count, rejected_count = self._materialize_corrections(
            batch,
            corrections,
            batch_failed=batch_failed,
            used_mode=used_mode,
        )

        guard_fallback_threshold = max(2, len(batch) // 4)
        if (
            not batch_failed
            and fallback_enabled
            and used_mode == "changed_only"
            and rejected_count >= guard_fallback_threshold
        ):
            logger.info(
                "changed-only 교정 가드 폐기 %d/%d건으로 full 모드 재시도",
                rejected_count,
                len(batch),
            )
            full_corrections, full_failed, full_mode, _ = self._collect_corrections(
                backend,
                batch,
                system_prompt,
                mode="full",
            )
            (
                full_results,
                full_corrected_count,
                full_failed_count,
                _,
            ) = self._materialize_corrections(
                batch,
                full_corrections,
                batch_failed=full_failed,
                used_mode=full_mode,
            )
            if full_failed_count < failed_count or (
                full_failed_count == failed_count and full_corrected_count > corrected_count
            ):
                logger.info(
                    "full 모드 재시도 결과 채택: corrected %d→%d, failed %d→%d",
                    corrected_count,
                    full_corrected_count,
                    failed_count,
                    full_failed_count,
                )
                return full_results, full_corrected_count, full_failed_count

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

        # 적응형 배치 크기 선택 (발화 수 기반)
        self._batch_size = self._resolve_batch_size(len(merged.utterances))
        if self._batch_size != self._base_batch_size:
            logger.info(
                f"배치 크기 적응 조정: {self._base_batch_size} → {self._batch_size} "
                f"(발화 {len(merged.utterances)} >= 임계값 {self._ADAPTIVE_BATCH_THRESHOLD})"
            )

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

        # 품질 관측성 경고: 보정율이 비정상적으로 낮으면 원인 점검 필요
        # (프롬프트 빌드 실패 / 모델 응답 포맷 이탈 / 배치 파싱 오류 등)
        if len(all_corrected) >= 10 and result.correction_rate < 0.05:
            logger.warning(
                "보정율이 비정상적으로 낮습니다 (%.1f%%). "
                "LLM 응답 포맷, 프롬프트 템플릿, 활성 용어집을 점검하세요.",
                result.correction_rate * 100,
            )
        # 전량 실패 감지
        if len(all_corrected) >= 10 and total_failed >= len(all_corrected) * 0.5:
            logger.warning(
                "보정 실패율이 높습니다 (%d/%d). LLM 백엔드/모델 상태를 점검하세요.",
                total_failed,
                len(all_corrected),
            )

        return result
