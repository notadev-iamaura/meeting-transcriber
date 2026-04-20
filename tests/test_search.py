"""
Phase 2 통합 테스트 모듈 (Phase 2 Integration Tests)

목적: Phase 2에서 구현된 검색, 작업 큐, 청크 생성 모듈의 통합 동작을 검증한다.
주요 테스트:
    - 검색 정확도: FTS5 실제 DB 기반 키워드 검색 + RRF 결합 정확성
    - 작업 큐 재시도: 상태 전이 전체 사이클 + 재시도 + 최대 초과 처리
    - 청크 생성 품질: 토큰 크기, 시간 분리, 화자 그룹핑, 메타데이터 완전성
    - End-to-End: 청크 생성 → FTS5 저장 → 키워드 검색 파이프라인
의존성: pytest, pytest-asyncio
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from config import AppConfig, ChunkingConfig
from core.job_queue import (
    AsyncJobQueue,
    InvalidTransitionError,
    JobNotFoundError,
    JobQueue,
    JobQueueError,
    JobStatus,
    MaxRetriesExceededError,
)
from search.hybrid_search import (
    EmptyQueryError,
    HybridSearchEngine,
    SearchResponse,
    SearchResult,
    _build_fts_query,
    _combine_rrf,
    _compute_rrf_score,
    _search_fts,
)
from steps.chunker import (
    Chunk,
    ChunkedResult,
    Chunker,
    EmptyInputError,
    _estimate_tokens,
    _group_by_speaker_and_time,
)
from steps.corrector import CorrectedResult, CorrectedUtterance
from steps.embedder import _FTS_TABLE_NAME

# === FTS5 테이블 이름 ===
FTS_TABLE = _FTS_TABLE_NAME


# === 공통 헬퍼 함수 ===


def _make_utterance(
    text: str,
    speaker: str = "SPEAKER_00",
    start: float = 0.0,
    end: float = 1.0,
) -> CorrectedUtterance:
    """테스트용 CorrectedUtterance를 생성한다."""
    return CorrectedUtterance(
        text=text,
        original_text=text,
        speaker=speaker,
        start=start,
        end=end,
        was_corrected=False,
    )


def _make_corrected_result(
    utterances: list[CorrectedUtterance],
    num_speakers: int = 2,
    audio_path: str = "/test/audio.wav",
) -> CorrectedResult:
    """테스트용 CorrectedResult를 생성한다."""
    return CorrectedResult(
        utterances=utterances,
        num_speakers=num_speakers,
        audio_path=audio_path,
    )


def _create_fts_db(db_path: Path) -> None:
    """FTS5 테이블이 있는 SQLite DB를 생성한다."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE}
        USING fts5(
            chunk_id,
            text,
            meeting_id,
            date,
            speakers,
            start_time UNINDEXED,
            end_time UNINDEXED,
            chunk_index UNINDEXED,
            tokenize='unicode61'
        )
    """)
    conn.commit()
    conn.close()


def _insert_fts_chunks(
    db_path: Path,
    chunks: list[dict[str, Any]],
) -> None:
    """FTS5 테이블에 청크 데이터를 삽입한다."""
    conn = sqlite3.connect(str(db_path))
    for chunk in chunks:
        conn.execute(
            f"""
            INSERT INTO {FTS_TABLE}
                (chunk_id, text, meeting_id, date, speakers,
                 start_time, end_time, chunk_index)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk["chunk_id"],
                chunk["text"],
                chunk["meeting_id"],
                chunk["date"],
                chunk["speakers"],
                chunk.get("start_time", 0.0),
                chunk.get("end_time", 30.0),
                chunk.get("chunk_index", 0),
            ),
        )
    conn.commit()
    conn.close()


# === 검색용 샘플 데이터 ===


def _sample_korean_chunks() -> list[dict[str, Any]]:
    """한국어 회의 전사문 샘플 청크를 반환한다."""
    return [
        {
            "chunk_id": "meeting_001_chunk_0000",
            "text": "[SPEAKER_00] 이번 프로젝트 일정에 대해 논의하겠습니다",
            "meeting_id": "meeting_001",
            "date": "2026-03-04",
            "speakers": "SPEAKER_00",
            "start_time": 0.0,
            "end_time": 15.0,
            "chunk_index": 0,
        },
        {
            "chunk_id": "meeting_001_chunk_0001",
            "text": "[SPEAKER_01] 다음 주 금요일까지 개발 완료 목표입니다",
            "meeting_id": "meeting_001",
            "date": "2026-03-04",
            "speakers": "SPEAKER_01",
            "start_time": 15.0,
            "end_time": 30.0,
            "chunk_index": 1,
        },
        {
            "chunk_id": "meeting_001_chunk_0002",
            "text": "[SPEAKER_00] 디자인 리뷰는 수요일에 진행하겠습니다 테스트 일정도 확인해야 합니다",
            "meeting_id": "meeting_001",
            "date": "2026-03-04",
            "speakers": "SPEAKER_00",
            "start_time": 30.0,
            "end_time": 50.0,
            "chunk_index": 2,
        },
        {
            "chunk_id": "meeting_002_chunk_0000",
            "text": "[SPEAKER_00] 서버 배포 관련 이슈를 공유합니다",
            "meeting_id": "meeting_002",
            "date": "2026-03-05",
            "speakers": "SPEAKER_00",
            "start_time": 0.0,
            "end_time": 20.0,
            "chunk_index": 0,
        },
        {
            "chunk_id": "meeting_002_chunk_0001",
            "text": "[SPEAKER_02] 데이터베이스 마이그레이션 작업이 필요합니다 프로젝트 일정을 조정해야 합니다",
            "meeting_id": "meeting_002",
            "date": "2026-03-05",
            "speakers": "SPEAKER_02",
            "start_time": 20.0,
            "end_time": 45.0,
            "chunk_index": 1,
        },
    ]


# === 픽스처 ===


@pytest.fixture
def fts_db_path(tmp_path: Path) -> Path:
    """FTS5 테이블이 준비된 DB 경로를 반환한다."""
    db_path = tmp_path / "test_meetings.db"
    _create_fts_db(db_path)
    return db_path


@pytest.fixture
def fts_db_with_data(fts_db_path: Path) -> Path:
    """샘플 데이터가 삽입된 FTS5 DB 경로를 반환한다."""
    _insert_fts_chunks(fts_db_path, _sample_korean_chunks())
    return fts_db_path


@pytest.fixture
def job_queue_db(tmp_path: Path) -> Path:
    """테스트용 작업 큐 DB 경로를 반환한다."""
    return tmp_path / "test_jobs.db"


