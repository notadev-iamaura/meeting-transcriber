#!/usr/bin/env python3
"""
한국어 Whisper MLX 모델 양자화 + 벤치마크 비교 스크립트

목적:
    1. 한국어 fine-tune된 fp16 Whisper 모델을 4bit MLX로 양자화
    2. Zeroth Korean test set으로 CER/WER 측정
    3. 원본 vs 양자화 vs 현재 모델 3-way 비교

비교 모델:
    A. youngouk/whisper-medium-komixv2-mlx (현재, fp16)
    B. ghost613/whisper-large-v3-turbo-korean (turbo, fp16)
    C. komixv2 → 4bit 양자화 (직접 변환)
    D. ghost613 turbo-korean → 4bit 양자화 (직접 변환)
"""

import gc
import json
import subprocess
import sys
import time
from pathlib import Path

import psutil

# === 경로 ===
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MLX_EXAMPLES = Path.home() / "Projects" / "mlx-examples" / "whisper"
MODELS_DIR = Path.home() / "models"
RESULTS_PATH = PROJECT_ROOT / "scripts" / "whisper_benchmark_results.json"

# === 비교 모델 ===
# 비교 전략:
#   - A: 현재 사용 모델 (komixv2, MLX fp16)
#   - B: 일반 Whisper-large-turbo (mlx-community, fp16) - 베이스라인
#   - C: 일반 Whisper-large-turbo q4 (mlx-community 사전 양자화) - 직접 양자화 비교용
#   - D: 한국어 fine-tune turbo → 4bit 양자화 (직접 변환, 우리의 목표)
MODELS = [
    {
        "label": "A. komixv2 (현재, MLX fp16)",
        "source": "youngouk/whisper-medium-komixv2-mlx",
        "path": "youngouk/whisper-medium-komixv2-mlx",
        "needs_convert": False,
    },
    {
        "label": "B. whisper-large-v3-turbo (mlx-community, fp16)",
        "source": "mlx-community/whisper-large-v3-turbo",
        "path": "mlx-community/whisper-large-v3-turbo",
        "needs_convert": False,
    },
    {
        "label": "C. whisper-large-v3-turbo-q4 (mlx-community, 4bit 사전)",
        "source": "mlx-community/whisper-large-v3-turbo-q4",
        "path": "mlx-community/whisper-large-v3-turbo-q4",
        "needs_convert": False,
    },
    {
        "label": "D. ghost613 turbo-korean → 4bit 직접 양자화",
        "source": "ghost613/whisper-large-v3-turbo-korean",
        "path": str(MODELS_DIR / "turbo-korean-4bit"),
        "needs_convert": True,
    },
]


def get_memory_gb() -> float:
    """현재 시스템 메모리 사용량(GB)을 반환한다."""
    return psutil.virtual_memory().used / (1024**3)


def clear_memory():
    """메모리를 정리한다."""
    gc.collect()
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception:
        pass
    time.sleep(2)


def convert_model(source: str, output_path: str) -> bool:
    """fp16 Whisper 모델을 4bit MLX로 양자화한다.

    Args:
        source: HuggingFace 모델 ID 또는 로컬 경로
        output_path: 변환 결과를 저장할 경로

    Returns:
        성공 여부
    """
    out = Path(output_path)
    weights_link = out / "weights.safetensors"
    if out.exists() and weights_link.exists():
        print(f"  이미 변환됨, 스킵: {output_path}")
        return True

    out.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(MLX_EXAMPLES / "convert.py"),
        "--torch-name-or-path",
        source,
        "--mlx-path",
        output_path,
        "-q",
        "--q-bits",
        "4",
        "--q-group-size",
        "64",
    ]
    print(f"  변환 명령: {' '.join(cmd)}")
    t0 = time.perf_counter()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.perf_counter() - t0

    if result.returncode != 0:
        print(f"  ❌ 변환 실패 ({elapsed:.1f}초)")
        print(f"  stderr: {result.stderr[-500:]}")
        return False

    # mlx-whisper 호환성: model.safetensors → weights.safetensors 심볼릭 링크
    model_file = out / "model.safetensors"
    if model_file.exists() and not weights_link.exists():
        weights_link.symlink_to("model.safetensors")
        print("  심볼릭 링크 생성: weights.safetensors → model.safetensors")

    print(f"  ✅ 변환 완료 ({elapsed:.1f}초)")
    return True


def get_dir_size_mb(path: Path) -> float:
    """디렉토리 크기(MB)를 반환한다."""
    if not path.exists():
        return 0.0
    if path.is_file():
        return path.stat().st_size / (1024**2)
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024**2)


def load_test_samples(num_samples: int = 5):
    """Zeroth Korean test set에서 샘플을 로드한다."""
    from datasets import load_dataset

    print(f"\nZeroth Korean test set 로드 ({num_samples}개)...")
    ds = load_dataset("kresnik/zeroth_korean", split="test", streaming=True)
    samples = []
    for i, s in enumerate(ds.take(num_samples)):
        samples.append(
            {
                "index": i + 1,
                "audio_array": s["audio"]["array"],
                "sample_rate": s["audio"]["sampling_rate"],
                "duration": len(s["audio"]["array"]) / s["audio"]["sampling_rate"],
                "reference": s["text"],
            }
        )
        print(f"  [{i + 1}] {samples[-1]['duration']:.1f}s | {s['text'][:60]}")
    return samples


