#!/usr/bin/env python3
"""
Top 2 한국어 Whisper 모델 정밀 메모리 측정

비교 모델:
    - seastar105/whisper-medium-ko-zeroth (medium, 4bit 양자화)
    - ghost613/whisper-large-v3-turbo-korean (turbo, 4bit 양자화)

측정 항목:
    1. 프로세스 RSS (해당 Python 프로세스만)
    2. MLX Metal GPU 피크 메모리 (실제 GPU 사용량)
    3. 시스템 전체 메모리 (참고용)
    4. CER/WER (Zeroth Korean test 30 샘플)
    5. RTF, 디스크 크기

각 모델을 별도 프로세스에서 실행해서 측정 노이즈를 최소화한다.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MLX_EXAMPLES = Path.home() / "Projects" / "mlx-examples" / "whisper"
MODELS_DIR = Path.home() / "models"
RESULTS_PATH = PROJECT_ROOT / "scripts" / "whisper_top2_results.json"

MODELS = [
    {
        "id": "current-komixv2",
        "label": "komixv2-mlx (현재, fp16)",
        "source": "youngouk/whisper-medium-komixv2-mlx",
        "path": "youngouk/whisper-medium-komixv2-mlx",
        "base": "medium-fp16",
    },
    {
        "id": "seastar-medium",
        "label": "seastar105 medium-ko-zeroth (4bit)",
        "source": "seastar105/whisper-medium-ko-zeroth",
        "path": str(MODELS_DIR / "seastar-medium-ko-4bit"),
        "base": "medium",
    },
    {
        "id": "ghost613-turbo",
        "label": "ghost613 turbo-korean (4bit)",
        "source": "ghost613/whisper-large-v3-turbo-korean",
        "path": str(MODELS_DIR / "turbo-korean-4bit"),
        "base": "turbo",
    },
]

NUM_SAMPLES = 30


def run_single_benchmark(model: dict) -> dict:
    """별도 Python 프로세스에서 단일 모델 벤치마크 실행 (메모리 격리)."""
    script = f"""
import os, sys, gc, time, json
import psutil
import mlx.core as mx
import mlx_whisper
import numpy as np
from datasets import load_dataset
from jiwer import cer, wer

proc = psutil.Process(os.getpid())

# MLX 메모리 카운터 리셋
try:
    mx.reset_peak_memory()
except Exception:
    pass

# 시작 시점 메모리
gc.collect()
mem_baseline_rss = proc.memory_info().rss / (1024**3)
mem_baseline_sys = psutil.virtual_memory().used / (1024**3)

# 샘플 로드
ds = load_dataset('kresnik/zeroth_korean', split='test', streaming=True)
samples = list(ds.take({NUM_SAMPLES}))

# 모델 로드
t_load = time.perf_counter()
# 워밍업 (모델 다운로드/로드)
warmup_audio = np.array(samples[0]['audio']['array'], dtype=np.float32)
result = mlx_whisper.transcribe(
    warmup_audio,
    path_or_hf_repo='{model["path"]}',
    language='ko',
    verbose=False,
)
t_load = time.perf_counter() - t_load

mem_after_load_rss = proc.memory_info().rss / (1024**3)
mem_after_load_sys = psutil.virtual_memory().used / (1024**3)
mlx_after_load = mx.get_peak_memory() / (1024**3) if hasattr(mx, 'get_peak_memory') else None

# 전사 실행 (워밍업 제외, 30개 - 1)
transcripts = [{{
    'index': 1,
    'reference': samples[0]['text'],
    'hypothesis': result.get('text', '').strip(),
    'time_s': 0,
}}]

total_time = 0
total_audio = samples[0]['audio']['array'].shape[0] / samples[0]['audio']['sampling_rate']

# MLX 피크 리셋 (워밍업 후)
try:
    mx.reset_peak_memory()
except Exception:
    pass

for i, s in enumerate(samples[1:], start=2):
    audio = np.array(s['audio']['array'], dtype=np.float32)
    audio_dur = len(audio) / s['audio']['sampling_rate']
    total_audio += audio_dur

    t0 = time.perf_counter()
    out = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo='{model["path"]}',
        language='ko',
        verbose=False,
    )
    elapsed = time.perf_counter() - t0
    total_time += elapsed

    transcripts.append({{
        'index': i,
        'reference': s['text'],
        'hypothesis': out.get('text', '').strip(),
        'time_s': round(elapsed, 2),
    }})

