"""LLM Wiki API 라우터."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.dependencies import get_config as _get_config
from api.dependencies import get_job_queue as _get_job_queue

logger = logging.getLogger(__name__)

router = APIRouter()


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """백그라운드 태스크의 미처리 예외를 로깅한다."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            f"백그라운드 태스크 실패: {task.get_name()}: {exc}",
            exc_info=exc,
        )


# === LLM Wiki Phase 1 엔드포인트 (PRD §7.1 부분 구현) ===
#
# Phase 1 범위: wiki 페이지 목록 조회 + HEALTH 상태 조회 두 가지만.
# - 컴파일/생성/수정/삭제는 Phase 2 이후 도입.
# - wiki.enabled=False 또는 wiki 디렉토리 부재 시 반드시 빈 목록을 돌려준다
#   (404 가 아님). 사용자 경험상 "위키 미활성화" 와 "위키 페이지 0개" 는
#   동일한 의미이므로 200 OK + 빈 배열로 통일한다.
# - core/wiki/* 는 lazy import 하여 wiki 비활성 시 import 비용을 0 으로 둔다.


class WikiPageItem(BaseModel):
    """위키 페이지 목록 항목 응답 스키마.

    Attributes:
        path: wiki 루트 기준 상대 경로 (예: "decisions/2026-04-15-foo.md").
        type: PageType.value 문자열 (예: "decision", "person", "project", "topic").
        title: frontmatter 의 title 필드. 없으면 None.
        last_updated: frontmatter 의 last_updated 필드 (ISO 8601 권장). 없으면 None.
    """

    path: str
    type: str
    title: str | None = None
    last_updated: str | None = None


class WikiPagesResponse(BaseModel):
    """GET /api/wiki/pages 응답 스키마.

    Attributes:
        pages: 위키 페이지 항목 리스트.
        total: 전체 페이지 수.
    """

    pages: list[WikiPageItem]
    total: int


class WikiHealthResponse(BaseModel):
    """GET /api/wiki/health 응답 스키마.

    Phase 1 에서는 D4 자동 lint 가 아직 동작하지 않으므로 status="no_lint_yet"
    을 기본값으로 사용한다. HEALTH.md 가 디스크에 존재하는 경우에는 raw_markdown
    필드로 그대로 노출해 클라이언트가 직접 파싱하도록 한다 (Phase 2 에서
    구조화된 필드로 확장 예정).

    Attributes:
        status: "no_lint_yet" | "ok" | "warnings".
        last_lint_at: 최근 lint 시각 (ISO 8601). 미실행이면 None.
        raw_markdown: HEALTH.md 의 원문 마크다운. 파일이 없으면 None.
    """

    status: str
    last_lint_at: str | None = None
    raw_markdown: str | None = None


@router.get(
    "/wiki/pages",
    response_model=WikiPagesResponse,
    summary="위키 페이지 목록 조회",
    description=(
        "LLM Wiki Phase 1 — wiki 디렉토리 하위의 일반 페이지(decisions/people/"
        "projects/topics) 목록을 반환한다. wiki.enabled=False 거나 디렉토리가 "
        "없으면 빈 목록을 돌려준다."
    ),
)
async def list_wiki_pages(request: Request) -> WikiPagesResponse:
    """위키 페이지 목록을 반환한다 (PRD §7.1).

    동작:
        1. config.wiki.enabled=False → 빈 목록 (200 OK)
        2. wiki 루트 디렉토리 부재 → 빈 목록
        3. wiki 루트 존재 → WikiStore.all_pages() 결과를 직렬화

    Args:
        request: FastAPI Request 객체.

    Returns:
        WikiPagesResponse — 페이지 목록 + 총 개수.
    """
    config = _get_config(request)
    wiki_cfg = getattr(config, "wiki", None)

    # Phase 1 — wiki 비활성 시 즉시 종료.
    if wiki_cfg is None or not getattr(wiki_cfg, "enabled", False):
        return WikiPagesResponse(pages=[], total=0)

    wiki_root: Path = wiki_cfg.resolved_root
    if not wiki_root.exists():
        # 디렉토리 자체가 없으면 위키 페이지도 0개 — 사용자 관점에서는 동일.
        return WikiPagesResponse(pages=[], total=0)

    # core.wiki 는 wiki 활성 시에만 lazy import 한다 (RAG 경로 import 부담 0).
    from core.wiki.store import WikiStore, WikiStoreError  # noqa: PLC0415

    store = WikiStore(wiki_root)
    items: list[WikiPageItem] = []
    for rel_path in store.all_pages():
        try:
            page = store.read_page(rel_path)
        except WikiStoreError as exc:
            # 깨진 페이지 1건 때문에 전체 목록이 깨지지 않도록 경고만 남기고 skip.
            logger.warning("wiki 페이지 read 실패: %s (%s)", rel_path, exc.detail or exc.reason)
            continue
        except Exception as exc:  # noqa: BLE001 — 미지의 파싱 오류 방어
            logger.warning("wiki 페이지 처리 실패: %s (%s)", rel_path, exc)
            continue

        # frontmatter 에서 title / last_updated 만 안전하게 추출.
        fm = page.frontmatter or {}
        title = fm.get("title")
        last_updated = fm.get("last_updated") or fm.get("updated_at")
        items.append(
            WikiPageItem(
                path=str(rel_path),
                type=str(page.page_type.value),
                title=str(title) if title is not None else None,
                last_updated=str(last_updated) if last_updated is not None else None,
            )
        )

    # 경로 사전순 정렬 — 응답을 deterministic 하게 유지.
    items.sort(key=lambda item: item.path)
    return WikiPagesResponse(pages=items, total=len(items))


