"""DecisionExtractor — 회의에서 결정사항을 추출하여 decisions/{date}-{slug}.md 페이지로 변환.

목적: PRD §4.2 decisions 템플릿 + §5.4 페이지 갱신 프롬프트를 결합한 추출기.
WikiCompilerV2 가 회의 ingest 시 호출하며, 다음 두 단계로 동작한다:

    1. extract(meeting_id, summary, utterances)
       → ExtractedDecision 리스트 (LLM 1회 호출, JSON 응답).
    2. render_pages(decisions, store)
       → [(rel_path, new_content), ...] 디스크 갱신 후보.

의존성:
    - core.wiki.llm_client.WikiLLMClient (LLM 추상화)
    - core.wiki.models.Citation
    - core.wiki.store.WikiStore (read_page 만 사용)
    - core.wiki.schema.render_decision_template (신규 페이지 초기 텍스트)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

from config import WikiGroundingConfig
from core.wiki.decision_record import DecisionRecord
from core.wiki.llm_client import WikiLLMClient, sanitize_utterance_text
from core.wiki.models import Citation

logger = logging.getLogger(__name__)


# PRD R3 리스크 대응: 회의당 페이지 갱신 상한.
# 한 회의에서 너무 많은 결정사항이 동시에 잡히면 LLM 호출 비용/시간이 폭증하므로
# confidence 상위 N건만 처리한다. 8건은 PRD §5.4 + Reviewer Phase 2.D 합의값.
_MAX_PAGES_PER_MEETING: int = 8


# ─────────────────────────────────────────────────────────────────────────
# 3.1 데이터 모델
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ActionItemRef:
    """결정사항에 딸린 후속 액션의 경량 참조.

    Attributes:
        owner: 담당자 이름.
        description: 액션 한 줄 설명.
        citation: 후속 발언이 등장한 인용 (timestamp).
    """

    owner: str
    description: str
    citation: Citation | None


@dataclass
class ExtractedDecision:
    """LLM 추출 결과 — 1차 가공 단계.

    Attributes:
        title: 페이지 제목.
        slug: 파일명에 들어갈 URL-safe 식별자.
        decision_text: "## 결정 내용" 섹션 본문 (인용 포함).
        background: "## 배경" 섹션 본문 (인용 포함).
        follow_ups: 후속 액션 메모.
        participants: 입력 회의 원문의 전체 화자 라벨. 개별 결정 동의자나 실명 확인을 뜻하지 않는다.
        projects: 관련 프로젝트 slug 목록.
        citations: 본 결정에 포함된 모든 Citation 평탄화 목록.
        confidence: LLM 자체 평가 0~10 정수.
    """

    title: str
    slug: str
    decision_text: str
    background: str
    follow_ups: list[ActionItemRef] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    confidence: int = 0
    citation_unresolved: bool = False
    unresolved_citation_fields: tuple[str, ...] = ()


# ─────────────────────────────────────────────────────────────────────────
# 3.2 헬퍼
# ─────────────────────────────────────────────────────────────────────────

# 한국어 고유명사 뒤 외국어 병기 패턴 — "배미령(Baimilong)" 형태 제거.
#
# Gemma 4 가 자주 만드는 병기 패턴 차단:
#   1. 영어 병기: "배미령(Baimilong)", "배미령(Bea Mi-ryeong)"
#   2. 한자 병기: "배미령(裵美玲)" (Gemma 다국어 특성상 한자 병기 빈번)
#   3. 일본어 병기: "배미령(ベミリョン)", "배미령(ばいみりょん)" (가타카나/히라가나)
#   4. 공백 변형: "배미령 (Baimilong)" — 한국어 뒤 공백 0~1개 허용
#
# 매칭 조건:
#   - 괄호 앞에 한국어 글자(최소 1자) 가 있어야 함 → "Project A(프로젝트)" 는 비매칭
#   - 괄호 안 첫 글자가 라틴/한자/가타카나/히라가나 중 하나여야 함
#   - 괄호 안 내용에는 같은 종류 + 공백/하이픈 허용
#   - "배미령(백미령)" 같이 괄호 안이 한국어이면 별칭 표기로 보존 (비매칭)
#   - "API(Application Programming Interface)" 같은 영영 병기: 앞이 한국어가 아니므로 비매칭 → 보존
#
# 유니코드 범위:
#   - 한국어 한글: U+AC00..U+D7A3
#   - CJK 통합 한자: U+4E00..U+9FFF
#   - 일본어 히라가나: U+3040..U+309F
#   - 일본어 가타카나: U+30A0..U+30FF
_FOREIGN_GLOSS_PATTERN: re.Pattern[str] = re.compile(
    r"([\uAC00-\uD7A3]+)\s?"
    r"\("
    r"([A-Za-z\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF]"
    r"[A-Za-z\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF\s\-]*)"
    r"\)"
)

# 인용 마커 추출 (citations.CITATION_PATTERN 과 동일)
_CITATION_PATTERN: re.Pattern[str] = re.compile(
    r"\[meeting:([A-Za-z0-9_]+)@(\d{2}):(\d{2}):(\d{2})\]"
)
_CITATION_LIKE_PATTERN = re.compile(r"\[meeting:[^\]\r\n]*\]")

# slug 정규화 — 한국어/공백을 제거하고 영문/숫자/하이픈만 남김
_SLUG_INVALID: re.Pattern[str] = re.compile(r"[^a-z0-9\-_]+")


def _strip_paren_latin(text: str) -> str:
    """한국어 고유명사 뒤 영어/중국어 병기를 제거한다.

    예: "배미령(Baimilong)" → "배미령"

    Args:
        text: 원본 텍스트.

    Returns:
        병기가 제거된 텍스트.
    """
    if not text:
        return text
    return _FOREIGN_GLOSS_PATTERN.sub(r"\1", text)


def _normalize_slug(raw: str, fallback: str = "decision") -> str:
    """제목/임의 문자열을 filename-safe slug 로 정규화한다.

    동작:
        1. 소문자화.
        2. 공백·구두점을 하이픈으로 치환.
        3. 한국어 등 비-ASCII 문자는 제거.
        4. 결과가 비면 fallback 반환.

    Args:
        raw: 원본 문자열.
        fallback: 정규화 결과가 비었을 때 사용할 기본값.

    Returns:
        영문 소문자/숫자/하이픈/언더스코어로만 구성된 문자열.
    """
    if not raw:
        return fallback
    # 소문자화 + 공백 → 하이픈
    s = raw.strip().lower()
    s = re.sub(r"\s+", "-", s)
    # 비-ASCII / 비-허용 문자 제거
    s = _SLUG_INVALID.sub("", s)
    # 연속 하이픈 정리
    s = re.sub(r"-+", "-", s).strip("-_")
    return s or fallback


def _citation_from_ts(meeting_id: str, ts_str: str) -> Citation | None:
    """HH:MM:SS 형태의 timestamp 를 Citation 으로 변환한다.

    Args:
        meeting_id: 실제 회의 ID 또는 하위 호환 8자리 hex.
        ts_str: "HH:MM:SS" 문자열.

    Returns:
        Citation 인스턴스. 형식 불량 시 None.
    """
    if not ts_str:
        return None
    parts = ts_str.split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return Citation(
        meeting_id=meeting_id,
        timestamp_str=f"{h:02d}:{m:02d}:{s:02d}",
        timestamp_seconds=h * 3600 + m * 60 + s,
    )


def _utterance_time_range(utterance: Any) -> tuple[float, float] | None:
    """실제 발화의 유효한 시작·종료 시각만 반환하며 누락 시 0초를 만들지 않는다."""
    try:
        if isinstance(utterance, dict):
            raw_start, raw_end = utterance.get("start"), utterance.get("end")
        else:
            raw_start, raw_end = utterance.start, utterance.end
        if (
            raw_start is None
            or raw_end is None
            or isinstance(raw_start, bool)
            or isinstance(raw_end, bool)
        ):
            return None
        start, end = float(raw_start), float(raw_end)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
        return None
    return start, end


def source_citation_markers(meeting_id: str, utterances: list[Any]) -> set[str]:
    """유효한 실제 입력 발화의 시작 시각으로 제공할 수 있는 마커만 반환한다."""
    if re.fullmatch(r"[A-Za-z0-9_]+", meeting_id) is None:
        return set()
    markers: set[str] = set()
    for utt in utterances:
        time_range = _utterance_time_range(utt)
        if time_range is None:
            continue
        text = utt.get("text") if isinstance(utt, dict) else getattr(utt, "text", None)
        if not isinstance(text, str) or not sanitize_utterance_text(text):
            continue
        total = int(time_range[0])
        timestamp = f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
        markers.add(f"[meeting:{meeting_id}@{timestamp}]")
    return markers


def _is_claim_line(line: str) -> bool:
    """빈 줄과 명시적인 빈 값 표기를 제외한 주장 줄인지 확인한다."""
    text = line.strip().lstrip("- ").strip()
    return bool(text) and text not in {"없음", "없음.", "_(없음)_", "N/A", "n/a"}


def _line_has_input_citations(line: str, allowed: set[str]) -> bool:
    """주장 줄의 모든 마커가 실제 입력에 제공된 정확한 마커인지 검사한다."""
    markers = _CITATION_LIKE_PATTERN.findall(line)
    return (
        bool(markers)
        and line.count("[meeting:") == len(markers)
        and all(marker in allowed for marker in markers)
    )


def _decision_field_texts(decision: ExtractedDecision) -> dict[str, str]:
    """다른 필드의 인용을 빌리지 않고 각 주장 필드의 원문을 읽는다."""
    fields = {"decision_text": decision.decision_text, "background": decision.background}
    for index, follow_up in enumerate(decision.follow_ups):
        text = follow_up.description
        if follow_up.citation is not None:
            marker = (
                f"[meeting:{follow_up.citation.meeting_id}@{follow_up.citation.timestamp_str}]"
            )
            if marker not in text:
                text = f"{text} {marker}"
        fields[f"follow_ups.{index}"] = text
    return fields


def _repair_terms(text: str) -> set[str]:
    """인용 표기를 제외한 한글·영문 토큰과 문자 ngram으로 관련 발화를 찾는다."""
    text = _CITATION_LIKE_PATTERN.sub("", text).lower()
    words = re.findall(r"[가-힣a-z0-9]+", text)
    terms = {f"word:{word}" for word in words if len(word) > 1}
    for word in words:
        for width in (2, 3):
            terms.update(
                f"gram:{word[index : index + width]}" for index in range(len(word) - width + 1)
            )
    return terms


def _select_repair_evidence(
    lines: list[str], claims: list[dict[str, Any]], config: WikiGroundingConfig
) -> list[str]:
    """예산 안에서 주장별 관련 턴과 바로 앞뒤 턴을 원래 순서로 고르게 선택한다."""
    full_prompt = json.dumps({"utterances": lines, "claims": claims}, ensure_ascii=False)
    if len(full_prompt) <= config.max_input_chars and all(
        len(line) <= config.max_item_chars for line in lines
    ):
        return lines
    base_size = len(json.dumps({"utterances": [], "claims": claims}, ensure_ascii=False))
    available = config.max_input_chars - base_size
    if available <= 0:
        return []
    costs = {
        index: len(json.dumps(line, ensure_ascii=False)) + 2
        for index, line in enumerate(lines)
        if len(line) <= config.max_item_chars
    }
    source_terms = {
        index: _repair_terms(line.partition("]")[2].partition(":")[2])
        for index, line in enumerate(lines)
        if index in costs
    }
    rankings: list[list[int]] = []
    for claim in claims:
        terms = _repair_terms(str(claim["text"]))
        scored = [
            (len(terms & other) / len(terms | other), index)
            for index, other in source_terms.items()
            if terms & other
        ]
        rankings.append(
            [index for _score, index in sorted(scored, key=lambda item: (-item[0], item[1]))]
        )
    selected: set[int] = set()
    anchors: list[list[int]] = [[] for _ in claims]
    spent = 0

    def add(index: int, *, limit: int | None = None) -> bool:
        """완전한 발화만 추가하고 JSON 전체의 예산을 유지한다."""
        nonlocal spent
        if index in selected:
            return True
        cost = costs.get(index)
        if cost is None or spent + cost > available or (limit is not None and cost > limit):
            return False
        selected.add(index)
        spent += cost
        return True

    # 한 주장에 예산을 먼저 소진하지 않도록 첫 근거는 같은 몫으로 우선 배정한다.
    active = sum(bool(ranking) for ranking in rankings)
    share = available // active if active else 0
    for index, ranking in enumerate(rankings):
        anchor = next((item for item in ranking if add(item, limit=share)), None)
        if anchor is not None:
            anchors[index].append(anchor)
    # 긴 턴 때문에 첫 몫에 들어가지 못한 주장도 남은 예산에서 한 번씩 기회를 준다.
    for index, ranking in enumerate(rankings):
        if not anchors[index]:
            anchor = next((item for item in ranking if add(item)), None)
            if anchor is not None:
                anchors[index].append(anchor)

    def add_neighbors(round_anchors: list[int]) -> None:
        """선정한 발화의 질문·확답 문맥을 완전한 이웃 턴으로 보존한다."""
        for direction in (-1, 1):
            for anchor in round_anchors:
                add(anchor + direction)

    add_neighbors([items[0] for items in anchors if items])
    # 남는 공간은 다음 관련 발화를 주장별 순환으로 추가한다.
    for rank in range(max((len(items) for items in rankings), default=0)):
        round_anchors = []
        for index, ranking in enumerate(rankings):
            if rank < len(ranking):
                anchor = ranking[rank]
                if anchor not in anchors[index] and add(anchor):
                    anchors[index].append(anchor)
                    round_anchors.append(anchor)
        add_neighbors(round_anchors)
    return [lines[index] for index in sorted(selected)]


def _update_citation_state(decision: ExtractedDecision, allowed: set[str]) -> None:
    """필드별 누락·잘못된 인용과 실제 본문 인용 목록을 갱신한다."""
    fields = _decision_field_texts(decision)
    unresolved = tuple(
        key
        for key, text in fields.items()
        if any(
            _is_claim_line(line) and not _line_has_input_citations(line, allowed)
            for line in text.splitlines()
        )
    )
    if not _is_claim_line(decision.decision_text) and "decision_text" not in unresolved:
        unresolved = ("decision_text", *unresolved)
    decision.unresolved_citation_fields = unresolved
    decision.citation_unresolved = bool(unresolved)
    decision.citations = [
        citation for text in fields.values() for citation in _extract_citations_from_text(text)
    ]


def _extract_citations_from_text(text: str) -> list[Citation]:
    """텍스트에서 인용 마커를 모두 추출하여 Citation 리스트로 반환한다.

    Args:
        text: 검사 대상 문자열.

    Returns:
        Citation 인스턴스 리스트 (등장 순서).
    """
    results: list[Citation] = []
    if not text:
        return results
    for match in _CITATION_PATTERN.finditer(text):
        meeting_id = match.group(1)
        hh, mm, ss = match.group(2), match.group(3), match.group(4)
        results.append(
            Citation(
                meeting_id=meeting_id,
                timestamp_str=f"{hh}:{mm}:{ss}",
                timestamp_seconds=int(hh) * 3600 + int(mm) * 60 + int(ss),
            )
        )
    return results


def _extract_json_array(text: str) -> list[Any] | None:
    """LLM 응답에서 JSON 배열을 robust 하게 파싱한다.

    동작:
        1. 전체 텍스트로 json.loads 시도.
        2. 실패 시 첫 번째 `[` 와 마지막 `]` 사이를 추출하여 재시도.

    Args:
        text: LLM 원시 응답.

    Returns:
        파싱된 리스트 또는 None (실패 시).
    """
    if not text:
        return None
    text = text.strip()
    # 1차: 전체 파싱
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    # 2차: 첫 [ ~ 마지막 ] 추출
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    snippet = text[start : end + 1]
    try:
        result = json.loads(snippet)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _build_extracted_decision(
    item: dict[str, Any],
    meeting_id: str,
) -> ExtractedDecision:
    """LLM 응답의 dict 항목을 ExtractedDecision 으로 변환한다.

    Args:
        item: LLM JSON 배열 단일 항목.
        meeting_id: 회의 ID (Citation 변환 시 사용).

    Returns:
        ExtractedDecision 인스턴스.
    """
    title = str(item.get("title", "")).strip()
    decision_text = str(item.get("decision_text", "")).strip()
    background = str(item.get("background", "")).strip()
    confidence = int(item.get("confidence", 0)) if item.get("confidence") is not None else 0

    participants_raw = item.get("participants") or []
    projects_raw = item.get("projects") or []
    participants = [str(p) for p in participants_raw if p]
    projects = [str(p) for p in projects_raw if p]

    # follow_ups 변환
    follow_ups: list[ActionItemRef] = []
    for fu in item.get("follow_ups") or []:
        if not isinstance(fu, dict):
            continue
        owner = str(fu.get("owner", "") or "")
        desc = str(fu.get("description", "") or "")
        ts = str(fu.get("citation_ts", "") or "")
        cit = _citation_from_ts(meeting_id, ts)
        if owner and desc:
            follow_ups.append(ActionItemRef(owner=owner, description=desc, citation=cit))

    # citations: decision_text + background 에서 평탄화 추출
    flat_citations = _extract_citations_from_text(decision_text) + _extract_citations_from_text(
        background
    )
    flat_citations.extend(fu.citation for fu in follow_ups if fu.citation is not None)

    # slug: title 기반 정규화. 비-ASCII 만 있으면 hex 해시로 폴백
    slug = _normalize_slug(title)
    if slug == "decision":
        # 한국어 제목 등 정규화 결과가 비면 hash 기반 fallback
        import hashlib

        h = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8] if title else "untitled"
        slug = f"decision-{h}"

    return ExtractedDecision(
        title=title,
        slug=slug,
        decision_text=decision_text,
        background=background,
        follow_ups=follow_ups,
        participants=participants,
        projects=projects,
        citations=flat_citations,
        confidence=confidence,
    )


# ─────────────────────────────────────────────────────────────────────────
# 3.3 시스템 프롬프트
# ─────────────────────────────────────────────────────────────────────────

_EXTRACT_SYSTEM_PROMPT = """\
당신은 회의록에서 결정사항(decisions) 만 추출하는 분석가입니다.
출력은 반드시 JSON 배열이어야 하며, 각 항목은 다음 키를 포함합니다:
- title: 한 줄 요약 (한국어)
- decision_text: 결정 본문 (인용 마커 [meeting:{제공된 회의 ID}@HH:MM:SS] 필수)
- background: 배경 설명 (직접 근거가 있으면 인용 마커 필수, 추론만 가능하면 빈 문자열)
- follow_ups: [{owner, description, citation_ts}, ...] (없으면 빈 배열). 한 결정에 딸린 업무 분담은 별도 결정이 아니라 반드시 이 follow_ups 에 넣는다.
- participants: 화자 이름 배열
- projects: 프로젝트 slug 배열
- confidence: 0~10 정수

