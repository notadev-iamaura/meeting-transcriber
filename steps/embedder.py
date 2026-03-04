"""
임베딩 + 저장 모듈 (Embedding & Storage Module)

목적: RAG 청크를 벡터 임베딩으로 변환하고 ChromaDB + SQLite FTS5에 동시 저장한다.
주요 기능:
    - sentence_transformers로 multilingual-e5-small 모델 로드 (MPS 가속)
    - passage: 접두사 자동 추가 (e5 모델 요구사항)
    - 배치 임베딩 (config.embedding.batch_size)
    - ChromaDB PersistentClient에 벡터 저장
    - SQLite FTS5에 전문 검색 인덱스 저장
    - ModelLoadManager를 통한 모델 라이프사이클 관리
    - JSON 체크포인트 저장/복원 지원
    - 비동기(async) 인터페이스 지원
의존성: config 모듈, core/model_manager 모듈, sentence_transformers, chromadb
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from config import AppConfig, get_config
from core.model_manager import ModelLoadManager, get_model_manager
from steps.chunker import Chunk, ChunkedResult

logger = logging.getLogger(__name__)

# ChromaDB 컬렉션 이름
_CHROMA_COLLECTION_NAME = "meeting_chunks"

# FTS5 테이블 이름
_FTS_TABLE_NAME = "chunks_fts"


# === 에러 계층 ===


class EmbeddingError(Exception):
    """임베딩 처리 중 발생하는 에러의 기본 클래스."""


class ModelLoadError(EmbeddingError):
    """임베딩 모델 로드 실패 시 발생한다."""


class StorageError(EmbeddingError):
    """ChromaDB 또는 FTS5 저장 실패 시 발생한다."""


class EmptyChunksError(EmbeddingError):
    """임베딩할 청크가 비어있을 때 발생한다."""


# === 결과 데이터 클래스 ===


@dataclass
class EmbeddedChunk:
    """임베딩된 단일 청크를 나타내는 데이터 클래스.

    Attributes:
        chunk_id: 청크 고유 식별자 (meeting_id + chunk_index)
        text: 원본 텍스트 (passage 접두사 미포함)
        embedding: 384차원 벡터 (저장 시에는 리스트로 변환)
        meeting_id: 회의 식별자
        date: 회의 날짜 문자열
        speakers: 포함된 화자 목록
        start_time: 시작 시간 (초)
        end_time: 종료 시간 (초)
        chunk_index: 청크 순서 인덱스
    """

    chunk_id: str
    text: str
    embedding: list[float]
    meeting_id: str
    date: str
    speakers: list[str]
    start_time: float
    end_time: float
    chunk_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화용).

        Returns:
            청크 데이터 딕셔너리 (embedding 제외)
        """
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "meeting_id": self.meeting_id,
            "date": self.date,
            "speakers": self.speakers,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "chunk_index": self.chunk_index,
        }


