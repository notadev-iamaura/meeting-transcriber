"""WikiSearchIndex BM25/FTS5 behavior tests."""

from __future__ import annotations

from pathlib import Path

from core.wiki.search_index import WikiSearchIndex
from core.wiki.store import WikiStore


def _page(
    *,
    title: str,
    status: str = "decided",
    project: str = "Apollo",
    participants: str = "[민수, 지연]",
    owners: str = "[지연]",
    confidence: int | str = 9,
    decision_date: str = "2026-05-21",
    body: str = "Q3 출시일을 7월 15일로 확정했다. [meeting:1234abcd@00:01:20]",
) -> str:
    """검색 테스트용 canonical decision markdown."""
    return f"""---
type: decision
title: {title}
status: {status}
decision_date: {decision_date}
project: {project}
participants: {participants}
owners: {owners}
confidence: {confidence}
source_meetings: [1234abcd]
last_updated: 2026-05-21T10:00:00
---

# {title}

{body}
"""


def test_rebuild_upsert_delete가_검색_DB_상태를_반영한다(tmp_path: Path) -> None:
    """DW-D01: rebuild/upsert/delete API가 일관된 검색 결과를 만든다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("decisions/launch.md"), _page(title="Q3 출시일 확정"))
    store.write_page(
        Path("decisions/budget.md"),
        _page(title="예산 보류", status="pending", body="예산은 다음 회의에서 재논의한다."),
    )
    index = WikiSearchIndex(store.root)

    assert index.rebuild(store) >= 2
    assert [r.page_path for r in index.search("출시일")] == ["decisions/launch.md"]

    store.write_page(
        Path("decisions/budget.md"),
        _page(title="예산 확정", body="예산을 1억원으로 확정했다. [meeting:1234abcd@00:02:00]"),
    )
    index.upsert_page(store.read_page(Path("decisions/budget.md")))
    assert [r.page_path for r in index.search("예산 확정", top_k=1)] == ["decisions/budget.md"]

    index.delete_page("decisions/budget.md")
    assert index.search("1억원") == []


def test_filter_조합은_status_project_person_date_confidence를_모두_적용한다(
    tmp_path: Path,
) -> None:
    """DW-D03: decision status/date/project/person/confidence 필터 조합."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("decisions/launch.md"), _page(title="Q3 출시일 확정"))
    store.write_page(
        Path("decisions/other.md"),
        _page(
            title="Q3 출시일 보류",
            status="pending",
            project="Beta",
            participants="[영희]",
            owners="[철수]",
            confidence=5,
            decision_date="2026-04-01",
        ),
    )
    index = WikiSearchIndex(store.root)
    index.rebuild(store)

    results = index.search(
        "출시일 결정",
        page_types=["decision"],
        status="decided",
        project="Apollo",
        person="민수",
        date_from="2026-05-01",
        date_to="2026-05-31",
        min_confidence=7,
    )

    assert [r.page_path for r in results] == ["decisions/launch.md"]
    assert results[0].metadata["participants"] == ["민수", "지연"]
    assert results[0].metadata["owners"] == ["지연"]


def test_한국어_어미_prefix와_동의어_검색을_지원한다(tmp_path: Path) -> None:
    """DW-D04: FTS5 unicode61 한계를 prefix/synonym query로 보강한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("decisions/keyword.md"),
        _page(
            title="키워드매직 정책",
            body="이 위치에 키워드매직이 있습니다. [meeting:1234abcd@00:01:20]",
        ),
    )
    store.write_page(
        Path("decisions/confirm.md"),
        _page(title="가격 확정", body="가격 정책을 확정했다. [meeting:1234abcd@00:03:00]"),
    )
    index = WikiSearchIndex(store.root)
    index.rebuild(store)

    assert index.search("키워드매직")[0].page_path == "decisions/keyword.md"
    assert index.search("결정")[0].page_path == "decisions/confirm.md"


def test_malformed_confidence는_0으로_색인되고_min_confidence에서_제외된다(
    tmp_path: Path,
) -> None:
    """DW-C04: confidence가 비정상이면 0으로 안전 처리한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("decisions/bad-confidence.md"),
        _page(title="품질 정책 확정", confidence="unknown"),
    )
    index = WikiSearchIndex(store.root)
    index.rebuild(store)

    assert index.search("품질 정책", min_confidence=1) == []
    result = index.search("품질 정책", min_confidence=0)[0]
    assert result.metadata["confidence"] == 0
