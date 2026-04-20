#!/usr/bin/env python3
"""
LLM 모델 벤치마크 스크립트

목적: EXAONE 3.5 7.8B vs Gemma 4 E4B를 Apple Silicon MLX에서 직접 비교한다.
측정 항목: 모델 로드 시간, 메모리 사용량, 토큰 생성 속도, 한국어 품질
"""

import gc
import json
import os
import sys
import time

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_memory_mb() -> float:
    """현재 프로세스의 RSS 메모리(MB)를 반환한다."""
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def get_system_memory() -> dict:
    """시스템 메모리 상태를 반환한다."""
    import psutil

    mem = psutil.virtual_memory()
    return {
        "total_gb": round(mem.total / (1024**3), 1),
        "available_gb": round(mem.available / (1024**3), 1),
        "used_gb": round(mem.used / (1024**3), 1),
        "percent": mem.percent,
    }


def clear_memory():
    """메모리를 정리한다."""
    gc.collect()
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception:
        pass
    time.sleep(2)


# === 벤치마크 프롬프트 (실제 회의 전사 보정 시나리오) ===

# 한국어 전사 보정 프롬프트
CORRECTION_PROMPT = """다음은 음성 인식(STT)으로 전사된 한국어 회의 내용입니다.
오타, 맞춤법 오류, 부자연스러운 표현을 교정하고, 문맥에 맞게 수정해주세요.
원본의 의미를 변경하지 말고, 자연스러운 한국어로 교정만 해주세요.

[원본 전사]
네 지금부터 준 녹음 테스트를 진행하도록 하겠읍니다 네 화자 분리는 별도로 없이 한 사람에 대한 보이스만 녹음을 하게 될거고요 테스트 케이스를 한번 읽어보도록 하겠읍니다 이전 세션에서 모든 검증이 완료된 상태입니다 현재 커버 되지 않은 변경 사항을 정리하면 수정된 파일은 기존 어 콜파이 부분에서 유브이이 점 파일로 바뀌었구요

[교정 결과]"""

# 한국어 요약 프롬프트
SUMMARY_PROMPT = """다음 회의 전사문을 읽고, 핵심 내용을 3줄로 요약해주세요.

[회의 전사문]
참석자1: 이번 분기 매출이 전년 대비 15% 증가했습니다. 특히 B2B 부문에서 30% 성장을 기록했고요.
참석자2: 좋은 성과네요. 다음 분기 목표는 어떻게 설정할까요?
참석자1: 현재 파이프라인을 고려하면 20% 성장이 가능할 것으로 보입니다. 다만 신규 고객 확보를 위한 마케팅 예산 증액이 필요합니다.
참석자2: 마케팅 예산은 얼마나 더 필요한가요?
참석자1: 현재 대비 약 40% 증액을 요청드립니다. ROI 기준으로 충분히 정당화됩니다.
참석자3: 인력 충원도 함께 검토해야 합니다. 현재 영업팀 인원으로는 목표 달성이 어렵습니다.

[요약]"""

# 영어 추론 프롬프트 (일반 성능 비교)
REASONING_PROMPT = """Solve this step by step:
A store has 120 apples. On Monday, they sold 1/3 of the apples. On Tuesday, they sold 1/4 of the remaining apples. How many apples are left?"""


