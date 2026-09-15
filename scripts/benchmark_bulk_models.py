#!/usr/bin/env python3
"""고정 합성 입력으로 로컬 모델 재사용과 의존성 변경을 비교한다.

기본 workload는 고정 교정 프롬프트이며 Whisper·화자분리·검색은 포함하지 않는다.
--synthetic-audio를 명시하면 해당 합성 음성의 Whisper→Corrector 경로를 비교한다.
모델은 기존 HF 캐시만 사용하고 출력에는 입력/생성 텍스트 대신 해시·개수만 기록한다.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)
MODEL = "mlx-community/gemma-4-e4b-it-4bit"
SYSTEM_PROMPT = (
    "한국어 문장의 명백한 띄어쓰기 오류만 교정하세요. 이름, 숫자, 의미를 보존하세요. "
    "추론 과정이나 설명 없이 같은 번호와 교정 문장만 출력하세요."
)
FIXED_PROMPTS = (
    "1. 오늘 회의 에서는 다음 주 일정을 확인합니다.\n2. 민수는 수요일까지 초안을 작성합니다.",
    "1. 오늘 회의 에서는 지난 주 결과를 확인합니다.\n2. 지수는 금요일까지 보고서를 작성합니다.",
)


def environment_metadata() -> dict[str, Any]:
    """비밀·사용자 내용 없이 재현에 필요한 환경 정보를 반환한다."""
    versions: dict[str, str | None] = {}
    package_paths: dict[str, str] = {}
    for name in ("mlx", "mlx-lm", "mlx-vlm", "mlx-whisper", "transformers", "numpy"):
        try:
            distribution = importlib.metadata.distribution(name)
            versions[name] = distribution.version
            package_paths[name] = str(distribution.locate_file(name.replace("-", "_")))
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    hardware: dict[str, str] = {}
    if platform.system() == "Darwin":
        for key in ("machdep.cpu.brand_string", "hw.memsize"):
            result = subprocess.run(
                ["sysctl", "-n", key], capture_output=True, text=True, timeout=5, check=False
            )
            if result.returncode == 0:
                hardware[key] = result.stdout.strip()
    return {
        "python": platform.python_version(),
        "os": platform.platform(),
        "packages": versions,
        "package_paths": package_paths,
        "hardware": hardware,
    }


def text_fingerprint(text: str) -> dict[str, Any]:
    """실제 텍스트를 남기지 않고 동일 출력 여부를 비교할 수 있게 한다."""
    return {"chars": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()}


async def measure_lifecycle(
    runs: int,
    lifecycle: str,
    operation: Callable[[int], Awaitable[dict[str, Any]]],
    cleanup: Callable[[], Awaitable[None]],
) -> dict[str, Any]:
    """재로드 조건과 마지막 해제까지 포함한 실제 전체 시간을 기록한다."""
    records: list[dict[str, Any]] = []
    started = time.perf_counter()
    cleanup_seconds = 0.0
    try:
        for index in range(runs):
            iteration_started = time.perf_counter()
            record = await operation(index)
            record["run"] = index + 1
            record["operation_seconds"] = time.perf_counter() - iteration_started
            records.append(record)
            if lifecycle == "reload":
                cleanup_started = time.perf_counter()
                await cleanup()
                cleanup_seconds += time.perf_counter() - cleanup_started
    finally:
        cleanup_started = time.perf_counter()
        await cleanup()
        cleanup_seconds += time.perf_counter() - cleanup_started
    return {
        "lifecycle": lifecycle,
        "runs": records,
        "cleanup_seconds": cleanup_seconds,
        "total_wall_seconds": time.perf_counter() - started,
    }


def _generation_telemetry(backend: Any) -> Any:
    """백엔드가 공개하는 마지막 생성 계측만 읽고 비공개 모델 내부는 읽지 않는다."""
    getter = getattr(backend, "get_generation_metrics", None)
    return getter() if callable(getter) else None


async def benchmark_prompts(args: argparse.Namespace, config: Any) -> dict[str, Any]:
    """앱 모델 매니저와 thread-bound 백엔드로 고정 입력을 실행한다."""
    from core.llm_backend import create_backend
    from core.model_manager import ModelLoadManager, await_native_inference

    manager = ModelLoadManager()
    loads: list[float] = []

    def load() -> Any:
        """백엔드 생성 구간을 측정하되 지연 가중치 로딩은 분리되지 않음을 유지한다."""
        started = time.perf_counter()
        backend = create_backend(config.llm)
        loads.append(time.perf_counter() - started)
        return backend

    async def operation(index: int) -> dict[str, Any]:
        """같은 시스템 프롬프트와 서로 다른 합성 사용자 입력을 실행한다."""
        load_count_before = len(loads)
        async with manager.acquire("exaone", load, keep_loaded=True) as backend:
            prompt = FIXED_PROMPTS[index % len(FIXED_PROMPTS)]
            started = time.perf_counter()
            output = await await_native_inference(
                backend.chat,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=args.max_tokens,
            )
            generation_seconds = time.perf_counter() - started
            stats = _generation_telemetry(backend)
            return {
                "generation_wall_seconds": generation_seconds,
                "backend_created": len(loads) > load_count_before,
                "input": text_fingerprint(prompt),
                "output": text_fingerprint(output),
                "generation_stats": stats,
                "manager_status_after_generation": manager.get_status(),
            }

    result = await measure_lifecycle(args.runs, args.lifecycle, operation, manager.unload_model)
    result["backend_creation_seconds"] = loads
    result["manager_status_after_cleanup"] = manager.get_status()
    result["scope"] = "fixed_synthetic_prompts_only"
    result["limitations"] = [
        "Backend creation does not isolate all lazy weight loading or compilation.",
        "Different prompts alternate; timings are not repeated identical-input samples.",
        "Output hashes compare identical prompts across reports; they do not measure accuracy.",
    ]
    return result


async def benchmark_audio(args: argparse.Namespace, config: Any) -> dict[str, Any]:
    """명시된 합성 음성으로 전사·교정의 순차 또는 단계별 재사용을 비교한다."""
    from core.model_manager import ModelLoadManager
    from steps.corrector import Corrector
    from steps.merger import MergedResult, MergedUtterance
    from steps.transcriber import Transcriber

    class BenchmarkManager(ModelLoadManager):
        """격리 벤치마크에서 동일 단계의 모델만 다음 입력까지 유지한다."""

        def acquire(
            self,
            name: str,
            loader: Any,
            *,
            keep_loaded: bool = False,
            reuse_key: str | None = None,
        ) -> Any:
            """기존 매니저의 잠금·모델 교체·예외 해제 계약을 그대로 사용한다."""
            return super().acquire(
                name,
                loader,
                keep_loaded=keep_loaded or args.lifecycle == "retained",
                reuse_key=reuse_key,
            )

    manager = BenchmarkManager()
    records: list[dict[str, Any]] = [{"run": i + 1} for i in range(args.runs)]
    transcripts: dict[int, Any] = {}
    schedule = (
        [(stage, i) for stage in ("transcribe", "correct") for i in range(args.runs)]
        if args.lifecycle == "retained"
        else [(stage, i) for i in range(args.runs) for stage in ("transcribe", "correct")]
    )
    started = time.perf_counter()
    try:
        for stage, index in schedule:
            stage_started = time.perf_counter()
            if stage == "transcribe":
                transcripts[index] = await Transcriber(config, manager).transcribe(
                    args.synthetic_audio
                )
                records[index]["segment_count"] = len(transcripts[index].segments)
            else:
                transcript = transcripts.pop(index)
                merged = MergedResult(
                    utterances=[
                        MergedUtterance(s.text, "UNKNOWN", s.start, s.end)
                        for s in transcript.segments
                    ],
                    num_speakers=0,
                    audio_path=str(args.synthetic_audio),
                    unknown_count=len(transcript.segments),
                )
                corrected = await Corrector(config, manager).correct(merged)
                records[index]["correction_failed"] = corrected.total_failed
                records[index]["output"] = text_fingerprint(
                    "\n".join(u.text for u in corrected.utterances)
                )
                records[index]["result_ready_seconds"] = time.perf_counter() - started
                records[index]["last_batch_generation_stats"] = _generation_telemetry(
                    manager.current_model
                )
            records[index][f"{stage}_wall_seconds"] = time.perf_counter() - stage_started
            records[index][f"manager_status_after_{stage}"] = manager.get_status()
            if args.lifecycle == "reload":
                await manager.unload_model()
    finally:
        await manager.unload_model()
    return {
        "scope": "synthetic_whisper_correct_only",
        "lifecycle": args.lifecycle,
        "runs": records,
        "total_wall_seconds": time.perf_counter() - started,
        "manager_status_after_cleanup": manager.get_status(),
        "limitations": [
            "No diarization, summary, indexing, queue or cooldown.",
            "Grouped stages can delay the first complete result.",
            "Generation metrics describe the last correction batch, not the whole meeting.",
        ],
    }


def main() -> None:
    """외부 모델 다운로드 없이 격리 출력에 벤치마크 결과를 저장한다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="새 JSON 출력 경로")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--lifecycle", choices=("retained", "reload"), default="retained")
    parser.add_argument("--max-tokens", type=int, default=96)
    parser.add_argument("--synthetic-audio", type=Path, help="사용자가 지정한 합성 음성만 사용")
    args = parser.parse_args()
    if args.runs < 1 or args.max_tokens < 1:
        parser.error("runs와 max-tokens는 양수여야 합니다")
    if args.output.exists():
        parser.error("기존 출력은 덮어쓰지 않습니다")
    if args.synthetic_audio is not None and not args.synthetic_audio.is_file():
        parser.error("합성 음성 파일이 없습니다")
    args.output = args.output.absolute()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["MT_BASE_DIR"] = str(args.output.parent / f"{args.output.stem}-data")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(threadName)s %(message)s")
    from config import get_config

    config = get_config().model_copy(deep=True)
    config.llm.backend = "mlx"
    config.llm.mlx_model_name = MODEL
    config.stt.provider = "local"
    runner = benchmark_audio if args.synthetic_audio else benchmark_prompts
    report = {
        "schema_version": 1,
        "environment": environment_metadata(),
        "llm_model": MODEL,
        "prompt_max_tokens": args.max_tokens,
        "config": {
            "stt_model": config.stt.model_name,
            "word_timestamps": config.stt.word_timestamps,
            "correction_mode": config.llm.correction_mode,
            "correction_base_batch_size": config.llm.correction_batch_size,
        },
        "prompt_set": "fixed-korean-correction-v1",
    }
    report["result"] = asyncio.run(runner(args, config))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    logger.info(f"벤치마크 결과 저장: {args.output}")


if __name__ == "__main__":
    main()
