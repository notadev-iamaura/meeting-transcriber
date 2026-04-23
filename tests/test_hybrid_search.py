"""
하이브리드 검색 엔진 테스트 (Hybrid Search Engine Tests)

목적: HybridSearchEngine의 벡터 + FTS5 + RRF 결합 검색 로직을 검증한다.
주요 테스트:
    - RRF 점수 계산 정확성
    - RRF 결합 로직 (양쪽 결과 병합, 점수 내림차순 정렬)
    - FTS5 쿼리 빌더 (특수문자 제거, 한국어 지원)
    - 벡터 검색 (ChromaDB mock)
    - FTS5 검색 (실제 SQLite in-memory)
    - 하이브리드 검색 통합 (양쪽 + 한쪽만 + 빈 결과)
    - 날짜/화자/회의 필터링
    - 에러 처리 (빈 쿼리, 모델 로드 실패, graceful degradation)
    - 한국어 NFC 정규화
의존성: pytest, pytest-asyncio
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from search.hybrid_search import (
    EmptyQueryError,
    HybridSearchEngine,
    ModelLoadError,
    SearchError,
    SearchResponse,
    SearchResult,
    _build_fts_query,
    _combine_rrf,
    _compute_rrf_score,
    _search_fts,
)

# === 헬퍼 함수 ===


def _make_config(
    vector_weight: float = 0.6,
    fts_weight: float = 0.4,
    rrf_k: int = 60,
    top_k: int = 5,
) -> MagicMock:
    """테스트용 AppConfig mock을 생성한다."""
    config = MagicMock()
    config.search.vector_weight = vector_weight
    config.search.fts_weight = fts_weight
    config.search.rrf_k = rrf_k
    config.search.top_k = top_k
    config.embedding.model_name = "intfloat/multilingual-e5-small"
    config.embedding.device = "cpu"
    config.embedding.query_prefix = "query: "
    config.embedding.passage_prefix = "passage: "
    config.paths.resolved_chroma_db_dir = Path("/tmp/test_chroma")
    config.paths.resolved_meetings_db = Path("/tmp/test_meetings.db")
    return config


def _make_model_manager() -> MagicMock:
    """테스트용 ModelLoadManager mock을 생성한다."""
    manager = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=MagicMock())
    ctx.__aexit__ = AsyncMock(return_value=False)
    manager.acquire.return_value = ctx
    return manager


def _make_vector_results(n: int = 5) -> list[dict[str, Any]]:
    """테스트용 벡터 검색 결과를 생성한다."""
    return [
        {
            "chunk_id": f"meeting_001_chunk_{i:04d}",
            "text": f"벡터 검색 결과 텍스트 {i}",
            "meeting_id": "meeting_001",
            "date": "2026-03-04",
            "speakers": "SPEAKER_00,SPEAKER_01",
            "start_time": float(i * 30),
            "end_time": float(i * 30 + 25),
            "chunk_index": i,
        }
        for i in range(n)
    ]


def _make_fts_results(n: int = 5, offset: int = 2) -> list[dict[str, Any]]:
    """테스트용 FTS 검색 결과를 생성한다.

    offset으로 벡터 결과와 부분 겹침을 만든다.
    """
    return [
        {
            "chunk_id": f"meeting_001_chunk_{i + offset:04d}",
            "text": f"FTS 검색 결과 텍스트 {i + offset}",
            "meeting_id": "meeting_001",
            "date": "2026-03-04",
            "speakers": "SPEAKER_00",
            "start_time": float((i + offset) * 30),
            "end_time": float((i + offset) * 30 + 25),
            "chunk_index": i + offset,
        }
        for i in range(n)
    ]


# === RRF 점수 계산 테스트 ===


class TestComputeRrfScore:
    """RRF 점수 계산 함수 테스트."""

    def test_양쪽_모두_있는_경우(self) -> None:
        """벡터와 FTS 모두 결과가 있을 때 점수를 계산한다."""
        score = _compute_rrf_score(
            vector_rank=1,
            fts_rank=2,
            vector_weight=0.6,
            fts_weight=0.4,
            k=60,
        )
        # 0.6 * 1/(60+1) + 0.4 * 1/(60+2) = 0.6/61 + 0.4/62
        expected = 0.6 / 61 + 0.4 / 62
        assert abs(score - expected) < 1e-10

    def test_벡터만_있는_경우(self) -> None:
        """벡터 결과만 있을 때 FTS 기여는 0이다."""
        score = _compute_rrf_score(
            vector_rank=1,
            fts_rank=None,
            vector_weight=0.6,
            fts_weight=0.4,
            k=60,
        )
        expected = 0.6 / 61
        assert abs(score - expected) < 1e-10

    def test_fts만_있는_경우(self) -> None:
        """FTS 결과만 있을 때 벡터 기여는 0이다."""
        score = _compute_rrf_score(
            vector_rank=None,
            fts_rank=1,
            vector_weight=0.6,
            fts_weight=0.4,
            k=60,
        )
        expected = 0.4 / 61
        assert abs(score - expected) < 1e-10

    def test_양쪽_모두_없는_경우(self) -> None:
        """양쪽 모두 없으면 점수는 0이다."""
        score = _compute_rrf_score(
            vector_rank=None,
            fts_rank=None,
            vector_weight=0.6,
            fts_weight=0.4,
            k=60,
        )
        assert score == 0.0

    def test_순위가_높을수록_점수_높음(self) -> None:
        """순위 1이 순위 10보다 점수가 높다."""
        score_rank1 = _compute_rrf_score(1, None, 0.6, 0.4, 60)
        score_rank10 = _compute_rrf_score(10, None, 0.6, 0.4, 60)
        assert score_rank1 > score_rank10

    def test_k값_조정_효과(self) -> None:
        """k값이 클수록 순위 차이의 영향이 줄어든다."""
        # k=10일 때 순위 1과 2의 차이
        score1_k10 = _compute_rrf_score(1, None, 1.0, 0, 10)
        score2_k10 = _compute_rrf_score(2, None, 1.0, 0, 10)
        diff_k10 = score1_k10 - score2_k10

        # k=100일 때 순위 1과 2의 차이
        score1_k100 = _compute_rrf_score(1, None, 1.0, 0, 100)
        score2_k100 = _compute_rrf_score(2, None, 1.0, 0, 100)
        diff_k100 = score1_k100 - score2_k100

        # k가 클수록 차이가 줄어듦
        assert diff_k10 > diff_k100


# === RRF 결합 테스트 ===


class TestCombineRrf:
    """RRF 결합 로직 테스트."""

    def test_양쪽_결과_결합(self) -> None:
        """벡터 + FTS 결과를 결합하고 점수 내림차순으로 정렬한다."""
        vector = _make_vector_results(3)
        fts = _make_fts_results(3, offset=2)

        results = _combine_rrf(vector, fts, 0.6, 0.4, 60, 10)

        assert len(results) > 0
        # 점수 내림차순 검증
        for i in range(len(results) - 1):
            assert results[i].score >= results[i + 1].score

    def test_겹치는_결과_병합(self) -> None:
        """동일 chunk_id가 양쪽에 있으면 both로 표시된다."""
        vector = [_make_vector_results(1)[0]]
        fts = [_make_fts_results(1, offset=0)[0]]  # 동일 chunk_id

        results = _combine_rrf(vector, fts, 0.6, 0.4, 60, 10)

        assert len(results) == 1
        assert results[0].source == "both"

    def test_벡터만_있는_결과(self) -> None:
        """벡터에만 있는 결과의 source는 vector이다."""
        vector = _make_vector_results(1)
        fts: list[dict[str, Any]] = []

        results = _combine_rrf(vector, fts, 0.6, 0.4, 60, 10)

        assert len(results) == 1
        assert results[0].source == "vector"

    def test_fts만_있는_결과(self) -> None:
        """FTS에만 있는 결과의 source는 fts이다."""
        vector: list[dict[str, Any]] = []
        fts = _make_fts_results(1)

        results = _combine_rrf(vector, fts, 0.6, 0.4, 60, 10)

        assert len(results) == 1
        assert results[0].source == "fts"

    def test_top_k_절단(self) -> None:
        """결과가 top_k를 초과하면 절단한다."""
        vector = _make_vector_results(5)
        fts = _make_fts_results(5, offset=5)  # 겹침 없음

        results = _combine_rrf(vector, fts, 0.6, 0.4, 60, 3)

        assert len(results) == 3

    def test_빈_결과(self) -> None:
        """양쪽 모두 빈 결과이면 빈 리스트를 반환한다."""
        results = _combine_rrf([], [], 0.6, 0.4, 60, 5)
        assert results == []

    def test_양쪽에_있는_결과가_더_높은_점수(self) -> None:
        """양쪽 모두에 있는 결과는 한쪽에만 있는 결과보다 점수가 높다."""
        # 벡터 1위 + FTS에는 없음
        vector = [
            {
                "chunk_id": "only_vector",
                "text": "벡터만",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "S1",
                "start_time": 0.0,
                "end_time": 10.0,
                "chunk_index": 0,
            },
            {
                "chunk_id": "in_both",
                "text": "양쪽",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "S1",
                "start_time": 10.0,
                "end_time": 20.0,
                "chunk_index": 1,
            },
        ]
        fts = [
            {
                "chunk_id": "in_both",
                "text": "양쪽",
                "meeting_id": "m1",
                "date": "2026-03-04",
                "speakers": "S1",
                "start_time": 10.0,
                "end_time": 20.0,
                "chunk_index": 1,
            },
        ]

        results = _combine_rrf(vector, fts, 0.6, 0.4, 60, 10)

        # 양쪽에 있는 결과가 1위
        assert results[0].chunk_id == "in_both"
        assert results[0].source == "both"

    def test_speakers_문자열_파싱(self) -> None:
        """speakers가 콤마 문자열이면 리스트로 변환한다."""
        vector = [
            {
                "chunk_id": "c1",
                "text": "t",
                "meeting_id": "m1",
                "date": "d",
                "speakers": "SPEAKER_00,SPEAKER_01",
                "start_time": 0.0,
                "end_time": 10.0,
                "chunk_index": 0,
            }
        ]

        results = _combine_rrf(vector, [], 0.6, 0.4, 60, 10)

        assert results[0].speakers == ["SPEAKER_00", "SPEAKER_01"]

    def test_speakers_리스트_유지(self) -> None:
        """speakers가 이미 리스트이면 그대로 유지한다."""
        vector = [
            {
                "chunk_id": "c1",
                "text": "t",
                "meeting_id": "m1",
                "date": "d",
                "speakers": ["SPEAKER_00"],
                "start_time": 0.0,
                "end_time": 10.0,
                "chunk_index": 0,
            }
        ]

        results = _combine_rrf(vector, [], 0.6, 0.4, 60, 10)

        assert results[0].speakers == ["SPEAKER_00"]


# === FTS5 쿼리 빌더 테스트 ===


class TestBuildFtsQuery:
    """FTS5 쿼리 빌더 테스트."""

    def test_단일_단어(self) -> None:
        """단일 단어는 그대로 반환한다."""
        assert _build_fts_query("프로젝트") == "프로젝트"

    def test_복수_단어_OR_연결(self) -> None:
        """복수 단어는 OR로 연결한다."""
        result = _build_fts_query("프로젝트 일정")
        assert result == "프로젝트 OR 일정"

    def test_특수문자_제거(self) -> None:
        """FTS5 특수문자를 제거한다."""
        result = _build_fts_query("프로젝트@#$일정")
        # @#$ → 공백으로 변환 → "프로젝트 일정" → OR 연결
        assert "프로젝트" in result
        assert "일정" in result

    def test_빈_문자열(self) -> None:
        """빈 문자열은 빈 문자열을 반환한다."""
        assert _build_fts_query("") == ""
        assert _build_fts_query("   ") == ""

    def test_특수문자만(self) -> None:
        """특수문자만 있으면 빈 문자열을 반환한다."""
        assert _build_fts_query("@#$%") == ""

    def test_한국어_영어_혼합(self) -> None:
        """한국어와 영어가 혼합된 쿼리를 처리한다."""
        result = _build_fts_query("API 엔드포인트")
        assert "API" in result
        assert "엔드포인트" in result

    def test_숫자_포함(self) -> None:
        """숫자가 포함된 쿼리를 처리한다."""
        result = _build_fts_query("3월 4일 회의")
        assert "3월" in result
        assert "4일" in result


# === FTS5 검색 테스트 (실제 SQLite) ===


class TestSearchFts:
    """FTS5 검색 기능 테스트 (실제 SQLite 사용)."""

    def _setup_fts_db(self, db_path: Path) -> None:
        """테스트용 FTS5 데이터베이스를 생성하고 데이터를 삽입한다."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
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

        # 테스트 데이터 삽입
        test_data = [
            (
                "m1_c0",
                "프로젝트 일정 논의 내용입니다",
                "meeting_001",
                "2026-03-04",
                "SPEAKER_00,SPEAKER_01",
                0.0,
                30.0,
                0,
            ),
            (
                "m1_c1",
                "API 엔드포인트 설계에 대해 논의했습니다",
                "meeting_001",
                "2026-03-04",
                "SPEAKER_00",
                30.0,
                60.0,
                1,
            ),
            (
                "m1_c2",
                "데이터베이스 스키마 변경 사항 검토",
                "meeting_001",
                "2026-03-04",
                "SPEAKER_01",
                60.0,
                90.0,
                2,
            ),
            (
                "m2_c0",
                "분기 매출 보고서 프로젝트 업데이트",
                "meeting_002",
                "2026-03-05",
                "SPEAKER_02",
                0.0,
                30.0,
                0,
            ),
            (
                "m2_c1",
                "채용 일정 및 면접 프로세스",
                "meeting_002",
                "2026-03-05",
                "SPEAKER_02,SPEAKER_03",
                30.0,
                60.0,
                1,
            ),
        ]

        for data in test_data:
            conn.execute(
                """INSERT INTO chunks_fts
                   (chunk_id, text, meeting_id, date, speakers,
                    start_time, end_time, chunk_index)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                data,
            )
        conn.commit()
        conn.close()

    def test_키워드_검색(self, tmp_path: Path) -> None:
        """한국어 키워드로 검색한다."""
        db_path = tmp_path / "test.db"
        self._setup_fts_db(db_path)

        results = _search_fts("프로젝트", db_path, top_k=5)

        assert len(results) >= 1
        # "프로젝트"가 포함된 청크가 검색됨
        texts = [r["text"] for r in results]
        assert any("프로젝트" in t for t in texts)

    def test_복수_키워드_검색(self, tmp_path: Path) -> None:
        """복수 키워드(OR)로 검색한다."""
        db_path = tmp_path / "test.db"
        self._setup_fts_db(db_path)

        results = _search_fts("API 데이터베이스", db_path, top_k=5)

        # API 또는 데이터베이스가 포함된 결과 반환
        assert len(results) >= 1

    def test_날짜_필터(self, tmp_path: Path) -> None:
        """날짜 필터를 적용한다."""
        db_path = tmp_path / "test.db"
        self._setup_fts_db(db_path)

        results = _search_fts("프로젝트", db_path, top_k=5, date_filter="2026-03-05")

        # 2026-03-05 날짜의 결과만 반환
        for r in results:
            assert r["date"] == "2026-03-05"

    def test_화자_필터(self, tmp_path: Path) -> None:
        """화자 필터를 적용한다."""
        db_path = tmp_path / "test.db"
        self._setup_fts_db(db_path)

        results = _search_fts("프로젝트", db_path, top_k=5, speaker_filter="SPEAKER_02")

        # SPEAKER_02가 포함된 결과만 반환
        for r in results:
            assert "SPEAKER_02" in r["speakers"]

    def test_회의id_필터(self, tmp_path: Path) -> None:
        """회의 ID 필터를 적용한다."""
        db_path = tmp_path / "test.db"
        self._setup_fts_db(db_path)

        results = _search_fts("프로젝트", db_path, top_k=5, meeting_id_filter="meeting_001")

        for r in results:
            assert r["meeting_id"] == "meeting_001"

    def test_top_k_제한(self, tmp_path: Path) -> None:
        """top_k로 결과 수를 제한한다."""
        db_path = tmp_path / "test.db"
        self._setup_fts_db(db_path)

        results = _search_fts("프로젝트", db_path, top_k=1)

        assert len(results) <= 1

    def test_결과_없음(self, tmp_path: Path) -> None:
        """매칭되는 결과가 없으면 빈 리스트를 반환한다."""
        db_path = tmp_path / "test.db"
        self._setup_fts_db(db_path)

        results = _search_fts("존재하지않는키워드xyz", db_path, top_k=5)

        assert results == []

    def test_DB_파일_없음(self, tmp_path: Path) -> None:
        """DB 파일이 없으면 빈 리스트를 반환한다."""
        db_path = tmp_path / "nonexistent.db"

        results = _search_fts("프로젝트", db_path, top_k=5)

        assert results == []

    def test_FTS_테이블_없음(self, tmp_path: Path) -> None:
        """FTS5 테이블이 없으면 빈 리스트를 반환한다."""
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE dummy (id INTEGER)")
        conn.commit()
        conn.close()

        results = _search_fts("프로젝트", db_path, top_k=5)

        assert results == []


# === SearchResult 데이터 클래스 테스트 ===


class TestSearchResult:
    """SearchResult 데이터 클래스 테스트."""

    def test_to_dict(self) -> None:
        """딕셔너리 변환이 올바르게 동작한다."""
        result = SearchResult(
            chunk_id="c1",
            text="테스트 텍스트",
            score=0.5,
            meeting_id="m1",
            date="2026-03-04",
            speakers=["SPEAKER_00"],
            start_time=0.0,
            end_time=30.0,
            chunk_index=0,
            source="both",
        )

        d = result.to_dict()

        assert d["chunk_id"] == "c1"
        assert d["text"] == "테스트 텍스트"
        assert d["score"] == 0.5
        assert d["source"] == "both"
        assert d["speakers"] == ["SPEAKER_00"]


class TestSearchResponse:
    """SearchResponse 데이터 클래스 테스트."""

    def test_to_dict(self) -> None:
        """딕셔너리 변환이 올바르게 동작한다."""
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
                    end_time=30.0,
                ),
            ],
            query="테스트 쿼리",
            total_found=1,
            vector_count=1,
            fts_count=1,
        )

        d = response.to_dict()

        assert d["query"] == "테스트 쿼리"
        assert d["total_found"] == 1
        assert len(d["results"]) == 1


# === HybridSearchEngine 통합 테스트 ===


class TestHybridSearchEngine:
    """HybridSearchEngine 통합 테스트."""

    def _create_engine(self) -> HybridSearchEngine:
        """테스트용 엔진 인스턴스를 생성한다 (__new__ 패턴)."""
        import threading

        engine = HybridSearchEngine.__new__(HybridSearchEngine)
        config = _make_config()
        engine._config = config
        engine._model_manager = _make_model_manager()
        engine._vector_weight = config.search.vector_weight
        engine._fts_weight = config.search.fts_weight
        engine._rrf_k = config.search.rrf_k
        engine._top_k = config.search.top_k
        engine._model_name = config.embedding.model_name
        engine._device = config.embedding.device
        engine._query_prefix = config.embedding.query_prefix
        engine._chroma_dir = config.paths.resolved_chroma_db_dir
        engine._meetings_db = config.paths.resolved_meetings_db

        # PERF-005: 임베딩 모델 캐시 (테스트용 mock 모델 미리 설정)
        mock_model = MagicMock()
        mock_model.encode.return_value = [MagicMock(tolist=MagicMock(return_value=[0.1] * 384))]
        engine._embed_model = mock_model
        engine._embed_model_lock = threading.Lock()

        # PERF-011: ChromaDB 캐시 (검색 함수가 패치되므로 None으로 설정)
        engine._chroma_client = None
        engine._chroma_collection = None
        engine._chroma_lock = threading.Lock()

        # PERF: FTS5 SQLite 연결 캐시 (테스트에서는 None으로 설정)
        engine._fts_conn = None
        engine._fts_conn_lock = threading.Lock()
        return engine

    @pytest.mark.asyncio
    async def test_빈_쿼리_에러(self) -> None:
        """빈 쿼리 시 EmptyQueryError를 발생시킨다."""
        engine = self._create_engine()

        with pytest.raises(EmptyQueryError):
            await engine.search("")

        with pytest.raises(EmptyQueryError):
            await engine.search("   ")

    @pytest.mark.asyncio
    async def test_양쪽_검색_결과_결합(self) -> None:
        """벡터 + FTS 양쪽 결과를 RRF로 결합한다."""
        engine = self._create_engine()

        mock_model = MagicMock()
        mock_model.encode.return_value = [MagicMock(tolist=MagicMock(return_value=[0.1] * 384))]
        engine._model_manager = _make_model_manager()
        ctx = engine._model_manager.acquire.return_value
        ctx.__aenter__ = AsyncMock(return_value=mock_model)

        vector_results = _make_vector_results(3)
        fts_results = _make_fts_results(3, offset=2)

        with (
            patch(
                "search.hybrid_search._search_vector",
                return_value=vector_results,
            ),
            patch(
                "search.hybrid_search._search_fts",
                return_value=fts_results,
            ),
        ):
            response = await engine.search("프로젝트 일정")

        assert isinstance(response, SearchResponse)
        assert response.query == "프로젝트 일정"
        assert response.vector_count == 3
        assert response.fts_count == 3
        assert len(response.results) > 0
        # 점수 내림차순
        for i in range(len(response.results) - 1):
            assert response.results[i].score >= response.results[i + 1].score

    @pytest.mark.asyncio
    async def test_벡터만_성공_fts_실패(self) -> None:
        """FTS 검색 실패 시 벡터 결과만 반환한다 (graceful degradation)."""
        engine = self._create_engine()

        mock_model = MagicMock()
        mock_model.encode.return_value = [MagicMock(tolist=MagicMock(return_value=[0.1] * 384))]
        engine._model_manager = _make_model_manager()
        ctx = engine._model_manager.acquire.return_value
        ctx.__aenter__ = AsyncMock(return_value=mock_model)

        vector_results = _make_vector_results(3)

        with (
            patch(
                "search.hybrid_search._search_vector",
                return_value=vector_results,
            ),
            patch(
                "search.hybrid_search._search_fts",
                return_value=[],  # FTS 실패 → 빈 결과
            ),
        ):
            response = await engine.search("테스트")

        assert len(response.results) > 0
        assert response.vector_count == 3
        assert response.fts_count == 0
        # 모든 결과가 vector 소스
        for r in response.results:
            assert r.source == "vector"

    @pytest.mark.asyncio
    async def test_fts만_성공_벡터_실패(self) -> None:
        """벡터 검색 실패 시 FTS 결과만 반환한다."""
        engine = self._create_engine()

        mock_model = MagicMock()
        mock_model.encode.return_value = [MagicMock(tolist=MagicMock(return_value=[0.1] * 384))]
        engine._model_manager = _make_model_manager()
        ctx = engine._model_manager.acquire.return_value
        ctx.__aenter__ = AsyncMock(return_value=mock_model)

        fts_results = _make_fts_results(3)

        with (
            patch(
                "search.hybrid_search._search_vector",
                return_value=[],  # 벡터 실패 → 빈 결과
            ),
            patch(
                "search.hybrid_search._search_fts",
                return_value=fts_results,
            ),
        ):
            response = await engine.search("테스트")

        assert len(response.results) > 0
        assert response.vector_count == 0
        assert response.fts_count == 3
        for r in response.results:
            assert r.source == "fts"

    @pytest.mark.asyncio
    async def test_양쪽_모두_빈_결과(self) -> None:
        """양쪽 모두 결과가 없으면 빈 응답을 반환한다."""
        engine = self._create_engine()

        mock_model = MagicMock()
        mock_model.encode.return_value = [MagicMock(tolist=MagicMock(return_value=[0.1] * 384))]
        engine._model_manager = _make_model_manager()
        ctx = engine._model_manager.acquire.return_value
        ctx.__aenter__ = AsyncMock(return_value=mock_model)

        with (
            patch(
                "search.hybrid_search._search_vector",
                return_value=[],
            ),
            patch(
                "search.hybrid_search._search_fts",
                return_value=[],
            ),
        ):
            response = await engine.search("없는내용")

        assert response.results == []
        assert response.total_found == 0

    @pytest.mark.asyncio
    async def test_필터_적용(self) -> None:
        """날짜/화자/회의 필터가 올바르게 전달된다."""
        engine = self._create_engine()

        mock_model = MagicMock()
        mock_model.encode.return_value = [MagicMock(tolist=MagicMock(return_value=[0.1] * 384))]
        engine._model_manager = _make_model_manager()
        ctx = engine._model_manager.acquire.return_value
        ctx.__aenter__ = AsyncMock(return_value=mock_model)

        with (
            patch(
                "search.hybrid_search._search_vector",
                return_value=[],
            ) as _mock_vector,
            patch(
                "search.hybrid_search._search_fts",
                return_value=[],
            ) as _mock_fts,
        ):
            response = await engine.search(
                "테스트",
                date_filter="2026-03-04",
                speaker_filter="SPEAKER_00",
                meeting_id_filter="meeting_001",
            )

        # 필터가 검색 함수에 전달되었는지 확인
        # asyncio.to_thread로 호출되므로 직접 확인은 어려움
        # 대신 response의 filters_applied 확인
        assert response.filters_applied["date"] == "2026-03-04"
        assert response.filters_applied["speaker"] == "SPEAKER_00"
        assert response.filters_applied["meeting_id"] == "meeting_001"

    @pytest.mark.asyncio
    async def test_custom_top_k(self) -> None:
        """사용자 지정 top_k를 적용한다."""
        engine = self._create_engine()

        mock_model = MagicMock()
        mock_model.encode.return_value = [MagicMock(tolist=MagicMock(return_value=[0.1] * 384))]
        engine._model_manager = _make_model_manager()
        ctx = engine._model_manager.acquire.return_value
        ctx.__aenter__ = AsyncMock(return_value=mock_model)

        vector_results = _make_vector_results(10)

        with (
            patch(
                "search.hybrid_search._search_vector",
                return_value=vector_results,
            ),
            patch(
                "search.hybrid_search._search_fts",
                return_value=[],
            ),
        ):
            response = await engine.search("테스트", top_k=2)

        assert len(response.results) <= 2

    @pytest.mark.asyncio
    async def test_한국어_NFC_정규화(self) -> None:
        """검색 쿼리에 NFC 정규화를 적용한다."""
        engine = self._create_engine()

        mock_model = MagicMock()
        mock_model.encode.return_value = [MagicMock(tolist=MagicMock(return_value=[0.1] * 384))]
        engine._model_manager = _make_model_manager()
        ctx = engine._model_manager.acquire.return_value
        ctx.__aenter__ = AsyncMock(return_value=mock_model)

        # NFD 형태의 한국어 (분해형)
        nfd_query = "\u1112\u1161\u11ab\u1100\u116e\u11a8\u110b\u1165"  # "한국어" NFD

        with (
            patch(
                "search.hybrid_search._search_vector",
                return_value=[],
            ),
            patch(
                "search.hybrid_search._search_fts",
                return_value=[],
            ),
        ):
            response = await engine.search(nfd_query)

        # NFC로 정규화된 쿼리가 저장됨
        assert response.query == "한국어"

    @pytest.mark.asyncio
    async def test_query_prefix_적용(self) -> None:
        """쿼리 임베딩 시 query: 접두사가 적용된다."""
        engine = self._create_engine()

        mock_model = MagicMock()
        mock_model.encode.return_value = [MagicMock(tolist=MagicMock(return_value=[0.1] * 384))]

        # _embed_query 직접 호출하여 접두사 확인
        engine._embed_query(mock_model, "테스트 쿼리")

        # encode에 전달된 텍스트 확인
        call_args = mock_model.encode.call_args
        texts = call_args[0][0]
        assert texts[0].startswith("query: ")
        assert "테스트 쿼리" in texts[0]


# === 벡터 검색 테스트 ===


class TestSearchVector:
    """벡터 검색 기능 테스트 (ChromaDB mock)."""

    @pytest.mark.asyncio
    async def test_컬렉션_None(self) -> None:
        """컬렉션이 None이면 빈 결과를 반환한다. (PERF-011 변경 반영)"""
        from search.hybrid_search import _search_vector

        results = _search_vector(
            [0.1] * 384,
            None,  # 컬렉션이 없는 경우
            top_k=5,
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_빈_컬렉션(self) -> None:
        """비어있는 컬렉션이면 빈 결과를 반환한다."""
        from search.hybrid_search import _search_vector

        mock_collection = MagicMock()
        mock_collection.count.return_value = 0

        results = _search_vector([0.1] * 384, mock_collection, top_k=5)

        assert results == []

    @pytest.mark.asyncio
    async def test_검색_결과_변환(self) -> None:
        """ChromaDB 검색 결과를 올바르게 변환한다. (PERF-011 변경 반영)"""
        from search.hybrid_search import _search_vector

        # ChromaDB mock 컬렉션 설정
        mock_collection = MagicMock()
        mock_collection.count.return_value = 10
        mock_collection.query.return_value = {
            "ids": [["c1", "c2"]],
            "documents": [["텍스트1", "텍스트2"]],
            "metadatas": [
                [
                    {
                        "meeting_id": "m1",
                        "date": "2026-03-04",
                        "speakers": "S0,S1",
                        "start_time": 0.0,
                        "end_time": 30.0,
                        "chunk_index": 0,
                    },
                    {
                        "meeting_id": "m1",
                        "date": "2026-03-04",
                        "speakers": "S0",
                        "start_time": 30.0,
                        "end_time": 60.0,
                        "chunk_index": 1,
                    },
                ]
            ],
            "distances": [[0.1, 0.2]],
        }

        results = _search_vector([0.1] * 384, mock_collection, top_k=5)

        assert len(results) == 2
        assert results[0]["chunk_id"] == "c1"
        assert results[0]["text"] == "텍스트1"
        assert results[0]["meeting_id"] == "m1"


# === 에러 계층 테스트 ===


class TestErrorHierarchy:
    """에러 계층 구조 테스트."""

    def test_SearchError_상속(self) -> None:
        """모든 검색 에러가 SearchError를 상속한다."""
        assert issubclass(EmptyQueryError, SearchError)
        assert issubclass(ModelLoadError, SearchError)

    def test_에러_메시지(self) -> None:
        """에러 메시지가 올바르게 전달된다."""
        error = EmptyQueryError("빈 쿼리입니다")
        assert str(error) == "빈 쿼리입니다"

        error = ModelLoadError("모델 로드 실패")
        assert str(error) == "모델 로드 실패"


# === 성능 최적화 테스트 (PERF-005, PERF-010, PERF-011) ===


class TestPerformanceOptimizations:
    """성능 최적화 관련 테스트 (캐싱, 병렬 검색)."""

    def _create_engine_with_caching(self) -> HybridSearchEngine:
        """캐싱 테스트를 위한 엔진을 생성한다."""
        import threading

        engine = HybridSearchEngine.__new__(HybridSearchEngine)
        config = _make_config()
        engine._config = config
        engine._model_manager = _make_model_manager()
        engine._vector_weight = config.search.vector_weight
        engine._fts_weight = config.search.fts_weight
        engine._rrf_k = config.search.rrf_k
        engine._top_k = config.search.top_k
        engine._model_name = config.embedding.model_name
        engine._device = config.embedding.device
        engine._query_prefix = config.embedding.query_prefix
        engine._chroma_dir = config.paths.resolved_chroma_db_dir
        engine._meetings_db = config.paths.resolved_meetings_db

        # 캐시 필드 초기화 (비어있는 상태)
        engine._embed_model = None
        engine._embed_model_lock = threading.Lock()
        engine._chroma_client = None
        engine._chroma_collection = None
        engine._chroma_lock = threading.Lock()

        # PERF: FTS5 SQLite 연결 캐시
        engine._fts_conn = None
        engine._fts_conn_lock = threading.Lock()
        return engine

    def test_PERF005_임베딩_모델_캐시_재사용(self) -> None:
        """PERF-005: 임베딩 모델이 한 번만 로드되고 캐시되는지 확인한다."""
        engine = self._create_engine_with_caching()

        # mock 모델 로더 설정
        mock_model = MagicMock()
        mock_model.encode.return_value = [MagicMock(tolist=MagicMock(return_value=[0.1] * 384))]

        with patch.object(engine, "_load_model", return_value=mock_model) as mock_loader:
            # 첫 번째 호출: 모델 로드 실행
            model1 = engine._get_embed_model()
            # 두 번째 호출: 캐시에서 반환 (로드 안 함)
            model2 = engine._get_embed_model()
            # 세 번째 호출: 캐시에서 반환 (로드 안 함)
            model3 = engine._get_embed_model()

        # _load_model은 정확히 1번만 호출되어야 함
        assert mock_loader.call_count == 1
        # 모든 반환값이 동일한 인스턴스여야 함
        assert model1 is model2
        assert model2 is model3

    def test_PERF011_chroma_클라이언트_캐시_재사용(self, tmp_path: Path) -> None:
        """PERF-011: ChromaDB 클라이언트가 한 번만 생성되고 캐시되는지 확인한다."""
        engine = self._create_engine_with_caching()

        # 실제 ChromaDB 디렉토리 경로 설정
        chroma_dir = tmp_path / "chroma"
        chroma_dir.mkdir()
        engine._chroma_dir = chroma_dir

        # mock chromadb 설정
        mock_collection = MagicMock()
        mock_client = MagicMock()
        mock_client.get_collection.return_value = mock_collection

        mock_chromadb = MagicMock()
        mock_chromadb.PersistentClient.return_value = mock_client

        with patch.dict("sys.modules", {"chromadb": mock_chromadb}):
            # 첫 번째 호출: 클라이언트 생성
            col1 = engine._get_chroma_collection()
            # 두 번째 호출: 캐시에서 반환
            col2 = engine._get_chroma_collection()
            # 세 번째 호출: 캐시에서 반환
            col3 = engine._get_chroma_collection()

        # PersistentClient는 정확히 1번만 호출되어야 함
        assert mock_chromadb.PersistentClient.call_count == 1
        # 모든 반환값이 동일한 인스턴스여야 함
        assert col1 is col2
        assert col2 is col3
        # 캐시된 클라이언트가 엔진에 저장되어 있어야 함
        assert engine._chroma_client is mock_client
        assert engine._chroma_collection is mock_collection

    def test_PERF011_chroma_디렉토리_없으면_None_반환(self) -> None:
        """PERF-011: ChromaDB 디렉토리가 없으면 None을 반환한다."""
        engine = self._create_engine_with_caching()
        engine._chroma_dir = Path("/tmp/nonexistent_chroma_xyz_test")

        result = engine._get_chroma_collection()

        assert result is None

    def test_PERF011_chroma_컬렉션_없으면_None_반환(self, tmp_path: Path) -> None:
        """PERF-011: ChromaDB 컬렉션이 없으면 None을 반환한다."""
        engine = self._create_engine_with_caching()
        chroma_dir = tmp_path / "chroma"
        chroma_dir.mkdir()
        engine._chroma_dir = chroma_dir

        mock_client = MagicMock()
        mock_client.get_collection.side_effect = Exception("컬렉션 없음")
        mock_chromadb = MagicMock()
        mock_chromadb.PersistentClient.return_value = mock_client

        with patch.dict("sys.modules", {"chromadb": mock_chromadb}):
            result = engine._get_chroma_collection()

        assert result is None

    @pytest.mark.asyncio
    async def test_PERF010_병렬_검색_실행(self) -> None:
        """PERF-010: 벡터 검색과 FTS5 검색이 병렬로 실행되는지 확인한다.

        각 검색에 `SLEEP` 만큼 지연을 주고 전체 소요 시간을 측정한다.
        - 순차 실행: 약 2*SLEEP
        - 병렬 실행: 약 SLEEP

        threshold 는 `SLEEP * 1.9` 로 두어 CI 러너(특히 macOS runner) 의
        추가 오버헤드(스케줄링·GC·I/O·동시 실행 중인 다른 job)를 흡수하면서도
        순차 실행(2.0×)은 확실히 걸러낸다. PR #12 CI 에서 0.352초 관찰(임계값
        0.35 간발 초과)로 1.9× 로 완화. 순차(2×=0.40) 와의 여유는 0.02 초 확보.
        """
        import time

        SLEEP = 0.2  # 각 검색 지연 (초)
        THRESHOLD = SLEEP * 1.9  # 병렬 판정 임계 — 순차(2×)와 병렬(1×) 사이
        engine = self._create_engine_with_caching()

        # mock 임베딩 모델 설정
        mock_model = MagicMock()
        mock_model.encode.return_value = [MagicMock(tolist=MagicMock(return_value=[0.1] * 384))]
        engine._embed_model = mock_model

        def slow_vector_search(*args: Any, **kwargs: Any) -> list:
            time.sleep(SLEEP)
            return _make_vector_results(2)

        def slow_fts_search(*args: Any, **kwargs: Any) -> list:
            time.sleep(SLEEP)
            return _make_fts_results(2)

        with (
            patch(
                "search.hybrid_search._search_vector",
                side_effect=slow_vector_search,
            ),
            patch(
                "search.hybrid_search._search_fts",
                side_effect=slow_fts_search,
            ),
        ):
            start = time.monotonic()
            response = await engine.search("테스트 쿼리")
            elapsed = time.monotonic() - start

        assert elapsed < THRESHOLD, (
            f"검색이 병렬로 실행되지 않음: {elapsed:.3f}초 "
            f"(순차 실행 예상: ~{2 * SLEEP:.2f}초, 병렬 예상: ~{SLEEP:.2f}초, "
            f"임계값: {THRESHOLD:.2f}초)"
        )
        assert response.vector_count == 2
        assert response.fts_count == 2
