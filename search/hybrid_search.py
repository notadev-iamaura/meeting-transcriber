"""
하이브리드 검색 엔진 모듈 (Hybrid Search Engine Module)

목적: ChromaDB 벡터 검색과 SQLite FTS5 키워드 검색을 RRF로 결합하여
      한국어 회의 전사문을 의미 + 키워드 기반으로 검색한다.
주요 기능:
    - ChromaDB 벡터 유사도 검색 (cosine 거리, query: 접두사 자동 추가)
    - SQLite FTS5 전문 검색 (unicode61 토크나이저, 한국어 공백 기반)
    - RRF(Reciprocal Rank Fusion) 결합 (벡터 0.6 + FTS 0.4, k=60)
    - 날짜/화자 필터링 지원
    - 한쪽 검색 실패 시 다른 쪽 결과만 반환 (graceful degradation)
    - ModelLoadManager를 통한 임베딩 모델 라이프사이클 관리
    - 비동기(async) 인터페이스 지원
의존성: config 모듈, core/model_manager 모듈, steps/embedder 모듈,
        sentence_transformers, chromadb
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import AppConfig, get_config
from core.model_manager import ModelLoadManager, get_model_manager
from steps.embedder import _CHROMA_COLLECTION_NAME, _FTS_TABLE_NAME

logger = logging.getLogger(__name__)


# === 에러 계층 ===


class SearchError(Exception):
    """검색 처리 중 발생하는 에러의 기본 클래스."""


class EmptyQueryError(SearchError):
    """검색 쿼리가 비어있을 때 발생한다."""


class ModelLoadError(SearchError):
    """검색용 임베딩 모델 로드 실패 시 발생한다."""


# === 결과 데이터 클래스 ===


@dataclass
class SearchResult:
    """단일 검색 결과를 나타내는 데이터 클래스.

    Attributes:
        chunk_id: 청크 고유 식별자
        text: 청크 텍스트
        score: RRF 결합 점수 (높을수록 관련도 높음)
        meeting_id: 회의 식별자
        date: 회의 날짜 문자열
        speakers: 포함된 화자 목록
        start_time: 시작 시간 (초)
        end_time: 종료 시간 (초)
        chunk_index: 청크 순서 인덱스
        source: 검색 소스 ("vector", "fts", "both")
    """

    chunk_id: str
    text: str
    score: float
    meeting_id: str
    date: str
    speakers: list[str]
    start_time: float
    end_time: float
    chunk_index: int = 0
    source: str = "both"

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화용).

        Returns:
            검색 결과 딕셔너리
        """
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "score": self.score,
            "meeting_id": self.meeting_id,
            "date": self.date,
            "speakers": self.speakers,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "chunk_index": self.chunk_index,
            "source": self.source,
        }


