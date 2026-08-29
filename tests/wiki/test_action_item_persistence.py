"""Wiki 통합 액션아이템의 누적 보존 회귀 테스트."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.wiki.citation_verifier import CheckpointCitationVerifier
from core.wiki.compiler import WikiCompilerV2
from core.wiki.extractors.action_item import ActionItemExtractor, NewActionItem
from core.wiki.guard import WikiGuard
from core.wiki.models import Citation
from core.wiki.store import WikiStore


def _compiler_with_store(store: WikiStore) -> WikiCompilerV2:
    """파서 단위 테스트용 최소 컴파일러를 만든다."""
    compiler = object.__new__(WikiCompilerV2)
    compiler._store = store
    return compiler


@pytest.mark.asyncio
async def test_existing_action_items_round_trip_preserves_open_and_closed(
    tmp_path: Path,
) -> None:
    """두 번째 컴파일용 parse/render 왕복이 기존 항목을 보존한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("action_items.md"),
        """---
type: action_items
last_compiled: 2026-08-01T02:00:00
---

# Action Items

## Open (1)

- [ ] 철수: 출시 체크리스트 정리 (due: 2026-08-31) [meeting:meeting_20260801_090000@00:01:02]

## Closed (1)

- [x] ~~회의실 예약~~ [meeting:meeting_20260731_100000@00:02:03]
  - Closed by: 영희 [meeting:meeting_20260801_090000@00:04:05]

<!-- confidence: 9 -->
""",
    )
    compiler = _compiler_with_store(store)

    existing_open, existing_closed = await compiler._parse_existing_action_items()

    assert len(existing_open) == 1
    assert existing_open[0].owner == "철수"
    assert existing_open[0].description == "출시 체크리스트 정리"
    assert existing_open[0].due_date == "2026-08-31"
    assert len(existing_closed) == 1
    assert existing_closed[0].original.description == "회의실 예약"
    assert existing_closed[0].closed_by_speaker == "영희"

    extractor = ActionItemExtractor(llm=Mock())
    rendered = await extractor.render_unified_page(
        new_open=[],
        newly_closed=[],
        existing_open=existing_open,
        existing_closed=existing_closed,
        last_compiled_at="2026-08-02T02:00:00",
    )

    assert "출시 체크리스트 정리" in rendered
    assert "회의실 예약" in rendered
    assert "## Open (1)" in rendered
    assert "## Closed (1)" in rendered


@pytest.mark.asyncio
async def test_malformed_existing_action_item_fails_closed(tmp_path: Path) -> None:
    """기존 항목을 손실 없이 읽을 수 없으면 빈 목록으로 덮어쓰지 않는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("action_items.md"),
        """---
type: action_items
---

# Action Items

## Open (1)

- [ ] 인용 없는 기존 항목

## Closed (0)

_(없음)_
""",
    )
    compiler = _compiler_with_store(store)

    with pytest.raises(ValueError, match="정확히 한 개의 인용"):
        await compiler._parse_existing_action_items()


@pytest.mark.asyncio
async def test_action_items_구조가_아니면_빈_목록으로_간주하지_않는다(tmp_path: Path) -> None:
    """체크박스가 없는 비정형 기존 문서를 빈 action page로 덮어쓰지 않는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("action_items.md"),
        """---
type: action_items
---

# 사람이 작성한 메모

다음 회의에 확인할 내용입니다.
""",
    )
    compiler = _compiler_with_store(store)

    with pytest.raises(ValueError, match="제목|헤더"):
        await compiler._parse_existing_action_items()


class _ActionExtractor:
    """실제 renderer를 쓰되 새 action만 결정적으로 공급하는 테스트 더블."""

    def __init__(self) -> None:
        self._renderer = ActionItemExtractor(llm=Mock())

    async def extract_new(
        self,
        *,
        meeting_id: str,
        meeting_date: date,
        utterances: list[object],
        speaker_name_map: dict[str, str] | None = None,
    ) -> list[NewActionItem]:
        """meeting별 실제 발화 시각을 가리키는 새 항목 하나를 만든다."""
        _ = meeting_date, utterances, speaker_name_map
        seconds = 10 if meeting_id == "meeting_one" else 20
        citation = Citation(
            meeting_id=meeting_id,
            timestamp_str=f"00:00:{seconds:02d}",
            timestamp_seconds=seconds,
        )
        return [
            NewActionItem(
                owner="철수",
                description=f"{meeting_id} 액션",
                citation=citation,
                confidence=9,
            )
        ]

    async def detect_closed(
        self,
        *,
        existing_open: list[object],
        meeting_id: str,
        utterances: list[object],
    ) -> list[object]:
        """이 회귀에서는 기존 action을 닫지 않는다."""
        _ = existing_open, meeting_id, utterances
        return []

    async def render_unified_page(self, **kwargs: object) -> str:
        """프로덕션 renderer로 open/closed 누적 본문을 만든다."""
        return await self._renderer.render_unified_page(**kwargs)  # type: ignore[arg-type]