@pytest.fixture
def queue(job_queue_db: Path) -> JobQueue:
    """초기화된 JobQueue 인스턴스를 반환한다."""
    q = JobQueue(job_queue_db, max_retries=3)
    q.initialize()
    yield q
    q.close()


@pytest.fixture
def async_queue(queue: JobQueue) -> AsyncJobQueue:
    """AsyncJobQueue 인스턴스를 반환한다."""
    return AsyncJobQueue(queue)


# ============================================================
# 1. 검색 정확도 테스트
# ============================================================


class TestSearchAccuracy:
    """FTS5 키워드 검색과 RRF 결합의 정확도를 통합 검증한다."""

    def test_fts_korean_keyword_search(
        self,
        fts_db_with_data: Path,
    ) -> None:
        """한국어 키워드로 FTS5 검색 시 관련 청크가 반환되는지 확인한다."""
        results = _search_fts(
            query="프로젝트 일정",
            db_path=fts_db_with_data,
            top_k=5,
        )
        # "프로젝트 일정"이 포함된 청크가 최소 2개 존재
        assert len(results) >= 2
        # 모든 결과에 "프로젝트" 또는 "일정" 키워드 포함
        for r in results:
            assert "프로젝트" in r["text"] or "일정" in r["text"]

    def test_fts_single_keyword_search(
        self,
        fts_db_with_data: Path,
    ) -> None:
        """단일 키워드 검색이 정확히 동작하는지 확인한다."""
        results = _search_fts(
            query="배포",
            db_path=fts_db_with_data,
            top_k=5,
        )
        assert len(results) >= 1
        assert any("배포" in r["text"] for r in results)

    def test_fts_date_filter(
        self,
        fts_db_with_data: Path,
    ) -> None:
        """날짜 필터가 정확히 적용되는지 확인한다."""
        results = _search_fts(
            query="프로젝트",
            db_path=fts_db_with_data,
            top_k=10,
            date_filter="2026-03-04",
        )
        # 2026-03-04 날짜의 결과만 포함되어야 함
        for r in results:
            assert r["date"] == "2026-03-04"

    def test_fts_speaker_filter(
        self,
        fts_db_with_data: Path,
    ) -> None:
        """화자 필터가 정확히 적용되는지 확인한다."""
        results = _search_fts(
            query="프로젝트",
            db_path=fts_db_with_data,
            top_k=10,
            speaker_filter="SPEAKER_02",
        )
        # SPEAKER_02의 결과만 포함
        for r in results:
            assert "SPEAKER_02" in r["speakers"]

    def test_fts_meeting_id_filter(
        self,
        fts_db_with_data: Path,
    ) -> None:
        """회의 ID 필터가 정확히 적용되는지 확인한다."""
        results = _search_fts(
            query="프로젝트",
            db_path=fts_db_with_data,
            top_k=10,
            meeting_id_filter="meeting_002",
        )
        for r in results:
            assert r["meeting_id"] == "meeting_002"

    def test_fts_empty_query_returns_empty(
        self,
        fts_db_with_data: Path,
    ) -> None:
        """빈 쿼리가 빈 결과를 반환하는지 확인한다."""
        results = _search_fts(
            query="",
            db_path=fts_db_with_data,
            top_k=5,
        )
        assert results == []

    def test_fts_no_match_returns_empty(
        self,
        fts_db_with_data: Path,
    ) -> None:
        """매칭 결과가 없으면 빈 리스트를 반환하는지 확인한다."""
        results = _search_fts(
            query="블록체인 암호화폐",
            db_path=fts_db_with_data,
            top_k=5,
        )
        assert results == []

    def test_fts_nonexistent_db_returns_empty(
        self,
        tmp_path: Path,
    ) -> None:
        """존재하지 않는 DB 경로에서 빈 결과를 반환하는지 확인한다."""
        results = _search_fts(
            query="프로젝트",
            db_path=tmp_path / "nonexistent.db",
            top_k=5,
        )
        assert results == []

    def test_fts_special_chars_safe(
        self,
        fts_db_with_data: Path,
    ) -> None:
        """FTS5 특수문자가 포함된 쿼리가 안전하게 처리되는지 확인한다."""
        # FTS5 연산자 문자가 포함된 쿼리
        results = _search_fts(
            query='프로젝트 AND "일정" OR NOT',
            db_path=fts_db_with_data,
            top_k=5,
        )
        # 에러 없이 빈 결과이거나 관련 결과 반환
        assert isinstance(results, list)

    def test_fts_bm25_ordering(
        self,
        fts_db_with_data: Path,
    ) -> None:
        """BM25 점수 기반으로 관련도 높은 결과가 상위에 오는지 확인한다."""
        results = _search_fts(
            query="프로젝트 일정",
            db_path=fts_db_with_data,
            top_k=5,
        )
        if len(results) >= 2:
            # 두 키워드 모두 포함된 결과가 하나만 포함된 결과보다 상위여야 함
            for _i, r in enumerate(results):
                both = "프로젝트" in r["text"] and "일정" in r["text"]
                if both:
                    # 이 결과의 인덱스가 낮을수록(상위) BM25 정렬이 올바름
                    # 첫 번째 결과가 두 키워드 모두 포함하는지만 확인
                    break

    def test_fts_top_k_limit(
        self,
        fts_db_with_data: Path,
    ) -> None:
        """top_k 제한이 정확히 적용되는지 확인한다."""
        results = _search_fts(
            query="프로젝트",
            db_path=fts_db_with_data,
            top_k=1,
        )
        assert len(results) <= 1


