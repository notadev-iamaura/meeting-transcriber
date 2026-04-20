#!/usr/bin/env python3
"""
한국어 Whisper MLX 종합 벤치마크 (Apple-to-Apple 비교)

전략:
    1. 모든 후보 모델을 동일한 4bit MLX로 양자화 (또는 사전 양자화 모델 사용)
    2. 동일한 한국어 테스트 셋(Zeroth Korean test, 30 샘플)으로 평가
    3. CER/WER/RTF/메모리/디스크 5개 지표로 비교
    4. 동일한 전사 파라미터 (language=ko, beam_size=greedy)

비교 대상:
    [한국어 fine-tune 모델 - 4bit 양자화]
    1. ghost613/whisper-large-v3-turbo-korean → 4bit
    2. o0dimplz0o/Whisper-Large-v3-turbo-STT-Zeroth-KO-v2 → 4bit
    3. seastar105/whisper-medium-ko-zeroth → 4bit
    4. jangmin/whisper-medium-ko-normalized-1273h → 4bit (1273h AI-Hub)
    5. byoussef/whisper-large-v2-AiHub → 4bit (AI-Hub)

    [일반 Whisper - 베이스라인]
    6. mlx-community/whisper-large-v3-turbo (fp16) - 베이스라인
    7. mlx-community/whisper-large-v3-turbo-q4 (4bit 사전) - 일반 4bit 비교
    8. mlx-community/whisper-medium-mlx (fp16) - medium 베이스라인
    9. mlx-community/whisper-medium-mlx-q4 (4bit 사전)

    [현재 시스템]
    10. youngouk/whisper-medium-komixv2-mlx (현재, MLX fp16)
"""

import gc
import json
import subprocess
import sys
import time
from pathlib import Path

import psutil

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MLX_EXAMPLES = Path.home() / "Projects" / "mlx-examples" / "whisper"
MODELS_DIR = Path.home() / "models"
RESULTS_PATH = PROJECT_ROOT / "scripts" / "whisper_comprehensive_results.json"

# === 비교 모델 정의 ===
MODELS = [
    # 그룹 1: 한국어 fine-tune → 4bit 직접 양자화 (메인 후보)
    {
        "id": "ghost613-turbo",
        "label": "ghost613 turbo-korean (4bit)",
        "group": "한국어 fine-tune (4bit)",
        "source": "ghost613/whisper-large-v3-turbo-korean",
        "path": str(MODELS_DIR / "turbo-korean-4bit"),
        "needs_convert": True,
    },
    {
        "id": "dimplz0o-turbo-v2",
        "label": "o0dimplz0o turbo-korean-v2 (4bit)",
        "group": "한국어 fine-tune (4bit)",
        "source": "o0dimplz0o/Whisper-Large-v3-turbo-STT-Zeroth-KO-v2",
        "path": str(MODELS_DIR / "dimplz0o-turbo-korean-v2-4bit"),
        "needs_convert": True,
    },
    {
        "id": "seastar-medium",
        "label": "seastar105 medium-ko-zeroth (4bit)",
        "group": "한국어 fine-tune (4bit)",
        "source": "seastar105/whisper-medium-ko-zeroth",
        "path": str(MODELS_DIR / "seastar-medium-ko-4bit"),
        "needs_convert": True,
    },
    {
        "id": "jangmin-medium",
        "label": "jangmin medium-ko-1273h (4bit)",
        "group": "한국어 fine-tune (4bit)",
        "source": "jangmin/whisper-medium-ko-normalized-1273h",
        "path": str(MODELS_DIR / "jangmin-medium-ko-4bit"),
        "needs_convert": True,
    },
    # 그룹 2: 일반 Whisper - 베이스라인 비교
    {
        "id": "general-turbo-fp16",
        "label": "whisper-large-v3-turbo (fp16, 일반)",
        "group": "일반 Whisper",
        "source": "mlx-community/whisper-large-v3-turbo",
        "path": "mlx-community/whisper-large-v3-turbo",
        "needs_convert": False,
    },
    {
        "id": "general-turbo-q4",
        "label": "whisper-large-v3-turbo-q4 (4bit, 일반)",
        "group": "일반 Whisper",
        "source": "mlx-community/whisper-large-v3-turbo-q4",
        "path": "mlx-community/whisper-large-v3-turbo-q4",
        "needs_convert": False,
    },
    {
        "id": "general-medium-fp16",
        "label": "whisper-medium-mlx (fp16, 일반)",
        "group": "일반 Whisper",
        "source": "mlx-community/whisper-medium-mlx",
        "path": "mlx-community/whisper-medium-mlx",
        "needs_convert": False,
    },
    {
        "id": "general-medium-q4",
        "label": "whisper-medium-mlx-q4 (4bit, 일반)",
        "group": "일반 Whisper",
        "source": "mlx-community/whisper-medium-mlx-q4",
        "path": "mlx-community/whisper-medium-mlx-q4",
        "needs_convert": False,
    },
    # 그룹 3: 현재 사용 중
    {
        "id": "current-komixv2",
        "label": "komixv2-mlx (현재, fp16)",
        "group": "현재 시스템",
        "source": "youngouk/whisper-medium-komixv2-mlx",
        "path": "youngouk/whisper-medium-komixv2-mlx",
        "needs_convert": False,
    },
]

