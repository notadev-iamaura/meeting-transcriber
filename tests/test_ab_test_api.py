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
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from config import AppConfig, PathsConfig
from core import ab_test_runner, ab_test_store
from core.ab_test_runner import (
    new_test_id,
)
from steps.corrector import CorrectedResult, CorrectedUtterance
from steps.diarizer import DiarizationResult, DiarizationSegment
from steps.merger import MergedResult, MergedUtterance
from steps.summarizer import SummaryResult
from steps.transcriber import TranscriptResult, TranscriptSegment


def _denied_audio_admission(failure_kind_name: str) -> Any:
    """STT A/B admission 매핑용 비수락 결과를 만든다."""
    from core.audio_quality import AudioFailureKind, AudioQualityResult, AudioQualityStatus

    media_invalid = failure_kind_name == "MEDIA_INVALID"
    return AudioQualityResult(
        status=AudioQualityStatus.REJECT if media_invalid else AudioQualityStatus.ERROR,
        mean_volume_db=None,
        duration_seconds=1.0 if media_invalid else None,
        reason=f"admission denied: {failure_kind_name}",
        failure_kind=getattr(AudioFailureKind, failure_kind_name),
    )


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
    raw_queue = MagicMock()
    raw_queue.get_job_by_meeting_id.side_effect = lambda _meeting_id: SimpleNamespace(
        id=1,
        status="completed",
    )
    app.state.job_queue = SimpleNamespace(queue=raw_queue)
    return app


# ============================================================
# 픽스처
# ============================================================