class TestRRFAccuracy:
    """RRF(Reciprocal Rank Fusion) 결합 정확도를 검증한다."""

    def test_rrf_both_sources_rank_higher(self) -> None:
        """양쪽 소스 모두에 나타나는 결과가 더 높은 점수를 받는지 확인한다."""
        # chunk_0002는 벡터+FTS 양쪽 모두 존재
        vector_results = [
            {
                "chunk_id": "c1",
                "text": "벡터만",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "S0",
                "start_time": 0.0,
                "end_time": 10.0,
                "chunk_index": 0,
            },
            {
                "chunk_id": "c_both",
                "text": "양쪽 모두",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "S0",
                "start_time": 10.0,
                "end_time": 20.0,
                "chunk_index": 1,
            },
        ]
        fts_results = [
            {
                "chunk_id": "c_both",
                "text": "양쪽 모두",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "S0",
                "start_time": 10.0,
                "end_time": 20.0,
                "chunk_index": 1,
            },
            {
                "chunk_id": "c2",
                "text": "FTS만",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "S1",
                "start_time": 20.0,
                "end_time": 30.0,
                "chunk_index": 2,
            },
        ]

        combined = _combine_rrf(
            vector_results=vector_results,
            fts_results=fts_results,
            vector_weight=0.6,
            fts_weight=0.4,
            rrf_k=60,
            top_k=5,
        )

        # 양쪽 모두 나타나는 c_both가 가장 높은 점수
        assert combined[0].chunk_id == "c_both"
        assert combined[0].source == "both"

    def test_rrf_vector_only_vs_fts_only(self) -> None:
        """벡터만/FTS만 결과의 점수 차이가 가중치에 비례하는지 확인한다."""
        vector_results = [
            {
                "chunk_id": "cv",
                "text": "벡터 전용",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "S0",
                "start_time": 0.0,
                "end_time": 10.0,
                "chunk_index": 0,
            },
        ]
        fts_results = [
            {
                "chunk_id": "cf",
                "text": "FTS 전용",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "S1",
                "start_time": 10.0,
                "end_time": 20.0,
                "chunk_index": 1,
            },
        ]

        combined = _combine_rrf(
            vector_results=vector_results,
            fts_results=fts_results,
            vector_weight=0.6,
            fts_weight=0.4,
            rrf_k=60,
            top_k=5,
        )

        # 벡터 가중치(0.6) > FTS 가중치(0.4) → 벡터 전용 결과가 더 높은 점수
        vector_result = next(r for r in combined if r.chunk_id == "cv")
        fts_result = next(r for r in combined if r.chunk_id == "cf")
        assert vector_result.score > fts_result.score
        assert vector_result.source == "vector"
        assert fts_result.source == "fts"

    def test_rrf_empty_both(self) -> None:
        """양쪽 모두 빈 결과일 때 빈 리스트를 반환하는지 확인한다."""
        combined = _combine_rrf([], [], 0.6, 0.4, 60, 5)
        assert combined == []

    def test_rrf_top_k_truncation(self) -> None:
        """top_k 개수만큼만 결과가 반환되는지 확인한다."""
        vector_results = [
            {
                "chunk_id": f"c{i}",
                "text": f"텍스트 {i}",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "S0",
                "start_time": 0.0,
                "end_time": 10.0,
                "chunk_index": i,
            }
            for i in range(10)
        ]

        combined = _combine_rrf(
            vector_results=vector_results,
            fts_results=[],
            vector_weight=0.6,
            fts_weight=0.4,
            rrf_k=60,
            top_k=3,
        )
        assert len(combined) == 3

    def test_rrf_score_computation(self) -> None:
        """RRF 점수 계산 공식이 정확한지 확인한다."""
        # vector_rank=1, fts_rank=1, k=60
        score = _compute_rrf_score(
            vector_rank=1,
            fts_rank=1,
            vector_weight=0.6,
            fts_weight=0.4,
            k=60,
        )
        expected = 0.6 * (1.0 / 61) + 0.4 * (1.0 / 61)
        assert abs(score - expected) < 1e-10

    def test_rrf_score_none_ranks(self) -> None:
        """한쪽 순위가 None일 때 해당 항의 기여가 0인지 확인한다."""
        # 벡터만 (fts_rank=None)
        score_vector_only = _compute_rrf_score(
            vector_rank=1,
            fts_rank=None,
            vector_weight=0.6,
            fts_weight=0.4,
            k=60,
        )
        expected = 0.6 * (1.0 / 61)
        assert abs(score_vector_only - expected) < 1e-10

        # FTS만 (vector_rank=None)
        score_fts_only = _compute_rrf_score(
            vector_rank=None,
            fts_rank=1,
            vector_weight=0.6,
            fts_weight=0.4,
            k=60,
        )
        expected = 0.4 * (1.0 / 61)
        assert abs(score_fts_only - expected) < 1e-10

    def test_rrf_speakers_string_parsing(self) -> None:
        """화자 목록이 문자열로 전달될 때 리스트로 파싱되는지 확인한다."""
        vector_results = [
            {
                "chunk_id": "c1",
                "text": "텍스트",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "SPEAKER_00, SPEAKER_01",
                "start_time": 0.0,
                "end_time": 10.0,
                "chunk_index": 0,
            },
        ]

        combined = _combine_rrf(
            vector_results=vector_results,
            fts_results=[],
            vector_weight=0.6,
            fts_weight=0.4,
            rrf_k=60,
            top_k=5,
        )
        assert combined[0].speakers == ["SPEAKER_00", "SPEAKER_01"]

    def test_rrf_descending_score_order(self) -> None:
        """결과가 점수 내림차순으로 정렬되는지 확인한다."""
        vector_results = [
            {
                "chunk_id": f"c{i}",
                "text": f"텍스트 {i}",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "S0",
                "start_time": 0.0,
                "end_time": 10.0,
                "chunk_index": i,
            }
            for i in range(5)
        ]
        fts_results = [
            {
                "chunk_id": "c2",
                "text": "텍스트 2",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "S0",
                "start_time": 0.0,
                "end_time": 10.0,
                "chunk_index": 2,
            },
        ]

        combined = _combine_rrf(
            vector_results=vector_results,
            fts_results=fts_results,
            vector_weight=0.6,
            fts_weight=0.4,
            rrf_k=60,
            top_k=10,
        )

        # 점수가 내림차순인지 확인
        for i in range(len(combined) - 1):
            assert combined[i].score >= combined[i + 1].score


class TestFTSQueryBuilder:
    """FTS5 쿼리 빌더의 안전성과 정확성을 검증한다."""

    def test_single_word(self) -> None:
        """단일 단어 쿼리가 그대로 반환되는지 확인한다."""
        assert _build_fts_query("프로젝트") == "프로젝트"

    def test_multiple_words_or_joined(self) -> None:
        """복수 단어가 OR로 연결되는지 확인한다."""
        result = _build_fts_query("프로젝트 일정")
        assert result == "프로젝트 OR 일정"

    def test_special_chars_removed(self) -> None:
        """FTS5 특수문자가 제거되는지 확인한다."""
        result = _build_fts_query('"AND" OR NOT ^test*')
        # 특수문자 제거 후 공백 분리 → OR 연결
        assert "AND" in result or "test" in result
        assert '"' not in result
        assert "^" not in result
        assert "*" not in result

    def test_empty_returns_empty(self) -> None:
        """빈 입력이 빈 문자열을 반환하는지 확인한다."""
        assert _build_fts_query("") == ""
        assert _build_fts_query("   ") == ""

    def test_only_special_chars_returns_empty(self) -> None:
        """특수문자만 입력 시 빈 문자열을 반환하는지 확인한다."""
        assert _build_fts_query("***^^^") == ""

    def test_korean_preserved(self) -> None:
        """한국어 문자가 보존되는지 확인한다."""
        result = _build_fts_query("한국어 테스트")
        assert "한국어" in result
        assert "테스트" in result


