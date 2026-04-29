"""TopicExtractor — 반복 등장 개념을 topics/{concept}.md 페이지로 자동 합성.

목적: PRD §5.3 의 "topics 는 3회 이상 반복 등장한 개념만" 정책을 구현한다.
단일 회의에서만 언급된 개념은 **페이지 생성 안 함**. 회의를 거치며 mention
count 를 누적시키다가, 3회에 도달하면 비로소 topics/{slug}.md 를 LLM 으로
생성한다. 이미 페이지가 있으면 새 인용만 추가한다.

차별점 (decision/person/project 와 다른 점):
    1. 임계 누적 — 단일 회의는 페이지 생성 X. 메타파일에 카운트만 적립.
    2. 영속 메타파일 — wiki/.topic_mentions.json (git ignore — 디스크만).
    3. 합성 시점 분리 — extract_concepts (회의 ingest 시) + aggregate_and_render
       (3회 도달 시) 를 명시적 단계로 분리.

의존성:
    - core.wiki.llm_client.WikiLLMClient
    - core.wiki.models.Citation
    - core.wiki.store.WikiStore
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from core.wiki.llm_client import WikiLLMClient, sanitize_utterance_text
from core.wiki.models import Citation
from core.wiki.store import WikiStore

logger = logging.getLogger(__name__)


# 메타파일 경로 (wiki 루트 기준 상대).
_META_FILENAME: str = ".topic_mentions.json"

# 메타파일 스키마 버전 — 이후 변경 시 마이그레이션 트리거.
_META_VERSION: int = 1

# aggregate_and_render 에서 단일 회의당 처리할 최대 개념 수.
# 한 회의에서 LLM 이 수십 개의 개념을 추출해도 렌더 LLM 호출이 폭주하지 않도록 상한 설정.
# TODO(Phase 5): 상한 초과분은 confidence 내림차순 우선 처리로 개선.
_MAX_RENDER_PER_MEETING: int = 8

# slug 에 허용되는 문자 — 한글/영숫자/하이픈/언더스코어.
_TOPIC_SLUG_ALLOWED: re.Pattern[str] = re.compile(
    r"^[\uAC00-\uD7A3A-Za-z0-9\-_]+$"
)

# 페이지 인용 패턴 — citations.CITATION_PATTERN 와 동일.
_CITATION_PATTERN: re.Pattern[str] = re.compile(
    r"\[meeting:([a-f0-9]{8})@(\d{2}):(\d{2}):(\d{2})\]"
)


# ─────────────────────────────────────────────────────────────────────────
# 1. Utterance Protocol
# ─────────────────────────────────────────────────────────────────────────


class Utterance(Protocol):
    """corrector 단계의 발화 표현. duck-typing 계약."""

    speaker: str
    text: str
    start: float
    end: float


# ─────────────────────────────────────────────────────────────────────────
# 2. 데이터 모델
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExtractedConcept:
    """이번 회의에서 LLM 이 추출한 개념 1건.

    Attributes:
        name: 사람이 부르는 이름.
        slug: filename-safe.
        description: 한 줄 설명 (한국어).
        citations: 본 개념이 등장한 발화 인용 목록.
        confidence: LLM self-rated 0~10.
    """

    name: str
    slug: str
    description: str
    citations: list[Citation] = field(default_factory=list)
    confidence: int = 0


@dataclass
class ConceptMention:
    """디스크에 영속화되는 mention count 트래킹 항목.

    Attributes:
        slug: ExtractedConcept.slug 와 동일.
        name: 가장 최근 회의에서 본 name.
        meeting_ids: 이 개념이 등장한 회의 ID 리스트 (set 의미, 중복 제거).
        last_seen: 가장 최근 mention 의 ISO 날짜 문자열.
        page_created: 이미 토픽 페이지가 생성되었는지.
        last_citations: 가장 최근 회의의 citations.
        first_seen: 가장 처음 등장한 ISO 날짜 문자열.
    """

    slug: str
    name: str
    meeting_ids: list[str] = field(default_factory=list)
    last_seen: str = ""
    page_created: bool = False
    last_citations: list[Citation] = field(default_factory=list)
    first_seen: str = ""


# ─────────────────────────────────────────────────────────────────────────
# 3. 헬퍼
# ─────────────────────────────────────────────────────────────────────────


def _normalize_concept_slug(name: str) -> str:
    """개념 이름을 filename-safe slug 로 정규화한다.

    정책:
        - 영문 → lowercase + 공백을 hyphen 으로 치환.
        - 한글은 그대로 보존.
        - "/" 와 ".." 같은 path traversal 문자는 거부 (빈 문자열 반환).
        - NFC 정규화로 한글 자모 결합 표준화.

    Args:
        name: 원본 이름 (예: "pricing strategy", "온보딩 프로세스").

    Returns:
        slug 문자열. 정규화 실패 시 빈 문자열.
    """
    if not name:
        return ""
    # NFC 정규화 — 한글 자모 결합 표준화
    normalized = unicodedata.normalize("NFC", name).strip()
    if not normalized:
        return ""

    # path traversal 차단
    if "/" in normalized or ".." in normalized or "\\" in normalized:
        return ""

    # 영문 부분만 lowercase, 한글은 보존
    out_chars: list[str] = []
    for ch in normalized:
        if ch.isspace():
            out_chars.append("-")
        elif ch.isascii() and ch.isalpha():
            out_chars.append(ch.lower())
        elif ch.isascii() and ch.isdigit():
            out_chars.append(ch)
        elif ch in {"-", "_"}:
            out_chars.append(ch)
        elif "\uAC00" <= ch <= "\uD7A3":
            # 한글 음절
            out_chars.append(ch)
        else:
            # 그 외 허용 안 되는 문자는 hyphen 으로
            out_chars.append("-")

    slug = "".join(out_chars)
    # 연속 hyphen 정규화
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def _extract_json_array(text: str) -> list[Any] | None:
    """LLM 응답에서 JSON 배열을 robust 하게 파싱한다.

    Args:
        text: LLM 원시 응답.

    Returns:
        파싱된 리스트 또는 None.
    """
    if not text:
        return None
    text = text.strip()
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
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


def _serialize_citations(citations: list[Citation]) -> list[dict[str, Any]]:
    """Citation 리스트를 JSON 직렬화 가능한 dict 리스트로 변환한다."""
    return [
        {
            "meeting_id": c.meeting_id,
            "timestamp_str": c.timestamp_str,
            "timestamp_seconds": c.timestamp_seconds,
        }
        for c in citations
    ]


def _deserialize_citations(data: list[Any]) -> list[Citation]:
    """JSON dict 리스트를 Citation 리스트로 역직렬화한다."""
    if not isinstance(data, list):
        return []
    out: list[Citation] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            out.append(
                Citation(
                    meeting_id=str(item.get("meeting_id", "")),
                    timestamp_str=str(item.get("timestamp_str", "")),
                    timestamp_seconds=int(item.get("timestamp_seconds", 0)),
                )
            )
        except (TypeError, ValueError):
            continue
    return out


# ─────────────────────────────────────────────────────────────────────────
# 4. 시스템 프롬프트
# ─────────────────────────────────────────────────────────────────────────


_EXTRACT_CONCEPTS_SYSTEM_PROMPT = """\
당신은 회의록에서 반복 등장하는 핵심 개념(concepts) 을 추출하는 분석가입니다.

