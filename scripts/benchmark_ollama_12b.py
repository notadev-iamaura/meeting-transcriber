#!/usr/bin/env python3
"""
Ollama Gemma 4 12B 리소스·성능 측정 (MLX 12B 와 RAM/속도 비교용).

목적:
    "Ollama(GGUF) 12B 가 MLX(11.25GB)보다 정말 RAM 을 덜 쓰는가?" 를 실측한다.
    docs/GEMMA4_12B_ADOPTION.md §0.5 의 MLX 측정과 동일 태스크/프롬프트로 비교.

측정:
    - [G1] 시스템 used/available/swap 피크(백그라운드 샘플링) + ollama 프로세스 RSS 합 + `ollama ps` SIZE
    - [G2] 태스크별 tok/s (Ollama /api/chat 의 eval_count / eval_duration)
    - [G0] thinking/채널 태그 출현 탐지
    - [G4] 한국어 고유명사 영어/중국어 병기 탐지
    - [G3] 실제 summarizer 시스템 프롬프트로 회의 전사문 요약

전제: `ollama serve` 실행 + `ollama pull <model>` 완료.

사용법:
    PY=/Users/youngouksong/projects/meeting-transcriber/.venv/bin/python
    $PY scripts/benchmark_ollama_12b.py --model gemma4:12b
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

OLLAMA = "http://127.0.0.1:11434"

# === benchmark_llm_12b.py 와 동일 프롬프트/입력 (비교 일관성) ===
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

PROPER_NOUN_SYSTEM_PROMPT = """다음 문장을 한국어로 자연스럽게 다듬으세요. 인명·조직명 같은 한국어 고유명사에 영어나 중국어를 괄호로 병기하지 마세요."""
PROPER_NOUN_INPUT = """배미령 님은 Gemma 4 모델의 한국어 병기 문제를 검토합니다. 김도현 책임과 함께 회의록 품질을 점검합니다."""

THINKING_PATTERNS = [r"<\|?channel\|?>", r"<\|?/?think\|?>", r"</?think>", r"<\|?thought\|?>", r"\bthought\b"]
_THOUGHT_BLOCK_RES = [
    re.compile(r"<\|channel\|?>\s*thought.*?<\|?channel\|?>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE),
]
_GLOSS_RE = re.compile(r"[가-힣]\s*[\(\[（][A-Za-z一-鿿]")


def detect_thinking(text: str) -> list[str]:
    return [p for p in THINKING_PATTERNS if re.search(p, text, re.IGNORECASE)]


def strip_thought(text: str) -> str:
    out = text
    for rx in _THOUGHT_BLOCK_RES:
        out = rx.sub("", out)
    return out.strip()


def count_gloss(text: str) -> int:
    return len(_GLOSS_RE.findall(text))


# === 메모리 샘플러 (시스템 + ollama 프로세스 RSS) ===


class Sampler:
    def __init__(self, interval: float = 0.4) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self.peak_sys_used_gb = 0.0
        self.min_sys_avail_gb = 1e9
        self.peak_swap_used_gb = 0.0
        self.peak_ollama_rss_gb = 0.0
        self.swap0 = 0.0

    def _ollama_rss_gb(self, psutil) -> float:
        total = 0
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                nm = (p.info.get("name") or "").lower()
                if "ollama" in nm:
                    total += p.info["memory_info"].rss
            except Exception:
                pass
        return total / (1024**3)

    def _run(self) -> None:
        import psutil

        self.swap0 = psutil.swap_memory().used / (1024**3)
        while not self._stop.is_set():
            try:
                vm = psutil.virtual_memory()
                sw = psutil.swap_memory()
                self.peak_sys_used_gb = max(self.peak_sys_used_gb, vm.used / (1024**3))
                self.min_sys_avail_gb = min(self.min_sys_avail_gb, vm.available / (1024**3))
                self.peak_swap_used_gb = max(self.peak_swap_used_gb, sw.used / (1024**3))
                self.peak_ollama_rss_gb = max(self.peak_ollama_rss_gb, self._ollama_rss_gb(psutil))
            except Exception:
                pass
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._t:
            self._t.join(timeout=3)


def sys_snapshot() -> dict:
    import psutil

    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {
        "used_gb": round(vm.used / (1024**3), 2),
        "available_gb": round(vm.available / (1024**3), 2),
        "total_gb": round(vm.total / (1024**3), 2),
        "swap_used_gb": round(sw.used / (1024**3), 2),
    }


def ollama_ps() -> str:
    try:
        out = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except Exception as e:
        return f"(ollama ps 실패: {e})"


def chat(model: str, system: str, user: str, max_tokens: int) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        # gemma4 12B 는 thinking 기본 ON → 끄지 않으면 response 가 빈다. (MLX 의 채널태그 G0 대응)
        "think": False,
        "options": {"temperature": 0.0, "num_predict": max_tokens},
        "keep_alive": "5m",
    }
    req = urllib.request.Request(
        f"{OLLAMA}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    elapsed = time.perf_counter() - t
    raw = data.get("message", {}).get("content", "")
    eval_count = data.get("eval_count", 0)
    eval_dur_ns = data.get("eval_duration", 0)
    tps = (eval_count / (eval_dur_ns / 1e9)) if eval_dur_ns else (eval_count / elapsed if elapsed else 0)
    return {"raw": raw, "eval_count": eval_count, "tps": round(tps, 1), "wall_s": round(elapsed, 2)}


def run_task(model: str, system: str, user: str, label: str, max_tokens: int) -> dict:
    r = chat(model, system, user, max_tokens)
    raw = r["raw"]
    tags = detect_thinking(raw)
    clean = strip_thought(raw)
    gloss = count_gloss(clean)
    print(f"  [{label}] {r['wall_s']}s | {r['eval_count']} tok | {r['tps']} tok/s "
          f"| thinking태그 {'⚠️' + str(len(tags)) if tags else '없음'} | 병기 {gloss}건")
    return {
        "task": label, "tok_per_sec": r["tps"], "tokens": r["eval_count"], "wall_s": r["wall_s"],
        "thinking_tags": tags, "gloss_count": gloss, "output_clean": clean[:1200],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma4:12b")
    ap.add_argument("--max-tokens", type=int, default=600)
    args = ap.parse_args()

    print("=" * 70)
    print(f"  Ollama LLM 측정 — {args.model}")
    print("=" * 70)
    # 서버/모델 확인
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/version", timeout=5) as r:
            print("Ollama:", json.loads(r.read()).get("version"))
    except Exception as e:
        print(f"❌ Ollama 서버 미응답: {e}")
        return

    before = sys_snapshot()
    print(f"[메모리] 로드 전: used {before['used_gb']}GB / {before['total_gb']}GB, "
          f"avail {before['available_gb']}GB, swap {before['swap_used_gb']}GB")

    sampler = Sampler()
    sampler.start()

    results: dict = {"model": args.model, "mem_before": before}
    print("\n[2] 회의록 요약 (실제 summarizer 프롬프트)...")
    results["summary"] = run_task(args.model, SUMMARIZER_SYSTEM_PROMPT, SAMPLE_TRANSCRIPT, "요약", args.max_tokens)
    print("[ollama ps]\n" + ollama_ps())
    print("\n[3] 전사 교정...")
    results["correction"] = run_task(args.model, CORRECTION_SYSTEM_PROMPT, CORRECTION_INPUT, "교정", min(args.max_tokens, 400))
    print("\n[4] 고유명사 병기 가드 (G4)...")
    results["proper_noun"] = run_task(args.model, PROPER_NOUN_SYSTEM_PROMPT, PROPER_NOUN_INPUT, "고유명사", 200)

    sampler.stop()
    results["peak_sys_used_gb"] = round(sampler.peak_sys_used_gb, 2)
    results["sys_used_delta_gb"] = round(sampler.peak_sys_used_gb - before["used_gb"], 2)
    results["min_sys_avail_gb"] = round(sampler.min_sys_avail_gb, 2)
    results["peak_swap_used_gb"] = round(sampler.peak_swap_used_gb, 2)
    results["swap_used_delta_gb"] = round(sampler.peak_swap_used_gb - before["swap_used_gb"], 2)
    results["peak_ollama_rss_gb"] = round(sampler.peak_ollama_rss_gb, 2)
    results["ollama_ps"] = ollama_ps()
    avg = sum(results[k]["tok_per_sec"] for k in ("summary", "correction", "proper_noun")) / 3
    results["avg_tok_per_sec"] = round(avg, 1)
    results["emits_thinking_tags"] = any(results[k]["thinking_tags"] for k in ("summary", "correction", "proper_noun"))
    results["total_gloss_count"] = sum(results[k]["gloss_count"] for k in ("summary", "correction", "proper_noun"))

    print(f"\n[G1 메모리] ollama 프로세스 RSS 피크 {results['peak_ollama_rss_gb']}GB "
          f"| sys_used Δ+{results['sys_used_delta_gb']}GB → 피크 {results['peak_sys_used_gb']}GB "
          f"| min_avail {results['min_sys_avail_gb']}GB | swap Δ+{results['swap_used_delta_gb']}GB")
    print(f"[G2] 평균 {results['avg_tok_per_sec']} tok/s | [G0] thinking {results['emits_thinking_tags']} "
          f"| [G4] 병기 {results['total_gloss_count']}건")
    print("\n[요약 출력 G3]\n" + results["summary"]["output_clean"][:700])

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", args.model)
    out = Path(__file__).resolve().parent / f"benchmark_ollama_{safe}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n결과 저장: {out}")


if __name__ == "__main__":
    main()
