#!/usr/bin/env python3
"""
설정 검증 테스트: 미전사 녹음 N건에 현재 설정(komixv2 + VAD=OFF)을 적용하여
순도/커버리지/환각률을 측정한다.

사용법:
    source .venv/bin/activate
    python scripts/validate_settings.py
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import get_config
from core.model_manager import ModelLoadManager
from steps.hallucination_filter import filter_hallucinations
from steps.transcriber import Transcriber
from steps.vad_detector import VoiceActivityDetector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("validate_settings")

# 테스트 대상 (short / medium / long)
TEST_MEETINGS = [
    "meeting_20260413_113050",  # 144s (~2.4분)
    "meeting_20260410_121253",  # 396s (~6.6분)
    "meeting_20260413_094519",  # 844s (~14분)
]

NATURAL_CPS_MAX = 8.0  # 한국어 자연 발화 최대 글자/초


def _get_audio_duration(wav_path: Path) -> float:
    import soundfile as sf

    return sf.info(str(wav_path)).duration


async def run_test(
    config: Any,
    model_manager: ModelLoadManager,
    meeting_id: str,
) -> dict[str, Any]:
    """단일 녹음에 대해 전사 + 환각 필터를 실행하고 메트릭을 수집한다."""
    wav_path = config.paths.resolved_audio_input_dir / f"{meeting_id}.wav"
    if not wav_path.exists():
        return {"meeting_id": meeting_id, "error": f"파일 없음: {wav_path}"}

    audio_duration = _get_audio_duration(wav_path)
    logger.info(f"{'=' * 60}")
    logger.info(f"  {meeting_id}  ({audio_duration:.0f}초)")
    logger.info(f"{'=' * 60}")

    # VAD (config 에 따라 자동 on/off)
    vad_clip_timestamps: list[float] | None = None
    vad_config = getattr(config, "vad", None)
    if vad_config and getattr(vad_config, "enabled", False):
        try:
            vad = VoiceActivityDetector(config)
            vad_result = await vad.detect(wav_path)
            if vad_result:
                vad_clip_timestamps = vad_result.clip_timestamps
                logger.info(f"  VAD ON: {vad_result.num_segments}개 음성 구간")
        except Exception as e:
            logger.warning(f"  VAD 실패: {e}")

    # 전사
    t0 = time.perf_counter()
    transcriber = Transcriber(config, model_manager)
    transcript = await transcriber.transcribe(wav_path, vad_clip_timestamps=vad_clip_timestamps)
    elapsed = time.perf_counter() - t0

    raw_segs = len(transcript.segments)
    sum(len(s.text.strip()) for s in transcript.segments)

    # 환각 필터
    filtered_segs, removed = filter_hallucinations(transcript.segments, config)
    filtered_chars = sum(len(getattr(s, "text", "").strip()) for s in filtered_segs)

    # 커버리지
    if filtered_segs:
        covered_sec = sum(getattr(s, "end", 0) - getattr(s, "start", 0) for s in filtered_segs)
    else:
        covered_sec = 0.0
    coverage_pct = (covered_sec / audio_duration * 100) if audio_duration > 0 else 0

    # 순도 (글자/초)
    cps = filtered_chars / covered_sec if covered_sec > 0 else 0
    purity = min(1.0, NATURAL_CPS_MAX / cps) if cps > NATURAL_CPS_MAX else 1.0

    # 환각 유형
    reasons: dict[str, int] = {}
    for r in removed:
        key = r["reason"].split("(")[0].split("=")[0].strip()
        reasons[key] = reasons.get(key, 0) + 1

    result = {
        "meeting_id": meeting_id,
        "audio_sec": round(audio_duration, 1),
        "elapsed_sec": round(elapsed, 1),
        "rtf": round(elapsed / audio_duration, 2),
        "raw_segs": raw_segs,
        "hallucinations": len(removed),
        "hall_rate_pct": round(len(removed) / raw_segs * 100, 1) if raw_segs > 0 else 0,
        "filtered_segs": len(filtered_segs),
        "filtered_chars": filtered_chars,
        "coverage_pct": round(coverage_pct, 1),
        "cps": round(cps, 1),
        "purity_pct": round(purity * 100, 0),
        "effective_chars": int(filtered_chars * purity),
        "hallucination_reasons": reasons,
    }

    logger.info(
        f"  결과: {raw_segs}seg → -{len(removed)}환각 → {len(filtered_segs)}seg | "
        f"커버리지 {coverage_pct:.1f}% | 순도 {purity * 100:.0f}% | "
        f"{filtered_chars}자 | {elapsed:.1f}초 (RTF={elapsed / audio_duration:.2f}x)"
    )

    del transcript, transcriber
    gc.collect()
    return result


async def main() -> None:
    config = get_config()
    model_manager = ModelLoadManager()
    results: list[dict[str, Any]] = []

    logger.info(f"설정 검증 테스트: {len(TEST_MEETINGS)}건")
    logger.info(f"  모델: {config.stt.model_name}")
    logger.info(f"  VAD: {'ON' if config.vad.enabled else 'OFF'}")
    logger.info(f"  환각필터: {'ON' if config.hallucination_filter.enabled else 'OFF'}")

    total_start = time.perf_counter()
    for mid in TEST_MEETINGS:
        try:
            result = await run_test(config, model_manager, mid)
            results.append(result)
        except Exception as e:
            logger.error(f"  {mid} 실패: {e}")
            results.append({"meeting_id": mid, "error": str(e)})

    total_elapsed = time.perf_counter() - total_start

    # 결과 저장
    out = PROJECT_ROOT / "validate_settings_results.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(
            {"total_elapsed_sec": round(total_elapsed, 1), "cases": results},
            f,
            ensure_ascii=False,
            indent=2,
        )

    # 비교표
    print(f"\n{'=' * 95}")
    print(
        f"  설정 검증 결과  |  모델: komixv2 (fp16) + VAD=OFF  |  총 소요: {total_elapsed:.0f}초"
    )
    print(f"{'=' * 95}")
    print(
        f"{'회의ID':>30s} {'길이':>6s} {'세그':>5s} {'환각':>4s} {'필터후':>5s} {'글자수':>6s} {'커버리지':>8s} {'순도':>5s} {'RTF':>5s}"
    )
    print("-" * 95)
    for r in results:
        if "error" in r:
            print(f"{r['meeting_id']:>30s}  ERROR: {r['error'][:50]}")
            continue
        print(
            f"{r['meeting_id']:>30s} {r['audio_sec']:>5.0f}s "
            f"{r['raw_segs']:>5d} {r['hallucinations']:>4d} "
            f"{r['filtered_segs']:>5d} {r['filtered_chars']:>6d} "
            f"{r['coverage_pct']:>6.1f}% {r['purity_pct']:>4.0f}% "
            f"{r['rtf']:>5.2f}x"
        )
    print(f"{'=' * 95}")


if __name__ == "__main__":
    asyncio.run(main())