규칙:
1. 결정사항이 없으면 빈 배열 [] 만 출력.
2. 한국어 고유명사에 영어/중국어 병기 절대 금지.
3. 모든 사실 진술에 근거 발화 줄 맨 앞의 인용 마커 전체를 그대로 복사해 부착한다.
4. decision_text와 background에는 [meeting:실제회의ID@HH:MM:SS] 전체를 직접 복사한다. 회의 ID나 시간을 새로 조합·추정하지 않는다. follow_ups의 citation_ts는 해당 마커의 HH:MM:SS 부분만 그대로 복사한다.
5. 00:00:00은 발화 목록에 그 시각의 인용 마커가 실제로 있을 때만 사용하고, 추정 기본값으로 쓰지 말 것. 요약만 있고 근거 발화가 없는 내용은 추출하지 않는다.
6. 배경은 발화에서 직접 근거를 찾을 수 있을 때만 작성하고, 추론만 가능하면 빈 문자열로 둘 것.
7. 결정사항은 회의에서 **확정된 구체적 결론**만 추출한다. 다음은 결정이 아니므로 제외:
   - 결정 자체를 미루는 경우("나중에 정하자", "추후에 결론내자", "다음 회의로 넘기자", "자료를 더 모아 다시 논의하자")
   - 확정되지 않은 의견·아이디어·제안·브레인스토밍·질문
   ※ 단, "출시를 보류하기로 함", "채용을 동결하기로 함"처럼 행동 방침 자체가 확정된 경우는 결정이다.
