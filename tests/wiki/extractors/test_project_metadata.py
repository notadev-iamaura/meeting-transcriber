"""프로젝트 렌더가 미제공 담당자·날짜를 메타데이터로 추가하지 못하게 한다."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.wiki.extractors.project import (
    _RENDER_SYSTEM_PROMPT,
    ExtractedProject,
    ProjectExtractor,
)
from core.wiki.store import WikiStore, _parse_frontmatter

DOCUMENT = """---
type: project
slug: sample
status: in-progress
owner: 렌더 담당자
started: 1900-01-01
target: 1900-02-01
last_updated: 2026-11-03
---
# 예시 프로젝트

## 현재 상태
확인 질문이 남아 있다. [meeting:sample@00:00:20]

## 진행 타임라인

## 미해결 이슈

<!-- confidence: 9 -->
"""


def _project(
    *, owner: str | None = None, started: date | None = None, target: date | None = None
) -> ExtractedProject:
    """구조화된 필드만 포함하는 가상 프로젝트를 만든다."""
    return ExtractedProject(
        name="예시 프로젝트",
        slug="sample",
        status="in-progress",
        owner=owner,
        started=started,
        target=target,
        description="확인 질문이 남아 있다. [meeting:sample@00:00:20]",
        confidence=9,
    )


async def _render(
    store: WikiStore, project: ExtractedProject, *, document: str = DOCUMENT
) -> tuple[dict[str, object], str]:
    """모델 대신 고정 응답을 주고 실제 렌더 후처리의 메타데이터를 읽는다."""
    llm = MagicMock(generate=AsyncMock(return_value=document))
    pages = await ProjectExtractor(llm).render_or_update_pages(
        projects=[project],
        status_transitions={},
        meeting_id="sample",
        meeting_date=date(2026, 11, 4),
        existing_store=store,
        meeting_decisions=[],
        meeting_new_actions=[],
        existing_open_actions=[],
    )
    assert len(pages) == 1
    return _parse_frontmatter(pages[0][1])


@pytest.mark.asyncio
async def test_미제공_담당자와_날짜는_렌더가_생성해도_저장후보에서_제거한다(
    tmp_path: Path,
) -> None:
    """새 프로젝트의 회의일·임의 담당자·임의 목표일을 사실 필드로 남기지 않는다."""
    metadata, body = await _render(WikiStore(tmp_path), _project())
    assert all(key not in metadata for key in ("owner", "started", "target"))
    assert metadata["last_updated"] == "2026-11-04"
    assert body == _parse_frontmatter(DOCUMENT)[1]


@pytest.mark.asyncio
async def test_명시된_담당자와_날짜는_구조화된_입력값으로_강제한다(tmp_path: Path) -> None:
    """모델이 다른 값이나 중복 필드를 써도 추출된 명시값만 한 번 반영한다."""
    project = _project(owner="명시 담당자", started=date(2026, 2, 3), target=date(2026, 8, 9))
    duplicate = DOCUMENT.replace("owner: 렌더 담당자", "owner: 첫 추정\n owner: 두 번째 추정")
    metadata, _ = await _render(WikiStore(tmp_path), project, document=duplicate)
    assert metadata["owner"] == "명시 담당자"
    assert metadata["started"] == "2026-02-03"
    assert metadata["target"] == "2026-08-09"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, "SPEAKER_01"])
async def test_현재_정보가_없으면_기존_확인된_담당자와_날짜를_보존한다(
    tmp_path: Path, owner: str | None
) -> None:
    """새로 누락된 정보가 기존 확인값을 지우거나 모델의 추정값으로 대체하지 않는다."""
    store = WikiStore(tmp_path)
    previous = (
        DOCUMENT.replace("렌더 담당자", "기존 담당자")
        .replace("1900-01-01", "2026-01-02")
        .replace("1900-02-01", "2026-12-03")
    )
    store.write_page(Path("projects/sample.md"), previous)
    metadata, _ = await _render(store, _project(owner=owner))
    assert metadata["owner"] == "기존 담당자"
    assert metadata["started"] == "2026-01-02"
    assert metadata["target"] == "2026-12-03"
    assert store.read_raw_page(Path("projects/sample.md")) == previous


@pytest.mark.asyncio
async def test_기존_착수일은_현재_추출값이_달라도_보존한다(tmp_path: Path) -> None:
    """착수 이력은 유지하면서 현재 명시된 담당자와 목표일 변경은 반영한다."""
    store = WikiStore(tmp_path)
    previous = DOCUMENT.replace("1900-01-01", "2026-01-02")
    store.write_page(Path("projects/sample.md"), previous)
    metadata, _ = await _render(
        store, _project(owner="새 담당자", started=date(2026, 5, 6), target=date(2026, 9, 10))
    )
    assert metadata["owner"] == "새 담당자"
    assert metadata["started"] == "2026-01-02"
    assert metadata["target"] == "2026-09-10"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["SPEAKER_01", "UNKNOWN", "speaker-00", "화자 2"])
async def test_익명_화자는_추출과_렌더_양쪽에서_담당자_이름이_되지_않는다(
    tmp_path: Path, owner: str
) -> None:
    """예약 화자 라벨을 거르고 기존 확인된 담당자가 없으면 owner를 생략한다."""
    metadata, _ = await _render(WikiStore(tmp_path), _project(owner=owner))
    assert "owner" not in metadata
    extractor = ProjectExtractor(MagicMock())
    extracted = extractor._build_extracted_project(
        {"name": "예시", "slug": "sample", "status": "in-progress", "owner": owner},
        meeting_id="sample",
    )
    assert extracted is not None and extracted.owner is None


@pytest.mark.asyncio
async def test_기존_익명담당자와_null_날짜도_미확인으로_처리한다(tmp_path: Path) -> None:
    """기존 단순 파서의 문자열 null이나 예약 화자 라벨을 확인값처럼 보존하지 않는다."""
    store = WikiStore(tmp_path)
    previous = (
        DOCUMENT.replace("렌더 담당자", "SPEAKER_01")
        .replace("1900-01-01", "null")
        .replace("1900-02-01", "미정")
    )
    store.write_page(Path("projects/sample.md"), previous)
    metadata, _ = await _render(store, _project())
    assert all(key not in metadata for key in ("owner", "started", "target"))
    assert store.read_raw_page(Path("projects/sample.md")) == previous


def test_렌더_프롬프트가_정보_추정과_질문을_확정으로_바꾸는_것을_금지한다() -> None:
    """담당자·날짜 생략과 질문·확답의 정확한 양쪽 인용 계약을 명시한다."""
    assert "미제공 필드는" in _RENDER_SYSTEM_PROMPT
    assert "회의 날짜를 착수일·목표일로 추정" in _RENDER_SYSTEM_PROMPT
    assert "질문·제안·조건부 발언을 합의나 확정 사실로 바꾸지 않는다" in _RENDER_SYSTEM_PROMPT
    assert "양쪽의 정확한 인용 마커를 함께 복사" in _RENDER_SYSTEM_PROMPT
