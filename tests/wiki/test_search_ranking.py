"""WikiSearchIndex 다중신호 재랭킹(C1) 동작 테스트.

격차② — BM25 단일 점수를 recency·confidence·인용빈도·superseded 패널티(+선택적 MMR)와
결합한 후처리 재랭킹을 검증한다. 각 신호는 가중치를 격리 주입하여 단독 효과를 증명하고,
`enabled=False` escape hatch 가 순수 BM25 순서를 그대로 보존함을 확인한다.

결정성 보장:
    - 모든 검색은 `now` 를 명시적으로 고정한다(오늘 날짜 의존 제거).
    - 모든 테스트는 `WikiRankingConfig` 를 직접 주입한다(전역 config 비의존).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from config import WikiRankingConfig
from core.wiki.search_index import WikiSearchIndex
from core.wiki.store import WikiStore

# 재랭킹 신호를 격리하기 위한 고정 시점 (테스트 결정성).
_NOW = date(2026, 6, 1)


def _decision_md(
    *,
    title: str,
    body: str,
    status: str = "decided",
    project: str = "Apollo",
    participants: str = "[민수, 지연]",
    owners: str = "[지연]",
    confidence: int | str = 9,
    decision_date: str = "2026-05-21",
    last_updated: str = "2026-05-21T10:00:00",
    source_meetings: str = "[1234abcd]",
) -> str:
    """재랭킹 테스트용 canonical decision markdown 을 생성한다."""
    return f"""---
type: decision
title: {title}
status: {status}
decision_date: {decision_date}
project: {project}
participants: {participants}
owners: {owners}
confidence: {confidence}
source_meetings: {source_meetings}
last_updated: {last_updated}
---

# {title}

{body}
"""


def _index(tmp_path: Path, pages: dict[str, str], ranking: WikiRankingConfig) -> WikiSearchIndex:
    """주어진 페이지로 위키 스토어를 채우고 재랭킹 설정을 주입한 인덱스를 반환한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    for rel, md in pages.items():
        store.write_page(Path(rel), md)
    index = WikiSearchIndex(store.root, ranking=ranking)
    index.rebuild(store)
    return index


def test_recency가_동일조건에서_최신_결정을_상위로_올린다(tmp_path: Path) -> None:
    """w_recency 단독: 본문이 동일해도 decision_date 가 최신인 페이지가 먼저 온다."""
    body = "예산 배정을 합의했다. [meeting:1234abcd@00:01:20]"
    ranking = WikiRankingConfig(
        enabled=True,
        w_bm25=0.0,
        w_recency=1.0,
        w_confidence=0.0,
        w_citation=0.0,
        superseded_penalty=0.0,
    )
    index = _index(
        tmp_path,
        {
            "decisions/recent.md": _decision_md(
                title="결정 A", decision_date="2026-05-30", body=body
            ),
            "decisions/old.md": _decision_md(
                title="결정 B", decision_date="2026-01-01", body=body
            ),
        },
        ranking,
    )

    results = index.search("예산", now=_NOW)

    assert [r.page_path for r in results] == [
        "decisions/recent.md",
        "decisions/old.md",
    ]


def test_confidence가_동일조건에서_고신뢰_결정을_상위로_올린다(tmp_path: Path) -> None:
    """w_confidence 단독: 동일 본문/날짜에서 confidence 가 높은 페이지가 먼저 온다."""
    body = "예산 배정을 합의했다. [meeting:1234abcd@00:01:20]"
    ranking = WikiRankingConfig(
        enabled=True,
        w_bm25=0.0,
        w_recency=0.0,
        w_confidence=1.0,
        w_citation=0.0,
        superseded_penalty=0.0,
    )
    index = _index(
        tmp_path,
        {
            "decisions/high.md": _decision_md(title="결정 A", confidence=10, body=body),
            "decisions/low.md": _decision_md(title="결정 B", confidence=2, body=body),
        },
        ranking,
    )

    results = index.search("예산", now=_NOW)

    assert [r.page_path for r in results] == ["decisions/high.md", "decisions/low.md"]


def test_인용빈도가_높은_결정을_상위로_올린다(tmp_path: Path) -> None:
    """w_citation 단독: 인용(citation marker) 이 더 많은 페이지가 먼저 온다."""
    ranking = WikiRankingConfig(
        enabled=True,
        w_bm25=0.0,
        w_recency=0.0,
        w_confidence=0.0,
        w_citation=1.0,
        superseded_penalty=0.0,
    )
    index = _index(
        tmp_path,
        {
            "decisions/many.md": _decision_md(
                title="결정 A",
                body=(
                    "예산 합의. [meeting:1234abcd@00:01:20] "
                    "[meeting:1234abcd@00:02:30] [meeting:1234abcd@00:03:40]"
                ),
            ),
            "decisions/few.md": _decision_md(
                title="결정 B", body="예산 합의. [meeting:1234abcd@00:01:20]"
            ),
        },
        ranking,
    )

    results = index.search("예산", now=_NOW)

    assert [r.page_path for r in results] == ["decisions/many.md", "decisions/few.md"]