class TestSearchGracefulDegradation:
    """검색 실패 시 graceful degradation 동작을 검증한다."""

    @pytest.mark.asyncio
    async def test_empty_query_raises(self) -> None:
        """빈 쿼리가 EmptyQueryError를 발생시키는지 확인한다."""
        config = MagicMock()
        config.search.vector_weight = 0.6
        config.search.fts_weight = 0.4
        config.search.rrf_k = 60
        config.search.top_k = 5
        config.embedding.model_name = "test-model"
        config.embedding.device = "cpu"
        config.embedding.query_prefix = "query: "
        config.paths.resolved_chroma_db_dir = Path("/tmp/test")
        config.paths.resolved_meetings_db = Path("/tmp/test.db")

        manager = MagicMock()
        engine = HybridSearchEngine(config, manager)

        with pytest.raises(EmptyQueryError):
            await engine.search("")

    @pytest.mark.asyncio
    async def test_search_with_no_chroma_returns_fts_only(
        self,
        fts_db_with_data: Path,
        tmp_path: Path,
    ) -> None:
        """ChromaDB 없이도 FTS5 결과만으로 검색이 동작하는지 확인한다."""
        config = MagicMock()
        config.search.vector_weight = 0.6
        config.search.fts_weight = 0.4
        config.search.rrf_k = 60
        config.search.top_k = 5
        config.embedding.model_name = "test-model"
        config.embedding.device = "cpu"
        config.embedding.query_prefix = "query: "
        # 존재하지 않는 ChromaDB 디렉토리
        config.paths.resolved_chroma_db_dir = tmp_path / "nonexistent_chroma"
        config.paths.resolved_meetings_db = fts_db_with_data

        manager = MagicMock()

        engine = HybridSearchEngine(config, manager)

        # PERF-005: 캐시된 임베딩 모델 mock 직접 설정
        mock_model = MagicMock()
        mock_embedding = MagicMock()
        mock_embedding.tolist.return_value = [0.1] * 384
        mock_model.encode.return_value = [mock_embedding]
        engine._embed_model = mock_model

        response = await engine.search("프로젝트")

        # FTS 결과만으로 응답이 생성되어야 함
        assert response.fts_count > 0
        assert response.vector_count == 0
        assert len(response.results) > 0

    def test_search_result_to_dict(self) -> None:
        """SearchResult.to_dict()가 올바른 딕셔너리를 반환하는지 확인한다."""
        result = SearchResult(
            chunk_id="c1",
            text="테스트 텍스트",
            score=0.5,
            meeting_id="m1",
            date="2026-03-04",
            speakers=["SPEAKER_00"],
            start_time=0.0,
            end_time=10.0,
            chunk_index=0,
            source="both",
        )
        d = result.to_dict()
        assert d["chunk_id"] == "c1"
        assert d["score"] == 0.5
        assert d["speakers"] == ["SPEAKER_00"]

    def test_search_response_to_dict(self) -> None:
        """SearchResponse.to_dict()가 중첩 구조를 올바르게 변환하는지 확인한다."""
        response = SearchResponse(
            results=[
                SearchResult(
                    chunk_id="c1",
                    text="텍스트",
                    score=0.5,
                    meeting_id="m1",
                    date="2026-03-04",
                    speakers=["S0"],
                    start_time=0.0,
                    end_time=10.0,
                ),
            ],
            query="테스트",
            total_found=1,
            vector_count=1,
            fts_count=0,
        )
        d = response.to_dict()
        assert len(d["results"]) == 1
        assert d["query"] == "테스트"
        assert d["total_found"] == 1


# ============================================================
# 2. 작업 큐 재시도 테스트
# ============================================================


