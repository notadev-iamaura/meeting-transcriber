"""임시 화자 담당자를 회의별로 구분하고 원본 업무를 보존하는 현황 회귀."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from api.routers import wiki
from config import WikiDigestConfig
from core.wiki.digest import (
    ActionOwnerScope,
    build_digest,
    owner_group_label,
    parse_open_actions,
    render_digest_markdown,
)
from core.wiki.store import WikiStore

_A = "meeting_20261105_100001"
_B = "meeting_20261105_110002"


def _line(owner: str, number: int, meetings: list[str]) -> str:
    """업무 내용이나 화자 실명을 추정하지 않는 합성 항목이다."""
    markers = " ".join(f"[meeting:{mid}@00:00:10]" for mid in meetings)
    return f"- [ ] {owner}: 합성 작업 {number} (due: 2026-12-01) {markers}".rstrip()


def _store(tmp_path: Path, lines: list[str]) -> tuple[WikiStore, Path, bytes]:
    """격리된 원장에만 기록하고 파생 집계의 무수정을 검증한다."""
    store = WikiStore(tmp_path)
    path = tmp_path / "action_items.md"
    store.write_page(
        Path("action_items.md"),
        "---\ntype: action_items\n---\n## Open\n" + "\n".join(lines) + "\n\n## Closed\n",
    )
    return store, path, path.read_bytes()


def test_same_raw_owner_is_only_grouped_inside_its_single_meeting(tmp_path: Path) -> None:
    """S2 한 건과 다른 회의 S2 네 건을 합치지 않고 나머지 네 건도 누락하지 않는다."""
    lines = [_line("SPEAKER_02", 0, [_A])] + [_line("SPEAKER_02", i, [_B]) for i in range(1, 5)]
    lines += [_line("SPEAKER_03", i, [_A]) for i in range(5, 7)]
    lines += [_line("SPEAKER_04", i, [_A]) for i in range(7, 9)]
    store, path, before = _store(tmp_path, lines)
    parsed = parse_open_actions(before.decode())
    digest = build_digest(store, digest_config=WikiDigestConfig(), now=date(2026, 11, 5))
    groups = digest.open_actions_by_owner
    assert digest.total_open_actions == 9 and len(groups) == 4
    assert len(groups[ActionOwnerScope("SPEAKER_02", _A)]) == 1
    assert len(groups[ActionOwnerScope("SPEAKER_02", _B)]) == 4
    assert sorted(a.raw_line for items in groups.values() for a in items) == sorted(
        a.raw_line for a in parsed
    )
    assert sorted(
        (a.owner, a.description, a.due_date, tuple(a.citations))
        for items in groups.values()
        for a in items
    ) == sorted((a.owner, a.description, a.due_date, tuple(a.citations)) for a in parsed)
    rendered = render_digest_markdown(digest)
    assert f"SPEAKER_02 · {_A} (1)" in rendered and f"SPEAKER_02 · {_B} (4)" in rendered
    assert path.read_bytes() == before


@pytest.mark.parametrize("owner", ["SPEAKER_02", "speaker_02", "참석자 2", "화자2"])
def test_numbered_anonymous_owner_does_not_cross_meeting_boundary(
    tmp_path: Path, owner: str
) -> None:
    """표기 대소문자와 한국어 임시 번호도 회의 간 동일 인물로 연결하지 않는다."""
    store, path, before = _store(tmp_path, [_line(owner, 0, [_A]), _line(owner, 1, [_B])])
    digest = build_digest(store, digest_config=WikiDigestConfig(), now=date(2026, 11, 5))
    assert len(digest.open_actions_by_owner) == 2 and all(
        isinstance(k, ActionOwnerScope) for k in digest.open_actions_by_owner
    )
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "owner,meetings",
    [("SPEAKER_02", []), ("SPEAKER_02", [_A, _B]), ("UNKNOWN", [_A]), ("미지정", [_A])],
)
def test_ambiguous_assignment_or_unknown_person_stays_separate_per_item(
    tmp_path: Path, owner: str, meetings: list[str]
) -> None:
    """회의 미상·복수 출처·UNKNOWN·미지정을 같은 담당자로 묶지 않는다."""
    store, path, before = _store(tmp_path, [_line(owner, 0, meetings), _line(owner, 1, meetings)])
    digest = build_digest(store, digest_config=WikiDigestConfig(), now=date(2026, 11, 5))
    assert digest.total_open_actions == len(digest.open_actions_by_owner) == 2
    assert all(
        isinstance(k, ActionOwnerScope) and k.unresolved_item is not None
        for k in digest.open_actions_by_owner
    )
    assert all("확인 필요" in owner_group_label(k) for k in digest.open_actions_by_owner)
    assert path.read_bytes() == before


def test_manual_real_names_keep_existing_grouping_and_same_meeting_citations(
    tmp_path: Path,
) -> None:
    """명시한 실명은 기존 정확 일치 정책으로 묶고 같은 회의의 여러 인용은 보존한다."""
    store, path, before = _store(
        tmp_path,
        [_line("윤 담당", 0, [_A]), _line("윤 담당", 1, [_B]), _line("SPEAKER_02", 2, [_A, _A])],
    )
    digest = build_digest(store, digest_config=WikiDigestConfig(), now=date(2026, 11, 5))
    assert len(digest.open_actions_by_owner["윤 담당"]) == 2
    assert len(digest.open_actions_by_owner[ActionOwnerScope("SPEAKER_02", _A)]) == 1
    assert (
        digest.open_actions_by_owner[ActionOwnerScope("SPEAKER_02", _A)][0].citations
        == [f"[meeting:{_A}@00:00:10]"] * 2
    )
    assert path.read_bytes() == before


async def test_digest_api_exposes_context_without_replacing_raw_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 raw owner 두 그룹의 회의 범위를 API로 전달하고 원장은 보존한다."""
    store, path, before = _store(
        tmp_path,
        [
            _line("SPEAKER_02", 0, [_A]),
            _line("SPEAKER_02", 1, [_B]),
            _line("SPEAKER_02", 2, []),
            _line("윤 담당", 3, [_A]),
        ],
    )
    monkeypatch.setattr(wiki, "_resolve_wiki_root", lambda _request: store.root)
    monkeypatch.setattr(
        wiki,
        "_get_config",
        lambda _request: SimpleNamespace(wiki=SimpleNamespace(digest=WikiDigestConfig())),
    )
    response = await wiki.get_wiki_digest(Request({"type": "http"}))
    groups = response.open_actions
    assert response.total_open_actions == sum(len(g.items) for g in groups) == 4
    assert {(g.owner, g.meeting_id) for g in groups if g.identity_scope == "meeting"} == {
        ("SPEAKER_02", _A),
        ("SPEAKER_02", _B),
    }
    assert len([g for g in groups if g.identity_scope == "unresolved"]) == 1
    assert next(g for g in groups if g.owner == "윤 담당").identity_scope == "named"
    assert path.read_bytes() == before
