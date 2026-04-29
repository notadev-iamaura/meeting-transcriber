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
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

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
    citation: Citation


@dataclass
class ExtractedDecision:
    """LLM 추출 결과 — 1차 가공 단계.

    Attributes:
        title: 페이지 제목.
        slug: 파일명에 들어갈 URL-safe 식별자.
        decision_text: "## 결정 내용" 섹션 본문 (인용 포함).
        background: "## 배경" 섹션 본문 (인용 포함).
        follow_ups: 후속 액션 메모.
        participants: 참여 화자 이름 목록.
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
    r"\[meeting:([a-f0-9]{8})@(\d{2}):(\d{2}):(\d{2})\]"
)

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
        meeting_id: 8자리 hex.
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


def _build_extracted_decision(item: dict[str, Any], meeting_id: str) -> ExtractedDecision:
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
        if owner and desc and cit is not None:
            follow_ups.append(ActionItemRef(owner=owner, description=desc, citation=cit))

    # citations: decision_text + background 에서 평탄화 추출
    flat_citations = _extract_citations_from_text(decision_text) + _extract_citations_from_text(
        background
    )

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
- decision_text: 결정 본문 (인용 마커 [meeting:id@HH:MM:SS] 필수)
- background: 배경 설명 (인용 마커 필수)
- follow_ups: [{owner, description, citation_ts}, ...] (없으면 빈 배열)
- participants: 화자 이름 배열
- projects: 프로젝트 slug 배열
- confidence: 0~10 정수

규칙:
1. 결정사항이 없으면 빈 배열 [] 만 출력.
2. 한국어 고유명사에 영어/중국어 병기 절대 금지.
3. 모든 사실 진술에 인용 마커 부착.
"""

_RENDER_SYSTEM_PROMPT = """\
당신은 회의 결정사항을 마크다운 위키 페이지로 변환하는 작성자입니다.

페이지 형식:
---
type: decision
date: YYYY-MM-DD
meeting_id: <8 hex>
status: confirmed | superseded
participants: [이름, ...]
projects: [slug, ...]
confidence: 0~10
created_at: ISO8601
updated_at: ISO8601
---

# {title}

## 결정 내용
...

## 배경
...

## 후속 액션
- [ ] 담당자: 작업 [meeting:id@HH:MM:SS]

## 참고 회의
- [meeting_id](../../../app/viewer/meeting_id)

<!-- confidence: N -->

규칙:
1. 모든 사실 진술에 인용 마커 부착.
2. 한국어 고유명사 외국어 병기 금지.
3. 기존 페이지가 있으면 frontmatter 의 created_at 보존.
4. 마지막 줄에 confidence 마커 필수.
"""


# ─────────────────────────────────────────────────────────────────────────
# 3.4 추출기
# ─────────────────────────────────────────────────────────────────────────


