#!/usr/bin/env python3
"""
Wiki 결정사항 추출 — E4B 프롬프트 개선 + 10 TC 다중 실행 검증.

목적:
    1차 평가에서 E4B 가 TC2(결정 없음)에서 "추후 논의하기로 함"(연기)을 결정으로
    과추출(false positive)했다. 프롬프트에 "연기·보류 ≠ 결정" 규칙을 추가해 이를
    고치고, false-positive 를 자극하는 TC 를 늘려 다중 실행으로 재검증한다.

채점:
    - json_valid / n vs expected / 인용환각(cite_invalid) / 병기(gloss) / keys_ok
    - --runs N: 각 TC 를 N 회 반복해 일관성(consistency) 측정

사용법:
    PY=.../.venv/bin/python
    $PY scripts/eval_wiki_extraction.py --backend e4b --runs 3
    $PY scripts/eval_wiki_extraction.py --backend e4b --prompt old   # 개선 전 비교용
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

# === 개선 전(old) 프롬프트 = decision.py 원본 ===
WIKI_SYSTEM_OLD = """당신은 회의록에서 결정사항(decisions) 만 추출하는 분석가입니다.
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
4. citation_ts 와 인용 마커의 HH:MM:SS 는 반드시 발화 목록에 있는 실제 시각을 사용.
5. 00:00:00 은 발화 목록에 실제로 [00:00:00] 줄이 있을 때만 사용."""

# === 개선(new) 프롬프트 — 과추출 차단 규칙 6,7,8 추가 ===
WIKI_SYSTEM_NEW = """당신은 회의록에서 결정사항(decisions) 만 추출하는 분석가입니다.
출력은 반드시 JSON 배열이어야 하며, 각 항목은 다음 키를 포함합니다:
- title: 한 줄 요약 (한국어)
- decision_text: 결정 본문 (인용 마커 [meeting:{제공된 회의 ID}@HH:MM:SS] 필수)
- background: 배경 설명 (직접 근거가 있으면 인용 마커 필수, 추론만 가능하면 빈 문자열)
- follow_ups: [{owner, description, citation_ts}, ...] (없으면 빈 배열). 한 결정에 딸린 업무 분담은
  별도 결정이 아니라 반드시 이 follow_ups 에 넣는다.
- participants: 화자 이름 배열
- projects: 프로젝트 slug 배열
- confidence: 0~10 정수

규칙:
1. 결정사항이 없으면 빈 배열 [] 만 출력.
2. 한국어 고유명사에 영어/중국어 병기 절대 금지.
3. 모든 사실 진술에 인용 마커 부착.
4. citation_ts 와 인용 마커의 HH:MM:SS 는 반드시 발화 목록에 있는 실제 시각을 사용.
5. 00:00:00 은 발화 목록에 실제로 [00:00:00] 줄이 있을 때만 사용.
6. 결정사항은 회의에서 **확정된 구체적 결론**만 추출한다. 다음은 결정이 아니므로 제외:
   - 결정 자체를 미루는 경우("나중에 정하자", "추후에 결론내자", "다음 회의로 넘기자", "자료를 더 모아 다시 논의하자")
   - 확정되지 않은 의견·아이디어·제안·브레인스토밍·질문
   ※ 단, "출시를 보류하기로 함", "채용을 동결하기로 함"처럼 **행동 방침이 확정된 경우는 결정**이다
     (행동의 보류·중단 자체가 확정된 결론이면 포함).
7. 하나의 안건에서 나온 결론은 1건의 결정으로 묶고, 세부 업무 분담은 follow_ups 로 넣는다(과분할 금지).
8. 결정인지 애매하면 제외하라(과추출보다 누락이 낫다)."""

# === TC (1차 6개 + false-positive/edge 4개 추가) ===
TCS = [
    {"id": "TC1_다중결정", "stress": "완전성", "meeting_id": "meeting_t1", "expected": 3,
     "transcript": """[00:00:08] SPEAKER_00: 오늘 세 가지 정합니다. 첫째, 배포 일정입니다.
[00:00:20] SPEAKER_01: 다음 주 화요일로 하시죠. 금요일 배포는 위험합니다.
[00:00:31] SPEAKER_00: 좋습니다, 배포는 다음 주 화요일로 확정합니다.
[00:00:45] SPEAKER_02: 둘째 안건, 로그 보관 기간입니다.
[00:00:58] SPEAKER_01: 90일로 늘리는 걸로 결정하죠. 컴플라이언스 요구입니다.
[00:01:10] SPEAKER_00: 네 로그 보관은 90일로 합니다.
[00:01:25] SPEAKER_02: 마지막으로 온콜 당번은 김도현 책임이 맡기로 합니다.
[00:01:34] SPEAKER_00: 동의합니다. 온콜은 김도현 책임으로 확정합니다."""},
    {"id": "TC2_결정없음", "stress": "환각저항", "meeting_id": "meeting_t2", "expected": 0,
     "transcript": """[00:00:10] SPEAKER_00: 신규 기능 아이디어 자유롭게 얘기해봅시다.
