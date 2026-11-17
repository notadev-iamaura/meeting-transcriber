"""검토 중·거부·대체된 Wiki가 정상 답변 근거로 유입되지 않음을 검증한다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from config import AppConfig, PathsConfig, WikiConfig, WikiSemanticConfig
from core.wiki.chat_integration import HybridChatService
from core.wiki.router import RouteDecision, RouterVerdict
from core.wiki.search_index import WikiSearchIndex, WikiSearchResult
from core.wiki.semantic_search import fuse_hybrid
from core.wiki.store import WikiStore

_LIVE_CITATION = "[meeting:live_case@00:01:20]"


def _page(
    status: str, body: str, *, superseded_by: str = "", citation: str = _LIVE_CITATION
) -> str:
    """게시 상태가 다른 합성 문서를 만든다."""
    return (
        f"---\ntype: decision\ntitle: 예산 결정\nstatus: {status}\n"
        f"superseded_by: {superseded_by}\nconfidence: 9\n---\n\n"
        f"# 예산 결정\n\n{body} {citation}\n"
    )


class _Router:
    """항상 Wiki 경로를 선택하는 대역."""

    async def classify(self, query: str) -> RouterVerdict:
        """Wiki의 실제 답변 출처 선별을 검사한다."""
        return RouterVerdict(RouteDecision.WIKI, 9, "test_publication", [], False)


class _Rag:
    """게시된 근거가 없을 때만 호출돼야 하는 대역."""

    def __init__(self) -> None:
        """호출 수를 초기화한다."""
        self.calls = 0

    async def respond(self, query: str, **kwargs: Any) -> str:
        """원문 RAG에 위임됐음을 표시한다."""
        self.calls += 1
        return "원문에서 다시 확인합니다."


class _LLM:
    """금지된 문서가 프롬프트에 들어가지 않는지 검사하는 대역."""

    def __init__(self) -> None:
        """합성 입력 수집기를 준비한다."""
        self.prompts: list[str] = []

    async def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        """실제 모델 없이 답변 합성 계약을 확인한다."""
        self.prompts.append(user_prompt)
        return f"게시된 예산 결정을 확인했습니다. {_LIVE_CITATION}"


class _Semantic:
    """초안을 벡터 상위에 올리는 적대적 검색 대역."""

    def __init__(self, paths: list[str]) -> None:
        """고정된 벡터 랭킹을 준비한다."""
        self.paths = paths

    def query(self, embedding: list[float], top_k: int) -> list[tuple[str, int]]:
        """벡터 검색이 초안을 가장 관련 있다고 판단한 상황을 재현한다."""
        return [(path, i + 1) for i, path in enumerate(self.paths[:top_k])]

    def get_indexed_hashes(self) -> dict[str, str]:
        """이 fixture는 수동 편집된 페이지가 아니다."""
        return {}


@pytest.mark.parametrize("hybrid", [False, True])
@pytest.mark.parametrize("live_status", ["decided", "confirmed"])
async def test_상위초안이_많아도_게시된_현재결정만_프롬프트와_출처에_사용한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hybrid: bool, live_status: str
) -> None:
    """BM25와 실제 RRF 융합 양쪽에서 초안 top5가 게시 문서를 가리지 않는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    draft_paths = []
    for i in range(12):
        path = f"pending/decisions/draft-{i}.md"
        draft_paths.append(path)
        # metadata를 decided로 위장해도 pending 경로는 정상 답변에서 제외한다.
        store.write_page(Path(path), _page("decided", f"예산 DRAFT_ONLY_{i}"))
    store.write_page(
        Path("decisions/live.md"), _page(live_status, "예산 LIVE_ONLY " + "설명 " * 100)
    )
    index = WikiSearchIndex(store.root)
    index.rebuild(store)
    assert all(item.page_path.startswith("pending/") for item in index.search("예산", top_k=5))
    calls: list[int] = []

    async def fake_hybrid(query: str, **kwargs: Any) -> list[WikiSearchResult]:
        """실제 BM25와 벡터 RRF 경로를 합성 임베딩으로 실행한다."""
        calls.append(kwargs["top_k"])
        return fuse_hybrid(
            query,
            search_index=kwargs["search_index"],
            semantic_index=_Semantic([*draft_paths, "decisions/live.md"]),
            query_embedding=[1.0],
            semantic=kwargs["config"].wiki.semantic,
            ranking=kwargs["config"].wiki.ranking,
            top_k=kwargs["top_k"],
            page_types=kwargs["page_types"],
        )

    monkeypatch.setattr("core.wiki.semantic_search.wiki_hybrid_search", fake_hybrid)
    config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        wiki=WikiConfig(semantic=WikiSemanticConfig(enabled=hybrid)),
    )
    checkpoint = config.paths.resolved_checkpoints_dir / "live_case" / "correct.json"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(
        json.dumps(
            {
                "utterances": [
                    {"speaker": "SPEAKER_01", "text": "예산 LIVE_ONLY", "start": 80, "end": 85}
                ]
            }
        ),
        encoding="utf-8",
    )
    rag, llm = _Rag(), _LLM()
    result = await HybridChatService(
        rag, _Router(), store, llm, config if hybrid else None
    ).respond("예산")
    assert result.source_type == "wiki"
    assert [source.page_path for source in result.wiki_sources] == ["decisions/live.md"]
    assert "LIVE_ONLY" in llm.prompts[0]
    assert "DRAFT_ONLY" not in llm.prompts[0]
    assert "DRAFT_ONLY" not in (result.wiki_answer or "")
    assert rag.calls == 0
    assert not hybrid or calls == [50]


