"""G1 호출자 wiring 단위 테스트 (compiler 자동색인 + chat 하이브리드 분기).

핵심 검색 로직(test_semantic_*)·실 e5 e2e(test_semantic_real_e5)와 별도로, "연결부"
가 올바른 조건에서 호출/스킵되는지를 가짜 임베더·스파이로 검증한다(실 e5 없이).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from config import AppConfig, PathsConfig, WikiConfig, WikiSemanticConfig
from core.wiki.compiler import WikiCompilerV2
from core.wiki.router import RouteDecision, RouterVerdict
from core.wiki.search_index import WikiSearchResult
from core.wiki.store import WikiStore


def _md(title: str, body: str) -> str:
    return f"""---
type: decision
title: {title}
status: decided
decision_date: 2026-05-21
project: Apollo
participants: [민수]
owners: [민수]
confidence: 9
source_meetings: [1234abcd]
last_updated: 2026-05-21T10:00:00
---

# {title}

{body}
"""


def _store_with_pages(tmp_path: Path) -> WikiStore:
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("decisions/a.md"), _md("결정 A", "예산 합의 [meeting:1234abcd@00:01:20]")
    )
    store.write_page(
        Path("decisions/b.md"), _md("결정 B", "일정 합의 [meeting:1234abcd@00:02:00]")
    )
    return store


def _app_config(tmp_path: Path, *, semantic_enabled: bool) -> AppConfig:
    """tmp base_dir(=tmp chroma) + semantic on/off 인 AppConfig."""
    return AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        wiki=WikiConfig(semantic=WikiSemanticConfig(enabled=semantic_enabled)),
    )


def _compiler(
    tmp_path: Path,
    *,
    embedder: Any,
    semantic_enabled: bool = True,
) -> WikiCompilerV2:
    """_reindex_semantic 검증용 최소 컴파일러(나머지 의존성은 mock)."""
    return WikiCompilerV2(
        config=_app_config(tmp_path, semantic_enabled=semantic_enabled),
        store=_store_with_pages(tmp_path),
        llm=MagicMock(),
        guard=MagicMock(),
        decision_extractor=MagicMock(),
        action_item_extractor=MagicMock(),
        person_extractor=MagicMock(),
        project_extractor=MagicMock(),
        semantic_doc_embedder=embedder,
    )


# ── compiler 자동 벡터 색인 wiring ──────────────────────────────────────────


async def test_compiler_임베더_주입_semantic_활성시_페이지를_임베딩한다(tmp_path: Path) -> None:
    """semantic_doc_embedder 주입 + semantic.enabled → _reindex_semantic 이 전체 페이지 임베딩."""
    calls: list[list[str]] = []

    def _fake_embed(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [[1.0, 0.0] for _ in texts]

    compiler = _compiler(tmp_path, embedder=_fake_embed, semantic_enabled=True)
    await compiler._reindex_semantic()

    assert len(calls) == 1
    assert len(calls[0]) == 2  # 두 페이지 임베딩
    # 실제 색인 발생 → tmp chroma 디렉토리 생성됨(실 ~/.meeting-transcriber 아님)
    assert (tmp_path / "chroma_db").exists()


async def test_compiler_임베더_미주입시_색인_skip(tmp_path: Path) -> None:
    """semantic_doc_embedder 미주입(테스트 기본) → 색인 스킵, e5/chroma 미접근."""
    compiler = _compiler(tmp_path, embedder=None, semantic_enabled=True)
    await compiler._reindex_semantic()

    assert not (tmp_path / "chroma_db").exists()


async def test_compiler_semantic_비활성시_색인_skip(tmp_path: Path) -> None:
    """semantic.enabled=False → 임베더가 주입돼도 색인 스킵(임베더 미호출)."""
    calls: list[list[str]] = []

    def _fake_embed(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [[1.0, 0.0] for _ in texts]

    compiler = _compiler(tmp_path, embedder=_fake_embed, semantic_enabled=False)
    await compiler._reindex_semantic()

    assert calls == []
    assert not (tmp_path / "chroma_db").exists()


# ── chat 하이브리드 분기 wiring ─────────────────────────────────────────────


class _Router:
    """classify() 만 흉내내는 가짜 라우터(항상 WIKI 결정)."""

    def __init__(self) -> None:
        self.verdict = RouterVerdict(
            decision=RouteDecision.WIKI,
            confidence=9,
            reason="test_wiki",
            matched_signals=[],
            used_llm=False,
        )

    async def classify(self, query: str) -> RouterVerdict:
        return self.verdict


class _Chat:
    """respond() 만 흉내내는 가짜 RAG 챗(WIKI 분기에선 미호출 기대)."""

    def __init__(self) -> None:
        self.calls = 0

    async def respond(self, query: str, **kwargs: Any) -> Any:
        self.calls += 1
        return None


def _chat_service(tmp_path: Path, *, config: AppConfig | None):
    from core.wiki.chat_integration import HybridChatService

    return HybridChatService(
        chat_service=_Chat(),
        router=_Router(),
        wiki_store=_store_with_pages(tmp_path),
        config=config,
    )


async def test_chat_config주입_semantic활성시_하이브리드_검색을_쓴다(
    tmp_path: Path, monkeypatch
) -> None:
    """config 주입 + semantic.enabled → 위키-챗이 wiki_hybrid_search 로 분기한다."""
    called: list[dict[str, Any]] = []

    async def _spy(query: str, **kwargs: Any) -> list[WikiSearchResult]:
        called.append(kwargs)
        return [
            WikiSearchResult(
                page_path="decisions/a.md",
                page_type="decision",
                title="결정 A",
                snippet="예산 합의",
                score=1.0,
                citations=["[meeting:1234abcd@00:01:20]"],
                metadata={},
            )
        ]

    monkeypatch.setattr("core.wiki.semantic_search.wiki_hybrid_search", _spy)

    service = _chat_service(tmp_path, config=_app_config(tmp_path, semantic_enabled=True))
    result = await service.respond("예산 결정 알려줘")

    assert len(called) == 1  # 하이브리드 검색 호출됨
    assert result.source_type == "wiki"


async def test_chat_config미주입시_BM25를_쓴다(tmp_path: Path, monkeypatch) -> None:
    """config 미주입 → wiki_hybrid_search 미호출(BM25 index.search 경로)."""
    called: list[Any] = []

    async def _spy(query: str, **kwargs: Any) -> list[WikiSearchResult]:
        called.append(kwargs)
        return []

    monkeypatch.setattr("core.wiki.semantic_search.wiki_hybrid_search", _spy)

    service = _chat_service(tmp_path, config=None)
    result = await service.respond("예산")  # 페이지 본문에 매칭 → BM25 결과 존재

    assert called == []  # 하이브리드 미호출
    assert result.source_type == "wiki"  # BM25 로도 WIKI 답변 생성