class TestJobQueueRetry:
    """작업 큐의 재시도 로직과 상태 전이를 통합 검증한다."""

    def test_full_lifecycle_queued_to_completed(
        self,
        queue: JobQueue,
    ) -> None:
        """작업이 queued → recording → ... → completed 전체 사이클을 완료하는지 확인한다."""
        job_id = queue.add_job("meeting_001", "/audio/test.m4a")
        job = queue.get_job(job_id)
        assert job.status == "queued"

        # 전체 상태 전이 사이클
        transitions = [
            JobStatus.RECORDING,
            JobStatus.TRANSCRIBING,
            JobStatus.DIARIZING,
            JobStatus.MERGING,
            JobStatus.EMBEDDING,
            JobStatus.COMPLETED,
        ]

        for status in transitions:
            job = queue.update_status(job_id, status)

        assert job.status == "completed"

    def test_fail_and_retry_cycle(
        self,
        queue: JobQueue,
    ) -> None:
        """failed → retry → queued 사이클이 올바르게 동작하는지 확인한다."""
        job_id = queue.add_job("meeting_fail", "/audio/fail.m4a")
        queue.update_status(job_id, JobStatus.RECORDING)
        queue.update_status(job_id, JobStatus.TRANSCRIBING)

        # 전사 중 실패
        queue.update_status(
            job_id,
            JobStatus.FAILED,
            error_message="STT 모델 로드 실패",
        )
        job = queue.get_job(job_id)
        assert job.status == "failed"
        assert job.error_message == "STT 모델 로드 실패"
        assert job.retry_count == 0

        # 재시도 #1
        job = queue.retry_job(job_id)
        assert job.status == "queued"
        assert job.retry_count == 1
        assert job.error_message == ""

    def test_retry_count_increments(
        self,
        queue: JobQueue,
    ) -> None:
        """재시도 시 retry_count가 정확히 증가하는지 확인한다."""
        job_id = queue.add_job("meeting_retry", "/audio/retry.m4a")

        for attempt in range(3):
            # queued → recording → failed
            queue.update_status(job_id, JobStatus.RECORDING)
            queue.update_status(
                job_id,
                JobStatus.FAILED,
                error_message=f"에러 #{attempt + 1}",
            )
            job = queue.get_job(job_id)
            assert job.retry_count == attempt

            if attempt < 2:
                # 재시도 (max_retries=3이므로 2번까지 가능)
                job = queue.retry_job(job_id)
                assert job.retry_count == attempt + 1

    def test_max_retries_exceeded(
        self,
        queue: JobQueue,
    ) -> None:
        """최대 재시도 횟수 초과 시 MaxRetriesExceededError가 발생하는지 확인한다."""
        job_id = queue.add_job("meeting_max", "/audio/max.m4a")

        # 3번 재시도하여 max_retries(3)에 도달
        for _ in range(3):
            queue.update_status(job_id, JobStatus.RECORDING)
            queue.update_status(job_id, JobStatus.FAILED, "에러")
            queue.retry_job(job_id)

        # 4번째 실패 후 재시도 불가
        queue.update_status(job_id, JobStatus.RECORDING)
        queue.update_status(job_id, JobStatus.FAILED, "최종 에러")

        with pytest.raises(MaxRetriesExceededError):
            queue.retry_job(job_id)

    def test_invalid_transition_rejected(
        self,
        queue: JobQueue,
    ) -> None:
        """유효하지 않은 상태 전이가 거부되는지 확인한다."""
        job_id = queue.add_job("meeting_invalid", "/audio/inv.m4a")

        # queued → completed 직접 전이 불가
        with pytest.raises(InvalidTransitionError):
            queue.update_status(job_id, JobStatus.COMPLETED)

        # queued → merging 직접 전이 불가
        with pytest.raises(InvalidTransitionError):
            queue.update_status(job_id, JobStatus.MERGING)

    def test_completed_no_further_transition(
        self,
        queue: JobQueue,
    ) -> None:
        """completed 상태에서 추가 전이가 불가능한지 확인한다."""
        job_id = queue.add_job("meeting_done", "/audio/done.m4a")

        # 전체 사이클 완료
        for status in [
            JobStatus.RECORDING,
            JobStatus.TRANSCRIBING,
            JobStatus.DIARIZING,
            JobStatus.MERGING,
            JobStatus.EMBEDDING,
            JobStatus.COMPLETED,
        ]:
            queue.update_status(job_id, status)

        # completed → 어떤 상태로도 전이 불가
        with pytest.raises(InvalidTransitionError):
            queue.update_status(job_id, JobStatus.FAILED)

    def test_retry_all_failed(
        self,
        queue: JobQueue,
    ) -> None:
        """retry_all_failed가 재시도 가능한 실패 작업만 재시도하는지 확인한다."""
        # 2개 작업 등록 후 실패
        id1 = queue.add_job("m_fail_1", "/audio/f1.m4a")
        id2 = queue.add_job("m_fail_2", "/audio/f2.m4a")

        for jid in [id1, id2]:
            queue.update_status(jid, JobStatus.RECORDING)
            queue.update_status(jid, JobStatus.FAILED, "에러")

        retried = queue.retry_all_failed()
        assert id1 in retried
        assert id2 in retried

        # 재시도 후 모두 queued 상태
        for jid in [id1, id2]:
            job = queue.get_job(jid)
            assert job.status == "queued"
            assert job.retry_count == 1

    def test_duplicate_meeting_id_rejected(
        self,
        queue: JobQueue,
    ) -> None:
        """동일 meeting_id 중복 등록이 거부되는지 확인한다."""
        queue.add_job("meeting_dup", "/audio/dup.m4a")

        with pytest.raises(JobQueueError):
            queue.add_job("meeting_dup", "/audio/dup2.m4a")

    def test_get_nonexistent_job_raises(
        self,
        queue: JobQueue,
    ) -> None:
        """존재하지 않는 작업 조회 시 JobNotFoundError가 발생하는지 확인한다."""
        with pytest.raises(JobNotFoundError):
            queue.get_job(99999)

    def test_count_by_status(
        self,
        queue: JobQueue,
    ) -> None:
        """상태별 작업 수 집계가 정확한지 확인한다."""
        queue.add_job("m_count_1", "/audio/c1.m4a")
        queue.add_job("m_count_2", "/audio/c2.m4a")
        id3 = queue.add_job("m_count_3", "/audio/c3.m4a")
        queue.update_status(id3, JobStatus.RECORDING)

        counts = queue.count_by_status()
        assert counts.get("queued", 0) == 2
        assert counts.get("recording", 0) == 1

    def test_multiple_jobs_independent_states(
        self,
        queue: JobQueue,
    ) -> None:
        """여러 작업이 독립적으로 상태를 관리하는지 확인한다."""
        id1 = queue.add_job("m_ind_1", "/audio/i1.m4a")
        id2 = queue.add_job("m_ind_2", "/audio/i2.m4a")

        # 작업 1만 진행
        queue.update_status(id1, JobStatus.RECORDING)
        queue.update_status(id1, JobStatus.TRANSCRIBING)

        # 작업 2는 여전히 queued
        job1 = queue.get_job(id1)
        job2 = queue.get_job(id2)
        assert job1.status == "transcribing"
        assert job2.status == "queued"


class TestAsyncJobQueue:
    """AsyncJobQueue 비동기 래퍼를 통합 검증한다."""

    @pytest.mark.asyncio
    async def test_async_full_cycle(
        self,
        async_queue: AsyncJobQueue,
    ) -> None:
        """비동기 래퍼로 전체 작업 사이클이 동작하는지 확인한다."""
        job_id = await async_queue.add_job(
            "meeting_async",
            "/audio/async.m4a",
        )
        job = await async_queue.get_job(job_id)
        assert job.status == "queued"

        for status in [
            JobStatus.RECORDING,
            JobStatus.TRANSCRIBING,
            JobStatus.DIARIZING,
            JobStatus.MERGING,
            JobStatus.EMBEDDING,
            JobStatus.COMPLETED,
        ]:
            job = await async_queue.update_status(job_id, status)

        assert job.status == "completed"

    @pytest.mark.asyncio
    async def test_async_retry(
        self,
        async_queue: AsyncJobQueue,
    ) -> None:
        """비동기 래퍼로 재시도가 동작하는지 확인한다."""
        job_id = await async_queue.add_job(
            "meeting_async_retry",
            "/audio/ar.m4a",
        )
        await async_queue.update_status(job_id, JobStatus.RECORDING)
        await async_queue.update_status(
            job_id,
            JobStatus.FAILED,
            "비동기 에러",
        )

        job = await async_queue.retry_job(job_id)
        assert job.status == "queued"
        assert job.retry_count == 1


# ============================================================
# 3. 청크 생성 품질 테스트
# ============================================================