@dataclass
class EmbeddedResult:
    """전체 임베딩 결과를 담는 데이터 클래스.

    Attributes:
        chunks: 임베딩된 청크 목록
        meeting_id: 회의 식별자
        date: 회의 날짜 문자열
        total_chunks: 처리된 청크 수
        embedding_dimension: 벡터 차원 수
        chroma_stored: ChromaDB 저장 성공 여부
        fts_stored: FTS5 저장 성공 여부
    """

    chunks: list[EmbeddedChunk]
    meeting_id: str
    date: str
    total_chunks: int = 0
    embedding_dimension: int = 384
    chroma_stored: bool = False
    fts_stored: bool = False

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환한다 (JSON 직렬화/체크포인트 저장용).

        Returns:
            전체 임베딩 결과 딕셔너리 (embedding 벡터 제외)
        """
        return {
            "chunks": [c.to_dict() for c in self.chunks],
            "meeting_id": self.meeting_id,
            "date": self.date,
            "total_chunks": self.total_chunks,
            "embedding_dimension": self.embedding_dimension,
            "chroma_stored": self.chroma_stored,
            "fts_stored": self.fts_stored,
        }

    def save_checkpoint(self, output_path: Path) -> None:
        """임베딩 결과를 JSON 파일로 저장한다 (체크포인트).

        벡터 데이터는 ChromaDB에 저장되어 있으므로 체크포인트에는 포함하지 않는다.

        Args:
            output_path: 저장할 JSON 파일 경로
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info(f"임베딩 체크포인트 저장: {output_path}")

    @classmethod
    def from_checkpoint(cls, checkpoint_path: Path) -> EmbeddedResult:
        """체크포인트 JSON 파일에서 임베딩 결과를 복원한다.

        벡터 데이터는 ChromaDB에서 직접 조회해야 한다.

        Args:
            checkpoint_path: 체크포인트 JSON 파일 경로

        Returns:
            복원된 EmbeddedResult 인스턴스

        Raises:
            FileNotFoundError: 체크포인트 파일이 없을 때
            json.JSONDecodeError: JSON 파싱 실패 시
        """
        with open(checkpoint_path, encoding="utf-8") as f:
            data = json.load(f)

        chunks = [
            EmbeddedChunk(
                chunk_id=c["chunk_id"],
                text=c["text"],
                embedding=[],  # 벡터는 ChromaDB에서 조회
                meeting_id=c["meeting_id"],
                date=c["date"],
                speakers=c["speakers"],
                start_time=c["start_time"],
                end_time=c["end_time"],
                chunk_index=c.get("chunk_index", 0),
            )
            for c in data.get("chunks", [])
        ]

        return cls(
            chunks=chunks,
            meeting_id=data.get("meeting_id", ""),
            date=data.get("date", ""),
            total_chunks=data.get("total_chunks", 0),
            embedding_dimension=data.get("embedding_dimension", 384),
            chroma_stored=data.get("chroma_stored", False),
            fts_stored=data.get("fts_stored", False),
        )


# === FTS5 관리 ===


