#!/usr/bin/env python3
"""
llama-cpp-python 으로 GGUF 12B 를 in-process(서버 없이) 실행해 프로덕션 적합성 검증.

목적:
    "16GB + 서버 없음 + 12B 품질" 을 동시에 만족하는지 실측한다. 특히 **wiki 운영**
    (결정사항 JSON 추출) 처럼 까다로운 구조화 작업에서 품질이 production 에 쓸 만한지.

측정:
    - [G1] 메모리: 프로세스 RSS 피크 + 시스템 used (llama.cpp 는 in-process 라 RSS 가 유효)
    - [G2] 속도: 태스크별 tok/s (create_chat_completion usage)
    - [G3] 요약 품질 (실제 summarizer 프롬프트)
    - [WIKI] 결정사항 JSON 추출 (실제 core/wiki/extractors/decision.py 프롬프트) → JSON 유효성 검증
    - [G0] thinking 태그 / [G4] 고유명사 병기

사용법:
    PY=/Users/youngouksong/projects/meeting-transcriber/.venv/bin/python
    $PY scripts/benchmark_llamacpp_12b.py
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from huggingface_hub import hf_hub_download

GGUF_REPO = "unsloth/gemma-4-12B-it-qat-GGUF"
GGUF_FILE = "gemma-4-12B-it-qat-UD-Q4_K_XL.gguf"

# === 프롬프트 (benchmark_ollama 와 동일 + wiki) ===
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
3. 화자 이름을 그대로 사용하세요 (SPEAKER_00 등).
4. 간결하고 명확하게 작성하세요."""

# 타임스탬프 포함 전사문 (wiki citation 용)
TS_TRANSCRIPT = """[00:00:12] SPEAKER_00: 오늘 안건은 Gemma 4 12B 도입입니다. 배미령 책임이 한국어 품질을 봐주기로 했습니다.
[00:00:31] SPEAKER_01: 네 배미령입니다. 메모리는 MLX가 11기가라 16기가 맥에 안 맞고, GGUF는 7기가대라 가능합니다.
[00:01:05] SPEAKER_00: 그럼 기본값은 E4B 유지하고 12B는 요약 전용으로만 옵트인 하시죠.
[00:01:22] SPEAKER_02: 동의합니다. 다만 wiki 추출은 JSON이 깨지면 안 되니 12B 품질이 중요합니다.
[00:01:40] SPEAKER_00: 좋습니다. 그럼 배미령 책임은 다음 주까지 한국어 고유명사 병기율을 측정하고, SPEAKER_02는 16기가에서 스왑 발생 여부를 실측하기로 결정합니다.
[00:02:03] SPEAKER_01: 알겠습니다. 라이선스는 Apache 2.0이라 로컬 사용 제약은 없습니다."""

# 실제 wiki 결정사항 추출 프롬프트 (core/wiki/extractors/decision.py _EXTRACT_SYSTEM_PROMPT)
WIKI_EXTRACT_SYSTEM = """당신은 회의록에서 결정사항(decisions) 만 추출하는 분석가입니다.
출력은 반드시 JSON 배열이어야 하며, 각 항목은 다음 키를 포함합니다:
- title: 한 줄 요약 (한국어)
- decision_text: 결정 본문 (인용 마커 [meeting:{제공된 회의 ID}@HH:MM:SS] 필수)
- background: 배경 설명 (직접 근거가 있으면 인용 마커 필수, 추론만 가능하면 빈 문자열)
- follow_ups: [{owner, description, citation_ts}, ...] (없으면 빈 배열)
- participants: 화자 이름 배열
- projects: 프로젝트 slug 배열
- confidence: 0~10 정수

규칙:
1. 결정사항이 없으면 빈 배열 [] 만 출력.
2. 한국어 고유명사에 영어/중국어 병기 절대 금지.
3. 모든 사실 진술에 인용 마커 부착.
4. citation_ts 와 인용 마커의 HH:MM:SS 는 반드시 발화 목록에 있는 실제 시각을 사용."""

