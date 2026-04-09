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
import gc
import json
import logging
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from config import AppConfig, get_config
from core import ab_test_store
from core.model_manager import ModelLoadManager, get_model_manager
from steps.corrector import CorrectedResult, Corrector
from steps.diarizer import DiarizationResult, Diarizer
from steps.merger import MergedResult, Merger
from steps.summarizer import SummaryResult, Summarizer
from steps.transcriber import TranscriptResult, Transcriber

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
        backend: LLM 전용. "mlx" 또는 "ollama" (STT 에서는 관례상 "mlx")
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
    return (
        -2.0 * forbidden_total
        - 0.01 * elapsed_total
        + 0.5 * math.log1p(max(char_count, 0))
    )


def determine_winner(
    metrics_a: dict[str, Any], metrics_b: dict[str, Any]
) -> str:
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


def _resolve_meeting_dir(config: AppConfig, meeting_id: str) -> Path:
    """본 파이프라인의 `checkpoints/{meeting_id}/` 디렉터리 경로를 반환한다.

    A/B 테스트에 필요한 중간 산출물(merge.json, diarize.json, transcribe.json)은
    checkpoints/ 에 저장된다. outputs/ 에는 최종 산출물(corrected.json, summary.md)만 있다.
    """
    return config.paths.resolved_checkpoints_dir / meeting_id


def _resolve_wav_path(config: AppConfig, meeting_id: str) -> Path:
    """회의의 원본 WAV 파일 경로를 반환한다.

    WAV 는 audio_input/{meeting_id}.wav 에 저장된다 (pipeline 의 audio_converter 가 변환한 결과).
    """
    return config.paths.resolved_audio_input_dir / f"{meeting_id}.wav"


def _write_metrics_file(dir_path: Path, metrics: dict[str, Any]) -> None:
    """metrics.json 을 기록한다."""
    dir_path.mkdir(parents=True, exist_ok=True)
    with open(dir_path / "metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)


def _write_summary_markdown(dir_path: Path, markdown: str) -> None:
    """summary.md 를 기록한다."""
    dir_path.mkdir(parents=True, exist_ok=True)
    with open(dir_path / "summary.md", "w", encoding="utf-8") as f:
        f.write(markdown)


def _build_llm_temp_config(
    base_config: AppConfig, spec: ModelSpec
) -> AppConfig:
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


def _build_stt_temp_config(
    base_config: AppConfig, spec: ModelSpec
) -> AppConfig:
    """variant 별 STT 임시 설정을 생성한다 (원본 비오염).

    spec.model_id 가 레지스트리 짧은 ID (예: "seastar-medium-4bit")이면
    실제 HF repo ID (예: "youngouk/seastar-medium-ko-4bit-mlx")로 변환한다.
    """
    from core.stt_model_registry import get_by_id as stt_get_by_id

    # 레지스트리에서 실제 model_path(HF repo ID) 조회
    registry_spec = stt_get_by_id(spec.model_id)
    actual_model_name = registry_spec.model_path if registry_spec else spec.model_id
    new_stt = base_config.stt.model_copy(update={"model_name": actual_model_name})
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
        corrected.save_checkpoint(variant_dir / "correct.json")

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

    lock = _get_ab_test_lock()
    if lock.locked():
        raise RuntimeError("다른 A/B 테스트가 이미 진행 중입니다.")

    meeting_dir = _resolve_meeting_dir(config, source_meeting_id)
    merge_path = meeting_dir / "merge.json"
    if not merge_path.exists():
        raise FileNotFoundError(
            f"merge.json 이 없습니다: {merge_path}. 소스 회의를 먼저 처리해야 합니다."
        )

    mm = model_manager or get_model_manager()
    # test_id 선점: API 레이어가 202 응답에 포함시킬 ID 를 외부에서 주입할 수 있다.
    if test_id is None:
        test_id = new_test_id()
    elif not ab_test_store.is_valid_test_id(test_id):
        raise ValueError(f"유효하지 않은 test_id: {test_id!r}")

    # Race condition 방지: lock 획득 전에 pending 상태의 초기 metadata 를 먼저 기록한다.
    # asyncio.create_task() 로 발사된 코루틴이 실제로 lock 을 획득하기 전에
    # 프론트엔드가 GET /api/ab-tests/{test_id} 를 호출하면 FileNotFoundError 가
    # 발생해 404 를 반환하는 race condition 을 이 방식으로 차단한다.
    ab_test_store.create_test_dir(config, test_id)
    initial_metadata = _init_metadata(
        test_id=test_id,
        test_type="llm",
        source_meeting_id=source_meeting_id,
        source_snapshot={
            "merge_json_path": str(merge_path.resolve()),
            "wav_path": str(_resolve_wav_path(config, source_meeting_id).resolve()),
            "diarize_json_path": str((meeting_dir / "diarize.json").resolve())
            if (meeting_dir / "diarize.json").exists()
            else None,
        },
        variant_a=variant_a,
        variant_b=variant_b,
        scope={"correct": scope.correct, "summarize": scope.summarize},
    )
    # status 는 _init_metadata 기본값인 "pending" 유지 — lock 획득 후 "running" 으로 갱신
    ab_test_store.write_metadata(config, test_id, initial_metadata)

    async with lock:
        global _current_test_id
        _current_test_id = test_id

        test_dir = ab_test_store.resolve_test_dir(config, test_id)
        # lock 획득 후 상태를 "running" 으로 갱신
        ab_test_store.update_metadata(config, test_id, status="running")

        # 소스 merge 체크포인트 로드 (한 번만)
        merged = MergedResult.from_checkpoint(merge_path)

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

                variant_dir = test_dir / _variant_dir_name(variant)
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
                    logger.error(
                        f"A/B 테스트 variant {variant} 실패: {exc}", exc_info=True
                    )
                    variant_errors[variant] = str(exc)
                    # 에러 로그도 variant 디렉터리에 남긴다
                    try:
                        variant_dir.mkdir(parents=True, exist_ok=True)
                        (variant_dir / "stderr.log").write_text(
                            f"{type(exc).__name__}: {exc}\n", encoding="utf-8"
                        )
                    except OSError:
                        pass
                    await _force_unload_llm(mm)

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
    allow_diarize_rerun: bool,
) -> DiarizationResult:
    """diarize 체크포인트를 로드하거나, 허용 시 1회 재실행한다.

    Args:
        meeting_dir: checkpoints/{meeting_id}/ 경로 (diarize.json 위치)
        wav_path: audio_input/{meeting_id}.wav 경로 (재실행 시 필요)
    """
    ckpt = meeting_dir / "diarize.json"
    if ckpt.exists():
        return DiarizationResult.from_checkpoint(ckpt)
    if not allow_diarize_rerun:
        raise ValueError(
            "diarize 체크포인트가 없습니다. allow_diarize_rerun=True 로 재실행 허용 필요"
        )
    if not wav_path.exists():
        raise FileNotFoundError(f"WAV 파일이 없습니다: {wav_path}")
    diarizer = Diarizer(config, model_manager)
    return await diarizer.diarize(wav_path)


