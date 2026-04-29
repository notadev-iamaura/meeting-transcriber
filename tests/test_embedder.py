"""
임베딩 + 저장 모듈 테스트 (Embedder Test Module)

목적: steps/embedder.py의 임베딩 생성 및 ChromaDB/FTS5 저장 로직을 검증한다.
주요 테스트:
    - 정상 임베딩 및 이중 저장
    - 빈 청크 에러 처리
    - passage: 접두사 자동 추가
    - 배치 분할 임베딩
    - ChromaDB 저장 멱등성
    - FTS5 테이블 생성 및 저장
    - ChromaDB 실패 시 FTS5만 저장
    - FTS5 실패 시 ChromaDB만 저장
    - 체크포인트 저장/복원
    - NFC 정규화 적용
의존성: pytest, pytest-asyncio
"""

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from config import AppConfig, EmbeddingConfig, PathsConfig
from steps.chunker import Chunk, ChunkedResult
from steps.embedder import (
    _FTS_TABLE_NAME,
    EmbeddedChunk,
    EmbeddedResult,
    Embedder,
    EmptyChunksError,
    ModelLoadError,
    StorageError,
    _ensure_fts_table,
    _store_chunks_chroma,
    _store_chunks_fts,
)

# === 헬퍼 함수 ===


def _make_chunk(
    text: str = "테스트 발화입니다",
    meeting_id: str = "meeting_001",
    date: str = "2026-03-04",
    speakers: list[str] | None = None,
    start_time: float = 0.0,
    end_time: float = 5.0,
    chunk_index: int = 0,
) -> Chunk:
    """테스트용 Chunk를 생성한다."""
    return Chunk(
        text=text,
        meeting_id=meeting_id,
        date=date,
        speakers=speakers or ["SPEAKER_00"],
        start_time=start_time,
        end_time=end_time,
        estimated_tokens=50,
        chunk_index=chunk_index,
    )


def _make_chunked_result(
    chunks: list[Chunk] | None = None,
    meeting_id: str = "meeting_001",
    date: str = "2026-03-04",
) -> ChunkedResult:
    """테스트용 ChunkedResult를 생성한다."""
    if chunks is None:
        chunks = [_make_chunk(meeting_id=meeting_id, date=date)]
    return ChunkedResult(
        chunks=chunks,
        meeting_id=meeting_id,
        date=date,
        total_utterances=10,
        num_speakers=2,
        audio_path="/test/audio.wav",
    )


def _make_config(
    model_name: str = "intfloat/multilingual-e5-small",
    dimension: int = 384,
    device: str = "cpu",
    batch_size: int = 32,
) -> AppConfig:
    """테스트용 AppConfig를 생성한다."""
    return AppConfig(
        embedding=EmbeddingConfig(
            model_name=model_name,
            dimension=dimension,
            device=device,
            batch_size=batch_size,
        ),
        paths=PathsConfig(
            base_dir="/tmp/test-meeting-transcriber",
        ),
    )


def _make_embedder(config: AppConfig | None = None) -> Embedder:
    """테스트용 Embedder를 __init__ 우회하여 생성한다."""
    cfg = config or _make_config()
    embedder = Embedder.__new__(Embedder)
    embedder._config = cfg
    embedder._model_manager = MagicMock()
    embedder._model_name = cfg.embedding.model_name
    embedder._dimension = cfg.embedding.dimension
    embedder._device = cfg.embedding.device
    # 2-B: 청크 수 기반 적응 디바이스 (기본은 config 값과 동일)
    embedder._effective_device = cfg.embedding.device
    embedder._passage_prefix = cfg.embedding.passage_prefix
    embedder._batch_size = cfg.embedding.batch_size
    embedder._chroma_dir = cfg.paths.resolved_chroma_db_dir
    embedder._meetings_db = cfg.paths.resolved_meetings_db
    return embedder


def _make_fake_embeddings(count: int, dim: int = 384) -> list[list[float]]:
    """테스트용 가짜 임베딩 벡터를 생성한다."""
    return [np.random.randn(dim).tolist() for _ in range(count)]


def _make_fake_model(batch_size: int = 32, dim: int = 384) -> MagicMock:
    """encode() 호출 시 적절한 크기의 벡터를 반환하는 가짜 모델."""
    model = MagicMock()

    def fake_encode(texts, batch_size=32, show_progress_bar=False, normalize_embeddings=True):
        """텍스트 수만큼 랜덤 벡터를 반환한다."""
        return np.random.randn(len(texts), dim).astype(np.float32)

    model.encode = MagicMock(side_effect=fake_encode)
    return model


