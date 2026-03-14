#!/usr/bin/env python3
"""
STT 개선 A/B 비교 벤치마크

기능 OFF (baseline) vs 기능 ON (VAD + initial_prompt + 숫자 정규화)의
CER/WER 차이를 Zeroth-Korean 데이터셋으로 정량 측정한다.

사용법:
    # 기본 (10개 샘플, 빠른 비교)
    python scripts/benchmark_ab_test.py

    # 샘플 수 지정
    python scripts/benchmark_ab_test.py --samples 30

    # 결과 저장
    python scripts/benchmark_ab_test.py --output data/ab_result.json
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import jiwer

# 프로젝트 루트를 PYTHONPATH에 추가
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)


# ============================================================
# 데이터 클래스
# ============================================================


@dataclass
class SampleResult:
    """개별 샘플 전사 결과"""

    sample_id: str
    reference: str          # 정답 텍스트
    hypothesis: str         # 전사 결과
    cer: float              # 샘플별 CER
    audio_duration: float   # 오디오 길이 (초)
    processing_time: float  # 처리 시간 (초)


@dataclass
class ABResult:
    """A/B 테스트 결과"""

    mode: str               # "baseline" 또는 "enhanced"
    total_samples: int
    cer: float
    wer: float
    avg_time: float
    total_audio: float
    rtf: float
    samples: list[SampleResult] = field(default_factory=list)


# ============================================================
# 텍스트 정규화 (벤치마크용)
# ============================================================


def normalize_korean(text: str) -> str:
    """한국어 텍스트 정규화 (CER/WER 비교 전 적용)"""
    text = unicodedata.normalize("NFC", text)
    text = text.lower()
    # 한글, 영문, 숫자, 공백만 유지
    text = re.sub(r"[^\w\s가-힣ㄱ-ㅎㅏ-ㅣa-z0-9]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ============================================================
# 데이터셋 준비
# ============================================================


def prepare_dataset(num_samples: int, cache_dir: Path) -> list[dict]:
    """Zeroth-Korean 테스트 데이터셋을 준비한다."""
    import soundfile as sf
    from datasets import load_dataset

    logger.info(f"Zeroth-Korean 데이터셋 로드 중 (최대 {num_samples}개)...")

    audio_cache = cache_dir / "zeroth_audio"
    audio_cache.mkdir(parents=True, exist_ok=True)

    ds = load_dataset(
        "kresnik/zeroth_korean",
        split="test",
        trust_remote_code=True,
    )

    samples = []
    for i, item in enumerate(ds):
        if i >= num_samples:
            break

        audio = item["audio"]
        audio_array = audio["array"]
        sr = audio["sampling_rate"]

        audio_path = audio_cache / f"zeroth_{i:04d}.wav"
        if not audio_path.exists():
            sf.write(str(audio_path), audio_array, sr)

        duration = len(audio_array) / sr
        samples.append({
            "id": f"zeroth_{i:04d}",
            "audio_path": audio_path,
            "reference": item["text"].strip(),
            "duration": duration,
        })

    total_dur = sum(s["duration"] for s in samples)
    logger.info(f"데이터셋 준비 완료: {len(samples)}개 샘플, 총 {total_dur:.1f}초")
    return samples


# ============================================================
# STT 프로바이더
# ============================================================


class BaselineProvider:
    """기능 OFF: 기존 mlx-whisper 직접 호출 (VAD/initial_prompt 없음)"""

    def __init__(self, model_name: str, language: str = "ko"):
        self.model_name = model_name
        self.language = language
        self._whisper = None

    def _load(self) -> None:
        if self._whisper is None:
            import mlx_whisper
            self._whisper = mlx_whisper
            logger.info("[Baseline] mlx-whisper 로드 완료")

    def transcribe(self, audio_path: Path) -> str:
        """기능 없이 순수 전사"""
        self._load()
        try:
            result = self._whisper.transcribe(
                str(audio_path),
                path_or_hf_repo=self.model_name,
                language=self.language,
                word_timestamps=False,
                beam_size=5,
            )
        except NotImplementedError:
            result = self._whisper.transcribe(
                str(audio_path),
                path_or_hf_repo=self.model_name,
                language=self.language,
                word_timestamps=False,
            )
        return result.get("text", "").strip()


class EnhancedProvider:
    """기능 ON: VAD + initial_prompt + 숫자 정규화 적용"""

    def __init__(
        self,
        model_name: str,
        language: str = "ko",
        initial_prompt: str | None = None,
        vad_enabled: bool = True,
    ):
        self.model_name = model_name
        self.language = language
        self.initial_prompt = initial_prompt
        self.vad_enabled = vad_enabled
        self._whisper = None
        self._vad = None

    def _load(self) -> None:
        if self._whisper is None:
            import mlx_whisper
            self._whisper = mlx_whisper
            logger.info("[Enhanced] mlx-whisper 로드 완료")

        if self.vad_enabled and self._vad is None:
            self._load_vad()

    def _load_vad(self) -> None:
        """Silero VAD를 직접 로드 (VoiceActivityDetector 우회, 동기 실행용)"""
        try:
            import torch
            from silero_vad import get_speech_timestamps, load_silero_vad

            model = load_silero_vad()
            model.to(torch.device("cpu"))
            self._vad = {
                "model": model,
                "get_timestamps": get_speech_timestamps,
            }
            logger.info("[Enhanced] Silero VAD 로드 완료 (CPU)")
        except ImportError:
            logger.warning("[Enhanced] silero-vad 미설치, VAD 비활성")
            self.vad_enabled = False

    def _run_vad(self, audio_path: Path) -> list[float] | None:
        """VAD 실행 → clip_timestamps 반환"""
        if not self.vad_enabled or self._vad is None:
            return None

        import torch
        import torchaudio

        waveform, sample_rate = torchaudio.load(str(audio_path))
        if waveform.shape[0] > 1:
            waveform = waveform.mean(dim=0, keepdim=True)
        if sample_rate != 16000:
            resampler = torchaudio.transforms.Resample(
                orig_freq=sample_rate, new_freq=16000
            )
            waveform = resampler(waveform)
            sample_rate = 16000

        duration = float(waveform.shape[1] / sample_rate)

        segments = self._vad["get_timestamps"](
            waveform.squeeze(),
            self._vad["model"],
            threshold=0.5,
            min_speech_duration_ms=250,
            min_silence_duration_ms=100,
            speech_pad_ms=30,
            return_seconds=True,
        )

        if not segments:
            return None

        # clip_timestamps 변환
        clip_timestamps: list[float] = []
        for seg in segments:
            start = float(seg["start"])
            end = float(seg["end"])
            if start >= end:
                continue
            clip_timestamps.append(start)
            clip_timestamps.append(end)

        # 마지막 end == duration 근접 시 조정 (mlx-whisper 무한루프 방지)
        if clip_timestamps and abs(clip_timestamps[-1] - duration) < 0.15:
            adjusted = clip_timestamps[-1] - 0.1
            if len(clip_timestamps) >= 2 and adjusted > clip_timestamps[-2]:
                clip_timestamps[-1] = adjusted

        return clip_timestamps if clip_timestamps else None

    def transcribe(self, audio_path: Path) -> str:
        """VAD + initial_prompt 적용 전사"""
        self._load()

        # VAD 실행
        clip_timestamps = self._run_vad(audio_path)

        # 공통 kwargs 구성
        kwargs: dict = {
            "path_or_hf_repo": self.model_name,
            "language": self.language,
            "word_timestamps": False,
        }
        if self.initial_prompt is not None:
            kwargs["initial_prompt"] = self.initial_prompt
        if clip_timestamps is not None:
            kwargs["clip_timestamps"] = clip_timestamps

        # beam search → greedy 폴백
        try:
            result = self._whisper.transcribe(
                str(audio_path), beam_size=5, **kwargs,
            )
        except NotImplementedError:
            result = self._whisper.transcribe(
                str(audio_path), **kwargs,
            )

        text = result.get("text", "").strip()

        # 숫자 정규화 적용
        try:
            from steps.number_normalizer import normalize_numbers
            text = normalize_numbers(text, level=1)
        except Exception as e:
            logger.debug(f"숫자 정규화 실패 (원본 유지): {e}")

        return text


# ============================================================
# 벤치마크 실행
# ============================================================


def run_ab_test(
    provider: BaselineProvider | EnhancedProvider,
    mode: str,
    samples: list[dict],
) -> ABResult:
    """A 또는 B 벤치마크를 실행한다."""
    results: list[SampleResult] = []
    total_audio = 0.0
    total_time = 0.0

    logger.info(f"[{mode}] 벤치마크 시작: {len(samples)}개 샘플")

    for i, sample in enumerate(samples):
        try:
            start = time.time()
            hypothesis = provider.transcribe(sample["audio_path"])
            elapsed = time.time() - start

            # 샘플별 CER
            ref_n = normalize_korean(sample["reference"])
            hyp_n = normalize_korean(hypothesis)
            sample_cer = jiwer.cer(ref_n, hyp_n) if ref_n else 0.0

            results.append(SampleResult(
                sample_id=sample["id"],
                reference=sample["reference"],
                hypothesis=hypothesis,
                cer=sample_cer,
                audio_duration=sample["duration"],
                processing_time=elapsed,
            ))

            total_audio += sample["duration"]
            total_time += elapsed

            # 진행률
            if (i + 1) % 5 == 0 or i == len(samples) - 1:
                logger.info(
                    f"  [{mode}] {i + 1}/{len(samples)} 완료 "
                    f"(누적 {total_time:.1f}초)"
                )

        except Exception as e:
            logger.warning(f"  [{mode}] 샘플 {sample['id']} 실패: {e}")

    if not results:
        return ABResult(
            mode=mode, total_samples=0,
            cer=1.0, wer=1.0, avg_time=0, total_audio=0, rtf=0,
        )

    # 전체 CER/WER 계산
    refs = [normalize_korean(r.reference) for r in results]
    hyps = [normalize_korean(r.hypothesis) for r in results]
    cer = jiwer.cer(refs, hyps)
    wer = jiwer.wer(refs, hyps)

    return ABResult(
        mode=mode,
        total_samples=len(results),
        cer=cer,
        wer=wer,
        avg_time=total_time / len(results),
        total_audio=total_audio,
        rtf=total_time / total_audio if total_audio > 0 else 0,
        samples=results,
    )


# ============================================================
# 리포트
# ============================================================


def print_report(baseline: ABResult, enhanced: ABResult) -> None:
    """A/B 비교 리포트 출력"""
    cer_diff = enhanced.cer - baseline.cer
    wer_diff = enhanced.wer - baseline.wer
    cer_improved = cer_diff < 0
    wer_improved = wer_diff < 0

    print("\n" + "=" * 75)
    print("  STT 개선 A/B 비교 벤치마크 결과")
    print("  데이터셋: Zeroth-Korean (test split)")
    print("=" * 75)

    # 요약 테이블
    header = f"{'모드':<20} {'CER':>8} {'WER':>8} {'RTF':>8} {'샘플':>6}"
    print(f"\n{header}")
    print("-" * 55)
    print(
        f"{'A: Baseline (OFF)':<20} "
        f"{baseline.cer:>7.2%} {baseline.wer:>7.2%} "
        f"{baseline.rtf:>7.2f}x {baseline.total_samples:>6}"
    )
    print(
        f"{'B: Enhanced (ON)':<20} "
        f"{enhanced.cer:>7.2%} {enhanced.wer:>7.2%} "
        f"{enhanced.rtf:>7.2f}x {enhanced.total_samples:>6}"
    )
    print("-" * 55)

    # 차이 분석
    cer_arrow = "↓ 개선" if cer_improved else "↑ 악화"
    wer_arrow = "↓ 개선" if wer_improved else "↑ 악화"

    print(f"\n  CER 변화: {baseline.cer:.2%} → {enhanced.cer:.2%} ({cer_diff:+.2%}p {cer_arrow})")
    print(f"  WER 변화: {baseline.wer:.2%} → {enhanced.wer:.2%} ({wer_diff:+.2%}p {wer_arrow})")

    if cer_improved:
        cer_reduction = abs(cer_diff) / baseline.cer * 100 if baseline.cer > 0 else 0
        print(f"  CER 상대적 감소율: {cer_reduction:.1f}%")

    # 샘플별 비교 (CER 변화가 큰 순서)
    if baseline.samples and enhanced.samples:
        diffs = []
        for b, e in zip(baseline.samples, enhanced.samples):
            diffs.append({
                "id": b.sample_id,
                "cer_diff": e.cer - b.cer,
                "baseline_cer": b.cer,
                "enhanced_cer": e.cer,
                "ref": b.reference,
                "baseline_hyp": b.hypothesis,
                "enhanced_hyp": e.hypothesis,
            })

        # 개선된 샘플
        improved = sorted([d for d in diffs if d["cer_diff"] < -0.01], key=lambda x: x["cer_diff"])
        degraded = sorted([d for d in diffs if d["cer_diff"] > 0.01], key=lambda x: x["cer_diff"], reverse=True)

        if improved:
            print(f"\n  개선된 샘플 Top 5:")
            for d in improved[:5]:
                print(f"    {d['id']}: CER {d['baseline_cer']:.2%} → {d['enhanced_cer']:.2%} ({d['cer_diff']:+.2%}p)")
                print(f"      정답:     {d['ref'][:70]}")
                print(f"      기존:     {d['baseline_hyp'][:70]}")
                print(f"      개선:     {d['enhanced_hyp'][:70]}")

        if degraded:
            print(f"\n  악화된 샘플 Top 5:")
            for d in degraded[:5]:
                print(f"    {d['id']}: CER {d['baseline_cer']:.2%} → {d['enhanced_cer']:.2%} ({d['cer_diff']:+.2%}p)")
                print(f"      정답:     {d['ref'][:70]}")
                print(f"      기존:     {d['baseline_hyp'][:70]}")
                print(f"      개선:     {d['enhanced_hyp'][:70]}")

        print(f"\n  요약: 개선 {len(improved)}건 / 악화 {len(degraded)}건 / 동일 {len(diffs) - len(improved) - len(degraded)}건")

    print("\n" + "=" * 75)


def save_results(baseline: ABResult, enhanced: ABResult, output_path: Path) -> None:
    """결과를 JSON으로 저장"""
    data = {
        "benchmark_info": {
            "type": "A/B comparison",
            "dataset": "Zeroth-Korean (test split)",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "summary": {
            "baseline_cer": round(baseline.cer, 6),
            "enhanced_cer": round(enhanced.cer, 6),
            "cer_diff": round(enhanced.cer - baseline.cer, 6),
            "baseline_wer": round(baseline.wer, 6),
            "enhanced_wer": round(enhanced.wer, 6),
            "wer_diff": round(enhanced.wer - baseline.wer, 6),
        },
        "baseline": {
            "cer": round(baseline.cer, 6),
            "wer": round(baseline.wer, 6),
            "samples": [
                {
                    "id": s.sample_id,
                    "reference": s.reference,
                    "hypothesis": s.hypothesis,
                    "cer": round(s.cer, 6),
                }
                for s in baseline.samples
            ],
        },
        "enhanced": {
            "cer": round(enhanced.cer, 6),
            "wer": round(enhanced.wer, 6),
            "samples": [
                {
                    "id": s.sample_id,
                    "reference": s.reference,
                    "hypothesis": s.hypothesis,
                    "cer": round(s.cer, 6),
                }
                for s in enhanced.samples
            ],
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"결과 저장: {output_path}")


# ============================================================
# 메인
# ============================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="STT 개선 A/B 비교 벤치마크",
    )
    parser.add_argument(
        "--samples", type=int, default=10,
        help="벤치마크 샘플 수 (기본: 10)",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="결과 JSON 저장 경로",
    )
    parser.add_argument(
        "--initial-prompt", type=str,
        default="회의, 안녕하세요, 감사합니다, 네, 말씀, 진행, 공유, 확인, 검토, 일정, 프로젝트",
        help="Enhanced 모드의 initial_prompt",
    )
    parser.add_argument(
        "--no-vad", action="store_true",
        help="Enhanced 모드에서 VAD 비활성화 (initial_prompt만 테스트)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    MODEL_NAME = "youngouk/whisper-medium-komixv2-mlx"
    cache_dir = PROJECT_ROOT / "data" / "benchmark_cache"

    # 데이터셋 준비
    samples = prepare_dataset(args.samples, cache_dir)
    if not samples:
        logger.error("데이터셋 로드 실패")
        sys.exit(1)

    # ── A: Baseline (기능 OFF) ──
    logger.info("=" * 60)
    logger.info("A 그룹: Baseline (기능 OFF)")
    logger.info("=" * 60)
    baseline_provider = BaselineProvider(model_name=MODEL_NAME)
    baseline_result = run_ab_test(baseline_provider, "Baseline", samples)

    # ── B: Enhanced (기능 ON) ──
    logger.info("=" * 60)
    logger.info("B 그룹: Enhanced (VAD + initial_prompt + 숫자 정규화)")
    logger.info("=" * 60)
    enhanced_provider = EnhancedProvider(
        model_name=MODEL_NAME,
        initial_prompt=args.initial_prompt,
        vad_enabled=not args.no_vad,
    )
    enhanced_result = run_ab_test(enhanced_provider, "Enhanced", samples)

    # ── 리포트 ──
    print_report(baseline_result, enhanced_result)

    output_path = Path(args.output) if args.output else (
        PROJECT_ROOT / "data" / "ab_test_results.json"
    )
    save_results(baseline_result, enhanced_result, output_path)


if __name__ == "__main__":
    main()