def test_superseded는_기본가중치에서_구조적으로_모든_live_아래로_간다(tmp_path: Path) -> None:
    """역전 0% 구조 보장: superseded 가 BM25·최신성·신뢰도·인용 모두 우위여도 live 결정 아래.

    기본 WikiRankingConfig(penalty=0.5)로 검증한다 — 가중치 과장 없이도, superseded 는
    어떤 비-superseded 결정보다 위로 올라오지 못한다.
    """
    ranking = WikiRankingConfig()  # 기본값(penalty=0.5)
    index = _index(
        tmp_path,
        {
            # superseded 인데 모든 신호 최강 (최신·고신뢰·다인용·BM25 우위)
            "decisions/superseded.md": _decision_md(
                title="옛 결정",
                status="superseded",
                decision_date="2026-05-31",
                confidence=10,
                body=(
                    "예산 예산 재조정. [meeting:1234abcd@00:01:20] "
                    "[meeting:1234abcd@00:02:30]"
                ),
            ),
            # live(decided) 인데 모든 신호 최약 (오래됨·저신뢰·1인용·BM25 열위)
            "decisions/current.md": _decision_md(
                title="새 결정",
                status="decided",
                decision_date="2026-01-01",
                confidence=1,
                body="예산 재조정. [meeting:1234abcd@00:03:00]",
            ),
        },
        ranking,
    )

    results = index.search("예산", now=_NOW)

    assert results[0].page_path == "decisions/current.md"
    assert results[-1].page_path == "decisions/superseded.md"


def test_enabled_플래그가_BM25순서와_재랭킹순서를_정반대로_바꾼다(tmp_path: Path) -> None:
    """escape hatch 증명: 같은 데이터에서 enabled=False 는 BM25 순서, True 는 재랭킹 순서.

    old_strong 은 BM25 우위(예산 다수)·오래됨, recent_weak 는 BM25 열위·최신.
    enabled=False → BM25 → [old_strong, recent_weak]
    enabled=True(w_recency 우세) → [recent_weak, old_strong] (정반대)
    enabled 플래그를 무시하면 두 순서가 같아져 이 대조 검증이 깨진다.
    """
    pages = {
        "decisions/old_strong.md": _decision_md(
            title="결정 A",
            decision_date="2026-01-01",
            body="예산 예산 예산 재조정. [meeting:1234abcd@00:01:20]",
        ),
        "decisions/recent_weak.md": _decision_md(
            title="결정 B",
            decision_date="2026-05-30",
            body="예산 재조정. [meeting:1234abcd@00:02:00]",
        ),
    }
    on = WikiRankingConfig(
        enabled=True,
        w_bm25=0.0,
        w_recency=1.0,
        w_confidence=0.0,
        w_citation=0.0,
        superseded_penalty=0.0,
    )
    off = WikiRankingConfig(enabled=False)

    order_on = [r.page_path for r in _index(tmp_path / "on", pages, on).search("예산", now=_NOW)]
    order_off = [r.page_path for r in _index(tmp_path / "off", pages, off).search("예산", now=_NOW)]

    assert order_on == ["decisions/recent_weak.md", "decisions/old_strong.md"]
    assert order_off == ["decisions/old_strong.md", "decisions/recent_weak.md"]
    assert order_on == list(reversed(order_off))


def test_superseded_외_종결상태_rejected는_패널티_대상이_아니다(tmp_path: Path) -> None:
    """범위 명시: 구조적 하향은 status=superseded 만 대상. rejected 등은 비대상."""
    ranking = WikiRankingConfig(
        enabled=True,
        w_bm25=1.0,
        w_recency=0.0,
        w_confidence=0.0,
        w_citation=0.0,
        superseded_penalty=2.0,
    )
    index = _index(
        tmp_path,
        {
            # rejected 인데 BM25 우위 → superseded 가 아니므로 구조적 하향 비대상 → 1위 유지
            "decisions/rejected.md": _decision_md(
                title="반려 A",
                status="rejected",
                body="예산 예산 재조정. [meeting:1234abcd@00:01:20]",
            ),
            "decisions/decided.md": _decision_md(
                title="확정 B",
                status="decided",
                body="예산 재조정. [meeting:1234abcd@00:02:00]",
            ),
        },
        ranking,
    )

    results = index.search("예산", now=_NOW)

    assert results[0].page_path == "decisions/rejected.md"


def test_기본가중치에서_최신_고신뢰_다인용_결정이_종합_상위다(tmp_path: Path) -> None:
    """기본 WikiRankingConfig: 최신·고신뢰·다인용·decided 가 종합 점수 1위가 된다."""
    ranking = WikiRankingConfig()  # 기본값(enabled=True)
    index = _index(
        tmp_path,
        {
            "decisions/best.md": _decision_md(
                title="핵심 결정",
                status="decided",
                decision_date="2026-05-30",
                confidence=10,
                body=(
                    "예산 확정. [meeting:1234abcd@00:01:20] "
                    "[meeting:1234abcd@00:02:30]"
                ),
            ),
            "decisions/worst.md": _decision_md(
                title="폐기 결정",
                status="superseded",
                decision_date="2026-01-01",
                confidence=2,
                body="예산 확정. [meeting:1234abcd@00:01:20]",
            ),
        },
        ranking,
    )

    results = index.search("예산", now=_NOW)

    assert results[0].page_path == "decisions/best.md"


