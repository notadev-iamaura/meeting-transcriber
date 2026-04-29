"""Wiki linter — D4 자동 lint (PRD §6 D4)

목적: 5회의마다 1회 위키 전체 건강 검진을 수행하여 HEALTH.md 를 갱신한다.
**자동 수정 금지** — 발견만 보고. (R1 환각 누적 방어 정책 — 자동 수정이
오히려 환각을 정착시킬 위험.)

검진 항목 (4종):
    1. 모순 탐지 (LLM 옵션) — 같은 인물/프로젝트 페이지 내부의 상충 사실
    2. 고아 페이지 (정적) — 들어오는 링크가 0인 페이지
    3. 순환 인용 (정적) — A → B → C → A 형태의 cyclic 페이지 그래프
    4. citation 검증 통과율 (D2 재실행) — 디스크 페이지 vs 회의 utterances

비용 모델:
    - 정적 분석 (2/3) — O(N²) 스캔, LLM 호출 0회
    - 모순 탐지 (1) — config.lint_contradictions=True 일 때만, O(N²) LLM 호출
    - citation 통과율 (1) — D2 verifier 재실행, 페이지 개수 만큼 verifier 호출

운영 정책:
    - 매 5회의 ingest 후 자동 호출 (PRD §6 D4)
    - HEALTH.md 가 갱신되며 git_commit_atomic 으로 커밋
    - lint 자체 실패는 ingest 를 막지 않음 (graceful, log.md 에 경고만)

의존성:
    - core.wiki.store.WikiStore (디스크 read/all_pages)
    - core.wiki.guard.CitationVerifier (D2 재실행용)
    - core.wiki.llm_client.WikiLLMClient (모순 탐지 옵션)
    - core.wiki.models.HealthReport
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.wiki.citations import CITATION_PATTERN
from core.wiki.guard import CitationVerifier
from core.wiki.models import HealthReport
from core.wiki.store import WikiStore, WikiStoreError

logger = logging.getLogger(__name__)


# 정적 분석 상한 — 폭주 방지 (PRD §3 비기능 요구사항 "전체 페이지 수 < 1000").
_DEFAULT_MAX_PAGES_PER_LINT: int = 1000

# 모순 탐지에서 분석할 최대 페이지 수 (LLM 호출 비용 제한).
_MAX_CONTRADICTION_PAGES: int = 50

# 항상 노출되는 특수 페이지 — 고아 검사에서 면제.
_ALWAYS_VISIBLE_PAGES: frozenset[str] = frozenset(
    {"action_items.md", "index.md", "log.md", "HEALTH.md", "CLAUDE.md"}
)

# 페이지 본문에서 다른 페이지를 가리키는 마크다운 링크 패턴.
# 예시:
#   [topics/B.md](topics/B.md)
#   [../people/철수.md](../people/철수.md)
#   [../../decisions/2026-04-15-x.md]
_MD_LINK_PATTERN: re.Pattern[str] = re.compile(r"\[[^\]]+\]\(([^)]+\.md)\)")

# `[../path/file.md]` 형식의 단순 페이지 링크.
_BARE_PAGE_LINK_PATTERN: re.Pattern[str] = re.compile(r"\[((?:\.\./)+[^\]\s]+\.md)\]")


# ─────────────────────────────────────────────────────────────────────────
# 1. 보조 dataclass
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContradictionItem:
    """탐지된 모순 1건.

    Attributes:
        page_path: wiki 루트 기준 상대 경로.
        description: 한국어 설명. LLM 이 자연어로 기술.
        confidence: LLM self-rated 정수 0~10.
        evidence_lines: 모순의 근거가 된 본문 라인 번호 리스트 (1-based).
    """

    page_path: str
    description: str
    confidence: int
    evidence_lines: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class CyclicChain:
    """순환 인용 사이클 1건.

    Attributes:
        pages: 사이클을 구성하는 페이지 경로 리스트.
            마지막 항목이 첫 항목과 동일.
        edges: (from_path, to_path) 의 인용 링크 리스트.
    """

    pages: list[str]
    edges: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class LintHealthReport:
    """전체 lint 결과를 담은 풍부한 보고서.

    Attributes:
        last_lint_at: ISO8601.
        contradictions: ContradictionItem 리스트.
        orphans: 고아 페이지 경로 리스트.
        cyclic_citations: CyclicChain 리스트.
        citation_pass_rate: D2 재검증 통과율 (0.0~1.0).
        total_pages: 검사된 페이지 수.
        total_citations: 검사된 인용 수.
        meetings_since_last_lint: 직전 lint 이후 ingest 된 회의 수.
        lint_duration_seconds: 본 lint 실행 시간.
        llm_calls_made: 모순 탐지에 사용된 LLM 호출 수.
    """

    last_lint_at: str
    contradictions: list[ContradictionItem] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    cyclic_citations: list[CyclicChain] = field(default_factory=list)
    citation_pass_rate: float = 1.0
    total_pages: int = 0
    total_citations: int = 0
    meetings_since_last_lint: int = 0
    lint_duration_seconds: float = 0.0
    llm_calls_made: int = 0

    def to_health_md(self) -> str:
        """PRD §4.2 HEALTH.md 형식 직렬화.

        Returns:
            마크다운 문자열. 헤더 + 4개 섹션(통과/주의/통계/모순) 포함.
        """
        # 통과율 백분율 — 정수면 ".0" 형태로 표시 (테스트는 50.0% / 50% 둘 다 허용)
        pass_rate_pct = self.citation_pass_rate * 100.0

        lines: list[str] = []
        lines.append("# 위키 건강 보고서")
        lines.append("")
        lines.append(f"- 최종 lint: {self.last_lint_at}")
        lines.append(f"- 마지막 lint 이후 회의 수: {self.meetings_since_last_lint}")
        lines.append("")

        # ── ✅ 통과 섹션 ──
        lines.append("## ✅ 통과")
        lines.append("")
        if self.citation_pass_rate >= 1.0:
            lines.append("- 모든 인용이 D2 재검증을 통과했습니다.")
        else:
            lines.append(f"- 인용 검증 통과율 {pass_rate_pct:.1f}% — 일부 phantom citation 존재.")
        if not self.cyclic_citations:
            lines.append("- 순환 인용 없음.")
        lines.append("")

        # ── ⚠️ 주의 섹션 ──
        lines.append("## ⚠️ 주의")
        lines.append("")
        if self.orphans:
            lines.append(f"- 고아 페이지 {len(self.orphans)}건 발견:")
            for path in self.orphans[:20]:  # 상위 20건만 노출
                lines.append(f"  - `{path}`")
        else:
            lines.append("- 고아 페이지 없음.")
        if self.cyclic_citations:
            lines.append(f"- 순환 인용 {len(self.cyclic_citations)}건 발견.")
            for chain in self.cyclic_citations[:10]:
                pretty = " → ".join(chain.pages)
                lines.append(f"  - {pretty}")
        lines.append("")

        # ── 📊 통계 섹션 ──
        lines.append("## 📊 통계")
        lines.append("")
        lines.append(f"- 총 페이지 수: {self.total_pages}")
        lines.append(f"- 총 인용 수: {self.total_citations}")
        lines.append(f"- 인용 검증 통과율: {pass_rate_pct:.1f}%")
        lines.append(f"- lint 실행 시간: {self.lint_duration_seconds:.2f}초")
        lines.append(f"- LLM 호출 수: {self.llm_calls_made}")
        lines.append("")

        # ── ❌ 모순 섹션 ──
        lines.append("## ❌ 모순")
        lines.append("")
        if not self.contradictions:
            lines.append("- 탐지된 모순 없음.")
        else:
            lines.append(f"- 탐지된 모순 {len(self.contradictions)}건:")
            for item in self.contradictions:
                lines.append(
                    f"  - `{item.page_path}` (confidence {item.confidence}): {item.description}"
                )
        lines.append("")

        return "\n".join(lines)

    def to_models_health(self) -> HealthReport:
        """`core.wiki.models.HealthReport` 슬림 변환.

        Returns:
            HealthReport 인스턴스 — store / 외부 API 호환용.
        """
        # cyclic_links 는 tuple[str, ...] 형식
        cyclic_links_tuples: list[tuple[str, ...]] = [
            tuple(chain.pages) for chain in self.cyclic_citations
        ]
        return HealthReport(
            last_lint_at=self.last_lint_at,
            contradictions=[item.page_path for item in self.contradictions],
            orphans=list(self.orphans),
            cyclic_links=cyclic_links_tuples,
            citation_pass_rate=self.citation_pass_rate,
            total_pages=self.total_pages,
            total_citations=self.total_citations,
        )


# ─────────────────────────────────────────────────────────────────────────
# 2. 내부 헬퍼 — 페이지 그래프 빌드
# ─────────────────────────────────────────────────────────────────────────


def _normalize_page_link(target: str, source_dir: Path) -> str | None:
    """링크 문자열을 wiki 루트 기준 상대 경로로 정규화한다.

    링크 해석 정책 (테스트 호환):
        - "../path/file.md" 처럼 "../" 로 시작하면 source_dir 기준 상대 경로.
        - 그 외에는 wiki 루트 기준 (예: "topics/B.md") 으로 해석.

    Args:
        target: 링크 raw 문자열 (예: "../people/철수.md", "topics/B.md").
        source_dir: 소스 페이지의 디렉토리 경로 (wiki 루트 기준).

    Returns:
        정규화된 wiki 루트 기준 상대 경로 또는 해석 실패 시 None.
    """
    if not target:
        return None
    target = target.strip()
    # 절대 URL 또는 외부 링크 거부
    if target.startswith(("http://", "https://", "/")):
        return None

    # `../` 가 등장하면 source_dir 기준 상대 경로로 해석.
    # 그렇지 않으면 wiki 루트 기준 절대 경로로 해석한다.
    if target.startswith("../") or "/../" in target:
        base_parts = list(source_dir.parts)
        target_parts = Path(target).parts
    else:
        base_parts = []
        target_parts = Path(target).parts

    try:
        parts: list[str] = list(base_parts)
        for p in target_parts:
            if p == "..":
                if parts:
                    parts.pop()
            elif p == ".":
                continue
            else:
                parts.append(p)
        if not parts:
            return None
        return "/".join(parts)
    except (ValueError, OSError):
        return None


def _extract_outgoing_links(content: str, source_path: Path) -> set[str]:
    """페이지 본문에서 다른 페이지를 가리키는 링크를 추출한다.

    Args:
        content: 페이지 마크다운 본문.
        source_path: 소스 페이지의 wiki 루트 기준 상대 경로.

    Returns:
        링크된 페이지의 wiki 루트 기준 상대 경로 set.
    """
    links: set[str] = set()
    source_dir = source_path.parent

    # 1. 마크다운 링크 [text](path.md)
    for match in _MD_LINK_PATTERN.finditer(content):
        normalized = _normalize_page_link(match.group(1), source_dir)
        if normalized is not None:
            links.add(normalized)

    # 2. 단순 페이지 링크 [../path/file.md]
    for match in _BARE_PAGE_LINK_PATTERN.finditer(content):
        normalized = _normalize_page_link(match.group(1), source_dir)
        if normalized is not None:
            links.add(normalized)

    return links


# ─────────────────────────────────────────────────────────────────────────
# 3. 모순 탐지 LLM 시스템 프롬프트
# ─────────────────────────────────────────────────────────────────────────


_CONTRADICTION_SYSTEM_PROMPT = """\
당신은 위키 페이지 1건 안에서 frontmatter 와 본문 사이 모순을 탐지하는 분석가입니다.