# 피크 메모리 측정
mem_peak_rss = proc.memory_info().rss / (1024**3)
mem_peak_sys = psutil.virtual_memory().used / (1024**3)
mlx_peak = mx.get_peak_memory() / (1024**3) if hasattr(mx, 'get_peak_memory') else None

# 메트릭 계산
refs = [t['reference'] for t in transcripts]
hyps = [t['hypothesis'] for t in transcripts]
metrics_cer = cer(refs, hyps) * 100
metrics_wer = wer(refs, hyps) * 100

result_data = {{
    'id': '{model["id"]}',
    'label': '{model["label"]}',
    'source': '{model["source"]}',
    'path': '{model["path"]}',
    'base': '{model["base"]}',
    # 메모리 (정밀)
    'mem_baseline_rss_gb': round(mem_baseline_rss, 3),
    'mem_after_load_rss_gb': round(mem_after_load_rss, 3),
    'mem_peak_rss_gb': round(mem_peak_rss, 3),
    'mem_load_delta_rss_gb': round(mem_after_load_rss - mem_baseline_rss, 3),
    'mem_inference_delta_rss_gb': round(mem_peak_rss - mem_after_load_rss, 3),
    # MLX GPU
    'mlx_after_load_gb': round(mlx_after_load, 3) if mlx_after_load else None,
    'mlx_peak_gb': round(mlx_peak, 3) if mlx_peak else None,
    # 시스템 (참고)
    'mem_baseline_sys_gb': round(mem_baseline_sys, 3),
    'mem_peak_sys_gb': round(mem_peak_sys, 3),
    # 정확도
    'cer_percent': round(metrics_cer, 2),
    'wer_percent': round(metrics_wer, 2),
    # 시간
    'load_time_s': round(t_load, 2),
    'total_time_s': round(total_time, 2),
    'total_audio_s': round(total_audio, 2),
    'rtf': round(total_time / total_audio, 3) if total_audio > 0 else 0,
    'num_samples': len(transcripts),
}}