@dataclass
class SearchResponse:
    """전체 검색 응답을 담는 데이터 클래스.

    Attributes:
        results: 검색 결과 목록 (점수 내림차순)
        query: 원본 검색 쿼리
        total_found: 검색된 총 결과 수 (top_k 적용 전)
        vector_count: 벡터 검색 결과 수
        fts_count: FTS 검색 결과 수
        filters_applied: 적용된 필터 정보
    """

    results: list[SearchResult]
    query: str
    total_found: int = 0
    vector_count: int = 0
    fts_count: int = 0
    filters_applied: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화용).

        Returns:
            검색 응답 딕셔너리
        """
        return {
            "results": [r.to_dict() for r in self.results],
            "query": self.query,
            "total_found": self.total_found,
            "vector_count": self.vector_count,
            "fts_count": self.fts_count,
            "filters_applied": self.filters_applied,
        }


# === 내부 데이터 구조 ===


@dataclass
class _RankedItem:
    """RRF 결합을 위한 내부 랭킹 아이템.

    Attributes:
        chunk_id: 청크 고유 식별자
        text: 청크 텍스트
        meeting_id: 회의 식별자
        date: 회의 날짜
        speakers: 화자 목록
        start_time: 시작 시간
        end_time: 종료 시간
        chunk_index: 청크 인덱스
        vector_rank: 벡터 검색 순위 (없으면 None)
        fts_rank: FTS 검색 순위 (없으면 None)
    """

    chunk_id: str
    text: str
    meeting_id: str
    date: str
    speakers: list[str]
    start_time: float
    end_time: float
    chunk_index: int = 0
    vector_rank: int | None = None
    fts_rank: int | None = None


# === RRF 결합 함수 ===


def _compute_rrf_score(
    vector_rank: int | None,
    fts_rank: int | None,
    vector_weight: float,
    fts_weight: float,
    k: int,
) -> float:
    """RRF(Reciprocal Rank Fusion) 점수를 계산한다.

    RRF 공식: score = w_v * 1/(k + rank_v) + w_f * 1/(k + rank_f)
    한쪽 순위가 없으면 해당 항의 기여는 0이다.

    Args:
        vector_rank: 벡터 검색 순위 (1-based, None이면 미포함)
        fts_rank: FTS 검색 순위 (1-based, None이면 미포함)
        vector_weight: 벡터 검색 가중치
        fts_weight: FTS 검색 가중치
        k: RRF 파라미터 (순위 차이의 영향 완화)

    Returns:
        RRF 결합 점수 (0 이상)
    """
    score = 0.0
    if vector_rank is not None:
        score += vector_weight * (1.0 / (k + vector_rank))
    if fts_rank is not None:
        score += fts_weight * (1.0 / (k + fts_rank))
    return score


def _combine_rrf(
    vector_results: list[dict[str, Any]],
    fts_results: list[dict[str, Any]],
    vector_weight: float,
    fts_weight: float,
    rrf_k: int,
    top_k: int,
) -> list[SearchResult]:
    """벡터 검색과 FTS 검색 결과를 RRF로 결합한다.

    동일 chunk_id를 가진 결과를 병합하고, RRF 점수로 정렬하여
    상위 top_k개만 반환한다.

    Args:
        vector_results: 벡터 검색 결과 목록
        fts_results: FTS 검색 결과 목록
        vector_weight: 벡터 검색 가중치
        fts_weight: FTS 검색 가중치
        rrf_k: RRF 파라미터
        top_k: 반환할 최대 결과 수

    Returns:
        RRF 점수로 정렬된 검색 결과 목록
    """
    # chunk_id → _RankedItem 매핑 구성
    items: dict[str, _RankedItem] = {}

    # 벡터 검색 결과 추가 (1-based 순위)
    for rank, result in enumerate(vector_results, start=1):
        chunk_id = result["chunk_id"]
        speakers = result.get("speakers", [])
        if isinstance(speakers, str):
            speakers = [s.strip() for s in speakers.split(",") if s.strip()]

        if chunk_id not in items:
            items[chunk_id] = _RankedItem(
                chunk_id=chunk_id,
                text=result.get("text", ""),
                meeting_id=result.get("meeting_id", ""),
                date=result.get("date", ""),
                speakers=speakers,
                start_time=float(result.get("start_time", 0.0)),
                end_time=float(result.get("end_time", 0.0)),
                chunk_index=int(result.get("chunk_index", 0)),
            )
        items[chunk_id].vector_rank = rank

    # FTS 검색 결과 추가 (1-based 순위)
    for rank, result in enumerate(fts_results, start=1):
        chunk_id = result["chunk_id"]
        speakers = result.get("speakers", [])
        if isinstance(speakers, str):
            speakers = [s.strip() for s in speakers.split(",") if s.strip()]

        if chunk_id not in items:
            items[chunk_id] = _RankedItem(
                chunk_id=chunk_id,
                text=result.get("text", ""),
                meeting_id=result.get("meeting_id", ""),
                date=result.get("date", ""),
                speakers=speakers,
                start_time=float(result.get("start_time", 0.0)),
                end_time=float(result.get("end_time", 0.0)),
                chunk_index=int(result.get("chunk_index", 0)),
            )
        items[chunk_id].fts_rank = rank

    # RRF 점수 계산 및 SearchResult 생성
    scored_results: list[SearchResult] = []
    for item in items.values():
        score = _compute_rrf_score(
            vector_rank=item.vector_rank,
            fts_rank=item.fts_rank,
            vector_weight=vector_weight,
            fts_weight=fts_weight,
            k=rrf_k,
        )

        # 검색 소스 결정
        if item.vector_rank is not None and item.fts_rank is not None:
            source = "both"
        elif item.vector_rank is not None:
            source = "vector"
        else:
            source = "fts"

        scored_results.append(
            SearchResult(
                chunk_id=item.chunk_id,
                text=item.text,
                score=score,
                meeting_id=item.meeting_id,
                date=item.date,
                speakers=item.speakers,
                start_time=item.start_time,
                end_time=item.end_time,
                chunk_index=item.chunk_index,
                source=source,
            )
        )

    # 점수 내림차순 정렬 후 상위 top_k개 반환
    scored_results.sort(key=lambda r: r.score, reverse=True)
    return scored_results[:top_k]


# === 벡터 검색 ===


def _search_vector(
    query_embedding: list[float],
    collection: Any,
    top_k: int,
    date_filter: str | None = None,
    speaker_filter: str | None = None,
    meeting_id_filter: str | None = None,
) -> list[dict[str, Any]]:
    """ChromaDB에서 벡터 유사도 검색을 수행한다.

    캐시된 ChromaDB 컬렉션 객체를 직접 받아 사용하므로
    매 쿼리마다 PersistentClient를 재생성하지 않는다. (PERF-011)

    Args:
        query_embedding: 쿼리 임베딩 벡터 (384차원)
        collection: 캐시된 ChromaDB 컬렉션 객체 (None이면 빈 결과 반환)
        top_k: 반환할 최대 결과 수
        date_filter: 날짜 필터 (정확 매칭, 예: "2026-03-04")
        speaker_filter: 화자 필터 (포함 매칭, 예: "SPEAKER_00")
        meeting_id_filter: 회의 ID 필터 (정확 매칭)

    Returns:
        검색 결과 딕셔너리 목록 (chunk_id, text, metadata 포함)
    """
    try:
        # 컬렉션이 없으면 빈 결과 반환 (graceful degradation)
        if collection is None:
            logger.debug("ChromaDB 컬렉션 미초기화 — 빈 결과 반환")
            return []

        # 컬렉션이 비어있는 경우
        if collection.count() == 0:
            logger.debug("ChromaDB 컬렉션이 비어있습니다.")
            return []

        # 필터 조건 구성
        where_conditions: list[dict[str, Any]] = []
        if date_filter:
            where_conditions.append({"date": {"$eq": date_filter}})
        if speaker_filter:
            where_conditions.append({"speakers": {"$contains": speaker_filter}})
        if meeting_id_filter:
            where_conditions.append({"meeting_id": {"$eq": meeting_id_filter}})

        # ChromaDB where절 구성
        where: dict[str, Any] | None = None
        if len(where_conditions) == 1:
            where = where_conditions[0]
        elif len(where_conditions) > 1:
            where = {"$and": where_conditions}

        # 쿼리 실행
        query_params: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": top_k,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            query_params["where"] = where

        results = collection.query(**query_params)

        # 결과 변환
        output: list[dict[str, Any]] = []
        if results["ids"] and results["ids"][0]:
            ids = results["ids"][0]
            documents = results["documents"][0] if results["documents"] else []
            metadatas = results["metadatas"][0] if results["metadatas"] else []

            for i, chunk_id in enumerate(ids):
                meta = metadatas[i] if i < len(metadatas) else {}
                output.append(
                    {
                        "chunk_id": chunk_id,
                        "text": documents[i] if i < len(documents) else "",
                        "meeting_id": meta.get("meeting_id", ""),
                        "date": meta.get("date", ""),
                        "speakers": meta.get("speakers", ""),
                        "start_time": meta.get("start_time", 0.0),
                        "end_time": meta.get("end_time", 0.0),
                        "chunk_index": meta.get("chunk_index", 0),
                    }
                )

        logger.debug(f"벡터 검색 결과: {len(output)}개")
        return output

    except Exception as e:
        logger.exception(f"벡터 검색 실패: {e}")
        return []


# === FTS5 검색 ===


def _search_fts(
    query: str,
    db_path: Path,
    top_k: int,
    date_filter: str | None = None,
    speaker_filter: str | None = None,
    meeting_id_filter: str | None = None,
    cached_conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """SQLite FTS5에서 키워드 검색을 수행한다.

    FTS5 MATCH 구문으로 전문 검색하고, bm25 점수로 정렬한다.
    PERF: cached_conn이 제공되면 매번 connect/close를 반복하지 않고 재사용한다.

    Args:
        query: 검색 쿼리 문자열
        db_path: SQLite 데이터베이스 파일 경로
        top_k: 반환할 최대 결과 수
        date_filter: 날짜 필터 (정확 매칭)
        speaker_filter: 화자 필터 (포함 매칭)
        meeting_id_filter: 회의 ID 필터 (정확 매칭)
        cached_conn: 캐시된 SQLite 연결 (None이면 새 연결 생성 후 닫음)

    Returns:
        검색 결과 딕셔너리 목록
    """
    try:
        # PERF: 캐시된 연결 사용 시 connect/close 생략
        conn: sqlite3.Connection | None = cached_conn
        should_close = False

        if conn is None:
            if not db_path.exists():
                logger.warning(f"FTS5 데이터베이스 없음: {db_path}")
                return []
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            should_close = True

        try:
            # FTS5 테이블 존재 확인
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (_FTS_TABLE_NAME,),
            )
            if not cursor.fetchone():
                logger.warning(f"FTS5 테이블 미존재: {_FTS_TABLE_NAME}")
                return []

            # FTS5 MATCH 쿼리 구성
            fts_query = _build_fts_query(query)
            if not fts_query:
                return []

            # 기본 FTS5 쿼리
            sql = f"""
                SELECT
                    chunk_id, text, meeting_id, date, speakers,
                    start_time, end_time, chunk_index,
                    bm25({_FTS_TABLE_NAME}) AS rank_score
                FROM {_FTS_TABLE_NAME}
                WHERE {_FTS_TABLE_NAME} MATCH ?
            """
            params: list[Any] = [fts_query]

            # 추가 필터 조건 (FTS5 content 컬럼 기반)
            if date_filter:
                sql += " AND date = ?"
                params.append(date_filter)
            if speaker_filter:
                sql += " AND speakers LIKE ?"
                params.append(f"%{speaker_filter}%")
            if meeting_id_filter:
                sql += " AND meeting_id = ?"
                params.append(meeting_id_filter)

            # bm25 점수 내림차순 (bm25는 음수 → 작을수록 관련도 높음)
            sql += " ORDER BY rank_score LIMIT ?"
            params.append(top_k)

            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()

            # 결과 변환
            output: list[dict[str, Any]] = []
            for row in rows:
                output.append(
                    {
                        "chunk_id": row["chunk_id"],
                        "text": row["text"],
                        "meeting_id": row["meeting_id"],
                        "date": row["date"],
                        "speakers": row["speakers"],
                        "start_time": row["start_time"],
                        "end_time": row["end_time"],
                        "chunk_index": row["chunk_index"],
                    }
                )

            logger.debug(f"FTS5 검색 결과: {len(output)}개")
            return output

        finally:
            # 캐시된 연결은 닫지 않음
            if should_close:
                conn.close()

    except Exception as e:
        logger.exception(f"FTS5 검색 실패: {e}")
        return []


def _build_fts_query(query: str) -> str:
    """검색 쿼리를 FTS5 MATCH 형식으로 변환한다.

    공백으로 분리된 각 단어를 OR로 연결하여 부분 매칭을 지원한다.
    특수 문자는 제거하여 FTS5 파싱 오류를 방지한다.

    Args:
        query: 사용자 검색 쿼리

    Returns:
        FTS5 MATCH 호환 쿼리 문자열 (빈 문자열이면 검색 불가)
    """
    # FTS5 특수 문자 제거 (안전한 검색을 위해)
    # FTS5 연산자 문자: AND, OR, NOT, *, ^, "
    safe_chars = []
    for ch in query:
        if ch.isalnum() or ch == " " or ord(ch) > 127:
            # 알파벳, 숫자, 공백, 비ASCII(한국어 등) 허용
            safe_chars.append(ch)
        else:
            safe_chars.append(" ")

    cleaned = "".join(safe_chars).strip()
    if not cleaned:
        return ""

    # 공백으로 분리된 단어를 OR로 연결
    words = cleaned.split()
    if not words:
        return ""

    # 단일 단어면 그대로, 복수면 OR 연결
    if len(words) == 1:
        return words[0]

    return " OR ".join(words)


# === 메인 클래스 ===


class HybridSearchEngine:
    """하이브리드 검색 엔진.

    ChromaDB 벡터 검색과 SQLite FTS5 키워드 검색을 RRF로 결합하여
    의미 기반 + 키워드 기반 하이브리드 검색을 제공한다.

    벡터 검색은 의미적 유사도를 포착하고, FTS5는 정확한 키워드 매칭을
    수행하여 서로 보완적인 검색 결과를 생성한다.

    Args:
        config: 애플리케이션 설정 (None이면 싱글턴 사용)
        model_manager: 모델 로드 매니저 (None이면 싱글턴 사용)

    사용 예시:
        engine = HybridSearchEngine(config, model_manager)
        response = await engine.search("프로젝트 일정 논의")
        for r in response.results:
            print(f"[{r.score:.4f}] {r.text[:50]}...")
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        model_manager: ModelLoadManager | None = None,
    ) -> None:
        """HybridSearchEngine을 초기화한다.

        Args:
            config: 애플리케이션 설정 (None이면 get_config() 사용)
            model_manager: 모델 로드 매니저 (None이면 get_model_manager() 사용)
        """
        self._config = config or get_config()
        self._model_manager = model_manager or get_model_manager()

        # 검색 설정 캐시
        self._vector_weight = self._config.search.vector_weight
        self._fts_weight = self._config.search.fts_weight
        self._rrf_k = self._config.search.rrf_k
        self._top_k = self._config.search.top_k

        # 임베딩 설정 캐시
        self._model_name = self._config.embedding.model_name
        self._device = self._config.embedding.device
        self._query_prefix = self._config.embedding.query_prefix

        # 저장소 경로
        self._chroma_dir = self._config.paths.resolved_chroma_db_dir
        self._meetings_db = self._config.paths.resolved_meetings_db

        # PERF-005: 임베딩 모델 캐시 (지연 초기화, 스레드 안전)
        self._embed_model: Any = None
        self._embed_model_lock = threading.Lock()

        # PERF-011: ChromaDB 클라이언트 및 컬렉션 캐시 (지연 초기화, 스레드 안전)
        self._chroma_client: Any = None
        self._chroma_collection: Any = None
        self._chroma_lock = threading.Lock()

        # PERF: FTS5 SQLite 연결 캐시 (매 검색마다 connect/close 반복 제거)
        self._fts_conn: sqlite3.Connection | None = None
        self._fts_conn_lock = threading.Lock()

        logger.info(
            f"HybridSearchEngine 초기화: "
            f"vector_weight={self._vector_weight}, "
            f"fts_weight={self._fts_weight}, "
            f"rrf_k={self._rrf_k}, top_k={self._top_k}"
        )

    def _get_chroma_collection(self) -> Any:
        """캐시된 ChromaDB 컬렉션을 반환한다 (지연 초기화). (PERF-011)

        첫 호출 시 PersistentClient와 컬렉션을 생성하고 캐시한다.
        이후 호출에서는 캐시된 인스턴스를 재사용하여 50-200ms 오버헤드를 제거한다.
        컬렉션이 존재하지 않으면 None을 반환한다 (빈 결과로 처리).

        Returns:
            ChromaDB 컬렉션 객체 또는 None (컬렉션 미존재 시)
        """
        # 빠른 경로: 이미 초기화된 경우 락 없이 반환
        if self._chroma_collection is not None:
            return self._chroma_collection

        with self._chroma_lock:
            # 더블 체크 락킹 (Double-Checked Locking)
            if self._chroma_collection is not None:
                return self._chroma_collection

            if not self._chroma_dir.exists():
                logger.warning(f"ChromaDB 디렉토리 없음: {self._chroma_dir}")
                return None

            try:
                import chromadb  # lazy import: chromadb가 무거우므로 필요 시에만 로드

                self._chroma_client = chromadb.PersistentClient(path=str(self._chroma_dir))

                # 컬렉션 존재 확인
                self._chroma_collection = self._chroma_client.get_collection(
                    name=_CHROMA_COLLECTION_NAME,
                )
                logger.info(f"ChromaDB 컬렉션 캐시 완료: {_CHROMA_COLLECTION_NAME}")
                return self._chroma_collection

            except Exception:
                logger.warning(f"ChromaDB 컬렉션 미존재 또는 접근 실패: {_CHROMA_COLLECTION_NAME}")
                return None

    def _get_embed_model(self) -> Any:
        """캐시된 임베딩 모델을 반환한다 (지연 초기화). (PERF-005)

        첫 호출 시 SentenceTransformer 모델을 로드하고 캐시한다.
        이후 호출에서는 캐시된 인스턴스를 재사용하여 1-2초 오버헤드를 제거한다.
        threading.Lock으로 동시 초기화를 방지한다.

        Returns:
            SentenceTransformer 모델 인스턴스

        Raises:
            ModelLoadError: 모델 로드 실패 시
        """
        # 빠른 경로: 이미 로드된 경우 락 없이 반환
        if self._embed_model is not None:
            return self._embed_model

        with self._embed_model_lock:
            # 더블 체크 락킹 (Double-Checked Locking)
            if self._embed_model is not None:
                return self._embed_model

            self._embed_model = self._load_model()
            return self._embed_model

    def _get_fts_connection(self) -> sqlite3.Connection | None:
        """캐시된 FTS5 SQLite 연결을 반환한다 (지연 초기화).

        첫 호출 시 WAL 모드로 연결을 생성하고 캐시한다.
        이후 호출에서는 캐시된 연결을 재사용하여 연결 생성 오버헤드를 제거한다.
        DB 파일이 없으면 None을 반환한다.

        Returns:
            sqlite3.Connection 또는 None (DB 파일 미존재 시)
        """
        # 빠른 경로: 이미 초기화된 경우 락 없이 반환
        if self._fts_conn is not None:
            return self._fts_conn

        with self._fts_conn_lock:
            # 더블 체크 락킹 (Double-Checked Locking)
            if self._fts_conn is not None:
                return self._fts_conn

            if not self._meetings_db.exists():
                logger.warning(f"FTS5 데이터베이스 없음: {self._meetings_db}")
                return None

            try:
                conn = sqlite3.connect(
                    str(self._meetings_db),
                    check_same_thread=False,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                self._fts_conn = conn
                logger.info(f"FTS5 SQLite 연결 캐시 완료: {self._meetings_db}")
                return self._fts_conn
            except Exception as e:
                logger.exception(f"FTS5 SQLite 연결 실패: {e}")
                return None

    def _load_model(self) -> Any:
        """sentence_transformers 모델을 로드한다.

        검색 쿼리 임베딩 생성을 위해 모델을 로드한다.
        MPS 디바이스를 사용하여 Apple Silicon 가속을 활용한다.

        Returns:
            SentenceTransformer 모델 인스턴스

        Raises:
            ModelLoadError: 모델 로드 실패 시
        """
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(
                self._model_name,
                device=self._device,
            )
            logger.info(
                f"검색용 임베딩 모델 로드 완료: {self._model_name} (device={self._device})"
            )
            return model
        except Exception as e:
            raise ModelLoadError(f"검색용 임베딩 모델 로드 실패: {self._model_name} - {e}") from e

    def _embed_query(self, model: Any, query: str) -> list[float]:
        """검색 쿼리를 벡터로 변환한다.

        query: 접두사를 자동 추가하고 NFC 정규화를 적용한다.

        Args:
            model: SentenceTransformer 모델 인스턴스
            query: 검색 쿼리 문자열

        Returns:
            384차원 쿼리 임베딩 벡터
        """
        # query: 접두사 추가 (e5 모델 비대칭 검색 요구사항)
        prefixed_query = f"{self._query_prefix}{query}"

        # NFC 정규화 (한국어 유니코드 일관성)
        prefixed_query = unicodedata.normalize("NFC", prefixed_query)

        # 단일 텍스트 인코딩
        embedding = model.encode(
            [prefixed_query],
            show_progress_bar=False,
            normalize_embeddings=True,
        )

        return embedding[0].tolist()

    async def search(
        self,
        query: str,
        date_filter: str | None = None,
        speaker_filter: str | None = None,
        meeting_id_filter: str | None = None,
        top_k: int | None = None,
    ) -> SearchResponse:
        """하이브리드 검색을 수행한다.

        1. 쿼리를 벡터로 임베딩 (ModelLoadManager 사용)
        2. ChromaDB 벡터 유사도 검색
        3. SQLite FTS5 키워드 검색
        4. RRF로 결합하여 최종 결과 반환

        Args:
            query: 검색 쿼리 문자열
            date_filter: 날짜 필터 (예: "2026-03-04")
            speaker_filter: 화자 필터 (예: "SPEAKER_00")
            meeting_id_filter: 회의 ID 필터 (예: "meeting_001")
            top_k: 반환할 최대 결과 수 (None이면 설정값 사용)

        Returns:
            검색 응답 (SearchResponse)

        Raises:
            EmptyQueryError: 쿼리가 비어있을 때
            ModelLoadError: 임베딩 모델 로드 실패 시
        """
        # 쿼리 전처리
        query = query.strip()
        if not query:
            raise EmptyQueryError("검색 쿼리가 비어있습니다.")

        # NFC 정규화
        query = unicodedata.normalize("NFC", query)

        effective_top_k = top_k or self._top_k
        # 각 소스에서 더 많은 결과를 가져와 RRF 결합 후 top_k로 절단
        fetch_k = effective_top_k * 3

        logger.info(f"하이브리드 검색 시작: query='{query}', top_k={effective_top_k}")

        # 필터 정보 기록
        filters_applied: dict[str, Any] = {}
        if date_filter:
            filters_applied["date"] = date_filter
        if speaker_filter:
            filters_applied["speaker"] = speaker_filter
        if meeting_id_filter:
            filters_applied["meeting_id"] = meeting_id_filter

        # 1. 쿼리 임베딩 생성 (PERF-005: 캐시된 모델 사용)
        model = self._get_embed_model()
        query_embedding = await asyncio.to_thread(self._embed_query, model, query)

        # 2-3. 벡터 검색과 FTS5 검색을 병렬 실행 (PERF-010)
        # 두 검색은 독립적인 I/O 작업이므로 동시에 수행할 수 있다.
        # PERF-011: 캐시된 ChromaDB 컬렉션 사용
        collection = self._get_chroma_collection()

        vector_task = asyncio.to_thread(
            _search_vector,
            query_embedding,
            collection,
            fetch_k,
            date_filter,
            speaker_filter,
            meeting_id_filter,
        )
        # PERF: 캐시된 FTS SQLite 연결 사용 (매 검색마다 connect/close 제거)
        fts_conn = self._get_fts_connection()
        fts_task = asyncio.to_thread(
            _search_fts,
            query,
            self._meetings_db,
            fetch_k,
            date_filter,
            speaker_filter,
            meeting_id_filter,
            fts_conn,
        )
        vector_results, fts_results = await asyncio.gather(vector_task, fts_task)

        # 4. RRF 결합
        combined = _combine_rrf(
            vector_results=vector_results,
            fts_results=fts_results,
            vector_weight=self._vector_weight,
            fts_weight=self._fts_weight,
            rrf_k=self._rrf_k,
            top_k=effective_top_k,
        )

        response = SearchResponse(
            results=combined,
            query=query,
            total_found=len(combined),
            vector_count=len(vector_results),
            fts_count=len(fts_results),
            filters_applied=filters_applied,
        )

        logger.info(
            f"하이브리드 검색 완료: query='{query}', "
            f"벡터={response.vector_count}개, "
            f"FTS={response.fts_count}개, "
            f"결합={response.total_found}개"
        )

        return response