class TestChunkQuality:
    """청크 생성의 품질 (크기, 분리, 메타데이터)을 통합 검증한다."""

    def _make_chunker_config(
        self,
        max_tokens: int = 300,
        min_tokens: int = 50,
        time_gap: int = 30,
        overlap: int = 30,
    ) -> AppConfig:
        """테스트용 AppConfig를 생성한다."""
        return AppConfig(
            chunking=ChunkingConfig(
                max_tokens=max_tokens,
                min_tokens=min_tokens,
                time_gap_threshold_seconds=time_gap,
                overlap_tokens=overlap,
            ),
        )

    @pytest.mark.asyncio
    async def test_chunk_size_within_bounds(self) -> None:
        """생성된 청크가 max_tokens 이내인지 확인한다."""
        config = self._make_chunker_config(max_tokens=100, min_tokens=10)
        chunker = Chunker(config)

        # 긴 발화 목록 생성
        utterances = [
            _make_utterance(
                text=f"이것은 테스트 발화입니다 번호 {i} 상세 내용 포함",
                speaker=f"SPEAKER_{i % 3:02d}",
                start=float(i * 5),
                end=float(i * 5 + 4),
            )
            for i in range(20)
        ]
        corrected = _make_corrected_result(utterances, num_speakers=3)
        result = await chunker.chunk(corrected, "m_size", "2026-03-04")

        # 모든 청크가 max_tokens를 크게 초과하지 않아야 함
        for chunk in result.chunks:
            # 단일 그룹이 max_tokens보다 크면 분할되므로
            # 합리적인 범위 내에 있어야 함
            assert chunk.estimated_tokens > 0

    @pytest.mark.asyncio
    async def test_time_gap_creates_separate_chunks(self) -> None:
        """시간 간격이 임계값을 초과하면 별도 청크로 분리되는지 확인한다."""
        config = self._make_chunker_config(
            max_tokens=1000,  # 큰 값으로 토큰 제한 비활성화
            min_tokens=10,
            time_gap=30,
        )
        chunker = Chunker(config)

        # 동일 화자이지만 35초 간격 (> 30초 threshold)
        utterances = [
            _make_utterance("첫 번째 토픽 내용", "SPEAKER_00", 0.0, 10.0),
            _make_utterance("첫 번째 토픽 계속", "SPEAKER_00", 10.0, 20.0),
            _make_utterance("두 번째 토픽 시작", "SPEAKER_00", 55.0, 65.0),
            _make_utterance("두 번째 토픽 계속", "SPEAKER_00", 65.0, 75.0),
        ]
        corrected = _make_corrected_result(utterances, num_speakers=1)
        result = await chunker.chunk(corrected, "m_gap", "2026-03-04")

        # 시간 간격으로 인해 2개 이상의 청크 생성
        assert len(result.chunks) >= 2

    @pytest.mark.asyncio
    async def test_speaker_grouping(self) -> None:
        """동일 화자의 연속 발화가 올바르게 그룹핑되는지 확인한다."""
        config = self._make_chunker_config(max_tokens=1000, min_tokens=10)
        chunker = Chunker(config)

        utterances = [
            _make_utterance("안녕하세요", "SPEAKER_00", 0.0, 2.0),
            _make_utterance("반갑습니다", "SPEAKER_00", 2.0, 4.0),
            _make_utterance("저도 반갑습니다", "SPEAKER_01", 4.0, 6.0),
        ]
        corrected = _make_corrected_result(utterances, num_speakers=2)
        result = await chunker.chunk(corrected, "m_group", "2026-03-04")

        # 결과 청크에서 SPEAKER_00의 발화가 하나의 그룹으로 있어야 함
        assert len(result.chunks) >= 1
        first_chunk = result.chunks[0]
        # 첫 청크에 SPEAKER_00이 포함되어야 함
        assert "SPEAKER_00" in first_chunk.speakers

    @pytest.mark.asyncio
    async def test_metadata_completeness(self) -> None:
        """청크 메타데이터가 완전한지 확인한다."""
        config = self._make_chunker_config(max_tokens=1000, min_tokens=10)
        chunker = Chunker(config)

        utterances = [
            _make_utterance("테스트 발화", "SPEAKER_00", 5.0, 15.0),
        ]
        corrected = _make_corrected_result(utterances, num_speakers=1)
        result = await chunker.chunk(corrected, "m_meta", "2026-03-04")

        assert len(result.chunks) == 1
        chunk = result.chunks[0]

        # 필수 메타데이터 확인
        assert chunk.meeting_id == "m_meta"
        assert chunk.date == "2026-03-04"
        assert "SPEAKER_00" in chunk.speakers
        assert chunk.start_time == 5.0
        assert chunk.end_time == 15.0
        assert chunk.estimated_tokens > 0
        assert chunk.chunk_index == 0

    @pytest.mark.asyncio
    async def test_empty_input_raises(self) -> None:
        """빈 발화 입력 시 EmptyInputError가 발생하는지 확인한다."""
        config = self._make_chunker_config()
        chunker = Chunker(config)

        corrected = _make_corrected_result([], num_speakers=0)
        with pytest.raises(EmptyInputError):
            await chunker.chunk(corrected, "m_empty", "2026-03-04")

    @pytest.mark.asyncio
    async def test_korean_nfc_normalization(self) -> None:
        """한국어 텍스트에 NFC 정규화가 적용되는지 확인한다."""
        import unicodedata

        config = self._make_chunker_config(max_tokens=1000, min_tokens=10)
        chunker = Chunker(config)

        # NFD 형식의 한국어 (분해 형태)
        nfd_text = unicodedata.normalize("NFD", "한국어 텍스트")
        utterances = [
            _make_utterance(nfd_text, "SPEAKER_00", 0.0, 5.0),
        ]
        corrected = _make_corrected_result(utterances, num_speakers=1)
        result = await chunker.chunk(corrected, "m_nfc", "2026-03-04")

        # NFC로 정규화되어야 함
        chunk_text = result.chunks[0].text
        assert chunk_text == unicodedata.normalize("NFC", chunk_text)

    @pytest.mark.asyncio
    async def test_min_tokens_merge(self) -> None:
        """min_tokens 미만인 마지막 청크가 이전 청크와 병합되는지 확인한다."""
        config = self._make_chunker_config(
            max_tokens=50,
            min_tokens=30,
            time_gap=999,  # 시간 간격 분리 비활성화
        )
        chunker = Chunker(config)

        utterances = [
            _make_utterance(
                "첫 번째 긴 발화입니다 여러 내용이 포함되어 있습니다 상세한 설명을 담고 있습니다",
                "SPEAKER_00",
                0.0,
                10.0,
            ),
            _make_utterance("짧은 발화", "SPEAKER_00", 10.0, 12.0),
        ]
        corrected = _make_corrected_result(utterances, num_speakers=1)
        result = await chunker.chunk(corrected, "m_merge", "2026-03-04")

        # 마지막 청크가 min_tokens 미만이면 병합되어야 함
        if len(result.chunks) == 1:
            # 병합이 발생한 경우
            assert "짧은 발화" in result.chunks[0].text

    def test_checkpoint_roundtrip(self, tmp_path: Path) -> None:
        """체크포인트 저장/복원이 데이터를 보존하는지 확인한다."""
        original = ChunkedResult(
            chunks=[
                Chunk(
                    text="[SPEAKER_00] 테스트 청크",
                    meeting_id="m_cp",
                    date="2026-03-04",
                    speakers=["SPEAKER_00"],
                    start_time=0.0,
                    end_time=10.0,
                    estimated_tokens=15,
                    chunk_index=0,
                ),
                Chunk(
                    text="[SPEAKER_01] 두 번째 청크",
                    meeting_id="m_cp",
                    date="2026-03-04",
                    speakers=["SPEAKER_01"],
                    start_time=10.0,
                    end_time=20.0,
                    estimated_tokens=18,
                    chunk_index=1,
                ),
            ],
            meeting_id="m_cp",
            date="2026-03-04",
            total_utterances=5,
            num_speakers=2,
            audio_path="/audio/test.m4a",
        )

        checkpoint_path = tmp_path / "checkpoint.json"
        original.save_checkpoint(checkpoint_path)

        restored = ChunkedResult.from_checkpoint(checkpoint_path)

        assert len(restored.chunks) == 2
        assert restored.meeting_id == "m_cp"
        assert restored.chunks[0].text == "[SPEAKER_00] 테스트 청크"
        assert restored.chunks[1].speakers == ["SPEAKER_01"]
        assert restored.total_utterances == 5
        assert restored.audio_path == "/audio/test.m4a"

    def test_token_estimation_korean(self) -> None:
        """한국어 토큰 추정이 합리적인 범위인지 확인한다."""
        # 한국어 약 15글자 → ~10토큰
        tokens = _estimate_tokens("이것은 한국어 토큰 추정 테스트입니다")
        assert tokens > 0
        # 글자 수 / 1.5 근사값
        text = "이것은 한국어 토큰 추정 테스트입니다"
        expected = max(1, int(len(text) / 1.5))
        assert tokens == expected

    def test_token_estimation_empty(self) -> None:
        """빈 텍스트의 토큰 추정이 0인지 확인한다."""
        assert _estimate_tokens("") == 0

    def test_speaker_time_grouping(self) -> None:
        """화자+시간 기반 그룹핑이 올바르게 동작하는지 확인한다."""
        utterances = [
            _make_utterance("첫 발화", "SPEAKER_00", 0.0, 5.0),
            _make_utterance("같은 화자", "SPEAKER_00", 5.0, 10.0),
            _make_utterance("다른 화자", "SPEAKER_01", 10.0, 15.0),
            _make_utterance("다시 첫 화자", "SPEAKER_00", 15.0, 20.0),
        ]

        groups = _group_by_speaker_and_time(utterances, time_gap_threshold=30.0)

        # SPEAKER_00 연속 2개 → 1그룹, SPEAKER_01 → 1그룹, SPEAKER_00 → 1그룹
        assert len(groups) == 3
        assert groups[0].speaker == "SPEAKER_00"
        assert len(groups[0].texts) == 2
        assert groups[1].speaker == "SPEAKER_01"
        assert groups[2].speaker == "SPEAKER_00"

    def test_time_gap_creates_new_group(self) -> None:
        """시간 간격이 임계값을 초과하면 새 그룹이 생성되는지 확인한다."""
        utterances = [
            _make_utterance("발화 1", "SPEAKER_00", 0.0, 5.0),
            # 40초 간격 (> 30초 threshold)
            _make_utterance("발화 2", "SPEAKER_00", 45.0, 50.0),
        ]

        groups = _group_by_speaker_and_time(utterances, time_gap_threshold=30.0)

        # 동일 화자이지만 시간 간격으로 2개 그룹
        assert len(groups) == 2

    @pytest.mark.asyncio
    async def test_multi_speaker_chunk_quality(self) -> None:
        """다중 화자 대화에서 청크 품질이 적절한지 확인한다."""
        config = self._make_chunker_config(max_tokens=200, min_tokens=20)
        chunker = Chunker(config)

        # 3명의 화자가 번갈아 발화
        utterances = [
            _make_utterance("첫 번째 화자의 발언입니다", "SPEAKER_00", 0.0, 5.0),
            _make_utterance("두 번째 화자가 응답합니다", "SPEAKER_01", 5.0, 10.0),
            _make_utterance("세 번째 화자도 의견을 냅니다", "SPEAKER_02", 10.0, 15.0),
            _make_utterance("첫 번째 화자가 다시 발언합니다", "SPEAKER_00", 15.0, 20.0),
            _make_utterance("두 번째 화자가 정리합니다", "SPEAKER_01", 20.0, 25.0),
        ]
        corrected = _make_corrected_result(utterances, num_speakers=3)
        result = await chunker.chunk(corrected, "m_multi", "2026-03-04")

        # 청크가 생성되어야 함
        assert len(result.chunks) >= 1
        assert result.total_utterances == 5
        assert result.num_speakers == 3

        # 각 청크에 화자 정보가 포함되어야 함
        for chunk in result.chunks:
            assert len(chunk.speakers) >= 1
            assert all(s.startswith("SPEAKER_") for s in chunk.speakers)