각 페이지에서 다음 모순을 찾으세요:
- frontmatter status 가 "in-progress" 인데 본문에서 "이미 출시 완료" 라고 기술
- 날짜 모순 (started > target)
- frontmatter role 과 본문에서 자칭 role 이 다름
- 그 외 명백한 사실 충돌

출력은 JSON 배열:
[
  {
    "page_path": "...",
    "description": "...",
    "confidence": 0-10 정수,
    "evidence_lines": [라인번호, ...]
  }
]

모순이 없으면 빈 배열 [] 만 출력하세요.
"""


# ─────────────────────────────────────────────────────────────────────────
# 4. WikiLinter
# ─────────────────────────────────────────────────────────────────────────


class WikiLinter:
    """주기적 위키 건강 검진. 발견만 하고 자동 수정 안 함 (PRD §6 D4).

    Threading:
        - 인스턴스는 stateless (상태는 store/verifier 에 위임).
        - lint_all() 단일 코루틴에서 호출 가정.

    Attributes:
        _store: WikiStore.
        _verifier: CitationVerifier.
        _llm: 모순 탐지용 (None 이면 모순 검사 비활성화).
        _enable_contradictions: bool. False 면 LLM 호출 0회.
        _max_pages_per_lint: 정적 분석 상한.
    """

    def __init__(
        self,
        store: WikiStore,
        verifier: CitationVerifier,
        *,
        llm: Any | None = None,
        enable_contradictions: bool = False,
        max_pages_per_lint: int = _DEFAULT_MAX_PAGES_PER_LINT,
    ) -> None:
        """WikiLinter 를 초기화한다.

        Args:
            store: WikiStore 인스턴스.
            verifier: CitationVerifier (Phase 4 의 UtterancesCitationVerifier 등).
            llm: WikiLLMClient (선택). None 이면 모순 탐지 비활성화.
            enable_contradictions: True 면 LLM 호출하여 모순 탐지.
            max_pages_per_lint: 정적 분석 상한. 폭주 방지용.
        """
        self._store: WikiStore = store
        self._verifier: CitationVerifier = verifier
        self._llm: Any | None = llm
        self._enable_contradictions: bool = enable_contradictions
        self._max_pages_per_lint: int = max_pages_per_lint
        # LLM 호출 누적 카운터 (보고서 작성용)
        self._llm_calls_made: int = 0

    async def lint_all(
        self,
        *,
        meetings_since_last_lint: int = 0,
    ) -> LintHealthReport:
        """전체 위키 검진. 4가지 검사 항목을 순차 실행.

        Args:
            meetings_since_last_lint: 메타 정보로 보고서에 기록.

        Returns:
            LintHealthReport. 절대 raise 하지 않음.
        """
        import time

        start_ts = time.time()
        self._llm_calls_made = 0

        # ── 1. 페이지 목록 수집 (실패 격리) ─────────────────────────
        pages: list[Path] = []
        try:
            pages = list(self._store.all_pages())
            # 정렬 — 결정적 결과를 위해
            pages.sort(key=lambda p: str(p))
            # 상한 적용
            if len(pages) > self._max_pages_per_lint:
                logger.warning(
                    "lint: 페이지 수 %d 가 상한 %d 초과 — 처음 %d 건만 처리",
                    len(pages),
                    self._max_pages_per_lint,
                    self._max_pages_per_lint,
                )
                pages = pages[: self._max_pages_per_lint]
        except Exception as exc:  # noqa: BLE001 — graceful 폴백
            logger.warning("lint: 페이지 열거 실패 — 빈 보고서 반환: %r", exc)
            return LintHealthReport(
                last_lint_at=self._now_iso(),
                meetings_since_last_lint=meetings_since_last_lint,
                lint_duration_seconds=time.time() - start_ts,
            )

        # ── 2. 정적 분석 — 고아 + 순환 ───────────────────────────────
        try:
            orphans = await self._find_orphans(pages)
        except Exception as exc:  # noqa: BLE001
            logger.warning("lint: _find_orphans 실패: %r", exc)
            orphans = []

        try:
            cyclic_chains = await self._find_cyclic_citations(pages)
        except Exception as exc:  # noqa: BLE001
            logger.warning("lint: _find_cyclic_citations 실패: %r", exc)
            cyclic_chains = []

        # ── 3. citation 통과율 ──────────────────────────────────────
        try:
            pass_rate, total_citations, _pages_with_citations = await self._reverify_citations(
                pages
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("lint: _reverify_citations 실패: %r", exc)
            pass_rate, total_citations = 1.0, 0

        # ── 4. 모순 탐지 (옵션) ─────────────────────────────────────
        contradictions: list[ContradictionItem] = []
        if self._enable_contradictions and self._llm is not None:
            try:
                contradictions = await self._find_contradictions(pages)
            except Exception as exc:  # noqa: BLE001
                logger.warning("lint: _find_contradictions 실패: %r", exc)
                contradictions = []

        duration = time.time() - start_ts

        return LintHealthReport(
            last_lint_at=self._now_iso(),
            contradictions=contradictions,
            orphans=orphans,
            cyclic_citations=cyclic_chains,
            citation_pass_rate=pass_rate,
            total_pages=len(pages),
            total_citations=total_citations,
            meetings_since_last_lint=meetings_since_last_lint,
            lint_duration_seconds=duration,
            llm_calls_made=self._llm_calls_made,
        )

    # ── 내부 4가지 검사 메서드 ─────────────────────────────────────

    async def _find_orphans(self, pages: list[Path]) -> list[str]:
        """고아 페이지 탐지 (정적, LLM 0회).

        알고리즘:
            1. 모든 페이지의 incoming link 카운트 빌드.
            2. 카운트 == 0 인 페이지를 고아로 분류.
            3. 특수 페이지(action_items.md 등)는 제외.

        Args:
            pages: wiki 루트 기준 상대 경로 리스트.

        Returns:
            고아 페이지의 경로 (정렬됨).
        """
        # 모든 페이지의 incoming-degree 카운트
        incoming_count: dict[str, int] = {str(p).replace("\\", "/"): 0 for p in pages}

        for page_path in pages:
            try:
                content = self._read_page_content(page_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("lint: 페이지 read 실패 — skip: %s, %r", page_path, exc)
                continue

            outgoing = _extract_outgoing_links(content, page_path)
            for target in outgoing:
                if target in incoming_count:
                    incoming_count[target] += 1

        orphans: list[str] = []
        for path_str, count in incoming_count.items():
            if count > 0:
                continue
            # 특수 페이지 면제
            name = Path(path_str).name
            if name in _ALWAYS_VISIBLE_PAGES:
                continue
            orphans.append(path_str)

        orphans.sort()
        return orphans

    async def _find_cyclic_citations(self, pages: list[Path]) -> list[CyclicChain]:
        """순환 인용 탐지 (정적, DFS, LLM 0회).

        알고리즘:
            1. 각 페이지의 outgoing 링크로 directed graph 구축.
            2. DFS 로 사이클 검출.
            3. self-loop 도 포함.

        Args:
            pages: wiki 루트 기준 상대 경로 리스트.

        Returns:
            CyclicChain 리스트.
        """
        # 노드 수 상한 — 폭주 방지
        if len(pages) > self._max_pages_per_lint:
            return []

        # 인접 리스트 빌드
        graph: dict[str, set[str]] = {}
        page_set: set[str] = {str(p).replace("\\", "/") for p in pages}
        for page_path in pages:
            path_str = str(page_path).replace("\\", "/")
            try:
                content = self._read_page_content(page_path)
            except Exception:  # noqa: BLE001
                graph[path_str] = set()
                continue
            outgoing = _extract_outgoing_links(content, page_path)
            # graph 노드는 페이지 set 안의 것만 (외부 링크 무시)
            graph[path_str] = outgoing & page_set

        # DFS 로 사이클 검출 — 첫 발견 후 중복 사이클은 정규화하여 dedupe
        cycles_found: list[CyclicChain] = []
        seen_cycle_keys: set[tuple[str, ...]] = set()

        WHITE, GRAY, BLACK = 0, 1, 2  # noqa: N806
        color: dict[str, int] = {node: WHITE for node in graph}
        # DFS 스택 — 경로를 추적하여 사이클 발견 시 슬라이스
        path: list[str] = []
        path_set: set[str] = set()

        def visit(node: str) -> None:
            """단일 노드에 대한 DFS 방문."""
            if color.get(node, WHITE) == BLACK:
                return
            color[node] = GRAY
            path.append(node)
            path_set.add(node)

            for neighbor in sorted(graph.get(node, set())):
                if neighbor == node:
                    # self-loop
                    cycle_pages = [node, node]
                    key = tuple(sorted([node]))
                    if key not in seen_cycle_keys:
                        seen_cycle_keys.add(key)
                        cycles_found.append(
                            CyclicChain(
                                pages=cycle_pages,
                                edges=[(node, node)],
                            )
                        )
                    continue
                if neighbor in path_set:
                    # 사이클 발견 — neighbor 부터 현재 노드까지를 슬라이스
                    idx = path.index(neighbor)
                    cycle_nodes = path[idx:] + [neighbor]
                    # 정규화 키: 사이클 노드 set
                    key = tuple(sorted(set(cycle_nodes)))
                    if key not in seen_cycle_keys:
                        seen_cycle_keys.add(key)
                        edges: list[tuple[str, str]] = []
                        for i in range(len(cycle_nodes) - 1):
                            edges.append((cycle_nodes[i], cycle_nodes[i + 1]))
                        cycles_found.append(CyclicChain(pages=cycle_nodes, edges=edges))
                    continue
                if color.get(neighbor, WHITE) == WHITE:
                    visit(neighbor)

            color[node] = BLACK
            path.pop()
            path_set.discard(node)

        for node in sorted(graph.keys()):
            if color.get(node, WHITE) == WHITE:
                visit(node)

        return cycles_found

    async def _reverify_citations(self, pages: list[Path]) -> tuple[float, int, int]:
        """citation 검증 통과율 재계산.

        Args:
            pages: wiki 루트 기준 상대 경로 리스트.

        Returns:
            (pass_rate, total_citations, total_pages_with_citations).
        """
        total_citations = 0
        passed_citations = 0
        pages_with_citations = 0

        for page_path in pages:
            try:
                content = self._read_page_content(page_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("lint: 페이지 read 실패 — skip: %s, %r", page_path, exc)
                continue

            page_had_citation = False
            for match in CITATION_PATTERN.finditer(content):
                page_had_citation = True
                total_citations += 1
                meeting_id = match.group(1)
                hh, mm, ss = match.group(2), match.group(3), match.group(4)
                ts = int(hh) * 3600 + int(mm) * 60 + int(ss)
                try:
                    ok = await self._verifier.verify_exists(meeting_id, ts)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "lint: verifier 오류 — phantom 처리: %s, %r",
                        match.group(0),
                        exc,
                    )
                    ok = False
                if ok:
                    passed_citations += 1

            if page_had_citation:
                pages_with_citations += 1

        if total_citations == 0:
            # 인용 자체가 없으면 통과율 1.0 (관례)
            return (1.0, 0, pages_with_citations)

        pass_rate = passed_citations / total_citations
        return (pass_rate, total_citations, pages_with_citations)

    async def _find_contradictions(self, pages: list[Path]) -> list[ContradictionItem]:
        """모순 탐지 (LLM 호출, enable_contradictions=True 일 때만).

        Args:
            pages: wiki 루트 기준 상대 경로 리스트.

        Returns:
            ContradictionItem 리스트.
        """
        if self._llm is None:
            return []

        # 페이지 수 상한 — confidence 상위 50건만 처리는 단순화 (현재는 처음 50건)
        target_pages = pages[:_MAX_CONTRADICTION_PAGES]

        contradictions: list[ContradictionItem] = []
        for page_path in target_pages:
            try:
                content = self._read_page_content(page_path)
            except Exception:  # noqa: BLE001
                continue

            user_prompt = (
                f"페이지 경로: {page_path}\n\n"
                f"```markdown\n{content}\n```\n\n"
                "위 페이지에서 모순을 탐지하여 JSON 배열로 출력하세요."
            )

            try:
                # `ask` 또는 `generate` 둘 다 지원 — 테스트는 `ask` 를 mock
                if hasattr(self._llm, "ask"):
                    raw_response = await self._llm.ask(
                        system_prompt=_CONTRADICTION_SYSTEM_PROMPT,
                        user_prompt=user_prompt,
                    )
                else:
                    raw_response = await self._llm.generate(
                        system_prompt=_CONTRADICTION_SYSTEM_PROMPT,
                        user_prompt=user_prompt,
                    )
                self._llm_calls_made += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "lint: 모순 탐지 LLM 호출 실패 — skip: %s, %r",
                    page_path,
                    exc,
                )
                continue

            parsed = self._parse_contradiction_response(raw_response)
            for item in parsed:
                contradictions.append(item)

        return contradictions

    # ── 내부 헬퍼 ──────────────────────────────────────────────────

    def _read_page_content(self, rel_path: Path) -> str:
        """페이지 본문 raw 텍스트를 읽는다 (절대 경로 결합 없이 직접).

        WikiStore.read_page() 는 frontmatter 를 분리하지만 lint 는 raw text 가
        필요하므로 직접 read 한다.

        보안: wiki 루트 밖으로 탈출하는 경로를 차단한다.
            - ".." segment 포함 시 거부
            - resolve() 후 root 기준 상대 경로 검사

        Args:
            rel_path: wiki 루트 기준 상대 경로.

        Returns:
            페이지 raw 텍스트.

        Raises:
            WikiStoreError 또는 OSError: 디스크 read 실패 시.
            ValueError: path traversal 시도 감지 시.
        """
        # path traversal 방어 — all_pages() 경로는 안전하지만
        # 향후 외부 입력이 직접 전달될 경우를 대비한 심층 방어
        if ".." in rel_path.parts or rel_path.is_absolute():
            raise ValueError(
                f"lint._read_page_content: path traversal 또는 절대 경로 거부: {rel_path}"
            )
        abs_path = self._store.root / rel_path
        # symlink resolve 후 root 내부인지 이중 검사
        try:
            resolved = abs_path.resolve()
            root_resolved = self._store.root.resolve()
            resolved.relative_to(root_resolved)  # ValueError if outside
        except ValueError as exc:
            raise ValueError(f"lint._read_page_content: root 외부 경로 거부: {rel_path}") from exc
        return abs_path.read_text(encoding="utf-8")

    @staticmethod
    def _now_iso() -> str:
        """ISO8601 형식 현재 시각 (한국 시간대 권장이나 단순 isoformat)."""
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _parse_contradiction_response(raw: str) -> list[ContradictionItem]:
        """LLM 응답 JSON 을 ContradictionItem 리스트로 파싱한다.

        Args:
            raw: LLM 원시 응답.

        Returns:
            파싱된 ContradictionItem 리스트. 실패 시 빈 리스트.
        """
        if not raw or not raw.strip():
            return []
        text = raw.strip()
        # JSON 배열 추출 — 첫 [ 와 마지막 ] 사이
        try:
            data: Any = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            start = text.find("[")
            end = text.rfind("]")
            if start == -1 or end == -1 or end <= start:
                return []
            try:
                data = json.loads(text[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                return []

        if not isinstance(data, list):
            return []

        results: list[ContradictionItem] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            page_path = str(item.get("page_path", "")).strip()
            description = str(item.get("description", "")).strip()
            if not page_path or not description:
                continue
            try:
                confidence = int(item.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0
            evidence_raw = item.get("evidence_lines", [])
            evidence_lines: list[int] = []
            if isinstance(evidence_raw, list):
                for x in evidence_raw:
                    try:
                        evidence_lines.append(int(x))
                    except (TypeError, ValueError):
                        continue
            results.append(
                ContradictionItem(
                    page_path=page_path,
                    description=description,
                    confidence=confidence,
                    evidence_lines=evidence_lines,
                )
            )
        return results


# ─────────────────────────────────────────────────────────────────────────
# 5. 외부 노출 — public API
# ─────────────────────────────────────────────────────────────────────────


__all__ = [
    "ContradictionItem",
    "CyclicChain",
    "LintHealthReport",
    "WikiLinter",
]


# WikiStoreError 는 lint.py 내부에서 사용하지 않지만 공개 API 일관성을 위해 보존
_ = WikiStoreError  # noqa: B018