8. 하나의 안건에서 나온 결론은 1건의 결정으로 묶고, 세부 업무 분담은 별도 결정이 아니라 follow_ups 로 넣는다(과분할 금지).
9. 결정인지 애매하면 제외하라(과추출보다 누락이 낫다).
10. 상태·가능성·동의를 묻는 질문만으로 결정이나 합의가 있었다고 추론하지 않는다. 짧은 질문을 인용하고 그 답을 확정된 사실처럼 작성하지 않는다. 대상과 결론을 밝힌 명시적인 답변이나 확정 발언이 필요하다.
11. 조건부 계획은 조건을 유지하고, 가능성·검토·예정이라는 말의 강도를 확정·실행·합의로 높이지 않는다. 기존 방식에 대한 설명도 새 합의가 아니다. 조건을 붙인 방침 자체를 명시적으로 정한 경우에만 그 조건을 빠짐없이 포함한다.
12. 질문과 명시적인 답변을 함께 읽어야 결론을 알 수 있으면 decision_text의 해당 문장에 양쪽 발화의 전체 인용 마커를 모두 복사한다. 한 문장에 복수 인용을 붙일 수 있다. 질문 인용 하나로 답변 내용을 대신하지 말고, 모호한 단답만 있으면 제외한다.
"""

_REPAIR_CITATION_SYSTEM_PROMPT = """\
주어진 주장에 대응하는 실제 발화의 인용 마커만 연결하세요.
주장·조건·담당자·날짜·요약을 수정하거나 새 내용을 만들지 마세요.
근거가 있는 경우에만 입력 발화 줄 앞의 완전한 [meeting:ID@HH:MM:SS] 마커를 복사하세요.
질문과 명시적인 답변이 함께 필요하면 양쪽 마커를 모두 포함하세요.
가장 첫 발화나 가까운 시각을 근거 없이 선택하지 마세요. 근거가 불충분하면 citations: []로 두세요.
출력은 JSON 배열만 허용합니다. 각 항목은 {"id": 입력 주장 id, "citations": [전체 마커, ...]}입니다.
다른 필드나 주장의 본문을 출력하지 마세요.
"""

# ─────────────────────────────────────────────────────────────────────────
# 3.4 추출기
# ─────────────────────────────────────────────────────────────────────────


class DecisionExtractor:
    """회의 컨텍스트에서 결정사항을 LLM 으로 추출하고 페이지 후보를 렌더링한다."""

    def __init__(
        self, llm: WikiLLMClient, *, repair_config: WikiGroundingConfig | None = None
    ) -> None:
        """LLM과 기존 근거 검수 예산을 받아 인용 복구를 제한한다.

        Args:
            llm: WikiLLMClient (실구현 또는 mock).
        """
        self._llm: WikiLLMClient = llm
        self._repair_config = repair_config or WikiGroundingConfig()

    async def _repair_missing_citations(
        self, decisions: list[ExtractedDecision], lines: list[str], allowed: set[str]
    ) -> None:
        """누락된 주장들을 한 번만 묶어 대조하고 본문 변경 없이 마커만 부착한다."""
        targets: list[tuple[int, str, int]] = []
        field_lines: dict[tuple[int, str], list[str]] = {}
        claims: list[dict[str, Any]] = []
        for decision_index, decision in enumerate(decisions):
            _update_citation_state(decision, allowed)
            for key, text in _decision_field_texts(decision).items():
                values = text.splitlines(keepends=True)
                field_lines[(decision_index, key)] = values
                for line_index, line in enumerate(values):
                    if not _is_claim_line(line) or _line_has_input_citations(line, allowed):
                        continue
                    claims.append(
                        {"id": len(targets), "text": _CITATION_LIKE_PATTERN.sub("", line).strip()}
                    )
                    targets.append((decision_index, key, line_index))
        if not targets:
            return
        config = self._repair_config
        if len(targets) > config.max_claims or any(
            len(str(claim["text"])) > config.max_item_chars for claim in claims
        ):
            logger.warning("Decision citation 복구 보류: reason=item_or_claim_limit")
            return
        evidence_lines = _select_repair_evidence(lines, claims, config)
        prompt = json.dumps({"utterances": evidence_lines, "claims": claims}, ensure_ascii=False)
        if not evidence_lines or len(prompt) > config.max_input_chars:
            logger.warning("Decision citation 복구 보류: reason=input_limit")
            return
        repair_markers = {line.partition(" ")[0] for line in evidence_lines}
        try:
            raw = await asyncio.wait_for(
                self._llm.generate(
                    system_prompt=_REPAIR_CITATION_SYSTEM_PROMPT,
                    user_prompt=prompt,
                    max_tokens=config.max_output_tokens,
                    temperature=0.0,
                ),
                timeout=config.timeout_seconds,
            )
        except TimeoutError:
            logger.warning("Decision citation 복구 보류: reason=timeout")
            return
        except Exception as exc:  # noqa: BLE001 — 취소는 전파하고 복구 실패는 원문을 보존한다.
            logger.warning("Decision citation 복구 보류: reason=%s", type(exc).__name__)
            return
        if not isinstance(raw, str) or len(raw) > config.max_input_chars:
            return
        rows = _extract_json_array(raw)
        if rows is None:
            return
        mappings: dict[int, list[str]] = {}
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"id", "citations"}:
                return
            index, markers = row["id"], row["citations"]
            if type(index) is not int or not 0 <= index < len(targets) or index in seen:
                return
            seen.add(index)
            if not isinstance(markers, list) or any(
                not isinstance(marker, str) or marker not in repair_markers for marker in markers
            ):
                return
            if markers:
                mappings[index] = list(dict.fromkeys(markers))
        changed: set[tuple[int, str]] = set()
        for index, markers in mappings.items():
            decision_index, key, line_index = targets[index]
            values = field_lines[(decision_index, key)]
            original = values[line_index]
            ending = original[len(original.rstrip("\r\n")) :]
            text = _CITATION_LIKE_PATTERN.sub("", original.rstrip("\r\n")).rstrip()
            values[line_index] = f"{text} {' '.join(markers)}{ending}"
            changed.add((decision_index, key))
        for decision_index, key in changed:
            decision = decisions[decision_index]
            text = "".join(field_lines[(decision_index, key)])
            if key in {"decision_text", "background"}:
                setattr(decision, key, text)
            else:
                follow_up_index = int(key.partition(".")[2])
                decision.follow_ups[follow_up_index] = replace(
                    decision.follow_ups[follow_up_index], description=text, citation=None
                )
        for decision in decisions:
            _update_citation_state(decision, allowed)

    async def extract(
        self,
        *,
        meeting_id: str,
        meeting_date: date,
        summary: str,
        utterances: list,
    ) -> list[ExtractedDecision]:
        """LLM 1회 호출로 결정사항 후보 목록을 JSON 으로 추출한다.

        흐름:
            1. utterances 가 비면 LLM 호출 없이 빈 리스트 반환.
            2. 유효한 발화를 "[meeting:{실제ID}@HH:MM:SS] 화자: text"로 직렬화한다.
            3. system_prompt + user_prompt 조립.
            4. llm.generate() 호출. JSON 파싱 — 실패 시 1회 재시도.
            5. 항목별로 ExtractedDecision 변환. 화자 목록은 모델 응답 대신 입력에서 정한다.

        Args:
            meeting_id: 회의 ID.
            meeting_date: 회의 날짜.
            summary: 8단계 결과 마크다운.
            utterances: 5단계 corrector 출력 (CorrectedUtterance 또는 dict 호환).

        Returns:
            ExtractedDecision 리스트. 결정사항이 없거나 파싱 실패 시 빈 리스트.
        """
        # 빈 utterances → LLM 호출 없이 즉시 빈 리스트
        if not utterances:
            return []
        if re.fullmatch(r"[A-Za-z0-9_]+", meeting_id) is None:
            logger.warning("DecisionExtractor: 유효하지 않은 회의 ID로 추출을 건너뜁니다.")
            return []

        # utterances 직렬화
        lines: list[str] = []
        source_participants: set[str] = set()
        for utt in utterances:
            raw_speaker = (
                utt.get("speaker") if isinstance(utt, dict) else getattr(utt, "speaker", None)
            )
            if isinstance(raw_speaker, str) and raw_speaker.strip():
                source_participants.add(raw_speaker.strip())
            time_range = _utterance_time_range(utt)
            if time_range is None:
                continue
            raw_text = utt.get("text", "") if isinstance(utt, dict) else getattr(utt, "text", "")
            if not isinstance(raw_text, str):
                continue
            text = sanitize_utterance_text(raw_text)
            if not text:
                continue
            speaker = sanitize_utterance_text(str(raw_speaker or "UNKNOWN"))
            ts_str = self._seconds_to_hhmmss(time_range[0])
            lines.append(f"[meeting:{meeting_id}@{ts_str}] {speaker}: {text}")
        if not lines:
            return []

        user_prompt = (
            f"회의 ID: {meeting_id}\n"
            f"회의 날짜: {meeting_date.isoformat()}\n\n"
            f"## 8단계 요약\n{summary}\n\n"
            f"## 발화 목록\n" + "\n".join(lines) + "\n\n"
            "근거 발화 줄 맨 앞의 완전한 인용 마커를 결정 문장에 그대로 복사하세요. "
            "근거가 없으면 결정사항으로 만들지 마세요. 위 컨텍스트에서 결정사항을 JSON 배열로 추출하세요."
        )

        # 1차 호출
        try:
            raw = await self._llm.generate(
                system_prompt=_EXTRACT_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("DecisionExtractor.extract 1차 호출 실패: %r", exc)
            raise

        parsed = _extract_json_array(raw)

        # 2차 재시도 — 파싱 실패 시
        if parsed is None:
            logger.warning("DecisionExtractor: 1차 JSON 파싱 실패, 1회 재시도")
            try:
                raw = await self._llm.generate(
                    system_prompt=_EXTRACT_SYSTEM_PROMPT,
                    user_prompt=user_prompt + "\n\n반드시 JSON 배열만 출력하세요.",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("DecisionExtractor.extract 재시도 실패: %r", exc)
                return []
            parsed = _extract_json_array(raw)

        if parsed is None:
            return []

        results: list[ExtractedDecision] = []
        participants = sorted(source_participants)
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                results.append(
                    _build_extracted_decision({**item, "participants": participants}, meeting_id)
                )
            except Exception as exc:  # noqa: BLE001 — 항목별 실패는 skip
                logger.warning("DecisionExtractor: 항목 변환 실패 — skip: %r", exc)
                continue

        await self._repair_missing_citations(
            results, lines, source_citation_markers(meeting_id, utterances)
        )
        return results

    async def render_pages(
        self,
        *,
        decisions: list[ExtractedDecision],
        meeting_id: str,
        meeting_date: date,
        existing_store: Any,
    ) -> list[tuple[str, str]]:
        """ExtractedDecision 목록을 디스크 쓰기 후보 [(rel_path, content)] 로 변환.

        결정적 렌더링:
            - confidence 내림차순 정렬 후 상위 _MAX_PAGES_PER_MEETING(=8) 건만 처리.
            - 추출 결과를 canonical DecisionRecord로 변환하며 모델을 다시 호출하지 않는다.
            - 인용이 없는 후보는 skip 하되 다른 페이지 생성에는 영향 없음.

        흐름 (각 decision 별):
            1. rel_path 결정 — `decisions/{YYYY-MM-DD}-{slug}.md`.
            2. existing_store.read_page(rel_path) 시도 — 기존 페이지 컨텍스트 확보.
            3. ExtractedDecision 을 canonical DecisionRecord markdown 으로 변환.
            4. 한국어 고유명사 영문 병기 후처리 제거.
            5. 기존 페이지가 있으면 created_at 보존.

        Args:
            decisions: extract() 결과.
            meeting_id: 인용 검증용.
            meeting_date: 파일명 prefix 용.
            existing_store: 기존 페이지 read 전용.

        Returns:
            [(rel_path, new_content), ...]. 빈 리스트 가능. 입력 순서가 아니라
            confidence 내림차순으로 정렬된 결과 (최대 8건).
        """
        if not decisions:
            return []

        date_prefix = meeting_date.isoformat()

        # ── 0. confidence 내림차순 정렬 + 상위 N개 slice ────────────────
        # PRD R3: 회의당 페이지 폭증 방지. tie-break 는 입력 순서 보존을 위해 stable sort.
        sorted_decisions = sorted(decisions, key=lambda d: d.confidence, reverse=True)
        capped = sorted_decisions[:_MAX_PAGES_PER_MEETING]
        if len(decisions) > _MAX_PAGES_PER_MEETING:
            logger.info(
                "DecisionExtractor.render_pages: %d건 중 상위 %d건만 처리 (R3 상한)",
                len(decisions),
                _MAX_PAGES_PER_MEETING,
            )

        # ── 1. 추출된 구조체를 고정 스키마로 렌더한다 ──────────────────
        def _render_one(
            decision: ExtractedDecision,
        ) -> tuple[str, str] | None:
            """한 decision 을 (rel_path, content) 로 변환. 실패 시 None."""
            rel_path = self._resolve_decision_path(
                existing_store=existing_store,
                date_prefix=date_prefix,
                slug=decision.slug,
            )

            # 기존 페이지 확인
            existing_content: str | None = None
            existing_created_at: str | None = None
            existing_frontmatter: dict[str, Any] = {}
            try:
                existing_page = self._read_existing_page(existing_store, rel_path)
                existing_content = self._content_from_existing_page(existing_page)
                existing_frontmatter = dict(getattr(existing_page, "frontmatter", {}) or {})
            except Exception as exc:  # noqa: BLE001 — read 실패는 신규로 처리
                logger.debug("기존 페이지 없음 (신규 작성): %s (%r)", rel_path, exc)
                existing_content = None

            if existing_frontmatter:
                existing_created_at = str(existing_frontmatter.get("created_at") or "").strip()
                if not existing_created_at:
                    existing_created_at = None
            elif existing_content:
                existing_created_at = self._extract_created_at(existing_content)

            created_at = existing_created_at
            if created_at is None and existing_content:
                created_at = self._extract_created_at(existing_content)

            existing_sources = self._frontmatter_list(existing_frontmatter.get("source_meetings"))
            if not existing_sources and existing_frontmatter.get("meeting_id"):
                existing_sources = [str(existing_frontmatter.get("meeting_id"))]
            source_meetings = list(existing_sources)
            if meeting_id not in source_meetings:
                source_meetings.append(meeting_id)
            record_id = str(existing_frontmatter.get("id") or "").strip() or None

            record = DecisionRecord.from_extracted(
                decision=decision,
                meeting_id=meeting_id,
                meeting_date=meeting_date,
                created_at=created_at,
                record_id=record_id,
                source_meetings=source_meetings,
            )
            if not record.has_verified_candidate_citation and not record.citation_unresolved:
                logger.warning("DecisionExtractor.render_pages 빈 인용 후보를 건너뜁니다.")
                return None

            # 미해결 원문은 검토를 위해 보존하고, 정상 후보에만 기존 병기 정리를 적용한다.
            content = record.to_markdown()
            if not record.citation_unresolved:
                content = _strip_paren_latin(content)

            # 기존 created_at 보존
            if existing_created_at and existing_created_at not in content:
                content = self._inject_created_at(content, existing_created_at)

            return (rel_path, content)

        # 모델 응답을 사용하지 않던 페이지별 추론을 제거한다. 최종 결과는 이전과
        # 동일한 canonical markdown이며, 출력 경로·정렬·인용·created_at을 보존한다.
        rendered = [_render_one(decision) for decision in capped]

        # ── 3. None 제거 (실패한 페이지) ────────────────────────────────
        results: list[tuple[str, str]] = [r for r in rendered if r is not None]
        return results

    # ─────────────────────────────────────────────────────────────────────
    # 내부 헬퍼
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _seconds_to_hhmmss(seconds: float) -> str:
        """float 초를 HH:MM:SS 문자열로 변환."""
        total = int(seconds)
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    @staticmethod
    def _resolve_decision_path(*, existing_store: Any, date_prefix: str, slug: str) -> str:
        """같은 slug 의 기존 decision 페이지가 있으면 그 경로를 재사용한다.

        기본 dedupe 규칙: LLM 이 같은 결정을 같은 slug 로 반복 추출하면 날짜별
        신규 파일을 계속 만들지 않고 기존 canonical decision record 를 갱신한다.
        """
        default_path = f"decisions/{date_prefix}-{slug}.md"
        all_pages = getattr(existing_store, "all_pages", None)
        if not callable(all_pages):
            return default_path

        candidates: list[str] = []
        try:
            for rel in all_pages():
                path = Path(str(rel))
                if len(path.parts) < 2 or path.parts[0] != "decisions":
                    continue
                if path.name.endswith(f"-{slug}.md"):
                    candidates.append(str(path))
        except Exception as exc:  # noqa: BLE001
            logger.debug("decision dedupe scan 실패: slug=%s (%r)", slug, exc)
            return default_path

        if not candidates:
            return default_path
        return sorted(candidates)[0]

    @staticmethod
    def _read_existing_page(store: Any, rel_path: str) -> Any:
        """existing_store.read_page 를 Path 우선으로 호출한다."""
        try:
            return store.read_page(Path(rel_path))
        except TypeError:
            return store.read_page(rel_path)
        except Exception:
            return store.read_page(rel_path)

    @staticmethod
    def _content_from_existing_page(result: Any) -> str | None:
        """read_page 결과에서 본문 문자열을 추출한다.

        store 가 KeyError 또는 다른 예외를 raise 하면 None 반환.
        """
        # MockWikiStore 는 str 반환, 실제 WikiStore 는 WikiPage 반환
        if isinstance(result, str):
            return result
        if hasattr(result, "content"):
            # WikiPage 인 경우 frontmatter + body 재조립이 필요하지만, render 단계에서
            # LLM 에 컨텍스트로 전달하기 위해 간단히 content 만 반환해도 충분.
            # frontmatter 가 필요한 경우 호출자가 추가 처리.
            return getattr(result, "content", None)
        return None

    @staticmethod
    def _frontmatter_list(value: Any) -> list[str]:
        """frontmatter scalar/list 값을 list[str] 로 정규화한다."""
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            inner = text[1:-1].strip()
            if not inner:
                return []
            return [item.strip() for item in inner.split(",") if item.strip()]
        return [text]

    @staticmethod
    def _extract_created_at(content: str) -> str | None:
        """frontmatter 에서 created_at 값을 추출."""
        match = re.search(r"^created_at:\s*(.+)$", content, re.MULTILINE)
        if match:
            return match.group(1).strip()
        return None

    @staticmethod
    def _inject_created_at(content: str, created_at: str) -> str:
        """frontmatter 에 created_at 을 강제 주입.

        이미 있으면 교체, 없으면 frontmatter 닫기 직전에 추가.
        """
        # 기존 created_at 줄 교체
        new_content, count = re.subn(
            r"^created_at:\s*.+$",
            f"created_at: {created_at}",
            content,
            count=1,
            flags=re.MULTILINE,
        )
        if count > 0:
            return new_content
        # frontmatter 닫기(`---`) 직전에 추가
        lines = content.splitlines(keepends=True)
        # 두 번째 --- 위치 찾기
        in_fm = False
        for idx, line in enumerate(lines):
            if line.strip() == "---":
                if not in_fm:
                    in_fm = True
                    continue
                # 닫는 ---
                lines.insert(idx, f"created_at: {created_at}\n")
                return "".join(lines)
        return content
