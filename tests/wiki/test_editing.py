"""Wiki 수동 편집의 충돌·보존·이력·검색 일관성을 검증한다."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers.wiki import router
from config import AppConfig, PathsConfig, WikiConfig, WikiRankingConfig, WikiSemanticConfig
from core.wiki.editing import WikiEditor, editable_wiki_content, page_revision, related_pages
from core.wiki.search_index import WikiSearchIndex
from core.wiki.semantic_search import fuse_hybrid
from core.wiki.store import WikiStore, WikiStoreError

_PATH = Path("decisions/budget.md")
_RAW = """---
type: decision
title: 예산 검토
status: pending
project: Apollo
custom_field: keep-me
---

# 예산 검토
예산은 미정이다. [meeting:meeting_a@00:01:00]
"""


@pytest.mark.asyncio
async def test_edit_hides_and_preserves_identity_for_unchanged_action_only(tmp_path: Path) -> None:
    """본문 수정 화면은 식별 정보를 숨기며 바뀌지 않은 업무의 정보만 보존한다."""
    from api.routers.wiki import _load_wiki_detail
    from core.wiki.extractors.action_item import ActionItemExtractor, NewActionItem
    from core.wiki.llm_client import MockWikiLLMClient
    from core.wiki.models import Citation

    raw = await ActionItemExtractor(MockWikiLLMClient()).render_unified_page(
        new_open=[
            NewActionItem(
                owner="SPEAKER_01",
                description="출시 점검",
                citation=Citation("demo", "00:01:00", 60),
                due_date="2026-11-05",
                project_slug="launch",
                confidence=9,
            )
        ],
        newly_closed=[],
        existing_open=[],
        existing_closed=[],
        last_compiled_at="2026-11-04T00:00:00",
    )
    store = WikiStore(tmp_path / "wiki")
    rel = Path("action_items.md")
    store.write_page(rel, raw)
    detail = _load_wiki_detail(store.root, rel, "action_items")
    assert "action-identity-v1" in detail.content
    assert "action-identity-v1" not in detail.editable_content
    assert "출시 점검" in detail.editable_content
    editor = WikiEditor(store)
    revision = editor.save(
        rel,
        content=detail.editable_content.replace("# Action Items", "# Action Items\n\n사용자 메모"),
        expected_revision=detail.revision,
    )
    preserved = store.read_page(rel).content
    assert preserved.count("action-identity-v1:") == 1
    changed = editable_wiki_content(preserved).replace("출시 점검", "출시 점검 및 결과 공유")
    editor.save(rel, content=changed, expected_revision=revision)
    assert "action-identity-v1:" not in store.read_page(rel).content
    assert "사용자 메모" in store.read_page(rel).content


def _seed(tmp_path: Path) -> WikiStore:
    """Git·모델 초기화 없이 격리 Wiki를 만든다."""
    store = WikiStore(tmp_path / "wiki")
    store.write_page(_PATH, _RAW)
    return store


def _changed_body(store: WikiStore) -> str:
    """인용을 유지하고 본문만 수정한다."""
    return store.read_page(_PATH).content.replace(
        "예산은 미정이다", "예산은 다음 회의에서 다시 검토한다"
    )


def test_edit_preserves_metadata_original_and_blocks_automatic_overwrite(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    original_revision = page_revision(_RAW)
    changed = WikiEditor(store).save(
        _PATH, content=_changed_body(store), expected_revision=original_revision, reviewed=True
    )
    page = store.read_page(_PATH)
    assert changed != original_revision
    assert page.frontmatter["status"] == "pending"  # 확인과 승격은 다른 행위
    assert page.frontmatter["custom_field"] == "keep-me"
    assert page.frontmatter["human_reviewed"] == "true"
    with pytest.raises(WikiStoreError, match="human_protected"):
        WikiStore(store.root).write_page(_PATH, _RAW)
    revisions = WikiEditor(store).history(_PATH)
    assert len(revisions) == 2
    assert {item["revision"] for item in revisions} == {original_revision, changed}
    assert any(item["content"] == _RAW.split("---\n", 2)[2] for item in revisions)


def test_stale_revision_and_citation_change_preserve_current_file(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    editor = WikiEditor(store)
    with pytest.raises(WikiStoreError, match="revision_conflict"):
        editor.save(_PATH, content=_changed_body(store), expected_revision="0" * 64)
    with pytest.raises(WikiStoreError, match="citation_changed"):
        editor.save(_PATH, content="# 출처 삭제", expected_revision=page_revision(_RAW))
    assert store.read_raw_page(_PATH) == _RAW


def test_restore_creates_new_manual_revision_and_preserves_both_versions(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    editor = WikiEditor(store)
    revision = editor.save(
        _PATH, content=_changed_body(store), expected_revision=page_revision(_RAW), reviewed=True
    )
    restored = editor.restore(_PATH, revision=page_revision(_RAW), expected_revision=revision)
    assert store.read_page(_PATH).content == _RAW.split("---\n", 2)[2]
    assert store.read_page(_PATH).frontmatter["human_reviewed"] == "false"
    assert restored not in {revision, page_revision(_RAW)}
    assert len(editor.history(_PATH)) == 3


def test_corrupt_history_is_not_offered_or_restored(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    editor = WikiEditor(store)
    current = editor.save(
        _PATH, content=_changed_body(store), expected_revision=page_revision(_RAW)
    )
    snapshot = next((store.root / ".history").glob("*/*.json"))
    data = json.loads(snapshot.read_text())
    data["raw"] = "tampered"
    snapshot.write_text(json.dumps(data))
    assert len(editor.history(_PATH)) == 1
    with pytest.raises(WikiStoreError, match="version_not_found"):
        editor.restore(_PATH, revision=page_revision(_RAW), expected_revision=current)
    assert page_revision(store.read_raw_page(_PATH)) == current


def test_competing_editors_only_one_can_publish_same_revision(tmp_path: Path) -> None:
    store = _seed(tmp_path)

    def save(label: str) -> str:
        """별도 Store 인스턴스의 동일 버전 수정을 시도한다."""
        try:
            return WikiEditor(WikiStore(store.root)).save(
                _PATH,
                content=f"{label} [meeting:meeting_a@00:01:00]",
                expected_revision=page_revision(_RAW),
            )
        except WikiStoreError as exc:
            return exc.reason

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["첫 수정", "둘째 수정"]))
    assert results.count("revision_conflict") == 1
    assert len(WikiEditor(store).history(_PATH)) == 2


def test_symlink_source_and_history_never_overwrite_outside(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("외부 데이터")
    (store.root / "decisions" / "evil.md").symlink_to(outside)
    with pytest.raises(WikiStoreError):
        store.read_page(Path("decisions/evil.md"))
    with pytest.raises(WikiStoreError):
        store.write_page(Path("decisions/evil.md"), "덮어쓰기")
    (store.root / ".history").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WikiStoreError):
        WikiEditor(store).save(
            _PATH, content=_changed_body(store), expected_revision=page_revision(_RAW)
        )
    assert outside.read_text() == "외부 데이터"
    assert store.read_raw_page(_PATH) == _RAW


def test_related_pages_find_backlinks_project_and_ignore_external_links(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    store.write_page(Path("projects/apollo.md"), "---\nproject: Apollo\n---\n# Apollo\n")
    store.write_page(
        Path("topics/review.md"),
        "# 검토\n[예산](../decisions/budget.md)\n[외부](https://example.com)\n",
    )
    store.write_page(Path("topics/other.md"), "# 기타\n[탈출](../../outside.md)\n")
    found = {item["path"]: item["relation"] for item in related_pages(store, _PATH)}
    assert found == {"projects/apollo.md": "project", "topics/review.md": "backlink"}


def test_related_project_and_person_pages_use_real_metadata_shape(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    store.write_page(
        Path("projects/apollo.md"), "---\ntype: project\nslug: apollo\n---\n# Apollo\n"
    )
    store.write_page(Path("people/민수.md"), "---\ntype: person\nname: 민수\n---\n# 민수\n")
    store.write_page(Path("decisions/owner.md"), "---\nparticipants: [민수]\n---\n# 담당자\n")
    assert any(item["path"] == "projects/apollo.md" for item in related_pages(store, _PATH))
    assert any(
        item["path"] == "people/민수.md" and item["relation"] == "person"
        for item in related_pages(store, Path("decisions/owner.md"))
    )


def _client(store: WikiStore, tmp_path: Path) -> TestClient:
    """실제 Wiki 라우터만 연결한 로컬 클라이언트를 만든다."""
    app = FastAPI()
    app.state.config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)), wiki=WikiConfig(enabled=True, root=store.root)
    )
    app.include_router(router, prefix="/api")
    return TestClient(app, base_url="http://127.0.0.1")


def test_api_edit_roundtrip_conflict_review_history_and_keyword_search(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    client = _client(store, tmp_path)
    url = "/api/wiki/pages/decisions/budget"
    detail = client.get(url).json()
    assert detail["revision"] == page_revision(_RAW)
    response = client.put(
        url,
        json={
            "content": detail["content"].replace("미정이다", "재논의한다"),
            "expected_revision": detail["revision"],
            "reviewed": True,
        },
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["saved"] and updated["human_reviewed"]
    assert updated["index_status"] == "keyword_ready_semantic_pending"
    assert len(WikiSearchIndex(store.root).search("재논의")) == 1
    assert (
        client.put(
            url, json={"content": detail["content"], "expected_revision": detail["revision"]}
        ).status_code
        == 409
    )
    history = client.get("/api/wiki/history/decisions/budget").json()["revisions"]
    assert len(history) == 2
    restored = client.post(
        "/api/wiki/restore/decisions/budget",
        json={"revision": detail["revision"], "expected_revision": updated["revision"]},
    )
    assert restored.status_code == 200
    assert "미정이다" in restored.json()["content"]


def test_api_rejects_external_origin_and_stale_index_failure_still_reports_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _seed(tmp_path)
    client = _client(store, tmp_path)
    body = {"content": _changed_body(store), "expected_revision": page_revision(_RAW)}
    response = client.put(
        "/api/wiki/pages/decisions/budget", headers={"origin": "https://example.com"}, json=body
    )
    assert response.status_code == 403

    def fail_index(*_args: object, **_kwargs: object) -> None:
        """본문 게시 뒤 별개 검색 DB 실패를 모사한다."""
        raise OSError("read-only index")

    monkeypatch.setattr(WikiSearchIndex, "upsert_page", fail_index)
    response = client.put("/api/wiki/pages/decisions/budget", json=body)
    assert response.status_code == 200
    assert response.json()["saved"]
    assert response.json()["index_status"] == "keyword_rebuild_needed"


def test_stale_manual_vector_excluded_until_current_body_reembedded(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    old_hash = hashlib.sha256(store.read_page(_PATH).content.encode()).hexdigest()
    WikiEditor(store).save(
        _PATH, content=_changed_body(store), expected_revision=page_revision(_RAW)
    )
    index = WikiSearchIndex(store.root)
    index.rebuild(store)
    hashes = {str(_PATH): old_hash}
    vector = SimpleNamespace(
        query=lambda *_args: [(str(_PATH), 1)], get_indexed_hashes=lambda: hashes
    )
    kwargs = {
        "search_index": index,
        "semantic_index": vector,
        "query_embedding": [0.0],
        "semantic": WikiSemanticConfig(),
        "ranking": WikiRankingConfig(),
    }
    assert fuse_hybrid("없는단어", **kwargs) == []
    hashes[str(_PATH)] = hashlib.sha256(store.read_page(_PATH).content.encode()).hexdigest()
    assert [r.page_path for r in fuse_hybrid("없는단어", **kwargs)] == [str(_PATH)]
