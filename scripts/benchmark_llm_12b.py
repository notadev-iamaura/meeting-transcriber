#!/usr/bin/env python3
"""
LLM 리소스·성능 측정 고도화 벤치마크 (Gemma 4 12B 도입 검증용)

목적:
    docs/GEMMA4_12B_ADOPTION.md 의 검증 게이트(G0~G4)를 실제 측정으로 채우기 위한
    하네스. 기존 scripts/benchmark_llm.py 를 다음 항목으로 고도화한다.

    - [G1] 메모리: 백그라운드 샘플링으로 "생성 중 피크 RSS" + 시스템 used/available
            + **스왑 사용량(swap used / sin / sout)** 까지 추적 → 스왑 발생 여부 판정
    - [G2] 속도: 태스크별 tok/s, 총 소요 (E4B 대비 비교)
    - [G0] thinking 태그: 응답에서 `<|channel>thought` / `<|think|>` 등 출현 탐지 + 제거
    - [G3] 요약: steps/summarizer.py 의 실제 시스템 프롬프트로 긴 한국어 전사문 요약
    - [G4] 고유명사 병기: "배미령" 케이스로 영어/중국어 로마자 병기 발생 탐지
    - 모델 디스크 크기(HF 캐시) 측정

사용법:
    # 메인 venv(python에 mlx 설치)로 실행
    PY=/Users/youngouksong/projects/meeting-transcriber/.venv/bin/python

    $PY scripts/benchmark_llm_12b.py --only e4b          # E4B 베이스라인만
    $PY scripts/benchmark_llm_12b.py --only 12b          # 12B 만
    $PY scripts/benchmark_llm_12b.py                     # E4B + 12B 비교
    $PY scripts/benchmark_llm_12b.py --models exaone,e4b,12b

의존성: mlx-lm, mlx-vlm(>=0.6.0 권장, 12B), psutil
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# === 모델 레지스트리 ===============================================

# key -> (HF repo, 표시명, use_vlm)
MODEL_REGISTRY: dict[str, tuple[str, str, bool]] = {
    "exaone": ("mlx-community/EXAONE-3.5-7.8B-Instruct-4bit", "EXAONE 3.5 7.8B", False),
    "e4b": ("mlx-community/gemma-4-e4b-it-4bit", "Gemma 4 E4B", True),
    # E4B 품질 업그레이드 후보 (현 기본 E4B 와 동급 크기/속도, 품질↑)
    "e4b-qat": ("mlx-community/gemma-4-E4B-it-qat-4bit", "Gemma 4 E4B QAT", True),  # mlx-vlm 멀티모달
    "e4b-optiq": ("mlx-community/gemma-4-e4b-it-OptiQ-4bit", "Gemma 4 E4B OptiQ", False),  # mlx-lm 텍스트전용
    "12b": ("mlx-community/gemma-4-12B-it-4bit", "Gemma 4 12B", True),
    "12b-mxfp4": ("mlx-community/gemma-4-12B-mxfp4", "Gemma 4 12B mxfp4", True),
}

# === 측정 대상 프롬프트 ============================================

# steps/summarizer.py 의 _FALLBACK_SUMMARIZER_PROMPT 와 동일 (요약 충실 재현)
SUMMARIZER_SYSTEM_PROMPT = """당신은 한국어 회의록 작성 전문가입니다.
회의 전사문을 분석하여 구조화된 마크다운 형식의 회의록을 작성합니다.

다음 형식으로 출력하세요:

## 회의 개요
- 참석자: (화자 목록)
- 주요 주제 한 줄 요약

## 주요 안건
1. 안건 제목
   - 세부 내용

## 결정 사항
- 결정된 내용을 항목별로 정리

## 액션 아이템
- [ ] 담당자: 할 일 내용

## 기타 논의
- 위 항목에 포함되지 않는 중요 논의 사항