class _NoDecisionExtractor:
    """결정 페이지를 만들지 않는 테스트 더블."""

    async def extract(self, **kwargs: object) -> list[object]:
        """빈 결정 목록을 반환한다."""
        _ = kwargs
        return []


class _NoPersonExtractor:
    """사람 페이지를 만들지 않는 테스트 더블."""

    async def extract_speakers(self, **kwargs: object) -> list[object]:
        """빈 사람 목록을 반환한다."""
        _ = kwargs
        return []


class _NoProjectExtractor:
    """프로젝트 페이지를 만들지 않는 테스트 더블."""

    async def extract_projects(self, **kwargs: object) -> list[object]:
        """빈 프로젝트 목록을 반환한다."""
        _ = kwargs
        return []

    async def detect_status_transitions(self, **kwargs: object) -> list[object]:
        """빈 상태 변경 목록을 반환한다."""
        _ = kwargs
        return []


class _NoopSearchIndex:
    """두 회의 action/guard 회귀에서 파생 검색 색인을 생략한다."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        """생성자 호환용 no-op."""

    def rebuild(self, _store: WikiStore) -> None:
        """파생 색인 작업을 하지 않는다."""


def _compile_with_checkpoint_verifier(
    *,
    store: WikiStore,
    checkpoints_dir: Path,
    meeting_id: str,
    utterances: list[dict[str, object]],
) -> WikiCompilerV2:
    """실제 D1/D2 guard를 거치는 minimal WikiCompilerV2를 생성한다."""
    verifier = CheckpointCitationVerifier(
        checkpoints_dir=checkpoints_dir,
        utterances_by_meeting={meeting_id: utterances},
    )
    return WikiCompilerV2(
        config=SimpleNamespace(wiki=SimpleNamespace(digest=None)),
        store=store,
        llm=SimpleNamespace(calls=[]),
        guard=WikiGuard(verifier=verifier, confidence_threshold=7),
        decision_extractor=_NoDecisionExtractor(),
        action_item_extractor=_ActionExtractor(),
        person_extractor=_NoPersonExtractor(),
        project_extractor=_NoProjectExtractor(),
    )


@pytest.mark.asyncio
async def test_서로_다른_두_회의_컴파일이_과거_checkpoint_citation을_검증하며_누적된다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """두 번째 컴파일은 첫 회의의 저장된 correct 원장을 D2로 재검증해야 한다."""
    monkeypatch.setattr("core.wiki.compiler.WikiSearchIndex", _NoopSearchIndex)
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    checkpoints_dir = (tmp_path / "checkpoints").resolve()
    first_utterances: list[dict[str, object]] = [
        {"speaker": "철수", "text": "첫 회의 액션", "start": 10.0, "end": 12.0}
    ]
    first_compiler = _compile_with_checkpoint_verifier(
        store=store,
        checkpoints_dir=checkpoints_dir,
        meeting_id="meeting_one",
        utterances=first_utterances,
    )

    first_result = await first_compiler.compile_meeting(
        meeting_id="meeting_one",
        meeting_date=date(2026, 8, 1),
        summary="첫 회의",
        utterances=first_utterances,
    )
    assert "action_items.md" in first_result.pages_updated

    first_checkpoint = checkpoints_dir / "meeting_one" / "correct.json"
    first_checkpoint.parent.mkdir(parents=True)
    first_checkpoint.write_text(
        json.dumps({"utterances": first_utterances}, ensure_ascii=False),
        encoding="utf-8",
    )
    second_utterances: list[dict[str, object]] = [
        {"speaker": "철수", "text": "두 번째 회의 액션", "start": 20.0, "end": 22.0}
    ]
    second_compiler = _compile_with_checkpoint_verifier(
        store=store,
        checkpoints_dir=checkpoints_dir,
        meeting_id="meeting_two",
        utterances=second_utterances,
    )

    second_result = await second_compiler.compile_meeting(
        meeting_id="meeting_two",
        meeting_date=date(2026, 8, 2),
        summary="두 번째 회의",
        utterances=second_utterances,
    )

    assert "action_items.md" in second_result.pages_updated
    assert not any(path == "action_items.md" for path, _reason in second_result.pages_rejected)
    action_items = store.read_page(Path("action_items.md")).content
    assert "meeting_one 액션" in action_items
    assert "meeting_two 액션" in action_items
