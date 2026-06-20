#!/usr/bin/env python3
"""AI 파이프라인 단계별 성능/메모리 측정 하네스.

실제 PipelineManager 단계 구현을 순서대로 호출하면서 wall time, RSS, 가용 메모리,
swap, MLX active/peak memory, 단계별 품질 지표를 JSON 으로 저장한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import AppConfig, load_config
from core.pipeline import PipelineManager
from steps.diarizer import DiarizationResult, DiarizationSegment

logger = logging.getLogger(__name__)


@dataclass
class ResourceSnapshot:
    """측정 시점의 프로세스/시스템/MLX 메모리 상태."""

    rss_mb: float
    available_mb: float
    swap_used_mb: float
    mlx_active_mb: float | None = None
    mlx_peak_mb: float | None = None


@dataclass
class StepMeasurement:
    """단일 파이프라인 단계 측정 결과."""

    name: str
    elapsed_seconds: float
    before: ResourceSnapshot
    after: ResourceSnapshot
    rss_delta_mb: float
    available_delta_mb: float
    swap_delta_mb: float
    quality: dict[str, Any]
    success: bool = True
    error: str = ""


def _mb(value: float | int) -> float:
    """바이트 값을 MB 단위 float 으로 변환한다."""
    return round(float(value) / (1024 * 1024), 3)


def _mlx_memory_snapshot() -> tuple[float | None, float | None]:
    """이미 로드된 mlx.core 에서 active/peak memory 를 읽는다."""
    mx = sys.modules.get("mlx.core")
    if mx is None:
        return None, None

    active_fn = getattr(mx, "get_active_memory", None)
    peak_fn = getattr(mx, "get_peak_memory", None)
    active = _mb(active_fn()) if callable(active_fn) else None
    peak = _mb(peak_fn()) if callable(peak_fn) else None
    return active, peak


def _reset_mlx_peak_memory() -> None:
    """가능한 경우 MLX peak memory counter 를 리셋한다."""
    mx = sys.modules.get("mlx.core")
    reset_fn = getattr(mx, "reset_peak_memory", None) if mx is not None else None
    if callable(reset_fn):
        reset_fn()


def snapshot_resources() -> ResourceSnapshot:
    """현재 리소스 상태를 측정한다."""
    process = psutil.Process()
    virtual = psutil.virtual_memory()
    swap = psutil.swap_memory()
    mlx_active, mlx_peak = _mlx_memory_snapshot()
    return ResourceSnapshot(
        rss_mb=_mb(process.memory_info().rss),
        available_mb=_mb(virtual.available),
        swap_used_mb=_mb(swap.used),
        mlx_active_mb=mlx_active,
        mlx_peak_mb=mlx_peak,
    )


async def measure_step(
    name: str,
    factory: Callable[[], Awaitable[Any]],
    quality_factory: Callable[[Any], dict[str, Any]],
) -> tuple[Any, StepMeasurement]:
    """비동기 단계 실행 전후 리소스와 품질 지표를 측정한다."""
    _reset_mlx_peak_memory()
    before = snapshot_resources()
    started = time.perf_counter()
    try:
        result = await factory()
    except Exception as exc:
        elapsed = time.perf_counter() - started
        after = snapshot_resources()
        measurement = StepMeasurement(
            name=name,
            elapsed_seconds=round(elapsed, 3),
            before=before,
            after=after,
            rss_delta_mb=round(after.rss_mb - before.rss_mb, 3),
            available_delta_mb=round(after.available_mb - before.available_mb, 3),
            swap_delta_mb=round(after.swap_used_mb - before.swap_used_mb, 3),
            quality={},
            success=False,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise RuntimeError(json.dumps(asdict(measurement), ensure_ascii=False)) from exc

    elapsed = time.perf_counter() - started
    after = snapshot_resources()
    measurement = StepMeasurement(
        name=name,
        elapsed_seconds=round(elapsed, 3),
        before=before,
        after=after,
        rss_delta_mb=round(after.rss_mb - before.rss_mb, 3),
        available_delta_mb=round(after.available_mb - before.available_mb, 3),
        swap_delta_mb=round(after.swap_used_mb - before.swap_used_mb, 3),
        quality=quality_factory(result),
    )
    return result, measurement


def _bool_arg(value: str) -> bool:
    """CLI bool 값을 파싱한다."""
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"bool 값이 아닙니다: {value}")


def _find_latest_audio(config: AppConfig) -> Path:
    """입력 폴더에서 가장 최근 오디오 파일을 찾는다."""
    input_dir = config.paths.resolved_audio_input_dir
    suffixes = {f".{suffix.lower()}" for suffix in config.audio.supported_input_formats}
    candidates = [
        path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() in suffixes
    ]
    if not candidates:
        raise FileNotFoundError(f"입력 폴더에 오디오 파일이 없습니다: {input_dir}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _apply_overrides(config: AppConfig, args: argparse.Namespace) -> None:
    """CLI variant 인자를 AppConfig에 반영한다."""
    if args.stt_word_timestamps is not None:
        config.stt.word_timestamps = args.stt_word_timestamps
    if args.vad_mode is not None:
        config.vad.mode = args.vad_mode
        config.vad.enabled = args.vad_mode != "off"
    if args.diarization_model:
        config.diarization.model_name = args.diarization_model
    if args.diarization_output_mode:
        config.diarization.output_mode = args.diarization_output_mode
    if args.correction_mode:
        config.llm.correction_mode = args.correction_mode
    if args.no_adaptive_correction_tokens:
        config.llm.correction_adaptive_max_tokens = False


def _quality_for_transcript(result: Any) -> dict[str, Any]:
    """전사 결과 품질 관측 지표를 추출한다."""
    segments = getattr(result, "segments", []) or []
    return {
        "segment_count": len(segments),
        "full_text_chars": len(getattr(result, "full_text", "") or ""),
        "duration_seconds": round(
            max((getattr(seg, "end", 0.0) for seg in segments), default=0.0), 3
        ),
        "avg_logprob_mean": _mean([getattr(seg, "avg_logprob", 0.0) for seg in segments]),
        "no_speech_prob_mean": _mean([getattr(seg, "no_speech_prob", 0.0) for seg in segments]),
    }


def _quality_for_diarization(result: Any) -> dict[str, Any]:
    """화자분리 결과 품질 관측 지표를 추출한다."""
    segments = getattr(result, "segments", []) or []
    return {
        "segment_count": len(segments),
        "num_speakers": getattr(result, "num_speakers", 0),
        "duration_seconds": round(getattr(result, "total_duration", 0.0), 3),
        "model_name": getattr(result, "model_name", ""),
        "output_mode": getattr(result, "output_mode", ""),
    }


def _quality_for_merge(result: Any) -> dict[str, Any]:
    """병합 결과 품질 관측 지표를 추출한다."""
    utterances = getattr(result, "utterances", []) or []
    unknown_count = int(getattr(result, "unknown_count", 0) or 0)
    return {
        "utterance_count": len(utterances),
        "num_speakers": getattr(result, "num_speakers", 0),
        "unknown_count": unknown_count,
        "unknown_ratio": round(unknown_count / len(utterances), 4) if utterances else 0.0,
        "duration_seconds": round(getattr(result, "total_duration", 0.0), 3),
    }


def _quality_for_correction(result: Any) -> dict[str, Any]:
    """교정 결과 품질 관측 지표를 추출한다."""
    utterances = getattr(result, "utterances", []) or []
    return {
        "utterance_count": len(utterances),
        "total_corrected": getattr(result, "total_corrected", 0),
        "total_failed": getattr(result, "total_failed", 0),
        "correction_rate": round(getattr(result, "correction_rate", 0.0), 4),
    }


def _quality_for_summary(result: Any) -> dict[str, Any]:
    """요약 결과 품질 관측 지표를 추출한다."""
    return {
        "markdown_chars": len(getattr(result, "markdown", "") or ""),
        "was_chunked": bool(getattr(result, "was_chunked", False)),
        "chunk_count": getattr(result, "chunk_count", 1),
    }


def _mean(values: list[float]) -> float:
    """빈 리스트에 안전한 평균 계산."""
    if not values:
        return 0.0
    return round(sum(float(value) for value in values) / len(values), 4)


def _single_speaker_diarization(transcript: Any, audio_path: Path) -> DiarizationResult:
    """화자분리 생략 시 병합 가능한 단일 화자 결과를 만든다."""
    segments = getattr(transcript, "segments", []) or []
    duration = max((float(getattr(segment, "end", 0.0)) for segment in segments), default=0.0)
    return DiarizationResult(
        segments=[DiarizationSegment("SPEAKER_00", 0.0, duration)] if duration > 0 else [],
        num_speakers=1 if duration > 0 else 0,
        audio_path=str(audio_path),
        model_name="single-speaker-skip",
        output_mode="synthetic",
    )


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    """CLI 인자를 기준으로 AI 파이프라인 측정을 실행한다."""
    config = load_config(Path(args.config))
    _apply_overrides(config, args)

    audio_path = Path(args.audio).expanduser() if args.audio else _find_latest_audio(config)
    if not audio_path.exists():
        raise FileNotFoundError(f"오디오 파일을 찾을 수 없습니다: {audio_path}")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = (
        Path(args.output_dir).expanduser()
        if args.output_dir
        else (config.paths.resolved_base_dir / "benchmarks" / run_id)
    )
    output_root.mkdir(parents=True, exist_ok=True)
    output_dir = output_root / "outputs"
    checkpoint_dir = output_root / "checkpoints"
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    manager = PipelineManager(config)
    measurements: list[StepMeasurement] = []

    wav_path, measurement = await measure_step(
        "convert",
        lambda: manager._run_step_convert(audio_path, output_dir),
        lambda result: {"wav_path": str(result), "wav_size_mb": _mb(Path(result).stat().st_size)},
    )
    measurements.append(measurement)

    transcript, measurement = await measure_step(
        "transcribe",
        lambda: manager._run_step_transcribe(wav_path, checkpoint_dir / "transcribe.json"),
        _quality_for_transcript,
    )
    measurements.append(measurement)

    if args.skip_diarization:
        diarization = _single_speaker_diarization(transcript, wav_path)
        measurements.append(
            StepMeasurement(
                name="diarize",
                elapsed_seconds=0.0,
                before=snapshot_resources(),
                after=snapshot_resources(),
                rss_delta_mb=0.0,
                available_delta_mb=0.0,
                swap_delta_mb=0.0,
                quality=_quality_for_diarization(diarization),
            )
        )
    else:
        diarization, measurement = await measure_step(
            "diarize",
            lambda: manager._run_step_diarize(wav_path, checkpoint_dir / "diarize.json"),
            _quality_for_diarization,
        )
        measurements.append(measurement)

    merged, measurement = await measure_step(
        "merge",
        lambda: manager._run_step_merge(transcript, diarization, checkpoint_dir / "merge.json"),
        _quality_for_merge,
    )
    measurements.append(measurement)

    if args.skip_llm:
        corrected = manager._build_passthrough_corrected_result(merged)
        measurements.append(
            StepMeasurement(
                name="correct",
                elapsed_seconds=0.0,
                before=snapshot_resources(),
                after=snapshot_resources(),
                rss_delta_mb=0.0,
                available_delta_mb=0.0,
                swap_delta_mb=0.0,
                quality=_quality_for_correction(corrected),
            )
        )
    else:
        corrected, measurement = await measure_step(
            "correct",
            lambda: manager._run_step_correct(merged, checkpoint_dir / "correct.json"),
            _quality_for_correction,
        )
        measurements.append(measurement)

        summary, measurement = await measure_step(
            "summarize",
            lambda: manager._run_step_summarize(
                corrected,
                checkpoint_dir / "summarize.json",
                output_dir,
            ),
            _quality_for_summary,
        )
        measurements.append(measurement)
        await manager._unload_llm_model_if_current()

    total_elapsed = round(sum(measurement.elapsed_seconds for measurement in measurements), 3)
    report = {
        "run_id": run_id,
        "audio_path": str(audio_path),
        "output_root": str(output_root),
        "config": {
            "stt_model": config.stt.model_name,
            "stt_word_timestamps": config.stt.word_timestamps,
            "vad_mode": config.vad.mode,
            "diarization_model": config.diarization.model_name,
            "diarization_output_mode": config.diarization.output_mode,
            "llm_model": config.llm.mlx_model_name
            if config.llm.backend == "mlx"
            else config.llm.model_name,
            "llm_backend": config.llm.backend,
            "correction_mode": config.llm.correction_mode,
            "correction_adaptive_max_tokens": config.llm.correction_adaptive_max_tokens,
        },
        "total_elapsed_seconds": total_elapsed,
        "steps": [asdict(measurement) for measurement in measurements],
    }

    report_path = output_root / "ai_pipeline_benchmark.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def build_parser() -> argparse.ArgumentParser:
    """CLI parser 를 생성한다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.yaml"), help="config.yaml 경로")
    parser.add_argument("--audio", help="측정할 오디오 파일. 생략 시 입력 폴더의 최신 파일")
    parser.add_argument("--output-dir", help="측정 결과 저장 디렉토리")
    parser.add_argument(
        "--stt-word-timestamps", type=_bool_arg, help="STT word_timestamps override"
    )
    parser.add_argument("--vad-mode", choices=["off", "on", "auto"], help="VAD mode override")
    parser.add_argument("--diarization-model", help="pyannote model_name override")
    parser.add_argument(
        "--diarization-output-mode",
        choices=["regular", "exclusive", "auto"],
        help="pyannote output mode override",
    )
    parser.add_argument(
        "--correction-mode",
        choices=["full", "changed_only", "auto"],
        help="LLM correction output mode override",
    )
    parser.add_argument(
        "--no-adaptive-correction-tokens",
        action="store_true",
        help="교정 max_tokens 동적 축소 비활성화",
    )
    parser.add_argument(
        "--skip-diarization", action="store_true", help="단일 화자로 화자분리 생략"
    )
    parser.add_argument("--skip-llm", action="store_true", help="교정/요약 LLM 단계 생략")
    parser.add_argument("--log-level", default="INFO", help="로그 레벨")
    return parser


def main() -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        report = asyncio.run(run_benchmark(args))
    except Exception as exc:
        logger.error("AI pipeline benchmark failed: %s", exc)
        return 1

    print(
        json.dumps({"report_path": report["report_path"], "summary": report}, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
