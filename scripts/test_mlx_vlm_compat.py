#!/usr/bin/env python3
"""
mlx-vlm 버전별 Gemma 4 E4B / 12B 동시 로드 호환성 실측 스크립트.

목적:
    "MLX 에서 E4B(기본)와 12B 를 동시에 지원하는 mlx-vlm 버전이 있는가?" 를
    버전을 바꿔가며 실제 로드해서 판정한다. (docs/GEMMA4_12B_ADOPTION.md §7 버전 충돌)

사용법:
    PY=/Users/youngouksong/projects/meeting-transcriber/.venv/bin/python
    $PY scripts/test_mlx_vlm_compat.py            # E4B + 12B (mlx-vlm)
    $PY scripts/test_mlx_vlm_compat.py --mlx-lm   # 12B 를 mlx-lm(텍스트 전용) 로도 시도

로드만 수행하고 즉시 해제한다(생성 없음). 한 번에 모델 1개만 적재.
"""

from __future__ import annotations

import argparse
import gc
import sys

E4B = "mlx-community/gemma-4-e4b-it-4bit"
G12B = "mlx-community/gemma-4-12B-it-4bit"


def _clear() -> None:
    gc.collect()
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception:
        pass


def try_load_vlm(repo: str) -> tuple[bool, str]:
    """mlx-vlm 으로 로드 시도. (성공여부, 메시지)."""
    try:
        from mlx_vlm import load as vlm_load

        model, processor = vlm_load(repo)
        del model, processor
        _clear()
        return True, "OK"
    except Exception as e:
        _clear()
        return False, f"{type(e).__name__}: {str(e)[:160]}"


def try_load_lm(repo: str) -> tuple[bool, str]:
    """mlx-lm(텍스트 전용) 으로 로드 시도."""
    try:
        from mlx_lm import load as lm_load

        model, tok = lm_load(repo, tokenizer_config={"trust_remote_code": True})
        del model, tok
        _clear()
        return True, "OK"
    except Exception as e:
        _clear()
        return False, f"{type(e).__name__}: {str(e)[:160]}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mlx-lm", action="store_true", help="12B 를 mlx-lm 로도 시도")
    args = ap.parse_args()

    try:
        import mlx_vlm

        vlm_ver = mlx_vlm.__version__
    except Exception as e:
        print(f"mlx-vlm import 실패: {e}")
        sys.exit(1)
    try:
        import mlx_lm

        lm_ver = getattr(mlx_lm, "__version__", "?")
    except Exception:
        lm_ver = "?"

    print(f"== mlx-vlm {vlm_ver} | mlx-lm {lm_ver} ==")

    print(f"\n[mlx-vlm] E4B 로드 시도 ({E4B}) ...")
    e_ok, e_msg = try_load_vlm(E4B)
    print(f"  → {'✅ 성공' if e_ok else '❌ 실패'} : {e_msg}")

    print(f"\n[mlx-vlm] 12B 로드 시도 ({G12B}) ...")
    g_ok, g_msg = try_load_vlm(G12B)
    print(f"  → {'✅ 성공' if g_ok else '❌ 실패'} : {g_msg}")

    verdict = "둘 다 성공 → 이 버전으로 동시 지원 가능 ✅" if (e_ok and g_ok) else (
        "E4B만 성공(12B 미지원)" if e_ok else (
            "12B만 성공(E4B 깨짐)" if g_ok else "둘 다 실패"))
    print(f"\n[판정] mlx-vlm {vlm_ver}: {verdict}")

    if args.mlx_lm:
        print(f"\n[mlx-lm] 12B 텍스트전용 로드 시도 ({G12B}) ...")
        l_ok, l_msg = try_load_lm(G12B)
        print(f"  → {'✅ 성공' if l_ok else '❌ 실패'} : {l_msg}")
        if l_ok:
            print("  ※ mlx-lm 로 12B 로드 가능 → mlx-vlm 버전 안 올리고도 12B 가능(텍스트 전용)")


if __name__ == "__main__":
    main()