@router.get(
    "/wiki/health",
    response_model=WikiHealthResponse,
    summary="위키 건강 상태 조회",
    description=(
        "LLM Wiki Phase 1 — wiki/HEALTH.md 의 raw 마크다운을 반환한다. 파일이 "
        "없으면 status=no_lint_yet 을 돌려준다 (D4 자동 lint Phase 2 도입 예정)."
    ),
)
async def get_wiki_health(request: Request) -> WikiHealthResponse:
    """위키 HEALTH.md 의 현재 상태를 반환한다 (PRD §7.1, §6 D4).

    Phase 1 동작:
        - HEALTH.md 가 없으면 status=no_lint_yet, last_lint_at=None.
        - 파일이 있으면 raw_markdown 으로 원문 노출.
        - wiki.enabled=False 라도 HEALTH.md 가 있으면 그대로 반환 (감사용).

    Args:
        request: FastAPI Request 객체.

    Returns:
        WikiHealthResponse — status / last_lint_at / raw_markdown.
    """
    config = _get_config(request)
    wiki_cfg = getattr(config, "wiki", None)

    # wiki 설정이 없거나 root 가 부재면 즉시 no_lint_yet.
    if wiki_cfg is None:
        return WikiHealthResponse(status="no_lint_yet", last_lint_at=None)

    wiki_root: Path = wiki_cfg.resolved_root
    health_path = wiki_root / "HEALTH.md"

    if not health_path.exists():
        return WikiHealthResponse(status="no_lint_yet", last_lint_at=None)

    try:
        raw = health_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("HEALTH.md 읽기 실패: %s (%s)", health_path, exc)
        return WikiHealthResponse(status="no_lint_yet", last_lint_at=None)

    # Phase 1 — 마크다운 본문은 그대로 두고 status 는 보수적으로 ok 로 표시.
    # Phase 2 D4 도입 시 frontmatter 또는 첫 줄 메타데이터에서 status 를 파싱.
    return WikiHealthResponse(status="ok", last_lint_at=None, raw_markdown=raw)


# === LLM Wiki Phase 2.G 엔드포인트 (PRD §7.1) ============================
#
# Phase 2.G 범위: 단일 페이지 raw markdown 조회 + 단순 substring 검색.
# - WikiView (Phase 2.F) 가 트리에서 페이지 클릭 시 호출하는 엔드포인트.
# - 검색은 Phase 2 단순 substring 매칭만 — FTS5/BM25 는 Phase 3 이후.
# - core/wiki/* 는 wiki 활성 시에만 lazy import (RAG 경로 부담 0).

# page_type 화이트리스트 — PRD §4.1 디렉토리 레이아웃과 일치.
# spa.js 는 PageType.value (단수형) 또는 디렉토리명 (복수형) 둘 다 보낼 수 있어
# 양쪽을 모두 수용한다. 화이트리스트 외 입력은 400 으로 차단해 path traversal
# 의 1차 방어선 역할을 겸한다.
_WIKI_PAGE_TYPE_TO_DIRNAME: dict[str, str] = {
    # 복수형 (디스크 디렉토리명)
    "decisions": "decisions",
    "people": "people",
    "projects": "projects",
    "topics": "topics",
    # 단수형 (PageType.value, /api/wiki/pages 응답의 type 필드)
    "decision": "decisions",
    "person": "people",
    "project": "projects",
    "topic": "topics",
}