# === EmbeddedChunk 테스트 ===


class TestEmbeddedChunk:
    """EmbeddedChunk 데이터 클래스 테스트."""

    def test_to_dict_embedding_제외(self) -> None:
        """to_dict()는 embedding 필드를 포함하지 않는다."""
        chunk = EmbeddedChunk(
            chunk_id="m1_chunk_0000",
            text="테스트",
            embedding=[0.1, 0.2, 0.3],
            meeting_id="m1",
            date="2026-03-04",
            speakers=["A"],
            start_time=0.0,
            end_time=5.0,
        )
        d = chunk.to_dict()
        assert "embedding" not in d
        assert d["chunk_id"] == "m1_chunk_0000"
        assert d["text"] == "테스트"


# === EmbeddedResult 테스트 ===


class TestEmbeddedResult:
    """EmbeddedResult 데이터 클래스 테스트."""

    def test_to_dict(self) -> None:
        """to_dict()가 모든 필드를 포함한다."""
        result = EmbeddedResult(
            chunks=[],
            meeting_id="m1",
            date="2026-03-04",
            total_chunks=0,
            chroma_stored=True,
            fts_stored=True,
        )
        d = result.to_dict()
        assert d["meeting_id"] == "m1"
        assert d["chroma_stored"] is True
        assert d["fts_stored"] is True

    def test_checkpoint_저장_복원(self, tmp_path: Path) -> None:
        """체크포인트 저장 후 복원 시 동일한 데이터를 반환한다."""
        original = EmbeddedResult(
            chunks=[
                EmbeddedChunk(
                    chunk_id="m1_chunk_0000",
                    text="한국어 테스트",
                    embedding=[0.1] * 384,
                    meeting_id="m1",
                    date="2026-03-04",
                    speakers=["A", "B"],
                    start_time=0.0,
                    end_time=10.0,
                    chunk_index=0,
                ),
            ],
            meeting_id="m1",
            date="2026-03-04",
            total_chunks=1,
            embedding_dimension=384,
            chroma_stored=True,
            fts_stored=True,
        )

        checkpoint_path = tmp_path / "embed_checkpoint.json"
        original.save_checkpoint(checkpoint_path)

        # 복원
        restored = EmbeddedResult.from_checkpoint(checkpoint_path)
        assert restored.meeting_id == "m1"
        assert restored.total_chunks == 1
        assert len(restored.chunks) == 1
        assert restored.chunks[0].text == "한국어 테스트"
        assert restored.chunks[0].embedding == []  # 벡터는 복원 안 됨
        assert restored.chroma_stored is True


# === FTS5 함수 테스트 ===


