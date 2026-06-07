"""Wiki BM25/FTS5 search index.

Decision Wiki 검색은 transcript RAG 와 분리된 인덱스를 사용한다. 이 모듈은
위키 페이지를 SQLite FTS5 로 색인하고 BM25 점수로 page/decision 검색을 제공한다.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.wiki.models import WikiPage
from core.wiki.store import WikiStore, WikiStoreError

if TYPE_CHECKING:
    from config import WikiRankingConfig

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
    """Wiki 검색 결과.

    score 는 '해당 쿼리 내 상대 랭킹 점수'다. 재랭킹(enabled=True) 시 후보 풀 기준
    정규화 결합점수(비-superseded 0~Σw, superseded 음수)이고, enabled=False 시
    -BM25(rank_score) 다. 절대 비교·임계 필터·transcript RAG 점수와의 혼용은
    불가하며, 결과의 '순서'만 신뢰해야 한다.
    """

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


# ─── 다중신호 재랭킹(Memorable Wiki C1) ────────────────────────────────────
# BM25 단일 점수에 recency·confidence·인용빈도·superseded 처리(+선택적 MMR
# 다양성)를 결합한다. 원문/스키마는 변경하지 않고 검색 점수만 후처리로 조정한다
# (불변식 #2 — 점수만 조정).
#
# 후보 풀 주의: 재랭킹은 BM25 상위 candidate_pool 건 안에서만 수행된다(검색 시
# SQL LIMIT). 한 쿼리에 candidate_pool 을 초과해 매칭되는 페이지가 있으면, BM25
# 하위지만 최신/고신뢰인 결정은 풀 밖으로 밀려 재랭킹 전에 누락될 수 있다. 소규모
# corpus 가정에서는 무해하며, 매칭 폭이 커지면 config 의 candidate_pool 을 올린다.

# 인용 마커는 MMR 어휘 유사도에서 노이즈(공통 'meeting'/id/timestamp 토큰)를 만들어
# 제거한다.
_CITATION_MARKER_RE = re.compile(r"\[meeting:[^\]]*\]")


@dataclass(frozen=True)
class _Candidate:
    """재랭킹 입력 후보 — 검색 결과 1건의 원시 신호."""

    page_path: str
    page_type: str
    title: str | None
    snippet: str
    bm25: float  # 높을수록 관련도 높음 (= -FTS5 rank_score)
    status: str
    decision_date: str
    last_updated: str
    confidence: int
    citations: list[str]
    citation_count: (
        int  # 메타 citations 컬럼 기준 인용 마커 수(본문 아님). _string_list 통합과 무관
    )
    metadata: dict[str, Any]


def _row_to_candidate(row: sqlite3.Row, query: str, *, bm25: float) -> _Candidate:
    """SQL row(meta + body 포함)를 재랭킹 후보 _Candidate 로 변환한다.

    bm25 는 호출자가 명시한다 — BM25 검색은 -rank_score, 벡터 전용 보강은 0.0
    placeholder(하이브리드 융합에서 RRF 로 대체).
    """
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
    try:
        confidence = int(row["confidence"]) if row["confidence"] is not None else 0
    except (TypeError, ValueError):
        logger.warning(
            "재랭킹: confidence 파싱 실패 page=%s raw=%r → 0 폴백",
            row["page_path"],
            row["confidence"],
        )
        confidence = 0
    return _Candidate(
        page_path=row["page_path"],
        page_type=row["page_type"],
        title=row["title"],
        snippet=_snippet(row["body"], query),
        bm25=bm25,
        status=row["status"] or "",
        decision_date=row["decision_date"] or "",
        last_updated=row["last_updated"] or "",
        confidence=confidence,
        citations=citations,
        citation_count=str(row["citations"] or "").count("[meeting:"),
        metadata=metadata,
    )


def _parse_date(value: str | None) -> date | None:
    """'YYYY-MM-DD' 또는 ISO datetime 문자열의 날짜부를 date 로 파싱한다."""
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _recency_score(date_str: str | None, now: date, half_life_days: float) -> float:
    """반감기 감쇠 최신성 점수 [0,1]. age=0 → 1.0, age=half_life → 0.5, 미파싱 → 0.0."""
    parsed = _parse_date(date_str)
    if parsed is None:
        return 0.0
    age_days = (now - parsed).days
    if age_days < 0:
        age_days = 0
    # float ** float 의 mypy 추론이 Any 이므로 명시적 float 캐스트.
    return float(0.5 ** (age_days / half_life_days))


def _minmax(values: list[float]) -> list[float]:
    """리스트를 [0,1] 로 min-max 정규화한다. 전부 동률이면 신호 0(기여 없음)."""
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if hi <= lo:
        return [0.0 for _ in values]
    span = hi - lo
    return [(v - lo) / span for v in values]


def _tokens(text: str) -> set[str]:
    """MMR 유사도용 토큰 집합 (영문/숫자/한글 유지, 구두점/공백으로 분리)."""
    out: list[str] = []
    cur: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return set(out)


def _jaccard(a: set[str], b: set[str]) -> float:
    """두 토큰 집합의 Jaccard 유사도."""
    union = len(a | b)
    if union == 0:
        return 0.0
    return len(a & b) / union


def _rerank(
    candidates: list[_Candidate], ranking: WikiRankingConfig, now: date
) -> list[tuple[_Candidate, float]]:
    """다중신호 선형 결합으로 (후보, 최종점수) 를 점수 내림차순 정렬해 반환한다.

    superseded 페이지는 가중치와 무관하게 모든 비-superseded 페이지 아래로 강제된다
    ('역전 0%' 구조적 보장). 비-superseded 점수는 [0, Σw] 범위이고, superseded 는
    `positive − Σw − superseded_penalty` 로 항상 음수가 되어 두 집합이 분리된다.
    """
    weight_sum = ranking.w_bm25 + ranking.w_recency + ranking.w_confidence + ranking.w_citation
    bm25_norm = _minmax([c.bm25 for c in candidates])
    cite_norm = _minmax([float(c.citation_count) for c in candidates])
    scored: list[tuple[_Candidate, float]] = []
    for i, c in enumerate(candidates):
        recency = _recency_score(
            c.decision_date or c.last_updated, now, ranking.recency_half_life_days
        )
        confidence = max(0, min(10, c.confidence)) / 10.0
        positive = (
            ranking.w_bm25 * bm25_norm[i]
            + ranking.w_recency * recency
            + ranking.w_confidence * confidence
            + ranking.w_citation * cite_norm[i]
        )
        if (c.status or "").strip().lower() == "superseded":
            # 구조적 하향: live 점수대(≥0) 아래로 강제 → 가중치와 무관하게 역전 0%.
            score = positive - weight_sum - ranking.superseded_penalty
        else:
            score = positive
        scored.append((c, score))
    # 점수 내림차순, 동률은 page_path 로 결정성 확보(SQL tie-break 비의존).
    scored.sort(key=lambda t: (-t[1], t[0].page_path))
    return scored


def _mmr_rerank(
    scored: list[tuple[_Candidate, float]], ranking: WikiRankingConfig, top_k: int
) -> list[tuple[_Candidate, float]]:
    """MMR 다양성 재정렬 — 1순위는 관련도 최댓값으로 시드하고 이후 다양성을 반영한다.

    관련도는 [0,1] 로 정규화하여 어휘 Jaccard 유사도(0~1)와 같은 스케일에서
    `λ·관련도 − (1−λ)·기선택집합과의_최대유사도` 를 최대화하는 후보를 차례로 고른다.
    선택되지 못한 후보는 원래 관련도 순서로 뒤에 이어 붙인다.
    """
    if len(scored) <= 1:
        return scored
    rel_norm = _minmax([s for _, s in scored])
    tokens = [
        _tokens(_CITATION_MARKER_RE.sub(" ", (c.title or "") + " " + c.snippet)) for c, _ in scored
    ]
    lam = ranking.mmr_lambda
    remaining = list(range(len(scored)))
    first = max(remaining, key=lambda i: rel_norm[i])
    selected = [first]
    remaining.remove(first)
    limit = min(max(1, int(top_k)), len(scored))
    while remaining and len(selected) < limit:
        best_i = remaining[0]
        best_mmr: float | None = None
        for i in remaining:
            max_sim = max((_jaccard(tokens[i], tokens[j]) for j in selected), default=0.0)
            mmr = lam * rel_norm[i] - (1.0 - lam) * max_sim
            if best_mmr is None or mmr > best_mmr:
                best_mmr = mmr
                best_i = i
        selected.append(best_i)
        remaining.remove(best_i)
    ordered = [scored[i] for i in selected]
    leftover = [scored[i] for i in remaining]  # 미선택분: 관련도(원순서) 유지
    return ordered + leftover


class WikiSearchIndex:
    """WikiStore 페이지를 SQLite FTS5/BM25 로 색인하고 검색한다."""

    def __init__(
        self,
        wiki_root: Path,
        db_path: Path | None = None,
        ranking: WikiRankingConfig | None = None,
    ) -> None:
        """인덱스를 초기화한다.

        Args:
            wiki_root: 위키 루트 디렉토리.
            db_path: 검색 DB 경로. None 이면 wiki_root/.index 하위 기본 경로.
            ranking: 다중신호 재랭킹 설정. None 이면 검색 시점에 get_config() 의
                wiki.ranking 에서 지연 해석한다(rebuild-only 사용 시 config 조기 로드 회피).
        """
        self._wiki_root = wiki_root
        self._db_path = db_path or _index_db_path(wiki_root)
        self._ranking = ranking

    @property
    def db_path(self) -> Path:
        """검색 DB 경로."""
        return self._db_path

    def _resolve_ranking(self) -> WikiRankingConfig:
        """주입된 ranking 또는 전역 config 의 wiki.ranking 을 해석한다."""
        if self._ranking is not None:
            return self._ranking
        from config import get_config  # noqa: PLC0415 — 지연 임포트(조기 config 로드 회피)

        return get_config().wiki.ranking

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
        now: date | None = None,
        top_k: int = 20,
    ) -> list[WikiSearchResult]:
        """BM25 + 다중신호 재랭킹 기반 Wiki 검색을 수행한다.

        ranking.enabled(기본 True) 이면 BM25 상위 후보를 recency·confidence·
        인용빈도·superseded 패널티(+선택적 MMR)로 재랭킹한다. enabled=False 면
        순수 BM25 정렬(기존 동작)을 그대로 반환한다.

        Args:
            now: recency 계산 기준일. None 이면 date.today(). 테스트 결정성용 주입 포인트.
            (그 외 인자는 기존과 동일한 필터/페이지네이션 의미)
        """
        query = query.strip()
        if not query:
            return []

        ranking = self._resolve_ranking()
        # 재랭킹 시 BM25 상위 후보 풀(candidate_pool)을 가져온 뒤 후처리로 top_k 선별.
        if ranking.enabled:
            candidate_limit = max(1, int(top_k), ranking.candidate_pool)
        else:
            candidate_limit = max(1, min(int(top_k), 100))
        candidates = self.bm25_candidates(
            query,
            page_types=page_types,
            status=status,
            project=project,
            participant=participant,
            owner=owner,
            person=person,
            date_from=date_from,
            date_to=date_to,
            min_confidence=min_confidence,
            limit=candidate_limit,
        )

        limit = max(1, int(top_k))
        if ranking.enabled:
            scored = _rerank(candidates, ranking, now or date.today())
            if ranking.mmr_enabled:
                scored = _mmr_rerank(scored, ranking, limit)
            ordered = scored[:limit]
        else:
            # escape hatch: 순수 BM25 정렬(기존 동작). 점수는 -rank_score(=bm25).
            ordered = [(c, c.bm25) for c in candidates[:limit]]

        return [
            WikiSearchResult(
                page_path=c.page_path,
                page_type=c.page_type,
                title=c.title,
                snippet=c.snippet,
                score=score,
                citations=c.citations,
                metadata=c.metadata,
            )
            for c, score in ordered
        ]

    def bm25_candidates(
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
        limit: int = 50,
    ) -> list[_Candidate]:
        """필터 적용 BM25 검색 결과를 BM25 순(_Candidate)으로 반환한다(재랭킹 전).

        `search()` 와 G1 하이브리드 경로가 공유하는 BM25 후보 추출. bm25 점수는
        -rank_score(높을수록 관련도 높음).
        """
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
            params.append(max(1, int(limit)))

            rows = conn.execute(sql, params).fetchall()

        return [_row_to_candidate(row, query, bm25=-float(row["rank_score"])) for row in rows]

    def fetch_candidates(self, page_paths: list[str], query: str) -> dict[str, _Candidate]:
        """주어진 page_path 들의 메타+본문을 _Candidate 로 조회한다.

        G1 하이브리드에서 벡터가 찾았으나 BM25 후보에 없는(어휘 비매칭) 페이지의
        메타를 보강한다. bm25 는 0.0 placeholder(융합에서 RRF 로 대체).
        """
        paths = [str(p) for p in page_paths if p]
        if not paths:
            return {}
        placeholders = ",".join("?" for _ in paths)
        with _connect(self._db_path) as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                f"""
                SELECT
                    m.page_path,
                    m.page_type,
                    m.title,
                    m.status,
                    m.project,
                    m.decision_date,
                    m.confidence,
                    m.participants,
                    m.owners,
                    m.source_meetings,
                    m.citations,
                    m.last_updated,
                    f.body
                FROM {_META_TABLE} m
                JOIN {_FTS_TABLE} f ON f.rowid = m.rowid
                WHERE m.page_path IN ({placeholders})
                """,
                paths,
            ).fetchall()
        return {row["page_path"]: _row_to_candidate(row, query, bm25=0.0) for row in rows}

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
        rowid = int(cursor.lastrowid or 0)  # lastrowid 는 INSERT 후 항상 int (mypy int|None 해소)
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