# 검색 결과 limit 의 안전 상한. 기본 20, 사용자가 100 까지 요청할 수 있고
# 그 이상은 모두 100 으로 클램프하여 응답 크기를 통제한다.
_WIKI_SEARCH_DEFAULT_LIMIT: int = 20
_WIKI_SEARCH_MAX_LIMIT: int = 100

# 검색 snippet 의 양옆 컨텍스트 길이 (q 양쪽으로 잘라낼 글자 수).
_WIKI_SEARCH_SNIPPET_BEFORE: int = 30
_WIKI_SEARCH_SNIPPET_AFTER: int = 30


class WikiCitationItem(BaseModel):
    """단일 페이지에서 추출된 인용 마커 응답 스키마.

    PRD §4.3 인용 형식 표준 `[meeting:{id}@{HH:MM:SS}]` 와 1:1 매핑된다.
    spa.js WikiView 가 인용을 클릭 가능한 링크로 렌더링할 때 사용.

    Attributes:
        meeting_id: 8자리 hex 문자열 (예: "abc12345").
        timestamp: 원문 그대로의 "HH:MM:SS" 문자열.
        timestamp_seconds: HH:MM:SS 를 초 단위 정수로 변환.
    """

    meeting_id: str
    timestamp: str
    timestamp_seconds: int


class WikiPageDetail(BaseModel):
    """GET /api/wiki/pages/{page_type}/{slug} 응답 스키마.

    Attributes:
        path: wiki 루트 기준 상대 경로 (예: "decisions/foo.md").
        type: page_type (디렉토리명, 복수형). spa.js 가 이 값으로 카테고리를
            판정한다.
        title: frontmatter 의 title 또는 본문 첫 H1. 없으면 None.
        content: frontmatter 를 제외한 본문 raw markdown. spa.js 가 인용
            마커를 클릭 가능한 링크로 변환해 렌더링한다.
        frontmatter: YAML 헤더 파싱 결과 (단순 scalar / inline list 만).
        citations: 본문에서 추출된 모든 인용 마커.
    """

    path: str
    type: str
    title: str | None = None
    content: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    citations: list[WikiCitationItem] = Field(default_factory=list)


class WikiSearchResult(BaseModel):
    """단일 검색 결과 항목.

    Attributes:
        path: wiki 루트 기준 상대 경로.
        type: page_type (디렉토리명, 복수형).
        title: 페이지 제목 (frontmatter 또는 첫 H1).
        snippet: q 주변 컨텍스트 발췌 (앞 30 + q + 뒤 30 자 안팎).
        score: 단순 매칭 횟수 (Phase 3 에서 BM25 로 교체 예정).
    """

    path: str
    type: str
    title: str | None = None
    snippet: str
    score: float


class WikiSearchResponse(BaseModel):
    """GET /api/wiki/search 응답 스키마.

    Attributes:
        results: 검색 결과 목록 (score 내림차순 정렬, limit 으로 잘림).
        total: 반환된 results 의 길이 (limit 적용 후).
        query: 요청된 검색어 (응답 검증용 echo).
    """

    results: list[WikiSearchResult]
    total: int
    query: str


def _extract_title_from_markdown(frontmatter: dict[str, Any], content: str) -> str | None:
    """페이지 제목을 frontmatter → 첫 H1 → None 순으로 결정한다.

    Args:
        frontmatter: 파싱된 frontmatter dict.
        content: frontmatter 가 제거된 본문.

    Returns:
        결정된 제목 문자열. 둘 다 없으면 None.
    """
    title = frontmatter.get("title")
    if title is not None:
        # frontmatter 의 title 이 정수/리스트일 가능성을 방어
        return str(title) if not isinstance(title, str) else title

    # 본문 첫 H1 (`# 제목`) 추출 — `## ` 는 H2 이므로 제외
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return stripped[2:].strip()
    return None


def _resolve_wiki_root(request: Request) -> Path | None:
    """wiki 활성 + 루트 디렉토리 존재 여부를 검사하고 root 경로를 반환한다.

    Args:
        request: FastAPI Request 객체.

    Returns:
        wiki 루트 경로. wiki 비활성/디렉토리 부재 시 None.
    """
    config = _get_config(request)
    wiki_cfg = getattr(config, "wiki", None)
    if wiki_cfg is None or not getattr(wiki_cfg, "enabled", False):
        return None
    wiki_root: Path = wiki_cfg.resolved_root
    if not wiki_root.exists():
        return None
    return wiki_root