@pytest.mark.parametrize(
    "status,superseded_by",
    [
        ("pending", ""),
        ("proposed", ""),
        ("rejected", ""),
        ("superseded", ""),
        ("decided", "later-decision"),
        ("confirmed", "later-decision"),
        ("", ""),
    ],
)
async def test_게시되지_않거나_대체된_결정만_있으면_모델합성없이_원문으로_폴백한다(
    tmp_path: Path, status: str, superseded_by: str
) -> None:
    """공식 decisions 경로라도 실제 metadata가 미검증/과거 상태면 제외한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("decisions/draft.md"), _page(status, "예산 DRAFT_ONLY", superseded_by=superseded_by)
    )
    rag, llm = _Rag(), _LLM()
    result = await HybridChatService(rag, _Router(), store, llm).respond("예산")
    assert result.source_type == "rag"
    assert not result.wiki_sources
    assert llm.prompts == []
    assert rag.calls == 1


async def test_검색결과의_위조된_상태와_snippet과_citation은_최종근거가_되지_않는다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """색인 상태를 믿지 않고 읽은 실제 문서의 상태·본문·인용만 사용한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("decisions/live.md"), _page("decided", "예산 LIVE_ONLY"))
    store.write_page(Path("decisions/draft.md"), _page("pending", "예산 DRAFT_ONLY"))

    def stale_search(self: Any, query: str, **kwargs: Any) -> list[WikiSearchResult]:
        """모든 색인 필드가 게시 상태를 잘못 주장하는 결과를 반환한다."""
        return [
            WikiSearchResult(
                path,
                "decision",
                "DRAFT_ONLY_INDEX_TITLE",
                "DRAFT_ONLY_INDEX_SNIPPET",
                10.0,
                ["[meeting:forged_index@00:00:00]"],
                {"status": "decided"},
            )
            for path in ["decisions/draft.md", "decisions/live.md"]
        ]

    monkeypatch.setattr(WikiSearchIndex, "search", stale_search)
    rag, llm = _Rag(), _LLM()
    result = await HybridChatService(rag, _Router(), store, llm).respond("예산")
    assert [source.page_path for source in result.wiki_sources] == ["decisions/live.md"]
    assert result.wiki_sources[0].citations == [_LIVE_CITATION]
    assert "DRAFT_ONLY" not in llm.prompts[0]
    assert "DRAFT_ONLY" not in result.wiki_sources[0].snippet
    assert "forged_index" not in (result.wiki_answer or "")


def _force_candidates(monkeypatch: pytest.MonkeyPatch, paths: list[str]) -> None:
    """주제와 무관한 후보도 반환하는 검색기의 최종 출처 경계를 검사한다."""

    def search(self: Any, query: str, **kwargs: Any) -> list[WikiSearchResult]:
        """색인의 제목·발췌는 주제와 일치한다고 잘못 주장한다."""
        return [
            WikiSearchResult(
                path,
                "decision",
                "기술감사 프로젝트 AUDIT",
                "기술감사 프로젝트 AUDIT",
                10.0,
                [_LIVE_CITATION],
                {"status": "decided"},
            )
            for path in paths
        ]

    monkeypatch.setattr(WikiSearchIndex, "search", search)


@pytest.mark.parametrize(
    "quoted_topic",
    ["'기술감사 프로젝트'", '"기술감사 프로젝트"', "‘기술감사 프로젝트’", "“기술감사 프로젝트”"],
)
async def test_따옴표의_주제와_무관한_공개결정은_합성하지_않는다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quoted_topic: str
) -> None:
    """일반 결정 의도어나 잘못된 색인 발췌만으로 다른 결정을 답하지 않는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("decisions/live.md"), _page("decided", "최근 예산은 20만 원으로 확정"))
    _force_candidates(monkeypatch, ["decisions/live.md"])
    rag, llm = _Rag(), _LLM()
    result = await HybridChatService(rag, _Router(), store, llm).respond(
        f"최근 {quoted_topic} 관련 결정은 무엇인가요?"
    )
    assert result.source_type == "rag"
    assert result.wiki_sources == []
    assert llm.prompts == []
    assert rag.calls == 1
    assert result.error_message == "wiki_synthesis_failed: wiki_no_readable_search_results"


async def test_따옴표의_주제를_서로_다른_결정에서_조합하지_않는다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """각 후보가 주제의 일부만 담고 있으면 결합해 관련 문서로 만들지 않는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("decisions/one.md"), _page("decided", "기술감사 예산 확정"))
    store.write_page(Path("decisions/two.md"), _page("decided", "별도 프로젝트의 예산 확정"))
    _force_candidates(monkeypatch, ["decisions/one.md", "decisions/two.md"])
    rag, llm = _Rag(), _LLM()
    result = await HybridChatService(rag, _Router(), store, llm).respond(
        "'기술감사 프로젝트'의 결정은 무엇인가요?"
    )
    assert result.source_type == "rag"
    assert llm.prompts == []
    assert rag.calls == 1


