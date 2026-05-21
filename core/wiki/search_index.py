"""Wiki BM25/FTS5 search index.

Decision Wiki 검색은 transcript RAG 와 분리된 인덱스를 사용한다. 이 모듈은
위키 페이지를 SQLite FTS5 로 색인하고 BM25 점수로 page/decision 검색을 제공한다.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.wiki.models import WikiPage
from core.wiki.store import WikiStore, WikiStoreError

logger = logging.getLogger(__name__)

_FTS_TABLE = "wiki_fts"
_META_TABLE = "wiki_page_meta"
_DEFAULT_DB_NAME = "wiki_search.db"

_SYNONYMS: dict[str, tuple[str, ...]] = {
    "결정": ("확정", "합의", "결론", "결정사항"),
    "결정사항": ("결정", "확정", "합의", "결론"),
    "액션": ("액션아이템", "할일", "할 일", "담당", "TODO", "todo"),
    "일정": ("마감", "데드라인", "출시일"),
    "보류": ("미정", "재논의", "pending"),
}


@dataclass(frozen=True)
class WikiSearchResult:
    """Wiki 검색 결과."""

    page_path: str
    page_type: str
    title: str | None
    snippet: str
    score: float
    citations: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON 직렬화 가능한 dict 로 변환한다."""
        return {
            "page_path": self.page_path,
            "page_type": self.page_type,
            "title": self.title,
            "snippet": self.snippet,
            "score": self.score,
            "citations": list(self.citations),
            "metadata": dict(self.metadata),
        }


def _index_db_path(wiki_root: Path) -> Path:
    """wiki root 하위의 기본 검색 DB 경로를 반환한다."""
    return wiki_root / ".index" / _DEFAULT_DB_NAME


def _connect(db_path: Path) -> sqlite3.Connection:
    """SQLite 연결을 생성하고 row_factory/WAL 을 설정한다."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Wiki 검색 테이블을 생성한다."""
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE}
        USING fts5(
            page_path,
            page_type,
            title,
            body,
            project,
            participants,
            owners,
            status,
            citations,
            tokenize='unicode61'
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {_META_TABLE} (
            page_path TEXT PRIMARY KEY,
            rowid INTEGER NOT NULL,
            page_type TEXT NOT NULL,
            title TEXT,
            status TEXT,
            project TEXT,
            decision_date TEXT,
            confidence INTEGER,
            participants TEXT,
            owners TEXT,
            source_meetings TEXT,
            citations TEXT,
            last_updated TEXT
        )
    """)
    conn.commit()


def _string_list(value: Any) -> list[str]:
    """frontmatter scalar/list 값을 문자열 리스트로 정규화한다."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[meeting:"):
        return [text]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [item.strip() for item in inner.split(",") if item.strip()]
    if "," in text:
        return [item.strip() for item in text.split(",") if item.strip()]
    return [text]


def _title_from_page(page: WikiPage) -> str | None:
    """frontmatter title 또는 첫 H1 에서 제목을 추출한다."""
    title = page.frontmatter.get("title") if page.frontmatter else None
    if title:
        return str(title)
    for line in page.content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return stripped[2:].strip()
    return None


def _citation_strings(page: WikiPage) -> list[str]:
    """WikiPage citations 를 raw marker 문자열로 변환한다."""
    return [f"[meeting:{c.meeting_id}@{c.timestamp_str}]" for c in page.citations]


def _build_fts_query(query: str) -> str:
    """사용자 쿼리를 FTS5 MATCH 문자열로 변환한다."""
    safe_chars: list[str] = []
    for ch in query:
        if ch.isalnum() or ch == " " or ord(ch) > 127:
            safe_chars.append(ch)
        else:
            safe_chars.append(" ")
    words = [w for w in "".join(safe_chars).split() if w]
    expanded: list[str] = []
    for word in words:
        expanded.append(word)
        if len(word) >= 2:
            expanded.append(f"{word}*")
        expanded.extend(_SYNONYMS.get(word, ()))
    # stable unique
    seen: set[str] = set()
    unique = []
    for word in expanded:
        if word in seen:
            continue
        seen.add(word)
        unique.append(word)
    return " OR ".join(unique)


