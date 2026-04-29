"""WikiLinter TDD Red 단계 테스트 모듈

목적: core/wiki/lint.py 의 WikiLinter, LintHealthReport, ContradictionItem,
      CyclicChain 인터페이스에 대한 Red 단계 테스트를 작성한다.
      core/wiki/lint.py 가 아직 존재하지 않으므로 모든 테스트는 ImportError 로 실패해야 한다.

주요 테스트 범주:
    1. LintHealthReport dataclass 기본 생성 및 to_health_md() 출력 검증 (3건)
    2. 정적 분석 — 고아 페이지 탐지 (2건)
    3. 정적 분석 — 순환 인용 탐지 (2건)
    4. citation 통과율 검증 (2건)
    5. 모순 탐지 LLM 옵션 — enable_contradictions=False/True (2건)
    6. Edge cases — 빈 위키 (1건)

설계 결정:
    - 정적 분석(고아/순환)의 그래프는 {page_path: set[linked_page_path]} 형태의
      인접 리스트(adjacency list)를 사용한다. WikiLinter._find_orphans 는
      전체 링크를 스캔해 incoming-degree = 0 인 노드를 찾고,
      WikiLinter._find_cyclic_citations 는 DFS 기반 사이클 검출을 수행한다.
    - MockVerifier 는 test_guard.py 의 MockCitationVerifier 와 동일 구조를
      이 파일 내에 인라인으로 정의한다 (conftest.py 금지 제약).
    - asyncio_mode = "auto" 이므로 @pytest.mark.asyncio 불필요.

의존성:
    - pytest
    - core.wiki.store.WikiStore (Phase 1, 실제 구현 존재 — tmp_path 격리 사용)
    - core.wiki.lint (Phase 4, 아직 미구현 → ImportError Red)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ─── Phase 4 대상 모듈 (아직 미구현 → ImportError Red) ──────────────────────
from core.wiki.lint import (  # type: ignore[import]  # noqa: E402
    ContradictionItem,
    CyclicChain,
    LintHealthReport,
    WikiLinter,
)

# ─── Phase 1 실제 구현 (변경 금지) ────────────────────────────────────────────
from core.wiki.store import WikiStore

# ─────────────────────────────────────────────────────────────────────────────
# 테스트 전용 MockVerifier — test_guard.py 의 MockCitationVerifier 와 동일 구조
# conftest.py 금지 제약에 따라 이 파일 내부에 인라인 정의
# ─────────────────────────────────────────────────────────────────────────────


class MockVerifier:
    """테스트용 CitationVerifier — known_citations dict 기반.

    (meeting_id, timestamp_seconds) → 발화 텍스트 딕셔너리를 주입받아
    verify_exists 는 키 존재 여부로 True/False 를 반환한다.
    CitationVerifier Protocol 을 만족한다.

    Attributes:
        known_citations: (meeting_id, ts_seconds) → 발화 텍스트 매핑.
        calls: 호출 기록 리스트. 검증 횟수 assert 에 사용.
    """

    def __init__(self, known_citations: dict[tuple[str, int], str]) -> None:
        """알려진 인용 매핑을 초기화한다.

        Args:
            known_citations: (meeting_id, timestamp_seconds) → 발화 텍스트.
        """
        self._known = known_citations
        # 호출 기록 — LLM 호출 횟수 검증에 사용
        self.calls: list[tuple[str, int]] = []

    async def verify_exists(self, meeting_id: str, ts: int) -> bool:
        """주어진 (meeting_id, ts) 가 known_citations 에 존재하면 True 반환.

        Args:
            meeting_id: 8자리 hex 회의 ID.
            ts: timestamp (초 단위 정수).

        Returns:
            known_citations 에 키가 있으면 True, 없으면 False.
        """
        self.calls.append((meeting_id, ts))
        return (meeting_id, ts) in self._known

    async def fetch_utterance(self, meeting_id: str, ts: int) -> str | None:
        """매핑된 발화 텍스트를 반환한다. 없으면 None.

        Args:
            meeting_id: 8자리 hex 회의 ID.
            ts: timestamp (초 단위 정수).

        Returns:
            known_citations 에서 조회한 텍스트. 없으면 None.
        """
        return self._known.get((meeting_id, ts))


# ─────────────────────────────────────────────────────────────────────────────
# 공용 Fixture
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def wiki_root(tmp_path: Path) -> Path:
    """격리된 임시 wiki 루트 디렉토리를 반환한다.

    Returns:
        tmp_path 하위의 'wiki' 디렉토리 경로 (아직 미생성 상태).
    """
    return tmp_path / "wiki"


@pytest.fixture()
def initialized_store(wiki_root: Path) -> WikiStore:
    """init_repo() 가 완료된 WikiStore 인스턴스를 반환한다.

    Args:
        wiki_root: tmp_path 기반 격리 경로.

    Returns:
        init_repo() 가 이미 호출된 WikiStore 인스턴스.
    """
    store = WikiStore(wiki_root)
    store.init_repo()
    return store


@pytest.fixture()
def empty_verifier() -> MockVerifier:
    """known_citations 가 비어있는 MockVerifier 를 반환한다.

    모든 verify_exists 호출이 False 를 반환하므로 phantom 100% 상황.

    Returns:
        빈 MockVerifier 인스턴스.
    """
    return MockVerifier(known_citations={})


@pytest.fixture()
def all_pass_verifier() -> MockVerifier:
    """미리 정의된 인용을 모두 통과시키는 MockVerifier 를 반환한다.

    Returns:
        테스트 인용을 모두 알고 있는 MockVerifier 인스턴스.
    """
    return MockVerifier(
        known_citations={
            ("abc12345", 60): "철수: 5월 1일 출시 확정",
            ("abc12345", 120): "영희: 마케팅 예산 동의",
            ("abc12345", 180): "철수: 온보딩 개편안 시작",
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. LintHealthReport dataclass + to_health_md() 검증 (3건)
# ─────────────────────────────────────────────────────────────────────────────


class TestLintHealthReport:
    """LintHealthReport dataclass 생성 및 to_health_md() 출력을 검증한다."""

    async def test_기본_생성_시_모든_리스트_빈_상태이고_통과율_1이다(self) -> None:
        """LintHealthReport 를 기본값으로 생성하면 모든 컬렉션이 비어있고 citation_pass_rate=1.0 이어야 한다.

        Arrange: last_lint_at 만 전달하여 기본값으로 생성한다.
        Act: LintHealthReport 인스턴스 생성.
        Assert: contradictions=[], orphans=[], cyclic_citations=[], total_pages=0,
                citation_pass_rate=1.0 이다.
        """
        # Arrange & Act
        report = LintHealthReport(last_lint_at="2026-04-29T10:00:00+09:00")

        # Assert
        assert report.contradictions == [], "contradictions 는 기본값이 빈 리스트여야 합니다"
        assert report.orphans == [], "orphans 는 기본값이 빈 리스트여야 합니다"
        assert report.cyclic_citations == [], "cyclic_citations 는 기본값이 빈 리스트여야 합니다"
        assert report.total_pages == 0, "total_pages 는 기본값이 0 이어야 합니다"
        assert report.citation_pass_rate == 1.0, "citation_pass_rate 는 기본값이 1.0 이어야 합니다"

    async def test_to_health_md_는_필수_헤더와_4개_섹션을_포함한다(self) -> None:
        """to_health_md() 출력에는 지정된 헤더와 4개 섹션이 포함되어야 한다.

        Arrange: total_pages=3 인 기본 보고서 생성.
        Act: to_health_md() 호출.
        Assert: '# 위키 건강 보고서' 헤더와 '최종 lint', '✅ 통과',
                '⚠️ 주의', '📊 통계', '❌ 모순' 섹션이 모두 포함된다.
        """
        # Arrange
        report = LintHealthReport(
            last_lint_at="2026-04-29T10:00:00+09:00",
            total_pages=3,
            citation_pass_rate=1.0,
        )

        # Act
        md = report.to_health_md()

        # Assert — 헤더
        assert "# 위키 건강 보고서" in md, "HEALTH.md 에 메인 헤더가 있어야 합니다"
        # Assert — 필수 콘텐츠 키워드
        assert "최종 lint" in md, "'최종 lint' 날짜 레이블이 있어야 합니다"
        # Assert — 4개 섹션
        assert "✅ 통과" in md, "'✅ 통과' 섹션이 있어야 합니다"
        assert "⚠️ 주의" in md, "'⚠️ 주의' 섹션이 있어야 합니다"
        assert "📊 통계" in md, "'📊 통계' 섹션이 있어야 합니다"
        assert "❌ 모순" in md, "'❌ 모순' 섹션이 있어야 합니다"

    async def test_citation_pass_rate_0_5일_때_마크다운에_퍼센트가_표시된다(self) -> None:
        """citation_pass_rate=0.5 일 때 to_health_md() 에 '50.0%' 형식이 포함되어야 한다.

        Arrange: citation_pass_rate=0.5, total_citations=10 보고서 생성.
        Act: to_health_md() 호출.
        Assert: '50.0%' 또는 '50%' 형식의 통과율 문자열이 포함된다.
        """
        # Arrange
        report = LintHealthReport(
            last_lint_at="2026-04-29T10:00:00+09:00",
            citation_pass_rate=0.5,
            total_citations=10,
            total_pages=5,
        )

        # Act
        md = report.to_health_md()

        # Assert — 50% 형식 (소수점 있는 "50.0%" 또는 없는 "50%" 둘 다 허용)
        assert ("50.0%" in md) or ("50%" in md), (
            f"통과율 50% 가 마크다운에 표시되어야 합니다. 실제:\n{md}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. 정적 분석 — 고아 페이지 탐지 (2건)
# ─────────────────────────────────────────────────────────────────────────────


class TestFindOrphans:
    """WikiLinter._find_orphans() 의 고아 페이지 탐지를 검증한다."""

    async def test_모든_페이지가_연결되면_orphans는_빈_리스트다(
        self,
        initialized_store: WikiStore,
        empty_verifier: MockVerifier,
    ) -> None:
        """A → B, B → C, C → A 처럼 모든 페이지가 상호 참조되면 orphans=[] 이어야 한다.

        Arrange:
            - decisions/A.md — topics/B.md 를 링크
            - topics/B.md — people/C.md 를 링크
            - people/C.md — decisions/A.md 를 링크
            WikiLinter 생성.
        Act: linter._find_orphans(pages) 호출.
        Assert: orphans 가 빈 리스트다.
        """
        # Arrange — 모든 페이지가 서로 링크하는 구조
        initialized_store.write_page(
            Path("decisions/A.md"),
            "---\ntype: decision\nmeeting_id: abc12345\ndate: 2026-04-29\nconfidence: 8\n---\n\n"
            "A 페이지입니다. [topics/B.md](topics/B.md) 참조.\n",
        )
        initialized_store.write_page(
            Path("topics/B.md"),
            "---\ntype: topic\n---\n\nB 페이지입니다. [people/C.md](people/C.md) 참조.\n",
        )
        initialized_store.write_page(
            Path("people/C.md"),
            "---\ntype: person\nname: 철수\n---\n\n"
            "C 페이지입니다. [decisions/A.md](decisions/A.md) 참조.\n",
        )
        pages = list(initialized_store.all_pages())
        linter = WikiLinter(
            store=initialized_store,
            verifier=empty_verifier,
        )

        # Act
        orphans = await linter._find_orphans(pages)

        # Assert
        assert orphans == [], (
            f"모든 페이지가 연결되어 있으므로 orphans 는 빈 리스트여야 합니다. 실제: {orphans}"
        )

    async def test_들어오는_링크가_없는_페이지는_orphan으로_분류된다(
        self,
        initialized_store: WikiStore,
        empty_verifier: MockVerifier,
    ) -> None:
        """A → B 링크만 있고 C 에 들어오는 링크가 없으면 C 가 orphan 이어야 한다.

        Arrange:
            - decisions/A.md — topics/B.md 를 링크 (A → B)
            - topics/B.md — 아무 링크 없음
            - topics/C.md — 아무도 참조하지 않음 (고아)
            WikiLinter 생성.
        Act: linter._find_orphans(pages) 호출.
        Assert: orphans 에 'topics/C.md' 가 포함된다.
        """
        # Arrange — C 는 아무도 참조하지 않는 고아
        initialized_store.write_page(
            Path("decisions/A.md"),
            "---\ntype: decision\nmeeting_id: abc12345\ndate: 2026-04-29\nconfidence: 8\n---\n\n"
            "A 페이지. [topics/B.md](topics/B.md) 참조.\n",
        )
        initialized_store.write_page(
            Path("topics/B.md"),
            "---\ntype: topic\n---\n\nB 페이지. 다른 링크 없음.\n",
        )
        initialized_store.write_page(
            Path("topics/C.md"),
            "---\ntype: topic\n---\n\nC 페이지. 아무도 이 페이지를 링크하지 않습니다.\n",
        )
        pages = list(initialized_store.all_pages())
        linter = WikiLinter(
            store=initialized_store,
            verifier=empty_verifier,
        )

        # Act
        orphans = await linter._find_orphans(pages)

        # Assert
        assert "topics/C.md" in orphans, (
            f"들어오는 링크가 없는 topics/C.md 는 orphans 에 포함되어야 합니다. 실제: {orphans}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. 정적 분석 — 순환 인용 탐지 (2건)
# ─────────────────────────────────────────────────────────────────────────────


class TestFindCyclicCitations:
    """WikiLinter._find_cyclic_citations() 의 순환 탐지를 검증한다."""

    async def test_비순환_그래프에서는_cyclic_citations가_빈_리스트다(
        self,
        initialized_store: WikiStore,
        empty_verifier: MockVerifier,
    ) -> None:
        """A → B, B → C 로 단방향 연결만 있으면 cyclic_citations=[] 이어야 한다.

        Arrange:
            - decisions/A.md — topics/B.md 링크
            - topics/B.md — people/C.md 링크
            - people/C.md — 링크 없음
            WikiLinter 생성.
        Act: linter._find_cyclic_citations(pages) 호출.
        Assert: cyclic_citations 가 빈 리스트다.
        """
        # Arrange — DAG (비순환 유향 그래프)
        initialized_store.write_page(
            Path("decisions/A.md"),
            "---\ntype: decision\nmeeting_id: abc12345\ndate: 2026-04-29\nconfidence: 8\n---\n\n"
            "A 결정. [topics/B.md](topics/B.md) 근거.\n",
        )
        initialized_store.write_page(
            Path("topics/B.md"),
            "---\ntype: topic\n---\n\nB 토픽. [people/C.md](people/C.md) 관련자.\n",
        )
        initialized_store.write_page(
            Path("people/C.md"),
            "---\ntype: person\nname: 영희\n---\n\nC 사람. 링크 없음.\n",
        )
        pages = list(initialized_store.all_pages())
        linter = WikiLinter(
            store=initialized_store,
            verifier=empty_verifier,
        )

        # Act
        cycles = await linter._find_cyclic_citations(pages)

        # Assert
        assert cycles == [], f"단방향 연결만 있으므로 순환이 없어야 합니다. 실제: {cycles}"

    async def test_A_B_C_A_순환이_있으면_CyclicChain이_1건_발견된다(
        self,
        initialized_store: WikiStore,
        empty_verifier: MockVerifier,
    ) -> None:
        """A → B → C → A 순환이 있으면 CyclicChain 이 1건 이상 반환되어야 한다.

        Arrange:
            - decisions/A.md — topics/B.md 링크
            - topics/B.md — people/C.md 링크
            - people/C.md — decisions/A.md 링크 (순환 완성)
            WikiLinter 생성.
        Act: linter._find_cyclic_citations(pages) 호출.
        Assert: 반환 리스트에 CyclicChain 이 1건 이상 있고,
                A, B, C 경로가 모두 해당 체인의 pages 에 포함된다.
        """
        # Arrange — A → B → C → A 순환 그래프
        initialized_store.write_page(
            Path("decisions/A.md"),
            "---\ntype: decision\nmeeting_id: abc12345\ndate: 2026-04-29\nconfidence: 8\n---\n\n"
            "A 결정. [topics/B.md](topics/B.md) 관련 토픽.\n",
        )
        initialized_store.write_page(
            Path("topics/B.md"),
            "---\ntype: topic\n---\n\nB 토픽. [people/C.md](people/C.md) 담당자.\n",
        )
        initialized_store.write_page(
            Path("people/C.md"),
            "---\ntype: person\nname: 철수\n---\n\n"
            "C 사람. [decisions/A.md](decisions/A.md) 결정 근거.\n",
        )
        pages = list(initialized_store.all_pages())
        linter = WikiLinter(
            store=initialized_store,
            verifier=empty_verifier,
        )

        # Act
        cycles = await linter._find_cyclic_citations(pages)

        # Assert — CyclicChain 이 1건 이상 발견되어야 함
        assert len(cycles) >= 1, (
            f"A→B→C→A 순환이 있으므로 CyclicChain 이 1건 이상 있어야 합니다. 실제: {cycles}"
        )
        # 순환 체인의 pages 에 3개 노드가 모두 포함되어야 함
        first_chain = cycles[0]
        assert isinstance(first_chain, CyclicChain), (
            f"반환 항목이 CyclicChain 인스턴스여야 합니다. 실제 타입: {type(first_chain)}"
        )
        chain_pages_flat = " ".join(first_chain.pages)
        assert "decisions/A.md" in chain_pages_flat or "A" in chain_pages_flat, (
            "CyclicChain.pages 에 decisions/A.md 가 포함되어야 합니다"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Citation 통과율 검증 (2건)
# ─────────────────────────────────────────────────────────────────────────────


class TestReverifyCitations:
    """WikiLinter._reverify_citations() 의 통과율 계산을 검증한다."""

    async def test_모든_인용이_verifier를_통과하면_pass_rate가_1_0이다(
        self,
        initialized_store: WikiStore,
    ) -> None:
        """verifier 가 모든 인용을 True 로 반환하면 citation_pass_rate=1.0 이어야 한다.

        Arrange:
            - 2개 페이지를 작성, 각각 [meeting:abc12345@00:01:00] 인용 포함
            - 해당 인용을 모두 알고 있는 MockVerifier 준비
            WikiLinter 생성.
        Act: linter._reverify_citations(pages) 호출.
        Assert: 반환된 pass_rate == 1.0 이다.
        """
        # Arrange — 인용이 있는 페이지 2개
        initialized_store.write_page(
            Path("decisions/출시결정.md"),
            "---\ntype: decision\nmeeting_id: abc12345\ndate: 2026-04-29\nconfidence: 8\n---\n\n"
            "5월 출시 확정 [meeting:abc12345@00:01:00].\n",
        )
        initialized_store.write_page(
            Path("people/철수.md"),
            "---\ntype: person\nname: 철수\n---\n\n철수가 제안 [meeting:abc12345@00:02:00].\n",
        )
        # 두 인용을 모두 통과시키는 verifier
        verifier = MockVerifier(
            known_citations={
                ("abc12345", 60): "철수: 5월 출시 확정",
                ("abc12345", 120): "철수: 제안합니다",
            }
        )
        pages = list(initialized_store.all_pages())
        linter = WikiLinter(store=initialized_store, verifier=verifier)

        # Act
        pass_rate, total_citations, _ = await linter._reverify_citations(pages)

        # Assert
        assert pass_rate == 1.0, (
            f"모든 인용이 통과하므로 pass_rate=1.0 이어야 합니다. 실제: {pass_rate}"
        )
        assert total_citations >= 2, (
            f"최소 2개의 인용이 검사되어야 합니다. 실제: {total_citations}"
        )

    async def test_인용_10개_중_3개_phantom이면_pass_rate가_0_7이다(
        self,
        initialized_store: WikiStore,
    ) -> None:
        """10개 인용 중 3개가 phantom(verifier=False) 이면 citation_pass_rate=0.7 이어야 한다.

        Arrange:
            - 10개의 인용이 포함된 페이지들 작성
            - 그 중 7개만 알고 있는 MockVerifier (나머지 3개는 phantom)
            WikiLinter 생성.
        Act: linter._reverify_citations(pages) 호출.
        Assert: pass_rate ≈ 0.7 이다.
        """
        # Arrange — 총 10개 인용 (7개 통과, 3개 phantom)
        # 5개씩 2페이지에 나눠서 인용 배치
        citations_page1 = "\n".join([f"[meeting:abc12345@00:{i:02d}:00]" for i in range(1, 6)])
        citations_page2 = "\n".join([f"[meeting:abc12345@00:{i:02d}:00]" for i in range(6, 11)])
        initialized_store.write_page(
            Path("decisions/결정A.md"),
            f"---\ntype: decision\nmeeting_id: abc12345\ndate: 2026-04-29\nconfidence: 8\n---\n\n"
            f"인용들:\n{citations_page1}\n",
        )
        initialized_store.write_page(
            Path("decisions/결정B.md"),
            f"---\ntype: decision\nmeeting_id: abc12345\ndate: 2026-04-29\nconfidence: 8\n---\n\n"
            f"인용들:\n{citations_page2}\n",
        )

        # 10개 중 7개만 알고 있는 verifier (ts=60~420, 즉 1~7분)
        known: dict[tuple[str, int], str] = {
            ("abc12345", i * 60): f"발화 {i}" for i in range(1, 8)
        }
        verifier = MockVerifier(known_citations=known)
        pages = list(initialized_store.all_pages())
        linter = WikiLinter(store=initialized_store, verifier=verifier)

        # Act
        pass_rate, total_citations, _ = await linter._reverify_citations(pages)

        # Assert — 7/10 = 0.7
        assert total_citations == 10, (
            f"총 10개의 인용이 검사되어야 합니다. 실제: {total_citations}"
        )
        assert abs(pass_rate - 0.7) < 0.01, (
            f"10개 중 7개 통과이므로 pass_rate≈0.7 이어야 합니다. 실제: {pass_rate}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. 모순 탐지 — enable_contradictions 옵션 (2건)
# ─────────────────────────────────────────────────────────────────────────────


class TestContradictions:
    """WikiLinter 의 모순 탐지 LLM 옵션을 검증한다."""

    async def test_enable_contradictions_False_기본값이면_LLM_호출_0회(
        self,
        initialized_store: WikiStore,
        empty_verifier: MockVerifier,
    ) -> None:
        """enable_contradictions=False(기본) 이면 LLM 호출이 0회이고 contradictions=[] 이어야 한다.

        Arrange:
            - 페이지 1개 작성
            - llm mock 을 주입하되 call_count 를 추적
            - enable_contradictions=False (기본값) 으로 WikiLinter 생성
        Act: linter.lint_all() 호출.
        Assert:
            - report.contradictions == []
            - llm mock 의 호출 횟수 == 0
        """
        # Arrange
        initialized_store.write_page(
            Path("decisions/결정.md"),
            "---\ntype: decision\nmeeting_id: abc12345\ndate: 2026-04-29\nconfidence: 8\n---\n\n"
            "테스트 결정 [meeting:abc12345@00:01:00].\n",
        )
        # LLM mock — 호출되면 안 됨
        mock_llm = MagicMock()
        mock_llm.ask = AsyncMock(return_value="[]")
        linter = WikiLinter(
            store=initialized_store,
            verifier=empty_verifier,
            llm=mock_llm,
            enable_contradictions=False,  # 기본값이지만 명시
        )

        # Act
        report = await linter.lint_all(meetings_since_last_lint=1)

        # Assert
        assert report.contradictions == [], (
            f"enable_contradictions=False 이면 contradictions 는 빈 리스트여야 합니다. 실제: {report.contradictions}"
        )
        mock_llm.ask.assert_not_called()

    async def test_enable_contradictions_True이면_LLM이_호출되고_ContradictionItem이_반환된다(
        self,
        initialized_store: WikiStore,
        empty_verifier: MockVerifier,
    ) -> None:
        """enable_contradictions=True 이면 LLM 이 호출되고 모순 1건이 반환되어야 한다.

        Arrange:
            - 페이지 1개 작성 (status 모순 포함)
            - LLM mock 이 ContradictionItem 1건의 JSON 을 반환하도록 설정
            - enable_contradictions=True 로 WikiLinter 생성
        Act: linter.lint_all() 호출.
        Assert:
            - report.contradictions 에 ContradictionItem 1건이 포함된다.
            - llm mock 의 ask 가 1회 이상 호출된다.
        """
        # Arrange — status 모순이 있는 페이지
        initialized_store.write_page(
            Path("projects/온보딩개편.md"),
            "---\ntype: project\nslug: 온보딩개편\nstatus: in-progress\n---\n\n"
            "온보딩 개편안. 이미 출시 완료된 상황이라 다음 단계로 진행.\n",
        )
        # LLM mock — 모순 1건을 JSON 으로 반환
        contradiction_json = (
            '[{"page_path": "projects/온보딩개편.md", '
            '"description": "status 가 in-progress 인데 본문에서 이미 출시 완료", '
            '"confidence": 8, "evidence_lines": [5]}]'
        )
        mock_llm = MagicMock()
        mock_llm.ask = AsyncMock(return_value=contradiction_json)
        linter = WikiLinter(
            store=initialized_store,
            verifier=empty_verifier,
            llm=mock_llm,
            enable_contradictions=True,
        )

        # Act
        report = await linter.lint_all(meetings_since_last_lint=5)

        # Assert — LLM 호출 발생
        assert mock_llm.ask.call_count >= 1, (
            f"enable_contradictions=True 이면 LLM 이 1회 이상 호출되어야 합니다. 실제: {mock_llm.ask.call_count}"
        )
        # Assert — ContradictionItem 반환
        assert len(report.contradictions) >= 1, (
            f"LLM 이 모순 1건을 반환했으므로 contradictions 에 1건 이상 있어야 합니다. 실제: {report.contradictions}"
        )
        assert isinstance(report.contradictions[0], ContradictionItem), (
            f"반환 항목이 ContradictionItem 인스턴스여야 합니다. 실제: {type(report.contradictions[0])}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Edge cases — 빈 위키 (1건)
# ─────────────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    """WikiLinter 의 경계 조건을 검증한다."""

    async def test_페이지가_없는_빈_위키에서는_모든_결과가_기본값이다(
        self,
        initialized_store: WikiStore,
        empty_verifier: MockVerifier,
    ) -> None:
        """페이지가 0개인 빈 위키에서 lint_all() 을 실행하면 안전한 기본값을 반환해야 한다.

        PRD §6 D4 — lint 자체 실패는 ingest 를 막지 않음 (graceful).
        빈 위키에서도 예외 없이 기본 LintHealthReport 를 반환해야 한다.

        Arrange: init_repo() 직후 빈 store, 빈 verifier.
        Act: WikiLinter(store, verifier).lint_all() 호출.
        Assert:
            - orphans == []
            - cyclic_citations == []
            - contradictions == []
            - citation_pass_rate == 1.0 (관례: 인용 없음 = 모두 통과)
            - 예외 없이 LintHealthReport 반환.
        """
        # Arrange
        linter = WikiLinter(
            store=initialized_store,
            verifier=empty_verifier,
        )

        # Act — 예외 발생 없이 완료되어야 함
        report = await linter.lint_all(meetings_since_last_lint=0)

        # Assert
        assert isinstance(report, LintHealthReport), (
            f"LintHealthReport 인스턴스를 반환해야 합니다. 실제: {type(report)}"
        )
        assert report.orphans == [], (
            f"빈 위키의 orphans 는 [] 이어야 합니다. 실제: {report.orphans}"
        )
        assert report.cyclic_citations == [], (
            f"빈 위키의 cyclic_citations 는 [] 이어야 합니다. 실제: {report.cyclic_citations}"
        )
        assert report.contradictions == [], (
            f"빈 위키의 contradictions 는 [] 이어야 합니다. 실제: {report.contradictions}"
        )
        assert report.citation_pass_rate == 1.0, (
            f"인용이 없으면 citation_pass_rate=1.0 (관례) 이어야 합니다. 실제: {report.citation_pass_rate}"
        )