NUM_SAMPLES = 30  # Zeroth Korean test set 샘플 수


def get_memory_gb() -> float:
    return psutil.virtual_memory().used / (1024**3)


def clear_memory():
    gc.collect()
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception:
        pass
    time.sleep(2)


def convert_model(source: str, output_path: str) -> tuple[bool, str]:
    """fp16 Whisper 모델을 4bit MLX로 양자화한다.

    Returns:
        (성공 여부, 에러 메시지)
    """
    out = Path(output_path)
    weights_link = out / "weights.safetensors"
    if out.exists() and weights_link.exists():
        return True, "이미 변환됨"

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
    print(f"  변환 중... ({source})")
    t0 = time.perf_counter()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    elapsed = time.perf_counter() - t0

    if result.returncode != 0:
        err = result.stderr[-500:].strip()
        return False, f"변환 실패: {err}"

    # 심볼릭 링크 생성
    model_file = out / "model.safetensors"
    if model_file.exists() and not weights_link.exists():
        weights_link.symlink_to("model.safetensors")

    return True, f"변환 완료 ({elapsed:.1f}초)"


def get_dir_size_mb(path) -> float:
    """디스크 크기(MB). HuggingFace 캐시 또는 로컬 디렉토리."""
    p = Path(path)
    if not p.exists():
        # HF 캐시 확인
        cache_root = Path.home() / ".cache" / "huggingface" / "hub"
        cache_name = "models--" + str(path).replace("/", "--")
        cache_path = cache_root / cache_name
        if cache_path.exists():
            p = cache_path
        else:
            return 0.0

    if p.is_file():
        return p.stat().st_size / (1024**2)

    # 심볼릭 링크 따라가는 실제 파일 크기 (HF 캐시는 symlink 구조)
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
            elif f.is_symlink():
                resolved = f.resolve()
                if resolved.is_file():
                    total += resolved.stat().st_size
        except (OSError, ValueError):
            continue
    return total / (1024**2)


def load_test_samples(num_samples: int):
    """Zeroth Korean test set에서 샘플을 로드한다."""
    from datasets import load_dataset

    print(f"\nZeroth Korean test set 로드 ({num_samples}개 샘플)...")
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
    total_dur = sum(s["duration"] for s in samples)
    print(f"  총 {len(samples)}개 샘플, 총 길이 {total_dur:.1f}초")
    return samples


def transcribe_samples(model_path: str, samples: list, verbose: bool = False) -> dict:
    """주어진 모델로 모든 샘플을 전사한다."""
    import mlx_whisper
    import numpy as np

    results = {
        "transcripts": [],
        "total_time_s": 0.0,
        "total_audio_s": 0.0,
        "errors": 0,
    }

    for s in samples:
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
            text = ""
            elapsed = 0.0
            results["errors"] += 1
            if verbose:
                print(f"    ❌ 샘플 {s['index']} 실패: {str(e)[:80]}")

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
        if verbose and (s["index"] % 5 == 0 or s["index"] == 1):
            print(f"    [{s['index']}/{len(samples)}] {elapsed:.1f}s | {text[:50]}")

    return results


def compute_metrics(transcripts: list) -> dict:
    """CER/WER 메트릭 계산."""
    from jiwer import cer, wer

    refs = [t["reference"] for t in transcripts if t["hypothesis"]]
    hyps = [t["hypothesis"] for t in transcripts if t["hypothesis"]]
    if not refs:
        return {"cer": 100.0, "wer": 100.0, "valid_samples": 0}
    return {
        "cer": round(cer(refs, hyps) * 100, 2),
        "wer": round(wer(refs, hyps) * 100, 2),
        "valid_samples": len(refs),
    }