@router.get(
    "/wiki/pages/{page_type}/{slug:path}",
    response_model=WikiPageDetail,
    summary="위키 단일 페이지 상세 조회",
    description=(
        "LLM Wiki Phase 2.G — 단일 위키 페이지의 raw markdown + frontmatter "
        "+ 인용 목록을 반환한다. wiki.enabled=False / 페이지 부재 시 404, "
        "page_type 화이트리스트 위반·path traversal 시도 시 400 반환."
    ),
)
async def get_wiki_page_detail(request: Request, page_type: str, slug: str) -> WikiPageDetail:
    """위키 단일 페이지의 상세 정보를 반환한다 (PRD §7.1).

    동작:
        1. wiki.enabled=False 또는 디렉토리 부재 → 404
        2. page_type 화이트리스트 위반 → 400
        3. slug 에 `..` 포함 → 400 (path traversal 차단)
        4. 페이지 파일 부재 → 404
        5. 정상 → frontmatter / content / citations 반환

    Args:
        request: FastAPI Request 객체.
        page_type: "decisions" | "people" | "projects" | "topics" (또는 단수형).
        slug: 페이지 슬러그 (확장자 .md 없이) 또는 nested path.

    Returns:
        WikiPageDetail — path / type / title / content / frontmatter / citations.

    Raises:
        HTTPException(400): page_type 화이트리스트 위반 / slug path traversal.
        HTTPException(404): wiki 비활성 / 페이지 부재.
    """
    # ── 1. wiki 활성·디렉토리 검사 → 미활성이면 404 ─────────────────
    wiki_root = _resolve_wiki_root(request)
    if wiki_root is None:
        raise HTTPException(status_code=404, detail="위키가 활성화되어 있지 않습니다.")

    # ── 2. page_type 화이트리스트 검증 → 위반 시 400 ────────────────
    dirname = _WIKI_PAGE_TYPE_TO_DIRNAME.get(page_type)
    if dirname is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"알려지지 않은 page_type: {page_type!r}. "
                "허용 값: decisions / people / projects / topics."
            ),
        )

    # ── 3. slug 검증 — path traversal 시도 1차 차단 ─────────────────
    # FastAPI 가 percent-encoded `..` 를 디코드해서 path 로 넘긴다.
    # WikiStore._validate_relative_path 가 2차 방어를 하지만, 여기서 미리 거부해
    # 명확한 400 메시지를 돌려준다.
    if not slug:
        raise HTTPException(status_code=400, detail="slug 가 비어 있습니다.")
    if ".." in Path(slug).parts:
        raise HTTPException(
            status_code=400,
            detail="slug 에 상위 디렉토리 참조(`..`) 는 허용되지 않습니다.",
        )

    # ── 4. 페이지 read — slug 끝에 .md 가 없으면 자동 부착 ─────────
    # core.wiki 는 wiki 활성 시에만 lazy import (RAG 경로 import 부담 0).
    from core.wiki.store import WikiStore, WikiStoreError  # noqa: PLC0415

    rel_path_str = slug if slug.endswith(".md") else f"{slug}.md"
    rel_path = Path(dirname) / rel_path_str

    store = WikiStore(wiki_root)
    try:
        page = store.read_page(rel_path)
    except WikiStoreError as exc:
        # WikiStore 가 path_traversal / invalid_path 를 추가로 감지할 수 있다.
        if exc.reason in {"path_traversal", "invalid_path"}:
            raise HTTPException(
                status_code=400,
                detail=exc.detail or f"잘못된 경로 요청입니다: {rel_path}",
            ) from exc
        # page_not_found 또는 그 외 디스크 오류 → 404 통일.
        raise HTTPException(
            status_code=404,
            detail=exc.detail or f"페이지를 찾을 수 없습니다: {rel_path}",
        ) from exc

    # ── 5. citations 직렬화 ────────────────────────────────────────
    citation_items: list[WikiCitationItem] = [
        WikiCitationItem(
            meeting_id=c.meeting_id,
            timestamp=c.timestamp_str,
            timestamp_seconds=c.timestamp_seconds,
        )
        for c in page.citations
    ]

    # ── 6. title 결정 (frontmatter > 첫 H1) ────────────────────────
    title = _extract_title_from_markdown(page.frontmatter, page.content)

    return WikiPageDetail(
        path=str(rel_path),
        type=dirname,  # 응답은 항상 복수형(디렉토리명) 으로 통일
        title=title,
        content=page.content,
        frontmatter=dict(page.frontmatter),
        citations=citation_items,
    )