WIKI_EXTRACT_USER = f"""회의 ID: meeting_20260607_1000
발화 목록:
{TS_TRANSCRIPT}

위 컨텍스트에서 결정사항을 JSON 배열로 추출하세요."""

CORRECTION_SYSTEM = """다음은 음성 인식(STT)으로 전사된 한국어 회의 내용입니다.
오타·맞춤법·부자연스러운 표현을 교정하세요. 의미 변경 금지, 한국어 고유명사에 영어/중국어 병기 금지."""
CORRECTION_INPUT = """네 지금부터 준 녹음 테스트를 진행하도록 하겠읍니다 화자 분리는 별도로 없이 한 사람에 대한 보이스만 녹음을 하게 될거고요 수정된 파일은 기존 콜파이 부분에서 유브이이 점 파일로 바뀌었구요"""

THINKING_PAT = [r"<\|?channel\|?>", r"<\|?/?think\|?>", r"</?think>", r"\bthought\b"]
_THOUGHT_RE = [
    re.compile(r"<\|channel\|?>\s*thought.*?<\|?channel\|?>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE),
]
_GLOSS_RE = re.compile(r"[가-힣]\s*[\(\[（][A-Za-z一-鿿]")


def strip_thought(t: str) -> str:
    for rx in _THOUGHT_RE:
        t = rx.sub("", t)
    return t.strip()


def detect_think(t: str) -> bool:
    return any(re.search(p, t, re.IGNORECASE) for p in THINKING_PAT)


def extract_json_array(text: str):
    """텍스트에서 JSON 배열을 robust 하게 추출 (wiki decision.py 와 동일 전략)."""
    t = strip_thought(text)
    t = re.sub(r"^```(json)?|```$", "", t.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(t), None
    except (json.JSONDecodeError, ValueError):
        pass
    i, j = t.find("["), t.rfind("]")
    if i != -1 and j != -1 and j > i:
        try:
            return json.loads(t[i : j + 1]), None
        except (json.JSONDecodeError, ValueError) as e:
            return None, f"부분추출 실패: {e}"
    return None, "JSON 배열 없음"


def rss_gb() -> float:
    import psutil

    return psutil.Process().memory_info().rss / (1024**3)


def sys_used_gb() -> float:
    import psutil

    return psutil.virtual_memory().used / (1024**3)


def main() -> None:
    import psutil

    print("=" * 72)
    print("  llama-cpp-python GGUF 12B (QAT) — in-process 프로덕션 검증")
    print("=" * 72)
    vm0 = psutil.virtual_memory()
    print(f"시스템: RAM {vm0.total/1024**3:.0f}GB | 로드 전 used {vm0.used/1024**3:.1f}GB "
          f"| avail {vm0.available/1024**3:.1f}GB")

    print(f"\n[0] GGUF 확보: {GGUF_REPO}/{GGUF_FILE}")
    gguf = hf_hub_download(GGUF_REPO, GGUF_FILE)
    disk_gb = Path(gguf).stat().st_size / (1024**3)
    print(f"    경로: {gguf} ({disk_gb:.2f}GB)")

    from llama_cpp import Llama

    print("\n[1] 모델 로드 (in-process, Metal, 서버 없음)...")
    rss_before = rss_gb()
    sw0 = psutil.swap_memory().used / (1024**3)
    t0 = time.perf_counter()
    llm = Llama(
        model_path=gguf,
        n_ctx=4096,
        n_gpu_layers=-1,  # 전체 레이어 Metal GPU 적재
        verbose=False,
    )
    load_s = time.perf_counter() - t0
    rss_after = rss_gb()
    print(f"    로드 {load_s:.1f}s | 프로세스 RSS {rss_before:.2f}→{rss_after:.2f}GB "
          f"(+{rss_after-rss_before:.2f}) | sys_used {sys_used_gb():.1f}GB")

    peak_rss = rss_after
    peak_sys = sys_used_gb()

    def run(system: str, user: str, label: str, max_tokens: int) -> dict:
        nonlocal peak_rss, peak_sys
        t = time.perf_counter()
        out = llm.create_chat_completion(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=max_tokens,
        )
        dt = time.perf_counter() - t
        txt = out["choices"][0]["message"]["content"] or ""
        ntok = out.get("usage", {}).get("completion_tokens", 0)
        tps = ntok / dt if dt > 0 else 0
        peak_rss = max(peak_rss, rss_gb())
        peak_sys = max(peak_sys, sys_used_gb())
        clean = strip_thought(txt)
        gloss = len(_GLOSS_RE.findall(clean))
        thinking = detect_think(txt)
        print(f"    [{label}] {dt:.1f}s | {ntok} tok | {tps:.1f} tok/s "
              f"| thinking {'⚠️' if thinking else '없음'} | 병기 {gloss}")
        return {"label": label, "time_s": round(dt, 1), "tokens": ntok,
                "tok_per_sec": round(tps, 1), "thinking": thinking, "gloss": gloss,
                "output": clean}

    print("\n[2] 회의록 요약 (G3)...")
    summ = run(SUMMARIZER_SYSTEM_PROMPT, TS_TRANSCRIPT, "요약", 600)
    print("\n[3] 전사 교정...")
    corr = run(CORRECTION_SYSTEM, CORRECTION_INPUT, "교정", 300)
    print("\n[4] ★ WIKI 결정사항 JSON 추출 (production wiki 워크로드)...")
    wiki = run(WIKI_EXTRACT_SYSTEM, WIKI_EXTRACT_USER, "wiki추출", 800)

    # WIKI JSON 유효성 검증
    parsed, err = extract_json_array(wiki["output"])
    print("\n--- WIKI JSON 검증 ---")
    if parsed is not None:
        n = len(parsed) if isinstance(parsed, list) else 0
        has_cite = any("[meeting:" in json.dumps(x, ensure_ascii=False) for x in parsed) if n else False
        keys_ok = all(
            isinstance(x, dict) and "title" in x and "decision_text" in x and "confidence" in x
            for x in parsed
        ) if n else False
        print(f"    ✅ JSON 파싱 성공 | 결정 {n}건 | 인용마커 {'있음' if has_cite else '없음'} "
              f"| 필수키 {'정상' if keys_ok else '누락'}")
        wiki_verdict = {"json_valid": True, "decisions": n, "has_citation": has_cite, "keys_ok": keys_ok}
        for x in (parsed if isinstance(parsed, list) else [])[:2]:
            print("    •", json.dumps(x, ensure_ascii=False)[:180])
    else:
        print(f"    ❌ JSON 파싱 실패: {err}")
        print("    출력 일부:", wiki["output"][:200])
        wiki_verdict = {"json_valid": False, "error": err}

    print("\n=== 종합 (G1/G2) ===")
    print(f"  디스크: {disk_gb:.2f}GB | 모델 RSS: ~{rss_after-rss_before:.2f}GB | 피크 RSS: {peak_rss:.2f}GB")
    print(f"  피크 sys_used: {peak_sys:.1f}GB | 16GB 적합: {'예' if peak_sys < 13 else '주의'}")
    avg_tps = round((summ["tok_per_sec"] + corr["tok_per_sec"] + wiki["tok_per_sec"]) / 3, 1)
    print(f"  평균 속도: {avg_tps} tok/s")
    print(f"  thinking 태그: {summ['thinking'] or corr['thinking'] or wiki['thinking']}")
    print(f"  요약 품질(일부): {summ['output'][:160]}")

    results = {
        "model": f"{GGUF_REPO}/{GGUF_FILE}", "backend": "llama-cpp-python",
        "disk_gb": round(disk_gb, 2), "load_s": round(load_s, 1),
        "model_rss_gb": round(rss_after - rss_before, 2), "peak_rss_gb": round(peak_rss, 2),
        "peak_sys_used_gb": round(peak_sys, 1), "avg_tok_per_sec": avg_tps,
        "summary": summ, "correction": corr, "wiki": wiki, "wiki_verdict": wiki_verdict,
    }
    out = Path(__file__).resolve().parent / "benchmark_llamacpp_12b_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n결과 저장: {out}")


if __name__ == "__main__":
    main()