print('===RESULT_START===')
print(json.dumps(result_data, ensure_ascii=False))
print('===RESULT_END===')
"""

    print(f"\n{'=' * 80}")
    print(f"  실행: {model['label']}")
    print(f"  베이스: {model['base']}")
    print(f"  경로: {model['path']}")
    print(f"{'=' * 80}")

    # 별도 프로세스로 실행
    t_total = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=900,
    )
    t_total = time.perf_counter() - t_total

    if result.returncode != 0:
        print("  ❌ 실패")
        print(f"  stderr: {result.stderr[-1000:]}")
        return {"id": model["id"], "label": model["label"], "error": result.stderr[-500:]}

    # 결과 파싱
    output = result.stdout
    try:
        start = output.index("===RESULT_START===") + len("===RESULT_START===")
        end = output.index("===RESULT_END===")
        json_str = output[start:end].strip()
        data = json.loads(json_str)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"  ❌ 결과 파싱 실패: {e}")
        print(f"  stdout 마지막 500자: {output[-500:]}")
        return {"id": model["id"], "label": model["label"], "error": str(e)}

    print("\n  📊 측정 결과:")
    print(f"     베이스라인 RSS:       {data['mem_baseline_rss_gb']} GB")
    print(
        f"     로드 후 RSS:          {data['mem_after_load_rss_gb']} GB (+{data['mem_load_delta_rss_gb']} GB)"
    )
    print(
        f"     추론 피크 RSS:        {data['mem_peak_rss_gb']} GB (+{data['mem_inference_delta_rss_gb']} GB)"
    )
    if data.get("mlx_peak_gb"):
        print(f"     MLX GPU 로드 후:      {data['mlx_after_load_gb']} GB")
        print(f"     MLX GPU 피크:         {data['mlx_peak_gb']} GB")
    print(
        f"     시스템 메모리 변화:    {data['mem_baseline_sys_gb']} → {data['mem_peak_sys_gb']} GB"
    )
    print(f"     CER: {data['cer_percent']}% | WER: {data['wer_percent']}%")
    print(
        f"     RTF: {data['rtf']}x | 로드: {data['load_time_s']}s | 추론: {data['total_time_s']}s"
    )
    print(f"     총 실행 시간: {t_total:.1f}초")

    return data


def get_disk_size_mb(path: str) -> float:
    """디스크 크기(MB)."""
    p = Path(path)
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size / (1024**2)
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


def print_comparison(results: list):
    """비교 표 출력."""
    print(f"\n\n{'=' * 100}")
    print(f"  🎯 정밀 비교 결과 (별도 프로세스, 메모리 격리, {NUM_SAMPLES}개 샘플)")
    print(f"{'=' * 100}\n")

    if len(results) < 2 or any("error" in r for r in results):
        print("일부 모델 실패")
        return

    a, b = results[0], results[1]

    print(f"{'항목':<35} {a['label'][:30]:<32} {b['label'][:30]:<32}")
    print("-" * 100)

    rows = [
        ("디스크 크기 (MB)", a.get("disk_mb", 0), b.get("disk_mb", 0), "MB", "lower"),
        (
            "로드 후 RSS (GB)",
            a["mem_after_load_rss_gb"],
            b["mem_after_load_rss_gb"],
            "GB",
            "lower",
        ),
        (
            "로드 시 RSS 증가 (GB)",
            a["mem_load_delta_rss_gb"],
            b["mem_load_delta_rss_gb"],
            "GB",
            "lower",
        ),
        ("추론 피크 RSS (GB)", a["mem_peak_rss_gb"], b["mem_peak_rss_gb"], "GB", "lower"),
        (
            "MLX GPU 로드 후 (GB)",
            a.get("mlx_after_load_gb"),
            b.get("mlx_after_load_gb"),
            "GB",
            "lower",
        ),
        ("MLX GPU 피크 (GB)", a.get("mlx_peak_gb"), b.get("mlx_peak_gb"), "GB", "lower"),
        ("로드 시간 (초)", a["load_time_s"], b["load_time_s"], "s", "lower"),
        ("RTF (실시간 배수)", a["rtf"], b["rtf"], "x", "lower"),
        ("CER (%)", a["cer_percent"], b["cer_percent"], "%", "lower"),
        ("WER (%)", a["wer_percent"], b["wer_percent"], "%", "lower"),
    ]

    for name, va, vb, unit, better in rows:
        if va is None or vb is None:
            sa = str(va)
            sb = str(vb)
            winner = "?"
        else:
            sa = f"{va}{unit}"
            sb = f"{vb}{unit}"
            if better == "lower":
                if va < vb:
                    winner = "← 우위"
                elif vb < va:
                    winner = "→ 우위"
                else:
                    winner = "동일"
            else:
                if va > vb:
                    winner = "← 우위"
                elif vb > va:
                    winner = "→ 우위"
                else:
                    winner = "동일"
        print(f"{name:<35} {sa:<32} {sb:<32} {winner}")

    print()
    print("💡 메모리 분석:")
    diff_load = abs(a["mem_load_delta_rss_gb"] - b["mem_load_delta_rss_gb"])
    diff_peak = abs(a["mem_peak_rss_gb"] - b["mem_peak_rss_gb"])
    print(f"   로드 시 메모리 차이: {diff_load:.2f} GB")
    print(f"   추론 피크 메모리 차이: {diff_peak:.2f} GB")

    if a.get("mlx_peak_gb") and b.get("mlx_peak_gb"):
        diff_mlx = abs(a["mlx_peak_gb"] - b["mlx_peak_gb"])
        print(f"   MLX GPU 피크 차이: {diff_mlx:.2f} GB ← 가장 정확한 비교 지표")


def main():
    print("=" * 80)
    print("  Top 2 한국어 Whisper 정밀 메모리 측정")
    print("  (각 모델 별도 프로세스에서 실행, MLX GPU 메모리 측정 포함)")
    print("=" * 80)

    import platform

    import psutil

    sys_mem_gb = psutil.virtual_memory().total / (1024**3)
    print(f"\n시스템: {platform.processor()} | RAM: {sys_mem_gb:.0f}GB")

    results = []
    for model in MODELS:
        # 디스크 크기
        disk_mb = get_disk_size_mb(model["path"])

        result = run_single_benchmark(model)
        if "error" not in result:
            result["disk_mb"] = round(disk_mb, 1)
        results.append(result)

        with open(RESULTS_PATH, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

    print_comparison(results)

    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n결과 저장: {RESULTS_PATH}")


if __name__ == "__main__":
    main()