def _make_search_snippet(content: str, query_lower: str) -> str:
    """본문에서 q 주변 컨텍스트를 발췌한 snippet 을 만든다.

    Args:
        content: 페이지 본문 (frontmatter 제거 후).
        query_lower: 소문자로 변환된 검색어.

    Returns:
        앞 30 + q + 뒤 30자 안팎의 발췌 문자열. q 가 본문에 없으면 빈 문자열.
    """
    content_lower = content.lower()
    pos = content_lower.find(query_lower)
    if pos == -1:
        return ""

    start = max(0, pos - _WIKI_SEARCH_SNIPPET_BEFORE)
    end = min(len(content), pos + len(query_lower) + _WIKI_SEARCH_SNIPPET_AFTER)
    snippet = content[start:end]

    # 시작/끝이 잘렸음을 표시하기 위해 ellipsis 추가 (UX 향상).
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return f"{prefix}{snippet}{suffix}".strip()


@router.get(
    "/wiki/search",
    response_model=WikiSearchResponse,
    summary="위키 페이지 전문 검색",
    description=(
        "LLM Wiki Phase 2.G — 단순 substring 매칭으로 페이지 본문/제목을 검색한다. "
        "Phase 3 에서 SQLite FTS5 또는 BM25 로 교체 예정. "
        "wiki.enabled=False 면 빈 결과 반환."
    ),
)
async def search_wiki(
    request: Request,
    q: str = "",
    limit: int = _WIKI_SEARCH_DEFAULT_LIMIT,
) -> WikiSearchResponse:
    """위키 페이지를 단순 substring 매칭으로 검색한다 (PRD §7.1).

    Phase 2 한계 (PRD §8 의 명시적 단순화):
        - FTS5 / BM25 / 토큰화 없음 — 본문에 q 가 그대로 들어있어야 매칭.
        - 동의어 / 형태소 분석 없음.
        - 한국어 어미 변형 매칭 없음 ("결정한", "결정했다" 별도 매칭).
        - score 는 단순 매칭 횟수 — 정규화 안 함.

    Args:
        request: FastAPI Request 객체.
        q: 검색어. 빈 문자열이면 빈 결과 반환.
        limit: 최대 반환 개수 (기본 20, 최대 100).

    Returns:
        WikiSearchResponse — results / total / query.
    """
    # limit 클램프 — 음수·0 은 기본값으로, 100 초과는 100 으로 강제.
    if limit <= 0:
        limit = _WIKI_SEARCH_DEFAULT_LIMIT
    if limit > _WIKI_SEARCH_MAX_LIMIT:
        limit = _WIKI_SEARCH_MAX_LIMIT

    # ── 1. wiki 활성·디렉토리 검사 → 미활성이면 빈 결과 ─────────────
    wiki_root = _resolve_wiki_root(request)
    if wiki_root is None:
        return WikiSearchResponse(results=[], total=0, query=q)

    # ── 2. 빈 q → 빈 결과 ──────────────────────────────────────────
    query = q.strip()
    if not query:
        return WikiSearchResponse(results=[], total=0, query=q)

    query_lower = query.lower()

    # ── 3. 모든 페이지 read 후 매칭 검사 ───────────────────────────
    from core.wiki.store import WikiStore, WikiStoreError  # noqa: PLC0415

    store = WikiStore(wiki_root)
    candidates: list[tuple[float, WikiSearchResult]] = []

    for rel_path in store.all_pages():
        try:
            page = store.read_page(rel_path)
        except WikiStoreError as exc:
            logger.warning(
                "wiki 검색: 페이지 read 실패: %s (%s)",
                rel_path,
                exc.detail or exc.reason,
            )
            continue
        except Exception as exc:  # noqa: BLE001 — 깨진 페이지 1건이 검색을 막지 않게
            logger.warning("wiki 검색: 페이지 처리 실패: %s (%s)", rel_path, exc)
            continue

        # 매칭 대상은 (제목 + 본문). frontmatter title 도 함께 검사.
        fm_title_raw = page.frontmatter.get("title", "") if page.frontmatter else ""
        fm_title = str(fm_title_raw) if fm_title_raw is not None else ""
        haystack = f"{fm_title}\n{page.content}".lower()
        match_count = haystack.count(query_lower)
        if match_count == 0:
            continue

        # 디렉토리명을 type 필드로 직접 사용 (단수/복수 혼동 회피).
        first_part = rel_path.parts[0] if rel_path.parts else ""
        type_str = (
            first_part if first_part in _WIKI_PAGE_TYPE_TO_DIRNAME else str(page.page_type.value)
        )

        title = _extract_title_from_markdown(page.frontmatter, page.content)
        snippet = _make_search_snippet(page.content, query_lower)
        score = float(match_count)

        candidates.append(
            (
                score,
                WikiSearchResult(
                    path=str(rel_path),
                    type=type_str,
                    title=title,
                    snippet=snippet,
                    score=score,
                ),
            )
        )

    # ── 4. score 내림차순 정렬 + limit 적용 ────────────────────────
    # path 를 보조 정렬키로 사용해 동점일 때 deterministic 순서를 보장.
    candidates.sort(key=lambda item: (-item[0], item[1].path))
    results = [item[1] for item in candidates[:limit]]

    return WikiSearchResponse(results=results, total=len(results), query=q)


