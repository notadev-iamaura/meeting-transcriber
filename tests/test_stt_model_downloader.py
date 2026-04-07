"""STT 모델 다운로더 테스트 (TDD)

STTModelDownloader 의 비동기 다운로드/양자화/검증 파이프라인을 검증한다.
huggingface_hub 및 subprocess 호출은 모두 mock 처리한다.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def tmp_models_dir(tmp_path):
    d = tmp_path / "stt_models"
    d.mkdir()
    return d


@pytest.fixture
def patched_specs(tmp_models_dir, monkeypatch):
    """STT_MODELS 의 로컬 경로를 tmp 디렉토리로 리다이렉트한다."""
    from core import stt_model_registry
    from core.stt_model_registry import STTModelSpec

    original = stt_model_registry.STT_MODELS
    patched: list[STTModelSpec] = []
    for spec in original:
        if spec.needs_quantization:
            patched.append(
                replace(
                    spec,
                    model_path=str(tmp_models_dir / spec.id),
                )
            )
        else:
            patched.append(spec)
    monkeypatch.setattr(stt_model_registry, "STT_MODELS", patched)
    return patched


@pytest.fixture
def downloader(tmp_models_dir, tmp_path, patched_specs):
    from core.stt_model_downloader import STTModelDownloader

    fake_mlx_examples = tmp_path / "mlx-examples" / "whisper"
    fake_mlx_examples.mkdir(parents=True)
    (fake_mlx_examples / "convert.py").write_text("# fake\n")
    return STTModelDownloader(
        models_dir=tmp_models_dir,
        mlx_examples_path=fake_mlx_examples,
    )


# ============================================================
# Tests
# ============================================================


class TestSTTModelDownloader:
    async def test_다운로드_시작시_job_id_반환(self, downloader, monkeypatch):
        """start_download 호출 시 job_id를 즉시 반환해야 한다."""
        async def fake_hf(spec, job):
            await asyncio.sleep(0.05)

        async def fake_quant(spec, job):
            # 검증 통과를 위해 가짜 산출물 생성
            path = Path(spec.model_path).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            (path / "config.json").write_text("{}")
            (path / "weights.safetensors").write_bytes(b"x" * 16)

        monkeypatch.setattr(downloader, "_hf_download", fake_hf)
        monkeypatch.setattr(downloader, "_quantize", fake_quant)

        job_id = await downloader.start_download("seastar-medium-4bit")
        assert job_id.startswith("stt-download-seastar-medium-4bit-")

        progress = downloader.get_progress("seastar-medium-4bit")
        assert progress is not None
        assert progress.model_id == "seastar-medium-4bit"

        # 백그라운드 태스크 완료 대기
        await downloader.wait_for("seastar-medium-4bit")

    async def test_동일_모델_중복_다운로드시_Conflict(self, downloader, monkeypatch):
        from core.stt_model_downloader import DownloadConflictError

        async def slow_hf(spec, job):
            await asyncio.sleep(0.3)

        async def fake_quant(spec, job):
            path = Path(spec.model_path).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            (path / "config.json").write_text("{}")
            (path / "weights.safetensors").write_bytes(b"x")

        monkeypatch.setattr(downloader, "_hf_download", slow_hf)
        monkeypatch.setattr(downloader, "_quantize", fake_quant)

        await downloader.start_download("seastar-medium-4bit")
        with pytest.raises(DownloadConflictError):
            await downloader.start_download("seastar-medium-4bit")

        await downloader.wait_for("seastar-medium-4bit")

    async def test_다운로드_완료시_status_READY(self, downloader, monkeypatch):
        from core.stt_model_status import ModelStatus

        async def fake_hf(spec, job):
            pass

        async def fake_quant(spec, job):
            path = Path(spec.model_path).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            (path / "config.json").write_text("{}")
            (path / "weights.safetensors").write_bytes(b"x" * 8)

        monkeypatch.setattr(downloader, "_hf_download", fake_hf)
        monkeypatch.setattr(downloader, "_quantize", fake_quant)

        await downloader.start_download("seastar-medium-4bit")
        await downloader.wait_for("seastar-medium-4bit")

        progress = downloader.get_progress("seastar-medium-4bit")
        assert progress.status == ModelStatus.READY
        assert progress.progress_percent == 100
        assert progress.completed_at is not None
        assert progress.error_message is None

    async def test_HF_다운로드_실패시_ERROR(self, downloader, monkeypatch):
        from core.stt_model_status import ModelStatus

        async def failing_hf(spec, job):
            raise RuntimeError("네트워크 오류")

        monkeypatch.setattr(downloader, "_hf_download", failing_hf)

        await downloader.start_download("seastar-medium-4bit")
        await downloader.wait_for("seastar-medium-4bit")

        progress = downloader.get_progress("seastar-medium-4bit")
        assert progress.status == ModelStatus.ERROR
        assert "네트워크 오류" in progress.error_message

    async def test_양자화_실패시_ERROR(self, downloader, monkeypatch):
        from core.stt_model_status import ModelStatus

        async def fake_hf(spec, job):
            pass

        async def failing_quant(spec, job):
            raise RuntimeError("양자화 실패")

        monkeypatch.setattr(downloader, "_hf_download", fake_hf)
        monkeypatch.setattr(downloader, "_quantize", failing_quant)

        await downloader.start_download("seastar-medium-4bit")
        await downloader.wait_for("seastar-medium-4bit")

        progress = downloader.get_progress("seastar-medium-4bit")
        assert progress.status == ModelStatus.ERROR
        assert "양자화" in progress.error_message

    async def test_needs_quantization_False는_양자화_스킵(
        self, downloader, monkeypatch
    ):
        """komixv2는 needs_quantization=False 이므로 _quantize가 호출되지 않아야 한다."""
        from core.stt_model_status import ModelStatus

        hf_called = {"count": 0}
        quant_called = {"count": 0}

        async def fake_hf(spec, job):
            hf_called["count"] += 1

        async def fake_quant(spec, job):
            quant_called["count"] += 1

        # _verify는 HF 경로 기반 komixv2에 대해 True를 반환하도록 패치
        monkeypatch.setattr(downloader, "_hf_download", fake_hf)
        monkeypatch.setattr(downloader, "_quantize", fake_quant)
        monkeypatch.setattr(downloader, "_verify", lambda spec: True)

        await downloader.start_download("komixv2")
        await downloader.wait_for("komixv2")

        progress = downloader.get_progress("komixv2")
        assert progress.status == ModelStatus.READY
        assert hf_called["count"] == 1
        assert quant_called["count"] == 0

    async def test_verify_weights_safetensors_존재_확인(
        self, downloader, patched_specs
    ):
        """_verify 는 weights.safetensors + config.json 존재를 체크한다."""
        from core.stt_model_registry import get_by_id

        spec = get_by_id("seastar-medium-4bit")
        assert not downloader._verify(spec)

        path = Path(spec.model_path).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        (path / "config.json").write_text("{}")
        assert not downloader._verify(spec)  # weights 없음

        (path / "weights.safetensors").write_bytes(b"x")
        assert downloader._verify(spec)

    async def test_진행률_0에서_100까지_단조증가(self, downloader, monkeypatch):
        """진행률이 중간(>=50) 을 거쳐 100 으로 끝나야 한다."""
        observed: list[int] = []

        async def fake_hf(spec, job):
            observed.append(job.progress_percent)

        async def fake_quant(spec, job):
            observed.append(job.progress_percent)
            path = Path(spec.model_path).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            (path / "config.json").write_text("{}")
            (path / "weights.safetensors").write_bytes(b"x")

        monkeypatch.setattr(downloader, "_hf_download", fake_hf)
        monkeypatch.setattr(downloader, "_quantize", fake_quant)

        await downloader.start_download("seastar-medium-4bit")
        await downloader.wait_for("seastar-medium-4bit")

        progress = downloader.get_progress("seastar-medium-4bit")
        assert progress.progress_percent == 100
        # _hf_download 진입 시점 < _quantize 진입 시점
        assert observed[0] < observed[1]
        assert observed[1] >= 50