async def _run_stt_variant(
    *,
    config: AppConfig,
    model_manager: ModelLoadManager,
    spec: ModelSpec,
    wav_path: Path,
    cached_diarize: DiarizationResult,
    variant_dir: Path,
) -> dict[str, Any]:
    """단일 STT variant 를 실행한다."""
    elapsed: dict[str, float] = {}
    temp_cfg = _build_stt_temp_config(config, spec)

    await _force_unload_llm(model_manager)

    t0 = time.perf_counter()
    transcriber = Transcriber(temp_cfg, model_manager)
    transcript: TranscriptResult = await transcriber.transcribe(wav_path)
    elapsed["transcribe"] = time.perf_counter() - t0
    transcript.save_checkpoint(variant_dir / "transcribe.json")

    t1 = time.perf_counter()
    merger = Merger()
    merged = await merger.merge(transcript, cached_diarize)
    elapsed["merge"] = time.perf_counter() - t1
    merged.save_checkpoint(variant_dir / "merge.json")

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

    Returns:
        test_id

    Raises:
        RuntimeError: 다른 A/B 테스트가 이미 진행 중일 때
        FileNotFoundError: input.wav 가 없을 때
        ValueError: 두 variant 가 동일하거나 diarize 체크포인트가 없고 재실행 비허용
    """
    if variant_a.model_id == variant_b.model_id:
        raise ValueError("variant_a 와 variant_b 가 동일합니다.")

    lock = _get_ab_test_lock()
    if lock.locked():
        raise RuntimeError("다른 A/B 테스트가 이미 진행 중입니다.")

    meeting_dir = _resolve_meeting_dir(config, source_meeting_id)
    wav_path = _resolve_wav_path(config, source_meeting_id)
    if not wav_path.exists():
        raise FileNotFoundError(f"WAV 파일이 없습니다: {wav_path}")

    mm = model_manager or get_model_manager()
    # test_id 선점: API 레이어가 202 응답에 포함시킬 ID 를 외부에서 주입할 수 있다.
    if test_id is None:
        test_id = new_test_id()
    elif not ab_test_store.is_valid_test_id(test_id):
        raise ValueError(f"유효하지 않은 test_id: {test_id!r}")

    # Race condition 방지: lock 획득 전에 pending 상태의 초기 metadata 를 먼저 기록한다.
    # asyncio.create_task() 로 발사된 코루틴이 실제로 lock 을 획득하기 전에
    # 프론트엔드가 GET /api/ab-tests/{test_id} 를 호출하면 FileNotFoundError 가
    # 발생해 404 를 반환하는 race condition 을 이 방식으로 차단한다.
    # diarize 경로는 lock 진입 후에 결정되므로 일단 None 으로 기록하고 갱신한다.
    ab_test_store.create_test_dir(config, test_id)
    initial_metadata = _init_metadata(
        test_id=test_id,
        test_type="stt",
        source_meeting_id=source_meeting_id,
        source_snapshot={
            "merge_json_path": str((meeting_dir / "merge.json").resolve()),
            "wav_path": str(wav_path.resolve()),
            "diarize_json_path": None,  # diarize 확보 후 갱신됨
        },
        variant_a=variant_a,
        variant_b=variant_b,
        scope={"allow_diarize_rerun": allow_diarize_rerun},
    )
    # status 는 _init_metadata 기본값인 "pending" 유지 — lock 획득 후 "running" 으로 갱신
    ab_test_store.write_metadata(config, test_id, initial_metadata)

    async with lock:
        global _current_test_id
        _current_test_id = test_id

        test_dir = ab_test_store.resolve_test_dir(config, test_id)

        # diarize 캐시 확보 (variant 전에 1회)
        cached_diarize = await _ensure_diarize(
            config=config,
            model_manager=mm,
            meeting_dir=meeting_dir,
            wav_path=wav_path,
            allow_diarize_rerun=allow_diarize_rerun,
        )

        # diarize 경로 확정 후 metadata 갱신 + 상태 "running" 으로 전환
        diarize_path = meeting_dir / "diarize.json"
        ab_test_store.update_metadata(
            config,
            test_id,
            status="running",
            source_snapshot={
                "merge_json_path": str((meeting_dir / "merge.json").resolve()),
                "wav_path": str(wav_path.resolve()),
                "diarize_json_path": (
                    str(diarize_path.resolve()) if diarize_path.exists() else None
                ),
            },
        )

        variant_errors: dict[str, str] = {}
        variant_success: dict[str, dict[str, Any]] = {}

        try:
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

                variant_dir = test_dir / _variant_dir_name(variant)
                try:
                    metrics = await _run_stt_variant(
                        config=config,
                        model_manager=mm,
                        spec=spec,
                        wav_path=wav_path,
                        cached_diarize=cached_diarize,
                        variant_dir=variant_dir,
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
                    logger.error(
                        f"STT A/B variant {variant} 실패: {exc}", exc_info=True
                    )
                    variant_errors[variant] = str(exc)
                    try:
                        variant_dir.mkdir(parents=True, exist_ok=True)
                        (variant_dir / "stderr.log").write_text(
                            f"{type(exc).__name__}: {exc}\n", encoding="utf-8"
                        )
                    except OSError:
                        pass
                    await _force_unload_llm(mm)

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

        except Exception as exc:  # noqa: BLE001
            logger.exception("STT A/B 테스트 실행 중 예외")
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
# 조회 / 삭제 / 취소 (공개 API)
# ============================================================


def _read_variant_dir(variant_dir: Path) -> dict[str, Any]:
    """variant 디렉터리의 산출물을 딕셔너리로 읽어 반환한다 (없으면 빈값)."""
    out: dict[str, Any] = {"metrics": None, "correct": None, "summary": None}
    metrics_path = variant_dir / "metrics.json"
    if metrics_path.exists():
        try:
            with open(metrics_path, encoding="utf-8") as f:
                out["metrics"] = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"metrics.json 읽기 실패: {metrics_path} ({exc})")

    correct_path = variant_dir / "correct.json"
    if correct_path.exists():
        try:
            with open(correct_path, encoding="utf-8") as f:
                out["correct"] = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"correct.json 읽기 실패: {correct_path} ({exc})")

    summary_path = variant_dir / "summary.md"
    if summary_path.exists():
        try:
            out["summary"] = summary_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(f"summary.md 읽기 실패: {summary_path} ({exc})")

    transcribe_path = variant_dir / "transcribe.json"
    if transcribe_path.exists():
        try:
            with open(transcribe_path, encoding="utf-8") as f:
                out["transcribe"] = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"transcribe.json 읽기 실패: {transcribe_path} ({exc})")

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
    test_dir = ab_test_store.resolve_test_dir(config, test_id)
    return {
        "metadata": meta,
        "variant_a": _read_variant_dir(test_dir / "variant_a"),
        "variant_b": _read_variant_dir(test_dir / "variant_b"),
    }


def list_tests(
    config: AppConfig, source_meeting_id: str | None = None
) -> list[dict[str, Any]]:
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
    _cancel_requests.add(test_id)
    logger.info(f"A/B 테스트 취소 요청 등록: {test_id}")