def benchmark_model(model_name: str, label: str, use_vlm: bool = False) -> dict:
    """단일 모델의 벤치마크를 실행한다.

    Args:
        model_name: HuggingFace 모델 경로
        label: 표시용 모델 이름
        use_vlm: True이면 mlx-vlm 사용 (Gemma 4 등 멀티모달 모델)
    """
    results = {"model": model_name, "label": label}

    print(f"\n{'=' * 60}")
    print(f"  {label}: {model_name}")
    print(f"  (backend: {'mlx-vlm' if use_vlm else 'mlx-lm'})")
    print(f"{'=' * 60}")

    # 시스템 메모리 (로드 전)
    sys_mem_before = get_system_memory()
    print(
        f"\n[메모리] 로드 전: {sys_mem_before['used_gb']}GB / {sys_mem_before['total_gb']}GB ({sys_mem_before['percent']}%)"
    )
    results["mem_before_gb"] = sys_mem_before["used_gb"]

    # 모델 로드
    print("\n[1/4] 모델 로드 중...")
    t0 = time.perf_counter()
    try:
        if use_vlm:
            from mlx_vlm import generate as vlm_generate
            from mlx_vlm import load as vlm_load

            model, processor = vlm_load(model_name)
            tokenizer = processor.tokenizer if hasattr(processor, "tokenizer") else processor
        else:
            from mlx_lm import generate as lm_generate
            from mlx_lm import load as lm_load

            model, tokenizer = lm_load(
                model_name,
                tokenizer_config={"trust_remote_code": True},
            )
            processor = None
    except Exception as e:
        print(f"  모델 로드 실패: {e}")
        results["error"] = str(e)
        return results

    load_time = time.perf_counter() - t0
    results["load_time_s"] = round(load_time, 2)
    print(f"  로드 시간: {load_time:.2f}초")

    # 시스템 메모리 (로드 후)
    sys_mem_after = get_system_memory()
    mem_delta = sys_mem_after["used_gb"] - sys_mem_before["used_gb"]
    results["mem_after_gb"] = sys_mem_after["used_gb"]
    results["mem_delta_gb"] = round(mem_delta, 2)
    print(
        f"  메모리 증가: +{mem_delta:.2f}GB (현재: {sys_mem_after['used_gb']}GB, {sys_mem_after['percent']}%)"
    )

    # 생성 함수 — tokenizer/model/processor 는 위 try 블록에서 바인딩된 outer scope 변수
    # (ruff 의 정적 분석이 try/except 내 할당을 추적 못해 F821 오보가 나지만 런타임 정상)
    def run_generation(prompt_text: str, task_name: str, max_tokens: int = 300) -> dict:
        """프롬프트를 실행하고 속도를 측정한다."""
        messages = [{"role": "user", "content": prompt_text}]
        formatted = tokenizer.apply_chat_template(  # noqa: F821
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )

        # 생성
        t_start = time.perf_counter()
        if use_vlm:
            result = vlm_generate(
                model,  # noqa: F821
                processor,  # noqa: F821
                prompt=formatted,
                max_tokens=max_tokens,
                verbose=False,
            )
            output_text = result.text
            output_tokens = result.generation_tokens
            tok_per_sec = result.generation_tps
        else:
            output_text = lm_generate(
                model,  # noqa: F821
                tokenizer,  # noqa: F821
                prompt=formatted,
                max_tokens=max_tokens,
                verbose=False,
            )
            output_tokens = len(tokenizer.encode(output_text))  # noqa: F821
            elapsed_inner = time.perf_counter() - t_start
            tok_per_sec = output_tokens / elapsed_inner if elapsed_inner > 0 else 0

        t_end = time.perf_counter()
        elapsed = t_end - t_start

        print(f"  [{task_name}]")
        print(
            f"    생성 시간: {elapsed:.2f}초 | 토큰: {output_tokens}개 | 속도: {tok_per_sec:.1f} tok/s"
        )
        print(f"    출력 (처음 200자): {output_text[:200]}...")

        return {
            "time_s": round(elapsed, 2),
            "tokens": output_tokens,
            "tok_per_sec": round(tok_per_sec, 1),
            "output": output_text[:500],
        }

    # 벤치마크 실행
    print("\n[2/4] 한국어 전사 보정...")
    results["correction"] = run_generation(CORRECTION_PROMPT, "전사 보정")

    print("\n[3/4] 한국어 요약...")
    results["summary"] = run_generation(SUMMARY_PROMPT, "회의 요약", max_tokens=200)

    print("\n[4/4] 영어 추론...")
    results["reasoning"] = run_generation(REASONING_PROMPT, "영어 추론", max_tokens=200)

    # 평균 속도
    avg_tps = (
        results["correction"]["tok_per_sec"]
        + results["summary"]["tok_per_sec"]
        + results["reasoning"]["tok_per_sec"]
    ) / 3
    results["avg_tok_per_sec"] = round(avg_tps, 1)

    # 모델 언로드
    print("\n[정리] 모델 언로드 중...")
    del model, tokenizer, processor
    clear_memory()

    sys_mem_final = get_system_memory()
    print(f"  메모리 복원: {sys_mem_final['used_gb']}GB ({sys_mem_final['percent']}%)")

    return results