def test_decision_date가_없으면_recency_0으로_최신_페이지_아래로_간다(tmp_path: Path) -> None:
    """엣지: date 부재 → recency 0. 크래시 없이, 정상 date 페이지보다 하위로 격리 증명."""
    ranking = WikiRankingConfig(
        enabled=True,
        w_bm25=0.0,
        w_recency=1.0,
        w_confidence=0.0,
        w_citation=0.0,
        superseded_penalty=0.0,
    )
    index = _index(
        tmp_path,
        {
            "decisions/dated.md": _decision_md(
                title="결정 A",
                decision_date="2026-05-30",
                body="예산 합의. [meeting:1234abcd@00:01:20]",
            ),
            "decisions/nodate.md": _decision_md(
                title="결정 B",
                decision_date="",
                last_updated="",
                body="예산 합의. [meeting:1234abcd@00:02:00]",
            ),
        },
        ranking,
    )

    results = index.search("예산", now=_NOW)

    assert [r.page_path for r in results] == ["decisions/dated.md", "decisions/nodate.md"]


def test_candidate_pool이_재랭킹_후보를_BM25_상위로_제한한다(tmp_path: Path) -> None:
    """알려진 한계의 회귀 안전망: candidate_pool 밖(BM25 하위)의 최신 결정은 끌어올려지지 않는다.

    pool=1, top_k=1 이면 BM25 1위만 후보가 되어, 더 최신인 페이지가 있어도 재랭킹
    대상에서 제외된다. corpus·쿼리 매칭 폭이 커지면 candidate_pool 상향이 필요함을 명문화.
    """
    ranking = WikiRankingConfig(
        enabled=True,
        candidate_pool=1,
        w_bm25=0.0,
        w_recency=1.0,
        w_confidence=0.0,
        w_citation=0.0,
        superseded_penalty=0.0,
    )
    index = _index(
        tmp_path,
        {
            # BM25 우위(예산 다수)지만 오래됨 → pool=1 에서 유일 후보
            "decisions/bm25_top.md": _decision_md(
                title="결정 A",
                decision_date="2026-01-01",
                body="예산 예산 예산 재조정. [meeting:1234abcd@00:01:20]",
            ),
            # 더 최신이지만 BM25 열위 → pool 밖으로 제외
            "decisions/recent.md": _decision_md(
                title="결정 B",
                decision_date="2026-05-30",
                body="예산 재조정. [meeting:1234abcd@00:02:00]",
            ),
        },
        ranking,
    )

    results = index.search("예산", now=_NOW, top_k=1)

    # w_recency=1 이면 recent 가 1위여야 하지만, pool=1 컷오프로 BM25 1위만 후보 → bm25_top.
    assert [r.page_path for r in results] == ["decisions/bm25_top.md"]


def test_mmr_enabled면_2순위로_가장_다른_페이지를_선택한다(tmp_path: Path) -> None:
    """MMR(C1c): mmr_enabled 시 1순위 선정 후 가장 다른(다양성) 페이지를 다음으로 올린다.

    doc1·doc2 는 거의 동일 본문(near-duplicate), doc3 는 어휘가 크게 다름. 관련도(recency)
    는 doc1>doc2>doc3 이지만, λ=0.3(다양성 가중) 으로 doc1 선택 후 2순위는 near-dup(doc2)
    대신 어휘가 다른 doc3 가 선택되어야 한다.
    """
    ranking = WikiRankingConfig(
        enabled=True,
        w_bm25=0.0,
        w_recency=1.0,
        w_confidence=0.0,
        w_citation=0.0,
        superseded_penalty=0.0,
        mmr_enabled=True,
        mmr_lambda=0.3,
    )
    index = _index(
        tmp_path,
        {
            "decisions/doc1.md": _decision_md(
                title="알파 베타 감마",
                decision_date="2026-05-31",
                body="예산 알파 베타 감마 델타 합의 [meeting:1234abcd@00:01:10]",
            ),
            "decisions/doc2.md": _decision_md(
                title="알파 베타 감마",
                decision_date="2026-05-30",
                body="예산 알파 베타 감마 델타 합의 [meeting:1234abcd@00:02:20]",
            ),
            "decisions/doc3.md": _decision_md(
                title="제타 에타 세타",
                decision_date="2026-05-29",
                body="예산 제타 에타 세타 이오타 별개 [meeting:1234abcd@00:03:30]",
            ),
        },
        ranking,
    )

    results = index.search("예산", now=_NOW, top_k=3)

    paths = [r.page_path for r in results]
    assert paths[0] == "decisions/doc1.md"
    # 2순위는 near-dup(doc2) 이 아니라 어휘가 가장 다른 doc3
    assert paths[1] == "decisions/doc3.md"
    assert paths[2] == "decisions/doc2.md"