def benchmark_model(model: dict, samples: list) -> dict:
    """단일 모델 벤치마크."""
    print(f"\n{'=' * 80}")
    print(f"  [{model['group']}] {model['label']}")
    print(f"  소스: {model['source']}")
    print(f"{'=' * 80}")

    result = {
        "id": model["id"],
        "label": model["label"],
        "group": model["group"],
        "source": model["source"],
        "path": model["path"],
        "needs_convert": model["needs_convert"],
    }

    # 1. 양자화
    if model["needs_convert"]:
        ok, msg = convert_model(model["source"], model["path"])
        print(f"  [1] 양자화: {msg}")
        if not ok:
            result["error"] = msg
            result["cer_percent"] = None
            result["wer_percent"] = None
            return result

    # 2. 디스크 크기
    size_mb = get_dir_size_mb(model["path"])
    result["disk_size_mb"] = round(size_mb, 1)
    print(f"  [2] 디스크: {size_mb:.0f}MB")

    # 3. 전사
    clear_memory()
    mem_before = get_memory_gb()
    print(f"  [3] 전사 시작 ({len(samples)}개 샘플, 메모리 {mem_before:.2f}GB)")

    transcribe_result = transcribe_samples(model["path"], samples, verbose=True)

    mem_after = get_memory_gb()
    result["mem_delta_gb"] = round(mem_after - mem_before, 2)
    result["mem_peak_gb"] = round(mem_after, 2)

    # 4. 메트릭
    metrics = compute_metrics(transcribe_result["transcripts"])
    result["cer_percent"] = metrics["cer"]
    result["wer_percent"] = metrics["wer"]
    result["valid_samples"] = metrics["valid_samples"]
    result["errors"] = transcribe_result["errors"]
    result["total_time_s"] = round(transcribe_result["total_time_s"], 2)
    result["total_audio_s"] = round(transcribe_result["total_audio_s"], 2)
    if transcribe_result["total_audio_s"] > 0:
        result["rtf"] = round(
            transcribe_result["total_time_s"] / transcribe_result["total_audio_s"], 3
        )
    else:
        result["rtf"] = 0.0
    result["transcripts"] = transcribe_result["transcripts"]

    print(
        f"  [4] 결과: CER={result['cer_percent']}% | WER={result['wer_percent']}% | "
        f"RTF={result['rtf']}x | 메모리={result['mem_delta_gb']}GB | 에러={result['errors']}"
    )

    clear_memory()
    return result


def print_comparison(results: list):
    """결과 비교 표 출력."""
    print(f"\n\n{'=' * 120}")
    print(f"  종합 비교 결과 (Zeroth Korean test set, {NUM_SAMPLES}개 샘플)")
    print(f"{'=' * 120}\n")

    # 그룹별 정렬
    groups = {}
    for r in results:
        g = r.get("group", "기타")
        groups.setdefault(g, []).append(r)

    print(
        f"{'그룹':<20} {'모델':<45} {'디스크':>10} {'메모리':>10} {'CER':>8} {'WER':>8} {'RTF':>10}"
    )
    print("-" * 120)

    for group_name, group_results in groups.items():
        for r in group_results:
            label = r["label"]
            if "error" in r and r.get("cer_percent") is None:
                print(
                    f"{group_name:<20} {label:<45} {'ERROR':>10} {'-':>10} {'-':>8} {'-':>8} {'-':>10}"
                )
                continue
            print(
                f"{group_name:<20} {label:<45} "
                f"{r['disk_size_mb']:>8.0f}MB "
                f"{r['mem_delta_gb']:>8.2f}GB "
                f"{r['cer_percent']:>6.2f}% "
                f"{r['wer_percent']:>6.2f}% "
                f"{r['rtf']:>8.3f}x"
            )
        print()

    # 최고 모델 (CER 기준)
    valid = [r for r in results if r.get("cer_percent") is not None]
    if valid:
        best = min(valid, key=lambda x: x["cer_percent"])
        print(f"\n🏆 최고 정확도: {best['label']}")
        print(
            f"   CER: {best['cer_percent']}% | WER: {best['wer_percent']}% | RTF: {best['rtf']}x"
        )
        print(f"   디스크: {best['disk_size_mb']}MB | 메모리: {best['mem_delta_gb']}GB")


def main():
    print("=" * 80)
    print("  한국어 Whisper MLX 종합 벤치마크 (Apple-to-Apple)")
    print("=" * 80)

    import platform

    sys_mem_gb = psutil.virtual_memory().total / (1024**3)
    print(f"\n시스템: {platform.processor()} | RAM: {sys_mem_gb:.0f}GB")
    print(f"모델 수: {len(MODELS)}개")
    print(f"샘플 수: {NUM_SAMPLES}개 (Zeroth Korean test set)")

    # 테스트 샘플 로드
    samples = load_test_samples(NUM_SAMPLES)

    # 각 모델 벤치마크
    results = []
    for i, model in enumerate(MODELS, 1):
        print(f"\n\n>>> [{i}/{len(MODELS)}] 진행 중")
        try:
            result = benchmark_model(model, samples)
            results.append(result)
            # 중간 저장
            with open(RESULTS_PATH, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"\n❌ {model['label']} 실패: {e}")
            import traceback

            traceback.print_exc()
            results.append(
                {
                    "id": model["id"],
                    "label": model["label"],
                    "group": model["group"],
                    "error": str(e),
                    "cer_percent": None,
                    "wer_percent": None,
                }
            )

    print_comparison(results)

    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n결과 저장: {RESULTS_PATH}")


if __name__ == "__main__":
    main()
