"""
A/B 테스트 러너 모듈.

목적: 동일한 회의에 대해 LLM 또는 STT 모델 2종을 순차 실행하고 결과를
`ab_tests/{test_id}/` 에 격리 저장한다. 본 파이프라인(`core/pipeline.py`)의
부수효과(큐/DB/임베딩/검색 인덱싱)를 완전히 우회하며, 기존 step 모듈
(Corrector, Summarizer, Transcriber, Merger, Diarizer) 만 직접 호출한다.

주요 결정 (`docs/plans/2026-04-09-ab-test-feature.md` 참조):
    - ADR-6: 별도 러너, 본 파이프라인 수정 없음
    - ADR-7: 독립 실행, 프로세스 내 `_ab_test_lock` 으로 동시 1개 제한
    - ADR-9: `model_copy(update=...)` 로 temp config 생성, 원본 비오염
    - ADR-8: STT A/B 는 diarize 체크포인트 재사용, 없으면 opt-in 재실행

Phase 1 범위 제한:
    - API/WebSocket 미포함. `ws_broadcaster` 콜러블을 선택적으로 주입받되,
      None 이면 no-op. 러너는 WebSocket 모듈을 직접 import 하지 않는다.

의존성: config, core/ab_test_store, core/model_manager, steps/*
"""

from __future__ import annotations

import asyncio
import errno
import gc
import json
import logging
import math
import os
import re
import stat
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

from config import AppConfig
from core import ab_test_store
from core.audio_quality import (
    AudioFailureKind,
    AudioQualityStatus,
    validate_audio_quality,
)
from core.model_manager import ModelLoadManager, get_model_manager
from steps.corrector import CorrectedResult, Corrector
from steps.diarizer import DiarizationResult, DiarizationSegment, Diarizer
from steps.merger import MergedResult, MergedUtterance, Merger
from steps.summarizer import Summarizer, SummaryResult
from steps.transcriber import (
    AudioAdmissionError,
    AudioFileIdentity,
    EmptyAudioError,
    Transcriber,
    inspect_audio_path_no_symlinks,
)

logger = logging.getLogger(__name__)


# ============================================================
# 데이터 클래스
# ============================================================


@dataclass(frozen=True)
class ModelSpec:
    """A/B 테스트에서 비교할 단일 variant 의 모델 스펙.

    Attributes:
        label: 사용자에게 보여지는 라벨 (예: "EXAONE 3.5 7.8B 4bit")
        model_id: 모델 식별자. LLM 은 HF repo id, STT 는 registry id 또는 HF repo id
        backend: "mlx", "ollama" 또는 명시적 외부 STT인 "openai"
    """

    label: str
    model_id: str
    backend: str = "mlx"


@dataclass(frozen=True)
class LlmScope:
    """LLM A/B 테스트 실행 범위."""

    correct: bool = True
    summarize: bool = True


# ============================================================
# 모듈 상태 (동시성 제어)
# ============================================================


# 한 번에 하나의 A/B 테스트만 실행되도록 직렬화 (ADR-7)
# lazy 초기화: 모듈 임포트 시점에 이벤트 루프가 없어도 안전하도록 함수로 래핑.
# 여러 테스트가 `asyncio.run()` 을 번갈아 호출해 루프가 바뀌는 상황에서도 Lock 을
# 재생성하여 "Event loop is closed" / "different loop" 오류를 회피한다.
_ab_test_lock: asyncio.Lock | None = None
_ab_test_lock_loop: Any = None


def _get_ab_test_lock() -> asyncio.Lock:
    """현재 이벤트 루프에 바인딩된 `_ab_test_lock` 을 반환한다.

    루프가 바뀌었거나 Lock 이 아직 없으면 새로 생성한다.
    """
    global _ab_test_lock, _ab_test_lock_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _ab_test_lock is None or _ab_test_lock_loop is not loop:
        _ab_test_lock = asyncio.Lock()
        _ab_test_lock_loop = loop
    return _ab_test_lock


def is_ab_test_busy() -> bool:
    """현재 event loop에서 A/B slot이 점유 중인지 반환한다."""
    return _get_ab_test_lock().locked()


@asynccontextmanager
async def _managed_ab_test_lock(
    config: AppConfig,
    test_id: str,
    lock: asyncio.Lock,
) -> AsyncIterator[None]:
    """예약 metadata가 lock 대기/실행 취소에도 terminal 상태가 되게 한다."""
    try:
        async with lock:
            yield
    except asyncio.CancelledError:
        try:
            ab_test_store.update_metadata(
                config,
                test_id,
                status="cancelled",
                current_variant=None,
                current_step=None,
                completed_at=_now_iso(),
                error="A/B 테스트 태스크가 lock 대기 또는 실행 중 취소되었습니다.",
            )
        except Exception as metadata_error:  # noqa: BLE001 - 원래 취소를 보존한다.
            logger.error(
                "취소된 A/B metadata 갱신 실패: test_id=%s, error=%s",
                test_id,
                metadata_error,
            )
        global _current_test_id
        if _current_test_id == test_id:
            _current_test_id = None
        _pop_cancel(test_id)
        raise


# 현재 진행 중인 테스트 ID (취소 진단용)
_current_test_id: str | None = None

# cancel_test() 가 추가하는 집합. variant 경계에서 러너가 확인.
_cancel_requests: set[str] = set()


# ============================================================
# test_id 생성
# ============================================================


