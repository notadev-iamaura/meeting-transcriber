"""A/B 테스트 API 통합 테스트.

목적: api/routes.py 에 추가된 7개 A/B 테스트 엔드포인트를 httpx AsyncClient +
ASGI transport 로 검증한다. 러너 내부 LLM/STT 호출은 monkeypatch 로 stub 하고,
ws_manager 의 broadcast_event 호출 여부와 payload 를 Mock 으로 검증한다.

의존성: pytest, pytest-asyncio, httpx, fastapi
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from config import AppConfig, PathsConfig
from core import ab_test_runner, ab_test_store
from core.ab_test_runner import (
    LlmScope,
    ModelSpec,
    new_test_id,
)
from steps.corrector import CorrectedResult, CorrectedUtterance
from steps.diarizer import DiarizationResult, DiarizationSegment
from steps.merger import MergedResult, MergedUtterance
from steps.summarizer import SummaryResult
from steps.transcriber import TranscriptResult, TranscriptSegment


# ============================================================
# 헬퍼: 최소 FastAPI 앱 생성 (lifespan 생략, 라우터만 등록)
# ============================================================


def _make_minimal_app(config: AppConfig) -> FastAPI:
    """테스트용 최소 FastAPI 앱을 생성한다.

    Args:
        config: 격리된 AppConfig

    Returns:
        FastAPI 인스턴스
    """
    from api.routes import router

    app = FastAPI()
    app.include_router(router)
    app.state.config = config
    app.state.ws_manager = None
    app.state.model_manager = None
    return app


# ============================================================
# 픽스처
# ============================================================


@pytest.fixture
def tmp_config(tmp_path: Path) -> AppConfig:
    """tmp_path 기반 AppConfig."""
    cfg = AppConfig()
    cfg = cfg.model_copy(
        update={"paths": PathsConfig(base_dir=str(tmp_path))}
    )
    cfg.paths.resolved_outputs_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.resolved_checkpoints_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.resolved_audio_input_dir.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def sample_merged() -> MergedResult:
    """최소 MergedResult."""
    return MergedResult(
        utterances=[
            MergedUtterance(text="안녕하세요", speaker="SPEAKER_00", start=0.0, end=1.0),
            MergedUtterance(text="반갑습니다", speaker="SPEAKER_01", start=1.0, end=2.0),
        ],
        num_speakers=2,
        audio_path="/fake/input.wav",
    )


@pytest.fixture
def meeting_id_with_merge(tmp_config: AppConfig, sample_merged: MergedResult) -> str:
    """merge.json + input.wav 가 준비된 가짜 회의 ID."""
    mid = "meeting_20260409-100000"
    # 체크포인트 디렉터리 (merge.json 등)
    ckpt_dir = tmp_config.paths.resolved_checkpoints_dir / mid
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    sample_merged.save_checkpoint(ckpt_dir / "merge.json")
    # WAV 는 audio_input/ 에 저장
    (tmp_config.paths.resolved_audio_input_dir / f"{mid}.wav").write_bytes(
        b"RIFF....WAVEfmt "
    )
    # outputs 에 metadata (기존 회의 조회용)
    out_dir = tmp_config.paths.resolved_outputs_dir / mid
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metadata.json").write_text(
        json.dumps({"meeting_id": mid}), encoding="utf-8"
    )
    return mid


@pytest.fixture
def app(tmp_config: AppConfig) -> FastAPI:
    """최소 FastAPI 앱."""
    return _make_minimal_app(tmp_config)


class _DummyManager:
    """ModelLoadManager stub."""

    async def unload_model(self) -> None:
        return None


def _make_corrected(merged: MergedResult) -> CorrectedResult:
    """stub CorrectedResult."""
    return CorrectedResult(
        utterances=[
            CorrectedUtterance(
                text=u.text + "(수정)",
                original_text=u.text,
                speaker=u.speaker,
                start=u.start,
                end=u.end,
                was_corrected=True,
            )
            for u in merged.utterances
        ],
        num_speakers=merged.num_speakers,
        audio_path=merged.audio_path,
        total_corrected=len(merged.utterances),
    )


def _make_summary(markdown: str = "## 요약\n\n테스트") -> SummaryResult:
    """stub SummaryResult."""
    return SummaryResult(
        markdown=markdown,
        audio_path="/fake/input.wav",
        num_speakers=2,
        speakers=["SPEAKER_00", "SPEAKER_01"],
        num_utterances=2,
    )


@pytest.fixture
def patch_llm_steps(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Corrector/Summarizer stub 교체."""
    counts = {"correct": 0, "summarize": 0}

    class StubCorrector:
        def __init__(self, config: Any, manager: Any) -> None:
            pass

        async def correct(self, merged: MergedResult) -> CorrectedResult:
            counts["correct"] += 1
            return _make_corrected(merged)

    class StubSummarizer:
        def __init__(self, config: Any, manager: Any) -> None:
            pass

        async def summarize(self, corrected: CorrectedResult) -> SummaryResult:
            counts["summarize"] += 1
            return _make_summary()

    monkeypatch.setattr(ab_test_runner, "Corrector", StubCorrector)
    monkeypatch.setattr(ab_test_runner, "Summarizer", StubSummarizer)
    return counts