# === LLM Wiki Phase 4.E 엔드포인트 — 백필 (PRD §7.1, §9 Phase 4) =========
#
# 백필은 long-running 작업이라 동기 API 가 부적합하다. POST 로 작업을 등록
# 하면 백그라운드 태스크가 실행되며 즉시 job_id 를 반환한다. GET 으로 진행
# 상태를 조회하고 cancel 엔드포인트로 중단 가능.
#
# 작업 추적은 in-memory ProgressTracker (dict) 로 단순화 — 서버 재시작 시
# 작업이 사라진다는 단점은 있으나, 백필은 사용자 명시 호출 시점에만 실행
# 되므로 운영상 충분하다 (영속화는 필요 시 Phase 5 에서 SQLite 통합).


# 백필 작업 추적용 in-memory 레지스트리.
# {job_id: {"status": str, "result": BackfillResult|None, "task": asyncio.Task|None,
#           "cancel_event": asyncio.Event, "started_at": str, "current_meeting_id": str|None,
#           "processed": int, "total": int}}
_wiki_backfill_jobs: dict[str, dict[str, Any]] = {}
_wiki_backfill_lock = threading.Lock()


class WikiBackfillRequest(BaseModel):
    """POST /api/wiki/backfill 요청 스키마.

    Attributes:
        since: ISO 날짜 문자열 (예: "2026-04-01"). 지정 시 이 날짜 이후 회의만.
        until: ISO 날짜 문자열. 지정 시 이 날짜 이전(포함) 회의만.
        meeting_ids: 명시적 회의 ID 목록. 지정 시 since/until 무시.
        dry_run: True 면 실제 컴파일 없이 대상 회의 수만 계산.
    """

    since: str | None = Field(
        default=None,
        description="ISO 날짜 (포함), 예: 2026-04-01.",
    )
    until: str | None = Field(
        default=None,
        description="ISO 날짜 (포함), 예: 2026-04-29.",
    )
    meeting_ids: list[str] | None = Field(
        default=None,
        description="명시적 회의 ID 목록. since/until 우선.",
    )
    dry_run: bool = Field(
        default=False,
        description="True 면 컴파일 호출 없이 목록만 시뮬레이션.",
    )


class WikiBackfillStartedResponse(BaseModel):
    """POST /api/wiki/backfill 응답 스키마.

    Attributes:
        job_id: 백필 작업 식별자 (UUID 문자열).
        started_at: ISO8601 시작 시각.
        message: 사람이 읽는 안내 메시지 (한국어).
    """

    job_id: str
    started_at: str
    message: str


class WikiBackfillErrorItem(BaseModel):
    """백필 오류 1건 — BackfillError 직렬화."""

    meeting_id: str
    error_type: str
    message: str