async def test_숨김_주석과_인용_식별자는_주제의_본문_근거가_아니다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """숨김 필드나 출처 ID에만 있는 단어로 무관한 본문을 허용하지 않는다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(
        Path("decisions/live.md"),
        _page(
            "decided", "예산 확정 <!-- 기술감사 프로젝트 -->", citation="[meeting:AUDIT@00:01:20]"
        ),
    )
    _force_candidates(monkeypatch, ["decisions/live.md"])
    rag, llm = _Rag(), _LLM()
    service = HybridChatService(rag, _Router(), store, llm)
    for query in ("'기술감사 프로젝트' 결정은?", "'AUDIT' 결정은?"):
        result = await service.respond(query)
        assert result.source_type == "rag"
        assert result.wiki_sources == []
    assert llm.prompts == []
    assert rag.calls == 2


@pytest.mark.parametrize(
    "body,expected", [("보안 AUDIT 진행을 확정", "wiki"), ("auditor 채용 확정", "rag")]
)
async def test_명시한_영문_주제는_표기를_정규화하되_다른_단어의_일부로_매칭하지_않는다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, expected: str
) -> None:
    """전각·대소문자 차이는 허용하고 audit와 auditor는 구분한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("decisions/live.md"), _page("decided", body))
    _force_candidates(monkeypatch, ["decisions/live.md"])
    rag, llm = _Rag(), _LLM()
    result = await HybridChatService(rag, _Router(), store, llm).respond(
        "'ａｕｄｉｔ' 관련 결정은?"
    )
    assert result.source_type == expected
    assert len(llm.prompts) == int(expected == "wiki")
    assert rag.calls == int(expected == "rag")


async def test_따옴표_없는_질문의_의미검색_후보는_새_문자일치_제한을_받지_않는다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """명시적인 주제가 없는 바꿔 말한 질문의 기존 검색 동작을 유지한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("decisions/live.md"), _page("decided", "개발비는 20만 원"))
    _force_candidates(monkeypatch, ["decisions/live.md"])
    rag, llm = _Rag(), _LLM()
    result = await HybridChatService(rag, _Router(), store, llm).respond("지출 한도를 알려 주세요")
    assert result.source_type == "wiki"
    assert len(llm.prompts) == 1
    assert rag.calls == 0


async def test_따옴표로_여러_주제를_비교할_때_문서별로_모든_주제를_요구하지_않는다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """독립된 두 주제의 문서를 함께 검색하는 기존 비교 질문은 보존한다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("decisions/one.md"), _page("decided", "기술감사 예산 확정"))
    store.write_page(Path("decisions/two.md"), _page("decided", "별도 프로젝트의 예산 확정"))
    _force_candidates(monkeypatch, ["decisions/one.md", "decisions/two.md"])
    rag, llm = _Rag(), _LLM()
    result = await HybridChatService(rag, _Router(), store, llm).respond(
        "'기술감사'와 '프로젝트'의 결정을 비교해 주세요"
    )
    assert result.source_type == "wiki"
    assert [source.page_path for source in result.wiki_sources] == [
        "decisions/one.md",
        "decisions/two.md",
    ]
    assert len(llm.prompts) == 1
    assert rag.calls == 0


async def test_주제_단어의_순서가_달라도_뒤의_관련_게시문서를_선택한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """길이순으로 조합한 실제 검증 질문도 같은 제목의 문서를 찾을 수 있다."""
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    store.write_page(Path("decisions/irrelevant.md"), _page("decided", "최근 결정 내용 공유"))
    store.write_page(
        Path("decisions/relevant.md"),
        _page("decided", "향후 오류 제보 시 전체 화면 캡처를 요청한다"),
    )
    _force_candidates(monkeypatch, ["decisions/irrelevant.md", "decisions/relevant.md"])
    rag, llm = _Rag(), _LLM()
    result = await HybridChatService(rag, _Router(), store, llm).respond(
        "'캡처 오류 화면' 관련 결정은 무엇인가요?"
    )
    assert result.source_type == "wiki"
    assert [source.page_path for source in result.wiki_sources] == ["decisions/relevant.md"]
    assert "최근 결정 내용 공유" not in llm.prompts[0]
    assert len(llm.prompts) == 1
    assert rag.calls == 0