각 회의에서 다루는 핵심 주제·개념·이슈를 JSON 배열로 출력합니다:
[
  {
    "name": "개념 이름",
    "slug": "filename-safe slug (영소문자+하이픈, 한글 보존)",
    "description": "한 줄 설명 (한국어)",
    "citations": [{"meeting_id": "...", "timestamp_str": "HH:MM:SS", "timestamp_seconds": 정수}, ...],
    "confidence": 0~10 정수
  }
]

규칙:
1. 잡담·일상 인사 등은 추출 X.
2. 명확하고 반복 등장하는 개념만.
3. confidence 7 미만의 모호한 개념은 추출 X.
4. 한국어 고유명사는 외국어 병기 금지.
5. 식별 불가하면 빈 배열 [].
"""


_RENDER_TOPIC_SYSTEM_PROMPT = """\
당신은 회의에서 반복 등장한 개념에 대한 위키 페이지를 작성합니다.

PRD §4.2 topics 템플릿:
---
type: topic
name: <개념 이름>
first_seen: <YYYY-MM-DD>
last_seen: <YYYY-MM-DD>
mention_count: <int>
confidence: <0-10>
---

# <개념 이름>

## 정의
(개념 정의, 인용 마커 [meeting:{8hex}@HH:MM:SS] 필수)

## 관련 회의
- [{meeting_id} — YYYY-MM-DD 회의](/app/viewer/{meeting_id})

## 자주 함께 등장하는 개념
- (있다면 나열)