@pytest.fixture
def patch_stt_steps(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Transcriber/Diarizer/Merger stub 교체."""
    counts = {"transcribe": 0, "diarize": 0, "merge": 0}

    class StubTranscriber:
        def __init__(self, config: Any, manager: Any) -> None:
            pass

        async def transcribe(self, wav_path: Path) -> TranscriptResult:
            counts["transcribe"] += 1
            return TranscriptResult(
                segments=[
                    TranscriptSegment(text="전사 결과", start=0.0, end=1.0),
                ],
                full_text="전사 결과",
                language="ko",
                audio_path=str(wav_path),
            )

    class StubDiarizer:
        def __init__(self, config: Any, manager: Any) -> None:
            pass

        async def diarize(self, wav_path: Path) -> DiarizationResult:
            counts["diarize"] += 1
            return DiarizationResult(
                segments=[
                    DiarizationSegment(speaker="SPEAKER_00", start=0.0, end=1.0),
                ],
                audio_path=str(wav_path),
                num_speakers=1,
            )

    class StubMerger:
        async def merge(
            self, transcript: TranscriptResult, diarize: DiarizationResult
        ) -> MergedResult:
            counts["merge"] += 1
            return MergedResult(
                utterances=[
                    MergedUtterance(text="전사 결과", speaker="SPEAKER_00", start=0.0, end=1.0),
                ],
                num_speakers=1,
                audio_path=transcript.audio_path,
            )

    monkeypatch.setattr(ab_test_runner, "Transcriber", StubTranscriber)
    monkeypatch.setattr(ab_test_runner, "Diarizer", StubDiarizer)
    monkeypatch.setattr(ab_test_runner, "Merger", StubMerger)
    return counts


# ============================================================
# 요청 바디 헬퍼
# ============================================================


def _llm_body(meeting_id: str) -> dict[str, Any]:
    """LLM A/B 테스트 요청 바디."""
    return {
        "source_meeting_id": meeting_id,
        "variant_a": {"label": "A", "model_id": "model-a"},
        "variant_b": {"label": "B", "model_id": "model-b"},
        "scope": {"correct": True, "summarize": True},
    }


def _stt_body(meeting_id: str) -> dict[str, Any]:
    """STT A/B 테스트 요청 바디."""
    return {
        "source_meeting_id": meeting_id,
        "variant_a": {"label": "A", "model_id": "stt-model-a"},
        "variant_b": {"label": "B", "model_id": "stt-model-b"},
        "allow_diarize_rerun": True,
    }


# ============================================================
# 백그라운드 태스크 완료 대기 헬퍼
# ============================================================


async def _wait_for_task_completion(
    config: AppConfig, test_id: str, timeout: float = 10.0
) -> dict[str, Any]:
    """A/B 테스트가 완료될 때까지 비동기 폴링한다.

    Args:
        config: AppConfig
        test_id: 대기할 테스트 ID
        timeout: 최대 대기 시간 (초)

    Returns:
        최종 metadata
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            meta = ab_test_store.read_metadata(config, test_id)
            if meta.get("status") in ("completed", "failed", "partial_failed", "cancelled"):
                return meta
        except (FileNotFoundError, ValueError):
            pass
        # asyncio.sleep 으로 이벤트 루프에 제어를 양보 → create_task 가 진행됨
        await asyncio.sleep(0.05)
    raise TimeoutError(f"A/B 테스트 {test_id} 가 {timeout}초 내 완료되지 않음")


# ============================================================
# httpx AsyncClient 픽스처
# ============================================================


@pytest.fixture
def async_client(app: FastAPI) -> httpx.AsyncClient:
    """httpx AsyncClient (ASGI transport)."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ============================================================
# POST /api/ab-tests/llm
# ============================================================


class TestPostLlmAbTest:
    @pytest.mark.asyncio
    async def test_해피_패스(
        self,
        async_client: httpx.AsyncClient,
        app: FastAPI,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        patch_llm_steps: dict[str, int],
    ) -> None:
        """202 + test_id 반환 + metadata 파일 생성 확인."""
        app.state.model_manager = _DummyManager()

        resp = await async_client.post(
            "/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge)
        )
        assert resp.status_code == 202
        data = resp.json()
        assert "test_id" in data
        assert ab_test_store.is_valid_test_id(data["test_id"])

        meta = await _wait_for_task_completion(tmp_config, data["test_id"])
        assert meta["status"] == "completed"

    @pytest.mark.asyncio
    async def test_동일_모델_쌍_거부(
        self,
        async_client: httpx.AsyncClient,
        meeting_id_with_merge: str,
    ) -> None:
        """동일 모델이면 400."""
        body = {
            "source_meeting_id": meeting_id_with_merge,
            "variant_a": {"label": "A", "model_id": "same"},
            "variant_b": {"label": "B", "model_id": "same"},
        }
        resp = await async_client.post("/api/ab-tests/llm", json=body)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_존재하지_않는_meeting_id_거부(
        self,
        async_client: httpx.AsyncClient,
    ) -> None:
        """없는 회의 ID 는 404."""
        body = _llm_body("nonexistent_meeting")
        resp = await async_client.post("/api/ab-tests/llm", json=body)
        assert resp.status_code == 404


# ============================================================
# POST /api/ab-tests/stt
# ============================================================


class TestPostSttAbTest:
    @pytest.mark.asyncio
    async def test_해피_패스(
        self,
        async_client: httpx.AsyncClient,
        app: FastAPI,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        patch_stt_steps: dict[str, int],
    ) -> None:
        """202 + test_id 반환."""
        app.state.model_manager = _DummyManager()

        resp = await async_client.post(
            "/api/ab-tests/stt", json=_stt_body(meeting_id_with_merge)
        )
        assert resp.status_code == 202
        data = resp.json()
        assert "test_id" in data
        assert ab_test_store.is_valid_test_id(data["test_id"])

        meta = await _wait_for_task_completion(tmp_config, data["test_id"])
        assert meta["status"] in ("completed", "partial_failed")

    @pytest.mark.asyncio
    async def test_동일_모델_쌍_거부(
        self,
        async_client: httpx.AsyncClient,
        meeting_id_with_merge: str,
    ) -> None:
        """동일 STT 모델이면 400."""
        body = {
            "source_meeting_id": meeting_id_with_merge,
            "variant_a": {"label": "A", "model_id": "same"},
            "variant_b": {"label": "B", "model_id": "same"},
        }
        resp = await async_client.post("/api/ab-tests/stt", json=body)
        assert resp.status_code == 400


# ============================================================
# GET /api/ab-tests
# ============================================================


class TestListAbTests:
    @pytest.mark.asyncio
    async def test_빈_상태(self, async_client: httpx.AsyncClient) -> None:
        """초기 상태에서 빈 목록."""
        resp = await async_client.get("/api/ab-tests")
        assert resp.status_code == 200
        assert resp.json() == {"tests": []}

    @pytest.mark.asyncio
    async def test_여러_항목(
        self,
        async_client: httpx.AsyncClient,
        app: FastAPI,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        patch_llm_steps: dict[str, int],
    ) -> None:
        """테스트 생성 후 목록 조회."""
        app.state.model_manager = _DummyManager()

        resp = await async_client.post(
            "/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge)
        )
        tid = resp.json()["test_id"]
        await _wait_for_task_completion(tmp_config, tid)

        resp2 = await async_client.get("/api/ab-tests")
        assert resp2.status_code == 200
        tests = resp2.json()["tests"]
        assert len(tests) >= 1
        assert any(t["test_id"] == tid for t in tests)

    @pytest.mark.asyncio
    async def test_source_meeting_id_필터(
        self,
        async_client: httpx.AsyncClient,
        app: FastAPI,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        patch_llm_steps: dict[str, int],
    ) -> None:
        """source_meeting_id 쿼리로 필터링."""
        app.state.model_manager = _DummyManager()

        resp = await async_client.post(
            "/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge)
        )
        tid = resp.json()["test_id"]
        await _wait_for_task_completion(tmp_config, tid)

        resp2 = await async_client.get(
            f"/api/ab-tests?source_meeting_id={meeting_id_with_merge}"
        )
        assert len(resp2.json()["tests"]) >= 1

        resp3 = await async_client.get("/api/ab-tests?source_meeting_id=nonexistent")
        assert resp3.json()["tests"] == []


# ============================================================
# GET /api/ab-tests/{test_id}
# ============================================================


class TestGetAbTestById:
    @pytest.mark.asyncio
    async def test_정상_조회(
        self,
        async_client: httpx.AsyncClient,
        app: FastAPI,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        patch_llm_steps: dict[str, int],
    ) -> None:
        """완료된 테스트 상세 조회."""
        app.state.model_manager = _DummyManager()

        resp = await async_client.post(
            "/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge)
        )
        tid = resp.json()["test_id"]
        await _wait_for_task_completion(tmp_config, tid)

        resp2 = await async_client.get(f"/api/ab-tests/{tid}")
        assert resp2.status_code == 200
        data = resp2.json()
        assert "metadata" in data
        assert "variant_a" in data
        assert "variant_b" in data
        assert data["metadata"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_404(self, async_client: httpx.AsyncClient) -> None:
        """존재하지 않는 test_id 는 404."""
        fake_id = new_test_id()
        resp = await async_client.get(f"/api/ab-tests/{fake_id}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_id",
        [
            "invalid-format",
            "ab_00000000-000000_ZZZZZZZZ",
            "ab_20260409-143000_A1B2C3D4",  # 대문자 16진수
        ],
    )
    async def test_path_traversal_거부(
        self, async_client: httpx.AsyncClient, bad_id: str
    ) -> None:
        """유효하지 않은 test_id 형식은 400."""
        resp = await async_client.get(f"/api/ab-tests/{bad_id}")
        assert resp.status_code == 400


# ============================================================
# GET /api/ab-tests/{test_id}/variant/{variant}/summary
# ============================================================


class TestGetSummary:
    @pytest.mark.asyncio
    async def test_정상_조회(
        self,
        async_client: httpx.AsyncClient,
        app: FastAPI,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        patch_llm_steps: dict[str, int],
    ) -> None:
        """summary.md 가 text/markdown 으로 반환되는지 확인."""
        app.state.model_manager = _DummyManager()

        resp = await async_client.post(
            "/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge)
        )
        tid = resp.json()["test_id"]
        await _wait_for_task_completion(tmp_config, tid)

        resp2 = await async_client.get(f"/api/ab-tests/{tid}/variant/a/summary")
        assert resp2.status_code == 200
        assert "text/markdown" in resp2.headers["content-type"]
        assert len(resp2.text) > 0

    @pytest.mark.asyncio
    async def test_invalid_variant_거부(self, async_client: httpx.AsyncClient) -> None:
        """variant=c 는 400."""
        fake_id = new_test_id()
        resp = await async_client.get(f"/api/ab-tests/{fake_id}/variant/c/summary")
        assert resp.status_code == 400


# ============================================================
# DELETE /api/ab-tests/{test_id}
# ============================================================


class TestDeleteAbTest:
    @pytest.mark.asyncio
    async def test_삭제_성공(
        self,
        async_client: httpx.AsyncClient,
        app: FastAPI,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        patch_llm_steps: dict[str, int],
    ) -> None:
        """삭제 후 204 + 디렉터리 제거 확인."""
        app.state.model_manager = _DummyManager()

        resp = await async_client.post(
            "/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge)
        )
        tid = resp.json()["test_id"]
        await _wait_for_task_completion(tmp_config, tid)

        resp2 = await async_client.delete(f"/api/ab-tests/{tid}")
        assert resp2.status_code == 204

        test_dir = ab_test_store.resolve_test_dir(tmp_config, tid)
        assert not test_dir.exists()

    @pytest.mark.asyncio
    async def test_없는_테스트_삭제(self, async_client: httpx.AsyncClient) -> None:
        """존재하지 않는 test_id 삭제는 204 (idempotent)."""
        fake_id = new_test_id()
        resp = await async_client.delete(f"/api/ab-tests/{fake_id}")
        assert resp.status_code == 204


# ============================================================
# POST /api/ab-tests/{test_id}/cancel
# ============================================================


class TestCancelAbTest:
    @pytest.mark.asyncio
    async def test_취소_요청(
        self,
        async_client: httpx.AsyncClient,
    ) -> None:
        """유효한 test_id 에 대한 취소 요청 시 202."""
        tid = new_test_id()
        resp = await async_client.post(f"/api/ab-tests/{tid}/cancel")
        assert resp.status_code == 202
        data = resp.json()
        assert data["test_id"] == tid
        assert data["status"] == "cancelling"

        assert tid in ab_test_runner._cancel_requests
        ab_test_runner._cancel_requests.discard(tid)

    @pytest.mark.asyncio
    async def test_잘못된_test_id_거부(self, async_client: httpx.AsyncClient) -> None:
        """유효하지 않은 test_id 는 400."""
        resp = await async_client.post("/api/ab-tests/invalid-id/cancel")
        assert resp.status_code == 400


# ============================================================
# 원본 회의 미변경 검증
# ============================================================


class TestOriginalMeetingUnchanged:
    @pytest.mark.asyncio
    async def test_원본_meeting_outputs_미변경(
        self,
        async_client: httpx.AsyncClient,
        app: FastAPI,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        patch_llm_steps: dict[str, int],
    ) -> None:
        """A/B 실행 후 원본 회의 디렉터리의 파일이 변경되지 않음을 확인."""
        app.state.model_manager = _DummyManager()

        # outputs/ 와 checkpoints/ 모두 원본 상태 기록
        mdir = tmp_config.paths.resolved_outputs_dir / meeting_id_with_merge
        ckpt_dir = tmp_config.paths.resolved_checkpoints_dir / meeting_id_with_merge
        before = {}
        for d in (mdir, ckpt_dir):
            if d.exists():
                for f in d.iterdir():
                    if f.is_file():
                        before[str(f)] = f.read_bytes()

        resp = await async_client.post(
            "/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge)
        )
        tid = resp.json()["test_id"]
        await _wait_for_task_completion(tmp_config, tid)

        after = {}
        for d in (mdir, ckpt_dir):
            if d.exists():
                for f in d.iterdir():
                    if f.is_file():
                        after[str(f)] = f.read_bytes()

        assert before.keys() == after.keys(), "파일 목록이 변경됨"
        for name in before:
            assert before[name] == after[name], f"{name} 내용이 변경됨"


# ============================================================
# WebSocket 브로드캐스트 검증
# ============================================================


class TestWebSocketBroadcast:
    @pytest.mark.asyncio
    async def test_ws_broadcast_호출됨(
        self,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        patch_llm_steps: dict[str, int],
    ) -> None:
        """A/B 러너 실행 시 ws_manager.broadcast_event 가 호출되는지 확인."""
        mock_ws_manager = MagicMock()
        mock_ws_manager.broadcast_event = AsyncMock(return_value=1)

        app = _make_minimal_app(tmp_config)
        app.state.model_manager = _DummyManager()
        app.state.ws_manager = mock_ws_manager

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge)
            )
            assert resp.status_code == 202
            tid = resp.json()["test_id"]
            await _wait_for_task_completion(tmp_config, tid)

        assert mock_ws_manager.broadcast_event.call_count >= 1

        found_ab_payload = False
        for call in mock_ws_manager.broadcast_event.call_args_list:
            event = call.args[0] if call.args else call.kwargs.get("event")
            if hasattr(event, "data"):
                d = event.data
                if d.get("ab_test_id") and d.get("variant"):
                    found_ab_payload = True
                    assert d["type"] == "step_progress"
                    break

        assert found_ab_payload, "ab_test_id + variant 를 포함하는 브로드캐스트가 없음"

    @pytest.mark.asyncio
    async def test_ws_manager_없어도_정상_동작(
        self,
        async_client: httpx.AsyncClient,
        app: FastAPI,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        patch_llm_steps: dict[str, int],
    ) -> None:
        """ws_manager 가 None 이어도 러너가 정상 완료."""
        app.state.model_manager = _DummyManager()
        app.state.ws_manager = None

        resp = await async_client.post(
            "/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge)
        )
        assert resp.status_code == 202
        tid = resp.json()["test_id"]
        meta = await _wait_for_task_completion(tmp_config, tid)
        assert meta["status"] == "completed"