class TestFTS5:
    """FTS5 테이블 생성 및 저장 테스트."""

    def test_fts_테이블_생성(self, tmp_path: Path) -> None:
        """FTS5 테이블이 정상적으로 생성된다."""
        db_path = tmp_path / "test.db"
        _ensure_fts_table(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            # 테이블 존재 확인
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (_FTS_TABLE_NAME,),
            )
            assert cursor.fetchone() is not None
        finally:
            conn.close()

    def test_fts_테이블_멱등성(self, tmp_path: Path) -> None:
        """FTS5 테이블을 두 번 생성해도 에러가 발생하지 않는다."""
        db_path = tmp_path / "test.db"
        _ensure_fts_table(db_path)
        _ensure_fts_table(db_path)  # 두 번째 호출도 성공해야 함

    def test_fts_저장_및_조회(self, tmp_path: Path) -> None:
        """청크가 FTS5에 정상적으로 저장되고 검색된다."""
        db_path = tmp_path / "test.db"
        _ensure_fts_table(db_path)

        chunks = [
            EmbeddedChunk(
                chunk_id="m1_chunk_0000",
                text="오늘 회의에서 프로젝트 일정을 논의했습니다",
                embedding=[],
                meeting_id="m1",
                date="2026-03-04",
                speakers=["A", "B"],
                start_time=0.0,
                end_time=10.0,
                chunk_index=0,
            ),
            EmbeddedChunk(
                chunk_id="m1_chunk_0001",
                text="다음 주까지 디자인 리뷰를 완료하겠습니다",
                embedding=[],
                meeting_id="m1",
                date="2026-03-04",
                speakers=["B"],
                start_time=10.0,
                end_time=20.0,
                chunk_index=1,
            ),
        ]

        _store_chunks_fts(chunks, db_path, "m1")

        # 저장 확인
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(f"SELECT COUNT(*) FROM {_FTS_TABLE_NAME}")
            assert cursor.fetchone()[0] == 2

            # 한국어 FTS5 검색
            cursor = conn.execute(
                f"SELECT chunk_id FROM {_FTS_TABLE_NAME} WHERE {_FTS_TABLE_NAME} MATCH '프로젝트'"
            )
            results = cursor.fetchall()
            assert len(results) == 1
            assert results[0][0] == "m1_chunk_0000"
        finally:
            conn.close()

    def test_fts_멱등성_재저장(self, tmp_path: Path) -> None:
        """동일 meeting_id로 재저장 시 기존 데이터가 교체된다."""
        db_path = tmp_path / "test.db"
        _ensure_fts_table(db_path)

        chunk_v1 = EmbeddedChunk(
            chunk_id="m1_chunk_0000",
            text="버전 1 텍스트",
            embedding=[],
            meeting_id="m1",
            date="2026-03-04",
            speakers=["A"],
            start_time=0.0,
            end_time=5.0,
        )
        _store_chunks_fts([chunk_v1], db_path, "m1")

        # 같은 meeting_id로 다른 텍스트 저장
        chunk_v2 = EmbeddedChunk(
            chunk_id="m1_chunk_0000",
            text="버전 2 텍스트",
            embedding=[],
            meeting_id="m1",
            date="2026-03-04",
            speakers=["A"],
            start_time=0.0,
            end_time=5.0,
        )
        _store_chunks_fts([chunk_v2], db_path, "m1")

        # 1개만 존재해야 함
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(f"SELECT text FROM {_FTS_TABLE_NAME}")
            rows = cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "버전 2 텍스트"
        finally:
            conn.close()


# === Embedder 동기 메서드 테스트 ===


class TestEmbedderSync:
    """Embedder 동기 메서드 단위 테스트."""

    def test_generate_embeddings_passage_접두사(self) -> None:
        """passage: 접두사가 자동으로 추가된다."""
        embedder = _make_embedder()
        model = _make_fake_model()

        texts = ["테스트 문장"]
        embedder._generate_embeddings(model, texts)

        # model.encode에 전달된 텍스트 확인
        call_args = model.encode.call_args
        prefixed = call_args[0][0]
        assert prefixed[0] == "passage: 테스트 문장"

    def test_generate_embeddings_배치_크기(self) -> None:
        """배치 크기가 올바르게 전달된다."""
        embedder = _make_embedder(_make_config(batch_size=16))
        model = _make_fake_model()

        texts = ["문장 1", "문장 2"]
        embedder._generate_embeddings(model, texts)

        call_args = model.encode.call_args
        assert call_args[1]["batch_size"] == 16

    def test_generate_embeddings_정규화(self) -> None:
        """normalize_embeddings=True가 전달된다."""
        embedder = _make_embedder()
        model = _make_fake_model()

        embedder._generate_embeddings(model, ["테스트"])

        call_args = model.encode.call_args
        assert call_args[1]["normalize_embeddings"] is True

    def test_generate_embeddings_벡터_차원(self) -> None:
        """반환되는 벡터의 차원이 384이다."""
        embedder = _make_embedder()
        model = _make_fake_model(dim=384)

        result = embedder._generate_embeddings(model, ["테스트"])
        assert len(result) == 1
        assert len(result[0]) == 384

    def test_process_chunks_chunk_id_형식(self) -> None:
        """chunk_id가 meeting_id_chunk_XXXX 형식으로 생성된다."""
        embedder = _make_embedder()
        model = _make_fake_model()

        chunks = [
            _make_chunk(chunk_index=0),
            _make_chunk(chunk_index=1),
        ]
        chunked = _make_chunked_result(chunks)

        embedded = embedder._process_chunks(model, chunked)
        assert embedded[0].chunk_id == "meeting_001_chunk_0000"
        assert embedded[1].chunk_id == "meeting_001_chunk_0001"

    def test_process_chunks_메타데이터_보존(self) -> None:
        """원본 청크의 메타데이터가 EmbeddedChunk에 보존된다."""
        embedder = _make_embedder()
        model = _make_fake_model()

        chunk = _make_chunk(
            text="중요한 회의 내용",
            speakers=["A", "B"],
            start_time=10.0,
            end_time=20.0,
        )
        chunked = _make_chunked_result([chunk])

        embedded = embedder._process_chunks(model, chunked)
        assert embedded[0].text == "중요한 회의 내용"
        assert embedded[0].speakers == ["A", "B"]
        assert embedded[0].start_time == 10.0
        assert embedded[0].end_time == 20.0

    def test_process_chunks_다수_배치(self) -> None:
        """배치 크기보다 많은 청크도 올바르게 처리된다.

        PERF: _compute_adaptive_batch_size가 10개 이하 청크를
        한 배치로 처리하므로, 배치 분할을 검증하려면 11개 이상이 필요하다.
        batch_size=5, 청크 11개 → adaptive는 config값(5) 사용 → 3회 호출 (5+5+1)
        """
        embedder = _make_embedder(_make_config(batch_size=5))
        model = _make_fake_model()

        chunks = [_make_chunk(text=f"문장 {i}", chunk_index=i) for i in range(11)]
        chunked = _make_chunked_result(chunks)

        embedded = embedder._process_chunks(model, chunked)
        assert len(embedded) == 11
        # encode가 3번 호출되어야 함 (5+5+1)
        assert model.encode.call_count == 3