@pytest.fixture
def tmp_config(tmp_path: Path) -> AppConfig:
    """tmp_path 기반 AppConfig."""
    cfg = AppConfig()
    cfg = cfg.model_copy(update={"paths": PathsConfig(base_dir=str(tmp_path))})
    cfg.paths.resolved_outputs_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.resolved_checkpoints_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.resolved_audio_input_dir.mkdir(parents=True, exist_ok=True)
    # 기존 A/B 테스트 fixture는 최소 WAV 헤더만 사용하므로 gate 전용 테스트
    # 외에는 미디어 품질 검증을 비활성화한다.
    cfg.audio_quality.enabled = False
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
    (tmp_config.paths.resolved_audio_input_dir / f"{mid}.wav").write_bytes(b"RIFF....WAVEfmt ")
    # outputs 에 metadata (기존 회의 조회용)
    out_dir = tmp_config.paths.resolved_outputs_dir / mid
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metadata.json").write_text(json.dumps({"meeting_id": mid}), encoding="utf-8")
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

        async def transcribe(self, wav_path: Path, **kwargs) -> TranscriptResult:
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
    @pytest.mark.parametrize("endpoint", ["llm", "stt"])
    async def test_meeting_id_path_separator는_공통으로_400(
        self,
        async_client: httpx.AsyncClient,
        endpoint: str,
    ) -> None:
        """LLM/STT 모두 direct-child가 아닌 ID를 공통으로 거부한다."""
        body = _llm_body("valid/escape") if endpoint == "llm" else _stt_body("valid/escape")

        response = await async_client.post(f"/api/ab-tests/{endpoint}", json=body)

        assert response.status_code == 400

    def test_한국어와_공백_meeting_id는_기존호환을_유지한다(self) -> None:
        """watcher/Pipeline이 만드는 Unicode·공백 단일 요소를 A/B도 허용한다."""
        from api.routers.ab_tests import _validate_meeting_id

        _validate_meeting_id("회의 1")

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

        resp = await async_client.post("/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge))
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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("artifact_name", ["merge.json", "diarize.json"])
    async def test_LLM_AB는_source_artifact_symlink_target을_읽지_않는다(
        self,
        async_client: httpx.AsyncClient,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        monkeypatch: pytest.MonkeyPatch,
        artifact_name: str,
    ) -> None:
        """merge/diarize final symlink는 test ID 생성 전 SECURITY_BLOCKED다."""
        import api.routers.ab_tests as ab_routes

        checkpoint_dir = tmp_config.paths.resolved_checkpoints_dir / meeting_id_with_merge
        artifact = checkpoint_dir / artifact_name
        if artifact.exists():
            artifact.unlink()
        external = tmp_config.paths.resolved_base_dir / f"external-{artifact_name}"
        external.write_text('{"sentinel": "DO-NOT-READ"}', encoding="utf-8")
        artifact.symlink_to(external)
        new_id = MagicMock(return_value="ab_20260409-143000_deadbeef")
        monkeypatch.setattr(ab_routes, "_runner_new_test_id", new_id)

        response = await async_client.post(
            "/api/ab-tests/llm",
            json=_llm_body(meeting_id_with_merge),
        )

        assert response.status_code == 400
        assert "SECURITY_BLOCKED" in response.json()["detail"]
        new_id.assert_not_called()
        assert external.read_text(encoding="utf-8") == '{"sentinel": "DO-NOT-READ"}'
        assert not (tmp_config.paths.resolved_base_dir / "ab_tests").exists()

    @pytest.mark.asyncio
    async def test_LLM_AB는_checkpoint_중간_symlink를_따르지_않는다(
        self,
        async_client: httpx.AsyncClient,
        tmp_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """configured checkpoint 경로 중간 symlink는 외부 JSON 접근 전에 차단한다."""
        import api.routers.ab_tests as ab_routes

        meeting_id = "linked-checkpoint-meeting"
        lexical_parent = tmp_config.paths.resolved_base_dir / "safe"
        lexical_parent.mkdir()
        external_root = tmp_config.paths.resolved_base_dir / "external-checkpoints"
        external_meeting = external_root / "checkpoints" / meeting_id
        external_meeting.mkdir(parents=True)
        sentinel = external_meeting / "merge.json"
        sentinel.write_text('{"sentinel": "DO-NOT-READ"}', encoding="utf-8")
        (lexical_parent / "jump").symlink_to(external_root, target_is_directory=True)
        tmp_config.paths.checkpoints_dir = "safe/jump/checkpoints"
        new_id = MagicMock(return_value="ab_20260409-143000_deadbeef")
        monkeypatch.setattr(ab_routes, "_runner_new_test_id", new_id)

        response = await async_client.post(
            "/api/ab-tests/llm",
            json=_llm_body(meeting_id),
        )

        assert response.status_code == 400
        assert "SECURITY_BLOCKED" in response.json()["detail"]
        new_id.assert_not_called()
        assert sentinel.read_text(encoding="utf-8") == '{"sentinel": "DO-NOT-READ"}'

    @pytest.mark.asyncio
    async def test_LLM_AB는_사용하지않는_diarize_schema에_결합되지않는다(
        self,
        async_client: httpx.AsyncClient,
        app: FastAPI,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        patch_llm_steps: dict[str, int],
    ) -> None:
        """LLM preflight는 diarize path 안전성만 확인하고 schema는 소비하지 않는다."""
        app.state.model_manager = _DummyManager()
        diarize_path = (
            tmp_config.paths.resolved_checkpoints_dir / meeting_id_with_merge / "diarize.json"
        )
        diarize_path.write_text(
            json.dumps({"segments": [{"future_field": 1}]}),
            encoding="utf-8",
        )

        response = await async_client.post(
            "/api/ab-tests/llm",
            json=_llm_body(meeting_id_with_merge),
        )

        assert response.status_code == 202
        metadata = await _wait_for_task_completion(
            tmp_config,
            response.json()["test_id"],
        )
        assert metadata["status"] == "completed"

    @pytest.mark.asyncio
    async def test_LLM_AB는_malformed_merge를_ID생성전_422로_거부한다(
        self,
        async_client: httpx.AsyncClient,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """schema TypeError를 500으로 노출하거나 ghost metadata를 만들지 않는다."""
        import api.routers.ab_tests as ab_routes

        merge_path = (
            tmp_config.paths.resolved_checkpoints_dir / meeting_id_with_merge / "merge.json"
        )
        merge_path.write_text(
            json.dumps({"utterances": [{"unexpected": 1}]}),
            encoding="utf-8",
        )
        new_id = MagicMock(return_value="ab_20260409-143000_deadbeef")
        monkeypatch.setattr(ab_routes, "_runner_new_test_id", new_id)

        response = await async_client.post(
            "/api/ab-tests/llm",
            json=_llm_body(meeting_id_with_merge),
        )

        assert response.status_code == 422
        new_id.assert_not_called()
        assert not (tmp_config.paths.resolved_base_dir / "ab_tests").exists()

    @pytest.mark.asyncio
    async def test_LLM_AB_202직후에도_pending_metadata를_조회할수있다(
        self,
        async_client: httpx.AsyncClient,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """route는 background runner 시작 전에 metadata를 예약해 즉시 GET 404를 막는다."""
        import api.routers.ab_tests as ab_routes

        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking_runner(**kwargs: Any) -> str:
            entered.set()
            await release.wait()
            return str(kwargs["test_id"])

        monkeypatch.setattr(ab_routes, "_runner_run_llm_ab_test", blocking_runner)

        response = await async_client.post(
            "/api/ab-tests/llm",
            json=_llm_body(meeting_id_with_merge),
        )

        assert response.status_code == 202
        test_id = response.json()["test_id"]
        detail = await async_client.get(f"/api/ab-tests/{test_id}")
        assert detail.status_code == 200
        assert detail.json()["metadata"]["status"] == "pending"
        assert ab_test_store.read_metadata(tmp_config, test_id)["status"] == "pending"
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        release.set()
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", ["llm", "stt"])
    async def test_AB_route는_이미_busy면_ID나_metadata없이_409(
        self,
        async_client: httpx.AsyncClient,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        monkeypatch: pytest.MonkeyPatch,
        endpoint: str,
    ) -> None:
        """이미 알려진 lock 점유는 route mutation 전에 동기 409로 응답한다."""
        import api.routers.ab_tests as ab_routes

        new_id = MagicMock(return_value="ab_20260409-143000_deadbeef")
        monkeypatch.setattr(ab_routes, "_runner_new_test_id", new_id)
        lock = ab_test_runner._get_ab_test_lock()
        await lock.acquire()
        try:
            response = await async_client.post(
                f"/api/ab-tests/{endpoint}",
                json=(
                    _llm_body(meeting_id_with_merge)
                    if endpoint == "llm"
                    else _stt_body(meeting_id_with_merge)
                ),
            )
        finally:
            lock.release()

        assert response.status_code == 409
        new_id.assert_not_called()
        assert not (tmp_config.paths.resolved_base_dir / "ab_tests").exists()


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

        resp = await async_client.post("/api/ab-tests/stt", json=_stt_body(meeting_id_with_merge))
        assert resp.status_code == 202
        data = resp.json()
        assert "test_id" in data
        assert ab_test_store.is_valid_test_id(data["test_id"])

        meta = await _wait_for_task_completion(tmp_config, data["test_id"])
        assert meta["status"] in ("completed", "partial_failed")

    @pytest.mark.asyncio
    async def test_route가_admission_identity를_background_runner에_전달한다(
        self,
        async_client: httpx.AsyncClient,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """202 뒤 runner가 동의 대상과 다른 inode를 새 기준으로 채택하지 못한다."""
        import api.routers.ab_tests as ab_routes

        _path, approved_identity = ab_test_runner._inspect_stt_audio_source(
            tmp_config,
            meeting_id_with_merge,
        )
        captured: dict[str, Any] = {}
        called = asyncio.Event()

        async def runner(**kwargs: Any) -> str:
            captured.update(kwargs)
            called.set()
            return str(kwargs["test_id"])

        monkeypatch.setattr(ab_routes, "_runner_run_stt_ab_test", runner)

        response = await async_client.post(
            "/api/ab-tests/stt",
            json=_stt_body(meeting_id_with_merge),
        )
        await asyncio.wait_for(called.wait(), timeout=1.0)

        assert response.status_code == 202
        assert captured["expected_source_identity"] == approved_identity

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

    @pytest.mark.asyncio
    async def test_진행중인_회의는_STT_AB를_동시에_시작하지_않는다(
        self,
        async_client: httpx.AsyncClient,
        app: FastAPI,
        meeting_id_with_merge: str,
    ) -> None:
        """main 전사와 A/B 외부 업로드가 같은 회의에서 중복 실행되지 않는다."""
        app.state.job_queue.queue.get_job_by_meeting_id.return_value = SimpleNamespace(
            status="transcribing"
        )
        app.state.job_queue.queue.get_job_by_meeting_id.side_effect = None

        response = await async_client.post(
            "/api/ab-tests/stt",
            json=_stt_body(meeting_id_with_merge),
        )

        assert response.status_code == 409
        assert "완료된 회의만" in response.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("failure_kind_name", "expected_status"),
        [
            ("MEDIA_INVALID", 422),
            ("SOURCE_BUSY", 409),
            ("INFRA_UNAVAILABLE", 503),
            ("SECURITY_BLOCKED", 400),
        ],
    )
    async def test_STT_AB는_audio_ACCEPT_전에_task를_생성하지_않는다(
        self,
        async_client: httpx.AsyncClient,
        app: FastAPI,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        monkeypatch: pytest.MonkeyPatch,
        failure_kind_name: str,
        expected_status: int,
    ) -> None:
        """A/B 요청은 비수락 오디오에 test id나 runner task를 만들면 안 된다."""
        import api.routers.ab_tests as ab_routes
        import core.audio_quality as audio_quality

        admission = MagicMock(return_value=_denied_audio_admission(failure_kind_name))
        new_id = MagicMock(return_value="ab_20260409-143000_deadbeef")
        runner = AsyncMock(return_value=None)
        monkeypatch.setattr(audio_quality, "validate_audio_quality", admission)
        monkeypatch.setattr(ab_routes, "validate_audio_quality", admission, raising=False)
        monkeypatch.setattr(ab_routes, "_runner_new_test_id", new_id)
        monkeypatch.setattr(ab_routes, "_runner_run_stt_ab_test", runner)
        tmp_config.audio_quality.enabled = True

        response = await async_client.post(
            "/api/ab-tests/stt",
            json=_stt_body(meeting_id_with_merge),
        )

        assert response.status_code == expected_status
        assert failure_kind_name in response.json()["detail"]
        admission.assert_called_once()
        new_id.assert_not_called()
        runner.assert_not_called()
        assert not (tmp_config.paths.resolved_base_dir / "ab_tests").exists()

    @pytest.mark.asyncio
    async def test_STT_AB는_gate_disabled여도_symlink_target을_열지_않는다(
        self,
        async_client: httpx.AsyncClient,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """quality 정책을 꺼도 최종 symlink는 task 생성 전에 SECURITY_BLOCKED다."""
        import api.routers.ab_tests as ab_routes

        wav_path = tmp_config.paths.resolved_audio_input_dir / f"{meeting_id_with_merge}.wav"
        external = tmp_config.paths.resolved_base_dir / "external.wav"
        external.write_bytes(b"do not open")
        wav_path.unlink()
        wav_path.symlink_to(external)
        validate = MagicMock(side_effect=AssertionError("symlink target must not be decoded"))
        new_id = MagicMock(return_value="ab_20260409-143000_deadbeef")
        monkeypatch.setattr(ab_routes, "validate_audio_quality", validate)
        monkeypatch.setattr(ab_routes, "_runner_new_test_id", new_id)

        response = await async_client.post(
            "/api/ab-tests/stt",
            json=_stt_body(meeting_id_with_merge),
        )

        assert response.status_code == 400
        assert "SECURITY_BLOCKED" in response.json()["detail"]
        validate.assert_not_called()
        new_id.assert_not_called()
        assert external.read_bytes() == b"do not open"

    @pytest.mark.asyncio
    async def test_STT_AB는_gate중_변경된_파일을_409로_보류한다(
        self,
        async_client: httpx.AsyncClient,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """full gate 전후 fingerprint가 달라지면 task ID를 만들지 않는다."""
        import api.routers.ab_tests as ab_routes
        from core.audio_quality import AudioQualityResult, AudioQualityStatus

        wav_path = tmp_config.paths.resolved_audio_input_dir / f"{meeting_id_with_merge}.wav"

        def mutate_during_gate(*args: Any, **kwargs: Any) -> AudioQualityResult:
            wav_path.write_bytes(wav_path.read_bytes() + b"changed")
            return AudioQualityResult(
                status=AudioQualityStatus.ACCEPT,
                mean_volume_db=-20.0,
                duration_seconds=30.0,
            )

        validate = MagicMock(side_effect=mutate_during_gate)
        new_id = MagicMock(return_value="ab_20260409-143000_deadbeef")
        monkeypatch.setattr(ab_routes, "validate_audio_quality", validate)
        monkeypatch.setattr(ab_routes, "_runner_new_test_id", new_id)
        tmp_config.audio_quality.enabled = True

        response = await async_client.post(
            "/api/ab-tests/stt",
            json=_stt_body(meeting_id_with_merge),
        )

        assert response.status_code == 409
        assert "SOURCE_BUSY" in response.json()["detail"]
        validate.assert_called_once()
        new_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_202후_runner_재검증실패도_metadata_failed로_조회된다(
        self,
        async_client: httpx.AsyncClient,
        tmp_config: AppConfig,
        meeting_id_with_merge: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """route→runner 사이 source 변경이 반환된 test_id를 영구 404로 만들지 않는다."""
        from core.audio_quality import AudioFailureKind
        from steps.transcriber import AudioAdmissionError

        def fail_runner_reinspection(*args: Any, **kwargs: Any) -> None:
            raise AudioAdmissionError(
                "source changed after route admission",
                failure_kind=AudioFailureKind.SOURCE_BUSY,
            )

        monkeypatch.setattr(
            "core.ab_test_runner._inspect_stt_audio_source",
            fail_runner_reinspection,
        )

        response = await async_client.post(
            "/api/ab-tests/stt",
            json=_stt_body(meeting_id_with_merge),
        )

        assert response.status_code == 202
        test_id = response.json()["test_id"]
        metadata = await _wait_for_task_completion(tmp_config, test_id)
        assert metadata["status"] == "failed"
        assert "SOURCE_BUSY" in metadata["error"]


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

        resp = await async_client.post("/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge))
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

        resp = await async_client.post("/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge))
        tid = resp.json()["test_id"]
        await _wait_for_task_completion(tmp_config, tid)

        resp2 = await async_client.get(f"/api/ab-tests?source_meeting_id={meeting_id_with_merge}")
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

        resp = await async_client.post("/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge))
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
    async def test_path_traversal_거부(self, async_client: httpx.AsyncClient, bad_id: str) -> None:
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

        resp = await async_client.post("/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge))
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

        resp = await async_client.post("/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge))
        tid = resp.json()["test_id"]
        await _wait_for_task_completion(tmp_config, tid)
        cache_dir = ab_test_store.resolve_test_dir(tmp_config, tid) / ".openai-transcribe-parts"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached_response = cache_dir / "chunk.json"
        cached_response.write_text('{"response":"sensitive transcript"}', encoding="utf-8")

        resp2 = await async_client.delete(f"/api/ab-tests/{tid}")
        assert resp2.status_code == 204

        test_dir = ab_test_store.resolve_test_dir(tmp_config, tid)
        assert not test_dir.exists()
        assert not cached_response.exists()

    @pytest.mark.asyncio
    async def test_없는_테스트_삭제(self, async_client: httpx.AsyncClient) -> None:
        """존재하지 않는 test_id 삭제는 204 (idempotent)."""
        fake_id = new_test_id()
        resp = await async_client.delete(f"/api/ab-tests/{fake_id}")
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_진행중인_테스트는_삭제하지_않는다(
        self,
        async_client: httpx.AsyncClient,
        tmp_config: AppConfig,
    ) -> None:
        """외부 청크 전송 중인 저장소를 지우지 않고 먼저 취소하도록 요구한다."""
        test_id = new_test_id()
        test_dir = ab_test_store.create_test_dir(tmp_config, test_id)
        ab_test_store.write_metadata(
            tmp_config,
            test_id,
            {"test_id": test_id, "status": "running"},
        )

        response = await async_client.delete(f"/api/ab-tests/{test_id}")

        assert response.status_code == 409
        assert "취소 완료 후" in response.json()["detail"]
        assert test_dir.exists()


# ============================================================
# POST /api/ab-tests/{test_id}/cancel
# ============================================================


class TestCancelAbTest:
    @pytest.mark.asyncio
    async def test_취소_요청(
        self,
        async_client: httpx.AsyncClient,
        tmp_config: AppConfig,
    ) -> None:
        """유효한 test_id 에 대한 취소 요청 시 202."""
        tid = new_test_id()
        ab_test_store.create_test_dir(tmp_config, tid)
        ab_test_store.write_metadata(
            tmp_config,
            tid,
            {"test_id": tid, "status": "running", "error": None},
        )
        resp = await async_client.post(f"/api/ab-tests/{tid}/cancel")
        assert resp.status_code == 202
        data = resp.json()
        assert data["test_id"] == tid
        assert data["status"] == "cancelling"
        assert ab_test_store.read_metadata(tmp_config, tid)["status"] == "cancelling"

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

        resp = await async_client.post("/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge))
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
            resp = await client.post("/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge))
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

        resp = await async_client.post("/api/ab-tests/llm", json=_llm_body(meeting_id_with_merge))
        assert resp.status_code == 202
        tid = resp.json()["test_id"]
        meta = await _wait_for_task_completion(tmp_config, tid)
        assert meta["status"] == "completed"