<!-- confidence: N -->

규칙:
1. 모든 사실 진술에 인용 마커 [meeting:id@HH:MM:SS] 1개 이상 필수.
2. confidence 마커 <!-- confidence: N --> 누락 금지.
3. 외국어 병기 금지.
"""


_UPDATE_TOPIC_SYSTEM_PROMPT = """\
당신은 기존 토픽 페이지에 새 회의의 인용을 추가합니다.

기존 페이지에 새 회의 발화를 통합하되:
- 기존 frontmatter / 섹션 구조 유지
- last_seen, mention_count 갱신
- 관련 회의 섹션에 새 회의 추가
- 새 인용 마커 [meeting:id@HH:MM:SS] 자연스럽게 본문에 반영
- <!-- confidence: N --> 마커 보존

규칙:
1. 기존 사실을 임의로 수정하지 마세요.
2. 외국어 병기 금지.
"""


# ─────────────────────────────────────────────────────────────────────────
# 5. TopicExtractor
# ─────────────────────────────────────────────────────────────────────────


class TopicExtractor:
    """반복 등장 개념을 topics/{slug}.md 로 자동 합성.

    Threading: 단일 코루틴 가정. 페이지별 LLM 호출은 직렬.

    Attributes:
        _llm: WikiLLMClient.
        _min_meetings_threshold: 페이지 생성 최소 회의 수.
        _min_confidence_to_count: mention count 누적 최소 confidence.
    """

    def __init__(
        self,
        llm: WikiLLMClient,
        *,
        min_meetings_threshold: int = 3,
        min_confidence_to_count: int = 7,
    ) -> None:
        """LLM + 임계값을 받는다.

        Args:
            llm: WikiLLMClient (실구현 또는 mock).
            min_meetings_threshold: topic 페이지 생성 임계 회의 수.
            min_confidence_to_count: mention count 적립 최소 confidence.
        """
        self._llm: WikiLLMClient = llm
        self._min_meetings_threshold: int = min_meetings_threshold
        self._min_confidence_to_count: int = min_confidence_to_count

    async def extract_concepts(
        self,
        *,
        meeting_id: str,
        meeting_date: date,
        utterances: list[Utterance],
        summary: str,
    ) -> list[ExtractedConcept]:
        """이번 회의에서 LLM 으로 개념 추출 (LLM 1회).

        Args:
            meeting_id: 회의 ID.
            meeting_date: 회의 날짜.
            utterances: 5단계 corrector 결과.
            summary: 요약.

        Returns:
            ExtractedConcept 리스트. utterances 비면 LLM 호출 0회.
        """
        # 빈 utterances → LLM 호출 없이 즉시 빈 리스트
        if not utterances:
            return []

        # utterances 직렬화 (sanitize 적용)
        lines: list[str] = []
        for utt in utterances:
            text = sanitize_utterance_text(getattr(utt, "text", ""))
            speaker = getattr(utt, "speaker", "UNKNOWN")
            start = float(getattr(utt, "start", 0.0))
            ts_str = self._seconds_to_hhmmss(start)
            lines.append(f"[{ts_str}] {speaker}: {text}")

        user_prompt = (
            f"회의 ID: {meeting_id}\n"
            f"회의 날짜: {meeting_date.isoformat()}\n\n"
            f"## 요약\n{summary}\n\n"
            f"## 발화 목록\n" + "\n".join(lines) + "\n\n"
            "위 컨텍스트에서 반복 등장 가능성 높은 개념을 JSON 배열로 추출하세요."
        )

        try:
            raw = await self._llm.generate(
                system_prompt=_EXTRACT_CONCEPTS_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
        except Exception as exc:  # noqa: BLE001 — graceful 폴백
            logger.warning("TopicExtractor.extract_concepts 1차 호출 실패: %r", exc)
            return []

        parsed = _extract_json_array(raw)
        if parsed is None:
            # 1회 재시도
            logger.warning("TopicExtractor: 1차 JSON 파싱 실패, 1회 재시도")
            try:
                raw = await self._llm.generate(
                    system_prompt=_EXTRACT_CONCEPTS_SYSTEM_PROMPT,
                    user_prompt=user_prompt + "\n\n반드시 JSON 배열만 출력하세요.",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("TopicExtractor 재시도 실패: %r", exc)
                return []
            parsed = _extract_json_array(raw)

        if parsed is None:
            return []

        results: list[ExtractedConcept] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                concept = self._build_concept(item)
            except Exception as exc:  # noqa: BLE001 — 항목별 실패는 skip
                logger.warning("TopicExtractor: 항목 변환 실패 — skip: %r", exc)
                continue
            if concept is None:
                continue
            # confidence 게이트
            if concept.confidence < self._min_confidence_to_count:
                logger.debug(
                    "TopicExtractor: confidence %d < %d — skip: %s",
                    concept.confidence,
                    self._min_confidence_to_count,
                    concept.name,
                )
                continue
            results.append(concept)
        return results

    async def aggregate_and_render(
        self,
        *,
        new_concepts: list[ExtractedConcept],
        meeting_id: str,
        meeting_date: date,
        existing_store: WikiStore,
    ) -> list[tuple[str, str, int]]:
        """기존 mention count + 새 등장 누적. 임계 도달 시 페이지 렌더.

        Args:
            new_concepts: extract_concepts 결과.
            meeting_id: 회의 ID.
            meeting_date: 회의 날짜.
            existing_store: WikiStore.

        Returns:
            [(rel_path, content, confidence), ...].
        """
        # ── 1. 메타파일 로드 ─────────────────────────────────────────
        meta = self._load_meta(existing_store)

        # ── 2. mention 갱신 (멱등성 보장) ──────────────────────────
        for concept in new_concepts:
            slug = concept.slug or _normalize_concept_slug(concept.name)
            if not slug:
                logger.warning(
                    "TopicExtractor: slug 정규화 실패 — skip: name=%s",
                    concept.name,
                )
                continue

            mention = meta.get(slug)
            if mention is None:
                mention = ConceptMention(
                    slug=slug,
                    name=concept.name,
                    meeting_ids=[],
                    last_seen=meeting_date.isoformat(),
                    page_created=False,
                    last_citations=[],
                    first_seen=meeting_date.isoformat(),
                )
                meta[slug] = mention

            # 멱등성: 같은 meeting_id 면 추가 안 함
            if meeting_id in mention.meeting_ids:
                logger.debug(
                    "TopicExtractor: meeting_id %s 이미 mention 에 존재 — skip",
                    meeting_id,
                )
                continue

            mention.meeting_ids.append(meeting_id)
            mention.name = concept.name  # 가장 최근 이름 갱신
            mention.last_seen = meeting_date.isoformat()
            mention.last_citations = list(concept.citations)
            if not mention.first_seen:
                mention.first_seen = meeting_date.isoformat()

        # ── 3. 메타파일 저장 ─────────────────────────────────────────
        self._save_meta(existing_store, meta)

        # ── 4. 페이지 렌더 후보 결정 ─────────────────────────────────
        rendered_pages: list[tuple[str, str, int]] = []

        # LLM 호출 폭주 방지 — 단일 회의당 최대 _MAX_RENDER_PER_MEETING 건만 처리.
        # 초과분은 다음 회의 ingest 시 재시도된다 (mention.page_created=False 유지).
        render_count = 0

        for concept in new_concepts:
            slug = concept.slug or _normalize_concept_slug(concept.name)
            if not slug:
                continue
            mention = meta.get(slug)
            if mention is None:
                continue

            count = len(mention.meeting_ids)
            if count < self._min_meetings_threshold:
                # 임계 미달 — 페이지 변경 X
                continue

            # 단일 회의 LLM 호출 상한 초과 시 중단
            if render_count >= _MAX_RENDER_PER_MEETING:
                logger.warning(
                    "TopicExtractor: 단일 회의 렌더 상한(%d) 도달 — 나머지 개념은 다음 ingest 에서 처리",
                    _MAX_RENDER_PER_MEETING,
                )
                break

            try:
                if not mention.page_created:
                    # 신규 페이지 생성
                    result = await self._render_new_topic_page(
                        mention=mention,
                        concept=concept,
                        meeting_id=meeting_id,
                        meeting_date=meeting_date,
                    )
                    if result is not None:
                        rendered_pages.append(result)
                        render_count += 1
                        # 메타 갱신: page_created=True
                        mention.page_created = True
                else:
                    # 기존 페이지 갱신
                    result = await self._update_existing_topic_page(
                        mention=mention,
                        concept=concept,
                        meeting_id=meeting_id,
                        meeting_date=meeting_date,
                        store=existing_store,
                    )
                    if result is not None:
                        rendered_pages.append(result)
                        render_count += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "TopicExtractor: 페이지 렌더 실패 — skip: slug=%s, %r",
                    slug,
                    exc,
                )
                continue

        # ── 5. page_created 변경 반영을 위해 메타파일 재저장 ─────
        self._save_meta(existing_store, meta)

        return rendered_pages

    # ── 내부 헬퍼 ──────────────────────────────────────────────────

    def _build_concept(
        self, item: dict[str, Any]
    ) -> ExtractedConcept | None:
        """LLM JSON 항목을 ExtractedConcept 으로 변환한다.

        Args:
            item: LLM JSON 응답 1개 (dict).

        Returns:
            ExtractedConcept 또는 None (변환 실패 시).
        """
        name = str(item.get("name", "") or "").strip()
        if not name:
            return None
        # slug — LLM 이 제공한 값 우선, 없으면 name 정규화
        slug_raw = str(item.get("slug", "") or "").strip()
        slug = slug_raw if slug_raw else _normalize_concept_slug(name)
        # 추가 검증: slug 가 traversal/슬래시 포함이면 재정규화
        if not slug or "/" in slug or ".." in slug:
            slug = _normalize_concept_slug(name)
            if not slug:
                return None

        description = str(item.get("description", "") or "").strip()
        try:
            confidence = int(item.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0

        # citations 파싱
        citations: list[Citation] = []
        cit_raw = item.get("citations", [])
        if isinstance(cit_raw, list):
            for c in cit_raw:
                if not isinstance(c, dict):
                    continue
                try:
                    citations.append(
                        Citation(
                            meeting_id=str(c.get("meeting_id", "")),
                            timestamp_str=str(c.get("timestamp_str", "")),
                            timestamp_seconds=int(c.get("timestamp_seconds", 0)),
                        )
                    )
                except (TypeError, ValueError):
                    continue

        return ExtractedConcept(
            name=name,
            slug=slug,
            description=description,
            citations=citations,
            confidence=confidence,
        )

    def _load_meta(self, store: WikiStore) -> dict[str, ConceptMention]:
        """`.topic_mentions.json` 을 읽어 슬러그 → ConceptMention dict 로 반환.

        Args:
            store: WikiStore.

        Returns:
            메타 dict. 파일 없거나 깨지면 빈 dict.
        """
        meta_path = store.root / _META_FILENAME
        if not meta_path.exists():
            return {}
        try:
            raw = meta_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "TopicExtractor: 메타파일 로드 실패 — 빈 dict 폴백: %r", exc
            )
            return {}

        if not isinstance(data, dict):
            return {}
        mentions_raw = data.get("mentions", {})
        if not isinstance(mentions_raw, dict):
            return {}

        result: dict[str, ConceptMention] = {}
        for slug, item in mentions_raw.items():
            if not isinstance(item, dict):
                continue
            try:
                mention = ConceptMention(
                    slug=str(item.get("slug", slug)),
                    name=str(item.get("name", "")),
                    meeting_ids=list(item.get("meeting_ids", []) or []),
                    last_seen=str(item.get("last_seen", "")),
                    page_created=bool(item.get("page_created", False)),
                    last_citations=_deserialize_citations(
                        item.get("last_citations", [])
                    ),
                    first_seen=str(item.get("first_seen", "")),
                )
                # meeting_ids 항목들이 모두 string 인지 확인
                mention.meeting_ids = [
                    str(mid) for mid in mention.meeting_ids if mid
                ]
                result[slug] = mention
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "TopicExtractor: 메타 항목 변환 실패 — skip: slug=%s, %r",
                    slug,
                    exc,
                )
                continue
        return result

    def _save_meta(
        self, store: WikiStore, meta: dict[str, ConceptMention]
    ) -> None:
        """슬러그 → ConceptMention dict 를 `.topic_mentions.json` 에 저장.

        Args:
            store: WikiStore.
            meta: 메타 dict.
        """
        payload: dict[str, Any] = {
            "version": _META_VERSION,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "mentions": {
                slug: {
                    "slug": mention.slug,
                    "name": mention.name,
                    "meeting_ids": list(mention.meeting_ids),
                    "last_seen": mention.last_seen,
                    "page_created": mention.page_created,
                    "last_citations": _serialize_citations(
                        mention.last_citations
                    ),
                    "first_seen": mention.first_seen,
                }
                for slug, mention in meta.items()
            },
        }

        meta_path = store.root / _META_FILENAME
        try:
            # 부모 디렉토리 보장
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = meta_path.with_suffix(meta_path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(meta_path)
        except OSError as exc:
            logger.warning("TopicExtractor: 메타파일 저장 실패: %r", exc)

    async def _render_new_topic_page(
        self,
        mention: ConceptMention,
        concept: ExtractedConcept,
        meeting_id: str,
        meeting_date: date,
    ) -> tuple[str, str, int] | None:
        """3회 임계 도달 — 신규 topics/{slug}.md 페이지를 LLM 으로 생성한다.

        Args:
            mention: 누적 mention 정보.
            concept: 이번 회의 추출 결과.
            meeting_id: 회의 ID.
            meeting_date: 회의 날짜.

        Returns:
            (rel_path, content, confidence) 또는 None.
        """
        # 인용 직렬화
        cit_lines: list[str] = []
        for c in mention.last_citations or concept.citations:
            cit_lines.append(
                f"- [{c.meeting_id}@{c.timestamp_str}] (timestamp_seconds={c.timestamp_seconds})"
            )

        user_prompt = (
            f"슬러그: {mention.slug}\n"
            f"이름: {mention.name}\n"
            f"등장 회의 수: {len(mention.meeting_ids)}\n"
            f"first_seen: {mention.first_seen or meeting_date.isoformat()}\n"
            f"last_seen: {meeting_date.isoformat()}\n"
            f"이번 회의 ID: {meeting_id}\n"
            f"설명: {concept.description}\n"
            f"인용:\n" + "\n".join(cit_lines) + "\n\n"
            "위 정보로 PRD §4.2 topics 템플릿에 맞게 페이지 본문을 생성하세요."
        )

        try:
            content = await self._llm.generate(
                system_prompt=_RENDER_TOPIC_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TopicExtractor: 신규 페이지 LLM 호출 실패: slug=%s, %r",
                mention.slug,
                exc,
            )
            return None

        if not content or not content.strip():
            return None

        rel_path = f"topics/{mention.slug}.md"
        return (rel_path, content, concept.confidence)

    async def _update_existing_topic_page(
        self,
        mention: ConceptMention,
        concept: ExtractedConcept,
        meeting_id: str,
        meeting_date: date,
        store: WikiStore,
    ) -> tuple[str, str, int] | None:
        """기존 topic 페이지에 새 회의 인용을 추가한다 (LLM 1회).

        Args:
            mention: 누적 mention 정보.
            concept: 이번 회의 추출 결과.
            meeting_id: 회의 ID.
            meeting_date: 회의 날짜.
            store: WikiStore.

        Returns:
            (rel_path, content, confidence) 또는 None.
        """
        rel_path = f"topics/{mention.slug}.md"
        # 기존 내용 read
        existing_content = ""
        try:
            existing_path = store.root / rel_path
            if existing_path.exists():
                existing_content = existing_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "TopicExtractor: 기존 페이지 read 실패 — skip: %s, %r",
                rel_path,
                exc,
            )
            return None

        # 인용 직렬화
        cit_lines: list[str] = []
        for c in concept.citations:
            cit_lines.append(
                f"- [{c.meeting_id}@{c.timestamp_str}] (timestamp_seconds={c.timestamp_seconds})"
            )

        user_prompt = (
            f"기존 페이지:\n```markdown\n{existing_content}\n```\n\n"
            f"새 회의 ID: {meeting_id}\n"
            f"새 회의 날짜: {meeting_date.isoformat()}\n"
            f"등장 회의 수: {len(mention.meeting_ids)}\n"
            f"새 인용:\n" + "\n".join(cit_lines) + "\n\n"
            "기존 구조를 유지하며 새 회의 인용을 통합한 전체 페이지 본문을 출력하세요."
        )

        try:
            content = await self._llm.generate(
                system_prompt=_UPDATE_TOPIC_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TopicExtractor: 갱신 페이지 LLM 호출 실패: slug=%s, %r",
                mention.slug,
                exc,
            )
            return None

        if not content or not content.strip():
            return None

        return (rel_path, content, concept.confidence)

    @staticmethod
    def _seconds_to_hhmmss(seconds: float) -> str:
        """초 단위 float 를 HH:MM:SS 문자열로 변환한다."""
        try:
            total = int(round(float(seconds)))
        except (TypeError, ValueError):
            return "00:00:00"
        if total < 0:
            total = 0
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        return f"{h:02d}:{m:02d}:{s:02d}"


__all__ = [
    "ConceptMention",
    "ExtractedConcept",
    "TopicExtractor",
]