# === Embedder 비동기 메서드 테스트 ===


class TestEmbedderAsync:
    """Embedder 비동기 embed() 메서드 테스트."""

    @pytest.mark.asyncio
    async def test_빈_청크_에러(self) -> None:
        """빈 청크 목록으로 embed() 호출 시 EmptyChunksError가 발생한다."""
        embedder = _make_embedder()
        chunked = _make_chunked_result(chunks=[])

        with pytest.raises(EmptyChunksError):
            await embedder.embed(chunked)

    @pytest.mark.asyncio
    async def test_정상_임베딩_및_저장(self) -> None:
        """정상적으로 임베딩 생성 후 ChromaDB + FTS5에 저장된다."""
        embedder = _make_embedder()
        model = _make_fake_model()

        # ModelLoadManager.acquire 모킹
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=model)
        ctx.__aexit__ = AsyncMock(return_value=False)
        embedder._model_manager.acquire = MagicMock(return_value=ctx)

        chunks = [_make_chunk(text="회의 내용 테스트")]
        chunked = _make_chunked_result(chunks)

        with (
            patch("steps.embedder._store_chunks_chroma") as _mock_chroma,
            patch("steps.embedder._ensure_fts_table") as _mock_fts_init,
            patch("steps.embedder._store_chunks_fts") as _mock_fts_store,
        ):
            result = await embedder.embed(chunked)

        assert result.total_chunks == 1
        assert result.chroma_stored is True
        assert result.fts_stored is True
        assert result.meeting_id == "meeting_001"
        assert len(result.chunks) == 1
        assert result.chunks[0].chunk_id == "meeting_001_chunk_0000"
        # 벡터가 생성되었는지 확인
        assert len(result.chunks[0].embedding) == 384

    @pytest.mark.asyncio
    async def test_chromadb_실패_시_StorageError_전파(self) -> None:
        """ChromaDB 저장 실패 시 StorageError 가 그대로 전파된다 (fail-loud).

        정책: 반쪽 인덱스(벡터만 있고 키워드 없음)는 RAG 품질을 떨어뜨려
        '회의 완료인데 검색 부정확' 상태가 되므로 명시적으로 실패 처리.
        파이프라인 재시도 루프 또는 사용자에게 노출되어 백필이 가능해진다.
        """
        embedder = _make_embedder()
        model = _make_fake_model()

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=model)
        ctx.__aexit__ = AsyncMock(return_value=False)
        embedder._model_manager.acquire = MagicMock(return_value=ctx)

        chunked = _make_chunked_result()

        with (
            patch(
                "steps.embedder._store_chunks_chroma",
                side_effect=StorageError("ChromaDB 연결 실패"),
            ),
            patch("steps.embedder._ensure_fts_table"),
            patch("steps.embedder._store_chunks_fts"),
            pytest.raises(StorageError, match="ChromaDB 연결 실패"),
        ):
            await embedder.embed(chunked)

    @pytest.mark.asyncio
    async def test_fts_실패_시_StorageError_전파(self) -> None:
        """FTS5 저장 실패 시 StorageError 가 그대로 전파된다 (fail-loud).

        정책: 키워드 검색 인덱스 없이 벡터만 있는 상태도 의미·키워드 결합 검색
        품질을 망가뜨리므로 명시적 실패로 처리.
        """
        embedder = _make_embedder()
        model = _make_fake_model()

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=model)
        ctx.__aexit__ = AsyncMock(return_value=False)
        embedder._model_manager.acquire = MagicMock(return_value=ctx)

        chunked = _make_chunked_result()

        with (
            patch("steps.embedder._store_chunks_chroma"),
            patch("steps.embedder._ensure_fts_table"),
            patch(
                "steps.embedder._store_chunks_fts",
                side_effect=StorageError("FTS5 오류"),
            ),
            pytest.raises(StorageError, match="FTS5 오류"),
        ):
            await embedder.embed(chunked)

    @pytest.mark.asyncio
    async def test_model_manager_acquire_호출(self) -> None:
        """ModelLoadManager.acquire가 'e5' 이름으로 호출된다."""
        embedder = _make_embedder()
        model = _make_fake_model()

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=model)
        ctx.__aexit__ = AsyncMock(return_value=False)
        embedder._model_manager.acquire = MagicMock(return_value=ctx)

        chunked = _make_chunked_result()

        with (
            patch("steps.embedder._store_chunks_chroma"),
            patch("steps.embedder._ensure_fts_table"),
            patch("steps.embedder._store_chunks_fts"),
        ):
            await embedder.embed(chunked)

        # acquire가 "e5"로 호출되었는지 확인
        embedder._model_manager.acquire.assert_called_once()
        call_args = embedder._model_manager.acquire.call_args
        assert call_args[0][0] == "e5"

    @pytest.mark.asyncio
    async def test_다수_청크_임베딩(self) -> None:
        """여러 청크가 올바르게 임베딩되고 저장된다."""
        embedder = _make_embedder(_make_config(batch_size=2))
        model = _make_fake_model()

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=model)
        ctx.__aexit__ = AsyncMock(return_value=False)
        embedder._model_manager.acquire = MagicMock(return_value=ctx)

        chunks = [_make_chunk(text=f"청크 {i}", chunk_index=i) for i in range(5)]
        chunked = _make_chunked_result(chunks)

        with (
            patch("steps.embedder._store_chunks_chroma") as mock_chroma,
            patch("steps.embedder._ensure_fts_table"),
            patch("steps.embedder._store_chunks_fts") as mock_fts,
        ):
            result = await embedder.embed(chunked)

        assert result.total_chunks == 5
        # ChromaDB와 FTS5에 5개 청크가 전달되었는지 확인
        chroma_chunks = mock_chroma.call_args[0][0]
        assert len(chroma_chunks) == 5
        fts_chunks = mock_fts.call_args[0][0]
        assert len(fts_chunks) == 5