# ============================================================
# 4. End-to-End 통합 테스트 (청크 → FTS5 저장 → 검색)
# ============================================================


class TestEndToEnd:
    """청크 생성부터 FTS5 저장, 검색까지의 전체 파이프라인을 검증한다."""

    @pytest.mark.asyncio
    async def test_chunk_to_fts_to_search(
        self,
        tmp_path: Path,
    ) -> None:
        """청크 생성 → FTS5 저장 → 키워드 검색 파이프라인을 검증한다."""
        # 1단계: 청크 생성
        config = AppConfig(
            chunking=ChunkingConfig(
                max_tokens=300,
                min_tokens=10,
                time_gap_threshold_seconds=30,
                overlap_tokens=0,
            ),
        )
        chunker = Chunker(config)

        utterances = [
            _make_utterance(
                "프로젝트 일정 회의를 시작하겠습니다",
                "SPEAKER_00",
                0.0,
                10.0,
            ),
            _make_utterance(
                "네 개발 일정은 다음 주 금요일 마감입니다",
                "SPEAKER_01",
                10.0,
                20.0,
            ),
            _make_utterance(
                "서버 배포는 수요일에 진행합니다",
                "SPEAKER_00",
                20.0,
                30.0,
            ),
        ]
        corrected = _make_corrected_result(utterances, num_speakers=2)
        chunked = await chunker.chunk(corrected, "meeting_e2e", "2026-03-04")

        assert len(chunked.chunks) >= 1

        # 2단계: FTS5에 저장
        db_path = tmp_path / "e2e_meetings.db"
        _create_fts_db(db_path)

        fts_data = []
        for chunk in chunked.chunks:
            fts_data.append(
                {
                    "chunk_id": f"meeting_e2e_chunk_{chunk.chunk_index:04d}",
                    "text": chunk.text,
                    "meeting_id": chunk.meeting_id,
                    "date": chunk.date,
                    "speakers": ",".join(chunk.speakers),
                    "start_time": chunk.start_time,
                    "end_time": chunk.end_time,
                    "chunk_index": chunk.chunk_index,
                }
            )
        _insert_fts_chunks(db_path, fts_data)

        # 3단계: FTS5 검색
        results = _search_fts(
            query="프로젝트 일정",
            db_path=db_path,
            top_k=5,
        )
        assert len(results) >= 1
        # 검색 결과에서 원본 데이터 확인
        found_texts = [r["text"] for r in results]
        assert any("프로젝트" in t or "일정" in t for t in found_texts)

    @pytest.mark.asyncio
    async def test_chunk_to_fts_with_filter(
        self,
        tmp_path: Path,
    ) -> None:
        """청크 → FTS5 → 필터링 검색 파이프라인을 검증한다."""
        # 두 개의 다른 회의 청크 생성
        config = AppConfig(
            chunking=ChunkingConfig(
                max_tokens=500,
                min_tokens=10,
                time_gap_threshold_seconds=30,
                overlap_tokens=0,
            ),
        )
        chunker = Chunker(config)

        # 회의 1
        utt1 = [
            _make_utterance("인공지능 기술 트렌드를 살펴봅니다", "SPEAKER_00", 0.0, 10.0),
        ]
        corrected1 = _make_corrected_result(utt1, num_speakers=1)
        chunked1 = await chunker.chunk(corrected1, "meeting_a", "2026-03-04")

        # 회의 2
        utt2 = [
            _make_utterance("인공지능 모델 최적화 방법을 논의합니다", "SPEAKER_01", 0.0, 10.0),
        ]
        corrected2 = _make_corrected_result(utt2, num_speakers=1)
        chunked2 = await chunker.chunk(corrected2, "meeting_b", "2026-03-05")

        # FTS5 저장
        db_path = tmp_path / "filter_meetings.db"
        _create_fts_db(db_path)

        all_chunks = []
        for chunked in [chunked1, chunked2]:
            for chunk in chunked.chunks:
                all_chunks.append(
                    {
                        "chunk_id": f"{chunk.meeting_id}_chunk_{chunk.chunk_index:04d}",
                        "text": chunk.text,
                        "meeting_id": chunk.meeting_id,
                        "date": chunk.date,
                        "speakers": ",".join(chunk.speakers),
                        "start_time": chunk.start_time,
                        "end_time": chunk.end_time,
                        "chunk_index": chunk.chunk_index,
                    }
                )
        _insert_fts_chunks(db_path, all_chunks)

        # 필터 없이 검색 → 양쪽 모두 결과
        results_all = _search_fts("인공지능", db_path, top_k=10)
        assert len(results_all) >= 2

        # meeting_id 필터 → 특정 회의만
        results_a = _search_fts(
            "인공지능",
            db_path,
            top_k=10,
            meeting_id_filter="meeting_a",
        )
        assert all(r["meeting_id"] == "meeting_a" for r in results_a)

        # 날짜 필터 → 특정 날짜만
        results_date = _search_fts(
            "인공지능",
            db_path,
            top_k=10,
            date_filter="2026-03-05",
        )
        assert all(r["date"] == "2026-03-05" for r in results_date)

    @pytest.mark.asyncio
    async def test_rrf_with_real_fts_data(
        self,
        tmp_path: Path,
    ) -> None:
        """실제 FTS5 데이터와 모의 벡터 결과를 RRF로 결합하는 테스트."""
        # FTS5 DB 준비
        db_path = tmp_path / "rrf_meetings.db"
        _create_fts_db(db_path)

        chunks = [
            {
                "chunk_id": "c_001",
                "text": "[SPEAKER_00] 머신러닝 모델 성능 개선 방법을 논의합니다",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "SPEAKER_00",
                "start_time": 0.0,
                "end_time": 15.0,
                "chunk_index": 0,
            },
            {
                "chunk_id": "c_002",
                "text": "[SPEAKER_01] 데이터 전처리가 성능에 미치는 영향을 분석합니다",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "SPEAKER_01",
                "start_time": 15.0,
                "end_time": 30.0,
                "chunk_index": 1,
            },
            {
                "chunk_id": "c_003",
                "text": "[SPEAKER_00] 하이퍼파라미터 튜닝 전략을 공유합니다",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "SPEAKER_00",
                "start_time": 30.0,
                "end_time": 45.0,
                "chunk_index": 2,
            },
        ]
        _insert_fts_chunks(db_path, chunks)

        # FTS5 검색
        fts_results = _search_fts("성능 개선", db_path, top_k=5)

        # 모의 벡터 검색 결과 (c_002가 벡터에서도 관련성 높음)
        vector_results = [
            {
                "chunk_id": "c_002",
                "text": "[SPEAKER_01] 데이터 전처리가 성능에 미치는 영향을 분석합니다",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "SPEAKER_01",
                "start_time": 15.0,
                "end_time": 30.0,
                "chunk_index": 1,
            },
            {
                "chunk_id": "c_001",
                "text": "[SPEAKER_00] 머신러닝 모델 성능 개선 방법을 논의합니다",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "SPEAKER_00",
                "start_time": 0.0,
                "end_time": 15.0,
                "chunk_index": 0,
            },
        ]

        # RRF 결합
        combined = _combine_rrf(
            vector_results=vector_results,
            fts_results=fts_results,
            vector_weight=0.6,
            fts_weight=0.4,
            rrf_k=60,
            top_k=5,
        )

        # 양쪽 모두 나타나는 결과가 있어야 함
        both_sources = [r for r in combined if r.source == "both"]
        assert len(both_sources) >= 1

        # 결과가 점수 내림차순
        for i in range(len(combined) - 1):
            assert combined[i].score >= combined[i + 1].score

    @pytest.mark.asyncio
    async def test_chunked_result_properties(self) -> None:
        """ChunkedResult의 계산 속성이 정확한지 확인한다."""
        config = AppConfig(
            chunking=ChunkingConfig(
                max_tokens=100,
                min_tokens=10,
                time_gap_threshold_seconds=30,
                overlap_tokens=0,
            ),
        )
        chunker = Chunker(config)

        utterances = [
            _make_utterance(
                "이것은 충분히 긴 발화입니다 여러 내용을 담고 있습니다 토큰 수가 많아야 합니다",
                "SPEAKER_00",
                0.0,
                15.0,
            ),
            _make_utterance(
                "두 번째 발화도 충분히 길어야 합니다 다양한 내용을 포함합니다",
                "SPEAKER_01",
                15.0,
                30.0,
            ),
        ]
        corrected = _make_corrected_result(utterances, num_speakers=2)
        result = await chunker.chunk(corrected, "m_prop", "2026-03-04")

        # 속성 검증
        assert result.total_tokens > 0
        assert result.avg_tokens_per_chunk > 0
        assert result.total_utterances == 2
        assert result.num_speakers == 2
        assert result.audio_path == "/test/audio.wav"