def new_test_id() -> str:
    """`ab_{YYYYMMDD-HHMMSS}_{8자 16진수}` 형식의 test_id 를 생성한다.

    Returns:
        정규식 `^ab_\\d{8}-\\d{6}_[a-f0-9]{8}$` 을 만족하는 문자열
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    return f"ab_{ts}_{suffix}"


# ============================================================
# 금지 패턴 / 메트릭
# ============================================================


# §6.1 금지 패턴
_SPEAKER_PLACEHOLDER_RE = re.compile(r"SPEAKER_\d+")
_UNKNOWN_LABEL_RE = re.compile(r"\bUNKNOWN\b")
# 한글(English) 병기 — 괄호 안 첫 글자가 대문자인 영문
_ENGLISH_GLOSS_RE = re.compile(r"[가-힣]+\([A-Z][a-zA-Z]+\)")


def count_forbidden_patterns(text: str) -> dict[str, int]:
    """금지 패턴 발생 횟수를 센다.

    Args:
        text: 검사할 텍스트

    Returns:
        {speaker_placeholder, unknown_label, english_gloss, total}
    """
    if not text:
        return {
            "speaker_placeholder": 0,
            "unknown_label": 0,
            "english_gloss": 0,
            "total": 0,
        }
    sp = len(_SPEAKER_PLACEHOLDER_RE.findall(text))
    un = len(_UNKNOWN_LABEL_RE.findall(text))
    en = len(_ENGLISH_GLOSS_RE.findall(text))
    return {
        "speaker_placeholder": sp,
        "unknown_label": un,
        "english_gloss": en,
        "total": sp + un + en,
    }


def _concat_correct_text(corrected: CorrectedResult | None) -> str:
    """보정 결과의 발화를 줄바꿈으로 연결한다."""
    if corrected is None or not corrected.utterances:
        return ""
    return "\n".join(u.text for u in corrected.utterances)


def compute_metrics(
    corrected: CorrectedResult | None,
    summary_markdown: str | None,
    elapsed_seconds_by_step: dict[str, float],
) -> dict[str, Any]:
    """variant 별 `metrics.json` 에 쓸 딕셔너리를 만든다.

    Args:
        corrected: 보정 결과 (없으면 None)
        summary_markdown: 요약 마크다운 본문 (없으면 None)
        elapsed_seconds_by_step: 단계별 경과 시간 (초)

    Returns:
        §3.3 스키마의 딕셔너리
    """
    correct_text = _concat_correct_text(corrected)
    summary_text = summary_markdown or ""
    combined = (correct_text + "\n" + summary_text).strip()

    utterance_count = len(corrected.utterances) if corrected else 0
    correct_chars = len(correct_text)
    summary_chars = len(summary_text)
    avg_len = correct_chars / utterance_count if utterance_count > 0 else 0.0

    elapsed = dict(elapsed_seconds_by_step)
    elapsed["total"] = round(sum(elapsed_seconds_by_step.values()), 3)

    return {
        "elapsed_seconds": {k: round(v, 3) for k, v in elapsed.items()},
        "char_count": {"correct": correct_chars, "summary": summary_chars},
        "utterance_count": utterance_count,
        "avg_utterance_len": round(avg_len, 2),
        "forbidden_patterns": count_forbidden_patterns(combined),
    }


def compute_winner_score(metrics: dict[str, Any]) -> float:
    """§6.3 공식에 따라 참고용 점수를 계산한다.

    score = -2 * forbidden_total - 0.01 * elapsed_total + 0.5 * log1p(char_count)

    Args:
        metrics: compute_metrics 의 반환값

    Returns:
        스칼라 점수 (높을수록 우세)
    """
    forbidden_total = int(metrics.get("forbidden_patterns", {}).get("total", 0))
    elapsed_total = float(metrics.get("elapsed_seconds", {}).get("total", 0.0))
    char_count = int(
        metrics.get("char_count", {}).get("correct", 0)
        + metrics.get("char_count", {}).get("summary", 0)
    )
    return -2.0 * forbidden_total - 0.01 * elapsed_total + 0.5 * math.log1p(max(char_count, 0))


def determine_winner(metrics_a: dict[str, Any], metrics_b: dict[str, Any]) -> str:
    """두 variant 의 메트릭을 비교하여 참고용 승자를 결정한다.

    Args:
        metrics_a: variant A 메트릭
        metrics_b: variant B 메트릭

    Returns:
        "A" | "B" | "무승부"
    """
    score_a = compute_winner_score(metrics_a)
    score_b = compute_winner_score(metrics_b)
    # 근사 동등 판정 — 부동소수 오차 허용
    if math.isclose(score_a, score_b, rel_tol=1e-9, abs_tol=1e-6):
        return "무승부"
    return "A" if score_a > score_b else "B"


# ============================================================
# 내부 헬퍼
# ============================================================


async def _safe_broadcast(
    ws_broadcaster: Callable[[dict[str, Any]], Awaitable[None]] | None,
    payload: dict[str, Any],
) -> None:
    """ws_broadcaster 호출을 예외로부터 격리한다.

    Phase 2 의 WebSocket 모듈이 주입되기 전까지는 대부분 None 이며, 실패해도
    러너의 핵심 로직을 중단시키지 않는다.
    """
    if ws_broadcaster is None:
        return
    try:
        await ws_broadcaster(payload)
    except Exception as exc:  # noqa: BLE001 — 브로드캐스트는 best-effort
        logger.warning(f"A/B 테스트 브로드캐스트 실패(무시): {exc}")


async def _force_unload_llm(model_manager: ModelLoadManager) -> None:
    """현재 로드된 LLM/STT 모델을 언로드하고 짧게 대기한다.

    `ModelLoadManager` 에 전용 `force_unload_llm` 이 없으므로 `unload_model()`
    + `gc.collect()` + 짧은 sleep 으로 대체한다 (ADR-9 대비책).
    """
    try:
        await model_manager.unload_model()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"모델 언로드 실패(무시): {exc}")
    gc.collect()
    await asyncio.sleep(0.1)


def _variant_dir_name(variant: str) -> str:
    """'A' / 'B' → 'variant_a' / 'variant_b'."""
    if variant.upper() == "A":
        return "variant_a"
    if variant.upper() == "B":
        return "variant_b"
    raise ValueError(f"알 수 없는 variant: {variant!r}")


def _now_iso() -> str:
    """현재 시각 ISO 문자열."""
    return datetime.now().astimezone().isoformat()


def _lexical_configured_path(config: AppConfig, field_name: str, fallback: Path) -> Path:
    """base_dir symlink를 resolve로 숨기지 않은 설정 경로를 반환한다."""
    raw_base = getattr(config.paths, "base_dir", None)
    raw_child = getattr(config.paths, field_name, None)
    if isinstance(raw_base, (str, Path)) and isinstance(raw_child, (str, Path)):
        lexical_base = Path(raw_base).expanduser().absolute()
        child = Path(raw_child).expanduser()
        if child == Path(".") or ".." in child.parts or "\x00" in str(child):
            raise AudioAdmissionError(
                f"{field_name}은 base_dir 하위 상대경로여야 합니다: {raw_child!r}",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )
        candidate = child.absolute() if child.is_absolute() else (lexical_base / child).absolute()
        try:
            relative = candidate.relative_to(lexical_base)
        except ValueError as exc:
            raise AudioAdmissionError(
                f"{field_name}이 base_dir 밖을 가리킵니다: {candidate}",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            ) from exc
        if not relative.parts:
            raise AudioAdmissionError(
                f"{field_name}은 base_dir의 직접/하위 경로여야 합니다",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )
        return candidate
    return Path(fallback).expanduser().absolute()


def _validate_source_meeting_id(meeting_id: str) -> None:
    """A/B 원본 ID가 안전한 단일 경로 요소인지 검증한다."""
    if (
        not meeting_id
        or meeting_id in {".", ".."}
        or "\x00" in meeting_id
        or "/" in meeting_id
        or "\\" in meeting_id
        or Path(meeting_id).name != meeting_id
    ):
        raise AudioAdmissionError(
            f"유효하지 않은 A/B 회의 ID입니다: {meeting_id!r}",
            failure_kind=AudioFailureKind.SECURITY_BLOCKED,
        )


def _read_json_artifact_no_symlinks(
    artifact_path: Path,
    *,
    label: str,
) -> tuple[dict[str, Any], AudioFileIdentity]:
    """openat/O_NOFOLLOW로 JSON artifact를 target 경로를 따라가지 않고 읽는다."""
    lexical_path = artifact_path.expanduser().absolute()
    parts = lexical_path.parts[1:] if lexical_path.is_absolute() else lexical_path.parts
    if not lexical_path.is_absolute() or not parts or ".." in parts:
        raise AudioAdmissionError(
            f"유효하지 않은 {label} 경로입니다: {artifact_path}",
            failure_kind=AudioFailureKind.SECURITY_BLOCKED,
        )

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC

    def _raise_open_error(
        parent_fd: int,
        component: str,
        current_path: Path,
        *,
        is_final: bool,
        error: OSError,
    ) -> NoReturn:
        try:
            entry_stat = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"{label}을 찾을 수 없습니다: {artifact_path}") from exc
        except OSError as exc:
            raise AudioAdmissionError(
                f"{label} 경로 상태를 확인할 수 없습니다: {current_path} ({exc})",
                failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
            ) from exc

        if stat.S_ISLNK(entry_stat.st_mode):
            raise AudioAdmissionError(
                f"{label} 경로에 심볼릭 링크를 사용할 수 없습니다: {current_path}",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            ) from error
        expected_mode = stat.S_ISREG if is_final else stat.S_ISDIR
        if not expected_mode(entry_stat.st_mode):
            raise AudioAdmissionError(
                f"{label} 경로 요소가 안전한 일반 파일/디렉터리가 아닙니다: {current_path}",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            ) from error
        failure_kind = (
            AudioFailureKind.SECURITY_BLOCKED
            if error.errno in {errno.ELOOP, errno.ENOTDIR}
            else AudioFailureKind.INFRA_UNAVAILABLE
        )
        raise AudioAdmissionError(
            f"{label} 경로를 안전하게 열 수 없습니다: {current_path} ({error})",
            failure_kind=failure_kind,
        ) from error

    current_fd: int | None = None
    final_fd: int | None = None
    current_path = Path(lexical_path.anchor)
    try:
        current_fd = os.open(lexical_path.anchor, directory_flags)
        for component in parts[:-1]:
            current_path /= component
            try:
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            except OSError as exc:
                _raise_open_error(
                    current_fd,
                    component,
                    current_path,
                    is_final=False,
                    error=exc,
                )
            os.close(current_fd)
            current_fd = next_fd

        current_path /= parts[-1]
        try:
            final_fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
        except OSError as exc:
            _raise_open_error(
                current_fd,
                parts[-1],
                current_path,
                is_final=True,
                error=exc,
            )
        final_stat = os.fstat(final_fd)
        if not stat.S_ISREG(final_stat.st_mode):
            raise AudioAdmissionError(
                f"{label}이 일반 파일이 아닙니다: {artifact_path}",
                failure_kind=AudioFailureKind.SECURITY_BLOCKED,
            )
        owned_fd = final_fd
        final_fd = None
        with os.fdopen(owned_fd, encoding="utf-8") as artifact_file:
            payload = json.load(artifact_file)
            after_stat = os.fstat(artifact_file.fileno())
        before_identity: AudioFileIdentity = (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_size,
            final_stat.st_mtime_ns,
            final_stat.st_ctime_ns,
        )
        after_identity: AudioFileIdentity = (
            after_stat.st_dev,
            after_stat.st_ino,
            after_stat.st_size,
            after_stat.st_mtime_ns,
            after_stat.st_ctime_ns,
        )
        if after_identity != before_identity:
            raise AudioAdmissionError(
                f"{label}이 읽는 동안 변경되었습니다: {artifact_path}",
                failure_kind=AudioFailureKind.SOURCE_BUSY,
            )
    except AudioAdmissionError:
        raise
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise AudioAdmissionError(
            f"{label}을 안전하게 읽을 수 없습니다: {artifact_path} ({exc})",
            failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
        ) from exc
    finally:
        if final_fd is not None:
            os.close(final_fd)
        if current_fd is not None:
            os.close(current_fd)

    if not isinstance(payload, dict):
        raise ValueError(f"{label} JSON root가 object가 아닙니다: {artifact_path}")
    return payload, before_identity


def _read_merged_checkpoint_no_symlinks(
    merge_path: Path,
) -> tuple[MergedResult, AudioFileIdentity]:
    """merge checkpoint를 no-follow fd에서 파싱한다."""
    payload, identity = _read_json_artifact_no_symlinks(merge_path, label="merge checkpoint")
    try:
        utterances = [MergedUtterance(**item) for item in payload.get("utterances", [])]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"유효하지 않은 merge checkpoint schema: {merge_path}") from exc
    return (
        MergedResult(
            utterances=utterances,
            num_speakers=payload.get("num_speakers", 0),
            audio_path=payload.get("audio_path", ""),
            unknown_count=payload.get("unknown_count", 0),
        ),
        identity,
    )


def _read_diarize_checkpoint_no_symlinks(
    diarize_path: Path,
) -> tuple[DiarizationResult, AudioFileIdentity]:
    """diarize checkpoint를 no-follow fd에서 파싱한다."""
    payload, identity = _read_json_artifact_no_symlinks(
        diarize_path,
        label="diarize checkpoint",
    )
    try:
        segments = [DiarizationSegment(**item) for item in payload.get("segments", [])]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"유효하지 않은 diarize checkpoint schema: {diarize_path}") from exc
    return (
        DiarizationResult(
            segments=segments,
            num_speakers=payload.get("num_speakers", 0),
            audio_path=payload.get("audio_path", ""),
            model_name=payload.get("model_name", ""),
            output_mode=payload.get("output_mode", "regular"),
        ),
        identity,
    )


def _resolve_meeting_dir(config: AppConfig, meeting_id: str) -> Path:
    """본 파이프라인의 `checkpoints/{meeting_id}/` 디렉터리 경로를 반환한다.

    A/B 테스트에 필요한 중간 산출물(merge.json, diarize.json, transcribe.json)은
    checkpoints/ 에 저장된다. outputs/ 에는 최종 산출물(corrected.json, summary.md)만 있다.
    """
    _validate_source_meeting_id(meeting_id)
    checkpoints_root = _lexical_configured_path(
        config,
        "checkpoints_dir",
        config.paths.resolved_checkpoints_dir,
    )
    return checkpoints_root / meeting_id


def inspect_llm_ab_source(
    config: AppConfig,
    meeting_id: str,
) -> tuple[Path, AudioFileIdentity]:
    """LLM A/B source snapshot의 기존 artifact를 no-follow로 검증한다."""
    meeting_dir = _resolve_meeting_dir(config, meeting_id)
    merge_path = meeting_dir / "merge.json"
    _merged, identity = _read_merged_checkpoint_no_symlinks(merge_path)
    try:
        _read_json_artifact_no_symlinks(
            meeting_dir / "diarize.json",
            label="diarize checkpoint",
        )
    except FileNotFoundError:
        pass
    return merge_path, identity


def _resolve_wav_path(config: AppConfig, meeting_id: str) -> Path:
    """회의의 원본 WAV 파일 경로를 반환한다.

    WAV 는 audio_input/{meeting_id}.wav 에 저장된다 (pipeline 의 audio_converter 가 변환한 결과).
    """
    _validate_source_meeting_id(meeting_id)
    audio_input_root = _lexical_configured_path(
        config,
        "audio_input_dir",
        config.paths.resolved_audio_input_dir,
    )
    return audio_input_root / f"{meeting_id}.wav"


def _inspect_stt_audio_source(
    config: AppConfig,
    meeting_id: str,
) -> tuple[Path, AudioFileIdentity]:
    """STT A/B용 변환 WAV가 허용된 저장소의 안전한 자식인지 검사한다."""
    _validate_source_meeting_id(meeting_id)

    audio_input_dir = _lexical_configured_path(
        config,
        "audio_input_dir",
        config.paths.resolved_audio_input_dir,
    )
    outputs_root = _lexical_configured_path(
        config,
        "outputs_dir",
        config.paths.resolved_outputs_dir,
    )
    expected_output_dir = outputs_root / meeting_id

    # 처리된 업로드(M4A/MP3 포함)는 pipeline_state의 output-localized WAV를
    # 우선 사용한다. 예전 녹음/A-B fixture는 audio_input/{id}.wav로 폴백한다.
    wav_path: Path | None = None
    state_path = _resolve_meeting_dir(config, meeting_id) / "pipeline_state.json"
    try:
        state, _state_identity = _read_json_artifact_no_symlinks(
            state_path,
            label="pipeline state",
        )
        raw_wav_path = state.get("wav_path")
        if isinstance(raw_wav_path, str) and raw_wav_path:
            candidate = Path(raw_wav_path).expanduser().absolute()
            legacy_input_wav = _resolve_wav_path(config, meeting_id).expanduser().absolute()
            if candidate.suffix.lower() != ".wav" or (
                candidate.parent != expected_output_dir and candidate != legacy_input_wav
            ):
                raise AudioAdmissionError(
                    f"pipeline WAV가 허용된 회의 저장소의 직접 자식이 아닙니다: {candidate}",
                    failure_kind=AudioFailureKind.SECURITY_BLOCKED,
                )
            wav_path = candidate
    except FileNotFoundError:
        pass

    if wav_path is None:
        wav_path = _resolve_wav_path(config, meeting_id).expanduser().absolute()
    if wav_path.parent not in {audio_input_dir, expected_output_dir}:
        raise AudioAdmissionError(
            f"STT A/B 오디오가 허용된 저장소의 직접 자식이 아닙니다: {wav_path}",
            failure_kind=AudioFailureKind.SECURITY_BLOCKED,
        )

    try:
        identity = inspect_audio_path_no_symlinks(wav_path)
    except EmptyAudioError as exc:
        raise AudioAdmissionError(
            str(exc),
            failure_kind=AudioFailureKind.MEDIA_INVALID,
        ) from exc
    return wav_path, identity


def _assert_stt_audio_identity(
    wav_path: Path,
    expected_identity: AudioFileIdentity,
) -> None:
    """STT A/B 원본이 admission 이후 동일한 일반 파일인지 재확인한다."""
    try:
        current_identity = inspect_audio_path_no_symlinks(wav_path)
    except AudioAdmissionError:
        raise
    except (EmptyAudioError, FileNotFoundError) as exc:
        raise AudioAdmissionError(
            f"STT A/B 오디오가 검사 중 사라지거나 변경되었습니다: {wav_path}",
            failure_kind=AudioFailureKind.SOURCE_BUSY,
        ) from exc

    if current_identity != expected_identity:
        raise AudioAdmissionError(
            f"STT A/B 오디오가 검사 중 변경되었습니다: {wav_path}",
            failure_kind=AudioFailureKind.SOURCE_BUSY,
        )


async def _require_stt_audio_admission(
    config: AppConfig,
    wav_path: Path,
    expected_identity: AudioFileIdentity,
) -> None:
    """정책 설정과 무관한 identity 검사 후, 활성화된 full gate를 적용한다."""
    _assert_stt_audio_identity(wav_path, expected_identity)
    quality_config = getattr(config, "audio_quality", None)
    if quality_config is None or getattr(quality_config, "enabled", False) is not True:
        _assert_stt_audio_identity(wav_path, expected_identity)
        return

    try:
        admission = await asyncio.to_thread(
            validate_audio_quality,
            wav_path,
            min_mean_db=quality_config.min_mean_volume_db,
            min_duration_s=quality_config.min_duration_seconds,
            expected_identity=expected_identity,
            decode_timeout_base_seconds=quality_config.decode_timeout_base_seconds,
            decode_timeout_factor=quality_config.decode_timeout_factor,
            decode_timeout_cap_seconds=quality_config.decode_timeout_cap_seconds,
        )
    except Exception as exc:
        _assert_stt_audio_identity(wav_path, expected_identity)
        raise AudioAdmissionError(
            f"오디오 품질 검증 실패: {exc}",
            failure_kind=AudioFailureKind.INFRA_UNAVAILABLE,
        ) from exc

    _assert_stt_audio_identity(wav_path, expected_identity)
    if admission.status is AudioQualityStatus.ACCEPT:
        return

    failure_kind = admission.failure_kind or AudioFailureKind.INFRA_UNAVAILABLE
    reason = admission.reason or "오디오 품질 검증 비수락"
    raise AudioAdmissionError(
        f"오디오 품질 검증 거부 ({failure_kind.name}): {reason}",
        failure_kind=failure_kind,
    )


def _write_metrics_file(dir_path: Path, metrics: dict[str, Any]) -> None:
    """metrics.json 을 기록한다."""
    ab_test_store.write_variant_json(dir_path, "metrics.json", metrics)


def _write_summary_markdown(dir_path: Path, markdown: str) -> None:
    """summary.md 를 기록한다."""
    ab_test_store.write_variant_text(dir_path, "summary.md", markdown)


def _build_llm_temp_config(base_config: AppConfig, spec: ModelSpec) -> AppConfig:
    """variant 별 LLM 임시 설정을 생성한다 (원본 비오염).

    ADR-9: pydantic `model_copy(update=...)` 체이닝.
    """
    new_llm = base_config.llm.model_copy(
        update={
            "mlx_model_name": spec.model_id,
            "model_name": spec.model_id,
            "backend": spec.backend,
        }
    )
    return base_config.model_copy(update={"llm": new_llm})


def _build_stt_temp_config(base_config: AppConfig, spec: ModelSpec) -> AppConfig:
    """variant 별 STT 임시 설정을 생성한다 (원본 비오염).

    spec.model_id 가 레지스트리 짧은 ID (예: "seastar-medium-4bit")이면
    get_effective_model_path() 로 실제 사용할 경로를 결정한다.
    우선순위: 수동 임포트 로컬 경로 > HF 캐시 > HF repo ID.
    이렇게 해야 SSL 차단 환경에서 수동 다운로드한 모델도 A/B 테스트에서 쓸 수 있다.

    base_config 의 나머지 STT 설정 (transcribe_timeout_seconds 등) 은
    model_copy 로 그대로 유지된다.
    """
    from core.stt_model_registry import get_by_id as stt_get_by_id
    from core.stt_model_status import get_effective_model_path
    from core.transcription_models import (
        LOCAL_TRANSCRIPTION_ID,
        OPENAI_TRANSCRIBE_DIARIZE_MODEL,
        OPENAI_TRANSCRIPTION_ID,
    )

    if spec.backend == "openai":
        if spec.model_id != OPENAI_TRANSCRIPTION_ID:
            raise ValueError("지원하지 않는 OpenAI STT variant입니다.")
        new_stt = base_config.stt.model_copy(
            update={
                "provider": "openai",
                "openai_model": OPENAI_TRANSCRIBE_DIARIZE_MODEL,
            }
        )
        return base_config.model_copy(update={"stt": new_stt})

    if spec.model_id == LOCAL_TRANSCRIPTION_ID:
        actual_model_name = base_config.stt.resolve_model_path(
            base_dir=base_config.paths.resolved_base_dir
        )
        logger.info("STT 임시 config 생성: 현재 활성 로컬 모델")
        new_stt = base_config.stt.model_copy(
            update={"provider": "local", "model_name": actual_model_name}
        )
        return base_config.model_copy(update={"stt": new_stt})

    registry_spec = stt_get_by_id(spec.model_id)
    if registry_spec is not None:
        # 수동 임포트 > HF 캐시 > HF repo ID 우선순위로 실제 경로 결정
        actual_model_name = get_effective_model_path(registry_spec)
    else:
        # 레지스트리에 없는 ID — 그대로 전달 (사용자가 HF repo ID 를 직접 입력한 경우)
        actual_model_name = spec.model_id
    logger.info(f"STT 임시 config 생성: {spec.model_id} → {actual_model_name}")
    new_stt = base_config.stt.model_copy(
        update={"provider": "local", "model_name": actual_model_name}
    )
    return base_config.model_copy(update={"stt": new_stt})


def _is_cancelled(test_id: str) -> bool:
    """취소 요청 존재 여부."""
    return test_id in _cancel_requests


def _pop_cancel(test_id: str) -> None:
    """취소 플래그 제거."""
    _cancel_requests.discard(test_id)


def _init_metadata(
    *,
    test_id: str,
    test_type: str,
    source_meeting_id: str,
    source_snapshot: dict[str, Any],
    variant_a: ModelSpec,
    variant_b: ModelSpec,
    scope: dict[str, Any] | None,
) -> dict[str, Any]:
    """초기 metadata 딕셔너리를 만든다."""
    return {
        "test_id": test_id,
        "test_type": test_type,
        "source_meeting_id": source_meeting_id,
        "source_snapshot": source_snapshot,
        "scope": scope or {},
        "variant_a": asdict(variant_a),
        "variant_b": asdict(variant_b),
        "status": "pending",
        "current_variant": None,
        "current_step": None,
        "progress_pct": 0,
        "started_at": _now_iso(),
        "completed_at": None,
        "error": None,
        "variant_errors": {},
        "schema_version": 1,
    }


def reserve_stt_ab_test(
    config: AppConfig,
    *,
    test_id: str,
    source_meeting_id: str,
    wav_path: Path,
    variant_a: ModelSpec,
    variant_b: ModelSpec,
    allow_diarize_rerun: bool,
) -> None:
    """API가 202를 반환하기 전에 조회 가능한 STT pending metadata를 예약한다."""
    if not ab_test_store.is_valid_test_id(test_id):
        raise ValueError(f"유효하지 않은 test_id: {test_id!r}")
    meeting_dir = _resolve_meeting_dir(config, source_meeting_id)
    ab_test_store.create_test_dir(config, test_id)
    ab_test_store.write_metadata(
        config,
        test_id,
        _init_metadata(
            test_id=test_id,
            test_type="stt",
            source_meeting_id=source_meeting_id,
            source_snapshot={
                "merge_json_path": str(meeting_dir / "merge.json"),
                "wav_path": str(wav_path),
                "diarize_json_path": None,
            },
            variant_a=variant_a,
            variant_b=variant_b,
            scope={"allow_diarize_rerun": allow_diarize_rerun},
        ),
    )


def reserve_llm_ab_test(
    config: AppConfig,
    *,
    test_id: str,
    source_meeting_id: str,
    merge_path: Path,
    variant_a: ModelSpec,
    variant_b: ModelSpec,
    scope: LlmScope,
) -> None:
    """API가 202를 반환하기 전 조회 가능한 LLM pending metadata를 예약한다."""
    if not ab_test_store.is_valid_test_id(test_id):
        raise ValueError(f"유효하지 않은 test_id: {test_id!r}")
    meeting_dir = _resolve_meeting_dir(config, source_meeting_id)
    diarize_path = meeting_dir / "diarize.json"
    try:
        _read_json_artifact_no_symlinks(
            diarize_path,
            label="diarize checkpoint",
        )
        safe_diarize_path: str | None = str(diarize_path)
    except FileNotFoundError:
        safe_diarize_path = None

    ab_test_store.create_test_dir(config, test_id)
    ab_test_store.write_metadata(
        config,
        test_id,
        _init_metadata(
            test_id=test_id,
            test_type="llm",
            source_meeting_id=source_meeting_id,
            source_snapshot={
                "merge_json_path": str(merge_path),
                "wav_path": str(_resolve_wav_path(config, source_meeting_id)),
                "diarize_json_path": safe_diarize_path,
            },
            variant_a=variant_a,
            variant_b=variant_b,
            scope={"correct": scope.correct, "summarize": scope.summarize},
        ),
    )


# ============================================================
# LLM A/B 러너
# ============================================================


async def _run_llm_variant(
    *,
    config: AppConfig,
    model_manager: ModelLoadManager,
    variant: str,
    spec: ModelSpec,
    scope: LlmScope,
    merged: MergedResult,
    variant_dir: Path,
) -> dict[str, Any]:
    """단일 variant 에 대해 correct/summarize 를 수행하고 metrics 를 기록한다.

    Returns:
        metrics 딕셔너리
    """
    elapsed: dict[str, float] = {}
    corrected: CorrectedResult | None = None
    summary: SummaryResult | None = None

    temp_cfg = _build_llm_temp_config(config, spec)

    await _force_unload_llm(model_manager)

    if scope.correct:
        t0 = time.perf_counter()
        corrector = Corrector(temp_cfg, model_manager)
        corrected = await corrector.correct(merged)
        elapsed["correct"] = time.perf_counter() - t0
        ab_test_store.write_variant_json(
            variant_dir,
            "correct.json",
            corrected.to_dict(),
        )

    if scope.summarize:
        if corrected is None:
            # 교정을 스킵한 경우 merged → CorrectedResult 변환을 우회하기 위해
            # 원본 발화를 그대로 사용하는 얕은 CorrectedResult 를 만든다.
            from steps.corrector import CorrectedUtterance

            corrected = CorrectedResult(
                utterances=[
                    CorrectedUtterance(
                        text=u.text,
                        original_text=u.text,
                        speaker=u.speaker,
                        start=u.start,
                        end=u.end,
                        was_corrected=False,
                    )
                    for u in merged.utterances
                ],
                num_speakers=merged.num_speakers,
                audio_path=merged.audio_path,
            )
        t1 = time.perf_counter()
        summarizer = Summarizer(temp_cfg, model_manager)
        summary = await summarizer.summarize(corrected)
        elapsed["summarize"] = time.perf_counter() - t1
        _write_summary_markdown(variant_dir, summary.markdown)

    metrics = compute_metrics(
        corrected,
        summary.markdown if summary else None,
        elapsed,
    )
    _write_metrics_file(variant_dir, metrics)

    await _force_unload_llm(model_manager)

    return metrics


async def run_llm_ab_test(
    config: AppConfig,
    source_meeting_id: str,
    variant_a: ModelSpec,
    variant_b: ModelSpec,
    scope: LlmScope,
    ws_broadcaster: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    model_manager: ModelLoadManager | None = None,
    test_id: str | None = None,
    expected_merge_identity: AudioFileIdentity | None = None,
    metadata_reserved: bool = False,
) -> str:
    """기존 회의의 `merge.json` 을 입력으로 LLM 2종의 교정/요약을 순차 실행한다.

    Args:
        config: 앱 설정
        source_meeting_id: 원본 회의 ID (`outputs/{id}/merge.json` 필요)
        variant_a: A 모델 스펙
        variant_b: B 모델 스펙
        scope: 실행 범위 (correct/summarize)
        ws_broadcaster: (선택) step_progress 브로드캐스트 콜러블
        model_manager: (선택) 테스트에서 Mock 주입용
        test_id: (선택) 외부에서 미리 선점한 test_id. None 이면 내부 생성.
            API 레이어가 202 응답에 포함시킬 ID 를 먼저 확보한 뒤 러너를 백그라운드로
            실행할 때 사용한다.

    Returns:
        생성된 test_id

    Raises:
        RuntimeError: 다른 A/B 테스트가 이미 진행 중일 때
        FileNotFoundError: merge.json 이 없을 때
        ValueError: 두 variant 의 model_id 가 동일할 때
    """
    if variant_a.model_id == variant_b.model_id and variant_a.backend == variant_b.backend:
        raise ValueError("variant_a 와 variant_b 가 동일합니다.")

    # test_id 선점: API 레이어가 202 응답에 포함시킬 ID 를 외부에서 주입할 수 있다.
    if test_id is None:
        if metadata_reserved:
            raise ValueError("metadata_reserved에는 test_id가 필요합니다.")
        test_id = new_test_id()
    elif not ab_test_store.is_valid_test_id(test_id):
        raise ValueError(f"유효하지 않은 test_id: {test_id!r}")

    metadata_available = metadata_reserved
    try:
        meeting_dir = _resolve_meeting_dir(config, source_meeting_id)
        merge_path = meeting_dir / "merge.json"
        merged, current_merge_identity = _read_merged_checkpoint_no_symlinks(merge_path)
        if (
            expected_merge_identity is not None
            and current_merge_identity != expected_merge_identity
        ):
            raise AudioAdmissionError(
                f"LLM A/B merge checkpoint가 요청 후 변경되었습니다: {merge_path}",
                failure_kind=AudioFailureKind.SOURCE_BUSY,
            )
        if not metadata_reserved:
            reserve_llm_ab_test(
                config,
                test_id=test_id,
                source_meeting_id=source_meeting_id,
                merge_path=merge_path,
                variant_a=variant_a,
                variant_b=variant_b,
                scope=scope,
            )
            metadata_available = True

        lock = _get_ab_test_lock()
        if lock.locked():
            raise RuntimeError("다른 A/B 테스트가 이미 진행 중입니다.")
    except asyncio.CancelledError:
        if metadata_available:
            ab_test_store.update_metadata(
                config,
                test_id,
                status="cancelled",
                completed_at=_now_iso(),
                error="A/B 테스트 태스크가 시작 전 취소되었습니다.",
            )
        raise
    except Exception as exc:
        if metadata_available:
            ab_test_store.update_metadata(
                config,
                test_id,
                status="failed",
                completed_at=_now_iso(),
                error=f"{type(exc).__name__}: {exc}",
            )
        raise

    mm = model_manager or get_model_manager()

    async with _managed_ab_test_lock(config, test_id, lock):
        global _current_test_id
        _current_test_id = test_id

        # lock 획득 후 상태를 "running" 으로 갱신
        ab_test_store.update_metadata(config, test_id, status="running")

        variant_success: dict[str, dict[str, Any]] = {}
        variant_errors: dict[str, str] = {}

        try:
            for variant, spec in (("A", variant_a), ("B", variant_b)):
                # 취소 요청 확인 (variant 경계)
                if _is_cancelled(test_id):
                    logger.info(f"A/B 테스트 취소 감지: {test_id} (variant={variant})")
                    ab_test_store.update_metadata(
                        config,
                        test_id,
                        status="cancelled",
                        current_variant=variant,
                        completed_at=_now_iso(),
                    )
                    _pop_cancel(test_id)
                    return test_id

                ab_test_store.update_metadata(
                    config,
                    test_id,
                    current_variant=variant,
                    current_step="correct",
                )
                await _safe_broadcast(
                    ws_broadcaster,
                    {
                        "type": "step_progress",
                        "ab_test_id": test_id,
                        "variant": variant,
                        "step": "correct",
                        "status": "start",
                        "progress": 0.0,
                    },
                )

                variant_dir = ab_test_store.resolve_variant_dir(
                    config,
                    test_id,
                    _variant_dir_name(variant),
                )
                try:
                    metrics = await _run_llm_variant(
                        config=config,
                        model_manager=mm,
                        variant=variant,
                        spec=spec,
                        scope=scope,
                        merged=merged,
                        variant_dir=variant_dir,
                    )
                    variant_success[variant] = metrics
                    await _safe_broadcast(
                        ws_broadcaster,
                        {
                            "type": "step_progress",
                            "ab_test_id": test_id,
                            "variant": variant,
                            "step": "summarize" if scope.summarize else "correct",
                            "status": "complete",
                            "progress": 1.0,
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"A/B 테스트 variant {variant} 실패: {exc}", exc_info=True)
                    variant_errors[variant] = str(exc)
                    # 에러 로그도 variant 디렉터리에 남긴다
                    try:
                        ab_test_store.write_variant_text(
                            variant_dir,
                            "stderr.log",
                            f"{type(exc).__name__}: {exc}\n",
                        )
                    except (OSError, ValueError):
                        pass
                    await _force_unload_llm(mm)

            if _is_cancelled(test_id):
                ab_test_store.update_metadata(
                    config,
                    test_id,
                    status="cancelled",
                    current_variant=None,
                    current_step=None,
                    completed_at=_now_iso(),
                    error="사용자가 A/B 테스트를 취소했습니다.",
                )
                return test_id

            # 최종 상태 결정
            if not variant_errors:
                final_status = "completed"
            elif len(variant_errors) == len(("A", "B")):
                final_status = "failed"
            else:
                final_status = "partial_failed"

            ab_test_store.update_metadata(
                config,
                test_id,
                status=final_status,
                current_variant=None,
                current_step=None,
                completed_at=_now_iso(),
                variant_errors=variant_errors,
                error=None if not variant_errors else "일부 variant 실패",
            )
            return test_id

        except Exception as exc:  # noqa: BLE001 — 예상외 오류 전반
            logger.exception("A/B 테스트 실행 중 예외")
            ab_test_store.update_metadata(
                config,
                test_id,
                status="failed",
                completed_at=_now_iso(),
                error=str(exc),
            )
            raise
        finally:
            _current_test_id = None
            _pop_cancel(test_id)


# ============================================================
# STT A/B 러너
# ============================================================


async def _ensure_diarize(
    *,
    config: AppConfig,
    model_manager: ModelLoadManager,
    meeting_dir: Path,
    wav_path: Path,
    expected_identity: AudioFileIdentity,
    allow_diarize_rerun: bool,
) -> DiarizationResult:
    """diarize 체크포인트를 로드하거나, 허용 시 1회 재실행한다.

    Args:
        meeting_dir: checkpoints/{meeting_id}/ 경로 (diarize.json 위치)
        wav_path: audio_input/{meeting_id}.wav 경로 (재실행 시 필요)
    """
    ckpt = meeting_dir / "diarize.json"
    try:
        cached_result, _identity = _read_diarize_checkpoint_no_symlinks(ckpt)
        logger.info(f"diarize 체크포인트 재사용: {ckpt}")
        return cached_result
    except FileNotFoundError:
        pass
    if not allow_diarize_rerun:
        raise RuntimeError(
            "화자분리 체크포인트가 없습니다. 화자분리 재실행에 동의한 뒤 다시 시도해 주세요."
        )
    logger.info("diarize 체크포인트 없음 → 사용자 동의에 따라 화자분리 1회 실행")
    _assert_stt_audio_identity(wav_path, expected_identity)
    diarizer = Diarizer(config, model_manager)
    return await diarizer.diarize(wav_path)


async def _run_stt_variant(
    *,
    config: AppConfig,
    model_manager: ModelLoadManager,
    spec: ModelSpec,
    wav_path: Path,
    expected_identity: AudioFileIdentity,
    cached_diarize: DiarizationResult,
    variant_dir: Path,
    openai_resume_dir: Path,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """단일 STT variant 를 실행한다.

    본 파이프라인(pipeline.py)과 동일하게 VAD 전처리를 적용하여
    무음 구간의 환각(hallucination)을 방지한다.
    """
    elapsed: dict[str, float] = {}
    temp_cfg = _build_stt_temp_config(config, spec)

    _assert_stt_audio_identity(wav_path, expected_identity)
    await _force_unload_llm(model_manager)

    # VAD 전처리: 음성 구간만 추출하여 무음 환각 방지 (pipeline.py 와 동일)
    vad_clip_timestamps: list[float] | None = None
    vad_config = getattr(config, "vad", None)
    if (
        spec.backend != "openai"
        and vad_config is not None
        and getattr(vad_config, "enabled", False)
    ):
        try:
            from steps.vad_detector import VoiceActivityDetector

            vad = VoiceActivityDetector(config)
            vad_result = await vad.detect(wav_path)
            if vad_result is not None:
                vad_clip_timestamps = vad_result.clip_timestamps
                logger.info(
                    f"A/B STT VAD 적용: {vad_result.num_segments}개 음성 구간, "
                    f"무음 {vad_result.total_silence_seconds:.1f}초 제거"
                )
        except Exception as e:
            logger.warning(f"A/B STT VAD 실패, 전체 오디오로 폴백: {e}")

    _assert_stt_audio_identity(wav_path, expected_identity)
    t0 = time.perf_counter()
    if spec.backend == "openai":
        from steps.openai_transcriber import OpenAITranscriber

        transcriber: Any = OpenAITranscriber(temp_cfg)
    else:
        transcriber = Transcriber(temp_cfg, model_manager)
    if spec.backend == "openai":
        transcript = await transcriber.transcribe(
            wav_path,
            vad_clip_timestamps=vad_clip_timestamps,
            resume_dir=openai_resume_dir,
            should_cancel=should_cancel,
            expected_audio_identity=expected_identity,
        )
    else:
        transcript = await transcriber.transcribe(
            wav_path,
            vad_clip_timestamps=vad_clip_timestamps,
        )
    elapsed["transcribe"] = time.perf_counter() - t0
    ab_test_store.write_variant_json(
        variant_dir,
        "transcribe.json",
        transcript.to_dict(),
    )

    t1 = time.perf_counter()
    merger = Merger()
    merged = await merger.merge(transcript, cached_diarize)
    elapsed["merge"] = time.perf_counter() - t1
    ab_test_store.write_variant_json(
        variant_dir,
        "merge.json",
        merged.to_dict(),
    )

    # STT 테스트에서는 LLM 교정/요약을 수행하지 않으므로 corrected=None
    metrics = compute_metrics(None, None, elapsed)
    # 전사 본문 글자수만 집계에 반영 (correct 위치에 넣는다)
    metrics["char_count"]["correct"] = sum(len(u.text) for u in merged.utterances)
    metrics["utterance_count"] = len(merged.utterances)
    if merged.utterances:
        metrics["avg_utterance_len"] = round(
            metrics["char_count"]["correct"] / len(merged.utterances), 2
        )
    # 금지 패턴 재계산
    body = "\n".join(u.text for u in merged.utterances)
    metrics["forbidden_patterns"] = count_forbidden_patterns(body)
    _write_metrics_file(variant_dir, metrics)

    if spec.backend == "openai":
        try:
            transcriber.cleanup_resume_cache(openai_resume_dir)
        except Exception as exc:  # noqa: BLE001 - variant 산출물은 이미 안전하다.
            logger.warning(f"OpenAI A/B 재개 캐시 정리 실패: {exc}")

    await _force_unload_llm(model_manager)
    return metrics


async def run_stt_ab_test(
    config: AppConfig,
    source_meeting_id: str,
    variant_a: ModelSpec,
    variant_b: ModelSpec,
    allow_diarize_rerun: bool = False,
    ws_broadcaster: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    model_manager: ModelLoadManager | None = None,
    test_id: str | None = None,
    metadata_reserved: bool = False,
    expected_source_identity: AudioFileIdentity | None = None,
) -> str:
    """STT 모델 2종을 순차 실행하고 결과를 격리 저장한다.

    Args:
        config: 앱 설정
        source_meeting_id: 원본 회의 ID (`outputs/{id}/input.wav` 필요)
        variant_a: A 모델 스펙 (STT)
        variant_b: B 모델 스펙 (STT)
        allow_diarize_rerun: diarize 체크포인트가 없을 때 재실행을 허용할지
        ws_broadcaster: (선택) 브로드캐스트 콜러블
        model_manager: (선택) 주입용
        test_id: (선택) 외부 주입 test_id. None 이면 내부 생성.
        expected_source_identity: API admission에서 동의한 원본 파일 identity.
            제공되면 runner가 새 파일을 기준으로 다시 채택하지 않는다.

    Returns:
        test_id

    Raises:
        RuntimeError: 다른 A/B 테스트가 이미 진행 중일 때
        FileNotFoundError: input.wav 가 없을 때
        ValueError: 두 variant 가 동일하거나 diarize 체크포인트가 없고 재실행 비허용
    """
    if variant_a.model_id == variant_b.model_id:
        raise ValueError("variant_a 와 variant_b 가 동일합니다.")

    # test_id 선점: API 레이어가 202 응답에 포함시킬 ID 를 외부에서 주입할 수 있다.
    if test_id is None:
        if metadata_reserved:
            raise ValueError("metadata_reserved에는 test_id가 필요합니다.")
        test_id = new_test_id()
    elif not ab_test_store.is_valid_test_id(test_id):
        raise ValueError(f"유효하지 않은 test_id: {test_id!r}")

    try:
        wav_path, inspected_identity = _inspect_stt_audio_source(config, source_meeting_id)
        expected_identity = expected_source_identity or inspected_identity
        if expected_source_identity is not None:
            _assert_stt_audio_identity(wav_path, expected_source_identity)
        await _require_stt_audio_admission(config, wav_path, expected_identity)
        meeting_dir = _resolve_meeting_dir(config, source_meeting_id)
    except asyncio.CancelledError:
        if metadata_reserved:
            try:
                ab_test_store.update_metadata(
                    config,
                    test_id,
                    status="cancelled",
                    current_variant=None,
                    current_step=None,
                    completed_at=_now_iso(),
                    error="A/B STT admission 대기 중 태스크가 취소되었습니다.",
                )
            except Exception as metadata_error:  # noqa: BLE001 - 원래 취소를 보존한다.
                logger.error(
                    "취소된 STT A/B metadata 갱신 실패: test_id=%s, error=%s",
                    test_id,
                    metadata_error,
                )
        _pop_cancel(test_id)
        raise
    except Exception as exc:
        if metadata_reserved:
            failure_kind = getattr(exc, "failure_kind", None)
            error_prefix = (
                f"{failure_kind.name}: " if isinstance(failure_kind, AudioFailureKind) else ""
            )
            ab_test_store.update_metadata(
                config,
                test_id,
                status="failed",
                completed_at=_now_iso(),
                error=f"{error_prefix}{type(exc).__name__}: {exc}",
            )
        raise

    mm = model_manager or get_model_manager()

    # Race condition 방지: lock 획득 전에 pending 상태의 초기 metadata 를 먼저 기록한다.
    # asyncio.create_task() 로 발사된 코루틴이 실제로 lock 을 획득하기 전에
    # 프론트엔드가 GET /api/ab-tests/{test_id} 를 호출하면 FileNotFoundError 가
    # 발생해 404 를 반환하는 race condition 을 이 방식으로 차단한다.
    # diarize 경로는 lock 진입 후에 결정되므로 일단 None 으로 기록하고 갱신한다.
    if not metadata_reserved:
        reserve_stt_ab_test(
            config,
            test_id=test_id,
            source_meeting_id=source_meeting_id,
            wav_path=wav_path,
            variant_a=variant_a,
            variant_b=variant_b,
            allow_diarize_rerun=allow_diarize_rerun,
        )

    lock = _get_ab_test_lock()
    if lock.locked():
        busy_error = RuntimeError("다른 A/B 테스트가 이미 진행 중입니다.")
        ab_test_store.update_metadata(
            config,
            test_id,
            status="failed",
            current_variant=None,
            current_step=None,
            completed_at=_now_iso(),
            error=f"RuntimeError: {busy_error}",
        )
        raise busy_error

    async with _managed_ab_test_lock(config, test_id, lock):
        global _current_test_id
        _current_test_id = test_id

        try:
            # lock 대기 중 바뀐 원본을 모델/diarize가 열기 전에 다시 차단한다.
            _assert_stt_audio_identity(wav_path, expected_identity)

            # diarize 캐시 확보 (variant 전에 1회). 실제 재실행 직전에도 helper가
            # 동일 identity를 다시 확인한다.
            cached_diarize = await _ensure_diarize(
                config=config,
                model_manager=mm,
                meeting_dir=meeting_dir,
                wav_path=wav_path,
                expected_identity=expected_identity,
                allow_diarize_rerun=allow_diarize_rerun,
            )

            # diarize 경로 확정 후 metadata 갱신 + 상태 "running" 으로 전환
            diarize_path = meeting_dir / "diarize.json"
            try:
                _read_diarize_checkpoint_no_symlinks(diarize_path)
                safe_diarize_path: str | None = str(diarize_path)
            except FileNotFoundError:
                safe_diarize_path = None
            ab_test_store.update_metadata(
                config,
                test_id,
                status="running",
                source_snapshot={
                    "merge_json_path": str(meeting_dir / "merge.json"),
                    "wav_path": str(wav_path),
                    "diarize_json_path": safe_diarize_path,
                },
            )

            variant_errors: dict[str, str] = {}
            variant_success: dict[str, dict[str, Any]] = {}

            for variant, spec in (("A", variant_a), ("B", variant_b)):
                if _is_cancelled(test_id):
                    ab_test_store.update_metadata(
                        config,
                        test_id,
                        status="cancelled",
                        current_variant=variant,
                        completed_at=_now_iso(),
                    )
                    _pop_cancel(test_id)
                    return test_id

                ab_test_store.update_metadata(
                    config,
                    test_id,
                    current_variant=variant,
                    current_step="transcribe",
                )
                await _safe_broadcast(
                    ws_broadcaster,
                    {
                        "type": "step_progress",
                        "ab_test_id": test_id,
                        "variant": variant,
                        "step": "transcribe",
                        "status": "start",
                        "progress": 0.0,
                    },
                )

                variant_dir = ab_test_store.resolve_variant_dir(
                    config,
                    test_id,
                    _variant_dir_name(variant),
                )
                try:
                    metrics = await _run_stt_variant(
                        config=config,
                        model_manager=mm,
                        spec=spec,
                        wav_path=wav_path,
                        expected_identity=expected_identity,
                        cached_diarize=cached_diarize,
                        variant_dir=variant_dir,
                        # 테스트별 저장소에 격리해 다른 test의 DELETE/재시도와
                        # raw provider 응답 캐시가 경합하지 않게 한다.
                        openai_resume_dir=variant_dir.parent / ".openai-transcribe-parts",
                        should_cancel=lambda: _is_cancelled(test_id),
                    )
                    if _is_cancelled(test_id):
                        raise asyncio.CancelledError(
                            "사용자가 OpenAI STT A/B 테스트를 취소했습니다."
                        )
                    variant_success[variant] = metrics
                    await _safe_broadcast(
                        ws_broadcaster,
                        {
                            "type": "step_progress",
                            "ab_test_id": test_id,
                            "variant": variant,
                            "step": "merge",
                            "status": "complete",
                            "progress": 1.0,
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"STT A/B variant {variant} 실패: {exc}", exc_info=True)
                    variant_errors[variant] = str(exc)
                    try:
                        ab_test_store.write_variant_text(
                            variant_dir,
                            "stderr.log",
                            f"{type(exc).__name__}: {exc}\n",
                        )
                    except (OSError, ValueError):
                        pass
                    await _force_unload_llm(mm)

            if _is_cancelled(test_id):
                raise asyncio.CancelledError("사용자가 OpenAI STT A/B 테스트를 취소했습니다.")
            if not variant_errors:
                final_status = "completed"
            elif len(variant_errors) == 2:
                final_status = "failed"
            else:
                final_status = "partial_failed"

            ab_test_store.update_metadata(
                config,
                test_id,
                status=final_status,
                current_variant=None,
                current_step=None,
                completed_at=_now_iso(),
                variant_errors=variant_errors,
                error=None if not variant_errors else "일부 variant 실패",
            )
            return test_id

        except asyncio.CancelledError:
            ab_test_store.update_metadata(
                config,
                test_id,
                status="cancelled",
                current_variant=None,
                current_step=None,
                completed_at=_now_iso(),
                error="A/B 테스트 태스크가 취소되었습니다.",
            )
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("STT A/B 테스트 실행 중 예외")
            ab_test_store.update_metadata(
                config,
                test_id,
                status="failed",
                current_variant=None,
                current_step=None,
                completed_at=_now_iso(),
                error=str(exc),
            )
            raise
        finally:
            _current_test_id = None
            _pop_cancel(test_id)


# ============================================================
# 조회 / 삭제 / 취소 (공개 API)
# ============================================================


def _read_variant_dir(variant_dir: Path) -> dict[str, Any]:
    """variant 디렉터리의 산출물을 딕셔너리로 읽어 반환한다 (없으면 빈값)."""
    out: dict[str, Any] = {"metrics": None, "correct": None, "summary": None}
    for filename, key in (
        ("metrics.json", "metrics"),
        ("correct.json", "correct"),
        ("transcribe.json", "transcribe"),
    ):
        try:
            out[key] = ab_test_store.read_variant_json(variant_dir, filename)
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning(f"{filename} 읽기 실패: {variant_dir} ({exc})")

    try:
        out["summary"] = ab_test_store.read_variant_text(variant_dir, "summary.md")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning(f"summary.md 읽기 실패: {variant_dir} ({exc})")

    return out


def get_test_status(config: AppConfig, test_id: str) -> dict[str, Any]:
    """metadata.json 만 간략히 읽어 상태 요약을 반환한다.

    Raises:
        ValueError: test_id 부적합
        FileNotFoundError: metadata.json 이 없을 때
    """
    meta = ab_test_store.read_metadata(config, test_id)
    return {
        "test_id": meta.get("test_id"),
        "test_type": meta.get("test_type"),
        "status": meta.get("status"),
        "current_variant": meta.get("current_variant"),
        "current_step": meta.get("current_step"),
        "progress_pct": meta.get("progress_pct", 0),
        "started_at": meta.get("started_at"),
        "completed_at": meta.get("completed_at"),
        "error": meta.get("error"),
    }


def get_test_result(config: AppConfig, test_id: str) -> dict[str, Any]:
    """metadata + variant_a + variant_b 산출물을 하나의 딕셔너리로 반환한다."""
    meta = ab_test_store.read_metadata(config, test_id)
    return {
        "metadata": meta,
        "variant_a": _read_variant_dir(
            ab_test_store.resolve_variant_dir(config, test_id, "variant_a")
        ),
        "variant_b": _read_variant_dir(
            ab_test_store.resolve_variant_dir(config, test_id, "variant_b")
        ),
    }


def list_tests(config: AppConfig, source_meeting_id: str | None = None) -> list[dict[str, Any]]:
    """저장된 테스트 목록을 최신순 요약으로 반환한다."""
    result: list[dict[str, Any]] = []
    for tid in ab_test_store.list_test_ids(config, source_meeting_id):
        try:
            meta = ab_test_store.read_metadata(config, tid)
        except (FileNotFoundError, ValueError):
            continue
        result.append(
            {
                "test_id": tid,
                "test_type": meta.get("test_type"),
                "status": meta.get("status"),
                "source_meeting_id": meta.get("source_meeting_id"),
                "variant_a": meta.get("variant_a"),
                "variant_b": meta.get("variant_b"),
                "started_at": meta.get("started_at"),
                "completed_at": meta.get("completed_at"),
            }
        )
    return result


def recover_orphaned_tests(config: AppConfig) -> int:
    """재시작 시 live task가 없는 active metadata를 terminal 상태로 복구한다."""
    recovered = 0
    for test_id in ab_test_store.list_test_ids(config):
        try:
            metadata = ab_test_store.read_metadata(config, test_id)
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("A/B orphan metadata 조회 실패: test_id=%s, error=%s", test_id, exc)
            continue
        status = metadata.get("status")
        if status not in {"pending", "running", "cancelling"}:
            continue
        cancelled = status == "cancelling"
        try:
            ab_test_store.update_metadata(
                config,
                test_id,
                status="cancelled" if cancelled else "failed",
                current_variant=None,
                current_step=None,
                completed_at=_now_iso(),
                error=(
                    "앱 종료 중 취소된 A/B 테스트입니다."
                    if cancelled
                    else "앱 종료로 중단된 A/B 테스트입니다. 다시 시작해 주세요."
                ),
            )
            recovered += 1
        except (OSError, ValueError) as exc:
            logger.error("A/B orphan metadata 복구 실패: test_id=%s, error=%s", test_id, exc)
    return recovered


def delete_test(config: AppConfig, test_id: str) -> None:
    """테스트 디렉터리를 삭제한다."""
    ab_test_store.delete_test_dir(config, test_id)


async def cancel_test(config: AppConfig, test_id: str) -> None:
    """테스트 취소를 요청한다 (best-effort).

    러너는 variant 경계에서 `_cancel_requests` 를 확인하므로, 이미 실행 중인
    variant 의 LLM 호출을 즉시 중단시키지는 못한다.
    """
    if not ab_test_store.is_valid_test_id(test_id):
        raise ValueError(f"유효하지 않은 test_id: {test_id!r}")
    metadata = ab_test_store.read_metadata(config, test_id)
    status = str(metadata.get("status", ""))
    if status not in {"pending", "running", "cancelling"}:
        raise ValueError(f"취소할 수 있는 A/B 테스트 상태가 아닙니다: {status or 'unknown'}")
    if status != "cancelling":
        ab_test_store.update_metadata(
            config,
            test_id,
            status="cancelling",
            error="사용자 취소 요청이 저장되었습니다.",
        )
    _cancel_requests.add(test_id)
    logger.info(f"A/B 테스트 취소 요청 등록: {test_id}")