class WikiBackfillStatusResponse(BaseModel):
    """GET /api/wiki/backfill/{job_id} 응답 스키마.

    Attributes:
        job_id: 작업 식별자.
        status: "running" | "completed" | "failed" | "cancelled".
        processed: 현재까지 처리된 회의 수.
        total: 전체 대상 회의 수.
        current_meeting_id: 현재 처리 중인 회의 ID (없으면 None).
        succeeded: 성공한 회의 수.
        skipped: 건너뛴 회의 수.
        failed: 실패한 회의 수.
        errors: 실패 항목 리스트.
        started_at: 시작 시각 (ISO8601).
        finished_at: 종료 시각 (ISO8601). 진행 중이면 None.
        duration_seconds: 경과 시간. 진행 중이면 None.
    """

    job_id: str
    status: str
    processed: int = 0
    total: int = 0
    current_meeting_id: str | None = None
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[WikiBackfillErrorItem] = Field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None


def _get_raw_job_queue(request: Request) -> Any:
    """app.state.job_queue 에서 동기 JobQueue (queue 속성) 를 추출한다.

    Returns:
        ``core.job_queue.JobQueue`` 인스턴스.

    Raises:
        HTTPException: job_queue 가 초기화되지 않았을 때 (503).
    """
    async_queue = _get_job_queue(request)
    # AsyncJobQueue 는 .queue 속성으로 동기 인스턴스를 노출한다.
    raw_queue = getattr(async_queue, "queue", async_queue)
    return raw_queue


@router.post(
    "/wiki/backfill",
    response_model=WikiBackfillStartedResponse,
    status_code=202,
    summary="기존 회의 일괄 위키화 시작",
    description=(
        "Phase 4.E 백필 — wiki.enabled=False 시기의 회의들을 일괄 컴파일한다. "
        "백그라운드 태스크로 실행되며 즉시 job_id 반환. "
        "GET /api/wiki/backfill/{job_id} 로 진행 조회."
    ),
)
async def start_wiki_backfill(
    request: Request,
    body: WikiBackfillRequest,
) -> WikiBackfillStartedResponse:
    """백필 작업을 백그라운드 태스크로 시작한다.

    Args:
        request: FastAPI Request — app.state.job_queue 접근용.
        body: 요청 파라미터.

    Returns:
        WikiBackfillStartedResponse — job_id 와 시작 시각.

    Raises:
        HTTPException(400): since/until 파싱 실패.
        HTTPException(503): job_queue 미초기화.
    """
    import uuid as _uuid

    # Lazy import — 백필 모듈 의존성을 wiki 비활성 환경에 노출하지 않음.
    from scripts import backfill_wiki as _backfill_module  # noqa: PLC0415

    config = _get_config(request)
    raw_queue = _get_raw_job_queue(request)

    # since / until 파싱 — 잘못된 형식은 400.
    since_date: Any = None
    until_date: Any = None
    if body.since:
        try:
            since_date = _backfill_module._parse_iso_date(body.since)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400,
                detail=f"since 형식 오류 (YYYY-MM-DD 사용): {body.since}",
            ) from exc
    if body.until:
        try:
            until_date = _backfill_module._parse_iso_date(body.until)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400,
                detail=f"until 형식 오류 (YYYY-MM-DD 사용): {body.until}",
            ) from exc

    job_id = _uuid.uuid4().hex[:16]
    cancel_event = asyncio.Event()
    from datetime import datetime as _dt

    started_at = _dt.now().isoformat()

    # 작업 상태 슬롯 등록.
    job_state: dict[str, Any] = {
        "status": "running",
        "result": None,
        "task": None,
        "cancel_event": cancel_event,
        "started_at": started_at,
        "finished_at": None,
        "current_meeting_id": None,
        "processed": 0,
        "total": 0,
    }
    with _wiki_backfill_lock:
        _wiki_backfill_jobs[job_id] = job_state

    def _progress_cb(processed: int, total: int, current: str) -> None:
        # 동시 접근은 dict 단위로 안전하지만, 명시적 락으로 일관성 보장.
        with _wiki_backfill_lock:
            job_state["processed"] = processed
            job_state["total"] = total
            job_state["current_meeting_id"] = current

    async def _run_backfill() -> None:
        """백그라운드에서 backfill 호출 후 결과를 job_state 에 저장."""
        try:
            # _backfill_module.backfill 을 직접 호출 (테스트가 monkeypatch 가능).
            result = await _backfill_module.backfill(
                config=config,
                job_queue=raw_queue,
                since=since_date,
                until=until_date,
                meeting_ids=body.meeting_ids,
                dry_run=body.dry_run,
                progress_callback=_progress_cb,
                cancel_event=cancel_event,
            )
            with _wiki_backfill_lock:
                job_state["result"] = result
                if cancel_event.is_set():
                    job_state["status"] = "cancelled"
                elif result.failed > 0 and result.succeeded == 0 and result.total > 0:
                    job_state["status"] = "failed"
                else:
                    job_state["status"] = "completed"
                job_state["finished_at"] = _dt.now().isoformat()
        except Exception as exc:  # noqa: BLE001 — 백그라운드 미처리 예외 격리.
            logger.error("백필 백그라운드 실패: job_id=%s, %r", job_id, exc)
            with _wiki_backfill_lock:
                job_state["status"] = "failed"
                job_state["finished_at"] = _dt.now().isoformat()

    task = asyncio.create_task(_run_backfill(), name=f"wiki_backfill_{job_id}")
    task.add_done_callback(_log_task_exception)
    job_state["task"] = task

    return WikiBackfillStartedResponse(
        job_id=job_id,
        started_at=started_at,
        message=(
            f"백필 작업을 시작했습니다. GET /api/wiki/backfill/{job_id} 로 진행을 확인하세요."
        ),
    )


