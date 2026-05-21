"""WikiCompiler decision guard dispatch tests."""

from __future__ import annotations

from pathlib import Path

from core.wiki.compiler import WikiCompilerV2
from core.wiki.guard import GuardVerdict
from core.wiki.store import WikiStore, WikiStoreError


def _content(confidence: int = 6) -> str:
    """테스트용 decision markdown."""
    return f"""---
type: decision
title: 검토 필요 결정
status: decided
confidence: {confidence}
---

# 검토 필요 결정
결정 본문 [meeting:1234abcd@00:01:20]
<!-- confidence: {confidence} -->
"""


def test_low_confidence는_pending_경로에_status_pending으로_저장된다(
    tmp_path: Path,
) -> None:
    """DW-C01: low-confidence decision은 canonical이 아니라 pending으로 격리된다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    created: list[str] = []
    updated: list[str] = []
    pending: list[str] = []
    rejected: list[tuple[str, str]] = []

    WikiCompilerV2._dispatch_page_by_verdict(
        rel_path="decisions/needs-review.md",
        content=_content(confidence=6),
        verdict=GuardVerdict(
            passed=False,
            reason="low_confidence",
            confidence=6,
            cleaned_content=_content(confidence=6),
        ),
        store=store,
        pages_created=created,
        pages_updated=updated,
        pages_pending=pending,
        pages_rejected=rejected,
    )

    page = store.read_page(Path("pending/decisions/needs-review.md"))
    assert created == []
    assert updated == []
    assert pending == ["decisions/needs-review.md"]
    assert rejected == []
    assert page.frontmatter["status"] == "pending"

    try:
        store.read_page(Path("decisions/needs-review.md"))
    except WikiStoreError as exc:
        assert exc.reason == "page_not_found"
    else:
        raise AssertionError("low-confidence decision must not be stored canonically")


def test_phantom_citation은_디스크에_저장하지_않고_rejected로_기록한다(
    tmp_path: Path,
) -> None:
    """DW-C02: phantom/rejected verdict는 어떤 decision 파일도 쓰지 않는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    created: list[str] = []
    updated: list[str] = []
    pending: list[str] = []
    rejected: list[tuple[str, str]] = []

    WikiCompilerV2._dispatch_page_by_verdict(
        rel_path="decisions/phantom.md",
        content=_content(confidence=9),
        verdict=GuardVerdict(
            passed=False,
            reason="phantom_citation",
            confidence=-1,
            rejected_citations=["[meeting:1234abcd@09:09:09]"],
            cleaned_content=_content(confidence=9),
        ),
        store=store,
        pages_created=created,
        pages_updated=updated,
        pages_pending=pending,
        pages_rejected=rejected,
    )

    assert created == []
    assert updated == []
    assert pending == []
    assert rejected == [("decisions/phantom.md", "phantom_citation")]
    for rel_path in (Path("decisions/phantom.md"), Path("pending/decisions/phantom.md")):
        try:
            store.read_page(rel_path)
        except WikiStoreError as exc:
            assert exc.reason == "page_not_found"
        else:
            raise AssertionError(f"rejected page should not exist: {rel_path}")