# === ChromaDB 저장 함수 테스트 ===


class TestStoreChunksChroma:
    """ChromaDB 저장 함수 테스트."""

    def _make_mock_chromadb(self) -> MagicMock:
        """chromadb 모듈 목을 생성한다."""
        return MagicMock()

    def _make_test_chunk(self) -> EmbeddedChunk:
        """테스트용 EmbeddedChunk를 생성한다."""
        return EmbeddedChunk(
            chunk_id="m1_chunk_0000",
            text="테스트",
            embedding=[0.1] * 384,
            meeting_id="m1",
            date="2026-03-04",
            speakers=["A"],
            start_time=0.0,
            end_time=5.0,
        )

    def test_정상_저장(self) -> None:
        """ChromaDB에 청크가 정상적으로 저장된다."""
        mock_chromadb = self._make_mock_chromadb()
        mock_collection = MagicMock()
        mock_collection.get.return_value = {"ids": []}
        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = mock_collection
        mock_chromadb.PersistentClient.return_value = mock_client

        chunks = [self._make_test_chunk()]

        with patch.dict("sys.modules", {"chromadb": mock_chromadb}):
            _store_chunks_chroma(chunks, Path("/tmp/chroma"), "m1")

        # collection.add가 호출되었는지 확인
        mock_collection.add.assert_called_once()
        add_args = mock_collection.add.call_args
        assert add_args[1]["ids"] == ["m1_chunk_0000"]
        assert add_args[1]["documents"] == ["테스트"]

    def test_멱등성_기존_삭제(self) -> None:
        """동일 meeting_id의 기존 데이터가 삭제 후 재삽입된다."""
        mock_chromadb = self._make_mock_chromadb()
        mock_collection = MagicMock()
        mock_collection.get.return_value = {"ids": ["m1_chunk_0000", "m1_chunk_0001"]}
        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = mock_collection
        mock_chromadb.PersistentClient.return_value = mock_client

        chunks = [
            EmbeddedChunk(
                chunk_id="m1_chunk_0000",
                text="새 텍스트",
                embedding=[0.1] * 384,
                meeting_id="m1",
                date="2026-03-04",
                speakers=["A"],
                start_time=0.0,
                end_time=5.0,
            ),
        ]

        with patch.dict("sys.modules", {"chromadb": mock_chromadb}):
            _store_chunks_chroma(chunks, Path("/tmp/chroma"), "m1")

        # 기존 데이터 삭제 확인
        mock_collection.delete.assert_called_once_with(ids=["m1_chunk_0000", "m1_chunk_0001"])
        # 새 데이터 삽입 확인
        mock_collection.add.assert_called_once()

    def test_cosine_거리_메트릭(self) -> None:
        """ChromaDB 컬렉션이 cosine 거리 메트릭으로 생성된다."""
        mock_chromadb = self._make_mock_chromadb()
        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_collection.get.return_value = {"ids": []}
        mock_client.get_or_create_collection.return_value = mock_collection
        mock_chromadb.PersistentClient.return_value = mock_client

        chunks = [self._make_test_chunk()]

        with patch.dict("sys.modules", {"chromadb": mock_chromadb}):
            _store_chunks_chroma(chunks, Path("/tmp/chroma"), "m1")

        # cosine 메트릭 확인
        create_args = mock_client.get_or_create_collection.call_args
        assert create_args[1]["metadata"] == {"hnsw:space": "cosine"}

    def test_저장_실패_시_StorageError(self) -> None:
        """ChromaDB 저장 실패 시 StorageError가 발생한다."""
        mock_chromadb = self._make_mock_chromadb()
        mock_chromadb.PersistentClient.side_effect = Exception("연결 실패")

        chunks = [self._make_test_chunk()]

        with (
            patch.dict("sys.modules", {"chromadb": mock_chromadb}),
            pytest.raises(StorageError, match="ChromaDB 저장 실패"),
        ):
            _store_chunks_chroma(chunks, Path("/tmp/chroma"), "m1")