@router.get(
    "/wiki/backfill/{job_id}",
    response_model=WikiBackfillStatusResponse,
    summary="백필 작업 진행 조회",
    description="등록된 백필 작업의 현재 진행 상태와 결과를 조회한다.",
)
async def get_wiki_backfill_status(
    request: Request,
    job_id: str,
) -> WikiBackfillStatusResponse:
    """백필 작업의 현재 상태를 반환한다.

    Args:
        request: FastAPI Request.
        job_id: 백필 작업 식별자.

    Returns:
        WikiBackfillStatusResponse.

    Raises:
        HTTPException(404): 등록되지 않은 job_id.
    """
    with _wiki_backfill_lock:
        state = _wiki_backfill_jobs.get(job_id)
        if state is None:
            raise HTTPException(
                status_code=404,
                detail=f"백필 작업을 찾을 수 없습니다: {job_id}",
            )
        # 스냅샷 (락 안에서 dict 복사).
        snapshot = dict(state)

    result = snapshot.get("result")
    errors_serialized: list[WikiBackfillErrorItem] = []
    duration: float | None = None
    succeeded = 0
    skipped = 0
    failed = 0
    total = snapshot.get("total", 0)

    if result is not None:
        # BackfillResult 직렬화.
        succeeded = getattr(result, "succeeded", 0)
        skipped = getattr(result, "skipped", 0)
        failed = getattr(result, "failed", 0)
        total = getattr(result, "total", total)
        duration = getattr(result, "duration_seconds", None)
        for err in getattr(result, "errors", []) or []:
            errors_serialized.append(
                WikiBackfillErrorItem(
                    meeting_id=getattr(err, "meeting_id", ""),
                    error_type=getattr(err, "error_type", "unknown"),
                    message=getattr(err, "message", ""),
                )
            )

    return WikiBackfillStatusResponse(
        job_id=job_id,
        status=snapshot.get("status", "running"),
        processed=snapshot.get("processed", 0),
        total=total,
        current_meeting_id=snapshot.get("current_meeting_id"),
        succeeded=succeeded,
        skipped=skipped,
        failed=failed,
        errors=errors_serialized,
        started_at=snapshot.get("started_at"),
        finished_at=snapshot.get("finished_at"),
        duration_seconds=duration,
    )


@router.post(
    "/wiki/backfill/{job_id}/cancel",
    summary="백필 작업 취소",
    description=(
        "실행 중인 백필 작업의 cancel_event 를 set 한다. "
        "현재 처리 중인 회의가 끝난 직후 중단되며, 이후 회의는 처리되지 않는다."
    ),
)
async def cancel_wiki_backfill(
    request: Request,
    job_id: str,
) -> dict[str, str]:
    """백필 작업에 취소 신호를 전송한다.

    Args:
        request: FastAPI Request.
        job_id: 백필 작업 식별자.

    Returns:
        {"job_id": ..., "status": "cancelling"} 형태의 응답.

    Raises:
        HTTPException(404): 등록되지 않은 job_id.
    """
    with _wiki_backfill_lock:
        state = _wiki_backfill_jobs.get(job_id)
        if state is None:
            raise HTTPException(
                status_code=404,
                detail=f"백필 작업을 찾을 수 없습니다: {job_id}",
            )
        cancel_event: asyncio.Event = state["cancel_event"]

    cancel_event.set()
    logger.info("백필 취소 신호 전송: job_id=%s", job_id)
    return {"job_id": job_id, "status": "cancelling"}