[00:00:22] SPEAKER_01: 다크모드 있으면 좋을 것 같아요.
[00:00:30] SPEAKER_02: 음성 검색도 재밌겠네요.
[00:00:41] SPEAKER_00: 둘 다 흥미롭네요. 일단 더 고민해보죠.
[00:00:52] SPEAKER_01: 네 결론은 다음에 내요."""},
    {"id": "TC3_번복상충", "stress": "최종결정", "meeting_id": "meeting_t3", "expected": 1,
     "transcript": """[00:00:09] SPEAKER_00: DB는 지난번에 MongoDB로 가기로 했었죠.
[00:00:18] SPEAKER_01: 그런데 트랜잭션 요구가 생겨서요. MongoDB는 애매합니다.
[00:00:30] SPEAKER_02: 그럼 PostgreSQL로 바꾸는 게 맞겠네요.
[00:00:39] SPEAKER_00: 네, 기존 MongoDB 결정을 뒤집고 PostgreSQL로 최종 결정합니다."""},
    {"id": "TC4_기술용어_고유명사", "stress": "병기금지", "meeting_id": "meeting_t4", "expected": 3,
     "transcript": """[00:00:11] SPEAKER_00: 인증은 OAuth 2.0으로 가고, 배미령 책임이 맡기로 결정합니다.
[00:00:24] SPEAKER_01: Kubernetes 클러스터는 EKS로 확정했고 박서준 님이 셋업합니다.
[00:00:37] SPEAKER_00: RAG 파이프라인 임베딩 모델은 e5-small로 결정합니다."""},
    {"id": "TC5_잡음_매장된결정", "stress": "인용정확도", "meeting_id": "meeting_t5", "expected": 1,
     "transcript": """[00:00:05] SPEAKER_00: 오늘 점심 뭐 먹을지부터... 농담이고요.
[00:00:14] SPEAKER_01: 어제 비 많이 왔죠. 출근 힘들었어요.
[00:00:25] SPEAKER_02: 그러게요. 아 그리고 주차장 공사한다네요.
[00:00:38] SPEAKER_00: 자 본론. 모바일 앱 출시는 일단 보류하기로 결정합니다. QA가 덜 됐어요.
[00:00:50] SPEAKER_01: 네 동의합니다. 다음 주말 날씨 좋다던데요.
[00:01:02] SPEAKER_02: 점심 이제 먹으러 가시죠."""},
    {"id": "TC6_액션아이템", "stress": "follow_up 묶기", "meeting_id": "meeting_t6", "expected": 1,
     "transcript": """[00:00:12] SPEAKER_00: 보안 감사는 다음 달 15일까지 완료하기로 결정합니다.
[00:00:25] SPEAKER_01: 침투 테스트는 제가, 김도현 책임이 코드 리뷰, 박서준 님이 리포트 작성으로 나누죠.
[00:00:40] SPEAKER_00: 좋습니다. 침투테스트는 SPEAKER_01, 코드리뷰 김도현 책임, 리포트 박서준 님. 마감 다음 달 15일."""},
    # --- 추가: false-positive / edge ---
    {"id": "TC7_보류결정(포함)", "stress": "보류=결정(과교정 방지)", "meeting_id": "meeting_t7", "expected": 1,
     "transcript": """[00:00:07] SPEAKER_00: 시장 상황이 안 좋습니다.
[00:00:15] SPEAKER_01: 이번 분기 신규 채용은 어떻게 할까요.
[00:00:24] SPEAKER_00: 신규 채용은 이번 분기 동결하기로 결정합니다.
[00:00:33] SPEAKER_02: 알겠습니다, 동결로 진행하겠습니다."""},
    {"id": "TC8_의견질문(없음)", "stress": "의견·질문≠결정", "meeting_id": "meeting_t8", "expected": 0,
     "transcript": """[00:00:09] SPEAKER_00: 신규 결제 수단 도입은 어떻게 생각하세요?
[00:00:19] SPEAKER_01: 개인적으론 좋다고 봅니다. 사용자 요청이 많아요.
[00:00:28] SPEAKER_02: 저는 보안 검토가 먼저라고 생각해요.
[00:00:37] SPEAKER_00: 의견 잘 들었습니다. 참고하겠습니다."""},
    {"id": "TC9_조건부결정(포함)", "stress": "조건부지만 확정", "meeting_id": "meeting_t9", "expected": 1,
     "transcript": """[00:00:10] SPEAKER_00: 베타 피드백이 긍정적이면 정식 출시로 갑니다.
[00:00:21] SPEAKER_01: 기준은요?
[00:00:27] SPEAKER_00: 만족도 80% 이상이면 정식 출시하기로 결정합니다."""},
    {"id": "TC10_안건연기(없음)", "stress": "안건 연기=결정아님", "meeting_id": "meeting_t10", "expected": 0,
     "transcript": """[00:00:08] SPEAKER_00: 내년 예산 안건 보겠습니다.