def transcribe_samples(model_path: str, samples: list) -> dict:
    """주어진 모델로 모든 샘플을 전사하고 메트릭을 반환한다."""
    import mlx_whisper
    import numpy as np

    results = {
        "model_path": model_path,
        "transcripts": [],
        "total_time_s": 0.0,
        "total_audio_s": 0.0,
    }

    for s in samples:
        # numpy array를 직접 전달
        audio = np.array(s["audio_array"], dtype=np.float32)

        t0 = time.perf_counter()
        try:
            out = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=model_path,
                language="ko",
                verbose=False,
            )
            elapsed = time.perf_counter() - t0
            text = out.get("text", "").strip()
        except Exception as e:
            print(f"    ❌ 전사 실패 (샘플 {s['index']}): {e}")
            text = ""
            elapsed = 0.0

        results["transcripts"].append(
            {
                "index": s["index"],
                "reference": s["reference"],
                "hypothesis": text,
                "time_s": round(elapsed, 2),
                "audio_s": round(s["duration"], 2),
            }
        )
        results["total_time_s"] += elapsed
        results["total_audio_s"] += s["duration"]
        print(f"    [{s['index']}] {elapsed:.1f}s | {text[:60]}")

    return results


def compute_metrics(transcripts: list) -> dict:
    """CER/WER 메트릭을 계산한다."""
    from jiwer import cer, wer

    refs = [t["reference"] for t in transcripts]
    hyps = [t["hypothesis"] for t in transcripts]
    return {
        "cer": round(cer(refs, hyps) * 100, 2),  # %
        "wer": round(wer(refs, hyps) * 100, 2),  # %
        "samples": len(transcripts),
    }


def benchmark_model(model: dict, samples: list) -> dict:
    """단일 모델 벤치마크를 실행한다."""
    print(f"\n{'=' * 70}")
    print(f"  {model['label']}")
    print(f"  경로: {model['path']}")
    print(f"{'=' * 70}")

    result = {
        "label": model["label"],
        "path": model["path"],
        "needs_convert": model["needs_convert"],
    }

    # 1. 양자화 (필요 시)
    if model["needs_convert"]:
        print("\n[1] 양자화 변환...")
        if not convert_model(model["source"], model["path"]):
            result["error"] = "변환 실패"
            return result

    # 2. 디스크 크기
    size_mb = get_dir_size_mb(Path(model["path"]))
    result["disk_size_mb"] = round(size_mb, 1)
    print(f"\n[2] 디스크 크기: {size_mb:.1f}MB")

    # 3. 전사 + 메모리 측정
    print("\n[3] 전사 시작...")
    clear_memory()
    mem_before = get_memory_gb()
    print(f"    메모리 (전): {mem_before:.2f}GB")

    transcribe_result = transcribe_samples(model["path"], samples)

    mem_after = get_memory_gb()
    result["mem_delta_gb"] = round(mem_after - mem_before, 2)
    result["mem_peak_gb"] = round(mem_after, 2)
    print(f"    메모리 (후): {mem_after:.2f}GB (+{result['mem_delta_gb']:.2f}GB)")

    # 4. 메트릭
    metrics = compute_metrics(transcribe_result["transcripts"])
    result["cer_percent"] = metrics["cer"]
    result["wer_percent"] = metrics["wer"]
    result["total_time_s"] = round(transcribe_result["total_time_s"], 2)
    result["total_audio_s"] = round(transcribe_result["total_audio_s"], 2)
    result["rtf"] = round(
        transcribe_result["total_time_s"] / transcribe_result["total_audio_s"], 3
    )
    result["transcripts"] = transcribe_result["transcripts"]

    print("\n[4] 결과:")
    print(f"    CER: {result['cer_percent']}%")
    print(f"    WER: {result['wer_percent']}%")
    print(f"    총 전사 시간: {result['total_time_s']}초")
    print(f"    오디오 길이: {result['total_audio_s']}초")
    print(f"    RTF (실시간 배수): {result['rtf']}x")

    clear_memory()
    return result


def print_comparison(results: list):
    """결과 비교 표를 출력한다."""
    print(f"\n\n{'=' * 100}")
    print("  최종 비교 결과 (Zeroth Korean test set)")
    print(f"{'=' * 100}\n")

    header = f"{'모델':<40} {'디스크':>10} {'메모리':>10} {'CER':>8} {'WER':>8} {'RTF':>8}"
    print(header)
    print("-" * 100)

    for r in results:
        if "error" in r:
            print(f"{r['label']:<40} {'ERROR':>10} {'-':>10} {'-':>8} {'-':>8} {'-':>8}")
            continue
        print(
            f"{r['label']:<40} "
            f"{r['disk_size_mb']:>8.0f}MB "
            f"{r['mem_delta_gb']:>8.2f}GB "
            f"{r['cer_percent']:>6.2f}% "
            f"{r['wer_percent']:>6.2f}% "
            f"{r['rtf']:>6.3f}x"
        )

    print()
    print("CER (Character Error Rate): 낮을수록 좋음")
    print("WER (Word Error Rate): 낮을수록 좋음")
    print("RTF (Real-Time Factor): 낮을수록 빠름 (1.0 미만 = 실시간보다 빠름)")


def main():
    print("=" * 70)
    print("  한국어 Whisper MLX 양자화 + 벤치마크")
    print("=" * 70)

    # 시스템 정보
    import platform

    print(
        f"\n시스템: {platform.processor()} | RAM: {psutil.virtual_memory().total / (1024**3):.0f}GB"
    )

    # 테스트 샘플 로드
    samples = load_test_samples(num_samples=5)

    # 모델별 벤치마크
    results = []
    for model in MODELS:
        try:
            result = benchmark_model(model, samples)
            results.append(result)
        except Exception as e:
            print(f"\n❌ {model['label']} 실패: {e}")
            import traceback

            traceback.print_exc()
            results.append(
                {
                    "label": model["label"],
                    "error": str(e),
                }
            )

    # 비교 표 출력
    print_comparison(results)

    # JSON 저장
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n결과 저장: {RESULTS_PATH}")


if __name__ == "__main__":
    main()