# === _load_model 테스트 ===


class TestLoadModel:
    """모델 로드 테스트."""

    def test_모델_로드_실패_시_ModelLoadError(self) -> None:
        """sentence_transformers import 실패 시 ModelLoadError가 발생한다."""
        embedder = _make_embedder()

        # sentence_transformers import 시 예외 발생하도록 설정
        mock_st = MagicMock()
        mock_st.SentenceTransformer.side_effect = Exception("모델 파일 없음")

        with (
            patch.dict("sys.modules", {"sentence_transformers": mock_st}),
            pytest.raises(ModelLoadError, match="임베딩 모델 로드 실패"),
        ):
            embedder._load_model()

    def test_정상_모델_로드(self) -> None:
        """sentence_transformers가 정상적으로 로드된다."""
        embedder = _make_embedder(_make_config(device="cpu"))

        mock_model = MagicMock()
        mock_st = MagicMock()
        mock_st.SentenceTransformer.return_value = mock_model

        with patch.dict("sys.modules", {"sentence_transformers": mock_st}):
            result = embedder._load_model()

        assert result == mock_model
        mock_st.SentenceTransformer.assert_called_once_with(
            "intfloat/multilingual-e5-small",
            device="cpu",
        )


# === NFC 정규화 테스트 ===


class TestNFCNormalization:
    """NFC 정규화 관련 테스트."""

    def test_passage_접두사에_nfc_적용(self) -> None:
        """generate_embeddings에서 NFC 정규화가 적용된다."""
        embedder = _make_embedder()
        model = _make_fake_model()

        # NFD로 분해된 한국어 ("한국어")
        nfd_text = "\u1112\u1161\u11ab\u1100\u116e\u11a8\u110b\u1165"

        embedder._generate_embeddings(model, [nfd_text])

        call_args = model.encode.call_args
        encoded_text = call_args[0][0][0]
        # NFC로 정규화되어야 함
        assert encoded_text.startswith("passage: ")
        # NFC 정규화 확인 (분해된 자모가 아닌 완성형)
        import unicodedata

        text_without_prefix = encoded_text[len("passage: ") :]
        assert text_without_prefix == unicodedata.normalize("NFC", nfd_text)
