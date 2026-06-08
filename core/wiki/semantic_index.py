"""G1 위키 벡터 스토어 — ChromaDB `wiki_pages` 컬렉션 래퍼.

임베딩(text→vector)은 호출자(오케스트레이터/인덱서)가 수행하고, 이 모듈은 벡터의
upsert/query/delete 만 담당한다(모델 비의존 → 단위 테스트 가능). ChromaDB 불가
(Python 3.13+ SIGSEGV preflight, import 실패 등)면 모든 연산이 graceful no-op:
query 는 빈 결과를 반환해 자연스럽게 BM25-only 폴백으로 이어진다(불변식 #4·#6·#7).
"""

from __future__ import annotations

import hashlib
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_HNSW_COSINE = {"hnsw:space": "cosine"}


class WikiSemanticIndex:
    """위키 페이지 벡터를 ChromaDB 별도 컬렉션에 저장/검색한다.

    transcript RAG 컬렉션과 분리된 `wiki_pages` 컬렉션을 사용한다. collection_factory
    를 주입하면(테스트) ChromaDB 없이 가짜 컬렉션으로 검증할 수 있다.
    """

    def __init__(
        self,
        chroma_dir: str | Path,
        *,
        collection_name: str = "wiki_pages",
        collection_factory: Callable[[], Any] | None = None,
    ) -> None:
        """인덱스를 초기화한다.

        Args:
            chroma_dir: ChromaDB PersistentClient 디렉토리.
            collection_name: 위키 전용 컬렉션 이름(transcript 와 분리).
            collection_factory: () -> collection|None. None 이면 실제 ChromaDB 컬렉션을
                preflight 가드와 함께 지연 생성. 테스트는 가짜 컬렉션/ None 을 주입.
        """
        self._chroma_dir = Path(chroma_dir)
        self._collection_name = collection_name
        self._collection_factory = collection_factory
        self._collection: Any = None
        self._resolved = False

    def _collection_obj(self) -> Any:
        """컬렉션 객체를 1회 해석해 캐시한다(실패 시 None=graceful)."""
        if not self._resolved:
            factory = self._collection_factory or self._default_collection
            try:
                self._collection = factory()
            except Exception as exc:  # noqa: BLE001 — 어떤 실패도 BM25-only 폴백
                logger.warning("위키 벡터 컬렉션 초기화 실패, BM25-only 폴백: %s", exc)
                self._collection = None
            self._resolved = True
        return self._collection

    def _default_collection(self) -> Any:
        """실제 ChromaDB 컬렉션을 preflight 가드와 함께 생성한다(불가 시 None)."""
        from core.preflight import run_preflight

        if not run_preflight().can_use_chromadb:
            logger.info(
                "ChromaDB 비호환(Python %d.%d) — 위키 벡터 검색 비활성, BM25-only.",
                sys.version_info.major,
                sys.version_info.minor,
            )
            return None
        import chromadb  # lazy: chromadb 가 무거우므로 필요 시에만 로드

        self._chroma_dir.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(self._chroma_dir))
        return client.get_or_create_collection(name=self._collection_name, metadata=_HNSW_COSINE)

    def upsert(
        self,
        *,
        page_paths: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> int:
        """page_path 기준 벡터를 upsert(멱등)한다. 반환: 저장 건수(불가 시 0)."""
        coll = self._collection_obj()
        if coll is None or not page_paths:
            return 0
        try:
            coll.upsert(
                ids=list(page_paths),
                embeddings=list(embeddings),
                metadatas=list(metadatas),
            )
            return len(page_paths)
        except Exception as exc:  # noqa: BLE001 — 저장 실패는 경고 후 무시(검색은 폴백)
            logger.warning("위키 벡터 upsert 실패(무시): %s", exc)
            return 0

    def query(self, embedding: list[float], top_k: int) -> list[tuple[str, int]]:
        """쿼리 벡터에 가까운 page_path 를 (page_path, 1-based rank) 로 반환한다.

        컬렉션 불가/빈/오류 시 빈 리스트 → 호출자에서 BM25-only 폴백.
        """
        coll = self._collection_obj()
        if coll is None:
            return []
        try:
            if coll.count() == 0:
                return []
            res = coll.query(
                query_embeddings=[list(embedding)],
                n_results=max(1, int(top_k)),
                include=["distances"],
            )
        except Exception as exc:  # noqa: BLE001 — 검색 실패는 폴백(graceful)
            logger.warning("위키 벡터 query 실패, BM25-only 폴백: %s", exc)
            return []
        ids = res.get("ids") or []
        first = ids[0] if ids else []
        return [(str(pid), i + 1) for i, pid in enumerate(first)]

    def delete_page(self, page_path: str | Path) -> None:
        """단일 페이지 벡터를 제거한다(불가 시 no-op)."""
        coll = self._collection_obj()
        if coll is None:
            return
        try:
            coll.delete(ids=[str(page_path)])
        except Exception as exc:  # noqa: BLE001
            logger.warning("위키 벡터 delete 실패(무시): %s", exc)

    def count(self) -> int:
        """색인된 벡터 수(불가 시 0)."""
        coll = self._collection_obj()
        if coll is None:
            return 0
        try:
            return int(coll.count())
        except Exception:  # noqa: BLE001
            return 0

    def get_indexed_hashes(self) -> dict[str, str]:
        """색인된 {page_path: content_hash} 를 반환한다(증분 색인용. 불가/없음 시 {})."""
        coll = self._collection_obj()
        if coll is None:
            return {}
        try:
            if coll.count() == 0:
                return {}
            res = coll.get(include=["metadatas"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("위키 벡터 메타 조회 실패: %s", exc)
            return {}
        ids = res.get("ids") or []
        metas = res.get("metadatas") or []
        return {
            str(ids[i]): str((metas[i] or {}).get("content_hash", ""))
            for i in range(len(ids))
        }


def _page_embed_text(page: Any) -> str:
    """페이지 임베딩 입력 텍스트. 본문(content)이 H1 제목을 포함하므로 그대로 사용."""
    return str(page.content)


def _content_hash(text: str) -> str:
    """임베딩 입력 텍스트의 안정 해시(증분 색인 변경 감지용)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _gather_store_pages(store: Any) -> dict[str, tuple[str, str]]:
    """스토어의 모든 페이지를 {page_path: (embed_text, page_type)} 로 모은다(읽기 실패는 skip)."""
    out: dict[str, tuple[str, str]] = {}
    for rel_path in store.all_pages():
        try:
            page = store.read_page(rel_path)
        except Exception as exc:  # noqa: BLE001 — 깨진 페이지 1건이 전체를 막지 않게 skip
            logger.warning("위키 벡터 색인: 페이지 읽기 skip %s (%s)", rel_path, exc)
            continue
        out[str(rel_path)] = (_page_embed_text(page), str(page.page_type.value))
    return out


def _embed_and_upsert(
    semantic_index: WikiSemanticIndex,
    pages: dict[str, tuple[str, str]],
    paths: list[str],
    embed_documents: Callable[[list[str]], list[list[float]]],
) -> int:
    """paths 의 페이지를 임베딩해 content_hash 와 함께 upsert(임베딩 실패 시 0)."""
    if not paths:
        return 0
    try:
        embeddings = embed_documents([pages[p][0] for p in paths])
    except Exception as exc:  # noqa: BLE001 — 임베딩 실패는 무시(검색은 BM25 폴백)
        logger.warning("위키 벡터 임베딩 실패(무시): %s", exc)
        return 0
    metadatas = [
        {"page_type": pages[p][1], "content_hash": _content_hash(pages[p][0])} for p in paths
    ]
    return semantic_index.upsert(page_paths=paths, embeddings=embeddings, metadatas=metadatas)


def rebuild_semantic_index(
    store: Any,
    *,
    semantic_index: WikiSemanticIndex,
    embed_documents: Callable[[list[str]], list[list[float]]],
) -> int:
    """스토어의 모든 위키 페이지를 임베딩해 벡터 스토어를 재구축한다(전체, 백필용).

    embed_documents(texts)->vectors 주입(테스트는 목, 운영은 e5). content_hash 를
    메타에 저장해 이후 reindex_incremental 이 변경분만 갱신할 수 있게 한다. 페이지
    읽기/임베딩 실패는 graceful skip → 검색은 BM25 폴백.

    Returns:
        색인된 페이지 수(불가/실패 시 0).
    """
    pages = _gather_store_pages(store)
    if not pages:
        return 0
    return _embed_and_upsert(semantic_index, pages, list(pages), embed_documents)


def reindex_incremental(
    store: Any,
    *,
    semantic_index: WikiSemanticIndex,
    embed_documents: Callable[[list[str]], list[list[float]]],
) -> dict[str, int]:
    """변경된 페이지만 재임베딩하고, 사라진 페이지의 벡터를 제거한다(일관성 유지).

    content_hash 비교로 신규/변경 페이지만 임베딩(전체 재임베딩 회피 → 발열/시간 절감).
    스토어에 없는 색인 항목(거부/이동된 페이지)은 orphan 으로 삭제해 stale 벡터(존재하지
    않는 페이지가 검색에 노출)를 막는다.

    Returns:
        {"embedded": 임베딩한 수, "deleted": orphan 삭제 수, "total": 스토어 페이지 수}.
    """
    pages = _gather_store_pages(store)
    existing = semantic_index.get_indexed_hashes()  # path -> content_hash

    changed = [p for p, (text, _t) in pages.items() if existing.get(p) != _content_hash(text)]
    orphans = [p for p in existing if p not in pages]

    embedded = _embed_and_upsert(semantic_index, pages, changed, embed_documents)
    for path in orphans:
        semantic_index.delete_page(path)
    return {"embedded": embedded, "deleted": len(orphans), "total": len(pages)}


def make_default_embed_documents(
    config: Any,
) -> Callable[[list[str]], list[list[float]]]:
    """e5-small 로 문서 배치를 임베딩하는 동기 콜백을 만든다(운영용).

    `passage:` 접두사 + NFC + normalize (steps/embedder 와 동일 규약).

    뮤텍스 주의: 쿼리 임베더(_make_default_embed_query)는 ModelLoadManager.acquire 로
    직렬화하나, 이 문서 임베더는 직접 로드한다. 정당화 — compiler._reindex_semantic 은
    파이프라인 메인 시퀀스(STT~EMBED) 완료 후 WIKI_COMPILE 단계에서 호출되어 다른 대형
    모델이 언로드된 상태이므로 동시 적재 위험이 낮다(불변식 #7, 설계문서 §8). 호출자가
    실패를 잡아 graceful 처리(rebuild_semantic_index 가 0 반환).
    """

    def _embed(texts: list[str]) -> list[list[float]]:
        import unicodedata

        from sentence_transformers import SentenceTransformer

        emb = config.embedding
        model = SentenceTransformer(emb.model_name, device=emb.device)
        prefixed = [unicodedata.normalize("NFC", f"{emb.passage_prefix}{t}") for t in texts]
        vecs = model.encode(prefixed, normalize_embeddings=True, show_progress_bar=False)
        return [[float(v) for v in row.tolist()] for row in vecs]

    return _embed