규칙:
1. 전사문의 내용만 기반으로 작성하세요. 추측하지 마세요.
2. 결정 사항이 없으면 해당 섹션에 "없음"이라고 적으세요.
3. 액션 아이템이 없으면 해당 섹션에 "없음"이라고 적으세요.
4. 화자 이름을 그대로 사용하세요 (SPEAKER_00 등).
5. 간결하고 명확하게 작성하세요.
6. 마크다운 형식을 정확히 지켜주세요."""

# 현실적인 한국어 회의 전사문 (요약 입력). 인명/조직명/영어 용어 포함 → G4 동시 측정.
SAMPLE_TRANSCRIPT = """SPEAKER_00: 자 그럼 회의 시작하겠습니다. 오늘 안건은 Gemma 4 12B 모델 도입 검토하고요, 배미령 책임님이 한국어 품질 쪽 정리해주시기로 했습니다.
SPEAKER_01: 네 배미령입니다. 먼저 메모리부터 말씀드리면 MLX 4bit 버전이 11기가라서 16기가 맥에서는 빠듯하고요, 올라마 GGUF는 7.6기가라 좀 낫습니다.
SPEAKER_00: 그럼 기본값을 바꾸는 건 무리겠네요.
SPEAKER_01: 네 교정 태스크는 지금 E4B가 이미 정답지 유사도 92.9프로라 충분하고요, 12B는 요약 쪽에서 이득이 클 것 같습니다.
SPEAKER_02: 속도는 어떤가요? 팬리스 맥북에어에서 돌리면 서멀 쿨다운도 걸릴 텐데요.
SPEAKER_01: 12B는 E4B보다 두세 배 느릴 걸로 추정하는데, 실측은 아직입니다. 그래서 이번에 벤치마크 돌려보려고요.
SPEAKER_00: 좋습니다. 그리고 thinking 태그 이슈 있다고 들었는데요.
SPEAKER_01: 맞습니다. 12B는 thinking 꺼도 채널 태그를 뱉어서 우리 교정 파서랑 위키 JSON 파서가 깨질 수 있어요. 백엔드에서 제거 처리가 선행돼야 합니다.
SPEAKER_02: 그럼 정리하면, 기본값은 E4B 유지하고 12B는 옵트인으로만 추가하는 걸로요.
SPEAKER_00: 네 그렇게 가시죠. 배미령 책임님은 다음 주까지 한국어 고유명사 병기율 측정 부탁드리고요, SPEAKER_02님은 메모리 스왑 발생 여부 실측 부탁드립니다.
SPEAKER_01: 알겠습니다. 라이선스는 Apache 2.0이라 로컬 사용 제약은 없습니다.
SPEAKER_02: 확인했습니다. 그럼 다음 회의 때 실측 결과 공유하는 걸로 마무리하겠습니다."""

CORRECTION_SYSTEM_PROMPT = """다음은 음성 인식(STT)으로 전사된 한국어 회의 내용입니다.
오타, 맞춤법 오류, 부자연스러운 표현을 교정하세요.
원본의 의미를 변경하지 말고, 자연스러운 한국어로 교정만 하세요. 한국어 고유명사에 영어/중국어를 병기하지 마세요."""

CORRECTION_INPUT = """네 지금부터 준 녹음 테스트를 진행하도록 하겠읍니다 화자 분리는 별도로 없이 한 사람에 대한 보이스만 녹음을 하게 될거고요 이전 세션에서 모든 검증이 완료된 상태입니다 현재 커버 되지 않은 변경 사항을 정리하면 수정된 파일은 기존 콜파이 부분에서 유브이이 점 파일로 바뀌었구요"""

# G4: 고유명사 병기 가드 (evals/quality_golden_cases.json proper_noun_regression_guard 기반)
PROPER_NOUN_SYSTEM_PROMPT = """다음 문장을 한국어로 자연스럽게 다듬으세요. 인명·조직명 같은 한국어 고유명사에 영어나 중국어를 괄호로 병기하지 마세요."""
PROPER_NOUN_INPUT = """배미령 님은 Gemma 4 모델의 한국어 병기 문제를 검토합니다. 김도현 책임과 함께 회의록 품질을 점검합니다."""

# thinking 태그 / 채널 태그 패턴 (G0)
THINKING_PATTERNS = [
    r"<\|?channel\|?>",
    r"<\|?/?think\|?>",
    r"</?think>",
    r"<\|?thought\|?>",
    r"\bthought\b",
]
# 출력에서 thought 블록 제거: <|channel>thought ... <channel|>  형태 + <think>...</think>
_THOUGHT_BLOCK_RES = [
    re.compile(r"<\|channel\|?>\s*thought.*?<\|?channel\|?>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<\|channel\|?>thought.*?<channel\|?>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE),
]
# 영어/중국어 병기 탐지: 한글 바로 뒤 괄호 안에 라틴/한자
_GLOSS_RE = re.compile(r"[가-힣]\s*[\(\[（][A-Za-z一-鿿]")


def detect_thinking_tags(text: str) -> list[str]:
    """응답에서 thinking/채널 태그가 출현했는지 탐지한다 (G0)."""
    found = []
    for pat in THINKING_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            found.append(pat)
    return found


def strip_thought_blocks(text: str) -> str:
    """thought 블록을 제거한다 (백엔드 경계 처리 시뮬레이션)."""
    out = text
    for rx in _THOUGHT_BLOCK_RES:
        out = rx.sub("", out)
    return out.strip()


def count_gloss(text: str) -> int:
    """한국어 고유명사 영어/중국어 병기 추정 건수 (G4)."""
    return len(_GLOSS_RE.findall(text))


# === 메모리/리소스 측정 ============================================


@dataclass
class ResourceSampler:
    """백그라운드 스레드로 RSS/시스템메모리/스왑 피크를 샘플링한다."""

    interval: float = 0.4
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    # 피크 기록
    peak_proc_rss_gb: float = 0.0
    peak_sys_used_gb: float = 0.0
    min_sys_avail_gb: float = 1e9
    peak_swap_used_gb: float = 0.0
    swap_sin_start: int = 0
    swap_sout_start: int = 0
    swap_sin_end: int = 0
    swap_sout_end: int = 0
    samples: int = 0

    def _run(self) -> None:
        import psutil

        proc = psutil.Process()
        sw0 = psutil.swap_memory()
        self.swap_sin_start, self.swap_sout_start = sw0.sin, sw0.sout
        while not self._stop.is_set():
            try:
                rss = proc.memory_info().rss / (1024**3)
                vm = psutil.virtual_memory()
                sw = psutil.swap_memory()
                self.peak_proc_rss_gb = max(self.peak_proc_rss_gb, rss)
                self.peak_sys_used_gb = max(self.peak_sys_used_gb, vm.used / (1024**3))
                self.min_sys_avail_gb = min(self.min_sys_avail_gb, vm.available / (1024**3))
                self.peak_swap_used_gb = max(self.peak_swap_used_gb, sw.used / (1024**3))
                self.swap_sin_end, self.swap_sout_end = sw.sin, sw.sout
                self.samples += 1
            except Exception:
                pass
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def swap_delta_in_mb(self) -> float:
        """측정 동안 디스크→메모리로 스왑인된 양 추정 (MB). 0이면 스왑 거의 없음."""
        return max(0, self.swap_sin_end - self.swap_sin_start) / (1024**2)

    @property
    def swap_delta_out_mb(self) -> float:
        return max(0, self.swap_sout_end - self.swap_sout_start) / (1024**2)


def sys_mem_snapshot() -> dict:
    import psutil

    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {
        "total_gb": round(vm.total / (1024**3), 2),
        "used_gb": round(vm.used / (1024**3), 2),
        "available_gb": round(vm.available / (1024**3), 2),
        "percent": vm.percent,
        "swap_used_gb": round(sw.used / (1024**3), 2),
    }


def mlx_mem() -> dict:
    """MLX 통합메모리(Metal) 할당량을 직접 조회한다.

    psutil RSS 는 MLX/Metal 버퍼를 과소 측정하므로, 모델의 실제 메모리
    footprint 는 MLX 자체 카운터로 재는 것이 정확하다. 이 값이 G1 의 주력 지표.
    """
    try:
        import mlx.core as mx

        return {
            "active_gb": round(mx.get_active_memory() / (1024**3), 2),
            "peak_gb": round(mx.get_peak_memory() / (1024**3), 2),
            "cache_gb": round(mx.get_cache_memory() / (1024**3), 2),
        }
    except Exception:
        return {"active_gb": None, "peak_gb": None, "cache_gb": None}


def mlx_reset_peak() -> None:
    try:
        import mlx.core as mx

        mx.reset_peak_memory()
    except Exception:
        pass


def model_disk_size_gb(repo: str) -> float | None:
    """HF 캐시에서 모델 디스크 크기(GB)를 계산한다."""
    cache = Path.home() / ".cache" / "huggingface" / "hub"
    name = "models--" + repo.replace("/", "--")
    d = cache / name
    if not d.exists():
        return None
    total = 0
    for p in d.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return round(total / (1024**3), 2)


def clear_memory() -> None:
    gc.collect()
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception:
        pass
    time.sleep(1.5)


# === 단일 모델 벤치마크 ============================================


def benchmark_model(repo: str, label: str, use_vlm: bool, max_tokens: int) -> dict:
    results: dict = {"model": repo, "label": label, "use_vlm": use_vlm}
    print(f"\n{'=' * 64}")
    print(f"  {label}  ({repo})")
    print(f"  backend: {'mlx-vlm' if use_vlm else 'mlx-lm'}")
    print(f"{'=' * 64}")

    disk = model_disk_size_gb(repo)
    results["disk_size_gb"] = disk
    print(f"[디스크] 모델 크기: {disk if disk is not None else '미다운로드'} GB")

    before = sys_mem_snapshot()
    results["mem_before"] = before
    print(
        f"[메모리] 로드 전: used {before['used_gb']}GB / {before['total_gb']}GB "
        f"({before['percent']}%), avail {before['available_gb']}GB, swap {before['swap_used_gb']}GB"
    )

    sampler = ResourceSampler()
    mlx_reset_peak()  # MLX 피크 카운터 초기화
    sampler.start()

    # --- 모델 로드 ---
    print("\n[1] 모델 로드 중...")
    t0 = time.perf_counter()
    model = tokenizer = processor = None
    vlm_generate = lm_generate = None
    try:
        if use_vlm:
            from mlx_vlm import generate as vlm_generate  # type: ignore
            from mlx_vlm import load as vlm_load  # type: ignore

            model, processor = vlm_load(repo)
            tokenizer = getattr(processor, "tokenizer", processor)
        else:
            from mlx_lm import generate as lm_generate  # type: ignore
            from mlx_lm import load as lm_load  # type: ignore

            model, tokenizer = lm_load(repo, tokenizer_config={"trust_remote_code": True})
    except Exception as e:
        sampler.stop()
        print(f"  ❌ 모델 로드 실패: {e}")
        results["error"] = f"{type(e).__name__}: {e}"
        return results

    results["load_time_s"] = round(time.perf_counter() - t0, 2)
    peak_after_load = sampler.peak_proc_rss_gb
    results["peak_rss_after_load_gb"] = round(peak_after_load, 2)
    mlx_after_load = mlx_mem()
    results["mlx_active_after_load_gb"] = mlx_after_load["active_gb"]
    print(
        f"  로드 시간: {results['load_time_s']}초 | MLX active: {mlx_after_load['active_gb']}GB "
        f"(RSS는 MLX 과소측정: {peak_after_load:.2f}GB)"
    )

    # --- 생성 함수 ---
    def run(system_prompt: str, user_text: str, task: str, mtok: int) -> dict:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        try:
            prompt = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        except Exception:
            # system role 미지원 템플릿 폴백 → user 에 합침
            merged = f"{system_prompt}\n\n{user_text}"
            prompt = tokenizer.apply_chat_template(
                [{"role": "user", "content": merged}],
                tokenize=False,
                add_generation_prompt=True,
            )

        t = time.perf_counter()
        if use_vlm:
            r = vlm_generate(model, processor, prompt=prompt, max_tokens=mtok, verbose=False)
            raw = r.text
            ntok = getattr(r, "generation_tokens", len(tokenizer.encode(raw)))
            tps = getattr(r, "generation_tps", None)
        else:
            raw = lm_generate(model, tokenizer, prompt=prompt, max_tokens=mtok, verbose=False)
            ntok = len(tokenizer.encode(raw))
            tps = None
        elapsed = time.perf_counter() - t
        if tps is None:
            tps = ntok / elapsed if elapsed > 0 else 0

        tags = detect_thinking_tags(raw)
        clean = strip_thought_blocks(raw)
        gloss = count_gloss(clean)

        print(f"  [{task}] {elapsed:.1f}s | {ntok} tok | {tps:.1f} tok/s "
              f"| thinking태그 {'⚠️ ' + str(len(tags)) + '종' if tags else '없음'} "
              f"| 병기 {gloss}건")
        return {
            "task": task,
            "time_s": round(elapsed, 2),
            "tokens": int(ntok),
            "tok_per_sec": round(float(tps), 1),
            "thinking_tags": tags,
            "gloss_count": gloss,
            "output_clean": clean[:1200],
            "output_raw_head": raw[:200],
        }

    print("\n[2] 회의록 요약 (실제 summarizer 프롬프트)...")
    results["summary"] = run(SUMMARIZER_SYSTEM_PROMPT, SAMPLE_TRANSCRIPT, "요약", max_tokens)
    print("\n[3] 전사 교정...")
    results["correction"] = run(CORRECTION_SYSTEM_PROMPT, CORRECTION_INPUT, "교정", min(max_tokens, 400))
    print("\n[4] 고유명사 병기 가드 (G4)...")
    results["proper_noun"] = run(PROPER_NOUN_SYSTEM_PROMPT, PROPER_NOUN_INPUT, "고유명사", 200)

    # 생성 중 전체 피크
    mlx_end = mlx_mem()
    results["mlx_peak_gb"] = mlx_end["peak_gb"]  # ★ G1 주력 지표 (MLX 통합메모리 피크)
    results["mlx_cache_gb"] = mlx_end["cache_gb"]
    results["peak_rss_overall_gb"] = round(sampler.peak_proc_rss_gb, 2)
    results["peak_sys_used_gb"] = round(sampler.peak_sys_used_gb, 2)
    results["sys_used_delta_gb"] = round(sampler.peak_sys_used_gb - before["used_gb"], 2)
    results["min_sys_avail_gb"] = round(sampler.min_sys_avail_gb, 2)
    results["swap_used_before_gb"] = before["swap_used_gb"]
    results["peak_swap_used_gb"] = round(sampler.peak_swap_used_gb, 2)
    results["swap_used_delta_gb"] = round(sampler.peak_swap_used_gb - before["swap_used_gb"], 2)
    # pageins(sin)은 macOS에서 mmap 파일읽기까지 포함 → 참고용
    results["pageins_mb_ref"] = round(sampler.swap_delta_in_mb, 1)

    avg_tps = sum(results[k]["tok_per_sec"] for k in ("summary", "correction", "proper_noun")) / 3
    results["avg_tok_per_sec"] = round(avg_tps, 1)
    any_tags = any(results[k]["thinking_tags"] for k in ("summary", "correction", "proper_noun"))
    results["emits_thinking_tags"] = any_tags
    total_gloss = sum(results[k]["gloss_count"] for k in ("summary", "correction", "proper_noun"))
    results["total_gloss_count"] = total_gloss

    sampler.stop()
    print(
        f"\n[G1 메모리] MLX peak {results['mlx_peak_gb']}GB (모델 실측) "
        f"| sys_used Δ+{results['sys_used_delta_gb']}GB → 피크 {results['peak_sys_used_gb']}GB "
        f"| min_avail {results['min_sys_avail_gb']}GB "
        f"| swap Δ+{results['swap_used_delta_gb']}GB (피크 {results['peak_swap_used_gb']}GB)"
    )
    print(f"[G0] thinking 태그 출현: {'⚠️ 예' if any_tags else '아니오'}")
    print(f"[G4] 고유명사 병기 총 {total_gloss}건")

    # 언로드
    del model, tokenizer, processor
    clear_memory()
    after = sys_mem_snapshot()
    results["mem_after_unload"] = after
    print(f"[정리] 언로드 후: used {after['used_gb']}GB ({after['percent']}%)")
    return results


# === 비교 출력 =====================================================


def print_comparison(results: list[dict]) -> None:
    ok = [r for r in results if "error" not in r]
    if len(ok) < 2:
        return
    print(f"\n\n{'=' * 78}")
    print("  측정 비교 (E4B vs 12B 핵심 게이트)")
    print(f"{'=' * 78}")
    labels = [r["label"] for r in ok]
    hdr = "  {:<26s}".format("항목") + "".join(f"{l:>22s}" for l in labels)
    print("\n" + hdr)
    print("  " + "-" * (26 + 22 * len(ok)))

    def line(name, key, fmt="{}"):
        vals = []
        for r in ok:
            v = r.get(key)
            vals.append(fmt.format(v) if v is not None else "N/A")
        print("  {:<26s}".format(name) + "".join(f"{v:>22s}" for v in vals))

    line("디스크 크기(GB)", "disk_size_gb")
    line("로드 시간(s)", "load_time_s")
    line("MLX active 로드후(GB)", "mlx_active_after_load_gb")
    line("MLX peak(GB) ★[G1]", "mlx_peak_gb")
    line("sys_used 증가(GB)", "sys_used_delta_gb")
    line("최소 가용메모리(GB)", "min_sys_avail_gb")
    line("스왑 증가(GB) [G1]", "swap_used_delta_gb")
    line("평균 속도(tok/s) [G2]", "avg_tok_per_sec")
    line("thinking태그 [G0]", "emits_thinking_tags")
    line("고유명사 병기 [G4]", "total_gloss_count")

    # 요약 출력 미리보기
    print(f"\n\n{'=' * 78}\n  회의록 요약 출력 비교 [G3]\n{'=' * 78}")
    for r in ok:
        print(f"\n--- {r['label']} (요약, thinking태그 {'예' if r['summary']['thinking_tags'] else '없음'}) ---")
        print(r["summary"]["output_clean"][:700])


def main() -> None:
    ap = argparse.ArgumentParser(description="Gemma 4 12B 도입 검증 벤치마크")
    ap.add_argument("--models", type=str, default=None,
                    help="쉼표구분 키 (exaone,e4b,12b,12b-mxfp4)")
    ap.add_argument("--only", type=str, default=None, help="단일 키만 실행")
    ap.add_argument("--max-tokens", type=int, default=700, help="요약 최대 생성 토큰")
    args = ap.parse_args()

    if args.only:
        keys = [args.only]
    elif args.models:
        keys = [k.strip() for k in args.models.split(",") if k.strip()]
    else:
        keys = ["e4b", "12b"]

    invalid = [k for k in keys if k not in MODEL_REGISTRY]
    if invalid:
        print(f"알 수 없는 모델 키: {invalid}. 가능: {list(MODEL_REGISTRY)}")
        sys.exit(1)

    import platform

    print("=" * 78)
    print("  Meeting Transcriber — LLM 리소스·성능 측정 (Gemma 4 12B 도입 검증)")
    print("=" * 78)
    sm = sys_mem_snapshot()
    print(f"시스템: {platform.processor()} | RAM {sm['total_gb']}GB | macOS {platform.mac_ver()[0]} "
          f"| Python {platform.python_version()}")
    try:
        import mlx.core as mx
        import mlx_vlm  # type: ignore

        print(f"MLX {mx.__version__} | mlx-vlm {getattr(mlx_vlm, '__version__', '?')}")
    except Exception as e:
        print(f"MLX 정보 조회 실패: {e}")
    # 디스크 여유
    try:
        import shutil

        free = shutil.disk_usage(str(Path.home())).free / (1024**3)
        print(f"디스크 여유: {free:.1f}GB")
    except Exception:
        pass

    results = []
    for key in keys:
        repo, label, use_vlm = MODEL_REGISTRY[key]
        clear_memory()
        results.append(benchmark_model(repo, label, use_vlm, args.max_tokens))

    print_comparison(results)

    out = Path(__file__).resolve().parent / "benchmark_llm_12b_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n결과 저장: {out}")


if __name__ == "__main__":
    main()