def _snippet(content: str, query: str, max_chars: int = 180) -> str:
    """검색 결과에 보여줄 짧은 snippet 을 만든다."""
    stripped = " ".join(line.strip() for line in content.splitlines() if line.strip())
    if not stripped:
        return ""
    terms = [term for term in query.split() if term]
    lower = stripped.lower()
    pos = -1
    for term in terms:
        pos = lower.find(term.lower())
        if pos >= 0:
            break
    if pos < 0:
        return stripped[: max_chars - 1].rstrip() + ("…" if len(stripped) > max_chars else "")
    start = max(0, pos - 60)
    end = min(len(stripped), start + max_chars)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(stripped) else ""
    return f"{prefix}{stripped[start:end].strip()}{suffix}"


class WikiSearchIndex:
    """WikiStore 페이지를 SQLite FTS5/BM25 로 색인하고 검색한다."""

    def __init__(self, wiki_root: Path, db_path: Path | None = None) -> None:
        """인덱스를 초기화한다."""
        self._wiki_root = wiki_root
        self._db_path = db_path or _index_db_path(wiki_root)

    @property
    def db_path(self) -> Path:
        """검색 DB 경로."""
        return self._db_path

    def rebuild(self, store: WikiStore | None = None) -> int:
        """전체 Wiki 페이지 인덱스를 재구축한다."""
        store = store or WikiStore(self._wiki_root)
        with _connect(self._db_path) as conn:
            _ensure_schema(conn)
            conn.execute(f"DELETE FROM {_FTS_TABLE}")
            conn.execute(f"DELETE FROM {_META_TABLE}")
            count = 0
            for rel_path in store.all_pages():
                try:
                    page = store.read_page(rel_path)
                except WikiStoreError as exc:
                    logger.warning("WikiSearchIndex rebuild: read skip %s (%s)", rel_path, exc)
                    continue
                self._upsert_page_conn(conn, page)
                count += 1
            conn.commit()
            return count

    def upsert_page(self, page: WikiPage) -> None:
        """단일 페이지를 색인한다."""
        with _connect(self._db_path) as conn:
            _ensure_schema(conn)
            self._upsert_page_conn(conn, page)
            conn.commit()

    def delete_page(self, rel_path: str | Path) -> None:
        """단일 페이지를 인덱스에서 제거한다."""
        page_path = str(rel_path)
        with _connect(self._db_path) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                f"SELECT rowid FROM {_META_TABLE} WHERE page_path = ?",
                (page_path,),
            ).fetchone()
            if row is not None:
                conn.execute(f"DELETE FROM {_FTS_TABLE} WHERE rowid = ?", (int(row["rowid"]),))
            conn.execute(f"DELETE FROM {_META_TABLE} WHERE page_path = ?", (page_path,))
            conn.commit()

    def search(
        self,
        query: str,
        *,
        page_types: list[str] | None = None,
        status: str | None = None,
        project: str | None = None,
        participant: str | None = None,
        owner: str | None = None,
        person: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        min_confidence: int | None = None,
        top_k: int = 20,
    ) -> list[WikiSearchResult]:
        """BM25 기반 Wiki 검색을 수행한다."""
        query = query.strip()
        if not query:
            return []
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []

        with _connect(self._db_path) as conn:
            _ensure_schema(conn)
            sql = f"""
                SELECT
                    f.rowid AS fts_rowid,
                    f.page_path,
                    f.page_type,
                    f.title,
                    f.body,
                    bm25({_FTS_TABLE}, 4.0, 1.0, 2.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
                        AS rank_score,
                    m.status,
                    m.project,
                    m.decision_date,
                    m.confidence,
                    m.participants,
                    m.owners,
                    m.source_meetings,
                    m.citations,
                    m.last_updated
                FROM {_FTS_TABLE} f
                JOIN {_META_TABLE} m ON m.rowid = f.rowid
                WHERE {_FTS_TABLE} MATCH ?
            """
            params: list[Any] = [fts_query]
            if page_types:
                placeholders = ",".join("?" for _ in page_types)
                sql += f" AND m.page_type IN ({placeholders})"
                params.extend(page_types)
            if status:
                sql += " AND m.status = ?"
                params.append(status)
            if project:
                sql += " AND m.project = ?"
                params.append(project)
            if participant:
                sql += " AND m.participants LIKE ?"
                params.append(f"%{participant}%")
            if owner:
                sql += " AND m.owners LIKE ?"
                params.append(f"%{owner}%")
            if person:
                sql += " AND (m.participants LIKE ? OR m.owners LIKE ?)"
                params.extend([f"%{person}%", f"%{person}%"])
            if date_from:
                sql += " AND m.decision_date >= ?"
                params.append(date_from)
            if date_to:
                sql += " AND m.decision_date <= ?"
                params.append(date_to)
            if min_confidence is not None:
                sql += " AND m.confidence >= ?"
                params.append(int(min_confidence))
            sql += " ORDER BY rank_score LIMIT ?"
            params.append(max(1, min(int(top_k), 100)))

            rows = conn.execute(sql, params).fetchall()

        results: list[WikiSearchResult] = []
        for row in rows:
            citations = _string_list(row["citations"])
            metadata = {
                "status": row["status"],
                "project": row["project"],
                "decision_date": row["decision_date"],
                "confidence": row["confidence"],
                "participants": _string_list(row["participants"]),
                "owners": _string_list(row["owners"]),
                "source_meetings": _string_list(row["source_meetings"]),
                "last_updated": row["last_updated"],
            }
            results.append(
                WikiSearchResult(
                    page_path=row["page_path"],
                    page_type=row["page_type"],
                    title=row["title"],
                    snippet=_snippet(row["body"], query),
                    # FTS5 bm25() 는 낮을수록 관련도 높음. API 는 높을수록 좋게 노출.
                    score=-float(row["rank_score"]),
                    citations=citations,
                    metadata=metadata,
                )
            )
        return results

    @staticmethod
    def _upsert_page_conn(conn: sqlite3.Connection, page: WikiPage) -> None:
        """열린 connection 에 단일 WikiPage 를 upsert 한다."""
        page_path = str(page.path)
        conn.execute(
            f"DELETE FROM {_FTS_TABLE} WHERE rowid IN "
            f"(SELECT rowid FROM {_META_TABLE} WHERE page_path = ?)",
            (page_path,),
        )
        conn.execute(f"DELETE FROM {_META_TABLE} WHERE page_path = ?", (page_path,))

        fm = page.frontmatter or {}
        title = _title_from_page(page)
        page_type = str(page.page_type.value)
        project_values = _string_list(fm.get("project")) or _string_list(fm.get("projects"))
        project = project_values[0] if project_values else ""
        participants = _string_list(fm.get("participants"))
        owners = _string_list(fm.get("owners"))
        status = str(fm.get("status") or "")
        decision_date = str(fm.get("decision_date") or fm.get("date") or "")
        confidence_raw = fm.get("confidence", 0)
        try:
            confidence = int(confidence_raw)
        except (TypeError, ValueError):
            confidence = 0
        source_meetings = _string_list(fm.get("source_meetings"))
        if not source_meetings and fm.get("meeting_id"):
            source_meetings = [str(fm.get("meeting_id"))]
        citations = _citation_strings(page)
        last_updated = str(fm.get("last_updated") or fm.get("updated_at") or "")

        cursor = conn.execute(
            f"""
            INSERT INTO {_FTS_TABLE}
                (page_path, page_type, title, body, project, participants,
                 owners, status, citations)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                page_path,
                page_type,
                title or "",
                page.content,
                project,
                " ".join(participants),
                " ".join(owners),
                status,
                " ".join(citations),
            ),
        )
        rowid = int(cursor.lastrowid)
        conn.execute(
            f"""
            INSERT INTO {_META_TABLE}
                (page_path, rowid, page_type, title, status, project,
                 decision_date, confidence, participants, owners,
                 source_meetings, citations, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                page_path,
                rowid,
                page_type,
                title,
                status,
                project,
                decision_date,
                confidence,
                ",".join(participants),
                ",".join(owners),
                ",".join(source_meetings),
                ",".join(citations),
                last_updated,
            ),
        )