def _ensure_fts_table(db_path: Path) -> None:
    """FTS5 테이블이 없으면 생성한다.

    unicode61 토크나이저를 사용하여 한국어 검색을 지원한다.

    Args:
        db_path: SQLite 데이터베이스 파일 경로
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE_NAME}
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
        logger.debug(f"FTS5 테이블 확인/생성 완료: {db_path}")
    finally:
        conn.close()


def _store_chunks_fts(
    chunks: list[EmbeddedChunk],
    db_path: Path,
    meeting_id: str,
) -> None:
    """청크를 FTS5 테이블에 저장한다.

    동일 meeting_id의 기존 데이터를 삭제 후 재삽입한다 (멱등성).

    Args:
        chunks: 저장할 임베딩된 청크 목록
        db_path: SQLite 데이터베이스 파일 경로
        meeting_id: 회의 식별자

    Raises:
        StorageError: FTS5 저장 실패 시
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # 멱등성: 기존 데이터 삭제
        conn.execute(
            f"DELETE FROM {_FTS_TABLE_NAME} WHERE meeting_id = ?",
            (meeting_id,),
        )
        # 새 데이터 삽입
        for chunk in chunks:
            conn.execute(
                f"""
                INSERT INTO {_FTS_TABLE_NAME}
                    (chunk_id, text, meeting_id, date, speakers,
                     start_time, end_time, chunk_index)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk.chunk_id,
                    chunk.text,
                    chunk.meeting_id,
                    chunk.date,
                    ",".join(chunk.speakers),
                    chunk.start_time,
                    chunk.end_time,
                    chunk.chunk_index,
                ),
            )
        conn.commit()
        logger.info(
            f"FTS5 저장 완료: meeting_id={meeting_id}, "
            f"{len(chunks)}개 청크"
        )
    except Exception as e:
        conn.rollback()
        raise StorageError(f"FTS5 저장 실패: {e}") from e
    finally:
        conn.close()


# === ChromaDB 관리 ===


def _store_chunks_chroma(
    chunks: list[EmbeddedChunk],
    chroma_dir: Path,
    meeting_id: str,
) -> None:
    """청크를 ChromaDB에 저장한다.

    PersistentClient를 사용하여 디스크에 영구 저장한다.
    동일 meeting_id의 기존 데이터를 삭제 후 재삽입한다 (멱등성).

    Args:
        chunks: 저장할 임베딩된 청크 목록
        chroma_dir: ChromaDB 저장 디렉토리 경로
        meeting_id: 회의 식별자

    Raises:
        StorageError: ChromaDB 저장 실패 시
    """
    import chromadb  # lazy import: chromadb가 무거우므로 필요 시에만 로드

    try:
        chroma_dir.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(chroma_dir))
        collection = client.get_or_create_collection(
            name=_CHROMA_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

        # 멱등성: 기존 데이터 삭제
        existing = collection.get(
            where={"meeting_id": meeting_id},
        )
        if existing["ids"]:
            collection.delete(ids=existing["ids"])
            logger.debug(
                f"ChromaDB 기존 데이터 삭제: meeting_id={meeting_id}, "
                f"{len(existing['ids'])}개"
            )

        # 배치 삽입
        ids = [c.chunk_id for c in chunks]
        embeddings = [c.embedding for c in chunks]
        documents = [c.text for c in chunks]
        metadatas = [
            {
                "meeting_id": c.meeting_id,
                "date": c.date,
                "speakers": ",".join(c.speakers),
                "start_time": c.start_time,
                "end_time": c.end_time,
                "chunk_index": c.chunk_index,
            }
            for c in chunks
        ]

        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )
        logger.info(
            f"ChromaDB 저장 완료: meeting_id={meeting_id}, "
            f"{len(chunks)}개 청크"
        )
    except Exception as e:
        raise StorageError(f"ChromaDB 저장 실패: {e}") from e


# === 메인 클래스 ===


class Embedder:
    """RAG 검색용 임베딩 생성 및 저장 모듈.

    multilingual-e5-small 모델로 청크를 벡터화하고,
    ChromaDB(벡터 검색)와 SQLite FTS5(키워드 검색)에 동시 저장한다.
    ModelLoadManager를 통해 모델 수명을 관리하여 메모리 사용을 제어한다.

    Args:
        config: 애플리케이션 설정 (None이면 싱글턴 사용)
        model_manager: 모델 로드 매니저 (None이면 싱글턴 사용)

    사용 예시:
        embedder = Embedder(config, model_manager)
        result = await embedder.embed(chunked_result)
        print(f"저장 완료: ChromaDB={result.chroma_stored}, FTS5={result.fts_stored}")
    """

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        model_manager: Optional[ModelLoadManager] = None,
    ) -> None:
        """Embedder를 초기화한다.

        Args:
            config: 애플리케이션 설정 (None이면 get_config() 사용)
            model_manager: 모델 로드 매니저 (None이면 get_model_manager() 사용)
        """
        self._config = config or get_config()
        self._model_manager = model_manager or get_model_manager()

        # 임베딩 설정 캐시
        self._model_name = self._config.embedding.model_name
        self._dimension = self._config.embedding.dimension
        self._device = self._config.embedding.device
        self._passage_prefix = self._config.embedding.passage_prefix
        self._batch_size = self._config.embedding.batch_size

        # 저장소 경로
        self._chroma_dir = self._config.paths.resolved_chroma_db_dir
        self._meetings_db = self._config.paths.resolved_meetings_db

        logger.info(
            f"Embedder 초기화: model={self._model_name}, "
            f"dim={self._dimension}, device={self._device}, "
            f"batch_size={self._batch_size}"
        )

    def _load_model(self) -> Any:
        """sentence_transformers 모델을 로드한다.

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
                f"임베딩 모델 로드 완료: {self._model_name} "
                f"(device={self._device})"
            )
            return model
        except Exception as e:
            raise ModelLoadError(
                f"임베딩 모델 로드 실패: {self._model_name} - {e}"
            ) from e

    def _generate_embeddings(
        self,
        model: Any,
        texts: list[str],
    ) -> list[list[float]]:
        """텍스트 목록을 벡터로 변환한다.

        passage: 접두사를 자동 추가하고 배치 단위로 인코딩한다.

        Args:
            model: SentenceTransformer 모델 인스턴스
            texts: 임베딩할 텍스트 목록

        Returns:
            384차원 벡터 목록
        """
        # passage: 접두사 추가 (e5 모델 요구사항)
        prefixed_texts = [
            f"{self._passage_prefix}{text}" for text in texts
        ]

        # NFC 정규화 적용
        prefixed_texts = [
            unicodedata.normalize("NFC", t) for t in prefixed_texts
        ]

        # 배치 인코딩
        embeddings = model.encode(
            prefixed_texts,
            batch_size=self._batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
        )

        # numpy 배열을 Python 리스트로 변환
        return [emb.tolist() for emb in embeddings]

    def _process_chunks(
        self,
        model: Any,
        chunked_result: ChunkedResult,
    ) -> list[EmbeddedChunk]:
        """청크를 임베딩하고 EmbeddedChunk 목록으로 변환한다 (동기 메서드).

        Args:
            model: SentenceTransformer 모델 인스턴스
            chunked_result: 청크 분할 결과

        Returns:
            임베딩된 청크 목록
        """
        chunks = chunked_result.chunks
        texts = [chunk.text for chunk in chunks]

        logger.info(f"임베딩 생성 시작: {len(texts)}개 텍스트")

        # 배치 단위로 임베딩 생성
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            batch_texts = texts[i:i + self._batch_size]
            batch_embeddings = self._generate_embeddings(model, batch_texts)
            all_embeddings.extend(batch_embeddings)
            logger.debug(
                f"배치 임베딩 완료: {i + len(batch_texts)}/{len(texts)}"
            )

        # EmbeddedChunk 목록 생성
        embedded_chunks: list[EmbeddedChunk] = []
        for chunk, embedding in zip(chunks, all_embeddings):
            chunk_id = f"{chunked_result.meeting_id}_chunk_{chunk.chunk_index:04d}"
            embedded_chunks.append(EmbeddedChunk(
                chunk_id=chunk_id,
                text=chunk.text,
                embedding=embedding,
                meeting_id=chunked_result.meeting_id,
                date=chunked_result.date,
                speakers=chunk.speakers,
                start_time=chunk.start_time,
                end_time=chunk.end_time,
                chunk_index=chunk.chunk_index,
            ))

        logger.info(
            f"임베딩 생성 완료: {len(embedded_chunks)}개, "
            f"차원={self._dimension}"
        )
        return embedded_chunks

    async def embed(
        self,
        chunked_result: ChunkedResult,
    ) -> EmbeddedResult:
        """청크를 임베딩하고 ChromaDB + FTS5에 저장한다.

        1. ModelLoadManager로 e5-small 모델 로드
        2. 배치 임베딩 생성
        3. ChromaDB에 벡터 저장
        4. SQLite FTS5에 전문 검색 인덱스 저장
        5. 모델 언로드

        Args:
            chunked_result: 청크 분할 결과

        Returns:
            임베딩 및 저장 결과 (EmbeddedResult)

        Raises:
            EmptyChunksError: 청크가 비어있을 때
            ModelLoadError: 모델 로드 실패 시
            EmbeddingError: 임베딩 처리 중 오류 발생 시
        """
        if not chunked_result.chunks:
            raise EmptyChunksError("임베딩할 청크가 비어있습니다.")

        meeting_id = chunked_result.meeting_id
        logger.info(
            f"임베딩 파이프라인 시작: meeting_id={meeting_id}, "
            f"청크 {len(chunked_result.chunks)}개"
        )

        # 1. 모델 로드 및 임베딩 생성
        async with self._model_manager.acquire("e5", self._load_model) as model:
            # 별도 스레드에서 임베딩 실행 (이벤트 루프 블로킹 방지)
            embedded_chunks = await asyncio.to_thread(
                self._process_chunks, model, chunked_result
            )

        # 2. 결과 객체 생성
        result = EmbeddedResult(
            chunks=embedded_chunks,
            meeting_id=meeting_id,
            date=chunked_result.date,
            total_chunks=len(embedded_chunks),
            embedding_dimension=self._dimension,
        )

        # 3. ChromaDB 저장
        try:
            await asyncio.to_thread(
                _store_chunks_chroma,
                embedded_chunks,
                self._chroma_dir,
                meeting_id,
            )
            result.chroma_stored = True
        except StorageError:
            logger.exception(f"ChromaDB 저장 실패: meeting_id={meeting_id}")

        # 4. FTS5 저장
        try:
            await asyncio.to_thread(
                _ensure_fts_table, self._meetings_db
            )
            await asyncio.to_thread(
                _store_chunks_fts,
                embedded_chunks,
                self._meetings_db,
                meeting_id,
            )
            result.fts_stored = True
        except StorageError:
            logger.exception(f"FTS5 저장 실패: meeting_id={meeting_id}")

        # 5. 결과 로깅
        logger.info(
            f"임베딩 파이프라인 완료: meeting_id={meeting_id}, "
            f"청크 {result.total_chunks}개, "
            f"ChromaDB={'성공' if result.chroma_stored else '실패'}, "
            f"FTS5={'성공' if result.fts_stored else '실패'}"
        )

        return result