class DecisionExtractor:
    """회의 컨텍스트에서 결정사항을 LLM 으로 추출하고 페이지 후보를 렌더링한다."""

    def __init__(self, llm: WikiLLMClient) -> None:
        """LLM 추상화 1개만 받는다.

        Args:
            llm: WikiLLMClient (실구현 또는 mock).
        """
        self._llm: WikiLLMClient = llm

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
            2. utterances 를 "[HH:MM:SS] 화자: text" 라인으로 직렬화 (sanitize 적용).
            3. system_prompt + user_prompt 조립.
            4. llm.generate() 호출. JSON 파싱 — 실패 시 1회 재시도.
            5. 항목별로 ExtractedDecision 변환.

        Args:
            meeting_id: 회의 ID.
            meeting_date: 회의 날짜.
            summary: 8단계 결과 마크다운.
            utterances: 5단계 corrector 출력 (CorrectedUtterance 호환).

        Returns:
            ExtractedDecision 리스트. 결정사항이 없거나 파싱 실패 시 빈 리스트.
        """
        # 빈 utterances → LLM 호출 없이 즉시 빈 리스트
        if not utterances:
            return []

        # utterances 직렬화
        lines: list[str] = []
        for utt in utterances:
            text = sanitize_utterance_text(getattr(utt, "text", ""))
            speaker = getattr(utt, "speaker", "UNKNOWN")
            start = getattr(utt, "start", 0.0)
            ts_str = self._seconds_to_hhmmss(float(start))
            lines.append(f"[{ts_str}] {speaker}: {text}")

        user_prompt = (
            f"회의 ID: {meeting_id}\n"
            f"회의 날짜: {meeting_date.isoformat()}\n\n"
            f"## 8단계 요약\n{summary}\n\n"
            f"## 발화 목록\n" + "\n".join(lines) + "\n\n"
            f"위 컨텍스트에서 결정사항을 JSON 배열로 추출하세요."
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
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                results.append(_build_extracted_decision(item, meeting_id))
            except Exception as exc:  # noqa: BLE001 — 항목별 실패는 skip
                logger.warning("DecisionExtractor: 항목 변환 실패 — skip: %r", exc)
                continue

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

        Phase 2.E 변경:
            - confidence 내림차순 정렬 후 상위 _MAX_PAGES_PER_MEETING(=8) 건만 처리.
            - 페이지 단위 LLM 호출을 asyncio.gather 로 병렬화 — 회의당 시간 단축.
            - 페이지별 실패는 skip 하되 다른 페이지 생성에는 영향 없음.

        흐름 (각 decision 별):
            1. rel_path 결정 — `decisions/{YYYY-MM-DD}-{slug}.md`.
            2. existing_store.read_page(rel_path) 시도 — 기존 페이지 컨텍스트 확보.
            3. LLM 1회 호출로 갱신 본문 생성.
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
        sorted_decisions = sorted(
            decisions, key=lambda d: d.confidence, reverse=True
        )
        capped = sorted_decisions[:_MAX_PAGES_PER_MEETING]
        if len(decisions) > _MAX_PAGES_PER_MEETING:
            logger.info(
                "DecisionExtractor.render_pages: %d건 중 상위 %d건만 처리 (R3 상한)",
                len(decisions),
                _MAX_PAGES_PER_MEETING,
            )

        # ── 1. 단일 decision 처리 코루틴 정의 ──────────────────────────
        async def _render_one(
            decision: ExtractedDecision,
        ) -> tuple[str, str] | None:
            """한 decision 을 (rel_path, content) 로 변환. 실패 시 None."""
            rel_path = f"decisions/{date_prefix}-{decision.slug}.md"

            # 기존 페이지 확인
            existing_content: str | None = None
            existing_created_at: str | None = None
            try:
                existing_content = self._read_existing(existing_store, rel_path)
            except Exception as exc:  # noqa: BLE001 — read 실패는 신규로 처리
                logger.debug("기존 페이지 없음 (신규 작성): %s (%r)", rel_path, exc)
                existing_content = None

            if existing_content:
                existing_created_at = self._extract_created_at(existing_content)

            # LLM 호출
            user_prompt = self._build_render_prompt(
                decision=decision,
                meeting_id=meeting_id,
                meeting_date=meeting_date,
                existing_content=existing_content,
                rel_path=rel_path,
            )

            try:
                raw = await self._llm.generate(
                    system_prompt=_RENDER_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                )
            except Exception as exc:  # noqa: BLE001 — 페이지별 실패는 skip
                logger.warning(
                    "DecisionExtractor.render_pages 페이지 생성 실패 — skip: path=%s, err=%r",
                    rel_path,
                    exc,
                )
                return None

            # 후처리: 한국어 고유명사 영문 병기 제거
            content = _strip_paren_latin(raw)

            # 기존 created_at 보존 (LLM 이 누락했을 경우 강제 주입)
            if existing_created_at and existing_created_at not in content:
                content = self._inject_created_at(content, existing_created_at)

            return (rel_path, content)

        # ── 2. asyncio.gather 로 병렬 실행 ─────────────────────────────
        # 예외는 _render_one 내부에서 catch → None 반환되므로 gather 가 raise 하지 않음.
        rendered = await asyncio.gather(
            *[_render_one(d) for d in capped]
        )

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
    def _read_existing(store: Any, rel_path: str) -> str | None:
        """existing_store.read_page 를 호출하여 기존 페이지 본문을 반환.

        store 가 KeyError 또는 다른 예외를 raise 하면 None 반환.
        """
        result = store.read_page(rel_path)
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

    @staticmethod
    def _build_render_prompt(
        *,
        decision: ExtractedDecision,
        meeting_id: str,
        meeting_date: date,
        existing_content: str | None,
        rel_path: str,
    ) -> str:
        """render_pages() 의 user_prompt 를 조립한다."""
        parts: list[str] = []
        parts.append(f"파일 경로: {rel_path}")
        parts.append(f"회의 ID: {meeting_id}")
        parts.append(f"회의 날짜: {meeting_date.isoformat()}")
        parts.append("")
        parts.append("## 추출된 결정사항")
        parts.append(f"제목: {decision.title}")
        parts.append(f"결정 내용: {decision.decision_text}")
        parts.append(f"배경: {decision.background}")
        parts.append(f"참여자: {decision.participants}")
        parts.append(f"프로젝트: {decision.projects}")
        parts.append(f"신뢰도: {decision.confidence}")
        parts.append("")
        if existing_content:
            parts.append("## 기존 페이지 (frontmatter 의 created_at 은 보존하세요)")
            parts.append(existing_content)
            parts.append("")
        parts.append("위 정보를 바탕으로 마크다운 페이지를 작성하세요.")
        return "\n".join(parts)