[00:00:16] SPEAKER_01: 아직 재무 데이터가 안 나왔습니다.
[00:00:25] SPEAKER_00: 그럼 오늘은 결정하지 않고, 자료 모아서 다음에 다시 보겠습니다.
[00:00:34] SPEAKER_02: 네 그게 좋겠습니다."""},
]

_TS_RE = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]")
_CITE_RE = re.compile(r"@(\d{2}:\d{2}:\d{2})")
_GLOSS_RE = re.compile(r"[가-힣]\s*[\(\[（][A-Za-z一-鿿]")
_THINK_RE = [re.compile(r"<\|channel\|?>\s*thought.*?<\|?channel\|?>", re.DOTALL | re.IGNORECASE),
             re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)]


def strip_thought(t: str) -> str:
    for rx in _THINK_RE:
        t = rx.sub("", t)
    return t.strip()


def parse_json_array(text: str):
    t = re.sub(r"```(json)?|```", "", strip_thought(text)).strip()
    for cand in (t, t[t.find("["): t.rfind("]") + 1] if "[" in t and "]" in t else ""):
        try:
            v = json.loads(cand)
            if isinstance(v, list):
                return v
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def score(tc: dict, raw: str) -> dict:
    valid_ts = set(_TS_RE.findall(tc["transcript"]))
    arr = parse_json_array(raw)
    clean = strip_thought(raw)
    # 인용 마커 [meeting:...] 는 한글 뒤에 와도 병기가 아니므로 제외하고 병기 카운트
    gloss = len(_GLOSS_RE.findall(re.sub(r"\[meeting:[^\]]*\]", "", clean)))
    cites = _CITE_RE.findall(clean)
    cite_invalid = sum(1 for c in cites if c not in valid_ts)
    if arr is None:
        return {"json_valid": False, "n": None, "expected": tc["expected"], "count_ok": False,
                "cite_invalid": cite_invalid, "gloss": gloss, "keys_ok": False}
    n = len(arr)
    keys_ok = all(isinstance(x, dict) and {"title", "decision_text", "confidence"} <= set(x) for x in arr)
    return {"json_valid": True, "n": n, "expected": tc["expected"], "count_ok": n == tc["expected"],
            "cite_invalid": cite_invalid, "gloss": gloss, "keys_ok": keys_ok,
            "pass": (n == tc["expected"] and cite_invalid == 0 and gloss == 0 and keys_ok),
            "items": arr}


def _gen_e4b(prompt_text: str):
    from mlx_vlm import generate as g, load
    model, proc = load("mlx-community/gemma-4-e4b-it-4bit")
    tok = proc.tokenizer

    def fn(tc, temp):
        user = f"회의 ID: {tc['meeting_id']}\n발화 목록:\n{tc['transcript']}\n\n위 컨텍스트에서 결정사항을 JSON 배열로 추출하세요."
        prm = tok.apply_chat_template(
            [{"role": "system", "content": prompt_text}, {"role": "user", "content": user}],
            tokenize=False, add_generation_prompt=True)
        return g(model, proc, prompt=prm, max_tokens=900, temperature=temp, verbose=False).text
    return fn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="e4b", choices=["e4b"])
    ap.add_argument("--prompt", default="new", choices=["new", "old"])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--temp", type=float, default=0.0)
    args = ap.parse_args()

    prompt_text = WIKI_SYSTEM_NEW if args.prompt == "new" else WIKI_SYSTEM_OLD
    gen = _gen_e4b(prompt_text)

    print(f"{'='*82}\n  E4B Wiki 추출 — 프롬프트={args.prompt} | runs={args.runs} | temp={args.temp}\n{'='*82}")
    print(f"  {'TC':<24}{'정답':>5}{'  실행별 결정수':<16}{'일관성':>8}{'PASS율':>8}")
    results = []
    agg_pass = 0
    for tc in TCS:
        runs = []
        t0 = time.perf_counter()
        for _ in range(args.runs):
            raw = gen(tc, args.temp)
            runs.append(score(tc, raw))
        ns = [r["n"] if r["json_valid"] else "X" for r in runs]
        passes = sum(1 for r in runs if r.get("pass"))
        consistent = len(set(map(str, ns))) == 1
        pass_rate = passes / args.runs
        if pass_rate == 1.0:
            agg_pass += 1
        mark = "✅" if pass_rate == 1.0 else ("⚠️" if pass_rate > 0 else "❌")
        print(f"  {tc['id']:<24}{tc['expected']:>5}  {str(ns):<14}{'동일' if consistent else '변동':>8}"
              f"{mark}{int(pass_rate*100):>4}%  ({round(time.perf_counter()-t0,1)}s)")
        results.append({"tc": tc["id"], "expected": tc["expected"], "runs": runs,
                        "consistent": consistent, "pass_rate": pass_rate})
    n = len(TCS)
    print(f"\n  종합: 전(全)실행 PASS {agg_pass}/{n} TC | (프롬프트={args.prompt})")

    out = Path(__file__).resolve().parent / f"eval_wiki_e4b_{args.prompt}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  저장: {out}")


if __name__ == "__main__":
    main()
