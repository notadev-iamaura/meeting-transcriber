"""G1 위키 벡터 스토어 — ChromaDB `wiki_pages` 컬렉션 래퍼.

임베딩(text→vector)은 호출자(오케스트레이터/인덱서)가 수행하고, 이 모듈은 벡터의
upsert/query/delete 만 담당한다(모델 비의존 → 단위 테스트 가능). ChromaDB 불가
(Python 3.13+ SIGSEGV preflight, import 실패 등)면 모든 연산이 graceful no-op:
query 는 빈 결과를 반환해 자연스럽게 BM25-only 폴백으로 이어진다(불변식 #4·#6·#7).
"""

from __future__ import annotations

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