def print_comparison(r1: dict, r2: dict):
    """두 모델의 결과를 비교 테이블로 출력한다."""
    print(f"\n\n{'=' * 70}")
    print("  벤치마크 결과 비교")
    print(f"{'=' * 70}")

    def row(label, v1, v2, unit="", better="lower"):
        """비교 행을 출력한다."""
        s1 = f"{v1}{unit}" if v1 is not None else "N/A"
        s2 = f"{v2}{unit}" if v2 is not None else "N/A"
        if v1 is not None and v2 is not None:
            if better == "lower":
                winner = "←" if v1 < v2 else ("→" if v2 < v1 else "=")
            else:
                winner = "←" if v1 > v2 else ("→" if v2 > v1 else "=")
        else:
            winner = "?"
        print(f"  {label:<20s}  {s1:>15s}  {s2:>15s}  {winner}")

    print(f"\n  {'항목':<20s}  {'EXAONE 3.5':>15s}  {'Gemma 4 E4B':>15s}  승자")
    print(f"  {'-' * 20}  {'-' * 15}  {'-' * 15}  ---")

    row("로드 시간", r1.get("load_time_s"), r2.get("load_time_s"), "초", "lower")
    row("메모리 증가", r1.get("mem_delta_gb"), r2.get("mem_delta_gb"), "GB", "lower")

    if "correction" in r1 and "correction" in r2:
        row(
            "보정 속도",
            r1["correction"]["tok_per_sec"],
            r2["correction"]["tok_per_sec"],
            " tok/s",
            "higher",
        )
        row("보정 시간", r1["correction"]["time_s"], r2["correction"]["time_s"], "초", "lower")

    if "summary" in r1 and "summary" in r2:
        row(
            "요약 속도",
            r1["summary"]["tok_per_sec"],
            r2["summary"]["tok_per_sec"],
            " tok/s",
            "higher",
        )
        row("요약 시간", r1["summary"]["time_s"], r2["summary"]["time_s"], "초", "lower")

    if "reasoning" in r1 and "reasoning" in r2:
        row(
            "추론 속도",
            r1["reasoning"]["tok_per_sec"],
            r2["reasoning"]["tok_per_sec"],
            " tok/s",
            "higher",
        )
        row("추론 시간", r1["reasoning"]["time_s"], r2["reasoning"]["time_s"], "초", "lower")

    row("평균 속도", r1.get("avg_tok_per_sec"), r2.get("avg_tok_per_sec"), " tok/s", "higher")

    # 한국어 출력 비교
    print(f"\n\n{'=' * 70}")
    print("  한국어 출력 비교")
    print(f"{'=' * 70}")

    for task, task_label in [("correction", "전사 보정"), ("summary", "회의 요약")]:
        print(f"\n--- {task_label} ---")
        if task in r1:
            print("\n[EXAONE 3.5]")
            print(f"{r1[task]['output'][:400]}")
        if task in r2:
            print("\n[Gemma 4 E4B]")
            print(f"{r2[task]['output'][:400]}")


def main():
    """벤치마크를 실행한다."""
    print("=" * 70)
    print("  Meeting Transcriber — LLM 벤치마크")
    print("  EXAONE 3.5 7.8B (4bit) vs Gemma 4 E4B (4bit)")
    print("=" * 70)

    # 시스템 정보
    import platform

    sys_mem = get_system_memory()
    print(
        f"\n시스템: {platform.processor()} | RAM: {sys_mem['total_gb']}GB | macOS {platform.mac_ver()[0]}"
    )
    print(f"Python: {platform.python_version()}")

    try:
        import mlx.core as mx

        print(f"MLX: {mx.__version__}")
    except Exception:
        print("MLX: 미설치")

    # (모델명, 표시 이름, mlx-vlm 사용 여부)
    models = [
        ("mlx-community/EXAONE-3.5-7.8B-Instruct-4bit", "EXAONE 3.5", False),
        ("mlx-community/gemma-4-e4b-it-4bit", "Gemma 4 E4B", True),
    ]

    results = []
    for model_name, label, use_vlm in models:
        clear_memory()
        result = benchmark_model(model_name, label, use_vlm=use_vlm)
        results.append(result)

    # 비교 테이블 출력
    if len(results) == 2 and "error" not in results[0] and "error" not in results[1]:
        print_comparison(results[0], results[1])

    # 결과 JSON 저장
    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "benchmark_results.json",
    )
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n결과 저장: {output_path}")


if __name__ == "__main__":
    main()
